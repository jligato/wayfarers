"""HTTP contract for `server/mission_board_routes.py`.

WHAT THIS FILE IS
-----------------
Two routes, and this file pins what each one accepts and what it answers:

    GET  /mission-board         the board: every objective, its progress,
                                whether it is claimed, its reward, and the
                                countdown to the next turn of each timescale.
    POST /mission-board/claim   claim one completed objective.

"Contract" here means the shape a CLIENT can rely on: which method, where the
player id must appear, which fields come back, which header is mandatory, and
which refusal answers which mistake. Every status code below was measured by
driving the real app through the real auth seam -- none is read off the source.

MEASURED REFERENCE TABLE (2026-10-03, re-measured 2026-10-05, this tree)
-----------------------------------------------------------------------
    GET  /mission-board, no Authorization header         401
    GET  /mission-board, no ?player_id                  422 ["query","player_id"]
    GET  /mission-board, another player's id            403
    GET  /mission-board, session player not in store    401 "invalid or missing session token"
    GET  /mission-board, valid                          200
    POST /mission-board                                  405
    POST /mission-board/claim, no Authorization header   401
    POST /mission-board/claim, no Idempotency-Key        400
    POST /mission-board/claim, blank Idempotency-Key     400
    POST /mission-board/claim, Idempotency-Key > 128     400
    POST /mission-board/claim, control char in the key   400
    POST /mission-board/claim, another player's id       403
    POST /mission-board/claim, unknown player            401 "invalid or missing session token"
    POST /mission-board/claim, no body / empty body      422
    POST /mission-board/claim, unknown objective         404 "Unknown objective"
    POST /mission-board/claim, incomplete objective      400 "Objective not complete"
    POST /mission-board/claim, completed                 200 ok=true
    POST /mission-board/claim, already claimed           200 ok=false
    GET  /mission-board/claim                            405

WHY THE ORDER OF THE REFUSALS MATTERS
-------------------------------------
Four gates run before the handler does anything, and their order is part of the
contract because it decides which message a mistake gets:

    auth (401)  ->  ownership (403)  ->  Idempotency-Key (400)
              ->  player exists (404)  ->  objective exists (404)
              ->  complete? (400)

An anonymous caller learns nothing about whether a player or an objective
exists. A caller naming somebody else learns nothing either -- 403, never 404,
so the route cannot be used to probe which player ids are real. And a missing
idempotency key is refused BEFORE the player is looked up, so a caller who
forgot the header gets the same 400 whether or not the player exists.

"PLAYER EXISTS (404)" IS NOT REACHABLE OVER HTTP, AND THE ORDER ABOVE SAYS WHY
--------------------------------------------------------------------------
The order is written as ``auth`` first, so the ``player exists (404)`` row is
unreachable: ``require_player`` runs as a ``Depends`` and refuses a session
whose player was never stored (401) before the handler's own store lookup can
answer 404. The 404 branches in ``mission_board_routes.py`` are therefore dead
over HTTP as the guard is wired today. Measured on this tree, and asserted by
``test_the_player_is_checked_before_the_objective``:

    session player absent, objective made up   401
    session player absent, objective real      401   (byte-identical body)

Both rows read 404 in earlier drafts of this file. That was the assertion, not
the server: the two rows are indistinguishable to the caller, which is the
property that actually matters, so the test now pins the PAIR rather than a
status code it cannot produce. Reachability of the 404 objective arm is kept
honest by ``test_the_objective_is_checked_before_completion``, which signs in as
a real player and does get 404.

THE TWO CLAIM ARMS ARE DIFFERENT SHAPES, NOT ONE SHAPE WITH A FLAG
----------------------------------------------------------------
The paid arm answers seven keys -- it adds ``idempotency_key`` and
``credited_items``. The already-claimed arm answers FIVE: it is built by a
different call and never receives those two. That is measured, not inferred:

    paid           [already_claimed, credited_items, cycle_key,
                    idempotency_key, objective_id, ok, reward]
    already claimed[already_claimed, cycle_key, objective_id, ok, reward]

A client that reads ``body["idempotency_key"]`` on a retry would raise
KeyError on exactly the response its retry path exists to handle. This file
pins the difference rather than papering over it with ``.get(..., None)``, and
``TestAlreadyClaimedArm`` carries the negative control that proves the
assertions can tell the two arms apart.

NEGATIVE CONTROLS
-----------------
A contract file is worth nothing if its assertions pass vacuously. Three
controls are in here, in the file rather than in a comment:

* ``test_claim_shape_detector_tells_the_two_arms_apart`` -- the shape detector
  used by the paid/refused assertions, run against both arms and against a
  deliberately emptied body, so a detector that recognises nothing fails.
* ``test_query_id_detector_flags_a_query_only_route`` -- the 422 detector,
  run against a throwaway app carrying the defect shape it is meant to catch.
* Every ownership test asserts 403 AND that the victim's board did not change,
  so a route that ignored ownership would fail on the state, not just the code.

THE AUTH ASSERTIONS, AND WHY ONE MUTATION SURVIVES THEM
------------------------------------------------------
``TestAuthentication`` and ``TestOwnership`` were checked by breaking the route
on purpose: delete the ``Depends(require_player)`` from each signature, re-run,
and see what turns red.

Deleting it from ONE route leaves this file fully green -- and that is correct,
not a weak assertion. ``server/ownership_guard.py`` is a central ratchet that
appends a real ``Depends(require_player)`` to any route carrying a bare player
id claim and no auth dependency, so the route stays guarded when its own
dependency is removed. Two layers, either of which is enough. Deleting the
route's dependency AND stubbing the ratchet turns 10 of these tests red.

The assertion is therefore about the SERVED contract -- an anonymous caller
gets 401 and a substituted id gets 403 -- which is what a client can observe,
and it is exactly the property that survives the ratchet. A test that asserted
"this signature contains this text" would fail on a route that is genuinely
safe, which is the wrong kind of failure.

NOTES ON THE FIXTURES
---------------------
``clear_signup_rate_limit`` is required: ``/player/create`` allows 5 creations
per 60 seconds per process, and this file creates more than five players, so
without it the later tests fail at the create call before reaching any
assertion. That limit has its own tests; this is not one of them.

``reset_claim_rate_limit`` is required for the same shape of reason.
``server/rate_limit.py`` mounts ``enforce_claim_rate_limit`` on every POST
route with a ``claim`` path segment, ``/mission-board/claim`` included, at 30
attempts per 60 seconds keyed per player. This file makes more claim calls than
that in one run, so the counters are cleared around each test. The limiter
itself is covered by ``server/tests/test_rate_limit_claims.py``; nothing here
asserts anything about its numbers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import main  # noqa: E402
from server.tests.wc_test_auth import (  # noqa: E402
    Session,
    clear_signup_rate_limit,
)

import mission_board  # noqa: E402
import rate_limit  # noqa: E402
from mission_board import BOARD_OBJECTIVES  # noqa: E402

BOARD = "/mission-board"
CLAIM = "/mission-board/claim"

#: An objective with no baseline to fight: a THRESHOLD row on a plain
#: counter, so setting the field makes it complete with no cycle bookkeeping.
#: Chosen over the hunt rows because those read the mission event ledger and
#: would make a shape test depend on the combat system.
OBJECTIVE = "quick_qi_ready"          # threshold on player.progress >= 100
WEEKLY_OBJECTIVE = "weekly_pvp_three"  # delta on player.pvp_wins >= 3


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """Clear the two process-wide counters this file would otherwise exhaust.

    Neither limit is what this file is about, and neither is relaxed here: the
    helpers clear counters, they do not raise the limits.
    """
    clear_signup_rate_limit()
    rate_limit.reset_claim_rate_limit()
    yield
    rate_limit.reset_claim_rate_limit()


@pytest.fixture()
def client():
    """A signed-in-capable client against the real app.

    ``Session`` adds the Authorization header on every call, so each test's
    calls go out as the player it created. A test that wants the anonymous case
    says so with ``as_player(None)`` -- there is deliberately no autouse token,
    because several tests below assert that the UNAUTHENTICATED call fails.
    """
    with TestClient(main.app, raise_server_exceptions=False) as raw:
        yield Session(raw)


def make_player(client, name):
    """Create a player through the public route and sign the client in as them."""
    r = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    client.sign_in(pid)
    return pid


def other_player(client, name):
    """A second, real player the caller does NOT own."""
    r = client.post("/player/create", json={"name": name, "spirit_root": "water"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def get_board(client, pid, **params):
    return client.get(BOARD, params={"player_id": pid, **params})


def claim(client, pid, objective_id=OBJECTIVE, key="mb-contract-1", **body):
    payload = {"player_id": pid, "objective_id": objective_id}
    payload.update(body)
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(CLAIM, json=payload, headers=headers)


def set_source(player_id, field, value):
    """Write a field the board reads as a source, through the store transaction.

    The board's progress is derived from state the SERVER wrote (a hunt count,
    a tower floor, qi banked). Writing those fields directly is arranging a
    precondition -- the claim route under test is what decides whether the
    objective is complete -- and it keeps a shape test from depending on the
    combat system. It goes through ``transaction(...)`` because a detached read
    is discarded by design; see ``test_mission_board_persistence.py``.
    """
    with main.players_db.transaction(player_id):
        main.players_db[player_id][field] = value


def complete_quick(client, pid):
    """Make OBJECTIVE complete through the store, and prove the board agrees."""
    row = next(o for o in BOARD_OBJECTIVES if o["objective_id"] == OBJECTIVE)
    set_source(pid, "progress", row["target"] + 5)
    entry = board_entry(client, pid, OBJECTIVE)
    assert entry["completed"] is True, entry
    return entry


def board_entry(client, pid, objective_id):
    body = get_board(client, pid).json()
    return next(o for o in body["objectives"]
                if o["objective_id"] == objective_id)


# --- detectors, shared by the real tests and the negative controls ----------


def missing_query_player_id(response) -> bool:
    """True when the framework rejected ``player_id`` read as a QUERY value.

    TOLD APART BY ``loc``, not by the status code. A bare scalar parameter that
    FastAPI decides is a query value answers with ``loc == ["query",
    "player_id"]``; a genuine body validation failure names ``body`` and carries
    a constraint type. Both are 422, so the status code alone would call a body
    error a query error.
    """
    if response.status_code != 422:
        return False
    try:
        detail = response.json().get("detail", [])
    except ValueError:
        return False
    if not isinstance(detail, list):
        return False
    return any(
        isinstance(d, dict)
        and d.get("loc") == ["query", "player_id"]
        and d.get("type") == "missing"
        for d in detail
    )


def body_loc_names(response, *names):
    """The set of field names the framework named in the BODY."""
    if response.status_code != 422:
        return set()
    try:
        detail = response.json().get("detail", [])
    except ValueError:
        return set()
    if not isinstance(detail, list):
        return set()
    return {d["loc"][1] for d in detail
            if isinstance(d, dict) and isinstance(d.get("loc"), list)
            and len(d["loc"]) == 2 and d["loc"][0] == "body"}


#: The five keys BOTH claim arms answer. ``outcome_for`` builds both, so a key
#: missing from here is a key the board stopped promising.
COMMON_CLAIM_KEYS = {"ok", "objective_id", "cycle_key", "already_claimed", "reward"}

#: The two keys only the PAID arm adds, after the payout and the item credit.
PAID_ONLY_CLAIM_KEYS = {"idempotency_key", "credited_items"}


def is_paid_arm(body) -> bool:
    """A claim body that reports a completed payout."""
    return COMMON_CLAIM_KEYS <= set(body) and PAID_ONLY_CLAIM_KEYS <= set(body)


def is_already_claimed_arm(body) -> bool:
    """A claim body that reports the objective was already claimed this cycle.

    Also asserts the ABSENCE of the paid-only keys. That absence is the point:
    a client reading ``body["idempotency_key"]`` on its retry path would raise
    KeyError on precisely this response, and an assertion that only checked for
    the common keys would call both arms correct.
    """
    return (COMMON_CLAIM_KEYS <= set(body)
            and not (PAID_ONLY_CLAIM_KEYS & set(body)))


class TestRouteTable:
    """The surface itself: method, where the id is read from, what is guarded."""

    def _route(self, path):
        found = [r for r in main.app.routes
                 if getattr(r, "path", None) == path]
        assert len(found) == 1, (
            f"{path} is mounted {len(found)} times; a shadowed copy answers "
            f"first and the other one is dead code")
        return found[0]

    def test_board_is_a_single_get_route(self):
        route = self._route(BOARD)
        assert "GET" in route.methods, route.methods
        assert "POST" not in route.methods, (
            "POST /mission-board started answering; a client could believe a "
            "read mutates")

    def test_claim_is_a_single_post_route(self):
        route = self._route(CLAIM)
        assert "POST" in route.methods, route.methods
        assert "GET" not in route.methods, (
            "GET /mission-board/claim started answering; a read must never pay")

    def test_board_reads_player_id_from_the_query_string(self):
        """A bare scalar param, so FastAPI classifies it as a query value.

        Pinned because it is invisible in the source: the handler reads
        ``player_id: str``, and the only thing making it a QUERY value is
        FastAPI's rule that a bare scalar is a query parameter. Change it to a
        body model, or rename it, and the client's existing
        ``/mission-board?player_id=`` calls start 422-ing.
        """
        route = self._route(BOARD)
        assert route.body_field is None, "the board read must not take a body"
        assert [q.name for q in route.dependant.query_params] == ["player_id"]

    def test_claim_reads_both_fields_from_the_json_body(self):
        route = self._route(CLAIM)
        assert route.body_field is not None, (
            "the claim body model is gone; player_id/objective_id would be read "
            "as query values and a real client's POST would 422")
        assert [q.name for q in route.dependant.query_params] == [], (
            "the claim route grew query parameters; the id must come from the "
            "body alone")

    def test_both_routes_require_a_session(self):
        for path in (BOARD, CLAIM):
            deps = [d.call.__name__ for d in self._route(path).dependant.dependencies]
            assert "require_player" in deps, (path, deps)

    def test_the_published_body_schema_names_exactly_the_two_fields(self):
        """What a generated client is told to send.

        Read from the live OpenAPI document rather than from the model, because
        the OpenAPI document is what a client generator consumes.
        """
        spec = client_schema()
        schema = spec["components"]["schemas"]["MissionBoardClaimRequest"]
        assert set(schema["required"]) == {"player_id", "objective_id"}, schema
        assert set(schema["properties"]) == {"player_id", "objective_id"}, schema
        for name, prop in schema["properties"].items():
            assert prop["type"] == "string", (name, prop)

    def test_the_published_document_marks_the_board_id_required(self):
        spec = client_schema()
        params = spec["paths"][BOARD]["get"]["parameters"]
        player = next(p for p in params if p["name"] == "player_id")
        assert player["in"] == "query", player
        assert player["required"] is True, (
            "player_id stopped being required in the document; a client "
            "generator would stop sending it and every call would 422")


def client_schema():
    """The app's own OpenAPI document, fetched through the live app."""
    with TestClient(main.app, raise_server_exceptions=False) as raw:
        r = raw.get("/openapi.json")
        assert r.status_code == 200, r.text
        return r.json()


class TestAuthentication:
    """Both routes are guarded. Nothing here works without a session."""

    def test_board_refuses_an_anonymous_caller(self, client):
        anon = client.as_player(None)
        r = anon.get(BOARD, params={"player_id": "whoever"})
        assert r.status_code == 401, r.text

    def test_claim_refuses_an_anonymous_caller(self, client):
        anon = client.as_player(None)
        r = anon.post(CLAIM,
                      json={"player_id": "whoever", "objective_id": OBJECTIVE},
                      headers={"Idempotency-Key": "anon-1"})
        assert r.status_code == 401, r.text

    def test_a_forged_token_is_refused(self, client):
        r = client.get(BOARD, params={"player_id": "whoever"},
                       headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401, r.text

    def test_an_authenticated_caller_is_served(self, client):
        pid = make_player(client, "MbAuthOk")
        assert get_board(client, pid).status_code == 200

    def test_the_refusal_leaks_nothing_about_the_board(self, client):
        """A 401 body must not carry a board, a player, or an objective."""
        anon = client.as_player(None)
        for r in (anon.get(BOARD, params={"player_id": "whoever"}),
                  anon.post(CLAIM,
                            json={"player_id": "whoever", "objective_id": OBJECTIVE},
                            headers={"Idempotency-Key": "anon-2"})):
            assert r.status_code == 401, r.text
            body = r.json()
            assert "objectives" not in body, r.text
            assert "reward" not in body, r.text


class TestBoardRequestShape:
    """GET /mission-board: what the caller must send."""

    def test_the_player_id_is_mandatory(self, client):
        pid = make_player(client, "MbNoId")
        r = client.get(BOARD)
        assert missing_query_player_id(r), (
            f"a call with no player_id answered {r.status_code}: {r.text}. The "
            f"id must be a required QUERY value, not an optional one.")

    def test_a_valid_request_answers_the_board(self, client):
        pid = make_player(client, "MbOk")
        r = get_board(client, pid)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True, r.text

    def test_an_unknown_query_parameter_is_ignored(self, client):
        """Unknown extras do not break a client that sends one."""
        pid = make_player(client, "MbExtra")
        r = get_board(client, pid, nope="1")
        assert r.status_code == 200, r.text


class TestBoardResponseShape:
    """GET /mission-board: what the caller gets back, field by field."""

    @pytest.fixture()
    def body(self, client):
        pid = make_player(client, "MbShape")
        r = get_board(client, pid)
        assert r.status_code == 200, r.text
        return r.json()

    def test_the_top_level_carries_exactly_four_keys(self, body):
        assert set(body) == {"ok", "objectives", "server_time", "cycles"}, (
            f"the board's top-level shape changed to {sorted(body)}; a client "
            f"reading any other key is now reading nothing")

    def test_server_time_is_a_number(self, body):
        assert isinstance(body["server_time"], (int, float)), body["server_time"]

    def test_every_objective_is_reported(self, body):
        assert [o["objective_id"] for o in body["objectives"]] == \
               [row["objective_id"] for row in BOARD_OBJECTIVES], (
            "the board reports a different set, or a different order, than the "
            "definition table; a client indexing by position is now wrong")

    def test_an_objective_carries_exactly_the_fields_the_panel_draws(self, body):
        required = {"objective_id", "cycle", "cycle_key", "kind", "description",
                    "points_at", "unit", "target", "progress", "completed",
                    "claimed", "reward", "seconds_to_refresh"}
        for entry in body["objectives"]:
            assert set(entry) == required, (
                f"{entry['objective_id']} answers {sorted(entry)}")

    def test_every_timescale_reports_a_key_and_a_countdown(self, body):
        assert set(body["cycles"]) == {"quick", "daily", "weekly"}, body["cycles"]
        for cycle, info in body["cycles"].items():
            assert set(info) == {"key", "seconds_to_refresh"}, (cycle, info)
            assert info["key"], cycle
            assert 0 < info["seconds_to_refresh"] <= 7 * 86400, (cycle, info)

    def test_each_objective_reports_its_own_countdown_and_cycle_key(self, body):
        for entry in body["objectives"]:
            cycle = body["cycles"][entry["cycle"]]
            assert entry["cycle_key"] == cycle["key"], entry["objective_id"]
            assert entry["seconds_to_refresh"] == cycle["seconds_to_refresh"], \
                entry["objective_id"]

    def test_progress_is_never_negative_or_past_the_target(self, body):
        for entry in body["objectives"]:
            assert 0 <= entry["progress"] <= entry["target"], entry

    def test_completed_and_claimed_are_booleans(self, body):
        for entry in body["objectives"]:
            assert isinstance(entry["completed"], bool), entry["objective_id"]
            assert isinstance(entry["claimed"], bool), entry["objective_id"]
            assert entry["completed"] == (
                entry["progress"] >= entry["target"]), entry

    def test_a_fresh_player_sees_every_objective_incomplete(self, client):
        """The first-ever view reports nothing done, and nothing claimed."""
        pid = make_player(client, "MbFresh")
        body = get_board(client, pid).json()
        assert len(body["objectives"]) == len(BOARD_OBJECTIVES)
        for entry in body["objectives"]:
            assert entry["progress"] == 0, entry["objective_id"]
            assert entry["completed"] is False, entry["objective_id"]
            assert entry["claimed"] is False, entry["objective_id"]

    def test_the_reward_is_advertised_on_the_board(self, client):
        """The reward the claim would pay is visible BEFORE the claim.

        Keyed on the definition table rather than a literal: the amounts are
        retuned, and a hard-coded number would fail on a retune while still
        passing against a board that advertises nothing.
        """
        pid = make_player(client, "MbReward")
        body = get_board(client, pid).json()
        by_id = {o["objective_id"]: o for o in body["objectives"]}
        for row in BOARD_OBJECTIVES:
            expected = mission_board.reward_for(row)
            assert by_id[row["objective_id"]]["reward"] == expected, \
                row["objective_id"]

    def test_the_board_never_pays_fateum(self, client):
        """The only currency the board may pay is spirit stone."""
        pid = make_player(client, "MbCurrency")
        body = get_board(client, pid).json()
        for entry in body["objectives"]:
            for key in entry["reward"]:
                assert "fateum" not in str(key).lower(), entry["objective_id"]


class TestOwnership:
    """The body/query player_id is a CLAIM; the session is the authority."""

    def test_reading_another_players_board_is_forbidden(self, client):
        me = make_player(client, "MbOwnerA")
        victim = other_player(client, "MbOwnerB")
        r = get_board(client, victim)
        assert r.status_code == 403, r.text
        assert "objectives" not in r.json(), (
            "a 403 that still carried a board would leak it in the body")

    def test_a_forbidden_read_does_not_touch_the_victims_board(self, client):
        me = make_player(client, "MbOwnerC")
        victim = other_player(client, "MbOwnerD")
        # Read the victim's board as them, so there IS a board to protect.
        client.sign_in(victim)
        victim_board = get_board(client, victim).json()
        client.sign_in(me)
        assert get_board(client, victim).status_code == 403
        client.sign_in(victim)
        after = get_board(client, victim).json()
        assert after["objectives"] == victim_board["objectives"], (
            "a forbidden read changed the victim's board")

    def test_claiming_for_another_player_is_forbidden(self, client):
        me = make_player(client, "MbClaimA")
        victim = other_player(client, "MbClaimB")
        stones_before = _stones(victim)
        r = claim(client, victim, key="steal-1")
        assert r.status_code == 403, r.text
        assert _stones(victim) == stones_before, (
            "a forbidden claim moved the victim's currency")

    def test_a_non_string_player_id_is_refused_by_ownership(self, client):
        """A number where the id belongs is refused, never coerced into a match.

        Ownership reads the RAW body before the model parses it, so a caller
        cannot present a different type for the same id and slip past the check.
        """
        me = make_player(client, "MbInt")
        r = client.post(CLAIM, json={"player_id": 5, "objective_id": OBJECTIVE},
                        headers={"Idempotency-Key": "int-1"})
        assert r.status_code == 403, r.text
        # ...and the caller's OWN id, correctly typed, still reaches the handler.
        mine = client.post(CLAIM, json={"player_id": me,
                                        "objective_id": OBJECTIVE},
                           headers={"Idempotency-Key": "int-2"})
        assert mine.status_code == 400, (
            f"the caller's own id was not served normally: {mine.text}")


def _stones(pid):
    account = main.wallet_store.get(pid)
    return 0 if account is None else account.balance.to_dict().get("spirit_stone", 0)


class TestClaimRequestShape:
    """POST /mission-board/claim: what the caller must send."""

    def test_both_fields_are_required(self, client):
        pid = make_player(client, "MbReqA")
        r = client.post(CLAIM, json={}, headers={"Idempotency-Key": "req-1"})
        assert r.status_code == 422, r.text
        assert body_loc_names(r) == {"player_id", "objective_id"}, r.text

    def test_a_missing_objective_id_names_only_that_field(self, client):
        pid = make_player(client, "MbReqB")
        r = client.post(CLAIM, json={"player_id": pid},
                        headers={"Idempotency-Key": "req-2"})
        assert r.status_code == 422, r.text
        assert body_loc_names(r) == {"objective_id"}, r.text

    def test_a_request_with_no_body_at_all_is_refused(self, client):
        make_player(client, "MbReqC")
        r = client.post(CLAIM, headers={"Idempotency-Key": "req-3"})
        assert r.status_code == 422, r.text

    def test_the_id_is_read_from_the_body_and_not_the_query(self, client):
        """The client's real shape: the id in the JSON body.

        A body-borne id that FastAPI decided was a query value would answer 422
        naming ``["query","player_id"]``. Asserted so the two failure modes stay
        distinguishable -- this is the exact detector the negative control proves.
        """
        pid = make_player(client, "MbReqD")
        r = claim(client, pid, key="req-4")
        assert not missing_query_player_id(r), r.text
        assert r.status_code == 400, r.text  # complete-or-not, past the framework
        assert "query" not in r.text

    def test_an_unknown_body_field_is_ignored(self, client):
        """Extra fields do not reject a request; they are simply not read."""
        pid = make_player(client, "MbReqE")
        r = claim(client, pid, key="req-5", amount=9999)
        assert r.status_code != 422, (
            f"an extra body field started rejecting the request: {r.text}")
        assert r.status_code == 400, r.text  # incomplete, not a shape error


class TestIdempotencyKeyHeader:
    """The key is required, never minted. Measured against idem_gate's rules."""

    def test_a_missing_key_is_refused(self, client):
        pid = make_player(client, "MbIdemA")
        r = claim(client, pid, key=None)
        assert r.status_code == 400, r.text
        assert "Idempotency-Key" in r.json()["detail"], r.text

    def test_a_blank_key_is_refused(self, client):
        """Whitespace is not a key. The server will not invent one."""
        pid = make_player(client, "MbIdemB")
        r = claim(client, pid, key="   ")
        assert r.status_code == 400, r.text

    def test_an_oversized_key_is_refused(self, client):
        pid = make_player(client, "MbIdemC")
        from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
        r = claim(client, pid, key="x" * (MAX_IDEMPOTENCY_KEY_LEN + 1))
        assert r.status_code == 400, r.text
        assert "too long" in r.json()["detail"].lower(), r.text

    def test_a_key_at_the_limit_is_accepted(self, client):
        """The bound is inclusive: exactly the limit still reaches the handler.

        Asserted against the handler's own refusal ("Objective not complete"),
        which is the proof the key got PAST the gate rather than being rejected
        by it.
        """
        pid = make_player(client, "MbIdemD")
        from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
        r = claim(client, pid, key="y" * MAX_IDEMPOTENCY_KEY_LEN)
        assert r.status_code == 400, r.text
        assert "Idempotency-Key" not in r.json()["detail"], (
            "a key exactly at the limit was refused by the gate")

    def test_a_key_carrying_a_control_character_is_refused(self, client):
        pid = make_player(client, "MbIdemE")
        r = claim(client, pid, key="a\x00b")
        assert r.status_code == 400, r.text
        assert "control character" in r.json()["detail"], r.text

    def test_the_key_is_checked_before_the_player_is_looked_up(self, client):
        """An unauthenticated caller naming an unknown player gets 401 twice.

        The two arms must be BYTE-IDENTICAL: if the idempotency-key gate ran
        before auth, the keyless call would answer 400 while the keyed call
        answered 404, and the difference would tell an anonymous caller whether
        the player id is real. Pinning the order means pinning that they match.
        """
        client.sign_in("__no_such_player__")
        missing = claim(client, "__no_such_player__", key=None)
        with_key = claim(client, "__no_such_player__", key="idem-order")
        assert missing.status_code == 401, missing.text
        assert with_key.status_code == 401, with_key.text
        assert missing.json() == with_key.json(), (
            "the keyed and keyless arms must be indistinguishable to an "
            "anonymous caller, or the route is a player-id oracle")

    def test_the_key_gate_is_reachable_for_a_real_player(self, client):
        """Control: the 400 on a missing key IS reachable once you can sign in.

        Without this, the test above would pass just as well on a route that had
        lost its idempotency-key gate entirely.
        """
        pid = make_player(client, "MbIdemF")
        r = claim(client, pid, key=None)
        assert r.status_code == 400, r.text
        assert "Idempotency-Key" in r.json()["detail"], r.text


class TestClaimErrorCases:
    """The refusals a genuine mistake produces, and which status each gets."""

    def test_an_unknown_player_is_not_a_404_oracle(self, client):
        """An unknown player id is refused at auth, never at a lookup.

        Auth runs before the player is looked up, so a caller who cannot
        authenticate learns nothing about whether the id exists: the answer is
        401 and the body must not mention a lookup. A 404 here would be a
        player-id oracle for anyone willing to send an unauthenticated call.
        """
        client.sign_in("__no_such_player__")
        r = claim(client, "__no_such_player__", key="err-1")
        assert r.status_code == 401, r.text
        assert "not found" not in r.json()["detail"].lower(), (
            "the refusal must not disclose whether the player exists")

    def test_an_unknown_objective_is_404(self, client):
        pid = make_player(client, "MbErrB")
        r = claim(client, pid, objective_id="__no_such_objective__", key="err-2")
        assert r.status_code == 404, r.text
        assert "objective" in r.json()["detail"].lower(), r.text

    def test_an_incomplete_objective_is_400(self, client):
        """Complete the board read first, so "incomplete" is measured."""
        pid = make_player(client, "MbErrC")
        get_board(client, pid)  # stamps the baseline for this cycle
        r = claim(client, pid, key="err-3")
        assert r.status_code == 400, r.text
        assert "not complete" in r.json()["detail"].lower(), r.text

    def test_the_player_is_checked_before_the_objective(self, client):
        """Two wrong things at once: auth wins, and nothing is disclosed.

        A caller must not be able to learn which objective ids exist by naming
        a player that does not exist. Measured on this tree: both refusals are
        401, because ``Depends(require_player)`` refuses a session whose player
        was never stored BEFORE the handler runs its store lookup. So the two
        lookups this test is about are never reached.

        The oracle question is therefore answered by comparing the two ARMS, not
        by a status code: an unknown player with a REAL objective must answer
        byte-identically to an unknown player with a made-up objective. If the
        handler looked either one up, one arm would be 404 "Unknown objective"
        and the other 401, and the pair would enumerate the objective table.

        ``test_the_objective_is_checked_before_completion`` below is the control
        that the 404 objective arm is reachable once auth passes, so the 401
        above is the auth seam and not a route that cannot name an objective.
        """
        client.sign_in("__no_such_player__")
        both_wrong = claim(client, "__no_such_player__",
                           objective_id="also_not_real", key="err-4")
        objective_real = claim(client, "__no_such_player__",
                               objective_id=OBJECTIVE, key="err-4-real-obj")
        assert both_wrong.status_code == 401, both_wrong.text
        assert objective_real.status_code == 401, objective_real.text
        assert both_wrong.json() == objective_real.json(), (
            "the two arms differ, so naming an objective discloses whether it "
            "exists")

    def test_the_objective_is_checked_before_completion(self, client):
        """An unknown objective is a 404 even when nothing is complete."""
        pid = make_player(client, "MbErrD")
        r = claim(client, pid, objective_id="__no_such__", key="err-5")
        assert r.status_code == 404, r.text
        assert "objective" in r.json()["detail"].lower(), r.text

    def test_a_refusal_pays_nothing(self, client):
        pid = make_player(client, "MbErrE")
        get_board(client, pid)
        before_stones, bag_before = _stones(pid), _held(pid)
        assert claim(client, pid, key="err-6").status_code == 400
        assert _stones(pid) == before_stones, "a refusal paid currency"
        assert _held(pid) == bag_before, "a refusal credited an item"


def _held(pid):
    """The reward material in the bag, read from the canonical store."""
    items = {mission_board.TIER_MATERIAL[row["cycle"]]
             for row in BOARD_OBJECTIVES}
    return {item: main.inventory_store.get_item_count(pid, item)
            for item in items}


class TestClaimSuccessShape:
    """The paid arm, field by field."""

    @pytest.fixture()
    def paid(self, client):
        pid = make_player(client, "MbPaid")
        entry = complete_quick(client, pid)
        r = claim(client, pid, key="pay-1")
        assert r.status_code == 200, r.text
        return pid, entry, r.json()

    def test_the_paid_arm_answers_the_full_seven_key_shape(self, paid):
        _, _, body = paid
        assert is_paid_arm(body), (
            f"the paid arm answered {sorted(body)}; expected "
            f"{sorted(COMMON_CLAIM_KEYS | PAID_ONLY_CLAIM_KEYS)}")

    def test_the_paid_arm_reports_success(self, paid):
        _, _, body = paid
        assert body["ok"] is True, body
        assert body["already_claimed"] is False, body

    def test_the_paid_arm_names_the_objective_and_its_cycle(self, paid):
        _, _, body = paid
        assert body["objective_id"] == OBJECTIVE, body
        # cycle_key must be the key the board is currently serving, so a client
        # can match a claim answer against the board it just read.
        from mission_board import cycle_key
        assert body["cycle_key"] == cycle_key(
            next(o for o in BOARD_OBJECTIVES
                 if o["objective_id"] == OBJECTIVE)["cycle"]), body

    def test_the_paid_arm_advertises_the_reward_the_board_showed(self, paid):
        _, entry, body = paid
        assert body["reward"] == entry["reward"], (
            "the claim paid something other than what the board advertised")

    def test_the_paid_arm_echoes_the_namespaced_idempotency_key(self, paid):
        _, _, body = paid
        assert body["idempotency_key"] == "mission_board:pay-1", (
            "the echoed key must be the namespaced one, so a retry that stored "
            "the server's value replays instead of charging again")

    def test_the_paid_arm_names_the_items_it_credited(self, paid):
        _, entry, body = paid
        expected = {"item_id": entry["reward"]["material"]["item_id"],
                    "quantity": entry["reward"]["material"]["quantity"]}
        assert body["credited_items"] == [expected], body

    def test_the_paid_arm_moves_the_currency_it_advertised(self, client):
        pid = make_player(client, "MbPaidStone")
        entry = complete_quick(client, pid)
        before = _stones(pid)
        claim(client, pid, key="pay-2")
        assert _stones(pid) - before == entry["reward"]["spirit_stone"], (
            "the claim advertised a stone amount and paid a different one")

    def test_the_paid_arm_marks_the_row_claimed_on_the_next_read(self, client):
        pid = make_player(client, "MbPaidMark")
        complete_quick(client, pid)
        claim(client, pid, key="pay-3")
        entry = board_entry(client, pid, OBJECTIVE)
        assert entry["claimed"] is True, entry


class TestAlreadyClaimedArm:
    """The refusal arm. 200 with ok=false -- a retry that pays nothing is a
    SUCCESS for the player, so it is not an error status."""

    def test_a_second_claim_with_the_same_key_is_200_not_an_error(self, client):
        pid = make_player(client, "MbAgainA")
        complete_quick(client, pid)
        assert claim(client, pid, key="dup-1").json()["ok"] is True
        r = claim(client, pid, key="dup-1")
        assert r.status_code == 200, (
            f"a retry answered {r.status_code}: {r.text}. The client's retry "
            f"path reads ok, and an error status would be a false failure.")
        assert r.json()["ok"] is False, r.text

    def test_a_second_claim_with_a_fresh_key_is_also_refused(self, client):
        """A per-request key cannot catch this one; the board's marker can."""
        pid = make_player(client, "MbAgainB")
        complete_quick(client, pid)
        claim(client, pid, key="dup-2")
        r = claim(client, pid, key="a-completely-different-key")
        assert r.status_code == 200, r.text
        assert r.json()["already_claimed"] is True, r.text

    def test_the_refused_arm_answers_the_five_key_shape(self, client):
        pid = make_player(client, "MbAgainC")
        complete_quick(client, pid)
        claim(client, pid, key="dup-3")
        body = claim(client, pid, key="dup-4").json()
        assert is_already_claimed_arm(body), (
            f"the already-claimed arm answered {sorted(body)}; expected the five "
            f"common keys and none of {sorted(PAID_ONLY_CLAIM_KEYS)}")

    def test_the_refused_arm_reports_no_reward(self, client):
        pid = make_player(client, "MbAgainD")
        complete_quick(client, pid)
        claim(client, pid, key="dup-5")
        body = claim(client, pid, key="dup-6").json()
        assert body["reward"] == {}, (
            "a refused claim still advertised a reward; a client would show the "
            "player a payout they did not receive")

    def test_a_retry_pays_nothing_at_all(self, client):
        pid = make_player(client, "MbAgainE")
        entry = complete_quick(client, pid)
        item_id = entry["reward"]["material"]["item_id"]
        claim(client, pid, key="dup-7")
        stones_after_first, held_after_first = _stones(pid), _held(pid)
        for key in ("dup-7", "dup-8", "dup-9"):
            claim(client, pid, key=key)
        assert _stones(pid) == stones_after_first, "a retry paid currency twice"
        assert _held(pid) == held_after_first, "a retry credited the item twice"

    def test_claiming_one_objective_does_not_consume_another(self, client):
        """Two objectives, two independent claims."""
        pid = make_player(client, "MbAgainF")
        weekly = next(o for o in BOARD_OBJECTIVES
                      if o["objective_id"] == WEEKLY_OBJECTIVE)
        set_source(pid, "pvp_wins", weekly["target"] + 5)
        complete_quick(client, pid)
        assert claim(client, pid, objective_id=OBJECTIVE,
                     key="two-1").json()["ok"] is True
        second = claim(client, pid, objective_id=WEEKLY_OBJECTIVE, key="two-2")
        assert second.status_code == 200, second.text
        assert second.json()["ok"] is True, second.text

    def test_a_fresh_player_has_nothing_already_claimed(self, client):
        pid = make_player(client, "MbAgainG")
        body = get_board(client, pid).json()
        assert all(o["claimed"] is False for o in body["objectives"])


class TestMethodNegatives:
    """The two paths answer only their own method."""

    def test_posting_to_the_board_path_is_405(self, client):
        pid = make_player(client, "MbMethA")
        r = client.post(BOARD, params={"player_id": pid})
        assert r.status_code == 405, r.text

    def test_getting_the_claim_path_is_405(self, client):
        make_player(client, "MbMethB")
        r = client.get(CLAIM)
        assert r.status_code == 405, r.text

    def test_a_405_does_not_pay_or_write(self, client):
        pid = make_player(client, "MbMethC")
        complete_quick(client, pid)
        before = _stones(pid)
        assert client.get(CLAIM).status_code == 405
        assert _stones(pid) == before, "a 405 moved currency"


class TestNegativeControls:
    """Proof the assertions above are not vacuous.

    Each control is defined HERE rather than pointed at another module's route,
    so it cannot rot when an owner fixes their file.
    """

    def test_claim_shape_detector_tells_the_two_arms_apart(self):
        """The detector used by ``is_paid_arm`` / ``is_already_claimed_arm``.

        A detector that recognises nothing would report BOTH arms correct, which
        is exactly the state the shape assertions above exist to rule out.
        """
        paid = dict.fromkeys(COMMON_CLAIM_KEYS | PAID_ONLY_CLAIM_KEYS, 1)
        refused = dict.fromkeys(COMMON_CLAIM_KEYS, 1)
        # A body the two arms could both be confused for: the common keys plus
        # ONE of the paid-only keys. The detector must refuse it.
        half = dict.fromkeys(COMMON_CLAIM_KEYS | {"idempotency_key"}, 1)

        assert is_paid_arm(paid) is True
        assert is_already_claimed_arm(paid) is False
        assert is_already_claimed_arm(refused) is True
        assert is_paid_arm(refused) is False
        assert is_paid_arm(half) is False, (
            "the detector accepted a body carrying only one paid-only key")
        assert is_already_claimed_arm(half) is False, (
            "the detector accepted a body carrying a paid-only key as refused")
        assert is_paid_arm({}) is False
        assert is_already_claimed_arm({}) is False

    def test_query_id_detector_flags_a_query_only_route(self):
        """The detector used by ``missing_query_player_id``.

        A throwaway app carries the exact defect shape: a bare scalar that
        FastAPI decides is a query value, while a real client sends the id in
        the body. The detector must flag the first and not the second.

        ``_ProbeBody`` is at MODULE scope on purpose -- this file carries
        ``from __future__ import annotations``, so FastAPI resolves a body model
        against module globals. A model declared inside a function is not there,
        the forward reference never resolves, and FastAPI silently demotes the
        body to a query parameter -- the probe would be testing a second,
        unrelated bug and would pass for the wrong reason.
        """
        probe = FastAPI()

        @probe.post("/broken")
        def broken(player_id: str, req: _ProbeBody):  # bare scalar == query
            return {"player_id": player_id, "objective_id": req.objective_id}

        with TestClient(probe, raise_server_exceptions=False) as c:
            in_body = c.post("/broken", json={"player_id": "p1",
                                              "objective_id": "m"})
            assert missing_query_player_id(in_body), in_body.text
            in_query = c.post("/broken?player_id=p1", json={"objective_id": "m"})
            assert in_query.status_code == 200, in_query.text
            assert not missing_query_player_id(in_query)
            bad_body = c.post("/broken?player_id=p1", json={})
            assert bad_body.status_code == 422, bad_body.text
            assert not missing_query_player_id(bad_body), (
                "a body validation failure was mistaken for the query defect")

    def test_the_ownership_guard_really_is_what_answers_403(self, client):
        """A 403 must come from ownership, not from an incidental 404 or 422.

        Proven by construction: the SAME request naming the caller's OWN id is
        served normally, and only the substituted id produces the 403. So the
        403 tracks the substitution, not the objective, the key or the shape.
        """
        me = make_player(client, "MbCtrl")
        victim = other_player(client, "MbCtrlB")

        mine = claim(client, me, key="ctrl-1")
        assert mine.status_code == 400, mine.text  # mine: not complete yet
        assert "not complete" in mine.json()["detail"].lower(), mine.text
        theirs = claim(client, victim, key="ctrl-2")
        assert theirs.status_code == 403, theirs.text

    def test_the_board_is_actually_readable_by_the_caller(self, client):
        """The 403 tests only mean something if a legal read still works."""
        me = make_player(client, "MbCtrlC")
        r = get_board(client, me)
        assert r.status_code == 200, r.text
        assert r.json()["objectives"], r.text


class _ProbeBody(BaseModel):
    """Body model for the negative control's throwaway app. Module scope."""

    objective_id: str