"""Player-to-player routes: five chat channels, friends, and sect group activity.

WHAT THIS MODULE IS
-------------------
The client-facing surface for social play, in one file. It adds:

* **Chat, five channels** -- ``world``, ``sect``, ``party``, ``whisper`` and
  ``system``. A *channel* is a named room players write into; each has its own
  access rule and its own send rate.
* **Friends** -- add, remove, visit a friend's cultivation chamber, and send a
  gift into their inbox.
* **Sect group activity** -- the meditation window and the beast hunt, both
  resolved from the sect's own committed power rather than from a client number.

WHAT THIS MODULE DELIBERATELY DOES NOT RE-IMPLEMENT
----------------------------------------------------
Three of the surfaces below already exist in this tree and are wired to live
clients. This module does not duplicate them and does not shadow their paths:

* ``/comm/send`` and ``/comm/history`` (``communication.py``) remain the chat
  write path. ``/social/chat/*`` here is the *five-channel* surface the client
  panel calls, and it reuses ``CommunicationStore`` for storage so both paths
  read the same messages.
* ``/friends/request``, ``/friends/remove``, ``/friends/block*``
  (``friends.py``) remain the friendship graph. Only ``visit`` and ``gift`` are
  new here, because those two are the ones the design marks as 404.
* ``SectV2Store`` (``sect_routes.py``) already holds the meditation and beast
  hunt logic, so this module reuses that store rather than writing a second
  implementation.

CANONICAL STATUS -- MEASURED 2026-10-02, NOT A CLAIM
-----------------------------------------------------
The comment this section used to carry said "One implementation, two entry
points, no drift." That was false and is withdrawn. Measured against a booted
app (``TestClient(main.app)`` inside the lifespan context, 352 route+method
pairs):

* ``/sect-v2/*`` -- **0** mounted. All 13 answer 404. ``register_sect_v2_routes``
  is genuinely never called from ``main.py``. There is no second entry point;
  there is no entry point at all.
* ``/social/sect/*`` -- 6 mounted, and every one of them answers **503
  "Sect activity unavailable"** for a real authenticated caller. ``main.py``
  builds ``SocialStore()`` with no arguments, so ``social_store.sect_store``
  is ``None``, so ``store_v2`` is ``None`` at registration time and
  ``_require_v2()`` refuses every request. That is a wiring defect in
  ``main.py``, not in this file, and it is NOT fixed here.

So ``SectV2Store`` is the canonical *logic* (it is what ``sect_war.py:842``
also imports) and ``/social/sect/*`` is the canonical *route surface* for sect
group activity. ``sect_routes.py`` is load-bearing, not dead code: with that
module made unimportable, ``register_social_routes`` raises ``ImportError``.
Do not delete it.

WHY THE PREFIX IS ``/social``
-----------------------------
``/sect/`` in ``main.py`` is a catch-all that matches any single segment, so a
new route under it is read as a player name and answered 403. ``/social`` has no
such shadow, and it keeps this module's routes greppable as one feature.

THE RULES, IN THE ORDER THEY BITE
---------------------------------
1. **Identity comes from the session, never the body.** Every handler declares
   ``Depends(require_player)`` by hand and acts on ``identity.player_id``. A
   route that reads a player id out of a request body acts on something the
   caller *typed*. The request models below therefore carry no ``player_id``
   field at all. A caller who posts one does NOT reach a shape validator: the
   ``player_id`` is read out of the request body by ``auth.enforce_ownership``
   (see ``auth.py``, ``_collect_claims``) and refused with **403 "player_id does
   not match the authenticated session"** before the handler or the model runs.
   Measured against a bare app with the real ``require_player``: a cross-player
   body answers 403, the same body with no session answers 401, and a body with
   the caller's own id behaves exactly like a body with no id at all. (The
   older "422 from the shape validator" wording was wrong -- 422 is what a body
   that is not valid JSON gets, which is a different case.)
2. **Block is symmetric.** ``is_blocked(a, b) OR is_blocked(b, a)``. Checking
   only the sender's own list lets a blocked player keep writing.
3. **Friendship gates the whisper channel**, and fails CLOSED when the friend
   layer is not loaded. A whisper must NAME a recipient: a scope-less whisper
   was accepted and filed under the shared key ``whisper:``, so any signed-in
   player could read it back. A note to yourself is allowed, but only when you
   address it to yourself by id.
4. **One refusal string for every reason.** "Not friends", "they blocked you"
   and "you blocked them" must be indistinguishable, or the refusal is an oracle
   for testing whether a named account blocked you.
5. **Content is validated BEFORE the rate limit is charged**, so junk cannot
   burn a real player's allowance.
6. **Escape on the way OUT as well as in.** ``sanitise_inbound`` cleans what a
   caller sends; ``sanitise_outbound`` removes the *possibility* of interpreting
   what survived. Historical rows predate the rule and are still stored.
7. **Server mints the message id.** The client never names a message.

NO ROUTE HERE PAYS CURRENCY FOR BEING SOCIAL
--------------------------------------------
A reward for friending, gifting or chatting can be earned with no other human
present, by running several accounts, and becomes purchasable within a week.
The only value-moving surface here is the gift, and a gift moves an item the
sender already holds. Sect activity pays *contribution points and qi*, which
are account-bound and unsellable.

REGISTRATION POSITION IS LOAD-BEARING
-------------------------------------
``register_social_routes(app, ...)`` must be called ABOVE
``install_ownership_guard(app)``, which ``main.py`` mounts at the bottom and
which REWRITES routes in place. A register call below the mount registers too
late for the guard to see those routes.

THE OWNERSHIP GUARD DOES NOT COVER THESE ROUTES, AND THAT IS CORRECT
--------------------------------------------------------------------
MEASURED against the booted app after mounting: ``main._GUARDED_ROUTES``
contains ZERO ``/social`` paths, and 15 of the 16 routes carry
``Depends(require_player)`` of their own.

That is not a wiring failure, it is how the guard works. ``install_ownership_guard``
matches a route by SHAPE -- ``ownership_guard.player_claim_params`` looks for a
``player_id``/``pid`` in the path, the query or a Pydantic body -- and refuses to
attach anything when a route names no player at all. Every route here takes the
caller from the session and names no player id, so the guard correctly finds
nothing to guard and the explicit dependency is the mechanism rather than a
belt-and-braces extra. The 15 routes were then each driven with no session and
answered 401, which is the evidence that the dependency is real.

The sixteenth, ``GET /social/chat/channels``, is deliberately public: it returns
five static channel names and their static rate limits, a client can show the
rules before it has one, and nothing about a player crosses it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

# `auth`, `social_policy` and friends are BARE sibling modules that only resolve
# once this file's own directory is on sys.path, which main.py arranges before
# importing from here. The insert is idempotent and makes this module importable
# on its own from a test.
#
# It must be MODULE scope, not function scope: `from __future__ import
# annotations` above turns every annotation into a STRING, and FastAPI resolves
# those strings against the MODULE's globals. An import inside the register
# function leaves `Request` and `PlayerIdentity` absent from module globals, so
# FastAPI cannot tell a Request from a query parameter and answers
# `loc=["query","request"]` 422.
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth as _auth_module  # noqa: E402,F401
import social_policy  # noqa: E402
from auth import PlayerIdentity, require_player  # noqa: E402
from idem_gate import require_idempotency_key  # noqa: E402


# =============================================================================
# Constants
# =============================================================================

#: The five channels, and who may write to each. This is the whole chat
#: vocabulary; a value outside it is a 422 rather than a silent new channel.
CHANNEL_WORLD = "world"
CHANNEL_SECT = "sect"
CHANNEL_PARTY = "party"
CHANNEL_WHISPER = "whisper"
CHANNEL_SYSTEM = "system"

CHANNELS = (CHANNEL_WORLD, CHANNEL_SECT, CHANNEL_PARTY, CHANNEL_WHISPER, CHANNEL_SYSTEM)

#: Per-channel send limits, per player. `world` is the noisiest room and the only
#: one a stranger can reach a player through, so it is the tightest.
#: `system` is read-only for players: 0 means a player send is always refused,
#: and the admin surface is not part of this module.
CHANNEL_RATE_LIMITS: Dict[str, int] = {
    CHANNEL_WORLD: 5,
    CHANNEL_SECT: 20,
    CHANNEL_PARTY: 20,
    CHANNEL_WHISPER: 10,
    CHANNEL_SYSTEM: 0,
}

CHANNEL_RATE_WINDOW_SECONDS = 60.0

#: A visit fans out into a friend-scoped read, so an unbounded fan-out is a way
#: to make the server do work for free.
VISIT_RATE_LIMIT = 30
VISIT_RATE_WINDOW_SECONDS = 3600.0

#: Gifts. One of a kind, per friend, per day, each direction -- plus an
#: ABSOLUTE daily cap per sender, because a percentage cap alone is defeated by
#: splitting a transfer across many sends.
GIFT_RATE_LIMIT = 20
GIFT_RATE_WINDOW_SECONDS = 86400.0
GIFT_ABSOLUTE_DAILY_CAP = 20
GIFT_RECIPIENT_DAILY_CAP = 5
GIFT_EXPIRY_SECONDS = 30 * 24 * 3600

#: The gift catalogue, mapped from design INTENT onto items that EXIST in
#: `inventory.ITEM_DEFINITIONS` (measured: 22 items). The design document names
#: five gifts -- `stone_pouch`, `beast_ration`, `qi_scroll`, `training_manual`,
#: `hall_decor_token` -- and NOT ONE of those identifiers exists in the code. A
#: catalogue built from the document fails on the first unknown id.
#:
#: The two intentions with no real equivalent (the stone pouch and the training
#: manual) are CUT rather than invented, because inventing them means inventing
#: an economy.
GIFTS: Dict[str, Dict[str, Any]] = {
    "beast_ration": {"item_id": "beast_food_basic", "label": "Beast Rations", "units": 10},
    "cultivation_aid": {"item_id": "insight_scroll", "label": "Insight Scroll", "units": 1},
    "pill": {"item_id": "cultivation_pill_low", "label": "Cultivation Pill", "units": 1},
    "sect_contribution": {"item_id": "sect_contribution_token", "label": "Sect Token", "units": 1},
}

#: Account-bound things are never giftable. Progression, realm, unlocks and
#: titles have no legitimate transfer use; every transfer is abuse or a mistake.
#: The catalogue above is an ALLOW list, so this is structural, not a filter
#: that a future item can slip past.
NON_GIFTABLE = ("realm", "cultivation_realm", "stage", "progress", "unlocks", "titles")

#: One refusal string for every social refusal. Not friends, they blocked you,
#: you blocked them, no such friend: all four answer identically, because
#: distinct messages turn the refusal into a way to test whether a named
#: account blocked you.
SOCIAL_REFUSAL = "Not available."

#: A hunt fewer than this many sect members can open is a two-person private
#: payout wearing a group costume. The reward requires the group.
BEAST_HUNT_MIN_PARTY = 3

#: Bound on retained messages per channel. Chat that a player scrolls is small;
#: a channel that grows without limit is a memory cost paid by every read.
MAX_CHANNEL_MESSAGES = 2000

#: History page size is bounded on the SERVER. A caller asking for 10000 gets
#: the cap, not the world.
MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 50

#: Guard for the module's own mutable state. FastAPI runs sync handlers in a
#: threadpool, so two requests from one player really can interleave.
_LOCK = threading.Lock()


# =============================================================================
# Request models
# =============================================================================
#
# None of these carry a player_id. The caller is the session; a body that names
# another player is refused with 403 by the ownership check in ``auth.py``
# before this model is ever consulted, which is louder and more honest than a
# handler that quietly ignores it.
#
# NOTE: communication.py also lets a body name its own player. That is a
# deliberate tightening here and it is NOT adopted -- see CHANNEL_ALIASES.


#: The canonical room vocabulary, adopted from ``communication.ChannelType``
#: (sect | party | dm | realm | system | board). This module's own history
#: named two of those rooms differently, and a client built against the
#: communication vocabulary got a 422 for a room that plainly exists:
#:
#:   ``whisper`` -> ``dm``   same room, two names
#:   ``world``   -> ``world`` UNCHANGED, deliberately (see below)
#:
#: ``world`` is NOT renamed to ``realm``. They are not two names for one room:
#: ``world`` is open to every player at 5 messages/minute, ``realm`` is gated
#: at cultivation level 12. Merging them would silently open a gated room (or
#: close an open one), which is a product decision, not a vocabulary rename.
#:
#: The alias is applied to the INPUT only. The stored bucket key and the
#: outbound reply keep the name this module already published, because the
#: published contract is frozen by exact-key-set assertions and a client that
#: reads ``channel`` must keep working. Renaming the output is a separate,
#: breaking change.
CHANNEL_ALIASES: Dict[str, str] = {
    "dm": CHANNEL_WHISPER,
    "whisper": CHANNEL_WHISPER,
}


def canonical_channel(name: Optional[str]) -> Optional[str]:
    """Map a caller's room name onto this module's internal name.

    Unknown names are returned UNCHANGED so the existing closed-vocabulary
    check still refuses them: an alias table must never widen what is
    accepted. Case and whitespace are deliberately not normalised -- the
    vocabulary is closed and exact.
    """
    if name is None:
        return None
    return CHANNEL_ALIASES.get(name, name)


class ChatSendRequest(BaseModel):
    """One message into one of the five channels.

    ``channel`` is the historical field name and ``channel_type`` is the
    canonical one shared with communication.py. BOTH are accepted and at least
    one is required, so a client written against either vocabulary works and
    neither is a silent drop. Supplying both with DIFFERENT values is a
    request whose intent is ambiguous, and is refused rather than resolved.
    """

    channel: Optional[str] = Field(
        None, description="Legacy name: world | sect | party | whisper | system"
    )
    channel_type: Optional[str] = Field(
        None, description="Canonical name, shared with communication.py: dm | realm | ..."
    )
    content: str = Field(..., min_length=1, description="Message text, capped server-side")
    scope: Optional[str] = Field(
        None,
        description=(
            "sect id, party id, or the friend's player id for whisper/dm. "
            "world and system ignore it."
        ),
    )
    reply_to: Optional[str] = Field(None, description="Message id being replied to")

    @model_validator(mode="after")
    def _resolve_channel(self):
        """Fold ``channel_type`` into ``channel`` before the handler reads it.

        Keeps every downstream check, store call and test reading one field,
        so the alias exists in exactly one place and cannot drift.
        """
        if self.channel_type is not None:
            folded = canonical_channel(self.channel_type)
            if self.channel is not None and canonical_channel(self.channel) != folded:
                raise ValueError(
                    "channel and channel_type name different rooms; send one"
                )
            self.channel = folded
        elif self.channel is not None:
            self.channel = canonical_channel(self.channel)
        if self.channel is None:
            raise ValueError("channel or channel_type is required")
        return self


class ChatHistoryRequest(BaseModel):
    """A page of one channel's messages.

    Accepts the same two field names as ``ChatSendRequest``, for the same
    reason: the reader and the writer must not disagree about the vocabulary.
    """

    channel: Optional[str] = None
    channel_type: Optional[str] = None
    scope: Optional[str] = None
    limit: int = Field(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT)
    before: Optional[str] = Field(
        None,
        description=(
            "Inclusive cursor: a message id, returned as the newest row of the "
            "page. Not a timestamp."
        ),
    )

    @model_validator(mode="after")
    def _resolve_channel(self):
        if self.channel_type is not None:
            folded = canonical_channel(self.channel_type)
            if self.channel is not None and canonical_channel(self.channel) != folded:
                raise ValueError(
                    "channel and channel_type name different rooms; send one"
                )
            self.channel = folded
        elif self.channel is not None:
            self.channel = canonical_channel(self.channel)
        if self.channel is None:
            raise ValueError("channel or channel_type is required")
        return self


class FriendTargetRequest(BaseModel):
    """A friend action that names only the OTHER player.

    ``target_id`` carries the SAME ``max_length=64`` bound that
    ``friends.FriendRequestCreate`` / ``FriendRemoveRequest`` / ``BlockRequest``
    already put on that name (measured, both modules). It is the one parameter
    name the two surfaces share, so a caller that drives
    ``/social/friends/add`` and then ``/friends/request/accept`` should not find
    that a 65-character id is a 422 on one side and a 404 "Player not found"
    on the other -- the two answers describe the same mistake differently.
    """

    target_id: str = Field(
        ..., min_length=1, max_length=64, description="The other player's id"
    )


class GiftSendRequest(BaseModel):
    """Send one gift. The item comes from the catalogue, never from the caller."""

    target_id: str = Field(
        ..., min_length=1, max_length=64, description="The friend's player id"
    )
    gift: str = Field(..., min_length=1, description="A key from the gift catalogue")


class GiftClaimRequest(BaseModel):
    """Claim one gift from your own inbox."""

    gift_id: str = Field(..., min_length=1)


class HuntStartRequest(BaseModel):
    """Open a sect beast hunt."""

    wave: int = Field(..., ge=1, le=50, description="Which wave the party commits to")


class HuntHitRequest(BaseModel):
    """Land one hit on the shared target."""

    damage: int = Field(
        0, ge=0, le=1_000_000, description="Advisory only. The server computes the credit."
    )


# =============================================================================
# Rate limiting
# =============================================================================


class WindowLimiter:
    """Per-key sliding-window attempt counter.

    Shaped after ``social_policy.MessageRateLimiter``: a deque of attempt
    instants per key, lazy eviction, and a 429 carrying ``Retry-After``.

    The key is the PLAYER id, never the network address. Throttling by address
    would let one player reset their allowance by changing address, while
    punishing every player behind a shared address for one player's behaviour.

    Thread-safe because FastAPI runs sync handlers in a threadpool.
    """

    def __init__(self, limit: int, window_seconds: float, label: str,
                 clock=time.monotonic) -> None:
        self._limit = max(0, int(limit))
        self._window = float(window_seconds)
        self._label = label
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: Dict[str, deque] = {}

    def check(self, key: str) -> None:
        """Count one attempt for ``key``; 429 when over the cap.

        Call this AFTER validating the request, so a malformed request does not
        consume a real player's allowance, and BEFORE storing anything.
        """
        if not key:
            raise HTTPException(status_code=401, detail="invalid or missing session token")
        if self._limit == 0:
            # A zero limit is a CLOSED channel, not an unlimited one. This is
            # how `system` is read-only for players.
            raise HTTPException(status_code=403, detail="This channel is read-only.")
        now = float(self._clock())
        with self._lock:
            bucket = self._attempts.get(key)
            if bucket is None:
                bucket = deque()
                self._attempts[key] = bucket
            while bucket and now - bucket[0] >= self._window:
                bucket.popleft()
            if len(bucket) >= self._limit:
                retry_after = max(1, int(self._window - (now - bucket[0])))
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"You are {self._label} too quickly. "
                        f"Try again in {retry_after}s."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

    def remaining(self, key: str) -> int:
        """Attempts still available to ``key`` in the current window."""
        with self._lock:
            bucket = self._attempts.get(key)
            return max(0, self._limit - len(bucket)) if bucket else self._limit

    def reset(self) -> None:
        """Drop all counters. Exposed so a test can start from a clean slate."""
        with self._lock:
            self._attempts.clear()


#: One limiter per surface, created per STORE rather than as a module global,
#: so two apps in one process (the shipped app and a test's) do not share a
#: budget and one cannot exhaust the other's allowance. They are built in
#: ``SocialStore.__init__``, not here: a limiter constructed inside a request
#: handler is a fresh empty bucket on every call and enforces nothing.


# =============================================================================
# Store
# =============================================================================


class SocialStore:
    """Messages, gift boxes, and the shared counters over them.

    Holds no friendship of its own and no sect membership of its own: it reads
    ``FriendsStore`` and ``SectStore`` for both, so there is only one answer to
    "are these two friends" and one answer to "who is in this sect".
    """

    def __init__(self, comm_store=None, friends_store=None, sect_store=None,
                 inventory_store=None, players_db_provider=None) -> None:
        self.comm_store = comm_store
        self.friends_store = friends_store
        self.sect_store = sect_store
        self.inventory_store = inventory_store
        self._players_db_provider = players_db_provider

        #: channel key -> list of message dicts, newest last.
        self.channels: Dict[str, List[dict]] = {}
        #: player id -> list of pending gift dicts.
        self.gift_boxes: Dict[str, List[dict]] = {}
        #: (sender, recipient) -> day index -> count. The per-pair daily cap.
        self._gift_pairs: Dict[tuple, Dict[int, int]] = {}
        #: player id -> day index -> count. The absolute daily cap.
        self._gift_daily: Dict[str, Dict[int, int]] = {}
        #: recipient -> day index -> {gift_key: count}. So one player cannot
        #: fill another's bag across fifty friends.
        self._gift_inbound: Dict[str, Dict[int, Dict[str, int]]] = {}
        #: sender -> idempotency key -> {fingerprint, response}, with a FIFO
        #: order list bounded to GIFT_RECEIPT_LIMIT entries.
        self._gift_receipts: Dict[str, Dict[str, Any]] = {}

        # Limiters live HERE, one per store, because a limiter constructed
        # inside a request handler is a fresh empty bucket on every call and
        # therefore enforces nothing. Owning them here also means two apps in
        # one process (the shipped app and a test's) do not share an allowance.
        self.visit_limiter = WindowLimiter(
            VISIT_RATE_LIMIT, VISIT_RATE_WINDOW_SECONDS, "visiting")
        self.gift_limiter = WindowLimiter(
            GIFT_RATE_LIMIT, GIFT_RATE_WINDOW_SECONDS, "gifting")
        #: One limiter per channel, so a player cannot spend the whisper
        #: allowance in the world channel.
        self.channel_limiters = {
            name: WindowLimiter(CHANNEL_RATE_LIMITS.get(name, 0),
                                CHANNEL_RATE_WINDOW_SECONDS,
                                f"chatting in {name}")
            for name in CHANNELS
        }

    # -- idempotency receipts ---------------------------------------------

    def replayed_gift(self, sender_id: str, key: str,
                      fingerprint: str) -> Optional[dict]:
        """The stored response when this key already ran, else None.

        A replay that CHANGES the request is a 409 rather than the old answer:
        the caller reused one key for two different sends, and answering with
        the first response would hand them a success for a gift they never
        made.
        """
        state = self._gift_receipts.get(sender_id)
        if not isinstance(state, dict):
            return None
        receipts = state.get("receipts")
        if not isinstance(receipts, dict):
            # A stored sender block whose receipts map is not a dict answers as
            # "no receipt here" rather than raising on the .get() below.
            return None
        # Value-level guard, the same rule as equipment.py: the map can be a
        # dict and still hold a string, a number or a list under one key, and
        # `stored.get("fingerprint")` below raises AttributeError on those --
        # not an HTTPException, so it escaped as a plain 500 on every gift send
        # for that sender. record_gift_receipt is the only writer and it always
        # stores a dict, so a non-dict value was never a receipt: drop it and
        # treat the key as new.
        with _LOCK:
            for _key in [k for k, v in receipts.items()
                         if not isinstance(v, dict)]:
                receipts.pop(_key, None)
        stored = receipts.get(key)
        if stored is None:
            return None
        if stored.get("fingerprint") != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="That Idempotency-Key was already used for a different gift.",
            )
        return dict(stored.get("response") or {})

    def record_gift_receipt(self, sender_id: str, key: str, fingerprint: str,
                            response: dict) -> None:
        """Remember one gift send so a retry with the same key moves nothing."""
        with _LOCK:
            state = self._gift_receipts.setdefault(
                sender_id, {"receipts": {}, "order": []})
            state["receipts"][key] = {"fingerprint": fingerprint,
                                      "response": deepcopy(response)}
            state["order"].append(key)
            while len(state["order"]) > GIFT_RECEIPT_LIMIT:
                state["receipts"].pop(state["order"].pop(0), None)

    # -- lookups ----------------------------------------------------------

    def _friends(self):
        return self.friends_store

    def _are_friends(self, a: str, b: str) -> bool:
        """Friendship, failing CLOSED when the friend layer is absent.

        A missing friends store must refuse, never allow. Failing open turns a
        wiring mistake into an open door to every private channel.
        """
        store = self._friends()
        if store is None:
            return False
        try:
            return bool(store.are_friends(a, b))
        except Exception:
            return False

    def _blocked_either_way(self, a: str, b: str) -> bool:
        """True when EITHER player has blocked the other.

        The player who blocks is the one holding the block, so checking only
        the sender's own list lets the blocked player keep writing. That was
        the exact open case measured before this rule existed.
        """
        store = self._friends()
        if store is None:
            return False
        try:
            return bool(store.is_blocked(a, b)) or bool(store.is_blocked(b, a))
        except Exception:
            # Fail closed: an unreadable block list is not permission.
            return True

    def _channel_key(self, channel: str, scope: str) -> str:
        return f"{channel}:{scope}"

    # -- channel access ---------------------------------------------------

    def _may_write(self, channel: str, scope: str, caller_id: str,
                   caller: dict) -> bool:
        """Whether ``caller_id`` may write to this channel.

        Each rule fails closed on missing data. The order is: is the channel
        open at all, then the channel-specific relationship.
        """
        if channel == CHANNEL_WORLD:
            return True
        if channel == CHANNEL_SECT:
            return bool(scope) and self._caller_sect(caller_id) == str(scope)
        if channel == CHANNEL_PARTY:
            return bool(scope) and self._in_party(str(scope), caller_id)
        if channel == CHANNEL_WHISPER:
            target = str(scope or "")
            if not target:
                # A whisper that names NOBODY is not a whisper -- it is a
                # message in a channel every authenticated player can read,
                # filed under the single shared key ``whisper:``. That is the
                # leak this branch used to allow: the caller believed they
                # had written a private note to themselves, and a stranger
                # read it back with an ordinary history call.
                #
                # A note to SELF is still allowed, but it must be addressed:
                # ``scope`` names the caller, which keys the bucket to that
                # one player and so is readable only by them.
                return False
            if target == caller_id:
                # Addressed to oneself. It lands in ``whisper:<own id>``,
                # which only the owner can read back, because a second
                # player asking for that scope is not a friend of the owner.
                return not self._blocked_either_way(caller_id, target)
            if self._blocked_either_way(caller_id, target):
                return False
            return self._are_friends(caller_id, target)
        if channel == CHANNEL_SYSTEM:
            # Read-only for players. An admin posting is out of this module's
            # scope and belongs to the moderation surface.
            return False
        return False

    def _caller_sect(self, player_id: str) -> Optional[str]:
        if not self.sect_store:
            return None
        try:
            return self.sect_store.get_player_sect(player_id)
        except Exception:
            return None

    def _in_party(self, party_id: str, player_id: str) -> bool:
        """Party membership from the AUTHORITATIVE roster.

        Deliberately NOT ``player["party_ids"]``: nothing in the tree writes that
        field, so the obvious read is always empty and the channel is always
        refused. The live membership is the party's own member set.
        """
        try:
            from dungeons import get_dungeon_manager
            dm = get_dungeon_manager()
            party = dm.get_party(party_id)
        except Exception:
            return False
        if party is None:
            return False
        return player_id in (getattr(party, "members", {}) or {})

    # -- messages ---------------------------------------------------------

    def send(self, channel: str, scope: str, sender: dict, content: str,
             reply_to: Optional[str] = None) -> dict:
        """Store one message.

        Order of checks is deliberate and load-bearing:

        1. channel is real, 2. the caller may write it, 3. content is cleaned
        and length-capped, 4. the rate limit is charged, 5. store.

        Content is validated BEFORE the rate limit is charged, so a caller
        cannot burn a real player's allowance by sending junk -- and an over-long
        message is refused outright rather than truncated, because silently
        cutting a message at the cap makes the client and the server disagree
        about what was sent.
        """
        if channel not in CHANNELS:
            raise HTTPException(status_code=422, detail=f"unknown channel {channel!r}")

        if not self._may_write(channel, scope, str(sender.get("id")), sender):
            # ONE string for "not friends", "they blocked you", "you blocked
            # them" and "not in this sect". A blocked player learns nothing
            # from the difference.
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)

        # 3. Clean in, then cap. `sanitise_inbound` raises 422 on empty or
        # over-long, which is a request-shape error and NOT a rate-limit event.
        cleaned = social_policy.sanitise_inbound(content)

        # 4. Charge the allowance only now, once the message is known good.
        limiter = self.channel_limiters.get(channel)
        if limiter is None:
            limiter = self.channel_limiters[CHANNEL_WORLD]
        limiter.check(str(sender.get("id")))

        key = self._channel_key(channel, scope or "")
        with _LOCK:
            bucket = self.channels.setdefault(key, [])
            msg = {
                # The SERVER mints the id. The client never names a message;
                # this is the single most important anti-abuse property here.
                "id": str(uuid.uuid4()),
                "channel": channel,
                "scope": scope or "",
                "sender_id": str(sender.get("id")),
                "sender_name": social_policy.sanitise_outbound(
                    str(sender.get("name") or "Unknown"))[:48],
                "content": cleaned,
                "reply_to": reply_to,
                "created_at": time.time(),
            }
            bucket.append(msg)
            if len(bucket) > MAX_CHANNEL_MESSAGES:
                # Keep the newest. A player scrolls the end of a channel.
                del bucket[: len(bucket) - MAX_CHANNEL_MESSAGES]
        return self._outbound_view(msg)

    def history(self, channel: str, scope: str, limit: int,
                before: Optional[str]) -> List[dict]:
        """A page of messages, newest last, with an INCLUSIVE ``before`` cursor.

        The cursor is a message id and that message is the NEWEST row of the
        page it returns, so a client paging backwards sees the row it anchored
        on again and simply keeps the older ones. The page is still sliced
        BACKWARDS from the cursor -- ``bucket[:index + 1]`` -- because slicing
        forwards (``bucket[index:]``, the old behaviour) handed back everything
        from the cursor to the newest message, so a client paging backwards got
        the newest page every time and ``before`` was effectively ignored.

        ``limit`` is clamped here rather than trusted, and a ``before`` that is
        not a message id in THIS channel is a 422 rather than a silent reset to
        the newest page -- a client that pages backwards must be able to tell a
        cursor error from the end of history.
        """
        if channel not in CHANNELS:
            raise HTTPException(status_code=422, detail=f"unknown channel {channel!r}")
        limit = max(1, min(int(limit or DEFAULT_HISTORY_LIMIT), MAX_HISTORY_LIMIT))
        key = self._channel_key(channel, scope or "")
        with _LOCK:
            bucket = list(self.channels.get(key, ()))
            if before:
                ids = [m.get("id") for m in bucket]
                if before not in ids:
                    raise HTTPException(
                        status_code=422,
                        detail="before is not a message id in this channel",
                    )
                # INCLUSIVE of the cursor: the cursor message is the newest row
                # of the page, and everything before it is the older history.
                # The slice still runs BACKWARDS -- `index + 1`, not `index` --
                # so a client paging backwards walks older messages and
                # terminates. The old forward slice (``bucket[index:]``) handed
                # back the cursor-to-newest tail instead, so a client paging
                # backwards looped on one page forever and ``before`` was
                # effectively ignored.
                bucket = bucket[:ids.index(before) + 1]
            page = bucket[-limit:]
            return [self._outbound_view(m) for m in page]

    def _outbound_view(self, msg: dict) -> dict:
        """What another player is allowed to read.

        Escapes on the way OUT as well as on the way in: input cleaning removes
        dangerous characters, output escaping removes the *possibility* of
        interpreting what survived -- and a row stored before these rules
        existed is still stored.

        NOTE: communication.py publishes a nested ``sender`` object where this
        module publishes flat ``sender_id`` / ``sender_name``. Unifying that is
        NOT done here: this module's eight-key message shape is a PUBLISHED
        contract pinned by exact-key-set assertions in both its contract suites
        and in the shipped client, so adding or renaming a key is a breaking
        change for every player already on this build. It is recorded as an
        open vocabulary item rather than shipped silently.
        """
        return {
            "id": msg.get("id"),
            "channel": msg.get("channel"),
            "scope": msg.get("scope", ""),
            "sender_id": msg.get("sender_id"),
            "sender_name": msg.get("sender_name", "Unknown"),
            "content": social_policy.sanitise_outbound(msg.get("content", "")),
            "reply_to": msg.get("reply_to"),
            "timestamp": msg.get("created_at", 0.0),
        }

    # -- gifts ------------------------------------------------------------

    @staticmethod
    def _day_index(now: Optional[float] = None) -> int:
        """The UTC day number, derived from the server clock.

        A player's own clock must never decide whether a daily cap has been
        reached, so the window is the SERVER's day and there is no stored week
        or day label for a caller to edit.
        """
        return int((time.time() if now is None else float(now)) // 86400)

    def gift_state(self, player_id: str, now: Optional[float] = None) -> dict:
        """The caller's own gift counters, for showing a cap before it is hit.

        ``per_friend`` is read by SCANNING the pair keys that begin with this
        sender, because the pair ledger is keyed ``(sender, recipient)`` and
        there is no wildcard row. The old ``(player_id, "*")`` lookup asked
        for a key nothing ever writes, so this dict was ALWAYS empty -- a
        client rendering it showed no per-friend history and no test caught
        it, because a permanently-empty field looks exactly like a player who
        has not sent anything yet.
        """
        day = self._day_index(now)
        per_friend: Dict[str, Dict[str, int]] = {}
        for (sender, target), days in self._gift_pairs.items():
            if sender != player_id:
                continue
            count = int(days.get(day, 0))
            if count:
                per_friend[target] = {"sent_today": count, "limit": 1}
        return {
            "day": day,
            "sent_today": int(self._gift_daily.get(player_id, {}).get(day, 0)),
            "absolute_daily_cap": GIFT_ABSOLUTE_DAILY_CAP,
            "recipient_daily_cap": GIFT_RECIPIENT_DAILY_CAP,
            "per_friend_per_day": 1,
            "per_friend": per_friend,
            # Kept as an alias so a client already reading `windows` sees the
            # same rows rather than silently continuing to read nothing.
            "windows": per_friend,
        }

    def send_gift(self, sender_id: str, target_id: str, gift_key: str,
                  now: Optional[float] = None) -> dict:
        """Create one gift record in the RECIPIENT's inbox.

        The item is chosen from the catalogue by KEY, never named by the
        caller, and the sender is debited by this same call. There is no state
        in which the record exists and the debit did not: both happen under one
        lock, and the debit is checked first.
        """
        if gift_key not in GIFTS:
            raise HTTPException(status_code=422, detail=f"unknown gift {gift_key!r}")
        spec = GIFTS[gift_key]

        if sender_id == target_id:
            raise HTTPException(status_code=400, detail="Cannot gift yourself")
        if self._blocked_either_way(sender_id, target_id):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)
        if not self._are_friends(sender_id, target_id):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)

        day = self._day_index(now)
        with _LOCK:
            # Absolute daily cap first: it is the one that is hardest to split
            # across sends, so it is checked before the cheaper per-pair ones.
            sent = int(self._gift_daily.setdefault(sender_id, {}).get(day, 0))
            if sent >= GIFT_ABSOLUTE_DAILY_CAP:
                raise HTTPException(
                    status_code=429,
                    detail="Daily gift limit reached. Try again tomorrow.",
                )

            # Per pair, per day, each direction: one of a kind.
            pair = self._gift_pairs.setdefault((sender_id, target_id), {})
            if int(pair.get(day, 0)) >= 1:
                raise HTTPException(
                    status_code=429,
                    detail="You already sent this friend a gift today.",
                )

            # Per recipient, per gift type, per day, so one player cannot fill
            # another's bag by sending across fifty friends.
            inbound = self._gift_inbound.setdefault(target_id, {}).setdefault(day, {})
            if int(inbound.get(gift_key, 0)) >= GIFT_RECIPIENT_DAILY_CAP:
                raise HTTPException(
                    status_code=429,
                    detail="They have received enough of this gift today.",
                )

            units = int(spec.get("units", 1))
            item_id = str(spec["item_id"])
            held = self._sender_holds(sender_id, item_id, units)
            if held < units:
                raise HTTPException(
                    status_code=400,
                    detail=f"You do not hold {spec['label']}.",
                )
            self._debit(sender_id, item_id, units)

            # From here the sender has PAID and nothing has been delivered
            # yet, so every remaining step is inside the rollback. Building
            # and storing the record is all that can still fail, and a
            # failure there used to destroy the units outright: measured with
            # an inbox that refuses the write, a send left the sender 10->9
            # with no gift row anywhere.
            #
            # The refund is on the FAILURE path only. Refunding after a
            # SUCCESSFUL append would pay the sender AND deliver the gift --
            # measured, that is a mint: sender 5->5 while the receiver went
            # 0->1, one free item per send, up to GIFT_ABSOLUTE_DAILY_CAP a
            # day. `claim_gift` already refunds the claim-side shortfall; this
            # one covers the send side.
            try:
                record = {
                    "gift_id": str(uuid.uuid4()),
                    "gift_key": gift_key,
                    "item_id": item_id,
                    "label": spec["label"],
                    "units": units,
                    "from_id": sender_id,
                    "to_id": target_id,
                    "day": day,
                    "sent_at": time.time() if now is None else float(now),
                    "expires_at": (time.time() if now is None else float(now))
                    + GIFT_EXPIRY_SECONDS,
                }
                self.gift_boxes.setdefault(target_id, []).append(record)
            except Exception:
                # Best-effort, and never masks the original cause: a refund
                # that cannot land must not replace the real error the caller
                # needs to see.
                try:
                    self._refund(sender_id, item_id, units)
                except Exception:
                    pass
                raise

            self._gift_daily.setdefault(sender_id, {})[day] = sent + 1
            pair[day] = int(pair.get(day, 0)) + 1
            inbound[gift_key] = int(inbound.get(gift_key, 0)) + 1

        # No currency is created or destroyed here. The sender's own explicit
        # item choice is the only thing that moved.
        return {"ok": True, "gift_id": record["gift_id"], "label": record["label"],
                "units": units, "item_id": item_id, "to_id": target_id,
                "sent_today": sent + 1, "absolute_daily_cap": GIFT_ABSOLUTE_DAILY_CAP}

    def inbox(self, player_id: str, now: Optional[float] = None) -> List[dict]:
        """Unclaimed gifts, oldest first, with expired rows dropped."""
        current = time.time() if now is None else float(now)
        with _LOCK:
            rows = list(self.gift_boxes.get(player_id, ()))
        return [self._gift_view(r) for r in rows if float(r.get("expires_at", 0)) > current]

    def claim_gift(self, player_id: str, gift_id: str,
                   now: Optional[float] = None) -> dict:
        """Move one gift out of the inbox and into the inventory.

        One lock, one critical section: a claim either removes the row and
        grants the item, or does neither. A crash between the two would either
        destroy an item or print one, and both are worse than a retry.
        """
        current = time.time() if now is None else float(now)
        with _LOCK:
            rows = self.gift_boxes.get(player_id, [])
            for row in rows:
                if row.get("gift_id") != gift_id:
                    continue
                if float(row.get("expires_at", 0)) <= current:
                    rows.remove(row)
                    raise HTTPException(status_code=410, detail="That gift has expired.")
                units = int(row.get("units", 1))
                item_id = str(row.get("item_id"))
                granted = self._grant(player_id, item_id, units)

                # The sender was DEBITED at send time, so a grant that lands
                # short must give the difference back here or the units are
                # destroyed. Measured before this block existed: with the
                # receiver's stack already at its ceiling, a 1-unit gift left
                # the sender 1->0 and the receiver 10->10. One unit existed in
                # no bag at all, and the reply reported ``granted: 1``.
                #
                # The refund is inside the SAME critical section that removes
                # the row, so a crash cannot refund twice or refund without
                # removing -- and it targets ``from_id`` from the row, never a
                # caller-supplied id, so it can only ever return value to the
                # player it was taken from.
                refunded = 0
                shortfall = units - granted
                if shortfall > 0:
                    sender_id = str(row.get("from_id") or "")
                    refunded = self._refund(sender_id, item_id, shortfall)

                rows.remove(row)
                return {"ok": True, "gift_id": gift_id, "item_id": item_id,
                        "units": units, "granted": granted,
                        "refunded_to_sender": refunded,
                        "from_id": row.get("from_id")}
        raise HTTPException(status_code=404, detail="No such gift")

    @staticmethod
    def _gift_view(row: dict) -> dict:
        return {
            "gift_id": row.get("gift_id"),
            "gift": row.get("gift_key"),
            "item_id": row.get("item_id"),
            "label": row.get("label"),
            "units": row.get("units"),
            "from_id": row.get("from_id"),
            "sent_at": row.get("sent_at"),
            "expires_at": row.get("expires_at"),
        }

    # -- inventory plumbing ------------------------------------------------
    #
    # Every method below is written so that a MISSING inventory store refuses
    # rather than silently succeeding. A gift that debits nothing and delivers
    # anyway is a printed item.

    def _sender_holds(self, player_id: str, item_id: str, units: int) -> int:
        store = self.inventory_store
        if store is None:
            return 0
        try:
            return int(store.get_item_count(player_id, item_id))
        except Exception:
            return 0

    def _debit(self, player_id: str, item_id: str, units: int) -> None:
        store = self.inventory_store
        if store is None:
            raise HTTPException(status_code=503, detail="Inventory unavailable")
        result = store.remove_item(player_id, item_id, units)
        if not isinstance(result, dict) or not result.get("ok"):
            raise HTTPException(status_code=400, detail="Gift could not be sent")

    def _refund(self, sender_id: str, item_id: str, units: int) -> int:
        """Return ``units`` to the SENDER. Returns what actually went back.

        Fails LOUD rather than silent: a refund that cannot land raises, and
        because the caller's row removal is in the same critical section, a
        raise leaves the gift in the inbox so a retry can still deliver it. A
        refund that reported success while dropping the units would be the
        same defect this function exists to remove.
        """
        if not sender_id or units <= 0:
            return 0
        store = self.inventory_store
        if store is None:
            raise HTTPException(status_code=503, detail="Inventory unavailable")
        result = store.add_item(sender_id, item_id, units)
        if isinstance(result, dict) and not result.get("ok"):
            raise HTTPException(status_code=503,
                                detail="Gift refund failed; gift left in inbox")
        if isinstance(result, dict):
            return int(result.get("granted") or 0)
        return units

    def _grant(self, player_id: str, item_id: str, units: int) -> int:
        """Put ``units`` in the bag and report how many ACTUALLY landed.

        The inventory store serves a full stack PARTIALLY and says so in
        ``granted``/``discarded``. This used to read
        ``result.get("granted", units) or units``, and that ``or units`` was a
        reporting bug rather than a default: when the store granted ZERO -- a
        stack already at its ceiling -- the falsy ``0`` fell through to
        ``units``, so a claim that delivered nothing answered ``granted: 1``.
        The count reported is now the count the store returned, and the
        shortfall is the caller's to refund (see ``claim_gift``).
        """
        store = self.inventory_store
        if store is None:
            raise HTTPException(status_code=503, detail="Inventory unavailable")
        result = store.add_item(player_id, item_id, units)
        if isinstance(result, dict) and not result.get("ok"):
            raise HTTPException(status_code=400, detail="Gift could not be claimed")
        if isinstance(result, dict):
            # Read the granted count WITHOUT an `or units` fallback: zero is a
            # real answer, not a missing one.
            return int(result.get("granted") or 0)
        return units

    # -- visit view --------------------------------------------------------

    @staticmethod
    @staticmethod
    def _bond_of(beast: dict) -> int:
        """The beast's loyalty, read by the ONE bond reader.

        Delegates to ``spirit_beasts._bond_of`` rather than repeating the
        rule, so a corrupt bond cannot mean one thing on the companion
        screen and another thing on a friend's visit screen. That reader's
        docstring is the decision: a corrupt bond is 0, which is what an
        absent bond already means and what is below the refusing floor.

        The value lives at ``beast["contract"]["bond"]``. Reading
        ``beast["bond"]`` -- which is what this used to do -- found no such
        key on any real beast row, so the published number was always 0 and
        the guard was guarding nothing.
        """
        # Imported here, not at module scope: spirit_beasts pulls in
        # beast_config and the cultivation tables, and a module-level
        # edge from a social module into the beast stack is one a
        # half-written peer file can turn into an import cycle.
        import spirit_beasts

        return spirit_beasts._bond_of(beast.get("contract"))

    def visit_view(self, caller_id: str, friend_id: str, player: dict) -> dict:
        """A read-only look at a friend's chamber.

        NO wallet, NO inventory, NO contribution numbers. Not greyed out --
        ABSENT. A screen that renders a field it was told not to show is one
        copy-paste away from showing it.
        """
        if friend_id == caller_id:
            raise HTTPException(status_code=400, detail="Visit someone else")
        if self._blocked_either_way(caller_id, friend_id):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)
        if not self._are_friends(caller_id, friend_id):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)

        sect_id = player.get("sect_id")
        sect_name = None
        if sect_id and self.sect_store:
            try:
                sect = self.sect_store.get_sect(str(sect_id))
                sect_name = sect.get("name") if sect else None
            except Exception:
                sect_name = None

        companions = []
        beasts = player.get("beasts")
        if isinstance(beasts, list):
            for beast in beasts:
                if not isinstance(beast, dict):
                    continue
                companions.append({
                    "beast_id": beast.get("beast_id"),
                    "name": social_policy.sanitise_outbound(
                        str(beast.get("name") or "Beast"))[:48],
                    "realm": social_policy.sanitise_outbound(
                        str(beast.get("realm") or ""))[:48],
                    "level": int(beast.get("level", 0) or 0),
                    # The bond is read off the CONTRACT (see
                    # spirit_beasts.REFUSING_BOND), never off the top level of
                    # the beast row, which has no such key and so always read 0.
                    # And it is read through the module's own stored-number
                    # guard, so a corrupt bond has ONE meaning here and there:
                    # 0, the same as an absent bond, which is below the
                    # refusing floor. A bare ``int()`` here raised ValueError /
                    # OverflowError on a hand-edited row and took the whole
                    # visit screen down with it.
                    "bond": self._bond_of(beast),
                })

        # The whitelist is written out field by field on purpose. Adding a key
        # to the player record must NOT silently publish it to every friend.
        return {
            "ok": True,
            "friend": {
                "id": friend_id,
                "name": social_policy.sanitise_outbound(
                    str(player.get("name") or "Unknown"))[:48],
                "realm": social_policy.sanitise_outbound(
                    str(player.get("cultivation_realm") or "mortal"))[:48],
                "sect_id": str(sect_id) if sect_id else None,
                "sect_name": social_policy.sanitise_outbound(str(sect_name))[:48]
                if sect_name else None,
            },
            "companions": companions,
            "server_now": time.time(),
        }


# =============================================================================
# Sect group activity
# =============================================================================
#
# The meditation window and the beast hunt both resolve from the SECT's own
# committed power, never from a number the client sent. Two properties are
# load-bearing and both are inherited from ``SectV2Store`` rather than
# reimplemented here:
#
#   * **Median-realm scaling.** One high-realm member cannot lift a window for
#     everyone, because the payout scales with the members' median rather than
#     their best.
#   * **One shared roll on the hunt.** A per-participant roll is a faucet one
#     account can farm by inviting alts.


def _sect_view(sect_id: Optional[str], sect_store) -> Dict[str, Any]:
    """The small public fact set about a sect: id and name only."""
    if not sect_id or not sect_store:
        return {"sect_id": None, "sect_name": None}
    try:
        sect = sect_store.get_sect(str(sect_id))
    except Exception:
        sect = None
    return {
        "sect_id": str(sect_id),
        "sect_name": social_policy.sanitise_outbound(str(sect.get("name") or ""))[:64]
        if sect else None,
    }


# =============================================================================
# Route registration
# =============================================================================


def register_social_routes(app, players_db_provider, social_store: SocialStore,
                           sect_v2_store=None) -> Dict[str, List[str]]:
    """Mount the social routes. Call ABOVE ``install_ownership_guard``.

    Args:
        app: the FastAPI app.
        players_db_provider: zero-arg callable returning the PlayerStore.
        social_store: the SocialStore holding messages, gifts and counters.
        sect_v2_store: an existing ``SectV2Store``. One is built from
            ``social_store.sect_store`` when omitted, so the caller cannot name
            a second, disagreeing implementation.

    Returns:
        The paths registered, so a caller can assert the mount landed.
    """
    registered: List[str] = []

    def _db():
        raw = players_db_provider() if callable(players_db_provider) else players_db_provider
        return raw() if callable(raw) else raw

    def _player(player_id: str) -> dict:
        db = _db()
        try:
            return db[player_id]
        except KeyError:
            raise HTTPException(status_code=404, detail="Player not found") from None

    def _caller_sect(identity: "PlayerIdentity") -> str:
        """The caller's sect, read from the EXISTING store.

        Never from the body. A caller who names another sect here is naming
        something they did not earn, and the 403 is the same for a stranger's
        sect as for no sect at all.
        """
        sid = social_store._caller_sect(identity.player_id)
        if not sid:
            raise HTTPException(status_code=403, detail="Not in a sect")
        return str(sid)

    def _friends_ready() -> None:
        if social_store.friends_store is None:
            # Fail closed, loudly. A social surface with no friend layer cannot
            # answer "are these two friends", and guessing yes is the one
            # answer that is always wrong.
            raise HTTPException(status_code=503, detail="Friend layer unavailable")

    router = APIRouter(tags=["social"])

    # -- chat: five channels ------------------------------------------------

    @router.post("/social/chat/send")
    async def social_chat_send(
        req: ChatSendRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Write one message into one of the five channels.

        Takes no player id from the path, the query or the body: the caller is
        the session, and ``identity.player_id`` is who they are.
        """
        _friends_ready()
        player = _player(identity.player_id)
        return {"ok": True, "message": social_store.send(
            req.channel, req.scope or "", player, req.content, req.reply_to)}

    @router.post("/social/chat/history")
    async def social_chat_history(
        req: ChatHistoryRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """A page of one channel's messages.

        Reading is gated exactly as writing is: a stranger does not get to read
        a whisper room they could not write to.
        """
        _friends_ready()
        player = _player(identity.player_id)
        channel = req.channel
        if channel not in CHANNELS:
            raise HTTPException(status_code=422, detail=f"unknown channel {channel!r}")
        if channel not in (CHANNEL_SYSTEM,) and not social_store._may_write(
            channel, req.scope or "", identity.player_id, player
        ):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)
        rows = social_store.history(channel, req.scope or "", req.limit, req.before)
        return {"ok": True, "channel": channel, "scope": req.scope or "",
                "count": len(rows), "messages": rows, "server_now": time.time()}

    @router.get("/social/chat/channels")
    async def social_chat_channels():
        """The five channels and their send limits.

        Declares no auth dependency, so the ownership guard's shape match skips
        it -- which is correct. Every value is a static constant, so a client
        can show the rules before it has a session and this route stays the
        confirming read rather than a required call.
        """
        return {
            "ok": True,
            "channels": [
                {"channel": name,
                 "max_per_minute": CHANNEL_RATE_LIMITS.get(name, 0),
                 "read_only": CHANNEL_RATE_LIMITS.get(name, 0) == 0,
                 "needs_scope": name in (CHANNEL_SECT, CHANNEL_PARTY, CHANNEL_WHISPER)}
                for name in CHANNELS
            ],
            "max_message_length": social_policy.MAX_MESSAGE_LENGTH,
            "rate_window_seconds": CHANNEL_RATE_WINDOW_SECONDS,
        }

    registered += ["/social/chat/send", "/social/chat/history", "/social/chat/channels"]

    # -- friends: add, remove, visit, gift ----------------------------------

    @router.post("/social/friends/add")
    async def social_friend_add(
        req: FriendTargetRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Send a friend REQUEST.

        A request, not a friendship: the target accepts. Auto-accept on a
        request means a stranger becomes your friend in one call from their
        side, which is the impersonation-adjacent hole this layer exists to
        close.
        """
        _friends_ready()
        store = social_store.friends_store
        if req.target_id == identity.player_id:
            raise HTTPException(status_code=400, detail="Cannot add yourself")
        if social_store._blocked_either_way(identity.player_id, req.target_id):
            raise HTTPException(status_code=403, detail=SOCIAL_REFUSAL)
        _player(req.target_id)  # 404 for a stranger who does not exist
        with _LOCK:
            try:
                created = store.create_request(identity.player_id, req.target_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # `create_request` returns a friends.FriendRequest, whose id field is
        # named `id` -- there is no `request_id` attribute. The old
        # getattr(..., "request_id", None) silently answered None for every
        # send, so a client that copied this id and posted it to
        # /friends/request/accept sent JSON null and got a 422 from
        # FriendRequestAction.request_id (min_length=1). The pending-list route
        # (friends.py) already published req.id under this same key.
        #
        # `status` is read off the created record rather than hard-coded, so it
        # cannot claim "pending" for a request the store actually declined or
        # blocked. The VALUE vocabulary is friends.py's -- this module does not
        # mint a second one. (The friend/relationship vocabulary lives in
        # friends.py `get_friend_status`: self / friends / request_sent /
        # request_received / blocked_by_you / none. That is a different axis --
        # the state of the RELATIONSHIP, not of the request row -- and is not
        # what this key reports.)
        return {"ok": True,
                "status": getattr(created, "status", None)
                or "pending",
                "request_id": created.id,
                "target_id": req.target_id}

    @router.post("/social/friends/remove")
    async def social_friend_remove(
        req: FriendTargetRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Unfriend. Removes the edge from BOTH sides."""
        _friends_ready()
        with _LOCK:
            removed = social_store.friends_store.remove_friend(
                identity.player_id, req.target_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Not friends")
        return {"ok": True, "removed": True, "target_id": req.target_id}

    @router.get("/social/friends/visit/{target_id}")
    async def social_friend_visit(
        target_id: str,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Look at a friend's cultivation chamber. Read-only.

        Rate limited to 30 an hour: a visit fans out into a friend-scoped read,
        and an unbounded fan-out is a way to make the server do work for free.
        """
        _friends_ready()
        # Validate the relationship BEFORE charging the allowance, so a
        # stranger cannot spend a real player's visit budget by guessing ids.
        view = social_store.visit_view(identity.player_id, target_id,
                                       _player(target_id))
        social_store.visit_limiter.check(identity.player_id)
        return view

    @router.get("/social/friends/gifts")
    async def social_gift_catalogue(
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """The giftable catalogue and what the caller has sent today.

        The catalogue is an ALLOW list built from items that exist in the
        server's own item table, so a client can never name an item id the
        server has not heard of.
        """
        return {
            "ok": True,
            "gifts": [
                {"gift": key, "label": spec["label"], "units": spec["units"],
                 # ``item_id`` is the server's own, not the caller's: the
                 # catalogue is an allow list over ITEM_DEFINITIONS, so a
                 # client can show what a gift IS but never name one itself.
                 "item_id": spec["item_id"],
                 "held": social_store._sender_holds(
                     identity.player_id, str(spec["item_id"]), 0)}
                for key, spec in sorted(GIFTS.items())
            ],
            "caps": social_store.gift_state(identity.player_id),
            "non_giftable": list(NON_GIFTABLE),
        }

    @router.post("/social/friends/gift/send")
    async def social_gift_send(
        request: Request,
        req: GiftSendRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Send one gift into a friend's inbox.

        Requires an ``Idempotency-Key`` header, via the SHARED gate the money
        routes use. A retry on a flaky connection must move nothing; without it,
        the same gift is sent twice.

        The gift record and the inventory debit happen under one lock and the
        debit is checked first, so there is no state in which the record exists
        and the item did not leave the sender.
        """
        _friends_ready()
        key = require_idempotency_key(request, route="social-gift-send")
        fingerprint = _gift_fingerprint(identity.player_id, req.target_id, req.gift)
        replay = social_store.replayed_gift(identity.player_id, key, fingerprint)
        if replay is not None:
            return replay

        _player(req.target_id)
        # Content is validated BEFORE the allowance is charged, the same order
        # the chat route uses. The limiter used to run first, so a caller who
        # named a gift that does not exist burned a real player's daily gift
        # allowance on every junk request -- measured at 6 refusals costing 6
        # of 20 sends. Junk must not be able to spend somebody else's day.
        if req.gift not in GIFTS:
            raise HTTPException(status_code=422, detail=f"unknown gift {req.gift!r}")
        social_store.gift_limiter.check(identity.player_id)
        result = social_store.send_gift(identity.player_id, req.target_id, req.gift)
        result["idempotency_key"] = key
        social_store.record_gift_receipt(identity.player_id, key, fingerprint, result)
        return result

    @router.get("/social/friends/gift/inbox")
    async def social_gift_inbox(
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Unclaimed gifts, oldest first."""
        rows = social_store.inbox(identity.player_id)
        return {"ok": True, "count": len(rows), "gifts": rows,
                "server_now": time.time()}

    @router.post("/social/friends/gift/claim")
    async def social_gift_claim(
        req: GiftClaimRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Move one gift from your own inbox into your inventory."""
        _friends_ready()
        return social_store.claim_gift(identity.player_id, req.gift_id)

    registered += [
        "/social/friends/add", "/social/friends/remove",
        "/social/friends/visit/{target_id}", "/social/friends/gifts",
        "/social/friends/gift/send", "/social/friends/gift/inbox",
        "/social/friends/gift/claim",
    ]

    # -- sect group activity ------------------------------------------------

    store_v2 = sect_v2_store
    if store_v2 is None and social_store.sect_store is not None:
        from sect_routes import SectV2Store
        store_v2 = SectV2Store(social_store.sect_store)

    def _require_v2():
        if store_v2 is None:
            raise HTTPException(status_code=503, detail="Sect activity unavailable")
        return store_v2

    @router.get("/social/sect/meditation")
    async def social_meditation_view(
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """The caller's meditation standing for today's window."""
        v2 = _require_v2()
        sid = social_store._caller_sect(identity.player_id)
        with _LOCK:
            view = v2.meditation_view(identity.player_id, sid, time.time())
        return {**view, **_sect_view(sid, social_store.sect_store)}

    @router.post("/social/sect/meditation/join")
    async def social_meditation_join(
        request: Request,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Join today's meditation window and take its payout.

        The day index IS the guard: it is derived from the server clock, so
        yesterday's claim is simply a different key and the guarantee survives a
        restart. No receipt dictionary, no eviction window.
        """
        v2 = _require_v2()
        require_idempotency_key(request, route="social-meditation-join")
        sid = _caller_sect(identity)
        with _LOCK:
            result = v2.claim_meditation(identity.player_id, sid, time.time())
        return {**result, **_sect_view(sid, social_store.sect_store)}

    @router.get("/social/sect/beast-hunt")
    async def social_hunt_view(
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """The waves a sect may commit to, and any hunt in progress."""
        v2 = _require_v2()
        sid = social_store._caller_sect(identity.player_id)
        with _LOCK:
            view = v2.waves_view(sid, time.time())
            hunt = v2.hunts.get(identity.player_id)
        return {
            **view,
            **_sect_view(sid, social_store.sect_store),
            "your_hunt": {"wave": hunt.get("wave"), "party": list(hunt.get("party", []))}
            if hunt else None,
        }

    @router.post("/social/sect/beast-hunt/start")
    async def social_hunt_start(
        request: Request,
        req: HuntStartRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Open a hunt with your own sect-mates.

        The party is not caller-named. ``SectV2Store.form_party`` resolves every
        member against the CALLER'S OWN sect, so a stranger's id simply is not
        in it and the request 403s rather than joining somebody else's hunt.
        """
        v2 = _require_v2()
        require_idempotency_key(request, route="social-beast-hunt-start")
        sid = _caller_sect(identity)
        roster = _sect_roster(sid, social_store.sect_store)
        if len(roster) < BEAST_HUNT_MIN_PARTY:
            raise HTTPException(
                status_code=409,
                detail="A hunt needs more sect members to be worth opening.",
            )
        with _LOCK:
            result = v2.form_party(identity.player_id, sid, req.wave, roster,
                                   time.time())
        return {**result, **_sect_view(sid, social_store.sect_store)}

    @router.post("/social/sect/beast-hunt/hit")
    async def social_hunt_hit(
        request: Request,
        req: HuntHitRequest,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Land one hit on the shared target.

        ``req.damage`` is advisory and is NOT used to score. The server compares
        the party's committed wave power against the stored hunt, so a client
        that posts a huge damage number scores exactly what its real party did.
        """
        v2 = _require_v2()
        require_idempotency_key(request, route="social-beast-hunt-hit")
        sid = _caller_sect(identity)
        with _LOCK:
            hunt = v2.hunts.get(identity.player_id)
            if not hunt or hunt.get("sect_id") != sid:
                raise HTTPException(status_code=404, detail="No hunt in progress")
            credited = v2.record_activity(identity.player_id, "beast_hunt_wave",
                                          time.time(), unit=1)
        return {"ok": True, "credited": credited,
                "wave": int(hunt.get("wave", 0)),
                "party_size": len(hunt.get("party", []))}

    @router.post("/social/sect/beast-hunt/settle")
    async def social_hunt_settle(
        request: Request,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Score the hunt against the SERVER'S own power number.

        The client does not say it won. It asks for the result, and the server
        compares committed power to target. A per-participant roll would be a
        faucet one account can farm by inviting alts, so there is ONE roll for
        the party.
        """
        v2 = _require_v2()
        require_idempotency_key(request, route="social-beast-hunt-settle")
        sid = _caller_sect(identity)
        with _LOCK:
            result = v2.resolve_hunt(identity.player_id, time.time())
        return {**result, **_sect_view(sid, social_store.sect_store)}

    registered += [
        "/social/sect/meditation", "/social/sect/meditation/join",
        "/social/sect/beast-hunt", "/social/sect/beast-hunt/start",
        "/social/sect/beast-hunt/hit", "/social/sect/beast-hunt/settle",
    ]

    app.include_router(router)
    return {name: ["social"] for name in registered}


def _sect_roster(sect_id: str, sect_store) -> List[str]:
    """Every member of ``sect_id``, read from the sect record itself."""
    if not sect_store:
        return []
    try:
        sect = sect_store.get_sect(str(sect_id))
    except Exception:
        return []
    if not sect:
        return []
    return [str(pid) for pid in (sect.get("members") or {})]


# =============================================================================
# Gift receipts
# =============================================================================
#
# A bounded, in-process receipt list held BY THE STORE, not by a module global.
# (An earlier draft kept it in a module-level ``_ACTIVE_STORE`` that the
# registrar set; a global is the wrong owner for per-app state, because two
# apps in one process would share one receipt budget and a test's replay could
# silence the shipped app's. The store is already per-app, so it owns this.)
#
# It is a convenience against a double-tap, not the once-only guarantee: the
# daily caps inside ``SocialStore.send_gift`` are the guarantee, and they are
# derived from the server clock rather than from anything a caller can replay.


GIFT_RECEIPT_LIMIT = 50


def _gift_fingerprint(sender_id: str, target_id: str, gift: str) -> str:
    """A stable fingerprint of WHAT was asked for, so a replay that CHANGES the
    request is refused rather than silently answered with the old response."""
    return hashlib.sha256(
        json.dumps({"op": "social-gift", "from": sender_id, "to": target_id,
                    "gift": gift}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _init_social_store(players_db_provider=None, comm_store=None,
                       friends_store=None, sect_store=None,
                       inventory_store=None) -> SocialStore:
    """Build a SocialStore wired to the app's existing singletons."""
    return SocialStore(
        comm_store=comm_store,
        friends_store=friends_store,
        sect_store=sect_store,
        inventory_store=inventory_store,
        players_db_provider=players_db_provider,
    )