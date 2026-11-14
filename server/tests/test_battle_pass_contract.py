"""
CONTRACT TEST: server/battle_pass.py -- the core module and its own mount.

WHY THIS FILE IS SEPARATE FROM THE TWO EXISTING COMPANIONS
-----------------------------------------------------------
``battle_pass_routes.py`` is the mount MODULE. It delegates the seven classic
paths straight back to ``battle_pass.register_battle_pass_routes`` and then adds
four ``/battle-pass/api/*`` paths of its own. The two existing contract files
test it through that wrapper, so every request they make arrives on an app that
carries eleven paths.

This file mounts ``battle_pass.py`` ON ITS OWN, on a bare ``FastAPI()``. That is
the only way to see the surface this module is actually responsible for: seven
paths, no ``/api`` sibling, no payment verifier, and the module's own pure
helpers exercised directly rather than only through HTTP.

WHAT IS PINNED
--------------
* The mounted route table, compared as a SET of (path, method) pairs -- not a
  count, which a module that mounted seven and an eighth would satisfy.
* The exact reply keys of every route on every branch it has.
* That the five per-player routes refuse an anonymous caller (401) and a session
  naming somebody else (403), while ``/battle-pass/season`` is deliberately
  public.
* The error cases: an unknown track, a level off the track, a missing query
  parameter, a player row that no longer exists.
* The once-only claims as RELATIONSHIPS: the purse moves by the reward table's
  own number, a retry that invents a fresh key is still a replay, and a refusal
  hands the position back so the player can claim it later.
* The pure helpers that decide money and experience: the level curve, the reward
  table, the once-only ledgers, the spirit-stone helpers.

NUMBERS ARE READ, NEVER RESTATED
---------------------------------
Every price comes from ``battle_pass.TIER_COST``, the level ceiling from
``battle_pass.MAX_LEVEL``, the single-grant ceiling from
``battle_pass.MAX_XP_PER_GRANT`` and the retry-key bound from
``idem_gate.MAX_IDEMPOTENCY_KEY_LEN``. Re-pricing a tier or re-scaling the curve
leaves this file green; a route that grew its own copy of one of those numbers
does not.

THE 404 REGRESSION THIS FILE GUARDS
-----------------------------------
``claim_weekly_bonus`` is a PAID-TRACK-ONLY bonus. Its route once opened the
player's transaction BEFORE checking that the player existed, so a deleted row
answered 500 while all four sibling routes answered 404 -- one fault, one fix,
and the fix landed. The behaviour is now pinned as CORRECT:
``test_all_five_per_player_routes_answer_the_same_404_for_a_gone_row``. That test
exists because the asymmetry was real once, and a later edit that moved the
check back inside the transaction would otherwise pass unnoticed.

WHAT THIS FILE DELIBERATELY DOES NOT TOUCH
------------------------------------------
``server/main.py`` is nowhere in this file. The mount goes onto a bare
``FastAPI()`` with its own in-memory player store, so a result here depends on
``battle_pass.py``, ``battle_pass_season.py``, ``auth.py``, ``idem_gate.py`` and
``player_store.py`` and nothing else.

The repository root ``conftest.py`` imports ``server.main`` at COLLECTION time.
If a peer is half-way through editing ``main.py``, every pytest run in the repo
aborts before a single test executes. That traceback is not a defect here.

Run
---
    cd G:/Projects/wayfarers-cycle
    tools/wc_pytest.sh server/tests/test_battle_pass_contract.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid
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
import battle_pass  # noqa: E402
import idem_gate  # noqa: E402
import qi_rewards  # noqa: E402 -- read_qi reads the field progression actually uses
from battle_pass import (BattlePassTier, MAX_LEVEL, PlayerBattlePass,  # noqa: E402
                         TIER_COST)
from player_store import PlayerStore  # noqa: E402

#: The seven paths ``battle_pass.py`` mounts, with the query parameters each one
#: reads. Frozen deliberately: the shipped Godot client calls these URLs, so a
#: rename or a move from the query string into the body breaks every caller.
#:
#: ``summary`` is the SEVENTH and was added after the other six were frozen
#: here. It is the single-season view over the banked rows that
#: ``GET /battle-pass/{player_id}`` already returns under ``season_history``:
#: ``battle_pass_season_proof.proof_summary`` and ``play_battle_pass.play()`
#: both read it, and while it was absent from the app both of them answered 404
#: for every caller -- so a correct rollover still read as a rollover failure.
#: Listed in REGISTRATION order, which is the order the live table has.
ROUTES = {
    "season": ("/battle-pass/season", "GET", ()),
    "get": ("/battle-pass/{player_id}", "GET", ()),
    "summary": ("/battle-pass/{player_id}/summary", "GET", ("season",)),
    "claim": ("/battle-pass/{player_id}/claim", "POST", ("level", "tier")),
    "claim_all": ("/battle-pass/{player_id}/claim-all", "POST", ("tier",)),
    "upgrade": ("/battle-pass/{player_id}/upgrade", "POST", ("tier",)),
    "weekly": ("/battle-pass/{player_id}/weekly-bonus", "POST", ("week",)),
}

#: The exact reply keys, measured on each branch each route has.
SEASON_KEYS = {"ok", "season"}
GET_KEYS = {"ok", "battle_pass", "season_history"}
GET_BP_KEYS = {
    "available_rewards", "claimed", "level", "max_level", "purchased_at",
    "season", "season_number", "season_xp_total", "tier", "track",
    "weekly_bonus_claimed", "xp", "xp_for_next",
}
CLAIM_KEYS = {"ok", "replayed", "reward", "level", "tier", "new_level"}
CLAIM_ALL_KEYS = {"ok", "tier", "claimed_count", "spirit_stones", "new_level"}
UPGRADE_KEYS = {"ok", "new_tier", "cost", "replayed"}
WEEKLY_KEYS = {"ok", "reward", "week", "new_level"}
WEEKLY_REWARD_KEYS = {"xp_gained", "spirit_stones", "new_level", "leveled_up"}
TRACK_ROW_KEYS = {"level", "free", "senior", "super"}
CLAIMED_KEYS = {"free", "senior", "super"}
AVAILABLE_KEYS = {"free", "senior", "super"}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A bare app carrying ONLY this module's seven paths, plus a fresh store.

    Function-scoped on purpose: the no-database claim and purchase ledgers are
    PROCESS-GLOBAL dicts and a set, so a shared world would let one test's claim
    mark another test's player as already paid.
    """
    players_db = PlayerStore()
    app = FastAPI()
    battle_pass.register_battle_pass_routes(app, lambda: players_db)
    yield {
        "app": app,
        "client": TestClient(app, raise_server_exceptions=False),
        "players_db": players_db,
    }


def _seed(players_db: Any, stones: int = 100_000, level: int = 1,
          tag: str = "bp-core", tier: str = "free") -> Dict[str, Any]:
    """Write one signed-in player into a NAMED store and return its session.

    A FRESH id per call. Every once-only key in this module is derived from the
    player's own id and the server's season, so a shared id would let one test's
    purchase swallow another's. That is what makes the file order-independent.

    Takes the store rather than the world so a test that mounts its own app on
    its own store can reuse the same seeding without inventing a second copy.

    ``tier`` SEEDS the paid track the cheap way, without paying for it. That is
    deliberately not the same as buying it through the route: a test about the
    cost of a track must go through the route, while a test about what a track
    PAYS needs the track and nothing else. Mixing the two either pays 2,499
    stones to assert a reward table, or leaves a paid track unlocked by seeding,
    which would hide a regression in the unlock guard.
    """
    pid = "%s-%s" % (tag, uuid.uuid4().hex[:12])
    row = {
        "id": pid,
        "name": pid,
        "spirit_stones": {"low_grade": stones},
        "progress": {"realm": "qi", "stage": 1, "level": 1},
        "inventory": {},
    }
    players_db[pid] = row
    if level != 1 or tier != "free":
        # Seed the stored pass so the player starts partway up the track. Written
        # through the store because __getitem__ hands out a copy.
        row = players_db[pid]
        row["battle_pass"] = {
            "season_id": battle_pass.get_season_id(),
            "tier": tier,
            "xp": battle_pass.get_xp_for_level(level),
            "level": level,
            "claimed_free": [],
            "claimed_senior": [],
            "claimed_super": [],
            "weekly_bonus_claimed": [],
            "purchased_at": None if tier == "free" else time_now(),
        }
        players_db.persist(pid, row)
    return {"player_id": pid,
            "headers": {"Authorization": "Bearer " + auth.issue_session(pid)}}


def make_player(world: Dict[str, Any], stones: int = 100_000,
                level: int = 1) -> Dict[str, Any]:
    """One signed-in player in this world's store, with a known purse and track."""
    return _seed(world["players_db"], stones=stones, level=level)


def stones(world: Dict[str, Any], player_id: str) -> int:
    """The spendable balance, read through the module's own helper."""
    return battle_pass.stone_balance(world["players_db"][player_id])


def set_stones(world: Dict[str, Any], player_id: str, value: int) -> None:
    """Write the purse THROUGH THE STORE, or it lands on a throwaway copy."""
    row = world["players_db"][player_id]
    row["spirit_stones"] = {"low_grade": value}
    world["players_db"].persist(player_id, row)


def _bank_a_season(world: Dict[str, Any], player_id: str) -> str:
    """Give one player a banked season, and return its season id.

    ``GET /battle-pass/{player_id}/summary`` serves FINISHED seasons, so a
    player who has never finished one has nothing for it to answer with. The
    row is produced by ``summarize_season`` and filed by
    ``record_season_history`` -- both the module's own helpers -- so this seeds
    the shape the server writes rather than one written by hand here. Written
    through the store, because ``__getitem__`` hands out a copy.
    """
    old = battle_pass.get_season_id(1_700_000_000)  # a real past month
    row = world["players_db"][player_id]
    finished = battle_pass.PlayerBattlePass(season_id=old, level=4, xp=900,
                                            claimed_free=[1, 2, 3])
    battle_pass.record_season_history(
        row, battle_pass.summarize_season(finished))
    world["players_db"].persist(player_id, row)
    return old


# ---------------------------------------------------------------------------
# Per-position reward measurement
# ---------------------------------------------------------------------------
# WHY NOT A SUM. The obvious way to check a reward is to add up what the table
# promises for several positions and compare that total to what the store moved.
# A total cannot tell WHICH position paid what: two positions trading rewards --
# position 5 paid row 6's stones while position 25 paid row 24's -- leaves the
# sum untouched and passes. So every reward assertion in this file measures ONE
# position at a time and compares each field for EQUALITY against that
# position's own row. A row that pays nothing must move nothing.

def _read(world: Dict[str, Any], player_id: str) -> Dict[str, Any]:
    """Every field a reward can move, read FRESH from the store.

    ``PlayerStore.__getitem__`` hands out a copy, so a value read before a claim
    and kept across it would compare a stale row against a fresh one.
    """
    row = world["players_db"][player_id]
    return {
        "stones": battle_pass.stone_balance(row),
        "qi": qi_rewards.read_qi(row),
        "bag": dict(row.get("inventory") or {}),
        "cosmetics": frozenset((row.get("cosmetics") or {}).keys()),
    }


def _expected(before: Dict[str, Any], reward: Dict[str, Any]) -> Dict[str, Any]:
    """What ONE position's row promises, field by field.

    Same shape as :func:`_read`, so the two are compared for EQUALITY. The bag
    is the WHOLE bag, not a per-item delta: that way a promise nothing position
    must not write one spare key either.
    """
    bag = dict(before["bag"])
    for name in reward.get("items") or []:
        bag[name] = bag.get(name, 0) + 1
    tickets = int(reward.get("gacha_tickets", 0) or 0)
    if tickets:
        bag["Gacha Ticket"] = bag.get("Gacha Ticket", 0) + tickets
    promised = {reward["cosmetic"]} if reward.get("cosmetic") else set()
    return {
        "stones": before["stones"] + int(reward.get("spirit_stones", 0) or 0),
        "qi": before["qi"] + float(reward.get("qi", 0) or 0),
        "bag": bag,
        "cosmetics": frozenset(before["cosmetics"]) | frozenset(promised),
    }


def _assert_paid_exactly(world: Dict[str, Any], player_id: str,
                         before: Dict[str, Any], reward: Dict[str, Any],
                         level: int, tier: str) -> Dict[str, Any]:
    """The store moved by EXACTLY ``reward``: every field, nothing summed."""
    after = _read(world, player_id)
    want = _expected(before, reward)
    assert after == want, (
        "level %d on %s: the row promises %r\n  the store was %r\n  the store "
        "is now  %r" % (level, tier, reward, before, after))
    return after


def path_for(name: str, player_id: Optional[str] = None) -> str:
    """One declared path with ``{player_id}`` filled in."""
    raw = ROUTES[name][0]
    return raw if player_id is None else raw.format(player_id=player_id)


def call(world: Dict[str, Any], name: str, session: Optional[Dict[str, Any]],
         player_id: Optional[str] = None,
         params: Optional[Dict[str, Any]] = None, **headers: str):
    """One request. The session is opt-in so no test forgets it silently."""
    method = ROUTES[name][1]
    target = path_for(name, player_id)
    h: Dict[str, str] = {}
    if session is not None:
        h.update(session["headers"])
    h.update(headers)
    if method == "GET":
        return world["client"].get(target, headers=h, params=params)
    return world["client"].post(target, headers=h, params=params, json={})


def buy(world: Dict[str, Any], session: Dict[str, Any], tier: str,
        key: Optional[str] = None):
    """Buy a paid track through this module's own upgrade route."""
    return call(world, "upgrade", session, session["player_id"],
                params={"tier": tier},
                **{"Idempotency-Key": key or ("buy-" + uuid.uuid4().hex)})


# ---------------------------------------------------------------------------
# 1. THE MOUNT -- seven paths, no /api sibling, and the removed route stays gone
# ---------------------------------------------------------------------------

def test_the_module_mounts_exactly_its_seven_declared_paths(world):
    """The app carries every declared path, and nothing else under the prefix.

    Compared as a SET of (path, method) pairs. FastAPI adds ``HEAD`` alongside
    ``GET``, so that is subtracted; the POST arms carry ``{"POST"}``.
    """
    live = set()
    for route in world["app"].routes:
        p = getattr(route, "path", "")
        if not p.startswith("/battle-pass"):
            continue
        for method in set(getattr(route, "methods", set()) or set()) - {"HEAD"}:
            live.add((p, method))

    declared = {(p, m) for p, m, _params in ROUTES.values()}
    assert live == declared, (
        "the app carries %s; the module declares %s"
        % (sorted(live), sorted(declared)))


def test_mounting_battle_pass_alone_never_mixes_in_the_api_surface(world):
    """The four ``/battle-pass/api/*`` paths belong to the OTHER module.

    ``battle_pass_routes.register_battle_pass_routes`` delegates here and then
    adds its own four. If this module grew an ``/api`` path, the wrapper would
    mount it twice and FastAPI would silently serve the first copy.
    """
    paths = {getattr(r, "path", "") for r in world["app"].routes}
    assert not [p for p in paths if p.startswith("/battle-pass/api/")], (
        "battle_pass.py mounted an /api path; the mount module owns those")


def test_the_removed_add_xp_route_is_gone_and_stays_gone(world):
    """``POST /battle-pass/{player_id}/add-xp`` must not come back.

    It took ``amount`` off the query string, so any caller could mint any amount
    of season experience, and a negative amount walked it backwards. XP is now
    awarded only in-process by ``battle_pass.grant_xp``. Measured: the path
    answers 404 and is absent from the table. The single-segment wildcard
    ``/battle-pass/{player_id}`` cannot absorb two segments, so 404 is the honest
    answer rather than the wildcard reading "add-xp" as a player.
    """
    session = make_player(world)
    removed = "/battle-pass/%s/add-xp" % session["player_id"]

    resp = world["client"].post(removed, headers=session["headers"],
                                params={"amount": 1_000_000}, json={})

    assert resp.status_code == 404, resp.text
    assert not any(getattr(r, "path", "") == "/battle-pass/{player_id}/add-xp"
                   for r in world["app"].routes), (
        "the removed route is back in the route table")


@pytest.mark.parametrize("name", sorted(n for n in ROUTES if n != "season"))
def test_each_route_answers_only_its_own_method(world, name):
    """A POST route answers 405 to GET, and the GET route answers 405 to POST.

    Asserted with a control: the same client and session DO reach the matching
    verb. Without that arm a blanket 405 would satisfy every row.

    ``summary`` needs one banked season first. Its shipped behaviour for a
    player who has never finished a season is 404 ("No finished season for this
    player"), which is right -- "this season does not exist for you" and "this
    season earned nothing" are different facts -- but it would leave the control
    arm asserting a refusal. The season is banked through the module's OWN
    ``record_season_history``, so the row shape is the server's rather than one
    written by hand here.
    """
    session = make_player(world)
    if name == "summary":
        _bank_a_season(world, session["player_id"])
    target = path_for(name, session["player_id"])
    method = ROUTES[name][1]

    if method == "GET":
        assert world["client"].get(target, headers=session["headers"]).status_code == 200, (
            "the matching verb did not serve the route")
        assert world["client"].post(target, headers=session["headers"],
                                    json={}).status_code == 405
    else:
        params = {"level": 1, "tier": "free", "week": 1}
        ok = world["client"].post(target, headers=session["headers"],
                                  params=params, json={})
        assert ok.status_code in (200, 400), ok.text
        assert world["client"].get(target, headers=session["headers"]).status_code == 405


# ---------------------------------------------------------------------------
# 2. GET /battle-pass/season -- public by design
# ---------------------------------------------------------------------------

def test_the_season_screen_needs_no_session_and_names_the_server_clock(world):
    """No player is in this reply, so no credential is asked for.

    Asserted as a pair: with no header at all it answers 200, and what it
    reports is the server's own season id, so a caller cannot name their own.
    """
    resp = world["client"].get("/battle-pass/season")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == SEASON_KEYS, (
        "the season reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(SEASON_KEYS)))
    assert body["ok"] is True
    assert body["season"]["season_id"] == battle_pass.get_season_id()

    # A caller naming its own season changes nothing.
    forced = world["client"].get("/battle-pass/season",
                                 params={"season_id": "1999-01", "now": 0})
    assert forced.json()["season"]["season_id"] == body["season"]["season_id"]


# ---------------------------------------------------------------------------
# 3. GET /battle-pass/{player_id} -- the whole panel
# ---------------------------------------------------------------------------

def test_the_player_screen_carries_every_key_the_panel_draws(world):
    """The reply key set is frozen; the numbers inside are read at run time.

    A missing key is a dead panel and a renamed one reads ``undefined`` on a
    live screen, so the NAMES are frozen while ``max_level`` and
    ``season_xp_total`` are compared against the module's own constants.
    """
    session = make_player(world)

    resp = call(world, "get", session, session["player_id"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == GET_KEYS, (
        "the reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(GET_KEYS)))
    assert body["ok"] is True
    assert body["season_history"] == []

    bp = body["battle_pass"]
    assert set(bp) == GET_BP_KEYS, (
        "battle_pass carries %s; the pinned contract is %s"
        % (sorted(bp), sorted(GET_BP_KEYS)))
    assert bp["max_level"] == MAX_LEVEL
    assert bp["season_xp_total"] == battle_pass.TOTAL_XP
    assert bp["tier"] == "free"
    assert set(bp["claimed"]) == CLAIMED_KEYS
    assert set(bp["available_rewards"]) == AVAILABLE_KEYS


def test_the_track_names_every_level_and_a_state_for_every_track(world):
    """A missing row is a dead button; an empty reward is a promise the server
    cannot keep. So the row COUNT and the per-row KEY SET are pinned.
    """
    session = make_player(world)
    bp = call(world, "get", session, session["player_id"]).json()["battle_pass"]

    rows = bp["track"]["levels"]
    assert len(rows) == MAX_LEVEL, (
        "the track served %d rows; the module's own ceiling is %d"
        % (len(rows), MAX_LEVEL))
    assert [r["level"] for r in rows] == list(range(1, MAX_LEVEL + 1))
    for row in rows:
        assert set(row) == TRACK_ROW_KEYS, (
            "row %d carries %s" % (row["level"], sorted(row)))
        for tier in BattlePassTier:
            assert set(row[tier.value]) == {"state", "reward"}
            assert row[tier.value]["state"] in {"claimed", "claimable", "locked"}
        # The reward shown IS the table's reward for that position.
        assert row["free"]["reward"] == battle_pass.reward_for(
            row["level"], BattlePassTier.FREE)


def test_the_screen_reports_the_server_level_not_one_the_caller_names(world):
    """Level, experience and the next-level gap come from the stored row.

    A caller-supplied ``level`` or ``xp`` query parameter is ignored rather
    than honoured: a panel that drew a level the server does not hold shows the
    player positions they cannot claim.
    """
    session = make_player(world, level=5)
    bp = call(world, "get", session, session["player_id"],
              params={"level": 50, "xp": 999_999}).json()["battle_pass"]

    assert bp["level"] == 5
    assert bp["xp"] == battle_pass.get_xp_for_level(5)
    assert bp["xp_for_next"] == (battle_pass.get_xp_for_level(6) - bp["xp"])


def test_the_screen_answers_404_for_a_player_row_that_is_gone(world):
    """A session can outlive its player. 404 keeps that a normal outcome."""
    session = make_player(world)
    pid = session["player_id"]
    assert call(world, "get", session, pid).status_code == 200

    world["players_db"]._mem.pop(pid, None)
    world["players_db"]._known.discard(pid)

    gone = call(world, "get", session, pid)
    assert gone.status_code == 404, gone.text
    assert gone.json()["detail"] == "Player not found"


# ---------------------------------------------------------------------------
# 4. POST /battle-pass/{player_id}/claim -- once, ever
# ---------------------------------------------------------------------------

def test_claim_carries_the_same_keys_on_a_first_attempt_and_on_a_replay(world):
    """A client that timed out mid-claim reads these names while holding real
    money, so the two key sets are asserted EQUAL and frozen.
    """
    session = make_player(world)

    first = call(world, "claim", session, session["player_id"],
                 params={"level": 1, "tier": "free"})
    assert first.status_code == 200, first.text
    fresh = first.json()
    assert set(fresh) == CLAIM_KEYS, (
        "the first claim carries %s; the pinned contract is %s"
        % (sorted(fresh), sorted(CLAIM_KEYS)))
    assert fresh["ok"] is True and fresh["replayed"] is False

    second = call(world, "claim", session, session["player_id"],
                  params={"level": 1, "tier": "free"},
                  **{"Idempotency-Key": "A-COMPLETELY-DIFFERENT-KEY"})
    assert second.status_code == 200, second.text
    replayed = second.json()
    assert set(replayed) == CLAIM_KEYS, (
        "the replay carries %s; a timed-out client reads undefined for a reward "
        "it was already paid" % sorted(replayed))
    assert replayed["replayed"] is True
    assert replayed["reward"] == fresh["reward"], (
        "the replay lost the first attempt's receipt")


def test_claim_pays_the_table_reward_exactly_once(world):
    """The purse moves by the reward table's own number, read at run time.

    Asserted as a relationship between the reply and the store, so a reply that
    reports a reward nothing received fails here.
    """
    session = make_player(world)
    reward = battle_pass.reward_for(1, BattlePassTier.FREE)
    before = _read(world, session["player_id"])

    body = call(world, "claim", session, session["player_id"],
                params={"level": 1, "tier": "free"}).json()

    assert body["reward"] == reward
    assert body["tier"] == "free" and body["level"] == 1
    assert body["new_level"] == 1, "claiming awards no experience"
    # This position's whole row, not its stones alone and not a total.
    _assert_paid_exactly(world, session["player_id"], before, reward, 1, "free")

    for _ in range(3):
        again = call(world, "claim", session, session["player_id"],
                     params={"level": 1, "tier": "free"})
        assert again.status_code == 200, again.text
        assert again.json()["replayed"] is True
    assert _read(world, session["player_id"]) == _expected(before, reward), (
        "three replays moved the store past the single payment owed")


def test_the_claim_ledger_is_keyed_on_the_position_not_on_the_header(world):
    """A retry that invents a brand-new key is still a replay.

    This is the property a caller-keyed table cannot have. Asserted as a pair:
    two players sending the byte-identical key both pay, because the guard
    ignores the header entirely and keys on (player, season, track, level).
    """
    first = make_player(world)
    second = make_player(world)

    one = call(world, "claim", first, first["player_id"],
               params={"level": 1, "tier": "free"},
               **{"Idempotency-Key": "shared-key"})
    two = call(world, "claim", second, second["player_id"],
               params={"level": 1, "tier": "free"},
               **{"Idempotency-Key": "shared-key"})

    assert one.status_code == two.status_code == 200
    assert one.json()["replayed"] is False
    assert two.json()["replayed"] is False, (
        "one player's claim was swallowed by another's identical header")
    reward = battle_pass.reward_for(1, BattlePassTier.FREE)
    assert stones(world, second["player_id"]) == 100_000 + reward["spirit_stones"]


def test_claim_reads_level_and_tier_from_the_query_not_the_body(world):
    """The caller names WHICH position, and only from the query string.

    Both are bare scalars, so FastAPI classifies them as query parameters and a
    JSON body carrying them is IGNORED. A missing or non-numeric level is a 422
    that says where it belongs, rather than defaulting to level 1.
    """
    session = make_player(world)
    pid = session["player_id"]

    body_only = world["client"].post("/battle-pass/%s/claim" % pid,
                                     headers=session["headers"],
                                     json={"level": 40, "tier": "super"})
    assert body_only.status_code == 422, body_only.text
    assert any(d.get("loc") == ["query", "level"]
               for d in body_only.json()["detail"]), (
        "the 422 does not say the level belongs in the QUERY, so the developer "
        "is left hunting a payload bug: %s" % body_only.text)

    assert call(world, "claim", session, pid,
                params={"tier": "free"}).status_code == 422
    assert call(world, "claim", session, pid,
                params={"tier": "free", "level": "abc"}).status_code == 422
    assert call(world, "claim", session, pid,
                params={"level": 1}).status_code == 422
    # Control: the query spelling works.
    assert call(world, "claim", session, pid,
                params={"level": 1, "tier": "free"}).status_code == 200


def test_claim_bounds_the_level_and_refuses_an_unknown_track_by_name(world):
    """A level off the track is 400; a typo track is 400 too.

    The bound is read from ``MAX_LEVEL`` at run time, with a control at each
    end so a route that refused EVERY level would fail this test.
    """
    session = make_player(world)
    pid = session["player_id"]

    for label, level in (("zero", 0), ("negative", -1),
                         ("one past the top", MAX_LEVEL + 1),
                         ("absurd", 10_000_000)):
        resp = call(world, "claim", session, pid,
                    params={"level": level, "tier": "free"})
        assert resp.status_code == 400, "%s -> %s" % (label, resp.text)
        assert resp.json()["detail"] == "Level not reached"

    # Level 1 is inside the range, so a blanket refusal would have failed.
    assert call(world, "claim", session, pid,
                params={"level": 1, "tier": "free"}).status_code == 200

    typo = call(world, "claim", session, pid,
                params={"level": 1, "tier": "senoir"})
    assert typo.status_code == 400, typo.text
    assert typo.json()["detail"] == "Invalid tier"


def test_claim_refuses_a_position_the_player_has_not_reached_or_not_bought(world):
    """Two different refusals, two different messages, and neither pays.

    The refusal must also hand the position back, or the player's own mistake
    (tapping a locked row) deletes real money for the rest of the season. That
    is asserted by measuring the LEDGER, not by reading the reply twice.
    """
    session = make_player(world, level=3)
    pid = session["player_id"]
    before = stones(world, pid)
    season = battle_pass.get_season_id()

    too_high = call(world, "claim", session, pid,
                    params={"level": 4, "tier": "free"})
    assert too_high.status_code == 400, too_high.text
    assert too_high.json()["detail"] == "Level not reached"

    for tier in ("senior", "super"):
        locked = call(world, "claim", session, pid,
                      params={"level": 1, "tier": tier})
        assert locked.status_code == 400, "%s -> %s" % (tier, locked.text)
        assert locked.json()["detail"] == "Tier not unlocked"

    assert stones(world, pid) == before

    # THE POSITIONS ARE STILL CLAIMABLE. A refusal that burned the key would
    # leave the ledger answering "already paid" for a reward nobody received.
    for level in (4,):
        assert battle_pass.reserve_claim(
            world["players_db"], pid, season, "free", level)[0] is True, (
            "the refusal left the position reserved; the player can never claim "
            "it again this season")
        battle_pass.release_claim(world["players_db"], pid, season, "free", level)
    for tier in ("senior", "super"):
        assert battle_pass.reserve_claim(
            world["players_db"], pid, season, tier, 1)[0] is True
        battle_pass.release_claim(world["players_db"], pid, season, tier, 1)

    # And the real thing still pays once the player owns it.
    assert buy(world, session, "senior").status_code == 200
    paid = call(world, "claim", session, pid,
                params={"level": 1, "tier": "senior"})
    assert paid.status_code == 200, paid.text
    assert paid.json()["reward"] == battle_pass.reward_for(1, BattlePassTier.SENIOR)


def test_claim_answers_404_for_a_player_row_that_is_gone(world):
    """The existence check runs before the transaction, so 404 is reachable."""
    session = make_player(world)
    pid = session["player_id"]
    world["players_db"]._mem.pop(pid, None)
    world["players_db"]._known.discard(pid)

    resp = call(world, "claim", session, pid,
                params={"level": 1, "tier": "free"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Player not found"


# ---------------------------------------------------------------------------
# 5. POST /battle-pass/{player_id}/claim-all -- sweep one track
# ---------------------------------------------------------------------------

def test_claim_all_carries_its_keys_and_pays_only_the_unclaimed_rows(world):
    """The sweep's reply names a COUNT and a STONE TOTAL; both are checked
    against the store, so a count that reports rows nobody received fails here.

    The TOTAL is measured as a single row, never as a sum. A sweep that paid
    row 4 where row 3 was asked for, or paid rows 1-5 for a level-4 player,
    would still balance out against the summed table. So the positions BELOW
    the one under test are claimed one at a time first (each against its own
    row), leaving the sweep exactly ONE row to pay -- and the reply's total is
    then that row's own number, with no other position to cancel against.
    """
    for target in (1, 2, 3, 4):
        session = _seed(world["players_db"], level=target,
                        tag="bp-sweep-%d" % target)
        pid = session["player_id"]
        for lower in range(1, target):
            resp = call(world, "claim", session, pid,
                        params={"level": lower, "tier": "free"})
            assert resp.status_code == 200, resp.text
        before = _read(world, pid)

        resp = call(world, "claim_all", session, pid, params={"tier": "free"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == CLAIM_ALL_KEYS, (
            "the sweep reply carries %s; the pinned contract is %s"
            % (sorted(body), sorted(CLAIM_ALL_KEYS)))
        assert body["ok"] is True and body["tier"] == "free"
        assert body["claimed_count"] == 1, (
            "with position %d the only one left, the sweep claimed %s rows"
            % (target, body["claimed_count"]))

        # The one row the sweep owed, read from the table at run time.
        owed = battle_pass.reward_for(target, BattlePassTier.FREE)
        assert body["spirit_stones"] == owed["spirit_stones"], (
            "sweeping position %d reported %s stones and the table pays %s"
            % (target, body["spirit_stones"], owed["spirit_stones"]))
        after = _assert_paid_exactly(world, pid, before, owed, target, "free")

        # A second sweep of the same track pays nothing.
        again = call(world, "claim_all", session, pid, params={"tier": "free"})
        assert again.status_code == 200, again.text
        assert again.json()["claimed_count"] == 0
        assert again.json()["spirit_stones"] == 0
        assert _read(world, pid) == after, (
            "the second sweep moved the store even though it claimed nothing")


def test_claim_all_on_an_unowned_track_is_a_silent_no_op_that_burns_nothing(world):
    """Sweeping a track the player has not bought skips every row, and every
    one of them must stay claimable rather than reserved-but-unpaid.
    """
    session = make_player(world, level=3)
    pid = session["player_id"]
    season = battle_pass.get_season_id()

    resp = call(world, "claim_all", session, pid, params={"tier": "super"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["claimed_count"] == 0
    assert resp.json()["spirit_stones"] == 0
    for level in (1, 2, 3):
        assert battle_pass.reserve_claim(
            world["players_db"], pid, season, "super", level)[0] is True, (
            "the skipped position %d stayed reserved; buying the track would "
            "answer the player with a silent zero-reward replay" % level)
        battle_pass.release_claim(world["players_db"], pid, season, "super", level)

    assert stones(world, pid) == 100_000


def test_claim_all_refuses_a_missing_or_misspelled_track(world):
    """A track name is the only input, so both failures are 400."""
    session = make_player(world)
    pid = session["player_id"]

    missing = call(world, "claim_all", session, pid)
    assert missing.status_code == 422, missing.text
    assert any(d.get("loc") == ["query", "tier"]
               for d in missing.json()["detail"]), missing.text

    typo = call(world, "claim_all", session, pid, params={"tier": "supre"})
    assert typo.status_code == 400, typo.text
    assert typo.json()["detail"] == "Invalid tier"


# ---------------------------------------------------------------------------
# 6. POST /battle-pass/{player_id}/upgrade -- the server's price, once
# ---------------------------------------------------------------------------

def test_upgrade_charges_the_server_price_once_and_moves_the_track(world):
    """The amount is compared against ``TIER_COST`` at run time and the purse
    delta against the reply, so re-pricing a tier leaves this file green.
    """
    session = make_player(world)
    pid = session["player_id"]
    cost = TIER_COST[BattlePassTier.SENIOR]
    before = stones(world, pid)

    resp = buy(world, session, "senior")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == UPGRADE_KEYS, (
        "the upgrade reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(UPGRADE_KEYS)))
    assert body["ok"] is True
    assert body["new_tier"] == "senior"
    assert body["cost"] == cost
    assert body["replayed"] is False
    assert stones(world, pid) == before - cost

    # The upgrade PERSISTED, not just the reply.
    assert world["players_db"][pid]["battle_pass"]["tier"] == "senior"
    screen = call(world, "get", session, pid).json()["battle_pass"]
    assert screen["tier"] == "senior"
    assert screen["purchased_at"] is not None


def test_upgrade_is_keyed_on_the_track_so_a_fresh_key_still_does_not_charge(world):
    """The ledger key is server-derived (player, season, track), so a double
    tap's second insert conflicts AND a brand-new header conflicts too.
    """
    session = make_player(world)
    pid = session["player_id"]
    cost = TIER_COST[BattlePassTier.SENIOR]

    assert buy(world, session, "senior", "u-1").status_code == 200
    paid_once = stones(world, pid)
    assert paid_once == 100_000 - cost

    replay = buy(world, session, "senior", "A-DIFFERENT-KEY-ENTIRELY")
    assert replay.status_code == 200, replay.text
    assert set(replay.json()) == UPGRADE_KEYS, (
        "the replay reply carries %s; a timed-out client reads undefined for "
        "the price it already paid" % sorted(replay.json()))
    assert replay.json()["replayed"] is True
    assert stones(world, pid) == paid_once


def test_two_paid_tracks_in_one_season_are_two_intents_and_both_pay(world):
    """Senior then Super is a legitimate second purchase, so it is charged.

    The counterpart to the test above: keying on the track must not swallow a
    real second request.
    """
    session = make_player(world)
    pid = session["player_id"]
    senior = TIER_COST[BattlePassTier.SENIOR]
    super_cost = TIER_COST[BattlePassTier.SUPER]

    assert buy(world, session, "senior", "two-1").json()["new_tier"] == "senior"
    assert buy(world, session, "super", "two-2").json()["new_tier"] == "super"
    assert stones(world, pid) == 100_000 - senior - super_cost

    # Going back DOWN a track costs nothing and is refused as a move.
    back = buy(world, session, "senior", "two-3")
    assert back.status_code == 200, back.text
    assert back.json()["replayed"] is True
    assert stones(world, pid) == 100_000 - senior - super_cost


def test_upgrade_refuses_the_free_track_by_name_and_never_charges_for_it(world):
    """The free track is the default, not a purchase. Charging 0 through the
    ledger would burn the key and deny the real purchase behind it.
    """
    session = make_player(world)
    pid = session["player_id"]
    before = stones(world, pid)

    free = buy(world, session, "free", "free-1")
    assert free.status_code == 400, free.text
    assert free.json()["detail"] == "Invalid tier"

    typo = buy(world, session, "senoir", "free-2")
    assert typo.status_code == 400, typo.text

    missing = call(world, "upgrade", session, pid)
    assert missing.status_code == 422, missing.text

    assert stones(world, pid) == before
    # The refusal did not burn the key: the real purchase still works.
    assert buy(world, session, "senior", "free-3").status_code == 200


def test_upgrade_requires_the_retry_header_and_names_its_own_route(world):
    """A per-attempt key the server invented double-charges a retry, so the
    header is REQUIRED and the bound is read from ``idem_gate``.
    """
    session = make_player(world)
    pid = session["player_id"]
    before = stones(world, pid)

    missing = call(world, "upgrade", session, pid, params={"tier": "senior"})
    assert missing.status_code == 400, missing.text
    assert "Idempotency-Key" in missing.json()["detail"]
    assert "battle_pass_upgrade" in missing.json()["detail"], (
        "the refusal does not name the route, so a developer cannot tell which "
        "of the money routes is demanding the header")

    for label, key in (("blank", ""),
                       ("whitespace only", "   "),
                       ("one over the bound",
                        "k" * (idem_gate.MAX_IDEMPOTENCY_KEY_LEN + 1)),
                       ("embedded newline", "key\ninjected"),
                       ("null byte", "key\x00injected")):
        resp = call(world, "upgrade", session, pid, params={"tier": "senior"},
                    **{"Idempotency-Key": key})
        assert resp.status_code == 400, "%s -> %s" % (label, resp.text)

    # Exactly at the bound is accepted, and nothing was charged by the refusals.
    at_bound = call(world, "upgrade", session, pid, params={"tier": "senior"},
                    **{"Idempotency-Key": "k" * idem_gate.MAX_IDEMPOTENCY_KEY_LEN})
    assert at_bound.status_code == 200, at_bound.text
    assert stones(world, pid) == before - TIER_COST[BattlePassTier.SENIOR]


def test_a_broke_player_is_refused_and_may_retry_after_a_top_up(world):
    """Tap buy while short, top up, tap buy again: it works.

    Measured: the ledger key is handed back on the refusal, so the retry is a
    genuine first attempt rather than a replay reporting a purchase that never
    happened. This is an entirely ordinary player sequence.
    """
    session = make_player(world, stones=0)
    pid = session["player_id"]
    cost = TIER_COST[BattlePassTier.SENIOR]

    refused = buy(world, session, "senior", "broke-1")
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"] == "Insufficient Spirit Stones"
    assert stones(world, pid) == 0

    ledger_key = "bp_upgrade:%s:%s:senior" % (pid, battle_pass.get_season_id())
    assert ledger_key not in battle_pass._MEM_PURCHASE_KEYS, (
        "the refusal left the key reserved; a top-up could never complete")

    set_stones(world, pid, 5_000)
    retried = buy(world, session, "senior", "broke-2")
    assert retried.status_code == 200, retried.text
    assert retried.json()["replayed"] is False
    assert retried.json()["cost"] == cost
    assert stones(world, pid) == 5_000 - cost


def test_upgrade_answers_404_for_a_player_row_that_is_gone(world):
    """The existence check runs before the money is touched."""
    session = make_player(world)
    pid = session["player_id"]
    world["players_db"]._mem.pop(pid, None)
    world["players_db"]._known.discard(pid)

    resp = buy(world, session, "senior", "ghost-1")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Player not found"


# ---------------------------------------------------------------------------
# 6b. THE PRICE IS THE SERVER'S, NAMED BY NOBODY
# ---------------------------------------------------------------------------
# Kills the injected defect ``upgrade-caller-priced``.
#
# WHY THIS TEST HAD TO BE WRITTEN. The tests above this one all buy a track with
# NO price parameter anywhere in the request. Every one of them therefore still
# passes against a route that reads its price off the caller: the caller said
# nothing, so the caller's number and the server's number are the same number.
# A green suite proved only that nothing was WRONG -- never that the price was
# UNAVAILABLE to a caller. That is the whole shape of a surviving mutant: the
# suite exercised the code without ever putting the attacker in position.
#
# So this test does the one thing the others do not: it NAMES a price. The
# caller offers 0 in the query string AND in the body, under three different
# spellings a real client might reach for, and the server must charge its own.
#
# The purse is asserted as well as the reply. A route can echo back the number
# it was told while charging another, so a reply-only assertion would pass
# against the worst version of this defect.

_CALLER_PRICE_SPELLINGS = ("cost", "price", "amount")


def _named_price_request(session: Dict[str, Any], pid: str, tier: str,
                         key: str, offer: int):
    """An upgrade request that NAMES a price, in every place one could hide.

    ``offer`` is the number the caller tries to pay:

    * ``offer=1`` -- the realistic attack. A caller who offers one stone for a
      999-stone track is not asking for something absurd, so a route that
      charges the caller's number looks entirely plausible in production. The
      track is cheap and the purchase succeeds, which is the whole theft.
    * ``offer=0`` -- the boundary. A route that reads the caller's price meets
      the shipped ``cost <= 0`` guard and refuses with a different reason, so
      this arm distinguishes "charged the caller's number" from "refused for a
      different reason".
    """
    named = {name: offer for name in _CALLER_PRICE_SPELLINGS}
    return {
        "params": dict(named, tier=tier),
        "json": dict(named, tier=tier),
        "headers": dict(session["headers"], **{"Idempotency-Key": key}),
    }


def test_the_upgrade_price_is_the_servers_even_when_the_caller_names_one(world):
    """The server prices the track. A caller's number is never read.

    Measured against the shipped route: this request still answers
    ``cost == TIER_COST[SENIOR]`` and debits exactly that much.

    The caller offers ONE stone for a 999-stone track. That is the number that
    makes this test worth having: it is small enough to look like a rounding
    slip or a discount flag, so a route that charged it would pass review, and
    it is far enough from the server's price that no honest arithmetic reaches
    it by accident.
    """
    session = make_player(world)
    pid = session["player_id"]
    server_price = TIER_COST[BattlePassTier.SENIOR]
    assert server_price > 1, "a 1-stone track cannot prove anything"
    before = stones(world, pid)

    request = _named_price_request(session, pid, "senior", "named-price-1", offer=1)
    resp = world["client"].post(path_for("upgrade", pid), **request)

    assert resp.status_code == 200, resp.text
    assert resp.json()["cost"] == server_price, (
        "the reply reported a price the CALLER supplied: %r, server price is %r"
        % (resp.json()["cost"], server_price))
    assert stones(world, pid) == before - server_price, (
        "the PURSE moved by something other than the server's price, so the "
        "route charged a caller's number even if it echoed the right one")


def test_a_broke_player_naming_a_free_price_is_still_refused_for_being_broke(world):
    """The affordability gate reads the real purse, not the caller's number.

    The other arm, and the dangerous half. If the route read the caller's price
    for its BALANCE CHECK as well as its debit, a player holding nothing could
    pass ``cost=0``, be told the server price is 999, and still walk away with
    the paid track. The refusal therefore has to come from the purse.

    Note the refusal is asserted by its REASON, not only its status code. A
    route that priced from the caller meets the shipped ``cost <= 0`` guard and
    answers 400 too -- with a different detail. Asserting the status alone would
    let that mutant through wearing the same number.
    """
    session = make_player(world, stones=0)
    pid = session["player_id"]
    server_price = TIER_COST[BattlePassTier.SENIOR]

    request = _named_price_request(session, pid, "senior", "named-price-2", offer=0)
    resp = world["client"].post(path_for("upgrade", pid), **request)

    assert resp.status_code == 400, (
        "a player holding nothing bought a %d-stone track by naming price 0: %s"
        % (server_price, resp.text))
    assert "insufficient" in resp.json()["detail"].lower(), (
        "refused for the wrong reason (%r): the route is no longer deciding "
        "affordability from the player's purse"
        % resp.json()["detail"])
    assert stones(world, pid) == 0, "the refusal still moved the purse"

    # And nothing was quietly granted behind the refusal.
    screen = call(world, "get", session, pid).json()["battle_pass"]
    assert screen["tier"] == "free", (
        "the player owns the paid track after a refusal")


# ---------------------------------------------------------------------------
# 7. POST /battle-pass/{player_id}/weekly-bonus -- the clock decides the week
# ---------------------------------------------------------------------------

def test_weekly_bonus_carries_the_keys_a_paid_track_earns(world):
    """The reply nests the bonus under ``reward`` and echoes the week.

    The stones are credited ONCE and the experience is added ONCE. The route
    used to add the experience a second time, which turned a 5,000 bonus into
    10,000 with a stale stored level, so the purse delta and the stored level
    are both measured against the module's own table.
    """
    session = make_player(world)
    pid = session["player_id"]
    assert buy(world, session, "senior", "w-buy").status_code == 200
    before_stones = stones(world, pid)
    before_row = world["players_db"][pid]["battle_pass"]

    resp = call(world, "weekly", session, pid, params={"week": 1})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == WEEKLY_KEYS, (
        "the weekly reply carries %s; the pinned contract is %s"
        % (sorted(body), sorted(WEEKLY_KEYS)))
    assert body["ok"] is True and body["week"] == 1
    assert set(body["reward"]) == WEEKLY_REWARD_KEYS, sorted(body["reward"])
    bonus = battle_pass.WEEKLY_BONUS[BattlePassTier.SENIOR]
    assert body["reward"]["xp_gained"] == bonus["xp"]
    assert body["reward"]["spirit_stones"] == bonus["spirit_stones"]
    assert stones(world, pid) - before_stones == bonus["spirit_stones"]
    after_row = world["players_db"][pid]["battle_pass"]
    assert after_row["xp"] - before_row["xp"] == bonus["xp"], (
        "the bonus added the experience twice (or not at all)")
    assert after_row["level"] == body["new_level"], (
        "the stored level does not match the experience actually held")


def test_weekly_bonus_refuses_a_replayed_week_and_one_the_clock_has_not_reached(world):
    """Once per week, and only a week the server clock says has begun."""
    session = make_player(world)
    pid = session["player_id"]
    assert buy(world, session, "senior", "w2-buy").status_code == 200

    assert call(world, "weekly", session, pid,
                params={"week": 1}).status_code == 200
    replay = call(world, "weekly", session, pid, params={"week": 1})
    assert replay.status_code == 400, replay.text
    assert replay.json()["detail"] == "Already claimed"

    # The ceiling comes from the calendar month the server is in.
    season = battle_pass.get_season_id()
    weeks = max(1, -(-(battle_pass.get_season_end(season)
                       - battle_pass.get_season_start(season)) // (7 * 86400)))
    for label, week in (("zero", 0), ("negative", -5),
                        ("one past the season", weeks + 1),
                        ("absurd", 1_000_000_000)):
        resp = call(world, "weekly", session, pid, params={"week": week})
        assert resp.status_code == 400, "%s -> %s" % (label, resp.text)
        assert resp.json()["detail"] == "Week must be 1-%d" % weeks, (
            "the refusal does not name the range the clock implies: %s"
            % resp.json()["detail"])


def test_weekly_bonus_refuses_the_free_track_by_name(world):
    """The bonus is paid-track only, and the refusal says so."""
    session = make_player(world)
    pid = session["player_id"]
    before = stones(world, pid)

    resp = call(world, "weekly", session, pid, params={"week": 1})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Free tier has no weekly bonus"
    assert stones(world, pid) == before


def test_weekly_bonus_names_a_missing_week_parameter(world):
    """``week`` is a bare scalar, so it is a query parameter and its absence is
    a 422 that says where it belongs.
    """
    session = make_player(world)
    pid = session["player_id"]
    assert buy(world, session, "senior", "w3-buy").status_code == 200

    resp = call(world, "weekly", session, pid)

    assert resp.status_code == 422, resp.text
    assert any(d.get("loc") == ["query", "week"]
               for d in resp.json()["detail"]), resp.text


def test_all_six_per_player_routes_answer_the_same_404_for_a_gone_row(world):
    """Every per-player route agrees on a deleted row: 404, with the same message.

    The weekly-bonus route is in this list because it was the one exception. It
    opened the transaction before checking existence, and
    ``PlayerStore.transaction`` raises ``KeyError`` for an id it does not hold,
    so the raise happened before the check could answer and a deleted account
    produced a 500 -- a server fault in every operator's dashboard, from a
    routine "my account was deleted" moment.

    The check now runs before the block on all six -- ``summary`` checks twice,
    once outside the block and once inside it. Asserted as ONE loop over every
    route so the asymmetry cannot come back on ONE of them unnoticed: the
    per-route 404 tests elsewhere in this file would each have caught it too,
    but they were written when the bug was live and the point here is that the
    six agree with each other, which no single-route test can say.
    """
    session = make_player(world)
    pid = session["player_id"]
    # Control: the row is reachable with a valid session before it is removed.
    assert call(world, "get", session, pid).status_code == 200

    world["players_db"]._mem.pop(pid, None)
    world["players_db"]._known.discard(pid)

    calls = {
        "get": {},
        "summary": {},
        "claim": {"level": 1, "tier": "free"},
        "claim_all": {"tier": "free"},
        "upgrade": {"tier": "senior"},
        "weekly": {"week": 1},
    }
    for name, params in calls.items():
        resp = call(world, name, session, pid, params=params,
                    **{"Idempotency-Key": "gone-1"})
        assert resp.status_code == 404, "%s -> %s" % (name, resp.text)
        assert resp.json()["detail"] == "Player not found", (
            "%s does not use the module's own 404 message" % name)


# ---------------------------------------------------------------------------
# 8. Identity -- the session is the authority, the path is a claim about it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["get", "summary", "claim", "claim_all",
                                   "upgrade", "weekly"])
def test_every_per_player_route_refuses_an_anonymous_caller(world, name):
    """401 with no credential at all, on every route that names a player.

    Asserted as a pair: with a valid session the same call is NOT a 401, so a
    route that refused everything would fail.
    """
    session = make_player(world)
    params = {"level": 1, "tier": "free", "week": 1}

    anonymous = call(world, name, None, "somebody", params=params,
                     **{"Idempotency-Key": "anon-1"})
    assert anonymous.status_code == 401, anonymous.text

    allowed = call(world, name, session, session["player_id"], params=params,
                   **{"Idempotency-Key": "anon-2"})
    assert allowed.status_code != 401, allowed.text


def test_a_bogus_bearer_token_is_refused_on_every_per_player_route(world):
    """A made-up token is 401 everywhere, so the guard is not route-specific."""
    bogus = {"Authorization": "Bearer not-a-real-token"}

    for name in ("get", "summary", "claim", "claim_all", "upgrade", "weekly"):
        resp = call(world, name, None, "somebody",
                    params={"level": 1, "tier": "free", "week": 1}, **bogus)
        assert resp.status_code == 401, "%s -> %s" % (name, resp.text)


def test_a_session_cannot_reach_another_players_pass_by_editing_the_url(world):
    """Naming somebody else is 403 on every per-player route.

    The path keeps a player id because the shipped client calls these URLs, so
    the session is the authority and the path is only a claim about it.
    """
    actor = make_player(world)
    victim = make_player(world)
    reward = battle_pass.reward_for(1, BattlePassTier.FREE)

    for name, params in (("get", {}),
                         ("summary", {}),
                         ("claim", {"level": 1, "tier": "free"}),
                         ("claim_all", {"tier": "free"}),
                         ("upgrade", {"tier": "senior"}),
                         ("weekly", {"week": 1})):
        resp = call(world, name, actor, victim["player_id"], params=params,
                    **{"Idempotency-Key": "crossed-1"})
        assert resp.status_code == 403, "%s -> %s" % (name, resp.text)

    # The victim's purse never moved and their own session still reads clean.
    assert stones(world, victim["player_id"]) == 100_000
    their_own = call(world, "get", victim, victim["player_id"])
    assert their_own.status_code == 200, their_own.text
    assert their_own.json()["battle_pass"]["claimed"]["free"] == []


def test_one_players_claim_never_moves_another_players_purse(world):
    """Two players, one position each: the isolation is asserted on the store."""
    first = make_player(world)
    second = make_player(world)
    reward = battle_pass.reward_for(1, BattlePassTier.FREE)

    for session in (first, second):
        resp = call(world, "claim", session, session["player_id"],
                    params={"level": 1, "tier": "free"})
        assert resp.status_code == 200, resp.text

    assert stones(world, first["player_id"]) == 100_000 + reward["spirit_stones"]
    assert stones(world, second["player_id"]) == 100_000 + reward["spirit_stones"]


# ---------------------------------------------------------------------------
# 9. The pure helpers -- the decisions the routes stand on
# ---------------------------------------------------------------------------

def test_the_level_curve_is_a_consistent_pair_of_inverses():
    """``get_level_for_xp`` and ``get_xp_for_level`` must not disagree.

    Asserted as a relationship over the whole track rather than a spot check:
    for every level, the experience that reaches it buys exactly that level, and
    the ceiling is never exceeded.
    """
    for level in range(1, MAX_LEVEL + 1):
        assert battle_pass.get_level_for_xp(
            battle_pass.get_xp_for_level(level)) == level, (
            "the experience that reaches level %d buys a different level" % level)
    assert battle_pass.get_level_for_xp(0) == 1
    assert battle_pass.get_level_for_xp(-500) == 1
    assert battle_pass.get_level_for_xp(battle_pass.TOTAL_XP) == MAX_LEVEL
    assert battle_pass.get_level_for_xp(battle_pass.TOTAL_XP * 100) == MAX_LEVEL
    assert battle_pass.get_xp_for_level(0) == 0
    assert battle_pass.get_xp_for_level(-1) == 0


def test_a_single_grant_is_capped_and_a_negative_grant_is_refused_outright():
    """Experience is earned, never spent, and one call cannot mint a season.

    A silent zero would let a caller believe it had applied a correction, so a
    negative amount RAISES rather than clamps. The ceiling is read from
    ``MAX_XP_PER_GRANT``.
    """
    bp = PlayerBattlePass(season_id="2026-01")

    capped = battle_pass.add_xp(bp, MAX_LEVEL * 10_000)
    assert capped["xp_gained"] == battle_pass.MAX_XP_PER_GRANT
    assert capped["xp_clamped"] is True
    assert capped["total_xp"] == battle_pass.MAX_XP_PER_GRANT
    assert capped["leveled_up"] is True

    exact = battle_pass.add_xp(bp, 10)
    assert exact["xp_gained"] == 10 and exact["xp_clamped"] is False

    for label, amount in (("negative", -1), ("none", None), ("not a number", "abc")):
        with pytest.raises(ValueError):
            battle_pass.add_xp(bp, amount)
    assert bp.xp == battle_pass.MAX_XP_PER_GRANT + 10, (
        "a refused grant moved the experience anyway")


def test_a_stored_row_of_the_wrong_shape_still_gives_the_player_a_usable_pass():
    """A row may hold the track as text, hold a level off the scale, or hold
    duplicated claim ids. None of them may lock the player out.
    """
    bp = PlayerBattlePass(season_id="2026-01", tier="senoir",
                          level=9999, claimed_free=[1, 1, "2", "x", None])
    assert bp.tier is BattlePassTier.FREE, "an unreadable track fell back to paid"
    assert bp.level == MAX_LEVEL
    assert bp.claimed_free == [1, 2], (
        "duplicate and unreadable claim ids were kept; the panel draws a row the "
        "player already owns")

    missing = PlayerBattlePass(season_id="2026-01", tier=None, level=None, xp=None)
    assert missing.tier is BattlePassTier.FREE
    assert missing.level == 1 and missing.xp == 0


def test_the_reward_table_reads_off_the_track_as_none_rather_than_an_empty_dict():
    """``None`` rather than ``{}``: an unchecked level would otherwise answer
    200 having paid nothing and marked nothing, which is a success the player
    can neither use nor retry.
    """
    for level in (0, -1, MAX_LEVEL + 1, "5", None):
        assert battle_pass.reward_for(level, BattlePassTier.FREE) is None
    reward = battle_pass.reward_for(1, BattlePassTier.SENIOR)
    assert isinstance(reward, dict) and reward


def test_the_three_tracks_are_genuinely_different_not_one_with_bigger_numbers():
    """Every paid track carries something the free one does not.

    A "premium" tier that is the free tier with the numbers doubled is not a
    tier, it is a multiplier, and a player can tell. So the claim is structural:
    the key SETS differ, not just the values.
    """
    free_keys = set(battle_pass.reward_for(10, BattlePassTier.FREE))
    senior_keys = set(battle_pass.reward_for(10, BattlePassTier.SENIOR))
    super_keys = set(battle_pass.reward_for(10, BattlePassTier.SUPER))

    assert senior_keys - free_keys == {"gacha_tickets", "cosmetic"}, (
        "the senior track is the free track with different numbers")
    assert super_keys - free_keys == {"gacha_tickets", "cosmetic"}
    for tier in BattlePassTier:
        for level in range(1, MAX_LEVEL + 1):
            reward = battle_pass.reward_for(level, tier)
            assert reward is not None, "%s level %d has no reward" % (tier, level)
            assert reward["spirit_stones"] > 0 and reward["qi"] > 0, (
                "a position that pays nothing is a promise the server cannot keep")


def test_buying_a_higher_track_implies_the_lower_one_and_never_goes_backwards():
    """``upgrade_tier`` moves a player up a track only, and stamps the time."""
    bp = PlayerBattlePass(season_id="2026-01")

    assert battle_pass.upgrade_tier(bp, BattlePassTier.FREE) is False
    assert bp.purchased_at is None
    assert battle_pass.upgrade_tier(bp, BattlePassTier.SENIOR) is True
    assert bp.tier is BattlePassTier.SENIOR and bp.purchased_at is not None

    assert battle_pass.upgrade_tier(bp, BattlePassTier.SENIOR) is False, (
        "buying the track you already hold moved you")
    assert battle_pass.upgrade_tier(bp, BattlePassTier.FREE) is False
    assert bp.tier is BattlePassTier.SENIOR
    assert battle_pass.upgrade_tier(bp, BattlePassTier.SUPER) is True
    assert bp.tier is BattlePassTier.SUPER


def test_a_finished_season_is_banked_and_the_new_one_starts_empty():
    """The paid track does NOT carry over: a new season is a new purchase.

    Also asserted: the banked entry keeps the track the player BOUGHT, because
    without it support cannot answer "I bought this, where is it", and the
    bank's real end is reported separately from the moment the player logged in.
    """
    old = battle_pass.get_season_id(1_700_000_000)  # a real past month
    player = {
        "battle_pass": {
            "season_id": old, "tier": "super", "xp": 12_345, "level": 9,
            "claimed_free": [1, 2], "claimed_senior": [1, 2],
            "claimed_super": [1], "weekly_bonus_claimed": [1],
            "purchased_at": 1_700_000_000.0,
        }
    }

    fresh = battle_pass.get_or_create_bp(player, time_now())

    assert fresh.season_id == battle_pass.get_season_id(time_now())
    assert fresh.tier is BattlePassTier.FREE, "the paid track carried over"
    assert fresh.xp == 0 and fresh.level == 1
    assert fresh.claimed_free == [] and fresh.weekly_bonus_claimed == []

    history = battle_pass.player_history(player)
    assert len(history) == 1
    banked = history[0]
    assert banked["season_id"] == old
    assert banked["tier_purchased"] == "super"
    assert banked["ended_at"] == battle_pass.get_season_end(old)
    assert banked["observed_at"] != banked["ended_at"], (
        "the bank reports the login moment as the season's end, so a late "
        "returner is told the season lasted longer than it did")


def time_now() -> float:
    """The server's own clock, read through the module's default path."""
    import time
    return time.time()


def test_the_season_bank_is_bounded_and_free_of_duplicates():
    """The rollover runs on every pass-touching call, so two calls in the same
    new season must not bank the same season twice, and the bank is bounded.
    """
    old = battle_pass.get_season_id(1_700_000_000)
    player = {"battle_pass": {"season_id": old, "level": 4, "xp": 900,
                              "claimed_free": [1, 2, 3]}}
    for _ in range(3):
        battle_pass.get_or_create_bp(player, time_now())

    assert len(battle_pass.player_history(player)) == 1

    for month in range(2, battle_pass.SEASON_HISTORY_LIMIT + 4):
        summary = {"season_id": "1999-%02d" % month, "season_number": month,
                   "final_level": 1}
        battle_pass.record_season_history(player, summary)

    bank = battle_pass.player_history(player)
    assert len(bank) == battle_pass.SEASON_HISTORY_LIMIT, (
        "the season bank grew without bound")
    assert bank[0]["season_id"] == "1999-%02d" % (battle_pass.SEASON_HISTORY_LIMIT + 3), (
        "the newest season is not at index 0; a returning player reads the "
        "wrong entry")


def test_the_two_spirit_stone_helpers_agree_on_both_stored_shapes():
    """The canonical row stores stones as a tiered dict; older fixtures store a
    plain integer. Both are the F2P (free-to-play) balance, and the two helpers
    must agree about it on both shapes.
    """
    for row, expected in (({"spirit_stones": {"low_grade": 750}}, 750),
                          ({"spirit_stones": 750}, 750),
                          ({"spirit_stones": {"high_grade": 9}}, 0),
                          ({}, 0),
                          ({"spirit_stones": None}, 0)):
        assert battle_pass.stone_balance(row) == expected, row

    dict_row = {"spirit_stones": {"low_grade": 100, "high_grade": 5}}
    battle_pass.grant_stones(dict_row, 250)
    assert dict_row["spirit_stones"] == {"low_grade": 350, "high_grade": 5}

    scalar_row = {"spirit_stones": 100}
    battle_pass.grant_stones(scalar_row, 250)
    assert scalar_row["spirit_stones"] == 350

    # A grant of nothing or a negative is a no-op, never a debit.
    for amount in (0, -50, None):
        before = battle_pass.stone_balance(dict_row)
        battle_pass.grant_stones(dict_row, amount)
        assert battle_pass.stone_balance(dict_row) == before


def test_the_no_database_ledgers_answer_a_replay_with_the_first_attempts_receipt():
    """Both ledgers fall back to process-local storage here, and both store the
    RECEIPT rather than a bare marker, so a replay is answered from what the
    first attempt paid.

    The three-step dance is what makes it a test rather than an assertion: the
    same key is reserved twice, and the second answer must carry the first
    attempt's reward.
    """
    store = PlayerStore()
    season = battle_pass.get_season_id()
    reward = {"spirit_stones": 55, "qi": 110}

    assert battle_pass.reserve_claim(store, "p", season, "free", 1) == (True, None)
    battle_pass.record_claim_reward(store, "p", season, "free", 1, reward)
    is_first, stored = battle_pass.reserve_claim(store, "p", season, "free", 1)
    assert is_first is False
    assert stored == {"reward": reward}, (
        "the replay lost the first attempt's reward; a client that timed out "
        "reads an empty reward while holding real money")

    # The stored level is OPTIONAL: a receipt that claims level 1 for a level-30
    # player makes the replay answer a number the player is not at.
    assert "new_level" not in stored
    battle_pass.release_claim(store, "p", season, "free", 1)
    assert battle_pass.reserve_claim(store, "p", season, "free", 1)[0] is False, (
        "a PAID claim was released; the same reward would be paid twice")


def test_a_refused_claim_gives_the_position_back_but_a_paid_one_never_does(world):
    """The no-database dict has no rollback, so ``release_claim`` is the only
    thing standing between a refusal and a deleted reward.
    """
    store = PlayerStore()
    season = battle_pass.get_season_id()

    assert battle_pass.reserve_claim(store, "r", season, "free", 3) == (True, None)
    battle_pass.release_claim(store, "r", season, "free", 3)
    assert battle_pass.reserve_claim(store, "r", season, "free", 3)[0] is True, (
        "the refused position stayed reserved and the player can never claim it")

    assert battle_pass.reserve_claim(store, "r", season, "free", 4) == (True, None)
    battle_pass.record_claim_reward(store, "r", season, "free", 4,
                                    {"spirit_stones": 1})
    battle_pass.release_claim(store, "r", season, "free", 4)
    assert battle_pass.reserve_claim(store, "r", season, "free", 4)[0] is False, (
        "a PAID claim was released; the same reward would be paid twice")


def test_a_refused_purchase_gives_the_key_back_so_the_retry_can_complete():
    """A purchase's intent is the key, so an under-funded attempt must hand it
    back or the player can never complete it after topping up.
    """
    key = "bp_upgrade:refused-player:%s:senior" % battle_pass.get_season_id()
    assert battle_pass.reserve_purchase(PlayerStore(), key, "p", "s", "senior", 1) \
        == (True, None)
    battle_pass.release_purchase(key)
    assert battle_pass.reserve_purchase(
        PlayerStore(), key, "p", "s", "senior", 1)[0] is True, (
        "the refused purchase kept its key; a top-up could never complete it")


def test_the_weekly_bonus_is_paid_tracks_only_once_per_week_and_clock_bounded():
    """The helper's own contract, measured without HTTP.

    The week is a POSITION IN THE SEASON and the server clock decides how many
    exist, so an attacker who supplies the id cannot mint one per invented id.
    """
    season = battle_pass.get_season_id()
    weeks = max(1, -(-(battle_pass.get_season_end(season)
                       - battle_pass.get_season_start(season)) // (7 * 86400)))

    free = PlayerBattlePass(season_id=season)
    ok, payload = battle_pass.claim_weekly_bonus(free, 1)
    assert ok is False and payload["error"] == "Free tier has no weekly bonus"

    paid = PlayerBattlePass(season_id=season, tier=BattlePassTier.SENIOR)
    assert battle_pass.claim_weekly_bonus(paid, 1)[0] is True
    again = battle_pass.claim_weekly_bonus(paid, 1)
    assert again[0] is False and again[1]["error"] == "Already claimed"

    for week in (0, -5, weeks + 1, 1_000_000_000):
        ok, payload = battle_pass.claim_weekly_bonus(paid, week)
        assert ok is False, week
        assert payload["error"] == "Week must be 1-%d" % weeks


def test_the_claim_decision_refuses_every_reason_and_moves_nothing_on_a_refusal():
    """``claim_reward`` is the pure decision the route stands on: a position off
    the track, one above the player, one already claimed, and a track not held.
    """
    bp = PlayerBattlePass(season_id="2026-01", level=3)

    for level in (0, -1, MAX_LEVEL + 1):
        ok, payload = battle_pass.claim_reward(bp, level, BattlePassTier.FREE)
        assert ok is False and payload["error"] == "Level not reached"

    ok, payload = battle_pass.claim_reward(bp, 4, BattlePassTier.FREE)
    assert ok is False and payload["error"] == "Level not reached"

    for tier in (BattlePassTier.SENIOR, BattlePassTier.SUPER):
        ok, payload = battle_pass.claim_reward(bp, 1, tier)
        assert ok is False and payload["error"] == "Tier not unlocked"

    ok, reward = battle_pass.claim_reward(bp, 1, BattlePassTier.FREE)
    assert ok is True and reward == battle_pass.reward_for(1, BattlePassTier.FREE)
    assert bp.claimed_free == [1]

    ok, payload = battle_pass.claim_reward(bp, 1, BattlePassTier.FREE)
    assert ok is False and payload["error"] == "Already claimed"
    assert bp.claimed_free == [1], "a refused claim mutated the player's row"


def test_the_available_reward_list_hides_tracks_the_player_does_not_hold():
    """The list is the panel's rendering input, so it must not offer a track the
    player cannot buy.
    """
    free = PlayerBattlePass(season_id="2026-01", level=3)
    available = battle_pass.get_available_rewards(free)
    assert set(available) == AVAILABLE_KEYS
    assert len(available["free"]) == 3
    assert available["senior"] == [] and available["super"] == []

    senior = PlayerBattlePass(season_id="2026-01", level=3,
                              tier=BattlePassTier.SENIOR)
    paid = battle_pass.get_available_rewards(senior)
    assert len(paid["free"]) == 3 and len(paid["senior"]) == 3
    assert paid["super"] == []

    super_ = PlayerBattlePass(season_id="2026-01", level=3,
                              tier=BattlePassTier.SUPER)
    everything = battle_pass.get_available_rewards(super_)
    assert all(len(everything[t.value]) == 3 for t in BattlePassTier)


def test_a_receipt_read_back_from_a_database_tolerates_a_text_driver():
    """A JSON column may come back as text, bytes or a dict depending on the
    driver, and a receipt that reads as empty makes a paid claim look unpaid.
    """
    receipt = {"reward": {"spirit_stones": 55}}
    assert battle_pass._as_json(receipt) == receipt
    assert battle_pass._as_json('{"reward": {"spirit_stones": 55}}') == receipt
    assert battle_pass._as_json(b'{"reward": {"spirit_stones": 55}}') == receipt
    for bad in (None, "", "not json", "[1, 2]", 17, object()):
        assert battle_pass._as_json(bad) is None, repr(bad)


# ---------------------------------------------------------------------------
# 10. The reward REACHES the player -- not just the stones
# ---------------------------------------------------------------------------
# A claim that reports a reward it never credits is the worst failure this file
# can catch: the reply looks perfect, the panel animates, and the player is
# short. So every COMPONENT of a paid reward is asserted on the store, measured
# against the table's own numbers read at run time.

def test_every_component_of_a_paid_reward_reaches_the_player(world):
    """Stones, experience, tickets, items and the cosmetic are all credited.

    Three positions chosen because between them they exercise every field the
    Senior track pays: level 5 carries an ITEM, level 10 carries a GACHA
    TICKET, level 25 carries a COSMETIC. Each is compared against
    ``REWARD_TABLES`` so retuning a track leaves this test green, while a
    component that stops being credited does not.

    EACH POSITION IS MEASURED ON ITS OWN, IMMEDIATELY AFTER ITS OWN CLAIM. The
    earlier version added the three rows together and compared the total, which
    cannot tell the positions apart: a payout that sent level 10 row 5's stones
    and level 5 row 10's balances to the same total and passes. Reading the
    store around each claim instead means one position's wrong row has nowhere
    to hide, and the whole bag is compared field by field so a position that
    promises no item must not write one either.

    Experience is measured through ``qi_rewards.read_qi`` rather than by
    indexing ``player["qi"]``: the breakthrough gate reads ``progress``, and a
    helper that reported a paid reward in a field nothing reads is the exact
    failure this section exists to catch.
    """
    session = _seed(world["players_db"], level=25, tier="senior")
    pid = session["player_id"]

    for level in (5, 10, 25):
        reward = battle_pass.reward_for(level, BattlePassTier.SENIOR)
        before = _read(world, pid)
        resp = call(world, "claim", session, pid,
                    params={"level": level, "tier": "senior"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["reward"] == reward, (
            "level %d: the reply carries %r and the table pays %r"
            % (level, resp.json()["reward"], reward))
        # stones, experience, tickets, items and cosmetics, all of them, for
        # THIS position only.
        _assert_paid_exactly(world, pid, before, reward, level, "senior")

    # The cosmetic is owned and stamped, not merely named. Read fresh: the
    # store hands out a copy, so the row taken before the claims is not the
    # row the claims wrote.
    cosmetic = battle_pass.REWARD_TABLES[BattlePassTier.SENIOR][25]["cosmetic"]
    row = world["players_db"][pid]
    assert row["cosmetics"][cosmetic]["owned"] is True
    assert row["cosmetics"][cosmetic]["granted_at"] > 0

    # A position whose row names NO item writes none. Asserted as an unchanged
    # snapshot read on both sides of the claim, because a stale snapshot on one
    # side would make the comparison vacuous.
    assert battle_pass.REWARD_TABLES[BattlePassTier.SENIOR][6]["items"] == []
    assert battle_pass.REWARD_TABLES[BattlePassTier.SENIOR][6].get(
        "gacha_tickets", 0) == 0
    before_quiet = _read(world, pid)
    quiet = call(world, "claim", session, pid,
                 params={"level": 6, "tier": "senior"})
    assert quiet.status_code == 200, quiet.text
    after_quiet = _read(world, pid)
    assert after_quiet["bag"] == before_quiet["bag"], (
        "the reward table names nothing at level 6 but the bag changed: %s -> %s"
        % (before_quiet["bag"], after_quiet["bag"]))
    assert after_quiet["cosmetics"] == before_quiet["cosmetics"], (
        "level 6 carries no cosmetic but one was granted")


def test_the_super_track_pays_the_most_and_still_pays_something_free_does_not():
    """The tracks are ordered by value, at every position.

    A retune that made Super cheaper than Senior would pass every other test in
    this file (they all read their own price) and quietly make the most
    expensive thing on the track the cheapest. The ORDER is the contract.
    """
    for level in range(1, MAX_LEVEL + 1):
        free = battle_pass.reward_for(level, BattlePassTier.FREE)
        senior = battle_pass.reward_for(level, BattlePassTier.SENIOR)
        super_ = battle_pass.reward_for(level, BattlePassTier.SUPER)
        assert senior["spirit_stones"] > free["spirit_stones"], (
            "level %d: the Senior track pays no more than the free one" % level)
        assert super_["spirit_stones"] > senior["spirit_stones"], (
            "level %d: the Super track pays no more than Senior" % level)
        assert super_["qi"] > senior["qi"] > free["qi"], level

    # And the price order holds on the purchase side too.
    assert TIER_COST[BattlePassTier.SUPER] > TIER_COST[BattlePassTier.SENIOR] \
        > TIER_COST[BattlePassTier.FREE] == 0


# ---------------------------------------------------------------------------
# 11. The earn path -- the only way XP moves
# ---------------------------------------------------------------------------

def test_grant_xp_is_the_earn_path_and_it_persists_to_the_stored_row():
    """``grant_xp`` is what every earn path calls, so its contract is pinned.

    Asserted as a RELATIONSHIP: the reply's total matches what the stored row
    holds, and the row survives a fresh read. A reply that reported a level the
    store does not hold would show the player a position they cannot claim.
    """
    player = {"id": "earn-1", "spirit_stones": {"low_grade": 0},
              "progress": 0.0, "inventory": {}}

    result = battle_pass.grant_xp(player, 500, source="event_quest")

    assert set(result) == {"ok", "source", "xp_gained", "new_level",
                           "leveled_up", "total_xp"}, sorted(result)
    assert result["ok"] is True
    assert result["source"] == "event_quest", (
        "the earn source is not echoed, so an audit cannot tell which system paid")
    assert result["xp_gained"] == 500 and result["total_xp"] == 500

    row = player["battle_pass"]
    assert row["xp"] == result["total_xp"], (
        "the reply reports experience the stored row does not hold")
    assert row["level"] == result["new_level"] == battle_pass.get_level_for_xp(500)
    assert row["season_id"] == battle_pass.get_season_id()

    # The level agrees with the curve's own inverse, and a second grant stacks.
    stacked = battle_pass.grant_xp(player, 500, source="daily_boss")
    assert stacked["total_xp"] == 1_000
    assert stacked["new_level"] == battle_pass.get_level_for_xp(1_000)


def test_grant_xp_refuses_a_removal_and_caps_a_single_grant():
    """Experience is earned, never spent -- measured on the function that
    actually writes the row, not only on the helper beneath it.

    ``add_xp`` is covered on its own elsewhere in this file. What is checked
    here is that ``grant_xp`` does not swallow the refusal: it must not leave a
    partially-written row behind when the amount is rejected.
    """
    player = {"id": "earn-2", "spirit_stones": {"low_grade": 0},
              "progress": 0.0, "inventory": {}}
    battle_pass.grant_xp(player, 100, source="test")
    held = player["battle_pass"]["xp"]

    for label, amount in (("negative", -1), ("none", None),
                          ("not a number", "abc")):
        with pytest.raises(ValueError):
            battle_pass.grant_xp(player, amount, source="control_" + label)
        assert player["battle_pass"]["xp"] == held, (
            "a refused grant (%s) still moved the stored experience" % label)

    capped = battle_pass.grant_xp(
        player, battle_pass.MAX_XP_PER_GRANT * 50, source="test")
    assert capped["xp_gained"] == battle_pass.MAX_XP_PER_GRANT, (
        "one in-process call minted more than the single-grant ceiling")


# ---------------------------------------------------------------------------
# 12. The season bank the client reads back
# ---------------------------------------------------------------------------

def test_a_banked_season_carries_every_key_the_archive_screen_draws(world):
    """The bank is served to the client as ``season_history``, so its row shape
    is a contract, and it is the only record that a purchase ever leaves.

    Asserted after a REAL rollover through the mounted route, so the row is the
    one the server actually writes rather than one built by hand.
    """
    old = battle_pass.get_season_id(1_700_000_000)
    players_db = world["players_db"]
    session = _seed(players_db, level=6, tier="senior", tag="bp-bank")
    pid = session["player_id"]

    row = players_db[pid]
    row["battle_pass"] = {
        "season_id": old, "tier": "senior", "xp": 1_234, "level": 6,
        "claimed_free": [1, 2, 3], "claimed_senior": [1, 2],
        "claimed_super": [], "weekly_bonus_claimed": [1],
        "purchased_at": 1_700_000_000.0,
    }
    players_db.persist(pid, row)

    body = call(world, "get", session, pid).json()

    assert len(body["season_history"]) == 1, body["season_history"]
    banked = body["season_history"][0]
    assert set(banked) == {
        "season_id", "season_number", "final_level", "final_xp",
        "tier_purchased", "purchased_at", "earned_free", "earned_paid",
        "unclaimed_free", "unclaimed_paid", "ended_at", "observed_at",
    }, ("the banked row carries %s" % sorted(banked))

    assert banked["season_id"] == old
    assert banked["tier_purchased"] == "senior", (
        "the bank dropped the track the player paid for, so support cannot "
        "answer 'I bought this, where is it'")
    assert banked["final_level"] == 6 and banked["final_xp"] == 1_234
    assert banked["ended_at"] == battle_pass.get_season_end(old)
    assert banked["unclaimed_free"] == 3, (
        "the bank lost count of the rows the player never claimed")
    assert banked["unclaimed_paid"] == 4, (
        "the bank lost count of the paid rows the player never claimed")
    assert set(banked["earned_free"]) == {
        "tiers_claimed", "spirit_stones", "qi", "gacha_tickets"}
    assert banked["earned_free"]["tiers_claimed"] == 3
    assert banked["earned_paid"]["tiers_claimed"] == 2

    # And the new season starts empty: the paid track does NOT carry over.
    live = body["battle_pass"]
    assert live["season"] ["season_id"] == battle_pass.get_season_id()
    assert live["tier"] == "free" and live["level"] == 1 and live["xp"] == 0
    assert live["claimed"]["senior"] == []


def test_the_track_view_marks_every_position_with_a_state_the_panel_can_draw(world):
    """Claimed, claimable, locked -- the three words a button is built from.

    Asserted as a TRANSITION rather than a membership test: the same position
    moves locked -> claimable -> claimed as the player reaches it and claims it,
    and a paid track's positions stay locked for a free player. A state the
    client cannot map is a dead button drawn as live.
    """
    session = _seed(world["players_db"], level=3)
    pid = session["player_id"]

    def state(level: int, tier: str) -> str:
        bp = call(world, "get", session, pid).json()["battle_pass"]
        row = next(r for r in bp["track"]["levels"] if r["level"] == level)
        return row[tier]["state"]

    assert state(1, "free") == "claimable", "a reached free row is not clickable"
    assert state(3, "free") == "claimable"
    assert state(4, "free") == "locked", "an unreached row is drawn as clickable"
    for tier in ("senior", "super"):
        assert state(1, tier) == "locked", (
            "%s is drawable by a player who never bought it" % tier)

    assert call(world, "claim", session, pid,
                params={"level": 1, "tier": "free"}).status_code == 200
    assert state(1, "free") == "claimed", "the row did not flip after the claim"

    paid = _seed(world["players_db"], level=3, tier="senior")
    bp = call(world, "get", paid, paid["player_id"]).json()["battle_pass"]
    rows = {r["level"]: r for r in bp["track"]["levels"]}
    assert rows[1]["senior"]["state"] == "claimable", (
        "the bought track is not clickable for the player who bought it")
    assert rows[1]["super"]["state"] == "locked", (
        "a Senior player was offered the Super track for free")
    assert rows[4]["senior"]["state"] == "locked"


# ---------------------------------------------------------------------------
# 14. The daily-boss strike this module's experience is granted by
# ---------------------------------------------------------------------------
# Kills the injected defect ``strike-ownership-dropped``.
#
# This file's whole surface is seven ``/battle-pass`` paths, and the strike route
# is not one of them -- it lives in ``main.py``. It is here anyway because this
# module's paid tiers are PAID FOR WITH THE EXPERIENCE THAT ROUTE GRANTS, so
# "can this id be struck for" is a question about this feature's money, not
# about somebody else's.
#
# WHY IT IS MOUNTED BARE, AND WHY THAT IS THE ONLY WAY. Through the real
# application this route answers 403 for an id the caller does not own, because
# ``main.py`` installs an ownership guard over the whole route table and that
# guard answers FIRST. So the route's own "does this player exist" check is
# unreachable behind it: delete that check and a caller still gets a 403, and a
# test written against the real app cannot tell the two apart. Mounting the
# module-level handler on a bare app, with no guard in front of it, is the only
# arrangement in which the handler's own refusal is observable.
#
# This is a real property, not a test trick. The guard is what protects the
# shipped app; the handler's own check is what protects any app that mounts the
# handler without one, and it is the line that decides whether
# ``players_db[player_id]`` -- the write this feature pays out on -- is reached
# for a row that is not there.

@pytest.fixture
def unguarded_strike() -> Iterator[TestClient]:
    """The shipped strike handler with no ownership guard in front of it.

    The handler reads a module-global store rather than taking it as an
    argument, so it needs no wiring to be served.
    """
    from server import main as server_main
    import main as bare_main
    handler = bare_main.post_daily_boss_strike

    app = FastAPI()
    app.post("/daily-boss/{player_id}/strike")(handler)
    yield TestClient(app, raise_server_exceptions=False)
    del server_main


def _strike_a_real_player() -> str:
    """Seed one real row into the store the bare handler reads, and return its id.

    Seeded THROUGH the store rather than created over HTTP: the bare app carries
    no ``/player/create``, and the point is the handler's own behaviour, not the
    creation route's.
    """
    from server import main as server_main

    pid = "strike-contract-%s" % uuid.uuid4().hex[:12]
    server_main.players_db[pid] = {
        "id": pid,
        "name": pid,
        "spirit_stones": {"low_grade": 10_000},
        "progress": {"realm": "qi", "stage": 1, "level": 1},
        "inventory": {},
    }
    return pid


def test_the_strike_route_refuses_an_id_it_does_not_own_on_its_own(unguarded_strike):
    """KILLS ``strike-ownership-dropped``.

    Mounted bare, the shipped handler answers 404 for an id the store does not
    hold. With its existence check deleted it reaches ``players_db[player_id]``
    for a row that is not there, and answers 500 instead. The assertion below
    fails on the deleted check and passes on the shipped one, so the difference
    is observable here and nowhere else.

    WHY IT MATTERS HERE: this is the write that pays experience toward a paid
    track, so "is this row real" is the only thing between a stranger's session
    and a credit on somebody's pass.
    """
    resp = unguarded_strike.post("/daily-boss/never-created/strike")

    assert resp.status_code == 404, (
        "an id the store does not hold answered %s instead of 404 -- the "
        "handler's own existence check is not doing its job: %s"
        % (resp.status_code, resp.text[:200]))
    assert resp.json()["detail"] == "Player not found"


def test_the_unguarded_strike_handler_still_serves_a_row_it_does_own(unguarded_strike):
    """The serving arm, so the refusal above cannot pass on a broken handler.

    A test that only asserts a refusal is passed just as happily by a handler
    that refuses everything. The route must still answer 200 for a row the store
    really holds, and must still grant the experience this feature is paid with.
    """
    pid = _strike_a_real_player()
    try:
        resp = unguarded_strike.post("/daily-boss/%s/strike" % pid)

        assert resp.status_code == 200, resp.text
        grant = resp.json().get("battle_pass") or {}
        assert grant.get("ok") is True, (
            "the strike did not grant experience: %r" % grant)
        assert grant.get("xp_gained", 0) > 0, (
            "the strike granted no experience, so the paid tiers this module "
            "sells have nothing to be paid with: %r" % grant)
    finally:
        from server import main as server_main
        for attr in ("_mem", "_known", "_keys"):
            container = getattr(server_main.players_db, attr, None)
            if isinstance(container, dict):
                container.pop(pid, None)
            elif isinstance(container, set):
                container.discard(pid)


# ---------------------------------------------------------------------------
# 13. The mount's optional third argument
# ---------------------------------------------------------------------------

def test_the_optional_wallet_argument_mounts_the_same_surface(world):
    """The third parameter exists for the payment-verifier callers.

    ``register_battle_pass_routes(app, store_provider, wallet_store_provider)``
    takes it so those callers keep working unchanged. This file's own ``world``
    passes only two arguments, so nothing here would notice the three-argument
    form breaking -- a mount that raised on the extra parameter would take the
    whole paid season down while this suite stayed green.

    Asserted as a set of (path, method) pairs, the same comparison the route
    table test uses, so "mounts the same surface" means the same paths and
    verbs rather than the same count.
    """
    three = FastAPI()
    battle_pass.register_battle_pass_routes(
        three, lambda: PlayerStore(), lambda: PlayerStore())

    def surface(app) -> set:
        found = set()
        for route in app.routes:
            p = getattr(route, "path", "")
            if not p.startswith("/battle-pass"):
                continue
            for method in set(getattr(route, "methods", set()) or set()) - {"HEAD"}:
                found.add((p, method))
        return found

    assert surface(three) == surface(world["app"]), (
        "the three-argument mount carries %s; the two-argument one carries %s"
        % (sorted(surface(three)), sorted(surface(world["app"]))))

    # And it actually SERVES, not just registers.
    client = TestClient(three, raise_server_exceptions=False)
    season = client.get("/battle-pass/season")
    assert season.status_code == 200, season.text
    assert set(season.json()) == SEASON_KEYS

# ---------------------------------------------------------------------------
# 14. Season boundaries -- the window is a calendar month, not a 30-day guess
# ---------------------------------------------------------------------------
#
# Section 9 asserts the rollover BANSKS a finished season. These assert the
# thing the rollover is keyed on: that a season's window is exactly one real
# calendar month, that the label changes at the same instant the window closes,
# and that a position earned in a season that has ended cannot be claimed in
# the next one.
#
# The 30-day version of this module computed the window as
# ``get_season_start(sid) + 30 * 86400`` in the machine's LOCAL zone. Both halves
# were wrong: a 31-day month reported progress 100% while ``season_id`` still
# named the old month (so the pass never rolled over), and February claimed to
# run two days past its own boundary. Asserted as a RELATIONSHIP against the
# stdlib calendar rather than as a restated day count, so the file does not
# carry its own copy of the bug's numbers.

import calendar  # noqa: E402


def test_a_season_window_is_exactly_one_calendar_month_and_chains_to_the_next():
    """Swept over four whole years, including a leap February.

    Three relationships per month, none of them a restated constant:

    1. the window's length equals that month's real number of days;
    2. the window ends exactly where the NEXT month's window begins;
    3. the label is stable at both ends of the window and flips AT the end.
    """
    checked = 0
    for year in (2023, 2024, 2025, 2026):
        for month in range(1, 13):
            sid = "%04d-%02d" % (year, month)
            start = battle_pass.get_season_start(sid)
            end = battle_pass.get_season_end(sid)
            real_days = calendar.monthrange(year, month)[1]
            checked += 1

            assert round((end - start) / 86400.0) == real_days, (
                "%s is %s days long but the calendar says %s; a window that is "
                "not the calendar month is what let a 31-day season report 100%% "
                "while its own label still named the old month"
                % (sid, (end - start) / 86400.0, real_days))

            assert end == battle_pass.get_season_start(
                battle_pass._season.next_season_id(sid)), (
                "%s does not end where the next season begins; two seasons can "
                "then both claim the same instant" % sid)

            # The label must not move INSIDE the window, and must move AT its end.
            assert battle_pass.get_season_id(start + 1) == sid, (
                "%s does not start at its own label" % sid)
            assert battle_pass.get_season_id(end - 1) == sid, (
                "%s ended a second early" % sid)
            assert battle_pass.get_season_id(end) != sid, (
                "%s did not roll over at its own boundary; nothing would ever "
                "bank it" % sid)

    assert checked == 48, "the sweep covered %s months, not 48" % checked


def test_a_season_is_over_at_its_end_and_not_one_second_earlier():
    """The exact instant a season closes is the thing a scheduler hangs on.

    ``server/battle_pass_season.py`` records the shipped defect this guards: a
    season that reported itself finished while its ``season_id`` still named it.
    So the two answers are asserted as a PAIR at both sides of the boundary.
    """
    for sid in ("2026-01", "2026-02", "2026-12"):
        end = battle_pass.get_season_end(sid)
        assert battle_pass._season.season_is_over(sid, end - 1) is False, (
            "%s calls itself over one second early" % sid)
        assert battle_pass._season.season_is_over(sid, end) is True, (
            "%s does not call itself over at its own boundary" % sid)
        assert battle_pass.get_season_id(end - 1) == sid
        assert battle_pass.get_season_id(end) == battle_pass._season.next_season_id(sid)


def test_a_position_earned_in_a_finished_season_cannot_be_claimed_in_the_next(world):
    """The boundary is enforced on the CLAIM path, not only on the read path.

    A player who reached level 9 on the Super track last month reads as level 1
    on the free track this month (measured), so the stored rows are a genuine
    cross-season claim attempt. Both tracks are tried, because the track the
    player bought is also season-scoped: last month's purchase does not carry.
    """
    session = make_player(world)
    pid = session["player_id"]
    finished = battle_pass.get_season_id(1_700_000_000)  # a real past month

    row = world["players_db"][pid]
    row["battle_pass"] = {
        "season_id": finished, "tier": "super",
        "xp": battle_pass.get_xp_for_level(9), "level": 9,
        "claimed_free": [], "claimed_senior": [], "claimed_super": [],
        "weekly_bonus_claimed": [], "purchased_at": time_now(),
    }
    world["players_db"].persist(pid, row)

    before = stones(world, pid)
    for tier in ("free", "senior", "super"):
        resp = call(world, "claim", session, pid,
                    params={"level": 9, "tier": tier})
        assert resp.status_code == 400, (
            "claiming last season's level 9 on the %s track answered %s"
            % (tier, resp.status_code))

    # The read agrees with the claim: the new season is level 1 and the old one
    # is banked, so the refusal is the boundary and not a missing unlock.
    view = call(world, "get", session, pid).json()
    assert view["battle_pass"]["season"]["season_id"] == battle_pass.get_season_id()
    assert view["battle_pass"]["level"] == 1
    assert view["battle_pass"]["tier"] == "free"
    assert [row["season_id"] for row in view["season_history"]] == [finished]
    assert stones(world, pid) == before, "a refused cross-season claim paid anyway"


# ---------------------------------------------------------------------------
# 15. Currency goes through wallet_dispatch, and the row alone never moves it
# ---------------------------------------------------------------------------
#
# Section 13 asserts the third mount ARGUMENT exists. These assert what the
# bound wallet actually DOES once it is there, which is the property the module
# was changed for and the one a mount-shape test cannot see:
#
#   * a claim credits the WALLET by the reward table's own number, not the row
#     only -- measured before the fix: a claim-all paying 990 stones moved the
#     player row 500 -> 1490 and left the wallet at 0;
#   * a purchase DEBITS the wallet when the wallet is the book holding the
#     money, because awards land there now;
#   * the two books are two VIEWS of one balance, so the larger pays and the
#     sum is never advertised.
#
# The real in-memory wallet is used rather than a fake, because the properties
# under test (the per-key idempotency ledger, the currency whitelist, the
# version guard) live inside it. A stub would satisfy a call assertion and
# prove nothing about the guard.

from wallet_defense import WalletStore as InMemoryWalletStore  # noqa: E402
from wallet_dispatch import earn as wallet_earn  # noqa: E402


def _wallet_world(stones=0):
    """An app whose wallet holds ``stones`` for one player, plus the pieces.

    Returns ``(client, players_db, wallet, session)``. The wallet is funded
    through ``wallet_dispatch.earn`` -- the same entry point the module uses --
    so the fixture cannot create a balance shape the real earn path would
    refuse.
    """
    players_db = PlayerStore()
    app = FastAPI()
    wallet = InMemoryWalletStore()
    battle_pass.register_battle_pass_routes(app, lambda: players_db,
                                            lambda: wallet)
    session = _seed(players_db, stones=0)
    pid = session["player_id"]
    wallet.create(pid, name=pid)
    if stones:
        receipt = wallet_earn(wallet, pid, battle_pass.WALLET_STONE_CURRENCY,
                              stones, "seed:%s" % pid, "seed")
        assert receipt.get("ok"), receipt
    return TestClient(app, raise_server_exceptions=False), players_db, wallet, session


def test_a_claim_credits_the_wallet_by_the_reward_tables_own_number():
    """The wallet is credited too, and by the table's number -- not a copy.

    Both books are written on purpose (see ``_pay_reward``): the pass charges
    the row, so a wallet-only credit would move the reward somewhere the pass
    cannot spend it. The contract asserted here is that neither book is left
    behind, and that the wallet's movement equals the table.
    """
    client, players_db, wallet, session = _wallet_world(stones=0)
    pid = session["player_id"]
    reward = battle_pass.reward_for(1, BattlePassTier.FREE)
    assert reward["spirit_stones"] > 0, "the free track pays no stones to check"

    before = battle_pass.wallet_stone_balance(wallet, pid)
    resp = client.post("/battle-pass/%s/claim?level=1&tier=free" % pid,
                       headers=session["headers"])
    assert resp.status_code == 200, resp.text
    after = battle_pass.wallet_stone_balance(wallet, pid)

    assert after - before == reward["spirit_stones"], (
        "the wallet moved %s but the reward table says %s"
        % (after - before, reward["spirit_stones"]))
    assert battle_pass.stone_balance(players_db[pid]) == reward["spirit_stones"], (
        "the row was left behind; the pass charges the row, so a wallet-only "
        "grant is money the player cannot spend on the next track")


def test_a_replayed_claim_credits_the_wallet_exactly_once_across_a_fresh_key():
    """The wallet's own ledger is keyed on the SERVER's key, not the header's.

    The claim ledger and the wallet are two independent guards on one property.
    Sending a brand-new ``Idempotency-Key`` walks past anything keyed on the
    header; only a server-derived key makes a double-tap a replay on both.
    """
    client, _players_db, wallet, session = _wallet_world(stones=0)
    pid = session["player_id"]
    url = "/battle-pass/%s/claim?level=1&tier=free" % pid

    first = client.post(url, headers=dict(session["headers"],
                                          **{"Idempotency-Key": "first"}))
    assert first.status_code == 200, first.text
    after_first = battle_pass.wallet_stone_balance(wallet, pid)

    replay = client.post(url, headers=dict(session["headers"],
                                           **{"Idempotency-Key": "brand-new-key"}))
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    after_replay = battle_pass.wallet_stone_balance(wallet, pid)

    assert after_replay == after_first, (
        "a replay under a fresh header key credited the wallet again "
        "(%s -> %s); the wallet key is the caller's to vary"
        % (after_first, after_replay))


def test_the_weekly_bonus_credits_the_wallet_rather_than_only_the_row():
    """The bonus is a third payout path and it takes the wallet leg too.

    It is deliberately NOT ``_pay_reward`` (it carries no qi or items), so it
    is a separate call site and a separate place for the wallet credit to be
    forgotten.
    """
    client, _players_db, wallet, session = _wallet_world(stones=0)
    pid = session["player_id"]

    row = _players_db[pid]
    row["battle_pass"] = {
        "season_id": battle_pass.get_season_id(), "tier": "senior",
        "xp": battle_pass.get_xp_for_level(5), "level": 5,
        "claimed_free": [], "claimed_senior": [], "claimed_super": [],
        "weekly_bonus_claimed": [], "purchased_at": time_now(),
    }
    _players_db.persist(pid, row)

    before = battle_pass.wallet_stone_balance(wallet, pid) or 0
    resp = client.post("/battle-pass/%s/weekly-bonus?week=1" % pid,
                       headers=session["headers"])
    assert resp.status_code == 200, resp.text
    paid = resp.json()["reward"]["spirit_stones"]
    after = battle_pass.wallet_stone_balance(wallet, pid)

    assert paid > 0, "the weekly bonus paid no stones to check"
    assert after - before == paid, (
        "the weekly bonus moved the wallet by %s but paid %s" % (after - before, paid))


def test_a_purchase_debits_the_wallet_when_the_wallet_is_the_book_that_holds_it():
    """A purchase charges the WALLET when the stones arrived through it.

    Every award now credits the wallet, so a player holding their money there
    has an empty player row. The row-only debit this replaces refused them for
    being broke while holding the exact price.
    """
    cost = TIER_COST[BattlePassTier.SENIOR]
    client, players_db, wallet, session = _wallet_world(stones=cost * 3)
    pid = session["player_id"]

    row_before = battle_pass.stone_balance(players_db[pid])
    wallet_before = battle_pass.wallet_stone_balance(wallet, pid)
    assert row_before == 0, "the fixture meant to leave the row empty"

    resp = client.post("/battle-pass/%s/upgrade?tier=senior" % pid,
                       headers=dict(session["headers"],
                                    **{"Idempotency-Key": "buy-1"}))
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_tier"] == "senior"
    assert resp.json()["cost"] == cost, "the route reported its own price"

    wallet_after = battle_pass.wallet_stone_balance(wallet, pid)
    assert wallet_before - wallet_after == cost, (
        "the wallet moved %s for a %s stone purchase"
        % (wallet_before - wallet_after, cost))
    assert battle_pass.stone_balance(players_db[pid]) == row_before, (
        "the empty row was charged as well; one purchase cannot cost two books")


def test_a_replayed_purchase_debits_the_wallet_exactly_once_across_a_fresh_key():
    """The purchase's wallet key is derived from (player, season, tier).

    Same property as the claim's, on the debit side: a retry that varies its
    header key must not walk past the store that owns money.
    """
    cost = TIER_COST[BattlePassTier.SENIOR]
    client, _players_db, wallet, session = _wallet_world(stones=cost * 3)
    pid = session["player_id"]
    url = "/battle-pass/%s/upgrade?tier=senior" % pid

    first = client.post(url, headers=dict(session["headers"],
                                          **{"Idempotency-Key": "buy-a"}))
    assert first.status_code == 200, first.text
    after_first = battle_pass.wallet_stone_balance(wallet, pid)

    replay = client.post(url, headers=dict(session["headers"],
                                           **{"Idempotency-Key": "buy-b"}))
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    after_replay = battle_pass.wallet_stone_balance(wallet, pid)

    assert after_replay == after_first, (
        "a replay under a fresh header key debited the wallet again "
        "(%s -> %s)" % (after_first, after_replay))


def test_the_two_books_are_one_balance_so_the_larger_pays_and_the_sum_is_never_offered():
    """600 in each book does not buy a 999 track.

    ``_pay_reward`` credits both books, so summing them would advertise a
    balance the player does not have and then decline a purchase either book
    alone covers. This is the assertion that keeps the two ledgers converging
    instead of drifting apart.
    """
    cost = TIER_COST[BattlePassTier.SENIOR]
    assert cost > 2, "this test needs a price two half-balances cannot reach"

    half = cost // 2 + 1
    client, players_db, wallet, session = _wallet_world(stones=half)
    pid = session["player_id"]
    row = players_db[pid]
    row["spirit_stones"] = {"low_grade": half}
    players_db.persist(pid, row)

    assert half * 2 > cost, "the fixture must make the sum look sufficient"
    assert max(half, half) < cost, "one book alone must not cover it"

    resp = client.post("/battle-pass/%s/upgrade?tier=senior" % pid,
                       headers=dict(session["headers"],
                                    **{"Idempotency-Key": "split-1"}))
    assert resp.status_code == 400, resp.text
    assert "Insufficient" in resp.json()["detail"]

    # And nothing moved on the refusal.
    assert battle_pass.stone_balance(players_db[pid]) == half
    assert battle_pass.wallet_stone_balance(wallet, pid) == half


def test_the_season_window_the_wallet_keys_on_is_the_same_one_the_ledger_is():
    """The wallet's purchase key and the purchase ledger share a season.

    ``wallet_upgrade_key`` is (player, season, tier) and the ledger row is keyed
    on the same tuple, so the two guards cannot disagree about which season a
    purchase belongs to. Asserted as a relationship by rebuilding the key from
    the module's own helpers rather than restating the format string.
    """
    pid, season, tier = "someone", "2026-03", "super"
    key = battle_pass.wallet_upgrade_key(pid, season, tier)
    assert key == "battle_pass:upgrade:%s:%s:%s" % (pid, season, tier), (
        "the wallet purchase key changed shape; the format is the shared "
        "identity with the purchase ledger")
    # Distinct per player, season and tier -- so no two intents collide.
    others = [battle_pass.wallet_upgrade_key(pid, "2026-04", tier),
              battle_pass.wallet_upgrade_key(pid, season, "senior"),
              battle_pass.wallet_upgrade_key("other", season, tier)]
    assert len(set([key] + others)) == 4, "two purchase intents share one key"


# ---------------------------------------------------------------------------
# 16. A refused purchase leaves the retry available to the person who can do it
# ---------------------------------------------------------------------------
#
# The purchase ledger is a process-global SET in no-database mode, with no
# rollback, so a refusal that reserved the key and never released it would burn
# the player's own retry forever: tap buy, get refused, tap recharge, tap buy
# again -- and the second tap is answered "already bought" while the player
# still holds the free track. `release_purchase` is the only thing standing
# between that and a player who can never complete a purchase.
#
# Membership is read off the module's own set rather than through
# ``reserve_purchase``, which would RESERVE the key and make the assertion
# unfalsifiable.

def _purchase_key(pid: str, tier: str) -> str:
    return "bp_upgrade:%s:%s:%s" % (pid, battle_pass.get_season_id(), tier)


@pytest.fixture
def clean_purchase_ledger():
    """Snapshot the process-global purchase set and restore it afterwards.

    The set is module state shared by every test in the process, so a test that
    left a key in it would change another test's answer.
    """
    before = set(battle_pass._MEM_PURCHASE_KEYS)
    yield
    battle_pass._MEM_PURCHASE_KEYS.clear()
    battle_pass._MEM_PURCHASE_KEYS.update(before)


def test_an_anonymous_or_crossed_out_purchase_refusal_does_not_burn_the_owners_key(
        world, clean_purchase_ledger):
    """A guard that runs BEFORE the ledger must not reach the ledger.

    Read as a three-step dance: two refusals that stop at the guard, then the
    owner buying normally. If either refusal had reserved the key, the owner's
    purchase would come back as a replay -- a 200 that granted nothing.
    """
    owner = make_player(world)
    thief = make_player(world)
    key = _purchase_key(owner["player_id"], "senior")
    assert key not in battle_pass._MEM_PURCHASE_KEYS

    anon = call(world, "upgrade", None, owner["player_id"],
                params={"tier": "senior"},
                **{"Idempotency-Key": "refuse-anon"})
    assert anon.status_code == 401, anon.text

    crossed = call(world, "upgrade", thief, owner["player_id"],
                   params={"tier": "senior"},
                   **{"Idempotency-Key": "refuse-cross"})
    assert crossed.status_code == 403, crossed.text

    assert key not in battle_pass._MEM_PURCHASE_KEYS, (
        "a request refused at the guard reserved the purchase key; the owner's "
        "own purchase is now answered as a replay")

    cost = TIER_COST[BattlePassTier.SENIOR]
    bought = call(world, "upgrade", owner, owner["player_id"],
                  params={"tier": "senior"},
                  **{"Idempotency-Key": "owner-buys"})
    assert bought.status_code == 200, bought.text
    assert bought.json()["replayed"] is False, (
        "the owner's genuine purchase came back as a replay")
    assert bought.json()["new_tier"] == "senior"
    assert stones(world, owner["player_id"]) == 100_000 - cost


def test_an_underfunded_purchase_can_be_retried_after_a_top_up(world,
                                                               clean_purchase_ledger):
    """The ordinary sequence: buy, be refused for being broke, top up, buy.

    Asserted end to end rather than on the key alone, because "the key came
    back" is only interesting if the retry then actually charges.
    """
    session = make_player(world, stones=10)
    pid = session["player_id"]
    key = _purchase_key(pid, "senior")

    broke = call(world, "upgrade", session, pid, params={"tier": "senior"},
                 **{"Idempotency-Key": "broke-1"})
    assert broke.status_code == 400, broke.text
    assert key not in battle_pass._MEM_PURCHASE_KEYS, (
        "an underfunded purchase kept its own retry key; the player can never "
        "complete it after topping up")

    set_stones(world, pid, 100_000)
    cost = TIER_COST[BattlePassTier.SENIOR]
    retried = call(world, "upgrade", session, pid, params={"tier": "senior"},
                   **{"Idempotency-Key": "broke-2"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["replayed"] is False
    assert retried.json()["new_tier"] == "senior"
    assert stones(world, pid) == 100_000 - cost, (
        "the retry charged a second time or not at all")