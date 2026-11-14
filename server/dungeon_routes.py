"""
Dungeon routes for Wayfarer's Cycle.
Provides endpoints for dungeon templates, party management, instance creation, and rewards.
"""
import re
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request
from fastapi.responses import JSONResponse
from starlette.convertors import Convertor, register_url_convertor
from pydantic import BaseModel, Field

# `import auth` (BARE, not `server.auth`) is load-bearing and must not be
# "tidied" into a package-qualified import. server/main.py binds the same
# bare module as `auth_module`, and the session store is a per-module
# singleton -- so a second module object means a second store, every minted
# token lands in the store the routes do not read, and require_player rejects
# every legitimate owner with 401. Measured: this exact mistake made all
# three owner arms fail while the two refusal arms stayed green.
import auth as auth_module
from auth import PlayerIdentity, require_player

from server import dungeons
from server.dungeons import get_dungeon_manager, DungeonManager, InstanceState
import dungeon_claims
import wc_logging as wcl
from contextvars import ContextVar


# ---------------------------------------------------------------------------
# PER-REQUEST STORE RESOLUTION
#
# WHY THIS IS NOT JUST A MODULE GLOBAL
# ------------------------------------
# ``register_dungeon_routes(app, players_db)`` used to capture the store in a
# module global and every handler read that global. This tree reaches
# ``main.py`` two ways -- ``import main`` from inside ``server/`` and
# ``from server import main`` from a test -- and those are two SEPARATE module
# objects, each with its own ``PlayerStore``. The second identity's
# ``register_dungeon_routes`` call therefore REBINDS this global, and every
# /dungeons route then answers ``404 "Player not found"`` for a player who
# demonstrably exists. That reads as a route defect and is not one; see
# server/tests/test_dungeon_ownership_and_lifecycle_claim.py, trap 1.
#
# The resolution that cannot be ambiguous is the REQUEST ITSELF.
# ``request.app`` IS the app handling this call, and ``app.state`` is what that
# app's own registration published. ``main.py`` is not ours to edit, so
# ``register_dungeon_routes`` publishes the store onto ``app.state`` and the
# dependency below copies it into a context variable for the duration of the
# call. Handlers keep calling the zero-argument ``_get_players_db()``, whose
# TEXT is pinned by server/tests/test_dungeon_guard_negative_controls.py at five
# anchors -- so the per-request store is delivered by the dependency instead of
# by a new argument.
#
# The module global remains ONLY as the fallback for a call made outside a
# request (a test asserting the wiring directly, or any non-ASGI caller), which
# is what keeps those callers working.
# ---------------------------------------------------------------------------

#: The store serving THIS request, published per call by :func:`_bind_store`.
_request_players_db: "ContextVar[object]" = ContextVar("wc_dungeon_players_db", default=None)

#: ``app.state`` key this module owns. It is NAMESPACED to this module on
#: purpose: ``wc_players_db`` was previously a single key that dungeon_routes,
#: zone_routes and npc_routes all wrote, so registering two of them with
#: different stores on one app made the LAST registration win for all of them
#: and a route then served another module's store. One key per module means the
#: three can never overwrite each other.
PLAYERS_DB_STATE_KEY = "wc_dungeon_players_db"


async def _bind_store(request: Request):
    """Publish the store of the app SERVING this request, then clear it again.

    ``request.app.state`` is the only store identity that cannot be ambiguous:
    it belongs to the app that received this exact call. It is published here
    rather than read inside each handler so the call sites keep the text that
    server/tests/test_dungeon_guard_negative_controls.py pins.

    THE CLEAR IS NOT OPTIONAL. This is a context variable, and a set with no
    matching reset is inherited by whatever runs next in that context -- which
    is how a store belonging to one app would end up serving another app's
    request, i.e. the very bug this change exists to remove. server/
    request_logging.py documents the same rule for the request-id variable.
    Yielding makes the reset run after the response has been sent, so the
    variable is unset for every request that did not come through this router.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    store = getattr(state, PLAYERS_DB_STATE_KEY, None)
    token = _request_players_db.set(store)
    # The ``yield`` is what makes this a FastAPI GENERATOR dependency: the
    # framework resumes the function after the response has been sent, which is
    # when the reset must happen. A plain ``return`` here would run the reset
    # BEFORE the handler, leaving the handler to read the registration-time
    # global -- i.e. the bug this change exists to remove, with a green test
    # suite, because a single-app test cannot tell the two apart.
    try:
        yield store
    finally:
        _request_players_db.reset(token)


router = APIRouter(prefix="/dungeons", tags=["dungeons"], dependencies=[Depends(_bind_store)])


# ======================================================================================
# ROUTE ORDER IS NOT ENOUGH -- THE READ ROUTES MUST NOT MATCH THE POST-ONLY LITERALS
# ======================================================================================
# Reordering cannot fix the shadowing. Starlette walks the route table and keeps the
# FIRST route that fully matches path AND method; a path match with the wrong method
# is remembered only as a fallback. So when ``GET /dungeons/party/{party_id}`` is
# registered before ``POST /dungeons/party/create``, the read route still wins the GET
# -- measured, not assumed: with the POST literal declared FIRST, a GET to the literal
# still answered 200 from the read handler.
#
# So the read routes are constrained instead. ``{party_id:dungeon_party_id}`` matches
# any single path segment EXCEPT the literal words that belong to a sibling POST route.
# A GET to ``/dungeons/party/create`` therefore matches no GET route at all, and the
# router falls back to the POST route it remembered -> a real 405 naming ``Allow:
# POST``, instead of a membership refusal that reads like an authorisation bug.
#
# The two reserved sets differ because the two families have different POST siblings.
# An unknown-but-legal id (``zzz-does-not-exist``) still reaches the read route and
# still gets its membership refusal, so the read route's own behaviour is unchanged --
# only the borrowed literals are excluded.
_PARTY_RESERVED = ("create", "invite", "leave", "ready")
_INSTANCE_RESERVED = ("create", "start", "advance", "fail", "abandon", "claim-loot")


class _NotReservedWord(Convertor):
    """A path convertor that refuses a fixed set of sibling-route literals."""

    def __init__(self, reserved):
        self.reserved = tuple(reserved)
        joined = "|".join(re.escape(word) for word in self.reserved)
        self.regex = f"(?!(?:{joined})$)[^/]+"

    def convert(self, value):
        return value

    def to_string(self):
        return "dungeon_" + "_".join(self.reserved)


register_url_convertor("dungeon_party_id",
                       _NotReservedWord(_PARTY_RESERVED))
register_url_convertor("dungeon_instance_id",
                       _NotReservedWord(_INSTANCE_RESERVED))


class PartyCreateRequest(BaseModel):
    leader_id: str = Field(..., description="Player ID of party leader")


class PartyInviteRequest(BaseModel):
    # RENAMED from `player_id`. The field names the INVITEE, i.e. a THIRD
    # PARTY -- it was never the acting player. The central ownership guard reads
    # any body field called `player_id` as the acting-player claim, so with the
    # old name it demanded a session belonging to the person being invited.
    # Measured on the live app before the rename:
    #
    #     leader (the only entitled caller) invites member -> 403  REFUSED
    #     outsider invites THEMSELVES into that party      -> 200  ACCEPTED
    #
    # The guard was not merely incomplete, it was INVERTED: it blocked the real
    # leader and waved through a stranger, and because joining was free, every
    # downstream route gated on bare party membership was then defeated by that
    # one request. An `invitee_id` is not in the guard's claim-name set, so the
    # guard correctly stops reading this route as a cross-player route, and the
    # route below does the real work: authenticate the caller, then require the
    # caller to already be in THAT party.
    party_id: str = Field(..., description="Party ID to invite to")
    invitee_id: str = Field(..., description="Player ID of the player being invited")


class PartyReadyRequest(BaseModel):
    player_id: str = Field(..., description="Player ID")
    ready: bool = Field(..., description="Ready status")


class InstanceCreateRequest(BaseModel):
    party_id: str = Field(..., description="Party ID")
    template_id: str = Field(..., description="Dungeon template ID")


class InstanceActionRequest(BaseModel):
    instance_id: str = Field(..., description="Instance ID")
    player_id: str = Field(..., description="Player ID")


class LootClaimRequest(BaseModel):
    instance_id: str = Field(..., description="Instance ID")
    player_id: str = Field(..., description="Player ID")
    reward_type: str = Field(..., description="loot, first_clear, or weekly")


# Fallback for callers OUTSIDE a request. Inside a request the store comes
# from ``request.app.state`` via :func:`_bind_store`; see the note above.
_players_db = None


def _get_players_db():
    """The store serving THIS request, else the one captured at registration.

    An explicit ``None`` global stays authoritative for "never wired", so a
    kill-switch set by hand is not shadowed by a store left on ``app.state``
    from an earlier registration. See :func:`_bind_store`.
    """
    if _players_db is None:
        return None
    bound = _request_players_db.get()
    if bound is not None:
        return bound
    return _players_db


# --------------------------------------------------------------------------------------
# PARTICIPATION
#
# THE QUESTION THIS FILE KEEPS ASKING
# -----------------------------------
# "Is the caller in this party?" is not a security boundary here, because there
# is a FREE route into a party: one POST /dungeons/party/invite with a session.
# That is exactly the shape of the sect-war defect this file's sibling guard in
# server/sect.py closes -- "is the caller in one of the two warring sects?" was
# answerable one request earlier via the free POST /sect/join.
#
# So membership is a LOOKUP, not a credential. What makes a caller a real
# participant here is PARTICIPATION IN THIS SPECIFIC PARTY, and for the routes
# below that is checked against the party's own member map -- which is only
# reachable now that the invite route requires the inviter to already be in the
# party. Charging for, rate-limiting or cooling down party FORMATION is
# explicitly out of scope: it has a wide blast radius across every consumer of
# party membership, and it fixes the wrong end. The action routes have exactly
# one caller each, and they are what this closes.
#
# Ordering is deliberate everywhere below: authenticate, then confirm the
# caller's own claim, and only then look the party up. A stranger learns nothing
# about whether a guessed party id exists.
# --------------------------------------------------------------------------------------
def _require_party_member(dm, party_id: str, caller: str) -> Any:
    """The party named by ``party_id``, or 403 unless ``caller`` is in it.

    One helper for every route whose real gate is "this player belongs to this
    party", so the rule is written once. A 403 (not a 404) is deliberate for the
    same reason sect.py gives: a 404 would be an oracle that lets an outsider
    distinguish "party exists" from "no such party".
    """
    party = dm.get_party(party_id)
    if party is None or caller not in party.members:
        raise HTTPException(status_code=403, detail="Not a member of this party")
    return party


def _require_instance_party_member(dm, instance_id: str, caller: str) -> Any:
    """The instance named by ``instance_id``, or 403 unless ``caller`` is in
    the party that owns THAT instance.

    Membership is checked against the instance's own party, not against the
    caller's current party: a caller who left the party, or who is in a
    different one, must not drive a run they are not part of. Read from
    ``party.members`` rather than ``dm.player_party`` because the latter is a
    convenience index and the former is the authoritative roster.
    """
    instance = dm.instances.get(instance_id)
    if instance is None:
        # Same answer as a membership refusal: a 404 here would let a stranger
        # enumerate live instance ids.
        raise HTTPException(status_code=403, detail="Not a participant in this instance")
    party = dm.parties.get(instance.party_id)
    if party is None or caller not in party.members:
        raise HTTPException(status_code=403, detail="Not a participant in this instance")
    return instance


@router.get("/templates", response_model=List[Dict[str, Any]])
async def list_dungeon_templates(
    player_id: str = Query(..., description="Player ID"),
    realm: str = Query(..., description="Player cultivation realm"),
    level: int = Query(..., description="Player level"),
    identity: PlayerIdentity = Depends(require_player),
):
    """List available dungeon templates for the player.

    F3 -- CONSISTENCY, NOT A LEAK
    -----------------------------
    This route carried no `require_player` dependency. Measured before this
    change it was still refused to an anonymous caller (the central guard reads
    the query `player_id`) and answered 200 to a signed-in outsider, who read
    `first_clear_rewards` and `weekly_rewards` in full. Severity is genuinely
    LOW: these are static catalogue rows, no state changes, no payout, and the
    same numbers are learnable by playing. It is fixed so that all fourteen
    dungeon routes declare their own identity requirement explicitly instead of
    four of them riding a field-name matcher in another file.

    WHAT IS DELIBERATELY NOT CHANGED
    --------------------------------
    The `realm` and `level` query parameters stay caller-supplied. Deriving
    them from the player row instead was considered and rejected: a freshly
    created player is realm `mortal` at level 1, and
    `get_available_templates("mortal", 1)` returns an EMPTY list, so deriving
    would hand every new player a blank catalogue and break the shipped
    contract in server/test_dungeons.py, which asserts a non-empty list. The
    route also has no `level` column to read: the player row carries
    `cultivation_realm`, `cultivation_stage` and `progress`, never `level`.
    Changing that mapping is a design decision for the human, not a security
    fix smuggled in here.

    AN UNRANKABLE REALM IS A 400, NOT A 500
    ---------------------------------------
    ``dm.get_available_templates`` raises ``UnknownRealmError`` for a realm this
    gate cannot rank, and that refusal is deliberate (see UnknownRealmError in
    server/dungeons.py for why the two realm ladders disagree). It used to
    escape this handler unhandled, so the request answered HTTP 500 with the
    plain-text body "Internal Server Error" -- not even JSON, so a client reading
    ``detail`` off the failure got nothing at all, and no realm was named. It is
    caught below and answered 400 in this router's own ``{"detail": ...}`` shape,
    carrying the exception's message, which names both the rejected realm and
    the realms the ladder does know.

    Deliberately NOT broadened to a blanket ``except Exception``: a genuinely
    unexpected error must still surface as a 500 rather than being laundered
    into a tidy 400 that names the wrong thing.
    """
    if player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    templates = dm.get_available_templates(realm, level)

    return [{
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "instance_type": t.instance_type.value,
        "min_level": t.min_level,
        "max_level": t.max_level,
        "min_party_size": t.min_party_size,
        "max_party_size": t.max_party_size,
        "recommended_realm": t.recommended_realm,
        "time_limit_seconds": t.time_limit_seconds,
        "total_floors": len(t.floors),
        "entrance_zone": t.entrance_zone,
        "first_clear_rewards": t.first_clear_rewards,
        "weekly_rewards": t.weekly_rewards,
    } for t in templates]


@router.get("/templates/{template_id}", response_model=Dict[str, Any])
async def get_dungeon_template(
    template_id: str,
    player_id: str = Query(..., description="Player ID"),
    identity: PlayerIdentity = Depends(require_player),
):
    """Get detailed dungeon template information.

    F3, the same consistency fix as the list route: this route returned the
    full `loot_table` to any signed-in outsider. Static catalogue, no state
    change, so severity is low -- fixed so no dungeon route depends on the
    central guard noticing a query field name.
    """
    if player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    template = dm.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Dungeon template not found")

    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "instance_type": template.instance_type.value,
        "min_level": template.min_level,
        "max_level": template.max_level,
        "min_party_size": template.min_party_size,
        "max_party_size": template.max_party_size,
        "recommended_realm": template.recommended_realm,
        "time_limit_seconds": template.time_limit_seconds,
        "floors": template.floors,
        "entrance_zone": template.entrance_zone,
        "entrance_coords": template.entrance_coords,
        "loot_table": template.loot_table,
        "first_clear_rewards": template.first_clear_rewards,
        "weekly_rewards": template.weekly_rewards,
    }


# Party Endpoints
@router.post("/party/create", response_model=Dict[str, Any])
async def create_party(
    request: PartyCreateRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Create a new party.

    THE DEFECT THIS CLOSES
    ----------------------
    This router had ZERO sign-in checks. The only guard was
    `if request.leader_id not in players_db: 404`, which proves the id NAMES
    SOMEONE -- it says nothing about WHO IS CALLING. Measured on the live app
    before this change: an ANONYMOUS POST naming a victim created seconds
    earlier returned 200 and left a real Party row with the victim as leader.

    THREE GATES, each a separate failure if removed
    -----------------------------------------------
    1. IDENTITY -- Depends(require_player) raises 401 with no session.
    2. ACTOR == CLAIM -- the body field is `leader_id`, NOT `player_id`, so
       require_player's body-claim check does not see it. If it did, every
       caller creating a party would be refused. The explicit check below is
       what actually enforces the actor here, and it is load-bearing.
    3. No group-membership claim: a leader creating their OWN party is the
       legitimate owner and MUST still succeed.
    """
    if request.leader_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.leader_id not in players_db:
        raise HTTPException(status_code=404, detail="Leader not found")

    dm = get_dungeon_manager()

    # Check if leader already in a party
    if request.leader_id in dm.player_party:
        raise HTTPException(status_code=400, detail="Already in a party")

    party = dm.create_party(request.leader_id)
    return {
        "ok": True,
        "party_id": party.id,
        "leader_id": party.leader_id,
        "members": [{"player_id": pid, "role": m.role.value} for pid, m in party.members.items()],
    }


@router.get("/party/{party_id:dungeon_party_id}", response_model=Dict[str, Any])
async def get_party(
    party_id: str,
    player_id: str = Query(..., description="Player ID"),
    identity: PlayerIdentity = Depends(require_player),
):
    """Get party information.

    THE DEFECT THIS CLOSES
    ----------------------
    The query ``player_id`` was only ever checked for EXISTENCE
    (``if player_id not in players_db``), so the central guard's "is the claim
    the caller's own?" was the whole authorization story -- and a caller who
    names THEMSELVES passes it truthfully. Measured on the live app before this
    change: a signed-in player in no party read another party's roster and
    received every member's id, role and ready flag.

    That roster is the map an attacker reads before choosing a target, and it is
    the same membership-blindspot shape as the rest of this file. The gate that
    matters is PARTICIPATION: only a member of THAT party reads it.
    """
    if player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    party = _require_party_member(dm, party_id, identity.player_id)

    return {
        "ok": True,
        "party_id": party.id,
        "leader_id": party.leader_id,
        "instance_id": party.instance_id,
        "created_at": party.created_at,
        "members": [{"player_id": pid, "role": m.role.value, "joined_at": m.joined_at, "ready": m.ready} for pid, m in party.members.items()],
    }


@router.post("/party/invite", response_model=Dict[str, Any])
async def invite_to_party(
    request: PartyInviteRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Invite a player to a party.

    THE DEFECT THIS CLOSES
    ----------------------
    This route had no resource-level gate at all. Its only check was
    ``if request.player_id not in players_db: 404``, which proves the invitee
    NAMES SOMEONE and says nothing about WHO IS CALLING. Measured on the live
    app before this change, with the central ownership guard installed:

        leader (the only entitled caller) invites a member  -> 403
        an outsider invites THEMSELVES into that party       -> 200

    So it was not merely unguarded, it was INVERTED: the guard read the body's
    ``player_id`` field -- the INVITEE, a third party -- as the acting-player
    claim, so it demanded a session belonging to the person being invited. That
    refused the real leader and admitted a stranger, and because joining a party
    was free and instant, every downstream route gated on bare party membership
    was then defeated by this single request. End to end on a COMPLETED run,
    that chain moved three dungeon loot items into a stranger's inventory and
    left the real member with ``rewards: []`` forever.

    THREE GATES, each a separate failure if removed
    -----------------------------------------------
    1. IDENTITY  -- Depends(require_player) raises 401 with no session. The
       guard no longer supplies this: the body field is now ``invitee_id``,
       which is not in the guard's claim-name set, so this route matches no
       shape rule and the dependency is explicit rather than incidental.
    2. INVITER IS IN THE PARTY  -- this is the actual fix. The caller must
       already be a member of THAT party. This is a property of the RESOURCE,
       so no shape-matching heuristic can supply it: nothing about the request
       says whether this particular player belongs to this particular party.
    3. INVITEE IS NOT THE CALLER -- a member cannot "invite" themselves, which
       would otherwise be a second free route into a party, and their own
       session would satisfy both gates above.

    ORDERING IS A GATE, not a style choice. `_require_party_member` runs BEFORE
    the invitee's player row is looked up. Measured on the live app before this
    change, the order was the other way round, so an outsider learned whether a
    PLAYER id existed: 404 "Player not found" for a fake invitee against 403 for
    a real one. That is a player-id oracle, and it disappears once membership
    is asked first. The invitee lookup is kept, not deleted -- a leader who
    belongs to the party still gets a 404 on a typo, which is the whole reason
    the lookup is allowed to stay.
    """
    if request.invitee_id == identity.player_id:
        raise HTTPException(status_code=403, detail="Not a member of this party")

    dm = get_dungeon_manager()
    _require_party_member(dm, request.party_id, identity.player_id)

    players_db = _get_players_db()
    if request.invitee_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    if dm.invite_to_party(request.party_id, request.invitee_id):
        # Add to player_party tracking
        dm.player_party[request.invitee_id] = request.party_id
        return {"ok": True}
    else:
        raise HTTPException(status_code=400, detail="Cannot invite - party full, player already in party, or party not found")


@router.post("/party/leave", response_model=Dict[str, Any])
async def leave_party(
    player_id: str = Body(..., embed=True),
    identity: PlayerIdentity = Depends(require_player),
):
    """Leave current party.

    THE DEFECT THIS CLOSES
    ----------------------
    No sign-in check at all. Measured on the live app before this change: an
    ANONYMOUS POST naming a victim returned 200 and EJECTED that victim from
    their own party -- a signed-out stranger could dismantle anyone's group.

    Gates: (1) require_player raises 401 with no session. (2) The body field
    IS `player_id`, so require_player already refuses a body naming someone
    else with 403; the explicit re-check is deliberate belt-and-braces on a
    cross-player mutation. Leaving YOUR OWN party must still work.
    (3) The party must not own a run that is IN PROGRESS -- see below.

    THE RUN THIS ROUTE USED TO STRAND
    ---------------------------------
    Measured on the live app: a sole leader, a solo template, a run already
    STARTED and advanced one floor.

        leave                        -> 200 {"ok": true}
        the party row afterwards     -> GONE (a solo leader disbands it)
        the instance row afterwards  -> still present, still state "active"
        advance / read / claim-loot  -> 403 "Not a participant in this instance"
        create a party again         -> 200, a brand new unrelated party

    The run became unreachable FOREVER. _require_instance_party_member resolves
    the party from the instance, the party no longer exists, and the instance it
    pointed at is still in dm.instances as "active" and is never reaped. No
    route can advance it, finish it, or pay its loot: `abandon` is the route
    meant to end a run and it is itself gated on participation in that same
    party's instance. One tap on "leave" therefore destroyed a live run and the
    loot already earned, and left a permanent orphan in memory.

    SCOPE, set by what was actually measured and by contracts already pinned:

    * ACTIVE -- refused. The case above: floors cleared, loot pending, all of it
      destroyed by a leave.
    * WAITING (created, never started) -- ALLOWED, unchanged. Nothing is spent
      and no loot exists. Two contract tests deliberately pin that a solo leader
      leaving a party that still holds a waiting instance gets 200 and
      disbands, so a broader refusal would have broken a decision that is not
      mine to overturn.
    * COMPLETED / FAILED / ABANDONED -- ALLOWED, unchanged. No route can
      advance such a run, so letting the party go strands nothing.

    Deliberately NOT auto-abandoning the run instead: that would silently end a
    party-mates' shared run because one member left, and since loot drops are
    chance-gated the player could not tell what they had forfeited. A visible
    refusal beats a quiet destruction.
    """
    if player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()

    # Gate 3, before anything is mutated. Resolved through the caller's OWN
    # party, so this is about the run they are about to strand, and so a
    # stranger cannot use it to probe another party's state.
    party = dm.get_player_party(player_id)
    if party is not None and party.instance_id:
        live = dm.instances.get(party.instance_id)
        if live is not None and live.state == InstanceState.ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=("Cannot leave a party with a dungeon run in progress. "
                        "Abandon the run first with POST /dungeons/instance/abandon."),
            )

    if dm.leave_party(player_id):
        return {"ok": True}
    else:
        raise HTTPException(status_code=400, detail="Not in a party")


@router.post("/party/ready", response_model=Dict[str, Any])
async def set_party_ready(
    request: PartyReadyRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Set party ready status.

    THE DEFECT THIS CLOSES
    ----------------------
    Here the body's ``player_id`` IS the acting player, so the central guard
    already refuses an anonymous caller and refuses one naming someone else --
    but it can only answer "is this claim the caller's own?". A caller naming
    THEMSELVES passes that truthfully, and the route then wrote another
    player's ready flag on their own party. Measured on the live app before this
    change, a signed-in player in no party flipped a member's ready flag.

    Ready is not cosmetic: ``DungeonManager.create_instance`` requires
    ``party.all_ready()``, so this is a write on a real precondition. The
    membership check below is belt-and-braces on the manager's own
    ``get_player_party`` lookup, stated explicitly so the route does not depend
    on the shape matcher in another file staying attached.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    party = dm.get_player_party(identity.player_id)
    if party is None or identity.player_id not in party.members:
        raise HTTPException(status_code=403, detail="Not a member of this party")

    if dm.set_member_ready(request.player_id, request.ready):
        return {"ok": True, "ready": request.ready}
    else:
        raise HTTPException(status_code=400, detail="Not in a party")


# Instance Endpoints
@router.post("/instance/create", response_model=Dict[str, Any])
async def create_instance(
    request: InstanceCreateRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Create a dungeon instance for a party.

    THE DEFECT THIS CLOSES
    ----------------------
    No sign-in check. The body carries no `player_id` at all -- only a party
    id and a template id -- so a stranger could name any party and start its
    instance.

    Gate: require_player raises 401 with no session. The meaningful second
    gate is MEMBERSHIP, because this is a property of the RESOURCE, not of
    the request shape: only a member of that party may start its instance.

    That gate is `_require_party_member`, which answers 403 for a missing
    party AND for a party the caller does not belong to. Measured on the live
    app before this change, the lookup ran the other way round -- `dm.get_party`
    then a 404 "Party not found", then the membership 403 -- so a signed-in
    outsider could confirm any guessed party id by reading which code it got.
    """
    dm = get_dungeon_manager()
    party = _require_party_member(dm, request.party_id, identity.player_id)

    players_db = _get_players_db()

    # Verify all party members exist
    for pid in party.members.keys():
        if pid not in players_db:
            raise HTTPException(status_code=404, detail=f"Party member {pid} not found")

    instance = dm.create_instance(request.template_id, request.party_id)
    if not instance:
        raise HTTPException(status_code=400, detail="Cannot create instance - check party size, readiness, and template requirements")

    template = dm.templates.get(request.template_id)
    return {
        "ok": True,
        "instance_id": instance.id,
        "template_id": instance.template_id,
        "template_name": template.name if template else "Unknown",
        "state": instance.state.value,
        "created_at": instance.created_at,
    }


@router.get("/instance/{instance_id:dungeon_instance_id}", response_model=Dict[str, Any])
async def get_instance(
    instance_id: str,
    player_id: str = Query(..., description="Player ID"),
    identity: PlayerIdentity = Depends(require_player),
):
    """Get instance progress and state.

    THE DEFECT THIS CLOSES
    ----------------------
    This route had no sign-in dependency, and the membership check below the
    progress read was DECORATION: the condition was computed correctly and the
    body of the `if` was `pass`. Measured on the live app before this change, a
    signed-in player in no party read a live run and received 200 with a 12-key
    body including `current_floor_enemies` -- the full stat block of every
    monster on the floor the party is currently fighting. Two further faults in
    those three lines: `instance.party_id not in dm.player_party.get(...)` is a
    SUBSTRING test against a string rather than a membership test, and
    `dm.player_party` is a convenience index an attacker can flip with one free
    `POST /dungeons/party/invite`.

    The comment claiming the reply is "basic info for discovery" does not
    survive reading the payload: a monster stat block is not discovery.

    Gate: identity, then the ACTOR == CLAIM check, then PARTICIPATION via the
    same `_require_instance_party_member` that `advance`, `fail`, `abandon` and
    `claim-loot` already use. It answers 403 rather than 404, so run ids stay
    unguessable to a stranger.
    """
    if player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    _require_instance_party_member(dm, instance_id, identity.player_id)

    progress = dm.get_instance_progress(instance_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Instance not found")

    return progress


@router.post("/instance/start", response_model=Dict[str, Any])
async def start_instance(
    request: InstanceActionRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Start a dungeon instance.

    THE DEFECT THIS CLOSES
    ----------------------
    No `require_player` dependency and no resource check at all. The route
    survived on the CENTRAL guard alone, which works only because the body
    field happens to be named `player_id`; that is one rename from failing, and
    the sibling `invite` route shows exactly how: its body field used to be
    `player_id` naming the INVITEE, the guard read it as the acting player, and
    the result was an INVERTED route that refused the real leader and admitted
    a stranger. The rename to `invitee_id` at the top of this file is
    load-bearing, and a route that depends on it is one refactor away from the
    same failure.

    The second fault is bigger: the route asked only "is this caller the party
    leader?" and never "is this caller in this party?". A session for ANY
    player could drive ANY leader's start, provided the body named the real
    leader -- the central guard was satisfied truthfully, because the caller
    was claiming to be the leader and the route believed them.

    Gates, in order: identity, ACTOR == CLAIM, PARTICIPATION in the party that
    owns THIS instance, then the leader rule. The leader test compares
    `identity.player_id`, not the body field, so it cannot be forged.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    # The call is wrapped across three lines on purpose: `advance` uses the
    # two-line form, and the negative-control harness anchors on that exact
    # text and fails if it occurs twice. Keeping the two call sites
    # textually distinct is what stops a control from editing the wrong route.
    instance = _require_instance_party_member(
        dm, request.instance_id,
        identity.player_id)

    party = dm.parties.get(instance.party_id)
    if not party or party.leader_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Only party leader can start instance")

    if dm.start_instance(request.instance_id):
        return {"ok": True, "state": InstanceState.ACTIVE.value, "started_at": instance.started_at}
    else:
        raise HTTPException(status_code=400, detail="Cannot start instance")


@router.post("/instance/advance", response_model=Dict[str, Any])
async def advance_instance_floor(
    request: InstanceActionRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Advance to the next floor in an active instance.

    THE DEFECT THIS CLOSES
    ----------------------
    The only gate was ``instance.party_id != dm.player_party.get(
    request.player_id)``, i.e. "is the id in the body in this party" -- and the
    body id was only checked for existence. Measured on the live app before this
    change, a signed-in player in no party advanced another party's run. And
    because joining a party was free, ``dm.player_party`` was a lookup the
    attacker flipped one request earlier, so the check was not a boundary even
    for a party member. PARTICIPATION in the party that owns THIS instance is
    the gate; it is a property of the resource, so no shape matcher can supply
    it. Advancing is shared and one-way (the final floor completes the run and
    drops its loot), so it is not a stranger's call.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    instance = _require_instance_party_member(
        dm, request.instance_id, identity.player_id)

    if dm.advance_floor(request.instance_id):
        instance = dm.instances.get(request.instance_id)
        template = dm.templates.get(instance.template_id)
        return {
            "ok": True,
            "state": instance.state.value,
            "current_floor": instance.current_floor,
            "total_floors": len(template.floors) if template else 0,
            "completed": instance.state == InstanceState.COMPLETED,
            "loot_dropped": instance.loot_dropped,
        }
    else:
        raise HTTPException(status_code=400, detail="Cannot advance floor")


@router.post("/instance/fail", response_model=Dict[str, Any])
async def fail_instance(
    request: InstanceActionRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Fail an instance (party wiped).

    THE DEFECT THIS CLOSES
    ----------------------
    Same shape as instance/advance, and worse: failing is PERMANENT and it
    ends the run for the WHOLE party. There is no route back out of a failed
    instance, so a stranger who could call this destroyed every member's run.
    The old gate read ``dm.player_party``, a free lookup, and never checked that
    the caller was in the party that owns THIS instance.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    _require_instance_party_member(dm, request.instance_id, identity.player_id)

    if dm.fail_instance(request.instance_id):
        instance = dm.instances.get(request.instance_id)
        return {"ok": True, "state": instance.state.value}
    else:
        raise HTTPException(status_code=400, detail="Cannot fail instance")


@router.post("/instance/abandon", response_model=Dict[str, Any])
async def abandon_instance(
    request: InstanceActionRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Abandon an instance.

    THE DEFECT THIS CLOSES
    ----------------------
    Same shape as instance/fail, and equally permanent: abandoning ends the run
    for the whole party and clears the party's ``instance_id``, so there is no
    route back into it. The old gate read the free ``dm.player_party`` lookup
    and never confirmed the caller was in the party that owns THIS instance.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    dm = get_dungeon_manager()
    _require_instance_party_member(dm, request.instance_id, identity.player_id)

    if dm.abandon_instance(request.instance_id):
        instance = dm.instances.get(request.instance_id)
        return {"ok": True, "state": instance.state.value}
    else:
        raise HTTPException(status_code=400, detail="Cannot abandon instance")


# Reward Endpoints
def _credit_loot(players_db, player_id: str, loot: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add each claimed item to the player's inventory and persist it.

    This is the write-back that the claim route was missing: the manager
    decides WHAT is owed and records the claim, but it holds no player store,
    so before this the response advertised rewards that never reached the
    wallet at all.

    Runs inside the caller's transaction and mutates the live player dict, so
    the caller controls commit order. Item counts are accumulated rather than
    assigned, so two entries for the same item_id in one payout sum correctly
    instead of the second overwriting the first.
    """
    if not loot:
        return []

    player = players_db[player_id]
    inventory = player.get("inventory")
    if not isinstance(inventory, dict):
        inventory = {}
        player["inventory"] = inventory

    credited: List[Dict[str, Any]] = []
    for item in loot:
        item_id = item.get("item_id")
        count = int(item.get("count", 0) or 0)
        if not item_id or count <= 0:
            continue
        inventory[item_id] = int(inventory.get(item_id, 0) or 0) + count
        credited.append({"item_id": item_id, "count": count,
                         "source": item.get("source", "dungeon")})

    players_db.persist(player_id, player)
    return credited


@router.post("/instance/claim-loot", response_model=Dict[str, Any])
async def claim_loot(
    request: LootClaimRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Claim loot from completed instance.

    THE PAYOUT, and therefore the sharpest edge of this class
    -------------------------------------------------------
    The payout is committed to the player's inventory here, not merely described
    in the response: the manager's claim methods are pure calculators plus a
    ledger, so a route that only returned their list would advertise a reward
    forever and grant it never.

    THE DEFECT THIS CLOSES
    ----------------------
    Measured on the live app before this change, end to end on a COMPLETED run:

        an outsider POSTs party/invite naming THEMSELVES   -> 200 (joined)
        the outsider POSTs this route                      -> 200, 3 items credited
        the member who actually ran it claims the same loot -> 200, rewards: []
        that member's inventory                             -> {}, permanently

    Three defects stacked. (1) The route trusted ``request.player_id`` naming
    SOMEONE, checked only for existence. (2) The manager's own
    ``claim_loot`` does check ``player_party[player_id] == instance.party_id``,
    but joining a party was FREE and INSTANT, so that check was a lookup the
    attacker flipped one request earlier -- exactly the membership-vs-
    participation hole the sect-war guard in server/sect.py closes. (3) The
    credit burns the run's shared claim ledger, so the theft is not merely
    doubled up: the victim is left unable to ever retry.

    Gates, in order, each a separate failure if removed:
      1. IDENTITY            -- Depends(require_player) raises 401.
      2. ACTOR == CLAIM      -- the body must name the caller, so a member
                                cannot claim into ANOTHER member's inventory.
      3. PARTICIPATION       -- the caller must be in the party that owns THIS
                                instance. Checked before the claim ledger is
                                touched, so a refused caller burns nothing.
    """
    if request.player_id != identity.player_id:
        raise HTTPException(status_code=403, detail="Not your player")

    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    if request.reward_type not in ("loot", "first_clear", "weekly"):
        # Before the manager is consulted, and before any ledger is touched.
        raise HTTPException(status_code=400, detail="Invalid reward type")

    dm = get_dungeon_manager()
    _require_instance_party_member(dm, request.instance_id, identity.player_id)

    with players_db.transaction(request.player_id):
        if request.reward_type == "loot":
            loot = dm.claim_loot(request.instance_id, request.player_id)
        elif request.reward_type == "first_clear":
            loot = dm.claim_first_clear_rewards(request.instance_id, request.player_id)
        elif request.reward_type == "weekly":
            loot = dm.claim_weekly_rewards(request.instance_id, request.player_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid reward type")

        # F4 -- THE ONCE-ONLY GUARD BECOMES A FACT ABOUT THE DATA
        # --------------------------------------------------------
        # Everything above this line is the manager's in-process ledgers, and an
        # in-process dict is a promise about ONE server: it dies with the
        # process and a second server pointed at the same database holds an
        # empty one, so two servers each pay the first clear. The key is derived
        # here from the instance's own template id, the instance id, and the
        # server's UTC week -- never from the request -- and it is reserved
        # INSIDE this transaction, before the credit, so the insert commits with
        # the payout and a failure mid-credit rolls the marker back with it.
        #
        # Ordering note: the manager is consulted FIRST and an empty reward list
        # touches no ledger at all. That is deliberate, because the manager's
        # dict and the database can disagree (a wiped database, or a payout whose
        # database write rolled back), and reserving a key for a reward this
        # process is not about to pay would burn a claim the player is still
        # owed.
        credited: List[Dict[str, Any]] = []
        if loot:
            instance = dm.instances.get(request.instance_id)
            claim_key = dungeon_claims.claim_key_for(
                request.reward_type,
                request.instance_id,
                instance.template_id if instance else "",
                dungeons.utc_week_key(),
            )
            if dungeon_claims.reserve_claim(
                    players_db, request.player_id, request.reward_type, claim_key):
                try:
                    credited = _credit_loot(players_db, request.player_id, loot)
                except Exception:
                    # Two ledgers, two unwinds, and both are needed:
                    #  * the manager's dicts live OUTSIDE this transaction, so
                    #    unwinding here would leave the claim burned and the
                    #    player unable to ever retry;
                    #  * in no-database mode the memory ledger is also outside
                    #    the transaction, so it needs the explicit release.
                    #    In PostgreSQL the rollback removes the inserted row with
                    #    the credit, and release_claim is a documented no-op.
                    dm.release_claim(request.instance_id, request.player_id,
                                     request.reward_type)
                    dungeon_claims.release_claim(
                        players_db, request.player_id, request.reward_type, claim_key)
                    raise
            else:
                # Another process already paid this exact claim key. Hand the
                # in-process dict entry back so this process's own state stays
                # truthful about what IT paid, and answer the replay with an
                # empty reward list rather than a second payout.
                dm.release_claim(request.instance_id, request.player_id,
                                 request.reward_type)

    # Read-only structured audit of the loot payout, emitted after the dungeon
    # manager has committed the claim. safe_log_event cannot raise, so paid-out
    # loot can never be turned into a 500 by its own debug line.
    wcl.safe_log_event(
        wcl.EVENTS["DUNGEON_LOOT_CLAIM"],
        player_id=request.player_id,
        route="/dungeons/instance/claim-loot",
        currency="dungeon_loot",
        amount=len(credited) if hasattr(credited, "__len__") else None,
        outcome="claimed",
        ok=True,
        reward_type=request.reward_type,
        instance_id=request.instance_id,
        rewards=credited,
    )

    return {"ok": True, "rewards": credited}


async def _unrankable_realm_is_a_400(request, exc):
    """Turn an unrankable realm into a 400 this router's clients can read.

    ``DungeonManager.get_available_templates`` raises ``UnknownRealmError``
    for a realm its ladder cannot rank, and that refusal is deliberate -- see
    UnknownRealmError in server/dungeons.py for why the two realm ladders
    disagree. Left unhandled it escaped the /templates handler as HTTP 500
    with the plain-text body "Internal Server Error": not even JSON, so a
    client reading ``detail`` off the failure got nothing and no realm was
    named. Measured before and after this handler existed.

    Registered app-wide rather than inline in the route body because
    server/tests/test_dungeon_guard_negative_controls.py mutates the exact
    source text of that handler and asserts it occurs exactly once, so an
    inline try/except there silently disarms a security control.

    Scoped to this one exception type on purpose: a blanket
    ``except Exception`` would launder genuinely unexpected failures into a
    tidy 400 that names the wrong thing.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def register_dungeon_routes(app, players_db, player_store=None):
    """Register dungeon routes with the FastAPI app.

    The store is published onto THIS app's own ``state`` under
    :data:`PLAYERS_DB_STATE_KEY` -- a key this module owns alone -- so a
    request can find the store belonging to the app serving it, and captured
    as the module-global fallback for callers outside a request. See
    :func:`_bind_store` for why nothing here may rely on a module-level
    reference to ``main``.
    """
    global _players_db
    _players_db = players_db
    try:
        setattr(app.state, PLAYERS_DB_STATE_KEY, _players_db)
    except Exception:
        pass
    app.add_exception_handler(dungeons.UnknownRealmError,
                             _unrankable_realm_is_a_400)
    app.include_router(router)
    # PRESENT THE READ ROUTES BY THEIR PUBLIC PATH, NOT THE CONSTRAINT
    # ----------------------------------------------------------------
    # The two read routes match through the reserved-word convertor above,
    # but a client -- and any route census -- must still SEE them as
    # ``/dungeons/party/{{party_id}}``. Starlette computes that public
    # spelling once as ``path_format``; only the display attribute carries
    # the convertor.
    #
    # This MUST run after include_router, and on the app's own route
    # objects. include_router rebuilds each route from ``route.path``, so
    # cleaning the name BEFORE the include hands FastAPI a path with no
    # convertor in it, the rebuilt route compiles an unconstrained regex,
    # and the shadowing silently returns. Placed here, the regex is already
    # compiled, so only the name a caller sees changes.
    #
    # Left unfixed, this broke test_exactly_fourteen_dungeon_routes_are_
    # mounted, which compares the live route table against the literal
    # strings a client would use. A constraint that quietly changes the
    # public contract is a regression, however sound the matching is.
    for _route in app.routes:
        if getattr(_route, "path", "").startswith("/dungeons/"):
            _public = getattr(_route, "path_format", None)
            if _public and _public != _route.path:
                _route.path = _public


    # PRESENT THE READ ROUTES BY THEIR PUBLIC PATH, NOT THE CONSTRAINT
    # ----------------------------------------------------------------
    # The two read routes match via the reserved-word convertor above, but
    # a client (and a route census) must still SEE them as
    # ``/dungeons/party/{{party_id}}``. Starlette already computes the
    # public spelling once, as ``path_format``; only the display string
    # carries the convertor. Matching is done by the compiled regex, so
    # restoring the public spelling here changes what the route is CALLED
    # and nothing about which requests it accepts.
    #
    # Left unfixed this broke test_exactly_fourteen_dungeon_routes_are_
    # mounted, which compares the route table against the literal strings a
    # client would use. A constraint that changes the public contract is a
    # regression, however correct the matching is.
