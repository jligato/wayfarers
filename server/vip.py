"""
Wayfarer's Cycle -- VIP / Privilege tier system.

Competitor finding this module answers
--------------------------------------
`docs/top-priorities.md` row 9 reads: "**VIP = Privileges NOT Multipliers**.
**Critical**: No cultivation speed multiplier -- only discounts, support, gifts,
early access". The reverse-engineering closeout on the competitor's own client
(``audit/.../mine-gacha-summon-monetization-pay-battle-pass-e.md``) found its
19-level ladder paid a bonus row of
``0 0 0 0 0 0 0 0 0 5 5 5 8 8 8 10 10 10 10`` -- a dead first 60% of the track
that granted nothing at all.

So the design rule for this file is: a VIP tier buys CONVENIENCE and
COSMETICS, never POWER.

The non-negotiable constraint
-----------------------------
No tier in this file may grant a base cultivation speed multiplier, a passive
qi tick boost, or a combat stat multiplier. Those three make the game's
economy collapse (a paying account out-earns a grinding one forever) and turn
a cosmetic convenience tier into pay-to-win. The permitted benefit categories
are exactly five, and they are enumerated in :data:`PERMITTED_BENEFIT_KEYS`:

* ``daily_claim``        -- a daily resource claim (spirit stones + items)
* ``bag_slots``          -- inventory bag expansion slots
* ``shop_discount_pct``  -- shop discount vouchers
* ``cosmetics``          -- cosmetic frames / aura tints
* ``extra_daily_attempts`` -- REMOVED. Dungeon and tower runs have no
  daily attempt cap at all, so this benefit could never be honoured; see
  "Why there is no extra_daily_attempts" below.

This is not a comment nobody reads. :func:`validate_tier_table` walks the table
and raises on a forbidden key, :func:`register_vip_routes` calls it before it
mounts a single route, and the test file asserts the table contains none of
:data:`FORBIDDEN_BENEFIT_KEYS`. Adding a power multiplier to this file breaks a
test rather than shipping.

Why there is no extra_daily_attempts
----------------------------------
This table used to grant ``extra_daily_attempts`` (0, 1, 2, 3 by tier) and
NOTHING ever read it. Paid players bought extra dungeon and tower runs and
received exactly the same number of runs as the free tier, which is the
"dead first 60% of the track" shape this module was written to avoid.

It was removed rather than wired because the counters it would have widened
do not exist. Measured 2026-10-05 against the live tree:

* ``server/dungeons.py`` has no daily counter and no refusal at all -- it
  raises zero ``HTTPException`` and holds no ``*_today`` state key. Dungeon
  runs are already unlimited.
* ``server/tower_run.py`` and ``server/tower_routes.py`` DO track
  ``runs_today``, but ``TOWER_RUN_DAILY_FREE_RUNS`` (``tower_config.py:183``)
  is a reward TAPER, not a cap. ``rewards_for_run(20, 4)`` returns
  ``repeat=True`` and a reduced stone payment; it never refuses. A player on
  the free tier runs as many times as a paying one, for less.

So the benefit had nothing to grant. Re-adding it requires first building a
real daily attempt cap and refusing past it -- otherwise it is a lie in the
status payload a client renders. ``validate_tier_table`` now raises on it,
because a key outside :data:`PERMITTED_BENEFIT_KEYS` is rejected, so the
removal cannot be undone by accident.
Data-driven, not an if/elif ladder
----------------------------------
:data:`VIP_TIERS` is a tuple of plain dicts. Adding a tier is one dict, not a
new branch. Each row's ``benefits`` are the INCREMENTAL grant for reaching that
tier; :func:`tier_benefits` folds rows 0..N so a higher tier's effective
benefits are the honest sum of the lower ones. That makes the ladder
monotonic by construction instead of by hand-copying five numbers and getting
one wrong.

Idempotency: pay the daily grant EXACTLY ONCE per calendar day
---------------------------------------------------------------
Same defect class as ``server/tribulation.py``'s aftermath claim and
``server/daily_streak.py``'s streak claim: a route that checks a flag outside
its transaction lets a double press pay twice. This module follows the proven
precedent rather than inventing a mechanism:

1. The route opens ``players_db.transaction(player_id)``. In PostgreSQL mode
   that takes ``pg_advisory_xact_lock(GLOBAL_LOCK_ID)`` and re-reads the row
   fresh under READ COMMITTED; in memory mode it takes the store's ``RLock``.
   Either way the check and the write cannot interleave.
2. The player is re-read INSIDE that scope, because ``players_db[player_id]``
   hands back a snapshot, not a live reference. Mutating the copy from outside
   the transaction and returning 200 is the exact bug the battle-pass claim
   route shipped.
3. The payment happens BEFORE the day key is written. The whole player row
   commits atomically, so a raise anywhere in the payout rolls the key back
   with it and a retry is still allowed.
4. The key is the integer UTC day index (``last_claim_utc_day``), not a
   timestamp and not "seconds since last claim". Only an integer day index
   makes "one payout per calendar day" literally true at the midnight edge.

A player on the FREE tier has an empty daily grant, and an empty grant must
NOT consume the day key (see the ``grant_is_empty`` branch). Otherwise buying
the tier an hour after a free-tier press would silently deny the day's
resources.

State lives on the player dict under ``vip``, so the existing player store
persists it with no schema migration -- the ``daily_streak`` convention.

Mounting
--------
This module imports nothing from ``main``, so there is no import cycle. The
parent app mounts it with two lines; see :func:`register_vip_routes`.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# NOTE: this module deliberately imports NO reward helper. The daily grant pays
# spirit stones and inventory items only. It never pays qi, because a standing
# or passive qi drip is precisely the "passive Qi tick boost" the design
# constraint forbids -- a paying account would out-earn a grinding one forever.

__all__ = [
    "VIP_TIERS",
    "PERMITTED_BENEFIT_KEYS",
    "FORBIDDEN_BENEFIT_KEYS",
    "STATE_KEY",
    "VipError",
    "utc_day",
    "default_state",
    "get_state",
    "tier_by_id",
    "tier_for_points",
    "tier_benefits",
    "benefits_for_state",
    "get_vip_status",
    "add_vip_points",
    "grant_vip_points_for_xp",
    "install_xp_earn_hook",
    "uninstall_xp_earn_hook",
    "VIP_POINTS_PER_XP",
    "STATE_KEY_POINTS_REMAINDER",
    "evaluate_claim",
    "claim_daily",
    "validate_tier_table",
    "register_vip_routes",
]

SECONDS_PER_DAY: int = 86400
"""One calendar day in seconds. Used only to turn a timestamp into a day index."""

STATE_KEY = "vip"
"""Player-dict key holding VIP state, so the player store persists it for free."""

STATE_KEY_LAST_CLAIM = "last_claim_utc_day"
"""The idempotency key: the integer UTC day index of the last paid daily grant."""


STATE_KEY_POINTS_REMAINDER = "points_remainder_xp"
"""Battle-pass XP earned but not yet worth a whole VIP point.

At :data:`VIP_POINTS_PER_XP` XP per point, a 3-XP sect contribution converts to
zero points. Dropping the remainder would make every small gameplay action grant
nothing, forever, silently. It lives on the player row beside the rest of the
VIP state, so the existing player store persists it with no schema migration.
"""

VIP_POINTS_PER_XP = 10
"""Battle-pass XP that buys one VIP point.

DERIVED FROM MEASURED EARN AMOUNTS, NOT GUESSED. Every gameplay earn already
funnels through ``battle_pass.grant_xp``, whose measured per-call amounts are 10
(monthly event daily), 50 (daily boss strike), 100 (sect contribution and the
monthly event quest) and 200 (tribulation, which is its own cap). At 10 XP per
point each of those converts to an exact whole number, so a normal action never
loses XP to rounding.

At this rate the VIP ladder lands on battle-pass levels a player actually
reaches, checked against ``battle_pass.get_level_for_xp``:
``wanderer`` (500 pts) is 5,000 XP = level 6, ``practitioner`` (2,000 pts) is
20,000 XP = level 11, ``immortal`` (8,000 pts) is 80,000 XP = level 20. The first
paid tier is a short climb, the top one a long grind, which is what the
spirit-stone prices (3,000 / 12,000 / 50,000) already imply.
"""

PERMITTED_BENEFIT_KEYS: Tuple[str, ...] = (
    "daily_claim",
    "bag_slots",
    "shop_discount_pct",
    "cosmetics",
)
"""The ONLY benefit categories a tier may declare. See the module docstring."""

FORBIDDEN_BENEFIT_KEYS: Tuple[str, ...] = (
    "cultivation_speed",
    "cultivation_speed_multiplier",
    "qi_tick",
    "qi_tick_multiplier",
    "passive_qi",
    "breakthrough_speed",
    "stat_multiplier",
    "atk_multiplier",
    "def_multiplier",
    "hp_multiplier",
    "mp_multiplier",
    "spd_multiplier",
    "damage_multiplier",
    "pvp_rating",
    "drop_rate",
    "loot_quality",
    "offline_rate",
)
"""Benefit keys that must never appear. A tier granting any of these is a
pay-to-win regression, so both :func:`validate_tier_table` and the test file
fail on them."""


class VipError(Exception):
    """Raised when a VIP action cannot be completed."""


def _now() -> float:
    """Single clock seam. Tests patch THIS, never ``time.time`` (see conftest)."""
    return time.time()


# --- The tier ladder ---------------------------------------------------------
# Every row is the INCREMENTAL grant for reaching that tier. `tier_benefits`
# folds rows 0..N. Costs are in spirit stones; `points_required` is lifetime
# VIP points, which gameplay code grants through `add_vip_points`.
#
# Item ids are real ids from shared/items/items.json and cosmetic ids are real
# ids from server/cosmetics.py COSMETIC_CATALOG -- both are asserted by the
# test file, so a typo here fails a test instead of paying out nothing.

VIP_TIERS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "traveler",
        "display_name": "Traveler",
        "points_required": 0,
        "cost_spirit_stones": 0,
        "benefits": {
            "daily_claim": {"spirit_stones": 0, "items": {}},
            "bag_slots": 0,
            "shop_discount_pct": 0,
            "cosmetics": (),
        },
    },
    {
        "id": "wanderer",
        "display_name": "Wanderer",
        "points_required": 500,
        "cost_spirit_stones": 3000,
        "benefits": {
            "daily_claim": {"spirit_stones": 150, "items": {"spirit_herb": 1}},
            "bag_slots": 10,
            "shop_discount_pct": 5,
            "cosmetics": ("aura_qi_flow",),
        },
    },
    {
        "id": "practitioner",
        "display_name": "Practitioner",
        "points_required": 2000,
        "cost_spirit_stones": 12000,
        "benefits": {
            "daily_claim": {"spirit_stones": 400, "items": {"spirit_herb": 2, "beast_bone": 1}},
            "bag_slots": 25,
            "shop_discount_pct": 10,
            "cosmetics": ("aura_qi_river", "banner_sect_elite"),
        },
    },
    {
        "id": "immortal",
        "display_name": "Immortal",
        "points_required": 8000,
        "cost_spirit_stones": 50000,
        "benefits": {
            "daily_claim": {
                "spirit_stones": 1200,
                "items": {"spirit_herb": 4, "beast_bone": 2, "insight_scroll": 1},
            },
            "bag_slots": 50,
            "shop_discount_pct": 15,
            "cosmetics": ("aura_qi_ocean", "title_eternal_ascendant"),
        },
    },
)

TIER_IDS: Tuple[str, ...] = tuple(t["id"] for t in VIP_TIERS)
"""Tier ids in ascending order, so index order IS rank order."""


# --- Table integrity ---------------------------------------------------------

def validate_tier_table() -> None:
    """Raise ``VipError`` if the tier table is malformed or grants power.

    Called at import-time-safe points only: :func:`register_vip_routes` runs it
    before mounting, and the test file calls it directly. A forbidden benefit
    key here is a pay-to-win regression, so it must be a hard failure and not
    a warning nobody reads.
    """
    if not VIP_TIERS:
        raise VipError("VIP_TIERS is empty")

    if VIP_TIERS[0]["points_required"] != 0:
        raise VipError("the first tier must be free (points_required == 0)")

    seen_ids = set()
    previous_points = -1
    for index, tier in enumerate(VIP_TIERS):
        tid = tier.get("id")
        if not isinstance(tid, str) or not tid:
            raise VipError(f"tier {index} has no id")
        if tid in seen_ids:
            raise VipError(f"duplicate tier id {tid!r}")
        seen_ids.add(tid)

        points = tier.get("points_required")
        if not isinstance(points, int) or isinstance(points, bool) or points < 0:
            raise VipError(f"tier {tid!r} points_required must be a non-negative int")
        if points <= previous_points:
            raise VipError(f"tier {tid!r} points_required must strictly increase")
        previous_points = points

        cost = tier.get("cost_spirit_stones")
        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
            raise VipError(f"tier {tid!r} cost_spirit_stones must be a non-negative int")
        if index == 0 and cost != 0:
            raise VipError("the free tier must cost 0 spirit stones")

        benefits = tier.get("benefits")
        if not isinstance(benefits, dict):
            raise VipError(f"tier {tid!r} benefits must be a dict")
        for key in benefits:
            if key in FORBIDDEN_BENEFIT_KEYS:
                raise VipError(
                    f"tier {tid!r} declares forbidden benefit {key!r}: "
                    "VIP must grant privileges, never power"
                )
            if key not in PERMITTED_BENEFIT_KEYS:
                raise VipError(
                    f"tier {tid!r} declares unknown benefit {key!r}; "
                    f"permitted keys are {list(PERMITTED_BENEFIT_KEYS)}"
                )

        grant = benefits.get("daily_claim", {})
        if not isinstance(grant, dict):
            raise VipError(f"tier {tid!r} daily_claim must be a dict")
        stones = grant.get("spirit_stones", 0)
        if not isinstance(stones, int) or isinstance(stones, bool) or stones < 0:
            raise VipError(f"tier {tid!r} daily_claim spirit_stones must be >= 0 int")
        items = grant.get("items", {})
        if not isinstance(items, dict):
            raise VipError(f"tier {tid!r} daily_claim items must be a dict")
        for item_id, qty in items.items():
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
                raise VipError(f"tier {tid!r} item {item_id!r} quantity must be >= 0 int")

        for numeric in ("bag_slots", "shop_discount_pct"):
            value = benefits.get(numeric, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise VipError(f"tier {tid!r} {numeric} must be a non-negative int")
        if not isinstance(benefits.get("cosmetics", ()), (tuple, list)):
            raise VipError(f"tier {tid!r} cosmetics must be a tuple or list")


# --- Pure clock / day helpers ------------------------------------------------

def utc_day(now: Optional[float] = None) -> int:
    """The integer UTC day index for a timestamp.

    Day 0 is the epoch. An integer index (rather than a stored timestamp) is
    what makes the same-day idempotency key exact; see the module docstring.
    """
    if now is None:
        now = _now()
    return int(now // SECONDS_PER_DAY)


# --- Pure tier lookup --------------------------------------------------------

def tier_by_id(tier_id: str) -> Optional[Dict[str, Any]]:
    """The tier row with this id, or ``None``. Pure."""
    for tier in VIP_TIERS:
        if tier["id"] == tier_id:
            return tier
    return None


def tier_index_for_points(points: int) -> int:
    """Index into :data:`VIP_TIERS` for a lifetime point total. Pure.

    Never returns a negative index: a corrupt or negative point value lands on
    the free tier rather than indexing backwards off the table.
    """
    total = _read_stored_int(points, 0)
    index = 0
    for i, tier in enumerate(VIP_TIERS):
        if total >= int(tier["points_required"]):
            index = i
        else:
            break
    return index


def tier_for_points(points: int) -> Dict[str, Any]:
    """The tier row a point total earns. Pure."""
    return VIP_TIERS[tier_index_for_points(points)]


def tier_benefits(tier_id: str) -> Dict[str, Any]:
    """Folded benefits for a tier: the sum of every row up to and including it.

    Pure. ``tier_benefits("immortal")["bag_slots"] == 85`` is 0+10+25+50, so
    the ladder is monotonic without anyone hand-copying a total.
    """
    index = None
    for i, tier in enumerate(VIP_TIERS):
        if tier["id"] == tier_id:
            index = i
            break
    if index is None:
        raise VipError(f"unknown tier id {tier_id!r}")

    folded: Dict[str, Any] = {
        "daily_claim": {"spirit_stones": 0, "items": {}},
        "bag_slots": 0,
        "shop_discount_pct": 0,
        "cosmetics": [],
    }
    for tier in VIP_TIERS[: index + 1]:
        benefits = tier["benefits"]
        claim = benefits.get("daily_claim", {})
        folded["daily_claim"]["spirit_stones"] += int(claim.get("spirit_stones", 0))
        for item_id, qty in (claim.get("items", {}) or {}).items():
            folded["daily_claim"]["items"][item_id] = (
                folded["daily_claim"]["items"].get(item_id, 0) + int(qty)
            )
        for numeric in ("bag_slots", "shop_discount_pct"):
            folded[numeric] += int(benefits.get(numeric, 0))
        for cosmetic_id in benefits.get("cosmetics", ()):
            if cosmetic_id not in folded["cosmetics"]:
                folded["cosmetics"].append(cosmetic_id)
    return folded


# --- Player state ------------------------------------------------------------

def default_state() -> Dict[str, Any]:
    """A fresh, unclaimed VIP state for a player with no points."""
    return {
        "points": 0,
        "tier_id": VIP_TIERS[0]["id"],
        STATE_KEY_LAST_CLAIM: None,
        "total_claims": 0,
        "total_spirit_stones_claimed": 0,
        STATE_KEY_POINTS_REMAINDER: 0,
    }


def get_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's VIP state, creating it if absent. Tolerant of legacy saves.

    A pre-upgrade player has no ``vip`` dict at all and a half-migrated one may
    be missing keys, so any missing field is filled from
    :func:`default_state` rather than raising.
    """
    if not isinstance(player, dict):
        raise VipError("player must be a dict")
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        player[STATE_KEY] = raw
    for key, value in default_state().items():
        if key not in raw:
            raw[key] = value
    # The tier is derived from points, so a stale or hand-edited value is
    # corrected here rather than trusted.
    raw["tier_id"] = tier_for_points(raw.get("points", 0))["id"]
    return raw


def benefits_for_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Folded benefits for a state dict. Pure. Raises on an unknown tier id."""
    return tier_benefits(state.get("tier_id") or VIP_TIERS[0]["id"])



def _read_stored_int(value: Any, default: int = 0) -> int:
    """Read a stored number, answering ``default`` for a value that is not one.

    WHY THIS IS NOT ``stored_numbers.stored_int``
    --------------------------------------------
    ``stored_int`` coerces through ``float()`` and answers the default for
    anything non-finite. That is right for a bounded float field, and wrong
    here, because two behaviours are DELIBERATELY pinned by
    ``test_vip_contract.py`` and this helper keeps both:

    * a non-finite total (``inf`` / ``nan``) must RAISE, not read as zero --
      a silent zero would let a corrupt save resolve to a paid tier, and
      ``test_a_corrupt_stored_point_total_fails_the_claim_without_burning_the_day``
      pins the resulting 500 as the correct answer, day key unburned;
    * an unbounded int (``10 ** 400``) is LEGAL and must land on the top tier,
      which ``float()`` cannot represent at all.

    So numbers keep ``int()``'s own behaviour, and only a value that is not a
    number at all falls back to ``default``. That closes the real gap: a row
    holding ``"abc"`` / ``[]`` / ``{}`` used to raise out of the status read
    and 500 both the status panel and the claim, with no test pinning it.
    """
    # bool is an int subclass; int(True) == 1 keeps the old reading.
    if isinstance(value, (bool, int, float)):
        return int(value)  # OverflowError / ValueError propagate on purpose.
    # A numeric string has always parsed ("7" -> 7); keep that, and treat an
    # unparseable one as junk rather than a crash.
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return default

def get_vip_status(player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """A client-friendly snapshot. PURE -- mutates nothing on the player.

    Reports the current tier, progress toward the next one, the folded
    benefits, and whether today's daily grant is still claimable. The last
    field is what lets a client grey the button out without reimplementing the
    same-day rule.
    """
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = default_state()
    state = dict(raw)
    state["points"] = _read_stored_int(state.get("points", 0), 0)
    state["tier_id"] = tier_for_points(state["points"])["id"]

    today = utc_day(now)
    last = state.get(STATE_KEY_LAST_CLAIM)
    claimed_today = last is not None and _read_stored_int(last, 0) >= today

    tier = tier_by_id(state["tier_id"]) or VIP_TIERS[0]
    next_index = tier_index_for_points(state["points"]) + 1
    next_tier = VIP_TIERS[next_index] if next_index < len(VIP_TIERS) else None
    if next_tier is None:
        points_to_next = 0
        progress_pct = 100.0
    else:
        points_to_next = int(next_tier["points_required"]) - state["points"]
        span = int(next_tier["points_required"]) - int(tier["points_required"])
        progress_pct = 100.0 if span <= 0 else round(
            100.0 * (state["points"] - int(tier["points_required"])) / span, 2
        )

    benefits = tier_benefits(state["tier_id"])
    return {
        "tier_id": tier["id"],
        "display_name": tier["display_name"],
        "points": state["points"],
        "points_to_next": max(0, points_to_next),
        "progress_pct": max(0.0, min(100.0, progress_pct)),
        "next_tier_id": None if next_tier is None else next_tier["id"],
        "next_tier_cost_spirit_stones": (
            None if next_tier is None else int(next_tier["cost_spirit_stones"])
        ),
        "is_max_tier": next_tier is None,
        "benefits": benefits,
        "claimed_today": bool(claimed_today),
        STATE_KEY_LAST_CLAIM: last,
        "server_utc_day": today,
        "total_claims": int(state.get("total_claims", 0) or 0),
        "total_spirit_stones_claimed": int(
            state.get("total_spirit_stones_claimed", 0) or 0
        ),
        "tiers": [
            {
                "id": t["id"],
                "display_name": t["display_name"],
                "points_required": int(t["points_required"]),
                "cost_spirit_stones": int(t["cost_spirit_stones"]),
            }
            for t in VIP_TIERS
        ],
    }


def add_vip_points(player: Dict[str, Any], amount: int, source: str = "gameplay") -> Dict[str, Any]:
    """Add lifetime VIP points to a player and re-derive their tier.

    This is a MODULE FUNCTION, not a route. A POST endpoint that grants points
    to whoever asks is a self-serve cheat vector, so the only caller is trusted
    server-side gameplay code. Returns a dict describing the change.
    """
    if not isinstance(player, dict):
        raise VipError("player must be a dict")
    delta = int(amount or 0)
    state = get_state(player)
    previous_tier = state["tier_id"]
    state["points"] = max(0, _read_stored_int(state.get("points", 0), 0) + delta)
    state["tier_id"] = tier_for_points(state["points"])["id"]
    return {
        "ok": True,
        "source": source,
        "points_gained": delta,
        "total_points": state["points"],
        "previous_tier_id": previous_tier,
        "tier_id": state["tier_id"],
        "tier_changed": state["tier_id"] != previous_tier,
    }


def grant_vip_points_for_xp(player: Dict[str, Any], xp: int,
                            source: str = "gameplay") -> Dict[str, Any]:
    """Turn battle-pass XP into VIP points. The gameplay earn bridge.

    WHY THIS EXISTS
    ---------------
    ``add_vip_points`` used to have zero non-test callers, so every tier above the
    free one was unreachable: ``points`` stayed 0 forever and the whole ladder was
    dead content. Gameplay code still had no reason to call it directly, because
    the thing a player actually earns on a boss kill is XP.

    So the earn happens where the XP already happens. :func:`install_xp_earn_hook`
    wraps ``battle_pass.grant_xp`` -- the single funnel every earn path already
    uses (daily boss, sect contribution, tribulation, monthly event, offline
    progress) -- and this function does the conversion. That keeps one earn path
    instead of adding a second, parallel one that only some callers remember.

    THE REMAINDER IS CARRIED, NOT DROPPED
    -------------------------------------
    ``xp`` is usually not a multiple of :data:`VIP_POINTS_PER_XP`; a 3-XP
    contribution is 0 points. Discarding the leftover would make every small earn
    grant nothing at all, silently and forever. The leftover is stored under
    :data:`STATE_KEY_POINTS_REMAINDER` and spent by the next grant.

    THE MAGNITUDE COMES FROM XP THE SERVER GRANTED
    ----------------------------------------------
    ``xp`` is what ``battle_pass.grant_xp`` actually awarded, not what a caller
    asked for. ``grant_xp`` clamps a single grant to ``MAX_XP_PER_GRANT``, so
    reading the requested amount would pay VIP points for XP that never landed.

    No VIP route can reach here: it takes no request, and the HTTP surface is
    still exactly GET status + POST claim-daily.
    """
    if not isinstance(player, dict):
        raise VipError("player must be a dict")

    earned = max(0, int(xp or 0))
    state = get_state(player)
    carry = max(0, int(state.get(STATE_KEY_POINTS_REMAINDER, 0) or 0))
    pooled = carry + earned
    whole, leftover = divmod(pooled, VIP_POINTS_PER_XP)

    # Written even when ``whole`` is 0, so the carry survives to the next grant.
    state[STATE_KEY_POINTS_REMAINDER] = leftover

    if whole <= 0:
        return {
            "ok": True,
            "source": source,
            "xp_earned": earned,
            "points_gained": 0,
            "total_points": state["points"],
            "carry_xp": leftover,
            "tier_id": state["tier_id"],
            "tier_changed": False,
        }

    before = state["tier_id"]
    result = add_vip_points(player, whole, source)
    result["xp_earned"] = earned
    result["carry_xp"] = leftover
    result["previous_tier_id"] = before
    result["tier_changed"] = state["tier_id"] != before
    return result


# --- Daily grant: evaluate (pure) then apply (mutating) ----------------------

def _peek_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's effective VIP state, WITHOUT writing anything back.

    The read-only twin of :func:`get_state`: same derivation, same
    defaults, but the caller's dict is never touched. ``evaluate_claim``
    is documented as a pure preview, so it must not create a ``vip``
    block or rewrite a hand-edited ``tier_id`` on the stored row -- a
    preview that silently saves is a write wearing a read's clothes.
    """
    raw = player.get(STATE_KEY) if isinstance(player, dict) else None
    state = dict(raw) if isinstance(raw, dict) else {}
    for key, value in default_state().items():
        if key not in state:
            state[key] = value
    state["points"] = _read_stored_int(state.get("points", 0), 0)
    state["tier_id"] = tier_for_points(state["points"])["id"]
    return state


def _grant_is_empty(grant: Dict[str, Any]) -> bool:
    """True when a folded daily grant would pay nothing at all.

    A free-tier player pressing the claim button has earned nothing yet. The
    day key must NOT be consumed for a zero payout, or buying a tier later the
    same day would silently deny that day's resources.
    """
    if int(grant.get("spirit_stones", 0) or 0) > 0:
        return False
    return not any(int(q or 0) > 0 for q in (grant.get("items", {}) or {}).values())


def evaluate_claim(player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Pure: what a claim right now WOULD do, mutating nothing.

    Returns ``ok``, ``already_claimed``, ``empty_grant``, ``tier_id``,
    ``reward`` and ``utc_day``. A player who polls this cannot farm: the test
    file asserts the player dict is byte-identical afterwards.
    """
    state = _peek_state(player)
    today = utc_day(now)
    last = state.get(STATE_KEY_LAST_CLAIM)
    tier_id = state.get("tier_id") or VIP_TIERS[0]["id"]
    grant = tier_benefits(tier_id)["daily_claim"]

    if last is not None and _read_stored_int(last, 0) >= today:
        # THE idempotency guard. Same UTC day, or a clock that jumped backwards.
        return {
            "ok": True,
            "already_claimed": True,
            "empty_grant": False,
            "tier_id": tier_id,
            "reward": {"spirit_stones": 0, "items": {}},
            "reason": "already_claimed_today",
            "utc_day": today,
        }

    if _grant_is_empty(grant):
        # Not an error and NOT a consumed claim: the player simply has nothing
        # to collect right now, and the day key stays free for after a purchase.
        return {
            "ok": True,
            "already_claimed": False,
            "empty_grant": True,
            "tier_id": tier_id,
            "reward": {"spirit_stones": 0, "items": {}},
            "reason": "tier_grants_nothing_daily",
            "utc_day": today,
        }

    return {
        "ok": True,
        "already_claimed": False,
        "empty_grant": False,
        "tier_id": tier_id,
        "reward": {
            "spirit_stones": int(grant.get("spirit_stones", 0) or 0),
            "items": dict(grant.get("items", {}) or {}),
        },
        "reason": "claimed",
        "utc_day": today,
    }


def _apply_daily_claim(player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """The mutating half of a claim. MUST be called inside a store transaction.

    Splits payment from the day-key write on purpose. The whole player row
    commits atomically, so an exception during payment rolls the key back with
    it; paying first means a transient failure can never leave a player with a
    "already claimed" mark and no resources.
    """
    # This is the mutating half, so it owns normalising the row: the
    # ``vip`` block is created HERE, before either early return, rather
    # than as a side effect of the pure preview.
    state = get_state(player)
    outcome = evaluate_claim(player, now=now)
    if outcome["already_claimed"] or outcome["empty_grant"]:
        # Mutate nothing at all -- not even the clock-dependent fields.
        return outcome

    reward = outcome["reward"]
    stones = int(reward["spirit_stones"])

    if stones > 0:
        _credit_spirit_stones(player, stones)
    for item_id, qty in reward["items"].items():
        inventory = player.setdefault("inventory", {})
        inventory[item_id] = int(inventory.get(item_id, 0) or 0) + int(qty)

    # Written LAST. See the docstring: payment first, key second.
    state[STATE_KEY_LAST_CLAIM] = outcome["utc_day"]
    state["tier_id"] = outcome["tier_id"]
    state["total_claims"] = int(state.get("total_claims", 0) or 0) + 1
    state["total_spirit_stones_claimed"] = (
        int(state.get("total_spirit_stones_claimed", 0) or 0) + stones
    )
    return outcome


def _credit_spirit_stones(player: Dict[str, Any], amount: int) -> None:
    """Add spirit stones to either shape the player dict uses.

    New players store a tiered dict and old fixtures a plain int; both are
    accepted, same convention as ``battle_pass.grant_stones``.
    """
    if amount <= 0:
        return
    stones = player.get("spirit_stones")
    if isinstance(stones, dict):
        stones["low_grade"] = int(stones.get("low_grade", 0) or 0) + amount
    else:
        player["spirit_stones"] = int(stones or 0) + amount


def _resolve_store(players_db: Any) -> Any:
    """Accept a store, a zero-arg provider, or nothing (fall back to main).

    Lets the route mount a provider (the house convention) while still letting
    ``claim_daily(player_id)`` work with no second argument, exactly as the
    module docstring advertises.
    """
    if players_db is not None:
        return players_db() if callable(players_db) else players_db
    import main  # local import: avoids an import cycle at module load

    return main.players_db


def claim_daily(player_id: str, players_db: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Claim today's VIP daily grant. Pays EXACTLY ONCE per UTC day.

    ``players_db`` may be a store, a zero-arg callable returning one, or
    ``None`` to fall back to ``main.players_db``. Returns an outcome dict with
    ``already_claimed`` / ``empty_grant`` so the caller can render the button
    without reimplementing the rule.

    THE GUARDED REGION. The same-day check and the day-key write both live
    inside ``players_db.transaction(player_id)``, which takes a PostgreSQL
    advisory lock (or the in-memory RLock) and re-reads the row fresh. The
    player is re-read inside that scope on purpose -- the copy the store hands
    back may be a snapshot, and mutating it outside the transaction is the
    battle-pass claim bug: HTTP 200, nothing saved.
    """
    if not isinstance(player_id, str) or not player_id:
        raise VipError("player_id must be a non-empty string")

    store = _resolve_store(players_db)
    if player_id not in store:
        raise KeyError(player_id)

    with store.transaction(player_id):
        player = store[player_id]
        result = _apply_daily_claim(player, now=now)
        if not (result["already_claimed"] or result["empty_grant"]):
            # Explicit persist, matching daily_streak. Inside a transaction this
            # is a no-op on the staged row, but it is what makes the write
            # survive a store implementation that only commits on persist().
            store.persist(player_id, player)
    return result


# --- The gameplay earn hook --------------------------------------------------

#: Set on the wrapper so a second install cannot double-wrap. Double-wrapping is
#: not cosmetic: every XP grant would then pay VIP points twice.
_XP_EARN_HOOK_MARKER = "_wc_vip_xp_earn_hook"

#: The most recent bridge outcome, for tests and diagnostics. The HTTP reply
#: cannot carry it (see install_xp_earn_hook), so this is where a caller reads it.
LAST_XP_EARN: Dict[str, Any] = {}


def install_xp_earn_hook() -> bool:
    """Wrap ``battle_pass.grant_xp`` so gameplay XP also earns VIP points.

    Returns True if this call installed the hook, False if it was already there.

    WHY THE HOOK AND NOT A CALL IN EVERY EARN ROUTE
    ------------------------------------------------
    The gameplay earn paths are spread across half a dozen modules (daily boss,
    sect contribution, tribulation, monthly event, offline progression), and this
    task may only change ``vip.py``. Adding a VIP call to each of them is both out
    of scope and the more fragile fix: one missed earn path is a dead tier
    ladder again, and nothing would fail. ``battle_pass.grant_xp`` is the single
    funnel all of them already call, so hooking it once reaches every current and
    future earn path.

    All production callers reach it by module attribute (``battle_pass.grant_xp``)
    or by a function-local ``from battle_pass import grant_xp``, which resolves at
    call time -- both see the wrapper.

    THE RETURN DICT IS PASSED THROUGH UNTOUCHED
    --------------------------------------------
    The wrapper returns the original ``grant_xp`` result object, unmodified and
    with no key added. Six contract tests assert the exact key set of that reply
    (``{ok, source, xp_gained, new_level, leveled_up, total_xp}``), including
    receipts served inside a daily-boss payout, so adding a ``vip`` key would be a
    client-facing contract change. The VIP outcome goes to :data:`LAST_XP_EARN`
    and, for callers that want it, :func:`grant_vip_points_for_xp` directly.

    A VIP FAILURE NEVER TAKES THE XP WITH IT
    -----------------------------------------
    ``grant_xp`` has already written the player's XP by the time the bridge runs.
    If the bridge raised, the caller would see a 500 for an XP grant that really
    landed -- money-shaped data with an error attached. So the bridge is
    contained, and the containment is recorded in :data:`LAST_XP_EARN` rather than
    swallowed silently.
    """
    import battle_pass

    original = battle_pass.grant_xp
    if getattr(original, _XP_EARN_HOOK_MARKER, False):
        return False

    def grant_xp_and_vip(player: Dict[str, Any], amount: int,
                         source: str = "gameplay") -> Dict[str, Any]:
        result = original(player, amount, source)
        global LAST_XP_EARN
        # `xp_gained` is what the server actually awarded after grant_xp's own
        # clamp, which is the only honest basis for a VIP payout.
        landed = int(result.get("xp_gained", 0) or 0) if isinstance(result, dict) else 0
        try:
            LAST_XP_EARN = grant_vip_points_for_xp(player, landed, source)
            LAST_XP_EARN["ok"] = True
        except Exception as exc:  # noqa: BLE001 - contained on purpose, see above
            LAST_XP_EARN = {
                "ok": False,
                "source": source,
                "xp_earned": landed,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result

    setattr(grant_xp_and_vip, _XP_EARN_HOOK_MARKER, True)
    setattr(grant_xp_and_vip, "__wrapped__", original)
    setattr(grant_xp_and_vip, "__doc__", getattr(original, "__doc__", None))
    battle_pass.grant_xp = grant_xp_and_vip
    return True


def uninstall_xp_earn_hook() -> bool:
    """Put the original ``grant_xp`` back. For tests and for a clean teardown."""
    import battle_pass

    current = battle_pass.grant_xp
    original = getattr(current, "__wrapped__", None)
    if original is None or not getattr(current, _XP_EARN_HOOK_MARKER, False):
        return False
    battle_pass.grant_xp = original
    return True


# --- Server integration ------------------------------------------------------

def register_vip_routes(app, store_provider) -> None:
    """Register the VIP endpoints on the FastAPI app.

    ``store_provider`` is a zero-arg callable returning the player store, not a
    captured store, so tests that monkeypatch ``main.players_db`` get a fresh
    reference on every call -- the same convention as
    ``register_daily_streak_routes`` and ``register_tribulation_routes``.

    Nothing in this module imports this function, and importing this module has
    no side effects, so ``main.py`` needs exactly two lines::

        from vip import register_vip_routes
        register_vip_routes(app, _players_db_accessor)
    """
    from fastapi import HTTPException

    # Fail loudly BEFORE mounting if the ladder ever grows a power multiplier.
    validate_tier_table()

    # THE EARN PATH GOES LIVE HERE. Without this call the tier ladder above the
    # free one is unreachable: nothing else in the server earns VIP points, so
    # `points` stays 0 forever. It installs from the mount rather than at import
    # because this module's contract is that importing it has no side effects --
    # a test that imports vip to read the tier table must not wrap battle_pass.
    install_xp_earn_hook()

    @app.get("/vip/{player_id}/status")
    async def get_vip_status_route(player_id: str):
        players_db = store_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")
        return {"ok": True, "vip": get_vip_status(players_db[player_id], now=_now())}

    @app.post("/vip/{player_id}/claim-daily")
    async def claim_vip_daily_route(player_id: str):
        players_db = store_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")
        try:
            return claim_daily(player_id, players_db, now=_now())
        except KeyError:
            # Lost a race with a delete between the membership check and here.
            raise HTTPException(status_code=404, detail="Player not found")
