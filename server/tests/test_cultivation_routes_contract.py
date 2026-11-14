"""
CONTRACT TEST: server/cultivation_routes.py -- the six /cultivation-ladder/* routes.

WHAT THIS FILE PINS
------------------
The wire contract of ``register_cultivation_routes``: which paths mount and with
which method, the exact reply keys of each of the six endpoints, which status
each refusal answers, and the five anti-abuse properties the module's own
docstring claims (session-scoped identity, server-owned numbers, an optional
retry label on claim, a REQUIRED one on the two money routes, and the
mount-time proof that all six carry an auth dependency).

It asserts RELATIONSHIPS rather than frozen numbers: the bar and the rate are
read from ``cultivation_config`` at run time, the pillar ceiling and the
attempt cost are read from ``breakthrough``, and the offline cap is read from
``offline_progression``. Rebalancing the economy does not break this file.

WHY THIS FILE BUILDS ITS OWN APP
--------------------------------
``server/tests/wc_test_auth.py`` and most other suites import ``server.main``,
so their result depends on whatever another agent is mid-edit in that 3000-line
import graph. This file mounts ``register_cultivation_routes`` onto a bare
``FastAPI()`` with its own in-memory player store, so it measures this module
and nothing else. It touches ``server/main.py`` nowhere.

One shared-tree hazard is NOT avoidable: the repository root ``conftest.py``
imports ``server.main`` at COLLECTION time, so if a peer is half-way through
editing ``main.py`` every pytest run in the repo aborts before a single test
executes. Re-read ``main.py`` at the cited line before reading that traceback
as a defect in this file.

A SECOND shared-tree hazard bites harder here, and it is worth stating before
anyone reads a red run as this file's bug: this module calls
``lifecycle_guard.check_status_inline``. On 2026-10-03 a peer truncated
``server/lifecycle_guard.py`` to 0 bytes mid-write, and all four write routes
answered 500 with ``AttributeError: module 'lifecycle_guard' has no attribute
'check_status_inline'``. ``git show HEAD:server/lifecycle_guard.py`` still
carries the function (111 lines), which is how the transient was told apart
from a real defect. If the four write routes suddenly 500 together, check that
file's size FIRST.

WHY EVERY TEST MINTS A FRESH PLAYER ID
--------------------------------------
``progression_ledger`` keeps a process-global in-memory ledger in memory mode
(``_MEM_LEDGER``), keyed on ``(player_id, milestone_id, period_key)``. A shared
player id would let one test's claim swallow another's and turn a real 200 into
a silent "already_claimed" replay. Measured: that is what makes this file
order-independent.

Run
---
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD \\
        -u WC_PG_PORT -u PYTHONPATH \\
        python -m pytest server/tests/test_cultivation_routes_contract.py -v
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
import uuid
from typing import Any, Dict, Iterator, Optional

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# In-memory storage must be forced. Without it a single-file run reports a wall
# of errors from an absent database, and reading those as failures sends an
# engineer hunting a bug that does not exist.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # bare, NOT server.auth -- main.py binds the same module object
import breakthrough  # noqa: E402
import cultivation_config  # noqa: E402
import cultivation_routes as cr  # noqa: E402
import idem_gate  # noqa: E402
import lifecycle  # noqa: E402
import offline_progression  # noqa: E402
import ownership_guard  # noqa: E402
import progression_ladders as ladders  # noqa: E402
from player_store import PlayerStore  # noqa: E402


class _Request:
    """The smallest thing ``idem_gate.require_idempotency_key`` accepts.

    The gate reads exactly one attribute -- ``request.headers`` -- and refuses
    anything with a control character or an over-length value. Rather than hand
    roll the namespace arithmetic the gate does, the ceiling tests call the real
    gate through this stub, so a change to the gate's ``route + ":" + key``
    contract breaks them instead of being assumed.
    """

    def __init__(self, headers):
        self.headers = headers


STATE = "/cultivation-ladder/state"
CULTIVATE = "/cultivation-ladder/cultivate"
CLAIM = "/cultivation-ladder/claim"
BT_STATE = "/cultivation-ladder/breakthrough/state"
BT_PREPARE = "/cultivation-ladder/breakthrough/prepare"
BT_ATTEMPT = "/cultivation-ladder/breakthrough/attempt"

#: The six paths and the single method each one answers. Compared as a SET of
#: (path, method) pairs rather than a count, because a module that mounted six
#: and a seventh under the same prefix would satisfy a count check. FastAPI adds
#: ``HEAD`` alongside ``GET``, so the GET arms carry ``{"GET", "HEAD"}``.
DECLARED = (
    (STATE, "GET"),
    (CULTIVATE, "POST"),
    (CLAIM, "POST"),
    (BT_STATE, "GET"),
    (BT_PREPARE, "POST"),
    (BT_ATTEMPT, "POST"),
)

#: The module declares its own paths as data; the mount is checked against that
#: tuple rather than against a list retyped here, so adding a seventh route to
#: the module and forgetting this file fails LOUDLY instead of silently leaving
#: the new path untested.
assert tuple(sorted(cr.CULTIVATION_ROUTES)) == tuple(sorted(p for p, _ in DECLARED)), (
    "this file pins %s but the module declares %s"
    % (sorted(p for p, _ in DECLARED), sorted(cr.CULTIVATION_ROUTES)))

#: The exact reply keys each endpoint returns on its success path, measured on
#: the booted app. Frozen deliberately: a client reads these names, so ADDING a
#: key is safe but RENAMING or DROPPING one breaks every caller.
STATE_KEYS = {
    "ok", "server_now", "realm", "stage", "realm_label", "stage_label",
    "lifespan_days", "progress", "progress_required", "bar_full", "remaining",
    "rate_per_hour", "rate_error", "next_realm", "next_stage", "at_ceiling",
    "away_seconds", "away_hours", "offline_cap_hours", "away_capped",
    "pending_progress", "axis_steps", "unified_stats", "claimable",
    "breakthrough",
}
#: ``reason`` and ``elapsed_seconds`` are in every cultivate reply; the
#: ``no_progress_reason`` label is present ONLY on the dust branch, because the
#: handler adds it after building the reply. Two shapes, measured on both.
CULTIVATE_KEYS_BANKED = {
    "ok", "reason", "progress_gained", "elapsed_seconds", "hours_capped",
    "rate_per_hour", "progress_required", "deduplicated", "state", "persisted",
}
CULTIVATE_KEYS_DUST = CULTIVATE_KEYS_BANKED | {"no_progress_reason"}
#: The claim has THREE measured shapes and the differences are load-bearing.
#: A fresh claim is the widest. The replay is an early return that never reaches
#: the display/axis/progression lines, so ``display_name``, ``axis_key``,
#: ``period_kind``, ``catchup``, ``progression`` and ``persisted`` are ABSENT --
#: a client that assumes they are always present crashes on the second press.
CLAIM_KEYS_FRESH = {
    "ok", "reason", "milestone_id", "display_name", "axis_key", "period_kind",
    "period_key", "paid", "deduplicated", "catchup", "progression",
    "claimable", "persisted",
}
CLAIM_KEYS_REPLAY = {
    "ok", "reason", "milestone_id", "period_key", "paid", "deduplicated",
    "claimable",
}
#: Prepare fresh vs replay: the replay swaps in the stored receipt and adds the
#: two dedupe labels.
PREPARE_KEYS_FRESH = {
    "ok", "pillar", "label", "spent_stones", "gained_points", "value", "max",
    "gate", "at", "state", "persisted",
}
PREPARE_KEYS_REPLAY = PREPARE_KEYS_FRESH | {"reason", "deduplicated"}
#: The attempt delegates its whole body to ``breakthrough.attempt``, so the route
#: adds only two keys of its own: the re-read proof and nothing else.
ATTEMPT_KEYS_FRESH = {"ok", "success", "result", "state", "persisted"}
ATTEMPT_KEYS_REPLAY = ATTEMPT_KEYS_FRESH | {"reason", "deduplicated"}
BT_STATE_KEYS = {
    "ok", "realm", "stage", "realm_label", "stage_label", "progress",
    "progress_required", "bar_full", "target_realm", "target_stage", "gate",
    "gate_open", "chance", "base_chance", "balance_ratio", "can_attempt",
    "blockers", "cooldown_remaining", "stones", "attempt_cost_stones",
    "tribulation_waves", "successes", "failures", "attempts", "temper",
    "last_result", "history",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A mounted app, a fresh player store. One per test.

    Function-scoped on purpose: the once-only claim ledger is process-global in
    memory mode, so a shared world would let one test's claim mark another
    test's player as already claimed and turn a real 200 into a replay.
    """
    players_db = PlayerStore()
    app = FastAPI()
    cr.register_cultivation_routes(app, lambda: players_db)

    yield {
        "app": app,
        "client": TestClient(app, raise_server_exceptions=False),
        "players_db": players_db,
    }


def make_player(world: Dict[str, Any], *, stage: str = "early", stones: int = 0,
                progress: float = 0.0, away_hours: float = 0.0,
                realm: str = "mortal", with_life: bool = True,
                status: Optional[str] = None) -> Dict[str, Any]:
    """One signed-in player with a known bar, purse and away window.

    ``away_hours`` is measured back from NOW, never from an epoch value. With an
    epoch-scale cursor the elapsed-time calculation reads a decades-long gap,
    fills the bar, and every "this player has nothing to bank" arm of the suite
    becomes unreachable -- the assertions would then pass for the wrong reason.

    ``with_life`` defaults to True. On the FIRST touch the lifecycle reconcile
    sets ``last_cultivation_time`` to now, which zeroes the away window; a test
    that wants to measure a real absence must seed a lifecycle so that first
    touch already happened.
    """
    now = time.time()
    pid = "cult-contract-%s" % uuid.uuid4().hex[:12]
    row: Dict[str, Any] = {
        "id": pid,
        "name": pid,
        "cultivation_realm": realm,
        "cultivation_stage": stage,
        "progress": progress,
        "spirit_stones": stones,
        "last_cultivation_time": now - away_hours * 3600.0,
    }
    if with_life:
        lc = lifecycle.new_state(now)
        lc["last_reconciled_at"] = now
        if status is not None:
            lc["status"] = status
        row["lifecycle"] = lc
    world["players_db"][pid] = row
    return {
        "player_id": pid,
        "headers": {"Authorization": "Bearer " + auth.issue_session(pid)},
    }


def call(world: Dict[str, Any], method: str, path: str,
         session: Optional[Dict[str, Any]] = None, body: Any = None,
         params: Optional[Dict[str, Any]] = None, **headers: str):
    """One request. The session is opt-in so no test forgets it silently."""
    h: Dict[str, str] = {}
    if session is not None:
        h.update(session["headers"])
    h.update(headers)
    if method == "GET":
        return world["client"].get(path, headers=h, params=params)
    return world["client"].post(path, headers=h, params=params,
                                json={} if body is None else body)


def row_of(world: Dict[str, Any], player_id: str) -> Dict[str, Any]:
    """The committed row, read back THROUGH the store.

    ``PlayerStore.__getitem__`` hands out a copy on a detached read, so a write
    that never calls ``persist`` lands on a throwaway. Reading the row back is
    the only way to prove a write landed.
    """
    return world["players_db"].get(player_id) or {}


def fill_pillars(world: Dict[str, Any], session: Dict[str, Any], tag: str) -> None:
    """Drive all three pillars to full through the real prepare route.

    Goes over HTTP rather than writing the record, so the gate state the
    attempt sees is the one the routes actually produce. Each press mints a
    unique key: the replay marker is a SINGLE slot, so reusing one key would
    answer with the stored receipt instead of doing the work.
    """
    counter = 0
    for pillar in breakthrough.PILLARS:
        for _ in range(40):
            resp = call(world, "POST", BT_PREPARE, session, {"pillar": pillar},
                        **{"Idempotency-Key": "%s-%s-%d" % (tag, pillar, counter)})
            counter += 1
            if resp.status_code != 200:
                break
            values = {p["pillar"]: p["value"]
                      for p in resp.json().get("gate", {}).get("pillars", [])}
            if values.get(pillar, 0) >= breakthrough.PILLAR_MAX:
                break


# ---------------------------------------------------------------------------
# 1. THE MOUNT -- six paths, six methods, all six proved guarded
# ---------------------------------------------------------------------------

def test_register_mounts_exactly_the_six_declared_paths(world):
    """The app carries the six routes the module declares, and no other.

    Compared as a SET of (path, method) pairs rather than a count: a module
    that mounted six and a seventh under the same prefix would satisfy a count
    check.
    """
    live = set()
    for route in world["app"].routes:
        path = getattr(route, "path", "")
        if not path.startswith("/cultivation-ladder"):
            continue
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD"}
        for method in methods:
            live.add((path, method))

    assert live == set(DECLARED), (
        "the app carries %s; the module declares %s"
        % (sorted(live), sorted(DECLARED)))


def test_the_prefix_does_not_shadow_the_four_legacy_cultivation_routes(world):
    """Nothing under this module answers on the legacy ``/cultivation/`` family.

    The module's whole naming argument is that ``/cultivation/`` already serves
    four live paths in main.py, so a new ``/cultivation/breakthrough/prepare``
    would sit one segment away from them. That argument is only worth anything
    if the mount really did stay away -- and this bare app has no main.py routes
    at all, so a 404 here proves the prefix, not an absence of wiring.
    """
    session = make_player(world)
    for legacy in ("/cultivation/breakthrough", "/cultivation/breakthrough/v2",
                   "/cultivation/claim-offline/v2", "/cultivation/offline-progress"):
        assert call(world, "POST", legacy, session, {}).status_code == 404, (
            "a legacy path answered from this module")


def test_mount_proves_all_six_carry_an_auth_dependency():
    """``register_cultivation_routes`` returns the same six in both lists.

    ``guarded`` is the subset ``ownership_guard.has_auth_dependency`` could
    actually vouch for. A route in ``registered`` but missing from ``guarded``
    means the mount shipped something it could not prove -- which is the exact
    hole the assertion exists to prevent.
    """
    report = cr.register_cultivation_routes(FastAPI(), lambda: PlayerStore())

    assert sorted(report["registered"]) == sorted(cr.CULTIVATION_ROUTES)
    assert sorted(report["guarded"]) == sorted(cr.CULTIVATION_ROUTES), (
        "registered %s but proved guarded %s"
        % (sorted(report["registered"]), sorted(report["guarded"])))


def test_mount_refuses_rather_than_shipping_an_unguarded_route(monkeypatch):
    """A mount that cannot prove its own guard raises instead of shipping a hole.

    Asserted by forcing the guard's OWN predicate to fail, which monkeypatch
    restores afterwards. This is the property that makes the hand-wired
    ``Depends(require_player)`` load-bearing: without the refusal, dropping one
    dependency would leave an open route and a comment.
    """
    monkeypatch.setattr(ownership_guard, "has_auth_dependency", lambda d: False)
    with pytest.raises(RuntimeError) as excinfo:
        cr.register_cultivation_routes(FastAPI(), lambda: PlayerStore())

    message = str(excinfo.value)
    assert "refused to mount" in message
    # Every unmounted path is named, so the failure says WHICH route is open.
    assert STATE in message and BT_ATTEMPT in message


def test_the_six_routes_answer_only_their_own_method(world):
    """A POST route answers 405 to GET, and a GET route answers 405 to POST.

    Asserted with a control: the same client and session do get a 200 from the
    matching verb. Without that arm a blanket 405 would satisfy both assertions.
    """
    session = make_player(world, stones=1000)

    assert call(world, "GET", STATE, session).status_code == 200
    assert call(world, "POST", CULTIVATE, session, {}).status_code == 200
    assert call(world, "GET", CULTIVATE, session).status_code == 405
    assert call(world, "POST", STATE, session, {}).status_code == 405
    assert call(world, "POST", BT_STATE, session, {}).status_code == 405
    assert call(world, "GET", CLAIM, session).status_code == 405
    assert call(world, "GET", BT_PREPARE, session).status_code == 405
    assert call(world, "GET", BT_ATTEMPT, session).status_code == 405


# ---------------------------------------------------------------------------
# 2. AUTH -- session-scoped identity on all six
# ---------------------------------------------------------------------------

ANON_ARMS = [
    (STATE, "GET", None),
    (BT_STATE, "GET", None),
    (CULTIVATE, "POST", {}),
    (CLAIM, "POST", {"milestone_id": "catchup_daily"}),
    (BT_PREPARE, "POST", {"pillar": "qi"}),
    (BT_ATTEMPT, "POST", {}),
]


@pytest.mark.parametrize("path,method,body", ANON_ARMS)
def test_every_route_refuses_an_anonymous_caller(world, path, method, body):
    """No session answers 401 on any of the six, including the two pure reads.

    The reads are the interesting half. None of these paths names a player in
    the path, the query or the body, so ``ownership_guard``'s shape match skips
    them BY DESIGN -- the hand-wired ``Depends(require_player)`` is the whole
    protection, and this is the test that proves it is still there.
    """
    headers = {"Idempotency-Key": "anon"} if path in (BT_PREPARE, BT_ATTEMPT) else {}
    assert call(world, method, path, None, body, **headers).status_code == 401


@pytest.mark.parametrize("path,method,body", ANON_ARMS[:4])
def test_a_garbage_token_is_treated_as_anonymous_and_answers_401(world, path, method, body):
    """A malformed token is ANONYMOUS, not a stranger: 401, never 200.

    Worth stating because probing the stranger arm with junk is a classic way to
    make a correctly-guarded route look open -- the status a caller sees for
    "your token is nonsense" and "your token belongs to someone else" differ,
    and only the second one proves a route resolves identity at all.
    """
    junk = {"headers": {"Authorization": "Bearer not-a-real-token"}}
    assert call(world, method, path, junk, body).status_code == 401


def test_a_session_cannot_steer_a_write_at_another_players_row(world):
    """Naming another player in the query answers 403, and their row does not move.

    The strongest form of the ownership claim: not merely "the reply is about my
    row" but "a caller who names somebody else is refused". Checked in the
    query because ``extra="forbid"`` would 422 a body field, and 422 would be a
    shape answer rather than a permission one.
    """
    victim = make_player(world, stones=5000, progress=0.0)
    attacker = make_player(world, stones=5000)

    resp = call(world, "POST", CLAIM, attacker,
                {"milestone_id": "catchup_daily"},
                params={"player_id": victim["player_id"]})
    assert resp.status_code == 403
    assert row_of(world, victim["player_id"]).get("progress") == 0.0, (
        "the victim's row moved after a foreign session named it")


def test_a_session_only_ever_sees_its_own_row(world):
    """Two players, one client: each reply is built from that caller's row."""
    a = make_player(world, stones=100, away_hours=3.0)
    b = make_player(world, stones=9999, away_hours=0.0)

    body_a = call(world, "GET", STATE, a).json()
    body_b = call(world, "GET", STATE, b).json()

    assert body_a["away_hours"] > 0.0
    assert body_b["away_hours"] == 0.0
    assert call(world, "GET", BT_STATE, a).json()["stones"] == 100
    assert call(world, "GET", BT_STATE, b).json()["stones"] == 9999


# ---------------------------------------------------------------------------
# 3. A MISSING PLAYER ROW
# ---------------------------------------------------------------------------

def test_a_session_for_a_player_that_does_not_exist_answers_404(world):
    """A valid session with no row is a 404, not a 500 and not a 200.

    Measured through a dict-backed store double, which is the harness the module
    explicitly supports: ``_scope`` falls back to ``contextlib.nullcontext``
    when the store has no ``transaction``. See the sibling test below for why
    the real store cannot reach this branch.
    """
    dict_store: Dict[str, Any] = {}
    bare_app = FastAPI()
    cr.register_cultivation_routes(bare_app, lambda: dict_store)
    client = TestClient(bare_app, raise_server_exceptions=False)
    ghost = {"Authorization": "Bearer " + auth.issue_session("ghost-no-row")}

    for path, body, extra in ((STATE, None, {}), (BT_STATE, None, {}),
                              (CULTIVATE, {}, {}),
                              (CLAIM, {"milestone_id": "catchup_daily"}, {}),
                              (BT_PREPARE, {"pillar": "qi"}, {"Idempotency-Key": "g1"})):
        headers = dict(ghost)
        headers.update(extra)
        if path in (STATE, BT_STATE):
            resp = client.get(path, headers=headers)
        else:
            resp = client.post(path, headers=headers, json=body)
        assert resp.status_code == 404, "%s answered %s" % (path, resp.status_code)
        assert resp.json()["detail"] == "Player not found"


def test_a_missing_row_500s_on_the_real_store_and_that_is_a_defect_not_a_contract(world):
    """KNOWN GAP, pinned so it cannot rot silently: the 404 is unreachable on ``PlayerStore``.

    ``_player`` raises ``HTTPException(404)`` for a missing row, but every write
    route opens ``_scope`` FIRST, and ``PlayerStore.transaction`` does
    ``self[player_id]``, which raises ``KeyError`` before the handler ever
    reaches the 404 branch. Measured 2026-10-03 on the booted app: all six paths
    answer 500 for a ghost session.

    This test asserts the WIRE as measured, and its name says why. When the
    store's transaction stops raising, this test goes red and the 404 becomes
    reachable -- at which point delete this test and keep the one above.
    """
    ghost = {"headers": {"Authorization": "Bearer " + auth.issue_session("ghost-no-row")}}
    resp = call(world, "GET", STATE, ghost)
    assert resp.status_code == 500, (
        "the ghost-row 500 is gone -- the documented 404 is now reachable; "
        "delete this test and keep test_a_session_for_a_player_that_does_not_exist_answers_404")


# ---------------------------------------------------------------------------
# 4. THE BODY SHAPE -- extra="forbid", and NO caller-owned numbers
# ---------------------------------------------------------------------------

CANNED_NUMBER_ARMS = [
    (CULTIVATE, {"progress": 100.0}),
    (CULTIVATE, {"hours": 99999}),
    (CULTIVATE, {"qi": 1000.0}),
    (CLAIM, {"milestone_id": "cultivation_step_1", "reward": {"qi": 1000000.0}}),
    (CLAIM, {"milestone_id": "cultivation_step_1", "period_key": "0"}),
    (BT_PREPARE, {"pillar": "qi", "stones": 0}),
    (BT_PREPARE, {"pillar": "qi", "amount": 10 ** 9}),
    (BT_ATTEMPT, {"chance": 1.0}),
    (BT_ATTEMPT, {"realm": "immortal"}),
]


@pytest.mark.parametrize("path,body", CANNED_NUMBER_ARMS)
def test_a_body_naming_a_number_the_server_owns_is_refused(world, path, body):
    """Every caller-owned number is a 422, never a silently accepted 200.

    This is the anti-abuse claim in its most testable form and the whole reason
    the models are ``extra="forbid"``. With pydantic's default an unknown field
    is DROPPED and the route answers 200, which reads to a client as "the
    server took my number". Each field here is one the module's own docstring
    names as server-owned: the hours, the gain, the reward, the period, the
    stone cost, the chance, the realm.
    """
    session = make_player(world, stones=100000, progress=100.0)
    headers = {"Idempotency-Key": "shape"} if path in (BT_PREPARE, BT_ATTEMPT) else {}
    resp = call(world, "POST", path, session, body, **headers)

    assert resp.status_code == 422, (
        "%s accepted a caller-owned number: %s" % (path, resp.text[:200]))
    assert resp.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("path,missing", [
    (CLAIM, "milestone_id"),
    (BT_PREPARE, "pillar"),
])
def test_a_missing_required_field_is_a_422_that_names_the_field(world, path, missing):
    """A shape error answers 422 rather than reaching the handler as a 500.

    Only these two models have a required field. ``CultivateRequest`` and
    ``AttemptRequest`` carry nothing but the optional retry label, so an empty
    body is well-formed on both -- see the next test, which pins that.
    """
    session = make_player(world, stones=1000)
    headers = {"Idempotency-Key": "shape"} if path == BT_PREPARE else {}
    resp = call(world, "POST", path, session, {}, **headers)

    assert resp.status_code == 422, resp.text[:200]
    assert resp.json()["detail"][0]["loc"] == ["body", missing]


def test_an_empty_body_is_legal_on_cultivate_as_well_as_attempt(world):
    """Two of the four POST models have NO required field, and ``{}`` is valid.

    Measured on both: an empty cultivate body answers 200 with the same keys as
    any other press. This is the reason the four write routes are not
    interchangeable -- a test that assumes "empty body means shape error" is
    wrong on two of them.
    """
    session = make_player(world, stones=1000, away_hours=4.0)

    cultivate = call(world, "POST", CULTIVATE, session, {})
    assert cultivate.status_code == 200, cultivate.text[:200]
    assert set(cultivate.json()) == CULTIVATE_KEYS_BANKED

    attempt = call(world, "POST", BT_ATTEMPT, session, {}, **{"Idempotency-Key": "empty"})
    assert attempt.status_code == 409, attempt.text[:200]


def test_the_optional_body_label_is_bounded(world):
    """``idempotency_key`` in the BODY is bounded by the same 128 as the header.

    A hostile caller must not be able to write an unbounded string onto the
    player's row through the body, which the header gate alone would not stop.
    """
    session = make_player(world)
    resp = call(world, "POST", CULTIVATE, session,
                {"idempotency_key": "x" * (cr.MAX_IDEM_KEY_LEN + 1)})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "string_too_long"


def test_the_claim_milestone_id_is_bounded_at_both_ends(world):
    """An empty milestone id and a 65-character one are both 422."""
    session = make_player(world)
    assert call(world, "POST", CLAIM, session, {"milestone_id": ""}).status_code == 422
    assert call(world, "POST", CLAIM, session, {"milestone_id": "m" * 65}).status_code == 422


# ---------------------------------------------------------------------------
# 5. GET /cultivation-ladder/state -- the screen's own numbers
# ---------------------------------------------------------------------------

def test_state_returns_every_documented_key_and_agrees_with_the_config_tables(world):
    """The reply names every field a client draws the screen from.

    Every number is compared against ``cultivation_config``'s OWN table, read at
    run time, so a rebalance of the bar or the rate does not fail this file --
    but a route that started computing its own copy of those numbers would.
    """
    session = make_player(world, away_hours=2.0)
    body = call(world, "GET", STATE, session).json()

    assert set(body) == STATE_KEYS, (
        "the state reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(STATE_KEYS)))
    assert body["ok"] is True
    assert body["realm"] == "mortal"
    assert body["progress_required"] == cultivation_config.stage_progress_required("mortal", "early")
    assert body["rate_per_hour"] == cultivation_config.stage_rate_per_hour("mortal", "early")
    assert body["offline_cap_hours"] == offline_progression.MAX_OFFLINE_HOURS
    assert body["rate_error"] is None
    # The axis keys are the module's own, not a list retyped here.
    assert set(body["axis_steps"]) == set(ladders.AXIS_KEYS)
    # Every window figure derives from ONE clock read, so they agree.
    assert body["away_seconds"] == pytest.approx(body["away_hours"] * 3600.0, abs=1.0)
    assert body["away_seconds"] <= cr.MAX_OFFLINE_SECONDS
    assert body["away_capped"] is False


def test_state_bar_relationships_hold_at_empty_and_at_full(world):
    """``bar_full`` and ``remaining`` are a function of ``progress``, at both ends."""
    empty = make_player(world, progress=0.0)
    body = call(world, "GET", STATE, empty).json()
    assert body["bar_full"] is False
    assert body["remaining"] == pytest.approx(body["progress_required"], abs=0.01)

    full = make_player(world, progress=1000.0)
    body = call(world, "GET", STATE, full).json()
    assert body["bar_full"] is True
    assert body["remaining"] == 0.0


def test_state_is_a_read_and_writes_nothing(world):
    """A GET must not publish a write, even though the view helper mutates.

    ``breakthrough.state_of`` installs a default ``breakthrough`` record on the
    dict it is handed, so a GET that passed the live row straight through would
    publish that write when the scope closed. The module deep-copies first for
    exactly this reason. Measured by comparing the whole row across two reads,
    so a lazily-created record would have had time to land.
    """
    session = make_player(world)
    pid = session["player_id"]

    call(world, "GET", STATE, session)
    first = dict(row_of(world, pid))
    call(world, "GET", STATE, session)
    second = dict(row_of(world, pid))

    assert first == second, (
        "the read changed the row; a later read shows %s" % (set(second) - set(first)))


def test_state_reports_a_config_gap_rather_than_inventing_a_rate(world):
    """An unknown stage answers 200 with ``rate_error`` set and the rate at zero.

    The module's stated rule is that a stage the table does not describe is a
    CONFIGURATION GAP and is reported as one, never papered over with a number
    indistinguishable from a tuned one. Both halves are asserted: the error is
    non-null AND the rate is zero, so a client can tell "broken config" from
    "a slow stage".
    """
    session = make_player(world, stage="not_a_stage")
    body = call(world, "GET", STATE, session).json()

    assert body["ok"] is True
    assert body["rate_error"] is not None
    assert body["rate_per_hour"] == 0.0
    assert body["stage"] == "not_a_stage"
    # Nothing to advance to, so the screen reports the ceiling rather than
    # inventing a next rung.
    assert body["at_ceiling"] is True
    assert body["next_realm"] is None


def test_state_lists_claimable_milestones_and_the_whole_breakthrough_gate(world):
    """The one reply a client needs to draw the screen carries both sub-views."""
    body = call(world, "GET", STATE, make_player(world, stones=250)).json()

    assert isinstance(body["claimable"], list)
    assert all("milestone_id" in row and "period_key" in row for row in body["claimable"])
    assert set(body["breakthrough"]) == set(BT_STATE_KEYS)
    assert body["breakthrough"]["attempt_cost_stones"] == breakthrough.ATTEMPT_STONE_COST


# ---------------------------------------------------------------------------
# 6. GET /cultivation-ladder/breakthrough/state -- the gate, priced
# ---------------------------------------------------------------------------

def test_breakthrough_state_returns_every_documented_key_and_the_module_tables(world):
    """The gate the screen draws is the gate the attempt is judged against.

    Both sides are read from ``breakthrough`` at run time, so this is a
    relationship: a client cannot be shown a cost the charge does not use.
    """
    session = make_player(world, stones=4321, progress=100.0)
    body = call(world, "GET", BT_STATE, session).json()

    assert set(body) == BT_STATE_KEYS, (
        "the breakthrough reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(BT_STATE_KEYS)))
    assert body["stones"] == 4321
    assert body["attempt_cost_stones"] == breakthrough.ATTEMPT_STONE_COST
    assert [p["pillar"] for p in body["gate"]["pillars"]] == list(breakthrough.PILLARS)
    for pillar in body["gate"]["pillars"]:
        assert pillar["max"] == breakthrough.PILLAR_MAX
    assert body["bar_full"] is True
    # A full bar with empty pillars is NOT an open gate. If these two ever read
    # the same, the blockers list is decorative.
    assert body["gate_open"] is False
    assert body["can_attempt"] is False
    assert body["blockers"]


# ---------------------------------------------------------------------------
# 7. POST /cultivation-ladder/cultivate -- bank elapsed time, never promote
# ---------------------------------------------------------------------------

def test_cultivate_banks_the_rate_times_the_elapsed_window(world):
    """The credited figure is ``rate x elapsed``, compared against the tables.

    The elapsed window is read from the ROW against the server's clock, so the
    assertion derives the same product from the same two tables rather than
    freezing a literal. A route that took the window from the body would fail
    here, because no body field can carry one (see the shape tests above).
    """
    session = make_player(world, away_hours=10.0)
    body = call(world, "POST", CULTIVATE, session, {}).json()

    expected_rate = cultivation_config.stage_rate_per_hour("mortal", "early")
    assert body["reason"] == "cultivated"
    assert body["rate_per_hour"] == expected_rate
    assert body["elapsed_seconds"] == pytest.approx(10 * 3600.0, abs=5.0)
    assert body["progress_gained"] == pytest.approx(expected_rate * 10.0, abs=0.5)
    assert body["hours_capped"] is False
    assert set(body) == CULTIVATE_KEYS_BANKED
    # The write landed on the row, not just in a dict the handler is dropping.
    assert body["persisted"]["progress"] == pytest.approx(body["progress_gained"], abs=0.01)
    assert row_of(world, session["player_id"])["progress"] == pytest.approx(
        body["progress_gained"], abs=0.01)


def test_cultivate_clamps_the_elapsed_window_at_the_imported_offline_cap(world):
    """A year-long absence is paid as the cap, and says so.

    ``hours_capped`` is the label; the relationship is that the elapsed figure
    reported is the cap and never more. Read from ``offline_progression`` so a
    change to the cap does not fail this file.
    """
    session = make_player(world, away_hours=365 * 24)
    body = call(world, "POST", CULTIVATE, session, {}).json()

    assert body["hours_capped"] is True
    assert body["elapsed_seconds"] == pytest.approx(cr.MAX_OFFLINE_SECONDS, abs=1.0)
    assert body["elapsed_seconds"] <= cr.MAX_OFFLINE_SECONDS
    assert cr.MAX_OFFLINE_SECONDS == pytest.approx(offline_progression.MAX_OFFLINE_HOURS * 3600.0)


def test_cultivate_clamps_the_gain_to_the_remainder_of_the_bar(world):
    """Time alone can never walk past the stage into the next one.

    The player starts one point short of full and has been away far longer than
    the bar needs. The gain is the REMAINDER, not the rate times the window --
    this is what stops a single press chaining a second stage.
    """
    required = cultivation_config.stage_progress_required("mortal", "early")
    session = make_player(world, progress=required - 1.0, away_hours=100.0)
    body = call(world, "POST", CULTIVATE, session, {}).json()

    assert body["progress_gained"] == pytest.approx(1.0, abs=0.01)
    assert body["state"]["progress"] == pytest.approx(required, abs=0.01)
    assert body["state"]["bar_full"] is True


def test_a_second_press_in_the_same_second_pays_nothing(world):
    """Cultivate is naturally once-only: the cursor advances, so a retry is free.

    A caller retrying after a dropped response cannot double-bank, and does not
    need to for the route to be safe. The reply says ``nothing_to_bank`` rather
    than reporting dust as a bank, and carries the extra label that says why.
    """
    session = make_player(world, away_hours=10.0)
    first = call(world, "POST", CULTIVATE, session, {}).json()
    second = call(world, "POST", CULTIVATE, session, {}).json()

    assert first["reason"] == "cultivated"
    assert second["reason"] == "nothing_to_bank"
    assert second["progress_gained"] == 0.0
    assert set(second) == CULTIVATE_KEYS_DUST
    assert second["no_progress_reason"] == "nothing_to_bank"
    # The cursor advanced on the first press, which is the whole mechanism.
    assert row_of(world, session["player_id"])["progress"] == pytest.approx(
        first["progress_gained"], abs=0.01)


def test_a_first_press_by_a_row_with_no_lifecycle_banks_nothing(world):
    """A pre-clock row cannot claim a retroactive windfall.

    On the FIRST touch the lifecycle reconcile sets ``last_cultivation_time`` to
    now, so a row written before the clock started measures zero elapsed rather
    than the age of the row. Measured with a 50-hour absence that is discarded.
    """
    session = make_player(world, away_hours=50.0, with_life=False)
    body = call(world, "POST", CULTIVATE, session, {}).json()

    assert body["elapsed_seconds"] == 0.0
    assert body["progress_gained"] == 0.0
    assert body["reason"] == "nothing_to_bank"


def test_cultivate_refuses_a_stage_the_config_table_does_not_describe(world):
    """A config gap is a 409 naming the gap, not an invented rate.

    The read surfaces the same gap as a 200 with ``rate_error``; the WRITE
    refuses, because inventing a rate here would silently pay the player
    nothing (or something) for time they earned.
    """
    session = make_player(world, stage="not_a_stage")
    resp = call(world, "POST", CULTIVATE, session, {})

    assert resp.status_code == 409, resp.text[:200]
    detail = resp.json()["detail"]
    assert detail["reason"] == "no_configured_rate"
    assert detail["stage"] == "not_a_stage"
    assert detail["realm"] == "mortal"


def test_cultivate_never_promotes_a_realm_or_a_stage(world):
    """Filling the bar does not roll for a breakthrough. This is the design.

    A player banked past the ceiling keeps the realm and stage they had; the
    only route that may promote is the attempt, which is gated, priced and
    risky. The bar-full FLAG is written; the promotion is not.
    """
    session = make_player(world, away_hours=500.0)
    call(world, "POST", CULTIVATE, session, {})
    row = row_of(world, session["player_id"])

    assert row["cultivation_realm"] == "mortal"
    assert row["cultivation_stage"] == "early"
    assert row.get("breakthrough_ready") is True


# ---------------------------------------------------------------------------
# 8. POST /cultivation-ladder/claim -- once per period, header is not the guard
# ---------------------------------------------------------------------------

def test_claim_answers_404_for_a_milestone_the_server_does_not_own(world):
    """A milestone id is a LOOKUP KEY: an unknown one is a 404 naming the miss."""
    session = make_player(world)
    resp = call(world, "POST", CLAIM, session, {"milestone_id": "not_a_milestone"})

    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "unknown_milestone"


def test_claim_refuses_an_unearned_milestone_with_409_and_burns_nothing(world):
    """The requirement check runs BEFORE the reservation, so a refusal costs nothing.

    Two things are asserted together because either alone is satisfiable by a
    broken route: the 409 carries the reason and the player's own axis steps,
    and the SAME milestone is still claimable by a player who HAS earned it --
    which it could not be if the refusal had reserved the once-per-period key.
    """
    session = make_player(world, stage="early")
    milestone = str(ladders.MILESTONES[0]["milestone_id"])
    assert ladders.requirement_met({}, ladders.MILESTONES[0]["requirement"]) is False

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone})
    assert resp.status_code == 409, resp.text[:200]
    detail = resp.json()["detail"]
    assert detail["reason"] == "requirement_not_met"
    assert detail["axis_steps"] == ladders.axis_steps(row_of(world, session["player_id"]))

    earned = make_player(world, stage="mid_late")
    follow = call(world, "POST", CLAIM, earned, {"milestone_id": milestone})
    assert follow.status_code == 200, (
        "the refusal burned the key: %s" % follow.text[:200])


def test_claim_pays_the_catalogue_reward_and_answers_every_documented_key(world):
    """A fresh claim pays what the SERVER's catalogue says and names every field.

    The amount is compared against ``validate_reward`` on the milestone's own
    row, so re-pricing the ladder does not fail this file.
    """
    seed = {"cultivation_realm": "mortal", "cultivation_stage": "mid_late"}
    milestone = next(m for m in ladders.MILESTONES
                     if ladders.requirement_met(seed, m["requirement"]))
    expected = ladders.validate_reward(milestone["reward"])
    session = make_player(world, stage="mid_late")

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone["milestone_id"]})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert set(body) == CLAIM_KEYS_FRESH, (
        "a fresh claim carries %s; the pinned contract is %s"
        % (sorted(body), sorted(CLAIM_KEYS_FRESH)))
    assert body["reason"] == "claimed"
    assert body["deduplicated"] is False
    assert body["milestone_id"] == milestone["milestone_id"]
    assert body["display_name"] == milestone["display_name"]
    assert body["period_kind"] == milestone["period_kind"]
    assert body["paid"]["qi"] == expected.get("qi", 0.0)
    # The period key is the SERVER's, derived from its clock.
    assert body["period_key"] == ladders.period_key_for(milestone["period_kind"], time.time())


def test_a_replay_is_answered_with_the_first_receipt_not_a_refusal(world):
    """A second press in the same period returns what the FIRST press paid.

    The client that timed out after a successful payout needs to learn what it
    already received; a synthesised answer would be a guess about somebody's
    currency. The reply is also a DIFFERENT SHAPE, and a client assuming the
    fresh keys are always present crashes on the second press.
    """
    session = make_player(world, stage="mid_late")
    milestone = "cultivation_step_1"

    first = call(world, "POST", CLAIM, session, {"milestone_id": milestone})
    assert first.status_code == 200
    second = call(world, "POST", CLAIM, session, {"milestone_id": milestone})

    assert second.status_code == 200
    body = second.json()
    assert set(body) == CLAIM_KEYS_REPLAY, (
        "the replay carries %s; the pinned contract is %s"
        % (sorted(body), sorted(CLAIM_KEYS_REPLAY)))
    assert body["reason"] == "already_claimed"
    assert body["deduplicated"] is True
    assert body["paid"] == first.json()["paid"]


def test_a_fresh_idempotency_key_per_press_still_loses_the_second_payout(world):
    """The header is NOT the once-only guarantee, and this is the proof.

    Four presses: three with DIFFERENT body keys and one with no key at all.
    The second and later are still replays. The guarantee is the
    ``(player, milestone, period)`` key in the ledger, and the header is not
    part of it -- which is why this route deliberately does not require one.
    """
    session = make_player(world, stage="mid_late")

    first = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert first.json()["reason"] == "claimed"
    for key in ("fresh-A", "fresh-B"):
        replay = call(world, "POST", CLAIM, session,
                      {"milestone_id": "cultivation_step_1", "idempotency_key": key})
        assert replay.json()["reason"] == "already_claimed", (
            "a fresh key minted a second payout: %s" % replay.text[:200])
    no_key = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert no_key.json()["reason"] == "already_claimed"

    # Paid once, not four times.
    assert row_of(world, session["player_id"])["progress"] == pytest.approx(
        first.json()["paid"]["qi"], abs=0.01)


def test_claim_does_not_require_an_idempotency_header(world):
    """The claim route answers 200 with no ``Idempotency-Key`` at all.

    Required on the two money routes, never minted here. Requiring it would
    teach clients that the header is what makes a claim safe, which is the
    belief the ledger's primary key exists to remove.
    """
    session = make_player(world, stage="mid_late")
    resp = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert resp.status_code == 200
    assert "Idempotency-Key" not in session["headers"]


def test_a_catchup_claim_pays_the_absence_not_the_flat_catalogue_figure(world):
    """A catch-up row is priced off the elapsed absence, with decay.

    Compared against ``progression_ladders``'s own evaluator rather than a
    literal, so the rate and the decay table can be retuned without breaking
    this file. What is pinned is the RELATION: the amount equals the evaluated
    catch-up, not the flat reward the catalogue carries.
    """
    session = make_player(world, stage="mid_late", away_hours=5.0)
    milestone = next(m for m in ladders.MILESTONES
                     if m.get("is_catchup") and m["period_kind"] == "daily")

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone["milestone_id"]})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert body["reason"] == "claimed"
    assert body["catchup"]["eligible"] is True
    assert body["catchup"]["absence_hours"] == pytest.approx(5.0, abs=0.05)
    assert body["paid"]["qi"] == pytest.approx(body["catchup"]["amount_qi"], abs=0.01)
    # The decayed absence is deliberately NOT the flat catalogue figure.
    assert body["paid"]["qi"] != float(milestone["reward"]["qi"])
    # The absence is consumed, so the next claim measures the NEXT absence.
    assert body["catchup"]["next_multiplier"] == pytest.approx(
        ladders.catchup_multiplier(1), abs=0.001)


def test_a_catchup_claim_under_one_hour_away_is_refused_with_409(world):
    """A player who pokes the button ten times a second is paid nothing.

    The refusal happens before the reservation, so it does not burn the
    once-per-period key either -- the same rule the requirement check follows.
    """
    session = make_player(world, stage="mid_late", away_hours=0.1)
    milestone = next(m for m in ladders.MILESTONES
                     if m.get("is_catchup") and m["period_kind"] == "daily")

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone["milestone_id"]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "catchup_not_eligible"
    assert detail["catchup"]["eligible"] is False


def test_a_catchup_payout_is_clamped_to_the_remainder_and_says_so(world):
    """The receipt names the un-clamped figure rather than shaving it quietly.

    The absence is priced at the catch-up rate for up to the cap, which can
    exceed the stage still needs. Clamping to the remainder keeps the preview in
    ``pending_progress`` and the payout telling the same story; a player who
    cannot see why the payout shrank reads it as a bug.

    MEASURED, and a docstring could mislead here: the clamp is NOT applied to
    ``amount_qi``. That field keeps the raw evaluated figure; the clamped one is
    published separately as ``clamped_to_remainder``, alongside
    ``amount_qi_before_clamp`` and ``bar_remainder``. What the player is actually
    credited is ``paid``, and that IS the clamped figure. A test asserting
    ``amount_qi <= room`` fails against the real reply.
    """
    required = cultivation_config.stage_progress_required("mortal", "middle")
    session = make_player(world, stage="mid_late", away_hours=48.0, progress=required - 2.0)
    milestone = next(m for m in ladders.MILESTONES
                     if m.get("is_catchup") and m["period_kind"] == "daily")

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone["milestone_id"]})
    assert resp.status_code == 200, resp.text[:300]
    catchup = resp.json()["catchup"]

    # The three clamp-audit fields exist only when the clamp actually bit.
    assert catchup["bar_remainder"] == pytest.approx(2.0, abs=0.01)
    assert catchup["clamped_to_remainder"] == pytest.approx(2.0, abs=0.01)
    # The un-clamped figure is retained as the auditable record.
    assert catchup["amount_qi_before_clamp"] > catchup["clamped_to_remainder"]
    assert catchup["amount_qi"] == pytest.approx(catchup["amount_qi_before_clamp"], abs=0.01)
    # The player is credited the CLAMPED figure, and the bar lands exactly full.
    assert resp.json()["paid"]["qi"] == pytest.approx(catchup["clamped_to_remainder"], abs=0.01)
    assert row_of(world, session["player_id"])["progress"] == pytest.approx(required, abs=0.01)


# ---------------------------------------------------------------------------
# 9. POST /cultivation-ladder/breakthrough/prepare -- spends stones, needs a key
# ---------------------------------------------------------------------------

def test_prepare_refuses_a_missing_blank_or_oversized_idempotency_key(world):
    """The header is required and is never minted by the server.

    Three refusals, all 400: absent, whitespace-only, and empty. Blank matters
    because a present-but-empty header is falsy, so an ``or`` fallback would
    treat it as absent and serve the request anyway. The fourth arm is the
    ceiling, refused by the header gate rather than by the body bound.
    """
    session = make_player(world, stones=1000)
    for headers in ({}, {"Idempotency-Key": "  "}, {"Idempotency-Key": ""}):
        resp = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
        assert resp.status_code == 400, "%s answered %s" % (headers, resp.status_code)
        assert "will not mint one" in resp.json()["detail"]

    over = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"},
                **{"Idempotency-Key": "x" * (cr.MAX_IDEM_KEY_LEN + 1)})
    assert over.status_code == 400
    assert "too long" in over.json()["detail"]


def test_prepare_refuses_a_pillar_outside_the_declared_three(world):
    """The pillar is checked against ``breakthrough.PILLARS`` in the handler too.

    A 422, not a 500: a future rename in one place must refuse cleanly instead
    of exploding inside the engine. The expected list is the module's own, so
    this follows a rename rather than freezing three names.
    """
    session = make_player(world, stones=1000)
    resp = call(world, "POST", BT_PREPARE, session, {"pillar": "bogus"},
                **{"Idempotency-Key": "p-bogus"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "unknown_pillar"
    assert detail["expected"] == list(breakthrough.PILLARS)


@pytest.mark.parametrize("pillar", list(breakthrough.PILLARS))
def test_prepare_spends_stones_and_raises_exactly_one_pillar(world, pillar):
    """Each of the three declared pillars spends stones and moves its own value.

    The cost and the yield come from ``breakthrough``'s own tables, so a retune
    does not fail this file -- but a route that computed its own copy of a price
    would, because the balance delta is compared to the reply.
    """
    session = make_player(world, stones=10000)
    before = call(world, "GET", BT_STATE, session).json()["stones"]

    resp = call(world, "POST", BT_PREPARE, session, {"pillar": pillar},
                **{"Idempotency-Key": "p-%s" % pillar})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert set(body) == PREPARE_KEYS_FRESH, (
        "prepare carries %s; the pinned contract is %s"
        % (sorted(body), sorted(PREPARE_KEYS_FRESH)))
    assert body["pillar"] == pillar
    assert body["value"] == body["gained_points"]
    after = call(world, "GET", BT_STATE, session).json()["stones"]
    assert before - after == body["spent_stones"], (
        "the reply says %s stones spent but the balance moved %s"
        % (body["spent_stones"], before - after))
    values = {p["pillar"]: p["value"] for p in body["gate"]["pillars"]}
    assert values[pillar] == body["value"]
    assert body["persisted"]["spirit_stones"] == after


def test_prepare_refuses_when_the_purse_is_short(world):
    """A 409 naming the shortfall, and nothing is spent."""
    session = make_player(world, stones=0)
    resp = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"},
                **{"Idempotency-Key": "p-broke"})

    assert resp.status_code == 409
    assert "spirit stones" in resp.json()["detail"]
    assert row_of(world, session["player_id"])["spirit_stones"] == 0


def test_prepare_refuses_a_pillar_that_is_already_full(world):
    """A full pillar answers 409 rather than spending for no gain."""
    session = make_player(world, stones=10 ** 7)
    resp = None
    for i in range(60):
        resp = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"},
                    **{"Idempotency-Key": "full-%d" % i})
        if resp.status_code != 200:
            break
    assert resp.status_code == 409, resp.text[:200]
    assert "already full" in resp.json()["detail"]


def test_a_replayed_prepare_key_is_answered_with_the_stored_receipt_and_spends_nothing(world):
    """The single-slot marker refuses a retry of the SAME key and charges once.

    The balance is compared before and after the replay, because a route that
    re-ran the charge and then answered with the stored receipt would look
    identical on the wire.
    """
    session = make_player(world, stones=10000)
    headers = {"Idempotency-Key": "prep-once"}

    first = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert first.status_code == 200
    spent_once = call(world, "GET", BT_STATE, session).json()["stones"]

    replay = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert replay.status_code == 200
    body = replay.json()

    assert set(body) == PREPARE_KEYS_REPLAY, (
        "the replay carries %s; the pinned contract is %s"
        % (sorted(body), sorted(PREPARE_KEYS_REPLAY)))
    assert body["reason"] == "replay"
    assert body["deduplicated"] is True
    assert body["spent_stones"] == first.json()["spent_stones"]
    assert call(world, "GET", BT_STATE, session).json()["stones"] == spent_once, (
        "the replay charged the player a second time")


def test_a_different_key_is_a_genuine_second_purchase(world):
    """A new key really does spend again -- the marker is one slot, not a deny-list.

    The control for the test above: without it, a route that refused EVERY
    second prepare would pass the replay test while being unusable.
    """
    session = make_player(world, stones=10000)
    call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **{"Idempotency-Key": "one"})
    second = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"},
                  **{"Idempotency-Key": "two"})

    assert second.status_code == 200
    assert "reason" not in second.json()
    assert second.json()["value"] > 0


# ---------------------------------------------------------------------------
# 9b. THE STORED-MARKER CEILING -- header bound + route name + 1


def test_the_stored_marker_ceiling_is_the_header_bound_plus_the_route_name():
    """The ceiling on the replay marker is DERIVED, not guessed.

    ``idem_gate.require_idempotency_key`` returns ``route + ":" + key``, so the
    value that reaches the player's row is longer than the header by
    ``len(route) + 1``. Slicing the stored marker at the header bound on its own
    cut it shorter than the key ``_replay_slot`` compares against, so for every
    header longer than ``MAX_IDEM_KEY_LEN - len(route) - 1`` the comparison never
    held and the replay was charged again as a fresh purchase.

    The ceiling is asserted against the route names as DATA, so renaming a route
    and leaving the ceiling alone fails here instead of silently reintroducing
    the double-charge.
    """
    longest_route = max(cr._IDEM_ROUTES)
    expected = cr.MAX_IDEM_KEY_LEN + len(longest_route) + 1
    assert cr.MAX_STORED_IDEM_KEY_LEN == expected, (
        "the stored ceiling is %d, but the longest gate-produced key is %d "
        "(header %d + route %r = %d + separator 1)"
        % (cr.MAX_STORED_IDEM_KEY_LEN, expected, cr.MAX_IDEM_KEY_LEN,
           longest_route, cr.MAX_IDEM_KEY_LEN + len(longest_route)))
    assert longest_route in cr._IDEM_ROUTES


@pytest.mark.parametrize("route", sorted(cr._IDEM_ROUTES))
def test_a_maximum_length_key_survives_the_store_slice_whole(route):
    """A key at the header bound still EQUALS itself after being stored.

    The pure half: no HTTP, no player, just the slice ``_write_replay_slot``
    performs. This is the assertion the truncation defect violated, isolated
    from everything else the route does.
    """
    gated = idem_gate.require_idempotency_key(
        _Request({"Idempotency-Key": "K" * cr.MAX_IDEM_KEY_LEN}), route=route
    )
    assert len(gated) == cr.MAX_IDEM_KEY_LEN + len(route) + 1
    stored = str(gated)[: cr.MAX_STORED_IDEM_KEY_LEN]
    assert stored == gated, (
        "a %d-char key was truncated to %d on the way to the player's row; the "
        "replay comparison can never match a truncated marker"
        % (len(gated), len(stored)))
    # The ceiling is still a floor, not an absence of one.
    assert len(str("x" * 5000)[: cr.MAX_STORED_IDEM_KEY_LEN]) == \
        cr.MAX_STORED_IDEM_KEY_LEN


@pytest.mark.parametrize("key_len", [1, 64, 107, 108, 109, 128])
def test_a_replay_of_a_long_key_is_answered_from_the_marker_and_spends_nothing(world, key_len):
    """The replay guard holds at EVERY header length, not just the short ones.

    The gate namespaces by ``len(route) + 1`` = 20 characters, so a header of N
    characters becomes an N+20 marker. The old 128-char slice therefore cut the
    tail of every header over 108 -- **109 is the first length that broke**,
    measured by re-injecting the old ceiling and watching these arms go red
    while 1, 64, 107 and 108 stayed green. 108 survives because it stores
    exactly 128 characters and the 128-char slice loses nothing.

    Both sides of that boundary are pinned here so a future ceiling change is
    caught on the exact boundary rather than four characters past it.

    The pair ``test_a_replayed_prepare_key...`` above only ever pressed an
    8-character key, which is why the defect survived a green suite.

    The balance is compared across the replay for the same reason: a route that
    charged twice and THEN answered with the stored receipt would look
    identical on the wire.
    """
    session = make_player(world, stones=10000)
    headers = {"Idempotency-Key": "P" * key_len}

    first = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert first.status_code == 200, first.text[:200]
    spent_once = call(world, "GET", BT_STATE, session).json()["stones"]

    replay = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert replay.status_code == 200, replay.text[:200]
    body = replay.json()

    assert body["reason"] == "replay", (
        "a %d-char key replayed as a FRESH purchase -- the stored marker was "
        "truncated shorter than the key it is compared against" % key_len)
    assert body["deduplicated"] is True
    assert call(world, "GET", BT_STATE, session).json()["stones"] == spent_once, (
        "the replay charged the player a second time")


# ---------------------------------------------------------------------------
# 10. POST /cultivation-ladder/breakthrough/attempt -- the gated gamble
# ---------------------------------------------------------------------------

def test_attempt_requires_the_header_too(world):
    """Same rule as prepare, with its own route name in the refusal.

    The namespacing matters: a key minted for a prepare must never dedupe an
    attempt, so the two refusals name different routes.
    """
    session = make_player(world, stones=1000)
    resp = call(world, "POST", BT_ATTEMPT, session, {})

    assert resp.status_code == 400
    assert "cultivation-attempt" in resp.json()["detail"]


def test_attempt_refuses_while_the_gate_is_closed_and_charges_nothing(world):
    """A closed gate answers 409 with the engine's own reason, and spends nothing.

    The balance is the proof: a gate that refused AFTER the charge would pass a
    status assertion and quietly drain the player, which is the refusal case a
    money test most often misses.
    """
    session = make_player(world, stones=10000)
    before = call(world, "GET", BT_STATE, session).json()["stones"]

    resp = call(world, "POST", BT_ATTEMPT, session, {}, **{"Idempotency-Key": "a-closed"})
    assert resp.status_code == 409
    assert resp.json()["detail"]
    assert call(world, "GET", BT_STATE, session).json()["stones"] == before


def test_attempt_refuses_while_the_pillars_are_incomplete(world):
    """A full bar is not an open gate: the three pillars are checked too."""
    session = make_player(world, stones=100000, progress=100.0)
    call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **{"Idempotency-Key": "one"})

    resp = call(world, "POST", BT_ATTEMPT, session, {}, **{"Idempotency-Key": "a-half"})
    assert resp.status_code == 409
    assert "Preparation is incomplete" in resp.json()["detail"]


def test_an_attempt_on_a_full_gate_charges_the_stone_cost_before_the_roll(world):
    """A failure is never free: the cost is debited whatever the roll decides.

    The roll is random, so the OUTCOME is not asserted -- the contract is that
    ``ATTEMPT_STONE_COST`` is gone either way, and that the reply carries the
    route's own five keys. Compared against the module's constant, so a rebalance
    does not fail this file.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "charge")

    gate = call(world, "GET", BT_STATE, session).json()
    assert gate["gate_open"] is True, (
        "the harness failed to fill the pillars: blockers=%s" % gate["blockers"])
    before = gate["stones"]

    resp = call(world, "POST", BT_ATTEMPT, session, {}, **{"Idempotency-Key": "a-open"})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert set(body) == ATTEMPT_KEYS_FRESH, (
        "the attempt carries %s; the pinned contract is %s"
        % (sorted(body), sorted(ATTEMPT_KEYS_FRESH)))
    assert isinstance(body["success"], bool)

    after = call(world, "GET", BT_STATE, session).json()["stones"]
    assert before - after == breakthrough.ATTEMPT_STONE_COST, (
        "the attempt cost %s but the balance moved %s"
        % (breakthrough.ATTEMPT_STONE_COST, before - after))


def test_a_failed_attempt_starts_a_cooldown_and_a_replay_returns_the_stored_outcome(world):
    """The engine's own consequence is visible: a failure costs and then gates.

    A failure is the outcome this harness can produce without seeding a win, and
    it is the interesting one -- it must cost stones AND leave a cooldown. The
    replay of the SAME key returns the first outcome rather than charging again.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "fail")
    gate = call(world, "GET", BT_STATE, session).json()
    assert gate["gate_open"] is True, "the harness failed to fill the pillars"

    headers = {"Idempotency-Key": "a-fail"}
    first = call(world, "POST", BT_ATTEMPT, session, {}, **headers)
    assert first.status_code == 200, first.text[:300]
    spent_once = call(world, "GET", BT_STATE, session).json()["stones"]

    if first.json()["success"] is False:
        after = call(world, "GET", BT_STATE, session).json()
        assert after["cooldown_remaining"] > 0.0, "a failure left no cooldown"
        assert after["failures"] >= 1

    replay = call(world, "POST", BT_ATTEMPT, session, {}, **headers)
    assert replay.status_code == 200, replay.text[:300]
    assert set(replay.json()) == ATTEMPT_KEYS_REPLAY, (
        "the replay carries %s; the pinned contract is %s"
        % (sorted(replay.json()), sorted(ATTEMPT_KEYS_REPLAY)))
    assert replay.json()["reason"] == "replay"
    assert replay.json()["deduplicated"] is True
    assert replay.json()["success"] == first.json()["success"]
    assert call(world, "GET", BT_STATE, session).json()["stones"] == spent_once, (
        "the replay charged the player a second time")


# ---------------------------------------------------------------------------
# 11. LIFECYCLE -- a dead life and a memorial life are refused differently
# ---------------------------------------------------------------------------

def test_a_dead_life_is_410_on_every_write_and_still_readable(world):
    """A dead player answers 410 on all four writes; the two reads still serve.

    The reads matter: a screen a dead player cannot open is a support ticket,
    and the module's own rule is that read is allowed for every status.
    """
    session = make_player(world, stones=1000, away_hours=1.0, status="dead")
    pid = session["player_id"]
    row = row_of(world, pid)
    row["lifecycle"]["dies_at"] = time.time() - 10.0
    world["players_db"].persist(pid, row)

    for path, body, extra in ((CULTIVATE, {}, {}),
                              (CLAIM, {"milestone_id": "catchup_daily"}, {}),
                              (BT_PREPARE, {"pillar": "qi"}, {"Idempotency-Key": "d1"}),
                              (BT_ATTEMPT, {}, {"Idempotency-Key": "d2"})):
        resp = call(world, "POST", path, session, body, **extra)
        assert resp.status_code == 410, "%s answered %s" % (path, resp.status_code)

    assert call(world, "GET", STATE, session).status_code == 200
    assert call(world, "GET", BT_STATE, session).status_code == 200


def test_a_memorial_life_is_409_not_410(world):
    """Memorial and dead are DIFFERENT refusals, and a client must be able to tell.

    410 means the life ended; 409 means this life is over but a new one is
    possible. Collapsing them would leave a memorial player with no way to learn
    that rebirth is the answer.
    """
    session = make_player(world, stones=1000, away_hours=1.0, status="memorial")
    resp = call(world, "POST", CULTIVATE, session, {})

    assert resp.status_code == 409, resp.text[:200]
    detail = resp.json()["detail"]
    assert isinstance(detail, dict) and "lifecycle" in detail


# ---------------------------------------------------------------------------
# 12. CLAIM ERROR BRANCHES -- three refusals the suite did not reach, plus the
#     two properties that make a refusal safe rather than merely correct.
# ---------------------------------------------------------------------------

def test_a_catchup_claim_on_an_already_full_bar_is_refused_not_paid_zero(world):
    """A full bar refuses with 409 rather than paying 0.0.

    The refusal is load-bearing, not politeness. A 0.0 payout would still burn
    the once-per-period key, so the player would lose the reward for the whole
    day AND see a zero receipt -- the same mistake the requirement check exists
    to avoid. The reply therefore names both figures so a client can explain
    itself without guessing.
    """
    required = cultivation_config.stage_progress_required("mortal", "middle")
    session = make_player(world, stage="mid_late", away_hours=5.0, progress=required)
    milestone = next(m for m in ladders.MILESTONES
                     if m.get("is_catchup") and m["period_kind"] == "daily")

    resp = call(world, "POST", CLAIM, session, {"milestone_id": milestone["milestone_id"]})

    assert resp.status_code == 409, resp.text[:300]
    detail = resp.json()["detail"]
    assert detail["reason"] == "bar_already_full"
    assert detail["progress_required"] == pytest.approx(required, abs=0.01)
    assert detail["progress"] == pytest.approx(required, abs=0.01)

    # Nothing was paid and, because the refusal precedes the reservation, the
    # once-per-period key is NOT burned: draining the bar and re-claiming pays.
    row = row_of(world, session["player_id"])
    row["progress"] = 0.0
    world["players_db"].persist(session["player_id"], row)

    retry = call(world, "POST", CLAIM, session,
                 {"milestone_id": milestone["milestone_id"]})
    assert retry.status_code == 200, (
        "the refusal burned the once-per-period key: %s" % retry.text[:200])
    assert retry.json()["reason"] == "claimed"


def test_an_unpriceable_requirement_is_409_and_burns_nothing(world, monkeypatch):
    """A requirement kind the engine cannot price is a configuration gap, not "not yet".

    Forcing ``requirement_met`` to raise is how the branch is reached without
    editing the shared catalogue, which a peer may own. Both defaults the author
    rejected are wrong: False reads as "you have not earned it yet", True pays a
    rung nobody can price.

    The second arm is the property that matters: the refusal happens BEFORE the
    reservation, so restoring the engine lets the same player claim normally.
    """
    session = make_player(world, stage="mid_late")

    def unpriceable(*args, **kwargs):
        raise ladders.ProgressionError("no such requirement kind")

    monkeypatch.setattr(ladders, "requirement_met", unpriceable)
    refused = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert refused.status_code == 409, refused.text[:300]
    detail = refused.json()["detail"]
    assert detail["reason"] == "unsupported_requirement"
    assert detail["milestone_id"] == "cultivation_step_1"
    assert "requirement kind" in detail["error"]

    # Control: monkeypatch has restored the real engine, and the key is unspent.
    monkeypatch.undo()
    ok = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert ok.status_code == 200, (
        "the 409 burned the claim for the period: %s" % ok.text[:200])
    assert ok.json()["reason"] == "claimed"


def test_a_failed_payout_releases_the_reservation_rather_than_burning_the_day(world,
                                                                              monkeypatch):
    """A crash between reserve and pay must not cost the player the whole period.

    Measured: with ``pay_reward`` forced to raise, the request answers 500 and
    the NEXT identical request pays normally. Without the explicit release in the
    handler's ``except`` arm, the in-memory ledger keeps the reservation and
    every later claim in that period reads as a replay of a payout that never
    happened.
    """
    session = make_player(world, stage="mid_late")

    def exploding_payout(player, reward):
        raise RuntimeError("simulated payout failure")

    monkeypatch.setattr(ladders, "pay_reward", exploding_payout)
    failed = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert failed.status_code == 500, failed.text[:200]

    # Control: the engine works again and the reservation was rolled back.
    monkeypatch.undo()
    recovered = call(world, "POST", CLAIM, session,
                     {"milestone_id": "cultivation_step_1"})
    assert recovered.status_code == 200, (
        "one failed payout burned the claim for the period: %s" % recovered.text[:200])
    assert recovered.json()["reason"] == "claimed"
    assert recovered.json()["paid"]["qi"] > 0.0


def test_the_wallet_argument_is_accepted_and_never_consulted():
    """``wallet_store_accessor`` exists so sibling registrars can call alike.

    It is declared in the signature and never read in the body (measured: the
    only two occurrences in the module are the parameter and its docstring), so
    the three-argument call a caller wires beside the other ladder registrars
    must work AND must not reach for the wallet. Passing a callable that raises
    is what proves the second half -- an accessor that was quietly consulted
    would fail this test rather than passing it.
    """
    def exploding_wallet():
        raise AssertionError("cultivation settlement must not read the wallet")

    players_db = PlayerStore()
    app = FastAPI()
    report = cr.register_cultivation_routes(app, lambda: players_db, exploding_wallet)

    assert sorted(report["registered"]) == sorted(cr.CULTIVATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)
    world = {"app": app, "client": client, "players_db": players_db}
    session = make_player(world, stage="mid_late")

    # A paying claim end to end with a wallet that would raise if it were read.
    resp = call(world, "POST", CLAIM, session, {"milestone_id": "cultivation_step_1"})
    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["reason"] == "claimed"


def test_the_stored_replay_marker_is_bounded_but_dedupe_still_works_at_the_bound(world):
    """The marker is truncated to 128 chars; a key at that bound still dedupes.

    The module stores ``str(key)[:MAX_IDEM_KEY_LEN]``, while the header gate
    namespaces the caller's key to ``len(route) + 1 + len(key)``. Those two
    lengths are only equal while ``len(route) + 1 == 0``, so for every key past
    roughly 108 characters on the prepare route the stored marker is SHORTER
    than the key it must equal, the equality never holds, and the replay is not
    recognised. This test pins the boundary that is currently correct so the
    regression below is unambiguous when it is read beside it.

    The defect it sits next to, measured on this build: a 120-character key
    pressed three times costs 91 stones where the same three presses with a
    20-character key cost 25. That is a double charge on a route whose entire
    reason for requiring the header is that it cannot be double charged. Root
    cause is the truncation above -- ``MAX_IDEM_KEY_LEN`` is the header bound,
    not the namespaced-key bound, and ``idem_gate`` documents the stored ceiling
    as ``MAX_IDEMPOTENCY_KEY_LEN + len(route) + 1``.
    """
    session = make_player(world, stones=1000)

    # At the boundary the marker round-trips exactly and the replay is answered
    # with the stored receipt rather than spending a second time.
    boundary = cr.MAX_IDEM_KEY_LEN - len("cultivation-prepare") - 1
    headers = {"Idempotency-Key": "b" * boundary}
    first = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert first.status_code == 200, first.text[:200]
    spent_once = first.json()["spent_stones"]

    stored = (row_of(world, session["player_id"]).get("breakthrough", {})
              .get("last_prepare", {}).get("key"))
    assert stored is not None
    assert len(stored) <= cr.MAX_IDEM_KEY_LEN, (
        "the marker is unbounded: %d chars" % len(stored))

    replay = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
    assert replay.status_code == 200, replay.text[:200]
    assert replay.json()["reason"] == "replay"
    assert replay.json()["deduplicated"] is True
    assert (row_of(world, session["player_id"]).get("spirit_stones")
            == 1000 - spent_once), "the boundary replay charged a second time"


def test_a_replay_at_a_long_idempotency_key_is_not_charged_twice(world):
    """A replay of a money route must be charged once, at EVERY accepted key length.

    The header gate accepts up to 128 characters, so a client is entitled to
    send a key at any length in that range and receive the once-only guarantee
    the route advertises. This measures 20 (already covered), 108 (the
    boundary), and 120 (the length that used to break).

    ONE PLAYER PER KEY LENGTH, deliberately. The single slot this route stores
    is one of the numbers that make a re-press interesting: a player whose
    ``qi`` pillar is part-full buys the NEXT press at a higher unit price. A
    single reused player therefore charges a different total for the same three
    presses purely because the first sweep left the meter part-full, and the
    test then reports a double charge that never happened. Measured on this
    build with one shared player: 25 for the 20-char sweep and 28 for the
    120-char sweep, with the route behaving identically at both. Each sweep
    below mints its own player so the only variable is the key length.
    """
    def spend_for(key_length: int) -> int:
        session = make_player(world, stones=100000)
        headers = {"Idempotency-Key": "z" * key_length}
        before = row_of(world, session["player_id"])["spirit_stones"]
        for _ in range(3):
            resp = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
            assert resp.status_code == 200, resp.text[:200]
        return before - row_of(world, session["player_id"])["spirit_stones"]

    # Three identical presses at a 20-char key is the once-only cost of the action.
    baseline = spend_for(20)
    assert baseline > 0.0

    # Each comparison figure is computed ONCE and reused in the message, because
    # calling ``spend_for`` inside the assertion would spend a second time and
    # report a number that has nothing to do with the failure being described.
    for key_length in (108, 120):
        spend = spend_for(key_length)
        assert spend == baseline, (
            "a %d-char idempotency key was charged %s over three identical "
            "presses where a 20-char key costs %s"
            % (key_length, spend, baseline))


def test_a_replay_costs_nothing_at_every_accepted_key_length(world):
    """Permanent regression pin for the namespaced replay-key truncation.

    The defect this pins: ``_write_replay_slot`` truncated the stored marker to
    ``MAX_IDEM_KEY_LEN`` (128), the HEADER bound, while the key that
    ``_replay_slot`` compares against is the header namespaced to the route --
    ``len(route) + 1 + len(header)``. Past ``128 - len(route) - 1`` characters the
    stored marker was shorter than the key it had to equal, so the equality
    never held and the replay re-bought the pillar at its next price tier. The
    fix stores at ``MAX_IDEM_KEY_LEN + len(route) + 1``.

    The lengths below bracket that arithmetic on purpose: 20 is comfortably
    short, 107 and 108 sit either side of the old cut for the
    ``cultivation-prepare`` route, and 120 and 128 are the two lengths a client
    can actually send that the old bound chopped.

    Two claims are pinned per length, and the second is the one that matters:
    presses two and three must answer ``reason == "replay"`` with
    ``deduplicated`` true AND spend nothing. A test that only compared totals
    could still pass on a route that re-charged at the same price.
    """
    lengths = (20, 107, 108, 120, 128)
    costs = {}

    for key_length in lengths:
        session = make_player(world, stones=100000)
        headers = {"Idempotency-Key": "k" * key_length}
        before = row_of(world, session["player_id"])["spirit_stones"]

        first = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
        assert first.status_code == 200, first.text[:200]
        # A fresh prepare reply carries no ``reason`` at all (measured:
        # breakthrough.prepare returns ok/pillar/spent_stones/... only), while a
        # replay carries reason == "replay". Asserting on the paid shape keeps
        # this arm from passing vacuously if the route ever starts echoing back
        # the stored receipt on the FIRST press.
        assert first.json().get("reason") != "replay", (
            "the FIRST press at %d chars was answered as a replay: %s"
            % (key_length, first.text[:200]))
        assert first.json().get("spent_stones", 0) > 0, (
            "the first press at %d chars charged nothing: %s"
            % (key_length, first.text[:200]))

        for press in (2, 3):
            again = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"}, **headers)
            assert again.status_code == 200, again.text[:200]
            body = again.json()
            # ``.get`` not ``[]`` on purpose: under the regression a re-press is
            # executed as a FRESH purchase, and a fresh prepare reply carries no
            # ``reason`` key at all. A subscript here would raise KeyError and
            # hide the one fact that matters -- that the press was not a replay.
            assert body.get("reason") == "replay", (
                "press %d of a %d-char key answered %s rather than replay: %s"
                % (press, key_length, body.get("reason"), again.text[:200]))
            assert body.get("deduplicated") is True, (
                "press %d of a %d-char key was not marked deduplicated"
                % (press, key_length))

        costs[key_length] = before - row_of(world, session["player_id"])["spirit_stones"]

    # Every accepted length buys the pillar exactly once, at the same price.
    baseline = costs[lengths[0]]
    assert baseline > 0.0, "a first press at %d chars cost nothing" % lengths[0]
    for key_length in lengths[1:]:
        assert costs[key_length] == baseline, (
            "three identical presses at a %d-char key cost %s where a %d-char "
            "key costs %s: %s"
            % (key_length, costs[key_length], lengths[0], baseline, costs))


# ===========================================================================
# 13. AUTH ON THE BREAKTHROUGH PAIR -- the stranger arm, and why the shape
#     guard cannot be the thing that stops it.
#
# Section 2 proves an ANONYMOUS caller is refused. That is half the property,
# and the half that is easy: a junk token is treated as anonymous and answers
# 401, which a route with NO guard at all also answers. What distinguishes a
# guarded route from an open one is the STRANGER arm -- a token that is valid,
# minted by the server, and belongs to somebody else. Measured on the booted
# app: all four claim-bearing routes answer 403 with one message, and the
# victim's purse does not move.
# ===========================================================================

def test_a_valid_token_for_another_player_is_403_not_401(world):
    """The stranger arm: a real session for a different player is refused 403.

    The distinction is load-bearing and worth stating. A malformed token is
    treated as ANONYMOUS and answers 401; a well-formed token for the WRONG
    player answers 403. A route with no guard at all answers 200 to both, so
    probing with a junk token -- the obvious choice -- cannot tell a guarded
    route from an open one.

    Every route is probed with a REAL second session, minted by the server's own
    ``auth.issue_session``, rather than with a hand-built string.
    """
    victim = make_player(world, stones=5000, progress=100.0)
    attacker = make_player(world, stones=5000, progress=100.0)

    arms = [
        (STATE, "GET", None, {}),
        (BT_STATE, "GET", None, {}),
        (CULTIVATE, "POST", {}, {}),
        (CLAIM, "POST", {"milestone_id": "cultivation_step_1"}, {}),
        (BT_PREPARE, "POST", {"pillar": "qi"}, {"Idempotency-Key": "stranger-prep"}),
        (BT_ATTEMPT, "POST", {}, {"Idempotency-Key": "stranger-att"}),
    ]
    for path, method, body, extra in arms:
        resp = call(world, method, path, attacker, body,
                    params={"player_id": victim["player_id"]}, **extra)
        assert resp.status_code == 403, (
            "%s answered %s to a session for another player: %s"
            % (path, resp.status_code, resp.text[:200]))
        assert resp.json()["detail"] == "player_id does not match the authenticated session"

    # The strongest half: the refusal is not a courtesy, it is a refusal.
    assert row_of(world, victim["player_id"])["spirit_stones"] == 5000, (
        "a foreign session spent the victim's stones")
    assert row_of(world, victim["player_id"])["progress"] == 100.0


def test_junk_token_and_foreign_session_are_distinguishable(world):
    """401 and 403 are DIFFERENT answers and a caller must be able to tell.

    Pinned as a pair because either arm alone is satisfiable by a broken route:
    a route that answered 401 to everything would pass the anonymous test while
    never resolving identity at all, and a route that answered 403 to everything
    would pass the stranger test while refusing to serve its owner. The control
    at the end is what rules both out -- the owner is served.
    """
    stranger = make_player(world, stones=1000)
    junk = {"headers": {"Authorization": "Bearer not-a-real-token"}}

    assert call(world, "POST", BT_PREPARE, junk, {"pillar": "qi"},
                **{"Idempotency-Key": "j"}).status_code == 401
    assert call(world, "POST", BT_PREPARE, stranger, {"pillar": "qi"},
                params={"player_id": "someone-else"},
                **{"Idempotency-Key": "s"}).status_code == 403

    # Control: the owner of their own row is served normally.
    ok = call(world, "POST", BT_PREPARE, stranger, {"pillar": "qi"},
              **{"Idempotency-Key": "own"})
    assert ok.status_code == 200, ok.text[:200]


def test_the_shape_guard_is_blind_to_all_six_so_the_hand_wiring_is_load_bearing():
    """``player_claim_params`` finds NO player id on any of the six paths.

    This is the reason the module declares ``Depends(require_player)`` BY HAND
    rather than trusting the repository's shape-based ownership guard. That
    guard matches a bare ``player_id`` in the path, the query or the body, and
    none of these six routes names one -- so the guard skips all six BY DESIGN,
    and the hand-wiring is the whole protection rather than a belt on a brace.

    Asserted rather than left as a comment, because the failure mode it guards
    is silent: a future edit that dropped one dependency would leave an open
    route and no error anywhere.
    """
    app = FastAPI()
    report = cr.register_cultivation_routes(app, lambda: PlayerStore())
    assert report["guarded"], "the mount proved nothing guarded"

    # Only this module's own paths are inspected. A bare ``FastAPI()`` also
    # carries FastAPI's four built-in documentation routes, so the filter is a
    # prefix match rather than a "the app is empty" check.
    paths = {}
    for route in app.routes:
        if getattr(route, "path", "").startswith("/cultivation-ladder"):
            paths[route.path] = route

    assert set(paths) == set(cr.CULTIVATION_ROUTES)
    for path, route in sorted(paths.items()):
        assert ownership_guard.player_claim_params(route) == [], (
            "%s now names a player id, so the shape guard would cover it and the "
            "hand-wiring could be dropped" % path)
        assert ownership_guard.has_auth_dependency(route.dependant) is True, (
            "%s mounted without an auth dependency" % path)


# ===========================================================================
# 14. TRIBULATION IS DETERMINISTIC -- same inputs, same heaven, re-derivable
#
# The engine does NOT use the global ``random`` module. It draws from a private
# stream seeded on ``(player_id, attempt_number)`` via a SHA-256 digest
# (``breakthrough._seeded_random``). Measured consequences, all pinned below:
#
#   * the same player at the same attempt number gets a BYTE-IDENTICAL storm --
#     the full per-wave transcript, not just the total;
#   * a different attempt number gets a DIFFERENT storm, so the seed is real
#     and not a constant folded into a helper;
#   * a different player at the same attempt number also gets a different
#     storm, so the id is part of the seed rather than a bystander;
#   * the reported ``strain_total`` equals the LAST wave of the transcript the
#     client is shown, so the animated storm and the number beside it describe
#     one storm and not two.
#
# WHY this matters to a player: a disputed outcome has to be re-derivable. A
# server whose storm came from an unseeded global generator cannot answer "you
# died on wave 6, here is why" with anything but a guess.
# ===========================================================================

def _full_gate_snapshot(world, session):
    """The committed row AFTER the pillars are full, ready to be restored.

    ``PlayerStore`` hands out copies, so this is a detached read and restoring it
    is a genuine write. Restoring from a snapshot rather than hand-clearing
    counters is what makes the two runs in the next tests genuinely identical:
    ``attempts``, ``history``, ``successes`` and ``failures`` all feed the seed
    and the reply.
    """
    return row_of(world, session["player_id"])


def test_the_same_player_and_attempt_number_replays_the_same_storm_byte_for_byte(world):
    """Restoring the row and re-attempting reproduces the storm exactly.

    Compared as a whole transcript, not just ``strain_total``: the reply's one
    varying field is the wall-clock ``at`` stamp, which is dropped before the
    comparison. Every wave's strike, strain and absorbed figure must match, so
    a defect that reproduced the TOTAL but redrew the strikes would still fail
    here.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "det")
    snapshot = _full_gate_snapshot(world, session)

    first = call(world, "POST", BT_ATTEMPT, session, {},
                 **{"Idempotency-Key": "det-1"}).json()
    assert first["ok"] is True

    world["players_db"].persist(session["player_id"], snapshot)
    second = call(world, "POST", BT_ATTEMPT, session, {},
                  **{"Idempotency-Key": "det-2"}).json()

    a = dict(first["result"]); b = dict(second["result"])
    a.pop("at", None); b.pop("at", None)          # the only field that may move
    assert a == b, (
        "the same (player, attempt) produced two different storms: %s vs %s"
        % (a["tribulation"]["strain_total"], b["tribulation"]["strain_total"]))

    assert a["tribulation"] == b["tribulation"]
    assert first["success"] == second["success"]
    assert a["roll"] == b["roll"]
    assert a["chance"] == b["chance"]


def test_the_reported_strain_total_is_the_last_wave_of_the_transcript_it_ships(world):
    """The number beside the animation describes the SAME storm the client sees.

    The engine used to resolve the wave loop twice -- once to build the
    transcript and again to recompute the total -- and because the seeded stream
    is ADVANCED by each draw, the second pass drew different strikes. A probe
    over 30 seeded runs found the reported total contradicting the transcript in
    30 of 30. This pins the property that fixed it.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "strain")

    trib = call(world, "POST", BT_ATTEMPT, session, {},
                **{"Idempotency-Key": "strain-1"}).json()["result"]["tribulation"]
    transcript = trib["transcript"]

    assert transcript, "the storm shipped no waves at all"
    assert trib["strain_total"] == transcript[-1]["strain_total"], (
        "the reported total is %s but the last wave the client is shown carries %s"
        % (trib["strain_total"], transcript[-1]["strain_total"]))

    # Strain is a running total, so it never decreases along the transcript.
    running = [w["strain_total"] for w in transcript]
    assert running == sorted(running), "the transcript's strain went backwards: %s" % running
    assert transcript[-1]["wave"] == transcript[-1]["of"], (
        "a broken storm still claims to have finished every wave")


def test_a_different_attempt_number_draws_a_different_storm(world):
    """The seed is the attempt number, not a constant folded into a helper.

    This is the control for the test above. Without it, a "deterministic" storm
    that returned one hard-coded answer forever would pass -- determinism and a
    frozen constant are indistinguishable if you only ever run one attempt.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "seed")
    snapshot = _full_gate_snapshot(world, session)

    first = call(world, "POST", BT_ATTEMPT, session, {},
                 **{"Idempotency-Key": "seed-1"}).json()["result"]

    # Restore, then move ONLY the attempt counter, so (id, attempt_no) changes
    # and nothing else about the gate does.
    moved = dict(snapshot)
    state = breakthrough.state_of(moved)
    state["attempts"] = int(state["attempts"]) + 1
    world["players_db"].persist(session["player_id"], moved)

    second = call(world, "POST", BT_ATTEMPT, session, {},
                  **{"Idempotency-Key": "seed-2"}).json()["result"]

    assert second["attempt"] == first["attempt"] + 1, (
        "the attempt counter did not advance, so the seed never moved")
    assert (second["tribulation"]["strain_total"]
            != first["tribulation"]["strain_total"]) or (
        second["roll"] != first["roll"]), (
        "a different attempt number reproduced the identical storm and roll")


def test_two_different_players_at_the_same_attempt_number_get_different_storms(world):
    """The player id is part of the seed, so one player cannot copy another's heaven.

    The other half of the seed. A storm seeded only on the attempt number would
    be identical for every player on attempt 1, which is both a fairness problem
    and a way for one player's outcome to be replayed by another.
    """
    strains = {}
    rolls = {}
    for _ in range(4):
        session = make_player(world, stones=100000, progress=100.0)
        fill_pillars(world, session, "who")
        result = call(world, "POST", BT_ATTEMPT, session, {},
                      **{"Idempotency-Key": "who-1"}).json()["result"]
        strains[session["player_id"]] = result["tribulation"]["strain_total"]
        rolls[session["player_id"]] = result["roll"]

    assert len(set(strains.values())) > 1, (
        "four different players all drew strain %s -- the id is not in the seed"
        % sorted(set(strains.values())))
    assert len(set(rolls.values())) > 1, (
        "four different players all rolled %s -- the id is not in the roll seed"
        % sorted(set(rolls.values())))


def test_the_chance_the_gate_advertises_is_the_chance_the_roll_is_judged_against(world):
    """``GET breakthrough/state`` and the attempt that follows quote one number.

    A screen that advertised a different probability from the one the server
    enforced would be a lie the player acts on, and nothing else in this module
    would catch it.
    """
    session = make_player(world, stones=100000, progress=100.0)
    fill_pillars(world, session, "chance")

    advertised = call(world, "GET", BT_STATE, session).json()["chance"]
    judged = call(world, "POST", BT_ATTEMPT, session, {},
                  **{"Idempotency-Key": "chance-1"}).json()["result"]

    assert judged["chance"] == pytest.approx(advertised, abs=1e-4), (
        "the screen advertised %s and the roll was judged at %s"
        % (advertised, judged["chance"]))
    # And the advertised probability is a probability.
    assert 0.0 <= advertised <= 1.0


# ===========================================================================
# 15. REALM PROGRESSION IS MONOTONIC -- forward only, one rung at a time
#
# "Monotonic" here means one specific thing: along the ladder
# ``(realm index, stage index)``, a successful breakthrough always moves the
# player strictly FORWARD, exactly one rung, and nothing ever moves them back.
# Three claims, all measured:
#
#   * the target function is forward-only across all 18 rungs of the six-realm
#     ladder, and reports no target at the top rather than inventing one;
#   * walking the whole ladder end to end produces 17 consecutive forward steps
#     and lands on the ceiling;
#   * a FAILED attempt leaves the realm and stage exactly where they were, and
#     a FAILED tribulation clears the pillars and starts a cooldown.
#
# The forced-success harness replaces the engine's seeded stream with a zero
# stream. That is what makes the walk reachable: unpatched, a full gate survives
# at mortal and foundation but breaks at core and above (measured), so a
# full-ladder walk is unreachable without it. The patch is restored by
# monkeypatch, and each arm asserts its own gate is open before attempting --
# so a broken harness fails as "gate_open is False" rather than as a mystery.
# ===========================================================================

class _ZeroStream:
    """A ``random.Random`` stand-in whose ``random()`` is always 0.0.

    Stands in for BOTH draws the engine makes: the per-wave storm jitter and the
    final gate roll. Zeroing only the roll is not enough, because a success
    requires surviving the storm as well as beating the chance -- measured, and
    it is the reason a naive "force the roll" harness leaves the upper ladder
    stuck.
    """

    def __init__(self, *_args, **_kwargs):
        pass

    def random(self) -> float:
        return 0.0


class _OneStream:
    """The mirror of ``_ZeroStream``: ``random()`` is always 1.0.

    Used to force a FAILURE on every realm. 1.0 maximises every lightning strike
    -- the storm factor is ``0.85 + 0.30 * rng.random()``, so 1.0 is the worst
    case and the storm breaks -- and it also defeats every gate roll, since a
    roll of 1.0 is never below a chance of 1.0 or less.

    It exists because rolling for a failure makes the test a coin flip. An
    earlier version of the failure test rolled for real and went red under two
    UNRELATED mutations purely because the random stream happened to change,
    which is a flaky test masquerading as a contract.

    Independently measured on this build: the unforced failure arm at
    foundation/middle, whose advertised chance is 0.75, was given 60 freshly
    minted player ids and SUCCEEDED 28 times -- a 46.7% failure rate for the
    test. Roughly one run in two was green by luck rather than by verdict.
    """

    def __init__(self, *_args, **_kwargs):
        pass

    def random(self) -> float:
        return 1.0


def ladder_rung(player_row: Dict[str, Any]) -> int:
    """One comparable integer for a row's position on the ladder.

    Read from ``breakthrough.REALM_LADDER`` and the stage table at run time, so a
    ladder that is renamed, reordered or regrown moves this function's answers
    instead of silently passing against a stale ordering.

    The number is FLATTENED on purpose -- ``realm_index * stages_per_realm +
    stage_index`` -- rather than the pair ``(realm_index, stage_index)``. The pair
    is not ordered the way a player experiences the ladder: ``mortal/late`` and
    ``foundation/early`` are adjacent rungs, but as a pair they compare
    ``(0, 2)`` against ``(1, 0)``, which reads as a jump backwards. Flattening
    makes "one rung forward" the literal claim ``rung + 1``, which is what the
    tests below actually mean.
    """
    realm = str(player_row.get("cultivation_realm", "mortal"))
    stage = str(player_row.get("cultivation_stage", "early"))
    bridged = cultivation_config.STAGE_NAME_ALIASES.get(stage, stage)
    realm_index = breakthrough.REALM_LADDER.index(realm)
    stage_names = [s.name for s in cultivation_config.get_realm(realm).stages]
    stage_index = stage_names.index(bridged)
    return realm_index * len(stage_names) + stage_index


def test_every_rung_of_the_ladder_points_forward_and_the_top_points_nowhere(world):
    """The whole ladder, walked as data: 18 rungs, all forward, top reports none.

    This is the pure half -- no HTTP, no player -- and it is what the walk test
    below depends on for its notion of "forward". A rung that pointed sideways,
    backwards, or at itself would make the end-to-end walk pass by never moving
    while still looking healthy.
    """
    ladder = list(breakthrough.REALM_LADDER)
    stages = [s.name for s in cultivation_config.REALMS[0].stages]
    expected_rungs = len(ladder) * len(stages)
    assert expected_rungs == 18, (
        "the ladder is %d realms x %d stages; this test's arithmetic assumes 18"
        % (len(ladder), len(stages)))

    forwards = 0
    for realm_index, realm in enumerate(ladder):
        for stage_index, stage in enumerate(stages):
            row = {"cultivation_realm": realm, "cultivation_stage": stage}
            nxt_r, nxt_s = breakthrough._target(dict(row))
            if realm_index == len(ladder) - 1 and stage_index == len(stages) - 1:
                assert (nxt_r, nxt_s) == (None, None), (
                    "the top rung named a successor: %s" % (nxt_r,))
                continue
            assert nxt_r is not None, "%s/%s named no successor" % (realm, stage)
            # Flattened, so "one rung forward" is literally +1 even across a
            # realm boundary: mortal/late (2) -> foundation/early (3).
            here = realm_index * len(stages) + stage_index
            there = (ladder.index(nxt_r) * len(stages)
                     + stages.index(cultivation_config.STAGE_NAME_ALIASES.get(nxt_s, nxt_s)))
            assert there == here + 1, (
                "%s/%s points at %s/%s -- rung %d to rung %d, not one forward"
                % (realm, stage, nxt_r, nxt_s, here, there))
            forwards += 1

    assert forwards == expected_rungs - 1, (
        "walked %d forward rungs, expected %d" % (forwards, expected_rungs - 1))


def test_a_full_ladder_walk_is_seventeen_forward_steps_and_lands_on_the_ceiling(world, monkeypatch):
    """End to end: 17 successes, never a backward step, never a skip.

    The single strongest statement of monotonicity available. Each iteration
    tops the CURRENT stage's bar up to its own configured requirement, fills the
    three pillars through the real prepare route, asserts the gate is open, and
    attempts -- so the ladder is walked entirely through the module's own
    handlers with the module's own tables, not by writing a realm onto the row.
    """
    monkeypatch.setattr(breakthrough, "_seeded_random",
                        lambda *a, **k: _ZeroStream())

    session = make_player(world, stones=10 ** 8, progress=100.0)
    walked = [("mortal", "early")]

    for step in range(24):
        row = row_of(world, session["player_id"])
        if (row["cultivation_realm"], row["cultivation_stage"]) == (
                breakthrough.REALM_LADDER[-1], "late"):
            break
        # Top the bar up to the requirement of the stage the player is ON.
        bridged = cultivation_config.STAGE_NAME_ALIASES.get(
            row["cultivation_stage"], row["cultivation_stage"])
        row["progress"] = cultivation_config.stage_progress_required(
            row["cultivation_realm"], bridged)
        world["players_db"].persist(session["player_id"], row)

        fill_pillars(world, session, "walk%d" % step)
        gate = call(world, "GET", BT_STATE, session).json()
        assert gate["gate_open"] is True, (
            "the harness could not open the gate at %s/%s on step %d: blockers=%s"
            % (row["cultivation_realm"], row["cultivation_stage"], step,
               gate["blockers"]))

        resp = call(world, "POST", BT_ATTEMPT, session, {},
                    **{"Idempotency-Key": "walk-%d" % step})
        assert resp.status_code == 200, (
            "step %d refused: %s" % (step, resp.text[:200]))
        assert resp.json()["success"] is True, (
            "step %d failed with a forced roll: %s" % (step, resp.text[:200]))

        after = row_of(world, session["player_id"])
        walked.append((after["cultivation_realm"], after["cultivation_stage"]))

    assert len(walked) == 18, (
        "the walk visited %d rungs, expected 18: %s" % (len(walked), walked))
    for before, after in zip(walked, walked[1:]):
        assert ladder_rung({"cultivation_realm": before[0],
                            "cultivation_stage": before[1]}) <                ladder_rung({"cultivation_realm": after[0],
                            "cultivation_stage": after[1]}), (
            "the walk went backwards or sideways: %s -> %s" % (before, after))
    assert walked[-1] == (breakthrough.REALM_LADDER[-1], "late"), (
        "the walk stopped at %s rather than the ceiling" % (walked[-1],))


def test_an_attempt_at_the_ceiling_is_refused_and_charges_nothing(world):
    """The top rung has no successor, so the attempt refuses BEFORE the charge.

    The balance is the proof, and it is the half a status assertion misses: a
    route that charged and then discovered it had nowhere to promote to would
    answer 409 and quietly drain the player.

    The ceiling is read from the ladder rather than typed here, so a ladder that
    grew a seventh realm moves this test's starting point with it.
    """
    top_realm = breakthrough.REALM_LADDER[-1]
    top_stage = [s.name for s in cultivation_config.REALMS[0].stages][-1]
    session = make_player(world, realm=top_realm, stage=top_stage,
                          stones=10000, progress=100.0)
    fill_pillars(world, session, "ceiling")

    gate = call(world, "GET", BT_STATE, session).json()
    assert gate["gate_open"] is True, "the harness could not fill the pillars"
    before = gate["stones"]

    resp = call(world, "POST", BT_ATTEMPT, session, {},
                **{"Idempotency-Key": "ceiling"})
    assert resp.status_code == 409, resp.text[:200]
    assert "beyond this one" in resp.json()["detail"]

    after = call(world, "GET", BT_STATE, session).json()
    assert after["stones"] == before, (
        "the ceiling refusal charged %s stones" % (before - after["stones"]))
    row = row_of(world, session["player_id"])
    assert (row["cultivation_realm"], row["cultivation_stage"]) == (top_realm, top_stage)


def test_a_failed_attempt_never_demotes_and_never_refunds(world, monkeypatch):
    """A failure costs, gates, and leaves the player exactly where they stood.

    Monotonicity has a negative half that is easy to miss: not merely "success
    moves forward" but "failure does not move at all". A failure is also the
    outcome a real player hits most often, so this is the arm worth having.

    The realm and the stage are both checked, and the bar is checked for having
    been reset rather than for a specific number -- the retained fraction is the
    engine's own tuning and this file does not freeze it.
    """
    # The failure is FORCED, not hoped for. Measured: an earlier version of this
    # test rolled for real and went red under two unrelated mutations purely
    # because the roll changed -- which makes it a coin flip dressed as a
    # contract. A stream of 1.0 maximises every lightning strike (the storm
    # breaks) and defeats every gate roll (1.0 is never below the chance), so
    # the outcome is a failure on every run and on every realm.
    monkeypatch.setattr(breakthrough, "_seeded_random",
                        lambda *a, **k: _OneStream())

    session = make_player(world, realm="foundation", stage="middle",
                          stones=100000, progress=100.0)
    fill_pillars(world, session, "fail")
    gate = call(world, "GET", BT_STATE, session).json()
    assert gate["gate_open"] is True, "the harness could not fill the pillars"

    resp = call(world, "POST", BT_ATTEMPT, session, {},
                **{"Idempotency-Key": "fail-1"})
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert body["success"] is False, (
        "the forced-failure stream produced a success: %s" % body["result"].get("outcome"))

    after = call(world, "GET", BT_STATE, session).json()
    row = row_of(world, session["player_id"])

    assert (row["cultivation_realm"], row["cultivation_stage"]) == ("foundation", "middle"), (
        "a failure moved the player to %s/%s"
        % (row["cultivation_realm"], row["cultivation_stage"]))
    assert gate["stones"] - after["stones"] == breakthrough.ATTEMPT_STONE_COST, (
        "a failure cost %s, not the module's %s"
        % (gate["stones"] - after["stones"], breakthrough.ATTEMPT_STONE_COST))
    assert after["cooldown_remaining"] > 0.0, "a failure left no cooldown"
    assert after["failures"] >= 1
    assert row["progress"] < after["progress_required"], (
        "a failure left the bar full, so the next attempt skips the earning")
    # A failure consumes the preparation too, so the next gate is a new one.
    assert all(p["value"] == 0 for p in after["gate"]["pillars"]), (
        "a failure left the pillars full: %s" % after["gate"]["pillars"])


def test_cultivate_fills_the_bar_and_never_walks_the_ladder(world):
    """Monotonicity's other side: time alone never promotes anyone.

    The module's stated design is that exactly ONE route may promote a player,
    and that it is the attempt. A player banked far past the ceiling still
    stands where they stood, with the bar-full FLAG set. This is the same
    property the existing cultivate tests assert at a single rung; here it is
    asserted as an ordinal, so it holds whatever the ladder looks like.
    """
    session = make_player(world, away_hours=1000.0, progress=0.0)
    before = ladder_rung(row_of(world, session["player_id"]))

    body = call(world, "POST", CULTIVATE, session, {}).json()
    after_row = row_of(world, session["player_id"])

    assert body["reason"] == "cultivated"
    assert body["state"]["bar_full"] is True, "the harness did not fill the bar"
    assert ladder_rung(after_row) == before, (
        "cultivate promoted the player from %s to %s"
        % (before, ladder_rung(after_row)))
    assert after_row["cultivation_realm"] == "mortal"
    assert after_row["cultivation_stage"] == "early"


# ===========================================================================
# 16. THE CACHE-KEY CEILING IS FIXED -- permanent regression pin
#
# Sections 9b and the trailing money-route tests pin this over HTTP. The half
# pinned here is the one that does NOT need a player and therefore cannot be
# faked by a store that quietly discards a write: the arithmetic that produced
# the defect, asserted directly on the constant and on the slice the module
# performs.
#
# The defect: ``_write_replay_slot`` truncated the stored marker to
# ``MAX_IDEM_KEY_LEN`` (128), the HEADER bound, while the key it is later
# compared against is the caller's key namespaced to the route --
# ``route + ":" + key``. The stored marker was therefore SHORTER than the key
# for every header over ``128 - len(route) - 1`` characters, the equality never
# held, and the replay was executed as a FRESH purchase. Measured on the build
# that carried it: three identical presses with a 120-character key cost 91
# stones where the same three presses with a 20-character key cost 25.
#
# The fix: store at ``MAX_IDEM_KEY_LEN + len(route) + 1``, DERIVED from the
# route names rather than retyped.
# ===========================================================================

def test_the_stored_ceiling_exceeds_the_header_bound_it_used_to_copy():
    """The ceiling is not the header bound. That difference IS the fix.

    A one-line assertion that fails if anyone re-points the constant at 128 again,
    and it is a relationship rather than a snapshot: it compares the module's
    own ceiling against its own header bound.
    """
    assert cr.MAX_STORED_IDEM_KEY_LEN > cr.MAX_IDEM_KEY_LEN, (
        "the stored ceiling is back at the header bound (%d); every key longer "
        "than %d chars will be truncated shorter than the key it is compared "
        "against" % (cr.MAX_STORED_IDEM_KEY_LEN, cr.MAX_IDEM_KEY_LEN))


@pytest.mark.parametrize("route", sorted(cr._IDEM_ROUTES))
def test_the_old_ceiling_would_have_truncated_every_gate_produced_key(route):
    """The old 128-char slice is demonstrably too short for this route's keys.

    Two assertions in one, because either alone is weak. First: the gate really
    does produce keys longer than the header bound -- proving the truncation
    hazard is reachable, not theoretical. Second: slicing at the header bound
    really does change the string -- proving the hazard is real. Together they
    are the defect, reproduced, with the fix asserted beside them.
    """
    longest_header = "K" * cr.MAX_IDEM_KEY_LEN
    gated = idem_gate.require_idempotency_key(
        _Request({"Idempotency-Key": longest_header}), route=route
    )

    # The gate namespaces, so a header at the bound produces a longer key.
    assert len(gated) > cr.MAX_IDEM_KEY_LEN, (
        "the gate produced a key no longer than the header; the namespacing "
        "contract this test rests on has changed")

    # The OLD ceiling -- the header bound -- truncates it.
    old = str(gated)[: cr.MAX_IDEM_KEY_LEN]
    assert old != gated, (
        "the header bound no longer truncates this route's keys; the stored "
        "ceiling may now be derivable from the header bound alone")

    # The CURRENT ceiling stores it whole.
    stored = str(gated)[: cr.MAX_STORED_IDEM_KEY_LEN]
    assert stored == gated, (
        "a %d-char key does not survive the store slice on route %r"
        % (len(gated), route))

    # And the replay comparison is an equality, so a truncated marker can only
    # ever mismatch. Asserted by performing the comparison the module performs.
    assert stored == gated and old != gated


def test_the_marker_written_onto_the_row_equals_the_key_the_route_comparages_against(world):
    """Over HTTP: what lands on the player's row is the full namespaced key.

    The constant-level tests prove the arithmetic; this proves the module
    actually applies it on the way to the row, by reading the stored marker back
    THROUGH the store and comparing it to the key the gate produced for that
    exact header.
    """
    session = make_player(world, stones=100000)
    header = "R" * cr.MAX_IDEM_KEY_LEN
    resp = call(world, "POST", BT_PREPARE, session, {"pillar": "qi"},
                **{"Idempotency-Key": header})
    assert resp.status_code == 200, resp.text[:200]

    expected = idem_gate.require_idempotency_key(
        _Request({"Idempotency-Key": header}), route=cr.IDEM_ROUTE_PREPARE
    )
    stored = (row_of(world, session["player_id"]).get("breakthrough", {})
              .get("last_prepare", {}).get("key"))

    assert stored is not None, "no replay marker reached the player's row"
    assert stored == expected, (
        "the stored marker is %d chars against a %d-char gate key; the replay "
        "comparison can never match a truncated marker"
        % (len(stored), len(expected)))
    # The receipt rides along with the marker, which is what a replay answers
    # from; a marker with no receipt would answer a retry with nothing.
    assert (row_of(world, session["player_id"]).get("breakthrough", {})
            .get("last_prepare", {}).get("receipt"))


def test_a_marker_that_grew_unbounded_would_still_be_caught_by_the_ceiling(world):
    """The ceiling is a FLOOR on what may reach the row, not an absence of one.

    The control for the test above: a module that stored NO bound at all would
    pass every replay test, because nothing would truncate anything. Asserted
    directly on the slice so the floor cannot be quietly deleted.
    """
    absurd = "x" * 5000
    assert len(str(absurd)[: cr.MAX_STORED_IDEM_KEY_LEN]) == cr.MAX_STORED_IDEM_KEY_LEN
    assert cr.MAX_STORED_IDEM_KEY_LEN < len(absurd)
