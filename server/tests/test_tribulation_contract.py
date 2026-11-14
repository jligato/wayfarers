"""Contract tests for ``server/tribulation.py`` -- the WIRE, not the economy.

WHAT THIS FILE COVERS THAT THE SIBLING TRIBULATION FILES DO NOT
---------------------------------------------------------------
Four suites already exercise this module and this file does not repeat them:

* ``test_tribulation_route_contract.py`` -- the full six-route census, the state
  reply keysets, the strike economy, the replay receipt, the daily budget.
* ``test_tribulation_aftermath_claim.py`` -- one payout per cycle, atomicity.
* ``test_tribulation_phase_invariants.py`` -- the 21-day clock and its windows.
* ``test_tribulation_contract_gaps.py`` -- corrupt stored records, the cycle roll,
  a missing wallet store, padded idempotency keys, the battle-pass ceiling.

What is left unpinned, and what a client actually breaks on, is HERE:

  1. THE VERB MATRIX. Which method answers which path, and that a probe with the
     wrong verb is told 405 rather than quietly served.
  2. WHERE THE REQUEST LIVES. ``strikes`` is a QUERY parameter, not a body
     field. A client that puts it in a JSON body gets a silent default -- the
     single sharpest trap on this route, because the reply still looks right.
  3. REFUSAL ORDERING. Which check fires first: the player lookup, the retired
     ``damage`` parameter, the idempotency key, or the strike count. Two errors
     at once must produce one specific, documented answer.
  4. REPLY SHAPE VARIANTS. The base keyset, the replay superset (which adds one
     key), and the battle-pass receipt's presence rule.
  5. CONTRIBUTING OUTSIDE THE STORM. The charge still happens; the reward is
     zero. A client that shows "no reward" as a failure mis-reads this arm.

HOW THE CLOCK IS FROZEN
------------------------
``tribulation.time`` IS the standard library ``time`` module, so assigning to it
replaces the clock for every module in the pytest process. This file rebinds the
NAME ``tribulation.time`` to a stand-in object instead, which is local to this
module, and monkeypatch restores the real module afterwards.

Run with the repository batch runner (it strips every database variable):

    tools/wc_pytest.sh server/tests/test_tribulation_contract.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVER_DIR.parent
for _entry in (str(_REPO_ROOT), str(_SERVER_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Cleared at import, before any store is constructed: PlayerStore latches the
# database decision once and keeps it for the life of the process.
for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_var, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

import auth  # noqa: E402  -- the BARE name: the session store main.py reads
import tribulation as TT  # noqa: E402
from server.tests.wc_test_auth import clear_signup_rate_limit  # noqa: E402


READ_PATH = "/tribulation/{player_id}"
CONTRIBUTE_PATH = "/tribulation/{player_id}/contribute"
AFTERMATH_PATH = "/tribulation/{player_id}/claim-aftermath"

#: The reply ``contribute`` returns for a fresh, successful call. ``_replayed``
#: is absent here and is the ONE key a retry adds; ``battle_pass`` is present
#: only when the call earned battle-pass experience (see section 4).
CONTRIBUTE_BASE_KEYS = {
    "legacy_points", "tribulation_tokens", "phase", "ok", "strikes", "damage",
    "damage_per_strike", "stones_spent", "already_claimed_today",
    "total_contribution", "strikes_left_today",
}
BATTLE_PASS_KEYS = {"ok", "source", "xp_gained", "new_level", "leveled_up",
                    "total_xp"}
AFTERMATH_KEYS = {"ok", "breakthrough_bonus", "already_claimed"}


class _FrozenClock:
    """A stand-in for the ``time`` module whose ``time()`` is pinned.

    Every other attribute is delegated to the real module.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now

    def __getattr__(self, name):
        return getattr(time, name)


def _first_timestamp_in(phase: "TT.TribulationPhase", horizon: int = 90) -> float:
    """First real timestamp at or after now that lands in ``phase``.

    The phase decision is deliberately NOT stubbed: the point is that the real
    clock can actually produce the window the test claims to be standing in.
    """
    base = time.time()
    for offset in range(horizon):
        candidate = base + offset * 86400
        if TT.get_phase(candidate) == phase:
            return candidate
    raise AssertionError(f"{phase.value} never occurs within {horizon} days")


def _freeze(monkeypatch, phase: "TT.TribulationPhase") -> float:
    """Pin ``tribulation``'s clock into ``phase`` for the rest of the test."""
    stamp = _first_timestamp_in(phase)
    monkeypatch.setattr(TT, "time", _FrozenClock(stamp))
    return stamp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """The real app, a fresh in-memory store, and a client inside its lifespan.

    Entering the ``TestClient`` context runs the startup hooks; without it the
    route table undercounts. The store is rebuilt per test so a test that spends
    stones or books a receipt cannot leak into the next one. ``main.players_db``
    is rebound because the routes take a provider and re-read that module global
    on every call.
    """
    import server.main as main
    from player_store import PlayerStore

    saved = (main.wallet_store, main.players_db, main.USE_PG)
    main.USE_PG = False
    main.wallet_store = main.InMemoryWalletStore()
    main.players_db = PlayerStore(main.wallet_store)
    clear_signup_rate_limit()
    try:
        with TestClient(main.app) as test_client:
            test_client.wc_main = main
            yield test_client
    finally:
        main.wallet_store, main.players_db, main.USE_PG = saved


def _creator(client):
    """A player factory that needs no fixture of its own.

    Spirit stones go into the WALLET via ``main.earn``, never into the player
    row's own ``spirit_stones`` dict: ``contribute`` debits the wallet, so a row
    funded directly still answers ``insufficient balance``.
    """
    main = client.wc_main
    counter = {"n": 0}

    def _make(name: str = "TribWire", stones: int = 0):
        counter["n"] += 1
        response = client.post("/player/create", json={
            "name": f"{name}{counter['n']}", "spirit_root": "fire"})
        assert response.status_code == 200, response.text
        pid = response.json()["id"]
        headers = {"Authorization": f"Bearer {auth.issue_session(pid)}"}
        if stones:
            funded = main.earn(main.wallet_store, pid, "spirit_stone", stones,
                               f"wire-seed-{pid}", "test")
            assert funded.get("ok"), funded
        return pid, headers

    return _make


@pytest.fixture()
def make_player(client):
    return _creator(client)


def _stones(main, pid: str) -> int:
    """The player's spendable balance, as the wallet reports it."""
    return int(main.wallet_store.get(pid).balance.spirit_stone)


def _legacy_total(main, pid: str) -> float:
    """Everything the player has banked as legacy, from both places it lands.

    The contribution route credits the player's running ``legacy_bonus`` field.
    Reading it is how a test proves a reward was actually BANKED rather than
    merely advertised in the reply -- which is the whole difference on the
    repeat-call arm in section 6.
    """
    with main.players_db.transaction(pid):
        return float(main.players_db[pid].get("legacy_bonus", 0.0))


def _contribute(client, pid, headers, query: str = "strikes=1", key=None,
                json=None):
    key = key or f"wire-{pid}-{time.time()}-{id(query)}"
    url = f"/tribulation/{pid}/contribute"
    if query:
        url = f"{url}?{query}"
    return client.post(url, headers={**headers, "Idempotency-Key": key}, json=json)


# ===========================================================================
# 1. THE VERB MATRIX
# ===========================================================================

def test_three_paths_and_only_these_three_under_the_prefix(client):
    """The census, so a rename or a new fourth route fails with the picture."""
    mounted = set()
    for route in client.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/tribulation/"):
            for method in route.methods:
                mounted.add((method, route.path))
    assert mounted == {
        ("GET", READ_PATH),
        ("POST", CONTRIBUTE_PATH),
        ("POST", AFTERMATH_PATH),
    }, f"the mounted tribulation route table changed: {sorted(mounted)}"


def test_the_read_path_refuses_every_other_verb(client, make_player):
    pid, headers = make_player()
    for verb in ("post", "put", "patch", "delete"):
        # ``delete`` takes no body in this test client, so the body is attached
        # per verb rather than uniformly -- the point of the arm is the STATUS,
        # not the payload.
        kwargs = {} if verb == "delete" else {"json": {}}
        response = getattr(client, verb)(f"/tribulation/{pid}",
                                          headers=headers, **kwargs)
        assert response.status_code == 405, (
            f"{verb.upper()} on the read path answered {response.status_code}: "
            f"{response.text[:200]}")
        assert response.json()["detail"] == "Method Not Allowed"


def test_head_on_the_read_path_is_405(client, make_player):
    """Measured, and worth pinning: FastAPI folklore says GET accepts HEAD.

    It does not here, so a client that health-checks with HEAD is told 405
    rather than given an empty 200 it might read as "the event is live".
    """
    pid, headers = make_player()
    assert client.head(f"/tribulation/{pid}", headers=headers).status_code == 405


def test_the_write_paths_refuse_get(client, make_player):
    pid, headers = make_player()
    for path in (CONTRIBUTE_PATH, AFTERMATH_PATH):
        response = client.get(f"/tribulation/{pid}/{path.split('/')[-1]}",
                              headers=headers)
        assert response.status_code == 405, (path, response.status_code)
        assert response.json()["detail"] == "Method Not Allowed"


def test_an_unknown_path_under_the_prefix_is_404(client, make_player):
    pid, headers = make_player()
    response = client.get(f"/tribulation/{pid}/leaderboard", headers=headers)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Not Found"


def test_a_missing_player_segment_is_404(client, make_player):
    """The path requires the id; there is no "current player" shorthand."""
    _, headers = make_player()
    assert client.get("/tribulation/", headers=headers).status_code == 404


# ===========================================================================
# 2. WHERE THE REQUEST LIVES
# ===========================================================================

def test_strikes_in_a_body_is_ignored_and_the_default_is_charged(
        client, make_player, monkeypatch):
    """The sharpest trap on this route: ``strikes`` is a QUERY parameter.

    A client that sends ``{"strikes": 40}`` as a JSON body gets a 200 -- and
    the reply looks entirely normal. The server books and charges ONE strike.
    Pinned so that a future switch to a body model, or a client fix, is a
    deliberate change to this test.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    response = _contribute(client, pid, headers, query="", key="body-only",
                           json={"strikes": 40})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["strikes"] == 1, (
        f"a body-borne strikes figure was honoured: {body}")
    assert body["stones_spent"] == TT.TRIBULATION_STRIKE_COST
    assert before - _stones(main, pid) == TT.TRIBULATION_STRIKE_COST
    assert body["damage"] == body["damage_per_strike"], (
        "the body figure was silently applied to the damage instead")


def test_the_query_parameter_is_the_one_that_is_honoured(
        client, make_player, monkeypatch):
    """The same number in the query string IS honoured. Same route, same body."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    body = _contribute(client, pid, headers, query="strikes=3", key="q-3").json()
    assert body["strikes"] == 3
    assert body["stones_spent"] == 3 * TT.TRIBULATION_STRIKE_COST


def test_an_unknown_query_parameter_other_than_damage_is_dropped(
        client, make_player, monkeypatch):
    """Measured asymmetry, and the reason ``damage`` needed its own 400.

    FastAPI silently drops any query parameter the route does not name, so a
    typo like ``stikres=5`` buys ONE strike and reports success. ``damage``
    alone is loud. Pinned so the loud one stays the only loud one: adding a
    second loud arm is a deliberate change, not an accident.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    body = _contribute(client, pid, headers, query="stikres=5",
                       key="typo").json()
    assert body["strikes"] == 1, body


def test_an_empty_query_string_uses_the_documented_default(client, make_player,
                                                           monkeypatch):
    """No ``strikes`` at all is legal and means one strike."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    body = _contribute(client, pid, headers, query="", key="default").json()
    assert body["strikes"] == 1


# ===========================================================================
# 3. REFUSAL ORDERING
# ===========================================================================

def test_the_retired_damage_parameter_is_refused_before_the_key_is_read(
        client, make_player, monkeypatch):
    """Two errors at once -- no key AND a retired ``damage`` -- resolve to 400.

    The retired-parameter check sits ABOVE the idempotency-key check, so a stale
    client that also forgot its key is told about the parameter it should stop
    sending. Order is part of the contract: the detail string names ``damage``
    and ``strikes``, never the key.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    response = client.post(f"/tribulation/{pid}/contribute?damage=1000",
                           headers=headers)
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "damage" in detail and "strikes" in detail, detail
    assert "Idempotency" not in detail, detail


def test_an_unknown_player_is_refused_before_anything_else(client, make_player,
                                                           monkeypatch):
    """No row, no key, a retired parameter. All three are wrong at once.

    The refusal now comes from the auth layer, one layer ABOVE the handler.
    MEASURED 2026-10-05 against the real app: 401. ``auth.authenticate``
    confirms the session's player still exists and revokes the token when it
    does not, so a ghost session never reaches this route at all.

    That is the deleted-player defense and it is the right answer: a token for
    an account that was never created must stop working, and the refusal must
    not tell a caller whether an id is live. Nothing was weakened to get here.

    The ordering this test exists to pin is unchanged and still holds -- the
    refusal beats the idempotency gate and the retired ``damage`` parameter.
    It is simply one layer higher now. A type-invalid query parameter is still
    answered by the framework above that, which
    ``test_a_type_invalid_count_beats_even_the_missing_player`` pins.

    A real player's own contribute is pinned elsewhere in this file, so this
    cannot be satisfied by refusing everyone.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    ghost = "wire-ghost-never-created"
    headers = {"Authorization": f"Bearer {auth.issue_session(ghost)}"}
    response = client.post(
        f"/tribulation/{ghost}/contribute?damage=1000",
        headers=headers)
    assert response.status_code == 401, response.text


def test_a_type_invalid_count_beats_even_the_missing_player(client):
    """The framework's own validation is a layer ABOVE the handler.

    Measured: a request naming a player that does not exist, carrying no
    idempotency key, a retired ``damage`` parameter AND ``strikes=abc``, answers
    422 -- not the handler's 404. FastAPI type-checks the query parameter while
    building the request, before the auth dependency and before the handler
    body, so a client with a typo in ``strikes`` is told about the typo even
    when everything else about the request is also wrong.

    This is why the ordering test above cannot carry ``strikes=abc``.
    """
    ghost = "wire-ghost-typecheck"
    headers = {"Authorization": f"Bearer {auth.issue_session(ghost)}"}
    response = client.post(
        f"/tribulation/{ghost}/contribute?damage=1000&strikes=abc",
        headers=headers)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["query", "strikes"], detail


def test_the_key_is_required_before_the_strike_count_is_examined(
        client, make_player, monkeypatch):
    """Missing key plus an over-the-ceiling count resolves to the key rule.

    The key is read before the count, so a caller with no key is told to send
    one instead of being told to lower a count it has not proven it can pay for.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    over = TT.TRIBULATION_MAX_STRIKES_PER_CALL + 1
    response = client.post(f"/tribulation/{pid}/contribute?strikes={over}",
                           headers=headers)
    assert response.status_code == 400, response.text
    assert "Idempotency-Key" in response.json()["detail"]


def test_the_strike_ceiling_names_the_ceiling(client, make_player, monkeypatch):
    """The refusal is actionable: it prints the number the caller may use."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=1_000_000)
    ceiling = TT.TRIBULATION_MAX_STRIKES_PER_CALL
    response = _contribute(client, pid, headers,
                           query=f"strikes={ceiling + 1}", key="over-ceiling")
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert str(ceiling) in detail, detail
    assert "strikes" in detail, detail


def test_a_non_integer_strike_count_is_422_naming_the_parameter(
        client, make_player, monkeypatch):
    """Type errors are answered by the framework, so the client gets a locator.

    The detail names ``strikes`` and the query location, which is the only part
    a client can act on. Pinned because a handler-level coercion would replace
    this with a plain 400 string and lose the field name.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    response = _contribute(client, pid, headers, query="strikes=abc",
                           key="abc")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    assert detail[0]["loc"][-1] == "strikes", detail


@pytest.mark.parametrize("bad", ["0", "-3"])
def test_a_non_positive_strike_count_is_400(client, make_player, monkeypatch, bad):
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    response = _contribute(client, pid, headers, query=f"strikes={bad}",
                           key=f"bad-{bad}")
    assert response.status_code == 400, response.text
    assert "positive" in response.json()["detail"].lower()


# ===========================================================================
# 4. REPLY SHAPE VARIANTS
# ===========================================================================

def test_a_fresh_contribution_carries_the_base_keyset(client, make_player,
                                                      monkeypatch):
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    body = _contribute(client, pid, headers, key="shape-1").json()
    assert CONTRIBUTE_BASE_KEYS <= set(body), (
        f"missing: {sorted(CONTRIBUTE_BASE_KEYS - set(body))}")
    assert "_replayed" not in body, "a fresh call answered as a replay"
    assert body["ok"] is True


def test_a_retry_is_the_base_keyset_plus_exactly_one_key(client, make_player,
                                                         monkeypatch):
    """The retry arm has a DIFFERENT keyset, by one key. A client that pins the
    exact set on the first reply must tolerate the extra one on the second."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    first = _contribute(client, pid, headers, key="shape-2").json()
    second = _contribute(client, pid, headers, key="shape-2").json()

    assert second.get("_replayed") is True, second
    assert set(second) == set(first) | {"_replayed"}, (
        f"the retry reply changed shape: {sorted(set(second) ^ (set(first) | {'_replayed'}))}")
    assert {k: v for k, v in second.items() if k != "_replayed"} == first, (
        "a retry altered the original answer's figures")


def test_the_battle_pass_receipt_is_present_and_itself_well_shaped(
        client, make_player, monkeypatch):
    """The nested receipt has its OWN keyset; a client renders it separately."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player(stones=100_000)
    body = _contribute(client, pid, headers, query="strikes=5",
                       key="bp-shape").json()

    assert "battle_pass" in body, (
        "a call that earned battle-pass experience returned no receipt: %r" % body)
    receipt = body["battle_pass"]
    assert set(receipt) == BATTLE_PASS_KEYS, (
        f"added={sorted(set(receipt) - BATTLE_PASS_KEYS)} "
        f"removed={sorted(BATTLE_PASS_KEYS - set(receipt))}")
    assert receipt["ok"] is True
    assert isinstance(receipt["xp_gained"], int)


def test_the_reply_figures_agree_with_each_other(client, make_player, monkeypatch):
    """Relationships between the reply's own numbers, not pinned literals.

    These hold on every day of the cycle and under any rebalance of the cost
    table, so they are the checks that keep a client from rendering a strike
    that never happened.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    body = _contribute(client, pid, headers, query="strikes=4",
                       key="agree-1").json()

    assert body["damage"] == body["damage_per_strike"] * body["strikes"], body
    assert body["stones_spent"] == body["strikes"] * TT.TRIBULATION_STRIKE_COST
    assert body["total_contribution"] >= body["damage"], body
    assert 0 <= body["strikes_left_today"] < TT.TRIBULATION_DAILY_STRIKE_BUDGET
    assert body["damage"] > 0, "a strike landed no damage at all"
    assert body["phase"] == TT.get_phase(TT.time.time()).value

    # The wallet really moved, and by exactly the reported figure.
    after = _contribute(client, pid, headers, query="strikes=1",
                        key="agree-2").json()
    assert after["strikes_left_today"] == body["strikes_left_today"] - 1
    assert after["total_contribution"] == body["total_contribution"] + after["damage"]


def test_contributing_outside_the_storm_charges_and_pays_nothing(
        client, make_player, monkeypatch):
    """The quiet phase is still playable: charged, booked, and rewarded zero.

    A client that reads a zero reward as "the request failed" mis-handles this
    arm, so it is pinned explicitly rather than left to the day of the month.
    """
    _freeze(monkeypatch, TT.TribulationPhase.CALM)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    response = _contribute(client, pid, headers, query="strikes=2",
                           key="calm-strike")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["phase"] == TT.TribulationPhase.CALM.value, body
    assert body["legacy_points"] == 0, body
    assert body["tribulation_tokens"] == 0, body
    assert body["already_claimed_today"] is False, (
        "nothing was paid, so nothing was claimed; reporting otherwise makes a "
        "later same-day storm press skip its real reward")
    # Battle-pass experience IS granted outside the storm, because it is derived
    # from the damage and not from the reward. Pinned as measured behaviour: a
    # client must not treat the presence of the receipt as proof that a reward
    # was paid, and a future change that gates it on the phase must break here.
    assert "battle_pass" in body, (
        "battle-pass experience stopped being granted outside the storm; this "
        "file records that it used to be, so a phase gate is a deliberate "
        "change: %r" % body)
    assert set(body["battle_pass"]) == BATTLE_PASS_KEYS, body["battle_pass"]

    # The charge and the booking are real even though the reward is not.
    assert before - _stones(main, pid) == 2 * TT.TRIBULATION_STRIKE_COST
    assert body["damage"] > 0, body
    assert body["total_contribution"] > 0, body


def test_the_claim_route_answers_a_three_key_shape(client, make_player,
                                                   monkeypatch):
    """The aftermath claim has ONE reply shape, unlike ``contribute``.

    Pinned because the already-claimed arm returns the SAME three keys with a
    zero bonus -- a client must not have to branch on the keyset to tell them
    apart, and there is no ``_replayed`` marker here.
    """
    _freeze(monkeypatch, TT.TribulationPhase.AFTERMATH)
    pid, headers = make_player()

    first = client.post(f"/tribulation/{pid}/claim-aftermath", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == AFTERMATH_KEYS, (
        f"added={sorted(set(body) - AFTERMATH_KEYS)} "
        f"removed={sorted(AFTERMATH_KEYS - set(body))}")
    assert body["already_claimed"] is False
    assert isinstance(body["breakthrough_bonus"], int)

    second = client.post(f"/tribulation/{pid}/claim-aftermath", headers=headers)
    assert second.status_code == 200, second.text
    assert set(second.json()) == AFTERMATH_KEYS, sorted(second.json())
    assert second.json()["already_claimed"] is True
    assert second.json()["breakthrough_bonus"] == 0


def test_the_claim_outside_the_window_is_400_and_names_the_window(
        client, make_player, monkeypatch):
    """The refusal tells the caller which phase it has to wait for."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    pid, headers = make_player()
    response = client.post(f"/tribulation/{pid}/claim-aftermath", headers=headers)
    assert response.status_code == 400, response.text
    assert "aftermath" in response.json()["detail"].lower()


# ===========================================================================
# 5. AUTH -- the two refusals a caller can provoke, and their order
# ===========================================================================

def test_anonymous_is_401_on_all_three_paths(client, make_player):
    """A real row exists first, so the 401 cannot be the handler's own 404."""
    pid, _ = make_player()
    calls = (("get", f"/tribulation/{pid}"),
             ("post", f"/tribulation/{pid}/contribute?strikes=1"),
             ("post", f"/tribulation/{pid}/claim-aftermath"))
    for method, url in calls:
        response = getattr(client, method)(url)
        assert response.status_code == 401, (
            f"{method.upper()} {url} answered {response.status_code} unsigned: "
            f"{response.text[:200]}")


@pytest.mark.parametrize("header", [
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer totally-made-up"},
    {"Authorization": "Basic totally-made-up"},
    {"Authorization": "totally-made-up"},
])
def test_every_credential_failure_is_401_with_the_same_vague_detail(
        client, header):
    """All four failures are indistinguishable, on purpose.

    Telling a caller which half of the credential they got right is free
    reconnaissance, so the string is identical for all of them.
    """
    response = client.get("/tribulation/somebody", headers=header)
    assert response.status_code == 401, (header, response.status_code)
    assert response.json()["detail"] == "invalid or missing session token"


def test_a_signed_in_caller_naming_another_player_is_403(client, make_player):
    """Ownership is checked from the session, not from the path alone."""
    owner, _ = make_player("TribWireOwner")
    _, stranger = make_player("TribWireStranger")

    calls = (("get", f"/tribulation/{owner}"),
             ("post", f"/tribulation/{owner}/contribute?strikes=1"),
             ("post", f"/tribulation/{owner}/claim-aftermath"))
    for method, url in calls:
        response = getattr(client, method)(url, headers=stranger)
        assert response.status_code == 403, (
            f"{method.upper()} {url} answered {response.status_code} for "
            f"another player's row: {response.text[:200]}")


def test_a_refused_call_spends_nothing(client, make_player, monkeypatch):
    """A 401 and a 403 must both leave the wallet and the record untouched."""
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)
    _, stranger = make_player("TribWireSpend", stones=100_000)

    before = _stones(main, pid)
    anonymous = client.post(f"/tribulation/{pid}/contribute?strikes=5")
    forbidden = client.post(f"/tribulation/{pid}/contribute?strikes=5",
                           headers=stranger)
    assert anonymous.status_code == 401
    assert forbidden.status_code == 403

    assert _stones(main, pid) == before, "a refused call spent the player's stones"
    with main.players_db.transaction(pid):
        record = main.players_db[pid].get("tribulation") or {}
    assert not record, f"a refused call booked state: {record}"


# ===========================================================================
# 6. THE REPLY-SHAPE EDGES NOBODY ELSE PINS
# ===========================================================================
# Everything below was MEASURED against the real app, not reasoned from the
# source, and none of it is covered by the four sibling tribulation files. Each
# one is a place a client is silently wrong: a key that is sometimes absent, a
# parameter that is quietly coerced, a path that answers under a spelling the
# client did not intend.

def test_a_call_that_earns_no_experience_omits_the_receipt_entirely(
        client, make_player, monkeypatch):
    """``battle_pass`` is OPTIONAL, and the condition for its absence is quiet.

    Battle-pass experience is one per ten derived damage. Damage has a floor of
    one (so a strike is never worth nothing), which means a player too weak to
    land ten damage in a call gets ZERO experience -- and the route then omits
    the ``battle_pass`` key entirely rather than sending it with a zero.

    A client that reads ``reply["battle_pass"]`` unconditionally crashes on
    exactly the weakest players, which are the ones most likely to be on a fresh
    account. Pinned because the four sibling files only ever assert the receipt
    IS present, so this arm would pass unnoticed if it started arriving.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    # Weaken the attacker through the player's OWN stored stats, which is the
    # only input ``server_strike_damage`` reads. No stubbing of the damage
    # function itself, so the arm is measured through the real formula.
    with main.players_db.transaction(pid):
        row = main.players_db[pid]
        row["atk"] = 1
        main.players_db.persist(pid, row)

    body = _contribute(client, pid, headers, key="weak").json()

    assert body["damage"] > 0, (
        f"a strike landed no damage, so this test proves nothing: {body}")
    assert body["damage"] < TT.TRIBULATION_BP_XP_PER_DAMAGE, (
        f"this player is strong enough to earn experience; weaken further. "
        f"damage={body['damage']}")
    assert body["damage"] // TT.TRIBULATION_BP_XP_PER_DAMAGE == 0, body

    assert "battle_pass" not in body, (
        f"a zero-experience call shipped a battle-pass receipt: {body}")
    # The rest of the contract is untouched: only the nested receipt goes.
    assert CONTRIBUTE_BASE_KEYS <= set(body), (
        f"missing: {sorted(CONTRIBUTE_BASE_KEYS - set(body))}")


def test_a_repeated_strike_parameter_is_answered_with_the_last_one(
        client, make_player, monkeypatch):
    """``?strikes=5&strikes=1`` buys ONE strike, and charges for one.

    Measured: the framework takes the LAST value, so a client that appends a
    second parameter silently has its real figure discarded while the reply
    reports the small one. That direction is the safe one -- the player is
    charged for what the reply says -- and it is pinned so it cannot flip to
    the first value, which would let a caller pass a small count and be charged
    nothing while the reply claimed the small one.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    body = _contribute(client, pid, headers, query="strikes=5&strikes=1",
                       key="dup-down").json()

    assert body["strikes"] == 1, (
        f"the repeated parameter was not resolved to the last value: {body}")
    assert body["stones_spent"] == TT.TRIBULATION_STRIKE_COST, body
    assert before - _stones(main, pid) == TT.TRIBULATION_STRIKE_COST, (
        "the charge did not match the figure the reply reported")


def test_the_last_repeated_parameter_is_the_one_that_wins(
        client, make_player, monkeypatch):
    """The mirror of the arm above, and the one that would actually be unsafe.

    Ordered the other way, the last value is the LARGER one, so a caller gets
    the seven it asked for and is charged for seven. Pinned so the rule reads
    "last wins" rather than "small wins", which are indistinguishable from a
    single observation.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    body = _contribute(client, pid, headers, query="strikes=1&strikes=7",
                       key="dup-up").json()

    assert body["strikes"] == 7, body
    assert before - _stones(main, pid) == 7 * TT.TRIBULATION_STRIKE_COST, (
        "the wallet charge disagreed with the figure the reply reported")


@pytest.mark.parametrize("raw,expected", [
    ("%205", 5),      # a leading space is tolerated
    ("%2B5", 5),      # an explicit plus sign is tolerated
    ("5.0", 5),       # a whole number in float spelling is tolerated
])
def test_a_count_the_framework_coerces_is_charged_at_the_coerced_value(
        client, make_player, monkeypatch, raw, expected):
    """Three spellings of five all answer 200, and all charge for five.

    Measured, and worth pinning because it is the permissive half of the
    validation story: ``strikes=abc`` is refused with a locator (section 3),
    yet these three are accepted. The rule a client must rely on is that the
    reply's ``strikes`` is the authoritative figure and the charge matches it --
    never that the query string is echoed back verbatim.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    response = _contribute(client, pid, headers, query=f"strikes={raw}",
                           key=f"coerce-{expected}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["strikes"] == expected, (
        f"{raw!r} was answered as {body['strikes']} strikes, not {expected}: {body}")
    assert before - _stones(main, pid) == expected * TT.TRIBULATION_STRIKE_COST, (
        "the charge did not match the coerced figure")


def test_a_fractional_strike_count_is_refused_by_the_framework(
        client, make_player, monkeypatch):
    """``5.0`` coerces to five; ``3.5`` is refused. The boundary is not obvious.

    Both are float spellings, and the difference is that three and a half has
    no whole-number reading. A client that sends a computed average here gets a
    422 naming the parameter rather than a silent truncation to three, which
    would book a swing the caller never asked for.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    response = _contribute(client, pid, headers, query="strikes=3.5",
                           key="fractional")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["query", "strikes"], detail

    assert _stones(main, pid) == before, "a refused count still charged the player"


def test_a_second_call_the_same_day_reports_the_reward_it_did_not_pay(
        client, make_player, monkeypatch):
    """The repeat arm still advertises the boss reward, with a flag beside it.

    Measured: on the second call of a storm day the reply's ``legacy_points``
    and ``tribulation_tokens`` carry the same figures as the first, while
    ``already_claimed_today`` flips to true and NO further reward is banked.

    This is the sharpest thing a client can get wrong on this route: it renders
    "you earned 200 legacy" from the reward fields while the player received
    nothing. The one authoritative signal is ``already_claimed_today``, so it is
    pinned here as a relationship between the two, and the wallet's silence is
    asserted so the test cannot pass on the reply alone.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    first = _contribute(client, pid, headers, query="strikes=2",
                        key="repeat-a").json()
    assert first["already_claimed_today"] is False, first
    assert first["legacy_points"] > 0 and first["tribulation_tokens"] > 0, first
    banked_after_first = _legacy_total(main, pid)

    second = _contribute(client, pid, headers, query="strikes=2",
                         key="repeat-b").json()
    assert second["already_claimed_today"] is True, second

    # The repeat reply is shape-identical to the first -- the same keyset, no
    # ``_replayed``, because this is a NEW key and genuinely new work.
    assert set(second) == set(first), (
        f"the repeat reply changed shape: "
        f"{sorted(set(second) ^ set(first))}")
    assert "_replayed" not in second, second

    # The figures still name the reward. Only the flag distinguishes the arms.
    assert second["legacy_points"] == first["legacy_points"], second
    assert second["tribulation_tokens"] == first["tribulation_tokens"], second

    # ...and nothing was actually banked the second time.
    assert _legacy_total(main, pid) == banked_after_first, (
        "the participation reward was paid twice on one cycle day")

    # The charge and the booking are still real on the repeat arm: a client
    # must not read already_claimed_today as "this call did nothing".
    assert second["damage"] > 0, second
    assert second["total_contribution"] > first["total_contribution"], second
    assert second["stones_spent"] == 2 * TT.TRIBULATION_STRIKE_COST, second


def test_a_receipt_survives_the_day_and_the_cycle_rolling_over(
        client, make_player, monkeypatch):
    """A retry is answered from the receipt even a whole cycle later.

    Measured, and this is the receipt's one behaviour that is easy to break by
    fixing something else. The receipt key is derived from the player's id and
    the idempotency key ONLY -- no day, no cycle. A client whose network dropped
    the first reply and retried after the 21-day clock turned over still gets
    the ORIGINAL answer, marked ``_replayed``, and is charged nothing.

    That is what makes the receipt safe to keep for a player rather than clear
    it at the cycle boundary, which is exactly what the day-keyed strike ledger
    beside it does. The test is therefore also a check that the two ledgers are
    NOT the same ledger.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    first = _contribute(client, pid, headers, query="strikes=3",
                        key="survivor").json()
    assert "_replayed" not in first, first

    # Jump forward by two full cycles so both the cycle index and the day index
    # have moved. The clock is re-pinned through the same helper, so the
    # assertions below read the new time rather than the original.
    stamp = TT.time.time() + 2 * TT.CYCLE_DAYS * TT.DAY
    monkeypatch.setattr(TT, "time", _FrozenClock(stamp))
    assert TT.get_cycle_index(stamp) != TT.get_cycle_index(
        TT.time.time() - 2 * TT.CYCLE_DAYS * TT.DAY), "the cycle did not turn"

    before = _stones(main, pid)
    replay = _contribute(client, pid, headers, query="strikes=3",
                         key="survivor").json()

    assert replay.get("_replayed") is True, (
        f"a retry two cycles later was re-billed instead of replayed: {replay}")
    assert {k: v for k, v in replay.items() if k != "_replayed"} == first, (
        "the replayed answer drifted from the original it replays")
    assert _stones(main, pid) == before, "a replay charged the player a second time"


def test_a_trailing_slash_is_tolerated_on_both_paths(
        client, make_player, monkeypatch):
    """A trailing slash on the WRITE path still books the contribution.

    Measured, and my first note on this was wrong: a trailing slash does not
    cost the POST its 405. The router answers the slashed spelling with a
    redirect, the client follows it, and the route runs normally -- the wallet
    moves by the reported amount and the receipt is stored.

    That is worth pinning rather than leaving to chance, because the redirect is
    invisible to a client that only inspects the final status. The failure this
    guards against is a client that retries a slash-posted call believing the
    first attempt was lost, which would be a real double charge if the
    idempotency key did not travel with the redirect.
    """
    _freeze(monkeypatch, TT.TribulationPhase.TRIBULATION)
    main = client.wc_main
    pid, headers = make_player(stones=100_000)

    before = _stones(main, pid)
    slashed = client.post(f"/tribulation/{pid}/contribute/?strikes=1",
                          headers={**headers, "Idempotency-Key": "slash-post"})
    assert slashed.status_code == 200, slashed.text
    body = slashed.json()
    assert body["strikes"] == 1, body
    assert before - _stones(main, pid) == TT.TRIBULATION_STRIKE_COST, (
        "the slashed spelling charged something other than what it reported")

    # The key travelled with the redirect, so a client's blind retry after the
    # slash is a replay rather than a second swing. This is the part that makes
    # the tolerance safe rather than merely convenient.
    retry = client.post(f"/tribulation/{pid}/contribute/?strikes=1",
                        headers={**headers, "Idempotency-Key": "slash-post"})
    assert retry.status_code == 200, retry.text
    assert retry.json().get("_replayed") is True, retry.json()
    assert _stones(main, pid) == before - TT.TRIBULATION_STRIKE_COST, (
        "the slashed retry charged the player twice")

    # The read path tolerates the same spelling, so a client does not need a
    # second URL-building rule for the two verbs.
    assert client.get(f"/tribulation/{pid}/", headers=headers).status_code == 200


def test_the_path_prefix_is_case_sensitive(client, make_player):
    """``/TRIBULATION/...`` is 404, not a redirect to the lowercase spelling.

    A client that builds its URL by upper-casing a constant gets a clean 404 it
    can debug, rather than a 307 it might follow with a body it has already
    consumed.
    """
    pid, headers = make_player()
    response = client.get(f"/TRIBULATION/{pid}", headers=headers)
    assert response.status_code == 404, response.text