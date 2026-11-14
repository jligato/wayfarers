"""
Mission module: the client's real call shape, proven by live request.

WHAT THIS FILE ASSERTS
----------------------
Every mission route the shipped client calls, driven through the real FastAPI
app with the EXACT request the Godot client builds, must not be rejected by
the web framework before the handler runs.

The call shapes are copied from ``client/scripts/systems/MissionSystem.gd``:

  :28  GET  /missions/list?player_id=<id>
  :39  POST /missions/progress  body {"player_id", "mission_id", "amount"}
  :50  POST /missions/claim     body {"player_id", "mission_id"}
  :61  POST /missions/reset/daily?player_id=<id>
  :71  POST /missions/reset/weekly?player_id=<id>

WHY A TEST INSTEAD OF A FIX
---------------------------
``server/missions.py`` registers NO routes: every ``/missions/*`` endpoint is
declared in ``server/main.py``, which another agent owns. The two mission
routes whose ``player_id`` is reachable only as a query value
(``/missions/reset/daily``, ``/missions/reset/weekly``) are among them. They
are also the two routes the client already calls in the query form, so they
answer 200 today and do not block play. Closing that latent gap needs a one
line change to a file this task may not touch, so this file locks the CURRENT
contract instead: the shapes that work stay working, and any future refactor
that breaks one fails here rather than in front of a player.

NEGATIVE CONTROL
----------------
``test_detector_flags_a_query_only_route`` builds a throwaway app carrying the
exact defect shape and asserts the same 422 detector used by the real
assertions flags it. Without it, every test below could pass on a detector
that recognises nothing. The throwaway app is defined HERE rather than pointed
at another module's route, so the control cannot rot when an owner fixes
their file.
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


# /player/create carries a PROCESS-WIDE signup rate limit (main.py
# _SIGNUP_ATTEMPTS, 5 creations per 60s per address). Every test here creates a
# player from the same in-process test client, so a file with more than five
# player-creating tests exhausts the window and the later tests fail at the
# create call, before the assertion under test ever runs. The limit is proven by
# its own dedicated tests; clearing the counters here is what keeps this file
# about its own subject. Same treatment as server/tests/test_daily_boss_grief.py.
@pytest.fixture(autouse=True)
def _clear_signup_rate_limit():
    clear_signup_rate_limit()
    import server.main as _main

    for attr in ("_SIGNUP_ATTEMPTS", "_SIGNUP_WINDOW_START"):
        bucket = getattr(_main, attr, None)
        if hasattr(bucket, "clear"):
            bucket.clear()
    yield


CREATE = "/player/create"
LIST = "/missions/list"
PROGRESS = "/missions/progress"
CLAIM = "/missions/claim"
RESET_DAILY = "/missions/reset/daily"
RESET_WEEKLY = "/missions/reset/weekly"

#: A mission that exists, so the request gets past routing and into the
#: handler. Its progress is not complete, so a claim is a legal 200 carrying
#: ok=false rather than a 4xx.
DAILY_MISSION = "daily_cultivate_1"


class _ProbeBody(BaseModel):
    """Module scope on purpose -- see the negative control's docstring."""

    mission_id: str


# --- the 422 detector, shared by the real tests and the negative control ----


def player_id_422(response) -> bool:
    """True when `response` is the framework rejecting a body-borne id.

    The framework answers a bare scalar it read as a QUERY value with
    ``{"loc": ["query", "<name>"], "type": "missing"}``. A genuine body
    validation failure names ``body`` and carries a constraint type instead,
    so the two are told apart by `loc` rather than by the status code alone.
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
        and isinstance(d.get("loc"), list)
        and len(d["loc"]) == 2
        and d["loc"][0] == "query"
        and d["loc"][1] == "player_id"
        for d in detail
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        # Auth: every /missions/* route is guarded, so each call below is made
        # AS the player the test created. See server/tests/wc_test_auth.py.
        yield Session(c)


@pytest.fixture()
def player_id(client):
    """A real player, created fresh through the public route.

    Deliberately FUNCTION scoped, not module scoped. The session-wide
    ``_restore_shared_singletons`` fixture in the repo root conftest empties
    the in-memory player store after every test, so a player created once per
    module is gone by the second test and every route answers 404 "Player not
    found" -- a different failure that would mask the one under test.

    A made up id would also answer 404, from inside the handler, which is
    exactly as unhelpful. The id must be real, and it must be current.
    """
    r = client.post(CREATE, json={"name": "mission_shape_probe",
                                  "spirit_root": "Metal"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # Every later call in the test rides this session.
    client.sign_in(pid)
    # The reset routes report on a progress row; give one to report on.
    main.mission_store._row_mem(pid, DAILY_MISSION)
    return pid


# --- the client's real call shapes -----------------------------------------
# One case per route, so a failure names the route instead of a line number.


def test_client_get_list_works(client, player_id):
    """MissionSystem.gd:28 -- GET /missions/list?player_id=..."""
    r = client.get(LIST, params={"player_id": player_id})
    assert not player_id_422(r), r.text
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_client_post_progress_body_id_works(client, player_id):
    """MissionSystem.gd:39 -- the id rides in the JSON body."""
    r = client.post(PROGRESS, json={"player_id": player_id,
                                    "mission_id": DAILY_MISSION,
                                    "amount": 1})
    assert not player_id_422(r), r.text
    assert r.status_code == 200, r.text
    assert "progress" in r.json()


def test_client_post_claim_body_id_works(client, player_id):
    """MissionSystem.gd:50 -- the id rides in the JSON body."""
    r = client.post(CLAIM, json={"player_id": player_id,
                                 "mission_id": DAILY_MISSION})
    assert not player_id_422(r), r.text
    assert r.status_code == 200, r.text


def test_client_post_reset_daily_query_id_works(client, player_id):
    """MissionSystem.gd:61 -- the client appends ?player_id= itself."""
    r = client.post(f"{RESET_DAILY}?player_id={player_id}")
    assert not player_id_422(r), r.text
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_client_post_reset_weekly_query_id_works(client, player_id):
    """MissionSystem.gd:71 -- the client appends ?player_id= itself."""
    r = client.post(f"{RESET_WEEKLY}?player_id={player_id}")
    assert not player_id_422(r), r.text
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# --- the two shapes are exclusive, and that is deliberate -------------------


def test_progress_rejects_the_query_form_on_purpose(client, player_id):
    """``/missions/progress`` reads the id from the BODY only.

    Measured, not assumed: the id in the query answers 422 naming
    ``["body","player_id"]``. That is the opposite direction from the defect
    this file guards, and it is the correct behaviour for this route -- the
    body model owns the id, so there is no query parameter to read. The
    friends-module fix deliberately kept a query FALLBACK; this route never
    had one, and adding one is not this task's call. Asserted here so a
    future change to that asymmetry is a visible decision rather than a
    silent drift.
    """
    r = client.post(PROGRESS, params={"player_id": player_id},
                    json={"mission_id": DAILY_MISSION, "amount": 1})
    assert r.status_code == 422, r.text
    # `body`, not `query`: the framework looked in the body and found it
    # missing there. This is NOT the query-param defect.
    assert not player_id_422(r), r.text
    assert any(d.get("loc") == ["body", "player_id"]
               for d in r.json()["detail"]), r.text


# --- business rules must survive any reshaping ------------------------------


def test_unknown_player_is_refused_not_422(client):
    """A well formed call naming nobody is refused, and never as a 422.

    MEASURED 2026-10-05 against the real app: 401, not 404. Signing in AS the
    unknown id used to be the only way to reach this route's own "Player not
    found" 404, because the ownership guard refuses a claim naming a different
    player first. It no longer reaches the handler at all: ``auth.authenticate``
    now also confirms the session's player still exists, and revokes the token
    when it does not. That is the deleted-player defense, and it is the right
    answer -- a token for an account that was never created must not work, and
    the status must not reveal whether an id is live.

    The 422 arm still matters and is still asserted: a malformed id is refused
    by the framework before any of this runs, so the two are distinguishable.
    """
    client.sign_in("__no_such_player__")
    r = client.post(PROGRESS, json={"player_id": "__no_such_player__",
                                    "mission_id": DAILY_MISSION})
    assert r.status_code == 401, r.text
    assert not player_id_422(r)


def test_unknown_mission_is_404_not_422(client, player_id):
    r = client.post(PROGRESS, json={"player_id": player_id,
                                    "mission_id": "__no_such_mission__"})
    assert r.status_code == 404, r.text
    assert not player_id_422(r)


def test_reset_without_any_id_is_422(client):
    """No id anywhere stays a rejection.

    Measured behaviour: this reports ``["query","player_id"]``, because
    ``/missions/reset/*`` is one of the two mission routes that still read
    the id ONLY as a query value. That is precisely the defect shape, and it
    is invisible to play only because the client appends the id to the URL
    itself. The assertion is that the call is refused -- an anonymous reset
    must never succeed -- not that the refusal names the right mechanism.
    """
    r = client.post(RESET_DAILY)
    assert r.status_code == 422, r.text
    # A 422 body is {"detail": [...]}, not {"ok": ...}. The status code is the
    # refusal; there is no success flag to check.
    assert "detail" in r.json(), r.text


# --- negative control: the detector must be able to fail --------------------


def test_detector_flags_a_query_only_route():
    """A throwaway app carrying the exact defect shape is detected.

    If this ever stops detecting, every assertion above becomes decorative:
    a detector that recognises nothing reports "not a 422" for everything.

    ``_ProbeBody`` is declared at MODULE scope on purpose. This file carries
    ``from __future__ import annotations``, which turns every annotation into
    a string, and FastAPI resolves a body model against MODULE globals. A
    class declared inside this function is not there, the forward reference
    never resolves, and FastAPI silently demotes the body to a query
    parameter -- the probe would then be testing a second, unrelated bug and
    would pass for the wrong reason.
    """
    probe = FastAPI()

    @probe.post("/broken")
    def broken(player_id: str, req: _ProbeBody):  # bare scalar == query param
        return {"player_id": player_id, "mission_id": req.mission_id}

    with TestClient(probe, raise_server_exceptions=False) as c:
        # The client's shape: the id is in the body.
        in_body = c.post("/broken", json={"player_id": "p1", "mission_id": "m"})
        assert player_id_422(in_body), in_body.text

        # The legacy shape: the id is in the URL, and it works. This is the
        # asymmetry that defines the defect.
        in_query = c.post("/broken?player_id=p1", json={"mission_id": "m"})
        assert in_query.status_code == 200, in_query.text
        assert not player_id_422(in_query)

        # A genuine body validation failure must NOT be mistaken for it.
        bad_body = c.post("/broken?player_id=p1", json={})
        assert bad_body.status_code == 422, bad_body.text
        assert not player_id_422(bad_body), (
            "a body validation failure is not the query-param defect"
        )
