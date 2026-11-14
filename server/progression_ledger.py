"""
Database-enforced milestone claim ledger for the Unified Progression Ladders.

THE DEFECT THIS WOULD BE WITHOUT
--------------------------------
A milestone reward is a CREDIT. A credit paid twice mints currency, and the
player walks away with more than the economy allows. The house rule for that
direction is already written twice on this tree:

  * ``battle_pass.reserve_claim``  (migration 006)
  * ``dungeon_claims.reserve_claim`` (migration 008)

Both make "pays once" a PRIMARY KEY rather than a marker on a player row, and
both say why: a marker in a Python dict or a JSON list is a property of one
process. It is lost on restart, invisible to a second server pointed at the
same database, and trivially deleted by a refactor that stops writing it.

This is the third instance of the same rule, and it inherits the same
arguments verbatim rather than re-deriving them.

THE KEY IS SERVER-DERIVED, NEVER READ FROM THE REQUEST
------------------------------------------------------
``(player_id, milestone_id, period_key)`` is built here from the authenticated
player, a milestone id the caller MAY name (it must, to say which button it
pressed) and a period key this module computes from the server's own clock. A
caller cannot name a key that has not been paid, and a retry that invents a
different ``Idempotency-Key`` still loses, because the Idempotency-Key is not
part of this key at all. A caller-minted key would make "pays once" a property
of the client's memory.

CLAIM FIRST, PAY SECOND
-----------------------
The row is inserted BEFORE the reward is applied, inside the same
``players_db.transaction(player_id)`` that performs the credit. A failure
mid-payout therefore rolls the marker back with the credit and the milestone
stays claimable. There is no state in which a reward was paid with no receipt.

THE REWARD COLUMN IS THE RECEIPT
--------------------------------
A replay is answered with the reward the FIRST attempt stored, not with a bare
400. A client that timed out after a successful payout needs to learn what it
already received, and a synthesised answer would be a guess about somebody's
money.

NO-DATABASE MODE IS EXPLICITLY NOT THE GUARANTEE
------------------------------------------------
The game runs with no PostgreSQL at all. There the ledger falls back to a
process-local dict behind a lock, which is what a marker on a player row was,
and it says so rather than pretending otherwise. The PRIMARY KEY is the
guarantee. The fallback stores the RECEIPT rather than a bare marker so the two
backends answer identically, which means a test run without a database is not
measuring a weaker route than production runs.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Dict, Optional, Set, Tuple

#: Process-local fallback, used ONLY when the store has no database connection.
#: Maps the natural key to the RECEIPT, not to a bare True. Same contract as
#: ``battle_pass._MEM_CLAIM_KEYS``.
_MEM_LEDGER: Dict[Tuple[str, str, str], Optional[dict]] = {}
_MEM_LOCK = threading.Lock()

_LEDGER_SQL = (
    "INSERT INTO milestone_claim_ledger "
    "(player_id, milestone_id, period_key, reward, step_index) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (player_id, milestone_id, period_key) DO NOTHING "
    "RETURNING milestone_id"
)

_LEDGER_FILL_SQL = (
    "UPDATE milestone_claim_ledger SET reward = %s "
    "WHERE player_id = %s AND milestone_id = %s AND period_key = %s "
    # Only a reservation that never received a receipt. The column defaults to
    # '{}' and reserve_claim writes exactly that, so this clause is the
    # database-side twin of the fallback's "still None" check: a second write
    # for a key that was already paid updates NO ROWS, and the paid receipt
    # survives it. Without it this statement is an unconditional overwrite of a
    # money record, which is how a timed-out client ends up told it received a
    # payout that never happened.
    "AND reward = '{}'::jsonb"
)

_LEDGER_READ_SQL = (
    "SELECT reward FROM milestone_claim_ledger "
    "WHERE player_id = %s AND milestone_id = %s AND period_key = %s"
)

_LEDGER_KEYS_SQL = (
    "SELECT milestone_id, period_key FROM milestone_claim_ledger "
    "WHERE player_id = %s"
)

_PROGRESSION_UPSERT = (
    "INSERT INTO player_progression "
    "(player_id, axis_key, current_step, highest_step, catchup_daily_claims, "
    " catchup_weekly_claims, catchup_qi_earned) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (player_id, axis_key) DO UPDATE SET "
    "current_step = EXCLUDED.current_step, "
    "highest_step = GREATEST(player_progression.highest_step, EXCLUDED.highest_step), "
    "catchup_daily_claims = player_progression.catchup_daily_claims + EXCLUDED.catchup_daily_claims, "
    "catchup_weekly_claims = player_progression.catchup_weekly_claims + EXCLUDED.catchup_weekly_claims, "
    "catchup_qi_earned = player_progression.catchup_qi_earned + EXCLUDED.catchup_qi_earned, "
    "updated_at = NOW() "
    "RETURNING catchup_daily_claims, catchup_weekly_claims"
)

_PROGRESSION_READ_SQL = (
    "SELECT current_step, highest_step, catchup_daily_claims, "
    "catchup_weekly_claims, catchup_qi_earned "
    "FROM player_progression WHERE player_id = %s AND axis_key = %s"
)


def _as_json(value: Any) -> Optional[dict]:
    if isinstance(value, str):
        try:
            import json

            return json.loads(value)
        except Exception:  # noqa: BLE001 - a corrupt row is a replay, not a crash
            return None
    return value


def _conn(players_db: Any):
    return getattr(players_db, "current_connection", None)


def _int(value: Any) -> int:
    """``int()`` that cannot raise, on any input at all.

    Measured on this interpreter, a non-finite source fails two DIFFERENT ways
    and one of them is not an error the reader expects:

        int(float("inf"))    -> OverflowError
        int(float("nan"))    -> ValueError
        int(Decimal("NaN"))  -> ValueError

    So ``except (TypeError, ValueError)`` is not enough -- ``OverflowError`` is
    a third exception that escapes it and turns the whole route into a server
    error. An unreadable step is corrupt data, not a large one, so it costs
    that one field and nothing else. Matches ``achievements._int``.

    ``value or 0`` is deliberately NOT the opening line: NaN is truthy, so
    ``nan or 0`` is still ``nan`` and the guard would never fire.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    # An int() that SUCCEEDED is finite by construction, but a Decimal or numpy
    # scalar can reach this point having rounded a non-finite value into
    # something int() accepts, so the check is kept rather than assumed away.
    try:
        return coerced if math.isfinite(float(coerced)) else 0
    except (TypeError, ValueError, OverflowError):
        # 10 ** 400 is a legal Python int and int() reads it fine; only the
        # float() placement overflows. Such a step is unreadable here, and the
        # column it targets is a 4-byte INTEGER that cannot hold it anyway.
        return 0


def _finite_float(value: Any) -> float:
    """``float()`` that returns a finite number or 0.0.

    ``float()`` is the DANGEROUS half of the pair: unlike ``int()`` it does not
    raise on an infinity or a NaN, it RETURNS one. So a corrupt stored value
    sails through an unguarded conversion and lands in the reply, and
    ``catchup_qi_earned`` is read by the screen as "you have earned N". Worse,
    the upsert ACCUMULATES that column (``existing + EXCLUDED``), and both
    ``inf + 1.0`` and ``nan + 1.0`` are still non-finite -- so one bad write
    poisons the player's running total permanently and no later correct write
    recovers it. That is why the write path below guards this too, and not only
    the read.
    """
    try:
        coerced = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return coerced if math.isfinite(coerced) else 0.0


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------

def reserve_claim(
    players_db: Any,
    player_id: str,
    milestone_id: str,
    period_key: str,
    step_index: int,
) -> Tuple[bool, Optional[dict]]:
    """Claim ``(player, milestone, period)``. True = first attempt.

    Call INSIDE the same ``players_db.transaction(player_id)`` that pays the
    reward, and BEFORE the payout. In PostgreSQL the insert is then atomic with
    the credit. Returns ``(True, None)`` on the first attempt and
    ``(False, receipt)`` on a replay, where ``receipt`` is what the first
    attempt paid.
    """
    key = (str(player_id), str(milestone_id), str(period_key))
    conn = _conn(players_db)
    if conn is None:
        with _MEM_LOCK:
            if key in _MEM_LEDGER:
                return False, _MEM_LEDGER[key] or None
            # Reserve with an empty receipt; record_claim_reward fills it in
            # before the caller commits, exactly as the SQL path does.
            _MEM_LEDGER[key] = None
        return True, None

    import psycopg2.extras

    with conn.cursor() as cur:
        cur.execute(
            _LEDGER_SQL,
            (player_id, milestone_id, period_key,
             psycopg2.extras.Json({}), _int(step_index)),
        )
        if cur.fetchone() is not None:
            return True, None
        cur.execute(_LEDGER_READ_SQL, (player_id, milestone_id, period_key))
        row = cur.fetchone()
    return False, (_as_json(row[0]) if row else None) or None


def record_claim_reward(
    players_db: Any,
    player_id: str,
    milestone_id: str,
    period_key: str,
    reward: dict,
) -> None:
    """Store what this claim paid, so a retry gets the same answer. Same unit.

    FIRST WRITE WINS. A second call for a key that already holds a receipt is
    IGNORED on both backends, not applied.

    The receipt is not a status flag, it is the record of money that ALREADY
    MOVED, and it is the only thing a retry is answered from. Overwriting it
    cannot pay twice -- the once-only PRIMARY KEY stopped that already -- but it
    does make the ledger report a payout nobody made, so a client that timed
    out after a real claim is told it received a figure it never received.

    Nothing legitimate needs the replace: the claim route reaches this function
    only on the branch where ``reserve_claim`` reported the FIRST attempt, and a
    retry returns on the replay branch before getting here. So a second write can
    only be a caller bug, a retry loop, or a refactor that moved the call out
    from under ``if first``. On a money path the safe reading of that is "ignore
    it" -- the same choice the sibling release guard makes when it declines to
    release a key already holding a paid receipt.

    Both branches therefore fill only a reservation that has NOT been filled:
    ``None`` in the fallback, ``{}`` in the column.
    """
    key = (str(player_id), str(milestone_id), str(period_key))
    conn = _conn(players_db)
    if conn is None:
        with _MEM_LOCK:
            # ``None`` is the reserved-but-unpaid marker. A key that is absent
            # gets nothing (no write without a reservation) and a key already
            # holding a receipt keeps it (first write wins).
            if key in _MEM_LEDGER and _MEM_LEDGER[key] is None:
                _MEM_LEDGER[key] = dict(reward or {})
        return
    import psycopg2.extras

    with conn.cursor() as cur:
        cur.execute(
            _LEDGER_FILL_SQL,
            (psycopg2.extras.Json(reward or {}), player_id, milestone_id, period_key),
        )


def release_claim(players_db: Any, player_id: str, milestone_id: str, period_key: str) -> None:
    """Undo a reservation whose payout then failed.

    PostgreSQL needs no help: the exception unwinds ``transaction()``, whose
    rollback took the inserted row with it. This exists only so the no-database
    fallback behaves the same way instead of permanently burning the claim.
    """
    key = (str(player_id), str(milestone_id), str(period_key))
    if _conn(players_db) is not None:
        return
    with _MEM_LOCK:
        _MEM_LEDGER.pop(key, None)


def claimed_keys(players_db: Any, player_id: str) -> Set[Tuple[str, str]]:
    """Every ``(milestone_id, period_key)`` this player already holds.

    Feeds ``progression_ladders.claimable_milestones`` so a replay is not
    offered back to the player as claimable.
    """
    conn = _conn(players_db)
    if conn is None:
        with _MEM_LOCK:
            return {
                (mid, period)
                for (pid, mid, period) in _MEM_LEDGER
                if pid == str(player_id)
            }
    with conn.cursor() as cur:
        cur.execute(_LEDGER_KEYS_SQL, (player_id,))
        rows = cur.fetchall() or []
    return {(str(r[0]), str(r[1])) for r in rows}


def has_claimed(players_db: Any, player_id: str, milestone_id: str, period_key: str) -> bool:
    """One key, without pulling the player's whole ledger."""
    conn = _conn(players_db)
    if conn is None:
        with _MEM_LOCK:
            return (str(player_id), str(milestone_id), str(period_key)) in _MEM_LEDGER
    with conn.cursor() as cur:
        cur.execute(_LEDGER_READ_SQL, (player_id, milestone_id, period_key))
        return cur.fetchone() is not None


def _mem_read_progression(player_id: str, axis_key: str) -> Dict[str, Any]:
    with _MEM_LOCK:
        rows = [k for k in _MEM_LEDGER if k[0] == str(player_id)]
    # The fallback tracks claims, not steps, so steps are derived from the
    # player row by the engine and only the CLAIM COUNTS live here.
    daily = sum(1 for (_p, mid, _k) in rows if mid.startswith("catchup_") and _k.isdigit())
    return {
        "current_step": 0,
        "highest_step": 0,
        "catchup_daily_claims": daily,
        "catchup_weekly_claims": 0,
        "catchup_qi_earned": 0.0,
    }


def read_progression(players_db: Any, player_id: str, axis_key: str) -> Dict[str, Any]:
    """One axis row, normalised. Never raises on a missing row."""
    conn = _conn(players_db)
    if conn is None:
        return _mem_read_progression(player_id, axis_key)
    with conn.cursor() as cur:
        cur.execute(_PROGRESSION_READ_SQL, (player_id, axis_key))
        row = cur.fetchone()
    if row is None:
        return {
            "current_step": 0,
            "highest_step": 0,
            "catchup_daily_claims": 0,
            "catchup_weekly_claims": 0,
            "catchup_qi_earned": 0.0,
        }
    # A stored infinity or NaN in ANY of these is a corrupt row, not a large
    # one. ``catchup_qi_earned`` is DOUBLE PRECISION, a type PostgreSQL accepts
    # 'Infinity' and 'NaN' into, and ``float()`` would hand both straight
    # through -- so the reply would carry a poisoned lifetime total and the
    # screen would render it as a real number.
    return {
        "current_step": _int(row[0]),
        "highest_step": _int(row[1]),
        "catchup_daily_claims": _int(row[2]),
        "catchup_weekly_claims": _int(row[3]),
        "catchup_qi_earned": _finite_float(row[4]),
    }


def record_axis_progress(
    players_db: Any,
    player_id: str,
    axis_key: str,
    step: int,
    *,
    daily_claims: int = 0,
    weekly_claims: int = 0,
    qi_earned: float = 0.0,
) -> Dict[str, int]:
    """Write the player's step and their catch-up counters.

    ``highest_step`` is GREATEST-ed in SQL and never moves backwards, so a
    prestige that resets ``current_step`` cannot erase the evidence that the
    player ever got there. The catch-up counters ADD rather than assign, so the
    diminishing-returns curve is monotonic: a prestige cannot hand the player a
    fresh set of full-size payouts.
    """
    conn = _conn(players_db)
    if conn is None:
        # No database: the engine's own pure evaluation is the source of truth
        # and the fallback counter is derived from the ledger in
        # read_progression(). Nothing to write.
        return {
            "catchup_daily_claims": _int(daily_claims),
            "catchup_weekly_claims": _int(weekly_claims),
        }
    # THE WRITE IS GUARDED, NOT ONLY THE READ. The statement below accumulates
    # (`catchup_qi_earned = existing + EXCLUDED`), and a non-finite value added
    # to a non-finite value stays non-finite, so writing one here would leave
    # the player's lifetime total poisoned FOREVER -- every later correct
    # claim adds to it and recovers nothing. One corrupt caller therefore
    # breaks this player permanently, which is a far worse outcome than the
    # 0.0 fallback the guard costs them on that single claim.
    with conn.cursor() as cur:
        cur.execute(
            _PROGRESSION_UPSERT,
            (player_id, axis_key, _int(step), _int(step),
             _int(daily_claims), _int(weekly_claims), _finite_float(qi_earned)),
        )
        row = cur.fetchone() or (daily_claims, weekly_claims)
    return {
        "catchup_daily_claims": _int(row[0]),
        "catchup_weekly_claims": _int(row[1]),
    }
