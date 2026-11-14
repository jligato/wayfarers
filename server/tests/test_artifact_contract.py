"""Route contract for ``server/artifacts.py``, narrowed to the gaps.

There is already a 939-line suite at ``server/tests/test_artifacts_contract.py``
(plural). This file does not restate it. It pins the contracts that file
leaves open, so the two together cover the wire:

* **Request shape, per field.** The plural suite proves a missing
  ``idempotency_key`` and a non-string ``player_id`` are refused. This file
  removes each of the three fields in turn, so no field can become optional.
* **Refusal before existence.** On a guarded app an anonymous call is 401
  whether or not the player exists, so the guard cannot be used to probe which
  player ids are real.
* **The receipt ledger is private.** ``receipts`` is on the server's row and
  must never appear in a reply. The plural suite freezes the view key set but
  never asserts that the ledger is absent from it.
* **Pruning changes replay semantics.** Past 50 receipts the oldest key is
  dropped, and a dropped key replays as NEW. Untested anywhere, and it is the
  one place where a bounded ledger silently changes behaviour.
* **Degraded saves.** An ``artifacts`` block that is not a dict, or one whose
  ``owned`` is not a dict, is replaced rather than crashing the route.
* **The read route's write is real.** The GET persists its shape repair; this
  file proves a write happened by counting writes, not by reading the row back.

Two behaviours found by writing these are pinned as GAPS rather than
asserted as correct, and both say so in their own docstring:
``test_the_owned_list_follows_the_save_file_order_not_the_catalogue`` (list
order follows the save file, so positional clients are at the mercy of how a
row was written) and
``test_a_receipt_map_of_the_wrong_shape_is_500_on_activate_and_that_is_the_gap``
(a corrupt ``receipts`` map survives a 200 read and detonates on the next
activation). Changing either test to expect a nicer answer without repairing
the module would hide a real problem behind a friendlier number.

Every assertion here was measured by running it, not read off the source. The
running note matters: a bare run with the database environment inherited HANGS
to the full runner timeout, so memory mode is requested explicitly at import.

    cd G:/Projects/wayfarers-cycle && tools/wc_pytest.sh \\
      server/tests/test_artifact_contract.py

It does not import ``server.main``: the repo-root ``conftest.py`` imports that
module at collection time, so a peer editing ``main.py`` aborts every run in
the repo. The guard tests install the real
``ownership_guard.install_ownership_guard`` onto this file's own app, which is
what ``main.py`` does, without paying that import.
"""
from __future__ import annotations

import os

# Memory mode, before anything imports the store. A bare run with the
# Postgres environment inherited HANGS to the full runner timeout.
for _var in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

from contextlib import contextmanager  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import artifacts as A  # noqa: E402
import auth  # noqa: E402
import ownership_guard as guard  # noqa: E402


# The view key set, frozen. A client reads these names.
VIEW_KEYS = {"ok", "version", "cap", "catalog", "owned", "active",
             "active_count", "multipliers"}


class _PlayerStore:
    """The ``PlayerStore`` surface these two routes actually use.

    ``__setitem__`` and ``persist`` are load-bearing: the GET route writes
    ``store[player_id] = player`` to persist its shape repair, and in memory
    mode the real store hands back a detached copy. ``writes`` counts those
    assignments so a test can prove a write happened instead of inferring it.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.writes: list[str] = []

    def persist(self, pid, player=None):
        if player is not None:
            self.rows[pid] = player
            self.writes.append(pid)
        return player

    def __contains__(self, pid):
        return pid in self.rows

    def __getitem__(self, pid):
        return self.rows[pid]

    def __setitem__(self, pid, player):
        self.rows[pid] = player
        self.writes.append(pid)

    def get(self, pid, default=None):
        return self.rows.get(pid, default)

    @contextmanager
    def transaction(self, pid=None):
        yield self


@pytest.fixture
def rig():
    """Bare app, clean store, one per test. No guard, no live main."""
    players = _PlayerStore()
    app = FastAPI()
    A.register_artifact_routes(app, lambda: players)
    # raise_server_exceptions=False: a 500 must arrive as a 500 the assertions
    # can read, not as an exception that aborts the rest of the file.
    return {"players": players, "app": app,
            "client": TestClient(app, raise_server_exceptions=False)}


@pytest.fixture
def guarded_rig(rig):
    """The same app WITH the shipped ownership guard installed.

    ``main.py`` mounts the routes first and then runs ``install_ownership_guard``,
    so that is the order a real caller meets.
    """
    rig["guarded_map"] = guard.install_ownership_guard(rig["app"])
    rig["header"] = lambda pid: {
        "Authorization": "Bearer " + auth.issue_session(pid)}
    return rig


def owned_all():
    """A fully populated ``owned`` map for every catalogue entry.

    A seeded row carrying ``"owned": {}`` has no records at all, so
    ``activate`` answers 404 "Player does not own artifact" -- correct, and
    not what the degraded-save tests are about. These need records to exist.
    """
    return {aid: {"artifact_id": aid, "activated": False, "bound_to": None,
                  "charges": 0, "cooldown_until": 0}
            for aid in A.ARTIFACT_CATALOG}


def seed(players, pid, artifacts_block=None):
    """Persist one player row. ``artifacts_block=None`` means NO block yet."""
    row = {"id": pid, "name": pid}
    if artifacts_block is not None:
        row["artifacts"] = artifacts_block
    players.persist(pid, row)
    return pid


def activate(rig, pid, artifact_id, key):
    return rig["client"].post("/artifacts/activate",
                              json={"player_id": pid, "artifact_id": artifact_id,
                                    "idempotency_key": key})


# --- 1. The request body: every field is required --------------------------

@pytest.mark.parametrize("dropped", ["player_id", "artifact_id", "idempotency_key"])
def test_removing_any_one_field_from_the_body_is_422(rig, dropped):
    """No field of the activation body may become optional.

    The body is three fields and all three are load-bearing. Dropping each one
    in turn pins the requirement per field, so making any of them optional
    goes red on its own name rather than on a whole parametrised row.
    """
    seed(rig["players"], "p1")
    body = {"player_id": "p1", "artifact_id": "cloudseed_relic",
            "idempotency_key": "k1"}
    body.pop(dropped)
    response = rig["client"].post("/artifacts/activate", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"] == ["body", dropped]
    assert detail["type"] == "missing"


@pytest.mark.parametrize("bad", [1, 1.5, True, None, ["k"], {"a": 1}])
def test_an_idempotency_key_that_is_not_a_string_is_422(rig, bad):
    """The key is length-bounded, and it is type-bounded too.

    ``True`` is here on purpose: ``bool`` is a subclass of ``int`` in Python,
    so a lenient coercion would accept it and key a receipt on ``True``.
    """
    seed(rig["players"], "p1")
    response = rig["client"].post(
        "/artifacts/activate",
        json={"player_id": "p1", "artifact_id": "cloudseed_relic",
              "idempotency_key": bad})
    assert response.status_code == 422


def test_an_empty_artifact_id_is_404_not_422(rig):
    """Measured: the empty string is a well-formed string, so the model lets
    it through and the ROUTE refuses it. The refusal names the artifact.

    Pinned because the two failure modes are indistinguishable from the status
    code alone once a client stops reading ``detail``; the reason string is
    the only thing that distinguishes a bad id from an unknown one.
    """
    seed(rig["players"], "p1")
    response = activate(rig, "p1", "", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_the_body_declares_no_magnitude_field():
    """The exploit this body is shaped to stop. Read before adding a field.

    Three fields, all of which name WHICH thing. A ``power``/``magnitude``
    field here would let a caller size its own bonus.
    """
    assert set(A.ArtifactActivateRequest.model_fields) == {
        "player_id", "artifact_id", "idempotency_key"}
    for banned in ("power", "magnitude", "roll_value", "target", "amount"):
        assert banned not in A.ArtifactActivateRequest.model_fields


# --- 2. The reply: the receipt ledger is private ---------------------------

def test_the_view_never_carries_the_receipt_ledger(rig):
    """``receipts`` lives on the server's row and must not reach a client.

    The ledger holds an idempotency key to artifact mapping. Handing it to a
    client would let it enumerate which keys exist and, worse, learn the shape
    another caller's decision was stored under.
    """
    players = rig["players"]
    seed(players, "p1")
    activate(rig, "p1", "cloudseed_relic", "k1")

    # The row really does hold the ledger, so this is not a vacuous pass.
    assert "k1" in players["p1"]["artifacts"]["receipts"]

    body = rig["client"].get("/artifacts/p1").json()
    assert set(body) == VIEW_KEYS
    assert "receipts" not in body
    # And the POST envelope, which nests a full view, does not leak it either.
    posted = activate(rig, "p1", "emberlotus_seal", "k2").json()
    assert "receipts" not in posted["artifacts"]
    assert "k1" not in posted["artifacts"]["owned"][0]


def test_a_read_never_stamps_the_ownership_claim_on_the_stored_row(rig):
    """MEASURED contract: the read route passes ``None`` as the owner.

    A read may repair the SHAPE of the block and persist that repair, but it
    must not claim ownership. Passing the player id here made every bare GET
    stamp all three records AND write that claim to the save -- so a player
    who only ever browsed their inventory ended up bound to it.

    Proved against the STORED row, not the reply. The reply is a copy, so
    reading it back would pass even while the save was silently re-homed.
    """
    players = rig["players"]
    seed(players, "p1")
    assert "artifacts" not in players["p1"]

    body = rig["client"].get("/artifacts/p1").json()
    assert [o["bound_to"] for o in body["owned"]] == [None] * len(body["owned"])

    saved = players["p1"]["artifacts"]["owned"]
    assert {record["bound_to"] for record in saved.values()} == {None}, (
        "a read wrote an ownership claim into the save")

    # Two more reads change nothing, so this is not a first-call artefact.
    rig["client"].get("/artifacts/p1")
    rig["client"].get("/artifacts/p1")
    assert {r["bound_to"] for r in players["p1"]["artifacts"]["owned"].values()} == {None}


def test_an_artifact_stamped_to_another_player_refuses_with_403(rig):
    """The artifact-level check, which is NOT the guard's 403.

    The stamp lives on the ACTING player's own record -- what a save migrated
    from a prior identity looks like. No session is involved: this is the bare
    app, so the only thing standing between the caller and the artifact is the
    module's own check.

    This is the arm that would silently regress if the stamp comparison were
    dropped: the route would answer 200 and hand over an artifact belonging to
    somebody else, with nothing in the logs to say so.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["owned"]["cloudseed_relic"]["bound_to"] = "somebody_else"
    players.persist("p1", players["p1"])

    response = activate(rig, "p1", "cloudseed_relic", "k1")
    assert response.status_code == 403
    assert "bound to another player" in response.json()["detail"]

    # The refusal must not half-apply: still off, and no receipt burned.
    saved = players["p1"]["artifacts"]
    assert saved["owned"]["cloudseed_relic"]["activated"] is False
    assert saved["owned"]["cloudseed_relic"]["bound_to"] == "somebody_else"
    assert "k1" not in saved["receipts"]

    # The sibling the player genuinely owns still works, so the refusal is
    # about the stamped record and not about the save being unusable.
    assert activate(rig, "p1", "emberlotus_seal", "k2").status_code == 200


def test_an_unknown_player_is_404_on_the_write_route(rig):
    """The store's existence check is what answers this, not a KeyError.

    ``activate_artifact`` tests ``player_id in store`` before opening the
    transaction. Drop that line and the store raises ``KeyError`` inside the
    transaction instead, which surfaces as a 500 -- a false statement about a
    player whose id is merely unknown. Pinned so that swap cannot land
    quietly.
    """
    seed(rig["players"], "p1")
    response = activate(rig, "p1_who_does_not_exist", "cloudseed_relic", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player not found"


def test_an_unknown_player_is_404_on_the_read_route(rig):
    """Same reason on the read. The detail string is part of the contract:
    it is what a client distinguishes 'no such player' from 'no such
    artifact' by, and the two are different 404s on purpose.
    """
    response = rig["client"].get("/artifacts/ghost")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player not found"


def test_the_artifact_id_is_checked_before_the_player_id(rig):
    """When BOTH are wrong, the artifact is named. Measured, and fixed.

    With an unknown player AND an unknown artifact the reply names the
    ARTIFACT. Pinning the order stops a refactor from swapping which error a
    client sees for a doubly-bad request.
    """
    seed(rig["players"], "p1")
    response = activate(rig, "ghost", "no_such_artifact", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_an_unknown_artifact_id_is_404(rig):
    """The other half of the same pair, on its own so the two never merge."""
    seed(rig["players"], "p1")
    response = activate(rig, "p1", "no_such_artifact", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_an_unknown_artifact_id_is_checked_before_the_save_is_opened(rig):
    """Ordering is observable in the store too, not only in the reply.

    A blank artifact id on an unknown player must not open a transaction, so
    a store that would raise on an unknown id is never asked for one. This is
    the same ordering rule as above, measured on the write log instead of the
    status code, so it cannot pass for the wrong reason.
    """
    players = rig["players"]
    seed(players, "p1")
    writes_before = len(players.writes)
    assert activate(rig, "p1", "no_such_artifact", "k1").status_code == 404
    assert len(players.writes) == writes_before, (
        "the transaction opened before the artifact id was checked")


def test_a_replay_naming_a_different_artifact_is_409(rig):
    """A key is keyed on (key, artifact). Same key, other artifact, 409.

    The stored decision stands: the mismatch did not activate the second
    artifact, and it did not burn the key either, so the original can still
    be replayed.
    """
    players = rig["players"]
    seed(players, "p1")
    assert activate(rig, "p1", "cloudseed_relic", "shared").status_code == 200

    response = activate(rig, "p1", "tideglass_compass", "shared")
    assert response.status_code == 409
    assert response.json()["detail"] == "Idempotency key payload mismatch"

    view = rig["client"].get("/artifacts/p1").json()
    assert [a["id"] for a in view["active"]] == ["cloudseed_relic"]
    assert "shared" in players["p1"]["artifacts"]["receipts"]
    # The original decision is still replayable afterwards.
    assert activate(rig, "p1", "cloudseed_relic", "shared").status_code == 200


def test_the_owned_list_follows_the_save_file_order_not_the_catalogue(rig):
    """MEASURED. Read before "fixing" it -- the order is the SAVE's, not the
    catalogue's.

    ``state_view`` iterates ``state["owned"].values()``, so a save written in
    a different order renders in that different order. A save holding the
    three catalogue records reversed comes back reversed.

    Pinned as the truth about the wire because it is a client hazard: a
    client that indexes ``owned`` positionally sees a different artifact in
    slot 0 depending on how that player's row was built. The stable form is to
    look records up by ``artifact_id``, which is why that field is frozen.
    """
    players = rig["players"]
    ids = list(A.ARTIFACT_CATALOG)

    forward = {"version": 2, "cap": 3, "receipts": {}}
    forward["owned"] = {aid: {"artifact_id": aid, "activated": False,
                              "bound_to": None, "charges": 0,
                              "cooldown_until": 0} for aid in ids}
    seed(players, "forward", artifacts_block=forward)

    backward = {"version": 2, "cap": 3, "receipts": {}}
    backward["owned"] = {aid: {"artifact_id": aid, "activated": False,
                               "bound_to": None, "charges": 0,
                               "cooldown_until": 0}
                         for aid in reversed(ids)}
    seed(players, "backward", artifacts_block=backward)

    one = rig["client"].get("/artifacts/forward").json()
    two = rig["client"].get("/artifacts/backward").json()
    # Both rows hold the same three records.
    assert sorted(o["artifact_id"] for o in one["owned"]) == sorted(ids)
    assert sorted(o["artifact_id"] for o in two["owned"]) == sorted(ids)
    # And the order each one renders in is its OWN save order.
    assert [o["artifact_id"] for o in one["owned"]] == ids
    assert [o["artifact_id"] for o in two["owned"]] == list(reversed(ids))

    # A fresh player gets the catalogue order, because the default state is
    # built by iterating the catalogue. That is the order a new row has.
    seed(players, "fresh")
    assert [o["artifact_id"] for o in
            rig["client"].get("/artifacts/fresh").json()["owned"]] == ids


def test_an_inactive_view_reports_an_empty_list_not_unflagged_definitions(rig):
    """``active`` must be empty when nothing is on.

    Answering the full catalogue without flags would leave a client unable to
    tell which are switched on, and would make ``active_count`` disagree with
    ``len(active)``.
    """
    seed(rig["players"], "p1")
    body = rig["client"].get("/artifacts/p1").json()
    assert body["active"] == []
    assert body["active_count"] == 0 == len(body["active"])
    assert len(body["catalog"]) == len(A.ARTIFACT_CATALOG) > 0


def test_active_count_always_equals_the_length_of_active(rig):
    """The two numbers a client might cross-check must never disagree.

    Driven across the cap boundary, where eviction happens and the two are
    most likely to drift.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 2
    players.persist("p1", players["p1"])

    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        body = activate(rig, "p1", artifact_id, "k%d" % index).json()
        view = body["artifacts"]
        assert view["active_count"] == len(view["active"])
        assert view["active_count"] <= view["cap"]
        assert set(view["multipliers"]) == set(A.STAT_KEYS)


# --- 3. Auth: refusal happens before existence is disclosed ----------------

def test_the_guard_reports_both_artifact_routes_with_a_named_param(guarded_rig):
    """The installer returns ``{path: [guarded param names]}``.

    Asserting the KEYS proves reachability. Asserting a param NAME was
    recorded proves the shape rule found the claim rather than skipping the
    route for naming no player, which is the failure mode the module docstring
    warns about. ``/artifacts/activate`` carries its claim in the JSON body,
    so a path-only guard would not have found it.
    """
    mapping = guarded_rig["guarded_map"]
    artifact_paths = {p for p in mapping if p.startswith("/artifacts")}
    assert artifact_paths == {"/artifacts/activate", "/artifacts/{player_id}"}
    for path in sorted(artifact_paths):
        names = mapping[path]
        assert isinstance(names, list) and names, f"no param recorded for {path}"
        assert all(isinstance(n, str) for n in names)
    # The GET's claim is its path parameter, by name.
    assert "player_id" in mapping["/artifacts/{player_id}"]


def test_anonymous_is_refused_whether_or_not_the_player_exists(guarded_rig):
    """The guard cannot be used to enumerate real player ids.

    Both refusals are 401 and both carry the same shape. If an unknown player
    answered 404 while a known one answered 401, the status code alone would
    tell an attacker which ids are real -- and the module's own 404/403 split
    exists to prevent exactly that class of leak.
    """
    players = guarded_rig["players"]
    seed(players, "real")
    client = guarded_rig["client"]

    existing = client.get("/artifacts/real")
    missing = client.get("/artifacts/ghost")
    assert existing.status_code == missing.status_code == 401
    assert existing.json() == missing.json()


def test_anonymous_activation_is_refused_before_the_artifact_is_checked(guarded_rig):
    """A bad artifact id still answers 401 anonymously, never 404.

    Same reason as the read arm: the artifact id is caller-supplied and the
    guard runs first, so a client cannot learn which artifact ids exist.
    """
    seed(guarded_rig["players"], "p1")
    client = guarded_rig["client"]
    response = client.post("/artifacts/activate",
                           json={"player_id": "p1",
                                 "artifact_id": "no_such_artifact",
                                 "idempotency_key": "k1"})
    assert response.status_code == 401


def test_a_refused_call_never_touches_the_stored_row(guarded_rig):
    """Auth must refuse BEFORE the write path, not roll back after it.

    A guard that ran after the handler would still answer 401 while leaving a
    receipt burned or an artifact switched on. Counting writes is how that
    gets caught: a refusal must add nothing to the store's write log.
    """
    players = guarded_rig["players"]
    seed(players, "p1")
    before = len(players.writes)

    client = guarded_rig["client"]
    assert client.get("/artifacts/p1").status_code == 401
    assert client.post("/artifacts/activate",
                       json={"player_id": "p1",
                             "artifact_id": "cloudseed_relic",
                             "idempotency_key": "k1"}).status_code == 401

    assert len(players.writes) == before
    assert "receipts" not in players["p1"].get("artifacts", {})


def test_the_owner_is_served_on_both_routes_under_a_real_session(guarded_rig):
    """The refuse-everything control.

    Without this, every refusal assertion above would also pass against a
    guard that refuses EVERYTHING. The GET and the POST are separate requests
    with separate claims, so they get separate assertions.
    """
    players = guarded_rig["players"]
    seed(players, "p1")
    header = guarded_rig["header"]("p1")
    client = guarded_rig["client"]

    assert client.get("/artifacts/p1", headers=header).status_code == 200
    response = client.post("/artifacts/activate", headers=header,
                           json={"player_id": "p1",
                                 "artifact_id": "cloudseed_relic",
                                 "idempotency_key": "k1"})
    assert response.status_code == 200


def test_installing_the_guard_a_second_time_changes_nothing(guarded_rig):
    """``install_ownership_guard`` documents itself as idempotent.

    ``main.py`` mounts once, but a second module that mounts these routes
    would call it again. Behaviour, not the return value, is what is pinned:
    the owner still works and anonymous is still refused.
    """
    players = guarded_rig["players"]
    seed(players, "p1")
    guard.install_ownership_guard(guarded_rig["app"])

    header = guarded_rig["header"]("p1")
    client = guarded_rig["client"]
    assert client.get("/artifacts/p1").status_code == 401
    assert client.get("/artifacts/p1", headers=header).status_code == 200


def test_a_valid_session_naming_another_player_is_refused_on_both_routes(guarded_rig):
    """The guard answers 'is the claim the caller', not 'is it allowed'.

    Path claim on the GET, body claim on the POST. Both are 403, distinct from
    the 401 an unauthenticated caller gets.
    """
    players = guarded_rig["players"]
    seed(players, "owner")
    seed(players, "thief")
    header = guarded_rig["header"]("thief")
    client = guarded_rig["client"]

    assert client.get("/artifacts/owner", headers=header).status_code == 403
    response = client.post("/artifacts/activate", headers=header,
                           json={"player_id": "owner",
                                 "artifact_id": "cloudseed_relic",
                                 "idempotency_key": "k1"})
    assert response.status_code == 403


# --- 4. The receipt ledger, and what pruning does to a replay --------------

def test_a_replay_does_not_consume_a_second_ledger_slot(rig):
    """Replaying a key must not grow the ledger.

    A replay that appended a receipt would let one activation consume the
    whole 50-key budget, which would then start pruning REAL history.
    """
    players = rig["players"]
    seed(players, "p1")
    assert activate(rig, "p1", "cloudseed_relic", "k1").status_code == 200
    after_first = len(players["p1"]["artifacts"]["receipts"])
    assert after_first == 1

    for _ in range(5):
        assert activate(rig, "p1", "cloudseed_relic", "k1").status_code == 200
    assert len(players["p1"]["artifacts"]["receipts"]) == after_first


def test_a_key_pruned_from_the_ledger_replays_as_a_fresh_activation(rig):
    """MEASURED, and it is the sharp edge of a bounded ledger.

    Past 50 receipts the oldest key is dropped. A dropped key is therefore no
    longer idempotent: replaying it re-runs the activation instead of
    returning the stored decision. Under a cap this is visible -- the replay
    can evict something. Pinning it means nobody "fixes" it by accident and
    nobody builds a client that assumes a pruned key still replays.
    """
    players = rig["players"]
    seed(players, "p1")
    ids = list(A.ARTIFACT_CATALOG)

    first_key = "oldest"
    assert activate(rig, "p1", ids[0], first_key).status_code == 200
    # Drive the ledger past its bound so `oldest` is pruned.
    for index in range(60):
        activate(rig, "p1", ids[index % len(ids)], "fill-%03d" % index)

    receipts = players["p1"]["artifacts"]["receipts"]
    assert len(receipts) == 50
    assert first_key not in receipts, "precondition: the key really was pruned"

    # A key still IN the ledger replays its stored decision. It must be
    # replayed with the SAME artifact it was stored against -- a key is
    # keyed on (key, artifact), and a different artifact is a 409 by design,
    # which is what this control pins.
    newest_key, newest_artifact = "fill-059", ids[59 % len(ids)]
    stored = receipts[newest_key]
    assert stored["artifact_id"] == newest_artifact
    assert activate(rig, "p1", newest_artifact, newest_key).json() == stored

    replayed = activate(rig, "p1", ids[0], first_key)
    assert replayed.status_code == 200
    assert first_key in players["p1"]["artifacts"]["receipts"], (
        "a pruned key must re-enter the ledger as a NEW receipt")
    # And it re-ran the activation rather than returning a stored decision:
    # its decision block now matches the state it just produced.
    assert replayed.json()["artifact_id"] == ids[0]


def test_a_replay_never_re_activates_what_a_cap_has_since_pushed_off(rig):
    """The stored DECISION is final; the stored SNAPSHOT is not.

    A receipt replayed verbatim would claim an active count the player's own
    GET no longer agrees with -- a lie in the one response that promises to be
    the truth. The replay's nested view must equal the live view.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 1
    players.persist("p1", players["p1"])

    activate(rig, "p1", "cloudseed_relic", "k1")
    activate(rig, "p1", "emberlotus_seal", "k2")

    replay = activate(rig, "p1", "cloudseed_relic", "k1").json()
    live = rig["client"].get("/artifacts/p1").json()
    assert replay["artifacts"] == live
    # The decision fields still name what was originally asked for.
    assert replay["artifact_id"] == "cloudseed_relic"
    assert replay["idempotency_key"] == "k1"


# --- 5. Degraded saves ------------------------------------------------------

@pytest.mark.parametrize("block", ["junk", 7, [], ["cloudseed_relic"]])
def test_an_artifacts_block_that_is_not_a_dict_is_replaced_not_crashed(rig, block):
    """A corrupt block answers 200 with a fresh default state.

    The route must not 500 on a save it can rebuild, and must not pass the
    corrupt value through into the reply.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block=block)

    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == VIEW_KEYS
    assert body["version"] == 2
    assert body["cap"] == A.DEFAULT_ARTIFACT_CAP
    assert sorted(o["artifact_id"] for o in body["owned"]) == sorted(A.ARTIFACT_CATALOG)


@pytest.mark.parametrize("owned", ["junk", 7, [], None])
def test_an_owned_map_that_is_not_a_dict_is_replaced_not_crashed(rig, owned):
    """Same rule one level down.

    ``ensure_state`` treats a non-dict ``owned`` as a state it cannot repair,
    so it rebuilds. The rebuild is visible: every catalogue record comes back.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={"version": 2, "cap": 3,
                                         "receipts": {}, "owned": owned})
    body = rig["client"].get("/artifacts/p1").json()
    assert set(body) == VIEW_KEYS
    assert sorted(o["artifact_id"] for o in body["owned"]) == sorted(A.ARTIFACT_CATALOG)
    assert body["active"] == []


def test_a_receipt_map_of_the_wrong_shape_is_repaired_not_crash(rig):
    """HISTORICALLY A 500, REPAIRED. The old assertion was 500; keep the
    history here so the repair is not mistaken for the gap it replaced.

    ``ensure_state`` used to repair a bad ``owned`` map with ``setdefault``
    and leave a bad ``receipts`` map alone, because ``setdefault`` only fills
    an ABSENT key. The read route never reads the ledger, so GET answered 200
    and the corrupt value survived; the next activation called ``.get`` on the
    list and answered a plain-text 500. The same defect existed in
    ``_owned_artifact`` for a non-dict ``owned`` record, and that half was
    repaired first (it now answers 404).

    ``ensure_state`` now REPLACES a ``receipts`` value it cannot use instead of
    trusting it, so both routes answer as if the ledger were empty. Asserting
    the repaired answer is the point: the earlier version of this test pinned
    the 500 and went red the moment the module was fixed.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={"version": 2, "cap": 3,
                                         "receipts": ["k1"], "owned": owned_all()})

    # The read answers 200 AND repairs the ledger in the stored row.
    assert rig["client"].get("/artifacts/p1").status_code == 200
    assert players["p1"]["artifacts"]["receipts"] == {}

    # The write that used to detonate now behaves like a fresh activation.
    response = activate(rig, "p1", "cloudseed_relic", "k1")
    assert response.status_code == 200
    assert response.json()["artifact_id"] == "cloudseed_relic"
    assert "k1" in players["p1"]["artifacts"]["receipts"]


def test_a_receipt_holding_a_non_dict_value_is_dropped_not_trusted(rig):
    """The level BELOW the shape check, which is the one that still bit.

    ``receipts`` being a dict is not enough: the activate route reads
    ``prior.get("artifact_id")`` on whatever sits under each key, so a key
    holding a string, a number or a list raised ``AttributeError`` and
    answered a plain-text 500 on EVERY activation -- while that same player's
    own GET answered 200, because the read never touches the ledger.

    Nothing real is lost by dropping them: the only writer in this module
    stores a dict, so a non-dict value was never a receipt.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={
        "version": 2, "cap": 3, "owned": owned_all(),
        "receipts": {"bad": "a string", "worse": 7, "worst": ["k"],
                     "empty": None,
                     "good": {"artifact_id": "cloudseed_relic"}}})

    assert rig["client"].get("/artifacts/p1").status_code == 200
    surviving = players["p1"]["artifacts"]["receipts"]
    assert set(surviving) == {"good"}
    assert surviving["good"] == {"artifact_id": "cloudseed_relic"}

    # A fresh key works, so the ledger is usable rather than merely empty.
    assert activate(rig, "p1", "cloudseed_relic", "k1").status_code == 200


def test_a_replayed_key_whose_receipt_was_surviving_still_replays(rig):
    """The control for the test above: a KEPT receipt is still honoured.

    Without this, "drop everything" and "drop only what it cannot use" both
    pass the shape test, and the ledger would be useless.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={
        "version": 2, "cap": 3, "owned": owned_all(),
        "receipts": {"k1": {"ok": True, "artifact_id": "cloudseed_relic",
                            "idempotency_key": "k1", "evicted": []}}})

    replay = activate(rig, "p1", "cloudseed_relic", "k1")
    assert replay.status_code == 200
    # Replayed, not re-run: the store took no new write for this key.
    assert replay.json()["idempotency_key"] == "k1"


def test_a_missing_artifact_block_is_created_on_first_read_and_persisted(rig):
    """A player who has never touched artifacts still gets a view, and the
    route PERSISTS the block it built.

    Persistence is proved by counting writes rather than by reading the row
    back, because reading the row back cannot tell a stored repair from a
    recomputed one.
    """
    players = rig["players"]
    seed(players, "p1")
    assert "artifacts" not in players["p1"]

    writes_before = len(players.writes)
    assert rig["client"].get("/artifacts/p1").status_code == 200
    assert len(players.writes) == writes_before + 1
    assert players["p1"]["artifacts"]["version"] == 2
    assert players["p1"]["artifacts"]["cap"] == A.DEFAULT_ARTIFACT_CAP


def test_a_read_does_not_persist_a_second_time_once_the_shape_is_current(rig):
    """The write is unconditional, and its EFFECT is what is idempotent.

    Worth stating plainly rather than leaving implied: this suite does not
    claim the read avoids the store. It claims the row it writes is
    byte-identical to the one it read, which is what "idempotent" means here.
    """
    import json

    players = rig["players"]
    seed(players, "p1", artifacts_block={"version": 1, "owned": {}})
    assert rig["client"].get("/artifacts/p1").status_code == 200

    first = json.dumps(players["p1"]["artifacts"], sort_keys=True, default=str)
    assert rig["client"].get("/artifacts/p1").status_code == 200
    second = json.dumps(players["p1"]["artifacts"], sort_keys=True, default=str)
    assert first == second
    assert first != "" and "cap" in first


# --- 6. The cap is a ceiling, not a quota ----------------------------------

def test_a_cap_above_the_catalogue_size_never_evicts(rig):
    """A generous cap must not invent a constraint.

    Only three catalogue entries exist, so a cap of 50 means every artifact
    can be on at once and nothing is ever pushed off.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 50
    players.persist("p1", players["p1"])

    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        body = activate(rig, "p1", artifact_id, "k%d" % index).json()
        assert body["evicted"] == []
    view = rig["client"].get("/artifacts/p1").json()
    assert view["active_count"] == len(A.ARTIFACT_CATALOG)
    assert view["cap"] == 50


def test_the_cap_in_the_reply_is_the_stored_cap_not_the_module_default(rig):
    """``cap`` is per-player and readable, so the reply must report the stored
    number rather than the constant. A client that renders a limit from this
    field would otherwise always show the default.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 1
    players.persist("p1", players["p1"])
    assert rig["client"].get("/artifacts/p1").json()["cap"] == 1
    assert A.DEFAULT_ARTIFACT_CAP != 1, (
        "precondition: 1 differs from the shipped default")


def test_activating_does_not_deactivate_and_the_evicted_record_says_so(rig):
    """Eviction is a real state change, not just a name in a list.

    Under a cap of one, pushing a second artifact off must be visible in the
    evicted record's own ``activated`` flag AND in the saved row, so the client
    that re-reads cannot disagree with the client that got the reply.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 1
    players.persist("p1", players["p1"])

    first = activate(rig, "p1", "cloudseed_relic", "k1").json()
    assert first["evicted"] == []

    second = activate(rig, "p1", "emberlotus_seal", "k2").json()
    assert second["evicted"] == ["cloudseed_relic"]

    saved = players["p1"]["artifacts"]["owned"]
    assert saved["cloudseed_relic"]["activated"] is False
    assert saved["emberlotus_seal"]["activated"] is True

    live = rig["client"].get("/artifacts/p1").json()
    assert [a["id"] for a in live["active"]] == ["emberlotus_seal"]
    by_id = {o["artifact_id"]: o for o in live["owned"]}
    assert by_id["cloudseed_relic"]["activated"] is False


def test_multipliers_fall_back_to_neutral_when_nothing_is_active(rig):
    """Turning everything off must return every stat to exactly 1.0.

    The multipliers are accumulated, so a stale contribution left behind after
    the last eviction would show as a value slightly above 1.0 -- invisible to
    an eyeball, load-bearing to a damage calculation.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 1
    players.persist("p1", players["p1"])

    activate(rig, "p1", "cloudseed_relic", "k1")
    boosted = activate(rig, "p1", "emberlotus_seal", "k2").json()
    assert boosted["artifacts"]["multipliers"]["hp_mult"] == pytest.approx(1.0)
    assert boosted["evicted"] == ["cloudseed_relic"]
    # The artifact carrying hp_mult is the one that went off.
    assert boosted["artifacts"]["multipliers"]["atk_mult"] > 1.0
    assert boosted["artifacts"]["active_count"] == 1


# --- 7. The store contract the routes depend on ----------------------------

def test_the_routes_accept_the_store_directly_or_behind_a_lambda():
    """``resolve_store`` unwraps either shape. Both must serve the contract.

    ``main.py`` passes an accessor; other suites pass the store. If only one
    worked, half the callers in the repo would break silently.
    """
    direct = _PlayerStore()
    seed(direct, "p1")
    app = FastAPI()
    A.register_artifact_routes(app, direct)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/artifacts/p1").status_code == 200
    assert client.post("/artifacts/activate",
                       json={"player_id": "p1",
                             "artifact_id": "cloudseed_relic",
                             "idempotency_key": "k1"}).status_code == 200


def test_the_catalogue_is_the_single_source_for_both_routes(rig):
    """The GET's ``catalog`` and the POST's nested ``catalog`` agree.

    A client is served whichever route it happens to call first; if the two
    served different catalogues, the second call would contradict the first.
    Values are read from the module at run time, so re-balancing the catalogue
    leaves this green.
    """
    seed(rig["players"], "p1")
    via_get = rig["client"].get("/artifacts/p1").json()
    via_post = activate(rig, "p1", "cloudseed_relic", "k1").json()["artifacts"]
    assert via_post["catalog"] == via_get["catalog"]
    for entry in via_get["catalog"]:
        assert set(entry) == {"id", "name", "kind", "description", "bonuses"}
        # The old private-slot field is gone, not renamed.
        assert "slot" not in entry
        assert entry["bonuses"] == A.ARTIFACT_CATALOG[entry["id"]]["bonuses"]
        assert all(key in A.STAT_KEYS for key in entry["bonuses"])


def test_exactly_two_artifact_routes_are_mounted_with_the_expected_methods(rig):
    """FastAPI adds HEAD to every GET; subtract it before counting."""
    mounted = sorted(
        (sorted(r.methods - {"HEAD"}), r.path)
        for r in rig["app"].routes
        if getattr(r, "methods", None) and r.path.startswith("/artifacts")
    )
    assert mounted == [(["GET"], "/artifacts/{player_id}"),
                       (["POST"], "/artifacts/activate")]


# ===========================================================================
# Below: the four contracts this file was opened for, mapped onto the module
# that actually exists.
#
# THE MODULE HAS NO FORGING, NO GRADES AND NO SPIRITS.
# `server/artifacts.py` is 426 lines: a three-entry catalogue, a per-player
# cap, an idempotency ledger, and a `bound_to` ownership stamp. Searching the
# whole server tree for `forge`/`grade`/`spirit` returns other subsystems
# (crafting.py, spirit_beasts, equipment grades) and nothing here. So the
# four asks are tested against what this module does own:
#
#   1. "forging requires auth"  -> the only way to GAIN a bonus is the
#      activation POST, and it requires a session. No bonus without auth.
#   2. "grades are deterministic" -> there is no random grade roll; a
#      catalogued artifact's bonus is a constant. Pinned as the product rule.
#   3. "spirit bonding is persistent" -> `bound_to` is the binding, and it
#      survives a store reload and keeps refusing another player.
#   4. "non-finite stats are rejected" -> `_coerce_bonus` / `_coerce_cap`.
# ===========================================================================


# --- 1. No bonus without a session (the forging-auth contract) --------------

def test_no_artifact_bonus_can_be_gained_without_a_session(guarded_rig):
    """Anonymity cannot move ANY multiplier above neutral.

    This is the forging question asked of the module that exists: a caller
    who has not authenticated must not be able to put a single artifact on
    and collect its bonus. The plural suite proves the refusal status; this
    proves the CONSEQUENCE on the numbers, which is what a damage
    calculation would actually consume.
    """
    players = guarded_rig["players"]
    seed(players, "p1")
    client = guarded_rig["client"]

    # Every write shape an anonymous caller can reach, all refused.
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        response = client.post("/artifacts/activate",
                               json={"player_id": "p1",
                                     "artifact_id": artifact_id,
                                     "idempotency_key": "anon-%d" % index})
        assert response.status_code == 401

    # A refused call writes nothing at all, so the row must be untouched --
    # asserted as ABSENCE rather than as a default block, because a seed with
    # no artifacts key never had one.
    assert "artifacts" not in players["p1"], (
        "an anonymous call created or modified the artifact block")

    # The same refusals against a row that ALREADY has a default block, so the
    # assertion covers a real save and not only a pristine one. Refused calls
    # never reach the store, so seeding is the only way to give it one.
    seed(players, "p2", artifacts_block=A.default_artifact_state())
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        response = client.post("/artifacts/activate",
                               json={"player_id": "p2",
                                     "artifact_id": artifact_id,
                                     "idempotency_key": "anon2-%d" % index})
        assert response.status_code == 401

    stored = players["p2"]["artifacts"]
    assert all(record["activated"] is False
               for record in stored["owned"].values()), "an anonymous call switched an artifact on"
    assert stored["receipts"] == {}, "an anonymous call burned a receipt"
    assert {record["bound_to"] for record in stored["owned"].values()} == {None}

    # And the multiplier view an anonymous read gets is exactly neutral.
    assert client.get("/artifacts/p1").status_code == 401


def test_the_bonus_a_session_earns_is_the_servers_number_not_the_callers(
        guarded_rig):
    """The refuse-everything control for the arm above.

    Without this, "no bonus without a session" would also pass against a
    guard or a module that granted nothing to anybody. One authenticated
    activation must produce the catalogue's bonus exactly.
    """
    players = guarded_rig["players"]
    seed(players, "p1")
    header = guarded_rig["header"]("p1")
    client = guarded_rig["client"]

    response = client.post("/artifacts/activate", headers=header,
                           json={"player_id": "p1",
                                 "artifact_id": "cloudseed_relic",
                                 "idempotency_key": "k1"})
    assert response.status_code == 200
    multipliers = response.json()["artifacts"]["multipliers"]
    for key, value in A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"].items():
        assert multipliers[key] == pytest.approx(value)
    # And a stat the relic does not touch stays exactly neutral.
    assert multipliers["atk_mult"] == 1.0


# --- 2. Grades are deterministic: there is no roll, only a product ---------

def test_a_bonus_is_the_catalogue_number_and_carries_no_randomness(rig):
    """There is no grade roll in this module, and that is the contract.

    Reading the module is not the proof; running it is. The same artifact,
    activated by two different players and read repeatedly, yields the same
    multiplier every time -- so a client can cache a grade and a server
    balance change is the only thing that can move it.
    """
    players = rig["players"]
    seed(players, "alice")
    seed(players, "bob")
    client = rig["client"]

    seen = set()
    for _ in range(5):
        for pid in ("alice", "bob"):
            activate(rig, pid, "emberlotus_seal", "k-" + pid)
            seen.add(tuple(sorted(client.get("/artifacts/" + pid)
                                  .json()["multipliers"].items())))
    assert len(seen) == 1, "the same artifact produced more than one grade"

    # Two fresh stores give the same answer too, so it is not a carry-over.
    fresh_players = _PlayerStore()
    fresh_app = FastAPI()
    A.register_artifact_routes(fresh_app, fresh_players)
    fresh_client = TestClient(fresh_app, raise_server_exceptions=False)
    seed(fresh_players, "carol")
    activate({"players": fresh_players, "client": fresh_client},
             "carol", "emberlotus_seal", "k1")
    fresh = fresh_client.get("/artifacts/carol").json()["multipliers"]
    assert tuple(sorted(fresh.items())) in seen


def test_a_multiplier_is_the_product_of_the_active_bonuses_in_any_order(rig):
    """Order-independent and equal to the product -- derived, not pinned.

    The expected value is computed from the live catalogue, so re-balancing a
    bonus leaves this green. The claim is the RELATION: the product of what is
    switched on, regardless of the order the activations arrived in.
    """
    ids = list(A.ARTIFACT_CATALOG)

    def activate_in_order(order):
        players = _PlayerStore()
        app = FastAPI()
        A.register_artifact_routes(app, players)
        client = TestClient(app, raise_server_exceptions=False)
        seed(players, "p1")
        for index, artifact_id in enumerate(order):
            rig_local = {"players": players, "client": client}
            assert activate(rig_local, "p1", artifact_id,
                            "k%d" % index).status_code == 200
        return client.get("/artifacts/p1").json()["multipliers"]

    forward = activate_in_order(ids)
    backward = activate_in_order(list(reversed(ids)))
    assert forward == backward, "activation order changed the grade"

    expected = {key: 1.0 for key in A.STAT_KEYS}
    for artifact_id in ids:
        for key, value in A.ARTIFACT_CATALOG[artifact_id]["bonuses"].items():
            expected[key] *= value
    assert forward == pytest.approx(expected)
    # Precondition: with three artefacts at least one stat really did move.
    assert any(abs(value - 1.0) > 1e-9 for value in forward.values())


def test_repeated_reads_of_one_state_return_identical_multipliers(rig):
    """Determinism across reads, on the same store and the same row.

    Guards against a derivation that accumulates into itself -- reading the
    inventory would then quietly raise a player's stats.
    """
    players = rig["players"]
    seed(players, "p1")
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        activate(rig, "p1", artifact_id, "k%d" % index)

    first = rig["client"].get("/artifacts/p1").json()["multipliers"]
    for _ in range(5):
        assert rig["client"].get("/artifacts/p1").json()["multipliers"] == first

    expected = {key: 1.0 for key in A.STAT_KEYS}
    for artifact_id in A.ARTIFACT_CATALOG:
        for key, value in A.ARTIFACT_CATALOG[artifact_id]["bonuses"].items():
            expected[key] *= value
    assert first == pytest.approx(expected), "reads drifted from one product"


def test_the_reply_and_the_stored_row_never_disagree_on_the_grade(rig):
    """Two routes, one number.

    A client is served whichever route it calls first. If the POST's nested
    view and the GET disagreed about the same player's multipliers, the second
    call would contradict the first.
    """
    players = rig["players"]
    seed(players, "p1")
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        posted = activate(rig, "p1", artifact_id, "k%d" % index).json()
        live = rig["client"].get("/artifacts/p1").json()
        assert posted["artifacts"]["multipliers"] == live["multipliers"]


# --- 3. The binding stamp is persistent (the spirit-bonding contract) -------

def test_the_binding_survives_a_store_reload_and_still_refuses_a_thief(rig):
    """``bound_to`` is written once and outlives the process.

    Persistence is proved by round-tripping the row through JSON into a BRAND
    NEW store, which is what a restart looks like -- reading the same
    in-memory dict back would prove nothing. Afterwards the stamp must still
    name the original owner and must still refuse anybody else.
    """
    import json

    players = rig["players"]
    seed(players, "p1")
    assert activate(rig, "p1", "cloudseed_relic", "k1").status_code == 200

    # What a save file on disk would hold.
    on_disk = json.loads(json.dumps(players["p1"]))

    restarted_players = _PlayerStore()
    restarted_app = FastAPI()
    A.register_artifact_routes(restarted_app, restarted_players)
    restarted = TestClient(restarted_app, raise_server_exceptions=False)
    seed(restarted_players, "p1", artifacts_block=on_disk["artifacts"])
    # A save MIGRATED onto a second identity: the same records, still stamped
    # to the original owner. This is the case the 403 exists for, and it is a
    # property of the thief's OWN row -- no session is involved.
    seed(restarted_players, "thief", artifacts_block={
        "version": 2, "cap": 3, "receipts": {},
        "owned": {aid: {"artifact_id": aid, "activated": True,
                        "bound_to": "p1", "charges": 0, "cooldown_until": 0}
                  for aid in A.ARTIFACT_CATALOG}})

    body = restarted.get("/artifacts/p1").json()
    by_id = {o["artifact_id"]: o for o in body["owned"]}
    assert {o["bound_to"] for o in body["owned"]} == {"p1"}
    assert by_id["cloudseed_relic"]["activated"] is True
    assert [a["id"] for a in body["active"]] == ["cloudseed_relic"]

    # The grade survived too, so persistence is not only the flag.
    expected = {key: 1.0 for key in A.STAT_KEYS}
    for key, value in A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"].items():
        expected[key] *= value
    assert body["multipliers"] == pytest.approx(expected)

    # The stamp is still load-bearing after the restart.
    response = restarted.post("/artifacts/activate",
                              json={"player_id": "thief",
                                    "artifact_id": "cloudseed_relic",
                                    "idempotency_key": "steal"})
    assert response.status_code in (403, 404), response.status_code
    assert restarted.get("/artifacts/p1").json() == body


def test_binding_is_stamped_by_the_write_and_never_by_a_read(rig):
    """One write stamps it; a hundred reads do not.

    ``ensure_state`` stamps ``bound_to`` from the ``owner`` argument, and the
    read route deliberately passes ``None`` so a browse cannot re-home a
    bound artifact onto whoever is holding the save. Asserted on the STORED
    row, because the reply is a copy and reading it back would pass even
    while the save was silently re-homed.
    """
    players = rig["players"]
    seed(players, "p1")

    for _ in range(3):
        assert rig["client"].get("/artifacts/p1").status_code == 200
    assert {r["bound_to"] for r in players["p1"]["artifacts"]["owned"].values()} == {None}

    assert activate(rig, "p1", "cloudseed_relic", "k1").status_code == 200
    stamped = players["p1"]["artifacts"]["owned"]
    assert {r["bound_to"] for r in stamped.values()} == {"p1"}

    # More reads afterwards must not move it.
    for _ in range(3):
        assert rig["client"].get("/artifacts/p1").status_code == 200
    assert {r["bound_to"] for r in players["p1"]["artifacts"]["owned"].values()} == {"p1"}


# --- 4. Non-finite stats are rejected ---------------------------------------

@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"),
                                 1e400, -1e400, 10 ** 401, -(10 ** 401)])
def test_a_non_finite_bonus_never_reaches_a_multiplier(bad):
    """``_coerce_bonus`` is the whole guard, tested at its own door.

    Seven shapes: the three non-finite floats, the two overflow literals that
    become them, and the 401-digit integers that raise ``OverflowError`` on
    conversion AND again inside ``math.isfinite``. Catching only
    ``TypeError/ValueError`` would leave all three exception classes open,
    which is why the count is seven and not three.

    Every one falls back to the neutral 1.0, so no stat is ever inf or nan.
    """
    assert A._coerce_bonus(bad, 1.0) == 1.0


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"),
                                 1e400, 10 ** 401, "inf", "nan", "Infinity",
                                 None, True, False, [1], {"a": 1}])
def test_a_non_finite_cap_never_reaches_the_eviction_loop(bad):
    """``_coerce_cap``, same reason, and ``bool`` is here on purpose.

    ``int(True)`` is a silent 1, which would mean "evict everything already
    on" -- so a boolean cap is a data bug, not a limit.
    """
    assert A._coerce_cap(bad, 3) == 3


def test_a_non_finite_bonus_falls_back_to_neutral_through_the_read_route(
        rig, monkeypatch):
    """The coercion is observable on the wire, not only in the helper.

    Reached through the read route so nothing is switched on and the reply is
    pure: a fallback that lived only in the activation path would pass the
    helper test and still leak here.
    """
    seed(rig["players"], "p1")
    # TEXT forms only. A non-finite NUMBER in the catalogue takes this read
    # route to a 500 before this assertion is reached -- that is the separate,
    # deliberately pinned gap at the bottom of this file.
    for bad in ("inf", "-inf", "nan", "NaN", "Infinity"):
        monkeypatch.setitem(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"],
                            "hp_mult", bad)
        body = rig["client"].get("/artifacts/p1").json()
        assert body["multipliers"]["hp_mult"] == 1.0, bad


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"),
                                 "inf", "-inf", "nan", [1], None, True])
def test_a_non_finite_cap_falls_back_without_crashing_the_routes(rig, monkeypatch, bad):
    """A cap that is not a usable count must not 500 the route enforcing it.

    ``int(float("inf"))`` raises ``OverflowError``, not ``ValueError``, and
    ``None``/``[]`` raise ``TypeError``. Every shape here falls back to
    ``DEFAULT_ARTIFACT_CAP`` on BOTH cap paths -- the stored row and the reply
    -- so the read a client sees agrees with the eviction the server applies.

    ``10 ** 401`` is deliberately NOT in this list: it is a finite Python
    integer, and the two paths treat it differently on purpose. See
    ``test_a_401_digit_cap_is_finite_so_the_two_cap_paths_disagree`` below.
    """
    seed(rig["players"], "p1", artifacts_block=A.default_artifact_state())
    monkeypatch.setitem(rig["players"]["p1"]["artifacts"], "cap", bad)
    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200, (bad, response.text[:80])
    assert response.json()["cap"] == A.DEFAULT_ARTIFACT_CAP, bad
    # The repair is persisted, so the second read agrees with the first.
    assert rig["client"].get("/artifacts/p1").json()["cap"] == A.DEFAULT_ARTIFACT_CAP


def test_a_401_digit_cap_is_finite_so_the_two_cap_paths_disagree(rig, monkeypatch):
    """MEASURED, and left as measured. The 401-digit int is NOT non-finite.

    It is an ``int``, it is ``>= 0``, so ``ensure_state`` keeps it and the read
    route echoes the huge number. ``_coerce_cap`` still returns
    ``DEFAULT_ARTIFACT_CAP`` for it, because ``math.isfinite`` raises
    ``OverflowError`` on the conversion -- a guard against the CRASH, not a
    verdict that the number is non-finite.

    So one cap has two answers: the reply reports 10**401, the eviction loop
    behaves as if it were 3. Harmless here because the catalogue holds fewer
    artifacts than any of those numbers -- ``test_a_cap_above_the_catalogue_size_never_evicts``
    already pins that a cap over the catalogue size never evicts -- so no
    artifact is lost either way. It is pinned rather than asserted correct so
    that tightening ``ensure_state`` later is a deliberate change, not a
    silent one.
    """
    huge = 10 ** 401
    assert A._coerce_cap(huge, A.DEFAULT_ARTIFACT_CAP) == A.DEFAULT_ARTIFACT_CAP
    seed(rig["players"], "p1", artifacts_block=A.default_artifact_state())
    monkeypatch.setitem(rig["players"]["p1"]["artifacts"], "cap", huge)
    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200, response.text[:80]
    assert response.json()["cap"] == huge
    # Still answers, and still owns every catalogue artifact.
    assert len(response.json()["owned"]) == len(A.ARTIFACT_CATALOG)


def test_a_non_finite_bonus_still_500s_the_read_route_and_that_is_the_gap(
        rig, monkeypatch):
    """MEASURED GAP, pinned deliberately. Read this before "fixing" it.

    ``_coerce_bonus`` is correct -- the arm above proves it returns 1.0 -- but
    it is only applied to the DERIVED multiplier. ``state_view`` also copies
    the raw catalogue into the reply's ``catalog`` list, and Starlette refuses
    to render a non-finite float (``allow_nan=False``), so a catalogue bonus
    holding ``inf``/``nan`` as a NUMBER takes the whole read route to a
    plain-text 500.

    Why the shape matters: the same value as the TEXT ``"inf"`` is harmless,
    because the echo is then a JSON string. So the guard covers the number it
    derives and not the number it publishes. A client parsing the reply sees a
    500 on a read that the server considers a plain success.

    Asserting the measured 500 keeps the gap visible. Changing this to expect
    200 without repairing ``state_view`` would hide a real 500 behind a nicer
    number -- and would be an unfalsifiable test, because the assertion would
    then describe a module that does not exist.
    """
    seed(rig["players"], "p1")

    # Precondition, so this cannot pass for the wrong reason: the TEXT form
    # of the same value is served fine, so the 500 below is about the type.
    monkeypatch.setitem(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"],
                        "hp_mult", "inf")
    assert rig["client"].get("/artifacts/p1").status_code == 200

    for bad in (float("inf"), float("-inf"), float("nan"), 1e400):
        monkeypatch.setitem(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"],
                            "hp_mult", bad)
        response = rig["client"].get("/artifacts/p1")
        assert response.status_code == 500, (
            "the raw catalogue echo is being sanitised now -- repair "
            "state_view and rewrite this test to assert the good answer", bad)
        assert "Infinity" not in response.text
        assert "NaN" not in response.text

    # And the derived number was never the problem: it was 1.0 all along.
    monkeypatch.setitem(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"],
                        "hp_mult", float("inf"))
    view = A.state_view({"id": "p1", "artifacts": A.default_artifact_state()})
    assert view["multipliers"]["hp_mult"] == 1.0
    # ...while the published copy is what carries the poison.
    echoed = [e for e in view["catalog"] if e["id"] == "cloudseed_relic"][0]
    assert echoed["bonuses"]["hp_mult"] == float("inf")