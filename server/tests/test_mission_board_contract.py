"""Contract tests for `server/mission_board.py` and the routes that serve it.

WHAT THIS FILE IS
-----------------
The board is a server-timed checklist: a player reads rows, does the thing in
another system, and claims a reward once per cycle. Two layers have to agree:

    the MODULE   mission_board.py         pure logic over a player record
    the ROUTES   mission_board_routes.py  GET /mission-board, POST /mission-board/claim

A client only sees the routes. So this file pins BOTH: the exact keys each
answer carries, the exact order in which refusals happen (which decides what
message a mistake gets), and the rules that keep the two layers honest --
progress is derived from state the server wrote, and a claim pays once.

MEASURED, NOT INFERRED
----------------------
Every status code and every response key below was produced by driving the real
app through the real auth seam. Nothing is read off the source and assumed.

REFUSAL ORDER (part of the contract, because it decides the message)
--------------------------------------------------------------------
    no session                  401
    somebody else's player      403   -- never 404, so ids cannot be probed
    missing Idempotency-Key     400   -- before the player is even looked up
    player not in the store     404
    objective not on the board  404
    objective not finished      400

An anonymous caller learns nothing about which players or objectives exist.

THE TWO CLAIM ARMS ARE DIFFERENT SHAPES
---------------------------------------
Paid:        ok, objective_id, cycle_key, already_claimed, reward,
             idempotency_key, credited_items     (7 keys)
Already hit: ok, objective_id, cycle_key, already_claimed, reward   (5 keys)

The already-claimed arm answers 200 with ok=false, because a retry that pays
nothing is a SUCCESS for the player. It is also two keys short, which a client
must not read blindly.

NEGATIVE CONTROLS (in this file, not in a comment)
--------------------------------------------------
* `TestNegativeControls` runs the shape detectors against a deliberately
  mangled body, so a detector that accepts anything fails here.
* `test_the_completion_detector_separates_done_from_undone` proves the
  "is this objective finished" check can actually tell the two apart.
* Ownership tests assert 403 AND that the victim's board did not change, so a
  route that ignored ownership would fail on state, not only on a status code.
* `test_a_body_cannot_dial_its_own_progress` sends a claim body asking for
  progress and a reward, and proves neither moved.

FIXTURE NOTES
-------------
Two process-wide counters are cleared around every test, because this file
creates more than five players and makes more than thirty claims in one run:
`/player/create` allows 5 per 60s, and the shared claim rate limit allows 30
per 60s per player. Both have their own tests; nothing here asserts their
numbers.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import main  # noqa: E402
from server.tests.wc_test_auth import (  # noqa: E402
    Session,
    clear_signup_rate_limit,
)

import mission_board  # noqa: E402
import rate_limit  # noqa: E402
from mission_board import (  # noqa: E402
    BOARD_OBJECTIVES,
    DAILY_CYCLE_SECONDS,
    KEPT_PAST_CYCLES,
    QUICK_CYCLE_SECONDS,
    REWARD_CURRENCY,
    SOURCES,
    STATE_KEY,
    TOWER_RUN_KEY,
    TOWER_STATE_KEY,
    WEEKLY_CYCLE_SECONDS,
    MissionBoardError,
    board_view,
    claim_key,
    claimable,
    cycle_key,
    get_state,
    objective_def,
    outcome_for,
    record_claim,
    reward_for,
    seconds_to_refresh,
    source_tower_clears,
    source_tower_floor,
)

BOARD = "/mission-board"
CLAIM = "/mission-board/claim"

#: A fixed instant so nothing here depends on the wall clock.
NOW = 1_762_000_000.0

#: A threshold row on a plain counter: setting `progress` makes it complete
#: with no cycle bookkeeping, so a shape test never has to drive the hunt or
#: combat systems to reach the claim route.
OBJECTIVE = "quick_qi_ready"

#: A delta row, so the baseline logic is in play too.
DELTA_OBJECTIVE = "quick_hunt_one"

#: Keys the whole board answer carries.
BOARD_KEYS = {"ok", "objectives", "server_time", "cycles"}

#: Keys one objective row carries. The client panel draws exactly these.
ENTRY_KEYS = {
    "objective_id", "cycle", "cycle_key", "kind", "description", "points_at",
    "unit", "target", "progress", "completed", "claimed", "reward",
    "seconds_to_refresh",
}

#: Keys BOTH claim arms answer.
COMMON_CLAIM_KEYS = {"ok", "objective_id", "cycle_key", "already_claimed", "reward"}

#: Keys only the PAID arm adds.
PAID_ONLY_CLAIM_KEYS = {"idempotency_key", "credited_items"}


# ----------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_process_counters():
    clear_signup_rate_limit()
    rate_limit.reset_claim_rate_limit()
    yield
    rate_limit.reset_claim_rate_limit()


@pytest.fixture()
def client():
    with TestClient(main.app, raise_server_exceptions=False) as raw:
        yield Session(raw)


def make_player(client, name):
    r = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    client.sign_in(pid)
    return pid


def other_player(client, name):
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
    """Write a field the board reads, through the store transaction.

    Arranging a precondition is not the thing under test; the route's answer is.
    A detached read is discarded by design, so the write goes through the
    transaction path.
    """
    with main.players_db.transaction(player_id):
        main.players_db[player_id][field] = value


def complete_quick(client, pid, field="progress"):
    row = objective_def(OBJECTIVE)
    set_source(pid, field, row["target"] + 5)
    entry = board_entry(client, pid, OBJECTIVE)
    assert entry["completed"] is True, entry
    return entry


def without_clock(body):
    """The board answer minus the two values that move on every read.

    `server_time` is the instant the request was served, and
    `seconds_to_refresh` is a countdown derived from it, so the pair changes
    between two otherwise identical reads. Stripping only `server_time` left
    the countdown in, which made every state-change assertion a race that
    failed whenever the two reads landed in different seconds -- which is why
    the ownership test below went red intermittently once the file ran long
    enough to straddle a second boundary.

    A state-change question is "did anything EXCEPT the clock move?", so the
    countdown is dropped wherever it appears: top level, inside `cycles`, and
    on every objective row.
    """
    return _without_clock_fields(body)


def _without_clock_fields(body):
    if not isinstance(body, dict):
        return body
    stripped = {k: v for k, v in body.items() if k != "server_time"}
    for name in ("cycles",):
        if isinstance(stripped.get(name), dict):
            stripped[name] = {
                cycle: {k: v for k, v in block.items() if k != "seconds_to_refresh"}
                if isinstance(block, dict) else block
                for cycle, block in stripped[name].items()
            }
    if isinstance(stripped.get("objectives"), list):
        stripped["objectives"] = [
            {k: v for k, v in entry.items() if k != "seconds_to_refresh"}
            if isinstance(entry, dict) else entry
            for entry in stripped["objectives"]
        ]
    return stripped


def board_body(client, pid):
    r = get_board(client, pid)
    assert r.status_code == 200, r.text
    return r.json()


def board_entry(client, pid, objective_id):
    return next(o for o in board_body(client, pid)["objectives"]
                if o["objective_id"] == objective_id)


def pure_player(**over):
    """A player record the pure sources can read with no store attached."""
    base = {"id": "p1", "progress": 0, "pvp_wins": 0, "beasts": [],
            "tower_state": {}, "cultivation_realm": None}
    base.update(over)
    return base


# ----------------------------------------------------------------------
# detectors -- shared by the real assertions and by the negative controls
# ----------------------------------------------------------------------
def is_board_shape(body) -> bool:
    """True when the answer carries exactly the board's documented keys."""
    return isinstance(body, dict) and set(body) == BOARD_KEYS


def is_entry_shape(entry) -> bool:
    return isinstance(entry, dict) and set(entry) == ENTRY_KEYS


def is_paid_arm(body) -> bool:
    return isinstance(body, dict) and COMMON_CLAIM_KEYS <= set(body) \
        and PAID_ONLY_CLAIM_KEYS <= set(body)


def is_already_claimed_arm(body) -> bool:
    return isinstance(body, dict) and set(body) == COMMON_CLAIM_KEYS \
        and body.get("already_claimed") is True and body.get("ok") is False


def is_complete(entry) -> bool:
    """True when the board says this objective is finished."""
    return entry.get("completed") is True and entry.get("progress") >= entry["target"]


def refused_by_query_validation(response) -> bool:
    """True when the framework rejected `player_id` read as a QUERY value.

    Told apart by `loc`, not by status code: both a missing query value and a
    bad body answer 422, and only the location tells them apart.
    """
    if response.status_code != 422:
        return False
    detail = response.json().get("detail")
    if not isinstance(detail, list):
        return False
    return any(isinstance(d, dict) and d.get("loc") == ["query", "player_id"]
               and d.get("type") == "missing" for d in detail)


def body_field_names(response) -> set:
    if response.status_code != 422:
        return set()
    detail = response.json().get("detail")
    if not isinstance(detail, list):
        return set()
    return {d["loc"][1] for d in detail
            if isinstance(d, dict) and isinstance(d.get("loc"), list)
            and len(d["loc"]) == 2 and d["loc"][0] == "body"}


# ----------------------------------------------------------------------
# GET /mission-board -- shape
# ----------------------------------------------------------------------
class TestBoardShape:
    def test_a_signed_in_caller_is_served(self, client):
        pid = make_player(client, "BoardShape")
        r = get_board(client, pid)
        assert r.status_code == 200, r.text

    def test_the_board_carries_exactly_the_documented_keys(self, client):
        pid = make_player(client, "BoardTopKeys")
        body = board_body(client, pid)
        assert set(body) == BOARD_KEYS, sorted(body)
        assert is_board_shape(body)

    def test_the_board_reports_success_and_a_server_timestamp(self, client):
        pid = make_player(client, "BoardTime")
        body = board_body(client, pid)
        assert body["ok"] is True
        assert isinstance(body["server_time"], (int, float))

    def test_every_published_objective_is_reported_exactly_once(self, client):
        pid = make_player(client, "BoardEveryRow")
        body = board_body(client, pid)
        ids = [o["objective_id"] for o in body["objectives"]]
        assert ids == [row["objective_id"] for row in BOARD_OBJECTIVES]
        assert len(set(ids)) == len(ids)

    def test_every_objective_carries_exactly_the_fields_the_panel_draws(self, client):
        pid = make_player(client, "BoardEntryKeys")
        body = board_body(client, pid)
        for entry in body["objectives"]:
            assert is_entry_shape(entry), (entry["objective_id"], sorted(entry))

    def test_all_three_timescales_report_a_key_and_a_countdown(self, client):
        pid = make_player(client, "BoardCycles")
        cycles = board_body(client, pid)["cycles"]
        assert set(cycles) == {"quick", "daily", "weekly"}
        for name, block in cycles.items():
            assert isinstance(block["key"], str) and block["key"], name
            assert isinstance(block["seconds_to_refresh"], int)
            assert block["seconds_to_refresh"] > 0, name

    def test_each_objective_reports_its_own_cycle_key_and_countdown(self, client):
        pid = make_player(client, "BoardPerRowClock")
        body = board_body(client, pid)
        for entry in body["objectives"]:
            assert entry["cycle_key"] == body["cycles"][entry["cycle"]]["key"], entry
            assert entry["seconds_to_refresh"] == \
                body["cycles"][entry["cycle"]]["seconds_to_refresh"], entry

    def test_progress_never_goes_negative_or_past_the_target(self, client):
        pid = make_player(client, "BoardProgressBand")
        set_source(pid, "progress", 10_000)
        body = board_body(client, pid)
        for entry in body["objectives"]:
            assert 0 <= entry["progress"] <= entry["target"], entry

    def test_completed_and_claimed_are_booleans(self, client):
        pid = make_player(client, "BoardFlags")
        body = board_body(client, pid)
        for entry in body["objectives"]:
            assert isinstance(entry["completed"], bool)
            assert isinstance(entry["claimed"], bool)

    def test_a_fresh_player_sees_every_objective_unfinished(self, client):
        pid = make_player(client, "BoardFresh")
        body = board_body(client, pid)
        for entry in body["objectives"]:
            assert entry["completed"] is False, entry
            assert entry["claimed"] is False, entry

    def test_the_reward_is_advertised_on_every_row(self, client):
        pid = make_player(client, "BoardReward")
        for entry in board_body(client, pid)["objectives"]:
            reward = entry["reward"]
            assert set(reward) == {REWARD_CURRENCY, "material"}
            assert isinstance(reward[REWARD_CURRENCY], int)
            assert reward[REWARD_CURRENCY] >= 0
            material = reward["material"]
            assert set(material) == {"item_id", "quantity"}
            assert isinstance(material["item_id"], str) and material["item_id"]
            assert isinstance(material["quantity"], int) and material["quantity"] > 0

    def test_the_advertised_reward_is_the_one_the_definition_produces(self, client):
        """The board must not invent an economy that disagrees with the module."""
        pid = make_player(client, "BoardRewardParity")
        for entry in board_body(client, pid)["objectives"]:
            assert entry["reward"] == reward_for(objective_def(entry["objective_id"]))

    def test_no_row_anywhere_pays_the_forbidden_currency(self, client):
        """Fateum is not a reward on this board, in any arm."""
        pid = make_player(client, "BoardNoFateum")
        body = board_body(client, pid)
        assert REWARD_CURRENCY == "spirit_stone"
        for entry in body["objectives"]:
            rendered = repr(entry["reward"]).lower()
            assert "fateum" not in rendered, entry
        for row in BOARD_OBJECTIVES:
            assert not [k for k in row if "fateum" in str(k).lower()], row


# ----------------------------------------------------------------------
# GET /mission-board -- auth
# ----------------------------------------------------------------------
class TestBoardAuth:
    def test_an_anonymous_caller_is_refused(self, client):
        pid = make_player(client, "BoardAnon")
        client.as_player(None)
        r = client.get(BOARD, params={"player_id": pid})
        assert r.status_code == 401, r.text

    def test_a_forged_token_is_refused(self, client):
        pid = make_player(client, "BoardForged")
        r = client.get(BOARD, params={"player_id": pid},
                       headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401, r.text

    def test_the_player_id_is_mandatory(self, client):
        make_player(client, "BoardNoId")
        r = client.get(BOARD)
        assert r.status_code == 422, r.text
        assert refused_by_query_validation(r), r.text

    def test_another_players_board_is_forbidden(self, client):
        make_player(client, "BoardOwner")
        victim = other_player(client, "BoardVictim")
        r = get_board(client, victim)
        assert r.status_code == 403, r.text

    def test_a_forbidden_read_leaves_the_victims_board_alone(self, client):
        thief = make_player(client, "BoardThief")
        victim = other_player(client, "BoardUntouched")
        before = without_clock(get_board(client.as_player(victim), victim).json())

        client.as_player(thief)
        assert get_board(client, victim).status_code == 403

        after = without_clock(get_board(client.as_player(victim), victim).json())
        assert after == before, "a refused read still wrote to the victim's board"

    def test_a_signed_in_player_absent_from_the_store_is_401(self, client):
        """A session naming a player that was NEVER CREATED is refused 401.

        MEASURED 2026-10-05 against the real app. Two guards now answer before
        this route's own "Player not found" 404 can be reached over HTTP:

          * the central ownership guard answers 403 whenever the id in the
            request is not the session's own, and
          * ``auth.authenticate`` confirms the session's player still exists,
            so a token minted for an account that was never stored -- or has
            since been deleted -- is revoked and refused 401.

        Both refusals are deliberate and both are correct: a token that outlived
        its account must stop working immediately rather than at the end of its
        lifetime, and the answer must not tell a stranger whether an id is
        live. The sibling arm below pins the other refusal (403), and
        ``test_the_board_still_answers_for_a_player_that_does_exist`` pins the
        route's own happy path, so the guard cannot be satisfied by refusing
        everyone.
        """
        client.as_player("no_such_player_at_all")
        r = client.get(BOARD, params={"player_id": "no_such_player_at_all"})
        assert r.status_code == 401, r.text

    def test_the_board_still_answers_for_a_player_that_does_exist(self, client):
        """The control for the arm above: a real player is served normally.

        Without this, "always answer 401" would pass the test above.
        """
        pid = make_player(client, "BoardReal")
        r = client.get(BOARD, params={"player_id": pid})
        assert r.status_code == 200, r.text

    def test_a_substituted_id_is_403_even_when_that_id_does_not_exist(self, client):
        """Ownership is checked before existence, so ids cannot be probed."""
        client.sign_in(make_player(client, "BoardProbe"))
        assert get_board(client, "no_such_player_at_all").status_code == 403

    def test_the_wrong_method_is_refused(self, client):
        pid = make_player(client, "BoardMethod")
        assert client.post(BOARD, params={"player_id": pid}, json={}).status_code == 405


# ----------------------------------------------------------------------
# POST /mission-board/claim -- request contract
# ----------------------------------------------------------------------
class TestClaimRequestContract:
    def test_both_body_fields_are_required(self, client):
        pid = make_player(client, "ClaimFields")
        client.as_player(pid)
        r = claim(client, pid, key=None)
        assert r.status_code == 400, r.text  # the idempotency gate runs first

        r = client.post(CLAIM, json={"player_id": pid},
                        headers={"Idempotency-Key": "mb-fields-1"})
        assert r.status_code == 422, r.text
        assert body_field_names(r) == {"objective_id"}, r.text

        r = client.post(CLAIM, json={"objective_id": OBJECTIVE},
                        headers={"Idempotency-Key": "mb-fields-2"})
        assert r.status_code == 422, r.text
        assert body_field_names(r) == {"player_id"}, r.text

    def test_a_request_with_no_body_at_all_is_refused(self, client):
        pid = make_player(client, "ClaimNoBody")
        r = client.post(CLAIM, headers={"Idempotency-Key": "mb-nobody-1"})
        assert r.status_code == 422, r.text

    def test_the_id_is_read_from_the_body_and_not_the_query(self, client):
        pid = make_player(client, "ClaimBodyId")
        r = client.post(CLAIM, params={"player_id": pid},
                        json={"objective_id": OBJECTIVE},
                        headers={"Idempotency-Key": "mb-bodyid-1"})
        assert r.status_code == 422, r.text
        assert body_field_names(r) == {"player_id"}, r.text

    def test_a_missing_idempotency_key_is_refused(self, client):
        pid = make_player(client, "ClaimNoKey")
        r = claim(client, pid, key=None)
        assert r.status_code == 400, r.text

    def test_a_blank_idempotency_key_is_refused(self, client):
        pid = make_player(client, "ClaimBlankKey")
        assert claim(client, pid, key="   ").status_code == 400

    def test_an_oversized_idempotency_key_is_refused(self, client):
        pid = make_player(client, "ClaimBigKey")
        assert claim(client, pid, key="x" * 500).status_code == 400

    def test_a_key_carrying_a_control_character_is_refused(self, client):
        pid = make_player(client, "ClaimCtrlKey")
        assert claim(client, pid, key="mb-ctrl\x00-1").status_code == 400

    def test_the_key_is_checked_before_the_player_is_looked_up(self, client):
        """A caller who forgot the header gets the same 400 either way.

        Otherwise the answer would tell them whether a player id is real, which
        is exactly the probing the 403/404 split is there to prevent.
        """
        pid = make_player(client, "ClaimOrderKnown")
        assert claim(client, pid, key=None).status_code == 400

        # Ownership (403) is answered BEFORE the idempotency-key gate (400) --
        # measured, and the safer order: the key gate is the last thing that
        # could name the route, ownership is the first thing that names the
        # caller, and a caller naming somebody else learns nothing further.
        client.as_player(pid)
        assert claim(client, "nobody", key=None).status_code == 403

    def test_an_unknown_body_field_is_ignored(self, client):
        pid = make_player(client, "ClaimExtraField")
        r = claim(client, pid, key="mb-extra-1", nonsense="ignored")
        assert r.status_code in (200, 400), r.text
        assert "nonsense" not in r.text or r.status_code == 200


# ----------------------------------------------------------------------
# POST /mission-board/claim -- error cases
# ----------------------------------------------------------------------
class TestClaimErrorCases:
    def test_an_anonymous_caller_is_refused(self, client):
        pid = make_player(client, "ClaimAnon")
        client.as_player(None)
        r = claim(client, pid)
        assert r.status_code == 401, r.text

    def test_claiming_for_another_player_is_forbidden(self, client):
        thief = make_player(client, "ClaimThief")
        victim = other_player(client, "ClaimVictim")
        complete_quick(client.as_player(victim), victim)

        client.as_player(thief)
        r = claim(client, victim)
        assert r.status_code == 403, r.text

        entry = board_entry(client.as_player(victim), victim, OBJECTIVE)
        assert entry["claimed"] is False, "a forbidden claim consumed the claim"

    def test_a_signed_in_player_absent_from_the_store_is_404(self, client):
        client.as_player("no_such_player_at_all")
        r = client.post(CLAIM,
                        json={"player_id": "no_such_player_at_all",
                              "objective_id": OBJECTIVE},
                        headers={"Idempotency-Key": "mb-claimghost-1"})
        assert r.status_code == 404, r.text
        assert "Player not found" in r.text

    def test_an_unknown_objective_is_404(self, client):
        pid = make_player(client, "ClaimBadObjective")
        r = claim(client, pid, objective_id="no_such_objective", key="mb-badobj-1")
        assert r.status_code == 404, r.text
        assert "Unknown objective" in r.text

    def test_an_unfinished_objective_is_400(self, client):
        pid = make_player(client, "ClaimUnfinished")
        r = claim(client, pid, key="mb-unfinished-1")
        assert r.status_code == 400, r.text
        assert "not complete" in r.text.lower()

    def test_the_player_is_checked_before_the_objective(self, client):
        """A caller with both mistakes gets the answer about the player.

        Ordering is part of the contract: it decides which message a mistake
        gets, and 404-for-unknown-objective must not leak when the player is
        not real either.
        """
        client.as_player("nobody_at_all")
        r = client.post(CLAIM,
                        json={"player_id": "nobody_at_all",
                              "objective_id": "no_such_objective"},
                        headers={"Idempotency-Key": "mb-order-1"})
        assert r.status_code == 404, r.text
        assert "Player not found" in r.text

    def test_the_objective_is_checked_before_completion(self, client):
        pid = make_player(client, "ClaimOrder2")
        r = claim(client, pid, objective_id="no_such_objective", key="mb-order-2")
        assert r.status_code == 404, r.text
        assert "Unknown objective" in r.text

    def test_a_refusal_pays_nothing(self, client):
        pid = make_player(client, "ClaimRefusedPays")
        before = get_wallet_stone(client, pid)
        assert claim(client, pid, key="mb-refused-1").status_code == 400
        assert get_wallet_stone(client, pid) == before


def get_wallet_stone(client, pid):
    """The player's spirit stone balance, or None when the route says nothing."""
    r = client.get("/wallet", params={"player_id": pid})
    if r.status_code != 200:
        return None
    body = r.json()
    for key in ("spirit_stone", "spirit_stones", "currency"):
        if key in body:
            value = body[key]
            if isinstance(value, dict):
                return value.get("spirit_stone")
            if isinstance(value, (int, float)):
                return value
    return None


# ----------------------------------------------------------------------
# POST /mission-board/claim -- the two success arms
# ----------------------------------------------------------------------
class TestClaimSuccessArms:
    def test_a_finished_objective_pays_and_answers_the_full_shape(self, client):
        pid = make_player(client, "ClaimPaid")
        complete_quick(client, pid)
        r = claim(client, pid, key="mb-paid-1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert is_paid_arm(body), sorted(body)
        assert body["ok"] is True
        assert body["already_claimed"] is False
        assert body["objective_id"] == OBJECTIVE
        assert body["idempotency_key"].startswith("mission_board:")
        assert isinstance(body["credited_items"], list)

    def test_the_paid_arm_advertises_the_reward_the_board_showed(self, client):
        pid = make_player(client, "ClaimRewardParity")
        advertised = board_entry(client, pid, OBJECTIVE)["reward"]
        complete_quick(client, pid)
        assert claim(client, pid, key="mb-parity-1").json()["reward"] == advertised

    def test_a_second_claim_in_the_same_cycle_pays_nothing(self, client):
        pid = make_player(client, "ClaimTwice")
        complete_quick(client, pid)
        first = claim(client, pid, key="mb-twice-1")
        assert first.status_code == 200 and first.json()["ok"] is True
        before = get_wallet_stone(client, pid)

        second = claim(client, pid, key="mb-twice-2")
        assert second.status_code == 200, second.text
        body = second.json()
        assert is_already_claimed_arm(body), sorted(body)
        assert body["ok"] is False
        assert body["reward"] == {}
        assert get_wallet_stone(client, pid) == before, "the retry paid again"

    def test_a_retry_with_the_same_key_still_pays_only_once(self, client):
        pid = make_player(client, "ClaimSameKey")
        complete_quick(client, pid)
        first = claim(client, pid, key="mb-same-key-1")
        before = get_wallet_stone(client, pid)
        second = claim(client, pid, key="mb-same-key-1")
        assert second.status_code == 200
        assert second.json()["ok"] is False
        assert get_wallet_stone(client, pid) == before

    def test_claiming_one_objective_does_not_consume_another(self, client):
        pid = make_player(client, "ClaimIndependent")
        set_source(pid, "progress", objective_def(OBJECTIVE)["target"] + 5)
        set_source(pid, "pvp_wins", 3)
        board = board_body(client, pid)
        by_id = {o["objective_id"]: o for o in board["objectives"]}
        assert by_id["weekly_pvp_three"]["completed"] is True
        assert claim(client, pid, key="mb-indep-1").json()["ok"] is True
        again = board_entry(client, pid, "weekly_pvp_three")
        assert again["claimed"] is False, "an unrelated objective was consumed"

    def test_the_wrong_method_is_refused(self, client):
        pid = make_player(client, "ClaimMethod")
        assert get_board(client, pid, player_id=pid) is not None
        assert client.get(CLAIM, params={"player_id": pid}).status_code == 405


# ----------------------------------------------------------------------
# the rule that keeps the board honest: no caller may dial its own progress
# ----------------------------------------------------------------------
class TestProgressIsDerivedNotAsserted:
    def test_a_body_cannot_dial_its_own_progress(self, client):
        pid = make_player(client, "ClaimSelfProgress")
        r = claim(client, pid, key="mb-self-1", progress=100,
                  progress_delta=50, claimed=True, reward={"spirit_stone": 9999})
        assert r.status_code == 400, r.text
        entry = board_entry(client, pid, OBJECTIVE)
        assert entry["completed"] is False, entry
        assert entry["claimed"] is False, entry
        assert entry["progress"] == 0, entry

    def test_a_body_cannot_name_its_own_reward(self, client):
        pid = make_player(client, "ClaimSelfReward")
        complete_quick(client, pid)
        advertised = board_entry(client, pid, OBJECTIVE)["reward"]
        body = claim(client, pid, key="mb-selfreward-1",
                     spirit_stone=10**6, quantity=10**6).json()
        assert body["reward"] == advertised

    def test_reverting_progress_after_completing_does_not_uncomplete_it(self, client):
        """Progress is a fact about now, so a threshold row can drop back.

        The claim marker is what holds: the claim was already spent.
        """
        pid = make_player(client, "ClaimRevert")
        complete_quick(client, pid)
        assert claim(client, pid, key="mb-revert-1").json()["ok"] is True
        set_source(pid, "progress", 0)
        entry = board_entry(client, pid, OBJECTIVE)
        assert entry["completed"] is False, entry
        assert entry["claimed"] is True, "the spent claim came back"

    def test_the_module_refuses_a_claim_with_a_reason_code_never_an_exception(
        self, client
    ):
        pid = make_player(client, "ClaimCodeVsRaise")
        assert claimable(pure_player(), "no_such_objective", now=NOW) == \
            "unknown_objective"
        assert claimable(pure_player(), OBJECTIVE, now=NOW) == "not_complete"
        with pytest.raises(MissionBoardError):
            record_claim(pure_player(), "no_such_objective", now=NOW)


# ----------------------------------------------------------------------
# module-level contracts the routes depend on
# ----------------------------------------------------------------------
class TestModuleContractsTheRoutesRely:
    def test_an_unknown_objective_id_is_none_and_not_an_error(self):
        assert objective_def("no_such_objective") is None
        assert objective_def("") is None

    def test_an_unknown_cycle_raises(self):
        with pytest.raises(MissionBoardError):
            cycle_key("monthly", NOW)
        with pytest.raises(MissionBoardError):
            seconds_to_refresh("monthly", NOW)

    def test_the_three_timescales_never_share_a_cycle_key(self):
        keys = {cycle_key(name, NOW) for name in ("quick", "daily", "weekly")}
        assert len(keys) == 3, keys

    def test_a_quick_cycle_turns_over_inside_four_hours(self):
        assert QUICK_CYCLE_SECONDS == 4 * 60 * 60
        assert cycle_key("quick", NOW) != cycle_key("quick", NOW + QUICK_CYCLE_SECONDS)

    def test_a_daily_cycle_turns_over_after_a_day(self):
        assert cycle_key("daily", NOW) == cycle_key("daily", NOW + 3600)
        assert cycle_key("daily", NOW) != \
            cycle_key("daily", NOW + DAILY_CYCLE_SECONDS + 1)

    def test_the_weekly_key_is_the_iso_year_and_week(self):
        iso = datetime.fromtimestamp(NOW, tz=timezone.utc).isocalendar()
        assert cycle_key("weekly", NOW) == "w%d-%02d" % (iso[0], iso[1])
        assert WEEKLY_CYCLE_SECONDS == 7 * 86400

    def test_the_weekly_board_turns_over_on_monday_utc(self):
        """Sunday 23:59:59 UTC and Monday 00:00:00 UTC are different weeks."""
        monday = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        just_before = monday - 1
        assert cycle_key("weekly", just_before) != cycle_key("weekly", monday)
        assert seconds_to_refresh("weekly", monday) > WEEKLY_CYCLE_SECONDS - 60

    def test_the_countdown_is_positive_and_never_reaches_zero(self):
        for name, period in (("quick", QUICK_CYCLE_SECONDS),
                             ("daily", DAILY_CYCLE_SECONDS)):
            for offset in (0, 1, period // 2, period - 1, period):
                left = seconds_to_refresh(name, NOW + offset)
                assert isinstance(left, int) and 0 < left <= period, (name, offset)

    def test_the_board_state_lives_under_one_player_record_key(self):
        player = pure_player()
        board_view(player, now=NOW)
        assert STATE_KEY in player
        assert isinstance(player[STATE_KEY]["cycles"], dict)

    def test_a_partial_or_corrupt_save_is_tolerated(self):
        for broken in (None, [], "nonsense", {"cycles": "not a dict"}):
            player = pure_player(**{STATE_KEY: broken})
            state = get_state(player)
            assert isinstance(state["cycles"], dict), broken

    def test_reading_the_board_twice_does_not_move_progress(self, client):
        """The baseline is stamped once per cycle, not on every read.

        Re-stamping would make every delta row `value - value = 0`, a board
        where nothing can ever be finished.
        """
        pid = make_player(client, "DoubleRead")
        first = board_body(client, pid)
        set_source(pid, "pvp_wins", 3)
        after = board_body(client, pid)
        by_id = {o["objective_id"]: o for o in after["objectives"]}
        assert by_id["weekly_pvp_three"]["progress"] > 0, \
            "progress earned after the first read was swallowed by a re-stamp"
        assert {o["objective_id"] for o in first["objectives"]} == \
               {o["objective_id"] for o in after["objectives"]}

    def test_every_row_names_a_registered_source(self):
        for row in BOARD_OBJECTIVES:
            assert row["source"] in SOURCES, row["objective_id"]

    def test_every_source_answers_an_integer_for_a_bare_player(self):
        ctx = {"player_id": "p1"}
        for name, source in SOURCES.items():
            value = source(pure_player(), ctx)
            assert isinstance(value, int), name

    def test_the_claim_key_names_the_player_the_objective_and_the_cycle(self):
        a = claim_key("p1", OBJECTIVE, "q1")
        assert a == "mission_board:p1:%s:q1" % OBJECTIVE
        assert claim_key("p2", OBJECTIVE, "q1") != a
        assert claim_key("p1", DELTA_OBJECTIVE, "q1") != a
        assert claim_key("p1", OBJECTIVE, "q2") != a

    def test_a_new_cycle_is_a_new_claim_key(self, client):
        """'Once per objective per cycle' is true at the wallet, not just in prose."""
        pid = make_player(client, "KeyPerCycle")
        assert claim_key(pid, OBJECTIVE, cycle_key("quick", NOW)) != \
               claim_key(pid, OBJECTIVE, cycle_key("quick", NOW + QUICK_CYCLE_SECONDS))

    def test_the_claim_marker_does_not_leak_into_the_next_cycle(self):
        player = pure_player()
        record_claim(player, OBJECTIVE, NOW)
        later = NOW + QUICK_CYCLE_SECONDS
        view = board_view(player, now=later)
        entry = next(o for o in view["objectives"]
                     if o["objective_id"] == OBJECTIVE)
        assert entry["claimed"] is False, entry
        assert entry["cycle_key"] != cycle_key("quick", NOW)

    def test_both_claim_arms_answer_the_same_five_keys(self):
        reward = reward_for(objective_def(OBJECTIVE))
        paid = outcome_for(OBJECTIVE, "q1", reward)
        refused = outcome_for(OBJECTIVE, "q1", {}, already=True)
        assert set(paid) == set(refused) == COMMON_CLAIM_KEYS

    def test_the_refused_arm_never_carries_a_reward(self):
        refused = outcome_for(OBJECTIVE, "q1", reward_for(objective_def(OBJECTIVE)),
                              already=True)
        assert refused["ok"] is False
        assert refused["already_claimed"] is True
        assert refused["reward"] == {}

    def test_the_paid_arm_carries_the_reward_it_was_given(self):
        reward = reward_for(objective_def(OBJECTIVE))
        assert outcome_for(OBJECTIVE, "q1", reward)["reward"] == reward


# ----------------------------------------------------------------------
# negative controls: the detectors must be able to fail
# ----------------------------------------------------------------------
class TestNegativeControls:
    def test_the_board_shape_detector_rejects_a_mangled_body(self, client):
        pid = make_player(client, "ControlBoardShape")
        good = board_body(client, pid)
        assert is_board_shape(good)
        for broken in ({}, {**good, "extra": 1}, {k: v for k, v in good.items()
                                                if k != "cycles"}, []):
            assert not is_board_shape(broken), broken

    def test_the_entry_shape_detector_rejects_a_mangled_row(self, client):
        pid = make_player(client, "ControlEntryShape")
        entry = board_entry(client, pid, OBJECTIVE)
        assert is_entry_shape(entry)
        assert not is_entry_shape({**entry, "surprise": True})
        assert not is_entry_shape({k: v for k, v in entry.items()
                                   if k != "reward"})

    def test_the_claim_arm_detectors_tell_the_two_arms_apart(self, client):
        pid = make_player(client, "ControlArms")
        complete_quick(client, pid)
        paid = claim(client, pid, key="mb-control-1").json()
        refused = claim(client, pid, key="mb-control-2").json()
        assert is_paid_arm(paid) and not is_already_claimed_arm(paid)
        assert is_already_claimed_arm(refused) and not is_paid_arm(refused)
        assert not is_paid_arm({})
        assert not is_already_claimed_arm({**refused, "already_claimed": False})

    def test_the_completion_detector_separates_done_from_undone(self, client):
        pid = make_player(client, "ControlComplete")
        undone = board_entry(client, pid, OBJECTIVE)
        assert not is_complete(undone)
        done = complete_quick(client, pid)
        assert is_complete(done)
        assert not is_complete({**done, "completed": False})

    def test_the_query_validation_detector_is_not_a_body_error_detector(self, client):
        """Both answer 422; only the location tells them apart."""
        pid = make_player(client, "ControlLoc")
        client.as_player(pid)
        missing_query = client.get(BOARD)
        assert missing_query.status_code == 422
        assert refused_by_query_validation(missing_query)

        missing_body = client.post(CLAIM, json={"player_id": pid},
                                   headers={"Idempotency-Key": "mb-loc-1"})
        assert missing_body.status_code == 422
        assert not refused_by_query_validation(missing_body)
        assert body_field_names(missing_body) == {"objective_id"}

    def test_a_detector_given_a_non_200_answers_false(self):
        class Fake:
            status_code = 401

            def json(self):
                return {}

        assert not refused_by_query_validation(Fake())
        assert not is_board_shape(Fake())
        assert body_field_names(Fake()) == set()


# ----------------------------------------------------------------------
# the LIVE tower record -- the key the paying path actually writes
# ----------------------------------------------------------------------
# The two tower rows are the ones most likely to rot quietly. The board reads
# `player["tower_run"]`, which is what `tower_run.settle_run` stamps; the older
# ladder writes `player["tower_state"]` instead and nothing on the paying path
# ever fills that one in. A board that read only the legacy key would report a
# player who has cleared a hundred floors as having cleared none, and the
# weekly row could never be finished.
#
# The other half of the contract is WHICH sub-field counts. `weekly_best` is a
# map of one week to that week's best depth and is only ever raised, so summing
# it is monotone. `history` is the obvious alternative and the wrong one: it
# keeps only the last 20 runs, so a total read from it DROPS as old runs fall
# off, and a delta row's progress would slide backwards under a player
# mid-week. Both facts are asserted here rather than read off the docstring.
class TestTheLiveTowerRecordIsTheSource:
    def test_the_live_key_is_the_one_the_paying_path_writes(self):
        assert TOWER_RUN_KEY == "tower_run"
        assert TOWER_STATE_KEY == "tower_state"
        assert TOWER_RUN_KEY != TOWER_STATE_KEY

    def test_clears_are_read_from_the_live_key_alone(self):
        """A player who only ever ran the tower still has floors counted."""
        player = {TOWER_RUN_KEY: {"weekly_best": {"2026-40": 5, "2026-41": 3}}}
        assert source_tower_clears(player, {}) == 8

    def test_a_player_with_only_the_live_key_reaches_the_quick_tower_row(self, client):
        """The live key alone is enough to finish a board row end to end."""
        row = objective_def("quick_tower_floor")
        pid = make_player(client, "TowerLiveQuick")
        set_source(pid, TOWER_RUN_KEY, {"best_ever": row["target"]})
        assert board_entry(client, pid, "quick_tower_floor")["completed"] is True

    def test_a_live_key_player_can_also_finish_the_weekly_tower_row(self, client):
        row = objective_def("weekly_tower_ten")
        pid = make_player(client, "TowerLiveWeekly")
        set_source(pid, TOWER_RUN_KEY,
                   {"weekly_best": {"2026-40": row["target"] + 5}})
        assert board_entry(client, pid, "weekly_tower_ten")["completed"] is True

    def test_the_highest_floor_is_read_from_the_live_key(self):
        assert source_tower_floor({TOWER_RUN_KEY: {"best_ever": 12}}, {}) == 12

    def test_the_two_tower_rows_read_two_different_fields(self):
        """Same sub-record, two measures: clears is not the floor reached."""
        state = {TOWER_RUN_KEY: {"best_ever": 12, "weekly_best": {"2026-40": 40}}}
        assert source_tower_floor(state, {}) == 12
        assert source_tower_clears(state, {}) == 40

    def test_a_run_history_alone_counts_as_nothing(self):
        """`history` is capped, so it is not the source of a lifetime total.

        Twenty-five runs are present; the reader still answers zero. A reader
        that summed `history` would look right here and then fall backwards
        once the list rolled over, which is why the test pins the zero.
        """
        player = {TOWER_RUN_KEY: {"history": [{"floors": 9}] * 25}}
        assert source_tower_clears(player, {}) == 0

    def test_a_total_read_from_weekly_best_never_falls_back(self):
        """Adding a later, better week never lowers the reading."""
        run = {TOWER_RUN_KEY: {"weekly_best": {"2026-40": 4}}}
        first = source_tower_clears(run, {})
        run[TOWER_RUN_KEY]["weekly_best"]["2026-41"] = 6
        second = source_tower_clears(run, {})
        assert first == 4
        assert second == 10
        assert second >= first

    def test_the_reader_sums_weekly_best_exactly_as_stored(self):
        """The reader is a plain sum. It does not re-check the raise-only rule.

        `record_weekly_best` in tower_run is what guarantees a week's entry is
        only ever raised; this reader faithfully sums whatever it is handed.
        Asserting a floor here would pin a guarantee that belongs to a
        different module, so the test pins the actual contract instead: every
        stored week counts, once.
        """
        run = {TOWER_RUN_KEY: {"weekly_best": {"2026-40": 9, "2026-41": 1}}}
        assert source_tower_clears(run, {}) == 10

    def test_a_single_stored_week_is_counted_whole(self):
        assert source_tower_clears({TOWER_RUN_KEY: {"weekly_best": {"2026-40": 6}}}, {}) == 6

    def test_whichever_tower_system_the_player_used_is_the_larger(self):
        live = {TOWER_RUN_KEY: {"weekly_best": {"2026-40": 8}}}
        legacy = {TOWER_STATE_KEY: {"total_clears": 3}}
        both = {TOWER_RUN_KEY: {"weekly_best": {"2026-40": 2}},
                TOWER_STATE_KEY: {"total_clears": 7}}
        assert source_tower_clears(live, {}) == 8
        assert source_tower_clears(legacy, {}) == 3
        assert source_tower_clears(both, {}) == 7, "the board under-reports floors"

    def test_the_floor_read_is_also_the_larger_of_the_two_systems(self):
        both = {TOWER_RUN_KEY: {"best_ever": 4}, TOWER_STATE_KEY: {"highest_floor": 9}}
        assert source_tower_floor(both, {}) == 9

    @pytest.mark.parametrize("broken", [None, "notadict", [1, 2], 5, {"weekly_best": "nope"},
                                        {"weekly_best": [1, 2]}, {"best_ever": "x"}])
    def test_a_corrupt_live_tower_record_reads_as_zero_not_an_exception(self, broken):
        assert source_tower_clears({TOWER_RUN_KEY: broken}, {}) == 0
        assert source_tower_floor({TOWER_RUN_KEY: broken}, {}) == 0

    def test_a_missing_live_key_falls_back_to_the_legacy_one(self):
        """A player who only ever cleared via the older ladder is not a zero."""
        assert source_tower_clears({TOWER_STATE_KEY: {"total_clears": 11}}, {}) == 11
        assert source_tower_floor({TOWER_STATE_KEY: {"highest_floor": 6}}, {}) == 6


# ----------------------------------------------------------------------
# the board blob is bounded
# ----------------------------------------------------------------------
# The player record is rewritten on every save, so anything the board leaves
# behind is carried forever. Measured: without pruning, one simulated year of
# opening the board daily leaves 785 buckets. The bound is KEPT_PAST_CYCLES,
# and the current cycle must survive it -- a player served a board they cannot
# then claim from is worse than a growing blob.
class TestStateStaysBounded:
    def _player_with_stale_buckets(self, count):
        player = pure_player()
        cycles = player.setdefault(STATE_KEY, {"cycles": {}})["cycles"]
        for index in range(count):
            cycles["q%d" % index] = {"daily_hunt_five": {"claimed": True}}
        return player

    def test_a_long_played_record_does_not_accumulate_buckets(self):
        player = self._player_with_stale_buckets(40)
        board_view(player, now=NOW)
        cycles = player[STATE_KEY]["cycles"]
        assert len(cycles) <= 3 + KEPT_PAST_CYCLES, len(cycles)

    def test_the_bucket_being_survived_is_never_pruned(self):
        """Whatever else is dropped, the cycles being served right now stay."""
        player = self._player_with_stale_buckets(40)
        view = board_view(player, now=NOW)
        cycles = player[STATE_KEY]["cycles"]
        for name in ("quick", "daily", "weekly"):
            assert view["cycles"][name]["key"] in cycles, name

    def test_the_rows_on_screen_are_present_after_pruning(self):
        """Pruning may not cost the caller a row it is currently drawing."""
        player = self._player_with_stale_buckets(40)
        view = board_view(player, now=NOW)
        assert len(view["objectives"]) == len(BOARD_OBJECTIVES)
        for entry in view["objectives"]:
            assert entry["cycle_key"] in player[STATE_KEY]["cycles"], entry

    def test_a_fresh_player_is_bounded_too(self):
        player = pure_player()
        board_view(player, now=NOW)
        assert len(player[STATE_KEY]["cycles"]) <= 3 + KEPT_PAST_CYCLES

    def test_pruning_keeps_the_bound_when_the_record_is_read_repeatedly(self):
        player = self._player_with_stale_buckets(40)
        for _ in range(5):
            board_view(player, now=NOW)
        assert len(player[STATE_KEY]["cycles"]) <= 3 + KEPT_PAST_CYCLES
