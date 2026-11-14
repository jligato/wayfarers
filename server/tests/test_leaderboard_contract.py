"""
CONTRACT TEST: server/leaderboard_payout.py -- the two leaderboard HTTP routes.

WHAT THIS FILE PINS, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
The named module ``server/leaderboard.py`` does not exist. The leaderboard HTTP
surface is three modules, and they are tested by role:

  * ``leaderboard_rewards.py``            -- pure calculator, no HTTP
  * ``leaderboard_rewards_routes.py``    -- GET  /leaderboard/reward-table
  * ``leaderboard_payout.py``            -- GET  /leaderboard/{player_id}/payout-status
                                            POST /leaderboard/{player_id}/claim-payout

The reward-table route already has a full contract file
(``test_leaderboard_rewards_routes_contract.py``). The two payout routes have
three behaviour files -- ``test_leaderboard_payout.py``,
``test_leaderboard_payout_authz.py`` and
``test_leaderboard_payout_amount_authority.py`` -- which are thorough about
BEHAVIOUR: pays once, refuses twice, never lets the caller price the band.

What those files do not do is pin the WIRE CONTRACT: the exact key set of a 200,
which of those keys are echoed caller values, the shape of a refusal body, the
verbs each path answers, and whether the shape survives the real app. Those are
the things a client reads, and a client cannot tell "renamed" from "still
there" on its own. So this file pins them and leaves behaviour alone.

So: exact key sets (paths, keys, statuses) are frozen, because that IS the
contract. Numbers are asserted as RELATIONS to the server's own reward table,
so a balance retune does not need this file re-baselined.

THE AUTH ARMS ARE PINS, NOT DUPLICATES
---------------------------------------
401-anonymous and 403-cross-player are already covered elsewhere. They are
repeated here in ONE combined arm, for one reason: this file's other arms are
about response shape, and shape is only observable for a request that is not
refused. The refusals are what stop a shape assertion from being measured on a
404. Each is asserted to move NO balance, which is the part that matters.

WHY THE READ ROUTE IS PINNED AS A NON-WRITER
--------------------------------------------
``leaderboard_payout.read_state`` exists because the status route used to call
the repairing getter, so every status poll ATTACHED a fresh
``leaderboard_payout`` key to the player's row. On a copy-on-read store (the
real one) that write vanished silently; on the in-memory store it landed. Same
request, two different outcomes. The status route must not persist anything, and
this file proves that from the outside by asserting the store was never written.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    <project-python> -m pytest server/tests/test_leaderboard_contract.py -q

No database: the store and wallet are in-memory fakes with the real store's two
load-bearing semantics (copy-on-read, real transaction lock).
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import threading
from copy import deepcopy

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import leaderboard_payout as LP  # noqa: E402
import leaderboard_rewards as pure  # noqa: E402

STATUS_PATH = "/leaderboard/{player_id}/payout-status"
CLAIM_PATH = "/leaderboard/{player_id}/claim-payout"

#: Exact key set of a 200 from the claim route. Frozen deliberately: a client
#: reading a key is entitled to an exact-set test so a RENAME is distinguishable
#: from a value that merely changed. ``wallet`` is present only when the route
#: actually paid, which is its own arm below.
CLAIM_KEYS = frozenset({
    "ok", "already_paid", "season", "rank", "band", "reward",
    "base_amount", "currency", "reason",
    "requested_rank", "requested_base_amount",
})
CLAIM_PAID_KEYS = CLAIM_KEYS | {"wallet"}

#: Exact key set of a 200 from the status route.
STATUS_KEYS = frozenset({
    "ok", "season", "rank", "band", "already_paid", "settling",
    "paid_seasons", "identity_player_id",
})

#: The reasons the claim route can report. Every one is a refusal of payment or
#: an acknowledgement of one, and a client branches on this string.
CLAIM_REASONS = frozenset({
    "paid", "already_paid_this_season", "season_settling",
    "no_server_ranked_standing", "rank_outside_paying_bands",
})


class _FakeStore(dict):
    """Player store with the real store's two load-bearing semantics.

    1. ``__getitem__`` returns a DETACHED COPY; only ``persist`` writes back.
    2. ``transaction`` really serialises writers.
    """

    def __init__(self):
        super().__init__()
        self._locks = {}
        self.persist_calls = []

    def __getitem__(self, player_id):
        return deepcopy(dict.__getitem__(self, player_id))

    def persist(self, player_id, player):
        self.persist_calls.append(player_id)
        dict.__setitem__(self, player_id, deepcopy(dict(player)))

    def transaction(self, player_id=None):
        lock = self._locks.setdefault(player_id, threading.Lock())
        lock.acquire()

        class _Ctx:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                lock.release()

        return _Ctx()


def _balance(client, player_id: str) -> float:
    """A player's wallet balance, or 0.0 when they have no wallet row yet.

    The wallet returns ``None`` for a player it has never paid, which is the
    normal state before a claim, so a plain attribute read would crash on
    exactly the arms that prove nothing moved.
    """
    wallet = client.wc_wallet.get(player_id)
    if wallet is None:
        return 0.0
    return float(getattr(wallet.balance, LP.PAYOUT_CURRENCY, 0))


def _sign_in(pid: str) -> dict:
    """A real session token, minted by the server's own auth module.

    This file never hand-rolls a header the server would not accept.
    """
    import auth as auth_mod

    return {"Authorization": f"Bearer {auth_mod.issue_session(pid)}"}


def _record_standing(store: _FakeStore, pid: str, rank: int, season=None) -> str:
    """Write the rank the SERVER assigned, through the settlement helper.

    ``record_standing`` has no HTTP route of its own, which is precisely why a
    caller cannot reach it -- so every test that wants a payout has to call it
    directly rather than putting the rank in the request body.
    """
    season = season or LP.season_id()
    with store.transaction(pid):
        player = store[pid]
        LP.record_standing(player, season, rank)
        store.persist(pid, player)
    return season


def _build(standing=None, players=("p1",)):
    from wallet_defense import WalletStore

    wallet = WalletStore()
    store = _FakeStore()
    for pid in players:
        store[pid] = {"id": pid, "name": f"Player {pid}", "wallet": {}}
    app = FastAPI()
    # Mounted through the module's own registrar, so the registrar is exercised.
    LP.register_leaderboard_payout_routes(app, lambda: store, lambda: wallet)
    client = TestClient(app, raise_server_exceptions=False)
    client.wc_store = store
    client.wc_wallet = wallet
    for pid, rank in (standing or {}).items():
        _record_standing(store, pid, rank)
    return client


@pytest.fixture
def ranked_client():
    """A client whose ``p1`` holds a server-recorded top-band standing."""
    return _build(standing={"p1": 1})


def _claim(client, pid="p1", rank=1, base_amount=100.0, **kw):
    body = {"rank": rank, "base_amount": base_amount}
    headers = kw.pop("headers", None) or _sign_in(pid)
    return client.post(f"/leaderboard/{pid}/claim-payout", json=body,
                       headers=headers, **kw)


def _status(client, pid="p1", **kw):
    headers = kw.pop("headers", None) or _sign_in(pid)
    return client.get(f"/leaderboard/{pid}/payout-status", headers=headers, **kw)


# ===========================================================================
# 1. WHAT MOUNTS
# ===========================================================================

def test_the_registrar_mounts_exactly_the_two_known_paths():
    app = FastAPI()
    LP.register_leaderboard_payout_routes(app, lambda: _FakeStore(),
                                          lambda: None)
    mounted = sorted(r.path for r in app.routes if isinstance(r, APIRoute))
    assert mounted == sorted([STATUS_PATH, CLAIM_PATH]), "mounted %r" % (mounted,)


def test_each_path_answers_exactly_one_verb():
    """Status is a read, claim is a write. Anything else is surface to review."""
    app = FastAPI()
    LP.register_leaderboard_payout_routes(app, lambda: _FakeStore(),
                                          lambda: None)
    verbs = {r.path: sorted(r.methods) for r in app.routes if isinstance(r, APIRoute)}
    assert verbs[STATUS_PATH] == ["GET"], verbs[STATUS_PATH]
    assert verbs[CLAIM_PATH] == ["POST"], verbs[CLAIM_PATH]


def test_the_wrong_verb_is_405_with_the_right_allow_header(ranked_client):
    c = ranked_client
    r = c.get("/leaderboard/p1/claim-payout", headers=_sign_in("p1"))
    assert r.status_code == 405, r.status_code
    allow = {m.strip() for m in r.headers.get("allow", "").split(",") if m.strip()}
    assert allow == {"POST"}, allow

    s = c.post("/leaderboard/p1/payout-status", json={}, headers=_sign_in("p1"))
    assert s.status_code == 405, s.status_code
    allow = {m.strip() for m in s.headers.get("allow", "").split(",") if m.strip()}
    assert allow == {"GET"}, allow


def test_no_settlement_route_is_exposed_here():
    """Recording a standing has no HTTP route in this module, by design.

    If a route that writes a season's standings ever appears beside these two,
    the caller gets a path to choose their own band -- which is the defect the
    module docstring says was measured and fixed.
    """
    c = _build(standing={"p1": 1})
    for path in ("/leaderboard/p1/settle", "/leaderboard/p1/standing",
                 "/leaderboard/settle", "/leaderboard/p1/rank"):
        for verb, kw in (("post", {"json": {"rank": 1}}),
                         ("get", {})):
            r = getattr(c, verb)(path, headers=_sign_in("p1"), **kw)
            assert r.status_code in (404, 405), "%s %s answered %d" % (
                verb.upper(), path, r.status_code)


# ===========================================================================
# 2. AUTH: both routes need a session and their own player
# ===========================================================================

def test_anonymous_is_refused_by_both_routes_and_moves_no_money():
    """One combined arm, because shape is only observable for a request that
    is NOT refused -- and the refusal moving no balance is the part that
    matters."""
    c = _build(standing={"p1": 1})
    s = c.get("/leaderboard/p1/payout-status")
    assert s.status_code == 401, "status answered %d: %s" % (s.status_code, s.text[:200])
    k = c.post("/leaderboard/p1/claim-payout", json={"rank": 1})
    assert k.status_code == 401, "claim answered %d: %s" % (k.status_code, k.text[:200])
    assert _balance(c, "p1") == 0.0, _balance(c, "p1")


def test_a_session_for_another_player_is_403_on_both_routes():
    c = _build(standing={"p1": 1, "p2": 1}, players=("p1", "p2"))
    s = c.get("/leaderboard/p1/payout-status", headers=_sign_in("p2"))
    assert s.status_code == 403, "status answered %d" % s.status_code
    k = c.post("/leaderboard/p1/claim-payout", json={"rank": 1},
               headers=_sign_in("p2"))
    assert k.status_code == 403, "claim answered %d" % k.status_code
    # The victim must still be able to claim their own money afterwards.
    ok = c.post("/leaderboard/p1/claim-payout", json={"rank": 1},
                headers=_sign_in("p1"))
    assert ok.status_code == 200, ok.text[:200]
    assert ok.json()["reason"] == "paid", ok.json()["reason"]


def test_a_refusal_body_carries_detail_and_nothing_else():
    """A client branching on the status must not also find half a table."""
    c = _build(standing={"p1": 1})
    for r in (c.get("/leaderboard/p1/payout-status"),
              c.post("/leaderboard/p1/claim-payout", json={"rank": 1})):
        body = r.json()
        assert r.status_code == 401, r.status_code
        assert set(body) == {"detail"}, sorted(body)
        assert isinstance(body["detail"], str) and body["detail"], body


def test_an_unknown_player_id_is_404_from_both_routes():
    c = _build(standing={"p1": 1})
    s = _status(c, "nobody")
    assert s.status_code == 404, s.status_code
    k = _claim(c, "nobody")
    assert k.status_code == 404, k.status_code
    assert set(s.json()) == {"detail"} and set(k.json()) == {"detail"}


def test_the_auth_dependency_is_declared_on_both_routes():
    """Structural: a dependency named after the session gate must be present,
    so a future edit that drops it fails HERE even if a test fixture happens to
    be signed in."""
    from auth import require_player

    app = FastAPI()
    LP.register_leaderboard_payout_routes(app, lambda: _FakeStore(),
                                          lambda: None)
    for path in (STATUS_PATH, CLAIM_PATH):
        route = next(r for r in app.routes
                     if isinstance(r, APIRoute) and r.path == path)
        calls = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
        assert require_player.__name__ in calls, (
            "%s carries dependencies %r -- the session gate is gone" % (path, calls))


# ===========================================================================
# 3. THE CLAIM RESPONSE SHAPE
# ===========================================================================

def test_a_paid_claim_answers_the_exact_key_set(ranked_client):
    body = _claim(ranked_client).json()
    assert set(body) == CLAIM_PAID_KEYS, sorted(body)
    assert body["ok"] is True, body["ok"]


def test_a_refusing_claim_omits_the_wallet_key(ranked_client):
    """``wallet`` appears only when money actually moved.

    A client checking ``if body.get("wallet")`` is fine either way, but a client
    that assumes the key exists breaks on every refusal.
    """
    body = _claim(ranked_client).json()
    assert body["reason"] == "paid", body["reason"]
    again = _claim(ranked_client).json()
    assert again["reason"] == "already_paid_this_season", again["reason"]
    assert "wallet" not in again, sorted(again)


def test_the_season_label_is_a_utc_month_string(ranked_client):
    """Clients key a "claimed" tick by season, so the shape of the label is the
    contract: ``YYYY-MM``."""
    for r in (_claim(ranked_client), _status(ranked_client)):
        season = r.json()["season"]
        assert isinstance(season, str) and len(season) == 7, season
        year, _, month = season.partition("-")
        assert year.isdigit() and month.isdigit(), season
        assert 1 <= int(month) <= 12, season


def test_the_reason_is_one_of_the_documented_strings(ranked_client):
    """A client branches on this exact string, so the vocabulary is pinned."""
    body = _claim(ranked_client).json()
    assert body["reason"] in CLAIM_REASONS, body["reason"]
    again = _claim(ranked_client).json()
    assert again["reason"] in CLAIM_REASONS, again["reason"]


def test_the_band_is_a_real_string_and_the_reward_a_number(ranked_client):
    """``band`` is compared for equality by the client, so a truthy non-string
    would render the wrong row."""
    body = _claim(ranked_client).json()
    assert isinstance(body["band"], str) and body["band"], repr(body["band"])
    assert isinstance(body["reward"], int), repr(body["reward"])
    assert isinstance(body["already_paid"], bool), repr(body["already_paid"])
    assert body["currency"] == LP.PAYOUT_CURRENCY, body["currency"]


def test_the_reward_is_what_the_server_table_prices_for_the_recorded_rank():
    """A RELATION, not a frozen number: retuning the reward table keeps this
    honest."""
    for rank in (1, 7, 60, 199):
        c = _build(standing={"p1": rank})
        body = _claim(c).json()
        base = LP.server_base_amount(LP.PAYOUT_CURRENCY)
        expected = pure.reward_for_rank_int(rank, base) if rank >= 1 else 0
        assert body["rank"] == rank, body["rank"]
        assert body["reward"] == expected, (
            "rank %d: served %r, server table prices %r"
            % (rank, body["reward"], expected))
        if expected > 0:
            assert body["band"] == pure.band_for_rank(rank), body["band"]


def test_the_response_echoes_both_caller_values_verbatim():
    """``requested_rank`` / ``requested_base_amount`` are how a client PROVES
    it was overridden, so the echo must be exact, not normalised."""
    c = _build(standing={"p1": 1})
    body = _claim(c, rank=199, base_amount=99999.0).json()
    assert body["requested_rank"] == 199, body["requested_rank"]
    assert body["requested_base_amount"] == 99999.0, body["requested_base_amount"]
    # And the echo must not have become the amount paid.
    assert body["base_amount"] == LP.server_base_amount(LP.PAYOUT_CURRENCY), (
        "base_amount is %r -- it must be the SERVER's value"
        % (body["base_amount"],))


def test_the_wallet_receipt_is_nested_under_wallet_on_a_paid_claim(ranked_client):
    """The receipt is passed through from the wallet layer, so a client reading
    it needs the wallet's own ``ok``."""
    body = _claim(ranked_client).json()
    receipt = body.get("wallet")
    assert isinstance(receipt, dict), repr(receipt)
    assert receipt.get("ok") is True, receipt
    assert receipt.get("reward_type") == "leaderboard_payout", receipt


def test_a_response_is_json(ranked_client):
    for r in (_claim(ranked_client), _status(ranked_client)):
        assert r.headers["content-type"].startswith("application/json"), \
            r.headers["content-type"]


# ===========================================================================
# 4. THE STATUS RESPONSE SHAPE
# ===========================================================================

def test_the_status_route_answers_the_exact_key_set(ranked_client):
    body = _status(ranked_client).json()
    assert set(body) == STATUS_KEYS, sorted(body)
    assert body["ok"] is True, body["ok"]


def test_the_status_route_reports_the_session_player_it_answered_for(ranked_client):
    """The field a client uses to prove it did not read another player's row."""
    assert _status(ranked_client, "p1").json()["identity_player_id"] == "p1"


def test_the_status_route_sends_a_list_and_never_the_stored_one(ranked_client):
    """``paid_seasons`` is copied out. If the route handed over the stored
    list, a client appending to it would corrupt the guard."""
    body = _status(ranked_client).json()
    seasons = body["paid_seasons"]
    assert isinstance(seasons, list), repr(seasons)
    seasons.append("mutated-by-a-client")
    assert "mutated-by-a-client" not in _status(ranked_client).json()["paid_seasons"], (
        "the served list is the live stored list, not a copy")


def test_status_reflects_the_recorded_rank_and_paid_flag(ranked_client):
    """Before a claim: unpaid, and the rank the server recorded. After: paid."""
    before = _status(ranked_client).json()
    assert before["already_paid"] is False, before
    assert before["band"] == pure.band_for_rank(1), before["band"]

    _claim(ranked_client)
    after = _status(ranked_client).json()
    assert after["already_paid"] is True, after
    assert before["season"] in after["paid_seasons"], after["paid_seasons"]


def test_the_settling_flag_is_a_real_boolean(ranked_client):
    """A plain boolean a client branches on, so ``is False`` rather than
    truthiness: a 0 or "" would still be falsy but is the wrong type on the
    wire."""
    assert _status(ranked_client).json()["settling"] is False


# ===========================================================================
# 5. THE STATUS ROUTE IS A NON-WRITER
# ===========================================================================

def test_polling_status_persists_nothing():
    """The read-mutates defect this arm exists for.

    ``preview`` used to reach through the repairing getter, so every status poll
    ATTACHED a fresh ``leaderboard_payout`` key to the player's row. On the real
    copy-on-read store that write vanished silently; on the in-memory store it
    landed. Asserted from outside by checking the store was never written.
    """
    c = _build(standing={"p1": 1})
    before = copy.deepcopy(dict.__getitem__(c.wc_store, "p1"))
    # The SETUP writes once (recording the standing). Snapshot the write log
    # here, or this arm would measure its own fixture rather than the route.
    writes_before = list(c.wc_store.persist_calls)
    for _ in range(3):
        assert _status(c).status_code == 200
    after = copy.deepcopy(dict.__getitem__(c.wc_store, "p1"))
    assert before == after, (
        "the status route changed durable state; keys went %r -> %r"
        % (sorted(before), sorted(after)))
    assert c.wc_store.persist_calls == writes_before, (
        "the status route wrote to the store: %r"
        % (c.wc_store.persist_calls[len(writes_before):],))


def test_status_never_moves_money(ranked_client):
    before = _balance(ranked_client, "p1")
    for _ in range(3):
        _status(ranked_client)
    after = _balance(ranked_client, "p1")
    assert before == after == 0.0, (before, after)


# ===========================================================================
# 6. THE REQUEST BODY
# ===========================================================================

def test_the_body_requires_rank(ranked_client):
    """``rank`` is required (``...``) even though it is INERT. Pinned because a
    shipped client sends it, and a future edit that drops it from the model
    would 200 on a body that used to be valid -- while a client still sending
    it would keep working. Freezing the requirement keeps the change visible."""
    r = ranked_client.post("/leaderboard/p1/claim-payout", json={},
                           headers=_sign_in("p1"))
    assert r.status_code == 422, r.status_code


def test_the_body_may_omit_base_amount(ranked_client):
    """It has a default, so omitting it is valid and pays the server's table."""
    r = ranked_client.post("/leaderboard/p1/claim-payout", json={"rank": 1},
                           headers=_sign_in("p1"))
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["base_amount"] == LP.server_base_amount(LP.PAYOUT_CURRENCY)
    assert body["requested_base_amount"] == LP.DEFAULT_BASE_AMOUNT


@pytest.mark.parametrize("rank", [-1, "abc", None, 1.5, [], {"a": 1}])
def test_a_rank_that_is_not_a_non_negative_int_is_422(ranked_client, rank):
    """``rank`` is declared ``ge=0``, so these all fail at the type or range
    boundary -- before the handler prices anything."""
    r = ranked_client.post("/leaderboard/p1/claim-payout", json={"rank": rank},
                           headers=_sign_in("p1"))
    assert r.status_code == 422, "rank=%r answered %d" % (rank, r.status_code)


@pytest.mark.parametrize("amount", [-1, "abc", None])
def test_a_base_amount_that_is_not_a_non_negative_number_is_422(ranked_client, amount):
    """Same boundary for the inert amount field. Inert does not mean unchecked:
    an unbounded number would still be a number the server has to echo back."""
    r = ranked_client.post("/leaderboard/p1/claim-payout",
                           json={"rank": 1, "base_amount": amount},
                           headers=_sign_in("p1"))
    assert r.status_code == 422, "base_amount=%r answered %d" % (amount, r.status_code)


def test_a_validation_refusal_names_the_offending_field(ranked_client):
    """A client telling a player WHICH field was wrong needs the field name in
    the body, not a generic message."""
    r = ranked_client.post("/leaderboard/p1/claim-payout",
                           json={"rank": "abc", "base_amount": 1.0},
                           headers=_sign_in("p1"))
    assert r.status_code == 422, r.status_code
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail, repr(detail)
    assert any("rank" in str(e.get("loc", "")) for e in detail), detail


def test_an_unknown_body_field_is_ignored(ranked_client):
    """A shipped client sending extra keys must still get a real answer, not a
    422 -- that would be a client-visible regression bought for nothing."""
    r = ranked_client.post("/leaderboard/p1/claim-payout",
                           json={"rank": 1, "base_amount": 100.0, "evil": "x"},
                           headers=_sign_in("p1"))
    assert r.status_code == 200, r.text[:200]
    assert r.json()["ok"] is True


# ===========================================================================
# 7. PATH HYGIENE
# ===========================================================================

@pytest.mark.parametrize("path", [
    "/leaderboard/CLAIM-PAYOUT/p1",
    "/leaderboard//p1/claim-payout",
    "/leaderboard/p1/claim-payout/extra",
    "/leaderboard/p1/claim-payouts",
])
def test_near_miss_claim_paths_are_404(ranked_client, path):
    """Case, doubled slash, extra segment and a plural are all absent, so the
    exact path is provably what is mounted."""
    assert ranked_client.post(path, json={"rank": 1},
                              headers=_sign_in("p1")).status_code == 404, path


def test_an_empty_player_id_cannot_match_the_path(ranked_client):
    """``/leaderboard//claim-payout`` is a doubled slash, not a blank id."""
    assert ranked_client.post("/leaderboard//claim-payout", json={"rank": 1},
                              headers=_sign_in("p1")).status_code == 404


def test_a_numeric_player_id_is_matched_as_a_string(ranked_client):
    """Ids are path strings. A numeric id must still resolve, and must still be
    refused when the session is a different id of the same number."""
    c = _build(standing={"42": 1}, players=("42",))
    ok = c.post("/leaderboard/42/claim-payout", json={"rank": 1},
                headers=_sign_in("42"))
    assert ok.status_code == 200, ok.text[:200]
    other = c.post("/leaderboard/42/claim-payout", json={"rank": 1},
                   headers=_sign_in("43"))
    assert other.status_code == 403, other.status_code


# ===========================================================================
# 8. THE SHIPPING APP -- the routes are mounted in the real tree
# ===========================================================================

def test_the_shipping_app_serves_the_same_paths():
    """Everything above used a bare app. This proves it is SHIPPED.

    ``main`` is imported inside the test body on purpose: at module scope this
    file's whole result would depend on whatever another agent is editing in
    main.py's import graph, and a peer's half-finished edit has made whole test
    files unrunnable while their own code was unchanged. Here it can cost one
    test instead of the file.

    Not ``pytest.importorskip``: that re-raises when the ImportError comes from
    a module OTHER than the one named, which is the common case here, so it
    would fail the test instead of skipping it. The reason carries the offending
    module so the skip says what to fix.
    """
    try:
        import main as main_module
    except ImportError as exc:
        pytest.skip("server/main.py will not import on the shared tree: %r" % (exc,))

    app = main_module.app
    paths = {getattr(r, "path", None) for r in app.routes}
    for path in (STATUS_PATH, CLAIM_PATH):
        assert path in paths, "the shipping app does not mount %s" % path

    with TestClient(app, raise_server_exceptions=False) as c:
        # Anonymous is refused on the REAL app, where the ownership guard and
        # the modules' own require_player both act. A 200 here means one of
        # them stopped applying.
        assert c.get("/leaderboard/nobody/payout-status").status_code == 401, (
            "the shipping app served a payout-status read without a session")
        assert c.post("/leaderboard/nobody/claim-payout",
                      json={"rank": 1}).status_code == 401, (
            "the shipping app served a claim without a session")


# ===========================================================================
# 9. RANKING IS DETERMINISTIC -- same board, same answer, every time
# ===========================================================================
#
# WHY THIS SECTION IS HERE AND NOT IN THE PURE-MODULE FILE
# -------------------------------------------------------
# ``test_leaderboard_rewards_contract.py`` already pins the tie rule and
# ``original_index`` at the ``assign_rewards`` level, and that is the right
# place for it. What no file pinned is the property a CLIENT depends on: the
# HTTP reply is a pure function of the recorded standing. Same stored rank ->
# same band, same reward, same currency, on every call and on a freshly built
# app. A payout that varied between calls would be unreplayable -- a player
# could not be shown a quote and then be guaranteed that quote.
#
# Asserted as a RELATIONSHIP between two independent calls, never as a literal
# reward amount, so a balance retune does not break this section.


def _claim_shape(client, pid="p1", **kw):
    """The money-relevant part of a paid claim reply, for comparing two runs."""
    body = _claim(client, pid, **kw).json()
    return (
        body["ok"], body["season"], body["rank"], body["band"],
        body["reward"], body["base_amount"], body["currency"],
        body["reason"], body["already_paid"],
    )


def test_the_same_standing_answers_identically_on_a_rebuilt_app():
    """Two independent app/store/wallet stacks, one standing, one answer.

    Fresh construction each time is what makes this a determinism claim rather
    than a caching claim: if a hidden module-level dict, a random seed or a
    clock read leaked into the reply, the two shapes would differ.
    """
    first = _claim_shape(_build(standing={"p1": 1}))
    second = _claim_shape(_build(standing={"p1": 1}))
    assert first == second, "run1=%r run2=%r" % (first, second)


def test_the_reply_is_stable_across_many_calls():
    """Twenty rebuilds, one shape. A wobble would show up as a flaky payment.

    Twenty is not a magic number -- it is enough that a per-process clock or
    counter would have to be deliberately aligned to hide inside it.
    """
    shapes = {_claim_shape(_build(standing={"p1": 1})) for _ in range(20)}
    assert len(shapes) == 1, "the payout reply took %d distinct shapes" % len(shapes)


def test_a_better_standing_never_pays_less_than_a_worse_one():
    """The ordering property, as a relation between two replies.

    Rank 1 is better than rank 5, so it must not pay less. This survives a
    retune of the amounts themselves, which is why it is written as ``>=``
    between two measured replies rather than against a frozen number.
    """
    top = _claim(_build(standing={"p1": 1}), rank=1).json()
    worse = _claim(_build(standing={"p1": 5}), rank=5).json()
    assert top["reward"] >= worse["reward"], (top["reward"], worse["reward"])
    # And the server's own rank is the one echoed, not the caller's claim.
    assert top["rank"] == 1 and worse["rank"] == 5


def test_ranking_does_not_depend_on_which_other_players_exist():
    """A player's payout is a function of THEIR standing alone.

    This is the anti-griefing half of determinism: if another player's
    standing could move this player's band, then adding a rival would silently
    change an already-quoted payout. Two boards differing only in a bystander
    must give the same answer.
    """
    alone = _claim_shape(_build(standing={"p1": 1}, players=("p1",)))
    crowded = _claim_shape(
        _build(standing={"p1": 1, "other": 2}, players=("p1", "other")))
    assert alone == crowded, "alone=%r crowded=%r" % (alone, crowded)


def test_the_pure_ranker_is_deterministic_for_a_tied_board():
    """The pure function behind the reply, on the board shape that stresses it.

    A tie is where a sort key is easiest to get wrong, because equal scores
    leave the order to the sort's stability. Three entries, two tied, run ten
    times: the whole row list must be identical every run.
    """
    board = [{"pid": "a", "score": 900.0},
             {"pid": "b", "score": 800.0},
             {"pid": "c", "score": 900.0}]
    runs = [pure.assign_rewards(board, 100.0, key="score") for _ in range(10)]
    assert all(r == runs[0] for r in runs), "the tie order is not stable"
    # And the tie is real: the two 900s share rank 1 and the 800 is demoted.
    ranks = {row["entry"]["pid"]: row["rank"] for row in runs[0]}
    assert ranks["a"] == ranks["c"] == 1, ranks
    assert ranks["b"] > ranks["a"], ranks


# ===========================================================================
# 10. NON-FINITE NUMBERS ARE REFUSED -- MEASURED DEFECT, LIVE
# ===========================================================================
#
# MEASURED DEFECT (probes run against this tree, 2026-10-05):
#
#   POST /leaderboard/p1/claim-payout  with the JSON body
#       {"rank": 1, "base_amount": 1e400}
#   answers 500 Internal Server Error -- and the player's wallet has ALREADY
#   been credited 1000 spirit_stone by the time the reply fails.
#
#   Traceback: ValueError: Out of range float values are not JSON compliant:
#   inf  -- raised by json/encoder.py while serialising dict item
#   'requested_base_amount'.
#
# WHY: ``base_amount`` is declared ``float = Field(DEFAULT_BASE_AMOUNT, ge=0)``.
# ``ge=0`` is satisfied by +inf (``inf >= 0`` is True), so the model accepts
# it. The handler echoes the caller's number back as ``requested_base_amount``,
# and Starlette's JSONResponse uses ``json.dumps(..., allow_nan=False)``, so
# encoding +inf raises. The payout has already been committed at that point --
# the failure is in RESPONSE construction, not in the money path.
#
# THE CONSEQUENCE IS NOT DOUBLE-PAYMENT. The retry returns 200 with
# ``already_paid: true`` and the balance is unchanged (measured). What a client
# sees is a 500 on a payment that succeeded, so a sane retry-on-5xx client
# re-reads a balance that moved without being told, and a naive one shows the
# player an error for money they were actually given. A 500 that also MOVES
# MONEY is the worst of both worlds: it is indistinguishable from a real fault.
#
# ``rank`` has the same shape of failure by a different mechanism. ``rank`` is
# ``int = Field(..., ge=0)`` and pydantic's ``finite_number`` check refuses
# +inf at the model layer (verified: ``model(rank=float('inf'))`` raises
# ``finite_number``) -- but FastAPI then tries to render that refusal, and the
# error's own ``input`` field carries the +inf, so building the 422 raises the
# SAME ValueError and the client gets 500 instead of 422. Measured: no money
# moves on this one, because the handler never runs.
#
# WHY THE RAW-BYTES HELPER BELOW
# ------------------------------
# ``TestClient.post(json=...)`` cannot express this attack: httpx serialises
# with ``allow_nan=False`` and raises client-side before the request is sent.
# A real wire client sends the JSON NUMBER LITERAL ``1e400``, which is valid
# JSON text, and the server's own parser turns it into ``float('inf')``. So the
# tests send raw bytes. That is not a contrivance -- it is the only way to
# reproduce what a real client does.
#
# THE FIX IS TWO PARTS, BOTH MEASURED (not guessed)
# -----------------------------------------------------
# A control run applied candidate fixes to this file and recorded what the
# route then answered. The result is not the obvious one:
#
#   shipped ................................ +inf -> 500, wallet PAID 1000
#   allow_inf_nan=False only ............... +inf -> 500, wallet 0.0
#   allow_inf_nan=False + an app-level
#   RequestValidationError handler that
#   drops the 'input' key ................ +inf -> 422, wallet 0.0   FIXED
#
# So ``allow_inf_nan=False`` is NECESSARY BUT NOT SUFFICIENT. It stops the
# payout, but the 422 envelope still embeds pydantic's offending value under
# the ``input`` key, and Starlette serialises the reply with
# ``allow_nan=False`` -- so building the 422 raises the same ValueError and the
# client still gets 500. Both parts are needed: the field bound to keep the bad
# number out of the model, and a handler to keep it out of the error body.
#
# WHY THE `xfail` ARMS ARE MARKED `strict=False`
# ---------------------------------------------
# They state the CONTRACT (a 422, no money) and are red today. Verified against
# the two-part fix above: all six XPASS. `strict=False` means the suite stays
# green while the defect is live and reports XPASS the moment it is fixed, so
# the hole cannot be quietly forgotten. The tests in
# ``TestNonFiniteIsRefused`` assert the CURRENT behaviour directly; those are
# the arms that go RED when the fix lands, and they are deleted or inverted at
# the same time. Same idiom, same reason, as
# ``test_save_encryption_contract.py``.
# --------------------------------------------------------------------------

NON_FINITE_XFAIL = (
    "LIVE DEFECT: leaderboard_payout.LeaderboardPayoutRequest bounds rank and "
    "base_amount with ge=0 only, and ge=0 is satisfied by +inf, so a body of "
    '{"rank":1,"base_amount":1e400} is accepted. The handler credits the wallet '
    "and THEN Starlette's JSONResponse raises ValueError (allow_nan=False) "
    "while serialising requested_base_amount, so the client gets 500 AFTER "
    "being paid. rank=1e400 turns pydantic's own finite_number refusal into a "
    "500 for the same reason, via the 'input' key in the error body. "
    "MEASURED FIX, two parts, both required: (1) allow_inf_nan=False on both "
    "Fields -- this alone stops the money but leaves the 500; (2) an app-level "
    "RequestValidationError handler that drops the 'input' key from the error "
    "payload -- this turns it into a 422. XPASS = fixed."
)


def _raw_claim(client, raw_body: str, pid: str = "p1"):
    """POST a RAW JSON body, bypassing httpx's non-finite-strict serialiser.

    httpx encodes with ``allow_nan=False`` and raises before the wire, so the
    only faithful reproduction of a real client's ``1e400`` is to hand the
    bytes over untouched.
    """
    return client.post(
        f"/leaderboard/{pid}/claim-payout",
        content=raw_body.encode("utf-8"),
        headers={**_sign_in(pid), "Content-Type": "application/json"},
    )


#: The JSON number literals a client can put on the wire that parse to a
#: non-finite float. ``1e400`` overflows to +inf on IEEE-754 parse; the others
#: are the bare tokens, which ``json.loads`` accepts with allow_nan=True.
NON_FINITE_LITERALS = ("1e400", "-1e400")


def _reject_constant(name):
    """``parse_constant`` hook that refuses the non-RFC-8259 bare tokens.

    ``json.loads`` calls this for ``Infinity``, ``-Infinity`` and ``NaN``,
    which it accepts by default (allow_nan=True). A strict client does not.
    Raising here makes the strictness a test assertion rather than a comment.
    """
    raise ValueError("reply carried the non-standard JSON token %r" % (name,))


class TestNonFiniteIsRefused:
    """What the module does TODAY, asserted so the defect below is falsifiable."""

    @pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
    def test_a_non_finite_base_amount_answers_500_today(self, literal):
        r = _raw_claim(_build(standing={"p1": 1}),
                       '{"rank": 1, "base_amount": %s}' % literal)
        assert r.status_code == 500, (
            "base_amount=%s answered %d, not the measured 500" % (literal, r.status_code))
        # And the body is NOT the JSON error envelope a client branches on.
        assert "detail" not in r.text, r.text[:200]

    def test_a_non_finite_rank_answers_500_today(self):
        r = _raw_claim(_build(standing={"p1": 1}), '{"rank": 1e400}')
        assert r.status_code == 500, r.status_code

    def test_the_500_on_base_amount_happens_AFTER_the_wallet_is_paid(self):
        """The money moved even though the client was told the request failed.

        This is the part that makes the 500 worth fixing rather than papering
        over: the fault is not "a bad number was rejected badly", it is "a
        request reported as failed had already succeeded".
        """
        client = _build(standing={"p1": 1})
        assert _balance(client, "p1") == 0.0, "precondition: wallet starts empty"

        r = _raw_claim(client, '{"rank": 1, "base_amount": 1e400}')
        assert r.status_code == 500, r.status_code
        assert _balance(client, "p1") > 0.0, (
            "expected the wallet to be credited before the reply failed; "
            "if this now holds, the defect changed shape and the xfail arms "
            "below need re-measuring")

    def test_the_retry_after_a_500_does_not_pay_a_second_time(self):
        """No double-payment. The player is not farmed by the 500.

        Worth pinning on its own: it is the reason this defect is a bad
        client experience rather than a money hole, and a future edit that
        reordered the response construction could quietly change it.
        """
        client = _build(standing={"p1": 1})
        _raw_claim(client, '{"rank": 1, "base_amount": 1e400}')
        after_first = _balance(client, "p1")

        retry = _claim(client, rank=1)
        assert retry.status_code == 200, retry.text[:200]
        body = retry.json()
        assert body["already_paid"] is True, body
        assert body["reason"] == "already_paid_this_season", body
        assert _balance(client, "p1") == after_first, (
            "the retry paid again: %r -> %r" % (after_first,
                                               _balance(client, "p1")))

    def test_a_non_finite_rank_moves_no_money(self):
        """The rank arm fails BEFORE the handler, so it is only a wrong status.

        Recorded so the two arms are not conflated: one is "paid then failed",
        the other is "refused then failed to say so".
        """
        client = _build(standing={"p1": 1})
        r = _raw_claim(client, '{"rank": 1e400}')
        assert r.status_code == 500, r.status_code
        assert _balance(client, "p1") == 0.0, (
            "the rank arm paid, which contradicts the measurement")

    def test_finite_numbers_are_unaffected_by_the_defect(self):
        """The control. Ordinary amounts, including a very large finite one,
        must still be accepted and must still pay the server's own table price.

        Without this, a fix that clamps everything would also pass.
        """
        for literal in ("0", "100.0", "1e300", "12345678901234567890"):
            client = _build(standing={"p1": 1})
            r = _raw_claim(client, '{"rank": 1, "base_amount": %s}' % literal)
            assert r.status_code == 200, (
                "base_amount=%s answered %d: %s" % (literal, r.status_code,
                                                    r.text[:160]))
            body = r.json()
            # The echoed amount is the caller's; the PAID amount is the
            # server's. That separation is the whole point of the field being
            # inert, so it is asserted rather than assumed.
            assert body["requested_base_amount"] == float(literal), body
            assert body["base_amount"] == LP.server_base_amount(LP.PAYOUT_CURRENCY)


@pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_base_amount_is_a_422_not_a_500(literal):
    """THE CONTRACT. A number the server cannot represent is a client error.

    422 is what every other bad body in section 6 answers, and what this file
    already pins for ``rank="-abc"``. A non-finite literal is the same class of
    mistake -- a malformed number in the body -- and must not be the only one
    that answers 500. Marked ``strict=False`` because it is red today.
    """
    r = _raw_claim(_build(standing={"p1": 1}),
                   '{"rank": 1, "base_amount": %s}' % literal)
    assert r.status_code == 422, (
        "base_amount=%s answered %d; expected a 422 refusal" % (literal,
                                                                 r.status_code))


@pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
def test_a_non_finite_rank_is_a_422_not_a_500():
    """THE CONTRACT for the rank field. pydantic already refuses it; the
    refusal just cannot be serialised. Same fix, same reason."""
    r = _raw_claim(_build(standing={"p1": 1}), '{"rank": 1e400}')
    assert r.status_code == 422, r.status_code


@pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
def test_a_non_finite_base_amount_moves_no_money():
    """THE CONTRACT, stated in the unit that matters: a refused body pays
    nothing. Today it pays, which is what the 500 hides."""
    client = _build(standing={"p1": 1})
    _raw_claim(client, '{"rank": 1, "base_amount": 1e400}')
    assert _balance(client, "p1") == 0.0, (
        "a body the server refused still credited the wallet")


@pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
def test_the_request_model_refuses_a_non_finite_amount_at_the_field():
    """THE CONTRACT at the model layer, where the fix belongs.

    Written against the module's own request model rather than over HTTP, so
    it names the single line that has to change. ``allow_inf_nan=False`` turns
    the accepted +inf into pydantic's ``finite_number`` 422, and leaves
    ``ge=0`` still refusing negatives.
    """
    from pydantic import ValidationError

    model = LP.LeaderboardPayoutRequest
    with pytest.raises(ValidationError):
        model(rank=1, base_amount=float("inf"))
    # The control: ge=0 must still bite, so a fix cannot be "drop the bound".
    with pytest.raises(ValidationError):
        model(rank=1, base_amount=-1.0)
    # And the normal path must still validate.
    assert model(rank=1, base_amount=100.0).base_amount == 100.0


@pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
def test_the_reply_is_strict_json_even_for_a_refused_body():
    """No response on this route may carry a bare ``Infinity`` token.

    RFC 8259 has no literal for ``Infinity``, ``-Infinity`` or ``NaN``. A reply
    containing one parses in Python and fails in every other client, so
    "the server answered" is not the same as "the client can read it". This is
    the property Starlette's ``allow_nan=False`` already gives for free once
    no non-finite value can enter the reply -- it is pinned so a future
    response-model change cannot reintroduce it quietly.
    """
    r = _raw_claim(_build(standing={"p1": 1}), '{"rank": 1, "base_amount": 1e400}')
    assert r.status_code in (200, 422), r.status_code
    body = r.text
    for token in ("Infinity", "-Infinity", "NaN"):
        assert token not in body, "reply carried the bare token %r: %s" % (
            token, body[:200])
    # And it parses under a STRICT parser, which rejects those tokens.
    json.loads(body, parse_constant=_reject_constant)


# ===========================================================================
# 11. THE NON-WRITER ARM, PROVEN TO BITE (measured gap, filled 2026-10-05)
# ===========================================================================
#
# MEASURED GAP IN THE ARM ABOVE
# -----------------------------
# ``test_polling_status_persists_nothing`` (section 5) asserts the right
# thing, but it CANNOT FAIL. Proven, not assumed:
#
#   * Re-introducing the defect it exists for -- ``preview`` reaching the
#     repairing ``get_state`` instead of ``read_state`` -- leaves the test
#     GREEN. Measured by loading this real test file with that mutation applied
#     through a pytest plugin: "1 passed".
#
# WHY IT SURVIVES: this file's ``_FakeStore`` is copy-on-read, so ``__getitem__``
# hands the route a DETACHED COPY. The mutating read writes a
# ``leaderboard_payout`` key onto that copy, and the copy is thrown away
# unpersisted. The stored row never changes and ``persist`` is never called, so
# both assertions in section 5 hold while the defect is present.
#
# That is not a flaw in the fake -- copy-on-read IS the real store's semantics,
# and dropping the write is exactly the silent behaviour that made this defect
# hard to see in the first place. It is a flaw in using copy-on-read ALONE to
# test "this path writes nothing": the property is invisible to the very
# backend it was measured on. A mutating read is observable only where the read
# hands back a LIVE object, which is the in-memory backend.
#
# So the fix is a SECOND STORE, not a changed assertion: the same contract is
# re-stated against a live-reference store, where a write is observable. These
# arms are the control that proves section 5 is not vacuous -- if the module
# regresses, THESE go red.


class _LiveRefStore(_FakeStore):
    """Player store whose reads hand back the LIVE stored dict.

    Same interface as ``_FakeStore``, opposite read semantics. This is the
    in-memory backend's behaviour, and it is the only backend on which "this
    path mutated the player's row" is observable at all.
    """

    def __getitem__(self, player_id):
        return dict.__getitem__(self, player_id)


def _build_live_ref(standing=None, players=("p1",)):
    """``_build`` against a live-reference store, with the same guarantees."""
    from wallet_defense import WalletStore

    wallet = WalletStore()
    store = _LiveRefStore()
    for pid in players:
        store[pid] = {"id": pid, "name": f"Player {pid}", "wallet": {}}
    app = FastAPI()
    LP.register_leaderboard_payout_routes(app, lambda: store, lambda: wallet)
    client = TestClient(app, raise_server_exceptions=False)
    client.wc_store = store
    client.wc_wallet = wallet
    for pid, rank in (standing or {}).items():
        _record_standing(store, pid, rank)
    return client


def test_the_live_reference_store_can_actually_observe_a_write():
    """THE CONTROL. Without this, the three arms below are unfalsifiable.

    If this store could not see a write even when one really happened, then
    "the status route wrote nothing" would be true of every possible
    implementation and would prove nothing. So: drive a route that genuinely
    does persist, and assert this store notices.
    """
    c = _build_live_ref(standing={"p1": 1})
    before = dict.__getitem__(c.wc_store, "p1")
    assert LP.STATE_KEY not in before, "precondition: no payout state yet"

    # The claim route persists on the paid branch, so this is a real write.
    body = _claim(c).json()
    assert body["reason"] == "paid", body["reason"]
    after = dict.__getitem__(c.wc_store, "p1")
    assert LP.STATE_KEY in after, (
        "the live-reference store did not observe a write that really "
        "happened, so it cannot be used to prove a non-writer either")


def test_polling_status_writes_nothing_on_a_live_reference_store():
    """THE SAME CONTRACT AS SECTION 5, on a store that can observe it.

    This is the arm that fails when ``preview`` regresses to the repairing
    getter. Measured: with that mutation applied, this test fails with the
    player's row gaining a ``leaderboard_payout`` key.
    """
    c = _build_live_ref(standing={"p1": 1})
    before = deepcopy(dict.__getitem__(c.wc_store, "p1"))
    writes_before = list(c.wc_store.persist_calls)

    for _ in range(3):
        assert _status(c).status_code == 200

    after = deepcopy(dict.__getitem__(c.wc_store, "p1"))
    assert before == after, (
        "the status route mutated the player's row on a live-reference store; "
        "keys went %r -> %r" % (sorted(before), sorted(after)))
    assert c.wc_store.persist_calls == writes_before, (
        "the status route persisted: %r"
        % (c.wc_store.persist_calls[len(writes_before):],))


def test_the_status_route_never_reaches_the_repairing_getter():
    """Names the exact call the defect used, rather than its side effect.

    Section 5 proves the OBSERVABLE (nothing changed). This proves the CAUSE
    (the read-side accessor is not the repairing one), which is what a reviewer
    needs in order to know the difference is deliberate rather than luck.

    ``read_state`` defaults WITHOUT writing; ``get_state`` writes. A GET path
    that reaches the second one is the defect, whatever the backend does with
    the resulting copy.
    """
    calls = []
    original = LP.get_state

    def _spy(player):
        calls.append(player)
        return original(player)

    LP.get_state = _spy
    try:
        c = _build_live_ref(standing={"p1": 1})
        _status(c)
    finally:
        LP.get_state = original

    assert calls == [], (
        "the status route reached the repairing get_state %d time(s); it must "
        "use read_state" % (len(calls),))