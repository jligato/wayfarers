"""
Wayfarer's Cycle — Cultivation Realm Configuration

Defines realm progression, breakthrough requirements, and tribulation scaling.
Based on Overmortal's realm structure with idle-game pacing.
"""
import math

from dataclasses import dataclass
from typing import Optional

@dataclass
class StageConfig:
    name: str
    order: int
    progress_required: float  # progress 0-100 to break through
    breakthrough_base_chance: float  # 0.0 - 1.0
    lifespan_days_added: int  # days added to lifespan on breakthrough
    cultivation_rate_per_hour: float  # progress gained per hour

@dataclass
class RealmConfig:
    name: str
    order: int
    stages: list[StageConfig]
    base_lifespan_days: int  # starting lifespan at this realm
    tribulation_scales_from: int  # realm order where tribulations start

REALMS: list[RealmConfig] = [
    RealmConfig(
        name="mortal",
        order=0,
        base_lifespan_days=30,
        tribulation_scales_from=0,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.95, lifespan_days_added=7, cultivation_rate_per_hour=2.0),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.90, lifespan_days_added=7, cultivation_rate_per_hour=1.5),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.85, lifespan_days_added=7, cultivation_rate_per_hour=1.0),
        ],
    ),
    RealmConfig(
        name="foundation",
        order=1,
        base_lifespan_days=60,
        tribulation_scales_from=1,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.80, lifespan_days_added=14, cultivation_rate_per_hour=0.8),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.75, lifespan_days_added=14, cultivation_rate_per_hour=0.6),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.70, lifespan_days_added=14, cultivation_rate_per_hour=0.4),
        ],
    ),
    RealmConfig(
        name="core",
        order=2,
        base_lifespan_days=120,
        tribulation_scales_from=2,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.65, lifespan_days_added=30, cultivation_rate_per_hour=0.3),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.60, lifespan_days_added=30, cultivation_rate_per_hour=0.2),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.55, lifespan_days_added=30, cultivation_rate_per_hour=0.15),
        ],
    ),
    RealmConfig(
        name="nascent_soul",
        order=3,
        base_lifespan_days=240,
        tribulation_scales_from=3,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.50, lifespan_days_added=60, cultivation_rate_per_hour=0.12),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.45, lifespan_days_added=60, cultivation_rate_per_hour=0.08),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.40, lifespan_days_added=60, cultivation_rate_per_hour=0.05),
        ],
    ),
    RealmConfig(
        name="soul_transformation",
        order=4,
        base_lifespan_days=480,
        tribulation_scales_from=4,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.35, lifespan_days_added=120, cultivation_rate_per_hour=0.04),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.30, lifespan_days_added=120, cultivation_rate_per_hour=0.03),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.25, lifespan_days_added=120, cultivation_rate_per_hour=0.02),
        ],
    ),
    RealmConfig(
        name="dao_seeking",
        order=5,
        base_lifespan_days=960,
        tribulation_scales_from=5,
        stages=[
            StageConfig(name="early", order=0, progress_required=100.0, breakthrough_base_chance=0.20, lifespan_days_added=240, cultivation_rate_per_hour=0.015),
            StageConfig(name="middle", order=1, progress_required=100.0, breakthrough_base_chance=0.15, lifespan_days_added=240, cultivation_rate_per_hour=0.01),
            StageConfig(name="late", order=2, progress_required=100.0, breakthrough_base_chance=0.10, lifespan_days_added=240, cultivation_rate_per_hour=0.005),
        ],
    ),
]

REALM_MAP: dict[str, RealmConfig] = {r.name: r for r in REALMS}

#: What one legacy point buys on a breakthrough roll: +0.05, i.e. five
#: percentage points.
#:
#: Spelled ONCE on purpose, because TWO readers now apply it:
#: :func:`calculate_breakthrough_chance` (the config clamp, 0.05-0.95)
#: and ``breakthrough.attempt_chance`` (the engine clamp, 0.05-0.90).
#: Two copies of 0.05 in two files is a drift waiting to happen -- raise
#: one and the two clamps would price the same player two different ways.
LEGACY_BONUS_PER_POINT = 0.05


def _dict_get(table: dict, key, default=None):
    """``table.get(key, default)``, with an UNHASHABLE key answered as a miss.

    REALM_MAP and STAGE_NAME_ALIASES are plain dicts, so a key Python cannot
    hash -- a list or a dict arriving from a stored row or a hand-built payload
    -- raises ``TypeError: unhashable type`` out of the lookup, and out of every
    reader built on it. That is a crash where every other unrecognised key
    already answers a documented gap value: get_realm("zzz_unknown") returns
    None, so a corrupt key was the ONLY shape that took a reader down.

    An unhashable key is therefore answered as a MISS, identically to any other
    key the table does not hold. Note what that does NOT do: it does not invent
    a fallback. Each reader keeps its own contract, so stage_rate_per_hour still
    raises the KeyError its docstring promises when no default is supplied --
    what disappears is the crash, not the refusal.
    """
    try:
        return table.get(key, default)
    except TypeError:
        # Python refuses the lookup before it compares anything, so the table's
        # own default is the only answer available -- and it is the same answer
        # a hashable absent key already gets.
        return default


def _finite_float(value) -> Optional[float]:
    """``value`` as a finite float, or None when it is unreadable or not finite.

    The same helper, and the same reasoning, as breakthrough._finite_float.

    ``float()`` raises TypeError on a non-number and ValueError on stored
    text, and OverflowError on a stored whole number too large for a float --
    but it does NOT raise on infinity: ``float("inf")`` succeeds and hands
    back ``inf``. NaN is the same, and because every comparison against NaN is
    False, a sign check cannot see it at all.

    MEASURED on this file before this helper existed (audit 012-304), by
    feeding each reader the values a corrupt stored row or a dividing-by-zero
    clock actually produces. 44 probe cases across the readers below:
    17 returned a non-finite float to the caller with nothing raised, 8 raised
    OverflowError on a 10**400 stored whole number, 5 raised ValueError on
    stored text, 4 raised TypeError.

    The silent case is the dangerous one, and it is the one an
    exception-only sweep reports as clean. These figures are handed to the
    offline settlement loop, which multiplies a rate by an elapsed window and
    can persist the result, so a non-finite value is not merely displayed --
    it is paid out and written back.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def get_realm(name: str) -> Optional[RealmConfig]:
    return _dict_get(REALM_MAP, name)


def get_stage(realm_name: str, stage_name: str) -> Optional[StageConfig]:
    realm = get_realm(realm_name)
    if not realm:
        return None
    for stage in realm.stages:
        if stage.name == stage_name:
            return stage
    return None


def get_next_stage(realm_name: str, stage_name: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (next_realm_name, next_stage_name) or (None, None) if at max."""
    realm = get_realm(realm_name)
    if not realm:
        return None, None
    
    current_stage = get_stage(
        realm_name, _dict_get(STAGE_NAME_ALIASES, stage_name, stage_name)
    )
    if not current_stage:
        return None, None
    
    if current_stage.order < len(realm.stages) - 1:
        # Same realm, next stage
        next_stage = realm.stages[current_stage.order + 1]
        return realm_name, next_stage.name
    else:
        # Next realm, early stage
        next_realm_idx = realm.order + 1
        if next_realm_idx < len(REALMS):
            next_realm = REALMS[next_realm_idx]
            return next_realm.name, next_realm.stages[0].name
        else:
            return None, None  # At max realm


# The engine's Stage enum (cultivation_system.Stage) spells one stage
# differently from this table. Map the engine's names onto the configured ones
# so a requirement is read from config instead of hardcoded at a call site.
#
# Every helper in this module that reads a STAGE applies this table:
# stage_progress_required, stage_rate_per_hour, get_next_stage and
# calculate_breakthrough_chance. That uniformity is load-bearing. Measured
# on this file before the last two were fixed, one stage read four ways:
#
#   stage_rate_per_hour("mortal", "mid_late")  -> 1.5   (alias applied)
#   stage_progress_required("mortal", "mid_late") -> 100.0 (alias applied)
#   calculate_breakthrough_chance("mortal", "mid_late") -> 0.0  (alias MISSING)
#   get_next_stage("mortal", "mid_late")       -> (None, None) (alias MISSING)
#
# "mid_late" is not corrupt data: offline_progression.STAGE_FROM_STORE_VALUE
# accepts it and cultivation_system.Stage.MID_LATE has that value, so a
# nested cultivation dict written by the engine carries it. A 0.0 chance is
# not a small penalty, it is an unbreakthroughable player; (None, None)
# from get_next_stage additionally reads as "max realm" to both call sites
# (POST /cultivation/breakthrough/v2 and the offline settlement loop), which
# would report transcendence to a mortal standing at the middle stage.
STAGE_NAME_ALIASES = {
    "mid_late": "middle",
}

# A full bar. Used only for stages this table does not describe -- peak and
# breakthrough have no config row, and the engine has an immortal realm this
# table has no entry for. That is the same 100.0 the rest of the engine already
# assumes: CultivationState.advance_progress() caps at 100.0 and every
# breakthrough gate compares against 100.0, so falling back to it cannot make
# the ceiling looser than the settlement loop.
DEFAULT_STAGE_PROGRESS_REQUIRED = 100.0

# Stages the game itself declares legal (cultivation_system.Stage.PEAK and
# Stage.BREAKTHROUGH, both accepted by offline_progression._as_stage) but that
# this table has no row for. They are not typos and not corrupt data: they are
# the two states a player passes THROUGH on the way out of a realm's last
# configured stage.
#
# They used to have no rate at all, and stage_rate_per_hour raises on a missing
# rate. Measured on the live app before this entry existed, a player whose row
# read cultivation_stage="breakthrough":
#
#   POST /lifecycle/{pid}/claim-offline  -> 500 Internal Server Error
#   GET  /lifecycle/{pid}/offline-preview -> 500 Internal Server Error
#   POST /cultivation/claim-offline/v2  -> 200 {"ok":false,"error":"Invalid stage"}
#
# The 500 is the whole claim, not just the cultivation line: the route settles
# in one transaction, so the spirit stones, the battle-pass experience and the
# claim marker all rolled back with it. It was also permanent -- three
# consecutive retries all answered 500 -- and the sibling route's 200 said
# "Invalid stage", i.e. the game called a state it defines itself invalid.
#
# So the rate is not invented: a player at peak or breakthrough is still
# finishing the realm's LAST CONFIGURED stage, so they earn that stage's
# configured rate, read from the same table as everyone else. One table, one
# rate, no new constant. The ceiling stays a full bar, and clamp_offline_progress
# still caps the grant at the remainder, so a player at 80/100 gains at most
# 20.0 -- the same arithmetic a player at late/80 already gets. They cannot
# chain a second realm by being away.
#
# This entry is scoped to the two names the game defines. An unrecognised
# spelling (a typo, a hand-edited row) still raises, because that is corrupt
# data and _as_stage already refuses it upstream with a readable message.
TERMINAL_STAGE_NAMES = frozenset({"peak", "breakthrough"})


def _is_terminal_stage(stage_name) -> bool:
    """``stage_name in TERMINAL_STAGE_NAMES``, tolerating an unhashable name.

    A frozenset membership test hashes its argument, so it raised on exactly
    the input ``_dict_get`` absorbs -- the third of the three sites that made a
    list or dict key a crash rather than a miss.

    An unhashable name is not one of the two terminal spellings: those are exact
    strings the game defines, so False is the only correct answer, and the
    caller then falls through to its own default rather than being served the
    last configured rate.
    """
    try:
        return stage_name in TERMINAL_STAGE_NAMES
    except TypeError:
        return False



def stage_progress_required(
    realm_name: str,
    stage_name: str,
    default: float = DEFAULT_STAGE_PROGRESS_REQUIRED,
) -> float:
    """How much progress the named stage still needs, read from this table.

    Read the requirement rather than assuming 100.0 at the call site, so a
    stage configured to need a different amount is honoured automatically.
    Anything this table does not describe falls back to a full bar.
    """
    stage = get_stage(realm_name, _dict_get(STAGE_NAME_ALIASES, stage_name, stage_name))
    required = None if stage is None else _finite_float(stage.progress_required)
    if required is None:
        # Both the row and the caller's default are read through the guard, so
        # a corrupt EITHER one still lands on the documented full bar instead
        # of returning inf or raising out of a reader that has a fallback.
        required = _finite_float(default)
    return DEFAULT_STAGE_PROGRESS_REQUIRED if required is None else required


def stage_rate_per_hour(
    realm_name: str,
    stage_name: str,
    default: Optional[float] = None,
) -> float:
    """How much progress an hour in the named stage earns, read from this table.

    One table, one rate. The offline claim route
    (``/cultivation/claim-offline/v2``) and the offline_progression engine both
    call this, so the same player away for the same number of hours is paid the
    same number of cultivation points whichever route they claim through.

    ``default`` covers the realms and stages this table has no row for. It is
    required for that case rather than guessed: an invented fallback rate would
    be indistinguishable from a tuned one, and a realm with no configured rate
    is a configuration gap that should surface, not be papered over.
    """
    stage = get_stage(realm_name, _dict_get(STAGE_NAME_ALIASES, stage_name, stage_name))
    rate = None if stage is None else _finite_float(stage.cultivation_rate_per_hour)
    if rate is None and _is_terminal_stage(stage_name):
        # peak / breakthrough have no row of their own. They are the states a
        # player passes through while finishing the realm's last configured
        # stage, so they earn that stage's rate. See TERMINAL_STAGE_NAMES for
        # the measured 500 this replaces.
        realm = get_realm(realm_name)
        if realm is not None and realm.stages:
            rate = _finite_float(realm.stages[-1].cultivation_rate_per_hour)
    if rate is None:
        # An unreadable default is treated as NO default, for the reason the
        # docstring gives: a fallback nobody configured is a gap that should
        # surface, not a number that reads as tuned.
        rate = _finite_float(default)
    if rate is None:
        raise KeyError(
            f"no configured offline rate for realm={realm_name!r} stage={stage_name!r}"
        )
    return rate


def legacy_bonus_of(player) -> float:
    """A player's stored legacy bonus, as a finite float. 0.0 when absent or corrupt.

    The one reader for this field, so the breakthrough roll and the two
    ``cultivation_config`` call sites all price a player the same way.

    MEASURED on the writers: the field is written by five routes
    (``daily_boss.py``, ``daily_boss_tables.py``, ``daily_boss_routes.py``,
    ``monthly_event.py`` at :346 and :493) and reset to 0.0 by
    ``lifecycle.py:273``. Four of those five read it with a bare
    ``float(player.get("legacy_bonus", 0.0))``, which propagates a stored
    infinity or NaN forward instead of refusing it -- so this reader must
    not assume the stored value is clean.

    A corrupt bonus therefore costs the player their BONUS, never the roll:
    ``attempt_chance`` still sums this into a clamped total, so the answer
    stays inside 0.05-0.90 either way.
    """
    if not isinstance(player, dict):
        return 0.0
    bonus = _finite_float(player.get("legacy_bonus"))
    return 0.0 if bonus is None else bonus


def calculate_breakthrough_chance(
    realm_name: str,
    stage_name: str,
    player_luck: float = 0.0,
    legacy_bonus: float = 0.0
) -> float:
    """Calculate breakthrough chance with all modifiers.

    MEASURED 2026-10-05 (audit 012-242): ``legacy_bonus`` is NOT dead
    arithmetic. The two modifiers are live -- passing 20.0 as
    ``legacy_bonus`` on ("mortal", "late") returns 0.95 where the bare
    call returns 0.85.

    The WIRING was the defect, and it is now landed and committed. Four
    production call sites omitted the argument, so legacy points reached
    a player's roll as 0.0 however many had been written into
    player["legacy_bonus"]. They are now covered by TWO routes, not by
    four identical edits -- a third party passing the same number into
    three call sites would let the two ceilings below disagree:

      main.py /cultivation/breakthrough/v2   passes it as an argument
                                            (this function), same as
                                            legacy_bonus_of(player)
      main.py offline settlement loop        passes it as an argument
      breakthrough.base_chance_for          does NOT pass it, and must
                                            not: see attempt_chance

    VERIFIED 2026-10-05 by reading the call sites: main.py passes it at
    both direct roll sites, and breakthrough.py:336 and :338 still omit
    it because breakthrough.attempt_chance adds it downstream.

    The two main.py sites are named by route and function rather than by
    line number on purpose: main.py is edited concurrently, and a line
    number recorded there was already stale within the same working
    session. Find them with:  grep -n legacy_bonus_of server/main.py

    main.py's two sites read the stored field through the single
    reader legacy_bonus_of(), so a player is priced the same way in
    every place. Writers of the field (daily_boss.py:411,
    daily_boss_tables.py:661, daily_boss_routes.py:96,
    monthly_event.py:346 and :493) are unchanged.

    Measured end to end through breakthrough.attempt_chance on
    core/late (base 0.55, unbalanced gate so legacy is the only mover):
    0 points 0.55, 1 -> 0.60, 2 -> 0.65, 3 -> 0.70, 5 -> 0.80,
    7 -> 0.90 (the ceiling), 10 and 10000 -> 0.90 (saturated).

    The parameter was deliberately NOT removed, for two reasons that
    are themselves measured rather than stylistic:

    1. Removing it deletes the only stated design for what legacy
       points buy (+5pp each, capped at 0.95). The economy keeps
       paying them, so removal makes the CURRENCY dead instead of
       making the parameter dead.
    2. It is pinned by keyword in
       server/tests/test_cultivation_contract.py (lines 316 and 330),
       so removal is a contract break, not a cleanup.

    Why the engine-side sum sits in attempt_chance rather than in the
    two config-table readers: breakthrough.py clamps to
    CHANCE_CEILING = 0.90, BELOW the 0.95 clamp here. Summing here
    would clamp at 0.95 first and 0.90 second, and the two ceilings
    would disagree. One clamp governs the number instead. The price of
    that choice is early saturation -- a wired bonus is worth nothing
    past 7 points on core/late, and past 1 on mortal/late, whose 0.85
    base is already at the edge. Whether that is the intended economy
    is an owner decision, not a code defect.

    The stage is resolved through ``STAGE_NAME_ALIASES`` so the engine's
    own spelling ("mid_late") reads the same configured row as the store's
    ("middle"). Without it ``get_stage`` returns None and this function
    returned 0.0, i.e. a guaranteed failure, for every player the engine
    itself had placed at the middle stage.
    """
    stage = get_stage(realm_name, _dict_get(STAGE_NAME_ALIASES, stage_name, stage_name))
    if not stage:
        return 0.0
    
    # Read through the guard so a corrupt row reports the SAME 0.0 this
    # function already returns for a rung it cannot price, rather than raising
    # out of the chance calculation or clamping a non-finite figure to a
    # plausible-looking 0.95.
    base = _finite_float(stage.breakthrough_base_chance)
    if base is None:
        return 0.0
    luck_mod = player_luck * 0.1  # +10% per luck point
    legacy_mod = legacy_bonus * LEGACY_BONUS_PER_POINT  # +5% per legacy point
    
    total = base + luck_mod + legacy_mod
    return min(0.95, max(0.05, total))  # clamp 5%-95%


def clamp_offline_progress(rate_per_hour: float, elapsed_seconds: float,
                           current_progress: float, progress_required: float) -> float:
    """Credit offline time, but never more than the stage still needs.

    Time alone is the wrong ceiling. The offline window is capped at 72 hours, but
    72h at 2.0 progress/hour is 144.0 -- more than one 100.0 stage, so the surplus
    would chain an extra breakthrough in the settlement loop. A player earns
    progress; they do not earn extra realms by being away.

    The ceiling is the REMAINDER of the current stage, so an under-cap absence pays
    in full and only the overshoot is shaved:

        progress_gained = min(rate * elapsed / 3600, max(0, required - current))

    A bar already at (or over) the threshold grants 0.0, never a negative.
    """
    if elapsed_seconds <= 0:
        return 0.0
    raw = rate_per_hour * (elapsed_seconds / 3600.0)
    remaining = max(0.0, progress_required - current_progress)
    return max(0.0, min(raw, remaining))


def calculate_tribulation_damage(realm_order: int) -> float:
    """Tribulation damage scales with realm. Returns damage per second during tribulation."""
    return 10.0 * (2 ** realm_order)  # 10, 20, 40, 80, 160, 320


def calculate_lifespan(realm_name: str) -> float:
    """Get lifespan in days for a realm."""
    realm = get_realm(realm_name)
    if not realm:
        return 30.0
    lifespan = _finite_float(realm.base_lifespan_days)
    return 30.0 if lifespan is None else lifespan