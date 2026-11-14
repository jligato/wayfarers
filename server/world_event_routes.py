"""
Wayfarer's Cycle — World events: the four routes.

WHAT THESE ROUTES ARE
---------------------
The client-facing half of the mode. Between them, four routes cover: read the
banner, read one event's screen, join it, fight a slot, and claim. There is no
``/world-events/start`` and no ``/world-events/end``, and the reason is in
``world_events``: start and end are ARITHMETIC, not actions. The phase is a pure
function of the clock. A player-triggered start means somebody can be late, and a
late start is a player who arrives during the warning and finds the event already
over.

WHERE THE ANTI-ABUSE LIVES, ROUTE BY ROUTE
------------------------------------------
The request MODELS are the guard, and that is deliberate. Read what is NOT here:
no ``player_id``, no ``power``, no ``rate``, no ``tier``, no ``contribution``, no
``amount``, no ``points``, no ``damage``, no ``result``, no ``now``. A client that
posts one gets a 422 from the shape validator, which is a louder and more honest
answer than a handler that quietly ignores it.

  * the player comes from the SESSION;
  * which occurrence comes from the server's own scheduler, or from the epoch the
    screen is already showing (an identity, not a start time);
  * which slot comes from the client, because that is an INTENT -- and the
    server decides whether it was cleared;
  * the fight, the tier, the contribution and the amount are all server-read.

AUTH: DECLARED BY NAME, NOT RELIED ON
-------------------------------------
Every route except ``GET /world-events/banner`` declares
``Depends(require_player)`` by name. Not because the shape-based ownership guard
would not catch it -- because a route that takes no player id and declares no
login check is left completely open, and the guard's exemption list is small and
hand-audited. ``ownership_guard.py:253`` documents in its own words that a
query-and-path scan cannot see a body-carried claim, which is how
``POST /events/claim`` once reached its handler with no session at all.

``GET /world-events/banner`` declares no auth, and that is intended: it returns
which events are running and when, and NO player field. It is the same shape as
the shipped open catalogue routes. An event screen that had to sign in to learn
"a festival is running" would be a worse product for no security gain.

EVERY RESPONSE CARRIES server_now
--------------------------------
Not just the banner. Every world-event response carries the clamped ``server_now``
the server used. One helper, one value, every call site. A route that computed a
countdown from a different ``now`` than the one it reported is the bug the
``utc-lifecycle-reconciler`` pattern exists to prevent.

REGISTRATION POSITION IS LOAD-BEARING
--------------------------------------
``register_world_event_routes(app, ...)`` must be called ABOVE
``install_ownership_guard(app)``. The guard REWRITES routes in place; a register
call below the mount registers too late for the guard to see those routes, and
every world-event route ships unguarded. main.py mounts the guard at the very
bottom of the file, so the call belongs beside the other ``register_*`` calls.

IDEMPOTENT REGISTRATION
-----------------------
The flag is an attribute on the APP object, not a module global, because
``main.lifespan`` can run more than once and FastAPI does not deduplicate. That
is the shape ``event_routes.register_event_routes`` already uses.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

# MODULE scope, not function scope. This module enables postponed annotation
# evaluation, so FastAPI resolves `Request` and `PlayerIdentity` as STRINGS
# against the module's globals. Importing them inside the register function
# leaves them absent and FastAPI reads the parameter as a query parameter,
# answering loc=["query", ...] 422 on every call instead of mounting the route.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth as _auth_module  # noqa: E402

require_player = _auth_module.require_player
PlayerIdentity = _auth_module.PlayerIdentity

import world_events as we  # noqa: E402


# --------------------------------------------------------------------------
# Request models. MODULE SCOPE ON PURPOSE -- see the note above.
#
# EVERY MODEL IS extra="forbid", and that is the load-bearing half. Without it a
# body carrying `{"amount": 999999}` is silently IGNORED and the route answers
# 200, which reads as "the server accepted my amount" and is exactly the
# confusion this file exists to prevent. With it, that body is a 422.
#
# A handler parameter with NO type annotation is even worse: FastAPI treats it as
# a QUERY parameter, so every JSON body is rejected with 422 "Field required:
# query.<name>". Every model here is annotated.
# --------------------------------------------------------------------------

class WorldEventEpochRequest(BaseModel):
    """Which occurrence. The identity of one, NOT a start time.

    Omit ``epoch_start`` and the server picks the occurrence its own scheduler
    says is current. Name it and you are naming WHICH event you are looking at --
    the boundaries are still derived from it plus the definition, never accepted
    from the caller.
    """
    model_config = ConfigDict(extra="forbid")
    def_key: str = Field(min_length=1, max_length=64)
    epoch_start: float | None = None


class WorldEventJoinRequest(WorldEventEpochRequest):
    """Join an occurrence. Nothing else to say -- the snapshot is server-read."""
    pass


class WorldEventAttemptRequest(WorldEventEpochRequest):
    """Fight one slot. ``slot`` is an INTENT, never a result.

    Bounded to [1, 32] so "slot 99" is a 422 at the shape validator rather than
    a handler that has to remember to check. The real ceiling is the definition's
    own ladder length, and the handler refuses past that.
    """
    slot: int = Field(ge=1, le=32)


# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------

def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Invoke a state-machine function, mapping its error type to a status.

    The state machine raises typed errors and knows nothing about HTTP; the
    route owns the status codes. One place, so a new error type cannot quietly
    become a 500.
    """
    try:
        return fn(*args, **kwargs)
    except we.WorldEventError as exc:
        raise HTTPException(exc.status, exc.message)


def _caller_player(players_db, identity: "PlayerIdentity") -> Dict[str, Any]:
    """The session's player record.

    Read INSIDE ``players_db.transaction`` because in memory mode a read outside
    a transaction returns the committed object while the transaction stages a
    detached copy -- which makes a later write land on a row nothing else reads.
    """
    with players_db.transaction():
        player = players_db.get(identity.player_id)
        if player is None:
            raise HTTPException(404, "Player not found")
        return dict(player)


def _ready(players_db) -> None:
    """Refuse before doing work if the migration never ran."""
    we.assert_world_event_schema(players_db)


def _epoch_or_current(players_db, req: WorldEventEpochRequest) -> float:
    """The occurrence this request names, or the one the scheduler says is live.

    MEASURED DEFECT, fixed here. A caller-named ``epoch_start`` was returned
    RAW, and it is one third of the participation row's primary key. The phase
    boundaries are DERIVED from that value (``warn_at = epoch_start``), so every
    distinct invented float opened its own window and every one of them landed
    inside the admitting band. Measured on this tree before the fix: 36 invented
    epochs inside ONE real occurrence produced 36 participation rows AND 36
    instance rows for one player, all answering 200. At the shipped rate limit
    (12 per 60s) that is 17,280 rows per player per day, in two tables, growing
    forever.

    The money was already safe -- ``anchor_lattice`` collapses the payout ledger
    key, so 12 invented epochs paid 5.0 points once, not 60. That is why this
    read as harmless storage rather than a mint. It is not harmless, it is just
    not money.

    The fix snaps the value to the definition's own occurrence lattice BEFORE it
    becomes a key, so every invented value inside one period collapses onto the
    one real anchor: one participation row, one instance row, per occurrence.

    Two properties keep this from breaking honest callers:

    * It is IDEMPOTENT on a real anchor -- snapping an anchor the scheduler
      produced returns it unchanged, which ``world_events.anchor_lattice``
      documents and its own test pins.
    * A non-finite epoch is returned UNCHANGED, so it still reaches the phase
      gate that has always refused it (409) rather than raising out of
      ``math.floor`` and becoming a 500.
    """
    defn = we.get_definition(players_db, req.def_key)
    if defn is None:
        raise HTTPException(404, "No world event called %r" % req.def_key)
    if req.epoch_start is not None:
        return we.anchor_lattice(defn, req.epoch_start)
    return we.next_epoch(defn)


# --------------------------------------------------------------------------
# Registration.
# --------------------------------------------------------------------------

def register_world_event_routes(app: FastAPI, players_db_provider,
                                wallet_store_provider=None) -> Dict[str, List[str]]:
    """Mount the four world-event routes. Call ABOVE ``install_ownership_guard``.

    Args:
        app: the FastAPI app.
        players_db_provider: zero-arg callable returning the PlayerStore.
        wallet_store_provider: zero-arg callable returning the WalletStore. Not
            used for the payout -- this mode credits cultivation progress through
            ``qi_rewards.grant_qi``, not a wallet currency -- but accepted so the
            call site matches every other register_* on this tree and nobody has
            to special-case it.

    Returns:
        The paths registered, so a caller can assert the mount landed.
    """
    # Idempotent by APP attribute, not module global: main.lifespan can run more
    # than once and FastAPI does not deduplicate a second registration.
    if getattr(app, "_wayfarer_world_event_routes_registered", False):
        return {"registered": list(getattr(app, "_wayfarer_world_event_route_paths", []))}

    registered: List[str] = []

    # Fail at mount rather than at 03:00 in production if the power anchor
    # cannot resolve every configured realm. A silent 1.0 fallback there
    # flattens every event in the game to a fixed wall.
    try:
        we.assert_realm_multipliers_resolve()
    except Exception as exc:  # noqa: BLE001 -- refuse to mount a broken mode
        raise RuntimeError("world event routes refused to mount: %s" % exc) from exc

    # --------------------------------------------------------------- banner --
    @app.get("/world-events/banner")
    async def world_events_banner():
        """Which world events are running, nearest boundary first.

        Public, and the reason is in the module docstring: this response carries
        NO player field, so an anonymous caller learns nothing they could act on.
        Same shape as the shipped open catalogue routes.
        """
        players_db = players_db_provider()
        _ready(players_db)
        return we.banner_view(players_db)

    registered.append("GET /world-events/banner")

    # ----------------------------------------------------------------- join --
    @app.post("/world-events/join")
    async def world_events_join(
        req: WorldEventJoinRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Join an occurrence and freeze this player's snapshot. Idempotent.

        Idempotent in the useful sense: a second call returns the same row with
        ``already_entered`` true rather than an error, and it does NOT re-freeze
        the snapshot or re-roll the difficulty. A client retry on a flaky
        connection should not be punished, and a double-tap must not be an
        advantage.
        """
        players_db = players_db_provider()
        _ready(players_db)
        # INSIDE _call(), deliberately. ATTEMPT_LIMITER.check() raises
        # EventRateLimited, which carries status=429, and _call() is the ONLY
        # place WorldEventError subclasses are mapped onto their .status. Called
        # outside it, the exception escaped unhandled and a designed 429 shipped
        # as a 500 with no Retry-After. Measured on this tree at the 12-per-60s
        # cap: 12x200 then 8x500. Routing the call through _call() restores the
        # 429 and keeps the one-mapper rule this file already documents.
        _call(we.ATTEMPT_LIMITER.check, identity.player_id)
        player = _caller_player(players_db, identity)
        return _call(we.enter_event, players_db, req.def_key,
                     _epoch_or_current(players_db, req), player)

    registered.append("POST /world-events/join")

    # -------------------------------------------------------------- attempt --
    @app.post("/world-events/attempt")
    async def world_events_attempt(
        req: WorldEventAttemptRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Fight one ladder slot. The server resolves it and stores the outcome.

        The client names which slot it is TRYING. It does not report a result, a
        damage number, or whether it won -- those are re-derived here from the
        frozen snapshot and the shipped combat function.

        A loss costs nothing and still returns the whole ladder. A player is
        denied the reward, never the play.
        """
        players_db = players_db_provider()
        _ready(players_db)
        player = _caller_player(players_db, identity)
        return _call(we.attempt_slot, players_db, req.def_key,
                     _epoch_or_current(players_db, req), player, req.slot)

    registered.append("POST /world-events/attempt")

    # ---------------------------------------------------------------- claim --
    @app.post("/world-events/claim")
    async def world_events_claim(
        req: WorldEventJoinRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Claim this occurrence's reward. Once per player, ever.

        THE ONLY ROUTE THAT PAYS, and the only one whose repeatability is a money
        bug. The guarantee is not the ``claimed_at`` flag and not anything in the
        response body: it is ``world_event_reward_log``'s PRIMARY KEY, taken
        BEFORE the credit in the same transaction. A replay therefore gets the
        FIRST attempt's stored receipt and moves no progress.

        Refuse whole or pay whole. A half-paid tier is a bug report with a
        screenshot.
        """
        players_db = players_db_provider()
        _ready(players_db)
        # Inside _call() for the same reason as join: a 429 raised outside the
        # mapper answers 500. See the note on the join handler.
        _call(we.ATTEMPT_LIMITER.check, identity.player_id)
        player = _caller_player(players_db, identity)
        return _call(we.settle_event, players_db, req.def_key,
                     _epoch_or_current(players_db, req), player)

    registered.append("POST /world-events/claim")

    app._wayfarer_world_event_routes_registered = True  # type: ignore[attr-defined]
    app._wayfarer_world_event_route_paths = registered  # type: ignore[attr-defined]
    return {"registered": registered}