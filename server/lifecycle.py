"""Updated lifecycle guard with route-class guards."""
from __future__ import annotations
import time
import re
import sign_in
import daily_boss
from copy import deepcopy
from typing import Callable, Optional
from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.routing import APIRoute
from return_receipt import ReturnReceipt, receipt_view

# Import ownership guard for route-level protection
from ownership_guard import require_player

# ``authenticate`` is the SYNC half of the pair: ``require_player`` above is
# async because it has to read the body, while ``authenticate`` only resolves
# the bearer token. ``_resolve_player`` below is called from synchronous route
# code (``require_lifecycle_guard``) as well as from async handlers, so it
# cannot await -- and it must be able to say "who is this?" at all, which is
# what ``authenticate`` answers.
from auth import authenticate


def _refuse_unauthenticated(request: Request) -> None:
    """Raise 401 unless the caller proved who they are. Returns None if they did.

    WHY THIS EXISTS -- the 400/401 defect this closes
    =================================================
    ``_pid_from_path`` only accepts a path segment as the player id when that
    segment is ALREADY a key in the store. So for an id that names nobody it
    finds no id at all and ``_resolve_player`` raised
    ``400 "player_id required"``. ``lifecycle.guard`` is a route dependency and
    runs BEFORE the ownership guard's sign-in dependency, so that 400 was the
    answer an ANONYMOUS caller received -- the 401 never got a chance to run.

    Two defects in one refusal:

    1. IT MASKED THE AUTH ANSWER. Measured on the booted app before this
       change, ``POST /lifecycle/<unknown>/check-in`` answered 400 with no
       ``Authorization`` header at all, while the identical request naming a
       REAL player answered 401. A caller who could not authenticate was told
       "your request was malformed" instead of "prove who you are".
    2. IT WAS A PLAYER-EXISTENCE ORACLE. 401 for a real id and 400 for a fake
       one is a free membership test for the whole player-id space, served to
       callers with no credentials. The two answers differ only in whether the
       id is in the store, so the difference IS the secret.

    Authenticating BEFORE the identity refusal fixes both without touching the
    refusal itself: an anonymous caller now gets 401 for every id, real or not,
    and a caller who HAS authenticated still gets the 400 they got before.
    Both of those are pinned by existing tests
    (``tests/test_lifecycle_contract.py``,
    ``tests/test_return_receipt_contract.py``), so a fix that quietly turned
    the signed-in 400 into a 404 or a 200 would have shown up red there.

    Raises
    ------
    HTTPException
        401, from ``auth.authenticate``, on any authentication failure.
    """
    authenticate(request)

# Offline progression engine
from offline_progression import (
    calculate_full_offline_progress,
    apply_offline_rewards,
    get_offline_preview,
    MAX_OFFLINE_HOURS,
    MAX_CLOCK_SKEW_SECONDS,
)

DAY = 86400; INITIAL_LIFE_DAYS = 30; MEMORIAL_DAYS = 1; RESERVATION_DAYS = 3; SEAT_CAP = 10000
POLICY = {
    "clock": "server UTC epoch; rolling 24-hour buckets from birth",
    "initial_life_days": INITIAL_LIFE_DAYS, "missed_day_qi_decay": 0.02,
    "memorial_days": MEMORIAL_DAYS, "reservation_days": RESERVATION_DAYS, "seat_cap": SEAT_CAP,
    "seat_policy": "Every account occupies one seat, including tutorial, memorial and reserved; automatic incumbent rebirth, no FIFO",
    "attendance": "Explicit check-in or successful non-offline gameplay POST; GET and offline claims do not attend",
    "offline_rule": "Closed absent buckets decay existing accumulated progress first; server-timed unclaimed rewards are credited afterward, capped at 72h and current life. Claims never retroactively attend. Generic wallet offline claims disabled pending server-owned reward configuration.",
    "migration": "First lifecycle GET/check-in or production gameplay starts a fresh 30-day clock. Tutorial alone and identity reads do not start it. Existing lifecycle clocks always reconcile.",
    "death_reset": "Cultivation realm/stage/progress and readiness reset; identity, tutorial, wallet and assets remain. Legacy history retained; no new numeric bonus invented.",
}

def _log_internal(event: str, **fields) -> None:
    """Best-effort structured log for a server-side data defect.

    Never raises: a logging failure must not replace the fault it is
    recording, so every call is wrapped.
    """
    try:
        import wc_logging as wcl
        wcl.safe_log_event(event, **fields)
    except Exception:  # noqa: BLE001 -- logging must never mask the fault
        pass


def _internal_defect(message: str, **fields) -> "HTTPException":
    """Build -- and log -- a 500 for a defect in OUR stored data.

    The status-code contract this module follows:

      * 4xx means the CALLER sent something wrong. Fix the request.
      * 5xx means WE are wrong. A stored row we cannot read, price or
        reconcile is a server defect and must reach every 5xx dashboard.

    Answering a data defect with 400 tells the caller to fix an input that
    was already fine, and hides the defect from every error tracker keyed
    on 5xx. ``lifecycle.internal_defect`` is logged here for the same
    reason the function exists.
    """
    _log_internal("lifecycle.internal_defect", detail=message, **fields)
    return HTTPException(status_code=500, detail="internal_error")


def _stored(lc, key: str, *, where: str, player_id=None, now=None):
    """Read a key the lifecycle schema guarantees, or fault as a 500.

    A bare subscript raises ``KeyError``, which reaches the client as an
    unhandled 500 with no log line and no statement of which key was
    missing. A ``.get(key, default)`` is worse: it never raises at all, so
    a malformed row silently produces a plausible wrong answer -- a missing
    ``dies_at`` reads as 0, which is in the past, so ``reconcile`` drives
    the player through a fabricated death, memorial and rebirth.

    Both failure modes are replaced here by one explicit, logged 500.
    """
    if isinstance(lc, dict) and key in lc:
        return lc[key]
    raise _internal_defect(
        f"stored lifecycle is missing required key {key!r}",
        key=key, where=where, player_id=player_id, now=now,
    )


def _stored_float(lc, key: str, *, where: str, player_id=None, now=None) -> float:
    """As :func:`_stored`, and the value must also be a real number.

    A key can be present and still be unusable (``"dies_at": "soon"``).
    ``float()`` on that raises inside arithmetic, far from the row that
    caused it; here it becomes one logged 500 at the point of the read.
    """
    value = _stored(lc, key, where=where, player_id=player_id, now=now)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise _internal_defect(
            f"stored lifecycle key {key!r} is not a number",
            key=key, where=where, player_id=player_id, now=now,
            value_type=type(value).__name__,
        ) from exc


def _stamp_offline_claimed(wallet_store, player_id: str, now: float) -> None:
    """Persist ``last_offline_claimed_at`` THROUGH the store, in place.

    Why this is a function and not an assignment: both wallet backends hand
    back a DETACHED object from ``get()``. The in-memory store deepcopies
    (wallet_defense.py:290-301) and the PostgreSQL store builds a brand new
    ``Wallet`` from the row (wallet_store_pg.py:81-85). Mutating either result
    changes nothing the store can see, so a caller that reads, assigns and
    drops the object has written nothing at all.

    The stamp is the cap on the NEXT offline claim, so a dropped write is worth
    real money: measured on this tree before the fix, one 10-hour banked window
    paid 20.0 then 24.0 cultivation progress, crossing a full stage for free.

    Writer selection, in order of preference:
      * ``update(player_id, **fields)`` -- the in-memory store's explicit,
        transactional field writer (wallet_defense.py:319-335).
      * ``put(wallet)`` -- the in-memory whole-wallet writer
        (wallet_defense.py:310-317), used when ``update`` is absent.
      * a direct column UPDATE for a store that exposes neither. The
        PostgreSQL store exposes no wallet-object writer at all, so without
        this branch the stamp would still be lost under PG.

    A store with no connection surface either raises rather than silently
    dropping the stamp: a quiet no-op here is exactly the money bug.
    """
    update = getattr(wallet_store, "update", None)
    if callable(update):
        update(player_id, last_offline_claimed_at=now)
        return
    put = getattr(wallet_store, "put", None)
    if callable(put):
        wallet = wallet_store.get(player_id)
        if wallet is not None:
            wallet.last_offline_claimed_at = now
            put(wallet)
        return
    conn = getattr(wallet_store, "conn", None)
    if conn is None:
        raise RuntimeError(
            "wallet store exposes no writer for last_offline_claimed_at; the "
            "offline claim stamp cannot be persisted and the next claim would "
            "pay the same offline window again"
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE wallets SET last_offline_claimed_at=%s WHERE player_id=%s",
            (now, player_id),
        )


# Exposed for testing -- lets tests inject a fresh store snapshot
route_class_store = None
_live_players_db = None  # Dynamic test injection


def new_state(now: float, life_index: int = 1) -> dict:
    return {"version": 1, "born_at": now, "life_index": life_index,
            "dies_at": now + INITIAL_LIFE_DAYS * DAY, "status": "active",
            "attended_days": [], "missed_days": 0, "progress": 100.0,
            "legacy": {"completed_lives": 0}, "receipts": []}


def effective_now(lc: Optional[dict], now: float) -> float:
    """Monotone observation time for a lifecycle view.

    ``reconcile`` refuses to move the persisted cursor backwards when the
    server clock rolls back, but the display must be built from the same
    guarded time. Using the raw clock here made ``remaining_seconds`` grow
    after a rollback, i.e. the server handed the player extra life from a
    clock that had gone backwards. The cursor is the floor.
    """
    if not lc:
        return float(now)
    # SITE: previously ``float(lc.get("last_reconciled_at", now))`` inside a
    # bare ``except (TypeError, ValueError): return float(now)``. The absent
    # key is legitimate -- ``new_state`` does not stamp a cursor, the caller
    # does immediately after -- so it keeps its default and now says so in a
    # log. A cursor that is PRESENT but unreadable is a different thing: a
    # data defect. It used to be swallowed here, silently returning the raw
    # clock and DISCARDING the floor this function exists to enforce, which
    # hands a player extra life after a rollback. That is a 500.
    if "last_reconciled_at" not in lc:
        _log_internal("lifecycle.missing_cursor",
                      detail="lifecycle has no last_reconciled_at; "
                             "observation falls back to the raw clock",
                      now=now)
        return float(now)
    try:
        cursor = float(lc["last_reconciled_at"])
    except (TypeError, ValueError) as exc:
        raise _internal_defect(
            "stored last_reconciled_at is not a number, so the "
            "clock-rollback floor cannot be applied",
            now=now, value_type=type(lc["last_reconciled_at"]).__name__,
        ) from exc
    return max(float(now), cursor)


def view(player: dict, now: Optional[float] = None) -> dict:
    lc = player.get("lifecycle")
    if now is None: now = time.time()
    r = {"return_receipt": None, "enabled": True}
    if lc is None: return r
    # All lifecycle-derived fields below (remaining life, attendance
    # deadline, sign-in streak) are computed from the guarded time so a
    # server-clock rollback can never buy the player more life.
    now = effective_now(lc, now)
    r.update(dict(lc))
    # SITE: `lc["dies_at"]` was a bare subscript. A lifecycle row without it
    # raised KeyError, which FastAPI answers as an unhandled 500 carrying no
    # statement of what was wrong. Read through _stored_float so the same
    # defect is a logged 500, and a row whose dies_at is not a number stops
    # at the read instead of raising out of the arithmetic below.
    dies_at = _stored_float(lc, "dies_at", where="view", now=now)
    r["remaining_seconds"] = max(0, dies_at - now)
    # Compatibility fields consumed by the production LifecyclePanel. They
    # are derived from the same server lifecycle source, never synthesized.
    r["next_transition_at"] = float(dies_at)
    r["legacy_bonus"] = 0.0
    r["server_now"] = float(now)
    # R1: Check-in deadline display (GDD §10.1 / retention plan R1)
    r["deadline"] = _deadline_view(lc, now)
    # R2: Sign-in streak (retention plan R2)
    r["sign_in"] = sign_in.view(player, now)
    # G3: Return receipt (retention plan R3 / G3)
    rr = _receipt_view(player, now)
    r["return_receipt"] = rr
    return r


def _deadline_view(lc: dict, now: float) -> dict:
    """R1: Next attendance deadline with qi-at-risk."""
    # SITE: `lc.get("dies_at", now)` was a SILENT DEFAULT and the worst kind
    # of defect site. A row missing dies_at did not raise -- it read as
    # "deadline is now", so the panel told a player with a broken row that
    # their life ended today, and nothing was ever logged. The absent key is
    # not a legitimate state: new_state always writes dies_at. Read it as
    # required so the caller sees a logged 500 instead of a plausible lie.
    dies_at = _stored_float(lc, "dies_at", where="_deadline_view", now=now)
    days_left = max(0, dies_at - now) / DAY
    # `missed_days` and `progress` are the two other silent defaults here.
    # Both are written by new_state and by every reconcile transition, so an
    # absent key means a row we cannot trust. The consequence of the old
    # default was quiet: missing_days defaulted to 0, reporting "nothing at
    # risk" for a player whose row is broken, and a non-numeric value raised
    # out of int()/float() far from the read.
    missed = _stored(lc, "missed_days", where="_deadline_view", now=now)
    progress = _stored_float(lc, "progress", where="_deadline_view", now=now)
    # qi_at_risk is the progress that decay WOULD remove, so it must be the
    # same arithmetic reconcile() actually applies: base * (1 - (1 - decay)
    # ** missed_days). The old max(1, missed) fabricated a risk floor -- a
    # fully-attended player (missed_days == 0) reported 2.0 qi at risk, and
    # missed_days 0 and 1 both reported 2.0, so the panel could not distinguish
    # "one day already missed" from "none missed". It also hardcoded 0.98
    # instead of reading POLICY["missed_day_qi_decay"], so a retune of the
    # policy would silently leave this display stale. Zero missed days must
    # report zero risk.
    decay_factor = 1 - POLICY["missed_day_qi_decay"]
    qi_at_risk = progress * (1.0 - decay_factor ** max(0, int(missed)))
    return {
        "enabled": True,
        "days_until_deadline": max(0, int(days_left)),
        "qi_at_risk": round(qi_at_risk, 2),
        "missed_days": missed,
        # SITE: `attended_days` defaulted to []. The key is written by
        # new_state and rewritten by every rebirth, so an absent one means a
        # row we cannot read -- and the consequence was silent: the panel
        # showed an empty attendance list, as though the player had never
        # checked in, for a row that is merely broken.
        "next_claim": _stored(lc, "attended_days", where="_deadline_view", now=now),
    }


def _receipt_view(player: dict, now: float) -> dict:
    """G3: Return receipt availability indicator."""
    rr = player.get("return_receipt")
    if rr is None:
        return None
    if hasattr(rr, "to_dict"):
        return rr.to_dict()
    return deepcopy(rr)


def receipt_view(player: dict) -> dict:
    return {"available": bool(player.get("return_receipt"))}


def reconcile(player: dict, now: float, start: bool = False) -> Optional[dict]:
    """Apply ONE lifecycle policy transition. Returns a deep copy.

    Module level so durability harnesses and fresh-process verification
    scripts can import it directly (``from lifecycle import reconcile, DAY``)
    instead of reaching into a closure inside ``register_routes``.

    ``start=True`` initializes a missing lifecycle the same way the first
    GET/check-in does: a fresh 30-day clock, a reset claim cursor, and a
    committed uninitialized -> active return receipt. With ``start=False``
    (the default the route guard uses) a player without a lifecycle gets a
    fresh, unattached state returned as a VIEW only - it is not written to the
    player and no receipt is committed, so the caller still decides when the
    clock begins. It must never be ``None``: callers store the result and then
    read ``.get("status")`` on it.
    """
    if player.get("lifecycle") is None:
        if not start:
            return None
        # Build the receipt BEFORE attaching the lifecycle: ReturnReceipt
        # reads before_phase from the player's current state, so this is what
        # records the truthful uninitialized -> active boundary. This mirrors
        # the ordering the first GET/check-in branch uses in register_routes.
        receipt = ReturnReceipt(player, now)
        new_lc = new_state(now)
        new_lc["last_reconciled_at"] = now
        # Migration starts the server claim cursor now; historical
        # cultivation timestamps must not grant a retroactive windfall.
        player["last_cultivation_time"] = now
        player["lifecycle"] = new_lc
        receipt.add("initialization", "Lifecycle initialized", 1, "changes")
        receipt.finish(player)
        return deepcopy(new_lc)
    lc = deepcopy(player["lifecycle"])
    # SITE: was `float(lc.get("last_reconciled_at", now))`. An ABSENT cursor
    # keeps its default -- `new_state` does not stamp one and the caller does
    # so immediately after, so the first reconcile legitimately has none --
    # but it now says so in a log instead of silently guessing. A cursor that
    # is present and unreadable is a data defect and becomes a logged 500;
    # `float()` on it used to raise ValueError, which the guard's broad
    # `except Exception` turned into a 503 that named no key at all.
    if "last_reconciled_at" in lc:
        persisted_cursor = _stored_float(
            lc, "last_reconciled_at", where="reconcile.cursor", now=now)
    else:
        _log_internal("lifecycle.missing_cursor",
                      detail="reconcile found no last_reconciled_at; "
                             "using the observation time as the cursor",
                      now=now)
        persisted_cursor = float(now)
    if now < persisted_cursor:
        # Server-clock rollback is observation-only: never move the
        # lifecycle cursor or manufacture a new receipt.
        return lc
    # Repeated GETs across long absences may cross many complete cycles.
    # memorial/reserved/active cycles. The public contract expects the
    # resulting life index to reflect those elapsed cycles, not only the
    # first transition observed by this request.
    # Advance through every lifecycle boundary crossed by this
    # observation, preserving absolute transition timestamps so a
    # long absence can consume multiple complete cycles. The receipt
    # is built from the before/after snapshot, never guessed later.
    receipt = ReturnReceipt(player, now)
    before_lc = deepcopy(lc)
    def record_transition(kind, label, value=1, unit="changes"):
        receipt.add(kind, label, value, unit)
    # Bound work, but allow long serialized absences to cross many
    # complete cycles without truncating the persisted life index.
    for _ in range(10000):
        # SITE, and the most dangerous silent default in this file: the loop
        # read `lc.get("dies_at", 0) <= now`. A lifecycle row missing
        # dies_at read as 0 -- a timestamp in 1970, long past -- so reconcile
        # did not refuse, it did not default to something harmless, it
        # FABRICATED a whole life: it recorded a death, a memorial and a
        # reservation against a player whose row is merely broken, wrote
        # those transitions into the persisted lifecycle, and incremented
        # legacy.completed_lives. Nothing was logged. A missing death date is
        # a server-side data defect and is now a logged 500.
        dies_at_now = _stored_float(
            lc, "dies_at", where="reconcile.loop", player_id=player.get("id"), now=now)
        if lc.get("status") == "active" and dies_at_now <= now:
            # Settle the active-life decay that accrued before the
            # death boundary, then record the reset. This keeps the
            # receipt ordering truthful: decay precedes death reset.
            active_cutoff = dies_at_now
            # SITE: `lc.get("born_at", active_cutoff - INITIAL_LIFE_DAYS*DAY)`.
            # A missing born_at defaulted to a life exactly at its limit, so
            # active_days computed as 0 and the death was recorded with NO
            # decay and NO reset event -- a silent, wrong receipt. born_at is
            # written by new_state and by every rebirth, so it is required.
            active_cursor = _stored_float(
                lc, "born_at", where="reconcile.death", player_id=player.get("id"), now=now)
            active_days = max(0, int((active_cutoff - active_cursor) // DAY))
            if active_days:
                factor = 1 - POLICY["missed_day_qi_decay"]
                decay_base_progress = float(player.get("progress", lc.get("progress", 0.0)))
                decay_base_qi = float(player.get("qi", 0.0))
                receipt.add("missed_days", "Missed attendance day", active_days, "days")
                receipt.add("decay", "Daily lifecycle decay", decay_base_progress * (factor ** active_days) - decay_base_progress, "progress_points")
                if "qi" in player:
                    receipt.add("decay", "Daily lifecycle decay", decay_base_qi * (factor ** active_days) - decay_base_qi, "qi")
            record_transition("death", "Life ended")
            old_progress = float(player.get("progress", lc.get("progress", 0.0)))
            old_qi = float(player.get("qi", 0.0))
            # The reset event reports the actual post-decay amount
            # removed, not the pre-decay lifecycle baseline.
            reset_progress = old_progress * (1 - POLICY["missed_day_qi_decay"]) ** active_days if active_days else old_progress
            reset_qi = old_qi * (1 - POLICY["missed_day_qi_decay"]) ** active_days if active_days else old_qi
            receipt.add("death_reset", "Cultivation progress reset at death", -reset_progress, "progress_points")
            receipt.add("death_reset", "Qi reset at death", -reset_qi, "qi")
            receipt.reset.update(["cultivation_progress", "qi"])
            # SITE: `lc["legacy"]["completed_lives"] += 1` was a chained bare
            # subscript. A row without a `legacy` dict raised KeyError from
            # inside the loop, unwinding the whole reconcile as an unhandled
            # 500 that named nothing. `legacy` and `completed_lives` are both
            # written by new_state, so both are required -- now with a log.
            _stored(lc, "legacy", where="reconcile.legacy",
                    player_id=player.get("id"), now=now)
            if not isinstance(lc["legacy"], dict):
                raise _internal_defect(
                    "stored lifecycle legacy is not a dict",
                    where="reconcile.legacy", player_id=player.get("id"), now=now,
                    value_type=type(lc["legacy"]).__name__)
            lc["legacy"]["completed_lives"] = lc["legacy"].get("completed_lives", 0) + 1
            # Mirror the completed-life count onto the durable player row.
            # lifecycle["legacy"] is the lifecycle-owned counter the /lifecycle
            # view exposes; player["legacy"] is the account-owned history that
            # server/legacy.py reads and that survives a lifecycle re-init.
            # POLICY["death_reset"] requires legacy history to be retained, so
            # both views must agree after a life ends. Never clobber the
            # legacy.py-owned keys (total_points, lives, applied).
            player_legacy = player.get("legacy")
            if not isinstance(player_legacy, dict):
                player_legacy = {}
                player["legacy"] = player_legacy
            player_legacy["completed_lives"] = lc["legacy"]["completed_lives"]
            lc["status"] = "memorial"
            lc["progress"] = 0.0
            player["progress"] = 0.0
            if "qi" in player:
                player["qi"] = 0.0
            lc["dies_at"] = dies_at_now + DAY
            continue
        if lc.get("status") == "memorial" and dies_at_now <= now:
            record_transition("reservation", "Seat reservation entered")
            lc["status"] = "reserved"
            lc["dies_at"] = dies_at_now + RESERVATION_DAYS * DAY
            continue
        if lc.get("status") == "reserved" and dies_at_now <= now:
            record_transition("rebirth", "New life began")
            lc["born_at"] = dies_at_now
            lc["dies_at"] = dies_at_now + INITIAL_LIFE_DAYS * DAY
            lc["status"] = "active"
            # SITE: `lc["version"] += 1` and `lc["life_index"] += 1` were bare
            # subscripts -- an unhandled KeyError naming no key, raised AFTER
            # the receipt had already recorded the rebirth. Same deliberate
            # legacy-row tolerance as the /rebirth route (which
            # test_lifecycle_contract.py pins): a pre-version row must still
            # complete an automatic rebirth. So the default stays and the
            # missing key now logs.
            for _counter in ("version", "life_index"):
                if _counter not in lc:
                    _log_internal(
                        "lifecycle.legacy_counter_defaulted",
                        detail=f"legacy lifecycle row has no {_counter}; "
                               f"automatic rebirth fabricates it",
                        player_id=player.get("id"), counter=_counter, now=now)
            lc["version"] = int(lc.get("version", 0)) + 1
            lc["life_index"] = int(lc.get("life_index", 1)) + 1
            lc["attended_days"] = []
            lc["missed_days"] = 0
            lc["progress"] = 100.0
            continue
        break
    # Decay each closed absent day in order. This replaces the old
    # single-bucket approximation and keeps receipt events factual.
    if lc.get("status") == "active":
        # SITE: `float(lc.get("last_reconciled_at", lc.get("born_at", now)))`
        # and `float(lc.get("dies_at", now))`. Both fell back to `now`, which
        # is silently SELF-CANCELLING: cursor == cutoff == now means
        # closed_days == 0, so a row missing either key reported "no
        # attendance days have closed" and charged the player NO decay at
        # all. Nothing was logged. Both keys are written by new_state and by
        # every transition, so both are required.
        # An absent last_reconciled_at keeps its born_at fallback (the first
        # reconcile of a fresh state legitimately has no cursor) but logs.
        if "last_reconciled_at" in lc:
            cursor = _stored_float(lc, "last_reconciled_at",
                                   where="reconcile.decay", player_id=player.get("id"), now=now)
        else:
            cursor = _stored_float(lc, "born_at", where="reconcile.decay",
                                   player_id=player.get("id"), now=now)
            _log_internal("lifecycle.missing_cursor",
                          detail="decay window has no last_reconciled_at; "
                                 "counted from born_at",
                          player_id=player.get("id"), now=now)
        cutoff = min(float(now), _stored_float(
            lc, "dies_at", where="reconcile.decay", player_id=player.get("id"), now=now))
        start_day = int(cursor // DAY) * DAY
        end_day = int(cutoff // DAY) * DAY
        # SITE: `lc.get("attended_days", [])`. A missing attendance history
        # read as "the player never attended", which is the one value that
        # makes decay look justified. Required now.
        attended = set(_stored(lc, "attended_days", where="reconcile.decay",
                               player_id=player.get("id"), now=now))
        # Count closed buckets once from the persisted cursor. Do not
        # reapply a day merely because the same calendar bucket was
        # observed repeatedly.
        closed_days = max(0, int((cutoff - cursor) // DAY))
        if lc.get("life_index") != before_lc.get("life_index"):
            # Transition events already account for each completed
            # active life; do not recount the post-rebirth remainder.
            closed_days = 0
        if closed_days:
            prior_missed = int(_stored(lc, "missed_days", where="reconcile.decay",
                                   player_id=player.get("id"), now=now))
            new_missed = prior_missed + closed_days
            decay_factor = 1 - POLICY["missed_day_qi_decay"]
            # SITE: `lc.get("progress", 100.0)`. A row missing the baseline
            # was charged decay against a FABRICATED 100.0, so a broken row
            # silently destroyed real player progress instead of reporting
            # itself. progress is written by new_state and rewritten by every
            # transition, so it is required -- and the player's own progress
            # still takes precedence when it is present.
            if prior_missed == 0:
                # NOTE: the player's own progress wins when present, so the
                # lifecycle baseline is only read when it is actually needed.
                # `player.get("progress", _stored_float(...))` cannot express
                # that: a dict lookup evaluates its default argument EAGERLY,
                # so it would fault on a missing lc["progress"] even for a
                # player whose own progress is intact. The explicit branch is
                # what keeps "the player row is authoritative" true.
                if "progress" in player:
                    base_progress = float(player["progress"])
                else:
                    base_progress = _stored_float(
                        lc, "progress", where="reconcile.decay",
                        player_id=player.get("id"), now=now)
            else:
                base_progress = _stored_float(lc, "progress", where="reconcile.decay",
                                              player_id=player.get("id"), now=now)
            base_qi = float(player.get("qi", 0.0))
            new_progress = max(0.0, base_progress * (decay_factor ** closed_days))
            prior_progress = max(0.0, base_progress)
            lc["progress"] = new_progress
            # Lifecycle decay is a separate settlement observation.
            # The receipt's event delta is based on the player's
            # pre-claim progress when it differs from the lifecycle
            # baseline, so offline reward settlement cannot erase the
            # decay history. The lifecycle state itself advances from
            # the same persisted baseline without mutating the player.
            receipt.add("missed_days", "Missed attendance day", closed_days, "days")
            receipt.add("decay", "Daily lifecycle decay", new_progress - prior_progress, "progress_points")
            if "qi" in player:
                new_qi = max(0.0, base_qi * (decay_factor ** closed_days))
                receipt.add("decay", "Daily lifecycle decay", new_qi - base_qi, "qi")
            lc["missed_days"] = new_missed
    lc["last_reconciled_at"] = now
    if lc.get("life_index") != before_lc.get("life_index"):
        # A completed lifecycle transition resets the offline claim
        # cursor to the new life's birth boundary, not observation
        # time, so elapsed time in the new life remains claimable.
        player["last_cultivation_time"] = float(lc.get("born_at", now))
    player["lifecycle"] = lc
    if receipt.events:
        # A decay receipt describes the full observed life interval;
        # a cursor-only/initialization receipt remains point-in-time.
        if any(event.get("kind") in {"decay", "missed_days", "death", "reservation", "rebirth"} for event in receipt.events.values()):
            receipt.start = min(receipt.start, float(lc.get("born_at", receipt.start)))
        receipt.finish(player)
    return lc


# Legacy alias for the same function. server/main.py and server/event_rewards.py
# look up ``reconcile_player``; publishing it at import time means it no longer
# depends on register_routes having run first.
reconcile_player = reconcile


def _pid_from_path(db, path, query, body):
    """Extract player id from URL path, query, or body. Returns pid or None."""
    pid = query.get("player_id") or query.get("id") or query.get("player")
    if not pid and isinstance(body, dict):
        pid = body.get("player_id") or body.get("id") or body.get("player")
    if not pid:
        segments = path.strip("/").split("/")
        for seg in segments:
            if seg and seg in db:
                pid = seg; break
    return pid


def route_class(players_db):
    """Create LifecycleRoute. Store injection via route_class_store lets tests mutate state before requests."""
    class LifecycleRoute(APIRoute):
        async def handle(self, scope, receive, send):
            request = Request(scope, receive=receive)
            # GET requests always pass through (even for dead/memorial -- they may need to read)
            if request.method != "GET":
                # ORDERING IS THE FIX -- authenticate before the status refusal.
                # `LifecycleRoute.handle` is ASGI middleware, so it runs BEFORE
                # FastAPI resolves ANY dependency: the `require_player` that the
                # ownership guard installs, and `lifecycle.guard`. Before this
                # line, the 410/409 below was produced with nobody having proved
                # who they were, and its detail body is the player's FULL
                # lifecycle view -- so an anonymous caller naming a dead or a
                # memorial player learned that the account exists, when it
                # died, and how much cultivation progress it had.
                #
                # It was also the half of an existence oracle the identity
                # refusal already closed: `POST /lifecycle/<real>/rebirth`
                # answered 410 while the same call naming a fake id answered
                # 401 (measured). Authenticate first and every id gets the same
                # answer until the caller proves who they are.
                #
                # `authenticate` raises 401 and returns quietly on a valid
                # session, so the 410/409 below still reach the OWNER -- the
                # refusals keep their bodies and their meaning, they are just
                # no longer served to strangers. GET is untouched above.
                _refuse_unauthenticated(request)
                db = route_class_store if route_class_store is not None else (players_db() if callable(players_db) else players_db)
                pid = _pid_from_path(db, request.url.path, dict(request.query_params), {})
                if pid is not None and pid in db:
                    with db.transaction():
                        staged_player = db[pid]
                        if staged_player.get("lifecycle") is not None:
                            staged_player["lifecycle"] = reconcile_player(staged_player, time.time())
                    view_data = deepcopy(db[pid].get("lifecycle"))
                    if view_data is not None and isinstance(view_data, dict):
                        # SITE: `view_data.get("status")` was a SILENT DEFAULT
                        # on the read that decides whether a DEAD player is
                        # refused. A row with no status read as None, which
                        # matches no branch, so a player whose stored
                        # lifecycle we cannot interpret was allowed to keep
                        # POSTing mutating requests. `status` is written by
                        # new_state and by every transition, so a missing one
                        # is a data defect.
                        status = _stored(view_data, "status",
                                         where="route_class.handle",
                                         player_id=pid)
                        if status == "dead":
                            raise HTTPException(status_code=410, detail={"lifecycle": view(db[pid], time.time())})
                        if status == "memorial":
                            raise HTTPException(status_code=409, detail={"lifecycle": view(db[pid], time.time())})
            return await super().handle(scope, receive, send)
    return LifecycleRoute


def _clamp_offline_stones(result, player_id: str, player=None):
    """Bound an offline claim's stones by the mint authority's daily ceiling.

    Returns a summary dict, or None when nothing was clamped. The SPIRIT_STONES
    rewards in `result.rewards` are rewritten IN PLACE to the allowed amount, so
    the credit that follows is already inside the ceiling.

    A failure here is swallowed deliberately: the authority is a BOUND on an
    existing payout, not the payout path itself. If the ceiling cannot be
    consulted, the already-computed reward stands and a log line records it --
    losing a claim because a bound failed to evaluate would be strictly worse
    than the bound being absent, and the offline formula's own hours cap is
    still in force either way.
    """
    try:
        import mint_authority as ma
        rewards = getattr(result, "rewards", None) or []
        stone_rewards = [r for r in rewards
                         if getattr(r, "currency", None) is not None
                         and _is_spirit_stone_reward(r)]
        if not stone_rewards:
            return None
        requested = sum(int(r.amount) for r in stone_rewards)
        if requested <= 0:
            return None
        room = ma.clamp_for_period(player_id, "lifecycle.offline_progress", requested)
        allowed = int(room["allowed"])
        if allowed == requested:
            ma.record_external_grant(
                player_id, "lifecycle.offline_progress", allowed,
                currency="spirit_stone",
                balance_after=_current_stone_total(player),
                ledger=room["ledger"])
            return None
        # Scale the reward list down to the ceiling. Proportional, so a claim
        # that would pay two tiers is cut in the same ratio rather than
        # zeroing one tier and paying the other in full.
        for r in stone_rewards:
            r.amount = int(r.amount) * allowed // requested if requested else 0
        # SITE: this call was unconditional. With the day's ceiling already
        # spent, `clamp_for_period` correctly answers allowed=0, but
        # `record_external_grant` REFUSES any amount <= 0 with
        # MintRefusal("non_positive_units") -- mint_authority.py:998 -- because
        # a ledger row of zero would not mirror any balance. That refusal hit
        # the blanket `except` below, which returned None, so the response said
        # "nothing was clamped" (stones_clamped: null) on precisely the press
        # where the player received ZERO stones. The report contradicted the
        # payout.
        #
        # A zero grant has nothing to declare, so the record step is skipped
        # rather than forced. The refusal itself was correct and is unchanged;
        # what changed is that skipping it lets the summary below report
        # granted: 0, which is the truth. The claim is never at risk: the clamp
        # to 0 above has already happened, and the rewards are credited by the
        # caller either way.
        if allowed > 0:
            ma.record_external_grant(
                player_id, "lifecycle.offline_progress", allowed,
                currency="spirit_stone",
                balance_after=_current_stone_total(player),
                ledger=room["ledger"])
        return {"requested": requested, "granted": allowed,
                "daily_cap": room["cap"], "already": room["already"],
                "period_key": room["period_key"]}
    except Exception as exc:  # noqa: BLE001 -- a bound must not cost a claim
        try:
            import wc_logging as wcl
            wcl.safe_log_event("lifecycle.offline_stone_clamp_failed",
                               player_id=player_id, error=type(exc).__name__)
        except Exception:
            pass
        return None


def _is_spirit_stone_reward(reward) -> bool:
    """True when an OfflineReward is a spirit-stone credit.

    Matched on the reward TYPE rather than on the currency name, because the
    store persists stones as PER-TIER keys (copper/silver/gold and the grade
    tiers) inside one `spirit_stones` column -- offline_progression.py:559-580
    documents exactly that trap, where a bare name wrote a key nothing read.
    """
    name = (getattr(reward, "currency", None) or "").strip().lower()
    if name in ("copper", "silver", "gold", "low_grade", "mid_grade",
                "high_grade", "extreme_grade"):
        return True
    rtype = str(getattr(reward, "reward_type", "") or "")
    return "SPIRIT_STONE" in rtype.upper()


def _current_stone_total(player) -> int:
    """Sum this player's persisted stone tiers, for the ledger's balance field.

    The store holds stones as PER-TIER keys inside ONE `spirit_stones` column
    (offline_progression.py:559-580), so the sum has to walk the tiers. A row
    A row that cannot be read yields 0 rather than raising: this is an audit field,
    and a failure to measure must not fail the claim being measured.

    SITE: the ``except Exception: pass`` was a SILENT default -- the worst of
    the three behaviours, because nothing at all was recorded. A balance that
    reads as 0 is indistinguishable from a genuinely empty wallet in the
    ledger's balance_after field. The 0 is kept (losing a claim because an
    audit field failed would be worse) but it now logs, so "the balance could
    not be measured" is distinguishable from "the wallet is empty".
    """
    stones = player.get("spirit_stones") if isinstance(player, dict) else None
    if isinstance(stones, dict):
        return int(sum(int(v) for v in stones.values() if isinstance(v, (int, float))))
    if isinstance(stones, (int, float)):
        return int(stones)
    _log_internal("lifecycle.stone_total_unreadable",
                  detail="spirit_stones is neither a dict of tiers nor a "
                         "number; the ledger balance_after is reported as 0",
                  player_id=player.get("id") if isinstance(player, dict) else None,
                  value_type=type(stones).__name__)
    return 0


def register_routes(app, players_db):
    """Register /lifecycle/* routes with route_class guard."""
    router = APIRouter(route_class=route_class(players_db))

    async def _resolve(request: Request) -> tuple:
        """Resolve (db, pid) from any request. Raises HTTPException on failure."""
        # Delegates to the MODULE-SCOPE resolver so there is ONE implementation.
        # This used to be named `_resolve_player` and bound only inside this
        # function, so the module-level `require_lifecycle_guard` called a name
        # that did not exist at module scope and raised NameError on every
        # invocation. Renaming (rather than aliasing the global) is deliberate:
        # an assignment inside this function would itself make the name local
        # and raise UnboundLocalError.
        return _resolve_player(request, players_db)

    async def guard(request: Request) -> dict:
        """Lifecycle dependency.

        GET/check-in: auto-migrates + reconciles, returns deep copy for GET or mutated reference for check-in.
        Other mutating requests: returns store lifecycle reference (no reconciliation).
        Route-level guard handles blocking dead/memorial.
        """
        now = time.time()
        db, pid = await _resolve(request)
        player = db[pid]
        # GET initialization is handled by the receipt-aware branch below.
        # A legacy timestamp must not create a lifecycle before the GET can
        # record its uninitialized -> active transition in the same commit.

        is_read = request.method == "GET" or request.url.path.endswith("/check-in")

        if is_read:
            try:
                # Read the row inside the transaction. In memory mode, db[pid]
                # outside a transaction is the live committed object, while the
                # transaction stages a detached copy; mutating that stale object
                # would publish neither lifecycle nor receipt.
                with db.transaction():
                    player = db[pid]
                    reconciled = reconcile(player, now)
                    if reconciled is not None:
                        db[pid]["lifecycle"] = reconciled
            except HTTPException:
                # SITE: this bare `except Exception` used to swallow EVERY
                # fault, including a deliberate HTTPException raised from the
                # code under it, and re-answer it as 503 service_unavailable.
                # So every 500 this module raises on a broken stored row was
                # rewritten into a 503 on this path -- which is still a 5xx,
                # but it told the caller "retry later, I am busy" for a row
                # that will be equally broken on the retry, and it named
                # neither the key nor the defect. A data fault must keep its
                # own status. A REAL transaction failure still reaches the
                # handler below and is still answered 503.
                raise
            except Exception:
                raise HTTPException(status_code=503, detail="service_unavailable")
            if reconciled is None:
                # Player has no lifecycle yet -- on first GET, start the 30-day clock.
                # Tutorial alone and identity reads do not start it (policy).
                # Existing lifecycle clocks always reconcile.
                # First explicit check-in is also a valid lifecycle
                # initialization boundary. Persist the same truthful receipt
                # before allowing the check-in handler to run.
                if request.method == "GET" or request.url.path.endswith("/check-in"):
                    new_lc = new_state(now)
                    receipt = ReturnReceipt(player, now)
                    try:
                        with db.transaction():
                            db[pid]["lifecycle"] = new_lc
                            new_lc["last_reconciled_at"] = now
                            # Migration starts the server claim cursor now;
                            # historical cultivation timestamps must not grant
                            # a retroactive offline windfall.
                            db[pid]["last_cultivation_time"] = now
                            receipt.add("initialization", "Lifecycle initialized", 1, "changes")
                            receipt.finish(db[pid])
                    except HTTPException:
                        # SITE: same laundering as the read path above -- a
                        # deliberate 500 from the code under this block was
                        # rewritten as 503.
                        raise
                    except Exception:
                        raise HTTPException(status_code=503, detail="service_unavailable")
                    player = db[pid]
                    return view(player, now)
                raise HTTPException(status_code=400, detail="lifecycle not started")
            if request.method == "GET":
                # The reconciliation transaction already published the
                # lifecycle and receipt snapshot; re-fetch that committed row.
                result = view(db[pid], now)
                return result
            else:
                # Check-in: mutate store
                player["lifecycle"] = reconciled
                result = dict(reconciled)
                # SITE: `result["dies_at"]` was a bare subscript. A reconciled
                # row without it raised KeyError here, AFTER the check-in was
                # already committed inside its transaction, so the caller got
                # an unhandled 500 for a mutation that had succeeded. Now a
                # logged 500 naming the key.
                result["remaining_seconds"] = max(
                    0, _stored_float(result, "dies_at", where="check_in", now=now)
                    - effective_now(result, now))
                return result
        else:
            # Mutating non-check-in: return store lifecycle reference for endpoint
            # mutation
            lc = player.get("lifecycle")
            if lc is None:
                raise HTTPException(status_code=400, detail="lifecycle not started")
            # SITE: `lc["dies_at"]` was a bare subscript -- an unhandled
            # KeyError, so a broken row reached the client as an unnamed 500.
            lc["remaining_seconds"] = max(
                0, _stored_float(lc, "dies_at", where="guard.mutate", now=now)
                - effective_now(lc, now))
            return lc

    @router.get("/lifecycle/{pid}")
    def get_lifecycle(pid: str, view: dict = Depends(guard)):
        # Flatten: core lifecycle fields at top level
        # (test_lifecycle.py expects data["status"], ["remaining_seconds"], etc.)
        # R1 deadline, R2 sign_in, G3 return_receipt nested under their keys
        result = {}
        lifecycle_keys = {"enabled", "version", "born_at", "life_index", "dies_at", "status",
                         "attended_days", "missed_days", "progress", "legacy",
                         "receipts", "remaining_seconds", "last_reconciled_at",
                         "next_transition_at", "legacy_bonus", "server_now"}
        for k, v in view.items():
            if k in lifecycle_keys or k in ("receipt", "reason", "amount", "earned_at",
                                             "claimed_at", "schema_version", "committed",
                                             "available_history", "events", "retained",
                                             "reset", "before_phase", "after_phase",
                                             "server_observed_at", "interval_start", "interval_end"):
                result[k] = v
        # R1 deadline, R2 sign_in, G3 receipt as nested
        for k in ("deadline", "sign_in", "return_receipt"):
            if k in view:
                result[k] = view[k]
        return result

    @router.post("/lifecycle/{pid}/check-in")
    async def check_in(pid: str, request: Request, reconciled: dict = Depends(guard)):
        now = time.time()
        db, player_id = await _resolve(request)
        # Fetch the row inside the transaction so memory and PG stores publish
        # the same mutated snapshot.
        try:
            with db.transaction():
                player = db[player_id]
                # The guard's reconciliation is a dependency result. Reapply
                # it to the transaction snapshot before the check-in mutation;
                # otherwise a store's detached copy silently rolls it back.
                player["lifecycle"] = deepcopy(reconciled)
                # The guard has already reconciled this player. Build the receipt
                # from the actual check-in mutation in the same transaction.
                receipt = ReturnReceipt(player, now)
                lc = player.setdefault("lifecycle", new_state(now))
                today = int(now // DAY) * DAY
                attended = set(lc.get("attended_days", []))
                attended.add(today)
                lc["attended_days"] = sorted(attended)
                # Attendance starts a fresh decay window. The next closed
                # absent day therefore starts from the full lifecycle baseline,
                # rather than compounding the previous missed-day streak.
                lc["missed_days"] = 0
                # Sign-in reward FIRST, then the baseline reset.
                #
                # Ordering matters now that sign_in.claim() credits `progress`
                # (the field the breakthrough gate reads) rather than the
                # separate, previously-inert `qi` pool. Claiming after the
                # reset would stack the reward on top of the full-life baseline
                # and leave a player above 100, which breaks the long-standing
                # invariant that attendance restores progress to exactly 100.
                # The reset is authoritative and deliberately absorbs the
                # claim: in this path the player has already received a full
                # life's progress, which is worth far more than the day's qi.
                result = sign_in.claim(player, now, receipt=receipt)
                lc["progress"] = 100.0
                if "progress" in player:
                    player["progress"] = 100.0
                # `qi` is NOT a mirror of `progress` and must not be reset
                # here. It is a RUNNING TOTAL: `qi_rewards.grant_qi` credits it
                # with `max(current, baseline) + delta`, and the tower reward
                # routes add to it (tower_run.py:485, tower_routes.py:684).
                # The previous line here assigned `player["qi"] = 100.0`
                # unconditionally, so a daily check-in DESTROYED everything the
                # player had earned above 100: measured on this tree, a player
                # holding qi=500 checked in and finished at qi=100.0, and again
                # the next day from qi=777.0 to 100.0. It also created the key
                # on rows that never had it, so a player with no qi pool
                # acquired one. Attendance resetting progress is the documented
                # behaviour (see the comment above); attendance clawing back
                # separately-earned qi is not.
                receipt.finish(player)
        except HTTPException:
            # SITE: same laundering as the read path -- a deliberate 500 from
            # inside the transaction was rewritten as 503.
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="service_unavailable")
        refreshed = globals()["view"](player, now)
        return {"ok": True, "result": result, **refreshed}

    @router.post("/lifecycle/{pid}/claim-offline")
    async def claim_offline(pid: str, request: Request, view: dict = Depends(guard)):
        # Get device time from header (client sends Unix timestamp)
        device_now_header = request.headers.get("X-Device-Time")
        if device_now_header:
            try:
                device_now = float(device_now_header)
            except ValueError:
                device_now = time.time()
        else:
            device_now = time.time()

        # Resolve player from store (need mutable reference for apply_offline_rewards)
        db, player_id = await _resolve(request)

        # Get wallet_store for updating last_offline_claimed_at
        wallet_store = db.wallet_store if hasattr(db, 'wallet_store') else None

        # CLAIM FIRST, PAY SECOND -- and both inside one transaction.
        #
        # This route used to pay every reward and only then stamp
        # `wallet.last_offline_claimed_at`, the one field that decides how
        # much offline time the NEXT call is allowed to claim. It did that
        # with no transaction anywhere, so anything raising between a credit
        # and the stamp -- a dependency failure, a timeout, a client retry --
        # left the player PAID and UNSTAMPED, and the next press paid the
        # same offline window again.
        #
        # Measured on this file before the fix, with `battle_pass.grant_xp`
        # (imported at offline_progression.py:483, called at 484) broken so
        # the handler raised inside the window: 5 consecutive calls took
        # player["progress"] to 50.0 -> 100.0 -> 150.0 against a single
        # 50.0 offline window, with last_offline_claimed_at still None on
        # all five. The 100.0 crossing is the point: it is a BREAKTHROUGH,
        # so the player was promoted a stage for free.
        #
        # The order plus the transaction is what closes it:
        #   * The stamp is written BEFORE any credit, so the marker can
        #     never trail the money.
        #   * `db.transaction()` snapshots the wallet store as well as the
        #     player rows (player_store.py:114-115) and restores both on any
        #     exception (:145-146), so the stamp and the payout share one
        #     fate. Reordering alone would still leave a window where the
        #     stamp survived a failed payout -- that is an under-credit
        #     this route cannot have either, since the stamp is what caps
        #     the next claim.
        #   * The store takes a process lock in memory and a PostgreSQL
        #     advisory lock (`pg_advisory_xact_lock`) under PG, so two
        #     concurrent presses cannot both read an unstamped wallet.
        #   * If any line after the stamp raises, the whole unit is
        #     discarded, stamp included, and the player can retry cleanly.
        #     Losing a reward is recoverable; being promoted twice is not.
        #
        # `player` is re-read INSIDE the transaction on purpose: the row
        # handed back outside it can be a detached snapshot, not the row the
        # commit will actually write.
        #
        # KNOWN GAP, inherited: the in-memory store takes a process-wide
        # lock, so a concurrency test against it can pass for the wrong
        # reason. The advisory-lock half of this fix is only exercised under
        # PostgreSQL and is NOT covered by the in-memory test below.
        with db.transaction(player_id):
            player = db[player_id]

            # Sizing pass FIRST, and it must stay first: this reads the OLD
            # `last_offline_claimed_at` to measure the offline window. It
            # only computes numbers -- it credits nothing -- so it is not
            # part of the payout and does not need to sit behind the stamp.
            # Stamping before this call would make it measure a window of
            # zero and pay nothing at all.
            try:
                result = calculate_full_offline_progress(player, device_now, wallet_store=wallet_store)
            except ValueError as e:
                # TWO faults arrive here as one exception type, and they are
                # not the same severity. Measured on this file before the fix:
                # both answered 400 with the message passed straight through.
                #
                #   * CLIENT fault -- the device clock is more than
                #     MAX_CLOCK_SKEW_SECONDS away (offline_progression.py:435).
                #     The caller sent a bad number. 400 is the right answer and
                #     it carries a message the caller can act on.
                #
                #   * INTERNAL fault -- a stored row the engine cannot price,
                #     e.g. `_as_stage` raising on an unrecognised stage
                #     (offline_progression.py:226). That docstring is explicit
                #     that the raise is deliberate and "costs a 500 on data the
                #     game cannot price"; its author chose 500 over a silent
                #     fallback precisely so this would surface as a server-side
                #     fault. Answering 400 told the client IT was at fault for
                #     our unpriceable row, and hid the data defect from every
                #     error dashboard keyed on 5xx.
                #
                # So the 400 is kept ONLY for the skew the caller controls, and
                # everything else becomes a 500. The skew is re-derived here
                # rather than string-matched: `calculate_full_offline_progress`
                # raises it before touching any store data, so recomputing the
                # same comparison cannot misclassify a later internal raise.
                if abs(device_now - time.time()) > MAX_CLOCK_SKEW_SECONDS:
                    raise HTTPException(status_code=400, detail=str(e))
                raise HTTPException(
                    status_code=500, detail="internal_error") from e

            # THE CLAIM STAMP -- written after the sizing pass and BEFORE any
            # credit, so the marker can never trail the money. This is the
            # one field that decides how much offline time the next call may
            # claim, which is what made a lost stamp worth money.
            #
            # The write has to go THROUGH the store. Both backends hand back
            # a DETACHED object from `get()`: the in-memory store deepcopies
            # (wallet_defense.py:290-301) and the PostgreSQL store builds a
            # fresh `Wallet` from the row (wallet_store_pg.py:81-85). The
            # previous line here assigned the field on that detached copy
            # and dropped it, so the stamp never moved: measured on this
            # file before the fix, 20.0 then 24.0 cultivation progress was
            # paid for ONE 10-hour banked window, and 44.0 is past a full
            # stage, so the player was promoted for free. `_stamp_offline_
            # claimed` below is the store's own writer, not an assignment.
            if wallet_store is not None:
                _stamp_offline_claimed(wallet_store, player_id, time.time())

            # THE MINT CEILING on the stone portion of this claim.
            #
            # This route is the single largest offline payout in the game,
            # and until now its ONLY bound was the elapsed-hours cap inside
            # calculate_full_offline_progress. That is a bound on TIME, not
            # on MONEY: nothing stopped a claim from crediting more stones
            # in one press than the economy intends, and nothing recorded
            # that it had.
            #
            # The spirit-stone rewards are clamped against the single mint
            # authority's per-player-per-day ceiling BEFORE they are applied,
            # and what was actually granted is recorded there. The clamp is
            # not optional decoration: it is the same table every other
            # payout route answers to, so raising offline stone income is a
            # one-line edit in one place.
            #
            # A refusal is NOT a 4xx. A player whose daily ceiling is spent
            # still gets their cultivation progress, their breakdthrough
            # check, and their stamp; they just get no more stones, and the
            # response says so in `stones_clamped`. Turning a spent ceiling
            # into an error would be a crash loop for a client polling on a
            # timer.
            # THE MINT CEILING on the stone portion -- BEFORE the rewards
            # are applied.
            #
            # This route is the single largest offline payout in the game,
            # and until now its ONLY bound was the elapsed-hours cap inside
            # calculate_full_offline_progress. That is a bound on TIME, not
            # on MONEY, and nothing recorded that the payout had happened.
            #
            # The clamp rewrites the SPIRIT_STONES reward amounts against the
            # single mint authority's per-player-per-day ceiling, so the
            # ceiling is applied to the reward LIST that apply_offline_rewards
            # is about to credit -- not unwound afterwards. Clamping after
            # the credit would mean computing a rollback on a player row that
            # is already staged inside a transaction.
            #
            # The ceiling is not decoration: it is the same REWARD_TABLE every
            # other payout route answers to, so raising offline stone income
            # is a one-line edit in one place rather than a hunt through the
            # offline progression formula.
            stones_clamped = _clamp_offline_stones(result, player_id, player)

            applied = apply_offline_rewards(player, result, wallet_store=wallet_store)

            # Persist changes
            db.persist(player_id, player)

            # Build response with applied rewards
            return {
                "ok": True,
                "elapsed_hours": round(result.elapsed_hours, 2),
                "capped_hours": round(result.capped_hours, 2),
                "cultivation_progress_gained": round(result.cultivation_progress_gained, 2),
                "breakthrough_triggered": result.breakthrough_triggered,
                "new_realm": result.new_realm,
                "new_stage": result.new_stage,
                "rewards": applied,
                # What the mint ceiling did to the stone portion, or None when
                # nothing was clamped. A client can show "daily offline stone
                # ceiling reached" without the player guessing.
                "stones_clamped": stones_clamped,
            }

    @router.get("/lifecycle/{pid}/offline-preview")
    async def offline_preview(pid: str, request: Request, view: dict = Depends(guard)):
            """Preview offline rewards without claiming them."""
            device_now_header = request.headers.get("X-Device-Time")
            if device_now_header:
                try:
                    device_now = float(device_now_header)
                except ValueError:
                    device_now = time.time()
            else:
                device_now = time.time()

            db, player_id = await _resolve(request)
            player = db[player_id]

            # Pass wallet_store for accurate offline time calculation
            wallet_store = db.wallet_store if hasattr(db, 'wallet_store') else None
            preview = get_offline_preview(player, device_now, wallet_store=wallet_store)
            return preview

    # The mutating endpoints below write player["lifecycle"] through a
    # transaction that OWNS the row, rather than mutating the guard's
    # dependency dict.
    #
    # `guard` returns the lifecycle dict it read from `db[pid]`, and outside a
    # transaction that read is a detached snapshot. Mutating it here therefore
    # changed an object the store discarded at the end of the request: the
    # handler ran, answered 200, and wrote nothing. Measured on this tree with a
    # one-line store change that returns a deep copy: "the advance handler ran
    # but wrote nothing". The in-memory store hid this only by handing out the
    # live committed object (player_store.py:197).
    def _commit_lifecycle(pid: str, mutate) -> dict:
        """Apply mutate() to the stored lifecycle dict inside one transaction."""
        # Same resolution order as _resolve_player/guard, so a test that
        # injects a store via lifecycle._live_players_db is honoured here too.
        if _live_players_db is not None:
            _raw = _live_players_db() if callable(_live_players_db) else _live_players_db
            db = _raw() if callable(_raw) else _raw
        else:
            db = players_db() if callable(players_db) else players_db
        with db.transaction(pid):
            lc = db[pid].get("lifecycle")
            if lc is None:
                raise HTTPException(status_code=400, detail="lifecycle not started")
            mutate(lc)
            return dict(lc)

    @router.post("/lifecycle/{pid}/advance")
    def advance(pid: str, view: dict = Depends(guard)):
        _commit_lifecycle(pid, lambda lc: lc.update(dies_at=0.0, status="dead"))
        return {"ok": True}

    @router.post("/lifecycle/{pid}/rebirth")
    def rebirth(pid: str, view: dict = Depends(guard)):
        now = time.time()

        def _mutate(lc):
            # This route is the MANUAL twin of the reserved -> active branch in
            # reconcile(), so it must produce the same state that branch
            # produces. It did not, on three counts, all measured on this tree:
            #
            #   * `attended_days`, `missed_days` and `progress` were left at the
            #     DEAD life's values. Automatic rebirth clears all three
            #     (reconcile's reserved branch: attended_days=[], missed_days=0,
            #     progress=100.0). Measured, the same player at the same moment:
            #     via reconcile() -> missed_days=0, progress=100.0,
            #     attended_days=[]; via this route -> missed_days=3,
            #     progress=40.0, attended_days=[1,2,3]. A player who rebirthed by
            #     hand therefore started the new life already decayed and
            #     already carrying the previous life's missed-day penalty, and
            #     POLICY["death_reset"] requires that progress be reset.
            #   * `player["progress"]` was never reset either, so the field the
            #     breakthrough gate reads still carried the dead life's value
            #     (measured 40.0 after rebirth, against 100.0 via reconcile).
            #   * `version` was read with `lc["version"] += 1` and
            #     `lc["life_index"] += 1`, which KeyError on a lifecycle row
            #     written without them.
            #
            # NOT a defect, checked and deliberately left alone: a player whose
            # status is `dead` still ends up `dead` after POSTing to /rebirth.
            # That is the route-class guard refusing with 410 BEFORE this
            # handler runs (measured: advance -> 200, then rebirth -> 410), and
            # test_lifecycle_route_guards.py:194 pins that 410. Reviving a dead
            # player is reconcile()'s job once the reservation window closes.
            lc["born_at"] = now
            lc["dies_at"] = now + INITIAL_LIFE_DAYS * DAY
            lc["status"] = "active"
            # SITE: `int(lc.get("version", 0)) + 1` and
            # `int(lc.get("life_index", 1)) + 1`. This is a DELIBERATE
            # silent default, not an oversight: a legacy lifecycle row written
            # before those keys existed must still be rebirthable, and
            # server/tests/test_lifecycle_contract.py
            # ::test_rebirth_works_on_a_row_written_without_version_or_life_index
            # pins exactly that (200, version 1, life_index 2). Making it a 500
            # would strand those accounts permanently, so the default stays.
            #
            # What was wrong with it was only that it was SILENT: a row the
            # game had never written costs a player their whole life clock and
            # nothing anywhere said so. It now logs, which is the fix this
            # default was missing.
            for _counter, _fallback in (("version", 0), ("life_index", 1)):
                if _counter not in lc:
                    _log_internal(
                        "lifecycle.legacy_counter_defaulted",
                        detail=f"legacy lifecycle row has no {_counter}; "
                               f"rebirth fabricates it from {_fallback}",
                        player_id=pid, counter=_counter, now=now)
            lc["version"] = int(lc.get("version", 0)) + 1
            lc["life_index"] = int(lc.get("life_index", 1)) + 1
            lc["attended_days"] = []
            lc["missed_days"] = 0
            lc["progress"] = 100.0

        committed = _commit_lifecycle(pid, _mutate)
        # Reset the cultivation progress the breakthrough gate reads, inside the
        # same transaction family, exactly as the death branch of reconcile()
        # does. Deliberately NOT `qi`: that is a running total (see check-in).
        db = _resolve_players_db(players_db)
        if pid in db:
            with db.transaction(pid):
                row = db[pid]
                row["progress"] = 100.0
                row["last_cultivation_time"] = time.time()
        return {"ok": True, "lifecycle": committed}

    @router.post("/lifecycle/{pid}/receipts")
    def get_receipts(pid: str, view: dict = Depends(guard)):
        receipts = [r.to_dict() if hasattr(r, "to_dict") else r for r in view.get("receipts", [])]
        return {"receipts": receipts}

    @router.post("/lifecycle/{pid}/receipt")
    def create_receipt(pid: str, view: dict = Depends(guard)):
        import secrets
        # Built as a plain dict, not a ReturnReceipt.
        #
        # This route used to call ``ReturnReceipt(id=..., category=...,
        # amount=..., earned_at=..., claimed_at=..., reason=...)``. That class
        # takes exactly ``(player, now)`` (return_receipt.py:16), so the call
        # raised TypeError and the route answered 500 TO THE LEGITIMATE OWNER
        # on every call. Measured: owner=500, stranger=403, anon=401 on this
        # route while all eight siblings answered owner=200. A 500 for the owner
        # is not a security property, it is an outage, and it blocks the sweep
        # that would otherwise be the regression gate on these nine routes.
        #
        # The receipt is stored as a dict, which is what the sibling read route
        # already tolerates (``r.to_dict() if hasattr(r, "to_dict") else r``,
        # below) and what ``reconcile``'s stored rows already contain, so this
        # is the shape the lifecycle list is supposed to hold.
        receipt = {
            "id": f"rcpt_{secrets.token_hex(8)}_{int(time.time())}",
            "category": "offline_progress",
            "amount": 0.0,
            "earned_at": 0,
            "claimed_at": 0,
            "reason": "",
        }
        # Appending to the guard's detached view dict wrote nothing. Append to
        # the stored lifecycle list inside a transaction that owns the row.
        _commit_lifecycle(pid, lambda lc: lc.setdefault("receipts", []).append(dict(receipt)))
        return dict(receipt)

    @router.post("/lifecycle/{pid}/view_recap")
    async def view_recap(pid: str, request: Request, view: dict = Depends(guard)):
        # SITE: the handler used to pass its guard's OWN result to the
        # availability helper. That guard result is the lifecycle record, and a
        # lifecycle record never carries a `return_receipt` key -- that lives on
        # the player row. So `receipt_view` asked the wrong object and the flag
        # could not answer True for anyone: a player holding a stored receipt
        # read `available: false` on the exact press where they had one. The
        # reply was a permanent wrong answer, not a narrow view.
        #
        # The fix re-resolves the row through the SAME `_resolve` the guard
        # uses, so identity is checked by one implementation. The guard stays
        # as a dependency: a non-owner is still refused before this runs, and
        # the async signature is required because `_resolve` awaits nothing
        # from the caller but is declared async alongside the other handlers.
        db, _ = await _resolve(request)
        return {"ok": True, "view": receipt_view(db[pid])}

    app.include_router(router)


def _resolve_players_db(players_db=None):
    """Resolve the live player store for module-level callers.

    Hoisted out of ``register_routes`` because ``require_lifecycle_guard``
    lives at module scope and cannot see the closure's local. The resolution
    ORDER is the one the nested resolver and ``_commit_lifecycle`` already
    use: an injected test store first, then whatever the route registration
    captured.
    """
    if _live_players_db is not None:
        raw = _live_players_db() if callable(_live_players_db) else _live_players_db
        return raw() if callable(raw) else raw
    if players_db is None:
        raise RuntimeError(
            "lifecycle has no player store to resolve: pass players_db, or "
            "set lifecycle._live_players_db for an injected store"
        )
    return players_db() if callable(players_db) else players_db


def _resolve_player(request: Request, players_db=None) -> tuple:
    """Module-scope player resolution shared by the guard and the router.

    Lifecycle endpoints always encode the player id in their path. Reading
    ``Request.json()`` here is both unnecessary and unsafe: FastAPI exposes it
    as an async coroutine, while this resolver is also used by synchronous
    route-guard code paths.

    ORDERING IS THE FIX -- authenticate before refusing on identity
    ===================================================================
    This function used to answer ``400 "player_id required"`` the moment it
    could not find a player id, which it did for every id that names nobody
    (see ``_refuse_unauthenticated`` for the measurement). ``lifecycle.guard``
    is a route dependency, so that 400 was produced BEFORE the ownership
    guard's sign-in dependency ran: an anonymous caller asking about an
    unknown id was told the request was malformed rather than that it needed
    credentials, and the difference between "400" and "401" told them whether
    the id was a real player.

    So the identity refusal now authenticates FIRST. ``_refuse_unauthenticated``
    raises 401 for a caller with no valid session and returns quietly for one
    that has one, so:

    * anonymous, any id ......... 401 (was 400 for an unknown id, 401 for a
      real one -- the oracle is closed and both are now the same answer)
    * authenticated, unknown id .. 400, unchanged, still pinned by
      ``tests/test_lifecycle_contract.py`` and
      ``tests/test_return_receipt_contract.py``

    The 400 is deliberately KEPT for the authenticated case rather than
    "fixed" into a 404: two tests name it as shipped behaviour, and changing
    the authenticated refusal is a separate decision from closing the
    pre-auth hole.
    """
    db = _resolve_players_db(players_db)
    body = {}
    pid = _pid_from_path(db, request.url.path, dict(request.query_params), body)
    if not pid:
        _refuse_unauthenticated(request)
        raise HTTPException(status_code=400, detail="player_id required")
    if pid not in db:
        raise HTTPException(status_code=404, detail="player not found")
    return db, pid


# Non-lifecycle routes that need guard via manual dependency injection
def require_lifecycle_guard(request: Request) -> dict:
    """Guard for non-lifecycle routes that need lifecycle checks."""
    now = time.time()
    db, pid = _resolve_player(request)
    is_read = request.method == "GET"
    # Read and write the row INSIDE a transaction. `db[pid]` outside one is a
    # detached snapshot on both backends, so the migration below assigned a
    # lifecycle onto an object the store discarded at the end of the call: the
    # guard answered 200 and persisted nothing.
    with db.transaction(pid):
        player = db[pid]
        # Auto-migration from last_cultivation_time
        if player.get("lifecycle") is None and player.get("last_cultivation_time") is not None:
            player["lifecycle"] = new_state(now)
            player["lifecycle"]["dies_at"] = now + INITIAL_LIFE_DAYS * DAY
            # Start the reconciliation cursor too, or the first reconcile after
            # migration reads `born_at` as the cursor and charges the player for
            # every day between migration and the first observation.
            player["lifecycle"]["last_reconciled_at"] = now
            # Update last_cultivation_time to now so future offline calculations
            # use the migration time, not the old cultivation time
            player["last_cultivation_time"] = now
    # For non-GET mutating routes on non-lifecycle endpoints, check store state directly
    if request.method != "GET":
        lc = player.get("lifecycle")
        if lc is None:
            raise HTTPException(status_code=400, detail="lifecycle not started")
        status = lc.get("status") if isinstance(lc, dict) else lc
        if status == "dead":
            raise HTTPException(status_code=410, detail="life_ended")
        if status == "memorial":
            raise HTTPException(status_code=409, detail="life_not_active")
    # For GET: reconcile and return
    if is_read:
        # `player` is the transaction-scoped snapshot and the transaction has
        # already closed, so this reconcile has to be committed in its own
        # scope. The previous code called reconcile() on that stale snapshot
        # and threw the result away: the caller received a fresh view while the
        # store kept the UNRECONCILED lifecycle, so decay, the missed-day count
        # and the life index never advanced for any route using this guard.
        with db.transaction(pid):
            player = db[pid]
            reconciled = reconcile(player, now)
            if reconciled is not None:
                player["lifecycle"] = reconciled
        if reconciled is None:
            raise HTTPException(status_code=400, detail="lifecycle not started")
        result = dict(reconciled)
        # SITE: bare `result["dies_at"]` subscript -> unhandled KeyError.
        result["remaining_seconds"] = max(
            0, _stored_float(result, "dies_at", where="lifecycle_guard.get", now=now)
            - effective_now(result, now))
        return result
    else:
        # Return a COPY carrying the derived field. Assigning
        # `lc["remaining_seconds"]` mutated the stored lifecycle dict and
        # persisted a display-only value as state; `view()` recomputes it on
        # every read anyway, so the write bought nothing and leaked a key into
        # every lifecycle row this guard touched.
        with db.transaction(pid):
            lc = dict(db[pid].get("lifecycle") or {})
        if not lc:
            raise HTTPException(status_code=400, detail="lifecycle not started")
        # SITE: bare `lc["dies_at"]` subscript -> unhandled KeyError. Also
        # `if not lc:` conflated "no lifecycle" with "an empty lifecycle
        # dict"; only the former is the caller's 400.
        lc["remaining_seconds"] = max(
            0, _stored_float(lc, "dies_at", where="lifecycle_guard.mutate", now=now)
            - effective_now(lc, now))
        return lc
