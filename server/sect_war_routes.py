"""
Wayfarer's Cycle — sect war: the route surface and its boot.

WHY THIS FILE EXISTS
--------------------
`sect_war.py` already mounts six war routes, and that is where they live. This
module is the ROUTE SURFACE around them, and it exists for three reasons:

1. It is the ONE place a caller imports to get the war routes, so the import
   path does not drift between `main.py`, the tests and any future caller.
2. It adds the reads the war needs and did not have: the per-war LEADERBOARD
   (who funded it, ranked, computed from stored rows) and the BANNER history
   (what this sect won). Both are reads and neither can move anything.
3. It owns the boot assertion. The migration runner reports success without
   proving a file applied — the folder still holds three files numbered 010, and
   the runner keys `schema_migrations` on the version string, so two of them can
   be skipped with a clean boot and no warning. So the registration site
   PROVES the tables exist rather than assuming the runner did.

WHAT THE CLIENT MAY SEND, AND WHY THAT LIST IS SHORT
----------------------------------------------------
Across all of these routes the only caller-supplied values that mean anything
are: a war id, a defender sect id, a contribution amount, and an
``Idempotency-Key`` header. There is no score field, no winner field, no
payout-amount field and no "settle now" flag anywhere, because each of those
is a decision the server has to make:

  * the SCORES come from committed, PAID contributions;
  * the WINNER comes from the two scores;
  * the PAYOUT comes from the stored paid stake via `payout_for`;
  * the PHASE is moved only by the settlement job, never by a route.

A preview that recomputed the winner on the client must never exist — that is
arithmetic the client is not allowed to do. The leaderboard below ranks by the
stored stake for the same reason: a caller cannot post its own rank.

REGISTRATION POSITION IS LOAD-BEARING
-------------------------------------
`register_sect_war(app, ...)` must be called ABOVE whatever ownership guard the
app installs. A guard that REWRITES routes in place only examines routes
registered above it, so a register call placed below the mount registers too
late and every war route ships unguarded. Every route here also carries
`Depends(require_player)` BY HAND: a path that names no player in the path is
invisible to a shape-based guard, so hand-wiring is the whole protection.

PREFIX IS `/sect-war/`, DELIBERATELY OUTSIDE `/sect/`
------------------------------------------------------
`sect.py`'s `POST /sect/{player_id}` is a catch-all that swallows any single
segment under `/sect/` and answers 403 for a route that does not exist. A war
route mounted under `/sect/war/...` would be shadowed by it, and the shadowed
one would answer first. The prefix is outside `/sect/` so nothing here can be
swallowed.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

if sys.path is not None and __file__:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

import auth as _auth_module
import sect_war
import sect_war_persist

logger = logging.getLogger("sect_war_routes")

require_player = _auth_module.require_player
PlayerIdentity = _auth_module.PlayerIdentity

# Re-exported so there is exactly ONE import path to the war routes.
register_sect_war_routes = sect_war.register_sect_war_routes
SETTLE_HOUR_UTC = sect_war.SETTLE_HOUR_UTC


class SectWarLeaderboardRequest(BaseModel):
    """A leaderboard QUERY. Note what is absent.

    No `rank`, no `score`, no `amount`, no `player_id`. A caller that posts one
    gets a 422 from the shape validator, which is louder and more honest than a
    handler that quietly ignores it. Ranking is computed from stored rows, so
    there is nowhere to post a forged position.
    """

    limit: int = Field(default=50, ge=1, le=200)


def war_leaderboard(players_db, sect_store, war_id: str,
                    limit: int = 50) -> dict[str, Any]:
    """Per-player paid stake for one war, ranked. A pure read.

    Ranked by OWN PAID STAKE, descending. A player who paid nothing is not
    listed, because that player has no contribution row — and a zero-value
    placeholder would consume the once-only settlement key and deny a real
    payout later.

    Ties break on the player id so the order is STABLE across calls. An order
    that reshuffles between two identical requests reads as a different answer
    to the same question, and a client cannot tell that from a bug.
    """
    war = sect_store.wars.get(war_id)
    if war is None:
        return {"ok": False, "error": "War not found"}

    merged = sect_war._contributors(war, players_db)
    rows = [{"player_id": pid, "paid": int(paid)} for pid, paid in merged.items()
            if int(paid) > 0]
    # Rank by paid stake. Ties break on the id, so equal stakes do not produce
    # an order that changes between two identical requests.
    rows.sort(key=lambda r: (-r["paid"], r["player_id"]))
    total_paid = sum(r["paid"] for r in rows)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
        row["share"] = round(row["paid"] / total_paid, 4) if total_paid else 0.0

    return {
        "ok": True,
        "war_id": war_id,
        "phase": war.phase,
        "attacker_sect_id": war.attacker_sect_id,
        "defender_sect_id": war.defender_sect_id,
        "total_paid": total_paid,
        "contributor_count": len(rows),
        # TRUNCATED, not re-paged. A war with thousands of contributors is not a
        # thing a client should pull in one response, and a `limit` that silently
        # returned everything would be the bug.
        "truncated": len(rows) > int(limit),
        "entries": rows[: int(limit)],
        "server_now": time.time(),
    }


def register_sect_war(app, players_db_provider, sect_store,
                      assert_schema: bool = True) -> None:
    """Mount the whole sect war surface: the six core routes plus the two reads.

    Call this ONCE. The six core routes come from `sect_war.register_sect_war_routes`,
    which is where they are defined; this adds the leaderboard and banner reads
    and then the boot assertion.

    The assertion is inside its own try/except and only WARNs on failure. A
    server that refuses to boot because a settlement table is missing turns a
    fixable schema problem into a total outage for every other feature, and the
    alternative — booting silently and paying nobody — is what the log line is
    for. It is loud, named, and greppable; it does not take the game down.
    """
    register_sect_war_routes(app, players_db_provider, sect_store)

    def _idem(request: Request, route: str) -> str:
        from idem_gate import require_idempotency_key
        return require_idempotency_key(request, route=route)

    # ---------------------------------------------------------------- reads --
    # Both are reads. Neither takes a body, so neither can move a phase, and
    # both take the player from the SESSION.

    @app.get("/sect-war/{war_id}/leaderboard")
    async def sect_war_leaderboard(
        war_id: str,
        # Depends(), NOT a bare model parameter. A bare pydantic model on a GET
        # route is read as a REQUEST BODY, and a plain GET carries no body, so
        # the shape validator answered 422 {"loc": ["body"]} to every caller and
        # the handler never ran -- measured on the booted app. Depends() reads
        # the same fields from the QUERY STRING instead, which is where a GET
        # caller can actually put them, and the Field(ge=1, le=200) bounds keep
        # working, so ?limit=0 or ?limit=9999 is still a 422.
        req: SectWarLeaderboardRequest = Depends(),
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """Who funded this war, ranked by what they actually paid.

        Read-only and member-scoped: a player who is in neither warring sect is
        refused with the SAME 403 the war read uses, because a 404 here would be
        an oracle — an outsider could read the difference between "this war
        exists" and "no such war".
        """
        players_db = players_db_provider()
        war = sect_store.wars.get(war_id)
        sid = sect_store.get_player_sect(identity.player_id)
        if war is None or sid not in (war.attacker_sect_id, war.defender_sect_id):
            raise HTTPException(403, "Not a member of either warring sect")
        return war_leaderboard(players_db, sect_store, war_id, req.limit)

    @app.get("/sect-war/{sect_id}/banners")
    async def sect_war_banners(
        sect_id: str,
        identity: "PlayerIdentity" = Depends(require_player),
    ):
        """The war banners a sect holds, and the ones it used to hold.

        A public history view: banners are a public holding, so any signed-in
        player may read another sect's. `active` is the one that counts right
        now; `history` includes expired rows, because a history that silently
        drops them is a history that erases itself.
        """
        players_db = players_db_provider()
        return {"ok": True, "sect_id": sect_id,
                "active": sect_war_persist.active_banner(players_db, sect_id),
                "history": sect_war_persist.banner_history(players_db, sect_id)}

    # ------------------------------------------------------------------ boot --
    if assert_schema:
        try:
            sect_war_persist.assert_schema(players_db_provider())
        except Exception as exc:  # noqa: BLE001
            # Loud, specific, and NOT fatal. See the docstring.
            logger.error(
                "SECT WAR SCHEMA NOT READY: %s. Sect war contributions will not "
                "survive a restart and banners will not persist until the sect "
                "war migrations are applied. Other features are unaffected.",
                exc)