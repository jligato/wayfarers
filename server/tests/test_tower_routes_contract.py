"""
Contract test for ``server/tower_routes.py`` - the 13-route ``/tower/spire/*``
ascent surface.

WHAT THIS FILE IS
-----------------
A second, COMPLEMENTARY contract file. ``server/tests/test_tower_spire_contract.py``
already pins the happy-path reply key sets, the money guards and the party
split, so this one does not repeat them. It pins the parts of the wire that a
panel or an attacker actually hits and that file does not cover:

  * the 422 / 400 / 403 / 404 shape of every malformed or hostile request,
  * which route demands an idempotency key (idempotency = "do this at most
    once") and which does not,
  * that a save written by an older build, or a hand-edited row, is REPAIRED
    rather than crashed on,
  * the ranking and exclusion rules of the weekly board,
  * that one player's run is invisible to another player,
  * and one live defect, pinned as a defect.

Every number is read from ``tower_config`` at call time and only RELATIONSHIPS
are asserted, so a rebalance leaves this file green. The reply KEY SETS are
frozen on purpose: a client reads those names.

MOUNTING
--------
A bare ``FastAPI()``, never ``server.main``. ``main.py`` is 3000+ lines and
several agents edit it at once, so a file that imports it measures whatever a
peer is mid-write. ``players_db`` and ``wallet_store`` arrive as PROVIDERS and
are resolved per call, which is why a swapped wallet is actually read.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
        PYTHONPATH="G:/Projects/wayfarers-cycle/server;G:/Projects/wayfarers-cycle" \\
        WC_STORAGE_BACKEND=memory \\
        <python> -m pytest server/tests/test_tower_routes_contract.py -q

``--noconftest`` is NOT required on this tree. Re-measured 2026-10-05: the root
``conftest.py`` collects fine and this file runs green without it. An earlier
version of this note claimed the flag WAS required, because the root
``conftest.py`` imported ``server.main`` at collection time and
``server/main.py`` imported ``check_status_inline`` from ``lifecycle_guard``,
which did not export it. That export exists now - ``lifecycle_guard`` has
``check_status_inline`` and ``main`` imports it without error - so the flag was
carrying a repair that has already landed. Kept as a record rather than
silently deleted, because a note that hides a whole test directory behind an
unnecessary flag is how real tests go unrun for days.

THE PINNED DEFECT - a 500 on the money route
--------------------------------------------
``verify_depth`` catches ``KeyError``/``TypeError``/``ValueError`` while READING
a floor record, which reads like it covers every unreadable record. It does not:
it never validates the record's ``stat`` sub-dict, and ``_simulate_fight`` then
subscripts ``player_stats["atk"]`` directly. A stored record whose ``stat`` is
missing a key therefore escapes as an unhandled ``KeyError`` and the MONEY route
answers 500 with an empty body - measured, wallet untouched.

Every OTHER hostile record shape (a missing ``floor``, a record that is not a
dict, a record whose recorded win disagrees with the re-simulation) is handled
cleanly, and ``normalize_spire_state`` repairs a corrupt block at the top level
but never looks inside a floor record. So the hole is narrow and specific: the
guard's own repair function does not reach the one field that breaks.

It is pinned as ``xfail(strict=True)`` on the assertion that SHOULD hold, so the
suite is green today and goes RED the moment the behaviour is fixed and the
marker is forgotten. Severity is bounded and stated plainly: reaching it needs a
caller who can already write their own player row, which is the same trust level
as forging a depth - and forging a depth is what ``verify_depth`` exists to
defeat. This is a robustness gap on a state the module claims it repairs, not a
free-money exploit.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tower_config as cfg
import tower_routes as tr


#: The thirteen, as (method, path). The census below pins the whole set.
SPIRE_ROUTES = [
    ("POST", "/tower/spire/enter"),
    ("GET", "/tower/spire/floor-info"),
    ("POST", "/tower/spire/combat/start"),
    ("POST", "/tower/spire/combat/action"),
    ("POST", "/tower/spire/combat/end"),
    ("POST", "/tower/spire/settle"),
    ("GET", "/tower/spire/history"),
    ("GET", "/tower/spire/ladder"),
    ("GET", "/tower/spire/rewards"),
    ("POST", "/tower/spire/party/invite"),
    ("POST", "/tower/spire/party/accept"),
    ("POST", "/tower/spire/party/leave"),
    ("GET", "/tower/spire/replay"),
]

#: A body every POST route can accept without tripping validation.
NEUTRAL_BODY = {"outcome": "death"}


class _PlayerStore:
    """The ``PlayerStore`` surface the routes actually use.

    ``persist`` is load-bearing, not decoration: in memory mode ``store[pid]``
    hands back a DETACHED copy, so a write the route did not persist is a write
    nothing can see. A money assertion built on the unpersisted copy passes for
    the wrong reason.
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

    ``settle_run`` reaches ``wallet_dispatch.earn``, which calls
    ``store.transaction()`` on whatever it is handed. A hand-written stand-in
    with an ``earn`` method never runs, so every money assertion built on one
    passes against nothing. The real store also carries the wallet's own
    third idempotency layer, which a stub hides.
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


def body_of(response):
    """Read a body that a 500 may not have.

    A 500 carries an empty body, so an unguarded ``r.json()`` raises and kills
    the whole probe, leaving every later assertion unmeasured.
    """
    try:
        return response.json()
    except Exception:
        return None


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
    # leave every later assertion unmeasured - which is exactly what the pinned
    # defect below needs to be able to observe.
    client = TestClient(app, raise_server_exceptions=False)

    def seed(name, **over):
        pid = "c_" + str(name)
        row = {"id": pid, "name": str(name), "atk": 250000, "hp": 2500000,
               "def_": 300, "cultivation_realm": "core",
               "cultivation_stage": "late"}
        row.update(over)
        players.persist(pid, row)
        return pid

    def hdr(pid):
        return {"Authorization": "Bearer " + auth.issue_session(pid)}

    def call(method, path, pid, json=None, extra_headers=None, params=None):
        headers = hdr(pid)
        headers.update(extra_headers or {})
        return client.request(method, path, headers=headers, json=json,
                              params=params)

    return {"client": client, "players": players, "wallet": wallet, "app": app,
            "seed": seed, "hdr": hdr, "call": call}


def play_one_floor(rig, pid, action="strike"):
    """Enter, open a fight, drive it to a decision, close it."""
    c = rig["client"]
    h = rig["hdr"](pid)
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    for _ in range(cfg.TOWER_RUN_MAX_TURNS + 5):
        r = c.post("/tower/spire/combat/action", headers=h, json={"action": action})
        if r.status_code != 200 or body_of(r).get("resolved"):
            break
    return c.post("/tower/spire/combat/end", headers=h)


def winning_seed(stat, floor, actions=None, limit=2000):
    """A seed whose recorded fight the server reproduces as a PLAYER win.

    Found by driving the shipped ``_simulate_fight`` rather than by writing
    numbers by hand, so the record genuinely reproduces. A record that does not
    reproduce cannot distinguish a paid-set refusal from "nothing to pay".
    """
    acts = list(actions or ["strike"] * 30)
    for candidate in range(1, limit + 1):
        if tr._simulate_fight(stat, cfg.enemy_stats_for_floor(floor),
                              candidate, acts)["winner"] == "player":
            return candidate
    return None


def reproducible_run(run_id, stat, floor=1, seed_used=None, depth=1):
    """A run record holding ONE floor the server can re-derive."""
    acts = ["strike"] * 30
    seed_used = seed_used if seed_used is not None else winning_seed(stat, floor, acts)
    assert seed_used is not None, "no reproducible seed for the fixture"
    return {"run_id": run_id, "floor": floor + 1, "depth": depth,
            "started_at": 1.0, "ended_at": None, "outcome": None, "fight": None,
            "floors": [{"floor": floor, "seed": seed_used, "stat": dict(stat),
                        "actions": acts, "won": True, "turns": 30}]}


def seed_run_state(rig, name, run_id="RID", settled=(), run=None, **state_bits):
    """A player whose ascent block holds ``run`` and nothing else of interest."""
    pid = rig["seed"](name)
    row = rig["players"][pid]
    stat = tr.player_stat_line(row)
    block = {"settled_runs": list(settled), "last_settled": None,
             "active_run": run if run is not None
             else reproducible_run(run_id, stat)}
    block.update(state_bits)
    row[tr.SPIRE_KEY] = tr.normalize_spire_state(block)
    rig["players"].persist(pid, row)
    return pid


#: Two measured stat lines. On floors 1-3 across 300 seeds, STRONG_WINS wins 299
#: and WEAK_LOSES loses 299 - so a fixture built from them reproduces
#: consistently instead of depending on one lucky seed. Hunting for a single
#: losing seed does not work on this curve: at a fresh player's stat line every
#: seed WINS a low floor, which is why the first version of the prefix test
#: could not build a failing record at all.
STRONG_WINS = {"hp": 1000, "atk": 100, "def_": 50, "realm": "mortal",
               "stage": "early", "element": "fire"}
WEAK_LOSES = {"hp": 50, "atk": 1, "def_": 0, "realm": "mortal",
              "stage": "early", "element": "fire"}
STRIKES = ["strike"] * 30


def _sim_winner(stat, floor, seed_used, actions=None):
    return tr._simulate_fight(stat, cfg.enemy_stats_for_floor(floor), seed_used,
                              list(actions or STRIKES))["winner"]


# ============================================================ the route census


def test_the_thirteen_routes_are_exactly_these_thirteen(rig):
    """The whole surface, pinned as a set.

    A rename is a breaking change for every caller and must be a failing test.
    ``HEAD`` is excluded because FastAPI adds it to every ``GET`` silently.
    """
    served = set()
    for route in rig["app"].routes:
        for method in (getattr(route, "methods", None) or ()):
            if method == "HEAD":
                continue
            served.add((method, getattr(route, "path", "")))
    assert {p for _, p in served if p.startswith("/tower/spire")} == {
        p for _, p in SPIRE_ROUTES}
    assert len(SPIRE_ROUTES) == 13


def test_the_spire_prefix_does_not_shadow_the_three_older_ladders(rig):
    """``/tower/enter`` and ``/tower/info`` belong to the numbered ladder.

    FastAPI answers the FIRST match, so a fourth shape reusing either name would
    exist in source and answer 404 forever. Asserted as disjoint prefixes
    rather than as a count.
    """
    paths = {getattr(r, "path", "") for r in rig["app"].routes}
    older = {p for p in paths if p.startswith("/tower/")}
    assert older == {p for _, p in SPIRE_ROUTES}, "an older ladder route is missing"
    assert not {"/tower/enter", "/tower/info", "/tower/claim"} & older


# ==================================================== authentication (the guard)


@pytest.mark.parametrize("method,path", SPIRE_ROUTES)
def test_every_route_refuses_a_caller_with_no_session(rig, method, path):
    """No route is reachable without a token. The party and board routes name
    no player, so nothing but the hand-wired dependency protects them."""
    r = rig["client"].request(method, path, json=NEUTRAL_BODY)
    assert r.status_code in (401, 403), path + " answered " + str(r.status_code)


#: Every route EXCEPT the weekly board. ``/tower/spire/ladder`` is ranked from
#: every player's stored state and never loads the caller's own row, so it has
#: nothing to 404 on - pinned separately below rather than hidden here.
PLAYER_SCOPED_ROUTES = [(m, p) for m, p in SPIRE_ROUTES
                        if p != "/tower/spire/ladder"]


@pytest.mark.parametrize("method,path", PLAYER_SCOPED_ROUTES)
def test_a_token_for_a_player_who_does_not_exist_is_404(rig, method, path):
    """A VALID token for an unknown player is 404, never a 500 and never a
    silently created row.

    ``settle`` is sent WITH an idempotency key on purpose: that gate runs before
    the player is loaded, so without the header this route answers 400 for a
    reason that has nothing to do with the player being missing.
    """
    import auth
    token = auth.issue_session("c_" + path.strip("/").replace("/", "_"))
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "ghost-key"}
    params = {"run_id": "some-run"} if path == "/tower/spire/replay" else None
    payload = NEUTRAL_BODY
    if path == "/tower/spire/party/invite":
        payload = {"invitee_id": "somebody"}
    elif path == "/tower/spire/combat/action":
        payload = {"action": "strike"}
    r = rig["client"].request(method, path, headers=headers, json=payload,
                              params=params)
    assert r.status_code == 404, path + " answered " + str(r.status_code)
    assert body_of(r)["detail"] == "Player not found"


def test_the_weekly_board_answers_for_a_player_with_no_row(rig):
    """``/tower/spire/ladder`` is the one route that reads no player row.

    It ranks every player's stored state and reports the caller's own rank, so
    there is nothing to 404 on. The contract is that it still requires a SESSION
    (a no-token call is refused by the test above) and that it leaks nothing
    about a caller who has no row: an empty board, rank 0.
    """
    import auth
    token = auth.issue_session("c_never_existed")
    r = rig["client"].get("/tower/spire/ladder",
                          headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200, body_of(r)
    body = body_of(r)
    assert body["player_rank"] == 0
    assert body["week"], "the board must still name its week"
    # Nothing about a missing caller is echoed back.
    assert "c_never_existed" not in body["ladder"]


def test_a_body_naming_another_player_is_refused_not_ignored(rig):
    """The acting player is the session's, and the session's alone.

    Measured: a body carrying somebody else's ``player_id`` is refused with 403
    and an ``AUTH_OWNERSHIP_VIOLATION`` log line, and the named player's run is
    untouched. Pin the refusal, not the absence of damage - a route that merely
    IGNORED the field would pass a weaker version of this test.
    """
    victim = rig["seed"]("victim")
    stranger = rig["seed"]("stranger")
    hv = rig["hdr"](victim)
    rig["client"].post("/tower/spire/enter", headers=hv)
    run_id = rig["players"][victim][tr.SPIRE_KEY]["active_run"]["run_id"]

    r = rig["call"]("POST", "/tower/spire/combat/action", stranger,
                    json={"action": "strike", "player_id": victim})
    assert r.status_code == 403, body_of(r)
    assert run_id == rig["players"][victim][tr.SPIRE_KEY]["active_run"]["run_id"]
    assert rig["players"][victim][tr.SPIRE_KEY]["active_run"]["fight"] is None

    # The same on a route with no body model at all.
    r2 = rig["call"]("POST", "/tower/spire/enter", stranger,
                     json={"player_id": victim})
    assert r2.status_code == 403, body_of(r2)


def test_one_players_run_is_invisible_to_another_player(rig):
    """Every read is the CALLER's own. A run started by A changes nothing B sees."""
    a = rig["seed"]("Alpha")
    b = rig["seed"]("Bravo")
    rig["client"].post("/tower/spire/enter", headers=rig["hdr"](a))
    rig["client"].post("/tower/spire/combat/start", headers=rig["hdr"](a))
    rig["client"].post("/tower/spire/combat/action", headers=rig["hdr"](a),
                       json={"action": "strike"})

    a_floor = body_of(rig["call"]("GET", "/tower/spire/floor-info", a))
    b_floor = body_of(rig["call"]("GET", "/tower/spire/floor-info", b))
    assert a_floor["run_active"] is True
    assert a_floor["fight_open"] is True
    assert b_floor["run_active"] is False
    assert b_floor["fight_open"] is False
    assert b_floor["current_floor"] == 1, "B inherited A's floor pointer"
    assert body_of(rig["call"]("GET", "/tower/spire/history", b))["run_active"] is False


# ================================================ request validation (the 422s)


@pytest.mark.parametrize("payload,reason", [
    ({"action": 5}, "a number is not a stance name"),
    ({"action": None}, "null is not a stance name"),
    ({"action": ["strike"]}, "a list is not a stance name"),
    ({"action": {"name": "strike"}}, "an object is not a stance name"),
])
def test_a_stance_that_is_not_a_string_is_422(rig, payload, reason):
    """Wrong TYPE is a 422 from the body model, before the handler runs.

    Distinct from an unknown stance NAME, which is a 400. A panel sending the
    wrong JSON type must be told so by the schema, not by a combat message.
    """
    pid = rig["seed"]("type")
    h = rig["hdr"](pid)
    rig["client"].post("/tower/spire/enter", headers=h)
    rig["client"].post("/tower/spire/combat/start", headers=h)
    r = rig["call"]("POST", "/tower/spire/combat/action", pid, json=payload)
    assert r.status_code == 422, reason + " -> " + str(body_of(r))
    assert body_of(r)["detail"][0]["loc"] == ["body", "action"]


@pytest.mark.parametrize("payload", [{}, {"invitee_id": ""}, {"invitee_id": None},
                                     {"invitee_id": 5}, {"invitee_id": "a" * 65}])
def test_the_invitee_must_be_a_bounded_non_empty_string(rig, payload):
    """``invitee_id`` is 1..64 characters. A missing, empty, null, non-string or
    oversized invitee is 422 from the schema.

    The field is deliberately NOT called ``player_id``: naming a third party
    that way would make the ownership guard treat it as a claimed acting player.
    """
    pid = rig["seed"]("inv" + str(abs(hash(str(payload))) % 9999))
    r = rig["call"]("POST", "/tower/spire/party/invite", pid, json=payload)
    assert r.status_code == 422, str(payload) + " -> " + str(body_of(r))
    assert body_of(r)["detail"][0]["loc"] == ["body", "invitee_id"]


def test_the_invitee_length_bounds_are_the_declared_ones(rig):
    """Pin the two edges, not just the fact that a long one fails.

    The bounds live in ``FieldInfo.metadata`` as ``MinLen``/``MaxLen`` entries,
    not as attributes on the field - an attribute read raises. Asserting through
    ``metadata`` is what makes this a contract rather than a pydantic-version
    accident.
    """
    from pydantic import BaseModel

    field = tr.SpireInviteRequest.model_fields["invitee_id"]
    kinds = {type(m).__name__ for m in field.metadata}
    assert kinds == {"MinLen", "MaxLen"}, kinds
    lengths = {(getattr(m, "min_length", None), getattr(m, "max_length", None))
               for m in field.metadata}
    assert lengths == {(1, None), (None, 64)}, lengths
    assert tr.SpireInviteRequest(invitee_id="a" * 64).invitee_id == "a" * 64
    with pytest.raises(Exception):
        tr.SpireInviteRequest(invitee_id="a" * 65)


def test_settle_with_no_body_at_all_is_422(rig):
    """The body model exists but every field has a default, so an ABSENT body is
    still 422 - FastAPI requires the body object to be present.

    Measured, and worth pinning: a client that treats "all fields optional" as
    "body optional" reads 200 in a mock and 422 in production.
    """
    pid = rig["seed"]("nobody")
    rig["client"].post("/tower/spire/enter", headers=rig["hdr"](pid))
    r = rig["call"]("POST", "/tower/spire/settle", pid,
                    extra_headers={"Idempotency-Key": "nb"})
    assert r.status_code == 422, body_of(r)
    assert body_of(r)["detail"][0]["loc"] == ["body"]


@pytest.mark.parametrize("key,label", [("", "empty"), ("   ", "whitespace")])
def test_a_blank_idempotency_key_is_refused_as_missing(rig, key, label):
    """A header that is present but blank is not a key. Refused by the gate,
    before the run is read, so it costs nothing."""
    pid = seed_run_state(rig, "blank_" + label)
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": key})
    assert r.status_code == 400, label + " -> " + str(body_of(r))
    assert "Idempotency-Key" in body_of(r)["detail"]
    assert rig["wallet"].balance(pid) == 0


def test_no_request_model_names_an_acting_player(rig):
    """The session is the only source of the acting player, so NO request model
    may carry a field the guard could mistake for one, and none may carry a
    number a caller could use to state an outcome."""
    for name in ("SpireEnterRequest", "SpireActionRequest",
                 "SpireEndFightRequest", "SpireSettleRequest",
                 "SpireInviteRequest", "SpireAcceptRequest"):
        fields = set(getattr(tr, name).model_fields)
        assert "player_id" not in fields, name + " can name an acting player"
        assert "pid" not in fields, name + " can name an acting player"
        assert not fields & {"depth", "floors_cleared", "stone", "amount",
                             "reward", "damage", "won", "outcome_depth",
                             "verified_depth"}, name + " takes a number it must not"


def test_settles_outcome_field_cannot_declare_the_result(rig):
    """``outcome`` is accepted and then IGNORED - the run record decides.

    Measured: a body of ``{"outcome": "cleared"}`` on a run that ended mid-ladder
    still reports ``"death"``, because ``settle_run`` overwrites ``outcome``
    from the floor pointer. Pinned so a client does not trust the echo.
    """
    pid = seed_run_state(rig, "outcome")
    r = rig["call"]("POST", "/tower/spire/settle", pid,
                    json={"outcome": "cleared"},
                    extra_headers={"Idempotency-Key": "out-1"})
    assert r.status_code == 200, body_of(r)
    summary = body_of(r)["summary"]
    assert summary["outcome"] == "death", "the body's outcome reached the summary"
    # The run stopped well short of the top of the ladder.
    assert summary["floors_cleared"] < cfg.TOWER_RUN_TOTAL_FLOORS


# ======================================== idempotency: which route demands it


def test_only_the_money_route_demands_an_idempotency_key(rig):
    """``settle`` refuses without one; every other write proceeds without it.

    Idempotency protects a route that MOVES VALUE, so demanding a key on a
    route that writes no value is noise a client cannot act on. Asserted by
    driving the free routes with no header at all.
    """
    pid = rig["seed"]("noidem")
    h = rig["hdr"](pid)
    c = rig["client"]
    assert "Idempotency-Key" not in h
    assert c.post("/tower/spire/enter", headers=h).status_code == 200
    assert c.post("/tower/spire/combat/start", headers=h).status_code == 200
    assert c.post("/tower/spire/combat/action", headers=h,
                  json={"action": "strike"}).status_code == 200
    # Close the resolved fight, then prove the MONEY route is the one that stops.
    assert c.post("/tower/spire/combat/end", headers=h).status_code == 200
    assert c.post("/tower/spire/settle", headers=h,
                  json=NEUTRAL_BODY).status_code == 400
    assert "Idempotency-Key" in body_of(c.post(
        "/tower/spire/settle", headers=h, json=NEUTRAL_BODY))["detail"]
    assert rig["wallet"].balance(pid) == 0, "an unkeyed settle paid something"


def test_the_idempotency_key_is_named_in_the_refusal(rig):
    """The refusal names the header AND the route, so a client can fix itself
    without reading this file."""
    pid = seed_run_state(rig, "named")
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY)
    detail = body_of(r)["detail"]
    assert "Idempotency-Key" in detail
    assert "tower_spire_settle" in detail


# ============================================ corrupt saves are repaired, not fatal


@pytest.mark.parametrize("label,raw", [
    ("missing", "__ABSENT__"),
    ("null", None),
    ("string", "garbage"),
    ("list", [1, 2, 3]),
    ("number", 5),
    ("empty_dict", {}),
    ("every_field_wrong_type", {
        "active_run": "x", "settled_runs": "no", "history": {},
        "weekly_best": [], "best_ever": "abc", "runs_today": None,
        "runs_day": 7, "party_invites": "none", "last_settled": 9}),
    ("unknown_extra_key", {"best_ever": 3, "surprise_key": 1}),
])
def test_a_corrupt_spire_block_is_repaired_not_crashed(rig, label, raw):
    """An old save, a hand-edited row and a row from a future build all land in
    ``normalize_spire_state``, and every one is REPAIRED.

    Every read route answers 200 and the state block comes back to the shipped
    shape. A route that raised on a corrupt row would turn a bad row into an
    unrecoverable account.
    """
    pid = rig["seed"]("corrupt_" + label)
    row = rig["players"][pid]
    if raw == "__ABSENT__":
        assert tr.SPIRE_KEY not in row
    else:
        row[tr.SPIRE_KEY] = raw
        rig["players"].persist(pid, row)

    h = rig["hdr"](pid)
    c = rig["client"]
    for path in ("/tower/spire/floor-info", "/tower/spire/history",
                 "/tower/spire/rewards", "/tower/spire/ladder"):
        assert c.get(path, headers=h).status_code == 200, label + " on " + path
    assert c.post("/tower/spire/enter", headers=h).status_code == 200

    repaired = rig["players"][pid][tr.SPIRE_KEY]
    assert set(repaired) == set(tr.blank_spire_state()), label + " left the shape wrong"
    assert repaired["active_run"] is not None, label + " lost the run it just started"
    assert repaired["settled_runs"] == [] and repaired["history"] == []
    assert repaired["weekly_best"] == {} and repaired["party_invites"] == []
    # MEASURED GAP: ``last_settled`` is the one key normalize_spire_state does
    # NOT type-check. A block carrying the number 9 there keeps the 9, so the
    # block is not as repaired as the docstring's "every one of them are
    # repaired" claims. Harmless today - every reader does isinstance(..., dict)
    # before using it, so no route crashes - but it is the shape that exists.
    assert not isinstance(repaired["last_settled"], dict), (
        label + ": last_settled is now type-checked; update this assertion")


def test_repair_keeps_the_numbers_that_survived_and_drops_what_it_cannot_read(rig):
    """Two halves, pinned together because one without the other is a bug in
    either direction: a readable counter is KEPT (a rebalance must not reset a
    player's record), and an unreadable one falls back to zero rather than
    raising."""
    kept = tr.normalize_spire_state({"best_ever": 12, "runs_today": 3})
    assert kept["best_ever"] == 12 and kept["runs_today"] == 3

    dropped = tr.normalize_spire_state({"best_ever": "abc", "runs_today": None})
    assert dropped["best_ever"] == 0 and dropped["runs_today"] == 0

    # An unknown key is DROPPED, not carried: the block is a closed shape, so a
    # row from a future build cannot smuggle a field a route would then trust.
    assert "surprise_key" not in tr.normalize_spire_state({"surprise_key": 1})


def test_a_corrupt_block_does_not_lose_a_players_recorded_depth(rig):
    """The repair runs on EVERY read, so a corrupt block must not reset a real
    number a player already earned - the board would silently forget them."""
    pid = rig["seed"]("depthkeeper")
    row = rig["players"][pid]
    row[tr.SPIRE_KEY] = {"best_ever": 41, "weekly_best": {"2026-W01": 9},
                         "settled_runs": "corrupt", "history": "corrupt"}
    rig["players"].persist(pid, row)
    info = body_of(rig["call"]("GET", "/tower/spire/floor-info", pid))
    assert info["best_ever"] == 41, "the repair reset a player's best"


# ==================================== the weekly board: ranking and exclusion


def _board_store(week, rows):
    store = _PlayerStore()
    store.rows = rows
    return store


def test_the_board_ranks_by_depth_then_name_then_id(rig):
    """The sort key is a CONTRACT: depth first, and the two tie-breaks make the
    order total, so the same state always renders the same board.

    Measured: two players on 7 rank by NAME, not by store order or player id.
    """
    from server.dungeons import utc_week_key
    week = utc_week_key()
    store = _board_store(week, {
        "z_7": {"name": "Zoe", tr.SPIRE_KEY: {"weekly_best": {week: 7}}},
        "a_7": {"name": "Abe", tr.SPIRE_KEY: {"weekly_best": {week: 7}}},
        "m_3": {"name": "Moe", tr.SPIRE_KEY: {"weekly_best": {week: 3}}},
    })
    rows = tr.weekly_board(store, week=week, limit=50)
    assert [r["player_id"] for r in rows] == ["a_7", "z_7", "m_3"]
    assert [r["best_depth"] for r in rows] == [7, 7, 3]
    assert set(rows[0]) == {"player_id", "name", "best_depth", "best_ever"}


def test_the_board_excludes_a_player_with_no_depth_this_week(rig):
    """A zero, an absent week and a different week are all "not on the board".

    A board that listed every player would rank a player who has never played
    against one who cleared 90 floors.
    """
    from server.dungeons import utc_week_key
    week = utc_week_key()
    store = _board_store(week, {
        "on_it": {"name": "On", tr.SPIRE_KEY: {"weekly_best": {week: 4}}},
        "zero": {"name": "Zero", tr.SPIRE_KEY: {"weekly_best": {week: 0}}},
        "no_week": {"name": "None", tr.SPIRE_KEY: {"weekly_best": {}}},
        "last_week": {"name": "Old", tr.SPIRE_KEY: {"weekly_best": {"1999-01-01": 99}}},
        "no_block": {"name": "Bare"},
        "bad_block": {"name": "Bad", tr.SPIRE_KEY: "not-a-dict"},
        "not_a_dict": "not-a-dict",
    })
    rows = tr.weekly_board(store, week=week, limit=50)
    assert [r["player_id"] for r in rows] == ["on_it"]
    # A player on a DIFFERENT week is ranked on THAT week, not this one.
    assert [r["player_id"] for r in
            tr.weekly_board(store, week="1999-01-01", limit=50)] == ["last_week"]


def test_a_player_with_no_name_is_listed_as_unknown_rig(rig):
    """A missing display name must not drop the row or crash the board - the
    player still cleared floors, and the board measures depth."""
    from server.dungeons import utc_week_key
    week = utc_week_key()
    store = _board_store(week, {
        "anon": {tr.SPIRE_KEY: {"weekly_best": {week: 2}}}})
    rows = tr.weekly_board(store, week=week, limit=50)
    assert rows == [{"player_id": "anon", "name": "Unknown", "best_depth": 2,
                     "best_ever": 0}]


def test_the_board_limit_is_a_floor_of_one_not_a_floor_of_zero(rig):
    """``limit`` is clamped to at least one row, so a client sending 0 or a
    negative number gets the top of the board rather than an empty page that
    looks like "nobody has played"."""
    from server.dungeons import utc_week_key
    week = utc_week_key()
    store = _board_store(week, {
        "a": {"name": "A", tr.SPIRE_KEY: {"weekly_best": {week: 5}}},
        "b": {"name": "B", tr.SPIRE_KEY: {"weekly_best": {week: 4}}},
        "c": {"name": "C", tr.SPIRE_KEY: {"weekly_best": {week: 3}}},
    })
    assert len(tr.weekly_board(store, week=week, limit=2)) == 2
    for limit in (0, -5, -100):
        assert len(tr.weekly_board(store, week=week, limit=limit)) == 1, limit
    assert len(tr.weekly_board(store, week=week, limit=999)) == 3


def test_the_ladder_reports_a_rank_or_zero_and_never_a_guess(rig):
    """``player_rank`` is 1-based over the returned rows, and 0 means "not on
    this board". 0 is deliberately distinct from 1 so a panel can say so."""
    from server.dungeons import utc_week_key
    week = utc_week_key()
    ranked = rig["seed"]("ranked")
    row = rig["players"][ranked]
    row[tr.SPIRE_KEY] = tr.normalize_spire_state({"weekly_best": {week: 4}})
    rig["players"].persist(ranked, row)

    body = body_of(rig["call"]("GET", "/tower/spire/ladder", ranked))
    assert set(body) == {"ok", "week", "ladder", "player_rank"}
    assert body["week"] == week
    assert body["player_rank"] == 1
    assert body["ladder"][0]["player_id"] == ranked

    unranked = rig["seed"]("unranked")
    assert body_of(rig["call"]("GET", "/tower/spire/ladder",
                               unranked))["player_rank"] == 0


def test_the_ladder_limit_must_be_a_number(rig):
    """A non-numeric limit is 422 from the query model. Measured; asserted so a
    client building the query string finds out at the type, not at runtime."""
    pid = rig["seed"]("lim")
    r = rig["call"]("GET", "/tower/spire/ladder", pid, params={"limit": "abc"})
    assert r.status_code == 422, body_of(r)
    assert body_of(r)["detail"][0]["loc"] == ["query", "limit"]


# ================================== the re-simulation's input tolerance


@pytest.mark.xfail(strict=True, reason="verify_depth raises KeyError on a floor "
                                       "record whose stat is missing a key")
def test_a_floor_record_missing_a_stat_key_is_reported_not_raised(rig):
    """THE PINNED DEFECT. The load-bearing guard must not be the thing that
    breaks.

    ``verify_depth`` catches ``KeyError`` while READING a floor record, which
    reads like it covers every unreadable record. It does not: it never validates
    the record's ``stat`` sub-dict, and ``_simulate_fight`` subscripts
    ``player_stats["atk"]`` directly. A stored record missing a stat key escapes
    as an unhandled ``KeyError``.

    Measured twice. Called directly, ``verify_depth`` raises. Driven over HTTP,
    ``POST /tower/spire/settle`` answers 500 with an EMPTY body, on the one route
    that moves currency, with the wallet untouched. Every OTHER hostile record
    shape - a missing ``floor``, a record that is not a dict, a seed that is not
    a number - is handled cleanly (see the tests below), and
    ``normalize_spire_state`` repairs the block at the top level but never looks
    inside a floor record. So the hole is narrow: the repair function does not
    reach the one field that breaks.

    ``xfail(strict=True)``: green today, RED the moment the behaviour is fixed
    and the marker is forgotten. Severity is bounded and stated plainly -
    reaching it needs a caller who can already write their own player row, the
    same trust level as forging a depth, and forging a depth is what
    ``verify_depth`` exists to defeat. A robustness gap on state the module
    claims it repairs, not a free-money exploit.

    What SHOULD happen instead: the partial record is reported like any other
    unreadable record, which verifies zero floors, so settle refuses with the
    same 400 every other unverifiable run gets and the wallet stays untouched.
    That is exactly the shape the three sibling tests below already assert.
    """
    pid = seed_run_state(rig, "partialstat", run_id="RP",
                         run={"run_id": "RP", "floor": 2, "depth": 1,
                              "started_at": 1.0, "ended_at": None,
                              "outcome": None, "fight": None,
                              "floors": [{"floor": 1, "seed": 1,
                                          "stat": {"hp": 1000},
                                          "actions": ["strike"] * 30,
                                          "won": True, "turns": 30}]})
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": "partial-1"})
    # A correct fix reports the record as unreadable, which verifies ZERO
    # floors, which makes settle refuse for the same reason every other
    # unverifiable run refuses. The answer is therefore a 400 and no money -
    # not a 200 with a proof block, because a refusal carries no proof block.
    assert r.status_code == 400, (
        "settle answered " + str(r.status_code) + " on a record whose stat is "
        "partial; the re-simulation guard raised instead of reporting")
    assert rig["wallet"].balance(pid) == 0, "an unreadable record paid something"


@pytest.mark.parametrize("label,record,verified,reason_substring", [
    ("record_is_not_a_dict", "garbage", 0, "unreadable record"),
    ("missing_floor_key", {"seed": 1, "stat": {"hp": 1}, "actions": []},
     0, "unreadable record"),
    ("seed_is_not_a_number", {"floor": 1, "seed": "not-a-number",
                              "stat": {"hp": 1, "atk": 1}, "actions": []},
     0, "unreadable record"),
])
def test_a_malformed_floor_record_yields_zero_verified_floors(rig, label, record,
                                                              verified, reason_substring):
    """The shapes the guard DOES handle. Pinned beside the one it does not, so
    the hole above reads as a specific gap and not as "the guard is absent".

    A malformed record must verify NOTHING. Paying for a record the server
    cannot re-derive is the exact forgery the guard exists to stop.
    """
    pid = seed_run_state(rig, "mal_" + label, run_id="RM", run={
        "run_id": "RM", "floor": 2, "depth": 1, "started_at": 1.0,
        "ended_at": None, "outcome": None, "fight": None,
        "floors": [record]})
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": "mal-1"})
    assert r.status_code == 400, label + " -> " + str(body_of(r))
    assert rig["wallet"].balance(pid) == 0, label + " paid something"


def test_a_record_whose_recorded_win_disagrees_is_reported_with_its_floor(rig):
    """The finding names the FLOOR, not just the index: the index is meaningless
    to a player reading a summary and the floor is not."""
    run = {"depth": 1, "floors": [{"floor": 1, "seed": 1,
                                   "stat": dict(WEAK_LOSES),
                                   "actions": list(STRIKES), "won": True,
                                   "turns": 30}]}
    assert _sim_winner(WEAK_LOSES, 1, 1) != "player", "the fixture no longer loses"
    proof = tr.verify_depth(run)
    assert proof["verified_depth"] == 0, "a disagreeing record verified as a floor"
    assert len(proof["mismatched"]) == 1
    finding = proof["mismatched"][0]
    assert finding["floor"] == 1, finding
    assert "reproduces" in finding["reason"]
    assert "index" in finding, finding


@pytest.mark.xfail(strict=True, reason="the two reason strings are swapped")
@pytest.mark.parametrize("recorded_win,stat,label", [
    (True, WEAK_LOSES, "recorded as a WIN, reproduces as a LOSS"),
    (False, STRONG_WINS, "recorded as a LOSS, reproduces as a WIN"),
])
def test_the_mismatch_reason_names_the_right_direction(rig, recorded_win, stat,
                                                       label):
    """SECOND PINNED DEFECT: the two reason strings are SWAPPED.

    Measured both directions on the shipped code:

        record says won,  re-simulation loses  ->  "recorded as a loss but
                                                       reproduces as a win"
        record says lost, re-simulation wins   ->  "recorded as a win but
                                                       reproduces as a loss"

    Each sentence describes the OPPOSITE of what happened. The guard behaves
    correctly - it stops the payout and records the floor - so this is a
    REPORTING defect, not a money defect: a panel or an operator reading the
    finding is told the inverse of the truth about which side forged.

    ``xfail(strict=True)`` so swapping the strings turns this RED.

    Severity: low. It misinforms a reader of the finding; it does not move
    currency, because ``verified_depth`` is 0 in both directions either way.
    """
    run = {"depth": 1, "floors": [{"floor": 1, "seed": 1, "stat": dict(stat),
                                   "actions": list(STRIKES),
                                   "won": recorded_win, "turns": 30}]}
    proof = tr.verify_depth(run)
    finding = proof["mismatched"][0]
    assert proof["verified_depth"] == 0, label + " paid something"
    expected = ("recorded as a win but reproduces as a loss" if recorded_win
                else "recorded as a loss but reproduces as a win")
    assert finding["reason"] == expected, (
        label + ": the module reports " + repr(finding["reason"]))


@pytest.mark.xfail(strict=True, reason="the two reason strings are swapped")
def test_the_mismatch_reason_text_is_inverted_today(rig):
    """The swap itself, pinned so it cannot be quietly re-introduced. Flip to a
    plain assertion once the strings are corrected."""
    run = {"depth": 1, "floors": [{"floor": 1, "seed": 1,
                                   "stat": dict(WEAK_LOSES),
                                   "actions": list(STRIKES), "won": True,
                                   "turns": 30}]}
    reason = tr.verify_depth(run)["mismatched"][0]["reason"]
    assert reason == "recorded as a win but reproduces as a loss", reason


def test_verification_stops_at_the_first_floor_that_does_not_reproduce(rig):
    """A run is a PREFIX. Three records, the SECOND not reproducing, pays the
    first and nothing after - it does not pay the verifiable third.

    The fixture is built from two MEASURED stat lines rather than one lucky
    seed. On this curve a fresh player's stat line wins a low floor on 299 of
    300 seeds, so there is no "losing seed on floor 2" to find: the first
    version of this test hunted for one and could not build a fixture at all.
    Varying the record's OWN stat line is both measurable and seed-stable.
    """
    assert _sim_winner(STRONG_WINS, 1, 1) == "player"
    assert _sim_winner(WEAK_LOSES, 2, 1) != "player"
    run = {"run_id": "RP2", "floor": 4, "depth": 3, "started_at": 1.0,
           "ended_at": None, "outcome": None, "fight": None,
           "floors": [
               {"floor": 1, "seed": 1, "stat": dict(STRONG_WINS),
                "actions": list(STRIKES), "won": True, "turns": 30},
               # Floor 2 CLAIMS a win on a fight that does not reproduce.
               {"floor": 2, "seed": 1, "stat": dict(WEAK_LOSES),
                "actions": list(STRIKES), "won": True, "turns": 30},
               # Floor 3 is verifiable and must still not be paid.
               {"floor": 3, "seed": 1, "stat": dict(STRONG_WINS),
                "actions": list(STRIKES), "won": True, "turns": 30}]}
    proof = tr.verify_depth(run)
    assert proof["records"] == 3
    assert proof["claimed_depth"] == 3
    assert proof["verified_depth"] == 1, "verification skipped past the bad floor"
    assert proof["mismatched"][0]["floor"] == 2, proof["mismatched"]


def test_the_settle_summary_reports_the_verified_depth_not_the_claimed_one(rig):
    """Route level, not helper level. The payout is keyed on ``verified_depth``
    and so is correct on its own; what this pins is the SUMMARY a panel renders.

    ``floors_cleared`` is overwritten with the verified depth precisely so a
    client cannot show a player 90 floors they did not earn, while
    ``claimed_floors_cleared`` keeps the raw number for the finding. Driven
    through HTTP with a run record whose counter was raised by hand - the only
    place this can be observed, because the two depths are equal in every
    honest run.
    """
    pid = rig["seed"]("forged")
    stat = tr.player_stat_line(rig["players"][pid])
    run = reproducible_run("RF", stat)
    run["depth"] = 90
    row = rig["players"][pid]
    row[tr.SPIRE_KEY] = tr.normalize_spire_state(
        {"settled_runs": [], "last_settled": None, "active_run": run})
    rig["players"].persist(pid, row)

    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": "forge-1"})
    assert r.status_code == 200, body_of(r)
    summary = body_of(r)["summary"]
    proof = summary["reward"]["proof"]

    assert proof["claimed_depth"] == 90, "the claim was not reported"
    assert proof["verified_depth"] == 1, "verification followed the claim"
    assert proof["mismatched"], "a forged counter produced no finding"
    # THE contract: the rendered number is the verified one.
    assert summary["floors_cleared"] == 1, "the summary showed the forged depth"
    assert summary["claimed_floors_cleared"] == 90, "the raw claim was lost"
    # And the money matches the verified depth, read from the shipped table.
    assert summary["reward"]["granted"]["stone"] == cfg.rewards_for_run(
        1, runs_today=1)["stone"]


def test_an_honest_reproducible_loss_ends_a_run_without_being_a_finding(rig):
    """A recorded loss that REPRODUCES as a loss is the end of a run, not a
    forgery. Reporting it as one puts a phantom mismatch in every single run's
    response, which is how a real finding stops being read.

    The two halves are adjacent on purpose: this must not report, and the test
    above must. Together they show the guard is discriminating rather than just
    refusing.
    """
    run = {"run_id": "RL", "floor": 2, "depth": 1, "started_at": 1.0,
           "ended_at": None, "outcome": None, "fight": None,
           "floors": [
               {"floor": 1, "seed": 1, "stat": dict(STRONG_WINS),
                "actions": list(STRIKES), "won": True, "turns": 30},
               {"floor": 2, "seed": 1, "stat": dict(WEAK_LOSES),
                "actions": list(STRIKES), "won": False, "turns": 30}]}
    proof = tr.verify_depth(run)
    assert proof["verified_depth"] == 1, "the honest win before it was dropped"
    assert proof["mismatched"] == [], "an honest loss became a finding"


def test_a_claimed_depth_above_the_reproducible_records_is_reported(rig):
    """The counter is a currency input, so a counter that disagrees with its own
    record list is a finding even when every record reproduces.

    Pinned because the payout is keyed on ``verified_depth`` and is therefore
    already correct here: what is being tested is that the DISAGREEMENT is
    visible rather than inferred from a smaller payout.
    """
    run = {"run_id": "RC", "floor": 2, "depth": 90, "started_at": 1.0,
           "ended_at": None, "outcome": None, "fight": None,
           "floors": [{"floor": 1, "seed": 1, "stat": dict(STRONG_WINS),
                       "actions": list(STRIKES), "won": True, "turns": 30}]}
    proof = tr.verify_depth(run)
    assert proof["claimed_depth"] == 90
    assert proof["verified_depth"] == 1, "the payout followed the claim"
    assert len(proof["mismatched"]) == 1
    finding = proof["mismatched"][0]
    assert finding["index"] is None, "a counter mismatch is not tied to a record"
    assert "90" in finding["reason"] and "1" in finding["reason"]


# =============================================== the seed is derived, not chosen


def test_the_fight_seed_is_derived_from_the_run_not_the_request(rig):
    """``combat/start`` takes no body and returns no seed.

    Two runs by the SAME player on the SAME floor get different seeds (the run
    id is in the digest), and the digest is stable, so the settle-time
    re-simulation still reproduces it. Both halves matter: a per-process seed
    would break re-derivation after a restart.
    """
    import hashlib

    def digest_of(run_id, floor):
        """The digest the module documents, recomputed independently."""
        return int(hashlib.sha256(
            f"{pid}|{run_id}|{floor}".encode("utf-8")).hexdigest()[:8],
            16) % (2 ** 31)

    pid = rig["seed"]("seeds")
    h = rig["hdr"](pid)
    c = rig["client"]

    observed = []
    for attempt in range(2):
        run_id = body_of(c.post("/tower/spire/enter", headers=h))["run_id"]
        started = body_of(c.post("/tower/spire/combat/start", headers=h))
        # Read the seed off the STORED fight: combat/end clears it, so reading
        # after the close measures nothing.
        fight = rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]
        assert fight["floor"] == started["floor"]
        assert "seed" not in started, "the seed was handed back to the caller"
        observed.append((run_id, fight["floor"], int(fight["seed"])))

        play_one_floor(rig, pid)
        c.post("/tower/spire/settle", headers={**h, "Idempotency-Key": f"s-{attempt}"},
               json=NEUTRAL_BODY)

    assert observed[0][0] != observed[1][0], "the run id repeated"
    assert observed[0][2] != observed[1][2], "the seed is not derived from the run"
    # Reproducible from values the server holds - which is what makes the
    # settle-time re-simulation exact rather than approximate.
    for run_id, floor, got in observed:
        assert got == digest_of(run_id, floor), (run_id, floor, got)


def test_combat_start_takes_no_body_and_returns_no_seed(rig):
    """Both directions of the same rule: the caller cannot supply a seed and
    cannot read one back."""
    pid = rig["seed"]("nos")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    r = c.post("/tower/spire/combat/start", headers=h,
               json={"seed": 12345, "floor": 99})
    assert r.status_code == 200, body_of(r)
    body = body_of(r)
    assert "seed" not in body, "the fight seed was handed back"
    assert body["floor"] == 1, "the body chose a floor"
    assert body["current_floor"] == 1


def test_the_action_list_advances_one_turn_per_call(rig):
    """``combat/action`` is ONE turn, never a whole fight. A client batching ten
    calls advances ten turns and no more, so the turn cap is the client's to
    respect."""
    pid = rig["seed"]("turns")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    seen = []
    for _ in range(3):
        body = body_of(c.post("/tower/spire/combat/action", headers=h,
                              json={"action": "strike"}))
        seen.append(body["turn"])
        if body["resolved"]:
            break
    assert seen == list(range(1, len(seen) + 1)), "turn numbers skipped"
    assert rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]["turn"] == seen[-1]


def test_a_resolved_fight_refuses_further_turns(rig):
    """Once a fight is decided it is closed to more turns. Otherwise a client
    could keep hitting a dead enemy for damage."""
    pid = rig["seed"]("resolved")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    last = None
    for _ in range(cfg.TOWER_RUN_MAX_TURNS + 5):
        last = body_of(c.post("/tower/spire/combat/action", headers=h,
                              json={"action": "strike"}))
        if last.get("resolved"):
            break
    assert last["resolved"] is True
    after = c.post("/tower/spire/combat/action", headers=h,
                   json={"action": "strike"})
    assert after.status_code == 400, body_of(after)
    assert "resolved" in body_of(after)["detail"]


def test_a_loss_does_not_advance_the_floor_pointer(rig):
    """A closed fight the player LOST must not move the run forward. Depth and
    the floor pointer are the two numbers the payout and the next fight read."""
    stat = tr.player_stat_line({"id": "z", "hp": 10, "atk": 1, "def_": 0,
                                "cultivation_realm": "mortal",
                                "cultivation_stage": "early"})
    state = tr.blank_spire_state()
    run = tr.start_run(state, now=1.0)
    fight = tr.open_fight(run, {"id": "z", "hp": 10, "atk": 1})
    while not fight["resolved"]:
        tr.take_action(run, "strike")
    closed = tr.close_fight(run)
    assert closed["won"] is False
    assert closed["depth"] == 0, "a loss banked a floor"
    assert closed["current_floor"] == 1, "a loss advanced the floor pointer"
    assert closed["run_active"] is True, "the run must survive to be settled"


def test_winning_the_final_floor_clamps_the_pointer_but_not_the_counter(rig):
    """The pointer is clamped to the top of the ladder; the COUNTER is not.

    Measured: the run sits on the final floor, wins it, and ``current_floor``
    stays at the top - good. Re-fighting and re-winning that same final floor
    keeps ADDING to ``depth``, and depth is the input to both the reward table
    and the weekly board. The skill notes call for clamping the COUNTER, which
    is the half not yet done; pinned as the measured shape so the day it lands
    is visible.
    """
    total = cfg.TOWER_RUN_TOTAL_FLOORS
    stat = tr.player_stat_line({"id": "z", "atk": 100000, "hp": 25000000,
                                "def_": 100000, "cultivation_realm": "dao_seeking",
                                "cultivation_stage": "peak"})
    enemy = cfg.enemy_stats_for_floor(total)
    top_seed = winning_seed(stat, total, ["strike"] * 60)
    assert top_seed is not None, "no reproducible seed on the final floor"

    state = tr.blank_spire_state()
    run = tr.start_run(state, now=1.0)
    run["floor"] = total
    for expected_depth in (1, 2, 3):
        run["fight"] = {"floor": total, "seed": top_seed, "stat": dict(stat),
                        "enemy": dict(enemy),
                        "hp": {"player_hp": stat["hp"], "enemy_hp": enemy["hp"]},
                        "actions": ["strike"] * 60, "log": [], "turn": 60,
                        "resolved": True}
        closed = tr.close_fight(run)
        assert closed["current_floor"] == total, "the floor pointer escaped"
        assert closed["depth"] == expected_depth, "the counter is now clamped"

    proof = tr.verify_depth(run)
    assert proof["verified_depth"] == 3
    assert proof["mismatched"] == []
    # Depth feeds the reward table, so an additive counter is a currency input.
    assert cfg.rewards_for_run(3, 1)["stone"] > cfg.rewards_for_run(1, 1)["stone"]


# ================================================ the reply envelope contract


def test_no_reply_wraps_its_payload_in_a_data_envelope(rig):
    """Every ascent reply is FLAT.

    On the client ``data`` is the transport envelope ``APIClient`` builds and the
    server's keys land inside it, so a panel reading ``body["data"]`` from a raw
    test is chasing a field the server never sends. Pinned so it is not filed a
    second time.
    """
    pid = rig["seed"]("flat")
    h = rig["hdr"](pid)
    c = rig["client"]
    replies = [body_of(c.post("/tower/spire/enter", headers=h)),
               body_of(c.get("/tower/spire/floor-info", headers=h)),
               body_of(c.get("/tower/spire/rewards", headers=h)),
               body_of(c.get("/tower/spire/history", headers=h)),
               body_of(c.get("/tower/spire/ladder", headers=h))]
    for reply in replies:
        assert reply is not None and "data" not in reply, str(sorted(reply))


def test_settle_progress_is_a_bare_number_not_an_object(rig):
    """``progress`` is a float. The other three tower shapes return a progress
    OBJECT, so a panel copied from one of them calls ``.get()`` on this and
    crashes. That is the contract."""
    pid = seed_run_state(rig, "prog")
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": "prog-1"})
    assert r.status_code == 200, body_of(r)
    body = body_of(r)
    assert set(body) == {"ok", "summary", "progress"}
    assert isinstance(body["progress"], float), "progress became an object"
    assert not isinstance(body["progress"], dict)
    assert body["progress"] >= 0.0


def test_a_settle_write_is_visible_to_the_next_request(rig):
    """The store hands out a DETACHED copy, so a write that is not persisted is
    a write nothing sees. Measured through the wire: settle, then re-read."""
    pid = seed_run_state(rig, "persist")
    before = rig["wallet"].balance(pid)
    r = rig["call"]("POST", "/tower/spire/settle", pid, json=NEUTRAL_BODY,
                    extra_headers={"Idempotency-Key": "persist-1"})
    assert r.status_code == 200, body_of(r)
    granted = body_of(r)["summary"]["reward"]["granted"]["stone"]
    assert granted > 0
    assert rig["wallet"].balance(pid) == before + granted
    # A FRESH read of the player row, not the object the route mutated.
    row = rig["players"][pid]
    assert row[tr.SPIRE_KEY]["active_run"] is None, "the run survived in the store"
    assert row[tr.SPIRE_KEY]["history"], "the run left no history entry"
    history = body_of(rig["call"]("GET", "/tower/spire/history", pid))["history"]
    assert len(history) == 1
    assert set(history[0]) == {"run_id", "depth", "claimed_depth", "outcome",
                               "stone", "qi", "ended_at"}


def test_replay_needs_a_run_id_and_answers_404_for_an_unknown_one(rig):
    """``run_id`` is a required query parameter, so omitting it is a 422 while
    naming one that is not yours is a 404.

    404 rather than 403 so the route is not an oracle for guessing other
    players' run ids - the row can only ever hold the caller's own runs.
    """
    pid = rig["seed"]("replayargs")
    h = rig["hdr"](pid)
    c = rig["client"]
    assert c.get("/tower/spire/replay", headers=h).status_code == 422
    assert c.get("/tower/spire/replay?run_id=", headers=h).status_code == 404
    assert c.get("/tower/spire/replay?run_id=nope", headers=h).status_code == 404

    run_id = body_of(c.post("/tower/spire/enter", headers=h))["run_id"]
    ok = c.get("/tower/spire/replay", headers=h, params={"run_id": run_id})
    assert ok.status_code == 200
    assert set(body_of(ok)) == {"ok", "run_id", "found", "started_at",
                                "ended_at", "floors", "total_floors"}
    assert body_of(ok)["found"] is True
    assert body_of(ok)["started_at"] is not None
    assert body_of(ok)["ended_at"] is None, "an open run reported an end time"


def test_the_parties_routes_are_reachable_and_refuse_on_membership(rig):
    """All three answer; each refusal is a 400 naming the reason, so a panel can
    tell "you have no party" from "that invite has expired"."""
    pid = rig["seed"]("partyrefs")
    h = rig["hdr"](pid)
    c = rig["client"]
    invite = c.post("/tower/spire/party/invite", headers=h,
                    json={"invitee_id": "somebody"})
    assert invite.status_code == 400
    assert body_of(invite)["detail"] == "You are not in a party"

    accept = c.post("/tower/spire/party/accept", headers=h)
    assert accept.status_code == 400
    assert body_of(accept)["detail"] == "No pending invite"

    leave = c.post("/tower/spire/party/leave", headers=h)
    assert leave.status_code == 400
    assert body_of(leave)["detail"] == "You are not in a party"

    # accept and leave take NO body: naming a party in one is not a parameter
    # they expose, and the pending invite on the caller's row is the authority.
    for name in ("SpireAcceptRequest",):
        assert getattr(tr, name).model_fields == {}


def test_no_route_answers_500_to_a_malformed_or_hostile_call(rig):
    """Across the whole surface, one bad request never produces a server error.

    A 500 is the one status a client cannot act on and the one that hides a
    defect, so it is asserted against the whole surface rather than per route.
    Every call below is deliberately wrong in some way: a missing player, a
    missing key, a missing body, a blank body field.
    """
    import auth

    ghost = {"Authorization": "Bearer " + auth.issue_session("c_ghost"),
             "Idempotency-Key": "k"}
    live = rig["seed"]("fivehundred")
    mine = rig["hdr"](live)
    probes = [
        ("GET", "/tower/spire/floor-info", ghost, None),
        ("GET", "/tower/spire/history", mine, None),
        ("GET", "/tower/spire/rewards", mine, None),
        ("GET", "/tower/spire/ladder?limit=abc", mine, None),
        ("GET", "/tower/spire/replay", mine, None),
        ("GET", "/tower/spire/replay?run_id=missing", mine, None),
        ("POST", "/tower/spire/enter", mine, {}),
        ("POST", "/tower/spire/combat/start", mine, {}),
        ("POST", "/tower/spire/combat/action", mine, {"action": 5}),
        ("POST", "/tower/spire/combat/action", mine, {"action": "nonsense"}),
        ("POST", "/tower/spire/combat/end", mine, {}),
        ("POST", "/tower/spire/settle", mine, NEUTRAL_BODY),
        ("POST", "/tower/spire/party/invite", mine, {"invitee_id": ""}),
        ("POST", "/tower/spire/party/invite", mine, {"invitee_id": "someone"}),
        ("POST", "/tower/spire/party/accept", mine, {}),
        ("POST", "/tower/spire/party/leave", mine, {}),
    ]
    for method, path, headers, payload in probes:
        r = rig["client"].request(method, path, headers=headers, json=payload)
        assert r.status_code != 500, path + " answered 500: " + str(body_of(r))


# ============================ the refusal vocabulary of the run lifecycle
#
# The sibling file pins the HAPPY-path key sets and the money guards. What is
# pinned here is the other half of the wire: for every lifecycle step, the
# exact status code and the exact refusal sentence. These are the strings a
# panel branches on, so a reworded message is a breaking change and must fail
# a test rather than silently change what a client shows.
#
# Every expectation below was MEASURED by driving the shipped module, not read
# off the source: the status codes are 400 with the quoted ``detail``, except
# ``combat/start`` with no run open, which is a 404 (no run to act on is a
# missing resource) and ``settle`` with no run, which is a 400 because the
# idempotency key gate and the run rule both fire on the same route.


def test_entering_a_second_run_is_refused_while_one_is_open(rig):
    """``enter`` twice is a 400 naming the reason, not a second run.

    Starting a second run would orphan the first, and the first is what the
    player is looking at.
    """
    pid = rig["seed"]("twice")
    h = rig["hdr"](pid)
    c = rig["client"]
    first = body_of(c.post("/tower/spire/enter", headers=h))
    assert first["run_id"]
    again = c.post("/tower/spire/enter", headers=h)
    assert again.status_code == 400, body_of(again)
    assert body_of(again)["detail"] == "A run is already in progress"
    # The first run is untouched: not replaced, not re-numbered.
    stored = rig["players"][pid][tr.SPIRE_KEY]["active_run"]
    assert stored["run_id"] == first["run_id"], "the second enter replaced the run"
    assert stored["floor"] == 1 and stored["depth"] == 0


def test_opening_a_second_fight_is_refused_until_the_first_is_closed(rig):
    """``combat/start`` is once-per-run, and the refusal says which step to take.

    Without this, a second start would silently mint a second seed for the same
    floor and orphan the action list the first fight was recording into.
    """
    pid = rig["seed"]("twofights")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    first = c.post("/tower/spire/combat/start", headers=h)
    assert first.status_code == 200, body_of(first)
    seed_before = rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]["seed"]

    again = c.post("/tower/spire/combat/start", headers=h)
    assert again.status_code == 400, body_of(again)
    assert body_of(again)["detail"] == "A fight is already open; end it first"
    # The open fight kept ITS seed and its untouched turn counter.
    fight = rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]
    assert int(fight["seed"]) == int(seed_before), "the open fight was replaced"
    assert fight["turn"] == 0


def test_closing_an_unfinished_fight_is_refused(rig):
    """The load-bearing refusal: ``combat/end`` cannot bank an unfinished floor.

    A caller who could close early would declare a win for a fight they had not
    won, and the floor pointer would advance on that declaration.
    """
    # A WEAK player, deliberately: the strong player the rig seeds by default
    # one-shots floor 1, so its fight is RESOLVED after one turn and there would
    # be nothing to close early. Asserted below, so the fixture cannot silently
    # stop exercising this refusal if the floor curve is rebalanced upward.
    pid = rig["seed"]("unfinished", atk=1, hp=50, def_=0)
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    opened = body_of(c.post("/tower/spire/combat/action", headers=h,
                            json={"action": "strike"}))
    assert opened["resolved"] is False, (
        "the weak fixture resolved in one turn; the refusal is unmeasured")

    early = c.post("/tower/spire/combat/end", headers=h)
    assert early.status_code == 400, body_of(early)
    assert body_of(early)["detail"] == (
        "The fight is not over; keep taking actions or let the turn cap finish it")
    # And the refusal banked nothing.
    run = rig["players"][pid][tr.SPIRE_KEY]["active_run"]
    assert run["depth"] == 0, "an unfinished fight banked a floor"
    assert run["floors"] == [], "an unfinished fight wrote a floor record"
    assert run["fight"] is not None, "the refused close dropped the open fight"


def test_closing_a_fight_that_was_never_opened_is_refused(rig):
    """``combat/end`` with no fight is a 400 - a different sentence from the
    unfinished one, so a panel can tell "start one" from "keep playing"."""
    pid = rig["seed"]("nofight")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    r = c.post("/tower/spire/combat/end", headers=h)
    assert r.status_code == 400, body_of(r)
    assert body_of(r)["detail"] == "No fight is open"


@pytest.mark.parametrize("path,headers_factory,label", [
    ("/tower/spire/combat/start", "own", "combat/start"),
    ("/tower/spire/combat/action", "own", "combat/action"),
    ("/tower/spire/combat/end", "own", "combat/end"),
])
def test_a_combat_route_with_no_run_open_is_refused(rig, path, headers_factory,
                                                   label):
    """Every combat route needs an open run, and the refusal names it.

    Measured: all THREE answer 404, not a mix. ``own_run`` raises one
    ``HTTPException(404, "No run in progress")`` and every combat route calls
    it, so the status is uniform. Pinned as uniform because a client that
    special-cases one of them as a 400 would show the wrong message - and
    because 404 here means "you have no run", NOT "no such route".
    """
    pid = rig["seed"]("norun_" + label.replace("/", "_"))
    r = rig["call"]("POST", path, pid, json={"action": "strike"})
    assert r.status_code == 404, label + " -> " + str(body_of(r))
    assert body_of(r)["detail"] == "No run in progress"


def test_an_unknown_stance_name_is_400_and_lists_the_three_that_exist(rig):
    """A wrong TYPE is a 422 (pinned above); a wrong NAME is a 400, and the
    refusal enumerates the legal names.

    Measured on the shipped route: the stance name is echoed back inside the
    message, so the caller can see what it sent and what it should have sent.
    """
    pid = rig["seed"]("badname")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    r = c.post("/tower/spire/combat/action", headers=h, json={"action": "nonsense"})
    assert r.status_code == 400, body_of(r)
    detail = body_of(r)["detail"]
    assert detail == ("Unknown action 'nonsense'; expected one of strike, "
                      "heavy, guard"), detail
    # The turn counter did not advance: a refused stance is not a turn.
    assert rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]["turn"] == 0
    assert rig["players"][pid][tr.SPIRE_KEY]["active_run"]["fight"]["actions"] == []


def test_an_omitted_stance_defaults_to_strike_and_the_default_is_declared(rig):
    """The other half of the default: an ABSENT ``action`` is a legal request.

    ``SpireActionRequest.action`` has a default, so ``{}`` is accepted and plays
    ``strike``. The value is pinned on the model, not just the route, because a
    client building the body needs to know the default exists.
    """
    assert tr.SpireActionRequest().action == "strike"
    assert tr.SpireActionRequest(action="guard").action == "guard"

    pid = rig["seed"]("default")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    c.post("/tower/spire/combat/start", headers=h)
    r = c.post("/tower/spire/combat/action", headers=h, json={})
    assert r.status_code == 200, body_of(r)
    assert body_of(r)["action"] == "strike", "the default stance was not strike"
    assert body_of(r)["turn"] == 1


# ================================== the lifecycle reply key sets, frozen
#
# A client reads these names. The sibling file pins the happy-path key sets for
# the same routes, so these four are deliberately NOT repeated here; what is
# pinned here instead is that the key set does not GROW with state. A route that
# leaked an internal field (the fight seed above all) would break a client that
# renders every key it is given.


def test_the_combat_replies_carry_no_internal_fight_fields(rig):
    """``combat/start`` and ``combat/action`` never hand back the seed, the
    stat line, or the action list.

    The seed is the input the design removes: a caller who reads it can
    pre-compute every roll of the fight. The stat line is the frozen snapshot
    settle re-simulates against. Both are server state, so neither belongs in a
    reply. Asserted as a set difference, so a future field is caught by name.
    """
    pid = rig["seed"]("noleak")
    h = rig["hdr"](pid)
    c = rig["client"]
    c.post("/tower/spire/enter", headers=h)
    start = body_of(c.post("/tower/spire/combat/start", headers=h))
    action = body_of(c.post("/tower/spire/combat/action", headers=h,
                            json={"action": "strike"}))
    internal = {"seed", "stat", "actions_log", "log", "run_id", "idempotency_key"}
    assert not internal & set(start), sorted(internal & set(start))
    assert not internal & set(action), sorted(internal & set(action))
    # ``actions`` on combat/start is the LEGAL STANCE LIST, not the fight's
    # recorded actions - pinned because the two are easy to confuse and a
    # client rendering the wrong one leaks the action history.
    assert start["actions"] == list(tr.ACTIONS)
    assert start["actions"] != []


def test_the_entered_run_reply_names_the_floor_and_the_total(rig):
    """``enter`` opens on floor 1 and names the top of the ladder, so a panel
    can render progress without a second request."""
    pid = rig["seed"]("enterkeys")
    body = body_of(rig["call"]("POST", "/tower/spire/enter", pid))
    assert set(body) == {"ok", "run_id", "current_floor", "floors_cleared",
                         "total_floors", "best_ever", "enemy"}
    assert body["ok"] is True
    assert body["current_floor"] == 1
    assert body["floors_cleared"] == 0, "a fresh run reports cleared floors"
    assert body["total_floors"] == cfg.TOWER_RUN_TOTAL_FLOORS
    assert body["enemy"]["hp"] > 0, "the opening floor has no enemy"


# ============================== the daily run counter is keyed on the day
#
# MEASURED GAP, and the only money-shaped one this file adds. ``runs_today``
# feeds ``rewards_for_run``, so it is a payout input. The counter is reset by
# comparing the STORED day string against today's UTC day - and both the counter
# and the day are read from the caller's own player row.
#
# Two halves, pinned together because either alone is a bug:
#   * a stale day resets the counter to zero, so yesterday's runs do not
#     suppress today's payout, and
#   * the ``/tower/spire/rewards`` route derives its ``runs_today`` the same
#     way, so what a player is shown matches what they will be paid.


def test_a_stale_day_resets_the_run_counter_rather_than_carrying_it(rig):
    """A run count from yesterday must not suppress today's reward."""
    today = tr.utc_day_key()
    # A PINNED clock (the epoch), so the day the stamp is rewritten to is a
    # constant rather than whatever "today" happens to be when the suite runs.
    stale = tr.normalize_spire_state({"runs_day": "1999-01-01", "runs_today": 7})
    assert tr._bump_runs_today(stale, 0.0) == 1, "a stale counter carried over"
    assert stale["runs_day"] == "1970-01-01", "the stamp was not rewritten"
    assert stale["runs_today"] == 1

    same_day = tr.normalize_spire_state({"runs_day": tr.utc_day_key(0.0),
                                         "runs_today": 7})
    assert tr._bump_runs_today(same_day, 0.0) == 8, "a same-day bump did not count"
    assert same_day["runs_today"] == 8
    assert same_day["runs_day"] == tr.utc_day_key(0.0), "the stamp moved on a bump"

    # A day boundary RESETS, it does not accumulate: yesterday's seven are gone.
    boundary = tr.normalize_spire_state({"runs_day": tr.utc_day_key(0.0),
                                         "runs_today": 7})
    assert tr._bump_runs_today(boundary, 86400.0) == 1


def test_the_rewards_route_reads_the_same_counter_the_payout_uses(rig):
    """What the rewards screen shows and what settle pays come from one counter.

    Measured: with a STALE day the route reports ``runs_today`` 1 and a
    non-repeat reward; with the day set to today and seven runs already, it
    reports 8 and a REPEAT reward. Same rule in both directions, so a player is
    never quoted a first-run reward they will not receive, nor a repeat reward
    that is about to be restored to full.
    """
    pid = rig["seed"]("counter")

    row = rig["players"][pid]
    row[tr.SPIRE_KEY] = tr.normalize_spire_state(
        {"runs_day": "1999-01-01", "runs_today": 7})
    rig["players"].persist(pid, row)
    stale = body_of(rig["call"]("GET", "/tower/spire/rewards", pid))
    assert stale["runs_today"] == 1, "a stale day was carried into today's quote"
    assert stale["at_current_depth"]["repeat"] is False

    row = rig["players"][pid]
    row[tr.SPIRE_KEY] = tr.normalize_spire_state(
        {"runs_day": tr.utc_day_key(), "runs_today": 7})
    rig["players"].persist(pid, row)
    current = body_of(rig["call"]("GET", "/tower/spire/rewards", pid))
    assert current["runs_today"] == 8, "today's seventh run was not counted"
    assert current["at_current_depth"]["repeat"] is True

    # And the quoted reward is the shipped table's answer for that run number,
    # so the screen cannot drift from the payout.
    assert current["at_current_depth"] == cfg.rewards_for_run(
        current["current_depth"], 8)


# ============================================= the party lifecycle, end to end
#
# The sibling file pins the REFUSALS (no party, second invite, stranger
# accepting). What is missing there, and measured here, is the one path that
# actually WORKS: a leader invites, the invite lands on the INVITEE's row, the
# invitee accepts, and the membership list changes. A refusal-only suite is
# green on a module whose happy path is broken.


def _party_of(rig, leader):
    """Put the caller in a party through the DUNGEON surface.

    The tower routes read party membership from the dungeon manager rather than
    minting a party of their own, so a party created here is the same party the
    routes will see. Building it any other way would test a party nothing reads.
    """
    from server.dungeons import get_dungeon_manager
    return get_dungeon_manager().create_party(leader)


def test_the_whole_party_round_trip_works(rig):
    """invite -> the INVITEE's row -> accept -> membership, verified on a fresh
    read of both rows at each step."""
    lead = rig["seed"]("lead")
    mate = rig["seed"]("mate")
    party = _party_of(rig, lead)

    sent = rig["call"]("POST", "/tower/spire/party/invite", lead,
                       json={"invitee_id": mate})
    assert sent.status_code == 200, body_of(sent)
    body = body_of(sent)
    assert set(body) == {"ok", "party_id", "invitee_id", "pending"}
    assert body["party_id"] == party.id
    assert body["invitee_id"] == mate, "the reply echoed a different invitee"
    assert body["pending"] == 1

    # A FRESH read of the invitee's row, not the object the route held. In
    # memory mode the store hands out a detached copy, so a write that was not
    # persisted is invisible here - which is exactly the bug this read catches.
    pending = rig["players"][mate][tr.SPIRE_KEY]["party_invites"]
    assert len(pending) == 1, "the invite did not land on the invitee's row"
    assert pending[0]["party_id"] == party.id
    assert pending[0]["from"] == lead
    # And the LEADER's row carries no invite: the invite belongs to the target.
    # And the LEADER's row carries no invite: the invite belongs to the target.
    # Read through ``normalize_spire_state`` because the leader never opened a
    # run, so its row has no ascent block yet - which is itself correct.
    assert tr.normalize_spire_state(
        rig["players"][lead].get(tr.SPIRE_KEY))["party_invites"] == []

    joined = rig["call"]("POST", "/tower/spire/party/accept", mate)
    assert joined.status_code == 200, body_of(joined)
    body = body_of(joined)
    assert set(body) == {"ok", "party_id", "leader_id", "members", "pending"}
    assert body["members"] == sorted([lead, mate]), body["members"]
    assert body["leader_id"] == lead, "acceptance changed who leads"
    # The invite is CONSUMED: accepting twice must not be possible.
    assert body["pending"] == 0
    assert rig["players"][mate][tr.SPIRE_KEY]["party_invites"] == []
    assert rig["call"]("POST", "/tower/spire/party/accept",
                       mate).status_code == 400


def test_leaving_removes_the_caller_from_the_membership_list(rig):
    """``leave`` is the caller's own and only: it names no party and no other
    player, and it reports the list as it stands AFTER the departure."""
    lead = rig["seed"]("lead2")
    mate = rig["seed"]("mate2")
    party = _party_of(rig, lead)
    rig["call"]("POST", "/tower/spire/party/invite", lead,
                json={"invitee_id": mate})
    rig["call"]("POST", "/tower/spire/party/accept", mate)

    left = rig["call"]("POST", "/tower/spire/party/leave", mate)
    assert left.status_code == 200, body_of(left)
    body = body_of(left)
    assert set(body) == {"ok", "left_party_id", "members"}
    assert body["left_party_id"] == party.id
    assert mate not in body["members"], "the leaver is still listed"
    assert lead in body["members"], "the leader left with the leaver"
    # Leaving twice is a 400, not a silent success.
    assert rig["call"]("POST", "/tower/spire/party/leave",
                       mate).status_code == 400


@pytest.mark.parametrize("payload,status,detail,label", [
    ({"invitee_id": ""}, 422, None, "empty invitee is a schema error"),
])
def test_party_invite_schema_and_membership_errors_are_distinct(
        rig, payload, status, detail, label):
    """A schema error and a membership error are different classes of failure.

    A 422 means "the request is malformed" and the client should fix its JSON; a
    400 means "the request is well formed but you may not do this". A client
    that treats them alike retries forever. Pinned so the split cannot blur.
    """
    pid = rig["seed"]("invsplit")
    r = rig["call"]("POST", "/tower/spire/party/invite", pid, json=payload)
    assert r.status_code == status, label + " -> " + str(body_of(r))
    if detail is None:
        assert body_of(r)["detail"][0]["loc"] == ["body", "invitee_id"]


def test_inviting_yourself_is_refused_as_an_invalid_invitee(rig):
    """A self-invite would be accepted by the "not already a member" rule - the
    caller IS a member - so it needs its own refusal."""
    lead = rig["seed"]("solo_lead")
    _party_of(rig, lead)
    r = rig["call"]("POST", "/tower/spire/party/invite", lead,
                    json={"invitee_id": lead})
    assert r.status_code == 400, body_of(r)
    assert body_of(r)["detail"] == "Invalid invitee"


def test_inviting_a_player_who_does_not_exist_is_404(rig):
    """A well-formed invitee that names no row is a missing resource (404), so
    a panel can tell "no such player" from "you may not invite them" (400)."""
    lead = rig["seed"]("ghostlead")
    _party_of(rig, lead)
    r = rig["call"]("POST", "/tower/spire/party/invite", lead,
                    json={"invitee_id": "c_definitely_not_a_player"})
    assert r.status_code == 404, body_of(r)
    assert body_of(r)["detail"] == "Invitee not found"


def test_accepting_an_invite_to_a_party_that_no_longer_exists_consumes_it(rig):
    """The stale-invite path: a 400 that ALSO clears the dead invite.

    Otherwise the caller is stuck re-accepting the same dead invite on every
    request, because the pending list is always the same first entry and the
    refusal never advances it. Measured: after the refusal the list is empty and
    a second accept says "No pending invite".
    """
    solo = rig["seed"]("staletarget")
    row = rig["players"][solo]
    row[tr.SPIRE_KEY] = tr.normalize_spire_state(
        {"party_invites": [{"party_id": "party_does_not_exist",
                            "from": "c_somebody", "at": 1.0}]})
    rig["players"].persist(solo, row)

    first = rig["call"]("POST", "/tower/spire/party/accept", solo)
    assert first.status_code == 400, body_of(first)
    assert body_of(first)["detail"] == "That party no longer exists"
    assert rig["players"][solo][tr.SPIRE_KEY]["party_invites"] == [], (
        "the dead invite was not consumed; the caller is stuck on it")

    second = rig["call"]("POST", "/tower/spire/party/accept", solo)
    assert second.status_code == 400, body_of(second)
    assert body_of(second)["detail"] == "No pending invite"