"""Contract test for ``server/dungeon_routes.py`` -- all 14 ``/dungeons`` routes.

WHY A CONTRACT TEST AND NOT ANOTHER AUTHZ SUITE
------------------------------------------------
``server/tests/test_dungeon_route_participation_authz.py`` already proves who may
call each route. This file pins the OTHER half of the contract, which no client
panel can be written against until it exists: the exact request shape each route
demands and the exact key set each one hands back. A prior worker built a
``DungeonView`` Godot panel against these routes and it was never mounted, so the
shapes below were never read by anything. They are pinned here so the next panel
is written against measurement rather than against a guess.

Everything asserted here was MEASURED on the live app with
``fastapi.testclient.TestClient`` before this file existed. Where a docstring
quotes a status code, that code is a real response body captured from a run, not a
prediction. The measurements are reproducible: run either of the two scratch
probes listed under REPRODUCING THIS FILE.

A contract test is allowed to freeze a value -- that is what a contract IS. What
it must not do is assert something that no caller could rely on, so this file
avoids three traps the module makes easy to fall into:

  * An empty template list is a FILTER RESULT, not a failure. ``realm=mortal``
    legitimately returns ``[]``, so no arm here asserts "200 implies non-empty"
    without also pinning the realm/level pair that makes it non-empty.
  * A refusal that reads 403 may still have written something. Every refusal arm
    also asserts the thing the caller was NOT allowed to do did not happen.
  * The four request-body field names are load-bearing and were renamed once
    already (``invitee_id``, was ``player_id``). They are pinned by name.

THE RELATION, NOT THE SNAPSHOT
------------------------------
Where a value could reasonably change, this file asserts a relationship instead
of a number. The template catalogue is the case that matters: rather than pinning
"4 templates", it pins that raising a player's realm or level NEVER REMOVES a
template from the list. That is the property a client depends on, and it stays
true when a fifth dungeon is added.

AN UNRANKABLE REALM IS A 400, NOT A 500 (was a defect, now fixed)
-----------------------------------------------------------------
``GET /dungeons/templates`` with a realm string the server cannot rank used to
answer HTTP 500, because ``DungeonManager.get_available_templates`` raises
``UnknownRealmError`` and no handler caught it -- so the body was the plain-text
string "Internal Server Error" and a client reading ``detail`` got nothing. The
route now catches it and answers 400 in the standard ``{"detail": ...}`` shape,
naming both the rejected realm and the realms the ladder knows.

Kept pinned because the SHAPE is the contract, not the status code: a future
refactor that lets this decay into a silent empty list would pass a loose
"not 500" assertion but would reintroduce the ambiguity -- an empty list is
indistinguishable from "correctly gated out", which is how this hid.

ORDERING HAZARD -- READ BEFORE ADDING A TEST
--------------------------------------------
``POST /dungeons/party/leave`` DISBANDS a solo leader's party, and ``advance``,
``fail``, ``abandon`` and ``claim-loot`` each exhaust their run. Run early, a
leave makes every later owner arm answer 403 and look like a guard refusing its
own owner. Every arm below therefore builds its own party and its own instance;
none of them share mutable dungeon state.

REPRODUCING THIS FILE
---------------------
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_DB -u WC_PG_HOST -u PYTHONPATH python -m pytest \\
        server/tests/test_dungeon_routes_contract.py -q

    WC_STORAGE_BACKEND=memory WC_PG_HOST= python -m pytest \\
        server/tests/test_dungeon_routes_contract.py -q     # solo run
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.dungeons import get_dungeon_manager  # noqa: E402
from server.main import app, players_db  # noqa: E402
import auth as auth_module  # noqa: E402

# `import auth` (BARE, not `server.auth`) is load-bearing. server/main.py binds
# the same bare module object, and the session store is a per-module singleton,
# so a package-qualified import mints tokens into a store the routes never read
# and every owner arm 401s while the refusal arms stay green. Same reason as the
# comment at the top of dungeon_routes.py.
assert auth_module is sys.modules["auth"], (
    "bare `auth` and the module main.py bound must be the same object, or every "
    "minted session 401s"
)

RUN_TAG = uuid.uuid4().hex[:8]

# The 14 routes, as they must appear on the mounted app. Pinned as a SET of
# (method, path-template) so a renamed or dropped route reds here rather than
# silently reducing the surface a client panel can reach.
EXPECTED_ROUTES = {
    ("GET", "/dungeons/templates"),
    ("GET", "/dungeons/templates/{template_id}"),
    ("POST", "/dungeons/party/create"),
    ("GET", "/dungeons/party/{party_id}"),
    ("POST", "/dungeons/party/invite"),
    ("POST", "/dungeons/party/leave"),
    ("POST", "/dungeons/party/ready"),
    ("POST", "/dungeons/instance/create"),
    ("GET", "/dungeons/instance/{instance_id}"),
    ("POST", "/dungeons/instance/start"),
    ("POST", "/dungeons/instance/advance"),
    ("POST", "/dungeons/instance/fail"),
    ("POST", "/dungeons/instance/abandon"),
    ("POST", "/dungeons/instance/claim-loot"),
}

# Response key sets, measured. A key appearing or disappearing is a wire-format
# change a mounted panel would read as a null.
TEMPLATE_LIST_KEYS = {
    "id", "name", "description", "instance_type", "min_level", "max_level",
    "min_party_size", "max_party_size", "recommended_realm",
    "time_limit_seconds", "total_floors", "entrance_zone",
    "first_clear_rewards", "weekly_rewards",
}
TEMPLATE_DETAIL_KEYS = TEMPLATE_LIST_KEYS - {"total_floors"} | {
    "floors", "entrance_coords", "loot_table",
}
PARTY_CREATE_KEYS = {"ok", "party_id", "leader_id", "members"}
PARTY_READ_KEYS = {"ok", "party_id", "leader_id", "instance_id",
                   "created_at", "members"}
INSTANCE_CREATE_KEYS = {"ok", "instance_id", "template_id", "template_name",
                        "state", "created_at"}
INSTANCE_START_KEYS = {"ok", "state", "started_at"}
INSTANCE_ADVANCE_KEYS = {"ok", "state", "current_floor", "total_floors",
                         "completed", "loot_dropped"}
INSTANCE_TERMINATE_KEYS = {"ok", "state"}
CLAIM_LOOT_KEYS = {"ok", "rewards"}
REWARD_ITEM_KEYS = {"item_id", "count", "source"}
INSTANCE_PROGRESS_KEYS = {
    "instance_id", "template_id", "name", "state", "current_floor",
    "total_floors", "time_limit", "started_at", "completed_at",
    "loot_dropped", "rewards_claimed", "current_floor_enemies",
}
# get_instance_progress resolves each enemy through the monster stat table, so
# this is a full stat block, not a name list.
ENEMY_STAT_KEYS = {"id", "name", "hp", "max_hp", "atk", "def", "mp", "spd",
                   "realm", "element", "is_boss"}

# The request-body field names. `invitee_id` was RENAMED from `player_id`
# because the central ownership guard reads any body field called `player_id`
# as the acting-player claim, which had inverted the route: it refused the real
# leader and admitted a stranger. A rename back would silently reopen that.
BODY_FIELDS = {
    "POST /dungeons/party/create": {"leader_id"},
    "POST /dungeons/party/invite": {"party_id", "invitee_id"},
    "POST /dungeons/party/ready": {"player_id", "ready"},
    "POST /dungeons/instance/create": {"party_id", "template_id"},
    "POST /dungeons/instance/start": {"instance_id", "player_id"},
    "POST /dungeons/instance/advance": {"instance_id", "player_id"},
    "POST /dungeons/instance/fail": {"instance_id", "player_id"},
    "POST /dungeons/instance/abandon": {"instance_id", "player_id"},
    "POST /dungeons/instance/claim-loot": {"instance_id", "player_id",
                                           "reward_type"},
}

VALID_REWARD_TYPES = ("loot", "first_clear", "weekly")


# ======================================================================================
# HARNESS
# ======================================================================================
@pytest.fixture(scope="module")
def client():
    """The real mounted app, under its lifespan.

    ``raise_server_exceptions=False`` so a genuine 500 is OBSERVED as a 500
    instead of exploding out of the test as a traceback -- which is exactly what
    the unknown-realm arm below needs in order to pin it.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _session(player_id: str) -> dict:
    """Bearer headers for one named player.

    Opt-in per call site on purpose: a blanket autouse session would make this
    file structurally unable to observe the anonymous case, which is half the
    contract.
    """
    return {"Authorization": f"Bearer {auth_module.issue_session(player_id)}"}


def _new_player(client, role: str) -> str:
    made = client.post("/player/create", json={
        "name": f"DungeonContract{role}{RUN_TAG}", "spirit_root": "fire"})
    assert made.status_code == 200, made.text
    player_id = made.json()["id"]
    # An explicit dict so `inventory` is never absent: claim-loot writes through
    # it and a missing container would read as "credited nothing".
    with players_db.transaction(player_id):
        row = players_db[player_id]
        if not isinstance(row.get("inventory"), dict):
            row["inventory"] = {}
        players_db.persist(player_id, row)
    return player_id


def _inventory(client, player_id: str) -> dict:
    with players_db.transaction(player_id):
        return dict(players_db[player_id].get("inventory") or {})


def _new_party(client, leader: str, members=()):
    """A party led by ``leader``, with ``members`` recruited and EVERYONE ready."""
    created = client.post("/dungeons/party/create", json={"leader_id": leader},
                          headers=_session(leader))
    assert created.status_code == 200, created.text
    party_id = created.json()["party_id"]

    for member in members:
        joined = client.post("/dungeons/party/invite", json={
            "party_id": party_id, "invitee_id": member}, headers=_session(leader))
        assert joined.status_code == 200, (
            f"fixture: the leader could not recruit {member}, so every later "
            f"arm would prove nothing: {joined.text}")

    # EVERY member readies, or `instance/create` answers 400 for a reason that
    # reads exactly like a party-size problem. Measured, and the single easiest
    # way to misread this module.
    for pid in (leader, *members):
        readied = client.post("/dungeons/party/ready", json={
            "player_id": pid, "ready": True}, headers=_session(pid))
        assert readied.status_code == 200, readied.text
    return party_id


def _new_instance(client, leader: str, party_id: str,
                  template_id: str = "spirit_beast_den") -> str:
    made = client.post("/dungeons/instance/create", json={
        "party_id": party_id, "template_id": template_id}, headers=_session(leader))
    assert made.status_code == 200, made.text
    return made.json()["instance_id"]


def _waiting_run(client, role: str, template_id: str = "spirit_beast_den") -> dict:
    """A solo party with a WAITING (not started) instance."""
    leader = _new_player(client, role)
    party_id = _new_party(client, leader)
    return {"leader": leader, "party_id": party_id,
            "instance_id": _new_instance(client, leader, party_id, template_id)}


def _active_run(client, role: str, template_id: str = "spirit_beast_den") -> dict:
    """A solo party with a STARTED (active) instance."""
    run = _waiting_run(client, role, template_id)
    started = client.post("/dungeons/instance/start", json={
        "instance_id": run["instance_id"], "player_id": run["leader"]},
        headers=_session(run["leader"]))
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "active"
    return run


def _completed_run(client, role: str, template_id: str = "spirit_beast_den") -> dict:
    """A solo party whose run is COMPLETED, with one known item dropped.

    Completed because ``claim_loot`` returns nothing for a run that has not
    finished -- a fixture stopped at `active` would let a broken payout look
    harmless, because the refusal would be the manager declining rather than the
    route refusing. The drop is SEEDED because the loot table is chance-gated per
    item, so a completed run legitimately sometimes drops nothing, and leaving it
    to chance makes the payout arms unfalsifiable. The seed is a decision about
    WHAT WAS DROPPED; the route, the claim ledger and the credit stay real.
    """
    run = _active_run(client, role, template_id)
    leader, instance_id = run["leader"], run["instance_id"]

    for _ in range(12):
        advanced = client.post("/dungeons/instance/advance", json={
            "instance_id": instance_id, "player_id": leader},
            headers=_session(leader))
        assert advanced.status_code == 200, advanced.text
        if advanced.json()["completed"]:
            break
    else:
        pytest.fail("the run never completed; the fixture is not exercising claim-loot")

    dm = get_dungeon_manager()
    dm.instances[instance_id].loot_dropped = [
        {"item_id": "contract_dungeon_token", "count": 2, "source": "dungeon_loot"},
    ]
    dm.instances[instance_id].rewards_claimed = set()
    return run


# ======================================================================================
# 1. THE SURFACE ITSELF
#
# No client panel can be written until the route list is known, and the list route
# is the only way a client discovers what exists. Asserted as a set so a rename
# reds instead of quietly shrinking the reachable surface.
# ======================================================================================
def test_exactly_fourteen_dungeon_routes_are_mounted(client):
    live = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/dungeons"):
            continue
        for method in (getattr(route, "methods", None) or ()):
            live.add((method, path))

    assert live == EXPECTED_ROUTES, (
        "the /dungeons surface changed:\n"
        f"  missing: {sorted(EXPECTED_ROUTES - live)}\n"
        f"  extra:   {sorted(live - EXPECTED_ROUTES)}")


def test_every_dungeon_route_is_in_the_openapi_document(client):
    """The panel author reads the schema, not the source.

    Measured: all 14 paths appear, and each POST's body model is a NAMED
    component schema (a pydantic model), so a generated client would bind to it.
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200, (
        "the schema document did not render, so no client can be generated "
        f"against these routes: {response.status_code}")
    spec = response.json()
    paths = {p for p in spec["paths"] if p.startswith("/dungeons")}

    live = {path for _, path in EXPECTED_ROUTES}
    assert paths == live, (
        f"openapi and the live route table disagree: "
        f"only-in-spec {sorted(paths - live)}, only-live {sorted(live - paths)}")

    # A query-param-only GET must publish its params, or a generated client
    # cannot supply them. Measured: templates publishes player_id, realm, level.
    names = [p.get("name") for p in
             spec["paths"]["/dungeons/templates"]["get"]["parameters"]]
    assert names == ["player_id", "realm", "level"], names


def test_every_dungeon_route_refuses_an_anonymous_caller(client):
    """401 with no session, on all 14.

    Swept over the real route table rather than a hand-written list, so a route
    added later cannot ship unauthenticated without this going red. The bodies
    are well-formed enough to call: each is a refusal, so the ids do not need to
    exist, but the call must still reach a sign-in check and not a 404.
    """
    anonymous = [
        ("GET", "/dungeons/templates", {"player_id": "p", "realm": "qi_refining",
                                        "level": 1}, None),
        ("GET", "/dungeons/templates/spirit_beast_den", {"player_id": "p"}, None),
        ("POST", "/dungeons/party/create", None, {"leader_id": "p"}),
        ("GET", "/dungeons/party/party_nope", {"player_id": "p"}, None),
        ("POST", "/dungeons/party/invite", None,
         {"party_id": "party_nope", "invitee_id": "p"}),
        ("POST", "/dungeons/party/leave", None, {"player_id": "p"}),
        ("POST", "/dungeons/party/ready", None, {"player_id": "p", "ready": True}),
        ("POST", "/dungeons/instance/create", None,
         {"party_id": "party_nope", "template_id": "spirit_beast_den"}),
        ("GET", "/dungeons/instance/instance_nope", {"player_id": "p"}, None),
        ("POST", "/dungeons/instance/start", None,
         {"instance_id": "instance_nope", "player_id": "p"}),
        ("POST", "/dungeons/instance/advance", None,
         {"instance_id": "instance_nope", "player_id": "p"}),
        ("POST", "/dungeons/instance/fail", None,
         {"instance_id": "instance_nope", "player_id": "p"}),
        ("POST", "/dungeons/instance/abandon", None,
         {"instance_id": "instance_nope", "player_id": "p"}),
        ("POST", "/dungeons/instance/claim-loot", None,
         {"instance_id": "instance_nope", "player_id": "p", "reward_type": "loot"}),
    ]
    assert len(anonymous) == len(EXPECTED_ROUTES), (
        "the anonymous sweep no longer covers every dungeon route; update both")

    for method, path, params, body in anonymous:
        response = client.request(method, path, params=params, json=body)
        assert response.status_code == 401, (
            f"an anonymous caller reached {method} {path}: "
            f"{response.status_code} {response.text[:160]}")


# ======================================================================================
# 2. REQUEST SHAPES
#
# Where each id travels -- query string vs JSON body -- is the thing a client gets
# wrong first, because FastAPI answers a misplaced field with 422 and the route
# then looks missing.
# ======================================================================================
@pytest.mark.parametrize("route", sorted(BODY_FIELDS))
def test_every_post_body_declares_its_fields_as_required(client, route):
    """Required, by name, from the published schema.

    A field that silently acquired a default would let a caller omit it and get
    a 200 that does nothing -- the ``AUTO_BATTLE`` class of bug.
    """
    verb, path = route.split(" ", 1)
    spec = client.get("/openapi.json").json()
    ref = spec["paths"][path][verb.lower()]["requestBody"]["content"][
        "application/json"]["schema"]["$ref"]
    model = ref.rsplit("/", 1)[-1]
    required = set(spec["components"]["schemas"][model]["required"])
    assert required == BODY_FIELDS[route], (
        f"{route} required fields moved: {sorted(required)}")


@pytest.mark.parametrize("route", [
    "POST /dungeons/party/create",
    "POST /dungeons/party/ready",
    "POST /dungeons/instance/create",
    "POST /dungeons/instance/claim-loot",
])
def test_a_post_body_missing_a_required_field_is_422(client, route):
    verb, path = route.split(" ", 1)
    owner = _new_player(client, f"Body{path.rsplit('/', 1)[-1]}")
    response = client.request(verb, path, json={}, headers=_session(owner))
    assert response.status_code == 422, (
        f"an empty body was accepted by {route}: "
        f"{response.status_code} {response.text[:160]}")


def test_party_leave_takes_an_embedded_object_not_a_bare_string(client):
    """``Body(..., embed=True)`` means ``{"player_id": ...}``.

    Measured: a bare JSON string is 422. This is pinned because the two shapes
    look identical in a hand-written call and differ by one character.
    """
    run = _waiting_run(client, "LeaveEmbed")
    leader = run["leader"]

    as_string = client.post("/dungeons/party/leave", json=leader,
                            headers=_session(leader))
    assert as_string.status_code == 422, (
        f"a bare JSON string was accepted as the leave body: {as_string.text[:160]}")

    as_object = client.post("/dungeons/party/leave", json={"player_id": leader},
                            headers=_session(leader))
    assert as_object.status_code == 200, as_object.text


def test_party_invite_rejects_the_old_player_id_field_name(client):
    """``invitee_id``, not ``player_id``.

    Measured on the live app: posting the old field name answers 403
    "player_id does not match the authenticated session", because the central
    ownership guard reads any body field called ``player_id`` as the acting-player
    claim -- and here the field named a THIRD PARTY. That is not an accident of
    this test; it is the reason for the rename, so the rename is pinned.
    """
    leader = _new_player(client, "InviteFieldName")
    recruit = _new_player(client, "InviteRecruit")
    party_id = _new_party(client, leader)

    legacy = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "player_id": recruit}, headers=_session(leader))
    assert legacy.status_code == 403, (
        "the legacy player_id field was accepted on party/invite, which means "
        f"the central guard is back reading the invitee as the actor: {legacy.text[:160]}")
    assert get_dungeon_manager().player_party.get(recruit) is None, (
        "the refused invite still recruited the invitee")


def test_the_two_id_carriers_are_not_interchangeable(client):
    """A body field sent as a query param is refused, and vice versa.

    Asserting the 422 STATUS alone would be weak, because a missing field
    produces the same code. What distinguishes the carrier is WHERE the 422 says
    the field is missing, and that is measured here:

      * ``POST /dungeons/party/ready`` with ``player_id`` and ``ready`` in the
        QUERY and no body at all answers 422 with ``loc: ["body"]`` -- i.e. "no
        body was sent". Put ``ready`` in the query and ``player_id`` in the body
        and it still answers 422, now naming ``loc: ["body", "ready"]``. So the
        route reads the body and only the body; the query copy is inert.
      * ``GET /dungeons/templates`` with all three values in the BODY answers 422
        naming all three at ``loc: ["query", ...]``, and the same values in the
        query answer 200.

    A panel author moving a field between the two gets a 422 that says which
    carrier it expected, and this test pins that the message really says it.
    """
    owner = _new_player(client, "CarrierSwap")

    body_as_query = client.post("/dungeons/party/ready",
                                params={"player_id": owner, "ready": True},
                                headers=_session(owner))
    assert body_as_query.status_code == 422, (
        "party/ready read its body fields from the query string: "
        f"{body_as_query.status_code} {body_as_query.text[:200]}")
    missing = [d.get("loc") for d in body_as_query.json()["detail"]
               if isinstance(d, dict)]
    assert ["body"] in missing, (
        f"the 422 no longer says 'no body was sent': {missing}")

    # Split the pair: `ready` in the query is still not a body field.
    split = client.post("/dungeons/party/ready", params={"ready": True},
                        json={"player_id": owner}, headers=_session(owner))
    assert split.status_code == 422, split.text
    split_missing = [d.get("loc") for d in split.json()["detail"]
                     if isinstance(d, dict)]
    assert ["body", "ready"] in split_missing, (
        "party/ready started accepting `ready` from the query string: "
        f"{split_missing}")

    query_as_body = client.request("GET", "/dungeons/templates",
                                   json={"player_id": owner,
                                         "realm": "qi_refining", "level": 1},
                                   headers=_session(owner))
    assert query_as_body.status_code == 422, (
        "templates read its query fields from the body: "
        f"{query_as_body.status_code} {query_as_body.text[:200]}")
    query_locs = {tuple(d.get("loc", ())) for d in query_as_body.json()["detail"]
                  if isinstance(d, dict)}
    for field in ("player_id", "realm", "level"):
        assert ("query", field) in query_locs, (
            f"the 422 for a body-only templates call no longer names "
            f"query.{field}: {sorted(query_locs)}")

    # ...and the working shape, so the arm above is not passing because the
    # route is broken in both directions.
    working = client.get("/dungeons/templates", params={
        "player_id": owner, "realm": "qi_refining", "level": 1},
        headers=_session(owner))
    assert working.status_code == 200, working.text


# ======================================================================================
# 3. THE TEMPLATE CATALOGUE
#
# The only discovery surface a client has, so its shape and its filter behaviour
# are pinned exactly -- including that an empty list is a valid answer.
# ======================================================================================
def test_template_list_row_carries_exactly_these_keys(client):
    owner = _new_player(client, "TplListShape")
    response = client.get("/dungeons/templates", params={
        "player_id": owner, "realm": "golden_core", "level": 50},
        headers=_session(owner))
    assert response.status_code == 200, response.text

    body = response.json()
    assert isinstance(body, list) and body, (
        "the catalogue came back empty for a realm/level pair that admits "
        "templates, so the key assertion below would pass vacuously")
    for row in body:
        assert set(row) == TEMPLATE_LIST_KEYS, (
            f"template list row keys changed: extra {sorted(set(row) - TEMPLATE_LIST_KEYS)}, "
            f"missing {sorted(TEMPLATE_LIST_KEYS - set(row))}")


def test_template_detail_carries_the_list_keys_plus_the_heavy_three(client):
    """The detail route is a strict SUPERSET of the list row, minus one field.

    ``total_floors`` (a count) becomes ``floors`` (the definitions), and the
    detail adds ``entrance_coords`` and ``loot_table``. A client panel that reads
    ``loot_table`` off the LIST row will find it absent -- measured, and worth
    pinning because the two routes look interchangeable.
    """
    owner = _new_player(client, "TplDetailShape")
    listed = client.get("/dungeons/templates", params={
        "player_id": owner, "realm": "golden_core", "level": 50},
        headers=_session(owner)).json()
    detail = client.get("/dungeons/templates/spirit_beast_den",
                        params={"player_id": owner},
                        headers=_session(owner))
    assert detail.status_code == 200, detail.text
    body = detail.json()

    assert set(body) == TEMPLATE_DETAIL_KEYS, (
        f"template detail keys changed: extra {sorted(set(body) - TEMPLATE_DETAIL_KEYS)}, "
        f"missing {sorted(TEMPLATE_DETAIL_KEYS - set(body))}")
    assert set(listed[0]) - set(body) == {"total_floors"}, (
        "the list row and the detail row drifted apart in a new way")

    assert isinstance(body["floors"], list) and body["floors"], (
        "the detail route published no floor definitions")
    assert set(body["floors"][0]) == {"floor", "name", "enemies", "boss"}, (
        f"floor definition keys changed: {sorted(body['floors'][0])}")
    assert isinstance(body["loot_table"], list) and body["loot_table"], (
        "the detail route published no loot table")
    assert set(body["loot_table"][0]) == {"item_id", "chance",
                                          "min_count", "max_count"}
    for key in ("first_clear_rewards", "weekly_rewards"):
        assert isinstance(body[key], list) and body[key], f"{key} came back empty"
        assert set(body[key][0]) == {"item_id", "chance", "count"}
    assert set(body["entrance_coords"]) == {"x", "y"}


def test_a_raising_realm_or_level_never_removes_a_template(client):
    """THE RELATION, not the snapshot.

    Measured catalogue at four points: ``qi_refining``@1/10/50 all admit
    ``spirit_beast_den``; ``golden_core``@50 admits all four; ``mortal``@99 admits
    none. Rather than freezing those counts, this pins the property a client
    actually relies on: raising a player's realm or level only ever ADDS
    templates. A gate that ranks one of its two inputs as -1 -- which is how this
    one once handed every new player the top-tier dungeon -- breaks the nesting
    and goes red here, whatever the catalogue happens to contain today.
    """
    owner = _new_player(client, "TplMonotonic")

    def visible(realm, level):
        response = client.get("/dungeons/templates", params={
            "player_id": owner, "realm": realm, "level": level},
            headers=_session(owner))
        assert response.status_code == 200, (
            f"realm={realm} level={level} did not answer 200: {response.text[:160]}")
        return {row["id"] for row in response.json()}

    for realm in ("qi_refining", "golden_core"):
        ladder = [visible(realm, level) for level in (1, 10, 50)]
        for lower_level, higher in zip(ladder, ladder[1:]):
            assert lower_level <= higher, (
                f"raising the level REMOVED templates for realm {realm}: "
                f"{sorted(lower_level - higher)}")

    mortal = visible("mortal", 1)
    qi_refining = visible("qi_refining", 1)
    golden_core = visible("golden_core", 1)
    assert mortal <= qi_refining <= golden_core, (
        "raising the realm removed templates: the gate is not monotone in realm")

    # A new player is `mortal` at level 1, so this is the pair a fresh account
    # actually sends. An empty catalogue here is CORRECT, and pinning it stops a
    # future fix from "helpfully" handing a level-1 account the top dungeon.
    assert mortal == set(), f"a mortal level-1 player was offered {sorted(mortal)}"
    assert qi_refining, "qi_refining offered nothing at level 1"


def test_an_unrankable_realm_is_a_loud_failure_not_a_silent_empty_list(client):
    """FIXED. Was pinned as a 500; see the module docstring.

    ``get_available_templates`` raises ``UnknownRealmError`` for a realm it cannot
    rank. It used to escape the route unhandled, so the request answered HTTP 500
    with the plain-text body "Internal Server Error" -- no realm named, and not
    even JSON, so a client reading `detail` off the failure got nothing at all.
    That is now a 400 in this router's own {"detail": ...} shape.

    The refusal is still LOUD, which is the property worth keeping: a quiet empty
    list is indistinguishable from "correctly gated out", and that ambiguity is
    why this hid for so long. So the test still pins the realm by name, and still
    refuses to let the answer become a bare 200-with-[].
    """
    owner = _new_player(client, "TplBadRealm")
    response = client.get("/dungeons/templates", params={
        "player_id": owner, "realm": "definitely_not_a_realm", "level": 1},
        headers=_session(owner))

    assert response.status_code == 400, (
        f"the unrankable realm must be a 400 naming the realm, got "
        f"{response.status_code} {response.text[:200]}")
    # The body must be this router's standard {"detail": ...} JSON, so a panel
    # reads the realm off it the same way it reads every other refusal here.
    assert set(response.json()) == {"detail"}, (
        f"the 400 body is not the {{'detail': ...}} shape: {response.text[:200]!r}")
    detail = response.json()["detail"]
    assert "definitely_not_a_realm" in detail, (
        f"the 400 does not name the realm that was rejected: {detail!r}")
    # And it must stay loud rather than degrading into a silent empty list, which
    # is the failure mode this test was written to prevent.
    assert "not a known realm" in detail, (
        f"the 400 no longer explains WHY the realm was refused: {detail!r}")


def test_an_unknown_template_id_is_404_and_a_foreign_name_is_403(client):
    """The two refusals, distinguished -- and both measured.

    MEASURED, and worth knowing before writing a panel: the cross-player arm is
    answered by the CENTRAL ownership guard, not by this route's own check, so
    the message is "player_id does not match the authenticated session" and the
    route's own "Not your player" string never fires for a query-carried id. The
    route-level message is reachable only where the id is NOT a guard claim
    field -- ``POST /party/create``, whose body field is ``leader_id``, and which
    is pinned separately in test_party_create_... below.

    A caller naming THEMSELVES after a template that does not exist gets 404
    "Dungeon template not found". A panel must be able to tell "you may not ask"
    from "it is not there".
    """
    owner = _new_player(client, "TplRefusal")
    stranger = _new_player(client, "TplStranger")

    foreign = client.get("/dungeons/templates/spirit_beast_den",
                         params={"player_id": owner}, headers=_session(stranger))
    assert foreign.status_code == 403, foreign.text
    assert foreign.json()["detail"] == (
        "player_id does not match the authenticated session")

    unknown = client.get("/dungeons/templates/no_such_template",
                         params={"player_id": owner}, headers=_session(owner))
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"] == "Dungeon template not found"

    ghost = "ghost_player_no_such_row"
    missing = client.get("/dungeons/templates", params={
        "player_id": ghost, "realm": "qi_refining", "level": 1},
        headers=_session(ghost))
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Player not found"


def test_party_create_is_guarded_by_the_route_not_the_central_guard(client):
    """The one dungeon route whose own "Not your player" check IS load-bearing.

    Measured: the body field is ``leader_id``, which is not in the central
    guard's claim-name set, so nobody upstream stops a caller naming a victim --
    the explicit check at the top of ``create_party`` is the whole gate. Delete
    it and this answers 200 and creates a party with a stranger as leader.
    """
    owner = _new_player(client, "CreateGuardOwner")
    victim = _new_player(client, "CreateGuardVictim")

    response = client.post("/dungeons/party/create", json={"leader_id": victim},
                           headers=_session(owner))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Not your player"
    assert get_dungeon_manager().player_party.get(victim) is None, (
        "the refused create still put the victim in a party")
    assert get_dungeon_manager().player_party.get(owner) is None, (
        "the refused create still created a party for the caller")


def test_a_non_numeric_level_is_422(client):
    """``level`` is an int. A client sending a string gets 422, not a 500."""
    owner = _new_player(client, "TplLevelType")
    response = client.get("/dungeons/templates", params={
        "player_id": owner, "realm": "qi_refining", "level": "fifty"},
        headers=_session(owner))
    assert response.status_code == 422, response.text


# ======================================================================================
# 4. PARTIES
# ======================================================================================
def test_party_create_reads_back_the_leader_and_the_roster(client):
    owner = _new_player(client, "PartyCreate")
    response = client.post("/dungeons/party/create", json={"leader_id": owner},
                           headers=_session(owner))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == PARTY_CREATE_KEYS, (
        f"party/create keys changed: extra {sorted(set(body) - PARTY_CREATE_KEYS)}, "
        f"missing {sorted(PARTY_CREATE_KEYS - set(body))}")
    assert body["ok"] is True
    assert body["leader_id"] == owner
    # The CREATE reply's member rows carry only two fields. The READ reply
    # carries four. A panel cannot draw a ready-up column from this reply, which
    # is why a client must re-read the party afterwards.
    assert [m["player_id"] for m in body["members"]] == [owner]
    assert set(body["members"][0]) == {"player_id", "role"}
    assert body["members"][0]["role"] == "leader"


def test_a_second_party_for_the_same_leader_is_400(client):
    owner = _new_player(client, "PartyTwice")
    assert _new_party(client, owner)
    again = client.post("/dungeons/party/create", json={"leader_id": owner},
                        headers=_session(owner))
    assert again.status_code == 400, again.text
    assert again.json()["detail"] == "Already in a party"


def test_party_read_carries_the_four_fields_the_create_reply_lacks(client):
    leader = _new_player(client, "PartyReadLead")
    member = _new_player(client, "PartyReadMem")
    party_id = _new_party(client, leader, members=(member,))

    response = client.get(f"/dungeons/party/{party_id}", params={"player_id": leader},
                          headers=_session(leader))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == PARTY_READ_KEYS, (
        f"party read keys changed: extra {sorted(set(body) - PARTY_READ_KEYS)}, "
        f"missing {sorted(PARTY_READ_KEYS - set(body))}")
    assert body["party_id"] == party_id
    assert body["leader_id"] == leader
    # No instance yet, so this is null -- the field exists before it is populated.
    assert body["instance_id"] is None
    assert isinstance(body["created_at"], (int, float))

    assert set(body["members"][0]) == {"player_id", "role", "joined_at", "ready"}
    roles = {m["player_id"]: m["role"] for m in body["members"]}
    assert roles == {leader: "leader", member: "member"}
    # `_new_party` readies everyone, so a ready-up column can be drawn here.
    assert all(m["ready"] is True for m in body["members"])


def test_party_read_refuses_a_stranger_with_403_and_leaks_nothing(client):
    """403, not 404 -- a 404 would let a stranger tell "party exists" from
    "no such party". And the refusal body carries no roster."""
    leader = _new_player(client, "RosterLead")
    stranger = _new_player(client, "RosterStranger")
    party_id = _new_party(client, leader)

    response = client.get(f"/dungeons/party/{party_id}",
                          params={"player_id": stranger}, headers=_session(stranger))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Not a member of this party"
    assert leader not in response.text, "the refusal body leaked the roster"

    # An id that does not exist gives the SAME answer, which is the point.
    unknown = client.get("/dungeons/party/party_does_not_exist",
                         params={"player_id": stranger}, headers=_session(stranger))
    assert unknown.status_code == 403, unknown.text
    assert unknown.json()["detail"] == unknown.json()["detail"] == (
        "Not a member of this party")


def test_invite_returns_only_ok_so_the_client_must_re_read(client):
    """``{"ok": true}`` and nothing else. Measured.

    This is the shape a client panel gets wrong: after recruiting somebody it
    wants to draw the new roster, and this reply cannot produce one.
    """
    leader = _new_player(client, "InviteShapeLead")
    recruit = _new_player(client, "InviteShapeRecruit")
    party_id = _new_party(client, leader)

    response = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": recruit}, headers=_session(leader))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}, (
        "party/invite grew a key; a client that re-reads after each invite "
        f"should say so here: {response.json()}")

    # Which is why the party must then be re-read to see the member at all.
    reread = client.get(f"/dungeons/party/{party_id}", params={"player_id": leader},
                        headers=_session(leader))
    assert recruit in {m["player_id"] for m in reread.json()["members"]}


def test_a_member_cannot_invite_themselves(client):
    """Gate 3 on the invite route, measured.

    Without it, a signed-in player is a second free route into any party, and
    their own session satisfies both other gates -- so every downstream route
    gated on bare membership falls to one request.
    """
    leader = _new_player(client, "SelfInviteLead")
    party_id = _new_party(client, leader)

    response = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": leader}, headers=_session(leader))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Not a member of this party"


def test_ready_echoes_the_flag_it_set(client):
    """``{"ok", "ready"}``. No party id, no roster -- same re-read requirement."""
    leader = _new_player(client, "ReadyShape")
    party_id = _new_party(client, leader)

    response = client.post("/dungeons/party/ready", json={
        "player_id": leader, "ready": False}, headers=_session(leader))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "ready": False}

    reread = client.get(f"/dungeons/party/{party_id}", params={"player_id": leader},
                        headers=_session(leader))
    assert all(m["ready"] is False for m in reread.json()["members"])


def test_ready_by_a_player_in_no_party_is_403_not_the_managers_400(client):
    """A self-named stranger, which is the only shape the actor-equals-claim
    check cannot refuse.

    Naming a VICTIM in the body trips the claim check first and returns 403 for
    an unrelated reason, so it cannot prove the membership check exists. This arm
    names the CALLER, which is what isolates it -- and the status is asserted
    precisely: delete the membership check and the request falls through to the
    manager's own lookup, which answers 400 instead.
    """
    stranger = _new_player(client, "ReadyStranger")
    assert get_dungeon_manager().player_party.get(stranger) is None, (
        "fixture: the stranger is already in a party, so this arm would measure "
        "the wrong thing")

    response = client.post("/dungeons/party/ready", json={
        "player_id": stranger, "ready": True}, headers=_session(stranger))
    assert response.status_code == 403, (
        "a player in no party was not refused by the membership check: "
        f"{response.status_code} {response.text[:200]}")


def test_ready_naming_a_victim_is_refused_by_the_claim_check(client):
    """The cross-player arm, and it never reaches the flag write."""
    leader = _new_player(client, "ReadyVictimLead")
    victim = _new_player(client, "ReadyVictim")
    party_id = _new_party(client, leader, members=(victim,))

    response = client.post("/dungeons/party/ready", json={
        "player_id": victim, "ready": False}, headers=_session(leader))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "player_id does not match the authenticated session"

    reread = client.get(f"/dungeons/party/{party_id}", params={"player_id": leader},
                        headers=_session(leader))
    assert all(m["ready"] is True for m in reread.json()["members"]), (
        "the refused request still moved the victim's ready flag")


def test_leaving_a_solo_party_disbands_it(client):
    """Measured: ``{"ok": true}``, and the party index entry is GONE afterwards.

    This is the ordering hazard named in the module docstring, so it is pinned
    rather than left as tribal knowledge.
    """
    run = _waiting_run(client, "LeaveDisband")
    leader = run["leader"]

    response = client.post("/dungeons/party/leave", json={"player_id": leader},
                           headers=_session(leader))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert get_dungeon_manager().player_party.get(leader) is None, (
        "leaving a solo party did not disband it; later owner arms will 403")

    twice = client.post("/dungeons/party/leave", json={"player_id": leader},
                        headers=_session(leader))
    assert twice.status_code == 400, twice.text
    assert twice.json()["detail"] == "Not in a party"


def test_leaving_a_party_with_members_keeps_the_party(client):
    """The contrast with the arm above: a leader with members does not disband.

    A panel that hard-codes "leave = disband" would be wrong for every party with
    more than one person in it.
    """
    leader = _new_player(client, "LeaveKeepLead")
    member = _new_player(client, "LeaveKeepMem")
    party_id = _new_party(client, leader, members=(member,))

    left = client.post("/dungeons/party/leave", json={"player_id": member},
                       headers=_session(member))
    assert left.status_code == 200, left.text
    assert get_dungeon_manager().player_party.get(member) is None
    assert get_dungeon_manager().player_party.get(leader) == party_id, (
        "a member leaving disbanded the whole party")


# ======================================================================================
# 5. INSTANCES
# ======================================================================================
def test_instance_create_publishes_six_keys_and_starts_in_waiting(client):
    leader = _new_player(client, "InstCreate")
    party_id = _new_party(client, leader)

    response = client.post("/dungeons/instance/create", json={
        "party_id": party_id, "template_id": "spirit_beast_den"},
        headers=_session(leader))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == INSTANCE_CREATE_KEYS, (
        f"instance/create keys changed: extra {sorted(set(body) - INSTANCE_CREATE_KEYS)}, "
        f"missing {sorted(INSTANCE_CREATE_KEYS - set(body))}")
    assert body["state"] == "waiting"
    assert body["template_id"] == "spirit_beast_den"
    # The name is resolved here and NOWHERE else -- the progress read has `name`
    # for the template but this reply is the only place `template_name` appears.
    assert body["template_name"] == "Spirit Beast Den"
    assert get_dungeon_manager().instances[body["instance_id"]].state.value == "waiting"


def test_instance_create_refusals_are_403_for_an_unknown_party_and_400_for_a_bad_fit(
        client):
    """The one 400 covers three different causes -- pinned as the shape a client
    must handle, with the re-read that disambiguates it.

    An unknown party is 403, not 404. That changed on 2026-10-04: the route used
    to look the party up before checking membership, so 404/403 was a party-id
    oracle. `_require_party_member` now answers 403 for both, the same as every
    other party route in this file.
    """
    outsider = _new_player(client, "InstCreateOutsider")
    unknown_party = client.post("/dungeons/instance/create", json={
        "party_id": "party_does_not_exist", "template_id": "spirit_beast_den"},
        headers=_session(outsider))
    assert unknown_party.status_code == 403, unknown_party.text
    assert unknown_party.json()["detail"] == "Not a member of this party"

    # A party of one cannot fill `ancient_ruins` (min_party_size 3), so the same
    # 400 a readiness failure produces also covers a size failure.
    leader = _new_player(client, "InstCreateSize")
    party_id = _new_party(client, leader)
    too_small = client.post("/dungeons/instance/create", json={
        "party_id": party_id, "template_id": "ancient_ruins"}, headers=_session(leader))
    assert too_small.status_code == 400, too_small.text
    assert "party size" in too_small.json()["detail"], (
        "the instance/create 400 no longer names its causes: "
        f"{too_small.json()['detail']}")


def test_instance_progress_publishes_twelve_keys_and_the_enemy_stat_block(client):
    """The largest reply on the router, and the one that used to leak.

    Read past floor 0 first: at floor 0 there are fewer monsters, so a test that
    only reads a fresh run is a weaker check than it looks.
    """
    run = _active_run(client, "InstProgress")
    leader, instance_id = run["leader"], run["instance_id"]

    advanced = client.post("/dungeons/instance/advance", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert advanced.status_code == 200, advanced.text

    response = client.get(f"/dungeons/instance/{instance_id}",
                          params={"player_id": leader}, headers=_session(leader))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == INSTANCE_PROGRESS_KEYS, (
        f"instance progress keys changed: extra {sorted(set(body) - INSTANCE_PROGRESS_KEYS)}, "
        f"missing {sorted(INSTANCE_PROGRESS_KEYS - set(body))}")
    assert body["instance_id"] == instance_id
    assert body["state"] == "active"
    assert body["current_floor"] == 1
    # `total_floors` is a count here, not the floor definitions the detail route
    # publishes. Two routes, two meanings for a similarly named pair of fields.
    # And it counts the TEMPLATE's floors, not the enemies on this floor -- an
    # earlier draft of this line compared the two and was wrapped in `or True`,
    # which can never fail and so measured nothing. The real relation is pinned
    # now, and it is the same number `advance` publishes for `total_floors`.
    template_floors = len(get_dungeon_manager().templates[
        body["template_id"]].floors)
    assert body["total_floors"] == template_floors, (
        f"the progress read counts {body['total_floors']} floors but the "
        f"template defines {template_floors}")
    assert body["current_floor"] < body["total_floors"], (
        "a run mid-flight reported a current floor at or past the last one")
    assert isinstance(body["rewards_claimed"], list)
    assert isinstance(body["loot_dropped"], list)

    enemies = body["current_floor_enemies"]
    assert enemies, "a live floor resolved no enemies at all"
    assert set(enemies[0]) == ENEMY_STAT_KEYS, (
        f"enemy stat block keys changed: {sorted(enemies[0])}")
    for key in ("hp", "max_hp", "atk", "def", "mp", "spd"):
        assert isinstance(enemies[0][key], (int, float)), (
            f"enemy stat {key} is not numeric: {enemies[0][key]!r}")


def test_instance_progress_is_refused_to_a_stranger_in_three_ways(client):
    """Identity, then claim, then participation -- each with its own answer.

    MEASURED: naming a victim is refused by the CENTRAL guard with "player_id
    does not match the authenticated session" (not this route's own "Not your
    player" string, which the guard pre-empts for a query-carried id). Naming
    yourself while in no party, about someone else's run, reaches the route and
    gets "Not a participant in this instance" -- and that refusal body carries no
    monster stat block, which a 403 that still leaked the stats would.
    """
    run = _active_run(client, "InstProgressRefusal")
    leader, instance_id = run["leader"], run["instance_id"]

    cross = client.get(f"/dungeons/instance/{instance_id}",
                       params={"player_id": leader}, headers=_session(
                           _new_player(client, "InstProgressStranger")))
    assert cross.status_code == 403, cross.text
    assert cross.json()["detail"] == (
        "player_id does not match the authenticated session")
    assert "current_floor_enemies" not in cross.text, (
        "the refusal body carried the enemy stat block")

    stranger = _new_player(client, "InstProgressSolo")
    assert get_dungeon_manager().player_party.get(stranger) is None, "fixture"
    self_named = client.get(f"/dungeons/instance/{instance_id}",
                            params={"player_id": stranger}, headers=_session(stranger))
    assert self_named.status_code == 403, self_named.text
    assert self_named.json()["detail"] == "Not a participant in this instance"
    assert "current_floor_enemies" not in self_named.text, (
        "the participation refusal carried the enemy stat block")

    # A guessed run id gives the SAME answer as a real one, so ids stay
    # unguessable rather than becoming an existence oracle.
    unknown = client.get("/dungeons/instance/instance_does_not_exist",
                         params={"player_id": stranger}, headers=_session(stranger))
    assert unknown.status_code == 403, unknown.text
    assert unknown.json()["detail"] == "Not a participant in this instance"


def test_start_publishes_three_keys_and_moves_waiting_to_active(client):
    run = _waiting_run(client, "InstStart")
    leader, instance_id = run["leader"], run["instance_id"]

    response = client.post("/dungeons/instance/start", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == INSTANCE_START_KEYS, (
        f"instance/start keys changed: extra {sorted(set(body) - INSTANCE_START_KEYS)}, "
        f"missing {sorted(INSTANCE_START_KEYS - set(body))}")
    assert body["state"] == "active"
    assert isinstance(body["started_at"], (int, float))
    assert get_dungeon_manager().instances[instance_id].state.value == "active"


def test_start_is_leader_only_and_advancing_is_not(client):
    """The two rules are separate, and a panel has to show both.

    A non-leader member passes participation and is still refused by the leader
    rule; a non-leader member may advance their own run freely. Measured.
    """
    leader = _new_player(client, "LeaderRuleLead")
    member = _new_player(client, "LeaderRuleMem")
    party_id = _new_party(client, leader, members=(member,))
    instance_id = _new_instance(client, leader, party_id)

    refused = client.post("/dungeons/instance/start", json={
        "instance_id": instance_id, "player_id": member}, headers=_session(member))
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "Only party leader can start instance"
    assert get_dungeon_manager().instances[instance_id].state.value == "waiting", (
        "the refused start still moved the shared run state")

    started = client.post("/dungeons/instance/start", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert started.status_code == 200, started.text

    advanced_by_member = client.post("/dungeons/instance/advance", json={
        "instance_id": instance_id, "player_id": member}, headers=_session(member))
    assert advanced_by_member.status_code == 200, advanced_by_member.text


def test_advance_publishes_six_keys_and_counts_the_run_down(client):
    run = _active_run(client, "InstAdvance")
    leader, instance_id = run["leader"], run["instance_id"]

    response = client.post("/dungeons/instance/advance", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == INSTANCE_ADVANCE_KEYS, (
        f"instance/advance keys changed: extra {sorted(set(body) - INSTANCE_ADVANCE_KEYS)}, "
        f"missing {sorted(INSTANCE_ADVANCE_KEYS - set(body))}")
    assert body["state"] == "active"
    assert body["current_floor"] == 1
    # total_floors comes from the TEMPLATE, not the instance, and the same
    # template served every floor -- so it is the same number on every advance.
    assert body["total_floors"] >= 2, (
        "a template with fewer than two floors cannot exercise a counted run")
    assert body["completed"] is False
    assert isinstance(body["loot_dropped"], list)

    template_floors = len(get_dungeon_manager().templates["spirit_beast_den"].floors)
    assert body["total_floors"] == template_floors, (
        "advance's total_floors disagrees with the template's floor count")


def test_the_state_machine_refuses_every_illegal_move(client):
    """The transitions, measured. A panel draws a run bar from these.

    ``waiting -> active`` by the leader; ``advance`` needs active; the last
    advance completes the run; nothing returns to ``waiting``, so every arm here
    builds its own run rather than sharing one.
    """
    # advance before start
    waiting = _waiting_run(client, "StateAdvanceEarly")
    early = client.post("/dungeons/instance/advance", json={
        "instance_id": waiting["instance_id"], "player_id": waiting["leader"]},
        headers=_session(waiting["leader"]))
    assert early.status_code == 400, early.text
    assert early.json()["detail"] == "Cannot advance floor"

    # start twice
    twice = _active_run(client, "StateStartTwice")
    again = client.post("/dungeons/instance/start", json={
        "instance_id": twice["instance_id"], "player_id": twice["leader"]},
        headers=_session(twice["leader"]))
    assert again.status_code == 400, again.text
    assert again.json()["detail"] == "Cannot start instance"

    # fail needs active, NOT waiting -- a panel must not offer it pre-start
    fail_waiting = _waiting_run(client, "StateFailWaiting")
    failed = client.post("/dungeons/instance/fail", json={
        "instance_id": fail_waiting["instance_id"],
        "player_id": fail_waiting["leader"]}, headers=_session(fail_waiting["leader"]))
    assert failed.status_code == 400, failed.text
    assert failed.json()["detail"] == "Cannot fail instance"

    # ...but it works on an active run, and ends it permanently
    fail_active = _active_run(client, "StateFailActive")
    ok = client.post("/dungeons/instance/fail", json={
        "instance_id": fail_active["instance_id"], "player_id": fail_active["leader"]},
        headers=_session(fail_active["leader"]))
    assert ok.status_code == 200, ok.text
    assert set(ok.json()) == INSTANCE_TERMINATE_KEYS
    assert ok.json()["state"] == "failed"

    # abandon accepts WAITING, unlike fail
    abandon_waiting = _waiting_run(client, "StateAbandonWaiting")
    abandoned = client.post("/dungeons/instance/abandon", json={
        "instance_id": abandon_waiting["instance_id"],
        "player_id": abandon_waiting["leader"]},
        headers=_session(abandon_waiting["leader"]))
    assert abandoned.status_code == 200, abandoned.text
    assert set(abandoned.json()) == INSTANCE_TERMINATE_KEYS
    assert abandoned.json()["state"] == "abandoned"

    # and nothing re-enters `waiting`
    after_abandon = client.post("/dungeons/instance/start", json={
        "instance_id": abandon_waiting["instance_id"],
        "player_id": abandon_waiting["leader"]},
        headers=_session(abandon_waiting["leader"]))
    assert after_abandon.status_code == 400, after_abandon.text


def test_failing_a_run_is_permanent_and_denies_every_later_move(client):
    """Both terminal states refuse advance AND claim, and claim answers 200 with
    an empty reward list rather than an error.

    Measured: ``claim-loot`` after the run ends returns 200 and ``rewards: []``.
    A panel that treats "200" as "you were paid" would draw an empty chest and no
    error; the paid/not-paid signal is the LIST, never the status code.
    """
    run = _active_run(client, "StateTerminal")
    leader, instance_id = run["leader"], run["instance_id"]

    assert client.post("/dungeons/instance/fail", json={
        "instance_id": instance_id, "player_id": leader},
        headers=_session(leader)).status_code == 200

    advanced = client.post("/dungeons/instance/advance", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert advanced.status_code == 400, advanced.text

    claimed = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert claimed.status_code == 200, claimed.text
    assert set(claimed.json()) == CLAIM_LOOT_KEYS
    assert claimed.json()["rewards"] == [], (
        "a failed run paid loot; the status code alone would have hidden this")


# ======================================================================================
# 6. THE PAYOUT
#
# The one route that moves value, so it is checked against the STORE, not against
# its own reply. A route that advertises a reward and credits nothing is exactly
# the defect `test_dungeon_loot_credit.py` was written for.
# ======================================================================================
def test_claim_loot_credits_the_inventory_and_the_repeat_pays_nothing(client):
    """THE PAIR, because neither half is meaningful alone.

    First claim returns the item AND the player's inventory actually holds it.
    The repeat returns 200 with an empty list -- the once-only guard working, not
    a bug. Asserting "a repeat is refused" would be wrong; asserting only "the
    first paid" would pass against a route that pays on every call.
    """
    run = _completed_run(client, "PayoutLoot")
    leader, instance_id = run["leader"], run["instance_id"]

    before = _inventory(client, leader)
    assert "contract_dungeon_token" not in before

    first = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert first.status_code == 200, first.text
    assert set(first.json()) == CLAIM_LOOT_KEYS, (
        f"claim-loot keys changed: {sorted(first.json())}")
    assert first.json()["ok"] is True

    rewards = first.json()["rewards"]
    assert rewards, "the completed, seeded run paid nothing"
    assert set(rewards[0]) == REWARD_ITEM_KEYS, (
        f"reward item keys changed: {sorted(rewards[0])}")
    assert rewards[0]["item_id"] == "contract_dungeon_token"
    assert rewards[0]["count"] == 2

    # The store, not the reply: this is the assertion that catches a route which
    # describes a reward it never grants.
    after = _inventory(client, leader)
    assert after.get("contract_dungeon_token") == 2, (
        f"the reply advertised 2 tokens and the inventory holds "
        f"{after.get('contract_dungeon_token')!r}")

    repeat = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["rewards"] == [], (
        "the once-only loot guard did not hold; a second claim paid out")
    assert _inventory(client, leader).get("contract_dungeon_token") == 2, (
        "the blocked repeat still credited the inventory")


@pytest.mark.parametrize("reward_type", ["first_clear", "weekly"])
def test_first_clear_and_weekly_pay_the_caller_too(client, reward_type):
    """Both are keyed per PLAYER, not per run -- measured, both players paid.

    This is the contrast with the ``loot`` ledger, which is keyed on the item id
    alone and starves a co-player. A panel showing a shared chest must know that
    these two pay everyone and ``loot`` does not.
    """
    leader = _new_player(client, f"Payout{reward_type}Lead")
    member = _new_player(client, f"Payout{reward_type}Mem")
    party_id = _new_party(client, leader, members=(member,))
    instance_id = _new_instance(client, leader, party_id)
    assert client.post("/dungeons/instance/start", json={
        "instance_id": instance_id, "player_id": leader},
        headers=_session(leader)).status_code == 200
    for _ in range(12):
        step = client.post("/dungeons/instance/advance", json={
            "instance_id": instance_id, "player_id": leader},
            headers=_session(leader))
        if step.json()["completed"]:
            break
    else:
        pytest.fail("the two-member run never completed")

    paid = {}
    for pid in (leader, member):
        response = client.post("/dungeons/instance/claim-loot", json={
            "instance_id": instance_id, "player_id": pid,
            "reward_type": reward_type}, headers=_session(pid))
        assert response.status_code == 200, response.text
        assert set(response.json()) == CLAIM_LOOT_KEYS
        paid[pid] = response.json()["rewards"]
        assert paid[pid], (
            f"the {reward_type} payout starved a party member; this ledger is "
            "documented as per-player, so an empty list is a defect")

    for pid, rewards in paid.items():
        assert set(rewards[0]) == REWARD_ITEM_KEYS
        assert _inventory(client, pid), (
            f"{reward_type} advertised items but credited nothing for {pid}")

    # And a repeat by the same player pays nothing, per this ledger's own key.
    repeat = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader,
        "reward_type": reward_type}, headers=_session(leader))
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["rewards"] == [], (
        f"the {reward_type} once-per-player guard did not hold")


def test_an_unknown_reward_type_is_400_before_any_ledger_is_touched(client):
    """The validation happens BEFORE the manager is consulted, so a typo cannot
    burn a claim the player is still owed."""
    run = _completed_run(client, "PayoutBadType")
    leader, instance_id = run["leader"], run["instance_id"]
    before = _inventory(client, leader)

    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "jackpot"},
        headers=_session(leader))
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Invalid reward type"
    assert _inventory(client, leader) == before

    # The three real values are exactly these -- a fourth must not be invented.
    assert set(VALID_REWARD_TYPES) == {"loot", "first_clear", "weekly"}


def test_a_claim_by_a_stranger_is_403_and_credits_nobody(client):
    """The refusal, paired with the store read: a 403 that still credited the
    caller would pass a status-only assertion."""
    run = _completed_run(client, "PayoutStranger")
    leader, instance_id = run["leader"], run["instance_id"]
    stranger = _new_player(client, "PayoutStrangerSelf")
    assert get_dungeon_manager().player_party.get(stranger) is None, "fixture"

    before = _inventory(client, stranger)
    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": stranger, "reward_type": "loot"},
        headers=_session(stranger))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Not a participant in this instance"
    assert _inventory(client, stranger) == before, (
        "the refused claim still credited the stranger")

    # And the run's owner is still owed everything -- a refusal must not burn it.
    paid = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert paid.status_code == 200, paid.text
    assert paid.json()["rewards"], "the refused theft left the real member unpaid"


def test_claiming_your_own_run_under_another_members_name_is_403(client):
    """The body ``player_id`` IS the acting-player claim here, so a member
    cannot pay the run into somebody else's inventory."""
    leader = _new_player(client, "ClaimCrossLead")
    member = _new_player(client, "ClaimCrossMem")
    party_id = _new_party(client, leader, members=(member,))
    instance_id = _new_instance(client, leader, party_id)
    assert client.post("/dungeons/instance/start", json={
        "instance_id": instance_id, "player_id": leader},
        headers=_session(leader)).status_code == 200

    leader_inventory = _inventory(client, leader)
    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(member))
    assert response.status_code == 403, response.text
    assert _inventory(client, leader) == leader_inventory, (
        "a member claimed the run under the leader's name and was paid")


def test_claiming_an_unknown_instance_is_403_not_404(client):
    """Same answer as a real run, so a guessed id cannot be confirmed."""
    owner = _new_player(client, "ClaimGhost")
    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": "instance_does_not_exist", "player_id": owner,
        "reward_type": "loot"}, headers=_session(owner))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Not a participant in this instance"


# ======================================================================================
# 7. THE 409 LEAVE GATE  (added 2026-10-03)
#
# The newest behaviour on this router and, before this section, the only one with
# NO test anywhere in server/tests/ -- `grep -rn 409 server/tests/test_dungeon*`
# returned zero hits while `dungeon_routes.py` carried the branch. It is pinned
# here because it is the one gate whose removal is DESTRUCTIVE rather than
# merely permissive: see the route's own docstring for the run it used to strand.
#
# It is a 409 and not a 400 because nothing about the request was malformed --
# the request is fine, the world state is what blocks it. A client that treats
# every 4xx alike will retry this forever, so the code and the remedy (abandon
# the run, by its named route) are both asserted.
# ======================================================================================
def test_leaving_a_party_with_a_run_in_progress_is_409(client):
    """The gate, plus the two things a 409 must NOT have done."""
    run = _active_run(client, "LeaveInProgress")
    leader, party_id, instance_id = run["leader"], run["party_id"], run["instance_id"]

    response = client.post("/dungeons/party/leave", json={"player_id": leader},
                           headers=_session(leader))
    assert response.status_code == 409, (
        f"leaving a party with a live run must be refused with 409, got "
        f"{response.status_code} {response.text[:200]}")
    detail = response.json()["detail"]

    # The remedy is named AND is a real route, so a client can act on the
    # message without the reader having to know the router's surface.
    assert "in progress" in detail, f"the 409 does not say what blocks it: {detail!r}"
    assert "POST /dungeons/instance/abandon" in detail, (
        f"the 409 names no route to recover with: {detail!r}")
    # The refused leave mutated NOTHING. A status-only assertion would pass
    # against a gate that disbands the party and then reports 409.
    assert get_dungeon_manager().player_party.get(leader) == party_id, (
        "the refused leave still removed the leader from the party")
    assert get_dungeon_manager().instances[instance_id].state.value == "active", (
        "the refused leave still ended the run")


def test_abandoning_the_run_first_unblocks_the_leave(client):
    """The gate is a SEQUENCE, not a permanent lock: the contract is a path.

    Without this arm the 409 above would read as "leaving is impossible while a
    run exists", which is not what the code does and would strand a player who
    has no other way to dissolve a party.
    """
    run = _active_run(client, "LeaveAfterAbandon")
    leader, instance_id = run["leader"], run["instance_id"]

    blocked = client.post("/dungeons/party/leave", json={"player_id": leader},
                          headers=_session(leader))
    assert blocked.status_code == 409, blocked.text

    abandoned = client.post("/dungeons/instance/abandon", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert abandoned.status_code == 200, abandoned.text

    allowed = client.post("/dungeons/party/leave", json={"player_id": leader},
                          headers=_session(leader))
    assert allowed.status_code == 200, (
        f"leave stayed blocked after the run was abandoned: {allowed.text[:200]}")


@pytest.mark.parametrize("started", [False], ids=["waiting_run_is_still_allowed"])
def test_the_409_gate_does_not_reach_a_run_that_never_started(client, started):
    """The scope decision, pinned because it is a DELIBERATE narrowing.

    The route's docstring says WAITING is allowed on purpose: nothing is spent
    and no loot exists yet, so a broader refusal would overturn a decision that
    belongs to the human. Left unpinned, a future "make it consistent" edit
    would silently take that decision away -- and it would look like a fix.
    """
    run = _waiting_run(client, "LeaveWaitingScope")
    leader = run["leader"]
    assert get_dungeon_manager().instances[run["instance_id"]].state.value == "waiting", (
        "fixture: the run is not waiting, so this arm measures nothing")

    response = client.post("/dungeons/party/leave", json={"player_id": leader},
                           headers=_session(leader))
    assert response.status_code == 200, (
        f"the 409 gate reached a run that was never started, which the route's "
        f"own docstring says it must not: {response.status_code} {response.text[:200]}")
    assert get_dungeon_manager().player_party.get(leader) is None, (
        "the allowed leave did not disband the solo party")


def test_the_409_refusal_does_not_leak_another_players_run_state(client):
    """The gate resolves the run through the CALLER's own party, not a claimed id.

    A stranger naming a victim in the body is refused upstream by the central
    ownership guard, so the status here proves that guard, not this branch. What
    this adds is the consequence: the victim's party and run are untouched, so
    the refusal cannot be used to probe another player's run state.
    """
    victim_run = _active_run(client, "LeaveGateVictim")
    victim, party_id, instance_id = (victim_run["leader"], victim_run["party_id"],
                                     victim_run["instance_id"])
    stranger = _new_player(client, "LeaveGateStranger")

    response = client.post("/dungeons/party/leave", json={"player_id": victim},
                           headers=_session(stranger))
    assert response.status_code == 403, response.text
    assert get_dungeon_manager().player_party.get(victim) == party_id, (
        "the refused leave ejected the victim from their own party")
    assert get_dungeon_manager().instances[instance_id].state.value == "active", (
        "the refused leave ended the victim's run")


# ======================================================================================
# 8. THE CREDIT ITSELF  (added 2026-10-03)
#
# ``_credit_loot`` is the write-back that decides what a player's inventory
# actually holds. Its contract has two edges that a response-only test cannot
# see, because both concern rows the reply DROPS:
#
#   * two entries for the same item in one payout must SUM, not overwrite. An
#     assignment (``inventory[item_id] = count``) would make the reply and the
#     store disagree with each other -- the reply would carry both rows while
#     the wallet held only the second. Asserting the reply alone passes on that
#     bug, so these arms read the store.
#   * a non-positive count is skipped entirely, and appears in NEITHER the reply
#     nor the inventory. A route that echoed it would advertise a reward it did
#     not grant.
#
# Both measured before being pinned; see the probe note in the commit message.
# ======================================================================================
def _completed_run_with_loot(client, role: str, loot) -> dict:
    """A completed run whose drop list is FORCED to ``loot``.

    ``_completed_run`` seeds one row; this takes any list, because the shapes
    under test -- a duplicated item and a zero count -- are precisely the ones
    a random drop would not reliably produce.
    """
    run = _completed_run(client, role)
    dm = get_dungeon_manager()
    dm.instances[run["instance_id"]].loot_dropped = list(loot)
    dm.instances[run["instance_id"]].rewards_claimed = set()
    return run


def test_two_rows_for_the_same_item_sum_into_one_inventory_count(client):
    """The reply lists both rows; the inventory holds ONE summed count.

    The assertion that matters is the store read. An implementation that
    ASSIGNED rather than accumulated would return a correct-looking two-row
    reply and hold only the second row's count -- a payout described in full and
    partly never granted.
    """
    run = _completed_run_with_loot(client, "CreditAccum", [
        {"item_id": "contract_dup_token", "count": 2, "source": "dungeon_loot"},
        {"item_id": "contract_dup_token", "count": 3, "source": "dungeon_loot"},
    ])
    leader, instance_id = run["leader"], run["instance_id"]

    before = _inventory(client, leader)
    assert "contract_dup_token" not in before

    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert response.status_code == 200, response.text

    rewards = response.json()["rewards"]
    assert len(rewards) == 2, (
        f"the reply should carry both rows verbatim, got {rewards}")
    assert [r["count"] for r in rewards] == [2, 3], rewards

    after = _inventory(client, leader)
    assert after.get("contract_dup_token") == 5, (
        f"two rows of 2 and 3 should credit 5, the inventory holds "
        f"{after.get('contract_dup_token')!r} -- the second row overwrote the first")


def test_a_zero_count_row_is_credited_to_neither_the_reply_nor_the_inventory(client):
    """Skipped rows must not appear anywhere.

    Two halves, and the second is the one that catches a real bug: a route that
    echoed the skipped row would tell the player it paid something it did not.
    """
    run = _completed_run_with_loot(client, "CreditZeroCount", [
        {"item_id": "contract_zero_token", "count": 0, "source": "dungeon_loot"},
        {"item_id": "contract_real_token", "count": 1, "source": "dungeon_loot"},
    ])
    leader, instance_id = run["leader"], run["instance_id"]

    before = _inventory(client, leader)
    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert response.status_code == 200, response.text

    rewards = response.json()["rewards"]
    credited_ids = {r["item_id"] for r in rewards}
    assert "contract_zero_token" not in credited_ids, (
        f"a zero-count row was advertised as paid: {rewards}")
    assert credited_ids == {"contract_real_token"}, (
        f"the surviving rows changed: {sorted(credited_ids)}")

    after = _inventory(client, leader)
    assert after.get("contract_zero_token") in (None, 0), (
        f"a zero-count row reached the inventory as "
        f"{after.get('contract_zero_token')!r}")
    assert after.get("contract_real_token") == 1, (
        f"the real row was not credited: {after.get('contract_real_token')!r}")
    # The ONLY change to the store is the surviving row. Asserting the two
    # specific keys is what makes this arm falsifiable: an earlier draft ended
    # on `after != before or before.get(...) == 1`, which is true for almost any
    # two dicts and so could not fail.
    assert set(after) - set(before) == {"contract_real_token"}, (
        f"the payout added keys the player never earned: "
        f"{sorted(set(after) - set(before))}")


def test_a_claim_adds_to_an_existing_inventory_rather_than_replacing_it(client):
    """The one accumulation edge that is reachable without inventing state.

    ``_credit_loot`` does ``inventory[item_id] = inventory.get(item_id, 0) + count``.
    An implementation that ASSIGNED would pass every other payout arm in this
    file -- a fresh player has an empty inventory, so assigning and adding are
    indistinguishable -- while silently DESTROYING a stack the player already
    held. Seeding one is therefore the whole point of this test.

    Reachable without contrivance: the player simply already owns the item. A
    nameless row is NOT tested here because it is unreachable -- the only writer
    of ``instance.loot_dropped`` is ``dungeons.py:628``, which copies
    ``item_id`` from the loot table, so a row without one cannot occur. An
    earlier draft of this section seeded one and answered 500 from the manager's
    own ``item["item_id"]`` subscript, which measured a fiction, not a contract.
    """
    run = _completed_run_with_loot(client, "CreditStack", [
        {"item_id": "contract_stacked_token", "count": 2, "source": "dungeon_loot"},
    ])
    leader, instance_id = run["leader"], run["instance_id"]

    with players_db.transaction(leader):
        row = players_db[leader]
        inventory = row.setdefault("inventory", {})
        inventory["contract_stacked_token"] = 7
        inventory["contract_untouched_token"] = 3
        players_db.persist(leader, row)

    response = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": instance_id, "player_id": leader, "reward_type": "loot"},
        headers=_session(leader))
    assert response.status_code == 200, response.text
    assert response.json()["rewards"], "the completed, seeded run paid nothing"

    after = _inventory(client, leader)
    assert after.get("contract_stacked_token") == 9, (
        f"the claim must ADD to the 7 already held, the inventory holds "
        f"{after.get('contract_stacked_token')!r} -- the stack was replaced")
    assert after.get("contract_untouched_token") == 3, (
        "the payout dropped an unrelated stack the player already held")


# ======================================================================================
# 9. WHAT THE ROUTER LEAKS BY ITS STATUS CODES  (added 2026-10-04)
#
# Every section above pins what a route DOES. This one pins where a route tells a
# stranger something it did not mean to, which is the half of a contract a client
# author cannot see and an attacker reads first.
#
# This section found two leaks on 2026-10-04 and BOTH ARE NOW CLOSED. The two tests
# that measured them are rewritten below from "here is the oracle" into "here is the
# refusal, and it is the same one as for a real id". Read them as the fix's pins, not
# as open findings:
#
#   1. `POST /dungeons/instance/create` looked the party up with `dm.get_party` and
#      raised 404 "Party not found" BEFORE the membership check, so 404-for-a-guessed-
#      id against 403-for-a-real-one confirmed the party id space to any signed-in
#      player. It now calls `_require_party_member`, which answers 403 for both.
#   2. `POST /dungeons/party/invite` checked `invitee_id` against the player store
#      BEFORE `_require_party_member`, so a stranger learned whether a PLAYER id
#      existed. The membership gate now runs first; the invitee lookup happens after,
#      for callers who have already proved they belong to the party.
#
# Both routes now match `GET /dungeons/party/{party_id}` (pinned above) and
# `POST /dungeons/instance/claim-loot` (pinned below): one status code for "no such
# thing" and "not yours". A client can no longer tell a typo from a stolen id.
#
# The one contrast still standing is below: `claim-loot` answers 400 for a bad
# reward type ahead of the 403. That ordering is deliberate and is pinned as-is.
# ======================================================================================
def test_instance_create_answers_403_for_a_guessed_party_and_a_real_one(client):
    """Same outsider, same call, now the same answer for both -- the leak is closed.

    Before 2026-10-04 this arm read 404 "Party not found" for `party_no_such_id`
    and 403 "Not a member of this party" for a real party, which turned
    `/dungeons/instance/create` into an oracle for "was somebody's party really
    created with this id?". That difference is gone: `_require_party_member` raises
    403 whether the party is missing or merely not the caller's.
    """
    outsider = _new_player(client, "LeakCreateOutsider")
    victim = _new_player(client, "LeakCreateVictim")
    real_party = _new_party(client, victim)

    guessed = client.post("/dungeons/instance/create", json={
        "party_id": "party_no_such_id", "template_id": "spirit_beast_den"},
        headers=_session(outsider))
    actual = client.post("/dungeons/instance/create", json={
        "party_id": real_party, "template_id": "spirit_beast_den"},
        headers=_session(outsider))

    assert guessed.status_code == 403, guessed.text
    assert guessed.json()["detail"] == "Not a member of this party"
    assert actual.status_code == 403, actual.text
    assert actual.json()["detail"] == "Not a member of this party"
    assert guessed.status_code == actual.status_code and \
        guessed.json() == actual.json(), (
        "the two answers differ again, so the party id space is confirmable")

    # The refusal created nothing for either party.
    dm = get_dungeon_manager()
    assert dm.player_party.get(victim) == real_party
    assert dm.get_party(real_party).instance_id is None, (
        "the refused create still bound an instance to the victim's party")


def test_party_invite_answers_403_for_a_fake_invitee_and_a_real_one(client):
    """The mirror image, also closed: an outsider no longer learns who exists.

    `party/invite` used to validate `invitee_id` against the player store BEFORE it
    asked whether the caller was in the party, so a stranger got 404 for a fake id
    and 403 for a real one -- a player-id oracle. `_require_party_member` now runs
    first, so an outsider is refused identically in both cases.
    """
    outsider = _new_player(client, "LeakInviteOutsider")
    real_invitee = _new_player(client, "LeakInviteReal")
    victim = _new_player(client, "LeakInviteVictim")
    party_id = _new_party(client, victim)

    fake = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": "player_no_such_row"},
        headers=_session(outsider))
    real = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": real_invitee},
        headers=_session(outsider))

    assert fake.status_code == 403, fake.text
    assert fake.json()["detail"] == "Not a member of this party"
    assert real.status_code == 403, real.text
    assert real.json()["detail"] == "Not a member of this party"
    assert fake.json() == real.json(), (
        "the two answers differ again, so the player id space is confirmable")

    # The second refusal is the load-bearing one: the outsider is not in the
    # party, so nothing was recruited despite having a real invitee id.
    assert get_dungeon_manager().player_party.get(real_invitee) is None, (
        "an outsider recruited somebody into a party they do not belong to")
    assert get_dungeon_manager().get_party(party_id).members.keys() == {victim}, (
        "the refused invite still grew the victim's roster")

    # And the invitee lookup did not move out of the route: a caller who IS in
    # the party still gets 404 for a player id that does not exist, so a real
    # leader learns nothing from a typo. Both branches survive the reorder.
    leader_probe = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": "player_no_such_row"},
        headers=_session(victim))
    assert leader_probe.status_code == 404, leader_probe.text
    assert leader_probe.json()["detail"] == "Player not found"


def test_a_bad_reward_type_is_refused_before_participation_is_considered(client):
    """400 beats 403: input validation runs ahead of the ownership gate.

    MEASURED. A stranger in no party gets 400 "Invalid reward type" for a bogus
    type and 403 "Not a participant in this instance" for a real one -- so the
    route validates the enum before it looks at the run. That ordering is
    deliberate (the module docstring says so: a typo must not burn a claim the
    player is still owed) and it is what makes the bad-type arm above safe to run
    against a completed run.

    Pinned because a client cannot otherwise tell a typo from a stolen run: both
    are 4xx, and only the ORDER says which happened first.
    """
    stranger = _new_player(client, "RewardOrderStranger")
    assert get_dungeon_manager().player_party.get(stranger) is None, (
        "fixture: the stranger is in a party, so this arm measures the wrong gate")

    bad = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": "instance_no_such_id", "player_id": stranger,
        "reward_type": "jackpot"}, headers=_session(stranger))
    assert bad.status_code == 400, bad.text
    assert bad.json()["detail"] == "Invalid reward type"

    good = client.post("/dungeons/instance/claim-loot", json={
        "instance_id": "instance_no_such_id", "player_id": stranger,
        "reward_type": "loot"}, headers=_session(stranger))
    assert good.status_code == 403, good.text
    assert good.json()["detail"] == "Not a participant in this instance"


# ======================================================================================
# 10. THE COMPLETION REPLY  (added 2026-10-04)
#
# `advance` has exactly two outcomes worth drawing -- one more floor, or the run is
# finished -- and this file pinned only the FIRST. A panel draws its win banner off
# the second, so the reply that ends a run is the one most worth freezing.
#
# Measured on `spirit_beast_den`: the third advance answers ``state:
# "completed"``, ``completed: true``, and ``current_floor == total_floors == 3``.
# A fourth advance answers 400 "Cannot advance floor".
# ======================================================================================
def test_the_final_advance_is_the_only_reply_that_completes_the_run(client):
    """Same six keys, one of them flipped: `completed` and `state`."""
    run = _active_run(client, "CompletionReply")
    leader, instance_id = run["leader"], run["instance_id"]

    final = None
    for _ in range(12):
        step = client.post("/dungeons/instance/advance", json={
            "instance_id": instance_id, "player_id": leader},
            headers=_session(leader))
        assert step.status_code == 200, step.text
        assert set(step.json()) == INSTANCE_ADVANCE_KEYS, (
            f"the completion reply changed shape: {sorted(step.json())}")
        if step.json()["completed"]:
            final = step.json()
            break
    assert final is not None, "the run never completed; this arm measures nothing"

    assert final["state"] == "completed"
    assert final["completed"] is True
    # The floor counter lands ON the last floor rather than one past it, so a
    # panel indexing floors by `current_floors - 1` stays in range.
    assert final["current_floor"] == final["total_floors"], (
        f"the run ended on floor {final['current_floor']} of "
        f"{final['total_floors']}")
    assert isinstance(final["loot_dropped"], list), (
        "the completion reply carries no loot list; the drop may be empty")

    # ...and the run is closed: one more advance is refused, not silently
    # accepted as a no-op.
    after = client.post("/dungeons/instance/advance", json={
        "instance_id": instance_id, "player_id": leader}, headers=_session(leader))
    assert after.status_code == 400, after.text
    assert after.json()["detail"] == "Cannot advance floor"


# ======================================================================================
# 11. TWO REFUSALS A CLIENT CANNOT BRANCH ON  (added 2026-10-04)
#
# Both measured. Neither is claimed to be a bug; both are contract facts a panel
# author will otherwise assume the hard way, because the code reads as though the
# case is handled.
#
#  * `party/invite`'s 400 message promises three causes -- "party full, player
#    already in party, or party not found". MEASURED: `spirit_beast_den` caps a
#    party at 3, and the fourth invite is ACCEPTED (200, roster of 4). So the
#    "party full" cause is unreachable on this template and the client cannot
#    render a capacity warning from this route's answer.
#  * `instance/create`'s 400 covers "check party size, readiness, and template
#    requirements" -- three causes behind one string, with the re-read as the only
#    way to tell them apart. Already pinned above for party size; recorded here so
#    the two single-cause-looking 400s are not confused for each other.
# ======================================================================================
def test_party_invite_does_not_enforce_the_templates_party_cap(client):
    """MEASURED: a party of 4 is accepted where the template caps it at 3.

    Pinned because the route's own 400 text lists "party full" first. A panel
    that reads that message as a capacity signal would draw a warning the server
    never sends, and a group could form that the template's own `max_party_size`
    says should be impossible.
    """
    template = get_dungeon_manager().templates["spirit_beast_den"]
    cap = template.max_party_size
    leader = _new_player(client, "CapLead")
    party_id = _new_party(client, leader)

    for index in range(cap - 1):
        recruit = _new_player(client, f"CapRecruit{index}")
        joined = client.post("/dungeons/party/invite", json={
            "party_id": party_id, "invitee_id": recruit}, headers=_session(leader))
        assert joined.status_code == 200, joined.text

    overflow = _new_player(client, "CapOverflow")
    over = client.post("/dungeons/party/invite", json={
        "party_id": party_id, "invitee_id": overflow}, headers=_session(leader))
    assert over.status_code == 200, (
        "the invite route now enforces the template cap, so the 400 below is "
        f"unreachable and this arm's premise changed: {over.status_code} "
        f"{over.text[:200]}")

    reread = client.get(f"/dungeons/party/{party_id}", params={"player_id": leader},
                        headers=_session(leader))
    roster = reread.json()["members"]
    assert len(roster) == cap + 1, (
        f"the roster holds {len(roster)} but the invite was answered 200")
    assert template.max_party_size < len(roster), (
        "the template's cap no longer disagrees with the roster, so the "
        "'party full' branch of the 400 has become reachable and this arm is "
        "stale -- re-measure before updating it")
    assert overflow in {m["player_id"] for m in roster}


def test_leave_by_a_session_for_a_player_with_no_row_is_refused(client):
    """A session naming a player the store has never heard of is refused.

    MEASURED 2026-10-05 against the real app: 401, not 404. ``issue_session``
    still mints a token for any string, but ``auth.authenticate`` now confirms
    the session's player still exists and revokes the token when it does not,
    so the request never reaches this route. That is the deleted-player
    defense and it is deliberately the stronger answer: a token must stop
    working the moment its account is gone, and the status must not reveal
    whether an id is live.

    The state assertion below is kept and is the point that still matters: a
    refused leave must not have indexed a party for a player with no row.
    """
    ghost = "ghost_player_no_such_row_for_leave"
    response = client.post("/dungeons/party/leave", json={"player_id": ghost},
                           headers=_session(ghost))
    assert response.status_code == 401, response.text
    assert get_dungeon_manager().player_party.get(ghost) is None, (
        "the refused leave still indexed a party for a player with no row")


# ======================================================================================
# 12. THE RESERVED-WORD PATH CONVERTOR
#
# The one part of this module that is not an ordinary route. `dungeon_routes.py`
# registers two custom Starlette path convertors that make the two READ routes
# refuse a fixed set of sibling POST literals. Without them, Starlette keeps the
# FIRST route that fully matches path AND method, so `GET /dungeons/party/create`
# is answered by `get_party` with a MEMBERSHIP refusal -- which reads to a client
# as an authorization bug on the create route. The convertor instead makes the
# read route not match at all, so the router falls back to the POST route it
# remembered and the caller gets a real 405 naming the verb it should have used.
#
# This is a CLIENT-FACING contract, which is why it belongs in a contract test
# and not only in the router's own comments: a panel that probes its own route
# table with the wrong verb must see 405, never a 403.
#
# Both arms below were MEASURED on the live app before being written, and the 405
# arm was proven to BITE: a negative control that replaced the two read routes'
# compiled regexes with a plain single-segment matcher -- i.e. "the convertor
# exists but excludes nothing" -- turned all four probed literals from 405 into
# 403 "Not a member of this party" / "Not a participant in this instance", the
# read handler shadowing the POST route exactly as the convertor prevents. A 405
# arm that survives that sabotage is measuring something other than the
# convertor.
# ======================================================================================
@pytest.mark.parametrize("literal", [
    "/dungeons/party/create", "/dungeons/party/invite",
    "/dungeons/party/leave", "/dungeons/party/ready",
    "/dungeons/instance/create", "/dungeons/instance/start",
    "/dungeons/instance/advance", "/dungeons/instance/fail",
    "/dungeons/instance/abandon", "/dungeons/instance/claim-loot",
])
def test_a_get_on_a_post_only_literal_is_405_naming_post(client, literal):
    """A signed-in caller's GET is still 405 -- this is not an auth artefact.

    MEASURED on all ten: 405, header ``Allow: POST``, body
    ``{"detail":"Method Not Allowed"}``.

    The session is real and no body is sent on purpose: the point is that the
    read route refuses to match a reserved literal, so the request falls through
    to method dispatch. Asserting ``405`` alone would be satisfied by a route that
    405s for any reason, so the ``Allow`` header is pinned too -- that is the part
    a client reads to discover the correct verb.
    """
    owner = _new_player(client, "Reserved" + literal.rsplit("/", 1)[-1])
    response = client.get(literal, params={"player_id": owner},
                          headers=_session(owner))
    assert response.status_code == 405, (
        f"GET {literal} was answered {response.status_code} instead of 405, so a "
        "read route is shadowing the POST route, or the reserved-word convertor "
        f"is gone: {response.text[:160]}")
    assert response.headers.get("allow") == "POST", (
        f"GET {literal} did not name POST in its Allow header: "
        f"{response.headers.get('allow')!r}")
    assert response.json()["detail"] == "Method Not Allowed"


def test_an_unknown_but_legal_id_still_reaches_the_read_route(client):
    """The convertor excludes TEN words, not "anything unfamiliar".

    Its negative lookahead is anchored on the borrowed POST literals only, so an
    id the store has never heard of -- a made-up string that is still a legal
    single path segment -- must match the read route and be refused on its own
    merits. MEASURED: 403 "Not a member of this party" for the party read and 403
    "Not a participant in this instance" for the instance read.

    This arm keeps the 405 arms honest in the OTHER direction. If the exclusion
    were widened to "anything the store does not know", the read routes would
    answer 404 to every unknown id and the 405 test above would stay green while a
    client could no longer distinguish "no such party" from "you may not read that
    party". Asserting the refusal DETAIL, not just the status, is what pins the
    distinction: both answers are 403 and neither is a 404, so a stranger cannot
    enumerate live ids through either route.
    """
    owner = _new_player(client, "UnknownId")
    headers = _session(owner)

    party = client.get("/dungeons/party/zzz-does-not-exist",
                       params={"player_id": owner}, headers=headers)
    assert party.status_code == 403, party.text
    assert party.json()["detail"] == "Not a member of this party"

    instance = client.get("/dungeons/instance/zzz-does-not-exist",
                          params={"player_id": owner}, headers=headers)
    assert instance.status_code == 403, instance.text
    assert instance.json()["detail"] == "Not a participant in this instance"


def test_the_convertor_does_not_change_the_public_path_a_client_reads(client):
    """The constraint is invisible to the route table and to the schema.

    ``register_dungeon_routes`` rewrites each constrained route's ``path`` to its
    ``path_format`` AFTER ``include_router``, so both the live route table and the
    OpenAPI document spell the two reads the way a client writes them:
    ``/dungeons/party/{party_id}`` and ``/dungeons/instance/{instance_id}``.

    MEASURED: neither the route table nor the schema contains the convertor name,
    and both contain the 14 public paths. Pinned because the fix is order-
    dependent -- cleaning the name BEFORE ``include_router`` rebuilds the route
    from a path with no convertor in it, the shadowing silently returns, and a
    generated client is handed ``{party_id:dungeon_party_id}``.
    """
    live = {getattr(route, "path", "") for route in app.routes
            if getattr(route, "path", "").startswith("/dungeons")}
    for path in ("/dungeons/party/{party_id}", "/dungeons/instance/{instance_id}"):
        assert path in live, (
            f"{path} is not the published spelling, so a route census and any "
            f"generated client would read something else: {sorted(live)}")
    leaked = {p for p in live
              if "dungeon_party_id" in p or "dungeon_instance_id" in p}
    assert not leaked, (
        f"the internal path convertor name leaked into the public route table: "
        f"{sorted(leaked)}")

    spec = client.get("/openapi.json").json()
    for path in ("/dungeons/party/{party_id}", "/dungeons/instance/{instance_id}"):
        assert path in spec["paths"], (
            f"{path} is missing from the schema document: "
            f"{sorted(p for p in spec['paths'] if p.startswith('/dungeons'))}")


def test_the_post_literal_still_answers_a_post_after_the_read_route_declines_it(client):
    """The 405 above must not have cost the literal its own working verb.

    A convertor that excluded a POST literal from EVERY route, not only from the
    read route, would make the 405 arms pass while leaving the whole party-
    management surface unreachable. So each arm is closed by driving the literal
    the way a client actually does.

    MEASURED: ``POST /dungeons/party/create`` answers 200 with the four-key
    party-create shape, and a following ``GET`` on the returned ``party_id``
    answers 200 -- so the returned id is usable in the read route's path, which is
    the round trip a panel performs.
    """
    leader = _new_player(client, "LiteralRoundTrip")
    created = client.post("/dungeons/party/create", json={"leader_id": leader},
                          headers=_session(leader))
    assert created.status_code == 200, created.text
    body = created.json()
    assert set(body) == PARTY_CREATE_KEYS, sorted(body)
    assert body["leader_id"] == leader

    read_back = client.get(f"/dungeons/party/{body['party_id']}",
                           params={"player_id": leader}, headers=_session(leader))
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["party_id"] == body["party_id"]
