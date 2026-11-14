"""
The mint-authority routes must be handed the WALLET store, not the player store.

The defect
----------
``server/main.py`` registers every ``mint_authority`` route with a single
``store_provider``:

    register_mint_authority_routes(app, lambda: players_db)     # main.py:3710

Every handler in that module then calls ``earn(store, ...)``, and
``mint_authority.earn`` hands the store to ``wallet_defense.earn``
(mint_authority.py:574). ``wallet_defense.earn`` is typed
``store: WalletStore`` and its FIRST statement on the store is

    replay = store.recall(idem_key, player_id)                  # wallet_defense.py:490

``PlayerStore`` has no ``recall`` -- it is a player-record store, not a
currency store. So every authorised caller of ``POST /admin/events/create`` and
``POST /purchase/validate`` died with

    AttributeError: 'PlayerStore' object has no attribute 'recall'

which the ASGI layer turns into HTTP 500. That is not a permission answer and
not a missing-route answer: the admin gate lets the request through, the handler
starts, and the payment function explodes.

Why the assertions are CAPABILITY assertions
--------------------------------------------
The bug class here is "the route was handed an object of the wrong KIND".
Naming a class (``type(store) is WalletStore``) would encode today's
implementation, would break the moment a Postgres-backed wallet replaces the
in-memory one, and would pass even if a future store lacked the method the route
actually calls. So every assertion below asks the question the route's own body
asks: *does this object have the things the payment path calls?*

The concrete capability set is read from the source of the payment path, not
guessed: the four ``store.*`` methods ``wallet_defense.earn`` reaches for.

Reverting the fix makes this file RED (measured both ways; the footers are in
the delivery report).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

ADMIN = "operator-token-store-shape"
ADMIN_ENV = "WC_ADMIN_TOKEN"          # identity.py:63
ADMIN_HEADER = "X-Admin-Token"        # identity.py:64

#: A SECOND, INDEPENDENT prerequisite, not part of the defect under audit.
#: ``mint_authority.sign_grant`` refuses to sign when this is unset
#: (mint_authority.py:308-319) and deliberately has no development default. The
#: repo's own mint-authority suite sets it the same way
#: (tests/test_mint_authority.py:41). Setting it here isolates the measurement to
#: the STORE WIRING: with it unset the route still fails, but for a different,
#: already-known reason that would mask the one under test.
SIGNING_ENV = "WC_MINT_SIGNING_KEY"
SIGNING_VALUE = "test-only-mint-signing-key-32-bytes-ok"

#: The methods the payment path calls on the store it is given. Read from
#: ``wallet_defense.earn`` (wallet_defense.py:490-504), not invented here.
WALLET_CAPABILITY = ("recall", "_live_for_update", "bump_version", "seen")


@pytest.fixture(scope="module")
def live_client():
    """The real app, lifespan run.

    ``raise_server_exceptions=False`` so an unhandled server error arrives as an
    observable HTTP 500 RESPONSE instead of being re-raised through the ASGI
    transport. Without it the wrong-store crash would abort the test with an
    ``AttributeError`` traceback that names neither the route nor the assertion
    it was meant to test -- the failure would point at the transport, not at the
    wiring under audit.
    """
    from server.main import app
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="module")
def admin_env():
    """Configure BOTH operator/session prerequisites BEFORE any request.

    ``identity.require_admin`` FAILS CLOSED: with no token configured it answers
    503 for every caller, which would be indistinguishable from a working gate
    if the test never presented one. Setting it here is what makes a 500 mean
    "the payment path crashed" instead of "the gate refused".

    The signing key is set alongside it, and the cache is reset, because
    ``mint_authority`` memoises the key in a module global on first read. Setting
    the variable after another test already read it would be a no-op and the
    route would keep raising the unset-key error -- a stale-cache artefact
    dressed up as a product bug.
    """
    previous = {name: os.environ.get(name)
                for name in (ADMIN_ENV, SIGNING_ENV)}
    os.environ[ADMIN_ENV] = ADMIN
    os.environ[SIGNING_ENV] = SIGNING_VALUE
    import mint_authority
    mint_authority._reset_signing_key_cache()
    yield ADMIN
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    mint_authority._reset_signing_key_cache()


@pytest.fixture(scope="module")
def route_store(live_client):
    """The object the mint-authority routes actually receive at request time.

    Read out of the registered route's dependency rather than re-imported from
    main.py, so this measures what FastAPI really bound -- a call site that
    reads correctly and a dependency that captured something else are different
    states, and only the second one is what ships.
    """
    from fastapi.routing import APIRoute

    for route in live_client.app.routes:
        if isinstance(route, APIRoute) and route.path == "/purchase/validate":
            for dep in route.dependant.dependencies:
                # The dependency FastAPI bound to the handler's `store` parameter
                # is named "store". Selecting by that NAME rather than by calling
                # every dependency matters: the sibling `require_player`
                # dependency needs a live Request, so calling it here would raise
                # TypeError and the test would die on its own instrument instead
                # of measuring the thing under audit.
                if dep.name != "store":
                    continue
                return dep.call()
    pytest.fail("/purchase/validate is not registered at all, or has no "
                "`store` dependency to inspect")


def _admin_headers(idem: str):
    return {"X-Admin-Token": ADMIN, "Idempotency-Key": idem}


def _player_headers(player_id: str):
    """A real session, minted the way the app mints one (auth.issue_session)."""
    import auth
    return {"Authorization": f"Bearer {auth.issue_session(player_id)}",
            "Idempotency-Key": f"store-shape-{player_id}"}


# ---------------------------------------------------------------------------
# 1. CAPABILITY: the store handed to the routes has what earn() calls.
# ---------------------------------------------------------------------------

def test_route_store_has_the_methods_the_payment_path_calls(route_store):
    """The regression assertion for the wiring itself.

    Fails with a named list of the missing capabilities, so the message says
    which method is absent rather than only "assertion failed".
    """
    missing = [cap for cap in WALLET_CAPABILITY if not hasattr(route_store, cap)]
    assert not missing, (
        f"the mint-authority routes are handed {type(route_store).__name__}, "
        f"which lacks the wallet methods the payment path calls: {missing}. "
        f"wallet_defense.earn calls store.recall() at wallet_defense.py:490.")


def test_route_store_is_not_the_player_record_store(live_client, route_store):
    """The two stores must be DIFFERENT objects, not merely compatible ones.

    ``PlayerStore`` happens to expose ``get``/``__contains__`` too, so a
    duck-type-only check can pass on the wrong object. Identity is what
    distinguishes them, and passing the player store where the wallet store
    belongs is the whole defect.
    """
    from server.main import players_db

    assert route_store is not players_db, (
        "the mint-authority routes are handed players_db itself; the payment "
        "path needs the wallet store, which is a different object.")


# ---------------------------------------------------------------------------
# 2. BEHAVIOUR: an authorised caller gets a real answer, not a 500.
# ---------------------------------------------------------------------------

def test_admin_event_create_answers_for_an_authorised_operator(
        live_client, admin_env, route_store):
    """POST /admin/events/create -- the route that was 500 for every operator."""
    player_id = "shape_admin_operator"
    resp = live_client.post(
        "/admin/events/create",
        headers=_admin_headers("store-shape-admin-1"),
        params={"player_id": player_id},
    )
    assert resp.status_code != 500, (
        f"an authorised operator got HTTP 500: {resp.text[:300]}")
    assert resp.status_code == 200, (
        f"expected the grant to succeed, got HTTP {resp.status_code}: "
        f"{resp.text[:300]}")


def test_purchase_validate_answers_for_an_authorised_player(
        live_client, admin_env, route_store):
    """POST /purchase/validate -- the second route broken by the same wiring.

    The buyer is CREATED first, on purpose. This test asserts that the route
    answers 200 for an authorised caller, and "authorised" means the caller's
    account exists: ``auth.authenticate`` confirms the session's player still
    exists and refuses a token whose account was never created. Minting a
    session for a bare id string (which is what this did) is no longer an
    authorised caller, so it answers 401 -- correctly, and for a reason that
    has nothing to do with the store wiring under audit. Seeding the player
    keeps the measurement on the defect: a real caller must get 200 and never
    500 from the wrong-store crash.
    """
    player_id = "shape_purchase_buyer"
    created = live_client.post("/player/create", json={
        "name": player_id, "spirit_root": "fire"})
    assert created.status_code in (200, 201), created.text

    resp = live_client.post(
        "/purchase/validate",
        headers=_player_headers(player_id),
        params={"player_id": player_id},
        json={"receipt_id": "receipt-store-shape"},
    )
    assert resp.status_code != 500, (
        f"a signed-in buyer got HTTP 500: {resp.text[:300]}")
    assert resp.status_code == 200, (
        f"expected the receipt to validate, got HTTP {resp.status_code}: "
        f"{resp.text[:300]}")


def test_the_admin_gate_is_still_the_one_that_answers(live_client, admin_env):
    """The permission check must NOT have been weakened to make the above pass.

    An unauthorised caller -- no header at all -- is still refused, and refused
    by the gate rather than by the crash below it.
    """
    resp = live_client.post(
        "/admin/events/create",
        headers={"Idempotency-Key": "store-shape-unauth"},
        params={"player_id": "shape_unauthorised"},
    )
    assert resp.status_code in (401, 403, 503), (
        f"an unauthenticated caller was NOT refused: HTTP {resp.status_code} "
        f"{resp.text[:200]}")
    assert resp.status_code != 500, (
        "the refusal did not come from the gate -- the payout path ran first "
        "and crashed, which is the pre-fix behaviour")
