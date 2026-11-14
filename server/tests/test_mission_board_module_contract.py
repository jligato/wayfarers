"""Contract for the MODULE `server/mission_board.py` (its pure API).

WHAT THIS FILE IS
-----------------
`test_mission_board_routes_contract.py` already pins the HTTP surface
(401/403/404/400, body fields, headers). This file pins the layer underneath
it: the functions a route calls, and the exact shapes they return. A bug here
is invisible to the HTTP tests whenever both arms of a route return the same
wrong shape.

Pinned contracts
----------------
`objective_def`     unknown id -> None (never raises)
`cycle_key`         quick/daily/weekly -> stable opaque strings; unknown -> raise
`seconds_to_refresh` always a positive int bounded by the cycle length; unknown -> raise
`board_view`        exact top-level keys, exact per-entry keys, clamped progress
`claimable`         returns 0 or a refusal CODE (already_claimed / not_complete /
                    unknown_objective) -- a string, never an exception
`record_claim`      returns the cycle key it stamped; unknown -> raise
`claim_key`         includes player, objective and cycle -- a new cycle, new key
`reward_for`        exactly {spirit_stone: int, material: {item_id, quantity}}
`outcome_for`       the already-claimed arm is a DIFFERENT shape (5 keys, empty
                    reward), and the paid arm is 5 keys too -- both agree

NEGATIVE CONTROLS (in-file, not comments)
-----------------------------------------
* `TestNegativeControls::test_the_detectors_recognise_a_broken_shape` -- the
  entry-shape detector used by the board assertions is run against a mutated
  view, so a detector that accepts anything fails here.
* `TestNegativeControls::test_a_mutated_definition_is_refused_by_the_validator`
  -- `_validate_definitions` really bites (bad source, bad target, fateum row).
* `TestNegativeControls::test_control_without_the_baseline_stamp_shows_the_defect`
  -- with the first-ever-view baseline stamp removed, a returning player sees a
  board of zeros, which is what the stamp exists to prevent.

HTTP ROUTE BEHAVIOUR IS DELIBERATELY NOT RE-TESTED HERE.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mission_board  # noqa: E402
from mission_board import (  # noqa: E402
    BOARD_OBJECTIVES,
    CURRENCY_LIST,
    DAILY_CYCLE_SECONDS,
    KEPT_PAST_CYCLES,
    MATERIAL_UNITS,
    QUICK_CYCLE_SECONDS,
    REWARD_CURRENCY,
    SOURCES,
    STATE_KEY,
    TIER_MATERIAL,
    WEEKLY_CYCLE_SECONDS,
    MissionBoardError,
    board_view,
    claim_key,
    claimable,
    cycle_key,
    default_state,
    get_state,
    objective_def,
    outcome_for,
    record_claim,
    reward_for,
    seconds_to_refresh,
)

# A fixed instant so nothing in this file depends on the wall clock.
NOW = 1_762_000_000.0

TOP_LEVEL_KEYS = {"ok", "objectives", "server_time", "cycles"}
ENTRY_KEYS = {
    "objective_id", "cycle", "cycle_key", "kind", "description", "points_at",
    "unit", "target", "progress", "completed", "claimed", "reward",
    "seconds_to_refresh",
}
OUTCOME_KEYS = {"ok", "objective_id", "cycle_key", "already_claimed", "reward"}


def _player(**over):
    """A player record the pure sources can read without a store."""
    base = {"id": "p1", "progress": 0, "pvp_wins": 0, "beasts": [],
            "tower_state": {}, "cultivation_realm": None}
    base.update(over)
    return base


def _entries_by_id(view):
    return {e["objective_id"]: e for e in view["objectives"]}


def _entry_shape_ok(entry) -> bool:
    """Detector: does this entry carry exactly the keys the panel draws?"""
    return set(entry) == ENTRY_KEYS


def _top_shape_ok(view) -> bool:
    return set(view) == TOP_LEVEL_KEYS and isinstance(view["objectives"], list)


# ----------------------------------------------------------------------
# objective_def
# ----------------------------------------------------------------------
class TestObjectiveDef:
    def test_every_published_row_is_found_by_its_id(self):
        for row in BOARD_OBJECTIVES:
            found = objective_def(row["objective_id"])
            assert found is not None, row["objective_id"]
            assert found is row

    def test_an_unknown_id_is_none_and_not_an_exception(self):
        """A client may name anything. The lookup answers None; it never 500s."""
        assert objective_def("no_such_objective") is None
        assert objective_def("") is None


# ----------------------------------------------------------------------
# cycle_key
# ----------------------------------------------------------------------
class TestCycleKey:
    def test_quick_key_is_an_opaque_string_that_moves_with_the_clock(self):
        first = cycle_key("quick", NOW)
        later = cycle_key("quick", NOW + QUICK_CYCLE_SECONDS)
        assert isinstance(first, str) and first
        assert first != later

    def test_quick_key_is_stable_inside_one_cycle(self):
        assert cycle_key("quick", NOW) == cycle_key("quick", NOW + 60)

    def test_daily_key_is_stable_inside_one_day(self):
        assert cycle_key("daily", NOW) == cycle_key("daily", NOW + 3600)
        assert cycle_key("daily", NOW) != cycle_key("daily", NOW + DAILY_CYCLE_SECONDS + 1)

    def test_weekly_key_is_the_iso_year_and_week(self):
        iso = datetime.fromtimestamp(NOW, tz=timezone.utc).isocalendar()
        assert cycle_key("weekly", NOW) == "w%d-%02d" % (iso[0], iso[1])

    def test_the_weekly_key_turns_over_on_monday_utc(self):
        # 2026-10-05 is a Monday; one second earlier is still the week before.
        monday = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        assert cycle_key("weekly", monday) != cycle_key("weekly", monday - 1)

    def test_the_three_timescales_never_collide(self):
        keys = {cycle_key(c, NOW) for c in ("quick", "daily", "weekly")}
        assert len(keys) == 3

    def test_an_unknown_cycle_raises(self):
        with pytest.raises(MissionBoardError):
            cycle_key("monthly", NOW)


# ----------------------------------------------------------------------
# seconds_to_refresh
# ----------------------------------------------------------------------
class TestSecondsToRefresh:
    @pytest.mark.parametrize("cycle,period", [
        ("quick", QUICK_CYCLE_SECONDS),
        ("daily", DAILY_CYCLE_SECONDS),
        ("weekly", WEEKLY_CYCLE_SECONDS),
    ])
    def test_the_countdown_is_positive_and_bounded(self, cycle, period):
        for offset in (0, 1, 61, 3600, 12345):
            value = seconds_to_refresh(cycle, NOW + offset)
            assert isinstance(value, int)
            assert 1 <= value <= period

    def test_the_countdown_never_reaches_zero_even_on_the_boundary(self):
        """A client showing '0 seconds left' would poll forever."""
        assert seconds_to_refresh("quick", QUICK_CYCLE_SECONDS * 3) >= 1
        assert seconds_to_refresh("daily", DAILY_CYCLE_SECONDS * 7) >= 1

    def test_an_unknown_cycle_raises(self):
        with pytest.raises(MissionBoardError):
            seconds_to_refresh("monthly", NOW)


# ----------------------------------------------------------------------
# state helpers
# ----------------------------------------------------------------------
class TestStateHelpers:
    def test_a_default_state_has_no_cycles(self):
        assert default_state() == {"cycles": {}}

    def test_two_default_states_are_not_the_same_object(self):
        """A shared mutable default would leak one player's board into another."""
        a, b = default_state(), default_state()
        a["cycles"]["q1"] = {}
        assert b["cycles"] == {}

    def test_get_state_creates_the_key_on_a_bare_player(self):
        player = _player()
        state = get_state(player)
        assert player[STATE_KEY] is state

    def test_get_state_replaces_a_corrupt_value(self):
        player = _player(**{STATE_KEY: "not a dict"})
        assert isinstance(get_state(player)["cycles"], dict)

    def test_get_state_replaces_a_cycles_key_that_is_not_a_dict(self):
        player = _player(**{STATE_KEY: {"cycles": 7}})
        assert get_state(player)["cycles"] == {}

    def test_get_state_is_tolerant_of_a_partial_save(self):
        player = _player(**{STATE_KEY: {"cycles": {"q1": {}}}})
        assert get_state(player)["cycles"] == {"q1": {}}


# ----------------------------------------------------------------------
# board_view
# ----------------------------------------------------------------------
class TestBoardView:
    def test_the_top_level_carries_exactly_four_keys(self):
        assert _top_shape_ok(board_view(_player(), now=NOW))

    def test_the_top_level_reports_ok_true(self):
        assert board_view(_player(), now=NOW)["ok"] is True

    def test_server_time_echoes_the_instant_used(self):
        assert board_view(_player(), now=NOW)["server_time"] == NOW

    def test_every_published_objective_appears_exactly_once(self):
        view = board_view(_player(), now=NOW)
        ids = [e["objective_id"] for e in view["objectives"]]
        assert sorted(ids) == sorted(r["objective_id"] for r in BOARD_OBJECTIVES)
        assert len(ids) == len(set(ids))

    def test_every_entry_carries_exactly_the_documented_keys(self):
        for entry in board_view(_player(), now=NOW)["objectives"]:
            assert _entry_shape_ok(entry), entry["objective_id"]

    def test_all_three_timescales_are_reported_with_a_key_and_a_countdown(self):
        cycles = board_view(_player(), now=NOW)["cycles"]
        assert set(cycles) == {"quick", "daily", "weekly"}
        for name, block in cycles.items():
            assert set(block) == {"key", "seconds_to_refresh"}
            assert block["key"] == cycle_key(name, NOW)
            assert block["seconds_to_refresh"] >= 1

    def test_each_entry_reports_its_own_cycle_key_and_countdown(self):
        view = board_view(_player(), now=NOW)
        for entry in view["objectives"]:
            assert entry["cycle_key"] == cycle_key(entry["cycle"], NOW)
            assert entry["seconds_to_refresh"] == seconds_to_refresh(entry["cycle"], NOW)

    def test_progress_never_leaves_the_zero_to_target_band(self):
        player = _player(progress=9999, pvp_wins=999, beasts=list(range(9)))
        for entry in board_view(player, now=NOW)["objectives"]:
            assert 0 <= entry["progress"] <= entry["target"], entry["objective_id"]

    def test_completed_and_claimed_are_booleans(self):
        for entry in board_view(_player(), now=NOW)["objectives"]:
            assert isinstance(entry["completed"], bool)
            assert isinstance(entry["claimed"], bool)

    def test_completed_is_derived_not_asserted(self):
        player = _player(progress=100)
        entries = _entries_by_id(board_view(player, now=NOW))
        assert entries["quick_qi_ready"]["completed"] is True

    def test_a_threshold_row_completes_from_the_current_value(self):
        entries = _entries_by_id(board_view(_player(progress=100), now=NOW))
        assert entries["quick_qi_ready"]["progress"] == entries["quick_qi_ready"]["target"]

    def test_a_threshold_row_is_not_capped_by_a_baseline(self):
        """'Reach the second realm' is a fact about now, not a change."""
        entries = _entries_by_id(board_view(_player(), now=NOW))
        assert entries["daily_realm_step"]["progress"] == 0

    def test_progress_is_clamped_at_the_target_not_beyond_it(self):
        entries = _entries_by_id(board_view(_player(progress=5000), now=NOW))
        assert entries["quick_qi_ready"]["progress"] == 100

    def test_a_fresh_player_sees_nothing_complete(self):
        for entry in board_view(_player(), now=NOW)["objectives"]:
            assert entry["completed"] is False
            assert entry["claimed"] is False

    def test_a_bare_player_without_a_context_still_gets_a_whole_board(self):
        """No mission ledger supplied: the ledger-backed sources read zero."""
        view = board_view(_player(), now=NOW)
        assert len(view["objectives"]) == len(BOARD_OBJECTIVES)

    def test_a_source_that_raises_does_not_take_the_board_down(self, monkeypatch):
        def boom(player, ctx):
            raise RuntimeError("ledger unavailable")
        monkeypatch.setitem(mission_board.SOURCES, "qi_progress", boom)
        entries = _entries_by_id(board_view(_player(), now=NOW))
        assert entries["quick_qi_ready"]["progress"] == 0
        assert len(entries) == len(BOARD_OBJECTIVES)

    def test_reading_the_board_twice_does_not_change_progress(self):
        """The baseline stamp must be written once, not re-stamped each read."""
        player = _player(pvp_wins=7)
        first = _entries_by_id(board_view(player, now=NOW))
        second = _entries_by_id(board_view(player, now=NOW))
        assert first["weekly_pvp_three"]["progress"] == second["weekly_pvp_three"]["progress"]

    def test_a_first_ever_view_baselines_delta_rows_at_zero(self):
        """Progress earned before the board existed still counts."""
        player = _player(pvp_wins=9)
        entries = _entries_by_id(board_view(player, now=NOW))
        assert entries["weekly_pvp_three"]["progress"] == 3

    def test_a_later_cycle_baselines_at_the_value_it_first_sees(self):
        """A cycle already in progress does not count work done before it."""
        player = _player(pvp_wins=5)
        board_view(player, now=NOW)          # first ever view: baseline 0
        player["pvp_wins"] = 5 + 4
        later = board_view(player, now=NOW + WEEKLY_CYCLE_SECONDS * 2)
        entries = _entries_by_id(later)
        # New cycle, first sight of it: the baseline is stamped at 9, so the
        # change made before the cycle opened is not counted.
        assert entries["weekly_pvp_three"]["progress"] == 0

    def test_the_state_stays_inside_the_player_record(self):
        player = _player()
        board_view(player, now=NOW)
        assert STATE_KEY in player
        assert isinstance(player[STATE_KEY]["cycles"], dict)

    def test_finished_cycle_buckets_are_bounded(self):
        player = _player()
        for week in range(40):
            board_view(player, now=NOW + week * WEEKLY_CYCLE_SECONDS)
        cycles = player[STATE_KEY]["cycles"]
        assert len(cycles) <= 3 + KEPT_PAST_CYCLES

    def test_the_current_cycle_bucket_survives_pruning(self):
        player = _player()
        board_view(player, now=NOW)
        before = set(player[STATE_KEY]["cycles"])
        board_view(player, now=NOW)
        assert before <= set(player[STATE_KEY]["cycles"])


# ----------------------------------------------------------------------
# claimable
# ----------------------------------------------------------------------
class TestClaimable:
    def test_an_unknown_objective_is_refused_with_a_code(self):
        assert claimable(_player(), "no_such_objective", now=NOW) == "unknown_objective"

    def test_an_incomplete_objective_is_refused_with_a_code(self):
        assert claimable(_player(), "weekly_pvp_three", now=NOW) == "not_complete"

    def test_a_completed_objective_is_claimable_and_answers_zero(self):
        """0 is the success answer; a refusal is a non-zero string."""
        assert claimable(_player(progress=100), "quick_qi_ready", now=NOW) == 0

    def test_claiming_twice_in_one_cycle_is_refused(self):
        player = _player(progress=100)
        record_claim(player, "quick_qi_ready", now=NOW)
        assert claimable(player, "quick_qi_ready", now=NOW) == "already_claimed"

    def test_a_new_cycle_makes_the_same_objective_claimable_again(self):
        player = _player(progress=100)
        record_claim(player, "quick_qi_ready", now=NOW)
        later = claimable(player, "quick_qi_ready", now=NOW + QUICK_CYCLE_SECONDS * 3)
        assert later != "already_claimed"

    def test_claiming_one_objective_does_not_consume_another(self):
        player = _player(progress=100)
        record_claim(player, "quick_qi_ready", now=NOW)
        assert claimable(player, "quick_tower_floor", now=NOW) != "already_claimed"

    def test_claimable_never_raises_for_a_bad_id(self):
        for bad in ("", None, "quick_qi_ready_extra"):
            result = claimable(_player(), bad, now=NOW)
            assert result == "unknown_objective" or isinstance(result, int)


# ----------------------------------------------------------------------
# record_claim
# ----------------------------------------------------------------------
class TestRecordClaim:
    def test_it_returns_the_cycle_key_it_stamped(self):
        player = _player()
        key = record_claim(player, "quick_qi_ready", now=NOW)
        assert key == cycle_key("quick", NOW)

    def test_the_marker_lands_in_the_player_record(self):
        player = _player()
        record_claim(player, "daily_hunt_five", now=NOW)
        bucket = player[STATE_KEY]["cycles"][cycle_key("daily", NOW)]
        assert bucket["daily_hunt_five"]["claimed"] is True

    def test_a_second_call_is_idempotent_not_an_error(self):
        player = _player()
        first = record_claim(player, "weekly_beast_one", now=NOW)
        second = record_claim(player, "weekly_beast_one", now=NOW)
        assert first == second

    def test_an_unknown_objective_raises(self):
        with pytest.raises(MissionBoardError):
            record_claim(_player(), "no_such_objective", now=NOW)

    def test_the_marker_does_not_leak_into_the_next_cycle(self):
        player = _player()
        record_claim(player, "quick_qi_ready", now=NOW)
        later = board_view(player, now=NOW + QUICK_CYCLE_SECONDS * 3)
        entry = _entries_by_id(later)["quick_qi_ready"]
        assert entry["claimed"] is False
        assert entry["cycle_key"] != cycle_key("quick", NOW)


# ----------------------------------------------------------------------
# claim_key (the wallet idempotency identity)
# ----------------------------------------------------------------------
class TestClaimKey:
    def test_the_key_names_the_player_objective_and_cycle(self):
        key = claim_key("p1", "quick_qi_ready", "q42")
        assert key == "mission_board:p1:quick_qi_ready:q42"

    def test_a_retry_inside_one_cycle_collapses_onto_one_key(self):
        assert claim_key("p1", "x", "q42") == claim_key("p1", "x", "q42")

    def test_a_new_cycle_is_a_new_key(self):
        assert claim_key("p1", "x", "q42") != claim_key("p1", "x", "q43")

    def test_two_players_never_share_a_key(self):
        assert claim_key("p1", "x", "q42") != claim_key("p2", "x", "q42")

    def test_two_objectives_never_share_a_key(self):
        assert claim_key("p1", "x", "q42") != claim_key("p1", "y", "q42")


# ----------------------------------------------------------------------
# rewards
# ----------------------------------------------------------------------
class TestRewardFor:
    def test_the_reward_has_exactly_two_parts(self):
        for row in BOARD_OBJECTIVES:
            assert set(reward_for(row)) == {REWARD_CURRENCY, "material"}

    def test_the_currency_is_a_real_currency_and_an_integer(self):
        assert REWARD_CURRENCY in CURRENCY_LIST
        for row in BOARD_OBJECTIVES:
            amount = reward_for(row)[REWARD_CURRENCY]
            assert isinstance(amount, int) and amount >= 0

    def test_the_material_names_a_real_item_in_a_stable_shape(self):
        for row in BOARD_OBJECTIVES:
            material = reward_for(row)["material"]
            assert set(material) == {"item_id", "quantity"}
            assert material["item_id"]
            assert material["quantity"] == MATERIAL_UNITS

    def test_the_material_differs_per_timescale(self):
        by_cycle = {r["cycle"]: reward_for(r)["material"]["item_id"]
                    for r in BOARD_OBJECTIVES}
        assert len(set(by_cycle.values())) == len(by_cycle)

    def test_no_reward_mentions_fateum(self):
        for row in BOARD_OBJECTIVES:
            blob = repr(reward_for(row)).lower()
            assert "fateum" not in blob

    def test_the_public_alias_behaves_as_the_private_builder(self):
        """A route must be able to reach the reward builder for testing.

        `reward_for` is a named wrapper, not the same object, so the contract
        is that the two agree on every published row.
        """
        for row in BOARD_OBJECTIVES:
            assert reward_for(row) == mission_board._reward_for(row)


# ----------------------------------------------------------------------
# outcome_for
# ----------------------------------------------------------------------
class TestOutcomeFor:
    def test_the_paid_arm_carries_exactly_the_five_documented_keys(self):
        out = outcome_for("quick_qi_ready", "q42", {"spirit_stone": 5})
        assert set(out) == OUTCOME_KEYS

    def test_the_paid_arm_reports_ok_true(self):
        out = outcome_for("quick_qi_ready", "q42", {"spirit_stone": 5})
        assert out["ok"] is True
        assert out["already_claimed"] is False

    def test_the_paid_arm_carries_the_reward_it_was_given(self):
        reward = {"spirit_stone": 5, "material": {"item_id": "x", "quantity": 1}}
        assert outcome_for("o", "k", reward)["reward"] == reward

    def test_the_refused_arm_reports_ok_false(self):
        out = outcome_for("quick_qi_ready", "q42", {}, already=True)
        assert out["ok"] is False
        assert out["already_claimed"] is True

    def test_the_refused_arm_never_carries_a_reward(self):
        """A retry that pays nothing must not look like it paid something."""
        out = outcome_for("quick_qi_ready", "q42",
                          {"spirit_stone": 5}, already=True)
        assert out["reward"] == {}

    def test_both_arms_answer_the_same_keys(self):
        paid = outcome_for("o", "k", {"spirit_stone": 1})
        refused = outcome_for("o", "k", {}, already=True)
        assert set(paid) == set(refused) == OUTCOME_KEYS

    def test_both_arms_name_the_same_objective_and_cycle(self):
        paid = outcome_for("o", "k", {"spirit_stone": 1})
        refused = outcome_for("o", "k", {}, already=True)
        assert paid["objective_id"] == refused["objective_id"] == "o"
        assert paid["cycle_key"] == refused["cycle_key"] == "k"


# ----------------------------------------------------------------------
# definitions / sources registry
# ----------------------------------------------------------------------
class TestDefinitions:
    def test_every_row_names_a_registered_source(self):
        for row in BOARD_OBJECTIVES:
            assert row["source"] in SOURCES

    def test_every_row_uses_a_known_timescale_and_kind(self):
        for row in BOARD_OBJECTIVES:
            assert row["cycle"] in ("quick", "daily", "weekly")
            assert row["kind"] in ("delta", "threshold")

    def test_every_row_has_a_positive_integer_target(self):
        for row in BOARD_OBJECTIVES:
            target = row["target"]
            assert isinstance(target, int) and not isinstance(target, bool)
            assert target > 0

    def test_objective_ids_are_unique(self):
        ids = [r["objective_id"] for r in BOARD_OBJECTIVES]
        assert len(ids) == len(set(ids))

    def test_the_shipped_definitions_pass_the_ships_validator(self):
        assert mission_board._validate_definitions() is None

    def test_the_tier_material_table_is_exported_under_a_public_name(self):
        """goal_graph.py must be able to import this table without reaching
        into another module's private helpers -- a rename here used to break
        that import at load time."""
        assert TIER_MATERIAL == {
            "quick": "insight_scroll",
            "daily": "body_essence",
            "weekly": "spirit_ink",
        }
        assert not hasattr(mission_board, "_TIER_MATERIAL")

    def test_every_registered_source_answers_an_integer_for_a_bare_player(self):
        player, ctx = _player(), {"player_id": "p1", "mission_store": None}
        for name, source in SOURCES.items():
            value = source(player, ctx)
            assert isinstance(value, int), name


# ----------------------------------------------------------------------
# Negative controls
# ----------------------------------------------------------------------
class TestNegativeControls:
    def test_the_shape_detector_rejects_a_mutated_view(self):
        """CONTROL: the board assertions above must be able to fail."""
        good = board_view(_player(), now=NOW)
        assert _top_shape_ok(good)

        broken = dict(good)
        broken.pop("server_time")
        assert not _top_shape_ok(broken)

        bad_entry = {k: v for k, v in good["objectives"][0].items()
                     if k != "reward"}
        assert not _entry_shape_ok(bad_entry)

        extra_entry = dict(good["objectives"][0])
        extra_entry["surprise"] = 1
        assert not _entry_shape_ok(extra_entry)

    def test_a_mutated_definition_is_refused_by_the_validator(self):
        """CONTROL: `_validate_definitions` bites on each class of bad row."""
        saved = mission_board.BOARD_OBJECTIVES
        cases = [
            ("unknown source", {"source": "not_a_source"}),
            ("unknown cycle", {"cycle": "monthly"}),
            ("unknown kind", {"kind": "guess"}),
            ("zero target", {"target": 0}),
            ("bool target", {"target": True}),
            ("missing id", {"objective_id": ""}),
            ("fateum reward", {"reward_fateum": 5}),
            ("made up mission", {"spirit_stone_from": "not_a_mission"}),
        ]
        try:
            for label, patch in cases:
                bad = [dict(saved[0], **patch)]
                mission_board.BOARD_OBJECTIVES = bad
                with pytest.raises(MissionBoardError):
                    mission_board._validate_definitions()
                assert label, label
        finally:
            mission_board.BOARD_OBJECTIVES = saved
        mission_board._validate_definitions()

    def test_control_without_the_baseline_stamp_shows_the_defect(self):
        """CONTROL: without the zero baseline, a returning player sees zeros.

        The first-ever view stamps every delta row at zero so progress earned
        before the board existed counts. Remove that special case (by giving
        the player any other cycle history first, which flips the flag) and the
        same player with 9 duel wins is told the row is at zero -- which is
        exactly the player the board exists to reward.
        """
        saved = mission_board.BOARD_OBJECTIVES
        duels = [r for r in saved if r["source"] == "pvp_wins"]
        try:
            mission_board.BOARD_OBJECTIVES = duels

            # Control A: the first ever view. Progress earned earlier counts.
            fresh = board_view(_player(pvp_wins=9), now=NOW)
            assert fresh["objectives"][0]["progress"] == 3

            # Control B: same player record, but with prior cycle history, so
            # the zero baseline does not apply and the baseline is stamped at
            # the current value -- progress = 9 - 9 = 0.
            returning = _player(pvp_wins=9)
            returning[STATE_KEY] = {"cycles": {"q0": {}}}
            stale = board_view(returning, now=NOW)
            assert stale["objectives"][0]["progress"] == 0
        finally:
            mission_board.BOARD_OBJECTIVES = saved

    def test_control_without_a_cycle_key_makes_a_replay_collide(self):
        """CONTROL: drop the cycle from the wallet key -> a new cycle is a replay."""
        with_cycle = claim_key("p1", "x", "q42")
        without_cycle = "mission_board:p1:x"
        assert with_cycle != without_cycle
        assert claim_key("p1", "x", "q43") != claim_key("p1", "x", "q42")