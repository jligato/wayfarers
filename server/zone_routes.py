"""
Zone routes for Wayfarer's Cycle.
Provides endpoints for zone listing, navigation, and zone-specific data.

Three groups:

* IDENTITY and LOOKUP -- the original read routes, unchanged in shape.
* MOVEMENT -- ``POST /zones/travel`` starts a journey that costs server time,
  and every read that reports a location reconciles it first.
* THE MAP -- ``GET /zones/map`` is the one call the map screen makes: it
  returns every region with its element, qi density, danger, harvest nodes and
  map coordinates, plus where the player is and what is on the road to it.

The persistence rule that applies to every write here
-----------------------------------------------------
``server/world.py`` does the reading and editing; this file is the only place
that decides HOW a write reaches the store, and the answer is always
``with players_db.transaction(player_id):``. The player store hands out
COPIES, so mutating the dict a read returned and calling nothing else would
write to a throwaway and still answer 200. See ``server/world.py`` for the
long form.
"""
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from pydantic import BaseModel, Field

from server.zones import get_zone_manager, ZoneManager, ZoneType, Zone, ZoneDataError
from server import zone_config as zc
from server import world
from server import stamina as stamina_system

# The wallet CHARGE goes through the project's own dispatcher, which branches
# on the store's backend. Importing `wallet_store_pg.spend` directly bound a
# PostgreSQL-only helper here: every in-memory store reached `store.conn` and
# died with AttributeError (HTTP 500), so the only configuration that could
# pay for a refill was the one the server does not run by default. The same
# module already ships the correct selector; this line now uses it.
from server.wallet_dispatch import spend as wallet_spend
from contextvars import ContextVar


# ---------------------------------------------------------------------------
# PER-REQUEST STORE RESOLUTION
#
# WHY THIS IS NOT JUST A MODULE GLOBAL
# ------------------------------------
# ``register_zone_routes`` used to capture both stores in module globals, and
# every handler read those globals. This tree reaches ``main.py`` two ways --
# ``import main`` from inside ``server/`` and ``from server import main`` from a
# test -- and those are two SEPARATE module objects, each with its own stores.
# The second identity's registration REBINDS these globals, so the app under
# test keeps serving from a store it never registered and every route answers
# ``404 "Player not found"`` for a player who demonstrably exists. That reads
# as a route defect and is not one.
#
# The resolution that cannot be ambiguous is the REQUEST ITSELF. ``request.app``
# IS the app handling this call, and ``app.state`` is what that app's own
# registration published. ``main.py`` is not ours to edit, so
# ``register_zone_routes`` publishes both stores onto ``app.state`` and the
# dependency below copies them into context variables for the duration of the
# call. Handlers keep calling the zero-argument accessors -- whose signatures
# server/tests/test_stamina_refill_route_live.py asserts directly -- so the
# per-request stores are delivered by the dependency rather than by a new
# argument.
#
# The module globals remain ONLY as the fallback for a call made outside a
# request, which is what keeps those direct callers working.
# ---------------------------------------------------------------------------

#: The stores serving THIS request, published per call by :func:`_bind_store`.
_request_players_db: "ContextVar[object]" = ContextVar("wc_zone_players_db", default=None)
_request_wallet_store: "ContextVar[object]" = ContextVar("wc_zone_wallet_store", default=None)

#: ``app.state`` keys this module owns. They are NAMESPACED to this module on
#: purpose: the bare ``wc_players_db`` key was previously shared with
#: dungeon_routes and npc_routes, so registering two of those modules with
#: different stores on one app made the last registration win for all of them.
#: One key per module per store means they cannot overwrite each other.
PLAYERS_DB_STATE_KEY = "wc_zone_players_db"
WALLET_STORE_STATE_KEY = "wc_zone_wallet_store"


async def _bind_store(request: Request):
    """Publish the stores of the app SERVING this request, then clear them again.

    ``request.app.state`` is the only store identity that cannot be ambiguous:
    it belongs to the app that received this exact call. They are published here
    rather than read inside each handler so the call sites keep the
    zero-argument accessors that server/tests/test_stamina_refill_route_live.py
    asserts directly.

    THE CLEARS ARE NOT OPTIONAL. These are context variables, and a set with no
    matching reset is inherited by whatever runs next in that context -- which
    is how a store belonging to one app would end up serving another app's
    request, i.e. the very bug this change exists to remove. server/
    request_logging.py documents the same rule for the request-id variable.
    Yielding makes both resets run after the response has been sent, so nothing
    is left published for a request that did not come through this router.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    players = getattr(state, PLAYERS_DB_STATE_KEY, None)
    wallet = getattr(state, WALLET_STORE_STATE_KEY, None)
    players_token = _request_players_db.set(players)
    wallet_token = _request_wallet_store.set(wallet)
    # The ``yield`` is what makes this a FastAPI GENERATOR dependency: the
    # framework resumes the function after the response has been sent, which is
    # when both resets must happen. A plain ``return`` here would run them
    # BEFORE the handler, leaving it to read the registration-time globals --
    # i.e. the bug this change exists to remove, with a green test suite,
    # because a single-app test cannot tell the two apart.
    try:
        yield players
    finally:
        _request_players_db.reset(players_token)
        _request_wallet_store.reset(wallet_token)


router = APIRouter(prefix="/zones", tags=["zones"], dependencies=[Depends(_bind_store)])


class ZoneResponse(BaseModel):
    id: str
    name: str
    description: str
    required_realm: str
    recommended_realm: str
    zone_type: str
    background: str
    music: str
    npcs: List[str]
    features: Dict[str, Any]
    connections: List[str]
    unlock_requirements: Dict[str, Any]
    daily_missions: List[str]
    accessible: bool = True
    current: bool = False
    #: World shape, from shared/zones/regions.json via server/zone_config.py.
    #: Present on every zone response so the map screen never has to make a
    #: second call per zone to learn what a region is made of.
    region: Dict[str, Any] = Field(default_factory=dict)
    #: Cultivation rate scalar for THIS player's spirit root in this zone.
    affinity: float = 1.0


class ZoneListResponse(BaseModel):
    zones: List[ZoneResponse]
    current_zone: Optional[str] = None
    recommended: List[str] = []


class MapZone(BaseModel):
    id: str
    name: str
    description: str
    zone_type: str
    required_realm: str
    recommended_realm: str
    element: str
    terrain: str
    qi_density: str
    qi_density_label: str
    qi_density_index: int
    scarred: bool
    danger: int
    danger_label: str
    encounter_difficulty: float
    reward_scalar: float
    map_x: float
    map_y: float
    reason: str
    specialty: str
    connections: List[str]
    harvest_nodes: List[Dict[str, Any]]
    accessible: bool
    current: bool
    affinity: float
    cultivating_here: bool
    travel_seconds: int
    travel_stamina: int
    locked_reason: Optional[str] = None


class MapResponse(BaseModel):
    zones: List[MapZone]
    current_zone: Optional[str] = None
    affinity: float = 1.0
    encounter_difficulty: float = 1.0
    journey: Optional[Dict[str, Any]] = None
    essence: Dict[str, int] = Field(default_factory=dict)
    recommended: List[str] = []


class TravelRequest(BaseModel):
    player_id: str = Field(..., description="Player ID")
    target_zone_id: str = Field(..., description="Zone ID to travel to")
    travel_method: str = Field(default="walk", description="walk, fly, teleport")


class TravelResponse(BaseModel):
    success: bool
    message: str
    #: The region being travelled TO. It is not the player's location yet:
    #: read ``arrives_at`` / ``seconds_until_arrival`` for that.
    new_zone: Optional[str] = None
    travel_time_seconds: int = 0
    stamina_cost: int = 0
    current_zone: Optional[str] = None
    departs_at: float = 0.0
    arrives_at: float = 0.0
    seconds_until_arrival: int = 0


class HarvestRequest(BaseModel):
    player_id: str = Field(..., description="Player ID")
    node_id: str = Field(..., description="Harvest node id, from the region's node list")


class HarvestResponse(BaseModel):
    success: bool
    zone_id: str
    node_id: str
    node_name: str
    element: str
    progress: float
    essence: int
    essence_capped: int
    affinity: float
    reward_scalar: float
    cooldown_seconds: int
    ready_at: float


class LocationResponse(BaseModel):
    zone_id: str
    zone_name: str
    element: str
    qi_density: str
    danger: int
    affinity: float
    encounter_difficulty: float
    cultivating_here: bool
    journey: Optional[Dict[str, Any]] = None
    seconds_until_arrival: int = 0
    essence: Dict[str, int] = Field(default_factory=dict)
    harvest_nodes: List[Dict[str, Any]] = Field(default_factory=list)


def zone_to_response(zone, current_zone_id: Optional[str] = None,
                     accessible: bool = True, spirit_root: str = "none",
                     zm: Optional[ZoneManager] = None) -> ZoneResponse:
    manager = zm or get_zone_manager()
    region = zone.region
    return ZoneResponse(
        id=zone.id,
        name=zone.name,
        description=zone.description,
        required_realm=zone.required_realm,
        recommended_realm=zone.recommended_realm,
        zone_type=zone.zone_type.value,
        background=zone.background,
        music=zone.music,
        npcs=zone.npcs,
        features={
            "cultivation_spots": zone.features.cultivation_spots,
            "combat_encounters": zone.features.combat_encounters,
            "gathering_nodes": zone.features.gathering_nodes,
            "sect_outpost": zone.features.sect_outpost,
            "teleport_enabled": zone.features.teleport_enabled,
        },
        connections=zone.connections,
        unlock_requirements=zone.unlock_requirements,
        daily_missions=zone.daily_missions,
        accessible=accessible,
        current=(zone.id == current_zone_id),
        region=region.to_dict() if region else {},
        affinity=manager.affinity_for(zone.id, spirit_root),
    )


# Fallbacks for callers OUTSIDE a request. Inside a request both stores come
# from ``request.app.state`` via :func:`_bind_store`; see the note above.
_players_db = None
_wallet_store = None


def _get_players_db():
    """The player store serving THIS request, else the registered fallback.

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


def _get_wallet_store():
    """The wallet store serving THIS request, else the registered fallback.

    The explicit-``None`` branch is load-bearing and is pinned by
    server/tests/test_zone_routes_contract.py: it sets the global to ``None`` to
    reach the "No wallet available for stamina refill" refusal, and a store left
    on ``app.state`` by an earlier registration must not paper over it.
    """
    if _wallet_store is None:
        return None
    bound = _request_wallet_store.get()
    if bound is not None:
        return bound
    return _wallet_store


def _player_root(player: Dict[str, Any]) -> str:
    """The spirit root string the affinity tables key on."""
    return str(player.get("spirit_root", "none") or "none")


def _get_player(players_db, player_id: str, zm: Optional[ZoneManager] = None):
    """``(player, arrival)`` with any due journey already landed and SAVED.

    Every read that reports a location goes through here. Landing inside a
    transaction is what makes an arrival durable: a landing that was computed
    and returned but never written would leave ``zone_journey`` on the record
    forever, and the map would show the player permanently on the road.

    Returns copies, because the transaction's staged row is only valid inside
    its ``with`` block. Handing the staged row to a caller that then reads it
    after commit is the copy-on-read trap wearing a different hat.
    """
    with players_db.transaction(player_id):
        player = players_db[player_id]
        arrival = world.reconcile(player, zm)
    return player, arrival


@router.get("", response_model=ZoneListResponse)
async def list_zones(
    player_id: str = Query(..., description="Player ID"),
    zone_type: Optional[str] = Query(None, description="Filter by zone type"),
    accessible_only: bool = Query(True, description="Only show accessible zones"),
):
    """List all zones, optionally filtered by type and accessibility."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, _ = _get_player(players_db, player_id, zm)
    player_realm = player.get("cultivation_realm", "mortal")
    root = _player_root(player)

    if accessible_only:
        zones = zm.get_accessible_zones(player_realm, player)
    else:
        zones = zm.get_all_zones()

    if zone_type:
        try:
            ztype = ZoneType(zone_type)
            zones = [z for z in zones if z.zone_type == ztype]
        except ValueError:
            pass

    current_zone = world.current_zone_id(player)
    recommended = [z.id for z in zm.get_recommended_zones(player_realm, player)]

    return ZoneListResponse(
        zones=[zone_to_response(z, current_zone, True, root, zm) for z in zones],
        current_zone=current_zone,
        recommended=recommended,
    )


class StaminaRefillResponse(BaseModel):
    success: bool
    message: str
    stamina: int
    max_stamina: int
    spirit_stone_spent: int
    spirit_stone_remaining: int


@router.get("/stamina")
async def get_stamina_status(
    player_id: str = Query(..., description="Player ID"),
):
    """Return the player's stamina bar, including the regeneration countdown."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return stamina_system.status(players_db[player_id])


@router.post("/stamina/refill", response_model=StaminaRefillResponse)
async def refill_stamina(
    request: TravelRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Buy a full stamina bar with spirit stones.

    The wallet is a separate server-authoritative store, so the charge goes
    through its atomic spend helper rather than a dict mutation. The
    Idempotency-Key header is required so a double-tap cannot be charged twice.

    Four refusals, none of which touch the wallet: no player, no wallet store,
    no wallet row, not enough stones, and -- added after it was measured -- a
    bar that is ALREADY full. That last one was a live currency loss: the route
    charged the full price and granted nothing, because a grant cannot raise a
    bar that already sits at the ceiling.
    """
    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    wallet_store = _get_wallet_store()
    if wallet_store is None:
        raise HTTPException(status_code=400, detail="No wallet available for stamina refill")

    w = wallet_store.get(request.player_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Player wallet not found")

    # Handle both dict (PG) and CurrencyBalance (in-memory) wallet balance
    if isinstance(w.balance, dict):
        balance = w.balance.get("spirit_stone", 0)
    else:
        balance = getattr(w.balance, "spirit_stone", 0)

    if balance < stamina_system.REFILL_COST_SPIRIT_STONE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient spirit stones. Need "
                f"{stamina_system.REFILL_COST_SPIRIT_STONE}, have {balance}"
            ),
        )

    # A refill that grants nothing must not cost anything. Before this arm the
    # route debited the wallet at the full price and then handed back a bar
    # that was already full, so the player lost REFILL_COST_SPIRIT_STONE and
    # received zero stamina. Measured, and previously pinned in the zone
    # contract test.
    #
    # The bar is read through current_stamina -- the same regeneration-aware
    # reading GET /zones/stamina serves -- so a player whose bar refilled itself
    # while they were away is refused here too, rather than charged for stamina
    # the server was already going to hand back.
    player_now = players_db[request.player_id]
    have = stamina_system.current_stamina(player_now)
    if have >= stamina_system.MAX_STAMINA:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Stamina already full ({have}/{stamina_system.MAX_STAMINA}); "
                "there is nothing to refill"
            ),
        )

    result = wallet_spend(
        wallet_store,
        request.player_id,
        "spirit_stone",
        stamina_system.REFILL_COST_SPIRIT_STONE,
        w.version,
        idempotency_key,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"Stamina refill failed: {result}")

    # Charge succeeded; now grant the stamina on the server's player record.
    # The grant and the record write share one transaction so a crash between
    # them cannot leave the player having paid for stamina they never got.
    with players_db.transaction(request.player_id):
        player = players_db[request.player_id]
        stamina_system.refill(player, {"spirit_stone": balance})
        if hasattr(players_db, "persist"):
            players_db.persist(request.player_id, player)
    # Read the granted value back through the STORE, not through the dict the
    # transaction staged. The staged row is the transaction's working copy;
    # reading a field off it after commit is a value nothing else can see.
    stored = players_db[request.player_id]

    w_after = wallet_store.get(request.player_id)
    return StaminaRefillResponse(
        success=True,
        message="Stamina refilled",
        stamina=int(stored.get("stamina", stamina_system.MAX_STAMINA)),
        max_stamina=stamina_system.MAX_STAMINA,
        spirit_stone_spent=stamina_system.REFILL_COST_SPIRIT_STONE,
        spirit_stone_remaining=int(
            w_after.balance.get("spirit_stone", 0) if w_after and isinstance(w_after.balance, dict) else getattr(w_after.balance, "spirit_stone", 0) if w_after else 0
        ),
    )


@router.get("/map", response_model=MapResponse)
async def get_world_map(
    player_id: str = Query(..., description="Player ID"),
    travel_method: str = Query("walk", description="Method used to price every hop"),
):
    """The whole world in one call, priced from where the player stands.

    Every zone comes back with its element, qi density, danger tier, harvest
    nodes and map coordinate, plus what it would cost to get there NOW. The
    map screen therefore never needs a per-zone follow-up request, and the
    prices it shows are produced by the same function ``POST /zones/travel``
    charges with, so a quoted cost cannot drift from a charged one.
    """
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, _ = _get_player(players_db, player_id, zm)
    realm = player.get("cultivation_realm", "mortal")
    root = _player_root(player)
    current = world.current_zone_id(player)
    travelling = world.is_travelling(player)

    rows: List[MapZone] = []
    for zone in zm.get_all_zones():
        region = zone.region
        if region is None:
            continue
        accessible = zm.can_access_zone(zone.id, realm, player)
        locked_reason = None
        if not accessible:
            locked_reason = f"Requires {zone.required_realm.replace('_', ' ')}"
        priced = zm.travel_quote(current, zone.id, travel_method, realm)
        rows.append(MapZone(
            id=zone.id,
            name=zone.name,
            description=zone.description,
            zone_type=zone.zone_type.value,
            required_realm=zone.required_realm,
            recommended_realm=zone.recommended_realm,
            element=region.element,
            terrain=region.terrain,
            qi_density=region.qi_density,
            qi_density_label=zc.QI_DENSITY_TIERS[region.qi_density]["label"],
            qi_density_index=region.qi_index,
            scarred=region.scarred,
            danger=region.danger,
            danger_label=region.danger_stats["label"],
            encounter_difficulty=region.danger_stats["encounter_difficulty"],
            reward_scalar=region.danger_stats["reward"],
            map_x=region.map_x,
            map_y=region.map_y,
            reason=region.reason,
            specialty=region.specialty,
            connections=zone.connections,
            harvest_nodes=[
                {"id": n.id, "name": n.name, "element": n.element, "note": n.note}
                for n in region.harvest_nodes
            ],
            accessible=accessible,
            current=(zone.id == current),
            affinity=zm.affinity_for(zone.id, root),
            cultivating_here=(zone.id == current and not travelling),
            travel_seconds=0 if zone.id == current else priced["travel_seconds"],
            travel_stamina=0 if zone.id == current else world.travel_stamina(
                priced, current, zone.id, realm, zm
            ),
            locked_reason=locked_reason,
        ))

    return MapResponse(
        zones=rows,
        current_zone=current,
        affinity=zm.affinity_for(current, root),
        encounter_difficulty=world.encounter_difficulty(current, zm),
        journey=world.zone_state(player) or None,
        essence=world.essence_held(player),
        recommended=[z.id for z in zm.get_recommended_zones(realm, player)],
    )


@router.get("/location", response_model=LocationResponse)
async def get_location(
    player_id: str = Query(..., description="Player ID"),
):
    """Where the player is, what it is worth, and whether they are on the road.

    This is the honest answer to "where am I". It reconciles first, so a
    journey whose time is up is landed and SAVED by the time this returns.
    """
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, arrival = _get_player(players_db, player_id, zm)
    zone_id = world.current_zone_id(player)
    zone = zm.get_zone(zone_id)
    region = zm.get_region(zone_id)
    if zone is None or region is None:
        raise HTTPException(status_code=404, detail=f"Unknown region: {zone_id}")

    root = _player_root(player)
    node_rows = []
    for node in region.harvest_nodes:
        ready, left = world.can_harvest(player, node.id)
        node_rows.append({
            "id": node.id, "name": node.name, "element": node.element,
            "note": node.note, "ready": ready, "seconds_until_ready": left,
        })

    return LocationResponse(
        zone_id=zone_id,
        zone_name=zone.name,
        element=region.element,
        qi_density=region.qi_density,
        danger=region.danger,
        affinity=zm.affinity_for(zone_id, root),
        encounter_difficulty=region.danger_stats["encounter_difficulty"],
        cultivating_here=not world.is_travelling(player),
        journey=world.zone_state(player) or None,
        seconds_until_arrival=world.seconds_until_arrival(player),
        essence=world.essence_held(player),
        harvest_nodes=node_rows,
    )


@router.get("/{zone_id}", response_model=ZoneResponse)
async def get_zone(
    zone_id: str,
    player_id: str = Query(..., description="Player ID"),
):
    """Get detailed information about a specific zone."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, _ = _get_player(players_db, player_id, zm)
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    player_realm = player.get("cultivation_realm", "mortal")
    accessible = zm.can_access_zone(zone_id, player_realm, player)

    return zone_to_response(
        zone, world.current_zone_id(player), accessible, _player_root(player), zm
    )


@router.get("/{zone_id}/connected", response_model=List[ZoneResponse])
async def get_connected_zones(
    zone_id: str,
    player_id: str = Query(..., description="Player ID"),
):
    """Get zones connected to the specified zone."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, _ = _get_player(players_db, player_id, zm)
    player_realm = player.get("cultivation_realm", "mortal")
    connected = zm.get_connected_zones(zone_id, player_realm, player)
    current_zone = world.current_zone_id(player)
    root = _player_root(player)
    return [zone_to_response(z, current_zone, True, root, zm) for z in connected]


@router.get("/{zone_id}/npcs")
async def get_zone_npcs(
    zone_id: str,
    player_id: str = Query(..., description="Player ID"),
):
    """Get NPCs available in a zone."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    npcs = zm.get_zone_npcs(zone_id)
    return {"zone_id": zone_id, "npcs": npcs}


@router.get("/{zone_id}/features")
async def get_zone_features(
    zone_id: str,
    player_id: str = Query(..., description="Player ID"),
):
    """Get features available in a zone (cultivation spots, combat encounters, gathering nodes)."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    features = zm.get_zone_features(zone_id)
    if not features:
        raise HTTPException(status_code=404, detail="Zone not found")

    return {
        "zone_id": zone_id,
        "cultivation_spots": features.cultivation_spots,
        "combat_encounters": features.combat_encounters,
        "gathering_nodes": features.gathering_nodes,
        "sect_outpost": features.sect_outpost,
        "teleport_enabled": features.teleport_enabled,
    }


@router.get("/{zone_id}/daily-missions")
async def get_zone_daily_missions(
    zone_id: str,
    player_id: str = Query(..., description="Player ID"),
):
    """Get daily missions available in a zone."""
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    zone = zm.get_zone(zone_id)
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")

    missions = zm.get_zone_daily_missions(zone_id)
    return {"zone_id": zone_id, "daily_missions": missions}


@router.post("/travel", response_model=TravelResponse)
async def travel_to_zone(
    request: TravelRequest,
):
    """Set out for a zone. The move completes when the server clock says so.

    Travel is a JOURNEY, not a jump. This handler spends the stamina, writes
    the arrival timestamp, and returns; ``current_zone`` is untouched until
    some later read observes ``now >= arrives_at`` and lands the player (see
    ``world.reconcile``). That is what makes being in a zone worth something:
    the region you left keeps earning for nobody while you are on the road,
    and the region you arrive at starts earning only when you are actually
    standing in it.

    Everything that can refuse the move -- realm gate, adjacency for a walking
    journey, teleport array presence, an unfinished journey, stamina -- is
    checked and priced BEFORE the stamina is spent, inside one transaction. A
    refusal raises, which rolls the transaction back, so there is no path that
    charges a player for a journey that did not start.
    """
    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()

    with players_db.transaction(request.player_id):
        player = players_db[request.player_id]
        world.reconcile(player, zm)
        try:
            terms = world.check_travel(
                zm, player, request.target_zone_id, request.travel_method
            )
        except world.WorldAccessError as exc:
            # Reachability, not a malformed request. A client that gets 403
            # knows to send the player away to train; 400 would read as "try a
            # different route", which is useless advice for a realm wall.
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except world.WorldError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            # spend() applies pending time-based regeneration first, so a
            # player who was away long enough is never blocked by a stale bar.
            stamina_system.spend(player, terms["stamina_cost"])
        except stamina_system.StaminaError as exc:
            have = stamina_system.current_stamina(player)
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stamina. Need {terms['stamina_cost']}, have {have}",
            ) from exc

        player[world.JOURNEY_KEY] = world.build_journey(terms)
        if hasattr(players_db, "persist"):
            players_db.persist(request.player_id, player)
        origin = world.current_zone_id(player)
        left = world.seconds_until_arrival(player)

    return TravelResponse(
        success=True,
        message=f"Setting out for {zm.get_zone(terms['to_zone_id']).name}.",
        new_zone=terms["to_zone_id"],
        travel_time_seconds=terms["travel_seconds"],
        stamina_cost=terms["stamina_cost"],
        current_zone=origin,
        departs_at=terms["departs_at"],
        arrives_at=terms["arrives_at"],
        seconds_until_arrival=left,
    )


@router.post("/harvest", response_model=HarvestResponse)
async def harvest_node(request: HarvestRequest):
    """Gather one node in the region the player is standing in.

    Refused while the player is on the road, refused for a node that does not
    grow in this region, and refused while the node is regrowing. Pays a small
    amount of cultivation progress scaled by the region's danger and this
    player's affinity, plus a little of the node's own element as essence. It
    pays no currency and there is no route that turns essence into currency.
    """
    players_db = _get_players_db()
    if request.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    with players_db.transaction(request.player_id):
        player = players_db[request.player_id]
        world.reconcile(player, zm)
        try:
            result = world.harvest(
                request.node_id, player, _player_root(player), zm
            )
        except world.WorldAccessError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except world.WorldError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if hasattr(players_db, "persist"):
            players_db.persist(request.player_id, player)

    return HarvestResponse(success=True, **result)


@router.post("/arrive", response_model=LocationResponse)
async def force_arrival(
    player_id: str = Query(..., description="Player ID"),
):
    """Land any journey whose time is up. Exists so a client can settle a
    countdown without waiting for an unrelated read to do it.

    It calls the same ``world.reconcile`` every read uses, so it can only
    land a journey the clock has already paid for -- it cannot be used to skip
    a journey.
    """
    players_db = _get_players_db()
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    zm = get_zone_manager()
    player, _ = _get_player(players_db, player_id, zm)
    zone_id = world.current_zone_id(player)
    zone = zm.get_zone(zone_id)
    region = zm.get_region(zone_id)
    if zone is None or region is None:
        raise HTTPException(status_code=404, detail=f"Unknown region: {zone_id}")

    return LocationResponse(
        zone_id=zone_id,
        zone_name=zone.name,
        element=region.element,
        qi_density=region.qi_density,
        danger=region.danger,
        affinity=zm.affinity_for(zone_id, _player_root(player)),
        encounter_difficulty=region.danger_stats["encounter_difficulty"],
        cultivating_here=not world.is_travelling(player),
        journey=world.zone_state(player) or None,
        seconds_until_arrival=world.seconds_until_arrival(player),
        essence=world.essence_held(player),
        harvest_nodes=[
            {"id": n.id, "name": n.name, "element": n.element, "note": n.note,
             "ready": world.can_harvest(player, n.id)[0],
             "seconds_until_ready": world.can_harvest(player, n.id)[1]}
            for n in region.harvest_nodes
        ],
    )


def register_zone_routes(app, players_db, player_store=None, wallet_store=None):
    """Register zone routes with the FastAPI app.

    `wallet_store` is OPTIONAL and defaults to the wallet the player store was
    built with. `main.py` constructs `players_db = PlayerStore(wallet_store)`,
    so the wallet is already reachable from the store this function is handed;
    requiring the caller to pass it a second time meant the single wiring line
    that supplied it was simply absent, the module global stayed `None` for the
    life of the process, and every paid route here answered

        400 {"detail": "No wallet available for stamina refill"}

    That refusal reads like a feature switch rather than a missing wire, which
    is why it survived: it is a plausible answer. Resolving the wallet from the
    store keeps the existing signature and every explicit caller working, and
    removes the need for the second wiring line entirely.
    """
    global _players_db, _wallet_store
    _players_db = players_db
    if wallet_store is None:
        wallet_store = getattr(players_db, "wallet_store", None)
    _wallet_store = wallet_store
    # Published onto THIS app's own state, so a request finds the stores
    # belonging to the app serving it. See _bind_store for why nothing here
    # may rely on a module-level reference to `main`.
    try:
        setattr(app.state, PLAYERS_DB_STATE_KEY, _players_db)
        setattr(app.state, WALLET_STORE_STATE_KEY, _wallet_store)
    except Exception:
        pass
    app.include_router(router)
