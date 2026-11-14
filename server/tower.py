"""
F8: Tower / Spirit Realm Ascent (server-side config and logic).

References:
- docs/feature-roadmap.md F8: Tower / Spirit Realm Ascent
- TowerPanel exists (319 loC) but is a static UI display — no server backend
- Build: Server-side tower state (floors, paths, rewards), client interactive tower
  with path selection, floor results, reward claiming
"""
from __future__ import annotations
import os
import random
import time
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from pydantic import BaseModel, Field
# Module-level on purpose: this file uses postponed annotation evaluation,
# so an annotation naming `Request` is resolved against module globals
# rather than the registrar's local scope. See require_player_tower_404.
from fastapi import Request

# === Tower Configuration ===

# Total floors in the tower
TOWER_TOTAL_FLOORS = 100

# Paths per floor (player chooses one)
TOWER_PATHS_PER_FLOOR = 3

# Path types
TOWER_PATH_TYPES = ["combat", "elite_combat", "treasure", "rest", "event", "shop", "boss"]

# Floor enemy configuration
FLOOR_ENEMIES = {
    1: {"realm": "mortal", "stage": "early", "element": "fire", "hp_mult": 1.0, "atk_mult": 1.0},
    10: {"realm": "mortal", "stage": "peak", "element": "water", "hp_mult": 1.5, "atk_mult": 1.3},
    20: {"realm": "foundation", "stage": "early", "element": "earth", "hp_mult": 2.0, "atk_mult": 1.5},
    30: {"realm": "foundation", "stage": "middle", "element": "metal", "hp_mult": 2.5, "atk_mult": 2.0},
    40: {"realm": "core", "stage": "early", "element": "wood", "hp_mult": 3.0, "atk_mult": 2.5},
    50: {"realm": "core", "stage": "middle", "element": "wind", "hp_mult": 4.0, "atk_mult": 3.0},
    60: {"realm": "nascent_soul", "stage": "early", "element": "lightning", "hp_mult": 5.0, "atk_mult": 3.5},
    70: {"realm": "nascent_soul", "stage": "middle", "element": "light", "hp_mult": 6.0, "atk_mult": 4.0},
    80: {"realm": "soul_transformation", "stage": "early", "element": "void", "hp_mult": 8.0, "atk_mult": 5.0},
    90: {"realm": "soul_transformation", "stage": "middle", "element": "void", "hp_mult": 10.0, "atk_mult": 6.0},
    100: {"realm": "dao_seeking", "stage": "early", "element": "void", "hp_mult": 15.0, "atk_mult": 8.0, "is_boss": True},
}

# Base enemy stats
BASE_ENEMY_STATS = {
    "hp": 500,
    "atk": 50,
    "def": 25,
    "mp": 100,
    "spd": 100,
}

# Realm multipliers
REALM_MULTIPLIERS = {
    "mortal": 1.0,
    "foundation": 1.5,
    "core": 2.2,
    "nascent_soul": 3.5,
    "soul_transformation": 5.0,
    "dao_seeking": 8.0,
}

STAGE_MULTIPLIERS = {
    "early": 1.0,
    "middle": 1.15,
    "late": 1.35,
    "peak": 1.6,
}

# Floor rewards
FLOOR_REWARDS = {
    "base_spirit_stone": 100,
    "base_fateum": 10,
    "elite_bonus": 2.0,
    "treasure_bonus": 3.0,
    "boss_bonus": 10.0,
    "clear_reward": {
        "spirit_stone": 1000,
        "fateum": 100,
    },
}

# Path rewards
PATH_REWARDS = {
    "combat": {"spirit_stone": 50, "fateum": 5},
    "elite_combat": {"spirit_stone": 150, "fateum": 15},
    "treasure": {"spirit_stone": 300, "fateum": 30, "items": ["treasure_key"]},
    "rest": {"hp_restore": 0.5, "mp_restore": 0.5},
    "event": {"spirit_stone": 100, "fateum": 10, "random_item": True},
    "shop": {"discount": 0.2},
    "boss": {"spirit_stone": 1000, "fateum": 100, "items": ["boss_token"]},
}

# Tower seasons (reset every 30 days)
TOWER_SEASON_DAYS = 30


def get_floor_enemy_config(floor: int) -> dict:
    """Get enemy configuration for a specific floor."""
    # Find the closest defined floor <= current floor
    config = {}
    for f in sorted(FLOOR_ENEMIES.keys()):
        if f <= floor:
            config = FLOOR_ENEMIES[f].copy()
        else:
            break
    if not config:
        config = FLOOR_ENEMIES[1].copy()
    
    # Interpolate between defined floors for scaling
    # Add some randomness for variety
    config["hp_mult"] = config.get("hp_mult", 1.0) * random.uniform(0.9, 1.1)
    config["atk_mult"] = config.get("atk_mult", 1.0) * random.uniform(0.9, 1.1)
    
    return config


def generate_floor_enemies(floor: int, paths: int = TOWER_PATHS_PER_FLOOR) -> List[dict]:
    """Generate enemies for each path on a floor."""
    config = get_floor_enemy_config(floor)
    
    base_stats = BASE_ENEMY_STATS.copy()
    realm = config.get("realm", "mortal")
    stage = config.get("stage", "early")
    
    realm_mult = REALM_MULTIPLIERS.get(realm, 1.0)
    stage_mult = STAGE_MULTIPLIERS.get(stage, 1.0)
    hp_mult = config.get("hp_mult", 1.0)
    atk_mult = config.get("atk_mult", 1.0)
    
    enemies = []
    # Path types without boss (boss only on floors 10, 20, 30... at index 0)
    path_types = [t for t in TOWER_PATH_TYPES if t != "boss"]
    
    for i in range(paths):
        # Keep the first floor choice deterministic and rewarding; randomize
        # only the remaining choices so tests, tutorials, and first-session UX
        # receive a stable combat path instead of a no-currency rest/shop path.
        # The optional deterministic mode is for acceptance fixtures that need
        # to prove a specific item-bearing path without changing live randomness.
        if os.environ.get("WC_TOWER_DETERMINISTIC_TREASURE") == "1":
            deterministic_types = ["combat", "treasure", "boss"]
        else:
            deterministic_types = None
        path_type = deterministic_types[i] if deterministic_types else ("combat" if i == 0 else random.choice(path_types))
        
        # Boss floor override
        if floor % 10 == 0 and i == 0:
            path_type = "boss"
        
        hp = int(base_stats["hp"] * realm_mult * stage_mult * hp_mult)
        atk = int(base_stats["atk"] * realm_mult * stage_mult * atk_mult)
        def_ = int(base_stats["def"] * realm_mult * stage_mult)
        
        enemy = {
            "path_type": path_type,
            "realm": realm,
            "stage": stage,
            "element": config.get("element", "fire"),
            "hp": hp,
            "max_hp": hp,
            "atk": atk,
            "def": def_,
            "is_boss": path_type == "boss",
        }
        enemies.append(enemy)
    
    return enemies


def get_path_rewards(path_type: str, floor: int) -> dict:
    """Get rewards for completing a path.

    The row is rebuilt, never shallow-copied. `dict.copy()` hands the caller
    the SAME `items` list object the global PATH_REWARDS table holds, so one
    caller editing the rewards it was given would rewrite treasure and boss
    rewards for every player in the process. Only the list values need
    rebuilding; the scalars are immutable and safe to carry by value.
    """
    row = PATH_REWARDS.get(path_type)
    if not row:
        return {}
    base = {k: list(v) if isinstance(v, list) else v for k, v in row.items()}
    
    # Scale rewards by floor
    floor_mult = 1.0 + (floor / 100.0)
    
    for key in ["spirit_stone", "fateum"]:
        if key in base:
            base[key] = int(base[key] * floor_mult)
    
    return base


def get_floor_clear_rewards(floor: int) -> dict:
    """Get rewards for clearing a floor (all paths)."""
    base = FLOOR_REWARDS["clear_reward"].copy()
    floor_mult = 1.0 + (floor / 100.0)
    for key in base:
        base[key] = int(base[key] * floor_mult)
    return base


@dataclass
class TowerState:
    """Per-player tower state."""
    player_id: str
    current_floor: int = 0
    highest_floor: int = 0
    current_hp: int = 0
    current_mp: int = 0
    selected_path: int = 0
    selected_path_type: Optional[str] = None
    path_rewards_claimed: List[Any] = field(default_factory=list)  # [floor, path_index] markers
    floor_cleared: List[int] = field(default_factory=list)  # Floors fully cleared
    season: int = 1
    last_reset: float = field(default_factory=time.time)
    season_rewards_claimed: bool = False
    total_clears: int = 0
    best_run: int = 0  # Highest floor reached in a single run


# Global tower store (in-memory, PG version deferred)
tower_store: Dict[str, TowerState] = {}


def init_tower_store(pg_pool=None) -> Dict[str, TowerState]:
    """Initialize global tower store."""
    global tower_store
    tower_store = {}
    return tower_store


def get_tower_state(player_id: str) -> TowerState:
	"""Get or create the in-process Tower state for a player."""
	if player_id not in tower_store:
		tower_store[player_id] = TowerState(player_id=player_id)
	return tower_store[player_id]


def reset_tower_season(player_id: str) -> None:
    """Reset tower for new season."""
    state = get_tower_state(player_id)
    state.season += 1
    state.current_floor = 0
    state.current_hp = 0
    state.current_mp = 0
    state.path_rewards_claimed = []
    state.floor_cleared = []
    state.last_reset = time.time()
    state.season_rewards_claimed = False
    state.best_run = max(state.best_run, state.highest_floor)


def generate_floor_paths(floor: int) -> List[dict]:
    """Generate path choices for a floor."""
    enemies = generate_floor_enemies(floor)
    paths = []
    
    for i, enemy in enumerate(enemies):
        path_type = enemy["path_type"]
        rewards = get_path_rewards(path_type, floor)
        
        path = {
            "path_index": i,
            "path_type": path_type,
            "enemy": {
                "realm": enemy["realm"],
                "stage": enemy["stage"],
                "element": enemy["element"],
                "hp": enemy["hp"],
                "atk": enemy["atk"],
                "def": enemy["def"],
                "is_boss": enemy.get("is_boss", False),
            },
            "rewards": rewards,
            "difficulty": _get_difficulty_rating(path_type, floor),
        }
        paths.append(path)
    
    return paths


def _get_difficulty_rating(path_type: str, floor: int) -> str:
    """Get difficulty rating for a path."""
    base_difficulty = {
        "rest": 1,
        "treasure": 2,
        "event": 2,
        "shop": 1,
        "combat": 3,
        "elite_combat": 4,
        "boss": 5,
    }
    
    diff = base_difficulty.get(path_type, 3)
    
    # Scale by floor
    if floor > 50:
        diff += 1
    if floor > 80:
        diff += 1
    
    ratings = {1: "Trivial", 2: "Easy", 3: "Normal", 4: "Hard", 5: "Deadly", 6: "Impossible"}
    # Clamp to the TOP of the table, not to its default. `ratings.get(diff,
    # "Normal")` reported the EASIEST label for anything past the last key: a
    # boss path scores 5, +1 above floor 50 and +1 above floor 80, so it reached
    # 7 on floors 81-100 and was labelled "Normal" for the whole top fifth of the
    # tower -- while an ordinary combat path at the same floor read "Deadly".
    return ratings[min(diff, max(ratings))]


def apply_path_rewards(player: dict, path_type: str, floor: int, wallet_store=None) -> dict:
    """Apply path completion rewards to player."""
    rewards = get_path_rewards(path_type, floor)
    granted = {}
    
    for currency, amount in rewards.items():
        if currency in ["spirit_stone", "fateum", "destium", "souldust"]:
            # Report ONLY what the wallet actually credited. `earn` answers a
            # repeat of an idempotency key with the ORIGINAL response plus a
            # `_replayed` marker, so an unconditional `granted[currency] = amount`
            # advertised money that was never paid: claiming a second path on a
            # floor returned spirit_stone 51 granted while the balance moved by
            # 0. The player was told they had been paid and had not.
            if not (wallet_store and amount > 0):
                granted[currency] = amount
            else:
                from wallet_dispatch import earn
                # The currency MUST be part of the key. `earn` de-duplicates on
                # (key, player_id) only -- it has no notion of currency -- so a
                # key shared by two currencies makes the second one hit the
                # replay branch and be silently dropped while the response
                # still reports it as granted.
                idem = f"tower_path:{player.get('id','')}:{floor}:{path_type}:{currency}"
                resp = earn(wallet_store, player.get("id", ""), currency, amount, idem, "tower_path")
                # `_replayed` IS the wallet's dedupe branch: the key was already
                # spent, so this call credited nothing. Reporting it as granted
                # is the false receipt this branch exists to prevent.
                if not (isinstance(resp, dict) and resp.get("_replayed")):
                    granted[currency] = amount
        elif currency == "items":
            # Add items to inventory
            inventory = player.get("inventory", {})
            for item_id in amount:
                inventory[item_id] = inventory.get(item_id, 0) + 1
            granted["items"] = amount
            player["inventory"] = inventory
    
    return {"ok": True, "granted": granted}


# Export for main.py registration
# Pydantic models for tower routes (module level for FastAPI body parsing)
class TowerViewRequest(BaseModel):
    player_id: str

class TowerPathSelectRequest(BaseModel):
    player_id: str
    floor: int = Field(..., ge=1, le=TOWER_TOTAL_FLOORS)
    path_index: int = Field(..., ge=0, le=TOWER_PATHS_PER_FLOOR - 1)
    client_version: int = 0

class TowerPathClaimRequest(BaseModel):
    player_id: str
    floor: int = Field(..., ge=1, le=TOWER_TOTAL_FLOORS)
    path_index: int = Field(..., ge=0, le=TOWER_PATHS_PER_FLOOR - 1)
    client_version: int = 0

class TowerFloorClearRequest(BaseModel):
    player_id: str
    floor: int = Field(..., ge=1, le=TOWER_TOTAL_FLOORS)
    client_version: int = 0


class TowerSeasonRewardRequest(BaseModel):
    player_id: str
    client_version: int = 0


def _tower_state_to_dict(state: TowerState) -> dict:
    return {
        "current_floor": state.current_floor,
        "highest_floor": state.highest_floor,
        "current_hp": state.current_hp,
        "current_mp": state.current_mp,
        "selected_path": state.selected_path,
        "selected_path_type": state.selected_path_type,
        "path_rewards_claimed": list(state.path_rewards_claimed),
        "floor_cleared": list(state.floor_cleared),
        "season": state.season,
        "last_reset": state.last_reset,
        "season_rewards_claimed": state.season_rewards_claimed,
        "total_clears": state.total_clears,
        "best_run": state.best_run,
    }


def _as_int(value):
    """Whole number from a save field, or None when it is unreadable.

    int() raises on text that is not a number ("abc") and on values that
    are not numbers at all (None, a list). A save is data, not a contract:
    it can be truncated, hand-edited, or written by an older build. One
    unreadable field must not raise a server error out of every tower
    route that loads this player.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalise_claim_marker(m):
    """One [floor, path_index] marker, or None when the entry is unusable.

    Markers are [floor, path_index] pairs. Legacy saves stored a bare floor
    int; normalise those to path 0 so an old save cannot resurrect a claim
    that the current schema would have allowed. An entry that is neither a
    2-element pair nor a readable number is DROPPED. Keeping it would be
    worse than losing it: the payout guard tests membership with
    `[list(m) for m in state.path_rewards_claimed]`, which raises on any
    entry that is not a pair, so an unreadable marker blocks the claim it
    was standing in for AND every other claim on that player.
    """
    if isinstance(m, (list, tuple)) and len(m) == 2:
        floor = _as_int(m[0])
        path_index = _as_int(m[1])
        if floor is None or path_index is None:
            return None
        return [floor, path_index]
    floor = _as_int(m)
    if floor is None:
        return None
    return [floor, 0]


def _restore_tower_state(player: dict, state: TowerState) -> None:
    data = player.get("tower_state") if isinstance(player, dict) else None
    if not isinstance(data, dict):
        return
    for name in ("current_floor", "highest_floor", "current_hp", "current_mp", "selected_path", "season", "total_clears", "best_run"):
        if name in data:
            # Drop the FIELD, never the request. The dataclass default is
            # the safe answer for every one of these (floor 0 claims and
            # reaches nothing), so an unreadable value costs the player
            # their progress record instead of the whole tower surface.
            value = _as_int(data[name])
            if value is not None:
                setattr(state, name, value)
    if "selected_path_type" in data:
        state.selected_path_type = data["selected_path_type"]
    if "path_rewards_claimed" in data and isinstance(data.get("path_rewards_claimed"), list):
        normalised = []
        for m in data["path_rewards_claimed"]:
            marker = _normalise_claim_marker(m)
            if marker is not None:
                normalised.append(marker)
        state.path_rewards_claimed = normalised
    if "floor_cleared" in data and isinstance(data.get("floor_cleared"), list):
        cleared = [_as_int(x) for x in data["floor_cleared"]]
        state.floor_cleared = [x for x in cleared if x is not None]
    if "last_reset" in data:
        try:
            state.last_reset = float(data["last_reset"])
        except (TypeError, ValueError, OverflowError):
            pass  # keep the construction-time default
    if "season_rewards_claimed" in data:
        state.season_rewards_claimed = bool(data["season_rewards_claimed"])


def _persist_tower_state(player: dict, state: TowerState) -> None:
    player["tower_state"] = _tower_state_to_dict(state)


def _claim_marker(floor: int, path_index: int) -> List[int]:
    """Stable identity of one logical claim operation.

    A floor alone is not enough: a floor has several paths, and each path pays a
    different reward. Storing the bare floor made one path's claim suppress
    another path's claim, and made the guard depend on a bare number instead of
    on the operation being guarded.
    """
    return [int(floor), int(path_index)]


def _valid_floor(floor: int) -> bool:
    """True only for a floor that actually exists in the tower.

    Guards must be bounded on BOTH sides. An upper-bound-only check lets a
    player at current_floor == 0 claim floor 0 and every negative floor; each of
    those is a distinct payout with a distinct key, so the amount is unbounded.
    """
    return 1 <= int(floor) <= TOWER_TOTAL_FLOORS


def register_tower_routes(app, players_db, wallet_store):
    """Register tower routes. Resolve callable stores at request time.

    Also registers the tower RUN routes (server/tower_run.py) and the tower
    LADDER routes (server/tower_ladder.py). Both are mounted from here rather
    than from main.py so that shipping them does not require an edit to
    main.py, which other agents are editing concurrently.
    """
    from tower_run import register_tower_run_routes
    register_tower_run_routes(app, players_db, wallet_store)

    from tower_ladder import register_tower_ladder_routes
    register_tower_ladder_routes(app, players_db, wallet_store)

    # The ASCENT surface: 13 session-gated routes under /tower/spire/*.
    # A distinct prefix on purpose -- /tower/enter and /tower/info are
    # already the LADDER's, and FastAPI answers the first match, so reusing
    # either name here would be a route that exists in source and is dead
    # at runtime. `wallet_store` goes in as a zero-arg PROVIDER: a value
    # would be captured at registration time, so a test that swaps the
    # wallet would edit a copy no route reads.
    from tower_routes import register_tower_spire_routes
    register_tower_spire_routes(app, players_db, lambda: wallet_store)

    from fastapi import Depends, HTTPException, Request

    def db():
        return players_db() if callable(players_db) else players_db

    # The session guard, TOWER-SCOPED.
    #
    # `auth.require_player` answers 401 when a session names a player row that
    # no longer exists (auth._require_live_player). That is right for a token
    # problem, and wrong here: every handler in this registrar already answers
    # 404 "Player not found" for exactly that case, and the guard fires FIRST,
    # so the 404 was unreachable and nine contract tests measured 401.
    #
    # Scope, not a rewrite of the global guard. Three suites elsewhere pin 401
    # for the same condition on their own routes, so changing `auth` for the
    # whole server would trade this defect for a wider one. Only the ONE
    # condition named here is intercepted -- a token that resolves to a player
    # the tower store has no row for becomes a 404. Everything else, including
    # every 401, the 403 ownership refusal, and the fail-closed store-fault
    # path, is delegated UNCHANGED to `require_player`, so this cannot become
    # a hole.
    #
    # Named `require_player...` deliberately: `ownership_guard.has_auth_
    # dependency` recognises a callable by that prefix, so the shape guard
    # leaves these seven routes to this dependency instead of stacking its own
    # `require_player` on top (which would answer 401 first and undo this).
    async def require_player_tower_404(request: Request):
        from auth import (
            PlayerIdentity,
            extract_bearer_token,
            get_session_store,
            require_player,
        )

        # A missing or malformed header still 401s here, from auth.
        token = extract_bearer_token(request)
        session = get_session_store().resolve(token)
        if session is not None and session.player_id not in db():
            raise HTTPException(status_code=404, detail="Player not found")

        # Otherwise this IS require_player: same 401s, same 403, same
        # fail-closed behaviour.
        return await require_player(request)
    
    @app.get("/tower/view")
    def get_tower_view(player_id: str, identity=Depends(require_player_tower_404)):
        """Get current tower view for player."""
        store = db()
        if player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        state = get_tower_state(player_id)
        player = store[player_id]
        _restore_tower_state(player, state)
        floor = state.current_floor
        
        # Generate paths for current floor if not at max
        paths = []
        if floor < TOWER_TOTAL_FLOORS:
            paths = generate_floor_paths(floor + 1)
        
        return {
            "ok": True,
            "current_floor": floor,
            "highest_floor": state.highest_floor,
            "current_hp": state.current_hp,
            "current_mp": state.current_mp,
            "paths": paths,
            "season": state.season,
            "total_clears": state.total_clears,
            "best_run": state.best_run,
            "is_max_floor": floor >= TOWER_TOTAL_FLOORS,
        }
    
    @app.post("/tower/select-path")
    def post_tower_select_path(req: TowerPathSelectRequest, identity=Depends(require_player_tower_404)):
        """Select a path on the next floor."""
        store = db()
        if req.player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        state = get_tower_state(req.player_id)
        player = store[req.player_id]
        _restore_tower_state(player, state)
        next_floor = state.current_floor + 1
        
        if next_floor > TOWER_TOTAL_FLOORS:
            raise HTTPException(status_code=400, detail="Tower already completed")
        
        if req.path_index < 0 or req.path_index >= TOWER_PATHS_PER_FLOOR:
            raise HTTPException(status_code=400, detail="Invalid path index")
        
        # Generate paths for the floor
        paths = generate_floor_paths(next_floor)
        selected_path = paths[req.path_index]
        
        # Store selected path in state
        state.current_floor = next_floor
        state.selected_path = req.path_index
        state.selected_path_type = selected_path["path_type"]
        _persist_tower_state(player, state)
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        
        return {"ok": True, "floor": next_floor, "path": selected_path}
    
    @app.post("/tower/claim-path-reward")
    def post_tower_claim_path_reward(req: TowerPathClaimRequest, identity=Depends(require_player_tower_404)):
        """Claim reward for completing a path."""
        store = db()
        if req.player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        state = get_tower_state(req.player_id)
        player = store[req.player_id]
        _restore_tower_state(player, state)
        
        if not _valid_floor(req.floor):
            raise HTTPException(status_code=400, detail="Floor not reached")
        
        if req.floor > state.current_floor:
            raise HTTPException(status_code=400, detail="Floor not reached")
        
        marker = _claim_marker(req.floor, req.path_index)
        if marker in [list(m) for m in state.path_rewards_claimed]:
            raise HTTPException(status_code=400, detail="Reward already claimed")
        
        if req.path_index < 0 or req.path_index >= TOWER_PATHS_PER_FLOOR:
            raise HTTPException(status_code=400, detail="Invalid path index")
        
        paths = generate_floor_paths(req.floor)
        selected_path = paths[req.path_index]
        if state.selected_path_type:
            selected_path["path_type"] = state.selected_path_type
            selected_path["rewards"] = get_path_rewards(state.selected_path_type, req.floor)
        
        store = db()
        player = store[req.player_id]
        result = apply_path_rewards(player, selected_path["path_type"], req.floor, wallet_store)
        
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        
        state.path_rewards_claimed.append(marker)
        _persist_tower_state(player, state)
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        wallet = wallet_store.get(req.player_id) if wallet_store else None
        if wallet is None:
            wallet_dict, wallet_version = {}, 0
        elif isinstance(wallet, dict):
            wallet_dict = dict(wallet)
            wallet_version = int(wallet.get("version", 0) or 0)
        else:
            balance = getattr(wallet, "balance", None)
            wallet_dict = balance.to_dict() if balance is not None and hasattr(balance, "to_dict") else (dict(balance) if isinstance(balance, dict) else {})
            wallet_version = int(getattr(wallet, "version", 0) or 0)
        inventory = dict(player.get("inventory", {})) if isinstance(player.get("inventory", {}), dict) else {}
        return {"ok": True, "granted": result.get("granted", {}),
                "inventory": inventory,
                "wallet": wallet_dict, "wallet_version": wallet_version}
    
    @app.post("/tower/clear-floor")
    def post_tower_clear_floor(req: TowerFloorClearRequest, identity=Depends(require_player_tower_404)):
        """Mark floor as cleared and claim floor clear rewards."""
        store = db()
        if req.player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        state = get_tower_state(req.player_id)
        player = store[req.player_id]
        _restore_tower_state(player, state)
        
        if not _valid_floor(req.floor):
            raise HTTPException(status_code=400, detail="Floor not reached")
        
        if req.floor > state.current_floor:
            raise HTTPException(status_code=400, detail="Floor not reached")
        
        if req.floor in state.floor_cleared:
            raise HTTPException(status_code=400, detail="Floor already cleared")
        
        # Check all paths completed
        # For simplicity, we require path reward claimed for this floor
        # Markers are [floor, path_index] pairs; any path on the floor counts.
        if not any(m[0] == req.floor for m in state.path_rewards_claimed
                   if isinstance(m, (list, tuple)) and len(m) == 2):
            raise HTTPException(status_code=400, detail="Complete all paths first")
        
        store = db()
        player = store[req.player_id]
        _restore_tower_state(player, state)
        if req.floor in [int(x) for x in state.floor_cleared]:
            raise HTTPException(status_code=400, detail="Floor already cleared")
        rewards = get_floor_clear_rewards(req.floor)
        granted = {}
        
        for currency, amount in rewards.items():
            if currency in ["spirit_stone", "fateum", "destium", "souldust"]:
                from wallet_dispatch import earn
                # Currency must be part of the key: `earn` de-duplicates on
                # (key, player_id) only, so one key for two currencies drops
                # the second silently.
                idem = f"tower_floor:{req.player_id}:{req.floor}:{currency}"
                # `earn` REFUSES BY RETURN VALUE; it does not raise. Both
                # backends answer an unknown currency or a negative amount
                # with {"ok": False, ...}, and a spent idempotency key with
                # the ORIGINAL response plus `_replayed`. Discarding that
                # receipt made this line advertise money that was never
                # paid: a replayed floor clear answered
                # `granted {spirit_stone: 1020, fateum: 102}` while the
                # wallet moved by 0. The player was told they had been
                # paid and had not. Report only what the wallet credited,
                # and let a genuine refusal raise so the floor is NOT
                # marked cleared on a payout that never landed.
                resp = earn(wallet_store, req.player_id, currency, amount, idem, "tower_floor")
                if not (isinstance(resp, dict) and resp.get("ok")):
                    raise HTTPException(status_code=500,
                                        detail=(f"Wallet refused the {currency} floor-clear "
                                                f"reward: {resp.get('error') if isinstance(resp, dict) else resp}"))
                if not resp.get("_replayed"):
                    granted[currency] = amount
        
        state.floor_cleared.append(req.floor)
        state.highest_floor = max(state.highest_floor, req.floor)
        state.best_run = max(state.best_run, req.floor)
        _persist_tower_state(player, state)
        
        if req.floor == TOWER_TOTAL_FLOORS:
            state.total_clears += 1
        _persist_tower_state(player, state)
        
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        
        wallet = wallet_store.get(req.player_id) if wallet_store else None
        if wallet is None:
            wallet_dict, wallet_version = {}, 0
        elif isinstance(wallet, dict):
            wallet_dict = dict(wallet)
            wallet_version = int(wallet.get("version", 0) or 0)
        else:
            balance = getattr(wallet, "balance", None)
            wallet_dict = balance.to_dict() if balance is not None and hasattr(balance, "to_dict") else (dict(balance) if isinstance(balance, dict) else {})
            wallet_version = int(getattr(wallet, "version", 0) or 0)
        inventory = dict(player.get("inventory", {})) if isinstance(player.get("inventory", {}), dict) else {}
        return {"ok": True, "granted": granted, "tower_complete": req.floor == TOWER_TOTAL_FLOORS,
                "inventory": inventory,
                "wallet": wallet_dict, "wallet_version": wallet_version}
    
    @app.post("/tower/claim-season-reward")
    def post_tower_claim_season_reward(req: TowerSeasonRewardRequest, identity=Depends(require_player_tower_404)):
        """Claim the one-time end-of-season reward after floor 100."""
        store = db()
        if req.player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        state = get_tower_state(req.player_id)
        player = store[req.player_id]
        _restore_tower_state(player, state)
        if state.season_rewards_claimed:
            raise HTTPException(status_code=400, detail="Season reward already claimed")
        if TOWER_TOTAL_FLOORS not in state.floor_cleared:
            raise HTTPException(status_code=400, detail="Complete the Tower season first")
        rewards = {"spirit_stone": 10000, "fateum": 1000, "items": ["tower_season_token"]}
        granted = {}
        for currency, amount in (("spirit_stone", rewards["spirit_stone"]), ("fateum", rewards["fateum"])):
            from wallet_dispatch import earn
            # Currency must be part of the key: `earn` de-duplicates on
            # (key, player_id) only, so one key for two currencies drops the
            # second silently while the response still reports it as granted.
            resp = earn(wallet_store, req.player_id, currency, amount,
                        f"tower_season:{req.player_id}:{state.season}:{currency}", "tower_season")
            # Same defect as the floor clear, and MEASURED live here too:
            # `earn` refuses BY RETURN VALUE and never raises, and it marks a
            # spent key with `_replayed`. Throwing the receipt away and
            # returning `rewards` wholesale advertised spirit_stone 10000 /
            # fateum 1000 on a replay that moved the wallet by 0. Report only
            # what the wallet actually credited.
            if not (isinstance(resp, dict) and resp.get("ok")):
                raise HTTPException(status_code=500,
                                    detail=(f"Wallet refused the {currency} season reward: "
                                            f"{resp.get('error') if isinstance(resp, dict) else resp}"))
            if not resp.get("_replayed"):
                granted[currency] = amount
        inventory = player.get("inventory", {})
        if not isinstance(inventory, dict):
            inventory = {}
        inventory["tower_season_token"] = inventory.get("tower_season_token", 0) + 1
        player["inventory"] = inventory
        state.season_rewards_claimed = True
        _persist_tower_state(player, state)
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        wallet = wallet_store.get(req.player_id) if wallet_store else None
        if isinstance(wallet, dict):
            wallet_dict, wallet_version = dict(wallet), int(wallet.get("version", 0) or 0)
        elif wallet is not None:
            balance = getattr(wallet, "balance", None)
            wallet_dict = balance.to_dict() if balance is not None and hasattr(balance, "to_dict") else (dict(balance) if isinstance(balance, dict) else {})
            wallet_version = int(getattr(wallet, "version", 0) or 0)
        else:
            wallet_dict, wallet_version = {}, 0
        # `granted` reports only the currency the wallet confirmed above. The
        # token was written straight into the inventory, so it is not mediated
        # by a wallet receipt and is reported as granted.
        granted["items"] = rewards["items"]
        return {"ok": True, "granted": granted, "inventory": dict(inventory), "wallet": wallet_dict, "wallet_version": wallet_version}

    @app.post("/tower/reset")
    def post_tower_reset(req: TowerViewRequest, identity=Depends(require_player_tower_404)):
        """Advance the tower to a new season.

        This route MUST persist, and MUST advance the season only once
        TOWER_SEASON_DAYS have elapsed. Both halves ship together:

        * Persistence. Every other tower route rehydrates from
          player["tower_state"] via _restore_tower_state(). This route used
          to mutate only the in-process TowerState, so the next request
          overwrote it and the reported new_season silently reverted.

        * The 30-day window. This is what keeps the season reward safe. The
          reward is keyed per season in the wallet
          (tower_season:{player}:{season}:{currency}), so an unbounded,
          free-to-call reset is an unbounded season-reward mint. While the
          reset silently failed to persist, the season_rewards_claimed flag
          it failed to clear was the only thing holding that shut -- fixing
          persistence alone would have re-opened the double payout.
        """
        store = db()
        if req.player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        state = get_tower_state(req.player_id)
        player = store[req.player_id]
        _restore_tower_state(player, state)
        
        last_reset = float(state.last_reset or 0.0)
        elapsed_days = (time.time() - last_reset) / 86400.0
        if elapsed_days < TOWER_SEASON_DAYS:
            raise HTTPException(
                status_code=429,
                detail=("Tower season resets every %d days; %.1f days remaining"
                        % (TOWER_SEASON_DAYS, TOWER_SEASON_DAYS - elapsed_days)))
        
        old_season = state.season
        reset_tower_season(req.player_id)
        _persist_tower_state(player, state)
        if hasattr(store, "persist"):
            store.persist(req.player_id, player)
        
        return {"ok": True, "old_season": old_season, "new_season": state.season,
                "next_reset_at": state.last_reset + TOWER_SEASON_DAYS * 86400}
    
    @app.get("/tower/leaderboard")
    def get_tower_leaderboard(player_id: str, identity=Depends(require_player_tower_404)):
        """Get tower leaderboard (by highest floor)."""
        store = db()
        if player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        
        # Build the board from the STORE and rehydrate each player's persisted
        # tower_state, not from the in-process `tower_store` alone. Every other
        # tower route calls _restore_tower_state() before reading a field, so
        # after a restart /tower/view reported highest_floor 4 from the stored
        # row while this route returned an empty board and player_rank 0: the
        # same player, two different answers, one of them a lie.
        leaderboard = []
        entries = {}
        backing = db()
        rows = backing.items_bulk() if hasattr(backing, "items_bulk") else backing.items()
        for pid, p in rows:
            # A player with neither persisted nor in-process tower state has
            # never climbed, so they do not belong on the board.
            if not isinstance(p.get("tower_state"), dict) and pid not in tower_store:
                continue
            state = get_tower_state(pid)
            _restore_tower_state(p, state)
            entries[pid] = (state, p.get("name", "Unknown"))
        for pid, state in tower_store.items():
            # In-process state with no backing row still counts as a climb.
            entries.setdefault(pid, (state, "Unknown"))
        for pid, (state, name) in entries.items():
            leaderboard.append({
                "player_id": pid,
                "name": name,
                "highest_floor": state.highest_floor,
                "best_run": state.best_run,
                "total_clears": state.total_clears,
                "season": state.season,
            })
        
        leaderboard.sort(key=lambda x: (-x["highest_floor"], -x["best_run"]))
        
        # Find player's rank
        player_rank = next((i + 1 for i, p in enumerate(leaderboard) if p["player_id"] == player_id), 0)
        
        return {"ok": True, "leaderboard": leaderboard[:50], "player_rank": player_rank}