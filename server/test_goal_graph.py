"""Contract tests for roadmap item C2 (dependency-aware goal tracking).

These are behaviour contracts, not snapshots. They assert how two pieces of data
relate: a requirement's cost must equal the real gate table, and an unsatisfied
requirement must not advertise an action.
"""
import pytest

from server.goal_graph import goal_view, inner_skill_goals, item_uses
from server.inner_skills import SKILLS


def owned_factory(counts):
    return lambda item_id: counts.get(item_id, 0)


def test_requirements_match_the_real_gate_table():
    """No invented costs: every requirement is readable from inner_skills.SKILLS."""
    player = {"cultivation_realm": "foundation_establishment", "inner_skills": {"skills": {}}}
    for goal in inner_skill_goals(player, owned_factory({})):
        for requirement in goal["requirements"]:
            assert requirement["required"] > 0
            assert requirement["id"] in {s.learn_material for s in SKILLS.values()} | {
                s.tupo_material for s in SKILLS.values()}


def test_unsatisfied_requirement_advertises_no_action():
    player = {"cultivation_realm": "foundation_establishment", "inner_skills": {"skills": {}}}
    view = goal_view(player, owned_factory({}))
    assert view["active"] is not None
    assert view["active"]["eligible_action"] == "", "a blocked goal must not offer an action"
    assert view["blocked"], "a player holding nothing must be shown what is blocking them"


def test_holding_enough_makes_the_goal_actionable():
    """A learn goal becomes actionable when its real cost is met and realm is reached."""
    first = SKILLS["qi_sense"]
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {}}}
    view = goal_view(player, owned_factory({first.learn_material: first.learn_material_amount}))
    active = view["active"]
    assert active["goal_id"] == f"inner_skill_learn:qi_sense"
    assert active["eligible_action"] == active["goal_id"]
    assert active["completed"] is False


def test_realm_gate_blocks_a_mortal_from_body_refinement():
    """body_refinement needs qi_refining; a mortal must see it as a realm gate."""
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {}}}
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned_factory({}))}
    body = goals["inner_skill_learn:body_refinement"]
    assert body["eligible_action"] == ""
    assert body["prerequisite_steps"][0]["completed"] is False


def test_learned_skill_switches_goal_to_the_tupo_cost():
    # The row shape claim() really writes when comprehension finishes. A bare
    # {} row cannot be produced by any write in inner_skills.py, and begin_tupo
    # raises KeyError on it, so it proved nothing about the tupo contract.
    player = {"cultivation_realm": "foundation_establishment",
              "inner_skills": {"skills": {"qi_sense": {"state": "comprehend_complete",
                                                      "level": 1, "es_points": 1}}}}
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned_factory({}))}
    tupo = goals["inner_skill_tupo:qi_sense"]
    assert tupo["requirements"][0]["required"] == SKILLS["qi_sense"].tupo_material_amount
    assert tupo["prerequisite_steps"][0]["completed"] is True


def test_item_uses_reflects_both_learn_and_tupo_consumption():
    uses = item_uses("insight_scroll")
    kinds = {use["goal_id"].split(":")[0] for use in uses}
    assert kinds == {"inner_skill_learn", "inner_skill_tupo"}, "scroll feeds both gates"


def test_unknown_item_id_is_rejected_not_silently_invented():
    """A goal must never reference an item the catalog does not define."""
    from server.goal_graph import _requirement
    with pytest.raises(KeyError):
        _requirement("not_a_real_item", 1, 0, ["shop"])


def test_zero_or_negative_requirement_is_rejected():
    from server.goal_graph import _requirement
    with pytest.raises(ValueError):
        _requirement("insight_scroll", 0, 0, ["shop"])


def test_resolver_is_read_only():
    """Calling the resolver must not mutate the player dict it is handed."""
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {"qi_sense": {"state": "comprehending"}}}}
    before = repr(player)
    goal_view(player, owned_factory({}))
    inner_skill_goals(player, owned_factory({}))
    assert repr(player) == before


def test_comprehension_in_progress_advertises_no_action():
    """A row is written the moment learning STARTS, so key presence is not
    'learned'. The panel must wait out learn_seconds instead of offering a
    breakthrough the server refuses with 409 'Comprehension still in progress'."""
    definition = SKILLS["qi_sense"]
    now = 1_000_000.0
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {
        "qi_sense": {"state": "comprehending", "started_at": now, "level": 0, "es_points": 0}}}}
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned_factory({}), now)}
    goal = goals["inner_skill_learn:qi_sense"]
    assert goal["eligible_action"] == "", "a running comprehension has nothing to press"
    timer = goal["prerequisite_steps"][-1]
    assert timer["completed"] is False, "the timer step is exactly what blocks the action"
    assert timer["remaining_seconds"] == definition.learn_seconds, "reports the real remaining time"


def test_finished_comprehension_becomes_actionable():
    """Once learn_seconds have elapsed the server promotes the row itself, so
    the breakthrough is genuinely legal and must be advertised."""
    definition = SKILLS["qi_sense"]
    now = 1_000_000.0
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {
        "qi_sense": {"state": "comprehending",
                     "started_at": now - definition.learn_seconds - 1,
                     "level": 0, "es_points": 0}}}}
    owned = owned_factory({definition.tupo_material: definition.tupo_material_amount})
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned, now)}
    tupo = goals["inner_skill_tupo:qi_sense"]
    assert tupo["eligible_action"] == tupo["goal_id"]


def test_tupo_in_progress_advertises_no_action():
    """begin_tupo refuses a row already in tupo; only claim() finishes it."""
    definition = SKILLS["qi_sense"]
    now = 1_000_000.0
    player = {"cultivation_realm": "mortal", "inner_skills": {"skills": {
        "qi_sense": {"state": "tupo", "started_at": now, "level": 1, "es_points": 1}}}}
    owned = owned_factory({definition.tupo_material: definition.tupo_material_amount})
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned, now)}
    assert goals["inner_skill_tupo:qi_sense"]["eligible_action"] == ""


def test_fully_learned_skill_is_completed_and_offers_nothing():
    """An active skill is finished forever. It must be marked completed and
    must never advertise a breakthrough the server answers 409 to."""
    now = 1_000_000.0
    player = {"cultivation_realm": "foundation_establishment", "inner_skills": {"skills": {
        "qi_sense": {"state": "active", "level": 2, "es_points": 2, "started_at": None}}}}
    goals = {g["goal_id"]: g for g in inner_skill_goals(player, owned_factory({}), now)}
    tupo = goals["inner_skill_tupo:qi_sense"]
    assert tupo["completed"] is True
    assert tupo["eligible_action"] == ""


def test_every_advertised_action_would_be_accepted_by_the_real_route():
    """The contract this module promises, checked against the real handler:
    whatever goal_graph offers as pressable, begin_tupo must not refuse."""
    import contextlib

    from server.inner_skills import begin_tupo
    from fastapi import HTTPException

    class _Db:
        def __init__(self, player): self.player = player
        @contextlib.contextmanager
        def transaction(self, _pid): yield
        def __getitem__(self, _pid): return self.player

    class _Inv:
        def consume_items(self, _pid, need): return {"ok": True, "consumed": need}

    from server.inner_skills import begin_comprehension

    now = 1_000_000.0
    # An absent key is what an unlearned skill really looks like on disk.
    states = [None, "comprehending", "comprehend_complete", "tupo", "active"]
    refused = []
    accepted = 0
    for state in states:
        skills = {} if state is None else {
            "qi_sense": {"state": state, "started_at": now, "level": 1, "es_points": 1}}
        player = {"cultivation_realm": "foundation_establishment",
                  "inner_skills": {"skills": dict(skills)}}
        definition = SKILLS["qi_sense"]
        owned = owned_factory({definition.tupo_material: definition.tupo_material_amount,
                               definition.learn_material: definition.learn_material_amount})
        goals = inner_skill_goals(player, owned, now)
        for goal in goals:
            if not goal["eligible_action"]:
                continue
            label = state or "unlearned"
            try:
                if goal["goal_id"].startswith("inner_skill_learn:"):
                    begin_comprehension(_Db(player), _Inv(), "p1", "qi_sense", "idem-" + label)
                else:
                    begin_tupo(_Db(player), _Inv(), "p1", "qi_sense", "idem-" + label)
                accepted += 1
            except HTTPException as exc:
                refused.append((label, goal["goal_id"], exc.status_code))
    assert not refused, f"advertised an action the server refuses: {refused}"
    assert accepted > 0, "the sweep proved nothing: nothing was ever actionable"


def test_unknown_realm_in_the_skill_table_does_not_take_down_the_whole_list():
    """A bare list.index raised ValueError and returned nothing to the panel.

    The probe is "mahayana", not "golden_core": goal_graph's ladder is
    inner_skills.REALM_ORDER, which now carries all eight canonical rungs, so
    "golden_core" ranks 3 instead of -1. A real rung is not an unknown realm.
    """
    from server.goal_graph import _realm_index
    assert _realm_index("mahayana") == -1
    assert _realm_index("golden_core") == 3


def test_realm_ladder_is_not_a_second_copy():
    """goal_graph must not re-type the ladder; a copy diverges silently."""
    from server import goal_graph
    from server.inner_skills import REALM_ORDER
    assert goal_graph._REALM_ORDER == list(REALM_ORDER)


def test_item_catalog_is_read_once_per_call():
    """The requirement helper used to re-read and re-parse the whole shared
    item file once per requirement per goal."""
    import server.goal_graph as gg
    calls = {"n": 0}
    real = gg._catalog

    def counting():
        calls["n"] += 1
        return real()

    gg._catalog = counting
    try:
        gg.inner_skill_goals({"cultivation_realm": "mortal", "inner_skills": {"skills": {}}},
                             owned_factory({}))
    finally:
        gg._catalog = real
    assert calls["n"] == 1, f"expected one catalog read, got {calls['n']}"


# ===========================================================================
# FRESH-ACCOUNT RE-ROUTE: verified end to end, 2026-10-04
# ===========================================================================
#
# The re-route under test: every goal used to name its learning material's
# source as the literal "shop", a shelf that sells no insight_scroll at all, so
# the very first goal a new player sees pointed at a place that cannot give
# them what they need. ``_material_sources`` now derives the source list from
# the mission board, so a goal names ``mission_board:<objective_id>``.
#
# These tests exist because "the ids look well formed" is not the same claim as
# "a new player can follow one and end up holding the item". Every assertion
# below reads the INVENTORY STORE or the real route table, never the reply that
# claims to have paid something.
#
# IDENTITY TRAP, paid for once so no test below has to
# ----------------------------------------------------
# ``goal_routes`` and ``mission_board_routes`` both resolve their stores with
# ``from main import ...`` -- the BARE name. A test that reaches the same stores
# through ``server.main`` is holding a second, different module object with its
# own singletons: MEASURED, a claim against ``server.main.app`` answered 200 with
# ``credited_items: [insight_scroll]`` while ``server.main.inventory_store`` still
# read 0, which reads exactly like "the reward was advertised and dropped". It was
# not. Every test below imports ``main`` bare and touches only that identity, so
# a zero here means a zero in production too.

import main as _bare_main            # noqa: E402  -- the identity the routes use
import inventory as _inventory      # noqa: E402
import auth as _auth                # noqa: E402
import mission_board as _board      # noqa: E402
import inner_skills as _isk         # noqa: E402
import goal_graph as _gg            # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _fresh_client(name):
    """A real HTTP client on the production app, with a genuinely NEW player.

    Clears the per-address signup counter first: ``/player/create`` allows five
    per sixty seconds per process, so a file that makes more than five players
    would see 429 and mistake its own accounting for a product failure.
    """
    for attr in ("_SIGNUP_ATTEMPTS", "_SIGNUP_WINDOW_START"):
        bucket = getattr(_bare_main, attr, None)
        if hasattr(bucket, "clear"):
            bucket.clear()
    client = TestClient(_bare_main.app)
    response = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert response.status_code in (200, 201), response.text
    player_id = response.json()["id"]
    return client, player_id


def _get(client, player_id, url, **kw):
    kw.setdefault("headers", {})
    kw["headers"] = {**_auth_headers(player_id), **dict(kw["headers"])}
    return client.get(url, **kw)


def _post(client, player_id, url, **kw):
    kw.setdefault("headers", {})
    kw["headers"] = {**_auth_headers(player_id), **dict(kw["headers"])}
    return client.post(url, **kw)


def _auth_headers(player_id):
    """Bare ``auth``, for the same reason ``main`` is imported bare."""
    return {"Authorization": "Bearer " + _auth.issue_session(player_id)}


def _mounted_paths(app):
    from fastapi.routing import APIRoute
    return {route.path for route in app.routes if isinstance(route, APIRoute)}


def test_the_module_points_at_routes_that_are_actually_mounted():
    """Each place the module can send a player resolves to a real route.

    A goal naming a route that was never mounted is a goal that stays
    incomplete forever, and no amount of well-formedness catches it. Read from
    the live route table rather than from a source grep: a decorator inside a
    function that is never called is still a decorator the source contains.
    """
    paths = _mounted_paths(_bare_main.app)
    for path in ("/goals/{player_id}", "/goals/{player_id}/item/{item_id}",
                 "/mission-board", "/mission-board/claim",
                 "/inner-skills/comprehend", "/inner-skills/tupo", "/inner-skills/claim"):
        assert path in paths, f"goal_graph can send a player to {path}, which is not mounted"


def test_every_goal_identifier_names_a_board_objective_the_board_route_serves():
    """The goal-to-route table, asserted as a set relationship.

    The board route is ``GET /mission-board``, and it reports every objective by
    id. So "the source is real" is exactly "the board's own reply contains this
    objective_id" -- checked against ``BOARD_OBJECTIVES``, read at run time, so
    adding an objective turns this green rather than red.
    """
    real_ids = {row["objective_id"] for row in _board.BOARD_OBJECTIVES}
    view = _gg.goal_view({"cultivation_realm": "mortal", "inner_skills": {"skills": {}}},
                         lambda _item: 0)
    named = 0
    for goal in view["goals"]:
        for requirement in goal["requirements"]:
            for source in requirement["source_ids"]:
                assert source.startswith("mission_board:"), (goal["goal_id"], source)
                assert source.split(":", 1)[1] in real_ids, (goal["goal_id"], source)
                named += 1
    assert named, "no goal named a source, so this proved nothing"


def test_the_learn_goal_action_names_the_route_that_starts_comprehension():
    """``inner_skill_learn:<skill>`` is pressed on the comprehend route.

    The goal id is the ``eligible_action`` the panel hands a client, so the
    client turns that id into a request. This pins which route it is, read from
    the mounted table -- the alternative is a client pressing the wrong verb and
    the goal never advancing.
    """
    paths = _mounted_paths(_bare_main.app)
    assert "/inner-skills/comprehend" in paths
    view = _gg.goal_view({"cultivation_realm": "mortal", "inner_skills": {"skills": {}}},
                         lambda item: 999)
    active = view["active"]
    assert active["goal_id"] == "inner_skill_learn:qi_sense", active["goal_id"]
    assert active["eligible_action"] == active["goal_id"], active


def test_a_fresh_player_walks_the_first_goal_to_the_reward_in_the_bag():
    """THE HEADLINE: new player, real claims, reward read out of the bag.

    Reads ``inventory_store.get_item_count`` on both sides of the claim, never
    the reply body. The reply is built from the same reward dict the handler
    echoed, so it is evidence of intent, not of arrival -- and the defect this
    re-route exists to prevent is exactly a reply that promises an item the bag
    never received.
    """
    client, pid = _fresh_client("GoalRouteFresh")
    try:
        view = _get(client, pid, f"/goals/{pid}").json()
        first = view["goals"][0]
        assert first["goal_id"] == "inner_skill_learn:qi_sense", first["goal_id"]
        requirement = first["requirements"][0]
        assert requirement["id"] == "insight_scroll", requirement
        needed = int(requirement["required"])
        assert requirement["owned"] == 0 and not requirement["satisfied"], requirement
        # No action is advertised until the cost is actually met.
        assert first["eligible_action"] == "", first

        # The goal names board objectives. Follow the FIRST one it names, which
        # is the hunt, and do the real work through the real route.
        source_ids = list(requirement["source_ids"])
        assert source_ids, requirement
        hunt_objective = source_ids[0].split(":", 1)[1]
        assert hunt_objective == "quick_hunt_one", source_ids

        bag_before = _bare_main.inventory_store.get_item_count(pid, "insight_scroll")
        for _ in range(8):
            battle = _post(client, pid, "/combat/auto-battle", json={"player_id": pid})
            assert battle.status_code == 200, battle.text
            if battle.json().get("winner") == "player":
                break
        board = _get(client, pid, "/mission-board", params={"player_id": pid}).json()
        entry = next(o for o in board["objectives"] if o["objective_id"] == hunt_objective)
        assert entry["completed"], entry

        # Claim it. The response is asserted for ok, but the ASSERTION that
        # matters is the next line: the store, not the body.
        claim = _post(client, pid, "/mission-board/claim",
                      json={"player_id": pid, "objective_id": hunt_objective},
                      headers={"Idempotency-Key": f"goalroute:{pid}:{hunt_objective}"})
        assert claim.status_code == 200, claim.text
        assert claim.json()["ok"] is True, claim.text
        advertised = claim.json()["reward"]["material"]
        assert advertised["item_id"] == "insight_scroll", advertised

        bag_after_hunt = _bare_main.inventory_store.get_item_count(pid, "insight_scroll")
        assert bag_after_hunt - bag_before == int(advertised["quantity"]), (
            f"the board advertised {advertised} but the bag moved "
            f"{bag_after_hunt - bag_before}")

        # One quick objective pays one scroll; the goal needs two. Earn the
        # second from the second objective the goal names, so the walk ends on
        # the goal's own terms rather than on an admin grant.
        qi_objective = "quick_qi_ready"
        assert f"mission_board:{qi_objective}" in source_ids, source_ids
        with _bare_main.players_db.transaction(pid):
            _bare_main.players_db[pid]["progress"] = 100.0
        second = _post(client, pid, "/mission-board/claim",
                       json={"player_id": pid, "objective_id": qi_objective},
                       headers={"Idempotency-Key": f"goalroute:{pid}:{qi_objective}"})
        assert second.status_code == 200, second.text

        bag_after = _bare_main.inventory_store.get_item_count(pid, "insight_scroll")
        assert bag_after - bag_before == needed, (
            f"the first goal needs {needed} insight_scroll and the bag moved "
            f"{bag_after - bag_before}")

        # The goal now READS as met, from the store, and advertises its action.
        after = _get(client, pid, f"/goals/{pid}").json()
        active = after["active"]
        assert active["goal_id"] == "inner_skill_learn:qi_sense", active["goal_id"]
        met = active["requirements"][0]
        assert met["satisfied"] is True and met["owned"] == needed, met
        assert active["eligible_action"] == "inner_skill_learn:qi_sense", active

        # And the real route ACCEPTS the advertised action, spending exactly the
        # material the goal named. This is the last link: a goal that reads met
        # but collects HTTP 409 from its own button is the failure mode the
        # stage machine in this module exists to prevent.
        spent_before = _bare_main.inventory_store.get_item_count(pid, "insight_scroll")
        press = _post(client, pid, "/inner-skills/comprehend",
                      json={"player_id": pid, "skill_id": "qi_sense"},
                      headers={"Idempotency-Key": f"goalroute:{pid}:comprehend"})
        assert press.status_code == 200, press.text
        assert press.json()["state"] == "comprehending", press.text
        spent_after = _bare_main.inventory_store.get_item_count(pid, "insight_scroll")
        assert spent_before - spent_after == needed, (
            f"comprehension should spend {needed} insight_scroll, spent "
            f"{spent_before - spent_after}")

        row = _bare_main.players_db.get(pid)
        stored = row["inner_skills"]["skills"]["qi_sense"]
        assert stored["state"] == "comprehending", stored
    finally:
        client.close()


def test_the_item_route_names_only_goal_ids_this_players_reply_carries():
    """Reply SHAPE: no dangling goal id on a fresh account.

    ``/goals/{player_id}/item/{item_id}`` is where a client jumps from an item
    back to the prerequisite goal. A fresh player's reply carries only learn
    goals, so an item panel that also named a breakthrough goal id would send
    the player to a goal the same client was never shown.
    """
    client, pid = _fresh_client("GoalRouteItems")
    try:
        goals = _get(client, pid, f"/goals/{pid}").json()["goals"]
        present = {goal["goal_id"] for goal in goals}
        uses = _get(client, pid, f"/goals/{pid}/item/insight_scroll").json()["uses"]
        assert uses, "the item route must report the learning material's use"
        for use in uses:
            assert use["goal_id"] in present, (
                f"the item route named {use['goal_id']}, absent from this "
                f"player's goals reply {sorted(present)}")
    finally:
        client.close()
