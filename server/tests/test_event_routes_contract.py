"""
EVENT ROUTES: THE HTTP CONTRACT, PINNED BY MEASUREMENT.

WHAT THIS FILE IS FOR
--------------------
``server/event_routes.py`` registers four paths. This file answers a different
question from the three existing event test files (which cover the authz defect,
the cancel-id defect, and the rate-authority defect): what does a client that
has never read the Python actually SEND and RECEIVE on each of the four, and
what does each one refuse?

Every assertion below was MEASURED by calling the routes through a real
``TestClient``, not read off the source or off the ``return`` dictionaries.
Where a measured answer disagrees with what the module's own comments imply,
the measured answer is what is written here and the disagreement is called out
in a comment at the test that depends on it.

THE MOUNT
---------
The four routes are mounted by hand with a bare ``FastAPI()``. ``main.py`` is
3000+ lines and several agents edit it at once; importing it measures whatever
a peer is mid-write. Every fixture also asserts the route really registered, so
no test in this file can pass against a 404.

Run (memory mode -- unset the PG vars so the event scheduler's state writer
cannot reach for a database and the wallet store stays in memory):

    env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
        -u PYTHONPATH <python> -m pytest server/tests/test_event_routes_contract.py -q
"""
from __future__ import annotations

import os

# Popped BEFORE any import so an ambient Postgres setting cannot drag this file
# into a live-database branch. The event scheduler's state writer would then try
# to open a connection on every cancel.
for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

import json
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth                                                   # noqa: E402
import event_routes                                           # noqa: E402
from event_rewards import EVENT_ITEM_SOULDUST_RATE            # noqa: E402
from event_scheduler import EventConfig, EventScheduler       # noqa: E402
from wallet_defense import WalletStore                        # noqa: E402

LIST = "/events/list"
CONVERT = "/events/convert"
SCHEDULE = "/admin/events/schedule"
CANCEL = "/admin/events/{event_id}/cancel"

#: Set per-test by the admin fixture; ``require_admin`` reads it from the
#: environment at call time, never from a session.
ADMIN_TOKEN = "contract-admin-token"
ADMIN_HEADER = {"X-Admin-Token": ADMIN_TOKEN}

ITEM = "evt-contract-1"
EVENT = "e-contract"
OTHER = "stranger"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _session(player_id: str) -> dict:
    """A real bearer session, minted the way server code mints one.

    There is no login route in this game and there must not be one.
    """
    return {"Authorization": f"Bearer {auth.issue_session(player_id)}"}


def _convert_body(player_id: str, **over) -> dict:
    """A COMPLETE convert body.

    All four of ``player_id``/``event_id``/``item_id``/``quantity`` are
    required; omitting any one of them answers 422, which reads like a refusal
    but is only validation.
    """
    body = {"player_id": player_id, "event_id": EVENT,
            "item_id": ITEM, "quantity": 10}
    body.update(over)
    return body


def _player(players_db: dict, player_id: str, items: int = 50) -> dict:
    row = {"id": player_id, "name": player_id.title(), "inventory": {},
           "spirit_stones": {}}
    if items:
        row["inventory"][ITEM] = items
    players_db[player_id] = row
    return row


def _items(players_db: dict, player_id: str) -> int:
    return players_db[player_id].get("inventory", {}).get(ITEM, 0)


def _paths(app) -> set:
    return {r.path for r in app.routes
            if getattr(r, "path", "") and r.path not in ("/openapi.json",
                                                         "/docs",
                                                         "/docs/oauth2-redirect",
                                                         "/redoc")}


def _methods(app, path) -> set:
    """Every method a path answers. FastAPI adds HEAD to each GET, so collect
    the whole set rather than ``next(iter(...))``."""
    out = set()
    for r in app.routes:
        if getattr(r, "path", "") == path:
            out |= set(getattr(r, "methods", set()) or set())
    return out


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture()
def wire():
    """A mounted app, one player holding items, and an admin token configured.

    The admin token is written into the environment because ``require_admin``
    reads ``WC_ADMIN_TOKEN`` at call time. It is torn down so a later test in
    another file cannot inherit it.
    """
    previous = os.environ.get("WC_ADMIN_TOKEN")
    os.environ["WC_ADMIN_TOKEN"] = ADMIN_TOKEN
    players_db = {}
    _player(players_db, "owner")
    _player(players_db, OTHER)

    wallet = WalletStore()
    wallet.create("owner")
    wallet.create(OTHER)

    scheduler = EventScheduler()
    app = FastAPI()
    event_routes.register_event_routes(app, players_db, wallet, scheduler)

    registered = _paths(app)
    for path in (LIST, CONVERT, SCHEDULE, CANCEL):
        assert path in registered, (
            f"{path} is not mounted, so this whole file would be vacuous "
            f"against a 404. Registered: {sorted(registered)}")

    yield {"app": app, "client": TestClient(app, raise_server_exceptions=False),
           "players_db": players_db, "wallet": wallet, "scheduler": scheduler}

    if previous is None:
        os.environ.pop("WC_ADMIN_TOKEN", None)
    else:
        os.environ["WC_ADMIN_TOKEN"] = previous


@pytest.fixture()
def guarded_wire(wire):
    """``wire`` plus the two things ``main.lifespan`` adds AFTER the registrar.

    In this exact order, because the order is the whole point:

    1. ``attach_player_directory`` -- declares which store answers "does this
       player still exist?". Without it a session is accepted even when its
       player row is gone, so a deleted account keeps reading.
    2. ``install_ownership_guard(app)`` -- attaches ``require_player`` to the
       routes that name a player id and declare no guard of their own.
       ``POST /events/list`` is one of those: the id arrives in the REQUEST
       BODY, and the shape matcher only looks at the path and query string, so
       the source signature carries no ``Depends`` and the installer is the
       ONLY thing closing it.

    ``wire`` on its own measures the registrar in isolation, which is useful
    for the shape and payout tests but proves nothing about authorisation.
    """
    from ownership_guard import install_ownership_guard

    auth.attach_player_directory(wire["app"], wire["players_db"])
    install_ownership_guard(wire["app"])
    return wire


# ==========================================================================
# 1b. THE FOUR COVERAGE AREAS, AS A SUMMARY
# ==========================================================================
#
# The task this file answers has four parts. Each is one section, and each is
# asserted from BOTH sides wherever a gate is involved -- a test that only
# checks a refusal passes just as happily against a route that is dead,
# misrouted or permanently broken, so it cannot tell a working gate from a
# missing one.
#
#   (1) event listing requires auth ....... section 2b
#   (2) no duplicate endpoint registration  section 1
#   (3) player disclosure prevented ........ section 2c
#   (4) non-finite values rejected ........ section 6


# ==========================================================================
# 1. THE REGISTRATION SURFACE ITSELF
# ==========================================================================

def test_this_module_registers_exactly_four_paths(wire):
    """The contract is the whole surface, so it is pinned as a set.

    Measured: exactly four paths, all POST.
    """
    assert _paths(wire["app"]) == {LIST, CONVERT, SCHEDULE, CANCEL}
    for path in (LIST, CONVERT, SCHEDULE, CANCEL):
        assert _methods(wire["app"], path) == {"POST"}, (
            f"{path} answers {_methods(wire['app'], path)}, expected POST only")


@pytest.mark.parametrize("path", [LIST, CONVERT, SCHEDULE])
def test_post_only_routes_answer_405_to_get(wire, path):
    """MEASURED: 405, not 404. The path exists; the verb does not."""
    assert wire["client"].get(path).status_code == 405


def test_the_three_paths_this_module_deliberately_does_not_register(wire):
    """Three paths look like they belong here and are owned by ``main.py``.

    MEASURED: all three are 404 on an app where only this module is mounted.
    That is the pinned behaviour. If a future change re-adds one of them here,
    it becomes a SECOND copy of a live path: FastAPI serves the first
    registration, so the new copy is dead code that still reads as live, and
    every fix applied to it is a no-op at runtime.
    """
    for path in ("/events", "/events/claim", "/admin/events/create"):
        assert path not in _paths(wire["app"]), (
            f"{path} is registered by event_routes; it is owned by main.py")
        r = wire["client"].post(path, json={}) if path != "/events" \
            else wire["client"].get(path)
        assert r.status_code == 404, (
            f"{path} answered {r.status_code}, expected 404")


def test_registering_twice_adds_no_routes_and_refreshes_the_scheduler(wire):
    """MEASURED: the route count is stable across a second startup.

    ``main.lifespan`` calls the register function on every startup and a
    process can start the app more than once. Without the app-marked guard,
    FastAPI keeps both copies and the route table grows each time.
    """
    before = len(wire["app"].routes)
    replacement = EventScheduler()
    event_routes.register_event_routes(wire["app"], wire["players_db"],
                                       wire["wallet"], replacement)

    assert len(wire["app"].routes) == before, (
        "a second registration added routes; the idempotency guard is gone")
    holder = wire["app"]._wayfarer_event_scheduler_holder
    assert holder["scheduler"] is replacement, (
        "a second registration did not refresh the scheduler holder, so the "
        "closures would keep serving the already-stopped first scheduler")


# ==========================================================================
# 2. POST /events/list  -- the post-shaped listing, and WHO may read it
# ==========================================================================
#
# THE HEADING THIS SECTION USED TO CARRY WAS WRONG
# ------------------------------------------------
# This section was previously titled "the public, signed-out, post-shaped
# read", and it asserted that the listing needs no session at all. That is
# true of a BARE registrar and FALSE of the running server, which is the trap
# a bare app sets: ``main.lifespan`` mounts ``register_event_routes()`` first
# and calls ``install_ownership_guard(app)`` at the very END, after every route
# exists, and that installer is the only thing closing this route.
#
# MEASURED 2026-10-05 on the running server, ``server.main`` with the lifespan
# entered: an anonymous ``POST /events/list`` answers **401**
# ``invalid or missing session token``. Same app, same body, the only
# difference being that the startup installer had run. The module's own
# docstring says the same thing at ``event_routes.py:250`` ("It is therefore NOT
# a public-by-design endpoint") and records that an earlier revision claiming
# the opposite was deleted as stale.
#
# So the two fixtures below are both load-bearing and they measure different
# things. ``wire`` is the registrar alone. ``guarded_wire`` adds the two things
# ``main.lifespan`` adds AFTER it: the player directory and the guard
# installer. A refusal asserted against ``wire`` would prove nothing; a
# refusal asserted against ``guarded_wire`` is the production contract.

def test_list_reply_shape(wire):
    """MEASURED: the envelope has one key; each row has nine."""
    scheduler = wire["scheduler"]
    scheduler.create_event(
        EventConfig(key="list-key", name="List Probe", event_type="probe",
                    rewards=[{"type": "spirit_stone", "amount": 5}]), None)

    r = wire["client"].post(LIST, json={"player_id": "owner"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"events"}, body
    assert len(body["events"]) == 1, body
    row = body["events"][0]
    # Frozen: a client reads these names. Adding one is safe; renaming one
    # breaks every caller.
    assert set(row) == {"event_id", "instance_id", "name", "type", "state",
                        "days_remaining", "start_time", "end_time",
                        "rewards"}, row
    assert row["event_id"] == "list-key", row
    assert isinstance(row["instance_id"], int), row
    assert row["type"] == "probe", row
    assert row["rewards"] == [{"type": "spirit_stone", "amount": 5}], row
    assert isinstance(row["start_time"], float), row
    assert row["end_time"] > row["start_time"], row


def test_list_publishes_the_string_key_and_the_integer_instance_id(wire):
    """The field named ``event_id`` is a STRING. This is the half of the
    contract the cancel route used to get wrong; it is pinned here because the
    cancel route's own file depends on it."""
    created = wire["scheduler"].create_event(
        EventConfig(key="two-ids", name="Two Ids"), None)

    row = wire["client"].post(LIST, json={"player_id": "owner"}).json()["events"][0]

    assert row["event_id"] == "two-ids" and isinstance(row["event_id"], str)
    assert row["instance_id"] == created and isinstance(row["instance_id"], int)


def test_the_bare_registrar_leaves_the_listing_open_and_production_does_not(wire):
    """THE CONTROL. The two fixtures differ by ONE call, and it decides 200 vs
    401.

    This test previously existed in the opposite form -- it asserted the
    listing needed no session at all -- which is what a bare registrar shows
    and what the running server does NOT do. Kept, inverted, because it is the
    evidence for the section's claim: the guard is load-bearing, and this is
    where that is shown rather than assumed.
    """
    bare = TestClient(wire["app"], raise_server_exceptions=False)
    before = bare.post(LIST, json={"player_id": "owner"})

    auth.attach_player_directory(wire["app"], wire["players_db"])
    from ownership_guard import install_ownership_guard
    install_ownership_guard(wire["app"])
    after = bare.post(LIST, json={"player_id": "owner"})

    assert before.status_code == 200, (
        "the bare registrar now guards /events/list on its own, so this file's "
        f"claim about the startup installer is stale: {before.text[:200]}")
    assert after.status_code == 401, (
        "with the startup installer run, an anonymous caller was still served; "
        f"the listing's sign-in check is gone: {after.status_code} {after.text[:200]}")


# ==========================================================================
# 2b. COVERAGE AREA 1: THE LISTING REQUIRES AUTH
# ==========================================================================

@pytest.mark.parametrize("label,headers", [
    ("absent", {}),
    ("a-bearer-token-never-issued", {"Authorization": "Bearer not-a-real-session"}),
    ("an-operator-token-as-a-bearer", {"Authorization": "Bearer contract-admin-token"}),
    ("an-operator-token-in-its-own-header", ADMIN_HEADER),
], ids=["absent", "never-issued", "operator-as-bearer", "operator-header"])
def test_listing_refuses_every_unauthenticated_caller(guarded_wire, label, headers):
    """The listing needs a PLAYER session. An operator credential is not one.

    The third and fourth cases matter because ``X-Admin-Token`` is a real,
    working credential on this codebase and it opens two of this module's four
    routes. A probe that only tried "no token" and "wrong token" would not
    notice a listing that had been switched over to the operator gate, which
    would be a far worse hole: every operator could read every listing.
    """
    r = guarded_wire["client"].post(LIST, json={"player_id": "owner"},
                                    headers=headers)
    assert r.status_code == 401, (
        f"the listing answered {r.status_code} to a {label} caller: {r.text[:200]}")


def test_listing_serves_the_signed_in_owner(guarded_wire):
    """THE SERVING HALF -- without it, the four refusals above prove nothing.

    Same route, same body, one valid session: it must answer 200.
    """
    guarded_wire["scheduler"].create_event(
        EventConfig(key="served", name="Served"), None)

    r = guarded_wire["client"].post(LIST, json={"player_id": "owner"},
                                   headers=_session("owner"))

    assert r.status_code == 200, r.text
    assert set(r.json()) == {"events"}, r.text


def test_listing_refuses_a_signed_in_caller_naming_someone_else(guarded_wire):
    """Ownership, not merely sign-in.

    ``require_player`` enforces that the body claim matches the session, so a
    valid session for one player naming another is a 403. Without this the
    listing would be a way to read somebody else's view of the event board.
    """
    r = guarded_wire["client"].post(LIST, json={"player_id": OTHER},
                                    headers=_session("owner"))
    assert r.status_code == 403, (
        f"a valid session naming a different player answered {r.status_code}: "
        f"{r.text[:200]}")


def test_a_session_for_a_deleted_account_is_refused(guarded_wire):
    """A real token whose player row is gone answers 401.

    The session proves WHO someone was; it says nothing about whether the
    account still exists. This needs the player directory attached, which is
    why the assertion lives here and not in the bare-``wire`` section.
    """
    del guarded_wire["players_db"]["owner"]

    r = guarded_wire["client"].post(LIST, json={"player_id": "owner"},
                                    headers=_session("owner"))

    assert r.status_code == 401, (
        f"a deleted account's session still reads the listing: {r.status_code}")


def test_the_running_server_agrees_that_the_listing_requires_auth():
    """The same refusal, measured on the assembled ``server.main`` app.

    SKIPS rather than fails when ``main`` will not import, because the blocker
    is a peer's half-written file and not this contract. It re-asserts itself
    with no edit once that clears.
    """
    try:
        from server.main import app as main_app
    except Exception as exc:
        pytest.skip(f"server.main does not import right now "
                    f"({type(exc).__name__}: {exc}); on 2026-10-03 a peer "
                    f"truncated lifecycle_guard.py to 0 bytes mid-write, which "
                    f"breaks that import. Check the file size first.")
    client = TestClient(main_app, raise_server_exceptions=False)
    with client:
        for path, body in ((LIST, {"player_id": "owner"}),
                           (CONVERT, _convert_body("owner"))):
            r = client.post(path, json=body)
            assert r.status_code == 401, (
                f"{path} answered {r.status_code} to an anonymous caller on the "
                f"running server: {r.text[:200]}")


# ==========================================================================
# 2c. COVERAGE AREA 3: PLAYER DISCLOSURE IS PREVENTED
# ==========================================================================
#
# The listing used to open with
#
#     if req.player_id not in players_db:
#         raise HTTPException(404, "Player not found")
#
# which made it an EXISTENCE ORACLE: an anonymous caller posting any id got
# 404 for an id nobody holds and 200 for an id somebody holds, so one request
# per account confirmed one account. The check was removed because
# ``req.player_id`` is read nowhere else in the handler -- the rows come from
# the shared scheduler, not from any player record -- so it could only ever
# subtract.
#
# An oracle is only visible as a DIFFERENCE, so every test here compares two
# replies byte for byte.

def test_an_anonymous_probe_cannot_tell_existing_from_unknown_players(guarded_wire):
    """Both ids answer 401 with a byte-identical body.

    Measured before the fix: existing player -> 200 with an ``events`` key,
    unknown player -> 404 with a ``detail`` key.
    """
    known = guarded_wire["client"].post(LIST, json={"player_id": "owner"})
    unknown = guarded_wire["client"].post(LIST, json={"player_id": "no-such-player"})

    assert known.status_code == unknown.status_code == 401, (
        f"the reply varies by whether the account exists "
        f"({known.status_code} vs {unknown.status_code}); that is an oracle")
    assert known.text == unknown.text, (
        "the two refusals differ in wording, which tells a caller which case it "
        "was for free")


def test_a_signed_in_probe_cannot_tell_other_from_unknown_players(guarded_wire):
    """The same three-way comparison for a caller who IS signed in.

    Naming another real account and naming one that does not exist must be
    indistinguishable, so a signed-in caller cannot walk the roster either.
    """
    other_real = guarded_wire["client"].post(LIST, json={"player_id": OTHER},
                                            headers=_session("owner"))
    unknown = guarded_wire["client"].post(LIST, json={"player_id": "no-such-player"},
                                         headers=_session("owner"))

    assert other_real.status_code == unknown.status_code == 403, (
        f"a signed-in probe can tell the two apart "
        f"({other_real.status_code} vs {unknown.status_code})")
    assert other_real.text == unknown.text


def test_the_listing_reply_carries_no_player_identity(guarded_wire):
    """What a caller receives about THEMSELVES holds no per-player field.

    The rows are the shared scheduler's, so the reply is the same for everyone.
    Asserting the body never names the account keeps it that way: the first
    field added here would be the first thing to start leaking.
    """
    guarded_wire["scheduler"].create_event(
        EventConfig(key="identity", name="Identity"), None)

    r = guarded_wire["client"].post(LIST, json={"player_id": "owner"},
                                    headers=_session("owner"))
    assert r.status_code == 200, r.text
    rows = r.json()["events"]
    assert rows, "no instance was listed, so there was nothing to inspect"
    for row in rows:
        assert "owner" not in json.dumps(row), f"a listing row names the account: {row}"
        assert "player_id" not in row, f"a listing row grew a player_id: {sorted(row)}"


def test_two_different_players_receive_the_same_rows(guarded_wire):
    """Read twice, by two different players, compared whole.

    This is the relationship the whole disclosure question turns on: if two
    callers get different bodies, the reply is carrying per-player data
    regardless of what the fields are called.
    """
    guarded_wire["scheduler"].create_event(
        EventConfig(key="shared", name="Shared"), None)

    mine = guarded_wire["client"].post(LIST, json={"player_id": "owner"},
                                       headers=_session("owner"))
    theirs = guarded_wire["client"].post(LIST, json={"player_id": OTHER},
                                        headers=_session(OTHER))

    assert mine.status_code == theirs.status_code == 200
    assert mine.json() == theirs.json(), (
        "two different players received different listings; the event rows are "
        "meant to be shared, so this reply carries per-player data")


def test_convert_keeps_a_404_because_it_pays_and_the_listing_does_not(wire):
    """WHY THE TWO ROUTES DIFFER, so a well-meaning refactor does not
    "symmetrise" them.

    The listing's existence check gated nothing -- the rows are not the player's
    -- so it was removed. ``POST /events/convert`` PAYS OUT currency, so there
    the check stands between a payout and a misspelled id. Pinned as a
    deliberate difference, and asserted through the guard that fires FIRST so
    the 404 stays reachable rather than merely declared.
    """
    r = wire["client"].post(CONVERT, json=_convert_body(OTHER),
                            headers=_session("owner"))
    assert r.status_code == 403, (
        "the ownership check must fire before the existence check, or the 404 "
        "branch below is unreachable and becomes a hidden existence oracle")


def test_list_does_not_reveal_whether_an_account_exists(wire):
    """THE ORACLE TEST. MEASURED: byte-identical bodies.

    This route used to answer 404 for an id nobody holds and 200 for an id
    somebody holds, which let an anonymous caller confirm one account per
    request. The reply must not vary with existence.
    """
    wire["scheduler"].create_event(EventConfig(key="oracle", name="Oracle"), None)

    known = wire["client"].post(LIST, json={"player_id": "owner"})
    unknown = wire["client"].post(LIST, json={"player_id": "no-such-player"})

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json(), (
        "the reply varies with whether the account exists, which is an "
        f"existence oracle.\n known:  {known.json()}\n unknown: {unknown.json()}")


@pytest.mark.parametrize("body", [
    {},                                  # no player_id at all
    {"player_id": None},                 # explicit null
    {"player_id": 12345},                # wrong type
])
def test_list_requires_a_string_player_id(wire, body):
    """MEASURED: 422 for a missing, null, or non-string player_id.

    This is a uniform validation answer for every caller, which is why it is
    safe to keep the field while dropping the existence check.
    """
    assert wire["client"].post(LIST, json=body).status_code == 422


def test_list_with_no_body_at_all_is_422(wire):
    assert wire["client"].post(LIST).status_code == 422


def test_list_ignores_unknown_body_fields(wire):
    """MEASURED: 200. The model does not forbid extras."""
    r = wire["client"].post(LIST, json={"player_id": "owner", "bogus": 1,
                                       "event_id": 999})
    assert r.status_code == 200, r.text


def test_list_state_filter_selects_and_can_return_nothing(wire):
    """MEASURED: a matching filter returns the row; a non-matching one returns
    an empty list, not an error and not every row."""
    wire["scheduler"].create_event(EventConfig(key="filt", name="Filt"), None)

    matching = wire["client"].post(LIST, json={"player_id": "owner",
                                              "state": "active"})
    empty = wire["client"].post(LIST, json={"player_id": "owner",
                                           "state": "no-such-state"})

    assert matching.status_code == 200
    assert len(matching.json()["events"]) == 1, matching.text
    assert empty.status_code == 200
    assert empty.json() == {"events": []}, empty.text


def test_list_state_filter_must_be_a_string(wire):
    assert wire["client"].post(LIST, json={"player_id": "owner",
                                          "state": 5}).status_code == 422


def test_list_with_no_scheduler_answers_an_empty_list_not_an_error(wire):
    """MEASURED: 200 ``{"events": []}``.

    A missing scheduler is a read with nothing in it, not a failure. Pinned
    because a client drawing a banner cannot tell a 200-with-empty from a
    broken schedule otherwise.
    """
    empty_app = FastAPI()
    event_routes.register_event_routes(empty_app, {}, WalletStore(), None)
    r = TestClient(empty_app, raise_server_exceptions=False).post(
        LIST, json={"player_id": "owner"})

    assert r.status_code == 200, r.text
    assert r.json() == {"events": []}, r.text


def test_days_remaining_is_derived_per_state(wire):
    """MEASURED, as RELATIONSHIPS rather than literals.

    The four branches of the reply: a scheduled event counts down to its start,
    a running one to its end, a finished one to its post-end, and anything else
    reports zero. Each is measured from a hand-set clock so no wall-clock flake.
    """
    import time
    scheduler = wire["scheduler"]
    now = time.time()

    def make(key):
        return scheduler.create_event(EventConfig(key=key, name=key), None)

    pre = make("pre")
    scheduler._instances[pre].state = "pre_start"
    scheduler._instances[pre].start_time = now + 5 * 86400
    scheduler._instances[pre].end_time = now + 6 * 86400

    active = make("active")
    scheduler._instances[active].state = "active"
    scheduler._instances[active].end_time = now + 3 * 86400

    post = make("post")
    scheduler._instances[post].state = "post_end"
    scheduler._instances[post].post_end_time = now + 2 * 86400

    expired = make("expired")
    scheduler._instances[expired].state = "expired"

    rows = {r["event_id"]: r
            for r in wire["client"].post(LIST, json={"player_id": "owner"}
                                         ).json()["events"]}

    assert rows["pre"]["days_remaining"] == 5, rows["pre"]
    assert rows["active"]["days_remaining"] == 3, rows["active"]
    assert rows["post"]["days_remaining"] == 2, rows["post"]
    assert rows["expired"]["days_remaining"] == 0, rows["expired"]


def test_days_remaining_is_zero_when_a_post_end_event_has_no_post_end_time(wire):
    """MEASURED: 0, not a crash. The instance carries a null post-end stamp
    whenever the schedule was created without a post-end window."""
    scheduler = wire["scheduler"]
    iid = scheduler.create_event(EventConfig(key="no-post", name="No Post"), None)
    scheduler._instances[iid].state = "post_end"
    scheduler._instances[iid].post_end_time = None

    row = [r for r in wire["client"].post(LIST, json={"player_id": "owner"}
                                          ).json()["events"]
           if r["event_id"] == "no-post"][0]

    assert row["days_remaining"] == 0, row


# ==========================================================================
# 3. POST /events/convert  -- the paying route
# ==========================================================================

def test_convert_reply_shape_and_server_side_rate(wire):
    """MEASURED shape; the payout is pinned as a RELATIONSHIP.

    ``souldust`` is read against the module's own rate constant rather than a
    literal, so re-balancing the economy keeps this test green while a route
    that computed its own copy of the price still goes red.
    """
    r = wire["client"].post(CONVERT, json=_convert_body("owner"),
                            headers=_session("owner"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "event_id", "item_id", "quantity", "souldust",
                         "converted"}, body
    assert body["ok"] is True and body["converted"] is True, body
    assert body["event_id"] == EVENT and body["item_id"] == ITEM, body
    assert body["quantity"] == 10, body
    assert body["souldust"] == 10 * EVENT_ITEM_SOULDUST_RATE, (
        f"the payout is not quantity x the server-side rate: {body}")
    assert _items(wire["players_db"], "owner") == 40, "the items were not debited"


def test_convert_requires_a_real_session(wire):
    """MEASURED: 401 for no header and for a token that was never issued."""
    assert wire["client"].post(CONVERT, json=_convert_body("owner")).status_code == 401
    assert wire["client"].post(
        CONVERT, json=_convert_body("owner"),
        headers={"Authorization": "Bearer not-a-real-session"}
    ).status_code == 401
    assert _items(wire["players_db"], "owner") == 50, "items were spent anyway"


@pytest.mark.parametrize("missing", ["player_id", "event_id", "item_id",
                                     "quantity"])
def test_convert_requires_all_four_body_fields(wire, missing):
    """MEASURED: 422 naming the omitted field.

    A 422 here is validation, not a refusal -- the authz tests below always
    carry a complete body so they cannot pass for this reason.
    """
    body = _convert_body("owner")
    del body[missing]

    r = wire["client"].post(CONVERT, json=body, headers=_session("owner"))

    assert r.status_code == 422, r.text
    assert any(e.get("loc", [""])[-1] == missing for e in r.json()["detail"]), \
        r.text


def test_convert_refuses_a_signed_in_player_naming_someone_else(wire):
    """MEASURED: 403 and nothing is debited or paid.

    Two gates sit in front of this payout and both are load-bearing. The
    session check refuses the mismatch first; the handler then repeats the
    comparison so a future refactor of the claim reader cannot silently reopen
    the route (see the control at the end of this section).
    """
    r = wire["client"].post(CONVERT, json=_convert_body("owner"),
                            headers=_session(OTHER))

    assert r.status_code == 403, f"a stranger spent another player's items: {r.text}"
    assert _items(wire["players_db"], "owner") == 50, "the owner's items were spent"
    assert _items(wire["players_db"], OTHER) == 50


def test_convert_answers_404_when_the_session_names_an_absent_player(wire):
    """MEASURED: 404 "Player not found".

    Reachable only by a session whose player id is not in the player store --
    the store is a plain dict in this harness, so no row means no row. Pinned
    because the sibling listing deliberately answers 200 for the same
    situation, and a client that assumes the two agree will be wrong.
    """
    _player(wire["players_db"], "owner")
    del wire["players_db"]["owner"]

    r = wire["client"].post(CONVERT, json=_convert_body("owner"),
                            headers=_session("owner"))

    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Player not found", r.text


@pytest.mark.parametrize("quantity", [0, -1])
def test_convert_refuses_a_non_positive_quantity(wire, quantity):
    """MEASURED: 400, and the detail is an OBJECT, not a string.

    ``detail`` here is ``{"ok": false, "error": ...}``. A client that renders
    ``detail`` as text will show a Python dict.
    """
    r = wire["client"].post(CONVERT,
                            json=_convert_body("owner", quantity=quantity),
                            headers=_session("owner"))

    assert r.status_code == 400, r.text
    assert r.json()["detail"]["ok"] is False, r.text
    assert "error" in r.json()["detail"], r.text


def test_convert_refuses_more_items_than_the_player_holds(wire):
    """MEASURED: 400 "insufficient event items", and the balance is untouched."""
    r = wire["client"].post(CONVERT,
                            json=_convert_body("owner", quantity=9999),
                            headers=_session("owner"))

    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "insufficient event items", r.text
    assert _items(wire["players_db"], "owner") == 50


def test_convert_refuses_a_fractional_quantity_at_validation(wire):
    """MEASURED: 422. A whole number with a fraction is refused by the model;
    a whole float is not (see the coercion test below)."""
    r = wire["client"].post(CONVERT,
                            json=_convert_body("owner", quantity=2.5),
                            headers=_session("owner"))
    assert r.status_code == 422, r.text


def test_convert_accepts_a_caller_supplied_rate_but_ignores_it(wire):
    """MEASURED: the payout does not move when the caller names a rate.

    The body field is retained only so an older client still validates. This
    is the arm that proves the rate is server-side.
    """
    r = wire["client"].post(
        CONVERT,
        json=_convert_body("owner", quantity=4, souldust_per_item=999999,
                           idempotency_key="rate-probe"),
        headers=_session("owner"))

    assert r.status_code == 200, r.text
    assert r.json()["souldust"] == 4 * EVENT_ITEM_SOULDUST_RATE, (
        "a caller-supplied rate reached the payout: " + r.text)


def test_convert_replaying_the_same_idempotency_key_pays_once(wire):
    """MEASURED: the second identical call is 200 again, is marked
    ``_replayed``, and does not move the balance a second time."""
    body = _convert_body("owner", idempotency_key="replay-key")
    headers = _session("owner")

    first = wire["client"].post(CONVERT, json=body, headers=headers)
    second = wire["client"].post(CONVERT, json=body, headers=headers)

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert "_replayed" not in first.json(), first.text
    assert second.json()["_replayed"] is True, (
        f"the replay was not marked: {second.text}")
    assert _items(wire["players_db"], "owner") == 40, (
        "the replay debited the items a second time")


def test_convert_without_an_idempotency_key_derives_one_that_ignores_quantity(
        wire):
    """MEASURED, and this is a real wire trap: with no key in the body the
    server derives one from (event, player, item) ONLY -- quantity is not part
    of it. So a second call for the same item with a DIFFERENT quantity replays
    the FIRST receipt instead of converting the new amount.

    A client that omits the key and changes the quantity gets a 200 that
    silently did nothing. Pinned so the behaviour is a decision, not a
    surprise.
    """
    headers = _session("owner")

    first = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=2),
                                headers=headers)
    second = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=7),
                                 headers=headers)

    assert first.status_code == second.status_code == 200
    assert second.json().get("_replayed") is True, (
        f"a different quantity with no idempotency key did not replay: {second.text}")
    assert second.json()["quantity"] == 2, (
        "the derived key does not include quantity, so the receipt returned is "
        f"the first one: {second.text}")
    assert _items(wire["players_db"], "owner") == 48, (
        "the second call debited despite replaying")


def test_convert_accepts_a_numeric_string_and_a_whole_float_as_the_quantity(
        wire):
    """MEASURED: both are accepted as whole numbers.

    The model is in lax mode, so ``"3"`` and ``3.0`` are coerced to integers
    before the handler sees them. Only a value with a real fraction (2.5) is
    refused. Pinned because a client sending a JSON number-as-string is
    accepted rather than told off.
    """
    for value, expected in (("3", 3), (3.0, 3)):
        players = {}
        _player(players, f"p{value}")
        app = FastAPI()
        event_routes.register_event_routes(app, players, WalletStore(),
                                           EventScheduler())
        r = TestClient(app, raise_server_exceptions=False).post(
            CONVERT, json=_convert_body(f"p{value}", quantity=value,
                                        idempotency_key=f"coerce-{value}"),
            headers=_session(f"p{value}"))

        assert r.status_code == 200, f"{value!r}: {r.text}"
        assert r.json()["quantity"] == expected, f"{value!r}: {r.text}"


def test_convert_removes_the_item_from_the_inventory_once_it_hits_zero(wire):
    """MEASURED: the key is dropped, not left at zero."""
    wire["players_db"]["owner"]["inventory"][ITEM] = 10

    r = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=10),
                            headers=_session("owner"))

    assert r.status_code == 200, r.text
    assert ITEM not in wire["players_db"]["owner"]["inventory"], (
        "a fully consumed item was left behind at zero")


def test_convert_ignores_unknown_body_fields(wire):
    """MEASURED: 200. The model does not forbid extras."""
    r = wire["client"].post(CONVERT,
                            json=_convert_body("owner", bogus=1,
                                               idempotency_key="extra"),
                            headers=_session("owner"))
    assert r.status_code == 200, r.text


# --- CONTROL: the second gate is load-bearing on its own -------------------
#
# The session check already refuses a mismatch, so over HTTP the handler's own
# ``elif identity.player_id != req.player_id`` line is never the one that
# answers. That makes it look like dead code. This control removes the FIRST
# gate -- by overriding the dependency to hand back an identity naming somebody
# else -- and shows the SECOND gate then answers 403 by itself. If the handler's
# own comparison is deleted, this control goes red.

def test_control_the_handler_owns_a_second_independent_ownership_gate(wire):
    wire["app"].dependency_overrides[auth.require_player] = lambda: \
        auth.PlayerIdentity(player_id=OTHER)
    client = TestClient(wire["app"], raise_server_exceptions=False)

    r = client.post(CONVERT, json=_convert_body("owner"),
                    headers=_session("owner"))

    assert r.status_code == 403, (
        "with the session gate neutralised the handler did NOT refuse, so its "
        f"own comparison is not load-bearing: {r.status_code} {r.text}")
    assert r.json()["detail"] == "not your player", r.text
    assert _items(wire["players_db"], "owner") == 50, "items were spent anyway"


def test_control_the_internal_mint_token_cannot_reach_the_payout(wire):
    """MEASURED: 401.

    The handler opens with an ``if identity.internal:`` branch that trusts the
    server-to-server mint token to name its own target. Over HTTP that branch
    cannot be taken: the route authenticates with the plain session guard,
    which does not accept the mint token, so a request carrying it is refused
    before the handler runs. Pinned so nobody reads the branch as a live
    alternate caller.
    """
    os.environ["WC_INTERNAL_EARN_TOKEN"] = "contract-internal-token"
    try:
        r = wire["client"].post(
            CONVERT, json=_convert_body("owner"),
            headers={"Authorization": "Bearer contract-internal-token"})
    finally:
        os.environ.pop("WC_INTERNAL_EARN_TOKEN", None)

    assert r.status_code == 401, (
        f"the internal mint token reached the payout route: {r.status_code} {r.text}")
    assert _items(wire["players_db"], "owner") == 50


# ==========================================================================
# 4. POST /admin/events/schedule  -- operator-only
# ==========================================================================

def test_schedule_uses_a_dedicated_admin_header_not_a_player_session(wire):
    """MEASURED: 401 for no token, a wrong token, and a player bearer token.

    The operator credential is a plain header, and a player session is not one.
    """
    body = {"event_key": "hdr", "name": "Hdr"}

    assert wire["client"].post(SCHEDULE, json=body).status_code == 401
    assert wire["client"].post(
        SCHEDULE, json=body, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert wire["client"].post(
        SCHEDULE, json=body, headers=_session("owner")).status_code == 401
    assert wire["client"].post(SCHEDULE, json=body,
                               headers=ADMIN_HEADER).status_code == 200


def test_schedule_reply_shape_and_the_created_instance(wire):
    """MEASURED: ``{"ok": true, "event_id": N}`` where N is the INTEGER
    instance id -- not the string key. The listing publishes the key under the
    name ``event_id``; this route publishes the integer under the same name.
    A client reading one and posting it to the other must use both forms."""
    r = wire["client"].post(
        SCHEDULE,
        json={"event_key": "made", "name": "Made", "event_type": "seasonal",
              "duration_hours": 6, "pre_start_hours": 1, "post_end_hours": 2,
              "rewards": [{"type": "spirit_stone", "amount": 3}]},
        headers=ADMIN_HEADER)

    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ok", "event_id"}, r.text
    assert r.json()["ok"] is True
    created = r.json()["event_id"]
    assert isinstance(created, int), (
        f"schedule published a non-integer event_id: {r.text}")

    instance = wire["scheduler"]._instances[created]
    assert instance.event_key == "made"
    assert instance.event_type == "seasonal"
    assert instance.rewards == [{"type": "spirit_stone", "amount": 3}]


def test_schedule_applies_the_documented_defaults(wire):
    """MEASURED: the defaults are applied to the created instance, not just
    echoed. Duration is pinned as a RELATIONSHIP (end minus start), so a
    re-balance of the default keeps this green."""
    r = wire["client"].post(SCHEDULE,
                            json={"event_key": "dflt", "name": "Dflt"},
                            headers=ADMIN_HEADER)
    instance = wire["scheduler"]._instances[r.json()["event_id"]]

    assert instance.event_type == "generic"
    assert instance.end_time - instance.start_time == pytest.approx(
        24 * 3600, abs=5), (
        "the default 24-hour window is no longer what an omitted "
        f"duration_hours produces: {instance.end_time - instance.start_time}")


def test_schedule_requires_a_key_and_a_name(wire):
    """MEASURED: 422. Both are required; the rest have defaults."""
    assert wire["client"].post(SCHEDULE, json={}, headers=ADMIN_HEADER).status_code == 422
    assert wire["client"].post(SCHEDULE, json={"event_key": "only"},
                               headers=ADMIN_HEADER).status_code == 422
    assert wire["client"].post(SCHEDULE, json={"name": "only"},
                               headers=ADMIN_HEADER).status_code == 422
    # Both together, with every optional field omitted, is the smallest valid
    # body -- the arm that proves the 422s above are about the missing field.
    assert wire["client"].post(SCHEDULE, json={"event_key": "k", "name": "n"},
                               headers=ADMIN_HEADER).status_code == 200


def test_schedule_validates_the_rewards_shape(wire):
    """MEASURED: rewards must be a list of objects. Two different 422s."""
    bad_list = wire["client"].post(
        SCHEDULE, json={"event_key": "r1", "name": "R1", "rewards": "nope"},
        headers=ADMIN_HEADER)
    bad_item = wire["client"].post(
        SCHEDULE, json={"event_key": "r2", "name": "R2", "rewards": [1, 2]},
        headers=ADMIN_HEADER)

    assert bad_list.status_code == 422, bad_list.text
    assert bad_item.status_code == 422, bad_item.text


def test_schedule_ignores_unknown_body_fields(wire):
    """MEASURED: 200. Including the ``schedule`` field, which the model
    accepts and the handler never reads -- a caller posting a recurring rule
    gets a single one-off event and a 200 saying it worked."""
    r = wire["client"].post(
        SCHEDULE,
        json={"event_key": "ign", "name": "Ign", "schedule": {"cron": "* * * * *"},
              "bogus": 1},
        headers=ADMIN_HEADER)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("duration", [0, -5])
def test_schedule_accepts_a_non_positive_duration(wire, duration):
    """MEASURED: 200 with an end time at or before the start.

    There is no lower bound on ``duration_hours``, so an operator can create an
    event that is already over. Pinned as measured rather than as wished-for:
    it is an operator-only route, and the point of the test is that the shape
    is KNOWN, not that it is good.
    """
    r = wire["client"].post(
        SCHEDULE, json={"event_key": f"d{duration}", "name": "D",
                        "duration_hours": duration},
        headers=ADMIN_HEADER)

    assert r.status_code == 200, r.text
    instance = wire["scheduler"]._instances[r.json()["event_id"]]
    assert instance.end_time <= instance.start_time, (
        f"duration_hours={duration} produced a forward window: "
        f"{instance.start_time} -> {instance.end_time}")


def test_schedule_with_no_scheduler_answers_503(wire):
    """MEASURED: 503. A missing scheduler must not look like a created event."""
    app = FastAPI()
    event_routes.register_event_routes(app, {}, WalletStore(), None)
    r = TestClient(app, raise_server_exceptions=False).post(
        SCHEDULE, json={"event_key": "n", "name": "N"}, headers=ADMIN_HEADER)

    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "Event scheduler not available", r.text


def test_create_event_instance_is_usable_directly_and_fails_closed(wire):
    """The create step is a plain function, not a route, because two callers
    reach it. MEASURED: it refuses when handed no scheduler."""
    from fastapi import HTTPException

    for missing in (None, False):
        with pytest.raises(HTTPException) as caught:
            event_routes.create_event_instance(
                event_routes.AdminEventCreateRequest(event_key="a", name="B"),
                missing)
        assert caught.value.status_code == 503
        assert caught.value.detail == "Event scheduler not available"


# ==========================================================================
# 5. POST /admin/events/{event_id}/cancel  -- operator-only
# ==========================================================================

def test_cancel_accepts_both_id_forms_the_listing_publishes(wire):
    """MEASURED: the string key AND the integer instance id both answer 200.

    The listing publishes the key under the name ``event_id`` and the integer
    under ``instance_id``; this route accepts either form. Widening the
    accepted forms must not widen WHO may cancel -- see the gate tests below.
    """
    created = wire["scheduler"].create_event(
        EventConfig(key="both-forms", name="Both"), None)

    by_key = wire["client"].post(CANCEL.format(event_id="both-forms"),
                                 headers=ADMIN_HEADER)
    by_int = wire["client"].post(CANCEL.format(event_id=created),
                                 headers=ADMIN_HEADER)

    assert by_key.status_code == 200, by_key.text
    assert by_int.status_code == 200, by_int.text
    assert by_key.json() == {"ok": True}
    assert by_int.json() == {"ok": True}


def test_cancel_sets_the_state_to_expired(wire):
    created = wire["scheduler"].create_event(
        EventConfig(key="gone", name="Gone"), None)

    r = wire["client"].post(CANCEL.format(event_id="gone"), headers=ADMIN_HEADER)

    assert r.status_code == 200, r.text
    assert wire["scheduler"]._instances[created].state == "expired", (
        "the cancel answered ok but the instance is not expired")


def test_cancel_is_repeatable(wire):
    """MEASURED: cancelling an already-expired instance is 200 again."""
    wire["scheduler"].create_event(EventConfig(key="twice", name="Twice"), None)

    first = wire["client"].post(CANCEL.format(event_id="twice"),
                                headers=ADMIN_HEADER)
    second = wire["client"].post(CANCEL.format(event_id="twice"),
                                 headers=ADMIN_HEADER)

    assert first.status_code == second.status_code == 200, (first.text, second.text)


def test_cancel_refuses_an_id_that_matches_nothing(wire):
    """MEASURED: 404 "Event not found".

    Accepting two id forms must not make the match match everything.
    """
    r = wire["client"].post(CANCEL.format(event_id="no-such-event"),
                            headers=ADMIN_HEADER)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Event not found", r.text


def test_cancel_reads_only_the_path_segment(wire):
    """MEASURED: a body or query parameter naming another event is ignored.

    The id lives in the PATH. A client that sends the id in both places gets
    the path one cancelled, silently.
    """
    wire["scheduler"].create_event(EventConfig(key="target", name="T"), None)
    wire["scheduler"].create_event(EventConfig(key="decoy", name="D"), None)

    r = wire["client"].post(CANCEL.format(event_id="target"),
                            json={"event_id": "decoy"},
                            params={"event_id": "decoy"}, headers=ADMIN_HEADER)

    assert r.status_code == 200, r.text
    assert wire["scheduler"]._instances[
        max(wire["scheduler"]._instances)].state != "expired", (
        "a decoy id in the body or query was cancelled instead of the path id")


def test_cancel_requires_the_admin_token(wire):
    """MEASURED: 401 for nothing, for a wrong token, and for a player session."""
    wire["scheduler"].create_event(EventConfig(key="guarded", name="G"), None)
    path = CANCEL.format(event_id="guarded")

    assert wire["client"].post(path).status_code == 401
    assert wire["client"].post(
        path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert wire["client"].post(path, headers=_session("owner")).status_code == 401
    assert wire["client"].post(path, headers=ADMIN_HEADER).status_code == 200


def test_cancel_with_no_scheduler_answers_503(wire):
    app = FastAPI()
    event_routes.register_event_routes(app, {}, WalletStore(), None)
    r = TestClient(app, raise_server_exceptions=False).post(
        CANCEL.format(event_id="anything"), headers=ADMIN_HEADER)

    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "Event scheduler not available", r.text


def test_both_admin_routes_fail_closed_when_the_token_is_unset(wire):
    """MEASURED: 503 for EVERYONE when ``WC_ADMIN_TOKEN`` is unset.

    The operator guard consults only the environment. With it unset the guard
    refuses every caller -- including one sending a token -- rather than
    treating "unconfigured" as "allowed". 503 rather than 401 because the
    server, not the caller, is the thing that is broken.
    """
    saved = os.environ.pop("WC_ADMIN_TOKEN", None)
    try:
        for path, payload in ((CANCEL.format(event_id="x"), None),
                              (SCHEDULE, {"event_key": "a", "name": "b"})):
            r = wire["client"].post(path, json=payload,
                                    headers={"X-Admin-Token": "anything"})
            assert r.status_code == 503, f"{path}: {r.status_code} {r.text}"
            assert "WC_ADMIN_TOKEN" in r.json()["detail"], r.text
    finally:
        if saved is not None:
            os.environ["WC_ADMIN_TOKEN"] = saved
        else:
            os.environ["WC_ADMIN_TOKEN"] = ADMIN_TOKEN


def test_the_admin_gate_is_checked_before_the_scheduler(wire):
    """MEASURED: with no scheduler AND no token the answer is 401, not 503.

    Ordering is pinned because it is the difference between "you are not
    allowed" and "this server is broken". The authorisation answer comes
    first, so an unauthorised caller learns nothing about the scheduler.
    """
    app = FastAPI()
    event_routes.register_event_routes(app, {}, WalletStore(), None)
    client = TestClient(app, raise_server_exceptions=False)

    unauthenticated = client.post(CANCEL.format(event_id="x"))
    authorised = client.post(CANCEL.format(event_id="x"), headers=ADMIN_HEADER)

    assert unauthenticated.status_code == 401, unauthenticated.text
    assert authorised.status_code == 503, authorised.text


# ==========================================================================
# 6. COVERAGE AREA 4: NON-FINITE VALUES
# ==========================================================================
#
# Python's JSON reader accepts ``NaN``, ``Infinity`` and ``-Infinity`` as bare
# literals. A hand-written client, a proxy that rewrites numbers, or a body
# assembled by string concatenation all put them on the wire.
#
# Starlette's JSONResponse serialises with ``allow_nan=False``. So when pydantic
# REJECTS a non-finite value and echoes the offending input back inside its
# 422 detail, the ERROR REPLY ITSELF cannot be encoded -- the server raises
# ValueError out of its own exception handler and the caller sees a 500.
#
# THE TWO OPEN DEFECTS BELOW ARE ``xfail(strict=True)``. Each asserts the
# CORRECT behaviour, currently fails, and turns RED the moment the fix lands --
# which is the entire point. A plain ``xfail`` would stay green forever and stop
# watching. Do not relax these to make a run green: the fix is either a
# ``allow_nan`` escape hatch on the error path or a non-finite guard in the
# model, and one of those lands, and then this marker must come off.
#
#   DEFECT 1 -- a non-finite value in a REQUEST answers 500, not 422.
#   DEFECT 2 -- an operator can poison the listing for EVERY player, forever.
#
# Both are correctness/availability findings, not money findings: the tests at
# the end of the section assert the halves that DO hold, namely that nothing is
# written and nothing is paid, so a reader knows the blast radius.

NON_FINITE = ("NaN", "Infinity", "-Infinity")


def _raw(client, path: str, payload: str, headers: dict | None = None):
    """POST a body containing a non-finite literal.

    httpx refuses to ENCODE NaN or Infinity itself -- its JSON writer also sets
    ``allow_nan=False`` -- so a probe using ``json=`` never reaches the server
    and would measure the test client instead of the route. The bytes are
    written by hand to get the literal onto the wire.

    ``Content-Type`` is set explicitly because ``content=`` does not infer it.
    Without it FastAPI reads the body as a string and answers 422 for a reason
    that has nothing to do with the number under test.
    """
    sent = dict(headers or {})
    sent["Content-Type"] = "application/json"
    return client.post(path, content=payload.encode("utf-8"), headers=sent)


@pytest.mark.parametrize("literal", NON_FINITE, ids=lambda s: s)
@pytest.mark.xfail(strict=True, reason="DEFECT 1: a non-finite body value answers "
                                      "500, not 422 -- pydantic echoes the value "
                                      "in its 422 detail and Starlette encodes "
                                      "with allow_nan=False")
def test_non_finite_quantity_is_a_422_not_a_500(guarded_wire, literal):
    """A number the server cannot use is a client error, not a server crash."""
    payload = (f'{{"player_id": "owner", "event_id": "{EVENT}", '
               f'"item_id": "{ITEM}", "quantity": {literal}}}')

    r = _raw(guarded_wire["client"], CONVERT, payload, _session("owner"))

    assert r.status_code == 422, (
        f"quantity={literal} answered {r.status_code}: {r.text[:160]}")


@pytest.mark.parametrize("literal", NON_FINITE, ids=lambda s: s)
@pytest.mark.xfail(strict=True, reason="DEFECT 1: same cause -- the failure is in "
                                      "the error REPLY, so it hits every route "
                                      "with a pydantic body, not just this one")
def test_non_finite_listing_filter_is_a_422_not_a_500(guarded_wire, literal):
    payload = f'{{"player_id": "owner", "state": {literal}}}'

    r = _raw(guarded_wire["client"], LIST, payload, _session("owner"))

    assert r.status_code == 422, f"state={literal} answered {r.status_code}"


@pytest.mark.parametrize("literal", NON_FINITE, ids=lambda s: s)
@pytest.mark.xfail(strict=True, reason="DEFECT 1: an operator posting a "
                                      "non-finite duration gets 500, so an admin "
                                      "dashboard reads a server fault")
def test_non_finite_duration_is_a_422_not_a_500(wire, literal):
    payload = (f'{{"event_key": "nf-{literal}", "name": "N", '
               f'"duration_hours": {literal}}}')

    r = _raw(wire["client"], SCHEDULE, payload, ADMIN_HEADER)

    assert r.status_code == 422, (
        f"duration_hours={literal} answered {r.status_code}: {r.text[:160]}")


@pytest.mark.xfail(strict=True, reason="DEFECT 2: rewards rows pass through "
                                      "unvalidated (List[dict]), so one NaN row "
                                      "breaks /events/list for every player "
                                      "permanently -- cancelling does not help")
def test_one_non_finite_reward_row_cannot_break_the_listing_for_everyone(wire):
    """The availability half of the non-finite contract.

    ``AdminEventScheduleRequest.rewards`` is ``List[dict]``, so the reward rows
    are stored unvalidated. A row carrying ``{"amount": NaN}`` is ACCEPTED, and
    then every later ``POST /events/list`` answers 500 -- for every player, for
    the life of the process -- because the poison row is still in the instance's
    own ``rewards`` field.

    Cancelling does NOT clear it: the instance survives a cancel by design (see
    ``test_cancel_is_repeatable``), so the row stays in the listing. Asserted
    after the cancel too, so the fix cannot be "delete the row on cancel".
    """
    payload = ('{"event_key": "poison", "name": "Poison", "duration_hours": 1, '
               '"rewards": [{"item": "x", "amount": NaN}]}')

    created = _raw(wire["client"], SCHEDULE, payload, ADMIN_HEADER)
    assert created.status_code == 200, created.text[:200]

    listed = wire["client"].post(LIST, json={"player_id": "owner"})
    assert listed.status_code == 200, (
        "one event with a NaN reward broke the listing for every player: "
        f"{listed.status_code} {listed.text[:160]}")

    cancelled = wire["client"].post(CANCEL.format(event_id="poison"),
                                    headers=ADMIN_HEADER)
    assert cancelled.status_code == 200, cancelled.text[:200]
    after = wire["client"].post(LIST, json={"player_id": "owner"})
    assert after.status_code == 200, (
        "cancelling the poisoned event did not restore the listing; the row "
        "survives a cancel, so the poison survives it too")


# --- the halves of the contract that DO hold ------------------------------
#
# The 500 above is a wrong status code on a REFUSAL, not a partial write. These
# assert the half that matters for money, and they read the STORE rather than
# the reply, so they stay meaningful even while the status code is wrong.

@pytest.mark.parametrize("literal", NON_FINITE, ids=lambda s: s)
def test_a_non_finite_value_is_refused_before_anything_is_written(wire, literal):
    """No instance created, no items spent, no currency paid."""
    instances_before = len(wire["scheduler"]._instances)
    items_before = _items(wire["players_db"], "owner")

    payload = (f'{{"player_id": "owner", "event_id": "{EVENT}", '
               f'"item_id": "{ITEM}", "quantity": {literal}}}')
    r = _raw(wire["client"], CONVERT, payload, _session("owner"))

    assert r.status_code >= 400, (
        f"quantity={literal} was ACCEPTED with {r.status_code}")
    assert len(wire["scheduler"]._instances) == instances_before, (
        "a refused convert still created an event instance")
    assert _items(wire["players_db"], "owner") == items_before, (
        "a refused convert still debited the player's items")


@pytest.mark.parametrize("literal", NON_FINITE, ids=lambda s: s)
def test_a_non_finite_duration_creates_no_event(wire, literal):
    """The operator-side pairing: the event is refused, not created."""
    payload = (f'{{"event_key": "nfd-{literal}", "name": "N", '
               f'"duration_hours": {literal}}}')
    before = len(wire["scheduler"]._instances)

    r = _raw(wire["client"], SCHEDULE, payload, ADMIN_HEADER)

    assert r.status_code >= 400, f"duration={literal} was ACCEPTED ({r.status_code})"
    assert len(wire["scheduler"]._instances) == before, (
        "a refused schedule still created an instance")


def test_a_non_finite_reward_row_writes_no_currency(wire):
    """What makes DEFECT 2 availability rather than a money bug.

    Pinned separately so a fix to the listing cannot quietly start paying the
    poisoned row out.
    """
    before = dict(wire["players_db"]["owner"])
    payload = ('{"event_key": "poison-money", "name": "N", "duration_hours": 1, '
               '"rewards": [{"item": "x", "amount": NaN}]}')

    created = _raw(wire["client"], SCHEDULE, payload, ADMIN_HEADER)

    assert created.status_code in (200, 422), created.text[:160]
    assert dict(wire["players_db"]["owner"]) == before, (
        "scheduling an event changed a player's own row")


def test_a_well_formed_huge_quantity_is_refused_by_the_payout_not_by_a_crash(wire):
    """A number too large to be an item count is refused by a named rule.

    MEASURED: ``quantity: 10**30`` answers 400 ``insufficient event items``
    (the player holds none) and ``-5`` answers 400 naming the positive-whole-
    quantity rule. Neither reaches arithmetic.
    """
    huge = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=10 ** 30),
                              headers=_session("owner"))
    negative = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=-5),
                                  headers=_session("owner"))

    assert huge.status_code == 400, huge.text
    assert negative.status_code == 400, negative.text
    assert "quantity" in str(negative.json()["detail"]).lower()


def test_a_plain_mistyped_number_is_already_a_422(wire):
    """THE CONTROL for the xfails above -- read this before trusting them.

    A number that is merely the wrong SHAPE (``"abc"``, ``1.5``) is refused
    cleanly with a 422 today, on the same pydantic path. If this ever stops
    answering 422 then the non-finite failures are NOT about non-finite values:
    they would be a broken model, and DEFECT 1 would be misdiagnosed and
    "fixed" in the wrong place.
    """
    for value in ("abc", 1.5):
        r = wire["client"].post(CONVERT, json=_convert_body("owner", quantity=value),
                                headers=_session("owner"))
        assert r.status_code == 422, (
            f"quantity={value!r} answered {r.status_code}; the plain mistype "
            f"path is broken, so the NaN finding is misdiagnosed")