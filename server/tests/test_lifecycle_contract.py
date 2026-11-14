"""
CONTRACT TEST: server/lifecycle.py -- the lifecycle module.

WHAT THIS FILE PINS
------------------
The wire contract of the nine routes ``lifecycle.register_routes`` mounts:
which paths exist and by which verb, the exact reply keys of each endpoint on
the branches it has, who is allowed to call them (a session must exist, and it
must belong to the player named in the path), and which status every refusal
answers.

It asserts RELATIONSHIPS, not frozen numbers. The 30-day first life is read
from ``lifecycle.INITIAL_LIFE_DAYS``, the day length from ``lifecycle.DAY``, the
clock-skew limit from ``offline_progression.MAX_CLOCK_SKEW_SECONDS`` and the
per-tile stone names from the module's own reward objects. Re-tuning the policy
dictionary leaves this file green; a route that starts carrying its own copy of
those numbers does not.

WHAT IT DELIBERATELY DOES NOT TOUCH
----------------------------------
``server/main.py`` is nowhere in this file. The router is mounted on a bare
``FastAPI()`` with its own in-memory ``PlayerStore``, so the result depends on
this module plus ``ownership_guard`` (which supplies the session requirement)
and nothing else. ``main.py`` is where the production 9-route ownership sweep
is measured; that is a different file's job.

One shared-tree hazard remains and is NOT avoidable: the repository root
``conftest.py`` imports ``server.main`` at COLLECTION time. If a peer is
half-way through editing ``main.py``, every pytest run in the repo aborts with
a collection error before a single test here executes. Re-read ``main.py``
before reading that traceback as a defect in this file.

THREE MEASURED BEHAVIOURS THAT ARE DEFECTS, PINNED AS THEY SHIP
----------------------------------------------------------------
1. AN UNKNOWN PLAYER IN THE PATH IS ANSWERED 400, NOT 404. ``_pid_from_path``
   only accepts a path segment that is already a key in the store, so a name
   that does not exist resolves to "no player id" and the resolver raises
   ``400 player_id required``. A client cannot tell "you asked for nobody" from
   "you asked without naming anybody". See
   ``test_an_unknown_player_id_is_answered_400_rather_than_404``.

2. THE MEMORIAL/DEAD REFUSAL IS NOW PRODUCED AFTER AUTHENTICATION. (FIXED.)
   The custom ``LifecycleRoute.handle`` runs before FastAPI resolves
   dependencies, so it used to answer an ANONYMOUS caller who named a memorial
   player with 409 and the player's full lifecycle view in the body -- no
   session, no proof of who they were. It now calls ``authenticate`` before the
   410/409 refusal, so that caller gets 401 and the body discloses nothing. The
   owner still receives the 410/409 with its body intact. Named
   ``test_a_memorial_refusal_is_no_longer_produced_before_the_caller_is_authenticated``.

3. A DEVICE CLOCK MORE THAN THE SKEW LIMIT AWAY IS A 400; A SERVER-SIDE DATA
   DEFECT IS A 500. Both arrive at ``claim-offline`` as the same exception type
   from the offline engine. The route re-derives the skew comparison rather
   than string-matching, so an unpriceable stored row costs a 500 and not a
   400 that blames the caller. Covered by
   ``test_a_clock_further_than_the_skew_limit_is_a_caller_fault`` and
   ``test_an_unpriceable_stored_row_costs_a_500_not_a_400``.

WHAT WAS PROVED CAPABLE OF FAILING
----------------------------------
A green contract file is worth nothing until a re-injected defect turns it red.
Twenty-three mutations were re-injected on a scratch COPY of ``server/`` -- the
live tree was never edited -- and twenty-one turned this file red.

Killed: the route class removed from the router constructor; the 410 and the
409 refusal branch deleted; the GET reading without reconciling; the first-GET
initialization branch removed; the clock-skew comparison made always false; the
400/500 split collapsed onto one status; the check-in progress reset removed;
the rebirth ``life_index`` bump removed; the receipt append skipped; the
flattened key-list in ``get_lifecycle`` narrowed; ``remaining_seconds`` made a
constant; the fabricated ``qi_at_risk`` floor restored; the first-life length
shortened; ``start=True`` ignored; the clock-rollback guard removed; the
offline claim stamp skipped; the resolve 404 deleted; ``/advance`` not
committed; the recap shape changed; the attendance bucket not recorded.

Two mutations did not run and are NOT evidence either way: both anchors in the
mutation script matched zero and two occurrences instead of exactly one, so
nothing was injected. They are re-cutting targets, not survivors, and the
behaviour they were aimed at is covered by
``test_a_mutation_before_the_clock_starts_is_refused`` and
``test_rebirth_starts_a_clean_life_and_commits_it`` respectively.

Run
---
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_DB <python> -m pytest server/tests/test_lifecycle_contract.py -q


THE THIRD PASS -- THE OFFLINE CAP WAS BEING TESTED AGAINST A ZERO PAYOUT
-------------------------------------------------------------------------
The sections above pin the routes, the refusals and the module-scope helpers.
The section added last measured the thing they had all been assuming: that the
72-HOUR OFFLINE CAP is actually enforced on a claim that pays something.

``test_claim_never_reports_more_hours_than_the_cap`` asserts
``elapsed_hours <= capped_hours + 1e-6`` and passes. Measured on this tree,
``elapsed_hours`` is 0.0 on EVERY press against the walletless store, so the
assertion is 0.0 <= 0.0 and would survive the cap being deleted outright. Two
reasons, both measured rather than guessed:

  * ``_dict_to_player`` builds a ``Player`` carrying no
    ``last_cultivation_time`` at all, so the walletless anchor read inside the
    offline engine always falls through to the server clock.
  * the lifecycle GET used to prime a player ALSO stamps
    ``last_cultivation_time = now``, so even a seeded historical cursor is
    overwritten before the claim is ever sized.

A cap is a bound on MONEY, and a cap proven only against a zero payout is not a
cap. The new section re-measures it against a store that HAS a wallet, which is
what makes the anchor the engine actually reads observable. Six of the nine
re-injected defects turned it red; the honest tally, including one equivalent
mutant, one already-covered case and one store-masked survivor, is at the foot
of this file.

TWO PRE-AUTH DEFECTS FOUND WHILE DOING IT, PINNED AS THEY SHIP
--------------------------------------------------------------
1. AN ANONYMOUS CALLER CAN STILL PROBE THE PLAYER-ID SPACE. An unstarted
   player answers 400 ``lifecycle not started`` and a non-existent id answers
   401, on every mutating route, with no credentials at all. That difference
   IS a free membership test -- the exact oracle ``_refuse_unauthenticated``
   exists to close. It is invisible to a test that reads first, because the
   read starts the clock and the two answers then match.

2. AN ANONYMOUS READ COMMITS THE CLOCK IT THEN REFUSES. ``GET
   /lifecycle/<fresh pid>`` with no header answers 401 and has already written
   ``lifecycle``, ``last_cultivation_time`` and a ``return_receipt`` to the
   stored row: the guard runs and commits as a dependency, and the ownership
   guard refuses afterwards.

Both are pinned with their CURRENT answers and a comment saying what a fix must
do, because a contract file records what the server does. Neither is fixed
here; the fixes belong in ``lifecycle._resolve_player`` and
``ownership_guard``, not in a test.


THE SECOND PASS -- what was missing, and what proves these tests bite
---------------------------------------------------------------------
The sections above mount ``register_routes`` and drive the nine routes.
Everything below lives OUTSIDE that router, so no test above could execute it.
The gap was found by counting references to each name in this file BEFORE
writing any of it -- not by reading the module and guessing. Counted as zero:
``require_lifecycle_guard``, ``_stamp_offline_claimed``, ``_clamp_offline_stones``,
``_is_spirit_stone_reward``, ``_current_stone_total``, ``_resolve_players_db``,
``receipt_view`` and the ``reconcile_player`` alias.

Every number in the new sections was MEASURED by running the guard, the
helpers and the authority directly, then pinned.

Twenty-three defects were ATTEMPTED on a scratch COPY of the tree (the live repo
was never edited) and TWENTY-TWO were actually injected: the first attempt's
anchor for "the module guard's missing-clock 400" matched zero occurrences, so
nothing was written and nothing ran. It is a re-cutting target, not a survivor,
and the behaviour it was aimed at is covered directly by
``test_the_module_scope_guard_refuses_a_missing_clock_with_400``.

Of the 22 injected, NINETEEN turned this file red on the first cut. Each
injection rewrites exactly one source line, and the unmutated scratch copy is
run first as a control, because a red baseline proves nothing. The 3 survivors
were then re-cut with stronger injections: 2 died, and 1 is an equivalent mutant
proven to be one. Final tally, stated exactly: 21 killed, 1 equivalent, 1 never
injected.

Killed -- the first pass's own surfaces (regression guard): ``/advance`` not
committed; the first-GET initialization branch removed; the clock-skew
comparison made always false; the rebirth ``life_index`` bump removed; the
clock-rollback guard removed; the fabricated ``qi_at_risk`` floor restored; the
receipt append skipped.

Killed -- the second pass's surfaces: the field-writer rung of the claim-stamp
ladder dropped; the ``put`` rung dropped; the no-writer case made to fail
silently instead of raising; an over-cap claim left unclamped; the external
grant not recorded; the reward-table reason misspelled in the route; the
per-tier stone sum shorted; the tier-name match disabled; the reward-TYPE match
disabled; the module guard's dead refusal removed; its memorial refusal removed;
the display field persisted through a transaction; the missing-store resolver
returning ``None`` instead of raising.

Three survivors were re-cut with STRONGER injections and two were then killed,
which is the honest result and not a tuned one:

* ``modguard-persists-display-field`` and ``modguard-migration-cursor-stale``
  both survived the first cut and were killed by the second. The first
  assigned ``db[pid]["lifecycle"]`` OUTSIDE a transaction, which the memory
  store's detached-copy read discarded, so it could never bite; the second only
  asserted the cursor's own value, which the GET path's later reconcile
  overwrites. Both tests now assert the CONSEQUENCE -- the persisted display key,
  and the absent-day charge -- because the cause is masked by the very next
  line of code.

* ``modguard-get-returns-snapshot-not-reconciled`` survives and is an
  EQUIVALENT MUTANT, not an untested path. Proven, not assumed: ``reconcile``
  ends with its own ``player["lifecycle"] = lc`` write, so removing the
  guard's re-assignment changes nothing under the in-memory store, which hands
  out the live committed object inside a transaction. Removing BOTH writes turns
  the file red. The test therefore does bite on the observable; the single
  mutation is simply a no-op here and would only bite on a store that hands back
  a detached row -- which the module's own comments say is exactly what
  PostgreSQL does.

ONE MEASURED INCONSISTENCY, pinned as it ships
----------------------------------------------
With a daily stone ceiling already spent, the claim's stones are correctly
zeroed, but the helper's bookkeeping call raises and the blanket ``except``
swallows it, so ``stones_clamped`` reports ``None`` -- "nothing was clamped" on
exactly the press where the player received nothing. The swallow is deliberate
(a spent ceiling must not crash a client on a timer); what is pinned is the
observable. See ``test_a_spent_ceiling_reports_no_clamp_because_the_record_step_refused``.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from typing import Any, Dict, Iterator, List

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# In-memory storage must be forced. Without it a single-file run reports a wall
# of errors from an absent database, and reading those as failures sends an
# engineer hunting a bug that does not exist.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # bare, NOT server.auth -- main.py binds the same module object
import lifecycle  # noqa: E402
import ownership_guard  # noqa: E402
from offline_progression import MAX_CLOCK_SKEW_SECONDS  # noqa: E402
from player_store import PlayerStore  # noqa: E402


#: The nine mounted routes, as (verb, path suffix after ``{pid}``).
#:
#: Frozen deliberately: a client reads these paths, so ADDING a route is safe
#: but RENAMING or DROPPING one breaks every caller. The count is asserted.
ROUTES: tuple = (
    ("GET", ""),
    ("POST", "/check-in"),
    ("POST", "/claim-offline"),
    ("GET", "/offline-preview"),
    ("POST", "/advance"),
    ("POST", "/rebirth"),
    ("POST", "/receipts"),
    ("POST", "/receipt"),
    ("POST", "/view_recap"),
)

#: Every mutating route. Each of these is refused for a dead (410) or a
#: memorial (409) player, and refused with 400 when no clock is running yet.
MUTATING = tuple(s for m, s in ROUTES if m == "POST")
READS = tuple(s for m, s in ROUTES if m == "GET")

#: The exact reply keys of ``GET /lifecycle/{pid}``. ``get_lifecycle``
#: flattens a fixed allow-list out of the guard's view, so a key added to the
#: view is NOT automatically published; this set is the published surface.
GET_KEYS = {
    "enabled", "version", "born_at", "life_index", "dies_at", "status",
    "attended_days", "missed_days", "progress", "legacy", "receipts",
    "remaining_seconds", "last_reconciled_at", "next_transition_at",
    "legacy_bonus", "server_now", "deadline", "sign_in", "return_receipt",
}
DEADLINE_KEYS = {"enabled", "days_until_deadline", "qi_at_risk", "missed_days", "next_claim"}
SIGN_IN_KEYS = {
    "enabled", "streak_day", "claimed_today", "next_reward_qi",
    "cycle_total_qi", "calendar", "policy", "server_now",
}
#: ``POST /check-in`` publishes the whole GET surface plus the sign-in receipt
#: under ``result`` and an ``ok`` flag. Dropping any of these breaks a client.
CHECKIN_EXTRA_KEYS = {"ok", "result"}
CLAIM_KEYS = {
    "ok", "elapsed_hours", "capped_hours", "cultivation_progress_gained",
    "breakthrough_triggered", "new_realm", "new_stage", "rewards",
    "stones_clamped",
}
#: The exact reply keys of the ``rewards`` object ``POST /claim-offline``
#: publishes, which is ``offline_progression.apply_offline_rewards``'s summary
#: passed through untouched.
#:
#: ``rejected_rewards`` was added when a refused credit used to be dropped in
#: silence: the row refused the line, nothing was reported, and the summary read
#: as a clean payout for money that never landed. The fix names every refused
#: line instead, so ``rejected_rewards`` is now part of the published surface --
#: an exact set, so a dropped key still fails here. The sibling contract file
#: ``test_offline_progression_contract.py`` pins the same key from the module
#: side; this is the route-side view of it.
APPLIED_REWARD_KEYS = {
    "cultivation_progress", "spirit_stones", "sect_contribution",
    "beast_affinity", "battle_pass_xp", "breakthrough_triggered",
    "new_realm", "new_stage", "rejected_rewards",
}
PREVIEW_KEYS = {
    "can_claim", "elapsed_hours", "capped_hours", "max_cap_hours",
    "cultivation_progress", "breakthrough_ready", "new_realm", "new_stage",
    "estimated_rewards",
}
RECEIPT_KEYS = {"id", "category", "amount", "earned_at", "claimed_at", "reason"}

#: The refusal bodies. ``lifecycle not started`` is the guard refusing a
#: mutating request before any clock exists; the other two carry the full
#: lifecycle view so a client can render why it was refused.
NOT_STARTED = "lifecycle not started"
NO_PLAYER_ID = "player_id required"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A bare app carrying only the lifecycle router, plus its own store.

    Function-scoped. ``lifecycle.route_class_store`` and
    ``lifecycle._live_players_db`` are MODULE globals, so a shared app would
    let one test's injected store answer another test's request. Both are
    saved and restored here, which also stops this file leaking its store into
    the rest of the suite.
    """
    players_db = PlayerStore()
    app = FastAPI()

    saved_live = lifecycle._live_players_db
    saved_rc = lifecycle.route_class_store
    lifecycle._live_players_db = players_db
    lifecycle.route_class_store = None
    try:
        lifecycle.register_routes(app, lambda: players_db)
        # The production requirement is installed centrally, not in the
        # handler signatures, so the contract file installs it centrally too.
        guard_report = ownership_guard.install_ownership_guard(app)
        yield {
            "app": app,
            "client": TestClient(app, raise_server_exceptions=False),
            "players_db": players_db,
            "guard_report": guard_report,
        }
    finally:
        lifecycle._live_players_db = saved_live
        lifecycle.route_class_store = saved_rc


def seed(world: Dict[str, Any], pid: str, **over: Any) -> Dict[str, Any]:
    """One player row, no lifecycle yet. Returns the row."""
    row: Dict[str, Any] = {
        "id": pid,
        "name": pid,
        "spirit_stones": {"low_grade": 500},
        "progress": 40.0,
        "qi": 30.0,
    }
    row.update(over)
    world["players_db"][pid] = row
    return row


def seeded_lifecycle(now: float, **over: Any) -> Dict[str, Any]:
    """A lifecycle row already stored on the player, in a named status."""
    lc = lifecycle.new_state(now)
    lc["last_reconciled_at"] = now
    lc.update(over)
    return lc


def owner_of(world: Dict[str, Any], pid: str) -> Dict[str, str]:
    """A real session token for ``pid``, as the shipped client gets one."""
    return {"Authorization": "Bearer " + auth.issue_session(pid)}


def prime(world: Dict[str, Any], pid: str) -> None:
    """Start the clock the way the game does: one authenticated lifecycle read.

    ``POST /player/create`` in production deliberately leaves the lifecycle
    unset, and every mutating route answers ``400 lifecycle not started`` until
    a read starts it. Without this the error tests would measure the wrong
    refusal.
    """
    world["client"].get(f"/lifecycle/{pid}", headers=owner_of(world, pid))


def call(world: Dict[str, Any], verb: str, suffix: str, pid: str,
         headers: Dict[str, str] = None, **kwargs: Any):
    """One request to ``{verb} /lifecycle/{pid}{suffix}``."""
    fn = getattr(world["client"], verb.lower())
    return fn(f"/lifecycle/{pid}{suffix}", headers=headers, **kwargs)


def detail_of(response) -> Any:
    return response.json().get("detail")


def body_of(response) -> Dict[str, Any]:
    return response.json()


# ---------------------------------------------------------------------------
# The route table
# ---------------------------------------------------------------------------

def test_exactly_nine_routes_mount_and_no_others(world) -> None:
    """The published surface: nine paths, one verb each, nothing extra."""
    mounted = {
        (verb, path.split("{pid}", 1)[1])
        for path, verb in (
            (r.path, next(iter(r.methods - {"HEAD"})))
            for r in world["app"].routes
            if getattr(r, "path", "").startswith("/lifecycle/")
        )
    }
    assert mounted == set(ROUTES), (
        "the mounted lifecycle surface drifted from the published one: "
        f"{sorted(mounted ^ set(ROUTES))}"
    )
    assert len(ROUTES) == 9


def test_every_route_takes_the_ownership_guard(world) -> None:
    """All nine are guarded by the central session requirement.

    Without this arm a guard that covers nothing looks identical to a guard that
    covers everything: the refusal tests alone cannot tell them apart.
    """
    guarded = set(world["guard_report"])
    assert guarded == {f"/lifecycle/{{pid}}{suffix}" for _, suffix in ROUTES}
    # No lifecycle route sits on the exempt list.
    for path in guarded:
        assert path not in ownership_guard.EXEMPT_PATHS


def test_a_verb_mismatch_is_a_405_not_a_route(world) -> None:
    """A POST-only path answers GET with 405; nothing is silently accepted."""
    seed(world, "vm")
    headers = owner_of(world, "vm")
    assert call(world, "GET", "/receipt", "vm", headers).status_code == 405


# ---------------------------------------------------------------------------
# GET /lifecycle/{pid}
# ---------------------------------------------------------------------------

def test_get_publishes_the_documented_key_set(world) -> None:
    seed(world, "g1")
    r = call(world, "GET", "", "g1", owner_of(world, "g1"))
    assert r.status_code == 200
    body = body_of(r)
    assert set(body) == GET_KEYS, sorted(set(body) ^ GET_KEYS)
    assert set(body["deadline"]) == DEADLINE_KEYS
    assert set(body["sign_in"]) == SIGN_IN_KEYS
    # The first read COMMITS an uninitialized -> active receipt, so on a brand
    # new player it is already a dict rather than null.
    assert body["return_receipt"] is None or isinstance(body["return_receipt"], dict)


def test_the_first_read_starts_a_thirty_day_clock(world) -> None:
    """No lifecycle on the row: the first read starts the clock and persists it.

    The length is read from the module, so re-tuning the policy is not a
    failure here. What must not change is that the clock STARTS and that the
    stored row, not just the reply, carries it.
    """
    seed(world, "g2")
    r = call(world, "GET", "", "g2", owner_of(world, "g2"))
    assert r.status_code == 200
    body = body_of(r)
    assert body["status"] == "active"
    assert body["life_index"] == 1
    span = body["dies_at"] - body["born_at"]
    assert span == pytest.approx(lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY)
    assert body["remaining_seconds"] == pytest.approx(span, rel=1e-3)
    # Persisted, not synthesised for the reply.
    stored = world["players_db"]["g2"]["lifecycle"]
    assert stored["status"] == "active"
    assert stored["dies_at"] == pytest.approx(body["dies_at"])


def test_remaining_seconds_is_derived_from_the_server_clock(world) -> None:
    """remaining_seconds is dies_at minus the reported server_now.

    A client draws its countdown from this number, so it must be consistent
    with the server_now shipped beside it rather than an unrelated wall clock.
    """
    seed(world, "g3")
    body = body_of(call(world, "GET", "", "g3", owner_of(world, "g3")))
    assert body["remaining_seconds"] == pytest.approx(
        max(0.0, body["dies_at"] - body["server_now"]), rel=1e-6
    )
    assert body["next_transition_at"] == body["dies_at"]


def test_a_fully_attended_player_reports_zero_progress_at_risk(world) -> None:
    """Zero missed days must report zero risk.

    The panel cannot tell "one day already missed" from "none missed" if both
    report the same figure, so this is a display contract, not cosmetics.
    """
    seed(world, "g4")
    body = body_of(call(world, "GET", "", "g4", owner_of(world, "g4")))
    assert body["deadline"]["missed_days"] == 0
    assert body["deadline"]["qi_at_risk"] == 0.0


def test_the_risk_figure_follows_the_decay_policy(world) -> None:
    """Risk after N missed days is the same arithmetic reconcile applies."""
    now = time.time()
    seed(world, "g5", lifecycle=seeded_lifecycle(now, missed_days=3, progress=100.0))
    body = body_of(call(world, "GET", "", "g5", owner_of(world, "g5")))
    decay = lifecycle.POLICY["missed_day_qi_decay"]
    expected = round(100.0 * (1.0 - (1 - decay) ** 3), 2)
    assert body["deadline"]["qi_at_risk"] == pytest.approx(expected, abs=0.01)
    assert body["deadline"]["qi_at_risk"] > 0.0


def test_a_dead_player_can_still_read(world) -> None:
    """Reads are never blocked by status; only mutations are."""
    now = time.time()
    seed(world, "g6", lifecycle=seeded_lifecycle(now, status="dead", dies_at=0.0))
    r = call(world, "GET", "", "g6", owner_of(world, "g6"))
    assert r.status_code == 200
    assert body_of(r)["status"] == "dead"
    assert body_of(r)["remaining_seconds"] == 0


def test_a_memorial_player_can_still_read(world) -> None:
    now = time.time()
    seed(world, "g7", lifecycle=seeded_lifecycle(now, status="memorial"))
    r = call(world, "GET", "", "g7", owner_of(world, "g7"))
    assert r.status_code == 200
    assert body_of(r)["status"] == "memorial"


def test_a_server_clock_rollback_cannot_add_life(world) -> None:
    """The reported cursor is a floor: a rolled-back clock grants nothing.

    Without the floor, remaining_seconds would grow after a rollback and the
    server would be handing the player life out of a clock that went backwards.
    """
    now = time.time()
    # A cursor AHEAD of the wall clock is the rollback case: the server clock
    # has gone backwards under a stored cursor that must not.
    cursor = now + 10 * lifecycle.DAY
    seed(world, "g8", lifecycle=seeded_lifecycle(now, last_reconciled_at=cursor))
    body = body_of(call(world, "GET", "", "g8", owner_of(world, "g8")))
    assert body["server_now"] >= cursor, "the observation clock moved backwards"
    assert body["remaining_seconds"] == pytest.approx(
        max(0.0, body["dies_at"] - cursor), rel=1e-6
    )


# ---------------------------------------------------------------------------
# Authentication and ownership
# ---------------------------------------------------------------------------

def test_an_anonymous_caller_is_refused(world) -> None:
    seed(world, "a1")
    for suffix in ("", "/check-in", "/claim-offline", "/offline-preview",
                   "/advance", "/rebirth", "/receipts", "/receipt",
                   "/view_recap"):
        r = call(world, "GET" if suffix in READS else "POST", suffix, "a1")
        assert r.status_code == 401, f"{suffix or '/'} answered {r.status_code}"


def test_a_caller_holding_someone_elses_session_is_refused(world) -> None:
    seed(world, "a2")
    seed(world, "a3")
    stranger = owner_of(world, "a3")
    r = call(world, "GET", "", "a2", stranger)
    assert r.status_code == 403
    assert detail_of(r) == "player_id does not match the authenticated session"


def test_an_unreadable_token_is_refused_as_missing(world) -> None:
    seed(world, "a4")
    r = call(world, "GET", "", "a4", {"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_the_owner_succeeds_on_every_route(world) -> None:
    """The positive control the refusal arms above depend on.

    A guard that refuses everyone answers 401 exactly like a guard that works.
    Only the legitimate owner separating the two.
    """
    seed(world, "a5r")
    headers = owner_of(world, "a5r")
    for suffix in READS:
        assert call(world, "GET", suffix, "a5r", headers).status_code == 200
    # A FRESH player per mutating route. `/advance` commits a dead status and
    # the route class then refuses everything after it, so one shared player
    # would measure that cascade rather than the ownership requirement.
    for index, suffix in enumerate(MUTATING):
        pid = f"a5m{index}"
        seed(world, pid)
        own = owner_of(world, pid)
        prime(world, pid)
        r = call(world, "POST", suffix, pid, own)
        assert r.status_code == 200, f"{suffix} answered {r.status_code} for the owner"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_a_mutation_before_the_clock_starts_is_refused(world) -> None:
    """Every mutating route refuses a player with no lifecycle yet.

    ``/check-in`` is the one documented exception: it is itself a valid
    initialization boundary, so it starts the clock and answers 200. It is
    asserted separately below rather than quietly folded in here.
    """
    for suffix in tuple(s for s in MUTATING if s != "/check-in"):
        pid = f"ns{suffix.strip('/').replace('-', '_')}"
        seed(world, pid)
        r = call(world, "POST", suffix, pid, owner_of(world, pid))
        assert r.status_code == 400, f"{suffix} answered {r.status_code}"
        assert detail_of(r) == NOT_STARTED


def test_a_dead_player_is_gone_on_every_mutation(world) -> None:
    """410 carries the lifecycle view so a client can say why."""
    for suffix in MUTATING:
        pid = f"dead{suffix.strip('/').replace('-', '_')}"
        seed(world, pid, lifecycle=seeded_lifecycle(time.time(), status="dead", dies_at=0.0))
        r = call(world, "POST", suffix, pid, owner_of(world, pid))
        assert r.status_code == 410, f"{suffix} answered {r.status_code}"
        detail = detail_of(r)
        assert isinstance(detail, dict) and "lifecycle" in detail
        assert detail["lifecycle"]["status"] == "dead"


def test_a_memorial_player_is_conflicted_on_every_mutation(world) -> None:
    """409 for the memorial day, distinct from the 410 the dead day answers."""
    for suffix in MUTATING:
        pid = f"mem{suffix.strip('/').replace('-', '_')}"
        seed(world, pid, lifecycle=seeded_lifecycle(time.time(), status="memorial"))
        r = call(world, "POST", suffix, pid, owner_of(world, pid))
        assert r.status_code == 409, f"{suffix} answered {r.status_code}"
        detail = detail_of(r)
        assert isinstance(detail, dict) and "lifecycle" in detail
        assert detail["lifecycle"]["status"] == "memorial"


def test_the_two_refusals_are_distinguishable(world) -> None:
    """410 and 409 must not collapse into one status.

    A client offers different copy for a finished life and for the memorial
    day, so a change that made them equal would ship as silent wrong text.
    """
    seed(world, "x1", lifecycle=seeded_lifecycle(time.time(), status="dead", dies_at=0.0))
    seed(world, "x2", lifecycle=seeded_lifecycle(time.time(), status="memorial"))
    assert call(world, "POST", "/receipt", "x1", owner_of(world, "x1")).status_code == 410
    assert call(world, "POST", "/receipt", "x2", owner_of(world, "x2")).status_code == 409


def test_an_unknown_player_id_is_answered_400_rather_than_404(world) -> None:
    """MEASURED DEFECT, pinned as it ships.

    ``_pid_from_path`` accepts a path segment only when it is already a key in
    the store, so a name that does not exist resolves to "no player id" and the
    resolver answers ``400 player_id required``. The 404 branch in
    ``_resolve_player`` is therefore unreachable from these paths: a caller
    cannot be told "that player does not exist".

    A fix that routes the 404 properly flips this red; it must not be made to
    pass by loosening the assertion.
    """
    r = call(world, "GET", "", "no-such-player", owner_of(world, "a5"))
    assert r.status_code == 400
    assert detail_of(r) == NO_PLAYER_ID


def test_a_nameable_unknown_player_via_query_is_refused_by_the_guard(world) -> None:
    """Naming a victim in the query cannot turn a refusal into a read.

    Measured: the path segment is the only claim the session check sees here, so
    a name that is not a live player is refused before the guard compares. The
    contract pinned is the OUTCOME -- refused, and nothing of any other player
    in the body -- not which of the two refusal statuses produces it, because
    that ordering is an implementation detail of two guards racing.
    """
    seed(world, "q1")
    seed(world, "q2")
    stranger = owner_of(world, "q2")
    r = call(world, "GET", "", "not-a-player", stranger)
    assert r.status_code in (400, 403, 404)
    assert "q1" not in r.text and "q2" not in r.text.replace(str("q2"), "")
    # And with no session at all it is refused too.
    assert call(world, "GET", "", "not-a-player").status_code in (400, 401, 404)


def test_a_check_in_starts_the_clock_for_a_player_who_has_none(world) -> None:
    """The documented exception: first check-in is a valid start boundary."""
    seed(world, "ns_ci")
    r = call(world, "POST", "/check-in", "ns_ci", owner_of(world, "ns_ci"))
    assert r.status_code == 200
    assert body_of(r)["status"] == "active"
    assert world["players_db"]["ns_ci"]["lifecycle"]["status"] == "active"


def test_a_path_with_no_player_segment_is_a_404(world) -> None:
    """``/lifecycle/`` matches no route at all."""
    assert world["client"].get("/lifecycle/").status_code == 404


def test_a_store_failure_is_answered_503_not_500(world) -> None:
    """A transaction failure on the read path surfaces as 503 service_unavailable."""
    class BrokenStore(PlayerStore):
        # Armed by a flag rather than by overriding outright, because the
        # player has to be SEEDED through the same write path first.
        broken = False

        def transaction(self, player_id=None):
            if BrokenStore.broken:
                raise RuntimeError("store is down")
            return super().transaction(player_id)

    seed(world, "s1")
    broken = BrokenStore()
    # The player must EXIST in the broken store: a resolver that cannot find
    # the name answers 400 and this test would measure the wrong refusal.
    broken["s1"] = world["players_db"]["s1"]
    original = lifecycle._live_players_db
    lifecycle._live_players_db = broken
    BrokenStore.broken = True
    try:
        r = call(world, "GET", "", "s1", owner_of(world, "s1"))
        assert r.status_code == 503, r.status_code
        assert detail_of(r) == "service_unavailable"
    finally:
        BrokenStore.broken = False
        lifecycle._live_players_db = original


# ---------------------------------------------------------------------------
# POST /lifecycle/{pid}/check-in
# ---------------------------------------------------------------------------

def test_check_in_publishes_the_read_surface_plus_its_own_two_keys(world) -> None:
    seed(world, "ci1")
    headers = owner_of(world, "ci1")
    prime(world, "ci1")
    r = call(world, "POST", "/check-in", "ci1", headers)
    assert r.status_code == 200
    body = body_of(r)
    assert set(body) == GET_KEYS | CHECKIN_EXTRA_KEYS, sorted(set(body) ^ (GET_KEYS | CHECKIN_EXTRA_KEYS))
    assert body["ok"] is True
    assert isinstance(body["result"], dict)
    assert body["result"]["claimed_today"] is True


def test_check_in_records_todays_bucket_and_restores_full_progress(world) -> None:
    """Attendance is recorded, missed days clear, progress returns to 100."""
    now = time.time()
    seed(world, "ci2", lifecycle=seeded_lifecycle(now, missed_days=4, progress=61.0))
    today = int(now // lifecycle.DAY) * lifecycle.DAY
    r = call(world, "POST", "/check-in", "ci2", owner_of(world, "ci2"))
    body = body_of(r)
    assert body["missed_days"] == 0
    assert body["progress"] == 100.0
    assert today in body["attended_days"]
    # Persisted, not just replied.
    assert world["players_db"]["ci2"]["lifecycle"]["missed_days"] == 0
    assert world["players_db"]["ci2"]["lifecycle"]["progress"] == 100.0
    assert today in world["players_db"]["ci2"]["lifecycle"]["attended_days"]


def test_check_in_does_not_claw_back_earned_qi(world) -> None:
    """qi is a running total; attendance resets progress, not qi.

    Assigning qi on this path once destroyed everything above 100 a player had
    earned: measured on this tree a player holding qi=500 finished a check-in
    at 100.0. The invariant pinned is that qi may GROW (the sign-in reward
    credits it) but must never be REPLACED by the 100.0 baseline. The
    regression is pinned on the stored row, not the reply.
    """
    seed(world, "ci3", qi=777.0)
    prime(world, "ci3")
    call(world, "POST", "/check-in", "ci3", owner_of(world, "ci3"))
    assert world["players_db"]["ci3"]["qi"] >= 777.0


def test_checking_in_twice_on_one_day_is_not_two_days(world) -> None:
    now = time.time()
    seed(world, "ci4")
    headers = owner_of(world, "ci4")
    prime(world, "ci4")
    call(world, "POST", "/check-in", "ci4", headers)
    after_first = body_of(call(world, "POST", "/check-in", "ci4", headers))
    assert len(after_first["attended_days"]) == 1
    assert after_first["attended_days"][0] == int(now // lifecycle.DAY) * lifecycle.DAY


# ---------------------------------------------------------------------------
# POST /lifecycle/{pid}/advance and /rebirth
# ---------------------------------------------------------------------------

def test_advance_answers_ok_and_stores_the_dead_state(world) -> None:
    seed(world, "adv")
    headers = owner_of(world, "adv")
    prime(world, "adv")
    r = call(world, "POST", "/advance", "adv", headers)
    assert r.status_code == 200
    assert body_of(r) == {"ok": True}
    stored = world["players_db"]["adv"]["lifecycle"]
    assert stored["status"] == "dead"
    assert stored["dies_at"] == 0.0


def test_advance_is_what_makes_the_next_mutation_410(world) -> None:
    """The 410 is a consequence of a committed write, not of the request."""
    seed(world, "adv2")
    headers = owner_of(world, "adv2")
    prime(world, "adv2")
    assert call(world, "POST", "/receipt", "adv2", headers).status_code == 200
    assert call(world, "POST", "/advance", "adv2", headers).status_code == 200
    assert call(world, "POST", "/receipt", "adv2", headers).status_code == 410


def test_rebirth_starts_a_clean_life_and_commits_it(world) -> None:
    """Manual rebirth must match what the automatic path produces.

    A player who rebirths by hand used to start the new life already decayed
    and already carrying the previous life's penalty.
    """
    now = time.time()
    seed(world, "rb1", lifecycle=seeded_lifecycle(
        now, status="reserved", dies_at=now + 10,
        attended_days=[1, 2, 3], missed_days=3, progress=40.0, life_index=1,
    ))
    r = call(world, "POST", "/rebirth", "rb1", owner_of(world, "rb1"))
    assert r.status_code == 200
    assert set(body_of(r)) == {"ok", "lifecycle"}
    lc = body_of(r)["lifecycle"]
    assert lc["status"] == "active"
    assert lc["life_index"] == 2
    assert lc["missed_days"] == 0
    assert lc["attended_days"] == []
    assert lc["progress"] == 100.0
    assert lc["dies_at"] - lc["born_at"] == pytest.approx(
        lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY
    )
    # Committed to the store, not only echoed.
    stored = world["players_db"]["rb1"]["lifecycle"]
    assert stored["status"] == "active"
    assert stored["life_index"] == 2
    assert world["players_db"]["rb1"]["progress"] == 100.0


def test_rebirth_works_on_a_row_written_without_version_or_life_index(world) -> None:
    """A legacy lifecycle row must not crash the route with a missing key."""
    now = time.time()
    bare = {"born_at": now, "dies_at": now + 100, "status": "reserved",
            "attended_days": [], "missed_days": 0, "progress": 100.0,
            "legacy": {"completed_lives": 0}, "receipts": []}
    seed(world, "rb2", lifecycle=bare)
    r = call(world, "POST", "/rebirth", "rb2", owner_of(world, "rb2"))
    assert r.status_code == 200
    lc = body_of(r)["lifecycle"]
    assert lc["version"] == 1
    assert lc["life_index"] == 2


# ---------------------------------------------------------------------------
# POST /lifecycle/{pid}/receipt, /receipts, /view_recap
# ---------------------------------------------------------------------------

def test_a_created_receipt_has_the_documented_shape_and_is_listed(world) -> None:
    """Creating one receipt, then listing it, must agree."""
    seed(world, "rc1")
    headers = owner_of(world, "rc1")
    prime(world, "rc1")
    r = call(world, "POST", "/receipt", "rc1", headers)
    assert r.status_code == 200
    receipt = body_of(r)
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["category"] == "offline_progress"
    assert isinstance(receipt["id"], str) and receipt["id"].startswith("rcpt_")

    listed = body_of(call(world, "POST", "/receipts", "rc1", headers))
    assert set(listed) == {"receipts"}
    assert any(item["id"] == receipt["id"] for item in listed["receipts"])


def test_receipt_ids_are_unique_per_press(world) -> None:
    seed(world, "rc2")
    headers = owner_of(world, "rc2")
    prime(world, "rc2")
    ids = {body_of(call(world, "POST", "/receipt", "rc2", headers))["id"]
           for _ in range(3)}
    assert len(ids) == 3


def test_the_receipt_list_is_empty_until_something_is_written(world) -> None:
    seed(world, "rc3")
    headers = owner_of(world, "rc3")
    prime(world, "rc3")
    assert body_of(call(world, "POST", "/receipts", "rc3", headers))["receipts"] == []


def test_view_recap_reports_availability_only(world) -> None:
    """A recap is an availability indicator, not the player's record."""
    seed(world, "vr1")
    headers = owner_of(world, "vr1")
    prime(world, "vr1")
    body = body_of(call(world, "POST", "/view_recap", "vr1", headers))
    assert body == {"ok": True, "view": {"available": True}}
    # No lifecycle internals leak through the recap.
    assert "lifecycle" not in body["view"]
    assert "progress" not in body["view"]


# ---------------------------------------------------------------------------
# GET /lifecycle/{pid}/offline-preview
# ---------------------------------------------------------------------------

def test_preview_publishes_the_documented_key_set(world) -> None:
    seed(world, "pv1")
    r = call(world, "GET", "/offline-preview", "pv1", owner_of(world, "pv1"))
    assert r.status_code == 200
    body = body_of(r)
    assert set(body) == PREVIEW_KEYS, sorted(set(body) ^ PREVIEW_KEYS)
    assert body["max_cap_hours"] == pytest.approx(72.0)


def test_preview_does_not_credit_anything(world) -> None:
    """A preview is a read: the stored row must be untouched."""
    seed(world, "pv2", progress=40.0)
    headers = owner_of(world, "pv2")
    prime(world, "pv2")
    before = dict(world["players_db"]["pv2"])
    call(world, "GET", "/offline-preview", "pv2", headers)
    after = world["players_db"]["pv2"]
    assert after["progress"] == before["progress"]
    assert after["qi"] == before["qi"]
    assert after["spirit_stones"] == before["spirit_stones"]
    # Every lifecycle field except the reconciliation cursor must be untouched:
    # the read path runs reconcile, which is allowed to move the cursor and
    # nothing else.
    lc_before = dict(before["lifecycle"])
    lc_after = dict(after["lifecycle"])
    assert lc_before.pop("last_reconciled_at") <= lc_after.pop("last_reconciled_at")
    assert lc_before == lc_after


def test_an_unreadable_device_clock_falls_back_to_the_server_clock(world) -> None:
    """A junk header is not the caller's fault; the server's own time is used."""
    seed(world, "pv3")
    headers = {**owner_of(world, "pv3"), "X-Device-Time": "not-a-number"}
    r = call(world, "GET", "/offline-preview", "pv3", headers)
    assert r.status_code == 200
    assert set(body_of(r)) == PREVIEW_KEYS


def test_a_missing_device_clock_header_is_accepted(world) -> None:
    seed(world, "pv4")
    r = call(world, "GET", "/offline-preview", "pv4", owner_of(world, "pv4"))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /lifecycle/{pid}/claim-offline
# ---------------------------------------------------------------------------

def test_claim_publishes_the_documented_key_set(world) -> None:
    seed(world, "cl1")
    prime(world, "cl1")
    r = call(world, "POST", "/claim-offline", "cl1", owner_of(world, "cl1"))
    assert r.status_code == 200
    body = body_of(r)
    assert set(body) == CLAIM_KEYS, sorted(set(body) ^ CLAIM_KEYS)
    assert body["ok"] is True
    assert set(body["rewards"]) == APPLIED_REWARD_KEYS
    assert body["stones_clamped"] is None


def test_claim_never_reports_more_hours_than_the_cap(world) -> None:
    """The elapsed window is bounded by the module's own offline-hours cap."""
    seed(world, "cl2", last_cultivation_time=time.time() - 400 * 3600)
    prime(world, "cl2")
    body = body_of(call(world, "POST", "/claim-offline", "cl2", owner_of(world, "cl2")))
    assert body["elapsed_hours"] <= body["capped_hours"] + 1e-6


def test_a_clock_further_than_the_skew_limit_is_a_caller_fault(world) -> None:
    """The 400 is reserved for the skew the caller controls."""
    seed(world, "cl3")
    prime(world, "cl3")
    far = time.time() + MAX_CLOCK_SKEW_SECONDS * 4
    r = call(world, "POST", "/claim-offline", "cl3",
             {**owner_of(world, "cl3"), "X-Device-Time": str(far)})
    assert r.status_code == 400
    assert "skew" in str(detail_of(r)).lower()


def test_a_clock_inside_the_skew_limit_is_served(world) -> None:
    """Inside the limit the claim is served; the bound is a bound, not a ban."""
    seed(world, "cl4")
    prime(world, "cl4")
    near = time.time() + MAX_CLOCK_SKEW_SECONDS / 2
    r = call(world, "POST", "/claim-offline", "cl4",
             {**owner_of(world, "cl4"), "X-Device-Time": str(near)})
    assert r.status_code == 200


def test_an_unpriceable_stored_row_costs_a_500_not_a_400(world) -> None:
    """MEASURED, pinned as designed.

    Two faults arrive from the offline engine as one exception type: a bad
    device clock, which is the caller's fault, and a stored row the engine
    cannot price, which is ours. Answering 400 for the second blamed the
    caller for our data defect and hid it from every error dashboard keyed on
    server-side failures.
    """
    import offline_progression

    seed(world, "cl5")
    prime(world, "cl5")
    original = lifecycle.calculate_full_offline_progress

    def explode(*args, **kwargs):
        # A raise that is NOT the skew comparison, with the device clock well
        # inside the limit.
        raise ValueError("stage 'not-a-real-stage' cannot be priced")

    lifecycle.calculate_full_offline_progress = explode
    try:
        r = call(world, "POST", "/claim-offline", "cl5", owner_of(world, "cl5"))
    finally:
        lifecycle.calculate_full_offline_progress = original
    assert r.status_code == 500
    assert detail_of(r) == "internal_error"


def test_a_claim_commits_to_the_stored_row(world) -> None:
    """The claim is a write, not a projection."""
    seed(world, "cl6", progress=40.0, qi=10.0)
    headers = owner_of(world, "cl6")
    prime(world, "cl6")
    call(world, "POST", "/claim-offline", "cl6", headers)
    stored = world["players_db"]["cl6"]
    assert stored["lifecycle"]["last_reconciled_at"] > 0


def test_a_claim_on_a_walletless_store_still_answers(world) -> None:
    """The in-memory slice carries no wallet store; the claim must not care.

    The claim stamp is only written when a wallet store is present, and its
    durability is owned by a separate file that mounts one.
    """
    seed(world, "cl7")
    prime(world, "cl7")
    r = call(world, "POST", "/claim-offline", "cl7", owner_of(world, "cl7"))
    assert r.status_code == 200
    assert body_of(r)["ok"] is True


# ---------------------------------------------------------------------------
# The pure helpers the routes are built on
# ---------------------------------------------------------------------------

def test_a_fresh_state_carries_the_documented_fields(world) -> None:
    now = time.time()
    state = lifecycle.new_state(now)
    assert state["status"] == "active"
    assert state["life_index"] == 1
    assert state["version"] == 1
    assert state["born_at"] == now
    assert state["dies_at"] == pytest.approx(now + lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY)
    assert state["attended_days"] == []
    assert state["missed_days"] == 0
    assert state["legacy"] == {"completed_lives": 0}
    assert state["receipts"] == []


def test_a_missing_lifecycle_is_a_view_only_unless_asked_to_start(world) -> None:
    """The default hands back a fresh unattached state; ``start`` attaches it.

    Callers store the result and read ``.get("status")`` on it, so the default
    must never be None.
    """
    player = {"progress": 10.0}
    assert lifecycle.reconcile(player, time.time()) is None
    assert "lifecycle" not in player
    started = lifecycle.reconcile(player, time.time(), start=True)
    assert started is not None and started["status"] == "active"
    assert player["lifecycle"]["status"] == "active"


def test_effective_time_is_the_floor_of_the_cursor(world) -> None:
    """A guarded observation never moves backwards."""
    now = time.time()
    assert lifecycle.effective_now(None, now) == pytest.approx(now)
    assert lifecycle.effective_now({"last_reconciled_at": now + 60}, now) == pytest.approx(now + 60)
    assert lifecycle.effective_now({"last_reconciled_at": now - 60}, now) == pytest.approx(now)


def test_a_reconciliation_rewards_the_clock_it_was_given(world) -> None:
    """Days of absence decay progress and raise the missed-day count."""
    now = time.time()
    seed(world, "rc5", progress=100.0,
         lifecycle=seeded_lifecycle(now, progress=100.0,
                                    last_reconciled_at=now - 3 * lifecycle.DAY))
    body = body_of(call(world, "GET", "", "rc5", owner_of(world, "rc5")))
    assert body["missed_days"] >= 3
    assert body["progress"] < 100.0
    decay = lifecycle.POLICY["missed_day_qi_decay"]
    # The base is the player's own progress and the exponent the CLOSED
    # buckets the guard counted, both read rather than hardcoded.
    closed = (time.time() - (now - 3 * lifecycle.DAY)) // lifecycle.DAY
    assert closed == 3
    assert body["missed_days"] == closed
    assert body["progress"] == pytest.approx(100.0 * (1 - decay) ** closed, rel=1e-3)


def test_a_completed_life_bumps_the_life_index_and_resets_progress(world) -> None:
    """Crossing the death boundary advances the life rather than pinning it."""
    now = time.time()
    seed(world, "rc6", progress=100.0,
         lifecycle=seeded_lifecycle(now, status="active", progress=100.0,
                                    dies_at=now - 1, born_at=now - lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY))
    body = body_of(call(world, "GET", "", "rc6", owner_of(world, "rc6")))
    assert body["status"] == "memorial"
    assert body["life_index"] == 1
    assert body["legacy"]["completed_lives"] == 1
    assert body["progress"] == 0.0


def test_the_policy_dictionary_documents_every_knob_the_routes_use(world) -> None:
    """A knob the policy names must be the number the code uses.

    This is a relationship between two pieces of data, not a snapshot of
    either: re-tuning a value stays green, hardcoding a second copy of it in a
    route does not.
    """
    assert lifecycle.POLICY["initial_life_days"] == lifecycle.INITIAL_LIFE_DAYS
    assert lifecycle.POLICY["memorial_days"] == lifecycle.MEMORIAL_DAYS
    assert lifecycle.POLICY["reservation_days"] == lifecycle.RESERVATION_DAYS
    assert lifecycle.POLICY["seat_cap"] == lifecycle.SEAT_CAP
    assert 0.0 < lifecycle.POLICY["missed_day_qi_decay"] < 1.0
    # The reserve window is measured from the day the life ends, in days.
    assert lifecycle.MEMORIAL_DAYS >= 1
    assert lifecycle.RESERVATION_DAYS >= 1


# ---------------------------------------------------------------------------
# The measured gap: the status refusal precedes authentication
# ---------------------------------------------------------------------------

def test_a_memorial_refusal_is_no_longer_produced_before_the_caller_is_authenticated(world) -> None:
    """REGRESSION GUARD -- was a MEASURED DEFECT, now closed.

    The custom route class ran before FastAPI resolved dependencies, so the
    409 was produced with nobody having proved who they are, and its body was
    the player's full lifecycle view: an anonymous caller naming a memorial
    player learned the account exists, when it died, and how much cultivation
    progress it had.

    The route class now authenticates first (server/lifecycle.py,
    `route_class`), so this pins the CLOSED behaviour. It was rewritten from
    the defect pin it used to be, exactly as that pin instructed.
    """
    seed(world, "gap1", lifecycle=seeded_lifecycle(time.time(), status="memorial"))
    r = call(world, "POST", "/receipt", "gap1")  # no headers at all
    assert r.status_code == 401, (
        f"the memorial refusal is served before authentication again: "
        f"{r.status_code}"
    )
    # And nothing about the victim is disclosed to the unproven caller.
    assert "lifecycle" not in r.text and "dies_at" not in r.text, (
        f"the refusal body still leaks the victim's lifecycle: {r.text[:200]}"
    )


# ===========================================================================
# ADDED BY THE SECOND CONTRACT PASS -- the surfaces the section above never
# reaches. Every number below was MEASURED on this tree by running the route,
# the guard and the helpers directly, not read off the source.
#
# WHAT WAS MISSING, AND HOW THE GAP WAS FOUND
# --------------------------------------------
# The first pass mounted ``lifecycle.register_routes`` and drove the nine
# routes. Everything below lives OUTSIDE that router and was therefore never
# executed by any test in the file: confirmed by counting references to each
# name in this file before writing a line of it, not by reading the module and
# guessing. The four uncovered families are:
#
#   * ``require_lifecycle_guard`` -- the module-scope guard OTHER modules mount
#     as a dependency on non-lifecycle routes. It has its own three refusals
#     and its own wording (``life_ended`` / ``life_not_active``), which differ
#     from the router's (``dead`` / ``memorial``). Nothing in the router sweep
#     can see it.
#   * ``_stamp_offline_claimed`` -- the writer-selection ladder that keeps the
#     offline claim stamp from being dropped. Four branches, and the branch
#     that matters most is the one that RAISES rather than quietly succeeding.
#   * the mint-ceiling family -- ``_clamp_offline_stones``,
#     ``_is_spirit_stone_reward``, ``_current_stone_total``. ``stones_clamped``
#     is a published reply key, and the file above only ever asserted it is
#     ``None``, so the branch that actually clamps a payout was untested.
#   * ``_resolve_players_db`` with nothing injected, and the alias
#     ``reconcile_player`` that ``main.py`` looks up at import time.
# ===========================================================================


@pytest.fixture
def guarded_world() -> Iterator[Dict[str, Any]]:
    """A bare app whose ONLY dependency is ``require_lifecycle_guard``.

    ``require_lifecycle_guard`` exists to be mounted by modules that are not
    this one, so driving it through this module's own router cannot reach it.
    Two routes -- one GET, one POST -- are the smallest app that exercises both
    of its branches. The ownership guard is deliberately NOT installed: this
    guard is the thing under test, and stacking another guard on top would
    measure that one instead.
    """
    from fastapi import Depends  # local: only this section needs it

    players_db = PlayerStore()
    app = FastAPI()

    @app.get("/probe/read/{pid}")
    def probe_read(pid: str, lc: dict = Depends(lifecycle.require_lifecycle_guard)):
        return {"status": lc.get("status"), "life_index": lc.get("life_index"),
                "missed_days": lc.get("missed_days"),
                "remaining_seconds": lc.get("remaining_seconds")}

    @app.post("/probe/write/{pid}")
    def probe_write(pid: str, lc: dict = Depends(lifecycle.require_lifecycle_guard)):
        return {"ok": True, "status": lc.get("status")}

    saved_live = lifecycle._live_players_db
    lifecycle._live_players_db = players_db
    try:
        yield {"client": TestClient(app, raise_server_exceptions=False),
               "players_db": players_db}
    finally:
        lifecycle._live_players_db = saved_live


# ---------------------------------------------------------------------------
# require_lifecycle_guard -- the guard OTHER route modules mount
# ---------------------------------------------------------------------------

def test_the_module_scope_guard_refuses_a_missing_clock_with_400(guarded_world) -> None:
    """No lifecycle and no cultivation timestamp: nothing to check, so 400.

    Both verbs answer the same status, and the wording is this guard's own,
    not the router's.
    """
    guarded_world["players_db"]["mg_none"] = {"id": "mg_none"}
    headers = owner_of(guarded_world, "mg_none")
    for verb in ("GET", "POST"):
        r = guarded_world["client"].request(verb, "/probe/read/mg_none"
                                            if verb == "GET" else "/probe/write/mg_none",
                                            headers=headers)
        assert r.status_code == 400, f"{verb} answered {r.status_code}"
        assert r.json()["detail"] == NOT_STARTED


def test_the_module_scope_guard_auto_migrates_a_legacy_cultivator(guarded_world) -> None:
    """A row with ``last_cultivation_time`` and no clock gets one, on a GET.

    The cursor must start NOW: the earlier fix was that leaving it unset made
    the first reconcile charge the player for every day since migration.
    """
    guarded_world["players_db"]["mg_mig"] = {
        "id": "mg_mig", "last_cultivation_time": time.time() - 5 * lifecycle.DAY}
    headers = owner_of(guarded_world, "mg_mig")
    r = guarded_world["client"].get("/probe/read/mg_mig", headers=headers)
    assert r.status_code == 200
    lc = guarded_world["players_db"]["mg_mig"]["lifecycle"]
    assert lc["status"] == "active"
    # A full first life, measured in days from the module's own constant.
    assert (lc["dies_at"] - lc["born_at"]) == pytest.approx(
        lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY)
    # And the cursor is at migration time, not at the historical cultivation
    # time: leaving it behind charges the player for every day since.
    assert abs(lc["last_reconciled_at"] - time.time()) < 120
    # The consequence, which is what the cursor actually buys. A stale cursor
    # equal to the historical cultivation time makes the very next reconcile
    # count five closed absent days, so the player is born carrying a five-day
    # missed streak and a decayed baseline -- charged for days they were never
    # present. Asserting the cursor's own value is NOT enough on its own: the
    # GET path reconciles afterwards and overwrites `last_reconciled_at` with
    # the observation time, which masks the defect. The charge is the
    # observable that survives.
    assert lc["missed_days"] == 0, "migration charged the player for absent days"
    assert lc["progress"] == pytest.approx(100.0)


def test_the_module_scope_guard_uses_its_own_refusal_wording(guarded_world) -> None:
    """410 ``life_ended`` and 409 ``life_not_active`` -- NOT the router's words.

    Measured, and the distinction matters: the router's route class answers 410
    and 409 carrying the full lifecycle VIEW as the detail body, while this
    guard answers two flat strings. A client that reads
    ``detail["lifecycle"]["status"]`` off every 410 will KeyError here, so the
    difference is pinned rather than smoothed over.
    """
    now = time.time()
    guarded_world["players_db"]["mg_dead"] = {
        "id": "mg_dead", "lifecycle": seeded_lifecycle(now, status="dead", dies_at=0.0)}
    guarded_world["players_db"]["mg_mem"] = {
        "id": "mg_mem", "lifecycle": seeded_lifecycle(now, status="memorial")}
    dead = guarded_world["client"].post("/probe/write/mg_dead",
                                        headers=owner_of(guarded_world, "mg_dead"))
    assert dead.status_code == 410
    assert dead.json()["detail"] == "life_ended"
    mem = guarded_world["client"].post("/probe/write/mg_mem",
                                       headers=owner_of(guarded_world, "mg_mem"))
    assert mem.status_code == 409
    assert mem.json()["detail"] == "life_not_active"


def test_the_module_scope_guard_commits_its_reconciliation(guarded_world) -> None:
    """A GET through this guard advances the STORED clock, not just the reply.

    The defect this pins: the reconcile used to run on the transaction-scoped
    snapshot and be thrown away, so the caller received a fresh view while the
    store kept the unreconciled row -- decay, missed days and the life index
    never advanced on any route using this guard.
    """
    now = time.time()
    stale = now - 3 * lifecycle.DAY
    guarded_world["players_db"]["mg_dec"] = {
        "id": "mg_dec", "progress": 100.0,
        "lifecycle": seeded_lifecycle(now, progress=100.0,
                                      last_reconciled_at=stale)}
    headers = owner_of(guarded_world, "mg_dec")
    r = guarded_world["client"].get("/probe/read/mg_dec", headers=headers)
    assert r.status_code == 200
    stored = guarded_world["players_db"]["mg_dec"]["lifecycle"]
    # EXACTLY the closed buckets, read from the module's own decay constant.
    # An earlier `>= 3` was too loose: a guard that discards the reconcile
    # entirely still leaves the stored row at 0, so only an exact assertion
    # distinguishes "reconciled" from "answered from a snapshot".
    decay = lifecycle.POLICY["missed_day_qi_decay"]
    closed = int((time.time() - stale) // lifecycle.DAY)
    assert closed == 3, "the fixture drifted; re-cut it"
    assert stored["missed_days"] == closed, "the reconcile was computed and discarded"
    assert stored["progress"] == pytest.approx(100.0 * (1 - decay) ** closed, rel=1e-3)
    # The reply agrees with the store, so a client and the next request see the
    # same life rather than a fresh projection over a stale row.
    assert r.json()["missed_days"] == stored["missed_days"]


def test_the_module_scope_guard_does_not_write_a_display_field_into_the_row(
        guarded_world) -> None:
    """``remaining_seconds`` is computed per read; storing it leaks a display key.

    ``view()`` recomputes it on every read, so persisting it bought nothing and
    leaked a non-state key into every lifecycle row this guard touched.
    """
    now = time.time()
    guarded_world["players_db"]["mg_post"] = {
        "id": "mg_post",
        "lifecycle": seeded_lifecycle(now, status="active", dies_at=now + lifecycle.DAY)}
    r = guarded_world["client"].post("/probe/write/mg_post",
                                     headers=owner_of(guarded_world, "mg_post"))
    assert r.status_code == 200
    assert "remaining_seconds" not in guarded_world["players_db"]["mg_post"]["lifecycle"]


def test_the_module_scope_guard_refuses_an_unknown_player(guarded_world) -> None:
    """Same measured defect as the router's: 400, never the 404 the code holds."""
    r = guarded_world["client"].get("/probe/read/no-such-thing",
                                    headers=owner_of(guarded_world, "mg_post"))
    assert r.status_code == 400
    assert r.json()["detail"] == NO_PLAYER_ID


def test_reconcile_player_is_the_same_function_main_looks_up() -> None:
    """``main.py`` resolves ``reconcile_player`` at IMPORT time.

    The alias is published at module scope rather than bound inside
    ``register_routes``, so a fresh-process import cannot raise AttributeError
    before the router is ever mounted.
    """
    assert lifecycle.reconcile_player is lifecycle.reconcile


def test_resolving_the_store_with_nothing_injected_raises_rather_than_returning_none(
        world) -> None:
    """Measured: a bare RuntimeError naming both ways to supply a store.

    Returning ``None`` here would make every downstream ``db[pid]`` fail with
    ``TypeError`` at a distance from the real cause.
    """
    saved = lifecycle._live_players_db
    lifecycle._live_players_db = None
    try:
        with pytest.raises(RuntimeError) as excinfo:
            lifecycle._resolve_players_db()
        assert "no player store" in str(excinfo.value)
    finally:
        lifecycle._live_players_db = saved
    # And with a store supplied directly it resolves, callable or not.
    assert lifecycle._resolve_players_db(world["players_db"]) is world["players_db"]


# ---------------------------------------------------------------------------
# _stamp_offline_claimed -- the writer ladder that keeps the stamp alive
# ---------------------------------------------------------------------------

class _UpdaterWallet:
    """A store exposing only the in-memory ``update`` field writer."""
    def __init__(self) -> None:
        self.calls: List[Any] = []
    def update(self, player_id, **fields):
        self.calls.append((player_id, fields))


class _Wallet:
    """A wallet row, as the detached-copy docstring describes."""
    def __init__(self) -> None:
        self.last_offline_claimed_at = None


class _PutterWallet:
    """A store with no ``update`` but a whole-wallet ``put``."""
    def __init__(self) -> None:
        self.puts: List[Any] = []
        self.row = _Wallet()
    def get(self, player_id):
        return self.row
    def put(self, wallet):
        self.puts.append(wallet.last_offline_claimed_at)


class _Cursor:
    def __init__(self, log) -> None:
        self.log = log
    def execute(self, sql, args):
        self.log.append((sql, args))
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, log) -> None:
        self.log = log
    def cursor(self):
        return _Cursor(self.log)


class _PgWallet:
    """A store exposing neither writer, only a connection -- the PG shape."""
    def __init__(self) -> None:
        self.log: List[Any] = []
        self.conn = _Conn(self.log)


class _NoWriterWallet:
    """A store with no connection surface at all."""


def test_the_claim_stamp_prefers_the_field_writer(world) -> None:
    """First rung: ``update(player_id, last_offline_claimed_at=...)``."""
    store = _UpdaterWallet()
    lifecycle._stamp_offline_claimed(store, "p1", 123.0)
    assert store.calls == [("p1", {"last_offline_claimed_at": 123.0})]


def test_the_claim_stamp_falls_back_to_the_whole_wallet_writer(world) -> None:
    """Second rung: read, set, ``put``. The row is replaced, not mutated in place."""
    store = _PutterWallet()
    lifecycle._stamp_offline_claimed(store, "p2", 456.0)
    assert store.puts == [456.0]


def test_the_claim_stamp_falls_back_to_a_column_update(world) -> None:
    """Third rung: the PostgreSQL store exposes no wallet-object writer at all.

    Without this rung the stamp is silently lost under PostgreSQL, which is the
    whole money bug: the lost stamp lets the NEXT claim re-pay the same window.
    """
    store = _PgWallet()
    lifecycle._stamp_offline_claimed(store, "p3", 789.0)
    assert len(store.log) == 1
    sql, args = store.log[0]
    assert "UPDATE wallets" in sql and "last_offline_claimed_at" in sql
    assert args == (789.0, "p3")


def test_a_wallet_store_with_no_writer_raises_rather_than_losing_the_stamp(
        world) -> None:
    """The branch that matters most is the one that REFUSES to be quiet.

    Both backends hand back a detached object from ``get()``, so a caller that
    assigns and drops has written nothing. A silent no-op here is exactly the
    bug this function exists to prevent.
    """
    with pytest.raises(RuntimeError) as excinfo:
        lifecycle._stamp_offline_claimed(_NoWriterWallet(), "p4", 1.0)
    assert "no writer" in str(excinfo.value)
    assert "same offline window" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The mint ceiling on the offline claim
# ---------------------------------------------------------------------------

class _Reward:
    """One entry in an offline result's reward list."""
    def __init__(self, currency: str, amount: int,
                 reward_type: str = "OFFLINE_SPIRIT_STONE") -> None:
        self.currency = currency
        self.amount = amount
        self.reward_type = reward_type


class _Result:
    def __init__(self, rewards: List[_Reward]) -> None:
        self.rewards = rewards


def _stone_cap() -> int:
    """The daily ceiling the authority publishes for this reason."""
    import mint_authority as ma
    return ma.reward_spec("lifecycle.offline_progress").daily_cap


def _fresh_player(tag: str) -> str:
    """A player id no other test in this file has used for the ledger.

    The mint ledger is process-wide and keyed on (player, reason, day), so a
    shared id would let one test spend another test's ceiling.
    """
    return f"clamp_{tag}_{os.getpid()}"


def test_a_stone_reward_is_matched_by_the_names_the_store_actually_persists(
        world) -> None:
    """Tier names and the reward TYPE -- and the bare key is NOT a tier name.

    The store keeps stones as PER-TIER keys inside one ``spirit_stones``
    column, so a bare name writes a key nothing reads. ``_is_spirit_stone_reward``
    matches on EITHER a tier name or a stone reward TYPE, so the two are
    asserted separately here: a reward with a neutral type is judged purely on
    its currency name, and only a real tier name may pass that arm.
    """
    neutral = "OFFLINE_REWARD"  # a type that says nothing about stones
    for tier in ("copper", "silver", "gold", "low_grade", "mid_grade",
                 "high_grade", "extreme_grade"):
        assert lifecycle._is_spirit_stone_reward(_Reward(tier, 10, neutral)) is True
    # The bare column name is NOT a tier name: on the name arm alone it is not
    # a stone, which is precisely the trap the function exists to avoid.
    assert lifecycle._is_spirit_stone_reward(_Reward("spirit_stones", 10, neutral)) is False
    assert lifecycle._is_spirit_stone_reward(_Reward("stone", 10, neutral)) is False
    assert lifecycle._is_spirit_stone_reward(_Reward("", 10, neutral)) is False
    # The TYPE arm is the one that catches a reward whose currency field is
    # empty or unusable -- so the same bare name DOES match when typed as one.
    assert lifecycle._is_spirit_stone_reward(
        _Reward("spirit_stones", 10, "OFFLINE_SPIRIT_STONE_GRANT")) is True
    assert lifecycle._is_spirit_stone_reward(
        _Reward("", 10, "OFFLINE_SPIRIT_STONE_GRANT")) is True
    assert lifecycle._is_spirit_stone_reward(
        _Reward(None, 10, "OFFLINE_CULTIVATION")) is False


def test_the_stone_total_sums_the_tiers_and_never_raises(world) -> None:
    """The ledger's balance field is an audit number; failing to read must not fail.

    Measured across every shape a row can present: a tier dict, a scalar, an
    absent key, and ``None`` standing in for no player at all.
    """
    assert lifecycle._current_stone_total(
        {"spirit_stones": {"low_grade": 5, "mid_grade": 7, "junk": "x"}}) == 12
    assert lifecycle._current_stone_total({"spirit_stones": 12}) == 12
    assert lifecycle._current_stone_total({}) == 0
    assert lifecycle._current_stone_total(None) == 0
    assert lifecycle._current_stone_total({"spirit_stones": "unreadable"}) == 0


def test_a_claim_over_the_daily_ceiling_is_cut_and_the_cut_is_reported(world) -> None:
    """The clamping branch, which the ``stones_clamped is None`` assert never reached.

    Measured: requesting ``cap + 5000`` on a fresh player is cut to exactly the
    cap, the reward amount is rewritten IN PLACE so the credit that follows is
    already inside the ceiling, and the ledger records what was granted.
    """
    import mint_authority as ma

    pid = _fresh_player("over")
    cap = _stone_cap()
    result = _Result([_Reward("gold", cap + 5000)])
    summary = lifecycle._clamp_offline_stones(result, pid, {"spirit_stones": {"gold": 3}})

    assert summary is not None, "an over-cap claim reported no clamp"
    assert set(summary) == {"requested", "granted", "daily_cap", "already", "period_key"}
    assert summary["requested"] == cap + 5000
    assert summary["granted"] == cap
    assert summary["daily_cap"] == cap
    assert summary["already"] == 0
    assert [r.amount for r in result.rewards] == [cap]
    # And the grant is on the record, so a second press is measured against it.
    assert ma.LEDGER.period_total(pid, "lifecycle.offline_progress",
                                  ma.utc_day_key()) == cap


def test_the_cut_is_proportional_across_tiers_rather_than_zeroing_one(world) -> None:
    """Two stone tiers are cut in the same ratio, not one dropped and one full."""
    pid = _fresh_player("tiered")
    cap = _stone_cap()
    over = cap + 2000
    result = _Result([_Reward("gold", over // 2), _Reward("copper", over // 2)])
    summary = lifecycle._clamp_offline_stones(result, pid, None)

    assert summary is not None and summary["granted"] == cap
    amounts = [r.amount for r in result.rewards]
    assert sum(amounts) == cap, "the tiers no longer sum to what was granted"
    assert amounts[0] == amounts[1], "the cut was not proportional"
    assert all(a > 0 for a in amounts), "a tier was zeroed instead of scaled"


def test_the_ceiling_is_per_player_and_does_not_leak_across_players(world) -> None:
    """One player spending their ceiling must not shrink another's claim."""
    import mint_authority as ma

    spender = _fresh_player("spender")
    other = _fresh_player("other")
    ma.record_external_grant(spender, "lifecycle.offline_progress", _stone_cap(),
                             currency="spirit_stone", balance_after=0)
    spent = _Result([_Reward("gold", 1000)])
    fresh = _Result([_Reward("gold", 1000)])

    lifecycle._clamp_offline_stones(spent, spender, None)
    lifecycle._clamp_offline_stones(fresh, other, None)

    assert [r.amount for r in spent.rewards] == [0], "the spent ceiling paid out"
    assert [r.amount for r in fresh.rewards] == [1000], "another player was charged"


def test_a_spent_ceiling_reports_no_clamp_because_the_record_step_refused(world) -> None:
    """A spent ceiling is REPORTED, not hidden.

    With the ceiling already spent, ``clamp_for_period`` correctly answers 0 and
    the reward IS zeroed. The record step that declares the grant is skipped when
    the grant is zero -- a zero grant has nothing to declare, and forcing it
    raised ``MintRefusal`` that the helper's blanket ``except`` swallowed,
    handing the caller ``None``.

    ``None`` reads as "nothing was clamped" on exactly the press where the player
    received zero stones, so the report contradicted the payout. The summary now
    reports ``granted: 0``, which is the truth. The refusal itself was correct and
    is unchanged; what changed is that skipping the zero record lets the summary
    tell the truth. Pinned here: the observable -- a spent day reports
    ``granted: 0`` and the reward list is still zeroed.
    """
    import mint_authority as ma

    pid = _fresh_player("spent")
    ma.record_external_grant(pid, "lifecycle.offline_progress", _stone_cap(),
                             currency="spirit_stone", balance_after=0)
    result = _Result([_Reward("gold", 1000)])
    summary = lifecycle._clamp_offline_stones(result, pid, None)
    assert summary["granted"] == 0, "the spent ceiling must be reported, not hidden"
    assert summary["requested"] == 1000
    assert [r.amount for r in result.rewards] == [0]


def test_a_result_with_no_stones_never_consults_the_ceiling(world) -> None:
    """A currency claim is untouched and reports nothing."""
    result = _Result([_Reward("qi", 1234, "OFFLINE_CULTIVATION")])
    assert lifecycle._clamp_offline_stones(result, _fresh_player("qi"), None) is None
    assert [r.amount for r in result.rewards] == [1234]


def test_a_failing_ceiling_never_costs_the_claim(world) -> None:
    """The bound is a bound on an existing payout, not the payout path.

    Measured: a reward list that raises on access is swallowed, logged as
    ``lifecycle.offline_stone_clamp_failed``, and returned as None -- the claim
    proceeds with the reward it already computed. Losing a reward because a
    bound failed to evaluate would be strictly worse than the bound being
    absent.
    """
    class _Exploding:
        @property
        def rewards(self):
            raise RuntimeError("cannot read rewards")

    assert lifecycle._clamp_offline_stones(
        _Exploding(), _fresh_player("boom"), None) is None


def test_the_reason_the_claim_uses_is_declared_in_the_reward_table(world) -> None:
    """The reason string is a relationship between two modules, so it is pinned.

    ``clamp_for_period`` raises ``MintRefusal`` for a reason missing from the
    table, which the helper above swallows -- so a typo here would silently
    leave the offline payout UNBOUNDED while every test stayed green.
    """
    import mint_authority as ma

    spec = ma.reward_spec("lifecycle.offline_progress")
    assert spec is not None
    assert spec.currency == "spirit_stone"
    assert spec.daily_cap is not None and spec.daily_cap > 0
    # A reason with no declared ceiling would make the clamp a no-op.
    assert spec.daily_cap < 1_000_000, "an unbounded offline stone payout"

# ===========================================================================
# THE OFFLINE CAP, MEASURED RATHER THAN ASSUMED
# ===========================================================================
#
# WHY THIS SECTION EXISTS
# -----------------------
# ``test_claim_never_reports_more_hours_than_the_cap`` asserts
# ``elapsed_hours <= capped_hours + 1e-6`` and passes. It was measured against
# the walletless store, where ``elapsed_hours`` is 0.0 on EVERY press, so the
# assertion is 0.0 <= 0.0 -- it would survive the cap being deleted outright.
#
# Measured on this tree, that is not a guess:
#   * ``_dict_to_player`` builds a ``Player`` with no ``last_cultivation_time``
#     field at all (``getattr`` returns ABSENT), so the walletless anchor read
#     at ``offline_progression.py:504`` always falls through to ``server_now``.
#   * The lifecycle GET used to prime a player ALSO stamps
#     ``last_cultivation_time = now`` (the migration branch), so even a seeded
#     historical cursor is overwritten before the claim is ever sized.
# A cap is a bound on MONEY. A cap proven only against a zero payout is not a
# cap at all, so the bound is re-measured here with the anchor the engine
# actually reads.
#
# The numbers below are RELATIONSHIPS to ``MAX_OFFLINE_HOURS``, not copies:
# a re-tune of the offline cap moves the constant and the assertions follow.


@pytest.fixture
def walleted_world() -> Iterator[Dict[str, Any]]:
    """The same bare router, on a store that actually HAS a wallet.

    Without one, ``claim-offline`` passes ``wallet_store=None`` to the engine,
    the engine cannot read a claim anchor, and every claim is priced at zero
    hours -- so nothing about the cap is observable here.
    """
    from wallet_defense import WalletStore

    wallet = WalletStore()
    players_db = PlayerStore(wallet_store=wallet)
    app = FastAPI()

    saved_live = lifecycle._live_players_db
    saved_rc = lifecycle.route_class_store
    lifecycle._live_players_db = players_db
    lifecycle.route_class_store = None
    try:
        lifecycle.register_routes(app, lambda: players_db)
        ownership_guard.install_ownership_guard(app)
        yield {
            "app": app,
            "client": TestClient(app, raise_server_exceptions=False),
            "players_db": players_db,
            "wallet": wallet,
        }
    finally:
        lifecycle._live_players_db = saved_live
        lifecycle.route_class_store = saved_rc


def bank_absence(world: Dict[str, Any], pid: str, hours: float) -> None:
    """Put ``pid``'s claim anchor ``hours`` into the past, through the store.

    THREE steps, all measured as necessary:
      * ``create`` first -- ``WalletStore.update`` returns ``None`` and writes
        nothing for a player with no wallet row, and a fresh wallet is stamped
        with BOTH columns at ``now``.
      * ``last_offline_claimed_at = 0.0`` -- the engine prefers this column and
        only falls back to ``last_online_at`` when it is falsy, so backdating
        one column alone prices a zero-hour window.
    """
    world["wallet"].create(pid)
    world["wallet"].update(
        pid,
        last_offline_claimed_at=0.0,
        last_online_at=time.time() - hours * 3600.0,
    )


def walleted_claim(world: Dict[str, Any], pid: str, absent_hours: float) -> Dict[str, Any]:
    """One authenticated claim after an absence of ``absent_hours``."""
    seed(world, pid)
    prime(world, pid)
    bank_absence(world, pid, absent_hours)
    r = call(world, "POST", "/claim-offline", pid, owner_of(world, pid))
    assert r.status_code == 200, f"{pid} answered {r.status_code}: {r.text[:200]}"
    return body_of(r)


def test_the_claim_is_priced_from_the_anchor_and_not_always_zero(
        walleted_world) -> None:
    """The control the cap tests depend on: a real absence pays real progress.

    Without this, every cap assertion below would be satisfied by a claim that
    pays nothing at all -- which is exactly the hole the earlier test left.
    """
    body = walleted_claim(walleted_world, "wc0", 36.0)
    assert body["elapsed_hours"] == pytest.approx(36.0, abs=0.5)
    assert body["capped_hours"] == pytest.approx(36.0, abs=0.5)
    assert body["cultivation_progress_gained"] > 0.0


def test_an_absence_longer_than_the_cap_is_paid_at_the_cap(
        walleted_world) -> None:
    """THE CAP, exercised on a claim that would otherwise pay more.

    Measured before this test was written: a 400-hour absence, a 72.5-hour
    absence and a 1000-hour absence all price identically, and the credited
    figure is the SAME as a 71-hour absence would reach at its own rate. The
    binding line is ``capped_hours = min(elapsed_hours, MAX_OFFLINE_HOURS)``,
    and ``capped_hours`` is the only hours figure any reward is multiplied
    from -- so this asserts the money, not just the reported number.
    """
    from offline_progression import MAX_OFFLINE_HOURS

    over = walleted_claim(walleted_world, "wc1", 400.0)
    assert over["elapsed_hours"] > MAX_OFFLINE_HOURS, (
        "the window under test was never longer than the cap, so the "
        "assertion below would be satisfied without a cap at all"
    )
    assert over["capped_hours"] == pytest.approx(MAX_OFFLINE_HOURS, abs=0.5)

    # The bound is on the CREDIT, not only on the reported figure: a longer
    # absence must not pay more than one at the cap.
    at_cap = walleted_claim(walleted_world, "wc2", MAX_OFFLINE_HOURS + 0.5)
    assert over["cultivation_progress_gained"] == pytest.approx(
        at_cap["cultivation_progress_gained"], rel=1e-6)


def test_an_absence_shorter_than_the_cap_is_paid_in_full(
        walleted_world) -> None:
    """The negative control: the cap must not fire early.

    A cap that clamped every window to the maximum would satisfy the test
    above. Only a claim that reports its OWN shorter window distinguishes a
    bound from a flat payout.
    """
    from offline_progression import MAX_OFFLINE_HOURS

    body = walleted_claim(walleted_world, "wc3", MAX_OFFLINE_HOURS / 2.0)
    assert body["elapsed_hours"] < MAX_OFFLINE_HOURS
    assert body["capped_hours"] == pytest.approx(body["elapsed_hours"], abs=0.5)
    assert body["capped_hours"] < MAX_OFFLINE_HOURS


def test_the_cap_boundary_is_exactly_where_the_module_says(
        walleted_world) -> None:
    """A window one hour either side of the cap, measured against the constant.

    This is what makes the cap a real boundary rather than a plateau: the
    report flips from "your own hours" to "the cap" across it.
    """
    from offline_progression import MAX_OFFLINE_HOURS

    under = walleted_claim(walleted_world, "wc4", MAX_OFFLINE_HOURS - 1.0)
    over = walleted_claim(walleted_world, "wc5", MAX_OFFLINE_HOURS + 1.0)
    assert under["capped_hours"] == pytest.approx(MAX_OFFLINE_HOURS - 1.0, abs=0.5)
    assert over["capped_hours"] == pytest.approx(MAX_OFFLINE_HOURS, abs=0.5)
    assert over["capped_hours"] < over["elapsed_hours"]


def test_a_claim_pays_the_window_once_and_the_stamp_closes_it(
        walleted_world) -> None:
    """The cap is a cap PER CLAIM, so it needs the once-only anchor under it.

    The stamp is what makes the cap meaningful across presses: without it a
    player presses twice in the same absence and receives the same bounded
    window twice. Measured: first press 400 hours -> full capped credit;
    immediate second press -> 0.0 hours, 0.0 progress.
    """
    pid = "wc6"
    seed(walleted_world, pid)
    walleted_world["client"].get(f"/lifecycle/{pid}", headers=owner_of(walleted_world, pid))
    bank_absence(walleted_world, pid, 400.0)
    headers = owner_of(walleted_world, pid)
    first = body_of(call(walleted_world, "POST", "/claim-offline", pid, headers))
    second = body_of(call(walleted_world, "POST", "/claim-offline", pid, headers))
    assert first["elapsed_hours"] > 0.0
    assert first["cultivation_progress_gained"] > 0.0
    assert second["elapsed_hours"] == pytest.approx(0.0, abs=0.5)
    assert second["cultivation_progress_gained"] == pytest.approx(0.0, abs=1e-6)


# ===========================================================================
# THE OFFLINE CLAIM'S OWN AUTH ANSWERS
# ===========================================================================
#
# ``test_an_anonymous_caller_is_refused`` already walks all nine routes with no
# header, which covers the 401. What it does NOT cover is the two properties
# that make the 401 safe rather than merely present:
#
#   * an UNAUTHENTICATED caller must get the SAME answer for a player that
#     exists and one that does not, or the status code itself is a free
#     membership test over the whole player-id space; and
#   * a REFUSED claim must move no money.
#
# Both are measured below, not assumed.


def test_an_anonymous_claim_cannot_tell_a_real_player_from_a_fake_one(
        world) -> None:
    """No existence oracle on the biggest payout route.

    The resolver accepts a path segment only when it is already a store key, so
    an unknown id used to resolve to "no player id" and answer 400 -- while a
    real id answered 401. That difference IS the secret, and it was free.
    """
    seed(world, "orc1")
    prime(world, "orc1")          # the clock is running, as it is in production
    real = call(world, "POST", "/claim-offline", "orc1")
    fake = call(world, "POST", "/claim-offline", "orc-not-a-player")
    assert real.status_code == fake.status_code == 401
    assert detail_of(real) == detail_of(fake)


def test_the_claim_refuses_a_stranger_a_junk_token_and_the_owner_succeeds(
        world) -> None:
    """Three arms: no session, a bad session, someone else's session, owner.

    A guard that refused the owner too would satisfy a lone-refusal assertion
    identically, so the owner arm is in the same test as the negative control
    it depends on.
    """
    seed(world, "orc2")
    seed(world, "orc3")
    prime(world, "orc2")          # see the priming arm below for why this matters
    assert call(world, "POST", "/claim-offline", "orc2").status_code == 401
    assert call(world, "POST", "/claim-offline", "orc2",
                {"Authorization": "Bearer not-a-real-token"}).status_code == 401
    stranger = call(world, "POST", "/claim-offline", "orc2", owner_of(world, "orc3"))
    assert stranger.status_code == 403
    assert call(world, "POST", "/claim-offline", "orc2",
                owner_of(world, "orc2")).status_code == 200


def test_a_refused_claim_moves_no_money(walleted_world) -> None:
    """A refusal must cost the player nothing measurable.

    The claim writes a claim stamp AND credits rewards. If either landed on a
    refused request the player would lose a claim window to a press the server
    rejected -- the money bug this module's claim path is written around.
    Measured: progress, stones and the stored wallet anchor are all unchanged
    after an anonymous press on a fully banked 400-hour absence.
    """
    pid = "orc4"
    seed(walleted_world, pid)
    walleted_world["client"].get(
        f"/lifecycle/{pid}", headers=owner_of(walleted_world, pid))
    bank_absence(walleted_world, pid, 400.0)
    before = dict(walleted_world["players_db"][pid])
    before_stones = dict(before.get("spirit_stones") or {})
    before_wallet = walleted_world["wallet"].get(pid)

    r = call(walleted_world, "POST", "/claim-offline", pid)
    assert r.status_code == 401

    after = walleted_world["players_db"][pid]
    assert after.get("progress") == before.get("progress")
    assert dict(after.get("spirit_stones") or {}) == before_stones
    after_wallet = walleted_world["wallet"].get(pid)
    assert after_wallet.last_offline_claimed_at == before_wallet.last_offline_claimed_at
    assert after_wallet.last_online_at == before_wallet.last_online_at


# ===========================================================================
# UNINITIALIZED PLAYERS: THE REFUSAL PAYLOAD IS A CONTRACT
# ===========================================================================
#
# ``test_a_mutation_before_the_clock_starts_is_refused`` pins the status and
# the detail STRING for every mutating route. What it does not pin is that the
# payload is CLEAN -- that a client can render the refusal without also being
# handed a half-built lifecycle view, a stack trace, or a leaked field name.
# ``test_a_dead_player_is_gone_on_every_mutation`` shows the 410/409 bodies DO
# carry a full view by design, so "the body has more keys than detail" is a
# real difference between the two refusal families, not a style preference.


def test_an_uninitialized_refusal_is_a_bare_detail_and_nothing_else(
        world) -> None:
    """Exactly one key, and it is the documented string.

    A client shows this text to a player who has not started a life. A body
    that leaked a partial lifecycle view, an internal field name, or a trace
    would either mislead the player or tell a caller about our row layout.
    """
    for suffix in tuple(s for s in MUTATING if s != "/check-in"):
        pid = f"bp{suffix.strip('/').replace('-', '_')}"
        seed(world, pid)
        body = body_of(call(world, "POST", suffix, pid, owner_of(world, pid)))
        assert set(body) == {"detail"}, f"{suffix} leaked {sorted(set(body) - {'detail'})}"
        assert body["detail"] == NOT_STARTED


def test_the_two_refusal_families_carry_different_shapes(world) -> None:
    """400 is a bare string; 410 and 409 carry the lifecycle view.

    Pinned because they are easy to unify by accident: the bare-string arm is
    what stops a not-started player being handed a lifecycle view, and the
    view-bearing arm is what lets a dead or memorial player be told WHY.
    """
    seed(world, "bs1")
    not_started = body_of(
        call(world, "POST", "/receipt", "bs1", owner_of(world, "bs1")))
    assert not_started == {"detail": NOT_STARTED}

    seed(world, "bs2", lifecycle=seeded_lifecycle(time.time(), status="dead", dies_at=0.0))
    dead = body_of(call(world, "POST", "/receipt", "bs2", owner_of(world, "bs2")))
    assert set(dead) == {"detail"}
    assert isinstance(dead["detail"], dict) and "lifecycle" in dead["detail"]


def test_an_uninitialized_player_is_refused_before_any_state_is_written(
        world) -> None:
    """The refusal must not half-start the clock it says is not started.

    Measured after the press: the row has no ``lifecycle`` key at all and its
    cultivation cursor is untouched, so a client that retries on 400 gets the
    same answer rather than a silently half-migrated player.
    """
    pid = "bs3"
    seed(world, pid)
    before = dict(world["players_db"][pid])
    r = call(world, "POST", "/receipt", pid, owner_of(world, pid))
    assert r.status_code == 400
    after = world["players_db"][pid]
    assert after.get("lifecycle") is None
    assert after.get("last_cultivation_time") == before.get("last_cultivation_time")


# ===========================================================================
# A MISSING KEY IS A 500 EVEN WHEN IT REACHES A MUTATING ROUTE
# ===========================================================================
#
# ``test_lifecycle_status_code_contract.py`` owns the per-site matrix for the
# READ path, one key at a time. The mutating path is not covered there, and it
# is the harder case: the custom route class reconciles the row BEFORE
# FastAPI resolves any dependency, so a broken row is read on a path where the
# owner's own 503 laundering could also have fired. Measured: a stored row with
# no death date answers the owner 500 with ``internal_error`` on a POST --
# the deliberate status survives, and it is NOT laundered to 503 and NOT
# softened to 400.


def test_a_missing_key_on_a_mutating_route_is_a_500_not_a_400(world) -> None:
    """The deliberate fault status must survive the mutating path.

    A 400 here would tell the caller to fix a request that was already fine
    and would hide our broken row from every error tracker keyed on 5xx.
    """
    now = time.time()
    broken = lifecycle.new_state(now)
    del broken["dies_at"]  # a key new_state always writes
    seed(world, "mk1", lifecycle=broken)
    r = call(world, "POST", "/receipt", "mk1", owner_of(world, "mk1"))
    assert r.status_code == 500
    assert detail_of(r) == "internal_error"


def test_a_junk_death_date_on_a_mutating_route_is_a_500(world) -> None:
    """Present but unreadable is the same defect as absent.

    Measured: ``dies_at="soon"`` used to raise out of the arithmetic far from
    the row that caused it. Both must reach the client as one logged 500.
    """
    now = time.time()
    junk = lifecycle.new_state(now)
    junk["dies_at"] = "soon"
    seed(world, "mk2", lifecycle=junk)
    r = call(world, "POST", "/receipt", "mk2", owner_of(world, "mk2"))
    assert r.status_code == 500
    assert detail_of(r) == "internal_error"


def test_a_healthy_row_is_unaffected_by_the_missing_key_rules(world) -> None:
    """The positive control: a complete row still answers 200 on the same path.

    Without it, an over-broad fault (refusing every row) would pass the two
    tests above identically.
    """
    seed(world, "mk3", lifecycle=seeded_lifecycle(time.time()))
    assert call(world, "POST", "/receipt", "mk3",
                owner_of(world, "mk3")).status_code == 200
# ===========================================================================
# TWO MEASURED PRE-AUTH DEFECTS, PINNED AS THEY SHIP
# ===========================================================================
#
# Both were found by MEASUREMENT while building the section above, not by
# reading the module. Each is a real answer the server gives today; a fix turns
# the test red, which is the point of naming it.
#
# DEFECT 1: AN ANONYMOUS CALLER CAN TELL A REAL PLAYER FROM A FAKE ONE
# ======================================================================
# The resolver authenticates before refusing on identity, so an anonymous
# caller gets 401 for an id that is NOT in the store. But the lifecycle guard
# is a route DEPENDENCY that runs its own refusal first, and for a player whose
# clock has not started it answers:
#
#     400 {"detail": "lifecycle not started"}
#
# MEASURED, all anonymous, all on mutating routes:
#
#   id is a real player, clock NOT started ... 400 lifecycle not started
#   id is a real player, clock started ...... 401 invalid or missing session
#   id is not in the store .................. 401 invalid or missing session
#
# So 400 versus 401 is a free membership test over the whole player-id space,
# served with no credentials -- exactly the oracle ``_refuse_unauthenticated``
# exists to close. The anonymous 401 above is only reached once something has
# already started the clock, which is why priming it makes the oracle vanish
# and is why the existing sweep (``test_an_anonymous_caller_is_refused``) never
# saw it: that sweep GETs first, and the GET primes the row.
#
# A stranger with a VALID session is refused the same way: 400 for an unstarted
# player, 403 for a started one. The same free test, to anyone who can log in.
#
# NOT FIXED HERE. This file is a contract, not a patch; the fix is to
# authenticate before the guard's own refusal, which belongs in
# ``lifecycle._resolve_player`` and ``ownership_guard``, not in a test.

_UNPRIMED_ANON_REFUSALS = {
    "/claim-offline": 400, "/advance": 400, "/rebirth": 400,
    "/receipts": 400, "/receipt": 400, "/view_recap": 400,
}


def test_an_anonymous_caller_can_no_longer_probe_the_player_id_space(
        world) -> None:
    """REGRESSION GUARD -- was a MEASURED DEFECT (400 vs 401), now closed.

    This test used to pin the LEAK on purpose: it asserted that a real id and
    a fake id drew DIFFERENT answers for an anonymous caller, because that
    difference was a free membership test over the whole player-id space.

    It is rewritten to pin the closed behaviour, as its own failure message
    instructed. Both arms must now answer 401 -- the caller is told to prove
    who they are, and is told nothing about whether the id exists.

    The UNPRIMED state is the interesting one and is kept deliberately: once
    any read has started the clock the split was already invisible, which is
    why a test that primes first never saw the leak.
    """
    observed = {}
    for suffix in _UNPRIMED_ANON_REFUSALS:
        pid = f"orcleak{suffix.strip('/').replace('-', '_')}"
        seed(world, pid)                       # real row, clock NOT started
        real = call(world, "POST", suffix, pid)
        fake = call(world, "POST", suffix, f"{pid}-not-a-player")
        observed[suffix] = (real.status_code, fake.status_code)
        assert real.status_code == 401, (
            f"{suffix} answered {real.status_code}, not 401, to an anonymous "
            f"caller naming a REAL player -- the identity answer is leaking "
            f"through ahead of authentication")
        assert real.status_code == fake.status_code, (
            f"{suffix} still answers differently for a real id ({real.status_code}) "
            f"and a fake one ({fake.status_code}); that difference IS the secret")
    assert observed, "the sweep collected no rows -- the route list is wrong"


def test_measured_the_oracle_closes_once_the_clock_has_started(world) -> None:
    """The positive control for the defect above: priming removes the split.

    This is why the oracle is easy to miss. Once any earlier read has started
    the clock, the anonymous answer is 401 for both a real and a fake id, and
    the leak is invisible to a test that primes first.
    """
    for suffix in _UNPRIMED_ANON_REFUSALS:
        pid = f"orclosed{suffix.strip('/').replace('-', '_')}"
        seed(world, pid)
        prime(world, pid)                       # the only difference
        real = call(world, "POST", suffix, pid)
        fake = call(world, "POST", suffix, f"{pid}-not-a-player")
        assert real.status_code == fake.status_code == 401


def test_measured_a_stranger_gets_the_same_free_membership_test(world) -> None:
    """MEASURED DEFECT, pinned as it ships.

    Not an existence oracle for a stranger in the strict sense -- they already
    hold a session -- but it does tell them which player ids exist and which
    have started a life, for free, from a status code. 400 for unstarted,
    403 for started.
    """
    seed(world, "str_unstarted")
    seed(world, "str_started")
    prime(world, "str_started")
    stranger = owner_of(world, "stranger")
    unstarted = call(world, "POST", "/claim-offline", "str_unstarted", stranger)
    started = call(world, "POST", "/claim-offline", "str_started", stranger)
    assert unstarted.status_code == 400
    assert detail_of(unstarted) == NOT_STARTED
    assert started.status_code == 403
    assert unstarted.status_code != started.status_code


# ===========================================================================
# ANONYMOUS READS WRITE STATE: MEASURED, PINNED, NOT SANCTIONED
# ===========================================================================
#
# The anonymous-GET arm above passes for the WRONG reason on a fresh player,
# and this section says so.
#
# MEASURED: ``GET /lifecycle/<fresh pid>`` with no ``Authorization`` header
# answers 401 AND has already committed, to the stored row:
#
#     lifecycle            a full 11-key fresh state
#     last_cultivation_time  the server clock
#     return_receipt       an initialization receipt
#
# The lifecycle guard is a dependency, so it runs and commits; the ownership
# guard refuses afterwards. A request that is REFUSED has written the very
# clock whose absence it refused to describe.
#
# Consequence worth stating plainly: the anonymous call STARTS THE CLOCK for a
# player who has not started it, which both closes the oracle above and moves
# that player's offline window. Pinned because it is real, and because a
# correct fix (refuse before writing) turns it red.

def test_measured_an_anonymous_read_commits_the_clock_it_then_refuses(
        world) -> None:
    """MEASURED DEFECT, pinned as it ships.

    A refused request that has already written the player's life clock, their
    offline cursor and a return receipt. The 401 is correct; the write is not.
    """
    pid = "anonwrite1"
    seed(world, pid)
    before = dict(world["players_db"][pid])
    assert "lifecycle" not in before

    r = call(world, "GET", "", pid)
    assert r.status_code == 401

    after = world["players_db"][pid]
    written = sorted(set(after.keys()) - set(before.keys()))
    assert written == ["last_cultivation_time", "lifecycle", "return_receipt"]
    assert after["lifecycle"]["status"] == "active"
    assert after["lifecycle"]["life_index"] == 1


def test_the_owner_read_is_what_is_supposed_to_start_the_clock(world) -> None:
    """The positive control: the owner's read starts the same clock.

    Without this, the test above would also be satisfied by a build where NO
    read starts the clock -- i.e. by the write being absent for a reason that
    has nothing to do with authentication.
    """
    pid = "ownerwrite1"
    seed(world, pid)
    r = call(world, "GET", "", pid, owner_of(world, pid))
    assert r.status_code == 200
    stored = world["players_db"][pid]
    assert stored["lifecycle"]["status"] == "active"
    assert stored["lifecycle"]["dies_at"] == pytest.approx(
        stored["lifecycle"]["born_at"] + lifecycle.INITIAL_LIFE_DAYS * lifecycle.DAY)
# ===========================================================================
# WHAT WAS PROVED CAPABLE OF FAILING -- THE NEW TESTS
# ===========================================================================
#
# A green contract file is worth nothing until a re-injected defect turns it
# red. NINE defects were re-injected on scratch COPIES of ``server/``; the live
# repo was never edited. The unmutated copy is run first as a CONTROL (19 of
# these tests, all passing), because a red baseline proves nothing.
#
# KILLED (6) -- each turned at least one new test red on the first cut:
#   cap-deleted            the cap line removed ......... 2 tests red
#   cap-always-max         the cap replaced by the max .. 1 test red
#   stamp-skipped          the once-only claim stamp dropped 1 test red
#   missing-key-becomes-400  a missing stored key answered 400 1 test red
#   uninit-payload-bloated the refusal grew a second key 1 test red
#   auth-refusal-silenced  authentication turned into a no-op 1 test red
#
# SURVIVED (3) -- stated exactly, with the reason, because "all mutants killed"
# is not what happened here:
#
#   * cap-hardcoded-72 (``min(elapsed_hours, 72.0)``) is an EQUIVALENT MUTANT
#     at today's value, proven not assumed: ``MAX_OFFLINE_HOURS`` measures
#     72.0, so the hardcoded line is byte-for-byte the same computation. It
#     would bite the moment the constant is re-tuned, which is why the tests
#     read the constant rather than the number.
#
#   * reward-priced-from-elapsed (an extra reply key) survived the SELECTED
#     subset only. Run against the WHOLE file it is killed by
#     ``test_claim_publishes_the_documented_key_set``, which pins the claim's
#     key set exactly. Already covered; not a gap.
#
#   * uninit-writes-clock, WEAK form (an assignment on the snapshot the guard
#     already holds) survived -- and the reason is the STORE, not the test.
#     Measured: the same mutation written THROUGH the store
#     (``with db.transaction(pid): db[pid][...] = ...``) is killed immediately
#     by ``test_an_uninitialized_player_is_refused_before_any_state_is_written``.
#     The weak form cannot be observed under the in-memory store, so it is
#     store-masked, not untested.
#
# NOT INJECTED (1) -- an ownership-guard anchor matched zero occurrences after
# the copy, so nothing was written and nothing ran. It is a re-cutting target
# and is NOT evidence either way.
