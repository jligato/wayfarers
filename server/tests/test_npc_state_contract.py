"""
Contract tests for ``server/npc_state.py``.

WHICH MODULE THIS FILE ACTUALLY TESTS
-------------------------------------
``npc_state.py`` declares **no HTTP routes**. It is the pure state library
``server/npc_routes.py`` calls inside a store transaction. So the contract for
this file is the contract of the dicts and records its functions hand back,
which the HTTP layer serialises verbatim:

* the persisted shape -- one dict under the ``npc_state`` key of the player
  row, holding ``standing`` / ``met`` / ``offers`` / ``favours`` / ``leads``,
  plus ``active_favour_id`` while a request is open;
* the favour lifecycle -- offered -> accepted -> settled / failed / refused,
  where only ``accepted`` runs a deadline, and only terminal states block a
  re-offer;
* error cases -- an unknown favour id raises ``KeyError``, an illegal
  transition raises ``ValueError`` (which the router turns into a 409, not a
  500), and the COST of accepting is charged exactly once;
* survival across a "restart" -- load/save round-trip through a store that
  hands out copies on read outside a transaction, which is the bug class this
  module exists to prevent.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle && tools/wc_pytest.sh \
        server/tests/test_npc_state_contract.py

This file imports only ``npc_state`` and ``npc_roster_config`` themselves. It
never imports ``server.main``, so a half-written ``main.py`` on a shared tree
cannot abort this suite's collection.

WHAT IS PINNED, AND HOW
-----------------------
* Reply key SETS use ``<=`` (subset), not ``==``. A client reads these names, so
  ADDING a field is safe; REMOVING or RENAMING one breaks a caller and must go
  red.
* Numbers are read from ``npc_roster_config`` at run time (``FAVOURS``,
  ``FAVOUR_ORDER``, ``clamp_standing``, ``favour_deadline_hours``). Rebalancing
  the game leaves this file green.
* Nothing is frozen to a literal balance, cost or deadline.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import npc_state as ns  # noqa: E402
from npc_roster_config import (  # noqa: E402
    FAVOUR_ORDER,
    FAVOURS,
    NPC_ROSTER,
    STANDING_DISTRUSTED,
    STANDING_MAX,
    STANDING_MIN,
    STANDING_TRUSTED,
)

STATE_CONTAINER_KEYS = {"standing", "met", "offers", "favours", "leads"}
FAVOUR_VIEW_KEYS = {
    "favour_id", "npc_id", "ask", "status", "cost_npc_id", "cost_amount",
    "cost_standing", "gain_standing", "due_at", "hours_left", "settled_line",
}
ROSTER_ENTRY_KEYS = {
    "id", "name", "title", "faction", "wants", "voice", "standing", "band",
    "visits", "last_seen", "at_stake", "has_unmet_lead",
}
FAVOUR_RECORD_KEYS = {"status", "accepted_at", "due_at", "resolved_at"}
MEETING_RECORD_KEYS = {"visits", "last_seen"}


# === helpers ==============================================================

def _player(**over):
    """A player row shaped like the ones the routes hand to these functions."""
    row = {
        "id": "p1",
        "cultivation_realm": "mortal",
        "cultivation_stage": "early",
        "progress": 0.0,
        "sect_id": None,
    }
    row.update(over)
    return row


def _state():
    return ns.load_state({})


class CopyOnReadStore:
    """A stand-in for ``server/player_store.py``.

    Reads outside ``transaction()`` hand back a deep copy, and a write to that
    copy evaporates. Inside a transaction the staged row IS the live row and the
    transaction's exit is the writer. Reproducing that shape here is what lets a
    test prove state survives a restart rather than proving a dict still holds
    its own values.
    """

    def __init__(self, rows=None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.transactions_opened = 0

    def __getitem__(self, player_id):
        if player_id not in self.rows:
            raise KeyError(player_id)
        return json.loads(json.dumps(self.rows[player_id]))  # a detached copy

    def __setitem__(self, player_id, row):
        self.rows[player_id] = dict(row)

    def transaction(self, player_id):
        store = self

        class _Scope:
            def __enter__(self):
                store.transactions_opened += 1
                self.row = store.rows[player_id]
                return _LiveView(self.row)

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Scope()


class _LiveView:
    """What the staged row looks like to a handler inside the transaction."""

    def __init__(self, row):
        self.row = row

    def __getitem__(self, player_id):
        # The row carries its own id, and an unknown player must raise exactly
        # as the real store does so the route's 404 path is exercised.
        if player_id not in self.row.get("id", ""):
            raise KeyError(player_id)
        return self.row  # the LIVE staged row, not a copy


def _open_talk(store, player_id, npc_id):
    """The two-step a route does: read the flags, THEN record the meeting.

    Returns the talk reply, exactly the shape ``npc_routes.talk`` builds, so a
    test can compare one conversation to the next.
    """
    def _work(state, player):
        ns.expire_favours(state)
        first = ns.first_meeting(state, npc_id)
        changed = ns.changed_since_last_meeting(state, npc_id, player)
        meeting = ns.note_meeting(state, npc_id, player)
        return {
            "text": ns.greeting_for(
                state, npc_id, player, is_first_meeting=first, changed=changed
            ),
            "first": first,
            "changed": changed,
            "visits": int(meeting.get("visits", 0)),
        }

    return _mutate(store, player_id, _work)


def _mutate(store, player_id, fn):
    """Mirror ``npc_routes._mutate``: load, mutate in place, save, transaction."""
    with store.transaction(player_id) as scoped:
        player = scoped[player_id]
        state = ns.load_state(player)
        result = fn(state, player)
        ns.save_state(player, state)
        return result


# === load_state / save_state: the persistence shape ========================

def test_load_state_creates_every_documented_container():
    """A player who has never met anyone still gets a complete state dict."""
    state = ns.load_state({})
    assert STATE_CONTAINER_KEYS <= set(state)
    assert state["standing"] == {} and state["met"] == {}
    assert state["offers"] == {} and state["favours"] == {} and state["leads"] == []
    assert "active_favour_id" not in state


def test_load_state_returns_the_object_already_inside_the_row():
    """In-place mutation is only durable if this is the row's own dict."""
    player = {}
    state = ns.load_state(player)
    assert state is player[ns.STATE_KEY]
    state["standing"]["village_elder"] = 2
    assert player[ns.STATE_KEY]["standing"]["village_elder"] == 2


def test_load_state_repairs_every_malformed_container_in_place():
    """An old or corrupt row reads as a first meeting, never as a crash."""
    player = {
        ns.STATE_KEY: {
            "standing": "corrupt",
            "met": ["not", "a", "dict"],
            "offers": 7,
            "favours": None,
            "leads": {"not": "a list"},
        }
    }
    state = ns.load_state(player)
    assert state is player[ns.STATE_KEY]
    assert state["standing"] == {} and state["met"] == {}
    assert state["offers"] == {} and state["favours"] == {} and state["leads"] == []


@pytest.mark.parametrize("raw", [None, 0, "", [], "npc_state", 3.5, True])
def test_load_state_replaces_a_non_dict_state_with_a_fresh_one(raw):
    player = {ns.STATE_KEY: raw}
    state = ns.load_state(player)
    assert isinstance(state, dict)
    assert STATE_CONTAINER_KEYS <= set(state)
    assert player[ns.STATE_KEY] is state


def test_load_state_keeps_a_tuple_leads_value_as_a_fresh_list():
    """A tuple is not a list, so it is replaced rather than mutated in place."""
    player = {ns.STATE_KEY: {"leads": ("a", "b")}}
    state = ns.load_state(player)
    assert isinstance(state["leads"], list) and state["leads"] == []


def test_load_state_preserves_well_formed_values_it_did_not_need_to_repair():
    """Normalisation must not wipe a real player's history."""
    player = {
        ns.STATE_KEY: {
            "standing": {"village_elder": 2},
            "met": {"village_elder": {"visits": 3, "last_seen": 123.0}},
            "offers": {"qiu_water_levy_writ": {"first_offered": 10.0}},
            "favours": {"qiu_water_levy_writ": {"status": "accepted"}},
            "leads": ["a lead"],
        }
    }
    state = ns.load_state(player)
    assert state["standing"] == {"village_elder": 2}
    assert state["met"]["village_elder"]["visits"] == 3
    assert state["offers"]["qiu_water_levy_writ"]["first_offered"] == 10.0
    assert state["favours"]["qiu_water_levy_writ"]["status"] == "accepted"
    assert state["leads"] == ["a lead"]


def test_save_state_writes_the_state_onto_the_row_verbatim():
    """One door, no copy: the row ends up holding exactly this object."""
    player = {}
    state = _state()
    ns.save_state(player, state)
    assert player[ns.STATE_KEY] is state


def test_save_state_replaces_a_previous_state_entirely():
    player = {ns.STATE_KEY: {"standing": {"village_elder": 3}, "stale": 1}}
    fresh = _state()
    ns.save_state(player, fresh)
    assert player[ns.STATE_KEY] is fresh
    assert "stale" not in player[ns.STATE_KEY]


def test_the_whole_state_survives_a_json_round_trip():
    """It rides in ``extra_state``, so it has to be plain JSON -- no sets, no
    custom objects. A datetime or a set here would be a 500 on save."""
    state = _state()
    ns.adjust_standing(state, "village_elder", 2)
    ns.note_meeting(state, "village_elder", _player())
    ns.accept_favour(state, FAVOUR_ORDER[0])
    revived = json.loads(json.dumps(state))
    assert revived == state
    assert revived["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_ACCEPTED


def test_state_survives_a_restart_through_the_store():
    """The load/mutate/save cycle inside a transaction is durable; the same
    writes made to a copy returned by a plain read are not."""
    store = CopyOnReadStore({"p1": _player()})

    ns.adjust_standing(ns.load_state(store["p1"]), "village_elder", 2)  # dropped
    assert store["p1"].get(ns.STATE_KEY) in (None, {})

    _mutate(store, "p1", lambda state, player: ns.adjust_standing(
        state, "village_elder", 2
    ))
    assert store.rows["p1"][ns.STATE_KEY]["standing"]["village_elder"] == 2
    assert store["p1"][ns.STATE_KEY]["standing"]["village_elder"] == 2


def test_a_full_conversation_survives_a_restart_end_to_end():
    """Accept a request, restart, and the deadline clock is still running."""
    store = CopyOnReadStore({"p1": _player()})
    favour_id = FAVOUR_ORDER[0]
    record = _mutate(
        store, "p1", lambda state, player: ns.accept_favour(state, favour_id)
    )
    # A "restart": a brand new store built from what was persisted.
    rebooted = CopyOnReadStore(store.rows)
    assert rebooted["p1"][ns.STATE_KEY]["favours"][favour_id] == record
    assert rebooted["p1"][ns.STATE_KEY]["active_favour_id"] == favour_id
    view = _mutate(rebooted, "p1", lambda state, player: ns.favour_view(
        state, favour_id, record["due_at"]
    ))
    assert view["status"] == ns.STATUS_ACCEPTED
    assert view["hours_left"] == pytest.approx(0.0)


# === standing =============================================================

def test_get_standing_on_an_unknown_npc_is_zero():
    assert ns.get_standing(_state(), "nobody_at_all") == 0


def test_get_standing_returns_the_stored_value():
    state = _state()
    ns.adjust_standing(state, "village_elder", 2)
    assert ns.get_standing(state, "village_elder") == 2


@pytest.mark.parametrize("stored", [None, "abc", [], {}, object()])
def test_get_standing_reads_a_junk_stored_value_as_zero(stored):
    """A stringified or null row must not become a 500."""
    state = _state()
    state["standing"]["village_elder"] = stored
    assert ns.get_standing(state, "village_elder") == 0


def test_adjust_standing_accumulates_and_returns_the_new_value():
    state = _state()
    assert ns.adjust_standing(state, "village_elder", 1) == 1
    assert ns.adjust_standing(state, "village_elder", 1) == 2
    assert ns.get_standing(state, "village_elder") == 2


def test_adjust_standing_is_signed_and_per_npc():
    state = _state()
    ns.adjust_standing(state, "village_elder", -2)
    ns.adjust_standing(state, "wandering_merchant", 1)
    assert ns.get_standing(state, "village_elder") == -2
    assert ns.get_standing(state, "wandering_merchant") == 1


def test_adjust_standing_clamps_at_both_ends():
    """Standing is a small signed integer; a long favour chain cannot escape it."""
    state = _state()
    for _ in range(20):
        ns.adjust_standing(state, "village_elder", 1)
    assert ns.get_standing(state, "village_elder") == STANDING_MAX
    for _ in range(20):
        ns.adjust_standing(state, "village_elder", -1)
    assert ns.get_standing(state, "village_elder") == STANDING_MIN


@pytest.mark.parametrize(
    "value,band",
    [
        (STANDING_TRUSTED, "trusted"),
        (STANDING_MAX, "trusted"),
        (STANDING_TRUSTED - 1, "neutral"),
        (0, "neutral"),
        (STANDING_DISTRUSTED + 1, "neutral"),
        (STANDING_DISTRUSTED, "distrusted"),
        (STANDING_MIN, "distrusted"),
    ],
)
def test_standing_band_thresholds_come_from_the_roster_config(value, band):
    state = _state()
    ns.adjust_standing(state, "village_elder", value)
    assert ns.standing_band(state, "village_elder") == band


# === meeting ==============================================================

def test_first_meeting_is_true_before_any_visit_and_false_after():
    state = _state()
    assert ns.first_meeting(state, "village_elder") is True
    ns.note_meeting(state, "village_elder", _player())
    assert ns.first_meeting(state, "village_elder") is False


def test_first_meeting_ignores_a_malformed_meeting_record():
    state = _state()
    state["met"]["village_elder"] = "corrupt"
    assert ns.first_meeting(state, "village_elder") is True


def test_note_meeting_returns_the_documented_record_and_counts_visits():
    state = _state()
    record = ns.note_meeting(state, "village_elder", _player())
    assert MEETING_RECORD_KEYS <= set(record)
    assert record["visits"] == 1 and record["last_seen"] > 0.0
    for _ in range(3):
        record = ns.note_meeting(state, "village_elder", _player())
    assert record["visits"] == 4
    assert state["met"]["village_elder"] is record


def test_note_meeting_stores_what_the_npc_last_saw():
    """``observed_realm`` / ``observed_stage`` are the returning-player hook."""
    state = _state()
    record = ns.note_meeting(state, "village_elder", _player(cultivation_realm="qi"))
    assert record["observed_realm"] == "qi"
    assert record["observed_stage"] == "early"


def test_note_meeting_without_a_player_leaves_the_observation_untouched():
    state = _state()
    record = ns.note_meeting(state, "village_elder")
    assert record["observed_realm"] is None and record["observed_stage"] is None
    assert record["visits"] == 1


def test_changed_since_last_meeting_is_false_with_nothing_recorded():
    """No observation yet is not a change -- the first-meeting line covers it."""
    state = _state()
    assert ns.changed_since_last_meeting(state, "village_elder", _player()) is False


def test_changed_since_last_meeting_is_false_when_nothing_moved():
    state = _state()
    player = _player()
    ns.note_meeting(state, "village_elder", player)
    assert ns.changed_since_last_meeting(state, "village_elder", player) is False


@pytest.mark.parametrize(
    "changed_player",
    [
        {"cultivation_realm": "qi"},
        {"cultivation_stage": "late"},
    ],
)
def test_changed_since_last_meeting_is_true_after_realm_or_stage_moved(changed_player):
    state = _state()
    ns.note_meeting(state, "village_elder", _player())
    assert ns.changed_since_last_meeting(
        state, "village_elder", _player(**changed_player)
    ) is True


def test_changed_since_last_meeting_ignores_progress():
    """Progress moves every minute; commenting on it would make an NPC comment
    on breathing."""
    state = _state()
    ns.note_meeting(state, "village_elder", _player())
    assert ns.changed_since_last_meeting(
        state, "village_elder", _player(progress=99.9)
    ) is False


# === available_favour =====================================================

def test_available_favour_offers_the_first_id_in_the_roster_order():
    state = _state()
    assert ns.available_favour(state) == FAVOUR_ORDER[0]


def test_available_favour_is_none_while_another_request_is_open():
    """One at a time on purpose: a list of four requests is a menu."""
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    assert ns.available_favour(state) is None


def test_available_favour_skips_a_terminal_favour_and_moves_on():
    state = _state()
    ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


@pytest.mark.parametrize("terminal", sorted(ns.TERMINAL_STATUSES))
def test_no_terminal_status_is_ever_offered_again(terminal):
    """Re-offering a resolved request is how a player would farm one."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": terminal}
    assert ns.available_favour(state) != FAVOUR_ORDER[0]


def test_available_favour_is_none_once_every_favour_is_resolved():
    state = _state()
    for favour_id in FAVOUR_ORDER:
        state["favours"][favour_id] = {"status": ns.STATUS_SETTLED}
    assert ns.available_favour(state) is None


def test_an_accepted_favour_blocks_a_second_offer_through_the_active_slot():
    """The one-slot rule is enforced by ``active_favour_id``, which
    ``accept_favour`` sets. Note what this does NOT do: a bare ``accepted``
    record with no active slot still leaves the ask re-offerable, so the two
    fields must be cleared together."""
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    assert state["active_favour_id"] == FAVOUR_ORDER[0]
    assert ns.available_favour(state) is None


def test_available_favour_tolerates_a_malformed_favour_record():
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = "corrupt"
    assert ns.available_favour(state) == FAVOUR_ORDER[0]


# === favour_view ==========================================================

@pytest.mark.parametrize("favour_id", FAVOUR_ORDER)
def test_favour_view_carries_every_key_the_client_draws(favour_id):
    view = ns.favour_view(_state(), favour_id)
    assert FAVOUR_VIEW_KEYS <= set(view)
    assert view["favour_id"] == favour_id
    assert view["npc_id"] == FAVOURS[favour_id]["npc"]
    assert view["ask"] == FAVOURS[favour_id]["ask"]


def test_favour_view_on_an_unknown_favour_is_none():
    """None, not a raise: the router offers this for a possibly-absent id."""
    assert ns.favour_view(_state(), "no_such_favour") is None


def test_favour_view_prices_the_cost_as_a_person_not_a_number_of_stones():
    """The design forbids buying a favour with currency, so there is no money
    field at all -- the cost is standing with a named NPC."""
    view = ns.favour_view(_state(), FAVOUR_ORDER[0])
    assert view["cost_npc_id"] == FAVOURS[FAVOUR_ORDER[0]]["cost_npc"]
    assert view["cost_amount"] == int(FAVOURS[FAVOUR_ORDER[0]]["cost_amount"])
    assert not any("stone" in key or "price" in key or "cost_gold" in key
                   for key in view)


def test_favour_view_of_an_untouched_favour_reads_as_offered_with_no_deadline():
    view = ns.favour_view(_state(), FAVOUR_ORDER[0])
    assert view["status"] == ns.STATUS_OFFERED
    assert view["due_at"] is None and view["hours_left"] is None


def test_favour_view_counts_down_the_hours_left():
    state = _state()
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    view = ns.favour_view(state, FAVOUR_ORDER[0], record["due_at"] - 3600.0)
    assert view["hours_left"] == pytest.approx(1.0)


def test_favour_view_never_reports_negative_hours_left():
    state = _state()
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    view = ns.favour_view(state, FAVOUR_ORDER[0], record["due_at"] + 10_000.0)
    assert view["hours_left"] == 0.0


def test_favour_view_reports_the_current_cost_standing():
    state = _state()
    cost_npc = FAVOURS[FAVOUR_ORDER[0]]["cost_npc"]
    ns.adjust_standing(state, cost_npc, 2)
    assert ns.favour_view(state, FAVOUR_ORDER[0])["cost_standing"] == 2


def test_favour_view_tolerates_a_non_numeric_stored_deadline():
    """A stringified row must not 500 the roster screen."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": ns.STATUS_ACCEPTED, "due_at": "soon"}
    view = ns.favour_view(state, FAVOUR_ORDER[0])
    assert view["due_at"] is None and view["hours_left"] is None


# === a deadline that cannot be read =======================================
#
# Measured lockout: a saved deadline of ``inf`` / ``-inf`` / ``nan`` used to
# pass the ``isinstance(due_at, (int, float))`` guard and land in the reply.
# Standard JSON has no literal for either, so the roster reply could not be
# encoded at all, and the countdown never reached zero -- the player stayed
# locked out of the request for good. An unreadable deadline now reads as no
# deadline at all, and the sweep closes it, which is the same rule the string
# ``"soon"`` already gets.

UNREADABLE_DEADLINES = [float("inf"), float("-inf"), float("nan"), "soon"]
UNREADABLE_IDS = ["inf", "neg_inf", "nan", "string"]


def _accepted_with_deadline(due_at):
    """An open request whose saved deadline is whatever the test hands over."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {
        "status": ns.STATUS_ACCEPTED, "due_at": due_at,
    }
    state["active_favour_id"] = FAVOUR_ORDER[0]
    return state


def _assert_standard_json(reply):
    """``allow_nan=False`` is what forbids ``inf`` and ``nan`` on the way out."""
    return json.loads(json.dumps(reply, allow_nan=False))


@pytest.mark.parametrize("due_at", UNREADABLE_DEADLINES, ids=UNREADABLE_IDS)
def test_favour_view_of_an_unreadable_deadline_reports_none(due_at):
    state = _accepted_with_deadline(due_at)
    view = _assert_standard_json(ns.favour_view(state, FAVOUR_ORDER[0]))
    assert view["due_at"] is None and view["hours_left"] is None


@pytest.mark.parametrize("due_at", UNREADABLE_DEADLINES, ids=UNREADABLE_IDS)
def test_favour_view_reports_no_non_finite_number_anywhere(due_at):
    """The whole reply has to be encodable, not just the deadline field."""
    view = _assert_standard_json(ns.favour_view(state=_accepted_with_deadline(due_at),
                                               favour_id=FAVOUR_ORDER[0]))
    assert not [k for k, v in view.items() if isinstance(v, float) and not math.isfinite(v)]


@pytest.mark.parametrize("due_at", UNREADABLE_DEADLINES, ids=UNREADABLE_IDS)
def test_expire_favours_closes_a_request_whose_deadline_cannot_be_read(due_at):
    state = _accepted_with_deadline(due_at)
    assert ns.expire_favours(state, 10_000.0) == [FAVOUR_ORDER[0]]
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_FAILED


@pytest.mark.parametrize("due_at", UNREADABLE_DEADLINES, ids=UNREADABLE_IDS)
def test_expire_favours_frees_the_slot_and_the_next_request(due_at):
    """The lockout itself: a stuck request hides every later one."""
    state = _accepted_with_deadline(due_at)
    assert ns.available_favour(state) is None
    ns.expire_favours(state, 10_000.0)
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


@pytest.mark.parametrize("due_at", UNREADABLE_DEADLINES, ids=UNREADABLE_IDS)
def test_an_unreadable_deadline_is_closed_exactly_like_the_string_case(due_at):
    """Same rule for every unreadable deadline: a plain expiry, gain and all.

    The cost is NOT waived here. The suite already pins that a request closed
    by the sweep costs the asker the same step as one that ran out of time,
    and an unreadable deadline is closed by that same sweep -- waiving the
    cost would make a corrupt row a way to keep standing.
    """
    state = _accepted_with_deadline(due_at)
    ns.expire_favours(state, 10_000.0)
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == -int(
        FAVOURS[FAVOUR_ORDER[0]]["gain"]
    )


def test_a_readable_deadline_still_counts_down():
    """The widening must not swallow the ordinary case."""
    accepted = _state()
    record = ns.accept_favour(accepted, FAVOUR_ORDER[0])
    state = _accepted_with_deadline(record["due_at"])
    view = ns.favour_view(state, FAVOUR_ORDER[0], record["due_at"] - 3600.0)
    assert view["due_at"] == pytest.approx(record["due_at"])
    assert view["hours_left"] == pytest.approx(1.0)


# === accept_favour ========================================================

def test_accept_favour_charges_the_cost_exactly_once_at_acceptance():
    state = _state()
    favour = FAVOURS[FAVOUR_ORDER[0]]
    before = ns.get_standing(state, favour["cost_npc"])
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    assert FAVOUR_RECORD_KEYS <= set(record)
    assert record["status"] == ns.STATUS_ACCEPTED
    assert ns.get_standing(state, favour["cost_npc"]) == before - int(favour["cost_amount"])


def test_accept_favour_starts_the_deadline_from_now():
    state = _state()
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    from npc_roster_config import favour_deadline_hours

    expected = record["accepted_at"] + favour_deadline_hours(FAVOUR_ORDER[0]) * 3600.0
    assert record["due_at"] == pytest.approx(expected)
    assert record["resolved_at"] is None


def test_accept_favour_marks_it_active_and_remembers_when_it_was_offered():
    state = _state()
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    assert state["active_favour_id"] == FAVOUR_ORDER[0]
    assert state["offers"][FAVOUR_ORDER[0]]["first_offered"] == pytest.approx(
        record["accepted_at"]
    )


def test_accept_favour_does_not_pay_the_asker():
    """The gain is only paid on settlement; accepting is the cost, not the reward."""
    state = _state()
    asker = FAVOURS[FAVOUR_ORDER[0]]["npc"]
    before = ns.get_standing(state, asker)
    ns.accept_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, asker) == before


def test_accept_favour_refuses_an_unknown_id():
    with pytest.raises(KeyError):
        ns.accept_favour(_state(), "no_such_favour")


def test_accept_favour_refuses_a_terminal_favour_and_changes_nothing():
    state = _state()
    favour = FAVOURS[FAVOUR_ORDER[0]]
    ns.refuse_favour(state, FAVOUR_ORDER[0])
    cost_after_refusal = ns.get_standing(state, favour["cost_npc"])
    with pytest.raises(ValueError):
        ns.accept_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, favour["cost_npc"]) == cost_after_refusal


def test_accept_favour_refuses_a_second_accept_and_does_not_charge_twice():
    """The measured bug: a double-tap or a replayed request charged the cost
    again and restarted the clock, taking the third party to -2."""
    state = _state()
    favour = FAVOURS[FAVOUR_ORDER[0]]
    first = ns.accept_favour(state, FAVOUR_ORDER[0])
    standing_after_first = ns.get_standing(state, favour["cost_npc"])
    for _ in range(3):
        with pytest.raises(ValueError):
            ns.accept_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, favour["cost_npc"]) == standing_after_first
    assert state["favours"][FAVOUR_ORDER[0]]["due_at"] == pytest.approx(first["due_at"])


def test_a_failed_favour_cannot_be_accepted_again():
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    ns.fail_favour(state, FAVOUR_ORDER[0])
    with pytest.raises(ValueError):
        ns.accept_favour(state, FAVOUR_ORDER[0])


# === refuse_favour ========================================================

def test_refuse_favour_costs_the_asker_one_step_and_closes_the_offer():
    state = _state()
    asker = FAVOURS[FAVOUR_ORDER[0]]["npc"]
    record = ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert record["status"] == ns.STATUS_REFUSED
    assert record["accepted_at"] is None and record["due_at"] is None
    assert record["resolved_at"] > 0.0
    assert ns.get_standing(state, asker) == -1
    assert "active_favour_id" not in state


def test_refuse_favour_charges_no_cost_to_anyone_else():
    state = _state()
    cost_npc = FAVOURS[FAVOUR_ORDER[0]]["cost_npc"]
    ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, cost_npc) == 0


def test_refuse_favour_closes_an_already_accepted_request():
    """Refusing has to be genuinely available or the whole shape is theatre."""
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    record = ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert record["status"] == ns.STATUS_REFUSED
    assert "active_favour_id" not in state


def test_refuse_favour_refuses_an_unknown_id():
    with pytest.raises(KeyError):
        ns.refuse_favour(_state(), "no_such_favour")


@pytest.mark.parametrize("terminal", sorted(ns.TERMINAL_STATUSES))
def test_refuse_favour_refuses_a_terminal_favour(terminal):
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": terminal}
    asker = FAVOURS[FAVOUR_ORDER[0]]["npc"]
    with pytest.raises(ValueError):
        ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, asker) == 0


def test_a_refused_favour_stays_refused_on_a_replay():
    state = _state()
    ns.refuse_favour(state, FAVOUR_ORDER[0])
    first = dict(state["favours"][FAVOUR_ORDER[0]])
    with pytest.raises(ValueError):
        ns.refuse_favour(state, FAVOUR_ORDER[0])
    assert state["favours"][FAVOUR_ORDER[0]] == first
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == -1


# === settle_favour / fail_favour ==========================================

def _accepted(favour_id):
    state = _state()
    record = ns.accept_favour(state, favour_id)
    return state, record


def test_settle_favour_pays_the_asker_and_files_the_lead():
    state, record = _accepted(FAVOUR_ORDER[0])
    favour = FAVOURS[FAVOUR_ORDER[0]]
    settled = ns.settle_favour(state, FAVOUR_ORDER[0])
    assert settled is record
    assert settled["status"] == ns.STATUS_SETTLED
    assert settled["resolved_at"] > 0.0
    assert ns.get_standing(state, favour["npc"]) == int(favour["gain"])
    assert favour["lead"] in state["leads"]


def test_settle_favour_files_the_lead_exactly_once():
    """The dedup is inside settle_favour, and settle_favour is itself refused
    once resolved -- so the two together keep the lead list from doubling."""
    state, _ = _accepted(FAVOUR_ORDER[0])
    lead = FAVOURS[FAVOUR_ORDER[0]]["lead"]
    ns.settle_favour(state, FAVOUR_ORDER[0])
    for _ in range(3):
        with pytest.raises(ValueError):
            ns.settle_favour(state, FAVOUR_ORDER[0])
    assert state["leads"] == [lead]


def test_settle_favour_clears_the_active_request():
    state, _ = _accepted(FAVOUR_ORDER[0])
    ns.settle_favour(state, FAVOUR_ORDER[0])
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


def test_settle_favour_refuses_a_favour_that_was_never_accepted():
    state = _state()
    with pytest.raises(ValueError):
        ns.settle_favour(state, FAVOUR_ORDER[0])


def test_settle_favour_refuses_a_terminal_favour():
    state = _state()
    ns.refuse_favour(state, FAVOUR_ORDER[0])
    with pytest.raises(ValueError):
        ns.settle_favour(state, FAVOUR_ORDER[0])


def test_settle_favour_refuses_an_unknown_id():
    state = _state()
    with pytest.raises(KeyError):
        ns.settle_favour(state, "no_such_favour")


def test_settle_favour_pays_the_asker_only_once_on_a_replay():
    state, _ = _accepted(FAVOUR_ORDER[0])
    ns.settle_favour(state, FAVOUR_ORDER[0])
    paid = ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"])
    for _ in range(3):
        with pytest.raises(ValueError):
            ns.settle_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == paid


def test_fail_favour_costs_the_asker_the_step_they_would_have_gained():
    state, record = _accepted(FAVOUR_ORDER[0])
    favour = FAVOURS[FAVOUR_ORDER[0]]
    failed = ns.fail_favour(state, FAVOUR_ORDER[0])
    assert failed is record
    assert failed["status"] == ns.STATUS_FAILED
    assert failed["resolved_at"] > 0.0
    assert ns.get_standing(state, favour["npc"]) == -int(favour["gain"])
    assert state["leads"] == []


def test_failing_is_worse_than_never_agreeing():
    """The design claim that makes failing a real decision: the asker ends up
    below where they started, while refusing only costs one step."""
    failed_state = _state()
    ns.accept_favour(failed_state, FAVOUR_ORDER[0])
    ns.fail_favour(failed_state, FAVOUR_ORDER[0])
    refused_state = _state()
    ns.refuse_favour(refused_state, FAVOUR_ORDER[0])
    gain = int(FAVOURS[FAVOUR_ORDER[0]]["gain"])
    assert ns.get_standing(failed_state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == -gain
    assert ns.get_standing(refused_state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == -1


def test_fail_favour_clears_the_active_request_and_frees_the_next_ask():
    state, _ = _accepted(FAVOUR_ORDER[0])
    ns.fail_favour(state, FAVOUR_ORDER[0])
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


def test_fail_favour_refuses_a_favour_that_was_never_accepted():
    state = _state()
    with pytest.raises(ValueError):
        ns.fail_favour(state, FAVOUR_ORDER[0])


def test_fail_favour_refuses_a_terminal_favour_and_costs_nothing_more():
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    ns.settle_favour(state, FAVOUR_ORDER[0])
    paid = ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"])
    with pytest.raises(ValueError):
        ns.fail_favour(state, FAVOUR_ORDER[0])
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == paid


def test_fail_favour_refuses_an_unknown_id():
    with pytest.raises(KeyError):
        ns.fail_favour(_state(), "no_such_favour")


# === expire_favours =======================================================

def test_expire_favours_fails_an_overdue_request_and_returns_its_id():
    state, record = _accepted(FAVOUR_ORDER[0])
    expired = ns.expire_favours(state, record["due_at"] + 1.0)
    assert expired == [FAVOUR_ORDER[0]]
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_FAILED


def test_expire_favours_leaves_a_request_inside_its_deadline_alone():
    state, record = _accepted(FAVOUR_ORDER[0])
    assert ns.expire_favours(state, record["due_at"] - 1.0) == []
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_ACCEPTED


def test_expire_favours_treats_the_deadline_second_as_expired():
    state, record = _accepted(FAVOUR_ORDER[0])
    assert ns.expire_favours(state, record["due_at"]) == [FAVOUR_ORDER[0]]


def test_expire_favours_costs_the_asker_and_frees_the_slot():
    state, record = _accepted(FAVOUR_ORDER[0])
    ns.expire_favours(state, record["due_at"] + 1.0)
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == -int(
        FAVOURS[FAVOUR_ORDER[0]]["gain"]
    )
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


def test_expire_favours_on_an_empty_state_is_a_no_op():
    state = _state()
    assert ns.expire_favours(state) == []
    assert STATE_CONTAINER_KEYS <= set(state)


@pytest.mark.parametrize("terminal", sorted(ns.TERMINAL_STATUSES))
def test_expire_favours_ignores_a_resolved_request(terminal):
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": terminal, "due_at": 0.0}
    assert ns.expire_favours(state, 10_000.0) == []
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == terminal


def test_expire_favours_fails_an_accepted_request_with_no_deadline():
    """A malformed deadline is not a licence to keep it open forever."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": ns.STATUS_ACCEPTED, "due_at": "soon"}
    assert ns.expire_favours(state, 0.0) == [FAVOUR_ORDER[0]]
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_FAILED


def test_expire_favours_skips_a_malformed_record_without_crashing():
    state = _state()
    state["favours"]["junk"] = "not a dict"
    assert ns.expire_favours(state, 10_000.0) == []


def test_expire_favours_is_idempotent_on_a_second_pass():
    state, record = _accepted(FAVOUR_ORDER[0])
    assert ns.expire_favours(state, record["due_at"] + 1.0) == [FAVOUR_ORDER[0]]
    after = ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"])
    assert ns.expire_favours(state, record["due_at"] + 2.0) == []
    assert ns.get_standing(state, FAVOURS[FAVOUR_ORDER[0]]["npc"]) == after


# === what the NPC says ====================================================

def test_observe_player_uses_only_fields_the_row_really_has():
    """An NPC that reacts to a number that does not exist is worse than silence."""
    line = ns.observe_player(_player(cultivation_realm="qi", cultivation_stage="late"))
    assert "qi" in line and "late" in line


def test_observe_player_names_the_sect_only_when_there_is_one():
    assert "colours" in ns.observe_player(_player(sect_id="light_sect"))
    assert "no sect behind you" in ns.observe_player(_player(sect_id=None))


def test_observe_player_survives_a_row_with_no_cultivation_fields_at_all():
    line = ns.observe_player({"id": "p1"})
    assert isinstance(line, str) and line


@pytest.mark.parametrize("progress", [0, 10.0, 49.9, 50.0, 99.9, 100.0, 5000])
def test_observe_player_handles_any_progress_value(progress):
    line = ns.observe_player(_player(progress=progress))
    assert isinstance(line, str) and line.endswith(".")


def test_observe_player_survives_a_junk_progress_value():
    for junk in (None, "lots", [1], {}):
        assert isinstance(ns.observe_player(_player(progress=junk)), str)


def test_greeting_for_uses_the_neutral_greeting_by_default():
    state = _state()
    npc_id = FAVOURS[FAVOUR_ORDER[0]]["npc"]
    text = ns.greeting_for(state, npc_id, _player(), is_first_meeting=False,
                           changed=False)
    assert text == NPC_ROSTER[npc_id]["greeting"]


def test_greeting_for_switches_to_the_warm_and_cold_lines_at_the_bands():
    state = _state()
    npc_id = "village_elder"
    player = _player()
    ns.adjust_standing(state, npc_id, STANDING_TRUSTED)
    assert ns.greeting_for(state, npc_id, player, False, False) == NPC_ROSTER[npc_id]["warm"]
    ns.adjust_standing(state, npc_id, STANDING_MIN - STANDING_TRUSTED)
    assert ns.greeting_for(state, npc_id, player, False, False) == NPC_ROSTER[npc_id]["cold"]


def test_a_first_meeting_greeting_includes_the_reading_of_the_player():
    state = _state()
    text = ns.greeting_for(state, "village_elder", _player(), is_first_meeting=True,
                           changed=False)
    assert text.startswith(NPC_ROSTER["village_elder"]["greeting"])
    assert "You are mortal" in text


def test_a_changed_greeting_includes_the_reading_of_the_player():
    state = _state()
    text = ns.greeting_for(state, "village_elder", _player(), is_first_meeting=False,
                           changed=True)
    assert "You are mortal" in text


def test_greeting_for_an_unknown_npc_still_returns_a_string():
    """The route 404s an unknown npc, but the library must not raise here."""
    assert isinstance(ns.greeting_for(_state(), "nobody", _player()), str)


def test_request_line_is_the_ask_verbatim():
    for favour_id in FAVOUR_ORDER:
        assert ns.request_line(_state(), favour_id) == FAVOURS[favour_id]["ask"]


def test_request_line_raises_on_an_unknown_favour():
    with pytest.raises(KeyError):
        ns.request_line(_state(), "no_such_favour")


@pytest.mark.parametrize("event", ["accept", "refuse", "settle", "fail"])
def test_respond_line_returns_the_configured_line(event):
    favour_id = FAVOUR_ORDER[0]
    assert ns.respond_line(favour_id, event) == FAVOURS[favour_id][f"on_{event}"]


def test_respond_line_on_an_unknown_event_is_empty_not_a_raise():
    assert ns.respond_line(FAVOUR_ORDER[0], "shrug") == ""
    assert ns.respond_line("no_such_favour", "accept") == ""


# === roster_entry / npc_ids ===============================================

def test_npc_ids_matches_the_roster_config():
    assert set(ns.npc_ids()) == set(NPC_ROSTER)


@pytest.mark.parametrize("npc_id", sorted(NPC_ROSTER))
def test_roster_entry_carries_every_key_the_roster_screen_draws(npc_id):
    entry = ns.roster_entry(_state(), npc_id)
    assert ROSTER_ENTRY_KEYS <= set(entry)
    assert entry["id"] == npc_id
    assert entry["name"] == NPC_ROSTER[npc_id]["name"]
    assert entry["faction"] == NPC_ROSTER[npc_id]["faction"]
    assert entry["standing"] == 0 and entry["band"] == "neutral"
    assert entry["visits"] == 0 and entry["last_seen"] == 0.0


def test_roster_entry_on_an_unknown_npc_is_empty():
    """The route filters empty rows out; an unknown id must not raise."""
    assert ns.roster_entry(_state(), "nobody_at_all") == {}


def test_roster_entry_reports_visit_count_and_last_seen():
    state = _state()
    record = ns.note_meeting(state, "village_elder", _player())
    entry = ns.roster_entry(state, "village_elder")
    assert entry["visits"] == 1
    assert entry["last_seen"] == pytest.approx(record["last_seen"])


def test_roster_entry_shows_the_open_request_on_the_asking_npc_only():
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    asker = FAVOURS[FAVOUR_ORDER[0]]["npc"]
    at_stake = ns.roster_entry(state, asker)
    assert at_stake["has_unmet_lead"] is True
    assert FAVOUR_VIEW_KEYS <= set(at_stake["at_stake"])
    assert at_stake["at_stake"]["status"] == ns.STATUS_ACCEPTED
    for other in NPC_ROSTER:
        if other != asker:
            assert ns.roster_entry(state, other)["at_stake"] is None


def test_roster_entry_has_unmet_lead_is_false_with_nothing_open():
    assert ns.roster_entry(_state(), "village_elder")["has_unmet_lead"] is False


def test_roster_entry_offers_the_next_request_under_the_asking_npc():
    """Before anything is accepted the open ask belongs to the first NPC in the
    roster order -- so that NPC's row carries the request."""
    state = _state()
    asker = FAVOURS[ns.available_favour(state)]["npc"]
    assert ns.roster_entry(state, asker)["at_stake"] is not None


def test_roster_entry_tolerates_a_malformed_meeting_record():
    state = _state()
    state["met"]["village_elder"] = "corrupt"
    entry = ns.roster_entry(state, "village_elder")
    assert entry["visits"] == 0 and entry["last_seen"] == 0.0


def test_roster_entry_tolerates_a_malformed_active_favour_id():
    """A dangling id must not 500 the roster screen."""
    state = _state()
    state["active_favour_id"] = "a_favour_that_was_deleted"
    entry = ns.roster_entry(state, "village_elder")
    assert entry["at_stake"] is None
    assert entry["has_unmet_lead"] is True


# === the whole conversation, through the store ===========================

def test_a_second_conversation_answers_differently_from_the_first():
    """The reason `first` and `changed` are read before `note_meeting`: the
    returning-player observation must actually be reachable."""
    store = CopyOnReadStore({"p1": _player(cultivation_realm="mortal")})
    first_talk = _open_talk(store, "p1", "village_elder")
    assert first_talk["first"] is True and first_talk["visits"] == 1
    assert "You are mortal" in first_talk["text"]

    store.rows["p1"]["cultivation_realm"] = "qi"
    same_talk = _open_talk(store, "p1", "village_elder")
    assert same_talk["first"] is False and same_talk["changed"] is True
    assert same_talk["visits"] == 2
    assert "You are mortal" not in same_talk["text"]
    assert "You are qi" in same_talk["text"]

    unchanged_talk = _open_talk(store, "p1", "village_elder")
    assert unchanged_talk["first"] is False and unchanged_talk["changed"] is False
    assert unchanged_talk["visits"] == 3
    assert "You are qi" not in unchanged_talk["text"]
    assert unchanged_talk["text"] == NPC_ROSTER["village_elder"]["greeting"]


def test_a_proximity_poll_does_not_count_as_a_conversation():
    """`check_npc_proximity` polls every few seconds; counting it would make
    "how well do they know you" meaningless. Only note_meeting increments."""
    store = CopyOnReadStore({"p1": _player()})
    for _ in range(5):
        _mutate(store, "p1", lambda state, player: ns.expire_favours(state))
    assert ns.roster_entry(_state(), "village_elder")["visits"] == 0
    assert "npc_state" not in store.rows["p1"] or \
        store.rows["p1"]["npc_state"]["met"] == {}


def test_the_full_lifecycle_leaves_the_expected_persisted_shape():
    """Accept, settle, then read the row back: standing moved by the documented
    amounts, the lead is filed, and no bookkeeping field is left dangling."""
    store = CopyOnReadStore({"p1": _player()})
    favour_id = FAVOUR_ORDER[0]
    favour = FAVOURS[favour_id]
    _mutate(store, "p1", lambda state, player: ns.accept_favour(state, favour_id))
    _mutate(store, "p1", lambda state, player: ns.settle_favour(state, favour_id))

    saved = store["p1"][ns.STATE_KEY]
    assert STATE_CONTAINER_KEYS <= set(saved)
    assert "active_favour_id" not in saved
    assert saved["favours"][favour_id]["status"] == ns.STATUS_SETTLED
    assert saved["favours"][favour_id]["accepted_at"] > 0.0
    assert saved["favours"][favour_id]["due_at"] > 0.0
    assert saved["favours"][favour_id]["resolved_at"] > 0.0
    assert saved["offers"][favour_id]["first_offered"] > 0.0
    assert favour["lead"] in saved["leads"]
    assert saved["standing"][favour["cost_npc"]] == -int(favour["cost_amount"])
    assert saved["standing"][favour["npc"]] == int(favour["gain"])
    assert saved["met"] == {}  # nobody spoke during this lifecycle


# === table-level invariants the module leans on ==========================

def test_every_status_constant_is_accounted_for():
    """A new status must be classified, or a resolved favour becomes re-offerable."""
    declared = {
        ns.STATUS_OFFERED, ns.STATUS_ACCEPTED, ns.STATUS_REFUSED,
        ns.STATUS_SETTLED, ns.STATUS_FAILED,
    }
    assert ns.TERMINAL_STATUSES <= declared
    assert ns.STATUS_ACCEPTED not in ns.TERMINAL_STATUSES
    assert ns.STATUS_OFFERED not in ns.TERMINAL_STATUSES


def test_favour_order_covers_every_favour_exactly_once():
    assert set(FAVOUR_ORDER) == set(FAVOURS)
    assert len(FAVOUR_ORDER) == len(set(FAVOUR_ORDER))


def test_every_favour_names_npcs_that_exist():
    """A dangling npc id would KeyError inside get_standing's caller."""
    for favour in FAVOURS.values():
        assert favour["npc"] in NPC_ROSTER
        assert favour["cost_npc"] in NPC_ROSTER


def test_no_favour_is_paid_in_currency():
    """The whole favour table must stay a relationship economy: a favour priced
    in stones is a purchase, not a sacrifice."""
    for favour_id, favour in FAVOURS.items():
        assert isinstance(favour["lead"], str) and favour["lead"]
        assert not any(
            "stone" in key or "gold" in key or "price" in key for key in favour
        ), favour_id


def test_every_favour_has_a_line_for_each_of_the_four_events():
    for favour_id in FAVOUR_ORDER:
        for event in ("accept", "refuse", "settle", "fail"):
            assert ns.respond_line(favour_id, event), f"{favour_id}/{event}"


def test_every_roster_entry_carries_the_lines_a_greeting_can_choose_from():
    """greeting_for falls back to ``greeting`` but reads ``warm``/``cold``; a
    missing key would silently make every band sound the same."""
    for npc_id, entry in NPC_ROSTER.items():
        for key in ("greeting", "warm", "cold", "name", "title", "faction",
                    "wants", "voice"):
            assert entry.get(key), f"{npc_id}.{key}"


def test_a_favour_id_can_appear_in_the_order_without_being_in_the_table():
    """A config table and the order that walks it can drift. ``available_favour``
    skips the id it cannot find and offers the next real one, instead of raising
    inside the roster read."""
    monkeypatched_order = ("a_favour_that_was_deleted",) + FAVOUR_ORDER
    state = _state()
    original = ns.FAVOUR_ORDER
    ns.FAVOUR_ORDER = monkeypatched_order
    try:
        assert ns.available_favour(state) == FAVOUR_ORDER[0]
    finally:
        ns.FAVOUR_ORDER = original


# === second pass: cases the first pass left untested =====================

def test_note_meeting_replaces_a_malformed_record_with_a_whole_one():
    """``met`` is repaired by ``load_state``, but an entry INSIDE it can still be
    junk. note_meeting must build a fresh record rather than write into a
    string."""
    state = _state()
    state["met"]["village_elder"] = "corrupt"
    record = ns.note_meeting(state, "village_elder", _player())
    assert state["met"]["village_elder"] is record
    assert record["visits"] == 1 and record["last_seen"] > 0.0


def test_note_meeting_counts_up_from_a_stringified_visit_counter():
    """A row saved by an older build can hold ``"3"``. Counting must not reset
    the player's relationship history to one."""
    state = _state()
    state["met"]["village_elder"] = {"visits": "3"}
    assert ns.note_meeting(state, "village_elder")["visits"] == 4


def test_first_meeting_agrees_with_the_counter_it_can_read():
    state = _state()
    state["met"]["village_elder"] = {"visits": "3"}
    assert ns.first_meeting(state, "village_elder") is False


def test_load_state_preserves_the_active_slot_and_keeps_unknown_keys():
    """``active_favour_id`` is not one of the five containers, so normalisation
    must leave it alone -- it is the only thing that makes the one-at-a-time
    rule survive a restart."""
    player = {
        ns.STATE_KEY: {
            "active_favour_id": "jiu_sealed_jar",
            "standing": "corrupt",
            "something_a_newer_build_added": {"kept": True},
        }
    }
    state = ns.load_state(player)
    assert state["active_favour_id"] == "jiu_sealed_jar"
    assert state["standing"] == {}
    assert state["something_a_newer_build_added"] == {"kept": True}


def test_favour_view_hands_back_a_copy_the_caller_cannot_write_through():
    """The HTTP layer serialises this dict. If it were the stored record, a
    caller mutating the reply would silently rewrite saved state."""
    state = _state()
    record = ns.accept_favour(state, FAVOUR_ORDER[0])
    view = ns.favour_view(state, FAVOUR_ORDER[0])
    view["status"] = "TAMPERED"
    view["hours_left"] = 999.0
    assert state["favours"][FAVOUR_ORDER[0]] is record
    assert record["status"] == ns.STATUS_ACCEPTED
    assert ns.favour_view(state, FAVOUR_ORDER[0])["status"] == ns.STATUS_ACCEPTED


def test_roster_entry_hands_back_a_copy_the_caller_cannot_write_through():
    state = _state()
    ns.adjust_standing(state, "village_elder", 2)
    entry = ns.roster_entry(state, "village_elder")
    entry["standing"] = 999
    entry["band"] = "distrusted"
    assert ns.get_standing(state, "village_elder") == 2
    assert ns.roster_entry(state, "village_elder")["band"] == "trusted"


def test_a_mutated_roster_entry_leaves_the_stored_meeting_record_alone():
    state = _state()
    record = ns.note_meeting(state, "village_elder", _player())
    ns.roster_entry(state, "village_elder")["visits"] = 99
    assert record["visits"] == 1


def test_get_standing_truncates_a_float_stored_value():
    """Standing is a signed integer in the dialogue tables; a float row must be
    brought back to the same scale rather than kept as 2.7."""
    state = _state()
    state["standing"]["village_elder"] = 2.7
    assert ns.get_standing(state, "village_elder") == 2


def test_get_standing_clamps_a_stored_value_that_escaped_its_bounds():
    """Written before the clamp existed, or by hand. The band must still be one
    the greeting lines were written for."""
    state = _state()
    state["standing"]["village_elder"] = 99
    assert ns.get_standing(state, "village_elder") == STANDING_MAX
    assert ns.standing_band(state, "village_elder") == "trusted"
    state["standing"]["village_elder"] = -99
    assert ns.get_standing(state, "village_elder") == STANDING_MIN
    assert ns.standing_band(state, "village_elder") == "distrusted"


@pytest.mark.parametrize("bad_delta", ["abc", None, [1], {"x": 1}])
def test_adjust_standing_refuses_a_delta_it_cannot_read(bad_delta):
    """The delta is server-owned (it comes from the favour table), never caller
    data -- so raising here is correct, and it must raise BEFORE the standing
    is written rather than half-applying."""
    state = _state()
    with pytest.raises((TypeError, ValueError)):
        ns.adjust_standing(state, "village_elder", bad_delta)
    assert ns.get_standing(state, "village_elder") == 0


def test_settling_clears_the_active_slot_even_when_it_names_another_favour():
    """The slot and the record are cleared by different lines, so they can
    disagree. This pins the behaviour the roster depends on: after any
    resolution, ``available_favour`` offers the next ask."""
    state = _state()
    ns.accept_favour(state, FAVOUR_ORDER[0])
    state["active_favour_id"] = FAVOUR_ORDER[1]
    ns.settle_favour(state, FAVOUR_ORDER[0])
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


def test_the_same_lead_is_never_filed_twice():
    state = _state()
    lead = FAVOURS[FAVOUR_ORDER[0]]["lead"]
    state["leads"] = [lead]
    ns.accept_favour(state, FAVOUR_ORDER[0])
    ns.settle_favour(state, FAVOUR_ORDER[0])
    assert state["leads"] == [lead]


def test_statuses_are_compared_exactly_so_a_different_casing_is_a_409():
    """The router turns this ValueError into a 409, not a 500. A stored status
    that differs only in case must not be mistaken for a live request."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {"status": "ACCEPTED"}
    with pytest.raises(ValueError):
        ns.settle_favour(state, FAVOUR_ORDER[0])


def test_favour_view_needs_no_favours_container_to_have_been_created():
    """``favour_view`` is called on a player who has never been asked anything;
    it must not need ``load_state`` to have run first."""
    view = ns.favour_view({"standing": {}}, FAVOUR_ORDER[0])
    assert view["status"] == ns.STATUS_OFFERED
    assert view["due_at"] is None and view["hours_left"] is None


def test_npc_ids_is_a_tuple_in_the_roster_config_order():
    """Order is what makes the roster stable between page loads."""
    assert isinstance(ns.npc_ids(), tuple)
    assert ns.npc_ids() == tuple(NPC_ROSTER.keys())


def test_every_favour_can_be_told_apart_from_its_ask():
    """Two requests with identical wording would make the one-at-a-time choice
    unreadable on the roster."""
    asks = [FAVOURS[favour_id]["ask"] for favour_id in FAVOUR_ORDER]
    assert len(set(asks)) == len(asks)


def test_no_two_favours_share_a_cost_npc_and_an_asker():
    """Not a rule of the design, but a shared pair would mean accepting one
    request pays the person it costs, which is the choice the design removes."""
    pairs = [(f["npc"], f["cost_npc"]) for f in FAVOURS.values()]
    assert len(set(pairs)) == len(pairs)


# === two crash paths found by the second pass ============================
#
# Both are reachable from a row that an older build, a hand edit, or a config
# change left behind, and both used to raise out of a read the roster screen
# makes for EVERY player on EVERY load. npc_state now repairs both, so these
# are ordinary tests that must stay green.


def test_expiring_an_accepted_favour_whose_id_left_the_config_does_not_raise():
    """Same contract as the malformed-deadline case above: a favour the player
    can never resolve must not stay open forever. Either repair shape passes --
    reporting the id as expired, or dropping it -- but it may not raise, and it
    may not leave the record sitting in ``accepted``."""
    state = _state()
    state["favours"]["a_favour_that_was_deleted"] = {
        "status": ns.STATUS_ACCEPTED, "due_at": 0.0,
    }
    try:
        result = ns.expire_favours(state, 10_000.0)
    except KeyError:
        pytest.fail("expire_favours raised KeyError out of the roster read")
    assert isinstance(result, list)
    assert state["favours"]["a_favour_that_was_deleted"]["status"] != ns.STATUS_ACCEPTED


def test_roster_entry_survives_a_stringified_visit_counter():
    state = _state()
    state["met"]["village_elder"] = {"visits": "corrupt", "last_seen": "soon"}
    entry = ns.roster_entry(state, "village_elder")
    assert entry["visits"] == 0

# === third pass: the same visit-counter defect, on the TALK path =========
#
# ``roster_entry`` is covered above. These two are the worse half of the same
# bug: a visit counter that is not a number makes ``first_meeting`` and
# ``note_meeting`` raise ValueError, and ``npc_routes.talk`` calls BOTH of them
# on every single conversation -- so a player with this row cannot talk to
# anyone at all, and the roster fix alone does not help them.


def test_first_meeting_survives_a_non_numeric_visit_counter():
    state = _state()
    state["met"]["village_elder"] = {"visits": "corrupt"}
    assert ns.first_meeting(state, "village_elder") is False


def test_note_meeting_survives_a_non_numeric_visit_counter():
    """A numeric STRING is already handled ('3' counts up to 4, pinned above).
    Only a genuinely unreadable counter breaks, and it must not."""
    state = _state()
    state["met"]["village_elder"] = {"visits": "corrupt"}
    assert ns.note_meeting(state, "village_elder")["visits"] == 1


# === fourth pass: batch expiry and the one shape a bool sneaks through =====


def test_expire_favours_fails_every_overdue_request_in_one_pass():
    """expire_favours walks the dict while fail_favour rewrites it, so it has
    to iterate a snapshot. Three overdue requests must all expire, in roster
    order, without the walk skipping one."""
    state = _state()
    overdue = ["qiu_water_levy_writ", "jiu_sealed_jar", "wen_witness"]
    for favour_id in overdue:
        state["favours"][favour_id] = {
            "status": ns.STATUS_ACCEPTED, "due_at": 0.0,
            "accepted_at": 0.0, "resolved_at": None,
        }
    state["favours"]["wei_sparring_ground"] = {
        "status": ns.STATUS_ACCEPTED, "due_at": 9_000_000_000.0,
        "accepted_at": 0.0, "resolved_at": None,
    }
    assert ns.expire_favours(state, 10_000.0) == overdue
    for favour_id in overdue:
        assert state["favours"][favour_id]["status"] == ns.STATUS_FAILED
    assert state["favours"]["wei_sparring_ground"]["status"] == ns.STATUS_ACCEPTED


def test_batched_expiry_pays_each_asker_once_and_files_no_leads():
    """Failing is not settling: three failures cost three askers their step and
    hand the player no information."""
    state = _state()
    for favour_id in FAVOUR_ORDER[:3]:
        state["favours"][favour_id] = {
            "status": ns.STATUS_ACCEPTED, "due_at": 0.0,
            "accepted_at": 0.0, "resolved_at": None,
        }
    ns.expire_favours(state, 10_000.0)
    for favour_id in FAVOUR_ORDER[:3]:
        assert ns.get_standing(state, FAVOURS[favour_id]["npc"]) == -int(
            FAVOURS[favour_id]["gain"]
        )
    assert state["leads"] == []
    assert "active_favour_id" not in state


def test_favour_view_treats_a_bool_deadline_as_a_number_or_nothing():
    """``bool`` is a subclass of ``int`` in Python, so a stored ``True`` is a
    number of 1 second past the epoch and a deadline of already-expired. What
    must not happen is a crash or a negative countdown -- ``_hours_left``
    floors at zero for exactly this reason."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {
        "status": ns.STATUS_ACCEPTED, "due_at": True,
    }
    view = ns.favour_view(state, FAVOUR_ORDER[0], 10_000.0)
    assert view["hours_left"] == 0.0
    assert view["due_at"] is not None


def test_expire_favours_fails_an_accepted_request_whose_deadline_is_a_string():
    """A stringified deadline is malformed, so the request must close rather
    than sit in `accepted` forever -- the same rule the `due_at` of 0.0 case
    above pins, reached through a different shape of junk."""
    state = _state()
    state["favours"][FAVOUR_ORDER[0]] = {
        "status": ns.STATUS_ACCEPTED, "due_at": "3600",
    }
    assert ns.expire_favours(state, 10_000.0) == [FAVOUR_ORDER[0]]
    assert state["favours"][FAVOUR_ORDER[0]]["status"] == ns.STATUS_FAILED


# === fifth pass: the two decisions that were reasoned but never measured ====
#
# ``_stored_number`` replaced a bare ``int()`` on every stored counter. Two of
# its behaviours were decided by reading Python's type rules rather than by
# running them, so this section pins them by measurement:
#
#   1. a stored boolean reads as its NUMERIC value -- True -> 1.0, False -> 0.0.
#      This is a WIDENING of the ``int()`` it replaced, not a narrowing: a bool
#      is a subclass of int, so ``int(True)`` was already 1. What must not
#      happen is the bool being treated as a flag and discarded, or read as
#      "truthy" and counted differently from the number it stands for.
#
#   2. an infinite or not-a-number float reads as the default. ``int(inf)``
#      raises OverflowError and ``int(nan)`` raises ValueError, so without this
#      guard the crash the helper exists to remove comes straight back.
#
# Both shapes are reachable in production: the state rides inside
# ``extra_state``, and Python's ``json`` writes a bool as ``true`` and an
# infinity as the bare token ``Infinity`` -- so a row can carry either one and
# come back out of a restart unchanged.


@pytest.mark.parametrize("stored,expected", [(True, 1.0), (False, 0.0)])
def test_a_stored_boolean_reads_as_its_numeric_value(stored, expected):
    """Decision 1, measured. True is 1 and False is 0 -- the number they stand
    for, not a discarded flag."""
    assert ns._stored_number(stored) == expected
    assert isinstance(ns._stored_number(stored), float)


def test_a_stored_boolean_visit_count_is_read_by_its_number_not_its_truthiness():
    """The load-bearing half of decision 1: False must count as ZERO visits,
    which is a first meeting, and True must count as ONE visit, which is not.
    Reading the bool as a bare flag would make both of them answer the same."""
    state = _state()
    state["met"]["village_elder"] = {"visits": True}
    assert ns.roster_entry(state, "village_elder")["visits"] == 1
    assert ns.first_meeting(state, "village_elder") is False

    other = _state()
    other["met"]["village_elder"] = {"visits": False}
    assert ns.roster_entry(other, "village_elder")["visits"] == 0
    assert ns.first_meeting(other, "village_elder") is True


def test_counting_up_from_a_stored_boolean_uses_the_number_it_stands_for():
    """note_meeting adds one to whatever it reads. From True it must be 2; from
    False it must be 1 -- the counter resumes instead of resetting."""
    state = _state()
    state["met"]["village_elder"] = {"visits": True}
    assert ns.note_meeting(state, "village_elder")["visits"] == 2

    other = _state()
    other["met"]["village_elder"] = {"visits": False}
    assert ns.note_meeting(other, "village_elder")["visits"] == 1


def test_a_stored_boolean_standing_lands_on_the_band_its_number_names():
    """Standing is read through the same helper. True is one step of standing
    (neutral), False is none."""
    state = _state()
    state["standing"]["village_elder"] = True
    assert ns.get_standing(state, "village_elder") == 1
    assert ns.standing_band(state, "village_elder") == "neutral"
    state["standing"]["village_elder"] = False
    assert ns.get_standing(state, "village_elder") == 0
    assert ns.standing_band(state, "village_elder") == "neutral"


def test_adjust_standing_continues_from_a_stored_boolean_base():
    """The write path on top of a boolean row: 1 + 1 is 2, not a reset."""
    state = _state()
    state["standing"]["village_elder"] = True
    assert ns.adjust_standing(state, "village_elder", 1) == 2


@pytest.mark.parametrize("stored", [float("inf"), float("-inf"), float("nan")])
def test_an_infinite_or_not_a_number_float_reads_as_the_default(stored):
    """Decision 2, measured. ``int(inf)`` raises OverflowError and ``int(nan)``
    raises ValueError, so this is the guard that keeps the crash out."""
    assert ns._stored_number(stored) == 0.0
    assert ns._stored_number(stored, default=1) == 1


@pytest.mark.parametrize("stored", [float("inf"), float("-inf"), float("nan")])
def test_the_helper_never_returns_an_infinite_or_not_a_number(stored):
    """The invariant behind every caller: whatever comes in, what comes out is a
    finite number. A caller that multiplies or compares the result cannot be
    handed an infinity to propagate."""
    result = ns._stored_number(stored)
    assert math.isfinite(result)


def test_an_infinite_visit_count_reads_as_zero_and_the_meeting_still_stands():
    """A record that exists but cannot be read is still a meeting that HAPPENED,
    so an infinite counter must not replay the first-meeting greeting at a
    player who has already been here."""
    state = _state()
    state["met"]["village_elder"] = {"visits": float("inf"), "last_seen": float("inf")}
    entry = ns.roster_entry(state, "village_elder")
    assert entry["visits"] == 0
    assert entry["last_seen"] == 0.0
    assert math.isfinite(entry["last_seen"])
    assert ns.first_meeting(state, "village_elder") is False


@pytest.mark.parametrize("stored", [float("inf"), float("-inf"), float("nan")])
def test_counting_up_from_an_infinite_visit_count_starts_a_real_counter(stored):
    """The counter must be repaired to a real number, not left unusable."""
    state = _state()
    state["met"]["village_elder"] = {"visits": stored}
    record = ns.note_meeting(state, "village_elder")
    assert record["visits"] == 1
    assert isinstance(record["visits"], int)


@pytest.mark.parametrize("stored", [float("inf"), float("-inf"), float("nan")])
def test_an_infinite_standing_reads_as_zero_rather_than_escaping_the_clamp(stored):
    """Without the guard this is where the crash lives: ``int(inf)`` raises
    before the clamp can do anything. With it, standing is neutral."""
    state = _state()
    state["standing"]["village_elder"] = stored
    assert ns.get_standing(state, "village_elder") == 0
    assert ns.standing_band(state, "village_elder") == "neutral"


def test_adjust_standing_on_an_infinite_base_recovers_instead_of_raising():
    """The write path must survive the same row a read survives."""
    state = _state()
    state["standing"]["village_elder"] = float("inf")
    assert ns.adjust_standing(state, "village_elder", 1) == 1


@pytest.mark.parametrize("stored", [True, False, float("inf"), float("-inf"), float("nan")])
def test_a_boolean_or_infinite_visit_count_survives_the_store_round_trip(stored):
    """The whole point of storing the state: these shapes are read back out of
    a restart, not just out of the dict they were written into. Python's json
    preserves a bool and an ``Infinity`` token, so both really do come back."""
    store = CopyOnReadStore({"p1": _player()})
    store.rows["p1"][ns.STATE_KEY] = {"met": {"village_elder": {"visits": stored}}}
    rebooted = CopyOnReadStore(store.rows)  # a restart, from what was persisted

    def _work(state, player):
        ns.note_meeting(state, "village_elder", player)
        return ns.roster_entry(state, "village_elder")

    entry = _mutate(rebooted, "p1", _work)
    assert isinstance(entry["visits"], int)
    assert entry["visits"] in (0, 1, 2)


def test_the_helper_widens_the_int_it_replaced_and_never_raises_on_junk():
    """The safety claim the docstring makes: every value the old unguarded read
    handled still reads identically, and only the values that used to raise
    behave differently. Checked as a relationship against ``int()`` itself, not
    against a frozen literal, so this stays true if either side is retuned."""
    for value in (0, 1, -3, 2.7, "3", True, False):
        # int() truncates; the helper does not. Callers that want an integer
        # apply int() themselves, so compare the values both agree on.
        assert int(ns._stored_number(value)) == int(value)
    for junk in (None, "corrupt", [], {}, object()):
        assert isinstance(ns._stored_number(junk), float)


def test_the_helper_always_returns_a_finite_float_for_every_shape_it_accepts():
    """One invariant, swept: no caller of this helper can ever be handed an
    infinity or a not-a-number, whatever the row held."""
    for value in (
        True, False, 0, 1, -3, 2.7, "3", "corrupt", None, [], {},
        float("inf"), float("-inf"), float("nan"), float("-0.0"),
    ):
        result = ns._stored_number(value)
        assert isinstance(result, float), value
        assert math.isfinite(result), value


# === the WRITE half: what accept_favour puts on the row ==================
#
# Measured. The reply half of this defect was closed first, in
# ``server/npc_routes.py``: the deadline in the reply was routed through
# ``_readable_deadline`` so the response could never carry an infinity. The
# WRITE half was left behind, and that is the half that matters, because a value
# that cannot be encoded as standard JSON does not just fail to be sent -- it
# cannot be dumped, exported, replicated or read back by anything that speaks
# JSON. Measured on the unfixed tree: seeding ``FAVOURS[fid]["deadline_hours"]``
# with ``inf`` and calling ``accept_favour`` stored ``inf`` on the player row,
# and ``json.dumps(state, allow_nan=False)`` refused with
# ``"Out of range float values are not JSON compliant: inf"``. The request still
# closed -- the expiry sweep read the infinity as already expired -- so nothing
# looked broken from the outside.
#
# The deadline is the only number in this module that is COMPUTED rather than
# copied, so ``accept_favour`` is the only place one can be born. The tests below
# seed the config table and then check the row itself, not the reply.

#: Config ``deadline_hours`` values that make the computed deadline non-finite.
#: ``OVERFLOW_HOURS`` is finite and is here on purpose: ``1e308 * 3600.0`` is
#: still ``inf``, so a guard applied to the hours instead of the computed sum
#: would pass this one straight through.
NON_FINITE_CONFIG_HOURS = [float("inf"), float("-inf"), float("nan"), 1e308]
NON_FINITE_CONFIG_IDS = ["inf", "neg_inf", "nan", "finite_overflow"]


def _seed_config_deadline(monkeypatch, favour_id, hours):
    """Point one favour's config deadline at ``hours`` for this test only.

    ``FAVOURS`` is the same dict object ``npc_state`` reads, so this is the real
    reachable arm -- not a hand-edited row. A hand-edited row would prove the
    reader survives; this proves the writer cannot produce the value.
    """
    monkeypatch.setitem(FAVOURS[favour_id], "deadline_hours", hours)


@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_accept_favour_never_stores_a_non_finite_deadline(monkeypatch, hours):
    """The defect itself: the row must not hold a number JSON cannot encode."""
    favour_id = FAVOUR_ORDER[0]
    _seed_config_deadline(monkeypatch, favour_id, hours)
    state = _state()
    record = ns.accept_favour(state, favour_id)
    stored = state["favours"][favour_id]["due_at"]
    assert stored == record["due_at"], "the returned record must be the row"
    assert stored is None, f"stored {stored!r}"
    assert not (isinstance(stored, float) and not math.isfinite(stored))


@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_the_state_accept_favour_stores_encodes_as_standard_json(monkeypatch, hours):
    """The consequence that makes it worth fixing: the row becomes undumpable."""
    favour_id = FAVOUR_ORDER[0]
    _seed_config_deadline(monkeypatch, favour_id, hours)
    state = _state()
    ns.accept_favour(state, favour_id)
    encoded = _assert_standard_json(state)
    assert encoded["favours"][favour_id]["due_at"] is None


@pytest.mark.parametrize("favour_id", FAVOUR_ORDER)
@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_no_shipped_favour_can_write_a_non_finite_deadline(monkeypatch, favour_id, hours):
    """Swept across the whole table, not one hand-picked favour."""
    _seed_config_deadline(monkeypatch, favour_id, hours)
    state = _state()
    ns.accept_favour(state, favour_id)
    _assert_standard_json(state)
    assert state["favours"][favour_id]["due_at"] is None


@pytest.mark.parametrize("favour_id", FAVOUR_ORDER)
@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_a_non_finite_deadline_survives_a_restout_and_still_encodes(monkeypatch, favour_id, hours):
    """Through the store, so this is about the PERSISTED row and not a dict
    that still holds its own values."""
    _seed_config_deadline(monkeypatch, favour_id, hours)
    store = CopyOnReadStore({"p1": _player()})
    _mutate(store, "p1", lambda state, player: ns.accept_favour(state, favour_id))
    rebooted = CopyOnReadStore(store.rows)  # a restart, from what was persisted

    persisted = rebooted["p1"][ns.STATE_KEY]["favours"][favour_id]["due_at"]
    assert persisted is None
    _assert_standard_json(rebooted["p1"])


@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_a_non_finite_deadline_does_not_lock_the_player_out(monkeypatch, hours):
    """Storing ``None`` must not turn the bad config into a stuck request --
    the failure mode this whole defect class is about."""
    favour_id = FAVOUR_ORDER[0]
    _seed_config_deadline(monkeypatch, favour_id, hours)
    state = _state()
    ns.accept_favour(state, favour_id)
    assert ns.expire_favours(state, 10_000_000.0) == [favour_id]
    assert state["favours"][favour_id]["status"] == ns.STATUS_FAILED
    assert "active_favour_id" not in state
    assert ns.available_favour(state) == FAVOUR_ORDER[1]


@pytest.mark.parametrize("hours", NON_FINITE_CONFIG_HOURS, ids=NON_FINITE_CONFIG_IDS)
def test_a_non_finite_deadline_is_closed_exactly_like_the_string_case(monkeypatch, hours):
    """Same rule, same cost. A corrupt deadline must not become a way to keep
    standing: the suite already pins that a request closed by the sweep costs
    the asker the same step as one that ran out of time."""
    favour_id = FAVOUR_ORDER[0]
    _seed_config_deadline(monkeypatch, favour_id, hours)
    state = _state()
    ns.accept_favour(state, favour_id)
    ns.expire_favours(state, 10_000_000.0)
    assert ns.get_standing(state, FAVOURS[favour_id]["npc"]) == -int(FAVOURS[favour_id]["gain"])


@pytest.mark.parametrize("favour_id", FAVOUR_ORDER)
def test_a_finite_config_deadline_still_stores_a_real_number(favour_id):
    """The widening must not swallow the ordinary case: a normal acceptance
    still writes a number, and it is the number the config asked for."""
    from npc_roster_config import favour_deadline_hours

    state = _state()
    record = ns.accept_favour(state, favour_id)
    stored = record["due_at"]
    assert isinstance(stored, float) and math.isfinite(stored)
    expected = record["accepted_at"] + favour_deadline_hours(favour_id) * 3600.0
    assert stored == pytest.approx(expected)


@pytest.mark.parametrize("favour_id", FAVOUR_ORDER)
def test_a_zero_length_deadline_is_stored_as_zero_not_as_unreadable(monkeypatch, favour_id):
    """The ``None``-versus-``0.0`` distinction, on the WRITE side.

    ``_readable_deadline`` passes a real ``0.0`` through as ``0.0`` precisely so
    "expired the moment it started" stays distinct from "cannot be read". A
    guard that returned the unreadable marker for every falsy value would turn a
    legitimate zero-length request into an unreadable one and quietly change
    what the player sees.
    """
    _seed_config_deadline(monkeypatch, favour_id, 0.0)
    state = _state()
    record = ns.accept_favour(state, favour_id)
    assert record["due_at"] is not None
    assert record["due_at"] == pytest.approx(record["accepted_at"])


def test_the_deadline_is_guarded_after_the_arithmetic_not_before_it():
    """Why the guard sits on the computed sum.

    A finite config value can still overflow when it is multiplied out to
    seconds, so a guard on ``deadline_hours`` alone would let this through. Named
    as its own test because it is the arm a reviewer will otherwise assume is
    covered by the ``inf``/``nan`` cases.
    """
    assert math.isfinite(1e308)
    assert not math.isfinite(1e308 * 3600.0)


# === the ORDERING: the deadline read above the charge ===================
#
# Measured. ``accept_favour`` READS the computed deadline BEFORE it charges
# the cost. The other order is a theft the player cannot undo: a helper
# that refuses a bad value refuses it AFTER the standing was debited, and
# the refusal becomes a 409 the player has no way to clear. Measured on the
# unfixed tree: standing went 5 -> 2 for an acceptance that never happened.
#
# No test seeded with a bad VALUE can see this. Both shipped readers WIDEN
# rather than refuse -- ``favour_deadline_hours`` falls back to the module
# default and ``_readable_deadline`` answers ``None`` -- so on today's code
# the read never raises and the two orders behave identically. The
# discriminating arm is the OTHER design the fix names: a helper that
# refuses loudly. So these tests force the refusal, which is the only way to
# tell which of the two statements runs first.


def _deadline_read_refuses(monkeypatch, message):
    """Make the deadline read refuse, the way a strict helper would."""

    def _refuse(_value):
        raise ValueError(message)

    monkeypatch.setattr(ns, "_readable_deadline", _refuse)


def test_the_deadline_is_read_before_the_cost_is_charged(monkeypatch):
    """The ordering as a fact about two statements, not about a value.

    Move the charge above the read and this goes red: the debit lands, the
    read then refuses, and the exception escapes with the player already
    poorer. Fresh standing is 0 and the cost is a whole step, so the
    assertion can actually tell the two orders apart.
    """
    favour_id = FAVOUR_ORDER[0]
    favour = FAVOURS[favour_id]
    state = _state()
    before = ns.get_standing(state, favour["cost_npc"])
    _deadline_read_refuses(monkeypatch, "unusable deadline")

    with pytest.raises(ValueError, match="unusable deadline"):
        ns.accept_favour(state, favour_id)

    assert ns.get_standing(state, favour["cost_npc"]) == before


def test_a_refused_deadline_leaves_no_acceptance_half_written(monkeypatch):
    """The same ordering seen from the record instead of the standing.

    Reading first means the refusal happens before ANY state write, so an
    acceptance that did not happen leaves no record, no active slot and no
    offer timestamp. An acceptance the player is then told they never made
    is the other half of the same bug, and it survives a fix that only
    protects the standing.
    """
    favour_id = FAVOUR_ORDER[0]
    state = _state()
    _deadline_read_refuses(monkeypatch, "unusable deadline")

    with pytest.raises(ValueError):
        ns.accept_favour(state, favour_id)

    assert favour_id not in state["favours"]
    assert "active_favour_id" not in state
    assert favour_id not in state["offers"]


def test_a_strict_deadline_helper_leaves_the_next_request_offered(monkeypatch):
    """A refused acceptance must not consume the slot either.

    The measured failure is a permanent 409: the request stays marked as
    taken, so no offer ever comes round again and the player is stuck for
    the rest of the run.
    """
    favour_id = FAVOUR_ORDER[0]
    state = _state()
    _deadline_read_refuses(monkeypatch, "unusable deadline")

    with pytest.raises(ValueError):
        ns.accept_favour(state, favour_id)

    assert ns.available_favour(state) == favour_id
