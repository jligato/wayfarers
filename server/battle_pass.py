"""
Battle Pass — three tiers, one UTC calendar-month season, once-only payouts.

WHAT THIS MODULE IS RESPONSIBLE FOR
-----------------------------------
Four things, and nothing else:

1. the SEASON CLOCK (delegated to `battle_pass_season`, never re-derived here);
2. the XP CURVE and the three reward tables;
3. CLAIM and PURCHASE logic, both once-only by construction;
4. the HTTP surface, mounted by `register_battle_pass_routes`.

SEASON STATE IS SERVER-OWNED
----------------------------
Every route takes its season from `battle_pass_season.season_clock()`, which
reads the server's own clock. No route accepts a season id, a level, an XP
amount or a reward from the caller. `POST /battle-pass/{id}/add-xp` USED to
exist and took `amount` off the query string: three POSTs walked one player's
season XP from 100000 to 300000 with no gameplay behind them, and a negative
amount walked XP backwards. It is gone, and it is gone from the route TABLE, not
just from the source.

THE THREE ONCE-ONLY GUARDS
--------------------------
Guards for different intents, kept apart on purpose. Mixing them up is how a
ledger either double-pays or swallows a genuine purchase.

CLAIM -- keyed on the NATURAL key (player, season, tier, level)
    A claim has no second intent: that tuple IS the claim and there is exactly
    one of it. So the guarantee holds even when the client mints a fresh
    Idempotency-Key for a retry, which is exactly what the shipped panel does on
    a double-tap. A caller-keyed table pays twice there.

PURCHASE -- keyed on the CALLER'S Idempotency-Key, namespaced by player
    A player may legitimately buy Senior and then Super in one season: two
    intents, two keys. Keying a purchase on the natural tier instead would
    swallow the second one. Two players sending the byte-identical key must both
    be charged, so the ledger key is namespaced by player id.

THE ORDERING, WHICH IS THE PART THAT IS EASY TO GET WRONG
---------------------------------------------------------
The ledger row is written BEFORE the payout, inside the same transaction, so a
payout that raises rolls the marker back with it and the tier stays claimable.
There is no state in which a reward was paid with no receipt, and none in which
a receipt exists for a reward that was never paid.

Writing the marker AFTER the payout makes the reward unbounded under
concurrency: two callers both pay, then both write the marker, and the marker
proves nothing.

NO-DATABASE MODE IS EXPLICITLY NOT THE GUARANTEE
------------------------------------------------
The game runs with no PostgreSQL at all. There the ledger falls back to a
process-local dict behind a lock, which is what a marker on a player row was: it
dies with the process and does not span two servers. The PRIMARY KEY is the
guarantee. The fallback stores the RECEIPT rather than a bare marker, so both
backends answer a replay identically and a no-database test run is not
measuring a weaker route than production runs.

THE SEASON CLOCK IS NOT RE-DERIVED HERE
---------------------------------------
`get_season_start` / `get_season_end` / `get_season_id` delegate to
`battle_pass_season` verbatim. The shipped versions mixed UTC labels with
`time.mktime` (LOCAL time) and added a flat 30 days to a calendar month, which
ran 31-day months to 100% a day early and pushed February two days past its own
boundary. `SEASON_DAYS = 30` is kept because economy_config, tower and the
design notes all quote it, and it is now DESCRIPTIVE -- no window is sized
from it.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import battle_pass_season as _season
from qi_rewards import grant_qi

# MODULE LEVEL, not function-local, and never with a default. This module
# uses `from __future__ import annotations`, so FastAPI resolves a handler's
# annotations against the MODULE globals. A `Request` imported inside
# register_battle_pass_routes() is invisible to that resolver, so the
# parameter is classified as an untyped QUERY param and every request
# answers 422 {"loc": ["query", "request"]} with nothing in the log.
from fastapi import Depends, HTTPException, Request

# `import auth` BARE, not `server.auth`: the app resolves bearer tokens
# against the bare module's session store, so a token minted through
# `server.auth` is a token nothing ever reads.
from auth import PlayerIdentity, require_player
from idem_gate import require_idempotency_key

#: Descriptive only. The rest of the game quotes 30 days (economy_config.py,
#: tower.py, design/04-reward-economy.md 6.1); no window is sized from it.
SEASON_DAYS = 30

MAX_LEVEL = 50

#: How many finished seasons a player's row keeps. A returning player reads the
#: newest at index 0; anything older is not what they came back for, and an
#: unbounded history is an unbounded row.
SEASON_HISTORY_LIMIT = 5

#: Safety ceiling on ONE grant, applied at the one chokepoint every earn path
#: funnels through. It is a ceiling, not a tuned balance number.
#:
#: The largest legitimate single grant measured in this tree is 360 (72 hours of
#: offline progress at 5/hour), so 1,000 sits at ~2.8x real play. The rule to
#: re-derive it against is "roughly 2-3x the largest real grant, and at most a
#: few percent of the season total", so if the season total is retuned, retune
#: this with it. Do not carry the number across a rescale.
MAX_XP_PER_GRANT = 1_000


class BattlePassTier(Enum):
    FREE = "free"
    SENIOR = "senior"
    SUPER = "super"


#: The price of each tier, in spirit stones -- the F2P currency, the one
#: missions, gacha and the shop already spend from.
TIER_COST = {
    BattlePassTier.FREE: 0,
    BattlePassTier.SENIOR: 999,
    BattlePassTier.SUPER: 2499,
}

#: The currency name the WALLET store knows this money by. The player row calls
#: it ``spirit_stones`` and stores a tiered dict; the wallet calls it
#: ``spirit_stone`` and stores a flat field on ``CurrencyBalance``. Passing the
#: row's plural name to ``wallet_dispatch.earn`` is refused as an unknown
#: currency, which is a silent no-payout rather than a crash.
WALLET_STONE_CURRENCY = "spirit_stone"

#: The weekly bonus table. Keyed by tier so Senior and Super pay differently for
#: the same week.
WEEKLY_BONUS = {
    BattlePassTier.SENIOR: {"xp": 5_000, "spirit_stones": 500},
    BattlePassTier.SUPER: {"xp": 10_000, "spirit_stones": 1_500},
}

# ---------------------------------------------------------------------------
# the XP curve
# ---------------------------------------------------------------------------
# MEASURED 2026-10-01 against the shipped earn paths: the most experience the
# game can pay in a 30-day season is about 48,300 (an ACTIVE player -- every
# boss strike, every tribulation, every event quest, every contribution,
# daily). The curve below totals 724,849 for 50 levels, so an active player
# finishes around level 16 and two thirds of the track is arithmetically
# unreachable. That is a real balance defect and it is a DESIGNER'S CALL, not a
# coder's: the fix is either a lower level cap or a flatter curve, and which one
# is right depends on how the season should feel. The shape is therefore NAMED
# CONSTANTS so retuning is a one-line change every caller already picks up, and
# the numbers are recorded rather than silently baked in.
XP_LEVEL_CURVE_BASE = 100.0
XP_LEVEL_CURVE_EXPONENT = 1.5

#: Index 0 is unused so that ``XP_PER_LEVEL[level]`` reads naturally.
XP_PER_LEVEL: List[int] = [0]
for _i in range(1, MAX_LEVEL + 1):
    XP_PER_LEVEL.append(int(XP_LEVEL_CURVE_BASE * (_i ** XP_LEVEL_CURVE_EXPONENT)))

TOTAL_XP = sum(XP_PER_LEVEL[1:])

# ---------------------------------------------------------------------------
# the reward tables -- three tiers, genuinely distinct
# ---------------------------------------------------------------------------
# Each tier differs from the one below it in FOUR ways, not one: a bigger
# currency payout, a bigger qi payout, a different item cadence, and cosmetics
# only the paid tiers carry. A "premium" tier that is the free tier with the
# numbers doubled is not a tier, it is a multiplier, and a player can tell.
REWARD_TABLES: Dict[BattlePassTier, Dict[int, Dict[str, Any]]] = {
    BattlePassTier.FREE: {},
    BattlePassTier.SENIOR: {},
    BattlePassTier.SUPER: {},
}

for _level in range(1, MAX_LEVEL + 1):
    # FREE: the retention promise. A player who never pays still gets paid out,
    # which is the thing a paywall creeping into this table would destroy.
    REWARD_TABLES[BattlePassTier.FREE][_level] = {
        "spirit_stones": 50 + _level * 5,
        "qi": 100 + _level * 10,
        "items": ["Basic Pill"] if _level % 5 == 0 else [],
    }

    # SENIOR: roughly double the currency, a ticket every ten levels, the Senior
    # Pill instead of the Basic, and an aura at the two milestone levels.
    REWARD_TABLES[BattlePassTier.SENIOR][_level] = {
        "spirit_stones": 100 + _level * 10,
        "qi": 200 + _level * 20,
        "gacha_tickets": 1 if _level % 10 == 0 else 0,
        "items": ["Senior Pill"] if _level % 5 == 0 else [],
        "cosmetic": f"senior_aura_{_level}" if _level in (25, 50) else None,
    }

    # SUPER: four times the free currency, two tickets every ten levels, the
    # Super Pill, and skins at the opening level plus both milestones -- so the
    # most expensive thing on the track is visible from the first screen.
    REWARD_TABLES[BattlePassTier.SUPER][_level] = {
        "spirit_stones": 200 + _level * 20,
        "qi": 400 + _level * 40,
        "gacha_tickets": 2 if _level % 10 == 0 else 0,
        "items": ["Super Pill"] if _level % 5 == 0 else [],
        "cosmetic": f"super_skin_{_level}" if _level in (1, 25, 50) else None,
    }


# ---------------------------------------------------------------------------
# the claim ledger (migration 006) and the purchase ledger (migration 005)
# ---------------------------------------------------------------------------
#: Process-local fallback for the CLAIM ledger, used ONLY when the store has no
#: database connection. Maps the natural key to the RECEIPT, not to a bare True,
#: so a replay can be ANSWERED with what the first attempt paid.
_MEM_CLAIM_KEYS: Dict[Tuple[str, str, str, int], Optional[dict]] = {}
_MEM_CLAIM_LOCK = threading.Lock()

#: Process-local fallback for the PURCHASE ledger. A purchase's intent is
#: caller-named (Senior then Super in one season are two intents), so this holds
#: keys, not receipts.
_MEM_PURCHASE_KEYS: set = set()
_MEM_PURCHASE_LOCK = threading.Lock()

_CLAIM_RESERVE_SQL = (
    "INSERT INTO battle_pass_claims "
    "(player_id, season_id, tier, level, reward, player_level) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (player_id, season_id, tier, level) DO NOTHING "
    "RETURNING level"
)

#: Written in the SAME transaction that reserved the row, so no other process
#: can observe a committed row whose receipt is still empty.
_CLAIM_RECEIPT_SQL = (
    "UPDATE battle_pass_claims SET reward = %s, player_level = %s "
    "WHERE player_id = %s AND season_id = %s AND tier = %s AND level = %s"
)

_CLAIM_READ_SQL = (
    "SELECT reward FROM battle_pass_claims "
    "WHERE player_id = %s AND season_id = %s AND tier = %s AND level = %s"
)

_PURCHASE_RESERVE_SQL = (
    "INSERT INTO battle_pass_purchases "
    "(purchase_key, player_id, season_id, tier, cost, response) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (purchase_key) DO NOTHING "
    "RETURNING purchase_key"
)

_PURCHASE_RECEIPT_SQL = (
    "UPDATE battle_pass_purchases SET response = %s WHERE purchase_key = %s"
)

_PURCHASE_READ_SQL = (
    "SELECT response FROM battle_pass_purchases WHERE purchase_key = %s"
)


def _as_json(value: Any) -> Optional[dict]:
    """Coerce a JSONB read into a dict, tolerating a driver that returns text."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _dumps(value: Any) -> str:
    """JSON text for a database parameter."""
    return json.dumps(value, sort_keys=True, default=str)


def _connection(players_db):
    """The transaction's own connection, or None in no-database mode.

    Branch on the CONNECTION, never on an env var: the store decides, and a
    store with a database always opens its transaction before a route body runs.
    """
    return getattr(players_db, "current_connection", None)


# ---------------------------------------------------------------------------
# the season clock, delegated
# ---------------------------------------------------------------------------
def get_season_id(now: Optional[float] = None) -> str:
    """The season a timestamp falls in, e.g. ``"2026-09"``. Server clock only."""
    return _season.season_id(now)


def get_season_start(season_id: str) -> float:
    """UTC midnight on the first of that month. Delegates; never re-derived."""
    return _season.season_bounds(season_id)[0]


def get_season_end(season_id: str) -> float:
    """UTC midnight on the first of the NEXT month -- the next season's start."""
    return _season.season_bounds(season_id)[1]


def get_season_progress(now: Optional[float] = None) -> dict:
    """Everything a client needs for the countdown, from one server-timed call."""
    return _season.season_clock(now)


def get_season_number(now: Optional[float] = None) -> int:
    """1-indexed season count since the epoch month. Gapless and monotonic."""
    return _season.season_number(now)


# ---------------------------------------------------------------------------
# the player's pass
# ---------------------------------------------------------------------------
@dataclass
class PlayerBattlePass:
    season_id: str
    tier: BattlePassTier = BattlePassTier.FREE
    xp: int = 0
    level: int = 1
    claimed_free: List[int] = field(default_factory=list)
    claimed_senior: List[int] = field(default_factory=list)
    claimed_super: List[int] = field(default_factory=list)
    weekly_bonus_claimed: List[int] = field(default_factory=list)
    purchased_at: Optional[float] = None

    def __post_init__(self) -> None:
        # A stored row may hold the tier as text (older schema) or not hold it
        # at all (a hand-edited row). Neither may lock the player out.
        if isinstance(self.tier, str):
            try:
                self.tier = BattlePassTier(self.tier)
            except ValueError:
                self.tier = BattlePassTier.FREE
        elif not isinstance(self.tier, BattlePassTier):
            self.tier = BattlePassTier.FREE

        self.xp = int(self.xp or 0)
        self.level = max(1, min(MAX_LEVEL, int(self.level or 1)))

        # The claim lists are the client's rendering input, so a duplicate draws
        # a row the player already owns. De-duplicate and sort defensively.
        for name in ("claimed_free", "claimed_senior", "claimed_super",
                     "weekly_bonus_claimed"):
            value = getattr(self, name)
            if not isinstance(value, list):
                value = []
            seen = set()
            cleaned = []
            for item in value:
                try:
                    n = int(item)
                except (TypeError, ValueError):
                    continue
                if n not in seen:
                    seen.add(n)
                    cleaned.append(n)
            setattr(self, name, sorted(cleaned))

    @property
    def claims_for(self) -> Dict[BattlePassTier, List[int]]:
        return {
            BattlePassTier.FREE: self.claimed_free,
            BattlePassTier.SENIOR: self.claimed_senior,
            BattlePassTier.SUPER: self.claimed_super,
        }

    def as_dict(self) -> dict:
        """The stored shape. One place, so a new field cannot be written twice."""
        return {
            "season_id": self.season_id,
            "tier": self.tier.value,
            "xp": self.xp,
            "level": self.level,
            "claimed_free": list(self.claimed_free),
            "claimed_senior": list(self.claimed_senior),
            "claimed_super": list(self.claimed_super),
            "weekly_bonus_claimed": list(self.weekly_bonus_claimed),
            "purchased_at": self.purchased_at,
        }


def get_xp_for_level(level: int) -> int:
    """Total XP required to REACH ``level``."""
    if level <= 1:
        return 0
    return sum(XP_PER_LEVEL[1:level + 1])


def get_level_for_xp(xp: int) -> int:
    """The level ``xp`` buys. Never above MAX_LEVEL."""
    for level in range(MAX_LEVEL, 0, -1):
        if xp >= get_xp_for_level(level):
            return level
    return 1


# ---------------------------------------------------------------------------
# season rollover: a finished season is banked, never silently dropped
# ---------------------------------------------------------------------------
def summarize_season(bp: PlayerBattlePass, ended_at: Optional[float] = None,
                     observed_at: Optional[float] = None) -> dict:
    """What a player did in the season they just finished.

    ``ended_at`` is the SEASON'S real end, not the moment the player happened to
    log in. A player who returns three days late must not be told the season
    lasted longer than it did, so the observation time is reported separately in
    ``observed_at`` and the two are allowed to disagree.

    ``tier_purchased`` is recorded because the paid claims are dropped with the
    track: without it, support cannot answer "I bought this, where is it".
    """
    season = bp.season_id
    # The season's REAL end, from the clock. Never the caller's
    # observation: a player who returns three days late must not be
    # told the season lasted longer than it did.
    season_end = get_season_end(season)
    # The observation time is the CALLER'S. Only fall back to the clock when
    # the caller passed None: `ended_at` is the season's end, never the moment
    # the player was seen. Substituting it here made every late returner look
    # like they had finished exactly on time and threw away the argument the
    # one production caller (get_or_create_bp) supplies.
    if observed_at is None:
        observed_at = time.time()

    def earned(tier: BattlePassTier, claimed: List[int]) -> dict:
        stones = qi = tickets = 0
        for level in claimed:
            reward = REWARD_TABLES[tier].get(level, {})
            stones += int(reward.get("spirit_stones", 0) or 0)
            qi += int(reward.get("qi", 0) or 0)
            tickets += int(reward.get("gacha_tickets", 0) or 0)
        return {"tiers_claimed": len(claimed), "spirit_stones": stones,
                "qi": qi, "gacha_tickets": tickets}

    reached = max(1, min(int(bp.level or 1), MAX_LEVEL))
    paid_tier = bp.tier
    if paid_tier == BattlePassTier.SUPER:
        paid_claims = list(bp.claimed_senior) + list(bp.claimed_super)
        unclaimed_paid = max(0, 2 * reached - len(bp.claimed_senior)
                             - len(bp.claimed_super))
    elif paid_tier == BattlePassTier.SENIOR:
        paid_claims = list(bp.claimed_senior)
        unclaimed_paid = max(0, reached - len(bp.claimed_senior))
    else:
        paid_claims = []
        unclaimed_paid = 0

    return {
        "season_id": season,
        "season_number": _season.season_number(get_season_start(season)),
        "final_level": reached,
        "final_xp": int(bp.xp or 0),
        "tier_purchased": paid_tier.value,
        "purchased_at": bp.purchased_at,
        "earned_free": earned(BattlePassTier.FREE, bp.claimed_free),
        "earned_paid": (earned(paid_tier, paid_claims)
                        if paid_tier != BattlePassTier.FREE else {}),
        "unclaimed_free": max(0, reached - len(bp.claimed_free)),
        "unclaimed_paid": unclaimed_paid,
        "ended_at": season_end,
        "observed_at": observed_at,
    }


def record_season_history(player: dict, summary: dict) -> None:
    """Bank a finished season, newest first, bounded, no duplicates.

    De-duplicated on ``season_id`` because the rollover runs on every
    pass-touching call: a player who opens two routes in the same new season
    would otherwise get two entries for the season that just ended.
    """
    history = player.get("battle_pass_history")
    if not isinstance(history, list):
        history = []
    season_id = summary.get("season_id")
    history = [row for row in history
               if isinstance(row, dict) and row.get("season_id") != season_id]
    history.insert(0, summary)
    player["battle_pass_history"] = history[:SEASON_HISTORY_LIMIT]


def _fresh_bp(season_id: str) -> dict:
    """A new season's track.

    The paid tier does NOT carry over: a new season is a new purchase, and a
    carry-over would be a free season of a paid product.
    """
    return PlayerBattlePass(season_id=season_id).as_dict()


def get_or_create_bp(player: dict, now: Optional[float] = None) -> PlayerBattlePass:
    """The player's pass for the CURRENT season, rolling over if it has ended.

    The canonical entry point for every XP source and every pass route. Calling
    it is what performs the rollover: there is no scheduled job, because the
    season id is a property of the clock and any pass-touching call notices the
    change.
    """
    if now is None:
        now = time.time()
    current = get_season_id(now)
    stored = player.get("battle_pass")
    if not isinstance(stored, dict):
        stored = {}

    if stored.get("season_id") != current:
        if stored.get("season_id"):
            # A finished season. Bank what it was BEFORE resetting.
            try:
                finished = PlayerBattlePass(**{
                    k: v for k, v in stored.items()
                    if k in PlayerBattlePass.__dataclass_fields__
                })
                record_season_history(
                    player,
                    summarize_season(finished,
                                     ended_at=get_season_end(finished.season_id),
                                     observed_at=now),
                )
            except Exception:
                # A corrupt stored row must never lock a player out of the new
                # season. Losing the summary is recoverable; being stuck is not.
                pass
        player["battle_pass"] = _fresh_bp(current)
        stored = player["battle_pass"]

    bp = PlayerBattlePass(**{k: v for k, v in stored.items()
                             if k in PlayerBattlePass.__dataclass_fields__})
    bp.season_id = current
    player["battle_pass"] = bp.as_dict()
    return bp


# ---------------------------------------------------------------------------
# XP: earned, never minted
# ---------------------------------------------------------------------------
def add_xp(bp: PlayerBattlePass, amount: int) -> dict:
    """The one chokepoint every earn path funnels through.

    Refuses a negative grant outright rather than clamping it to zero: XP is
    earned, never spent, and a silent zero would let a caller believe it had
    applied a correction. Clamps a single grant to ``MAX_XP_PER_GRANT`` so one
    in-process call cannot mint a season.
    """
    if amount is None:
        raise ValueError("battle-pass XP is earned, never removed; amount is required")
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("battle-pass XP amount must be an integer") from exc
    if amount < 0:
        raise ValueError(
            f"battle-pass XP is earned, never removed; refusing {amount}")

    old_level = bp.level
    granted = min(amount, MAX_XP_PER_GRANT)
    bp.xp += granted
    bp.level = min(get_level_for_xp(bp.xp), MAX_LEVEL)
    return {
        "old_level": old_level,
        "new_level": bp.level,
        "leveled_up": bp.level > old_level,
        "xp_gained": granted,
        "xp_clamped": amount > MAX_XP_PER_GRANT,
        "total_xp": bp.xp,
    }


def grant_xp(player: dict, amount: int, source: str = "gameplay") -> dict:
    """Award XP from an earn path. The ONLY way any caller may add XP.

    Every earn path computes the amount server-side (daily boss 50, sect
    contribution a tenth capped at 100, tribulation a tenth capped at 200, and so
    on) and funnels through here, so no route takes the magnitude from a client.
    """
    bp = get_or_create_bp(player, time.time())
    result = add_xp(bp, amount)
    player["battle_pass"] = bp.as_dict()
    return {
        "ok": True,
        "source": source,
        "xp_gained": result["xp_gained"],
        "new_level": result["new_level"],
        "leveled_up": result["leveled_up"],
        "total_xp": result["total_xp"],
    }


# ---------------------------------------------------------------------------
# the claim ledger
# ---------------------------------------------------------------------------
def reserve_claim(players_db, player_id: str, season_id: str, tier: str,
                  level: int) -> Tuple[bool, Optional[dict]]:
    """Reserve the natural claim key. Returns ``(is_first, stored_receipt)``.

    One row back = first claim, pay it. Zero rows = already paid, pay nothing
    and hand back the receipt the first attempt stored. There is deliberately no
    SELECT before the write, so there is no window between "have I been paid?"
    and "pay": under a second concurrent claim the second statement blocks on
    the row, re-reads it after the first commits, sees the conflict, and updates
    nothing. The once-only decision stays a property of the DATA.
    """
    conn = _connection(players_db)
    if conn is None:
        key = (player_id, season_id, tier, int(level))
        with _MEM_CLAIM_LOCK:
            if key in _MEM_CLAIM_KEYS:
                return False, _MEM_CLAIM_KEYS[key] or None
            _MEM_CLAIM_KEYS[key] = None
        return True, None

    with conn.cursor() as cur:
        cur.execute(_CLAIM_RESERVE_SQL,
                    (player_id, season_id, tier, int(level), _dumps({}), 1))
        inserted = cur.fetchone() is not None
    if inserted:
        return True, None
    with conn.cursor() as cur:
        cur.execute(_CLAIM_READ_SQL, (player_id, season_id, tier, int(level)))
        row = cur.fetchone()
    return False, (_as_json(row[0]) if row else None)


def release_claim(players_db, player_id: str, season_id: str, tier: str,
                  level: int) -> None:
    """Give a REFUSED claim's key back, so it stays claimable.

    The database rolls the reserved row back with the refused transaction. A
    process-local dict has no rollback, so without this a refusal would burn
    the position for the rest of the season: the ledger says "already paid"
    while the player received nothing, and every later attempt is answered as a
    replay with an EMPTY reward and a 200. The player's own mistake -- tapping
    a level they have not reached yet, or sweeping a tier they have not bought
    -- permanently deleted real money.

    This is the claim-side counterpart to :func:`release_purchase`, and it is
    the reason the two ledgers behave the same way on a retry.
    """
    if _connection(players_db) is not None:
        # The transaction rolls back on refusal; the row is already gone.
        return
    key = (player_id, season_id, tier, int(level))
    with _MEM_CLAIM_LOCK:
        # Only a reservation that never received a receipt is released. A key
        # holding a receipt was genuinely paid, so releasing it would let the
        # same reward be paid twice. The route only calls this on the refusal
        # path, but the guard belongs here rather than in every caller.
        if _MEM_CLAIM_KEYS.get(key) is None:
            _MEM_CLAIM_KEYS.pop(key, None)


def record_claim_reward(players_db, player_id: str, season_id: str, tier: str,
                        level: int, reward: dict,
                        player_level: Optional[int] = None) -> None:
    """Write the receipt into the row reserved by :func:`reserve_claim`.

    Same transaction, so no other process can read this receipt while it is
    still empty.

    ``player_level`` is OPTIONAL, and omitting it omits ``new_level`` from the
    receipt rather than storing a made-up number. It defaulted to 1, and both
    route call sites below omit it, so every receipt this file writes claimed
    the player was level 1 and a replay answered with it. Measured over HTTP:
    a level-30 player claimed level 2 and got ``new_level: 30``; the replay,
    with a fresh Idempotency-Key so it took the replay branch, answered
    ``new_level: 1``.

    Omitting the key is safe because the replay path is
    ``receipt.get("new_level", bp.level)``: it falls back to the LIVE level, and
    claiming awards no XP, so the level at replay IS the level at the first
    claim. A wrong stored number is strictly worse than no stored number.
    """
    # The stored value is the RECEIPT -- the reward plus the level it left
    # the player at -- not a bare marker. The replay path answers from
    # this, so storing `reward` alone is what made a replay answer an
    # empty reward.
    receipt: Dict[str, Any] = {"reward": dict(reward or {})}
    if player_level is not None:
        receipt["new_level"] = int(player_level)
    conn = _connection(players_db)
    if conn is None:
        with _MEM_CLAIM_LOCK:
            _MEM_CLAIM_KEYS[(player_id, season_id, tier, int(level))] = receipt
        return
    with conn.cursor() as cur:
        cur.execute(_CLAIM_RECEIPT_SQL,
                    (_dumps(receipt), int(player_level), player_id, season_id,
                     tier, int(level)))


def wallet_claim_key(player_id: str, season_id: str, tier: str, level: int,
                     kind: str = "claim") -> str:
    """The wallet-store idempotency key for ONE pass payout. Server-derived.

    It mirrors the claim ledger's natural identity -- (player, season, tier,
    position) -- so the wallet's own receipt table is an INDEPENDENT second
    guard on the same once-only property. A client that varies its
    Idempotency-Key header, or sends none, cannot mint a second key here.

    ``kind`` separates the two intents that share (player, season, tier): a
    level claim and a weekly bonus are different payouts and must not collide.
    """
    return "battle_pass:%s:%s:%s:%s:%s" % (kind, player_id, season_id, tier,
                                           int(level))


# ---------------------------------------------------------------------------
# the purchase ledger
# ---------------------------------------------------------------------------
def reserve_purchase(players_db, ledger_key: str, player_id: str, season_id: str,
                     tier: str, cost: int) -> Tuple[bool, Optional[dict]]:
    """Reserve the purchase key. Returns ``(is_first, stored_response)``.

    ``ledger_key`` is the caller's Idempotency-Key namespaced by player id, which
    is required rather than optional for a purchase: a player may buy Senior and
    then Super in one season (two intents, two keys), and two players sending the
    byte-identical key must both be charged.
    """
    conn = _connection(players_db)
    if conn is None:
        with _MEM_PURCHASE_LOCK:
            if ledger_key in _MEM_PURCHASE_KEYS:
                return False, None
            _MEM_PURCHASE_KEYS.add(ledger_key)
        return True, None

    with conn.cursor() as cur:
        cur.execute(_PURCHASE_RESERVE_SQL,
                    (ledger_key, player_id, season_id, tier, int(cost), _dumps({})))
        inserted = cur.fetchone() is not None
    if inserted:
        return True, None
    with conn.cursor() as cur:
        cur.execute(_PURCHASE_READ_SQL, (ledger_key,))
        row = cur.fetchone()
    return False, (_as_json(row[0]) if row else None)


def record_purchase_response(players_db, ledger_key: str, response: dict) -> None:
    """Store the receipt so a replay from another process can be ANSWERED."""
    conn = _connection(players_db)
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(_PURCHASE_RECEIPT_SQL, (_dumps(response), ledger_key))


def release_purchase(ledger_key: str) -> None:
    """Give a refused purchase's key back in no-database mode.

    The database rolls the row back with the failed charge. A process-local set
    has no rollback, so without this an under-funded purchase burns its own retry
    key forever and the player can never complete it after topping up -- an
    entirely ordinary sequence: tap buy, tap recharge, tap buy again.
    """
    with _MEM_PURCHASE_LOCK:
        _MEM_PURCHASE_KEYS.discard(ledger_key)


# ---------------------------------------------------------------------------
# the reward tables, read
# ---------------------------------------------------------------------------
def reward_for(level: int, tier: BattlePassTier) -> Optional[dict]:
    """The reward for a position, or None when the position does not exist.

    ``None`` rather than ``{}`` for a level off the track: an unchecked level 0
    would otherwise answer 200 having paid nothing and marked nothing, which is
    a success the player can neither use nor retry.
    """
    if not isinstance(level, int) or level < 1 or level > MAX_LEVEL:
        return None
    reward = REWARD_TABLES[tier].get(level)
    return dict(reward) if reward is not None else None


def _tier_unlocked(player_tier: BattlePassTier, wanted: BattlePassTier) -> bool:
    """Buying a higher tier implies the lower one."""
    order = [BattlePassTier.FREE, BattlePassTier.SENIOR, BattlePassTier.SUPER]
    return order.index(player_tier) >= order.index(wanted)


def claim_reward(player_bp: PlayerBattlePass, level: int,
                 tier: BattlePassTier) -> Tuple[bool, dict]:
    """Pure decision: may this player claim this position on this track?

    Synchronous, three arguments, and never shadowed by a route handler of the
    same name -- an earlier version of this file named the nested FastAPI handler
    ``claim_reward`` too, which rebound the name for the whole enclosing function
    scope and made the helper call inside the handler invoke the coroutine
    instead, 500ing every claim. The handler is named ``claim_reward_endpoint``
    for that reason.
    """
    if not isinstance(level, int) or level < 1 or level > MAX_LEVEL:
        return False, {"error": "Level not reached"}
    if level > player_bp.level:
        return False, {"error": "Level not reached"}

    if level in player_bp.claims_for[tier]:
        return False, {"error": "Already claimed"}

    if tier == BattlePassTier.FREE:
        player_bp.claimed_free.append(level)
    elif tier == BattlePassTier.SENIOR:
        if not _tier_unlocked(player_bp.tier, BattlePassTier.SENIOR):
            return False, {"error": "Tier not unlocked"}
        player_bp.claimed_senior.append(level)
    elif tier == BattlePassTier.SUPER:
        if player_bp.tier != BattlePassTier.SUPER:
            return False, {"error": "Tier not unlocked"}
        player_bp.claimed_super.append(level)
    else:
        return False, {"error": "Invalid tier"}

    reward = reward_for(level, tier)
    if reward is None:
        return False, {"error": "Level not reached"}
    return True, reward


def get_available_rewards(player_bp: PlayerBattlePass) -> dict:
    """Every unclaimed position the player has actually reached, per track."""
    available: Dict[str, List[dict]] = {"free": [], "senior": [], "super": []}
    for level in range(1, player_bp.level + 1):
        for tier in BattlePassTier:
            if tier == BattlePassTier.SENIOR and not _tier_unlocked(
                    player_bp.tier, BattlePassTier.SENIOR):
                continue
            if tier == BattlePassTier.SUPER and player_bp.tier != BattlePassTier.SUPER:
                continue
            if level in player_bp.claims_for[tier]:
                continue
            available[tier.value].append({"level": level, **reward_for(level, tier)})
    return available


def track_view(player_bp: PlayerBattlePass) -> dict:
    """Every level of the track, each position marked claimed/claimable/locked.

    A missing state is a dead button in the client, and an empty reward cell is a
    promise the server cannot keep, so every row carries both for all three
    tiers regardless of what the player currently owns.
    """
    rows = []
    for level in range(1, MAX_LEVEL + 1):
        row: Dict[str, Any] = {"level": level}
        for tier in BattlePassTier:
            reward = reward_for(level, tier) or {}
            if level in player_bp.claims_for[tier]:
                state = "claimed"
            elif level > player_bp.level:
                state = "locked"
            elif tier == BattlePassTier.FREE:
                state = "claimable"
            elif tier == BattlePassTier.SENIOR and _tier_unlocked(
                    player_bp.tier, BattlePassTier.SENIOR):
                state = "claimable"
            elif tier == BattlePassTier.SUPER and player_bp.tier == BattlePassTier.SUPER:
                state = "claimable"
            else:
                state = "locked"
            row[tier.value] = {"state": state, "reward": reward}
        rows.append(row)
    return {"levels": rows}


def upgrade_tier(player_bp: PlayerBattlePass,
                 new_tier: BattlePassTier) -> bool:
    """Move the player up a track. Never sideways and never down."""
    order = [BattlePassTier.FREE, BattlePassTier.SENIOR, BattlePassTier.SUPER]
    if order.index(new_tier) <= order.index(player_bp.tier):
        return False
    player_bp.tier = new_tier
    player_bp.purchased_at = time.time()
    return True


def claim_weekly_bonus(player_bp: PlayerBattlePass, week: int) -> Tuple[bool, dict]:
    """The weekly bonus. Paid tracks only, once per week.

    ``week`` is a POSITION IN THE SEASON, and the season decides how many of
    them exist. It is never the caller's to invent.

    The bug this closes, measured over HTTP with a genuinely-paid Senior
    account and a valid session: the route took ``week`` straight off the query
    string and only ever checked "is this id already in the list", so the list
    de-duplicated the ids THE ATTACKER CHOSE and paid 500 stones + 5,000 XP for
    each new one:

        week=1          -> 200  500 stones  5000 xp
        week=2          -> 200  500 stones  5000 xp
        week=3          -> 200  500 stones  5000 xp
        week=-5         -> 200  500 stones  5000 xp
        week=0          -> 200  500 stones  5000 xp
        week=1000000000 -> 200  500 stones  5000 xp
        stones 100500 -> 103500,  battle-pass xp 0 -> 30000

    Unbounded for as long as the season lasts, and each call also skipped
    ``MAX_XP_PER_GRANT`` (1,000) because the bonus is added here rather than
    through :func:`add_xp`. A ``ge=1`` bound alone would not have helped --
    the attacker supplies the id, so the ceiling has to come from the clock.

    So the id is checked against the week the server clock says has actually
    begun: week 1 at the season start, week N once N-1 weeks have elapsed. A
    week that has not arrived is refused, and a week outside the season is
    refused, so the most one account can ever take is the number of weeks the
    season really contained.

    The XP is added HERE and nowhere else. The route used to add it a second
    time, so a Senior player's 5000 bonus became 10000 and the stored level was
    never recomputed to match the XP actually held.
    """
    # The clock is read HERE rather than passed in: the helper's parameter list
    # is part of this file's published shape (two owner test files assert it is
    # exactly ("player_bp", "week")), so the fix cannot widen the signature.
    now = time.time()
    start = get_season_start(player_bp.season_id)
    end = get_season_end(player_bp.season_id)
    weeks_in_season = max(1, int(math.ceil((end - start) / (7 * 86400))))
    reached = int((now - start) // (7 * 86400)) + 1
    if week < 1 or week > weeks_in_season:
        return False, {"error": f"Week must be 1-{weeks_in_season}"}
    if week > reached:
        return False, {"error": "Week not reached yet"}

    if player_bp.tier == BattlePassTier.FREE:
        return False, {"error": "Free tier has no weekly bonus"}
    if week in player_bp.weekly_bonus_claimed:
        return False, {"error": "Already claimed"}

    bonus = WEEKLY_BONUS.get(player_bp.tier)
    if bonus is None:
        return False, {"error": "Free tier has no weekly bonus"}

    player_bp.weekly_bonus_claimed.append(week)
    old_level = player_bp.level
    player_bp.xp += int(bonus["xp"])
    player_bp.level = min(get_level_for_xp(player_bp.xp), MAX_LEVEL)
    return True, {
        "xp_gained": int(bonus["xp"]),
        "spirit_stones": int(bonus["spirit_stones"]),
        "new_level": player_bp.level,
        "leveled_up": player_bp.level > old_level,
    }


# ---------------------------------------------------------------------------
# spirit stones
# ---------------------------------------------------------------------------
def stone_balance(player: dict) -> int:
    """The player's spendable spirit-stone balance.

    The canonical player shape stores stones as a tiered dict; older fixtures
    store a plain int. Both are accepted, and the dict form's ``low_grade`` tier
    is the F2P balance -- the same convention sect_wall.stone_balance and
    cosmetics._can_purchase already use.
    """
    stones = player.get("spirit_stones")
    if isinstance(stones, dict):
        return int(stones.get("low_grade", 0) or 0)
    return int(stones or 0)


def grant_stones(player: dict, amount: int) -> None:
    """Add ``amount`` spirit stones. Dict or scalar form."""
    if amount is None:
        return
    amount = int(amount)
    if amount <= 0:
        return
    stones = player.get("spirit_stones")
    if isinstance(stones, dict):
        stones["low_grade"] = int(stones.get("low_grade", 0) or 0) + amount
    else:
        player["spirit_stones"] = int(stones or 0) + amount


def earn_wallet_stones(wallet_store, player_id: str, amount: int,
                       idempotency_key: str) -> Optional[dict]:
    """Credit spirit stones through the wallet dispatcher, or None if no wallet.

    THE WALLET IS THE MONEY PATH. ``wallet_dispatch.earn`` carries the per-key
    idempotency ledger and the currency whitelist that a bare
    ``player["spirit_stones"]`` write has neither of. The key is SERVER-DERIVED
    (see :func:`wallet_claim_key`), so a retry is still a replay no matter what
    the client sends as its Idempotency-Key.

    Returns the wallet receipt, or None when no wallet is bound -- which is the
    two-argument mount's normal case and must not be an error. A refusal
    (``ok`` False) is RETURNED rather than raised, so the row write below still
    happens and the player is not left with a burned claim and nothing paid.
    """
    if wallet_store is None or not player_id:
        return None
    amount = int(amount or 0)
    if amount <= 0:
        return None
    # Imported HERE, not at module scope: wallet_dispatch picks its backend at
    # import time from WC_PG_HOST, and this module is imported by test runs with
    # that variable deliberately unset.
    from wallet_dispatch import earn as wallet_earn
    return wallet_earn(wallet_store, player_id, WALLET_STONE_CURRENCY,
                       amount, idempotency_key, "battle_pass_reward")


def wallet_stone_balance(wallet_store, player_id: str) -> Optional[int]:
    """The WALLET's spirit-stone balance, or None when it cannot be read.

    None means "no answer" and is NEVER 0. A 0 here would read as "the player is
    broke" and refuse a purchase their player row can pay for, which is the
    exact symptom this exists to remove.

    Both backend shapes are read: PostgreSQL hands back a plain dict, the
    in-memory store hands back the ``CurrencyBalance`` dataclass. Getting this
    wrong is a live crash hazard in exactly one of the two modes.
    """
    if wallet_store is None or not player_id:
        return None
    try:
        wallet = wallet_store.get(player_id)
    except Exception:  # noqa: BLE001 -- an unreadable wallet is no answer
        return None
    if wallet is None:
        return None
    balance = getattr(wallet, "balance", None)
    if balance is None:
        return None
    if isinstance(balance, dict):
        return int(balance.get(WALLET_STONE_CURRENCY, 0) or 0)
    return int(getattr(balance, WALLET_STONE_CURRENCY, 0) or 0)


def spend_wallet_stones(wallet_store, player_id: str, amount: int,
                        idempotency_key: str) -> Optional[dict]:
    """Debit spirit stones through the wallet dispatcher, or None if no wallet.

    THE MIRROR OF :func:`earn_wallet_stones`, and it exists because the purchase
    route used to move stones by writing ``player["spirit_stones"]`` and nothing
    else. The player row carries no currency whitelist, no per-key idempotency
    ledger and no version guard, so a debit through it is not once-only by any
    mechanism -- it is once-only only because the route's own purchase ledger
    happens to sit in front of it.

    THE VERSION IS THE SERVER'S, never a literal. The wallet compares the value
    it is given against its own row and refuses a mismatch; passing 0 makes every
    charge answer "stale wallet_version". ``cosmetics._wallet_spend`` and
    ``crafting._wallet_spend`` do it this way for the same reason.

    NO ``route`` KWARG. ``wallet_dispatch.spend`` takes six positional params
    and does not accept one; passing it raised TypeError inside a call this shape
    is destined to wrap, and the fallback then charged the player row anyway --
    a 200 with the wallet untouched. ``cosmetics.py:1126`` records that bug
    measured on the mounted app.

    Returns the wallet receipt, or None when no wallet is bound -- the
    two-argument mount's normal case, and not an error.
    """
    if wallet_store is None or not player_id:
        return None
    amount = int(amount or 0)
    if amount <= 0:
        return None
    from wallet_dispatch import spend as wallet_spend
    current = wallet_store.get(player_id)
    version = int(getattr(current, "version", 0) or 0)
    return wallet_spend(wallet_store, player_id, WALLET_STONE_CURRENCY,
                        amount, version, idempotency_key)


def wallet_upgrade_key(player_id: str, season_id: str, tier: str) -> str:
    """The wallet-store idempotency key for ONE track purchase. Server-derived.

    It is the purchase's natural identity -- (player, season, tier) -- which is
    the same tuple ``upgrade_tier_endpoint`` already keys its own ledger row on.
    So the wallet's receipt table is an INDEPENDENT second guard on the same
    once-only property: a client that varies its Idempotency-Key header, or
    sends none, cannot mint a second key here. The header is still required
    (``idem_gate.py``) because a retry label is the client's to send; it is
    simply not what either ledger is keyed on.
    """
    return "battle_pass:upgrade:%s:%s:%s" % (player_id, season_id, tier)


def _pay_reward(player: dict, reward: dict, *, wallet_store=None,
                player_id: str = "", claim_key: str = "") -> None:
    """Apply a reward to a player. Called only AFTER the ledger row is in.

    Qi goes through the shared helper because the breakthrough gate reads
    ``player["progress"]``: crediting ``player["qi"]`` alone is inert, so the
    reward would show in the UI and never advance anything.

    STONES ALSO GO TO THE WALLET WHEN ONE IS BOUND. Before this, every free and
    paid tier, and the weekly bonus, wrote ``player["spirit_stones"]`` and
    nothing else -- measured: a claim-all paying 990 stones moved the player row
    500 -> 1490 and left the wallet at 0. That is an off-wallet balance: the
    store carrying the idempotency ledger and the currency whitelist never saw
    the grant.

    THE ROW IS STILL WRITTEN, AND THAT IS DELIBERATE. This module's own purchase
    route charges ``player["spirit_stones"]["low_grade"]`` and cosmetics reads
    the same tier, so a wallet-only credit would move the reward somewhere the
    pass cannot spend it -- measured: a wallet holding 5000 is refused by
    ``/battle-pass/{id}/upgrade`` with "Insufficient Spirit Stones". Both stores
    are credited so the wallet holds a replay-guarded record of every pass grant
    while the row keeps the stones spendable today. Reconciling the two ledgers
    is a tree-wide change and is not this fix.

    The keyword arguments are optional so the existing two-argument callers --
    ``battle_pass_routes.py``'s ``/api/claim_reward`` among them -- keep working.
    """
    stones = int(reward.get("spirit_stones", 0) or 0)
    if stones > 0 and wallet_store is not None and player_id and claim_key:
        earn_wallet_stones(wallet_store, player_id, stones, claim_key)
    grant_stones(player, stones)

    qi = int(reward.get("qi", 0) or 0)
    player["progress"] = grant_qi(player, qi)

    tickets = int(reward.get("gacha_tickets", 0) or 0)
    if tickets:
        inv = player.setdefault("inventory", {})
        inv["Gacha Ticket"] = int(inv.get("Gacha Ticket", 0) or 0) + tickets

    for item in reward.get("items", []) or []:
        inv = player.setdefault("inventory", {})
        inv[item] = int(inv.get(item, 0) or 0) + 1

    if reward.get("cosmetic"):
        cosmetics = player.setdefault("cosmetics", {})
        cosmetics[reward["cosmetic"]] = {"owned": True, "granted_at": time.time()}


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------
def register_battle_pass_routes(app, store_provider,
                                wallet_store_provider=None) -> None:
    """Mount the battle-pass routes.

    ``store_provider`` is called per request rather than a captured
    ``players_db``, so a test that swaps the store sees the swap.
    ``wallet_store_provider`` is optional and defaults to None so every existing
    two-argument caller keeps working unchanged.

    OWNERSHIP: every per-player route resolves the player from the SESSION.
    ``require_player`` authenticates AND enforces that any ``player_id`` this
    request names -- in the path, the query or the body -- is the caller's own,
    so a signed-in player cannot read or spend another player's pass by editing
    the URL. ``player_id`` stays in the path because the shipped client calls
    these URLs; the session is the authority and the path is a claim about it.
    """
    def _player_or_404(players_db, player_id: str) -> None:
        """404 for an unknown player. CALL THIS BEFORE transaction().

        `players_db.transaction(player_id)` raises KeyError itself for an
        id it does not hold, so a check placed inside the block never runs
        and the caller gets a 500. Proving existence first is what makes the
        404 reachable at all.
        """
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")

    def _tier_or_400(tier: str) -> BattlePassTier:
        try:
            return BattlePassTier(tier)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid tier")

    def _wallet():
        """The bound wallet store, or None. Resolved PER REQUEST.

        main.py mounts this module with two arguments, so ``None`` is the
        normal production value until that call grows a third. The pass still
        pays through the player row in that case -- see ``_pay_reward`` -- so a
        missing wallet degrades to the old behaviour rather than to a lost
        reward. When one IS bound, the grant is additionally recorded in the
        wallet, which is the store carrying the idempotency ledger and the
        currency whitelist.

        Never raises: an unreadable wallet must not turn a claim into a 500
        after the reservation was already taken.
        """
        if wallet_store_provider is None:
            return None
        try:
            return wallet_store_provider()
        except Exception:  # noqa: BLE001 -- no wallet is a fallback, not a fault
            return None

    @app.get("/battle-pass/season")
    async def get_season_route():
        """The live season clock. No player in it, so no session is required."""
        return {"ok": True, "season": get_season_progress(time.time())}

    @app.get("/battle-pass/{player_id}")
    async def get_battle_pass_route(player_id: str,
                                    identity: PlayerIdentity = Depends(require_player)):
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404.
        _player_or_404(players_db, player_id)
        with players_db.transaction(player_id):
            bp = get_or_create_bp(players_db[player_id], time.time())
            payload = {
                "ok": True,
                "battle_pass": {
                    "season": get_season_progress(time.time()),
                    "season_number": get_season_number(),
                    "tier": bp.tier.value,
                    "level": bp.level,
                    "max_level": MAX_LEVEL,
                    "xp": bp.xp,
                    "season_xp_total": TOTAL_XP,
                    "xp_for_next": (get_xp_for_level(bp.level + 1) - bp.xp
                                    if bp.level < MAX_LEVEL else 0),
                    "claimed": {
                        "free": list(bp.claimed_free),
                        "senior": list(bp.claimed_senior),
                        "super": list(bp.claimed_super),
                    },
                    "available_rewards": get_available_rewards(bp),
                    "weekly_bonus_claimed": list(bp.weekly_bonus_claimed),
                    "purchased_at": bp.purchased_at,
                    "track": track_view(bp),
                },
                "season_history": player_history(players_db[player_id]),
            }
        return payload

    @app.get("/battle-pass/{player_id}/summary")
    async def get_battle_pass_summary_route(
            player_id: str, season: Optional[str] = None,
            identity: PlayerIdentity = Depends(require_player)):
        """One SEASON's summary: what the player did, and what they left behind.

        WHY THIS ROUTE EXISTS. The season proof (`battle_pass_season_proof.py`)
        and the play script (`play_battle_pass.py`) both read the finished
        season's summary over HTTP. There was no route for it: the path was
        absent from the app's route table, so the request answered 404 for every
        caller and the proof reported a rollover failure no matter what the
        rollover did. The database half of that check was sound the whole time --
        only the read had nowhere to land.

        `GET /battle-pass/{player_id}` does carry the banked rows, under
        `season_history`, and that surface stays exactly as it was. This route is
        the single-season view on top of it: one row, keyed by the `season`
        query parameter, which is what both callers actually want to assert on.

        `season` DEFAULTS to the most recently banked season, so a client that
        has not tracked season ids gets the current answer rather than a bare
        list it has to pick from. A season the player never played is a 404
        rather than a 200 with an empty object, because "this season does not
        exist for you" and "this season exists and earned nothing" are different
        facts and a support answer depends on telling them apart.

        THE ROW SERVED IS THE RENDERED SUMMARY, not the raw claim record echoed
        back: `summarize_season` is what turns three claimed free tiers into a
        stones total, so a route serving the stored record unchanged would look
        correct while handing the panel a shape it cannot render.

        Ownership is the session's, not the path's: `require_player` resolves
        the caller and rejects one naming somebody else, exactly as it does for
        the five sibling routes on this prefix.
        """
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404. Same ordering as
        # every sibling route on this prefix.
        _player_or_404(players_db, player_id)

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            history = player_history(players_db[player_id])

        if season is None:
            if not history:
                raise HTTPException(status_code=404,
                                    detail="No finished season for this player")
            return {"ok": True, "season_summary": history[0]}

        for row in history:
            if row.get("season_id") == season:
                return {"ok": True, "season_summary": row}
        raise HTTPException(
            status_code=404,
            detail=f"No battle-pass summary for season {season}")

    @app.post("/battle-pass/{player_id}/claim")
    async def claim_reward_endpoint(player_id: str, level: int, tier: str,
                                    request: Request,
                                    identity: PlayerIdentity = Depends(require_player)):
        """Claim one position. Pays once per (player, season, tier, level).

        The Idempotency-Key is accepted but never used as the key: a claim has
        exactly one natural identity, so a retry that invents a fresh key is
        still a replay. That is the property a caller-keyed table cannot have.
        """
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404.
        _player_or_404(players_db, player_id)
        tier_enum = _tier_or_400(tier)
        if level < 1 or level > MAX_LEVEL:
            raise HTTPException(status_code=400, detail="Level not reached")

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = get_or_create_bp(player, time.time())

            # RESERVE FIRST, PAY SECOND, in one transaction. A refusal or a
            # crash mid-payout rolls the marker back and the tier stays
            # claimable. The caller may still name WHICH position it is
            # claiming -- it must, to say which button it pressed -- but not
            # what that position pays or whether it has been paid.
            is_first, stored = reserve_claim(
                players_db, player_id, bp.season_id, tier_enum.value, level)
            if not is_first:
                receipt = stored or {}
                return {
                    "ok": True,
                    "replayed": True,
                    "reward": receipt.get("reward", {}),
                    "level": level,
                    "tier": tier_enum.value,
                    "new_level": receipt.get("new_level", bp.level),
                }

            ok, reward = claim_reward(bp, level, tier_enum)
            if not ok:
                # The reservation is released so the position stays claimable.
                # Without this, a refusal (tapped a level not reached yet)
                # permanently deletes that reward for the whole season.
                release_claim(players_db, player_id, bp.season_id,
                              tier_enum.value, level)
                raise HTTPException(status_code=400,
                                    detail=reward.get("error", "Claim failed"))

            # The receipt belongs to the FIRST attempt only: a replay returns
            # the stored one and never rewrites it.
            if is_first:
                _pay_reward(player, reward, wallet_store=_wallet(),
                            player_id=player_id,
                            claim_key=wallet_claim_key(player_id, bp.season_id,
                                                      tier_enum.value, level))
                # No level recompute: claiming awards no XP.
                player["battle_pass"] = bp.as_dict()
                # Same transaction, so no other process can read this receipt
                # while it is still empty.
                record_claim_reward(players_db, player_id, bp.season_id,
                                    tier_enum.value, level, reward)
            response = {
                "ok": True,
                "replayed": False,
                "reward": reward,
                "level": level,
                "tier": tier_enum.value,
                "new_level": bp.level,
            }

        _log_claim(bp.season_id, tier_enum.value, level, response["reward"])
        return response

    @app.post("/battle-pass/{player_id}/claim-all")
    async def claim_all_endpoint(player_id: str, tier: str,
                                 identity: PlayerIdentity = Depends(require_player)):
        """Sweep every unlocked, unclaimed position on one track, once each."""
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404.
        _player_or_404(players_db, player_id)
        tier_enum = _tier_or_400(tier)

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = get_or_create_bp(player, time.time())

            claimed = 0
            total_stones = 0
            for level in range(1, bp.level + 1):
                if level in bp.claims_for[tier_enum]:
                    continue
                is_first, _stored = reserve_claim(
                    players_db, player_id, bp.season_id, tier_enum.value, level)
                if not is_first:
                    continue
                ok, reward = claim_reward(bp, level, tier_enum)
                if not ok:
                    # A sweep that skips a position must not burn it. This is
                    # the reachable case: sweeping a tier the player has not
                    # bought skips every level, and every one of them stayed
                    # reserved-but-unpaid for the rest of the season.
                    release_claim(players_db, player_id, bp.season_id,
                                  tier_enum.value, level)
                    continue
                _pay_reward(player, reward, wallet_store=_wallet(),
                            player_id=player_id,
                            claim_key=wallet_claim_key(player_id, bp.season_id,
                                                      tier_enum.value, level))
                record_claim_reward(players_db, player_id, bp.season_id,
                                    tier_enum.value, level, reward)
                claimed += 1
                total_stones += int(reward.get("spirit_stones", 0) or 0)

            # No level recompute: a sweep pays rewards and awards no XP.
            player["battle_pass"] = bp.as_dict()
            new_level = bp.level

        return {"ok": True, "tier": tier_enum.value, "claimed_count": claimed,
                "spirit_stones": total_stones, "new_level": new_level}

    @app.post("/battle-pass/{player_id}/upgrade")
    async def upgrade_tier_endpoint(player_id: str, tier: str, request: Request,
                                    identity: PlayerIdentity = Depends(require_player)):
        """Buy a paid track. Once per key, and once per tier per season.

        ORDERING: the balance check and the reservation are both inside the
        guarded region, so a refusal releases the key in no-database mode and
        rolls the row back with the database. A player who taps buy while short,
        tops up, and taps buy again must be able to complete that purchase.
        """
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404.
        _player_or_404(players_db, player_id)
        tier_enum = _tier_or_400(tier)
        cost = TIER_COST[tier_enum]
        if cost <= 0:
            raise HTTPException(status_code=400, detail="Invalid tier")

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = get_or_create_bp(player, time.time())

            # THE LEDGER KEY IS SERVER-DERIVED, NEVER THE CALLER'S KEY.
            #
            # It is (player, season, tier): the natural identity of "this
            # player bought this track in this season". So a double-tap's
            # second insert conflicts (a replay, no charge), AND a FRESH key
            # for a tier already owned conflicts too (no silent debit).
            # A caller-minted key would make "charges once" a property of
            # the client's memory: omit it or vary it and you walk past the
            # gate entirely.
            #
            # The Idempotency-Key HEADER is still REQUIRED (idem_gate.py)
            # because a retry label is the client's to send; it is simply not
            # what the row is keyed on.
            ledger_key = f"bp_upgrade:{player_id}:{bp.season_id}:{tier_enum.value}"

            # The BALANCE check comes first, and deliberately so: a broke
            # player is refused for being broke whichever key they send. It
            # is read inside the transaction anyway -- reading it outside
            # would leave a window between the check and the debit.
            #
            # EITHER LEDGER CAN PAY. A player whose stones arrived through the
            # wallet (every award now does) has them there and not on the row,
            # and a row-only check refuses them for being broke while holding
            # the money. Neither book is an alternative to the other: they are
            # two views of one balance, so this is the LARGER of the two, never
            # the SUM -- summing would advertise 500 row + 500 wallet and then
            # decline a 900 purchase neither book alone covers.
            row_stones = stone_balance(player)
            wallet_stones = wallet_stone_balance(_wallet(), player_id)
            if max(x for x in (row_stones, wallet_stones) if x is not None) < cost:
                raise HTTPException(status_code=400,
                                    detail="Insufficient Spirit Stones")

            # The server never mints this key. See server/idem_gate.py: a
            # per-attempt key double-charges a retry, and a body-derived key
            # swallows a second genuine purchase. Required, and NOT what the
            # ledger row is keyed on.
            require_idempotency_key(request, route="battle_pass_upgrade")

            is_first, stored = reserve_purchase(
                players_db, ledger_key, player_id, bp.season_id,
                tier_enum.value, cost)
            if not is_first:
                receipt = stored or {}
                return {
                    "ok": True,
                    # A replay is ANSWERED with the first attempt's receipt, not
                    # refused: a client that timed out has to learn what it
                    # already paid.
                    "new_tier": receipt.get("new_tier", bp.tier.value),
                    "cost": receipt.get("cost", cost),
                    "replayed": True,
                }

            try:
                # THE DEBIT GOES THROUGH wallet_dispatch. The row write that used
                # to stand here bypassed the currency whitelist, the per-key
                # idempotency ledger and the version guard, so the store that
                # owns money had no record of the pass charging a player.
                #
                # ORDER: wallet first, row second, and each leg is only taken
                # when that leg can actually pay. A wallet REFUSAL is not
                # silently overwritten by the row debit -- it is released and
                # raised, because "insufficient balance" is an answer about the
                # player's money and quietly charging a second book behind it
                # makes one purchase cost two ledgers (crafting._debit_fee
                # records that reasoning).
                #
                # ROW FALLBACK IS DELIBERATE AND IS NOT A BYPASS. The two books
                # are two views of one balance, not two balances to pick from:
                # awards credit both (see _pay_reward), so a debit that touched
                # only the wallet would let the row re-spend stones already
                # spent. Charging whichever book holds the stones keeps them
                # converging instead of drifting apart.
                wallet = _wallet()
                wallet_ok = False
                if wallet is not None and (wallet_stones or 0) >= cost:
                    receipt = spend_wallet_stones(
                        wallet, player_id, cost,
                        wallet_upgrade_key(player_id, bp.season_id, tier_enum.value))
                    if receipt is not None and receipt.get("ok"):
                        wallet_ok = True
                    elif receipt is not None:
                        # A refusal that is NOT a replay: give the purchase key
                        # back so the player can retry after topping up, and
                        # let the row attempt stand only if the row can pay.
                        if not receipt.get("_replayed"):
                            release_purchase(ledger_key)
                            raise HTTPException(
                                status_code=400,
                                detail=str(receipt.get("error")
                                           or "Insufficient Spirit Stones"))
                if not wallet_ok:
                    if row_stones < cost:
                        release_purchase(ledger_key)
                        raise HTTPException(
                            status_code=400,
                            detail="Insufficient Spirit Stones")
                    stones = player.get("spirit_stones")
                    if isinstance(stones, dict):
                        stones["low_grade"] = row_stones - cost
                    else:
                        player["spirit_stones"] = row_stones - cost
                upgrade_tier(bp, tier_enum)
                player["battle_pass"] = bp.as_dict()
                response = {"ok": True, "new_tier": bp.tier.value, "cost": cost,
                            "replayed": False}
                record_purchase_response(players_db, ledger_key, response)
            except Exception:
                # The database rolls the row back with the failed charge. A
                # process-local set has no rollback, so give the key back here
                # or a refused purchase can never be retried.
                release_purchase(ledger_key)
                raise
            return response

    @app.post("/battle-pass/{player_id}/weekly-bonus")
    async def claim_weekly_bonus_endpoint(player_id: str, week: int,
                                          identity: PlayerIdentity = Depends(require_player)):
        """The weekly bonus. Once per week, paid tracks only."""
        players_db = store_provider()
        # Existence first: transaction(player_id) raises KeyError on an
        # unknown id, which would answer 500 instead of 404. Same ordering
        # as the four sibling routes; the check is repeated inside the
        # transaction so a player deleted between the two is still a 404.
        _player_or_404(players_db, player_id)

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = get_or_create_bp(player, time.time())

            ok, reward = claim_weekly_bonus(bp, week)
            if not ok:
                raise HTTPException(status_code=400,
                                    detail=reward.get("error", "Claim failed"))

            # Only the STONES are credited here. claim_weekly_bonus already
            # added the XP and recomputed the level; adding it again is what
            # turned a 5000 bonus into 10000 with a stale level.
            #
            # The wallet leg is keyed under kind="weekly" so a level claim and a
            # weekly bonus on the same position are two payouts, not one replay
            # of the other. Deliberately NOT _pay_reward: this reward carries no
            # qi/items/cosmetic, and _pay_reward would write player["progress"]
            # on a route that has never touched it.
            bonus_stones = int(reward.get("spirit_stones", 0) or 0)
            if bonus_stones > 0:
                earn_wallet_stones(
                    _wallet(), player_id, bonus_stones,
                    wallet_claim_key(player_id, bp.season_id, bp.tier.value,
                                     week, kind="weekly"))
                grant_stones(player, bonus_stones)
            bp.level = min(get_level_for_xp(bp.xp), MAX_LEVEL)
            player["battle_pass"] = bp.as_dict()
            response = {"ok": True, "reward": reward, "week": week,
                        "new_level": bp.level}
        return response


def player_history(player: dict) -> List[dict]:
    """The player's banked seasons, newest first."""
    history = player.get("battle_pass_history")
    if not isinstance(history, list):
        return []
    return [row for row in history if isinstance(row, dict)]


def _log_claim(season_id: str, tier: str, level: int, reward: dict) -> None:
    """Best-effort structured event. Never raises, never gates the claim."""
    try:
        import wc_logging
        wc_logging.safe_log_event(
            "BATTLE_PASS_CLAIM", season_id=season_id, tier=tier, level=level,
            spirit_stones=int((reward or {}).get("spirit_stones", 0) or 0),
        )
    except Exception:
        pass