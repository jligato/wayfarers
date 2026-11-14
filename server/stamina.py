"""
Wayfarer's Cycle — Stamina system.

Stamina is the primary daily-usage gate for zone travel. This module owns the
three pieces that were previously missing:

  1. A real spend. Travel actually deducts stamina.
  2. Passive regeneration over elapsed wall-clock time, so a player who runs
     dry recovers on their own instead of being permanently locked out.
  3. A purchasable refill, which is the standard monetization hook for this
     genre and the reason the regeneration rate is a tunable design parameter.

Design notes
------------
* State lives on the player dict, matching every other system in this server.
* Regen is computed from elapsed time (lazy, not tick-based), so a player who
  is offline for a week comes back to a full bar and the server never has to
  run a background job.
* ``spirit_stone`` is the free-to-play soft currency, so refills are purchasable
  without touching premium currency.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Both import shapes are real here: server/tests/test_stamina.py does
# `from server import stamina`, while server/tests/test_stamina_routes_
# contract.py does a flat `import stamina`. The flat name is the repo
# convention (npc_state.py:46, spirit_beasts.py:89), but it is only on
# sys.path in the second shape, so try it first and fall back. Same fork
# as breakthrough.py:508.
try:
    from stored_numbers import stored_float, stored_int
except ImportError:  # pragma: no cover - different entry-point shape
    from server.stored_numbers import stored_float, stored_int  # type: ignore

# --- Tunable design parameters ------------------------------------------------
# These are the numbers a designer would tune. They are gathered here (rather
# than inline in the route) so the whole stamina economy is visible in one file.

MAX_STAMINA: int = 100
"""Hard ceiling. A player can never hold more than this."""

STARTING_STAMINA: int = 100
"""What a brand-new player begins with."""

REGEN_PER_HOUR: int = 10
"""Stamina returned per hour of real time. At this rate a full bar from empty
takes 10 hours, which is deliberately slower than a typical daily session so
that a second session in the day still has something to spend."""

REFILL_COST_SPIRIT_STONE: int = 50
"""Spirit stones to buy a full refill. Cheap enough to be an impulse purchase."""

# The field on the player dict that records when stamina was last topped up.
_LAST_REGEN_FIELD = "stamina_last_regen_at"


class StaminaError(Exception):
    """Raised when a stamina action cannot be completed."""


def _now() -> float:
    return time.time()


def _clock_after_credit(last: Any, now: float, granted: int, owed: int) -> float:
    """Where the regeneration clock sits after ``granted`` stamina was credited.

    The clock may only advance by the time the credit actually paid for. Moving
    it straight to ``now`` throws away the sub-unit remainder on every call, so
    a player who checks more often than one stamina per ``3600 / REGEN_PER_HOUR``
    seconds never regenerates at all -- measured: polling every 3 minutes for 12
    hours banked 0 of the 100 stamina owed.

    When the ceiling is what limited the credit (``granted < owed``) the window
    really is spent -- the bar is full and nothing is pending -- so the clock
    goes to ``now``. Otherwise the unused fraction carries to the next call.
    """
    if granted < owed:
        return now
    # stored_float never raises and never returns inf. `float(last)` in a
    # two-class try was open twice over: OverflowError is the third class
    # Python raises for a stored number, and it was not in the tuple, so a
    # stored clock of 10**400 escaped it. A clock that cannot be read reads
    # as "now", which grants no windfall -- the same answer the except
    # clause was returning for every other failure.
    base = stored_float(last, now)
    limit = float(now)
    if granted <= 0:
        return base
    if REGEN_PER_HOUR <= 0:
        return limit
    return min(limit, base + granted * 3600.0 / REGEN_PER_HOUR)


def get_stamina(player: Dict[str, Any]) -> int:
    """Return the player's current stamina, without applying regeneration.

    Use this when you need a raw reading. For decisions, prefer
    ``current_stamina`` so the player is not punished for time they spent
    legitimately away from the game.
    """
    return stored_int(player.get("stamina", STARTING_STAMINA), STARTING_STAMINA)


def current_stamina(player: Dict[str, Any], now: Optional[float] = None) -> int:
    """Return stamina after applying any regeneration owed for elapsed time.

    This is a *read* that does not mutate the player. Call
    :func:`apply_regeneration` (or any mutating action) to actually bank the
    regenerated stamina.
    """
    now = _now() if now is None else now
    stored = stored_int(player.get("stamina", STARTING_STAMINA), STARTING_STAMINA)
    last = player.get(_LAST_REGEN_FIELD)

    if last is None:
        # Never initialised (legacy player). Treat the current value as
        # already topped-up at this moment so we do not grant a windfall.
        return max(0, min(MAX_STAMINA, stored))

    # A clock reading of -inf made `now - last` overflow to +inf, and
    # `int(inf * REGEN_PER_HOUR)` raised OverflowError out of a read the
    # client calls on every screen. An unreadable clock now reads as `now`.
    elapsed_hours = max(0.0, (now - stored_float(last, now)) / 3600.0)

    gained = int(elapsed_hours * REGEN_PER_HOUR)
    return max(0, min(MAX_STAMINA, stored + gained))


def apply_regeneration(player: Dict[str, Any], now: Optional[float] = None) -> int:
    """Bank elapsed-time regeneration into the player's stamina.

    Mutates ``player``. Returns the stamina value after regeneration.

    Calling this is idempotent with respect to time: the regeneration clock is
    advanced to ``now``, so calling it twice in a row does not double-credit.
    """
    now = _now() if now is None else now
    stored = stored_int(player.get("stamina", STARTING_STAMINA), STARTING_STAMINA)
    last = player.get(_LAST_REGEN_FIELD)

    if last is None:
        player[_LAST_REGEN_FIELD] = now
        player["stamina"] = max(0, min(MAX_STAMINA, stored))
        return int(player["stamina"])

    elapsed_hours = max(0.0, (now - stored_float(last, now)) / 3600.0)
    if not math.isfinite(elapsed_hours):
        # Belt and braces: stored_float above already refuses a non-finite
        # clock, so this cannot fire today. It is here because the next
        # edit to this line should have to delete a guard, not add one.
        player[_LAST_REGEN_FIELD] = now
        return max(0, min(MAX_STAMINA, stored))

    owed = int(elapsed_hours * REGEN_PER_HOUR)
    granted = min(owed, max(0, MAX_STAMINA - stored))
    player["stamina"] = max(0, min(MAX_STAMINA, stored + granted))
    player[_LAST_REGEN_FIELD] = _clock_after_credit(last, now, granted, owed)
    return int(player["stamina"])


def can_afford(player: Dict[str, Any], cost: int) -> bool:
    """True if the player can pay ``cost`` stamina right now."""
    return current_stamina(player) >= int(cost)


def spend(player: Dict[str, Any], cost: int) -> int:
    """Deduct ``cost`` stamina from the player.

    Applies pending regeneration first, so a player who waited long enough is
    never blocked by a stale stored value. Raises :class:`StaminaError` if the
    player cannot pay. Mutates ``player`` and returns the remaining stamina.
    """
    cost = int(cost)
    if cost < 0:
        raise ValueError("stamina cost cannot be negative")

    available = apply_regeneration(player)
    if available < cost:
        raise StaminaError(
            f"insufficient stamina: need {cost}, have {available}"
        )

    player["stamina"] = available - cost
    return int(player["stamina"])


def status(player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Return a client-friendly snapshot of the player's stamina.

    Includes the derived fields the UI needs to draw a bar and a countdown:
    how full the bar is, and when it will next be full.
    """
    now = _now() if now is None else now
    current = current_stamina(player, now=now)
    last = player.get(_LAST_REGEN_FIELD)

    # Seconds until the bar is full again (0 if already full or uninitialised).
    if current >= MAX_STAMINA or last is None:
        seconds_to_full = 0
    else:
        missing = MAX_STAMINA - current
        # REGEN_PER_HOUR stamina per 3600 seconds
        seconds_to_full = int(missing * 3600 / REGEN_PER_HOUR)

    return {
        "stamina": current,
        "max_stamina": MAX_STAMINA,
        "regen_per_hour": REGEN_PER_HOUR,
        "seconds_to_full": seconds_to_full,
        "refill_cost_spirit_stone": REFILL_COST_SPIRIT_STONE,
    }


def refill(player: Dict[str, Any], wallet: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
    """Spend spirit stones to refill stamina to full.

    Returns ``(stamina_after, spirit_stone_spent)``.

    ``wallet`` is the player's wallet mapping. If it is missing or too poor the
    refill is refused and nothing is mutated — this is a pure "check then
    charge" and callers that need atomicity should hold the player store
    transaction around it.
    """
    if wallet is None:
        raise StaminaError("no wallet available for stamina refill")

    balance = stored_int(wallet.get("spirit_stone", 0), 0)
    if balance < REFILL_COST_SPIRIT_STONE:
        raise StaminaError(
            f"insufficient spirit stones: need {REFILL_COST_SPIRIT_STONE}, have {balance}"
        )

    # Charge first, then grant. If anything between here and the grant raised,
    # we would want the charge rolled back by the caller's transaction, which
    # is why the caller must hold one.
    wallet["spirit_stone"] = balance - REFILL_COST_SPIRIT_STONE
    apply_regeneration(player)
    player["stamina"] = MAX_STAMINA
    return MAX_STAMINA, REFILL_COST_SPIRIT_STONE


# ---------------------------------------------------------------------------
# The persisted store (migration 012)
# ---------------------------------------------------------------------------
#
# Everything above this line is PURE arithmetic on a player dict. Everything
# below is the durable record: the pool row, the regeneration ledger, and the
# payment receipt. They are separated on purpose — the arithmetic is the part
# that is easy to test, the SQL is the part that must never be re-derived by
# hand.
#
# THE THREE TABLES, AND WHAT EACH ONE IS FOR
# -----------------------------------------
#   stamina_pool      the current bar. One row per player. A spend LOCKS this
#                     row, so two concurrent spends cannot both read the same
#                     "before" value and each deduct from it.
#   stamina_recovery  an append-only log of every regeneration credit. The
#                     audit trail for the one source a player did not have to
#                     do anything for.
#   stamina_purchase  the payment receipt, keyed (player, purchase_key). This
#                     PRIMARY KEY is the once-only guard: the second press of
#                     the same key conflicts and the wallet is never charged.
#
# NO-DATABASE MODE IS EXPLICITLY NOT THE GUARANTEE
# -------------------------------------------------
# The game runs with no PostgreSQL at all (local dev, in-memory fixtures). There
# the pool falls back to the player dict, which is what the pure functions above
# already read, and the purchase key falls back to a process-local set behind a
# lock — the same contract as ``dungeon_claims._MEM_CLAIM_KEYS``. That fallback
# does not survive a restart and does not span two servers, and it says so here
# rather than pretending otherwise. The guarantee is the primary key.

#: Process-local purchase keys, used ONLY when there is no database. Same
#: contract as ``dungeon_claims._MEM_CLAIM_KEYS``.
_MEM_PURCHASE_KEYS: set = set()
_MEM_PURCHASE_LOCK = threading.Lock()

#: Writes the pool row, creating it from a caller-supplied starting value the
#: first time. ``ON CONFLICT DO NOTHING`` is what makes two servers racing to
#: create the same player's pool safe: exactly one wins, the other reads.
_POOL_ENSURE_SQL = (
    "INSERT INTO stamina_pool (player_id, stamina, max_stamina, last_regen_at) "
    "VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (player_id) DO NOTHING"
)

#: ``FOR UPDATE`` is the load-bearing word. It takes a row lock that is held to
#: the end of the caller's transaction, so a second concurrent read of the same
#: player's bar blocks until the first one commits or rolls back.
_POOL_LOCK_SQL = (
    "SELECT stamina, max_stamina, last_regen_at, version "
    "FROM stamina_pool WHERE player_id = %s FOR UPDATE"
)

_POOL_READ_SQL = (
    "SELECT stamina, max_stamina, last_regen_at, version "
    "FROM stamina_pool WHERE player_id = %s"
)

_POOL_WRITE_SQL = (
    "UPDATE stamina_pool "
    "SET stamina = %s, max_stamina = %s, last_regen_at = %s, "
    "    version = version + 1, updated_at = NOW() "
    "WHERE player_id = %s "
    "RETURNING version"
)

#: The recovery ledger is INSERT-only. There is deliberately no UPDATE and no
#: DELETE statement for this table anywhere in the server: a correction is a
#: new row, so the history of what the server granted cannot be rewritten.
_RECOVERY_INSERT_SQL = (
    "INSERT INTO stamina_recovery "
    "(player_id, window_from, window_to, elapsed_seconds, "
    " stamina_owed, stamina_granted) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING recovery_id"
)

#: THE ONCE-ONLY GUARD. ``DO NOTHING RETURNING`` means a replay returns no
#: row, and the caller treats "no row" as "already paid" and charges nothing.
#: Same shape as ``dungeon_claims._CLAIM_SQL`` and the battle-pass reserve.
_PURCHASE_RESERVE_SQL = (
    "INSERT INTO stamina_purchase "
    "(player_id, purchase_key, currency, cost, stamina_before, stamina_after) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (player_id, purchase_key) DO NOTHING "
    "RETURNING purchase_id"
)


class StaminaInjectionError(StaminaError):
    """Raised when a caller tries to set stamina by asking rather than by acting.

    Split out from :class:`StaminaError` so the route can answer 400 to a
    forged payload and 409 to a genuine "you do not have enough", which are
    different bugs from the player's point of view and different copy on
    screen.
    """


# ---------------------------------------------------------------------------
# Stamina TYPES — what a spend is FOR, and what each one costs
# ---------------------------------------------------------------------------
#
# A spend with no category is unattributable. The old `/zones/stamina/refill`
# route spent stamina on "travel" by whatever number `world.travel_stamina`
# computed, and the recovery ledger could say a player spent 40 stamina but not
# on what. A type is the answer to "why is my bar empty", and it is also where
# the per-type cost lives, so a designer can rebalance combat without touching
# travel.
#
# WHY THE COST IS SERVER-OWNED AND NOT SENT BY THE CLIENT
# ------------------------------------------------------
# `COST_BY_TYPE` is the ONLY place a stamina price appears. A caller names the
# TYPE and the server reads the price, for the same reason the purchase route
# reads its own price: a caller-settable price is a caller-settable cost. The
# route takes no amount at all — `cost_for` is the amount.
#
# THE UNKNOWN-TYPE RULE
# ---------------------
# An unrecognised type is an ERROR, not a default. The tempting version is
# `COST_BY_TYPE.get(name, DEFAULT_COST)`, which is a hole: a typo, a renamed
# type, or a client sending `"travle"` would silently bill a default and the
# recovery ledger would record a spend nobody can attribute. Refusing means a
# new type has to be added here deliberately, which is the point.
#
# The costs are placeholders seeded from the existing economy file
# (`data/economy_defaults.json`: `base_travel_cost` 10, `teleport_cost_multiplier`
# 2.0 -> 20). They are meant to be tuned, and they live in one table so tuning
# is one edit.
COST_BY_TYPE: Dict[str, int] = {
    "zone_travel": 10,    # = economy_defaults.json stamina.base_travel_cost
    "teleport": 20,       # = base_travel_cost * teleport_cost_multiplier (2.0)
    "combat": 5,          # a monster fight; cheap because it is the core loop
    "harvest": 2,         # a node gather; the cheapest thing a player does
    "dungeon_entry": 15,  # a run's entry fee, above a single travel
}

#: The types the client may ask for. Named separately from ``COST_BY_TYPE`` so
#: the route can report the menu to a client without leaking the cost table's
#: internals, and so a type can be priced at zero later (a free action) without
#: becoming a member of the cost table.
KNOWN_TYPES = frozenset(COST_BY_TYPE) | frozenset({"pvp_entry", "event_entry"})

#: Types that cost nothing, or that the server settles itself rather than
#: through this route. Listed so ``/stamina/info`` can tell a client the whole
#: menu, including the entries that have no price here.
FREE_TYPES = frozenset({"pvp_entry", "event_entry"})


def is_known_type(type_name: str) -> bool:
    """True when ``type_name`` is a spend type this server recognises."""
    return isinstance(type_name, str) and type_name in KNOWN_TYPES


def cost_for(type_name: str) -> int:
    """The SERVER's price for one spend of this type.

    Raises :class:`StaminaInjectionError` for an unknown type. See the header
    for why there is deliberately no default.

    ``pvp_entry`` and ``event_entry`` are priced at zero here: those events are
    settled by the routes that own them, so charging through this route as well
    would double-bill. They are members of the menu, not of the cost table.
    """
    if not is_known_type(type_name):
        raise StaminaInjectionError(f"unknown stamina type {type_name!r}")
    return int(COST_BY_TYPE.get(type_name, 0))


def type_menu() -> list:
    """The spend menu, for ``GET /stamina/info``. Cost is included because a
    client has to be able to draw "this costs 10" without hard-coding it, and
    the number still comes from the server's table either way.
    """
    return [
        {"type": name, "cost": int(COST_BY_TYPE.get(name, 0)),
         "free": name in FREE_TYPES}
        for name in sorted(KNOWN_TYPES)
    ]


def purchase_key_for(player_id: str, now: Optional[float] = None) -> str:
    """The SERVER-DERIVED key for one refill. Never taken from a request.

    Two parts, both server-side: the player's own id, and the server's own UTC
    day index. A player therefore gets one refill per day, and two players on
    the same day get DIFFERENT keys.

    WHY THE PLAYER ID IS PART OF THE KEY, AND NOT ONLY OF THE PRIMARY KEY
    ------------------------------------------------------------------------
    The ``stamina_purchase`` primary key is ``(player_id, purchase_key)``, so on
    its own a day-only key would be enough for that table. It is NOT enough for
    the WALLET, because the wallet's replay key namespace is GLOBAL: see
    ``wallet_store_pg._replay``, which does ``SELECT ... WHERE key = %s`` with no
    player predicate and answers "idempotency key belongs to another player" when
    a second player presents a key the first one already used. A day-only key
    would therefore make the second player of the day unable to buy stamina at
    all, with a message about idempotency that has nothing to do with the bug.
    The player id is in the key so the wallet key is per-player too.

    The argument for deriving this here rather than accepting an
    ``Idempotency-Key`` header is the one migration 006 makes for the
    battle-pass claim ledger: a caller-supplied key makes "pays once" a
    property of the client's memory. A client that sends a fresh key on every
    press of the button pays on every press.
    """
    now = _now() if now is None else now
    return f"refill:{player_id}:{int(now // 86400)}"


def _mem_reserve(player_id: str, key: str) -> bool:
    with _MEM_PURCHASE_LOCK:
        pair = (player_id, key)
        if pair in _MEM_PURCHASE_KEYS:
            return False
        _MEM_PURCHASE_KEYS.add(pair)
        return True


def _mem_release(player_id: str, key: str) -> None:
    with _MEM_PURCHASE_LOCK:
        _MEM_PURCHASE_KEYS.discard((player_id, key))


def _conn(players_db):
    """The live database connection, or None when there is no database.

    Taken from the player store rather than opened here so the stamina write
    joins the caller's transaction instead of starting one of its own. A
    separate connection would be a separate transaction, and the pool row
    would commit even when the wallet charge it was paired with rolled back.
    """
    return getattr(players_db, "current_connection", None)


def _read_pool_row(cur, player_id: str):
    cur.execute(_POOL_READ_SQL, (player_id,))
    return cur.fetchone()


def ensure_pool(players_db, player_id: str, now: Optional[float] = None) -> None:
    """Create this player's pool row if it does not exist yet.

    Starting value is the server's own ``STARTING_STAMINA``, never a value the
    caller supplied, and ``last_regen_at`` is set to now so the first run of the
    new code grants no windfall to a player who has been away a week.
    """
    conn = _conn(players_db)
    if conn is None:
        return
    now = _now() if now is None else now
    with conn.cursor() as cur:
        cur.execute(_POOL_ENSURE_SQL, (player_id, STARTING_STAMINA, MAX_STAMINA, now))


def _regen_owed(stored: int, max_stamina: int, last: Optional[float],
                now: float) -> Tuple[int, int]:
    """Return ``(owed, granted)`` for elapsed time, both clamped by the ceiling.

    ``last is None`` means never banked, and owes NOTHING. That is the rule
    that stops a legacy player, or a player whose row was just created, from
    collecting a full bar for a window nobody measured.
    """
    if last is None:
        return 0, 0
    # Same defect as current_stamina/apply_regeneration, and this one is
    # the shared helper both persistence paths call, so it is the widest
    # of the four: `-inf` makes `now - last` overflow to +inf and
    # `int(inf * REGEN_PER_HOUR)` raises, while 10**400 raises inside
    # float() itself -- and OverflowError was in neither clause of the
    # two-class except that used to stand here. Unreadable clock owes 0.
    elapsed_hours = max(0.0, (now - stored_float(last, now)) / 3600.0)
    owed = int(elapsed_hours * REGEN_PER_HOUR)
    granted = min(owed, max(0, max_stamina - stored))
    return owed, granted


def bank_regeneration(players_db, player_id: str,
                      now: Optional[float] = None) -> Dict[str, Any]:
    """Bank owed regeneration into the pool and log it. Returns what happened.

    MUST be called inside the caller's ``players_db.transaction(player_id)`` so
    the pool UPDATE and the recovery INSERT commit together: a credited bar with
    no ledger row (or the reverse) is the state this design exists to prevent.

    ``owed`` and ``granted`` are both reported. They differ whenever the bar
    fills before the window closes, and a caller that only sees ``granted``
    cannot tell "the server gave me nothing" from "the server gave me all it
    could".
    """
    now = _now() if now is None else now
    result: Dict[str, Any] = {
        "stamina_owed": 0,
        "stamina_granted": 0,
        "stamina": 0,
        "max_stamina": MAX_STAMINA,
        "recovery_id": None,
        "seconds_to_full": 0,
    }

    conn = _conn(players_db)
    if conn is None:
        # No database: the pool IS the player dict, so the pure function above
        # is the whole implementation. The ledger has no table to write to.
        player = players_db[player_id]
        before = stored_int(player.get("stamina", STARTING_STAMINA), STARTING_STAMINA)
        owed, granted = _regen_owed(before, MAX_STAMINA,
                                     player.get(_LAST_REGEN_FIELD), now)
        after = stamina_after(before, granted, MAX_STAMINA)
        player["stamina"] = after
        player[_LAST_REGEN_FIELD] = now
        result.update(stamina_owed=owed, stamina_granted=granted, stamina=after,
                      max_stamina=MAX_STAMINA,
                      seconds_to_full=_seconds_to_full(after, MAX_STAMINA))
        return result

    ensure_pool(players_db, player_id, now=now)
    with conn.cursor() as cur:
        # FOR UPDATE: the row lock is held to the end of the transaction, so a
        # concurrent spend cannot read a "before" the bank is about to change.
        cur.execute(_POOL_LOCK_SQL, (player_id,))
        row = cur.fetchone()
        if row is None:
            return result
        stored, max_stamina, last, _version = row

        owed, granted = _regen_owed(stored, max_stamina, last, now)
        after = stamina_after(stored, granted, max_stamina)

        # This used to read `last or now`, which is wrong twice over: 0.0 is a
        # legitimate stored epoch AND it is falsy, so a player whose row said
        # 0.0 got a ledger line claiming a zero-length window for a full
        # 100-stamina credit. Measure the window from the value itself.
        window_from = float(last) if last is not None else now
        window_to = _clock_after_credit(window_from, now, granted, owed)

        cur.execute(_POOL_WRITE_SQL, (after, max_stamina, window_to, player_id))
        if granted > 0:
            # Only a real credit is logged. A zero row per read would be noise,
            # and it would grow without bound on a player who checks often.
            cur.execute(
                _RECOVERY_INSERT_SQL,
                (player_id, window_from, window_to,
                 max(0.0, window_to - window_from), owed, granted),
            )
            rec = cur.fetchone()
            result["recovery_id"] = rec[0] if rec else None

    result.update(stamina_owed=owed, stamina_granted=granted, stamina=after,
                  max_stamina=max_stamina,
                  seconds_to_full=_seconds_to_full(after, max_stamina))
    return result


def _seconds_to_full(current: int, max_stamina: int) -> int:
    if current >= max_stamina or REGEN_PER_HOUR <= 0:
        return 0
    missing = max_stamina - current
    return int(missing * 3600 / REGEN_PER_HOUR)


def stamina_after(stored: int, gained: int, max_stamina: int = MAX_STAMINA) -> int:
    """``stored + gained``, clamped to ``[0, max_stamina]``.

    The clamp is applied HERE, in the one function every write path calls, so a
    second code site cannot forget it. The database constraint
    (``stamina_pool_within_ceiling``) enforces the same rule independently —
    the CHECK is what makes the ceiling a property of the data rather than of a
    function a refactor can bypass.
    """
    return max(0, min(int(max_stamina), int(stored) + int(gained)))


def consume(players_db, player_id: str, amount: int, *,
            now: Optional[float] = None,
            reason: str = "unspecified") -> Dict[str, Any]:
    """Deduct ``amount`` from this player's bar, in the database.

    Refuses with :class:`StaminaInjectionError` when ``amount`` is not a plain
    positive integer, and with :class:`StaminaError` when the player genuinely
    does not have that much. The two are different failures and a client shows
    different copy for them.

    The amount the CALLER asks for is the amount that is checked and the amount
    that is taken. There is no code path in this function where a caller can
    cause stamina to be ADDED — the only write is a subtraction, and
    ``gained`` is never a parameter.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise StaminaInjectionError("stamina amount must be a whole number")
    if amount <= 0:
        raise StaminaInjectionError("stamina amount must be positive")
    if not isinstance(reason, str) or not reason.strip():
        raise StaminaInjectionError("a spend must say what it was for")

    now = _now() if now is None else now
    conn = _conn(players_db)

    if conn is None:
        # No database: the pool IS the player dict, so the pure helpers above
        # are the whole implementation. Bank first for the same reason the
        # SQL path does, then take the amount off the banked value — two steps,
        # not a re-derivation, so the in-memory and PostgreSQL paths cannot
        # disagree about what a spend costs.
        player = players_db[player_id]
        banked = apply_regeneration(player, now=now)
        if banked < amount:
            raise StaminaError(f"insufficient stamina: need {amount}, have {banked}")
        player["stamina"] = stamina_after(banked - amount, 0, MAX_STAMINA)
        return {"stamina": int(player["stamina"]), "max_stamina": MAX_STAMINA,
                "consumed": amount, "reason": reason}

    ensure_pool(players_db, player_id, now=now)
    with conn.cursor() as cur:
        cur.execute(_POOL_LOCK_SQL, (player_id,))
        row = cur.fetchone()
        if row is None:
            raise StaminaError("no stamina pool for this player")
        stored, max_stamina, last, _version = row

        # Bank first, so a player who waited long enough is never refused on a
        # stale stored value. This is the same "spend reads through regen" rule
        # the pure ``spend()`` above follows.
        owed, granted = _regen_owed(stored, max_stamina, last, now)
        available = stamina_after(stored, granted, max_stamina)

        if available < amount:
            raise StaminaError(f"insufficient stamina: need {amount}, have {available}")

        after = available - amount
        cur.execute(_POOL_WRITE_SQL, (after, max_stamina,
                                      _clock_after_credit(last, now, granted, owed),
                                      player_id))

    return {"stamina": after, "max_stamina": max_stamina, "consumed": amount,
            "reason": reason}


def reserve_purchase(players_db, player_id: str, key: str, cost: int,
                     stamina_before: int, stamina_after_value: int) -> bool:
    """Claim this purchase key. True = this call pays, False = already paid.

    MUST be called inside the caller's transaction and BEFORE the wallet is
    charged. In PostgreSQL the row insert is therefore atomic with the charge,
    so a failure after the charge rolls the receipt back with it and the key
    stays usable. In no-database mode it is the process-local fallback, which
    the caller must release by hand if the charge then fails.
    """
    if cost <= 0:
        raise StaminaInjectionError("purchase cost must be positive")
    if stamina_after_value <= stamina_before:
        raise StaminaInjectionError("a purchase that grants nothing is not a purchase")

    conn = _conn(players_db)
    if conn is None:
        return _mem_reserve(player_id, key)
    with conn.cursor() as cur:
        cur.execute(_PURCHASE_RESERVE_SQL,
                    (player_id, key, "spirit_stone", cost,
                     stamina_before, stamina_after_value))
        return cur.fetchone() is not None


def release_purchase(players_db, player_id: str, key: str) -> None:
    """Undo a reservation whose charge then failed.

    Only meaningful in the no-database fallback; in PostgreSQL the exception
    unwinds the transaction and its rollback removes the inserted row.
    """
    if _conn(players_db) is None:
        _mem_release(player_id, key)


def purchase_exists(players_db, player_id: str, key: str) -> Optional[bool]:
    """True/False read from the DATABASE. None when there is no database.

    Exposed so a test can assert against a fact the server wrote, rather than
    against the route's own "already purchased" flag, which is a claim.
    """
    conn = _conn(players_db)
    if conn is None:
        with _MEM_PURCHASE_LOCK:
            return (player_id, key) in _MEM_PURCHASE_KEYS
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM stamina_purchase WHERE player_id = %s AND purchase_key = %s",
            (player_id, key),
        )
        return cur.fetchone() is not None


def grant_full(players_db, player_id: str, *,
               now: Optional[float] = None) -> Dict[str, Any]:
    """Top the bar up to its ceiling. No payment — see ``stamina_routes`` for that.

    Separated from the paid path on purpose: a free grant and a purchase must
    not share a function, or a future free-grant caller inherits the payment
    code and can charge a player.
    """
    now = _now() if now is None else now
    conn = _conn(players_db)
    if conn is None:
        player = players_db[player_id]
        apply_regeneration(player, now=now)
        player["stamina"] = MAX_STAMINA
        return {"stamina": MAX_STAMINA, "max_stamina": MAX_STAMINA}

    ensure_pool(players_db, player_id, now=now)
    with conn.cursor() as cur:
        cur.execute(_POOL_LOCK_SQL, (player_id,))
        row = cur.fetchone()
        if row is None:
            raise StaminaError("no stamina pool for this player")
        _stored, max_stamina, _last, _version = row
        cur.execute(_POOL_WRITE_SQL, (max_stamina, max_stamina, now, player_id))
    return {"stamina": max_stamina, "max_stamina": max_stamina}


def pool_status(players_db, player_id: str,
                now: Optional[float] = None) -> Dict[str, Any]:
    """The client's view of the bar, banked from the pool when there is one.

    The response is assembled from the DATABASE row rather than from the pure
    ``status()`` above, so what the player is told is what the next spend will
    actually see. A status computed from one source and enforced against
    another is how a player gets told "100 stamina" and then spends against 4.
    """
    now = _now() if now is None else now
    conn = _conn(players_db)

    if conn is None:
        player = players_db[player_id]
        snap = status(player, now=now)
        snap["version"] = 0
        return snap

    ensure_pool(players_db, player_id, now=now)
    with conn.cursor() as cur:
        cur.execute(_POOL_READ_SQL, (player_id,))
        row = cur.fetchone()
    if row is None:
        return {"stamina": 0, "max_stamina": MAX_STAMINA, "regen_per_hour": REGEN_PER_HOUR,
                "seconds_to_full": 0, "refill_cost_spirit_stone": REFILL_COST_SPIRIT_STONE,
                "version": 0}

    stored, max_stamina, last, version = row
    _owed, granted = _regen_owed(stored, max_stamina, last, now)
    current = stamina_after(stored, granted, max_stamina)
    return {
        "stamina": current,
        "max_stamina": max_stamina,
        "regen_per_hour": REGEN_PER_HOUR,
        "seconds_to_full": _seconds_to_full(current, max_stamina),
        "refill_cost_spirit_stone": REFILL_COST_SPIRIT_STONE,
        # What the next mutating call will bank, before it does. The client can
        # draw an accurate countdown without the server having written anything.
        "pending_regen": granted,
        "version": version,
    }


def recovery_history(players_db, player_id: str, limit: int = 20) -> list:
    """The player's regeneration ledger, newest first. Empty without a database."""
    conn = _conn(players_db)
    if conn is None:
        return []
    limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recovery_id, window_from, window_to, elapsed_seconds, "
            "       stamina_owed, stamina_granted, recorded_at "
            "FROM stamina_recovery WHERE player_id = %s "
            "ORDER BY recorded_at DESC LIMIT %s",
            (player_id, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
