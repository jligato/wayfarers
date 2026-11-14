"""
Wayfarer's Cycle — Offline Progression Engine
Calculates cultivation XP, spirit stones, sect resources, and beast affinity gains while player is offline.
All calculations are server-authoritative using server time; device clock is only used for skew detection.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Union
from enum import Enum

# Import existing types
from cultivation_system import Realm, Stage, CultivationPath, SpiritRoot, Player, CultivationCalculator


class OfflineRewardType(Enum):
    CULTIVATION_PROGRESS = "cultivation_progress"
    SPIRIT_STONES = "spirit_stones"
    SECT_CONTRIBUTION = "sect_contribution"
    BEAST_AFFINITY = "beast_affinity"
    BATTLE_PASS_XP = "battle_pass_xp"


def _cultivation_source(player_dict: Dict) -> Dict:
    """Return the cultivation fields, in whichever shape the row actually uses.

    ``PlayerStore`` (server/player_store.py) persists cultivation as FLAT
    top-level columns -- ``progress``, ``cultivation_realm``,
    ``cultivation_stage``, ``path``, ``spirit_root``, ``spirit_stones`` -- and
    writes no nested ``cultivation`` dict at all (``cols.get("progress", 0.0)``
    at player_store.py:381, and ``progress`` at :29 in PLAYER_COLUMNS).
    ``apply_offline_rewards`` in this same module already reads those flat keys.

    This helper used to read ONLY ``player_dict["cultivation"]``, so on a real
    store row the nested lookup missed and every field fell to its default.
    Measured end-to-end before this fix: a player with a stored progress of
    60.0 and 90.0 was handed to the calculator as 0.0, the remainder ceiling
    became the whole bar, and ``POST /lifecycle/{pid}/claim-offline`` credited
    a flat 100.0 (a free stage promotion) instead of 40.0 and 10.0.

    A clamp cannot shave a remainder it was never given, so the fix is to read
    the flat columns first and keep the nested dict only as a legacy fallback
    for rows that genuinely carry one.
    """
    flat = {
        "realm": player_dict.get("cultivation_realm"),
        "stage": player_dict.get("cultivation_stage"),
        "path": player_dict.get("path"),
        "spirit_root": player_dict.get("spirit_root"),
        "progress": player_dict.get("progress"),
        "breakthrough_failures": player_dict.get("breakthrough_failures"),
        "tribulations_passed": player_dict.get("tribulations_passed"),
        "tribulations_failed": player_dict.get("tribulations_failed"),
    }
    nested = player_dict.get("cultivation")
    if not isinstance(nested, dict):
        return {k: v for k, v in flat.items() if v is not None}

    # Both shapes present: a flat column that was actually written wins, because
    # it is the value the store persisted. Nested-only keys still come through.
    merged = dict(nested)
    for key, value in flat.items():
        if value is not None:
            merged[key] = value
    return merged


def _dict_to_player(player_dict: Dict) -> Player:
    """Convert a player dict from store to Player object for calculations."""
    p = Player(
        id=player_dict.get("id", ""),
        name=player_dict.get("name", "")
        # cultivation will use default_factory=CultivationState
    )
    # Build cultivation state. Read via _cultivation_source so a real store row
    # (FLAT columns, no nested dict) is not seen as an empty bar.
    cult = _cultivation_source(player_dict)
    if isinstance(cult, dict):
        p.cultivation.realm = _as_enum(Realm, cult.get("realm", "mortal"), Realm.MORTAL)
        p.cultivation.stage = _as_stage(cult.get("stage", "early"))
        p.cultivation.path = _as_enum(CultivationPath, cult.get("path", "qi"), CultivationPath.QI)
        p.cultivation.spirit_root = _as_enum(SpiritRoot, cult.get("spirit_root", "none"), SpiritRoot.NONE)
        p.cultivation.progress = cult.get("progress", 0.0)
        p.cultivation.breakthrough_failures = cult.get("breakthrough_failures", 0)
        p.cultivation.tribulations_passed = cult.get("tribulations_passed", 0)
        p.cultivation.tribulations_failed = cult.get("tribulations_failed", 0)
        
        # Spirit stones. Same flat/nested split as the cultivation fields: the
        # store persists a top-level `spirit_stones` column, so a nested lookup
        # under `cult` would silently read {} and credit no stones.
        stones = player_dict.get("spirit_stones")
        if stones is None:
            stones = cult.get("spirit_stones", {})
        if isinstance(stones, dict):
            p.cultivation.copper = stones.get("copper", 0)
            p.cultivation.silver = stones.get("silver", 0)
            p.cultivation.gold = stones.get("gold", 0)
            p.cultivation.low_grade_stone = stones.get("low_grade", 0)
            p.cultivation.mid_grade_stone = stones.get("mid_grade", 0)
            p.cultivation.high_grade_stone = stones.get("high_grade", 0)
            p.cultivation.extreme_grade_stone = stones.get("extreme_grade", 0)
        else:
            # Legacy flat keys
            p.cultivation.copper = player_dict.get("copper", 0)
            p.cultivation.silver = player_dict.get("silver", 0)
            p.cultivation.gold = player_dict.get("gold", 0)
            p.cultivation.low_grade_stone = player_dict.get("low_grade_stone", 0)
            p.cultivation.mid_grade_stone = player_dict.get("mid_grade_stone", 0)
            p.cultivation.high_grade_stone = player_dict.get("high_grade_stone", 0)
            p.cultivation.extreme_grade_stone = player_dict.get("extreme_grade_stone", 0)
    
    # Other player fields
    p.level = player_dict.get("level", 1)
    p.exp = player_dict.get("exp", 0)
    p.atk = player_dict.get("atk", 100)
    p.def_ = player_dict.get("def_", 50)
    p.hp = player_dict.get("hp", 1000)
    p.mp = player_dict.get("mp", 500)
    p.spd = player_dict.get("spd", 100)
    p.crit_rate = player_dict.get("crit_rate", 0.05)
    p.crit_dmg = player_dict.get("crit_dmg", 1.5)
    p.pvp_rating = player_dict.get("pvp_rating", 1000)
    p.pvp_wins = player_dict.get("pvp_wins", 0)
    p.pvp_losses = player_dict.get("pvp_losses", 0)
    p.sect_id = player_dict.get("sect_id")
    p.sect_rank = player_dict.get("sect_rank")
    p.sect_contribution = player_dict.get("sect_contribution", 0)
    p.dao_companion_id = player_dict.get("dao_companion_id")
    p.last_login = player_dict.get("last_login", "")
    p.login_streak = player_dict.get("login_streak", 0)
    
    # Beasts
    p.beasts = player_dict.get("beasts", {})
    
    return p


def _finite_epoch(value):
    """Return ``value`` when it is a finite number, else None.

    The offline window is anchored on a wallet timestamp, and that column is
    free text in practice: ``server/wallet_store_pg.py:132`` builds its
    ``Wallet`` straight from the row with no NULL guard, and
    ``o11_schema_dir/schema.sql`` declares both timestamp columns as a bare
    ``DOUBLE`` -- nullable, with no default. So ``None`` is a shape this
    function is handed, not a hypothetical.

    Measured on this file before the guard, one missing anchor took down the
    payout route end to end:

        File "server/lifecycle.py", line 1061, in claim_offline
          result = calculate_full_offline_progress(player, device_now, ...)
        File "server/offline_progression.py", line 447
          elapsed_seconds = server_now - last_claim
        TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'

    i.e. HTTP 500 on ``POST /lifecycle/{pid}/claim-offline``. A NaN column is
    quieter and worse: it did not raise, and ``min(nan, 72.0)`` returns nan,
    so ``capped_hours`` and ``elapsed_hours`` both came back nan and the reply
    reported nan hours for a window that was never measured.

    ``bool`` is rejected deliberately -- it is an int subclass, and
    ``True`` would read as epoch second 1, i.e. a 56-year absence that the cap
    then prices as a full 72 hours.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


@dataclass
class OfflineReward:
    reward_type: OfflineRewardType
    amount: float
    currency: Optional[str] = None
    details: Dict = field(default_factory=dict)


@dataclass
class OfflineProgressResult:
    """Complete result of offline progression calculation."""
    elapsed_hours: float
    capped_hours: float
    rewards: List[OfflineReward]
    cultivation_progress_gained: float
    new_realm: Optional[str] = None
    new_stage: Optional[str] = None
    breakthrough_triggered: bool = False


# Realm promotion ladder, in the store's own vocabulary.
#
# `cultivation_realm` is a flat store column seeded as "mortal" by
# main.create_player (main.py:1160) and walked by the breakthrough route as
# ["mortal", "foundation", "core", "nascent_soul", "dao_seeking", "immortal"]
# (main.py:1520). Those values are exactly the `Realm` enum members
# (cultivation_system.py). Anything not in this list is not a realm this game
# has, so a promotion through an invented ladder silently does nothing.
REALM_LADDER = ["mortal", "foundation", "core", "nascent_soul", "dao_seeking", "immortal"]


# The store's stage vocabulary, and the engine enum members it maps onto.
#
# The store column `cultivation_stage` spells its middle stage "middle"
# (tower.py:34-40, combat_config.py:46, cultivation_config.REALMS all use
# early/middle/late). The engine's `Stage` enum calls that same stage
# "mid_late". Those are one stage, two spellings, and nothing translated
# between them.
#
# That is not cosmetic. `_as_enum(Stage, "middle", Stage.EARLY)` did not raise
# -- it returned the FALLBACK, so a player the store had recorded at the middle
# stage was handed to the rate table as being at the EARLY stage. Measured on
# one mortal player stored as "middle", a 10-hour absence was credited 20.0
# progress (the early rate, 2.0/h) instead of 15.0 (the middle rate, 1.5/h):
# 1.3333x, every time they claimed, at every realm, with no error and no log.
#
# So the mapping is explicit here, and an unrecognised spelling RAISES. A
# silent wrong default is what caused this overpayment; a loud error on an
# unknown stage name is the same fail-loud rule already applied to the rate
# table (stage_rate_per_hour raises when a realm has no configured rate rather
# than guessing one). peak and breakthrough are real enum members, so they
# still pass through without an alias.
#
# "mid_late" is kept as an accepted input: it is the engine's own spelling, and
# a nested `cultivation` dict written by an older engine build can carry it.
# Note this maps the STORE's name onto the ENGINE's member; the separate
# STAGE_NAME_ALIASES in cultivation_config maps the engine's name onto the
# CONFIG's, i.e. the opposite direction. Neither applies the other's rewrite,
# so the coercion is applied exactly once.
STAGE_FROM_STORE_VALUE = {
    "early": Stage.EARLY,
    "middle": Stage.MID_LATE,
    "late": Stage.LATE,
    "peak": Stage.PEAK,
    "breakthrough": Stage.BREAKTHROUGH,
    "mid_late": Stage.MID_LATE,
}


def _as_stage(value):
    """Coerce a stored stage string to a ``Stage``, or raise if unrecognised.

    Deliberately NOT the ``_as_enum`` shape. ``_as_enum``'s fallback is the
    right call for a realm (an unread realm earns mortal rates, the
    conservative direction -- see its docstring and
    test_offline_store_shape_sweep.py) but it is the wrong call for a stage,
    because a stage selects a rate and there is no conservative stage: the
    fallback ``early`` is the HIGHEST-rate stage in every realm, so every
    unknown spelling overpaid. Raising here costs a 500 on data the game cannot
    price; the fallback cost every such player real currency.
    """
    if isinstance(value, Stage):
        return value
    try:
        return STAGE_FROM_STORE_VALUE[value]
    except (KeyError, TypeError):
        raise ValueError(
            "unrecognised cultivation stage %r: the store spells a stage "
            "early/middle/late/peak/breakthrough, and the engine enum spells "
            "the middle one 'mid_late'. Add the store's spelling to "
            "STAGE_FROM_STORE_VALUE rather than letting it fall back to a "
            "rate." % (value,)
        ) from None


def _as_enum(enum_cls, value, fallback):
    """Coerce a stored string to an enum member, falling back on unknown values.

    The store column is free text: nothing constrains a writer to the `Realm`
    enum's values. ``loop_sim.DUNGEON_REALM`` (:106) writes "qi_refining" into
    ``cultivation_realm``, and a hand-edited or migrated row can carry anything.
    ``Realm(value)`` raised ``ValueError`` on those, which surfaced as a 500 on
    an offline CLAIM -- a read path failing on data the store itself accepted.

    An unknown realm is therefore read as `fallback` rather than raised, which
    is also the conservative direction: it credits the mortal rates, not a
    boosted one.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, KeyError, TypeError):
        return fallback

# Configuration constants
MAX_OFFLINE_HOURS = 72.0  # Hard cap on offline progression
MAX_CLOCK_SKEW_SECONDS = 300.0  # 5 minutes

# Spirit stone offline generation rates (per hour, by realm)
SPIRIT_STONE_OFFLINE_RATES = {
    Realm.MORTAL: {"copper": 50, "silver": 0, "gold": 0},
    Realm.FOUNDATION: {"copper": 30, "silver": 5, "gold": 0},
    Realm.CORE: {"copper": 20, "silver": 10, "gold": 1},
    Realm.NASCENT_SOUL: {"copper": 10, "silver": 15, "gold": 3},
    Realm.DAO_SEEKING: {"copper": 5, "silver": 20, "gold": 8},
    Realm.IMMORTAL: {"copper": 0, "silver": 25, "gold": 15},
}

# Spirit stone grade offline generation (converted from above)
SPIRIT_STONE_GRADE_RATES = {
    Realm.MORTAL: {"low_grade": 2, "mid_grade": 0, "high_grade": 0, "extreme_grade": 0},
    Realm.FOUNDATION: {"low_grade": 1, "mid_grade": 0.5, "high_grade": 0, "extreme_grade": 0},
    Realm.CORE: {"low_grade": 0.5, "mid_grade": 0.3, "high_grade": 0.1, "extreme_grade": 0},
    Realm.NASCENT_SOUL: {"low_grade": 0.2, "mid_grade": 0.2, "high_grade": 0.1, "extreme_grade": 0.02},
    Realm.DAO_SEEKING: {"low_grade": 0.1, "mid_grade": 0.15, "high_grade": 0.1, "extreme_grade": 0.05},
    Realm.IMMORTAL: {"low_grade": 0, "mid_grade": 0.1, "high_grade": 0.2, "extreme_grade": 0.1},
}

# Sect contribution offline rates (per hour)
SECT_CONTRIBUTION_OFFLINE_RATES = {
    Realm.MORTAL: 5,
    Realm.FOUNDATION: 10,
    Realm.CORE: 20,
    Realm.NASCENT_SOUL: 35,
    Realm.DAO_SEEKING: 50,
    Realm.IMMORTAL: 75,
}

# Beast affinity offline rates (per hour per beast)
BASE_BEAST_AFFINITY_PER_HOUR = 2.0

# Battle Pass XP offline rate
BATTLE_PASS_XP_PER_HOUR = 5


def calculate_offline_cultivation_progress(player: Player, hours: float) -> float:
    """
    Calculate cultivation progress from offline hours using existing CultivationCalculator.
    This reuses the established offline progress logic.
    """
    return CultivationCalculator.calculate_offline_progress(player, hours)


def calculate_offline_spirit_stones(player: Player, hours: float) -> Dict[str, float]:
    """Calculate spirit stones generated offline."""
    realm = player.cultivation.realm
    rates = SPIRIT_STONE_OFFLINE_RATES.get(realm, SPIRIT_STONE_OFFLINE_RATES[Realm.MORTAL])
    grade_rates = SPIRIT_STONE_GRADE_RATES.get(realm, SPIRIT_STONE_GRADE_RATES[Realm.MORTAL])

    # Apply spirit stone bonus (same as cultivation)
    stone_bonus = CultivationCalculator._stone_bonus(player)

    # Body cultivators generate slightly less currency
    path_mod = 0.9 if player.cultivation.path == CultivationPath.BODY else 1.0

    # Pure root bonus
    root_bonus = 1.05 if player.cultivation.spirit_root == SpiritRoot.PURE else 1.0

    modifier = stone_bonus * path_mod * root_bonus

    result = {}
    for currency, rate in rates.items():
        result[currency] = rate * hours * modifier

    for grade, rate in grade_rates.items():
        result[grade] = rate * hours * modifier

    return result


def calculate_offline_sect_contribution(player: Player, hours: float) -> float:
    """Calculate sect contribution generated offline (if in a sect)."""
    if not player.sect_id:
        return 0.0

    realm = player.cultivation.realm
    base_rate = SECT_CONTRIBUTION_OFFLINE_RATES.get(realm, SECT_CONTRIBUTION_OFFLINE_RATES[Realm.MORTAL])

    # Higher sect rank = better rates
    rank_multipliers = {
        "outer_disciple": 1.0,
        "inner_disciple": 1.5,
        "elder": 2.0,
        "protector": 2.5,
        "vice_patriarch": 3.0,
        "patriarch": 4.0,
    }
    rank_mult = rank_multipliers.get(player.sect_rank, 1.0)

    return base_rate * hours * rank_mult


def _iter_owned_beasts(roster) -> List[tuple]:
    """Return ``[(beast_id, beast_dict), ...]`` for a player roster.

    The store persists the roster as a LIST of beast dicts, not a mapping.
    ``PlayerStore._split`` (server/player_store.py:156) routes ``beasts``
    through ``extra_state`` and ``_row_to_dict`` merges it back with
    ``d.update(extra)`` (:186), so a loaded row carries a plain list. That is
    the shape every other reader uses: ``main.py:1750``, ``main.py:1875`` and
    ``main.py:1957`` all do ``player.get("beasts", [])`` and iterate it, and
    ``beast_config.grant_beast`` (:218-222) appends to a list.

    This used to call ``player.beasts.items()``, which raised
    ``AttributeError: 'list' object has no attribute 'items'`` for every player
    who owned a beast, so ``POST /lifecycle/{pid}/claim-offline`` returned a
    500 instead of paying out. A legacy mapping form is still accepted.
    """
    if isinstance(roster, dict):
        return [(bid, bdata) for bid, bdata in roster.items() if isinstance(bdata, dict)]
    if isinstance(roster, (list, tuple)):
        return [(b.get("beast_id"), b) for b in roster if isinstance(b, dict) and b.get("beast_id")]
    return []


def calculate_offline_beast_affinity(player: Player, hours: float) -> Dict[str, float]:
    """Calculate beast affinity gained offline for each owned beast."""
    if not hasattr(player, "beasts") or not player.beasts:
        return {}

    result = {}
    for beast_id, beast_data in _iter_owned_beasts(player.beasts):
        # Base affinity per hour
        affinity = BASE_BEAST_AFFINITY_PER_HOUR * hours

        # Realm bonus: higher realm players bond faster
        realm_order = [Realm.MORTAL, Realm.FOUNDATION, Realm.CORE,
                       Realm.NASCENT_SOUL, Realm.DAO_SEEKING, Realm.IMMORTAL]
        realm_bonus = 1.0 + (realm_order.index(player.cultivation.realm) * 0.1)

        # Spirit root match bonus
        beast_element = beast_data.get("element", "none")
        root_bonus = 1.0
        if player.cultivation.spirit_root.value != "none":
            # Simplified: matching elements give 20% bonus
            if _element_matches_root(beast_element, player.cultivation.spirit_root):
                root_bonus = 1.2

        result[beast_id] = affinity * realm_bonus * root_bonus

    return result


def _element_matches_root(beast_element: str, spirit_root: SpiritRoot) -> bool:
    """Check if beast element matches player's spirit root."""
    element_root_map = {
        "fire": SpiritRoot.FIRE,
        "water": SpiritRoot.WATER,
        "earth": SpiritRoot.EARTH,
        "wood": SpiritRoot.WOOD,
        "metal": SpiritRoot.METAL,
    }
    return element_root_map.get(beast_element) == spirit_root


def calculate_offline_battle_pass_xp(hours: float) -> float:
    """Calculate Battle Pass XP from offline time."""
    return BATTLE_PASS_XP_PER_HOUR * hours


def calculate_full_offline_progress(player: Union[Player, Dict], device_now: float, wallet_store=None) -> OfflineProgressResult:
    """
    Main entry point: calculate all offline progression for a player.
    Uses server time for all calculations; device_now only for clock skew check.
    Accepts either a Player object or a player dict from the store.
    wallet_store: optional WalletStore for accessing last_offline_claimed_at
    """
    # Convert dict to Player if needed
    if isinstance(player, dict):
        player = _dict_to_player(player)
    server_now = time.time()

    # Clock skew check
    skew = abs(device_now - server_now)
    if skew > MAX_CLOCK_SKEW_SECONDS:
        raise ValueError(f"Device clock skew exceeds {MAX_CLOCK_SKEW_SECONDS}s: {skew:.0f}s")

    # Get last offline claim time (fallback to last_online_at for new players)
    #
    # `last_claim = server_now` above is this module's own stated answer to
    # "we cannot tell when this player was last here": credit no time at all.
    # That default was only honoured when there was NO wallet. A wallet whose
    # two timestamp columns are unusable -- NULL, NaN, or free text -- used to
    # override it with `None` and take the payout route down with a TypeError,
    # or with NaN reported as the elapsed hours. Both are handled by the same
    # rule the no-wallet case already used: an anchor we cannot price is
    # `server_now`, which is elapsed 0 and therefore no payout.
    #
    # The truthiness test on the claimed stamp is NOT new. `a or b` returns
    # `b` when `a` is falsy, so 0.0 -- "never claimed" -- has always fallen
    # through to last_online_at. Preserved deliberately: a player with
    # last_online_at set to now must still credit zero hours, and treating
    # 0.0 as a real epoch-0 anchor would invent 56 years of absence and then
    # price the full 72-hour cap out of it.
    last_claim = server_now
    if wallet_store:
        wallet = wallet_store.get(getattr(player, 'id', ''))
        if wallet:
            if wallet.last_offline_claimed_at:
                last_claim = _finite_epoch(wallet.last_offline_claimed_at)
            else:
                last_claim = _finite_epoch(wallet.last_online_at)
    else:
        last_claim = _finite_epoch(
            getattr(player, "last_cultivation_time", None))
    if last_claim is None:
        last_claim = server_now

    elapsed_seconds = server_now - last_claim
    # `not math.isfinite` first: a NaN window fails every comparison, so
    # `elapsed_seconds < 0` is False for NaN and the clamp below would let it
    # through to a reply reporting nan hours.
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        elapsed_seconds = 0.0

    elapsed_hours = elapsed_seconds / 3600.0
    # The 72-hour cap. `capped_hours` is the ONLY hours figure any reward is
    # priced from, so this line is the whole bound on the credit; it is stated
    # once, here, and every caller multiplies from it.
    capped_hours = min(elapsed_hours, MAX_OFFLINE_HOURS)

    rewards = []

    # 1. Cultivation Progress
    cultivation_progress = calculate_offline_cultivation_progress(player, capped_hours)
    if cultivation_progress > 0:
        rewards.append(OfflineReward(
            reward_type=OfflineRewardType.CULTIVATION_PROGRESS,
            amount=cultivation_progress,
            details={"base_progress_per_hour": cultivation_progress / capped_hours if capped_hours > 0 else 0}
        ))

    # 2. Spirit Stones
    stone_rewards = calculate_offline_spirit_stones(player, capped_hours)
    for currency, amount in stone_rewards.items():
        if amount > 0:
            rewards.append(OfflineReward(
                reward_type=OfflineRewardType.SPIRIT_STONES,
                amount=amount,
                currency=currency,
                details={"rate_per_hour": amount / capped_hours if capped_hours > 0 else 0}
            ))

    # 3. Sect Contribution
    sect_contrib = calculate_offline_sect_contribution(player, capped_hours)
    if sect_contrib > 0:
        rewards.append(OfflineReward(
            reward_type=OfflineRewardType.SECT_CONTRIBUTION,
            amount=sect_contrib,
            details={"rate_per_hour": sect_contrib / capped_hours if capped_hours > 0 else 0}
        ))

    # 4. Beast Affinity
    beast_affinities = calculate_offline_beast_affinity(player, capped_hours)
    for beast_id, affinity in beast_affinities.items():
        if affinity > 0:
            rewards.append(OfflineReward(
                reward_type=OfflineRewardType.BEAST_AFFINITY,
                amount=affinity,
                details={"beast_id": beast_id}
            ))

    # 5. Battle Pass XP
    bp_xp = calculate_offline_battle_pass_xp(capped_hours)
    if bp_xp > 0:
        rewards.append(OfflineReward(
            reward_type=OfflineRewardType.BATTLE_PASS_XP,
            amount=bp_xp,
            details={"rate_per_hour": BATTLE_PASS_XP_PER_HOUR}
        ))

    # Check for breakthrough (progress would push to next stage)
    breakthrough_triggered = False
    new_realm = None
    new_stage = None
    if player.cultivation.progress + cultivation_progress >= 100.0:
        if player.cultivation.stage != Stage.BREAKTHROUGH:
            breakthrough_triggered = True
            # Simulate promotion to get new realm/stage
            from cultivation_system import CultivationState
            temp_state = CultivationState(
                realm=player.cultivation.realm,
                stage=player.cultivation.stage,
                path=player.cultivation.path,
                spirit_root=player.cultivation.spirit_root,
                progress=player.cultivation.progress
            )
            temp_state.advance_progress(cultivation_progress)
            if temp_state.stage == Stage.BREAKTHROUGH:
                # `advance_progress` only flips the STAGE to "breakthrough"; it
                # never touches `realm`. So `temp_state.realm.value` is the realm
                # the player is ALREADY in, and reporting it as `new_realm` told
                # the client a "mortal" player had been promoted to "mortal".
                # Resolve the promotion against the store's own ladder so the
                # response names the realm the row is actually moved to.
                _current = player.cultivation.realm.value
                if _current in REALM_LADDER and REALM_LADDER.index(_current) + 1 < len(REALM_LADDER):
                    new_realm = REALM_LADDER[REALM_LADDER.index(_current) + 1]
                new_stage = temp_state.stage.value

    return OfflineProgressResult(
        elapsed_hours=elapsed_hours,
        capped_hours=capped_hours,
        rewards=rewards,
        cultivation_progress_gained=cultivation_progress,
        new_realm=new_realm,
        new_stage=new_stage,
        breakthrough_triggered=breakthrough_triggered
    )


def apply_offline_rewards(player: Union[Player, Dict], result: OfflineProgressResult,
                          wallet_store=None, sect_store=None) -> Dict:
    """
    Apply calculated offline rewards to player state.
    Returns summary of applied changes.
    Handles both Player objects and player dicts from the store.
    """
    is_dict = isinstance(player, dict)

    def get_cult():
        return player.get("cultivation", {}) if is_dict else getattr(player, "cultivation", {})

    def get_beasts():
        return player.get("beasts", {}) if is_dict else getattr(player, "beasts", {})

    def _stone_tier(name: str) -> Optional[str]:
        """Map a stone/grade name onto the key the store actually persists.

        ``spirit_stones`` is ONE jsonb column holding a dict of tiers
        (``PlayerStore._upsert``, server/player_store.py:382, and
        ``main.create_player`` at :1165-1168 which seeds copper/silver/gold
        plus low/mid/high/extreme_grade). Spending code reads that dict:
        ``battle_pass.stone_balance`` and ``sect.stone_balance`` both do
        ``stones.get("low_grade", 0)`` when it is a dict.

        ``get_cult_attr``/``set_cult_attr`` ignored the tier dict and used the
        bare name, so a claim wrote NEW top-level keys ``player["copper"]`` and
        ``player["low_grade_stone"]``. Those are not store columns, so
        ``_split`` filed them under ``extra_state``, and no reader looks for
        them: the player's spendable balance did not move. Measured on a real
        store row with an 8h window: 600 copper + 24 low_grade were computed,
        reported in the response, and left the balance at exactly 500.
        """
        if name in ("copper", "silver", "gold"):
            return name
        return {"low_grade": "low_grade", "mid_grade": "mid_grade",
                "high_grade": "high_grade", "extreme_grade": "extreme_grade"}.get(name)

    def get_stone(name: str, default=0):
        """Read a spirit-stone tier the way every spender reads it."""
        tier = _stone_tier(name)
        if is_dict:
            if tier is None:
                return default
            stones = player.get("spirit_stones")
            if isinstance(stones, dict):
                return stones.get(tier, default)
            # Legacy rows carry a scalar balance; treat it as the F2P tier,
            # which is what battle_pass.stone_balance does for a scalar too.
            if tier == "low_grade":
                return int(stones or 0)
            return default
        attr = {"low_grade": "low_grade_stone", "mid_grade": "mid_grade_stone",
                "high_grade": "high_grade_stone", "extreme_grade": "extreme_grade_stone"}.get(name, name)
        return getattr(player.cultivation, attr, default)

    def set_stone(name: str, value) -> bool:
        """Write a spirit-stone tier into the dict the store persists.

        Returns whether the balance actually MOVED.

        A legacy row carrying a plain int balance stays a plain int, which is
        what ``battle_pass.grant_stones`` and ``sect._spend_stones`` both do
        for a scalar (``player["spirit_stones"] = int(stones or 0) + amount``).
        Promoting it to a dict here would move a balance into a different
        column shape than the rest of the economy reads.

        Two refusals exist and both are silent -- no error, no log, and the
        value handed in is simply dropped. That is exactly why the caller must
        act on this return value rather than assume the write happened:

        1. A currency name with no tier mapping. `_stone_tier` returns None and
           there is no column to write, because inventing one would create a
           balance no spender can ever read.
        2. A coin or upper-grade credit onto a LEGACY SCALAR balance, where
           only the free-to-play tier is spendable.

        This used to be annotated ``-> None``, so the caller credited its
        summary from the reward amount and a caller reading that summary was
        told about a payout that had never landed.
        """
        tier = _stone_tier(name)
        if not is_dict:
            attr = {"low_grade": "low_grade_stone", "mid_grade": "mid_grade_stone",
                    "high_grade": "high_grade_stone", "extreme_grade": "extreme_grade_stone"}.get(name, name)
            setattr(player.cultivation, attr, value)
            return True
        if tier is None:
            return False
        stones = player.get("spirit_stones")
        if stones is not None and not isinstance(stones, dict):
            # Legacy scalar balance: only the F2P tier is spendable, so a
            # copper/grade credit has nowhere to go. Keep the scalar and let
            # the low_grade tier absorb, exactly as the spender helpers read it.
            if tier == "low_grade":
                player["spirit_stones"] = value
                return True
            return False
        if not isinstance(stones, dict):
            stones = {}
            player["spirit_stones"] = stones
        stones[tier] = value
        return True

    def set_cult_attr(attr, value):
        if is_dict:
            # Player dicts use flat keys: 'progress', 'cultivation_realm', 'cultivation_stage', 'path'
            flat_key = "progress" if attr == "progress" else attr
            player[flat_key] = value
        else:
            setattr(player.cultivation, attr, value)

    def get_cult_attr(attr, default=0):
        if is_dict:
            flat_key = "progress" if attr == "progress" else attr
            return player.get(flat_key, default)
        return getattr(player.cultivation, attr, default)

    def get_player_attr(attr, default=None):
        if is_dict:
            return player.get(attr, default)
        return getattr(player, attr, default)

    def set_player_attr(attr, value):
        if is_dict:
            player[attr] = value
        else:
            setattr(player, attr, value)

    def get_bp_xp() -> float:
        """Read the battle-pass XP the row ACTUALLY holds, not the grant asked for.

        ``grant_xp`` clamps a single grant to ``MAX_XP_PER_GRANT`` and returns
        the clamped figure in ``xp_gained``. Crediting the summary from the
        REQUEST meant a caller reading ``applied["battle_pass_xp"]`` was told
        about XP that never landed: measured at 1500 requested against a
        ``MAX_XP_PER_GRANT`` of 1000, the reply said 1500 and the row held 1000.
        """
        if not is_dict:
            return 0.0
        stored = player.get("battle_pass")
        if not isinstance(stored, dict):
            return 0.0
        try:
            return float(stored.get("xp", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    # `rejected_rewards` records every line the row REFUSED to take. A
    # silently dropped credit is the worst outcome available: no error, no
    # log, and a summary that claims a payout which never landed. Naming
    # the refusal keeps the two honest -- `applied` says only what moved,
    # `rejected_rewards` says what did not, and neither has to be inferred.
    applied = {
        "cultivation_progress": 0.0,
        "spirit_stones": {},
        "sect_contribution": 0,
        "beast_affinity": {},
        "battle_pass_xp": 0.0,
        "breakthrough_triggered": result.breakthrough_triggered,
        "new_realm": result.new_realm,
        "new_stage": result.new_stage,
        "rejected_rewards": [],
    }

    def reject(reward, reason):
        """Record a refused line instead of reporting it as a payout."""
        applied["rejected_rewards"].append({
            "type": reward.reward_type.value,
            "currency": reward.currency,
            "requested": reward.amount,
            "reason": reason,
        })

    for reward in result.rewards:
        if reward.reward_type == OfflineRewardType.CULTIVATION_PROGRESS:
            current_progress = get_cult_attr("progress", 0)
            amount = reward.amount
            new_progress = current_progress + amount
            breakthrough = new_progress >= 100
            if breakthrough:
                new_progress = new_progress - 100
                applied["breakthrough_triggered"] = True
            set_cult_attr("progress", new_progress)
            # READ BACK, but verify the write -- do not DIFFERENCE it.
            #
            # The read-back exists so a key this module writes that is not
            # the key the store persists cannot be reported as progress that
            # does not exist. But taking the grant as
            # `stored - current_before` is wrong the moment the bar WRAPS:
            # a player at 90.0 who earns the 10.0 that fills the bar ends at
            # 0.0, and the difference reads -90.0. Measured on this file:
            # `applied["cultivation_progress"]` reported -90.0 for a claim
            # that granted +10.0, so a caller reading the summary was told
            # the player LOST 90 points of cultivation. The wrap is not a
            # loss; it is the realm promotion, reported separately below.
            #
            # So the grant reported is the clamped `amount` -- which the
            # calculator already bounded to the stage remainder -- and the
            # read-back answers a different question: did the store take
            # the value we wrote? Only then is the grant reported.
            stored_progress = get_cult_attr("progress", None)
            if stored_progress is None:
                applied["cultivation_progress"] = 0.0
                reject(reward, "progress did not land in the store")
            elif abs(float(stored_progress) - float(new_progress)) > 1e-6:
                # The row holds something other than what we wrote, so the
                # store did not take our value. Report nothing as paid.
                applied["cultivation_progress"] = 0.0
                reject(reward, "progress did not land in the store")
            else:
                applied["cultivation_progress"] = float(amount)

            if breakthrough and result.new_realm:
                # Advance realm against the STORE's ladder, and only
                # report the realm the row now actually holds.
                #
                # This used to walk a ladder of values ("qi_refining",
                # "golden_core", "mahayana", "ascension") that exist nowhere
                # in the game, and wrote to `player["realm"]` -- not a
                # store column. A breakthrough reported new_realm
                # "foundation" and left cultivation_realm still "mortal".
                current_realm = get_player_attr("cultivation_realm") or "mortal"
                if current_realm in REALM_LADDER:
                    idx = REALM_LADDER.index(current_realm)
                    if idx + 1 < len(REALM_LADDER):
                        set_player_attr("cultivation_realm",
                                        REALM_LADDER[idx + 1])
                        # READ BACK: the reply names the realm the row
                        # holds, never the one the ladder predicted.
                        landed = get_player_attr("cultivation_realm")
                        if landed in REALM_LADDER and \
                                REALM_LADDER.index(landed) > idx:
                            applied["new_realm"] = landed
                            applied["new_stage"] = "early"
                        else:
                            applied["new_realm"] = None
                            applied["new_stage"] = None

        elif reward.reward_type == OfflineRewardType.SPIRIT_STONES:
            currency = reward.currency
            amount = int(reward.amount)
            # `set_stone` reports whether the row took the credit, so the
            # summary names only tiers the player can actually spend.
            # Adding the amount unconditionally told a caller reading
            # `applied` about a payout that was refused and never landed.
            # The balance is then READ BACK so the reported figure is the
            # movement the store actually holds.
            before = get_stone(currency, 0)
            if set_stone(currency, before + amount):
                landed = get_stone(currency, 0) - before
                if landed > 0:
                    applied["spirit_stones"][currency] = \
                        applied["spirit_stones"].get(currency, 0) + landed
                else:
                    reject(reward, "balance did not move")
            else:
                reject(reward, "no spendable column for this tier")

        elif reward.reward_type == OfflineRewardType.SECT_CONTRIBUTION:
            amount = int(reward.amount)
            current = get_player_attr("sect_contribution", 0) or 0
            set_player_attr("sect_contribution", current + amount)
            # READ BACK the contribution actually stored.
            stored = get_player_attr("sect_contribution", None)
            if stored is None:
                applied["sect_contribution"] = 0
                reject(reward, "sect_contribution did not land in the store")
            else:
                applied["sect_contribution"] += int(stored) - int(current)

        elif reward.reward_type == OfflineRewardType.BEAST_AFFINITY:
            beast_id = reward.details.get("beast_id")
            if beast_id:
                beasts = get_beasts()
                # Same list-vs-mapping split as the reader: the store keeps
                # the roster as a list, so a mapping lookup by beast_id
                # never hit and affinity was computed then dropped.
                matched = False
                for owned_id, beast in _iter_owned_beasts(beasts):
                    if owned_id != beast_id:
                        continue
                    before_affinity = beast.get("affinity", 0) or 0
                    beast["affinity"] = before_affinity + reward.amount
                    # READ BACK: the reply names the affinity the beast
                    # dict now holds, not the amount we asked to add.
                    landed = beast.get("affinity", 0) - before_affinity
                    if landed > 0:
                        applied["beast_affinity"][beast_id] = landed
                    matched = True
                    break
                if not matched:
                    reject(reward, "beast is not in the roster")
            else:
                reject(reward, "reward names no beast")

        elif reward.reward_type == OfflineRewardType.BATTLE_PASS_XP:
            # Import battle_pass here to avoid circular import
            from battle_pass import grant_xp
            before_xp = get_bp_xp()
            bp_result = grant_xp(player, int(reward.amount), "offline_progress")
            # READ BACK the XP the row holds. `grant_xp` clamps a single
            # grant to MAX_XP_PER_GRANT, so reporting the request told the
            # player about XP that never landed: measured 1500 requested
            # against a real balance of 1000.
            landed_xp = get_bp_xp() - before_xp
            if landed_xp > 0:
                applied["battle_pass_xp"] = landed_xp
            else:
                applied["battle_pass_xp"] = 0.0
                reject(reward, "battle-pass XP balance did not move")
            applied["battle_pass_result"] = bp_result

    # Update wallet's last_offline_claimed_at if wallet_store available
    if wallet_store:
        wallet = wallet_store.get(get_player_attr("id", ""))
        if wallet:
            wallet.last_offline_claimed_at = time.time()

    return applied


def get_offline_preview(player: Union[Player, Dict], device_now: float, wallet_store=None) -> Dict:
    """
    Get a preview of what offline rewards would be claimed (without applying).
    Used by client to show "you will receive" before claim.
    """
    try:
        result = calculate_full_offline_progress(player, device_now, wallet_store=wallet_store)
    except ValueError as e:
        return {"error": str(e), "can_claim": False}

    preview = {
        "can_claim": True,
        "elapsed_hours": round(result.elapsed_hours, 2),
        "capped_hours": round(result.capped_hours, 2),
        "max_cap_hours": MAX_OFFLINE_HOURS,
        "cultivation_progress": round(result.cultivation_progress_gained, 2),
        "breakthrough_ready": result.breakthrough_triggered,
        "new_realm": result.new_realm,
        "new_stage": result.new_stage,
        "estimated_rewards": {}
    }

    for reward in result.rewards:
        key = reward.reward_type.value
        if reward.currency:
            key = f"{key}_{reward.currency}"
        elif reward.details.get("beast_id"):
            key = f"{key}_{reward.details['beast_id']}"

        preview["estimated_rewards"][key] = {
            "type": reward.reward_type.value,
            "amount": round(reward.amount, 2),
            "currency": reward.currency,
            "details": reward.details
        }

    return preview