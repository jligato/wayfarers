"""
F4: Inventory / Item System (server-side).

References:
- docs/feature-roadmap.md §F4 (line 119): Inventory/Item System — consumable items
  (cultivation aids, beast food, battle potions), inventory API, item-use endpoints.
- Depends: Wallet (currency for purchase), F1 (Sect contribution items), F3 (Missions).

Full implementation: item definitions loaded from JSON, PG-backed inventory store,
item use effects, shop integration.
"""
from __future__ import annotations
from copy import deepcopy
import json
import math
import threading
import time
import uuid
from typing import Optional, Dict, List, Any
from pathlib import Path
from pydantic import BaseModel, Field

from wallet_dispatch import spend as wallet_spend, earn as wallet_earn

# Largest number of items one /inventory/use call may consume. The route credits
# one item effect per call regardless of quantity, so this is a sanity ceiling on
# the request, not a rate limit -- the real bound on earning is ownership.
# Change it only alongside a real client that stacks a use.
MAX_USE_QUANTITY = 99

# Bag capacity for a player who has never used an expansion scroll. The scroll
# effect WRITES `inventory_capacity` onto the player's own record, and
# `InventoryStore.get_player_capacity` READS it back from there. Both sides
# import this one constant, so the writer and the reader cannot drift apart
# again -- which is exactly how the scroll became a paid no-op once already.
BASE_INVENTORY_CAPACITY = 50

# The most slots an expansion scroll may ever buy. Capacity was an unbounded
# number a player could raise forever with premium currency, which left "full"
# meaningless at every future point. The ceiling is what makes it a finite
# resource the design can reason about. It sits beside BASE_INVENTORY_CAPACITY
# deliberately: both are capacity numbers, and the one that writes capacity and
# the one that reads it are the same file.
MAX_INVENTORY_SLOTS = 150

# Slots one expansion scroll adds.
EXPANSION_SLOTS_PER_SCROLL = 10

# The catalogue id of the scroll that grows the bag. Named once so the ceiling
# arithmetic and the shop guard cannot disagree with each other about which
# item is "the expansion scroll".
EXPANSION_SCROLL_ITEM = "expansion_scroll"

# Largest quantity one /inventory/discard call may destroy. Matched to the
# biggest stack ceiling in the catalogue so a full material stack can be
# cleared in one call rather than a thousand.
MAX_DISCARD_QUANTITY = 999

# The canonical rarity order, best first. ONE list, used by the sort and by the
# client's rarity filter. Two lists of the same three values drift apart, and a
# drift here shows the player a rare item in the common section.
RARITY_ORDER = ("rare", "uncommon", "common")

# The canonical type order. Unknown sinks to the bottom so a bad item id is
# visible instead of quietly hidden at the top. Equipment has no rows today,
# so that slot costs nothing and is ready the day equipment enters the bag.
TYPE_ORDER = ("consumable", "material", "equipment")


def _rarity_rank(rarity: str) -> int:
    """Best-first position of a rarity; anything unknown sorts last."""
    try:
        return RARITY_ORDER.index(rarity)
    except ValueError:
        return len(RARITY_ORDER)


def _type_rank(item_type: str) -> int:
    """Position of an item type; anything unknown sorts last."""
    try:
        return TYPE_ORDER.index(item_type)
    except ValueError:
        return len(TYPE_ORDER)


# The one canonical order, applied by `sort_inventory` so the database branch
# and the in-memory branch cannot disagree. The first rule that separates two
# items decides: type, then rarity best-first, then name, then item id. The
# id is the final tie-break so two items sharing a name cannot swap places
# between visits.
def _sort_key_default(row: dict) -> tuple:
    return (
        _type_rank(row.get("type", "")),
        _rarity_rank(row.get("rarity", "")),
        str(row.get("name", "")),
        str(row.get("item_id", "")),
    )


def _sort_key_rarity(row: dict) -> tuple:
    return (
        _rarity_rank(row.get("rarity", "")),
        -int(row.get("quantity", 0) or 0),
        str(row.get("name", "")),
        str(row.get("item_id", "")),
    )


def _sort_key_newest(row: dict) -> tuple:
    """Newest first. A save field, not a contract.

    `float()` raises OverflowError on a number past the range of a
    double. An infinity or a NaN is a different failure: neither
    raises, and both poison the comparison -- `-inf` pins one row
    first forever and NaN makes every comparison False, so the bag
    order depends on which row the sort happened to touch first.
    An unreadable or non-finite timestamp sorts as 0 (oldest),
    which is where an unknown time belongs, and never takes the bag
    view down with it.
    """
    try:
        acquired_at = float(row.get("acquired_at", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        acquired_at = 0.0
    if not math.isfinite(acquired_at):
        acquired_at = 0.0
    return (-acquired_at, str(row.get("item_id", "")))


def _sort_key_quantity(row: dict) -> tuple:
    """Largest stack first. A save field, not a contract.

    `int()` raises OverflowError on an infinity, which is a save row
    taking the whole bag view down over one unreadable number. An
    unreadable quantity sorts as 0 (smallest), so the bag still
    renders and every readable stack keeps its place.
    """
    try:
        quantity = int(row.get("quantity", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        quantity = 0
    return (
        -quantity,
        str(row.get("name", "")),
        str(row.get("item_id", "")),
    )


#: Player-chosen orders accepted by `sort_inventory`. Anything else falls back
#: to the default rather than raising, so a client that invents a value gets
#: the bag in a stable order instead of an error.
SORT_ORDERS = {
    "default": _sort_key_default,
    "rarity": _sort_key_rarity,
    "newest": _sort_key_newest,
    "quantity": _sort_key_quantity,
}


def sort_inventory(rows: List[dict], order: str = "default") -> List[dict]:
    """Return `rows` in one of SORT_ORDERS. The single owner of bag order."""
    key = SORT_ORDERS.get(order or "default", _sort_key_default)
    return sorted(rows, key=key)


# ------------------------------------------------------------------
# Item Definition Loading
# ------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent / "shared"

def load_item_definitions() -> Dict[str, dict]:
    """Load item definitions from shared/items/items.json."""
    path = DATA_DIR / "items" / "items.json"
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return {item["id"]: item for item in data.get("items", [])}


ITEM_DEFINITIONS = load_item_definitions()


# ------------------------------------------------------------------
# Item Effect Handlers
# ------------------------------------------------------------------
def apply_item_effect(player: dict, item_def: dict, wallet_store=None) -> dict:
    """Apply item effect to player. Returns result dict."""
    effect = item_def.get("effect", {})
    effect_type = effect.get("type")
    
    if effect_type == "cultivation_progress":
        # Grant cultivation progress
        progress_gained = effect.get("value", 0)
        player["cultivation_progress"] = player.get("cultivation_progress", 0) + progress_gained
        return {"ok": True, "effect": "cultivation_progress", "progress_gained": progress_gained}
    
    elif effect_type == "breakthrough_difficulty_reduction":
        # Store temporary breakthrough bonus
        bonus = effect.get("value", 0)
        buffs = player.setdefault("active_buffs", {})
        buffs["breakthrough_difficulty_reduction"] = buffs.get("breakthrough_difficulty_reduction", 0) + bonus
        return {"ok": True, "effect": "breakthrough_bonus", "bonus": bonus}
    
    elif effect_type == "beast_growth":
        # Grant beast growth (stored for beast system to pick up)
        growth = effect.get("value", 0)
        player["beast_food_buffer"] = player.get("beast_food_buffer", 0) + growth
        return {"ok": True, "effect": "beast_growth", "growth_gained": growth}
    
    elif effect_type == "combat_qi_restore":
        # Store combat consumable for combat system
        player.setdefault("combat_consumables", []).append({
            "type": "qi_restore",
            "value": effect.get("value", 0.5),
            "item_id": item_def["id"],
            "used_at": time.time()
        })
        return {"ok": True, "effect": "combat_qi_restore_queued"}
    
    elif effect_type == "combat_hp_restore":
        player.setdefault("combat_consumables", []).append({
            "type": "hp_restore",
            "value": effect.get("value", 0.5),
            "item_id": item_def["id"],
            "used_at": time.time()
        })
        return {"ok": True, "effect": "combat_hp_restore_queued"}
    
    elif effect_type == "combat_atk_buff":
        player.setdefault("combat_consumables", []).append({
            "type": "atk_buff",
            "value": effect.get("value", 0.3),
            "duration_turns": effect.get("duration_turns", 3),
            "item_id": item_def["id"],
            "used_at": time.time()
        })
        return {"ok": True, "effect": "combat_atk_buff_queued"}
    
    elif effect_type == "combat_shield":
        player.setdefault("combat_consumables", []).append({
            "type": "shield",
            "value": effect.get("value", 0.2),
            "duration_turns": effect.get("duration_turns", 2),
            "item_id": item_def["id"],
            "used_at": time.time()
        })
        return {"ok": True, "effect": "combat_shield_queued"}
    
    elif effect_type == "combat_flee":
        player.setdefault("combat_consumables", []).append({
            "type": "flee",
            "guaranteed": effect.get("guaranteed", True),
            "item_id": item_def["id"],
            "used_at": time.time()
        })
        return {"ok": True, "effect": "combat_flee_queued"}
    
    elif effect_type == "teleport_sect":
        # Teleport handled client-side; just mark as used
        return {"ok": True, "effect": "teleport_sect", "message": "Use from world map to return to sect"}
    
    elif effect_type == "sect_contribution":
        # Grant sect contribution (requires sect membership)
        contribution = effect.get("value", 0)
        if player.get("sect_id"):
            player["sect_contribution"] = player.get("sect_contribution", 0) + contribution
            # Check rank promotion
            if player["sect_contribution"] >= 1000 and player.get("sect_rank") == "outer_disciple":
                player["sect_rank"] = "inner_disciple"
            return {"ok": True, "effect": "sect_contribution", "contribution_gained": contribution}
        return {"ok": False, "error": "Not in a sect"}
    
    elif effect_type == "inventory_expand":
        # Expand inventory capacity, refusing at the ceiling.
        #
        # This handler WRITES `inventory_capacity` and `get_player_capacity`
        # READS it back, which is why they share BASE_INVENTORY_CAPACITY. They
        # now share the ceiling for the same reason: capacity has to stop
        # somewhere, and a check that lives in one place the other cannot see
        # is how the scroll became a paid no-op once already.
        #
        # `new_capacity` in the answer below is the value the bag will enforce,
        # because it is the same value the reader falls back to.
        expand = effect.get("value", 0)
        current = player.get("inventory_capacity", BASE_INVENTORY_CAPACITY)
        try:
            current = int(current)
        except (TypeError, ValueError, OverflowError):
            current = BASE_INVENTORY_CAPACITY
        if current >= MAX_INVENTORY_SLOTS:
            return {
                "ok": False,
                "error": "at_capacity_ceiling",
                "slots": current,
                "max_slots": MAX_INVENTORY_SLOTS,
            }
        # A single use may not cross the ceiling either: one scroll is one
        # step, and the last permitted step is clamped rather than refused so
        # the answer the player gets is the one their bag will hold.
        player["inventory_capacity"] = min(current + expand, MAX_INVENTORY_SLOTS)
        return {
            "ok": True,
            "effect": "inventory_expanded",
            "new_capacity": player["inventory_capacity"],
            "max_slots": MAX_INVENTORY_SLOTS,
        }
    
    return {"ok": False, "error": f"Unknown effect type: {effect_type}"}


def _whole_quantity(value: Any) -> Optional[int]:
    """A caller-supplied count, or None when it is negative or not a whole number.

    One helper for every count that reaches the store, because "is this number
    sane" was being answered in the request models (three times) and in two
    route handlers (twice), and NOWHERE in the store methods those guards call.
    The floor that protects a paid good belongs on the store method itself: the
    models' bounds only run when a web framework parses a body, and every
    in-process caller -- a scheduled job, a refund path, another module --
    bypasses them entirely.

    ZERO IS ALLOWED, and that is a deliberate decision rather than a loosened
    rule. Zero moves no units in either direction, so it is an honest no-op,
    and real callers depend on it: a craft sweep seeds `cost - 1`, which reaches
    0 on a one-unit recipe, and a shop test computes `ceiling - 1`, which
    reaches 0 on a stack ceiling of 1. Refusing zero turned two passing suites
    red. Only a NEGATIVE count is a defect, because a negative is a subtraction
    that adds.

    Non-integers are refused here too, rather than left to raise. `int()` used
    to be the first thing that touched an amount, so a string, None, a list or
    a dict escaped as an uncaught TypeError or ValueError out of a route,
    where a refusal the caller can answer is what belongs.
    """
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # A fractional number is not a COUNT. `int()` used to truncate it, so
    # `add_item(..., 2.7)` answered `requested: 2, granted: 2` -- the caller
    # asked for 2.7 and every number in the receipt named a different one.
    # `discard_item` was the worse case: it receipted `discarded: 1.5` for a
    # stack that lost exactly one unit.
    try:
        if float(value) != count:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if count < 0:
        return None
    return count


def _capacity_written_to(player: dict) -> Optional[int]:
    """The capacity `apply_item_effect` just wrote on `player`, or None.

    Reads the dict the handler was given rather than the store, because that is
    the object the write landed on. See `expand_capacity` for the receipt this
    makes honest.
    """
    if not isinstance(player, dict) or "inventory_capacity" not in player:
        return None
    try:
        return int(player["inventory_capacity"])
    except (TypeError, ValueError, OverflowError):
        return None


def _inventory_row(item_id: str, quantity: int, acquired_at: float,
                   item_def: dict) -> dict:
    """The ONE shape every bag row has, for both stores.

    Built in one place so the database branch and the in-memory branch cannot
    answer with different keys. `useable` is derived here from whether the item
    HAS an effect, so the client never has to know the catalogue's internals to
    grey out a button -- and so an item with an unknown effect type reports
    `useable: false` rather than inviting a use that will be refused.
    """
    return {
        "item_id": item_id,
        "quantity": quantity,
        "acquired_at": acquired_at,
        "name": item_def.get("name", item_id),
        "type": item_def.get("type", "unknown"),
        "rarity": item_def.get("rarity", "common"),
        "description": item_def.get("description", ""),
        "stack_size": item_def.get("stack_size", 99),
        "useable": bool(item_def.get("effect")),
        "discardable": not bool(item_def.get("effect")),
    }


def _grant_receipt(item_id: str, requested: int, held_before: int, new_qty: int,
                   max_stack: int) -> dict:
    """The one shape every add_item success answers in.

    `granted` is what THIS call put in the bag; `discarded` is what the stack
    ceiling ate. A caller that charges the player for `requested` while reading
    only `ok` is how a full stack turns paid goods into nothing, so the two
    counts travel together on every success and a caller physically cannot ask
    "did it all arrive?" without being handed the answer.
    """
    granted = max(0, new_qty - held_before)
    discarded = max(0, requested - granted)
    return {
        "ok": True,
        "item_id": item_id,
        "requested": requested,
        "granted": granted,
        "discarded": discarded,
        "stack_full": discarded > 0,
        "quantity": new_qty,
        "max_stack": max_stack,
    }


# ------------------------------------------------------------------
# PG-backed Inventory Store
# ------------------------------------------------------------------
class InventoryStore:
    """
    PostgreSQL-backed player inventory store.
    Falls back to in-memory when PG not available (dev mode).
    """
    def __init__(self, pg_pool=None, player_store=None):
        self.pg_pool = pg_pool
        self.player_store = player_store
        # In-memory fallback: player_id -> {item_id: {quantity, acquired_at}}
        self._mem_inventory: Dict[str, Dict[str, dict]] = {}
        # Serializes the in-memory read-modify-write below. The Postgres branch
        # gets this atomicity from a single INSERT ... ON CONFLICT statement;
        # the in-memory branch gets it from here. Without it two overlapping
        # adds both read the same held count and the second write clobbers the
        # first: the receipt already said `granted: 1`, so a paid caller credits
        # the player for a unit that is not in the bag. Held per STORE, not per
        # player, so it costs nothing per call and cannot deadlock.
        self._mem_lock = threading.Lock()
        if pg_pool:
            self._init_schema()

    def _init_schema(self):
        """Create inventory table if not exists."""
        try:
            conn = self.pg_pool.getconn() if hasattr(self.pg_pool, "getconn") else self.pg_pool
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS player_inventory (
                    player_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    acquired_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (player_id, item_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_inventory_player
                ON player_inventory (player_id)
            """)
            conn.commit()
            cur.close()
            if hasattr(self.pg_pool, "putconn"):
                self.pg_pool.putconn(conn)
        except Exception:
            pass

    def _get_connection(self):
        if self.player_store is not None and self.player_store.current_connection is not None:
            return self.player_store.current_connection
        if self.pg_pool:
            return self.pg_pool.getconn() if hasattr(self.pg_pool, "getconn") else self.pg_pool
        return None

    def _release_connection(self, conn):
        if conn is None:
            return
        if self.player_store is not None and conn is self.player_store.current_connection:
            return
        if self.pg_pool and hasattr(self.pg_pool, "putconn"):
            self.pg_pool.putconn(conn)

    def _commit(self, conn):
        if self.player_store is not None and conn is self.player_store.current_connection:
            return
        conn.commit()

    def snapshot(self) -> Dict[str, Dict[str, dict]]:
        """Detached copy of every in-memory bag, for the rollback path.

        Mirrors the wallet store's `snapshot`/`restore` pair exactly: a
        deepcopy out, a deepcopy back in. The in-memory bag is the ONLY
        inventory state a transaction can lose, because the PG branch rides
        the player's own connection and is undone by `conn.rollback()`.

        Without this pair, `PlayerStore.transaction()` reverts the wallet and
        the player rows but leaves `_mem_inventory` mutated, so a route that
        spends an item and then raises keeps the debit. Deepcopy on BOTH ends
        means the snapshot cannot be aliased by a later in-place write.
        """
        with self._mem_lock:
            return deepcopy(self._mem_inventory)

    def restore(self, snapshot: Optional[Dict[str, Dict[str, dict]]]) -> None:
        """Reinstate a `snapshot()` after a failed transaction.

        `None` means "nothing was captured" (the transaction never reached the
        inventory), so there is nothing to put back and we must not clear the
        bag to empty -- an absent snapshot is not an empty inventory.
        """
        if snapshot is None:
            return
        with self._mem_lock:
            self._mem_inventory.clear()
            self._mem_inventory.update(deepcopy(snapshot))

    def add_item(self, player_id: str, item_id: str, quantity: int = 1) -> dict:
        """Add items to a player's bag, never dropping a unit without saying so.

        A stack has a hard ceiling, so an order bigger than the room left is
        served PARTIALLY: the units that fit are kept, and the shortfall comes
        back as `discarded` in the result. The ceiling is not the defect; a bare
        success flag beside it is. Every success therefore reports `requested`,
        `granted` and `discarded` (see `_grant_receipt`), and the paid caller
        refunds the difference before it replies.

        A row already over its ceiling (a `stack_size` reduced in a catalogue
        update, or legacy data) is never SHRUNK by an add: the ceiling only
        stops growth, it never deletes what is already held.

        EDGE BOUND. An add is a GRANT, so a quantity that is not positive is not
        a small grant -- it is a withdrawal that reports success. Measured on the
        in-memory branch: holding 10 bones, `add_item(..., -5)` returned
        `ok: True, granted: 0, discarded: 0` and left 5 in the bag. Five units
        vanished and every number in the receipt said nothing had happened,
        because `_grant_receipt` clamps both counts at zero and a negative
        quantity drives both negative. A caller that reads `ok`, or that refunds
        the `discarded` shortfall, cannot see the loss at all.
        """
        if item_id not in ITEM_DEFINITIONS:
            return {"ok": False, "error": "Invalid item"}

        count = _whole_quantity(quantity)
        if count is None:
            return {"ok": False, "error": "Quantity must be a whole number of 0 or more"}
        quantity = count

        item_def = ITEM_DEFINITIONS[item_id]
        max_stack = item_def.get("stack_size", 99)

        if self.pg_pool:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                # One statement, so the held-before count and the write cannot
                # disagree: `prior` reads the row the same INSERT ... ON CONFLICT
                # is about to update, and `upserted` returns the count that
                # actually landed. GREATEST keeps an over-full legacy row from
                # losing units on the way in.
                cur.execute("""
                    WITH prior AS (
                        SELECT quantity AS held FROM player_inventory
                        WHERE player_id = %s AND item_id = %s
                    ), upserted AS (
                        INSERT INTO player_inventory (player_id, item_id, quantity, acquired_at)
                        VALUES (%s, %s, LEAST(%s, %s), %s)
                        ON CONFLICT (player_id, item_id) DO UPDATE SET
                            quantity = LEAST(player_inventory.quantity + EXCLUDED.quantity,
                                            GREATEST(%s, player_inventory.quantity)),
                            acquired_at = EXCLUDED.acquired_at
                        RETURNING quantity AS held_after
                    )
                    SELECT upserted.held_after, COALESCE((SELECT held FROM prior), 0)
                    FROM upserted
                """, (player_id, item_id,
                      player_id, item_id, quantity, max_stack, time.time(),
                      max_stack))
                row = cur.fetchone()
                self._commit(conn)
                cur.close()
                if row is None:
                    # No row came back: nothing was written, so nothing arrived.
                    return {"ok": False, "error": "Inventory write returned no row"}
                return _grant_receipt(item_id, quantity, int(row[1] or 0), int(row[0]), max_stack)
            finally:
                self._release_connection(conn)
        else:
            # One lock across the read AND the write, not around each. The
            # receipt is derived from the same held count the write used, so
            # `granted` describes a unit that is genuinely in the bag.
            with self._mem_lock:
                inv = self._mem_inventory.setdefault(player_id, {})
                current = inv.get(item_id, {"quantity": 0, "acquired_at": time.time()})
                held = int(current["quantity"])
                new_qty = min(held + quantity, max(max_stack, held))
                inv[item_id] = {"quantity": new_qty, "acquired_at": time.time()}
                return _grant_receipt(item_id, quantity, held, new_qty, max_stack)

    def remove_item(self, player_id: str, item_id: str, quantity: int = 1) -> dict:
        """Remove items, refusing an overshoot and never storing a zero.

        The emptying case DELETEs the row instead of updating it to zero. It
        used to update first and delete second, which wrote a transient
        quantity of 0 -- and the `player_inventory_quantity_positive` check
        constraint in migration 012 refuses that write, so emptying the last
        unit of a stack raised CheckViolation instead of freeing the slot. That
        constraint is the right one: a stored zero is indistinguishable from a
        bug to the next reader, and it is what makes a negative quantity the
        alarming thing it should be. So the delete is expressed directly rather
        than relaxing the check.

        Measured on real PostgreSQL before and after the change: the old
        statement raised
        `psycopg2.errors.CheckViolation: new row for relation
        "player_inventory" violates check constraint
        "player_inventory_quantity_positive"`, DETAIL `Failing row contains
        (..., beast_bone, 0, ...)`.

        EDGE BOUND, and the floor sits ABOVE the branch because both branches
        minted. A negative quantity is a subtraction that adds. Measured on the
        in-memory branch: holding 10 bones, `remove_item(..., -3)` returned
        `ok: True, remaining: 13` -- thirteen bones from ten. The database branch
        mints identically: the DELETE cannot match a negative, and the UPDATE's
        `quantity > %s` guard becomes `quantity > -3`, true for every row in the
        bag, so it executes `quantity = quantity - (-3)`. The positive-quantity
        check constraint cannot catch this one, because the row is only ever
        made BIGGER and stays perfectly legal. Refusing here also stops the two
        branches from disagreeing about the same request.
        """
        count = _whole_quantity(quantity)
        if count is None:
            return {"ok": False, "error": "Quantity must be a whole number of 0 or more"}
        quantity = count

        if self.pg_pool:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                # Two branches, one statement each. The DELETE carries its own
                # `quantity >= %s` guard so it can only ever fire on a stack
                # that is genuinely being emptied, and it returns the count it
                # removed so `remaining` is still derived from the real write.
                cur.execute("""
                    DELETE FROM player_inventory
                    WHERE player_id = %s AND item_id = %s AND quantity = %s
                    RETURNING quantity
                """, (player_id, item_id, quantity))
                emptied = cur.fetchone()
                if emptied is not None:
                    self._commit(conn)
                    cur.close()
                    return {"ok": True, "item_id": item_id, "remaining": 0}
                cur.execute("""
                    UPDATE player_inventory
                    SET quantity = quantity - %s
                    WHERE player_id = %s AND item_id = %s AND quantity > %s
                    RETURNING quantity
                """, (quantity, player_id, item_id, quantity))
                row = cur.fetchone()
                if row:
                    new_qty = row[0]
                    self._commit(conn)
                    cur.close()
                    return {"ok": True, "item_id": item_id, "remaining": max(0, new_qty)}
                cur.close()
                return {"ok": False, "error": "Insufficient quantity"}
            finally:
                self._release_connection(conn)
        else:
            inv = self._mem_inventory.get(player_id, {})
            if item_id not in inv or inv[item_id]["quantity"] < quantity:
                return {"ok": False, "error": "Insufficient quantity"}
            inv[item_id]["quantity"] -= quantity
            if inv[item_id]["quantity"] <= 0:
                del inv[item_id]
            return {"ok": True, "item_id": item_id, "remaining": inv.get(item_id, {}).get("quantity", 0)}

    def ensure_player_inventory(self, player_id: str, legacy_inventory: Optional[dict] = None) -> dict:
        """Migrate legacy player.inventory materials into the canonical store once.

        This method OWNS the one-time marker. It used to write
        `__canonical_inventory_migrated` into whatever dict the player read
        returned and leave saving to the caller, which made the "once" in
        "once" a property of the caller rather than of this method. MEASURED:
        a caller that read the player, called this, and never wrote the record
        back left the marker in a detached copy, so a second lift ran again
        and a 3-unit legacy stack became 6 -- the same units granted twice.
        A PlayerStore read outside a transaction is copy-on-read by design, so
        the mutation was simply discarded every time.

        The fix is that the method makes the marker durable itself:

        * inside a caller-owned transaction it only edits the staged row, and
          that transaction's commit persists the marker -- nothing is committed
          here, so the caller's rollback still unwinds the marker too;
        * outside one it opens its own scope, so the marker is always saved and
          never depends on the caller remembering to save.

        A player that does not exist is not migrated and does not raise: there
        is nothing to lift for them.
        """
        store = self.player_store
        in_caller_transaction = store is not None and getattr(store, "_state", None) is not None
        if store is not None:
            if legacy_inventory is None:
                with store.transaction(player_id):
                    player = store[player_id]
                    return self.ensure_player_inventory(player_id, player.get("inventory", {}))
            if not in_caller_transaction:
                with store.transaction():
                    return self.ensure_player_inventory(player_id, legacy_inventory)
        legacy = legacy_inventory if isinstance(legacy_inventory, dict) else {}
        marker = "__canonical_inventory_migrated"
        player = None
        if store is not None:
            try:
                player = store[player_id]
            except KeyError:
                return {"migrated": False, "player": None}
            if player.get(marker):
                return {"migrated": False, "player": player}
        for item_id, quantity in list(legacy.items()):
            if item_id in ITEM_DEFINITIONS and int(quantity or 0) > 0:
                self.add_item(player_id, item_id, int(quantity))
        if player is not None:
            player[marker] = True
        return {"migrated": True, "player": player}

    def consume_items(self, player_id: str, costs: dict) -> dict:
        """Consume several canonical items atomically within the caller transaction."""
        if not isinstance(costs, dict):
            return {"ok": False, "error": "Invalid material cost"}
        # Every amount is normalised ONCE, up front, and a zero is DROPPED here
        # rather than skipped below. `int(amount)` used to be called twice per
        # item -- once to validate, once to remove -- and it was the first thing
        # that touched the value, so anything not int-able escaped as an
        # uncaught ValueError or TypeError. A cost table assembled from
        # configuration reaches this method with a string in it sooner or later,
        # and an exception here is a 500 nobody can answer, where a refusal is
        # a 400 the caller can.
        amounts = {}
        for item_id, amount in costs.items():
            if item_id not in ITEM_DEFINITIONS:
                return {"ok": False, "error": f"Invalid material {item_id}"}
            count = _whole_quantity(amount)
            if count is None:
                return {"ok": False, "error": f"Invalid material {item_id}"}
            if count == 0:
                continue
            amounts[item_id] = count
        for item_id, count in amounts.items():
            if self.get_item_count(player_id, item_id) < count:
                return {"ok": False, "error": f"Insufficient {item_id}"}
        remaining = {}
        # ALL the lines, or NONE of them.
        #
        # The pre-check above asks "does the player hold this count of
        # this item", one item at a time, and then this loop spends them
        # one at a time. Those two questions disagree the moment anything
        # can change between them -- a second request, a callback, a
        # store that refuses the second line for a reason the count check
        # cannot see. Then the FIRST lines are already gone from the bag,
        # this method returns ok: False, and the caller reads a plain
        # refusal: no receipt, no trace, no way to tell the player their
        # materials were silently destroyed. Measured on the in-memory
        # branch with costs {beast_bone: 2, taming_bait: 1} and a refusal
        # injected on the second remove: the bones went 5 -> 3 and stayed
        # at 3 while the return said only "Insufficient taming_bait".
        #
        # So each successful removal is recorded and, on any refusal, put
        # back before returning -- the same all-or-nothing shape
        # server/trading_market.py uses for escrow. The units being restored
        # were in this bag moments ago, so the write-back cannot overflow a
        # stack that held them before and cannot be partial either.
        #
        # This is deliberately INSIDE the store, not at each call site: the
        # outer players_db.transaction() restores the bag too, but only
        # when the caller RAISES. A caller that returns a refusal dict
        # (server/main.py /beasts/tame does) commits the transaction with
        # the partial spend already standing.
        spent: List[tuple] = []

        def refund() -> None:
            for spent_id, spent_count in spent:
                self.add_item(player_id, spent_id, spent_count)

        try:
            for item_id, count in amounts.items():
                result = self.remove_item(player_id, item_id, count)
                if not result.get("ok"):
                    refund()
                    return {"ok": False,
                            "error": result.get("error",
                                                f"Failed to consume {item_id}")}
                spent.append((item_id, count))
                remaining[item_id] = result.get("remaining", 0)
        except BaseException:
            # A REFUSAL is a returned dict; a FAILURE is a raised one. The
            # write-back only ever saw the first shape, because a raise
            # unwound straight past it and out of this method with the
            # earlier lines already debited. `remove_item` raises for real
            # reasons -- a database error mid-statement, a failed write, a
            # check constraint -- and none of them are refusals the caller
            # can recognise, so without this the player simply loses the
            # items and the exception says nothing about the bag.
            #
            # Measured on the in-memory branch: costs
            # {beast_bone: 2, taming_bait: 1} where the SECOND removal
            # RAISED instead of refusing. The method propagated the
            # RuntimeError and the bones stayed at 3 of 5 -- a real loss
            # produced by a real fault, with no receipt and no refusal.
            #
            # The refund runs BEFORE the re-raise, because once the
            # exception leaves this method nothing in it can put the units
            # back. It is deliberately NOT wrapped in its own try/except: if
            # a refund itself fails, the player is owed those units and that
            # second failure must stay visible instead of being swallowed
            # into the first.
            refund()
            raise
        return {"ok": True, "remaining": remaining}

    def get_inventory(self, player_id: str, sort: str = "default",
                      item_type: Optional[str] = None) -> List[dict]:
        """The bag, in one canonical order, optionally filtered by type.

        The order is applied HERE, after both branches have built their rows,
        rather than in each branch's SQL. Two branches that sort separately
        diverge the first time a row lands on disk in a different order -- and
        "it happened to look fine" is not a promise. `sort_inventory` is the
        single owner of the order for both.
        """
        items = []
        if self.pg_pool:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT item_id, quantity, acquired_at
                    FROM player_inventory
                    WHERE player_id = %s
                """, (player_id,))
                for row in cur.fetchall():
                    item_id, qty, acquired = row
                    item_def = ITEM_DEFINITIONS.get(item_id, {})
                    items.append(_inventory_row(item_id, qty, acquired, item_def))
                cur.close()
            finally:
                self._release_connection(conn)
        else:
            inv = self._mem_inventory.get(player_id, {})
            for item_id, data in inv.items():
                item_def = ITEM_DEFINITIONS.get(item_id, {})
                items.append(_inventory_row(item_id, data["quantity"],
                                           data["acquired_at"], item_def))
        if item_type and item_type != "all":
            items = [i for i in items if i.get("type") == item_type]
        return sort_inventory(items, sort)

    def get_slots_used(self, player_id: str) -> int:
        """How many SLOTS the bag holds -- one per row, not one per unit.

        This is the only number a capacity bar should draw, and the only number
        a "bag is full" refusal should compare against. Sixty bones is one
        slot, not sixty. The unit-summing `get_total_items` that used to answer
        this question is why one player could read as 60/50 full while the shop
        counted one slot and happily sold them something.
        """
        return len(self.get_inventory(player_id))

    def get_total_units(self, player_id: str) -> int:
        """Every unit the player owns, summed. For display, never for capacity."""
        if self.pg_pool:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COALESCE(SUM(quantity), 0) FROM player_inventory WHERE player_id = %s", (player_id,))
                row = cur.fetchone()
                cur.close()
                return row[0] if row else 0
            finally:
                self._release_connection(conn)
        return sum(data["quantity"] for data in self._mem_inventory.get(player_id, {}).values())

    def is_discardable(self, item_id: str) -> bool:
        """Whether the player may destroy this item themselves.

        Only an item with NO effect may be discarded. That is the whole
        anti-abuse rule for the discard route: a rare pill cannot be destroyed,
        so "I destroyed my rare" is always a bug and never a choice. A discard
        route that could destroy a usable item is a laundering path, not a
        convenience.
        """
        item_def = ITEM_DEFINITIONS.get(item_id)
        if not item_def:
            return False
        if item_def.get("effect"):
            return False
        if item_def.get("discardable") is False:
            return False
        return True

    def discard_item(self, player_id: str, item_id: str, quantity: int = 1) -> dict:
        """Destroy units of an effect-less item and report whether a slot freed.

        Discards NOTHING back: no currency, no progress, no event. Ever. The
        route exists so a player can free a slot, and a refund would be a
        dupe vector pointed the wrong way.
        """
        if item_id not in ITEM_DEFINITIONS:
            return {"ok": False, "error": "Invalid item"}
        # Route the count through the ONE helper every other count uses.
        # `quantity < 1` compared the caller's value to an int directly, so a
        # string or None raised TypeError out of the store -- an uncaught 500
        # on a paid-good floor, from the one method whose model bound an
        # in-process caller cannot reach.
        count = _whole_quantity(quantity)
        if count is None or count < 1 or count > MAX_DISCARD_QUANTITY:
            return {"ok": False, "error": "Quantity out of range"}
        quantity = count
        if not self.is_discardable(item_id):
            return {"ok": False, "error": "This item cannot be discarded"}
        slots_before = self.get_slots_used(player_id)
        held_before = self.get_item_count(player_id, item_id)
        result = self.remove_item(player_id, item_id, quantity)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "Insufficient quantity")}
        return {
            "ok": True,
            "item_id": item_id,
            "discarded": quantity,
            "remaining": int(result.get("remaining", 0)),
            "slots_used": self.get_slots_used(player_id),
            "slots_freed": slots_before - self.get_slots_used(player_id),
            "capacity": self.get_player_capacity(player_id),
        }

    def get_item_count(self, player_id: str, item_id: str) -> int:
        """Get quantity of specific item."""
        if self.pg_pool:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT quantity FROM player_inventory WHERE player_id = %s AND item_id = %s", (player_id, item_id))
                row = cur.fetchone()
                cur.close()
                return row[0] if row else 0
            finally:
                self._release_connection(conn)
        return self._mem_inventory.get(player_id, {}).get(item_id, {}).get("quantity", 0)

    def has_capacity(self, player_id: str, additional: int = 1) -> bool:
        """Whether `additional` MORE SLOTS would fit. Counts slots, not units.

        Kept as a helper so no caller has to remember the difference, but it now
        answers in slots. It used to sum quantities, which meant a player
        holding 60 bones was told "no room" while the shop said "plenty" -- the
        two routes disagreed about the same player.
        """
        used = self.get_slots_used(player_id)
        capacity = self.get_player_capacity(player_id)
        return used + additional <= capacity

    def get_player_capacity(self, player_id: str) -> int:
        """The player's bag capacity, read from the player the scroll wrote to.

        This used to `return 50` as a literal while the expansion-scroll effect
        wrote `inventory_capacity` onto the player's record. The item was
        consumed, the success message announced a bigger bag, and the bag never
        grew: the writer and the reader were two different places.

        The fallback is what keeps every existing player working with no data
        migration -- a player who has never used a scroll still has 50.
        """
        if self.player_store is not None:
            try:
                player = self.player_store[player_id]
            except Exception:
                player = None
            if isinstance(player, dict):
                try:
                    return int(player.get("inventory_capacity", BASE_INVENTORY_CAPACITY))
                except (TypeError, ValueError, OverflowError):
                    return BASE_INVENTORY_CAPACITY
        return BASE_INVENTORY_CAPACITY

    def get_scrolls_bought(self, player_id: str) -> int:
        """How many expansion scrolls this player has actually spent.

        Derived from the capacity rather than counted separately, because a
        second counter is a second thing that can disagree with the first. The
        ceiling check and this number read the same field, so they cannot
        disagree about how far along a player is.
        """
        gained = max(0, self.get_player_capacity(player_id) - BASE_INVENTORY_CAPACITY)
        return gained // max(1, EXPANSION_SLOTS_PER_SCROLL)

    def expand_capacity(self, player_id: str, player: dict) -> dict:
        """Apply ONE expansion scroll, checking the ceiling in place.

        The check and the write happen together, against the same `player` dict
        the route persists, so two simultaneous requests cannot both pass a
        check and then both write.

        The answer reports the value read back through `get_player_capacity`,
        never the value this code intended to set. That read-back IS the rule
        that would have caught the original paid no-op, where a handler
        announced 60 and the bag enforced 50 forever.

        The read-back has to be the same OBJECT the handler wrote, or it proves
        nothing. `get_player_capacity` reads the player's own record, so when
        there is no player store -- or the store cannot see this id -- it falls
        back to BASE_INVENTORY_CAPACITY and the receipt reports
        `slots_after: 50, slots_gained: 0` for a call that had just written 60
        onto the dict it was handed. Measured before this change. Reading the
        written dict first makes the reported number the one the bag will
        enforce in every configuration, not only the one with a live store.
        """
        before = self.get_player_capacity(player_id)
        # The ceiling is checked HERE, in the same call that writes. A check in
        # its own transaction is a race two simultaneous requests walk through.
        # The effect handler checks it too -- that one is defence in depth for
        # the generic /inventory/use route, which can reach the same effect.
        if before >= MAX_INVENTORY_SLOTS:
            return {
                "ok": False,
                "error": "at_capacity_ceiling",
                "slots": before,
                "max_slots": MAX_INVENTORY_SLOTS,
            }
        item_def = ITEM_DEFINITIONS.get(EXPANSION_SCROLL_ITEM)
        if not item_def:
            return {"ok": False, "error": "Expansion scroll missing from catalogue"}
        effect = apply_item_effect(player, item_def, None)
        if not effect.get("ok"):
            return {"ok": False, "error": effect.get("error", "Expansion failed")}
        after = _capacity_written_to(player)
        if after is None:
            after = self.get_player_capacity(player_id)
        return {
            "ok": True,
            "slots_before": before,
            "slots_after": after,
            "slots_gained": max(0, after - before),
            "total_slots_bought": self.get_scrolls_bought(player_id),
            "max_slots": MAX_INVENTORY_SLOTS,
        }


# Global instance
inventory_store = None


def init_inventory_store(pg_pool=None, player_store=None) -> InventoryStore:
    """Initialize global inventory store with PG pool."""
    global inventory_store
    inventory_store = InventoryStore(pg_pool, player_store)
    return inventory_store


# ------------------------------------------------------------------
# Shop Integration
# ------------------------------------------------------------------
SHOP_PRODUCTS = {
    "starter_pack": {"price": 99, "currency": "fateum", "limit": 1},
    "battle_pass": {"price": 999, "currency": "fateum", "limit": 1},
    "summon_bundle_10": {"price": 2700, "currency": "fateum", "limit": 10},
    "cultivation_pill_low": {"price": 100, "currency": "spirit_stone", "limit": 10},
    "cultivation_pill_med": {"price": 500, "currency": "spirit_stone", "limit": 5},
    "cultivation_pill_high": {"price": 100, "currency": "fateum", "limit": 2},
    "breakthrough_pill": {"price": 500, "currency": "fateum", "limit": 1},
    "beast_food_basic": {"price": 50, "currency": "spirit_stone", "limit": 20},
    "beast_food_quality": {"price": 300, "currency": "spirit_stone", "limit": 10},
    "beast_food_premium": {"price": 200, "currency": "fateum", "limit": 5},
    "battle_potion_qi": {"price": 200, "currency": "spirit_stone", "limit": 10},
    "battle_potion_hp": {"price": 200, "currency": "spirit_stone", "limit": 10},
    "battle_potion_buff": {"price": 500, "currency": "spirit_stone", "limit": 5},
    "battle_potion_shield": {"price": 500, "currency": "spirit_stone", "limit": 5},
    "talisman_escape": {"price": 1000, "currency": "spirit_stone", "limit": 3},
    "talisman_return": {"price": 500, "currency": "spirit_stone", "limit": 2},
    "sect_contribution_token": {"price": 100, "currency": "fateum", "limit": 5},
    "expansion_scroll": {"price": 1000, "currency": "fateum", "limit": 1},
    # Advertised by GET /shop/products (main.py) but missing here, so the live
    # /shop/buy handler answered "Product not found". Price matches the advertised cost.
    "spirit_stone_pack_small": {"price": 99, "currency": "fateum", "limit": 1},
}


# ------------------------------------------------------------------
# Route Models
# ------------------------------------------------------------------
class InventoryAddRequest(BaseModel):
    player_id: str
    item_id: str
    quantity: int = 1


class InventoryUseRequest(BaseModel):
    player_id: str
    item_id: str
    # EDGE BOUND. `quantity` was a bare int and the only guard was
    # `owned < req.quantity`, which is satisfied by every negative. Using an
    # item with quantity=-1 passed, then remove_item(-1) ADDED one back to the
    # inventory and apply_item_effect still ran the item's effect -- so a
    # negative quantity was a strictly-better use: it minted the item AND
    # consumed it. quantity=0 was the same mint with a subtler trace.
    # A use consumes a positive whole number of items; 1..MAX_USE_QUANTITY.
    quantity: int = Field(default=1, ge=1, le=MAX_USE_QUANTITY)


class InventoryBuyRequest(BaseModel):
    player_id: str
    product_id: str
    quantity: int = 1
    client_version: int = 0


class InventoryDiscardRequest(BaseModel):
    player_id: str
    item_id: str
    # EDGE BOUND, same reason as InventoryUseRequest.quantity. A negative
    # quantity would reach remove_item and ADD the units back, so a discard
    # would be a mint with a different verb. Both bounds live here AND are
    # re-checked in the handler, because an in-process caller can build this
    # model without validation ever running.
    quantity: int = Field(default=1, ge=1, le=MAX_DISCARD_QUANTITY)


class InventoryExpandRequest(BaseModel):
    player_id: str
    item_id: str = EXPANSION_SCROLL_ITEM
    # A scroll is a stack_size of 1, so more than one has to be refused
    # rather than clamped: a request for 5 that quietly becomes 1 is a
    # receipt that lies about what the player asked for.
    quantity: int = Field(default=1, ge=1, le=1)