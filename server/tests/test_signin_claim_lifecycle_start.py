"""Guards the sign-in claim fix. Runs against the REAL route, no source reading.

Each test states a CONTRACT between two pieces of observed data, so it survives
a refactor and cannot pass by snapshotting a literal.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

# Auth: /sign-in/{pid}/claim is guarded, so each claim below is made AS the
# player the test created. See server/tests/wc_test_auth.py.
from server.tests.wc_test_auth import Session, clear_signup_rate_limit


# /player/create carries a PROCESS-WIDE signup rate limit (main.py
# _SIGNUP_ATTEMPTS, 5 creations per 60s per address). Every test here creates a
# player from the same in-process test client, so a file with more than five
# player-creating tests exhausts the window and the later tests fail at the
# create call, before the assertion under test ever runs. The limit is proven by
# its own dedicated tests; clearing the counters here is what keeps this file
# about its own subject. Same treatment as server/tests/test_daily_boss_grief.py.
@pytest.fixture(autouse=True)
def _clear_signup_rate_limit():
    clear_signup_rate_limit()
    import server.main as _main

    for attr in ("_SIGNUP_ATTEMPTS", "_SIGNUP_WINDOW_START"):
        bucket = getattr(_main, attr, None)
        if hasattr(bucket, "clear"):
            bucket.clear()
    yield

SIGN_IN_CLAIM = "/sign-in/{pid}/claim"
CREATE = "/player/create"


@pytest.fixture()
def client():
    from player_store import PlayerStore
    import main

    main.USE_PG = False
    wallet = main.InMemoryWalletStore()
    main.wallet_store = wallet
    main.players_db = PlayerStore(wallet)
    return Session(TestClient(main.app)), main


def _create(client, main, name):
    r = client.post(CREATE, json={"name": name, "spirit_root": "water"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # Every later call in the test rides this session.
    client.sign_in(pid)
    return pid


def test_a_new_players_first_claim_succeeds(client):
    """The reported blocker: create -> first-ever sign-in claim must be 200."""
    c, main = client
    pid = _create(c, main, "FirstClaim")

    response = c.post(SIGN_IN_CLAIM.format(pid=pid))

    assert response.status_code == 200, response.text
    assert response.json()["streak_day"] == 1


def test_the_claim_really_starts_the_clock_rather_than_bypassing_it(client):
    """A 200 is not enough: the lifecycle must exist and be a real 30-day life.

    This is the half a status-code-only test misses. A route that deleted the
    guard and paid straight through would also answer 200 while leaving the
    player with no lifecycle at all.
    """
    c, main = client
    pid = _create(c, main, "ReallyStarted")

    c.post(SIGN_IN_CLAIM.format(pid=pid))

    lc = main.players_db[pid]["lifecycle"]
    assert lc is not None, "the claim returned 200 but no lifecycle was started"
    assert lc["status"] == "active"
    days = (lc["dies_at"] - time.time()) / 86400
    assert 29.0 < days <= 30.0, f"expected a fresh 30-day life, got {days:.2f} days"


def test_a_first_claim_grants_no_retroactive_offline_windfall(client):
    """Starting the clock must stamp the claim cursor to now.

    ``last_cultivation_time`` is what the offline-cultivation settlement pays
    over. If initialisation left it stale, the brand-new player would be handed
    an offline windfall for time they were never alive.
    """
    c, main = client
    pid = _create(c, main, "NoWindfall")

    c.post(SIGN_IN_CLAIM.format(pid=pid))

    drift = abs(main.players_db[pid]["last_cultivation_time"] - time.time())
    assert drift < 5.0, (
        "initialisation left the cultivation cursor stale; the player would "
        f"collect {drift:.0f}s of retroactive offline progress"
    )


def test_a_replay_on_the_same_day_does_not_pay_twice(client):
    """Starting the clock must not turn the route into a repeatable payout.

    The claim total after two calls must equal the total after one call.
    """
    c, main = client
    pid = _create(c, main, "NoDoublePay")

    first = c.post(SIGN_IN_CLAIM.format(pid=pid))
    second = c.post(SIGN_IN_CLAIM.format(pid=pid))

    assert first.status_code == second.status_code == 200
    assert first.json()["cycle_total_qi"] == second.json()["cycle_total_qi"]
    assert first.json()["streak_day"] == second.json()["streak_day"] == 1


def test_an_existing_player_is_not_re_initialised_by_a_claim(client):
    """The fix is a day-one path only; an established player must be untouched."""
    c, main = client
    pid = _create(c, main, "Existing")
    c.get(f"/lifecycle/{pid}")
    before = dict(main.players_db[pid]["lifecycle"])

    c.post(SIGN_IN_CLAIM.format(pid=pid))

    after = main.players_db[pid]["lifecycle"]
    assert after["life_index"] == before["life_index"]
    assert after["born_at"] == before["born_at"]


def test_an_unknown_player_is_still_refused(client):
    """The fix must not turn a missing player into a 200. It answers 401.

    MEASURED 2026-10-05 against the real app. ``auth.authenticate`` confirms
    the session's player still exists, so a token minted for an account that
    was never stored is revoked and refused 401 before this route runs. That
    refusal is the deleted-player defense and it is deliberately stronger than
    the 404 the handler declares: a caller must not be able to learn whether
    an id is live, and a token must stop working the moment its account goes.

    Signing in AS the id being looked up no longer reaches the handler's own
    404 -- the liveness check answers first, which is why this asserts 401.
    """
    c, _ = client

    c.sign_in("no-such-player")
    response = c.post(SIGN_IN_CLAIM.format(pid="no-such-player"))

    assert response.status_code == 401, response.text
