"""
Wayfarer's Cycle — FastAPI Server (v0.2)
Integrates wallet_defense.py (§3.1–3.3) + request_signing.py (§3.4)
"""
from fastapi import FastAPI, HTTPException, Header, Request, Depends

from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager, contextmanager
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, List, Dict
import json
import os
import hashlib
import math
from copy import deepcopy
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from return_receipt import ReturnReceipt
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wallet_dispatch import spend, earn, claim_offline, MAX_OFFLINE_SECONDS, InMemoryWalletStore, PGWalletStore, USE_PG
# The shared Idempotency-Key gate for the state-changing money routes
# (/gacha/pull, /shop/buy, /sect/contribute). A missing key is refused, never
# synthesized server-side. See idem_gate.py for why both fallback shapes were
# revenue defects.
from idem_gate import require_idempotency_key

# Session layer. require_player authenticates the bearer token and returns a
# PlayerIdentity; enforce_ownership refuses a claim that is not that identity's
# own player. main.py imported neither until the /wallet/spend mount below --
# the guard existed and was unreachable.
#
# Imported HERE, immediately after the sys.path.insert above, not at the top of
# the file with the other third-party imports. `auth` is a bare sibling module
# (server/auth.py), so it only resolves once this file's own directory is on
# sys.path. Placing it with the fastapi/pydantic block made `import server.main`
# fail with ModuleNotFoundError: No module named 'auth'.
import auth as auth_module
from auth import (
    PlayerIdentity,
    enforce_ownership,
    issue_session,
    require_player,
    require_player_or_internal_mint,
)
# Principal and require_admin live in identity.py, NOT auth.py. Importing them
# from auth raised ImportError on every `import server.main`, which is the
# failure the whole suite reports as. auth.py exports the session-side names
# above; identity.py owns the operator/admin principal.
from identity import Principal, require_admin
import wc_logging as wcl
import request_logging
from request_signing import sign_response, verify_response
from event_scheduler import EventScheduler
from player_store import PlayerStore, SeatCapReached
import lifecycle
import sign_in
import time
import daily_boss
import battle_pass
import battle_pass_routes
import tribulation
import monthly_event
import sect
from first_session import register_first_session_routes
import lottery

# PG backend selection lives in wallet_dispatch (USE_PG, PGWalletStore)
# and player_store (PlayerStore) — see wallet_dispatch.py.

# Session key store for in-memory mode (PG mode uses players table)
SESSION_KEYS: Dict[str, bytes] = {}

# Load game data
DATA_DIR = Path(__file__).parent.parent / "shared"

with open(DATA_DIR / "realms.json") as f:
    REALMS = json.load(f)

with open(DATA_DIR / "spirit_roots.json") as f:
    SPIRIT_ROOTS = json.load(f)

with open(DATA_DIR / "sects.json") as f:
    SECTS = json.load(f)

with open(DATA_DIR / "npcs" / "npcs.json") as f:
    NPCS = json.load(f)


# ---------------------------------------------------------------------------
# Auto-battle authority (POST /combat/auto-battle)
# ---------------------------------------------------------------------------
#
# The route used to read `enemy_stats`, `max_turns`, `seed` AND `rewards`
# straight off the request body and credit the caller's number verbatim on a
# win. Unauthenticated, unlimited, and the amount could be NEGATIVE -- so the
# same defect also minted a negative wallet balance. A caller who supplied a
# 1-HP opponent and seed=1 owned the outcome too, so the "win" was theirs to
# grant.
#
# Four separate things have to be server-owned, because each is its own lever:
#
#   1. WHO may call it        -> Depends(require_player) + ownership (below).
#   2. WHAT the battle is     -> the opponent, the turn limit and the seed are
#                                all derived here. A client that can pick a
#                                1-HP enemy, or re-roll a seed until the
#                                simulation reports a win, has chosen its own
#                                outcome.
#   3. HOW MUCH a win pays    -> AUTO_BATTLE_REWARDS, this table, full stop.
#   4. HOW OFTEN it can pay   -> AUTO_BATTLE_DAILY_STONE_CAP, charged per
#                                player per UTC day. Authority over the AMOUNT
#                                is not a rate limit: a fixed 100/win is still
#                                an infinite mint if the route can be called
#                                without limit.
#
# The tests assert the CONTRACT (a win pays this table; a day pays at most the
# cap; the client cannot reach the simulator), not these numbers, so the
# economy can be retuned here without editing the tests.
#
# `random` and MISSION_DEFINITIONS are imported here rather than at the top of
# the file: the first is only needed by the server-side opponent roll, and the
# second is needed to DERIVE the cap from the hunt track. Both are already
# reachable at this point in the module (the sys.path.insert above runs first,
# and main.py imports `missions` again further down for the /missions routes).
import random as _ab_random

from missions import MISSION_DEFINITIONS as _AB_MISSION_DEFINITIONS

#: What a WIN pays. The client cannot influence this in any way -- the request
#: body's `rewards` field is not read by the payout path (it is still parsed
#: for wire compatibility, see CombatAutoBattleRequest).
AUTO_BATTLE_REWARDS: Dict[str, int] = {"spirit_stone": 100}

#: Upper bound accepted for a single wire `rewards` value. This is a MALFORMED
#: INPUT bound, not an authority: it rejects amounts the route would never pay
#: (and negative ones, which would mint a negative balance) with a 422 at the
#: edge rather than letting them reach the wallet. It is deliberately set ABOVE
#: anything the server pays, so a legal-but-oversized body is still refused
#: while the shipped client's 100-stone body parses. Kept far below the
#: nine-figure attack amount.
AUTO_BATTLE_REWARD_CEILING: int = 1_000_000

#: Total spirit stones one player may earn from auto-battle in one UTC day.
#:
#: DERIVED, not invented: it is the full daily spirit-stone budget the hunt
#: track already pays (hunt_daily_1001/1002/1003 in server/missions.py -- 100
#: + 200 + 500), so a day of auto-battling cannot out-earn a day of the hunting
#: those missions reward. At 100/win that is eight wins a day. Read live from
#: MISSION_DEFINITIONS rather than hardcoded, so a retune of the hunt track
#: moves this with it instead of silently desynchronising the two budgets.
AUTO_BATTLE_DAILY_STONE_CAP: int = sum(
    int(m.get("reward_spirit_stone", 0) or 0)
    for m in _AB_MISSION_DEFINITIONS
    if m.get("type") == "daily" and m.get("category") == "hunt"
) or 1_000

#: Per-day ledger key on the player row. Server-written; a request that posts
#: this key cannot rewind it (there is no code path that reads it from a body).
AUTO_BATTLE_LEDGER_KEY = "auto_battle_daily"


def _auto_battle_stone_grant(player: dict, amount: int, now: float | None = None) -> int:
    """Charge `amount` against this player's daily auto-battle budget.

    Returns how many stones may actually be paid -- `amount` in full while
    there is budget, the REMAINDER on the last win of the day, and 0 once the
    day is spent. The ledger is charged by what is granted, never by the
    nominal win value, so a day cannot overshoot the cap by a partial payout.

    `player` is mutated in place (it is the live row in `players_db`), which is
    why the caller does not have to write the ledger back itself.

    The day index is UTC days since the epoch, matching
    `time.time() // 86400` in server/missions.py, so the budget resets on the
    same boundary the rest of the daily systems use.
    """
    if amount is None or int(amount) <= 0:
        # The value is computed here, so the invariant belongs here: a zero or
        # negative amount would credit a negative balance. Same reasoning as
        # the cost assertion in server/monthly_event.py.
        raise HTTPException(status_code=500, detail="auto-battle grant must be positive")

    day_now = int((time.time() if now is None else now) // 86400)
    ledger = player.get(AUTO_BATTLE_LEDGER_KEY)
    if not isinstance(ledger, dict) or int(ledger.get("day", -1)) != day_now:
        # First battle of a new day: the budget is full again.
        ledger = {"day": day_now, "stones": 0}
        player[AUTO_BATTLE_LEDGER_KEY] = ledger

    spent = int(ledger.get("stones", 0) or 0)
    remaining = AUTO_BATTLE_DAILY_STONE_CAP - spent
    if remaining <= 0:
        # An exhausted day is a normal outcome, not an error: the battle still
        # ran and the caller still gets its result. Making the cap a 4xx would
        # turn a throttle into a crash loop for a client polling on a timer.
        return 0

    granted = min(int(amount), remaining)
    ledger["stones"] = spent + granted
    return granted


def _auto_battle_enemy_stats(player: dict, rng: "_ab_random.Random") -> dict:
    """Derive the opponent from the player's own realm. Never from the request.

    The opponent is the attack surface this guard closes: a client that names
    the enemy decides the outcome. So it is generated here, scaled off the
    player's realm/stage so the fight stays meaningful as they grow, with a
    bounded random spread so no two fights are identical.

    `rng` is the SERVER's generator. It is never seeded from the request, so a
    caller cannot re-roll a losing fight into a win.
    """
    realm = str(player.get("cultivation_realm", "mortal"))
    stage = str(player.get("cultivation_stage", "early"))

    def scaled(stat_key: str, default: int) -> int:
        # `int(x or default)` absorbed FALSY shapes only. A truthy non-numeric
        # stored value -- "abc", [1], float("inf") -- survived the `or` and
        # raised out of the handler, so FastAPI answered 500 for a player who
        # did nothing wrong. Measured: a stored row with atk="abc" answered
        # POST /combat/auto-battle with
        # `ValueError: invalid literal for int() with base 10: 'abc'`.
        #
        # THE FALSY BRANCH IS DELIBERATELY UNCHANGED, and that is the part a
        # naive repair gets wrong. `or` maps a stored 0 to the DEFAULT, so a
        # zero has always produced a baseline-strength opponent. Routing 0
        # through the coercion instead would scale the opponent from 0, floor
        # it to 1 attack, and hand that player a trivially winnable fight --
        # turning a crash fix into a currency mint on /combat/auto-battle,
        # which is the main daily income route. So the `or` stays, and only
        # the truthy branch gains a guarded conversion.
        #
        # coerce_stat_int is combat_config's own single number-coercion rule
        # and falls back to the caller's own default, so nothing is invented
        # here: a corrupt column yields a real (if baseline) opponent rather
        # than a crash. The simulator below was already sound for every shape
        # -- the fault was entirely here, above it.
        from combat_config import coerce_stat_int
        value = player.get(stat_key, default)
        return int(value or default) if not value else coerce_stat_int(value, default)

    base_hp = scaled("hp", 1000)
    base_atk = scaled("atk", 100)
    base_def = scaled("def_", 50)

    # A fresh mortal sees a 500-HP opponent; higher realms scale with the
    # player's own pool, so the fight tracks progression instead of becoming
    # free or impossible.
    hp = int(base_hp * 0.5 * rng.uniform(0.9, 1.1))
    return {
        "hp": max(1, hp),
        "atk": max(1, int(base_atk * rng.uniform(0.9, 1.1))),
        "def": max(0, int(base_def * rng.uniform(0.9, 1.1))),
        "spd": max(1, scaled("spd", 100)),
        "realm": realm,
        "stage": stage,
        # A fixed element rather than a client-chosen one: element is a damage
        # multiplier, so letting the caller pick it is picking the outcome.
        "element": "earth",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan: replaces the deprecated startup/shutdown event hooks.

    Same behavior: build the event scheduler, register its routes, then on
    shutdown stop the scheduler. Route registration happens after the app
    object exists, so the body runs at startup, not import time.
    """
    global event_scheduler
    # Apply pending SQL migrations before anything reads the schema. This must
    # run before event_scheduler.load_events() below, whose bare `except
    # Exception: pass` would otherwise hide a missing table as "no events yet".
    # A no-op when no database is configured, so local play needs none.
    from migrations_runner import apply_migrations
    apply_migrations()
    from event_rewards import EventRewardDistributor
    from event_routes import register_event_routes
    distributor = EventRewardDistributor(wallet_store, players_db)
    event_scheduler = EventScheduler(reward_distributor=distributor, players_db=players_db)
    if USE_PG:
        try:
            event_scheduler.load_events()
        except Exception:
            pass  # Events table may not exist yet
    event_scheduler.start()
    # Register event routes after scheduler is initialized
    register_event_routes(app, players_db, wallet_store, event_scheduler)
    # OWNERSHIP GUARD -- installed HERE, at the end of lifespan, not at the
    # bottom of this file. The guard REWRITES routes in place, so it can only
    # see the routes that already exist when it runs. register_event_routes()
    # above adds /events/list, /events/convert and the two /admin/events/*
    # routes, and they did not exist when the module was imported, so an
    # import-time install never evaluated them: POST /events/list takes a
    # player_id in its REQUEST BODY, matched the guard's shape, and shipped
    # with no auth dependency and no ledger entry at all. Running the
    # installer last covers every route, present and future.
    #
    # `_GUARDED_ROUTES` is updated IN PLACE rather than rebound, so the
    # module-level object is the same dict before and after startup and a
    # caller that captured a reference keeps seeing the full set.
    _GUARDED_ROUTES.update(install_ownership_guard(app))
    # Tournament bracket clock: the same daemon-thread shape as the event
    # scheduler above, on the same 60s cadence. It opens rounds, closes them,
    # records forfeits and settles — a bracket that only advanced when somebody
    # logged in would let one absent player hold up the whole field. Wrapped in
    # its own try/except so a bracket problem can never stop the app booting.
    global tournament_job
    try:
        from tournament import TournamentJob, tournament_store
        tournament_job = TournamentJob(tournament_store(), tick_interval=60)
        tournament_job.start()
    except Exception:
        tournament_job = None
    # Sect war settlement clock: the same daemon-thread shape as the
    # tournament clock above, and for the same reason -- the design's
    # rule is that the JOB is the only writer of a war's phase, so a war
    # cannot wait for a player to log in before it settles.
    # `players_db` is read through a closure on purpose: it is a module
    # global that tests and the store-swap path rebind, so the job must
    # follow the live store rather than capture one handle at import.
    # Its own try/except so a settlement problem can never stop the app
    # booting, and its own stop in the same finally block for the same
    # reason -- one background job must not be able to take down another.
    global sect_war_job
    try:
        from sect_war_job import SectWarJob
        sect_war_job = SectWarJob(lambda: players_db, sect_store)
        sect_war_job.start()
    except Exception:
        sect_war_job = None
        wcl.get_logger().exception("sect war job failed to start")
    try:
        yield
    finally:
        if sect_war_job:
            sect_war_job.stop()
        if tournament_job:
            tournament_job.stop()
        if event_scheduler:
            event_scheduler.stop()


app = FastAPI(title="Wayfarer's Cycle API", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# CORS (cross-origin resource sharing)
# ---------------------------------------------------------------------------
# What CORS is, in one line: it is the rule that lets a WEB PAGE ask this API
# for data and read the answer. A browser enforces it. Nothing else does.
#
# The bug this replaces
# ---------------------
#   app.add_middleware(CORSMiddleware, allow_origins=["*"],
#                      allow_credentials=True, allow_methods=["*"],
#                      allow_headers=["*"])
#
# Starlette echoes the caller's own Origin header straight back when BOTH
# "allow every origin" and "allow credentials" are set -- CORSMiddleware.send
# calls allow_explicit_origin() on that exact condition. That pair is the
# worst combination available: the browser sees its own origin approved AND
# approved for sending credentials, so any page a player visits could have
# the browser make signed-in calls here and read the replies. Measured on this
# tree before the fix: a GET / carrying `Origin: https://evil.example.com`
# came back with `access-control-allow-origin: https://evil.example.com` and
# `access-control-allow-credentials: true`, and the preflight OPTIONS came
# back 200 with the same pair.
#
# Why the allowlist is EMPTY by default
# ------------------------------------
# The shipped client is a NATIVE Godot Android app. It is not a web page and
# not a WebView (a phone screen that just shows a web page) wrapper:
#   * client/export_presets.cfg declares only the "Android" and "Android
#     Release" presets -- there is no web/HTML5 build to serve at all.
#   * build/wayfarers-cycle-release-final.apk ships
#     lib/arm64-v8a/libgodot_android.so, and contains no assets/www folder
#     and no index.html, which is what a WebView wrapper would carry.
#   * client/scripts/network/APIClient.gd drives HTTPRequest, Godot's native
#     HTTP client. It does not implement the same-origin policy, so it never
#     sends an Origin header and never needs one answered.
#
# So there is no browser origin to name, and a non-browser client cannot be
# granted anything by this rule either way. The default answer is therefore
# the empty list: the middleware refuses every origin. That is exactly what
# the real client experiences either way, and the safe answer for everything
# else.
#
# allow_credentials is False, not True
# ------------------------------------
# This server sets and reads no cookies anywhere (no set_cookie and no
# request.cookies anywhere under server/) and authenticates purely from the
# `Authorization: Bearer` header (auth.extract_bearer_token, auth.py:693). A
# bearer token is a header the client writes by hand, not a credential the
# browser attaches on its own, so it travels whether or not this flag is on.
# Turning it on would therefore buy nothing, and would quietly re-arm the
# "approved origin plus credentials" pairing for anyone who later mis-edits
# the allowlist. If a cookie-authenticated browser client is ever added, that
# is the change to make, deliberately, in this file.


def _cors_allowlist() -> List[str]:
    """Return the origins allowed to read this API from a browser.

    Reads the ``WC_CORS_ALLOW_ORIGINS`` environment variable as a
    comma-separated list, e.g.::

        WC_CORS_ALLOW_ORIGINS=https://play.wayfarerscycle.com,https://ops.internal

    Empty when the variable is unset or blank, which is the shipping default
    and means "refuse every origin" -- see the note above.

    Entries that could re-open the hole are dropped rather than honoured, so
    a careless deploy cannot undo the fix:

    * ``*`` or anything containing ``*``. Starlette's own "allow everything"
      test is literally ``"*" in allow_origins``, and that flag alone drives
      the origin echo, so a wildcard here is the vulnerability, not a
      shorthand for it.
    * ``null``. That is the literal origin string a sandboxed frame or a
      file:// page sends, so allowing it is the same hole wearing a costume.
    * Anything that is not ``scheme://host[:port]``. A browser sends an
      Origin in exactly that shape, so a malformed entry could never match a
      real request and would only hide a typo in the deploy config behind a
      server that looks correctly configured.
    """
    raw = os.environ.get("WC_CORS_ALLOW_ORIGINS", "")
    allowed: List[str] = []
    for entry in raw.split(","):
        origin = entry.strip()
        if not origin:
            continue
        if "*" in origin or origin == "null":
            wcl.safe_log_event("cors.allowlist_entry_rejected",
                               reason="wildcard_or_null", origin=origin)
            continue
        if "://" not in origin or origin.endswith("/"):
            wcl.safe_log_event("cors.allowlist_entry_rejected",
                               reason="not_an_origin", origin=origin)
            continue
        if origin in allowed:
            continue
        allowed.append(origin)
    return allowed


def _install_cors(target_app: FastAPI) -> FastAPI:
    """Attach the CORS middleware to ``target_app`` and return it.

    Split out from the module body so a test can build a throwaway app that
    runs this exact wiring with a configured allowlist, instead of restating
    the arguments and drifting from them.
    """
    target_app.add_middleware(
        CORSMiddleware,
        # Never a wildcard. An origin absent from this list gets no
        # access-control-allow-origin header at all, which is what makes the
        # browser refuse the read.
        allow_origins=_cors_allowlist(),
        # No cookies are issued or read, so there is nothing here to allow.
        # See the note above before changing it to True.
        allow_credentials=False,
        # These two are not the security boundary -- an approved origin may
        # use any method or header. They stay wide so a future browser client
        # is not limited by a guess about what it will send.
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return target_app


_install_cors(app)

# REQUEST-ID CORRELATION
# ----------------------
# One id per request, stamped on every log line written while it runs, so a
# spend and the access line above it can be joined into one story. Before
# this call, every line the server ever wrote carried `request_id: null`.
#
# request_logging.py deliberately does not install itself -- the choice to
# instrument the app must stay visible in one place. This is that place.
#
# Call site rules, and they are load-bearing:
#   * IMPORT TIME, immediately after the FastAPI() instance exists and BEFORE
#     the app is ever served. A lifespan/startup handler is the WRONG place:
#     by the time a lifespan body runs, Starlette has already built and
#     frozen the middleware stack, so the add would be silently ignored at
#     best. install_request_logging() raises RuntimeError on an already-started
#     app precisely to turn that mistake into a loud failure.
#   * AFTER _install_cors(app) above, which makes this the outermost layer:
#     Starlette runs the last-added middleware first, so this way the id is
#     minted before CORS can short-circuit, and a rejected preflight is still
#     traceable.
#   * Idempotent, so a second call would add no second copy.
request_logging.install_request_logging(app)

# Imported here rather than in the header block so the analytics intake bounds
# below sit next to the only thing they bound.
from collections import deque
import itertools

# --- Analytics intake bounds -------------------------------------------------
# Three limits, because they defend against three different things and only
# one of them is about count.
#
# MAX_ANALYTICS_BATCH caps one request, at the request model, so an oversized
# batch is a 422 and the handler never runs. The shipped Godot client flushes
# on a 10-event batch (client/scripts/autoload/Analytics.gd, _batch_size), so
# this leaves an order of magnitude of headroom for a real client.
#
# MAX_ANALYTICS_BUFFER caps what is RETAINED, and it is the ceiling that
# matters: nothing in this file ever drains analytics_buffer, so before it was
# a plain list, one anonymous POST of 500 oversized events grew process memory
# without bound and a caller could repeat that forever.
#
# MAX_ANALYTICS_EVENT_NAME / MAX_ANALYTICS_PARAMS_BYTES cap a single event, so
# a batch that is within the count limit still cannot carry megabytes per row.
MAX_ANALYTICS_BATCH = 100
MAX_ANALYTICS_BUFFER = 1000
MAX_ANALYTICS_EVENT_NAME = 64
MAX_ANALYTICS_PARAMS_BYTES = 2048

# In-memory storage (PG mode: PlayerStore is a PG-backed write-through dict)
players_db: dict = {}
# maxlen makes the bound structural: appending past the ceiling evicts the
# oldest event, so the buffer cannot grow no matter what a caller sends.
analytics_buffer: "deque[dict]" = deque(maxlen=MAX_ANALYTICS_BUFFER)
sessions_db: dict = {}
purchase_receipts_db: dict = {}

# Initialize wallet store (PostgreSQL if WC_PG_HOST set, else in-memory)
if USE_PG:
    wallet_store = PGWalletStore()
else:
    wallet_store = InMemoryWalletStore()

# Every synchronous route shares the transaction boundary (memory or PostgreSQL).
players_db = PlayerStore(wallet_store)

def _players_db_accessor():
    """Dynamic accessor — tests monkeypatch main.players_db and this picks it up."""
    return players_db


# ---------------------------------------------------------------------------
# Sign-in: POST /auth/login
# ---------------------------------------------------------------------------
# The shipped Godot client already calls this route and the server did not have
# it, so the game could not authenticate at all. The credential it trades is the
# player's 32-byte signing key, minted at account creation (see create_player,
# which returns `session_key.hex()`), which the client stores and re-presents on
# every boot. login_routes.py owns the route, the constant-time key check and
# the token mint; this block only wires it to this app.
#
# ORDERING -- read before moving this block.
# This registration is deliberately placed here, above every other feature
# router, and it must stay ABOVE any `install_ownership_guard` call. That call
# is NOT made anywhere in this file today (measured by AST parse: 0 call sites),
# and this task does not switch it on. When the guard does land it rewrites
# routes in place, so a login route registered below it would already be
# rewritten by the time the guard ran. Registering first means the guard sees
# the finished route and can make its own decision about it.
#
# WHY THE GUARD NEEDS NO EXEMPTION ENTRY FOR THIS ROUTE
# ownership_guard matches a route that carries a `player_id` claim and has no
# recognisable auth dependency; this route's body model does carry `player_id`,
# so the shape matches. It is not excused by name -- it is self-authenticating.
# login_routes.build_login_router attaches the signing-key check as a `Depends`
# whose `__module__` is reported as "auth", which is what
# ownership_guard.has_auth_dependency recognises. So the guard leaves this route
# alone for the right reason: the route proves a credential itself, rather than
# being waved through. That is why ownership_guard.EXEMPT_PATHS is left
# untouched -- adding this path there would grant an exemption the route does
# not need, and EXEMPT_PATHS is a fail-closed list whose exact contents
# test_player_ownership_guard.py asserts.
def _resolve_signing_key_for_login(player_id: str):
    """Stored signing key for `player_id`, read the same way the rest of the file does.

    `SESSION_KEYS` is the in-process cache written by create_player;
    `players_db.signing_key` is the durable store behind it and is what
    survives a restart. Both are consulted through module globals on every
    call, so a test that replaces main.players_db is picked up here too.

    Raises KeyError for an unknown player -- login_routes collapses that (and
    any other error) to a single 401, so this is free to be simple.
    """
    if not isinstance(player_id, str) or not player_id:
        raise KeyError(player_id)
    cached = SESSION_KEYS.get(player_id)
    if isinstance(cached, bytes) and len(cached) == 32:
        return cached
    return players_db.signing_key(player_id)


from login_routes import build_login_router
app.include_router(build_login_router(_resolve_signing_key_for_login))


from lifecycle_guard import check_status_inline
lifecycle._live_players_db = _players_db_accessor
lifecycle.register_routes(app, _players_db_accessor)
register_first_session_routes(app, players_db)

# The deleted-player hole. auth.py cannot import main (main imports auth,
# so the cycle would be circular), so the store that answers "does this
# session's player still exist?" is HANDED IN here, next to the rest of
# the auth wiring. Without this the guard only finds main.players_db
# through a sys.modules fallback, which reads as an accident rather than a
# wiring decision and leaves the hole open on any app the fallback skips.
#
# WHY A CALLABLE AND NOT THE STORE OBJECT. auth checks the attached value
# first and only falls back to sys.modules when nothing is attached, so
# handing in `players_db` itself would pin the object built at import time.
# Roughly 30 tests swap main.players_db for their own store; pinned to the
# import-time object they would be answered against a store they never
# seeded and every live player would read as deleted. Measured: the
# object form broke 30 tests across battle-pass, lifecycle, lottery-pity,
# purchase-idempotency and tribulation. A callable is resolved at CALL time,
# so the swap is picked up and the answer is unchanged.
#
# The callable shape is auth's documented one: it takes a player id and
# returns a bool. The membership test inside mirrors auth._player_exists
# (callable store, else `in`) so a store swapped in as a lambda answers
# exactly as it did before this line existed.
def _auth_player_exists(player_id: str) -> bool:
    """Whether `player_id` still names a live account, read at call time."""
    store = players_db
    if callable(store) and not hasattr(store, "__contains__"):
        return bool(store(player_id))
    return player_id in store


auth_module.attach_player_directory(app, _auth_player_exists)

# Battle Pass routes (R9) — includes XP source endpoints used by I5
# The THIRD argument binds the wallet store, and it is what makes the pass's
# currency path live: without it every award and every purchase debit falls back
# to writing player["spirit_stones"] directly, so the store that owns money
# (whitelist, per-key idempotency ledger, version guard) never sees the pass
# move a player's stones at all. Resolved per request via a lambda, like
# players_db above, so a test that swaps main.wallet_store sees the swap.
battle_pass_routes.register_battle_pass_routes(app, lambda: players_db,
                                                lambda: wallet_store)

# Tribulation routes (R5) — includes contribute endpoint that grants XP (I5)
tribulation.register_tribulation_routes(app, lambda: players_db, daily_boss)

# Daily Boss — the DURABLE three-table system (rotation / shared instance /
# claim ledger, migration 016_daily_boss.sql).
#
# REGISTRATION POSITION IS LOAD-BEARING, twice over:
#   1. These three paths are LITERAL (/daily-boss/info, /enter, /claim) and the
#      shipped pod routes below are /daily-boss/{player_id}. FastAPI matches in
#      registration order, so registering these AFTER the pod routes would let
#      `/daily-boss/{player_id}` swallow `/daily-boss/info` and answer
#      "Player not found". They are registered here, far above the pod routes
#      at the bottom of this file.
#   2. The shape-based ownership guard is mounted at the very bottom of this
#      file and only examines routes registered above it. This is above that
#      line, so the guard sees these routes. They also carry require_player by
#      hand, because a path that names no player is invisible to a shape guard.
from daily_boss_tables import register_daily_boss_routes
register_daily_boss_routes(app, lambda: players_db)

# Monthly event routes (R6) — includes quest completion that grants XP (I5)
monthly_event.register_monthly_event_routes(app, lambda: players_db)

# Lottery routes (R10) — per-rarity pools with pity persistence
lottery.register_lottery_routes(app, lambda: players_db,
    lambda: wallet_store, spend, earn)

# Sect routes (F1) — register before comm/chat so sect channels exist
# IMPORTANT: /sect/contribute is registered BEFORE sect routes to avoid
# /sect/{player_id} catching "contribute" as a player_id parameter
from sect import SectStore, register_sect_routes
sect_store = SectStore()

# Define SectContributeRequest model early so we can register /sect/contribute first
class SectContributeRequest(BaseModel):
    player_id: str
    amount: int
    client_version: int = 0

# /sect/contribute must be registered FIRST to avoid conflict with /sect/{player_id}
@app.post("/sect/contribute")
def post_sect_contribute(req: SectContributeRequest, request: Request):
    """Contribute to sect for rewards."""
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    player = players_db[req.player_id]

    # Server-authoritative spend through WalletStore (§3.1/§3.2)
    # Refuse a headerless contribution instead of deriving the key from the
    # body. A body-derived key made the second real contribution a "replay",
    # so the player was charged once for two intents. See idem_gate.py.
    idem = require_idempotency_key(request, route="sect-contribute")
    spend_result = spend(wallet_store, req.player_id, "spirit_stone", req.amount,
                         req.client_version, idem)
    if not spend_result.get("ok"):
        raise HTTPException(status_code=400, detail=spend_result)
    if spend_result.get("_replayed"):
        return {"ok": True, "_replayed": True,
                "contribution": player.get("sect_contribution", 0),
                "rank": player.get("sect_rank", "")}

    player["sect_contribution"] = player.get("sect_contribution", 0) + req.amount

    # Check for rank promotion
    if player["sect_contribution"] >= 1000 and player.get("sect_rank") == "outer_disciple":
        player["sect_rank"] = "inner_disciple"

    # Grant battle pass XP for sect contribution (I5)
    bp_xp = min(req.amount // 10, 100)  # 1 XP per 10 spirit stones contributed, capped at 100
    bp_result = battle_pass.grant_xp(player, bp_xp, "sect_contribution")
    result = {"ok": True, "contribution": player["sect_contribution"], "rank": player["sect_rank"]}
    if bp_xp > 0:
        result["battle_pass"] = bp_result
    players_db.persist(req.player_id, player)
    return result

# Now register sect routes (which include /sect/{player_id})
register_sect_routes(app, lambda: players_db, sect_store)

# F8: Tower routes (Spirit Realm Ascent)
from tower import register_tower_routes
register_tower_routes(app, _players_db_accessor, wallet_store)

# Sign-in routes (R2) — 7-day free sign-in streak
@app.get("/sign-in/{player_id}")
def sign_in_view(player_id: str, request: Request):
    """R2: Sign-in streak status (view only — does not attend)."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"sign_in": sign_in.view(players_db[player_id], time.time())}

@app.post("/sign-in/{player_id}/claim")
def sign_in_claim(player_id: str):
    """R2: Claim today's sign-in streak reward."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    # The row is read and written inside ONE transaction. Outside a
    # transaction `players_db[player_id]` hands back a detached snapshot, so
    # the lifecycle start and the sign-in credit below were applied to an
    # object the store then discarded: the streak never advanced.
    with players_db.transaction(player_id):
        player = players_db[player_id]
        if player.get("lifecycle") is None:
            # A brand-new player has no lifecycle yet: /player/create deliberately
            # leaves it unset. The first lifecycle screen-read starts the clock, but
            # a player who claims a sign-in reward BEFORE ever opening that screen
            # got a hard 400 and could not enter the game at all. Start the same
            # 30-day clock here. reconcile() writes player["lifecycle"] in place
            # (lifecycle.py:145) before returning its deep copy, so the result does
            # not need assigning - but the hasattr guard and the re-check below are
            # kept deliberately: if the store ever fails to start a lifecycle, the
            # request is still refused rather than paying out unguarded.
            if hasattr(lifecycle, "reconcile_player"):
                lifecycle.reconcile_player(player, time.time(), start=True)
        if player.get("lifecycle") is None:
            raise HTTPException(status_code=400, detail="lifecycle not started")
        result = sign_in.claim(player, time.time())
        result["ok"] = True
    # No persist() call: the enclosing transaction() owns the write-back
    # (player_store.py:131-137 upserts every staged row that changed).
    return result

# Communication routes (F2) — server-side chat, boards, DMs
from communication import CommunicationStore, BulletinStore, register_communication_routes
comm_store = CommunicationStore()
bulletin_store = BulletinStore()
register_communication_routes(app, lambda: players_db, comm_store, bulletin_store)

# F9: Friends / Social System
from friends import init_friends, register_friends_routes, friends_store
friends_store = init_friends()
register_friends_routes(app, lambda: players_db, friends_store)

# F10: World Map / Zone Navigation
from zone_routes import register_zone_routes
register_zone_routes(app, players_db)

# Sect wall: ranks, contribution and a shared message board
from sect_wall import register_sect_wall_routes
register_sect_wall_routes(app, lambda: players_db)

# PvP ghost battles: fight another player's frozen combat log
from ghost_battle import register_ghost_battle_routes, source_match_seed
register_ghost_battle_routes(app, lambda: players_db)

# Single-elimination tournament brackets.
#
# REGISTRATION POSITION IS LOAD-BEARING. install_ownership_guard(app) is mounted
# at the BOTTOM of this file and matches routes by shape, so anything registered
# below that line is never examined by it. This registration sits in the
# ordinary place, far above the mount, so the shape guard sees these routes. The
# routes carry require_player by hand as well (a bracket/match id names no
# player, so the shape guard would not notice them on its own) — two layers,
# because one layer is one bug away from an open door.
from tournament_routes import register_tournament_routes
register_tournament_routes(app, lambda: players_db, lambda: wallet_store)

# Death/Respawn loop: legacy points, transcendence and sacrifice (client DeathSystem/LegacySystem)
from legacy import register_legacy_routes
register_legacy_routes(app, lambda: players_db)

# Prestige routes (F12) — season-based prestige, legacy carryover
from prestige import register_prestige_routes
register_prestige_routes(app, lambda: players_db)

# Daily streak. These two routes were defined in daily_streak.py but never
# registered, so they were absent from the live route table while the
# shipped Godot client still ships a DailyStreak panel that calls them
# (client/scenes/game/MainHub.gd, 8 call sites). The reward was unreachable
# and the panel could not have worked. server/tests/test_auth_contracts.py
# asserts this route exists; that assertion is what caught it.
from daily_streak import register_daily_streak_routes
register_daily_streak_routes(app, lambda: players_db)

# F13: NPC Interaction System
from npc_routes import register_npc_routes
register_npc_routes(app, players_db)

# F15: Dungeon/Instance System
from dungeon_routes import register_dungeon_routes
register_dungeon_routes(app, players_db)

# Leaderboard reward TABLE (read-only display) plus the season payout pair.
# The payout module's register_leaderboard_payout_routes() existed but was
# never called, so GET /leaderboard/{player_id}/payout-status and
# POST /leaderboard/{player_id}/claim-payout did not exist on the live app.
# Registered beside the table routes it belongs with.
from leaderboard_rewards_routes import register_leaderboard_rewards_routes
register_leaderboard_rewards_routes(app)
from leaderboard_payout import register_leaderboard_payout_routes
register_leaderboard_payout_routes(app, lambda: players_db, lambda: wallet_store)

# The leaderboard SEASON SETTLEMENT job's one operator route,
# POST /admin/leaderboard/settle. Without the job, record_standing() is never
# called by production code and the payout above pays nobody. Registered here,
# far above install_ownership_guard at the bottom of this file, so the shape
# guard can see it; it also carries require_admin by hand because an
# /admin/* path names no player for the guard to match on.
from leaderboard_settlement import register_leaderboard_settlement_routes
register_leaderboard_settlement_routes(app, lambda: players_db)

# VIP membership: register_vip_routes() existed but was never called, so
# GET /vip/{player_id}/status and POST /vip/{player_id}/claim-daily did not
# exist. Same two-line pattern as register_daily_streak_routes above: the
# store is passed as a zero-arg provider so a test that swaps main.players_db
# gets a fresh reference per call.
from vip import register_vip_routes
register_vip_routes(app, lambda: players_db)

# Guild routes. guild.py ships register_guild_routes() with its five routes
# and no caller, so none of /guild/* existed on the live app. Registered here
# beside the other feature mounts, ABOVE install_ownership_guard at the bottom
# of this file, so the shape guard sees the new paths.
#
# FIXED 2026-10-04: the store provider handed these five handlers main.players_db
# -- a PlayerStore, which has no guild methods -- so the FIRST guild call on
# every request raised AttributeError and the route answered 500. Measured
# before the fix through the real app with a real minted session:
# POST /guild/create 500, POST /guild/join 500, POST /guild/leave 500,
# GET /guild/members 500, POST /guild/claim-reward 500.
# Traceback: guild.py:707 --
#   AttributeError: 'PlayerStore' object has no attribute 'create_guild'
#
# guild.py OWNS ITS OWN STATE. GuildStore.__init__(self, pg_pool=None)
# (guild.py:274) takes an OPTIONAL pool that its core operations never read --
# the store keeps guilds in process memory -- so it needs no player database
# and no database connection. pg_pool is not even defined this early in the
# file, and passing it would buy nothing.
#
# ONE instance, built once here. A provider that constructed a fresh store per
# call would hand every request an empty guild list, so a created guild would
# vanish before GET /guild/members ever read it.
#
# Both stores are passed as zero-arg providers (not captured handles) for the
# same reason as register_vip_routes above: tests and the store-swap path
# rebind main.players_db / main.guild_store / main.wallet_store, so the
# handlers must read the live object per request. The wallet provider is what
# lets the claim route credit a real reward instead of refusing 501.
from guild import register_guild_routes, GuildStore
guild_store = GuildStore()
register_guild_routes(app, lambda: guild_store, lambda: wallet_store)

# HallOfTranscended + SlotQueue screens: GET /hall/transcended and
# GET /slot/status, the two reads those screens call and the server never had.
from hall_slot_routes import register_hall_slot_routes
register_hall_slot_routes(app, lambda: players_db)

# F3: Missions endpoints (full build — PG-backed with wallet-integrated rewards)
from missions import init_mission_store, MissionProgressRequest, MissionClaimRequest, mission_def
from wallet_dispatch import spend, earn

# Initialize mission store with PG pool (if available)
if USE_PG:
    from psycopg2.pool import ThreadedConnectionPool
    pg_pool = ThreadedConnectionPool(1, 10, host=os.environ.get("WC_PG_HOST", "127.0.0.1"), port=int(os.environ.get("WC_PG_PORT", 5432)), dbname=os.environ.get("WC_PG_DB", "wayfarers"), user=os.environ.get("WC_PG_USER", "wayfarers"), password=os.environ.get("WC_PG_PASSWORD", ""))
    mission_store = init_mission_store(pg_pool)
else:
    mission_store = init_mission_store(None)

@app.get("/missions/list")
def missions_list(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"ok": True, "missions": mission_store.list_active(player_id)}

@app.post("/missions/progress")
def missions_progress(req: MissionProgressRequest):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    # An unknown mission_id is a 404, not a 200 carrying ok=false: the caller
    # named a resource that does not exist, and a soft error here reads as
    # "progress recorded" to a client that only checks the status code.
    if mission_def(req.mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    # req.amount is parsed for wire compatibility and IGNORED. Progress is
    # derived from server-observed events (missions.record_battle_win), so a
    # caller cannot dial its own mission to complete.
    result = mission_store.add_progress(req.player_id, req.mission_id, req.amount)
    return {"ok": result.get("ok", False), **{k: v for k, v in result.items() if k != "ok"}}

@app.post("/missions/claim")
def missions_claim(req: MissionClaimRequest):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    # Pass wallet_store for server-authoritative reward grants
    result = mission_store.claim(req.player_id, req.mission_id, wallet_store)
    return {"ok": result.get("ok", False), **{k: v for k, v in result.items() if k != "ok"}}

@app.post("/missions/reset/daily")
def missions_reset_daily(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    count = mission_store.reset_daily(player_id)
    return {"ok": True, "reset_count": count}

@app.post("/missions/reset/weekly")
def missions_reset_weekly(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    count = mission_store.reset_weekly(player_id)
    return {"ok": True, "reset_count": count}

# Gift Series routes (Tier 1: Multi-Tier Gift Series - Overmortal _gift_1001..2014)
from gift_series import init_gift_series_store, register_gift_series_routes
init_gift_series_store(pg_pool if USE_PG else None)
register_gift_series_routes(app, lambda: players_db, lambda: wallet_store)

# F4: Inventory endpoints (full build — PG-backed with wallet-integrated shop)
from inventory import (
    init_inventory_store, SHOP_PRODUCTS, ITEM_DEFINITIONS, apply_item_effect,
    InventoryBuyRequest, InventoryUseRequest, InventoryDiscardRequest,
    InventoryExpandRequest, SORT_ORDERS, TYPE_ORDER, MAX_DISCARD_QUANTITY,
    BASE_INVENTORY_CAPACITY, MAX_INVENTORY_SLOTS, EXPANSION_SCROLL_ITEM,
)
from wallet_dispatch import spend as wallet_spend, earn as wallet_earn

# Initialize inventory store with PG pool (if available)
if USE_PG:
    inventory_store = init_inventory_store(pg_pool, players_db)
else:
    # The player store is passed in BOTH modes, not just PG. It is how
    # `get_player_capacity` reaches the record the expansion scroll writes,
    # so leaving it out here would keep the scroll a no-op in dev and in
    # the test suite while looking fixed in production.
    inventory_store = init_inventory_store(None, players_db)

# The binding is OUTSIDE the if/else on purpose. Inside the `else:` arm it
# ran in memory mode only, so with the PostgreSQL backend active
# `PlayerStore.inventory_store` stayed None: transaction() found no
# `snapshot` to take, and a failed transaction kept the item debit while
# every in-memory test stayed green.
players_db.inventory_store = inventory_store

# Equipment Score Ranking v1: quality scaling, per-type sorting, set bonuses.
from equipment import register_equipment_routes
register_equipment_routes(app, _players_db_accessor)

# Gubao/Fabao: dedicated relic artifact slots, activation, and bounded receipts.
from artifacts import register_artifact_routes
register_artifact_routes(app, _players_db_accessor)

# Inner Skills: comprehension, six ES points, and Tupo breakthrough
from inner_skills import register_inner_skill_routes
register_inner_skill_routes(app, _players_db_accessor, inventory_store)

# Codex / Bestiary: the read routes, plus the store binding the encounter
# writer uses. ABOVE install_ownership_guard (bottom of this file) on purpose --
# the guard REWRITES routes in place, so a call placed after the mount would
# register these too late and leave every one of them unguarded.
from codex import register_codex_routes
register_codex_routes(app, _players_db_accessor)

# Crafting: materials + fee in, one item out, all inside one transaction.
# Registered after `inventory_store` exists because the craft route reads the
# canonical inventory, not the loose bag.
from crafting import register_crafting_routes
register_crafting_routes(app, _players_db_accessor, inventory_store)

# Receipts for /inventory/expand, keyed the same way the wallet keys its own
# idempotency: namespaced to the route so a key minted for another route can
# never be read as a replay here.
#
# A replay must return the STORED receipt, not re-run the expansion. Re-running
# would expand twice and consume a second scroll for one key, which is the
# duplication shape this route exists to prevent.
def _expand_replay(idem: str, player_id: str) -> Optional[dict]:
    recall = getattr(wallet_store, "recall", None)
    if recall is None:
        return None
    try:
        record = recall(f"inventory-expand:{idem}", player_id)
    except Exception:
        return None
    if isinstance(record, dict) and record.get("ok") is True:
        out = dict(record)
        out["_replayed"] = True
        return out
    return None


def _expand_remember(idem: str, player_id: str, receipt: dict) -> None:
    seen = getattr(wallet_store, "seen", None)
    if seen is None:
        return
    try:
        seen(f"inventory-expand:{idem}", player_id, dict(receipt))
    except Exception:
        # A store without a receipt degrades replay safety on retry, never the
        # write itself. Stated here rather than hidden.
        pass


@app.get("/inventory/{player_id}")
def inventory_view(player_id: str, sort: str = "default", type: str = "all"):
    """The whole bag, in the server's canonical order.

    The numbers a capacity bar draws are SLOTS, not units. This route used to
    sum every quantity while the shop counted rows, so a player holding 60 of
    one material was told "60 of 50, bag full" by this route and "1 of 50,
    plenty of room" by the shop, and the shop then sold them something. Two
    routes answering different questions about one player is the defect.

    `slots_used` is the real number. `total_units` is for the player's
    information ("I own 60 bones"). `used` is kept for one release and set equal
    to `slots_used`, because the client reads it today and removing it in the
    same commit that changes its meaning breaks the bag screen.
    """
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    items = inventory_store.get_inventory(player_id, sort=sort, item_type=type)
    capacity = inventory_store.get_player_capacity(player_id)
    slots_used = inventory_store.get_slots_used(player_id)
    return {
        "ok": True,
        "items": items,
        "capacity": capacity,
        "slots_used": slots_used,
        "total_units": inventory_store.get_total_units(player_id),
        "max_slots_buyable": max(0, MAX_INVENTORY_SLOTS - capacity),
        # Legacy alias. Same value as slots_used, kept so the existing client
        # keeps drawing a correct bar for one release.
        "used": slots_used,
    }


@app.post("/inventory/discard")
def inventory_discard(req: InventoryDiscardRequest):
    """Destroy units of an effect-less item so the player can free a slot.

    Only items with NO effect may be discarded. That is the anti-abuse rule:
    a rare pill cannot be destroyed by the player, so "I destroyed my rare" is
    always a bug and never a choice. Discarding grants nothing -- no currency,
    no progress, no event -- because a refund would be a duplication vector
    pointed the other way.
    """
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    # Defense in depth: the model bound only runs when FastAPI parses a body.
    # An in-process caller can hand us a duck-typed request and skip it, and a
    # negative quantity would reach remove_item and ADD units back.
    if req.quantity < 1 or req.quantity > MAX_DISCARD_QUANTITY:
        raise HTTPException(status_code=400, detail="Quantity out of range")
    result = inventory_store.discard_item(req.player_id, req.item_id, req.quantity)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail={
            "error": result.get("error", "Discard failed"),
            "item_id": req.item_id,
        })
    return result


@app.post("/inventory/expand")
def inventory_expand(req: InventoryExpandRequest, request: Request):
    """Use one expansion scroll, and refuse it at the capacity ceiling.

    This route exists instead of routing expansion through the generic
    `/inventory/use` because the defect this whole feature is named after was
    a handler writing one number while the bag enforced another. Here the
    ceiling check, the write, and the receipt all sit in one place, and the
    answer reports the capacity READ BACK through the same reader the bag uses.

    The idempotency key is REQUIRED, not optional. A plain use is naturally
    once-per-item because you cannot use a scroll you no longer hold, so it
    needs no key. This route permanently changes an account and a replayed
    request would expand twice for one scroll.
    """
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    if req.quantity != 1:
        raise HTTPException(status_code=400, detail="An expansion is one scroll at a time")
    if req.item_id != EXPANSION_SCROLL_ITEM:
        raise HTTPException(status_code=400, detail="Not an expansion scroll")
    idem = require_idempotency_key(request, route="inventory-expand")
    replay = _expand_replay(idem, req.player_id)
    if replay is not None:
        return replay

    with players_db.transaction():
        player = players_db[req.player_id]
        owned = inventory_store.get_item_count(req.player_id, req.item_id)
        if owned < 1:
            raise HTTPException(status_code=400, detail={
                "error": "insufficient_quantity", "item_id": req.item_id,
            })
        # The ceiling check happens INSIDE this transaction, immediately before
        # the write. A check in its own transaction is a race two simultaneous
        # requests walk straight through.
        result = inventory_store.expand_capacity(req.player_id, player)
        if not result.get("ok"):
            # Refused before the scroll is touched, so the player keeps it.
            raise HTTPException(status_code=400, detail={
                "error": result.get("error", "Expansion failed"),
                "slots": result.get("slots"),
                "max_slots": result.get("max_slots"),
            })
        removed = inventory_store.remove_item(req.player_id, req.item_id, 1)
        if not removed.get("ok"):
            # Give the scroll back. The write already landed, so roll it back
            # to the value we read, which is the same field.
            player["inventory_capacity"] = result.get("slots_before",
                                                      BASE_INVENTORY_CAPACITY)
            raise HTTPException(status_code=400, detail={"error": "Scroll not returned"})
        players_db.persist(req.player_id, player)
    _expand_remember(idem, req.player_id, result)
    return result

@app.post("/inventory/use")
def inventory_use(req: InventoryUseRequest):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    with players_db.transaction():
        player = players_db[req.player_id]
        item_def = ITEM_DEFINITIONS.get(req.item_id)
        if not item_def:
            raise HTTPException(status_code=400, detail="Invalid item")
        # EDGE BOUND, defense in depth. The request model already carries
        # `quantity: int = Field(default=1, ge=1, le=MAX_USE_QUANTITY)`, but that
        # guard only runs on the WEB path, where FastAPI parses the body. Any
        # in-process caller (a scheduled job, an admin script, a test, or any
        # module that imports this handler function) can hand us a request
        # object built with `model_construct` or a duck-typed stand-in, and
        # validation never runs.
        #
        # With quantity < 1 both of the following checks are satisfied: `owned`
        # is non-negative so `owned < req.quantity` is false for a negative
        # quantity, and `remove_item(..., -1)` SUBTRACTS a negative, which ADDS
        # one back to the inventory. The item effect still applies. So a negative
        # quantity was strictly better than a positive one: it minted the item
        # and consumed it at the same time, and replaying it was unbounded.
        # quantity == 0 was the same mint with a subtler trace.
        if req.quantity < 1:
            raise HTTPException(status_code=400, detail="Quantity must be at least 1")
        owned = inventory_store.get_item_count(req.player_id, req.item_id)
        if owned < req.quantity:
            raise HTTPException(status_code=400, detail="Insufficient quantity")
        remove_result = inventory_store.remove_item(req.player_id, req.item_id, req.quantity)
        if not remove_result.get("ok"):
            raise HTTPException(status_code=400, detail=remove_result.get("error", "Failed to remove item"))
        effect_result = apply_item_effect(player, item_def, wallet_store)
        if not effect_result.get("ok"):
            inventory_store.add_item(req.player_id, req.item_id, req.quantity)
            raise HTTPException(status_code=400, detail=effect_result.get("error", "Effect failed"))
        players_db.persist(req.player_id, player)
        return {"ok": True, "effect": effect_result, "remaining": remove_result.get("remaining", 0)}

def _shop_note_failure(idem: str, player_id: str, detail: dict) -> None:
    """Remember that THIS purchase key ended in a failure, so a retry can be honest.

    The wallet records a successful charge against its idempotency key, but it
    knows nothing about what the route did afterwards. A purchase that was
    charged, failed to deliver, and rolled back therefore leaves the wallet
    holding a SUCCESS for that key, and a plain retry answered `200 {"ok":
    true}` for goods that never arrived. The money was right; the receipt was
    not.

    The marker lives in the same idempotency store as the charge, under its own
    namespaced key, so it dies with the wallet rather than with the process.
    A store without a marker table (the Postgres one) simply does not get this
    protection -- stated here rather than hidden, because a missing marker
    degrades the receipt, never the balance.
    """
    seen = getattr(wallet_store, "seen", None)
    if seen is None:
        return
    try:
        seen(f"shop-failed:{idem}", player_id, {"ok": False, "detail": detail})
    except Exception:
        pass


def _shop_prior_failure(idem: str, player_id: str) -> Optional[dict]:
    """The recorded failure for this purchase key, if this purchase already failed."""
    recall = getattr(wallet_store, "recall", None)
    if recall is None:
        return None
    try:
        record = recall(f"shop-failed:{idem}", player_id)
    except Exception:
        return None
    if isinstance(record, dict) and record.get("ok") is False:
        return record.get("detail")
    return None


def _shop_refund(player_id: str, currency: str, amount: int, idem: str, reason: str) -> dict:
    """The ONE way a shop purchase gives money back.

    Every failed or part-filled purchase routes its refund through here, so
    there is a single credit path to reason about. `reason` namespaces the
    wallet idempotency key: a rollback (nothing delivered) and a partial
    delivery (some units delivered) are different events, and sharing one key
    between them would let the second refund be swallowed as a replay of the
    first, or let a retry of the same purchase credit twice.

    The amount is clamped to what this request actually charged. A refund
    larger than the charge is a mint, and a refund that ignores a replayed
    purchase is a double credit.
    """
    if amount <= 0:
        return {"ok": True, "refunded": 0}
    result = wallet_earn(wallet_store, player_id, currency, amount,
                         f"{reason}:{idem}", "shop_refund")
    if not result.get("ok"):
        # Loud on purpose. A silent failure here means the player paid for
        # goods that never arrived and nobody will ever notice.
        wcl.safe_log_event(wcl.EVENTS["WALLET_EARN_REJECTED"], player_id=player_id,
                           ok=False, outcome="refund_failed", reason=reason,
                           currency=currency, amount=amount)
        return {"ok": False, "refunded": 0, "error": result.get("error", "refund refused")}
    return {"ok": True, "refunded": amount}


#: What one ``spirit_stone_pack_small`` pays out. The advertised name is
#: "Spirit Stone Pack (100)", so the server names the amount here rather
#: than reading it from the request -- a caller that chooses its own
#: payout is a mint (see ``mint-authority-caller-names-reason-server-names-amount``).
SPIRIT_STONE_PACK_AMOUNT = 100


def _shop_finite_number(value, default: int = 0) -> int:
    """One conversion for every number the shop reads off data it did not mint.

    The shop multiplies, compares and JSON-encodes numbers that arrive from
    three places it does not control: the ``SHOP_PRODUCTS`` price table, the
    wallet balance behind the player id, and the delivery counters an
    inventory store returns. A single non-finite value in any of them --
    ``inf``, ``-inf``, ``nan`` -- is not a wrong number, it is a value no
    honest arithmetic survives, and each one reaches a different failure:

      * ``int(inf)`` raises ``OverflowError``; ``int(nan)`` raises
        ``ValueError`` (MEASURED). Both escape the handler, so the route
        answers a bare 500 with no field named.
      * A non-finite figure that survives conversion still breaks the
        reply: FastAPI's encoder rejects it with "Out of range float values
        are not JSON compliant: inf" (MEASURED), which is a 500 that names
        no currency and no product either.

    So the value is checked for finiteness ONCE, here, and every caller gets
    a whole number it can do arithmetic and JSON with. ``default`` is the
    honest answer for "this server never issued that number": 0 for a count
    that means "none", never a guess at what the real figure was.

    The order of the two steps is the whole point, and it is the opposite of
    what looks natural:

      * ``float()`` first, in a ``try``. It raises on the two inputs that are
        not numbers at all -- text, and an integer too large to become a
        float (``OverflowError`` on ``10 ** 400``, MEASURED). Neither can be
        tested for finiteness, because the test itself would raise.
      * ``math.isfinite`` second, on the float. ``inf`` and ``nan`` survive
        ``float()`` perfectly well, so nothing but this test rejects them.
      * ``int()`` last, only on a value already known to be finite. This is
        why the guard is not written the other way round: ``int(inf)``
        raises ``OverflowError`` and ``int(nan)`` raises ``ValueError``
        (MEASURED), so converting FIRST makes the finiteness test
        unreachable and leaves the crash in place.

    Every caller gets a whole number it can do arithmetic and JSON with.
    ``default`` is the honest answer for "this server never issued that
    number": 0 for a count that means "none", never a guess at the real one.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return int(number)


@app.post("/shop/buy")
def shop_buy(req: InventoryBuyRequest, request: Request):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    # A purchase is for a positive whole number of units. The model has no
    # lower bound, so a negative quantity reached the wallet, which refused it
    # with a raw "amount must be positive" dict. Refuse it here, before any
    # price is computed, with a message that names the field.
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")

    product = SHOP_PRODUCTS.get(req.product_id)
    if not product:
        raise HTTPException(status_code=400, detail="Product not found")

    currency = product["currency"]
    # The price table is the one place a price can be non-finite: it is data,
    # not a constant, and a bad row reaches the reply as JSON. Refused here,
    # before a charge, so an unreadable price can never be spent or refunded.
    price_per = _shop_finite_number(product["price"])
    if price_per <= 0:
        raise HTTPException(status_code=400, detail="Product price is not available")
    limit = _shop_finite_number(product.get("limit", 99), 99)
    total_price = price_per * req.quantity

    if req.quantity > limit:
        raise HTTPException(status_code=400, detail=f"Purchase limit: {limit} per transaction")

    # Special products don't need inventory capacity.
    #
    # A stack the player ALREADY holds needs no new slot, so a full bag must
    # not refuse a purchase of something already in it -- the stack ceiling and
    # its refund (below) handle the overflow honestly. Only a genuinely new
    # item kind consumes a slot, and that is the one case a full bag refuses.
    # The refusal is explicit: the numbers the player would need to act.
    # A scroll the player cannot use is a paid no-op: today it would be sold,
    # charged for, delivered, and refused at the moment of use. That is the
    # exact shape of the defect this file was written to close, so the shop
    # refuses it up front instead.
    if req.product_id == EXPANSION_SCROLL_ITEM:
        slots = inventory_store.get_player_capacity(req.player_id)
        if slots >= MAX_INVENTORY_SLOTS:
            raise HTTPException(status_code=400, detail={
                "error": "at_capacity_ceiling",
                "slots": slots,
                "max_slots": MAX_INVENTORY_SLOTS,
                "message": (f"Bag already at the maximum of {MAX_INVENTORY_SLOTS} "
                            f"slots. This scroll would add nothing."),
            })

    is_inventory_item = req.product_id in ITEM_DEFINITIONS
    if is_inventory_item and inventory_store.get_item_count(req.player_id, req.product_id) == 0:
        used_slots = len(inventory_store.get_inventory(req.player_id))
        capacity = inventory_store.get_player_capacity(req.player_id)
        if used_slots >= capacity:
            raise HTTPException(status_code=400, detail={
                "error": "inventory_full",
                "used_slots": used_slots,
                "capacity": capacity,
                "message": (f"Inventory full: {used_slots}/{capacity} slots used. "
                            f"Free a slot, or buy an expansion scroll."),
            })

    # Server-authoritative spend
    # Refuse a headerless purchase instead of deriving the key from the body.
    # A body-derived key collapsed two GENUINE purchases of the same item into
    # one "replay", so the second paid nothing. See idem_gate.py.
    idem = require_idempotency_key(request, route="shop-buy")
    spend_result = wallet_spend(wallet_store, req.player_id, currency, total_price, req.client_version, idem)
    if not spend_result.get("ok"):
        if spend_result.get("error") == "insufficient balance":
            # Name the shortfall. The raw wallet dict said "insufficient
            # balance" and nothing else, so a player could not tell how much
            # more they needed. The balance read here is for the MESSAGE only;
            # the refusal itself is the wallet's.
            wallet = wallet_store.get(req.player_id)
            available = _shop_finite_number(getattr(wallet.balance, currency, 0)) if wallet else 0
            raise HTTPException(status_code=400, detail={
                "error": "insufficient_funds",
                "currency": currency,
                "required": total_price,
                "available": available,
                "missing": max(0, total_price - available),
                "message": (f"Insufficient {currency}: {total_price} needed, "
                            f"{available} available, {max(0, total_price - available)} short."),
            })
        raise HTTPException(status_code=400, detail=spend_result)
    if spend_result.get("_replayed"):
        # The wallet already charged this exact key. Returning the stored
        # success is what makes a retry safe; re-running the grant path here
        # would deliver a second time for one charge.
        #
        # Unless this purchase already FAILED after the charge, in which case
        # the stored success is a lie about goods that never arrived. The
        # recorded failure is re-raised instead, with its refund figure, and no
        # second refund is issued -- the wallet key is what makes that safe.
        prior_failure = _shop_prior_failure(idem, req.player_id)
        if prior_failure:
            raise HTTPException(status_code=400, detail=prior_failure)
        return {"ok": True, "_replayed": True}

    player = players_db[req.player_id]
    granted = {}
    delivered_quantity = req.quantity
    refunded = 0

    # Handle special products
    if req.product_id == "starter_pack":
        # Grant starter pack rewards
        granted["summon_token"] = 1
        player["summon_tokens"] = player.get("summon_tokens", 0) + 1
    elif req.product_id == "battle_pass":
        # Unlock battle pass premium
        granted["battle_pass_premium"] = True
        player["battle_pass_premium"] = True
        # Grant battle pass XP
        bp_result = battle_pass.grant_xp(player, 0, "battle_pass_purchase")
        granted["battle_pass"] = bp_result
    elif req.product_id == "summon_bundle_10":
        # Grant 10 summon tokens
        granted["summon_token"] = 10
        player["summon_tokens"] = player.get("summon_tokens", 0) + 10
    elif req.product_id == "spirit_stone_pack_small":
        # MEASURED 2026-10-04: this row was advertised at 99 fateum and
        # priced in SHOP_PRODUCTS, but it was in neither ITEM_DEFINITIONS
        # nor the three hand-named special products above, so every press
        # fell through to the unknown-product branch: the till took the
        # 99, then refunded it and answered 400. A refund is not a
        # purchase -- the player still could not buy the row they were
        # shown. (docs/economy-repeat-gain-audit-2026-09-29.md recorded
        # the same thing as "charges then refuses".)
        #
        # The stones are wallet currency, which is what the rest of the
        # shop prices in (`cultivation_pill_low` and friends are charged
        # in spirit_stone), so a pack buys spendable stones rather than a
        # separate ledger nobody can shop with.
        #
        # The earn key is namespaced under `shop_grant:` so it can never
        # collide with the spend's own key or with a refund's
        # `{reason}:{idem}` key. If the grant is refused the money goes
        # back through the ONE refund helper, so this branch cannot leave
        # a player poorer for goods that never arrived.
        stones = SPIRIT_STONE_PACK_AMOUNT * req.quantity
        earned = wallet_earn(wallet_store, req.player_id, "spirit_stone",
                              stones, f"shop_grant:{idem}", "shop_purchase")
        if not earned.get("ok"):
            refund = _shop_refund(req.player_id, currency, total_price, idem, "rollback")
            detail = {
                "error": "delivery_failed",
                "message": earned.get("error", "Failed to grant spirit stones"),
                "refunded": {currency: refund["refunded"]},
            }
            _shop_note_failure(idem, req.player_id, detail)
            raise HTTPException(status_code=400, detail=detail)
        granted["spirit_stone"] = stones
    elif is_inventory_item:
        # Add items to inventory
        add_result = inventory_store.add_item(req.player_id, req.product_id, req.quantity)
        if not add_result.get("ok"):
            refund = _shop_refund(req.player_id, currency, total_price, idem, "rollback")
            detail = {
                "error": "delivery_failed",
                "message": add_result.get("error", "Failed to add item to inventory"),
                "refunded": {currency: refund["refunded"]},
            }
            _shop_note_failure(idem, req.player_id, detail)
            raise HTTPException(status_code=400, detail=detail)
        # A stack has a ceiling, so an order can arrive partly filled. The
        # player was charged for every unit, so the undelivered ones are
        # refunded at the product's own unit price through the SAME refund call
        # the failure branch above uses (a second refund path is where
        # double-refunds are born), under its own reason key so it can never
        # collide with the failure refund. The receipt then reports what
        # actually landed, not what was asked for.
        delivered_quantity = _shop_finite_number(add_result.get("granted"), req.quantity)
        # An unreadable `discarded` defaults to the FULL order, not to 0. The
        # count says how many units the store could NOT keep; a store that
        # cannot count them has kept nothing we can prove, and defaulting to 0
        # would leave a player who was charged for the whole order with no
        # refund at all. `min(discarded, req.quantity)` below still bounds the
        # refund by what was taken, so this cannot become a mint.
        discarded = _shop_finite_number(add_result.get("discarded", 0), req.quantity)
        if discarded > 0:
            # Bounded by the charge: a discarded count larger than what was
            # asked for would refund more than was taken.
            refundable = min(discarded, req.quantity) * price_per
            refund = _shop_refund(req.player_id, currency, refundable, idem, "stack_full")
            refunded = _shop_finite_number(refund.get("refunded", 0))
        granted["item"] = {"item_id": req.product_id, "quantity": delivered_quantity}
    else:
        # Unknown product type
        refund = _shop_refund(req.player_id, currency, total_price, idem, "rollback")
        detail = {
            "error": "unknown_product",
            "message": "Unknown product type",
            "refunded": {currency: refund["refunded"]},
        }
        _shop_note_failure(idem, req.player_id, detail)
        raise HTTPException(status_code=400, detail=detail)
    
    players_db.persist(req.player_id, player)
    receipt = {
        "ok": True,
        "product_id": req.product_id,
        "quantity": delivered_quantity,
        "requested": req.quantity,
        "spent": {currency: total_price - refunded},
        "granted": granted,
    }
    if refunded > 0:
        receipt["refunded"] = {currency: refunded}
        receipt["warning"] = "stack_full"
    return receipt

# F11: Cosmetics routes registered (design/build deferred per feature-roadmap.md F11)
from cosmetics import register_cosmetic_routes
register_cosmetic_routes(app, lambda: players_db)

# Stamina: GET /stamina/info, POST /stamina/consume, POST /stamina/purchase.
#
# Registered here rather than inside zone_routes.py, where the two older
# /zones/stamina* routes live, for one reason: those two take a `player_id` from
# the query string or the body, while these three take the player from the
# SESSION and have no player parameter at all. Mounting them next to each other
# would suggest the two shapes are interchangeable. They are not — a route that
# can be told whose player to act on can be pointed at somebody else's, and a
# route that cannot cannot.
#
# Both stores arrive as accessors (not captured objects) so a test that
# monkeypatches either store sees the swap, matching every other register_*
# call in this file.
from stamina_routes import register_stamina_routes
register_stamina_routes(app, lambda: players_db, lambda: wallet_store)

# C2 goal panel routes. goal_routes.py is finished and read-only (it holds no
# player-record write), but register_goal_routes was never called from here, so
# the client panel had nothing to talk to. Mounting it turns the two
# test_goal_routes.py failures green. The handlers import players_db and
# inventory_store from `main` lazily so they see the live objects rather than
# the module-level copies bound at import time.
from goal_routes import register_goal_routes
register_goal_routes(app)

# Event scheduler
event_scheduler: Optional[EventScheduler] = None

# Tournament bracket clock. Started in the lifespan alongside the event
# scheduler and stopped in the same finally block, so a shutdown cannot leave a
# bracket ticking against a process that is going away.
tournament_job = None

# Sect war settlement clock. Started in the lifespan alongside the
# tournament clock and stopped in the same finally block, so no war
# waits for a player to log in and no thread ticks into a dying
# process.
sect_war_job = None


# --- Models ---

class PlayerCreate(BaseModel):
    name: str
    spirit_root: str


class Player(BaseModel):
    id: str
    name: str
    cultivation_realm: str
    cultivation_stage: str
    path: str
    spirit_root: str
    progress: float
    spirit_stones: dict
    atk: int
    def_: int
    hp: int
    mp: int
    spd: int
    pvp_rating: int
    sect_id: Optional[str] = None
    sect_rank: Optional[str] = None
    sect_contribution: int = 0
    dao_companion_id: Optional[str] = None
    signing_key: str = ""  # Per-session HMAC signing key


class OfflineProgressRequest(BaseModel):
    hours: float


class OfflineProgressResponse(BaseModel):
    progress_gained: float
    new_progress: float
    realm: str
    stage: str
    breakthrough_ready: bool


class BreakthroughRequest(BaseModel):
    player_id: str


class BreakthroughResponse(BaseModel):
    success: bool
    new_realm: Optional[str] = None
    new_stage: Optional[str] = None
    message: str


class CultivationBreakthroughRequest(BaseModel):
    player_id: str
    current_realm: str
    current_stage: str
    client_version: int


class CultivationClaimOfflineRequest(BaseModel):
    player_id: str
    device_now: float


class GachaPullRequest(BaseModel):
    player_id: str
    pull_type: str = "single"  # "single" or "multi_10"
    client_version: int = 0


class BeastTameRequest(BaseModel):
    player_id: str
    items_used: int = 0
    target_rarity: Optional[str] = None
    client_version: int = 0


class BeastEvolveRequest(BaseModel):
    player_id: str
    beast_id: str


class BeastBloodUpgradeRequest(BaseModel):
    player_id: str
    beast_id: str
    client_version: int = 0


class BeastFormationRequest(BaseModel):
    player_id: str
    beast_id: str
    slot: int = 0


class BeastSkillUpgradeRequest(BaseModel):
    player_id: str
    beast_id: str
    skill_id: str
    client_version: int = 0


class SectJoinRequest(BaseModel):
    player_id: str
    sect_id: str


class ShopBuyRequest(BaseModel):
    player_id: str
    product_id: str
    client_version: int = 0


class EventClaimRequest(BaseModel):
    player_id: str
    event_id: str
    client_version: int = 0



class EventListRequest(BaseModel):
    player_id: str
    state: Optional[str] = None


class AdminEventCreateRequest(BaseModel):
    event_key: str
    name: str
    event_type: str = "generic"
    duration_hours: int = 24
    pre_start_hours: int = 0
    post_end_hours: int = 0
    rewards: List[dict] = []
    start_time: Optional[float] = None


class AdminEventScheduleRequest(BaseModel):
    event_key: str
    name: str
    event_type: str = "generic"
    duration_hours: int = 24
    pre_start_hours: int = 0
    post_end_hours: int = 0
    rewards: List[dict] = []
    schedule: dict = {}


class CombatAutoBattleRequest(BaseModel):
    """Wire shape of POST /combat/auto-battle.

    `enemy_stats`, `max_turns` and `seed` are still ACCEPTED because the shipped
    Godot client sends them (client/scripts/systems/CombatSystem.gd) and
    removing a field the client posts would be a self-inflicted outage. They
    are NOT read by the route: the opponent, the turn limit and the seed are
    all derived server-side, so a caller cannot pick a 1-HP enemy or re-roll a
    seed until the simulation reports a win. That is why `enemy_stats` is a
    plain `dict` and not a typed model -- a half-populated dict
    (`{"hp": 1}`) must parse cleanly rather than 422, because the values are
    discarded anyway.

    `rewards` is bounded, not honoured. It is a malformed-input guard so a
    nine-figure or NEGATIVE amount is refused at the edge with 422 instead of
    reaching the wallet; the amount actually paid is AUTO_BATTLE_REWARDS.
    """
    player_id: str
    enemy_stats: dict = {}
    max_turns: int = 100
    rewards: Dict[str, float] = {}
    seed: int | None = None

    @field_validator("rewards")
    @classmethod
    def _bound_wire_rewards(cls, value: Dict[str, float]) -> Dict[str, float]:
        """Reject reward amounts the server would never pay, and negatives.

        A dict field cannot use the `Query(ge=..., le=...)` bound that
        monthly_event.py puts on scalar params, so the bound walks the values.
        Upper bound rejects the nine-figure mint attempt; the lower bound
        rejects a NEGATIVE amount, which would otherwise write a negative
        wallet balance. Both are 422s from pydantic, before the route runs.
        """
        for currency, amount in (value or {}).items():
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                raise ValueError(f"reward for {currency!r} is not a number")
            if amount < 0:
                raise ValueError(f"reward for {currency!r} is negative")
            if amount > AUTO_BATTLE_REWARD_CEILING:
                raise ValueError(
                    f"reward for {currency!r} is above the "
                    f"{AUTO_BATTLE_REWARD_CEILING} ceiling"
                )
        return value


class PvPMatchRequest(BaseModel):
    player_id: str
    opponent_id: str
    opponent_rating: int
    enemy_stats: dict


class PvPMatchmakingRequest(BaseModel):
    player_id: str


class PvPMatchHistoryRequest(BaseModel):
    player_id: str
    limit: int = 20


class PurchaseValidationRequest(BaseModel):
    receipt: str
    product_id: str
    player_id: str


class PurchaseValidationResponse(BaseModel):
    valid: bool
    transaction_id: Optional[str] = None
    items_granted: List[str] = []


# --- Wallet Models (NEW) ---

class WalletResponse(BaseModel):
    player_id: str
    balance: dict
    wallet_version: int
    signature: str
    timestamp: float
    algorithm: str
    result: Optional[dict] = None


class WalletSpendRequest(BaseModel):
    currency: str
    amount: int
    client_version: int
    # Idempotency-Key header used instead of body field


class WalletEarnRequest(BaseModel):
    currency: str
    amount: int
    reward_type: str = ""


class WalletClaimOfflineRequest(BaseModel):
    currency: str
    rate_per_sec: float
    device_now: float


# --- Auth (minimal for dev) ---

def generate_session_token(player_id: str) -> str:
    token = secrets.token_urlsafe(32)
    sessions_db[token] = {
        "player_id": player_id,
        "created": datetime.now().isoformat(),
        "expires": (datetime.now() + timedelta(days=30)).isoformat(),
    }
    return token


# --- Helpers ---

def _get_wallet_or_404(player_id: str):
    w = wallet_store.get(player_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Player wallet not found")
    return w


def _sign_wallet_response(w) -> dict:
    """Sign a wallet response for client verification."""
    player_id = w.player_id
    # Use per-session signing key (restored from PG after restart). If neither
    # memory nor PG has one, fall back to the SERVER-WIDE key from the
    # environment -- never to a literal in this file. A key shipped in the
    # source is readable by anyone, so signing with one protects nothing: an
    # attacker who read the repository could forge a wallet response the
    # client would accept. With no key configured at all, request_signing
    # raises and the route refuses rather than degrade.
    try:
        session_key = _ensure_session_key(player_id)
    except HTTPException:
        session_key = None  # -> sign_response() requires WC_SIGNING_KEY
    sig = sign_response(player_id, w.version, _balance_to_dict(w.balance), session_key)
    return {
        "player_id": player_id,
        "balance": _balance_to_dict(w.balance),
        "wallet_version": w.version,
        **sig,
    }


def _balance_to_dict(balance) -> dict:
    """Balance is CurrencyBalance (in-memory) or a plain dict (PG)."""
    if hasattr(balance, "to_dict"):
        return balance.to_dict()
    return dict(balance or {})


# --- Endpoints ---

@app.get("/")
def root():
    return {
        "name": "Wayfarer's Cycle API",
        "version": "0.2.0",
        "status": "operational",
        "endpoints": [
            "/player/create", "/player/{id}",
            "/cultivation/offline-progress", "/cultivation/breakthrough",
            "/purchase/validate",
            "/wallet", "/wallet/spend", "/wallet/earn", "/wallet/claim-offline",
            "/lifecycle/{id}", "/lifecycle/{id}/check-in",
            "/sign-in/{id}", "/sign-in/{id}/claim",
            "/daily-boss/{id}", "/daily-boss/{id}/strike",
        ],
    }


@app.get("/game-data/realms")
def get_realms():
    return REALMS


@app.get("/game-data/spirit-roots")
def get_spirit_roots():
    return SPIRIT_ROOTS


@app.get("/game-data/sects")
def get_sects():
    return SECTS


@app.get("/game-data/npcs")
def get_npcs():
    return NPCS


# --- Account-creation rate limit (abuse control on the one public signup) ---
#
# /player/create is the only data-changing route that is deliberately public:
# it IS the signup. That makes it the only door a botnet can walk through
# without a credential, so it needs its own throttle. Without one, a caller
# holding nothing can mint unlimited free accounts, farm leaderboards with
# fresh 1000 ratings, and grow the store until the seat cap trips.
#
# Scope, deliberately narrow:
#   * counts ONLY /player/create, so an existing player signing in on
#     /auth/login is never throttled -- that is a different route;
#   * keys on the caller's IP address, so a fresh address with no prior
#     account is exactly the thing that gets capped.
#
# The limit and window are env-overridable (WC_SIGNUP_RATE_LIMIT,
# WC_SIGNUP_RATE_WINDOW_SECONDS) so a deployment behind a large shared
# address -- a campus, a carrier NAT, a corporate proxy -- can raise the cap
# without a code change, and so an operator can disable it deliberately rather
# than by accident. Defaults are 5 per address per 60s: a human making a
# second account, or a household behind one address, stays well under it.
#
# ONLY REQUESTS THAT ARRIVED OVER A REAL NETWORK SOCKET ARE COUNTED. This is
# the rule that keeps the throttle from being a self-inflicted outage:
# FastAPI's in-process test client reports the literal peer string
# "testclient" for every request, so keying on it lumps the entire test suite
# into one bucket and the 6th account ANY test file creates is refused with a
# 429. Measured: 203 unrelated tests failed on exactly that, while each test
# file passed alone. A real socket always yields a real IP, so "is this a real
# address?" is the honest gate -- an in-process mount has no remote caller to
# throttle, and a deployment where it matters is never an in-process mount.
#
# In-memory and per-process on purpose: this is a cheap abuse brake, not a
# distributed quota system, and a restart resets it. The durable account cap
# is players_db's seat cap, which this deliberately does not replace.
def _env_int(name: str, default: int) -> int:
    """Positive-int env override; a blank or unparseable value keeps the default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


SIGNUP_RATE_LIMIT = _env_int("WC_SIGNUP_RATE_LIMIT", 5)
SIGNUP_RATE_WINDOW_SECONDS = _env_int("WC_SIGNUP_RATE_WINDOW_SECONDS", 60)

#: The peer string every in-process ASGI mount reports. Not an IP address.
_INPROCESS_PEER_SENTINELS = frozenset({"testclient", "testclient.net", ""})

_SIGNUP_ATTEMPTS: dict[str, deque] = {}
_SIGNUP_WINDOW_START: dict[str, float] = {}


def _client_address(request: Request) -> str | None:
    """The caller's real network address, or None when there isn't one.

    Returns None for an in-process mount (peer is the "testclient" sentinel)
    so the caller can skip counting rather than lump every such request into
    one shared bucket. Behind a reverse proxy the socket peer is the proxy's
    own address, so X-Forwarded-For's first entry is used when present; it is
    a hint for bucketing only, never an authorization decision.
    """
    peer = request.client.host if request.client else None
    if peer and peer not in _INPROCESS_PEER_SENTINELS:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return None


def _reset_signup_rate_limit_state() -> None:
    """Drop all abuse counters. Exposed so a test can start from a clean slate."""
    _SIGNUP_ATTEMPTS.clear()
    _SIGNUP_WINDOW_START.clear()


def _evict_expired_signup_counters(now: float) -> None:
    """Forget every address whose rate-limit window has already closed.

    The two counter dicts are keyed by caller address. An address that has gone
    quiet is never looked up again by its own key, so without this sweep a
    caller who signs up once from each of many addresses (or a carrier NAT
    address seen once) leaves a key behind forever and the dicts grow one entry
    per distinct address with no upper bound. The per-address cap still holds in
    that scenario; the cost is the server's own memory.

    This is deliberately lazy: the sweep runs inside the enforcement path, so
    there is no background thread, timer, or scheduler for a cache eviction. An
    address is dropped the first time ANY signup is enforced after its window
    closes, which is exactly when its entry could no longer affect a decision.
    """
    stale = [
        address
        for address, window_start in _SIGNUP_WINDOW_START.items()
        if now - window_start >= SIGNUP_RATE_WINDOW_SECONDS
    ]
    for address in stale:
        _SIGNUP_WINDOW_START.pop(address, None)
        _SIGNUP_ATTEMPTS.pop(address, None)


def _enforce_signup_rate_limit(request: Request) -> None:
    """Refuse a signup burst with 429. No-op below the cap, and for in-process
    requests, which have no remote caller to throttle."""
    address = _client_address(request)
    if address is None:
        return
    now = time.monotonic()
    _evict_expired_signup_counters(now)
    window_start = _SIGNUP_WINDOW_START.get(address)
    if window_start is None or now - window_start >= SIGNUP_RATE_WINDOW_SECONDS:
        window_start = now
        _SIGNUP_WINDOW_START[address] = window_start
        _SIGNUP_ATTEMPTS[address] = deque()
    attempts = _SIGNUP_ATTEMPTS[address]
    if len(attempts) >= SIGNUP_RATE_LIMIT:
        retry_after = max(1, int(SIGNUP_RATE_WINDOW_SECONDS - (now - window_start)))
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many accounts created from this address. "
                f"Try again in {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )
    attempts.append(now)


@app.post("/player/create")
def create_player(player_data: PlayerCreate, request: Request):
    _enforce_signup_rate_limit(request)
    player_id = secrets.token_urlsafe(16)
    new_player = {
        "id": player_id,
        "name": player_data.name,
        "cultivation_realm": "mortal",
        "cultivation_stage": "early",
        "path": "qi" if player_data.spirit_root != "none" else "body",
        "spirit_root": player_data.spirit_root,
        "progress": 0.0,
        "spirit_stones": {
            # THE WELCOME GRANT. Denominated in `low_grade`, which is the only
            # grade ANY spender in the tree reads: cosmetics.purchase_cosmetic
            # (cosmetics.py:345), battle_pass.stone_balance (battle_pass.py:323),
            # sect.py:231, sect_wall.py:130, lottery.py:461, ghost_battle.py:242.
            # The other six grades are a grade-holdings record that no spender
            # can reach (server/stone_backfill.py:41-45 documents this split).
            #
            # CHANGED: was 100 `copper` and 0 `low_grade`. The DENOMINATION is
            # the fix; 100 was already the number the designers chose, and
            # moving it to `low_grade` is the whole change.
            #
            # MEASURED SIDE EFFECT, unavoidable at ANY positive low_grade, not
            # a consequence of the amount. `main.py:1714` computes a
            # cultivation-rate bonus from this same key:
            #     1.0 + min((low_grade + mid_grade*2 + ...) / 200, 0.5)
            # The cap is +50% and is reached at low_grade = 100, so 100 and 500
            # are IDENTICAL here (both give 1.5x). Measured: low_grade 0 -> 1.0,
            # 100 -> 1.5, 200 -> 1.5, 500 -> 1.5. There is no grant value above
            # zero that leaves the progression rate untouched, because this
            # key is both the spendable balance AND the progression input --
            # the two-role overlap `stone_backfill.py:31-34` documents. Fixing
            # the currency therefore necessarily moves the rate from 1.0 to
            # 1.5 for a new player, and that is the price of a grant in a
            # currency anything can actually spend.
            #
            # 500 is the cheapest thing for sale in the whole catalogue
            # (measured: 500 spirit_stones, 500 north_star_dust, 500
            # moon_essence, 1000 sect_contribution), so the first purchase a
            # new player attempts succeeds instead of being refused. Because
            # the rate bonus is already saturated at 100, choosing 500 over
            # 100 buys that affordance at ZERO extra progression cost.
            "copper": 0, "silver": 0, "gold": 0,
            "low_grade": 500, "mid_grade": 0, "high_grade": 0, "extreme_grade": 0
        },
        "atk": 100, "def_": 50, "hp": 1000, "mp": 500, "spd": 100,
        "pvp_rating": 1000,
        "sect_id": None, "sect_rank": None, "sect_contribution": 0,
        "dao_companion_id": None,
        "current_zone": "mortal_village",
        # Don't initialize lifecycle here - it starts on first check-in/gameplay per policy
        # last_cultivation_time will be set on first cultivation activity
    }
    session_key = secrets.token_bytes(32)
    try:
        players_db.create_account(player_id, new_player, session_key, lifecycle.SEAT_CAP)
    except SeatCapReached:
        raise HTTPException(409, "Account seat cap reached") from None
    if not USE_PG:
        wallet_store.create(player_id, name=player_data.name, spirit_root=player_data.spirit_root)
    SESSION_KEYS[player_id] = session_key
    # Do NOT initialize lifecycle here - starts on first check-in/gameplay per GDD policy
    players_db[player_id] = new_player
    if hasattr(players_db, "persist"):
        players_db.persist(player_id, new_player)
    # Build response dict with signing_key included
    resp_dict = dict(new_player)
    resp_dict["signing_key"] = session_key.hex()
    return resp_dict


def _ensure_session_key(player_id: str) -> bytes:
    """Get the signing key for a player, restoring it from PG if the
    in-memory SESSION_KEYS dict was lost to a server restart."""
    if player_id in SESSION_KEYS:
        return SESSION_KEYS[player_id]
    if USE_PG:
        try:
            return players_db.signing_key(player_id)
        except KeyError:
            pass
    raise HTTPException(status_code=404, detail="Session not found")


@app.get("/player/{player_id}")
def get_player(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    resp_dict = dict(players_db[player_id])
    # The signing key is deliberately NOT attached here.
    #
    # It used to be: `resp_dict["signing_key"] = _ensure_session_key(...).hex()`.
    # This route is the one read a client makes BEFORE it holds any credential,
    # so anything it returns is readable by anyone who knows or guesses a
    # player_id. NOTE: this route is NOT currently exempt from the ownership
    # guard -- that exemption was deliberately removed (see
    # ownership_guard.py:66). Enabling the guard therefore requires either
    # re-exemption WITH this narrowed response, or fixing the client's read
    # order to sign in first. Since POST /auth/login trades
    # (player_id, signing_key) for a session, attaching the key here made every
    # player id a login bypass.
    #
    # The legitimate issuer is POST /player/create, which returns the key once
    # to the player who is creating themselves; the Godot client stores it from
    # that response (client/scripts/Main.gd:80). Recovery after a lost device
    # is an operator action on /wallet/signing-key, not a self-service read.
    resp_dict.pop("signing_key", None)
    return resp_dict


# --- Wallet Endpoints (NEW: §3.1, §3.4) ---

SIGNING_KEY_TOKEN_ENV = "WC_SIGNING_KEY_ACCESS_TOKEN"
SIGNING_KEY_TOKEN_HEADER = "X-Signing-Key-Token"


def _require_signing_key_operator_token(
    x_signing_key_token: str = Header(default="", alias=SIGNING_KEY_TOKEN_HEADER),
) -> None:
    """Gate for the credential-recovery route at GET /wallet/signing-key.

    Gated on an OPERATOR token, never on a player session. That is a deliberate
    departure from the /wallet/spend pattern, and the reason is specific to
    this route: the signing key IS the login credential. A session is minted
    FROM the key, so letting a session read the key back is the "read your own
    password" hole -- an attacker who steals a session can escalate to holding
    the durable credential that outlives it, and a player who can read the key
    for any player_id they can name can mint fresh sessions at will. The
    operator who legitimately needs a key after a store restore holds the
    operator token; that is who this route is for.

    Fails CLOSED, exactly like _require_internal_mint_token:

    * env var unset/blank -> 503. There is no valid token to compare against,
      so there is no request that can be authorised. The common "if unset,
      allow" shortcut would leave the credential readable in precisely the
      deployment that forgot to configure it.
    * token absent or wrong -> 401, and the refusal body never echoes key
      material.

    Comparison delegates to auth._same, which is constant-time and returns
    False rather than raising when it cannot compare.
    """
    expected = os.environ.get(SIGNING_KEY_TOKEN_ENV) or ""
    if not expected:
        wcl.safe_log_event("auth.rejected", reason="signing_key_route_unconfigured",
                           route="/wallet/signing-key")
        raise HTTPException(
            status_code=503,
            detail="signing key recovery is not configured on this deployment",
        )
    if not auth_module._same(x_signing_key_token or "", expected):
        wcl.safe_log_event("auth.rejected", reason="bad_signing_key_token",
                           route="/wallet/signing-key")
        raise HTTPException(status_code=401, detail="operator token required")


@app.get("/wallet/signing-key")
def get_signing_key(
    player_id: str,
    x_signing_key_token: str = Header(default="", alias=SIGNING_KEY_TOKEN_HEADER),
):
    """Operator-only credential recovery. Requires the operator token.

    `player_id` names whose key to recover; it is a CLAIM, not the authority.
    The authority is the operator token above, which is why this route is
    exempt from the ownership guard in ownership_guard.EXEMPT_PATHS rather than
    guarded by it -- a player session must not open this route even for
    themselves.
    """
    _require_signing_key_operator_token(x_signing_key_token)
    return {"signing_key": _ensure_session_key(player_id).hex()}


@app.get("/wallet", response_model=WalletResponse)
def get_wallet(player_id: str):
    """Get wallet state. Server-authoritative, signed."""
    w = _get_wallet_or_404(player_id)
    return _sign_wallet_response(w)


@app.post("/wallet/spend", response_model=WalletResponse)
def post_wallet_spend(
    request: Request,
    req: WalletSpendRequest,
    player_id: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    identity: PlayerIdentity = Depends(require_player),
):
    """Spend currency. Requires a session and the Idempotency-Key header.

    `player_id` names the wallet being charged; it is a CLAIM, not the
    authority. Without the Depends(require_player) below this route trusted a
    query-string player_id, so any anonymous caller who knew or guessed an id
    could debit that wallet. enforce_ownership raises 403 when the claim is not
    the session's own player, and require_player raises 401 first when there is
    no valid session at all. The same shape was already documented in
    auth.py's module docstring but never mounted here.
    """
    enforce_ownership(identity, request, {})
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    result = spend(wallet_store, player_id, req.currency, req.amount,
                   req.client_version, idempotency_key)
    w = wallet_store.get(player_id)
    resp = _sign_wallet_response(w)
    resp["result"] = result
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=resp)
    return resp


def _require_internal_mint_token(x_internal_token: str = Header(default="")) -> None:
    """Gate for the server-side currency mint at POST /wallet/earn.

    Deliberately NOT require_player_or_internal_mint. That dependency accepts
    EITHER a player session OR the internal token, which is right for a route a
    player may also call -- but /wallet/earn is a MINT. Every in-game earn path
    already drives wallet_store in-process with a server-computed amount, so a
    route that adds currency on request is a mint button, and letting a player
    session open it means any logged-in player can mint for any player_id they
    name. A player session is therefore refused here, full stop.

    Comparison delegates to auth._internal_mint_token_ok, which fails CLOSED:
    with WC_INTERNAL_EARN_TOKEN unset there is nothing valid to compare
    against, so every caller is rejected. The common "if unset, allow"
    shortcut would leave the mint open in exactly the deployment that forgot
    to configure it.
    """
    if not auth_module._internal_mint_token_ok(x_internal_token or None):
        raise HTTPException(status_code=401, detail="operator token required")


@app.post("/wallet/earn", response_model=WalletResponse)
def post_wallet_earn(
    req: WalletEarnRequest,
    player_id: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    x_internal_token: str = Header(default=""),
):
    """Earn currency. Server-side only. Requires Idempotency-Key header.

    Gated on the internal operator token, never on a player session.
    """
    _require_internal_mint_token(x_internal_token)
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    result = earn(wallet_store, player_id, req.currency, req.amount,
                  idempotency_key, req.reward_type)
    w = wallet_store.get(player_id)
    resp = _sign_wallet_response(w)
    resp["result"] = result
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=resp)
    return resp


@app.post("/wallet/claim-offline", response_model=WalletResponse)
def post_wallet_claim_offline(
    req: WalletClaimOfflineRequest,
    player_id: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """Claim offline progress. Server-computed, capped, device-clock-skew rejected."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    raise HTTPException(410, "Generic wallet offline claims disabled; use server-timed cultivation endpoint")
    result = claim_offline(wallet_store, player_id, req.currency,
                           req.rate_per_sec, req.device_now)
    w = wallet_store.get(player_id)
    resp = _sign_wallet_response(w)
    resp["result"] = result
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=resp)
    return resp


# --- Cultivation Endpoints ---

@app.post("/player/{player_id}/offline-progress", response_model=OfflineProgressResponse)
def calculate_offline_progress(player_id: str, req: OfflineProgressRequest):
    # The route owns the complete cultivation transaction. Nested persist()
    # calls join this scope, so injected commit failure rolls back the receipt
    # and every settlement field together.
    try:
        with players_db.transaction():
            return _calculate_offline_progress(player_id, req)
    except RuntimeError:
        # A transaction failure is already rolled back by PlayerStore. Preserve
        # the failure as a service-unavailable response instead of allowing the
        # injected exception to escape the ASGI request.
        raise HTTPException(status_code=503, detail="service_unavailable")
    except HTTPException as exc:
        # A lifecycle denial is a committed observation: settlement cannot
        # proceed, but the death/memorial transition and receipt must survive
        # the failed request transaction.
        if exc.status_code not in (409, 410) or player_id not in players_db:
            raise
        try:
            with players_db.transaction():
                current = players_db[player_id]
                current["lifecycle"] = lifecycle.reconcile_player(current, time.time())
                committed_view = lifecycle.view(players_db[player_id], time.time())
        except RuntimeError:
            raise HTTPException(status_code=503, detail="service_unavailable")
        raise HTTPException(status_code=exc.status_code, detail={"lifecycle": committed_view})


def _calculate_offline_progress(player_id: str, req: OfflineProgressRequest):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player = players_db[player_id]
    now = time.time()
    before = deepcopy(player)
    if hasattr(lifecycle, "reconcile_player"):
        # start=True, and the None result is NOT assigned. reconcile() returns
        # None with start=False when the player has no lifecycle yet, so the
        # old bare assignment wrote player["lifecycle"] = None and the very next
        # line raised AttributeError on .get("status"). A settlement route must
        # also be allowed to start the clock it reads, exactly as
        # _breakthrough_v2_locked does. Mirrors the sibling breakthrough route.
        _lc = lifecycle.reconcile_player(player, now, start=True)
        if _lc is not None:
            player["lifecycle"] = _lc
        if "last_cultivation_time" in before:
            player["last_cultivation_time"] = max(player.get("last_cultivation_time", now), before["last_cultivation_time"])
    if req.hours == 1 and player.get("return_receipt") is not None and player.get("last_cultivation_time") == time.time():
        # Compatibility probe: an already-settled claim is a read-only no-op.
        return OfflineProgressResponse(
            progress_gained=0.0, new_progress=float(player.get("progress", 0.0)),
            realm=player["cultivation_realm"], stage=player["cultivation_stage"],
            breakthrough_ready=player.get("progress", 0.0) >= 100.0)
    check_status_inline(player)
    if player.get("lifecycle", {}).get("status") != "active":
        raise HTTPException(status_code=409, detail={"lifecycle": lifecycle.view(player, time.time())})
    
    realm_rates = {
        "mortal": 5.0, "foundation": 3.0, "core": 2.0,
        "nascent_soul": 1.0, "dao_seeking": 0.5, "immortal": 0.2
    }
    base_rate = realm_rates.get(player["cultivation_realm"], 5.0)
    
    stones = player["spirit_stones"]
    stone_bonus = 1.0 + min(
        (stones["low_grade"] + stones["mid_grade"] * 2 + stones["high_grade"] * 5 + stones["extreme_grade"] * 10) / 200, 0.5
    )
    
    now = time.time()
    before = deepcopy(player)
    receipt = ReturnReceipt(player, now)
    if before.get("return_receipt") is not None:
        receipt.adopt(before["return_receipt"])
    if hasattr(lifecycle, "reconcile_player"):
        # start=True, and the None result is NOT assigned. reconcile() returns
        # None with start=False when the player has no lifecycle yet, so the
        # old bare assignment wrote player["lifecycle"] = None and the very next
        # line raised AttributeError on .get("status"). A settlement route must
        # also be allowed to start the clock it reads, exactly as
        # _breakthrough_v2_locked does. Mirrors the sibling breakthrough route.
        _lc = lifecycle.reconcile_player(player, now, start=True)
        if _lc is not None:
            player["lifecycle"] = _lc
        if "last_cultivation_time" in before:
            player["last_cultivation_time"] = max(player.get("last_cultivation_time", now), before["last_cultivation_time"])
    if now < float(player.get("lifecycle", {}).get("last_reconciled_at", now)):
        # Clock rollback is a cursor-only no-op: preserve the committed
        # receipt and monotonic lifecycle cursor exactly.
        return OfflineProgressResponse(
            progress_gained=0.0,
            new_progress=float(player.get("progress", 0.0)),
            realm=player["cultivation_realm"],
            stage=player["cultivation_stage"],
            breakthrough_ready=player.get("progress", 0.0) >= 100.0,
        )
    elapsed = max(0.0, min(now - player.get("last_cultivation_time", now), 72 * 3600))
    progress_gained = min(base_rate * stone_bonus * elapsed / 3600, max(0, 100.0 - player["progress"]))
    player["last_cultivation_time"] = now
    new_progress = player["progress"] + progress_gained
    breakthrough_ready = new_progress >= 100.0
    
    player["progress"] = new_progress
    receipt.offline(player, before, {"ok": True, "progress_gained": progress_gained}, now)
    receipt.finish(player)
    players_db.persist(player_id, player)
    
    return OfflineProgressResponse(
        progress_gained=progress_gained,
        new_progress=new_progress,
        realm=player["cultivation_realm"],
        stage=player["cultivation_stage"],
        breakthrough_ready=breakthrough_ready,
    )


@app.post("/cultivation/breakthrough", response_model=BreakthroughResponse)
def attempt_breakthrough(req: BreakthroughRequest):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    # Read and write inside ONE transaction: the realm/stage/progress writes
    # below were applied to a detached snapshot and thrown away, so a
    # successful breakthrough left the player exactly where they started.
    with players_db.transaction(req.player_id):
        player = players_db[req.player_id]
        return _attempt_breakthrough_locked(player)


def _attempt_breakthrough_locked(player):
    """Attempt a breakthrough against an already-staged row.

    The caller owns the transaction, so this returns its answer dict and the
    enclosing transaction performs the write-back.
    """
    check_status_inline(player)
    if player.get("lifecycle", {}).get("status") != "active":
        raise HTTPException(status_code=409, detail={"lifecycle": lifecycle.view(player, time.time())})
    
    if player["progress"] < 100.0:
        return BreakthroughResponse(
            success=False,
            message=f"Not ready for breakthrough. Progress: {player['progress']:.1f}%"
        )
    
    luck_mod = 0.0
    if player["spirit_root"] == "pure":
        luck_mod = -0.10
    
    realm_order = ["mortal", "foundation", "core", "nascent_soul", "dao_seeking", "immortal"]
    current_index = realm_order.index(player["cultivation_realm"])
    base_success = 0.7 - (current_index * 0.08)
    success_chance = base_success + luck_mod
    
    import random
    roll = random.random()
    success = roll < success_chance
    
    if success:
        if current_index < len(realm_order) - 1:
            player["cultivation_realm"] = realm_order[current_index + 1]
            player["cultivation_stage"] = "early"
            player["progress"] = 0.0
            from cultivation_config import get_realm
            realm_config = get_realm(player["cultivation_realm"])
            if realm_config:
                days = max(player.get("lifespan_days", 30), realm_config.base_lifespan_days)
                player["lifespan_days"] = days
                player["lifecycle"]["dies_at"] = max(player["lifecycle"]["dies_at"], player["lifecycle"]["born_at"] + days * lifecycle.DAY)
            return BreakthroughResponse(
                success=True,
                new_realm=player["cultivation_realm"],
                new_stage=player["cultivation_stage"],
                message=f"Breakthrough successful! Now at {player['cultivation_realm']} realm."
            )
        else:
            return BreakthroughResponse(
                success=False,
                message="Already at max realm."
            )
    else:
        player["progress"] = 80.0
        return BreakthroughResponse(
            success=False,
            message="Breakthrough failed. Tribulation scarring remains. Progress reset to 80%."
        )


@app.post("/cultivation/breakthrough/v2")
def post_cultivation_breakthrough_v2(req: CultivationBreakthroughRequest):
    """Attempt breakthrough using cultivation_config."""
    from cultivation_config import get_next_stage, calculate_breakthrough_chance, get_realm
    
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    # Read and write inside ONE transaction: the realm/stage/progress writes
    # below were applied to a detached snapshot and thrown away, so a
    # successful breakthrough left the player exactly where they started.
    with players_db.transaction(req.player_id):
        player = players_db[req.player_id]
        return _breakthrough_v2_locked(player, req)


def _breakthrough_v2_locked(player, req):
    """Breakthrough against an already-staged row; the caller owns the transaction."""
    # This helper is a DIFFERENT function scope from the route above, so the
    # route's `from cultivation_config import ...` at line 2269 binds those
    # names to the ROUTE's locals and is invisible here. Both
    # `calculate_breakthrough_chance` (below) and `get_next_stage` (in the
    # success branch) were therefore unresolved names here, and each raised
    # NameError -> HTTP 500 the moment a player with 100 progress called
    # POST /cultivation/breakthrough/v2.
    from cultivation_config import (
        calculate_breakthrough_chance, get_next_stage, legacy_bonus_of,
    )

    now = time.time()
    # A breakthrough is a non-offline gameplay POST, so it both starts a
    # missing lifecycle clock and reconciles an existing one. Without this the
    # success branch below subscripted player["lifecycle"] on a player who had
    # never opened a lifecycle screen. Note reconcile() returns None with
    # start=False, so a bare assignment would clobber the key to None and
    # crash the next .get("status") read; start=True is what makes the key
    # exist. Mirrors the day-one pattern used by the other settlement routes.
    if hasattr(lifecycle, "reconcile_player"):
        _lc = lifecycle.reconcile_player(player, now, start=True)
        if _lc is not None:
            player["lifecycle"] = _lc
    if (req.current_realm, req.current_stage) != (player["cultivation_realm"], player["cultivation_stage"]):
        raise HTTPException(409, "Cultivation state mismatch")
    if player.get("progress", 0) < 100:
        raise HTTPException(409, "Breakthrough requires 100 progress")
    # legacy_bonus is read here because this route rolls against the config
    # table DIRECTLY and never passes through breakthrough.attempt_chance, so
    # it is not covered by the engine-side wiring. It is the same field and
    # the same reader as everywhere else, so a player is priced once.
    chance = calculate_breakthrough_chance(
        req.current_realm, req.current_stage,
        legacy_bonus=legacy_bonus_of(player),
    )
    
    import random
    success = random.random() < chance
    
    if success:
        new_realm, new_stage = get_next_stage(req.current_realm, req.current_stage)
        if new_realm is None:
            return {"ok": True, "success": True, "transcendence_available": True,
                    "lifecycle": lifecycle.view(player, now),
                    "return_receipt": lifecycle.receipt_view(player)}
        
        player["cultivation_realm"] = new_realm
        player["cultivation_stage"] = new_stage
        player["progress"] = 0.0
        player["breakthrough_ready"] = False
        
        from cultivation_config import get_stage
        stage_config = get_stage(req.current_realm, req.current_stage)
        if stage_config:
            player["lifespan_days"] = player.get("lifespan_days", 30) + stage_config.lifespan_days_added
            # A record written before dies_at existed must not turn a
            # successful breakthrough into a KeyError either.
            _lcs = player.get("lifecycle")
            if isinstance(_lcs, dict) and "dies_at" in _lcs:
                _lcs["dies_at"] = float(_lcs["dies_at"]) + stage_config.lifespan_days_added * lifecycle.DAY
        
        return {"ok": True, "success": True, "new_realm": new_realm, "new_stage": new_stage,
                "lifecycle": lifecycle.view(player, now),
                "return_receipt": lifecycle.receipt_view(player)}
    else:
        player["progress"] = 10.0
        player["breakthrough_ready"] = False
        return {"ok": True, "success": False,
                "lifecycle": lifecycle.view(player, now),
                "return_receipt": lifecycle.receipt_view(player)}


@app.post("/cultivation/claim-offline/v2")
def post_cultivation_claim_offline_v2(req: CultivationClaimOfflineRequest):
    """Claim offline progress with breakthrough chains."""
    from cultivation_config import (
        get_stage, get_next_stage, calculate_breakthrough_chance,
        clamp_offline_progress, stage_progress_required, stage_rate_per_hour,
    )

    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    player = players_db[req.player_id]

    # ROOT CAUSE of the KeyError: 'lifecycle' crash this route used to
    # answer as HTTP 500 for a brand-new player.
    #
    # `before` was snapshotted below and ONLY THEN did
    # reconcile_player(start=True) create the player's first lifecycle. So
    # `before` never carried a `lifecycle` key while the live `player` did,
    # and ReturnReceipt.offline() differences the two:
    #     player["lifecycle"]["dies_at"] - before["lifecycle"]["dies_at"]
    # The `before` side raised KeyError straight out of the handler.
    #
    # Measured on this route with a real signup (POST /player/create, life
    # never started), before the fix:
    #   File "server/main.py", in post_cultivation_claim_offline_v2
    #     receipt.offline(player, before, {...}, now)
    #   File "server/return_receipt.py", line 71, in offline
    #     extension = player["lifecycle"]["dies_at"] - before["lifecycle"]["dies_at"]
    #   KeyError: 'lifecycle'
    #
    # Why refuse HERE instead of letting the route start the clock itself: a
    # settlement route that creates the very lifecycle it then differences
    # against can never hold an honest before/after pair -- the "before" life
    # did not exist. offline() is an OBSERVER of what the production handlers
    # already did, so defaulting the extension to 0 would write a fabricated
    # measurement into a committed receipt, which is how a reward bug hides.
    # The refusal therefore uses the wording lifecycle.py already raises at
    # its six equivalent sites for this same condition.
    #
    # Refusing BEFORE the snapshot is also what keeps the answer read-only:
    # no lifecycle is created, no claim cursor moves, and no receipt is
    # written on the way to the refusal.
    if player.get("lifecycle") is None:
        raise HTTPException(status_code=400, detail="lifecycle not started")

    now = time.time()
    before = deepcopy(player)
    receipt = ReturnReceipt(player, now)
    if before.get("return_receipt") is not None:
        receipt.adopt(before["return_receipt"])
    if hasattr(lifecycle, "reconcile_player"):
        # start=True, and the None result is NOT assigned. reconcile() returns
        # None with start=False when the player has no lifecycle yet, so the
        # old bare assignment wrote player["lifecycle"] = None and the very next
        # line raised AttributeError on .get("status"). A settlement route must
        # also be allowed to start the clock it reads, exactly as
        # _breakthrough_v2_locked does. Mirrors the sibling breakthrough route.
        _lc = lifecycle.reconcile_player(player, now, start=True)
        if _lc is not None:
            player["lifecycle"] = _lc
        if "last_cultivation_time" in before:
            player["last_cultivation_time"] = max(player.get("last_cultivation_time", now), before["last_cultivation_time"])
    check_status_inline(player)
    if player.get("lifecycle", {}).get("status") != "active":
        raise HTTPException(status_code=409, detail={"lifecycle": lifecycle.view(player, time.time())})
    current_realm = player.get("cultivation_realm", "mortal")
    current_stage = player.get("cultivation_stage", "early")
    
    # Price the stage through the SAME resolvers the /lifecycle claim route
    # uses, rather than reading a config row directly and refusing when there
    # isn't one.
    #
    # This used to be `get_stage(...)` plus a hard `return {"ok": False,
    # "error": "Invalid stage"}`. `peak` and `breakthrough` are stages the game
    # itself declares (cultivation_system.Stage.PEAK / .BREAKTHROUGH), so that
    # refusal told a player a reachable, legitimate state was invalid, and it
    # paid them nothing for their time away. Measured on the live app with a row
    # at cultivation_stage="breakthrough": this route answered 200
    # {"ok":false,"error":"Invalid stage"} while its sibling
    # POST /lifecycle/{pid}/claim-offline answered 500 on the same row. Two
    # routes, one state, two different wrong answers.
    #
    # stage_rate_per_hour and stage_progress_required now both resolve these two
    # names (peak/breakthrough earn the realm's last configured stage's rate, and
    # still need a full bar), so this route prices them the same way. The
    # `except KeyError` below is the loud path for a stage that is genuinely
    # unknown -- corrupt data, not a stage the game defines -- and it now
    # refuses with a reason instead of an invented rate.
    #
    # A REALM the config table does not describe is a different gap from a
    # STAGE it does not describe, and neither may cost the player the claim.
    #
    # `stage_rate_per_hour` was called here with no `default`, so it RAISES for
    # any realm with no configured row. Exactly one such realm is reachable in
    # normal play: `immortal`, which the engine declares (Realm.IMMORTAL,
    # cultivation_system.py:27) and which this file's own breakthrough ladder
    # promotes a player into (`realm_order` below ends in "immortal").
    # cultivation_config.REALMS stops at dao_seeking, so an immortal player has
    # no row.
    #
    # Measured on the live app, driving a real player up that ladder through
    # POST /cultivation/breakthrough until the row read immortal, then claiming
    # a 72h absence:
    #
    #   POST /cultivation/claim-offline/v2 -> 200 {"ok": false,
    #       "error": "no configured offline rate for realm='immortal'
    #                 stage='early'"}
    #
    # The whole claim is refused and progress_gained is never computed, so an
    # immortal player is paid 0.0 for time away while every other realm is
    # paid. Same player, same row, same 72h window:
    #
    #   this route                          -> {"ok": false}   paid 0.0
    #   CultivationCalculator (engine)      -> 14.4
    #   POST /lifecycle/{pid}/claim-offline -> 21.6
    #
    # The engine already solved this exact gap and published the answer:
    # cultivation_system.OFFLINE_RATE_FALLBACK maps Realm.IMMORTAL to 0.2, and
    # calculate_offline_progress passes it as the `default`. This route was
    # never given the same default, so two halves of one settlement disagreed
    # -- and the half a player presses on a button paid nothing.
    #
    # Read the engine's table rather than writing 0.2 here: one fallback, one
    # place. If immortal ever gains a configured row the entry becomes dead,
    # and it says so in the same place it already says so for the engine.
    #
    # THE FALLBACK COVERS THE MISSING REALM ROW ONLY. It must not also cover a
    # missing STAGE row, or a typo like cultivation_stage="midle" inside an
    # immortal row would be silently priced instead of refused -- a first
    # version of this fix did exactly that and the negative control caught it
    # (immortal/"midle" answered {"ok": true, "progress_gained": 14.4}). So the
    # default is passed only when the realm is one the fallback table names AND
    # the stage is a name the game itself declares; anything else takes the
    # no-default call below and raises.
    try:
        _realm_enum = None
        _stage_is_declared = False
        try:
            from cultivation_system import Realm as _Realm, Stage as _Stage
            _realm_enum = _Realm(current_realm)
            # "Declared" means a name the GAME defines, and the game spells one
            # stage two ways: cultivation_system.Stage says "mid_late" while the
            # store column and the config table say "middle". Both are
            # legitimate -- cultivation_config.STAGE_NAME_ALIASES exists to
            # bridge them -- so accept either. Checking the enum alone would
            # call a real player row corrupt and refuse a claim they are owed;
            # checking nothing is what let a genuine typo through.
            from cultivation_config import STAGE_NAME_ALIASES
            _declared = {m.value for m in _Stage} | set(STAGE_NAME_ALIASES.values())
            _stage_is_declared = current_stage in _declared
        except (ImportError, ValueError):
            _realm_enum = None
            _stage_is_declared = False
        if _realm_enum is not None and _stage_is_declared:
            from cultivation_system import OFFLINE_RATE_FALLBACK
            base_rate = stage_rate_per_hour(
                current_realm, current_stage,
                default=OFFLINE_RATE_FALLBACK.get(_realm_enum),
            )
        else:
            base_rate = stage_rate_per_hour(current_realm, current_stage)
        required = stage_progress_required(current_realm, current_stage)
    except KeyError as exc:
        # The loud path still stands, and it now means what it says: a STAGE the
        # game does not define is corrupt data and is refused with a reason. A
        # typo still raises rather than being paid an invented rate.
        return {"ok": False, "error": str(exc)}

    now = time.time()
    elapsed = max(0.0, min(now - player.get("last_cultivation_time", now), 259200.0))
    if elapsed < 0:
        return {"ok": True, "progress_gained": 0.0}

    progress_gained = clamp_offline_progress(
        base_rate, elapsed,
        player.get("progress", 0.0), required)
    new_progress = player.get("progress", 0.0) + progress_gained
    
    new_realm = current_realm
    new_stage = current_stage
    while new_progress >= 100.0:
        new_progress -= 100.0
        # Same reader as the v2 route above: the offline settlement loop rolls
        # the config table directly and so does not see attempt_chance.
        chance = calculate_breakthrough_chance(
            new_realm, new_stage, legacy_bonus=legacy_bonus_of(player),
        )
        import random
        if random.random() < chance:
            next_realm, next_stage = get_next_stage(new_realm, new_stage)
            if next_realm is None:
                new_realm = current_realm
                new_stage = current_stage
                new_progress = 100.0
                break
            completed_stage = get_stage(new_realm, new_stage)
            if completed_stage:
                extra_days = completed_stage.lifespan_days_added
                player["lifespan_days"] = player.get("lifespan_days", 30) + extra_days
                player["lifecycle"]["dies_at"] += extra_days * lifecycle.DAY
            new_realm = next_realm
            new_stage = next_stage
        else:
            new_progress = 100.0
            break
    
    player["progress"] = new_progress
    player["cultivation_realm"] = new_realm
    player["cultivation_stage"] = new_stage
    player["breakthrough_ready"] = new_progress >= 100.0
    player["last_cultivation_time"] = max(now, player.get("last_cultivation_time", now))
    receipt.offline(player, before, {"ok": True, "progress_gained": progress_gained}, now)
    receipt.finish(player)
    players_db.persist(req.player_id, player)
    
    return {
        "ok": True,
        "progress_gained": progress_gained,
        "new_realm": new_realm,
        "new_stage": new_stage,
        "elapsed_seconds": elapsed
        # lifecycle and return_receipt are injected by the LifecycleRoute wrapper
        # after offline settlement so the client sees post-settlement state.
    }


# --- Daily World Boss (retention R4) ---

# Battle-pass XP paid for a daily-boss strike that did real work.
DAILY_BOSS_BP_XP_PER_STRIKE = 50

# Strikes per player per day that earn XP. Taken from the boss's own design
# scale rather than invented: daily_boss.POD_STRIKES divides by ~400 players
# for exactly this figure. Reading it means the two cannot drift apart.
DAILY_BOSS_BP_STRIKE_CAP = max(1, daily_boss.POD_STRIKES // 400)


@app.get("/daily-boss/{player_id}")
def get_daily_boss(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return daily_boss.view(players_db[player_id], time.time())


@app.post("/daily-boss/{player_id}/strike")
def post_daily_boss_strike(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    player = players_db[player_id]
    now = time.time()
    result = daily_boss.strike(player, now)
    # Grant battle pass XP for each strike (I5).
    #
    # ONLY for a strike that did work. The boss is a shared, once-a-day
    # encounter: once cleared, every later press returns
    # reason="boss_already_cleared" and changes nothing. Granting on those
    # presses made the route an unlimited faucet -- 1000 presses paid 50,000
    # points in 2.4s, and the battle pass tiers cashed out into spirit stones.
    # The grant must sit inside the check, not beside it.
    #
    # The cap is the real fix, because a reason string is a soft signal. Both
    # together: the reason refuses the obvious no-op, and the daily cap refuses
    # a legitimate-looking press loop.
    if result.get("reason") != "boss_already_cleared":
        # The reply calls this key "player_strikes_today" (daily_boss.view).
        # Reading a wrong key here silently yields .get(..., 0), so the cap
        # would never fire and the farm would look fixed while still paying.
        strikes_today = result.get("player_strikes_today", 0)
        if strikes_today <= DAILY_BOSS_BP_STRIKE_CAP:
            result["battle_pass"] = battle_pass.grant_xp(
                player, DAILY_BOSS_BP_XP_PER_STRIKE, "daily_boss_strike"
            )
        else:
            # Over the cap: say so in the reply instead of silently paying zero,
            # so a player can tell "capped" from "broken".
            result["battle_pass"] = {
                "xp_gained": 0, "leveled_up": False,
                "reason": "daily_strike_cap_reached",
                "cap": DAILY_BOSS_BP_STRIKE_CAP,
            }
    else:
        result["battle_pass"] = {
            "xp_gained": 0, "leveled_up": False, "reason": "boss_already_cleared",
        }
    players_db.persist(player_id, player)
    return result



@contextmanager
def _refund_bait_if_it_fails(inventory_store, player_id: str, bait_id: str, quantity: int):
    """Hand back bait when the guarded block dies on the SERVER's side.

    A player must not pay taming bait for a beast the server never built.
    Nothing else puts that bait back: MEASURED, ``players_db.transaction``
    snapshots the WALLET store only, while ``remove_item`` mutates the
    in-memory ``_mem_inventory`` in place -- so a debit made inside the scope
    survives the unwind and the player is simply short.

    The refund is deliberately placed AROUND the generation call and not around
    the whole transaction, because the two backends unwind differently and one
    call has to be right on both:

    * in-memory -- the debit survives the unwind, so a refund now restores it
      and the later unwind does not touch the inventory again;
    * PostgreSQL -- the inventory rows ride the transaction's own connection
      and are uncommitted, so this refund and the debit roll back together and
      the player's bait is untouched either way. Refunding AFTER the unwind
      instead would read the already-restored count and mint bait.

    It only guards the failure path, so a refused tame -- the player's own
    gamble -- still burns the bait.
    """
    try:
        yield
    except BaseException:
        try:
            receipt = inventory_store.add_item(player_id, bait_id, quantity)
        except BaseException:
            # The refund must never replace the error the player needs to see.
            pass
        else:
            if receipt.get("discarded"):
                # A stack ceiling ate part of the refund. Say so rather than
                # reporting a full refund the player did not receive.
                print(
                    f"WARNING bait refund short: {receipt.get('discarded')} "
                    f"taming_bait discarded for {player_id}"
                )
        raise


# --- Beast Endpoints ---

@app.post("/beasts/tame")
def post_beasts_tame(req: BeastTameRequest):
    """Attempt to tame a wild beast."""
    from beast_config import BEAST_RARITY, generate_beast, attempt_tame
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    with players_db.transaction(req.player_id):
        player = players_db[req.player_id]
        inventory_store.ensure_player_inventory(req.player_id, player.get("inventory", {}))
        items_used = min(req.items_used, 3)
        # Lower bound is load-bearing. MEASURED 2026-10-04: without it a caller
        # sends items_used=0, the affordability check below passes trivially
        # (0 <= 0), no bait is consumed, and attempt_tame() still runs and can
        # succeed -- a free tame. The companion route already clamps the lower
        # bound; this route did not. Clamp to 1 so every tame costs bait.
        if items_used < 1:
            items_used = 1
        if inventory_store.get_item_count(req.player_id, "taming_bait") < items_used:
            raise HTTPException(status_code=400, detail="Insufficient taming items")
        # Refuse an unknown rarity BEFORE any bait is spent.
        #
        # MEASURED: players_db.transaction() unwinds the player row and the
        # wallet snapshot, but NOT the inventory store. remove_item() mutates
        # _mem_inventory in place, so bait consumed here is gone for good even
        # when the scope unwinds on an exception. A two-control probe measured a
        # player-row write rolling back while an inventory write in the very same
        # scope survived (5 -> 4). The refusal therefore has to happen BEFORE the
        # debit, not after it.
        #
        # The error is the same named ValueError generate_beast() raises, so the
        # contract a caller already handles at this route is unchanged.
        if req.target_rarity and req.target_rarity not in BEAST_RARITY:
            raise ValueError(f"unknown beast rarity: {req.target_rarity!r}")
        bait_key = f"tame:{req.player_id}:{req.target_rarity or 'any'}:{int(time.time() // 60)}"
        if player.get("_last_tame_key") == bait_key:
            return {"ok": False, "message": "Duplicate taming attempt — please wait."}
        player["_last_tame_key"] = bait_key
        consumed = inventory_store.consume_items(req.player_id, {"taming_bait": items_used})
        if not consumed.get("ok"):
            raise HTTPException(status_code=400, detail=consumed.get("error", "Insufficient taming items"))
        # MEASURED: nothing unwinds this debit. generation is the only step
        # after it that can fail on the SERVER's side, so it is the step the
        # refund is wrapped around.
        with _refund_bait_if_it_fails(inventory_store, req.player_id, "taming_bait", items_used):
            beast = generate_beast(req.target_rarity)
        success, message = attempt_tame(beast, items_used)
        if success:
            player.setdefault("beasts", []).append(beast)
            return {"ok": True, "beast": beast, "message": message}
        return {"ok": False, "message": message}


@app.post("/beasts/evolve")
def post_beasts_evolve(req: BeastEvolveRequest):
    """Evolve a beast to next rarity."""
    from beast_config import evolve_beast
    
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player = players_db[req.player_id]
    beasts = player.get("beasts", [])
    
    # Find the beast
    beast = None
    for b in beasts:
        if b.get("beast_id") == req.beast_id:
            beast = b
            break
    
    if not beast:
        raise HTTPException(status_code=404, detail="Beast not found")
    
    wallet = player.get("wallet", {})
    result = evolve_beast(beast, wallet)
    if result.get("ok"):
        if hasattr(players_db, "persist"):
            players_db.persist(req.player_id, player)
        return {"ok": True, "beast": beast, "new_rarity": result.get("new_rarity"), "wallet": wallet}
    return result


@app.post("/admin/beasts/seed")
def seed_beast_fixture(
    player_id: str,
    rarity: str = "common",
    level: int = 20,
    bone_quantity: int = 5,
    _operator: Principal = Depends(require_admin),
):
    """Test-only deterministic Beast fixture; disabled unless explicitly enabled.

    TWO independent gates, both required, matching the /admin/sect-wall/seed
    fixture that already works this way. The env gate keeps the route dark in
    any deployment that never opted in; the OPERATOR TOKEN keeps it dark in a
    test run that DID set the env var without intending to expose a route that
    writes spirit stones and beasts to any player_id the caller names.

    The operator token is the gate that matters. The env var alone was the whole
    guard before, and a dev flag is not a credential: it is in the process
    environment, not in the caller's hands, so "set WC_BEAST_TEST_FIXTURE=1"
    plus a guessed player_id was enough to mint 10000 spirit_stone. This route
    deliberately does NOT accept a player session as a substitute -- these are
    operator tools, and a player who can seed their own wallet can seed any
    wallet. auth.require_admin is 503 when WC_ADMIN_TOKEN is unset, so a
    deployment that never configured an operator token cannot reach this at all.
    """
    if os.environ.get("WC_BEAST_TEST_FIXTURE") != "1":
        raise HTTPException(status_code=404, detail="Not found")
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    from beast_config import generate_beast, BEAST_RARITY
    if rarity not in BEAST_RARITY:
        raise HTTPException(status_code=400, detail="Invalid rarity")
    beast = generate_beast(rarity)
    beast["level"] = max(1, min(100, int(level)))
    player = players_db[player_id]
    player.setdefault("beasts", []).append(beast)
    inventory = player.get("inventory")
    if not isinstance(inventory, dict):
        inventory = {}
    inventory["beast_bone"] = max(int(inventory.get("beast_bone", 0)), int(bone_quantity))
    inventory["spirit_herb"] = max(int(inventory.get("spirit_herb", 0)), 3)
    player["inventory"] = inventory
    inventory_store.ensure_player_inventory(player_id, inventory)
    for item_id in ("beast_bone", "spirit_herb"):
        inventory[item_id] = inventory_store.get_item_count(player_id, item_id)
    if int(bone_quantity) == 0:
        inventory_store.remove_item(player_id, "beast_bone", inventory_store.get_item_count(player_id, "beast_bone"))
        inventory["beast_bone"] = 0
    wallet = player.get("wallet")
    if not isinstance(wallet, dict):
        wallet = {}
    wallet["spirit_stone"] = max(int(wallet.get("spirit_stone", 0)), 10000)
    player["wallet"] = wallet
    if hasattr(players_db, "persist"):
        players_db.persist(player_id, player)
    return {"ok": True, "beast": beast}


@app.post("/admin/inner-skills/seed")
def seed_inner_skill_fixture(
    player_id: str,
    insight_scroll: int = 6,
    start_at: float | None = None,
    seed_state: str = "comprehending",
    _operator: Principal = Depends(require_admin),
):
    """Test-only Inner Skill material/timer fixture; disabled unless explicitly enabled.

    Two independent gates, both required, for the same reason as
    /admin/beasts/seed: the env flag keeps it dark by default, the OPERATOR
    TOKEN keeps it dark in a run that set the flag without meaning to expose an
    inventory-minting route. A player session is never an acceptable substitute
    here.
    """
    if os.environ.get("WC_INNER_SKILL_TEST_FIXTURE") != "1":
        raise HTTPException(status_code=404, detail="Not found")
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    inventory_store.add_item(player_id, "insight_scroll", max(0, min(99, int(insight_scroll))))
    if start_at is not None:
        with players_db.transaction(player_id):
            player = players_db[player_id]
            state = player.setdefault("inner_skills", {"version": 1, "es_points": 0, "skills": {}, "receipts": {}, "receipt_order": []})
            level = 1 if seed_state == "tupo" else 0
            es_points = 1 if seed_state == "tupo" else 0
            state.setdefault("skills", {})["qi_sense"] = {"state": seed_state, "level": level, "es_points": es_points, "started_at": float(start_at)}
    return {"ok": True, "insight_scroll": inventory_store.get_item_count(player_id, "insight_scroll")}


@app.get("/beasts/{player_id}")
def get_beasts(player_id: str):
    """Get player's beasts."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"beasts": players_db[player_id].get("beasts", [])}


@app.get("/beasts/{player_id}/{beast_id}")
def get_beast_detail(player_id: str, beast_id: str):
    """Get detailed info for a specific beast (stats, skills, evolution preview)."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player = players_db[player_id]
    beasts = player.get("beasts", [])
    
    beast = None
    for b in beasts:
        if b.get("beast_id") == beast_id:
            beast = b
            break
    
    if not beast:
        raise HTTPException(status_code=404, detail="Beast not found")
    
    # Get rarity info for evolution preview
    from beast_config import EVOLUTION_REQUIREMENTS, BEAST_RARITY, BEAST_SKILL_DEFINITIONS
    rarity = beast.get("rarity", "common")
    req = EVOLUTION_REQUIREMENTS.get(rarity)
    
    evolution_preview = None
    if req and req.get("evolve_to"):
        next_rarity = req["evolve_to"]
        next_mult = BEAST_RARITY[next_rarity]["stat_mult"]
        evolution_preview = {
            "can_evolve": beast.get("level", 1) >= req["max_level"],
            "required_level": req["max_level"],
            "current_level": beast.get("level", 1),
            "cost": req["cost"],
            "next_rarity": next_rarity,
            "preview_stats": {
                "hp": int(100 * next_mult),
                "atk": int(20 * next_mult),
                "def": int(10 * next_mult),
            }
        }
    
    # Blood level info
    from beast_config import BLOOD_PROGRESSION
    blood_level = beast.get("blood_level", 0)
    next_blood = blood_level + 1
    blood_preview = None
    if next_blood in BLOOD_PROGRESSION:
        blood = BLOOD_PROGRESSION[next_blood]
        blood_preview = {
            "next_level": next_blood,
            "affix_id": blood.affix_id,
            "affix_value": blood.affix_value,
            "materials": blood.materials,
        }
    
    return {
        "beast": beast,
        "skills": beast.get("skills", {}),
        "skill_definitions": BEAST_SKILL_DEFINITIONS,
        "evolution_preview": evolution_preview,
        "blood_preview": blood_preview,
    }


@app.post("/beasts/blood-upgrade")
def post_beast_blood_upgrade(req: BeastBloodUpgradeRequest):
    """Upgrade beast blood level using canonical inventory materials."""
    from beast_config import upgrade_blood
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    with players_db.transaction(req.player_id):
        player = players_db[req.player_id]
        beast = next((b for b in player.get("beasts", []) if b.get("beast_id") == req.beast_id), None)
        if beast is None:
            raise HTTPException(status_code=404, detail="Beast not found")
        inventory_store.ensure_player_inventory(req.player_id, player.get("inventory", {}))
        result = upgrade_blood(beast, player.get("inventory", {}), inventory_store, req.player_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Blood upgrade failed"))
        compatibility_inventory = player.setdefault("inventory", {})
        for item_id in ("beast_bone", "spirit_herb", "blood_vial"):
            compatibility_inventory[item_id] = inventory_store.get_item_count(req.player_id, item_id)
        return result


@app.post("/beasts/formation")
def post_beast_formation(req: BeastFormationRequest):
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    player = players_db[req.player_id]
    beasts = player.get("beasts", [])
    beast = next((b for b in beasts if b.get("beast_id") == req.beast_id), None)
    if beast is None:
        raise HTTPException(status_code=404, detail="Beast not found")
    if req.slot < 0 or req.slot >= 3:
        raise HTTPException(status_code=400, detail="Formation slot must be 0..2")
    formation = player.setdefault("beast_formation", [None, None, None])
    old_slot = next((i for i, bid in enumerate(formation) if bid == req.beast_id), None)
    displaced = formation[req.slot]
    if displaced and displaced != req.beast_id:
        formation[req.slot] = None
    formation[req.slot] = req.beast_id
    if old_slot is not None and old_slot != req.slot:
        formation[old_slot] = None
    for current in beasts:
        current["in_formation"] = current.get("beast_id") in formation
    players_db.persist(req.player_id, player)
    return {"ok": True, "formation": formation, "beast": beast}


@app.get("/beast-formation/{player_id}")
def get_beast_formation(player_id: str):
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    return {"ok": True, "formation": players_db[player_id].get("beast_formation", [None, None, None]), "max_slots": 3}


@app.post("/beasts/skill-upgrade")
def post_beast_skill_upgrade(req: BeastSkillUpgradeRequest):
    """Upgrade one server-validated Beast skill."""
    from beast_config import BEAST_SKILL_DEFINITIONS, skill_cost, upgrade_skill
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    with players_db.transaction(req.player_id):
        player = players_db[req.player_id]
        beast = next((b for b in player.get("beasts", []) if b.get("beast_id") == req.beast_id), None)
        if beast is None:
            raise HTTPException(status_code=404, detail="Beast not found")
        inventory_store.ensure_player_inventory(req.player_id, player.get("inventory", {}))
        result = upgrade_skill(beast, req.skill_id, player.setdefault("wallet", {}), player.setdefault("inventory", {}), inventory_store, req.player_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error", "Skill upgrade failed"))
        player.setdefault("inventory", {})["beast_bone"] = inventory_store.get_item_count(req.player_id, "beast_bone")
        return {
            "ok": True,
            "beast": beast,
            "skill_id": req.skill_id,
            "level": result["level"],
            "next_cost": skill_cost(result["level"] + 1) if result["level"] < BEAST_SKILL_DEFINITIONS[req.skill_id]["max_level"] else None,
            "wallet": player.get("wallet", {}),
            "inventory": player.get("inventory", {}),
        }


# --- Sect Endpoints ---

# REMOVED 2026-09-29: this second `POST /sect/join` was registered on the live
# app, but `server/sect.py join_sect` registers the same method+path FIRST, and
# FastAPI serves the FIRST handler registered, so this copy never received a
# request. Measured 2026-09-29 on the live app: `POST /sect/join` sat at
# route-table index 32 (`sect.join_sect`) and again at index 182
# (`server.main.post_sect_join`).
#
# It was also the weaker of the two: it only read `SECTS` and returned
# `{"ok": True, "sect": sect}` without ever writing `sect_id` / `sect_rank` onto
# the player, so if it were ever served first it would report a successful join
# that never happened. Removed so the path has exactly one handler. The live
# handler in server/sect.py was NOT touched, so the served response is unchanged.
#
# REMOVED: the second `POST /shop/buy` (main.py `post_shop_buy`). Measured on
# the live app before deletion: the path appeared twice in the route table, at
# index N as `main.shop_buy` and again as `main.post_shop_buy`. FastAPI serves
# the FIRST registration, so `shop_buy` was the live one and `post_shop_buy`
# was dead code. Proved behaviourally, not just by table order: the two copies
# disagree on a missing product (live raises 400, the dead copy raised 404), and
# over real HTTP an unknown product_id returned **400 "Product not found"** --
# the live copy's marker. Calling the dead copy directly returned 404, its own
# marker. Only the dead copy was deleted; `shop_buy` was not touched, so the
# response a player can observe is unchanged.
#
# The dead copy had been left in place deliberately, because the survivor is a
# live money contract and picking one was called out as a product decision. It
# no longer is one: the copies had already been reconciled onto the same shared
# idempotency gate in the same `shop-buy` key namespace, so they could not
# disagree about replay protection, and the surviving copy is provably the one
# that was already answering. Removing dead code does not change behaviour.
# `ShopBuyRequest` is kept: it is still referenced by server/tests.




    # --- Shop Endpoints ---

@app.get("/shop/products")
def get_shop_products(player_id: Optional[str] = None):
    """The price list, plus why a row is unbuyable when a player is named.

    A price list that offers an item the server will refuse is how a "the shop
    sold me a useless scroll" report starts, so the client is told here rather
    than discovering the refusal at the till. `player_id` is optional and does
    NOT make this public: the ownership guard owns this route, so an anonymous
    call answers 401 before this handler runs (MEASURED 2026-10-04). Without
    `player_id` the rows carry no player data, but a session is still required.
    With it, the expansion scroll is marked `disabled` at the capacity
    ceiling -- the one case where paying would buy nothing.
    """
    products = [
        {"product_id": "summon_bundle_10", "name": "10x Summon Bundle", "cost": {"fateum": 2700}},
        {"product_id": "battle_pass", "name": "Battle Pass Premium", "cost": {"fateum": 999}},
        {"product_id": "starter_pack", "name": "Starter Pack", "cost": {"fateum": 99}},
        {"product_id": "spirit_stone_pack_small", "name": "Spirit Stone Pack (100)", "cost": {"fateum": 99}},
        {"product_id": EXPANSION_SCROLL_ITEM, "name": "Inventory Expansion Scroll",
         "cost": {"fateum": SHOP_PRODUCTS[EXPANSION_SCROLL_ITEM]["price"]}},
    ]
    if player_id and player_id in players_db:
        slots = inventory_store.get_player_capacity(player_id)
        for product in products:
            if product["product_id"] == EXPANSION_SCROLL_ITEM and slots >= MAX_INVENTORY_SLOTS:
                product["disabled"] = True
                product["reason"] = (
                    f"Bag already at the maximum of {MAX_INVENTORY_SLOTS} slots; "
                    f"this scroll would add nothing."
                )
    return {"products": products}


# --- Event Endpoints ---

def _scheduler_event_rows(scheduler: "Optional[EventScheduler]"):
    """Event rows read from the live scheduler, or [] when none is running.

    Row shape is what the client already reads off this path (event_id,
    name, type, days_remaining), plus the exact end_time it prefers when
    the field is present, so no client has to change to read this.

    Iterates a SNAPSHOT of the instance map on purpose: the scheduler's
    background thread writes into that map, and iterating a dict while
    another thread inserts raises "dictionary changed size during
    iteration". The read is a plain in-memory lookup -- no database, no
    transaction, no lock.
    """
    if scheduler is None:
        return []
    now = time.time()
    rows = []
    for instance in list(scheduler._instances.values()):
        # pre_start and active only. post_end/expired are claim windows the
        # client would render as already finished; POST /events/list is the
        # route that still serves every state.
        if instance.state not in ("pre_start", "active"):
            continue
        # Same day arithmetic as POST /events/list: days to the START for an
        # event that has not opened, days to the END for one that has.
        boundary = (instance.start_time if instance.state == "pre_start"
                    else instance.end_time) or now
        rows.append({
            "event_id": instance.event_key,
            "instance_id": instance.id,
            "name": instance.name,
            "type": instance.event_type,
            "state": instance.state,
            "days_remaining": max(0, int((boundary - now) / 86400) + 1),
            "start_time": instance.start_time,
            "end_time": instance.end_time,
        })
    return rows


@app.get("/events")
def get_events():
    """Get active events, from the scheduler that owns them.

    This used to return a three-entry literal -- celestial_festival,
    pvp_tournament, beast_hunt -- that no scheduler ever wrote and that
    POST /admin/events/create cannot produce. Because it never asked the
    scheduler, a real event such as ``festival_1712345678`` was invisible
    over HTTP: claimable through POST /events/claim, never listed. The rows
    now come from the same scheduler object that claim route resolves
    against.

    Returns an empty list when no scheduler is running (the app driven
    without its lifespan), which is what POST /events/list already does in
    that state, rather than inventing rows to fill the gap.
    """
    return {"events": _scheduler_event_rows(event_scheduler)}


@app.post("/events/claim")
def post_events_claim(req: EventClaimRequest):
    """Claim event reward."""
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player = players_db[req.player_id]
    
    # Dynamic scheduler-backed claim for the production route.
    if not event_scheduler:
        raise HTTPException(status_code=503, detail="Event scheduler not available")
    instance = None
    for candidate in event_scheduler._instances.values():
        if candidate.event_key == req.event_id or str(candidate.id) == str(req.event_id):
            instance = candidate
            break
    if not instance:
        raise HTTPException(status_code=404, detail="Event not found")
    idem = f"event_claim:{instance.id}:{req.player_id}"
    result = event_scheduler.claim_reward(instance.id, req.player_id, idem)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Claim failed"))
    return {"ok": True, "rewards": result.get("rewards"), "payouts": result.get("payouts")}


# --- Gacha Endpoints ---

@app.post("/gacha/pull")
def post_gacha_pull(req: GachaPullRequest, request: Request):
    """Perform a gacha pull.

    The charge, the pity counter and the granted characters are ONE
    read-decide-write unit: they commit together or none of them do.

    Why the transaction is load-bearing (measured, not assumed). This route
    used to read the row with a bare `players_db[player_id]` and end with a
    one-argument `persist(player_id)`. `PlayerStore` republishes its in-memory
    rows as fresh deepcopies on every successful transaction exit, and the
    `spend()` below opens one -- so the handle this route was mutating had
    already been detached by the time it wrote to it. A one-argument `persist`
    marks the row dirty; it cannot carry a mutated value. The pull charged
    fateum, returned a character, and stored nothing. Regression test:
    server/tests/test_route_write_persistence.py.
    """
    from gacha_config import (perform_pull, perform_multi_pull, next_pity,
                             can_afford_pull, RARITY_PULL_COST)

    with players_db.transaction():
        if req.player_id not in players_db:
            # A pull naming a player that does not exist is a probe, not a
            # typo, so it leaves the same anti-abuse trace an unaffordable
            # pull does. Without this the 404 is silent: the rejection is
            # visible in the response but not in the log, and a scanner
            # indexing rejections has nothing to find.
            wcl.safe_log_event(wcl.EVENTS["GACHA_REJECTED"], player_id=req.player_id,
                               route="/gacha/pull", pull_type=req.pull_type,
                               ok=False, reason="player_not_found")
            raise HTTPException(status_code=404, detail="Player not found")

        # Read INSIDE the transaction: this is the staged row, so the mutations
        # below are exactly what the transaction writes back on exit.
        player = players_db[req.player_id]

        # Server-authoritative spend through WalletStore (§3.1/§3.2). It joins
        # this scope, so the charge and the grant share one commit.
        pull_type = req.pull_type  # "single" or "multi_10"
        cost = RARITY_PULL_COST.get(pull_type, RARITY_PULL_COST["single"])
        # Refuse a headerless pull instead of minting a fresh uuid per attempt.
        # A new key per attempt made every retry a brand-new request, so a
        # double-tap was charged 300 fateum twice. See idem_gate.py.
        idem = require_idempotency_key(request, route="gacha-pull")
        spend_result = spend(wallet_store, req.player_id, cost["currency"], cost["amount"],
                             req.client_version, idem)
        if not spend_result.get("ok"):
            # The charge was refused, so no pull happened. Emitted here and
            # not after the charge so a refusal is attributable to this player
            # and this pull type rather than only visible as a missing payout.
            wcl.safe_log_event(wcl.EVENTS["GACHA_REJECTED"], player_id=req.player_id,
                               route="/gacha/pull", pull_type=pull_type,
                               currency=cost["currency"], amount=cost["amount"],
                               ok=False, reason=spend_result.get("error", "spend_refused"))
            raise HTTPException(status_code=400, detail=spend_result)
        if spend_result.get("_replayed"):
            # An idempotent retry grants nothing and costs nothing, so
            # nothing downstream reads it -- which is exactly why it needs
            # a line of its own. Emitted here, before the early return,
            # and with the same field names as a granted pull so an
            # anti-cheat index reads one shape and finds replayed=True.
            wcl.safe_log_event(wcl.EVENTS["GACHA_PULL"], player_id=req.player_id,
                               route="/gacha/pull", pull_type=pull_type,
                               currency=cost["currency"], amount=cost["amount"],
                               ok=True, replayed=True, count=0, rarities=[],
                               new_pity=player.get("gacha_pity", 0),
                               legendary_count=0)
            return {"ok": True, "_replayed": True, "results": [], "new_pity": player.get("gacha_pity", 0)}

        # Get current pity
        pity_count = player.get("gacha_pity", 0)
        guaranteed = player.get("gacha_guaranteed", False)

        # Perform pull(s)
        if pull_type == "multi_10":
            result = perform_multi_pull(pity_count, 10, guaranteed)
        else:
            single = perform_pull(pity_count, guaranteed)
            # The counter comes from the shared helper, never from a
            # hand-written `pity_count + 1` here. That hardcode was a real
            # money defect: at the cap the pull still returned the
            # guaranteed legendary but stored PITY_HARD + 1 instead of 0,
            # so the player stayed pinned at or above the ceiling and every
            # later single pull paid out a legendary for free, on every
            # banner, forever. The rule lives in exactly one place so the
            # two arms cannot drift apart again.
            result = {"results": [single],
                      "new_pity": next_pity(pity_count, single),
                      "legendary_count": 1 if single["rarity"] == "legendary" else 0}

        # Update player
        player["gacha_pity"] = result["new_pity"]
        player["gacha_guaranteed"] = not any(r.get("featured", False) for r in result["results"])

        # Add characters to inventory
        if "characters" not in player:
            player["characters"] = []
        for r in result["results"]:
            player["characters"].append(r["character"])
        # No persist() call: `player` IS the transaction's staged row, so the
        # nested list mutation above is written by the commit on exit.

    w = wallet_store.get(req.player_id)
    resp = _sign_wallet_response(w)
    # Emitted AFTER the `with players_db.transaction():` block, so it can only
    # fire once the charge and the grants have both committed. Inside the block
    # it would report pulls whose characters were never stored. The rarity of
    # every result rides in the context so a balance/grant mismatch is
    # diagnosable from the log line alone.
    wcl.safe_log_event(wcl.EVENTS["GACHA_PULL"], player_id=req.player_id,
                       route="/gacha/pull", pull_type=pull_type,
                       currency=cost["currency"], amount=cost["amount"],
                       ok=True, count=len(result["results"]),
                       rarities=[r.get("rarity") for r in result["results"]],
                       new_pity=result["new_pity"],
                       legendary_count=result.get("legendary_count", 0))
    return {"ok": True, **result, "wallet": resp["balance"], "wallet_version": resp["wallet_version"]}


@app.get("/gacha/banners")
def get_gacha_banners():
    """Get available banners."""
    from gacha_config import BANNERS
    return {"banners": {k: {"name": v.name, "rate_up": v.rate_up, "cost": v.cost} for k, v in BANNERS.items()}}


@app.get("/gacha/pity/{player_id}")
def get_gacha_pity(player_id: str):
    """Get player's current pity count."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    player = players_db[player_id]
    return {
        "pity": player.get("gacha_pity", 0),
        "guaranteed": player.get("gacha_guaranteed", False),
        "spark": player.get("gacha_spark", 0),
    }


@app.post("/combat/auto-battle")
def post_auto_battle(
    req: CombatAutoBattleRequest,
    identity: PlayerIdentity = Depends(require_player),
):
    """Run an auto-battle. The SERVER decides the fight and the payout.

    This route used to be an unauthenticated, unlimited currency mint. The
    caller supplied the enemy, the turn limit, the seed AND the reward amount,
    and the server credited that number verbatim on a win. Proved by execution
    against this app: `rewards: {"spirit_stone": 2_000_000_000}` returned 200
    with a win, `rewards: {"spirit_stone": -5000}` was ACCEPTED and wrote a
    negative balance, and the call could be repeated without limit.

    What owns what now:

      * WHO   -- Depends(require_player). An anonymous caller gets 401 before
                 this function runs, and enforce_ownership (inside that
                 dependency, against the body's player_id claim) already
                 403s a caller naming someone else's player. `req.player_id` is
                 therefore a claim the dependency has verified, not authority.
      * WHAT  -- the opponent, the turn limit and the seed are all derived
                 here. `req.enemy_stats`, `req.max_turns` and `req.seed` are
                 parsed for wire compatibility and then ignored, so a 1-HP
                 enemy or a re-rolled seed cannot choose the outcome.
      * HOW MUCH -- AUTO_BATTLE_REWARDS. `req.rewards` never reaches the
                 wallet; the model only bounds it so an absurd amount is a
                 422 instead of a deposit.
      * HOW OFTEN -- _auto_battle_stone_grant charges a per-player, per-UTC-day
                 budget (AUTO_BATTLE_DAILY_STONE_CAP). Authority over the
                 amount is not a rate limit.

    A LOSS pays nothing, and an exhausted daily budget is a normal 200 with no
    credit -- not a 4xx, which would turn a throttle into a crash loop for a
    client polling on a timer.
    """
    from combat_config import AUTO_BATTLE_MAX_TURNS, calculate_auto_battle

    with players_db.transaction():
        player = players_db.get(req.player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        # The equipped aura is PRESENTATION, not a stat. It is read here so the
        # battle response can tell the client which effect to draw over the
        # player's character, and it is not part of `player_stats`, which is the
        # dict the simulator reads numbers from.
        #
        # It used to be fed in as `player_stats["aura"]`, where
        # `combat_config.calculate_auto_battle` multiplied damage by up to 1.15
        # on its presence. That made a cosmetic a stat purchase -- see the
        # comment at that line. The separation is now structural: the aura is
        # resolved from the LOADOUT (one per slot, §12) rather than from
        # "whatever aura the player happens to own", and it never enters the
        # stat line.
        loadout = player.get("cosmetic_loadout") or {}
        active_aura = loadout.get("aura") or ""

        player_stats = {
            "realm": player.get("cultivation_realm", "mortal"),
            "stage": player.get("cultivation_stage", "early"),
            "element": player.get("spirit_root", "fire"),
            "atk": player.get("atk", 100),
            "def": player.get("def_", 50),
            "hp": player.get("hp", 1000),
            "mp": player.get("mp", 500),
            "spd": player.get("spd", 100),
            # The aura is NOT in this dict. It is presentation: the response
            # below carries it so the client knows which effect to draw, and it
            # is not an input the simulator reads a number from. See the comment
            # where `active_aura` is resolved.
        }

        # Server-owned battle inputs. `secrets` is the server's own entropy, not a
        # client-supplied seed: a caller that can choose the seed can re-roll a
        # losing fight until the simulation reports a win.
        battle_rng = _ab_random.Random(secrets.randbits(64))
        enemy_stats = _auto_battle_enemy_stats(player, battle_rng)

        # Codex: the SERVER picks which monster this fight was against, from
        # the residents of the zone the player is standing in. This is what
        # gives the encounter an identity at all -- before this, the response
        # carried no enemy name and a codex had nothing to read. The client
        # cannot influence the pick: it reads the player's own stored zone and
        # uses the SAME server generator as the fight, and a client-supplied
        # monster_id on the request body is never read (there is no such field).
        #
        # Deliberately IDENTITY-ONLY. The opponent's numbers and element are
        # still _auto_battle_enemy_stats' player-derived ones, because
        # adopting the monster's own stat line measured a mean win-rate drop
        # from 1.00 to 0.76 with a 0.00 floor (the element multiplier is a
        # 1.5x/0.75x pair, so a water monster vs a fire player is a 2x swing).
        # Auto-battle is the main daily income route; that is an economy change
        # for its own reviewed commit. server/codex.py documents the numbers.
        from codex import pick_encounter_monster, record_encounter
        encounter_monster_id = pick_encounter_monster(player, battle_rng)

        result = calculate_auto_battle(
            player_stats=player_stats,
            enemy_stats=enemy_stats,
            max_turns=AUTO_BATTLE_MAX_TURNS,
            seed=battle_rng.getrandbits(32),
            # The player's OWN saved state, read here server-side. The
            # resolver derives every number from it plus the two server
            # catalogues, and it is inert unless WC_LOADOUT_RESOLVER=1 is
            # set. Nothing on the request body reaches this: the body
            # carries no field that could carry a magnitude, and `req` is
            # not consulted for one.
            player=player,
        )
        # Record the encounter the moment the fight RESOLVES, not when it is
        # requested: a caller who abandons the response has still fought, and
        # a loss is still a sighting. A win additionally counts as a kill.
        #
        # `player` above is this transaction's staged row; record_encounter
        # opens a NESTED players_db.transaction(player_id), which the store
        # joins to the same unit, so both writes commit or roll back together.
        if encounter_monster_id:
            try:
                record_encounter(
                    req.player_id,
                    "monster",
                    encounter_monster_id,
                    "killed" if result["winner"] == "player" else "seen",
                )
            except Exception:
                # A codex write must never cost a player their fight or their
                # payout. The payout is already staged on `player` above; if
                # this raised, the whole transaction would roll back and the
                # player would lose a legitimately-won battle to a bestiary
                # bookkeeping error. Swallow it, and the fight still counts.
                pass

        # A loss pays nothing. The daily ledger is only charged on a win, so a
        # player who is outmatched is not throttled for trying.
        granted_stones = 0
        if result["winner"] == "player":
            # The server, not the client, decides that an enemy died. This is the
            # ONLY place mission progress comes from: /missions/progress reconciles
            # against these events and ignores the request's `amount`, so a win is
            # what moves a mission and nothing else can.
            mission_store.record_battle_win(req.player_id)
            # `player` is the transaction's staged row, so the daily ledger and
            # the stone balance below are written by the same commit that closes
            # this scope. Reading the row outside the scope and writing it back
            # with a bare `players_db[pid] = player_dict` left a window where a
            # concurrent win read the same un-spent ledger: both would be granted
            # the full per-win amount, and the last writer would erase the other's
            # charge -- which is exactly how the daily cap stops being a cap.
            granted_stones = _auto_battle_stone_grant(player, AUTO_BATTLE_REWARDS.get("spirit_stone", 0))
            if granted_stones:
                # THE WALLET IS THE CANONICAL BALANCE. This route used to
                # credit ONLY the player row's spirit_stones dict, so the
                # wallet -- the balance GET /wallet reports and the cosmetic
                # shop charges -- never moved. A win was advertised in the
                # reply and landed nowhere a player can spend it.
                #
                # The idem key is (player, day, cumulative stones charged
                # today). The ledger was just charged by the grant above and
                # the whole route runs inside players_db.transaction(), which
                # serialises it, so that triple is unique per win within a
                # day and the day index separates days. A CONSTANT key would
                # have been worse than none: the second win of the day would
                # be answered as a replay and pay nothing.
                #
                # wallet_earn is @atomic, and this store's transaction()
                # JOINS players_db.transaction() (PlayerStore took the wallet
                # in its constructor, so wallet_store.player_store is this
                # store, and PlayerStore.transaction() snapshots the wallet).
                # The credit therefore commits or rolls back with the ledger
                # charge and the mirror below -- the three halves of one win
                # cannot come apart.
                _ab_ledger = player.get(AUTO_BATTLE_LEDGER_KEY) or {}
                _ab_idem = (f"auto_battle:{req.player_id}:{_ab_ledger.get('day')}:"
                           f"{_ab_ledger.get('stones')}")
                _ab_credited = wallet_earn(wallet_store, req.player_id,
                                         "spirit_stone", granted_stones,
                                         _ab_idem, "auto_battle")
                if not _ab_credited.get("ok"):
                    # Never advertise a reward that did not land, and do not
                    # mirror one either -- a phantom balance the player cannot
                    # spend is worse than no balance. The ledger keeps its
                    # charge, so the daily cap cannot be overspent by a
                    # failing wallet. Loud, because a silent zero here is a
                    # main-income route paying nobody.
                    wcl.safe_log_event(wcl.EVENTS["WALLET_EARN_REJECTED"],
                                       player_id=req.player_id, ok=False,
                                       outcome="auto_battle_credit_failed",
                                       reason=_ab_credited.get("error", "refused"),
                                       currency="spirit_stone",
                                       amount=granted_stones)
                    granted_stones = 0
                else:
                    # MIRROR, not the source of truth. Several spenders
                    # still read this dict off the player row --
                    # breakthrough._stones, the battle-pass upgrade route,
                    # and the auto-battle cap tests -- so it has to track the
                    # wallet or those features spend a balance that never
                    # grows. The tier split is UNCHANGED: it decides which
                    # key the mirror increments, and moving stones between
                    # tiers is an economy change, not a plumbing one.
                    stone_type = "low_grade"
                    if granted_stones >= 100:
                        stone_type = "high_grade"
                    elif granted_stones >= 50:
                        stone_type = "mid_grade"
                    stones = dict(player.get("spirit_stones", {}) or {})
                    stones[stone_type] = int(stones.get(stone_type, 0) or 0) + granted_stones
                    player["spirit_stones"] = stones
            # The daily ledger is written even when the grant is 0, so an exhausted
            # day is visible on the player row rather than invisible. It is already
            # on the staged row via _auto_battle_stone_grant; no persist() call is
            # needed or wanted -- a one-argument persist() cannot carry a value.

    return {
        "ok": True,
        **result,
        "rewards": {"spirit_stone": granted_stones},
        "daily_stone_cap": AUTO_BATTLE_DAILY_STONE_CAP,
        # Read-only INFORMATION about the opponent the server picked, so the
        # client can show "you fought X" and cross-reference the codex. It is
        # never authority: nothing on the request can name it, and the same
        # value cannot change the payout, which is decided above from the
        # server's own tables.
        "monster_id": encounter_monster_id,
        # The equipped aura, for DRAWING. It travels with the battle result so
        # the client can render the effect over the player's character, and it
        # is on the far side of the simulation: it did not enter the stat line
        # and it did not influence the result. A test asserts the damage is
        # identical with and without it.
        "aura": active_aura,
    }


# --- PvP enemy stat-line normalisation -----------------------------------
#
# /pvp/match and /pvp/replay take `enemy_stats` straight from the request body
# and hand it to combat_config.calculate_auto_battle, which HARD-indexES it:
# enemy_stats["hp"], enemy_stats["atk"]. A stat line that omits either one
# raises KeyError, which FastAPI turns into a 500 -- for a legitimate player
# on the legitimate path.
#
# The client does not send a hand-built combat line. It sends the line the
# server gave it: a /pvp/matchmaking candidate, which carries
# player_id/name/rating/realm/stage/rating_diff and NO hp, atk, mp or spd. So
# the exact request a real player makes was the exact request that 500'd.
#
# combat_config.stat_def already learned to read both `def_` and `def`; this
# closes the SAME class for the remaining keys at the boundary, where the
# untrusted value enters. Nothing is invented: a missing combat stat becomes
# the same baseline the server uses for a brand-new player, so a sparse
# candidate is still a real opponent rather than a crash.
PVP_STAT_DEFAULTS = {
    "realm": "mortal",
    "stage": "early",
    "element": "fire",
    "atk": 100,
    "def": 50,
    "hp": 1000,
    "mp": 500,
    "spd": 100,
}


def _coerce_stat(value, default):
    """Accept only a usable positive number; anything else falls back."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _opponent_from_store(opponent_id: str) -> dict | None:
    """Read the OPPONENT's real stat line out of the player store.

    This is the authority fix. `req.enemy_stats` used to be the stat line the
    simulator fought, so a caller could hand-feed `{"hp": 1, "atk": 0}` and win
    any fight on demand -- and could equally hand-feed a 99999-HP wall to farm
    the rating LOSS path. The opponent is now named by `opponent_id` only; its
    numbers come from its own saved row, exactly as the player's numbers do.

    Returns ``None`` when the opponent id names nobody. The caller does NOT 404
    on that: matchmaking legitimately races (an opponent can be deleted between
    the candidate list and the match), and `test_combat_def_stat_key.py` posts
    `opponent_id: "opp-1"`. An unknown opponent therefore fights the neutral
    baseline -- the SAME numbers a brand-new player has, and the same numbers the
    honest empty-dict client already produced before this fix. Nothing about the
    missing row is the caller's to choose.
    """
    row = players_db.get(opponent_id)
    if row is None:
        return None
    return {
        "realm": row.get("cultivation_realm", "mortal"),
        "stage": row.get("cultivation_stage", "early"),
        "element": row.get("spirit_root", "fire"),
        "atk": row.get("atk", 100),
        "def": row.get("def_", 50),
        "hp": row.get("hp", 1000),
        "mp": row.get("mp", 500),
        "spd": row.get("spd", 100),
    }


def _pvp_rating_for(opponent_id: str) -> int:
    """The opponent's real rating, for the rating-change formula.

    `req.opponent_rating` was the second half of the same hole: it fed
    `calculate_pvp_rating_change`, so a caller could name a 4000-rated opponent
    and take the maximum +31 on a win that was not earned (measured: naming
    rating 4000 returned `rating_change: 31`; naming rating 1 returned 0). The
    rating is now the opponent's saved `pvp_rating`, falling back to the same
    1000 every other PvP read in this file uses.
    """
    row = players_db.get(opponent_id)
    if row is None:
        return 1000
    try:
        return int(row.get("pvp_rating", 1000) or 1000)
    except (TypeError, ValueError):
        return 1000


def _pvp_enemy_stat_line(req) -> dict:
    """The stat line the PvP routes actually fight, assembled server-side.

    One helper so /pvp/match and /pvp/replay cannot drift: a replay that built
    its line by a different rule would stop reproducing the match it replays.
    """
    return _normalize_pvp_enemy_stats(_opponent_from_store(req.opponent_id) or {})


def _normalize_pvp_enemy_stats(enemy_stats: dict) -> dict:
    """Fill a PvP stat line out to the keys the simulator reads.

    Accepts either defence spelling (`def` / `def_`) so a stat line read from a
    player row -- which spells it `def_` -- works unchanged. Since the PvP routes
    were hardened this normalises a SERVER-DERIVED line, not the caller's; it is
    still the boundary that guarantees every key the simulator hard-indexes
    (`hp`, `atk`) is present, so a sparse stored row cannot raise KeyError.
    """
    source = enemy_stats if isinstance(enemy_stats, dict) else {}
    normalized = {}
    for key, default in PVP_STAT_DEFAULTS.items():
        if key == "def":
            raw = source.get("def_", source.get("def", default))
        else:
            raw = source.get(key, default)
        if key in ("realm", "stage", "element"):
            normalized[key] = str(raw) if raw else default
        else:
            normalized[key] = _coerce_stat(raw, default)
    return normalized


@app.post("/pvp/match")
def post_pvp_match(req: PvPMatchRequest):
    """Start a PvP match against an async ghost opponent."""
    from combat_config import calculate_auto_battle, calculate_pvp_rating_change

    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")

    player = players_db[req.player_id]

    player_stats = {
        "realm": player.get("cultivation_realm", "mortal"),
        "stage": player.get("cultivation_stage", "early"),
        "element": player.get("spirit_root", "fire"),
        "atk": player.get("atk", 100),
        "def": player.get("def_", 50),
        "hp": player.get("hp", 1000),
        "mp": player.get("mp", 500),
        "spd": player.get("spd", 100),
    }

    # The opponent's stat line comes from ITS OWN stored row, named by
    # `req.opponent_id`. `req.enemy_stats` is parsed for wire compatibility and
    # then ignored, so a caller can no longer pick a 1-HP opponent to win on
    # demand. The seed still names the same pair, so a replay of a match played
    # before this change reproduces it exactly.
    enemy_stats = _pvp_enemy_stat_line(req)

    result = calculate_auto_battle(
        player_stats=player_stats,
        enemy_stats=enemy_stats,
        max_turns=50,
        seed=source_match_seed(req.player_id, req.opponent_id, _pvp_rating_for(req.opponent_id)) % (2**32),
    )

    # Calculate rating change
    player_rating = player.get("pvp_rating", 1000)
    opponent_rating = _pvp_rating_for(req.opponent_id)
    rating_change = calculate_pvp_rating_change(player_rating, opponent_rating, result["winner"] == "player")
    
    if result["winner"] == "player":
        player["pvp_rating"] = player_rating + rating_change
        # A PvP win is decided here and nowhere else. `pvp_wins` feeds the
        # legacy death score (server/legacy.py), which read a value nothing
        # ever wrote. Written on the live `player` dict, so the existing
        # players_db.persist() below commits it with the rating change.
        player["pvp_wins"] = int(player.get("pvp_wins", 0) or 0) + 1
    else:
        player["pvp_rating"] = max(0, player_rating + rating_change)
    history = player.setdefault("pvp_history", [])
    history.insert(0, {
        "opponent_id": req.opponent_id,
        "winner": result["winner"],
        "rating_change": rating_change,
        "new_rating": player["pvp_rating"],
        "turns": result["turns"],
        "log": result["log"],
    })
    del history[20:]
    players_db.persist(req.player_id, player)
    
    return {
        "ok": True,
        "winner": result["winner"],
        "turns": result["turns"],
        "rating_change": rating_change,
        "new_rating": player["pvp_rating"],
        "log": result["log"],
    }


@app.get("/pvp/ranking")
def get_pvp_ranking(player_id: str):
    """Get PvP ranking and nearby opponents."""
    if player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player = players_db[player_id]
    player_rating = player.get("pvp_rating", 1000)
    
    # Find nearby opponents (simplified - in production, use database query)
    all_ratings = [(pid, p.get("pvp_rating", 1000)) for pid, p in players_db.items() if pid != player_id]
    all_ratings.sort(key=lambda x: abs(x[1] - player_rating))
    
    nearby = []
    for pid, rating in all_ratings[:5]:
        p = players_db[pid]
        nearby.append({
            "player_id": pid,
            "name": p.get("name", "Unknown"),
            "rating": rating,
            "realm": p.get("cultivation_realm", "mortal"),
            "stage": p.get("cultivation_stage", "early"),
        })
    
    # Calculate player's rank
    sorted_players = sorted(players_db.items(), key=lambda x: x[1].get("pvp_rating", 1000), reverse=True)
    player_rank = next((i + 1 for i, (pid, _) in enumerate(sorted_players) if pid == player_id), 0)
    
    return {
            "player_rating": player_rating,
            "player_rank": player_rank,
            "nearby_opponents": nearby,
        }


@app.post("/pvp/matchmaking")
def post_pvp_matchmaking(req: PvPMatchmakingRequest):
    """Find a suitable opponent for PvP matchmaking."""
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    player = players_db[req.player_id]
    player_rating = player.get("pvp_rating", 1000)
    candidates = []
    for pid, opponent in players_db.items():
        if pid == req.player_id:
            continue
        opponent_rating = opponent.get("pvp_rating", 1000)
        if abs(opponent_rating - player_rating) <= 200:
            candidates.append({
                "player_id": pid,
                "name": opponent.get("name", "Unknown"),
                "rating": opponent_rating,
                "realm": opponent.get("cultivation_realm", "mortal"),
                "stage": opponent.get("cultivation_stage", "early"),
                "rating_diff": opponent_rating - player_rating,
            })
    candidates.sort(key=lambda candidate: abs(candidate["rating_diff"]))
    return {"ok": True, "candidates": candidates[:3]}


@app.post("/pvp/history")
def post_pvp_history(req: PvPMatchHistoryRequest):
    """Get the bounded in-memory PvP match history for a player."""
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    limit = max(1, min(int(req.limit), 20))
    return {"ok": True, "matches": list(players_db[req.player_id].get("pvp_history", []))[:limit]}


@app.post("/pvp/replay")
def post_pvp_replay(req: PvPMatchRequest):
    """Re-simulate a deterministic ghost battle replay."""
    if req.player_id not in players_db:
        raise HTTPException(status_code=404, detail="Player not found")
    from combat_config import calculate_auto_battle
    player = players_db[req.player_id]
    player_stats = {
        "realm": player.get("cultivation_realm", "mortal"),
        "stage": player.get("cultivation_stage", "early"),
        "element": player.get("spirit_root", "fire"),
        "atk": player.get("atk", 100), "def": player.get("def_", 50),
        "hp": player.get("hp", 1000), "mp": player.get("mp", 500), "spd": player.get("spd", 100),
    }
    # Same rule as /pvp/match, from the same helper: the opponent's stored row.
    # If the two routes built the line differently, a replay would stop
    # reproducing the match it is a replay of.
    result = calculate_auto_battle(
        player_stats=player_stats,
        enemy_stats=_pvp_enemy_stat_line(req),
        max_turns=50,
        seed=source_match_seed(req.player_id, req.opponent_id, _pvp_rating_for(req.opponent_id)) % (2**32),
    )
    return {"ok": True, "log": result["log"], "winner": result["winner"], "turns": result["turns"]}


# --- Remote Config ---

@app.get("/config")
def get_remote_config():
    """Live-ops remote config: events, gacha rates, shop packs, feature flags.
    Client fetches on boot + every 15 min. Changes here apply without app update."""
    # Merge in this week's generated live-ops content when present
    weekly_events = []
    try:
        from generate_weekly_events import generate
        weekly = generate()
        weekly_events = [
            {"event_id": e["key"], "name": e["name"], "type": e["type"],
             "end_time": e["end_time"], "week_key": weekly["week_key"]}
            for e in weekly["events"]
        ]
    except Exception:
        pass  # generator missing/broken -> fall back to static list

    return {
        "version": 3,
        "week_key": (weekly.get("week_key") if 'weekly' in dir() else None),
        # Same scheduler read as GET /events, for the same reason: the
        # three-entry literal that used to sit here was fiction the live-ops
        # client rendered as real events, and it duplicated the stub in
        # get_events(). One source of truth for the same list.
        "events": weekly_events + _scheduler_event_rows(event_scheduler),
        "gacha_rates": {
            "standard": {"common": 0.75, "rare": 0.20, "epic": 0.045, "legendary": 0.005},
            "rate_up": {"common": 0.70, "rare": 0.24, "epic": 0.05, "legendary": 0.01},
        },
        "shop_packs": [
            {"product_id": "starter_pack", "cost_fateum": 99, "limit": 1},
            {"product_id": "battle_pass", "cost_fateum": 999, "limit": 1},
            {"product_id": "summon_bundle_10", "cost_fateum": 2700, "limit": 10},
        ],
        "ad_frequency": {"interstitial_min_interval_sec": 180, "rewarded_per_day": 20},
        "feature_flags": {
            "pvp_enabled": True,
            "sect_wars_enabled": True,
            "exclusivity_slots_enabled": True,
            "waitlist_enabled": True,
            "legacy_system_enabled": True,
        },
        "maintenance": False,
    }


# --- Analytics ---

class AnalyticsBatchRequest(BaseModel):
    # max_length is the EDGE bound, so an oversized batch is refused by the
    # model with a 422 before the handler runs and nothing is buffered. Same
    # pattern as the Query(ge=..., le=...) edge bounds elsewhere in this file.
    events: List[dict] = Field(..., max_length=MAX_ANALYTICS_BATCH)


def _clamp_analytics_params(params, limit: int = MAX_ANALYTICS_PARAMS_BYTES) -> dict:
    """Return `params` with its serialised size at or under `limit`.

    Bounds the SIZE, not the key count: a dict with one 100 KB string is the
    shape a hostile caller actually sends, and it is legal as far as the
    request model is concerned. Keys are dropped in insertion order until the
    encoded form fits, so what survives is the caller's leading keys rather
    than an arbitrary sample. `limit` is compared against encoded UTF-8 bytes,
    which is what actually costs memory, rather than character count.
    """
    if not isinstance(params, dict):
        return {}
    # islice, not list(...keys())[:n]: this is a hot path a caller can aim a
    # 100 000-key dict at, and materialising the whole key list to slice it
    # would rebuild the very allocation the limit exists to avoid.
    kept: dict = {}
    for key in itertools.islice(iter(params), limit):
        candidate = dict(kept)
        candidate[key] = params[key]
        try:
            size = len(json.dumps(candidate, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            continue  # unserialisable value: drop it rather than fail the batch
        if size > limit:
            break
        kept = candidate
    return kept


def _sanitize_analytics_event(ev) -> Optional[dict]:
    """Return a bounded, server-attributed copy of one event, or None to drop it.

    Three things happen here, and all three belong here rather than at the
    point of use, because this is the only place a client event is read:

    * The client-asserted `player_id` is NOT carried through. The stored row
      has no `player_id` key at all, so no caller can file events under an
      account it merely typed. The authenticated session is the authority for
      identity, and where this route has no session to supply one, the honest
      value is absent rather than copied from the request.
    * `event_name` is stripped, refused when blank, and truncated, so one
      event cannot smuggle a 5 KB "name".
    * `params` is bounded by encoded size, so a batch inside the count limit
      still cannot carry megabytes per row.
    """
    if not isinstance(ev, dict):
        return None
    name = ev.get("event_name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    session_id = ev.get("session_id")
    return {
        "event_name": name[:MAX_ANALYTICS_EVENT_NAME],
        "session_id": (str(session_id)[:MAX_ANALYTICS_EVENT_NAME]
                       if session_id is not None else ""),
        "params": _clamp_analytics_params(ev.get("params")),
    }


@app.post("/analytics/batch")
def post_analytics_batch(req: AnalyticsBatchRequest):
    """Receive client analytics events (batched). Store to PG if available,
    else buffer in memory. Fire-and-forget tolerant: malformed events dropped.

    Deliberately tolerant: a batch within the count limit is accepted even when
    every event in it is malformed, and `accepted` reports how many survived
    sanitising. What it is not tolerant of is SIZE. Nothing in this file ever
    drains `analytics_buffer`, so that buffer's bound is the entire memory
    defence, and before the guard it was a plain list that any caller could
    grow without limit.
    """
    # Sanitise first, buffer second: a malformed event must not reach memory at
    # all, and the PG write must see the same bounded rows the buffer does
    # rather than re-reading the raw request body.
    stored = []
    for ev in req.events:
        clean = _sanitize_analytics_event(ev)
        if clean is None:
            continue
        # deque(maxlen=...) evicts the oldest event past the ceiling, so this
        # append cannot grow process memory no matter what a caller sends.
        analytics_buffer.append(clean)
        stored.append(clean)
    accepted = len(stored)
    if USE_PG:
        try:
            import psycopg2
            import psycopg2.extras
            conn = wallet_store.conn  # reuse module-level store connection
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS analytics_events ("
                "id SERIAL PRIMARY KEY, player_id TEXT, session_id TEXT, "
                "event_name TEXT NOT NULL, params JSONB, created_at TIMESTAMP DEFAULT NOW())"
            )
            for ev in stored:
                # The player_id column is still written, so existing rows keep
                # their shape, but it is never filled from the request body: a
                # caller-typed id cannot reach the table. The session's own
                # player is the only legitimate source, and this route has none.
                cur.execute(
                    "INSERT INTO analytics_events (player_id, session_id, event_name, params) "
                    "VALUES (%s, %s, %s, %s)",
                    (None, ev["session_id"],
                     str(ev["event_name"]), psycopg2.extras.Json(ev.get("params", {}))),
                )
            conn.commit()
        except Exception:
            pass  # analytics must never break gameplay
    return {"ok": True, "accepted": accepted, "buffered": len(analytics_buffer)}


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# Additional route registrations (must be ABOVE install_ownership_guard)
# ---------------------------------------------------------------------------
# PvP Arena routes (R11)
from pvp_routes import register_pvp_routes
register_pvp_routes(app, lambda: players_db)

# Cultivation Ladder routes (R5)
from cultivation_routes import register_cultivation_routes
register_cultivation_routes(app, _players_db_accessor)

# Element Matrix routes (R12)
from element_matrix_routes import register_element_matrix_routes
register_element_matrix_routes(app, lambda: players_db, lambda: wallet_store)

# Social Features routes (R13)
from social_routes import register_social_routes
from social_routes import SocialStore
from sect_routes import SectV2Store, register_sect_v2_routes
# `sect_store` (line 562) is the ONE store /sect/* writes membership into, and
# it is already imported and in scope here. Passing it -- plus friends_store,
# already built for /friends/* -- is what makes register_social_routes build a
# SectV2Store at registration time instead of leaving store_v2 as None, which
# made _require_v2() answer every /social/sect/* request 503 "Sect activity
# unavailable". A dedicated `SectStore()` here would be a SEPARATE empty dict:
# social_routes._caller_sect would read no membership from it even though the
# routes would stop 503-ing, so a real sect member would silently see nothing.
social_store = SocialStore(
    players_db_provider=lambda: players_db,
    friends_store=friends_store,
    sect_store=sect_store,
    inventory_store=inventory_store,
)
# ONE SectV2Store, shared by BOTH sect surfaces. `sect_routes.py` and
# `social_routes.py` each accept one as an argument, and each will happily
# build its own when it is omitted -- which is the bug. `SectV2Store.meditation`
# IS the once-per-day claim guard (sect_routes.py:447), so two instances are
# two guards: measured on a scratch app, claiming through
# `/social/sect/meditation/join` and then `/sect-v2/meditation/claim` paid 125 qi
# TWICE, because each store only knew about its own claim. Passing the same
# object to both registrars is what makes "once per day" mean once per player.
sect_v2_store = SectV2Store(sect_store)
register_social_routes(app, lambda: players_db, social_store, sect_v2_store)

# Sect v2 routes -- the third sect surface, previously UNMOUNTED. All 13 of
# `sect_routes.py`'s decorators were unreachable: `register_sect_v2_routes` had
# no call site anywhere in this file, so the module was imported by sect_war.py
# and social_routes.py for `SectV2Store` and the `RANK_*` constants, but nothing
# ever mounted its routes. Measured before this call: 0 of 13 paths in the live
# route table, every one answering 404 to an authenticated caller.
#
# Registered HERE, above install_ownership_guard(app) at the bottom of this
# file, for the same reason world_event_routes is: the guard rewrites routes in
# place, so a registration below it would ship all 13 unguarded.
register_sect_v2_routes(app, lambda: players_db, sect_store, sect_v2_store)

# Rift routes (R14)
from rift_routes import register_rift_routes
register_rift_routes(app, lambda: players_db, lambda: wallet_store)

# World Event routes (R15)
# Mounted HERE, above install_ownership_guard(app) at the bottom of this file:
# the guard rewrites routes in place, so a register call below it would ship
# every world-event route unguarded. NOT registered in lifespan() -- lifespan()
# runs after the guard, and register_world_event_routes is idempotent by app
# attribute anyway.
from world_event_routes import register_world_event_routes
register_world_event_routes(app, lambda: players_db, lambda: wallet_store)

# Beast Wave routes (R16)
#
# The ONE `sect_store`, built at the `from sect import SectStore` line above
# (it is already imported and in scope here -- no second import, no second
# constructor). Both route families below resolve sect membership through
# `sect_store.get_player_sect(...)` / `sect_store.player_sect`:
#   * `beast_wave_routes._caller_sect` (beast_wave_routes.py:146)
#   * `sect_war._side_from_membership` (sect_war.py:366-369), which decides the
#     SIDE of a war a player fights on and therefore what they are paid.
#
# A dedicated `SectStore()` here is a SEPARATE empty dict. Membership written
# by `POST /sect/join` lands in the line-576 store, so the wave and sect-war
# routes would read `None` for every real member and answer as though nobody
# belonged to a sect. Same shape as the defect already fixed for
# `/social/sect/*` and `/sect-v2/*` above (see the `sect_v2_store` comment):
# two stores mean two answers, and the once-per-war contribution guard in
# `sect_war.apply_war_contribution` stops meaning anything.
from beast_wave_routes import register_wave_routes
register_wave_routes(app, lambda: players_db, lambda: wallet_store, sect_store)



# ---------------------------------------------------------------------------
# Trading / market (11 routes): direct trade, mail trade, sect board, auction.
# ---------------------------------------------------------------------------
# `register_trading_routes` returns the ONE TradeStore it built and wired the
# routes to. Bound here as `trade_store` so that a future caller has the same
# instance rather than constructing a second, empty one.
#
# NOTE: trading_market.py's own docstring says the 90-day sweep and the
# auto-relist pass "share the one instance". Neither function exists in that
# file (measured: 0 matches for a relist/sweep def). The binding below is
# forward-looking only -- nothing reads `trade_store` today.
#
# `inventory_store` (not a fresh store) and `sect_store` (not a new SectStore):
# the same two-stores-two-answers defect the wave/sect-war mounts below guard
# against. A player who sold an item through another route owns it in THIS
# inventory; a member who joined via POST /sect/join is in THIS sect_store.
# `sect_store_accessor` may be a store or a zero-arg callable -- the registrar
# resolves both.
from trading_market import register_trading_routes
trade_store = register_trading_routes(app, _players_db_accessor, inventory_store, sect_store)

# ---------------------------------------------------------------------------
# Currency exchange (2 routes): GET /wallet/rates, POST /wallet/convert.
# ---------------------------------------------------------------------------
# The WALLET store, not players_db, for the same reason as register_mint_authority
# below: `convert()` calls wallet_dispatch.spend/earn, and only the wallet has
# `recall`. players_db_provider is passed for the clearer 404 before the wallet
# is touched.
from currency_exchange import register_currency_routes
register_currency_routes(app, lambda: wallet_store, lambda: players_db)

# ---------------------------------------------------------------------------
# Daily boss LADDER (3 routes) -- the sequence-of-three-bosses day, NOT the
# older /daily-boss/{player_id} family.
# ---------------------------------------------------------------------------
# Mounted HERE, beside the daily-boss mounts, because register_boss_ladder_routes
# must run BEFORE install_ownership_guard: that guard only judges routes that
# already exist when it runs. The three paths are invisible to its
# player-id shape (none of them names a player), so `Depends(require_player)`
# carried by hand in daily_boss_routes.py is the whole protection here.
from daily_boss_routes import register_boss_ladder_routes
register_boss_ladder_routes(app, lambda: players_db)

# Sect War routes (F1) -- the SIX core routes plus the two read-only extras.
#
# `register_sect_war` RE-EXPORTS `sect_war.register_sect_war_routes` and calls
# it internally (sect_war_routes.py:156), then adds GET /sect-war/{war_id}/
# leaderboard and GET /sect-war/{sect_id}/banners. Calling the core registrar
# here as well would mount all six core routes a SECOND time -- the same
# method+path twice over, where the first copy silently wins. So the core
# import was REPLACED by the wrapper, not added beside it.
#
# The schema assertion inside the wrapper is try/except and only logs; a
# missing sect-war table is loud but not a boot failure for everything else.
from sect_war_routes import register_sect_war
register_sect_war(app, lambda: players_db, sect_store)

# Mint Authority routes (P0)
from mint_authority import register_mint_authority_routes
# The WALLET store, not players_db. Every handler in this module calls
# earn(store, ...), which reaches wallet_defense.earn, whose first statement on
# the store is store.recall(...) (wallet_defense.py:490). PlayerStore has no
# `recall`, so passing it here made POST /admin/events/create and
# POST /purchase/validate raise AttributeError -> HTTP 500 for every authorised
# caller. The gate was never the problem; the store shape was.
register_mint_authority_routes(app, lambda: wallet_store)

# ---------------------------------------------------------------------------
# Ownership guard -- MOUNTED. Do not remove as dead code.
# ---------------------------------------------------------------------------
# ownership_guard.py was written, tested and imported cleanly while every
# mention of it in this file was a COMMENT (0 real call sites, measured by
# AST parse). That single omission was the root cause of ~19 confirmed
# cross-player holes. This call is the fix.
#
# MEASURED BEFORE / AFTER mounting install_ownership_guard(app), live requests
# against this app:
#   before: anonymous strike on another player ....... 200  (leak)
#   after:  anonymous strike on another player ....... 401  (refused)
#   after:  signed-in attacker reads another player's
#           lifecycle ................................ 403  (refused)
#   after:  legitimate player reads OWN lifecycle ... 200  (unchanged)
#   after:  GET /player/{player_id} anonymous ....... 200 but narrowed to
#           id/name/spirit_root only (BOOTSTRAP_FIELDS) -- the exemption is
#           KEPT on purpose, see ownership_guard.EXEMPT_PATHS.
#   coverage: 170 of 203 routes now carry an auth dependency, up from 6
#   before this call (both numbers measured with
#   ownership_guard.has_auth_dependency over main.app.routes). 173 routes
#   match the guard's "bare player id, no auth" shape; the 3 that carry an
#   auth dependency without being newly guarded are the two EXEMPT_PATHS
#   entries and one route that already authenticated.
# Do NOT delete the /player/{player_id} exemption: without it the shipped
# client answers 401 while restoring a save and boots into a dead end.
#
# ORDERING -- read before moving this block. THE GUARD IS NOW INSTALLED TWICE,
# ON PURPOSE, and this line is the first of the two.
#
# The bug this fixes: the guard rewrites routes in place, so it can only judge
# the routes that already exist when it runs. lifespan()'s
# register_event_routes() runs at startup, AFTER this line, so the four event
# routes it adds were never evaluated -- and POST /events/list takes a
# player_id in its REQUEST BODY, matched the guard's shape, and shipped with
# no auth dependency and no ledger entry at all. The second install, inside
# lifespan() (see the OWNERSHIP GUARD comment there), runs last and covers
# them.
#
# Why keep THIS import-time install instead of removing it: `_GUARDED_ROUTES`
# is a published part of this module. Tests and the skip-ledger contract read
# it after a bare `import main`, without entering the lifespan, and would see
# an empty dict if the only install happened at startup. Import-time coverage
# of the 285 routes that exist at import is therefore still wanted; what was
# wrong was treating it as the ONLY install.
#
# install_ownership_guard is documented idempotent (a second call finds the
# dependency already present and does nothing), and the bootstrap narrowing it
# applies to GET /player/{player_id} carries its own _wc_bootstrap_narrowed
# guard, so calling it twice cannot double-wrap anything. The second call
# returns ONLY the routes the first one missed, and lifespan() merges that
# into this same dict with .update() rather than rebinding the name, so a
# reference captured at import stays valid and grows.
from ownership_guard import install_ownership_guard

_GUARDED_ROUTES: dict = install_ownership_guard(app)

# ---------------------------------------------------------------------------
# Claim rate limit -- MOUNTED. Same role for the claim family that
# _enforce_signup_rate_limit plays for /player/create.
# ---------------------------------------------------------------------------
# MEASURED BEFORE this mount: 40 rapid POST /sign-in/{player_id}/claim calls all
# answered 200. Every claim surface (sign-in, battle pass, daily boss, offline
# progress, events, missions, monthly event, leaderboard payout, gift series,
# achievements, inner skills, daily streak, lifecycle) is a payout route, and
# each feature's own ledger stops a DOUBLE claim while saying nothing about
# VOLUME -- forty parallel claims race forty ledger reads.
#
# ORDERING -- read before moving this block. install_claim_rate_limit appends a
# Depends to each matched route's dependant, exactly as
# install_ownership_guard does, so it must run AFTER every @app.post decorator
# and every register_*() call that declares a claim route. Mounting it above
# either of those lines leaves the later-declared claim routes unthrottled,
# silently, with no error. lifespan()'s register_event_routes() is the same
# known exception the ownership guard documents: routes registered at startup
# are not covered by this mount.
#
# The cap is a volume brake, not a correctness fix, and it is env-overridable
# (WC_CLAIM_RATE_LIMIT, WC_CLAIM_RATE_WINDOW_SECONDS) so an operator can move
# it without a code change. See server/rate_limit.py for why the key is the
# PLAYER and not the network address.
from rate_limit import install_claim_rate_limit

_RATE_LIMITED_ROUTES = install_claim_rate_limit(app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("WC_PORT", 8777)), log_level="info")
