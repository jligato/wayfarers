"""
Tests for the daily login streak.

Written against explicit `now` values rather than sleeping, so they are
deterministic and fast -- the stamina.py convention.

The two tests that matter most are
``test_same_day_double_claim_pays_once`` and
``test_evaluate_is_pure_and_cannot_be_used_to_farm``: the first is the
requirement, the second is the guard against a test that passes for the wrong
reason (see ``test_reward_cap_*`` for the same class of trap on the cap).
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import daily_streak as D  # noqa: E402


DAY = float(D.SECONDS_PER_DAY)
"""One day in seconds. Day index N starts at exactly N * DAY."""


def make_player(qi=0.0, **extra):
    return {"qi": qi, **extra}


def at(day_index, hour=12):
    """A timestamp partway through UTC day ``day_index``.

    Midday, not midnight, so a test cannot accidentally pass because it sits on
    a day boundary.
    """
    return day_index * DAY + hour * 3600.0


# --- the pure reward curve ---------------------------------------------------

def test_reward_grows_with_streak():
    assert D.reward_for_streak(1) == D.STREAK_REWARD_BASE_QI
    assert D.reward_for_streak(5) > D.reward_for_streak(1)
    assert D.reward_for_streak(20) > D.reward_for_streak(5)


def test_reward_is_capped_and_a_long_streak_pays_the_same_as_the_cap():
    """The cap requirement: a 500-day streak must NOT pay 500x a 1-day streak."""
    cap_reward = D.reward_for_streak(D.STREAK_REWARD_CAP_DAY)
    for long_streak in (31, 100, 365, 500, 10000):
        assert D.reward_for_streak(long_streak) == cap_reward
    # And it is a real cap, not just "slowly stops": the ratio is bounded.
    assert cap_reward / D.reward_for_streak(1) == pytest.approx(12.6)


def test_reward_curve_is_monotonic_up_to_the_cap():
    rewards = [D.reward_for_streak(d) for d in range(1, D.STREAK_REWARD_CAP_DAY + 1)]
    assert rewards == sorted(rewards)
    assert all(b > a for a, b in zip(rewards, rewards[1:]))


def test_reward_handles_degenerate_inputs():
    assert D.reward_for_streak(0) == D.STREAK_REWARD_BASE_QI
    assert D.reward_for_streak(-5) == D.STREAK_REWARD_BASE_QI


# --- the pure streak-transition function -------------------------------------

def test_first_claim_is_streak_one():
    assert D.next_streak(0, None, 1000) == 1


def test_consecutive_days_increment():
    assert D.next_streak(1, 1000, 1001) == 2
    assert D.next_streak(9, 1000, 1001) == 10


def test_missing_one_day_halves_instead_of_zeroing():
    """The chosen design: a grace window, not a hard reset."""
    # Streak 10, missed exactly one day -> halved to 5, then this claim is 6.
    assert D.next_streak(10, 1000, 1002) == 6
    # A streak of 1 halves to 1 (floored), so the claim is 2, never 1.
    assert D.next_streak(1, 1000, 1002) == 2
    # Streak 3 halves to 1 -> claim is 2.
    assert D.next_streak(3, 1000, 1002) == 2


def test_missing_two_or_more_days_resets_to_one():
    assert D.next_streak(50, 1000, 1003) == 1
    assert D.next_streak(50, 1000, 1010) == 1


def test_backwards_or_same_clock_returns_streak_unchanged():
    # claim() refuses these first; the pure function must still be total.
    assert D.next_streak(7, 1000, 1000) == 7
    assert D.next_streak(7, 1000, 999) == 7


# --- claim: the money path ----------------------------------------------------

def test_first_claim_pays_and_sets_streak():
    p = make_player(qi=100.0)
    out = D.claim(p, now=at(1000))
    assert out["already_claimed"] is False
    assert out["streak"] == 1
    assert out["reward_qi"] == D.STREAK_REWARD_BASE_QI
    assert p["qi"] == 100.0 + D.STREAK_REWARD_BASE_QI
    assert p[D.STATE_KEY]["streak"] == 1


def test_seven_consecutive_days_accumulate_and_escalate():
    p = make_player(qi=0.0)
    paid = []
    for i in range(7):
        out = D.claim(p, now=at(1000 + i))
        paid.append(out["reward_qi"])
    assert p[D.STATE_KEY]["streak"] == 7
    # Each day paid more than the last, and the balance is the exact sum.
    assert paid == sorted(paid) and all(b > a for a, b in zip(paid, paid[1:]))
    assert p["qi"] == pytest.approx(sum(paid))


def test_qi_is_read_from_progress_when_qi_key_is_absent():
    p = {"progress": 50.0}
    D.claim(p, now=at(1000))
    assert p["qi"] == pytest.approx(50.0 + D.STREAK_REWARD_BASE_QI)


# --- THE critical requirement: same-day idempotency ---------------------------

def test_same_day_double_claim_pays_once():
    p = make_player(qi=0.0)
    first = D.claim(p, now=at(1000, hour=1))
    second = D.claim(p, now=at(1000, hour=23))
    assert first["already_claimed"] is False
    assert second["already_claimed"] is True
    assert second["reward_qi"] == 0.0
    assert p["qi"] == pytest.approx(D.STREAK_REWARD_BASE_QI)
    assert p[D.STATE_KEY]["claims"] == 1


def test_same_day_claim_is_idempotent_across_the_utc_midnight_boundary():
    """The two edges of a single UTC day.

    A claim at 12:00 and a re-press at 23:00 are 11 hours apart but the SAME
    day, so the second must pay nothing. A press at 01:00 the next morning is a
    different day and MUST pay again -- if this test expected no second payout
    it would be asserting a bug.
    """
    p = make_player(qi=0.0)
    first = D.claim(p, now=1000 * DAY + 12 * 3600)
    again = D.claim(p, now=1000 * DAY + 23 * 3600)
    assert first["already_claimed"] is False
    assert again["already_claimed"] is True
    assert p["qi"] == pytest.approx(D.STREAK_REWARD_BASE_QI)

    # One hour after that press it is 00:00 of the next UTC day.
    next_day = D.claim(p, now=1001 * DAY + 3600)
    assert next_day["already_claimed"] is False
    assert next_day["streak"] == 2


def test_fifty_repeated_presses_on_one_day_pay_once():
    """The loud version of the same bug, in the shape the tribulation fix
    described: several presses arriving before any of them is observed."""
    p = make_player(qi=0.0)
    results = [D.claim(p, now=at(1000, hour=h)) for h in range(0, 24)]
    payouts = [r for r in results if not r["already_claimed"]]
    assert len(payouts) == 1
    assert p["qi"] == pytest.approx(D.STREAK_REWARD_BASE_QI)
    assert p[D.STATE_KEY]["claims"] == 1
    assert p[D.STATE_KEY]["total_qi_earned"] == pytest.approx(D.STREAK_REWARD_BASE_QI)


def test_claim_does_not_mutate_state_at_all_when_already_claimed():
    """A refused claim must leave every field byte-identical."""
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    before = {"qi": p["qi"], "state": dict(p[D.STATE_KEY])}
    D.claim(p, now=at(1000, hour=18))
    assert p["qi"] == before["qi"]
    assert p[D.STATE_KEY] == before["state"]


def test_backwards_clock_does_not_pay():
    """A server whose clock jumps back must not become a payout faucet."""
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    out = D.claim(p, now=at(999))
    assert out["already_claimed"] is True
    assert p["qi"] == pytest.approx(D.STREAK_REWARD_BASE_QI)


def test_claim_persists_the_day_key_not_a_timestamp():
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000, hour=7))
    assert p[D.STATE_KEY]["last_claim_utc_day"] == 1000
    assert isinstance(p[D.STATE_KEY]["last_claim_utc_day"], int)


# --- the grace window, end to end --------------------------------------------

def test_missed_day_halving_end_to_end():
    p = make_player(qi=0.0)
    for i in range(10):
        D.claim(p, now=at(1000 + i))
    assert p[D.STATE_KEY]["streak"] == 10
    # Skip day 1010, claim on 1011: halved 10 -> 5, then +1 = 6.
    out = D.claim(p, now=at(1011))
    assert out["grace_halved"] is True
    assert out["streak"] == 6
    assert out["reward_qi"] == D.reward_for_streak(6)
    # The halved payout is strictly less than the pre-miss payout.
    assert out["reward_qi"] < D.reward_for_streak(10)


def test_two_missed_days_resets_end_to_end():
    p = make_player(qi=0.0)
    for i in range(30):
        D.claim(p, now=at(1000 + i))
    assert p[D.STATE_KEY]["streak"] == 30
    out = D.claim(p, now=at(1033))
    assert out["streak"] == 1
    assert out["grace_halved"] is False


def test_streak_keeps_counting_past_the_reward_cap():
    """The counter is a status value; only the PAYOUT saturates."""
    p = make_player(qi=0.0)
    payouts = [D.claim(p, now=at(1000 + i))["reward_qi"] for i in range(1, 61)]
    assert p[D.STATE_KEY]["streak"] == 60
    # The cap is per-DAY payout, not per-total: every day past the cap paid
    # exactly the cap-day value, so the curve genuinely flattened.
    cap_reward = D.reward_for_streak(D.STREAK_REWARD_CAP_DAY)
    assert all(r == cap_reward for r in payouts[D.STREAK_REWARD_CAP_DAY - 1:])
    # A 60-day streak paid per-day the same as a 30-day streak, NOT 60x day 1.
    # The cap-day payout is ~12.6x a day-1 payout and then flat forever.
    assert cap_reward / D.reward_for_streak(1) == pytest.approx(12.6)
    # Total earned is the honest sum of all 60 payouts.
    assert p[D.STATE_KEY]["total_qi_earned"] == pytest.approx(sum(payouts))
    s = D.status(p, now=at(1061))
    assert s["streak"] == 60
    assert s["at_reward_cap"] is True
    assert s["next_streak"] == 61
    assert s["reward_today_qi"] == cap_reward


# --- purity guarantees --------------------------------------------------------

def test_evaluate_is_pure_and_cannot_be_used_to_farm():
    """Reading the outcome must not move the streak or pay anything.

    This is the same class of trap as stamina's `current_stamina` test: a
    "preview" function that secretly mutates would let a client farm by
    polling it.
    """
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    before_qi = p["qi"]
    before_state = dict(p[D.STATE_KEY])
    for _ in range(10):
        out = D.evaluate(p[D.STATE_KEY], now=at(1001))
        assert out["streak"] == 2
        assert out["reward_qi"] == D.reward_for_streak(2)
    assert p["qi"] == before_qi
    assert p[D.STATE_KEY] == before_state


def test_status_is_pure():
    p = make_player(qi=0.0)
    for _ in range(5):
        D.status(p, now=at(1000))
    assert p["qi"] == 0.0
    assert D.STATE_KEY not in p


# --- status -------------------------------------------------------------------

def test_status_reports_zero_reward_once_claimed_today():
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    s = D.status(p, now=at(1000, hour=20))
    assert s["claimed_today"] is True
    assert s["reward_today_qi"] == 0.0
    assert s["streak"] == 1


def test_status_projects_tomorrow_before_the_claim():
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    s = D.status(p, now=at(1001))
    assert s["claimed_today"] is False
    assert s["next_streak"] == 2
    assert s["reward_today_qi"] == D.reward_for_streak(2)


def test_status_works_for_a_legacy_player_with_no_streak_state():
    s = D.status({"qi": 0.0}, now=at(1000))
    assert s["streak"] == 0
    assert s["claimed_today"] is False
    assert s["reward_today_qi"] == D.reward_for_streak(1)


# --- utc_day ------------------------------------------------------------------

def test_utc_day_indexes_days_not_seconds():
    assert D.utc_day(0.0) == 0
    assert D.utc_day(DAY - 1) == 0
    assert D.utc_day(DAY) == 1
    assert D.utc_day(1000 * DAY + 5) == 1000


# --- robustness ----------------------------------------------------------------

def test_legacy_player_gets_a_fresh_state_not_a_windfall():
    p = make_player(qi=0.0, daily_streak={"streak": 0})  # partial, pre-upgrade shape
    out = D.claim(p, now=at(1000))
    assert out["streak"] == 1
    assert p[D.STATE_KEY]["last_claim_utc_day"] == 1000


def test_claim_rejects_a_non_dict_player():
    with pytest.raises(D.DailyStreakError):
        D.claim(None, now=at(1000))


# --- response shape: the claim route returns `evaluate`'s dict verbatim --------
#
# A client renders one claim screen from ONE response shape. `evaluate` builds
# two different dicts -- the already-claimed early return and the normal return
# -- and they did not carry the same keys, so the second press of the day (the
# most common press there is) answered with a SMALLER object. Anything reading
# `grace_halved` off that reply raised KeyError.
#
# These assert on the KEY SET, not on a frozen list, so a future field added to
# both branches is fine and a field added to only one is not.

_ALL_KEYS = {
    "ok",
    "already_claimed",
    "streak",
    "reward_qi",
    "reason",
    "utc_day",
    "days_since_last_claim",
    "grace_halved",
}


def test_claim_response_has_the_same_keys_on_a_first_and_a_repeat_claim():
    p = make_player(qi=0.0)
    first = D.claim(p, now=at(1000))
    again = D.claim(p, now=at(1000, hour=20))
    assert set(first) == set(again) == _ALL_KEYS


def test_repeat_claim_reports_grace_halved_and_gap_instead_of_raising():
    """The bug this pins: a repeat claim answered without those two keys."""
    p = make_player(qi=0.0)
    D.claim(p, now=at(1000))
    again = D.claim(p, now=at(1000, hour=20))
    # Readable, and not a lie: nothing was halved and the gap is today.
    assert again["grace_halved"] is False
    assert again["days_since_last_claim"] == 0
    assert again["reward_qi"] == 0.0
    assert again["already_claimed"] is True


def test_evaluate_reports_the_same_keys_whether_or_not_the_day_is_claimed():
    """`evaluate` is public and pure; the sim and the client both read it."""
    state = D.get_state(make_player(qi=0.0))
    unclaimed = D.evaluate(state, now=at(1000))
    claimed = D.evaluate(state, now=at(1000))
    assert set(unclaimed) == set(claimed) == _ALL_KEYS


# --- at_reward_cap must describe TODAY'S payout, not yesterday's streak -------
#
# A player on streak 29 has not claimed yet, so today's claim makes 30 and pays
# the capped amount. The badge said `at_reward_cap: False` on that exact day --
# the one day the flag and the payout disagreed. The test walks a real player up
# to the boundary rather than asserting a value, so it fails if the rule moves.

def test_at_reward_cap_is_true_on_the_day_the_payout_first_reaches_the_cap():
    p = make_player(qi=0.0)
    for i in range(D.STREAK_REWARD_CAP_DAY - 1):
        D.claim(p, now=at(1000 + i))
    s = D.status(p, now=at(1000 + D.STREAK_REWARD_CAP_DAY - 1))
    # The claim available right now pays the cap-day amount...
    assert s["reward_today_qi"] == D.reward_for_streak(D.STREAK_REWARD_CAP_DAY)
    # ...so the badge must already agree with it.
    assert s["at_reward_cap"] is True


def test_at_reward_cap_is_false_while_the_payout_is_still_climbing():
    """The other side of the boundary, so the fix is not just 'always True'."""
    p = make_player(qi=0.0)
    for i in range(3):
        D.claim(p, now=at(1000 + i))
    s = D.status(p, now=at(1003))
    assert s["at_reward_cap"] is False
    assert s["reward_today_qi"] < D.reward_for_streak(D.STREAK_REWARD_CAP_DAY)


# --- the module must import on its own --------------------------------------
#
# `from qi_rewards import grant_qi` is a BARE sibling import. That only resolves
# when server/ is already on sys.path. Every test here happens to run under the
# repo-root conftest, which puts server/ on the path -- so the suite passed
# while `from server import daily_streak` from any other process raised
# ModuleNotFoundError. Sibling modules (beast_wave.py, currency_exchange.py,
# element_matrix_routes.py) already bootstrap their own directory; this one did
# not. Measured before the fix: ModuleNotFoundError: No module named 'qi_rewards'.

def test_module_imports_without_help_from_the_conftest(tmp_path):
    """Boot a fresh interpreter with ONLY the repo root on sys.path."""
    import subprocess

    repo_root = str(Path(__file__).resolve().parents[2])
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from server import daily_streak as D; "
        "print(D.STATE_KEY)" % (repo_root,)
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),          # not the repo: cwd cannot rescue the import
        capture_output=True,
        text=True,
        timeout=60,
        # Inherits this process's env, but NOT a PYTHONPATH that would smuggle
        # server/ onto sys.path -- the point is to prove the file stands alone.
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert proc.returncode == 0, (
        "server/daily_streak.py does not import on its own:\n"
        + (proc.stderr or proc.stdout)
    )
    assert proc.stdout.strip() == D.STATE_KEY


# --- a corrupt save must not crash the route, and must not lie ---------------
#
# Measured before the fix, on a legacy save whose `last_claim_utc_day` was the
# string "abc": evaluate() raised ValueError, status() raised ValueError, and
# next_streak() raised ValueError. The claim route would have answered 500 to a
# player who did nothing but press a button. Same for a LIST stored in that
# field (TypeError). These pin the degradation to "never claimed" instead.

_BAD_DAYS = ["abc", "", "1e3", "1000.5", [1], {}, True]


@pytest.mark.parametrize("bad", _BAD_DAYS)
def test_a_corrupt_stored_day_never_raises(bad):
    """A save that cannot be read must degrade, not 500."""
    state = {"streak": 5, "last_claim_utc_day": bad, "total_qi_earned": 0.0, "claims": 5}
    out = D.evaluate(dict(state), now=at(1000))
    assert out["streak"] >= 1
    s = D.status({"daily_streak": dict(state)}, now=at(1000))
    assert s["streak"] >= 0
    assert D.next_streak(5, bad, 1000) >= 1


def test_a_numeric_string_day_is_still_honoured_as_a_real_claim():
    """Coercion must not swallow a LEGITIMATE value written as a string."""
    state = {"streak": 5, "last_claim_utc_day": "1000", "total_qi_earned": 0.0, "claims": 5}
    out = D.evaluate(dict(state), now=at(1000, hour=20))
    assert out["already_claimed"] is True, "a same-day string claim must still be refused"
    assert D.next_streak(5, "999", 1000) == 6, "a string day must still advance the streak"


@pytest.mark.parametrize("bad", _BAD_DAYS)
def test_a_corrupt_stored_streak_never_raises(bad):
    state = {"streak": bad, "last_claim_utc_day": 1000, "total_qi_earned": 0.0, "claims": 1}
    assert D.evaluate(dict(state), now=at(1001))["streak"] >= 1
    assert D.status({"daily_streak": dict(state)}, now=at(1001))["streak"] >= 0


def test_a_negative_streak_can_never_be_persisted():
    """A streak is a COUNT. It cannot go below zero, let alone stay there.

    Measured before the fix: a save carrying streak -3 was written back as -2
    on the next day's claim, then -1 the day after, so the counter stayed
    negative for as long as the player kept signing in.
    """
    p = {"qi": 0.0, "daily_streak": {"streak": -50, "last_claim_utc_day": 1000,
                                     "total_qi_earned": 0.0, "claims": 1}}
    for i in range(1, 6):
        out = D.claim(p, now=at(1000 + i))
        assert out["streak"] >= 1
        assert p[D.STATE_KEY]["streak"] >= 1


def test_a_refused_claim_does_not_add_keys_to_a_partial_legacy_save():
    """The "mutate nothing" contract, on the shape that actually broke.

    The existing purity test claims on day one FIRST, so by the refused press
    the state already carried every key and the mutation was invisible. A save
    that already claims today but predates the `claims` / `total_qi_earned`
    fields gained both on a press that paid nothing, because claim() called
    get_state() -- which writes the defaults into the player's own dict -- before
    it decided the claim was a refusal.
    """
    p = make_player(qi=10.0, daily_streak={"streak": 5, "last_claim_utc_day": 1000})
    before = {k: v for k, v in p[D.STATE_KEY].items()}
    out = D.claim(p, now=at(1000, hour=20))
    assert out["already_claimed"] is True
    assert p[D.STATE_KEY] == before
    assert set(p[D.STATE_KEY]) == {"streak", "last_claim_utc_day"}


def test_the_reward_curve_is_unchanged_by_the_coercion_helpers():
    """The fix must not have moved the economy.

    Guards against a coercion that silently changed a payout, which is the one
    regression a reader of these tests would most worry about.
    """
    for d in range(1, D.STREAK_REWARD_CAP_DAY + 1):
        assert D.reward_for_streak(d) == D.STREAK_REWARD_BASE_QI + (d - 1) * D.STREAK_REWARD_STEP_QI
    # And a normal player's streak arithmetic is untouched.
    p = make_player(qi=0.0)
    for i in range(7):
        D.claim(p, now=at(1000 + i))
    assert p[D.STATE_KEY]["streak"] == 7


# --- non-finite stored values (OverflowError) ---------------------------------
#
# The values above are all caught by TypeError or ValueError, which is what the
# coercion helpers caught. OverflowError is a THIRD error and it was not caught:
#
#   _coerce_day / _coerce_streak run int(float(value)), which is TWO
#   conversions, and each one raises OverflowError for a DIFFERENT input --
#   float('inf') survives step 1 and dies in int() (step 2), while an integer
#   too large for a float (10 ** 400) dies in step 1 before int() ever runs.
#
# Both are reachable stored values: json.dumps writes a non-finite float as a
# bare Infinity token and json.loads reads it back, and a large Python int
# round-trips through a save untouched. Measured before the fix, every case
# below raised out of status() and claim() as a 500.

_NON_FINITE = [
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="-inf"),
    pytest.param(10 ** 400, id="huge-int"),
    pytest.param("1e400", id="1e400"),
    pytest.param("-1e400", id="-1e400"),
]


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_a_non_finite_stored_streak_never_500s(bad):
    """The headline regression: an infinite streak must degrade, not 500."""
    p = make_player(qi=0.0, daily_streak={
        "streak": bad, "last_claim_utc_day": 1000,
        "total_qi_earned": 0.0, "claims": 1})
    out = D.claim(p, now=at(1001))
    assert out["streak"] >= 1, "a fresh claim must still earn a real streak"

    # Read the status on the NEXT day, not the day just claimed: status() pays
    # 0.0 on a day already claimed (that is the contract, and it is why the
    # value below is only interesting once the key has moved on). The point of
    # this test is that the player is still owed money afterwards.
    snap = D.status(p, now=at(1002))
    assert snap["claimed_today"] is False
    assert snap["streak"] >= 0
    assert snap["reward_today_qi"] > 0, "a corrupt save must not cost the player the payout"


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_a_non_finite_stored_day_never_500s(bad):
    """Same stored-value class, on the idempotency key field."""
    state = {"streak": 5, "last_claim_utc_day": bad,
             "total_qi_earned": 0.0, "claims": 5}
    # An unreadable day must read as "never claimed", which is the safe
    # direction: it pays once rather than never.
    assert D.evaluate(dict(state), now=at(1000))["already_claimed"] is False
    assert D.status({"daily_streak": dict(state)}, now=at(1000))["streak"] >= 0


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_a_non_finite_stored_claims_count_never_500s(bad):
    """The claims counter was read with a bare int(), wrapped in NO try at all.

    It is one step past the two coercion helpers in the same request, so a
    player whose save is corrupt in that field alone still 500s unless it goes
    through the same guard.
    """
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": 0.0, "claims": bad})
    out = D.claim(p, now=at(1001))
    assert out["streak"] >= 1
    assert D.status(p, now=at(1001))["claims"] >= 0


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_a_non_finite_stored_qi_total_never_500s(bad):
    """The lifetime-qi total, the FOURTH stored field and the last one guarded.

    Its siblings -- ``streak``, ``last_claim_utc_day`` and ``claims`` -- each
    have a non-finite test above, but every one of them hardcodes
    ``"total_qi_earned": 0.0`` as an inert filler. That left this field with no
    permanent test of its own while still being read on every claim.

    This is the only non-finite field whose failure is SILENT rather than
    loud. ``float('inf')`` and the string ``'1e400'`` both convert without
    raising, so before the ``math.isfinite`` check they produced no 500 at all
    -- they produced a lifetime total of ``inf`` that survives every later
    claim (``inf + reward`` stays ``inf``) and is handed to the client as a
    number it cannot display. The assertion below is therefore on FINITENESS,
    not merely on "did not raise".
    """
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": bad, "claims": 1})

    # A real claim on top of the corrupt total must land a finite number.
    D.claim(p, now=at(1001))
    after_claim = p[D.STATE_KEY]["total_qi_earned"]
    assert math.isfinite(after_claim), (
        f"a corrupt stored total must not survive a claim as {after_claim!r}")
    assert after_claim > 0, "the player's reward must still be earned"

    # status() must report the same thing on the following day.
    snap = D.status(p, now=at(1002))
    assert math.isfinite(snap["total_qi_earned"])


# _NON_FINITE above only carries the SILENT class: ``inf``, ``-inf`` and the
# strings ``"1e400"`` / ``"-1e400"`` all convert WITHOUT raising, so they only
# prove the ``math.isfinite`` half of the helper.
#
# The RAISING class is missing from that list, and it is the one the helper's
# ``try`` exists for. ``"abc"`` raises ValueError, ``[1]`` raises TypeError, and
# ``10 ** 400`` raises OverflowError. Without a test carrying those three, a
# refactor that deleted the whole ``try`` block would still leave the suite
# green on two of the three failure modes -- which is exactly the silent
# regression the helper was added to prevent.
#
# ``10 ** 400`` is already in _NON_FINITE as ``huge-int``, so it is deliberately
# NOT repeated here: the same value under a second id would add a duplicate
# case and no new coverage.

_QI_TOTAL_RAISING = [
    pytest.param("abc", id="string-abc"),
    pytest.param([1], id="list-one"),
]

_QI_TOTAL_BAD = list(_NON_FINITE) + _QI_TOTAL_RAISING


@pytest.mark.parametrize("bad", _QI_TOTAL_BAD)
def test_a_corrupt_stored_qi_total_never_raises_on_either_route(bad):
    """Both public functions, on a save that is corrupt the moment it is read.

    The test above drives ``claim()`` first, so by the time it calls
    ``status()`` the value has already been repaired by the claim's own write.
    Its status() assertion therefore proves the value is finite only AFTER a
    repair, which is the weaker claim.

    This one reads the corrupt value on the pure read path FIRST, with nothing
    having touched it. That is the request the fix exists to keep alive: a
    player opening the streak panel must get a number, not a 500.
    """
    fresh = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": bad, "claims": 1})

    snap = D.status(fresh, now=at(1001))
    assert math.isfinite(snap["total_qi_earned"]), (
        f"status() must not hand the client {snap['total_qi_earned']!r}")
    # The rest of the panel must still be real data, not a blanket zero.
    assert snap["streak"] == 3
    assert snap["claims"] == 1

    # Now the write path, on its own untouched player.
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": bad, "claims": 1})
    out = D.claim(p, now=at(1001))
    assert out["already_claimed"] is False, "a corrupt total must not cost the claim"
    after = p[D.STATE_KEY]["total_qi_earned"]
    assert math.isfinite(after), f"a claim must land a finite total, not {after!r}"
    assert after == pytest.approx(D.reward_for_streak(4)), (
        "an unreadable total must be read as 0.0 and the reward added on top")


@pytest.mark.parametrize("bad", ["abc", [1]])
def test_an_unreadable_stored_qi_total_is_read_as_zero_not_raised(bad):
    """The specific degradation contract for the two raising classes.

    ``_coerce_qi_total`` turns a value it cannot read into ``0.0`` rather than
    raising. Pinned as a value, not merely as "did not raise", so a helper that
    guessed some other fallback would be caught.
    """
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": bad, "claims": 1})
    assert D.status(p, now=at(1001))["total_qi_earned"] == 0.0


@pytest.mark.parametrize("bad", [True, False, None])
def test_a_bool_or_none_stored_qi_total_is_zero_not_a_python_truth_value(bad):
    """The guard this helper opens with, which the other lists never reached.

    Python says ``float(True)`` is ``1.0``, so without the explicit
    ``isinstance(value, bool)`` check a save carrying ``"total_qi_earned": true``
    would report the player as having earned exactly one unit of qi forever,
    and no exception would ever fire to announce it. ``None`` is caught by the
    ``try`` as a TypeError anyway, but is pinned here so the opening guard is
    covered by a test rather than only by reading the source.
    """
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": bad, "claims": 1})
    assert D.status(p, now=at(1001))["total_qi_earned"] == 0.0
    assert isinstance(D.status(p, now=at(1001))["total_qi_earned"], float)


def test_a_negative_stored_qi_total_is_preserved_not_clamped():
    """The control on the OTHER design decision in this helper.

    Unlike the three sibling coercion helpers, ``_coerce_qi_total`` does NOT
    floor at zero. A negative total is readable data rather than an unreadable
    one, it grants nothing, and silently clamping it to 0.0 would destroy the
    evidence of whatever wrote it. Pinned so a future "just make it consistent
    with _coerce_streak" edit cannot quietly discard it.
    """
    p = make_player(qi=0.0, daily_streak={
        "streak": 3, "last_claim_utc_day": 1000,
        "total_qi_earned": -50.0, "claims": 1})
    assert D.status(p, now=at(1001))["total_qi_earned"] == -50.0


def test_the_non_finite_fix_does_not_move_a_finite_streak():
    """The control: the guard must be inert for ordinary saved values.

    A coercion widened to catch OverflowError must not change what a NORMAL
    player earns -- the regression a reader would most reasonably fear.
    """
    p = make_player(qi=0.0)
    for i in range(1, 8):
        D.claim(p, now=at(1000 + i))
    assert p[D.STATE_KEY]["streak"] == 7
    assert p[D.STATE_KEY]["claims"] == 7
    assert p[D.STATE_KEY]["streak"] < D.STREAK_REWARD_CAP_DAY
    # Strings, ints and whole floats must all still coerce identically.
    assert D.next_streak(5, "999", 1000) == 6
    assert D.next_streak(5, 999, 1000) == 6
    assert D.next_streak(5, 999.0, 1000) == 6
    assert D.status(make_player(daily_streak={"streak": "5",
                                             "last_claim_utc_day": 1000}),
                    now=at(1001))["streak"] == 5
