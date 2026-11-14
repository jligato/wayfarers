"""BOOT SMOKE TEST: does the Wayfarer's Cycle server start and answer?

WHAT THIS FILE IS
-----------------
A smoke test, not a contract test. It asks one question -- "does the server
boot, and do a handful of key routes answer instead of hanging or crashing?" --
and it fails on the very first thing that breaks. Every other test file in this
directory assumes the app boots; this is the one that proves it.

It runs the REAL shipping app (``main.app``), in memory mode, with no database.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    tools/wc_pytest.sh server/tests/test_boot_smoke.py -v

FOUR THINGS THAT ARE LOAD-BEARING HERE
--------------------------------------
1. **Memory mode is forced before ``main`` is imported.** The player store
   latches its backend ONCE, at construction. Import ``main`` first and the
   store is already bound to whatever the environment happened to say. So the
   five ``WC_PG_*`` settings are cleared at module scope, before the import.
2. **``main`` is imported BARE, not as ``server.main``.** This is not a style
   choice. Several route modules do ``import main`` internally, and that
   resolves to a DIFFERENT module object than ``server.main`` -- with its own
   player store and its own session store. Minting a session through
   ``server.auth`` and calling the ``server.main`` app reads two unrelated
   stores, so every guarded request answers 401. Importing bare ``main`` puts
   the test and the routes in one module namespace. (Same trap, measured, in
   the sibling contract tests.)
3. **The player fixture is FUNCTION-scoped, not module-scoped.** The repository
   root ``conftest.py`` carries an autouse fixture, ``_restore_shared_singletons``,
   that empties the shared in-memory player store after EVERY test so one file
   cannot poison the next. A module-scoped account is therefore deleted out from
   under the test that wants it: it is created in the first test and every later
   one gets ``404 Player not found`` for an account that plainly existed a moment
   ago. This was measured, not guessed -- 8 failures all saying "Player not found"
   for a just-created id.
4. **No frozen numbers.** This file asserts that routes answer 200 and that
   reply SHAPES hold, not that a wallet balance is a particular figure. Adding a
   feature or retuning a boss does not break the smoke test.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid
from typing import Dict, Iterator, Tuple

# --------------------------------------------------------------------------
# Memory mode, forced before anything imports the app.
# --------------------------------------------------------------------------
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Bare, NOT server.main -- see note 2 above.
import main  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """A started app.

    Using the context manager runs the startup and shutdown lifespan, which is
    where the background schedulers attach. A smoke test that skipped lifespan
    would miss a boot failure that only appears in production.
    """
    with TestClient(main.app) as c:
        yield c


def _signup(client: TestClient) -> str:
    """Create one real account through the public signup route."""
    r = client.post("/player/create",
                    json={"name": "boot-smoke-%s" % uuid.uuid4().hex[:8],
                          "spirit_root": "fire"})
    assert r.status_code in (200, 201), (
        "the signup route answered %d: %s" % (r.status_code, r.text))
    assert r.json().get("id"), "signup returned no account id: %s" % r.text
    return r.json()["id"]


def _bearer(player_id: str) -> Dict[str, str]:
    """A session header for ``player_id``, minted in-process against the same
    bare ``auth`` module the route dependencies read."""
    import auth

    return {"Authorization": "Bearer " + auth.issue_session(player_id)}


@pytest.fixture
def player(client: TestClient) -> Dict[str, str]:
    """One real account and its bearer header.

    Function-scoped on purpose -- see note 3 in the docstring.
    """
    pid = _signup(client)
    return {"id": pid, "headers": _bearer(pid)}


# --------------------------------------------------------------------------
# 1. THE APP EXISTS AND CARRIES ROUTES
# --------------------------------------------------------------------------

def test_the_app_is_constructed_with_a_real_route_table(client: TestClient):
    """A boot that produces zero routes is a boot failure wearing a 200."""
    assert main.app is not None
    mounted = [r.path for r in main.app.routes if getattr(r, "path", None)]
    assert mounted, "the app mounted no routes at all -- the wiring is gone"

    # The OpenAPI schema is generated from the live route table, so it is a
    # second, independent read of the same wiring. Assert the RELATIONSHIP, never
    # a frozen count.
    schema = client.get("/openapi.json")
    assert schema.status_code == 200, schema.text
    documented = set(schema.json().get("paths", {}))
    assert documented, "the OpenAPI schema advertised zero paths"

    orphans = sorted(p for p in documented if p not in set(mounted))
    assert not orphans, "OpenAPI documents paths the app never mounted: %s" % orphans


def test_the_root_route_identifies_the_service(client: TestClient):
    """The one route a human, or an uptime check, hits first."""
    r = client.get("/")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "operational", body
    assert body.get("name"), "the root route did not name the service: %s" % body


# --------------------------------------------------------------------------
# 1b. THE ROUTE CENSUS
#
# This is the job server/rt.py used to do by hand: boot the app, count the
# distinct route paths, and list the ones that mention a war. That script was
# deleted -- it hardcoded an absolute path to one machine and made the
# composition root importable as a side effect of any tool that walked
# server/. The census is now a test, on the bootstrap above, so it runs
# wherever the suite runs.
# --------------------------------------------------------------------------

_SECT_WAR_FAMILY = (
    "/sect-war/current",
    "/sect-war/declare",
    "/sect/war/declare",
    "/sect/war/contribute",
)


def _distinct_route_paths() -> Tuple[str, ...]:
    """Every distinct path the live app has mounted, sorted."""
    return tuple(sorted(
        {r.path for r in main.app.routes if getattr(r, "path", None)}))


def test_the_census_counts_every_distinct_route_path(client: TestClient):
    """The app mounts a whole game's worth of routes, not one or two.

    The relationship is the point, not the number: the census must cover at
    least everything the OpenAPI schema advertises (a second, independently
    generated read of the same wiring), and it must be larger than the handful
    of routes this file exercises -- otherwise the count would be measuring
    the smoke test rather than the app. No frozen figure, so adding a feature
    does not break this.
    """
    paths = _distinct_route_paths()
    assert paths, "the app mounted no routes at all -- the wiring is gone"

    documented = set(client.get("/openapi.json").json().get("paths", {}))
    assert set(documented) <= set(paths), (
        "OpenAPI documents %d paths the census missed" % len(set(documented) - set(paths)))
    assert len(paths) > len(documented), (
        "the census found %d paths for %d advertised -- mounted-only routes "
        "(docs, mounts) are missing from the census" % (len(paths), len(documented)))

    print("TOTAL ROUTES: %d" % len(paths))


def test_the_census_finds_every_sect_war_route(client: TestClient):
    """The war family is the part of the table a human is most likely to go
    looking for, so the census must actually name it.

    Two things are proven here. First, the sect-war routes are mounted on the
    app -- a rename or an unwired module reds this. Second, the census picks
    them up, so the printed list is trustworthy rather than decorative. The
    full list is printed, not asserted: reward and claim routes legitimately
    contain the substring "war" too, and freezing them would make this file
    break on every balance tweak.
    """
    paths = _distinct_route_paths()
    war = [p for p in paths if "war" in p]
    assert war, "the census found no war-related path -- the sect-war module is gone"

    missing = [p for p in _SECT_WAR_FAMILY if p not in war]
    assert not missing, "the sect-war family lost these routes: %s" % missing

    for p in war:
        assert p.startswith("/"), "the census reported a non-path: %r" % p

    print("SECT WAR: %s" % war)


# --------------------------------------------------------------------------
# 2. STATIC GAME DATA -- read-only tables, no account needed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ("/game-data/realms", "/game-data/spirit-roots", "/game-data/sects", "/game-data/npcs"),
)
def test_static_game_data_tables_answer(client: TestClient, path: str):
    r = client.get(path)
    assert r.status_code == 200, "%s answered %d: %s" % (path, r.status_code, r.text)
    body = r.json()
    assert isinstance(body, (dict, list)) and body, "%s returned an empty table" % path


# --------------------------------------------------------------------------
# 3. SIGNUP AND A READ BACK OF THE ACCOUNT IT CREATED
# --------------------------------------------------------------------------

def test_signup_creates_an_account_that_can_be_read_back(
    client: TestClient, player: Dict[str, str]
):
    """End to end: a caller holding nothing gets an account, then can read it
    using the session that account earns."""
    pid, headers = player["id"], player["headers"]

    r = client.get("/player/%s" % pid, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("id") == pid, "the read-back returned a different account: %s" % body
    # A brand new account must carry the fields the client renders on first
    # login. Present-and-not-empty, never a frozen value.
    assert body.get("cultivation_realm"), "no starting realm on a new account: %s" % body


def test_a_second_account_is_independent(client: TestClient, player: Dict[str, str]):
    """A smoke test that only ever sees one account can pass while every account
    shares a single slot. Two accounts must both read back their own id."""
    other_id = _signup(client)
    assert other_id != player["id"], "signup handed back the same id twice"

    r = client.get("/player/%s" % other_id, headers=_bearer(other_id))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == other_id, r.json()


# --------------------------------------------------------------------------
# 4. PER-ACCOUNT STATE ROUTES
#
# A measured shape difference, recorded here because it cost this file its first
# draft: some routes take the id in the PATH (``/inventory/{id}``), others take
# it as a required QUERY parameter (``/wallet?player_id=``). Calling a
# path-shaped route without an id, or a query-shaped route without the
# parameter, is a correct refusal -- not a boot failure.
# --------------------------------------------------------------------------

_PATH_SHAPED: Tuple[Tuple[str, str], ...] = (
    ("/inventory/%s", "capacity"),
    ("/sign-in/%s", "sign_in"),
    ("/daily-boss/%s", "boss"),
    ("/beasts/%s", "beasts"),
)

_QUERY_SHAPED: Tuple[Tuple[str, str], ...] = (
    ("/wallet", "player_id"),
    ("/missions/list", "missions"),
)


@pytest.mark.parametrize("template,key", _PATH_SHAPED)
def test_path_shaped_state_routes_answer(
    client: TestClient, player: Dict[str, str], template: str, key: str
):
    pid, headers = player["id"], player["headers"]
    path = template % pid
    r = client.get(path, headers=headers)
    assert r.status_code == 200, "%s answered %d: %s" % (path, r.status_code, r.text)
    body = r.json()
    assert key in body, "%s replied without %r: %s" % (path, key, body)
    # A brand new account presents the field empty, not missing and not null.
    assert body[key] is not None, "%s reported a null %r on a fresh account" % (path, key)


@pytest.mark.parametrize("path,key", _QUERY_SHAPED)
def test_query_shaped_state_routes_answer(
    client: TestClient, player: Dict[str, str], path: str, key: str
):
    pid, headers = player["id"], player["headers"]
    r = client.get(path, headers=headers, params={"player_id": pid})
    assert r.status_code == 200, "%s answered %d: %s" % (path, r.status_code, r.text)
    body = r.json()
    assert key in body, "%s replied without %r: %s" % (path, key, body)


# --------------------------------------------------------------------------
# 5. THE NEGATIVE CONTROLS -- proving this file can actually fail
# --------------------------------------------------------------------------
#
# Without these, a client wired to nothing, or an assertion comparing a reply to
# itself, would pass everything above. Each control is EXPECTED to be refused.

def test_control_a_missing_query_player_id_is_refused(client: TestClient, player: Dict[str, str]):
    """``/wallet`` REQUIRES ``player_id``. A valid session without it must answer
    422.

    This is what gives section 4 its meaning: a 200 up there can only mean "the
    route read the id we passed", because the same call without it cannot
    succeed. The session is valid on purpose -- without one the route answers 401
    first and the test would pass without ever reaching the parameter check.
    """
    r = client.get("/wallet", headers=player["headers"])
    assert r.status_code == 422, (
        "a query-shaped route answered %d with a valid session and no player_id -- "
        "either the id is not actually required (so section 4 proves nothing) or "
        "the route invented an account: %s" % (r.status_code, r.text))


def test_control_a_forged_token_is_refused(client: TestClient, player: Dict[str, str]):
    """A made-up session must not authenticate a guarded route.

    Probed on the shipping tree: ``/inventory``, ``/sign-in``, ``/daily-boss``,
    ``/beasts`` and ``/missions/list`` all answer 401 to a forged token.
    ``/inventory`` stands in for that family.
    """
    r = client.get("/inventory/%s" % player["id"],
                   headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code in (401, 403), (
        "a forged bearer token was accepted (answered %d): %s"
        % (r.status_code, r.text))


def test_control_an_unknown_account_is_not_served(client: TestClient, player: Dict[str, str]):
    """An id that was never created must not resolve to a real account.

    This is what gives the two-account test its meaning: a store that answered
    200 for anything would pass that one too.
    """
    r = client.get("/player/%s" % uuid.uuid4().hex, headers=player["headers"])
    assert r.status_code == 404, (
        "an unknown account id was served (answered %d): %s" % (r.status_code, r.text))


def test_control_asking_for_someone_elses_account_is_refused(
    client: TestClient, player: Dict[str, str]
):
    """A valid session for account A must not read account B.

    The ownership guard answers 403 here (measured), which is the correct
    refusal and the one that proves the guard is actually mounted.
    """
    other_id = _signup(client)
    r = client.get("/wallet", headers=player["headers"], params={"player_id": other_id})
    assert r.status_code in (401, 403), (
        "account %s read account %s's wallet (answered %d): %s"
        % (player["id"], other_id, r.status_code, r.text))


# --------------------------------------------------------------------------
# 6. OBSERVED, NOT ASSERTED -- a real finding left for the owning team
# --------------------------------------------------------------------------

def test_observation_the_player_read_route_is_not_token_gated(
    client: TestClient, player: Dict[str, str]
):
    """RECORDS a measured fact about the shipping tree. Deliberately asserts
    NOTHING about whether the behaviour is correct.

    MEASURED on this tree: ``GET /player/{id}`` answers 200 with NO session at
    all, and also 200 to a forged token, while every other per-account route
    answers 401. That is a real difference from the rest of the app and it is
    worth a human deciding on, but a boot smoke test is the wrong place to freeze
    a security decision that belongs to the route's owner -- writing an assertion
    either way would make this file the de facto policy.

    The test asserts only that the call COMPLETES, which is still a smoke signal
    (the route did not hang or crash), and the fact itself lives in this
    docstring where a reviewer will see it.

    To turn this into a real gate once the decision is made, change the last two
    asserts to ``r.status_code in (401, 403)``.
    """
    anonymous = client.get("/player/%s" % player["id"])
    forged = client.get("/player/%s" % player["id"],
                        headers={"Authorization": "Bearer not-a-real-token"})
    # Completes without hanging or erroring -- that is all this asserts.
    assert anonymous.status_code < 500, anonymous.text
    assert forged.status_code < 500, forged.text


# --------------------------------------------------------------------------
# 7. THE BOOT STAYS UP ACROSS REPEATED CALLS
# --------------------------------------------------------------------------

def test_repeated_calls_keep_answering(client: TestClient, player: Dict[str, str]):
    """A server that answers once and then dies on the second call is not up.
    Each round signs up a fresh account, which also proves signup is repeatable
    rather than one-shot."""
    for i in range(3):
        assert _signup(client), "round %d: signup stopped working" % i

    for i in range(3):
        r = client.get("/player/%s" % player["id"], headers=player["headers"])
        assert r.status_code == 200, "round %d: %s" % (i, r.text)

    for i in range(3):
        r = client.get("/inventory/%s" % player["id"], headers=player["headers"])
        assert r.status_code == 200, "round %d: %s" % (i, r.text)