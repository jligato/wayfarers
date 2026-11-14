"""Server-authoritative Sect Wall: contribution, ranks, and a shared wall.

Design notes (original Wayfarer's Cycle design):
- All state lives on the canonical player record under `player["sect_wall"]`,
  so it is written through `PlayerStore` and survives a server restart and a
  fresh PostgreSQL-backed process (it rides the `extra_state` JSON column).
- Ranks are derived from lifetime contribution; nothing trusts a client rank.
- The wall is shared: every post is stored on its author's own player record
  and the feed is aggregated by scanning members of the same sect, so the wall
  has no second source of truth to keep in sync.
- Every mutation that spends currency or creates a post is idempotent through
  an `Idempotency-Key` receipt, bounded to RECEIPT_LIMIT entries.

Routes are registered by `register_sect_wall_routes`; nothing here imports
`server.main`, so the module is safe to import from a test harness.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from identity import Principal, require_admin
from idem_gate import require_idempotency_key

logger = logging.getLogger("sect_wall")

WEEK_SECONDS = 7 * 24 * 3600
WEEKLY_CONTRIBUTION_CAP = 2000
CONTRIBUTION_PER_STONE = 2
MAX_POSTS = 20
MAX_PINNED_POSTS = 3
MAX_POST_LENGTH = 200
POST_COOLDOWN_SECONDS = 30
RECEIPT_LIMIT = 50

# Ordered low -> high. `can_post` and `can_pin` are the only privileges the
# wall grants; rank itself is never a client-supplied value.
RANKS: list[dict[str, Any]] = [
    {"id": "outer_disciple", "name": "Outer Disciple", "min_lifetime": 0,
     "can_post": False, "can_pin": False},
    {"id": "inner_disciple", "name": "Inner Disciple", "min_lifetime": 500,
     "can_post": True, "can_pin": False},
    {"id": "core_disciple", "name": "Core Disciple", "min_lifetime": 2000,
     "can_post": True, "can_pin": False},
    {"id": "elder", "name": "Elder", "min_lifetime": 8000,
     "can_post": True, "can_pin": True},
    {"id": "sect_master", "name": "Sect Master", "min_lifetime": 25000,
     "can_post": True, "can_pin": True},
]


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "sect_id": None,
        "contribution": 0,
        "lifetime_contribution": 0,
        "week_started_at": 0.0,
        "week_contribution": 0,
        "last_post_at": 0.0,
        "posts": [],
        "receipts": {},
        "receipt_order": [],
    }


# --------------------------------------------------------------------------
# ranks
# --------------------------------------------------------------------------
def rank_for(lifetime: int) -> dict[str, Any]:
    """Highest rank whose lifetime threshold the player has reached."""
    best = RANKS[0]
    for rank in RANKS:
        if lifetime >= rank["min_lifetime"]:
            best = rank
    return best


def next_rank(lifetime: int) -> dict[str, Any] | None:
    for rank in RANKS:
        if lifetime < rank["min_lifetime"]:
            return rank
    return None


def rank_catalog() -> list[dict[str, Any]]:
    return [deepcopy(rank) for rank in RANKS]


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------
def _state_of(player: dict) -> dict[str, Any]:
    """Returns the player's wall state, healing missing keys from a legacy row."""
    state = player.get("sect_wall")
    if not isinstance(state, dict):
        state = empty_state()
        player["sect_wall"] = state
    for key, value in empty_state().items():
        state.setdefault(key, deepcopy(value))
    if not isinstance(state.get("posts"), list):
        state["posts"] = []
    if not isinstance(state.get("receipts"), dict):
        state["receipts"] = {}
    # MEASURED: the map check above is not enough on its own, because a dict can
    # hold a value of the wrong type. The replay read does
    # `prior.get("fingerprint")` on whatever the key holds, so a save whose
    # receipts map carried a string, a number or a list under one key raised
    # AttributeError. That is neither an HTTPException nor the ValueError the
    # callers already handle, so it escaped the route and answered a plain 500 on
    # every paying request for that player. Same rule as equipment.py: a key
    # holding None or [] was already read as "no receipt here" so the request
    # proceeds as fresh; this makes a string, a number and a list agree with them
    # instead of crashing, and the unreadable value is dropped. Nothing real is
    # lost -- the only writer here is _append_receipt, which always stores a dict,
    # so a non-dict value was never a receipt.
    for _key in [k for k, v in state["receipts"].items() if not isinstance(v, dict)]:
        state["receipts"].pop(_key, None)

    if not isinstance(state.get("receipt_order"), list):
        state["receipt_order"] = []
    return state


def _roll_week(state: dict[str, Any], now: float) -> None:
    """Resets the weekly contribution counter when the seven-day window lapses."""
    started = float(state.get("week_started_at") or 0.0)
    if started <= 0.0:
        state["week_started_at"] = now
        return
    if now - started < WEEK_SECONDS:
        return
    elapsed_weeks = int((now - started) // WEEK_SECONDS)
    state["week_started_at"] = started + elapsed_weeks * WEEK_SECONDS
    state["week_contribution"] = 0


def stone_balance(player: dict) -> int:
    stones = player.get("spirit_stones")
    if isinstance(stones, dict):
        return max(0, int(stones.get("low_grade", 0)))
    return max(0, int(stones or 0))


def _spend_stones(player: dict, amount: int) -> None:
    stones = player.get("spirit_stones")
    if isinstance(stones, dict):
        stones["low_grade"] = max(0, int(stones.get("low_grade", 0)) - amount)
    else:
        player["spirit_stones"] = max(0, int(stones or 0) - amount)


def _require_player(players_db, player_id: str) -> dict:
    try:
        return players_db[player_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Player not found") from exc


def _require_member(player: dict) -> str:
    sect_id = player.get("sect_id") or _state_of(player).get("sect_id")
    if not sect_id:
        raise HTTPException(status_code=409, detail="Not in a sect")
    return str(sect_id)


# --------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------
def _canonical_payload(operation: str, **fields: Any) -> str:
    return json.dumps({"operation": operation, **fields}, sort_keys=True, separators=(",", ":"))


def _receipt_key(player_id: str, idem: str) -> str:
    return hashlib.sha256(f"{player_id}\0{idem}".encode()).hexdigest()


def _append_receipt(state: dict, key: str, fingerprint: str, response: dict) -> None:
    state["receipts"][key] = {"fingerprint": fingerprint, "response": deepcopy(response)}
    state["receipt_order"].append(key)
    while len(state["receipt_order"]) > RECEIPT_LIMIT:
        state["receipts"].pop(state["receipt_order"].pop(0), None)


def _replayed(state: dict, key: str, fingerprint: str) -> dict | None:
    prior = state.get("receipts", {}).get(key)
    if not prior:
        return None
    if prior.get("fingerprint") != fingerprint:
        raise HTTPException(status_code=409, detail="Idempotency key reused with different payload")
    return {**deepcopy(prior["response"]), "_replayed": True}


def _public(result: dict) -> dict:
    public = deepcopy(result)
    public.pop("_replayed", None)
    return public


# --------------------------------------------------------------------------
# wall feed
# --------------------------------------------------------------------------
def _post_view(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_id": post.get("post_id"),
        "author_id": post.get("author_id"),
        "author_name": post.get("author_name", "Unknown"),
        "author_rank": post.get("author_rank", "outer_disciple"),
        "message": post.get("message", ""),
        "pinned": bool(post.get("pinned")),
        "created_at": post.get("created_at", 0.0),
    }


def wall_feed(players_db, sect_id: str, limit: int = MAX_POSTS) -> list[dict[str, Any]]:
    """Aggregates every member's posts, pinned first, then newest first."""
    collected: list[dict[str, Any]] = []
    # One bulk read of the whole population; items() costs one query per player.
    rows = players_db.items_bulk() if hasattr(players_db, "items_bulk") else list(players_db.items())
    for _pid, player in rows:
        state = player.get("sect_wall")
        if not isinstance(state, dict):
            continue
        if str(state.get("sect_id") or "") != str(sect_id):
            continue
        for post in state.get("posts", []):
            if isinstance(post, dict):
                collected.append(_post_view(post))
    collected.sort(key=lambda post: (not post["pinned"], -float(post.get("created_at", 0.0))))
    return collected[:max(1, min(int(limit or MAX_POSTS), MAX_POSTS))]


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------
def state_view(player: dict, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    state = _state_of(player)
    _roll_week(state, now)
    lifetime = max(0, int(state.get("lifetime_contribution", 0)))
    rank = rank_for(lifetime)
    upcoming = next_rank(lifetime)
    week_started = float(state.get("week_started_at") or now)
    cooldown_left = POST_COOLDOWN_SECONDS - (now - float(state.get("last_post_at") or 0.0))
    return {
        "ok": True,
        "sect_id": state.get("sect_id"),
        "rank": rank["id"],
        "rank_name": rank["name"],
        "can_post": bool(rank["can_post"]),
        "can_pin": bool(rank["can_pin"]),
        "next_rank": None if upcoming is None else {
            "id": upcoming["id"], "name": upcoming["name"],
            "min_lifetime": upcoming["min_lifetime"], "remaining": max(0, upcoming["min_lifetime"] - lifetime),
        },
        "ranks": rank_catalog(),
        "contribution": max(0, int(state.get("contribution", 0))),
        "lifetime_contribution": lifetime,
        "week_contribution": max(0, int(state.get("week_contribution", 0))),
        "week_cap": WEEKLY_CONTRIBUTION_CAP,
        "week_reset_in": max(0, int(WEEK_SECONDS - (now - week_started))),
        "contribution_per_stone": CONTRIBUTION_PER_STONE,
        "stones": stone_balance(player),
        "post_count": len(state.get("posts", [])),
        "post_cooldown": max(0, int(cooldown_left)),
        "max_post_length": MAX_POST_LENGTH,
        "max_pinned_posts": MAX_PINNED_POSTS,
        "server_now": now,
    }


def feed_view(player: dict, players_db, limit: int = MAX_POSTS, now: float | None = None) -> dict[str, Any]:
    view = state_view(player, now)
    if not view["sect_id"]:
        view["posts"] = []
        return view
    view["posts"] = wall_feed(players_db, view["sect_id"], limit)
    return view


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Activity-log bridge
# --------------------------------------------------------------------------
#
# WHY THIS IS A HOOK
# ------------------
# The brief asked for "server computes contributions from activity logs", and
# this file is where two of the three existing contribution counters live. The
# correct fix is NOT a fourth counter here. It is for the action that already
# happened to be recorded in ONE place every reader shares.
#
# `contribute()` already computes contribution correctly: `stones *
# CONTRIBUTION_PER_STONE`, with the stones genuinely debited. That is the right
# shape and it must not be reimplemented. What is missing is only the record
# that the activity OCCURRED, which sect_routes.SectV2Store needs for its daily
# caps and its settlement.
#
# So this hook hands over the EVENT -- player, what they did, and how much was
# actually paid -- and never a score. sect_routes decides what it is worth from
# its own ACTIVITY_POINTS table and applies its own caps. The number that
# crosses this boundary is measured from a completed server-side transaction,
# never read from a request field.
#
# The hook is OPTIONAL and defaults to None, so this module keeps its
# no-import-coupling property: sect_wall imports nothing from sect_routes, and a
# caller that never installs a bridge sees the original behaviour exactly.
#
# A raising bridge must never break a completed contribution. The spend, the
# receipt and the ledger write have all committed by this point, so refusing
# the response now would tell a player their stones were not taken when they
# were.
_ACTIVITY_BRIDGE = None


def set_activity_bridge(fn) -> None:
    """Install the callback. Signature: fn(player_id, action, unit, now) -> None."""
    global _ACTIVITY_BRIDGE
    _ACTIVITY_BRIDGE = fn


def _bridge_activity(player_id: str, action: str, unit: int = 1) -> None:
    if _ACTIVITY_BRIDGE is None:
        return
    try:
        _ACTIVITY_BRIDGE(player_id, action, int(unit), time.time())
    except Exception:  # noqa: BLE001 -- never fail a completed spend here
        logger.warning("sect activity bridge failed for %s/%s", player_id, action,
                       exc_info=True)


def contribute(players_db, player_id: str, stones: int, idem: str) -> dict[str, Any]:
    if stones <= 0:
        raise HTTPException(status_code=400, detail="Stone amount must be positive")
    fingerprint = _canonical_payload("contribute", stones=int(stones))
    key = _receipt_key(player_id, idem)
    # Existence check BEFORE the transaction opens. PlayerStore.transaction()
    # preloads player_id and raises a bare KeyError for an unknown id, so a check
    # written INSIDE the `with` is unreachable for exactly the case it was written
    # for: the caller got a 500 instead of the 404 below.
    # The value is deliberately discarded -- outside a transaction __getitem__
    # hands back a detached copy, and the write must use the live row.
    _require_player(players_db, player_id)
    with players_db.transaction(player_id):
        player = _require_player(players_db, player_id)
        state = _state_of(player)
        replay = _replayed(state, key, fingerprint)
        if replay:
            return replay
        _require_member(player)
        now = time.time()
        _roll_week(state, now)
        if stone_balance(player) < stones:
            raise HTTPException(status_code=409, detail="Not enough spirit stones")
        week_used = int(state.get("week_contribution", 0))
        if week_used + stones * CONTRIBUTION_PER_STONE > WEEKLY_CONTRIBUTION_CAP:
            raise HTTPException(status_code=409, detail="Weekly contribution cap reached")
        _spend_stones(player, stones)
        gained = stones * CONTRIBUTION_PER_STONE
        state["contribution"] = int(state.get("contribution", 0)) + gained
        state["lifetime_contribution"] = int(state.get("lifetime_contribution", 0)) + gained
        state["week_contribution"] = week_used + gained
        # Cosmetics buy sect banners with `player.sect.contribution`; keep the
        # two views on the same balance instead of leaving a second currency.
        player.setdefault("sect", {})["contribution"] = int(state["contribution"])
        result = {
            "ok": True, "player_id": player_id, "stones_spent": stones,
            "contribution_gained": gained, "rank": rank_for(int(state["lifetime_contribution"]))["id"],
        }
        _append_receipt(state, key, fingerprint, result)
    # Feed the sect v2 ACTIVITY LOG. See _bridge_activity for why this is a
    # hook and not a direct call.
    _bridge_activity(player_id, "sect_wall_contribute", stones)
    return result


def create_post(players_db, player_id: str, message: str, pin: bool, idem: str) -> dict[str, Any]:
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(text) > MAX_POST_LENGTH:
        raise HTTPException(status_code=400, detail=f"Message longer than {MAX_POST_LENGTH} characters")
    fingerprint = _canonical_payload("post", message=text, pin=bool(pin))
    key = _receipt_key(player_id, idem)
    # Existence check BEFORE the transaction opens; same reason and same
    # discarded-value rule as `contribute` above.
    _require_player(players_db, player_id)
    with players_db.transaction(player_id):
        player = _require_player(players_db, player_id)
        state = _state_of(player)
        replay = _replayed(state, key, fingerprint)
        if replay:
            return replay
        sect_id = _require_member(player)
        lifetime = int(state.get("lifetime_contribution", 0))
        rank = rank_for(lifetime)
        if not rank["can_post"]:
            raise HTTPException(status_code=403, detail="Rank cannot post on the sect wall")
        if pin and not rank["can_pin"]:
            raise HTTPException(status_code=403, detail="Rank cannot pin a sect wall message")
        now = time.time()
        cooldown_left = POST_COOLDOWN_SECONDS - (now - float(state.get("last_post_at") or 0.0))
        if cooldown_left > 0:
            raise HTTPException(status_code=409, detail="Sect wall post cooldown active")
        if pin:
            pinned = sum(1 for post in state["posts"] if post.get("pinned"))
            if pinned >= MAX_PINNED_POSTS:
                raise HTTPException(status_code=409, detail="Pinned message limit reached")
        post = {
            "post_id": hashlib.sha256(f"{player_id}\0{now}\0{text}".encode()).hexdigest()[:16],
            "author_id": player_id,
            "author_name": player.get("name", "Unknown"),
            "author_rank": rank["id"],
            "message": text,
            "pinned": bool(pin),
            "created_at": now,
        }
        state["posts"].append(post)
        while len(state["posts"]) > MAX_POSTS:
            state["posts"].pop(0)
        state["last_post_at"] = now
        result = {"ok": True, "player_id": player_id, "post": _post_view(post), "post_count": len(state["posts"])}
        _append_receipt(state, key, fingerprint, result)
    return result


def unpin_post(players_db, player_id: str, post_id: str) -> dict[str, Any]:
    # Existence check BEFORE the transaction opens; same reason and same
    # discarded-value rule as `contribute` above.
    _require_player(players_db, player_id)
    with players_db.transaction(player_id):
        player = _require_player(players_db, player_id)
        state = _state_of(player)
        _require_member(player)
        rank = rank_for(int(state.get("lifetime_contribution", 0)))
        if not rank["can_pin"]:
            raise HTTPException(status_code=403, detail="Rank cannot moderate the sect wall")
        for post in state["posts"]:
            if post.get("post_id") == post_id:
                post["pinned"] = False
                return {"ok": True, "post_id": post_id, "pinned": False}
    raise HTTPException(status_code=404, detail="Post not found on this member's wall")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
class ContributeRequest(BaseModel):
    player_id: str = Field(min_length=1)
    stones: int = Field(ge=1, le=100000)


class PostRequest(BaseModel):
    player_id: str = Field(min_length=1)
    message: str
    pin: bool = False


class PlayerActionRequest(BaseModel):
    player_id: str = Field(min_length=1)


class UnpinRequest(BaseModel):
    player_id: str = Field(min_length=1)
    post_id: str = Field(min_length=1)


def register_sect_wall_routes(app: FastAPI, players_db_accessor) -> None:
    # Registered before "/sect-wall/{player_id}" so "ranks" is not read as an id.
    @app.get("/sect-wall/ranks")
    def get_sect_wall_ranks():
        return {"ok": True, "ranks": rank_catalog()}

    @app.get("/sect-wall/{player_id}")
    def get_sect_wall(player_id: str):
        store = players_db_accessor()
        return feed_view(_require_player(store, player_id), store)

    @app.post("/sect-wall/contribute")
    def post_contribute(request: Request, req: ContributeRequest):
        idem = require_idempotency_key(request, route="sect-wall-contribute")
        result = contribute(players_db_accessor(), req.player_id, req.stones, idem)
        store = players_db_accessor()
        return {**_public(result), "view": feed_view(_require_player(store, req.player_id), store)}

    @app.post("/sect-wall/post")
    def post_wall_post(request: Request, req: PostRequest):
        idem = require_idempotency_key(request, route="sect-wall-post")
        result = create_post(players_db_accessor(), req.player_id, req.message, req.pin, idem)
        store = players_db_accessor()
        return {**_public(result), "view": feed_view(_require_player(store, req.player_id), store)}

    @app.post("/sect-wall/unpin")
    def post_unpin(req: UnpinRequest):
        return unpin_post(players_db_accessor(), req.player_id, req.post_id)

    @app.post("/admin/sect-wall/seed")
    def seed_sect_wall_fixture(
        player_id: str,
        sect_id: str | None = None,
        stones: int = 0,
        lifetime: int = 0,
        week_start: float | None = None,
        last_post_at: float | None = None,
        _admin: Principal = Depends(require_admin),
    ):
        """Test-only membership/contribution fixture; disabled unless enabled.

        Two independent gates, and both are required. The env gate keeps the
        route dark in any deployment that has not opted in; the operator token
        keeps it dark in a deployment or test run that HAS set the env var
        without intending to expose a currency-mutating route. Previously the
        env var alone was the whole guard, so a test that set it opened a route
        that writes spirit stones for any player_id the caller named.
        """
        if os.environ.get("WC_SECT_WALL_TEST_FIXTURE") != "1":
            raise HTTPException(status_code=404, detail="Not found")
        store = players_db_accessor()
        # Existence check BEFORE the transaction opens. PlayerStore.transaction()
        # preloads player_id and raises a bare KeyError for an unknown id, so
        # entering it first turned a missing player into an unhandled 500 and
        # made the 404 below unreachable for exactly the case it was written for.
        # The value is deliberately discarded: outside a transaction __getitem__
        # hands back a detached copy, and the write must use the live row.
        _require_player(store, player_id)
        with store.transaction(player_id):
            player = _require_player(store, player_id)
            state = _state_of(player)
            if sect_id is not None:
                state["sect_id"] = sect_id
                player["sect_id"] = sect_id
            stones = max(0, min(100000, int(stones)))
            current = player.get("spirit_stones")
            if isinstance(current, dict):
                current["low_grade"] = max(int(current.get("low_grade", 0)), stones)
            else:
                player["spirit_stones"] = max(int(current or 0), stones)
            if lifetime:
                # Lifetime and the spendable balance move together; the weekly
                # counter is deliberately left alone so a fixture cannot push a
                # player over WEEKLY_CONTRIBUTION_CAP and block real donations.
                state["lifetime_contribution"] = max(int(state.get("lifetime_contribution", 0)), int(lifetime))
                state["contribution"] = max(int(state.get("contribution", 0)), int(lifetime))
                player.setdefault("sect", {})["contribution"] = int(state["contribution"])
            if week_start is not None:
                state["week_started_at"] = float(week_start)
            if last_post_at is not None:
                state["last_post_at"] = float(last_post_at)
        return {"ok": True, "view": state_view(_require_player(store, player_id))}
