"""Status-code contract for every missing-key / malformed-value site in
``server/lifecycle.py``.

THE CONTRACT
============
* 4xx -- the CALLER sent something wrong. Fix the request.
* 5xx -- WE are wrong. A stored row we cannot read, price or reconcile is a
  server defect and must reach every 5xx dashboard.

A 400 for a defect in our own stored data tells the caller to fix an input
that was already fine, and hides the defect from every error tracker keyed on
5xx. A silent default is worse than both: it answers 200 with a plausible
wrong number and records nothing.

WHAT THIS FILE COVERS
====================
Each test seeds a lifecycle row with exactly ONE key removed or corrupted,
calls the route as the OWNER, and asserts the status code. The rest of the row
is complete, because a site that reads a default can only be caught when
everything around it is healthy.

Site list, before -> after:

  route_class.handle    status          silent None -> 500 logged
  view                  dies_at         unhandled KeyError -> 500 logged
  view                  dies_at (junk)  unhandled ValueError -> 500 logged
  _deadline_view        dies_at         silent "deadline now" -> 500 logged
  _deadline_view        missed_days     silent 0 -> 500 logged
  _deadline_view        progress        silent 100.0 -> 500 logged
  _deadline_view        attended_days   silent [] -> 500 logged
  reconcile.cursor      last_reconciled_at (junk)
                                      silent raw clock, floor dropped -> 500
  reconcile.death       born_at         silent full-life -> 500 logged
  reconcile.loop        dies_at         SILENT FABRICATED DEATH -> 500
  reconcile.rebirth     version         unhandled KeyError -> 200 + LOGGED
  reconcile.rebirth     life_index      unhandled KeyError -> 200 + LOGGED
  reconcile.decay       attended_days   silent [] -> 500 logged
  reconcile.decay       progress        silent 100.0 decay base -> 500 logged
  reconcile.decay       missed_days     silent 0 -> 500 logged
  check_in              dies_at         unhandled KeyError after commit -> 500
  guard.mutate          dies_at         unhandled KeyError -> 500 logged
  rebirth               version         SILENT default -> 200 + LOGGED
  rebirth               life_index      SILENT default -> 200 + LOGGED

  guard.mutate (all 3)  any of the above 500 LAUNDERED into 503 -> 500 kept

THE THREE SILENT DEFAULTS, measured rather than asserted
========================================================
NOTE ON THE LAST TWO ROWS. ``version`` and ``life_index`` are the exception
to "silent default = wrong". ``server/tests/test_lifecycle_contract.py``
::test_rebirth_works_on_a_row_written_without_version_or_life_index PINS a
legacy row (written before those keys existed) to answer 200 with a
fabricated version. That is deliberate backwards compatibility: a 500 would
strand those accounts permanently. The defect at those two sites was only
that the default was SILENT, so the fix is the log, and that is what the
tests assert.


1. ``reconcile``'s loop read ``lc.get("dies_at", 0) <= now``. A row missing
   ``dies_at`` read as 0 -- 1 January 1970, long past -- so reconcile did not
   refuse: it FABRICATED an entire life, recording a death, a memorial and a
   reservation against a player whose row is merely broken, incrementing
   ``legacy.completed_lives`` and writing it all back.
2. ``rebirth`` read ``int(lc.get("version", 0)) + 1``. Swapping a raising
   subscript for a default is not a fix: a row without a version now reads as
   0 and the first rebirth fabricates a version-1 life.
3. ``_deadline_view`` read four keys with defaults, so a broken row told the
   panel "your life ends today", "nothing at risk", "baseline 100.0" and "you
   never checked in" -- four plausible lies, none logged.

Each is asserted on the STORED ROW as well as the status code, because a 500
alone would not prove the fabrication stopped.

A FOURTH SITE, FOUND BY THE TESCHANGES
=======================================
The three guard handlers wrapped their work in ``except Exception: raise
HTTPException(503)``. That swallowed every deliberate 500 raised from the
code UNDER them and re-answered it as 503 "service unavailable" -- so all
of the corrections above were invisible on the request path until this was
fixed. Still a 5xx, but it told the caller "retry later, I am busy" for a
row that will be equally broken on the retry, and named neither the key nor
the defect. Each block now re-raises an HTTPException untouched; a REAL
transaction failure still reaches the handler and is still answered 503.

WHAT WAS PROVED CAPABLE OF FAILING
==================================
``test_the_control_reproduces_the_fabricated_life`` re-applies the old
``lc.get("dies_at", 0)`` arithmetic to a real row through the real
``reconcile`` and asserts the fabricated death and incremented counter come
back. Without that control, a green file here would only prove the assertions
can be written. The two arms at the end (a healthy row faults on nothing, and
every fault logs) close the other direction.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402  bare, NOT server.auth -- main.py binds the same object
import lifecycle  # noqa: E402
import ownership_guard  # noqa: E402
from player_store import PlayerStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A bare app carrying only the lifecycle router, plus its own store.

    Function-scoped. ``lifecycle.route_class_store`` and
    ``lifecycle._live_players_db`` are MODULE globals, so a shared app would
    let one test's injected store answer another test's request.
    """
    players_db = PlayerStore()
    app = FastAPI()

    saved_live = lifecycle._live_players_db
    saved_rc = lifecycle.route_class_store
    lifecycle._live_players_db = players_db
    lifecycle.route_class_store = None
    try:
        lifecycle.register_routes(app, lambda: players_db)
        guard_report = ownership_guard.install_ownership_guard(app)
        yield {
            "app": app,
            "client": TestClient(app, raise_server_exceptions=False),
            "players_db": players_db,
            "guarded": set(guard_report),
        }
    finally:
        lifecycle._live_players_db = saved_live
        lifecycle.route_class_store = saved_rc


def owner_of(world: Dict[str, Any], pid: str) -> Dict[str, str]:
    """A real session token for ``pid``, as the shipped client gets one.

    The ownership guard is installed on every lifecycle route, so an
    unauthenticated request is answered 401 before the code under audit runs.
    Every test here carries the OWNER's own token: the question is what the
    route does with a broken ROW, not who is asking.
    """
    return {"Authorization": "Bearer " + auth.issue_session(pid)}


def call(world: Dict[str, Any], verb: str, suffix: str, pid: str, **kwargs: Any):
    """One authenticated request to ``{verb} /lifecycle/{pid}{suffix}``."""
    fn = getattr(world["client"], verb.lower())
    return fn(f"/lifecycle/{pid}{suffix}", headers=owner_of(world, pid), **kwargs)


def seed_lifecycle(world: Dict[str, Any], pid: str, **over: Any) -> Dict[str, Any]:
    """One ACTIVE player whose lifecycle carries every schema key.

    Built from ``lifecycle.new_state`` and stamped with a cursor, so the only
    thing wrong with the row is what a test deliberately removes.
    """
    now = time.time()
    row: Dict[str, Any] = {
        "id": pid,
        "name": pid,
        "progress": 40.0,
        "qi": 30.0,
        "spirit_stones": {"low_grade": 500},
        "lifecycle": lifecycle.new_state(now),
    }
    row["lifecycle"]["last_reconciled_at"] = now
    row["last_cultivation_time"] = now
    row.update(over)
    world["players_db"][pid] = row
    return row


def mutate_row(world: Dict[str, Any], pid: str, change) -> None:
    """Apply ``change(lifecycle_dict)`` to the STORED row, in place.

    The store is COPY-ON-READ outside a transaction: ``__getitem__`` returns
    a deepcopy of the committed row (``player_store.py``, ``__getitem__``),
    and ``__setitem__`` stores a deepcopy of what it is handed. So mutating
    the dict that ``db[pid]`` returned edits a throwaway copy and the STORED
    row stays healthy -- which would make every test below pass for the wrong
    reason. The read-modify-write has to be written back through the store.
    """
    db = world["players_db"]
    row = dict(db[pid])
    lc = dict(row["lifecycle"])
    change(lc)
    row["lifecycle"] = lc
    db[pid] = row


def break_key(world: Dict[str, Any], pid: str, key: str, value: Any = ...) -> None:
    """Remove (or corrupt) one key on the STORED lifecycle. See mutate_row."""
    mutate_row(world, pid,
               lambda lc: (lc.pop(key, None) if value is ... else lc.update({key: value})))


def stale_life(world: Dict[str, Any], pid: str, days: int) -> None:
    """Age the stored life so the reconcile decay window actually has days."""
    cutoff = time.time() - days * lifecycle.DAY

    def _age(lc: Dict[str, Any]) -> None:
        lc["born_at"] = cutoff
        lc["last_reconciled_at"] = cutoff

    mutate_row(world, pid, _age)


def assert_internal_defect(response, where: str) -> None:
    """The fault is ours, so it is a 5xx that says so -- never a 4xx.

    Asserts the body too: a 500 carrying a caller-shaped message is still a
    mislabelled fault, and the detail is what a client actually reads.
    """
    assert response.status_code == 500, (
        f"{where}: expected 500 for a defect in our own stored row, got "
        f"{response.status_code} -- {response.text[:400]}"
    )
    assert response.json().get("detail") == "internal_error", response.text[:400]


# ---------------------------------------------------------------------------
# Caller faults stay 4xx. The corrections must not over-correct.
# ---------------------------------------------------------------------------

def test_the_ownership_guard_is_installed_on_the_route(world) -> None:
    """Precondition: without this, every 500 below could be a guard's 500."""
    assert "/lifecycle/{pid}" in world["guarded"]
    assert "/lifecycle/{pid}/advance" in world["guarded"]


def test_an_unknown_player_is_still_a_caller_fault(world) -> None:
    """An id in neither the path contract nor the store is a 4xx, not a 500."""
    r = call(world, "GET", "", "nobody-here")
    assert r.status_code in (400, 404), r.status_code


def test_a_mutating_route_before_the_clock_starts_is_still_400(world) -> None:
    """No lifecycle at all is a legitimate CALLER ordering fault, not ours."""
    world["players_db"]["p1"] = {"id": "p1", "name": "p1", "progress": 10.0}
    r = call(world, "POST", "/advance", "p1")
    assert r.status_code == 400, r.status_code
    assert r.json().get("detail") == "lifecycle not started", r.text[:300]


def test_a_healthy_row_is_unaffected(world) -> None:
    """The control: a complete row must still answer 200 with real numbers."""
    seed_lifecycle(world, "p1")
    r = call(world, "GET", "", "p1")
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["status"] == "active", body
    assert body["remaining_seconds"] > lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY * 0.5
    assert body["deadline"]["days_until_deadline"] > 0, body["deadline"]


# ---------------------------------------------------------------------------
# route_class.handle -- the read that decides a 410 refusal
# ---------------------------------------------------------------------------

def test_a_lifecycle_row_with_no_status_is_a_500_not_a_silent_pass(world) -> None:
    """Before: ``view_data.get("status")`` returned None, matched no branch.

    A player whose stored lifecycle we cannot interpret was allowed to keep
    POSTing mutating requests, because the 410/409 refusal is driven by that
    single read. A row we cannot interpret is not a live player.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "status")
    assert_internal_defect(call(world, "POST", "/advance", "p1"),
                            "route_class.handle")


# ---------------------------------------------------------------------------
# view() -- the remaining-life read
# ---------------------------------------------------------------------------

def test_a_view_row_with_no_death_date_is_a_500(world) -> None:
    """Before: a bare ``lc["dies_at"]`` subscript -> unhandled KeyError."""
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "dies_at")
    assert_internal_defect(call(world, "GET", "", "p1"), "view")


def test_a_view_row_with_a_junk_death_date_is_a_500(world) -> None:
    """Before: ``float()`` on the junk raised far from the row that caused it."""
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "dies_at", "soon")
    assert_internal_defect(call(world, "GET", "", "p1"), "view")


# ---------------------------------------------------------------------------
# _deadline_view -- four silent defaults, four lies told to the panel
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["dies_at", "missed_days", "progress",
                                 "attended_days"])
def test_a_deadline_view_silent_default_is_now_a_500(world, key: str) -> None:
    """Each read as a plausible number rather than as a fault.

    ``dies_at`` -> "your life ends today"; ``missed_days`` -> "nothing at
    risk"; ``progress`` -> a 100.0 baseline; ``attended_days`` -> "you never
    checked in". All four were wrong answers about a row we cannot read, and
    none of them logged.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", key)
    assert_internal_defect(call(world, "GET", "", "p1"), "_deadline_view")


def test_a_junk_progress_baseline_is_a_500(world) -> None:
    """Present but unusable is still a defect: ``float()`` used to raise."""
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "progress", "lots")
    assert_internal_defect(call(world, "GET", "", "p1"), "_deadline_view")


# ---------------------------------------------------------------------------
# reconcile -- the sites that move real state
# ---------------------------------------------------------------------------

def test_a_missing_death_date_does_not_fabricate_a_life(world) -> None:
    """The worst silent default in the file, asserted on the stored row too.

    Before: ``lc.get("dies_at", 0) <= now`` read 0 as 1970, so reconcile
    recorded a death, a memorial and a reservation, incremented
    ``legacy.completed_lives``, and wrote all of it back. A status-code
    assertion alone would not prove the fabrication stopped.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "dies_at")

    assert_internal_defect(call(world, "GET", "", "p1"), "reconcile.loop")

    stored = world["players_db"]["p1"]["lifecycle"]
    assert stored.get("status") == "active", (
        f"a broken row was rewritten as {stored.get('status')!r} -- the "
        f"fabricated transition was persisted, not merely computed"
    )
    assert stored["legacy"]["completed_lives"] == 0, (
        "completed_lives was incremented against a row with no death date"
    )


def test_a_junk_reconciliation_cursor_is_a_500(world) -> None:
    """Before: ``except (TypeError, ValueError): return float(now)``.

    That silently returned the raw clock, DISCARDING the floor the function
    exists to enforce -- which is exactly how a server-clock rollback hands a
    player extra life. A data defect, so a 500.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "last_reconciled_at", "yesterday")
    assert_internal_defect(call(world, "GET", "", "p1"), "reconcile.cursor")


def test_a_missing_birth_date_does_not_record_a_decayless_death(world) -> None:
    """Before: ``born_at`` defaulted to ``dies_at - 30 days``.

    That made ``active_days`` exactly 0, so the death was recorded with no
    decay and no reset event -- a receipt that is wrong rather than absent.
    """
    seed_lifecycle(world, "p1")
    stale_life(world, "p1", days=5)
    # The life must actually be OVER for the death branch to read born_at:
    # reconcile only enters it once dies_at is in the past.
    mutate_row(world, "p1",
               lambda lc: lc.update(dies_at=time.time() - 5 * lifecycle.DAY))
    break_key(world, "p1", "born_at")

    assert_internal_defect(call(world, "GET", "", "p1"), "reconcile.death")


@pytest.mark.parametrize("key", ["version", "life_index"])
def test_a_legacy_rebirth_row_still_births_but_now_logs(world, key: str,
                                                       monkeypatch) -> None:
    """NOT a 500: this default is DELIBERATE, and a peer test pins it.

    A lifecycle row written before ``version``/``life_index`` existed must
    still be rebirthable, or those accounts are stranded for life.
    ``server/tests/test_lifecycle_contract.py``
    ::test_rebirth_works_on_a_row_written_without_version_or_life_index pins
    exactly that. What was genuinely wrong was only that the default was
    SILENT, so this asserts the log -- the half that was actually missing.

    Recorded here because the distinction is the whole point of the audit:
    "silent default" is not automatically "wrong". It is wrong when the
    value it invents is a plausible-looking lie about real state (a death
    date, a progress baseline), and acceptable when it is the documented
    legacy tolerance for a key that did not exist yet.
    """
    seen = []
    monkeypatch.setattr(lifecycle, "_log_internal",
                        lambda event, **f: seen.append((event, f)))
    seed_lifecycle(world, "p1")
    break_key(world, "p1", key)

    r = call(world, "POST", "/rebirth", "p1")
    assert r.status_code == 200, r.text[:400]
    assert r.json()["lifecycle"][key] == (1 if key == "version" else 2), r.text[:300]
    assert any(event == "lifecycle.legacy_counter_defaulted" for event, _ in seen), (
        "a legacy row was silently fabricated with no log line -- that was "
        "the actual defect at this site"
    )


@pytest.mark.parametrize("key", ["attended_days", "progress", "missed_days"])
def test_a_decay_silent_default_is_a_500(world, key: str) -> None:
    """These defaults were quiet in the most expensive direction.

    ``progress`` defaulted to 100.0, so a broken row was charged decay
    against a FABRICATED baseline and destroyed real progress. The
    cursor/cutoff pair defaulted to ``now`` on both sides, which cancels to
    ``closed_days == 0`` -- the player silently paid no decay at all. Neither
    outcome is what a broken row should produce.

    ``progress`` is removed from the PLAYER row as well, and that is the
    point of the site: the player's own progress is authoritative while it
    exists, so the lifecycle baseline is only read once it is gone. Only then
    did the old code fall through to its fabricated 100.0.
    """
    seed_lifecycle(world, "p1")
    stale_life(world, "p1", days=4)
    break_key(world, "p1", key)
    if key == "progress":
        db = world["players_db"]
        row = dict(db["p1"])
        row.pop("progress", None)
        db["p1"] = row

    assert_internal_defect(call(world, "GET", "", "p1"), "reconcile.decay")


def test_a_players_own_progress_still_wins_over_a_broken_baseline(world) -> None:
    """The other arm: the player row is authoritative while it is present.

    Without this, "progress is required on the lifecycle" reads as a rule, and
    a reviewer could conclude the player's own value should not be used. It
    must be: the fault only exists when BOTH are gone.
    """
    seed_lifecycle(world, "p1")
    stale_life(world, "p1", days=4)
    break_key(world, "p1", "progress")  # lifecycle baseline broken, player row intact

    r = call(world, "GET", "", "p1")
    assert r.status_code == 200, r.text[:400]


# ---------------------------------------------------------------------------
# The check-in handler and the module guard
# ---------------------------------------------------------------------------

def test_a_check_in_row_with_no_death_date_is_a_500(world) -> None:
    """Before: a bare subscript raised AFTER the check-in was committed.

    The caller got an unhandled 500 for a mutation that had already been
    written, which makes retrying ambiguous -- the worst possible answer to a
    question about money and attendance.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "dies_at")
    assert_internal_defect(call(world, "POST", "/check-in", "p1"), "check_in")


class _FakeRequest:
    """The three attributes ``require_lifecycle_guard`` reads off a request."""

    def __init__(self, pid: str, method: str) -> None:
        self.url = type("U", (), {"path": f"/probe/{pid}/do-thing"})()
        self.query_params: Dict[str, str] = {}
        self.method = method


def test_the_module_level_guard_faults_on_a_broken_row(world) -> None:
    """``require_lifecycle_guard`` reads ``dies_at`` bare on both its paths.

    Driven directly rather than through a mounted route, because the point is
    the guard's own read, not another router's copy of the same logic.
    """
    seed_lifecycle(world, "p1")
    break_key(world, "p1", "dies_at")

    with pytest.raises(HTTPException) as caught:
        lifecycle.require_lifecycle_guard(_FakeRequest("p1", "GET"))
    assert caught.value.status_code == 500, caught.value
    assert caught.value.detail == "internal_error", caught.value


# ---------------------------------------------------------------------------
# rebirth -- the two sites that FABRICATED a life instead of reporting one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["version", "life_index"])
def test_the_manual_rebirth_default_is_logged(world, key: str, monkeypatch) -> None:
    """The manual /rebirth twin of the automatic one: same contract.

    ``int(lc.get("version", 0)) + 1`` and ``int(lc.get("life_index", 1)) + 1``
    do not raise, and that is intended -- see the test above. The defect was
    the silence, so that is what this asserts.
    """
    seen = []
    monkeypatch.setattr(lifecycle, "_log_internal",
                        lambda event, **f: seen.append((event, f)))
    seed_lifecycle(world, "p1")
    break_key(world, "p1", key)

    r = call(world, "POST", "/rebirth", "p1")
    assert r.status_code == 200, r.text[:400]
    assert any(event == "lifecycle.legacy_counter_defaulted" for event, _ in seen), (
        f"{key} was fabricated on a legacy row with no log line"
    )


# ---------------------------------------------------------------------------
# The controls: prove these assertions can fail, and that faults are logged
# ---------------------------------------------------------------------------

def test_the_control_reproduces_the_fabricated_life() -> None:
    """Re-apply the pre-fix arithmetic and watch the fabrication come back.

    A green file of 500-assertions proves nothing on its own -- it is exactly
    what a suite looks like when its assertions cannot fail. This drives the
    old ``lc.get("dies_at", 0)`` logic through the REAL ``reconcile``, with
    the one required read neutralised, and asserts that a life is fabricated:
    status rewritten, ``completed_lives`` incremented, a receipt committed.
    """
    now = time.time()
    player = {"id": "ctl", "progress": 40.0,
              "lifecycle": lifecycle.new_state(now)}
    player["lifecycle"].pop("dies_at", None)

    # Pre-fix: the missing key read as 0, and 0 is long past `now`.
    assert player["lifecycle"].get("dies_at", 0) <= now, \
        "the re-injected default no longer reads as a past date"

    real = lifecycle._stored_float
    lifecycle._stored_float = lambda lc, key, **kw: float(lc.get(key, 0))
    try:
        lifecycle.reconcile(player, now)
    finally:
        lifecycle._stored_float = real

    lc = player["lifecycle"]
    # The fabricated evidence is NOT the status. dies_at read as 1970, so the
    # bounded loop walked from 1970 forward one full cycle per pass (death,
    # memorial, reservation, rebirth = 34 days) until the NEXT death date
    # finally landed past the observation time: MEASURED 610 completed lives
    # and a life_index of 611, on one request, from one row that simply never
    # had a death date. The status lands back on "active", so status alone
    # would have hidden all of it.
    assert lc["legacy"]["completed_lives"] > 1, \
        f"the control fabricated only {lc['legacy']['completed_lives']} life(s) " \
        f"-- it is not exercising the loop and the tests above are unproven"
    assert lc["life_index"] == lc["legacy"]["completed_lives"] + 1, \
        (f"life_index={lc['life_index']} and "
         f"completed_lives={lc['legacy']['completed_lives']} disagree")
    # MEASURED: the loop stops at the first pass whose NEXT dies_at is still
    # in the future, so the row it settles on is a life already UNDER WAY --
    # born_at <= now < dies_at, spanning exactly one first life. `born_at`
    # therefore reads BEHIND the observation time, not past it; the boundary
    # did move (off the observation time and onto a day boundary derived from
    # 1970), and that is the relationship worth pinning. Asserting
    # `born_at > now` here demanded a state the loop cannot produce.
    assert lc["born_at"] != now, \
        "the fabricated rebirth left the birth boundary at the observation time"
    assert lc["born_at"] <= now < lc["dies_at"], \
        (f"the loop must settle on a life in progress: born_at="
         f"{lc['born_at']} dies_at={lc['dies_at']} now={now}")
    assert lc["dies_at"] - lc["born_at"] == lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY, \
        (f"the fabricated life spans "
         f"{(lc['dies_at'] - lc['born_at']) / lifecycle.DAY} days, not one full "
         f"{lifecycle.INITIAL_LIFE_DAYS}-day first life")
    assert player.get("return_receipt") is not None, \
        "the fabricated transitions were not committed to a receipt"
    assert lc["status"] == "active", \
        "the control is expected to end on a full, fresh life -- that is what " \
        "makes the corruption invisible to a status check"


def test_every_stored_helper_logs_before_it_faults(monkeypatch) -> None:
    """A 500 with no log is a silent default wearing a status code.

    Asserts the "at least logs" half of the contract rather than assuming
    it: the helpers must reach the log before they raise.
    """
    seen = []

    def _capture(event: str, **fields: Any) -> None:
        seen.append((event, fields))

    monkeypatch.setattr(lifecycle, "_log_internal", _capture)

    with pytest.raises(HTTPException) as caught:
        lifecycle._stored({}, "dies_at", where="unit", player_id="p9")
    assert caught.value.status_code == 500
    assert seen, "no log line was emitted for an internal defect"
    assert seen[0][0] == "lifecycle.internal_defect", seen
    assert seen[0][1].get("key") == "dies_at", seen
    assert seen[0][1].get("where") == "unit", seen

    with pytest.raises(HTTPException):
        lifecycle._stored_float({"dies_at": "soon"}, "dies_at", where="unit")
    assert len(seen) == 2, f"a junk value faulted without logging: {seen}"


def test_a_healthy_row_faults_on_nothing() -> None:
    """The other half of the control: a complete row raises nothing.

    Without this arm, a helper that always raised would pass every test above
    while making the game unusable.
    """
    now = time.time()
    lc = lifecycle.new_state(now)
    lc["last_reconciled_at"] = now
    assert lifecycle._stored_float(lc, "dies_at", where="unit") == lc["dies_at"]
    assert lifecycle._stored(lc, "status", where="unit") == "active"
    assert lifecycle._stored(lc, "missed_days", where="unit") == 0
    assert lifecycle._stored(lc, "attended_days", where="unit") == []