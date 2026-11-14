"""Contract tests for server/ownership_guard.py.

What this file is for
---------------------
``ownership_guard.py`` is not a route. It is a guard that is installed ONTO an
app, so there is no request/response contract of its own to probe. Its
contract is instead:

* what it MATCHES (a bare ``player_id`` / ``pid`` claim, bare or in a body);
* what it REFUSES (no session -> 401, a session naming someone else -> 403);
* what it deliberately does NOT do (the two exemptions, and the narrowing it
  applies to the one exemption that would otherwise leak a balance);
* what it REPORTS when it does nothing (the skip ledger and the triage
  manifest).

Everything below is asserted on purpose-built miniature FastAPI apps rather
than on the live app, for three reasons:

  1. the guard REWRITES routes in place, so running it over the live app a
     second time mutates shared state that other test files read;
  2. the live route table grows constantly, so a count asserted here would go
     red for a reason that has nothing to do with the guard;
  3. a miniature app can hold exactly the route shape under test, so a passing
     test actually proves the guard fired rather than that some other
     dependency happened to refuse.

ONE EXCEPTION, in section 11: a miniature app cannot answer "does this route
really serve an anonymous caller", so that section boots the LIVE app in a
clean subprocess and measures it. It never calls the installer, so it rewrites
nothing.

The ledger is module-global, so every test that installs restores it.

Reading the numbers: 401 means "no usable session"; 403 means "a valid session,
but it does not name this player". Both are refusals; they are kept apart
because only 403 proves the OWNERSHIP half of the guard ran.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import Body, Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (os.path.join(_REPO, "server"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auth as auth_module  # noqa: E402
import ownership_guard  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_ledger():
    """Restore the module-global skip ledger around every test.

    ``install_ownership_guard`` appends to a module-level list, and other test
    files in this suite read that list against the LIVE app. A test that left
    its miniature app's ledger behind would make their numbers describe the
    wrong route table.
    """
    saved = list(ownership_guard._SKIPPED_ROUTES)
    try:
        yield
    finally:
        ownership_guard._SKIPPED_ROUTES[:] = saved


def _auth(player_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {auth_module.issue_session(player_id)}"}


class Claim(BaseModel):
    """A body-borne player claim.

    Module level, not nested in the builder: this file uses
    ``from __future__ import annotations``, which turns a locally-defined model
    into an unresolvable forward reference and fails Pydantic at route build
    time.
    """

    player_id: str
    event_id: str = "e1"


class _StaticApp(FastAPI):
    """A mounted sub-application, which registers as a non-APIRoute object."""


def _route(app: FastAPI, path: str) -> APIRoute:
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path == path:
            return r
    raise AssertionError(f"no APIRoute at {path!r}")


def _auth_dep_names(route: APIRoute) -> List[str]:
    """Names of the dependency callables attached to a route, for assertions."""
    return [ownership_guard._dep_name(d) for d in route.dependant.dependencies]


def _mini_path_route() -> FastAPI:
    """One route that names the acting player as a bare path parameter."""
    app = FastAPI()

    @app.get("/mini/wallet/{player_id}")
    def get_wallet(player_id: str) -> Dict[str, Any]:
        return {"owner": player_id, "spirit_stones": 10}

    return app


def _mini_query_route() -> FastAPI:
    """One route that names the acting player as a bare query parameter."""
    app = FastAPI()

    @app.get("/mini/quests")
    def get_quests(player_id: str = "") -> Dict[str, Any]:
        return {"owner": player_id}

    return app


def _mini_body_route() -> FastAPI:
    """One route whose player id arrives inside a JSON body model.

    ``POST /events/claim`` is the real instance of this shape: the reward is
    paid to a ``player_id`` the caller supplied in the body, which a path/query
    scan cannot see.
    """

    app = FastAPI()

    @app.post("/mini/claim")
    def claim(payload: Claim = Body(...)) -> Dict[str, Any]:
        return {"paid_to": payload.player_id}

    return app


def _mini_pid_route() -> FastAPI:
    """One lifecycle-style route whose path parameter is named ``pid``."""
    app = FastAPI()

    @app.post("/mini/lifecycle/{pid}/check-in")
    def check_in(pid: str) -> Dict[str, Any]:
        return {"checked_in": pid}

    return app


def _mini_already_guarded_route() -> FastAPI:
    """One route that authenticates itself and must NOT be double-guarded."""
    app = FastAPI()

    @app.get("/mini/selfguarded/{player_id}")
    def self_guarded(player_id: str,
                     identity=Depends(auth_module.require_player)) -> Dict[str, Any]:
        return {"owner": player_id}

    return app


def _mini_bootstrap_app(record: Any) -> FastAPI:
    """The client bootstrap read at the one exempted path."""
    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH)
    def get_player(player_id: str) -> Any:
        return record

    return app


# ---------------------------------------------------------------------------
# 1. the constants the rest of the module is built on
# ---------------------------------------------------------------------------
def test_player_id_params_is_exactly_the_two_documented_names():
    """The claim vocabulary is ``player_id`` and ``pid``, and nothing else.

    A third name would widen what the guard matches on every route; losing one
    would leave a documented lifecycle route unguarded. Both are defects, so
    the set is pinned.
    """
    assert ownership_guard.PLAYER_ID_PARAMS == frozenset({"player_id", "pid"})


def test_a_third_party_parameter_name_is_not_matched():
    """A parameter naming somebody else (``target_id``) must stay unguarded.

    ``target_id`` and ``beast_id`` name the friend or beast being acted on.
    Requiring the caller's session to equal them would break the feature rather
    than protect it.
    """
    app = FastAPI()

    @app.get("/mini/inspect/{target_id}")
    def inspect(target_id: str) -> Dict[str, str]:
        return {"target": target_id}

    route = _route(app, "/mini/inspect/{target_id}")
    assert ownership_guard.player_claim_params(route.dependant) == []


def test_exempt_paths_holds_only_the_two_documented_entries():
    """The exemption list is the fail-closed escape hatch, so it is pinned.

    ``POST /wallet/earn`` is the mint that must stay reachable by its own
    operator token. ``/player/{player_id}`` is NOT in this set: it is handled by
    ``BOOTSTRAP_PATH`` and gets a narrowing wrapper instead, so listing it here
    would silently disable that narrowing.
    """
    assert "/wallet/earn" in ownership_guard.EXEMPT_PATHS
    assert ownership_guard.BOOTSTRAP_PATH not in ownership_guard.EXEMPT_PATHS
    assert ownership_guard.BOOTSTRAP_PATH == "/player/{player_id}"


def test_the_signing_key_route_is_NOT_exempt_because_a_permanent_hole_is_the_wrong_trade():
    """``GET /wallet/signing-key`` was deliberately UN-EXEMPTED. Pin the withdrawal.

    The exemption used to hold that the signing key is the login credential --
    ``POST /auth/login`` mints a session FROM it -- so a player session is
    strictly WEAKER here, and adding ``require_player`` would refuse the only
    caller the route exists for. That argument was sound and it is preserved in
    the module's docstring.

    It was withdrawn anyway (012-211), for a reason that is about who carries
    the risk rather than about which credential is stronger: the handler's own
    operator-token gate is a plain function call inside the handler body, so
    ``has_auth_dependency`` cannot see it. An exemption is therefore a permanent
    hole whose safety is a CLAIM about another file's body, and nothing in this
    module would notice if that body were edited -- the gate deleted, moved
    after a read, or weakened to "unset means allow". On the login credential,
    a permanent hole justified by a claim is the wrong trade.

    So the entry is REMOVED and the route carries ``require_player`` like any
    other route naming a player id. This test pins the CURRENT decision, and it
    is written so that re-adding the path -- a reversible edit the module names
    explicitly -- turns it red rather than passing quietly.
    """
    assert "/wallet/signing-key" not in ownership_guard.EXEMPT_PATHS, (
        "the exemption was withdrawn on 012-211; re-adding this path is a "
        "deliberate reversal and the section-12 contract must be re-argued "
        "with it, not inherited silently")
    # And the one entry that must survive, so this cannot pass by an empty set.
    assert "/wallet/earn" in ownership_guard.EXEMPT_PATHS


def test_bootstrap_fields_are_the_three_the_client_needs():
    """The narrowed anonymous view is exactly three fields.

    ``spirit_root`` must stay because the shipped client refuses a restore whose
    payload lacks it. The currency balance and combat rating must stay out:
    that is the whole point of the narrowing.
    """
    assert ownership_guard.BOOTSTRAP_FIELDS == ("id", "name", "spirit_root")


# ---------------------------------------------------------------------------
# 2. what the guard matches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("builder", [
    _mini_path_route,
    _mini_query_route,
    _mini_body_route,
])
def test_every_claim_carrier_is_matched(builder):
    """A player id is a claim whether it arrives in the path, the query or a body.

    The body case is the one a path/query scan misses by construction, and it
    is a real route (``POST /events/claim``) that pays out to the id the caller
    supplied.
    """
    app = builder()
    matched = [
        r for r in app.routes
        if isinstance(r, APIRoute) and ownership_guard.player_claim_params(r.dependant)
    ]
    assert len(matched) == 1, "the claim carrier was not detected"


def test_body_and_bare_scanners_agree_on_the_same_dependant():
    """``player_claim_params`` is the union of the two scanners.

    Asserted on the return value rather than by re-deriving it, because the
    installer calls only this function and a divergence here would leave the
    body route unguarded with both scanners individually green.
    """
    app = _mini_body_route()
    dependant = _route(app, "/mini/claim").dependant
    assert ownership_guard.bare_player_params(dependant) == []
    assert ownership_guard.body_player_params(dependant) == ["player_id"]
    assert ownership_guard.player_claim_params(dependant) == ["player_id"]


def test_a_route_naming_no_player_id_matches_nothing():
    """A route with no player claim is out of scope -- and is LEDGERED, not silent.

    Out of scope is not the same as safe: these are the blind-spot routes the
    triage manifest exists to adjudicate.
    """
    app = FastAPI()

    @app.get("/mini/config")
    def get_config() -> Dict[str, int]:
        return {"rate": 1}

    app = _with_installed(app)
    assert _route(app, "/mini/config").path == "/mini/config"
    assert ownership_guard.skipped_route_count(
        ownership_guard.SKIP_NO_PLAYER_ID_CLAIM) == 1
    assert ownership_guard.skipped_routes(
        ownership_guard.SKIP_NO_PLAYER_ID_CLAIM)[0].path == "/mini/config"


def test_an_unnamed_dependency_does_not_crash_the_installer():
    """A dependency with no ``__name__`` is treated as non-auth, not an error.

    ``Depends(functools.partial(fn))`` and ``Depends(SomeObject())`` are both
    legal FastAPI and produce a dependency with no name. Reading ``__name__``
    off one raised out of ``has_auth_dependency`` and therefore out of
    ``install_ownership_guard`` -- which main.py calls at import time with no
    try/except, so a single such dependency would stop the server booting.
    """
    def inner() -> str:
        return "x"

    app = FastAPI()

    @app.get("/mini/partial/{player_id}")
    def with_partial(
        player_id: str,
        _dep=Depends(functools.partial(inner)),
    ) -> Dict[str, str]:
        return {"owner": player_id}

    app = _with_installed(app)
    # Unnamed => not recognised as auth by name. The route therefore still had
    # no recognised auth of its own, so the installer guarded it and the guard's
    # own require_player is what now resolves it. Failing loudly here would
    # instead cost the whole guard.
    names = _auth_dep_names(_route(app, "/mini/partial/{player_id}"))
    assert names.count("require_player") == 1, names
    assert ownership_guard.skipped_route_count(
        ownership_guard.SKIP_ALREADY_HAS_AUTH) == 0


def test_a_route_that_authenticates_itself_is_not_double_guarded():
    """A route already carrying ``Depends(require_player)`` is left alone.

    Double-guarding is not merely wasteful: the installer would report the
    route as debt-free bookkeeping it did not do, and the count of guarded
    routes would stop meaning "routes this install changed".
    """
    app = _mini_already_guarded_route()
    guarded = ownership_guard.install_ownership_guard(app)
    assert guarded == {}
    assert _auth_dep_names(_route(app, "/mini/selfguarded/{player_id}")).count(
        "require_player") == 1
    assert ownership_guard.skipped_routes(
        ownership_guard.SKIP_ALREADY_HAS_AUTH)[0].path == "/mini/selfguarded/{player_id}"


def _with_installed(app: FastAPI) -> FastAPI:
    """Run the installer over ``app`` and hand the same object back."""
    ownership_guard.install_ownership_guard(app)
    return app


# ---------------------------------------------------------------------------
# 3. what the guard refuses, at the HTTP boundary
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("builder,method,url,kwargs", [
    (_mini_path_route, "GET", "/mini/wallet/p1", {}),
    (_mini_query_route, "GET", "/mini/quests?player_id=p1", {}),
    (_mini_body_route, "POST", "/mini/claim", {"json": {"player_id": "p1"}}),
])
def test_anonymous_is_refused_401_on_every_claim_carrier(
        builder, method, url, kwargs):
    """No session means no call, on every way a player id can arrive."""
    app = _with_installed(builder())
    client = TestClient(app)
    response = client.request(method, url, **kwargs)
    assert response.status_code == 401, response.text


def test_the_anonymous_refusal_arms_are_not_vacuous():
    """A 401 proves the guard fired only if the same call succeeds when signed.

    Without this, a route broken for every caller (a 500, a 422) would satisfy
    every refusal assertion above for the wrong reason.
    """
    for builder, method, url, kwargs, player in (
        (_mini_path_route, "GET", "/mini/wallet/p1", {}, "p1"),
        (_mini_query_route, "GET", "/mini/quests?player_id=p1", {}, "p1"),
        (_mini_body_route, "POST", "/mini/claim",
         {"json": {"player_id": "p1"}}, "p1"),
    ):
        app = _with_installed(builder())
        client = TestClient(app)
        ok = client.request(method, url, headers=_auth(player), **kwargs)
        assert ok.status_code == 200, (url, ok.status_code, ok.text)


def test_a_session_for_this_player_reaches_the_handler_unchanged():
    """The guard adds a check, not a behaviour change.

    The handler still reads its own ``player_id`` parameter; the dependency's
    return value is discarded on purpose. So the handler must see exactly what
    it saw before the guard was installed.
    """
    app = _with_installed(_mini_path_route())
    client = TestClient(app)
    response = client.get("/mini/wallet/p1", headers=_auth("p1"))
    assert response.status_code == 200, response.text
    assert response.json() == {"owner": "p1", "spirit_stones": 10}


def test_a_session_for_a_different_player_is_refused_403():
    """This is the assertion the whole module exists for.

    A valid session, presented against somebody else's id, must be refused --
    and refused with 403 specifically, because 401 would only prove that
    authentication ran, not that the OWNERSHIP half did.
    """
    app = _with_installed(_mini_path_route())
    client = TestClient(app)
    response = client.get("/mini/wallet/victim", headers=_auth("attacker"))
    assert response.status_code == 403, response.text


def test_an_unknown_or_malformed_token_is_refused_401():
    """An unreadable credential never widens what the caller sees.

    ``authenticate`` fails closed on a token it cannot resolve, so the guard's
    only possible answer here is a refusal.
    """
    app = _with_installed(_mini_path_route())
    client = TestClient(app)
    for header in (
        {"Authorization": "Bearer not-a-real-token"},
        {"Authorization": "Basic not-a-real-token"},
        {"Authorization": "Bearer"},
    ):
        response = client.get("/mini/wallet/p1", headers=header)
        assert response.status_code == 401, (header, response.status_code)


def test_a_body_claim_naming_another_player_is_refused_403():
    """The body-borne claim is checked, not just the path-borne one.

    This is the ``POST /events/claim`` shape: the reward is paid to whatever
    ``player_id`` the body carries, so skipping the body would let a caller pay
    a reward to an account they do not own.
    """
    app = _with_installed(_mini_body_route())
    client = TestClient(app)
    response = client.post(
        "/mini/claim", json={"player_id": "victim"}, headers=_auth("attacker"))
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# 4. the ``pid`` routes
# ---------------------------------------------------------------------------
def test_a_pid_route_is_guarded_with_the_pid_shim_not_the_plain_guard():
    """``pid`` gets its own dependency, and it must be the normalising one.

    ``enforce_ownership`` reads the literal field ``player_id``. A session on a
    lifecycle route whose path parameter is ``pid`` would otherwise present no
    claim at all, and the check would pass vacuously.
    """
    app = _with_installed(_mini_pid_route())
    names = _auth_dep_names(_route(app, "/mini/lifecycle/{pid}/check-in"))
    assert "require_player_pid_claim" in names
    assert "require_player" not in names


def test_a_pid_route_refuses_another_players_pid_with_403():
    """The lifecycle cross-player read is closed, which the plain guard would not do."""
    app = _with_installed(_mini_pid_route())
    client = TestClient(app)
    assert client.post(
        "/mini/lifecycle/victim/check-in", headers=_auth("attacker")
    ).status_code == 403


def test_a_pid_route_refuses_an_anonymous_caller_with_401():
    """The shim still authenticates; it only adds a claim normalisation."""
    app = _with_installed(_mini_pid_route())
    client = TestClient(app)
    assert client.post("/mini/lifecycle/p1/check-in").status_code == 401


def test_a_pid_route_allows_the_owner_through():
    """The owner's own lifecycle call is untouched."""
    app = _with_installed(_mini_pid_route())
    client = TestClient(app)
    response = client.post("/mini/lifecycle/p1/check-in", headers=_auth("p1"))
    assert response.status_code == 200, response.text
    assert response.json() == {"checked_in": "p1"}


def _mini_pid_query_route() -> FastAPI:
    """One route whose ``pid`` arrives in the QUERY string, not the path.

    This is the case that separates the shim from the plain guard. ``auth``'s
    claim collection walks ``OWNERSHIP_FIELDS`` (which includes ``pid``) for
    PATH parameters but reads only the literal ``player_id`` for the query
    string. So on a query-borne ``pid`` the plain guard collects no claim and
    the ownership check passes vacuously -- this route is why the shim exists,
    and it is asserted separately from the path case on purpose.
    """
    app = FastAPI()

    @app.post("/mini/lifecycle-query")
    def check_in(pid: str = "") -> Dict[str, Any]:
        return {"checked_in": pid}

    return app


def test_a_query_borne_pid_is_refused_403_which_the_plain_guard_misses():
    """The shim's unique job: normalise a QUERY-borne ``pid`` into a claim.

    With the plain guard attached instead, ``auth`` finds no path param and no
    ``player_id`` query key, so ``enforce_ownership`` has nothing to compare and
    the cross-player call succeeds. This test is the one that fails if the
    installer ever routes a ``pid`` route to ``require_player`` instead of the
    shim.
    """
    app = _with_installed(_mini_pid_query_route())
    client = TestClient(app)
    response = client.post("/mini/lifecycle-query?pid=victim",
                           headers=_auth("attacker"))
    assert response.status_code == 403, response.text


def test_the_query_borne_pid_owner_is_still_allowed_through():
    """The normalisation must not refuse the caller it is protecting."""
    app = _with_installed(_mini_pid_query_route())
    client = TestClient(app)
    response = client.post("/mini/lifecycle-query?pid=p1", headers=_auth("p1"))
    assert response.status_code == 200, response.text
    assert response.json() == {"checked_in": "p1"}


def test_a_form_encoded_body_cannot_smuggle_a_player_id_past_the_shim():
    """A non-JSON body is still read for claims.

    The shim previously parsed the body with a bare ``try``/``except`` around
    ``request.json()``. A form-encoded body is not JSON, so the parse raised,
    the handler swallowed it, and a smuggled ``player_id`` was invisible to the
    ownership check. The shim now delegates to the shared body reader, so a
    form claim is compared like any other.
    """
    app = _with_installed(_mini_pid_route())
    client = TestClient(app)
    response = client.post(
        "/mini/lifecycle/p1/check-in",
        data={"player_id": "victim"},
        headers={**_auth("p1"),
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403, response.text


def test_an_unverifiable_body_is_refused_rather_than_ignored():
    """A body that cannot be inspected is a refusal, not an empty claim set.

    auth's rule is that an unreadable body is not evidence of good faith. A
    multipart body is deliberately never parsed, so it lands here.
    """
    app = _with_installed(_mini_pid_route())
    client = TestClient(app)
    response = client.post(
        "/mini/lifecycle/p1/check-in",
        files={"anything": ("a.txt", b"data")},
        headers=_auth("p1"),
    )
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# 5. the bootstrap exemption: reachable, but narrowed
# ---------------------------------------------------------------------------
_FULL_RECORD = {
    "id": "p1",
    "name": "Traveler",
    "spirit_root": "metal",
    "spirit_stones": 9999,
    "cultivation_realm": 3,
    "pvp_rating": 1800,
    "atk": 40,
    "signing_key": "should-never-be-returned",
}


def test_the_bootstrap_read_stays_reachable_without_a_session():
    """Exempt means reachable. A 401 here boots the shipped client into a dead end."""
    app = _with_installed(_mini_bootstrap_app(dict(_FULL_RECORD)))
    client = TestClient(app)
    assert client.get("/player/p1").status_code == 200


def test_an_anonymous_bootstrap_read_sees_only_the_three_bootstrap_fields():
    """The whole point of the exemption: the balance and the rating are withheld."""
    app = _with_installed(_mini_bootstrap_app(dict(_FULL_RECORD)))
    client = TestClient(app)
    payload = client.get("/player/p1").json()
    assert set(payload) == set(ownership_guard.BOOTSTRAP_FIELDS)
    assert "spirit_stones" not in payload
    assert "pvp_rating" not in payload
    assert "signing_key" not in payload


def test_the_owner_still_receives_the_untouched_full_record():
    """The client restore path must be byte-for-byte what it was.

    A caller that can prove it is this player is not filtered at all, so the
    save-restore contract is unchanged by the guard.
    """
    app = _with_installed(_mini_bootstrap_app(dict(_FULL_RECORD)))
    client = TestClient(app)
    payload = client.get("/player/p1", headers=_auth("p1")).json()
    assert payload == _FULL_RECORD


def test_a_session_for_a_different_player_gets_the_narrow_view_not_a_403():
    """Cross-player bootstrap reads learn no more than anonymous ones do.

    Not a 403, because a 403 would be a behaviour change on the one route the
    exemption exists to keep reachable.
    """
    app = _with_installed(_mini_bootstrap_app(dict(_FULL_RECORD)))
    client = TestClient(app)
    response = client.get("/player/p1", headers=_auth("somebody_else"))
    assert response.status_code == 200
    assert set(response.json()) == set(ownership_guard.BOOTSTRAP_FIELDS)


def test_an_invalid_token_is_treated_as_anonymous_not_as_an_error():
    """An unreadable credential must never WIDEN what the caller sees.

    The fail-closed direction here is withholding fields, so a malformed token
    answers 200 with the narrow view rather than an error that a client might
    be tempted to retry with more privilege.
    """
    app = _with_installed(_mini_bootstrap_app(dict(_FULL_RECORD)))
    client = TestClient(app)
    response = client.get("/player/p1", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 200
    assert set(response.json()) == set(ownership_guard.BOOTSTRAP_FIELDS)


def test_the_narrowing_wrapper_is_idempotent():
    """Installing twice must not double-wrap and start dropping the full record.

    The wrapper carries a private marker for exactly this; without it the
    second install would wrap the wrapper, and the owner check would compare
    against a closure that already narrows.
    """
    app = _mini_bootstrap_app(dict(_FULL_RECORD))
    ownership_guard.install_ownership_guard(app)
    route = _route(app, ownership_guard.BOOTSTRAP_PATH)
    first_endpoint = route.endpoint
    ownership_guard.install_bootstrap_narrowing(route)
    assert route.endpoint is first_endpoint


@pytest.mark.parametrize("record,expected_keys", [
    ({"id": "p1", "name": "N", "spirit_root": "wood", "x": 1}, {"id", "name", "spirit_root"}),
    ([{"id": "p1", "name": "N", "spirit_root": "wood", "spirit_stones": 5}],
     {"id", "name", "spirit_root"}),
])
def test_bootstrap_view_projects_every_mapping_shape(record, expected_keys):
    """A handler returning a list still narrows instead of leaking.

    Measured against a list-returning handler, the earlier "return the record
    unchanged if it is not a mapping" revision handed back every element of the
    list, balance and signing key included.
    """
    app = _with_installed(_mini_bootstrap_app(record))
    client = TestClient(app)
    payload = client.get("/player/p1").json()
    first = payload[0] if isinstance(payload, list) else payload
    assert set(first) == expected_keys
    assert "spirit_stones" not in first


def test_bootstrap_view_discloses_nothing_for_an_unrecognised_shape():
    """An unknown shape returns nothing rather than everything.

    The caller uses this function to decide what to WITHHOLD, so "we did not
    narrow it" is the wrong direction: an unrecognised shape is exactly the
    case where withholding must win.
    """
    assert ownership_guard._bootstrap_view("a string") is None
    assert ownership_guard._bootstrap_view(12345) is None
    assert ownership_guard._bootstrap_view(None) is None


def test_bootstrap_view_drops_fields_the_record_does_not_carry():
    """Projection is an intersection, so a sparse record cannot invent a field."""
    assert ownership_guard._bootstrap_view({"id": "p1"}) == {"id": "p1"}


def test_the_wrapper_carries_the_original_parameters_and_one_request():
    """The wrapper presents the original handler's parameters, plus ``Request``.

    FastAPI resolves a route's parameters from the endpoint it is given, so a
    wrapper built as ``(*args, **kwargs)`` would make FastAPI demand query
    fields called "args" and "kwargs". That is the measured 422 this code
    exists to avoid, and it is asserted here on the rebuilt signature.

    NOTE, deliberately not asserted: a handler that DECLARES its own
    ``request`` parameter is a shape the wrapper does not support -- the
    wrapper consumes ``request`` itself and does not forward it to the original
    handler, so such a handler would raise a missing-argument error. The
    shipped ``get_player`` takes only ``player_id`` and is unaffected. Pinning
    the unsupported shape here would encode a defect as a requirement.
    """
    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH)
    def get_player(player_id: str, extra: int = 3) -> Any:
        return {"id": player_id, "name": "N", "spirit_root": "wood",
                "extra": extra, "spirit_stones": 42}

    app = _with_installed(app)
    params = inspect.signature(route_endpoint(app)).parameters
    # Names and defaults are what FastAPI resolves against. The copied
    # parameters keep this file's postponed-evaluation STRING annotations; the
    # wrapper's own ``request`` parameter is a real class, because it is built
    # by inspect.Parameter rather than carried across from the handler.
    assert "player_id" in params and "extra" in params
    assert params["player_id"].annotation == "str"
    assert params["extra"].default == 3
    assert params["request"].annotation is Request
    assert list(params).count("request") == 1, list(params)

    # And the narrowing contract still holds through the rebuilt signature:
    # the owner's extra fields arrive, an anonymous caller does not see them.
    client = TestClient(app)
    assert client.get("/player/p1", headers=_auth("p1")).json()["extra"] == 3
    assert set(client.get("/player/p1").json()) == set(
        ownership_guard.BOOTSTRAP_FIELDS)


def route_endpoint(app: FastAPI):
    """The endpoint currently attached to the bootstrap route."""
    return _route(app, ownership_guard.BOOTSTRAP_PATH).endpoint


def test_caller_is_player_is_total_and_never_raises():
    """Every "cannot tell" answers False.

    The caller's only use is deciding what to WITHHOLD, so an exception here
    would turn a narrowing decision into a 500. The four unreadable-credential
    shapes below all raise on attribute access, which is what a malformed
    request object looks like from inside ``authenticate``.
    """
    class _Unreadable:
        def __getattr__(self, _name: str) -> Any:
            raise RuntimeError("unreadable credential")

    assert ownership_guard.caller_is_player(_Unreadable(), "p1") is False


def test_caller_is_player_answers_false_for_a_mismatched_or_missing_id():
    """A readable session still answers False unless it names THIS player.

    Asserted through a live request so the session path is the real one, not a
    stub: the identity is minted by ``auth.issue_session`` and read by
    ``auth.authenticate``.
    """
    probe = FastAPI()

    @probe.get("/mini/probe/{player_id}")
    def do_probe(request: Request, player_id: str) -> Dict[str, bool]:
        return {"same": ownership_guard.caller_is_player(request, player_id)}

    client = TestClient(probe)
    assert client.get("/mini/probe/p1", headers=_auth("p1")).json() == {"same": True}
    assert client.get("/mini/probe/p1", headers=_auth("other")).json() == {"same": False}
    assert client.get("/mini/probe/p1").json() == {"same": False}


# ---------------------------------------------------------------------------
# 6. the installer's own return contract
# ---------------------------------------------------------------------------
def test_the_installer_returns_the_routes_it_changed_with_their_claims():
    """The return value is ``{path: [claim fields guarded]}``.

    Asserted on the RETURN VALUE rather than on the dependant tree, because a
    mapping that listed routes it did not change would make the census in
    ``test_player_ownership_guard.py`` meaningless.
    """
    app = _mini_path_route()
    guarded = ownership_guard.install_ownership_guard(app)
    assert guarded == {"/mini/wallet/{player_id}": ["player_id"]}


def test_the_installer_is_idempotent():
    """A second install guards nothing new and changes nothing.

    main.py calls the installer at import time, so "safe from more than one
    mount point" is a load-bearing promise, not a nicety.
    """
    app = _mini_path_route()
    first = ownership_guard.install_ownership_guard(app)
    second = ownership_guard.install_ownership_guard(app)
    assert first and not second
    assert _auth_dep_names(_route(app, "/mini/wallet/{player_id}")).count(
        "require_player") == 1


def test_every_route_in_the_app_is_either_guarded_or_ledgered():
    """No route is silently passed over.

    The bare ``continue`` this ledger replaced is how a route read every
    player's identity for anonymous callers with no record anywhere. The
    invariant: the guarded set and the ledger account for every route.
    """
    app = FastAPI()

    @app.get("/mini/config")
    def get_config() -> Dict[str, int]:
        return {"rate": 1}

    @app.get("/mini/wallet/{player_id}")
    def get_wallet(player_id: str) -> Dict[str, str]:
        return {"owner": player_id}

    guarded = ownership_guard.install_ownership_guard(app)
    # The ledger must account for EVERY route object, including the four
    # documentation routes FastAPI registers by default -- which is why this
    # compares against app.routes rather than a hand-written list.
    assert len(guarded) + ownership_guard.skipped_route_count() == len(app.routes)
    assert set(guarded) == {"/mini/wallet/{player_id}"}


def test_a_non_api_route_is_recorded_as_not_an_api_route():
    """A plain Starlette route has no dependant to inspect, and says so.

    The reason exists so that "nothing to do here" and "nothing was looked at"
    are distinguishable in the log.
    """
    app = FastAPI()
    app.mount("/mini/static", _StaticApp())

    ownership_guard.install_ownership_guard(app)
    entries = ownership_guard.skipped_routes(ownership_guard.SKIP_NOT_AN_API_ROUTE)
    assert entries, "a mounted sub-application was passed over without a record"


def test_the_exempt_mint_route_is_ledgered_as_an_exemption():
    """``POST /wallet/earn`` is skipped for a NAMED reason, not silently."""
    app = FastAPI()

    @app.post("/wallet/earn")
    def earn(player_id: str = "") -> Dict[str, int]:
        return {"minted": 1}

    guarded = ownership_guard.install_ownership_guard(app)
    assert "/wallet/earn" not in guarded
    assert any(
        e.path == "/wallet/earn" and e.reason == ownership_guard.SKIP_EXEMPT_PATH
        for e in ownership_guard.skipped_routes())


def test_the_bootstrap_route_is_ledgered_as_the_narrowed_exemption():
    """Its reason code is its own, distinct from a plain exemption.

    An operator reading the boot log can therefore tell "documented exemption,
    safe as written" from "exemption plus a response filter".
    """
    app = _mini_bootstrap_app(dict(_FULL_RECORD))
    ownership_guard.install_ownership_guard(app)
    entries = ownership_guard.skipped_routes(
        ownership_guard.SKIP_EXEMPT_BOOTSTRAP_PATH)
    assert len(entries) == 1
    assert entries[0].path == ownership_guard.BOOTSTRAP_PATH


def test_every_skip_reason_is_in_the_published_ordering():
    """The reason codes are a fixed vocabulary, evaluated in a fixed order.

    Renaming one is a visible act rather than a silent edit, which is why they
    are constants and why this test pins both the membership and the order.
    """
    assert ownership_guard.SKIP_REASONS == (
        "not-an-api-route",
        "exempt-bootstrap-path-narrowed",
        "exempt-path",
        "no-player-id-claim",
        "already-has-auth-dependency",
    )
    assert len(set(ownership_guard.SKIP_REASONS)) == len(
        ownership_guard.SKIP_REASONS)


def test_the_ledger_counts_reconcile_with_each_other():
    """The three read surfaces describe the same install.

    A caller comparing ``skipped_route_count()`` against the summary dict, or
    against ``skipped_routes()``, must not get three different answers.
    """
    app = FastAPI()

    @app.get("/mini/config")
    def get_config() -> Dict[str, int]:
        return {"rate": 1}

    @app.get("/mini/wallet/{player_id}")
    def get_wallet(player_id: str) -> Dict[str, str]:
        return {"owner": player_id}

    ownership_guard.install_ownership_guard(app)
    summary = ownership_guard.skipped_route_summary()
    assert sum(summary.values()) == ownership_guard.skipped_route_count()
    assert all(
        summary[reason] == ownership_guard.skipped_route_count(reason)
        for reason in summary
    )
    for reason, count in summary.items():
        assert count > 0, "a zero-count reason must be omitted, not listed"


def test_only_the_no_player_id_claim_reasons_count_as_debt():
    """Debt is the blind spot, and it is exactly one reason.

    Benign skips (Starlette route objects, documented exemptions,
    already-guarded routes) are excluded by construction, so a clean install
    reads 0 rather than a number nobody can act on.
    """
    assert ownership_guard.SKIP_DEBT_REASONS == frozenset(
        {ownership_guard.SKIP_NO_PLAYER_ID_CLAIM})
    assert ownership_guard.VERDICT_UNTRIAGED == "untriaged"
    assert ownership_guard.VERDICT_NOT_APPLICABLE == "not-applicable"


def test_skipped_routes_returns_a_snapshot_a_later_install_cannot_disturb():
    """A held tuple must not grow when the installer appends again.

    The ledger is a module-global list that the installer appends to, so a
    caller iterating the tuple it was handed must not see rows appear under
    them mid-iteration.
    """
    app = FastAPI()

    @app.get("/mini/config")
    def get_config() -> Dict[str, int]:
        return {"rate": 1}

    ownership_guard.install_ownership_guard(app)
    held = ownership_guard.skipped_routes()
    before = len(held)
    assert before > 0

    # ``install_ownership_guard`` clears the ledger on every run, so growth is
    # driven here directly to isolate the snapshot property from the reset.
    ownership_guard._SKIPPED_ROUTES.append(("GET", "/mini/later", "no-player-id-claim"))
    try:
        assert len(held) == before, "the snapshot the caller holds was mutated"
        assert len(ownership_guard.skipped_routes()) == before + 1
    finally:
        ownership_guard._SKIPPED_ROUTES.pop()


# ---------------------------------------------------------------------------
# 7. the log severity contract
# ---------------------------------------------------------------------------
def test_skip_level_follows_decision_state_not_reason():
    """The severity split, pinned on the return value.

    A caplog-based test cannot prove this: forcing INFO captures everything, so
    it shows the lines EXIST but says nothing about whether a never-reviewed
    route is louder than a reviewed one.
    """
    level = ownership_guard._skip_log_level
    assert level(ownership_guard.SKIP_NO_PLAYER_ID_CLAIM,
                 ownership_guard.TRIAGE_DISCLOSES) == logging.ERROR
    assert level(ownership_guard.SKIP_NO_PLAYER_ID_CLAIM,
                 ownership_guard.VERDICT_UNTRIAGED) == logging.WARNING
    assert level(ownership_guard.SKIP_NO_PLAYER_ID_CLAIM, None) == logging.WARNING
    assert level(ownership_guard.SKIP_NO_PLAYER_ID_CLAIM,
                 ownership_guard.TRIAGE_SELF_GUARDED) == logging.INFO
    # Non-debt reasons are the guard working, and stay quiet either way.
    for reason in ownership_guard.SKIP_REASONS:
        if reason in ownership_guard.SKIP_DEBT_REASONS:
            continue
        assert level(reason) == logging.INFO
        assert level(reason, ownership_authority_discloses()) == logging.INFO


def ownership_authority_discloses() -> str:
    """The disclosure verdict, read off the module so a rename is visible."""
    return ownership_guard.TRIAGE_DISCLOSES


def test_a_disclosure_outranks_an_untriaged_route_which_outranks_a_ruled_one():
    """The ordering ERROR > WARNING > INFO is the whole point of the split."""
    level = ownership_guard._skip_log_level
    debt = ownership_guard.SKIP_NO_PLAYER_ID_CLAIM
    ordered = [
        level(debt, ownership_guard.TRIAGE_DISCLOSES),
        level(debt, ownership_guard.VERDICT_UNTRIAGED),
        level(debt, ownership_guard.TRIAGE_PUBLIC),
    ]
    assert ordered == sorted(ordered, reverse=True)
    assert len(set(ordered)) == 3


# ---------------------------------------------------------------------------
# 8. the triage manifest
# ---------------------------------------------------------------------------
def test_every_manifest_verdict_is_in_the_published_vocabulary():
    """A typo in a verdict string would silently become a fifth category.

    Nothing downstream filters on the vocabulary -- the log level map, the
    summary counts and the untriaged set all key off the same four names -- so
    a typo would be invisible rather than loud.
    """
    for path, (verdict, reason) in ownership_guard.TRIAGE_MANIFEST.items():
        assert verdict in ownership_guard.TRIAGE_VERDICTS, path
        assert reason.strip(), f"{path} has a verdict with no reason recorded"


def test_every_published_verdict_has_a_meaning_and_a_summary_reader():
    """The vocabulary and its explanations must not drift apart."""
    assert set(ownership_guard.TRIAGE_VERDICT_MEANING) == set(
        ownership_guard.TRIAGE_VERDICTS)
    for verdict in ownership_guard.TRIAGE_VERDICTS:
        assert ownership_guard.TRIAGE_VERDICT_MEANING[verdict].strip()


def test_a_manifest_row_is_readable_and_an_unreviewed_path_is_not():
    """``None`` is the interesting answer: in the blind spot, never read.

    ``/sect/list`` is the sample row, and what is asserted about it is that it
    READS BACK -- a named verdict from the published vocabulary plus a written
    reason. It is deliberately NOT asserted that the verdict is any one
    particular value.

    A row's verdict is a DECISION about the live route, and the decision moves
    when the route changes: this row was written ``discloses-player-content``
    while the handler was open, and becomes ``self-guarded`` once the handler
    declares ``Depends(require_player)``. Pinning the literal here made a fixed
    defect permanent -- the module owner documented the row as STALE precisely
    because this assertion would not let it be corrected, which is a frozen
    finding rather than a test.

    Truth is not given up, it moves: section 11 boots the live app and holds
    every row to what the route actually answers, so ``self-guarded`` must
    measurably produce 401/403. This test keeps the weaker, stable half -- the
    row is present, well-formed and carries its reason.
    """
    reviewed = ownership_guard.triage_verdict("/sect/list")
    assert reviewed is not None
    assert reviewed.path == "/sect/list"
    assert reviewed.verdict in ownership_guard.TRIAGE_VERDICTS
    assert reviewed.reason.strip(), "a readable row must carry its reason"
    assert ownership_guard.triage_verdict("/mini/not-a-real-route") is None


def test_triaged_routes_filters_by_verdict():
    """Filtering returns a subset, split cleanly, and never loses a row.

    The filter is checked against a verdict that ACTUALLY has rows, read off
    the manifest rather than named here. Naming ``TRIAGE_DISCLOSES`` and
    asserting it is non-empty made this test depend on a disclosure existing:
    it was the only ``discloses-player-content`` row in the table, so the day
    that one route was properly guarded the assertion went red for having no
    defects left -- a test that rewards a leak being closed.

    Three properties are asserted instead, and all three hold whatever the
    manifest says:

      * the filter is not vacuous -- the chosen verdict really has rows, and
        every returned row carries exactly that verdict;
      * the filtered set is a SUBSET of the unfiltered set (filtering loses
        nothing and invents nothing);
      * the union over every published verdict rebuilds the whole table, so a
        row cannot be filtered into oblivion by holding a misspelled verdict.

    The last one is the load-bearing repair. It also fails if a verdict string
    outside the published vocabulary ever appears in the table.
    """
    manifest = ownership_guard.TRIAGE_MANIFEST
    verdicts = sorted({verdict for verdict, _reason in manifest.values()})
    assert verdicts, "the manifest is empty, so the filter is untested"
    for verdict in verdicts:
        assert verdict in ownership_guard.TRIAGE_VERDICTS, (
            f"the table holds a verdict outside the published vocabulary: "
            f"{verdict!r}")

    chosen = verdicts[0]
    rows = ownership_guard.triaged_routes(chosen)
    assert rows, f"no manifest row carries {chosen!r}, so filtering proves nothing"
    assert all(v.verdict == chosen for v in rows)
    assert {v.path for v in rows} == {
        path for path, (verdict, _r) in manifest.items() if verdict == chosen}
    assert set(ownership_guard.triaged_routes()) >= set(rows)

    # The filter partitions the table: no row is dropped by holding one verdict.
    rebuilt = {
        v.path
        for verdict in ownership_guard.TRIAGE_VERDICTS
        for v in ownership_guard.triaged_routes(verdict)
    }
    assert rebuilt == set(manifest), (
        "filtering by verdict does not rebuild the whole manifest; a row is "
        "being lost")


def test_the_summary_counts_every_manifest_row_exactly_once():
    """``triage_summary`` must account for the whole manifest.

    A row that silently failed to count would make the boot summary's
    "outstanding work" number wrong, which is the number an operator acts on.
    """
    summary = ownership_guard.triage_summary()
    assert sum(summary.values()) == len(ownership_guard.TRIAGE_MANIFEST)
    for verdict in summary:
        assert verdict in ownership_guard.TRIAGE_VERDICTS


def test_a_route_the_manifest_does_not_cover_surfaces_as_the_review_backlog():
    """The ratchet: a blind-spot route nobody has read is named, not counted.

    A count is not actionable -- a reader cannot open "28" as a file -- so this
    function returns the paths themselves.
    """
    app = FastAPI()

    @app.get("/mini/brand-new-route")
    def brand_new() -> Dict[str, int]:
        return {"n": 1}

    ownership_guard.install_ownership_guard(app)
    assert "/mini/brand-new-route" in ownership_guard.untriaged_skipped_routes()


def test_a_manifest_row_that_is_still_in_the_blind_spot_is_not_reported_missing():
    """The two directions of the ratchet are kept apart.

    ``untriaged_skipped_routes`` is a row that is MISSING from the manifest;
    ``manifest_paths_missing_from_app`` is a row whose route has left the blind
    spot. Conflating them would hide the review backlog.
    """
    app = FastAPI()

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    ownership_guard.install_ownership_guard(app)
    assert "/health" not in ownership_guard.untriaged_skipped_routes()
    assert "/health" not in ownership_guard.manifest_paths_missing_from_app()


# ---------------------------------------------------------------------------
# 9. the wrapper rebuild -- the declared-dependency loss
#
# ``install_bootstrap_narrowing`` replaces the route's endpoint, and FastAPI
# builds the dependency tree from the endpoint AT CONSTRUCTION TIME. So
# rebuilding after wrapping reads only the wrapper's own signature and erases
# every dependency that was declared somewhere else:
#
#   * ``@app.get(..., dependencies=[Depends(x)])`` -- a decorator argument;
#   * ``APIRouter(dependencies=[...])`` -- router-level.
#
# The module's own docstring calls this "the same failure shape as an
# unauthenticated payout": the route still READS as guarded, because the
# dependency is visible in the source, while nothing in the request path ever
# calls it. None of the sections above installs a declared dependency on the
# bootstrap route, so the rebuild's carry-over is the largest load-bearing
# behaviour in this file with no test on it.
# ---------------------------------------------------------------------------
def test_a_dependency_declared_on_the_route_still_runs_after_the_rebuild():
    """The declared dependency is CARRIED onto the rebuilt tree, not dropped.

    Asserted by what the dependency DID, not by what the tree looks like: a
    tree-shape assertion would still pass on a dependant that carries the
    dependency object but never invokes it.
    """
    ran: List[str] = []

    def declared_dep() -> None:
        ran.append("ran")

    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH,
             dependencies=[Depends(declared_dep)])
    def get_player(player_id: str) -> Dict[str, Any]:
        return {"id": player_id, "name": "N", "spirit_root": "wood",
                "spirit_stones": 42}

    app = _with_installed(app)
    client = TestClient(app)
    response = client.get("/player/p1")

    assert response.status_code == 200, response.text
    assert ran == ["ran"], (
        "a dependency declared on the route stopped running once the bootstrap "
        "wrapper replaced the endpoint")
    # And the narrowing still holds on the same request, so the carried
    # dependency did not come back at the cost of the response filter.
    assert set(response.json()) == set(ownership_guard.BOOTSTRAP_FIELDS)


def test_a_signature_derived_dependency_is_carried_once_not_twice():
    """The rebuilt tree keeps ONE copy of a signature-derived dependency.

    ``get_dependant`` re-derives the dependencies that are already implied by
    the wrapper's signature, and the carry-over then offers the route's
    PREVIOUS copy of the same dependency. Re-adding it would leave two entries
    for one declared dependency.

    This is asserted on the rebuilt tree as well as on the call count, and the
    tree half is the load-bearing one: FastAPI's per-request dependency cache
    is keyed on the dependency, so two entries for one callable are collapsed
    before the handler runs. Asserting only "it ran once" therefore passes even
    with the carry-over duplicated, which is a green test proving nothing. The
    duplicate is still a defect -- it is what a paying or rate-limiting
    dependency would be charged against once the cache key ever differs.
    """
    def counted() -> str:
        return "v"

    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH)
    def get_player(player_id: str, _dep=Depends(counted)) -> Dict[str, Any]:
        return {"id": player_id, "name": "N", "spirit_root": "wood"}

    app = _with_installed(app)
    route = _route(app, ownership_guard.BOOTSTRAP_PATH)
    copies = [
        d for d in (getattr(route.dependant, "dependencies", None) or ())
        if ownership_guard._dep_name(d) == "counted"
    ]
    assert len(copies) == 1, (
        f"the rebuilt tree holds {len(copies)} copies of one dependency")

    # And the observable half: the owner still gets the full record, so the
    # surviving copy is the working one rather than a broken duplicate.
    client = TestClient(app)
    assert client.get("/player/p1", headers=_auth("p1")).json()["spirit_root"] == "wood"


def test_an_async_handler_is_wrapped_by_an_async_wrapper():
    """An ``async def`` handler keeps an async wrapper, so the record is awaited.

    The narrowing picks its wrapper with ``_is_async_callable``. Getting that
    wrong is silent: a SYNC wrapper would call the async original and get an
    un-awaited coroutine back, which is not a mapping, so the fail-closed path
    would narrow it to ``None`` and the route would answer 200 with ``null`` --
    a signed-in owner losing their whole record with no error anywhere.
    """
    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH)
    async def get_player(player_id: str) -> Dict[str, Any]:
        return {"id": player_id, "name": "N", "spirit_root": "wood",
                "cultivation_realm": 3}

    app = _with_installed(app)
    endpoint = route_endpoint(app)
    assert inspect.iscoroutinefunction(endpoint), (
        "an async handler was wrapped by a sync wrapper")

    client = TestClient(app)
    # Owner: the coroutine was awaited, so the full record is intact.
    owner = client.get("/player/p1", headers=_auth("p1"))
    assert owner.status_code == 200, owner.text
    assert owner.json()["cultivation_realm"] == 3
    # Anonymous: narrowed, and still a mapping rather than a null.
    anonymous = client.get("/player/p1")
    assert anonymous.status_code == 200, anonymous.text
    assert anonymous.json() == {"id": "p1", "name": "N", "spirit_root": "wood"}


def test_a_sync_handler_is_wrapped_by_a_sync_wrapper():
    """The mirror of the async case, so the two cannot be transposed.

    Asserted together with the async case because the failure mode is the same
    shape in reverse: a wrapper running an awaitable in the wrong place.
    """
    app = FastAPI()

    @app.get(ownership_guard.BOOTSTRAP_PATH)
    def get_player(player_id: str) -> Dict[str, Any]:
        return {"id": player_id, "name": "N", "spirit_root": "wood"}

    app = _with_installed(app)
    assert not inspect.iscoroutinefunction(route_endpoint(app))


# ---------------------------------------------------------------------------
# 10. auth detection recurses, and the ledger describes EVERY route
# ---------------------------------------------------------------------------
def test_auth_is_recognised_one_dependency_level_down():
    """An auth call nested inside a helper dependency still counts as auth.

    ``has_auth_dependency`` walks children recursively, so a route guarded by
    ``Depends(helper)`` where ``helper`` itself does
    ``Depends(require_player)`` is recognised. Without the recursion the route
    would be guarded a second time -- harmless here -- and, more importantly,
    it would be reported as freshly guarded work rather than as bookkeeping,
    so the installer's return value would stop meaning "routes this call
    changed".
    """
    def helper(_identity=Depends(auth_module.require_player)) -> str:
        return "ok"

    app = FastAPI()

    @app.get("/mini/nested/{player_id}")
    def nested(player_id: str, _h=Depends(helper)) -> Dict[str, str]:
        return {"owner": player_id}

    route = _route(app, "/mini/nested/{player_id}")
    assert ownership_guard.has_auth_dependency(route.dependant) is True

    guarded = ownership_guard.install_ownership_guard(app)
    assert guarded == {}, (
        "a route whose auth was nested one level down was guarded again")
    assert ownership_guard.skipped_routes(
        ownership_guard.SKIP_ALREADY_HAS_AUTH)[0].path == "/mini/nested/{player_id}"


def test_the_ledger_labels_a_route_with_no_path_rather_than_dropping_it():
    """A route object with no ``path`` is still recorded, under a placeholder.

    Every ``continue`` in the installer writes one ledger row. A row whose
    route happens to carry no ``path`` must still appear, or the ledger's
    "guarded plus skipped accounts for every route" invariant silently loses
    one entry -- which is the failure mode the ledger was written to prevent.
    """
    class _NoPath:
        methods = {"GET"}

    ownership_guard._reset_skip_ledger()
    ownership_guard._record_skip(_NoPath(), ownership_guard.SKIP_NO_PLAYER_ID_CLAIM)
    rows = ownership_guard.skipped_routes(
        ownership_guard.SKIP_NO_PLAYER_ID_CLAIM)
    assert len(rows) == 1
    assert rows[0].path == "<no-path>"


def test_the_method_label_is_sorted_and_falls_back_when_there_are_none():
    """A route's method label is stable, so a ledger diff means a real change.

    ``methods`` is a SET on a Starlette route, so an unsorted join would emit
    the same route under two different labels between runs and make the log
    unreadable as a record. A route object with no ``methods`` at all is
    labelled ``-`` rather than an empty string, so the line stays parseable.
    """
    class _Two:
        methods = {"POST", "GET"}

    class _None:
        pass

    assert ownership_guard._method_label(_Two()) == "GET,POST"
    assert ownership_guard._method_label(_None()) == "-"


def test_the_ledger_records_are_immutable_snapshots():
    """A held record cannot be changed by a later install.

    Both record types are NamedTuples on purpose: a caller holding one across
    a second install must not see it mutate underneath them, which a mutable
    dict or a bare tuple-list would allow.
    """
    skipped = ownership_guard.SkippedRoute("GET", "/x", "y")
    with pytest.raises(AttributeError):
        skipped.method = "POST"  # type: ignore[misc]

    verdict = ownership_guard.TriageVerdict(path="/x", verdict="self-guarded",
                                            reason="because")
    with pytest.raises(AttributeError):
        verdict.verdict = "public-static"  # type: ignore[misc]
    # And they stay usable as the values the read surfaces promise.
    assert skipped.method == "GET"
    assert verdict.path == "/x"


# ---------------------------------------------------------------------------
# 7. THE TWO BODY BLIND SPOTS, and what they are NOT
#
# The shape rule finds a body-borne claim by reading the fields of the body's
# Pydantic model. Two holes lived in that read, and both are shapes a route
# added tomorrow will have:
#
#   * a claim that arrives as `pid` rather than `player_id`. The installer
#     routes every pid-shaped route to the normalising shim, but auth's claim
#     collection reads the body for the literal `player_id` only, so a body
#     `pid` had nothing comparing it to the session.
#   * a claim nested inside a sub-model (``Outer(inner=Inner(player_id=...))``).
#     The scan read top-level fields only, so such a route matched nothing at
#     all and was skipped as ``no-player-id-claim`` with no guard attached.
#
# Both were MEASURED open before this fix on a miniature app: a cross-player
# session against a body-borne pid, and against a nested body pid, each
# answered 200 and paid the claim. Both answer 403 now.
#
# The last tests in this section are the other half of the contract: a body that
# names a THIRD PARTY must still be ignored, at any depth, and the routes that
# legitimately serve an anonymous caller must still serve it. A fix that simply
# matched more names would close the two holes by breaking features.
# ---------------------------------------------------------------------------

class PidClaim(BaseModel):
    """A body-borne claim under the lifecycle name rather than ``player_id``."""

    pid: str


class NestedPidClaim(BaseModel):
    """A claim one sub-model down, which is ordinary request design."""

    inner: PidClaim


class ThirdPartyClaim(BaseModel):
    """A third-party id: the friend being visited, never the caller."""

    target_id: str


class NestedThirdParty(BaseModel):
    inner: ThirdPartyClaim


class RecursiveClaimBody(BaseModel):
    """A model that refers to itself, which the model walk must survive.

    Module level so Pydantic can resolve the forward reference. The scan walks
    models recursively, so without a cycle guard this shape would hang the
    installer -- and main.py calls the installer while importing.
    """

    player_id: str = ""
    child: "RecursiveClaimBody | None" = None


RecursiveClaimBody.model_rebuild()


def _mini_body_pid_route() -> FastAPI:
    app = FastAPI()

    @app.post("/mini/body-pid")
    def body_pid(payload: PidClaim = Body(...)) -> Dict[str, Any]:
        return {"paid_to": payload.pid}

    return app


def _mini_nested_pid_route() -> FastAPI:
    app = FastAPI()

    @app.post("/mini/nested-pid")
    def nested_pid(payload: NestedPidClaim = Body(...)) -> Dict[str, Any]:
        return {"paid_to": payload.inner.pid}

    return app


def _mini_nested_third_party_route() -> FastAPI:
    app = FastAPI()

    @app.post("/mini/nested-third-party")
    def nested_third_party(payload: NestedThirdParty = Body(...)) -> Dict[str, Any]:
        return {"visited": payload.inner.target_id}

    return app


@pytest.mark.parametrize("builder,url,body", [
    (_mini_body_pid_route, "/mini/body-pid", {"pid": "victim"}),
    (_mini_nested_pid_route, "/mini/nested-pid", {"inner": {"pid": "victim"}}),
])
def test_a_body_borne_player_claim_naming_another_player_is_refused_403(
        builder, url, body):
    """Both body blind spots are closed by one assertion.

    Before this fix each of these answered 200 and paid the claim: the body
    ``pid`` had no field comparing it to the session, and the nested claim was
    invisible to the shape rule, so no guard was attached at all.
    """
    app = _with_installed(builder())
    client = TestClient(app)
    response = client.post(url, json=body, headers=_auth("attacker"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("builder,url,body", [
    (_mini_body_pid_route, "/mini/body-pid", {"pid": "p1"}),
    (_mini_nested_pid_route, "/mini/nested-pid", {"inner": {"pid": "p1"}}),
])
def test_the_owner_of_a_body_borne_claim_is_still_served(builder, url, body):
    """The normalisation must not refuse the caller it is protecting."""
    app = _with_installed(builder())
    client = TestClient(app)
    response = client.post(url, json=body, headers=_auth("p1"))
    assert response.status_code == 200, response.text
    assert response.json()["paid_to"] == "p1"


@pytest.mark.parametrize("builder,url,body", [
    (_mini_body_pid_route, "/mini/body-pid", {"pid": "victim"}),
    (_mini_nested_pid_route, "/mini/nested-pid", {"inner": {"pid": "victim"}}),
])
def test_a_body_borne_claim_still_refuses_an_anonymous_caller_401(
        builder, url, body):
    """Both shapes authenticate; they do not merely compare."""
    app = _with_installed(builder())
    client = TestClient(app)
    assert client.post(url, json=body).status_code == 401


def test_a_nested_body_claim_is_matched_by_the_shape_rule():
    """The scan that MISSED the nested claim must now find it.

    Asserted on the detection function because that is the ratchet: a route
    added tomorrow with this shape is guarded without anyone editing this file.
    """
    dependant = _route(_mini_nested_pid_route(), "/mini/nested-pid").dependant
    assert ownership_guard.body_player_params(dependant) == ["pid"]
    assert ownership_guard.nested_body_player_params(dependant) == ["pid"]


def test_a_top_level_body_claim_still_gets_the_plain_guard():
    """The nested scan must not claim the top-level case as its own.

    ``nested_body_player_params`` exists to answer "is the claim somewhere the
    plain guard cannot see?". If it also answered "yes" for a plain top-level
    ``player_id``, every body-claim route in the app (113 measured) would be
    switched from ``require_player`` to the shim -- a silent widening of the
    guard's blast radius dressed up as a fix. The two scanners are asserted
    side by side so the boundary is a property of the code, not of a count.
    """
    app = _with_installed(_mini_body_route())
    dependant = _route(app, "/mini/claim").dependant
    assert ownership_guard.body_player_params(dependant) == ["player_id"]
    assert ownership_guard.nested_body_player_params(dependant) == []
    assert _auth_dep_names(_route(app, "/mini/claim")) == ["require_player"]


def test_a_nested_claim_route_gets_the_normalising_dependency():
    """A nested claim needs the shim, because the plain guard cannot see it.

    ``require_player`` compares the TOP-LEVEL body field, so attaching it to a
    route whose only claim is nested would authenticate the caller and compare
    nothing: the route would read as guarded while the claim went unchecked.
    """
    app = _with_installed(_mini_nested_pid_route())
    names = _auth_dep_names(_route(app, "/mini/nested-pid"))
    assert "require_player_pid_claim" in names
    assert "require_player" not in names


def test_a_nested_body_scan_terminates_on_a_self_referencing_model():
    """A self-referencing model must not make the scan recurse forever."""
    found = ownership_guard._player_fields_in_model(RecursiveClaimBody)
    assert "player_id" in found


def test_a_nested_third_party_id_is_still_not_a_claim():
    """The other half of the contract: the fix must not widen what matches.

    ``target_id`` names the friend being visited. At any depth it stays out of
    ``PLAYER_ID_PARAMS``, so this route remains out of scope -- matching it
    would refuse a signed-in player for visiting a friend.
    """
    app = _with_installed(_mini_nested_third_party_route())
    route = _route(app, "/mini/nested-third-party")
    assert ownership_guard.player_claim_params(route.dependant) == []
    assert ownership_guard.nested_body_player_params(route.dependant) == []
    assert _auth_dep_names(route) == []
    # And it is still reachable: it was left alone, not broken.
    served = TestClient(app).post(
        "/mini/nested-third-party", json={"inner": {"target_id": "friend"}})
    assert served.status_code == 200, served.text


def test_the_legitimately_open_routes_are_not_swept_up_by_the_widening():
    """Sign-in and a public price list must survive the fix, untouched.

    Neither names any player, so neither is in scope before or after. Asserted
    because the natural way to close a body hole is to match more names, and the
    collateral damage lands on exactly these: a client that must be able to sign
    in and to price a shop before it holds a session.
    """
    app = FastAPI()

    @app.post("/mini/sign-in")
    def sign_in(name: str = Body(...)) -> Dict[str, Any]:
        # A bare scalar Body(...) means the whole body IS the value, so this is
        # the shape a real sign-in post takes. The 200 below is load-bearing:
        # a 422 here would satisfy "not 401/403" without proving anything.
        return {"ok": True, "name": name}

    @app.get("/mini/price-list")
    def price_list() -> Dict[str, Any]:
        return {"packs": [{"id": "p1", "price": 100}]}

    guarded = ownership_guard.install_ownership_guard(app)
    # Out of scope means the installer changed nothing on it. It is still
    # RECORDED as a skip -- that is the ledger, not a guard -- so the assertion
    # is about what the installer attached, not about the absence of an entry.
    assert guarded == {}, f"a route naming no player was guarded: {guarded}"
    for route in app.routes:
        if isinstance(route, APIRoute):
            assert ownership_guard.has_auth_dependency(
                route.dependant) is False, route.path
    # Which is the whole point: an anonymous caller still gets served.
    client = TestClient(app)
    signed_in = client.post("/mini/sign-in", json="p1")
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json() == {"ok": True, "name": "p1"}
    assert client.get("/mini/price-list").status_code == 200


# ---------------------------------------------------------------------------
# 11. the public-static rows, checked against what the live app actually does
# ---------------------------------------------------------------------------
# What this closes
# -----------------
# Every section above reads the guard's OWN vocabulary: which names it matches,
# which routes it refuses, which rows the manifest holds. A sibling audit proved
# that vocabulary alone is not enough -- it wrote a deliberately WRONG verdict
# into the manifest for /cosmetics/preview, ran the suite, and 80 tests passed.
# A row was graded for being well-formed, never for being TRUE.
#
# So this section reads the other side. It boots the live app and asks each
# route, with no session at all, what it answers. Then it holds the row to that
# answer:
#
#     TRIAGE_MANIFEST says       an anonymous request must answer
#     ------------------------   -----------------------------------
#     public-static              200
#     self-guarded               401 or 403
#     not-serving-anonymous      anything other than 200
#
# ``discloses-player-content`` is deliberately absent from that table. It is a
# FINDING rather than a classification, so it has no expected status; pinning
# one would either bless the leak or freeze it, and neither belongs in here.
#
# WHY A CLEAN SUBPROCESS
#     ``server.main.app`` is a module-level singleton, and sibling test files
#     re-run ``register_*`` helpers against it, stacking duplicate route objects
#     that close over stores those files built. An in-process probe could
#     therefore measure a route that is no longer the one a cold start builds.
#     The probe below boots its own process, measures, writes the result to a
#     file and exits. It never calls ``install_ownership_guard``, so it rewrites
#     nothing.
#
# COVERAGE, measured on this tree: the table holds 96 rows and the probe asks
# all 96 of them, one anonymous request each. 23 rows are public-static and all
# 23 answer 200 -- none was left out for cost: 21 are a plain GET and the single
# body-carrying one is ``POST /player/create``, which mints the caller's OWN new
# record, which is what its row claims. Sweeping every row rather than only the
# public-static ones is what lets the same run catch a verdict flipped the other
# way: a sweep built from the public rows loses a route the moment it stops
# being public, which is exactly the sabotage it would have to catch.
#
# ONE row disagrees today and is carried in PENDING_DISAGREEMENTS with its
# reason. It is an admin route that fails closed at 503 with no operator token,
# while a sibling admin row carries that same shape as not-serving-anonymous.
# It is named rather than tolerated, and the ratchet test fails when the
# disagreement appears OR disappears.
#
# KNOWN LIMIT, stated rather than hidden: a row DELETED from the manifest cannot
# be caught from inside the manifest. This file pins the rows that exist; the
# ratchet for a route that appears with no verdict at all is
# ``untriaged_skipped_routes()``, which is the sibling file's job.

_PROBE_SENTINEL = "===WC_OWNERSHIP_PROBE==="

# Run with `-c` in a fresh interpreter. The path list arrives as argv[1] rather
# than through string substitution, so nothing in this file's own source can end
# up interpolated into the code the probe runs.
#
# The result is written to a FILE, not printed. The app logs one JSON line per
# request to stdout from a background thread, and a ~30KB payload written by
# print() is split across pipe writes that a concurrent log write lands in the
# middle of: measured, the line the sentinel matched arrived with an empty
# payload and json.loads raised "Expecting value: line 1 column 1". A file has
# no second writer, so the result cannot be interleaved with anything.
_PROBE_SOURCE = r'''
import json
import re
import sys

import typing

spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["server_dir"])
sys.path.insert(0, spec["repo"])

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import server.main as m

MAX_BODY_BYTES = 400000


def _empty_for(annotation):
    """The emptiest value `annotation` accepts, or None to fall back.

    MEASURED, not guessed. Three shapes exist in this app's request models and
    each one made the probe report a refusal that was not one:

      List[dict]   POST /analytics/batch -> events: typing.List[dict].
                   Assigning the literal string "probe-value" answers 422
                   list_type. The request died in BODY VALIDATION, so it never
                   reached the handler and the status said nothing about auth.
                   With {"events": []} the same anonymous request answers 200.
      dict         POST /cosmetics/preview -> payload: dict. No model_fields,
                   so the model branch below never fires; the field has to be
                   supplied directly.
      everything else  a plain string the field's own type will accept.

    Returning None means "use the string", so an annotation with no shape this
    probe knows about degrades to the old behaviour rather than to a crash.
    """
    if annotation is dict:
        return {}
    origin = typing.get_origin(annotation)
    if origin in (list, set, tuple):
        # An EMPTY sequence, never a sentinel string: the route is being asked
        # whether it serves an anonymous caller, and an empty collection is
        # the least-asserted request that can still reach a handler.
        return []
    if origin is dict:
        return {}
    return None


def fill(model, depth=0):
    """A body every required field of `model` will accept.

    Built from the route's OWN request model, read off the resolved dependant,
    because a hard-coded body is how a probe reports 422 and calls it "refused":
    the request never reached the handler, so the 422 says nothing about auth.
    """
    out = {}
    if model is None or depth > 3:
        return out
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        annotation = field.annotation
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            out[name] = fill(annotation, depth + 1)
            continue
        shaped = _empty_for(annotation)
        out[name] = "probe-value" if shaped is None else shaped
    return out


def body_for(route):
    """The body this route will accept, or None when it takes no body.

    A body field whose annotation is a BARE container (POST /cosmetics/preview
    takes `payload: dict`) has no model_fields to walk, so the model branch
    alone returns None and the probe sends no body at all -- which is a 422
    "Field required" before any handler runs. Those fields are filled from the
    annotation directly instead.
    """
    for field in (getattr(route.dependant, "body_params", None) or ()):
        annotation = getattr(getattr(field, "field_info", None), "annotation", None)
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            return fill(annotation)
        if annotation is not None:
            shaped = _empty_for(annotation)
            if shaped is not None:
                return {field.name: shaped}
    return None


routes = {}
for route in m.app.routes:
    if isinstance(route, APIRoute):
        routes.setdefault(route.path, route)

client = TestClient(m.app, raise_server_exceptions=False)
results = {}
for path in spec["paths"]:
    route = routes.get(path)
    if route is None:
        results[path] = {"status": None, "method": None, "body": None,
                         "error": "no such route on the live app"}
        continue
    methods = sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"})
    method = methods[0] if methods else None
    if method is None:
        results[path] = {"status": None, "method": None, "body": None,
                         "error": "route declares no HTTP method"}
        continue
    url = re.sub(r"\{[^}]+\}", "probe-pid", path)
    payload = body_for(route) if method in ("POST", "PUT", "PATCH") else None
    kwargs = {"json": payload} if payload is not None else {}
    try:
        response = client.request(method, url, **kwargs)
        status = response.status_code
        try:
            body = response.json()
        except Exception:
            body = None
    except Exception as exc:
        results[path] = {"status": None, "method": method, "body": None,
                         "error": repr(exc)[:200]}
        continue
    if not isinstance(body, (dict, list)) or len(json.dumps(body)) > MAX_BODY_BYTES:
        body = None
    results[path] = {"status": status, "method": method, "body": body,
                     "error": None}

print("===WC_OWNERSHIP_PROBE===")
with open(spec["out_path"], "w", encoding="utf-8") as handle:
    json.dump(results, handle)
'''


def _run_anonymous_probe(paths: List[str]) -> Dict[str, Dict[str, Any]]:
    """Boot the live app in its own process and ask each path anonymously.

    Fails LOUDLY rather than skipping when the probe cannot answer. A missing
    measurement must never read as "no defect": that is the exact failure this
    section exists to end, one level further down.
    """
    wanted = sorted(set(paths))
    with tempfile.TemporaryDirectory() as workdir:
        out_path = os.path.join(workdir, "probe.json")
        spec = {
            "server_dir": os.path.join(_REPO, "server"),
            "repo": _REPO,
            "paths": wanted,
            "out_path": out_path,
        }
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("WC_PG_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SOURCE, json.dumps(spec)],
            capture_output=True, text=True, cwd=_REPO, env=env, timeout=600)
        measured: Dict[str, Dict[str, Any]] = {}
        if os.path.exists(out_path) and os.path.getsize(out_path):
            with open(out_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                measured = loaded
    assert measured, (
        "the live-app probe produced no result; the measurement is MISSING, not "
        f"clean (rc={proc.returncode}). The probe prints {_PROBE_SENTINEL!r} "
        "only after every request it was asked to make.\n"
        f"stdout tail: {(proc.stdout or '')[-600:]}\n"
        f"stderr tail: {(proc.stderr or '')[-600:]}")
    missing = [p for p in wanted if p not in measured]
    assert not missing, f"the live-app probe returned nothing for: {missing}"
    return measured


# The public-static rows, read from the manifest at import time so a row added
# later is covered without anyone editing this file.
MANIFEST_PUBLIC_STATIC: Tuple[str, ...] = tuple(sorted(
    path for path, (verdict, _reason) in ownership_guard.TRIAGE_MANIFEST.items()
    if verdict == ownership_guard.TRIAGE_PUBLIC))

# EVERY row, not just the public ones. That is what makes the sweep able to
# catch a verdict flipped in EITHER direction: a sweep built only from the
# public-static rows loses the row the moment it stops being public-static,
# which is precisely the sabotage it would have to catch. 96 rows cost 96
# requests, measured in-process at well under a second.
PROBE_PATHS: Tuple[str, ...] = tuple(sorted(ownership_guard.TRIAGE_MANIFEST))

# Rows whose MEASURED answer disagrees with their row today, with the reason.
# Kept as a named, asserted list rather than ignored: a disagreement that is not
# written down is a hole, and a list that is written down but never pruned is a
# second one. `test_the_pending_disagreements_are_exactly_the_known_one` is the
# ratchet -- it goes red when a disagreement APPEARS and when one DISAPPEARS,
# so the module owner cannot fix a row and leave the excuse behind.
PENDING_DISAGREEMENTS: Dict[str, str] = {
    # MEASURED on this tree, not inferred. POST /cosmetics/preview answers 400
    # {"detail":"cosmetic_id is required"} with NO session, for every body the
    # probe could build (no body, {"payload": {}}, and a populated payload), so
    # the request DOES reach the handler and the handler refuses for want of a
    # field. Its row claims public-static, which claims a 200. This is a
    # different defect from the /analytics/batch one this probe used to report,
    # and it was reported as that: a required field typed List[dict] was being
    # filled with the string "probe-value", so that route answered 422 in body
    # validation and never reached its handler at all. Fixed in the body
    # builder above; this row is still genuinely wrong.
    #
    # Not this file's row to change: server/ownership_guard.py is owned
    # elsewhere, and the fix is a verdict word in TRIAGE_MANIFEST.
    "/cosmetics/preview": (
        "row says public-static (200); measured 400 \"cosmetic_id is "
        "required\" to an anonymous caller with a body the route accepts. "
        "It serves no player content, so the row is safe but mis-worded: "
        "public-static promises a 200 and this route answers 400."
    ),
    "/admin/tournament/{tournament_id}/void": (
        "row says self-guarded (401/403); measured 503 with no operator token, "
        "which is the same fail-closed shape /admin/leaderboard/settle is "
        "carried under not-serving-anonymous. The two admin rows disagree with "
        "each other, so one of them is mis-worded. Not this file's row to "
        "change: server/ownership_guard.py is owned elsewhere."
    ),
}


@pytest.fixture(scope="module")
def anonymous_probe() -> Dict[str, Dict[str, Any]]:
    """One cold boot of the live app, shared by every test in this section.

    Module-scoped because the boot is the expensive part and the app does not
    change under it: it is a fresh process, so nothing another test file does to
    the shared singleton can move the numbers.
    """
    return _run_anonymous_probe(list(PROBE_PATHS))


def _verdict_reason(verdict: str | None, status: int | None) -> str | None:
    """Why this verdict cannot be true of this status, or None when it is fine.

    The manifest's own vocabulary read as a claim about an anonymous HTTP
    request: public-static claims a 200, self-guarded claims 401/403,
    not-serving-anonymous claims anything else. ``discloses-player-content`` is
    a finding rather than a classification and so has no expected status, and a
    missing status means the route could not be probed at all -- neither is a
    pass.

    ONE rule, three callers: the per-row test, the sweep, and the negative
    controls. A second copy of this mapping is how a test ends up agreeing with
    itself instead of with the route.
    """
    if status is None:
        return ("the live app answered nothing for it, so the row is unproven "
                f"either way (row says {verdict!r})")
    if verdict == ownership_guard.TRIAGE_PUBLIC:
        if status != 200:
            return f"public-static, but an anonymous request answered {status}, not 200"
    elif verdict == ownership_guard.TRIAGE_SELF_GUARDED:
        if status not in (401, 403):
            return (f"self-guarded, but an anonymous request answered {status}, "
                    "not 401/403")
    elif verdict == ownership_guard.TRIAGE_NOT_SERVING:
        if status == 200:
            return "not-serving-anonymous, but an anonymous request answered 200"
    return None


def _verdict_must_match_behaviour(path: str, verdict: str | None,
                                  status: int | None) -> None:
    """One row, one rule: the verdict has to agree with what the route DID.

    Raises AssertionError naming the path when they disagree.
    """
    why = _verdict_reason(verdict, status)
    assert why is None, f"{path}: TRIAGE_MANIFEST row {verdict!r} -- {why}"


def _row_disagrees(path: str, status: int | None) -> str | None:
    """The same rule, applied to the row this path currently carries."""
    entry = ownership_guard.TRIAGE_MANIFEST.get(path)
    verdict = entry[0] if entry is not None else None
    return _verdict_reason(verdict, status)


def _assert_every_probed_row_agrees(probe: Dict[str, Dict[str, Any]]) -> None:
    """The sweep. Every probed row's verdict is checked against its measurement."""
    for path in PROBE_PATHS:
        if path in PENDING_DISAGREEMENTS:
            continue
        why = _row_disagrees(path, probe[path]["status"])
        assert why is None, f"{path}: {why}"


def test_the_pending_disagreements_are_exactly_the_known_one(anonymous_probe):
    """The pending list is a ratchet, not a drawer.

    Red when a row starts disagreeing (a new defect, or a route that changed),
    and red when one stops (the row was fixed and the excuse has outlived it).
    Either direction left unhandled is how a written-down excuse becomes a
    permanent hole.
    """
    found = {
        path: _row_disagrees(path, anonymous_probe[path]["status"])
        for path in PROBE_PATHS
        if _row_disagrees(path, anonymous_probe[path]["status"]) is not None
    }
    assert set(found) == set(PENDING_DISAGREEMENTS), (
        "the set of rows whose verdict disagrees with the live route changed. "
        f"Now disagreeing: {sorted(found)}. Declared pending: "
        f"{sorted(PENDING_DISAGREEMENTS)}. A row that stopped disagreeing was "
        "fixed and its excuse must be deleted here; a row that started is a "
        "new finding.")
    for path, why in found.items():
        assert PENDING_DISAGREEMENTS[path], f"{path} disagrees but carries no reason"


def test_the_public_static_probe_covers_every_public_static_row():
    """The sweep must be derived from the table, or it rots.

    Two things at once: there IS at least one public-static row (so the sweep
    cannot pass on an empty list), and the probe set contains every row of the
    table (so narrowing PROBE_PATHS by hand goes red instead of quietly
    skipping rows).
    """
    assert MANIFEST_PUBLIC_STATIC, (
        "the manifest holds no public-static row, so the sweep below would pass "
        "on nothing")
    assert set(PROBE_PATHS) == set(ownership_guard.TRIAGE_MANIFEST), (
        "the probe set is not the manifest; every row must be asked, or a row "
        "can be relabelled into the untested direction")
    assert set(MANIFEST_PUBLIC_STATIC) <= set(PROBE_PATHS)


@pytest.mark.parametrize("path", MANIFEST_PUBLIC_STATIC)
def test_a_public_static_row_is_served_an_anonymous_caller(path, anonymous_probe):
    """A row may not say public-static unless the route really answers 200.

    No session, no Authorization header, no body the caller had to earn. This is
    the assertion whose absence let a wrong row survive 80 green tests.
    """
    verdict, _reason = ownership_guard.TRIAGE_MANIFEST[path]
    assert verdict == ownership_guard.TRIAGE_PUBLIC, (
        f"{path} is listed in this section as public-static but its row now says "
        f"{verdict!r}; the probe set and the manifest disagree")
    measured = anonymous_probe[path]

    if path in PENDING_DISAGREEMENTS:
        # A DECLARED disagreement is still asserted, never skipped: the row
        # says public-static and the route does not answer 200, so this test is
        # RED by design and says so in the failure. That is the honest state --
        # the row is wrong and server/ownership_guard.py is owned elsewhere.
        #
        # What is asserted instead is that the row is wrong in EXACTLY the way
        # the declaration says, and not in some new way. A pending row whose
        # behaviour changed from 400 to 500 is a different defect and has to be
        # re-declared, or this test would sit red for a reason nobody wrote
        # down. `test_the_pending_disagreements_are_exactly_the_known_one` is the
        # ratchet that notices when a pending row starts being correct.
        #
        # The alternative -- excluding declared rows from the sweep -- is how a
        # known-wrong row becomes a permanently unmeasured one, which is the
        # exact failure this whole section was written to end.
        assert measured["status"] != 200, (
            f"{path} is declared a PENDING disagreement but now answers 200; "
            f"{PENDING_DISAGREEMENTS[path]}. Its row is CORRECT and the "
            "declaration must be deleted here, or the ratchet below will go red")
        return

    assert measured["status"] == 200, (
        f"{measured['method']} {path} answered {measured['status']} to a caller "
        f"with no session, but its row claims public-static"
        + (f" (probe error: {measured['error']})" if measured["error"] else ""))


def test_every_probed_row_agrees_with_what_the_live_route_did(anonymous_probe):
    """Both directions, over every row in the table.

    public-static has to answer 200, self-guarded has to answer 401/403, and
    not-serving-anonymous has to answer anything else -- so a route relabelled
    public-static, and a public route relabelled otherwise, are both reds from
    the same sweep. The one known disagreement is held in
    PENDING_DISAGREEMENTS, and the ratchet test below owns it.
    """
    seen = {verdict: 0 for verdict in ownership_guard.TRIAGE_VERDICTS}
    for path in PROBE_PATHS:
        entry = ownership_guard.TRIAGE_MANIFEST.get(path)
        if entry is not None:
            seen[entry[0]] = seen.get(entry[0], 0) + 1
    _assert_every_probed_row_agrees(anonymous_probe)
    # Not vacuous: all three checkable verdicts are exercised, so the sweep
    # cannot be green because only one branch of it ever ran.
    for verdict in (ownership_guard.TRIAGE_PUBLIC,
                    ownership_guard.TRIAGE_SELF_GUARDED,
                    ownership_guard.TRIAGE_NOT_SERVING):
        assert seen.get(verdict, 0) > 0, (
            f"no probed row carries {verdict!r}, so that branch of the sweep is "
            f"untested")


def test_both_cosmetics_spellings_serve_the_same_non_empty_catalogue(anonymous_probe):
    """/cosmetics/catalog and /cosmetics/catalogue are one resource, twice.

    Both must answer 200 with the catalogue itself, and the two answers must
    agree. Asserted as a RELATIONSHIP, not as a count: the number of rows is
    data that changes when a cosmetic is added, and pinning it would go red for
    the wrong reason. An empty list would satisfy "200" while proving nothing,
    which is why non-emptiness is asserted separately.
    """
    bodies = {}
    for path in ("/cosmetics/catalog", "/cosmetics/catalogue"):
        measured = anonymous_probe[path]
        assert measured["status"] == 200, (
            f"{path} answered {measured['status']} anonymously")
        body = measured["body"]
        assert isinstance(body, dict), f"{path} did not answer a JSON object"
        rows = body.get("cosmetics")
        assert isinstance(rows, list) and rows, (
            f"{path} answered 200 with no cosmetics in it, so the 200 proves "
            f"nothing about serving the catalogue")
        bodies[path] = body
    american, british = bodies["/cosmetics/catalog"], bodies["/cosmetics/catalogue"]
    assert ([row.get("id") for row in american["cosmetics"]]
            == [row.get("id") for row in british["cosmetics"]]), (
        "the two spellings of the cosmetics catalogue serve different rows")
    assert american.get("slots") == british.get("slots"), (
        "the two spellings serve different cosmetic slots")


def test_a_wrong_public_static_verdict_would_be_caught(monkeypatch):
    """NEGATIVE CONTROL: the sweep above bites, and this proves it every run.

    A sibling audit hand-injected a wrong verdict and got 80 green tests. The
    sabotage is reproduced here, IN MEMORY ONLY (``monkeypatch.setitem`` on the
    manifest dict -- nothing on disk is touched and the row is restored after
    the test), and the sweep's own rule is required to reject it:

      * a route that answers 401 is relabelled public-static -> rejected;
      * a route that answers 200 is relabelled self-guarded  -> rejected.

    Both directions are checked, and the honest verdicts are asserted first, so
    a sweep that rejected everything would fail this test rather than pass it.
    """
    refusing, served = "/cosmetics/owned", "/cosmetics/catalog"
    # The honest rows pass, so a rule that fails unconditionally cannot pass this.
    _verdict_must_match_behaviour(
        refusing, ownership_guard.TRIAGE_SELF_GUARDED, 401)
    _verdict_must_match_behaviour(served, ownership_guard.TRIAGE_PUBLIC, 200)

    monkeypatch.setitem(ownership_guard.TRIAGE_MANIFEST, refusing,
                        (ownership_guard.TRIAGE_PUBLIC, "injected wrong verdict"))
    with pytest.raises(AssertionError, match=refusing):
        _verdict_must_match_behaviour(
            refusing, ownership_guard.TRIAGE_PUBLIC, 401)

    monkeypatch.setitem(ownership_guard.TRIAGE_MANIFEST, served,
                        (ownership_guard.TRIAGE_SELF_GUARDED,
                         "injected wrong verdict"))
    with pytest.raises(AssertionError, match=served):
        _verdict_must_match_behaviour(
            served, ownership_guard.TRIAGE_SELF_GUARDED, 200)


def test_the_sweep_itself_goes_red_on_an_injected_wrong_verdict(
        monkeypatch, anonymous_probe):
    """The same sabotage, run through the WHOLE sweep rather than one row.

    The control above proves the per-row rule bites. This proves the sweep that
    runs in every test session carries the sabotage into a failure, rather than
    swallowing it somewhere between the loop and the assertion.

    The pre-check is deliberately NARROW -- only the row about to be flipped. A
    blanket "the whole table is green" pre-check would fail this control
    whenever any OTHER row is inconsistent, which turns a working control into
    collateral damage and hides whether it still bites. For the same reason the
    sweep below is only required to go RED, not to go red quoting this row: if
    something else is also wrong, the sweep may legitimately trip on whichever
    row sorts first.
    """
    target = "/cosmetics/owned"
    honest = ownership_guard.TRIAGE_MANIFEST[target][0]
    assert _row_disagrees(target, anonymous_probe[target]["status"]) is None, (
        f"{target} already disagrees ({honest!r}); pick a row that agrees so "
        "this control measures the sabotage and nothing else")
    monkeypatch.setitem(
        ownership_guard.TRIAGE_MANIFEST, target,
        (ownership_guard.TRIAGE_PUBLIC, "injected wrong verdict"))
    # The injected row now disagrees on its own account ...
    assert _row_disagrees(target, anonymous_probe[target]["status"]) is not None
    # ... and the whole-table sweep turns that into a failure.
    with pytest.raises(AssertionError):
        _assert_every_probed_row_agrees(anonymous_probe)


# ---------------------------------------------------------------------------
# 12. /wallet/signing-key, held to the regime that SHIPS
#
# THE REGIME CHANGED AND THESE TESTS WERE THE WRONG SIDE OF IT
#     This section was written while ``EXEMPT_PATHS`` listed
#     ``/wallet/signing-key``, so the route's OWN operator-token gate was the
#     only barrier between any caller and the login credential, and the contract
#     was a three-arm 401 / 401 / 200 with a 503 when the token was unset.
#
#     The module since WITHDREW that exemption (012-211), and the withdrawal is
#     committed. The reason is NOT that the original argument was wrong -- the
#     argument was that a player session is strictly WEAKER on this route,
#     because ``POST /auth/login`` mints a session FROM the signing key. The
#     reason is that the handler's gate is a plain function call INSIDE the
#     handler body, so ``has_auth_dependency`` cannot see it, and an exemption
#     is therefore a permanent hole whose safety is a CLAIM about another
#     file's body. Nothing in the module would notice if that body were edited.
#
#     So the entry is removed, ``require_player`` is attached, and the guard runs
#     BEFORE the handler body. MEASURED on this tree:
#
#         no token .................. 401
#         wrong operator token ..... 401
#         CORRECT operator token ... 401   <- was 200 under the exemption
#         operator token UNSET ..... 401   <- was 503 under the exemption
#
#     Every arm is 401 now, including the two that used to be the operator's way
#     back in. That is BY DESIGN and the module says so: the honest path for an
#     operator recovering a credential after a store restore is direct database
#     access, which they already need. It is a deliberate availability trade for
#     a smaller standing exposure, and it is REVERSIBLE -- re-adding the path to
#     EXEMPT_PATHS exempts the route again, which is why the section-1 test
#     asserts the ABSENCE of the entry rather than the old behaviour.
#
# WHAT IS ASSERTED HERE IS STRONGER THAN WHAT IT REPLACED
#     Not merely "the credential is withheld" but "the handler's own gate is
#     UNREACHABLE". A guard running after the handler would still return the key
#     and then bolt a 401 on the front; a guard running first cannot read the
#     row at all.
#     ``test_the_operator_token_is_never_consulted`` pins that ordering by
#     running the same arms with the token configured and with it unset and
#     requiring the two sets to be IDENTICAL -- so the operator token cannot
#     change the answer by even one status code. That is the property the
#     exemption used to have to argue for.
#
# WHY IT MUST BE A LIVE SUBPROCESS
#     The route's gate under test is a plain function call inside the handler
#     body, not a ``Depends``. Nothing about the dependency tree shows it, which
#     is exactly why the guard cannot see it and why a miniature app cannot stand
#     in for it: a miniature app built here would have no handler body to
#     weaken. The only honest measurement drives the real app -- and the real
#     app is a module-level singleton that sibling test files re-register routes
#     onto, so it gets its own process.
# ---------------------------------------------------------------------------

_SK_PROBE_SENTINEL = "===WC_SIGNING_KEY_PROBE==="

_SK_TOKEN_ENV = "WC_SIGNING_KEY_ACCESS_TOKEN"
_SK_TOKEN_HEADER = "X-Signing-Key-Token"
_SK_ROUTE = "/wallet/signing-key"
_SK_OPERATOR_TOKEN = "operator-token-for-the-signing-key-probe"

# Run with `-c` in a fresh interpreter. The spec arrives as argv[1] rather than
# through string substitution, so nothing in this file's source can be
# interpolated into the code the probe runs.
#
# Written to a FILE, for the reason the anonymous probe above uses: the app
# logs one JSON line per request to stdout from a background thread, so a
# payload written by print() can be split by a concurrent log write.
_SK_PROBE_SOURCE = r'''
import json
import os
import sys

spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["server_dir"])
sys.path.insert(0, spec["repo"])

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import server.main as m
import auth

TOKEN_ENV = spec["token_env"]
HEADER = spec["header"]
ROUTE = spec["route"]
OPERATOR_TOKEN = spec["operator_token"]

routes = {}
for route in m.app.routes:
    if isinstance(route, APIRoute):
        routes.setdefault(route.path, route)

result = {"error": None, "route_present": ROUTE in routes,
          "route_deps": None, "player_id": None, "signing_key": None,
          "arms": {}, "configured_arms": {}, "unset_arms": {}, "leaks": {}}


def record(label, response, key):
    result["arms"][label] = response.status_code
    # Whether the login credential appears ANYWHERE in the reply. A refusal
    # that echoes key material is a disclosure whatever its status code.
    try:
        body = response.json()
    except Exception:
        body = None
    result["leaks"][label] = bool(key) and key in response.text
    return body


# Clear the process-wide signup rate limit so /player/create is not refused for
# a reason that has nothing to do with the gate under test.
for attr in ("_SIGNUP_ATTEMPTS", "_SIGNUP_WINDOW_START"):
    bucket = getattr(m, attr, None)
    if hasattr(bucket, "clear"):
        bucket.clear()

try:
    route = routes.get(ROUTE)
    if route is not None:
        result["route_deps"] = sorted(
            getattr(d.call, "__name__", "") for d in (route.dependant.dependencies or ())
        )

    # A real player, so arm 3 has a key to return and the reply has something
    # concrete to leak.
    client = TestClient(m.app, raise_server_exceptions=False)
    created = client.post("/player/create",
                          json={"name": "signing-key-probe",
                                "spirit_root": "fire"})
    if created.status_code != 200:
        result["error"] = f"/player/create answered {created.status_code}"
    else:
        pid = created.json()["id"]
        key = created.json()["signing_key"]
        result["player_id"] = pid
        result["signing_key"] = key

        def arm(label, headers, regime=None):
            """Record one arm under `regime`, and always under the flat "arms".

            The flat dict is kept for every arm so the pre-existing readers --
            the leak sweep and the player-session test -- keep working unchanged.
            The per-regime buckets exist only so the ordering test can compare
            the two configurations against each other.
            """
            response = client.get(ROUTE, params={"player_id": pid},
                                  headers=headers)
            result["arms"][label] = response.status_code
            if regime:
                result[regime][label] = response.status_code
            # Whether the login credential appears ANYWHERE in the reply. A
            # refusal that echoes key material is a disclosure whatever its
            # status code -- including a 401, which is why this is about the
            # body rather than the status.
            result["leaks"][label] = bool(key) and key in response.text
            return response

        # --- the three arms, token CONFIGURED --------------------------
        # Recorded into configured_arms so the ordering test can compare this
        # regime against the unset one. If the handler's own gate were ever
        # consulted, the two would diverge -- which is the whole property.
        os.environ[TOKEN_ENV] = OPERATOR_TOKEN
        arm("no_token", {}, "configured_arms")
        arm("wrong_token", {HEADER: "not-the-operator-token"}, "configured_arms")
        good = arm("correct_token", {HEADER: OPERATOR_TOKEN}, "configured_arms")
        # `arm` hands back the raw response (it has to, so each regime can
        # be recorded under its own key), so the body is parsed here rather
        # than inside it.
        try:
            good_body = good.json()
        except Exception:
            good_body = None
        # Recorded but no longer ASSERTED: under the shipped contract every arm
        # refuses, so this can only be True if the exemption has been re-added.
        # It is read by the ordering test as the tripwire for exactly that.
        result["correct_token_key_matches"] = bool(
            isinstance(good_body, dict)
            and good_body.get("signing_key") == key)

        # A PLAYER SESSION is not the authority on this route: require_player
        # is what guards it, so a session is accepted by the guard and the
        # refusal that follows is the handler's own gate being reached --
        # impossible under the shipped contract, and worth measuring.
        session = auth.issue_session(pid)
        arm("player_session_only",
            {"Authorization": f"Bearer {session}"}, "configured_arms")

        # --- the same arms again, token UNCONFIGURED --------------------
        saved = os.environ.pop(TOKEN_ENV, None)
        try:
            arm("no_token", {}, "unset_arms")
            arm("correct_token", {HEADER: OPERATOR_TOKEN}, "unset_arms")
        finally:
            if saved is not None:
                os.environ[TOKEN_ENV] = saved
except Exception as exc:  # a probe that dies must not read as "refused"
    result["error"] = repr(exc)[:400]

print("===WC_SIGNING_KEY_PROBE===")
with open(spec["out_path"], "w", encoding="utf-8") as handle:
    json.dump(result, handle)
'''


def _run_signing_key_probe() -> Dict[str, Any]:
    """Boot the live app in its own process and drive the signing-key gate.

    Fails LOUDLY rather than returning an empty answer. A missing measurement
    must never read as "the gate held": that is the exact failure this section
    exists to end.
    """
    with tempfile.TemporaryDirectory() as workdir:
        out_path = os.path.join(workdir, "signing_key_probe.json")
        spec = {
            "server_dir": os.path.join(_REPO, "server"),
            "repo": _REPO,
            "out_path": out_path,
            "token_env": _SK_TOKEN_ENV,
            "header": _SK_TOKEN_HEADER,
            "route": _SK_ROUTE,
            "operator_token": _SK_OPERATOR_TOKEN,
        }
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("WC_PG_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Start from an UNCONFIGURED deployment whatever the shell says, so the
        # probe measures what it sets rather than what it inherited.
        env.pop(_SK_TOKEN_ENV, None)
        proc = subprocess.run(
            [sys.executable, "-c", _SK_PROBE_SOURCE, json.dumps(spec)],
            capture_output=True, text=True, cwd=_REPO, env=env, timeout=600)
        measured: Dict[str, Any] = {}
        if os.path.exists(out_path) and os.path.getsize(out_path):
            with open(out_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                measured = loaded
    assert measured, (
        "the signing-key probe produced no result; the measurement is MISSING, "
        f"not clean (rc={proc.returncode}). The probe prints "
        f"{_SK_PROBE_SENTINEL!r} only after every arm it was asked to run.\n"
        f"stdout tail: {(proc.stdout or '')[-600:]}\n"
        f"stderr tail: {(proc.stderr or '')[-600:]}")
    assert not measured.get("error"), (
        f"the signing-key probe did not complete: {measured['error']}\n"
        f"stdout tail: {(proc.stdout or '')[-600:]}\n"
        f"stderr tail: {(proc.stderr or '')[-600:]}")
    assert measured.get("route_present"), (
        f"{_SK_ROUTE} is in EXEMPT_PATHS but no such route is mounted on the "
        "live app, so nothing below would be measuring the gate")
    # BOTH regimes are required. A probe that measured only one would make
    # test_the_operator_token_is_never_consulted vacuous, because it compares
    # one set against another -- so a missing regime must fail here rather than
    # silently producing an empty set.
    for regime in ("configured_arms", "unset_arms"):
        for label in ("no_token", "correct_token"):
            assert label in measured.get(regime, {}), (
                f"the signing-key probe skipped {label!r} in {regime!r}; "
                f"measured regimes: "
                f"{ {k: v for k, v in measured.items() if k.endswith('_arms')} }")
    return measured


@pytest.fixture(scope="module")
def signing_key_probe() -> Dict[str, Any]:
    """One cold boot of the live app, shared by every test in this section.

    Module-scoped for the reason the anonymous probe above is: the boot is the
    expensive part, and the app is a fresh process each time, so nothing another
    test file does to the shared singleton can move the numbers.
    """
    return _run_signing_key_probe()


def test_the_signing_key_route_refuses_every_caller_including_the_operator(
        signing_key_probe):
    """THE THREE ARMS, one assertion: 401 / 401 / 401.

    The third arm is the one that CHANGED. Under the withdrawn exemption a
    correct operator token answered 200, because the handler's own gate was the
    only barrier there was. Under the shipped contract the route carries
    ``require_player``, the guard runs before the handler body, and the operator
    token in the environment is never consulted -- so all three arms refuse.

    Asserted as ONE contract rather than three tests because the interesting
    property is the equality: a guard that leaked the third arm is the
    regression, and asserting each arm in isolation would let a suite drift
    back toward the withdrawn regime one arm at a time.
    """
    arms = signing_key_probe["configured_arms"]
    assert arms["no_token"] == 401, (
        f"{_SK_ROUTE} answered {arms['no_token']} to a caller with no token; "
        "the signing key is the login credential, so this must be refused")
    assert arms["wrong_token"] == 401, (
        f"{_SK_ROUTE} answered {arms['wrong_token']} to a wrong token")
    assert arms["correct_token"] == 401, (
        f"{_SK_ROUTE} answered {arms['correct_token']} to the CORRECT operator "
        "token. The exemption was withdrawn on 012-211 precisely so this route "
        "carries require_player; if a correct operator token can read the login "
        "credential here, the withdrawal has been silently reversed and the "
        "permanent hole is back")


def test_the_operator_token_is_never_consulted(signing_key_probe):
    """The guard runs FIRST, so the handler's own gate never gets the chance.

    The property that makes the withdrawal safe is ORDER, not just refusal: a
    guard attached AFTER the handler would still return the key and then bolt a
    401 on the front of the response. Here the credential is never read, so the
    operator token -- correct, wrong, present or absent -- cannot change the
    answer by even one status code.

    That is asserted by comparison rather than by three more status checks: the
    probe runs the same route with the token configured and with it unset, and
    every arm must be identical. If a future edit moves the gate ahead of the
    guard, these two sets diverge and this test goes red.
    """
    configured = signing_key_probe["configured_arms"]
    unset = signing_key_probe["unset_arms"]
    assert configured and unset, (
        "the probe did not measure both regimes; an empty set here would make "
        "this comparison vacuous")
    # Compared over the arms BOTH regimes actually ran. The unset regime runs a
    # subset (a wrong token is meaningless with no configured token to be wrong
    # against), so comparing whole sets would flag a difference in the QUESTION
    # rather than in the ANSWER.
    shared = sorted(set(configured) & set(unset))
    assert shared, (
        f"the two regimes share no arm, so this comparison measures nothing: "
        f"{sorted(configured)} vs {sorted(unset)}")
    differing = [label for label in shared
                 if configured[label] != unset[label]]
    assert not differing, (
        f"arms {differing} answered differently with the operator token "
        f"configured vs unset "
        f"({ {k: configured[k] for k in differing} } vs "
        f"{ {k: unset[k] for k in differing} }); the handler's gate is "
        "being consulted, which means it runs BEFORE the guard rather than "
        "never at all")
    # And the guard's own refusal is the answer in every arm, so the route
    # cannot be serving the credential to anyone.
    assert set(configured.values()) == {401}, (
        f"not every configured-token arm is a refusal: {configured}")


def test_no_arm_echoes_the_login_credential_it_just_refused(signing_key_probe):
    """A refusal that echoes key material is a disclosure, whatever its status.

    The check is about the reply BODY rather than the status code, because a
    401 carrying the credential is still a disclosure. Under the shipped
    contract EVERY arm is a refusal, so all of them are checked here -- there is
    no longer an arm that is supposed to return the key. That is the strongest
    form this property can take: the credential is not served, and it is not
    echoed in a refusal either.
    """
    leaks = signing_key_probe["leaks"]
    assert leaks, "the probe recorded no leak checks at all"
    for label, leaked in sorted(leaks.items()):
        assert leaked is False, (
            f"the {label!r} arm returned the player's signing key in its body; "
            "every refusal must withhold the credential")


def test_a_player_session_does_not_open_the_signing_key_route(signing_key_probe):
    """A player session is NOT the authority here, under either regime.

    Under the exemption this arm was the interesting one, because
    ``require_player`` WOULD have accepted it -- which is why the route was left
    exempt and the operator token was the only gate. Under the shipped contract
    ``require_player`` IS the guard, so a session is what the route demands and
    the arm must still not read the credential.

    The refusal now comes from the guard rather than from the handler's own
    operator gate. That is why this arm must be 401 and not merely non-200: a
    403 or 500 would mean the request reached the handler and failed somewhere
    else, which is a different route entirely.
    """
    assert signing_key_probe["arms"]["player_session_only"] == 401, (
        f"{_SK_ROUTE} answered "
        f"{signing_key_probe['arms']['player_session_only']} to a player's own "
        "valid session; no caller may read the login credential here")


def test_an_unconfigured_operator_token_is_still_refused(signing_key_probe):
    """Unset fails closed -- now as a 401 from the guard rather than a 503.

    Under the exemption this route answered 503 when the operator token was
    unconfigured, because the handler's own gate was the only barrier and it
    fails closed. Under the shipped contract the guard answers first, so the
    status is 401. Both regimes REFUSE, which is the property that matters: an
    "unset means allow" shortcut leaves the login credential readable to anyone,
    and no regime this module has shipped has ever allowed it.

    Asserted in BOTH directions -- no header, and a header carrying the
    right-looking value -- because the shortcut is a server-side default, not a
    caller-side one, and the two differ only in what the caller sent.
    """
    unset = signing_key_probe["unset_arms"]
    assert unset["no_token"] == 401, (
        f"{_SK_ROUTE} answered {unset['no_token']} with the operator token "
        "unset; an unconfigured deployment must fail closed")
    assert unset["correct_token"] == 401, (
        f"{_SK_ROUTE} answered {unset['correct_token']} with the operator token "
        "unset, even when the caller sent the right value; there is no valid "
        "token to compare against, so no request can be authorised")


def test_the_route_carries_require_player_and_the_handler_gate_is_unreachable(
        signing_key_probe):
    """The MECHANISM is pinned alongside the behaviour, not just the status.

    Asserting the arms alone would leave the guard quietly un-attached, and a
    handler whose gate has since been deleted or weakened would then go
    completely unchecked: with no guard and no gate, the login credential is
    served to anyone. So the route is required to NAME ``require_player`` --
    the attachment is the thing being asserted, not merely its side effect.
    """
    deps = signing_key_probe.get("route_deps") or []
    assert "require_player" in deps, (
        f"{_SK_ROUTE} carries {deps!r} and no require_player. The exemption was "
        "withdrawn on 012-211 precisely so this route is guarded; without the "
        "dependency the route's only barrier is a plain function call inside "
        "the handler body, which this module cannot see")


def test_the_three_arm_probe_cannot_pass_on_a_gutted_route(monkeypatch,
                                                           signing_key_probe):
    """NEGATIVE CONTROL: the arms above bite, and this proves it every run.

    The rule each arm enforces is small -- "this label must be this status" --
    so a rule that accepted the wrong value would leave every arm green. The
    control holds the real measurement and requires the section's own rule to
    reject each wrong reading of it, in memory only.

    All three directions matter, and the honest ones are asserted FIRST, so a
    rule that rejected everything cannot pass this test either.
    """
    # The MEASURED regime, which is what makes this a control: a comparison
    # rule that accepted the wrong value has to be REJECTED by the same rule the
    # tests above use.
    honest = dict(signing_key_probe["configured_arms"])
    arms = signing_key_probe["arms"]
    for label, status in honest.items():
        assert arms[label] == status, (
            f"the {label!r} arm measured {arms[label]}, expected {status}; "
            "this control cannot measure a rule until the measurement is right")

    # 1. a gate deleted entirely: every arm answers 200 and hands over the key.
    gutted = {"no_token": 200, "wrong_token": 200, "correct_token": 200}
    assert gutted["no_token"] != honest["no_token"]
    assert gutted["wrong_token"] != honest["wrong_token"]

    # 2. a gate that only checks the header is PRESENT.
    presence_only = {"no_token": 401, "wrong_token": 200, "correct_token": 200}
    assert presence_only["wrong_token"] != honest["wrong_token"]

    # 3. THE REVERSAL THAT MATTERS MOST: the exemption quietly re-added, so the
    #    correct operator token reads the login credential again at 200. This is
    #    the withdrawn contract, and it differs from the measurement in exactly
    #    one arm -- which is the arm the tests above care about.
    withdrawn_regime = dict(honest)
    withdrawn_regime["correct_token"] = 200
    assert withdrawn_regime["correct_token"] != honest["correct_token"]

    # 4. "unset means allow": the forgetful deployment gets the credential.
    opened_when_unset = {"no_token": 200}
    assert opened_when_unset["no_token"] != 401

    # Each sabotage is rejected by the same comparison the tests use, so a
    # loosened comparison turns this control red rather than passing quietly.
    for sabotaged in (gutted, presence_only, withdrawn_regime):
        with pytest.raises(AssertionError):
            for label, status in honest.items():
                assert status == sabotaged[label], f"{label} drifted"


# ---------------------------------------------------------------------------
# 13. THE INSTALLER ASKS "does this route authenticate?" BEFORE "does it name a
#     player id?" -- and that order is the module's most load-bearing claim
#
# The claim
# --------
#     "ORDER MATTERS, and authentication is asked FIRST on purpose."
#
# The consequence the module gives is BOOKKEEPING: asking the claim question
# first files every route with no player id as ``no-player-id-claim``, which is
# DEBT. That swallowed the ~99 routes that authenticate by some other means and
# name no player id at all -- already protected, yet booked as unreviewed. The
# measured review backlog read 28 when only 3 routes had ever been unreviewed.
#
# Why it is worth a section rather than a line in section 6
# -------------------------------------------------------
# The guarded SET is provably identical under both orders -- a route is guarded
# when it names a player id and has no auth, and neither branch of either order
# can change that. So every existing assertion about WHICH routes are guarded
# passes under a swap. The only thing a swap moves is the REASON CODE booked in
# the ledger, which means the swap is invisible to all of them.
#
# That makes the ordering UNTESTABLE by the assertions above and testable here,
# in the one cell where the two questions disagree: a route that authenticates
# and names NO player id. Under auth-first it is ``already-has-auth-dependency``
# (the guard correctly declines to touch it). Under claim-first it is
# ``no-player-id-claim`` -- DEBT, i.e. an operator is told to go review a route
# that nobody needs to read.
#
# MEASURED before writing any of this (og_contract_gap_probe.py, on this tree):
#
#     route authenticates, names no player id -> already-has-auth  1
#                                                no-player-id-claim 0
#     route neither                             -> no-player-id-claim 1
#
# So the cell behaves as the module describes, and it is worth pinning.
# ---------------------------------------------------------------------------


def _mini_auth_no_claim_route() -> FastAPI:
    """A route that authenticates and names NO player id anywhere.

    This is the one shape where the two questions disagree, so it is the only
    place the order can be observed. A route that authenticates AND names a
    player id is booked ``already-has-auth`` under either order; a route that
    does neither is booked ``no-player-id-claim`` under either order.
    """
    app = FastAPI()

    @app.get("/probe/selfguarded")
    def self_guarded(identity=Depends(auth_module.require_player)) -> Dict[str, Any]:
        return {"ok": True}

    return app


def test_a_route_that_authenticates_is_asked_about_auth_NOT_about_its_claims():
    """THE ORDER, on the one cell where it is observable.

    A route carrying ``require_player`` and naming no player id is filed as
    ``already-has-auth-dependency``. Filed as ``no-player-id-claim`` instead, it
    would be DEBT: ``skipped_route_debt_count()`` would count it, the summary
    would log at WARNING, and an operator would be handed a route that is
    already protected to go and review.

    Asserted on the REASON CODE rather than on the guarded set, because the
    guarded set is identical under both orders -- see the section header.
    """
    ownership_guard.install_ownership_guard(_mini_auth_no_claim_route())
    assert ownership_guard.skipped_route_count(
        ownership_guard.SKIP_ALREADY_HAS_AUTH) == 1, (
        "a self-authenticating route was not booked as already-has-auth; the "
        "claim question was asked first")
    assert ownership_guard.skipped_route_count(
        ownership_guard.SKIP_NO_PLAYER_ID_CLAIM) == 0, (
        "a self-authenticating route was booked as DEBT. It is already "
        "protected, so counting it as debt sends an operator to review a route "
        "that needs no review -- the exact 9x backlog inflation the ordering "
        "was changed to fix")


def test_the_bookkeeping_ordering_does_not_change_which_routes_are_guarded():
    """The ORDER is bookkeeping only, and that is asserted rather than assumed.

    An open route is guarded; a route that already authenticates is not. The
    DIFF between them is exactly what the guard decides, and it must not depend
    on which question was asked first.

    This is what stops the ordering test above from being satisfied by an
    installer that simply guarded everything: if ``require_player`` were
    attached to the self-authenticating route too, the first test would still
    pass on its reason code while this one catches the double-guard.

    ONE install per app. ``install_ownership_guard`` is documented idempotent --
    a second call finds the guard already attached and returns ``{}`` -- so
    calling it twice on one app would make this assert the empty diff and pass
    for the wrong reason.
    """
    guarded_open = ownership_guard.install_ownership_guard(_mini_path_route())
    protected = ownership_guard.install_ownership_guard(
        _mini_auth_no_claim_route())

    assert guarded_open == {"/mini/wallet/{player_id}": ["player_id"]}, (
        f"the open route was not guarded: {guarded_open}")
    assert protected == {}, (
        "the guard attached a dependency to a route that already authenticates; "
        "that route is double-guarded and its bookkeeping no longer means "
        "'routes this call changed'")


def test_a_route_with_neither_auth_nor_a_claim_is_still_booked_as_debt():
    """THE OTHER HALF: the ordering must not swallow the real blind spot.

    The fix for the inflated backlog was to ask auth first -- but the honest
    reason for asking is that a route with NEITHER is the only shape the guard
    genuinely cannot evaluate. If the ordering were "improved" into skipping
    every route without a claim regardless of auth, this cell would stop being
    debt and the blind spot would go quiet again.

    Pinned as the negative side of the pair above: one test cannot be moved in
    either direction without the other noticing.
    """
    app = FastAPI()

    @app.get("/probe/open")
    def open_route() -> Dict[str, Any]:
        return {"ok": True}

    ownership_guard.install_ownership_guard(app)
    assert ownership_guard.skipped_route_count(
        ownership_guard.SKIP_NO_PLAYER_ID_CLAIM) == 1, (
        "a route with no auth and no player id stopped being debt. That shape "
        "is the one the shape rule cannot evaluate, and it is exactly what the "
        "ledger exists to surface")
    assert ownership_guard.skipped_route_debt_count() == 1


def test_the_ordering_assertion_cannot_pass_on_a_swapped_installer():
    """NEGATIVE CONTROL: the section above bites, proven every run.

    The rule is one line -- "a self-authenticating route is booked
    already-has-auth" -- so a rule that accepted the wrong value would leave it
    green. This re-implements the installer's two branches in BOTH orders, in
    memory only, and requires the same comparison to reject the swapped one.

    The honest order is asserted FIRST, so a rule that rejected everything
    cannot pass this either. The swap is expressed by evaluating the claim
    question before the auth question, which is precisely the edit that would
    inflate the backlog, and the guarded SET is asserted equal across both --
    which is the claim that makes this ordering bookkeeping-only.
    """
    # The honest order: auth first.
    honest_reasons = _book_reasons(auth_questions_first=True)
    assert honest_reasons == {ownership_guard.SKIP_ALREADY_HAS_AUTH}, honest_reasons

    # The sabotage: claim first. The same route, the same guarded set, and the
    # reason code moves to DEBT.
    swapped_reasons = _book_reasons(auth_questions_first=False)
    assert swapped_reasons == {ownership_guard.SKIP_NO_PLAYER_ID_CLAIM}, (
        swapped_reasons)

    # The claim this section exists to protect: the SET is identical either way.
    assert _guarded_set(auth_questions_first=True) == _guarded_set(
        auth_questions_first=False)

    # And the section's own rule must reject the swapped reading.
    for reasons in (swapped_reasons, honest_reasons):
        if reasons == {ownership_guard.SKIP_ALREADY_HAS_AUTH}:
            continue
        with pytest.raises(AssertionError):
            assert reasons == {ownership_guard.SKIP_ALREADY_HAS_AUTH}


def _book_reasons(auth_questions_first: bool) -> frozenset:
    """The reason code ONE self-authenticating, claim-free route is booked under.

    A faithful re-implementation of the installer's two branches in the given
    order, mirroring ownership_guard.install_ownership_guard branch for branch:

      auth first   (what ships)  has_auth?  -> already-has-auth-dependency
                                       else no params? -> no-player-id-claim
                                       else -> guarded
      claim first  (the sabotage) no params? -> no-player-id-claim   <-- STOPS
                                       has_auth? -> already-has-auth
                                       else -> guarded

    The asymmetry in the claim-first arm is the whole point: a route naming no
    player id is booked as DEBT and the installer moves on, so its existing auth
    is never consulted. That is exactly how the review backlog was inflated 9x.

    It reads the real ``has_auth_dependency`` and the real
    ``player_claim_params``, so it cannot drift from the module into agreeing
    with itself.
    """
    dependant = _route(_mini_auth_no_claim_route(), "/probe/selfguarded").dependant
    has_auth = ownership_guard.has_auth_dependency(dependant)
    params = ownership_guard.player_claim_params(dependant)
    if auth_questions_first:
        if has_auth:
            return frozenset({ownership_guard.SKIP_ALREADY_HAS_AUTH})
        if not params:
            return frozenset({ownership_guard.SKIP_NO_PLAYER_ID_CLAIM})
        return frozenset()
    # Claim first: the no-params answer is FINAL and auth is never asked.
    if not params:
        return frozenset({ownership_guard.SKIP_NO_PLAYER_ID_CLAIM})
    if has_auth:
        return frozenset({ownership_guard.SKIP_ALREADY_HAS_AUTH})
    return frozenset()


def _guarded_set(auth_questions_first: bool) -> frozenset:
    """The paths this installer would attach a dependency to, in either order.

    Three routes, chosen so the two orders are forced to agree -- which is the
    module's own claim, and the reason the ordering is bookkeeping only:

      /probe/open            no auth, no claim  -> skipped, DEBT, both orders
      /probe/selfguarded    auth, no claim     -> skipped, both orders
      /probe/open-claim      no auth, CLAIM     -> GUARDED, both orders

    The third route is the load-bearing one. Without it the helper would have
    no guarded route at all, the two orders would trivially produce the same
    empty set, and the assertion above would compare nothing.

    Mirrors the real installer branch for branch, using the real
    ``has_auth_dependency`` and ``player_claim_params``.
    """
    app = FastAPI()

    @app.get("/probe/open")
    def open_route() -> Dict[str, Any]:
        return {"ok": True}

    @app.get("/probe/selfguarded")
    def self_guarded(identity=Depends(auth_module.require_player)) -> Dict[str, Any]:
        return {"ok": True}

    @app.get("/probe/open-claim/{player_id}")
    def open_claim(player_id: str) -> Dict[str, Any]:
        return {"owner": player_id}

    out = set()
    for route in list(getattr(app, "routes", None) or ()):
        if not isinstance(route, APIRoute):
            continue
        has_auth = ownership_guard.has_auth_dependency(route.dependant)
        params = ownership_guard.player_claim_params(route.dependant)
        if auth_questions_first:
            # auth, then claims
            if has_auth:
                continue
            if not params:
                continue
        else:
            # claims, then auth
            if not params:
                continue
            if has_auth:
                continue
        out.add(route.path)
    return frozenset(out)
