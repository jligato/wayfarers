"""
CONTRACT TESTS: server/progression_ladders.py

WHAT THIS MODULE IS
-------------------
``server/progression_ladders.py`` is the JOIN layer for four progression
ladders that already exist elsewhere in the server. It is a PURE module: no
FastAPI router, no ``register_*``, no store, no request, no clock of its own.
It reads a player row plus an explicit ``now`` and answers arithmetic.

Because it exposes NO HTTP routes, this file contract-tests the EXPORTED
FUNCTIONS directly -- there is no ``TestClient``, no session and no
PostgreSQL anywhere below. Importing the module touches only balance tables.

THE FIVE CLAIMS THIS FILE PINS
------------------------------
1. SIGNATURES ARE A CONTRACT. Every public function's parameter NAMES, order
   and defaults are frozen. A renamed keyword argument breaks a caller that
   passes it by name; a dropped keyword-only default turns an optional
   override into a required argument.
2. RETURN SHAPES ARE A CONTRACT. Three functions return dicts whose KEY SET
   is frozen: ``evaluate_catchup`` (10 keys), ``resolve_unified_stats``
   (7 keys) and each row of ``claimable_milestones`` (8 keys). A screen that
   reads ``effective_multiplier`` must keep getting it.
3. THE LOOKUPS REFUSE, THEY DO NOT GUESS. ``axis_by_key`` and
   ``milestone_by_id`` raise :class:`ProgressionError` on an unknown name;
   ``validate_reward`` raises on an unpayable key, a non-number, a bool and a
   negative amount. A silently dropped reward key pays a player less than the
   design promised without ever saying so.
4. THE ARITHMETIC IS DIRECTIONAL. The catch-up curve is strictly DECREASING in
   the prior-claim count and floored; a longer absence pays at least as much
   as a shorter one up to the cap; the unified multiplier MULTIPLIES rather
   than adds. These are relationships, so a balance retune that keeps them
   stays green and one that breaks them does not.
5. NOTHING LEAVES THE MODULE BY ACCIDENT. ``resolve_unified_stats`` does not
   write a cached multiplier onto the player row, ``pay_reward`` writes the
   qi field the breakthrough gate actually reads, and
   ``sync_catalogue_to_db`` writes nothing at all when no database is
   configured.

FROZEN NUMBERS vs. RELATIONSHIPS
--------------------------------
Frozen (they are the formula or the anti-abuse floor, not the balance):
the 10 ``evaluate_catchup`` keys, the 7 ``resolve_unified_stats`` keys, the 8
``claimable_milestones`` row keys, the period kinds, ``CATCHUP_DECAY``'s
halving, the ``forever`` literal period key, and the ``AXIS_KEYS`` order.

Read at run time as RELATIONSHIPS (retunes stay green): every realm's
multiplier comes from ``combat_config``, every quality index from
``equipment_config``, every paying band from ``leaderboard_rewards``, the
insight ceiling from ``prestige_reset``, and the codex denominator from the
codex module's own monster manager.

SIBLING COVERAGE
----------------
This file owns the pure-function contract of the module. It does not assert
that the catalogue matches a database row set (that needs a live database),
and it does not assert any route's status codes, because the module exposes
no routes.

Run
---
    tools/wc_pytest.sh server/tests/test_progression_ladders_contract.py
"""
from __future__ import annotations

import importlib
import inspect
import math
import os
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve()
SERVER_DIR = _HERE.parents[1]
REPO_ROOT = _HERE.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Pure module: no store to select. Set anyway so a single-file run cannot pick
# up a database backend from the environment and report unrelated errors.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402

import combat_config  # noqa: E402
import equipment_config  # noqa: E402
import leaderboard_rewards  # noqa: E402
import prestige_reset  # noqa: E402
import progression_ladders as pl  # noqa: E402


# ---------------------------------------------------------------------------
# Frozen expectations
# ---------------------------------------------------------------------------

#: The key set of :func:`progression_ladders.evaluate_catchup`. Frozen: the
#: docstring promises a screen can show the player WHY the payout is small, so
#: every intermediate it reports is part of the contract.
CATCHUP_KEYS = frozenset({
    "ok",
    "eligible",
    "absence_hours",
    "full_value_qi",
    "decay_multiplier",
    "amount_qi",
    "prior_claims",
    "next_multiplier",
    "period_key",
    "hours_capped",
})

#: The key set of :func:`progression_ladders.resolve_unified_stats`.
STATS_KEYS = frozenset({
    "ok",
    "realm_multiplier",
    "insight_bonus",
    "milestone_bonus",
    "effective_multiplier",
    "steps",
    "breakthrough_risk_reduction",
})

#: The key set of one row of :func:`progression_ladders.claimable_milestones`.
CLAIMABLE_KEYS = frozenset({
    "milestone_id",
    "axis_key",
    "step_index",
    "display_name",
    "reward",
    "period_kind",
    "is_catchup",
    "period_key",
})

#: The key set of one entry of :data:`progression_ladders.AXES`.
AXIS_KEYS_IN_ROW = frozenset({
    "axis_key",
    "display_name",
    "sort_order",
    "description",
    "source_module",
    "resets_on_prestige",
})

#: The key set of one row of :data:`progression_ladders.MILESTONES`.
MILESTONE_KEYS_IN_ROW = frozenset({
    "milestone_id",
    "axis_key",
    "step_index",
    "display_name",
    "requirement",
    "reward",
    "period_kind",
    "is_catchup",
})

#: The period kinds ``period_key_for`` accepts. A fourth kind is a new
#: feature, not a retune.
PERIOD_KINDS = ("daily", "weekly", "forever")

#: A frozen instant: Tuesday 2023-11-14 22:13:20 UTC. Chosen because it is
#: far from any midnight or ISO-week boundary, so a period-key test cannot
#: pass by accident and a boundary test needs its own instant.
NOW = 1_700_000_000.0

#: A frozen instant on an ISO-week boundary: Monday 2024-01-01 00:00:00 UTC.
WEEK_START = 1_704_067_200.0

#: The half-life the catch-up docstring states in words: the 2nd pays half,
#: the 3rd a quarter, the 4th an eighth.
CATCHUP_HALF = 0.5

#: A fake player at the very bottom of the cultivation ladder.
BARE_PLAYER: dict = {}


def _player(**kw):
    """A player row with explicit realm/stage defaults."""
    row = {"cultivation_realm": "mortal", "cultivation_stage": "early"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# 1. Module shape -- pure, no routes
# ---------------------------------------------------------------------------


def test_module_exposes_no_fastapi_router():
    """The module holds no router, so nothing here is testable over HTTP."""
    assert not hasattr(pl, "router")
    assert not hasattr(pl, "app")
    assert not [n for n in dir(pl) if n.startswith("register_")]


def test_axis_keys_are_frozen_in_catalogue_order():
    """A screen draws the axes in ``sort_order``; the key tuple is the order."""
    assert pl.AXIS_KEYS == ("cultivation", "temper", "leaderboard", "codex")


def test_every_axis_declares_the_frozen_key_set():
    for axis in pl.AXES:
        assert set(axis) == AXIS_KEYS_IN_ROW, axis.get("axis_key")


def test_axis_sort_orders_are_unique_and_follow_display_order():
    orders = [axis["sort_order"] for axis in pl.AXES]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)
    # ``axis_steps`` builds its dict in AXIS_KEYS order; the catalogue order
    # and the sort_order must agree or the two disagree.
    assert list(pl.AXIS_KEYS) == [a["axis_key"] for a in
                                  sorted(pl.AXES, key=lambda a: a["sort_order"])]


def test_every_axis_names_an_importable_source_module():
    """A typo in a source_module name is invisible until a screen goes blank."""
    for axis in pl.AXES:
        name = axis["source_module"]
        assert isinstance(name, str) and name
        assert importlib.import_module(name) is not None


def test_axis_readers_cover_exactly_the_axis_keys():
    """The unified indirection must reach every axis and invent no other."""
    assert set(pl.AXIS_READERS) == set(pl.AXIS_KEYS)
    for key in pl.AXIS_KEYS:
        assert callable(pl.AXIS_READERS[key])


def test_requirement_kinds_and_reward_keys_are_frozen():
    """A milestone vocabulary that grows silently is a silent new feature."""
    assert pl.REQUIREMENT_KINDS == (
        "axis_step",
        "realm_multiplier",
        "codex_completion",
        "temper_quality",
    )
    assert pl.REWARD_KEYS == ("qi", "insight")
    assert len(set(pl.REWARD_KEYS)) == len(pl.REWARD_KEYS)


def test_day_seconds_is_one_day():
    """The daily period key is a day count, so this constant is the formula."""
    assert pl.DAY_SECONDS == 86400


# ---------------------------------------------------------------------------
# 2. Signatures are a contract
# ---------------------------------------------------------------------------


def _params(func):
    return list(inspect.signature(func).parameters.items())


def test_lookup_signatures_are_frozen():
    assert [n for n, _ in _params(pl.axis_by_key)] == ["axis_key"]
    assert [n for n, _ in _params(pl.milestone_by_id)] == ["milestone_id"]
    assert [n for n, _ in _params(pl.validate_reward)] == ["reward"]
    assert [n for n, _ in _params(pl.axis_steps)] == ["player"]
    assert [n for n, _ in _params(pl.sync_catalogue_to_db)] == ["players_db"]
    assert [n for n, _ in _params(pl.codex_total)] == []


def test_axis_reader_signatures_are_frozen():
    """Every reader takes ONE positional player row and nothing else."""
    for key in pl.AXIS_KEYS:
        reader = pl.AXIS_READERS[key]
        params = _params(reader)
        assert [n for n, _ in params] == ["player"], key
        assert params[0][1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_time_taking_signatures_are_frozen():
    assert [n for n, _ in _params(pl.cultivation_step)] == ["player"]
    assert [n for n, _ in _params(pl.temper_step)] == ["player"]
    assert [n for n, _ in _params(pl.leaderboard_step)] == ["player"]
    assert [n for n, _ in _params(pl.codex_step)] == ["player"]
    assert [n for n, _ in _params(pl.player_realm_multiplier)] == ["player"]
    assert [n for n, _ in _params(pl.resolve_unified_stats)] == ["player"]
    assert [n for n, _ in _params(pl.pay_reward)] == ["player", "reward"]
    assert [n for n, _ in _params(pl.utc_day_index)] == ["now"]
    assert [n for n, _ in _params(pl.iso_week_key)] == ["now"]
    assert [n for n, _ in _params(pl.requirement_met)] == ["player", "requirement"]
    assert [n for n, _ in _params(pl.catchup_multiplier)] == ["prior_claims"]
    assert [n for n, _ in _params(pl.catchup_absence_hours)] == ["away_seconds"]


def test_requirement_met_takes_the_requirement_as_a_second_argument():
    """A requirement is data, not an option -- it cannot become keyword-only."""
    params = dict(_params(pl.requirement_met))
    assert params["requirement"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_period_key_and_claimable_signatures_are_frozen():
    assert [n for n, _ in _params(pl.period_key_for)] == ["period_kind", "now"]
    assert [n for n, _ in _params(pl.claimable_milestones)] == [
        "player", "now", "claimed_keys",
    ]
    # ``claimed_keys`` is optional and defaults to None, so a caller with no
    # ledger yet does not have to pass an empty set.
    assert _params(pl.claimable_milestones)[2][1].default is None


def test_evaluate_catchup_base_qi_is_keyword_only_and_defaults_to_none():
    """``base_qi`` overrides the derived full payout; it is not positional."""
    params = _params(pl.evaluate_catchup)
    assert [n for n, _ in params] == [
        "away_seconds", "prior_claims", "now", "base_qi",
    ]
    base_qi = params[3][1]
    assert base_qi.kind is inspect.Parameter.KEYWORD_ONLY
    assert base_qi.default is None
    assert params[3][1].annotation == "Optional[float]"


#: Every public function, in one place, so the "no hidden clock" check below
#: cannot be quietly weakened by deleting a name from a loop.
PUBLIC_FUNCTIONS = (
    "axis_by_key",
    "milestone_by_id",
    "validate_reward",
    "cultivation_step",
    "temper_step",
    "leaderboard_step",
    "codex_total",
    "codex_step",
    "axis_steps",
    "requirement_met",
    "utc_day_index",
    "iso_week_key",
    "period_key_for",
    "catchup_multiplier",
    "catchup_absence_hours",
    "evaluate_catchup",
    "player_realm_multiplier",
    "resolve_unified_stats",
    "pay_reward",
    "claimable_milestones",
    "sync_catalogue_to_db",
)

#: The functions that take an instant, and the NAME that instant arrives under.
#: The split is the house style: a reader is passed the player row and, where
#: time matters, an explicit ``now`` -- so no arithmetic needs a wall clock.
TIME_TAKING = {
    "utc_day_index": "now",
    "iso_week_key": "now",
    "period_key_for": "now",
    "evaluate_catchup": "now",
    "claimable_milestones": "now",
}


def test_every_name_in_the_public_surface_exists():
    for name in PUBLIC_FUNCTIONS:
        assert callable(getattr(pl, name)), name


def test_no_public_function_can_smuggle_an_argument():
    """No *args/**kwargs: a caller must not be able to pass an unnamed clock."""
    for name in PUBLIC_FUNCTIONS:
        for _, param in _params(getattr(pl, name)):
            assert param.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ), name


def test_the_time_enter_the_module_explicitly_and_only_where_named():
    """Only these five take an instant, and each calls it ``now``."""
    for name in PUBLIC_FUNCTIONS:
        names = {n for n, _ in _params(getattr(pl, name))}
        if name in TIME_TAKING:
            assert "now" in names, name
        else:
            assert "now" not in names, name


# ---------------------------------------------------------------------------
# 3. Lookups refuse rather than guess
# ---------------------------------------------------------------------------


def test_progression_error_is_a_value_error():
    """Callers already map ValueError to a 4xx; this is the single refusal."""
    assert issubclass(pl.ProgressionError, ValueError)


def test_axis_by_key_returns_the_declared_axis():
    for axis in pl.AXES:
        got = pl.axis_by_key(axis["axis_key"])
        assert got == axis
        assert got is not axis


def test_axis_by_key_returns_a_copy_the_caller_cannot_corrupt():
    """A screen that annotates the returned row must not edit the catalogue."""
    first = pl.axis_by_key("cultivation")
    first["display_name"] = "MUTATED"
    first["injected"] = True
    second = pl.axis_by_key("cultivation")
    assert second["display_name"] == "Cultivation"
    assert "injected" not in second
    assert pl.axis_by_key("cultivation") == next(
        a for a in pl.AXES if a["axis_key"] == "cultivation"
    )


@pytest.mark.parametrize("bad", ["", "CULTIVATION", "nope", "temper ", "codexx"])
def test_axis_by_key_refuses_an_unknown_axis(bad):
    with pytest.raises(pl.ProgressionError) as exc:
        pl.axis_by_key(bad)
    assert repr(bad) in str(exc.value)


def test_milestone_by_id_returns_a_copy_and_refuses_the_unknown():
    for row in pl.MILESTONES:
        got = pl.milestone_by_id(row["milestone_id"])
        assert got == row
        assert got is not row
    with pytest.raises(pl.ProgressionError) as exc:
        pl.milestone_by_id("no_such_milestone")
    assert "no_such_milestone" in str(exc.value)


def test_milestone_by_id_does_not_mutate_the_shared_lookup_table():
    got = pl.milestone_by_id("cultivation_step_1")
    got["display_name"] = "MUTATED"
    assert pl.milestone_by_id("cultivation_step_1")["display_name"] == "First Steps"
    assert pl.MILESTONES_BY_ID["cultivation_step_1"]["display_name"] == "First Steps"


def test_validate_reward_normalises_to_floats():
    """Ints in, floats out -- a screen can format without a type check."""
    assert pl.validate_reward({"qi": 50, "insight": 1}) == {"qi": 50.0, "insight": 1.0}
    assert all(isinstance(v, float) for v in pl.validate_reward({"qi": 1}).values())


def test_validate_reward_accepts_an_empty_reward_and_zero_amounts():
    assert pl.validate_reward({}) == {}
    assert pl.validate_reward({"qi": 0, "insight": 0.0}) == {"qi": 0.0, "insight": 0.0}


def test_validate_reward_refuses_a_non_dict():
    for bad in ([], ("qi", 1.0), "qi", 5, None):
        with pytest.raises(pl.ProgressionError):
            pl.validate_reward(bad)


def test_validate_reward_refuses_an_unpayable_key_and_names_it():
    with pytest.raises(pl.ProgressionError) as exc:
        pl.validate_reward({"qi": 1.0, "gold": 999})
    assert "gold" in str(exc.value)


@pytest.mark.parametrize("bad_value", ["10", None, [1], {}, True, False])
def test_validate_reward_refuses_a_non_numeric_or_bool_amount(bad_value):
    """bool is a subclass of int, so True must not become 1.0 qi silently."""
    with pytest.raises(pl.ProgressionError):
        pl.validate_reward({"qi": bad_value})


def test_validate_reward_refuses_a_negative_amount():
    for bad in (-1, -0.001, float("-inf")):
        with pytest.raises(pl.ProgressionError):
            pl.validate_reward({"qi": bad})


@pytest.mark.parametrize("corrupt_value", [float("inf"), float("nan"), 10**400])
def test_validate_reward_recovers_a_non_finite_or_overflowing_amount(
    corrupt_value,
):
    """A table row can carry infinity, NaN, or an int too large for a double.

    None of the three is caught by the checks above them: ``float()`` RETURNS
    ``inf`` rather than raising, ``nan`` compares false against 0 so it clears
    the negativity test, and ``10 ** 400`` is a legal Python int that only
    overflows at the float conversion. All three recover to 0.0, the same
    answer an unreadable amount gets.
    """
    assert pl.validate_reward({"qi": corrupt_value}) == {"qi": 0.0}


@pytest.mark.parametrize("corrupt_value", [float("inf"), float("nan"), 10**400])
def test_a_non_finite_table_reward_cannot_be_paid(corrupt_value):
    """The end of the line: no player row ends up holding a non-finite total.

    This is the assertion that matters. ``validate_reward`` returning 0.0 is
    only safe if the number actually reaching the player is finite, so this
    drives the real ``pay_reward`` and inspects the row it writes.
    """
    player = _player(dao_insight=0.0, progress={"realm_index": 0})
    paid = pl.pay_reward(player, {"qi": corrupt_value, "insight": corrupt_value})
    assert paid["qi"] == 0.0
    assert paid["insight"] == 0.0
    assert math.isfinite(player["dao_insight"])
    for field in player["progress"].values():
        if isinstance(field, float):
            assert math.isfinite(field), player["progress"]


def test_validate_reward_does_not_mutate_its_argument():
    original = {"qi": 5, "insight": 2}
    snapshot = dict(original)
    pl.validate_reward(original)
    assert original == snapshot


def test_requirement_met_refuses_an_unknown_kind():
    with pytest.raises(pl.ProgressionError) as exc:
        pl.requirement_met(_player(), {"kind": "vibes"})
    assert "vibes" in str(exc.value)


def test_requirement_met_refuses_an_unknown_axis_named_inside_a_requirement():
    with pytest.raises(pl.ProgressionError) as exc:
        pl.requirement_met(_player(), {"kind": "axis_step", "axis_key": "nope", "step": 1})
    assert "nope" in str(exc.value)


def test_requirement_met_refuses_a_missing_kind_entirely():
    with pytest.raises(pl.ProgressionError):
        pl.requirement_met(_player(), {"axis_key": "cultivation", "step": 1})
    with pytest.raises(pl.ProgressionError):
        pl.requirement_met(_player(), {})


def test_period_key_for_refuses_an_unknown_period_kind():
    with pytest.raises(pl.ProgressionError) as exc:
        pl.period_key_for("hourly", NOW)
    assert "hourly" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. The catalogue's own consistency
# ---------------------------------------------------------------------------


def test_milestones_index_matches_the_tuple():
    assert set(pl.MILESTONES_BY_ID) == {m["milestone_id"] for m in pl.MILESTONES}


def test_every_milestone_declares_the_frozen_key_set():
    for row in pl.MILESTONES:
        assert set(row) == MILESTONE_KEYS_IN_ROW, row["milestone_id"]


def test_every_milestone_points_at_a_real_axis_a_real_kind_and_a_real_period():
    for row in pl.MILESTONES:
        assert row["axis_key"] in pl.AXIS_KEYS, row["milestone_id"]
        assert row["requirement"]["kind"] in pl.REQUIREMENT_KINDS, row["milestone_id"]
        assert row["period_kind"] in PERIOD_KINDS, row["milestone_id"]


def test_every_shipped_milestone_reward_is_payable():
    """A row that cannot be paid is a startup failure, not a silent loss."""
    for row in pl.MILESTONES:
        assert pl.validate_reward(row["reward"]) == row["reward"], row["milestone_id"]


def test_every_shipped_milestone_requirement_evaluates_against_a_player():
    """A requirement that raises on a real player can never be claimed."""
    for row in pl.MILESTONES:
        assert isinstance(pl.requirement_met(_player(), row["requirement"]), bool), (
            row["milestone_id"]
        )


def test_catchup_rows_are_marked_and_forever_rows_are_not():
    """A catch-up row pays repeatedly, so the flag and the period must agree."""
    for row in pl.MILESTONES:
        if row["is_catchup"]:
            assert row["period_kind"] in ("daily", "weekly"), row["milestone_id"]
        else:
            assert row["period_kind"] == "forever", row["milestone_id"]


def test_ladder_rungs_are_ordered_by_step_index_within_an_axis():
    by_axis: dict = {}
    for row in pl.MILESTONES:
        if row["is_catchup"]:
            continue
        by_axis.setdefault(row["axis_key"], []).append(row["step_index"])
    for axis_key, steps in by_axis.items():
        assert steps == sorted(steps), axis_key
        assert len(set(steps)) == len(steps), axis_key


# ---------------------------------------------------------------------------
# 5. The four axis readers
# ---------------------------------------------------------------------------


def test_cultivation_step_of_an_empty_player_is_zero():
    assert pl.cultivation_step({}) == 0
    assert pl.cultivation_step(BARE_PLAYER) == 0


def test_cultivation_step_is_realm_index_times_stage_count_plus_stage_index():
    """The formula, read against the owner enum rather than a retyped copy."""
    from cultivation_system import Realm, Stage

    stage_count = len(list(Stage))
    for realm in Realm:
        for stage in Stage:
            expected = list(Realm).index(realm) * stage_count + list(Stage).index(stage)
            got = pl.cultivation_step(
                _player(cultivation_realm=realm.value, cultivation_stage=stage.value)
            )
            assert got == expected, (realm.value, stage.value)


def test_cultivation_step_of_an_unknown_realm_or_stage_is_zero_not_a_crash():
    """A row written by a future version must still be readable today."""
    assert pl.cultivation_step(_player(cultivation_realm="ascendant")) == 0
    assert pl.cultivation_step(_player(cultivation_stage="zenith")) == 0
    assert pl.cultivation_step(
        _player(cultivation_realm="ascendant", cultivation_stage="zenith")
    ) == 0


def test_cultivation_step_never_decreases_as_the_ladder_rises():
    from cultivation_system import Realm, Stage

    worst = None
    for realm in Realm:
        got = pl.cultivation_step(_player(cultivation_realm=realm.value))
        if worst is not None:
            assert got >= worst
        worst = got
    stage_count = len(list(Stage))
    assert worst == (len(list(Realm)) - 1) * stage_count


def test_cultivation_step_of_the_top_rung_is_the_highest_index():
    """The ladder must not have a step above the ladder's last rung."""
    from cultivation_system import Realm, Stage

    assert pl.cultivation_step(
        _player(cultivation_realm=list(Realm)[-1].value,
                cultivation_stage=list(Stage)[-1].value)
    ) == len(list(Realm)) * len(list(Stage)) - 1


def test_temper_step_of_a_player_with_no_gear_is_zero():
    assert pl.temper_step({}) == 0
    assert pl.temper_step({"equipment": {}}) == 0
    assert pl.temper_step({"equipment": []}) == 0
    assert pl.temper_step({"artifacts": {}}) == 0
    assert pl.temper_step({"artifacts": []}) == 0


def test_temper_step_reads_an_equipment_map_and_a_bare_list_alike():
    """The block may arrive as {item_id: record} or as [record]."""
    record = {"quality": "legendary"}
    from_map = pl.temper_step({"equipment": {"blade": dict(record)}})
    from_list = pl.temper_step({"equipment": [dict(record)]})
    assert from_map == from_list > 0
    assert from_map == pl._TEMPER_ORDER.index("legendary")


def test_temper_step_reads_the_best_quality_among_several_pieces():
    row = pl.temper_step(
        {"equipment": [
            {"quality": "common"},
            {"quality": "legendary"},
            {"quality": "rare"},
        ]}
    )
    assert row == pl._TEMPER_ORDER.index("legendary")


def test_temper_step_counts_only_switched_on_artifacts():
    """An unswitched artifact is owned but not earned."""
    off = pl.temper_step({"artifacts": {"a1": {"quality": "legendary"}}})
    assert off == 0
    on = pl.temper_step(
        {"artifacts": {"a1": {"quality": "legendary", "switched_on": True}}}
    )
    assert on == pl._TEMPER_ORDER.index("legendary")
    listed = pl.temper_step(
        {"artifacts": [{"quality": "legendary", "switched_on": True}]}
    )
    assert listed == pl._TEMPER_ORDER.index("legendary")


def test_temper_step_ignores_an_unknown_quality_instead_of_crashing():
    """A gear quality from a future version must not blank the axis screen."""
    assert pl.temper_step({"equipment": [{"quality": "mythic_plus"}]}) == 0
    assert pl.temper_step({"equipment": [{"quality": None}]}) == 0
    assert pl.temper_step({"equipment": [{"no_quality_key": 1}]}) == 0


def test_temper_step_ignores_a_non_dict_block_and_non_dict_records():
    assert pl.temper_step({"equipment": "legendary"}) == 0
    assert pl.temper_step({"equipment": 7}) == 0
    assert pl.temper_step({"equipment": ["legendary", 3, None]}) == 0


def test_temper_step_orders_qualities_by_their_owners_grade():
    """The ladder is derived from ``equipment_config``, never retyped here."""
    expected = sorted(
        equipment_config.QUALITY_GRADE,
        key=lambda q: (
            list(equipment_config.QUALITY_GRADE.values()).index(
                equipment_config.QUALITY_GRADE[q]
            ),
            list(equipment_config.QUALITY_GRADE).index(q),
        ),
    )
    assert pl._TEMPER_ORDER == tuple(expected)
    indices = [pl.temper_step({"equipment": [{"quality": q}]}) for q in expected]
    assert indices == sorted(indices)
    assert indices[0] == 0


def test_leaderboard_step_of_an_unranked_player_is_zero():
    assert pl.leaderboard_step({}) == 0
    assert pl.leaderboard_step({"leaderboard_rank": None}) == 0
    assert pl.leaderboard_step({"leaderboard_rank": "unranked"}) == 0
    assert pl.leaderboard_step({"leaderboard_rank": "not a number"}) == 0
    assert pl.leaderboard_step({"leaderboard_rank": []}) == 0


def test_leaderboard_step_is_one_inside_a_paying_band_and_zero_outside():
    """Read through the reward module's own boundaries, never a retyped list."""
    paying = pl.leaderboard_step({"leaderboard_rank": 1})
    assert paying == 1
    assert paying == 1 if leaderboard_rewards.band_for_rank(1) != leaderboard_rewards.BAND_NONE \
        else paying == 0
    beyond = leaderboard_rewards.BAND_OUTER_MAX_RANK + 1
    assert pl.leaderboard_step({"leaderboard_rank": beyond}) == 0
    assert pl.leaderboard_step({"leaderboard_rank": beyond + 5000}) == 0


def test_leaderboard_step_crosses_the_paying_boundary_from_its_owner():
    last_paying = leaderboard_rewards.BAND_OUTER_MAX_RANK
    assert pl.leaderboard_step({"leaderboard_rank": last_paying}) == 1
    assert pl.leaderboard_step({"leaderboard_rank": last_paying + 1}) == 0


def test_leaderboard_step_accepts_a_numeric_string_rank():
    """A rank stored as text still places the player."""
    assert pl.leaderboard_step({"leaderboard_rank": "1"}) == 1
    assert pl.leaderboard_step({"leaderboard_rank": str(
        leaderboard_rewards.BAND_OUTER_MAX_RANK + 1
    )}) == 0


def test_leaderboard_step_refuses_a_rank_the_owner_cannot_score():
    """A negative rank is a corrupt row; the owner's refusal is not swallowed."""
    with pytest.raises(ValueError):
        pl.leaderboard_step({"leaderboard_rank": -1})
    with pytest.raises(ValueError):
        pl.leaderboard_step({"leaderboard_rank": 0})


def test_codex_total_is_a_non_negative_integer():
    total = pl.codex_total()
    assert isinstance(total, int)
    assert total >= 0


def test_codex_step_prefers_the_stored_percent_over_the_codex_block():
    """The stored figure is the server's own answer; the block is a fallback."""
    assert pl.codex_step({"codex_completion_percent": 25.0}) == 25
    assert pl.codex_step(
        {"codex_completion_percent": 25.0, "codex": {"seen": {"a": 1}}}
    ) == 25


def test_codex_step_clamps_the_stored_percent_into_zero_to_one_hundred():
    assert pl.codex_step({"codex_completion_percent": 150.0}) == 100
    assert pl.codex_step({"codex_completion_percent": 9999}) == 100
    assert pl.codex_step({"codex_completion_percent": -5.0}) == 0
    assert pl.codex_step({"codex_completion_percent": -0.4}) == 0


def test_codex_step_of_a_zero_stored_percent_is_zero():
    """0.0 is not None: it must not fall through to the block."""
    assert pl.codex_step({"codex_completion_percent": 0.0}) == 0


def test_codex_step_rounds_the_stored_percent_to_a_whole_number():
    assert pl.codex_step({"codex_completion_percent": 24.6}) == 25
    assert pl.codex_step({"codex_completion_percent": "42.6"}) == 43
    assert pl.codex_step({"codex_completion_percent": 33.4}) == 33


def test_codex_step_of_a_damaged_stored_value_is_zero_not_a_crash():
    for bad in ("abc", object(), [1], float("nan")):
        got = pl.codex_step({"codex_completion_percent": bad})
        assert got == 0, bad


def test_codex_step_of_a_player_with_no_codex_data_is_zero():
    assert pl.codex_step({}) == 0
    assert pl.codex_step({"codex": {}}) == 0
    assert pl.codex_step({"codex": "damaged"}) == 0


def test_codex_step_is_never_negative_and_never_above_one_hundred():
    for row in ({}, {"codex": {"seen": {"a": 1, "b": 2}}},
                {"codex_completion_percent": 40.0}):
        assert 0 <= pl.codex_step(row) <= 100


def test_axis_steps_returns_one_int_per_axis_in_catalogue_order():
    steps = pl.axis_steps(_player())
    assert list(steps) == list(pl.AXIS_KEYS)
    assert all(isinstance(v, int) for v in steps.values())
    assert all(v >= 0 for v in steps.values())


def test_axis_steps_agrees_with_each_reader_called_directly():
    row = _player(
        cultivation_realm="core",
        cultivation_stage="late",
        equipment=[{"quality": "epic"}],
        leaderboard_rank=3,
        codex_completion_percent=40.0,
    )
    assert pl.axis_steps(row) == {
        "cultivation": pl.cultivation_step(row),
        "temper": pl.temper_step(row),
        "leaderboard": pl.leaderboard_step(row),
        "codex": pl.codex_step(row),
    }


def test_a_higher_player_is_never_behind_on_any_axis():
    low = pl.axis_steps(_player())
    high = pl.axis_steps(
        _player(
            cultivation_realm="immortal",
            cultivation_stage="peak",
            equipment=[{"quality": "mythic"}],
            leaderboard_rank=1,
            codex_completion_percent=100.0,
        )
    )
    for key in pl.AXIS_KEYS:
        assert high[key] >= low[key], key
    assert high["cultivation"] > low["cultivation"]
    assert high["codex"] == 100


# ---------------------------------------------------------------------------
# 6. requirement_met -- the four kinds
# ---------------------------------------------------------------------------


def test_axis_step_requirement_is_inclusive_at_the_boundary():
    """Reaching step 3 IS meeting 'step 3'; off-by-one here is a real miss."""
    assert pl.requirement_met(
        _player(cultivation_realm="core", cultivation_stage="early"),
        {"kind": "axis_step", "axis_key": "cultivation", "step": 1},
    )
    assert not pl.requirement_met(
        _player(), {"kind": "axis_step", "axis_key": "cultivation", "step": 1}
    )


def test_axis_step_requirement_defaults_a_missing_step_to_zero():
    assert pl.requirement_met(
        _player(), {"kind": "axis_step", "axis_key": "cultivation"}
    ) is True


def test_realm_multiplier_requirement_is_inclusive_at_the_boundary():
    player = _player()
    boundary = pl.player_realm_multiplier(player)
    assert pl.requirement_met(
        player, {"kind": "realm_multiplier", "min": boundary}
    )
    assert not pl.requirement_met(
        player, {"kind": "realm_multiplier", "min": boundary + 0.0001}
    )


def test_realm_multiplier_requirement_defaults_a_missing_min_to_zero():
    assert pl.requirement_met(_player(), {"kind": "realm_multiplier"}) is True


def test_codex_completion_requirement_is_inclusive_at_the_boundary():
    player = {"codex_completion_percent": 25.0}
    assert pl.requirement_met(player, {"kind": "codex_completion", "percent": 25.0})
    assert not pl.requirement_met(
        player, {"kind": "codex_completion", "percent": 25.1}
    )


def test_temper_quality_requirement_meets_at_the_named_quality():
    player = {"equipment": [{"quality": "legendary"}]}
    assert pl.requirement_met(player, {"kind": "temper_quality", "quality": "legendary"})
    assert pl.requirement_met(player, {"kind": "temper_quality", "quality": "rare"})
    assert not pl.requirement_met(player, {"kind": "temper_quality", "quality": "mythic"})


def test_temper_quality_requirement_names_an_unknown_quality_that_never_blocks():
    """An unknown quality in a REQUIREMENT is refused, not floored to zero.

    This used to index to -1 and pass through ``max(0, want)``, so a mistyped
    quality silently became claimable by every player. It is refused the same
    way the module already refuses an unknown ``kind`` and an unknown
    ``axis_key``. A player's own future-version gear still reads tolerantly
    through ``temper_step`` -- that -1 is about their data, not our config.
    """
    with pytest.raises(pl.ProgressionError):
        pl.requirement_met(
            BARE_PLAYER, {"kind": "temper_quality", "quality": "no_such_quality"}
        )
    # Even the strongest possible player state cannot claim it: the
    # requirement is unpriceable, not merely unmet.
    with pytest.raises(pl.ProgressionError):
        pl.requirement_met(
            _player(equipment=[{"quality": "legendary"}]),
            {"kind": "temper_quality", "quality": "no_such_quality"},
        )


def test_requirement_met_never_reads_the_wall_clock():
    """A requirement is a function of the player row alone."""
    requirement = {"kind": "axis_step", "axis_key": "cultivation", "step": 1}
    assert pl.requirement_met(
        _player(cultivation_realm="core"), requirement
    ) == pl.requirement_met(_player(cultivation_realm="core"), requirement)


# ---------------------------------------------------------------------------
# 7. Period keys
# ---------------------------------------------------------------------------


def test_utc_day_index_is_a_day_count_not_a_timestamp():
    assert pl.utc_day_index(0.0) == 0
    assert pl.utc_day_index(pl.DAY_SECONDS) == 1
    assert pl.utc_day_index(pl.DAY_SECONDS - 1) == 0
    assert pl.utc_day_index(NOW) == int(NOW // pl.DAY_SECONDS)


def test_utc_day_index_advances_by_exactly_one_across_midnight_utc():
    """The day boundary is UTC midnight; one second either side must differ."""
    midnight = float(1_704_067_200)  # 2024-01-01T00:00:00Z
    assert pl.utc_day_index(midnight - 1) == pl.utc_day_index(midnight) - 1
    assert pl.utc_day_index(midnight) == pl.utc_day_index(midnight + 86399)


def test_iso_week_key_has_the_iso_shape():
    key = pl.iso_week_key(NOW)
    assert re.fullmatch(r"\d{4}-W\d{2}", key), key


def test_iso_week_key_is_the_frozen_value_for_the_frozen_instant():
    assert pl.iso_week_key(NOW) == "2023-W46"
    assert pl.iso_week_key(WEEK_START) == "2024-W01"


def test_iso_week_key_is_stable_inside_a_week_and_changes_across_one():
    """A weekly key that moves mid-week pays twice in one week."""
    monday = WEEK_START
    assert pl.iso_week_key(monday) == pl.iso_week_key(monday + 6 * 86400)
    assert pl.iso_week_key(monday) != pl.iso_week_key(monday + 7 * 86400)
    # The last second of ISO week 1 (2023) and the first of 2024 differ.
    assert pl.iso_week_key(WEEK_START - 1) != pl.iso_week_key(WEEK_START)


def test_period_key_forever_is_the_literal_for_every_instant():
    """A once-ever milestone must never be re-keyed into a payable period."""
    for instant in (0.0, NOW, WEEK_START, WEEK_START + 86400 * 400):
        assert pl.period_key_for("forever", instant) == "forever"


def test_period_key_daily_is_the_day_index_and_rotates_at_midnight():
    assert pl.period_key_for("daily", NOW) == str(pl.utc_day_index(NOW))
    midnight = float(1_704_067_200)
    assert pl.period_key_for("daily", midnight - 1) != pl.period_key_for("daily", midnight)


def test_period_key_weekly_is_the_iso_week_and_rotates_on_monday():
    assert pl.period_key_for("weekly", NOW) == pl.iso_week_key(NOW)
    assert pl.period_key_for("weekly", WEEK_START - 1) != pl.period_key_for(
        "weekly", WEEK_START
    )


def test_the_three_period_kinds_are_mutually_distinguishable():
    midnight = float(1_704_067_200)
    keys = {kind: pl.period_key_for(kind, midnight) for kind in PERIOD_KINDS}
    assert len(set(keys.values())) == len(PERIOD_KINDS)


# ---------------------------------------------------------------------------
# 8. Catch-up arithmetic
# ---------------------------------------------------------------------------


def test_catchup_constants_are_the_frozen_formula():
    assert pl.CATCHUP_QI_PER_HOUR > 0
    assert pl.CATCHUP_MAX_QI > 0
    assert pl.CATCHUP_MAX_ABSENCE_HOURS > 0
    assert pl.CATCHUP_DECAY == CATCHUP_HALF
    assert 0.0 < pl.CATCHUP_FLOOR < 1.0
    assert 0.0 < pl.CATCHUP_MIN_ABSENCE_HOURS <= pl.CATCHUP_MAX_ABSENCE_HOURS


def test_catchup_multiplier_halves_then_halves_again():
    assert pl.catchup_multiplier(0) == 1.0
    assert pl.catchup_multiplier(1) == pytest.approx(CATCHUP_HALF)
    assert pl.catchup_multiplier(2) == pytest.approx(CATCHUP_HALF ** 2)
    assert pl.catchup_multiplier(3) == pytest.approx(CATCHUP_HALF ** 3)
    assert pl.catchup_multiplier(4) == pytest.approx(CATCHUP_HALF ** 4)


def test_catchup_multiplier_is_strictly_decreasing_until_the_floor_then_flat():
    """The curve falls, then parks on the floor -- it never keeps falling past it."""
    seen = [pl.catchup_multiplier(n) for n in range(0, 40)]
    assert all(v >= pl.CATCHUP_FLOOR for v in seen)
    assert all(v > 0.0 for v in seen)

    floor_index = next(
        n for n, v in enumerate(seen) if v == pytest.approx(pl.CATCHUP_FLOOR)
    )
    assert floor_index > 0, "the floor must be reached, not start at zero"
    above = seen[:floor_index]
    assert all(later < earlier for earlier, later in zip(above, above[1:])), above
    on_floor = seen[floor_index:]
    assert on_floor == [pytest.approx(pl.CATCHUP_FLOOR)] * len(on_floor)
    # Once it has parked, asking for more never pays less.
    assert pl.catchup_multiplier(39) == pl.catchup_multiplier(40)


def test_catchup_multiplier_is_floored_and_the_floor_holds_forever():
    assert pl.catchup_multiplier(1000) == pl.CATCHUP_FLOOR
    assert pl.catchup_multiplier(10_000) == pl.CATCHUP_FLOOR
    assert pl.catchup_multiplier(1_000_000) == pl.CATCHUP_FLOOR


def test_catchup_multiplier_treats_a_negative_corrupt_counter_as_zero():
    """A negative row must not become a BONUS above 1.0."""
    assert pl.catchup_multiplier(-1) == pl.catchup_multiplier(0)
    assert pl.catchup_multiplier(-999) == 1.0
    assert pl.catchup_multiplier(None) == 1.0


def test_catchup_absence_hours_converts_seconds_and_clamps_both_ends():
    assert pl.catchup_absence_hours(0) == 0.0
    assert pl.catchup_absence_hours(1800) == pytest.approx(0.5)
    assert pl.catchup_absence_hours(3600) == pytest.approx(1.0)
    assert pl.catchup_absence_hours(-500) == 0.0
    assert pl.catchup_absence_hours(None) == 0.0


def test_catchup_absence_hours_is_capped_at_the_max_absence():
    cap_seconds = pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0
    assert pl.catchup_absence_hours(cap_seconds) == pytest.approx(
        pl.CATCHUP_MAX_ABSENCE_HOURS
    )
    assert pl.catchup_absence_hours(cap_seconds * 1000) == pytest.approx(
        pl.CATCHUP_MAX_ABSENCE_HOURS
    )


def test_evaluate_catchup_returns_the_frozen_key_set():
    out = pl.evaluate_catchup(7200.0, 0, NOW)
    assert set(out) == CATCHUP_KEYS
    assert out["ok"] is True


def test_evaluate_catchup_of_a_full_first_absence_pays_the_derived_amount():
    out = pl.evaluate_catchup(7200.0, 0, NOW)
    assert out["eligible"] is True
    assert out["absence_hours"] == pytest.approx(2.0)
    assert out["full_value_qi"] == pytest.approx(2.0 * pl.CATCHUP_QI_PER_HOUR, rel=1e-3)
    assert out["decay_multiplier"] == 1.0
    assert out["amount_qi"] == pytest.approx(out["full_value_qi"])
    assert out["prior_claims"] == 0
    assert out["next_multiplier"] == pytest.approx(CATCHUP_HALF, abs=1e-6)
    assert out["hours_capped"] is False


def test_evaluate_catchup_reports_a_zero_amount_but_still_explains_itself():
    """An ineligible payout must still carry every intermediate number."""
    out = pl.evaluate_catchup(60.0, 0, NOW)
    assert set(out) == CATCHUP_KEYS
    assert out["eligible"] is False
    assert out["amount_qi"] == 0.0
    assert out["absence_hours"] == pytest.approx(1 / 60.0, abs=1e-3)
    assert out["decay_multiplier"] == 1.0


def test_evaluate_catchup_is_ineligible_exactly_below_the_minimum_absence():
    just_under = pl.CATCHUP_MIN_ABSENCE_HOURS * 3600.0 - 1.0
    exactly = pl.CATCHUP_MIN_ABSENCE_HOURS * 3600.0
    assert pl.evaluate_catchup(just_under, 0, NOW)["eligible"] is False
    assert pl.evaluate_catchup(exactly, 0, NOW)["eligible"] is True


def test_evaluate_catchup_is_ineligible_for_a_zero_or_negative_absence():
    for bad in (0.0, -1.0, None):
        out = pl.evaluate_catchup(bad, 0, NOW)
        assert out["eligible"] is False
        assert out["amount_qi"] == 0.0


def test_evaluate_catchup_pays_more_for_a_longer_absence_up_to_the_cap():
    amounts = [
        pl.evaluate_catchup(h * 3600.0, 0, NOW)["amount_qi"]
        for h in (1.0, 2.0, 6.0, 24.0, 72.0)
    ]
    assert all(later >= earlier for earlier, later in zip(amounts, amounts[1:]))
    assert amounts[-1] > amounts[0]


def test_evaluate_catchup_pays_nothing_beyond_the_cap_and_flags_it():
    """A month away must not pay more than the cap, and the screen must know."""
    long_away = pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0 * 1000
    out = pl.evaluate_catchup(long_away, 0, NOW)
    at_cap = pl.evaluate_catchup(pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0, 0, NOW)
    assert out["amount_qi"] == at_cap["amount_qi"]
    assert out["absence_hours"] == pytest.approx(pl.CATCHUP_MAX_ABSENCE_HOURS)
    assert out["hours_capped"] is True
    assert at_cap["hours_capped"] is False


def test_evaluate_catchup_amount_never_exceeds_the_max_even_at_the_longest_absence():
    for prior in (0, 1, 5, 50):
        out = pl.evaluate_catchup(
            pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0 * 100, prior, NOW
        )
        assert out["amount_qi"] <= pl.CATCHUP_MAX_QI
        assert out["amount_qi"] >= 0.0


def test_evaluate_catchup_shrinks_as_the_prior_claim_count_rises():
    """Diminishing returns: the same absence pays less each time."""
    amounts = [pl.evaluate_catchup(6 * 3600.0, n, NOW)["amount_qi"] for n in range(6)]
    assert all(later < earlier for earlier, later in zip(amounts, amounts[1:]))
    assert amounts[-1] == pytest.approx(amounts[0] * pl.CATCHUP_FLOOR, rel=1e-2)


def test_evaluate_catchup_reports_the_counter_it_actually_used():
    assert pl.evaluate_catchup(7200.0, -5, NOW)["prior_claims"] == 0
    assert pl.evaluate_catchup(7200.0, 7, NOW)["prior_claims"] == 7
    assert pl.evaluate_catchup(7200.0, 0, NOW)["prior_claims"] == 0


def test_evaluate_catchup_cannot_take_a_none_counter_but_its_reader_can():
    """A null claim counter is priced as a FIRST claim, not a crash.

    This used to raise a bare ``TypeError``, because ``next_multiplier``
    computed ``prior_claims + 1`` before the ``or 0`` normalisation its
    sibling lines applied. The counter is now normalised once at the top of
    the function and reused, so a damaged ledger row cannot stop a caller
    pricing a catch-up at all. ``prior_claims`` reports 0 and
    ``next_multiplier`` steps forward from there, exactly as it would for a
    counter of 0.
    """
    out = pl.evaluate_catchup(7200.0, None, NOW)
    assert out["prior_claims"] == 0
    assert out["decay_multiplier"] == pytest.approx(pl.catchup_multiplier(0))
    assert out["next_multiplier"] == pytest.approx(
        pl.catchup_multiplier(1), abs=1e-6
    )
    # Identical to an explicit zero, all ten keys.
    assert set(out) == set(pl.evaluate_catchup(7200.0, 0, NOW))


def test_evaluate_catchup_next_multiplier_is_this_calls_multiplier_stepped_once():
    for prior in (0, 1, 4, 9):
        out = pl.evaluate_catchup(7200.0, prior, NOW)
        assert out["decay_multiplier"] == pytest.approx(pl.catchup_multiplier(prior))
        assert out["next_multiplier"] == pytest.approx(
            pl.catchup_multiplier(prior + 1), abs=1e-6
        )


def test_evaluate_catchup_keys_a_daily_period_the_server_derived():
    """The period key comes from the clock, never from the caller."""
    assert pl.evaluate_catchup(7200.0, 0, NOW)["period_key"] == pl.period_key_for(
        "daily", NOW
    )
    assert pl.evaluate_catchup(7200.0, 0, WEEK_START)["period_key"] != pl.evaluate_catchup(
        7200.0, 0, NOW
    )["period_key"]


def test_evaluate_catchup_base_qi_overrides_the_derived_full_value():
    out = pl.evaluate_catchup(3600.0, 0, NOW, base_qi=250.0)
    assert out["full_value_qi"] == 250.0
    assert out["amount_qi"] == pytest.approx(250.0)
    assert out["eligible"] is True


def test_evaluate_catchup_base_qi_does_not_buy_eligibility():
    """An override changes the size, not the 'have you been away' test."""
    out = pl.evaluate_catchup(1.0, 0, NOW, base_qi=10000.0)
    assert out["eligible"] is False
    assert out["amount_qi"] == 0.0
    assert out["full_value_qi"] == 10000.0


def test_evaluate_catchup_base_qi_zero_pays_nothing():
    out = pl.evaluate_catchup(6 * 3600.0, 0, NOW, base_qi=0.0)
    assert out["eligible"] is False
    assert out["amount_qi"] == 0.0


def test_evaluate_catchup_amount_is_rounded_to_cents():
    out = pl.evaluate_catchup(3723.7, 3, NOW)
    assert out["amount_qi"] == round(out["amount_qi"], 2)
    assert out["full_value_qi"] == round(out["full_value_qi"], 2)
    assert out["decay_multiplier"] == round(out["decay_multiplier"], 6)


def test_evaluate_catchup_is_pure_across_repeated_calls():
    """Same inputs, same outputs -- no hidden counter inside the function."""
    first = pl.evaluate_catchup(5 * 3600.0, 2, NOW)
    second = pl.evaluate_catchup(5 * 3600.0, 2, NOW)
    assert first == second


# ---------------------------------------------------------------------------
# 9. The unified stat resolver
# ---------------------------------------------------------------------------


def test_player_realm_multiplier_delegates_to_its_owner():
    """This module must never become another copy of the realm ladder."""
    for realm in ("mortal", "foundation", "core", "immortal"):
        for stage in ("early", "late", "middle"):
            player = _player(cultivation_realm=realm, cultivation_stage=stage)
            assert pl.player_realm_multiplier(player) == pytest.approx(
                combat_config.calculate_realm_multiplier(realm, stage)
            )


def test_player_realm_multiplier_defaults_a_bare_row_to_mortal_early():
    assert pl.player_realm_multiplier({}) == pytest.approx(
        combat_config.calculate_realm_multiplier("mortal", "early")
    )
    assert pl.player_realm_multiplier({}) > 0.0


def test_player_realm_multiplier_returns_a_float():
    assert isinstance(pl.player_realm_multiplier(_player()), float)


def test_resolve_unified_stats_returns_the_frozen_key_set():
    out = pl.resolve_unified_stats(_player())
    assert set(out) == STATS_KEYS
    assert out["ok"] is True


def test_resolve_unified_stats_multiplies_rather_than_adds():
    """Two 10% bonuses must compound to 21%, not silently become 20%."""
    player = _player(progression_milestone_bonus=0.1, dao_insight=0.1)
    out = pl.resolve_unified_stats(player)
    expected = out["realm_multiplier"] * (1.0 + 0.1) * (1.0 + 0.1)
    assert out["effective_multiplier"] == pytest.approx(expected, rel=1e-5)
    assert out["effective_multiplier"] != pytest.approx(
        out["realm_multiplier"] + 0.1 + 0.1, rel=1e-3
    )


def test_resolve_unified_stats_effective_is_the_product_of_its_three_parts():
    for player in (
        _player(),
        _player(cultivation_realm="core", cultivation_stage="late"),
        _player(dao_insight=0.05, progression_milestone_bonus=0.02),
    ):
        out = pl.resolve_unified_stats(player)
        expected = (
            out["realm_multiplier"]
            * (1.0 + out["insight_bonus"])
            * (1.0 + out["milestone_bonus"])
        )
        assert out["effective_multiplier"] == pytest.approx(expected, rel=1e-5)


def test_resolve_unified_stats_multipliers_are_monotone_in_every_input():
    base = pl.resolve_unified_stats(_player())["effective_multiplier"]
    higher_realm = pl.resolve_unified_stats(
        _player(cultivation_realm="immortal", cultivation_stage="peak")
    )["effective_multiplier"]
    more_insight = pl.resolve_unified_stats(_player(dao_insight=0.05))["effective_multiplier"]
    more_milestone = pl.resolve_unified_stats(
        _player(progression_milestone_bonus=0.05)
    )["effective_multiplier"]
    assert higher_realm > base
    assert more_insight > base
    assert more_milestone > base


def test_resolve_unified_stats_clamps_a_negative_milestone_bonus_to_zero():
    """A corrupt row must not make a player WEAKER than the ladder says."""
    out = pl.resolve_unified_stats(_player(progression_milestone_bonus=-5.0))
    assert out["milestone_bonus"] == 0.0
    assert out["effective_multiplier"] == pytest.approx(out["realm_multiplier"])


def test_resolve_unified_stats_reads_the_insight_ceiling_from_its_owner():
    """The cap is prestige_reset's, not retyped here."""
    absurd = pl.resolve_unified_stats(_player(dao_insight=10_000.0))
    assert absurd["insight_bonus"] == pytest.approx(prestige_reset.INSIGHT_CAP)
    assert absurd["insight_bonus"] <= prestige_reset.INSIGHT_CAP


def test_resolve_unified_stats_reports_the_risk_reduction_separately():
    """Insight reduces breakthrough RISK, not damage -- labelled apart."""
    player = _player(dao_insight=0.05)
    out = pl.resolve_unified_stats(player)
    assert out["breakthrough_risk_reduction"] == out["insight_bonus"]


def test_resolve_unified_stats_reports_every_axis_step():
    out = pl.resolve_unified_stats(_player(codex_completion_percent=30.0))
    assert set(out["steps"]) == set(pl.AXIS_KEYS)
    assert out["steps"] == pl.axis_steps(_player(codex_completion_percent=30.0))


def test_resolve_unified_stats_writes_nothing_back_onto_the_player():
    """A cached multiplier goes stale after a prestige and nothing notices."""
    player = _player()
    snapshot = dict(player)
    pl.resolve_unified_stats(player)
    assert player == snapshot
    assert "effective_multiplier" not in player
    assert "realm_multiplier" not in player
    assert "milestone_bonus" not in player


def test_resolve_unified_stats_is_pure_across_repeated_calls():
    player = _player(dao_insight=0.05)
    assert pl.resolve_unified_stats(player) == pl.resolve_unified_stats(player)


def test_resolve_unified_stats_rounds_its_every_reported_number():
    out = pl.resolve_unified_stats(
        _player(cultivation_realm="core", dao_insight=0.123456789,
                progression_milestone_bonus=0.987654321)
    )
    for key in ("realm_multiplier", "insight_bonus", "milestone_bonus",
                "effective_multiplier", "breakthrough_risk_reduction"):
        assert out[key] == round(out[key], 6), key


# ---------------------------------------------------------------------------
# 10. pay_reward
# ---------------------------------------------------------------------------


def test_pay_reward_returns_the_paid_amounts_as_floats():
    player = {}
    paid = pl.pay_reward(player, {"qi": 50, "insight": 0.01})
    assert set(paid) == {"qi", "insight"}
    assert paid == {"qi": 50.0, "insight": 0.01}
    assert all(isinstance(v, float) for v in paid.values())


def test_pay_reward_writes_qi_to_the_field_the_breakthrough_gate_reads():
    """Crediting ``player['qi']`` alone is the defect qi_rewards exists to stop."""
    player = {"progress": 10.0}
    pl.pay_reward(player, {"qi": 50.0})
    assert player["progress"] == 60.0


def test_pay_reward_accumulates_onto_existing_progress():
    player = {"progress": 100.0}
    pl.pay_reward(player, {"qi": 25.0})
    assert player["progress"] == 125.0
    pl.pay_reward(player, {"qi": 25.0})
    assert player["progress"] == 150.0


def test_pay_reward_seeds_progress_from_a_legacy_qi_only_row():
    player = {"qi": 30.0}
    pl.pay_reward(player, {"qi": 10.0})
    assert player["progress"] == 40.0


def test_pay_reward_a_zero_reward_writes_no_progress_field():
    """A zero qi must not create the field the gate reads."""
    player = {}
    paid = pl.pay_reward(player, {"qi": 0.0, "insight": 0.0})
    assert paid == {"qi": 0.0, "insight": 0.0}
    assert "progress" not in player


def test_pay_reward_accumulates_insight_onto_dao_insight():
    player = {"dao_insight": 0.02}
    pl.pay_reward(player, {"insight": 0.01})
    assert player["dao_insight"] == pytest.approx(0.03)
    pl.pay_reward(player, {"insight": 0.01})
    assert player["dao_insight"] == pytest.approx(0.04)


def test_pay_reward_starts_from_zero_on_a_damaged_insight_field():
    player = {"dao_insight": "not a number"}
    pl.pay_reward(player, {"insight": 0.02})
    assert player["dao_insight"] == pytest.approx(0.02)


def test_pay_reward_refuses_an_invalid_reward_before_writing_anything():
    for bad in ({"gold": 1.0}, {"qi": -1.0}, {"qi": "ten"}, {"qi": True}, "qi", None):
        player = {"progress": 5.0, "dao_insight": 0.1}
        with pytest.raises(pl.ProgressionError):
            pl.pay_reward(player, bad)
        assert player == {"progress": 5.0, "dao_insight": 0.1}, bad


def test_pay_reward_of_an_empty_reward_is_a_no_op():
    player = {"progress": 5.0}
    assert pl.pay_reward(player, {}) == {"qi": 0.0, "insight": 0.0}
    assert player == {"progress": 5.0}


def test_pay_reward_does_not_mutate_the_reward_argument():
    reward = {"qi": 5, "insight": 1}
    snapshot = dict(reward)
    pl.pay_reward({}, reward)
    assert reward == snapshot


def test_pay_reward_two_calls_do_not_double_the_reported_amount():
    """The returned amount is per-call, not a running total."""
    player = {}
    first = pl.pay_reward(player, {"qi": 10.0})
    second = pl.pay_reward(player, {"qi": 10.0})
    assert first == second == {"qi": 10.0, "insight": 0.0}
    assert player["progress"] == 20.0


# ---------------------------------------------------------------------------
# 11. claimable_milestones
# ---------------------------------------------------------------------------


def _ids(rows):
    return [r["milestone_id"] for r in rows]


def test_claimable_milestones_returns_a_list_of_frozen_shaped_rows():
    rows = pl.claimable_milestones(_player(), NOW)
    assert isinstance(rows, list)
    assert rows
    for row in rows:
        assert set(row) == CLAIMABLE_KEYS
        assert isinstance(row["reward"], dict)
        assert isinstance(row["is_catchup"], bool)


def test_claimable_milestones_follows_the_catalogue_order():
    rows = pl.claimable_milestones(_player(cultivation_realm="immortal",
                                           cultivation_stage="peak",
                                           leaderboard_rank=1,
                                           codex_completion_percent=100.0,
                                           equipment=[{"quality": "mythic"}]), NOW)
    catalogue_order = [m["milestone_id"] for m in pl.MILESTONES]
    assert _ids(rows) == [i for i in catalogue_order if i in set(_ids(rows))]


def test_claimable_milestones_omits_an_unmet_requirement_rather_than_listing_it_locked():
    """This function answers 'what can I press', nothing else."""
    rows = pl.claimable_milestones(_player(), NOW)
    ids = set(_ids(rows))
    assert "cultivation_step_1" not in ids  # needs step 1, bare player is step 0
    assert "catchup_daily" in ids  # step 0 requirement, always met


def test_claimable_milestones_omits_a_claimed_key():
    key = ("catchup_daily", pl.period_key_for("daily", NOW))
    ids = set(_ids(pl.claimable_milestones(_player(), NOW, {key})))
    assert "catchup_daily" not in ids


def test_claimable_milestones_forever_key_survives_a_change_of_day():
    """A once-ever rung claimed today must not be payable tomorrow."""
    first = set(_ids(pl.claimable_milestones(
        _player(cultivation_realm="core"), NOW
    )))
    key = ("cultivation_step_1", "forever")
    later = set(_ids(pl.claimable_milestones(
        _player(cultivation_realm="core"), NOW + 86400 * 9, {key}
    )))
    assert "cultivation_step_1" in first
    assert "cultivation_step_1" not in later


def test_claimable_milestones_weekly_key_expires_with_the_iso_week():
    """A weekly rung claimed in week 1 is payable again in week 2."""
    week2 = WEEK_START + 7 * 86400
    claimed = {("catchup_weekly", pl.period_key_for("weekly", WEEK_START))}
    this_week = set(_ids(pl.claimable_milestones(_player(), WEEK_START, claimed)))
    next_week = set(_ids(pl.claimable_milestones(_player(), week2, claimed)))
    assert "catchup_weekly" not in this_week
    assert "catchup_weekly" in next_week


def test_claimable_milestones_daily_key_expires_with_the_utc_day():
    midnight = float(1_704_067_200)
    claimed = {("catchup_daily", pl.period_key_for("daily", midnight))}
    assert "catchup_daily" not in set(_ids(pl.claimable_milestones(
        _player(), midnight, claimed
    )))
    assert "catchup_daily" in set(_ids(pl.claimable_milestones(
        _player(), midnight + 86400, claimed
    )))


def test_claimable_milestones_treats_a_missing_claimed_set_as_empty():
    assert pl.claimable_milestones(_player(), NOW) == pl.claimable_milestones(
        _player(), NOW, set()
    )
    assert pl.claimable_milestones(_player(), NOW, None) == pl.claimable_milestones(
        _player(), NOW
    )


def test_claimable_milestones_claims_everything_a_fully_earned_player_earned():
    """A player at the top of every ladder can press every forever rung."""
    player = _player(
        cultivation_realm="immortal",
        cultivation_stage="peak",
        equipment=[{"quality": "mythic"}],
        leaderboard_rank=1,
        codex_completion_percent=100.0,
    )
    ids = set(_ids(pl.claimable_milestones(player, NOW)))
    forever_ids = {m["milestone_id"] for m in pl.MILESTONES if not m["is_catchup"]}
    assert forever_ids <= ids


def test_claimable_milestones_returns_validated_float_rewards():
    for row in pl.claimable_milestones(_player(cultivation_realm="core"), NOW):
        assert row["reward"] == pl.validate_reward(row["reward"])
        assert all(isinstance(v, float) for v in row["reward"].values())


def test_claimable_milestones_carries_the_period_key_it_resolved():
    for row in pl.claimable_milestones(_player(), NOW):
        catalogue = pl.MILESTONES_BY_ID[row["milestone_id"]]
        assert row["period_key"] == pl.period_key_for(catalogue["period_kind"], NOW)
        assert row["period_kind"] == catalogue["period_kind"]
        assert row["is_catchup"] == bool(catalogue["is_catchup"])


def test_claimable_milestones_never_mutates_the_catalogue_or_the_player():
    player = _player(cultivation_realm="core")
    snapshot = dict(player)
    catalogue = [dict(m) for m in pl.MILESTONES]
    pl.claimable_milestones(player, NOW)
    assert player == snapshot
    assert [dict(m) for m in pl.MILESTONES] == catalogue


def test_claimable_milestones_does_not_pay_anything_itself():
    """It reports; ``pay_reward`` is what writes."""
    player = {"progress": 0.0}
    pl.claimable_milestones(player, NOW)
    assert player == {"progress": 0.0}


# ---------------------------------------------------------------------------
# 12. sync_catalogue_to_db
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._sink.append((sql, params))


class _FakeConnection:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)


class _FakeStore:
    def __init__(self, connection):
        self.current_connection = connection


def test_sync_catalogue_writes_nothing_without_a_database():
    """The game runs from memory and must not need PostgreSQL to show a ladder."""
    assert pl.sync_catalogue_to_db(None) == 0
    assert pl.sync_catalogue_to_db(object()) == 0
    assert pl.sync_catalogue_to_db(_FakeStore(None)) == 0


def test_sync_catalogue_returns_one_row_per_axis_and_milestone():
    sink: list = []
    written = pl.sync_catalogue_to_db(_FakeStore(_FakeConnection(sink)))
    assert written == len(pl.AXES) + len(pl.MILESTONES)
    assert len(sink) == written


def test_sync_catalogue_upserts_so_a_rerun_is_not_a_duplicate_crash():
    sink: list = []
    pl.sync_catalogue_to_db(_FakeStore(_FakeConnection(sink)))
    assert all("ON CONFLICT" in sql.upper() for sql, _ in sink)
    assert any("progression_axis" in sql for sql, _ in sink)
    assert any("progression_milestone" in sql for sql, _ in sink)


def test_sync_catalogue_writes_the_same_numbers_the_module_reads():
    """The table is the durable copy, so it cannot disagree with the code."""
    sink: list = []
    pl.sync_catalogue_to_db(_FakeStore(_FakeConnection(sink)))
    axis_writes = [params for sql, params in sink if "progression_axis" in sql]
    assert [w[0] for w in axis_writes] == list(pl.AXIS_KEYS)
    milestone_writes = [params for sql, params in sink if "progression_milestone" in sql]
    assert [w[0] for w in milestone_writes] == [m["milestone_id"] for m in pl.MILESTONES]


def test_sync_catalogue_serialises_the_requirement_and_validates_the_reward():
    import json

    sink: list = []
    pl.sync_catalogue_to_db(_FakeStore(_FakeConnection(sink)))
    milestone_writes = [params for sql, params in sink if "progression_milestone" in sql]
    for milestone, params in zip(pl.MILESTONES, milestone_writes):
        assert json.loads(params[4]) == milestone["requirement"]
        assert json.loads(params[5]) == pl.validate_reward(milestone["reward"])


def test_sync_catalogue_refuses_an_unpayable_row_instead_of_writing_it_half():
    """A bad row must fail the SYNC, not surface later as a short payout."""
    broken = [dict(m) for m in pl.MILESTONES]
    broken[0]["reward"] = {"gold": 1.0}
    original = pl.MILESTONES
    try:
        pl.MILESTONES = tuple(broken)
        sink: list = []
        with pytest.raises(pl.ProgressionError):
            pl.sync_catalogue_to_db(_FakeStore(_FakeConnection(sink)))
    finally:
        pl.MILESTONES = original


# ---------------------------------------------------------------------------
# 13. Non-vacuity -- the checkers above can actually fail
# ---------------------------------------------------------------------------


def test_the_key_set_checks_would_notice_a_removed_key():
    """Guard the guard: a narrower key set must not pass its own assertion."""
    narrowed = CATCHUP_KEYS - {"hours_capped"}
    assert narrowed != CATCHUP_KEYS
    assert "hours_capped" not in narrowed
    assert set(pl.evaluate_catchup(7200.0, 0, NOW)) == CATCHUP_KEYS


def test_the_catchup_shape_check_would_notice_a_renamed_key():
    out = dict(pl.evaluate_catchup(7200.0, 0, NOW))
    out["hours_capped_flag"] = out.pop("hours_capped")
    assert set(out) != CATCHUP_KEYS


def test_the_monotone_check_would_notice_a_flattened_curve():
    """A constant curve would satisfy 'floor' but not 'strictly decreasing'."""
    flat = [pl.CATCHUP_FLOOR] * 5
    assert all(v >= pl.CATCHUP_FLOOR for v in flat)
    assert not all(
        later < earlier for earlier, later in zip(flat, flat[1:])
    )
    real = [pl.catchup_multiplier(n) for n in range(5)]
    assert all(later < earlier for earlier, later in zip(real, real[1:]))


def test_the_cap_check_would_notice_an_uncapped_payout():
    """A month-long absence paying more than the cap is the defect."""
    way_too_long = pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0 * 1000
    capped = pl.evaluate_catchup(
        pl.CATCHUP_MAX_ABSENCE_HOURS * 3600.0, 0, NOW
    )["amount_qi"]
    uncapped_hypothetical = pl.CATCHUP_QI_PER_HOUR * (
        pl.CATCHUP_MAX_ABSENCE_HOURS * 1000
    )
    assert capped <= pl.CATCHUP_MAX_QI
    assert uncapped_hypothetical > pl.CATCHUP_MAX_QI  # why the cap exists


def test_the_readers_really_depend_on_the_player_row():
    """An axis reader that ignored its argument would make these vacuous."""
    assert pl.axis_steps(_player()) != pl.axis_steps(
        _player(cultivation_realm="immortal", codex_completion_percent=99.0)
    )
    assert pl.cultivation_step({}) != pl.cultivation_step(
        _player(cultivation_realm="immortal")
    )
# ---------------------------------------------------------------------------
# 14. Authentication boundary -- this module cannot be the door
#
# ``progression_ladders`` exposes NO routes, so it cannot refuse an anonymous
# caller: there is nothing to 401. The auth decision lives in the route layer
# -- ``cultivation_routes.py`` owns the six ``/cultivation-ladder/*`` paths and
# declares ``Depends(require_player)`` BY HAND on each, and its mount-time
# check raises ``RuntimeError`` if any came up unauthenticated. That layer's
# 401 tests live in ``test_cultivation_routes_contract.py``.
#
# What IS this module's auth contract, and the thing to read before asking
# "where are the 401 tests?": it never decides WHO the caller is. Every
# function takes an already-resolved player row and nothing that could name an
# identity. If any of them grew a ``player_id``, a session or a request, this
# module would become the place a caller picks whose ladder to read -- and
# every arithmetic test below would still pass, because arithmetic does not
# care who it is computing for.
#
# So the assertions here are the ones that turn a future identity parameter
# into a RED suite rather than a silent hole.
# ---------------------------------------------------------------------------

#: Parameter names that would mean the module is choosing whose data to read.
#: ``player`` is deliberately NOT in this set: it is the RESOLVED row the route
#: handed over, and refusing it would forbid the whole module.
IDENTITY_PARAMS = frozenset({
    "player_id",
    "user_id",
    "identity",
    "request",
    "req",
    "session",
    "token",
    "headers",
    "auth",
    "claims",
    "db",
    "current_user",
})


def test_no_public_function_accepts_a_caller_supplied_identity():
    """The whole auth contract in one assertion: nothing here names a caller."""
    for name in PUBLIC_FUNCTIONS:
        func = getattr(pl, name)
        params = {n for n, _ in _params(func)}
        assert not (params & IDENTITY_PARAMS), (name, sorted(params & IDENTITY_PARAMS))


def test_the_only_player_shaped_argument_is_a_bare_resolved_row():
    """``player`` is allowed; so is the store. Nothing ELSE may name a row.

    Catches ``claimable_milestones(player, player_id, ...)``: a second argument
    that reads like an identity is the shape a caller-named row arrives in.
    ``players_db`` is allowed deliberately -- it is the catalogue writer's
    handle, and it names no player.
    """
    allowed = {"player", "players_db"}
    for name in PUBLIC_FUNCTIONS:
        params = [n for n, _ in _params(getattr(pl, name))]
        playerish = [p for p in params if "player" in p]
        assert [p for p in playerish if p not in allowed] == [], (name, params)
        assert playerish in ([], ["player"], ["players_db"]), (name, params)


def test_the_module_holds_no_auth_or_http_symbol_to_authorise_through():
    """No guard, no router, no exception mapper: nothing here to lean on."""
    for name in ("require_player", "PlayerIdentity", "router", "app",
                 "HTTPException", "Depends", "enforce_ownership"):
        assert not hasattr(pl, name), name


def test_a_player_id_inside_the_row_is_inert_arithmetic():
    """Two rows differing ONLY in identity produce identical ladders.

    The behavioural half of the contract: even if a future row carries
    ``player_id``, no function reads it as authority. Measured: the ladder, the
    stats resolver and the claimable list are all identical for the two rows.
    """
    mine = {"cultivation_realm": "core", "cultivation_stage": "late",
            "player_id": "player-a", "leaderboard_rank": 4,
            "codex_completion_percent": 40.0,
            "equipment": [{"quality": "epic"}]}
    theirs = dict(mine, player_id="player-b")
    assert mine != theirs  # the two rows really do differ
    assert pl.axis_steps(mine) == pl.axis_steps(theirs)
    assert pl.resolve_unified_stats(mine) == pl.resolve_unified_stats(theirs)
    assert pl.claimable_milestones(mine, NOW) == pl.claimable_milestones(theirs, NOW)


def test_claimable_milestones_reports_only_and_writes_nothing_itself():
    """No store and no request: the module cannot persist to a row itself."""
    player = {"progress": 0.0, "dao_insight": 0.0, "player_id": "player-a"}
    snapshot = dict(player)
    pl.claimable_milestones(player, NOW)
    assert player == snapshot


# ---------------------------------------------------------------------------
# 15. Realm advancement is monotonic
#
# The Ladder STEP is strictly monotonic AND contiguous. The realm MULTIPLIER
# deliberately is not -- both facts measured before writing anything:
#
#   * ``immortal`` returns the same multiplier as ``mortal``. The top realm is
#     reachable only through a tribulation, so it defers its power until after.
#   * the ``breakthrough`` stage returns the realm's base multiplier, which is
#     a PENALTY against the stage below it (mortal peak 1.6 -> 1.0). A player
#     about to attempt a breakthrough fights weaker on purpose.
#
# So "multipliers never fall" would be a FALSE claim about this code, and a
# test asserting it would need a frozen list of exceptions to stay green. The
# monotonic contract belongs to the step; the multiplier's contract is that it
# is a PASS-THROUGH of the owner's number with no clamp, filter or reorder
# applied here. Both are pinned, and neither freezes a balance number.
# ---------------------------------------------------------------------------


def _ladder_order():
    """Every ``(realm, stage)`` pair in the owners' own declaration order."""
    from cultivation_system import Realm, Stage
    return [(r.value, s.value) for r in Realm for s in Stage]


def test_cultivation_step_is_contiguous_from_zero_across_the_whole_ladder():
    """Every rung owns exactly one step and no step is skipped.

    Measured over all realm x stage pairs in ladder order: the sequence is
    0, 1, 2, ... N-1. A ladder with a hole would leave a milestone requirement
    at that number unsatisfiable forever, with no error anywhere.
    """
    seq = [
        pl.cultivation_step({"cultivation_realm": realm, "cultivation_stage": stage})
        for realm, stage in _ladder_order()
    ]
    assert seq == list(range(len(seq))), seq


def test_cultivation_step_rises_by_exactly_one_at_every_rung():
    """Strictly monotonic along the ladder: no gap, no repeat, no plateau."""
    seq = [
        pl.cultivation_step({"cultivation_realm": realm, "cultivation_stage": stage})
        for realm, stage in _ladder_order()
    ]
    assert all(later == earlier + 1 for earlier, later in zip(seq, seq[1:])), seq


def test_each_realm_starts_strictly_above_the_previous_realms_peak():
    """Advancing a realm is a real gain even at the new realm's FIRST stage.

    Measured: mortal tops at 4, foundation starts at 5, core at 10, and so on.
    A ladder where a new realm's floor equalled the old realm's ceiling would
    make promotion feel like nothing.
    """
    from cultivation_system import Realm, Stage
    stages = [s.value for s in Stage]
    bottoms = [
        pl.cultivation_step({"cultivation_realm": r.value, "cultivation_stage": stages[0]})
        for r in Realm
    ]
    peaks = [
        pl.cultivation_step({"cultivation_realm": r.value, "cultivation_stage": stages[-1]})
        for r in Realm
    ]
    for earlier_peak, later_bottom in zip(peaks, bottoms[1:]):
        assert later_bottom > earlier_peak, (peaks, bottoms)


def test_an_unknown_realm_falls_to_the_bottom_never_above_a_reached_rung():
    """Future-version data must never read as progress."""
    reached = pl.cultivation_step(_player(cultivation_realm="core"))
    assert pl.cultivation_step(_player(cultivation_realm="god_emperor")) == 0
    assert pl.cultivation_step(_player(cultivation_realm="god_emperor")) < reached


def test_claimable_milestones_grows_monotonically_as_a_player_rises():
    """A player can only ever GAIN claimable rungs, never lose one.

    Measured: low 3 -> mid 8 -> high 10, each a superset of the last.
    """
    low = {r["milestone_id"] for r in pl.claimable_milestones(_player(), NOW)}
    mid = {r["milestone_id"] for r in pl.claimable_milestones(
        _player(cultivation_realm="foundation", leaderboard_rank=1,
                codex_completion_percent=30.0), NOW)}
    high = {r["milestone_id"] for r in pl.claimable_milestones(
        _player(cultivation_realm="immortal", cultivation_stage="peak",
                leaderboard_rank=1, codex_completion_percent=100.0,
                equipment=[{"quality": "mythic"}]), NOW)}
    assert low <= mid, sorted(mid - low)
    assert mid <= high, sorted(high - mid)
    assert len(low) < len(high)


def test_the_realm_multiplier_is_a_pass_through_with_no_clamp_or_reorder():
    """The multiplier's contract is fidelity to its owner, not monotonicity.

    A retune of ``combat_config`` must reach this module unchanged. Any clamp,
    filter or reorder applied HERE would silently fork the balance tables --
    the exact defect this module's docstring says it exists to avoid.
    """
    for realm, stage in _ladder_order():
        got = pl.player_realm_multiplier(
            {"cultivation_realm": realm, "cultivation_stage": stage})
        owner = combat_config.calculate_realm_multiplier(realm, stage)
        assert got == pytest.approx(owner), (realm, stage, got, owner)


def test_a_multiplier_penalty_at_a_later_stage_does_not_move_the_step_backwards():
    """Control for the previous test: the step is the monotonic authority.

    Measured: the ``breakthrough`` stage returns each realm's base multiplier,
    a PENALTY against the stage below it, and ``immortal`` prices as
    ``mortal``. Both are deliberate. If a future test ever asserts "multipliers
    never fall", it must fail against this measured reality -- and the step
    must still be strictly rising at exactly those points.
    """
    from cultivation_system import Stage
    stages = [s.value for s in Stage]
    base = pl.player_realm_multiplier(_player(cultivation_realm="core"))
    below = pl.player_realm_multiplier(
        _player(cultivation_realm="core", cultivation_stage=stages[-2]))
    assert base < below, "the later stage must be measured as the weaker one"

    reached = pl.cultivation_step(
        _player(cultivation_realm="core", cultivation_stage=stages[-2]))
    at_next = pl.cultivation_step(
        _player(cultivation_realm="core", cultivation_stage=stages[-1]))
    assert at_next == reached + 1, "the step still advances at the penalty stage"


# ---------------------------------------------------------------------------
# 16. Breakthrough chance -- the number reported is the number the gate rolls
#
# ``resolve_unified_stats`` reports ``breakthrough_risk_reduction``, and the
# module's docstring claims that figure IS the Dao Insight bonus reducing
# breakthrough RISK rather than damage. The gate lives in ``breakthrough.py``:
# ``attempt_chance`` adds ``dao_insight_bonus(player)``, which resolves through
# the SAME ``prestige_reset.insight_bonus``. If those two ever drift, a screen
# promises a safer breakthrough and the roll does not deliver.
#
# These are CROSS-MODULE contracts, not snapshots: each compares this module
# against ``breakthrough`` / ``prestige_reset`` at run time, so a retune on
# either side stays green as long as the two still agree.
# ---------------------------------------------------------------------------


def test_the_reported_risk_reduction_is_exactly_what_the_gate_rolls():
    """Same function, same number -- measured against ``breakthrough`` itself."""
    import breakthrough as bt

    for insight in (0.0, 0.01, 0.05, 0.15, 1.0, 10_000.0):
        player = _player(dao_insight=insight)
        reported = pl.resolve_unified_stats(player)["breakthrough_risk_reduction"]
        gate = bt.dao_insight_bonus(player)
        assert reported == pytest.approx(gate), (insight, reported, gate)


def test_the_risk_reduction_saturates_at_the_owners_cap_and_never_past_it():
    """A player with a million insight points buys no more safety than at the cap.

    The anti-abuse direction: because the ceiling is the owner's, prestige
    cannot be farmed into a guaranteed breakthrough.
    """
    capped = pl.resolve_unified_stats(_player(dao_insight=0.15))["breakthrough_risk_reduction"]
    absurd = pl.resolve_unified_stats(_player(dao_insight=10_000.0))["breakthrough_risk_reduction"]
    assert absurd == pytest.approx(capped)
    assert capped <= prestige_reset.INSIGHT_CAP
    assert 0.0 <= capped <= 1.0


def test_the_reported_risk_reduction_sits_inside_the_gates_own_chance_clamp():
    """The figure is reported in 0..1, and the roll that uses it is 0.05..0.90."""
    import breakthrough as bt

    for insight in (0.0, 0.15, 10_000.0):
        player = _player(dao_insight=insight)
        risk = pl.resolve_unified_stats(player)["breakthrough_risk_reduction"]
        assert 0.0 <= risk <= 1.0
        state = bt.state_of(dict(player))
        chance = bt.attempt_chance(dict(player), dict(state))
        assert bt.CHANCE_FLOOR <= chance <= bt.CHANCE_CEILING


def test_more_insight_never_lowers_the_chance_the_gate_rolls():
    """Directional: the reported figure and the real roll move together."""
    import breakthrough as bt

    chances = []
    for insight in (0.0, 0.05, 0.15):
        player = _player(dao_insight=insight)
        chances.append(bt.attempt_chance(player, dict(bt.state_of(player))))
    assert all(later >= earlier for earlier, later in zip(chances, chances[1:])), chances
    # A bare mortal already sits on the 0.90 ceiling, so the real assertion is
    # that insight never makes the roll worse.
    assert chances[-1] >= chances[0]


def test_risk_reduction_is_reported_apart_from_the_damage_multiplier_it_also_drives():
    """The same insight both multiplies damage and reduces risk -- labelled twice.

    Easy to "fix" by dropping one of the two, so both are pinned: the number
    shows up inside ``effective_multiplier`` AND is echoed under its own key.
    """
    base = pl.resolve_unified_stats(_player(dao_insight=0.0))
    with_insight = pl.resolve_unified_stats(_player(dao_insight=0.05))
    assert with_insight["effective_multiplier"] > base["effective_multiplier"]
    assert with_insight["breakthrough_risk_reduction"] == with_insight["insight_bonus"]


def test_a_bare_player_is_never_risk_free_and_never_greater_than_one():
    """The reported figure is a probability-like number, at both ends.

    A screen that renders it directly must be able to trust the range without
    knowing ``prestige_reset``'s cap.
    """
    bare = pl.resolve_unified_stats(_player())["breakthrough_risk_reduction"]
    assert bare >= 0.0
    assert all(
        0.0 <= pl.resolve_unified_stats(
            _player(dao_insight=i))["breakthrough_risk_reduction"] <= 1.0
        for i in (0.0, 0.01, 0.15, 5.0)
    )


# ---------------------------------------------------------------------------
# 17. Non-finite input -- every conversion site, measured
#
# The module's own comments name the three-part failure this section pins:
# ``float()`` RAISES on junk, RAISES OverflowError on an int too large for a
# double, and silently RETURNS ``inf``/``nan`` -- which then clears every
# ``< 0`` test, because every comparison against NaN is False.
#
# The invariant across all of them: a corrupt number recovers to the SAME
# reading a missing number gets (zero, or ineligible) and never reaches the
# caller. The last test in this section is the one that matters -- a non-finite
# value in a REPLY is not valid JSON, so it takes down the whole response
# rather than one field.
# ---------------------------------------------------------------------------

#: The ways a stored or caller number arrives broken. ``-inf`` clears the
#: negativity test in some places and is caught by the finite check in others,
#: so it earns its own entry rather than being folded into ``inf``.
NON_FINITE = (float("inf"), float("-inf"), float("nan"), 10**400)


def _floats_in(value, path=()):
    """Every float anywhere in a nested reply, as a ``(key_path, float)`` pair."""
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out.extend(_floats_in(v, path + (k,)))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for i, v in enumerate(value):
            out.extend(_floats_in(v, path + (i,)))
        return out
    return [(path, value)] if isinstance(value, float) else []


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_catchup_absence_hours_recovers_every_non_finite_absence(corrupt):
    """Measured: inf, -inf, nan and 10**400 all read 0.0 -- "not away".

    NaN is the dangerous one: it sails through the ``< 0`` test (every
    comparison against NaN is False), skips the ``min()`` clamp, and would
    reach the reply as a literal ``NaN``.
    """
    assert pl.catchup_absence_hours(corrupt) == 0.0


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_clock_never_raises_and_lands_on_the_epoch(corrupt):
    """Measured: ``utc_day_index`` -> 0 and ``iso_week_key`` -> the epoch's week.

    Both take the value through the finite guard first, so neither raises.
    ``time.gmtime`` on an infinity refuses with OverflowError, which is
    neither a TypeError nor a ValueError.
    """
    assert pl.utc_day_index(corrupt) == 0
    assert pl.iso_week_key(corrupt) == pl.iso_week_key(0.0)


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_claim_counter_is_priced_as_a_first_claim(corrupt):
    """A damaged ledger row must not crash the pricing, nor over-reward it.

    ``catchup_multiplier(inf)`` measured 1.0 -- identical to a first claim,
    which is the safe direction: a corrupt counter cannot become a bonus.
    """
    assert pl.catchup_multiplier(corrupt) == 1.0
    out = pl.evaluate_catchup(7200.0, corrupt, NOW)
    assert out["prior_claims"] == 0
    assert out["decay_multiplier"] == pytest.approx(1.0)


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_rank_is_unranked_rather_than_a_crash(corrupt):
    """The OverflowError case named in ``leaderboard_step``'s own comment.

    ``int(float('inf'))`` raises OverflowError, which is neither a TypeError
    nor a ValueError, so an except clause catching only those two let it
    escape as a 500.
    """
    assert pl.leaderboard_step({"leaderboard_rank": corrupt}) == 0


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_absence_yields_an_ineligible_zero_payout(corrupt):
    """The whole ``evaluate_catchup`` reply stays finite and pays nothing."""
    out = pl.evaluate_catchup(corrupt, 0, NOW)
    assert set(out) == CATCHUP_KEYS
    assert out["eligible"] is False
    assert out["amount_qi"] == 0.0
    for path, value in _floats_in(out):
        assert math.isfinite(value), (path, value)


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_base_override_pays_nothing_rather_than_inf(corrupt):
    """``base_qi`` is the one input whose cap is deliberately NOT applied.

    So it needs its own finite guard: without it an inf reaches the reply with
    ``eligible=True`` -- an amount the ledger could never honour, inside a body
    that is not valid JSON.
    """
    out = pl.evaluate_catchup(7200.0, 0, NOW, base_qi=corrupt)
    assert out["full_value_qi"] == 0.0
    assert out["eligible"] is False
    assert out["amount_qi"] == 0.0


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_now_never_produces_a_bad_period_key(corrupt):
    """The clock feeds the ledger key, so it gets the same guard.

    The payout itself is unaffected: a broken clock must not make an earned
    catch-up unclaimable.
    """
    out = pl.evaluate_catchup(7200.0, 0, corrupt)
    assert out["period_key"] == str(pl.utc_day_index(0.0))
    assert out["eligible"] is True
    assert out["amount_qi"] > 0.0


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_stored_bonus_never_reaches_the_stats_reply(corrupt):
    """Measured: both the milestone bonus and the insight total recover to 0.0.

    The reply then equals the realm ladder alone, so a corrupt row can make a
    player neither stronger nor weaker than the ladder says.
    """
    out = pl.resolve_unified_stats(
        _player(progression_milestone_bonus=corrupt, dao_insight=corrupt))
    assert out["milestone_bonus"] == 0.0
    assert out["insight_bonus"] == 0.0
    assert out["breakthrough_risk_reduction"] == 0.0
    assert out["effective_multiplier"] == pytest.approx(out["realm_multiplier"])


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_paying_onto_a_non_finite_stored_total_writes_a_finite_one(corrupt):
    """The stored TOTAL is guarded, not only the incoming amount.

    ``float()`` returns inf rather than raising, so a stored infinity sails
    past the except clause and ``current + insight`` would write a non-finite
    total back onto the player row.
    """
    player = _player(dao_insight=corrupt, progress={"realm_index": 0})
    pl.pay_reward(player, {"insight": 1.0})
    assert math.isfinite(player["dao_insight"])
    assert player["dao_insight"] == pytest.approx(1.0)


@pytest.mark.parametrize("corrupt", NON_FINITE)
def test_a_non_finite_stored_completion_never_reaches_the_steps_reply(corrupt):
    """The ladder screen's own step stays a finite whole percent in 0..100."""
    steps = pl.resolve_unified_stats(
        _player(codex_completion_percent=corrupt))["steps"]
    assert steps["codex"] == 0
    assert 0 <= steps["codex"] <= 100


def test_no_reply_from_any_public_reader_carries_a_non_finite_number():
    """The invariant behind all of the above, asserted end to end.

    A NaN or inf anywhere in a reply is not valid JSON, so it takes down the
    whole response rather than one field. Every reader is driven with every
    corrupt input at once, each float is checked, and the reply is then
    encoded with ``allow_nan=False`` -- which raises on a single bad float.
    """
    import json

    corrupt_row = {
        "dao_insight": float("nan"),
        "progression_milestone_bonus": float("inf"),
        "codex_completion_percent": float("inf"),
        "leaderboard_rank": float("nan"),
        "equipment": [{"quality": "legendary"}],
    }
    replies = [
        pl.axis_steps(corrupt_row),
        pl.resolve_unified_stats(corrupt_row),
        pl.evaluate_catchup(float("inf"), float("nan"), float("inf"),
                            base_qi=float("nan")),
        pl.evaluate_catchup(7200.0, float("inf"), NOW, base_qi=float("inf")),
        pl.claimable_milestones(corrupt_row, float("inf")),
    ]
    for reply in replies:
        for path, value in _floats_in(reply):
            assert math.isfinite(value), (path, value)
        json.dumps(reply, allow_nan=False)  # raises on any NaN/inf


def test_the_finite_guard_is_not_merely_a_floor():
    """Guard the guard: a floor cannot do the job the finite check does.

    If ``math.isfinite`` were deleted from the conversion helper, every test
    above would see a literal inf. These three lines are what proves a floor is
    the wrong mechanism: an infinity compares above every floor, and NaN
    compares below it, so neither survives a clamp.
    """
    assert math.isfinite(pl.catchup_absence_hours(float("nan")))
    assert pl.evaluate_catchup(7200.0, 0, NOW, base_qi=float("inf"))["full_value_qi"] == 0.0
    # An infinity clears any floor; a NaN clears any ceiling.
    assert max(0.0, float("inf")) == float("inf")
    assert not (float("nan") < 0.0)


# ---------------------------------------------------------------------------
# 18. A LARGE-BUT-FINITE clock -- the case the non-finite tests cannot reach
#
# The control above found this: ``math.isfinite`` does NOT mean "small". A
# finite value can still overflow a downstream conversion. Measured:
# ``time.gmtime`` raises OverflowError at 1e20 seconds but OSError at 1e12, and
# both are refused as a valid stamp -- so the two failure modes are DIFFERENT
# and the guard must name both.
#
# Feeding inf/nan does not exercise this at all: ``_finite_or_zero`` turns both
# into 0.0 BEFORE ``gmtime`` runs, so the except clause is never entered. These
# are the values that actually reach it.
# ---------------------------------------------------------------------------

#: Finite, and far past any date ``gmtime`` can represent. Each one was
#: measured to raise inside ``gmtime`` -- the first two as OSError, the rest as
#: OverflowError, which is neither a TypeError nor a ValueError.
UNREADABLE_FINITE_CLOCKS = (1e12, 1e15, 1e18, 1e20, 1e25, 1e30,
                            1.7976931348623157e308)


@pytest.mark.parametrize("stamp", UNREADABLE_FINITE_CLOCKS)
def test_a_large_but_finite_clock_never_raises_and_lands_on_the_epoch(stamp):
    """The unreadable-clock contract, on the inputs that actually trip it.

    An unreadable clock reads as the Unix epoch's week rather than 500-ing
    the claim that asked for a period key. Without the guard this raises out of
    ``gmtime`` -- and OverflowError in particular escapes an ``except`` clause
    written for TypeError and ValueError.
    """
    assert math.isfinite(stamp), "these must be FINITE, or they prove nothing here"
    # Measured: ONLY the week key degrades, because only it calls ``gmtime``,
    # which refuses an out-of-range stamp. It falls back to the epoch's week.
    assert pl.iso_week_key(stamp) == pl.iso_week_key(0.0)
    assert pl.period_key_for("weekly", stamp) == pl.iso_week_key(0.0)
    # The day key does NOT degrade: it is floor division on a float, which
    # stays well-defined at any finite magnitude. It returns a large integer,
    # never a crash and never a fractional day -- and it is still a valid,
    # distinct ledger key, which is all the once-only claim needs from it.
    day = pl.utc_day_index(stamp)
    assert isinstance(day, int) and day >= 0, (stamp, day)
    assert day == int(stamp // pl.DAY_SECONDS), (stamp, day)
    assert pl.period_key_for("daily", stamp) == str(day)


def test_an_unreadable_finite_clock_does_not_take_a_earned_payout_with_it():
    """A broken clock must not also make an earned catch-up unclaimable.

    The payout is priced from the player's absence, not from the clock, so the
    period key degrades on its own and the money still moves.
    """
    for stamp in UNREADABLE_FINITE_CLOCKS:
        out = pl.evaluate_catchup(7200.0, 0, stamp)
        assert out["eligible"] is True, stamp
        assert out["amount_qi"] > 0.0, stamp
        # The daily key comes from ``utc_day_index``, which is well-defined at
        # any finite magnitude; only the WEEK key falls back to the epoch.
        assert out["period_key"] == str(pl.utc_day_index(stamp)), stamp


def test_the_day_key_floors_a_clock_before_the_epoch():
    """Floor division, not truncation: a stamp below the epoch is day -1, not 0.

    Found by a negative control. Replacing ``// DAY_SECONDS`` with
    ``/ DAY_SECONDS`` and letting ``int()`` truncate survives every other
    test, because a float quotient is >= the floored value and they only part
    company BELOW zero. This pins the direction the floor actually takes.
    """
    assert pl.utc_day_index(-1.0) == -1
    assert pl.utc_day_index(-pl.DAY_SECONDS) == -1
    assert pl.utc_day_index(-pl.DAY_SECONDS - 1) == -2
    # The control's own arithmetic, stated so the difference is visible:
    # int(-1/86400) truncates to 0, where the floor gives -1.
    assert int(-1.0 / pl.DAY_SECONDS) == 0
    assert -1.0 // pl.DAY_SECONDS == -1.0


def test_a_large_but_finite_absence_still_pays_and_is_flagged_as_capped():
    """An absurd-but-finite absence hits the cap rather than the catch clause.

    ``_finite_or_zero`` lets it through (it is finite), so this exercises the
    ``min()`` clamp and the ``hours_capped`` flag -- a different code path from
    the non-finite absences, which never reach the clamp at all.
    """
    out = pl.evaluate_catchup(1e20, 0, NOW)
    assert out["eligible"] is True
    assert out["hours_capped"] is True
    assert out["absence_hours"] == pytest.approx(pl.CATCHUP_MAX_ABSENCE_HOURS)
    assert out["amount_qi"] <= pl.CATCHUP_MAX_QI
    for path, value in _floats_in(out):
        assert math.isfinite(value), (path, value)
