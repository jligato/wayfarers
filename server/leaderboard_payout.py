"""Wayfarer's Cycle — guarded, exactly-once leaderboard season payout.

Why this module exists
----------------------
``server/leaderboard_rewards.py`` is a PURE calculator: rank in, amount out. It
holds no record of who has been paid, so it cannot be exposed as a claim route
on its own -- a player pressing the button twice would be paid twice, and
because the band table pays the top-20 finish ten times the bottom band, that
is the most expensive repeatable reward in the game.

This module is the missing half. It adds the three things the calculator cannot:

  1. A SEASON KEY. A payout belongs to one leaderboard season, so the already-
     paid record has to be per season, not a single boolean. ``season_id`` is
     the UTC calendar month (``"2026-09"``), which matches the battle pass
     season boundary so both systems roll over on the same day.
  2. A DURABLE ALREADY-PAID FLAG stored on the player record
     (``leaderboard_payout.paid_seasons``). PlayerStore serialises
     non-column fields into ``players.extra_state``, so the flag survives a
     process restart and a database reload.
  3. A WALLET WRITE with a per-player idempotency key
     ``leaderboard_payout:<player_id>:<season_id>``, so even if the flag were
     somehow bypassed the wallet store's own receipt table refuses the second
     payment. Two independent guards, because one guard is one bug away from
     paying twice.

The proven in-repo pattern this mirrors
---------------------------------------
``server/tribulation.py``'s claim-aftermath route solved the identical problem
(one payout per cycle, not per button press) with a per-cycle claim ledger and
a guard held inside ``players_db.transaction(player_id)``. That store
transaction takes a database-level advisory lock
(``pg_advisory_xact_lock(GLOBAL_LOCK_ID)``) and re-reads the player fresh under
READ COMMITTED, which is what makes a check-then-record safe. The rule learned
there, applied here:

    THE CHECK AND THE WRITE MUST BE IN THE SAME GUARDED REGION.

Doing the check outside the transaction means concurrent requests each read an
unpaid flag, each decide to pay, and each write the flag back -- every one of
them pays. That is a real bug that shipped once already in this repo.

Currency
--------
Spirit stones, paid through ``wallet_dispatch.earn`` so the in-memory and
PostgreSQL wallet backends both work without this module knowing which is live.

Who owns the amount AND the band
--------------------------------
Two independent authorities, both server-side:

* :data:`PAYOUT_REWARD_TABLE` owns the amount.
* :data:`SEASON_STANDING_KEY` owns the band, via :func:`server_rank`.

The band was the second mint, and it survived the first fix. An earlier version
of this file already refused the caller any say over the AMOUNT: the band
table paid the top-20 finish ten times the bottom band, but the caller sent
``rank`` in the body and the server happily priced whatever band that rank
selected. So the server was pricing an attacker's choice. The measured result,
against real PostgreSQL, was that any signed-in player who sent ``rank: 1``
received 1000 spirit stones -- the top band -- having done nothing to earn it.
Pricing authority and band authority are separate axes in the same way amount
and idempotency were: a flag answers HOW MANY times, an amount answers HOW
MUCH, and neither answers WHICH BAND.

The fix follows the same shape as the amount fix: delete the caller's authority
rather than bound it. ``claim()`` no longer takes a ``rank`` parameter at all.
It reads the rank the SERVER recorded when it settled the season, out of
durable player state written by :func:`record_standing`. A caller cannot write
that field: no request model in this module accepts it, and no route reaches
the function that writes it.

The request field survives on the wire for one reason only: a shipped client
that still sends ``rank`` must keep validating, and a 422 on an extra-but-
harmless field is a client-visible regression bought for no security gain. It
is inert, echoed back as ``requested_rank``.

A player the server never ranked is not a player who finished outside the
paying bands. Both price to zero and neither burns the season, but they report
different reasons (``no_server_ranked_standing`` vs
``rank_outside_paying_bands``) so an operator can tell "we have not settled
this season yet" from "you finished 201st".

Who owns the identity
---------------------
Both routes carry ``Depends(require_player)`` and re-check that the session's
player matches ``{player_id}`` in the path. The mounted ownership guard in
``main.py`` already covers this shape -- measured, not assumed -- but the
module states its own requirement rather than depending on a mount that lives
in another file and can be reordered. A signed-in attacker claiming for
somebody else gets 403; an anonymous caller gets 401; neither moves a balance.

The amount field is likewise inert, echoed as ``requested_base_amount``:
observability, not a second input.
"""
from __future__ import annotations

import time
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fastapi import Depends
from pydantic import BaseModel, Field

from auth import PlayerIdentity, require_player
from leaderboard_rewards import (
    BAND_NONE,
    band_for_rank,
    reward_for_rank_int,
)

STATE_KEY = "leaderboard_payout"
"""Public name for the player-dict key, so routes and tests never reach into a
private constant."""

PAYOUT_CURRENCY: str = "spirit_stone"
"""Wallet currency the season payout is paid in. ``wallet_store_pg.CURRENCIES``
accepts exactly this value."""

DEFAULT_BASE_AMOUNT: float = 100.0
"""Flat amount the lowest paying band (ranks 51-200) receives. Matches the
default of the read-only GET /leaderboard/reward-table route in main.py.

Retained as a module-level name because tests and the read-only table route
pin against it. It is NOT a payout input any more -- :data:`PAYOUT_REWARD_TABLE`
is, and the default is read out of that table so the two can never drift.
"""

PAYOUT_REWARD_TABLE: Mapping[str, float] = MappingProxyType(
    {PAYOUT_CURRENCY: DEFAULT_BASE_AMOUNT}
)
"""The server-owned reward table: currency -> flat amount the lowest paying
band receives. Every band above that is this value times the server-owned
band multipliers in :mod:`server.leaderboard_rewards`.

This is the only thing that decides what a claim pays. A ``MappingProxyType``
rather than a dict so no request-scoped code can mutate the table in place
and widen every future payout in the process.

To re-price the season payout, edit this table. To do that per season, extend
it with season keys and pass the season into :func:`server_base_amount`; the
lookup and the mint are already separated, so nothing else has to change.
"""


def server_base_amount(currency: str = PAYOUT_CURRENCY) -> float:
    """The base amount for ``currency``, read from :data:`PAYOUT_REWARD_TABLE`.

    The single entry point through which this module learns what a band is
    worth. Takes the currency, never an amount -- there is deliberately no
    parameter a caller could pass to influence the return value.
    """
    try:
        return float(PAYOUT_REWARD_TABLE[currency])
    except KeyError:
        raise LeaderboardPayoutError(
            f"no server reward table entry for currency {currency!r}"
        ) from None


def max_payout(currency: str = PAYOUT_CURRENCY) -> int:
    """The most any single claim can ever pay, for any rank and any request.

    Exists so the bound is a stated property of the server rather than a
    number someone has to remember: the top band multiplier applied to the
    server's own base. A caller who sends any ``base_amount`` whatsoever, at
    any rank, cannot exceed this. Expressed as a relationship between the
    table and the band table rather than a hard-coded figure, so re-pricing
    either one moves it correctly.
    """
    return reward_for_rank_int(1, server_base_amount(currency))


def _now() -> float:
    """The single clock seam in this module.

    Everything time-dependent calls this, so a test can freeze the season
    boundary by patching ``leaderboard_payout._now``. It deliberately does NOT
    do what ``monkeypatch.setattr(module.time, "time", ...)`` would: ``module.time``
    IS the standard library ``time`` module, so that edit replaces ``time.time``
    for the ENTIRE test process and leaks into every other test collected after
    this one (the failure mode documented in server/conftest.py).
    """
    return time.time()


def season_id(now: Optional[float] = None) -> str:
    """The leaderboard season a timestamp belongs to, e.g. ``"2026-09"``.

    UTC calendar month, deliberately identical to
    ``battle_pass.get_season_id`` so the two seasonal systems roll over on the
    same day rather than drifting apart by a month boundary.

    A calendar month rather than a fixed-length window: seasons must be
    wall-clock aligned so every player in every timezone sees the same board.
    """
    if now is None:
        now = _now()
    return time.strftime("%Y-%m", time.gmtime(now))


def default_state() -> Dict[str, Any]:
    """A fresh, never-paid state."""
    return {"paid_seasons": [], "last_payment": None}


def get_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """Return the player's payout state, creating and repairing it if absent.

    Tolerant of a legacy or partial dict: a pre-upgrade save has no
    ``leaderboard_payout`` key at all, and a hand-edited one may have a
    non-list ``paid_seasons``. Both are repaired from ``default_state`` rather
    than raising, because a payout route that 500s on an old save is worse than
    one that starts the player off unpaid.

    This WRITES to ``player``. Read-only callers must use :func:`read_state`.
    """
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        player[STATE_KEY] = raw
    if not isinstance(raw.get("paid_seasons"), list):
        raw["paid_seasons"] = []
    if "last_payment" not in raw:
        raw["last_payment"] = None
    return raw


def read_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """The player's payout state, defaulted WITHOUT writing to ``player``.

    The read-side counterpart to :func:`get_state`, and the reason
    :func:`preview` stopped being a mutating read.

    :func:`preview` is served by ``GET /leaderboard/{player_id}/payout-status``
    and is documented as mutating nothing, but it went through the repairing
    getter. Every status poll therefore attached a fresh ``leaderboard_payout``
    key to the player's row. That is a write on a read path, and it behaves
    differently per backend: the real store is copy-on-read, so the write was
    silently dropped, while an in-memory store kept it. Same request, two
    different outcomes depending on which backend was live.

    Returns the stored dict as-is when it is well formed, so a caller reading
    ``paid_seasons`` still sees the live list, and a defaulted copy otherwise.
    """
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        return default_state()
    paid = raw.get("paid_seasons")
    if not isinstance(paid, list):
        return default_state()
    return raw


def has_paid(player: Dict[str, Any], season: str) -> bool:
    """Pure read: has this player already been paid for ``season``?"""
    state = player.get(STATE_KEY)
    if not isinstance(state, dict):
        return False
    paid = state.get("paid_seasons")
    if not isinstance(paid, list):
        return False
    return season in paid


def preview(player: Dict[str, Any], rank: int, now: Optional[float] = None,
            settling: bool = False) -> Dict[str, Any]:
    """What a claim right now WOULD do. Mutates nothing.

    Lets the client grey out a button without reimplementing the guard rule.

    ``settling`` says the season's ranks are being written RIGHT NOW. It is a
    plain boolean, not a database read, so this function stays pure and
    testable: the route supplies it from
    ``leaderboard_settlement.season_is_settling``.
    """
    season = season_id(now)
    multiplier_band = band_for_rank(rank) if rank and rank > 0 else BAND_NONE
    return {
        "season": season,
        "rank": rank,
        "band": multiplier_band,
        "already_paid": has_paid(player, season),
        "settling": bool(settling),
        # read_state, not get_state: this is a read, and the repairing
        # getter would attach a leaderboard_payout key to the player's
        # dict here -- a write on a GET path.
        "paid_seasons": list(read_state(player)["paid_seasons"]),
    }


class LeaderboardPayoutError(Exception):
    """Raised when a season payout cannot be completed."""


class LeaderboardPayoutRequest(BaseModel):
    """Body of POST /leaderboard/{player_id}/claim-payout.

    Declared at MODULE level, not inside register_*: this module uses
    ``from __future__ import annotations``, so a nested class's annotations are
    strings that FastAPI cannot resolve, and the route silently degrades to
    treating the body as a query parameter (every POST then 422s).
    """

    rank: int = Field(
        ...,
        ge=0,
        allow_inf_nan=False,
        description=(
            "DEPRECATED and INERT. Accepted only so a shipped client that still "
            "sends this field keeps validating. The band is decided by the "
            "rank the SERVER recorded when it settled the season, read from "
            "durable player state; sending a different number here does not "
            "change which band is paid. Your value is echoed back as "
            "`requested_rank`."
        ),
    )
    base_amount: float = Field(
        DEFAULT_BASE_AMOUNT,
        ge=0,
        allow_inf_nan=False,
        description=(
            "DEPRECATED and INERT. Accepted only so a shipped client that "
            "still sends this field keeps validating; it is NOT authoritative "
            "and has no effect on the amount paid. The payout is derived from "
            "the player's rank via the server-owned reward table. Sending a "
            "different value than the server's own will not change what is "
            "paid -- the server's value is echoed back as `base_amount` and "
            "yours as `requested_base_amount`."
        ),
    )


#: Name of the durable per-player record the server writes when a season is
#: settled, holding the rank the SERVER assigned. Keyed on the player dict
#: (serialised into ``players.extra_state``), so a caller cannot write it: no
#: route accepts it in a request body.
SEASON_STANDING_KEY = "leaderboard_standing"

#: The only rank a claim may use when the server has recorded no standing for
#: this player in this season. Zero is the pre-existing "not on the board"
#: sentinel in :mod:`server.tower` and is rejected by ``reward_for_rank_int``'s
#: caller-side validation, so it prices to zero and burns no season.
UNRANKED = 0


class UnrankedPlayerError(Exception):
    """Raised when a claim arrives for a player the server never ranked.

    Distinct from "rank 201", which is a real finishing position that happens
    to pay nothing. An absent standing is not a result; it is the absence of
    one, and it must not be payable by assertion.
    """


def record_standing(
    player: Dict[str, Any], season: str, rank: int, now: Optional[float] = None
) -> Dict[str, Any]:
    """Server-side settlement: write the rank the SERVER assigned this player.

    This is the only writer of :data:`SEASON_STANDING_KEY`. It is deliberately
    a plain function with no HTTP route of its own in this module, so a caller
    has no request field that reaches it. A season-settlement job calls this
    with a rank it computed; :func:`server_rank` reads it back.

    Re-recording the same season overwrites rather than appends, so a corrected
    settlement does not leave a stale rank behind for the claim to use.
    """
    if not isinstance(player, dict):
        raise LeaderboardPayoutError("player must be a dict")
    season = season or season_id(now)
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < UNRANKED:
        raise LeaderboardPayoutError(f"rank must be a non-negative int, got {rank!r}")
    table = player.setdefault(SEASON_STANDING_KEY, {})
    if not isinstance(table, dict):
        table = {}
        player[SEASON_STANDING_KEY] = table
    table[season] = int(rank)
    return {"season": season, "rank": int(rank)}


def server_rank(player: Dict[str, Any], season: str) -> int:
    """The rank the SERVER recorded for ``player`` in ``season``.

    Returns :data:`UNRANKED` when there is none. This is the sole input to the
    band, and it is read from durable player state rather than from the
    request, so the caller cannot select a band by typing a smaller number.
    """
    if not isinstance(player, dict):
        return UNRANKED
    table = player.get(SEASON_STANDING_KEY)
    if not isinstance(table, dict):
        return UNRANKED
    rank = table.get(season)
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < UNRANKED:
        return UNRANKED
    return rank


def payout_key(player_id: str, season: str) -> str:
    """The wallet idempotency key for one player's one season.

    The player id is part of the key on purpose. PostgreSQL's wallet receipt
    table has a globally unique key column and REJECTS a key presented by a
    different player, so omitting the player id would let one player's claim
    block another's.
    """
    return f"leaderboard_payout:{player_id}:{season}"


def _banked_band(state: Any, season: str, fallback_rank: int) -> Tuple[int, str]:
    """The rank and band actually BANKED for ``season``.

    ``fallback_rank`` is the live server standing, used only when the durable
    record is missing or names a different season -- a legacy save, or one that
    was hand-edited.

    Why this exists: the already-paid branch used to price the CURRENT standing,
    so a corrected re-settlement made a player who really was paid the top band
    read ``band="outer", reward=0`` on their own claim. That contradicts the
    durable ``last_payment`` written moments earlier -- the same record the
    module already treats as the audit authority -- and tells the player their
    payout was worth nothing. No money moves on that branch, so this was never
    a mint; it is a player-visible lie about a payment that really happened.
    """
    record = state.get("last_payment") if isinstance(state, dict) else None
    if not isinstance(record, dict) or record.get("season") != season:
        return (
            fallback_rank,
            band_for_rank(fallback_rank) if fallback_rank >= 1 else BAND_NONE,
        )
    paid_rank = record.get("rank")
    paid_band = record.get("band")
    if not isinstance(paid_rank, int) or isinstance(paid_rank, bool):
        paid_rank = fallback_rank
    if not isinstance(paid_band, str) or not paid_band:
        paid_band = band_for_rank(paid_rank) if paid_rank >= 1 else BAND_NONE
    return int(paid_rank), paid_band


def claim(
    player: Dict[str, Any],
    now: Optional[float] = None,
    settling: bool = False,
) -> Dict[str, Any]:
    """Pure-with-respect-to-wallet claim evaluation. Mutates ``player``.

    Records the season in ``paid_seasons`` but does NOT pay. The wallet write
    is the caller's job, inside the same store transaction -- see the module
    docstring. Splitting it this way keeps the guard rule in one testable place
    and keeps the wallet write impossible to omit silently: the route must call
    ``earn`` before it persists, and the tests prove both halves.

    NOTE FOR ROUTE AUTHORS: as with the tribulation aftermath route, the caller
    MUST hold ``players_db.transaction(player_id)`` and re-read
    ``players_db[player_id]`` inside it.

    There is no ``rank`` parameter and no ``base_amount`` parameter, and both
    absences are the point.

    * No ``rank``: the band used to be chosen by the caller, who sent whatever
      rank paid best. Pricing the amount server-side did not fix that -- the
      SERVER priced whatever BAND the caller picked, and the top band is ten
      times the bottom one, so the caller simply claimed rank 1. Authority over
      the band is now held by :func:`server_rank`, which reads the rank the
      server recorded at settlement time out of durable player state. Removing
      the parameter rather than ignoring the request value means a future route
      author cannot reintroduce the defect by forwarding ``req.rank``, and a
      test can assert the absence structurally.

    * No ``base_amount``: the amount is derived here from
      :func:`server_base_amount`, so the server's own table prices the band.

    ``settling`` is the C3 refusal, and it is a boolean rather than a database
    read so this function stays pure. The route supplies it from
    ``leaderboard_settlement.season_is_settling``. When true, the season's
    ranks are being written RIGHT NOW, and a player who claimed in that window
    would be paid against a rank a retry is about to overwrite -- so the
    number they were paid would stop describing their standing a minute later.
    It pays ZERO, records nothing, and burns no season: the same shape as the
    existing ``no_server_ranked_standing`` refusal, and it touches no money
    guard, no band selection, and no transaction.
    """
    if not isinstance(player, dict):
        raise LeaderboardPayoutError("player must be a dict")

    season = season_id(now)
    state = get_state(player)
    base_amount = server_base_amount(PAYOUT_CURRENCY)
    rank = server_rank(player, season)

    if season in state["paid_seasons"]:
        # Report WHAT WAS BANKED, not what the standing says now.
        paid_rank, paid_band = _banked_band(state, season, rank)
        return {
            "ok": True,
            "already_paid": True,
            "season": season,
            "rank": paid_rank,
            "band": paid_band,
            "reward": 0,
            "base_amount": base_amount,
            "currency": PAYOUT_CURRENCY,
            "reason": "already_paid_this_season",
        }

    if settling:
        return {
            "ok": True,
            "already_paid": False,
            "season": season,
            "rank": rank,
            "band": BAND_NONE,
            "reward": 0,
            "base_amount": base_amount,
            "currency": PAYOUT_CURRENCY,
            "reason": "season_settling",
        }

    band = band_for_rank(rank) if rank >= 1 else BAND_NONE
    reward = reward_for_rank_int(rank, base_amount) if rank >= 1 else 0
    if reward <= 0:
        # Either the player is outside the paying bands, or the server never
        # ranked them this season. Both price to zero, by design.
        # We deliberately do NOT record the season: a player who has earned
        # nothing yet must not burn their one seasonal claim on a zero payout,
        # which would silently deny them a reward for a later, better rank.
        # Nothing was paid, so nothing needs guarding.
        return {
            "ok": True,
            "already_paid": False,
            "season": season,
            "rank": rank,
            "band": BAND_NONE,
            "reward": 0,
            "base_amount": base_amount,
            "currency": PAYOUT_CURRENCY,
            "reason": (
                "no_server_ranked_standing"
                if rank == UNRANKED
                else "rank_outside_paying_bands"
            ),
        }

    state["paid_seasons"].append(season)
    state["last_payment"] = {
        "season": season,
        "rank": rank,
        "band": band,
        "reward": reward,
        "base_amount": base_amount,
        "currency": PAYOUT_CURRENCY,
    }
    return {
        "ok": True,
        "already_paid": False,
        "season": season,
        "rank": rank,
        "band": band,
        "reward": reward,
        "base_amount": base_amount,
        "currency": PAYOUT_CURRENCY,
        "reason": "paid",
    }


def register_leaderboard_payout_routes(app, store_provider, wallet_provider):
    """Register the leaderboard season-payout endpoints on the FastAPI app.

    ``store_provider()`` and ``wallet_provider()`` are callables rather than
    captured objects, matching ``register_tribulation_routes`` and
    ``register_daily_streak_routes``: tests that swap the store get a fresh
    reference per call.

    Importing this module has no side effects, and nothing in it imports this
    module -- this function exists so main.py needs a single registration line.
    """
    from fastapi import HTTPException
    from wallet_dispatch import earn

    # Imported here, not at module scope: leaderboard_settlement imports
    # record_standing from THIS module, so a top-level import would be a cycle.
    from leaderboard_settlement import season_is_settling

    def _settling(players_db, season: str) -> bool:
        """Read-only: is this season's rank table being written right now?

        Returns False rather than raising when the settlements table is absent,
        so a database that has not run migration 009 serves claims exactly as
        it did before.
        """
        return season_is_settling(season, players_db)

    @app.get("/leaderboard/{player_id}/payout-status")
    async def leaderboard_payout_status(
        player_id: str,
        identity: PlayerIdentity = Depends(require_player),
    ):
        players_db = store_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")
        # Belt and braces, matching the claim route below. require_player has
        # already refused a mismatch -- measured, not assumed: this route
        # answered 403 cross-player before this line existed -- so this changes
        # no reachable outcome. It is here so the module keeps the promise its
        # own docstring makes ("Both routes ... re-check that the session's
        # player matches {player_id}") rather than leaving one route's ownership
        # resting entirely on a mount in main.py that can be reordered.
        if str(identity.player_id) != str(player_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "status path player_id does not match the authenticated session"
                ),
            )
        # ONE clock read for the whole response. The season LABEL below and
        # the rank lookup must describe the same instant: reading the clock
        # twice let a month rollover land between them, and the route then
        # answered with a rank resolved from the PREVIOUS season's standings
        # under the NEW season's label (and an `already_paid` for a season
        # the rank was never looked up in). Freezing the TIMESTAMP and
        # deriving the label from it keeps the two in step. `now` is a
        # TIMESTAMP, so it must NOT be the label -- forwarding season_now
        # made preview() call season_id("2026-09") and 500 on every read.
        status_now = _now()
        season_now = season_id(status_now)
        preview_body = preview(
            players_db[player_id],
            rank=server_rank(players_db[player_id], season_now),
            now=status_now,
            settling=_settling(players_db, season_now),
        )
        preview_body["identity_player_id"] = identity.player_id
        return {"ok": True, **preview_body}

    @app.post("/leaderboard/{player_id}/claim-payout")
    async def claim_leaderboard_payout(
        player_id: str,
        req: LeaderboardPayoutRequest,
        identity: PlayerIdentity = Depends(require_player),
    ):
        players_db = store_provider()
        wallet_store = wallet_provider()
        if player_id not in players_db:
            raise HTTPException(status_code=404, detail="Player not found")

        # Belt and braces on top of the dependency. The dependency already
        # raises 403 when the path id is not the session's player, so reaching
        # this line with a mismatch is a bug in the guard rather than a
        # reachable state. The explicit refusal keeps that bug from becoming a
        # cross-player mint, and it names the two ids so a log tells you which
        # side drifted.
        if str(identity.player_id) != str(player_id):
            raise HTTPException(
                status_code=403,
                detail="claim path player_id does not match the authenticated session",
            )

        now = _now()

        # THE GUARDED REGION. The already-paid check, the flag write and the
        # wallet write all happen inside one store transaction. The store takes
        # a database-level advisory lock and re-reads the player fresh under
        # READ COMMITTED, so only the first press sees an unpaid season and the
        # rest are told they already claimed.
        #
        # `player` is re-read INSIDE the transaction on purpose: the copy
        # handed back by the store may be a snapshot, not a live reference.
        with players_db.transaction(player_id):
            player = players_db[player_id]
            # Neither `req.rank` nor `req.base_amount` is forwarded. The band
            # comes from the rank the server recorded at settlement
            # (server_rank -> durable player state) and the amount from the
            # server's own reward table. Both caller values are echoed back
            # purely so a client can prove it was overridden; neither is paid.
            result = claim(
                player,
                now=now,
                settling=_settling(players_db, season_id(now)),
            )
            result["requested_base_amount"] = req.base_amount
            result["requested_rank"] = req.rank

            if result["reason"] == "paid":
                # Pay BEFORE persisting the flag. If `earn` fails, the
                # exception unwinds the transaction and the flag write is rolled
                # back with it, so a failed payment can never leave a
                # "already paid" record that pays nothing forever.
                receipt = earn(
                    wallet_store,
                    player_id,
                    result["currency"],
                    int(result["reward"]),
                    payout_key(player_id, result["season"]),
                    reward_type="leaderboard_payout",
                )
                if not receipt.get("ok"):
                    raise HTTPException(
                        status_code=502, detail=receipt.get("error", "payout failed")
                    )
                result["wallet"] = receipt
                players_db.persist(player_id, player)

        return result
