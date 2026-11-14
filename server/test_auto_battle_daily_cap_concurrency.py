"""CONCURRENCY guard for the auto-battle daily stone cap in server/main.py.

Why this file exists
--------------------
A previous worker closed the unauthenticated /combat/auto-battle currency mint,
added a per-day cap, and reported in its own words:

    "The two auto-battle tests pass even with my fix reverted, so they do not
     actually guard that route."

That is the gap this file fills. Those two tests
(server/tests/test_auto_battle_daily_cap.py) fire their battles in a `for`
loop. A sequential loop cannot observe a read-modify-write race, so removing
the concurrency fix changes nothing they measure. They guard the CAP. They do
not guard the TRANSACTION that makes the cap hold under load.

The defect
----------
`_auto_battle_stone_grant` (server/main.py) charges the daily budget with a
read-modify-write on the player row:

    spent   = int(ledger.get("stones", 0) or 0)     # READ
    granted = min(amount, CAP - spent)              # DECIDE
    ledger["stones"] = spent + granted              # WRITE

Nothing inside that function is atomic. It is safe only because the route at
server/main.py wraps the read, the battle, the grant and the balance write in
one `players_db.transaction()` scope, and `PlayerStore.transaction()` holds a
re-entrant lock for the whole scope. Remove the wrapper and two overlapping
wins each read the pre-win ledger, each decide the full per-win payout is
affordable, and the day is charged twice.

How this test makes the race deterministic -- and why that was necessary
------------------------------------------------------------------------
The obvious test fires N concurrent requests and hopes they interleave inside
the grant. Measured, that does not work, and the reason is worth recording:

  * A 24-request burst against a build with the wrapper REMOVED paid exactly
    the 800-stone cap, 12 runs out of 12.
  * The requests genuinely overlap -- instrumenting the store showed all 24
    inside the route at once, 6 of 6 in a 6-request run.
  * The race still never fires, because the gap between the read and the write
    is a handful of bytecodes, far shorter than CPython's interpreter switch
    interval. The Global Interpreter Lock lets one thread run the whole
    read-modify-write before another is scheduled.

So a test that merely aims concurrent traffic at the route is a coin flip that
lands "pass" almost every time -- the exact failure mode this file was written
to close. The test therefore WIDENS THE WINDOW instead of hoping for one.

The ledger is a plain dict on the player row and the production code reads it
with `ledger.get("stones")`. `_SlowLedger` below substitutes a dict subclass
that samples the current value and only then sleeps. The sample happens BEFORE
the delay, so every thread that arrives during the delay sees the ledger as it
was when it arrived. That is precisely the "all of them read the pre-win
ledger" state the transaction wrapper makes impossible.

This widens the read; it does not change what is being decided. The grant
function, the cap, the store, the route and the request path are all the real
ones. A slow read is what a scheduler preemption or a slow disk would produce
in production anyway.

Measured, both directions, on an isolated copy of the tree:

    wrapper REMOVED   24 wins paid 2400 stones against an 800 cap  (4 runs)
    wrapper PRESENT    8 wins paid  800 stones against an 800 cap  (4 runs)

The fixed build pays 8 wins because the day allows 8; the reverted build pays
all 24 because each one read an unspent ledger.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from server.test_auth_helpers import auth_headers  # noqa: E402

#: Concurrent wins fired at one player. The day pays 800 stones at 100 a win,
#: so a correct build can pay 8 of them. 24 leaves a wide margin: the reverted
#: build pays all 24, which is 2400 stones against an 800 cap.
CONCURRENT_WINS = 24

#: How long the ledger read takes. Long enough that CPython must schedule other
#: threads inside the read-modify-write; short enough that the test stays fast.
#: Measured: the fixed build serialises on the store lock and takes ~1.2s for
#: 24 requests at this setting.
SLOW_READ_SECONDS = 0.05

#: A legal, attacker-shaped body. `enemy_stats`, `max_turns` and `seed` are
#: parsed for wire compatibility and then IGNORED by the route, so this test
#: cannot buy a win by asking for one -- the win is forced by the player's own
#: stats in `_seed_deterministic_winner` below.
BATTLE_BODY = {
    "enemy_stats": {"hp": 1, "atk": 1, "def": 0, "spd": 1},
    "max_turns": 1,
    "seed": 1,
    "rewards": {"spirit_stone": 100},
}


class _SlowLedger(dict):
    """The daily-cap ledger with a read that costs real wall-clock time.

    `_auto_battle_stone_grant` reads it with `ledger.get("stones")`. Sampling
    the value BEFORE the sleep is what makes this an instrument rather than a
    change of behaviour: a thread that arrives during another thread's sleep
    reads the ledger as it was on arrival, which is the stale read the
    transaction wrapper is supposed to prevent.
    """

    def get(self, key, default=None):
        value = super().get(key, default)
        if key == "stones":
            time.sleep(SLOW_READ_SECONDS)
        return value


@pytest.fixture(scope="module")
def client():
    from server.main import app

    with TestClient(app) as c:
        yield c


def _authed(client, name: str) -> tuple[str, dict]:
    """Create a player through the API and mint a session for them."""
    r = client.post("/player/create", json={"name": name, "spirit_root": "fire"})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], auth_headers(body["id"], body["signing_key"])


def _seed_deterministic_winner(client, pid: str) -> None:
    """Make `pid` win EVERY auto-battle, and instrument their daily ledger.

    The server derives the opponent from the player's own realm and stats
    (`_auto_battle_enemy_stats`) and the fight ends when the opponent's hit
    points reach zero. Enormous attack against zero defence kills the generated
    opponent on turn 1 whatever the server's random spread does, so
    `result["winner"] == "player"` is certain.

    This matters for the strength of the test: if the player only sometimes
    won, "the cap held" and "the player kept losing" would be indistinguishable,
    and a guard that cannot tell those apart is not a guard.

    The daily ledger is seeded for TODAY in instrumented form. Seeding today's
    day index matters: `_auto_battle_stone_grant` replaces the ledger with a
    fresh plain dict on the first battle of a new day, which would silently
    swap the instrument out and leave the race unprovable.
    """
    from server.main import players_db

    today = int(time.time() // 86400)
    with players_db.transaction():
        row = players_db[pid]
        row["atk"] = 10_000_000
        row["def_"] = 0
        row["hp"] = 10_000_000
        row["spirit_stones"] = {}
        row["auto_battle_daily"] = _SlowLedger(day=today, stones=0)


def _stone_total(stones: dict) -> int:
    return sum(int(v or 0) for v in (stones or {}).values())


def _read_stones(client, pid: str, hdrs: dict) -> dict:
    """Read the player's stone balances WITH a session.

    Measured 2026-10-05: an unauthenticated GET /player/{id} answers 200
    with a REDACTED body -- id, name and spirit root only, no
    spirit_stones. This read used to send no header, so it returned {} and
    BOTH the before and after readings computed as 0. The daily cap was
    correctly held at 800 stones and this file still went red, because a
    zero balance delta cannot be reconciled against the 800 the server
    reported paying. The session is load-bearing, so a balance-less reply
    is now an assertion instead of a silent zero that hides every real
    measurement in this file.
    """
    r = client.get(f"/player/{pid}", headers=hdrs)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "spirit_stones" in body, (
        f"player read came back redacted, no spirit_stones: {body!r} -- the "
        f"session header was dropped, so every stone delta here would be 0"
    )
    return body.get("spirit_stones", {}) or {}


def _fire_simultaneous_wins(client, pid: str, hdrs: dict, count: int) -> list[int]:
    """Post `count` auto-battles at ONE player, all released at the same instant.

    Returns the `rewards.spirit_stone` each response reported.

    The `threading.Barrier` is what makes this a concurrency test rather than a
    loop: every worker parks until all of them are present, so the requests
    enter the route as a group instead of trickling in one at a time.
    """
    barrier = threading.Barrier(count)

    def one_battle(_i: int) -> int:
        barrier.wait(timeout=60)
        resp = client.post(
            "/combat/auto-battle",
            headers=hdrs,
            json=dict(BATTLE_BODY, player_id=pid),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("winner") == "player", (
            f"a seeded-strong player lost ({body.get('winner')}); a loss pays "
            f"nothing, so this run cannot tell a held cap from a broken one"
        )
        return int((body.get("rewards") or {}).get("spirit_stone", 0) or 0)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(one_battle, range(count)))


def test_concurrent_wins_cannot_exceed_the_daily_cap(client):
    """24 overlapping wins on one player must not pay more than one day's cap.

    This is the guard the previous two tests could not be. They ran their
    battles one at a time, so removing the transaction wrapper left their
    measurements unchanged and they passed against broken code.

    Here the requests genuinely overlap AND the read that precedes the charge
    is slow enough that the interpreter must interleave threads inside the
    read-modify-write. The only thing standing between the caller and a 3x
    over-cap payout is that read-modify-write being atomic -- which is exactly
    what the `players_db.transaction()` scope around the route provides.

    Removing that wrapper makes this fail with stones paid above the cap.
    """
    from server.main import AUTO_BATTLE_DAILY_STONE_CAP, AUTO_BATTLE_REWARDS

    per_win = int(AUTO_BATTLE_REWARDS.get("spirit_stone", 0))
    assert per_win > 0, "auto-battle no longer pays spirit stones; nothing to guard"

    pid, hdrs = _authed(client, "ab-concurrent")
    _seed_deterministic_winner(client, pid)

    before = _stone_total(_read_stones(client, pid, hdrs))
    granted = _fire_simultaneous_wins(client, pid, hdrs, CONCURRENT_WINS)
    after = _stone_total(_read_stones(client, pid, hdrs))

    paid = sum(granted)
    balance_delta = after - before
    wins_paid = sum(1 for g in granted if g > 0)

    # Guard the guard, BEFORE the cap assertion. If nothing was paid at all,
    # every assertion below is vacuously true and this test would pass against a
    # build that simply never pays anybody. This precondition holds on a broken
    # build too, which is what keeps the failure below landing on the cap
    # rather than on this line.
    assert wins_paid > 0, (
        "not one concurrent request was paid, so this run observed no payout "
        "and cannot distinguish a held cap from a broken one"
    )

    # THE assertion. This is the one that must fail when the transaction
    # wrapper is removed, and it must be the line that fails, so a revert is
    # reported as an over-cap payout rather than as some incidental setup
    # complaint.
    assert paid <= AUTO_BATTLE_DAILY_STONE_CAP, (
        f"{wins_paid} overlapping wins paid {paid} stones, over the daily cap of "
        f"{AUTO_BATTLE_DAILY_STONE_CAP}. Concurrent requests each read the "
        f"pre-win ledger and each charged the cap as if the day were unspent, so "
        f"the read-modify-write in _auto_battle_stone_grant is not atomic. The "
        f"players_db.transaction() wrapper around the route in server/main.py "
        f"is what makes it atomic; it is not holding this read and its write "
        f"together."
    )
    assert balance_delta <= AUTO_BATTLE_DAILY_STONE_CAP, (
        f"the player's stone balance grew by {balance_delta} in one day, over "
        f"the cap of {AUTO_BATTLE_DAILY_STONE_CAP} (server reported {paid})"
    )
    # The balance and the reported payouts must agree. If they diverge, a
    # concurrent write was lost even when the total lands under the cap -- the
    # same defect wearing a smaller number.
    assert balance_delta == paid, (
        f"the server reported {paid} stones paid but the balance moved "
        f"{balance_delta}; a concurrent write was lost"
    )


def test_sequential_wins_still_respect_the_cap(client):
    """The one-at-a-time case, kept as the control for the concurrent test.

    A concurrency test that passes because nothing was ever paid is worthless,
    so this pins the same contract with no overlap at all. It also documents
    that the instrument above is what exposes the race: this test uses the same
    player setup but never overlaps, so it passes on both builds.
    """
    from server.main import AUTO_BATTLE_DAILY_STONE_CAP, AUTO_BATTLE_REWARDS

    per_win = int(AUTO_BATTLE_REWARDS.get("spirit_stone", 0))
    if per_win <= 0:
        pytest.skip("auto-battle no longer pays spirit stones")

    pid, hdrs = _authed(client, "ab-sequential")
    _seed_deterministic_winner(client, pid)

    before = _stone_total(_read_stones(client, pid, hdrs))
    battles = AUTO_BATTLE_DAILY_STONE_CAP // per_win + 6
    for _ in range(battles):
        resp = client.post(
            "/combat/auto-battle",
            headers=hdrs,
            json=dict(BATTLE_BODY, player_id=pid),
        )
        assert resp.status_code == 200, resp.text

    delta = _stone_total(_read_stones(client, pid, hdrs)) - before
    assert delta <= AUTO_BATTLE_DAILY_STONE_CAP, (
        f"{battles} sequential wins paid {delta} stones, over the daily cap of "
        f"{AUTO_BATTLE_DAILY_STONE_CAP}"
    )