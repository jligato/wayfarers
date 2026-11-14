"""Route contract tests for ``server/sect_wall.py``.

What "contract" means here
--------------------------
Every assertion below is about the *interface* of the six sect-wall routes --
the shape of the request the route accepts, the shape of the reply it returns,
who is allowed to call it, and which error it answers with. Nothing here pins a
balance number, a rank threshold, or a price; those are the module's business
and they live in ``server/test_sect_wall.py``.

Every expectation was measured against the running app before it was written
here, and each test names the layer that produces the behaviour, because the
status codes come from three different places and a reviewer needs to know
which one a test is pinning:

* ``422`` -- FastAPI/Pydantic request-body validation (a declared
  ``Field`` bound). Fires BEFORE the handler body runs, so it wins over the
  handler's own ``400`` for a missing ``Idempotency-Key``.
* ``401`` / ``403`` -- the app's session guard. ``GET /sect-wall/{player_id}``
  and the three body-carrying POST routes each run ``require_player`` and then
  an ownership check on the claimed player id.
* ``400`` / ``404`` / ``409`` -- the handler and the module functions it calls.

Two facts about this tree are load-bearing and easy to get wrong, so they are
stated here rather than rediscovered per test:

1. The wall feed is keyed on ``sect_id`` ALONE. ``wall_feed`` scans every player
   row in the store and collects posts whose ``sect_id`` matches, so a row this
   module leaked into a shared sect would corrupt a later run's feed. Every sect
   id here carries a per-run random tag, and the teardown deletes every player
   row the run created.
2. Player rows outlive the process (they are PostgreSQL-backed), so cleanup is
   not optional.

The file is self-contained on purpose: it imports the live ``app`` rather than
building a bare one, so it measures the routes exactly as deployed -- guard
installed, route order fixed, ownership check live.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

# ``server/`` must be importable as a bare directory: main.py binds ``auth``,
# ``identity`` and ``sect_wall`` under their bare names, so ``server.auth`` and
# ``auth`` would be two different module objects with two different session
# stores. A token minted against the wrong one comes back 401 for reasons that
# have nothing to do with the route under test.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.main import app, players_db
from server.sect_wall import MAX_POST_LENGTH
from server.tests.wc_test_auth import auth_headers, clear_signup_rate_limit

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
# The raw client, not the Session wrapper: these tests are ABOUT who is
# authenticated, so every call names its own ``Authorization`` header and the
# anonymous case is an explicit absence of one.
client = TestClient(app)

# The operator token the seed fixture is presented with. /admin/sect-wall/seed
# is behind TWO independent gates -- an environment variable and this token --
# and a test that opens only the first one is testing a different route.
SEED_ADMIN_TOKEN = "sect-wall-contract-operator-token"
SEED_HEADERS = {"X-Admin-Token": SEED_ADMIN_TOKEN}

# Per-run tag. The feed is keyed on sect_id alone, so a tag that is unique per
# run means a leaked row from any earlier run lands in a sect nobody reads.
RUN_TAG = uuid.uuid4().hex[:8]

# Every player row this run creates, so the teardown can remove all of them.
CREATED_PLAYER_IDS: list[str] = []

# Names this module has ever used. A row still carrying one of these belongs to
# an earlier run of this file and must go -- the module fixture purges those
# even if the per-test teardown never saw them.
_MODULE_PLAYER_NAMES = (
    "WallContractAnon", "WallContractNoSect", "WallContractSeeded",
    "WallContractElder", "WallContractJunior", "WallContractPinner",
    "WallContractDonor", "WallContractGhost",
)


def _sect(label: str) -> str:
    return f"{label}_{RUN_TAG}"


def _idem(*parts: str) -> dict[str, str]:
    return {"Idempotency-Key": ":".join(("wallc", RUN_TAG, *parts))}


def _as(player_id: str) -> dict[str, str]:
    """A session header for ``player_id``, minted against the store main.py reads."""
    return auth_headers(player_id)


def _create(name: str) -> str:
    # main.py refuses more than a handful of /player/create calls per address per
    # minute, and this module creates more than that.
    clear_signup_rate_limit()
    created = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert created.status_code == 200, f"player create failed: {created.status_code} {created.text}"
    player_id = created.json()["id"]
    CREATED_PLAYER_IDS.append(player_id)
    return player_id


def _seed(player_id: str, **params) -> dict:
    """Open both seed gates and set up sect membership / contribution / rank."""
    query = "&".join(f"{key}={value}" for key, value in params.items())
    response = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&{query}", headers=SEED_HEADERS
    )
    assert response.status_code == 200, f"seed failed: {response.status_code} {response.text}"
    return response.json()


def _delete_created_players() -> None:
    """Drop every row this run created. Player rows survive the process."""
    if not CREATED_PLAYER_IDS:
        return
    with players_db.transaction():
        for player_id in CREATED_PLAYER_IDS:
            if player_id in players_db:
                del players_db[player_id]
    CREATED_PLAYER_IDS.clear()


def _purge_module_players() -> None:
    """Drop this module's rows, new AND leaked from an earlier run."""
    with players_db.transaction():
        for player_id, player in list(players_db.items()):
            if player_id in CREATED_PLAYER_IDS or player.get("name") in _MODULE_PLAYER_NAMES:
                del players_db[player_id]
    CREATED_PLAYER_IDS.clear()


@pytest.fixture(autouse=True, scope="module")
def _purge_rows_from_earlier_runs():
    """Once per module: the cleanup only covers runs made after it existed."""
    _purge_module_players()
    yield
    _purge_module_players()


@pytest.fixture(autouse=True)
def _cleanup_created_players():
    """Every test leaves the store exactly as it found it."""
    yield
    _delete_created_players()


@pytest.fixture(autouse=True)
def _seed_gates_open(monkeypatch):
    """The seed fixture's environment gate, open for every test in this file.

    ``monkeypatch`` restores both variables afterwards, which matters because
    they are process-global and a leaked WC_ADMIN_TOKEN would open an operator
    route for whatever ran next in the same pytest process.
    """
    monkeypatch.setenv("WC_SECT_WALL_TEST_FIXTURE", "1")
    monkeypatch.setenv("WC_ADMIN_TOKEN", SEED_ADMIN_TOKEN)


# ---------------------------------------------------------------------------
# 1. the route table itself
# ---------------------------------------------------------------------------
def test_exactly_six_sect_wall_routes_are_mounted():
    """The module's whole HTTP surface, so a lost registration is a failure here.

    ``register_sect_wall_routes`` is called once from ``main.py``. Registering a
    second time on the live app produced duplicate operation ids and an
    unbuildable OpenAPI spec, so this test reads the mounted table rather than
    re-registering onto a copy.
    """
    mounted: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if "sect-wall" not in path:
            continue
        mounted[path] = set(getattr(route, "methods", set()) or set())

    assert mounted == {
        "/sect-wall/ranks": {"GET"},
        "/sect-wall/{player_id}": {"GET"},
        "/sect-wall/contribute": {"POST"},
        "/sect-wall/post": {"POST"},
        "/sect-wall/unpin": {"POST"},
        "/admin/sect-wall/seed": {"POST"},
    }


def test_ranks_is_mounted_before_the_player_path_so_it_is_not_read_as_an_id():
    """Route ORDER is part of the contract, not an implementation detail.

    ``/sect-wall/ranks`` is a literal path that also matches
    ``/sect-wall/{player_id}``. If the literal route is registered second,
    ``ranks`` is swallowed as a player id and the catalogue route answers 404.
    """
    order = [
        r.path for r in app.routes
        if getattr(r, "path", "") in ("/sect-wall/ranks", "/sect-wall/{player_id}")
    ]
    assert order.index("/sect-wall/ranks") < order.index("/sect-wall/{player_id}")


def test_ranks_catalogue_is_readable_without_a_session_and_hides_no_private_state():
    """``GET /sect-wall/ranks`` is the one public route; it carries no player state.

    Every other sect-wall route runs ``require_player``. This one does not,
    which is correct: it is a static privilege ladder with no player id in it.
    The contract it must keep is that it stays player-free -- if a rank row ever
    carried a player's contribution or stones, this would leak it to anyone.
    """
    anonymous = client.get("/sect-wall/ranks")
    assert anonymous.status_code == 200, anonymous.text
    body = anonymous.json()

    assert set(body) == {"ok", "ranks"}
    assert body["ok"] is True

    rank_keys = {key for rank in body["ranks"] for key in rank}
    assert rank_keys == {"id", "name", "min_lifetime", "can_post", "can_pin"}

    # Ordered low -> high, and the ladder is strictly ascending.
    ids = [r["id"] for r in body["ranks"]]
    floors = [r["min_lifetime"] for r in body["ranks"]]
    assert len(set(ids)) == len(ids), "rank ids must be unique"
    assert floors == sorted(floors) and len(set(floors)) == len(floors)

    # A player session must not change the answer: this route reads no player.
    player_id = _create("WallContractAnon")
    authenticated = client.get("/sect-wall/ranks", headers=_as(player_id))
    assert authenticated.status_code == 200
    assert authenticated.json() == body


# ---------------------------------------------------------------------------
# 2. GET /sect-wall/{player_id} -- auth, ownership, not-found, response shape
# ---------------------------------------------------------------------------
def test_wall_read_refuses_a_caller_with_no_session_and_a_bogus_token():
    """The wall is a per-member view, so an anonymous read is a 401."""
    player_id = _create("WallContractSeeded")
    _seed(player_id, sect_id=_sect("sect_read"), stones=10, lifetime=600)

    anonymous = client.get(f"/sect-wall/{player_id}")
    assert anonymous.status_code == 401, anonymous.text

    forged = client.get(f"/sect-wall/{player_id}", headers={"Authorization": "Bearer not-a-token"})
    assert forged.status_code == 401, forged.text


def test_wall_read_is_owned_another_player_may_not_read_it():
    """A valid session for player B cannot read player A's wall.

    This is the guard's ownership check, not a 404: the caller is authenticated,
    so pretending the wall does not exist would be a lie the client cannot act on.
    """
    owner = _create("WallContractSeeded")
    stranger = _create("WallContractGhost")
    _seed(owner, sect_id=_sect("sect_owner"), stones=10, lifetime=600)

    denied = client.get(f"/sect-wall/{owner}", headers=_as(stranger))
    assert denied.status_code == 403, denied.text
    assert "does not match" in denied.json()["detail"].lower()


def test_wall_read_is_404_when_the_sessions_own_player_does_not_exist():
    """A real session naming a player with no row is 404, not 500 and not 200.

    The session is valid and matches the path, so the ownership check passes and
    the module's own ``_require_player`` raises. A 200 here would hand back a
    wall for a player who was never created.
    """
    missing = "wall-contract-never-created"
    response = client.get(f"/sect-wall/{missing}", headers=_as(missing))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Player not found"


def test_mutations_refuse_a_missing_player_with_404_not_500():
    """Every wall mutation must answer 404 for a player that does not exist.

    Driven at the function layer on purpose. Over HTTP the session guard runs
    first and answers 401, which hides the branch under repair: a probe that
    stops at 401 proves the guard works, not that the route refuses cleanly.

    The ordering is the defect being pinned. ``PlayerStore.transaction(player_id)``
    preloads the row and raises a bare ``KeyError`` for an unknown id, so an
    existence check written INSIDE the ``with`` block is dead code for exactly
    this case -- the caller saw an unhandled 500 instead of this 404. The check
    therefore has to run BEFORE the transaction opens.
    """
    from server import sect_wall as sw

    class _Store:
        """Just the two PlayerStore behaviours this ordering depends on."""

        def __getitem__(self, player_id):
            if player_id != "wall-mutation-never-created":
                raise AssertionError(f"unexpected id requested: {player_id}")
            raise KeyError(player_id)

        def transaction(self, player_id=None):
            # Preload raises KeyError before the body is ever entered.
            self[player_id]
            raise AssertionError("transaction body must not run for a missing player")

    missing = "wall-mutation-never-created"
    store = _Store()
    calls = {
        "contribute": lambda: sw.contribute(store, missing, 1, "ord-1"),
        "create_post": lambda: sw.create_post(store, missing, "hi", False, "ord-2"),
        "unpin_post": lambda: sw.unpin_post(store, missing, "no-such-post"),
    }
    for name, call in calls.items():
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 404, f"{name} -> {caught.value.status_code}"
        assert caught.value.detail == "Player not found", name


def test_mutations_still_work_for_a_real_player_after_the_reorder():
    """Positive control: the hoisted check must not refuse a real player.

    A reorder that answered 404 for everybody would pass the test above, so this
    drives a live row through all three mutations and asserts the real effects.
    """
    from server import sect_wall as sw

    player = {"id": "ord-live", "name": "Live", "sect_id": "sect_ord",
              "spirit_stones": {"low_grade": 500}, "sect_wall": sw.empty_state()}
    # Elder threshold is 8000 lifetime contribution; elder is the lowest rank
    # that can both post AND unpin, which is what this control drives.
    player["sect_wall"]["lifetime_contribution"] = 8000

    class _Store:
        def __getitem__(self, player_id):
            assert player_id == "ord-live", player_id
            return player

        def transaction(self, player_id=None):
            # A plain generator is not a context manager; `with` needs
            # __enter__/__exit__, which PlayerStore.transaction supplies via
            # @contextmanager. mirror that with contextlib.
            return contextmanager(lambda: (yield self))()

    store = _Store()
    gave = sw.contribute(store, "ord-live", 10, "ord-live-1")
    assert gave["ok"] and gave["contribution_gained"] == 10 * sw.CONTRIBUTION_PER_STONE
    assert player["spirit_stones"]["low_grade"] == 490

    posted = sw.create_post(store, "ord-live", "hello wall", False, "ord-live-2")
    assert posted["ok"] and posted["post"]["message"] == "hello wall"

    unpinned = sw.unpin_post(store, "ord-live", posted["post"]["post_id"])
    assert unpinned["ok"] and unpinned["pinned"] is False


def test_wall_read_answers_the_documented_state_and_feed_shape():
    """The full reply key set, plus the invariants a client renders against.

    Not a snapshot: the constants are asserted as relationships against the
    module, so a rebalanced ladder without a contract change still passes.
    """
    from server import sect_wall as sw

    player_id = _create("WallContractSeeded")
    _seed(player_id, sect_id=_sect("sect_shape"), stones=40, lifetime=9000)

    response = client.get(f"/sect-wall/{player_id}", headers=_as(player_id))
    assert response.status_code == 200, response.text
    view = response.json()

    assert set(view) == {
        "ok", "sect_id", "rank", "rank_name", "can_post", "can_pin", "next_rank",
        "ranks", "contribution", "lifetime_contribution", "week_contribution",
        "week_cap", "week_reset_in", "contribution_per_stone", "stones",
        "post_count", "post_cooldown", "max_post_length", "max_pinned_posts",
        "server_now", "posts",
    }

    assert view["ok"] is True
    assert view["sect_id"].endswith(RUN_TAG)
    assert isinstance(view["posts"], list)
    assert isinstance(view["ranks"], list) and view["ranks"], "the ladder travels with the wall"

    # The advertised limits are the module's own, not numbers invented here.
    assert view["max_post_length"] == sw.MAX_POST_LENGTH
    assert view["max_pinned_posts"] == sw.MAX_PINNED_POSTS
    assert view["week_cap"] == sw.WEEKLY_CONTRIBUTION_CAP
    assert view["contribution_per_stone"] == sw.CONTRIBUTION_PER_STONE
    assert 0 <= view["post_cooldown"] <= sw.POST_COOLDOWN_SECONDS
    assert 0 <= view["week_reset_in"] <= sw.WEEK_SECONDS

    # Privilege flags agree with the rank row the view also ships.
    own_rank = next(r for r in view["ranks"] if r["id"] == view["rank"])
    assert view["can_post"] is own_rank["can_post"]
    assert view["can_pin"] is own_rank["can_pin"]
    assert view["rank_name"] == own_rank["name"]

    # A player with no posts reports zero and still carries the key.
    assert view["post_count"] == 0
    assert view["posts"] == []


def test_wall_read_omits_the_post_list_for_a_player_with_no_sect():
    """A sect-less wall answers with an empty feed rather than a 4xx.

    A freshly created player has no sect, and the client screen that renders
    this route has to render something for them.
    """
    player_id = _create("WallContractNoSect")
    response = client.get(f"/sect-wall/{player_id}", headers=_as(player_id))
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["sect_id"] is None
    assert view["posts"] == []
    assert view["rank"] == "outer_disciple"
    assert view["can_post"] is False


# ---------------------------------------------------------------------------
# 3. POST /sect-wall/contribute -- body bounds, idem header, refusals
# ---------------------------------------------------------------------------
def test_contribute_body_bounds_are_enforced_before_the_handler_runs():
    """Every declared ``Field`` bound on ContributeRequest, as a 422.

    ``stones`` carries ``ge=1, le=100000`` and ``player_id`` carries
    ``min_length=1``. The 422 is Pydantic's, and it lands BEFORE the handler --
    so it also beats the handler's own 400 for a missing ``Idempotency-Key``.
    That ordering is the contract; a caller that sends both a bad body and no key
    must be told about the body, because fixing only the key would just get the
    same rejection back.
    """
    player_id = _create("WallContractSeeded")
    _seed(player_id, sect_id=_sect("sect_bounds"), stones=100, lifetime=600)
    headers = _as(player_id)

    cases = {
        "missing player_id": ({"stones": 1}, "player_id"),
        "empty player_id": ({"player_id": "", "stones": 1}, "player_id"),
        "missing stones": ({"player_id": player_id}, "stones"),
        "zero stones": ({"player_id": player_id, "stones": 0}, "stones"),
        "negative stones": ({"player_id": player_id, "stones": -1}, "stones"),
        "stones above the ceiling": ({"player_id": player_id, "stones": 100001}, "stones"),
    }
    for label, (body, field) in cases.items():
        response = client.post(
            "/sect-wall/contribute", json=body, headers={**headers, **_idem(field)}
        )
        assert response.status_code == 422, f"{label}: {response.status_code} {response.text}"
        locations = {entry["loc"][-1] for entry in response.json()["detail"]}
        assert field in locations, f"{label}: 422 did not name {field}, got {locations}"

    # And the ordering claim itself: bad body wins over the missing key.
    both = client.post("/sect-wall/contribute", json={"stones": 0}, headers=headers)
    assert both.status_code == 422, both.text


def test_contribute_requires_the_idempotency_key_header():
    """A mutating, currency-spending route refuses a request with no replay key."""
    player_id = _create("WallContractSeeded")
    _seed(player_id, sect_id=_sect("sect_idem"), stones=50, lifetime=600)

    response = client.post(
        "/sect-wall/contribute", json={"player_id": player_id, "stones": 5}, headers=_as(player_id)
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Idempotency-Key required"


def test_contribute_refuses_a_player_id_that_is_not_the_sessions_owner():
    """The body-supplied player id is checked against the session, not trusted.

    This is the guard matching on the BODY. The write routes carry their
    identity in the body rather than the path, so this is the only thing standing
    between a valid session and spending somebody else's stones.
    """
    victim = _create("WallContractSeeded")
    attacker = _create("WallContractGhost")
    _seed(victim, sect_id=_sect("sect_victim"), lifetime=0)

    # Baseline measured from the live wall, never assumed: the signup grant and
    # the seed fixture both move this balance.
    before = client.get(f"/sect-wall/{victim}", headers=_as(victim)).json()

    response = client.post(
        "/sect-wall/contribute",
        json={"player_id": victim, "stones": 5},
        headers={**_as(attacker), **_idem("steal")},
    )
    assert response.status_code == 403, response.text
    assert "does not match" in response.json()["detail"].lower()

    # The victim's balance is untouched by the refused attempt.
    after = client.get(f"/sect-wall/{victim}", headers=_as(victim)).json()
    assert after["stones"] == before["stones"]
    assert after["contribution"] == before["contribution"]


def test_contribute_success_shape_and_the_view_it_carries():
    """The success reply is the receipt PLUS a fresh wall view.

    A client that renders from this route never has to make a second call to
    repaint after a contribution.
    """
    player_id = _create("WallContractDonor")
    # The seed fixture RAISES the wallet to a floor; it never sets a balance.
    # Ask for a floor well clear of the signup grant so the baseline is known,
    # then measure it anyway rather than trusting the number we asked for.
    _seed(player_id, sect_id=_sect("sect_donate"), stones=100, lifetime=600)
    before = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()

    response = client.post(
        "/sect-wall/contribute",
        json={"player_id": player_id, "stones": 10},
        headers={**_as(player_id), **_idem("ok")},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == {
        "ok", "player_id", "stones_spent", "contribution_gained", "rank", "view"
    }
    assert body["ok"] is True
    assert body["player_id"] == player_id
    assert body["stones_spent"] == 10
    assert body["rank"] == "inner_disciple"

    # The view agrees with the receipt: exactly stones_spent left the wallet, and
    # the contribution counters moved by exactly what the receipt gained.
    # Contribution is CUMULATIVE, so it is compared as a delta against the
    # baseline, not against the receipt amount.
    view = body["view"]
    assert view["stones"] == before["stones"] - 10
    assert view["contribution"] == before["contribution"] + body["contribution_gained"]
    assert view["lifetime_contribution"] == (
        before["lifetime_contribution"] + body["contribution_gained"]
    )
    assert view["week_contribution"] == before["week_contribution"] + body["contribution_gained"]


def test_contribute_replays_the_same_receipt_and_refuses_a_changed_payload():
    """One key + one payload = one charge; one key + a different payload = 409.

    The replay must return the ORIGINAL receipt rather than re-running the
    spend, which is why the second call cannot change the balance. It must not
    leak the internal ``_replayed`` marker either: that is an implementation
    detail, and ``_public`` exists to strip it.
    """
    player_id = _create("WallContractDonor")
    _seed(player_id, sect_id=_sect("sect_replay"), stones=100, lifetime=600)
    headers = {**_as(player_id), **_idem("once")}

    first = client.post(
        "/sect-wall/contribute", json={"player_id": player_id, "stones": 10}, headers=headers
    )
    assert first.status_code == 200, first.text

    replay = client.post(
        "/sect-wall/contribute", json={"player_id": player_id, "stones": 10}, headers=headers
    )
    assert replay.status_code == 200, replay.text
    assert "_replayed" not in replay.json()
    assert set(replay.json()) == set(first.json())
    assert replay.json()["view"]["stones"] == first.json()["view"]["stones"]

    changed = client.post(
        "/sect-wall/contribute", json={"player_id": player_id, "stones": 5}, headers=headers
    )
    assert changed.status_code == 409, changed.text
    assert "different payload" in changed.json()["detail"].lower()


def test_contribute_refuses_when_the_wallet_cannot_cover_it():
    """Insufficient balance is 409, and the balance does not move.

    The seed fixture only ever RAISES a wallet to a floor (``max(current,
    requested)``), so a fresh player's balance is at least the signup grant. The
    request below therefore asks for the route's declared maximum of 100000,
    which no funded fixture can cover -- a "spend more than I have" assertion
    that holds regardless of what the signup grant is worth.
    """
    from server.sect_wall import ContributeRequest

    player_id = _create("WallContractDonor")
    _seed(player_id, sect_id=_sect("sect_broke"), stones=10, lifetime=600)
    before = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()

    unaffordable = ContributeRequest(player_id=player_id, stones=100000).stones
    assert unaffordable > before["stones"], "this player must genuinely not afford the request"

    response = client.post(
        "/sect-wall/contribute",
        json={"player_id": player_id, "stones": unaffordable},
        headers={**_as(player_id), **_idem("broke")},
    )
    assert response.status_code == 409, response.text
    assert "stones" in response.json()["detail"].lower()

    after = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()
    assert after["stones"] == before["stones"]
    assert after["contribution"] == before["contribution"]


def test_contribute_refuses_a_player_who_is_in_no_sect():
    """No sect means no wall to contribute to: 409, not a silent credit."""
    player_id = _create("WallContractNoSect")
    response = client.post(
        "/sect-wall/contribute",
        json={"player_id": player_id, "stones": 1},
        headers={**_as(player_id), **_idem("nosect")},
    )
    assert response.status_code == 409, response.text
    assert "sect" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. POST /sect-wall/post -- message bounds, rank gate, cooldown, unpin
# ---------------------------------------------------------------------------
def test_post_body_validation_and_the_idempotency_key_are_both_required():
    """``message`` is required and ``player_id`` is bounded; the key header too."""
    player_id = _create("WallContractElder")
    _seed(player_id, sect_id=_sect("sect_post"), lifetime=9000)
    headers = _as(player_id)

    missing_message = client.post(
        "/sect-wall/post", json={"player_id": player_id}, headers={**headers, **_idem("m")}
    )
    assert missing_message.status_code == 422, missing_message.text

    empty_player = client.post(
        "/sect-wall/post", json={"player_id": "", "message": "hi"}, headers={**headers, **_idem("p")}
    )
    assert empty_player.status_code == 422, empty_player.text

    no_key = client.post("/sect-wall/post", json={"player_id": player_id, "message": "hi"}, headers=headers)
    assert no_key.status_code == 400, no_key.text
    assert no_key.json()["detail"] == "Idempotency-Key required"


def test_post_refuses_an_empty_or_oversized_message_with_400():
    """Message bounds are the module's own 400, not a Pydantic 422.

    ``PostRequest.message`` declares no ``Field`` bound; the length rules live in
    ``create_post``. That split is the contract, so these are asserted as 400
    rather than folded into the 422 table above.
    """
    player_id = _create("WallContractElder")
    _seed(player_id, sect_id=_sect("sect_len"), lifetime=9000)
    headers = _as(player_id)

    blank = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "   "},
        headers={**headers, **_idem("blank")},
    )
    assert blank.status_code == 400, blank.text
    assert blank.json()["detail"] == "Message is empty"

    too_long = client.post(
        "/sect-wall/post",
        json={"player_id": player_id, "message": "x" * (MAX_POST_LENGTH + 1)},
        headers={**headers, **_idem("long")},
    )
    assert too_long.status_code == 400, too_long.text
    assert str(MAX_POST_LENGTH) in too_long.json()["detail"]

    # A message of exactly the limit is accepted, so the bound is inclusive.
    at_limit = client.post(
        "/sect-wall/post",
        json={"player_id": player_id, "message": "y" * MAX_POST_LENGTH},
        headers={**headers, **_idem("limit")},
    )
    assert at_limit.status_code == 200, at_limit.text


def test_post_refuses_a_rank_that_may_not_post_and_allows_one_that_may():
    """Posting is a rank privilege, and the view's ``can_post`` predicts it.

    The same pair is checked against both the route and the flag the route
    advertises, so a client can grey out the button from the read it already has.
    """
    junior = _create("WallContractJunior")
    elder = _create("WallContractElder")
    _seed(junior, sect_id=_sect("sect_rank"), lifetime=0)
    _seed(elder, sect_id=_sect("sect_rank"), lifetime=9000)

    junior_view = client.get(f"/sect-wall/{junior}", headers=_as(junior)).json()
    assert junior_view["can_post"] is False
    denied = client.post(
        "/sect-wall/post", json={"player_id": junior, "message": "let me in"},
        headers={**_as(junior), **_idem("junior")},
    )
    assert denied.status_code == 403, denied.text

    elder_view = client.get(f"/sect-wall/{elder}", headers=_as(elder)).json()
    assert elder_view["can_post"] is True
    allowed = client.post(
        "/sect-wall/post", json={"player_id": elder, "message": "greetings"},
        headers={**_as(elder), **_idem("elder")},
    )
    assert allowed.status_code == 200, allowed.text


def test_post_success_shape_and_its_replay_contract():
    """The created post is echoed with a fixed key set, and a replay repeats it."""
    player_id = _create("WallContractElder")
    _seed(player_id, sect_id=_sect("sect_echo"), lifetime=9000)
    headers = {**_as(player_id), **_idem("echo")}

    created = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "  trimmed  "},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()

    assert set(body) == {"ok", "player_id", "post", "post_count", "view"}
    assert body["ok"] is True
    assert set(body["post"]) == {
        "post_id", "author_id", "author_name", "author_rank", "message", "pinned", "created_at"
    }
    assert body["post"]["message"] == "trimmed", "the stored message is the trimmed text"
    assert body["post"]["author_id"] == player_id
    assert body["post"]["pinned"] is False
    assert isinstance(body["post"]["post_id"], str) and body["post"]["post_id"]
    assert body["post_count"] == 1
    assert body["view"]["post_count"] == 1
    assert [p["post_id"] for p in body["view"]["posts"]] == [body["post"]["post_id"]]

    replay = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "  trimmed  "}, headers=headers
    )
    assert replay.status_code == 200, replay.text
    assert "_replayed" not in replay.json()
    assert replay.json()["post"]["post_id"] == body["post"]["post_id"]
    assert replay.json()["post_count"] == body["post_count"], "a replay must not add a second post"

    changed = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "different"}, headers=headers
    )
    assert changed.status_code == 409, changed.text


def test_post_enforces_a_cooldown_and_publishes_the_seconds_remaining():
    """The cooldown is a 409 on the second post, and the read says how long is left."""
    player_id = _create("WallContractElder")
    _seed(player_id, sect_id=_sect("sect_cd"), lifetime=9000)

    first = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "one"},
        headers={**_as(player_id), **_idem("cd1")},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "two"},
        headers={**_as(player_id), **_idem("cd2")},
    )
    assert second.status_code == 409, second.text
    assert "cooldown" in second.json()["detail"].lower()

    view = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()
    assert 0 < view["post_cooldown"] <= 30


def test_unpin_refuses_a_non_moderating_rank_and_a_post_that_is_not_there():
    """Unpin's two refusals: rank too low (403) and unknown post (404)."""
    from server import sect_wall as sw

    elder = _create("WallContractElder")
    junior = _create("WallContractJunior")
    _seed(elder, sect_id=_sect("sect_unpin"), lifetime=9000)
    _seed(junior, sect_id=_sect("sect_unpin"), lifetime=600)

    pinned = client.post(
        "/sect-wall/post", json={"player_id": elder, "message": "pinned", "pin": True},
        headers={**_as(elder), **_idem("pin")},
    )
    assert pinned.status_code == 200, pinned.text
    post_id = pinned.json()["post"]["post_id"]

    # A rank with can_post but not can_pin: allowed to post, not to moderate.
    junior_view = client.get(f"/sect-wall/{junior}", headers=_as(junior)).json()
    assert junior_view["can_post"] is True
    assert junior_view["can_pin"] is False
    denied = client.post(
        "/sect-wall/unpin", json={"player_id": junior, "post_id": post_id}, headers=_as(junior)
    )
    assert denied.status_code == 403, denied.text
    assert "moderate" in denied.json()["detail"].lower()

    # A valid moderator naming a post that is not on the wall.
    missing = client.post(
        "/sect-wall/unpin", json={"player_id": elder, "post_id": "no-such-post"}, headers=_as(elder)
    )
    assert missing.status_code == 404, missing.text
    assert "post not found" in missing.json()["detail"].lower()

    assert sw.MAX_PINNED_POSTS >= 1


def test_unpin_success_shape_has_no_view_and_clears_the_pin():
    """Unpin answers with the three keys its handler builds -- and no wall view.

    Unlike contribute and post, this route does not attach a ``view``. A client
    that assumed symmetry here would KeyError on the field; pinning it as absent
    is what makes that expectation explicit.
    """
    player_id = _create("WallContractPinner")
    _seed(player_id, sect_id=_sect("sect_unpin_ok"), lifetime=9000)

    pinned = client.post(
        "/sect-wall/post", json={"player_id": player_id, "message": "notice", "pin": True},
        headers={**_as(player_id), **_idem("pinok")},
    )
    assert pinned.status_code == 200, pinned.text
    post_id = pinned.json()["post"]["post_id"]
    assert pinned.json()["post"]["pinned"] is True

    unpinned = client.post(
        "/sect-wall/unpin", json={"player_id": player_id, "post_id": post_id}, headers=_as(player_id)
    )
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json() == {"ok": True, "post_id": post_id, "pinned": False}

    feed = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()
    assert [p["pinned"] for p in feed["posts"]] == [False]


def test_unpin_validates_its_body_and_enforces_ownership():
    """``post_id`` is required and bounded; a foreign player_id is refused."""
    player_id = _create("WallContractPinner")
    stranger = _create("WallContractGhost")
    _seed(player_id, sect_id=_sect("sect_unpin_body"), lifetime=9000)

    missing_field = client.post(
        "/sect-wall/unpin", json={"player_id": player_id}, headers=_as(player_id)
    )
    assert missing_field.status_code == 422, missing_field.text

    empty_field = client.post(
        "/sect-wall/unpin", json={"player_id": player_id, "post_id": ""}, headers=_as(player_id)
    )
    assert empty_field.status_code == 422, empty_field.text

    foreign = client.post(
        "/sect-wall/unpin", json={"player_id": player_id, "post_id": "whatever"}, headers=_as(stranger)
    )
    assert foreign.status_code == 403, foreign.text


# ---------------------------------------------------------------------------
# 5. POST /admin/sect-wall/seed -- the operator fixture route
# ---------------------------------------------------------------------------
def test_seed_route_is_dark_unless_both_of_its_gates_are_open(monkeypatch):
    """The env gate AND the operator token are each independently sufficient to hide it.

    This route writes spirit stones for any player id the caller names, so the
    two gates are not redundancy -- either one alone keeps it dark. Each arm below
    closes one gate and asserts the route does not answer.

    The autouse fixture opens both gates for every other test in this file, so
    each arm here has to close its own gate explicitly. An arm that only asserted
    "the route answers" would pass with both gates open and prove nothing.
    """
    player_id = _create("WallContractSeeded")

    # Gate 1 closed: environment variable unset. The route must be invisible.
    monkeypatch.delenv("WC_SECT_WALL_TEST_FIXTURE", raising=False)
    closed_env = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&stones=100", headers=SEED_HEADERS
    )
    assert closed_env.status_code == 404, closed_env.text

    # Gate 2 closed: environment variable open, no operator token.
    no_token = client.post(f"/admin/sect-wall/seed?player_id={player_id}&stones=100")
    assert no_token.status_code == 401, no_token.text

    wrong_token = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&stones=100",
        headers={"X-Admin-Token": "not-the-operator-token"},
    )
    assert wrong_token.status_code == 401, wrong_token.text

    # Both open: the route answers. The env gate has to be re-opened here --
    # monkeypatch.delenv above is still in force for the rest of this test.
    monkeypatch.setenv("WC_SECT_WALL_TEST_FIXTURE", "1")
    reachable = _create("WallContractGhost")
    both_open = client.post(
        f"/admin/sect-wall/seed?player_id={reachable}&stones=100", headers=SEED_HEADERS
    )
    assert both_open.status_code == 200, both_open.text


def test_seed_route_fails_closed_with_503_when_no_operator_token_is_configured(monkeypatch):
    """A deployment that never set WC_ADMIN_TOKEN must not let the route through.

    ``require_admin`` answers 503 rather than falling open. Asserting 503 and not
    401 matters: 401 means "the token you sent was wrong", which would point an
    operator at their client instead of at the missing server configuration.
    """
    player_id = _create("WallContractSeeded")
    monkeypatch.delenv("WC_ADMIN_TOKEN", raising=False)

    response = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&stones=100", headers=SEED_HEADERS
    )
    assert response.status_code == 503, response.text
    assert "WC_ADMIN_TOKEN" in response.json()["detail"]


def test_seed_route_returns_ok_and_a_wall_view():
    """Its reply is exactly ``{ok, view}``, and the view is the wall state shape."""
    player_id = _create("WallContractSeeded")
    seeded = _seed(player_id, sect_id=_sect("sect_seed"), stones=25, lifetime=9000)

    assert set(seeded) == {"ok", "view"}
    assert seeded["ok"] is True

    # The seeded view and the view served by GET /sect-wall are the same shape
    # minus the feed, which the seed route does not build.
    served = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()
    assert set(seeded["view"]) == set(served) - {"posts"}
    assert seeded["view"]["sect_id"] == served["sect_id"]
    assert seeded["view"]["rank"] == served["rank"]
    assert seeded["view"]["stones"] == served["stones"]


def test_seed_route_refuses_to_invent_a_player_row():
    """A seed for a player who does not exist must be 404, not a 500 crash.

    FIXED and re-measured 2026-10-04. This test was a strict xfail pinning the
    500: the route opened ``store.transaction(player_id)`` before checking the
    player existed, and ``PlayerStore.transaction`` reads ``self[player_id]`` on
    entry (player_store.py), so an unknown id raised a bare KeyError from inside
    the store instead of reaching ``_require_player``. The route now validates
    first, so the 404 the handler always wrote is finally reachable. The strict
    marker was removed only after the test passed on its own.
    """
    missing = "wall-contract-never-seeded"
    # raise_server_exceptions=False so any unhandled error becomes the status
    # code the route actually sends a client, rather than being re-raised.
    tolerant = TestClient(app, raise_server_exceptions=False)
    response = tolerant.post(
        f"/admin/sect-wall/seed?player_id={missing}&stones=5", headers=SEED_HEADERS
    )
    assert response.status_code == 404, (
        f"seed for an unknown player must be 404, got {response.status_code} {response.text}"
    )
    # The refusal must be the handler's own "Player not found", not the store's
    # KeyError leaking through as a 500-shaped body.
    assert response.json()["detail"] == "Player not found", response.text


def test_seed_route_validates_before_it_takes_the_store_lock(monkeypatch):
    """Ordering, measured: a missing player is refused with NO lock ever taken.

    The 404 alone does not prove the reorder is right -- a route could 404 after
    opening the lock and still pass the test above, and taking a global store
    lock to reject a typo is exactly the cost the fix was meant to avoid. So
    this asserts the ORDER, not just the status: the transaction must never be
    entered for an id that does not exist, and it MUST be entered for one that
    does (otherwise the ordering claim is unfalsifiable -- it would also hold
    for a route that never writes at all).
    """
    entered: list = []
    real_transaction = players_db.transaction

    def spy(player_id=None):
        entered.append(player_id)
        return real_transaction(player_id)

    monkeypatch.setattr(players_db, "transaction", spy)

    missing = "wall-contract-never-locked"
    tolerant = TestClient(app, raise_server_exceptions=False)
    refused = tolerant.post(
        f"/admin/sect-wall/seed?player_id={missing}&stones=5", headers=SEED_HEADERS
    )
    assert refused.status_code == 404, refused.text
    assert entered == [], (
        "the store lock was taken for a player that does not exist; the "
        f"existence check must run BEFORE the transaction, got {entered}"
    )

    # The other half of the control: a real player DOES reach the write, so the
    # assertion above is measuring order rather than a route that never writes.
    # ``stones`` is a FLOOR, not an assignment (see the floor/clamp test below),
    # so the value is measured against the player's real starting balance rather
    # than assumed -- a new player already holds a grant.
    real_player = _create("WallContractSeeded")
    floor = client.get(f"/sect-wall/{real_player}", headers=_as(real_player)).json()["stones"] + 7
    entered.clear()
    accepted = client.post(
        f"/admin/sect-wall/seed?player_id={real_player}&stones={floor}", headers=SEED_HEADERS
    )
    assert accepted.status_code == 200, accepted.text
    assert entered == [real_player], (
        f"the seed must open exactly one transaction, for the player it seeds; got {entered}"
    )
    assert accepted.json()["view"]["stones"] == floor, accepted.text


def test_seed_route_validates_the_player_it_actually_writes(monkeypatch):
    """The pre-lock check must read the SAME id the transaction writes.

    A reorder that validated one value and wrote another would still answer 404
    for unknown ids and still seed real ones -- every other assertion in this
    file would pass. So this pins the two ends together: the id the route locks
    is the id whose stored row changed.
    """
    seeded = _create("WallContractSeeded")
    bystander = _create("WallContractDonor")

    entered: list = []
    real_transaction = players_db.transaction

    def spy(player_id=None):
        entered.append(player_id)
        return real_transaction(player_id)

    monkeypatch.setattr(players_db, "transaction", spy)
    baseline = client.get(f"/sect-wall/{bystander}", headers=_as(bystander)).json()["stones"]
    before = client.get(f"/sect-wall/{seeded}", headers=_as(seeded)).json()["stones"]

    result = _seed(seeded, stones=before + 33, lifetime=100)

    assert entered == [seeded], f"locked {entered}, expected only the seeded player"
    assert result["view"]["stones"] == before + 33, result
    after = client.get(f"/sect-wall/{bystander}", headers=_as(bystander)).json()["stones"]
    assert after == baseline, (
        f"seeding {seeded} changed {bystander}'s balance {baseline} -> {after}; "
        "the route must write only the player it was asked about"
    )


def test_seed_route_raises_the_wallet_to_a_floor_and_clamps_the_request():
    """The fixture tops a wallet UP to a floor and caps the request it will honour.

    Measured behaviour, and the difference matters: ``stones`` is a floor, not an
    assignment. A request below the current balance leaves the balance alone, and
    an absurd request is clamped to the module's own 100000 ceiling rather than
    written verbatim.
    """
    from server.sect_wall import ContributeRequest

    ceiling = ContributeRequest.model_fields["stones"].metadata
    assert ceiling, "the stones ceiling must be declared on the request model"

    player_id = _create("WallContractSeeded")
    baseline = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()["stones"]

    # A floor below the current balance cannot lower it.
    below = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&stones=1", headers=SEED_HEADERS
    )
    assert below.status_code == 200, below.text
    assert below.json()["view"]["stones"] == baseline, (
        "the seed fixture raises a wallet to a floor; it must never lower one"
    )

    # An absurd request is clamped, not honoured and not written verbatim.
    absurd = client.post(
        f"/admin/sect-wall/seed?player_id={player_id}&stones=99999999", headers=SEED_HEADERS
    )
    assert absurd.status_code == 200, absurd.text
    assert absurd.json()["view"]["stones"] == 100000


# ---------------------------------------------------------------------------
# 6. NEGATIVE CONTROLS -- do these four rules actually fail when broken?
# ---------------------------------------------------------------------------
# Four rules in this module are asserted above but nothing in this file proves
# the assertion can go red:
#
#   * the weekly spending cap           (contribute -> 409 "Weekly contribution cap reached")
#   * the pinned-post limit             (create_post -> 409 "Pinned message limit reached")
#   * the posting cooldown              (create_post -> 409 "Sect wall post cooldown active")
#   * receipt trimming to RECEIPT_LIMIT  (_append_receipt's while-loop)
#
# A test that cannot fail proves nothing, so each one is paired with a control
# that breaks the rule and asserts the route then accepts what it must refuse.
# The four are the "sabotage list".
#
# HOW A SABOTAGE IS APPLIED
# -------------------------
# Each control runs on a CHILD interpreter (``sys.executable -c``) against an
# in-memory store, so the shipping tree is never edited and no PostgreSQL row is
# created. The module's own module-level constants are rebound to a value that
# disables the rule; because every rule reads its constant as a global at call
# time, rebinding is enough. Nothing else is stubbed.
#
# The child imports the module by its bare name (the docstring of sect_wall.py
# promises it imports no ``server.main``, so it is safe to import standalone),
# and prints ONE JSON line. The parent asserts the child exited 0, so a crashed
# probe is a loud failure rather than a silent "measured nothing" pass.
#
# The store is a two-line fake: the rules under test only ever call
# ``players_db.transaction(pid)`` (a context manager), ``players_db[pid]``, and
# ``players_db.persist(row)``. Nothing else in contribute/create_post touches
# the store, so this fake measures the RULES and not the storage layer.
#
# Every child ALSO runs the rule UNBROKEN first and prints that arm. A control
# that only printed the sabotaged arm could not tell "the sabotage broke the
# rule" apart from "the arm was broken to begin with". Each parent assertion
# below therefore reads a pair: honest -> refuses, sabotaged -> accepts.

_SERVER_DIR = _REPO_ROOT / "server"

# The child prelude. Injected with the server dir so ``sect_wall`` imports, plus
# a store fake and one player row. Each control appends its own probe code.
_CONTROL_PRELUDE = """
import json, sys, contextlib
sys.path.insert(0, %(server)r)
import sect_wall as sw


class _MemStore:
    \"\"\"Just enough store for contribute()/create_post(): a transaction context
    manager and dict access. Both rules mutate the row the transaction returned,
    which is how the real PlayerStore behaves inside its own transaction.\"\"\"

    def __init__(self):
        self.rows = {}
        self.persisted = []

    @contextlib.contextmanager
    def transaction(self, player_id=None):
        yield

    def __getitem__(self, player_id):
        return self.rows[player_id]

    def __contains__(self, player_id):
        return player_id in self.rows

    def persist(self, row):
        self.rows[row["id"]] = row
        self.persisted.append(row)

    def get(self, player_id, default=None):
        return self.rows.get(player_id, default)


def _player(**over):
    row = {
        "id": "ctl", "name": "ctl", "spirit_root": "fire",
        "spirit_stones": {"low_grade": 100000},
        "sect_id": "ctl-sect", "lifetime_contribution": 9000,
        "sect_wall": sw.empty_state(),
    }
    row["sect_wall"]["sect_id"] = "ctl-sect"
    row["sect_wall"]["lifetime_contribution"] = 9000
    row.update(over)
    return row


def _fresh(**over):
    store = _MemStore()
    store.persist(_player(**over))
    return store
"""


def _run_control(probe: str, sabotage: str = "") -> dict:
    """Run one probe body on a child interpreter with the shipping module.

    ``sabotage`` is Python source rebinding a module constant; it is injected
    AFTER the honest arm has already run, so the honest arm is measured on the
    unmodified module by construction rather than by convention.

    The child prints exactly one JSON line. Asserting on the exit code first
    means a probe that crashed cannot be mistaken for a probe that measured
    nothing, which is the failure mode that makes negative controls lie.
    """
    script = _CONTROL_PRELUDE % {"server": str(_SERVER_DIR)} + probe
    if sabotage:
        script += "\n# ---- SABOTAGE ----\n" + sabotage + "\n"
    script += "\nprint(json.dumps(_OUT))\n"

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=240,
    )
    assert proc.returncode == 0, (
        "the control probe crashed, so it measured nothing:\n%s"
        % proc.stderr.strip()[-800:]
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, "the control probe printed no JSON result line: %r" % proc.stdout[-400:]
    return json.loads(lines[-1])


def test_control_weekly_spending_cap_is_what_refuses_the_over_cap_contribution():
    """Sabotage the weekly cap; the over-cap contribution must then be accepted.

    The cap is a real rule -- spending past it must be refused -- but no test
    here proves the refusal comes from the CAP rather than from the wallet check
    or the 100000-stone body ceiling. This control removes only the cap and
    shows the exact contribution that is refused honestly becomes accepted.

    The honest arm needs a player who is NOT short of stones, so the only thing
    left that can refuse is the cap itself.
    """
    probe = """
# A player who already spent most of the week, with plenty of stones left, so
# the wallet check cannot be what refuses the next contribution.
store = _fresh()
store["ctl"]["spirit_stones"]["low_grade"] = 100000
store["ctl"]["sect_wall"]["week_contribution"] = sw.WEEKLY_CONTRIBUTION_CAP - 4
store["ctl"]["sect_wall"]["week_started_at"] = __import__("time").time()

from fastapi import HTTPException
try:
    sw.contribute(store, "ctl", 10, "ctl-key-honest")
    honest = {"refused": False, "status": None}
except HTTPException as exc:
    honest = {"refused": True, "status": exc.status_code, "detail": str(exc.detail)}

_OUT = {"honest": honest}
"""
    sabotage = """
# Disable the weekly cap. 4 is already spent of the cap, and this adds 20 more,
# so with the cap gone nothing is left to refuse it.
sw.WEEKLY_CONTRIBUTION_CAP = 10 ** 9

store2 = _fresh()
store2["ctl"]["spirit_stones"]["low_grade"] = 100000
store2["ctl"]["sect_wall"]["week_contribution"] = 1996
store2["ctl"]["sect_wall"]["week_started_at"] = __import__("time").time()
try:
    sw.contribute(store2, "ctl", 10, "ctl-key-sab")
    _OUT["sabotaged"] = {"refused": False, "status": None,
                         "week_contribution": store2["ctl"]["sect_wall"]["week_contribution"]}
except HTTPException as exc:
    _OUT["sabotaged"] = {"refused": True, "status": exc.status_code,
                         "detail": str(exc.detail)}
"""
    out = _run_control(probe, sabotage)

    # Precondition: the honest arm is a real refusal, not a fixture failure.
    assert out["honest"]["refused"] is True, out
    assert out["honest"]["status"] == 409, out
    assert "weekly" in out["honest"]["detail"].lower(), out

    # The control: with the cap gone, the same over-cap spend is accepted.
    assert out["sabotaged"]["refused"] is False, (
        "disabling the weekly cap did NOT let the over-cap contribution through, "
        "so the weekly-cap assertion cannot go red and proves nothing: %r" % (out,)
    )
    assert out["sabotaged"]["week_contribution"] == 2016, out


def test_control_pinned_post_limit_is_what_refuses_the_fourth_pin():
    """Sabotage the pinned-post limit; the MAX+1'th pin must then be accepted.

    ``MAX_PINNED_POSTS`` is read as a module global inside ``create_post``. The
    honest arm fills the wall to the limit and shows the next pin is refused;
    the sabotaged arm raises the limit and shows the same pin succeeds. The
    cooldown is not in play here because each child posts on a fresh row whose
    ``last_post_at`` is 0.
    """
    probe = """
import time
from fastapi import HTTPException

def _pin(store, i):
    # The cooldown is ALSO a 409 and would mask the pin limit: a fill loop that
    # posts back-to-back has its 2nd..nth pins refused by the cooldown instead,
    # and the control would then be measuring the wrong rule. Clearing
    # last_post_at before every attempt is what makes this a pin-limit probe.
    store["ctl"]["sect_wall"]["last_post_at"] = 0.0
    try:
        sw.create_post(store, "ctl", "pin-%d" % i, True, "ctl-pin-%d" % i)
        return {"refused": False}
    except HTTPException as exc:
        return {"refused": True, "status": exc.status_code, "detail": str(exc.detail)}

# Fill exactly to the limit, then ask for one more.
store = _fresh()
accepted = []
for i in range(sw.MAX_PINNED_POSTS):
    _OUT_i = _pin(store, i)
    if not _OUT_i["refused"]:
        accepted.append(i)
_OUT = {"honest": {"filled": len(accepted), "limit": sw.MAX_PINNED_POSTS,
                   "next_pin": _pin(store, 999)}}
"""
    sabotage = """
sw.MAX_PINNED_POSTS = 99

store2 = _fresh()
for i in range(3):
    _pin(store2, i)
_OUT["sabotaged"] = _pin(store2, 999)
"""
    out = _run_control(probe, sabotage)

    # Precondition: three pins are accepted honestly and the fourth is not.
    assert out["honest"]["filled"] == out["honest"]["limit"], out
    assert out["honest"]["limit"] >= 1, out
    assert out["honest"]["next_pin"]["refused"] is True, out
    assert out["honest"]["next_pin"]["status"] == 409, out
    assert "pinned" in out["honest"]["next_pin"]["detail"].lower(), out

    # The control: with the limit raised, the pin past the honest limit lands.
    assert out["sabotaged"]["refused"] is False, (
        "raising the pinned-post limit did NOT allow the extra pin, so the "
        "pinned-limit assertion cannot go red and proves nothing: %r" % (out,)
    )


def test_control_post_cooldown_is_what_refuses_the_second_post():
    """Sabotage the cooldown; a second post inside the window must be accepted.

    The cooldown is the only thing standing between a member and unlimited
    posts, and ``post_cooldown`` is advertised in the read reply -- so a silent
    break would be invisible to the client too. This control posts twice in the
    same second and shows the second is refused only because of the cooldown.
    """
    probe = """
import time
from fastapi import HTTPException

store = _fresh()
store["ctl"]["sect_wall"]["last_post_at"] = 0.0
first = sw.create_post(store, "ctl", "one", False, "ctl-cd-1")
try:
    sw.create_post(store, "ctl", "two", False, "ctl-cd-2")
    second = {"refused": False, "post_count": len(store["ctl"]["sect_wall"]["posts"])}
except HTTPException as exc:
    second = {"refused": True, "status": exc.status_code, "detail": str(exc.detail)}
_OUT = {"honest": {"first_post_count": first["post_count"], "second": second,
                   "cooldown_seconds": sw.POST_COOLDOWN_SECONDS}}
"""
    sabotage = """
sw.POST_COOLDOWN_SECONDS = 0

store2 = _fresh()
store2["ctl"]["sect_wall"]["last_post_at"] = 0.0
sw.create_post(store2, "ctl", "one", False, "ctl-cd-1")
try:
    sw.create_post(store2, "ctl", "two", False, "ctl-cd-2")
    _OUT["sabotaged"] = {"refused": False,
                         "post_count": len(store2["ctl"]["sect_wall"]["posts"])}
except HTTPException as exc:
    _OUT["sabotaged"] = {"refused": True, "status": exc.status_code,
                         "detail": str(exc.detail)}
"""
    out = _run_control(probe, sabotage)

    # Precondition: the first post lands and the second is refused as a cooldown.
    assert out["honest"]["first_post_count"] == 1, out
    assert out["honest"]["second"]["refused"] is True, out
    assert out["honest"]["second"]["status"] == 409, out
    assert "cooldown" in out["honest"]["second"]["detail"].lower(), out

    # The control: with the cooldown at zero, the second post lands.
    assert out["sabotaged"]["refused"] is False, (
        "zeroing the post cooldown did NOT allow the second post, so the "
        "cooldown assertion cannot go red and proves nothing: %r" % (out,)
    )
    assert out["sabotaged"]["post_count"] == 2, out


def test_control_receipt_trimming_bounds_the_receipt_table():
    """Sabotage the RECEIPT_LIMIT trim; the table must then grow past the limit.

    ``_append_receipt`` is the only thing that stops the receipt table growing
    without bound. If its while-loop stopped running, no assertion in this file
    would notice: the replay tests only ever write two receipts. This control
    writes RECEIPT_LIMIT+5 and checks the honest table is trimmed back to the
    limit, and that the sabotaged table is not.
    """
    probe = """
store = _fresh()
# Enough stones and weekly room for every write below.
store["ctl"]["spirit_stones"]["low_grade"] = 100000
for i in range(sw.RECEIPT_LIMIT + 5):
    sw.contribute(store, "ctl", 1, "ctl-receipt-%d" % i)
state = store["ctl"]["sect_wall"]
_OUT = {"honest": {
    "limit": sw.RECEIPT_LIMIT,
    "receipt_order": len(state["receipt_order"]),
    "receipts": len(state["receipts"]),
}}
"""
    sabotage = """
sw.RECEIPT_LIMIT = 10 ** 9

store2 = _fresh()
store2["ctl"]["spirit_stones"]["low_grade"] = 100000
for i in range(15):
    sw.contribute(store2, "ctl", 1, "ctl-sab-receipt-%d" % i)
state2 = store2["ctl"]["sect_wall"]
_OUT["sabotaged"] = {
    "receipt_order": len(state2["receipt_order"]),
    "receipts": len(state2["receipts"]),
}
"""
    out = _run_control(probe, sabotage)

    # Precondition: the honest table is trimmed to exactly RECEIPT_LIMIT.
    assert out["honest"]["limit"] >= 1, out
    assert out["honest"]["receipt_order"] == out["honest"]["limit"], out
    assert out["honest"]["receipts"] == out["honest"]["limit"], out

    # The control: with the trim defeated, every receipt survives.
    assert out["sabotaged"]["receipt_order"] == 15, (
        "defeating the receipt trim did NOT let the table grow past the limit, "
        "so the RECEIPT_LIMIT rule is untested: %r" % (out,)
    )
    assert out["sabotaged"]["receipts"] == 15, out


def test_control_sabotage_list_is_exactly_the_four_named_rules():
    """The sabotage list is pinned: four rules, each with a live control.

    A reviewer asking "which rules are proven?" should get one answer, not four
    separate memories. This asserts the controls above exist and are named, so
    dropping one turns this red instead of silently reducing coverage.
    """
    controls = sorted(
        name for name, obj in globals().items()
        if name.startswith("test_control_") and callable(obj)
    )
    # sorted() order, not definition order: "pinneD" < "posT" on the 4th letter.
    assert controls == [
        "test_control_pinned_post_limit_is_what_refuses_the_fourth_pin",
        "test_control_post_cooldown_is_what_refuses_the_second_post",
        "test_control_receipt_trimming_bounds_the_receipt_table",
        "test_control_sabotage_list_is_exactly_the_four_named_rules",
        "test_control_weekly_spending_cap_is_what_refuses_the_over_cap_contribution",
    ], controls

# ---------------------------------------------------------------------------
# 7. THE FOUR HEADLINE CONTRACTS
# ---------------------------------------------------------------------------
# Sections 1-6 above pin the whole surface. These four tests are the headline
# claims stated at the top of the module's brief, isolated so a reviewer can
# read one test per claim:
#
#   1. posting requires auth        -> test_posting_refuses_a_caller_with_no_session
#   2. posting requires membership  -> test_posting_requires_sect_membership
#   3. deletion requires ownership  -> test_wall_deletion_is_scoped_to_the_owning_session
#   4. pagination is correct       -> test_pagination_clamps_to_the_module_ceiling_and_orders_pinned_first
#
# Two of these needed a measured correction to the brief, and both corrections
# are pinned rather than papered over:
#
# * THE MODULE HAS NO DELETE ROUTE. `register_sect_wall_routes` mounts six
#   paths and not one accepts the HTTP DELETE method (asserted below, measured
#   against the live route table). The only mutation that REMOVES a fact from
#   the wall is POST /sect-wall/unpin, which clears the `pinned` flag. So
#   "deletion requires ownership" is tested against unpin, and test_wall_deletion_is_scoped_to_the_owning_session
#   asserts the missing delete route explicitly so nobody re-adds it assuming it
#   was already covered.
#
# * PAGINATION IS NOT EXPOSED OVER HTTP. `wall_feed` takes a `limit` and clamps
#   it, but NO handler in the module ever passes one -- `feed_view` is called
#   with its default everywhere. So `?limit=1` on the read route is silently
#   ignored by the web framework (unknown query parameters are not an error).
#   The clamping contract therefore lives at the FUNCTION layer and is tested
#   there; the HTTP layer's truth is that the whole feed ships in one response,
#   which is pinned too.


def test_posting_refuses_a_caller_with_no_session():
    """Claim 1: POST /sect-wall/post is closed to anonymous and forged callers.

    The wall is a private per-member space, so a post is a write behind a
    session. Both arms are measured: no Authorization header at all, and a
    well-formed header carrying a token the server never issued.

    The stronger half is the last assertion. A 401 on its own only proves the
    guard ran -- it does not prove the guard ran BEFORE the write. So this also
    reads the wall back and shows the refused attempt left no post behind.
    """
    player_id = _create("WallContractElder")
    _seed(player_id, sect_id=_sect("sect_auth"), lifetime=9000)

    anonymous = client.post(
        "/sect-wall/post",
        json={"player_id": player_id, "message": "let me in"},
        headers=_idem("anon"),
    )
    assert anonymous.status_code == 401, anonymous.text

    forged = client.post(
        "/sect-wall/post",
        json={"player_id": player_id, "message": "let me in"},
        headers={**_idem("forged"), "Authorization": "Bearer not-a-real-token"},
    )
    assert forged.status_code == 401, forged.text
    # Both refusals answer with the SAME body. That matters: a distinct body per
    # arm would let a caller tell "no session" from "wrong session" and probe
    # for which player ids hold a session.
    assert anonymous.json() == forged.json()

    # The refused posts wrote nothing.
    view = client.get(f"/sect-wall/{player_id}", headers=_as(player_id)).json()
    assert view["post_count"] == 0, view
    assert view["posts"] == [], view


def test_posting_requires_sect_membership():
    """Claim 2: a player in no sect gets 409 "Not in a sect", and no post lands.

    The brief says posting requires membership. Measured, the refusal is 409 and
    the message names the sect, NOT 403 (which this file uses for a rank that is
    too low) and NOT 404. A caller therefore has to act on it differently: a rank
    problem is not retryable by waiting, and neither is this one.

    Both orderings matter and both are measured here. A fresh player with no sect
    is ALSO the lowest rank, so the two refusals could be swapped and the
    status code would stay 409. The message text is what separates them: the
    sect refusal names the sect, the rank refusal does not. The second half of
    this test pins the rank arm on a player who IS in a sect, so a swapped pair
    cannot pass.
    """
    from server import sect_wall as sw

    # --- arm A: no membership. Refused for the sect, not for the rank.
    no_sect = _create("WallContractNoSect")
    no_sect_view = client.get(f"/sect-wall/{no_sect}", headers=_as(no_sect)).json()
    assert no_sect_view["sect_id"] is None
    assert no_sect_view["can_post"] is False, "an outer disciple may not post anyway"

    refused = client.post(
        "/sect-wall/post",
        json={"player_id": no_sect, "message": "hello?"},
        headers={**_as(no_sect), **_idem("nosect")},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "Not in a sect"

    after = client.get(f"/sect-wall/{no_sect}", headers=_as(no_sect)).json()
    assert after["post_count"] == 0, "a refused post must not be stored"

    # --- arm B: IS in a sect, but the rank may not post. Same 409? No: 403.
    junior = _create("WallContractJunior")
    _seed(junior, sect_id=_sect("sect_member"), lifetime=0)
    assert sw.rank_for(0)["can_post"] is False

    low_rank = client.post(
        "/sect-wall/post",
        json={"player_id": junior, "message": "hello?"},
        headers={**_as(junior), **_idem("lowrank")},
    )
    assert low_rank.status_code == 403, low_rank.text
    assert low_rank.json()["detail"] != refused.json()["detail"], (
        "the rank refusal and the membership refusal must be distinguishable: "
        f"both answered {low_rank.json()['detail']!r}"
    )
    assert "rank" in low_rank.json()["detail"].lower(), low_rank.text

    # --- the control: the same member, with the rank that may post, succeeds.
    elder = _create("WallContractElder")
    _seed(elder, sect_id=_sect("sect_member"), lifetime=9000)
    allowed = client.post(
        "/sect-wall/post",
        json={"player_id": elder, "message": "hello"},
        headers={**_as(elder), **_idem("member_ok")},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["post_count"] == 1


def test_wall_deletion_is_scoped_to_the_owning_session():
    """Claim 3: the wall's deletion-shaped mutation is refused for a non-owner.

    MEASURED CORRECTION TO THE BRIEF. There is no DELETE route on this module.
    The live route table is asserted below so that gap is a fact in this file and
    not an assumption: if a delete route is ever added, this test goes red and
    the new route has to be given its own ownership contract.

    What exists instead is POST /sect-wall/unpin, which is the only mutation
    that takes a fact back off the wall (it clears a post's `pinned` flag). So
    that is what "deletion requires ownership" is tested against.

    The refusal arm is built so ownership is the ONLY possible cause: the caller
    is a full Elder in the SAME sect as the victim, so its rank clears the
    moderation check and its sect clears the membership check. If this answers
    403, the only thing left that can have refused is the session not matching
    the player id in the body.

    Both halves are asserted: the stranger is refused, AND the owner succeeds.
    A test that only saw the 403 would also pass if the route refused everybody.
    """
    victim = _create("WallContractPinner")
    stranger = _create("WallContractGhost")
    sect = _sect("sect_delete")
    # Same sect, and both Elder -- the rank that is allowed to moderate.
    _seed(victim, sect_id=sect, lifetime=9000)
    _seed(stranger, sect_id=sect, lifetime=9000)

    stranger_view = client.get(f"/sect-wall/{stranger}", headers=_as(stranger)).json()
    assert stranger_view["can_pin"] is True, (
        "the refusing caller must clear the rank check, or this test is not "
        "measuring ownership"
    )

    # The victim pins something, so there IS a pinned fact to take back off.
    pinned = client.post(
        "/sect-wall/post",
        json={"player_id": victim, "message": "official notice", "pin": True},
        headers={**_as(victim), **_idem("pin_for_delete")},
    )
    assert pinned.status_code == 200, pinned.text
    post_id = pinned.json()["post"]["post_id"]
    assert pinned.json()["post"]["pinned"] is True

    # --- the refusal: a valid Elder session naming somebody else's wall.
    refused = client.post(
        "/sect-wall/unpin",
        json={"player_id": victim, "post_id": post_id},
        headers=_as(stranger),
    )
    assert refused.status_code == 403, refused.text
    assert "does not match" in refused.json()["detail"].lower()

    # The victim's pin survived the refused attempt.
    still_pinned = client.get(f"/sect-wall/{victim}", headers=_as(victim)).json()
    assert [p["pinned"] for p in still_pinned["posts"]] == [True], still_pinned["posts"]

    # --- the positive control: the owner takes it back off.
    unpinned = client.post(
        "/sect-wall/unpin",
        json={"player_id": victim, "post_id": post_id},
        headers=_as(victim),
    )
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json() == {"ok": True, "post_id": post_id, "pinned": False}

    # --- and there is no HTTP DELETE anywhere on this module.
    sect_wall_methods = {
        getattr(route, "path", ""): sorted(getattr(route, "methods", set()) or set())
        for route in app.routes
        if "sect-wall" in getattr(route, "path", "")
    }
    assert sect_wall_methods, "no sect-wall routes are mounted at all"
    for path, methods in sect_wall_methods.items():
        assert "DELETE" not in methods, (
            f"{path} now answers DELETE; it needs its own ownership contract, "
            "and this test no longer describes the module"
        )


def test_pagination_clamps_to_the_module_ceiling_and_orders_pinned_first():
    """Claim 4: the feed's window is clamped, and its order is pinned-then-newest.

    MEASURED CORRECTION TO THE BRIEF. Pagination is a FUNCTION-layer contract
    here, not an HTTP one. `wall_feed(players_db, sect_id, limit)` clamps its
    window to between 1 and MAX_POSTS, but no handler ever passes a limit, so
    `GET /sect-wall/{player_id}?limit=1` does not page anything -- the web
    framework silently ignores an unknown query parameter. The second half of
    this test pins that measured truth rather than pretending the parameter works.

    Driven against an in-memory store on purpose: this is about the window and
    the ordering, and building it out of five players x four posts through the
    live app would spend twenty signed-in sessions to measure arithmetic.

    The store fake implements `items()`, which is the one method `wall_feed`
    needs, and that is all it implements.
    """
    from server import sect_wall as sw

    class _MemStore:
        """Only `items()` -- the single call `wall_feed` makes on a store."""

        def __init__(self, rows):
            self.rows = rows

        def items(self):
            return list(self.rows.items())

    def _member(player_id: str, sect: str, post_count: int) -> dict:
        state = sw.empty_state()
        state["sect_id"] = sect
        for index in range(post_count):
            state["posts"].append({
                "post_id": f"{player_id}-{index}",
                "author_id": player_id,
                "author_name": player_id,
                "author_rank": "elder",
                "message": f"m{index}",
                "pinned": False,
                # Ascending, so the newest is the highest index.
                "created_at": 1000.0 + index,
            })
        return {"id": player_id, "sect_id": sect, "sect_wall": state}

    # 5 members x 4 posts = 20 posts, exactly the module's ceiling.
    rows = {f"p{k}": _member(f"p{k}", "sectX", 4) for k in range(5)}
    store = _MemStore(rows)
    ceiling = sw.MAX_POSTS
    assert ceiling > 1, "the ceiling must leave room for a limit below it to matter"

    # --- the clamp. Every row below is the measured value, and each is written
    # as a relationship against the module's own ceiling rather than a literal,
    # so a rebalanced MAX_POSTS does not need this file edited.
    measured = {
        1: 1,
        3: 3,
        ceiling: ceiling,
        ceiling + 500: ceiling,   # a caller asking for more than the ceiling
        0: ceiling,               # 0 is falsy, so it falls back to the ceiling
        None: ceiling,            # None is falsy too
        -5: 1,                    # negative clamps UP to the floor of 1
    }
    for requested, expected in measured.items():
        got = len(sw.wall_feed(store, "sectX", requested))
        assert got == expected, f"limit={requested!r} returned {got} posts, expected {expected}"

    # The clamp is a floor as well as a ceiling: it never returns an empty feed
    # to a member who asked for a page, which would read as "no posts exist".
    assert sw.wall_feed(store, "sectX", 1), "limit=1 must still return a post"

    # --- the feed is a SHARED wall: it aggregates every member of the sect.
    aggregate = sw.wall_feed(store, "sectX", ceiling)
    assert len(aggregate) == ceiling
    assert {post["author_id"] for post in aggregate} == set(rows)

    # --- and it is keyed on sect_id alone, so another sect cannot bleed in.
    outsider = _member("outsider", "sectOTHER", 3)
    mixed = _MemStore({**rows, "outsider": outsider})
    isolated = sw.wall_feed(mixed, "sectX", ceiling)
    assert "outsider" not in {post["author_id"] for post in isolated}
    assert len(isolated) == ceiling, "an outsider's posts must not change the count"

    # --- the order: pinned posts first, then newest first inside each group.
    ordered_rows = {"solo": _member("solo", "sectY", 3)}
    posts = ordered_rows["solo"]["sect_wall"]["posts"]
    posts[0]["pinned"] = True
    posts[0]["created_at"] = 1.0   # the OLDEST post, but pinned
    feed = sw.wall_feed(_MemStore(ordered_rows), "sectY")

    assert [post["post_id"] for post in feed] == ["solo-0", "solo-2", "solo-1"], feed
    assert [post["pinned"] for post in feed] == [True, False, False]
    # Within the unpinned group the newest comes first, so the timestamps
    # descend once the pinned post has been lifted to the front.
    unpinned_times = [post["created_at"] for post in feed if not post["pinned"]]
    assert unpinned_times == sorted(unpinned_times, reverse=True)

    # --- MEASURED: the HTTP route does NOT expose the window. Every post in a
    # sect's wall arrives in one response regardless of what is asked for.
    first = _create("WallContractSeeded")
    second = _create("WallContractSeeded")
    third = _create("WallContractSeeded")
    shared = _sect("sect_page")
    for player_id in (first, second, third):
        _seed(player_id, sect_id=shared, lifetime=9000)
    # One post each -- the post cooldown would refuse a second from one player,
    # which is why the wall is filled across three members instead.
    for index, player_id in enumerate((first, second, third)):
        posted = client.post(
            "/sect-wall/post",
            json={"player_id": player_id, "message": f"page probe {index}"},
            headers={**_as(player_id), **_idem(f"page{index}")},
        )
        assert posted.status_code == 200, posted.text

    default_read = client.get(f"/sect-wall/{first}", headers=_as(first)).json()
    assert len(default_read["posts"]) == 3, default_read["posts"]

    for ignored_param in ("limit=1", "limit=1&offset=2", "page=1", "offset=2"):
        paged = client.get(f"/sect-wall/{first}?{ignored_param}", headers=_as(first))
        assert paged.status_code == 200, paged.text
        assert len(paged.json()["posts"]) == 3, (
            f"?{ignored_param} changed the feed size to {len(paged.json()['posts'])}; "
            "if pagination was meant to be exposed here, this is the test that "
            "should change with it"
        )
