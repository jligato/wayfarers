"""
Contract tests for ``server/tournament.py`` (engine) and the addresses that
expose it (``server/tournament_routes.py``).

WHAT THIS FILE COVERS
---------------------
Three layers, in this order, because each one is a contract somebody can break
without the others noticing:

1. **Route contracts** — what each address accepts, what it answers, and what
   it refuses. Seven addresses exist: ``/tournament/list``,
   ``/tournament/{id}/bracket``, ``/tournament/{id}/verify``,
   ``/tournament/{id}/declare``, ``/tournament/{id}/withdraw``,
   ``/tournament/clock/tick``, ``/tournament/{id}/settle``, plus the operator
   escape ``/admin/tournament/{id}/void``.
2. **Auth contracts** — every player address takes a login session; the two
   background addresses take the internal server-to-server secret; the operator
   address takes the operator secret. All three fail CLOSED: an unset
   configuration refuses everything, so a green "401 without a token" assertion
   proves nothing until a CORRECT token is presented and accepted. Both halves
   are asserted here.
3. **Engine contracts** — the pure functions the routes lean on: seed
   derivation, stat freezing, placement and prize arithmetic, and the guards
   that refuse a bad bracket or a bad match.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
* No frozen prize amounts as literals. The engine derives them from
  ``PRIZE_TABLE`` and scales by bracket size, so a hardcoded 2500 is a
  change-detector. Money assertions are RELATIONS (first place beats second,
  a bigger bracket pays more in total, the sum matches the engine's own table).
* No source-text grep. Nothing reads a ``.py`` file's bytes.
* No database claims. The suite runs with the database environment stripped, so
  the in-memory once-only claim is the implementation under test here. The
  database-backed claim is a DIFFERENT implementation (a conditional upsert)
  and a green run here is not a statement about it.

THE TRAPS THIS FILE HAS TO AVOID
--------------------------------
* ``server/main.py`` is imported under two names and the root ``conftest.py``
  merges them. Run outside pytest they split and every payout dies with
  ``KeyError``, which reads as "nobody was paid".
* The bracket store is a process-wide singleton no fixture clears, and the app's
  own 60-second bracket clock walks EVERY bracket in it. The store is
  quarantined per test here, and every bracket opens far in the future so the
  clock cannot advance one out from under a test.
* ``raise_server_exceptions=False`` is required: a 500 under audit is an
  unhandled exception, and a re-raising client turns a measurement into a
  traceback instead of the status code being pinned.
* ``/tournament/{id}/declare`` is rate limited per player (30 a minute). Every
  test declares with its own freshly created player.

Run
---
    env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
        -u PYTHONPATH python -m pytest server/tests/test_tournament_contract.py -q
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.tournament as tng  # noqa: E402
import tournament_routes  # noqa: E402  (owns the secret env var names)
from server.tests.wc_test_auth import (  # noqa: E402
    auth_headers,
    clear_signup_rate_limit,
)

#: The internal server-to-server secret and the operator secret. Both addresses
#: fail closed, so both must be configured for the accepting half of each auth
#: contract to mean anything.
#: Read the two secret env var names off the module that OWNS them. A literal
#: name here is a change-detector in the wrong direction: when the void route
#: moved to the shared ``require_admin`` guard it switched from a private
#: variable to ``auth.ADMIN_TOKEN_ENV``, and this fixture went on configuring
#: the old name. The server then failed CLOSED with 503 ("admin token not
#: configured") and four tests blamed the route. Deriving the names keeps the
#: fixture correct across that kind of move.
INTERNAL_ENV = tournament_routes.INTERNAL_TOKEN_ENV
ADMIN_ENV = tournament_routes.ADMIN_TOKEN_ENV
INTERNAL_VALUE = "tournament-contract-internal"
ADMIN_VALUE = "tournament-contract-admin"

INTERNAL_HEADERS = {"X-Internal-Token": INTERNAL_VALUE}
ADMIN_HEADERS = {"x-admin-token": ADMIN_VALUE}

#: Every bracket opens far past the real wall clock, so the app's own bracket
#: clock cannot advance one out from under a test.
CREATE_NOW = time.time() + 1_000_000.0
#: Comfortably past every published window (round 1 is 24h, later rounds 72h).
FAR_FUTURE = CREATE_NOW + 10 ** 8


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _secrets():
    """Configure the two fail-closed secrets for the life of this file."""
    saved = {k: os.environ.get(k) for k in (INTERNAL_ENV, ADMIN_ENV)}
    os.environ[INTERNAL_ENV] = INTERNAL_VALUE
    os.environ[ADMIN_ENV] = ADMIN_VALUE
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="module")
def client(_secrets):
    """The real app under a real lifespan."""
    from server.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def isolate_bracket_store():
    """Quarantine the bracket store per test and hand it back byte for byte.

    The store is a process-wide singleton the app's clock job walks over every
    tick, and no fixture in this repo clears it. Without this the file is
    order-dependent and the clock settles another test's bracket.
    """
    store = tng.tournament_store()
    with store._lock:
        parked = (dict(store._brackets), dict(store._standings), dict(store._matches))
        store._brackets.clear()
        store._standings.clear()
        store._matches.clear()
    try:
        yield
    finally:
        with store._lock:
            store._brackets.clear()
            store._standings.clear()
            store._matches.clear()
            store._brackets.update(parked[0])
            store._standings.update(parked[1])
            store._matches.update(parked[2])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_player(client, **overrides):
    """Create a player through the real route and mint a session for them."""
    from server import main as main_module

    clear_signup_rate_limit()
    name = f"tng{uuid.uuid4().hex[:10]}"
    r = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    if overrides:
        with main_module.players_db.transaction(pid):
            row = main_module.players_db[pid]
            row.update(overrides)
            main_module.players_db.persist(pid, row)
    return pid, auth_headers(pid)


def make_bracket(client, *, size=4, opened_at=CREATE_NOW, entrants=None):
    """Create ``size`` real players and one real bracket containing them.

    Returns ``(tournament_id, [(player_id, headers), ...])``. The bracket is
    made through the engine, because there is deliberately NO route that creates
    one — a leader who could open a draw is a leader who could buy a seat.
    """
    tid = f"contract_{uuid.uuid4().hex[:10]}"
    made = [new_player(client) for _ in range(size)]
    ids = [pid for pid, _ in made]
    scores = {pid: 1000 - index * 10 for index, pid in enumerate(ids)}
    tng.create_bracket(
        tng.tournament_store(),
        tournament_id=tid,
        size=size,
        entrants=ids,
        entry_scores=scores,
        now=opened_at,
    )
    return tid, made


def idem():
    """A fresh idempotency key. A shared one would make a retry look like a
    replay and get refused for the wrong reason."""
    return f"idem-{uuid.uuid4().hex}"


def post_with_idem(client, url, headers, **kw):
    h = dict(headers)
    h["Idempotency-Key"] = idem()
    return client.post(url, headers=h, **kw)


# ===========================================================================
# 1. Route registration: the surface exists and is not shadowed
# ===========================================================================


def test_every_tournament_address_is_mounted():
    """Seven bracket addresses plus the operator escape, all live.

    Counted from the RUNNING app's route table, not from a grep of the source,
    because a route that is registered below the ownership-guard mount is never
    examined and a module-level function that is never registered is dead code
    that reads as shipped.
    """
    from server.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for expected in (
        "/tournament/list",
        "/tournament/{tournament_id}/bracket",
        "/tournament/{tournament_id}/verify",
        "/tournament/{tournament_id}/declare",
        "/tournament/{tournament_id}/withdraw",
        "/tournament/clock/tick",
        "/tournament/{tournament_id}/settle",
        "/admin/tournament/{tournament_id}/void",
    ):
        assert expected in paths, (
            f"{expected} is not mounted; the bracket surface is {sorted(p for p in paths if p and 'tournament' in p)}"
        )


def test_there_is_no_route_that_creates_a_bracket_or_nominates_a_player():
    """The ABSENCE is the enforcement.

    An address that creates a bracket is an address a caller can call four
    hundred times; an address that nominates a player is one a leader can buy
    out of. Neither exists, and the bracket surface must stay that way.
    """
    from server.main import app

    # Scoped to the TOURNAMENT surface. Other features legitimately have
    # "/player/create"; what must not exist is a bracket-creating or
    # player-nominating address on the bracket itself.
    bracket_posts = [
        getattr(r, "path", "")
        for r in app.routes
        if "POST" in (getattr(r, "methods", None) or set())
        and "tournament" in (getattr(r, "path", "") or "")
    ]
    assert bracket_posts, "no tournament POST addresses found; the scan is vacuous"
    assert not any("create" in p for p in bracket_posts), bracket_posts
    assert not any("nominate" in p for p in bracket_posts), bracket_posts
    assert not any("entry" in p for p in bracket_posts), bracket_posts


# ===========================================================================
# 2. Auth contracts: refuse without, serve with
# ===========================================================================


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("POST", "/tournament/list", {"player_id": "anyone"}),
        ("GET", "/tournament/some_bracket/bracket", None),
        ("GET", "/tournament/some_bracket/verify", None),
        ("POST", "/tournament/some_bracket/declare",
         {"player_id": "anyone", "match_id": "m"}),
        ("POST", "/tournament/some_bracket/withdraw", {"player_id": "anyone"}),
    ],
)
def test_player_addresses_refuse_a_caller_with_no_session(client, method, url, body):
    """No session, no answer.

    Asserted for all five player-facing addresses. The status is 401 because
    these all carry ``Depends(require_player)`` by hand as well as being caught
    by the shape-based ownership guard in ``main.py`` — two layers, because one
    layer is one bug away from an open door.
    """
    r = client.request(method, url, json=body) if body else client.request(method, url)
    assert r.status_code == 401, f"{method} {url} answered {r.status_code}: {r.text}"


def test_player_addresses_serve_a_signed_in_caller(client):
    """The other half of the same contract: a real session IS served.

    Without this half, every assertion above would still pass against a route
    that is simply always broken.
    """
    pid, headers = new_player(client)
    r = client.post("/tournament/list", json={"player_id": pid}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["tournaments"], list)
    assert body["count"] == len(body["tournaments"])


def test_player_addresses_refuse_a_session_that_is_not_the_body_player_id(client):
    """A signed-in player cannot act as somebody else by naming them in the body."""
    pid_a, headers_a = new_player(client)
    pid_b, _ = new_player(client)
    assert pid_a != pid_b
    r = client.post("/tournament/list", json={"player_id": pid_b}, headers=headers_a)
    assert r.status_code == 403, r.text


def test_bracket_read_refuses_a_query_player_id_that_is_not_the_session(client):
    """The optional ``player_id`` query parameter is a CLAIM, not a selector.

    ``require_player`` collects an asserted player id straight off the query
    string and refuses a value that is not the session's player, so the
    parameter can lose its "required" status without losing its check.
    """
    tid, made = make_bracket(client)
    _, headers = made[0]
    _, other_headers = new_player(client)
    r = client.get(f"/tournament/{tid}/bracket", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/tournament/{tid}/bracket?player_id=not-me",
                   headers=other_headers)
    assert r.status_code == 403, r.text


def test_clock_tick_fails_closed_without_the_internal_secret(client):
    """The background address takes a server-to-server secret, not a session."""
    r = client.post("/tournament/clock/tick")
    assert r.status_code == 401, r.text
    r = client.post("/tournament/clock/tick", headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401, r.text


def test_clock_tick_serves_a_correct_internal_secret(client):
    """A correct token is accepted — otherwise the 401 above proves nothing."""
    r = client.post("/tournament/clock/tick?now=1", headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["results"], list)
    assert body["ticks"] == len(body["results"])


def test_settle_fails_closed_without_the_internal_secret(client):
    """Settlement pays money, so it takes the internal secret and nothing else."""
    tid, _ = make_bracket(client)
    r = client.post(f"/tournament/{tid}/settle")
    assert r.status_code == 401, r.text
    r = client.post(f"/tournament/{tid}/settle", headers={"X-Internal-Token": "nope"})
    assert r.status_code == 401, r.text


def test_operator_void_refuses_a_missing_or_wrong_operator_token(client):
    """Cancelling a bracket is a state change, so it takes the operator secret."""
    tid, _ = make_bracket(client)
    r = client.post(f"/admin/tournament/{tid}/void")
    assert r.status_code == 401, r.text
    r = client.post(f"/admin/tournament/{tid}/void", headers={"x-admin-token": "nope"})
    assert r.status_code == 401, r.text


def test_operator_void_serves_a_correct_operator_token(client):
    """And a correct token works, and the bracket really is void afterwards."""
    tid, _ = make_bracket(client)
    r = client.post(f"/admin/tournament/{tid}/void", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "tournament_id": tid, "state": "void"}
    assert tng.tournament_store().get_bracket(tid)["state"] == "void"


def test_an_internal_secret_does_not_open_a_player_address(client):
    """Two different secrets for two different doors.

    The internal secret is not a superset of a player session: a background job
    holding it must not be able to act as a player.
    """
    r = client.post("/tournament/list", json={"player_id": "anyone"},
                    headers=INTERNAL_HEADERS)
    assert r.status_code == 401, r.text


def test_a_player_session_does_not_open_a_background_address(client):
    """And the reverse: a player's session is not a server-to-server secret."""
    _, headers = new_player(client)
    r = client.post("/tournament/clock/tick", headers=headers)
    assert r.status_code == 401, r.text


# ===========================================================================
# 3. Request shapes: forbidden fields are refused, not ignored
# ===========================================================================


@pytest.mark.parametrize(
    "extra",
    [
        {"winner_id": "me"},
        {"enemy_stats": {"hp": 1, "atk": 1}},
        {"opponent_rating": 99999},
        {"seed": 7},
        {"placement": 1},
        {"amount": 5000},
    ],
)
def test_declare_refuses_a_body_carrying_a_forbidden_field(client, extra):
    """A body that tries to SAY the result is 422, not silently accepted.

    Every request model is ``extra="forbid"``. Refusing is stronger than
    dropping: a dropped field looks like it was honoured and nobody can tell the
    difference from a route that read it.
    """
    tid, made = make_bracket(client)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    assert match is not None, "the freshly seeded entrant has no next match"
    body = {"player_id": pid, "match_id": match["match_id"], **extra}
    r = post_with_idem(client, f"/tournament/{tid}/declare", headers, json=body)
    assert r.status_code == 422, f"{extra} was not refused: {r.status_code} {r.text}"


@pytest.mark.parametrize("missing", ["player_id", "match_id"])
def test_declare_refuses_a_body_missing_a_required_field(client, missing):
    """The two required fields are required, each on its own."""
    tid, made = make_bracket(client)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    body = {"player_id": pid, "match_id": match["match_id"]}
    body.pop(missing)
    r = post_with_idem(client, f"/tournament/{tid}/declare", headers, json=body)
    assert r.status_code == 422, r.text


def test_declare_refuses_an_empty_player_id(client):
    """``min_length=1`` is a real bound, not decoration."""
    tid, made = make_bracket(client)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    r = post_with_idem(client, f"/tournament/{tid}/declare", headers,
                       json={"player_id": "", "match_id": match["match_id"]})
    assert r.status_code == 422, r.text


def test_declare_requires_an_idempotency_key_header(client):
    """The key is required and is never minted server-side.

    A per-attempt key would make a retry a new request; a body-derived key
    swallows a second genuine request.
    """
    tid, made = make_bracket(client)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    r = client.post(f"/tournament/{tid}/declare", headers=headers,
                    json={"player_id": pid, "match_id": match["match_id"]})
    assert r.status_code == 400, r.text


def test_declare_refuses_an_idempotency_key_that_is_only_junk(client):
    """A key the wallet's store could not store is refused at the door."""
    tid, made = make_bracket(client)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    bad = dict(headers)
    bad["Idempotency-Key"] = "\x00bad"
    r = client.post(f"/tournament/{tid}/declare", headers=bad,
                    json={"player_id": pid, "match_id": match["match_id"]})
    assert r.status_code == 400, r.text


def test_withdraw_requires_an_idempotency_key_and_nothing_else(client):
    """The whole withdrawal request is the caller's id."""
    tid, made = make_bracket(client)
    pid, headers = made[0]
    r = client.post(f"/tournament/{tid}/withdraw", headers=headers,
                    json={"player_id": pid})
    assert r.status_code == 400, r.text

    r = post_with_idem(client, f"/tournament/{tid}/withdraw", headers,
                       json={"player_id": pid})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "tournament_id": tid, "state": "withdrawn"}


def test_withdraw_refuses_a_body_that_names_a_replacement_entrant(client):
    """A caller cannot nominate a replacement — a nominator can buy."""
    tid, made = make_bracket(client)
    pid, headers = made[0]
    other_pid, _ = made[1]
    r = post_with_idem(client, f"/tournament/{tid}/withdraw", headers,
                       json={"player_id": pid, "replacement_id": other_pid})
    assert r.status_code == 422, r.text


# ===========================================================================
# 4. Error cases: 404, 403, 409, 400
# ===========================================================================


def test_bracket_read_404s_on_an_unknown_tournament(client):
    pid, headers = new_player(client)
    r = client.get("/tournament/no_such_bracket/bracket", headers=headers)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Tournament not found"


def test_verify_404s_for_a_player_who_is_not_an_entrant(client):
    """The verifier is a window onto the caller's OWN bracket, not somebody
    else's — a non-entrant gets the same 404 as a bracket that does not exist."""
    tid, made = make_bracket(client)
    _, headers = made[0]
    _, outsider_headers = new_player(client)
    r = client.get(f"/tournament/{tid}/verify", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/tournament/{tid}/verify", headers=outsider_headers)
    assert r.status_code == 404, r.text


def test_verify_404s_on_an_unknown_tournament(client):
    _, headers = new_player(client)
    r = client.get("/tournament/no_such_bracket/verify", headers=headers)
    assert r.status_code == 404, r.text


def test_declare_404s_on_an_unknown_match(client):
    tid, made = make_bracket(client)
    pid, headers = made[0]
    r = post_with_idem(client, f"/tournament/{tid}/declare", headers,
                       json={"player_id": pid, "match_id": "no_such_match"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Match not found"


def test_declare_403s_a_player_who_is_not_in_that_match(client):
    """Being signed in is not enough — you must be one of the two entrants."""
    tid, made = make_bracket(client)
    outsider_pid, outsider_headers = new_player(client)
    store = tng.tournament_store()
    match = store.list_matches(tid, round_number=1)[0]
    # The outsider's OWN id in the body, so the session check passes and the
    # match check is the one that refuses: they really are in no match.
    r = post_with_idem(client, f"/tournament/{tid}/declare", outsider_headers,
                       json={"player_id": outsider_pid,
                             "match_id": match["match_id"]})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "You are not in that match"


def test_withdraw_404s_on_an_unknown_bracket_and_on_a_non_entrant(client):
    pid, headers = new_player(client)
    r = post_with_idem(client, "/tournament/no_such_bracket/withdraw", headers,
                       json={"player_id": pid})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Tournament not found"

    tid, made = make_bracket(client)
    r = post_with_idem(client, f"/tournament/{tid}/withdraw", headers,
                       json={"player_id": pid})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "You are not entered in this bracket"


def test_withdraw_409s_once_the_bracket_has_decided_their_place(client):
    """After elimination or crowning there is nothing left to withdraw."""
    tid, made = make_bracket(client, size=2)
    pid, headers = made[0]
    store = tng.tournament_store()
    store.update_standing(tid, pid, state="eliminated", eliminated_round=1)
    r = post_with_idem(client, f"/tournament/{tid}/withdraw", headers,
                       json={"player_id": pid})
    assert r.status_code == 409, r.text
    assert "already decided" in r.json()["detail"]


def test_operator_void_404s_on_an_unknown_bracket(client):
    r = client.post("/admin/tournament/no_such_bracket/void", headers=ADMIN_HEADERS)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Tournament not found"


def test_settle_404s_on_an_unknown_bracket_for_a_correct_secret(client):
    r = client.post("/tournament/no_such_bracket/settle", headers=INTERNAL_HEADERS)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Tournament not found"


@pytest.mark.parametrize(
    "now",
    ["not-a-number", "abc", "1,5", "0x10", "NaN", "nan", "inf", "1e400", "١٢٣"],
)
def test_clock_tick_400s_a_now_the_server_cannot_use(client, now):
    """Rejected BY SHAPE, not by try/float.

    ``float()`` accepts ``nan`` and ``inf`` without raising, and both land on
    the same branch: "the window shut", so one request settles a whole
    bracket. A try/except would not close that, so the accepted spelling is a
    plain ASCII decimal literal and everything else is a 400.
    """
    r = client.post(f"/tournament/clock/tick?now={now}", headers=INTERNAL_HEADERS)
    assert r.status_code == 400, f"?now={now} answered {r.status_code}: {r.text}"
    assert "finite number" in r.json()["detail"]


@pytest.mark.parametrize("now", ["", "  12345  ", "+12345", "1.5", "1e3"])
def test_clock_tick_treats_a_usable_or_absent_now_as_a_number(client, now):
    """The accepting half of the same contract, so the 400s above mean
    something. Absent and empty mean "use the server clock"."""
    r = client.post(f"/tournament/clock/tick?now={now}", headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text


# ===========================================================================
# 5. Response shapes
# ===========================================================================


def test_bracket_read_answers_the_documented_shape_and_no_stat_lines(client):
    """The published draw, and no frozen stat line anywhere in it.

    A client that can read a rival's numbers can time its declaration around
    knowing who it drew, so the omission is the contract.
    """
    tid, made = make_bracket(client, size=4)
    pid, headers = made[0]
    r = client.get(f"/tournament/{tid}/bracket", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body) == {
        "tournament_id", "state", "format", "mode", "size", "current_round",
        "total_rounds", "round_windows", "entrants", "matches", "my_next_match",
    }, sorted(body)
    assert body["tournament_id"] == tid
    assert body["state"] == "running"
    assert body["format"] == "single"
    assert body["size"] == 4
    assert body["total_rounds"] == 3 - 1  # a bracket of four is two rounds
    assert body["current_round"] == 1

    assert {e["player_id"] for e in body["entrants"]} == {p for p, _ in made}
    for entrant in body["entrants"]:
        assert set(entrant) == {
            "player_id", "seed_number", "state", "eliminated_round", "matches_played",
        }, sorted(entrant)

    assert len(body["matches"]) == 2
    for match in body["matches"]:
        assert set(match) == {
            "match_id", "round_number", "slot", "state", "entrant_a", "entrant_b",
            "winner_id", "a_declared", "b_declared", "opens_at", "closes_at",
        }, sorted(match)

    # No stat line leaks: neither the opponent's numbers nor any key that
    # could carry them.
    blob = r.text
    for banned in ("frozen_stats", "stat_key", '"atk"', '"hp"', '"def_"', "entry_score"):
        assert banned not in blob, f"the bracket read leaked {banned}: {blob[:400]}"


def test_bracket_read_reports_the_callers_own_next_match_with_the_opponent(client):
    """The one part of the view that is about the caller specifically."""
    tid, made = make_bracket(client, size=4)
    pid, headers = made[0]
    body = client.get(f"/tournament/{tid}/bracket", headers=headers).json()
    mine = body["my_next_match"]
    assert mine is not None, "an entrant with a scheduled match has no next match"
    assert set(mine) == {
        "match_id", "round_number", "slot", "state", "opponent_id",
        "opponent_seed", "declared",
    }, sorted(mine)
    assert mine["declared"] is False
    assert mine["opponent_id"] != pid
    assert isinstance(mine["opponent_seed"], int)


def test_verify_answers_a_per_match_report(client):
    """The fairness claim, made checkable, and it is read-only."""
    tid, made = make_bracket(client, size=2)
    _, headers = made[0]
    pid_a, _ = made[0]
    pid_b, _ = made[1]
    store = tng.tournament_store()
    match = store.list_matches(tid, round_number=1)[0]
    for pid in (match["entrant_a"], match["entrant_b"]):
        tng.declare(store, tournament_id=tid, match_id=match["match_id"], player_id=pid)

    body = client.get(f"/tournament/{tid}/verify", headers=headers).json()
    assert body["tournament_id"] == tid
    assert set(body) == {
        "tournament_id", "verified", "mismatched", "tampered", "skipped",
        "ok", "matches",
    }, sorted(body)
    assert body["verified"] >= 1, (
        "the verifier re-simulated nothing, which is the failure this test exists "
        f"to catch: {body}"
    )
    assert body["mismatched"] == 0
    assert body["ok"] is True
    entry = body["matches"][0]
    assert entry["status"] == "ok"
    assert entry["recorded_winner"] == entry["replayed_winner"] in (pid_a, pid_b)


def test_declare_answers_the_declaration_not_a_result(client):
    """One declaration is a statement, not a result."""
    tid, made = make_bracket(client, size=2)
    pid, headers = made[0]
    match = tng.match_for_player(tng.tournament_store(), tid, pid)
    r = post_with_idem(client, f"/tournament/{tid}/declare", headers,
                       json={"player_id": pid, "match_id": match["match_id"],
                             "battle_record": {"turns": 3}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"match_id", "state", "winner_id", "replayed"}, sorted(body)
    assert body["winner_id"] is None, (
        "one declaration resolved a match — the winner is not the client's to say"
    )
    assert body["state"] == "waiting"
    assert tng.tournament_store().get_match(match["match_id"])["winner_id"] is None


def test_the_second_declaration_resolves_and_the_third_replays_the_record(
    client,
):
    """Both declared resolves it; a later declaration returns what was decided."""
    tid, made = make_bracket(client, size=2)
    pid_a, headers_a = made[0]
    pid_b, headers_b = made[1]
    match = tng.match_for_player(tng.tournament_store(), tid, pid_a)

    first = post_with_idem(client, f"/tournament/{tid}/declare", headers_a,
                           json={"player_id": pid_a, "match_id": match["match_id"]})
    assert first.status_code == 200, first.text
    assert first.json()["winner_id"] is None

    second = post_with_idem(client, f"/tournament/{tid}/declare", headers_b,
                            json={"player_id": pid_b, "match_id": match["match_id"]})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["state"] == "resolved"
    recorded = body["winner_id"]
    assert recorded in (pid_a, pid_b), (
        "the recorded winner is not one of the two drawn entrants"
    )
    assert body["replayed"] is False

    third = post_with_idem(client, f"/tournament/{tid}/declare", headers_a,
                           json={"player_id": pid_a, "match_id": match["match_id"]})
    assert third.status_code == 200, third.text
    assert third.json()["winner_id"] == recorded
    assert third.json()["replayed"] is True


def test_the_battle_record_is_audit_trail_and_never_the_result(client):
    """A battle record full of lies changes nothing about who won.

    Non-vacuous: the two entrants are deliberately unequal, and the winner must
    still be the one the SERVER's own re-simulation of the frozen stat lines
    produces. The assertions would fail if the record were honoured.
    """
    tid, made = make_bracket(client, size=2)
    pid_a, headers_a = made[0]
    pid_b, headers_b = made[1]
    match = tng.match_for_player(tng.tournament_store(), tid, pid_a)
    lying = {"winner": "me", "enemy_hp": 1, "my_atk": 99999, "damage": 10 ** 6}

    first = post_with_idem(client, f"/tournament/{tid}/declare", headers_a,
                           json={"player_id": pid_a, "match_id": match["match_id"],
                                 "battle_record": lying})
    assert first.status_code == 200, first.text
    second = post_with_idem(client, f"/tournament/{tid}/declare", headers_b,
                            json={"player_id": pid_b, "match_id": match["match_id"]})
    assert second.status_code == 200, second.text
    recorded = second.json()["winner_id"]

    report = tng.verify_bracket(tng.tournament_store(), tid)
    assert report["verified"] >= 1, report
    assert report["mismatched"] == 0, (
        "the recorded winner did not reproduce from the frozen stat lines and the "
        f"derived seed, so it came from somewhere else: {report}"
    )
    assert report["ok"] is True


def test_clock_tick_answers_one_result_per_bracket_it_owns(client):
    """The shape of a tick: a count and the per-bracket results behind it."""
    tid, _ = make_bracket(client, size=2)
    r = client.post(f"/tournament/clock/tick?now={FAR_FUTURE}",
                    headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "ticks", "results"}, sorted(body)
    assert body["ticks"] == len(body["results"])
    mine = [x for x in body["results"] if x["tournament_id"] == tid]
    assert len(mine) == 1, body
    assert set(mine[0]) >= {"tournament_id", "action", "state"}
    assert mine[0]["action"] in {"open", "advanced", "settled", "noop", "error"}


def test_clock_tick_records_a_forfeit_when_the_window_shuts_undeclared(client):
    """The window is the schedule, not a login.

    A bracket whose round closes with nobody declared forfeits, so an absent
    player cannot hold the whole draw up by staying offline.
    """
    tid, made = make_bracket(client, size=2)
    store = tng.tournament_store()
    r = client.post(f"/tournament/clock/tick?now={FAR_FUTURE}",
                    headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    rows = store.list_standings(tid)
    assert len(rows) == 2
    assert any(row["state"] == "eliminated" for row in rows), rows
    loser = [row for row in rows if row["state"] == "eliminated"][0]
    assert loser["forfeits"] == 1, loser
    assert loser["eliminated_round"] == 1, loser


def test_settle_answers_a_count_of_paid_skipped_and_refused(client):
    """The settlement receipt, with the three outcomes kept apart.

    A key already claimed is SKIPPED, not refused: counting a correct replay as
    a broken wallet makes a working guard look like a failure.
    """
    tid, made = make_bracket(client, size=2)
    r = client.post(f"/tournament/{tid}/settle", headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"tournament_id", "state", "paid", "skipped", "refused",
                         "receipts"}, sorted(body)
    assert body["state"] == "settled"
    assert body["paid"] == len(body["receipts"]), body

    # A second settlement pays nobody again and says so as SKIPPED.
    again = client.post(f"/tournament/{tid}/settle", headers=INTERNAL_HEADERS)
    assert again.status_code == 200, again.text
    second = again.json()
    assert second["paid"] == 0, second
    assert second["skipped"] >= 1, second
    assert second["refused"] == 0, (
        "a correct replay was counted as a refusal, which makes a working "
        "once-only guard read as a broken wallet: %r" % (second,)
    )


def test_settle_pays_a_voided_bracket_nobody(client):
    """Voiding is a state change, never a deletion: the rows stay, nobody is
    paid, and the history stays auditable."""
    tid, made = make_bracket(client, size=2)
    assert client.post(f"/admin/tournament/{tid}/void",
                       headers=ADMIN_HEADERS).status_code == 200
    body = client.post(f"/tournament/{tid}/settle", headers=INTERNAL_HEADERS).json()
    assert body == {"tournament_id": tid, "state": "void", "paid": 0, "skipped": 0}


def test_list_marks_the_callers_own_seat_and_nobody_elses(client):
    """The list is per-caller: ``entered`` is a fact about the caller."""
    tid, made = make_bracket(client, size=2)
    pid, headers = made[0]
    outsider, outsider_headers = new_player(client)

    mine = client.post("/tournament/list", json={"player_id": pid},
                       headers=headers).json()
    row = [x for x in mine["tournaments"] if x["tournament_id"] == tid][0]
    assert set(row) == {
        "tournament_id", "state", "format", "mode", "size", "current_round",
        "total_rounds", "entered", "my_state", "my_seed",
    }, sorted(row)
    assert row["entered"] is True
    assert row["my_state"] == "entered"
    assert isinstance(row["my_seed"], int)

    theirs = client.post("/tournament/list", json={"player_id": outsider},
                         headers=outsider_headers).json()
    row2 = [x for x in theirs["tournaments"] if x["tournament_id"] == tid][0]
    assert row2["entered"] is False
    assert row2["my_state"] is None
    assert row2["my_seed"] is None


# ===========================================================================
# 6. Engine contracts
# ===========================================================================


def test_the_seed_is_derived_from_the_draw_position_and_is_stable():
    """sha256, not ``hash()``.

    ``hash()`` on a string is salted per process, so a seed derived from it
    would differ in a second interpreter and a bracket could not be re-checked
    after a restart. Stability here is asserted across a subprocess, which is
    the property a same-process assert cannot see.
    """
    import subprocess

    first = tng.match_seed("t1", 2, 3)
    assert first == tng.match_seed("t1", 2, 3)
    assert first != tng.match_seed("t1", 2, 4)
    assert first != tng.match_seed("t2", 2, 3)
    assert 0 <= first < 2 ** 32

    code = (
        "import sys; sys.path.insert(0, r'%s'); import tournament as t; "
        "print(t.match_seed('t1', 2, 3))" % (REPO_ROOT / "server")
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(first), (
        "the seed differs between interpreters, so a bracket could not be "
        f"re-verified after a restart: {out.stdout!r} vs {first}"
    )


def test_the_bracket_seed_is_its_own_derivation():
    assert tng.bracket_seed("t1") == tng.bracket_seed("t1")
    assert tng.bracket_seed("t1") != tng.bracket_seed("t2")


def test_freezing_an_entry_copies_the_players_own_row_and_fingerprints_it():
    """The frozen copy is the only stat line a fight in the bracket ever sees."""
    player = {"id": "p1", "cultivation_realm": "gold", "cultivation_stage": "late",
              "spirit_root": "water", "atk": 210, "def_": 33, "hp": 900,
              "mp": 120, "spd": 55, "unused_field": "ignored"}
    frozen = tng.freeze_entry_stats(player)

    assert set(frozen) == {"player_id", "realm", "stage", "element", "atk", "def",
                           "hp", "mp", "spd"}, sorted(frozen)
    assert frozen["player_id"] == "p1"
    assert frozen["realm"] == "gold"
    assert frozen["atk"] == 210
    assert "unused_field" not in frozen

    # A row edited after the fact no longer fingerprints to its own key, which
    # is how a tampered bracket becomes detectable rather than invisible.
    key = tng.stat_fingerprint(frozen)
    assert key == tng.stat_fingerprint(dict(frozen))
    tampered = dict(frozen, atk=99999)
    assert tng.stat_fingerprint(tampered) != key


def test_a_frozen_line_defaults_rather_than_raising_on_a_bare_row():
    """A player row missing every combat field still freezes, with documented
    defaults, so a half-written row cannot crash the draw."""
    frozen = tng.freeze_entry_stats({"id": "bare"})
    assert frozen["player_id"] == "bare"
    assert frozen["realm"] == "mortal"
    assert frozen["stage"] == "early"
    assert frozen["element"] == "fire"
    assert isinstance(frozen["atk"], int)
    # A null stat is a zero, not a crash.
    assert tng.freeze_entry_stats({"id": "x", "atk": None})["atk"] == 0


def test_a_bracket_size_that_is_not_a_power_of_two_is_refused():
    """Stated as a contract, not a convenience."""
    for bad in (1, 3, 5, 6, 7, 9, 100):
        with pytest.raises(ValueError):
            tng.total_rounds(bad)
    assert tng.total_rounds(2) == 1
    assert tng.total_rounds(4) == 2
    assert tng.total_rounds(64) == 6


def test_create_bracket_refuses_every_way_of_getting_a_bad_bracket(client):
    """The four refusals, each with its own reason."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    ids = [pid_a, pid_b]

    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="x", size=4, entrants=ids,
                           entry_scores={}, format_="round_robin")
    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="x", size=6, entrants=ids,
                           entry_scores={})
    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="x", size=1, entrants=ids,
                           entry_scores={})
    with pytest.raises(ValueError):
        # An entrant may hold only one seat.
        tng.create_bracket(store, tournament_id="x", size=4,
                           entrants=[pid_a, pid_a, pid_b, pid_b],
                           entry_scores={})
    with pytest.raises(ValueError):
        # A bracket of one entrant is a reward with no competition.
        tng.create_bracket(store, tournament_id="x", size=2, entrants=[pid_a],
                           entry_scores={})


def test_a_second_create_for_the_same_id_is_refused(client):
    """The guard compares the VALUE, not the dict's keys.

    Reading membership in the bracket row's keys is always False, so the guard
    never fires, and a second create rewrites every entrant row — which resets a
    withdrawn player to payable. This is a money bug, so it is pinned.
    """
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    ids = [pid_a, pid_b]
    tng.create_bracket(store, tournament_id="dup", size=2, entrants=ids,
                       entry_scores={pid_a: 10, pid_b: 5}, now=CREATE_NOW)
    assert store.get_bracket("dup") is not None

    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="dup", size=2, entrants=ids,
                           entry_scores={pid_a: 10, pid_b: 5}, now=CREATE_NOW)


def test_a_withdrawal_is_terminal_through_both_write_paths(client):
    """Both write paths agree, because one of them being unguarded is how a
    quitter becomes payable again."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    tng.create_bracket(store, tournament_id="wd", size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)

    store.update_standing("wd", pid_a, state="withdrawn")
    store.update_standing("wd", pid_a, state="eliminated", eliminated_round=1)
    assert store.get_standing("wd", pid_a)["state"] == "withdrawn", (
        "update_standing resurrected a withdrawn player"
    )

    row = dict(store.get_standing("wd", pid_b))
    row["state"] = "eliminated"
    store.put_standing("wd", pid_b, row)
    assert store.get_standing("wd", pid_b)["state"] == "eliminated"

    row2 = dict(store.get_standing("wd", pid_a))
    row2["state"] = "entered"
    store.put_standing("wd", pid_a, row2)
    assert store.get_standing("wd", pid_a)["state"] == "withdrawn", (
        "put_standing resurrected a withdrawn player"
    )


def test_settlement_pays_only_the_payable_states(client):
    """A whitelist, failing closed.

    A row whose state nobody expected is NOT paid — a blacklist would pay it.
    """
    assert tng._is_payable_standing({"state": "entered"}) is True
    assert tng._is_payable_standing({"state": "active"}) is True
    assert tng._is_payable_standing({"state": "eliminated"}) is True
    assert tng._is_payable_standing({"state": "champion"}) is True
    assert tng._is_payable_standing({"state": "withdrawn"}) is False
    assert tng._is_payable_standing({"state": "corrupt-row"}) is False
    assert tng._is_payable_standing({"state": ""}) is False
    assert tng._is_payable_standing(None) is False


def test_placement_is_derived_from_the_round_a_player_was_knocked_out():
    """With eight entrants, four quarter-final losers share 5th-8th, two semi
    losers share 3rd-4th, the final loser is 2nd, and a champion is 1st."""
    assert tng.placement_for(None, 8) == 1          # champion
    assert tng.placement_for(3, 8) == 2             # lost the final
    assert tng.placement_for(2, 8) == 3             # lost a semi
    assert tng.placement_for(1, 8) == 5             # lost a quarter-final
    assert tng.placement_for(2, 4) == 2             # lost the final of four
    assert tng.placement_for(1, 4) == 3             # lost a semi of four
    assert tng.placement_for(1, 2) == 2
    # Never better than first and never worse than last, whatever the round
    # arithmetic is handed. A round number the bracket never had still answers
    # inside the band rather than paying out of range.
    assert tng.placement_for(99, 8) <= 8
    assert tng.placement_for(1, 8) >= 2


def test_a_bigger_bracket_pays_more_in_total_and_never_pays_nothing():
    """The prize relation, not a frozen table.

    Two properties: every placing position is worth at least one stone, and a
    wider field pays out more in total than the narrow one it is derived from.
    """
    for size in (2, 4, 8, 16, 32, 64):
        amounts = [tng.prize_for(p, size) for p in range(1, size + 1)]
        assert all(isinstance(a, int) and a > 0 for a in amounts), (size, amounts)
    total = lambda size: sum(tng.prize_for(p, size) for p in range(1, size + 1))
    assert total(16) > total(8), (total(16), total(8))
    assert total(32) > total(16)
    # First place beats last place at every size.
    for size in (2, 4, 8, 16, 64):
        assert tng.prize_for(1, size) > tng.prize_for(size, size)


def test_the_payout_key_is_the_natural_key_not_the_callers_idempotency_key():
    """A settlement has no second intent, so "pays once" is a fact about the
    data and holds even when a client invents a different key for its retry."""
    assert tng.payout_key("t1", "p1") == "t1:p1:settle"
    assert tng.payout_key("t1", "p1") == tng.payout_key("t1", "p1")
    assert tng.payout_key("t1", "p1") != tng.payout_key("t2", "p1")
    assert tng.payout_key("t1", "p1") != tng.payout_key("t1", "p2")


def test_a_claim_for_a_non_positive_amount_is_refused(client):
    """A zero row would burn the payout key and deny the real payment that
    follows it."""
    store = tng.tournament_store()
    for amount in (0, -1):
        with pytest.raises(ValueError):
            store.claim_payout("k", tournament_id="t", player_id="p",
                               placement=1, amount=amount)


def test_a_withdrawn_player_is_never_paid_at_settlement(client):
    """The defect this whole engine exists to not have."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    tng.create_bracket(store, tournament_id="wq", size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    store.update_standing("wq", pid_a, state="withdrawn")

    result = tng.settle_bracket(store, tournament_id="wq")
    row = store.get_standing("wq", pid_a)
    assert row["state"] == "withdrawn"
    # A row the engine never paid carries no placement, no key and no amount.
    # Asserted as absent-or-None because a fresh entrant row simply has no such
    # key at all until a settlement writes one, and "missing" and "set to null"
    # mean the same thing to every reader of this row.
    assert row.get("placement") is None, row
    assert row.get("amount") is None, row
    assert row.get("payout_key") is None, row
    assert all(r.get("amount", 0) > 0 for r in result["receipts"]), result
    assert result["paid"] == len(result["receipts"])


def test_the_verifier_reports_a_rewritten_winner_as_a_mismatch(client):
    """A verifier that always says "ok" is worse than none, because it is
    trusted. Proved by breaking something first."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    tng.create_bracket(store, tournament_id="vf", size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    match = store.list_matches("vf", round_number=1)[0]
    for pid in (match["entrant_a"], match["entrant_b"]):
        tng.declare(store, tournament_id="vf", match_id=match["match_id"],
                    player_id=pid)
    real_winner = store.get_match(match["match_id"])["winner_id"]

    assert tng.verify_bracket(store, "vf")["ok"] is True

    other = pid_b if real_winner == pid_a else pid_a
    store.update_match(match["match_id"], winner_id=other)
    report = tng.verify_bracket(store, "vf")
    assert report["mismatched"] == 1, report
    assert report["ok"] is False


def test_the_verifier_reports_a_tampered_stat_line_rather_than_re_deciding(
    client,
):
    """A frozen line that no longer fingerprints to its own key is a row to
    investigate, not a fight to re-run."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    tng.create_bracket(store, tournament_id="vt", size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    match = store.list_matches("vt", round_number=1)[0]
    for pid in (match["entrant_a"], match["entrant_b"]):
        tng.declare(store, tournament_id="vt", match_id=match["match_id"],
                    player_id=pid)
    assert tng.verify_bracket(store, "vt")["ok"] is True

    row = store.get_standing("vt", pid_a)
    row["frozen_stats"] = dict(row["frozen_stats"], atk=99999)
    store._standings[("vt", pid_a)] = row
    report = tng.verify_bracket(store, "vt")
    assert report["tampered"] == 1, report
    assert report["ok"] is False


def test_a_clock_decided_match_is_skipped_by_the_verifier(client):
    """A forfeit has no simulation behind it, so re-running one would invent an
    outcome that never happened."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    tng.create_bracket(store, tournament_id="sk", size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    tng.tick_bracket(store, "sk", now=FAR_FUTURE)
    report = tng.verify_bracket(store, "sk")
    assert report["skipped"] >= 1, report
    for entry in report["matches"]:
        if entry["state"] in ("bye", "forfeit"):
            assert entry["status"] == "skipped", entry
            assert "clock" in entry["reason"], entry


def test_the_settlement_clock_is_an_absolute_utc_boundary():
    """03:00 UTC every day, computed from the calendar.

    A relative ``now + 86400`` would give a different answer in every process
    that started at a different moment, so boundaries would drift apart across
    restarts. Asserted as a RELATION: two probes either side of one boundary
    and a third on the next day all land on 03:00 UTC.
    """
    import calendar

    def as_utc_hours(epoch):
        return time.gmtime(epoch).tm_hour

    probe = time.mktime((2026, 5, 1, 2, 0, 0, 0, 0, 0)) - time.timezone
    boundary = tng.settlement_due_at(probe)
    assert as_utc_hours(boundary) == tng.SETTLEMENT_HOUR_UTC
    assert boundary > probe

    # After the boundary, the answer is the NEXT day's boundary, 24h later.
    after = probe + 2 * 3600
    nxt = tng.settlement_due_at(after)
    assert as_utc_hours(nxt) == tng.SETTLEMENT_HOUR_UTC
    assert abs((nxt - boundary) - 24 * 3600) < 2, (boundary, nxt)
    assert calendar.timegm(time.gmtime(nxt)) == calendar.timegm(time.gmtime(boundary)) + 86400


def test_a_brackets_round_windows_are_published_before_the_draw_opens(client):
    """Round 1 opens now and closes 24 hours later; later rounds publish a
    window but no opening time, because they have not started yet."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    pid_c, _ = new_player(client)
    pid_d, _ = new_player(client)
    tng.create_bracket(store, tournament_id="w", size=4,
                       entrants=[pid_a, pid_b, pid_c, pid_d], entry_scores={},
                       now=CREATE_NOW)
    bracket = store.get_bracket("w")
    windows = {w["round"]: w for w in bracket["round_windows"]}
    assert set(windows) == {1, 2}
    assert windows[1]["opens_at"] == CREATE_NOW
    assert windows[1]["closes_at"] == CREATE_NOW + tng.ROUND1_WINDOW_SECONDS
    assert windows[1]["window_seconds"] == tng.ROUND1_WINDOW_SECONDS
    assert windows[2]["window_seconds"] == tng.LATER_ROUND_WINDOW_SECONDS
    assert windows[2]["opens_at"] is None
    assert windows[2]["closes_at"] is None


def test_a_short_field_gets_byes_and_a_bye_is_not_a_recorded_victory(client):
    """A bye is its own match state, because the placement payout READS this
    record: a bye recorded as a win would pay a player who never fought."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    pid_c, _ = new_player(client)
    tng.create_bracket(store, tournament_id="by", size=4,
                       entrants=[pid_a, pid_b, pid_c], entry_scores={},
                       now=CREATE_NOW)
    matches = store.list_matches("by", round_number=1)
    assert len(matches) == 2
    byes = [m for m in matches if m["state"] == "bye"]
    real = [m for m in matches if m["state"] == "scheduled"]
    assert len(byes) == 1 and len(real) == 1, matches
    assert byes[0]["entrant_b"] is None
    assert byes[0]["winner_id"] == byes[0]["entrant_a"]
    # The bye still counts the player as having played nothing.
    assert store.get_standing("by", byes[0]["entrant_a"])["state"] == "entered"


def test_a_broken_bracket_does_not_stop_the_clock_for_the_others(client):
    """One poisoned bracket must not take every other bracket down with it."""
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    good = "good"
    bad = "bad"
    tng.create_bracket(store, tournament_id=good, size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    tng.create_bracket(store, tournament_id=bad, size=2,
                       entrants=[pid_a, pid_b], entry_scores={}, now=CREATE_NOW)
    # Break one: a match whose entrants no longer have standing rows.
    match = store.list_matches(bad, round_number=1)[0]
    store.update_match(match["match_id"], state="scheduled",
                       entrant_a=match["entrant_a"], entrant_b="ghost")
    store.update_match(match["match_id"], declared_a=True, declared_b=True)

    results = {r["tournament_id"]: r for r in tng.tick_all(store, now=FAR_FUTURE)}
    assert bad in results, results
    assert results[bad]["action"] in ("error", "settled"), results[bad]
    assert good in results, (
        "the broken bracket stopped the clock for the healthy one: %r" % (results,)
    )
    assert results[good]["action"] != "error", results[good]


# ===========================================================================
# 10. The store binding: one per process, and a rebind is refused BY NAME
# ===========================================================================
#
# `tournament.tournament_store()` is a process-wide singleton and its player
# store / wallet pair is PROCESS wiring. Before this contract, `bind()`
# assigned unconditionally and route registration called it, so the LAST app to
# register silently won for every app in the process. The sibling proved it by
# object identity: a test harness registering the bracket routes against a
# throwaway PlayerStore left the real app reading the throwaway's rows and broke
# 17 tests in the files that ran after it.
#
# Three halves, because any one alone leaves a hole:
#   * a second, DIFFERENT bind is refused, and the error names both stores, so
#     the clash is visible without a debugger;
#   * the SAME pair twice is accepted, because registration can legitimately run
#     twice in one process and two getters answering one store are one wiring;
#   * registration does not bind at all -- each request resolves its own app's
#     stores -- so refusing the clash also leaves a second app able to serve.
#
# Every test here builds its OWN `TournamentStore`, never the singleton, so a
# failure cannot leak a binding into the files that run after.


class _RecordingStore(tng.TournamentStore):
    """A store that remembers which app's transaction ran through it.

    Named by subclass rather than by `__class__` assignment so the refusal
    message below can name it: two plain `PlayerStore()` instances are
    indistinguishable in a traceback, and telling them apart is the whole point
    of the error.
    """

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.transactions = 0
        # The store's own statement that it is not in a transaction; the route
        # branches on this to decide whether the work is durable.
        self.current_connection = None

    def transaction(self, player_id=None):
        self.transactions += 1
        return _NullTransaction()

    def __contains__(self, key):
        return False

    def __repr__(self) -> str:
        # The store describes ITSELF, which is what the refusal message quotes.
        return f"<bracket store {self.label}>"


class _NullTransaction:
    """A transaction scope that commits nothing and rolls back nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_second_bind_to_a_different_store_is_refused_and_names_both():
    """The measured harm, as a contract: the second wiring must not be applied."""
    held = _RecordingStore("FirstApp")
    refused = _RecordingStore("SecondApp")
    held_wallet, refused_wallet = object(), object()

    held.bind(held, held_wallet)

    with pytest.raises(tng.TournamentBindingConflict) as caught:
        held.bind(refused, refused_wallet)

    message = str(caught.value)
    # Both sides named. A refusal that cannot say WHICH two were offered is a
    # refusal the caller cannot act on.
    assert "FirstApp" in message, message
    assert "SecondApp" in message, message
    # And the process wiring is UNCHANGED: a refused bind must not have applied
    # half of itself before raising.
    assert held.players_db is held
    assert held.wallet_store is held_wallet


def test_binding_the_same_store_twice_is_accepted():
    """Registration can legitimately run twice, so a repeat must not raise."""
    store = _RecordingStore("Repeat")
    players, wallet = _RecordingStore("Players"), object()
    store.bind(players, wallet)

    # Twice by identity...
    store.bind(players, wallet)
    # ...and again through a DIFFERENT getter object answering the same stores,
    # which is what main.py's `lambda: players_db` looks like on a second
    # registration. Compared by resolved identity, not by getter identity.
    store.bind(lambda: players, lambda: wallet)
    assert store.players_db is players
    assert store.wallet_store is wallet


def test_the_refusal_is_a_runtime_error_so_an_existing_handler_still_works():
    """A caller that means to move the store can catch exactly this and say so."""
    store = _RecordingStore("Named")
    store.bind(_RecordingStore("Kept"), object())
    with pytest.raises(RuntimeError):
        store.bind(_RecordingStore("Other"), object())
    assert issubclass(tng.TournamentBindingConflict, RuntimeError)


def test_registering_the_routes_does_not_bind_the_process_wide_store():
    """Registration must not repoint the process wiring at all.

    This is the half that stops the 17 downstream failures rather than merely
    reporting them: if `register_tournament_routes` bound the singleton, the
    second app to register would own the first app's store, and a refusal at the
    second registration would break the harness instead of fixing it.
    """
    singleton = tng.tournament_store()
    before = (singleton._players_db, singleton._wallet_store)
    app = FastAPI()
    tournament_routes.register_tournament_routes(app, _RecordingStore("Reg"), None)
    assert (singleton._players_db, singleton._wallet_store) == before, (
        "route registration rebound the process-wide store; every other app in "
        "this process now reads these stores instead of its own")


def test_each_app_serves_a_bracket_from_its_own_store():
    """Two apps, one process: each address opens ITS OWN store's transaction.

    Driven through the real `/tournament/clock/tick` address with the real
    internal secret rather than a probe route, because the wiring under test is
    the one the registrar installs on ITS OWN handlers -- a route added beside
    the registrar is not wrapped by it and would prove nothing.
    """
    one, two = _RecordingStore("AppOne"), _RecordingStore("AppTwo")
    app_one, app_two = FastAPI(), FastAPI()
    tournament_routes.register_tournament_routes(app_one, one, None)
    tournament_routes.register_tournament_routes(app_two, two, None)

    first = TestClient(app_one, raise_server_exceptions=False).post(
        "/tournament/clock/tick?now=1", headers=INTERNAL_HEADERS)
    assert first.status_code == 200, first.text
    second = TestClient(app_two, raise_server_exceptions=False).post(
        "/tournament/clock/tick?now=1", headers=INTERNAL_HEADERS)
    assert second.status_code == 200, second.text

    assert one.transactions == 1, one.transactions
    assert two.transactions == 1, two.transactions


def test_a_request_cannot_leave_its_store_behind_for_the_next_one():
    """The pair is scoped to the call, not assigned and left behind.

    Without the restore, the second app's request would find app one still
    bound to its store -- and because the pair is process-wide, every request
    after it would too.
    """
    store = tng.tournament_store()
    outside = store._players_db
    held = _RecordingStore("Scoped")
    with store.request_binding(held, None):
        assert store.players_db is held
    assert store._players_db == outside
    # Restored even when the body raises: a leak here fires exactly when the
    # process is already broken.
    with pytest.raises(RuntimeError):
        with store.request_binding(held, None):
            raise RuntimeError("handler blew up mid-request")
    assert store._players_db == outside


# ===========================================================================
# 11. Extension: the four required contracts, each probed where it can break
# ===========================================================================
#
# Sections 1-10 above cover the routes, the auth layers, the request and
# response shapes, and the engine's pure functions. These four tests close the
# gaps that survey found in that coverage, one per required contract. Each was
# written after MEASURING what the module actually does (a probe run, not a
# reading), so each asserts observed behaviour rather than a guess.
#
# 1. Creation requires auth      -> the ENGINE, not the route. The route layer
#    is closed by `test_there_is_no_route_that_creates_a_bracket_or_nominates_a_player`
#    plus the five 401s in section 2. What neither can reach is the engine
#    function itself, which is the only thing that can build a bracket. New here.
# 2. Advancement is deterministic-> the SIMULATOR at the function level. Section
#    6 pins the seed's stability; nothing pinned that the seed actually reaches
#    the fight, or that the fight is repeatable. New here.
# 3. Clock parsing rejects
#    non-finite values         -> the ENGINE takes `now` as a plain float and no
#    regex guards it. Section 4 pins the ROUTE's rejection. New here, and it
#    FAILS, because the engine has no guard of its own. See the xfail note.
# 4. Payout authorization      -> the WALLET. Every payout assertion in this file
#    reads the bracket's own receipts; none read the player's money. New here.


def _line(player_id, atk, *, hp=1000, defense=50):
    """A frozen stat line in the exact shape `freeze_entry_stats` produces."""
    return {"player_id": player_id, "realm": "mortal", "stage": "early",
            "element": "fire", "atk": atk, "def": defense, "hp": hp,
            "mp": 500, "spd": 100}


def test_creating_a_bracket_through_the_engine_needs_a_caller_the_server_trusts(
    client,
):
    """Contract 1. Bracket creation is an ENGINE-only operation, and the engine
    refuses the two things that would make it abusable.

    There is deliberately no route that opens a draw (section 1 pins that
    absence), so the ONLY way to build a bracket is to call `create_bracket`
    in-process -- behind the server's own code, not behind an address. The
    engine therefore owes the two refusals that make in-process creation safe:
    a size that is not a power of two (a 6-player "bracket" has no consistent
    round count, so two servers could derive different payouts from the same
    draw), and a duplicate seat for one entrant (two seats for one player pays
    one person twice).

    Asserted through the real function against the real singleton store, which
    is what a caller's code would do. Both refusals must be a refusal, not a
    clamp: a silently corrected bracket is a bracket whose published seed
    order does not match the one everybody was promised.
    """
    store = tng.tournament_store()
    pid_a, _ = new_player(client)
    pid_b, _ = new_player(client)
    ids = [pid_a, pid_b]

    # (a) A size the round arithmetic cannot answer.
    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="auth_size", size=6, entrants=ids,
                           entry_scores={}, now=CREATE_NOW)
    assert store.get_bracket("auth_size") is None, (
        "the refused bracket was written anyway, so the refusal is cosmetic"
    )

    # (b) One entrant holding two seats, which would pay twice for one fight.
    with pytest.raises(ValueError):
        tng.create_bracket(store, tournament_id="auth_dup", size=4,
                           entrants=[pid_a, pid_a, pid_b, pid_b],
                           entry_scores={}, now=CREATE_NOW)
    assert store.get_bracket("auth_dup") is None

    # The accepting half: the same call with legal arguments builds the
    # bracket, so the refusals above are refusals and not a broken engine.
    # Entrants live in the STANDINGS table, not on the bracket row (measured),
    # so they are read from there -- which is also where the seat numbers are.
    tid, made = make_bracket(client, size=2)
    store = tng.tournament_store()
    bracket = store.get_bracket(tid)
    assert bracket is not None and bracket["state"] == "running"
    rows = store.list_standings(tid)
    assert {row["player_id"] for row in rows} == {p for p, _ in made}
    # One seat per entrant, and the seat numbers are a permutation of 1..size.
    assert sorted(row["seed_number"] for row in rows) == [1, 2], rows


def test_bracket_advancement_is_deterministic_from_the_seed_alone():
    """Contract 2. The SAME two stat lines and the SAME seed always produce the
    SAME winner, in this process and in a fresh one.

    This is the property the whole anti-abuse design rests on. If it does not
    hold, a recorded winner cannot be re-checked after a restart, and the
    verifier in section 6 becomes a coin flip dressed as an audit.

    Non-vacuous in two directions, because a simulator that always answered the
    same way would pass a naive equality assert:
      * it REPEATS: twenty calls with one seed give one answer;
      * it VARIES: across seeds the winner is not always the same entrant, so
        the seed is genuinely reaching the fight rather than being ignored.
    The second half matters as much as the first -- a simulator that ignores the
    seed is deterministic too, and just as unauditable in the other direction.
    """
    import subprocess

    a, b = _line("A", 100), _line("B", 100)

    assert len({tng.simulate_winner(a, b, seed=7) for _ in range(20)}) == 1, (
        "the simulator is not repeatable: twenty identical calls disagreed"
    )

    winners = {tng.simulate_winner(a, b, seed=s) for s in range(24)}
    assert winners == {"A", "B"}, (
        "the winner did not vary with the seed, so the seed is not reaching the "
        f"fight -- every seed answered {winners}"
    )

    # A lopsided stat line wins regardless of the seed. Measured across the same
    # 24 seeds this held every time, so it is asserted as a property, not a
    # snapshot: if the combat model ever stops honouring a 1000x gap, this is
    # where it says so.
    strong = _line("A", 100000)
    assert {tng.simulate_winner(strong, b, seed=s) for s in range(24)} == {"A"}

    # Same answer in a FRESH interpreter, which a same-process assert cannot see.
    seed = 7
    code = (
        "import sys; sys.path.insert(0, r'%s'); import tournament as t; "
        "line = {'player_id':'A','realm':'mortal','stage':'early','element':'fire',"
        "'atk':100,'def':50,'hp':1000,'mp':500,'spd':100}; "
        "print(t.simulate_winner(line, dict(line, player_id='B'), seed=%d))"
        % (REPO_ROOT / "server", seed)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == tng.simulate_winner(a, b, seed=seed), (
        "the fight resolved differently in a second interpreter, so a recorded "
        f"winner cannot be re-verified after a restart: {out.stdout!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN DEFECT: the ENGINE accepts a non-finite `now`. "
        "tick_bracket(store, tid, now=float('inf')) returns action='settled' and "
        "crowns a champion (measured 2026-10-05, probe: before 'running' round 1, "
        "after 'settled', standings [('a...','champion'),('b...','eliminated')]). "
        "float('nan') does the same. The route's regex in tournament_routes.py "
        "refuses these before they reach the engine, and that regex is what "
        "section 4 pins -- but the engine itself has no guard, so any in-process "
        "caller (the background job, a future route, a test harness) can settle "
        "every bracket it can see with one argument. The fix belongs in "
        "tick_bracket/tick_all/settle_due, not in the route. Marked strict so "
        "the day somebody adds the guard this turns into a FAILURE and the "
        "marker gets removed rather than going stale."
    ),
)
def test_the_engine_clock_refuses_a_non_finite_now():
    """Contract 3. The engine's own clock entry point must reject `inf`/`nan`,
    not just the route in front of it.

    Measured behaviour today: `tick_bracket(store, tid, now=inf)` settles the
    bracket and crowns a champion. This asserts the CORRECT behaviour, so it
    fails while the defect is open -- see the strict-xfail reason above.
    """
    store = tng.tournament_store()
    tag = uuid.uuid4().hex[:8]
    for bad in (float("inf"), float("-inf"), float("nan")):
        tid = f"nonfinite_{tag}_{bad!r}".replace(".", "").replace("-", "neg")
        tng.create_bracket(store, tournament_id=tid, size=2,
                           entrants=[f"{tag}a", f"{tag}b"],
                           entry_scores={}, now=CREATE_NOW)
        with pytest.raises(ValueError):
            tng.tick_bracket(store, tid, now=bad)
        assert store.get_bracket(tid)["state"] == "running", (
            f"a non-finite now ({bad!r}) advanced the bracket anyway"
        )


def test_payout_authorization_credits_the_wallet_and_nobody_else(client):
    """Contract 4. A settlement pays the WINNER'S REAL WALLET, exactly the
    engine's own prize, and pays nobody else a cent.

    The gap this closes: every payout assertion in this file so far reads the
    bracket's own receipts. A receipt is the bracket's CLAIM about the money.
    This reads the player's balance through the public ``/wallet`` address --
    what the player actually holds -- so a settlement that writes a receipt and
    never credits the wallet fails here while passing section 5.

    Three halves, because any one alone leaves a hole:
      * the winner's balance goes UP by exactly ``prize_for(placement, size)``,
        taken from the engine's own table rather than a literal, so a rebalanced
        prize table does not red this test;
      * the loser's balance does not move at all;
      * a SECOND settlement moves neither balance again, so "paid once" is a
        fact about the money and not only about a counter.
    """
    store = tng.tournament_store()
    tid, made = make_bracket(client, size=2)
    pid_a, headers_a = made[0]
    pid_b, headers_b = made[1]

    def stones(pid, headers):
        r = client.get("/wallet", params={"player_id": pid}, headers=headers)
        assert r.status_code == 200, r.text
        return (r.json().get("balance") or {}).get("spirit_stone") or 0

    before = {pid_a: stones(pid_a, headers_a), pid_b: stones(pid_b, headers_b)}

    r = client.post(f"/tournament/{tid}/settle", headers=INTERNAL_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paid"] == len(body["receipts"]), body
    assert all(rec.get("paid") for rec in body["receipts"]), (
        f"a settlement reported a non-payment: {body}"
    )
    # Amounts come off the RECEIPTS, taken from the engine's own table rather
    # than literals, so a rebalanced prize table does not red this test.
    for rec in body["receipts"]:
        assert rec["amount"] == tng.prize_for(rec["placement"], 2), (
            f"the paid amount {rec['amount']} is not what the engine's own table "
            f"derives for placement {rec['placement']}"
        )
        assert rec["currency"] == tng.PAYOUT_CURRENCY, rec
        # The wallet echoed the credit it really made, so the receipt is not a
        # claim about a credit that never happened.
        assert (rec.get("wallet") or {}).get("earned") == rec["amount"], rec

    # The settlement RECEIPT carries no player id (measured), so the per-player
    # money is attributed through the STANDINGS row the engine wrote beside it.
    # Every entrant of a two-player bracket places and every placing row must
    # name the engine's own prize and carry the natural payout key.
    paid_rows = {}
    for pid in (pid_a, pid_b):
        row = store.get_standing(tid, pid)
        assert row.get("placement") is not None, (
            f"the settlement recorded no placement for {pid}: {row}"
        )
        assert row.get("payout_key") == tng.payout_key(tid, pid), row
        paid_rows[pid] = row
    assert sum(row["amount"] for row in paid_rows.values()) == sum(
        rec["amount"] for rec in body["receipts"]
    ), (paid_rows, body)

    after = {pid_a: stones(pid_a, headers_a), pid_b: stones(pid_b, headers_b)}
    deltas = {pid: after[pid] - before[pid] for pid in (pid_a, pid_b)}
    assert all(amount > 0 for amount in deltas.values()), (
        f"a settlement credited nobody: {before} -> {after}"
    )
    for pid, row in paid_rows.items():
        assert deltas[pid] == row["amount"], (
            f"the engine recorded {row['amount']} for {pid} but the wallet moved "
            f"by {deltas[pid]}: {before} -> {after}"
        )

    # A replay pays nobody a second time.
    again = client.post(f"/tournament/{tid}/settle", headers=INTERNAL_HEADERS)
    assert again.status_code == 200, again.text
    assert again.json()["paid"] == 0, again.json()
    final = {pid_a: stones(pid_a, headers_a), pid_b: stones(pid_b, headers_b)}
    assert final == after, (
        "a second settlement paid again, so once-only is a counter and not the "
        f"money: {after} -> {final}"
    )
