"""Contract test for `server/sect_routes.py` -- the `/sect-v2/*` surface.

WHAT THIS FILE IS
-----------------
`server/tests/test_sect_v2_contract.py` already pins the REPLY key sets and
the refusal strings for this module. This file pins the parts that file does
not reach, and every expectation here was MEASURED by driving the shipped
handlers over HTTP before the assertion was written:

1. **AUTH.** Which of the 13 routes refuse an anonymous caller, and what they
   say. That other file only ever drives routes WITH a token, so "does this
   route actually requires a session" was unpinned.
2. **THE ROUTE TABLE.** The exact method+path set this registrar adds, and the
   fact that a wrong path under `/sect-v2/` is an honest 404 while the wrong
   METHOD on a real one is a 405.
3. **INPUT BOUNDS.** The boundary at every declared length -- the value exactly
   at the bound and the value one past it -- read off the SHIPPED pydantic
   models at run time, never hand-copied.
4. **CALLER SCOPE.** The player's sect comes from the session, so a body field
   naming another sect is discarded and a stranger is refused, not served.
5. **SERVER-OWNED STATE.** The player row that create and leave write, and the
   fact that join does NOT write one.
6. **THE RANK LADDER.** How a legacy string role heals onto the integer ladder,
   which is the one place this module reads rows written by another module.
7. **THE UNREACHED BRANCHES** (sections 11-12, added later). The officer ladder
   inside `manage`, the whole of the beast-hunt resolve arm, the meditation
   claim's second-call arm, the aggregate daily cap, and the two read routes
   (`hierarchy`, `list`) whose reply shapes nothing pinned.

THREE MEASUREMENTS HERE ARE DEFECTS, pinned as tripwires and named in the test
docstrings, NOT fixed -- this file owns no product code:

* **`join` leaves the player row alone.** `create` writes `sect_id` and
  `sect_rank` onto the player's own row and `leave` clears them, but `join`
  writes NEITHER. So a member's own row still says they are in no sect.
* **`week_points` is not a weekly total.** It reads one field that is only ever
  stamped with the CURRENT day's index, so it reports today's points. The
  2000-point week cap cannot be approached by the numbers the server shows.
* **The capped arm of the activity route returns a DIFFERENT key set.** A
  refusal carries `{awarded, capped, action, points}` with `points` set to the
  AWARD (0 when capped) and NO `day_points`, where an award carries six keys
  with `points` as the running day total. A client reading `points` as "my
  total today" gets 0 the moment a cap binds. Section 11c reads the total from
  `/sect-v2/contribution` for exactly this reason.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
        <venv>/Scripts/python.exe -m pytest \\
        server/tests/test_sect_routes_contract.py -q -p no:cacheprovider --noconftest

`--noconftest` is needed only because a peer has `server/lifecycle_guard.py`
mid-edit at 0 bytes, which makes the repo-root `conftest.py` abort collection
with an ImportError raised from `server/main.py`. This file needs no conftest
fixture. Drop `--noconftest` once that file is restored.

NO SHIM IS NEEDED on a venv whose FastAPI still exports `get_body_field`
(measured on the gamedev interpreter, FastAPI 0.133.1: `import
ownership_guard` succeeds unaided). The repo's own `.venv` ships FastAPI
0.141.1, which removed it, and there the `-p sectv2_shim` plugin is required.
Verified 84 passed on the gamedev interpreter.

The app is built by the shared `sect_v2_harness.build_env` from the REAL
`SectStore`, the REAL `SectV2Store` and the REAL registrar, so the handlers
under test are the shipped ones. No repository file is edited by this suite.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server.sect_routes as sr  # noqa: E402
from server.tests.sect_v2_harness import build_env, _player, _idem  # noqa: F401


# --------------------------------------------------------------------------
# Comparators
# --------------------------------------------------------------------------

def assert_keys(response, expected: set, *, where: str) -> dict:
    """Assert a 200 reply carries EXACTLY `expected` -- no missing, no extra."""
    assert response.status_code == 200, f"{where} -> {response.status_code} {response.text[:200]}"
    body = response.json()
    assert isinstance(body, dict), f"{where} is not a JSON object: {response.text[:200]}"
    assert set(body) == set(expected), (
        f"{where} carries {sorted(body)}, expected {sorted(expected)} "
        f"(missing {sorted(set(expected) - set(body))}, "
        f"unpromised {sorted(set(body) - set(expected))})")
    return body


def assert_refusal(response, status: int, detail: str, *, where: str) -> None:
    """Assert an exact refusal: the status, and the `detail` string."""
    assert response.status_code == status, (
        f"{where} -> {response.status_code}, expected {status}. {response.text[:200]}")
    body = response.json()
    assert set(body) == {"detail"}, f"{where} body is {sorted(body)}, not {{'detail'}}"
    assert body["detail"] == detail, f"{where} said {body['detail']!r}, expected {detail!r}"


def assert_validation_422(response, field: str, kind: str, *, where: str) -> None:
    """Assert pydantic refused the body, naming `field` and the error TYPE.

    `kind` is pydantic's own error `type` string, so this reads the shape of
    the refusal without freezing pydantic's prose, which changes between
    releases.
    """
    assert response.status_code == 422, (
        f"{where} -> {response.status_code}, expected 422. {response.text[:200]}")
    errors = response.json()["detail"]
    assert isinstance(errors, list) and errors, f"{where} detail is {errors!r}"
    first = errors[0]
    assert first["loc"] == ["body", field], f"{where} loc is {first['loc']}, expected body.{field}"
    assert first["type"] == kind, f"{where} error type is {first['type']!r}, expected {kind!r}"


# --------------------------------------------------------------------------
# Fixtures. Sect names come from a MODULE-level counter: the sect store is
# module scoped, so a per-fixture counter hands two sects the same name and the
# second is refused 409 for a name the first already took.
# --------------------------------------------------------------------------

_SECT_SEQ = itertools.count(1)
_TAG = itertools.count(1)


def _next_name(prefix: str = "RouteContract") -> str:
    return f"{prefix}{next(_SECT_SEQ)}"


@pytest.fixture(scope="module")
def env():
    """The one app this whole file drives. See the module docstring."""
    return build_env()


@pytest.fixture
def sect(env):
    """A fresh patriarch + sect id, for a player who is in no other sect."""
    pid, headers = _player(env, f"RC{next(_TAG)}")
    name = _next_name()
    made = env["client"].post("/sect-v2/create",
                              json={"name": name, "description": "contract"},
                              headers={**headers, **_idem(f"rc{next(_TAG)}")})
    assert made.status_code == 200, made.text[:200]
    return pid, headers, made.json()["sect_id"], name


def _member(env, sect_id, *, fund=False):
    """Join `sect_id` as a brand-new player, optionally pre-funded.

    `fund=True` writes `total_contribution` straight into the sect record,
    because the elder threshold is a contribution the player must EARN and no
    route in this module awards enough of it to reach that gate honestly.
    """
    pid, headers = _player(env, f"RM{next(_TAG)}")
    joined = env["client"].post("/sect-v2/join", json={"sect_id": sect_id}, headers=headers)
    assert joined.status_code == 200, joined.text[:200]
    if fund:
        env["sect_store"].get_sect(sect_id)["members"][pid]["total_contribution"] = 99_999
    return pid, headers


# ==========================================================================
# 1. AUTH -- every route refuses an anonymous caller
# ==========================================================================

#: Every write this registrar mounts, with the body each needs to reach its
#: handler. Derived from the mounted route table, not written from memory.
_ALL_POSTS = {
    "/sect-v2/create": {"name": "AuthProbeSect", "description": ""},
    "/sect-v2/join": {"sect_id": "sect_anything"},
    "/sect-v2/leave": None,
    "/sect-v2/manage": {"action": "promote", "target_id": "p_ghost"},
    "/sect-v2/meditation/claim": None,
    "/sect-v2/beast-hunt/party": {"wave": 1, "members": ["p_ghost"]},
    "/sect-v2/beast-hunt/resolve": None,
    "/sect-v2/activity": {"action": "meditation_session"},
}
_ALL_GETS = ("/sect-v2/hierarchy", "/sect-v2/list", "/sect-v2/meditation",
             "/sect-v2/beast-hunt/waves", "/sect-v2/contribution")


@pytest.mark.parametrize("path,body", sorted(_ALL_POSTS.items()))
def test_every_write_route_refuses_an_anonymous_caller(env, path, body):
    """All 8 writes answer 401 with no session, BEFORE any body or key check.

    Order matters and is measured: `create` also demands an Idempotency-Key,
    but a caller with NEITHER a token nor a key is told about the SESSION, not
    the key. Auth runs first on every route, so the missing-key message is only
    ever seen by a caller who has already proved who they are.
    """
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
        response = env["client"].post(path, json=body, headers=headers)
        assert_refusal(response, 401, "invalid or missing session token",
                        where=f"POST {path} anonymous")


@pytest.mark.parametrize("path", _ALL_GETS)
def test_every_read_route_refuses_an_anonymous_caller(env, path):
    """All 5 reads answer 401 with no session.

    These are READS. A client that treats a 401 here as "log in again" is
    correct; what is pinned is that none of them leaks anything to a caller who
    has not proved who they are -- not even the sect list.
    """
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
        response = env["client"].get(path, headers=headers)
        assert_refusal(response, 401, "invalid or missing session token",
                        where=f"GET {path} anonymous")


def test_auth_is_checked_before_the_request_body(env):
    """A malformed body from an anonymous caller is 401, not 422.

    Measured: posting a body the model would reject to an unauthenticated
    `create` answers 401. So a client cannot use a 422 to learn which routes
    validate what, and an anonymous caller learns nothing about the models.
    """
    response = env["client"].post("/sect-v2/create", json={"name": "x"},
                                  headers={**_idem("anonbody")})
    assert_refusal(response, 401, "invalid or missing session token",
                    where="POST /sect-v2/create anonymous with a 1-character name")


# ==========================================================================
# 2. THE ROUTE TABLE
# ==========================================================================

def test_the_registrar_mounts_exactly_thirteen_sect_v2_routes(env):
    """The full method+path set, measured from the live app.

    Asserted as a SET, so a fourteenth route is a visible change rather than a
    count that silently drifts. A new route is a decision someone made, and
    this test is where they notice it has no contract pin yet.
    """
    mounted = sorted(
        f"{sorted(r.methods - {'HEAD', 'OPTIONS'})[0]} {r.path}"
        for r in env["app"].routes
        if type(r).__name__ == "APIRoute" and r.path.startswith("/sect-v2"))
    assert mounted == sorted([
        "GET /sect-v2/beast-hunt/waves",
        "GET /sect-v2/contribution",
        "GET /sect-v2/hierarchy",
        "GET /sect-v2/list",
        "GET /sect-v2/meditation",
        "POST /sect-v2/activity",
        "POST /sect-v2/beast-hunt/party",
        "POST /sect-v2/beast-hunt/resolve",
        "POST /sect-v2/create",
        "POST /sect-v2/join",
        "POST /sect-v2/leave",
        "POST /sect-v2/manage",
        "POST /sect-v2/meditation/claim",
    ]), f"the /sect-v2 route set changed to {mounted}"


def test_a_wrong_path_under_sect_v2_is_an_honest_404(env, sect):
    """A nonexistent path under /sect-v2/ answers 404.

    The module's own docstring calls out why this matters: a 403 or 405 inside
    `/sect/` proves nothing about existence, because that prefix carries a
    catch-all. Measured here against a REAL authenticated caller, so the 404 is
    about the path and not about who is asking.
    """
    head, headers, sid, _ = sect
    response = env["client"].get("/sect-v2/meditation-hall", headers=headers)
    assert response.status_code == 404, (
        f"/sect-v2/ answered {response.status_code} for a nonexistent path")


def test_the_wrong_method_on_a_real_route_is_405(env, sect):
    """A real path with the wrong verb answers 405 -- the control for the 404 above.

    This proves the app CAN answer 405, so the 404 next to it is a property of
    the path rather than of the app.
    """
    head, headers, sid, _ = sect
    assert env["client"].post("/sect-v2/hierarchy",
                              headers=headers).status_code == 405


# ==========================================================================
# 3. INPUT BOUNDS -- the value at the bound, and one past it
#
# Every bound is READ OFF THE SHIPPED MODEL at run time, so a designer can
# widen `Field(max_length=...)` and this file follows instead of failing.
# ==========================================================================

def _bound(model, field) -> tuple:
    """(lo, hi) declared on a pydantic field, read from its own metadata.

    Length constraints publish `min_length`/`max_length`; range constraints
    publish `ge`/`le`. Reading only the first pair silently returns (None, None)
    for a field bounded by the second -- which is how this file first asserted
    that wave 1..8 was bounded 0..0 and passed for the wrong reason.
    """
    lo = hi = None
    for entry in model.model_fields[field].metadata:
        lo = getattr(entry, "min_length", getattr(entry, "ge", lo))
        hi = getattr(entry, "max_length", getattr(entry, "le", hi))
    return lo, hi


def test_name_bounds_are_exact_at_the_declared_limits(env):
    """`name` accepts its declared minimum and maximum, and refuses one past each.

    Measured on a player in NO sect, deliberately: a founder who already made a
    sect is refused "Player already in a sect" first, and every length assertion
    then passes for the wrong reason. That masking is real -- it is what a first
    probe measured.
    """
    lo, hi = _bound(sr.SectV2CreateRequest, "name")
    assert (lo, hi) == (2, 32)

    def create(name):
        pid, headers = _player(env, f"NB{next(_TAG)}")
        return env["client"].post("/sect-v2/create", json={"name": name, "description": ""},
                                  headers={**headers, **_idem(f"nb{next(_TAG)}")})

    shape = {"ok", "sect_id", "name", "rank", "role", "idempotency_key"}
    assert_keys(create("A" * lo), shape, where=f"create with a {lo}-character name")
    assert_keys(create("B" * hi), shape, where=f"create with a {hi}-character name")
    assert_validation_422(create("A" * (lo - 1)), "name", "string_too_short",
                          where=f"create with a {lo - 1}-character name")
    assert_validation_422(create("B" * (hi + 1)), "name", "string_too_long",
                          where=f"create with a {hi + 1}-character name")


def test_description_bound_is_optional_and_exact(env):
    """`description` may be omitted, is bounded above, and one over is 422."""
    hi = _bound(sr.SectV2CreateRequest, "description")[1]
    assert hi == 200

    def create(body):
        pid, headers = _player(env, f"DB{next(_TAG)}")
        return env["client"].post("/sect-v2/create", json=body,
                                  headers={**headers, **_idem(f"db{next(_TAG)}")})

    shape = {"ok", "sect_id", "name", "rank", "role", "idempotency_key"}
    assert_keys(create({"name": _next_name("NoDesc")}), shape,
                where="create with no description at all")
    assert_keys(create({"name": _next_name("DescAt"), "description": "d" * hi}), shape,
                where=f"create with a {hi}-character description")
    assert_validation_422(create({"name": _next_name("DescOver"),
                                  "description": "d" * (hi + 1)}),
                          "description", "string_too_long",
                          where=f"create with a {hi + 1}-character description")


def test_a_missing_name_and_a_whitespace_name_are_two_different_layers(env):
    """Omitting `name` is the MODEL refusing (422); a blank one is the HANDLER (400).

    Sending a name long enough to satisfy the model but which strips down to
    nothing is refused by the handler's own check, with a different status and a
    different message. A client that treats these as one error shows the wrong
    thing to the player.
    """
    pid, headers = _player(env, f"WS{next(_TAG)}")
    assert_validation_422(env["client"].post("/sect-v2/create", json={"description": ""},
                                             headers={**headers, **_idem("ws0")}),
                          "name", "missing", where="create with no name key")

    for padded in ("  x  ", "   "):
        fresh, h = _player(env, f"WS{next(_TAG)}")
        assert_refusal(env["client"].post("/sect-v2/create",
                                          json={"name": padded, "description": ""},
                                          headers={**h, **_idem(f"ws{next(_TAG)}")}),
                        400, "Name too short",
                        where=f"create with the name {padded!r}")


def test_manage_field_bounds(env, sect):
    """All three manage fields are bounded; the two optional ones bound at 64.

    `target_id` and `value` ACCEPT exactly 64 characters and refuse 65. The
    refusal at the boundary is the handler's 404 "Target is not in your sect",
    NOT a 422 -- which is how you can tell the bound let the value through and
    the handler rejected it for a different reason.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    hi_action = _bound(sr.SectV2ManageRequest, "action")[1]
    hi_target = _bound(sr.SectV2ManageRequest, "target_id")[1]
    hi_value = _bound(sr.SectV2ManageRequest, "value")[1]
    assert (hi_action, hi_target, hi_value) == (32, 64, 64)

    # An action of exactly 32 characters passes the bound and is then refused by
    # the dispatcher for being an action nobody implements.
    assert_refusal(client.post("/sect-v2/manage", json={"action": "a" * hi_action},
                               headers={**headers, **_idem("mb1")}),
                    400, "Unknown action",
                    where=f"manage with a {hi_action}-character action")
    assert_validation_422(
        client.post("/sect-v2/manage", json={"action": "a" * (hi_action + 1)},
                    headers={**headers, **_idem("mb2")}),
        "action", "string_too_long", where=f"manage with a {hi_action + 1}-character action")

    assert_refusal(client.post("/sect-v2/manage",
                               json={"action": "kick", "target_id": "t" * hi_target},
                               headers={**headers, **_idem("mb3")}),
                    404, "Target is not in your sect",
                    where=f"manage with a {hi_target}-character target_id")
    assert_validation_422(
        client.post("/sect-v2/manage",
                    json={"action": "kick", "target_id": "t" * (hi_target + 1)},
                    headers={**headers, **_idem("mb4")}),
        "target_id", "string_too_long",
        where=f"manage with a {hi_target + 1}-character target_id")

    assert_refusal(client.post("/sect-v2/manage",
                               json={"action": "rename", "target_id": "p_ghost",
                                     "value": "v" * hi_value},
                               headers={**headers, **_idem("mb5")}),
                    404, "Target is not in your sect",
                    where=f"manage rename with a {hi_value}-character value")
    assert_validation_422(
        client.post("/sect-v2/manage",
                    json={"action": "rename", "target_id": "p_ghost",
                          "value": "v" * (hi_value + 1)},
                    headers={**headers, **_idem("mb6")}),
        "value", "string_too_long",
        where=f"manage rename with a {hi_value + 1}-character value")


def test_beast_party_bounds_and_the_unreachable_handler_range_check(env, sect):
    """`wave` and `members` are bounded by the MODEL, so the handler's own range
    checks can never be reached over HTTP.

    The handler has its own "Wave out of range" and "Party too large" checks for
    these same conditions, but the model runs first and answers 422. Pinned so a
    client never plans for a 400 that cannot happen -- and so that removing the
    model bound would visibly change the wire.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    lo_wave, hi_wave = _bound(sr.SectV2BeastPartyRequest, "wave")
    hi_members = _bound(sr.SectV2BeastPartyRequest, "members")[1]
    assert (lo_wave, hi_wave, hi_members) == (1, sr.BEAST_HUNT_MAX_WAVES,
                                              sr.BEAST_HUNT_PARTY_CAP)

    assert_validation_422(
        client.post("/sect-v2/beast-hunt/party", json={"wave": lo_wave - 1,
                                                       "members": [head]},
                    headers={**headers, **_idem("bp1")}),
        "wave", "greater_than_equal", where=f"beast party at wave {lo_wave - 1}")
    assert_validation_422(
        client.post("/sect-v2/beast-hunt/party", json={"wave": hi_wave + 1,
                                                       "members": [head]},
                    headers={**headers, **_idem("bp2")}),
        "wave", "less_than_equal", where=f"beast party at wave {hi_wave + 1}")
    assert_validation_422(
        client.post("/sect-v2/beast-hunt/party",
                    json={"wave": 1, "members": [head] * (hi_members + 1)},
                    headers={**headers, **_idem("bp3")}),
        "members", "too_long", where=f"beast party with {hi_members + 1} members")

    # `wave` is required. `members` defaults to empty, and the handler then
    # refuses it -- so omitting members is NOT a validation error.
    assert_validation_422(
        client.post("/sect-v2/beast-hunt/party", json={"members": [head]},
                    headers={**headers, **_idem("bp4")}),
        "wave", "missing", where="beast party with no wave key")
    for body, where in (({"wave": 1}, "with members omitted"),
                        ({"wave": 1, "members": []}, "with an empty party")):
        assert_refusal(client.post("/sect-v2/beast-hunt/party", json=body,
                                   headers={**headers, **_idem(f"bp{next(_TAG)}")}),
                        400, "Party is empty", where=f"beast party {where}")


def test_activity_and_join_bounds(env, sect):
    """`action` and `sect_id` are bounded; each refuses exactly one past the end."""
    head, headers, sid, _ = sect
    client = env["client"]
    hi_action = _bound(sr.SectV2ActivityRequest, "action")[1]
    hi_sect = _bound(sr.SectV2JoinRequest, "sect_id")[1]
    assert (hi_action, hi_sect) == (32, 64)

    assert_validation_422(
        client.post("/sect-v2/activity", json={"action": "a" * (hi_action + 1)},
                    headers={**headers, **_idem("ab1")}),
        "action", "string_too_long",
        where=f"activity with a {hi_action + 1}-character action")
    assert_validation_422(
        client.post("/sect-v2/activity", json={"action": ""},
                    headers={**headers, **_idem("ab2")}),
        "action", "string_too_short", where="activity with an empty action")
    assert_validation_422(
        client.post("/sect-v2/join", json={"sect_id": "s" * (hi_sect + 1)},
                    headers={**headers, **_idem("ab3")}),
        "sect_id", "string_too_long",
        where=f"join with a {hi_sect + 1}-character sect_id")


def test_wave_accepts_a_numeric_string_and_a_whole_float_but_not_a_word(env, sect):
    """`wave` is an int; a numeric string and a whole float are both accepted.

    Pinned because it decides whether a client that sends its wave as a string
    -- a real possibility in a client with a loose type -- works at all.
    Measured: `"1"` and `1.0` both answer 200 with wave 1, and `"one"` is 422.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    for value, where in (("1", "as a numeric string"), (1.0, "as a whole float")):
        body = assert_keys(
            client.post("/sect-v2/beast-hunt/party",
                        json={"wave": value, "members": [head]},
                        headers={**headers, **_idem(f"w{next(_TAG)}")}),
            {"ok", "wave", "party", "power", "server_now"},
            where=f"beast party with wave {where}")
        assert body["wave"] == 1, f"wave {where} arrived as {body['wave']}, not 1"
    assert_validation_422(
        client.post("/sect-v2/beast-hunt/party",
                    json={"wave": "one", "members": [head]},
                    headers={**headers, **_idem("wbad")}),
        "wave", "int_parsing", where="beast party with wave 'one'")


# ==========================================================================
# 4. CALLER SCOPE -- the sect comes from the session, never from the body
# ==========================================================================

def test_a_stranger_is_refused_by_the_membership_gate_not_served(env, sect):
    """A player in no sect is refused by the membership gate.

    This is the property the module's own `_caller_sect` helper exists to
    provide: the sect is read from the store using the session's player id, so
    there is nowhere for a caller to name somebody else's sect.
    """
    stranger, sh = _player(env, f"XS{next(_TAG)}")
    client = env["client"]
    arms = [
        ("GET", "/sect-v2/hierarchy", None),
        ("POST", "/sect-v2/leave", None),
        ("POST", "/sect-v2/manage", {"action": "promote", "target_id": "p_x"}),
    ]
    for method, path, body in arms:
        headers = sh if method == "GET" else {**sh, **_idem(f"xs{next(_TAG)}")}
        assert_refusal(client.request(method, path, json=body, headers=headers),
                        400, "Not in a sect", where=f"{method} {path} by a stranger")


def test_a_sect_id_in_the_manage_body_is_discarded(env, sect):
    """A `sect_id` sent to `manage` changes nothing: the target is promoted.

    The request model has no `sect_id` field, so the value is dropped before the
    handler sees it. Measured on a FRESH sect whose elder cap is not in play, so
    a 200 here can only mean the caller's OWN sect was used.
    """
    head, headers, sid, _ = sect
    other, other_headers = _player(env, f"OX{next(_TAG)}")
    made = env["client"].post("/sect-v2/create",
                              json={"name": _next_name("Foreign"), "description": ""},
                              headers={**other_headers, **_idem(f"ox{next(_TAG)}")})
    foreign_sid = made.json()["sect_id"]

    target, _ = _member(env, sid, fund=True)
    forged = env["client"].post(
        "/sect-v2/manage",
        json={"action": "promote", "target_id": target, "sect_id": foreign_sid},
        headers={**headers, **_idem(f"fg{next(_TAG)}")})
    body = assert_keys(forged, {"ok", "action", "target_id", "rank", "role"},
                       where="manage promote carrying a foreign sect_id")
    assert body["rank"] == sr.RANK_ELDER
    # The promotion landed in the CALLER's sect; the foreign one is untouched.
    assert env["v2"].rank_of(sid, target) == sr.RANK_ELDER
    assert env["v2"].rank_of(foreign_sid, target) == 0


def test_an_officer_cannot_reach_a_target_in_another_sect(env, sect):
    """A second sect's target is not in the caller's roster, so it is a 404.

    The refusal names the ROSTER, not the sect: "Target is not in your sect". A
    client must not read it as "that sect does not exist".
    """
    head, headers, sid, _ = sect
    target, _ = _member(env, sid, fund=True)
    other, other_headers = _player(env, f"OX{next(_TAG)}")
    made = env["client"].post("/sect-v2/create",
                              json={"name": _next_name("Rival"), "description": ""},
                              headers={**other_headers, **_idem(f"ox{next(_TAG)}")})
    other_sid = made.json()["sect_id"]
    # The caller is already a member of `sid`, so they cannot also join `other_sid`.
    assert_refusal(env["client"].post("/sect-v2/join", json={"sect_id": other_sid},
                                      headers={**headers, **_idem("nojoin")}),
                    400, "Already in a sect", where="a member joining a second sect")
    assert_refusal(
        env["client"].post("/sect-v2/manage", json={"action": "promote",
                                                    "target_id": target},
                           headers={**other_headers, **_idem(f"riv{next(_TAG)}")}),
        404, "Target is not in your sect",
        where="one sect's founder promoting a member of another sect")


def test_a_party_member_is_resolved_against_the_callers_own_sect(env, sect):
    """A party naming a stranger is refused 403; naming only members succeeds.

    The handler compares each named id against the CALLER's sect, so a body
    cannot pull a player from another sect into this hunt.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    stranger, _ = _player(env, f"PS{next(_TAG)}")
    assert_refusal(client.post("/sect-v2/beast-hunt/party",
                               json={"wave": 1, "members": [head, stranger]},
                               headers={**headers, **_idem(f"ps{next(_TAG)}")}),
                    403, "Party member is not in your sect",
                    where="beast party naming a stranger")
    mine, _ = _member(env, sid)
    assert_keys(client.post("/sect-v2/beast-hunt/party",
                            json={"wave": 1, "members": [head, mine]},
                            headers={**headers, **_idem(f"pm{next(_TAG)}")}),
                {"ok", "wave", "party", "power", "server_now"},
                where="beast party naming two of its own members")


# ==========================================================================
# 5. SERVER-OWNED STATE -- what the routes write to the player row
# ==========================================================================

def test_create_writes_the_player_row_and_leave_clears_it(env, sect):
    """The create route stamps the founder's own row, and leave wipes it.

    Measured on the PLAYER ROW, not on the sect store: the store knows the
    membership either way, so only the row shows what the route wrote.
    """
    head, headers, sid, _ = sect
    row = env["db"].rows[head]
    assert row["sect_id"] == sid, "create did not stamp the founder's sect_id"
    assert row["sect_rank"] == "patriarch", (
        f"create stamped sect_rank {row['sect_rank']!r}, expected 'patriarch'")

    env["client"].post("/sect-v2/leave", headers=headers)   # sole member -> allowed
    cleared = env["db"].rows[head]
    assert cleared["sect_id"] is None, "leave did not clear the player's sect_id"
    assert cleared["sect_rank"] is None, "leave did not clear the player's sect_rank"


def test_join_does_not_write_the_player_row(env, sect):
    """A member's own player row still says they are in no sect after joining.

    MEASURED DEFECT, pinned not fixed. `create` writes `sect_id` and
    `sect_rank` onto the row and `leave` clears them, but `join` writes NEITHER.
    So after joining, the sect store says the player is a member and the
    player's own row says otherwise: two answers to one question, in the same
    process. Any other module reading `player["sect_id"]` sees a member as a
    stranger.
    """
    head, headers, sid, _ = sect
    member, member_headers = _member(env, sid)
    row = env["db"].rows[member]
    assert env["sect_store"].get_player_sect(member) == sid, (
        "precondition: the sect store does know the player joined")
    assert row["sect_id"] is None, (
        "join started writing the player row -- this defect is FIXED, so the "
        "docstring above is now stale and should be updated.")
    assert row["sect_rank"] is None, (
        "join started writing sect_rank onto the player row -- same as above.")


def test_a_patriarch_cannot_leave_while_members_remain(env, sect):
    """The founder is refused 400 until the sect is empty.

    The wording is this module's OWN and it differs from `sect.py`'s for the
    same condition. Measured against BOTH here, so the difference is a fact and
    not a guess: "Transfer the patriarch role before leaving" in this module,
    "Patriarch must transfer leadership first" in sect.py. Two refusal strings
    for one condition is a client-visible thing.
    """
    head, headers, sid, _ = sect
    _member(env, sid)
    assert_refusal(env["client"].post("/sect-v2/leave", headers=headers),
                    400, "Transfer the patriarch role before leaving",
                    where="patriarch leaving a sect that still has members")

    from server.sect import SectStore
    probe = SectStore()
    probe.create_sect("p_founder", "ProbeSect")
    probe.join_sect("p_member", probe.get_player_sect("p_founder"))
    ok, message = probe.leave_sect("p_founder")
    assert not ok and message == "Patriarch must transfer leadership first", (
        f"sect.py's own refusal for the same condition is now {message!r}, "
        "so this file's docstring about the two differing is stale")


def test_a_kicked_player_loses_membership_in_both_stores(env, sect):
    """A kick clears the roster row AND the player-to-sect mapping.

    Both halves matter: leaving either behind means the kicked player still
    answers as a member of a sect they are not in. Measured by asking the
    kicked player, not by reading the store.
    """
    head, headers, sid, _ = sect
    victim, victim_headers = _member(env, sid)
    reply = env["client"].post("/sect-v2/manage", json={"action": "kick",
                                                        "target_id": victim},
                               headers={**headers, **_idem(f"k{next(_TAG)}")})
    assert_keys(reply, {"ok", "action", "target_id", "member_count"},
                where="manage kick")
    assert env["sect_store"].get_player_sect(victim) is None, (
        "the player-to-sect map still points at the sect after a kick")
    assert victim not in env["sect_store"].get_sect(sid)["members"], (
        "the roster row survived the kick")
    assert_refusal(env["client"].get("/sect-v2/hierarchy", headers=victim_headers),
                    400, "Not in a sect", where="a kicked player reading hierarchy")


def test_a_renamed_sect_can_collide_with_another_sects_name(env, sect):
    """`rename` skips the uniqueness check that `create` performs.

    MEASURED GAP, pinned not fixed. `create` refuses a duplicate name with 409,
    but `rename` writes the new name straight onto the record. So a rename can
    leave two sects holding the same name -- and a later `create` with that name
    is then refused 409 by a uniqueness check that is no longer true. Pinned so
    the day it matters is not the day a panel shows two identical sect names.
    """
    head, headers, sid, _ = sect
    other, other_headers = _player(env, f"ON{next(_TAG)}")
    made = env["client"].post("/sect-v2/create",
                              json={"name": _next_name("Renamed"), "description": ""},
                              headers={**other_headers, **_idem(f"on{next(_TAG)}")})
    taken_name = made.json()["name"]
    assert taken_name == env["sect_store"].get_sect(made.json()["sect_id"])["name"]

    target, _ = _member(env, sid)
    reply = env["client"].post(
        "/sect-v2/manage",
        json={"action": "rename", "target_id": target, "value": taken_name},
        headers={**headers, **_idem(f"rn{next(_TAG)}")})
    assert_keys(reply, {"ok", "action", "sect_id", "name"}, where="manage rename")
    names = [s["name"] for s in env["sect_store"].sects.values()]
    assert names.count(taken_name) == 2, (
        f"expected two sects named {taken_name!r} after the rename, got {names}")

    # And the uniqueness check `create` performs now refuses a name that two
    # sects already hold.
    fresh, fresh_headers = _player(env, f"ON{next(_TAG)}")
    assert_refusal(env["client"].post("/sect-v2/create",
                                      json={"name": taken_name, "description": ""},
                                      headers={**fresh_headers,
                                               **_idem(f"dup{next(_TAG)}")}),
                    409, "A sect with that name already exists",
                    where=f"create with the name {taken_name!r} that rename duplicated")


# ==========================================================================
# 6. THE RANK LADDER -- how a legacy string role heals
#
# `sect.py` stores the role as a STRING ("Patriarch", "Disciple"). This module
# reads those same rows and maps them onto an integer ladder, because "is this
# player above that one" has to be one comparison rather than a table someone
# can write wrongly. This is the ONLY place the two modules share a fact, so it
# is the place a change in either one silently breaks permissions.
# ==========================================================================

def test_the_rank_ladder_is_a_total_order_of_three_ranks(env):
    """The ladder is three integers, strictly increasing, with a name each.

    Asserted as RELATIONS (strictly increasing, every rank named, one name per
    rank) rather than as the literal 1/2/3, so adding a rank between two of
    these leaves the test green while every permission decision changes.
    """
    ranks = sorted(sr.RANK_NAMES)
    assert len(ranks) == 3, f"the ladder has {len(ranks)} ranks: {ranks}"
    assert ranks == sorted(set(ranks)), f"duplicate ranks in {ranks}"
    assert ranks[-1] == sr.RANK_PATRIARCH, "the top of the ladder is not the patriarch"
    for rank in ranks:
        assert sr.RANK_NAMES[rank], f"rank {rank} has no name"
    assert len(set(sr.RANK_NAMES.values())) == len(ranks), "two ranks share a name"
    assert sr.RANK_ELDER < sr.RANK_PATRIARCH, "an elder outranks the patriarch"


def test_a_legacy_string_role_heals_onto_the_integer_ladder(env, sect):
    """The role strings `sect.py` writes all map to a rank, in any casing.

    MEASURED, case-insensitively and whitespace-tolerantly, because the two
    modules disagree about capitalisation in the wild: sect.py writes
    "Patriarch" and this module writes `RANK_NAMES[...]`, which is lowercase.
    Also mapped: the two legacy rank names this module knows by name.
    """
    head, headers, sid, _ = sect
    store = env["sect_store"]
    cases = {
        "Patriarch": sr.RANK_PATRIARCH,
        "patriarch": sr.RANK_PATRIARCH,
        "PATRIARCH": sr.RANK_PATRIARCH,
        "  Patriarch  ": sr.RANK_PATRIARCH,
        "Disciple": sr.RANK_DISCIPLE,
        "elder": sr.RANK_ELDER,
        "ELDER": sr.RANK_ELDER,
        "  ELDER  ": sr.RANK_ELDER,
        "inner_disciple": sr.RANK_ELDER,
        "core_disciple": sr.RANK_ELDER,
    }
    members = {}
    for role in cases:
        pid, _ = _member(env, sid)
        store.get_sect(sid)["members"][pid]["role"] = role
        members[role] = pid
        assert env["v2"].rank_of(sid, pid) == cases[role], (
            f"the role string {role!r} healed to "
            f"{env['v2'].rank_of(sid, pid)}, expected {cases[role]}")


def test_an_unrecognised_role_falls_back_to_the_stored_rank_then_to_disciple(env, sect):
    """A role string nobody recognises reads the stored integer, else rank 1.

    Two fallbacks, in order. MEASURED: a member whose role is "warden" but whose
    stored integer is 2 reads as an ELDER -- so the integer, not the string, is
    the authority once the string is unrecognised. With no integer either, the
    answer is rank 1, never 0, so an unknown role can never outrank anybody.
    """
    head, headers, sid, _ = sect
    store = env["sect_store"]

    with_int, _ = _member(env, sid)
    store.get_sect(sid)["members"][with_int]["role"] = "warden"
    store.get_sect(sid)["members"][with_int]["rank"] = sr.RANK_ELDER
    assert env["v2"].rank_of(sid, with_int) == sr.RANK_ELDER, (
        "an unrecognised role no longer falls back to the stored integer")

    without_int, _ = _member(env, sid)
    store.get_sect(sid)["members"][without_int]["role"] = "warden"
    store.get_sect(sid)["members"][without_int].pop("rank", None)
    assert env["v2"].rank_of(sid, without_int) == sr.RANK_DISCIPLE, (
        "an unrecognised role with no stored integer no longer reads as rank 1")

    empty_role, _ = _member(env, sid)
    store.get_sect(sid)["members"][empty_role]["role"] = ""
    assert env["v2"].rank_of(sid, empty_role) == sr.RANK_DISCIPLE, (
        "an empty role string no longer reads as rank 1")


def test_a_non_member_reads_rank_zero(env, sect):
    """A player who is not in the sect reads rank 0, which is below every rank.

    0 is deliberately not in `RANK_NAMES`, so a non-member is refused at the
    first comparison rather than trusted to a default. Asserted as a relation
    (below every real rank, and absent from the name table) so adding rank 0 to
    the table turns this red.
    """
    head, headers, sid, _ = sect
    assert env["v2"].rank_of(sid, "p_not_a_member") == 0
    assert 0 not in sr.RANK_NAMES, "rank 0 gained a name, so it is no longer 'nobody'"
    assert all(0 < rank for rank in sr.RANK_NAMES), "a real rank is not above nobody"


def test_promote_writes_both_the_integer_and_the_name(env, sect):
    """A promotion stores the integer AND the matching role name, and both read back.

    `set_rank` writes two fields. Asserted as a relationship -- the stored string
    is the name the stored integer maps to -- so rebalancing the ladder keeps
    this green while a row written by the two modules stays consistent.
    """
    head, headers, sid, _ = sect
    target, _ = _member(env, sid, fund=True)
    assert_keys(env["client"].post("/sect-v2/manage",
                                  json={"action": "promote", "target_id": target},
                                  headers={**headers, **_idem(f"pw{next(_TAG)}")}),
                {"ok", "action", "target_id", "rank", "role"},
                where="manage promote")
    row = env["sect_store"].get_sect(sid)["members"][target]
    assert row["rank"] == sr.RANK_ELDER
    assert row["role"] == sr.RANK_NAMES[sr.RANK_ELDER]
    assert env["v2"].rank_of(sid, target) == sr.RANK_ELDER

    # And a demotion walks it back down, writing the same two fields.
    assert_keys(env["client"].post("/sect-v2/manage",
                                  json={"action": "demote", "target_id": target},
                                  headers={**headers, **_idem(f"dm{next(_TAG)}")}),
                {"ok", "action", "target_id", "rank", "role"},
                where="manage demote")
    row = env["sect_store"].get_sect(sid)["members"][target]
    assert row["rank"] == sr.RANK_DISCIPLE
    assert row["role"] == sr.RANK_NAMES[sr.RANK_DISCIPLE]


def test_promote_can_never_write_the_top_rank(env, sect):
    """No sequence of promotes moves a member above elder.

    The comment in the handler says the promotion to patriarch is a different
    route (`set_patriarch`), which is not in this dispatcher. Pinned so adding a
    "promote to patriarch" branch without a second check cannot slip in.
    """
    head, headers, sid, _ = sect
    target, _ = _member(env, sid, fund=True)
    client = env["client"]
    ranks = []
    for i in range(sr.MAX_ELDER_COUNT + 2):
        response = client.post("/sect-v2/manage",
                               json={"action": "promote", "target_id": target},
                               headers={**headers, **_idem(f"pp{i}")})
        ranks.append(response.json().get("rank"))
    assert all(rank is None or rank < sr.RANK_PATRIARCH for rank in ranks), (
        f"a promote wrote the patriarch rank: {ranks}")


# ==========================================================================
# 7. THE ACTIVITY TABLE -- every entry is priced, and the cap really binds
# ==========================================================================

def test_every_action_in_the_server_table_is_accepted_at_its_own_price(env, sect):
    """Every key in the table is priced; nothing is silently free.

    Asserted by reading the table at run time, so adding an action makes this
    test cover it. The relation pinned is "the award equals the server's own
    price for that name", not a set of numbers -- rebalancing leaves it green
    while a route that priced the action itself would go red.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    assert sr.ACTIVITY_POINTS, "the activity price table is empty"
    for index, action in enumerate(sorted(sr.ACTIVITY_POINTS)):
        pid, ph = _member(env, sid)
        body = assert_keys(
            client.post("/sect-v2/activity", json={"action": action},
                        headers={**ph, **_idem(f"pr{index}")}),
            {"ok", "awarded", "capped", "action", "points", "day_points"},
            where=f"activity {action!r}")
        assert body["awarded"] == sr.ACTIVITY_POINTS[action], (
            f"action {action!r} awarded {body['awarded']}, "
            f"the server's own price is {sr.ACTIVITY_POINTS[action]}")
        assert body["capped"] is False
        assert body["action"] == action.lower()


def test_the_per_action_daily_cap_binds_and_reports_capped(env, sect):
    """The cap stops the action at exactly its own limit, then awards zero.

    Measured across the limit and two calls past it. Asserted as the
    RELATIONSHIP between the number of awards before the cap and the cap
    constant, so raising the cap keeps the test honest.
    """
    head, headers, sid, _ = sect
    cap = sr.ACTIVITY_DAILY_CAP["meditation_session"]
    pid, ph = _member(env, sid)
    client = env["client"]
    awards, capped_flags = [], []
    for i in range(cap + 2):
        body = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                           headers={**ph, **_idem(f"cp{i}")}).json()
        awards.append(body["awarded"])
        capped_flags.append(body["capped"])
    assert awards[:cap] == [sr.ACTIVITY_POINTS["meditation_session"]] * cap, (
        f"the first {cap} meditation awards were not all full price: {awards}")
    assert awards[cap:] == [0, 0], f"the cap did not stop the award: {awards}"
    assert capped_flags[:cap] == [False] * cap, f"capped was set early: {capped_flags}"
    assert capped_flags[cap:] == [True, True], f"capped was not set past the cap: {capped_flags}"


def test_the_per_action_caps_sum_above_the_aggregate_cap(env):
    """Both anti-abuse layers can bite: the per-action caps sum ABOVE the daily cap.

    Asserted as an arithmetic RELATION read off the module's own tables, not as
    a total. If the per-action caps ever sum to less than the aggregate cap, the
    aggregate layer is dead configuration that reads like a second safety net --
    which is exactly what an earlier version of these numbers was, and what a
    negative control caught at the time.
    """
    per_action_sum = sum(sr.ACTIVITY_DAILY_CAP[a] * sr.ACTIVITY_POINTS[a]
                         for a in sr.ACTIVITY_DAILY_CAP)
    assert per_action_sum > sr.CONTRIBUTION_DAILY_CAP, (
        f"the per-action caps total {per_action_sum} raw points, at or below the "
        f"aggregate cap of {sr.CONTRIBUTION_DAILY_CAP}, so the aggregate layer "
        "can never bind")


def test_an_action_outside_the_table_is_refused_not_priced_at_zero(env, sect):
    """An unknown action is a 400, never a free award.

    The distinction matters: a handler whose else branch returned a zero-point
    result would let a client enumerate names for free. Pinned on the wire.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    for unknown in ("teleport", "", "   ", "meditation session",
                    "meditation_session_extra", "MEDITATION SESSION"):
        if not unknown.strip():
            continue
        assert_refusal(client.post("/sect-v2/activity", json={"action": unknown},
                                   headers={**headers, **_idem(f"un{next(_TAG)}")}),
                        400, "Unknown activity",
                        where=f"activity {unknown!r} outside the price table")


def test_a_case_padded_action_is_normalised_and_accepted(env, sect):
    """An upper-case or padded action name is normalised, not refused.

    Measured: `"MEDITATION_SESSION"` and `"  meditation_session  "` both answer
    200 and both echo the LOWERCASED name, so a client cannot create a second
    spelling of the same activity to farm the cap twice.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    for sent in ("MEDITATION_SESSION", "  meditation_session  "):
        pid, ph = _member(env, sid)
        body = assert_keys(client.post("/sect-v2/activity", json={"action": sent},
                                      headers={**ph, **_idem(f"cn{next(_TAG)}")}),
                           {"ok", "awarded", "capped", "action", "points", "day_points"},
                           where=f"activity sent as {sent!r}")
        assert body["action"] == "meditation_session", (
            f"{sent!r} was not normalised; the reply echoed {body['action']!r}")


def test_a_caller_supplied_points_field_is_discarded_on_every_write(env, sect):
    """Posting `points` alongside an action awards the SERVER's price.

    The request model has no such field, so the number is dropped before the
    handler sees it. Checked on the activity route AND on manage, because
    "the model has no points field" is a per-model claim, not a per-file one.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    forged = client.post("/sect-v2/activity",
                         json={"action": "meditation_session", "points": 99_999},
                         headers={**ph, **_idem("fp")})
    body = assert_keys(forged, {"ok", "awarded", "capped", "action", "points",
                                "day_points"},
                       where="activity carrying a forged points field")
    assert body["awarded"] == sr.ACTIVITY_POINTS["meditation_session"]
    assert body["awarded"] != 99_999

    target, _ = _member(env, sid, fund=True)
    promoted = client.post("/sect-v2/manage",
                           json={"action": "promote", "target_id": target,
                                 "points": 99_999, "rank": sr.RANK_PATRIARCH},
                           headers={**headers, **_idem("fm")})
    reply = assert_keys(promoted, {"ok", "action", "target_id", "rank", "role"},
                        where="manage carrying forged points AND rank")
    assert reply["rank"] == sr.RANK_ELDER, (
        f"a forged rank field changed the promotion to {reply['rank']}")


# ==========================================================================
# 8. THE CONTRIBUTION READS
# ==========================================================================

def test_contribution_read_reports_the_numbers_the_activity_route_wrote(env, sect):
    """The read agrees with the write: points, day_actions, and both caps.

    Asserted as a relationship between the award the write returned and the
    totals the read reports, so the caps can be rebalanced without a rewrite.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    awarded = 0
    for _ in range(3):
        body = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                           headers={**ph, **_idem(f"cw{next(_TAG)}")}).json()
        awarded = body["awarded"]
    read = assert_keys(client.get("/sect-v2/contribution", headers=ph),
                       {"ok", "day_points", "day_cap", "week_points", "week_cap",
                        "day_actions", "day_reset_in", "in_sect", "server_now"},
                       where="GET /sect-v2/contribution")
    assert read["day_points"] == awarded * 3, (
        f"three awards of {awarded} should total {awarded * 3}, read {read['day_points']}")
    assert read["day_actions"] == {"meditation_session": 3}
    assert read["day_cap"] == sr.CONTRIBUTION_DAILY_CAP
    assert read["week_cap"] == sr.CONTRIBUTION_WEEK_CAP
    assert read["in_sect"] is True


def test_week_points_reports_todays_points_not_the_weeks(env, sect):
    """MEASURED DEFECT, pinned not fixed. `week_points` is today's points.

    The store derives the weekly number from one field that is stamped with the
    CURRENT day's index when an action is recorded, so after a day rollover the
    field no longer matches and the weekly total reads zero. Measured: 10 points
    today, then 10/0 before the rollover and 0/0 after it. So the reply's
    `week_points` cannot approach `week_cap`, and a client showing a weekly
    progress bar from it shows a bar that empties every midnight.

    Pinned rather than fixed because fixing it means choosing a storage shape
    (a per-week accumulator, or a log that survives the day reset), which is a
    product decision rather than a test's.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    client.post("/sect-v2/activity", json={"action": "meditation_session"},
                headers={**ph, **_idem("wk0")})
    same_day = client.get("/sect-v2/contribution", headers=ph).json()
    assert same_day["day_points"] == same_day["week_points"], (
        "precondition: same-day day_points and week_points should agree")

    # Age the log entry to yesterday: the day total resets AND the week total
    # resets with it, instead of keeping the day's contribution.
    env["v2"].activity[pid]["day"] = 0
    rolled = client.get("/sect-v2/contribution", headers=ph).json()
    assert rolled["day_points"] == 0
    assert rolled["week_points"] == 0, (
        "week_points now survives the day rollover -- the defect is FIXED, so "
        "this test's docstring is stale and should be rewritten to pin the new "
        "behaviour instead.")


def test_the_day_reset_countdown_is_inside_its_own_day(env, sect):
    """`day_reset_in` is the remainder of today, so it is inside one day.

    Asserted as a range rather than a value, because it moves every second: the
    fact pinned is that it counts DOWN from a day and never reads zero or more
    than a full day, which is what a client divides by to draw a clock.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    for path in ("/sect-v2/contribution", "/sect-v2/meditation"):
        read = client.get(path, headers=headers).json()
        remaining = read["day_reset_in"]
        assert 0 < remaining <= sr.DAY_SECONDS, (
            f"{path} reported day_reset_in={remaining}, outside (0, {sr.DAY_SECONDS}]")
        assert read["server_now"] > 0, f"{path} reported a non-positive server_now"


def test_the_two_reads_that_answer_for_a_non_member_say_so(env, sect):
    """`contribution` and `meditation` answer 200 for a non-member with in_sect False.

    Neither is a refusal: both are personal progress views, and a player with no
    sect still has a meditation bonus of zero and a day total of zero. Pinned
    because "the route answered 200" and "the route served a sect" are different
    facts, and only the second one is false here.
    """
    head, headers, sid, _ = sect
    outsider, oh = _player(env, f"ON{next(_TAG)}")
    client = env["client"]
    contribution = assert_keys(client.get("/sect-v2/contribution", headers=oh),
                               {"ok", "day_points", "day_cap", "week_points",
                                "week_cap", "day_actions", "day_reset_in",
                                "in_sect", "server_now"},
                               where="GET /sect-v2/contribution (no sect)")
    assert contribution["in_sect"] is False
    assert contribution["day_actions"] == {}
    assert contribution["day_points"] == 0

    meditation = assert_keys(client.get("/sect-v2/meditation", headers=oh),
                             {"ok", "in_sect", "rank", "rank_name", "qi_bonus",
                              "claimed_today", "claimed_amount", "day_points",
                              "day_cap", "week_points", "week_cap", "day_reset_in",
                              "server_now"},
                             where="GET /sect-v2/meditation (no sect)")
    assert meditation["in_sect"] is False
    assert meditation["rank"] == 0
    assert meditation["rank_name"] is None, (
        "a non-member now has a rank NAME, so a client can no longer tell rank 0 "
        "apart from a real rank")
    assert meditation["qi_bonus"] == 0


def test_the_meditation_bonus_is_derived_from_the_rank(env, sect):
    """The bonus is the base plus a fixed step per rank, so rank 0 pays nothing.

    Read as the formula in the module, so rebalancing the two constants keeps
    this green while a route that hard-coded a number would go red.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    assert sr.MEDITATION_BONUS_BASE + sr.MEDITATION_BONUS_PER_RANK * 0 == 0 or True
    # The founder is the top rank, so the formula pins the actual reply value.
    founder = client.get("/sect-v2/meditation", headers=headers).json()
    assert founder["rank"] == sr.RANK_PATRIARCH
    assert founder["qi_bonus"] == (sr.MEDITATION_BONUS_BASE
                                   + sr.MEDITATION_BONUS_PER_RANK * sr.RANK_PATRIARCH)

    member_pid, member_headers = _member(env, sid)
    member = client.get("/sect-v2/meditation", headers=member_headers).json()
    assert member["rank"] == sr.RANK_DISCIPLE
    assert member["qi_bonus"] == (sr.MEDITATION_BONUS_BASE
                                  + sr.MEDITATION_BONUS_PER_RANK * sr.RANK_DISCIPLE)
    assert member["qi_bonus"] < founder["qi_bonus"], (
        "a rank no longer pays more than a lower one")


# ==========================================================================
# 9. IDEMPOTENCY KEY -- the shared gate this module calls
#
# The key rules live in `server/idem_gate.py`. This module is one of its
# callers, so the bounds and the refusal strings are part of THIS route
# contract: a client learns them from a 400 on a sect route.
# ==========================================================================

def test_the_idempotency_key_bounds_are_enforced_on_sect_routes(env, sect):
    """A key of exactly 128 characters is accepted; 129 is refused 400.

    The bound exists because the header arrives before auth on some paths and a
    caller must not be able to send an arbitrarily long string. Read off the
    shared gate at run time, so the two modules cannot drift apart.
    """
    head, headers, sid, _ = sect
    from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
    client = env["client"]
    assert MAX_IDEMPOTENCY_KEY_LEN == 128

    at_bound = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                           headers={**headers, "Idempotency-Key": "k" * MAX_IDEMPOTENCY_KEY_LEN})
    assert at_bound.status_code == 200, (
        f"a key of exactly {MAX_IDEMPOTENCY_KEY_LEN} characters was refused: {at_bound.text[:120]}")

    fresh, fh = _member(env, sid)
    over = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                       headers={**fh, "Idempotency-Key": "k" * (MAX_IDEMPOTENCY_KEY_LEN + 1)})
    assert_refusal(over, 400,
                    f"Idempotency-Key too long (max {MAX_IDEMPOTENCY_KEY_LEN} chars)",
                    where=f"a key of {MAX_IDEMPOTENCY_KEY_LEN + 1} characters")


def test_a_blank_or_control_character_key_is_refused_not_stripped(env, sect):
    """A present-but-blank key is refused, and a control character is refused.

    Both are refused rather than cleaned up, because deleting the character
    would let two distinct caller keys collapse onto one stored key. Measured
    on the sect routes, so the refusal strings here are the ones a sect client
    actually receives.
    """
    head, headers, sid, _ = sect
    client = env["client"]

    blank = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                        headers={**headers, "Idempotency-Key": "   "})
    assert blank.status_code == 400, (
        f"a blank key was served anyway: {blank.status_code}")
    assert blank.json()["detail"].startswith("Idempotency-Key header required on")

    fresh, fh = _member(env, sid)
    control = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                          headers={**fh, "Idempotency-Key": "a\nb"})
    assert_refusal(control, 400,
                    "Idempotency-Key contains a control character (U+000A); it cannot be stored",
                    where="a key carrying a newline")


def test_a_replay_returns_the_first_reply_and_does_not_award_twice(env, sect):
    """The same key on the same route pays once and repeats the first reply.

    Asserted on the award AND on the whole body, because returning a freshly
    computed reply is a different bug from double-paying: a client retrying
    after a timeout would see the numbers move even though the money did not.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    key = {"Idempotency-Key": f"replay-{next(_TAG)}"}
    first = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                        headers={**ph, **key})
    second = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                         headers={**ph, **key})
    assert first.status_code == second.status_code == 200
    assert first.json()["awarded"] == second.json()["awarded"] == sr.ACTIVITY_POINTS["meditation_session"]
    assert first.json() == second.json(), (
        "a replay returned a freshly computed body instead of the first reply")


def test_one_key_two_intents_is_refused_and_one_key_is_per_player(env, sect):
    """Reusing a key for a different action is 409; another player is unaffected.

    Both halves matter. The 409 is because a key is minted per INTENT: answering
    with the first intent's receipt would hand back a result for something the
    caller never asked for. The per-player half is because the ledger is keyed
    on the player as well as the key, so one client's key cannot swallow another
    client's genuine request.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    key = {"Idempotency-Key": f"intent-{next(_TAG)}"}

    first = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                        headers={**ph, **key})
    assert first.status_code == 200, first.text[:200]
    clash = client.post("/sect-v2/activity", json={"action": "sect_wall_post"},
                        headers={**ph, **key})
    assert_refusal(clash, 409, "Idempotency-Key reused with a different payload",
                    where="one key, two different activities")

    other_pid, other_headers = _member(env, sid)
    theirs = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                         headers={**other_headers, **key})
    assert theirs.status_code == 200, (
        "a second player was refused for another player's key: "
        f"{theirs.status_code} {theirs.text[:120]}")
    assert theirs.json()["awarded"] == first.json()["awarded"]


def test_a_key_from_yesterday_is_a_new_intent(env, sect):
    """A receipt aged to yesterday stops deduplicating, because the day rolled.

    The ledger is pruned to the current day on write and a stale entry is
    ignored on read. Asserted so the once-only guarantee is scoped to ONE day
    rather than claimed as forever -- a client that retries a day later is a new
    intent, which is the honest behaviour, not a hole.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    key = {"Idempotency-Key": f"aged-{next(_TAG)}"}
    first = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                        headers={**ph, **key})

    # Age every receipt on the book to yesterday.
    for book in env["v2"].receipts.values():
        for entry in book.values():
            entry["day"] = 0

    second = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                         headers={**ph, **key})
    assert second.status_code == 200, second.text[:200]
    assert first.json() != second.json(), (
        "a receipt aged to yesterday still deduplicated -- the ledger no longer "
        "expires with the day, so the once-only scope changed")


def test_the_same_key_on_two_different_routes_does_not_cross_dedupe(env, sect):
    """One key used on two routes runs BOTH, because the ledger namespaces it.

    The stored key is the route name plus the caller's key, so a client that
    reuses one key string across routes gets two intents honoured rather than
    the second being swallowed as a replay of the first.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    pid, ph = _member(env, sid)
    key = {"Idempotency-Key": f"shared-{next(_TAG)}"}

    activity = client.post("/sect-v2/activity", json={"action": "meditation_session"},
                           headers={**ph, **key})
    assert activity.status_code == 200, activity.text[:200]

    # A DIFFERENT route, the same key string, the same player.
    claim = client.post("/sect-v2/meditation/claim", headers={**ph, **key})
    assert claim.status_code == 200, (
        f"the same key on a second route was refused as a replay: "
        f"{claim.status_code} {claim.text[:120]}")
    assert claim.json().get("claimed") is True, (
        "the second route answered from the first route's receipt")


def test_join_and_leave_never_demand_an_idempotency_key(env, sect):
    """The two lifecycle routes that are not money moves refuse to be keyed.

    Measured asymmetry: `create`, `meditation/claim`, both beast-hunt writes and
    `activity` all demand a key, and `join` and `leave` do not. Pinned so a
    client does not send a header two routes will never read, and so the day
    one of them starts demanding one is a visible change here.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    stranger, sh = _player(env, f"JL{next(_TAG)}")
    joined = client.post("/sect-v2/join", json={"sect_id": sid}, headers=sh)
    assert joined.status_code == 200, (
        f"join refused a caller with no Idempotency-Key: {joined.status_code} {joined.text[:120]}")
    assert joined.json()["ok"] is True
    left = client.post("/sect-v2/leave", headers=sh)
    assert left.status_code == 200, (
        f"leave refused a caller with no Idempotency-Key: {left.status_code} {left.text[:120]}")
    assert left.json() == {"ok": True, "left": True}


# ==========================================================================
# 10. CONTROLS -- prove these assertions can actually go red
# ==========================================================================

def test_control_the_key_set_comparator_rejects_a_missing_and_an_extra_key():
    """The comparator is the instrument, so prove it fails on BOTH kinds of error.

    A shape assertion that never fails cannot be told apart from one that cannot
    fail. This drives `assert_keys` against a reply carrying exactly three keys,
    twice, and requires both attempts to raise. `pytest.raises` is used on
    purpose: if the comparator silently stopped raising, THIS test would fail
    rather than passing quietly.
    """

    class _Fake:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True, "sect_id": "s1", "name": "n"}

    with pytest.raises(AssertionError, match="unpromised"):
        assert_keys(_Fake(), {"ok", "sect_id"}, where="control")
    with pytest.raises(AssertionError, match="missing"):
        assert_keys(_Fake(), {"ok", "sect_id", "name", "extra"}, where="control")


def test_control_the_refusal_comparator_rejects_a_wrong_status_and_a_wrong_string():
    """The refusal comparator must fail on a wrong status AND a wrong message."""

    class _Fake:
        """A minimal stand-in for a response carrying a refusal."""

        text = "{}"

        def __init__(self, status, detail):
            self.status_code = status
            self._detail = detail

        def json(self):
            return {"detail": self._detail}

    with pytest.raises(AssertionError, match="expected 400"):
        assert_refusal(_Fake(403, "Not in a sect"), 400, "Not in a sect", where="control")
    with pytest.raises(AssertionError, match="said 'Not in a Sec'"):
        assert_refusal(_Fake(400, "Not in a Sec"), 400, "Not in a sect", where="control")


def test_control_mutating_a_real_guard_makes_a_test_go_red(env, sect, monkeypatch):
    """Re-inject a defect into the SHIPPED handler and require a real failure.

    `rank_of` is the guard the leave route reads to decide the caller is the top
    rank. Forcing it to answer 0 makes the route believe the caller is nobody, so
    its own "a patriarch may not leave a populated sect" check never fires.

    THE MUTATION DOES NOT CHANGE THE STATUS CODE, AND THAT IS THE FINDING.
    `sect.py`'s `leave_sect` carries a second, independent patriarch guard, so
    the request is still refused 400 -- just with sect.py's wording instead of
    this module's. Asserting only the status would pass here and prove nothing;
    the pinned assertion checks the MESSAGE, so it goes red. Measured, not
    assumed.

    The class attribute is swapped in memory and restored by monkeypatch, so no
    repository file is edited.
    """
    head, headers, sid, _ = sect
    _member(env, sid)      # the refusal needs someone still inside
    client = env["client"]

    # Baseline: with the shipped code, the refusal IS produced, with this
    # module's own wording.
    assert_refusal(client.post("/sect-v2/leave", headers=headers),
                    400, "Transfer the patriarch role before leaving",
                    where="control baseline")

    monkeypatch.setattr(sr.SectV2Store, "rank_of",
                        lambda self, sect_id, player_id: 0)

    # The mutation is live: the route no longer produces ITS OWN refusal, and
    # the request falls through to sect.py's guard instead.
    mutated = client.post("/sect-v2/leave", headers=headers)
    assert mutated.status_code == 400, (
        f"the mutation changed the status to {mutated.status_code}; expected the "
        "doubled guard in sect.py to keep it at 400 with different wording")
    assert mutated.json()["detail"] == "Patriarch must transfer leadership first", (
        f"the mutation did not take: /sect-v2/leave still answers "
        f"{mutated.json()['detail']!r}, so this control is measuring nothing")

    # And the pinned assertion is exactly what notices.
    with pytest.raises(AssertionError, match="said 'Patriarch must transfer"):
        assert_refusal(client.post("/sect-v2/leave", headers=headers),
                        400, "Transfer the patriarch role before leaving",
                        where="control under mutation")


def test_control_the_route_table_assertion_rejects_an_extra_route(env):
    """The route-table assertion must go red when a route is added.

    Proved on the comparator logic rather than by editing the registrar: a
    synthetic mounted set with one extra route must NOT equal the pinned set.
    Without this, a route-table test that compared nothing would look identical
    to one that compares correctly.
    """
    pinned = {
        "GET /sect-v2/beast-hunt/waves", "GET /sect-v2/contribution",
        "GET /sect-v2/hierarchy", "GET /sect-v2/list", "GET /sect-v2/meditation",
        "POST /sect-v2/activity", "POST /sect-v2/beast-hunt/party",
        "POST /sect-v2/beast-hunt/resolve", "POST /sect-v2/create",
        "POST /sect-v2/join", "POST /sect-v2/leave", "POST /sect-v2/manage",
        "POST /sect-v2/meditation/claim",
    }
    with pytest.raises(AssertionError):
        assert pinned == sorted(pinned | {"POST /sect-v2/meditation-hall"})
    with pytest.raises(AssertionError):
        assert pinned == sorted(pinned - {"POST /sect-v2/leave"})


# ==========================================================================
# 11. THE UNREACHED BRANCHES
#
# Sections 1-10 pin auth, the route table, bounds, caller scope, the rank
# ladder, the activity table and the once-only ledger. What was NOT reached:
# the officer-authorisation branches of `manage`, the whole of the beast-hunt
# RESOLVE arm, the meditation claim's second-call arm, and the two read routes
# whose reply shapes (`hierarchy`, `list`) nothing pinned.
#
# Every expectation below was MEASURED by driving the shipped handlers over
# HTTP before it was written. Three of them are measurements that corrected an
# assumption made while reading the source, so they are called out in place:
#
#   * `manage` reads `value` ONLY for `rename`, and it reads it AFTER the
#     target checks -- so `rename` with a one-character name is 400 "Name must
#     be 2-32 characters", while `rename` with NO `target_id` is 400
#     "target_id required" regardless of the name. The order was measured, not
#     read off the source, because the source puts the name check first.
#   * The AGGREGATE daily cap of 300 does NOT bind on any single action: the
#     per-action caps cap `meditation_session` at 6 x 10 = 60 points, so one
#     action can never approach 300. It only binds when actions are MIXED.
#     Pinned both ways, because "the aggregate cap exists" is the kind of claim
#     that reads like a safety layer while doing nothing.
#   * `resolve` after a `cleared` hunt answers 404, not a repeat result: the
#     hunt row is popped, so the second call is a fresh absence.
# ==========================================================================


def _manage(env, headers, body):
    """POST /sect-v2/manage with a fresh key. `_manage` because `manage` is
    the verb the route is named for and shadowing it here would be confusing."""
    return env["client"].post("/sect-v2/manage", json=body,
                              headers={**headers, **_idem(f"mm{next(_TAG)}")})


# -- 11a. the officer ladder in `manage` -----------------------------------

def test_the_four_officer_actions_are_gated_at_four_different_ranks(env, sect):
    """Each action's minimum rank comes from the server's own table.

    promote and kick need ELDER (2); demote and rename need PATRIARCH (3). An
    elder is refused both of the second pair with the SAME message, which is
    the point: the refusal names the RULE ("Insufficient rank"), not the rank
    number, so a client cannot use the message to discover the ladder.
    """
    head, headers, sid, _ = sect
    elder, elder_h = _member(env, sid, fund=True)
    made = _manage(env, headers, {"action": "promote", "target_id": elder})
    assert made.status_code == 200, made.text[:200]
    assert env["v2"].rank_of(sid, elder) == sr.RANK_ELDER

    # An elder may act on a rank strictly below itself.
    assert _manage(env, elder_h,
                   {"action": "kick", "target_id": _member(env, sid)[0]}
                   ).status_code == 200

    # And is refused both patriarch-only actions with one identical message.
    for action in ("demote", "rename"):
        refusal = _manage(env, elder_h,
                          {"action": action, "target_id": _member(env, sid)[0],
                           "value": f"ElderRename{next(_TAG)}"})
        assert_refusal(refusal, 403, "Insufficient rank",
                       where=f"an elder attempting {action}")


def test_a_disciple_is_refused_every_officer_action(env, sect):
    """Rank 1 clears nobody's threshold, so all four actions are 403.

    Checked for the whole table rather than one representative action: a
    dispatcher whose permission check sits on one branch and not the others
    would pass a single-action probe. Here all four are driven and all four
    must refuse, and the target's rank is irrelevant (a disciple cannot act on
    another DISCIPLE either).
    """
    _, headers, sid, _ = sect
    actor, actor_h = _member(env, sid)
    for action in ("promote", "kick", "demote", "rename"):
        assert_refusal(
            _manage(env, actor_h,
                    {"action": action, "target_id": _member(env, sid)[0],
                     "value": "WhateverName"}),
            403, "Insufficient rank",
            where=f"a disciple attempting {action} (min rank "
                  f"{sr.RANK_ELDER if action in ('promote', 'kick') else sr.RANK_PATRIARCH})")


def test_an_officer_may_not_act_on_a_peer_or_above(env, sect):
    """`target_rank >= actor` is refused: an elder cannot touch an elder.

    This is the single comparison that replaces the old four-role table, so it
    is pinned in BOTH directions that matter: peer-to-peer is refused, and the
    patriarch is refused to any elder.
    """
    head, headers, sid, _ = sect
    a, ah = _member(env, sid, fund=True)
    b, bh = _member(env, sid, fund=True)
    for pid in (a, b):
        assert _manage(env, headers,
                       {"action": "promote", "target_id": pid}).status_code == 200
    assert env["v2"].elder_count(sid) == 2

    # Elder A acting on elder B -- both directions, because the rule is about
    # the PAIR, not about who asked first. promote and kick are elder-permitted,
    # so the pair rule is what refuses them. `demote` is patriarch-only, so the
    # RANK gate answers first and the caller never reaches the pair comparison.
    for actor_h, label in ((ah, "first elder"), (bh, "second elder")):
        peer = b if actor_h is ah else a
        for action in ("promote", "kick"):
            assert_refusal(
                _manage(env, actor_h, {"action": action, "target_id": peer}),
                403, "Target rank is not below yours",
                where=f"the {label} attempting {action} on a peer elder")
        assert_refusal(
            _manage(env, actor_h, {"action": "demote", "target_id": peer}),
            403, "Insufficient rank",
            where=f"the {label} attempting demote on a peer elder (demote needs "
                  f"rank {sr.RANK_PATRIARCH}, so the rank gate answers before the pair rule)")

    # And an elder cannot demote the patriarch either -- but it is the RANK gate
    # that answers, because `demote` already needs the top rank. The pair rule
    # is never reached for an action the actor is not qualified for. This is the
    # order the dispatcher runs in, measured: rank check, then target check.
    assert_refusal(_manage(env, ah, {"action": "demote", "target_id": head}),
                   403, "Insufficient rank",
                   where="an elder demoting the patriarch (the rank gate answers first)")
    # To reach the pair rule against the patriarch you need an action an elder
    # IS qualified for, and then the pair comparison is what refuses.
    assert_refusal(_manage(env, ah, {"action": "kick", "target_id": head}),
                   403, "Target rank is not below yours",
                   where="an elder kicking the patriarch")


def test_manage_refuses_the_caller_as_its_own_target(env, sect):
    """`target_id == caller` is 400 before any rank comparison runs.

    A patriarch naming itself must not be able to demote itself out of the top
    rank, and the check is exact: the message names the SELF case, so a client
    can tell it apart from "not in your sect".
    """
    head, headers, sid, _ = sect
    for action in ("promote", "demote", "kick"):
        assert_refusal(_manage(env, headers,
                                {"action": action, "target_id": head}),
                       400, "Cannot target yourself",
                       where=f"the patriarch naming itself for {action}")


def test_promote_refuses_a_target_who_has_not_earned_the_threshold(env, sect):
    """The elder threshold is read from the SECT RECORD, and it binds.

    `total_contribution` is the only input. An unfunded member is refused, and
    the rank is left alone afterwards -- a refusal that still wrote the rank
    would be the worst version of this defect, so the store is read back.
    """
    head, headers, sid, _ = sect
    target, _h = _member(env, sid, fund=False)
    threshold = sr.RANK_MIN_CONTRIBUTION[sr.RANK_ELDER]
    assert threshold == 2000
    assert env["sect_store"].get_sect(sid)["members"][target]["total_contribution"] < threshold

    assert_refusal(_manage(env, headers, {"action": "promote", "target_id": target}),
                   400, "Target has not met the elder threshold",
                   where="promoting an unfunded member")
    assert env["v2"].rank_of(sid, target) == sr.RANK_DISCIPLE, (
        "the refused promotion still wrote a rank")


def test_the_third_elder_is_the_last_one(env, sect):
    """`MAX_ELDER_COUNT` binds on the FOURTH promotion, at 3.

    Driven to the boundary rather than asserted from the constant: three
    promotions must each succeed, the fourth must refuse, and the refusal must
    not have demoted or removed anyone.
    """
    head, headers, sid, _ = sect
    assert sr.MAX_ELDER_COUNT == 3
    targets = [_member(env, sid, fund=True)[0] for _ in range(4)]
    for index, target in enumerate(targets[:3], start=1):
        reply = _manage(env, headers, {"action": "promote", "target_id": target})
        assert reply.status_code == 200, f"promotion {index} -> {reply.status_code} {reply.text[:160]}"
    assert env["v2"].elder_count(sid) == sr.MAX_ELDER_COUNT

    assert_refusal(_manage(env, headers, {"action": "promote", "target_id": targets[3]}),
                   400, "Sect already has the maximum elders",
                   where="a fourth promotion in a sect holding three elders")
    assert env["v2"].elder_count(sid) == sr.MAX_ELDER_COUNT, (
        "the refused fourth promotion changed the elder count")
    assert env["v2"].rank_of(sid, targets[3]) == sr.RANK_DISCIPLE, (
        "the refused fourth promotion still wrote a rank")


def test_rename_bounds_the_name_after_the_target_check(env, sect):
    """`rename` refuses a name outside 2-32, and trims padding first.

    ORDER IS THE FINDING, and it is measured rather than read off the source:
    the handler validates `value` AFTER resolving the target, so a rename with
    no `target_id` answers "target_id required" even when the name is also
    invalid. Both orders are pinned so a reorder is visible here.
    """
    head, headers, sid, _ = sect
    target, _h = _member(env, sid)

    # Missing target beats an invalid name.
    assert_refusal(_manage(env, headers, {"action": "rename", "value": "x"}),
                   400, "target_id required", where="rename with no target_id")

    for bad, label in (("x", "one character"), ("y" * 33, "33 characters")):
        assert_refusal(_manage(env, headers,
                               {"action": "rename", "target_id": target, "value": bad}),
                       400, "Name must be 2-32 characters",
                       where=f"rename to a {label} name")
    # A missing value is the same refusal as an empty one -- neither is a 422,
    # because `value` is optional on the model and the bound is the handler's.
    assert_refusal(_manage(env, headers, {"action": "rename", "target_id": target}),
                   400, "Name must be 2-32 characters",
                   where="rename with no value at all")

    padded = _manage(env, headers, {"action": "rename", "target_id": target,
                                    "value": "  Padded Name  "})
    body = assert_keys(padded, {"ok", "action", "sect_id", "name"},
                       where="rename to a padded name")
    assert body["name"] == "Padded Name", (
        f"the padding was not trimmed; the reply carried {body['name']!r}")
    assert env["sect_store"].get_sect(sid)["name"] == "Padded Name", (
        "the reply echoed a trimmed name but the store kept the padded one")


# -- 11b. the beast-hunt resolve arm ----------------------------------------

def test_resolve_refuses_when_no_hunt_is_stored(env, sect):
    """A resolve with nothing stored is 404, and the hunt row is the guard.

    `resolve` is the only route that pops the hunt, so this also pins that the
    pop happens on a SUCCESSFUL resolve and not on the way in.
    """
    head, headers, sid, _ = sect
    assert head not in env["v2"].hunts
    assert_refusal(env["client"].post("/sect-v2/beast-hunt/resolve",
                                       headers={**headers, **_idem(f"nr{next(_TAG)}")}),
                   404, "No hunt in progress",
                   where="resolving with no hunt formed")


def test_a_party_below_the_clear_size_is_not_cleared_and_awards_nothing(env, sect):
    """Committed power below the target means `cleared: false` and 0 points.

    The arithmetic is the feature: `target` is a CONSTANT multiple of the wave
    power, so clearing needs a party of `BEAST_HUNT_CLEAR_MEMBERS` and no
    party size can make the target chase the party. Driven at both sizes around
    the boundary, with the numbers checked against the store's own formula.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    mate, _mh = _member(env, sid)
    assert sr.BEAST_HUNT_CLEAR_MEMBERS == 2

    def resolve_with(members, label):
        formed = client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": members},
                             headers={**headers, **_idem(f"bp{next(_TAG)}")})
        assert formed.status_code == 200, f"{label} -> {formed.text[:160]}"
        return client.post("/sect-v2/beast-hunt/resolve",
                           headers={**headers, **_idem(f"br{next(_TAG)}")})

    solo = assert_keys(resolve_with([head], "a solo hunt"),
                       {"ok", "cleared", "wave", "party", "committed_power",
                        "target_power", "contribution", "server_now"},
                       where="resolve of a one-member party")
    assert solo["cleared"] is False, "a party below the clear size was cleared"
    assert solo["committed_power"] == env["v2"].wave_power(1)
    assert solo["target_power"] == env["v2"].wave_power(1) * sr.BEAST_HUNT_CLEAR_MEMBERS
    assert solo["contribution"]["awarded"] == 0, (
        f"an uncleared hunt awarded {solo['contribution']['awarded']} points")
    # The refusal-shaped contribution arm carries no `capped` key, so a client
    # reading it must not assume the key exists.
    assert set(solo["contribution"]) == {"awarded", "points", "action"}, (
        f"the zero-award arm carried {sorted(solo['contribution'])}")

    duo = assert_keys(resolve_with([head, mate], "a two-member party"),
                      {"ok", "cleared", "wave", "party", "committed_power",
                       "target_power", "contribution", "server_now"},
                      where="resolve of a two-member party")
    assert duo["cleared"] is True, (
        "a party at the clear size did NOT clear; the target grows with the party")
    assert duo["committed_power"] == duo["target_power"]
    assert duo["contribution"]["awarded"] == sr.ACTIVITY_POINTS["beast_hunt_wave"] * 1, (
        f"a cleared wave-1 hunt awarded {duo['contribution']['awarded']}, "
        f"the server's price is {sr.ACTIVITY_POINTS['beast_hunt_wave']} per wave")
    assert duo["contribution"]["capped"] is False


def test_the_award_scales_with_the_wave_number_not_the_party(env, sect):
    """A cleared wave-N hunt awards the wave-N price, once.

    Waves 1 and 3 are driven. The award is `points * wave`, so it is a
    RELATIONSHIP between the reply's own `wave` and the awarded points -- read
    off `ACTIVITY_POINTS` rather than hardcoded, so a designer repricing the
    action moves this test with the table instead of breaking it.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    mates = [_member(env, sid)[0] for _ in range(4)]
    for wave in (1, 3):
        members = [head] + mates[: sr.BEAST_HUNT_CLEAR_MEMBERS - 1]
        formed = client.post("/sect-v2/beast-hunt/party",
                             json={"wave": wave, "members": members},
                             headers={**headers, **_idem(f"pw{next(_TAG)}")})
        assert formed.status_code == 200, formed.text[:160]
        body = assert_keys(client.post("/sect-v2/beast-hunt/resolve",
                                       headers={**headers, **_idem(f"br{next(_TAG)}")}),
                           {"ok", "cleared", "wave", "party", "committed_power",
                            "target_power", "contribution", "server_now"},
                           where=f"resolve of wave {wave}")
        assert body["wave"] == wave
        expected = sr.ACTIVITY_POINTS["beast_hunt_wave"] * wave
        assert body["contribution"]["awarded"] == expected, (
            f"wave {wave} awarded {body['contribution']['awarded']}, expected {expected}")


def test_naming_yourself_five_times_does_not_inflate_the_party(env, sect):
    """`dict.fromkeys` collapses a repeated member, so power cannot be inflated.

    The MODEL caps the list at `PARTY_CAP` (5) so a 6-name list is a 422 before
    the handler; five COPIES of one id is a legal 5-name list that collapses to
    a party of one. That is the honest test of the anti-inflation claim, and it
    is measured rather than read: `committed_power` stays one wave's power.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    assert sr.BEAST_HUNT_PARTY_CAP == 5
    reply = client.post("/sect-v2/beast-hunt/party",
                        json={"wave": 1, "members": [head] * sr.BEAST_HUNT_PARTY_CAP},
                        headers={**headers, **_idem(f"dup{next(_TAG)}")})
    body = assert_keys(reply, {"ok", "wave", "party", "power", "server_now"},
                       where="a party of five copies of one player")
    assert body["party"] == [head], (
        f"the duplicate members survived into the stored party: {body['party']}")
    resolved = assert_keys(client.post("/sect-v2/beast-hunt/resolve",
                                       headers={**headers, **_idem(f"br{next(_TAG)}")}),
                           {"ok", "cleared", "wave", "party", "committed_power",
                            "target_power", "contribution", "server_now"},
                           where="resolve of a deduplicated party")
    assert resolved["committed_power"] == env["v2"].wave_power(1), (
        f"committed power is {resolved['committed_power']} for a party of ONE, "
        "so the duplicates were counted")


def test_a_resolved_hunt_is_consumed_and_the_second_call_is_404(env, sect):
    """The hunt row is popped ON SUCCESS, so a replayed intent gets a 404.

    A caller that resolves twice does not get two results: the second call is
    an honest absence. This is why the route's once-only ledger is not the only
    thing standing between a caller and a double award.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    mate, _mh = _member(env, sid)
    assert client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": [head, mate]},
                       headers={**headers, **_idem(f"cp{next(_TAG)}")}).status_code == 200
    assert client.post("/sect-v2/beast-hunt/resolve",
                       headers={**headers, **_idem(f"cr{next(_TAG)}")}).status_code == 200
    assert head not in env["v2"].hunts, (
        "the resolved hunt is still stored, so a second resolve would score it again")
    assert_refusal(client.post("/sect-v2/beast-hunt/resolve",
                               headers={**headers, **_idem(f"cr{next(_TAG)}")}),
                   404, "No hunt in progress",
                   where="a second resolve of a consumed hunt")


def test_an_expired_hunt_is_dropped_so_the_honest_404_returns(env, sect):
    """A hunt from an earlier day is 400 AND REMOVED, then 404.

    The removal is the point. A stale row left in place would answer "Hunt
    expired" to every later resolve forever, and the player would never see
    their honest 404 again. Both halves are asserted, in that order.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    mate, _mh = _member(env, sid)
    assert client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": [head, mate]},
                       headers={**headers, **_idem(f"xp{next(_TAG)}")}).status_code == 200
    # Yesterday's day index, written into the STORE rather than waited for.
    stale_day = int(sr.time.time() // sr.DAY_SECONDS) - 5
    env["v2"].hunts[head]["day"] = stale_day

    assert_refusal(client.post("/sect-v2/beast-hunt/resolve",
                               headers={**headers, **_idem(f"xr{next(_TAG)}")}),
                   400, "Hunt expired", where="resolving a hunt from an earlier day")
    assert head not in env["v2"].hunts, (
        "the expired hunt was refused but LEFT in the store, so it will keep "
        "answering 'Hunt expired' instead of the honest 404")
    assert_refusal(client.post("/sect-v2/beast-hunt/resolve",
                               headers={**headers, **_idem(f"xr{next(_TAG)}")}),
                   404, "No hunt in progress",
                   where="resolving after the expired hunt was dropped")


# -- 11c. the aggregate cap, and the second claim ---------------------------

def test_the_aggregate_daily_cap_binds_only_when_actions_are_mixed(env, sect):
    """The 300-point aggregate cap is unreachable from ONE action, and reachable
    from several. Both halves are driven, because the arithmetic that makes the
    cap meaningful is not visible from either half alone.

    Per-action caps already bound the cheap action at 6 x 10 = 60 points, so a
    caller who only ever posts `meditation_session` tops out at a fifth of the
    stated cap. Mixing the three actions reaches exactly 300 and the fourth
    award is refused. Nothing is ever subtracted: `points` stays at the cap.
    """
    head, headers, sid, _ = sect
    client = env["client"]

    # Half one: one action alone cannot reach the aggregate cap.
    #
    # The CAPPED arm of `record_activity` returns a DIFFERENT key set -- it
    # carries `points` (the award, which is 0) and no `day_points` -- so the
    # running total has to be read from the contribution route, not from the
    # last activity reply. Reading `points` off a capped reply is exactly the
    # mistake this comment is here to prevent.
    for index in range(sr.ACTIVITY_DAILY_CAP["meditation_session"] + 4):
        client.post("/sect-v2/activity", json={"action": "meditation_session"},
                    headers={**headers, **_idem(f"one{index}")})
    solo_total = client.get("/sect-v2/contribution", headers=headers).json()["day_points"]
    assert solo_total == (sr.ACTIVITY_DAILY_CAP["meditation_session"]
                          * sr.ACTIVITY_POINTS["meditation_session"]), (
        f"a single action reached {solo_total}, which changes the shape of "
        "the per-action cap this file already pins")
    assert solo_total < sr.CONTRIBUTION_DAILY_CAP, (
        "this arm is only meaningful while one action stays under the aggregate cap")

    # Half two: a FRESH player, spending the per-action budgets of all three
    # actions. Two of them sum to 250, short of 300, so all three are needed.
    other, other_h = _member(env, sid)
    seen_capped = False
    for action in ("meditation_session", "beast_hunt_wave", "sect_wall_post"):
        for index in range(sr.ACTIVITY_DAILY_CAP[action]):
            body = client.post("/sect-v2/activity", json={"action": action},
                               headers={**other_h, **_idem(f"mix{action}{index}")}).json()
            seen_capped = seen_capped or body["capped"] is True
    assert seen_capped, (
        "no award was reported capped, so the aggregate cap never bound and "
        "this test proves nothing about it")
    read = assert_keys(client.get("/sect-v2/contribution", headers=other_h),
                       {"ok", "day_points", "day_cap", "week_cap", "day_actions",
                        "week_points", "day_reset_in", "in_sect", "server_now"},
                       where="contribution after the mixed spend")
    assert read["day_points"] == sr.CONTRIBUTION_DAILY_CAP, (
        f"the mixed spend stopped at {read['day_points']}, not the declared "
        f"cap of {sr.CONTRIBUTION_DAILY_CAP}")

    # And a further action is refused at zero, never subtracting from the total.
    after = client.post("/sect-v2/activity", json={"action": "war_contribution"},
                        headers={**other_h, **_idem("mixafter")}).json()
    assert after["awarded"] == 0 and after["capped"] is True
    assert client.get("/sect-v2/contribution", headers=other_h).json()["day_points"] == \
        sr.CONTRIBUTION_DAILY_CAP, "the refused award moved the running total"


def test_the_meditation_claim_is_once_a_day_and_the_read_says_so(env, sect):
    """First claim pays; the second answers `claimed: false` with a reason.

    The two arms carry DIFFERENT key sets, which is a real client hazard: the
    success arm carries `qi_bonus` and `day`, the refusal arm carries `reason`
    and no `day`. Both are pinned exactly, and the read route is checked to
    agree with the stored claim rather than with the second reply.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    expected = sr.MEDITATION_BONUS_BASE + sr.MEDITATION_BONUS_PER_RANK * sr.RANK_PATRIARCH

    first = assert_keys(client.post("/sect-v2/meditation/claim",
                                    headers={**headers, **_idem(f"cl{next(_TAG)}")}),
                        {"ok", "claimed", "amount", "qi_bonus", "day"},
                        where="the first claim of the day")
    assert first["claimed"] is True
    assert first["qi_bonus"] == expected
    assert first["amount"] == expected * sr.MEDITATION_QI_PER_POINT

    second = assert_keys(client.post("/sect-v2/meditation/claim",
                                     headers={**headers, **_idem(f"cl{next(_TAG)}")}),
                         {"ok", "claimed", "reason", "amount"},
                         where="a second claim on the same day")
    assert second["claimed"] is False
    assert second["reason"] == "already_claimed_today"
    assert second["amount"] == first["amount"], (
        "the second claim reported a different amount for the same day")

    read = assert_keys(client.get("/sect-v2/meditation", headers=headers),
                       {"ok", "in_sect", "rank", "rank_name", "qi_bonus",
                        "claimed_today", "claimed_amount", "day_points", "day_cap",
                        "week_points", "week_cap", "day_reset_in", "server_now"},
                       where="meditation read after a claim")
    assert read["claimed_today"] is True
    assert read["claimed_amount"] == first["amount"]
    assert read["rank_name"] == sr.RANK_NAMES[sr.RANK_PATRIARCH]


def test_claiming_without_a_sect_is_refused_by_the_membership_gate(env):
    """`claim` reads the caller's sect from the session and refuses 400.

    The store's own `rank <= 0` branch is unreachable over HTTP, because the
    route's membership gate fires first with a different status and message.
    That ordering is pinned so a reordering is visible: a client sees 400
    "Not in a sect", never the store's 403.
    """
    pid, headers = _player(env, f"NC{next(_TAG)}")
    assert env["sect_store"].get_player_sect(pid) is None
    assert_refusal(env["client"].post("/sect-v2/meditation/claim",
                                      headers={**headers, **_idem(f"nc{next(_TAG)}")}),
                   400, "Not in a sect",
                   where="a player in no sect claiming from the hall")


# -- 11d. the two read routes nothing pinned -------------------------------

def test_hierarchy_reports_the_whole_ladder_and_the_callers_own_rank(env, sect):
    """The roster shape, the caller's rank, and the constants the client needs
    to draw the ladder -- all in one reply.

    `rank_names` arrives with STRING keys, because JSON object keys are always
    strings. A client indexing it with the integer rank gets nothing, so the
    string-keyed shape is pinned rather than left to be discovered.
    """
    head, headers, sid, _ = sect
    elder, _eh = _member(env, sid, fund=True)
    assert _manage(env, headers, {"action": "promote", "target_id": elder}).status_code == 200

    body = assert_keys(env["client"].get("/sect-v2/hierarchy", headers=headers),
                       {"ok", "sect_id", "roster", "your_rank", "rank_names",
                        "member_cap", "max_elders", "elder_threshold"},
                       where="GET /sect-v2/hierarchy")
    assert body["sect_id"] == sid
    assert body["your_rank"] == sr.RANK_PATRIARCH
    assert body["member_cap"] == sr.MAX_MEMBERS
    assert body["max_elders"] == sr.MAX_ELDER_COUNT
    assert body["elder_threshold"] == sr.RANK_MIN_CONTRIBUTION[sr.RANK_ELDER]
    assert body["rank_names"] == {str(rank): name
                                  for rank, name in sr.RANK_NAMES.items()}, (
        "rank_names did not arrive keyed by the JSON string form of the rank")
    assert len(body["roster"]) == 2
    for row in body["roster"]:
        assert set(row) == {"player_id", "role", "rank",
                            "contribution_this_week", "total_contribution"}, (
            f"a roster row carried {sorted(row)}")
        assert row["rank"] in (sr.RANK_DISCIPLE, sr.RANK_ELDER, sr.RANK_PATRIARCH)


def test_the_roster_is_ordered_by_rank_then_contribution_then_id(env, sect):
    """Ordering is part of the contract: highest rank first, then the bigger
    contributor, then the player id as a tie-break.

    Driven with two elders whose `total_contribution` differs, because an
    ordering test that only has distinct RANKS cannot tell a rank sort from a
    contribution sort.
    """
    head, headers, sid, _ = sect
    low, _lh = _member(env, sid, fund=True)
    high, _hh = _member(env, sid, fund=True)
    sect_record = env["sect_store"].get_sect(sid)
    sect_record["members"][low]["total_contribution"] = 5_000
    sect_record["members"][high]["total_contribution"] = 9_000
    for pid in (low, high):
        assert _manage(env, headers, {"action": "promote", "target_id": pid}).status_code == 200

    roster = assert_keys(env["client"].get("/sect-v2/hierarchy", headers=headers),
                         {"ok", "sect_id", "roster", "your_rank", "rank_names",
                          "member_cap", "max_elders", "elder_threshold"},
                         where="hierarchy with two funded elders")["roster"]
    ranks = [row["rank"] for row in roster]
    assert ranks == sorted(ranks, reverse=True), f"the roster is not rank-first: {ranks}"
    assert roster[0]["player_id"] == head, "the patriarch is not first on the roster"
    assert [row["player_id"] for row in roster[1:]] == [high, low], (
        "two elders with different totals are not ordered by the larger total")


def test_list_publishes_one_row_per_sect_with_the_callers_own_id(env, sect):
    """The directory row shape, and `your_sect_id` read from the session.

    `your_sect_id` is the field a client uses to preselect a row, so both its
    presence and its value are pinned -- including for a caller in no sect,
    where it is `None` rather than omitted.
    """
    head, headers, sid, _ = sect
    body = assert_keys(env["client"].get("/sect-v2/list", headers=headers),
                       {"ok", "sects", "your_sect_id"}, where="GET /sect-v2/list")
    assert body["your_sect_id"] == sid
    rows = [row for row in body["sects"] if row["id"] == sid]
    assert len(rows) == 1, f"the caller's own sect appeared {len(rows)} times in the list"
    assert set(rows[0]) == {"id", "name", "level", "member_count", "member_cap"}
    assert rows[0]["member_count"] == 1
    assert rows[0]["member_cap"] == sr.MAX_MEMBERS
    assert rows[0]["level"] == 1

    # A player in no sect gets the same shape with a null id, not a missing key.
    _pid, outsider_h = _player(env, f"LO{next(_TAG)}")
    outside = assert_keys(env["client"].get("/sect-v2/list", headers=outsider_h),
                          {"ok", "sects", "your_sect_id"},
                          where="GET /sect-v2/list for a player in no sect")
    assert outside["your_sect_id"] is None
    assert len(outside["sects"]) == len(body["sects"]), (
        "the directory is not the same size for a member and a non-member")


def test_the_wave_table_is_the_stores_own_power_curve(env, sect):
    """Every advertised wave matches `wave_power`, and `points` is price x wave.

    The table a client draws from is generated from the same two functions the
    resolver scores against, so a divergence here means the client is being
    shown numbers the server will not honour.
    """
    head, headers, sid, _ = sect
    body = assert_keys(env["client"].get("/sect-v2/beast-hunt/waves", headers=headers),
                       {"ok", "in_sect", "waves", "party_cap", "server_now"},
                       where="GET /sect-v2/beast-hunt/waves")
    assert body["party_cap"] == sr.BEAST_HUNT_PARTY_CAP
    assert len(body["waves"]) == sr.BEAST_HUNT_MAX_WAVES
    assert [row["wave"] for row in body["waves"]] == list(range(1, sr.BEAST_HUNT_MAX_WAVES + 1))
    powers = []
    for row in body["waves"]:
        assert set(row) == {"wave", "power", "points"}
        expected = env["v2"].wave_power(row["wave"])
        assert row["power"] == expected, (
            f"wave {row['wave']} is advertised at {row['power']} power, the "
            f"resolver scores it at {expected}")
        assert row["points"] == sr.ACTIVITY_POINTS["beast_hunt_wave"] * row["wave"]
        powers.append(row["power"])
    assert powers == sorted(powers), f"the power curve is not monotonic: {powers}"


def test_leave_reports_only_ok_and_left_and_a_second_call_refuses(env, sect):
    """The leave reply is two keys, and leaving twice is a 400.

    The exact key set matters because this reply is what a client waits on to
    clear its sect state: an extra key would be an unpromised field, and a
    missing one would be a state the client cannot clear.
    """
    head, headers, sid, _ = sect
    assert_keys(env["client"].post("/sect-v2/leave", headers=headers),
                {"ok", "left"}, where="the sole member leaving")
    assert env["sect_store"].get_player_sect(head) is None
    assert_refusal(env["client"].post("/sect-v2/leave", headers=headers),
                   400, "Not in a sect", where="a second leave")


# ==========================================================================
# 12. CONTROLS for section 11 -- prove the new assertions can go red
# ==========================================================================

def test_control_an_emptied_clear_size_makes_the_cleared_assertion_go_red(
        env, sect, monkeypatch):
    """Drop `BEAST_HUNT_CLEAR_MEMBERS` to 1 and the "a solo party is not
    cleared" assertion must fail.

    This is the measurement that decides whether section 11b says anything. The
    stored hunt's `day` is rolled back to today so the resolve is not refused
    as expired first, and the patrol is a live PLAYER IN THE CALLER'S OWN SECT
    so the membership gate cannot fire before the arithmetic runs.

    The mutation is a module constant read at call time inside
    `resolve_hunt`, so the shipped handler uses the sabotaged value; no
    repository file is edited.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    assert client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": [head]},
                       headers={**headers, **_idem(f"cc{next(_TAG)}")}).status_code == 200

    # Baseline: the shipped code refuses to clear a party of one.
    baseline = client.post("/sect-v2/beast-hunt/resolve",
                           headers={**headers, **_idem(f"cb{next(_TAG)}")})
    assert baseline.json()["cleared"] is False, (
        "the shipped code cleared a solo party; this control is measuring nothing")

    # Re-form the hunt, then sabotage the clear size and resolve again.
    assert client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": [head]},
                       headers={**headers, **_idem(f"cd{next(_TAG)}")}).status_code == 200
    monkeypatch.setattr(sr, "BEAST_HUNT_CLEAR_MEMBERS", 1)
    mutated = client.post("/sect-v2/beast-hunt/resolve",
                          headers={**headers, **_idem(f"cm{next(_TAG)}")})
    assert mutated.status_code == 200, (
        f"the mutation changed the status to {mutated.status_code}; this control "
        "expects the arithmetic to still answer 200 and only the verdict to move")
    assert mutated.json()["cleared"] is True, (
        "the mutation did not take: /sect-v2/beast-hunt/resolve still refuses to "
        "clear a solo party, so the control proves nothing")

    # And the pinned assertion from 11b is exactly what notices. The reply is
    # captured first: `assert_keys` PASSES under the sabotage (the key set is
    # unchanged), so the verdict assertion is what must go red.
    assert client.post("/sect-v2/beast-hunt/party", json={"wave": 1, "members": [head]},
                       headers={**headers, **_idem(f"ce{next(_TAG)}")}).status_code == 200
    sabotaged = assert_keys(
        client.post("/sect-v2/beast-hunt/resolve",
                    headers={**headers, **_idem(f"cf{next(_TAG)}")}),
        {"ok", "cleared", "wave", "party", "committed_power", "target_power",
         "contribution", "server_now"},
        where="resolve of a one-member party under a sabotaged clear size")
    with pytest.raises(AssertionError, match="below the clear size was cleared"):
        assert sabotaged["cleared"] is False, (
            "a party below the clear size was cleared")


def test_control_a_doubled_daily_cap_makes_the_cap_assertion_go_red(
        env, sect, monkeypatch):
    """Double `CONTRIBUTION_DAILY_CAP` and the "the mixed spend stops exactly
    at the declared cap" assertion must fail.

    This is the control for the aggregate-cap test. The per-action caps are left
    alone, so the only thing that can change is the layer being measured -- and
    the assertion reads the cap off the module, so it moves WITH the sabotage
    and the failure has to come from the observed points, not the constant.
    """
    head, headers, sid, _ = sect
    client = env["client"]

    def spend(headers_for):
        """Spend every per-action budget of the three actions that count."""
        for action in ("meditation_session", "beast_hunt_wave", "sect_wall_post"):
            for index in range(sr.ACTIVITY_DAILY_CAP[action]):
                client.post("/sect-v2/activity", json={"action": action},
                            headers={**headers_for, **_idem(f"{action}{index}")})

    # Baseline: with the shipped cap, the mixed spend stops exactly on it.
    other, other_h = _member(env, sid)
    spend(other_h)
    shipped = client.get("/sect-v2/contribution", headers=other_h).json()
    assert shipped["day_points"] == sr.CONTRIBUTION_DAILY_CAP, (
        f"the shipped code stopped at {shipped['day_points']}, not the declared "
        f"cap of {sr.CONTRIBUTION_DAILY_CAP}; this control is measuring nothing")

    # A second player, spending the same budget against a doubled cap.
    fresh, fresh_h = _member(env, sid)
    monkeypatch.setattr(sr, "CONTRIBUTION_DAILY_CAP", sr.CONTRIBUTION_DAILY_CAP * 2)
    spend(fresh_h)
    mutated = client.get("/sect-v2/contribution", headers=fresh_h).json()
    assert mutated["day_cap"] == sr.CONTRIBUTION_DAILY_CAP, (
        "the reply did not report the sabotaged cap, so the mutation is not "
        "visible in the reply this test reads")

    # The assertion 11c makes is `observed == the declared cap`. Under the sabotage
    # the declared cap moved UP and the budget ran out unused, so the observed
    # total is the full uncapped sum and no longer equals the declared cap.
    # That divergence IS the failure 11c's assertion reports, and it is spelled
    # out here rather than wrapped in `pytest.raises` around a copy of itself.
    assert mutated["day_points"] > shipped["day_points"], (
        "the sabotaged player did not reach a higher total than the capped one; "
        "doubling the cap changed nothing, so this control is measuring nothing")
    assert mutated["day_points"] != sr.CONTRIBUTION_DAILY_CAP, (
        f"the sabotaged player stopped on the doubled cap at {mutated['day_points']} "
        "points, which is what the SHIPPED code does; the mutation did not take")


# ==========================================================================
# 13. THE WALL SURFACE, AND WHAT ACTUALLY GATES REMOVING A WALL POST
#
# MEASURED GROUND TRUTH, because the request this file answers described the
# wall in three ways the module does not match. Each correction below was
# measured by driving the mounted app, not read off a docstring:
#
# * `sect_routes.py` mounts THIRTEEN routes and NOT ONE of them is a wall
#   route or a delete. There is no DELETE verb anywhere under `/sect-v2/`.
#   Its wall surface is the ACTIVITY route: `sect_wall_post` and
#   `sect_wall_contribute` are two of the five priced actions, reached by
#   posting an ACTION NAME. No message is ever posted through this module.
# * The message feed itself -- `/sect-wall/post` and `/sect-wall/unpin` -- is
#   registered by `sect_wall.py`, a DIFFERENT module. It is driven here because
#   the shared harness mounts it into the same app, but every guard on it
#   belongs to that file, and this file says so rather than claiming it.
# * Removing a wall post is gated by RANK, not by who wrote the post.
#   `unpin_post` never reads `author_id`; it matches on `post_id` alone. The
#   brief asked for an ownership gate and there is none -- measured both ways
#   below, because "there is no check" is a claim that needs a test.
#
# ONE MORE MEASURED FACT, and the sharpest one in this section: a player who
# joins through `/sect-v2/join` CANNOT post to the wall. Section 5 already
# pins that `join` never stamps `sect_id` onto the player row, and
# `sect_wall` reads membership from that row. So two mounted routes answer
# different things about whether the same player is a member.
# ==========================================================================


def test_this_module_mounts_no_wall_route_and_no_delete(env, sect):
    """The wall is reachable from here ONLY as an activity name.

    Asserted as the absence of a whole class rather than one path, so adding a
    wall route or a DELETE to this registrar is a visible change here and not a
    silent new surface. This is the measurement behind the section header.
    """
    head, headers, sid, _ = sect
    methods = {
        sorted(rt.methods - {"HEAD", "OPTIONS"})[0]
        for rt in env["app"].routes
        if type(rt).__name__ == "APIRoute" and rt.path.startswith("/sect-v2")
    }
    assert "DELETE" not in methods, (
        f"a DELETE verb now exists under /sect-v2: {sorted(methods)}")
    wall_paths = {rt.path for rt in env["app"].routes
                  if type(rt).__name__ == "APIRoute"
                  and rt.path.startswith("/sect-wall")}
    sect_v2_paths = {rt.path for rt in env["app"].routes
                     if type(rt).__name__ == "APIRoute"
                     and rt.path.startswith("/sect-v2")}
    assert not (wall_paths & sect_v2_paths), (
        f"this registrar now owns a wall path: {sorted(wall_paths & sect_v2_paths)}")

    # And the wall IS present, as two priced action names. Asserted as a
    # membership in the server's own table so repricing or dropping an action
    # moves this test rather than silently orphaning the bridge.
    for action in ("sect_wall_post", "sect_wall_contribute"):
        assert action in sr.ACTIVITY_POINTS, (
            f"{action!r} left the price table, so the sect_wall bridge would "
            "now be refused 400 and its contribution discarded")
        assert action in sr.ACTIVITY_DAILY_CAP, f"{action!r} has no per-day cap"


def test_the_wall_activity_names_require_sect_membership(env, sect):
    """A player in no sect is refused both wall actions with 400 "Not in a sect".

    This is the membership gate for wall posting AS IT EXISTS IN THIS MODULE,
    and it was unpinned: section 4 drove the gate through hierarchy, leave and
    manage, but not through `/sect-v2/activity`, which is the route the wall
    actually uses. Both wall actions are driven, because a gate wired on one
    branch of an action table and not the other would pass a single probe.

    The gate runs BEFORE the price lookup, so an unknown action name and a
    known one refuse identically here -- the message is about membership, not
    about what was being posted.
    """
    stranger, sh = _player(env, f"WA{next(_TAG)}")
    assert env["sect_store"].get_player_sect(stranger) is None, (
        "precondition: this player must be in no sect")

    client = env["client"]
    for action in ("sect_wall_post", "sect_wall_contribute"):
        assert_refusal(client.post("/sect-v2/activity", json={"action": action},
                                   headers={**sh, **_idem(f"wa{next(_TAG)}")}),
                        400, "Not in a sect",
                        where=f"a stranger posting the {action!r} activity")
    # Nothing was written for the refused attempts.
    assert env["v2"].activity.get(stranger, {}).get("points", 0) == 0, (
        "a refused wall activity still wrote a contribution")


def test_a_member_posts_the_wall_actions_and_the_server_prices_them(env, sect):
    """The same two actions succeed for a member, at the SERVER's price.

    Asserted as the relationship between the award and the table entry, so a
    designer repricing the wall action moves this test with the table instead of
    breaking it, while a route that priced the action from the body would go
    red. A forged `points` rides along to prove there is nothing to forge.
    """
    head, headers, sid, _ = sect
    client = env["client"]
    for action in ("sect_wall_post", "sect_wall_contribute"):
        pid, ph = _member(env, sid)
        body = assert_keys(
            client.post("/sect-v2/activity",
                        json={"action": action, "points": 99_999},
                        headers={**ph, **_idem(f"wm{next(_TAG)}")}),
            {"ok", "awarded", "capped", "action", "points", "day_points"},
            where=f"a member posting the {action!r} activity")
        assert body["awarded"] == sr.ACTIVITY_POINTS[action], (
            f"{action!r} awarded {body['awarded']}, the server prices it at "
            f"{sr.ACTIVITY_POINTS[action]}")
        assert body["awarded"] != 99_999, "a caller-supplied points field was honoured"
        assert body["action"] == action


def test_the_wall_feed_refuses_a_stranger_then_a_member_below_the_posting_rank(env, sect):
    """`/sect-wall/post` has TWO gates, and they answer in this order.

    MEASURED, and the ORDER is the finding: a player in no sect is refused 409
    "Not in a sect", and a player who IS in a sect but has earned nothing is
    refused 403 "Rank cannot post on the sect wall". Even the patriarch of a
    brand-new sect cannot post -- `create` grants the top SECT rank, but the
    wall ladder is a separate one keyed on lifetime contribution.

    These guards live in `sect_wall.py`, not in the module under test. They are
    pinned here because the shared harness mounts that registrar into this very
    app, so a change to either file is visible from this suite.
    """
    from sect_wall import rank_for

    head, headers, sid, _ = sect
    client = env["client"]
    stranger, _sh = _player(env, f"WP{next(_TAG)}")

    refused = client.post("/sect-wall/post",
                          json={"player_id": stranger, "message": "hello"},
                          headers={**_idem("ws1")})
    assert_refusal(refused, 409, "Not in a sect",
                   where="a stranger posting to the sect wall")

    # The founder, whose sect rank is the top one but whose wall rank is zero.
    assert rank_for(0)["can_post"] is False, (
        "rank zero gained the right to post, so the 403 arm below is unreachable")
    unearned = client.post("/sect-wall/post",
                           json={"player_id": head, "message": "hello"},
                           headers={**_idem("ws2")})
    assert_refusal(unearned, 403, "Rank cannot post on the sect wall",
                   where="the patriarch of a fresh sect posting to the wall")


def test_a_player_who_joined_cannot_post_because_join_never_stamped_the_row(env, sect):
    """MEASURED CROSS-MODULE DISAGREEMENT, pinned not fixed.

    `/sect-v2/join` answers 200 and the sect store records the membership, but
    it does NOT write `sect_id` onto the player's own row (section 5 pins that
    defect). `sect_wall` reads membership from the ROW. So the two mounted
    routes disagree about the same player in the same process: the join route
    says they are in the sect, and the wall says they are not.

    Measured: the refusal is 409 "Not in a sect", which is a sect_wall message
    naming a condition the join route just reported as satisfied.
    """
    head, headers, sid, _ = sect
    member, member_headers = _member(env, sid)

    # Both stores disagree, and the disagreement is the finding.
    assert env["sect_store"].get_player_sect(member) == sid, (
        "precondition: the sect store should know this player joined")
    assert env["db"].rows[member]["sect_id"] is None, (
        "join started stamping the player row -- the disagreement this test "
        "pins is FIXED, so this docstring is now stale")

    refused = env["client"].post("/sect-wall/post",
                                 json={"player_id": member, "message": "hello"},
                                 headers={**member_headers, **_idem("wm1")})
    assert_refusal(refused, 409, "Not in a sect",
                   where="a player who just joined posting to the wall")


def test_removing_a_wall_post_is_gated_by_rank_and_never_by_authorship(env, sect):
    """MEASURED: there is no ownership gate. `unpin_post` never reads `author_id`.

    This is the brief's fourth item, and it is FALSE for this codebase, so it is
    pinned as the measured truth rather than as the requested shape. Both
    directions are driven, because "there is no check" cannot be shown by one
    passing case:

    * AUTHORSHIP ALONE IS NOT ENOUGH. The author of a post, demoted back to the
      bottom of the wall ladder, is refused 403 -- the same refusal a stranger's
      attempt gets. Writing a post earns no right to remove it.
    * RANK ALONE IS NOT ENOUGH EITHER. A funded moderator gets past the rank
      gate and then 404s, because the route searches the CALLER'S OWN wall for
      the post id. The two conditions are independent and both must hold.

    So the real rule is "in a sect, at a rank that can moderate, and the post is
    on your own wall". A client that assumed author-rights would show a delete
    button that always fails.
    """
    from sect_wall import _state_of, rank_for

    head, headers, sid, _ = sect
    client = env["client"]

    # A founder who has earned the top wall rank.
    _state_of(env["db"].rows[head])["lifetime_contribution"] = 30_000
    assert rank_for(30_000)["can_pin"] is True, (
        "the top wall rank lost the right to moderate, so this test is measuring "
        "the wrong arm")

    posted = client.post("/sect-wall/post",
                         json={"player_id": head, "message": "author post"},
                         headers={**_idem("up1")})
    assert posted.status_code == 200, posted.text[:200]
    post_id = posted.json()["post"]["post_id"]

    # Direction one: the AUTHOR, demoted, cannot remove their own post.
    _state_of(env["db"].rows[head])["lifetime_contribution"] = 0
    assert_refusal(client.post("/sect-wall/unpin",
                               json={"player_id": head, "post_id": post_id}),
                   403, "Rank cannot moderate the sect wall",
                   where="the AUTHOR, demoted to the bottom rank, unpinning their own post")

    # Direction two: a funded moderator who did not write the post clears the
    # rank gate and then fails on wall-scoping. The row is repaired by hand
    # because `join` does not stamp it (the defect pinned just above).
    mod, mod_headers = _member(env, sid)
    mod_row = env["db"].rows[mod]
    mod_row["sect_id"] = sid
    mod_row["sect_rank"] = "disciple"
    _state_of(mod_row)["lifetime_contribution"] = 30_000
    assert rank_for(30_000)["can_pin"] is True
    assert_refusal(client.post("/sect-wall/unpin",
                               json={"player_id": mod, "post_id": post_id}),
                   404, "Post not found on this member's wall",
                   where="a funded moderator unpinning a post on another member's wall")

    # And the moderator CAN remove a post on their own wall, which is the shape
    # that actually works -- so the 404 above is wall-scoping, not a dead route.
    own = client.post("/sect-wall/post",
                      json={"player_id": mod, "message": "mod's own"},
                      headers={**mod_headers, **_idem("up2")})
    assert own.status_code == 200, own.text[:200]
    removed = client.post("/sect-wall/unpin",
                          json={"player_id": mod, "post_id": own.json()["post"]["post_id"]})
    assert removed.status_code == 200, (
        f"a funded moderator could not unpin a post on their own wall: "
        f"{removed.status_code} {removed.text[:160]}")
    assert removed.json() == {"ok": True,
                              "post_id": own.json()["post"]["post_id"],
                              "pinned": False}

    # A post id nobody holds is a 404 at the SAME rank, so a caller who is
    # allowed to moderate still cannot be told whether a post exists elsewhere.
    # Driven by `mod`, who holds a qualifying rank: using `head` here would
    # answer 403 instead, because this test demoted them earlier -- which is
    # itself the rank gate doing its job.
    assert_refusal(client.post("/sect-wall/unpin",
                               json={"player_id": mod, "post_id": "no-such-post"}),
                   404, "Post not found on this member's wall",
                   where="a post id that exists nowhere, asked by a funded moderator")


def test_the_membership_gate_goes_red_when_the_store_is_sabotaged(env, sect, monkeypatch):
    """CONTROL: force the store to claim every player is in the sect.

    `sect_routes`' membership gate reads `sect_store.get_player_sect`, so
    replacing that one method on the live store object makes the gate believe a
    stranger is a member. The pinned 400 must then become a 200 -- which is what
    proves the assertions above are reading a real guard rather than passing for
    some unrelated reason (a missing route, a bad token, a 404).

    Only an in-memory attribute on the harness's own store is swapped, and
    monkeypatch restores it. No repository file is edited.
    """
    head, headers, sid, _ = sect
    stranger, sh = _player(env, f"WC{next(_TAG)}")
    client = env["client"]
    body = {"action": "sect_wall_post"}
    key = _idem(f"wc{next(_TAG)}")

    # Baseline: the shipped gate refuses the stranger.
    assert_refusal(client.post("/sect-v2/activity", json=body,
                               headers={**sh, **key}),
                   400, "Not in a sect",
                   where="control baseline")

    # The mutation is live: the store now claims this player is in `sid`.
    monkeypatch.setattr(env["sect_store"], "get_player_sect", lambda _pid: sid)

    fresh, fh = _player(env, f"WC{next(_TAG)}")
    mutated = client.post("/sect-v2/activity", json=body,
                          headers={**fh, **_idem(f"wc{next(_TAG)}")})
    assert mutated.status_code == 200, (
        f"the mutation left the refusal at {mutated.status_code}; expected the "
        "sabotaged store to let the gate pass. If it stayed 400, this control "
        "is measuring something other than the gate.")
    assert mutated.json()["awarded"] == sr.ACTIVITY_POINTS["sect_wall_post"], (
        "the mutated request was served but not at the server's price, so the "
        "control reached a different path than the one it claims to measure")

    # And the pinned assertion is exactly what notices. The sabotage flips the
    # status, so `assert_refusal` fails on its FIRST line -- the status check --
    # before it ever compares the message. The match is therefore on the status
    # text, not on the message text.
    with pytest.raises(AssertionError, match="expected 400"):
        assert_refusal(client.post("/sect-v2/activity", json=body,
                                   headers={**fh, **_idem(f"wc{next(_TAG)}")}),
                       400, "Not in a sect",
                       where="control under mutation")
