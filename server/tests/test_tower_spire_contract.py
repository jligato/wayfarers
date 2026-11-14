"""
Contract test for ``server/tower_routes.py`` - the ``/tower/spire/*`` ascent surface.

WHAT THIS IS
------------
A contract test. It pins what the thirteen ascent routes actually DO over HTTP,
in the shape a client panel is written against: which paths exist, which keys
each reply carries, which body fields are refused, and what the money path
reports. It is deliberately not a value snapshot - the reward table, the enemy
curve and the floor count all move, so every number here is derived from
``tower_config`` at call time and only RELATIONSHIPS are asserted.

It also pins, as live assertions, the defect this module still carries, and the
repair of one this module used to carry.

DEFECT 1 - THE PARTY STORE SPLIT -- FIXED 2026-10-03
    The module did ``from dungeons import get_dungeon_manager`` while
    ``dungeon_routes.py:20`` did ``from server.dungeons import ...``. Those were
    TWO module objects holding TWO ``DungeonManager`` singletons. A party made
    through the package-qualified name was invisible to the tower, so
    ``POST /dungeons/party/create`` answered 200 and the very next
    ``POST /tower/spire/party/invite`` answered 400 "You are not in a party".
    Three changes closed it, and all three were needed:

    1. ``tower_routes.py:142`` now reads ``from server.dungeons import ...``.
    2. ``dungeons`` is on the root ``conftest.py`` ``_SERVER_HELPERS`` list.
    3. That alias prefers the PACKAGED spelling when both are loaded. Bare-first
       preference rebound both names to whichever copy loaded first -- and
       ``tower_run`` loads the bare one before the routes do -- so it pointed the
       module table at an object the live handlers did not hold.

    (1) alone fixes the shipped launch. (2)+(3) stop the class of split from
    recurring inside the suite. The repaired identity is pinned positively by
    ``test_the_party_split_was_a_module_identity_split_and_is_now_repaired``,
    which also re-drives the flow that used to answer 400.

DEFECT 2 - REPLAY IS EMPTY FOR EVERY SETTLED RUN (still open)
    ``GET /tower/spire/replay`` reads floor records from ``active_run["floors"]``
    and otherwise only checks that the run id appears in ``history``. The
    history entry carries seven keys and not the per-floor records, so the seeds
    are gone the moment a run settles: the reply is ``found: true`` with
    ``floors: []``. Mid-run it works. A replay screen built on this route shows
    nothing for exactly the runs a player most wants to look at.

Defect 2 remains ``xfail(strict=True)`` on the assertion that SHOULD hold, so the
suite is green today and goes RED the moment the behaviour is fixed and the
marker is forgotten. It was checked with ``--runxfail`` to fail on the named
assertion, not on some earlier refusal.

HOW TO RUN
----------
``server/tests`` collects and runs in the normal suite, with no shim of any
kind. Re-measured 2026-10-05:

    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_DB .venv/Scripts/python.exe -m pytest \\
        server/tests/test_tower_spire_contract.py -q

WHAT USED TO BE SAID HERE, AND WHY IT IS GONE
----------------------------------------------
An earlier version of this note claimed ``server/tests`` did not collect on
this tree because ``server/ownership_guard.py`` imported ``get_body_field``
from ``fastapi.dependencies.utils`` and the venv shipped fastapi 0.141.1,
which renamed that name to ``_get_body_field``. It therefore told the reader to
run with an EXTERNAL shim plugin.

Every part of that is false on the tree as it stands now:

  * ``fastapi.dependencies.utils.get_body_field`` exists (checked by importing
    it), and ``server/ownership_guard.py`` imports cleanly.
  * The installed fastapi is 0.133.1, not 0.141.1.
  * ``server/tests`` collects. This file alone runs 66 passed, 1 xfailed with
    plain ``pytest`` and no plugin.

A stale marker of that kind is not harmless documentation: it told a reader the
whole suite was dark when it was not, and it pointed them at a workaround for a
break that had already been repaired. If collection ever does break again, the
claim belongs in the run output, not frozen in a docstring.

Measurements taken on commit 02f1b7e; the collection claims re-measured
2026-10-05.
"""
import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tower_config as cfg
import tower_routes as tr


SPIRE_PATHS = [
    "/tower/spire/enter", "/tower/spire/floor-info",
    "/tower/spire/combat/start", "/tower/spire/combat/action",
    "/tower/spire/combat/end", "/tower/spire/settle",
    "/tower/spire/history", "/tower/spire/ladder", "/tower/spire/rewards",
    "/tower/spire/party/invite", "/tower/spire/party/accept",
    "/tower/spire/party/leave", "/tower/spire/replay",
]

ENTER_KEYS = {"ok", "run_id", "current_floor", "floors_cleared",
              "total_floors", "best_ever", "enemy"}
FLOOR_INFO_KEYS = {"ok", "run_active", "current_floor", "total_floors", "label",
                   "is_boss", "enemy", "fight_open", "floors_cleared",
                   "best_ever", "actions"}
COMBAT_START_KEYS = {"ok", "floor", "label", "enemy", "player_hp", "enemy_hp",
                     "turn", "resolved", "actions", "heavy_cooldown",
                     "current_floor"}
ACTION_KEYS = {"ok", "turn", "action", "damage_dealt", "crit", "damage_taken",
               "taken_crit", "player_hp", "enemy_hp", "resolved", "enemy",
               "depth"}
COMBAT_END_KEYS = {"ok", "floor", "won", "turns", "depth", "current_floor",
                   "enemy_hp_remaining", "player_hp_remaining", "run_active"}
SUMMARY_KEYS = {"run_id", "outcome", "floors_cleared", "best_ever", "paid",
                "already_paid", "reward", "week", "week_best_depth",
                "week_improved", "started_at", "ended_at",
                "claimed_floors_cleared", "qi_total"}


class _PlayerStore:
    """The ``PlayerStore`` surface the routes actually use.

    ``persist`` is not decoration: in memory mode ``store[pid]`` hands back a
    detached copy, so a write the route did not persist is a write nothing sees.
    """

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
    """The REAL shipped in-memory wallet, unwrapped.

    ``settle_run`` calls ``wallet_dispatch.earn``, which calls
    ``store.transaction()`` on whatever it is handed. A hand-written stand-in with
    an ``earn`` method never runs, so money assertions built on one pass or fail
    against nothing. The real store also carries the wallet's own idempotency
    layer, which a stub hides.
    """

    CURRENCY = cfg.TOWER_RUN_CURRENCY

    def __init__(self):
        from wallet_dispatch import InMemoryWalletStore
        self.store = InMemoryWalletStore()

    def balance(self, pid):
        w = self.store.get(pid)
        if w is None:
            return 0
        return int(getattr(w.balance, self.CURRENCY, 0) or 0)


@pytest.fixture
def rig():
    """Routes mounted, clean stores, real sessions, one app per test."""
    import auth

    players = _PlayerStore()
    wallet = _Wallet()
    app = FastAPI()
    # The RAW store object goes to the routes: wallet_dispatch calls
    # transaction() on it, and a wrapper that only forwards get() 500s settle.
    tr.register_tower_spire_routes(app, players, lambda: wallet.store)
    # raise_server_exceptions=False: one 500 would otherwise abort the test and
    # leave every later assertion unmeasured.
    client = TestClient(app, raise_server_exceptions=False)

    def seed(name, atk=250000, hp=2500000):
        pid = "c_" + str(name)
        players.persist(pid, {"id": pid, "name": str(name), "atk": atk,
                              "hp": hp, "def_": 300,
                              "cultivation_realm": "core",
                              "cultivation_stage": "late"})
        return pid

    def hdr(pid):
        return {"Authorization": "Bearer " + auth.issue_session(pid)}

    return {"client": client, "players": players, "wallet": wallet,
            "app": app, "seed": seed, "hdr": hdr}


def play_one_floor(rig, pid, action="strike"):
    """Enter, open a fight, drive it to a decision, close it."""
    c = rig["client"]
    h = rig["hdr"](pid)
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    for _ in range(cfg.TOWER_RUN_MAX_TURNS + 5):
        r = c.post("/tower/spire/combat/action", headers=h, json={"action": action})
        if r.status_code != 200 or r.json().get("resolved"):
            break
    return c.post("/tower/spire/combat/end", headers=h)


# ---------------------------------------------------------------- the census


def test_exactly_thirteen_ascent_routes_are_registered(rig):
    served = {(m, getattr(r, "path", "")) for r in rig["app"].routes
              for m in (getattr(r, "methods", []) or [])
              if m not in ("HEAD", "OPTIONS")
              and getattr(r, "path", "").startswith("/tower/spire")}
    missing = [p for p in SPIRE_PATHS
               if not any(path == p for _, path in served)]
    assert not missing, f"in the module docstring, absent from the app: {missing}"
    assert len({path for _, path in served}) == len(SPIRE_PATHS)


def test_the_prefix_does_not_collide_with_the_three_older_ladders(rig):
    """``/tower/enter`` and ``/tower/info`` already belong to tower_ladder, and
    FastAPI answers the FIRST match. A fourth shape reusing either name would be
    a route that exists in source and is dead at runtime."""
    paths = [getattr(r, "path", "") for r in rig["app"].routes]
    for borrowed in ("/tower/enter", "/tower/info", "/tower/claim"):
        assert borrowed not in paths, borrowed + " is the numbered ladder's"
    assert not any(p.startswith("/tower/run/") for p in paths)


def test_every_ascent_route_carries_an_auth_dependency(rig):
    """The shape guard cannot see these: it matches on a bare player_id/pid
    parameter, and ladder, rewards and all three party routes name no player.
    The hand-wired Depends(require_player) is the layer that holds for them."""
    import ownership_guard as og
    open_paths = sorted({
        getattr(r, "path", "") for r in rig["app"].routes
        if getattr(r, "path", "").startswith("/tower/spire")
        and not og.has_auth_dependency(r.dependant)})
    assert not open_paths, f"registered with no auth dependency: {open_paths}"


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
    ("POST", "/tower/spire/party/invite", {"invitee_id": "c_someone"}),
    ("POST", "/tower/spire/party/accept", None),
    ("POST", "/tower/spire/party/leave", None),
    ("GET", "/tower/spire/replay?run_id=x", None),
])
def test_anonymous_is_refused_on_every_route(rig, method, path, body):
    r = rig["client"].request(method, path, json=body)
    assert r.status_code == 401, f"{path} answered {r.status_code} anonymously"


# ------------------------------------------------------- the reply contract


def test_enter_reply_keys(rig):
    r = rig["client"].post("/tower/spire/enter", headers=rig["hdr"](rig["seed"]("enter")))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ENTER_KEYS
    assert body["ok"] is True
    assert isinstance(body["run_id"], str) and body["run_id"]
    assert body["current_floor"] == 1
    assert body["floors_cleared"] == 0
    assert body["total_floors"] == cfg.TOWER_RUN_TOTAL_FLOORS
    assert body["best_ever"] == 0
    assert body["enemy"]["floor"] == 1
    assert body["enemy"]["is_boss"] is False


def test_floor_info_reply_keys_with_and_without_a_run(rig):
    pid = rig["seed"]("floorinfo")
    h = rig["hdr"](pid)
    c = rig["client"]
    idle = c.get("/tower/spire/floor-info", headers=h)
    assert idle.status_code == 200
    j = idle.json()
    assert set(j) == FLOOR_INFO_KEYS
    # No run still reports floor 1 rather than erroring, so a panel can render
    # the tower before the player has entered.
    assert j["run_active"] is False
    assert j["fight_open"] is False
    assert j["floors_cleared"] == 0
    assert j["current_floor"] == 1
    assert j["actions"] == list(tr.ACTIONS)

    c.post("/tower/spire/enter", headers=h)
    active = c.get("/tower/spire/floor-info", headers=h).json()
    assert active["run_active"] is True
    assert active["fight_open"] is False

    c.post("/tower/spire/combat/start", headers=h)
    fighting = c.get("/tower/spire/floor-info", headers=h).json()
    assert fighting["fight_open"] is True


def test_combat_start_reply_keys_and_withholds_the_seed(rig):
    """The seed is what makes settle-time re-simulation possible, so returning
    it would hand the caller the input the tower design says to remove."""
    pid = rig["seed"]("start")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    r = rig["client"].post("/tower/spire/combat/start", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == COMBAT_START_KEYS
    assert body["turn"] == 0
    assert body["resolved"] is False
    assert body["actions"] == list(tr.ACTIONS)
    assert body["heavy_cooldown"] == tr.heavy_cooldown() == 2
    assert body["player_hp"] > 0 and body["enemy_hp"] > 0
    assert body["floor"] == body["current_floor"] == 1
    assert "seed" not in r.text, "the reply leaked the per-run seed"


def test_combat_action_reply_keys(rig):
    pid = rig["seed"]("act")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    r = c.post("/tower/spire/combat/action", headers=h, json={"action": "strike"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == ACTION_KEYS
    assert body["turn"] == 1
    assert body["action"] == "strike"
    assert body["damage_dealt"] >= 0
    assert isinstance(body["crit"], bool)
    assert isinstance(body["taken_crit"], bool)
    # A panel reads the enemy straight out of the turn reply.
    assert isinstance(body["enemy"], dict)


def test_combat_end_reply_keys_and_advances_one_floor(rig):
    r = play_one_floor(rig, rig["seed"]("end"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == COMBAT_END_KEYS
    assert body["won"] is True
    assert body["floor"] == 1
    assert body["depth"] == 1
    assert body["current_floor"] == 2
    assert body["turns"] >= 1
    assert body["run_active"] is True
    assert body["enemy_hp_remaining"] == 0


def test_history_ladder_and_rewards_reply_keys(rig):
    pid = rig["seed"]("reads")
    h = rig["hdr"](pid)
    c = rig["client"]

    hist = c.get("/tower/spire/history", headers=h)
    assert hist.status_code == 200
    assert set(hist.json()) == {"ok", "history", "best_ever", "run_active"}
    assert hist.json()["history"] == []
    assert hist.json()["run_active"] is False

    ladder = c.get("/tower/spire/ladder", headers=h)
    assert ladder.status_code == 200
    assert set(ladder.json()) == {"ok", "week", "ladder", "player_rank"}
    assert ladder.json()["week"], "the board must name the week it ranks"
    # 0 means "not on the board", which is distinct from rank 1.
    assert ladder.json()["player_rank"] == 0

    rewards = c.get("/tower/spire/rewards", headers=h)
    assert rewards.status_code == 200
    body = rewards.json()
    assert set(body) == {"ok", "currency", "current_depth", "runs_today",
                         "daily_free_runs", "repeat_multiplier", "total_floors",
                         "at_current_depth", "upcoming", "ladder_prestige_every"}
    assert body["currency"] == cfg.TOWER_RUN_CURRENCY
    assert body["total_floors"] == cfg.TOWER_RUN_TOTAL_FLOORS
    assert body["daily_free_runs"] == cfg.TOWER_RUN_DAILY_FREE_RUNS
    assert body["repeat_multiplier"] == cfg.TOWER_RUN_REPEAT_RUN_MULT
    assert body["ladder_prestige_every"] == cfg.TOWER_LADDER_PRESTIGE_EVERY
    assert set(body["at_current_depth"]) == {"stone", "qi", "repeat"}
    assert len(body["upcoming"]) == 5
    for step in body["upcoming"]:
        assert set(step) == {"floor", "label", "is_boss",
                             "run_reward_at_that_depth"}


def test_the_reward_table_is_richer_for_a_deeper_depth(rig):
    """Relationships between rows of the shipped table, not frozen figures. Any
    retune that made depth worthless would fail here."""
    one = cfg.rewards_for_run(1, runs_today=1)
    ten = cfg.rewards_for_run(10, runs_today=1)
    assert one["stone"] > 0
    assert ten["stone"] > one["stone"]
    assert one["qi"] > 0.0
    assert ten["qi"] > one["qi"]
    assert one["repeat"] is False and ten["repeat"] is False
    # Past the free allowance the same depth pays less.
    repeat = cfg.rewards_for_run(10, runs_today=cfg.TOWER_RUN_DAILY_FREE_RUNS + 1)
    assert repeat["repeat"] is True
    assert repeat["stone"] < ten["stone"]
    # A run that cleared nothing pays nothing: the board measures depth, not
    # attempts.
    assert cfg.rewards_for_run(0, runs_today=1)["stone"] == 0


def test_the_boss_flag_is_a_cadence_not_a_special_enemy(rig):
    """``is_boss`` is a label every TOWER_RUN_BOSS_EVERY floors. Difficulty is
    scaling only - the enemy object carries no boss-only field, so a panel must
    not expect one."""
    for floor in range(1, cfg.TOWER_RUN_TOTAL_FLOORS + 1):
        enemy = cfg.enemy_stats_for_floor(floor)
        assert set(enemy) == {"atk", "def_", "element", "floor", "hp",
                              "is_boss", "realm", "stage"}
        assert enemy["is_boss"] is (floor % cfg.TOWER_RUN_BOSS_EVERY == 0)
        assert enemy["floor"] == floor
    assert cfg.TOWER_RUN_BOSS_EVERY == 10
    assert cfg.TOWER_RUN_TOTAL_FLOORS == 100


def test_the_reply_carries_no_wall_clock_field(rig):
    """The cap is TOWER_RUN_MAX_TURNS turns, not seconds. A panel can show
    "turn N of M" and must NOT be handed a countdown to fake."""
    pid = rig["seed"]("clock")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    start = c.post("/tower/spire/combat/start", headers=h).json()
    action = c.post("/tower/spire/combat/action", headers=h,
                    json={"action": "strike"}).json()
    for reply in (start, action,
                  c.get("/tower/spire/floor-info", headers=h).json()):
        assert not ({"time_left", "seconds_left", "countdown", "expires_at",
                     "remaining_seconds"} & set(reply))
    assert "turn" in start and "turn" in action
    assert cfg.TOWER_RUN_MAX_TURNS == 100


def test_settle_reply_shape_including_the_bare_progress_float(rig):
    """``progress`` is a BARE FLOAT, not an object. The other three tower shapes
    return a progress object, so a panel copied from one of them calls .get() on
    this and crashes. That is the contract, so it is pinned here."""
    pid = rig["seed"]("settle")
    play_one_floor(rig, pid)
    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "s1"},
                           json={"outcome": "death"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "summary", "progress"}
    assert isinstance(body["progress"], float), "progress became an object"
    assert body["progress"] >= 0.0

    summary = body["summary"]
    assert SUMMARY_KEYS <= set(summary)
    assert summary["paid"] is True
    assert summary["already_paid"] is False
    assert summary["floors_cleared"] == summary["claimed_floors_cleared"]
    assert summary["week_best_depth"] == summary["floors_cleared"]
    assert summary["week_improved"] is True
    reward = summary["reward"]
    assert {"stone", "qi", "repeat", "granted", "proof"} <= set(reward)
    assert set(reward["proof"]) == {"claimed_depth", "verified_depth",
                                    "mismatched"}
    # proof lives at summary.reward.proof, NOT summary.proof.
    assert "proof" not in summary
    assert reward["proof"]["mismatched"] == []


def test_no_reply_carries_a_data_envelope(rig):
    """Every ascent reply is FLAT. On the client ``data`` is the transport
    envelope APIClient builds, and the server's keys land inside it. Pinning the
    absence stops that being filed as a bug a second time."""
    pid = rig["seed"]("flat")
    h = rig["hdr"](pid)
    c = rig["client"]
    replies = [
        c.post("/tower/spire/enter", headers=h).json(),
        c.get("/tower/spire/floor-info", headers=h).json(),
        c.get("/tower/spire/rewards", headers=h).json(),
        c.get("/tower/spire/history", headers=h).json(),
        c.get("/tower/spire/ladder", headers=h).json(),
    ]
    for reply in replies:
        assert "data" not in reply, "nested envelope: " + str(sorted(reply))


def test_replay_mid_run_returns_the_floor_it_recorded(rig):
    pid = rig["seed"]("replaymid")
    h = rig["hdr"](pid)
    c = rig["client"]
    run_id = c.post("/tower/spire/enter", headers=h).json()["run_id"]
    play_one_floor(rig, pid)
    r = c.get("/tower/spire/replay?run_id=" + run_id, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "run_id", "found", "started_at", "ended_at",
                         "floors", "total_floors"}
    assert body["found"] is True
    assert body["run_id"] == run_id
    assert body["total_floors"] == cfg.TOWER_RUN_TOTAL_FLOORS
    assert len(body["floors"]) == 1
    floor = body["floors"][0]
    assert set(floor) == {"floor", "label", "winner", "turns",
                          "player_hp_remaining", "enemy_hp_remaining", "log"}
    assert floor["winner"] == "player"
    assert floor["log"], "the replay must carry the turn log, not just a summary"


def test_replay_is_scoped_to_the_callers_own_runs(rig):
    victim = rig["seed"]("victim")
    stranger = rig["seed"]("stranger")
    run_id = rig["client"].post("/tower/spire/enter",
                                headers=rig["hdr"](victim)).json()["run_id"]
    # 404, not 403: the row can only hold this caller's runs, so "not yours" and
    # "not there" are the same answer by construction and the route is not an
    # oracle for guessing other run ids.
    assert rig["client"].get("/tower/spire/replay?run_id=" + run_id,
                             headers=rig["hdr"](stranger)).status_code == 404
    assert rig["client"].get("/tower/spire/replay?run_id=nosuch",
                             headers=rig["hdr"](victim)).status_code == 404


# ------------------------------------------------------- the state machine


def test_a_second_run_is_refused_while_one_is_open(rig):
    pid = rig["seed"]("double")
    h = rig["hdr"](pid)
    assert rig["client"].post("/tower/spire/enter", headers=h).status_code == 200
    assert rig["client"].post("/tower/spire/enter", headers=h).status_code == 400


def test_combat_start_refuses_a_second_open_fight(rig):
    pid = rig["seed"]("twofight")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    assert rig["client"].post("/tower/spire/combat/start",
                              headers=h).status_code == 200
    assert rig["client"].post("/tower/spire/combat/start",
                              headers=h).status_code == 400


def test_combat_end_refuses_a_fight_nobody_has_finished(rig):
    """Otherwise 'end early, bank a floor' is free depth."""
    pid = rig["seed"]("early")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    rig["client"].post("/tower/spire/combat/start", headers=h)
    assert rig["client"].post("/tower/spire/combat/end",
                              headers=h).status_code == 400


def test_combat_routes_refuse_with_no_run_open(rig):
    pid = rig["seed"]("norun")
    h = rig["hdr"](pid)
    c = rig["client"]
    assert c.post("/tower/spire/combat/start", headers=h).status_code == 404
    assert c.post("/tower/spire/combat/end", headers=h).status_code == 404
    assert c.post("/tower/spire/combat/action", headers=h,
                  json={"action": "strike"}).status_code == 404


@pytest.mark.parametrize("stance", ["critique", "", "Strike", "strike ", "123"])
def test_an_unknown_stance_is_refused_and_never_wins(rig, stance):
    """``action`` is a NAME. There is no field through which a caller can state a
    damage figure, and an unknown name is a 400, not a silent default."""
    pid = rig["seed"]("stance" + str(abs(hash(stance)) % 9999))
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    before = c.get("/tower/spire/floor-info", headers=h).json()
    r = c.post("/tower/spire/combat/action", headers=h,
               json={"action": stance, "damage": 10 ** 9})
    assert r.status_code == 400, stance + " was accepted"
    after = c.get("/tower/spire/floor-info", headers=h).json()
    assert after["floors_cleared"] == before["floors_cleared"] == 0


def test_the_turn_cap_ends_a_fight_as_a_loss_not_a_hang(rig):
    """Reaching the cap resolves the fight so combat/end will take it. A cap
    that left the fight open would deadlock the run."""
    state = tr.normalize_spire_state(None)
    run = tr.start_run(state)
    # Neither side may die before the cap, so the cap is the only thing that can
    # end this fight: minimum damage is 1 a turn, and both sides carry far more
    # health than 100 turns of it. A player at the default 1000 HP dies around
    # turn 10, which tests death, not the cap.
    player = {"id": "c_weak", "atk": 1, "hp": 100000, "def_": 10 ** 9,
              "cultivation_realm": "mortal", "cultivation_stage": "early",
              "path": "fire"}
    fight = tr.open_fight(run, player)
    fight["stat"] = tr.player_stat_line(player)
    fight["hp"] = {"player_hp": 100000, "enemy_hp": fight["enemy"]["hp"]}
    for _ in range(cfg.TOWER_RUN_MAX_TURNS + 5):
        try:
            tr.take_action(run, "strike")
        except tr.RunRuleError:
            break
        if run["fight"].get("resolved"):
            break
    assert run["fight"]["resolved"] is True
    assert run["fight"]["turn"] == cfg.TOWER_RUN_MAX_TURNS
    closed = tr.close_fight(run)
    assert closed["won"] is False, "running out of turns must be a loss"
    assert closed["turns"] == cfg.TOWER_RUN_MAX_TURNS
    assert run["depth"] == 0, "an unfinished fight must not bank a floor"


def test_an_unknown_player_is_404_not_500(rig):
    import auth
    token = auth.issue_session("c_ghost")
    for method, path in (("GET", "/tower/spire/floor-info"),
                         ("GET", "/tower/spire/history"),
                         ("POST", "/tower/spire/enter")):
        r = rig["client"].request(method, path,
                                  headers={"Authorization": "Bearer " + token})
        assert r.status_code == 404, path + " answered " + str(r.status_code)


# --------------------------------------------------------- the money path


def test_settle_refuses_a_missing_idempotency_key(rig):
    """The header is the CALLER's. The server will not mint one, because a
    minted key is a formality, not a guard."""
    pid = rig["seed"]("nokey")
    play_one_floor(rig, pid)
    r = rig["client"].post("/tower/spire/settle", headers=rig["hdr"](pid),
                           json={"outcome": "death"})
    assert r.status_code == 400
    assert rig["wallet"].balance(pid) == 0


def test_settle_refuses_a_run_that_cleared_nothing(rig):
    pid = rig["seed"]("nothing")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    r = rig["client"].post("/tower/spire/settle",
                           headers={**h, "Idempotency-Key": "z"},
                           json={"outcome": "death"})
    assert r.status_code == 400
    assert rig["wallet"].balance(pid) == 0


def test_settle_refuses_when_no_run_is_open(rig):
    pid = rig["seed"]("norunset")
    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "q"},
                           json={"outcome": "death"})
    assert r.status_code == 400


def test_settle_pays_the_verified_depth_into_the_real_wallet(rig):
    pid = rig["seed"]("pay")
    play_one_floor(rig, pid)
    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "p1"},
                           json={"outcome": "death"})
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["reward"]["proof"]["verified_depth"] == 1
    granted = summary["reward"]["granted"]
    # The reply and the WALLET must agree on a first payment. Reporting the
    # configured amount instead of the wallet's result is the "reply says
    # granted, wallet says zero" defect.
    assert granted["stone"] == summary["reward"]["stone"] > 0
    assert rig["wallet"].balance(pid) == granted["stone"]
    assert granted["qi"] == summary["reward"]["qi"]
    assert summary["qi_total"] == granted["qi"]


def test_settle_pays_once_across_a_retry_with_a_brand_new_key(rig):
    """The header catches a SAME-key retry only. The server-side paid set is what
    catches a caller who mints a fresh key per attempt."""
    pid = rig["seed"]("once")
    play_one_floor(rig, pid)
    c = rig["client"]
    h = rig["hdr"](pid)
    first = c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "k1"},
                   json={"outcome": "death"})
    paid = first.json()["summary"]["reward"]["granted"]["stone"]
    assert paid > 0
    assert rig["wallet"].balance(pid) == paid

    second = c.post("/tower/spire/settle",
                    headers={**h, "Idempotency-Key": "A-COMPLETELY-NEW-KEY"},
                    json={"outcome": "death"})
    assert second.status_code == 200
    summary = second.json()["summary"]
    assert summary["already_paid"] is True
    assert summary["reward"]["granted"]["stone"] == 0
    assert rig["wallet"].balance(pid) == paid, "the wallet moved on the retry"


def test_granted_and_stone_disagree_on_a_replay_and_granted_is_the_truth(rig):
    """Both fields sit in the same object and they do NOT agree after a replay:
    ``granted`` is zeroed while the sibling ``stone`` still reads the first
    payment. A panel must render ``granted``. House rule for every money route
    here, so it is pinned rather than left to be rediscovered."""
    pid = rig["seed"]("disagree")
    play_one_floor(rig, pid)
    c = rig["client"]
    h = rig["hdr"](pid)
    first = c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "d1"},
                   json={"outcome": "death"})
    first_paid = first.json()["summary"]["reward"]["granted"]["stone"]
    assert first_paid > 0
    retry = c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "d2"},
                   json={"outcome": "death"})
    reward = retry.json()["summary"]["reward"]
    assert reward["granted"]["stone"] == 0
    assert reward["stone"] == first_paid, "the sibling no longer contradicts"
    # The original is kept available for a summary screen.
    assert retry.json()["summary"]["original_reward"]["stone"] == first_paid
    assert retry.json()["summary"]["replayed"] is True
    assert rig["wallet"].balance(pid) == first_paid


def _reproducible_one_floor_run(run_id, player_row):
    """A run holding exactly ONE floor record the server can re-derive.

    Built with the shipped ``_simulate_fight`` rather than hand-written numbers,
    so the record genuinely reproduces. A record-less run cannot distinguish a
    paid-set refusal from "nothing to pay", which is why every arm below needs
    this.
    """
    stats = tr.player_stat_line(player_row)
    enemy = cfg.enemy_stats_for_floor(1)
    seed_used = 4242
    sim = tr._simulate_fight(stats, enemy, seed_used, ["strike"] * 30)
    if sim["winner"] != "player":
        for candidate in range(1, 400):
            trial = tr._simulate_fight(stats, enemy, candidate, ["strike"] * 30)
            if trial["winner"] == "player":
                sim, seed_used = trial, candidate
                break
    assert sim["winner"] == "player", "no winning seed found for the fixture"
    return {"run_id": run_id, "floor": 2, "depth": 1, "started_at": 1.0,
            "ended_at": None, "outcome": None, "fight": None,
            "floors": [{"floor": 1, "seed": seed_used, "stat": dict(stats),
                        "actions": ["strike"] * 30, "won": True,
                        "turns": sim["turns"]}]}


def _arm_run(rig, name, run_id, settled_runs):
    """A seeded player whose spire state differs ONLY by settled_runs."""
    pid = rig["seed"](name)
    row = rig["players"][pid]
    row["tower_spire"] = tr.normalize_spire_state({
        "settled_runs": settled_runs,
        "last_settled": None,
        "active_run": _reproducible_one_floor_run(run_id, row)})
    rig["players"].persist(pid, row)
    return pid


def test_the_server_side_paid_set_is_what_stops_the_fresh_key_retry(rig):
    """Isolates guard 2 from every layer in front of it.

    ``last_settled`` is cleared and the run holds a REAL reproducible floor
    record, so the only thing that can answer this request is the membership
    test on ``settled_runs``. Pair this with
    ``test_the_paid_set_neutral_arm_would_pay`` immediately below: the two arms
    differ by exactly one field.
    """
    pid = _arm_run(rig, "paidset", "RID-HIT", ["RID-HIT"])
    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "fresh"},
                           json={"outcome": "death"})
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["already_paid"] is True
    assert summary["paid"] is False
    assert summary["reward"]["granted"] == {"stone": 0, "qi": 0.0}
    assert rig["wallet"].balance(pid) == 0
    # The already-paid reply is a DIFFERENT SHAPE from a first payment, and a
    # panel that reads reward.proof unconditionally gets a KeyError here
    # (tower_routes.py:645-650 builds this reward dict without one). Pin the
    # asymmetry rather than wish it away.
    assert set(summary["reward"]) == {"stone", "qi", "repeat", "granted"}
    assert "proof" not in summary["reward"]
    # It also reports week_best_depth from best_ever, not from this run's depth.
    assert summary["week_improved"] is False


def test_the_paid_set_neutral_arm_would_pay(rig):
    """The negative control, one state field apart from the test above.

    WITHOUT this, the test above is not evidence: a run holding no floor records
    cannot be paid at all, so it would refuse for an unrelated reason and still
    look like a working guard. Measured pair - same run, same record, only
    ``settled_runs`` differs:

        run id IN settled_runs   -> 200, already_paid True,  granted 0, wallet 0
        run id NOT in settled_runs -> 200, already_paid False, granted 7, wallet 7

    That difference is the paid set, and nothing else."""
    pid = _arm_run(rig, "neutral", "RID-MISS", [])
    r = rig["client"].post("/tower/spire/settle",
                           headers={**rig["hdr"](pid), "Idempotency-Key": "fresh"},
                           json={"outcome": "death"})
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["already_paid"] is False
    assert summary["reward"]["granted"]["stone"] > 0
    assert summary["reward"]["proof"]["mismatched"] == []
    assert summary["reward"]["proof"]["verified_depth"] == 1
    assert rig["wallet"].balance(pid) == summary["reward"]["granted"]["stone"]


def test_settle_pays_the_re_simulated_depth_not_the_claimed_one(rig):
    """The load-bearing guard. A caller who writes a depth into their own run
    record is paid the depth the server can reproduce from its own seeds."""
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
    assert summary["claimed_floors_cleared"] == 90
    assert summary["reward"]["granted"]["stone"] == cfg.rewards_for_run(
        1, runs_today=1)["stone"]


def test_an_honest_reproducible_loss_is_not_reported_as_a_mismatch(rig):
    """A run that ends in an honest, reproducible loss is the end of a run, not a
    finding. Reporting it as one puts a phantom mismatch in every single run's
    response, which is how a real finding stops being read."""
    state = tr.normalize_spire_state(None)
    run = {"run_id": "R", "floor": 1, "depth": 0, "started_at": 1.0,
           "ended_at": None, "outcome": None, "fight": None,
           "floors": [{"floor": 1, "seed": 7,
                       "stat": {"hp": 10, "atk": 1, "def_": 0, "realm": "mortal",
                                "stage": "early", "element": "fire"},
                       "actions": ["strike"], "won": False, "turns": 1}]}
    proof = tr.verify_depth(run)
    assert proof["verified_depth"] == 0
    assert proof["claimed_depth"] == 0
    assert proof["mismatched"] == [], "an honest loss became a finding"


def test_the_turn_loop_is_a_pure_function_of_its_seed(rig):
    """Settle-time re-simulation is only exact if a fight is a pure function of
    (stat line, enemy, seed, actions) - not of how the caller chunked the turns."""
    stats = {"hp": 1000, "atk": 100, "def_": 50, "realm": "mortal",
             "stage": "early", "element": "fire"}
    enemy = cfg.enemy_stats_for_floor(7)
    a = tr._simulate_fight(stats, enemy, 12345, ["strike", "heavy", "guard"])
    b = tr._simulate_fight(stats, enemy, 12345, ["strike", "heavy", "guard"])
    assert a["winner"] == b["winner"]
    assert a["turns"] == b["turns"]
    assert a["enemy_hp_remaining"] == b["enemy_hp_remaining"]
    assert a["player_hp_remaining"] == b["player_hp_remaining"]


def test_the_heavy_stance_respects_its_own_cooldown(rig):
    """A caller cannot buy the 1.8x skill on consecutive turns by naming it. A
    cooldown that started charged would make the stance unavailable on turn 1."""
    stats = {"hp": 1000000, "atk": 100, "def_": 50, "realm": "mortal",
             "stage": "early", "element": "fire"}
    enemy = dict(cfg.enemy_stats_for_floor(1), hp=10 ** 7)
    sim = tr._simulate_fight(stats, enemy, 99, ["heavy"] * 4)
    dealt = [e["damage_dealt"] for e in sim["log"][:4]]
    assert len(dealt) == 4, "the fight ended early, nothing to compare: " + str(dealt)
    assert dealt[0] > dealt[1] > 0, "cooldown not enforced: " + str(dealt)
    assert dealt[1] == dealt[2], "cooldown decayed one turn early: " + str(dealt)
    assert dealt[3] > dealt[2], "heavy never came back up: " + str(dealt)


def test_no_request_model_accepts_a_caller_honoured_number(rig):
    """player_id, depth, amount, stone and seed must not be expressible in a
    body. The payout is server-derived from the re-simulation, so any such field
    is a hole no other guard covers."""
    offenders = []
    for name, obj in vars(tr).items():
        if (inspect.isclass(obj) and hasattr(obj, "model_fields")
                and obj.__module__ == tr.__name__):
            for field in obj.model_fields:
                if field in ("player_id", "pid", "depth", "amount", "stone",
                             "seed", "won", "winner", "outcome_depth"):
                    offenders.append(name + "." + field)
    assert not offenders, str(offenders)


def test_the_action_request_defaults_to_strike_and_takes_nothing_else(rig):
    pid = rig["seed"]("default")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    body = c.post("/tower/spire/combat/action", headers=h, json={}).json()
    assert body["action"] == "strike"


# ------------------------------------------------------------- the party


def test_a_player_in_no_party_cannot_invite(rig):
    pid = rig["seed"]("noparty")
    other = rig["seed"]("target")
    r = rig["client"].post("/tower/spire/party/invite",
                           headers=rig["hdr"](pid),
                           json={"invitee_id": other})
    assert r.status_code == 400
    assert "not in a party" in r.json()["detail"]


def test_an_invitee_that_is_not_the_caller_and_not_a_player_is_refused(rig):
    """invitee_id is a THIRD PARTY, which is why it is not named player_id:
    naming it that would make the route refuse the real leader. Two refusals
    share one shape here, so the detail distinguishes them."""
    from dungeons import get_dungeon_manager
    leader = rig["seed"]("lead1")
    get_dungeon_manager().create_party(leader)
    c = rig["client"]
    h = rig["hdr"](leader)
    # Self-invite is refused before the existence check.
    self_invite = c.post("/tower/spire/party/invite", headers=h,
                         json={"invitee_id": leader})
    assert self_invite.status_code == 400
    assert "Invalid invitee" in self_invite.json()["detail"]
    # A name that is not in the store is 404, not a 200 that invites a ghost.
    ghost = c.post("/tower/spire/party/invite", headers=h,
                   json={"invitee_id": "c_nobody_at_all"})
    assert ghost.status_code == 404
    assert "Invitee not found" in ghost.json()["detail"]


def test_a_second_invite_from_the_same_party_is_refused(rig):
    from dungeons import get_dungeon_manager
    leader = rig["seed"]("lead2")
    guest = rig["seed"]("guest2")
    get_dungeon_manager().create_party(leader)
    c = rig["client"]
    first = c.post("/tower/spire/party/invite", headers=rig["hdr"](leader),
                   json={"invitee_id": guest})
    assert first.status_code == 200
    assert first.json()["pending"] == 1
    again = c.post("/tower/spire/party/invite", headers=rig["hdr"](leader),
                   json={"invitee_id": guest})
    assert again.status_code == 400
    assert "already has your invite" in again.json()["detail"]


def test_the_pending_invite_survives_a_fresh_read_of_the_invitee_row(rig):
    """The invite is a cross-player write, and the store hands back a DETACHED
    copy in memory mode. If the write is not persisted the next request reads
    nothing, and 'pending: 1' would be a lie."""
    from dungeons import get_dungeon_manager
    leader = rig["seed"]("lead3")
    guest = rig["seed"]("guest3")
    get_dungeon_manager().create_party(leader)
    r = rig["client"].post("/tower/spire/party/invite",
                           headers=rig["hdr"](leader),
                           json={"invitee_id": guest})
    assert r.status_code == 200
    # Re-read the invitee from the store, not from the reply.
    stored = rig["players"][guest]["tower_spire"]["party_invites"]
    assert len(stored) == 1, "the invite was not persisted"
    assert stored[0]["from"] == leader
    assert stored[0]["party_id"] == r.json()["party_id"]


def test_accept_with_no_pending_invite_is_refused(rig):
    pid = rig["seed"]("acceptor")
    r = rig["client"].post("/tower/spire/party/accept", headers=rig["hdr"](pid))
    assert r.status_code == 400
    assert "No pending invite" in r.json()["detail"]


def test_a_stranger_cannot_accept_someone_elses_invite(rig):
    from dungeons import get_dungeon_manager
    leader = rig["seed"]("lead4")
    guest = rig["seed"]("guest4")
    third = rig["seed"]("third4")
    get_dungeon_manager().create_party(leader)
    c = rig["client"]
    assert c.post("/tower/spire/party/invite", headers=rig["hdr"](leader),
                  json={"invitee_id": guest}).status_code == 200
    r = c.post("/tower/spire/party/accept", headers=rig["hdr"](third))
    assert r.status_code == 400
    assert "No pending invite" in r.json()["detail"]


def test_leave_with_no_party_is_refused(rig):
    pid = rig["seed"]("leaver")
    r = rig["client"].post("/tower/spire/party/leave", headers=rig["hdr"](pid))
    assert r.status_code == 400
    assert "not in a party" in r.json()["detail"]


def test_the_combat_loop_never_reads_party_membership(rig):
    """A party is a social label on this surface, not a combat modifier. Measured
    by playing one floor inside a party and comparing the run to the identical
    run with no party at all - if party size fed the fight, the two would differ."""
    from dungeons import get_dungeon_manager
    with_party = rig["seed"]("cmp_party")
    without = rig["seed"]("cmp_solo")
    get_dungeon_manager().create_party(with_party)
    a = play_one_floor(rig, with_party).json()
    b = play_one_floor(rig, without).json()
    assert a["won"] is True and b["won"] is True
    assert a["turns"] == b["turns"]
    assert a["depth"] == b["depth"] == 1
    assert a["current_floor"] == b["current_floor"] == 2


# --------------------------------------------- the two known defects (xfail)


def test_a_party_created_through_the_dungeon_surface_is_visible_to_the_tower(rig):
    import server.dungeons as packaged
    leader = rig["seed"]("split_leader")
    guest = rig["seed"]("split_guest")
    party = packaged.get_dungeon_manager().create_party(leader)
    assert party is not None
    assert leader in party.members, "control: the party really exists"
    r = rig["client"].post("/tower/spire/party/invite",
                           headers=rig["hdr"](leader),
                           json={"invitee_id": guest})
    assert r.status_code == 200, (
        "party " + party.id + " exists in server.dungeons but /tower/spire/"
        "party/invite says 'You are not in a party'")


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT 2 - the replay route reads floor records from active_run['floors'] "
    "and otherwise only checks the run id against history. The history entry "
    "carries 7 keys and not the per-floor records, so the seeds are gone after "
    "settle and the reply is found:true with floors:[]. Mid-run it works."))
def test_replay_of_a_settled_run_returns_its_floor_records(rig):
    pid = rig["seed"]("replaysettled")
    h = rig["hdr"](pid)
    c = rig["client"]
    run_id = c.post("/tower/spire/enter", headers=h).json()["run_id"]
    play_one_floor(rig, pid)
    assert c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "r1"},
                  json={"outcome": "death"}).status_code == 200
    r = c.get("/tower/spire/replay?run_id=" + run_id, headers=h)
    assert r.status_code == 200
    body = r.json()
    # The control: the run IS on the board, so the route is answering, not
    # failing to find it.
    assert body["found"] is True, "control: the run is in history"
    assert body["ended_at"] is not None, "control: it is a settled run"
    assert len(body["floors"]) == 1, (
        "a settled run replays as 0 floors: the per-floor seeds were never "
        "carried into history")


def test_the_party_split_was_a_module_identity_split_and_is_now_repaired(rig):
    """Pins the ROOT CAUSE of defect 1 so a fix is attributable.

    Both spellings resolve to the same FILE, which is why this read as a
    plumbing bug and not a data problem, and why the shipped launch (which runs
    no conftest) was affected too. The split is GONE: the tower and the dungeon
    surface now share one ``DungeonManager``, so a party created through either
    route is visible to the other. Asserted positively -- a test that only
    checked "no longer split" would pass against a module that failed to import
    at all.
    """
    import dungeons as bare
    import server.dungeons as packaged
    assert bare.__file__ == packaged.__file__, "one file, one module object"
    assert bare is packaged, "the two spellings must be the SAME module object"
    # The load-bearing half: the route handler and the module agree, so a party
    # written through one spelling is the party the other one reads.
    assert tr.get_dungeon_manager is bare.get_dungeon_manager
    assert tr.get_dungeon_manager is packaged.get_dungeon_manager
    assert bare.get_dungeon_manager() is packaged.get_dungeon_manager()
    # And the fix is behavioural, not just an identity assertion: a party
    # created through the module the dungeon surface uses must be readable by
    # the tower, which is the flow that answered 400 before.
    leader = rig["seed"]("repaired_leader")
    party = packaged.get_dungeon_manager().create_party(leader)
    assert party is not None and leader in party.members
    guest = rig["seed"]("repaired_guest")
    r = rig["client"].post("/tower/spire/party/invite",
                           headers=rig["hdr"](leader),
                           json={"invitee_id": guest})
    assert r.status_code == 200, (
        "party " + party.id + " exists but /tower/spire/party/invite still "
        "answers 'You are not in a party'")


def test_replay_is_empty_for_a_settled_run_today_this_is_the_pinned_shape(rig):
    """The counterpart to the xfail above: this asserts what the route does
    RIGHT NOW, so a future change that alters the shape has to update one of the
    two tests rather than silently moving the goalposts."""
    pid = rig["seed"]("replayshape")
    h = rig["hdr"](pid)
    c = rig["client"]
    run_id = c.post("/tower/spire/enter", headers=h).json()["run_id"]
    play_one_floor(rig, pid)
    c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "r2"},
           json={"outcome": "death"})
    body = c.get("/tower/spire/replay?run_id=" + run_id, headers=h).json()
    assert body["found"] is True
    assert body["floors"] == []
    assert body["ended_at"] is not None
    # Mid-run the same route returns the record, which is what makes the settled
    # case a defect rather than a route that never worked.
    live_pid = rig["seed"]("replaylive")
    lh = rig["hdr"](live_pid)
    live_id = c.post("/tower/spire/enter", headers=lh).json()["run_id"]
    play_one_floor(rig, live_pid)
    live = c.get("/tower/spire/replay?run_id=" + live_id, headers=lh).json()
    assert len(live["floors"]) == 1
    # The history entry is the reason: it cannot carry a replay.
    hist = c.get("/tower/spire/history", headers=h).json()["history"]
    assert len(hist) == 1
    assert set(hist[0]) == {"run_id", "depth", "claimed_depth", "outcome",
                            "stone", "qi", "ended_at"}


def test_the_history_entry_cannot_carry_a_replay(rig):
    """The data-shape half of defect 2. Seven keys, no seeds, no actions: even a
    future replay implementation cannot re-derive a settled run from this."""
    pid = rig["seed"]("histkeys")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    play_one_floor(rig, pid)
    c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": "r3"},
           json={"outcome": "death"})
    entry = c.get("/tower/spire/history", headers=h).json()["history"][0]
    assert set(entry) == {"run_id", "depth", "claimed_depth", "outcome",
                          "stone", "qi", "ended_at"}
    assert not ({"floors", "records", "seed", "actions", "stat"} & set(entry))