"""
Battle-pass ROUTE WIRING — the mount point, separate from the pass logic.

WHY THIS MODULE EXISTS
----------------------
`battle_pass.py` holds the season clock, the XP curve, the reward tables and the
once-only ledgers: all the LOGIC. This module holds the one thing that is not
logic — the act of putting those handlers on a FastAPI app. Splitting them means
the money rules can be read, reviewed and unit-tested without a web framework in
sight, and it matches the house shape: `beast_wave_routes.py`,
`dungeon_routes.py`, `achievement_routes.py`, `daily_boss_routes.py`,
`sect_routes.py` and `gift_series.py` all mount the same way.

WHY THE HANDLERS STILL LIVE IN `battle_pass.py`
------------------------------------------------
This is deliberate and load-bearing, not an oversight.

Two test files treat `battle_pass.py` SOURCE TEXT as the contract:

  * `tests/test_battle_pass_money_path_control.py` reads
    `Path(battle_pass.__file__)`, deletes one exact block, and asserts the money
    path goes red. Its fixture asserts in teardown that
    `"ON CONFLICT (player_id, season_id, tier, level)"` is back in the file.
  * `tests/test_battle_pass_purchase_idempotency.py` copies the tree, counts
    `source.count(_RESERVE_ANCHOR)` and requires exactly 1, then sabotages that
    one call site.

Those anchors are indentation-sensitive: the purchase reservation must sit at 12
spaces (inside `with players_db.transaction(...)`) and the claim receipt at 16
(inside `if is_first:`). Moving the handler bodies to module level in this file
would re-indent both and the controls would report themselves "vacuous" — a
green that measures nothing. Keeping the bodies where the anchors live is what
lets those controls keep biting.

So: the ROUTE TABLE and the MOUNT live here; the HANDLER BODIES stay in
`battle_pass.py`, and this module delegates to them unchanged.

THE FOUR `/api` ROUTES ADDED 2026-10-01
---------------------------------------
`season_info`, `track_info`, `claim_reward` and `purchase_tier` are NEW, and
their handler bodies live HERE rather than in `battle_pass.py`. That is not an
inconsistency with the rule above, it is a consequence of it: the two control
tests named above anchor on text inside `battle_pass.py`'s handlers, so adding
routes there would not move them -- but these four are additions, not
moves, and keeping them in the mount module means the legacy handlers stay
byte-identical for those controls.

The `/api` segment is LOAD-BEARING, not decoration. `battle_pass.py` already
serves `GET /battle-pass/{player_id}`, a one-segment wildcard, and FastAPI
serves the FIRST matching route in registration order. A literal path with a
single segment after `/battle-pass/` would therefore be captured by that
wildcard and be dead code that still reads as present in a route table. Two
segments cannot be shadowed that way. The legacy paths stay because the shipped
Godot client calls them (`client/scenes/game/BattlePassPanel.gd`).

THE PAYER IS THE SESSION
------------------------
The four `/api` routes take NO `player_id` in the path, the query or the body.
The player IS the session, so there is nothing for a caller to edit. Each
declares `Depends(require_player)` by name rather than trusting the shape
guard: a route that names no player id does not match
`ownership_guard`'s "bare player id, no auth" shape, so the explicit
dependency is the ONLY thing guarding it. `has_auth_dependency` recognises it
(`require_player` is in `AUTH_CALLABLE_PREFIXES`), so the installer leaves
these four alone for the right reason -- they authenticate themselves rather
than being waved through.

MONEY: TWO LAYERS, NEITHER OF THEM THE CALLER'S KEY
----------------------------------------------------
1. The `Idempotency-Key` HEADER is REQUIRED on both money routes, via
   `idem_gate.require_idempotency_key`. The server never mints one: a
   per-attempt key double-charges a retry, and a body-derived key swallows a
   second genuine request.
2. The ledger row is keyed on a SERVER-DERIVED identity, never on that header
   and never on the payment token. A claim's natural identity is
   (player, season, tier, level); a purchase's is (player, season, tier). So a
   client that invents a fresh key for its retry is still a replay, which is
   the property a caller-keyed table cannot have. The header is a retry label,
   not the guard.

THE PAYMENT TOKEN IS VERIFIED OR REFUSED, NEVER BELIEVED
---------------------------------------------------------
`purchase_tier` takes a `payment_token`. It is NOT hashed, NOT checked for
length and NOT accepted because the caller asserted it. It is handed to a
verifier a real store integration registers. With no verifier registered --
this slice's state, because it has no real money in it -- the route refuses
with 501 and grants nothing.

That refusal is the feature. `POST /purchase/validate` in main.py keys its
replay dict on a hash of a receipt the CALLER supplied, so a caller who varies
the receipt walks straight past it; design/battle-pass-fix-design.md section
4.2 measured that guard as built, tested and unreachable from production. This
route is the opposite shape: a token is worth nothing until the server has
checked it with something the server trusts, and there is no such thing here,
so the honest answer is "not now".

A verifier is any callable with this signature::

    verifier(token, *, player_id, tier, season_id) -> dict

returning `{"ok": True, "transaction_id": "..."}` or `{"ok": False,
"reason": "..."}`. An exception is a refusal, never a pass.

CURRENCY: BOTH BOOKS, ON BOTH SURFACES
-------------------------------------
A claim here pays through `battle_pass._pay_reward` and a purchase charges
through `battle_pass.spend_wallet_stones`, falling back to the player row --
the SAME helpers, and the SAME two-ledger shape, `battle_pass.py`'s own routes
use. design/battle-pass-fix-design.md section 3.3 ruled this should move to the
wallet (the currency missions, gacha and the shop spend from).

The `/api` money routes used to be the exception: they passed only the player
row, so the wallet -- the store carrying the currency whitelist, the per-key
idempotency ledger and the version guard -- never saw a pass payout or a pass
charge. Measured on this file before the fix: a level-1 free claim paid 55
stones, moved the row 0 -> 55 and the wallet 0 -> 0; a purchase with the full
999 sitting in the wallet was refused 400 "Insufficient Spirit Stones". Both
surfaces now agree.

THE ROW IS STILL WRITTEN, AND THAT IS DELIBERATE. `battle_pass.py`'s own
purchase route charges `player["spirit_stones"]["low_grade"]` and cosmetics
reads the same tier, so a wallet-only debit would move the money somewhere the
pass cannot spend it. Both books move so the wallet holds a replay-guarded
record of every pass movement while the row keeps the stones spendable today.
Reconciling the two ledgers is a tree-wide change and is not this fix. See
`_CURRENCY_NOTE`.

Run: python -c "import battle_pass_routes; battle_pass_routes.declared_paths()"
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

# MODULE SCOPE, AND THAT IS LOAD-BEARING.
#
# This file has `from __future__ import annotations`, so EVERY annotation below
# is a STRING that FastAPI resolves against this module's globals. A
# function-local `from fastapi import Request` therefore leaves `Request`
# unresolvable, and FastAPI silently reclassifies the parameter as a QUERY
# parameter: the route registers, appears in the route table, answers
# `422 {"loc":["query","request"],"msg":"Field required"}` to every real POST,
# and reads as a working endpoint the whole time. Measured on this exact file
# before the imports were hoisted -- both money routes 422'd on every call.
# See the gamedev skill `local-pydantic-model-with-future-annotations`.
#
# `auth` is hoisted with the rest for the same reason, and is safe here: it
# imports only fastapi and the stdlib, so there is no cycle back through main.
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import battle_pass
from battle_pass import BattlePassTier
from idem_gate import require_idempotency_key

from auth import PlayerIdentity, require_player

__all__ = [
    "register_battle_pass_routes",
    "register_payment_verifier",
    "current_payment_verifier",
    "declared_paths",
    "ROUTE_CONTRACT",
    "API_ROUTE_CONTRACT",
]

#: The routes this module promises to mount, with the methods and the
#: caller-supplied inputs each one accepts.
#:
#: Exposed as DATA so a test can assert the mounted table against the promise
#: without parsing this file's source. A guard nobody can read is not a guard;
#: a route list nothing checks is a comment.
ROUTE_CONTRACT = (
    ("GET", "/battle-pass/season", ()),
    ("GET", "/battle-pass/{player_id}", ()),
    # The single-season summary. It was absent from this tuple AND from the
    # mount, so `battle_pass_season_proof.proof_summary` and
    # `play_battle_pass.play()` both read a path that answered 404 for every
    # caller. Declaring it here is what makes the promise checkable: the mount
    # test compares this tuple against the live route table as a SET, so a
    # route this file declares and the app does not carry now fails loudly.
    ("GET", "/battle-pass/{player_id}/summary", ("season",)),
    ("POST", "/battle-pass/{player_id}/claim", ("level", "tier")),
    ("POST", "/battle-pass/{player_id}/claim-all", ("tier",)),
    ("POST", "/battle-pass/{player_id}/upgrade", ("tier",)),
    ("POST", "/battle-pass/{player_id}/weekly-bonus", ("week",)),
)

#: The route that was REMOVED and must not come back.
#:
#: `POST /battle-pass/{player_id}/add-xp` took `amount` off the query string, so
#: any caller could mint any amount of season experience: three POSTs walked one
#: player's XP from 100000 to 300000, and a negative amount walked it backwards.
#: XP is now awarded only in-process by `battle_pass.grant_xp` from earn paths
#: that compute the amount server-side.
REMOVED_ROUTE = ("POST", "/battle-pass/{player_id}/add-xp")

#: The four `/api` routes, with the caller-supplied inputs each accepts.
#:
#: Every one of these names NO player id. The session names the player, so a
#: body that carries `player_id` is a 422 from pydantic rather than a handler
#: that quietly ignores it. Exposed as DATA for the same reason
#: `ROUTE_CONTRACT` is: a guard nobody can read is not a guard.
API_ROUTE_CONTRACT = (
    ("GET", "/battle-pass/api/season-info", ()),
    ("GET", "/battle-pass/api/track-info", ()),
    ("POST", "/battle-pass/api/claim-reward", ("level", "tier")),
    ("POST", "/battle-pass/api/purchase-tier", ("tier", "payment_token")),
)

#: Route names for the idempotency-key namespace. One per money route: a key
#: minted for one must never dedupe the other, because a shared namespace
#: swallows the second request as a replay of the first.
CLAIM_ROUTE = "battle_pass_api_claim"
PURCHASE_ROUTE = "battle_pass_api_purchase"

#: The payment-token verifier, when a store integration registers one. Held in
#: a holder dict rather than a module global read at import: FastAPI re-runs
#: every `register_*` on each `TestClient` context, and a request that arrives
#: after a re-registration must reach the NEW verifier, not the one captured by
#: the first startup's closure.
_VERIFIER_HOLDER: Dict[str, Optional[Callable]] = {"verifier": None}

#: What these routes pay and charge today, in one place, so the next reader does
#: not have to re-derive it and cannot half-migrate one surface.
_CURRENCY_NOTE = (
    "Both books: the wallet (via wallet_dispatch, the store carrying the "
    "currency whitelist, the per-key idempotency ledger and the version guard) "
    "and the player row's spirit_stones.low_grade (via battle_pass.grant_stones "
    "/ battle_pass.stone_balance), exactly as battle_pass.py's own claim and "
    "upgrade routes do. design/battle-pass-fix-design.md section 3.3 ruled this "
    "should move to the wallet; the /api routes carried the ruling last, and a "
    "purchase with the full price in the wallet used to be refused for being "
    "broke. The row is still written because the pass charges it today."
)


def register_payment_verifier(verifier: Optional[Callable]) -> None:
    """Register the callable that decides whether a payment token is real.

    A store integration calls this once at startup. ``None`` uninstalls it,
    which puts `purchase_tier` back into refusing-everything mode -- the state
    this slice ships in.
    """
    _VERIFIER_HOLDER["verifier"] = verifier


def current_payment_verifier() -> Optional[Callable]:
    """The registered verifier, or None. Read per request, never captured."""
    return _VERIFIER_HOLDER.get("verifier")


class PurchaseTierRequest(BaseModel):
    """Body of ``POST /battle-pass/api/purchase-tier``.

    ``player_id`` is deliberately ABSENT: the session names the player. So is
    any ``cost`` or ``amount`` field -- `battle_pass.TIER_COST` is the only
    price in the system, because a caller who can name the price is choosing
    what it costs.
    """

    tier: str
    payment_token: str


def _tier_or_400(tier: str) -> BattlePassTier:
    try:
        return BattlePassTier(tier)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid tier")


def _player_or_404(players_db: Any, player_id: str) -> None:
    """404 for an unknown player. CALL THIS BEFORE ``transaction()``.

    `players_db.transaction(player_id)` raises KeyError itself for an id it
    does not hold, so a check placed inside the block never runs and the caller
    gets a 500 instead of the 404 it should get.
    """
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")


def _verify_payment_token(token: str, *, player_id: str, tier: str,
                          season_id: str) -> Dict[str, Any]:
    """Ask the registered verifier whether this token is a real payment.

    Fails CLOSED three distinct ways, and each is a separate bug if it flipped:

    * no verifier registered -> 501. This slice has no store, so there is
      nothing here that could honestly say yes.
    * the verifier raises -> 502. An unreachable store is not a paid purchase.
    * the verifier says no -> 402, carrying its own reason.

    The token is never hashed and never compared against a locally derived
    value: a key made from a caller-supplied string is a key the caller
    controls.
    """
    verifier = current_payment_verifier()
    if verifier is None:
        raise HTTPException(
            status_code=501,
            detail=(
                "purchase_tier is disabled: no payment verifier is registered. "
                "A store integration must call "
                "battle_pass_routes.register_payment_verifier() at startup. "
                "Until then this route refuses rather than accept a token it "
                "cannot check."
            ),
        )
    try:
        verdict = verifier(token, player_id=player_id, tier=tier,
                           season_id=season_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any store failure is a refusal
        raise HTTPException(status_code=502,
                            detail=f"payment verification failed: {exc}")
    if not isinstance(verdict, dict):
        raise HTTPException(status_code=502,
                            detail="payment verifier returned a non-dict verdict")
    if not verdict.get("ok"):
        raise HTTPException(status_code=402,
                            detail=verdict.get("reason") or "payment token rejected")
    return verdict


def register_battle_pass_routes(app, store_provider: Callable[[], object],
                                wallet_store_provider: Callable[[], object] = None
                                ) -> None:
    """Mount the legacy battle-pass routes AND the four `/api` routes.

    ``store_provider`` is called once per request rather than capturing a
    ``players_db``, so a test that swaps the store sees the swap on the next
    call. ``wallet_store_provider`` is optional and defaults to ``None`` so every
    existing two-argument caller keeps working unchanged.

    REGISTRATION POSITION IS LOAD-BEARING. ``install_ownership_guard(app)`` is
    mounted at the BOTTOM of main.py and REWRITES routes in place, so anything
    registered below that line is never examined by it. main.py:679-687 says the
    same with the tournament_routes call as the worked example.

    Idempotent per app: a second call on the same app object would stack a
    duplicate of every path, and FastAPI silently serves the first of a
    duplicate -- which reads as "the route works" while half the code is dead.
    The marker is set on the APP, not in a module global, so it follows the app
    rather than import order.
    """
    if getattr(app, "_battle_pass_routes_registered", False):
        return
    setattr(app, "_battle_pass_routes_registered", True)

    # The handler bodies for the legacy paths stay in battle_pass.py, byte for
    # byte, because two control tests anchor on their source text. See the
    # module docstring.
    battle_pass.register_battle_pass_routes(app, store_provider,
                                            wallet_store_provider)

    _register_api_routes(app, store_provider, wallet_store_provider)


def _register_api_routes(app, store_provider: Callable[[], object],
                         wallet_store_provider: Optional[Callable[[], object]] = None
                         ) -> None:
    """The four `/api` handlers. Split out so `register_battle_pass_routes`
    reads as "delegate, then mount ours" rather than one 250-line block.

    Every store is resolved at REQUEST time, never captured: main.py builds
    these objects while this module is being imported, so a module-level
    capture would bind None.
    """
    # No function-local imports here, and that is deliberate: with
    # `from __future__ import annotations` a handler annotation is a string
    # FastAPI resolves against MODULE globals, so `Request` and
    # `PlayerIdentity` imported in here would leave `request` unresolvable and
    # FastAPI would reclassify it as a query parameter. See the import block.
    def players() -> Any:
        return store_provider() if callable(store_provider) else store_provider

    def wallet() -> Any:
        """The bound wallet store, or None. Resolved PER REQUEST.

        main.py mounts this module with the wallet provider, so the `/api`
        money routes now move the same balance the legacy ones already do.
        Never raises: an unreadable wallet must not turn a claim into a 500
        after the reservation was already taken.
        """
        if wallet_store_provider is None:
            return None
        try:
            return wallet_store_provider()
        except Exception:  # noqa: BLE001 -- no wallet is a fallback, not a fault
            return None

    @app.get("/battle-pass/api/season-info")
    async def get_season_info(identity: PlayerIdentity = Depends(require_player)):
        """The live season clock and what the three tracks cost.

        The clock comes from `battle_pass_season`, the single server-timed
        source of truth -- never from the request and never from the client's
        clock. Nothing here names a player, so the route is session-only.
        """
        now = time.time()
        return {
            "ok": True,
            "season": battle_pass.get_season_progress(now),
            "season_number": battle_pass.get_season_number(now),
            "max_level": battle_pass.MAX_LEVEL,
            "season_xp_total": battle_pass.TOTAL_XP,
            "tiers": [
                {"tier": tier.value,
                 "cost": battle_pass.TIER_COST[tier],
                 "currency_note": _CURRENCY_NOTE}
                for tier in (BattlePassTier.FREE, BattlePassTier.SENIOR,
                             BattlePassTier.SUPER)
            ],
        }

    @app.get("/battle-pass/api/track-info")
    async def get_track_info(identity: PlayerIdentity = Depends(require_player)):
        """Every row of the reward track, marked claimed / claimable / locked.

        State is computed server-side from the reward table and the player's own
        level, because a client that reconstructs "claimed" by subtraction gets
        it wrong the moment a tier is claimed out of order.
        """
        players_db = players()
        player_id = identity.player_id
        # Existence first: transaction() raises KeyError on an unknown id.
        _player_or_404(players_db, player_id)

        with players_db.transaction(player_id):
            bp = battle_pass.get_or_create_bp(players_db[player_id], time.time())
            return {
                "ok": True,
                "season_id": bp.season_id,
                "level": bp.level,
                "max_level": battle_pass.MAX_LEVEL,
                "xp": bp.xp,
                "tier": bp.tier.value,
                "claimed": {
                    "free": list(bp.claimed_free),
                    "senior": list(bp.claimed_senior),
                    "super": list(bp.claimed_super),
                },
                "track": battle_pass.track_view(bp),
            }

    @app.post("/battle-pass/api/claim-reward")
    async def post_claim_reward(request: Request, level: int, tier: str,
                                identity: PlayerIdentity = Depends(require_player)):
        """Claim one position on one track. Pays once, ever.

        RESERVE FIRST, PAY SECOND, inside one transaction. A refusal or a crash
        mid-payout rolls the marker back and the position stays claimable, so
        there is no state in which a reward was paid with no receipt.

        The `Idempotency-Key` header is required and is NOT the key. The ledger
        row is keyed on (player, season, tier, level) -- the natural identity of
        the claim -- so a retry that invents a brand-new key is still a replay,
        and gets the first attempt's stored reward back rather than a bare
        "already claimed".

        The caller names WHICH position it is claiming, because it has to: that
        is the button the player pressed. It does not name what the position pays
        or whether it has been paid.
        """
        # Required, never synthesised. See idem_gate.
        require_idempotency_key(request, route=CLAIM_ROUTE)

        players_db = players()
        player_id = identity.player_id
        # Existence and argument checks BEFORE the transaction, so an unknown
        # player is a 404 and a bad level is a 400 rather than a KeyError or a
        # burned ledger key.
        _player_or_404(players_db, player_id)
        tier_enum = _tier_or_400(tier)
        if level < 1 or level > battle_pass.MAX_LEVEL:
            raise HTTPException(
                status_code=400,
                detail=f"level must be 1-{battle_pass.MAX_LEVEL}, got {level}")

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = battle_pass.get_or_create_bp(player, time.time())

            is_first, stored = battle_pass.reserve_claim(
                players_db, player_id, bp.season_id, tier_enum.value, level)
            if not is_first:
                # Already paid this exact position. Pay nothing again, and hand
                # back the FIRST attempt's receipt re-nested under the same
                # "reward" key -- a different SHAPE on the retry is what makes a
                # client that timed out read `undefined`.
                receipt = stored or {}
                return {
                    "ok": True,
                    "replayed": True,
                    "reward": receipt.get("reward", {}),
                    "level": level,
                    "tier": tier_enum.value,
                    "season_id": bp.season_id,
                    "new_level": receipt.get("new_level", bp.level),
                }

            ok, reward = battle_pass.claim_reward(bp, level, tier_enum)
            if not ok:
                # Give the reserved key BACK before refusing. Without this a
                # refusal permanently burns the position for the whole season:
                # the ledger says "already paid" while the player received
                # nothing, and every later attempt -- including one that has
                # since earned the level -- is answered as a replay with an
                # EMPTY reward and a 200. In no-database mode the reservation
                # lives in a process-local dict with no rollback, so the
                # release is the only thing that undoes it; with a database the
                # HTTPException rolls the reserved row back with the
                # transaction and release_claim is a no-op.
                #
                # This is the claim-side counterpart to release_purchase
                # below, and it is why the two ledgers behave the same way on
                # a retry. battle_pass.py's own /battle-pass/{id}/claim route
                # has had this call since the burn was first measured there;
                # this /api twin was left behind when it was copied.
                battle_pass.release_claim(
                    players_db, player_id, bp.season_id, tier_enum.value, level)
                raise HTTPException(status_code=400,
                                    detail=reward.get("error", "Claim failed"))

            # THE REWARD GOES THROUGH wallet_dispatch. This call used to pass
            # only the player row, so the two-argument `_pay_reward` skipped its
            # wallet leg entirely and the store carrying the currency whitelist
            # and the per-key idempotency ledger never saw the grant. Measured
            # on this file before the change: a level-1 free claim paid 55
            # stones, moved the player row 0 -> 55 and the WALLET 0 -> 0.
            #
            # The key is the SERVER-DERIVED one, the same natural identity
            # (player, season, tier, position) this route's own claim ledger is
            # keyed on, so a client that varies its `Idempotency-Key` header is
            # still a replay in the wallet.
            battle_pass._pay_reward(
                player, reward, wallet_store=wallet(), player_id=player_id,
                claim_key=battle_pass.wallet_claim_key(
                    player_id, bp.season_id, tier_enum.value, level))
            # Claiming awards no XP, so no level recompute here.
            player["battle_pass"] = bp.as_dict()
            # Same transaction, so no other process can read this receipt while
            # it is still empty.
            battle_pass.record_claim_reward(
                players_db, player_id, bp.season_id, tier_enum.value, level,
                reward, player_level=bp.level)

            return {
                "ok": True,
                "replayed": False,
                "reward": reward,
                "level": level,
                "tier": tier_enum.value,
                "season_id": bp.season_id,
                "new_level": bp.level,
            }

    @app.post("/battle-pass/api/purchase-tier")
    async def post_purchase_tier(request: Request, req: PurchaseTierRequest,
                                 identity: PlayerIdentity = Depends(require_player)):
        """Buy a paid track. Once per tier per season, against a verified token.

        FOUR GATES, IN THIS ORDER, and the order is the design:

        1. the token is verified by the registered verifier (never believed)
        2. `Idempotency-Key` header required (never minted)
        3. the ledger row, keyed on (player, season, tier), server-derived
        4. already-owns check -- a player who holds the tier is not charged
           again, and this runs AFTER the ledger so a genuine retry of a
           purchase that succeeded is answered with its stored receipt
           rather than rewritten as "you already own this"

        The price comes from `battle_pass.TIER_COST` and nowhere else, so a
        caller cannot name what a tier costs.

        Steps 3 and 4 are in THIS ORDER because the reverse order silently
        destroyed the receipt. A retry arrives with the player already holding
        the tier, so an already-owns gate placed first caught every retry and
        answered `cost: 0, replayed: false` with the transaction id dropped --
        a client renders a 999-stone purchase as a free upgrade. Step 4
        releases the key it just took, so "a fresh key for a tier they already
        own" still does not burn that key.

        The ledger key does NOT contain the payment token. If it did, a retry
        with the same token would collide (correct) but a second purchase of the
        same tier would not (wrong), and a caller who varied the token would walk
        past the gate entirely. (player, season, tier) is the natural identity of
        "this player bought this track this season".
        """
        players_db = players()
        player_id = identity.player_id
        _player_or_404(players_db, player_id)
        tier_enum = _tier_or_400(req.tier)

        cost = battle_pass.TIER_COST[tier_enum]
        if cost <= 0:
            # Free tier is not a purchase; it is the default. Charging 0
            # through the ledger would burn the key and deny the real purchase
            # behind it.
            raise HTTPException(
                status_code=400,
                detail="the free tier is not a purchase; it is the default")

        token = (req.payment_token or "").strip()
        if not token:
            raise HTTPException(status_code=400,
                                detail="payment_token is required")

        with players_db.transaction(player_id):
            _player_or_404(players_db, player_id)
            player = players_db[player_id]
            bp = battle_pass.get_or_create_bp(player, time.time())

            # 1. Verified BEFORE money moves and BEFORE a key is burned, so a
            #    rejected token leaves the player able to try again with a good
            #    one.
            verdict = _verify_payment_token(token, player_id=player_id,
                                            tier=tier_enum.value,
                                            season_id=bp.season_id)

            # 2. Required, never synthesised. See idem_gate.
            require_idempotency_key(request, route=PURCHASE_ROUTE)

            # 3. SERVER-DERIVED LEDGER, BEFORE the already-owns gate.
            #
            #    ORDER IS THE FIX. A genuine retry of a purchase that SUCCEEDED
            #    arrives with the player already holding the tier, so an
            #    already-owns gate in FRONT of this line answered every retry
            #    with cost=0 / replayed=false and dropped the receipt and its
            #    transaction_id. Measured on this file: replaying a completed
            #    senior purchase returned
            #      {"cost": 0, "replayed": false, "owned": true,
            #       "detail": "Already holds senior"}
            #    -- the client renders a 999-stone purchase as a free upgrade,
            #    and the id it needs to reconcile the charge is gone. The note
            #    below used to promise the opposite and the code could not
            #    deliver it, because the gate made the ledger unreachable.
            ledger_key = f"bp_purchase:{player_id}:{bp.season_id}:{tier_enum.value}"
            is_first, stored = battle_pass.reserve_purchase(
                players_db, ledger_key, player_id, bp.season_id,
                tier_enum.value, cost)
            if not is_first:
                receipt = stored or {}
                if not receipt:
                    # The row exists but carries NO receipt: a reservation whose
                    # charge unwound, or the no-database store, which keeps no
                    # receipt at all. Nothing was paid, so answer with the
                    # ownership fact. Falling back to `cost` here would report a
                    # price the player was never charged.
                    return {"ok": True, "new_tier": bp.tier.value, "cost": 0,
                            "replayed": True, "owned": True,
                            "detail": f"Already holds {bp.tier.value}"}
                return {"ok": True,
                        "new_tier": receipt.get("new_tier", bp.tier.value),
                        "cost": receipt.get("cost", cost),
                        "replayed": True,
                        "owned": True,
                        "transaction_id": receipt.get("transaction_id")}

            # 4. Already owns it, or a higher tier: free. Runs AFTER the
            #    reservation, so it can tell a REPLAY (answered from the
            #    receipt above) from a FIRST attempt that merely has nothing to
            #    buy. The key it just took is handed straight back, so a fresh
            #    key for a tier the player already holds does not burn it: in
            #    the no-database store that row is discarded, and on PostgreSQL
            #    it is harmless, because a player never buys the same tier twice
            #    in one season, so the key can never be needed for a real
            #    purchase later.
            if battle_pass._tier_unlocked(bp.tier, tier_enum):
                battle_pass.release_purchase(ledger_key)
                return {"ok": True, "new_tier": bp.tier.value, "cost": 0,
                        "replayed": False, "owned": True,
                        "detail": f"Already holds {bp.tier.value}"}

            # EITHER LEDGER CAN PAY, and it is the LARGER of the two -- never the
            # sum. Every award credits the wallet as well as the row (see
            # `_pay_reward`), so a player whose stones arrived through the wallet
            # holds the exact price there and an empty row. The row-only check
            # that used to stand here refused them for being broke while holding
            # the money. Summing instead of taking the larger would advertise
            # 500 row + 500 wallet and then decline a 900 purchase neither book
            # alone covers.
            # NOT named `wallet`: a local of that name shadows the `wallet()`
            # resolver for the rest of the function and the next call raises
            # UnboundLocalError -- a 500 on every purchase.
            wallet_store = wallet()
            row_stones = battle_pass.stone_balance(player)
            wallet_stones = battle_pass.wallet_stone_balance(wallet_store,
                                                             player_id)

            try:
                if max(x for x in (row_stones, wallet_stones) if x is not None) < cost:
                    # No release_purchase() here on purpose: the HTTPException
                    # unwinds players_db.transaction(), whose rollback takes the
                    # inserted row with it. Calling release as well would be a
                    # second, divergent path for one condition.
                    raise HTTPException(status_code=400,
                                        detail="Insufficient Spirit Stones")

                # THE FEE GOES THROUGH wallet_dispatch, wallet first, and only
                # when that book can actually pay. The row write that used to
                # stand here bypassed the currency whitelist, the per-key
                # idempotency ledger and the version guard, so the store that
                # owns money had no record of the pass charging a player.
                # Measured on this file before the change: a wallet holding the
                # full 999 was refused 400 "Insufficient Spirit Stones" and the
                # wallet never moved.
                #
                # THE ROW FALLBACK IS NOT A BYPASS. The two books are two views
                # of one balance, not two balances to pick from, so a debit that
                # touched only the wallet would let the row re-spend stones
                # already spent. battle_pass.py's own upgrade route charges the
                # same way, for the same reason.
                #
                # A wallet REFUSAL that is not a replay is not silently
                # overwritten by the row debit: the purchase key is handed back
                # and the refusal is raised, because "insufficient balance" is an
                # answer about the player's money.
                wallet_ok = False
                if wallet_store is not None and (wallet_stones or 0) >= cost:
                    receipt = battle_pass.spend_wallet_stones(
                        wallet_store, player_id, cost,
                        battle_pass.wallet_upgrade_key(
                            player_id, bp.season_id, tier_enum.value))
                    if receipt is not None and receipt.get("ok"):
                        wallet_ok = True
                    elif receipt is not None and not receipt.get("_replayed"):
                        battle_pass.release_purchase(ledger_key)
                        raise HTTPException(
                            status_code=400,
                            detail=str(receipt.get("error")
                                       or "Insufficient Spirit Stones"))
                if not wallet_ok:
                    if row_stones < cost:
                        battle_pass.release_purchase(ledger_key)
                        raise HTTPException(
                            status_code=400,
                            detail="Insufficient Spirit Stones")
                    stones = player.get("spirit_stones")
                    if isinstance(stones, dict):
                        stones["low_grade"] = row_stones - cost
                    else:
                        player["spirit_stones"] = row_stones - cost
                battle_pass.upgrade_tier(bp, tier_enum)
                player["battle_pass"] = bp.as_dict()

                response = {"ok": True, "new_tier": bp.tier.value, "cost": cost,
                            "replayed": False, "owned": True,
                            "transaction_id": verdict.get("transaction_id")}
                # Same transaction, so no other process can read this key and
                # see a ledger row whose receipt is still empty.
                battle_pass.record_purchase_response(players_db, ledger_key,
                                                     response)
            except Exception:
                # PostgreSQL rolls the row back with the failed charge. The
                # in-memory fallback has no rollback, so hand the key back or a
                # player who taps buy while short can never complete the
                # purchase after topping up.
                battle_pass.release_purchase(ledger_key)
                raise
            return response


def declared_paths() -> List[str]:
    """Every path this module declares it mounts, legacy and `/api`.

    Read by the registration guard. One list, so a guard cannot check half of
    what this module promises.
    """
    return ([path for _method, path, _params in ROUTE_CONTRACT]
            + [path for _method, path, _params in API_ROUTE_CONTRACT])
