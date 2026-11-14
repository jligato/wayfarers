"""Regression tests for four defects found in server/world_events.py.

Each one was MEASURED before the fix, and each carries a negative control, so a
green run cannot mean the guard was simply deleted.

D1  A claim that already paid answered 409 once the payout window had closed,
    telling a player who HAD received the money that they had not. The fix reads
    the ledger before the phase gate. Money was never double-paid; the lie was
    about the receipt.
D2  attempt_slot's rate limiter read player["id"] alone while every other entry
    point read id-or-player_id, so a joined player was refused their own first
    fight with 403 "A session is required".
D3  The 409 message read "Rewards open when the event closes, not while it is
    closed" -- self-contradictory, because the payout window is cooldown.
D4  reset_memory_state() cleared _MEM_ATTEMPTS, a dict nothing writes to, and
    left the limiter that actually counts. Four call sites compensated with an
    undocumented second line; the fifth would not have.

Run:
  env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \
      -u PYTHONPATH <python> -m pytest server/tests/test_world_events_regressions.py -q
"""
from __future__ import annotations

import math
import os

for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
if str(_SERVER.parent) not in sys.path:
    sys.path.insert(0, str(_SERVER.parent))

import pytest

import world_events as we
from player_store import PlayerStore


@pytest.fixture(autouse=True)
def clean():
    we.reset_memory_state()
    yield
    we.reset_memory_state()


def _player(pid: str, progress: float = 0.0) -> dict:
    return {"id": pid, "name": pid, "cultivation_realm": "mortal",
            "cultivation_stage": "early", "progress": progress,
            "spirit_stones": 0, "hp": 120, "atk": 20, "def_": 10,
            "mp": 50, "spd": 10}


def _store_with(*players: dict) -> PlayerStore:
    store = PlayerStore()
    with store.transaction():
        for row in players:
            store[row["id"]] = row
    return store


def _prepared(def_key: str = "beast_swarm"):
    """A player who has joined, cleared slot 1, and can claim."""
    store = _store_with(_player(def_key + "-p"))
    player = store[def_key + "-p"]
    defn = we.get_definition(store, def_key)
    epoch = we.next_epoch(defn, we.server_now())
    inst = we.ensure_instance(store, defn, epoch, we.server_now())
    we.enter_event(store, def_key, epoch, player, inst["active_at"] + 1)
    we.attempt_slot(store, def_key, epoch, player, 1, inst["active_at"] + 2)
    return store, player, def_key, epoch, inst


# ---------------------------------------------------------------------------
# D1. A paid claim replays its stored receipt whatever the clock now says
# ---------------------------------------------------------------------------

def test_paid_claim_replays_receipt_after_the_payout_window_closes():
    """The exact sequence that measured the defect, in one test."""
    store, player, def_key, epoch, inst = _prepared()
    paid = we.settle_event(store, def_key, epoch, player,
                           inst["cooldown_at"] + 1)
    assert paid["already_claimed"] is False
    assert paid["receipt"]["progress_points"] == 5.0

    # The retry a flaky client makes minutes later. Before the fix this raised
    # EventRefused(409) and the player was told they had never been paid.
    replay = we.settle_event(store, def_key, epoch, player,
                             inst["closed_at"] + 60)
    assert replay["already_claimed"] is True, \
        "a paid claim must replay its receipt, not refuse"
    assert replay["receipt"]["progress_points"] == 5.0
    assert replay["receipt"]["tier"] == paid["receipt"]["tier"]

    # And the replay pays nothing further. This is the money assertion; the two
    # above are the receipt assertion.
    assert float(store[player["id"]]["progress"]) == 5.0, \
        "a replay must not credit a second time"


def test_unpaid_claim_outside_cooldown_still_refuses_and_pays_nothing():
    """NEGATIVE CONTROL for D1: moving the gate must not open the payout."""
    store, player, def_key, epoch, inst = _prepared()
    with pytest.raises(we.EventRefused) as exc:
        we.settle_event(store, def_key, epoch, player, inst["closed_at"] + 60)
    assert exc.value.status == 409
    assert float(store[player["id"]]["progress"]) == 0.0, \
        "an unpaid claim outside the window must not credit anything"


# ---------------------------------------------------------------------------
# D2. attempt_slot's limiter key
# ---------------------------------------------------------------------------

def test_attempt_accepts_a_player_row_keyed_on_player_id():
    """enter_event already accepted it; attempt_slot answered 403."""
    store = _store_with(_player("alias"))
    defn = we.get_definition(store, "beast_swarm")
    epoch = we.next_epoch(defn, we.server_now())
    inst = we.ensure_instance(store, defn, epoch, we.server_now())

    aliased = dict(store["alias"])
    del aliased["id"]
    aliased["player_id"] = "alias"

    entered = we.enter_event(store, "beast_swarm", epoch, aliased,
                             inst["active_at"] + 1)
    assert entered["already_entered"] is False

    outcome = we.attempt_slot(store, "beast_swarm", epoch, aliased, 1,
                              inst["active_at"] + 2)
    assert outcome["outcome"]["slot"] == 1


def test_attempt_still_refuses_a_row_with_no_identity_at_all():
    """NEGATIVE CONTROL for D2: the limiter must keep refusing anonymous rows."""
    store = _store_with(_player("anon"))
    defn = we.get_definition(store, "beast_swarm")
    epoch = we.next_epoch(defn, we.server_now())
    inst = we.ensure_instance(store, defn, epoch, we.server_now())

    anonymous = dict(store["anon"])
    del anonymous["id"]
    anonymous.pop("player_id", None)
    with pytest.raises(we.WorldEventError) as exc:
        we.attempt_slot(store, "beast_swarm", epoch, anonymous, 1,
                        inst["active_at"] + 2)
    assert exc.value.status == 403


# ---------------------------------------------------------------------------
# D3. The refusal message names the real window
# ---------------------------------------------------------------------------

def test_refusal_message_names_cooldown_and_is_not_self_contradictory():
    store, player, def_key, epoch, inst = _prepared()
    with pytest.raises(we.EventRefused) as exc:
        we.settle_event(store, def_key, epoch, player, inst["closed_at"] + 60)
    message = str(exc.value)
    assert "cooldown" in message, \
        "the message must name the window that actually pays: %r" % message
    assert "not while it is closed" not in message, \
        "the old wording told a closed event it was closed: %r" % message


# ---------------------------------------------------------------------------
# D4. The test seam resets the limiter that counts
# ---------------------------------------------------------------------------

def _recorded_attempts() -> int:
    """Attempts the limiter holds, summed over players.

    ``_attempts`` is a dict keyed by PLAYER, so len() counts players rather than
    calls -- measuring the wrong number is how a real bug reads as fixed.
    """
    return sum(len(bucket) for bucket in we.ATTEMPT_LIMITER._attempts.values())


def test_reset_memory_state_clears_the_shared_rate_limiter():
    for _ in range(3):
        we.ATTEMPT_LIMITER.check("seam-probe")
    assert _recorded_attempts() == 3, "the probe must actually record something"
    we.reset_memory_state()
    assert _recorded_attempts() == 0, \
        "the seam must clear the deque that counts, not an empty dict"


def test_rate_limiter_still_refuses_past_its_cap():
    """NEGATIVE CONTROL for D4: resetting more must not mean limiting less."""
    limiter = we.AttemptRateLimiter(limit=3, window_seconds=600.0)
    for _ in range(3):
        limiter.check("ctl")
    with pytest.raises(we.EventRateLimited) as exc:
        limiter.check("ctl")
    assert exc.value.status == 429

# ---------------------------------------------------------------------------
# D5. The once-only key was a CALLER-SUPPLIED key
# ---------------------------------------------------------------------------

def _invented_epochs(def_key: str, claim_at: float, count: int = 5) -> list:
    """Distinct caller-invented epochs whose cooldown window all contains ``claim_at``.

    ``beast_swarm`` warns for 2h, is active 24h, then pays for 6h. An epoch
    offset by warn+active lands its cooldown on ``claim_at``, and stepping the
    offset by an hour keeps every one of them inside that same 6h window --
    which is what makes them several DISTINCT keys for one real occurrence.
    """
    return [claim_at - 26.5 * 3600.0 - i * 3600.0 for i in range(count)]


def test_invented_epochs_cannot_mint_extra_claims():
    """MEASURED before the fix: five invented epochs paid 25.0, not 5.0.

    ``epoch_start`` is one third of the ledger's primary key and it arrives in
    the request body, so keying the payout on the raw float let a modified
    client name a fresh occurrence per attempt and be paid every time. The key
    is now the SERVER'S anchor, so every invented value inside one occurrence
    collapses onto one ledger row.
    """
    claim_at = 1_800_000_000.0
    store, player, def_key, _epoch, _inst = _prepared()
    we.ATTEMPT_LIMITER.reset()

    defn = we.get_definition(store, def_key)
    real = _invented_epochs(def_key, claim_at, count=1)[0]
    we.ATTEMPT_LIMITER.reset()
    we.enter_event(store, def_key, real, player, real + 1.0)
    we.attempt_slot(store, def_key, real, player, 1, real + 3.0)
    first = we.settle_event(store, def_key, real, player, claim_at)
    assert first["already_claimed"] is False, \
        "the control claim must pay, or the mint assertions below are vacuous"

    # Every invented value names a DIFFERENT key: the raw floats are distinct,
    # so a guard that keys on the raw float cannot collapse them. This is what
    # makes the assertions below mean something.
    epochs = [e for e in _invented_epochs(def_key, claim_at, 5) if e != real]
    assert len(set(epochs)) == len(epochs)
    assert len({we.ledger_key(def_key, e, player["id"])[1] for e in epochs}) == len(epochs)

    won = 0
    for e in epochs:
        we.ATTEMPT_LIMITER.reset()
        part = we.get_participation(store, def_key, e, player["id"])
        if part is None:
            we.enter_event(store, def_key, e, player, e + 1.0)
            we.attempt_slot(store, def_key, e, player, 1, e + 3.0)
        r = we.settle_event(store, def_key, e, player, claim_at)
        won += 0 if r["already_claimed"] else 1

    assert won == 0, (
        "%d invented epochs each won a payout; the once-only key is still "
        "caller-chosen" % won)
    assert float(store[player["id"]]["progress"]) == 5.0, (
        "the player was paid more than the one real occurrence owes")


def test_anchor_lattice_is_idempotent_on_a_real_anchor():
    """CONTROL: canonicalising the scheduler's own anchor must change nothing.

    Without this, a fix that snapped every epoch somewhere would pass the mint
    test by breaking every honest claim.
    """
    store, _player_row, def_key, _e, _i = _prepared()
    defn = we.get_definition(store, def_key)
    for anchor in (we.next_epoch(defn, we.server_now()),
                   we.utc_midnight(we.server_now()) + 86400.0,
                   we.utc_midnight(we.server_now())):
        assert we.anchor_lattice(defn, anchor) == anchor, (
            "a real anchor %r was moved to %r" % (anchor, we.anchor_lattice(defn, anchor)))


def test_a_claim_on_the_real_anchor_still_pays_exactly_once():
    """CONTROL: the guard must not be satisfied by refusing every claim."""
    store, player, def_key, epoch, inst = _prepared()
    paid = we.settle_event(store, def_key, epoch, player, inst["cooldown_at"] + 1)
    assert paid["already_claimed"] is False
    assert paid["receipt"]["progress_points"] == 5.0
    replay = we.settle_event(store, def_key, epoch, player, inst["cooldown_at"] + 2)
    assert replay["already_claimed"] is True
    assert float(store[player["id"]]["progress"]) == 5.0


def test_anchor_lattice_never_raises_on_a_degenerate_epoch():
    """A NaN epoch must still be a refusal, not a 500 out of math.floor."""
    store, _player_row, def_key, _e, _i = _prepared()
    defn = we.get_definition(store, def_key)
    for bad in (float("nan"), float("inf"), float("-inf")):
        # The contract is PASS-THROUGH, not a value: the non-finite value must
        # reach the phase gate unchanged so the gate (not math.floor) refuses
        # it. NaN is checked with math.isnan because NaN != NaN.
        out = we.anchor_lattice(defn, bad)
        assert math.isnan(out) if math.isnan(bad) else out == bad, (
            "a non-finite epoch was rewritten to %r before the phase gate saw "
            "it" % out)
