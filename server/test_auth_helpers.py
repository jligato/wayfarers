"""Session helpers for tests that drive an authenticated route.

Why this module exists
----------------------
The reward/progression surface in ``server/main.py`` carries
``Depends(require_player)``. A test that calls one of those routes over HTTP
must therefore present a session, or the route answers 401 before the handler
runs. These helpers mint that session the way trusted SERVER code does -- by
calling ``auth.issue_session`` directly.

There is deliberately no ``POST /auth/login``, and there must not be one. This
game has no player passwords or identity provider to check against, so such a
route would accept any ``player_id`` and hand every visitor a session for any
player they name. ``auth.issue_session`` says so in its own docstring. Mint
in-process, as here; never re-add the route to make a test pass.

Why NOT an autouse conftest fixture
-----------------------------------
An autouse fixture that attaches a bearer token to every request would make the
suite structurally unable to observe the unauthenticated case, and
``server/test_security_regression.py`` asserts precisely that an anonymous
caller is refused. A blanket token would turn those assertions into
tautologies and hide the defect this whole change exists to close. These
helpers are therefore opt-in: a test that wants a session asks for one.

Not a conftest
--------------
``server/conftest.py`` shadows the repository-root ``conftest.py`` for anything
collected under ``server/``, so a helper defined there is not importable by
name from ``server/test_*.py``. This is a plain module for that reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# BOTH roots, in the same order the repository-root conftest.py uses them
# (ROOT then SERVER). The repo root alone is not enough: ``auth.py`` lives in
# ``server/``, so a bare ``import auth`` below raises ModuleNotFoundError
# unless ``server/`` is on the path too. Under pytest the root conftest.py
# happens to add it, which is why this went unnoticed; importing this helper
# outside pytest (a probe script, a one-off check) failed on the import.
for _root in (str(_REPO_ROOT), str(_REPO_ROOT / "server")):
    if _root not in sys.path:
        sys.path.insert(0, _root)


def auth_headers(player_id: str, signing_key: str) -> dict:
    """Exchange a signing key for a session token; return Authorization headers.

    ``signing_key`` is retained in the signature for call-site compatibility.
    It is deliberately NOT used: this game has no player passwords, and
    ``auth.issue_session`` documents that mounting a login route would "hand
    every visitor a session for any player they name". There is no
    ``/auth/login`` route, so calling one is what made every session-using
    test fail in the helper rather than in the code under test.

    ``import auth`` (bare, not ``server.auth``) is load-bearing: ``main.py``
    binds the same module object and the session store is a per-module
    singleton, so importing the package-qualified copy would mint tokens into
    a second store and every one of them would 401.
    """
    import auth

    return {"Authorization": f"Bearer {auth.issue_session(player_id)}"}


def create_authed_player(client, name: str = "AuthedTester",
                         spirit_root: str = "fire") -> tuple:
    """Create a player through the API; return ``(player_id, auth_headers)``."""
    r = client.post("/player/create",
                    json={"name": name, "spirit_root": spirit_root})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], auth_headers(body["id"], body["signing_key"])


def authed_player_in_store(players_db, name: str = "StoreTester") -> tuple:
    """Mint a session for a player inserted DIRECTLY into the store.

    For tests that build their own row instead of going through
    ``/player/create``. There is no signing key for such a row, so one is
    registered against the session the same way account creation registers it.
    """
    import secrets

    from server import main as main_module

    player_id = f"authed_{secrets.token_urlsafe(8)}"
    key = secrets.token_bytes(32)
    players_db[player_id] = {
        "id": player_id, "name": name, "cultivation_realm": "mortal",
        "cultivation_stage": "early", "path": "qi", "spirit_root": "fire",
        "progress": 0.0, "spirit_stones": {"low_grade": 1000},
        "atk": 100, "def_": 50, "hp": 1000, "mp": 500, "spd": 100,
        "pvp_rating": 1000, "sect_id": None, "sect_rank": "",
        "sect_contribution": 0, "dao_companion_id": None,
    }
    if hasattr(players_db, "persist"):
        players_db.persist(player_id, players_db[player_id])
    main_module.SESSION_KEYS[player_id] = key
    return player_id, auth_headers(player_id, key.hex())


# ===========================================================================
# The helpers above are a shared library imported by ~43 test files, so a
# silent regression here does not fail one test -- it fails whichever file
# imports it next. These tests pin the contract those callers rely on.
# ===========================================================================


def _resolve(token):
    """Ask the SAME process-wide store the helper minted into."""
    import auth
    return auth.get_session_store().resolve(token)


def _bearer(headers):
    """Pull the raw token back out of the Authorization header."""
    assert list(headers) == ["Authorization"], headers
    scheme, _, token = headers["Authorization"].partition(" ")
    assert scheme == "Bearer", headers["Authorization"]
    return token


def test_auth_headers_returns_a_bearer_authorization_header():
    headers = auth_headers("helper_shape_player", "unused-signing-key")
    assert isinstance(headers, dict)
    assert _bearer(headers)


def test_auth_headers_mints_a_token_the_server_actually_accepts():
    """The token must resolve in the live store, not merely look well formed.

    This is the whole point of the helper: a header that is shaped correctly
    but minted into a different module's session store answers 401 on every
    guarded route, which is the failure this file exists to prevent.
    """
    headers = auth_headers("helper_live_player", "unused-signing-key")
    session = _resolve(_bearer(headers))
    assert session is not None, "the minted token did not resolve in the session store"
    assert session.player_id == "helper_live_player"


def test_auth_headers_ignores_the_signing_key_argument():
    """The key is kept for call-site compatibility and must not gate anything.

    If a future change starts rejecting a caller whose key does not match,
    every test that passes the player's real key from a different row breaks
    for a reason that has nothing to do with the code under test.
    """
    real = auth_headers("helper_key_player", "a" * 64)
    bogus = auth_headers("helper_key_player", "totally-wrong-key")
    assert _resolve(_bearer(real)) is not None
    assert _resolve(_bearer(bogus)) is not None


def test_auth_headers_mints_a_distinct_token_per_call():
    first = _bearer(auth_headers("helper_repeat_player", "k"))
    second = _bearer(auth_headers("helper_repeat_player", "k"))
    assert first != second, "reissuing returned the same token, so it is not a fresh session"


def test_auth_headers_refuses_an_empty_player_id():
    """An empty id must fail loudly here rather than 401 somewhere downstream."""
    for bad in ("", None):
        try:
            auth_headers(bad, "k")
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"auth_headers accepted an unusable player_id: {bad!r}")


def test_create_authed_player_returns_an_id_and_matching_headers(monkeypatch):
    """The returned id and the minted session must describe the SAME player."""
    import auth
    from fastapi.testclient import TestClient
    from server.main import app

    with TestClient(app) as client:
        player_id, headers = create_authed_player(client, name="HelperCreated")

    assert isinstance(player_id, str) and player_id
    session = auth.get_session_store().resolve(_bearer(headers))
    assert session is not None
    assert session.player_id == player_id, (
        "the header points at a different player than the returned id; a caller "
        "using them together would be authorised as somebody else"
    )


def test_authed_player_in_store_mints_a_session_for_the_row_it_inserted():
    """Tests that seed a store row directly rely on this pairing too."""
    from server.main import players_db

    player_id, headers = authed_player_in_store(players_db, name="HelperStored")
    assert player_id in players_db
    session = _resolve(_bearer(headers))
    assert session is not None, "a directly-inserted row got no usable session"
    assert session.player_id == player_id


def test_authed_player_in_store_registers_the_signing_key():
    """The row's signing key must be registered or later verification 500s."""
    from server.main import SESSION_KEYS, players_db

    player_id, _ = authed_player_in_store(players_db, name="HelperKeyed")
    assert player_id in SESSION_KEYS, (
        "SESSION_KEYS has no entry for the seeded player, so a signature check "
        "downstream has nothing to compare against"
    )


def test_a_minted_token_stops_resolving_once_revoked():
    """Revocation must reach tokens this helper minted, not just route ones."""
    import auth

    token = _bearer(auth_headers("helper_revoke_player", "k"))
    store = auth.get_session_store()
    assert store.resolve(token) is not None
    assert store.revoke(token) is True
    assert store.resolve(token) is None
