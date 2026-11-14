"""Hall of the Transcended and server seat status — the two READ routes the
Godot client calls but the server never registered.

Why this module exists
----------------------
Two shipped screens build correctly, load, and then call into nothing:

``client/scenes/main/HallOfTranscended.gd``
    ``HistorySystem.load_hall_of_transcended()`` issues
    ``GET /hall/transcended`` and reads ``result.data.get("hall", [])``.

``client/scenes/main/SlotQueue.gd``
    ``SlotManager.check_slot_status()`` issues ``GET /slot/status`` and reads
    ``result.data.get("status", "empty")``.

Neither path existed in the route table, so ``result.ok`` was always false: the
hall grid stayed empty and the waitlist screen could never learn whether a seat
was free. Both screens were dead ends — correct scene graphs driving requests
the server could not answer.

Both routes are GETs, which is why this module is read-only. It opens no
transaction, writes no row, and pays nothing.

THE SHAPES ARE NOT FREE-CHOOSEN
-------------------------------
Every key below is fixed by what the client reads, and the two responses do not
match each other on purpose — they are two different screens:

``/hall/transcended`` -> ``hall``, a LIST
    ``HallOfTranscended.create_hall_card`` reads exactly four keys per entry:
    ``name``, ``realm``, ``legacy_points``, ``transcendence_date``
    (``HallOfTranscended.gd:43-56``). The date is assigned straight into a
    ``Label.text``, so a POSIX float would render as ``1767225600.0`` on the
    player's screen. It is formatted to ``YYYY-MM-DD`` here for that reason.

``/slot/status`` -> ``status``, a STRING
    ``SlotManager.check_slot_status`` compares it against ``"dormant"`` and
    only then reads ``dormant_since`` as a float. The screen branches on "is a
    seat available" (it prints "Server is full" otherwise), so the value is a
    server-capacity answer, not a per-player answer.

Why ``/slot/status`` carries no player id
-----------------------------------------
The client sends none. ``SlotManager.check_slot_status()`` calls
``APIClient.request_get("/slot/status")`` with no query string and no body, and
there is no player identifier anywhere in that call. A per-player reading is
therefore not merely unauthenticated — it is not *computable* from the request.
The one self-consistent meaning is server capacity, and that is also what the
screen's own copy says ("Server is full" / "You have an active slot!").

The cap is not invented here. ``SEAT_CAP`` is imported from :mod:`lifecycle`:
the same constant ``/player/create`` passes to ``PlayerStore.create_account``,
and the same one whose breach raises ``SeatCapReached``. Importing it rather
than retyping the number is the whole point — a second literal would be free to
drift from the cap that actually gates account creation, and the screen would
then report "full" against a rule the server does not enforce.

The row count is a measurement, not a policy
--------------------------------------------
``occupied = len(store)`` counts every row, which is exactly the population
``create_account`` counts against the cap. Memorial, reserved and tutorial rows
all hold a seat (``POLICY["seat_policy"]``), so counting them is correct and
filtering them would be a second, different rule that nothing enforces.

WHY THE TWO WAITLIST ROUTES ARE NOT HERE
========================================
``POST /waitlist/join`` and ``POST /waitlist/leave`` are the other two paths
from the same audit, and they are deliberately absent. They are *mutating*
routes keyed to a caller, and the identity question they raise is not this
module's to answer. Two independent facts, both measured against the running
app rather than read off the source:

1. There is deliberately **no** ``POST /auth/login``, and
   ``server/test_auth_helpers.py`` says in its own module docstring that there
   "must not be one": this game has no passwords or identity provider to check
   against, so such a route would accept any ``player_id`` and hand every
   visitor a session for any player they name. Sessions are minted in-process
   by trusted server code (``auth.issue_session``). The absence is a security
   decision, not a gap — so the client cannot obtain a token over HTTP, and any
   route demanding a bearer token would deny every real client.
2. ``identity._PROVIDER`` is ``None`` after importing ``server.main``, so
   ``Depends(require_player)`` returns **503 "identity provider not
   installed"** — a dependency that reads as protection in the route table
   while rejecting everyone.

Together those mean there is currently **no** way for the shipped client to
present an identity to a mutating route. That makes the waitlist decision a
real design question rather than a wiring gap, and
``server/tests/test_auth_route_coverage_invariant.py`` makes the cost of
guessing explicit: a new unauthenticated mutating route is a hard failure
there, and its debt ledger is a ratchet that may only shrink. Adding a line to
it is the one thing that file forbids.

So the routes stay unregistered. The blocker is not "add a login route" — it is
that the client has no authenticated channel to these routes at all, and
inventing one (accepting a self-asserted ``account_id`` from the request body)
is precisely the cross-player write the auth guard exists to prevent. The
decision belongs to whoever owns player identity.

Structure
---------
House style, matching ``server/leaderboard_rewards_routes.py`` and
``server/daily_streak.py``: the response builders are PURE functions of store
rows, so the client-facing shape is assertable without standing up an app, and
the handlers stay thin wrappers that close over the store.

The registrar takes the store as a *callable* (``lambda: players_db``) rather
than a captured reference, because ``main.py`` rebinds ``players_db`` in tests
and a captured reference would go stale.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from lifecycle import SEAT_CAP

# The hall is a roll of honour, not an audit log: newest first.
#
# The cap is a real limit, not politeness. The client builds one Control per
# entry (``HallOfTranscended._on_hall_loaded`` -> ``create_hall_card``), so an
# unbounded hall would ask the Godot main thread to instantiate thousands of
# nodes in a single frame. Bounding the response bounds the client's work.
# ``limit`` is a query parameter so a caller that genuinely wants a deeper page
# can ask, and MAX_HALL_LIMIT stops that from becoming the same unbounded
# payload under another name.
DEFAULT_HALL_LIMIT = 100
MAX_HALL_LIMIT = 500

#: The two status strings the shipped screens actually distinguish.
#: ``SlotQueue`` shows the waitlist offer for anything that is not ``"active"``;
#: ``SlotManager`` reads ``"dormant"`` as a per-player state, which a
#: server-capacity answer is not and must never claim to be.
STATUS_SEAT_AVAILABLE = "active"
STATUS_SEAT_FULL = "full"

#: Ledger ``death_type`` written by ``POST /transcend`` in ``server/legacy.py``.
TRANSCENDENCE = "transcendence"


# --- Pure response builders ------------------------------------------------

def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a ledger field to float, treating bools and junk as absent.

    ``True`` is a Python ``int``, so a naive ``float(True)`` silently yields
    1.0 and would date a card to 1970. A bool in this data means corruption,
    not a number, so it takes the default. ``NaN`` and ``Infinity`` are
    rejected for the same reason: they parse as floats but cannot be turned
    into an ``int`` or compared honestly.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    # NaN and +-Infinity are floats, so they survive the coercion above and
    # then blow up one line later: ``int(nan)`` raises ValueError and
    # ``int(inf)`` raises OverflowError. Python's own json module accepts the
    # bare literals ``NaN``/``Infinity`` in input, so such a row really can
    # arrive from the store. A read route that 500s on one bad row takes the
    # hall down for every player, not just that one, so a non-finite number is
    # treated as the junk it is.
    if not math.isfinite(number):
        return default
    return number


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a ledger field to int, treating junk and non-finite as absent.

    Every ``int()`` in this module goes through here, so no caller can
    reintroduce a non-finite conversion by reaching for ``int()`` itself.

    Two distinct failure modes are covered, and they are not the same:

    - ``float("nan")`` and ``float("inf")`` parse fine and survive
      :func:`_as_float`'s coercion, then ``int()`` raises ``ValueError``
      (nan) or ``OverflowError`` (+-inf).
    - ``int()`` also raises ``OverflowError`` on a Python int too large to
      convert to a float -- which is exactly the input ``math.isfinite``
      itself rejects, so the isfinite test has to sit behind that arm too.

    ``int(1e308)`` succeeds, so a merely LARGE finite number is not junk and is
    returned as-is. Only genuinely non-representable values take the default.
    """
    number = _as_float(value, float("nan"))
    if not math.isfinite(number):
        return default
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def _iso_date(posix_seconds: Any) -> str:
    """Format a ledger timestamp as ``YYYY-MM-DD`` for the card's date label.

    ``HallOfTranscended.gd:55`` assigns this straight into a ``Label.text``. A
    float would render as ``1767225600.0`` on the player's screen, so this
    returns a calendar date. A missing or unparseable timestamp yields ``""``,
    which is also the client's own ``entry.get(...)`` default — an unreadable
    timestamp shows as absent rather than as a wrong date.

    UTC, not local time: the server clock is UTC (``POLICY["clock"]``), so a UTC
    date is the one that cannot shift with the viewer's timezone and disagree
    with every other date the game shows.

    A bool is rejected here as well as in :func:`_as_float`, for the same
    reason: ``float(True)`` is ``1.0``, which would print a confidently wrong
    1970 date on a card instead of the honest blank.
    """
    # Read through _as_float rather than float() directly. The two disagree
    # on exactly the inputs that matter here: _as_float rejects a bool, and
    # float(True) is 1.0 -- a 1970 date on a card whose real timestamp is
    # simply corrupt. _as_float also maps a non-finite value to NaN, which the
    # isfinite check below then rejects. One choke point, so no caller can
    # reintroduce the 1970 date by reaching for float() itself.
    seconds = _as_float(posix_seconds, float("nan"))
    if not math.isfinite(seconds):
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _legacy_ledger(player: Dict[str, Any]) -> List[dict]:
    """Return the player's legacy life ledger as a list, tolerating junk.

    The ledger lives at ``player["legacy"]["lives"]``, written by
    ``server/legacy.py::_record_life_end`` and persisted through the player
    store's extra-state column. It arrives from JSON, so a row can carry
    ``None`` where a dict was expected. A malformed ledger yields an empty list
    and the player is simply absent from the hall — never a 500 on a read.
    """
    legacy = player.get("legacy")
    if not isinstance(legacy, dict):
        return []
    lives = legacy.get("lives")
    if not isinstance(lives, list):
        return []
    return [entry for entry in lives if isinstance(entry, dict)]


def _latest_transcendence_at(player: Dict[str, Any]) -> Optional[float]:
    """POSIX seconds of the player's most recent transcendence, else ``None``.

    Single source of truth for "which transcendence is the latest one", shared by
    :func:`hall_entry` (which turns it into the card's date) and
    :func:`build_hall` (which orders the hall by it). Splitting this into two
    copies is how the two could disagree about which entry a card reports.

    Returns the *raw* timestamp, not the formatted date, because a date string
    has day granularity and cannot order two events inside one day.
    """
    transcensions = [e for e in _legacy_ledger(player) if e.get("death_type") == TRANSCENDENCE]
    if not transcensions:
        return None
    return _as_float(max(transcensions, key=lambda e: _as_float(e.get("at"), 0.0)).get("at"), 0.0)


def hall_entry(player: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One hall card for a player who has transcended, else ``None``.

    Pure: reads a player row, returns the card dict or ``None``. A player can
    transcend more than once, so the card reports their *latest* transcendence —
    a later one is a fresh achievement, and the older timestamp is not the date
    the hall should claim.

    ``legacy_points`` is the lifetime total, matching the "Legacy: N" card
    caption, and ``transcendence_points`` is what that final life itself
    earned. Both are reported because they answer different questions and the
    client picks one: the lifetime number is the trophy, the per-life number is
    the event. Emitting only one would force a choice this module has no basis
    to make.

    ``player_id`` is deliberately absent. The client reads four keys and none is
    an id, and a public roll of honour has no reason to hand out stable
    identifiers for the accounts it lists.

    ``realm`` is coerced to a non-empty string. The client builds its label as
    ``"Realm: " + entry.get("realm", "unknown")``, and ``.get`` substitutes that
    default only when the KEY IS MISSING. A JSON row carrying
    ``"cultivation_realm": null`` has the key, so the default never fires and the
    concatenation renders as ``Realm: <null>`` on the card. A missing key and a
    null key are the same fact for this reader, so both yield ``"unknown"``.
    """
    latest_at = _latest_transcendence_at(player)
    if latest_at is None:
        return None

    ledger = _legacy_ledger(player)
    transcensions = [e for e in ledger if e.get("death_type") == TRANSCENDENCE]
    latest = max(transcensions, key=lambda e: _as_float(e.get("at"), 0.0))
    legacy = player.get("legacy")
    total = _as_float(legacy.get("total_points"), 0.0) if isinstance(legacy, dict) else 0.0
    realm = player.get("cultivation_realm")
    # Same coercion as ``realm``, for the same reason: this module sorts the
    # hall by ``(timestamp, name)``, and a row whose ``name`` is a number or a
    # dict makes that comparison raise TypeError -- a 500 on a read route
    # caused by one bad row. The client also builds its label by assigning
    # this straight into a Godot Label, so a non-string is wrong twice over.
    name = player.get("name")

    return {
        "name": name if isinstance(name, str) and name else "Unknown",
        "realm": realm if isinstance(realm, str) and realm else "unknown",
        "legacy_points": _as_int(total),
        "transcendence_date": _iso_date(latest.get("at")),
        # Supporting detail the client ignores, present so the payload explains
        # itself to anyone reading it in a debugger.
        "transcendence_points": _as_int(latest.get("points"), 0),
        "transcendence_count": len(transcensions),
        "lives_completed": len(ledger),
    }

def build_hall(store: Any, limit: int = DEFAULT_HALL_LIMIT) -> Dict[str, Any]:
    """Build the ``/hall/transcended`` body from a player store.

    Pure with respect to the store: it reads rows and returns a new body. The
    store is passed in rather than imported, so the shape is assertable against
    a list of plain dicts with no app and no database.

    ``hall_total`` reports the full qualifying population alongside the
    truncated ``hall`` list. Without it a client cannot tell a quiet hall from a
    capped one, and would render "everyone" when the truth is "the newest 100".

    Ordering is by the RAW timestamp, not by the ``YYYY-MM-DD`` string on the
    card. The card's date has day granularity, so sorting on it ties every
    player who transcended on the same day, and the tie then falls to the name --
    which is alphabetical, not chronological. Measured before this fix: two
    players transcending two hours apart on 2026-01-01 came back Zed-then-Alice
    (reverse-alphabetical) with Zed the OLDER of the two, because "2026-01-01"
    compared equal and ``name`` broke the tie. "Newest first" was therefore only
    true across days. The raw float orders them correctly inside a day and is
    identical to the date order across days, so nothing that was right before
    changes.

    The name stays as the final tiebreaker so the response is DETERMINISTIC:
    two players with byte-identical timestamps still come back in a fixed order,
    which matters because the client builds one node per entry and a list that
    reshuffles between two identical requests would rebuild that grid.

    A player whose timestamp is unreadable sorts as ``0.0`` -- last, since the
    sort is descending. That is the honest place for it: the card renders an
    empty date, and an entry with no known date cannot claim to be recent.
    """
    # Each card travels with its own raw timestamp so the sort never has to
    # re-derive it, and so two cards can never be confused for one another.
    dated: List[tuple] = []
    for _, player in store.items():
        if not isinstance(player, dict):
            continue
        entry = hall_entry(player)
        if entry is not None:
            dated.append((_as_float(_latest_transcendence_at(player), 0.0), entry))

    dated.sort(key=lambda pair: (pair[0], pair[1]["name"]), reverse=True)
    entries: List[Dict[str, Any]] = [entry for _, entry in dated]

    total = len(entries)
    return {
        "ok": True,
        "hall": entries[:limit],
        "hall_total": total,
        "hall_limit": limit,
        "hall_truncated": total > limit,
    }


def build_slot_status(store: Any, now: Optional[float] = None) -> Dict[str, Any]:
    """Build the ``/slot/status`` body from a player store.

    Pure with respect to the store, and ``now`` is injected so the body is
    testable without a wall clock.

    ``status`` is the server-capacity answer, per the module docstring:
    ``"active"`` while a seat is available, ``"full"`` once the row count has
    reached ``SEAT_CAP``. ``dormant_since`` is reported as ``0.0`` because a
    capacity answer is not about any one player's dormancy — and the client only
    reads that key when ``status == "dormant"``, a value this route never emits.
    The key is present so the client's ``.get("dormant_since", 0.0)`` resolves
    against a real field rather than a default.
    """
    # ``now`` is an injected clock, so it can be anything a caller or a test
    # hands over -- including float("nan"), "Infinity", or a 401-digit int.
    # A bare float() let all three through: nan/inf became ``server_time`` and
    # then broke JSON encoding (Out of range float values are not JSON
    # compliant), while the huge int raised OverflowError inside float().
    # Falling back to the REAL clock rather than 0.0, because reporting
    # ``server_time: 0.0`` would be a confident false claim about the time.
    now = time.time() if now is None else _as_float(now, time.time())
    occupied = len(store)
    # SEAT_CAP is a module constant, not caller input, so this arm exists to
    # fail CLOSED if it is ever misconfigured: a capacity of 0 reports "full"
    # and hands out no seats, where an unchecked int() would have 500'd the
    # read route instead.
    capacity = _as_int(SEAT_CAP, 0)

    return {
        "ok": True,
        "status": STATUS_SEAT_AVAILABLE if occupied < capacity else STATUS_SEAT_FULL,
        "dormant_since": 0.0,
        "occupied": occupied,
        "capacity": capacity,
        "available": max(0, capacity - occupied),
        "server_time": now,
    }


# --- Route registration ----------------------------------------------------

def register_hall_slot_routes(app, store_provider) -> None:
    """Mount the hall and seat-status routes on ``app``.

    Same shape as every other feature module's registrar (see
    ``server/leaderboard_rewards_routes.py::register_leaderboard_rewards_routes``
    and ``server/daily_streak.py::register_daily_streak_routes``): an
    ``APIRouter`` plus a single ``include_router``, so ``main.py`` needs exactly
    one line and this module stays independently mountable in tests.

    The router is built *inside* this function, not at module scope. A
    module-level ``@router.get`` would register the same path twice the moment
    a test called the registrar against a second app, and
    ``server/tests/test_duplicate_route_shadowing.py`` fails on a duplicated
    method+path — the second copy is dead code that still reads as alive.

    The store arrives as a *provider callable* and is resolved per request,
    never taken as a request parameter. Resolving per request is what makes a
    test that rebinds ``main.players_db`` take effect immediately; keeping it
    out of the signature is what stops a caller naming the store.

    Importing this module has no side effects: nothing here touches ``main``,
    opens a connection, or reads configuration at import time.

    WHY ``/hall/transcended`` CARRIES AN EXPLICIT SESSION DEPENDENCY
    ================================================================
    ``build_hall`` reads EVERY row in the player store, and each card carries
    that player's real display name, realm and lifetime points. It names no
    player id in its own signature, which is exactly the shape
    ``ownership_guard`` matches on -- so the central guard skipped this route
    silently, with no log line and no error. That is an accident of the guard's
    matching rule, not a decision by anyone that a roll of honour is public.

    Measured before this fix (booted in-process, database environment
    stripped, two players minted through ``POST /player/create``, transcended
    state seeded through the store's own transaction and read back before the
    call was trusted):

        anonymous GET /hall/transcended -> 200 carrying a real player's
        ``name``, ``realm``, ``legacy_points`` and ``transcendence_date``
        forged ``Authorization`` header  -> the IDENTICAL 200

    A forged token producing the same body is the proof the route was not
    reading the header at all.

    The alternative -- keep it public and strip the identifying fields -- is
    rejected on two independent grounds:

    1. It does not survive the client. ``HallOfTranscended.create_hall_card``
       renders ``name``, ``realm``, ``legacy_points`` and
       ``transcendence_date`` (``client/scenes/main/HallOfTranscended.gd:43-56``).
       Stripping them empties the shipped screen, which is a feature deletion
       dressed as a privacy fix.
    2. A display name plus realm plus lifetime points IS still identifying. The
       account id is already absent, and omitting it did not help: a display
       name is a stable pseudonym that also appears in chat, on the friends
       list and on other boards, so these cards join back to a person across
       surfaces. Dropping the number while keeping the name protects nothing;
       dropping the name breaks the game.

    Requiring a session costs the shipped client nothing, and that is measured
    rather than assumed:

    - ``POST /auth/login`` IS mounted (``main.py:519-520``). The "no login
      route exists" claim this module's docstring used to rest on is STALE --
      the route trades the player's signing key for a token, and it issues one
      against the live app.
    - The client signs in immediately after account creation, before any other
      API call (``client/scripts/Main.gd:95-96``), and ``APIClient._request``
      attaches ``Authorization: Bearer <token>`` to EVERY request
      (``client/scripts/network/APIClient.gd:96-98``) -- including the
      ``GET /hall/transcended`` issued by
      ``HistorySystem.load_hall_of_transcended``
      (``client/scripts/systems/HistorySystem.gd:32-33``).

    So the legitimate caller already holds a token when this route runs, and the
    dependency costs it nothing. The same reasoning closed
    ``GET /equipment-ranking``, which leaked every player's id and name
    (``server/equipment.py:1103-1110``).

    ``require_player`` is imported INSIDE this function for the same reason
    ``equipment.register_equipment_routes`` imports it there: ``server/`` sits
    on ``sys.path`` as a plain directory, so the bare ``auth`` module only
    resolves once ``main.py`` has booted. Importing it at module scope would
    make ``import hall_slot_routes`` fail from any test that has not booted the
    app.

    ``/slot/status`` deliberately does NOT take the dependency. It answers
    server capacity, carries no player field, and the client calls it before it
    holds a session on a fresh install; a dependency there would break a
    screen that leaks nothing.
    """
    from auth import require_player
    from fastapi import Depends

    def _store() -> Any:
        return store_provider() if callable(store_provider) else store_provider

    router = APIRouter(tags=["hall", "slots"])

    @router.get("/hall/transcended")
    def hall_transcended(
        limit: int = Query(
            DEFAULT_HALL_LIMIT,
            ge=1,
            le=MAX_HALL_LIMIT,
            description="Most-recent transcenders to return, newest first.",
        ),
        
        _identity=Depends(require_player),
    ):
        """Roll of honour: the players who have transcended. Session required.

        The dependency is declared HERE, on the handler, because the route names
        no player id and the central shape guard therefore never attaches one.
        See the registrar docstring for the measurement that motivated it, for
        why stripping the fields instead was rejected, and for the proof that
        the shipped client already holds a token when this runs.

        ``limit`` lets a caller page deeper than the default; the shipped screen
        does not need it.
        """
        return build_hall(_store(), limit=limit)

    @router.get("/slot/status")
    def slot_status():
        """Whether the server has a seat available, and how many are left.

        The client reads ``status``, and ``dormant_since`` only when that
        status is ``"dormant"`` — which this route never returns. See the
        module docstring for why the answer is server-wide, not per-player.
        """
        return build_slot_status(_store())

    app.include_router(router)
