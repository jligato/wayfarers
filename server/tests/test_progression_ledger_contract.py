"""Contract tests for ``server/progression_ledger.py``.

WHAT IS UNDER TEST
------------------
``progression_ledger.py`` exposes NO HTTP routes. It is a data layer called from
route modules, so this file tests the seven exported functions directly:

    reserve_claim        claim (player, milestone, period) -> (first?, receipt)
    record_claim_reward  store what that claim paid
    release_claim        undo a reservation whose payout failed
    claimed_keys         every (milestone, period) a player holds
    has_claimed          one key, without the whole ledger
    read_progression     one axis row, normalised
    record_axis_progress write step + catch-up counters

THE THREE THINGS THIS FILE IS BUILT AROUND
------------------------------------------
1. AN APPEND-ONLY LEDGER READS BACK WHAT WAS WRITTEN.  A claim is a receipt: the
   first attempt records what it paid, and every replay is answered with that
   same figure. Not a synthesised zero, not a freshly computed guess.

2. A LIFETIME TOTAL IS NOT COMPUTED FROM A TRUNCATED LIST.  ``catchup_daily_claims``
   is a lifetime counter. The module derives it by COUNTING every matching ledger
   row, never by taking the length of a bounded slice. The test writes 300 claims
   and demands 300 back, so any ``[:N]``, ``maxlen``, or "only the newest N rows"
   cap turns it red.

3. A MISSING KEY DOES NOT SILENTLY READ AS ZERO.  A player with no ledger row, no
   claim, and no progression row must get a FULL answer -- every documented key
   present, correctly typed -- rather than a dict missing the field the caller
   was about to read, or a bare ``0`` that looks like "you have earned nothing"
   when the truth is "we have no idea".

BACKENDS
--------
``wc_pytest.sh`` strips every ``WC_PG_*`` variable, so the suite runs in the
no-database mode the module explicitly calls its FALLBACK. Both branches are
covered here:

  * the fallback, driven for real against the module's own state, and
  * the PostgreSQL branch, driven through a recording fake connection, so the
    once-only guarantee can be pinned on the statement actually sent to the
    database (the guarantee IS the statement -- there is no live database here
    to prove it against).

Isolation: the module keeps its fallback ledger in a module-level dict, so an
autouse fixture snapshots and restores it around every test. Without that, one
test's claims would silently appear in the next test's counts.
"""

from __future__ import annotations

import importlib
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

progression_ledger = importlib.import_module("progression_ledger")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _NoConnStore:
    """A store with NO database connection, which selects the fallback branch.

    ``current_connection`` must be an ATTRIBUTE and not a method. ``getattr``
    would return a bound method for a method, and ``bound method is not None``
    is True, so a method here would silently push the module onto its
    PostgreSQL path against a stub -- and every assertion below would pass for
    the wrong reason.
    """

    current_connection = None


class _RecordingConn:
    """Records the SQL the module sends; answers from a scripted row queue.

    Every ``fetchone()`` pops the next queued row. A queue exhausted by an extra
    read raises, so an unexpected extra query is a loud failure rather than a
    silent ``None``.
    """

    def __init__(self, queue: Optional[List[Any]] = None) -> None:
        self.calls: List[Tuple[str, Any]] = []
        self._queue = list(queue or [])

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return None

    def fetchone(self):
        if not self._queue:
            raise AssertionError(
                "the module read a row this fake was not told to return; the "
                "statement sequence changed"
            )
        return self._queue.pop(0)

    def fetchall(self):
        if not self._queue:
            return []
        return list(self._queue.pop(0))


class _ConnStore:
    """A store that DOES look connected, so the SQL branch is taken."""

    def __init__(self, queue=None) -> None:
        self.current_connection = _RecordingConn(queue)


@pytest.fixture(autouse=True)
def _isolated_fallback_ledger():
    """Snapshot and restore the module-level fallback ledger.

    ``_MEM_LEDGER`` is process-global on purpose (it stands in for the database).
    Restoring it keeps every test's counts its own.
    """
    saved = dict(progression_ledger._MEM_LEDGER)
    progression_ledger._MEM_LEDGER.clear()
    try:
        yield
    finally:
        progression_ledger._MEM_LEDGER.clear()
        progression_ledger._MEM_LEDGER.update(saved)


PLAYER = "ledger-contract-player"
OTHER = "ledger-contract-other-player"
AXIS = "stone_cloud"


def _claim(player: str, milestone: str, period: str, step: int = 0):
    return progression_ledger.reserve_claim(
        _NoConnStore(), player, milestone, period, step
    )


def _reserve_and_pay(player: str, milestone: str, period: str, reward: dict) -> bool:
    """The full happy path: reserve, then record what was paid."""
    first, _ = _claim(player, milestone, period)
    if first:
        progression_ledger.record_claim_reward(
            _NoConnStore(), player, milestone, period, reward
        )
    return first


# ---------------------------------------------------------------------------
# 1. Write / read round-trip -- an append-only ledger answers with the receipt
# ---------------------------------------------------------------------------

def test_first_claim_is_granted_and_replay_is_refused():
    first, receipt = _claim(PLAYER, "milestone_3", "2026-W40")
    assert first is True, "the first claim on an unused key was refused"
    assert receipt is None, (
        f"the first attempt was handed a receipt ({receipt!r}) before anything "
        f"was paid, so a caller cannot tell a fresh claim from a replay"
    )

    replay_first, replay_receipt = _claim(PLAYER, "milestone_3", "2026-W40")
    assert replay_first is False, (
        "a second claim on the same (player, milestone, period) was granted, so "
        "the ledger pays once per click instead of once per key"
    )
    assert replay_receipt is None, (
        f"a reserved-but-unpaid claim answered {replay_receipt!r} rather than "
        f"None; an empty dict here would read downstream as 'this payout was "
        f"worth zero'"
    )


def test_the_receipt_reads_back_exactly_what_the_first_attempt_wrote():
    reward = {"spirit_stones": 250, "item": "jade_vein", "nested": {"a": 1}}
    _reserve_and_pay(PLAYER, "milestone_1", "2026-W40", reward)

    _first, receipt = _claim(PLAYER, "milestone_1", "2026-W40")
    assert receipt == reward, (
        f"the replay was answered {receipt!r} but the first attempt wrote "
        f"{reward!r}. The stored reward IS the receipt: a client that timed out "
        f"after a real payout must learn what it already received, and a "
        f"synthesised answer is a guess about somebody's money."
    )


def test_a_stored_receipt_is_detached_from_the_dict_the_caller_still_holds():
    """The ledger keeps its own copy. Later mutation of the caller's dict is
    not a retroactive rewrite of history."""
    reward = {"spirit_stones": 100}
    _reserve_and_pay(PLAYER, "milestone_detach", "2026-W40", reward)

    reward["spirit_stones"] = 999_999  # the caller reuses/mutates its dict

    _first, receipt = _claim(PLAYER, "milestone_detach", "2026-W40")
    assert receipt == {"spirit_stones": 100}, (
        f"the receipt changed to {receipt!r} because the CALLER mutated the dict "
        f"it passed in. An append-only record must not be rewritable from the "
        f"outside."
    )


def test_replays_do_not_cross_contaminate_each_other_s_receipt():
    """Ordering: a claim answered with a DIFFERENT claim's reward is a lie."""
    _reserve_and_pay(PLAYER, "milestone_a", "2026-W40", {"spirit_stones": 10})
    _reserve_and_pay(PLAYER, "milestone_b", "2026-W40", {"spirit_stones": 20})

    _f, receipt_a = _claim(PLAYER, "milestone_a", "2026-W40")
    assert receipt_a == {"spirit_stones": 10}, (
        f"milestone_a's replay was answered {receipt_a!r}; receipts are stored in "
        f"a single shared slot, so the most recent claim overwrites an earlier one"
    )
    _f, receipt_b = _claim(PLAYER, "milestone_b", "2026-W40")
    assert receipt_b == {"spirit_stones": 20}


def test_a_replay_preserves_the_receipt_it_read():
    """Reading the ledger back must not consume or blank the receipt."""
    _reserve_and_pay(PLAYER, "milestone_keep", "2026-W40", {"spirit_stones": 7})
    for attempt in range(3):
        _first, receipt = _claim(PLAYER, "milestone_keep", "2026-W40")
        assert receipt == {"spirit_stones": 7}, (
            f"attempt {attempt} read the receipt as {receipt!r}; a repeat read "
            f"emptied it, so the second retry after the first would be told the "
            f"payout was worth zero"
        )


def test_a_replay_reserves_nothing_new():
    """The rejected replay must not add a second entry."""
    _reserve_and_pay(PLAYER, "milestone_once", "2026-W40", {"spirit_stones": 3})
    before = dict(progression_ledger._MEM_LEDGER)
    for _ in range(5):
        _claim(PLAYER, "milestone_once", "2026-W40")
    after = progression_ledger._MEM_LEDGER
    assert len(after) == len(before), (
        f"five rejected replays grew the ledger from {len(before)} to "
        f"{len(after)} entries"
    )


# ---------------------------------------------------------------------------
# 2. Ordering / key identity
# ---------------------------------------------------------------------------

def test_each_of_the_three_key_parts_is_part_of_the_identity():
    """(player, milestone, period): change any one, it is a different claim."""
    _reserve_and_pay(PLAYER, "milestone_k", "2026-W40", {"spirit_stones": 1})
    assert _claim(OTHER, "milestone_k", "2026-W40")[0] is True, (
        "a different PLAYER was refused the claim; the key does not include "
        "player_id"
    )
    assert _claim(PLAYER, "milestone_other", "2026-W40")[0] is True, (
        "a different MILESTONE was refused; the key does not include milestone_id"
    )
    assert _claim(PLAYER, "milestone_k", "2026-W41")[0] is True, (
        "a different PERIOD was refused; the key does not include period_key, so "
        "next week's claim would be permanently burnt"
    )
    assert _claim(PLAYER, "milestone_k", "2026-W40")[0] is False


def test_non_string_identifiers_are_coerced_so_one_player_is_one_player():
    """An id arriving as an int and the same id as a str are ONE key.

    Without the coercion a route that passes ``player_id`` straight from a
    numeric source would get a second entry and pay again.
    """
    first, _ = progression_ledger.reserve_claim(
        _NoConnStore(), 4242, "milestone_int", "2026-W40", 0
    )
    assert first is True
    replay_first, _ = progression_ledger.reserve_claim(
        _NoConnStore(), "4242", "milestone_int", "2026-W40", 0
    )
    assert replay_first is False, (
        "player 4242 and player '4242' are two different keys, so the same "
        "player pays twice depending on which route reached the ledger"
    )


def test_the_key_parts_are_not_concatenated_into_one_string():
    """Key injection: two different triples must not collide as text."""
    _reserve_and_pay(PLAYER, "m:b", "c", {"spirit_stones": 1})
    assert _claim(PLAYER, "m", "b:c")[0] is True, (
        "('m:b','c') and ('m','b:c') are different triples but collide, so the "
        "key is being built by string concatenation somewhere"
    )


def test_claimed_keys_lists_exactly_this_players_claims():
    _reserve_and_pay(PLAYER, "m1", "2026-W40", {"spirit_stones": 1})
    _reserve_and_pay(PLAYER, "m2", "2026-W41", {"spirit_stones": 2})
    _reserve_and_pay(OTHER, "m3", "2026-W40", {"spirit_stones": 3})

    keys = progression_ledger.claimed_keys(_NoConnStore(), PLAYER)
    assert keys == {("m1", "2026-W40"), ("m2", "2026-W41")}, (
        f"claimed_keys returned {keys!r}: another player's claims leaked in, or "
        f"one of this player's is missing"
    )
    assert ("m3", "2026-W40") not in keys


def test_claimed_keys_on_a_player_with_nothing_claimed_is_empty_not_partial():
    keys = progression_ledger.claimed_keys(_NoConnStore(), "player-with-no-ledger")
    assert keys == set(), (
        f"an unclaimed player got {keys!r}; a truthy-but-wrong answer here would "
        f"hide a claimable milestone from claimable_milestones"
    )


def test_has_claimed_agrees_with_claimed_keys():
    _reserve_and_pay(PLAYER, "m_here", "2026-W40", {"spirit_stones": 1})
    store = _NoConnStore()
    assert progression_ledger.has_claimed(store, PLAYER, "m_here", "2026-W40") is True
    for absent in (
        ("m_here", "2026-W41"),
        ("m_elsewhere", "2026-W40"),
    ):
        assert progression_ledger.has_claimed(store, PLAYER, *absent) is False, (
            f"has_claimed said yes for {absent!r}, which is not in the ledger"
        )
    # Cross-check against the whole-ledger read: the two must never disagree.
    listed = progression_ledger.claimed_keys(store, PLAYER)
    for milestone, period in listed:
        assert progression_ledger.has_claimed(
            store, PLAYER, milestone, period
        ) is True
    assert ("m_here", "2026-W40") in listed


# ---------------------------------------------------------------------------
# 3. Totals -- a lifetime count, never a truncated list
# ---------------------------------------------------------------------------

#: Larger than any plausible retention cap (30/50/100/200). A cap introduced
#: anywhere between the ledger and the total shows up as a shortfall here.
CLAIMS_FOR_LIFETIME_TOTAL = 300


def test_a_lifetime_total_counts_every_row_not_a_capped_slice():
    """THE truncated-list test.

    ``read_progression`` derives ``catchup_daily_claims`` by counting ledger rows.
    If that derivation ever takes a bounded slice -- ``[:100]``, a ``deque`` with
    ``maxlen``, "the newest N rows" -- the lifetime total silently becomes a
    recent-window total, and every diminishing-returns curve built on it starts
    paying full size again after the window fills.
    """
    store = _NoConnStore()
    for i in range(CLAIMS_FOR_LIFETIME_TOTAL):
        _reserve_and_pay(PLAYER, f"catchup_daily_{i}", str(i), {"qi": 1})

    total = progression_ledger.read_progression(store, PLAYER, AXIS)["catchup_daily_claims"]
    assert total == CLAIMS_FOR_LIFETIME_TOTAL, (
        f"{CLAIMS_FOR_LIFETIME_TOTAL} claims were written and the ledger holds "
        f"{len(progression_ledger._MEM_LEDGER)} rows, but the lifetime total reads "
        f"{total}. A lifetime total computed from a capped or truncated list is "
        f"not a lifetime total."
    )


def test_the_lifetime_total_is_monotonic_and_never_shrinks():
    store = _NoConnStore()
    seen = []
    for i in range(1, 61):
        _reserve_and_pay(PLAYER, f"catchup_daily_{i}", str(i), {"qi": 1})
        seen.append(progression_ledger.read_progression(store, PLAYER, AXIS)["catchup_daily_claims"])
    assert seen == list(range(1, 61)), (
        f"the total was not monotonically increasing across 60 writes: {seen}"
    )


def test_one_players_lifetime_total_excludes_another_players_claims():
    for i in range(5):
        _reserve_and_pay(PLAYER, f"catchup_daily_{i}", str(i), {"qi": 1})
    for i in range(9):
        _reserve_and_pay(OTHER, f"catchup_daily_{i}", str(i), {"qi": 1})

    store = _NoConnStore()
    assert progression_ledger.read_progression(store, PLAYER, AXIS)["catchup_daily_claims"] == 5
    assert progression_ledger.read_progression(store, OTHER, AXIS)["catchup_daily_claims"] == 9


def test_a_player_with_no_ledger_reads_zero_rather_than_a_wrong_nonzero():
    store = _NoConnStore()
    row = progression_ledger.read_progression(store, "nobody", AXIS)
    assert row["catchup_daily_claims"] == 0, (
        f"a player with an empty ledger read {row!r}"
    )


def test_released_claims_leave_the_lifetime_total():
    """A release undoes a failed payout, so it must not be counted as earned."""
    store = _NoConnStore()
    for i in range(4):
        _reserve_and_pay(PLAYER, f"catchup_daily_{i}", str(i), {"qi": 1})
    assert progression_ledger.read_progression(store, PLAYER, AXIS)["catchup_daily_claims"] == 4

    progression_ledger.release_claim(_NoConnStore(), PLAYER, "catchup_daily_2", "2")
    assert progression_ledger.read_progression(store, PLAYER, AXIS)["catchup_daily_claims"] == 3, (
        "a released claim is still counted as a lifetime claim, so a failed "
        "payout permanently consumes the player's diminishing-returns budget"
    )


def test_record_axis_progress_reports_the_running_total_not_the_delta():
    """The SQL branch ADDs the counters and returns the new running total.

    A caller that reads the return value as the player's lifetime count must get
    the cumulative figure. Handing back only the delta would make the first write
    look like the total.
    """
    conn = _ConnStore([(11, 4)])  # what the DB returns after adding
    out = progression_ledger.record_axis_progress(
        conn, PLAYER, AXIS, 3, daily_claims=2, weekly_claims=1, qi_earned=5.0
    )
    assert out == {"catchup_daily_claims": 11, "catchup_weekly_claims": 4}, (
        f"record_axis_progress returned {out!r}; it must report the running "
        f"total the database holds, not the delta it was handed"
    )
    for key in ("catchup_daily_claims", "catchup_weekly_claims"):
        assert isinstance(out[key], int) and not isinstance(out[key], bool)


# ---------------------------------------------------------------------------
# 4. A missing key must not read as zero
# ---------------------------------------------------------------------------

_PROGRESSION_KEYS = {
    "current_step",
    "highest_step",
    "catchup_daily_claims",
    "catchup_weekly_claims",
    "catchup_qi_earned",
}


def test_a_missing_progression_row_returns_every_documented_key():
    store = _NoConnStore()
    row = progression_ledger.read_progression(store, "player-with-no-row", AXIS)
    assert set(row) == _PROGRESSION_KEYS, (
        f"a player with no progression row got keys {sorted(row)}, not "
        f"{sorted(_PROGRESSION_KEYS)}. A missing key here becomes a silent zero "
        f"at the call site (row.get(k, 0)) and reads as 'earned nothing' when "
        f"the truth is 'unknown'."
    )


def test_an_empty_progression_row_is_fully_typed():
    """Zero is an answer; it must be the right TYPE of answer."""
    row = progression_ledger.read_progression(_NoConnStore(), "no-row-player", AXIS)
    for key in ("current_step", "highest_step", "catchup_daily_claims",
                "catchup_weekly_claims"):
        assert isinstance(row[key], int) and not isinstance(row[key], bool), (
            f"{key} came back as {row[key]!r} ({type(row[key]).__name__}), not an int"
        )
    assert isinstance(row["catchup_qi_earned"], float), (
        f"catchup_qi_earned came back as {row['catchup_qi_earned']!r}, not a float"
    )


def test_a_missing_postgres_progression_row_is_normalised_too():
    """Same contract on the SQL branch: a NULL row is a full zeroed row."""
    conn = _ConnStore([None])
    row = progression_ledger.read_progression(conn, "player-with-no-db-row", AXIS)
    assert set(row) == _PROGRESSION_KEYS
    assert row["current_step"] == 0 and row["highest_step"] == 0
    assert row["catchup_qi_earned"] == 0.0


def test_a_stored_postgres_progression_row_is_coerced_to_the_documented_types():
    """The database may hand back Decimals and numeric strings; the contract
    says ints and float, and a caller doing arithmetic on a Decimal-vs-float
    mismatch is a runtime error, not a rounding one."""
    import decimal

    conn = _ConnStore([(decimal.Decimal("3"), decimal.Decimal("9"),
                        decimal.Decimal("4"), decimal.Decimal("2"),
                        decimal.Decimal("1.5"))])
    row = progression_ledger.read_progression(conn, PLAYER, AXIS)
    assert row == {
        "current_step": 3,
        "highest_step": 9,
        "catchup_daily_claims": 4,
        "catchup_weekly_claims": 2,
        "catchup_qi_earned": 1.5,
    }
    assert isinstance(row["current_step"], int)
    assert isinstance(row["catchup_qi_earned"], float)


def test_recording_a_reward_for_an_unclaimed_key_does_not_invent_a_claim():
    """No write without a reservation. Otherwise a route that records after a
    crashed reserve can manufacture a 'claimed' row for a key nobody paid."""
    store = _NoConnStore()
    progression_ledger.record_claim_reward(
        store, PLAYER, "never-reserved", "2026-W40", {"spirit_stones": 500}
    )
    assert progression_ledger.has_claimed(store, PLAYER, "never-reserved", "2026-W40") is False, (
        "record_claim_reward created a claim out of nothing; a payout receipt "
        "must never be the thing that makes a claim look taken"
    )
    assert progression_ledger.claimed_keys(store, PLAYER) == set()


def test_releasing_a_key_that_was_never_claimed_is_harmless():
    store = _NoConnStore()
    progression_ledger.release_claim(store, PLAYER, "never-existed", "2026-W40")
    assert progression_ledger.claimed_keys(store, PLAYER) == set()


def test_a_release_puts_the_key_back_so_the_payout_can_be_retried():
    """The whole point of release_claim: a failed payout must not burn the
    claim forever."""
    store = _NoConnStore()
    _reserve_and_pay(PLAYER, "milestone_retry", "2026-W40", {"spirit_stones": 1})
    progression_ledger.release_claim(store, PLAYER, "milestone_retry", "2026-W40")

    assert progression_ledger.has_claimed(store, PLAYER, "milestone_retry", "2026-W40") is False
    first, _ = _claim(PLAYER, "milestone_retry", "2026-W40")
    assert first is True, (
        "a released key cannot be claimed again, so one failed payout makes a "
        "milestone permanently unclaimable"
    )


# ---------------------------------------------------------------------------
# 5. Error cases
# ---------------------------------------------------------------------------

def test_a_corrupt_receipt_row_is_treated_as_a_replay_not_a_crash():
    """A reward column that is not valid JSON must degrade to 'no receipt'.

    The module's own rule: a corrupt row is a replay, not a crash. Raising here
    would turn one bad row into a 500 on every retry of that claim.
    """
    import json

    conn = _ConnStore([None, ("{not json at all",)])
    first, receipt = progression_ledger.reserve_claim(
        conn, PLAYER, "milestone_corrupt", "2026-W40", 1
    )
    assert first is False, "an unparseable receipt row was read as a fresh claim"
    assert receipt is None, (
        f"a corrupt receipt was passed through as {receipt!r}; a caller would "
        f"credit that to a player"
    )
    # And a well-formed JSON string in the same column is decoded.
    conn2 = _ConnStore([None, (json.dumps({"spirit_stones": 12}),)])
    _first, receipt2 = progression_ledger.reserve_claim(
        conn2, PLAYER, "milestone_ok", "2026-W40", 1
    )
    assert receipt2 == {"spirit_stones": 12}, (
        f"a JSON-encoded receipt string was not decoded: {receipt2!r}"
    )


def test_an_empty_receipt_reads_as_no_receipt_rather_than_a_zero_payout():
    conn = _ConnStore([None, ({},)])
    _first, receipt = progression_ledger.reserve_claim(
        conn, PLAYER, "milestone_empty", "2026-W40", 1
    )
    assert receipt is None, (
        f"an empty stored receipt was handed back as {receipt!r}; that reads "
        f"downstream as 'this milestone paid nothing'"
    )


def test_a_missing_row_on_the_replay_read_reads_as_no_receipt():
    """The insert conflicted but the follow-up read found nothing (a race with a
    rolled-back row). That is still a replay -- it must not raise, and it must
    not be reported as the first attempt."""
    conn = _ConnStore([None, None])
    first, receipt = progression_ledger.reserve_claim(
        conn, PLAYER, "milestone_gone", "2026-W40", 1
    )
    assert first is False, (
        "a claim whose insert did not return a row was reported as the FIRST "
        "attempt, which is the double-payout"
    )
    assert receipt is None


def test_a_non_dict_reward_is_stored_as_an_empty_receipt_not_raised_on():
    """``record_claim_reward`` accepts ``dict | None``; ``None`` stores {}."""
    store = _NoConnStore()
    assert _claim(PLAYER, "milestone_none", "2026-W40")[0] is True
    progression_ledger.record_claim_reward(store, PLAYER, "milestone_none", "2026-W40", None)
    _first, receipt = _claim(PLAYER, "milestone_none", "2026-W40")
    assert receipt in ({}, None), f"unexpected receipt {receipt!r}"


def test_both_backends_answer_the_same_question_the_same_way():
    """The docstring's promise: the fallback is not a weaker route.

    Same sequence of calls, both backends, compared on the observable answer.
    """
    fallback = _NoConnStore()
    progression_ledger.reserve_claim(fallback, PLAYER, "m_same", "2026-W40", 0)
    progression_ledger.record_claim_reward(fallback, PLAYER, "m_same", "2026-W40", {"x": 1})
    fb_replay = progression_ledger.reserve_claim(fallback, PLAYER, "m_same", "2026-W40", 0)
    fb_has = progression_ledger.has_claimed(fallback, PLAYER, "m_same", "2026-W40")
    fb_keys = progression_ledger.claimed_keys(fallback, PLAYER)

    # SQL branch: the insert conflicts (returns nothing) and the read finds the
    # reward the first attempt stored. has_claimed then reads one row and
    # claimed_keys reads a result set -- four reads, four queued answers.
    conn = _ConnStore([None, ({"x": 1},), ({"x": 1},), [("m_same", "2026-W40")]])
    sql_replay = progression_ledger.reserve_claim(conn, PLAYER, "m_same", "2026-W40", 0)
    sql_has = progression_ledger.has_claimed(conn, PLAYER, "m_same", "2026-W40")
    sql_keys = progression_ledger.claimed_keys(conn, PLAYER)

    assert fb_replay[0] is sql_replay[0] is False
    assert fb_replay[1] == sql_replay[1] == {"x": 1}
    assert fb_has is sql_has is True
    assert fb_keys == sql_keys == {("m_same", "2026-W40")}


# ---------------------------------------------------------------------------
# 6. The PostgreSQL branch: the guarantee IS the statement
# ---------------------------------------------------------------------------
#
# wc_pytest.sh strips every WC_PG_* variable, so there is no live database here.
# The once-only guarantee in this module lives in the SQL text sent to the
# database (`INSERT ... ON CONFLICT DO NOTHING`) -- there is nothing else to
# point at. These tests pin that statement, and pin the shape of the call, so a
# rewrite that drops the conflict clause is red in CI rather than red in
# production.

def test_the_reservation_is_an_insert_that_refuses_the_conflict():
    conn = _ConnStore([("milestone_x",)])
    first, _ = progression_ledger.reserve_claim(
        conn, PLAYER, "milestone_x", "2026-W40", 5
    )
    assert first is True
    assert len(conn.current_connection.calls) == 1, (
        f"expected exactly one statement for a fresh claim, got "
        f"{[c[0] for c in conn.current_connection.calls]}"
    )
    sql = conn.current_connection.calls[0][0]
    assert "INSERT INTO milestone_claim_ledger" in sql, sql
    assert "ON CONFLICT (player_id, milestone_id, period_key) DO NOTHING" in sql, (
        f"the reservation no longer refuses a conflicting key: {sql}. Without "
        f"this clause the conflict RAISES instead of returning no row, and the "
        f"guard becomes an error rather than a replay."
    )
    assert "RETURNING" in sql, (
        f"the reservation cannot tell a fresh insert from a refused one: {sql}"
    )


def test_the_conflicting_key_is_decided_by_the_database_not_by_the_caller():
    """On a conflict the module issues a READ for the stored receipt, and the
    parameters it reads back are the same three key parts -- no caller-supplied
    idempotency key anywhere."""
    conn = _ConnStore([None, ({"spirit_stones": 5},)])
    progression_ledger.reserve_claim(conn, PLAYER, "milestone_y", "2026-W40", 2)
    statements = [c[0] for c in conn.current_connection.calls]
    assert len(statements) == 2, statements
    assert statements[1].startswith("SELECT reward FROM milestone_claim_ledger"), statements[1]
    assert ("milestone_y", "2026-W40") == tuple(
        conn.current_connection.calls[1][1]
    )[1:3]


def test_the_step_index_is_stored_but_never_part_of_the_key():
    """A second attempt at the same key with a DIFFERENT step must still be
    refused: the key is (player, milestone, period) only.

    ``step_index`` is caller-supplied, so if it ever reached the key a route could
    re-open a spent claim by naming a different step. Asserted two ways: the
    fallback builds its key in Python and must refuse; the SQL conflict clause
    must not mention step_index at all.
    """
    store = _NoConnStore()
    first, _ = progression_ledger.reserve_claim(store, PLAYER, "m", "2026-W40", 0)
    second, _ = progression_ledger.reserve_claim(store, PLAYER, "m", "2026-W40", 99)
    assert first is True
    assert second is False, (
        "changing step_index produced a different claim; the caller controls "
        "step_index and must not be able to re-open a spent key with it"
    )
    assert progression_ledger.claimed_keys(store, PLAYER) == {("m", "2026-W40")}

    insert_sql = " ".join(progression_ledger._LEDGER_SQL.split())
    import re

    conflict_target = re.search(r"ON CONFLICT\s*\(([^)]*)\)", insert_sql, re.I)
    assert conflict_target is not None, f"no ON CONFLICT clause: {insert_sql}"
    target = [c.strip() for c in conflict_target.group(1).split(",")]
    assert target == ["player_id", "milestone_id", "period_key"], (
        f"the conflict target is {target!r}, so step_index is part of the claim "
        f"key and a caller can re-open a spent claim by naming a different step"
    )
    assert "step_index" in insert_sql, (
        f"the step is no longer recorded at all: {insert_sql}"
    )


def test_the_high_water_mark_is_a_greatest_not_an_assignment():
    """``highest_step`` must never move backwards. A prestige that resets
    ``current_step`` must not erase the evidence the player ever got there."""
    sql = " ".join(progression_ledger._PROGRESSION_UPSERT.split())
    assert "highest_step = GREATEST(" in sql, (
        f"highest_step is assigned rather than max-ed: {sql}"
    )


def test_the_catch_up_counters_add_rather_than_assign():
    """Monotonic by construction: a prestige cannot hand out a fresh set of
    full-size payouts."""
    sql = " ".join(progression_ledger._PROGRESSION_UPSERT.split())
    assert "catchup_daily_claims = player_progression.catchup_daily_claims + EXCLUDED" in sql, sql
    assert "catchup_weekly_claims = player_progression.catchup_weekly_claims + EXCLUDED" in sql, sql


def test_record_claim_reward_writes_through_the_same_key():
    conn = _ConnStore()
    progression_ledger.record_claim_reward(conn, PLAYER, "m", "2026-W40", {"a": 1})
    statements = [c[0] for c in conn.current_connection.calls]
    assert len(statements) == 1, statements
    assert statements[0].startswith("UPDATE milestone_claim_ledger SET reward"), statements[0]
    params = conn.current_connection.calls[0][1]
    assert params[1:] == (PLAYER, "m", "2026-W40"), (
        f"the receipt was written against {params[1:]!r}, not the key it was "
        f"given, so the receipt lands on the wrong row"
    )


# ---------------------------------------------------------------------------
# 7. DECISION: one key, one receipt, forever
# ---------------------------------------------------------------------------

def test_a_second_write_for_one_key_is_ignored_because_the_receipt_is_the_paid_record():
    """DECISION: the FIRST write for a key wins. A second one is IGNORED.

    WHY IGNORED AND NOT REPLACED. The stored reward is not a status flag, it is
    the record of money that ALREADY MOVED, and it is the only thing a retry is
    answered from. Replacing it does not pay twice -- the once-only PRIMARY KEY
    has already stopped that -- it makes the ledger report a payout nobody made.
    A client that timed out after a real claim would be told it received a
    figure it never received, and the money it actually got would be
    unrecoverable from the server's own account of the event.

    Nothing legitimate needs the replace: the claim route calls this function
    only on the branch where ``reserve_claim`` reported the FIRST attempt, and a
    retry takes the replay branch and returns before reaching it. So a second
    write can only be a caller bug, a retry loop, or a refactor that moved the
    call out from under ``if first`` -- and on a money path the safe reading of
    a bug is "ignore it", exactly as the sibling release guard ignores a release
    of a key that already holds a paid receipt.

    BOTH BACKENDS. Overwrite was reachable in the fallback and in the SQL, and
    the two disagreeing would mean the no-database mode is the weaker route --
    the one thing this module's header forbids. The fallback check is
    behavioural and runs against real module state. The database branch cannot
    be observed without a live server, so it is pinned on the statement itself,
    which is where this file pins the rest of the once-only guarantee: the fill
    must refuse any row whose receipt is not still empty.
    """
    store = _NoConnStore()
    assert _claim(PLAYER, "m_immutable", "2026-W40")[0] is True
    progression_ledger.record_claim_reward(
        store, PLAYER, "m_immutable", "2026-W40", {"qi": 100.0}
    )
    # The bug this pins: a second write used to replace the first receipt here.
    progression_ledger.record_claim_reward(
        store, PLAYER, "m_immutable", "2026-W40", {"qi": 9999.0}
    )

    _first, receipt = _claim(PLAYER, "m_immutable", "2026-W40")
    assert receipt == {"qi": 100.0}, (
        f"the replay is answered {receipt!r} but the first attempt paid "
        f"{{'qi': 100.0}}. A receipt that a later call can overwrite is a status "
        f"flag, not a record of what was paid, so every retry after that call "
        f"is answered with a payout that never happened."
    )

    fill_sql = " ".join(progression_ledger._LEDGER_FILL_SQL.split())
    assert "AND reward = '{}'::jsonb" in fill_sql, (
        f"the receipt fill has no 'only if still empty' guard, so a second "
        f"record_claim_reward overwrites a paid receipt in the database: "
        f"{fill_sql}"
    )


# ---------------------------------------------------------------------------
# 8. AUTH: a refused caller must not burn a claim
# ---------------------------------------------------------------------------
#
# progression_ledger.py exposes no HTTP route of its own, so "the ledger
# requires auth" is not a property of this file -- it is a property of the one
# route that spends from it. POST /cultivation-ladder/claim calls
# reserve_claim, and it derives player_id from the SESSION, never from the body.
# The threat this pins is specific and not generic:
#
#   If a claim is reserved BEFORE the session is checked, then an anonymous or
#   junk-token caller BURNS the key. The claim then answers as "already paid"
#   to the real player who presses the button afterwards, and the reward is
#   never paid to anyone. A 401 that leaves a ledger row behind is not a safe
#   refusal, it is a denial of service with an authentication error attached.
#
# So the assertions below are on the LEDGER, not on the status code alone. The
# status proves the guard fired; the row count proves it fired BEFORE the
# reserve. Both are needed: a route that reserves then 401s passes a
# status-only test.
#
# WHY THE IMPORT IS INSIDE THE FIXTURE
# -------------------------------------
# cultivation_routes pulls in a wide import graph, so a peer mid-edit elsewhere
# on this shared tree can break it. Importing at module scope would take the
# other 37 assertions in this file down with it. Importing inside the fixture
# keeps the blast radius on the auth section, and a FAILED import fails these
# tests loudly -- it is never converted into a skip, because a silently skipped
# auth assertion reads as "auth verified" at the next review.

def _mount_ledger_caller():
    """Mount the one route that spends from this ledger, on a bare app."""
    import auth  # noqa: F401 - imported for its session issuer
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from player_store import PlayerStore

    import cultivation_routes as cr

    players_db = PlayerStore()
    app = FastAPI()
    cr.register_cultivation_routes(app, lambda: players_db)
    return TestClient(app, raise_server_exceptions=False)


def test_an_anonymous_claim_is_refused_and_burns_no_claim_key():
    client = _mount_ledger_caller()
    progression_ledger._MEM_LEDGER.clear()

    resp = client.post("/cultivation-ladder/claim", json={"milestone_id": "catchup_daily"})
    assert resp.status_code == 401, (
        f"an anonymous claim answered {resp.status_code}, not 401. This route "
        f"names no player in its path, so the shape-based ownership guard "
        f"skips it by design and the hand-wired session check is the only thing "
        f"standing here."
    )
    assert progression_ledger._MEM_LEDGER == {}, (
        f"the refused call left {list(progression_ledger._MEM_LEDGER)!r} in the "
        f"ledger. A 401 that reserves first BURNS the key: the real player then "
        f"presses the button, is told 'already paid', and nobody receives the "
        f"reward."
    )


def test_a_junk_token_claim_is_refused_and_burns_no_claim_key():
    """A malformed token is ANONYMOUS (401), not a stranger (403).

    Probing the stranger arm with junk is the classic way to make a correctly
    guarded route look open, so the token check and the reservation check are
    separate assertions here rather than one status comparison.
    """
    client = _mount_ledger_caller()
    progression_ledger._MEM_LEDGER.clear()

    resp = client.post(
        "/cultivation-ladder/claim",
        json={"milestone_id": "catchup_daily"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401, (
        f"a junk token answered {resp.status_code}; it must be treated as "
        f"anonymous rather than as a valid session"
    )
    assert progression_ledger._MEM_LEDGER == {}, (
        f"a junk-token call left {list(progression_ledger._MEM_LEDGER)!r} behind"
    )


def test_the_ledger_spending_route_carries_a_session_dependency():
    """The guard is declared, not merely observed once.

    Uses the guard module's OWN predicate (``has_auth_dependency``) rather than
    a second copy of it: a re-implementation would agree with itself even if
    the guard changed shape, which is exactly the drift this file exists to
    catch. This mirrors the mount-time assertion the route module itself makes.

    NOTE THE ARGUMENT. ``has_auth_dependency`` takes a DEPENDANT and walks that
    object's ``dependencies``; handing it the route itself walks a missing
    attribute and answers False for a route that is guarded -- measured, and the
    reason this test failed on its first run while the 401 arms passed.
    """
    import ownership_guard

    client = _mount_ledger_caller()
    claim_route = next(
        r for r in client.app.routes
        if getattr(r, "path", None) == "/cultivation-ladder/claim"
    )
    assert ownership_guard.has_auth_dependency(claim_route.dependant), (
        "POST /cultivation-ladder/claim is the only door onto this ledger and it "
        "no longer declares an auth dependency, so a caller with no session "
        "would reach reserve_claim directly"
    )
    assert "require_player" in [
        ownership_guard._dep_name(d)
        for d in (claim_route.dependant.dependencies or ())
    ], (
        "the claim route's only auth dependency is no longer require_player; "
        "something else is now standing in front of the ledger"
    )


def test_a_claim_for_an_unknown_player_creates_no_ledger_row():
    """A valid session naming a player who does not exist is not a mint.

    The session is real, so this is past the 401 -- the risk here is that the
    reserve runs before the row lookup, and a typo'd or deleted player silently
    banks claims nothing can ever claim.
    """
    import auth

    client = _mount_ledger_caller()
    progression_ledger._MEM_LEDGER.clear()

    resp = client.post(
        "/cultivation-ladder/claim",
        json={"milestone_id": "catchup_daily"},
        headers={"Authorization": "Bearer " + auth.issue_session("no-such-player")},
    )
    assert resp.status_code >= 400, (
        f"a session for a player who does not exist was answered "
        f"{resp.status_code} with body {resp.text[:200]!r}; it must not be "
        f"treated as a live account"
    )
    assert progression_ledger._MEM_LEDGER == {}, (
        f"a claim for a non-existent player left {list(progression_ledger._MEM_LEDGER)!r}"
    )


# ---------------------------------------------------------------------------
# 9. Non-finite values are rejected on BOTH the read and the write
# ---------------------------------------------------------------------------
#
# catchup_qi_earned is DOUBLE PRECISION, a type PostgreSQL happily accepts
# 'Infinity' and 'NaN' into, and Python's float() RETURNS a non-finite value
# rather than raising. So an unguarded conversion does not fail loudly -- it
# hands infinity to a screen that renders it as a real number.
#
# WHY THE WRITE PATH MATTERS AS MUCH AS THE READ. The upsert ACCUMULATES that
# column (existing + EXCLUDED), and inf + 1.0 and nan + 1.0 are both still
# non-finite. One bad write therefore poisons the player's lifetime total
# PERMANENTLY: every later correct claim adds to it and recovers nothing.
# Guarding only the read would let the poison in through the write.

#: The three shapes that fail in three DIFFERENT ways. int(float("inf"))
#: raises OverflowError, int(float("nan")) raises ValueError, and Decimal("NaN")
#: raises ValueError -- so a guard catching only (TypeError, ValueError) leaks
#: a THIRD exception that turns the whole route into a server error.
NON_FINITE_INPUTS = (
    ("float inf", float("inf")),
    ("float -inf", float("-inf")),
    ("float nan", float("nan")),
    ("decimal NaN", Decimal("NaN")),
    ("decimal Infinity", Decimal("Infinity")),
    ("decimal -Infinity", Decimal("-Infinity")),
    ("string Infinity", "Infinity"),
    ("string NaN", "NaN"),
)


@pytest.mark.parametrize("label,value", NON_FINITE_INPUTS)
def test_a_non_finite_value_never_becomes_a_step(label, value):
    assert progression_ledger._int(value) == 0, (
        f"{label} ({value!r}) converted to {progression_ledger._int(value)!r} "
        f"instead of the unreadable-value fallback of 0"
    )


@pytest.mark.parametrize("label,value", NON_FINITE_INPUTS)
def test_a_non_finite_value_never_becomes_a_earned_total(label, value):
    """float() is the dangerous half: it RETURNS inf/nan rather than raising."""
    coerced = progression_ledger._finite_float(value)
    assert coerced == 0.0, (
        f"{label} ({value!r}) reached the reply as {coerced!r}. The screen reads "
        f"catchup_qi_earned as 'you have earned N', so a non-finite value there "
        f"is displayed as a real number."
    )
    assert math.isfinite(coerced), f"{label} produced the non-finite {coerced!r}"


def test_a_value_too_large_for_the_column_costs_one_field_and_nothing_else():
    """10 ** 400 is a legal Python int; int() reads it fine, float() overflows.

    The column it would target is a 4-byte INTEGER, so the value is unreadable
    here. It must cost that ONE field, not the whole read.
    """
    assert progression_ledger._int(10 ** 400) == 0, (
        "an integer too large for the target column was not degraded to the "
        "unreadable-value fallback"
    )


def test_a_stored_non_finite_earned_total_is_normalised_on_read():
    """The database branch: a row holding NaN/Infinity reads as a finite number."""
    for label, bad in (("NaN", float("nan")), ("Infinity", float("inf"))):
        conn = _ConnStore([(3, 3, 2, 1, bad)])
        row = progression_ledger.read_progression(conn, PLAYER, AXIS)
        assert math.isfinite(row["catchup_qi_earned"]), (
            f"a row storing {label} read back as {row['catchup_qi_earned']!r}"
        )
        assert row["catchup_qi_earned"] == 0.0, (
            f"a row storing {label} should read as 0.0, got "
            f"{row['catchup_qi_earned']!r}"
        )


def test_a_stored_non_finite_step_does_not_take_the_whole_row_with_it():
    """One corrupt column costs one field; the readable siblings survive.

    A guard that raised, or that zeroed the entire row, would turn a single bad
    stored value into 'this player's progression is unknown'.
    """
    conn = _ConnStore([(float("nan"), 9, 4, 2, float("inf"))])
    row = progression_ledger.read_progression(conn, PLAYER, AXIS)
    assert row == {
        "current_step": 0,
        "highest_step": 9,
        "catchup_daily_claims": 4,
        "catchup_weekly_claims": 2,
        "catchup_qi_earned": 0.0,
    }, f"one corrupt row produced {row!r}; readable fields were lost with it"


def test_a_non_finite_earned_value_is_not_written_to_the_database():
    """THE WRITE GUARD. This is the one that stops the permanent poisoning.

    Without it, ``catchup_qi_earned = existing + EXCLUDED`` accumulates a NaN
    forever and no later correct write recovers the player's total.
    """
    for label, bad in (("NaN", float("nan")), ("Infinity", float("inf"))):
        conn = _ConnStore([(1, 1)])
        progression_ledger.record_axis_progress(
            conn, PLAYER, AXIS, 3, daily_claims=1, weekly_claims=1, qi_earned=bad
        )
        sent = conn.current_connection.calls[0][1]
        assert math.isfinite(float(sent[6])), (
            f"{label} was sent to the database as {sent[6]!r}. The upsert "
            f"ACCUMULATES this column and nan + 1.0 is still nan, so this write "
            f"poisons the player's lifetime total permanently."
        )
        assert float(sent[6]) == 0.0, (
            f"{label} should be written as 0.0, not {sent[6]!r}"
        )


def test_a_non_finite_step_is_not_written_to_the_database():
    """step_index / the step column is guarded on the way IN as well as out."""
    conn = _ConnStore([("milestone_x",)])
    progression_ledger.reserve_claim(
        conn, PLAYER, "milestone_nonfinite", "2026-W40", float("nan")
    )
    sent = conn.current_connection.calls[0][1]
    assert sent[4] == 0 and isinstance(sent[4], int), (
        f"step_index was written as {sent[4]!r} ({type(sent[4]).__name__})"
    )

    conn2 = _ConnStore([(1, 1)])
    progression_ledger.record_axis_progress(
        conn2, PLAYER, AXIS, float("inf"), daily_claims=1, weekly_claims=1
    )
    step_written = conn2.current_connection.calls[0][1]
    assert step_written[2] == 0 and isinstance(step_written[2], int), (
        f"the step was written as {step_written[2]!r} "
        f"({type(step_written[2]).__name__})"
    )


def test_a_non_finite_reply_can_still_be_json_encoded():
    """A guard that lets one through breaks serialisation of the WHOLE reply.

    ``json.dumps`` raises ValueError on inf/nan with allow_nan off and writes
    bare ``Infinity`` tokens with it on, which no strict client parser accepts.
    The guarded values must therefore be ordinary finite numbers.
    """
    conn = _ConnStore([(float("nan"), float("nan"), float("nan"), float("nan"),
                        float("nan"))])
    row = progression_ledger.read_progression(conn, PLAYER, AXIS)
    encoded = json.dumps(row, allow_nan=False)
    assert "Infinity" not in encoded and "NaN" not in encoded, (
        f"the encoded reply still carries a non-finite token: {encoded}"
    )
    assert set(json.loads(encoded)) == _PROGRESSION_KEYS


def test_one_poisoned_value_cannot_be_added_back_towards_a_finite_total():
    """The arithmetic argument, stated as an assertion rather than a comment.

    This is WHY the write is guarded: adding a finite number to a non-finite
    one leaves it non-finite, so a single bad write is unrecoverable. If a
    future refactor made the upsert assign rather than accumulate, this
    document stops being true -- and the guard would no longer be load-bearing,
    which is why it is worth pinning rather than leaving in a docstring.
    """
    assert not math.isfinite(float("nan") + 1.0)
    assert not math.isfinite(float("inf") + 1.0)
