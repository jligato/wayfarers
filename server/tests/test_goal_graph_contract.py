"""
CONTRACT TEST: server/goal_graph.py, over the HTTP surface that serves it.

WHAT goal_graph.py ACTUALLY IS
------------------------------
``goal_graph.py`` registers NO routes. It is a read-only library: one function
builds the goal list (``inner_skill_goals``), one picks the goal to show
(``goal_view``), one answers "what is this item for" (``item_uses``). So "the
route contract" of this module is the contract of the two GET routes that call
it, both mounted by ``goal_routes.register_goal_routes``:

    GET /goals/{player_id}
    GET /goals/{player_id}/item/{item_id}

Measured route table of ``register_goal_routes`` (see _ROUTE_TABLE below):
those two paths, plus the mission-board and achievement routes that the same
function mounts as a side effect. A test that asserts "exactly two routes" would
be asserting a lie.

WHAT THIS FILE PINS
-------------------
* the reply KEY SETS of every object on the wire (a client reads these names, so
  renaming one breaks every caller; adding one is safe),
* AUTH: anonymous refused, another player's session refused, owner served,
* the three distinct refusal statuses, and the inventory-failure split,
* relationships, never literals: every cost is read out of ``inner_skills.SKILLS``
  and ``items.json`` at run time, so rebalancing the economy leaves this green,
* READ-ONLY: neither route may mutate the player row,
* the module's own docstring promise -- an advertised action is one the real
  ``inner_skills`` route would ACCEPT.

WHY A BARE APP AND NOT server.main
----------------------------------
``server/main.py`` is 1300+ lines and several agents edit it at once, so a test
that imports it measures whatever a peer is mid-write. The repo-root conftest
imports it too, which is a separate, known trap (see
``wayfarers-server-tests-dir-and-pg-env-recipe`` Trap 5). This file mounts its
own app.

The auth arm cannot use a bare app alone: ``install_ownership_guard`` is what
puts auth on these routes, and in production it runs at server STARTUP
(``main.py`` calls it after every router is registered). So the app-under-test
here installs the same guard, in the same way, and the auth tests are therefore
a real three-arm proof (refuse anonymous / refuse cross-owner / serve owner)
rather than an assumption.

Run
---
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD \\
        -u WC_PG_PORT -u PYTHONPATH python -m pytest \\
        server/tests/test_goal_graph_contract.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, Iterator, List, Tuple

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# In-memory storage must be forced, or this file hangs to the runner timeout
# instead of failing. See the module docstring of
# wayfarers-server-tests-dir-and-pg-env-recipe.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# BARE imports, never ``from server import ...``. Two module objects mean two
# module-level singletons; ``main.py`` binds the bare ``auth`` object, so a
# token minted through ``server.auth`` lands in a store nothing reads and every
# request comes back 401 -- a false red that looks like a product defect.
import auth  # noqa: E402
import goal_graph as gg  # noqa: E402
import goal_routes as gr  # noqa: E402
import inner_skills as isk  # noqa: E402
from ownership_guard import install_ownership_guard  # noqa: E402


# ---------------------------------------------------------------------------
# The paths, named once.
# ---------------------------------------------------------------------------
GOALS = "/goals/{player_id}"
ITEM_USES = "/goals/{player_id}/item/{item_id}"

#: Both, and only these, are the goal graph's own routes. The mission-board and
#: achievement routes mounted by the same function are somebody else's contract.
_ROUTE_TABLE = {GOALS: {"GET"}, ITEM_USES: {"GET"}}

#: Every key a client reads off a goal. Frozen, because a rename breaks callers.
GOAL_KEYS = {"goal_id", "scope", "definition_version", "requirements",
             "prerequisite_steps", "eligible_action", "completed"}

#: Every key a client reads off one requirement.
REQUIREMENT_KEYS = {"id", "label", "required", "owned", "unit", "satisfied",
                    "source_ids"}

#: Keys every prerequisite step carries. Timer steps add ``remaining_seconds``.
STEP_KEYS = {"id", "label", "completed"}

#: Keys of one entry in the item-uses list.
USE_KEYS = {"goal_id", "label", "required"}

#: Top-level keys of the goals reply.
VIEW_KEYS = {"goals", "active", "blocked"}

#: Every real skill id. Used to tell a goal id that names a SKILL apart from
#: one of the module's own step ids, which the reply also carries.
SKILL_IDS = tuple(isk.SKILLS)


def _goal_material(goal_id: str) -> str:
    """The item a goal id charges, read out of the real skill table.

    Derived rather than typed, so the item-uses filter control keeps working
    when the economy is rebalanced or a skill's material is renamed. Raises for
    an id that is not a skill goal, which is what makes it a real check.
    """
    verb, skill_id = goal_id.rsplit(":", 1)
    definition = isk.SKILLS[skill_id]
    if verb == "inner_skill_learn":
        return definition.learn_material
    if verb == "inner_skill_tupo":
        return definition.tupo_material
    raise KeyError(goal_id)

#: One frozen clock for every timing assertion in this file. A wall-clock read
#: inside a test makes a boundary check flaky by construction: the elapsed
#: second is different on the next run. Frozen once, at import, so a test that
#: asserts "exactly at the duration" means it.
_NOW = 1_700_000_000.0


def auth_headers(player_id: str) -> Dict[str, str]:
    """Mint a session in-process against the store the app actually reads."""
    return {"Authorization": "Bearer " + auth.issue_session(player_id)}


class FakeInventory:
    """An inventory that can also be told to fail.

    ``raises`` exists because the two failure kinds are different answers:
    a deliberate ``HTTPException`` from the store is the store's own reply and
    must reach the caller untouched, while an unexpected exception means "the
    counts are unknown" and must NOT be laundered into a count of zero.
    """

    def __init__(self, held: Dict[str, Dict[str, int]] | None = None,
                 raises: BaseException | None = None) -> None:
        self.held = held or {}
        self.raises = raises

    def get_item_count(self, player_id: str, item_id: str) -> int:
        if self.raises is not None:
            raise self.raises
        return self.held.get(player_id, {}).get(item_id, 0)


def player_row(player_id: str, realm: str = "mortal",
               skills: Dict[str, Any] | None = None,
               inner_skills: Any = "__unset__") -> Dict[str, Any]:
    """A row shaped like the ones production writes.

    ``inner_skills`` defaults to a clean block. Pass ``__unset__``-free values
    deliberately: a corrupt block is its own test.
    """
    row = {
        "id": player_id, "name": player_id, "cultivation_realm": realm,
        "cultivation_stage": "early", "path": "qi", "spirit_root": "fire",
        "progress": 0.0, "spirit_stones": {"low_grade": 1000},
        "atk": 100, "def_": 50, "hp": 1000, "mp": 500, "spd": 100,
        "pvp_rating": 1000, "sect_id": None, "sect_rank": "",
        "sect_contribution": 0, "dao_companion_id": None,
    }
    row["inner_skills"] = ({"version": 1, "es_points": 0, "skills": skills or {},
                             "receipts": {}, "receipt_order": []}
                           if inner_skills == "__unset__" else inner_skills)
    return row


@pytest.fixture()
def players() -> Dict[str, Dict[str, Any]]:
    return {"p1": player_row("p1")}


@pytest.fixture()
def guarded_app(players) -> Iterator[Any]:
    """The two goal routes, with the ownership guard installed as in production.

    The guard is what makes these routes require a session, so an auth test that
    skipped it would prove nothing. It is installed HERE, on this app only.
    """
    app = FastAPI()
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    # register_goal_routes sets two module globals for the legacy router; they
    # are restored so one test's store cannot leak into the next test.
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    install_ownership_guard(app)
    try:
        yield app
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved


@pytest.fixture()
def bare_app(players) -> Iterator[Any]:
    """The same routes with NO guard, for the shape/refusal arms that do not
    need one. Mounting the guard would turn every 404 here into a 401 and the
    route's own answer would never be observed."""
    app = FastAPI()
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        yield app
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved


# ===========================================================================
# 1. WHAT MOUNTS
# ===========================================================================

def test_goal_routes_mount_exactly_the_two_declared_paths(guarded_app):
    """The goal graph's own surface is two GET routes, no writes.

    ``register_goal_routes`` ALSO mounts the mission board and achievements, so
    this filters to the ``/goals`` prefix rather than counting the whole table.
    FastAPI adds HEAD to every GET, hence ``methods >= {"GET"}``.
    """
    mine = {r.path: set(r.methods) for r in guarded_app.routes
            if getattr(r, "path", "").startswith("/goals")}
    assert mine == _ROUTE_TABLE, mine
    for path, methods in mine.items():
        assert "GET" in methods, (path, methods)
        # Read-only module: no write verb may appear on either path.
        assert not (methods - {"GET", "HEAD"}), (path, methods)


def test_the_goal_routes_are_registered_by_the_live_application():
    """``server/main.py`` must actually call the registration.

    An importable module is not a mounted route. This reads the live app's route
    table rather than the module, because the claim is "the server serves this",
    not "the file defines it".

    SKIP, not pass, when the live app cannot be imported. This tree is shared and
    several agents edit it at once; a half-written sibling module makes
    ``import server.main`` raise, and a bare ``except ImportError: return`` would
    turn that into a green that proves nothing. It skips ONLY when the broken
    import is OUTSIDE the goal chain -- if ``goal_routes`` itself were the
    casualty this must FAIL, because that is this test's actual subject.
    """
    try:
        import server.main as main_mod
    except ImportError as exc:
        offender = getattr(exc, "name", "") or str(exc)
        assert "goal" not in offender, (
            f"the goal route module itself failed to import: {offender}")
        pytest.skip(f"live app not importable right now (peer mid-write): {offender}")

    paths = {getattr(r, "path", "") for r in main_mod.app.routes}
    assert GOALS in paths, "GET /goals/{player_id} is not in the live app"
    assert ITEM_USES in paths, "GET /goals/{player_id}/item/{item_id} is not live"


# ===========================================================================
# 2. AUTH -- three arms, because one arm is a tautology
# ===========================================================================

def test_the_ownership_guard_classifies_both_goal_routes(guarded_app):
    """Both paths are GUARDED, not skipped.

    ``install_ownership_guard`` returns the paths it rewrote. If a goal route
    were silently skipped it would still answer 200 for an anonymous caller
    below, and the anonymous test would be measuring nothing.
    """
    from fastapi import FastAPI as _F
    probe = _F()
    gr.register_goal_routes(probe, lambda: {}, lambda: FakeInventory())
    guarded = install_ownership_guard(probe)
    assert set(guarded) >= set(_ROUTE_TABLE), guarded


def test_anonymous_caller_is_refused_on_both_goal_routes(guarded_app):
    """No token, no goals -- for either path."""
    client = TestClient(guarded_app, raise_server_exceptions=False)
    for path in (GOALS.format(player_id="p1"),
                 ITEM_USES.format(player_id="p1", item_id="insight_scroll")):
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text)


def test_another_players_session_is_refused_on_both_goal_routes(guarded_app):
    """A valid session for somebody ELSE must not read this player's goals.

    This is the arm that matters: the token is real and unexpired, so only
    ownership can refuse it. A 401 here would mean the guard never ran.
    """
    client = TestClient(guarded_app, raise_server_exceptions=False)
    headers = auth_headers("someone_else")
    for path in (GOALS.format(player_id="p1"),
                 ITEM_USES.format(player_id="p1", item_id="insight_scroll")):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, (path, response.status_code, response.text)


def test_the_owner_is_served_on_both_goal_routes(guarded_app):
    """The third arm: the guard must not refuse the legitimate caller."""
    client = TestClient(guarded_app, raise_server_exceptions=False)
    headers = auth_headers("p1")
    assert client.get(GOALS.format(player_id="p1"), headers=headers).status_code == 200
    assert client.get(
        ITEM_USES.format(player_id="p1", item_id="insight_scroll"),
        headers=headers).status_code == 200


def test_a_malformed_token_is_refused_rather_than_served(guarded_app):
    """A bearer scheme with no token behind it is a refusal, not a free read."""
    client = TestClient(guarded_app, raise_server_exceptions=False)
    response = client.get(GOALS.format(player_id="p1"),
                          headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401, (response.status_code, response.text)


# ===========================================================================
# 3. THE GOALS REPLY SHAPE
# ===========================================================================

def test_goals_reply_carries_exactly_its_three_top_level_keys(bare_app):
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    assert set(body) == VIEW_KEYS, sorted(body)


def test_every_goal_carries_the_full_goal_key_set(bare_app):
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    assert body["goals"], "a fresh player must still be shown goals"
    for goal in body["goals"]:
        assert set(goal) == GOAL_KEYS, (goal["goal_id"], sorted(set(goal) ^ GOAL_KEYS))


def test_every_requirement_carries_the_full_requirement_key_set(bare_app):
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    for goal in body["goals"]:
        for requirement in goal["requirements"]:
            assert set(requirement) == REQUIREMENT_KEYS, (
                goal["goal_id"], sorted(set(requirement) ^ REQUIREMENT_KEYS))


def test_every_prerequisite_step_carries_at_least_the_base_step_keys(bare_app):
    """Timer steps legitimately add ``remaining_seconds``; the base three are not
    optional, or a client cannot draw the step at all."""
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    for goal in body["goals"]:
        for step in goal["prerequisite_steps"]:
            assert STEP_KEYS <= set(step), (goal["goal_id"], step)
            assert isinstance(step["completed"], bool), step


def test_active_and_blocked_partition_the_goal_list(bare_app):
    """``active`` is one of the goals; ``blocked`` is exactly the rest without an
    action. A panel that highlights a goal it does not list is broken."""
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    goals = body["goals"]
    ids = [g["goal_id"] for g in goals]
    assert body["active"]["goal_id"] in ids, (body["active"]["goal_id"], ids)
    assert len(goals) == len([g for g in goals if g["eligible_action"]]) + len(body["blocked"])
    assert sorted(g["goal_id"] for g in body["blocked"]) == \
        sorted(g["goal_id"] for g in goals if not g["eligible_action"])


def test_an_empty_skill_table_yields_an_empty_but_well_formed_view():
    """The empty case must still carry all three keys, with ``active`` null.

    ``active`` being ABSENT would break every client that reads it; being null
    is the one honest answer when there is nothing to do.
    """
    saved = gg.SKILLS
    try:
        gg.SKILLS = {}
        view = gg.goal_view(player_row("p"), lambda _s: 0, 1000.0)
    finally:
        gg.SKILLS = saved
    assert view == {"goals": [], "active": None, "blocked": []}, view


# ===========================================================================
# 4. THE ITEM-USES REPLY SHAPE
# ===========================================================================

def test_item_uses_reply_names_the_item_and_carries_full_use_entries(bare_app):
    client = TestClient(bare_app, raise_server_exceptions=False)
    item_id = "insight_scroll"
    response = client.get(ITEM_USES.format(player_id="p1", item_id=item_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"item_id", "uses"}, sorted(body)
    assert body["item_id"] == item_id
    assert body["uses"], "a real consumed item must report its consumers"
    for use in body["uses"]:
        assert set(use) == USE_KEYS, sorted(set(use) ^ USE_KEYS)


def test_item_uses_amounts_come_from_the_real_skill_table():
    """Read the amounts out of the table, not out of a literal.

    ``insight_scroll`` is consumed by two different gates at two different
    prices; the reply must carry the same numbers the charge will use.
    """
    uses = gg.item_uses("insight_scroll")
    by_goal = {u["goal_id"]: u for u in uses}
    for skill_id, definition in isk.SKILLS.items():
        if definition.learn_material == "insight_scroll":
            entry = by_goal[f"inner_skill_learn:{skill_id}"]
            assert entry["required"] == definition.learn_material_amount, entry
        if definition.tupo_material == "insight_scroll":
            entry = by_goal[f"inner_skill_tupo:{skill_id}"]
            assert entry["required"] == definition.tupo_material_amount, entry


def test_an_item_the_catalog_knows_but_no_skill_consumes_is_200_with_no_uses(bare_app):
    """An empty list alone cannot mean "unknown item".

    A real catalogue item with no inner-skill consumer legitimately has no uses,
    so answering 404 there would be wrong. Only an id the catalogue has never
    heard of is a client bug -- see the next test.
    """
    catalog = gg._catalog()
    consumer_ids = {d.learn_material for d in isk.SKILLS.values()} | \
                   {d.tupo_material for d in isk.SKILLS.values()}
    quiet = [i for i in sorted(catalog) if i not in consumer_ids]
    assert quiet, "no catalogue item is free of inner-skill consumers"
    client = TestClient(bare_app, raise_server_exceptions=False)
    response = client.get(ITEM_USES.format(player_id="p1", item_id=quiet[0]))
    assert response.status_code == 200, (quiet[0], response.status_code, response.text)
    assert response.json()["uses"] == [], response.text


# ===========================================================================
# 5. ERROR CASES
# ===========================================================================

def test_an_unknown_player_is_404_on_both_routes(bare_app):
    """A valid session for the missing player: the session is real, the row is
    not, so the 404 is the route's OWN answer and not a guard's 401."""
    client = TestClient(bare_app, raise_server_exceptions=False)
    missing = "no_such_player_xyz"
    for path in (GOALS.format(player_id=missing),
                 ITEM_USES.format(player_id=missing, item_id="insight_scroll")):
        response = client.get(path, headers=auth_headers(missing))
        assert response.status_code == 404, (path, response.status_code, response.text)
        assert response.json()["detail"] == "Player not found", response.text


def test_an_unknown_item_id_is_404_not_an_empty_200(bare_app):
    """Telling the client the item is useless is a different claim from telling
    it the request was wrong, so the two must not share a status."""
    client = TestClient(bare_app, raise_server_exceptions=False)
    response = client.get(ITEM_USES.format(player_id="p1", item_id="not_an_item_zzz"))
    assert response.status_code == 404, (response.status_code, response.text)
    assert response.json()["detail"] == "Item not found", response.text


def test_a_failing_inventory_is_503_and_never_a_zero_count(players):
    """Zero is a real answer -- "you hold none" -- so a backend failure
    answered the same way sends a player who owns everything off to earn it."""
    app = FastAPI()
    inventory = FakeInventory(raises=RuntimeError("inventory backend is down"))
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(GOALS.format(player_id="p1"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 503, (response.status_code, response.text)


def test_a_deliberate_status_from_the_inventory_passes_through_untouched(players):
    """A store that answers with its own status is ANSWERING, not failing.

    Rewriting it into a 503 hides a deliberate refusal behind a generic outage,
    so the two must stay distinguishable.
    """
    app = FastAPI()

    def deliberate(_pid: str, _item: str) -> int:
        raise HTTPException(status_code=418, detail="teapot")

    class Raiser:
        get_item_count = staticmethod(deliberate)

    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: Raiser())
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(GOALS.format(player_id="p1"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 418, (response.status_code, response.text)
    assert response.json()["detail"] == "teapot", response.text


def test_an_unresolvable_requirement_raises_rather_than_guessing():
    """The module's design rule: a cost that cannot be resolved is an error, so
    the panel can never promise a route to a goal that does not exist."""
    with pytest.raises(KeyError):
        gg._requirement("not_an_item_zzz", 1, 0, ["shop"])


@pytest.mark.parametrize("required", [0, -3])
def test_a_non_positive_requirement_is_rejected(required: int):
    """A zero or negative cost would read as "satisfied" for every player."""
    with pytest.raises(ValueError):
        gg._requirement("insight_scroll", required, 0, ["shop"])


def test_every_advertised_material_exists_in_the_shared_item_catalogue():
    """A material the catalogue does not define cannot be priced, bought, or
    counted -- so the goal that names it is a lie waiting to be pressed."""
    catalog = gg._catalog()
    for skill_id, definition in isk.SKILLS.items():
        assert definition.learn_material in catalog, \
            (skill_id, "learn_material", definition.learn_material)
        assert definition.tupo_material in catalog, \
            (skill_id, "tupo_material", definition.tupo_material)


# ===========================================================================
# 6. THE MODULE'S OWN PROMISE: costs come from real tables
# ===========================================================================

def test_goal_costs_equal_the_real_skill_table():
    """Every requirement's id and amount is read from ``SKILLS`` at run time."""
    view = gg.goal_view(player_row("p"), lambda _s: 999, time.time())
    for goal in view["goals"]:
        skill_id = goal["goal_id"].split(":", 1)[1]
        definition = isk.SKILLS[skill_id]
        requirement = goal["requirements"][0]
        if "_learn:" in goal["goal_id"]:
            assert requirement["id"] == definition.learn_material, goal["goal_id"]
            assert requirement["required"] == definition.learn_material_amount
        else:
            assert requirement["id"] == definition.tupo_material, goal["goal_id"]
            assert requirement["required"] == definition.tupo_material_amount


@pytest.mark.parametrize("owned", [0, 1, 2, 3, 4, 99])
def test_satisfied_tracks_owned_against_required(owned: int):
    """The relationship, over a spread of counts: ``satisfied`` is exactly
    ``owned >= required``, never a cached or separately-computed flag."""
    view = gg.goal_view(player_row("p"), lambda _s, n=owned: n, time.time())
    seen = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            assert requirement["owned"] == owned
            assert requirement["satisfied"] == (owned >= requirement["required"]), \
                (requirement["id"], owned, requirement["required"])
            seen += 1
    assert seen, "no requirements were produced"


def test_the_owned_count_comes_from_the_inventory_not_from_the_player_row(bare_app):
    """The row says the player exists; the counts say what they hold. Reading
    the wrong store reports every requirement unmet for a player who owns the
    items outright."""
    client = TestClient(bare_app, raise_server_exceptions=False)
    body = client.get(GOALS.format(player_id="p1")).json()
    by_item = {r["id"]: r for g in body["goals"] for r in g["requirements"]}
    assert "insight_scroll" in by_item, sorted(by_item)
    assert by_item["insight_scroll"]["owned"] == 5, by_item["insight_scroll"]
    assert by_item["insight_scroll"]["satisfied"] is True


def test_the_realm_ladder_is_imported_not_retyped():
    """A second copy of the ladder is a second answer to "which realms exist",
    and it diverges the moment a realm is added."""
    assert gg._REALM_ORDER == list(isk.REALM_ORDER), gg._REALM_ORDER


def test_an_unknown_realm_name_does_not_take_the_goal_list_down():
    """The real gate answers an unknown realm with -1. A bare ``list.index``
    would raise and kill every goal for every such player."""
    view = gg.goal_view(player_row("p", realm="not_a_realm"), lambda _s: 0, time.time())
    assert view["goals"], "an unknown realm must not empty the panel"
    realm_steps = [s for g in view["goals"] for s in g["prerequisite_steps"]
                   if s["id"].startswith("realm:")]
    assert realm_steps
    # Falls back to the ladder's own default, so the first rung reads met and
    # every rung above it does not.
    first = realm_steps[0]
    assert first["id"] == f"realm:{isk.REALM_ORDER[0]}"
    assert first["completed"] is True


def test_the_spaced_realm_form_is_normalised():
    """Production writes both "Foundation Establishment" and
    "foundation_establishment". An exact lookup misses the spaced form and tells
    a Foundation player to reach Foundation forever."""
    spaced = gg._resolve_realm("Foundation Establishment")
    assert spaced == "foundation_establishment", spaced
    assert spaced in gg._REALM_ORDER


# ===========================================================================
# 7. THE DOCSTRING PROMISE: an advertised action is one the route ACCEPTS
# ===========================================================================

def _goals_for(skills: Dict[str, Any], owned: int = 999) -> List[Dict[str, Any]]:
    now = time.time()
    return gg.inner_skill_goals(
        player_row("p", skills=skills), lambda _s, n=owned: n, now)


def test_an_unlearned_skill_with_the_cost_met_offers_its_learn_action():
    now = time.time()
    goals = gg.inner_skill_goals(player_row("p"), lambda _s: 999, now)
    unlearned = [g for g in goals if g["goal_id"] == "inner_skill_learn:qi_sense"][0]
    assert unlearned["eligible_action"] == "inner_skill_learn:qi_sense", unlearned
    assert unlearned["completed"] is False


def test_a_running_comprehension_offers_no_action_at_all():
    """``begin_tupo`` refuses a ``comprehending`` row with HTTP 409. Advertising
    the breakthrough here would ship a highlighted button that always fails."""
    now = time.time()
    skills = {"qi_sense": {"state": "comprehending", "started_at": now}}
    goal = _goals_for(skills)[0]
    assert goal["goal_id"] == "inner_skill_learn:qi_sense", goal["goal_id"]
    assert goal["eligible_action"] == "", goal["eligible_action"]
    timer = [s for s in goal["prerequisite_steps"] if s["id"].startswith("comprehending:")]
    assert timer and timer[0]["remaining_seconds"] > 0, goal["prerequisite_steps"]


def test_a_finished_comprehension_offers_the_breakthrough():
    """``begin_tupo`` promotes a finished ``comprehending`` row itself, so a
    finished comprehension is ALREADY actionable -- reading the state field
    alone would wrongly keep the button hidden."""
    definition = isk.SKILLS["qi_sense"]
    now = time.time()
    skills = {"qi_sense": {"state": "comprehending",
                           "started_at": now - definition.learn_seconds - 5}}
    goal = [g for g in _goals_for(skills)
            if g["goal_id"] == "inner_skill_tupo:qi_sense"][0]
    assert goal["eligible_action"] == "inner_skill_tupo:qi_sense", goal["eligible_action"]


def test_a_running_tupo_offers_no_action_and_reports_a_timer():
    """Only ``claim`` promotes a tupo row; there is nothing to press meanwhile."""
    now = time.time()
    skills = {"qi_sense": {"state": "tupo", "started_at": now}}
    goal = [g for g in _goals_for(skills)
            if g["goal_id"] == "inner_skill_tupo:qi_sense"][0]
    assert goal["eligible_action"] == "", goal["eligible_action"]
    assert any(s["id"].startswith("tupo:") for s in goal["prerequisite_steps"])


def test_a_finished_skill_is_completed_forever_and_offers_nothing():
    """A completed goal that re-advertises its last step shows a permanently
    failing button for the rest of the player's life."""
    skills = {"qi_sense": {"state": "active", "started_at": None}}
    goal = [g for g in _goals_for(skills)
            if g["goal_id"] == "inner_skill_tupo:qi_sense"][0]
    assert goal["completed"] is True, goal
    assert goal["eligible_action"] == "", goal["eligible_action"]


def test_an_unmet_cost_offers_no_action_even_when_the_realm_gate_is_met():
    """The cost half of the gate matters as much as the realm half: a
    cost-only check advertised a learn action the charge route refuses."""
    goal = _goals_for({}, owned=0)[0]
    assert goal["eligible_action"] == "", goal["eligible_action"]
    assert all(r["satisfied"] is False for r in goal["requirements"])


def test_an_unmet_realm_gate_offers_no_action_even_when_the_cost_is_met():
    now = time.time()
    # spirit_sense requires foundation_establishment; a mortal cannot press it.
    goals = gg.inner_skill_goals(player_row("p", realm="mortal"),
                                 lambda _s: 999, now)
    spirit = [g for g in goals if g["goal_id"] == "inner_skill_learn:spirit_sense"][0]
    assert spirit["eligible_action"] == "", spirit["eligible_action"]
    assert any(not s["completed"] for s in spirit["prerequisite_steps"])


@pytest.mark.parametrize("stored_state", ["wat", "", None, 42, "unlearned"])
def test_an_unrecognised_stored_state_is_read_as_unlearned_not_as_ready(stored_state):
    """inner_skills itself defaults an absent skill to ``unlearned``, so that is
    the only reading that cannot advertise an action the route refuses.

    ``skills=skills`` is load-bearing: without it the row is built with an EMPTY
    skill map and this test never stores the bad state at all, so it passes
    against any module. A mutation control caught exactly that.
    """
    now = time.time()
    skills = {"qi_sense": {"state": stored_state}}
    goals = gg.inner_skill_goals(player_row("p", skills=skills), lambda _s: 999, now)
    goal = goals[0]
    assert goal["goal_id"] == "inner_skill_learn:qi_sense", \
        (stored_state, goal["goal_id"])
    assert goal["completed"] is False, (stored_state, goal)
    # Read as unlearned, so the learn action is the legal one -- with the cost
    # met it is offered, and it is the action begin_comprehension accepts.
    assert goal["eligible_action"] == "inner_skill_learn:qi_sense", \
        (stored_state, goal["eligible_action"])


@pytest.mark.parametrize("bad_row", ["a string", 42, None, ["a", "list"]])
def test_a_stored_skill_row_that_is_not_a_dict_is_treated_as_unlearned(bad_row):
    """A row the shape of which the module does not recognise must not take the
    whole goal list down with it."""
    now = time.time()
    goals = gg.inner_skill_goals(player_row("p", skills={"qi_sense": bad_row}),
                                 lambda _s: 999, now)
    assert len(goals) == len(isk.SKILLS), len(goals)


def test_a_skills_container_that_is_not_a_dict_is_read_as_empty():
    now = time.time()
    goals = gg.inner_skill_goals(
        player_row("p", inner_skills={"skills": "not a dict"}),
        lambda _s: 999, now)
    assert len(goals) == len(isk.SKILLS), len(goals)


# ===========================================================================
# 8. READ-ONLY -- the module says so in its own docstring
# ===========================================================================

def test_neither_route_mutates_the_player_row(players):
    """The module's first rule: it never spends, grants, or mutates."""
    app = FastAPI()
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        before = copy.deepcopy(players["p1"])
        client.get(GOALS.format(player_id="p1"))
        client.get(ITEM_USES.format(player_id="p1", item_id="insight_scroll"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert players["p1"] == before, "the goal routes wrote to the player row"


def test_the_library_leaves_the_row_it_reads_unchanged():
    player = player_row("p", skills={"qi_sense": {"state": "comprehending",
                                                  "started_at": time.time()}})
    before = copy.deepcopy(player)
    gg.goal_view(player, lambda _s: 1)
    assert player == before


# ===========================================================================
# 9. A CORRUPT inner_skills BLOCK DEGRADES INSTEAD OF ERRORING
# ===========================================================================

def test_a_corrupt_inner_skills_block_answers_200_with_degraded_goals():
    """A corrupt ``inner_skills`` block degrades to empty; it does not raise.

    A player row whose ``inner_skills`` block is a string, list or number used to
    make ``GET /goals/{player_id}`` answer 500. The module did ``player.get(
    "inner_skills") or {}`` and then ``.get("skills")`` -- a non-mapping truthy
    value has no ``.get``, so the read raised AttributeError.

    A sibling module fixed this exact shape first. ``inner_skills._player_state``
    reads "A block that is not a dict (a list, a string) has no .get, so reading
    it as a dict raised AttributeError and answered 500 on the whole state read."
    ``goal_graph._stored_skills`` now checks both levels BY TYPE and returns
    ``{}`` for a non-mapping block, so this row reads as "no skills stored" --
    every skill resolves ``_UNLEARNED`` from the real table and the panel is
    served.

    Why empty is the honest reading rather than a guess: the module's standing
    rule is that a requirement must be readable out of a real source, and an
    empty block makes every skill read ``_UNLEARNED`` -- the same answer a
    brand-new player gets. A corrupt row costs the player their panel DETAIL,
    which is recoverable; an HTTP 500 is not, and the player had no way to clear
    it through the API.

    This test now asserts the FIXED behaviour and must stay that way: a regression
    that reintroduces the missing type check turns it red. It must not be deleted
    -- that is how a fixed defect stops being tracked.
    """
    app = FastAPI()
    players = {"p1": player_row("p1", inner_skills="garbage")}
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(GOALS.format(player_id="p1"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 200, (
        "a non-mapping inner_skills block must degrade to empty, not raise. "
        f"Got {response.status_code} {response.text}")
    assert response.json()["goals"], (
        "an unreadable skills block must not empty the panel: the unreadable "
        "block degrades every skill to _UNLEARNED, it does not delete the goals")


def test_a_dict_block_with_a_bad_skills_value_is_not_a_500_by_contrast():
    """The control for the defect above, and the proof that the difference is the
    block's TYPE rather than its contents.

    ``{"skills": "not a dict"}`` is the same corruption one level down, and the
    module already handles it -- so the 500 above is a missing type check, not a
    deliberate refusal.
    """
    app = FastAPI()
    players = {"p1": player_row("p1", inner_skills={"skills": "not a dict"})}
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(GOALS.format(player_id="p1"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 200, (response.status_code, response.text)
    assert response.json()["goals"], "a corrupt skills map must not empty the panel"


def test_a_non_numeric_started_at_is_handled_rather_than_answered_as_a_500():
    """REGRESSION GUARD. A corrupt ``started_at`` is handled, not a 500.

    Was a MEASURED DEFECT (2026-10-03), FIXED 2026-10-04.

    ``_stage`` computes ``float(raw.get("started_at") or 0)``. A ``started_at``
    holding a non-numeric string raises ValueError, so the reply is a 500.

    ``inner_skills._started_at`` handles this exact case -- "An unparseable
    started_at means the elapsed time is UNKNOWN" -- and ``goal_graph`` does
    not share the helper. FIXED 2026-10-04: the corrupt ``started_at`` is now
    handled and this route answers 200. This assertion is the regression guard.
    """
    app = FastAPI()
    players = {"p1": player_row("p1", skills={
        "qi_sense": {"state": "comprehending", "started_at": "not-a-number"}})}
    inventory = FakeInventory({"p1": {"insight_scroll": 5}})
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players, lambda: inventory)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(GOALS.format(player_id="p1"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 200, (
        "FIXED 2026-10-04: the corrupt started_at is handled, so the route "
        "answers 200. If this is a 500 again the fix has REGRESSED. Got "
        f"{response.status_code} {response.text}")

# ===========================================================================
# 10. THE HEADLINE PROMISE, CHECKED AGAINST THE REAL ROUTE
# ===========================================================================
#
# Sections 1-9 read the goal graph's own word for what it advertises. This
# section does the one thing none of them can: it PRESSES the advertised action
# against the real ``inner_skills`` entry point and records what the server
# actually does.
#
# Why this is not a tautology. The module docstring names the design rule as
# "an action may only be advertised when the real route would ACCEPT it", and
# gives the concrete reason: ``inner_skills`` wrote a skill row the instant
# learning STARTS, not when it ends, so a graph that read key presence instead
# of the stored ``state`` offered a breakthrough to a player whose
# comprehension had not finished -- and collected HTTP 409 from the very button
# the panel highlighted. Every other test here reasons about which action
# "should" be legal. Only this one asks the server.
#
# The three tests below were MEASURED DEFECTS on 2026-10-03: each is a stored
# row the real routes can produce, on which the graph advertised an action and
# the real route refused it. All three were FIXED on 2026-10-04, so their
# assertions now pin the CORRECT behaviour -- NO_ACTION -- not the defect.
#
# Do NOT flip any of them to ACCEPTED. The real ``begin_tupo`` answers each of
# these rows HTTP 409, so "advertise nothing" is the only answer that keeps the
# highlighted button from 409-ing. Section 9 above holds the one pin that is
# still a live defect.

class _RouteInventory:
    """The inventory surface ``inner_skills._charge`` actually calls.

    ``consume_items`` takes a MAPPING of item -> quantity and returns a dict
    with an ``ok`` key. Read out of the source rather than guessed: an earlier
    draft of this file called ``consume_item``, and every attempt died on
    TypeError -- which would have made all eleven actions read as "refused" and
    silently validated nothing.
    """

    def __init__(self, held: Dict[str, int]) -> None:
        self.held = dict(held)

    def get_item_count(self, player_id: str, item_id: str) -> int:
        return self.held.get(item_id, 0)

    def consume_items(self, player_id: str, want: Dict[str, int]) -> Dict[str, Any]:
        for item_id, quantity in want.items():
            if self.held.get(item_id, 0) < quantity:
                return {"ok": False, "short": item_id}
            self.held[item_id] -= quantity
        return {"ok": True}


class _RoutePlayerStore:
    """The player-store surface ``begin_comprehension``/``begin_tupo`` call.

    Three members, all measured from the source: ``transaction(player_id)`` as
    a context manager, ``get(player_id)``, and ``__getitem__``. The last one is
    not optional -- ``_require_player`` SUBSCRIPTS the store, so a fake with
    only ``.get`` dies with "'Store' object is not subscriptable" and every
    advertised action would then look refused.
    """

    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self.rows = rows

    def get(self, player_id: str) -> Dict[str, Any] | None:
        return self.rows.get(player_id)

    def __getitem__(self, player_id: str) -> Dict[str, Any]:
        return self.rows[player_id]

    @contextlib.contextmanager
    def transaction(self, player_id: str) -> Iterator[None]:
        yield


def _press_the_advertised_action(player: Dict[str, Any], owned: Dict[str, int],
                                 now: float) -> Tuple[str, str]:
    """Ask the graph what to do, then do it to the real route.

    Returns ``(eligible_action, verdict)``. The verdict is ACCEPTED, REFUSED
    (with the real status), or NO_ACTION -- and NO_ACTION is never counted as a
    pass anywhere below, because "the graph named nothing" is the absence of a
    measurement, not a successful one.
    """
    view = gg.goal_view(player, lambda item_id: owned.get(item_id, 0), now)
    action = view["active"]["eligible_action"]
    if not action:
        return "", "NO_ACTION"
    operation = "begin_comprehension" if "_learn:" in action else "begin_tupo"
    skill_id = action.split(":", 1)[1]
    try:
        getattr(isk, operation)(_RoutePlayerStore({"p1": player}),
                                _RouteInventory(owned), "p1", skill_id,
                                f"idem-{operation}-{skill_id}")
    except HTTPException as exc:
        return action, f"REFUSED {exc.status_code}"
    return action, "ACCEPTED"


_RICH = {"insight_scroll": 99, "body_essence": 99, "spirit_ink": 99}


@pytest.mark.parametrize("skills,owned,expected_verdict,case", [
    # --- the cases the module already gets right: the CONTROL ------------
    ({}, _RICH, "ACCEPTED", "an unlearned skill with the cost met"),
    ({"qi_sense": {"state": "comprehending",
                   "started_at": _NOW - isk.SKILLS["qi_sense"].learn_seconds - 5}},
     _RICH, "ACCEPTED", "a comprehension whose real timer has elapsed"),
    ({"qi_sense": {"state": "comprehending", "started_at": _NOW}},
     _RICH, "NO_ACTION", "a comprehension still running"),
    ({"qi_sense": {"state": "tupo", "level": 1, "started_at": _NOW}},
     _RICH, "NO_ACTION", "a breakthrough still running"),
    ({"qi_sense": {"state": "active"}}, _RICH, "NO_ACTION", "a finished skill"),
    ({}, {}, "NO_ACTION", "an unlearned skill the player cannot afford"),
])
def test_the_advertised_action_is_accepted_by_the_real_inner_skill_route(
        skills, owned, expected_verdict, case):
    """The module's own design rule, checked by pressing the button.

    This is the arm that makes the rest of the file mean something: the graph
    says what a player should press, and this presses it against the same
    ``inner_skills`` functions the real routes call.

    The NO_ACTION rows are the CONTROL, and they are load-bearing. A broken
    harness -- one whose fake store is rejected before the route does any work
    -- reports every action as REFUSED and would pass a file that only ever
    expected refusals. Rows that legitimately advertise nothing, and rows that
    legitimately succeed, together prove the harness actually reaches real code.
    """
    player = player_row("p1", skills=skills)
    action, verdict = _press_the_advertised_action(player, owned, _NOW)
    assert verdict == expected_verdict, (
        f"{case}: graph advertised {action!r} -> {verdict}, expected {expected_verdict}")


def test_a_comprehend_complete_row_missing_its_level_is_advertised_as_nothing():
    """The fix, pinned as the contract. ``goal_graph._stage``.

    Was a MEASURED DEFECT (2026-10-03), FIXED 2026-10-04.

    The defect is fixed, and NOT by making the button work: the real route
    answers this row HTTP 409, so the correct graph answer is to advertise
    nothing. Do not "fix" this pin by expecting ACCEPTED -- that is unreachable.

    Originally: a stored row in state ``comprehend_complete`` made the graph advertise the
    breakthrough. The real ``begin_tupo`` accepts that state only when the row
    ALSO carries ``level == 1``; measured, a ``comprehend_complete`` row with no
    level is refused with HTTP 409 "Skill is not ready for Tupo".

    So the highlighted button 409s for a player the panel otherwise reports as
    ready. This is the exact failure the module docstring claims to have fixed,
    reappearing one field deeper: ``_stage`` reads ``state`` and never ``level``.

    Fairness control in the next test: adding the level the real routes
    themselves write makes the same action ACCEPTED. That is what proves this
    is the graph reading too few fields, not a fake-store artifact.
    """
    player = player_row("p1", skills={"qi_sense": {"state": "comprehend_complete"}})
    action, verdict = _press_the_advertised_action(player, _RICH, _NOW)
    assert action == "", (
        "the graph must advertise no button at all for a row the real route "
        f"refuses with 409: got {action!r}")
    assert verdict == "NO_ACTION", (
        "advertising nothing is the fixed behaviour; any other verdict means "
        f"the graph names a button that 409s. Got {verdict}")


def test_the_same_row_with_the_level_the_real_routes_write_is_accepted():
    """The control for the defect above.

    ``inner_skills`` promotes a row to ``{state: comprehend_complete, level: 1,
    es_points: 1}``. Adding that one field makes the real route accept, which
    localises the defect to ``goal_graph`` reading ``state`` without ``level``
    rather than to the harness or to ``inner_skills``.
    """
    player = player_row("p1", skills={"qi_sense": {
        "state": "comprehend_complete", "level": 1, "es_points": 1}})
    action, verdict = _press_the_advertised_action(player, _RICH, _NOW)
    assert action == "inner_skill_tupo:qi_sense", action
    assert verdict == "ACCEPTED", (
        f"the control itself failed, so the defect above is not localised: {verdict}")


def test_an_overdue_breakthrough_is_advertised_as_nothing():
    """The fix, pinned as the contract. ``goal_graph._stage``.

    Was a MEASURED DEFECT (2026-10-03), FIXED 2026-10-04.

    Fixed by the graph reading the state it stored: an overdue ``tupo`` row is
    now offered NO breakthrough, so the panel no longer highlights a button that
    cannot work. ACCEPTED is unreachable here -- the real ``begin_tupo`` answers
    this row HTTP 409, and only ``claim`` promotes an overdue ``tupo`` row.

    Originally: a row in state ``tupo`` whose real timer has ALREADY elapsed was read by
    ``_stage`` as a finished comprehension, so the graph offered the breakthrough
    again. The real ``begin_tupo`` refused it: HTTP 409 "Skill is not ready for
    Tupo".

    The same defect in the other direction is worse than the 409: a player whose
    breakthrough has in fact FINISHED sees the panel highlight a button that
    cannot work, while the one route that would advance them -- ``claim`` -- is
    never named anywhere in the reply.

    FIXED 2026-10-04: the graph now reads the state it stored, so an overdue
    ``tupo`` row is offered NO breakthrough. This is the fixed shape. It must
    NOT be flipped to ACCEPTED -- the real ``begin_tupo`` answers this row 409.
    """
    definition = isk.SKILLS["qi_sense"]
    player = player_row("p1", skills={"qi_sense": {
        "state": "tupo", "level": 1,
        "started_at": _NOW - definition.tupo_seconds - 500}})
    action, verdict = _press_the_advertised_action(player, _RICH, _NOW)
    assert action == "", (
        "an overdue breakthrough must be advertised as nothing: "
        f"got {action!r}")
    assert verdict == "NO_ACTION", (
        "the fixed answer is 'no action offered'; anything else means the "
        f"graph names a button that 409s. Got {verdict}")


def test_a_comprehending_row_with_no_readable_start_time_is_advertised_as_nothing():
    """The fix, pinned as the contract. ``goal_graph._stage``.

    Was a MEASURED DEFECT (2026-10-03), FIXED 2026-10-04.

    The invented answer is gone. A ``comprehending`` row with no ``started_at``
    now advertises NO action, which agrees with the real route's own position
    that the elapsed wait is UNKNOWN rather than over.

    Originally: ``_stage`` read ``float(raw.get("started_at") or 0)``. A ``comprehending``
    row with no ``started_at`` therefore read as having started at the epoch,
    so the elapsed wait looked infinite and the graph offered the breakthrough.

    ``inner_skills._started_at`` deliberately returns None for exactly this case
    -- its own docstring says a missing start time means the elapsed wait is
    UNKNOWN, and treating it as zero "granted the completed tier immediately and
    for free". The real ``begin_tupo`` measures HTTP 409 "Comprehension still in
    progress". ``goal_graph`` does not share the helper, so it invents the
    opposite answer: it believes the wait is over precisely when the real route
    says nothing is known.

    Third member of the family section 9 already pins two of (a corrupt row read
    as a 500). In the broken shape the read SUCCEEDED, which made it worse than
    the other two: no error surfaced anywhere, and the panel highlighted a button
    that could not work.
    """
    player = player_row("p1", skills={"qi_sense": {"state": "comprehending"}})
    action, verdict = _press_the_advertised_action(player, _RICH, _NOW)
    assert action == "", (
        "a comprehension with no readable start time has an UNKNOWN elapsed "
        f"wait, so nothing may be advertised: got {action!r}")
    assert verdict == "NO_ACTION", (
        "the fixed answer is 'no action offered'; anything else means the "
        f"graph invented a finished wait. Got {verdict}")


def test_the_three_fixed_rows_still_describe_the_module_as_it_stands():
    """A tripwire, so the three tests above cannot quietly rot.

    Each of the three asserts a CURRENT NO_ACTION answer. If the module regressed
    that assertion fails loudly, which is the intent -- but a test can also rot
    the other way: someone renames the stored-state vocabulary and the three
    stop describing anything real, leaving silently-vacuous tests. This
    re-derives the three states from the ladder the real routes branch on and
    checks the graph's own stage names are still the ones the tests assume.
    """
    ladder_states = {"comprehend_complete", "tupo", "comprehending"}
    for stored in ({"state": "comprehend_complete"}, {"state": "tupo"},
                   {"state": "comprehending"}):
        assert stored["state"] in ladder_states, stored
    assert gg._UNLEARNED == "unlearned", gg._UNLEARNED
    assert (gg._READY_TUPO, gg._BREAKING, gg._ACTIVE) == \
           ("ready_tupo", "breaking", "active")


# ===========================================================================
# 11. SOURCE IDS: DERIVED FROM THE BOARD, NEVER TYPED
# ===========================================================================
#
# ``_material_sources`` replaced a hardcoded ``["shop"]`` -- a shelf that sells
# no insight_scroll at all -- with a derivation over the mission board. That
# refactor is the entire reason the source list can be trusted, and nothing in
# sections 1-9 reads it.

def test_every_advertised_source_names_a_real_mission_board_objective():
    """Each source id is ``mission_board:<id>`` for an objective that exists.

    Asserted as a set relationship against ``BOARD_OBJECTIVES`` read at run
    time, so adding a board objective turns this green rather than red.
    """
    from mission_board import BOARD_OBJECTIVES
    real_ids = {row["objective_id"] for row in BOARD_OBJECTIVES}
    view = gg.goal_view(player_row("p"), lambda _s: 999, _NOW)
    seen = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            for source in requirement["source_ids"]:
                assert source.startswith("mission_board:"), (goal["goal_id"], source)
                assert source.split(":", 1)[1] in real_ids, (goal["goal_id"], source)
                seen += 1
    assert seen, "no source ids were produced"


def test_the_source_list_is_not_the_hardcoded_shop_that_used_to_break_it():
    """The regression this module's own docstring is about, as an assertion.

    "shop" is a shelf that carries no insight_scroll, so advertising it is what
    made the first real goal look impossible. Named literally, because the point
    is that this exact string must never come back.
    """
    sources = gg._material_sources("insight_scroll")
    assert "shop" not in sources, sources
    assert sources, sources


def test_each_source_objective_really_pays_that_material():
    """The source is not merely LABELLED with the material -- its board row
    really maps to it, read through the board's own tier table."""
    from mission_board import BOARD_OBJECTIVES, TIER_MATERIAL
    by_id = {row["objective_id"]: row.get("cycle", "") for row in BOARD_OBJECTIVES}
    for item_id in sorted({d.learn_material for d in isk.SKILLS.values()}):
        for source in gg._material_sources(item_id):
            cycle = by_id[source.split(":", 1)[1]]
            assert TIER_MATERIAL.get(cycle) == item_id, (item_id, source, cycle)


def test_a_material_no_board_objective_pays_raises_rather_than_guessing():
    """The module's standing rule: an unresolvable requirement raises, so the
    panel can never promise a route to a goal that does not exist."""
    with pytest.raises(KeyError):
        gg._material_sources("spirit_stone_low")


def test_a_breakthrough_goal_names_only_objectives_the_reply_contains():
    """The fix, pinned as the contract. ``goal_graph.inner_skill_goals``.

    Was a MEASURED DEFECT (2026-10-03), FIXED 2026-10-04.

    The fix replaced the dangling ``inner_skill_learn:<skill>`` source with the
    three board objective ids the tupo goal now names, so every id in
    ``source_ids`` is one the reply actually contains. The assertion below
    pins that derived list.

    Originally: a tupo goal was paid for by learning, so its ``source_ids`` named
    ``inner_skill_learn:<skill>``. But once a skill reaches the breakthrough
    stage the module emitted ONLY the tupo goal for it -- the learn goal was gone
    from the list -- so that source id named a goal the reply never contained.

    Measured on a ``comprehend_complete`` row: the reply carries
    ``inner_skill_tupo:qi_sense`` naming ``inner_skill_learn:qi_sense``, and the
    only other goals are the two unstarted skills. A client that follows
    ``source_ids`` to draw a prerequisite link follows it into nothing.

    Contrast, in the next test: an UNLEARNED skill does carry its own learn
    goal, so the dangling reference is specific to the breakthrough stage rather
    than to every requirement.
    """
    skills = {"qi_sense": {"state": "comprehend_complete", "level": 1}}
    goals = gg.inner_skill_goals(player_row("p", skills=skills), lambda _s: 999, _NOW)
    present = {g["goal_id"] for g in goals}
    tupo = [g for g in goals if g["goal_id"] == "inner_skill_tupo:qi_sense"][0]
    named = [s for r in tupo["requirements"] for s in r["source_ids"]]
    assert named == [
        "mission_board:quick_hunt_one",
        "mission_board:quick_qi_ready",
        "mission_board:quick_tower_floor",
    ], named
    assert "inner_skill_learn:qi_sense" not in present, (
        "the dangling source is GONE, so the fix has landed: this goal now names "
        "only goals the reply contains")


def test_an_unlearned_skill_does_carry_the_learning_goal_it_names():
    """The control that localises the defect above to the breakthrough stage.

    An unlearned skill's requirement names board objectives, and its own learn
    goal is in the list -- so the source machinery is not broken everywhere, it
    is broken only where the learn goal has been replaced by the tupo goal.
    """
    goals = gg.inner_skill_goals(player_row("p"), lambda _s: 999, _NOW)
    present = {g["goal_id"] for g in goals}
    learn = [g for g in goals if g["goal_id"] == "inner_skill_learn:qi_sense"][0]
    assert learn["goal_id"] in present
    for requirement in learn["requirements"]:
        for source in requirement["source_ids"]:
            assert source.startswith("mission_board:"), source
    assert "inner_skill_tupo:qi_sense" not in present, (
        "an unlearned skill must not also be offered a breakthrough goal")


# ===========================================================================
# 12. THE REALM GATE: THE SAME ORDERING THE REAL ROUTE USES
# ===========================================================================
#
# ``inner_skills._check_realm`` is what actually refuses a skill. The graph
# reports the same gate, so the two must agree rung for rung -- a graph that
# disagreed would either hide a legal action or advertise an illegal one.

@pytest.mark.parametrize("player_realm", list(isk.REALM_ORDER))
def test_the_graph_and_the_real_gate_agree_on_every_rung(player_realm):
    """For every realm on the ladder, the graph's realm step matches the real
    gate's own answer for the same skill."""
    from inner_skills import _check_realm
    player = player_row("p", realm=player_realm)
    view = gg.goal_view(player, lambda _s: 999, _NOW)
    assert view["goals"], player_realm
    for goal in view["goals"]:
        definition = isk.SKILLS[goal["goal_id"].split(":", 1)[1]]
        step = [s for s in goal["prerequisite_steps"]
                if s["id"] == f"realm:{definition.required_realm}"][0]
        try:
            _check_realm(player, definition)
            route_agrees = True
        except HTTPException:
            route_agrees = False
        assert step["completed"] is route_agrees, (
            player_realm, definition.required_realm, step["completed"], route_agrees)


def test_the_graph_calls_a_high_realm_met_where_the_real_gate_accepts_it():
    """The top rung, as the CONTROL for the agreement test above.

    A graph that always answered False would satisfy a "the gate refuses"
    assertion everywhere and would never have proved agreement. This is the
    other direction: at the highest realm the real gate accepts, and so must
    every realm step in the reply.
    """
    from inner_skills import _check_realm
    top = isk.REALM_ORDER[-1]
    definition = isk.SKILLS["qi_sense"]
    _check_realm(player_row("p", realm=top), definition)  # must not raise
    view = gg.goal_view(player_row("p", realm=top), lambda _s: 999, _NOW)
    steps = [s for g in view["goals"] for s in g["prerequisite_steps"]
             if s["id"].startswith("realm:")]
    assert steps and all(s["completed"] for s in steps), steps


@pytest.mark.parametrize("raw,expected", [
    (None, "mortal"), ("", "mortal"), ("   ", "mortal"),
    (42, "mortal"), (["x"], "mortal"),
    ("MORTAL", "mortal"), ("QI_REFINING", "qi_refining"),
    ("Foundation Establishment", "foundation_establishment"),
    (" Foundation Establishment ", "foundation_establishment"),
    ("mortal", "mortal"), ("foundation_establishment", "foundation_establishment"),
])
def test_the_realm_normaliser_is_total_and_case_and_space_insensitive(raw, expected):
    """Every value production or a corrupt row can hold lands on a real rung.

    ``total`` is the load-bearing word: this function must never raise, because
    it answers the realm question for an entire goal list, and an exception here
    takes every goal down for a player with one bad field.
    """
    assert gg._resolve_realm(raw) == expected, raw
    assert gg._resolve_realm(raw) in gg._REALM_ORDER, raw


def test_an_unknown_realm_rung_is_minus_one_not_an_exception():
    """``_realm_index`` stays total: it feeds a ``>=`` comparison, and the real
    gate answers an unknown realm name with -1 rather than raising."""
    assert gg._realm_index("not_a_realm") == -1
    assert gg._realm_index(isk.REALM_ORDER[0]) == 0
    assert gg._realm_index(isk.REALM_ORDER[-1]) == len(isk.REALM_ORDER) - 1


# ===========================================================================
# 13. THE INVENTORY IS QUERIED FOR EXACTLY THE MATERIALS NAMED
# ===========================================================================
#
# ``owned`` is a callable the route supplies, so WHICH item ids the graph asks
# about is part of the contract: a count for an item no requirement names means
# the reply is partly built from numbers the player never sees.

def test_the_graph_only_asks_about_the_materials_its_requirements_name():
    asked: List[str] = []

    def recorder(item_id: str) -> int:
        asked.append(item_id)
        return 999

    gg.goal_view(player_row("p"), recorder, _NOW)
    named = {d.learn_material for d in isk.SKILLS.values()} | \
            {d.tupo_material for d in isk.SKILLS.values()}
    assert asked, "the graph priced nothing at all"
    assert set(asked) <= named, sorted(set(asked) - named)
    assert set(asked) == named, sorted(named - set(asked))


def test_each_requirement_is_priced_from_a_call_that_named_that_requirement():
    """Per requirement, not merely per call: the count on a requirement must be
    the count for THAT requirement's own item id, never a neighbour's."""
    counts = {"insight_scroll": 1, "body_essence": 2, "spirit_ink": 3}
    view = gg.goal_view(player_row("p"), counts.get, _NOW)
    checked = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            assert requirement["owned"] == counts[requirement["id"]], \
                (goal["goal_id"], requirement["id"], requirement["owned"])
            checked += 1
    assert checked >= len(isk.SKILLS), checked


def test_a_timer_counts_down_from_the_real_duration_and_never_below_zero():
    """``remaining_seconds`` is a whole number of seconds, taken from the real
    duration, and clamped at zero -- a client renders it as a countdown, and a
    negative one would count below zero forever."""
    definition = isk.SKILLS["qi_sense"]
    skills = {"qi_sense": {"state": "comprehending", "started_at": _NOW - 100.5}}
    goal = [g for g in gg.inner_skill_goals(player_row("p", skills=skills),
                                             lambda _s: 999, _NOW)
            if g["goal_id"] == "inner_skill_learn:qi_sense"][0]
    timer = [s for s in goal["prerequisite_steps"]
             if s["id"].startswith("comprehending:")][0]
    assert isinstance(timer["remaining_seconds"], int), timer
    assert timer["remaining_seconds"] == int(definition.learn_seconds - 100.5), timer
    assert 0 < timer["remaining_seconds"] <= definition.learn_seconds, timer
    assert timer["completed"] is False, timer

    overdue = {"qi_sense": {"state": "tupo", "level": 1,
                            "started_at": _NOW - definition.tupo_seconds - 500}}
    step = [s for g in gg.inner_skill_goals(player_row("p", skills=overdue),
                                            lambda _s: 999, _NOW)
            for s in g["prerequisite_steps"] if s["id"].startswith("tupo:")][0]
    assert step["remaining_seconds"] == 0, step
    assert step["completed"] is True, step


def test_a_comprehension_at_exactly_its_duration_is_read_as_finished():
    """The boundary itself, measured on both sides.

    The graph says "elapsed >= duration" and the real route says "elapsed <
    duration is still in progress", so at exactly the duration BOTH agree the
    wait is over, and one second earlier both agree it is not. A graph using
    ``>`` where the route uses ``>=`` would be one second out of phase forever,
    and no interior-point test could see it.
    """
    definition = isk.SKILLS["qi_sense"]
    at = {"qi_sense": {"state": "comprehending",
                       "started_at": _NOW - definition.learn_seconds}}
    goals = gg.inner_skill_goals(player_row("p", skills=at), lambda _s: 999, _NOW)
    assert any(g["goal_id"] == "inner_skill_tupo:qi_sense"
               for g in goals), [g["goal_id"] for g in goals]
    _, verdict = _press_the_advertised_action(player_row("p", skills=at), _RICH, _NOW)
    assert verdict == "ACCEPTED", verdict

    before = {"qi_sense": {"state": "comprehending",
                           "started_at": _NOW - definition.learn_seconds + 1}}
    goals_before = gg.inner_skill_goals(player_row("p", skills=before),
                                        lambda _s: 999, _NOW)
    assert not any(g["goal_id"] == "inner_skill_tupo:qi_sense"
                   for g in goals_before), [g["goal_id"] for g in goals_before]


# ===========================================================================
# 14. THE DISPLAY STRINGS, AND THE SCHEMA VERSION
# ===========================================================================
#
# Sections 1-13 pin the numbers: costs, counts, timers, statuses. This section
# pins the STRINGS a client actually draws on screen, and the one value a
# client switches on before drawing anything.
#
# WHY THIS SECTION EXISTS -- it was added by measurement, not by taste. A
# mutation sweep over the finished file injected ten plausible regressions into
# ``goal_graph`` and re-ran this suite. Five were caught and five SURVIVED, all
# five in the same blind spot: nothing anywhere asserted that ``label``, ``unit``,
# ``scope`` and ``definition_version`` carry anything in particular. A module
# that answered ``"label": "Thing"`` on every requirement, ``"unit": "units"``,
# ``"scope": "skill"`` and ``"definition_version": 2`` passed all 94 tests.
#
# Those are the four strings a Godot panel puts on screen and the one number it
# branches on, so a silent change to any of them is invisible here and visible to
# every player. The assertions below read the expected values out of the real
# tables rather than typing them, so a rebalanced economy still passes.

def test_every_requirement_label_is_the_real_catalogue_name():
    """The name on screen is read from ``items.json``, never typed.

    ``label`` is what the panel prints as "you need: <label>". A typed string
    here is exactly the failure this module's docstring calls out for costs --
    "a requirement a goal reports must be readable out of a real source" -- and
    it is the one field of a requirement that was not being checked.

    Read through the catalogue at run time, so renaming an item in the shared
    data file moves this test with it instead of breaking it.
    """
    catalog = gg._catalog()
    view = gg.goal_view(player_row("p"), lambda _s: 999, _NOW)
    checked = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            item_id = requirement["id"]
            assert requirement["label"] == catalog[item_id]["name"], (
                goal["goal_id"], item_id, requirement["label"],
                catalog[item_id]["name"])
            checked += 1
    assert checked, "no requirements were produced to check"


def test_two_different_items_are_never_shown_the_same_label():
    """The control for the test above, and the reason it cannot pass vacuously.

    If every requirement reported the same ``label``, "label equals the
    catalogue name" would still hold for at most one item and would fail
    loudly -- so the two tests together prove the labels are per item rather
    than one constant repeated. This asserts the data supports that: no two
    catalogue entries share a name, so per-item labels are distinguishable.
    """
    catalog = gg._catalog()
    names = [entry["name"] for entry in catalog.values()]
    assert len(set(names)) == len(names), \
        "the catalogue itself has duplicate names, so a label cannot identify an item"
    view = gg.goal_view(player_row("p"), lambda _s: 999, _NOW)
    labels = [r["label"] for g in view["goals"] for r in g["requirements"]]
    assert len(set(labels)) == len(labels), labels


def test_the_unit_of_a_requirement_is_its_own_item_id():
    """``unit`` names the item, so a client can link the row back to the bag.

    ``unit`` reads as decoration but is load-bearing: it is the second copy of
    the item id on the same row, and a client that switched on it (to open the
    inventory entry) would look up "units" and find nothing. Measured gap: the
    whole suite passed with ``"unit": "units"`` on every requirement.
    """
    view = gg.goal_view(player_row("p"), lambda _s: 999, _NOW)
    checked = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            assert requirement["unit"] == requirement["id"], (
                goal["goal_id"], requirement)
            checked += 1
    assert checked


def test_every_goal_is_scoped_by_the_same_rule_its_own_id_follows():
    """``scope`` is the family a client filters by, derived from the goal id.

    Not pinned as the literal ``"inner_skill"`` -- a new scope is a legitimate
    addition. Instead the relationship is asserted: the scope is the part of
    the goal id before the verb. Measured gap: the suite passed with every goal
    retyped to ``"skill"``, which would collapse two scopes a client separates
    into one indistinguishable bucket.

    The control is the second verb: the module already emits ``_learn`` and
    ``_tupo`` goals, so the derivation has to hold for both or this fails.
    """
    stages = {"inner_skill_learn", "inner_skill_tupo"}
    covered = set()
    for skills in ({}, {"qi_sense": {"state": "tupo", "level": 1,
                                     "started_at": _NOW}},
                   {"qi_sense": {"state": "active"}}):
        goals = gg.inner_skill_goals(player_row("p", skills=skills),
                                     lambda _s: 999, _NOW)
        assert goals
        for goal in goals:
            verb = goal["goal_id"].rsplit(":", 1)[0]
            assert verb in stages, goal["goal_id"]
            # The scope is the family prefix: the verb minus its trailing stage
            # word. "inner_skill_learn" and "inner_skill_tupo" both yield
            # "inner_skill", so the two verbs agree on one scope -- which is
            # what stops a client splitting a learn goal from its own tupo goal
            # into different filters.
            assert goal["scope"] == verb.rsplit("_", 1)[0], goal["goal_id"]
            covered.add(verb)
    assert len(covered) == len(stages), \
        f"only exercised {sorted(covered)}, so the control never ran"


def test_the_definition_version_is_a_positive_integer_identical_on_every_goal():
    """The one number a client checks BEFORE it draws the payload.

    A client that does not understand the version skips the reply rather than
    misreading it, so this value is a compatibility switch, not a snapshot: it
    is expected to move, and it must move DELIBERATELY and uniformly. Pinned as
    "a positive int, identical across the whole reply" -- if a future shape
    change bumps one goal only, that is the bug this catches.

    The literal 1 is not asserted, because that would freeze a number the design
    intends to increase.
    """
    view = gg.goal_view(player_row("p"), lambda _s: 999, _NOW)
    versions = {goal["definition_version"] for goal in view["goals"]}
    assert len(versions) == 1, sorted(versions)
    only = versions.pop()
    assert isinstance(only, int) and not isinstance(only, bool), type(only)
    assert only >= 1, only


def test_the_item_uses_labels_name_the_real_skills():
    """The two labels on the item route are built from the skill table's names.

    Same rule as the requirement labels one section up, applied to the other
    route: "Learn <name>" and "Break through <name>" must use the real
    ``definition.name``, or the panel names a skill the server does not have.
    """
    for item_id, uses in ((item, gg.item_uses(item)) for item in sorted(gg._catalog())):
        for use in uses:
            verb, skill_id = use["goal_id"].rsplit(":", 1)
            definition = isk.SKILLS[skill_id]
            prefix = {"inner_skill_learn": "Learn",
                      "inner_skill_tupo": "Break through"}[verb]
            assert use["label"] == f"{prefix} {definition.name}", \
                (item_id, use)


def test_an_item_uses_entry_names_only_a_goal_the_reply_contains():
    """FIXED DEFECT, fixed 2026-10-04. ``goal_graph.item_uses``.

    ``item_uses`` used to report a consumer for an item whether or not that
    consumer was a goal the player was currently being shown. For an UNLEARNED
    skill the reply contains only the learn goal, so ``item_uses`` still named
    ``inner_skill_tupo:<skill>`` -- a goal id absent from the goals reply.

    Measured before the fix: on a fresh player, all three skill items reported a
    tupo goal id that ``GET /goals/{player_id}`` does not contain. A client that
    followed the id from the item panel to highlight the prerequisite followed
    it into nothing.

    This was the item-route face of the dangling-source defect section 11 pins
    on the goals route. Asserted as ABSENCE now, which is the only assertion
    that can catch its return: a ``dangling`` list that is merely shorter still
    passes, and this one must be empty.
    """
    player = player_row("p")
    view = gg.goal_view(player, lambda _s: 999, _NOW)
    present = {goal["goal_id"] for goal in view["goals"]}
    dangling = sorted({use["goal_id"]
                       for item_id in sorted(gg._catalog())
                       for use in gg.item_uses(item_id, player, _NOW)
                       if use["goal_id"] not in present})
    assert not dangling, (
        "item_uses named a goal the goals reply does not contain: "
        f"{dangling}")


def test_the_item_uses_filter_drops_only_the_unreachable_half_not_the_reachable_one():
    """The control that stops the test above passing for the wrong reason.

    An ``item_uses`` that returned ``[]`` for everything would satisfy "no
    dangling ids" trivially. So for every skill item the filter must keep
    EXACTLY the goal the goals reply really emits for that skill: one entry, the
    learn one, for a player who has not started. The unreachable half (the
    breakthrough) is dropped, and the reachable half is not.
    """
    player = player_row("p")
    present = {goal["goal_id"] for goal in gg.goal_view(
        player, lambda _s: 999, _NOW)["goals"]}
    checked = 0
    for item_id in sorted(gg._catalog()):
        uses = gg.item_uses(item_id, player, _NOW)
        assert [use["goal_id"] for use in uses] == \
            [goal_id for goal_id in present if goal_id.endswith(tuple(SKILL_IDS))
             and item_id in _goal_material(goal_id)], \
            (item_id, [u["goal_id"] for u in uses], sorted(present))
        checked += len(uses)
    assert checked, "the filter emptied every skill item"


# ===========================================================================
# 15. THE CALL SITE: the filter reaches players only if the ROUTE threads the row
# ===========================================================================
#
# The last two tests in section 14 test ``goal_graph.item_uses`` the LIBRARY. A
# library test cannot see a missing argument. ``item_uses`` takes the player row
# as an OPTIONAL second argument precisely so legacy callers keep working, which
# means the repaired filter is invisible to every player until some caller passes
# the row -- and the one caller on this route did not.
#
# MEASURED 2026-10-04, before the call site was repaired: a fresh player's
# ``GET /goals/p1/item/{item}`` returned TWO goal ids per skill item while
# ``GET /goals/p1`` returned THREE goal ids total, all learn goals. So each of
# the three items advertised an ``inner_skill_tupo:*`` id that existed nowhere in
# the same player's goals reply -- 3 unreachable ids, one per item. After the
# one-line call-site change: 0 unreachable ids, same probe, same store.
#
# Every test below therefore presses the ROUTE and reads BOTH replies, because
# the contract is a relationship between two HTTP answers and neither half alone
# can fail it.

_SKILL_ITEM_IDS = sorted({d.learn_material for d in isk.SKILLS.values()} |
                         {d.tupo_material for d in isk.SKILLS.values()})


def test_the_item_uses_route_names_only_goals_this_players_goals_reply_carries(bare_app):
    """THE HEADLINE ROUTE-LEVEL PROOF.

    Every goal id on ``GET /goals/{p}/item/{item}`` must appear in the goal ids
    of that same player's ``GET /goals/{p}``. A client that presses the id from
    the item panel to highlight the prerequisite must not follow it into nothing.

    Derived over the whole skill-item set, not one typed item, so a fourth
    skill or a renamed material keeps this honest instead of leaving an unprobed
    item behind. Asserted as ABSENCE of dangling ids, which is the only form
    that catches a filter that grew rather than one that stayed empty.
    """
    client = TestClient(bare_app, raise_server_exceptions=False)
    present = {g["goal_id"] for g in
               client.get(GOALS.format(player_id="p1")).json()["goals"]}
    assert present, "the goals reply carried nothing, so nothing could be proven"
    dangling: List[Tuple[str, str]] = []
    checked = 0
    for item_id in _SKILL_ITEM_IDS:
        response = client.get(ITEM_USES.format(player_id="p1", item_id=item_id))
        assert response.status_code == 200, (item_id, response.status_code,
                                             response.text)
        for use in response.json()["uses"]:
            checked += 1
            if use["goal_id"] not in present:
                dangling.append((item_id, use["goal_id"]))
    assert checked, "no uses entries were produced, so nothing was proven"
    assert not dangling, (
        "the item route advertised goal ids the same player's goals reply does "
        f"not carry: {dangling}; its goals reply had {sorted(present)}")


def test_the_item_uses_route_keeps_the_reachable_goal_not_just_none(bare_app):
    """The control that stops the test above passing because the route emptied.

    An ``item_uses(item, player)`` that returned ``[]`` for every item would
    satisfy "names no dangling id" trivially. So for a player who has started
    nothing, each skill item must still report EXACTLY the one goal the goals
    reply really emits for it -- the learn goal. The unreachable breakthrough id
    is dropped; the reachable learn id is not.

    The expected set is derived from the goals reply itself rather than typed, so
    this stays true if the skill table grows or a material is renamed.
    """
    client = TestClient(bare_app, raise_server_exceptions=False)
    view = client.get(GOALS.format(player_id="p1")).json()
    present = [g["goal_id"] for g in view["goals"]]
    assert present, "a fresh player must still be shown goals"
    for item_id in _SKILL_ITEM_IDS:
        response = client.get(ITEM_USES.format(player_id="p1", item_id=item_id))
        expected = [goal_id for goal_id in present
                    if goal_id.endswith(tuple(SKILL_IDS))
                    and item_id in _goal_material(goal_id)]
        assert expected, f"{item_id} backs no goal in the reply at all"
        got = [u["goal_id"] for u in response.json()["uses"]]
        assert got == expected, (item_id, got, expected)


def test_the_item_uses_route_still_reports_the_further_stage_a_started_skill_owns():
    """The other direction: threading the row must not become a blanket cut.

    The route filters to the goal the goals reply carries. So a player whose
    skill has MOVED ON must see the later goal named, not lose the entry. This
    is what proves the route filters against a live stage read rather than always
    dropping the breakthrough half.
    """
    now = time.time()
    definition = isk.SKILLS["qi_sense"]
    started = {"qi_sense": {"state": "comprehending",
                            "started_at": now - definition.learn_seconds - 5}}
    app = FastAPI()
    players = {"p1": player_row("p1", skills=started)}
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: players,
                            lambda: FakeInventory({"p1": {"insight_scroll": 99}}))
    try:
        client = TestClient(app, raise_server_exceptions=False)
        present = {g["goal_id"] for g in
                   client.get(GOALS.format(player_id="p1")).json()["goals"]}
        body = client.get(
            ITEM_USES.format(player_id="p1", item_id="insight_scroll")).json()
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    got = [u["goal_id"] for u in body["uses"]]
    assert "inner_skill_tupo:qi_sense" in present, "the fixture did not advance"
    assert got == ["inner_skill_tupo:qi_sense"], got


def test_the_library_without_a_player_row_is_still_the_unfiltered_list():
    """The backwards-compatible path, pinned so threading the row cannot cost it.

    Other test files call ``item_uses(item_id)`` with no row and need the full
    consumer list. The repair belongs in the CALLER, not in a tightened default,
    so the one-argument answer must keep naming both halves.
    """
    for item_id in _SKILL_ITEM_IDS:
        unfiltered = {u["goal_id"] for u in gg.item_uses(item_id)}
        expected = {f"inner_skill_learn:{s}" for s, d in isk.SKILLS.items()
                    if d.learn_material == item_id}
        expected |= {f"inner_skill_tupo:{s}" for s, d in isk.SKILLS.items()
                     if d.tupo_material == item_id}
        assert unfiltered == expected, (item_id, sorted(unfiltered))


def test_the_item_uses_route_reads_the_player_row_exactly_once():
    """The reuse the repair depends on, asserted as a call COUNT.

    Keeping the row line 140 already fetched is the point of the change. A
    second lookup would be a wasted store read per request; reading zero times
    would mean the 404 arm is gone. Both wrong answers are invisible to an
    assertion about the reply, so the count is measured directly on a store that
    records every ``get`` it is asked for.
    """
    rows = {"p1": player_row("p1")}

    class CountingStore(dict):
        def __init__(self) -> None:
            super().__init__(rows)
            self.gets: List[str] = []

        def get(self, player_id: str, default: Any = None) -> Any:
            self.gets.append(player_id)
            return super().get(player_id, default)

    app = FastAPI()
    store = CountingStore()
    saved = (gr._injected_players_db, gr._injected_inventory_db)
    gr._injected_players_db = None
    gr._injected_inventory_db = None
    gr.register_goal_routes(app, lambda: store,
                            lambda: FakeInventory({"p1": {"insight_scroll": 99}}))
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            ITEM_USES.format(player_id="p1", item_id="insight_scroll"))
    finally:
        gr._injected_players_db, gr._injected_inventory_db = saved
    assert response.status_code == 200, response.text
    assert store.gets.count("p1") == 1, store.gets