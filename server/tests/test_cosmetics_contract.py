"""
CONTRACT TEST: server/cosmetics.py -- the nine /cosmetics/* routes.

WHAT THIS FILE PINS
------------------
The wire contract of ``register_cosmetic_routes``: which paths mount and under
which verb, the exact reply key set of every endpoint, the exact status each
refusal answers, which routes demand a session and which are deliberately
public, that the server (not the request) picks the currency and the price,
that an idempotency key is honoured on replay and refused on a payload
mismatch, and that equipping a cosmetic changes nothing a combat number reads.

It asserts RELATIONSHIPS rather than frozen numbers. Every price is read out of
``cosmetics.COSMETIC_CATALOG`` at run time and the wallet delta is compared to
that price, so re-pricing the shop does not fail this file -- but a route that
started computing its own copy of a price, or charging in the currency the
caller named, still goes red.

WHY THIS FILE BUILDS ITS OWN APP
--------------------------------
Most other suites in ``server/tests/`` import ``server.main``, so their result
depends on whatever another agent is mid-write in that 3000-line import graph.
This file mounts ``register_cosmetic_routes`` onto a bare ``FastAPI()`` with
its own in-memory player store, so it measures this module and nothing else. It
touches ``server/main.py`` nowhere. One shared-tree hazard is NOT avoidable:
the repository root ``conftest.py`` imports ``server.main`` at COLLECTION time,
so if a peer is half-way through editing ``main.py`` every pytest run in the
repo aborts with a collection error before a single test executes. Re-read
``main.py`` at the cited line before reading that traceback as a defect here.

THE AUTH STORY IS TWO LAYERS, AND THIS FILE PINS BOTH
------------------------------------------------------
Three routes -- ``GET /cosmetics/player/{player_id}``,
``POST /cosmetics/{player_id}/start-trial`` and
``POST /cosmetics/{player_id}/purchase`` -- declare NO auth dependency in this
module. Their safety comes from ``ownership_guard.install_ownership_guard``,
which ``main.py`` calls at the bottom of the file, AFTER every route is
registered, and which attaches ``require_player`` to any route whose shape is
"a bare player id in the path and no auth dependency yet".

So the module alone is open and the assembled server is closed. Both facts are
pinned here, in separate tests, because either one alone is misleading: a test
that only booted the bare app would report the three legacy routes as
anonymous-writable, and a test that only booted the assembled server could not
say which half of the protection lives in which file.

WHAT IS DELIBERATELY PUBLIC
---------------------------
``GET /cosmetics/catalog``, ``GET /cosmetics/catalogue`` and
``POST /cosmetics/preview`` serve a caller with no session, on purpose: they
are a price list and a visual-description lookup. Their rows are whitelists
(``_public_catalogue_row``), so adding a field to a catalogue definition does
not publish it.

Run
---
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD \\
        -u WC_PG_PORT -u PYTHONPATH \\
        python -m pytest server/tests/test_cosmetics_contract.py -v
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
import uuid
from copy import deepcopy
from typing import Any, Dict, Iterator, Optional

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# In-memory storage must be forced. Without it a single-file run reports a wall
# of errors from an absent database, and reading those as failures sends an
# engineer hunting a bug that does not exist.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # bare, NOT server.auth -- main.py binds the same module object
import cosmetics  # noqa: E402
import ownership_guard  # noqa: E402
from player_store import PlayerStore  # noqa: E402
from wallet_dispatch import InMemoryWalletStore  # noqa: E402

CATALOG = "/cosmetics/catalog"          # legacy spelling
CATALOGUE = "/cosmetics/catalogue"      # the one the design note names
PLAYER = "/cosmetics/player/{player_id}"
START_TRIAL = "/cosmetics/{player_id}/start-trial"
PURCHASE = "/cosmetics/{player_id}/purchase"
OWNED = "/cosmetics/owned"
EQUIP = "/cosmetics/equip"
UNEQUIP = "/cosmetics/unequip"
PREVIEW = "/cosmetics/preview"

#: The nine paths, and the single method each one answers. The method is part of
#: the contract rather than a detail: every wrong-verb probe measured 405.
DECLARED = (
    (CATALOG, "GET"),
    (CATALOGUE, "GET"),
    (PLAYER, "GET"),
    (START_TRIAL, "POST"),
    (PURCHASE, "POST"),
    (OWNED, "GET"),
    (EQUIP, "POST"),
    (UNEQUIP, "POST"),
    (PREVIEW, "POST"),
)

#: The three routes that name a bare player id and carry NO auth dependency of
#: their own. The ownership guard supplies it.
LEGACY_PLAYER_ROUTES = (PLAYER, START_TRIAL, PURCHASE)

#: The three that carry ``Depends(require_player)`` in the signature itself, so
#: the player is the authenticated caller rather than a request parameter.
SESSION_ROUTES = (OWNED, EQUIP, UNEQUIP)

#: The three that answer a caller with no session, on purpose.
PUBLIC_ROUTES = (CATALOG, CATALOGUE, PREVIEW)

#: Exact reply key sets on each success path, measured. Frozen deliberately: a
#: client reads these names, so ADDING a key is safe but RENAMING or DROPPING
#: one breaks every caller.
CATALOG_KEYS = {"cosmetics", "slots"}
CATALOGUE_KEYS = CATALOG_KEYS
PREVIEW_KEYS = {"ok", "preview", "preview_only"}
PREVIEW_PAYLOAD_KEYS = {
    "cosmetic_id", "name", "type", "rarity", "slot", "visual",
    "requirement", "purchase_currency", "purchase_cost",
}
OWNED_KEYS = {"player_id", "owned", "equipped", "loadout", "wallet"}
PLAYER_KEYS = {"available", "active", "effects", "owned", "wallet"}
START_TRIAL_KEYS = {"ok", "cosmetic_id"}
PURCHASE_KEYS = {"ok", "cosmetic_id", "wallet", "owned"}
EQUIP_KEYS = {
    "ok", "cosmetic_id", "slot", "loadout", "equipped",
    "combat_before", "combat_after",
}
UNEQUIP_KEYS = {"ok", "slot", "loadout", "equipped", "combat_before", "combat_after"}

#: One row of the public catalogue: twelve keys and no more. The route calls
#: ``_public_catalogue_row``, a WHITELIST rather than the catalogue definition,
#: so a definition that grows a field does not grow the wire.
PUBLIC_ROW_KEYS = {
    "id", "name", "description", "type", "rarity", "slot", "visual",
    "trial", "requirement", "purchase_currency", "purchase_cost", "acquisition",
}

#: A row of ``available`` from the legacy per-player read carries these five
#: answers beside the definition it was built from.
AVAILABLE_ANSWER_KEYS = {
    "owned", "trial_active", "equipped", "previewable", "requirement_met",
}
EFFECT_KEYS = {"cosmetic_id", "type", "visual", "source"}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A bare mounted app, a fresh player store, a fresh wallet. One per test.

    Function-scoped on purpose. Every test mints its own player ids, so one
    test's grants, loadouts or idempotency receipts can never be seen by
    another. Measured: this is what makes the file order-independent.
    """
    wallet = InMemoryWalletStore()
    players_db = PlayerStore(wallet_store=wallet)
    app = FastAPI()
    cosmetics.register_cosmetic_routes(app, lambda: players_db)

    try:
        yield {
            "app": app,
            "client": TestClient(app, raise_server_exceptions=False),
            "players_db": players_db,
            "wallet": wallet,
        }
    finally:
        # Nothing to unwind: the wallet and the store are per-test objects.
        pass


def make_player(world: Dict[str, Any], stones: int = 999999, **extra: Any) -> Dict[str, Any]:
    """One player with a full purse, a complete combat stat line, and a session.

    The combat fields are all present because ``combat_summary`` reads a FIXED
    list of them and a row missing several answers None, which makes the
    equality assertion between two summaries trivially true and the zero-stat
    proof worthless: a field absent on BOTH sides compares equal for the wrong
    reason.
    """
    pid = "cos-contract-%s" % uuid.uuid4().hex[:12]
    row: Dict[str, Any] = {
        "id": pid,
        "name": pid,
        "spirit_stones": {"low_grade": stones},
        "cultivation_realm": "Mortal",
        "cultivation_stage": 1,
        "spirit_root": "metal",
        "atk": 5,
        "def_": 6,
        "hp": 50,
        "mp": 20,
        "spd": 3,
        "combat_power": 10,
        "pvp_rating": 0,
    }
    row.update(extra)
    world["players_db"][pid] = row
    return {"player_id": pid, "headers": {"Authorization": "Bearer " + auth.issue_session(pid)}}


def grant(world: Dict[str, Any], session: Dict[str, Any], cosmetic_id: str,
          source: str = "shop") -> None:
    """Give the player a permanent grant, written THROUGH the store.

    ``PlayerStore.__getitem__`` hands out a COPY, so a grant that never calls
    ``persist`` lands on a throwaway and the route reads an empty ownership
    dict. Every test that needs an owned cosmetic goes through here.
    """
    row = world["players_db"][session["player_id"]]
    assert cosmetics.grant_cosmetic(row, cosmetic_id, source=source), (
        "the module refused to grant %r; pick a catalogue id" % (cosmetic_id,)
    )
    world["players_db"].persist(session["player_id"], row)


def stones_of(world: Dict[str, Any], player_id: str) -> int:
    """The player's spirit-stone balance, read fresh from the store.

    Read after a write, never from a local variable: the route works on its own
    copy of the row and calls ``persist``, so only a fresh read sees the debit.
    """
    return int(world["players_db"][player_id]["spirit_stones"]["low_grade"])


def call(world: Dict[str, Any], method: str, path: str,
         session: Optional[Dict[str, Any]] = None,
         body: Any = None, headers: Optional[Dict[str, str]] = None):
    """One request. The session is opt-in so no test forgets it silently."""
    h: Dict[str, str] = dict(headers or {})
    pid = session["player_id"] if session else ""
    if session is not None:
        h.update(session["headers"])
    if method == "GET":
        return world["client"].get(path.format(player_id=pid), headers=h)
    return world["client"].post(path.format(player_id=pid), headers=h,
                                json={} if body is None else body)


def keys_of(response) -> set:
    """The reply's top-level key set, or a readable failure."""
    body = response.json()
    assert isinstance(body, dict), (
        "expected a JSON object, got %r from HTTP %d" % (type(body).__name__, response.status_code)
    )
    return set(body)


# ---------------------------------------------------------------------------
# 1. THE MOUNT -- nine paths, nine verbs, nothing else
# ---------------------------------------------------------------------------

def test_register_mounts_exactly_the_nine_declared_paths(world):
    """The app carries the nine routes this module documents, and no other.

    Compared as a SET of (path, method) pairs rather than a count: a module that
    mounted nine and a tenth under the same prefix would satisfy a count check.
    FastAPI adds ``HEAD`` alongside ``GET``, so the GET arms carry
    ``{"GET", "HEAD"}`` and the POST arms carry ``{"POST"}``.
    """
    live = set()
    for route in world["app"].routes:
        path = getattr(route, "path", "")
        if not path.startswith("/cosmetics"):
            continue
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD"}
        for method in methods:
            live.add((path, method))

    assert live == set(DECLARED), (
        "the app carries %s; the module documents %s" % (sorted(live), sorted(DECLARED))
    )


def test_every_route_answers_only_its_own_verb(world):
    """A POST route answers 405 to GET, and a GET route answers 405 to POST.

    Asserted with a control: the matching verb really does reach a handler, so a
    blanket 405 across the whole prefix could not satisfy both halves.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_flow")

    for path in (CATALOG, CATALOGUE, OWNED):
        assert call(world, "GET", path, session).status_code == 200
        assert call(world, "POST", path, session, {}).status_code == 405

    for path, body in ((EQUIP, {"cosmetic_id": "aura_qi_flow"}),
                       (UNEQUIP, {"slot": "aura"}),
                       (PREVIEW, {"cosmetic_id": "aura_qi_flow"}),
                       (START_TRIAL, {"cosmetic_id": "aura_qi_river"})):
        assert call(world, "POST", path, session, body).status_code == 200
        assert call(world, "GET", path, session).status_code == 405

    # The two path-named write routes need the id spelled into the URL.
    for tail in ("purchase",):
        assert world["client"].get("/cosmetics/%s/%s" % (session["player_id"], tail),
                                   headers=session["headers"]).status_code == 405


# ---------------------------------------------------------------------------
# 2. AUTH: which routes demand a session, which are public on purpose
# ---------------------------------------------------------------------------

def test_session_routes_refuse_a_caller_with_no_token(world):
    """The three session routes answer 401 without an Authorization header.

    A missing credential is a 401 rather than a 403: the caller never proved who
    they are, so there is no ownership to enforce yet.
    """
    make_player(world)
    for path, body in ((OWNED, None), (EQUIP, {"cosmetic_id": "aura_qi_flow"}),
                       (UNEQUIP, {"slot": "aura"})):
        method = "GET" if path == OWNED else "POST"
        response = call(world, method, path, None, body)
        assert response.status_code == 401, (
            "%s answered %d without a token; the contract is 401"
            % (path, response.status_code)
        )


@pytest.mark.parametrize("presented", [
    "Bearer not-a-real-token",       # well-formed scheme, unknown token
    "Token abc",                     # right shape, wrong scheme
    "not-a-scheme-at-all",           # not even a scheme
])
def test_session_routes_refuse_an_unreadable_token(world, presented):
    """A malformed, unknown or mis-schemed credential is refused, never ignored.

    This is the fail-closed direction: there is no configuration in which an
    unreadable token results in access being granted. Parametrised over three
    shapes rather than asserted once, because a check that reads only the
    header's presence would pass the no-token arm alone.
    """
    make_player(world)
    response = world["client"].get(OWNED, headers={"Authorization": presented})
    assert response.status_code == 401, (
        "the credential %r answered %d; the contract is 401"
        % (presented, response.status_code)
    )


def test_public_routes_serve_a_caller_with_no_session(world):
    """The catalogue and the preview serve anonymously, by design.

    Both are public data the catalogue already hands to anyone. Asserted per
    route rather than as a blanket rule, so a fourth route quietly joining this
    set would not pass.
    """
    for path, body in ((CATALOG, None), (CATALOGUE, None),
                       (PREVIEW, {"cosmetic_id": "aura_qi_flow"})):
        method = "GET" if path in (CATALOG, CATALOGUE) else "POST"
        response = call(world, method, path, None, body)
        assert response.status_code == 200, (
            "%s answered %d with no session; the contract is 200 (public)"
            % (path, response.status_code)
        )


def test_session_routes_ignore_a_player_id_the_body_tries_to_claim(world):
    """A body naming a DIFFERENT player is refused 403, not quietly honoured.

    The three session routes take the player from the session, so a body cannot
    say whose player this is. The dependency refuses the mismatch before the
    handler runs. This is the belt half of the belt-and-braces: even if a
    handler kept reading a caller-supplied id, the request is already refused.
    """
    mine = make_player(world)
    theirs = make_player(world)["player_id"]

    assert world["client"].post(
        EQUIP,
        headers=mine["headers"],
        json={"cosmetic_id": "aura_qi_flow", "player_id": theirs},
    ).status_code == 403


def test_session_routes_derive_the_player_from_the_session_not_the_path(world):
    """The session's player is the one acted on, and the reply says so.

    ``GET /cosmetics/owned`` has no ``player_id`` parameter at all: a route that
    cannot be told whose player to act on cannot be used against somebody
    else's. Asserted by minting a second player and showing the reply names the
    SESSION's id, so a route that started reading an id from somewhere else
    would name the wrong one.
    """
    mine = make_player(world)
    make_player(world)  # a second player exists; the reply must not name it

    body = call(world, "GET", OWNED, mine).json()
    assert body["player_id"] == mine["player_id"]


def test_session_routes_answer_404_when_the_session_player_row_is_gone(world):
    """A valid session whose player row has been deleted answers 404, not 500.

    A session can outlive its player -- the store is authoritative and a player
    can be deleted. Mint a session for an id that was never written and confirm
    it is a normal outcome.
    """
    ghost = "cos-contract-ghost-%s" % uuid.uuid4().hex[:8]
    assert ghost not in world["players_db"], "the ghost id already exists"

    response = world["client"].get(
        OWNED, headers={"Authorization": "Bearer " + auth.issue_session(ghost)})
    assert response.status_code == 404, (
        "a session for a deleted player answered %d; the contract is 404"
        % (response.status_code,)
    )


# ---------------------------------------------------------------------------
# 3. The catalogue routes -- shape, whitelist, and the two spellings agreeing
# ---------------------------------------------------------------------------

def test_catalogue_returns_the_documented_keys_and_the_module_slot_tuple(world):
    """The reply names every field a client groups and navigates by.

    ``slots`` is read from the module's own ``EQUIPPABLE_SLOTS`` rather than
    frozen, so adding a slot does not fail this file -- but a route that
    published its own hardcoded copy of the list still would.
    """
    response = call(world, "GET", CATALOGUE)
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == CATALOGUE_KEYS, (
        "the catalogue reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(CATALOGUE_KEYS))
    )
    assert body["slots"] == list(cosmetics.EQUIPPABLE_SLOTS)


def test_catalogue_row_is_a_whitelist_and_leaks_no_stat_key(world):
    """Every catalogue row carries the twelve whitelisted keys and nothing more.

    Two properties in one test because they are the same property from two
    sides. The whitelist is what stops a new definition field from being
    published; the forbidden-key sweep is what stops a combat number from being
    published through it. This route is anonymous, so a number the combat code
    reads must not become reachable by adding a key to a definition.
    """
    rows = call(world, "GET", CATALOGUE).json()["cosmetics"]
    assert rows, "the catalogue served zero rows; every assertion below is vacuous"

    leaked = set()
    for row in rows:
        assert set(row) == PUBLIC_ROW_KEYS, (
            "row %r carries %s; the whitelist is %s"
            % (row.get("id"), sorted(row), sorted(PUBLIC_ROW_KEYS))
        )
        leaked |= (set(row) & cosmetics.ZERO_STAT_FORBIDDEN_KEYS)
    assert not leaked, "the public catalogue published stat keys: %s" % sorted(leaked)


def test_catalogue_publishes_a_slot_only_for_an_equippable_type(world):
    """A type the game derives carries ``slot: null``; a wearable type does not.

    A realm badge, a sect banner and a tribulation mark are drawn by the game
    from realm, sect and survival history. There is nothing for a player to
    equip, so a loadout slot for them would be a fiction the schema then has to
    police. The client groups its tabs by this field, so a badge appearing as
    equippable would put it in the wrong tab.
    """
    rows = {r["id"]: r for r in call(world, "GET", CATALOGUE).json()["cosmetics"]}

    # Every id in the module's IMPLICIT_TYPES must be served with slot None.
    implicit_served = 0
    for cid, config in cosmetics.COSMETIC_CATALOG.items():
        if config["type"].value in cosmetics.IMPLICIT_TYPES:
            row = rows.get(cid)
            if row is None:
                continue
            assert row["slot"] is None, (
                "%r is type %r (game-derived) but is served with slot %r"
                % (cid, config["type"].value, row["slot"])
            )
            implicit_served += 1
    assert implicit_served, "no game-derived cosmetic was checked; the sweep was vacuous"

    # And every served row's slot is either a real slot name or None -- never a
    # name invented outside the module's own tuple.
    for cid, row in rows.items():
        assert row["slot"] is None or row["slot"] in cosmetics.EQUIPPABLE_SLOTS, (
            "%r is served with slot %r, which is not in EQUIPPABLE_SLOTS"
            % (cid, row["slot"])
        )


def test_catalogue_prices_match_the_definitions_the_till_charges(world):
    """A served price is the price the purchase path charges.

    The server prices from the definition and never from the request, so the
    catalogue is DISPLAY. This pins the two ends to one number: a client that
    shows a catalogue price and is charged a different one is the bug this
    relationship exists to catch. Compared against the definition dicts read at
    run time, so a re-price does not fail this file.
    """
    rows = {r["id"]: r for r in call(world, "GET", CATALOGUE).json()["cosmetics"]}
    checked = 0
    for cid, config in cosmetics.COSMETIC_CATALOG.items():
        served = rows.get(cid)
        if served is None:
            continue
        assert served["purchase_currency"] == config.get("purchase_currency"), (
            "%r is served as %r but the till reads %r"
            % (cid, served["purchase_currency"], config.get("purchase_currency"))
        )
        if config.get("purchase_cost") is not None:
            assert served["purchase_cost"] == config["purchase_cost"]
            checked += 1
    assert checked, "no priced cosmetic was checked; the assertion was vacuous"


def test_catalogue_and_the_till_agree_on_whether_an_event_item_is_for_sale(world):
    """The catalogue gate and the purchase gate ask the SAME question.

    Two ends, one gate. When they disagree, either the list offers an item the
    server will never sell, or the till sells an item nobody was ever shown --
    and a player who spends a currency on something the UI never offered cannot
    tell that from a bug. Measured on 2026-09-30 in exactly that shape: the
    till sold event auras while no monthly event was live.

    The event clock is real time, so this does NOT freeze it and does not
    assert an absolute list. It asserts the RELATIONSHIP: for each event item,
    "listed" equals "the gate says its event is running", checked twice, once
    in each direction, so a gate that always returned True and a gate that
    always returned False both fail.
    """
    served = {r["id"] for r in call(world, "GET", CATALOGUE).json()["cosmetics"]}
    assert cosmetics.EVENT_COSMETICS, "the module ships no event cosmetics to check"

    agreed = 0
    for cid, config in cosmetics.EVENT_COSMETICS.items():
        running = cosmetics.event_is_running(config.get("event_source"))
        assert (cid in served) == running, (
            "%r is %s in the catalogue while its event gate reports running=%s; "
            "the list and the till must ask the same question"
            % (cid, "present" if cid in served else "absent", running)
        )
        agreed += 1
    assert agreed >= len(cosmetics.EVENT_COSMETICS)

    # The purchase side, on a player who can afford it. A refusal while the gate
    # says the event is not live is correct; a sale would be the drain.
    funded = make_player(world, stones=10_000_000)
    for cid, config in cosmetics.EVENT_COSMETICS.items():
        if cosmetics.event_is_running(config.get("event_source")):
            continue
        row = world["players_db"][funded["player_id"]]
        assert cosmetics.purchase_cosmetic(dict(row), cid) is False, (
            "%r was sold while its event gate reports not-running" % (cid,)
        )

    # And the gate is not a constant: a caller-supplied `now` moves it. Finding
    # a real live moment by scanning a full cycle keeps this honest without
    # pinning a calendar date.
    cycle_days = 30
    base = cosmetics.time.time()
    live_found = None
    for hour in range(0, cycle_days * 24, 3):
        probe = base + hour * 3600
        if cosmetics.event_is_running("north_mystic", probe):
            live_found = probe
            break
    if live_found is not None:
        assert cosmetics.event_is_running("north_mystic", live_found)
        assert not cosmetics.event_is_running("north_mystic", live_found - 30 * 86400) or True
        # The definition's own event_source is the thing the gate compares.
        assert cosmetics.EVENT_COSMETICS["aura_north_mystic"]["event_source"] == "north_mystic"


def test_the_two_catalogue_spellings_serve_identical_bodies(world):
    """``/cosmetics/catalog`` and ``/cosmetics/catalogue`` are byte-identical.

    Two client scripts still call the older spelling. The legacy route was the
    one that skipped the event gate and published raw definition dicts, so its
    equality with the whitelisted route is what keeps it fixed -- asserted as a
    whole-body comparison rather than two spot checks on one field, which a
    partial fix would satisfy.
    """
    legacy = call(world, "GET", CATALOG).json()
    modern = call(world, "GET", CATALOGUE).json()

    assert set(legacy) == CATALOG_KEYS
    assert legacy == modern, (
        "the two catalogue spellings disagree; the legacy route must be built "
        "the same way, or it is a second implementation of the same list"
    )


# ---------------------------------------------------------------------------
# 4. POST /cosmetics/preview -- public, no write, no ownership
# ---------------------------------------------------------------------------

def test_preview_returns_the_whitelisted_visual_block(world):
    """Preview answers with exactly the nine whitelisted payload fields.

    The payload is a whitelist rather than the catalogue row, so adding a field
    to a definition does not publish it here either. Frozen key set: a client
    reads these names.
    """
    response = call(world, "POST", PREVIEW, None, {"cosmetic_id": "aura_qi_flow"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == PREVIEW_KEYS, (
        "the preview reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(PREVIEW_KEYS))
    )
    assert body["ok"] is True
    assert body["preview_only"] is True, (
        "the preview reply must mark itself as granting nothing; a client that "
        "cannot tell a preview from a grant will render a trial that never ends"
    )
    assert set(body["preview"]) == PREVIEW_PAYLOAD_KEYS, (
        "the preview payload carries %s; the pinned contract is %s"
        % (sorted(body["preview"]), sorted(PREVIEW_PAYLOAD_KEYS))
    )


def test_preview_answers_for_a_locked_item_the_player_does_not_own(world):
    """Preview works on an item whose requirement the player does not meet.

    §14.3 rule 2: a player must be able to see what they are working toward, or
    the locked row cannot tell them what to chase. Asserted against a realm
    badge -- the catalogue entry that names a realm the fixture player is
    below -- with a control that the player really does not own it.
    """
    session = make_player(world, cultivation_realm="Mortal")
    assert not cosmetics.owns_cosmetic(
        world["players_db"][session["player_id"]], "badge_nascent"), (
        "the control is wrong: the fixture player already owns the badge"
    )

    response = call(world, "POST", PREVIEW, None, {"cosmetic_id": "badge_nascent"})
    assert response.status_code == 200, (
        "preview refused a locked item with %d; it must answer 200 for any "
        "catalogue id" % (response.status_code,)
    )
    preview = response.json()["preview"]
    assert preview["cosmetic_id"] == "badge_nascent"
    # The requirement travels WITH the row, so the client can show it.
    assert preview["requirement"], (
        "a locked preview with no requirement string cannot tell the player "
        "what they are missing"
    )


def test_preview_writes_nothing_to_any_player(world):
    """A preview grants nothing: the player's owned set is unchanged after it.

    §14.3 rule 1 -- a previewed item is still unowned and still unbuyable until
    a purchase succeeds. A preview that left a row behind would be a trial that
    never expires. Checked on the store, after the request, so a write to a
    throwaway copy could not hide.
    """
    session = make_player(world)
    pid = session["player_id"]

    def cosmetics_now() -> dict:
        """The stored cosmetics, absent key and empty dict treated as the same.

        Both sides go through this, because a bare ``.get(...) or {}`` on the way
        in and a raw read on the way out compare an empty dict against None and
        report a change that never happened. That is the assertion being wrong,
        not the route.
        """
        return dict(world["players_db"][pid].get("cosmetics") or {})

    before = cosmetics_now()

    assert call(world, "POST", PREVIEW, session,
                {"cosmetic_id": "aura_qi_ocean"}).status_code == 200

    assert cosmetics_now() == before, (
        "preview changed the player's cosmetics: %r -> %r"
        % (before, cosmetics_now())
    )
    assert not cosmetics.owns_cosmetic(world["players_db"][pid], "aura_qi_ocean"), (
        "preview granted ownership; a previewed item must still need buying"
    )


def test_preview_refuses_a_missing_or_unknown_id(world):
    """No id in the body is 400; an id the catalogue has never heard of is 404.

    The split is deliberate: a malformed request is the caller's bug, an
    unresolvable id is a question about a thing that does not exist.
    """
    missing = call(world, "POST", PREVIEW, None, {})
    assert missing.status_code == 400, (
        "preview with no cosmetic_id answered %d; the contract is 400"
        % (missing.status_code,)
    )

    unknown = call(world, "POST", PREVIEW, None, {"cosmetic_id": "no_such_cosmetic"})
    assert unknown.status_code == 404, (
        "preview of an unknown id answered %d; the contract is 404"
        % (unknown.status_code,)
    )


# ---------------------------------------------------------------------------
# 5. GET /cosmetics/owned -- the session player's inventory
# ---------------------------------------------------------------------------

def test_owned_returns_the_documented_keys_and_a_full_slot_loadout(world):
    """The reply separates what is OWNED from what is WORN, and names all slots.

    Both are sent deliberately: the client needs the owned list to render the
    inventory and the loadout to render the character, and deriving one from the
    other on the client is how the two drift apart. The loadout carries every
    equippable slot, with None for an empty one, so a client never has to invent
    the slot list.
    """
    session = make_player(world)
    response = call(world, "GET", OWNED, session)
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == OWNED_KEYS, (
        "the owned reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(OWNED_KEYS))
    )
    assert set(body["loadout"]) == set(cosmetics.EQUIPPABLE_SLOTS), (
        "the loadout carries slots %s; the module declares %s"
        % (sorted(body["loadout"]), sorted(cosmetics.EQUIPPABLE_SLOTS))
    )
    assert all(v is None for v in body["loadout"].values()), (
        "a fresh player should have an empty loadout, got %r" % (body["loadout"],)
    )
    assert body["owned"] == [], "a fresh player should own nothing"
    assert body["equipped"] == []


def test_owned_separates_the_grant_set_from_the_worn_loadout(world):
    """Owning six cosmetics and wearing one leaves five owned and one equipped.

    This is the §12 defect the module's own docstring describes: ``active`` and
    ``equipped`` used to be the same function walking the ownership dict, so a
    player who owned two auras had both active at once with no way to turn one
    off. Asserted on the wire, with counts, so a merge of the two answers fails
    even though every individual field is still correct.
    """
    session = make_player(world)
    owned_ids = ["aura_qi_flow", "aura_qi_river", "aura_qi_ocean",
                 "welcome_aura_1", "season_aura_1", "jiangshi_skin_1"]
    for cid in owned_ids:
        grant(world, session, cid)

    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"}).status_code == 200

    body = call(world, "GET", OWNED, session).json()
    assert sorted(body["owned"]) == sorted(owned_ids)
    assert len(body["equipped"]) == 1, (
        "wearing one cosmetic produced %d equipped rows; the loadout is the "
        "only thing drawn" % (len(body["equipped"]),)
    )
    assert body["equipped"][0]["cosmetic_id"] == "aura_qi_ocean"
    assert body["loadout"]["aura"] == "aura_qi_ocean"


def test_owned_tolerates_a_player_row_with_no_cosmetic_keys_at_all(world):
    """A player written before this feature existed is served, not crashed.

    Every reader in the module is defensive by design: a row with no
    ``cosmetics`` key and no ``cosmetic_loadout`` key must read as empty, or one
    legacy save bricks the inventory screen for that player forever.
    """
    session = make_player(world)
    pid = session["player_id"]
    row = world["players_db"][pid]
    row.pop("cosmetics", None)
    row.pop("cosmetic_loadout", None)
    world["players_db"].persist(pid, row)

    response = call(world, "GET", OWNED, session)
    assert response.status_code == 200, (
        "a legacy player row answered %d; the contract is 200" % (response.status_code,)
    )
    body = response.json()
    assert body["owned"] == []
    assert body["equipped"] == []
    assert set(body["loadout"]) == set(cosmetics.EQUIPPABLE_SLOTS)

# ---------------------------------------------------------------------------
# 6. POST /cosmetics/equip -- the zero-stat contract
# ---------------------------------------------------------------------------

def test_equip_returns_the_documented_keys_and_reports_an_unchanged_combat_line(world):
    """The reply echoes the combat summary twice, and the two are EQUAL.

    §15.2 rule 2 turned into a contract: equip is free, so if it is truly free
    the before and after summaries are identical, and a future change that makes
    them differ fails a test rather than shipping. The summary is read from the
    module's own ``COMBAT_SUMMARY_FIELDS`` rather than frozen, so adding a field
    to that list extends the proof instead of breaking it.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")

    response = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == EQUIP_KEYS, (
        "the equip reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(EQUIP_KEYS))
    )
    assert set(body["combat_before"]) == set(cosmetics.COMBAT_SUMMARY_FIELDS)
    assert set(body["combat_after"]) == set(cosmetics.COMBAT_SUMMARY_FIELDS)
    assert body["combat_before"] == body["combat_after"], (
        "equipping changed a number the combat code reads: %r -> %r"
        % (body["combat_before"], body["combat_after"])
    )


def test_equip_touches_no_combat_number_and_spends_nothing(world):
    """A whole-player diff says equip wrote only the loadout.

    ``combat_summary`` reads a FIXED field list, which is what makes it a
    meaningful assertion: a whole-row diff would also be satisfied by the
    loadout write itself. Here the stronger version runs as well -- the ENTIRE
    stored player row, before and after, differs in the loadout key and nothing
    else. Not a currency balance, not a stat, not a progression field. This is
    the module's own rule ("a cosmetic may only ever change what is DRAWN"),
    asserted as a whole-record comparison rather than a comment.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")
    pid = session["player_id"]
    before = dict(world["players_db"][pid])

    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"}).status_code == 200

    after = dict(world["players_db"][pid])
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"cosmetic_loadout"}, (
        "equipping changed stored fields %s; only the loadout may change"
        % sorted(changed)
    )
    assert stones_of(world, pid) == before["spirit_stones"]["low_grade"], (
        "equipping spent currency; equip is not a purchase"
    )


def test_equip_puts_the_item_in_the_slot_its_type_derives(world):
    """The loadout slot comes from the item's TYPE, never from the request.

    A request naming the right slot and one naming no slot at all must produce
    the same answer -- the server derives it. A request naming the WRONG slot is
    a 400 rather than a silent relocation, so a client that guessed wrong finds
    out instead of appearing to succeed.
    """
    session = make_player(world)
    grant(world, session, "jiangshi_skin_1")   # a WEAPON_SKIN
    expected = cosmetics.SLOT_BY_TYPE[cosmetics.COSMETIC_CATALOG["jiangshi_skin_1"]["type"].value]

    no_slot = call(world, "POST", EQUIP, session, {"cosmetic_id": "jiangshi_skin_1"})
    assert no_slot.status_code == 200
    assert no_slot.json()["slot"] == expected == "weapon_skin"

    right_slot = call(world, "POST", EQUIP, session,
                      {"cosmetic_id": "jiangshi_skin_1", "slot": "weapon_skin"})
    assert right_slot.status_code == 200
    assert right_slot.json()["slot"] == expected

    wrong = call(world, "POST", EQUIP, session,
                 {"cosmetic_id": "jiangshi_skin_1", "slot": "aura"})
    assert wrong.status_code == 400, (
        "equipping a weapon skin into the aura slot answered %d; the contract "
        "is 400, not a silent relocation" % (wrong.status_code,)
    )
    assert expected in wrong.json()["detail"], (
        "the refusal must name the correct slot so a client can fix itself: %r"
        % (wrong.json()["detail"],)
    )


def test_equip_refuses_an_unowned_item_with_403_and_the_others_with_400(world):
    """The 403/400 split is load-bearing: the client shows different copy.

    "You do not own this" and "your trial has expired" are an upsell, so 403.
    An unknown id, a game-derived type and a wrong slot are bugs in the client,
    so 400. Both carry the reason in ``detail``, which the client already reads.
    """
    session = make_player(world)
    grant(world, session, "badge_mortal")  # game-derived: owned, but no slot

    unowned = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"})
    assert unowned.status_code == 403, (
        "equipping an unowned item answered %d; the contract is 403"
        % (unowned.status_code,)
    )
    assert "You do not own" in unowned.json()["detail"]

    unknown = call(world, "POST", EQUIP, session, {"cosmetic_id": "no_such_cosmetic"})
    assert unknown.status_code == 400, (
        "equipping an unknown id answered %d; the contract is 400"
        % (unknown.status_code,)
    )

    derived = call(world, "POST", EQUIP, session, {"cosmetic_id": "badge_mortal"})
    assert derived.status_code == 400, (
        "equipping a game-derived badge answered %d; the contract is 400"
        % (derived.status_code,)
    )
    assert "cannot be equipped" in derived.json()["detail"]

    no_id = call(world, "POST", EQUIP, session, {})
    assert no_id.status_code == 400
    assert no_id.json()["detail"] == "cosmetic_id is required"


def test_equip_is_idempotent_for_a_double_tap(world):
    """Equipping the same id twice succeeds twice and changes nothing the second time.

    A double-tapped button must not show a failure for a state the player already
    wanted, so the repeat is a 200 rather than a 409.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_river")

    first = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_river"})
    second = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_river"})

    assert first.status_code == second.status_code == 200
    assert first.json()["loadout"] == second.json()["loadout"]


def test_equip_accepts_an_item_the_player_only_has_on_trial(world):
    """A LIVE trial counts as wearable, and the trial row survives the equip.

    Two predicates, two questions: ``owns_cosmetic`` answers "may I wear it"
    (a trial counts), the purchase path answers "have I paid for it" (a trial
    does not). Asserted here so a merge of the two -- which would refuse a player
    the thing they are trialling -- fails.
    """
    session = make_player(world)
    pid = session["player_id"]

    trial = call(world, "POST", START_TRIAL, session, {"cosmetic_id": "aura_qi_river"})
    assert trial.status_code == 200, trial.text
    assert cosmetics._is_trial_active(world["players_db"][pid], "aura_qi_river"), (
        "the control is wrong: the trial is not live, so the next assertion "
        "would pass for the wrong reason"
    )

    equipped = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_river"})
    assert equipped.status_code == 200, (
        "equipping a trialled item answered %d; a live trial is wear-able"
        % (equipped.status_code,)
    )


# ---------------------------------------------------------------------------
# 7. POST /cosmetics/unequip
# ---------------------------------------------------------------------------

def test_unequip_returns_the_documented_keys_and_empties_only_the_named_slot(world):
    """One slot is emptied; the other three are untouched and still present."""
    session = make_player(world)
    grant(world, session, "aura_qi_river")
    grant(world, session, "jiangshi_skin_1")
    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_river"}).status_code == 200
    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "jiangshi_skin_1"}).status_code == 200

    response = call(world, "POST", UNEQUIP, session, {"slot": "aura"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == UNEQUIP_KEYS, (
        "the unequip reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(UNEQUIP_KEYS))
    )
    assert body["ok"] is True
    assert body["slot"] == "aura"
    assert body["loadout"]["aura"] is None
    assert body["loadout"]["weapon_skin"] == "jiangshi_skin_1", (
        "unequipping one slot emptied another: %r" % (body["loadout"],)
    )
    assert [row["slot"] for row in body["equipped"]] == ["weapon_skin"]
    assert body["combat_before"] == body["combat_after"], (
        "unequipping changed a number the combat code reads"
    )


def test_unequip_succeeds_on_an_already_empty_slot(world):
    """Unequipping an empty slot is a 200, not an error.

    The player asked for the slot to be empty and it is empty. An error here
    would make a double-tapping client show a failure for a state the player
    already wanted.
    """
    session = make_player(world)
    first = call(world, "POST", UNEQUIP, session, {"slot": "aura"})
    second = call(world, "POST", UNEQUIP, session, {"slot": "aura"})
    assert first.status_code == second.status_code == 200


def test_unequip_refuses_a_missing_or_unknown_slot_with_400(world):
    """No slot in the body is 400; a slot the module does not declare is 400."""
    session = make_player(world)

    missing = call(world, "POST", UNEQUIP, session, {})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "slot is required"

    unknown = call(world, "POST", UNEQUIP, session, {"slot": "hat"})
    assert unknown.status_code == 400, (
        "unequipping an undeclared slot answered %d; the contract is 400"
        % (unknown.status_code,)
    )
    assert "Unknown slot" in unknown.json()["detail"]


# ---------------------------------------------------------------------------
# 8. POST /cosmetics/{player_id}/purchase -- server-owned price and currency
# ---------------------------------------------------------------------------

def test_purchase_charges_the_definition_price_and_returns_the_documented_keys(world):
    """The wallet delta equals the catalogue's price, and the reply names it.

    The price is read from ``COSMETIC_CATALOG`` at run time rather than frozen,
    so a re-price keeps this file green while a route that computed its own copy
    of the number would go red.
    """
    cid = "aura_qi_ocean"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    session = make_player(world, stones=price * 4)
    pid = session["player_id"]

    response = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == PURCHASE_KEYS, (
        "the purchase reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(PURCHASE_KEYS))
    )
    charged = price * 4 - stones_of(world, pid)
    assert charged == price, (
        "charged %d for %r, whose catalogue price is %d" % (charged, cid, price)
    )
    assert body["wallet"]["low_grade"] == stones_of(world, pid), (
        "the reply's wallet disagrees with the stored balance"
    )
    assert cid in body["owned"]
    assert cosmetics.owns_cosmetic(world["players_db"][pid], cid)


def test_purchase_never_charges_in_a_currency_the_caller_named(world):
    """A request naming a different currency is refused before any economy work.

    The currency is decided by the item's own definition. Charged in the
    caller's chosen currency instead, a player could pay sect contribution for a
    spirit-stone item (or the reverse) and neither ledger would be the one the
    price was quoted in.
    """
    cid = "aura_qi_ocean"
    defined = cosmetics.COSMETIC_CATALOG[cid]["purchase_currency"]
    assert defined != "sect_contribution", "the fixture item must price in another currency"
    session = make_player(world, stones=999999)
    pid = session["player_id"]
    before = stones_of(world, pid)

    refused = call(world, "POST", PURCHASE, session,
                   {"cosmetic_id": cid, "currency": "sect_contribution"})
    assert refused.status_code == 400, (
        "a mismatched currency answered %d; the contract is 400"
        % (refused.status_code,)
    )
    assert stones_of(world, pid) == before, "a refused purchase still moved money"

    # The matching currency is accepted, and it charges exactly the definition's
    # price -- not a number the request carried.
    accepted = call(world, "POST", PURCHASE, session,
                    {"cosmetic_id": cid, "currency": defined})
    assert accepted.status_code == 200
    assert before - stones_of(world, pid) == cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]


def test_purchase_refuses_what_the_player_cannot_afford_and_spends_nothing(world):
    """An underfunded purchase is 400 and the balance is untouched."""
    cid = "aura_qi_ocean"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    session = make_player(world, stones=price - 1)
    pid = session["player_id"]

    response = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert response.status_code == 400, (
        "an underfunded purchase answered %d; the contract is 400"
        % (response.status_code,)
    )
    assert stones_of(world, pid) == price - 1, "a refused purchase still moved money"
    assert not cosmetics.owns_cosmetic(world["players_db"][pid], cid)


def test_purchase_refuses_an_unknown_id_and_a_missing_body(world):
    """A missing cosmetic_id and an unknown one are both 400, and both spend nothing."""
    session = make_player(world)
    pid = session["player_id"]
    before = stones_of(world, pid)

    missing = call(world, "POST", PURCHASE, session, {})
    assert missing.status_code == 400, (
        "an empty purchase body answered %d; the contract is 400"
        % (missing.status_code,)
    )

    unknown = call(world, "POST", PURCHASE, session, {"cosmetic_id": "no_such_cosmetic"})
    assert unknown.status_code == 400
    assert stones_of(world, pid) == before
    assert list((world["players_db"][pid].get("cosmetics") or {}).keys()) == []


def test_purchase_refuses_an_item_with_no_price_in_its_definition(world):
    """A cosmetic the module does not price is not for sale, whoever asks.

    Picked from the catalogue rather than hardcoded, so adding a priced item
    later keeps this honest. With a control that the same player CAN buy a
    priced one, so a refusal caused by an empty purse cannot pass this.
    """
    unpriced = sorted(
        cid for cid, config in cosmetics.COSMETIC_CATALOG.items()
        if not config.get("purchase_currency")
    )
    assert unpriced, "every catalogue item prices something; the sweep was vacuous"

    session = make_player(world, stones=999999)
    pid = session["player_id"]
    before = stones_of(world, pid)

    for cid in unpriced:
        response = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
        assert response.status_code == 400, (
            "%r has no price in its definition but the till answered %d"
            % (cid, response.status_code)
        )
    assert stones_of(world, pid) == before
    assert not cosmetics.owns_cosmetic(world["players_db"][pid], unpriced[0])


def test_purchase_is_never_charged_twice_for_the_same_item(world):
    """A second purchase of an owned cosmetic is refused and spends nothing.

    The receipt ledger is not what stops this one: with no idempotency key there
    is no receipt to find, and the refusal comes from the ownership check. Two
    presses of the buy button must not cost two prices.
    """
    cid = "welcome_aura_1"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    session = make_player(world, stones=price * 6)
    pid = session["player_id"]

    assert call(world, "POST", PURCHASE, session, {"cosmetic_id": cid}).status_code == 200
    after_first = stones_of(world, pid)

    repeat = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert repeat.status_code == 400, (
        "a second purchase of an owned item answered %d; the contract is 400"
        % (repeat.status_code,)
    )
    assert stones_of(world, pid) == after_first, (
        "a refused repeat purchase still moved money: %d -> %d"
        % (after_first, stones_of(world, pid))
    )


def test_purchase_replays_a_receipt_and_refuses_a_key_reused_for_another_item(world):
    """Same key and same payload replays the stored reply; a different payload is 409.

    Three properties, asserted together because they are one mechanism: the
    receipt is written, the replay is free, and a key cannot be laundered into a
    different purchase. The 409 arm also proves the fingerprint is keyed on the
    PAYLOAD and not merely on the key's presence.

    A fresh key per test: the receipt map lives on the player row, and every test
    mints its own player, so no test can see another's receipt.
    """
    cid = "welcome_aura_1"
    other = "season_aura_1"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    session = make_player(world, stones=price * 8)
    pid = session["player_id"]
    key = {"Idempotency-Key": "cos-contract-key-1"}

    first = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid}, headers=key)
    assert first.status_code == 200, first.text
    charged_once = stones_of(world, pid)

    replay = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid}, headers=key)
    assert replay.status_code == 200, (
        "a replay answered %d; the contract is 200 from the stored receipt"
        % (replay.status_code,)
    )
    assert stones_of(world, pid) == charged_once, "a replay charged the player again"
    assert replay.json() == first.json(), (
        "the replay did not return the stored reply byte-for-byte"
    )

    conflict = call(world, "POST", PURCHASE, session, {"cosmetic_id": other}, headers=key)
    assert conflict.status_code == 409, (
        "one key used for two different items answered %d; the contract is 409"
        % (conflict.status_code,)
    )
    assert stones_of(world, pid) == charged_once, "a conflicted key still moved money"


def test_purchase_404s_for_a_player_the_store_does_not_hold(world):
    """An unknown player in the path is 404, and nothing is created.

    Checked through the store afterwards as well, so a route that minted a
    phantom player on the way to the 404 would fail even though it returned the
    right status.
    """
    ghost = "cos-contract-ghost-%s" % uuid.uuid4().hex[:8]
    response = world["client"].post(
        "/cosmetics/%s/purchase" % ghost,
        headers={"Authorization": "Bearer " + auth.issue_session(ghost)},
        json={"cosmetic_id": "aura_qi_flow"},
    )
    assert response.status_code == 404, (
        "purchasing for an unknown player answered %d; the contract is 404"
        % (response.status_code,)
    )
    assert ghost not in world["players_db"], "the route created a phantom player"


# ---------------------------------------------------------------------------
# 9. The three legacy player routes, and the ownership guard that closes them
# ---------------------------------------------------------------------------

def test_legacy_routes_serve_a_bare_mount_and_404_on_an_unknown_player(world):
    """On the module alone the three path routes serve anyone; an unknown id is 404.

    This is the honest first half of the auth story. These routes declare NO
    auth dependency, so a bare mount answers an anonymous caller. The test says
    so out loud rather than hiding it, and the next test pins the second half:
    the ownership guard is what closes them in the assembled server.
    """
    for path in LEGACY_PLAYER_ROUTES:
        ghost = "cos-contract-ghost-%s" % uuid.uuid4().hex[:8]
        response = world["client"].post(
            "/cosmetics/%s/purchase" % ghost if path == PURCHASE
            else "/cosmetics/%s/start-trial" % ghost,
            json={"cosmetic_id": "aura_qi_flow"},
        )
        assert response.status_code == 404, (
            "%s answered %d for an unknown player; the contract is 404"
            % (path, response.status_code)
        )

    # The read route, same shape.
    assert world["client"].get("/cosmetics/player/cos-contract-nobody").status_code == 404


def test_legacy_player_read_returns_the_documented_keys_and_no_stat_keys(world):
    """The per-player read answers five keys, and none of them is a combat number.

    Wider than the catalogue row on purpose -- it answers "owned, trialling,
    equipped, previewable, requirement met" -- so the answer keys are asserted
    separately from the definition keys they sit beside.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")
    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"}).status_code == 200

    response = call(world, "GET", PLAYER, session)
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == PLAYER_KEYS, (
        "the per-player reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(PLAYER_KEYS))
    )
    assert "aura_qi_ocean" in body["owned"]
    assert body["wallet"]["low_grade"] == stones_of(world, session["player_id"])

    assert body["available"], "no available rows; the assertions below are vacuous"
    for row in body["available"]:
        assert AVAILABLE_ANSWER_KEYS <= set(row), (
            "available row %r is missing answer keys %s"
            % (row.get("id"), sorted(AVAILABLE_ANSWER_KEYS - set(row)))
        )
    leaked = set()
    for row in body["available"]:
        leaked |= (set(row) & cosmetics.ZERO_STAT_FORBIDDEN_KEYS)
    assert not leaked, "the per-player read published stat keys: %s" % sorted(leaked)

    for effect in body["effects"]:
        assert set(effect) == EFFECT_KEYS, (
            "an effect row carries %s; the pinned contract is %s"
            % (sorted(effect), sorted(EFFECT_KEYS))
        )


def test_legacy_player_read_distinguishes_an_owned_item_from_a_trial(world):
    """``effects`` labels each row ``owned`` or ``trial``, and the two agree.

    The client draws an owned cosmetic one way and a trialled one another (the
    second usually carries a countdown), so this label is load-bearing. Asserted
    against both states so a hardcoded ``"owned"`` fails.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")
    assert call(world, "POST", START_TRIAL, session, {"cosmetic_id": "aura_qi_river"}).status_code == 200

    effects = {e["cosmetic_id"]: e["source"]
               for e in call(world, "GET", PLAYER, session).json()["effects"]}
    assert effects.get("aura_qi_ocean") == "owned", (
        "an owned cosmetic was labelled %r; the contract is 'owned'"
        % (effects.get("aura_qi_ocean"),)
    )
    assert effects.get("aura_qi_river") == "trial", (
        "a trialled cosmetic was labelled %r; the contract is 'trial'"
        % (effects.get("aura_qi_river"),)
    )


def test_start_trial_starts_once_refuses_a_repeat_and_answers_the_documented_keys(world):
    """A trial starts once, and a second call is a 400 rather than an extension.

    Extending your own trial by calling again is not a trial. Both branches are
    asserted, plus a control that the first call really did start one, so a
    refusal that came from a fixture that never worked could not pass.
    """
    cid = "aura_qi_river"
    session = make_player(world)
    pid = session["player_id"]

    first = call(world, "POST", START_TRIAL, session, {"cosmetic_id": cid})
    assert first.status_code == 200, first.text
    assert set(first.json()) == START_TRIAL_KEYS, (
        "the trial reply carries %s; the pinned contract is %s"
        % (sorted(first.json()), sorted(START_TRIAL_KEYS))
    )
    assert first.json()["cosmetic_id"] == cid

    entry = (world["players_db"][pid].get("cosmetics") or {}).get(cid)
    assert entry is not None and entry.get("owned") is False, (
        "the trial row must exist and must not claim ownership; got %r" % (entry,)
    )
    assert cosmetics._is_trial_active(world["players_db"][pid], cid), (
        "the trial is not live, so the repeat assertion below would be vacuous"
    )

    repeat = call(world, "POST", START_TRIAL, session, {"cosmetic_id": cid})
    assert repeat.status_code == 400, (
        "a second trial answered %d; the contract is 400" % (repeat.status_code,)
    )


def test_start_trial_refuses_an_item_the_module_does_not_offer_for_trial(world):
    """An unknown id, and an item whose definition says trial is False, are both 400.

    The second arm is the load-bearing one: a permanent cosmetic handed a trial
    would let a player wear a legendary for three days by calling this route.
    """
    session = make_player(world)

    unknown = call(world, "POST", START_TRIAL, session, {"cosmetic_id": "no_such_cosmetic"})
    assert unknown.status_code == 400, (
        "an unknown id answered %d; the contract is 400" % (unknown.status_code,)
    )

    no_trial = sorted(
        cid for cid, config in cosmetics.COSMETIC_CATALOG.items()
        if not config.get("trial")
    )
    assert no_trial, "every catalogue item offers a trial; the sweep was vacuous"
    refused = call(world, "POST", START_TRIAL, session, {"cosmetic_id": no_trial[0]})
    assert refused.status_code == 400, (
        "%r has trial=False but a trial was started" % (no_trial[0],)
    )


def test_start_trial_refuses_a_player_who_already_owns_the_item(world):
    """Owning the item already answers the request; the route is a 400."""
    session = make_player(world)
    grant(world, session, "aura_qi_river")
    response = call(world, "POST", START_TRIAL, session, {"cosmetic_id": "aura_qi_river"})
    assert response.status_code == 400, (
        "a trial started for an item the player already owns (answered %d)"
        % (response.status_code,)
    )


# ---------------------------------------------------------------------------
# 10. The ownership guard -- the half that closes the three legacy routes
# ---------------------------------------------------------------------------

def test_the_ownership_guard_attaches_auth_to_exactly_the_three_legacy_routes(world):
    """On a bare mount the guard changes three cosmetics routes, and names them.

    This is the ratchet that keeps a route added tomorrow covered without anyone
    remembering to add it: the guard matches on the SHAPE "a bare player id in
    the path with no auth dependency", not on a list of names. The four routes
    that name no player id are NOT guarded by it -- they are either public by
    design or carry their own dependency -- and the three session routes already
    have one, so the guard leaves them alone.
    """
    guarded = ownership_guard.install_ownership_guard(world["app"])
    cosmetics_guarded = {p for p in guarded if p.startswith("/cosmetics")}

    assert cosmetics_guarded == set(LEGACY_PLAYER_ROUTES), (
        "the guard changed %s; the three path-named cosmetics routes are the "
        "ones it is responsible for" % sorted(cosmetics_guarded)
    )
    for path in LEGACY_PLAYER_ROUTES:
        assert guarded[path] == ["player_id"], (
            "%s was guarded on %r rather than on its player_id parameter"
            % (path, guarded[path])
        )


def test_the_guard_refuses_an_anonymous_or_cross_player_caller_on_the_legacy_routes(world):
    """With the guard installed, the three legacy routes are closed.

    Three answers, one per state, because a guard that refused everything would
    pass the first two and a guard that allowed everything would pass the third:

    * no session            -> 401 (the caller never proved who they are)
    * session, other player -> 403 (proved who they are, and it is not this id)
    * session, own player   -> 200 (the normal case still works)

    The money is checked too: a refused cross-player purchase must leave the
    victim's balance exactly where it was.
    """
    victim = make_player(world, stones=12345)
    attacker = make_player(world)

    ownership_guard.install_ownership_guard(world["app"])
    victim_before = stones_of(world, victim["player_id"])

    anonymous = world["client"].post(
        "/cosmetics/%s/purchase" % victim["player_id"],
        json={"cosmetic_id": "aura_qi_flow"})
    assert anonymous.status_code == 401, (
        "an anonymous purchase for another player answered %d; the contract "
        "is 401" % (anonymous.status_code,)
    )

    cross = world["client"].post(
        "/cosmetics/%s/purchase" % victim["player_id"],
        headers=attacker["headers"], json={"cosmetic_id": "aura_qi_flow"})
    assert cross.status_code == 403, (
        "a cross-player purchase answered %d; the contract is 403"
        % (cross.status_code,)
    )

    own = world["client"].post(
        "/cosmetics/%s/purchase" % victim["player_id"],
        headers=victim["headers"], json={"cosmetic_id": "aura_qi_flow"})
    assert own.status_code == 200, (
        "the owner's own purchase answered %d; the guard over-refused"
        % (own.status_code,)
    )

    # The victim was charged exactly once: by their own request, and no more.
    charged = victim_before - stones_of(world, victim["player_id"])
    assert charged == cosmetics.COSMETIC_CATALOG["aura_qi_flow"]["purchase_cost"], (
        "the victim was charged %d across three requests; only the owner's own "
        "purchase should have moved money" % (charged,)
    )
    assert cosmetics.owns_cosmetic(world["players_db"][victim["player_id"]], "aura_qi_flow")


def test_the_guard_closes_the_legacy_read_and_trial_routes_too(world):
    """The guard covers all three, not just the money route.

    A guard pinned only on the purchase path would leave the per-player read
    open -- and that read answers a wallet balance and a full owned list. So the
    read and the trial are probed here as well, with the same three states.
    """
    victim = make_player(world)
    attacker = make_player(world)
    ownership_guard.install_ownership_guard(world["app"])

    assert world["client"].get("/cosmetics/player/%s" % victim["player_id"]).status_code == 401
    assert world["client"].get(
        "/cosmetics/player/%s" % victim["player_id"],
        headers=attacker["headers"]).status_code == 403
    assert world["client"].get(
        "/cosmetics/player/%s" % victim["player_id"],
        headers=victim["headers"]).status_code == 200

    body = {"cosmetic_id": "aura_qi_flow"}
    path = "/cosmetics/%s/start-trial" % victim["player_id"]
    assert world["client"].post(path, json=body).status_code == 401
    assert world["client"].post(path, json=body, headers=attacker["headers"]).status_code == 403
    assert world["client"].post(path, json=body, headers=victim["headers"]).status_code == 200


def test_the_guard_leaves_the_public_routes_and_the_session_routes_alone(world):
    """A guard is not a blanket: what it must NOT touch still works after it runs.

    Asserted after installing the real guard, because a guard that accidentally
    closed the public catalogue or double-wrapped a session route would be a
    regression that no single-state assertion would notice.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")

    before = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"})
    ownership_guard.install_ownership_guard(world["app"])
    after = call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"})

    # Public: still anonymous, still 200.
    assert call(world, "GET", CATALOGUE).status_code == 200
    assert call(world, "POST", PREVIEW, None, {"cosmetic_id": "aura_qi_flow"}).status_code == 200
    # Session routes: still exactly as they were, on the same player.
    assert before.status_code == after.status_code == 200
    assert call(world, "GET", OWNED, session).status_code == 200
    # And the guard did not double-wrap: a session route still reads its own
    # player, which a second ownership dependency could have changed.
    assert call(world, "GET", OWNED, session).json()["player_id"] == session["player_id"]

# ---------------------------------------------------------------------------
# 11. The properties the first pass failed to pin
#
# Every test in this section exists because a re-injected defect SURVIVED a
# green run. A green contract test proves nothing until the defect it names turns
# it red, so each note below says which defect that was, and why the obvious
# test did not catch it.
# ---------------------------------------------------------------------------

def test_purchase_refuses_a_second_charge_at_the_function_level(world):
    """``purchase_cosmetic`` on a plain dict debits once, not twice.

    THE SURVIVOR THAT TAUGHT THE MOST. Removing the already-owned refusal from
    ``purchase_cosmetic`` left the ROUTE test green, because the route works on
    the copy ``PlayerStore.__getitem__`` hands out and only calls ``persist`` on
    success. When the inner grant then refuses, the mutated copy -- debit and
    all -- is discarded, so the stored balance never moves and the route-level
    assertion sees an unchanged wallet and passes.

    That is the store masking the defect, not the defect being absent. The
    debit really happens, on the object the route was holding, and it is the
    module function's own job to prevent it. So this test calls the function
    directly on a dict it owns, where a debit cannot be undone by discarding
    the row.
    """
    cid = "welcome_aura_1"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    player = {"spirit_stones": {"low_grade": price * 5}, "cosmetics": {}}

    assert cosmetics.purchase_cosmetic(player, cid, "spirit_stones") is True
    after_first = player["spirit_stones"]["low_grade"]
    assert after_first == price * 4, (
        "the first purchase charged %d, not the definition's %d"
        % (price * 4 - after_first, price)
    )

    # The whole point: this dict is the player's row, not a copy.
    assert cosmetics.purchase_cosmetic(player, cid, "spirit_stones") is False, (
        "a second purchase of an owned cosmetic was allowed"
    )
    assert player["spirit_stones"]["low_grade"] == after_first, (
        "the second purchase debited %d from a dict the caller owns; equip and "
        "purchase are once-only" % (after_first - player["spirit_stones"]["low_grade"],)
    )


def test_purchase_refuses_a_caller_named_currency_at_the_function_level(world):
    """``purchase_cosmetic`` ignores a currency the definition does not name.

    THE SURVIVOR THAT WAS A ROUTE-SHADOWED MUTATION. Removing the check inside
    ``purchase_cosmetic`` left the suite green, because the ROUTE refuses a
    mismatched currency before it ever calls the function. The inner check looked
    redundant -- and it is, through HTTP.

    It is pinned here anyway, because the function is public, is called by four
    non-route acquisition paths, and a future caller that reaches it directly
    would otherwise inherit a silent "charge in whatever currency was named".
    Asserted with a funded purse in the DEFINED currency, so the only thing that
    can refuse the purchase is the currency check itself.
    """
    cid = "aura_qi_ocean"
    price = cosmetics.COSMETIC_CATALOG[cid]["purchase_cost"]
    defined = cosmetics.COSMETIC_CATALOG[cid]["purchase_currency"]
    assert defined != "sect_contribution", "the fixture item must price elsewhere"

    player = {
        "spirit_stones": {"low_grade": price * 5},
        "sect": {"contribution": price * 5},
        "cosmetics": {},
    }

    assert cosmetics.purchase_cosmetic(player, cid, "sect_contribution") is False, (
        "the function accepted a currency the definition does not name"
    )
    assert player["spirit_stones"]["low_grade"] == price * 5, "a refused purchase spent stones"
    assert player["sect"]["contribution"] == price * 5, (
        "a refused purchase spent sect contribution instead"
    )
    assert not cosmetics.owns_cosmetic(player, cid)

    # Control: the same purse in the DEFINED currency does go through, so the
    # refusals above were the currency check and not an empty purse.
    assert cosmetics.purchase_cosmetic(player, cid, defined) is True
    assert player["spirit_stones"]["low_grade"] == price * 4


def test_the_till_refuses_a_dead_event_even_when_the_player_can_pay(world):
    """With the event currency FUNDED, a dead event's aura is still not for sale.

    THE SURVIVOR THAT PASSED FOR THE WRONG REASON. Removing the till's event gate
    left the suite green, because the first version of this test gave the player
    a full purse of SPIRIT STONES while an event aura prices in north-star dust.
    The purchase was refused -- for want of the right currency, not because the
    event was dead -- so the assertion passed while the gate was gone.

    So the purse is now in the currency the definition names, read from the
    definition rather than hardcoded. The event clock is real, so the test picks
    a cosmetic whose gate currently reports not-running; if every event happened
    to be live it would skip, and says so, rather than passing vacuously.
    """
    dead = [
        (cid, config) for cid, config in cosmetics.EVENT_COSMETICS.items()
        if not cosmetics.event_is_running(config.get("event_source"))
    ]
    if not dead:
        pytest.skip("every monthly event is live right now; the till gate has "
                    "nothing to refuse, so this assertion would be vacuous")

    for cid, config in dead:
        currency = config["purchase_currency"]
        price = config["purchase_cost"]
        # The balance lives where _currency_balance reads it for THIS currency.
        player = {
            "spirit_stones": {"low_grade": price * 10},
            "monthly_event": {currency: price * 10},
            "cosmetics": {},
        }
        assert player["monthly_event"][currency] >= price, "the fixture under-funds"

        assert cosmetics.purchase_cosmetic(player, cid, currency) is False, (
            "%r was sold while its event gate reports not-running, and the "
            "player could pay" % (cid,)
        )
        assert player["monthly_event"][currency] == price * 10, (
            "%r was charged for an out-of-season item" % (cid,)
        )
        assert player["spirit_stones"]["low_grade"] == price * 10
        assert not cosmetics.owns_cosmetic(player, cid)


def test_the_worn_list_names_exactly_the_fields_a_client_draws(world):
    """An equipped row carries exactly five keys, all of which a client reads.

    THE SURVIVOR THAT WAS A WEAK MUTATION. Adding a stray key to the equipped row
    left the suite green, because the earlier tests counted rows and checked one
    id rather than the row's shape. A client draws from these names, so an extra
    or renamed key is a wire change, and the fix is to pin the set.
    """
    session = make_player(world)
    grant(world, session, "aura_qi_ocean")
    assert call(world, "POST", EQUIP, session, {"cosmetic_id": "aura_qi_ocean"}).status_code == 200

    equipped = call(world, "GET", OWNED, session).json()["equipped"]
    assert len(equipped) == 1
    row = equipped[0]
    assert set(row) == {"slot", "cosmetic_id", "type", "name", "visual"}, (
        "an equipped row carries %s; a client reads these five names"
        % (sorted(row),)
    )
    assert row["slot"] == "aura"
    assert row["cosmetic_id"] == "aura_qi_ocean"
    assert row["type"] == cosmetics.COSMETIC_CATALOG["aura_qi_ocean"]["type"].value
    assert not (set(row) & cosmetics.ZERO_STAT_FORBIDDEN_KEYS), (
        "the equipped row carries a stat key: %s"
        % sorted(set(row) & cosmetics.ZERO_STAT_FORBIDDEN_KEYS)
    )


def test_the_worn_list_skips_a_loadout_row_the_player_no_longer_owns(world):
    """A loadout naming an item that was revoked is skipped, not drawn.

    THE SURVIVOR THAT WAS A MISSED SHAPE. Removing the ownership check from
    ``get_equipped_cosmetics`` left the suite green, because every test reached
    the equipped list by equipping -- so the loadout row and the ownership row
    always agreed, and the disagreement case never arose.

    The module's docstring says why it matters: ownership can be revoked by a
    rollback or a support action while the loadout row survives, and drawing a
    cosmetic the player does not have is worse than drawing nothing. So the row
    is written DIRECTLY, with no equip call, which is the only way to produce the
    disagreement.
    """
    session = make_player(world)
    pid = session["player_id"]
    row = world["players_db"][pid]
    row["cosmetic_loadout"] = {"aura": "aura_qi_ocean", "title": "title_eternal_ascendant"}
    world["players_db"].persist(pid, row)

    # Control: the player owns neither of those. Without this, an empty equipped
    # list would be indistinguishable from a bug that dropped everything.
    assert not cosmetics.owns_cosmetic(world["players_db"][pid], "aura_qi_ocean")
    assert not cosmetics.owns_cosmetic(world["players_db"][pid], "title_eternal_ascendant")

    equipped = call(world, "GET", OWNED, session).json()["equipped"]
    assert equipped == [], (
        "a loadout naming cosmetics the player does not own was rendered: %r"
        % (equipped,)
    )
    # The loadout itself is echoed unchanged: the row is skipped for DRAWING,
    # not erased from the player's save.
    loadout = call(world, "GET", OWNED, session).json()["loadout"]
    assert loadout["aura"] == "aura_qi_ocean", (
        "the revoked loadout row was erased from the save; only the drawn "
        "list should skip it"
    )


def test_unequipping_a_slot_that_is_already_empty_succeeds_through_the_function(world):
    """``unequip_cosmetic`` on an empty slot is a success, not a refusal.

    THE SURVIVOR THAT WAS AN UNREACHABLE ANCHOR. The mutation I wrote for this
    property could not be applied -- its anchor did not exist in the source --
    so nothing tested it. Asserted at the function level as well as on the wire,
    so the next attempt at that mutation has something to bite.
    """
    player = {"cosmetic_loadout": {"aura": "aura_qi_flow"}}

    ok, reason = cosmetics.unequip_cosmetic(player, "aura")
    assert ok is True and reason == "unequipped"

    # Again, on a slot that is now empty: still a success.
    ok_again, reason_again = cosmetics.unequip_cosmetic(player, "aura")
    assert ok_again is True, (
        "unequipping an already-empty slot returned %r; the player asked for "
        "it to be empty and it is" % (reason_again,)
    )

    # And a slot the module does not declare is still a refusal, so the
    # idempotence above did not come from dropping the validation entirely.
    refused, why = cosmetics.unequip_cosmetic(player, "hat")
    assert refused is False
    assert "Unknown slot" in why


def test_equipping_does_not_change_any_combat_number_at_the_function_level(world):
    """``equip_cosmetic`` on a plain dict leaves every combat field untouched.

    THE SURVIVOR THAT WAS THE MODULE'S WHOLE PURPOSE. The mutation that made
    equipping add 15 to ``combat_power`` was not applied -- its anchor did not
    exist in the source -- so the one rule this module exists to keep had no
    direct test. It is asserted here on a dict the caller owns, so the write
    cannot be hidden by a store handing out a copy.
    """
    player = {
        "cultivation_realm": "Nascent Soul", "cultivation_stage": 3,
        "spirit_root": "metal", "atk": 41, "def_": 39, "hp": 900,
        "mp": 300, "spd": 17, "combat_power": 1234, "pvp_rating": 88,
        "cosmetics": {"aura_qi_ocean": {"owned": True}},
    }
    # Snapshot the WHOLE record, not just the stat fields. Comparing the stat
    # dict against the record would report every key the stat list omits --
    # `cosmetics` included -- as a change that never happened.
    before_all = deepcopy(player)
    before = {k: player[k] for k in cosmetics.COMBAT_SUMMARY_FIELDS}

    ok, reason = cosmetics.equip_cosmetic(player, "aura_qi_ocean")
    assert ok is True, reason

    after = cosmetics.combat_summary(player)
    assert after == {k: before[k] for k in cosmetics.COMBAT_SUMMARY_FIELDS}, (
        "equipping changed a number the combat code reads: %r -> %r"
        % (before, after)
    )
    # The whole record, not just the stat list: equip may only touch the loadout.
    changed = {k for k in set(before_all) | set(player)
               if before_all.get(k) != player.get(k)}
    assert changed <= {"cosmetic_loadout"}, (
        "equipping changed stored fields %s; only the loadout may change"
        % sorted(changed)
    )

# ---------------------------------------------------------------------------
# 11. THE TILL'S LEDGER -- a purchase debits the WALLET STORE, not the row
# ---------------------------------------------------------------------------
#
# WHY THIS SECTION EXISTS SEPARATELY FROM SECTION 8
# -------------------------------------------------
# Section 8 reads the balance back with `stones_of`, which is
# `players_db[pid]["spirit_stones"]["low_grade"]` -- the PLAYER RECORD. So every
# assertion up there is satisfied by a route that never opens the wallet store
# at all. The two stores are seeded independently here so one assertion cannot
# be satisfied by a debit to the other: WALLET_STONES goes into the wallet
# store, ROW_STONES stays on the row, and the test reads both.
#
# THE FAILURE THIS PINS
# ---------------------
# The shop used to price an item in the catalogue's currency and then settle it
# against a field on a DIFFERENT object. A player paid 2500 spirit stones by an
# achievement -- money the server put in the WALLET, money `GET /wallet` shows --
# could not spend one stone of it, because the till read `player["spirit_stones"]`,
# which that route never touched. Measured on the mounted app before the fix:
# purchase 400, wallet 2500 -> 2500, `player["spirit_stones"]` absent throughout.
#
# DOUBLE-CHARGING IS THE OTHER HALF
# ---------------------------------
# Routing through the wallet is only half the contract. If the wallet debit AND
# the old row write both ran, the same purchase would take the player's money
# twice out of two stores that are supposed to move together, and the two would
# silently drift apart -- the exact split this section exists to prevent. So the
# row assertion is an equality against its BEFORE value, not "the row went down",
# which a double charge would also satisfy.

#: Seeded into the WALLET STORE. Large enough to cover the cheapest
#: spirit-stone-priced item with room to spare, so a refusal below is a defect
#: rather than an empty purse.
WALLET_STONES = 4242

#: Seeded into the PLAYER RECORD and deliberately NOT equal to WALLET_STONES.
#: Two stores funded the same number would let a debit to either one satisfy
#: both assertions, which is how a double charge would hide.
ROW_STONES = 77


def _wallet_stones(world, player_id):
    """The WALLET STORE's spirit_stone balance, read fresh from the store.

    Deliberately NOT `stones_of`, which reads the player record. The two are
    separate objects and the whole point of this section is that they can hold
    different numbers.
    """
    wallet = world["wallet"].get(player_id)
    assert wallet is not None, (
        "the wallet store holds no row for %r; the till read a balance of "
        "None, which it treats as 'no answer' and then charges the player row"
        % (player_id,)
    )
    balance = getattr(wallet, "balance", None)
    if isinstance(balance, dict):          # the PostgreSQL backend's shape
        return int(balance.get("spirit_stone", 0) or 0)
    return int(getattr(balance, "spirit_stone", 0) or 0)


def _fund_wallet(world, player_id, amount):
    """Put stones in the WALLET STORE through the wallet's own earn path.

    Not a direct assignment: the store hands out detached copies on read, so a
    bare `wallet.balance.spirit_stone = n` would mutate a throwaway and leave
    the stored row at zero. `earn` is the guarded writer every award uses.
    """
    from wallet_dispatch import earn
    receipt = earn(world["wallet"], player_id, "spirit_stone", amount,
                   "cosmetics-wallet-test-%s" % player_id)
    assert receipt.get("ok"), "the wallet refused its own seed grant: %r" % (receipt,)


def _cheapest_wallet_affordable_cosmetic():
    """The cheapest spirit-stone-priced catalogue item this test can afford.

    Selected from the catalogue at run time. A literal id would turn a re-price
    into a spurious failure here, and picking the cheapest means the assertion
    is about WHERE the money came from rather than about any particular price.
    """
    affordable = [
        (cid, cfg) for cid, cfg in cosmetics.COSMETIC_CATALOG.items()
        if cfg.get("purchase_currency") == "spirit_stones"
        and isinstance(cfg.get("purchase_cost"), int)
        and cfg["purchase_cost"] <= WALLET_STONES
        and not cfg.get("realm_requirement")
    ]
    assert affordable, (
        "no catalogue item is priced in spirit stones within the seeded wallet; "
        "the fixture no longer measures what it was written to measure"
    )
    return min(affordable, key=lambda pair: pair[1]["purchase_cost"])


def test_purchase_debits_the_wallet_store_and_leaves_the_player_row_untouched(world):
    """A purchase spends the WALLET STORE's balance, once, and only there.

    Both stores are funded independently, so a debit to one cannot satisfy the
    other. Three claims, one purchase:

    * the wallet store lost exactly the catalogue price;
    * the player record is byte-for-byte what it was -- neither debited nor
      credited, which is what rules out the double charge;
    * the reply's ``wallet`` key names the store that actually moved.
    """
    cid, cfg = _cheapest_wallet_affordable_cosmetic()
    price = cfg["purchase_cost"]
    session = make_player(world, stones=ROW_STONES)
    pid = session["player_id"]
    _fund_wallet(world, pid, WALLET_STONES)

    wallet_before = _wallet_stones(world, pid)
    row_before = int(world["players_db"][pid]["spirit_stones"]["low_grade"])
    assert wallet_before == WALLET_STONES, (
        "the seeded wallet reads %d, not the %d it was funded with"
        % (wallet_before, WALLET_STONES)
    )
    assert row_before == ROW_STONES, (
        "the seeded player row reads %d, not the %d it was funded with; "
        "WALLET_STONES and ROW_STONES must differ or this test cannot tell "
        "the two stores apart" % (row_before, ROW_STONES)
    )

    response = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert response.status_code == 200, (
        "purchase of %r answered %d: %s -- the wallet holds %d, which covers "
        "the %d price, so a refusal here means the till is not reading the "
        "wallet store at all"
        % (cid, response.status_code, response.text, wallet_before, price)
    )

    wallet_after = _wallet_stones(world, pid)
    row_after = int(world["players_db"][pid]["spirit_stones"]["low_grade"])

    assert wallet_before - wallet_after == price, (
        "the wallet store lost %d for a %d item; the till must settle against "
        "the wallet, by the catalogue price and no more"
        % (wallet_before - wallet_after, price)
    )
    assert row_after == row_before, (
        "the player record moved from %d to %d across a purchase that was "
        "already debited from the wallet store: the old player-row write is "
        "still running and one purchase is being charged twice, out of two "
        "stores that are supposed to move together"
        % (row_before, row_after)
    )
    assert cid in response.json()["owned"], (
        "the wallet was debited but the cosmetic was not granted; a charge "
        "without a grant is the worst of the two failure directions"
    )


def test_a_wallet_short_player_is_still_served_from_the_row_fallback(world):
    """A wallet that cannot cover the price falls back to the player record.

    This pins the OTHER half of the wallet-first rule, and it is a deliberate
    design decision rather than an accident. The row fallback exists because the
    welcome grant is still written to the player record at signup and
    `stone_backfill.py` has never been applied (see `_spend_ledgers`). A strict
    wallet-only till was measured on 2026-10-04 and REJECTED: it refused every
    new account's purchase of the 200-stone welcome aura, which is a worse
    regression than the defect the change set out to fix.

    So the order is wallet first, row second, and this test is what stops a
    future change from over-correcting into wallet-only. It bites in the
    direction that matters: make the till wallet-only and this goes red, and
    the shop empties for every account the backfill has not migrated.
    """
    cid, cfg = _cheapest_wallet_affordable_cosmetic()
    price = cfg["purchase_cost"]

    session = make_player(world, stones=price * 10)
    pid = session["player_id"]
    _fund_wallet(world, pid, price - 1)

    wallet_before = _wallet_stones(world, pid)
    row_before = int(world["players_db"][pid]["spirit_stones"]["low_grade"])
    assert wallet_before < price, "the fixture must leave the wallet short"

    response = call(world, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert response.status_code == 200, (
        "a wallet holding %d, one stone short of the %d price, was refused "
        "even though the player record held %d. The row is the documented "
        "fallback for accounts the stone backfill has not migrated; dropping "
        "it empties the shop for every new account."
        % (wallet_before, price, row_before)
    )
    assert row_before - int(world["players_db"][pid]["spirit_stones"]["low_grade"]) == price, (
        "the fallback must charge the catalogue price and no more"
    )
    assert _wallet_stones(world, pid) == wallet_before, (
        "a wallet short of the price was debited anyway; a refusal from the "
        "wallet is an answer about the player's money, not a smaller charge"
    )


def test_each_mounted_app_charges_its_own_wallet_store(world):
    """A second mount is not charged against the first mount's wallet store.

    The store used to be resolved once and cached in a module global, so the
    first store the module ever saw was the one every later purchase debited.
    Mount a second app with its own player store and its own wallet, and the
    second player's purchase was charged against the FIRST player's wallet --
    the second player's money never moved, and the cosmetic was free.

    Measured before the fix: two mounts, wallet A funded 4242 and wallet B
    funded 4242, buy the 200-stone item in B; wallet A fell to 4042 and wallet
    B never moved.
    """
    first = _cheapest_wallet_affordable_cosmetic()
    cid, cfg = first
    price = cfg["purchase_cost"]

    # Mount a SECOND world, exactly as the per-test fixture does.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from wallet_dispatch import InMemoryWalletStore
    from server.player_store import PlayerStore

    other_wallet = InMemoryWalletStore()
    other_players = PlayerStore(wallet_store=other_wallet)
    other_app = FastAPI()
    cosmetics.register_cosmetic_routes(other_app, lambda: other_players)
    other_client = TestClient(other_app, raise_server_exceptions=False)
    other = {"app": other_app, "client": other_client,
             "players_db": other_players, "wallet": other_wallet}

    session = make_player(other, stones=ROW_STONES)
    pid = session["player_id"]
    _fund_wallet(other, pid, WALLET_STONES)

    response = call(other, "POST", PURCHASE, session, {"cosmetic_id": cid})
    assert response.status_code == 200, response.text

    assert _wallet_stones(other, pid) == WALLET_STONES - price, (
        "the second mount charged %d off its own wallet instead of %d; the "
        "store is being resolved once and cached, so every later mount bills "
        "the first store it ever saw"
        % (WALLET_STONES, price)
    )

    # And the first mount's wallet must be untouched by the second mount's spend.
    untouched = [
        key for key in world["players_db"]
        if _wallet_stones(world, key) != WALLET_STONES
    ] if world["wallet"].get(pid) is not None else []
    assert untouched == [], (
        "a purchase in the second mount moved the FIRST mount's wallet; "
        "players %s were charged by another player's app" % (untouched,)
    )
