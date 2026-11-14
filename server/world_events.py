"""
Wayfarer's Cycle — World events: the clock, the four mechanics, the settlement.

WHAT THIS IS
------------
Timed events the SERVER owns. A definition is a row; an instance is one
occurrence of it; a player who fights one gets a row of their own. The server
computes the phase from its own clock, resolves the fight with the shipped
combat function, and decides the reward tier from that player's own recorded
contribution. The client sends an intent and nothing else.

THE FOUR EVENT TYPES, AND WHY THEY DIFFER
-----------------------------------------
They share one phase machine and one personal-relative ladder. They differ in
duration, in how contribution is measured, and in which tiers exist -- because
they are four different retention jobs and a shared duration would flatten that:

  boss       The set-piece. Long warning, short violent window. Contribution is
             DAMAGE, so a careful player who lands one big hit and a reckless
             one who chips for twenty minutes reach comparable numbers.
  invasion   The weekend. Long active window, generous cooldown. Contribution is
             SLOT DEPTH, because an invasion you return to twice is worth more
             than one you beat in a sitting.
  surge      The daily rhythm. Low stakes, everyone clears something.
  festival   No combat at all. Attendance only, one tier. It exists so a player
             who cannot fight today still has something to do, and its
             participation floor is zero by design rather than a special case.

THE FIVE ANTI-ABUSE LAYERS THAT ARE CODE
-----------------------------------------
1. THE SERVER OWNS THE EVENT STATE. Phase, boundaries, tier, contribution,
   reward amount: every one is computed from the server's own rows and the
   server's own clock. The request models in ``world_event_routes.py`` carry no
   field for any of them.
2. The snapshot is frozen once, per player per instance. Power and hourly rate
   are read on first entry and stored on that player's row. A player who
   cultivates at 3am does not make their own event trivial halfway through.
3. One claim per player per instance, ever, enforced by a PRIMARY KEY rather
   than a flag. The insert happens BEFORE the credit, in the same transaction,
   with ``ON CONFLICT DO NOTHING RETURNING``; the row count alone decides who
   pays. A failed credit rolls the marker back with it.
4. The phase is RECOMPUTED at every call. ``state`` is a persisted mirror for
   reports; it is never read by a payout or a denial. That is the difference
   between a label and a control.
5. A per-player sliding window on the two state-changing routes, so spamming
   them cannot burn CPU re-running the combat function.

LAYER SIX, THE ONE A MODIFIED CLIENT CANNOT PASS
------------------------------------------------
``world_event_reward_log``'s PRIMARY KEY (def_key, epoch_start, player_id).
The definition comes from the scheduler and the player from the session. The
epoch was NOT server-derived, and saying so here is what hid the defect this
module now guards: ``epoch_start`` arrives in the request body and is one
third of this key, so a client that named a fresh float per attempt got a
fresh key per attempt and was paid every time. MEASURED before the fix: five
invented epochs paid 25.0 points where one occurrence owes 5.0.

The key is therefore built from ``anchor_lattice`` -- the definition's own
occurrence lattice -- not from the float the caller sent. Every invented value
inside one occurrence now collapses onto the one real anchor, so the once-only
guarantee is server-derived end to end, and the sentence above is finally true.

THE CLOCK IS ONE FUNCTION, AND IT IS TWO GUARDS
------------------------------------------------
``server_now()`` returns ``max(time.time(), _HIGH_WATER)``. A system-clock
rollback cannot move time backwards.

That alone is not enough. Measured: at +30h an instance is in ``cooldown``;
after the clock rolls back to +2h the same pure phase function returns
``active``. Re-opening a closed phase is not a cosmetic bug -- it is a mint.
So the second guard is structural: ``closed_at`` is immutable once written, by a
DATABASE TRIGGER as well as by the handler. A rollback inside a window is
absorbed by the high-water mark within one request; a rollback across a
boundary is refused outright.

NO SHARED POOL
--------------
There is no shared balance column in this schema, and that is the design. A
forty-account grief measured in ``03-faction-beast-nation`` drained a shared
pool 20.7x and left every honest player at zero for the rest of the day. A
player's event progress is a row of their own and no account can spend
another's. The house rule: a player is denied the REWARD, never the PLAY. A
loss still costs nothing and still returns the ladder.

THE MEMORY FALLBACK IS WEAKER, AND SAYS SO
------------------------------------------
The game runs with no PostgreSQL by default. There the tables fall back to
process-local dicts behind a lock, which is what a flag on a record was. It dies
with the process and does not span two servers. The branch is on the store's
own connection rather than on an environment variable, so the store and the
ledger cannot disagree about which mode they are in. The fallback still stores
the RECEIPT rather than a bare marker, so a replay answers identically in both
modes and a test run without a database is not measuring a weaker route than
production.

THE +-12% ROLL, STATED HONESTLY
-------------------------------
Each entry rolls a difficulty multiplier in [0.88, 1.12]. It is a DESIGN CHOICE
wearing a measurement's clothes: the simulated ladder was seed-invariant, and a
mode whose outcome never varies has no reason to be retried. That is a reason,
not a playtest. ``verify_scaling_invariant`` reports the measured distribution
so a balance pass has something to argue with.

FILES
-----
``migrations/022_world_events.sql``  the four tables and the immutability trigger.
``world_events.py``                  this file.
``world_event_routes.py``            the four routes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# MODULE scope, not function scope. This module enables postponed annotation
# evaluation, so FastAPI resolves names as STRINGS against the module's globals.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import combat_config  # noqa: E402
import cultivation_config  # noqa: E402
from qi_rewards import grant_qi  # noqa: E402

logger = logging.getLogger("world_events")

_LOCK = threading.RLock()


# ==========================================================================
# The clock. One clamp, one phase function.
# ==========================================================================

#: The highest time this process has ever observed. A system-clock rollback
#: cannot move ``server_now()`` backwards. Module-level on purpose: one number,
#: one place, every call site goes through ``server_now()``.
_HIGH_WATER = 0.0


def server_now() -> float:
    """THE CLOCK. Every phase, every countdown, every receipt reads this.

    ``max(time.time(), _HIGH_WATER)``. This is ONE function for a reason: a
    clock rule that lives in a comment and is applied at some call sites leaks
    at whichever site was missed, so there is nothing to route around. If you
    need the current time, call this.

    The value returned here is also what every response reports as
    ``server_now``, so the client re-anchors its countdown on the server's
    number rather than running a free-running local timer.
    """
    global _HIGH_WATER
    raw = time.time()
    if raw > _HIGH_WATER:
        _HIGH_WATER = raw
    return _HIGH_WATER


def effective_now(instance: Optional[Dict[str, Any]], now: Optional[float] = None) -> float:
    """The clock, further clamped by one instance's own persisted floor.

    ``max(now, instance["clock_floor"])`` where ``clock_floor`` is the highest
    time the server has ever written for THIS instance. Two layers on purpose:
    the module mark absorbs a rollback inside a window for the whole process,
    and the persisted floor survives a restart -- a process that comes back up
    after a rollback must not re-open an instance it had already seen closed.
    """
    now = server_now() if now is None else float(now)
    if not instance:
        return now
    return max(now, float(instance.get("clock_floor") or 0.0))


def advance_clock_floor(instance: Dict[str, Any], now: float) -> float:
    """Raise an instance's floor to ``now`` if it is ahead. Monotonic by construction."""
    instance["clock_floor"] = max(float(instance.get("clock_floor") or 0.0), float(now))
    return float(instance["clock_floor"])


def utc_day_key(now: Optional[float] = None) -> str:
    """The server's UTC day key, ``YYYY-MM-DD``.

    Derived from the SERVER CLOCK, never from a record. A period key read off
    an instance row is a period key a participant can influence.
    """
    now = server_now() if now is None else float(now)
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def utc_midnight(now: float) -> float:
    """The UTC midnight that begins ``now``'s day."""
    return math.floor(float(now) / 86400.0) * 86400.0


def _parse_utc(value: Any) -> Optional[float]:
    """An ISO-8601 UTC instant as a float, or None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def next_epoch(defn: Dict[str, Any], now: Optional[float] = None) -> float:
    """The UTC anchor of the next occurrence of ``defn``.

    NEVER ``now + duration``. That is the measured defect behind this rule: the
    shipped scheduler set ``start_time = now``, so the same event created at
    three instants on one day got active windows of 01:46Z, 02:46Z and 07:46Z --
    two players in one "event" were not in the same event.

    Three shapes, all anchored:
      daily    the next UTC midnight strictly after ``now``.
      weekly   the next configured instant after ``now`` (or +7 days once it has
               passed).
      once     the configured instant. A past anchor is returned as-is rather
               than silently skipped, so an operator who scheduled a one-off in
               the past gets a closed instance and a visible "already over"
               instead of one that never appears.
    """
    now = server_now() if now is None else float(now)
    recurrence = str(defn.get("recurrence") or "daily")
    if recurrence == "daily":
        return utc_midnight(now) + 86400.0

    opens = defn.get("opens_at_utc")
    epoch = _parse_utc(opens) if opens else utc_midnight(now) + 86400.0
    if epoch is None:
        return utc_midnight(now) + 86400.0
    if recurrence == "weekly":
        while epoch <= now:
            epoch += 7 * 86400.0
        return epoch
    return epoch


def anchor_lattice(defn: Dict[str, Any], epoch_start: float) -> float:
    """The REAL occurrence anchor a caller-named ``epoch_start`` lands on.

    ``epoch_start`` arrives in the request body, and it is one third of the
    payout's primary key. Keyed on the raw float, it is a CALLER-SUPPLIED
    IDEMPOTENCY KEY after all, which is exactly what the module docstring says
    this feature does not have.

    MEASURED on this tree before this function existed: a client that named
    five invented epochs inside one cooldown window was paid five times --
    25.0 progress points where the honest answer is 5.0 -- and 7200 invented
    epochs were accepted at a single instant, none of them the anchor the
    scheduler itself would have chosen.

    So the key is snapped to the definition's own occurrence lattice: UTC
    midnights for a daily event, whole weeks from the configured instant for a
    weekly one, the configured instant alone for a one-off. Every invented
    value in a period collapses onto the same anchor as every other value in
    that period, which makes the once-only key server-derived end to end.

    IDEMPOTENT on a real anchor: snapping an anchor the scheduler produced
    returns it unchanged, so this is safe at every call site, including the ones
    already holding a server-derived value.
    """
    raw = float(epoch_start)
    if not math.isfinite(raw):
        # A non-finite epoch is refused downstream by the phase gate, which is
        # where it has always been refused. Canonicalising it here would raise
        # out of math.floor and turn that 409 into a 500.
        return raw
    recurrence = str(defn.get("recurrence") or "daily")
    if recurrence == "daily":
        return utc_midnight(raw)
    opens = _parse_utc(defn.get("opens_at_utc")) if defn.get("opens_at_utc") else None
    if opens is None:
        return utc_midnight(raw)
    if recurrence == "once":
        return opens
    week = 7.0 * 86400.0
    return opens + math.floor((raw - opens) / week) * week


#: The phase names. Five: three visible, plus a terminal one.
PHASES = ("pending", "warning", "active", "cooldown", "closed")

#: Phases in which a player may freeze their snapshot and fight a slot.
ENTRY_PHASES = ("warning", "active")


def boundaries(defn: Dict[str, Any], epoch_start: float) -> Dict[str, float]:
    """The four boundaries, derived from a UTC ANCHOR.

    Never from ``now + duration``. Stored on the instance so a report can filter
    on it, and recomputed on every write so no query has to trust it.
    """
    warn_at = float(epoch_start)
    active_at = warn_at + float(defn.get("warn_hours") or 0.0) * 3600.0
    cooldown_at = active_at + float(defn.get("active_hours") or 0.0) * 3600.0
    closed_at = cooldown_at + float(defn.get("cooldown_hours") or 0.0) * 3600.0
    return {
        "warn_at": warn_at,
        "active_at": active_at,
        "cooldown_at": cooldown_at,
        "closed_at": closed_at,
    }


def phase_at(bound: Dict[str, float], now: float) -> str:
    """The phase, COMPUTED from the clock over half-open intervals.

    A pure function over ``[at, next)``. A tick that runs late, twice, or not
    at all produces the same answer on the next read -- which an ``if/elif``
    walk toward the phase does not. Measured in this codebase: a scheduler that
    walked toward the phase left an event in ``post_end`` three ticks after it
    should have read ``expired``, so a process restarted after a day reported a
    closed event as still being fought.

    This is a CONTROL, not a label. A stored ``state`` column is a mirror that
    any payout or denial is free to ignore.
    """
    now = float(now)
    if now < float(bound["warn_at"]):
        return "pending"
    if now < float(bound["active_at"]):
        return "warning"
    if now < float(bound["cooldown_at"]):
        return "active"
    if now < float(bound["closed_at"]):
        return "cooldown"
    return "closed"


# ==========================================================================
# Errors. Distinct types so a route maps each to its own status code.
# ==========================================================================

class WorldEventError(Exception):
    """Base. Carries the HTTP status the route should answer."""

    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EventRefused(WorldEventError):
    """409 — well-formed, but the event refuses it (wrong phase, slot skipped)."""

    status = 409


class EventNotFound(WorldEventError):
    """404 — no such instance, or the player never entered it."""

    status = 404


class EventForbidden(WorldEventError):
    """403 — the caller is not the owner of the row they named."""

    status = 403


class EventRateLimited(WorldEventError):
    """429 — too many attempts inside the sliding window."""

    status = 429


# ==========================================================================
# The parameter pack. Data, not code — the same numbers also seed the migration.
# ==========================================================================

#: The default ladder: health and attack as multiples of the PLAYER'S OWN
#: snapshot. Ordered ascending by multiplier, not hand-ordered.
DEFAULT_LADDER: List[Dict[str, Any]] = [
    {"slot": 1, "name": "scout", "health_multiple": 0.55, "attack_multiple": 0.15},
    {"slot": 2, "name": "cull", "health_multiple": 1.10, "attack_multiple": 0.30},
    {"slot": 3, "name": "alpha", "health_multiple": 1.90, "attack_multiple": 0.48},
    {"slot": 4, "name": "matriarch", "health_multiple": 2.90, "attack_multiple": 0.66},
    {"slot": 5, "name": "titan", "health_multiple": 3.40, "attack_multiple": 0.75},
]

#: The four shipped definitions. `recurrence='daily'` anchors to the next UTC
#: midnight, so these need no operator to appear.
DEFAULT_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "def_key": "beast_swarm", "name": "Beast Swarm", "event_type": "surge",
        "warn_hours": 2.0, "active_hours": 24.0, "cooldown_hours": 6.0,
        "recurrence": "daily", "ladder_slots": DEFAULT_LADDER,
        "reward_tiers": [
            {"tier": "participate", "percent": 5.0},
            {"tier": "contribute", "percent": 15.0},
            {"tier": "top", "percent": 30.0},
        ],
    },
    {
        "def_key": "invasion_alert", "name": "Invasion Alert", "event_type": "invasion",
        "warn_hours": 6.0, "active_hours": 48.0, "cooldown_hours": 24.0,
        "recurrence": "daily", "ladder_slots": DEFAULT_LADDER,
        "reward_tiers": [
            {"tier": "participate", "percent": 5.0},
            {"tier": "contribute", "percent": 15.0},
            {"tier": "top", "percent": 30.0},
        ],
    },
    {
        "def_key": "warden_boss", "name": "Warden of the Broken Gate", "event_type": "boss",
        "warn_hours": 12.0, "active_hours": 12.0, "cooldown_hours": 6.0,
        "recurrence": "daily", "ladder_slots": DEFAULT_LADDER,
        "reward_tiers": [
            {"tier": "participate", "percent": 5.0},
            {"tier": "contribute", "percent": 15.0},
            {"tier": "top", "percent": 30.0},
        ],
    },
    {
        "def_key": "moon_festival", "name": "Moon Festival", "event_type": "festival",
        "warn_hours": 6.0, "active_hours": 24.0, "cooldown_hours": 12.0,
        "recurrence": "weekly", "opens_at_utc": "2026-01-01T00:00:00Z",
        # Attendance only. One tier, and a ladder slot that fights nothing: there
        # is no combat, so the attempt route records attendance and returns.
        "ladder_slots": [{"slot": 1, "name": "lantern", "health_multiple": 0.0,
                          "attack_multiple": 0.0}],
        "reward_tiers": [{"tier": "participate", "percent": 5.0}],
    },
]

#: The +-12% per-entry difficulty roll. See the module docstring: this is a
#: reason, not a playtest.
DIFFICULTY_ROLL = 0.12

#: Below this many distinct entrants an instance pays ``participate`` only. A
#: 90th percentile over three players is one player, and paying a top tier to a
#: three-player instance makes the top tier a lottery rather than an
#: achievement. The number is a balance dial, not a measurement.
MIN_TIER_SAMPLE = 8

#: Percentile cut-offs, per instance, never global.
CONTRIBUTE_PERCENTILE = 50.0
TOP_PERCENTILE = 90.0

#: Sliding window on the two state-changing player routes. Enter is naturally
#: limited to once per instance by its own idempotence and attempt to
#: best_slot+1, so the natural ceiling is five per instance anyway; this exists
#: for the abuse case where someone loops `attempt` on a capped instance.
ATTEMPT_RATE_LIMIT = 12
ATTEMPT_RATE_WINDOW = 60.0

#: Partial heal restored between slots. Measured: raising this from 40% to 55%
#: changed the median for exactly one of eighteen builds, so it is not a
#: balance lever at this slot spread. The slot multipliers are the only real
#: dial.
LADDER_HEAL_FRACTION = 0.40


# ==========================================================================
# Storage. PostgreSQL when the store has a connection, memory when it does not.
# ==========================================================================

def _conn(players_db) -> Any:
    """The transaction's own connection, or None in memory-only local mode.

    The CALLABLE check is load-bearing. On ``PlayerStore`` the name
    ``current_connection`` is a METHOD (player_store.py:84), so a plain getattr
    returns a BOUND METHOD -- always truthy -- and a memory-only branch written
    as ``if conn is None`` becomes unreachable. Measured on this tree: every
    write went down the PostgreSQL path in a game that ships memory-only by
    default, and it failed on the first query.

    So: callable means call it, attribute means read it, and only a real
    connection object counts as "a database is present". The store and the ledger
    must never disagree about which mode they are in.
    """
    raw = getattr(players_db, "current_connection", None)
    if callable(raw):
        raw = raw()
    return raw if raw is not None else None


def _is_pg(players_db) -> bool:
    return _conn(players_db) is not None


def assert_world_event_schema(players_db) -> None:
    """Boot assertion: the migration ran.

    Load-bearing because of the KNOWN TREE DEFECT in the migration header --
    server/migrations/ holds three files numbered 010, and the runner keys
    schema_migrations on the version string. On a database where one 010 already
    applied, the others are skipped SILENTLY with a clean boot and no warning.
    The runner reports success without proving a file applied.

    ``to_regclass`` answers with a row instead of raising, so one query checks all
    four tables and the message names which are absent.
    """
    conn = _conn(players_db)
    if conn is None:
        return  # memory-only local mode; nothing to assert
    names = ("world_event_reward_log", "world_event_participation",
             "world_event_instance", "world_event_definition")
    with conn.cursor() as cur:
        cur.execute("SELECT %s" % ", ".join("to_regclass(%s)" for _ in names), tuple(names))
        missing = [n for n, oid in zip(names, cur.fetchone()) if oid is None]
    if missing:
        raise RuntimeError(
            "world event migration did not run; missing tables: %s. Check for a "
            "duplicate migration number in server/migrations/." % ", ".join(missing)
        )


#: Process-local mirrors of the four tables. Same caveat as the ledger: NOT a
#: cross-process guarantee. Replaced by the primary keys whenever PostgreSQL is
#: present.
_MEM_DEFS: Dict[str, Dict[str, Any]] = {}
_MEM_INSTANCES: Dict[Tuple[str, float], Dict[str, Any]] = {}
_MEM_PARTICIPATION: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
_MEM_RECEIPTS: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
#: Per-player sliding window for the attempt route: key -> deque of instants.
_MEM_ATTEMPTS: Dict[str, deque] = {}
_MEM_LIMIT_LOCK = threading.Lock()


def reset_memory_state() -> None:
    """Drop every process-local mirror. A test seam, and a restart's view.

    It resets the SHARED ``ATTEMPT_LIMITER`` too, and that is the point: a test
    seam that leaves one piece of module state behind is a suite that leaks an
    allowance from one test into the next. MEASURED before this line was added
    -- ``_MEM_ATTEMPTS`` was declared here and cleared here and written by
    NOTHING, so the seam cleared an empty dict while the deque that actually
    counts (the limiter's own ``_attempts``) survived, and every caller had to
    remember a second line, ``ATTEMPT_LIMITER.reset()``, that no docstring
    mentioned. Four call sites did. The next one would not have.

    ``ATTEMPT_LIMITER`` is defined later in the file, so it is resolved at call
    time, not at import time -- a ``from __future__`` module whose reset seam
    runs before the limiter exists simply skips it.
    """
    with _LOCK:
        _MEM_DEFS.clear()
        _MEM_INSTANCES.clear()
        _MEM_PARTICIPATION.clear()
        _MEM_RECEIPTS.clear()
    with _MEM_LIMIT_LOCK:
        _MEM_ATTEMPTS.clear()
    limiter = globals().get("ATTEMPT_LIMITER")
    if limiter is not None:
        limiter.reset()


def _jsonb(raw: Any, default: Any) -> Any:
    """A JSONB column read back as text on some drivers, as a dict on others."""
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _ts(epoch: float) -> datetime:
    """A float instant as a timezone-aware datetime, for a TIMESTAMPTZ column.

    MEASURED, and it is the whole reason this helper exists: passing the raw float
    into ``created_at`` gives
    ``DatatypeMismatch: column "created_at" is of type timestamp with time zone but
    expression is of type numeric``. Every other column in these tables stores an
    epoch as a float, so the mismatch is invisible until the first write against a
    real database -- which is exactly where the memory-mode tests cannot reach.

    One conversion, one place. A module that converts at some call sites and not
    others is a module whose next write fails in production.
    """
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------

def default_definition(defn: Dict[str, Any]) -> Dict[str, Any]:
    """A pack row, normalised into the shape the storage layer uses."""
    out = dict(defn)
    out["ladder_slots"] = list(out.get("ladder_slots") or DEFAULT_LADDER)
    out["reward_tiers"] = list(out.get("reward_tiers") or [])
    out["warn_hours"] = float(out.get("warn_hours") or 0.0)
    out["active_hours"] = float(out.get("active_hours") or 24.0)
    out["cooldown_hours"] = float(out.get("cooldown_hours") or 6.0)
    out["recurrence"] = str(out.get("recurrence") or "daily")
    out.setdefault("enabled", True)
    return out


def load_definitions(players_db) -> List[Dict[str, Any]]:
    """Every enabled definition, from the table when there is one, else the pack."""
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT def_key, name, event_type, warn_hours, active_hours,"
                " cooldown_hours, recurrence, opens_at_utc, ladder_slots,"
                " reward_tiers, enabled FROM world_event_definition WHERE enabled"
                " ORDER BY def_key"
            )
            rows = cur.fetchall()
        if rows:
            return [default_definition({
                "def_key": row["def_key"], "name": row["name"],
                "event_type": row["event_type"],
                "warn_hours": float(row["warn_hours"] or 0.0),
                "active_hours": float(row["active_hours"] or 24.0),
                "cooldown_hours": float(row["cooldown_hours"] or 6.0),
                "recurrence": row["recurrence"], "opens_at_utc": row["opens_at_utc"],
                "ladder_slots": _jsonb(row["ladder_slots"], []),
                "reward_tiers": _jsonb(row["reward_tiers"], []),
                "enabled": bool(row["enabled"]),
            }) for row in rows]
    return [default_definition(d) for d in DEFAULT_DEFINITIONS]


def get_definition(players_db, def_key: str) -> Optional[Dict[str, Any]]:
    for row in load_definitions(players_db):
        if row["def_key"] == str(def_key):
            return row
    return None


def upsert_definition(players_db, defn: Dict[str, Any],
                      now: Optional[float] = None) -> Dict[str, Any]:
    """Write one definition. Configuration is a row, never a branch in a handler."""
    now = server_now() if now is None else float(now)
    defn = default_definition(defn)
    if not defn.get("def_key"):
        raise WorldEventError("def_key is required")
    if not defn["ladder_slots"]:
        raise WorldEventError("a definition needs at least one ladder slot")
    if not defn["reward_tiers"]:
        raise WorldEventError("a definition needs at least one reward tier")
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO world_event_definition (def_key, name, event_type,"
                " warn_hours, active_hours, cooldown_hours, recurrence, opens_at_utc,"
                " ladder_slots, reward_tiers, enabled, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (def_key) DO UPDATE SET name = EXCLUDED.name,"
                " event_type = EXCLUDED.event_type, warn_hours = EXCLUDED.warn_hours,"
                " active_hours = EXCLUDED.active_hours,"
                " cooldown_hours = EXCLUDED.cooldown_hours,"
                " recurrence = EXCLUDED.recurrence,"
                " opens_at_utc = EXCLUDED.opens_at_utc,"
                " ladder_slots = EXCLUDED.ladder_slots,"
                " reward_tiers = EXCLUDED.reward_tiers, enabled = EXCLUDED.enabled,"
                " updated_at = EXCLUDED.updated_at",
                (defn["def_key"], defn.get("name") or defn["def_key"],
                 defn.get("event_type") or "surge", defn["warn_hours"],
                 defn["active_hours"], defn["cooldown_hours"], defn["recurrence"],
                 defn.get("opens_at_utc"), json.dumps(defn["ladder_slots"]),
                 json.dumps(defn["reward_tiers"]), bool(defn.get("enabled", True)),
                 _ts(now), _ts(now)),
            )
        return defn
    _MEM_DEFS[defn["def_key"]] = defn
    return defn


# --------------------------------------------------------------------------
# Instances
# --------------------------------------------------------------------------

def _blank_instance(defn: Dict[str, Any], epoch_start: float, now: float) -> Dict[str, Any]:
    bound = boundaries(defn, epoch_start)
    return {
        "def_key": defn["def_key"],
        "epoch_start": float(epoch_start),
        **bound,
        "state": "pending",
        "clock_floor": float(now),
        "entrants": 0,
    }


def _row_to_instance(row: Any) -> Dict[str, Any]:
    keys = ("def_key", "epoch_start", "warn_at", "active_at", "cooldown_at",
            "closed_at", "state", "clock_floor", "entrants")
    return {k: row[k] for k in keys if k in row.keys()}


def get_instance(players_db, def_key: str,
                 epoch_start: float) -> Optional[Dict[str, Any]]:
    """Read one occurrence. PG row, memory dict, or None."""
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM world_event_instance WHERE def_key = %s"
                " AND epoch_start = %s", (str(def_key), float(epoch_start)),
            )
            row = cur.fetchone()
        return _row_to_instance(row) if row is not None else None
    return _MEM_INSTANCES.get((str(def_key), float(epoch_start)))


def save_instance(players_db, instance: Dict[str, Any],
                  now: Optional[float] = None) -> None:
    """Write an occurrence back. Always inside ``players_db.transaction``.

    ``closed_at`` is written ONCE and is deliberately absent from this UPDATE.
    The migration's trigger refuses a change as well; a trigger that is the only
    guard is one migration away from being dropped.
    """
    now = server_now() if now is None else float(now)
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE world_event_instance SET state = %s, clock_floor = %s,"
                " entrants = %s, updated_at = %s WHERE def_key = %s AND epoch_start = %s",
                (instance["state"], float(instance["clock_floor"]),
                 int(instance["entrants"]), _ts(now), instance["def_key"],
                 float(instance["epoch_start"])),
            )
        return
    _MEM_INSTANCES[(instance["def_key"], float(instance["epoch_start"]))] = instance


def ensure_instance(players_db, defn: Dict[str, Any], epoch_start: float,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """Materialise one occurrence, idempotently.

    The natural key (def_key, epoch_start) plus ON CONFLICT DO NOTHING is what
    makes a tick that runs twice, or two server processes, produce ONE row. The
    alternative -- a surrogate id -- would let a double materialisation create
    two real occurrences of one moment, each of which would pay out.
    """
    now = server_now() if now is None else float(now)
    existing = get_instance(players_db, defn["def_key"], epoch_start)
    if existing is not None:
        return existing
    fresh = _blank_instance(defn, epoch_start, now)
    conn = _conn(players_db)
    if conn is not None:
        bound = boundaries(defn, epoch_start)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO world_event_instance (def_key, epoch_start, warn_at,"
                " active_at, cooldown_at, closed_at, state, clock_floor, entrants,"
                " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)"
                " ON CONFLICT (def_key, epoch_start) DO NOTHING",
                (defn["def_key"], float(epoch_start), bound["warn_at"],
                 bound["active_at"], bound["cooldown_at"], bound["closed_at"],
                 # Through the clamp, like every other call site. On a brand-new
                 # instance the floor equals `now` so the two agree, but a
                 # codebase that holds the right rule at five call sites and a
                 # raw `now` at the sixth is a codebase where the sixth is a bug
                 # waiting for the day someone reuses this row.
                 phase_at(bound, effective_now(fresh, now)),
                 _ts(now), _ts(now), _ts(now)),
            )
        again = get_instance(players_db, defn["def_key"], epoch_start)
        return again or fresh
    _MEM_INSTANCES[(defn["def_key"], float(epoch_start))] = fresh
    return fresh


def refresh_instance(players_db, defn: Dict[str, Any], instance: Dict[str, Any],
                     now: Optional[float] = None) -> Dict[str, Any]:
    """Refresh the ``state`` MIRROR and the clock floor. Nothing else.

    The tick does exactly this. It never decides a phase -- ``phase_at`` is
    recomputed on every read -- so a tick that runs late, twice, or not at all
    produces the same answer on the next read. It also re-derives the boundaries
    and refuses to move ``closed_at``, because a duration edited after an
    instance opened must not rewrite its terminal instant.
    """
    now = server_now() if now is None else float(now)
    bound = boundaries(defn, instance["epoch_start"])
    if abs(float(instance.get("closed_at") or 0.0) - bound["closed_at"]) > 0.001:
        logger.warning(
            "world_event_instance %s@%s closed_at is immutable; keeping %s",
            defn["def_key"], instance["epoch_start"], instance.get("closed_at"),
        )
        return instance
    instance["warn_at"] = bound["warn_at"]
    instance["active_at"] = bound["active_at"]
    instance["cooldown_at"] = bound["cooldown_at"]
    instance["state"] = phase_at(instance, effective_now(instance, now))
    advance_clock_floor(instance, now)
    save_instance(players_db, instance, now)
    return instance


def tick(players_db, now: Optional[float] = None) -> Dict[str, Any]:
    """Materialise due occurrences and refresh their mirrors. Nothing else.

    Returns a small summary so a caller can log it.
    """
    now = server_now() if now is None else float(now)
    materialised: List[str] = []
    refreshed = 0
    for defn in load_definitions(players_db):
        if str(defn.get("recurrence")) == "once":
            # A one-off is materialised by its operator route, not by a clock.
            continue
        epoch = next_epoch(defn, now)
        for candidate in (epoch, epoch - 86400.0):
            existing = get_instance(players_db, defn["def_key"], candidate)
            if existing is None:
                ensure_instance(players_db, defn, candidate, now)
                materialised.append("%s@%d" % (defn["def_key"], int(candidate)))
            else:
                refresh_instance(players_db, defn, existing, now)
            refreshed += 1
    return {"materialised": materialised, "refreshed": refreshed, "server_now": now}


def live_instances(players_db, now: Optional[float] = None,
                   limit: int = 3) -> List[Dict[str, Any]]:
    """Open occurrences, NEAREST BOUNDARY FIRST, capped.

    The cap is deliberate: two definitions can be live at once by design, and a
    third is a scheduling mistake the cap should hide rather than render.
    """
    now = server_now() if now is None else float(now)
    defs = {d["def_key"]: d for d in load_definitions(players_db)}
    rows: List[Dict[str, Any]] = []

    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM world_event_instance WHERE closed_at > %s"
                " ORDER BY closed_at ASC", (now,),
            )
            rows = [_row_to_instance(row) for row in cur.fetchall()]
    else:
        rows = [inst for inst in _MEM_INSTANCES.values()
                if float(inst.get("closed_at") or 0.0) > now]

    out: List[Dict[str, Any]] = []
    for instance in sorted(rows, key=lambda r: float(r.get("closed_at") or 0.0)):
        defn = defs.get(instance["def_key"])
        if defn is None:
            continue
        instance = refresh_instance(players_db, defn, instance, now)
        out.append({
            "def_key": instance["def_key"],
            "name": defn.get("name") or instance["def_key"],
            "event_type": defn.get("event_type") or "surge",
            "epoch_start": float(instance["epoch_start"]),
            "phase": phase_at(instance, effective_now(instance, now)),
            "warn_at": float(instance["warn_at"]),
            "active_at": float(instance["active_at"]),
            "cooldown_at": float(instance["cooldown_at"]),
            "closed_at": float(instance["closed_at"]),
        })
        if len(out) >= max(1, int(limit)):
            break
    return out

# --------------------------------------------------------------------------
# Participation rows. One per player per instance. The anti-grief structure.
# --------------------------------------------------------------------------

_PARTICIPATION_COLUMNS = (
    "def_key", "epoch_start", "player_id", "snapshot_power", "snapshot_rate",
    "difficulty_roll", "best_slot", "contribution", "tier", "claimed_at",
    "claim_receipt", "entered_at", "updated_at",
)


def _blank_participation(def_key: str, epoch_start: float, player_id: str,
                         snapshot_power: float, snapshot_rate: float,
                         roll: float, now: float) -> Dict[str, Any]:
    return {
        "def_key": def_key, "epoch_start": float(epoch_start),
        "player_id": player_id,
        "snapshot_power": float(snapshot_power),
        "snapshot_rate": float(snapshot_rate),
        "difficulty_roll": float(roll),
        "best_slot": 0, "contribution": 0.0, "tier": None,
        "claimed_at": None, "claim_receipt": None,
        "entered_at": float(now), "updated_at": float(now),
    }


def _row_to_participation(row: Any) -> Dict[str, Any]:
    out = {k: row[k] for k in _PARTICIPATION_COLUMNS if k in row.keys()}
    out["claim_receipt"] = _jsonb(row.get("claim_receipt")
                                  if hasattr(row, "get") else None, None)
    return out


def get_participation(players_db, def_key: str, epoch_start: float,
                      player_id: str) -> Optional[Dict[str, Any]]:
    """Read one player's row for one occurrence. None means never entered."""
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM world_event_participation WHERE def_key = %s"
                " AND epoch_start = %s AND player_id = %s",
                (str(def_key), float(epoch_start), str(player_id)),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_participation(row)
    return _MEM_PARTICIPATION.get((str(def_key), float(epoch_start), str(player_id)))


def save_participation(players_db, part: Dict[str, Any],
                       now: Optional[float] = None) -> None:
    """Write a participation row back. Always inside ``players_db.transaction``."""
    now = server_now() if now is None else float(now)
    part["updated_at"] = float(now)
    conn = _conn(players_db)
    if conn is not None:
        receipt = part.get("claim_receipt")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE world_event_participation SET snapshot_power = %s,"
                " snapshot_rate = %s, difficulty_roll = %s, best_slot = %s,"
                " contribution = %s, tier = %s, claimed_at = %s, claim_receipt = %s,"
                " updated_at = %s WHERE def_key = %s AND epoch_start = %s"
                " AND player_id = %s",
                (float(part["snapshot_power"]), float(part["snapshot_rate"]),
                 float(part["difficulty_roll"]), int(part["best_slot"]),
                 float(part["contribution"]), part.get("tier"),
                 part.get("claimed_at"),
                 json.dumps(receipt) if receipt is not None else None,
                 part["updated_at"], part["def_key"], float(part["epoch_start"]),
                 part["player_id"]),
            )
        return
    _MEM_PARTICIPATION[(part["def_key"], float(part["epoch_start"]),
                        part["player_id"])] = part


def instance_entrants(players_db, def_key: str, epoch_start: float) -> int:
    """How many DISTINCT players entered this occurrence. Server's own count.

    The tier resolver reads this for the minimum-sample guard. No route accepts a
    claim of how many people turned up.
    """
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT player_id) AS n FROM world_event_participation"
                " WHERE def_key = %s AND epoch_start = %s",
                (str(def_key), float(epoch_start)),
            )
            row = cur.fetchone()
        return int(row["n"] if row else 0)
    return sum(1 for k in _MEM_PARTICIPATION
               if k[0] == str(def_key) and k[1] == float(epoch_start))


def contributions(players_db, def_key: str,
                  epoch_start: float) -> List[float]:
    """Every entrant's contribution for ONE occurrence, ascending.

    Per-instance, never global. A quiet instance and a busy one each have their
    own 50th and 90th percentile; a global percentile means one player's bad
    morning decides what a hundred other players earn, which is the shared-pool
    bug wearing a percentile's clothes.
    """
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT contribution FROM world_event_participation WHERE def_key = %s"
                " AND epoch_start = %s ORDER BY contribution ASC",
                (str(def_key), float(epoch_start)),
            )
            return [float(row["contribution"] or 0.0) for row in cur.fetchall()]
    return sorted(
        float(p.get("contribution") or 0.0)
        for k, p in _MEM_PARTICIPATION.items()
        if k[0] == str(def_key) and k[1] == float(epoch_start)
    )


def percentile_of(values: List[float], value: float, pct: float) -> bool:
    """Is ``value`` at or above the ``pct``-th percentile of ``values``?

    Nearest-rank on the sorted list, with a floor of one player so an empty
    instance never hands out a top tier to nobody.
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return False
    rank = max(1, int(math.ceil((float(pct) / 100.0) * len(ordered))))
    return float(value) >= ordered[min(rank, len(ordered)) - 1]


# ==========================================================================
# The frozen snapshot. Server-read once, at first entry.
# ==========================================================================

def player_snapshot(players_db, player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's own power and hourly rate, read ONCE and stored on their row.

    The power anchor is the shipped ``combat_config.calculate_realm_multiplier``
    -- the same function the rest of the game uses, so an event is exactly as
    hard as the player standing in front of it.

    MEASURED TRAP, and the reason ``assert_realm_multipliers_resolve`` exists:
    ``cultivation_system.Realm`` carries an ``immortal`` member the realm tables
    in combat_config and cultivation_config do not have, and
    ``calculate_realm_multiplier`` returns a silent ``1.0`` for a name it does
    not know. A silent 1.0 flattens every event to a fixed wall. The assertion
    fails the mount rather than shipping that.
    """
    realm = str(player.get("cultivation_realm") or "mortal")
    stage = str(player.get("cultivation_stage") or "early")
    power = float(combat_config.calculate_realm_multiplier(realm, stage))
    stage_cfg = cultivation_config.get_stage(realm, stage)
    rate = float(getattr(stage_cfg, "cultivation_rate_per_hour", 0.0) or 0.0)
    return {"power": max(power, 0.01), "rate": max(rate, 0.0),
            "realm": realm, "stage": stage}


def assert_realm_multipliers_resolve() -> Dict[str, Any]:
    """Every configured realm must be a NAME the power anchor knows, or refuse to mount.

    Load-bearing, and cheap. The trap is specific and it is a silent one:
    ``calculate_realm_multiplier`` is
    ``REALM_POWER_MULTIPLIER.get(realm, 1.0) * STAGE_MULTIPLIER.get(stage, 1.0)``,
    so a realm name the table does not have returns 1.0 with no error. And 1.0 is
    a LEGAL value here -- ``mortal/early`` really is 1.0, it is the baseline every
    other realm is measured against. So "the multiplier is above 1.0" is the
    wrong test and would reject the baseline.

    The right test is the one that catches the actual defect: every configured
    realm and stage must be a KEY in the anchor's tables. A name that is missing
    is the silent 1.0 that flattens every event in the game to a fixed wall.
    """
    out: Dict[str, float] = {}
    unknown: List[str] = []
    realms = {r.name for r in cultivation_config.REALMS}
    stages = {s.name for r in cultivation_config.REALMS for s in r.stages}
    for name in sorted(realms):
        if name not in combat_config.REALM_POWER_MULTIPLIER:
            unknown.append("realm %s" % name)
    for name in sorted(stages):
        if name not in combat_config.STAGE_MULTIPLIER:
            unknown.append("stage %s" % name)
    for realm_cfg in cultivation_config.REALMS:
        for stage in realm_cfg.stages:
            out["%s/%s" % (realm_cfg.name, stage.name)] = float(
                combat_config.calculate_realm_multiplier(realm_cfg.name, stage.name))
    if unknown:
        raise RuntimeError(
            "combat_config does not know these configured names: %s. The power "
            "anchor returns a silent 1.0 for an unknown name, and a silent 1.0 "
            "flattens every world event to a fixed wall." % ", ".join(unknown)
        )
    return out


def roll_difficulty(seed_parts: Tuple[Any, ...]) -> float:
    """The per-entry +-12% roll, seeded from the entry's OWN identity.

    Seeded rather than free so a client cannot re-roll a bad entry by retrying,
    and so two servers resolve the same entry to the same number.
    """
    rng = random.Random(":".join(str(p) for p in seed_parts))
    return round(1.0 + rng.uniform(-DIFFICULTY_ROLL, DIFFICULTY_ROLL), 6)


# ==========================================================================
# The four mechanics. Shared shape, different measurement.
# ==========================================================================

def _stat_line(players_db, part: Dict[str, Any], player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's OWN combat line for the shipped auto-battle.

    Read from the player's row at slot time for the CURRENT numbers, but scaled
    by the FROZEN power multiplier rather than their live one. That is the point
    of the snapshot: a player who cultivates at 3am does not make their own event
    trivial halfway through.
    """
    realm = str(player.get("cultivation_realm") or "mortal")
    stage = str(player.get("cultivation_stage") or "early")
    live = float(combat_config.calculate_realm_multiplier(realm, stage) or 1.0)
    scale = float(part["snapshot_power"]) / max(live, 0.0001)
    return {
        "hp": max(int(float(player.get("hp") or 100)), 1),
        "atk": max(int(float(player.get("atk") or 10)), 1),
        "def": max(int(float(player.get("def_") or 0)), 1),
        "realm": realm,
        "stage": stage,
        "element": str(player.get("spirit_root") or "fire"),
        # Folded into the two numbers the simulator reads rather than into the
        # realm name, so the simulator's own realm lookup stays the single place
        # that decides power.
        "snapshot_scale": scale,
    }


def _enemy_for(part: Dict[str, Any], slot: Dict[str, Any],
               stats: Dict[str, Any]) -> Dict[str, Any]:
    """One slot's enemy, as multiples of the player's OWN snapshot."""
    roll = float(part.get("difficulty_roll") or 1.0)
    scale = float(stats.get("snapshot_scale") or 1.0)
    hp = max(int(round(float(stats["hp"]) * scale
                       * float(slot.get("health_multiple") or 0.0) * roll)), 1)
    atk = max(int(round(float(stats["atk"]) * scale
                        * float(slot.get("attack_multiple") or 0.0) * roll)), 1)
    return {
        "hp": hp, "atk": atk,
        # The enemy's defence is the player's own, scaled. An enemy that hardens
        # against the player who is already hardest is the only way "as hard as
        # you are" stays true across realms.
        "def": max(int(round(float(stats["def"]) * scale * 0.8)), 1),
        "realm": stats["realm"], "stage": stats["stage"],
        "element": "earth",
    }


def resolve_slot(players_db, defn: Dict[str, Any], part: Dict[str, Any],
                 player: Dict[str, Any], slot_number: int,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """Fight one ladder slot with the SHIPPED combat function. Returns the outcome.

    The client sends an INTENT -- which slot it is trying -- and never a result.
    Everything below is re-derived here: the slot's multipliers from the
    definition, the difficulty roll from the entry's identity, and the fight
    itself from ``combat_config.calculate_auto_battle``. That is what stops a
    modified client from reporting a win.

    A festival has no fight: its single slot records attendance and returns. That
    is not a special case bolted on, it is the definition's own empty multipliers
    saying so.
    """
    now = server_now() if now is None else float(now)
    ladder = list(defn.get("ladder_slots") or [])
    chosen = next((s for s in ladder if int(s.get("slot")) == int(slot_number)), None)
    if chosen is None:
        raise EventRefused("This event has no slot %d" % int(slot_number))

    stats = _stat_line(players_db, part, player)
    health_mult = float(chosen.get("health_multiple") or 0.0)
    if health_mult <= 0.0:
        # Attendance-only event. Nothing to fight, and nothing to fake.
        return {"slot": int(slot_number), "name": chosen.get("name") or "attendance",
                "cleared": True, "winner": None, "turns": 0,
                "player_hp_remaining": int(stats["hp"]),
                "enemy_hp_remaining": 0, "damage_dealt": 0,
                "server_now": now}

    enemy = _enemy_for(part, chosen, stats)
    seed = int(hashlib_seed(part["def_key"], part["epoch_start"], part["player_id"],
                            slot_number))
    result = combat_config.calculate_auto_battle(
        {k: v for k, v in stats.items() if k != "snapshot_scale"},
        enemy, seed=seed,
    )
    cleared = result.get("winner") == "player"
    dealt = sum(int(turn.get("damage") or 0) for turn in result.get("log") or [])
    return {
        "slot": int(slot_number),
        "name": chosen.get("name") or ("slot %d" % slot_number),
        "cleared": bool(cleared),
        "winner": result.get("winner"),
        "turns": int(result.get("turns") or 0),
        "player_hp_remaining": int(result.get("player_hp_remaining") or 0),
        "enemy_hp_remaining": int(result.get("enemy_hp_remaining") or 0),
        "damage_dealt": int(dealt),
        "server_now": now,
    }


def hashlib_seed(*parts: Any) -> int:
    """A STABLE integer seed from an entry's identity.

    Python's built-in ``hash()`` is salted per process for strings, so two
    servers would resolve the same entry to different fights. This one is not.
    """
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8"))
    return int.from_bytes(digest.digest()[:8], "big", signed=False)


def contribution_for(defn: Dict[str, Any], part: Dict[str, Any],
                     damage_dealt: int, slots_total: int) -> float:
    """This player's normalised contribution, 0..1. Server-measured.

    Four event types, four measurements, and the difference is the point:

      festival   Attendance is the whole event, so entering is contributing.
                 Fixed at 1.0.
      boss       DAMAGE. A careful player who lands one big hit and a reckless
                 one who chips for twenty minutes reach comparable numbers,
                 which is what a boss should reward.
      invasion   SLOT DEPTH. An invasion you return to twice is worth more than
                 one you beat in a sitting, so depth beats damage here.
      surge      Slot depth as a fraction of the ladder -- the daily rhythm, and
                 the shallowest measurement on purpose.
    """
    event_type = str(defn.get("event_type") or "surge")
    total = max(1, int(slots_total))
    depth = max(0, min(int(part.get("best_slot") or 0), total)) / float(total)
    if event_type == "festival":
        return 1.0
    if event_type == "boss":
        # Damage, normalised against the reference: a player who clears the
        # whole ladder is the 1.0, so the ceiling is reachable and not a guess.
        dealt = max(0.0, float(damage_dealt or 0))
        reference = max(1.0, float(part.get("snapshot_power") or 1.0) * 100.0 * total)
        return max(0.0, min(1.0, dealt / reference))
    return max(0.0, min(1.0, depth))


# ==========================================================================
# The rate limiter. Per player, sliding window, keyed by the SESSION.
# ==========================================================================

class AttemptRateLimiter:
    """Per-player sliding-window limiter for the state-changing routes.

    Shaped after ``social_policy.MessageRateLimiter``: a deque of attempt
    instants per key, lazy eviction, and a 429 carrying ``Retry-After``.

    The key is the PLAYER id, never the network address. Throttling by address
    would let one player reset their allowance by changing address, while
    punishing every player behind a shared address for one player's behaviour.
    """

    def __init__(self, limit: int = ATTEMPT_RATE_LIMIT,
                 window_seconds: float = ATTEMPT_RATE_WINDOW,
                 clock=time.monotonic) -> None:
        self._limit = max(1, int(limit))
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: Dict[str, deque] = {}

    def check(self, player_id: str) -> None:
        """Count one attempt; 429 when over the cap. Call before doing the work."""
        if not player_id:
            raise EventForbidden("A session is required")
        now = float(self._clock())
        with self._lock:
            bucket = self._attempts.get(player_id)
            if bucket is None:
                bucket = deque()
                self._attempts[player_id] = bucket
            while bucket and now - bucket[0] >= self._window:
                bucket.popleft()
            if len(bucket) >= self._limit:
                retry_after = max(1, int(math.ceil(self._window - (now - bucket[0]))))
                raise EventRateLimited(
                    "Too many event actions. Try again in %ds." % retry_after)
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


#: One shared limiter for the module, so two routes cannot each hold their own
#: allowance for the same player.
ATTEMPT_LIMITER = AttemptRateLimiter()


# ==========================================================================
# Enter. Freeze the snapshot, once.
# ==========================================================================

def resolve_instance(players_db, def_key: str, epoch_start: Optional[float] = None,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """The instance a def_key/epoch names, materialising it if the clock says so.

    ``epoch_start`` is an INTENT (which occurrence the screen is showing), never
    a start time: the boundaries are derived from it plus the definition, so a
    caller cannot choose a window. The materialisation is idempotent.
    """
    now = server_now() if now is None else float(now)
    defn = get_definition(players_db, def_key)
    if defn is None:
        raise EventNotFound("No world event called %r" % def_key)
    if epoch_start is None:
        epoch_start = next_epoch(defn, now)
    instance = ensure_instance(players_db, defn, float(epoch_start), now)
    return refresh_instance(players_db, defn, instance, now)


def enter_event(players_db, def_key: str, epoch_start: float, player: Dict[str, Any],
                now: Optional[float] = None) -> Dict[str, Any]:
    """Freeze this player's snapshot for this occurrence. IDEMPOTENT.

    Four checks, in this order, and each one refuses rather than pays:

    1. Phase, RECOMPUTED from the clock. ``warning`` and ``active`` both admit;
       ``pending``, ``cooldown`` and ``closed`` refuse.
    2. The player exists. A caller acting for nobody gets 404.
    3. Idempotence. An existing row is returned UNCHANGED. A second call is not
       an error and does not re-freeze -- that is what makes a double-tap
       harmless, and it is also what stops a player re-rolling the +-12% roll by
       re-entering.
    4. The snapshot is read from the server's own player row, once.
    """
    now = server_now() if now is None else float(now)
    defn = get_definition(players_db, def_key)
    if defn is None:
        raise EventNotFound("No world event called %r" % def_key)
    instance = resolve_instance(players_db, def_key, epoch_start, now)
    phase = phase_at(instance, effective_now(instance, now))
    if phase not in ENTRY_PHASES:
        raise EventRefused(
            "This event is %s. You can only join during the warning or the "
            "event itself." % phase)

    player_id = str(player.get("id") or player.get("player_id") or "")
    if not player_id:
        raise EventNotFound("Player not found")

    with _LOCK:
        with players_db.transaction():
            existing = get_participation(players_db, def_key, float(epoch_start), player_id)
            if existing is not None:
                # Idempotent: returned unchanged, with the flag that says so.
                return {"ok": True, "already_entered": True, "phase": phase,
                        "participation": _public_participation(existing),
                        "server_now": now}

            snap = player_snapshot(players_db, player)
            roll = roll_difficulty((def_key, epoch_start, player_id))
            fresh = _blank_participation(def_key, float(epoch_start), player_id,
                                         snap["power"], snap["rate"], roll, now)
            conn = _conn(players_db)
            if conn is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO world_event_participation (def_key, epoch_start,"
                        " player_id, snapshot_power, snapshot_rate, difficulty_roll,"
                        " best_slot, contribution, entered_at, updated_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,0,0.0,%s,%s)"
                        " ON CONFLICT (def_key, epoch_start, player_id) DO NOTHING",
                        (def_key, float(epoch_start), player_id, snap["power"],
                         snap["rate"], roll, now, now),
                    )
                row = get_participation(players_db, def_key, float(epoch_start), player_id)
                part = row or fresh
            else:
                _MEM_PARTICIPATION[(def_key, float(epoch_start), player_id)] = fresh
                part = fresh

            # The entrant count is the server's own, maintained here because this
            # is the only place a player becomes an entrant.
            instance["entrants"] = instance_entrants(players_db, def_key, float(epoch_start))
            advance_clock_floor(instance, now)
            save_instance(players_db, instance, now)

            return {"ok": True, "already_entered": False, "phase": phase,
                    "participation": _public_participation(part),
                    "server_now": now}


# ==========================================================================
# Attempt. Advance the ladder by exactly one slot.
# ==========================================================================

def attempt_slot(players_db, def_key: str, epoch_start: float, player: Dict[str, Any],
                 slot: int, now: Optional[float] = None) -> Dict[str, Any]:
    """Fight one slot. The only route that runs the combat function.

    Rules, and each of them is a refusal rather than a silent adjustment:

    * The phase must be ``active`` (or ``warning`` for a festival's attendance
      slot, which has nothing to fight and therefore nothing to lose).
    * A participation row must exist. No row, no fight.
    * ``slot`` must be exactly ``best_slot + 1``. No skipping, no replaying a
      cleared slot.
    * A LOSS still returns the whole ladder. A player is denied the reward, never
      the play -- the house rule, and the reason there is no shared pool column.
    """
    now = server_now() if now is None else float(now)
    # MEASURED DEFECT, fixed here. This read was ``player.get("id")`` alone while
    # every other entry point in the module -- enter_event, settle_event,
    # instance_view -- reads ``id or player_id``. A player row that carries
    # ``player_id`` and not ``id`` therefore entered the event fine and was then
    # refused its own first fight with a 403 "A session is required": the row was
    # authenticated and joined, so the message was a lie. Measured on this tree
    # before the fix: enter -> OK, attempt -> 403 EventForbidden. The limiter
    # still refuses an EMPTY id, so the guard it exists for is intact.
    ATTEMPT_LIMITER.check(str(player.get("id") or player.get("player_id") or ""))

    defn = get_definition(players_db, def_key)
    if defn is None:
        raise EventNotFound("No world event called %r" % def_key)
    instance = resolve_instance(players_db, def_key, epoch_start, now)
    phase = phase_at(instance, effective_now(instance, now))
    ladder = list(defn.get("ladder_slots") or [])
    player_id = str(player.get("id") or player.get("player_id") or "")

    part = get_participation(players_db, def_key, float(epoch_start), player_id)
    if part is None:
        raise EventNotFound("You have not joined this event")
    if part.get("claimed_at") is not None:
        raise EventRefused("You have already claimed this event")
    if phase not in ENTRY_PHASES:
        raise EventRefused("This event is %s and is not being fought." % phase)

    slot = int(slot)
    if slot < 1 or slot > len(ladder):
        raise EventRefused("This event has %d slots." % len(ladder))
    best = int(part.get("best_slot") or 0)
    if slot != best + 1:
        if slot <= best:
            raise EventRefused("Slot %d is already behind you (you are at %d)."
                               % (slot, best))
        raise EventRefused("You cannot skip to slot %d. Next is %d." % (slot, best + 1))

    with _LOCK:
        outcome = resolve_slot(players_db, defn, part, player, slot, now)
        if outcome.get("cleared"):
            part["best_slot"] = max(best, slot)
        # Contribution is recomputed from what the server just measured. A
        # cleared slot carries a reference damage allowance so depth and damage
        # stay comparable across event types.
        part["contribution"] = contribution_for(
            defn, part, int(outcome.get("damage_dealt") or 0) + best * 100, len(ladder))
        save_participation(players_db, part, now)
        advance_clock_floor(instance, now)
        save_instance(players_db, instance, now)

        return {"ok": True, "phase": phase, "outcome": outcome,
                "participation": _public_participation(part),
                "ladder": ladder_view(defn, part), "server_now": now}


# ==========================================================================
# The ledger. THE ONCE-ONLY GUARD.
# ==========================================================================

def ledger_key(def_key: str, epoch_start: float,
               player_id: str) -> Tuple[str, float, str]:
    """The payout's natural identity, as the THREE parts of the primary key.

    Server-derived end to end: the definition and anchor from the server's own
    scheduler, the player from the session. There is no caller-supplied key
    column anywhere in this feature.
    """
    return (str(def_key), float(epoch_start), str(player_id))


def claim_payout(players_db, key: Tuple[str, float, str],
                 receipt: Dict[str, Any], now: float) -> bool:
    """Write the once-only receipt. TRUE means first claim -- now pay.

    The insert happens BEFORE the money moves and rides the SAME connection and
    the SAME commit as any credit that follows:

    * the credit fails -> the transaction rolls back, the receipt vanishes, and
      the payout is still claimable next time;
    * both commit       -> both landed.

    There is no SELECT before the write, so there is no window between "have I
    paid this?" and "pay this". The opposite order (mark, then pay) makes the
    payout unbounded under concurrency: two callers both mark, both see success,
    both pay.
    """
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO world_event_reward_log (def_key, epoch_start, player_id,"
                " progress_points, player_gate, tier, receipt, paid_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (def_key, epoch_start, player_id) DO NOTHING"
                " RETURNING player_id",
                (key[0], key[1], key[2], float(receipt["progress_points"]),
                 float(receipt["player_gate"]), receipt["tier"],
                 json.dumps(receipt), float(now)),
            )
            return cur.fetchone() is not None
    if key in _MEM_RECEIPTS:
        return False
    _MEM_RECEIPTS[key] = receipt
    return True


def read_receipt(players_db, key: Tuple[str, float, str]) -> Optional[Dict[str, Any]]:
    """The FIRST attempt's stored answer, for a replay.

    A client that timed out after a successful payout needs to learn what it
    already received. A synthesised answer is a guess about somebody's progress.
    """
    conn = _conn(players_db)
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT receipt FROM world_event_reward_log WHERE def_key = %s"
                " AND epoch_start = %s AND player_id = %s", key,
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _jsonb(row["receipt"], None)
    return _MEM_RECEIPTS.get(key)


# ==========================================================================
# Settlement. Tier resolution and the one payout.
# ==========================================================================

def player_gate(player: Dict[str, Any]) -> float:
    """The progress THIS player personally needs for their next breakthrough.

    Read from the shipped ``cultivation_config`` rather than hardcoded to 100, so
    a gate retune moves the reward with it instead of silently paying the wrong
    fraction. Measured at 100.0 points per stage on 2026-10-01.
    """
    realm = str(player.get("cultivation_realm") or "mortal")
    stage = str(player.get("cultivation_stage") or "early")
    cfg = cultivation_config.get_stage(realm, stage)
    return float(getattr(cfg, "progress_required", 100.0) or 100.0)


def resolve_tier(players_db, defn: Dict[str, Any], instance: Dict[str, Any],
                 part: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
    """This player's tier for this occurrence, or None when they get nothing.

    Three rules, and the second is the one a reviewer should argue with:

    1. ``participate`` needs one cleared slot (or an entered festival).
    2. ``contribute`` and ``top`` need a MINIMUM SAMPLE. Below
       ``MIN_TIER_SAMPLE`` distinct entrants the instance pays ``participate``
       only: a 90th percentile over three players is one player, and paying a top
       tier to a three-player instance makes the top tier a lottery rather than
       an achievement. The number is a balance dial with no measurement behind
       it, and this is where a balance pass should argue.
    3. Percentiles are PER INSTANCE. A quiet instance and a busy one each have
       their own cut-offs.
    """
    now = server_now() if now is None else float(now)
    if str(defn.get("event_type")) == "festival":
        # Attendance only, so participation has no slot to clear.
        return "participate" if int(part.get("best_slot") or 0) >= 1 else None
    if int(part.get("best_slot") or 0) < 1:
        return None

    entrants = instance_entrants(players_db, instance["def_key"], instance["epoch_start"])
    if entrants < MIN_TIER_SAMPLE:
        return "participate"

    field = contributions(players_db, instance["def_key"], instance["epoch_start"])
    value = float(part.get("contribution") or 0.0)
    if percentile_of(field, value, TOP_PERCENTILE):
        return "top"
    if percentile_of(field, value, CONTRIBUTE_PERCENTILE):
        return "contribute"
    return "participate"


def tier_percent(defn: Dict[str, Any], tier: str) -> float:
    """The fraction of the player's own gate this tier pays. Data, not code."""
    for row in defn.get("reward_tiers") or []:
        if str(row.get("tier")) == str(tier):
            return float(row.get("percent") or 0.0)
    return 0.0


def settle_event(players_db, def_key: str, epoch_start: float,
                 player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Claim this occurrence's reward. ONCE, EVER, per player.

    Seven checks, in an order chosen so the cheap refusals do not open a
    transaction:

    1. The player exists.
    2. A participation row exists from ``enter``. No row, no claim -- you cannot
       claim an event you never entered, which is what stops a script that claims
       without playing.
    3. The phase is RECOMPUTED from the clock. ``cooldown`` only. The stored
       ``state`` mirror is never read: that is the difference between a label and
       a control.
    4. The background guard -- a dead or decaying player is not paid. Reused from
       ``event_rewards`` unchanged.
    5. ``claimed_at`` is null. Non-null returns the stored receipt with
       ``already_claimed`` true and pays nothing.
    6. Tier resolution from the player's OWN row.
    7. The payout, through the ledger insert first and the credit second, in one
       transaction.

    REFUSE WHOLE OR PAY WHOLE. If any part of the credit fails, the whole
    transaction rolls back and the answer is a 409. A half-paid tier is a bug
    report with a screenshot.
    """
    from event_rewards import background_guard

    now = server_now() if now is None else float(now)
    player_id = str(player.get("id") or player.get("player_id") or "")
    if not player_id:
        raise EventNotFound("Player not found")

    defn = get_definition(players_db, def_key)
    if defn is None:
        raise EventNotFound("No world event called %r" % def_key)
    instance = resolve_instance(players_db, def_key, epoch_start, now)
    phase = phase_at(instance, effective_now(instance, now))
    # MEASURED DEFECT, fixed here. The phase gate ran BEFORE the ledger read, so
    # the promise in the docstring ("the ledger is consulted FIRST ... even when
    # the phase or the tier has since changed") was false for every caller whose
    # retry landed after the payout window. Measured: claim in cooldown paid
    # 5.0 points, then the same call at closed_at+60 answered 409 "Rewards open
    # when the event closes, not while it is closed." -- so a client that timed
    # out and retried minutes later was told it had never been paid, for money
    # it HAD received. The money was never double-paid (progress stayed 5.0), so
    # this was a lie about a receipt rather than a mint, but it is the exact
    # answer the once-only design exists to avoid synthesising.
    #
    # The fix reads the ledger BEFORE the gate, so a claim that already paid
    # always answers with its stored receipt whatever the clock now says. A
    # claim that has NOT paid still faces the same phase gate it always did.
    #
    # The anchor is the SERVER'S occurrence, not the float the body carried. See
    # anchor_lattice: the raw epoch is one third of this key, so keying on it
    # made the once-only guard a caller-chosen key and every invented value paid.
    anchor = anchor_lattice(defn, epoch_start)
    already_paid = read_receipt(players_db, ledger_key(def_key, anchor,
                                                       player_id))
    if already_paid is None and phase != "cooldown":
        raise EventRefused(
            "Rewards open while the event is in cooldown, and the window for "
            "%s closed. It is %s now." % (def_key, phase))

    with _LOCK:
        with players_db.transaction():
            part = get_participation(players_db, def_key, float(epoch_start), player_id)
            if part is None:
                raise EventNotFound("You did not join this event")

            key = ledger_key(def_key, anchor, player_id)
            # The ledger is consulted FIRST. A claim that already paid must get
            # its real receipt back even when the phase or the tier has since
            # changed -- a bare "already done" is a guess about their progress.
            existing = read_receipt(players_db, key)
            if existing is not None or part.get("claimed_at") is not None:
                stored = existing or _jsonb(part.get("claim_receipt"), None) or {}
                part["claimed_at"] = part.get("claimed_at") or now
                part["claim_receipt"] = part.get("claim_receipt") or stored
                save_participation(players_db, part, now)
                return {"ok": True, "already_claimed": True,
                        "receipt": stored, "phase": phase, "server_now": now}

            denied = background_guard(players_db, player_id)
            if denied is not None:
                raise EventRefused(str(denied.get("error") or "you cannot be paid now"))

            tier = resolve_tier(players_db, defn, instance, part, now)
            if tier is None:
                raise EventRefused("You did not clear a slot, so there is nothing to claim.")
            gate = player_gate(player)
            points = round(gate * (tier_percent(defn, tier) / 100.0), 4)
            if points <= 0:
                # A zero-value payout MUST NOT burn the ledger key: that would
                # deny the real payment that follows it.
                raise EventRefused("This tier pays nothing and cannot be claimed.")

            receipt = {
                "ok": True, "def_key": def_key, "epoch_start": float(epoch_start),
                "player_id": player_id, "tier": tier,
                "progress_points": points, "player_gate": gate,
                "percent_of_own_gate": tier_percent(defn, tier),
                "best_slot": int(part.get("best_slot") or 0),
                "contribution": round(float(part.get("contribution") or 0.0), 6),
                "entrants": instance_entrants(players_db, def_key, float(epoch_start)),
                "paid_at": now,
            }

            # THE INSERT IS THE DECISION. One row back means this is the first
            # claim; zero rows means a concurrent caller won it.
            if not claim_payout(players_db, key, receipt, now):
                stored = read_receipt(players_db, key) or receipt
                return {"ok": True, "already_claimed": True, "receipt": stored,
                        "phase": phase, "server_now": now}

            # The credit. Routed through qi_rewards.grant_qi so it lands on
            # `progress`, the field the breakthrough gate actually reads --
            # crediting `qi` alone made every reward on this tree decorative.
            fresh = players_db.get(player_id)
            if fresh is None:
                raise EventNotFound("Player not found")
            before = float(fresh.get("progress") or 0.0)
            grant_qi(fresh, points)
            receipt["progress_before"] = round(before, 4)
            receipt["progress_after"] = round(float(fresh.get("progress") or 0.0), 4)

            # Store the receipt back on the participation row too, so the
            # once-only marker and the human-readable answer travel together.
            part["tier"] = tier
            part["claimed_at"] = now
            part["claim_receipt"] = receipt
            save_participation(players_db, part, now)
            advance_clock_floor(instance, now)
            save_instance(players_db, instance, now)

            return {"ok": True, "already_claimed": False, "receipt": receipt,
                    "phase": phase, "server_now": now}


# ==========================================================================
# Views. What the client draws.
# ==========================================================================

def _public_participation(part: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A participation row, without the internals the client has no use for."""
    if part is None:
        return None
    return {
        "player_id": part["player_id"],
        "def_key": part["def_key"],
        "epoch_start": float(part["epoch_start"]),
        "snapshot_power": round(float(part.get("snapshot_power") or 0.0), 4),
        "snapshot_rate": round(float(part.get("snapshot_rate") or 0.0), 4),
        "difficulty_roll": round(float(part.get("difficulty_roll") or 1.0), 4),
        "best_slot": int(part.get("best_slot") or 0),
        "contribution": round(float(part.get("contribution") or 0.0), 6),
        "tier": part.get("tier"),
        "claimed": part.get("claimed_at") is not None,
        "entered_at": float(part.get("entered_at") or 0.0),
    }


def ladder_view(defn: Dict[str, Any], part: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The five slots, with the player's own best marked."""
    best = int((part or {}).get("best_slot") or 0)
    out: List[Dict[str, Any]] = []
    for row in defn.get("ladder_slots") or []:
        slot = int(row.get("slot"))
        out.append({
            "slot": slot,
            "name": row.get("name") or ("slot %d" % slot),
            "health_multiple": float(row.get("health_multiple") or 0.0),
            "attack_multiple": float(row.get("attack_multiple") or 0.0),
            "cleared": slot <= best,
            "next": slot == best + 1,
        })
    return out


def banner_view(players_db, now: Optional[float] = None,
                limit: int = 3) -> Dict[str, Any]:
    """THE BANNER'S DATA SOURCE. One thin line per live occurrence.

    No player field appears anywhere in here, which is why it can serve an
    anonymous caller: it cannot be used to act on a player. ``server_now`` is the
    same clamped value the phases were computed from, so a client re-anchors on
    it every poll rather than running a free-running local timer.
    """
    now = server_now() if now is None else float(now)
    return {"server_now": now, "events": live_instances(players_db, now, limit)}


def instance_view(players_db, def_key: str, epoch_start: Optional[float],
                  player: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """THE EVENT SCREEN'S SINGLE FETCH. Definition, boundaries, ladder, tiers, claim state."""
    now = server_now() if now is None else float(now)
    player_id = str(player.get("id") or player.get("player_id") or "")
    defn = get_definition(players_db, def_key)
    if defn is None:
        raise EventNotFound("No world event called %r" % def_key)
    instance = resolve_instance(players_db, def_key, epoch_start, now)
    phase = phase_at(instance, effective_now(instance, now))
    part = get_participation(players_db, def_key, float(instance["epoch_start"]), player_id)

    claim_state = "not_yet"
    if part is not None and part.get("claimed_at") is not None:
        claim_state = "claimed"
    elif part is not None and phase == "cooldown":
        claim_state = "claimable"

    return {
        "server_now": now,
        "def_key": def_key,
        "name": defn.get("name") or def_key,
        "event_type": defn.get("event_type"),
        "epoch_start": float(instance["epoch_start"]),
        "phase": phase,
        "boundaries": {
            "warn_at": float(instance["warn_at"]),
            "active_at": float(instance["active_at"]),
            "cooldown_at": float(instance["cooldown_at"]),
            "closed_at": float(instance["closed_at"]),
        },
        "ladder": ladder_view(defn, part),
        "tiers": [
            {"tier": row.get("tier"),
             "percent_of_own_gate": float(row.get("percent") or 0.0),
             "points": round(player_gate(player) * float(row.get("percent") or 0.0) / 100.0, 4),
             "current": (part or {}).get("tier") == row.get("tier")}
            for row in defn.get("reward_tiers") or []
        ],
        "entrants": instance_entrants(players_db, def_key, float(instance["epoch_start"])),
        "min_tier_sample": MIN_TIER_SAMPLE,
        "participation": _public_participation(part),
        "claim_state": claim_state,
        "player_gate": player_gate(player),
    }


# ==========================================================================
# Invariants. Run these; do not read them and assume.
# ==========================================================================

def verify_scaling_invariant(players_db, players: List[Dict[str, Any]],
                             slots: int = 5, seeds: int = 20) -> Dict[str, Any]:
    """Simulate the ladder across builds and report where runs stop.

    Reproduce this before trusting any difficulty claim. The +-12% roll was
    added BECAUSE the curve was seed-invariant; if the wall rate moves, the slot
    multipliers are wrong.

    Reads the shipped combat function rather than a re-implementation of it, which
    is the whole reason to run this at all.
    """
    wall: Dict[int, int] = {}
    clears = 0
    total = 0
    for player in players:
        defn = default_definition({"def_key": "_probe", "ladder_slots": DEFAULT_LADDER})
        for seed_index in range(seeds):
            roll = 1.0 + ((seed_index - (seeds / 2.0)) / max(1.0, seeds)) * 2.0 * DIFFICULTY_ROLL
            part = _blank_participation("_probe", 0.0, str(player.get("id") or seed_index),
                                        float(player_snapshot(players_db, player)["power"]),
                                        0.0, roll, 0.0)
            stopped = 0
            for slot_number in range(1, int(slots) + 1):
                outcome = resolve_slot(players_db, defn, part, player, slot_number)
                if not outcome.get("cleared"):
                    break
                stopped = slot_number
            wall[stopped] = wall.get(stopped, 0) + 1
            total += 1
            if stopped >= int(slots):
                clears += 1
    return {
        "builds": len(players), "runs": total,
        "stopped_at": {str(k): v for k, v in sorted(wall.items())},
        "full_clear_pct": round(100.0 * clears / max(1, total), 2),
        "modal_wall": max(wall.items(), key=lambda kv: kv[1])[0] if wall else None,
    }


def verify_payout_invariant(players_db, def_key: str, epoch_start: float) -> Dict[str, Any]:
    """Prove the ledger's primary key is the only thing that decides a payout.

    Fires the claim twice against the SAME key and reports whether the second
    attempt was refused by the key rather than by a flag on the player row. If
    this reports ``second_blocked_by_key`` the once-only guarantee is real; if it
    reports anything else, the guarantee is a comment.
    """
    defn = get_definition(players_db, def_key)
    instance = resolve_instance(players_db, def_key, epoch_start)
    key = ledger_key(def_key, float(instance["epoch_start"]), "_invariant_probe")
    before = dict(_MEM_RECEIPTS)
    _MEM_RECEIPTS.pop(key, None)
    first = claim_payout(players_db, key, {"progress_points": 1.0, "player_gate": 100.0,
                                           "tier": "participate"}, server_now())
    second = claim_payout(players_db, key, {"progress_points": 999.0,
                                            "player_gate": 100.0,
                                            "tier": "top"}, server_now())
    _MEM_RECEIPTS.clear()
    _MEM_RECEIPTS.update(before)
    return {
        "first_claim_won": bool(first),
        "second_claim_won": bool(second),
        "second_blocked_by_key": bool(first) and not second,
        "event_type": (defn or {}).get("event_type"),
    }
