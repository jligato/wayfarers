"""
CONTRACT TEST: server/goal_routes.py -- the two /goals/* routes.

WHAT THIS FILE PINS
------------------
``register_goal_routes(app, players_db_provider, inventory_store_provider)``
mounts exactly two goal routes:

    GET  /goals/{player_id}                 the whole goal view
    GET  /goals/{player_id}/item/{item_id}  what one item is for

and, as a side effect of the same call, also mounts the mission board and the
achievement routes. Those two belong to their own modules and their own tests;
this file pins only the goal routes, and one test pins that the ``/goals`` prefix
is not shadowed by the extra mounts.

"Contract" here means the shape a CLIENT can rely on: which methods mount, what
each reply carries, which store supplies each number, and which status answers
which mistake. Every status and every key set below was measured by driving the
routes through a real HTTP client, not read off the source.

MEASURED REFERENCE TABLE (2026-10-03, this tree)
------------------------------------------------
    GET  /goals/{player_id}, anonymous            200   (see AUTH, below)
    GET  /goals/{player_id}, unknown player       404   "Player not found"
    GET  /goals/{player_id}, unreadable inventory 503   "Inventory unavailable;
                                                           cannot price {item_id}"
    GET  /goals/{player_id}/item/{id}, anonymous  200
    GET  /goals/{player_id}/item/{id}, no player  404   "Player not found"
    GET  /goals/{player_id}/item/{id}, catalog id  with no consumer   200 uses=[]
    GET  /goals/{player_id}/item/{id}, id unknown to the catalog      404
                                                                    "Item not found"
    POST/PUT/DELETE on either goal path           405
    GET  /goals  or  /goals/  (no player id)      404

WHY THE MODULE ITSELF DECLARES NO AUTH, AND WHY THE LIVE APP IS STILL LOCKED
----------------------------------------------------------------------------
This is the single most surprising thing about this module and it is measured,
so it is pinned rather than left as a footnote. Read straight from the route
objects, both goal routes have ``has_auth_dependency == False`` and a player-id
claim of ``['player_id']``. Mounted on a bare FastAPI app they answer an
anonymous caller 200. They are NOT public in the real server: ``main.py`` calls
``install_ownership_guard(app)`` after every registration, and that guard
matches by SHAPE -- a route carrying a bare player-id claim and no auth
dependency. ``/goals`` is not in ``ownership_guard.EXEMPT_PATHS``, so the guard
rewrites both routes and an anonymous call answers 401.

Two tests pin both halves of that, because either alone is a lie:
``test_bare_module_routes_declare_no_auth`` and
``test_shape_guard_adds_auth_to_both_goal_routes``. If the module ever starts
declaring its own ``Depends(require_player)`` the first test turns red and the
guard's ledger changes; if the guard ever stops matching them the second does.

WHY THIS FILE BUILDS ITS OWN APP AND NEVER IMPORTS server.main
--------------------------------------------------------------
``main.py`` is thousands of lines and several agents edit it at once, so a file
that imports it measures whatever a peer is mid-write. That is not
hypothetical: partway through writing this file, ``import server.main`` failed
outright with ``ImportError: cannot import name 'check_status_inline' from
'lifecycle_guard'`` -- one peer's half-finished edit to another module -- and
the failure took down every pytest run in the repo, because the root
``conftest.py`` imports ``server.main`` at collection time. The same peer landed
the fix about an hour later, which is exactly why the result here cannot depend
on it.

The auth test below still gets the guard's real behaviour, by calling the guard
directly on a bare app -- the same call ``main.py`` makes.

Run
---
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
        -u PYTHONPATH python -m pytest server/tests/test_goal_routes_contract.py -v

Passes with and without the repo ``conftest.py`` (53 cases either way). Add
``--noconftest`` to run this file alone while a peer has ``main.py``'s import
graph broken; the root conftest imports ``server.main`` at collection time, so
one peer's half-finished edit otherwise aborts the whole run.

Measured 2026-10-03 on this tree: 44 test functions, 53 cases, 28 of 28
re-injected defects turned the file red.

Two fixture traps this file pays for once, so no test has to:

  * ``register_goal_routes`` REBINDS two module globals
    (``_injected_players_db`` / ``_injected_inventory_db``). A test that
    registers and does not restore them leaks its fake store into the next test
    in the session. ``bare_app`` restores both in a finally.
  * ``TestClient(raise_server_exceptions=False)``. A refusal that should be a
    clean 404/503 is raised as an exception otherwise, which turns a passing
    status assertion into an ERROR on the refusal tests only.
"""
from __future__ import annotations

import contextlib
import copy
import os
import pathlib
import sys
import time
from typing import Any, Dict, Iterator, List, Optional

# ---------------------------------------------------------------------------
# Path setup and in-memory storage, BEFORE the server imports.
# ---------------------------------------------------------------------------
_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A single file run without this reports a wall of errors from an absent
# database, and reading those as failures sends an engineer hunting a bug that
# does not exist. main.py's storage backend is latched at import time.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import goal_graph  # noqa: E402
import goal_routes  # noqa: E402
from goal_graph import _catalog, item_uses  # noqa: E402
from inner_skills import SKILLS  # noqa: E402

# ---------------------------------------------------------------------------
# The surface, named once. A test that retypes the paths is a place to forget.
# ---------------------------------------------------------------------------
GOALS = "/goals/{player_id}"
ITEM = "/goals/{player_id}/item/{item_id}"

# Reply key sets, FROZEN. Clients read these names, so adding a key is safe and
# renaming one breaks every caller. Every set below was measured, not inferred.
VIEW_KEYS = {"goals", "active", "blocked"}
GOAL_KEYS = {
    "goal_id", "scope", "definition_version", "requirements",
    "prerequisite_steps", "eligible_action", "completed",
}
REQUIREMENT_KEYS = {"id", "label", "required", "owned", "unit", "satisfied", "source_ids"}
STEP_KEYS = {"id", "label", "completed"}
TIMER_STEP_KEYS = STEP_KEYS | {"remaining_seconds"}
ITEM_REPLY_KEYS = {"item_id", "uses"}
USE_KEYS = {"goal_id", "label", "required"}


# ---------------------------------------------------------------------------
# Test doubles. The inventory double is a CONTRACT, not a convenience: the route
# calls exactly one method on it, and this file asserts which.
# ---------------------------------------------------------------------------
class FakeInventory:
    """Stands in for ``inventory_store``. Records every read.

    ``raises`` is the broken-store arm. ``raw`` is the unusable-count arm, and
    it needs a sentinel rather than a plain ``None`` default: ``None`` is
    itself a value this file tests (``int(None)`` raises), so a default of
    ``None`` would be indistinguishable from "not set".
    """

    _UNSET = object()

    def __init__(self, held: Optional[Dict[str, Dict[str, int]]] = None,
                 raises: Optional[BaseException] = None,
                 raw: Any = _UNSET) -> None:
        self.held = held or {}
        self.raises = raises
        self.raw = raw
        self.calls: List[tuple] = []

    def get_item_count(self, player_id: str, item_id: str) -> Any:
        self.calls.append((player_id, item_id))
        if self.raises is not None:
            raise self.raises
        if self.raw is not self._UNSET:
            return self.raw
        return self.held.get(player_id, {}).get(item_id, 0)


def player_row(player_id: str, realm: str = "mortal") -> dict:
    """A player row shaped the way goal_graph reads it.

    Only ``cultivation_realm`` and ``inner_skills`` change the reply; the rest
    is present so the row looks like a real player rather than a stub that
    happens to pass.
    """
    return {
        "id": player_id, "name": "Contract", "cultivation_realm": realm,
        "cultivation_stage": "early", "path": "qi", "spirit_root": "fire",
        "progress": 0.0, "spirit_stones": {"low_grade": 1000},
        "atk": 100, "def_": 50, "hp": 1000, "mp": 500, "spd": 100,
        "pvp_rating": 1000, "sect_id": None, "sect_rank": "",
        "sect_contribution": 0, "dao_companion_id": None,
    }


def client_for(players: Any, inventory: Any) -> TestClient:
    """A bare app with the goal routes mounted over the given stores.

    Value providers are accepted as well as callables: the module documents both
    (``players_provider() if callable(players_provider) else
    players_provider``) and a caller may hand either.
    """
    app = FastAPI()
    goal_routes.register_goal_routes(app, players, inventory)
    return TestClient(app, raise_server_exceptions=False)


@contextlib.contextmanager
def bare_app(players: Any, inventory: Any) -> Iterator[FastAPI]:
    """Yield an app whose goal routes are mounted, then restore the globals.

    ``register_goal_routes`` rebinds ``_injected_players_db`` and
    ``_injected_inventory_db``. Without the restore, the legacy module-level
    router -- and any later test -- would keep reading this file's fake store.
    A context manager rather than a pytest fixture, because most tests need
    their OWN store pair and a shared fixture would fix one shape on all of them.
    """
    saved = (goal_routes._injected_players_db, goal_routes._injected_inventory_db)
    app = FastAPI()
    try:
        goal_routes.register_goal_routes(app, players, inventory)
        yield app
    finally:
        goal_routes._injected_players_db, goal_routes._injected_inventory_db = saved


@pytest.fixture()
def goal_client() -> Iterator[TestClient]:
    """One player, ``probe_player``, holding nothing. The empty-hand baseline."""
    with bare_app({"probe_player": player_row("probe_player")}, FakeInventory()) as app:
        yield TestClient(app, raise_server_exceptions=False)


def owned_inventory(player_id: str, **amounts: int) -> FakeInventory:
    """An inventory that holds exactly the item amounts named."""
    return FakeInventory({player_id: dict(amounts)})


# ===========================================================================
# 1. SURFACE: which paths mount, on which methods
# ===========================================================================


def test_exactly_two_goal_paths_mount(goal_client):
    app = goal_client.app
    mounted = {(frozenset(r.methods), r.path) for r in app.routes
               if hasattr(r, "path") and r.path.startswith("/goals")}
    # Collected as a SET of methods, never as next(iter(...)): the set's order is
    # not guaranteed, so a single-element read picks whichever came first. This
    # tree's FastAPI (0.133.1) registers GET only and does not add HEAD, which
    # is why HEAD is accepted rather than required.
    for methods, _path in mounted:
        assert methods <= {"GET", "HEAD"}, f"a goal route answers {methods}"
        assert "GET" in methods, f"a goal route does not answer GET: {methods}"
    assert {path for _methods, path in mounted} == {GOALS, ITEM}


def test_goal_paths_are_not_shadowed_by_the_extra_mounts(goal_client):
    """``register_goal_routes`` also mounts the board and achievements.

    That is deliberate -- main.py has no other ``register_*`` hook for a new
    module -- but it means a prefix collision here would silently take the goal
    panel down with it. Asserted on ``app.routes``, which is the table Starlette
    actually dispatches from, not on the source.
    """
    app = goal_client.app
    prefixes = {r.path.split("/")[1] for r in app.routes
                if hasattr(r, "path") and r.path.startswith("/") and len(r.path) > 1}
    assert {"goals", "mission-board", "achievements"} <= prefixes
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert GOALS in paths and ITEM in paths


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_writes_are_refused_on_both_goal_paths(goal_client, method):
    assert getattr(goal_client, method)("/goals/probe_player").status_code == 405
    assert getattr(goal_client, method)(
        "/goals/probe_player/item/insight_scroll").status_code == 405


@pytest.mark.parametrize("path", ["/goals", "/goals/"])
def test_a_missing_player_id_is_not_a_routed_path(goal_client, path):
    # 404, not 422: the player id is a PATH segment, so omitting it does not
    # reach the handler at all.
    assert goal_client.get(path).status_code == 404


# ===========================================================================
# 2. AUTH: the module declares none, the live app's shape guard adds it
# ===========================================================================


def test_bare_module_routes_declare_no_auth(goal_client):
    """The module's own contribution to the auth surface is a player-id claim.

    Read from the route objects rather than the source, so a future
    ``Depends(require_player)`` here turns this red instead of quietly changing
    what the shape guard has to do.
    """
    from ownership_guard import has_auth_dependency, player_claim_params

    routes = {r.path: r for r in goal_client.app.routes
              if hasattr(r, "path") and r.path.startswith("/goals")}
    assert set(routes) == {GOALS, ITEM}
    for path, route in routes.items():
        assert has_auth_dependency(route.dependant) is False, (
            f"{path} now declares its own auth; the shape guard's ledger and "
            "this module's responsibility both change")
        assert player_claim_params(route.dependant) == ["player_id"], (
            f"{path} no longer carries a bare player-id claim, so "
            "install_ownership_guard can no longer match it by shape")


def test_anonymous_caller_is_served_when_no_guard_is_installed(goal_client):
    """The measured consequence of the row above, stated so it cannot drift.

    Read-only goal data with no session is what the module ships. main.py's
    guard closes it (next test); this file refuses to let the absence be
    mistaken for an oversight in the module, or for permission.
    """
    assert goal_client.get("/goals/probe_player").status_code == 200
    assert goal_client.get("/goals/probe_player/item/insight_scroll").status_code == 200


def test_shape_guard_adds_auth_to_both_goal_routes():
    """What main.py actually does, measured by calling main.py's own call.

    ``install_ownership_guard`` matches by shape, /goals is not in
    ``EXEMPT_PATHS``, and both routes are reported as guarded. An anonymous
    caller then gets 401, and the 404 for a missing player never leaks to one.
    """
    from ownership_guard import (
        EXEMPT_PATHS, has_auth_dependency, install_ownership_guard,
        player_claim_params,
    )

    assert not [p for p in EXEMPT_PATHS if "goal" in p], (
        "/goals became exempt from the ownership guard; the module's routes "
        "are public again")

    players = {"guarded_player": player_row("guarded_player")}
    with bare_app(players, owned_inventory("guarded_player", insight_scroll=9)) as app:
        guarded = install_ownership_guard(app)
        assert guarded.get(GOALS) == ["player_id"], guarded.get(GOALS)
        assert guarded.get(ITEM) == ["player_id"], guarded.get(ITEM)

        for route in app.routes:
            if hasattr(route, "path") and route.path.startswith("/goals"):
                assert has_auth_dependency(route.dependant) is True
                assert player_claim_params(route.dependant) == ["player_id"]

        client = TestClient(app, raise_server_exceptions=False)
        anonymous = client.get("/goals/guarded_player")
        assert anonymous.status_code == 401
        assert client.get("/goals/guarded_player/item/insight_scroll").status_code == 401
        # The refusal comes before the store lookup, so an anonymous caller
        # cannot use the 404 to learn which player ids exist.
        assert client.get("/goals/definitely_not_a_player").status_code == 401


# ===========================================================================
# 3. THE VIEW REPLY: key sets, and where every number comes from
# ===========================================================================


def test_view_reply_carries_exactly_three_keys(goal_client):
    body = goal_client.get("/goals/probe_player").json()
    assert set(body) == VIEW_KEYS


def test_every_goal_carries_exactly_the_documented_keys(goal_client):
    body = goal_client.get("/goals/probe_player").json()
    assert body["goals"], "a player with no skills at all must still be shown goals"
    for goal in body["goals"]:
        assert set(goal) == GOAL_KEYS


def test_one_goal_per_real_inner_skill_in_table_order(goal_client):
    """The goal list is derived from the real skill table, not a local copy.

    Count and ORDER both come from ``SKILLS`` at run time, so adding a skill to
    the table updates this file rather than breaking it.
    """
    body = goal_client.get("/goals/probe_player").json()
    expected = [f"inner_skill_learn:{skill_id}" for skill_id in SKILLS]
    assert [g["goal_id"] for g in body["goals"]] == expected
    assert len({g["goal_id"] for g in body["goals"]}) == len(body["goals"])


def test_every_requirement_carries_exactly_the_documented_keys(goal_client):
    body = goal_client.get("/goals/probe_player").json()
    seen = 0
    for goal in body["goals"]:
        assert goal["requirements"], f"{goal['goal_id']} advertised no requirement"
        for requirement in goal["requirements"]:
            assert set(requirement) == REQUIREMENT_KEYS
            seen += 1
    assert seen


def test_prerequisite_step_keys_and_the_timer_variant(goal_client):
    """A plain gate step has three keys; a running timer adds one.

    The timer key is not present on non-timer steps, so a client cannot read
    ``remaining_seconds`` unconditionally. Pinned as two cases because the
    difference is invisible in a single reply: a fresh player owns nothing and
    holds no timer step at all.
    """
    body = goal_client.get("/goals/probe_player").json()
    for goal in body["goals"]:
        for step in goal["prerequisite_steps"]:
            assert set(step) == STEP_KEYS

    learning = player_row("learning_player")
    learning["inner_skills"] = {"skills": {
        "qi_sense": {"state": "comprehending", "started_at": time.time()}}}
    with bare_app({"learning_player": learning},
                  owned_inventory("learning_player", insight_scroll=9)) as app:
        timer_body = TestClient(app, raise_server_exceptions=False).get(
            "/goals/learning_player").json()

    timed = [step for goal in timer_body["goals"]
             for step in goal["prerequisite_steps"] if "remaining_seconds" in step]
    assert timed, "a comprehending skill must report a running timer"
    for step in timed:
        assert set(step) == TIMER_STEP_KEYS


def test_requirement_amount_comes_from_the_real_skill_table(goal_client):
    """``required`` is read from ``inner_skills.SKILLS`` at run time.

    Frozen here, this would be a snapshot that silently disagrees with the
    economy after a rebalance. Read at run time, it fails the moment the route
    grows its own copy of the number -- which is the whole point.
    """
    skill_id = next(iter(SKILLS))
    definition = SKILLS[skill_id]
    body = goal_client.get("/goals/probe_player").json()
    goal = next(g for g in body["goals"]
                if g["goal_id"] == f"inner_skill_learn:{skill_id}")
    requirement = goal["requirements"][0]

    assert requirement["required"] == definition.learn_material_amount
    assert requirement["id"] == definition.learn_material == requirement["unit"]
    # The label is the shared catalog's display name, so a renamed item reaches
    # the panel without a change here.
    assert requirement["label"] == _catalog()[definition.learn_material]["name"]


def test_owned_count_comes_from_the_injected_inventory(goal_client):
    skill = SKILLS[next(iter(SKILLS))]
    amount = skill.learn_material_amount
    with bare_app({"holder": player_row("holder")},
                  owned_inventory("holder", **{skill.learn_material: amount})) as app:
        body = TestClient(app, raise_server_exceptions=False).get("/goals/holder").json()

    requirement = next(r for g in body["goals"]
                       for r in g["requirements"]
                       if r["id"] == skill.learn_material)
    assert requirement["owned"] == amount
    assert requirement["satisfied"] is True


def test_owned_zero_when_the_inventory_holds_nothing(goal_client):
    body = goal_client.get("/goals/probe_player").json()
    requirement = body["goals"][0]["requirements"][0]
    assert requirement["owned"] == 0
    assert requirement["satisfied"] is False


def test_satisfied_is_exactly_owned_at_least_required():
    """The flag is a comparison, so it holds on both sides of the boundary and
    at equality. ``amount`` and ``amount - 1`` pin both sides of the gate."""
    skill = SKILLS[next(iter(SKILLS))]
    amount = skill.learn_material_amount
    for held, expected in ((amount, True), (amount - 1, False), (0, False)):
        player = f"p_{held}"
        with bare_app({player: player_row(player)},
                      owned_inventory(player, **{skill.learn_material: held})) as app:
            body = TestClient(app, raise_server_exceptions=False).get(
                f"/goals/{player}").json()
        requirement = next(r for g in body["goals"]
                           for r in g["requirements"]
                           if r["id"] == skill.learn_material)
        assert requirement["owned"] == held
        assert requirement["satisfied"] is expected


def test_active_is_one_of_the_goals_and_carries_an_action_when_one_is_ready():
    """The panel's one highlighted goal, for a player who can act.

    ``active`` must be a member of ``goals`` -- not a copy -- or a client that
    cross-references the two lists cannot find the row it is drawing. The
    baseline player owns nothing (see the blocked test below), so this arm gives
    them the first skill's real material at its real amount.
    """
    skill = SKILLS[next(iter(SKILLS))]
    player = "ready_player"
    with bare_app({player: player_row(player)},
                  owned_inventory(player,
                                  **{skill.learn_material: skill.learn_material_amount})) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()

    assert body["active"] in body["goals"]
    ready = [g for g in body["goals"] if g["eligible_action"]]
    assert ready, "a mortal player holding the first skill's material has one ready goal"
    assert body["active"]["goal_id"] == ready[0]["goal_id"]
    assert body["active"]["eligible_action"] == body["active"]["goal_id"], (
        "an advertised action must be the goal's own id, which is what the "
        "inner-skill routes accept")


def test_a_fully_blocked_player_still_gets_an_explanation_not_an_empty_panel():
    """No goal is legal, yet ``active`` is non-null and blocked is complete.

    The panel exists to answer "why is this locked". A null ``active`` here
    would leave a brand-new player staring at an empty box.
    """
    player = "blocked_player"
    with bare_app({player: player_row(player, realm="mortal")}, FakeInventory()) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()

    assert body["active"] is not None
    assert body["active"]["eligible_action"] == "", (
        "no requirement is met and no skill is learned, so no action is legal")
    assert body["blocked"], "a blocked player must be shown what is blocking them"


def test_blocked_is_exactly_the_goals_with_no_eligible_action(goal_client):
    """The three lists partition. A goal missing from ``blocked`` is a goal the
    panel says is locked but does not explain."""
    body = goal_client.get("/goals/probe_player").json()
    every_id = [g["goal_id"] for g in body["goals"]]
    blocked_ids = [g["goal_id"] for g in body["blocked"]]
    assert blocked_ids == [gid for gid, g in zip(every_id, body["goals"])
                           if not g["eligible_action"]]
    # The partition is disjoint and total: every blocked row is a real row, and
    # a blocked row cannot appear twice.
    assert set(blocked_ids) <= set(every_id)
    assert len(blocked_ids) == len(set(blocked_ids))


def test_a_finished_skill_is_reported_completed_and_is_not_advertised():
    """``state: active`` means the skill is done forever.

    Pinned because the interesting half is that such a goal still appears in
    ``blocked``: it has no eligible action, so the panel explains it rather than
    hiding it, and a client counting blocked goals as failures would be wrong.
    """
    player = "finished_player"
    row = player_row(player)
    row["inner_skills"] = {"skills": {"qi_sense": {"state": "active"}}}
    with bare_app({player: row}, owned_inventory(player, insight_scroll=99)) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()

    finished = next(g for g in body["goals"]
                    if g["goal_id"] == "inner_skill_tupo:qi_sense")
    assert finished["completed"] is True
    assert finished["eligible_action"] == ""
    assert finished in body["blocked"]
    assert body["active"] == body["blocked"][0], (
        "with nothing ready, active falls back to the first blocked goal")


@pytest.mark.parametrize("stored, slug", [
    ("mortal", "mortal"),
    ("Mortal", "mortal"),
    (" qi_refining ", "qi_refining"),
    ("Foundation Establishment", "foundation_establishment"),
])
def test_a_realm_written_either_way_opens_the_same_gate(stored, slug):
    """Production writes both spellings -- "Foundation Establishment" and
    "foundation_establishment". An exact lookup misses the spaced form, the
    gate falls through to False, and a Foundation player is told to reach a
    realm they are already in.

    The expectation is the REAL ladder read at run time: a gate is complete when
    the stored realm sits at or above the gate's realm on it. Case and
    surrounding whitespace must not change the answer.
    """
    from inner_skills import REALM_ORDER

    player = "realm_player"
    with bare_app({player: player_row(player, realm=stored)},
                  owned_inventory(player, insight_scroll=99)) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()

    observed = {step["id"]: step["completed"] for goal in body["goals"]
                for step in goal["prerequisite_steps"]
                if step["id"].startswith("realm:")}
    assert observed, "every goal reports the realm gate it sits behind"
    # The expectation is the REAL ladder read at run time: a gate is complete when
    # the stored realm sits at or above the gate's realm on it. Case and
    # surrounding whitespace must not change the answer.
    #
    # Only rungs some skill actually gates on appear as steps. Asserting the
    # whole ladder would demand a step for rungs no goal sits behind -- an
    # over-specified test, not a product defect.
    gated = {f"realm:{d.required_realm}" for d in SKILLS.values()}
    assert set(observed) == gated, (
        "one realm step per gated rung, and no step for an ungated rung")
    expected = {
        gid: REALM_ORDER.index(slug) >= REALM_ORDER.index(gid.split(":", 1)[1])
        for gid in observed
    }
    assert observed == expected, (
        f"stored realm {stored!r} should normalise to {slug!r} on the ladder")


def test_a_met_cost_does_not_unlock_a_goal_whose_realm_gate_is_unmet():
    """Cost AND gate, or nothing. The one arm a fully-blocked player never hits.

    Every other test here either satisfies a cost or fails it; this one pays the
    material and still fails the realm gate, which is the combination a
    cost-only check advertises as ready. It matters because the panel highlights
    the action it names, and the real inner-skill route refuses a player below
    the skill's realm -- so a cost-only check ships a button that collects a
    refusal from the very thing it highlighted.

    Chosen by reading the table: a skill whose required realm is above the
    bottom rung, so a bottom-rung player can meet its cost and not its gate.
    """
    skill_id, definition = next(
        (sid, d) for sid, d in SKILLS.items() if d.required_realm != "mortal")
    from inner_skills import REALM_ORDER

    bottom = REALM_ORDER[0]
    assert definition.required_realm != bottom, "this test needs a gated skill"
    player = "paid_but_gated"
    row = player_row(player, realm=bottom)
    with bare_app({player: row},
                  owned_inventory(player,
                                  **{definition.learn_material:
                                     definition.learn_material_amount})) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()

    goal = next(g for g in body["goals"]
                if g["goal_id"] == f"inner_skill_learn:{skill_id}")
    requirement = goal["requirements"][0]
    assert requirement["satisfied"] is True, (
        "precondition: the cost IS met, so only the realm gate can block this")
    gate = next(s for s in goal["prerequisite_steps"]
                if s["id"] == f"realm:{definition.required_realm}")
    assert gate["completed"] is False, "precondition: the realm gate is unmet"
    assert goal["eligible_action"] == "", (
        "a met cost does not override an unmet realm gate")
    assert goal in body["blocked"]


def test_an_unknown_realm_name_is_read_as_the_bottom_rung_not_a_crash():
    """``_realm_index`` returns -1 for a name the ladder does not carry, so
    every gate reads as unmet and the player is told how to climb from the
    start. A bare ``list.index`` would have taken the whole reply down."""
    player = "odd_realm"
    with bare_app({player: player_row(player, realm="a_realm_that_does_not_exist")},
                  FakeInventory()) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()
    assert set(body) == VIEW_KEYS
    mortal_step = next(s for g in body["goals"] for s in g["prerequisite_steps"]
                       if s["id"] == "realm:mortal")
    assert mortal_step["completed"] is True


def test_the_ladder_lookup_is_total_for_a_name_it_does_not_carry():
    """Direct call, on purpose: this arm is UNREACHABLE through HTTP.

    ``_resolve_realm`` normalises whatever the player row holds onto a ladder
    member or onto ``mortal``, so by the time ``_realm_index`` sees a name it is
    always one the ladder carries -- no request can drive it onto the ValueError
    arm. Covered here as a standalone function call because that is the only
    honest way to reach it, and it is the branch that keeps an unexpected realm
    string from taking the whole panel down.
    """
    from inner_skills import REALM_ORDER

    assert goal_graph._realm_index("a_realm_that_does_not_exist") == -1
    for position, realm in enumerate(REALM_ORDER):
        assert goal_graph._realm_index(realm) == position
    assert all(goal_graph._realm_index(realm) >= 0 for realm in REALM_ORDER)


# ===========================================================================
# 4. THE ITEM REPLY
# ===========================================================================


def test_item_reply_echoes_the_requested_id_and_its_keys(goal_client):
    body = goal_client.get("/goals/probe_player/item/insight_scroll").json()
    assert set(body) == ITEM_REPLY_KEYS
    assert body["item_id"] == "insight_scroll"
    for use in body["uses"]:
        assert set(use) == USE_KEYS


def test_item_uses_amounts_come_from_the_real_skill_table(goal_client):
    """An item can pay ONE of the two goals a skill has: learning it, or
    breaking through. Both amounts come from ``SKILLS``, and the two differ --
    which is why this compares against the table rather than a pair of literals.

    WHICH of the two the reply may name is the skill's stage, read through the
    same ``_stage`` / ``_STAGE_VERB`` pair the route and the goal list use, so
    this asks the table which half to expect instead of pinning a literal.
    Measured across every rung: a row with no stored skill reads ``unlearned``
    (verb ``learn``), and every stored row at or past the comprehension wait
    reports verb ``tupo``. Exactly one half is ever named and the other is
    ABSENT -- a reply that advertised both sent the panel chasing a goal id the
    player's own goals reply does not contain, which is the defect the route
    fix threads the player row to prevent.

    ``goal_client`` holds a fresh player, whose only stage is ``unlearned``, so
    on its own this would pin the learn amount and leave the tupo half
    unreachable. The second half below runs a player whose stored row really is
    at the breakthrough, so BOTH amounts stay checked. One arm would have left
    the tupo amount asserted nowhere.
    """
    skill_id = next(iter(SKILLS))
    definition = SKILLS[skill_id]

    # The stored row that puts this skill PAST the breakthrough, so the tupo
    # half is reachable at all. "comprehend_complete" with level 1 is what the
    # real route accepts to begin a breakthrough, and it needs no elapsed timer,
    # so the stage cannot slip between the route read and the assertion read.
    at_breakthrough = player_row("probe_player")
    at_breakthrough["inner_skills"] = {
        "skills": {skill_id: {"state": "comprehend_complete", "level": 1}}
    }
    broke = goal_graph._stage(
        goal_graph._stored_skills(at_breakthrough)[skill_id], definition, time.time())
    assert goal_graph._STAGE_VERB[broke] == "tupo", (
        f"the row built for this test reads stage {broke}, so it would never "
        "reach the tupo arm")

    for probe, label, uses in (
            (player_row("probe_player"), "fresh player",
             goal_client.get("/goals/probe_player/item/insight_scroll").json()["uses"]),
            (at_breakthrough, "player at the breakthrough",
             _uses_for(at_breakthrough, "insight_scroll"))):
        stored = goal_graph._stored_skills(probe)
        stage = goal_graph._stage(stored.get(skill_id), definition, time.time())
        verb = goal_graph._STAGE_VERB[stage]

        by_goal = {use["goal_id"]: use for use in uses}
        learn = by_goal.get(f"inner_skill_learn:{skill_id}")
        tupo = by_goal.get(f"inner_skill_tupo:{skill_id}")
        if definition.learn_material == "insight_scroll":
            if verb == "learn":
                assert learn is not None, (
                    f"{label}: insight_scroll is a real learn material")
                assert learn["required"] == definition.learn_material_amount
            else:
                assert learn is None, (
                    f"{label}: stage {stage} reports verb {verb}, so the player "
                    "has no learn goal for this item reply to point at")
        if definition.tupo_material == "insight_scroll":
            if verb == "tupo":
                assert tupo is not None, (
                    f"{label}: insight_scroll is a real tupo material")
                assert tupo["required"] == definition.tupo_material_amount
            else:
                assert tupo is None, (
                    f"{label}: stage {stage} reports verb {verb}, so the player "
                    "has no breakthrough goal for this item reply to point at")


def _uses_for(row: dict, item_id: str) -> list:
    """The item reply a given player row actually gets, through the real route.

    Built on its own app because ``goal_client`` is fixed to the fresh player;
    this is what lets the tupo half be observed at all.
    """
    player_id = row["id"]
    with bare_app({player_id: row}, FakeInventory()) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player_id}/item/{item_id}").json()
    return body["uses"]


def test_item_uses_reports_every_real_consumer_of_that_item(goal_client):
    """Every consumer named is one the reply filtered to, derived from
    ``item_uses`` so a table change moves this test, not breaks it.

    The expected side is the two-argument call because that is what the route
    makes: ``item_uses(item, player)`` with the row it already fetched. The
    one-argument call answers a different question -- "what could this item ever
    be for" -- and on a fresh player it named a breakthrough goal id that
    existed nowhere in the goals reply the same client had just been given, so a
    panel following that id followed it into nothing.

    The second assertion is the property, not the implementation: every id this
    reply names is an id the player's OWN goals reply contains. That holds for
    the two-argument call and fails for the one-argument one, so this test stays
    red if the row is ever dropped again.
    """
    item = SKILLS[next(iter(SKILLS))].learn_material
    body = goal_client.get(f"/goals/probe_player/item/{item}").json()
    probe = player_row("probe_player")

    assert [u["goal_id"] for u in body["uses"]] == \
           [u["goal_id"] for u in item_uses(item, probe)]

    goal_ids = {g["goal_id"] for g in goal_graph.goal_view(probe, lambda _i: 0)["goals"]}
    named = {u["goal_id"] for u in body["uses"]}
    assert named <= goal_ids, (
        f"item reply names {sorted(named - goal_ids)}, which the player's own "
        f"goals reply does not contain (it carries {sorted(goal_ids)})")


def test_a_real_item_with_no_consumer_answers_200_and_an_empty_list(goal_client):
    """18 of the 22 catalog items are consumed by no inner skill.

    An empty list is the honest answer for those -- the item is not useless, it
    is just not a goal input -- so they answer 200 with ``uses: []`` rather than
    a 404.
    """
    unused = [item for item in _catalog() if not item_uses(item)]
    assert unused, "the catalog should contain items no goal consumes"
    response = goal_client.get(f"/goals/probe_player/item/{unused[0]}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ITEM_REPLY_KEYS
    assert body == {"item_id": unused[0], "uses": []}


def test_an_item_the_catalog_has_never_heard_of_is_404(goal_client):
    """Distinct from the row above, and deliberately so.

    The module's own docstring records why: an empty list alone cannot mean
    "unknown", so only an id the shared catalog does not carry is treated as a
    client bug. Answering 200/[] there told the panel the item was useless
    rather than that the request was wrong.
    """
    unknown = "an_item_id_that_is_in_no_catalog"
    assert unknown not in _catalog()
    assert not item_uses(unknown)
    response = goal_client.get(f"/goals/probe_player/item/{unknown}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}


def test_the_item_route_never_reads_the_inventory():
    """Item uses are a property of the catalog, not of a player.

    The route must not price an item, so a broken inventory cannot turn this
    read into a 503. Asserted by watching the double's call log, not by
    breaking the store.
    """
    inventory = FakeInventory(raises=RuntimeError("store is down"))
    with bare_app({"probe_player": player_row("probe_player")}, inventory) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player/item/insight_scroll")
    assert response.status_code == 200
    assert inventory.calls == []


# ===========================================================================
# 5. ERROR CASES
# ===========================================================================


def test_an_unknown_player_is_404_on_the_view():
    response = client_for({}, FakeInventory()).get("/goals/nobody_at_all")
    assert response.status_code == 404
    assert response.json() == {"detail": "Player not found"}


def test_an_unknown_player_is_404_on_the_item_route():
    """The player check runs FIRST, so an unknown player under a valid item
    reports the player and not the item."""
    response = client_for({}, FakeInventory()).get("/goals/nobody_at_all/item/insight_scroll")
    assert response.status_code == 404
    assert response.json() == {"detail": "Player not found"}


def test_a_broken_inventory_is_503_naming_the_item_it_could_not_price():
    """An unreadable inventory is not the same answer as an empty one.

    Zero is a real answer -- "you hold none" -- so a backend failure answered
    the same way sent a player who owned every item off to earn them again.
    """
    inventory = FakeInventory(raises=RuntimeError("inventory backend is down"))
    with bare_app({"probe_player": player_row("probe_player")}, inventory) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Inventory unavailable; cannot price insight_scroll"}


def test_a_store_that_raises_http_exception_passes_its_own_answer_through():
    """A deliberate status from the store is not a broken store.

    Catching it here rewrote a real refusal into a 503: a store answering
    HTTPException(418) came out of this route as 503 "Inventory unavailable".
    A teapot is a silly probe; the point is that the status survives.
    """
    inventory = FakeInventory(raises=HTTPException(status_code=418, detail="teapot"))
    with bare_app({"probe_player": player_row("probe_player")}, inventory) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player")
    assert response.status_code == 418
    assert response.json() == {"detail": "teapot"}


@pytest.mark.parametrize("value, expected", [("7", 7), (3.9, 3), (3.0, 3)])
def test_the_owned_count_is_coerced_to_an_int(value, expected):
    """``int()`` on the store's answer is what keeps a JSON store from putting a
    float in the panel. Both a numeric string and a float reach the client as an
    int, so a client can compare without a cast."""
    skill = SKILLS[next(iter(SKILLS))]
    player = "coerce_player"
    inventory = FakeInventory(raw=value)
    with bare_app({player: player_row(player)}, inventory) as app:
        body = TestClient(app, raise_server_exceptions=False).get(
            f"/goals/{player}").json()
    requirement = next(r for g in body["goals"] for r in g["requirements"]
                       if r["id"] == skill.learn_material)
    assert requirement["owned"] == expected
    assert isinstance(requirement["owned"], int)


def test_a_count_the_store_cannot_convert_is_503_not_a_broken_panel():
    """``int(None)`` raises, so an unusable count is a backend failure, not an
    answer. The distinction matters for the same reason 503 does: a silent 0
    here would read as "you own none"."""
    inventory = FakeInventory(raw=None)
    with bare_app({"probe_player": player_row("probe_player")}, inventory) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player")
    assert response.status_code == 503


def test_an_inventory_provider_that_RESOLVES_TO_nothing_is_503():
    """A provider that returns ``None`` is an unreadable store, and must not be
    laundered into a count of zero.

    Written in caps in the name on purpose: passing ``None`` AS THE PROVIDER is
    a different thing entirely. The module documents ``None`` as "read the live
    main store", so that path attempts an import and its status depends on
    whether ``main`` loads -- 500 in this tree, where a peer's in-flight edit to
    ``lifecycle_guard.py`` breaks the import. The next test pins that reading;
    this one pins the broken-store reading. Conflating them would make this
    assertion pass for the wrong reason.
    """
    with bare_app({"probe_player": player_row("probe_player")},
                  lambda: None) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player")
    assert response.status_code == 503


def test_a_provider_of_none_means_read_the_live_store_not_a_broken_one():
    """The documented meaning of a ``None`` provider, pinned so it cannot be
    mistaken for the broken-store arm above.

    ``register_goal_routes`` resolves its providers per request precisely so
    ``None`` can mean "read live main": main.py builds its singletons while this
    module is still being imported, so capturing by value at import time would
    bind ``None``.

    The STATUS is deliberately not asserted. With a ``None`` provider this route
    reads ``main.inventory_store``, so the answer depends on whether ``main``
    imports: it answers 200 when ``main`` loads, and a server error when it does
    not. A peer editing ``main.py``'s import graph changes it, which is a fact
    about that peer and not a contract of this module. What IS pinned is the
    distinction from the row above -- a ``None`` provider is a request to read
    the live store, never a broken-store answer, so it never produces the
    "Inventory unavailable" refusal that an unreadable store does.
    """
    with bare_app({"probe_player": player_row("probe_player")}, None) as app:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/goals/probe_player")

    assert "Inventory unavailable" not in response.text, (
        "a None provider means 'read the live main store'; it must not be "
        "answered as a broken store")
    # An injected player row still has to be the one the route found, or the
    # request never reached the store lookup at all.
    assert response.status_code in (200, 500), (
        f"unexpected status for a live-store read: {response.status_code} "
        f"{response.text[:200]}")


# ===========================================================================
# 6. STORE INJECTION: the module's own load-bearing promises
# ===========================================================================


def test_both_providers_are_honoured_and_may_be_values_or_callables():
    """Both call styles reach the handlers. The module documents
    ``provider() if callable(provider) else provider`` twice.

    Item counts come from the INVENTORY provider, not from main. They used to be
    read from ``main.inventory_store`` unconditionally, so an injected player
    store was paired with main's inventory and every requirement reported
    ``owned=0`` -- the row said the player exists while the counts said they own
    nothing.
    """
    skill = SKILLS[next(iter(SKILLS))]
    amount = skill.learn_material_amount
    players = {"value_player": player_row("value_player")}

    for label, players_arg, inventory_arg in (
        ("values", players, owned_inventory("value_player",
                                           **{skill.learn_material: amount})),
        ("callables", lambda: players,
         lambda: owned_inventory("value_player",
                                 **{skill.learn_material: amount})),
    ):
        with bare_app(players_arg, inventory_arg) as app:
            body = TestClient(app, raise_server_exceptions=False).get(
                "/goals/value_player").json()
        requirement = next(r for g in body["goals"]
                           for r in g["requirements"]
                           if r["id"] == skill.learn_material)
        assert requirement["owned"] == amount, label
        assert requirement["satisfied"] is True, label


def test_a_later_registration_cannot_rewire_an_app_already_mounted():
    """Each call mounts ITS OWN router over the providers IT was handed.

    The stores used to live in module globals the handlers read per request, so
    the last registration won for every app including apps already serving.
    Measured on this module: register B, register C, and an app mounted with B
    started answering 404 "Player not found" for a player who was in B.
    """
    players_a = {"player_a": player_row("player_a")}
    inventory_a = owned_inventory("player_a", insight_scroll=9)

    app_a = FastAPI()
    goal_routes.register_goal_routes(app_a, lambda: players_a, lambda: inventory_a)
    client_a = TestClient(app_a, raise_server_exceptions=False)

    players_b = {"player_b": player_row("player_b")}
    app_b = FastAPI()
    goal_routes.register_goal_routes(app_b, lambda: players_b,
                                     owned_inventory("player_b", insight_scroll=9))

    response = client_a.get("/goals/player_a")
    assert response.status_code == 200, (
        "a registration on an unrelated app rewired an app already mounted")
    requirement = response.json()["goals"][0]["requirements"][0]
    assert requirement["owned"] == 9, "app A's own inventory must answer"
    assert all(pid == "player_a" for pid, _ in inventory_a.calls), (
        "app A's inventory must never be asked about another app's player")
    assert client_a.get("/goals/player_b").status_code == 404


def test_register_restores_the_legacy_globals_so_no_fake_store_leaks():
    """``register_goal_routes`` rebinds two module globals on purpose -- the
    legacy module-level router reads them per request. A test that registers and
    does not restore them hands its fake store to the next test in the session,
    so the fixture that owns the registration owns the restore."""
    saved_players = goal_routes._injected_players_db
    saved_inventory = goal_routes._injected_inventory_db
    try:
        with bare_app({"leaky": player_row("leaky")}, FakeInventory()) as _app:
            assert goal_routes._injected_players_db is not None
            assert goal_routes._injected_inventory_db is not None
    finally:
        # bare_app's finally already restored these; assert it rather than
        # restoring again, so a future change to the fixture is caught.
        pass
    assert goal_routes._injected_players_db is saved_players
    assert goal_routes._injected_inventory_db is saved_inventory


def test_the_legacy_module_level_router_exposes_the_same_two_paths():
    """``goal_routes.router`` stays importable and unmounted. Nothing in the
    tree mounts it -- main.py calls ``register_goal_routes`` -- so it is not a
    route this file can drive, only a shape to pin."""
    assert {r.path for r in goal_routes.router.routes} == {GOALS, ITEM}


# ===========================================================================
# 7. READ-ONLY: the module never spends, grants, or mutates
# ===========================================================================


def test_answering_the_view_mutates_nothing():
    """The module is documented read-only. Asserted on both stores' own state,
    not on an absence of an exception."""
    players = {"quiet_player": player_row("quiet_player")}
    players["quiet_player"]["inner_skills"] = {"skills": {
        "qi_sense": {"state": "comprehending", "started_at": time.time()}}}
    inventory = owned_inventory("quiet_player", insight_scroll=9)
    row_before = copy.deepcopy(players["quiet_player"])
    held_before = copy.deepcopy(inventory.held)

    with bare_app(players, inventory) as app:
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/goals/quiet_player").status_code == 200
        assert client.get("/goals/quiet_player/item/insight_scroll").status_code == 200

    assert players["quiet_player"] == row_before
    assert inventory.held == held_before


def test_the_inventory_is_read_once_per_requirement():
    """The route calls the inventory exactly once per requirement it prices.

    Derived from ``goal_graph`` itself at run time, so a skill table change
    moves this number instead of breaking it. A cache that read nothing, or a
    second read of an id already priced, both show up here.
    """
    inventory = owned_inventory("probe_player", insight_scroll=9)
    with bare_app({"probe_player": player_row("probe_player")}, inventory) as app:
        TestClient(app, raise_server_exceptions=False).get("/goals/probe_player")

    expected = sum(len(goal["requirements"]) for goal in
                   goal_graph.inner_skill_goals(player_row("probe_player"),
                                                 lambda item_id: 0))
    assert len(inventory.calls) == expected
    assert all(pid == "probe_player" for pid, _ in inventory.calls)
    # One read per requirement, and no read at all for anything the goals do not
    # price. The item id read first must be the first goal's material, so a
    # route that stopped consulting the store for that goal cannot hide behind a
    # coincidentally equal total.
    assert inventory.calls[0][1] == SKILLS[next(iter(SKILLS))].learn_material
    assert {item for _, item in inventory.calls} == \
           {requirement["id"] for goal in goal_graph.inner_skill_goals(
               player_row("probe_player"), lambda item_id: 0)
            for requirement in goal["requirements"]}