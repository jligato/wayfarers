"""Route contract for ``server/artifacts.py``, pinned by running it.

Design this file answers three questions about the wire, in the order a
client meets them:

1. **What shape comes back.** The view keys, the owned-record keys, the
   catalogue keys, and the five multiplier keys are FROZEN. Adding a key is
   safe for a client; renaming or removing one breaks every caller, so the
   set is asserted exactly. Numbers are read from the module at run time,
   so re-balancing the catalogue leaves this file green.
2. **Who is allowed to ask.** Measured: ``register_artifact_routes`` on a
   bare app answers an anonymous GET and POST with 200. The route module
   declares no auth of its own. The auth requirement is real and lives one
   layer up -- ``main.py`` mounts these routes and then runs
   ``ownership_guard.install_ownership_guard``, which attaches
   ``require_player`` to both. Both layers are tested here: the bare app
   proves what the module itself does, and the guarded app proves what a
   shipped caller actually meets.
3. **What happens when it goes wrong.** 404 for an unknown artifact and an
   unknown player, 404 for a save record that is not an object, 403 for an
   artifact bound to a different player, 409 for an idempotency key replayed
   with a different artifact, 422 for a body the request model rejects, and
   405 for the wrong verb. The corrupt-save arm was a 500 in an earlier
   revision and is now a 404; see
   ``test_a_corrupt_owned_record_is_refused_with_404_on_activate`` for the
   measured reason that changed.

Three measured behaviours the module's own docstrings get wrong or do not
mention. They are pinned here because the wire is what a client sees:

* **The activation body has no magnitude field, and extra fields are
  IGNORED, not rejected.** Posting ``power``/``magnitude``/``roll_value``
  answers 200 and the server's own catalogue number is what lands. Pydantic's
  default is to drop unknown keys, so the field is unreachable by
  construction -- but a client that believes it is sending a multiplier gets
  silence, not an error.
* **A read never claims ownership.** ``GET /artifacts/{player_id}`` passes
  ``None`` as the owner, so ``bound_to`` stays null. One activate stamps it.
* **A replay re-derives the snapshot.** The receipt's decision (``ok``,
  ``artifact_id``, ``idempotency_key``, ``evicted``) is replayed verbatim;
  its ``artifacts`` snapshot is re-derived, so a replay after the state
  moved on reports the CURRENT view, not the old one.

Running it (the exact invocation; a bare run HANGS instead of failing):

    cd G:/Projects/wayfarers-cycle && env -u WC_PG_HOST -u WC_PG_DB \\
      -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT -u PYTHONPATH \\
      <python> -m pytest server/tests/test_artifacts_contract.py -q \\
      -p no:cacheprovider

It does NOT import ``server.main``: the repo-root ``conftest.py`` imports
that module at collection time, so a peer editing ``main.py`` aborts every
run in the repo. The guard tests install the real
``ownership_guard.install_ownership_guard`` onto this file's own app, which
is what ``main.py`` does, without paying that import.
"""
from __future__ import annotations

import json
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
import ownership_guard as guard  # noqa: E402


# --- The exact reply shapes, frozen -----------------------------------------
# Key SETS are frozen: a client reads these names. Values are not.
VIEW_KEYS = {"ok", "version", "cap", "catalog", "owned", "active",
             "active_count", "multipliers"}
OWNED_KEYS = {"artifact_id", "activated", "bound_to", "charges",
              "cooldown_until"}
CATALOG_KEYS = {"id", "name", "kind", "description", "bonuses"}
ACTIVATE_KEYS = {"ok", "artifact_id", "idempotency_key", "evicted", "artifacts"}


class _PlayerStore:
    """The ``PlayerStore`` surface these two routes actually use.

    ``persist`` and ``__setitem__`` are load-bearing, not decoration. The GET
    route writes ``store[player_id] = player`` to persist its shape repair,
    and in memory mode the real store hands back a detached copy, so a write
    nothing persists is a write nothing sees.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def persist(self, pid, player=None):
        if player is not None:
            self.rows[pid] = player
        return player

    def __contains__(self, pid):
        return pid in self.rows

    def __getitem__(self, pid):
        return self.rows[pid]

    def __setitem__(self, pid, player):
        self.rows[pid] = player

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

    # NOTE: deliberately returns the store handed to the routes as a LAMBDA.


@pytest.fixture
def guarded_rig(rig):
    """The same app WITH the shipped ownership guard installed.

    ``main.py`` mounts the routes first and then runs
    ``install_ownership_guard``, so this is the order a real caller meets.
    Running it here (rather than importing ``server.main``) measures the
    guard against these two routes without the collection-time import.
    """
    guarded = guard.install_ownership_guard(rig["app"])
    rig["guarded_paths"] = sorted(p for p in guarded if p.startswith("/artifacts"))
    return rig


def seed(players, pid, artifacts_block=None):
    row = {"id": pid, "name": pid}
    if artifacts_block is not None:
        row["artifacts"] = artifacts_block
    players.persist(pid, row)
    return pid


def activate(rig, pid, artifact_id, key, **extra):
    body = {"player_id": pid, "artifact_id": artifact_id, "idempotency_key": key}
    body.update(extra)
    return rig["client"].post("/artifacts/activate", json=body)


def owned_of(payload, artifact_id):
    for record in payload["owned"]:
        if record["artifact_id"] == artifact_id:
            return record
    return None


# --- 1. The reply shape ------------------------------------------------------

def test_get_answers_exactly_the_frozen_view_keys(rig):
    seed(rig["players"], "p1")
    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == VIEW_KEYS, f"view key set changed: {sorted(body)}"
    assert body["ok"] is True
    assert body["version"] == 2
    # ``cap`` replaces the old relic_1/2/3 slot list. The list must not
    # come back under any name: a numbered slot list cannot answer what
    # happens to a fourth artifact.
    assert "slots" not in body
    assert set(body["multipliers"]) == set(A.STAT_KEYS)


def test_get_owned_records_and_catalogue_carry_their_frozen_keys(rig):
    seed(rig["players"], "p1")
    body = rig["client"].get("/artifacts/p1").json()
    assert set(body["catalog"][0]) == CATALOG_KEYS
    assert sorted(o["artifact_id"] for o in body["owned"]) == sorted(A.ARTIFACT_CATALOG)
    for record in body["owned"]:
        assert set(record) == OWNED_KEYS, f"owned record keys changed: {sorted(record)}"


def test_the_catalogue_the_server_serves_is_the_module_catalogue(rig):
    seed(rig["players"], "p1")
    body = rig["client"].get("/artifacts/p1").json()
    assert sorted(c["id"] for c in body["catalog"]) == sorted(A.ARTIFACT_CATALOG)
    for entry in body["catalog"]:
        # No entry carries a ``slot`` key: the field is gone, not renamed.
        assert "slot" not in entry
        assert entry["bonuses"] == A.ARTIFACT_CATALOG[entry["id"]]["bonuses"]


def test_a_nothing_active_view_reports_neutral_multipliers(rig):
    seed(rig["players"], "p1")
    body = rig["client"].get("/artifacts/p1").json()
    assert body["active"] == []
    assert body["active_count"] == 0
    assert all(v == 1.0 for v in body["multipliers"].values())


def test_activate_answers_the_frozen_envelope_and_a_full_view(rig):
    seed(rig["players"], "p1")
    response = activate(rig, "p1", "cloudseed_relic", "k1")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ACTIVATE_KEYS
    assert body["ok"] is True
    assert body["artifact_id"] == "cloudseed_relic"
    assert body["idempotency_key"] == "k1"
    assert body["evicted"] == []
    assert set(body["artifacts"]) == VIEW_KEYS


def test_the_bonus_that_lands_is_the_server_number_read_at_run_time(rig):
    seed(rig["players"], "p1")
    body = activate(rig, "p1", "emberlotus_seal", "k1").json()
    catalogue = A.ARTIFACT_CATALOG["emberlotus_seal"]["bonuses"]
    landed = body["artifacts"]["multipliers"]
    assert landed["atk_mult"] == pytest.approx(float(catalogue["atk_mult"]))
    assert landed["def_mult"] == pytest.approx(float(catalogue["def_mult"]))
    # Untouched stats stay exactly neutral.
    assert landed["hp_mult"] == 1.0
    assert body["artifacts"]["active_count"] == 1


# --- 2. Auth: what the module does, and what a shipped caller meets ---------

def test_the_bare_route_module_declares_no_auth_of_its_own(rig):
    """Measured: an anonymous caller gets 200 from both routes.

    Pinned as the TRUTH about this module, not as the desired state. The
    module is a route TABLE, not a secured surface; the requirement is
    supplied by ``ownership_guard`` at mount time. If someone later adds
    auth to the module itself, this test goes red and says so -- which is the
    point of pinning it.
    """
    seed(rig["players"], "p1")
    assert rig["client"].get("/artifacts/p1").status_code == 200
    assert activate(rig, "p1", "cloudseed_relic", "anon").status_code == 200


def test_the_guard_installer_covers_both_artifact_routes(guarded_rig):
    """The auth requirement is reachable: the installer matches both shapes.

    ``/artifacts/{player_id}`` carries a bare path claim. ``/artifacts/
    activate`` carries its claim in the JSON body, and the installer reads
    BODY claims too -- so it is guarded, not skipped for naming no player.
    """
    assert guarded_rig["guarded_paths"] == ["/artifacts/activate",
                                           "/artifacts/{player_id}"]


def test_anonymous_is_refused_with_401_on_both_routes(guarded_rig):
    seed(guarded_rig["players"], "p1")
    client = guarded_rig["client"]
    assert client.get("/artifacts/p1").status_code == 401
    assert activate(guarded_rig, "p1", "cloudseed_relic", "k1").status_code == 401


def test_a_session_for_the_named_player_is_served(guarded_rig):
    """The refuse-everything arm is not the whole story: the owner still works.

    Without this, every refusal assertion above would pass for the wrong
    reason -- a guard that refused EVERYTHING would make them all green.
    """
    import auth
    seed(guarded_rig["players"], "p1")
    header = {"Authorization": "Bearer " + auth.issue_session("p1")}
    client = guarded_rig["client"]
    assert client.get("/artifacts/p1", headers=header).status_code == 200
    # The POST arm is a SEPARATE request and carries its own claim, so it
    # gets its own assertion; reusing the GET's would prove nothing about it.
    response = client.post("/artifacts/activate", headers=header,
                           json={"player_id": "p1",
                                 "artifact_id": "cloudseed_relic",
                                 "idempotency_key": "k1"})
    assert response.status_code == 200


def test_a_session_naming_another_player_is_refused_with_403(guarded_rig):
    """The shape guard answers 'is the claim the caller', not 'is it allowed'.

    A caller with a VALID session that names somebody else is refused on
    both routes -- the path claim on the GET, the body claim on the POST.
    """
    import auth
    players = guarded_rig["players"]
    seed(players, "owner")
    seed(players, "thief")
    header = {"Authorization": "Bearer " + auth.issue_session("thief")}
    client = guarded_rig["client"]
    assert client.get("/artifacts/owner", headers=header).status_code == 403
    response = client.post("/artifacts/activate", headers=header,
                           json={"player_id": "owner",
                                 "artifact_id": "cloudseed_relic",
                                 "idempotency_key": "k1"})
    assert response.status_code == 403


def test_a_garbage_session_token_is_refused(rig):
    """A wrong-shaped credential is refused, not treated as anonymous."""
    seed(rig["players"], "p1")
    guard.install_ownership_guard(rig["app"])
    response = rig["client"].get("/artifacts/p1",
                                 headers={"Authorization": "Bearer not-a-session"})
    assert response.status_code == 401


# --- 3. Ownership: the 403 that is NOT the guard's --------------------------

def test_an_artifact_bound_to_another_player_refuses_with_403(rig):
    """The artifact-level check, distinct from the session-level one.

    The stamp lives on the ACTING player's own record (what a migrated save
    from a prior identity looks like), not on another player's row.
    """
    players = rig["players"]
    seed(players, "thief")
    state = A.ensure_state(players["thief"], "thief")
    state["owned"]["cloudseed_relic"]["bound_to"] = "owner"
    players.persist("thief", players["thief"])

    response = activate(rig, "thief", "cloudseed_relic", "k1")
    assert response.status_code == 403
    assert "bound to another player" in response.json()["detail"]
    # A refusal must not half-apply: still inactive, no receipt burned.
    record = players["thief"]["artifacts"]["owned"]["cloudseed_relic"]
    assert record["activated"] is False
    assert "k1" not in players["thief"]["artifacts"]["receipts"]


def test_a_404_and_a_403_are_different_codes_for_the_two_situations(rig):
    """A 404 for a stolen item would tell an attacker the id is real.

    One artifact is bound elsewhere (403); a different artifact of the same
    player is simply unbound and activates fine (200). The two codes are
    deliberate and this pins that they do not collapse into one.
    """
    players = rig["players"]
    seed(players, "t2")
    state = A.ensure_state(players["t2"], "t2")
    state["owned"]["emberlotus_seal"]["bound_to"] = "somebody_else"
    players.persist("t2", players["t2"])

    assert activate(rig, "t2", "emberlotus_seal", "x").status_code == 403
    assert activate(rig, "t2", "tideglass_compass", "y").status_code == 200


# --- 4. Error cases ----------------------------------------------------------

def test_an_unknown_artifact_id_is_404(rig):
    seed(rig["players"], "p1")
    response = activate(rig, "p1", "no_such_artifact", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_an_unknown_player_is_404(rig):
    seed(rig["players"], "p1")
    response = activate(rig, "ghost", "cloudseed_relic", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player not found"


def test_the_artifact_id_is_checked_before_the_player(rig):
    """Ordering is observable when both are wrong, and it is fixed.

    With both unknown, the answer names the ARTIFACT. Pinning the order
    stops a refactor from silently swapping which error a client sees.
    """
    seed(rig["players"], "p1")
    response = activate(rig, "ghost", "no_such_artifact", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Artifact not found"


def test_an_unknown_player_on_the_read_route_is_404(rig):
    response = rig["client"].get("/artifacts/ghost")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player not found"


def test_a_replayed_key_naming_a_different_artifact_is_409(rig):
    seed(rig["players"], "p1")
    assert activate(rig, "p1", "cloudseed_relic", "shared").status_code == 200
    response = activate(rig, "p1", "tideglass_compass", "shared")
    assert response.status_code == 409
    assert response.json()["detail"] == "Idempotency key payload mismatch"
    # The original decision stands: the mismatch did not activate the second.
    view = rig["client"].get("/artifacts/p1").json()
    assert [a["id"] for a in view["active"]] == ["cloudseed_relic"]


def test_a_catalogue_artifact_missing_from_owned_refuses_with_404(rig):
    """The 'does not own' branch is reachable, and it is a 404.

    ``ensure_state`` backfills the SHAPE of every record it finds but never
    invents a record the save is missing, so a save that lost (or never
    had) an entry answers 404 'Player does not own artifact' rather than
    403. The code differs from the bound-to-elsewhere 403 on purpose: a 404
    here reveals nothing about another player.
    """
    players = rig["players"]
    seed(players, "p1")
    keep = {"artifact_id": "emberlotus_seal", "activated": False,
            "bound_to": "p1", "charges": 0, "cooldown_until": 0}
    seed(players, "p1", artifacts_block={"version": 2, "cap": 3,
                                         "receipts": {}, "owned": {"emberlotus_seal": keep}})
    players.persist("p1", players["p1"])

    response = activate(rig, "p1", "cloudseed_relic", "k1")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player does not own artifact"
    # And the sibling the player DOES own still works, so the refusal is
    # about the named id and not about the save being unusable.
    assert activate(rig, "p1", "emberlotus_seal", "k2").status_code == 200


def test_an_empty_owned_map_refuses_every_activation_with_404(rig):
    """The degenerate case of the same branch: nothing is owned, nothing activates."""
    players = rig["players"]
    seed(players, "p2", artifacts_block={"version": 2, "cap": 3,
                                         "receipts": {}, "owned": {}})
    players.persist("p2", players["p2"])
    for artifact_id in A.ARTIFACT_CATALOG:
        response = activate(rig, "p2", artifact_id, "k-" + artifact_id)
        assert response.status_code == 404
        assert response.json()["detail"] == "Player does not own artifact"


@pytest.mark.parametrize("bad_key", ["", "x" * 129])
def test_an_idempotency_key_outside_its_length_bounds_is_422(rig, bad_key):
    seed(rig["players"], "p1")
    assert activate(rig, "p1", "cloudseed_relic", bad_key).status_code == 422


def test_a_body_missing_the_idempotency_key_is_422(rig):
    seed(rig["players"], "p1")
    response = rig["client"].post(
        "/artifacts/activate",
        json={"player_id": "p1", "artifact_id": "cloudseed_relic"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "idempotency_key"]
    assert detail[0]["type"] == "missing"


def test_a_non_string_player_id_is_422(rig):
    """The request model is strict on type, even though it is lax on extras."""
    seed(rig["players"], "p1")
    response = rig["client"].post(
        "/artifacts/activate",
        json={"player_id": 12345, "artifact_id": "cloudseed_relic",
              "idempotency_key": "k1"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_type"


# --- 5. The body carries no magnitude, and extras are dropped ---------------

def test_the_activation_model_declares_exactly_three_fields():
    """A magnitude field must never appear here. If one is added, this fails."""
    assert set(A.ArtifactActivateRequest.model_fields) == {
        "player_id", "artifact_id", "idempotency_key"}


def test_caller_supplied_magnitudes_are_dropped_and_the_server_number_lands(rig):
    """The exploit the body is shaped to stop.

    A caller posts four invented fields trying to inflate the bonus. The
    request answers 200 and the multiplier is the catalogue's own number, so
    nothing the caller said reached the arithmetic.
    """
    seed(rig["players"], "p1")
    baseline = rig["client"].get("/artifacts/p1").json()["multipliers"]
    assert baseline["atk_mult"] == 1.0

    response = activate(rig, "p1", "emberlotus_seal", "m1", power=9999,
                        magnitude=5000, roll_value=42, target="everyone")
    assert response.status_code == 200
    catalogue = float(A.ARTIFACT_CATALOG["emberlotus_seal"]["bonuses"]["atk_mult"])
    assert response.json()["artifacts"]["multipliers"]["atk_mult"] == pytest.approx(catalogue)
    # Unknown keys are IGNORED, not rejected. A client that thinks it is
    # sending a multiplier gets silence rather than an error.
    assert "power" not in A.ArtifactActivateRequest.model_fields


# --- 6. Reads never claim ownership; activation does ------------------------

def test_a_read_leaves_bound_to_null_and_writes_no_claim(rig):
    seed(rig["players"], "p1")
    body = rig["client"].get("/artifacts/p1").json()
    assert body["owned"][0]["bound_to"] is None
    # And the null is not just in the reply -- nothing was written back.
    for record in players_bound(rig):
        assert record is None


def players_bound(rig):
    saved = rig["players"]["p1"]["artifacts"]["owned"]
    return [rec.get("bound_to") for rec in saved.values()]


def test_one_activation_stamps_bound_to_on_every_owned_record(rig):
    """MEASURED: the claim is NOT limited to the artifact that was activated.

    ``ensure_state`` stamps ``bound_to`` on every unbound record it walks,
    and the activate route passes the player id in. So one activation
    claims all three. This is pinned as the truth about the wire: a client
    reading the view sees every owned record bound to the activating
    player, not just the one it asked for.
    """
    seed(rig["players"], "p1")
    activate(rig, "p1", "cloudseed_relic", "k1")
    saved = rig["players"]["p1"]["artifacts"]["owned"]
    assert {rec["bound_to"] for rec in saved.values()} == {"p1"}


def test_the_read_route_persists_its_shape_repair_and_stays_idempotent(rig):
    seed(rig["players"], "p1",
         artifacts_block={"version": 1, "owned": {}})
    assert "cap" not in rig["players"]["p1"]["artifacts"]

    assert rig["client"].get("/artifacts/p1").status_code == 200
    saved = rig["players"]["p1"]["artifacts"]
    assert saved["version"] == 2
    assert saved["cap"] == A.DEFAULT_ARTIFACT_CAP
    assert saved["receipts"] == {}

    import json as _json
    first = _json.dumps(saved, sort_keys=True, default=str)
    rig["client"].get("/artifacts/p1")
    second = _json.dumps(saved, sort_keys=True, default=str)
    assert first == second


@pytest.mark.parametrize("bad_cap", ["three", -5, True, None, 1.5])
def test_a_cap_that_is_not_a_usable_int_is_repaired_to_the_default(rig, bad_cap):
    """A cap below 1 once drove an eviction loop off the end of a list.

    Each of these is repaired to the shipped default rather than trusted.
    ``True`` is in the list deliberately: ``bool`` is a subclass of ``int``
    in Python, so a naive ``isinstance(cap, int)`` would accept it.
    """
    seed(rig["players"], "p1", artifacts_block={"version": 2, "owned": {},
                                                "cap": bad_cap})
    assert rig["client"].get("/artifacts/p1").json()["cap"] == A.DEFAULT_ARTIFACT_CAP


# --- 7. The cap, the eviction, and its visible receipt ----------------------

def test_activating_up_to_the_cap_evicts_nobody(rig):
    players = rig["players"]
    seed(players, "p1")
    for artifact_id in A.ARTIFACT_CATALOG:
        response = activate(rig, "p1", artifact_id, "k-" + artifact_id)
        assert response.status_code == 200
        assert response.json()["evicted"] == []
    view = rig["client"].get("/artifacts/p1").json()
    assert view["active_count"] == len(A.ARTIFACT_CATALOG)


def test_reactivating_an_already_active_artifact_changes_nothing(rig):
    """The incoming id is excluded from the eviction set."""
    players = rig["players"]
    seed(players, "p1")
    for artifact_id in A.ARTIFACT_CATALOG:
        activate(rig, "p1", artifact_id, "k-" + artifact_id)
    response = activate(rig, "p1", "tideglass_compass", "again")
    assert response.status_code == 200
    assert response.json()["evicted"] == []
    assert response.json()["artifacts"]["active_count"] == len(A.ARTIFACT_CATALOG)


def test_exceeding_the_cap_evicts_and_reports_which_one(rig):
    """Eviction is reported, not merely inferable from a count.

    Cap 2 with the catalogue's first two active; a third activation must
    evict one, name it in ``evicted``, and leave the count at the cap.

    WHICH one is measured, not guessed: the eviction picks the lowest
    catalogue index among the already-active ones, which is the FIRST of
    the two -- not the second. With the catalogue's first two switched on
    and the third arriving, ``cloudseed_relic`` goes off.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 2
    players.persist("p1", players["p1"])

    catalogue_order = list(A.ARTIFACT_CATALOG)
    for artifact_id in catalogue_order[:2]:
        activate(rig, "p1", artifact_id, "k-" + artifact_id)

    incoming = catalogue_order[2]
    body = activate(rig, "p1", incoming, "k-third").json()
    evicted = body["evicted"]
    assert evicted == [catalogue_order[0]], (
        "eviction picks the lowest catalogue index among active records")
    assert body["artifacts"]["active_count"] == 2
    assert sorted(a["id"] for a in body["artifacts"]["active"]) == sorted(
        [catalogue_order[1], incoming])


def test_a_cap_of_one_or_zero_evicts_without_crashing(rig):
    """Cap 0 or 1 means 'evict everything already on' -- a list, not a crash."""
    for cap in (0, 1):
        players = rig["players"]
        seed(players, "p%d" % cap)
        state = A.ensure_state(players["p%d" % cap], "p%d" % cap)
        state["cap"] = cap
        players.persist("p%d" % cap, players["p%d" % cap])
        pid = "p%d" % cap

        activate(rig, pid, "cloudseed_relic", "a")
        body = activate(rig, pid, "emberlotus_seal", "b").json()
        assert body["evicted"] == ["cloudseed_relic"]
        assert body["artifacts"]["active_count"] == 1


def test_an_uncatalogued_record_holds_no_cap_slot_and_grants_no_bonus(rig):
    """A record the catalogue does not define is invisible to both readers.

    It used to hold a cap slot while granting nothing, so with cap 3 a
    player could switch on only two real artifacts: the third activation
    evicted a perfectly good artifact to make room for a phantom. The fix
    is that the cap counts exactly what the view counts.

    The SECOND half is the load-bearing one. Under no pressure (fewer
    activations than the cap) the phantom is invisible to both versions of
    the code, so a test that only activates one artifact proves nothing.
    This drives every catalogue entry under a cap of 2 and requires all
    three to end up on with a phantom sitting in ``owned``.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["cap"] = 2
    state["owned"]["retired_item"] = {"artifact_id": "retired_item",
                                      "activated": True, "bound_to": "p1",
                                      "charges": 0, "cooldown_until": 0}
    players.persist("p1", players["p1"])

    catalogue_ids = list(A.ARTIFACT_CATALOG)
    for index, artifact_id in enumerate(catalogue_ids):
        assert activate(rig, "p1", artifact_id, "k%d" % index).status_code == 200

    view = rig["client"].get("/artifacts/p1").json()
    # The phantom grants nothing and is never reported active.
    assert all(a["id"] in A.ARTIFACT_CATALOG for a in view["active"])
    assert "retired_item" not in [a["id"] for a in view["active"]]
    # The phantom must not consume one of the cap's slots. With it counted,
    # the third real activation would evict a REAL artifact to make room for
    # a record that grants nothing, leaving the player one short of the cap.
    assert view["active_count"] == state["cap"] == 2
    assert sorted(a["id"] for a in view["active"]) == sorted(catalogue_ids[:2]) \
        or sorted(a["id"] for a in view["active"]) == sorted(catalogue_ids[1:])
    # And every bonus is the catalogue's own, un-multiplied by the phantom.
    for record in view["active"]:
        for key, value in record["bonuses"].items():
            assert view["multipliers"][key] == pytest.approx(float(value))


# --- 8. Idempotency: replay re-derives, it does not re-report ---------------

def test_replaying_the_same_key_and_artifact_returns_the_same_decision(rig):
    seed(rig["players"], "p1")
    first = activate(rig, "p1", "cloudseed_relic", "k1").json()
    second = activate(rig, "p1", "cloudseed_relic", "k1")
    assert second.status_code == 200
    assert second.json() == first


def test_a_replay_re_derives_the_snapshot_from_current_state(rig):
    """The receipt's DECISION is final; its snapshot is not.

    The stored snapshot is a report of state that has since moved on. If a
    replay returned it verbatim, it would claim an active count that the
    player's own GET no longer agrees with -- a lie in the one response
    that promises to be the truth.
    """
    players = rig["players"]
    seed(players, "p1")
    activate(rig, "p1", "cloudseed_relic", "k1")
    activate(rig, "p1", "emberlotus_seal", "k2")

    replay = activate(rig, "p1", "cloudseed_relic", "k1").json()
    live = rig["client"].get("/artifacts/p1").json()
    assert replay["artifacts"] == live
    assert replay["artifact_id"] == "cloudseed_relic"
    assert replay["idempotency_key"] == "k1"


def test_the_receipt_ledger_is_bounded_and_keeps_the_newest(rig):
    players = rig["players"]
    seed(players, "p1")
    ids = list(A.ARTIFACT_CATALOG)
    for index in range(60):
        activate(rig, "p1", ids[index % len(ids)], "prune-%03d" % index)
    receipts = players["p1"]["artifacts"]["receipts"]
    assert len(receipts) == 50
    assert "prune-059" in receipts and "prune-000" not in receipts


# --- 9. The corrupt-save cases, measured -------------------------------------

def test_a_corrupt_owned_record_is_skipped_by_the_read_route_not_500(rig):
    """A half-written save renders as 'not active', never as a 500.

    Two of the three catalogue entries are intact; the third is a bare
    string where an object belongs. The read skips it and answers 200.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["owned"]["cloudseed_relic"] = "junk"
    players.persist("p1", players["p1"])

    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200
    body = response.json()
    assert "cloudseed_relic" not in [o["artifact_id"] for o in body["owned"]]
    assert body["active"] == []
    # Every element is still an object, so a client reading owned[i].activated
    # never trips over a bare string.
    assert all(isinstance(record, dict) for record in body["owned"])


def test_an_intact_artifact_still_activates_beside_a_corrupt_record(rig):
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["owned"]["cloudseed_relic"] = "junk"
    players.persist("p1", players["p1"])
    assert activate(rig, "p1", "emberlotus_seal", "e1").status_code == 200


def test_a_catalogue_bonus_outside_the_five_keys_is_ignored_not_a_crash(rig):
    """An unrecognised bonus key is dropped, matching the loadout resolver.

    A catalogue entry naming a key outside STAT_KEYS once raised KeyError,
    which the route's own handler reported as 404 'Player not found' for a
    player who plainly existed.
    """
    players = rig["players"]
    seed(players, "p1")
    added = {"id": "probe_only", "name": "Probe", "kind": "gubao",
             "description": "d", "bonuses": {"atk_mult": 1.5, "luck_mult": 9.0}}
    A.ARTIFACT_CATALOG["probe_only"] = added
    try:
        state = A.ensure_state(players["p1"], "p1")
        state["owned"]["probe_only"] = {"artifact_id": "probe_only",
                                        "activated": True, "bound_to": "p1",
                                        "charges": 0, "cooldown_until": 0}
        players.persist("p1", players["p1"])

        response = rig["client"].get("/artifacts/p1")
        assert response.status_code == 200
        multipliers = response.json()["multipliers"]
        assert multipliers["atk_mult"] == pytest.approx(1.5)
        # The unknown key is not invented into the reply.
        assert set(multipliers) == set(A.STAT_KEYS)
        assert activate(rig, "p1", "probe_only", "w1").status_code == 200
    finally:
        A.ARTIFACT_CATALOG.pop("probe_only", None)


def test_a_corrupt_owned_record_is_refused_with_404_on_activate(rig):
    """A non-dict owned record is a 404 on activate, not a 500.

    WAS a 500. ``_owned_artifact`` used to call ``.get("bound_to")`` on
    whatever the save held, so activating the corrupt artifact itself
    answered a plain-text 500 while the player's own GET had just answered
    200 for the same row. It now refuses a non-dict record the way
    ``ensure_state``, ``_derive``, ``state_view`` and ``_enforce_cap`` all
    skip one, and the route answers 404 'Player does not own artifact'.

    The 404 is the right code rather than a convenient one: the record is
    not a usable artifact by ANY of the module's rules, so it must read as
    absent rather than as a binding conflict (403) or a save failure.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["owned"]["cloudseed_relic"] = "junk"
    players.persist("p1", players["p1"])

    response = activate(rig, "p1", "cloudseed_relic", "e2")
    assert response.status_code == 404, (
        "activating a corrupt record answered %d; the measured contract is 404"
        % (response.status_code,)
    )
    # A JSON envelope, not a plain-text crash: a client can parse a reason.
    assert response.headers.get("content-type", "").startswith("application/json")
    assert response.json()["detail"] == "Player does not own artifact"
    # And the refusal is inert: no activation, and no receipt burned, so a
    # retry under a fresh key is still a plain 404 rather than a silent hit.
    saved = players["p1"]["artifacts"]
    assert saved["receipts"] == {}
    assert activate(rig, "p1", "emberlotus_seal", "e3").status_code == 200


# --- 9b. The active list, the composed numbers, and the detached reply -------

def test_each_entry_in_active_carries_the_catalogue_keys_plus_activated(rig):
    """``active`` is the catalogue definition plus one flag.

    A client reads ``activated`` off every entry here, and reads
    ``bonuses`` off it too. Both names are part of the wire. An inactive
    view must answer an EMPTY list rather than a list of unflagged
    definitions, or a client cannot tell which are on.
    """
    players = rig["players"]
    seed(players, "p1")
    assert rig["client"].get("/artifacts/p1").json()["active"] == []

    body = activate(rig, "p1", "emberlotus_seal", "k1").json()
    entries = body["artifacts"]["active"]
    assert len(entries) == 1
    assert set(entries[0]) == CATALOG_KEYS | {"activated"}
    assert entries[0]["activated"] is True
    assert entries[0]["id"] == "emberlotus_seal"
    assert entries[0]["bonuses"] == A.ARTIFACT_CATALOG["emberlotus_seal"]["bonuses"]


def test_two_active_artifacts_multiply_their_shared_stat(rig):
    """Multipliers COMPOSE, and only over the stats they both name.

    Two artifacts sharing one stat must multiply it, not overwrite it, and
    a stat only one of them names must keep the other's 1.0. Both halves
    are load-bearing: an overwrite passes the first assertion and fails the
    second, and a straight product passes both while being wrong for the
    stats the pair does not share.
    """
    players = rig["players"]
    seed(players, "p1")
    activate(rig, "p1", "cloudseed_relic", "k1")   # hp_mult, mp_mult
    body = activate(rig, "p1", "tideglass_compass", "k2").json()  # spd_mult, mp_mult

    assert body["artifacts"]["active_count"] == 2
    landed = body["artifacts"]["multipliers"]
    expected_mp = (float(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"]["mp_mult"])
                   * float(A.ARTIFACT_CATALOG["tideglass_compass"]["bonuses"]["mp_mult"]))
    assert landed["mp_mult"] == pytest.approx(expected_mp)
    # Named by exactly one of the two: the other must not disturb it.
    assert landed["hp_mult"] == pytest.approx(
        float(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"]["hp_mult"]))
    assert landed["spd_mult"] == pytest.approx(
        float(A.ARTIFACT_CATALOG["tideglass_compass"]["bonuses"]["spd_mult"]))
    # Named by neither.
    assert landed["atk_mult"] == 1.0 and landed["def_mult"] == 1.0


def test_the_reply_is_a_copy_so_editing_it_cannot_reach_the_save(rig):
    """A caller cannot edit the player's state by editing what it was sent.

    ``state_view`` deep-copies the catalogue and the owned records. If it
    handed back live stored objects, anyone holding the view would change
    the server's row with no write at all.

    This calls ``state_view`` DIRECTLY on purpose. Over HTTP it would prove
    nothing: TestClient serialises to JSON and the caller parses fresh
    copies, so a mutated reply cannot reach the store even when the view
    leaks live objects. A control run confirmed that -- the HTTP version
    stayed green under a deliberate leak. Only a direct call can see it.
    """
    players = rig["players"]
    seed(players, "p1")
    activate(rig, "p1", "cloudseed_relic", "k1")
    player = players["p1"]

    body = A.state_view(player, "p1")
    body["owned"][0]["activated"] = False
    body["catalog"][0]["bonuses"] = {"atk_mult": 999.0}
    body["active"][0]["bonuses"] = {"atk_mult": 999.0}

    saved = player["artifacts"]["owned"]["cloudseed_relic"]
    assert saved["activated"] is True
    assert A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"] != {"atk_mult": 999.0}

    # And the next read still reports the truth.
    fresh = A.state_view(players["p1"], "p1")
    assert any(record["activated"] for record in fresh["owned"])
    assert fresh["catalog"][0]["bonuses"] == A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"]
    assert fresh["active"][0]["bonuses"] == A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"]


def test_the_activate_routes_catalogue_matches_the_get_routes_catalogue(rig):
    """Both routes serve the same catalogue; a client may read either."""
    players = rig["players"]
    seed(players, "p1")
    get_body = rig["client"].get("/artifacts/p1").json()
    post_body = activate(rig, "p1", "cloudseed_relic", "k1").json()
    assert post_body["artifacts"]["catalog"] == get_body["catalog"]


def test_an_artifact_stays_active_until_the_cap_pushes_it_off(rig):
    """Activation does not toggle: a second call is idempotent on state.

    Only the receipt is keyed, so a FRESH key on an already-active artifact
    must leave it active rather than switching it off. A client that
    re-sends the button must not silently lose the bonus.
    """
    players = rig["players"]
    seed(players, "p1")
    activate(rig, "p1", "emberlotus_seal", "first")
    second = activate(rig, "p1", "emberlotus_seal", "second").json()
    assert second["evicted"] == []
    assert second["artifacts"]["active_count"] == 1
    view = rig["client"].get("/artifacts/p1").json()
    assert [a["id"] for a in view["active"]] == ["emberlotus_seal"]


# --- 10. Mount shape ---------------------------------------------------------

def test_exactly_two_routes_are_registered(rig):
    """FastAPI adds HEAD to every GET; subtract it before counting."""
    mounted = sorted(
        (sorted(r.methods - {"HEAD"}), r.path)
        for r in rig["app"].routes
        if getattr(r, "methods", None) and r.path.startswith("/artifacts")
    )
    assert mounted == [(["GET"], "/artifacts/{player_id}"),
                       (["POST"], "/artifacts/activate")]


def test_the_store_may_be_handed_over_as_a_lambda_or_directly():
    """``resolve_store`` accepts either; both must serve the same contract."""
    players = _PlayerStore()
    players.persist("direct", {"id": "direct"})
    app = FastAPI()
    A.register_artifact_routes(app, players)  # not a lambda
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/artifacts/direct").status_code == 200


def test_the_store_may_be_handed_over_as_a_factory_returning_a_factory():
    """The double-indirection shape resolves one level at a time, not twice.

    ``main.py`` builds its store lazily behind a callable, and a test harness
    may wrap that in its own lambda. ``resolve_store`` calls, then calls again
    if the answer is STILL callable -- so the doubly-wrapped store is found,
    and a store that merely happens to be callable is not called twice.
    """
    players = _PlayerStore()
    players.persist("direct", {"id": "direct"})

    wrapped = A.resolve_store(lambda: (lambda: players))
    assert wrapped is players

    app = FastAPI()
    A.register_artifact_routes(app, lambda: (lambda: players))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/artifacts/direct").status_code == 200


# --- 10b. Verb routing, measured ---------------------------------------------

def test_the_wrong_verb_on_the_view_path_is_405_not_404(rig):
    """``POST /artifacts/p1`` answers 405. The refusal is about the VERB.

    The player exists, so a 404 here would send a client looking for a
    missing player. With a control on the GET arm, so a route answering 405
    to everything could not satisfy both halves.
    """
    seed(rig["players"], "p1")
    assert rig["client"].get("/artifacts/p1").status_code == 200
    assert rig["client"].post("/artifacts/p1", json={}).status_code == 405


def test_the_wrong_verb_on_the_activate_path_is_405(rig):
    """``PUT /artifacts/activate`` answers 405.

    This arm has no ambiguity: the path is fixed, so no player id can be
    read out of it and 405 is the only correct answer.
    """
    seed(rig["players"], "p1")
    response = rig["client"].put("/artifacts/activate", json={})
    assert response.status_code == 405


def test_get_on_activate_reads_activate_as_a_player_id_and_404s(rig):
    """MEASURED ORDERING FACT: the path route is registered FIRST.

    ``GET /artifacts/activate`` is NOT a 405. FastAPI matches in
    registration order and ``/artifacts/{player_id}`` is mounted before the
    literal ``/artifacts/activate``, so the request is routed to the VIEW
    handler with the player id spelled "activate", which no player holds --
    hence 404 "Player not found".

    Pinned because the number a client sees is not guessable: a client that
    probes the wrong verb here sees 404, which looks like a missing player
    rather than a missing verb. A later reorder would change it to 405 and
    this test would say so.
    """
    response = rig["client"].get("/artifacts/activate")
    assert response.status_code == 404
    assert response.json()["detail"] == "Player not found"


# --- 11. Save-repair shapes the migration claims to cover --------------------

def test_an_artifacts_block_that_is_not_an_object_is_rebuilt(rig):
    """A save whose whole artifacts block is a bare string is rebuilt, not 500.

    ``ensure_state`` rebuilds on anything that is not a dict carrying a dict
    ``owned``. Asserted on BOTH routes: the read rebuilds and persists, and
    the activate route reaches the same state without its own guard, so a
    caller whose save is corrupt in the blunt way still gets a working
    inventory rather than a crash.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block="corrupt")

    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    # The repair reached the save, not just the reply.
    assert isinstance(players["p1"]["artifacts"], dict)

    # The activate path, from the same corrupt starting shape.
    seed(players, "p2", artifacts_block=42)
    assert activate(rig, "p2", "cloudseed_relic", "k1").status_code == 200


@pytest.mark.parametrize("bad_receipts", [[], "junk", 7, None, [("k", {})]])
def test_a_receipts_ledger_that_is_not_an_object_is_repaired_not_500(rig, bad_receipts):
    """A corrupt ledger is replaced, and activation then works.

    WAS a measured 500, and this test pinned the gap on purpose so it could
    not be lost: ``ensure_state`` used ``setdefault`` for ``receipts``,
    which only fills an ABSENT key. A save holding a list, a string, a
    number or null therefore survived the migration, and the activate
    route's ``state["receipts"].get(key)`` raised ``AttributeError`` on it
    -- a plain-text 500 for a player whose own GET had just answered 200
    for the same row. ``ensure_state`` now replaces a non-dict ``receipts``
    the way it already replaced a non-dict ``cap``.

    The three assertions below are all load-bearing, and each one alone
    would pass on the broken code:

    * 200 instead of 500 is the repair. A test that only checked "not 500"
      would also accept a 404 that blamed the wrong thing.
    * the envelope is the FROZEN one, so the answer is a real activation
      and not some other success.
    * the key is now IN the saved ledger, and replaying it returns the
      same decision. A repair that handed back an empty ledger on every
      call would answer 200 forever and never once be idempotent, which is
      the whole point of the ledger.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={
        "version": 2, "cap": 3, "receipts": bad_receipts,
        "owned": {"cloudseed_relic": {"artifact_id": "cloudseed_relic",
                                      "activated": False, "bound_to": "p1",
                                      "charges": 0, "cooldown_until": 0}}})
    players.persist("p1", players["p1"])

    # The read arm was always fine, because it never touches the ledger.
    assert rig["client"].get("/artifacts/p1").status_code == 200

    response = activate(rig, "p1", "cloudseed_relic", "k1")
    assert response.status_code == 200, (
        "activating with a corrupt receipts ledger answered %d; the measured "
        "contract is a working 200" % (response.status_code,)
    )
    body = response.json()
    assert set(body) == ACTIVATE_KEYS
    assert body["artifact_id"] == "cloudseed_relic"
    assert body["artifacts"]["active_count"] == 1
    # A JSON envelope, not a plain-text crash: a client can parse a reason.
    assert response.headers.get("content-type", "").startswith("application/json")

    # The repair reached the SAVE, and the ledger is usable: the receipt was
    # written there and the same key replays as the same decision.
    saved = players["p1"]["artifacts"]
    assert isinstance(saved["receipts"], dict)
    assert "k1" in saved["receipts"]
    replay = activate(rig, "p1", "cloudseed_relic", "k1")
    assert replay.status_code == 200
    assert replay.json() == body


def test_a_receipt_missing_its_artifact_id_is_a_clean_409_not_a_crash(rig):
    """The blanket-catch removal, measured on the case it was aimed at.

    A receipt with no ``artifact_id`` key used to raise ``KeyError``, which
    the route's own handler reported as 404 "Player not found" for a player
    who demonstrably exists. Now the mismatch is read with ``.get``, so a
    legacy receipt is refused as the payload mismatch it IS -- 409, naming
    the reason -- and the player's activation state is untouched.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["receipts"]["legacy"] = {"ok": True, "idempotency_key": "legacy",
                                   "evicted": []}
    players.persist("p1", players["p1"])

    response = activate(rig, "p1", "cloudseed_relic", "legacy")
    assert response.status_code == 409, (
        "a legacy receipt answered %d; the measured contract is 409"
        % (response.status_code,)
    )
    assert response.json()["detail"] == "Idempotency key payload mismatch"
    # The refusal is inert: nothing was activated and the receipt survived.
    saved = players["p1"]["artifacts"]
    assert saved["owned"]["cloudseed_relic"]["activated"] is False
    assert "legacy" in saved["receipts"]


def test_charges_and_cooldown_travel_from_the_save_to_the_reply(rig):
    """The two state fields a client actually reads are echoed verbatim.

    ``OWNED_KEYS`` already pins that the names exist; this pins that a
    NON-default value survives the round trip. A reader that reset either to
    zero would keep the key set intact and pass the shape assertion, while a
    cooldown timer on the client silently reads 0 forever.
    """
    players = rig["players"]
    seed(players, "p1")
    state = A.ensure_state(players["p1"], "p1")
    state["owned"]["cloudseed_relic"]["charges"] = 4
    state["owned"]["cloudseed_relic"]["cooldown_until"] = 999
    players.persist("p1", players["p1"])

    record = owned_of(rig["client"].get("/artifacts/p1").json(), "cloudseed_relic")
    assert record["charges"] == 4
    assert record["cooldown_until"] == 999

    # And the activate route's embedded view carries the same numbers, so a
    # client that only reads the POST reply is not reading stale state.
    embedded = owned_of(activate(rig, "p1", "emberlotus_seal", "k1").json()["artifacts"],
                        "cloudseed_relic")
    assert embedded["charges"] == 4
    assert embedded["cooldown_until"] == 999

@pytest.mark.parametrize("bad_value", ["junk", 7, ["x", "y"], None])
def test_a_receipt_slot_holding_a_string_number_list_or_null_is_dropped_not_crashed(
        rig, bad_value):
    """The map-level guard is not enough on its own: a dict can hold a
    value of the wrong type, and the activate route reads whatever the key
    holds.

    ``ensure_state`` checked that ``receipts`` was a dict but never looked
    at what was INSIDE it. So a save whose ledger was a proper dict holding
    the string "junk" under one key passed that check, and
    ``activate_artifact`` then ran ``prior.get("artifact_id")`` on the
    string -- AttributeError, a plain-text 500 on every activate, for a
    player whose own GET had just answered 200 for the same row. The read
    route never touches the ledger, which is why this hid so well.

    The corrupt key is the one being ACTIVATED. That is the whole defect:
    the ledger is only read by key, so a bad value under a key nobody asks
    about cannot crash anything. Activating with a different key would
    answer 200 on the broken code and prove nothing.

    A second bad key is left un-activated, so "the bad entries are dropped"
    is checked on its own and not only as a side effect of the activation
    rewriting that key with a fresh receipt.

    Each assertion below is load-bearing:

    * 200, with the FROZEN envelope, is the repair. "not 500" alone would
      also accept a 404 blaming the wrong thing.
    * the stored receipt for the activated key is a dict carrying this
      activation -- the corrupt value did not survive as a receipt, and the
      action still happened once.
    * the un-activated bad key is GONE from the saved ledger, while a real
      sibling receipt SURVIVES. A repair that emptied the whole map would
      pass the drop assertion and silently destroy every legitimate replay
      receipt in the save.
    * the replay returns the same decision, so the ledger is still
      idempotent after the repair rather than answering 200 forever.
    """
    players = rig["players"]
    seed(players, "p1", artifacts_block={
        "version": 2, "cap": 3,
        "receipts": {
            "bad": bad_value,
            "bad-unused": "also-not-a-receipt",
            "good": {"ok": True, "artifact_id": "emberlotus_seal",
                     "idempotency_key": "good", "evicted": []},
        },
        "owned": {"cloudseed_relic": {"artifact_id": "cloudseed_relic",
                                      "activated": False, "bound_to": "p1",
                                      "charges": 0, "cooldown_until": 0}}})
    players.persist("p1", players["p1"])

    # The read arm was always fine: it never touches the ledger.
    assert rig["client"].get("/artifacts/p1").status_code == 200

    # The corrupt slot is the key being activated, which is the only way the
    # consumer can reach it.
    response = activate(rig, "p1", "cloudseed_relic", "bad")
    assert response.status_code == 200, (
        "activating with a %s under the receipt key answered %d; the measured "
        "contract is a working 200" % (type(bad_value).__name__,
                                       response.status_code)
    )
    body = response.json()
    assert set(body) == ACTIVATE_KEYS
    assert body["artifact_id"] == "cloudseed_relic"
    assert body["artifacts"]["active_count"] == 1
    # A JSON envelope, not a plain-text crash: a client can parse a reason.
    assert response.headers.get("content-type", "").startswith("application/json")

    saved = players["p1"]["artifacts"]["receipts"]
    assert isinstance(saved, dict)
    # The corrupt value is gone from under that key, replaced by the fresh
    # receipt for the activation that just happened.
    assert isinstance(saved["bad"], dict)
    assert saved["bad"]["artifact_id"] == "cloudseed_relic"
    # The bad key nobody activated is dropped too, and on its own.
    assert "bad-unused" not in saved
    # A real receipt is NOT collateral damage.
    assert saved["good"]["artifact_id"] == "emberlotus_seal"
    # And the ledger is still idempotent after the repair.
    replay = activate(rig, "p1", "cloudseed_relic", "bad")
    assert replay.status_code == 200
    assert replay.json() == body
# --- 8. Non-finite and unusable caps -----------------------------------------
#
# Added 2026-10-05 for the `_coerce_bonus` / `_coerce_cap` pair in
# `artifacts.py`. The helpers exist because a save file is data the client
# never validated; a bad number in it used to become a plain-text 500 on a
# route the player considers an ordinary read.
#
# MEASURED FIRST, and the measurement changed what is asserted. Two layers
# guard `cap`, and they do NOT both see the same inputs:
#
#   * `ensure_state` (the `cap` repair) runs first and keeps a value only
#     when it is a real `int`, not a `bool`, and >= 0. It is the layer that
#     catches infinity, not-a-number, empty and a boolean.
#   * `_coerce_cap` runs second, in `_enforce_cap` on the activate path. By
#     then `ensure_state` has already replaced the four shapes above, so the
#     only value that actually REACHES it is a huge integer -- which is
#     finite Python, so the first layer keeps it.
#
# So a test that only posts `inf` proves `ensure_state` and leaves
# `_coerce_cap` unexecuted. That is the trap this block is written against,
# and it is why the huge integer is a first-class case here and not an
# afterthought.
#
# Every case is injected through the STORE, which is the real attack path for
# a corrupt save -- a request body cannot carry these values at all.
#
# `json.dumps` ACCEPTS `inf`/`nan` by default, so a reply containing one
# parses fine inside a test and still breaks a strict client. The assertions
# check the raw text for the bare `Infinity`/`NaN` tokens rather than
# trusting `response.json()`.

# The four shapes the FIRST layer (`ensure_state`) replaces outright.
_BAD_CAPS = [
    pytest.param(float("inf"), id="infinity"),
    pytest.param(float("nan"), id="not-a-number"),
    pytest.param(None, id="empty"),
    pytest.param(True, id="boolean"),
]
# The one shape that survives the first layer and reaches `_coerce_cap`.
HUGE_CAP = 10 ** 401


def _strict_json(response):
    """Parse a reply, refusing the bare ``Infinity``/``NaN`` tokens.

    Plain ``json.loads`` ACCEPTS both tokens by default, so ``.json()`` would
    happily hand back a poisoned number and a test written on it would prove
    nothing. ``parse_constant`` fires only on the BARE token, so a catalogue
    bonus echoing the TEXT ``"NaN"`` -- a perfectly legal JSON string --
    still parses, while a number that leaked through the guard does not.
    """
    return json.loads(response.text, parse_constant=_reject_constant)


def _reject_constant(token):
    raise AssertionError(f"reply carries the bare JSON token {token!r}")


@pytest.mark.parametrize("bad", _BAD_CAPS)
def test_a_non_finite_or_unusable_cap_answers_valid_json_and_the_default(
        rig, monkeypatch, bad):
    """A corrupt `cap` must not crash the read route and must not survive it.

    Two non-finite floats, an empty value, and a boolean (``int(True)`` is a
    silent 1, which would mean "evict everything already on"). Each must give
    a parseable reply free of the bare ``Infinity``/``NaN`` tokens, reporting
    ``DEFAULT_ARTIFACT_CAP``.

    The huge integer is deliberately NOT in this list -- it is finite, so it
    is a legal cap and the read echoes it. See
    ``test_a_huge_cap_is_legal_so_the_read_echoes_it_and_the_eviction_does_not``
    below, which pins that divergence instead of asserting it away.
    """
    seed(rig["players"], "p1", artifacts_block=A.default_artifact_state())
    monkeypatch.setitem(rig["players"]["p1"]["artifacts"], "cap", bad)

    response = rig["client"].get("/artifacts/p1")
    assert response.status_code == 200, (bad, response.text[:120])
    # Strict-encoder check, not `response.json()`: Starlette renders with
    # `allow_nan=False`, so a non-finite number reaching the reply is a 500
    # here, and the token check keeps that true even if the encoder is ever
    # loosened.
    assert _strict_json(response)["cap"] == A.DEFAULT_ARTIFACT_CAP, bad


@pytest.mark.parametrize("bad", _BAD_CAPS)
def test_a_non_finite_or_unusable_cap_never_crashes_the_activate_route(
        rig, monkeypatch, bad):
    """The activate route is where the cap is ENFORCED, so it is where a bad
    one bites hardest. Every activation must answer 200 and hold the line.

    ``_enforce_cap`` is the only caller of ``_coerce_cap``, so a guard proven
    on the read route alone would run unexecuted here.
    """
    seed(rig["players"], "p1", artifacts_block=A.default_artifact_state())
    monkeypatch.setitem(rig["players"]["p1"]["artifacts"], "cap", bad)

    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        response = activate(rig, "p1", artifact_id, f"key-{index}")
        assert response.status_code == 200, (bad, response.text[:120])
        _strict_json(response)

    body = rig["client"].get("/artifacts/p1").json()
    assert body["cap"] == A.DEFAULT_ARTIFACT_CAP, bad
    # The cap held: every catalogue entry stayed on and none were lost.
    assert body["active_count"] == len(A.ARTIFACT_CATALOG), bad


def test_a_huge_cap_is_legal_so_the_read_echoes_it_and_the_eviction_does_not(
        rig, monkeypatch):
    """MEASURED, and pinned rather than asserted correct. Read before "fixing".

    The 401-digit integer is NOT non-finite. It is an ``int``, it is
    ``>= 0``, so ``ensure_state`` keeps it and the read echoes the number
    back. ``_coerce_cap`` still returns ``DEFAULT_ARTIFACT_CAP`` for it,
    because ``math.isfinite`` raises ``OverflowError`` on the conversion --
    a guard against the CRASH, not a verdict that the number is bad.

    So one cap has two answers, and both are asserted here rather than only
    the convenient one:

    * the reply reports the huge number (a legal value, faithfully echoed);
    * the eviction loop behaves as if the cap were the default, which is why
      every activation still succeeds and nothing is lost.

    This is the only case in the block that actually executes
    ``_coerce_cap`` on the route, so it also asserts the split directly:
    ``ensure_state`` keeps the value, ``_coerce_cap`` does not.
    """
    huge = HUGE_CAP
    # A real int, not a bool, not negative -> the FIRST layer keeps it.
    assert A.ensure_state({"id": "p1", "artifacts": {"owned": {},
                                                     "cap": huge}})["cap"] == huge
    # The SECOND layer is what turns it into a usable count.
    assert A._coerce_cap(huge, A.DEFAULT_ARTIFACT_CAP) == A.DEFAULT_ARTIFACT_CAP

    seed(rig["players"], "p1", artifacts_block=A.default_artifact_state())
    monkeypatch.setitem(rig["players"]["p1"]["artifacts"], "cap", huge)
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        response = activate(rig, "p1", artifact_id, f"h{index}")
        assert response.status_code == 200, response.text[:120]
        _strict_json(response)

    body = rig["client"].get("/artifacts/p1").json()
    # Enforcement used the default cap: nothing was evicted, nothing was lost.
    assert body["active_count"] == len(A.ARTIFACT_CATALOG)
    # The published value is still the honest one.
    assert body["cap"] == huge


def test_a_non_finite_catalogue_bonus_never_reaches_a_multiplier(rig, monkeypatch):
    """The other half of the pair: a bad bonus must not become a multiplier.

    MEASURED TRAP, found by running this file with the guard sabotaged: the
    first version of this test never switched anything on, and
    ``_derive`` only multiplies bonuses for an artifact that is BOTH in the
    catalogue and activated. So the guard never ran, and the test was green
    on code with the guard deleted -- a vacuous pass, not a proof.

    Activating first is therefore part of the test, not decoration. The real
    ``hp_mult`` is 1.1, so asserting 1.0 also proves the bonus was actually
    applied and then neutralised, rather than ignored because nothing was on.

    TEXT forms are used on purpose. A non-finite NUMBER in the catalogue is a
    separate, deliberately pinned gap -- ``state_view`` echoes the raw
    catalogue, which takes the read route to a 500 because Starlette refuses
    to render it. That gap is asserted in the sibling file
    ``test_artifact_contract.py``; repeating it here would double a known gap
    rather than pin this guard.
    """
    pristine = A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"]["hp_mult"]
    assert pristine != 1.0, (
        "the fixture bonus is already neutral -- this test would pass for "
        "the wrong reason if the catalogue is rebalanced")

    # The unmodified bonus really does reach the multiplier. Asserted once,
    # before any patching, so a guard that silently ignored bonuses entirely
    # could not pass by making every value look neutral.
    seed(rig["players"], "clean")
    assert activate(rig, "clean", "cloudseed_relic", "k-clean").status_code == 200
    assert rig["client"].get("/artifacts/clean").json()["multipliers"][
        "hp_mult"] == pristine

    for bad in ("inf", "-inf", "nan", "NaN", "Infinity"):
        seed(rig["players"], "p1")
        assert activate(rig, "p1", "cloudseed_relic", f"k-{bad}").status_code == 200
        assert rig["client"].get("/artifacts/p1").json()["active_count"] == 1
        monkeypatch.setitem(A.ARTIFACT_CATALOG["cloudseed_relic"]["bonuses"],
                            "hp_mult", bad)
        response = rig["client"].get("/artifacts/p1")
        assert response.status_code == 200, (bad, response.text[:120])
        # The reply must stay strictly parseable AND neutral. Without
        # ``_coerce_bonus`` the multiplier becomes a bare ``Infinity`` token
        # and a strict client cannot read the response at all.
        assert _strict_json(response)["multipliers"]["hp_mult"] == 1.0, bad


def test_ordinary_caps_are_untouched_by_the_guards(rig, monkeypatch):
    """The control: the guards must be invisible to ordinary values.

    A guard that replaced every value with the default would be green on all
    of the cases above and would have deleted the feature. This proves 0, 1,
    2, 3 and 99 each survive the read route unchanged.
    """
    for ordinary in (0, 1, 2, 3, 99):
        seed(rig["players"], f"p-{ordinary}",
             artifacts_block=A.default_artifact_state())
        monkeypatch.setitem(rig["players"][f"p-{ordinary}"]["artifacts"],
                            "cap", ordinary)
        response = rig["client"].get(f"/artifacts/p-{ordinary}")
        assert response.status_code == 200
        assert response.json()["cap"] == ordinary, ordinary


def test_an_ordinary_cap_of_one_still_evicts_and_is_not_raised_to_the_default(
        rig):
    """The sharper half of the control, and the one a clamp cannot fake.

    A read-only assertion would still pass on a guard that replaced every cap
    with 3, provided it did so only in the view. Driving real activations
    shows cap 1 is still a LIMIT: three catalogue entries are activated in
    turn and only the newest survives. Cap 99, for contrast, keeps all three.
    """
    seed(rig["players"], "tight", artifacts_block=A.default_artifact_state())
    rig["players"]["tight"]["artifacts"]["cap"] = 1
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        assert activate(rig, "tight", artifact_id, f"t{index}").status_code == 200
    tight = rig["client"].get("/artifacts/tight").json()
    assert tight["cap"] == 1
    assert tight["active_count"] == 1

    seed(rig["players"], "roomy", artifacts_block=A.default_artifact_state())
    rig["players"]["roomy"]["artifacts"]["cap"] = 99
    for index, artifact_id in enumerate(A.ARTIFACT_CATALOG):
        assert activate(rig, "roomy", artifact_id, f"r{index}").status_code == 200
    roomy = rig["client"].get("/artifacts/roomy").json()
    assert roomy["cap"] == 99
    assert roomy["active_count"] == len(A.ARTIFACT_CATALOG)
