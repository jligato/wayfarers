"""
Tests for the 13 /tower/spire/* ascent routes (server/tower_routes.py).

These run inside the normal suite. The two hand-written probes
(`_probe_tower_spire.py`, `_probe_tower_spire_controls.py`) prove the same
properties against a self-built app and against deliberately broken COPIES;
this file is the version that lives in CI.

COLLECTION NOTE, re-measured 2026-10-05: `server/tests` collects and runs
normally. No shim, no external plugin:

    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_DB .venv/Scripts/python.exe \\
        -m pytest server/tests/test_tower_spire.py -q

WHAT THIS NOTE USED TO SAY, AND WHY IT IS GONE
----------------------------------------------
Measured on 2026-10-01, this note claimed the whole suite was dark because
`server/ownership_guard.py` imported `get_body_field` from
`fastapi.dependencies.utils` while the venv shipped fastapi 0.141.1, which had
renamed that name to `_get_body_field`. It told the reader to install an
external shim plugin.

All of that is false now. `fastapi.dependencies.utils.get_body_field` exists,
`server/ownership_guard.py` imports cleanly, the installed fastapi is 0.133.1,
and this file runs green under plain `pytest`. The break it described was
repaired; the marker outlived it. Kept here as a record because a note that
says "the suite is dark" is worth correcting loudly rather than silently
deleting.

Measured with that shim on 2026-10-01: 68 tower tests passed before and after
this file's subject change, so this feature was additive.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tower_routes as tr


SPIRE_PATHS = [
    "/tower/spire/enter", "/tower/spire/floor-info",
    "/tower/spire/combat/start", "/tower/spire/combat/action",
    "/tower/spire/combat/end", "/tower/spire/settle",
    "/tower/spire/history", "/tower/spire/ladder", "/tower/spire/rewards",
    "/tower/spire/party/invite", "/tower/spire/party/accept",
    "/tower/spire/party/leave", "/tower/spire/replay",
]


class _PlayerStore:
    """The PlayerStore surface the routes actually use."""

    def __init__(self):
        self.rows = {}

    def persist(self, pid, player=None):
        if player is not None:
            self.rows[pid] = player
        return player

    def __contains__(self, pid):
        return pid in self.rows

    def __getitem__(self, pid):
        return self.rows[pid]

    def get(self, pid, default=None):
        return self.rows.get(pid, default)

    def items(self):
        return list(self.rows.items())

    def values(self):
        return list(self.rows.values())

    def __iter__(self):
        return iter(self.rows)


class _Wallet:
    """The REAL shipped in-memory wallet.

    A hand-written stand-in with an ``earn`` method is worse than useless here:
    ``settle_run`` calls ``wallet_dispatch.earn``, which dispatches to
    ``wallet_defense.earn`` -- a different function. A stub therefore never
    runs, and the money assertions pass or fail against nothing. The real store
    also carries the third idempotency layer, which a stub would hide."""
    CURRENCY = "spirit_stone"

    def __init__(self):
        from wallet_dispatch import InMemoryWalletStore
        self.store = InMemoryWalletStore()

    def get(self, pid):
        return self.store.get(pid)

    def balance(self, pid):
        w = self.store.get(pid)
        if w is None:
            return 0
        return int(getattr(w.balance, self.CURRENCY, 0) or 0)


@pytest.fixture
def rig(monkeypatch):
    """An app with the routes mounted, the guard installed, and a clean store."""
    import auth
    import ownership_guard as og

    players = _PlayerStore()
    wallet = _Wallet()
    app = FastAPI()
    # The RAW store goes to the routes, not the wrapper: `wallet_dispatch.earn`
    # calls `store.transaction()` on whatever it is handed, and a wrapper that
    # only forwards `get` makes the money route 500.
    tr.register_tower_spire_routes(app, players, lambda: wallet.store)
    guarded = og.install_ownership_guard(app)
    client = TestClient(app, raise_server_exceptions=False)

    def seed(name, atk=250000, hp=2500000):
        pid = f"t_{name}"
        players.persist(pid, {"id": pid, "name": name, "atk": atk, "hp": hp,
                              "def_": 300, "cultivation_realm": "core",
                              "cultivation_stage": "late"})
        return pid

    return {"client": client, "players": players, "wallet": wallet,
            "guarded": guarded, "app": app,
            "hdr": lambda pid: {"Authorization": f"Bearer {auth.issue_session(pid)}"},
            "seed": seed}


def play_one_floor(rig, pid):
    """Enter, start a fight, drive it to a decision, close it."""
    c, h = rig["client"], rig["hdr"](pid)
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    for _ in range(200):
        r = c.post("/tower/spire/combat/action", headers=h, json={"action": "strike"})
        if r.status_code != 200 or r.json().get("resolved"):
            break
    return c.post("/tower/spire/combat/end", headers=h)


# ------------------------------------------------------------------ the census


def test_all_thirteen_routes_are_registered(rig):
    table = {(m, getattr(r, "path", "")) for r in rig["app"].routes
             for m in (getattr(r, "methods", []) or [])
             if m not in ("HEAD", "OPTIONS")}
    missing = [p for p in SPIRE_PATHS
               if not any(path == p for _, path in table)]
    assert not missing, f"unregistered: {missing}"
    served = [p for p in SPIRE_PATHS if p.startswith("/tower/spire")]
    assert len(served) == 13


def test_the_prefix_does_not_collide_with_the_older_ladders(rig):
    """`/tower/enter` and `/tower/info` are the LADDER's. A fourth shape that
    reused either name would be a route that exists in source and is dead at
    runtime, because FastAPI answers the first match."""
    paths = [getattr(r, "path", "") for r in rig["app"].routes]
    assert "/tower/enter" not in paths
    assert "/tower/info" not in paths
    assert not any(p.startswith("/tower/run/") for p in paths)


def test_every_route_carries_an_auth_dependency(rig):
    import ownership_guard as og
    open_paths = [getattr(r, "path", "") for r in rig["app"].routes
                  if getattr(r, "path", "").startswith("/tower/spire")
                  and not og.has_auth_dependency(r.dependant)]
    assert not open_paths, f"no auth dependency: {open_paths}"


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/tower/spire/enter", None),
    ("GET", "/tower/spire/floor-info", None),
    ("POST", "/tower/spire/combat/start", None),
    ("POST", "/tower/spire/combat/action", {"action": "strike"}),
    ("POST", "/tower/spire/combat/end", None),
    ("POST", "/tower/spire/settle", {"outcome": "death"}),
    ("GET", "/tower/spire/history", None),
    ("GET", "/tower/spire/ladder", None),
    ("GET", "/tower/spire/rewards", None),
    ("POST", "/tower/spire/party/invite", {"invitee_id": "t_someone"}),
    ("POST", "/tower/spire/party/accept", None),
    ("POST", "/tower/spire/party/leave", None),
    ("GET", "/tower/spire/replay?run_id=x", None),
])
def test_anonymous_is_refused_on_every_route(rig, method, path, body):
    r = rig["client"].request(method, path, json=body)
    assert r.status_code == 401, f"{path} answered {r.status_code} anonymously"


# ------------------------------------------------------------------ the loop


def test_a_run_is_played_and_closed(rig):
    pid = rig["seed"]("runner")
    r = play_one_floor(rig, pid)
    assert r.status_code == 200
    assert r.json()["won"] is True
    assert r.json()["depth"] == 1
    assert r.json()["current_floor"] == 2


def test_a_second_run_is_refused_while_one_is_open(rig):
    pid = rig["seed"]("double")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    r = rig["client"].post("/tower/spire/enter", headers=h)
    assert r.status_code == 400


def test_combat_end_refuses_a_fight_nobody_has_won(rig):
    """Otherwise "end early, bank a floor" is free depth."""
    pid = rig["seed"]("early")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    rig["client"].post("/tower/spire/combat/start", headers=h)
    r = rig["client"].post("/tower/spire/combat/end", headers=h)
    assert r.status_code == 400


def test_combat_start_does_not_hand_back_the_seed(rig):
    """Handing the seed over lets a caller pre-compute the fight, which is the
    input the tower design note recommends removing."""
    pid = rig["seed"]("nosteal")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    r = rig["client"].post("/tower/spire/combat/start", headers=h)
    assert r.status_code == 200
    assert "seed" not in r.text


def test_an_unknown_stance_is_refused(rig):
    pid = rig["seed"]("stance")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    rig["client"].post("/tower/spire/combat/start", headers=h)
    r = rig["client"].post("/tower/spire/combat/action", headers=h,
                           json={"action": "critique", "damage": 10 ** 9})
    assert r.status_code == 400


def test_two_fights_cannot_be_open_at_once(rig):
    pid = rig["seed"]("onefight")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    assert rig["client"].post("/tower/spire/combat/start", headers=h).status_code == 200
    assert rig["client"].post("/tower/spire/combat/start", headers=h).status_code == 400


# ------------------------------------------------------------------ the money


def test_settle_refuses_a_missing_idempotency_key(rig):
    pid = rig["seed"]("nokey")
    play_one_floor(rig, pid)
    r = rig["client"].post("/tower/spire/settle", headers=rig["hdr"](pid),
                           json={"outcome": "death"})
    assert r.status_code == 400


def test_settle_pays_once_across_a_retry_with_a_brand_new_key(rig):
    """The header is the caller's, so it catches a SAME-key retry only. The
    server-side paid set is what catches the fresh-key one."""
    pid = rig["seed"]("once")
    play_one_floor(rig, pid)
    c, h = rig["client"], rig["hdr"](pid)
    first = c.post("/tower/spire/settle",
                   headers={**h, "Idempotency-Key": "k1"}, json={"outcome": "death"})
    assert first.status_code == 200
    paid = first.json()["summary"]["reward"]["granted"]["stone"]
    assert paid > 0
    assert rig["wallet"].balance(pid) == paid

    second = c.post("/tower/spire/settle",
                    headers={**h, "Idempotency-Key": "A-COMPLETELY-NEW-KEY"},
                    json={"outcome": "death"})
    assert second.status_code == 200
    body = second.json()["summary"]
    assert body["already_paid"] is True
    assert body["reward"]["granted"]["stone"] == 0
    assert rig["wallet"].balance(pid) == paid, "the wallet moved on the retry"


def test_settle_pays_the_re_simulated_depth_not_the_claimed_one(rig):
    """The load-bearing guard. A caller who writes a depth into their own run
    record must be paid the depth the server can reproduce from its own
    seeds -- not the number they wrote."""
    pid = rig["seed"]("forger")
    play_one_floor(rig, pid)
    row = rig["players"][pid]
    row["tower_spire"]["active_run"]["depth"] = 90
    rig["players"].persist(pid, row)

    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "forge"},
                           json={"outcome": "death"})
    assert r.status_code == 200
    summary = r.json()["summary"]
    proof = summary["reward"]["proof"]
    assert proof["claimed_depth"] == 90
    assert proof["verified_depth"] == 1
    assert proof["mismatched"], "a forged depth must be reported, not silent"
    assert summary["floors_cleared"] == 1


def test_settle_reports_zero_on_a_lost_response_retry(rig):
    """The reply must say what THIS call granted. Replaying the stored summary
    verbatim reports the first call's payout while granting nothing -- the
    'reply says granted, wallet says zero' defect."""
    pid = rig["seed"]("lost")
    play_one_floor(rig, pid)
    c, h = rig["client"], rig["hdr"](pid)
    c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "a"},
           json={"outcome": "death"})
    retry = c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "b"},
                   json={"outcome": "death"})
    body = retry.json()["summary"]
    assert body["replayed"] is True
    assert body["reward"]["granted"]["stone"] == 0


def test_a_run_that_cleared_nothing_settles_for_nothing(rig):
    pid = rig["seed"]("nothing")
    c, h = rig["client"], rig["hdr"](pid)
    c.post("/tower/spire/enter", headers=h)
    r = c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "n"},
               json={"outcome": "death"})
    assert r.status_code == 400
    assert rig["wallet"].balance(pid) == 0


# ------------------------------------------------------------------ the party


def test_a_player_in_no_party_cannot_invite(rig):
    pid, other = rig["seed"]("noparty"), rig["seed"]("target")
    r = rig["client"].post("/tower/spire/party/invite", headers=rig["hdr"](pid),
                           json={"invitee_id": other})
    assert r.status_code == 400


def test_invite_then_accept_then_leave(rig):
    from dungeons import get_dungeon_manager
    leader, guest = rig["seed"]("leader"), rig["seed"]("guest")
    get_dungeon_manager().create_party(leader)
    c = rig["client"]
    assert c.post("/tower/spire/party/invite", headers=rig["hdr"](leader),
                  json={"invitee_id": guest}).status_code == 200
    assert c.post("/tower/spire/party/accept",
                  headers=rig["hdr"](guest)).status_code == 200
    assert guest in get_dungeon_manager().get_player_party(leader).members
    assert c.post("/tower/spire/party/leave", headers=rig["hdr"](guest)).status_code == 200
    assert guest not in get_dungeon_manager().get_player_party(leader).members


def test_a_stranger_cannot_accept_someone_elses_invite(rig):
    from dungeons import get_dungeon_manager
    leader, guest, third = (rig["seed"]("l2"), rig["seed"]("g2"), rig["seed"]("x2"))
    get_dungeon_manager().create_party(leader)
    c = rig["client"]
    c.post("/tower/spire/party/invite", headers=rig["hdr"](leader),
           json={"invitee_id": guest})
    r = c.post("/tower/spire/party/accept", headers=rig["hdr"](third))
    assert r.status_code == 400


# ------------------------------------------------------------------ the reads


def test_replay_is_scoped_to_the_callers_own_runs(rig):
    victim, stranger = rig["seed"]("v"), rig["seed"]("s")
    run_id = rig["client"].post("/tower/spire/enter",
                                headers=rig["hdr"](victim)).json()["run_id"]
    assert rig["client"].get(f"/tower/spire/replay?run_id={run_id}",
                             headers=rig["hdr"](stranger)).status_code == 404
    assert rig["client"].get(f"/tower/spire/replay?run_id={run_id}",
                             headers=rig["hdr"](victim)).status_code == 200


def test_the_read_routes_answer(rig):
    pid = rig["seed"]("reader")
    for path in ("/tower/spire/history", "/tower/spire/ladder",
                 "/tower/spire/rewards", "/tower/spire/floor-info"):
        assert rig["client"].get(path, headers=rig["hdr"](pid)).status_code == 200, path


# ------------------------------------------------------------------ the models


def test_no_request_model_accepts_a_caller_honoured_number():
    """`player_id`, `depth`, `amount` and `seed` must not be expressible in a
    body. The payout is server-derived from the re-simulation, so a field that
    could state it would be a hole that no other guard covers."""
    import inspect
    offenders = []
    for name, obj in vars(tr).items():
        if (inspect.isclass(obj) and hasattr(obj, "model_fields")
                and obj.__module__ == tr.__name__):
            for field in obj.model_fields:
                if field in ("player_id", "pid", "depth", "amount", "stone", "seed"):
                    offenders.append(f"{name}.{field}")
    assert not offenders, offenders


def test_the_turn_loop_is_reproducible_from_its_seed():
    """The settle-time re-simulation is only exact if a fight is a pure
    function of (stat line, enemy, seed, actions)."""
    stats = {"hp": 1000, "atk": 100, "def_": 50, "realm": "mortal",
             "stage": "early", "element": "fire"}
    enemy = tr.cfg.enemy_stats_for_floor(7)
    a = tr._simulate_fight(stats, enemy, 12345, ["strike", "heavy", "guard"])
    b = tr._simulate_fight(stats, enemy, 12345, ["strike", "heavy", "guard"])
    assert a["winner"] == b["winner"]
    assert a["turns"] == b["turns"]
    assert a["enemy_hp_remaining"] == b["enemy_hp_remaining"]


def test_the_heavy_stance_respects_its_own_cooldown():
    """A caller cannot buy the 1.8x skill on consecutive turns by naming it."""
    stats = {"hp": 1000000, "atk": 100, "def_": 50, "realm": "mortal",
             "stage": "early", "element": "fire"}
    # A floor-1 enemy dies in two swings, so there is no third turn to read.
    # Give it enough health that the fight is still running when turn 3 lands.
    enemy = dict(tr.cfg.enemy_stats_for_floor(1), hp=10 ** 7)
    sim = tr._simulate_fight(stats, enemy, 99, ["heavy"] * 4)
    dealt = [e["damage_dealt"] for e in sim["log"][:4]]
    assert len(dealt) == 4, f"the fight ended early, nothing to compare: {dealt}"
    assert dealt[0] > dealt[1] > 0, f"cooldown not enforced: {dealt}"
    assert dealt[1] == dealt[2], f"cooldown decayed one turn early: {dealt}"
    assert dealt[3] > dealt[2], f"heavy never came back up: {dealt}"
