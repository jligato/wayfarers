"""
Wayfarer's Cycle — Unified Progression Ladders: the ENGINE.

WHAT THIS IS
------------
One place that answers "where is this player on every progression ladder, and
what have they earned?". It is a PURE module: no FastAPI, no store, no request,
no clock of its own. Every function takes the player row and, where a time
matters, an explicit ``now``. That split is the house style (``stamina.py``,
``daily_streak.py``) and it is what makes the arithmetic testable without a
database, a session, or a wall clock.

THE FOUR LADDERS AND WHY THEY ARE NOT INVENTED HERE
----------------------------------------------------
The unified ladder does not define any progression. It READS four ladders that
already exist and already have their own owners, and joins them:

    axis            owner                        what a step is
    ---------------  ---------------------------  ------------------------
    cultivation     cultivation_system.py        realm x stage
    temper          equipment_config.py          highest gear quality owned
    leaderboard     leaderboard_rewards.py       the paying band you sit in
    codex           codex.py                     bestiary completion percent

Each number is imported from its owner, never retyped. The reconciliation note
(``design/progression-ladders-reconciliation.md``) measured ELEVEN disagreeing
copies of the realm ladder in this repo; a twelfth, written here, would be the
same defect wearing a new hat. So this module calls
``combat_config.calculate_realm_multiplier`` and ``equipment_config``'s own
tables rather than holding a list of realm names. The only realm-name literal it
contains is in a docstring.

THE ANTI-ABUSE SHAPE
--------------------
Three separate doors, three separate guards, and they are deliberately not the
same guard:

1. **Milestone rewards pay once.** Guarded by the ``milestone_claim_ledger``
   table (migration 010) as a PRIMARY KEY, enforced by
   ``progression_ledger.reserve_claim`` with ``ON CONFLICT DO NOTHING``. The
   claim key is SERVER-DERIVED. A caller cannot name a key that has not been
   paid, and a retry with a fresh Idempotency-Key still loses.

2. **Catch-up has diminishing returns.** Guarded here, in pure arithmetic:
   :func:`catchup_multiplier` is a decreasing function of how many catch-up
   payouts the player has already taken on that axis. It is bounded below by
   :data:`CATCHUP_FLOOR`, so the twentieth absence pays 5% of the first. The
   counter it reads is monotonically increasing and survives a prestige, so the
   curve cannot be reset by resetting the player.

3. **Client-supplied progress is inert.** The claim request model is declared
   with ``extra="forbid"`` and carries ONLY ``milestone_id``. There is no field
   a caller could send that would move a step, a reward, or an amount — the
   numbers are looked up in this module's tables by a server-resolved id. This
   is the ``extra="forbid"`` argument from ``tournament_routes.py``: silently
   dropping an unknown field looks like honouring it.

THE STAT RESOLVER
-----------------
:func:`resolve_unified_stats` is the ONE function that turns a player's ladder
position into the numbers a fight uses. It multiplies, it does not add, for the
same reason ``loadout_resolver.py`` does: two 10% bonuses added become a hidden
20%, while multiplied they become a legible 21% the player can predict. It
reads no request field, so a client cannot name its own multiplier.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

# Sibling modules, imported BARE. ``server/`` is on sys.path as a plain
# directory (that is how main.py imports them), and the repository-root
# conftest.py repairs duplicate module identity for the whole session. A
# package-qualified ``from server import combat_config`` here would create a
# SECOND copy of the balance tables on any tree where the repair is not loaded,
# and the copy could then disagree with the one combat uses.
import combat_config
import equipment_config
import leaderboard_rewards
import prestige_reset

from qi_rewards import grant_qi

DAY_SECONDS: int = 86400

# ---------------------------------------------------------------------------
# The axis catalogue
# ---------------------------------------------------------------------------

#: Every ladder this module unifies, in display order. ``source_module`` is
#: asserted to exist by ``test_progression_ladders.py`` — a typo in a module
#: name is otherwise invisible until a screen goes blank.
AXES: Tuple[Dict[str, Any], ...] = (
    {
        "axis_key": "cultivation",
        "display_name": "Cultivation",
        "sort_order": 0,
        "description": "Realm and stage. The ladder everything else is paced by.",
        "source_module": "cultivation_system",
        "resets_on_prestige": True,
    },
    {
        "axis_key": "temper",
        "display_name": "Tempering",
        "sort_order": 1,
        "description": "Highest equipment quality you have earned by tempering.",
        "source_module": "equipment_config",
        "resets_on_prestige": True,
    },
    {
        "axis_key": "leaderboard",
        "display_name": "Standing",
        "sort_order": 2,
        "description": "The paying leaderboard band you currently sit in.",
        "source_module": "leaderboard_rewards",
        "resets_on_prestige": False,
    },
    {
        "axis_key": "codex",
        "display_name": "Bestiary Codex",
        "sort_order": 3,
        "description": "Share of the bestiary you have recorded.",
        "source_module": "codex",
        "resets_on_prestige": False,
    },
)

AXIS_KEYS: Tuple[str, ...] = tuple(str(a["axis_key"]) for a in AXES)


class ProgressionError(ValueError):
    """Refusal carrying the HTTP status the route should answer with."""


def axis_by_key(axis_key: str) -> Dict[str, Any]:
    for axis in AXES:
        if axis["axis_key"] == axis_key:
            return dict(axis)
    raise ProgressionError(f"unknown progression axis {axis_key!r}")


# ---------------------------------------------------------------------------
# Requirement kinds — the vocabulary a milestone row may use
# ---------------------------------------------------------------------------

#: Every key a ``progression_milestone.requirement`` blob may carry. An unknown
#: key raises rather than defaulting: a milestone whose requirement cannot be
#: evaluated must be a startup failure, not a reward nobody can ever claim or a
#: reward everybody can claim instantly. The reconciliation note's rule, applied
#: to requirements.
REQUIREMENT_KINDS: Tuple[str, ...] = (
    "axis_step",      # the named axis is at or past N steps
    "realm_multiplier",  # combat power is at or past N
    "codex_completion",  # bestiary percent is at or past N
    "temper_quality",    # owns at least this quality
)

#: The reward keys a milestone may pay. A fixed set, so a hand-edited table row
#: cannot invent a currency that no creditor knows how to grant.
REWARD_KEYS: Tuple[str, ...] = ("qi", "insight")


def _require_kind(requirement: Dict[str, Any]) -> str:
    kind = str(requirement.get("kind", ""))
    if kind not in REQUIREMENT_KINDS:
        raise ProgressionError(
            f"unknown milestone requirement kind {kind!r}; "
            f"expected one of {list(REQUIREMENT_KINDS)}"
        )
    return kind


def validate_reward(reward: Dict[str, Any]) -> Dict[str, Any]:
    """Reject a reward carrying a key this module cannot pay.

    Called on the way IN (when a table row is loaded) and never skipped, because
    a reward dict is exactly the kind of thing an LLM-authored table row gets
    subtly wrong, and a silently dropped key pays a player less than the design
    promised without ever saying so.
    """
    if not isinstance(reward, dict):
        raise ProgressionError("milestone reward must be an object")
    unknown = sorted(set(reward) - set(REWARD_KEYS))
    if unknown:
        raise ProgressionError(
            f"milestone reward carries unpayable keys {unknown}; "
            f"payable keys are {list(REWARD_KEYS)}"
        )
    out: Dict[str, float] = {}
    for key, value in reward.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProgressionError(f"milestone reward {key!r} must be a number")
        if value < 0:
            raise ProgressionError(f"milestone reward {key!r} cannot be negative")
        # The same recovery ``pay_reward`` applies to a stored insight total,
        # and for the same two reasons. OverflowError is caught too because
        # float() on an integer too large for a double raises "int too large
        # to convert to float", which is neither a TypeError nor a ValueError.
        # And the finite check is not redundant with it: float() does NOT
        # raise on an infinity, it RETURNS inf, and float('nan') compares
        # false against 0 so it also cleared the negativity test above. Either
        # is corrupt table data rather than a large amount, so it pays 0.0 --
        # the same answer an unreadable amount gets -- instead of a credit
        # the player can never spend. Recovering rather than refusing is also
        # what keeps one bad row from 500-ing claimable_milestones, which
        # validates EVERY catalogue row.
        try:
            amount = float(value)
        except (TypeError, ValueError, OverflowError):
            amount = 0.0
        if not math.isfinite(amount):
            amount = 0.0
        out[key] = amount
    return out


# ---------------------------------------------------------------------------
# The milestone catalogue
# ---------------------------------------------------------------------------
#
# Written as DATA here rather than in the database because the game also runs
# with no database at all (``wallet_dispatch.USE_PG`` is the same signal), and a
# feature that only exists when PostgreSQL happens to be configured is a feature
# that silently does not exist in a test run. ``sync_catalogue_to_db`` writes
# these same rows into the table so the database and the code cannot disagree:
# the module is the source, the table is the durable copy, and
# ``test_progression_ladders.py`` asserts the two match.
#
# `period_kind` is 'forever' for the ladder rungs — reaching a realm is not a
# daily event — and 'daily'/'weekly' for the catch-up rows below.

MILESTONES: Tuple[Dict[str, Any], ...] = (
    # --- cultivation: realm rungs, once each, ever -------------------------
    {
        "milestone_id": "cultivation_step_1",
        "axis_key": "cultivation",
        "step_index": 1,
        "display_name": "First Steps",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 1},
        "reward": {"qi": 50.0},
        "period_kind": "forever",
        "is_catchup": False,
    },
    {
        "milestone_id": "cultivation_step_3",
        "axis_key": "cultivation",
        "step_index": 3,
        "display_name": "Established",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 3},
        "reward": {"qi": 400.0, "insight": 0.01},
        "period_kind": "forever",
        "is_catchup": False,
    },
    {
        "milestone_id": "cultivation_step_5",
        "axis_key": "cultivation",
        "step_index": 5,
        "display_name": "Transcendent",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 5},
        "reward": {"qi": 2000.0, "insight": 0.02},
        "period_kind": "forever",
        "is_catchup": False,
    },
    # --- temper: quality rungs ---------------------------------------------
    {
        "milestone_id": "temper_step_1",
        "axis_key": "temper",
        "step_index": 1,
        "display_name": "Refined Edge",
        "requirement": {"kind": "temper_quality", "quality": "rare"},
        "reward": {"qi": 120.0},
        "period_kind": "forever",
        "is_catchup": False,
    },
    {
        "milestone_id": "temper_step_2",
        "axis_key": "temper",
        "step_index": 2,
        "display_name": "Sacred Temper",
        "requirement": {"kind": "temper_quality", "quality": "legendary"},
        "reward": {"qi": 900.0, "insight": 0.01},
        "period_kind": "forever",
        "is_catchup": False,
    },
    # --- leaderboard: band rungs -------------------------------------------
    {
        "milestone_id": "leaderboard_step_1",
        "axis_key": "leaderboard",
        "step_index": 1,
        "display_name": "Top of the Board",
        "requirement": {"kind": "axis_step", "axis_key": "leaderboard", "step": 1},
        "reward": {"qi": 500.0},
        "period_kind": "forever",
        "is_catchup": False,
    },
    # --- codex: completion rungs -------------------------------------------
    {
        "milestone_id": "codex_step_1",
        "axis_key": "codex",
        "step_index": 1,
        "display_name": "Archivist",
        "requirement": {"kind": "codex_completion", "percent": 25.0},
        "reward": {"qi": 300.0},
        "period_kind": "forever",
        "is_catchup": False,
    },
    # --- the catch-up rows: daily, weekly, and the realm pill --------------
    {
        "milestone_id": "catchup_daily",
        "axis_key": "cultivation",
        "step_index": 0,
        "display_name": "Daily Catch-up",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 0},
        "reward": {"qi": 60.0},
        "period_kind": "daily",
        "is_catchup": True,
    },
    {
        "milestone_id": "catchup_weekly",
        "axis_key": "cultivation",
        "step_index": 0,
        "display_name": "Weekly Catch-up",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 0},
        "reward": {"qi": 500.0},
        "period_kind": "weekly",
        "is_catchup": True,
    },
    {
        "milestone_id": "catchup_realm_pill",
        "axis_key": "cultivation",
        "step_index": 0,
        "display_name": "Realm Catch-up Pill",
        "requirement": {"kind": "axis_step", "axis_key": "cultivation", "step": 0},
        "reward": {"qi": 250.0},
        "period_kind": "daily",
        "is_catchup": True,
    },
)

MILESTONES_BY_ID: Dict[str, Dict[str, Any]] = {m["milestone_id"]: dict(m) for m in MILESTONES}


def milestone_by_id(milestone_id: str) -> Dict[str, Any]:
    row = MILESTONES_BY_ID.get(str(milestone_id))
    if row is None:
        raise ProgressionError(f"unknown milestone {milestone_id!r}")
    return dict(row)


# ---------------------------------------------------------------------------
# Where the player stands on each axis
# ---------------------------------------------------------------------------

#: The temper ladder, ordered worst to best. Derived from equipment_config's own
#: quality list rather than retyped, then reversed, so a new quality added there
#: shows up here without a second edit. ``QUALITY_GRADE`` maps each quality to a
#: forge grade, and the grades are the ladder the design talks about.
_TEMPER_ORDER: Tuple[str, ...] = tuple(
    sorted(
        equipment_config.QUALITY_GRADE,
        key=lambda q: (
            list(equipment_config.QUALITY_GRADE.values()).index(
                equipment_config.QUALITY_GRADE[q]
            ),
            list(equipment_config.QUALITY_GRADE).index(q),
        ),
    )
)


def _equipment_quality_index(quality: str) -> int:
    try:
        return _TEMPER_ORDER.index(str(quality))
    except ValueError:
        # An unknown quality is the LOWEST, never a KeyError. A piece of gear
        # whose quality string is from a future version must not crash the axis
        # screen; it must simply not count toward the ladder yet.
        return -1


def cultivation_step(player: Dict[str, Any]) -> int:
    """Step on the cultivation axis: realm index x STAGE_COUNT + stage index.

    The realm and stage NAMES are resolved through the owners' own ladders, so
    this function cannot invent a thirteenth spelling. An unknown realm lands at
    step 0 (mortal) rather than raising, because a player row written by a
    future version must still be readable by today's screen.
    """
    from cultivation_system import Realm, Stage  # noqa: WPS433 - lazy, circular-safe

    realm = str(player.get("cultivation_realm", "mortal"))
    stage = str(player.get("cultivation_stage", "early"))
    try:
        realm_index = list(Realm).index(Realm(realm))
    except ValueError:
        realm_index = 0
    try:
        stage_index = list(Stage).index(Stage(stage))
    except ValueError:
        stage_index = 0
    return realm_index * len(list(Stage)) + stage_index


def temper_step(player: Dict[str, Any]) -> int:
    """Step on the temper axis: index of the best quality the player owns.

    Read from the player's OWN equipment records. It never grants and never
    rolls — a milestone screen that could hand out the gear it is measuring
    would be a faucet, so the quality has to already be on the player.
    """
    best = -1
    block = player.get("equipment")
    records: List[Dict[str, Any]] = []
    if isinstance(block, dict):
        # Either a {item_id: record} map or a bare list of records.
        values = block.values() if all(isinstance(v, dict) for v in block.values()) else []
        records.extend(v for v in values if isinstance(v, dict))
    elif isinstance(block, list):
        records.extend(v for v in block if isinstance(v, dict))

    artifacts = player.get("artifacts")
    if isinstance(artifacts, dict):
        for value in artifacts.values():
            if isinstance(value, dict) and value.get("switched_on"):
                records.append({"quality": value.get("quality")})
    elif isinstance(artifacts, list):
        records.extend(
            v for v in artifacts
            if isinstance(v, dict) and v.get("switched_on")
        )

    for record in records:
        index = _equipment_quality_index(record.get("quality"))
        if index > best:
            best = index
    return max(0, best)


def leaderboard_step(player: Dict[str, Any]) -> int:
    """Step on the leaderboard axis: 1 for a paying band, else 0.

    Derived from the player's OWN stored rank, read through the reward module's
    own band boundaries, so a band that stops paying demotes the player on this
    screen automatically. A player with no rank is unranked, i.e. step 0.
    """
    rank = player.get("leaderboard_rank")
    if rank is None or rank == "unranked":
        return 0
    try:
        rank_int = int(rank)
    except (TypeError, ValueError, OverflowError):
        # OverflowError too: a stored rank of float('inf') makes int() raise
        # "cannot convert float infinity to integer", which is neither a
        # TypeError nor a ValueError and used to escape as a 500. An
        # unreadable rank is unranked, which is step 0 — the same answer the
        # line above already gives a missing one.
        return 0
    return 1 if leaderboard_rewards.band_for_rank(rank_int) != leaderboard_rewards.BAND_NONE else 0


def codex_total() -> int:
    """How many monsters exist, read from the codex's own manager.

    A module-level import of ``codex`` would be circular (codex imports the
    store, and the store is what hands us the player), so it is lazy. A failure
    to reach the manager yields 0, which makes every codex milestone
    unsatisfiable rather than falsely satisfied — the safe direction, and the
    one the reconciliation note's "make the ranker raise" rule is arguing for
    in a different register.
    """
    try:
        import codex as codex_module

        return len(codex_module._monster_manager().get_monster_ids())
    except Exception:  # noqa: BLE001 - an unreachable manager must not crash the axis
        return 0


def codex_step(player: Dict[str, Any]) -> int:
    """Step on the codex axis: bestiary completion, as a whole percent 0-100.

    Percent rather than a fine-grained count because the ladder rung is
    "Archivist" at 25%, not "the 4th monster" — the milestone is a milestone,
    and a per-monster axis would make every codex entry a payout.

    The count comes from the codex module's own ``read_codex`` normaliser, so a
    damaged codex block reads 0 rather than crashing the ladder. The total comes
    from the codex's own monster manager, so a milestone is never satisfied
    against a hard-coded denominator that drifts when content ships.
    """
    stored = player.get("codex_completion_percent")
    if stored is not None:
        try:
            return max(0, min(100, int(round(float(stored)))))
        except (TypeError, ValueError, OverflowError):
            # OverflowError as well, and it fires at two different points in
            # this one expression: float() on an integer too large for a
            # double raises "int too large to convert to float", and float()
            # on a stored infinity yields inf, which int() then rejects with
            # "cannot convert float infinity to integer". Neither is a
            # TypeError or a ValueError, so both escaped the old except and
            # 500'd the ladder screen. 0% is the honest reading of a
            # completion figure that cannot be parsed.
            return 0
    try:
        import codex as codex_module

        seen = len(codex_module.read_codex(player)["seen"])
    except Exception:  # noqa: BLE001 - a damaged block is 0% complete
        return 0
    total = codex_total()
    if total <= 0:
        return 0
    return max(0, min(100, int(round(100.0 * seen / total))))


#: axis_key -> the pure function that reads that axis off a player row. This is
#: the indirection that makes the ladder UNIFIED: a screen asks for every axis
#: by key and does not need to know that one of them reads a realm enum and
#: another reads a list of gear dicts.
AXIS_READERS = {
    "cultivation": cultivation_step,
    "temper": temper_step,
    "leaderboard": leaderboard_step,
    "codex": codex_step,
}


def axis_steps(player: Dict[str, Any]) -> Dict[str, int]:
    """Every axis's current step, in catalogue order."""
    return {key: AXIS_READERS[key](player) for key in AXIS_KEYS}


def requirement_met(player: Dict[str, Any], requirement: Dict[str, Any]) -> bool:
    """Is this milestone's requirement satisfied by the player's OWN state?"""
    kind = _require_kind(requirement)
    if kind == "axis_step":
        axis_key = str(requirement.get("axis_key", ""))
        reader = AXIS_READERS.get(axis_key)
        if reader is None:
            raise ProgressionError(
                f"milestone requirement names unknown axis {axis_key!r}"
            )
        return reader(player) >= _finite_int(requirement.get("step", 0), 0)
    if kind == "realm_multiplier":
        return player_realm_multiplier(player) >= _finite_or_zero(
            requirement.get("min", 0.0)
        )
    if kind == "codex_completion":
        return codex_step(player) >= _finite_or_zero(requirement.get("percent", 0.0))
    if kind == "temper_quality":
        quality = str(requirement.get("quality", ""))
        want = _equipment_quality_index(quality)
        if want < 0:
            # A REQUIREMENT naming an unknown quality is a misconfigured
            # milestone, not a lenient one. Refuse it the way an unknown
            # ``kind`` or ``axis_key`` is refused. Flooring it to 0 instead
            # made a typo silently always-claimable by every player, paying out
            # a reward no one was ever meant to earn. The player's OWN gear
            # still reads tolerantly through ``temper_step`` -- that -1 is
            # about future-version data, not about our own config.
            raise ProgressionError(
                f"milestone requirement names unknown temper quality {quality!r}"
            )
        return temper_step(player) >= want
    raise ProgressionError(f"unhandled requirement kind {kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Period keys — server-derived, never from the request
# ---------------------------------------------------------------------------

def _finite_or_zero(value: Any) -> float:
    """``float(value)`` that answers 0.0 for anything non-finite or unconvertible.

    One helper for every numeric conversion in this module, because the failure
    is always the same three-part shape and each site used to catch a different
    subset of it:

    1. ``float()`` raises TypeError/ValueError on junk.
    2. ``float()`` on an integer too large for a double raises OverflowError --
       neither a TypeError nor a ValueError, so it escaped an except clause
       catching only the first two.
    3. ``float()`` does NOT raise on an infinity: it RETURNS inf. And
       ``float('nan')`` compares false against 0, so it also clears any
       ``< 0`` or ``> 0`` test written above it. Only ``math.isfinite`` sees
       either one.

    Point 3 is why a try/except alone was never enough here: a stored infinity
    writes straight through the except clause and poisons every later read.
    Recovering to 0.0 is the same answer an unreadable value already got, so a
    corrupt row cannot pay more than a missing one.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _finite_int(value: Any, default: int = 0) -> int:
    """``int(value)`` for a count, answering ``default`` for non-finite input.

    Same three-part failure as :func:`_finite_or_zero`, and it matters more
    here: ``int(inf)`` raises OverflowError and ``int(nan)`` raises ValueError,
    so an unguarded counter 500s the ladder screen. A count that cannot be read
    is a zero count, which is the reading a missing counter already got.
    """
    number = _finite_or_zero(value)
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def utc_day_index(now: float) -> int:
    """Integer UTC day. Not a timestamp: a timestamp key breaks at midnight and
    under DST, and "elapsed > 0" breaks under a same-second re-press.

    Non-finite ``now`` recovers to day 0. Both failure modes escape the obvious
    guard: ``int(inf // 86400)`` is ``int(nan)`` because floor-division an
    infinity yields nan, so it raises ValueError, not OverflowError.
    """
    return _finite_int(_finite_or_zero(now) // DAY_SECONDS, 0)


def iso_week_key(now: float) -> str:
    """``YYYY-Www``. ISO, not a day count, so the weekly boundary is the one
    the rest of the server's weekly rewards already use.

    ``time.gmtime`` refuses a non-finite or out-of-range stamp with
    OverflowError, which is neither a TypeError nor a ValueError. An
    unreadable clock reads as the Unix epoch's week rather than 500-ing the
    claim that asked for a period key.
    """
    seconds = _finite_or_zero(now)
    try:
        return time.strftime("%G-W%V", time.gmtime(seconds))
    except (OverflowError, OSError, ValueError):
        return time.strftime("%G-W%V", time.gmtime(0))


def period_key_for(period_kind: str, now: float) -> str:
    """The ledger key component for a milestone's period.

    'forever' -> the literal 'forever', so a once-ever milestone can never be
    re-keyed into a second payable period.
    """
    if period_kind == "daily":
        return str(utc_day_index(now))
    if period_kind == "weekly":
        return iso_week_key(now)
    if period_kind == "forever":
        return "forever"
    raise ProgressionError(f"unknown period_kind {period_kind!r}")


# ---------------------------------------------------------------------------
# Catch-up mechanics
# ---------------------------------------------------------------------------

#: The base payout rate for catch-up, per HOUR of absence, in qi.
CATCHUP_QI_PER_HOUR: float = 8.0

#: The most a single catch-up payout can be, whatever the absence length.
CATCHUP_MAX_QI: float = 600.0

#: Absence longer than this is treated as "this long" — an absence measured in
#: months is a player who left, and paying it in full would be a bigger reward
#: for quitting than for playing.
CATCHUP_MAX_ABSENCE_HOURS: float = 72.0

#: Each successive catch-up payout is worth this fraction of the one before.
#: 0.5 means the 2nd pays half, the 3rd a quarter, the 4th an eighth.
CATCHUP_DECAY: float = 0.5

#: The floor the decay curve cannot fall below, as a fraction of the FULL
#: payout. The twentieth absence still pays 5% — never zero, because a player
#: who gets nothing stops opening the game, which is the opposite of what
#: catch-up is for.
CATCHUP_FLOOR: float = 0.05

#: No catch-up for absences shorter than this. Otherwise every page refresh
#: would be a claim.
CATCHUP_MIN_ABSENCE_HOURS: float = 1.0


def catchup_multiplier(prior_claims: int) -> float:
    """The diminishing-returns factor for the Nth catch-up payout.

    Strictly decreasing in ``prior_claims``, bounded below by
    :data:`CATCHUP_FLOOR`, and a pure function of the counter. A negative
    counter (a corrupt row) is treated as zero rather than as a bonus.
    """
    n = max(0, _finite_int(prior_claims, 0))
    return max(CATCHUP_FLOOR, CATCHUP_DECAY ** n)


def catchup_absence_hours(away_seconds: float) -> float:
    """Absence length in hours, clamped to ``[0, CATCHUP_MAX_ABSENCE_HOURS]``.

    Non-finite recovers to 0.0, which reads as "not away" and so pays nothing.
    That direction matters: NaN is the one value that would otherwise sail
    through the ``< 0`` test below (every comparison against NaN is False), skip
    the ``min()`` clamp, and reach the reply as a literal ``NaN`` -- which is
    not valid JSON and takes down the whole response, not just one field.
    """
    if away_seconds is None:
        return 0.0
    seconds = _finite_or_zero(away_seconds)
    if seconds < 0:
        return 0.0
    return min(seconds / 3600.0, CATCHUP_MAX_ABSENCE_HOURS)


def evaluate_catchup(
    away_seconds: float,
    prior_claims: int,
    now: float,
    *,
    base_qi: Optional[float] = None,
) -> Dict[str, Any]:
    """What a catch-up payout is worth, and why. Pure.

    Returns a dict that always carries the intermediate numbers, so a screen
    can show a player WHY the payout is small ("you have been away 3 hours, and
    this is your 9th catch-up") instead of just the total. A player who cannot
    see the reason will read a shrinking reward as a bug.
    """
    hours = catchup_absence_hours(away_seconds)
    # Normalise the counter ONCE, here, and reuse ``claims`` everywhere below.
    # A null or negative count from a damaged ledger row is a zero, not a
    # crash: doing the ``or 0`` per-use left ``next_multiplier`` computing
    # ``None + 1`` before anyone had normalised it, so a null counter escaped
    # as a bare TypeError instead of being priced as a first claim.
    claims = max(0, _finite_int(prior_claims, 0))
    multiplier = catchup_multiplier(claims)
    if base_qi is None:
        full = min(CATCHUP_MAX_QI, hours * CATCHUP_QI_PER_HOUR)
    else:
        # An explicit base is deliberately NOT capped by CATCHUP_MAX_QI -- it
        # sizes the payout without being able to buy eligibility. It does need
        # the finite guard: a non-finite one used to reach the reply as `inf`
        # with eligible=True, an amount the row could never honour, inside a
        # body that is not valid JSON.
        full = _finite_or_zero(base_qi)
    amount = round(full * multiplier, 2)
    eligible = hours >= CATCHUP_MIN_ABSENCE_HOURS and amount > 0.0
    return {
        "ok": True,
        "eligible": bool(eligible),
        "absence_hours": round(hours, 3),
        "full_value_qi": round(full, 2),
        "decay_multiplier": round(multiplier, 6),
        "amount_qi": amount if eligible else 0.0,
        "prior_claims": claims,
        "next_multiplier": round(catchup_multiplier(claims + 1), 6),
        "period_key": period_key_for("daily", now),
        # Named so a client can show the cap rather than leaving the player to
        # wonder why a month away paid the same as a day away.
        "hours_capped": bool(
            away_seconds is not None
            and away_seconds > CATCHUP_MAX_ABSENCE_HOURS * 3600.0
        ),
    }


# ---------------------------------------------------------------------------
# The unified stat resolver
# ---------------------------------------------------------------------------

def player_realm_multiplier(player: Dict[str, Any]) -> float:
    """Combat power from the cultivation ladder, via its owner.

    Delegates to ``combat_config.calculate_realm_multiplier`` rather than
    multiplying the tables here, so this module cannot become the twelfth
    disagreeing copy of the realm ladder.
    """
    return float(
        combat_config.calculate_realm_multiplier(
            str(player.get("cultivation_realm", "mortal")),
            str(player.get("cultivation_stage", "early")),
        )
    )


def resolve_unified_stats(player: Dict[str, Any]) -> Dict[str, Any]:
    """THE one function that turns ladder position into fight numbers.

    Multiplies, never adds, and reads nothing from a request. The factors, in
    the order they are applied:

    1. the cultivation ladder's own realm x stage multiplier (owner: combat_config)
    2. the prestige insight bonus, which is a risk reduction rather than a
       damage multiplier (``prestige_reset.insight_bonus`` already caps it)
    3. the milestone ladder bonus, below

    The result is deliberately NOT written back onto the player row. Nothing
    else in the server is supposed to be reading a cached "unified multiplier"
    field, because the day one does, the day it goes stale after a prestige and
    nothing notices.
    """
    realm_mult = _finite_or_zero(player_realm_multiplier(player))
    insight = _finite_or_zero(prestige_reset.insight_bonus(player))
    milestone_bonus = _finite_or_zero(player.get("progression_milestone_bonus", 0.0))
    if milestone_bonus < 0.0:
        # A corrupt or hand-edited row must not be able to make a player WEAKER
        # than the realm ladder alone says. Clamped, not rejected: the value is
        # a display multiplier, and refusing to render the screen over a
        # negative float is a worse failure than showing 0% milestone bonus.
        milestone_bonus = 0.0

    ladder_multiplier = (1.0 + insight) * (1.0 + milestone_bonus)
    return {
        "ok": True,
        "realm_multiplier": round(realm_mult, 6),
        "insight_bonus": round(insight, 6),
        "milestone_bonus": round(milestone_bonus, 6),
        # The number a fight should use.
        "effective_multiplier": round(realm_mult * ladder_multiplier, 6),
        "steps": axis_steps(player),
        # The prestige bonus is a reduction in breakthrough RISK, not damage, so
        # it is reported separately and a screen can label it correctly.
        "breakthrough_risk_reduction": round(insight, 6),
    }


# ---------------------------------------------------------------------------
# The claim itself
# ---------------------------------------------------------------------------

def pay_reward(player: Dict[str, Any], reward: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a validated reward to the player row. Returns what was paid.

    Every qi credit goes through ``qi_rewards.grant_qi``, which writes the field
    the breakthrough gate actually reads. Crediting ``player["qi"]`` directly
    is the defect that module exists to stop: the UI moves and the gate does
    not.
    """
    reward = validate_reward(reward)
    qi = float(reward.get("qi", 0.0))
    if qi:
        player["progress"] = grant_qi(player, qi)
    insight = float(reward.get("insight", 0.0))
    if insight:
        # Written by hand rather than via prestige_reset's private grant: that
        # one is deliberately module-private to the reset. Same field, same
        # cap semantics — the CAP is applied when the bonus is READ, in
        # insight_bonus(), so a stored total may exceed it and stays auditable.
        try:
            current = float(player.get("dao_insight", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            # OverflowError too: float() on an integer too large for a double
            # raises "int too large to convert to float", which is neither a
            # TypeError nor a ValueError and used to escape as a 500 out of
            # the claim.
            current = 0.0
        # The clamp, and it is needed because float() does NOT raise on a
        # stored float('inf') — it returns inf, so the except above never
        # fires and `current + insight` would write a non-finite total back
        # onto the player row. A stored infinity (or NaN) is a corrupt
        # total, not a large one, so it recovers to 0.0 exactly like an
        # unreadable one. The CAP is deliberately NOT applied here: this
        # total stays auditable above it, which is the behaviour the
        # comment above promises. Every consumer is already safe against a
        # finite value because insight_bonus() clamps on READ.
        if not math.isfinite(current):
            current = 0.0
        player["dao_insight"] = current + insight
    return {"qi": qi, "insight": insight}


def claimable_milestones(
    player: Dict[str, Any], now: float, claimed_keys: Optional[set] = None
) -> List[Dict[str, Any]]:
    """Which milestones this player could claim right now, and has not claimed.

    ``claimed_keys`` is the set of ``(milestone_id, period_key)`` the ledger
    already holds. The route fills it from the database; the pure unit test
    passes a literal set. A milestone whose requirement is not met is simply
    absent rather than listed as locked, because this function answers "what
    can I press", and the screen builds the locked list from the catalogue.
    """
    claimed_keys = claimed_keys or set()
    out: List[Dict[str, Any]] = []
    for milestone in MILESTONES:
        key = (milestone["milestone_id"], period_key_for(milestone["period_kind"], now))
        if key in claimed_keys:
            continue
        if not requirement_met(player, milestone["requirement"]):
            continue
        out.append(
            {
                "milestone_id": milestone["milestone_id"],
                "axis_key": milestone["axis_key"],
                "step_index": milestone["step_index"],
                "display_name": milestone["display_name"],
                "reward": validate_reward(milestone["reward"]),
                "period_kind": milestone["period_kind"],
                "is_catchup": bool(milestone["is_catchup"]),
                "period_key": key[1],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Database catalogue sync
# ---------------------------------------------------------------------------

_CATALOGUE_UPSERT = (
    "INSERT INTO progression_axis "
    "(axis_key, display_name, sort_order, description, source_module, resets_on_prestige) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (axis_key) DO UPDATE SET "
    "display_name = EXCLUDED.display_name, sort_order = EXCLUDED.sort_order, "
    "description = EXCLUDED.description, source_module = EXCLUDED.source_module, "
    "resets_on_prestige = EXCLUDED.resets_on_prestige"
)

_MILESTONE_UPSERT = (
    "INSERT INTO progression_milestone "
    "(milestone_id, axis_key, step_index, display_name, requirement, reward, period_kind, is_catchup) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (milestone_id) DO UPDATE SET "
    "axis_key = EXCLUDED.axis_key, step_index = EXCLUDED.step_index, "
    "display_name = EXCLUDED.display_name, requirement = EXCLUDED.requirement, "
    "reward = EXCLUDED.reward, period_kind = EXCLUDED.period_kind, "
    "is_catchup = EXCLUDED.is_catchup"
)


def sync_catalogue_to_db(players_db: Any) -> int:
    """Write this module's axes and milestones into the database tables.

    Upsert, not insert, so a re-run after a balance edit is a no-op rather than
    a duplicate-key crash. Returns the number of rows written, and does
    nothing at all when no database is configured — the game runs fine from
    memory and must not need PostgreSQL to show a ladder.
    """
    conn = getattr(players_db, "current_connection", None)
    if conn is None:
        return 0
    import json

    written = 0
    with conn.cursor() as cur:
        for axis in AXES:
            cur.execute(
                _CATALOGUE_UPSERT,
                (
                    axis["axis_key"], axis["display_name"], int(axis["sort_order"]),
                    axis["description"], axis["source_module"],
                    bool(axis["resets_on_prestige"]),
                ),
            )
            written += 1
        for milestone in MILESTONES:
            # validate_reward runs here so an unpayable row fails the SYNC
            # rather than surfacing later as a claim that quietly pays less.
            reward = validate_reward(milestone["reward"])
            _require_kind(milestone["requirement"])
            cur.execute(
                _MILESTONE_UPSERT,
                (
                    milestone["milestone_id"], milestone["axis_key"],
                    int(milestone["step_index"]), milestone["display_name"],
                    json.dumps(milestone["requirement"]), json.dumps(reward),
                    milestone["period_kind"], bool(milestone["is_catchup"]),
                ),
            )
            written += 1
    return written
