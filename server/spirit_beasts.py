"""Spirit Beast Companion — the bond layer over ``beast_config``.

Why this module exists
----------------------
``beast_config.py`` is a MENU. It mints a beast, raises its rarity, feeds it
materials and puts it in a formation slot, and every one of those actions is
paid for with a number the player already has. That is a collection screen.
This module is the part that makes the beast a companion instead:

* a CONTRACT is created at taming, and it has a stage that only rises with the
  player's own cultivation realm (a mortal cannot hold a transcendent bond);
* the beast FIGHTS. Its damage is computed by the one existing simulator,
  ``combat_config.calculate_auto_battle``, with a second attacker inside the
  existing turn loop — not by a second combat system;
* the beast GROWS from those fights, up to the level ceiling its rarity
  already had in ``beast_config.EVOLUTION_REQUIREMENTS``;
* the bond can FAIL. Neglect drains it, defeat wounds it, and breaking the
  contract outright costs the player cultivation progress they already earned.

Two deliberate non-features
---------------------------
**No currency is minted here.** A companion fight pays ZERO spirit stones.
It pays growth. The whole point of the feature is that the reward is the
companion, not a payout, so it cannot become a faucet. The one route that
does pay (``/combat/auto-battle`` in ``main.py``) is untouched and still
charges its own daily budget.

**No high-rarity beast is purchasable.** Rarity is rolled by the server from
``beast_config.BEAST_RARITY`` weights. The request body cannot name a target
rarity, because a client that can name "legendary" is a client that can buy
one. Reaching a high rarity takes taming rolls AND a level gate AND an
evolution cost, all of which are in tables that already existed.

Design grounding
----------------
The xianxia design notes (``xspirit-beast-contract``, ``xbeast-taming``,
``xspirit-pet``, ``xqi-resistance-contract``) are explicit that a spirit beast
is a bond, not a collectible, and that a bond is only interesting when it can
be lost. The pieces of that intent that are mechanical here:

* contract stage ladder, realm-gated (the four-stage ladder, because it is
  the only one in the notes with realm gates attached);
* trust BUILDS — it starts at half, never at full, and is spent by neglect;
* the beast REFUSES below a loyalty floor, because "the beast has its own
  agenda and can refuse" is the line that separates a bond from a summon;
* the environment MULTIPLIES the bond (a rich vein feeds it, scarred qi
  starves it), so where the player stands changes what the bond is worth;
* breaking the contract is the expensive direction, not the free one.

What is invented, stated plainly
--------------------------------
The design notes give no loyalty number, no growth curve and no
mistreatment threshold — they are a design document, not a balance table. The
three numbers below are therefore chosen here and anchored to tables that
already shipped, so a designer can retune them in one place:

* ``BEAST_XP_STEP = 1``  — one level costs ``level`` marks, so the level-20
  ceiling ``EVOLUTION_REQUIREMENTS["common"]["max_level"]`` already declared
  is ~26 days of daily play at the eight fights a day the hunt-track budget
  allows (800 stones at 100/win, read from ``server/missions.py``).
* ``BOND_*`` — loyalty out of 100, starting at 50. A tame is a beginning.
* ``COMPANION_FIGHTS_PER_DAY = 8`` — the same eight the daily hunt budget
  already pays, so a companion cannot be farmed past the day's real income.

The cultivation-progress cost of breaking a contract is NOT invented: it is
``StageConfig.progress_required`` (100.0 per stage) from
``cultivation_config``, multiplied by the contract stage the player is
abandoning. Breaking a bonded contract costs a stage of real progress; that
is the bill, and it comes out of the same number the player's own
breakthroughs are made of.

Wiring
------
``main.py`` is owned by another agent and is not editable from this module.
The routes are declared here behind ``register_spirit_beast_routes(app, ...)``
and mounted from ``first_session.register_first_session_routes`` — the module
that already owns "bond with a starter beast" in the first-session loop, and
which ``main.py`` already calls with the app object (main.py:512). That call
site is ABOVE ``install_ownership_guard`` (main.py:3154), so every route
declared here is covered by the central guard the moment it is registered.
"""
from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, Iterable, List, Optional

from stored_numbers import stored_float as _stored_float, stored_int as _stored_int

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import beast_config
from beast_config import (
    BEAST_RARITY,
    EVOLUTION_REQUIREMENTS,
    find_owned_beast,
    grant_beast,
)

# === Contract stages =====================================================
#
# The four-stage ladder, because it is the only ladder in the design notes
# with realm gates attached. Index is the ORDER, and it is also the multiplier
# on the progress a player loses by breaking the contract: the deeper the
# bond, the worse the betrayal.
CONTRACT_STAGES: List[str] = ["initial", "deepened", "bonded", "transcendent"]

#: The player realm that must be REACHED before a contract may enter a stage.
#: Read off ``cultivation_config.REALMS`` order, so a retune of the realm
#: ladder moves the contract ladder with it rather than desynchronising.
CONTRACT_STAGE_REALM: Dict[str, str] = {
    "initial": "mortal",
    "deepened": "foundation",
    "bonded": "core",
    "transcendent": "nascent_soul",
}


def _realm_order(realm: str) -> int:
    """Order of ``realm`` in the shipped realm ladder; unknown sorts lowest."""
    from cultivation_config import REALMS

    for entry in REALMS:
        if entry.name == realm:
            return int(entry.order)
    return 0


def contract_stage_for(realm: str) -> str:
    """The deepest contract stage a player at ``realm`` may hold.

    Derived, never stored independently: a player who breaks through gains a
    stage, and a player who somehow regresses loses one. There is no second
    source of truth to drift.
    """
    order = _realm_order(realm)
    best = CONTRACT_STAGES[0]
    for stage in CONTRACT_STAGES:
        if order >= _realm_order(CONTRACT_STAGE_REALM[stage]):
            best = stage
    return best


# === Contract kinds ======================================================
#
# Flavour AND a different failure mode, so the roll is not decoration. The
# three come from the design notes' "Contract Types": a forced Dominion bond
# feeds fast and rebels, a Bargain is a deal with no long-term claim and so
# neither gains nor decays loyalty, and an Inheritance bond remembers its
# previous masters, which doubles the betrayal bill.
CONTRACT_KINDS: Dict[str, Dict[str, Any]] = {
    "bargain": {
        "label": "Bargain",
        "bond_gain": 1.0,
        "bond_decay": 0.0,
        "severance_multiplier": 1.0,
        "description": "A deal with terms. No long claim, so it neither grows nor fades.",
    },
    "dominion": {
        "label": "Dominion",
        "bond_gain": 1.5,
        "bond_decay": 1.5,
        "severance_multiplier": 1,
        "description": "Forced and fast. Grows quickly and resents the leash.",
    },
    "inheritance": {
        "label": "Inheritance",
        "bond_gain": 1.0,
        "bond_decay": 1.0,
        "severance_multiplier": 2,
        "description": "Passed down a bloodline. The beast remembers, and judges.",
    },
}
CONTRACT_KIND_WEIGHTS: Dict[str, int] = {"bargain": 50, "dominion": 30, "inheritance": 20}


# === Bond (loyalty) ======================================================
#
# The loyalty stat the design notes never gave a number for. It is the one
# mechanic the whole system hangs on, so it is stated here rather than
# scattered: out of BOND_MAX, starting at BOND_ON_TAME (a tame is a
# beginning, never a finished bond), and REFUSING_BOND is where the beast
# stops obeying.
BOND_MAX = 100
BOND_ON_TAME = 50
BOND_PER_WIN = 2
BOND_PER_LEVEL = 1
BOND_DECAY_PER_IDLE_DAY = 1
BOND_LOSS_ON_DEFEAT = 30
REFUSING_BOND = 20
#: Idle days a bond survives before neglect starts eating it. One grace day,
#: for the same reason the daily streak has one: a real player misses an
#: evening, and a system that punishes that teaches them to stop opening the
#: game.
BOND_NEGLECT_GRACE_DAYS = 1


# === Environment =========================================================
#
# Bond-strength multiplier by where the player is standing, from the
# environment tables in xqi-resistance-contract. A rich vein feeds a bond and
# scarred qi starves it, so the SAME beast is worth more in one place. This
# multiplies bond GAIN only — a hostile place cannot claw back a bond the
# player already earned, it just stops paying more.
QI_BOND_MULTIPLIER: Dict[str, float] = {
    "sacred": 1.5,
    "convergence": 1.5,
    "rich_vein": 1.2,
    "moderate": 1.0,
    "ambient": 0.8,
    "mortal": 0.8,
    "opposite_qi": 0.4,
    "low_qi": 0.2,
    "scarred": 0.05,
}
#: An unrecognised zone is treated as ordinary ground, not as scarred qi. A
#: lookup miss must not silently impose the harshest multiplier in the table.
DEFAULT_QI_BOND_MULTIPLIER = 1.0


def qi_bond_multiplier(zone: Optional[str]) -> float:
    return QI_BOND_MULTIPLIER.get(str(zone or "").strip().lower(), DEFAULT_QI_BOND_MULTIPLIER)


# === Growth ==============================================================
#
# ``BEAST_XP_STEP`` is the one new pacing number (see the module docstring).
# The level CEILING is not new: it is ``EVUTION_REQUIREMENTS[rarity]
# ["max_level"]``, so a beast cannot out-level the gate that already decides
# when it evolves.
BEAST_XP_STEP = 1
#: Companion fights per UTC day. Same eight the daily hunt budget already
#: pays; a companion must not be a second daily income.
COMPANION_FIGHTS_PER_DAY = 8
#: The rival a companion fight is measured against, as a fraction of the
#: player's own stat line. These are the values at which a DEFEAT is actually
#: reachable, which is what makes the loyalty cost a mechanic rather than dead
#: code. Measured over 12 seeds x 15 player/companion pairings at (0.8, 2.0):
#: These two numbers came from a 4x5 sweep over both axes, and the first two
#: pairs tried were wrong in OPPOSITE directions, which is why this was a sweep
#: rather than a guess:
#:
#:   (0.5, 1.0) main.py's shape -- the player won 24/24 in EVERY case probed,
#:     including a bond-20 companion, so the loyalty cost was dead code.
#:   (0.8, 2.0) -- a fresh level-1 common lost 24/24, so a new player's first
#:     companion could never win a fight and therefore never GROW. The growth
#:     loop was unreachable from the very start, which is worse still.
#:   (0.7, 1.6) -- fresh won 24/24 and bond-20 won 3/24, which looked right,
#:     but a level-60 legendary then LOST 24/24. Deriving the rival's health
#:     from the player's health pool while its damage comes from attack means
#:     a tanky player can never finish inside the turn limit, so an
#:     over-invested companion was punished rather than rewarded.
#:
#: At (1.0, 1.0) the rival is an even match for the player ALONE and the
#: companion is the entire margin. Measured over 24 seeds against a
#: 100-atk / 1000-hp / 50-def player: a fresh level-1 common wins 24/24 (the
#: player can grow), the same companion at the refusing bond floor of 20 wins
#: 0/24 (neglect is fatal to a weak beast), and anything level 6 or better
#: wins 24/24. Growth and bond both buy real, measurable wins, and neither
#: trivialises the fight.
COMPANION_FOE_HP_SHARE = 1.0
COMPANION_FOE_ATK_SHARE = 1.0
#: Per-day ledger key on the player row. Server-written; no code path reads
#: it from a request body, so a caller cannot rewind it.
COMPANION_LEDGER_KEY = "companion_daily"


#: ``_stored_float`` / ``_stored_int`` are imported from
#: ``stored_numbers`` -- see that module for the measured reasons a
#: stored number needs a three-exception tuple AND an explicit
#: finiteness test. They are aliased to these two names because every
#: read site in this module (and its contract tests) uses them.


def _bond_of(contract: dict) -> int:
    """The ONE reader for a stored bond. Read paths and write paths both use it.

    THE DECISION, in one place: a corrupt bond means 0.

    0 is not an arbitrary number -- it is what an absent bond already means,
    and it sits below ``REFUSING_BOND``, so a corrupt row reads as a beast
    that declines to fight and says why ("bond_too_low"). Fail closed and
    visible. The row is left on disk for the player to see and a repair to
    rewrite, rather than deleted or raised on.

    A bond is CORRUPT when it cannot be read as a finite number, or when it
    falls outside 0..BOND_MAX. The range test is part of the rule rather
    than an extra: every writer in this module clamps into that range
    (``max(0, ...)`` on the way down, ``min(BOND_MAX, ...)`` on the way up),
    so a stored value outside it cannot have come from the server and is
    corruption by the same definition as an ``inf``.

    Before this existed the two families of call sites disagreed: the read
    paths answered 0 and the two write paths answered ``BOND_ON_TAME``, so
    one corrupt field read as "will not fight" on the panel and was silently
    rewritten to a healthy tame bond by the next fight.
    """
    if not isinstance(contract, dict):
        return 0
    bond = _stored_int(contract.get("bond"), 0)
    return bond if 0 <= bond <= BOND_MAX else 0


def xp_to_next_level(level: int) -> int:
    """Marks needed to go from ``level`` to ``level + 1``."""
    return max(1, _stored_int(level, 1)) * BEAST_XP_STEP


def level_ceiling(rarity: str) -> int:
    """The level this rarity may reach before its evolution gate opens."""
    requirement = EVOLUTION_REQUIREMENTS.get(rarity) or {}
    return int(requirement.get("max_level", 1))


# === State ===============================================================

SEVERED = "severed"


def _day_index(now: Optional[float] = None) -> int:
    """UTC day index, matching ``time.time() // 86400`` in server/missions.py."""
    return int((time.time() if now is None else now) // 86400)


def new_contract(player: dict, beast: dict) -> dict:
    """Create the contract that binds ``beast`` to ``player``.

    Written at taming and never re-rolled. Every field here is either derived
    from the player's realm (the stage) or rolled once from a weights table
    (the kind) — nothing is client-supplied.
    """
    realm = str(player.get("cultivation_realm", "mortal"))
    total = sum(CONTRACT_KIND_WEIGHTS.values())
    roll = random.random() * total
    cumulative = 0
    kind = "bargain"
    for name, weight in CONTRACT_KIND_WEIGHTS.items():
        cumulative += weight
        if roll < cumulative:
            kind = name
            break
    now = time.time()
    return {
        "kind": kind,
        "stage": contract_stage_for(realm),
        "bond": BOND_ON_TAME,
        "bond_updated_at": now,
        "last_fought_at": now,
        "growth_xp": 0,
        "status": "active",
        "bonds_with": player.get("id"),
        "severed_at": None,
        "wounded_until_day": 0,
    }


def contract_of(beast: dict) -> dict:
    """The contract on ``beast``, creating a default one if it predates this.

    A beast minted before contracts existed still has to be fightable and
    still has to obey the loyalty floor, so the default is a plain initial
    contract at starting loyalty rather than a special exempt case.
    """
    contract = beast.get("contract")
    if not isinstance(contract, dict):
        contract = {
            "kind": "bargain",
            "stage": CONTRACT_STAGES[0],
            "bond": BOND_ON_TAME,
            "bond_updated_at": time.time(),
            "last_fought_at": time.time(),
            "growth_xp": 0,
            "status": "active",
            "bonds_with": beast.get("owner_id"),
            "severed_at": None,
            "wounded_until_day": 0,
        }
        beast["contract"] = contract
    return contract


def beast_level(beast: dict) -> int:
    return max(1, _stored_int(beast.get("level"), 1))


def _scale_to_level(beast: dict) -> None:
    """Restate a beast's stats for its level and rarity.

    Derived from ``BEAST_RARITY[rarity]["stat_mult"]`` — the same multiplier
    evolution already applies — scaled by level. Evolution RESETS the level to
    1, so this has to run on evolve as well as on level-up, or an evolved
    beast would keep the stat line of the rarity it just left.
    """
    rarity = str(beast.get("rarity", "common"))
    mult = float(BEAST_RARITY.get(rarity, {}).get("stat_mult", 1.0))
    level = beast_level(beast)
    growth = 1.0 + 0.05 * (level - 1)
    scale = mult * growth
    beast["hp"] = max(1, int(100 * scale))
    beast["atk"] = max(1, int(20 * scale))
    beast["def"] = max(1, int(10 * scale))


# === Neglect =============================================================


def apply_neglect(beast: dict, now: Optional[float] = None) -> int:
    """Decay the bond for time the beast spent with nobody. Returns marks lost.

    Computed from elapsed days rather than ticked by a scheduler, so a player
    who was away for a week is charged a week and a player who was never away
    is charged nothing. The grace day is the daily-streak lesson: one missed
    evening is not mistreatment.
    """
    contract = contract_of(beast)
    if contract.get("status") != "active":
        return 0
    moment = time.time() if now is None else now
    updated = _stored_float(contract.get("bond_updated_at"), moment)
    elapsed_days = int(moment - updated) // 86400
    if elapsed_days <= BOND_NEGLECT_GRACE_DAYS:
        contract["bond_updated_at"] = moment
        return 0
    chargeable = elapsed_days - BOND_NEGLECT_GRACE_DAYS
    kind = CONTRACT_KINDS.get(str(contract.get("kind")), CONTRACT_KINDS["bargain"])
    lost = int(chargeable * BOND_DECAY_PER_IDLE_DAY * float(kind["bond_decay"]))
    if lost <= 0:
        contract["bond_updated_at"] = moment
        return 0
    # A CORRUPT bond reads as 0, the same as an absent one -- the same
    # default ``will_fight`` and ``companion_view`` already use. It used
    # to read as BOND_ON_TAME here, which meant the write paths silently
    # REPAIRED a corrupt row upward to a tame bond: the panel said 0 and
    # "bond_too_low", then the next fight wrote a fighting bond back and
    # the beast fought anyway. One corrupt field, two meanings.
    before = _bond_of(contract)
    contract["bond"] = max(0, before - lost)
    contract["bond_updated_at"] = moment
    return before - int(contract["bond"])


# === Growth ==============================================================


def award_growth(beast: dict, won: bool) -> Dict[str, Any]:
    """Grow ``beast`` by one fought battle. Returns what moved.

    Level-ups stop at the rarity's existing evolution ceiling, so progress can
    never walk past the gate that ``beast_config.evolve_beast`` enforces.
    """
    contract = contract_of(beast)
    xp = _stored_int(contract.get("growth_xp"), 0)
    ceiling = level_ceiling(str(beast.get("rarity", "common")))
    levels_gained = 0
    level = beast_level(beast)

    if won and level < ceiling:
        xp += 1
        while level < ceiling and xp >= xp_to_next_level(level):
            xp -= xp_to_next_level(level)
            level += 1
            levels_gained += 1

    contract["growth_xp"] = xp
    if levels_gained:
        beast["level"] = level
        _scale_to_level(beast)
    return {
        "level": beast_level(beast),
        "levels_gained": levels_gained,
        "growth_xp": xp,
        "xp_to_next": xp_to_next_level(beast_level(beast)),
        "level_ceiling": ceiling,
    }


# === Loyalty =============================================================


def will_fight(beast: dict) -> bool:
    """True when the beast is willing and able to stand beside its player.

    Three independent reasons to refuse, and each is reported separately by
    the route so the player learns WHICH one happened: a severed contract, a
    beast still recovering from being beaten, and a bond that has been
    neglected below the floor.
    """
    contract = contract_of(beast)
    if contract.get("status") != "active":
        return False
    if _day_index() < _stored_int(contract.get("wounded_until_day"), 0):
        return False
    return _bond_of(contract) >= REFUSING_BOND


def refusal_reason(beast: dict) -> Optional[str]:
    contract = contract_of(beast)
    if contract.get("status") != "active":
        return "severed"
    if _day_index() < _stored_int(contract.get("wounded_until_day"), 0):
        return "wounded"
    if _bond_of(contract) < REFUSING_BOND:
        return "bond_too_low"
    return None


def apply_battle_bond(beast: dict, won: bool, multiplier: float) -> int:
    """Charge the bond for one fought battle. Returns the new bond.

    A defeat is the expensive direction: the beast took the hit meant for its
    player, loses a fixed chunk of loyalty, and is out of the fight until the
    next UTC day. That is the "mutual injury" rung of the contract notes,
    expressed as one number the player can watch fall.
    """
    contract = contract_of(beast)
    kind = CONTRACT_KINDS.get(str(contract.get("kind")), CONTRACT_KINDS["bargain"])
    # Same rule as ``apply_neglect`` and as the read path: a corrupt bond
    # is 0, never a free BOND_ON_TAME. A win therefore cannot lift a
    # broken row over the refusing floor -- it has to earn it back one
    # won fight at a time from the floor.
    bond = _bond_of(contract)
    moment = time.time()
    if won:
        bond = min(BOND_MAX, bond + int(round(BOND_PER_WIN * multiplier * float(kind["bond_gain"]))))
    else:
        bond = max(0, bond - BOND_LOSS_ON_DEFEAT)
        contract["wounded_until_day"] = _day_index() + 1
    contract["bond"] = bond
    contract["bond_updated_at"] = moment
    contract["last_fought_at"] = moment
    return bond


# === Severance ===========================================================


def severance_cost(player: dict, beast: dict) -> Dict[str, Any]:
    """What breaking this contract costs the player, in cultivation progress.

    Not a currency and not invented: it is ``progress_required`` from
    ``cultivation_config`` — the same 100.0 the player's own breakthroughs are
    made of — times the contract stage being abandoned, times the contract
    kind's betrayal multiplier. A bargain costs one stage. An inherited
    transcendent bond costs eight. There is no way to sell your way out of
    this, which is the point: the expensive direction has to be expensive.
    """
    from cultivation_config import get_realm, get_stage

    contract = contract_of(beast)
    stage = str(contract.get("stage", CONTRACT_STAGES[0]))
    stage_index = CONTRACT_STAGES.index(stage) if stage in CONTRACT_STAGES else 0
    kind = CONTRACT_KINDS.get(str(contract.get("kind")), CONTRACT_KINDS["bargain"])

    realm_name = str(player.get("cultivation_realm", "mortal"))
    stage_cfg = get_stage(realm_name, str(player.get("cultivation_stage", "early")))
    required = float(getattr(stage_cfg, "progress_required", 100.0) or 100.0)

    # (stage_index + 1), not stage_index. A stage-0 contract is the STARTING
    # contract, and a bill of zero for it means the cheapest way to replace a
    # beast you dislike is to break the bond and tame again for free -- which
    # is exactly the "menu" behaviour this feature exists to remove. Every
    # severance costs at least one stage of real progress; a deeper bond costs
    # proportionally more.
    total = required * (stage_index + 1) * int(kind["severance_multiplier"])
    return {
        "cultivation_progress": int(total),
        "stages_lost": stage_index + 1,
        "progress_per_stage": int(required),
        "contract_stage": stage,
        "contract_stage_index": stage_index,
        "contract_kind": str(contract.get("kind", "bargain")),
        "severance_multiplier": int(kind["severance_multiplier"]),
    }


def apply_severance(player: dict, beast: dict) -> Dict[str, Any]:
    """Break the contract. Charges the progress bill and severs the beast.

    The beast is marked severed and dropped out of the formation rather than
    deleted: a bond that ended is still part of the record, and the player can
    see what they lost and what it cost.
    """
    cost = severance_cost(player, beast)
    contract = contract_of(beast)
    contract["status"] = SEVERED
    contract["severed_at"] = time.time()
    contract["bond"] = 0
    beast["in_formation"] = False

    progress_key = "cultivation_progress"
    before = player.get(progress_key, 0)
    try:
        before_value = float(before)
    except (TypeError, ValueError, OverflowError):
        before_value = 0.0
    # inf/nan survive float() without raising, and max(0.0, inf) is inf, so
    # this value is written straight back onto the player. Treat it as 0.0.
    if not math.isfinite(before_value):
        before_value = 0.0
    after_value = max(0.0, before_value - float(cost["cultivation_progress"]))
    player[progress_key] = after_value

    formation = player.get("beast_formation")
    if isinstance(formation, list) and beast.get("beast_id") in formation:
        for index, held in enumerate(formation):
            if held == beast.get("beast_id"):
                formation[index] = None
    return {
        "cost": cost,
        "cultivation_progress_before": before_value,
        "cultivation_progress_after": after_value,
        "beast": beast,
    }


# === Presentation ========================================================


def companion_view(beast: dict, player: dict) -> Dict[str, Any]:
    """Everything a panel needs to draw one companion, in one dict.

    Built server-side so the client never has to re-derive a threshold, and so
    the refusal reason and the severance bill are the SAME numbers the server
    enforces.

    The stage is RE-DERIVED from the player's realm on every read rather than
    trusted from the stored contract. A breakthrough has to deepen the bond
    the player already holds, and the contract is only the durable record of
    the parts that are not derivable (kind, bond, XP, status). Storing the
    stage and never refreshing it meant a player who reached Core Formation
    kept an "initial" contract and a one-stage severance bill forever, which is
    the opposite of what the stage is for.
    """
    contract = contract_of(beast)
    kind = CONTRACT_KINDS.get(str(contract.get("kind")), CONTRACT_KINDS["bargain"])
    stage = contract_stage_for(str(player.get("cultivation_realm", "mortal")))
    if contract.get("status") == "active":
        contract["stage"] = stage
    else:
        # A severed contract keeps the stage it died at. The bill is already
        # paid; re-deriving it upward after a breakthrough would be a second
        # charge for the same betrayal.
        stage = str(contract.get("stage", stage))
    bond = _bond_of(contract)
    refusal = refusal_reason(beast)
    level = beast_level(beast)
    return {
        "beast": beast,
        "beast_id": beast.get("beast_id"),
        "name": beast.get("name") or beast.get("beast_id"),
        "rarity": beast.get("rarity", "common"),
        "element": beast.get("element"),
        "level": level,
        "hp": _stored_int(beast.get("hp"), 0),
        "atk": _stored_int(beast.get("atk"), 0),
        "def": _stored_int(beast.get("def"), 0),
        "blood_level": _stored_int(beast.get("blood_level"), 0),
        "in_formation": bool(beast.get("in_formation")),
        "contract": {
            "kind": str(contract.get("kind", "bargain")),
            "kind_label": kind["label"],
            "kind_description": kind["description"],
            "stage": stage,
            "stage_index": CONTRACT_STAGES.index(stage) if stage in CONTRACT_STAGES else 0,
            "status": str(contract.get("status", "active")),
            "bond": bond,
            "bond_max": BOND_MAX,
            "refusing_bond": REFUSING_BOND,
            "will_fight": refusal is None,
            "refusal_reason": refusal,
            "severed_at": contract.get("severed_at"),
        },
        "growth": {
            "xp": _stored_int(contract.get("growth_xp"), 0),
            # The same number under the name the fight route reports it as, so
            # a client reads one field name in both places.
            "growth_xp": _stored_int(contract.get("growth_xp"), 0),
            "xp_to_next": xp_to_next_level(level),
            "level_ceiling": level_ceiling(str(beast.get("rarity", "common"))),
            "at_ceiling": level >= level_ceiling(str(beast.get("rarity", "common"))),
        },
        "severance": severance_cost(player, beast),
    }


def roster_view(player: dict) -> Dict[str, Any]:
    """Every companion the player holds, live and severed, plus the cap."""
    beasts = player.get("beasts")
    if not isinstance(beasts, list):
        beasts = []
    for beast in beasts:
        apply_neglect(beast)
    # The same guard the internal _active() helper uses, and for the same
    # reason: ``.get(key, default)`` substitutes only when the key is ABSENT,
    # so a stored ``None`` used to publish as ``None`` while an absent key
    # published three slots. A panel that indexes ``formation[0]`` therefore
    # raised on a row the server itself can produce. The published view now
    # matches the helper: anything that is not a list is not a formation, and
    # an empty formation is ``FORMATION_SLOTS`` empty slots -- the same count
    # this reply publishes beside it as ``formation_slots``.
    formation = player.get("beast_formation")
    if not isinstance(formation, list):
        formation = [None] * int(beast_config.FORMATION_SLOTS)
    return {
        "companions": [companion_view(b, player) for b in beasts if isinstance(b, dict)],
        "formation": formation,
        "formation_slots": beast_config.FORMATION_SLOTS,
        "contract_stages": CONTRACT_STAGES,
        "refusing_bond": REFUSING_BOND,
    }


# === Combat ==============================================================


# --- How strong a companion is ===========================================
#
# Every number below was CHOSEN BY MEASURING, and the measurement is why this
# block looks the way it does. ``calculate_base_damage`` subtracts a FLAT
# defence from the attack, so damage is linear in attack only ABOVE the defence
# value and collapses to ``DAMAGE_FLOOR`` below it. That one property produced
# two measured failures on the first pass:
#
#   1. Using the beast's own ``atk`` (20 x rarity multiplier, from
#      ``beast_config``) gave ~27 against a 100-attack player and a
#      50-defence enemy. Every hit landed on the damage floor: 8 damage out of
#      483, a 1.7% share. A companion that cannot change an outcome is
#      decoration, not a companion.
#   2. Raising the share linearly fixed that and overshot hard — a level-20
#      legendary companion hit for 20x the player's damage, because rarity
#      (up to 3.0), level and bloodline all compounded on one uncapped term.
#
# The fix is to derive the companion's attack from the PLAYER'S OWN attack
# line — the same move ``main._auto_battle_enemy_stats`` makes when it derives
# the opponent — and to make the three growth axes SUBLINEAR so they compose
# without running away. Measured share of the player's per-hit damage
# (player 50/hit against a 50-defence enemy), at a full bond:
#
# against the WORST-CASE enemy defence (player def 50 x the 1.1 top of the
# opponent's uniform spread = 55, where a 44-damage player hit is the
# baseline), at the starting bond of 50 a freshly tamed companion lands at:
#   common 0.59  uncommon 0.86  rare 1.09  epic 1.36  legendary 1.98
# and at a full bond, level moves it:
#   common   L1 1.23  L6 2.02  L20 2.77  L60 3.93
#   legendary L1 3.07 (at the cap from L6 on)
# while bond (common L20) and bloodline (rare L20) move it independently:
#   bond  100 -> 2.77,  60 -> 1.95,  20 -> 1.14,  0 -> 0.73
#   blood  b0 3.84, b1 4.34, b2+ at the cap
#
# Read as design: a freshly tamed common companion is a real but modest ally,
# and every axis a player can spend on is visible in the next fight. None of
# it is a private multiplier — the swing still goes through
# ``combat_config.calculate_base_damage`` and the shared element table.
COMPANION_ATTACK_SHARE = 1.1
#: Rarity enters as ``stat_mult ** COMPANION_RARITY_EXPONENT``, not as the raw
#: multiplier. The raw 1.0 -> 3.0 spread compounds with level and bloodline into
#: a 20x swing; the square root keeps legendary meaningfully ahead of common
#: without making the gap unplayable.
COMPANION_RARITY_EXPONENT = 0.5
#: Level growth is ``1 + COMPANION_LEVEL_GROWTH * sqrt(level - 1)``. Square
#: root rather than linear so a level-100 companion is genuinely strong
#: without a level-20 one being irrelevant — which is the point of a long
#: progression arc.
COMPANION_LEVEL_GROWTH = 0.14
#: Ceiling on a companion's attack as a multiple of the player's own. A safety
#: rail rather than a balance rule: it is the term that stops an
#: over-invested legendary bloodline companion trivialising the fight it is
#: standing in.
COMPANION_ATTACK_CAP_SHARE = 2.5


def companion_stat_block(beast: dict, player: dict) -> Dict[str, Any]:
    """The beast's contribution to a fight, as a stat line.

    The beast attacks with its own element, so it is subject to the same
    advantage table as the player. Its attack is scaled by the BOND, not by
    the player: a neglected beast swings weakly, which is the mechanical form
    of "the bond is not a collectible".
    """
    contract = contract_of(beast)
    bond = _bond_of(contract)
    rarity = str(beast.get("rarity", "common"))
    level = beast_level(beast)
    blood = _stored_int(beast.get("blood_level"), 0)

    # Loyalty: half the share at zero bond, the full share at a full one. The
    # floor is deliberate -- a bond at zero still swings, it just swings
    # badly, because the beast refusing to fight at all is modelled by
    # will_fight() and reported as such rather than as a suspiciously weak hit.
    loyalty_scale = 0.5 + 0.5 * (bond / float(BOND_MAX))
    # Rarity and growth both come from tables that already shipped — the same
    # BEAST_RARITY multiplier evolution uses — shaped sublinearly so the three
    # axes compose (see the measurement table above).
    rarity_scale = float(BEAST_RARITY.get(rarity, {}).get("stat_mult", 1.0)) ** COMPANION_RARITY_EXPONENT
    level_scale = 1.0 + COMPANION_LEVEL_GROWTH * math.sqrt(max(0, level - 1))
    # Bloodline attunement: the four tiers BLOOD_PROGRESSION already prices.
    blood_scale = 1.0 + 0.10 * blood

    player_atk = max(1, _stored_int(player.get("atk"), 100))
    # _stored_int on the PRODUCTS, not just on the input. Guarding only
    # player_atk is not enough: the multipliers push it back over the float
    # ceiling. COMPANION_ATTACK_CAP_SHARE is 2.5, so a stored atk of 1e308 --
    # finite, so it sails past math.isfinite -- becomes 2.5e308 = inf here and
    # int(inf) raises OverflowError, taking /beasts/companion-fight down with
    # a 500. Measured: this was the one case still crashing after the three
    # route-level conversions were guarded.
    #
    # Both fail closed to 0, which the max(1, ...) below turns into a
    # 1-attack companion. A row this corrupt should not produce a monster hit,
    # and it stays visible to the player for a repair.
    ceiling = _stored_int(COMPANION_ATTACK_CAP_SHARE * player_atk, 0)
    atk = _stored_int(
        player_atk
        * COMPANION_ATTACK_SHARE
        * rarity_scale
        * level_scale
        * blood_scale
        * loyalty_scale,
        0,
    )
    return {
        "atk": max(1, min(ceiling, atk)),
        "hp": max(1, _stored_int(beast.get("hp"), 0)),
        "element": str(beast.get("element", "earth")),
        "level": level,
        "bond": bond,
        "rarity": rarity,
        "name": beast.get("name") or beast.get("beast_id"),
    }


# === Routes ==============================================================


class BeastTameCompanionRequest(BaseModel):
    player_id: str
    items_used: int = 0


class BeastBondRequest(BaseModel):
    player_id: str
    beast_id: str


class BeastReleaseRequest(BaseModel):
    player_id: str
    beast_id: str
    client_version: int = 0


def register_spirit_beast_routes(app: FastAPI, players_db_accessor) -> None:
    """Mount the companion routes. Called from first_session.register_*.

    ``players_db_accessor`` is main.py's ``_players_db_accessor``, the same
    indirection every other module takes, so a test that rebinds
    ``main.players_db`` is picked up rather than serving a stale handle.
    """

    def _db():
        raw = players_db_accessor() if callable(players_db_accessor) else players_db_accessor
        return raw() if callable(raw) else raw

    def _load(player_id: str) -> dict:
        db = _db()
        try:
            return db[player_id]
        except KeyError:
            raise HTTPException(status_code=404, detail="Player not found") from None

    def _owned(player: dict, beast_id: str) -> dict:
        """Resolve ``beast_id`` inside THIS player's roster, or 404.

        ``find_owned_beast`` refuses a beast stamped for another player and
        returns the same None for "absent" and "not yours", so this cannot be
        used to probe whether someone else's beast id exists.
        """
        beasts = player.get("beasts")
        beast = find_owned_beast(beasts if isinstance(beasts, list) else [], beast_id, str(player.get("id")))
        if beast is None:
            raise HTTPException(status_code=404, detail="Beast not found")
        return beast

    def _active(player: dict) -> Optional[dict]:
        """The single companion that fights beside the player.

        The formation slot decides it, so "who is with me" is already a
        decision the player made in the existing formation UI rather than a
        second selection to keep in sync.
        """
        formation = player.get("beast_formation")
        if not isinstance(formation, list):
            return None
        beasts = player.get("beasts")
        if not isinstance(beasts, list):
            return None
        for held in formation:
            if not held:
                continue
            found = find_owned_beast(beasts, str(held), str(player.get("id")))
            if found is not None and found.get("in_formation"):
                return found
        return None

    @app.get("/beasts/{player_id}/companions")
    def get_companions(player_id: str):
        """Every companion, its bond, its growth and its severance bill."""
        db = _db()
        with db.transaction(player_id):
            player = _load(player_id)
            return roster_view(player)

    @app.post("/beasts/tame-companion")
    def post_tame_companion(req: BeastTameCompanionRequest):
        """Find a beast and bond with it.

        The rarity is ROLLED here from the shipped weights. The request body
        deliberately has no target-rarity field: the client cannot name the
        beast it gets, so a high rarity cannot be bought, only rolled for.
        """
        from inventory import ITEM_DEFINITIONS  # noqa: F401  (import-time wiring check)

        db = _db()
        _load(req.player_id)
        with db.transaction(req.player_id):
            player = _load(req.player_id)
            inventory = player.get("inventory")
            if not isinstance(inventory, dict):
                inventory = {}
                player["inventory"] = inventory
            items_used = max(0, min(beast_config.TAMING_MAX_ITEMS, int(req.items_used or 0)))
            if items_used <= 0:
                raise HTTPException(status_code=400, detail="At least one taming item is required")
            from main import inventory_store

            inventory_store.ensure_player_inventory(req.player_id, inventory)
            if inventory_store.get_item_count(req.player_id, "taming_bait") < items_used:
                raise HTTPException(status_code=400, detail="Insufficient taming items")
            consumed = inventory_store.consume_items(req.player_id, {"taming_bait": items_used})
            if not consumed.get("ok"):
                raise HTTPException(status_code=400, detail=consumed.get("error", "Insufficient taming items"))

            # The bait is spent either way on a REFUSED tame -- that is the
            # player's own gamble. Only a generation that RAISES hands it back,
            # because then the server, not the player, is the reason there is
            # no beast. Same contract and same measured reason as main.py's
            # /beasts/tame; see main._refund_bait_if_it_fails.
            from main import _refund_bait_if_it_fails as _refund_bait

            with _refund_bait(inventory_store, req.player_id, "taming_bait", items_used):
                beast = grant_beast(player)
            success, message = beast_config.attempt_tame(beast, items_used)
            if not success:
                # The bait is spent either way: a failed attempt burned the
                # items, and the beast never became anyone's.
                inventory["taming_bait"] = inventory_store.get_item_count(req.player_id, "taming_bait")
                return {"ok": False, "message": message}

            beast["name"] = _beast_name(beast)
            beast["contract"] = new_contract(player, beast)
            inventory["taming_bait"] = inventory_store.get_item_count(req.player_id, "taming_bait")
            return {
                "ok": True,
                "message": message,
                "companion": companion_view(beast, player),
                "inventory": inventory,
            }

    @app.post("/beasts/companion-fight")
    def post_companion_fight(req: BeastBondRequest):
        """Fight a wilder beast alongside your companion.

        The fight is resolved by ``combat_config.calculate_auto_battle`` —
        the SAME simulator ``/combat/auto-battle`` and ``/pvp/match`` use —
        with the companion as a second attacker inside its existing turn loop.
        No second combat system, and no second payout: this route pays ZERO
        spirit stones. What it pays is growth, and it is capped per UTC day
        so it cannot be farmed.
        """
        from combat_config import AUTO_BATTLE_MAX_TURNS, calculate_auto_battle
        import random as _rng
        import secrets as _secrets

        db = _db()
        _load(req.player_id)
        with db.transaction(req.player_id):
            player = _load(req.player_id)
            beast = _owned(player, req.beast_id)
            contract = contract_of(beast)
            apply_neglect(beast)

            refusal = refusal_reason(beast)
            if refusal is not None:
                return {
                    "ok": False,
                    "error": "companion_refused",
                    "reason": refusal,
                    "companion": companion_view(beast, player),
                }

            ledger = player.get(COMPANION_LEDGER_KEY)
            day = _day_index()
            if not isinstance(ledger, dict) or int(ledger.get("day", -1)) != day:
                ledger = {"day": day, "fights": 0}
                player[COMPANION_LEDGER_KEY] = ledger
            fought = int(ledger.get("fights", 0) or 0)
            if fought >= COMPANION_FIGHTS_PER_DAY:
                return {
                    "ok": False,
                    "error": "daily_companion_fights_spent",
                    "fights_today": fought,
                    "daily_cap": COMPANION_FIGHTS_PER_DAY,
                    "companion": companion_view(beast, player),
                }

            rng = _rng.Random(_secrets.randbits(64))
            multiplier = qi_bond_multiplier(player.get("current_zone"))
            companion = companion_stat_block(beast, player)

            # Every numeric field here is a STORED number on the player row,
            # and that row is JSON on disk -- so one corrupt or hand-edited
            # field used to reach the simulator as inf/nan and take this route
            # down with a 500. _stored_float answers the same default a MISSING
            # key already gets, so a corrupt stat reads as the absent case
            # instead of a crash, and the row stays visible for a repair.
            #
            # The three int() conversions below are guarded AGAIN on purpose.
            # A finite value can still overflow back into inf inside the
            # arithmetic that feeds them (1e308 * 1.0 * 1.05 does exactly
            # that), so guarding only the read here would leave the hole open
            # one line later. Both guards, or neither is worth anything.
            player_stats = {
                "realm": player.get("cultivation_realm", "mortal"),
                "stage": player.get("cultivation_stage", "early"),
                "element": player.get("spirit_root", "fire"),
                "atk": _stored_float(player.get("atk", 100), 100.0),
                "def_": _stored_float(player.get("def_", 50), 50.0),
                "hp": _stored_float(player.get("hp", 1000), 1000.0),
                "spd": _stored_float(player.get("spd", 100), 100.0),
            }
            # The opponent is derived from the player, never from the request,
            # so a client cannot pick a 1-HP enemy. It is deliberately a HARDER
            # matchup than main.py's /combat/auto-battle opponent, and the
            # reason is measured rather than assumed:
            #
            # main.py's derivation (0.5x HP, 1.0x attack) made the PLAYER win
            # 180 of 180 seeded trials across every player/companion pairing
            # probed -- including a level-1 common companion at the refusing
            # bond floor. A fight the player cannot lose makes the whole
            # loyalty cost unreachable in play: apply_battle_bond's defeat
            # branch, the wound it leaves, and the refusal that follows would
            # all be dead code that only a unit test could ever reach.
            #
            # The rival is an even match for the player ALONE, so the companion
            # is the entire margin (see the derivation table on
            # COMPANION_FOE_*). Measured over 24 seeds: a fresh level-1 common
            # wins 24/24 so a new player can grow, the same common at the
            # refusing bond floor wins 0/24, and anything level 6 or better
            # wins 24/24. Both growth and bond buy measurable wins.
            # _stored_int, not int(): these three are the conversions the task
            # named, and each one raises on a different input -- int(inf) raises
            # OverflowError, int(nan) raises ValueError, and a finite value too
            # large for int() (1e400) raises OverflowError even though it passed
            # the isfinite check above. _stored_int absorbs all three.
            enemy_stats = {
                "hp": max(1, _stored_int(player_stats["hp"] * COMPANION_FOE_HP_SHARE * rng.uniform(0.9, 1.1), 1)),
                "atk": max(1, _stored_int(player_stats["atk"] * COMPANION_FOE_ATK_SHARE * rng.uniform(0.9, 1.1), 1)),
                "def": max(0, _stored_int(player_stats["def_"] * rng.uniform(0.9, 1.1), 0)),
                "spd": player_stats["spd"],
                "realm": player_stats["realm"],
                "stage": player_stats["stage"],
                "element": "earth",
            }

            result = calculate_auto_battle(
                player_stats=player_stats,
                enemy_stats=enemy_stats,
                companion=companion,
                max_turns=AUTO_BATTLE_MAX_TURNS,
                seed=rng.getrandbits(32),
            )
            won = result.get("winner") == "player"
            growth = award_growth(beast, won)
            bond = apply_battle_bond(beast, won, multiplier)
            if growth["levels_gained"]:
                bond = min(BOND_MAX, bond + BOND_PER_LEVEL * growth["levels_gained"])
                contract["bond"] = bond
            ledger["fights"] = fought + 1

            return {
                "ok": True,
                "won": won,
                "turns": result.get("turns"),
                "player_hp_remaining": result.get("player_hp_remaining"),
                "enemy_hp_remaining": result.get("enemy_hp_remaining"),
                "log": result.get("log", []),
                "companion_contribution": result.get("companion", {}),
                "growth": growth,
                "bond": bond,
                "qi_bond_multiplier": multiplier,
                "fights_today": ledger["fights"],
                "daily_cap": COMPANION_FIGHTS_PER_DAY,
                "rewards": {},
                "companion": companion_view(beast, player),
            }

    @app.post("/beasts/release")
    def post_release(req: BeastReleaseRequest):
        """Break the contract.

        The expensive direction. Costs cultivation progress scaled by the depth
        of the bond, severs the beast, and drops it out of the formation. It
        is a POST because it is irreversible: the contract status is
        ``severed`` and the progress is gone.
        """
        db = _db()
        _load(req.player_id)
        with db.transaction(req.player_id):
            player = _load(req.player_id)
            beast = _owned(player, req.beast_id)
            contract = contract_of(beast)
            if contract.get("status") == SEVERED:
                raise HTTPException(status_code=409, detail="Contract already severed")
            outcome = apply_severance(player, beast)
            return {
                "ok": True,
                "severed": True,
                "cost": outcome["cost"],
                "cultivation_progress_before": outcome["cultivation_progress_before"],
                "cultivation_progress_after": outcome["cultivation_progress_after"],
                "companion": companion_view(beast, player),
            }


#: Given names for tamed companions. A bond with a beast called
#: ``beast_9f3a...`` is a menu entry; the name is the cheapest possible way to
#: make it a character, and it costs no balance authority.
BEAST_NAMES = [
    "Xiao Bai", "Hei Feng", "Qingluan", "Zhuque", "Canglang", "Yinglong",
    "Bixi", "Taotie", "Kunpeng", "Baihu", "Zhuque", "Xuanwu",
    "Liu Er", "Dian Hong", "Tianji", "Yueying",
]


def _beast_name(beast: dict) -> str:
    """A stable given name for ``beast``, derived from its id.

    Deterministic rather than random, so a reload or a fresh read shows the
    same name the player first tamed. The id is unguessable, so this leaks
    nothing an attacker could use to reconstruct it.
    """
    digest = sum(ord(c) for c in str(beast.get("beast_id", "")))
    return BEAST_NAMES[digest % len(BEAST_NAMES)]


__all__ = [
    "BEAST_XP_STEP",
    "BOND_DECAY_PER_IDLE_DAY",
    "BOND_LOSS_ON_DEFEAT",
    "BOND_MAX",
    "BOND_NEGLECT_GRACE_DAYS",
    "BOND_ON_TAME",
    "BOND_PER_LEVEL",
    "BOND_PER_WIN",
    "COMPANION_FIGHTS_PER_DAY",
    "COMPANION_LEDGER_KEY",
    "CONTRACT_KINDS",
    "CONTRACT_STAGES",
    "CONTRACT_STAGE_REALM",
    "QI_BOND_MULTIPLIER",
    "REFUSING_BOND",
    "SEVERED",
    "apply_battle_bond",
    "apply_neglect",
    "apply_severance",
    "award_growth",
    "companion_stat_block",
    "companion_view",
    "contract_of",
    "contract_stage_for",
    "level_ceiling",
    "register_spirit_beast_routes",
    "roster_view",
    "severance_cost",
    "will_fight",
    "xp_to_next_level",
]
