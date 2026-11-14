"""
Contract test for ``server/ghost_battle.py`` -- the five async-PvP ghost routes.

WHAT THIS FILE IS
-----------------
The module registers five POST routes and, as of this writing, has no client.
A route nobody calls is untested by definition, so this file pins the contract
from the SERVER side: what each route accepts, what it answers, what it refuses,
and -- most of all -- what it refuses to pay twice.

    POST /ghost/record    capture a finished PvP battle as a fightable ghost
    POST /ghost/list      fightable ghosts owned by OTHER players
    POST /ghost/fight     fight one, paid at most once per ghost per UTC day
    POST /ghost/fights    the challenger's own bounded fight scroll
    POST /ghost/preview   re-derive a ghost's source battle, writing nothing

WHY IT TARGETS THE REAL APP
---------------------------
``main.py`` calls ``register_ghost_battle_routes`` and then, at the bottom of
the file, ``install_ownership_guard``. Both are load-bearing for what follows:
the guard is what turns a body field named ``player_id`` into a session check.
A throwaway app that skipped it would report these routes as anonymous-readable
when the shipped app does not, so this file boots ``server.main`` and measures
the wiring a deployment gets.

HOW THE FIXTURE BUILDS A REAL GHOST
------------------------------------
``/ghost/record`` will not invent a ghost. It re-simulates the source battle
with the seed ``/pvp/match`` used and refuses (409) unless the turn log matches
the one already in the player's ``pvp_history``. So the fixture plays a real
``POST /pvp/match`` first and feeds back the stat line the server itself
assembled from the opponent's stored row. That is the honest path: the test
never hand-writes the history entry the route checks against.

NEGATIVE CONTROLS, AND WHY THEY ARE NOT DECORATIVE
--------------------------------------------------
A test that cannot go red is a comment. Each control below sabotages ONE rule
in a CHILD INTERPRETER (never the shipping tree, never a copied directory --
nothing is written to disk at all) and asserts the sabotaged run behaves the
way the un-sabotaged run does NOT. Every control was run and observed to bite
before this file was written:

  * revert the per-day ledger  -> a SECOND FRESH KEY pays again (500 -> 550)
  * revert the receipt table   -> a replayed key reports replayed=False
  * revert ghost_seed to hash()-> seeds differ per PYTHONHASHSEED

The last one matters because ``ghost_battle.ghost_seed``'s docstring claims
sha256 was chosen precisely so a ghost replays identically in another process.
Claiming that in a comment is not evidence; the control is.

WHAT THIS FILE DELIBERATELY DOES NOT CLAIM
------------------------------------------
It does not claim the PostgreSQL half. The in-memory store cannot show that a
counter bumped inside a transaction reaches the database, and the module's own
docstring says so. Every durability claim here is scoped to the in-memory lane
the harness actually runs.

Run
---
    python -m pytest server/tests/test_ghost_battle_contract.py -v

The suite runs on the in-memory store (no WC_PG_* variables set). If those are
set in your shell the harness unsets them for the store, so this file measures
the same lane every time.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import ghost_battle as gb  # noqa: E402


#: The five paths, exactly as the module registers them. A route added later
#: fails this on purpose: the contract is the route table, not a sample of it.
GHOST_PATHS = ("/ghost/record", "/ghost/list", "/ghost/fight",
               "/ghost/fights", "/ghost/preview")

#: The reply keys each route is contracted to carry. Asserted as an EXACT set:
#: a renamed or dropped key is the break this file exists to catch, and an
#: exact set is what makes "the response lost a field" a red test rather than a
#: field someone stopped reading.
RECORD_KEYS = {"ok", "ghost_id", "replayed", "source_winner", "source_turns", "ghosts"}
LIST_KEYS = {"ok", "ghosts", "total"}
LIST_ITEM_KEYS = {"ghost_id", "owner_id", "owner_name", "owner_rating",
                  "source_winner", "source_turns", "challenged_by",
                  "defeated_by", "recorded_at", "rating_diff"}
FIGHT_KEYS = {"ok", "replayed", "ghost_id", "owner_id", "owner_name", "winner",
              "turns", "reward", "already_paid_today", "challenger_hp_remaining",
              "ghost_hp_remaining", "log", "source_winner", "defeated_by"}
FIGHT_SCROLL_KEYS = {"ok", "fights"}
FIGHT_SCROLL_ITEM_KEYS = {"ghost_id", "owner_id", "owner_name", "winner",
                          "turns", "reward", "fought_at"}
PREVIEW_KEYS = {"ok", "winner", "turns", "log", "source_winner"}

#: The reward figures, cross-checked against the module constants rather than
#: retyped, so a deliberate rebalance does not break the test and an accidental
#: change cannot hide behind a stale literal in the test.
WIN_REWARD = gb.GHOST_WIN_REWARD_STONES
LOSS_REWARD = gb.GHOST_LOSS_REWARD_STONES


def _session(player_id: str) -> Dict[str, str]:
    """Mint a session the app's own auth module recognises.

    Imported BARE (``auth``, not ``server.auth``) because the app reads
    ``sys.modules['auth']``; importing the other identity mints a token into a
    store nothing reads and every call 401s for a reason unrelated to ghosts.
    """
    import auth as auth_module

    return {"Authorization": "Bearer %s" % auth_module.issue_session(player_id)}


def _keyed(session_headers: Dict[str, str], key: str) -> Dict[str, str]:
    h = dict(session_headers)
    h["Idempotency-Key"] = key
    return h


def _fresh(prefix: str) -> str:
    return "%s-%s" % (prefix, os.urandom(6).hex())


def _stat_line(player_id: str) -> Dict[str, Any]:
    """The stat line ``/pvp/match`` assembles from this player's stored row.

    Read through the SERVER'S OWN normaliser (``main._pvp_enemy_stat_line``)
    rather than rebuilt here. An earlier version of this file spelled the eight
    keys out by hand and got ``spirit_root`` wrong for a fire-spirit player,
    which made ``/ghost/record`` answer 409 -- a failure that looks exactly
    like a broken route and is not. Delegating removes the second copy of the
    rule, so a future change to the stat line cannot desynchronise this
    fixture from the route it is feeding.

    Accepts BOTH defence spellings upstream, because a stored player row spells
    it ``def_`` while the simulator hard-indexes ``def``.
    """
    import server.main as main_module

    return dict(main_module._pvp_enemy_stat_line(
        type("Req", (), {"opponent_id": player_id})()))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def main_module():
    """``server.main``, imported once.

    Deliberately the WHOLE app rather than a purpose-built FastAPI instance:
    ``install_ownership_guard`` runs at the bottom of ``main.py`` and rewrites
    routes in place, so the auth behaviour these routes actually ship with is
    only visible on the real object.

    The database settings are saved and PUT BACK. ``server/conftest.py``
    documents why the deletion itself is the hazard: a developer who runs the
    real PostgreSQL suite on purpose must not have their settings silently
    dropped by an unrelated test file, so this fixture restores every value it
    touched instead of leaving the environment rewritten for the next test.
    """
    saved = {name: os.environ.get(name)
             for name in ("WC_PG_HOST", "WC_PG_PORT", "WC_PG_DB",
                          "WC_PG_USER", "WC_PG_PASSWORD", "DATABASE_URL")}
    for name in saved:
        os.environ.pop(name, None)
    try:
        import server.main as main_mod

        return main_mod
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="module")
def client(main_module):
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture
def pair(client, main_module):
    """Two fresh players, the stat line one of them fights with, and the
    stone balance the payout assertions measure against.

    Fresh per test on purpose: several tests below spend a per-day payout and
    fill a bounded scroll, and a shared player would make them order-dependent
    in exactly the way that hides a regression.

    The id is whatever the server minted -- ``POST /player/create`` mints its
    own and ignores the name, so the name is a uniqueness label, not the id.
    The starting stone balance is READ rather than hard-coded: a 500 typed into
    the test is a snapshot that goes red on an unrelated economy change and
    stays green if the reward silently doubles.
    """
    ids: Dict[str, str] = {}
    for label, root in (("owner", "fire"), ("foe", "water")):
        name = _fresh("ghost-contract-%s" % label)
        made = client.post("/player/create",
                           json={"name": name, "spirit_root": root})
        assert made.status_code == 200, made.text
        assert made.json()["name"] == name, (
            "the server did not create the player named %r: %s"
            % (name, made.text[:200]))
        ids[label] = made.json()["id"]

    return {
        "owner": ids["owner"],
        "foe": ids["foe"],
        "owner_headers": _session(ids["owner"]),
        "foe_headers": _session(ids["foe"]),
        "foe_stats": _stat_line(ids["foe"]),
        "foe_start_stones": _low_grade(main_module.players_db.get(ids["foe"])),
    }


@pytest.fixture
def recorded_ghost(client, pair):
    """A ghost that exists because a real PvP match was really played.

    Returns ``(ghost_id, opponent_stats)``. The route under test is the reason
    this works: ``/ghost/record`` re-simulates and 409s unless the log matches
    the stored history, so the only way to get a ghost is to have fought the
    battle honestly.
    """
    owner, foe = pair["owner"], pair["foe"]
    stats = pair["foe_stats"]

    fought = client.post("/pvp/match", headers=pair["owner_headers"],
                         json={"player_id": owner, "opponent_id": foe,
                               "opponent_rating": 1000, "enemy_stats": {}})
    assert fought.status_code == 200, fought.text

    rec = client.post("/ghost/record", headers=pair["owner_headers"],
                      json={"player_id": owner, "opponent_id": foe,
                            "opponent_rating": 1000, "opponent_stats": stats})
    assert rec.status_code == 200, (
        "the fixture could not record a ghost after a real PvP match: %s"
        % rec.text[:300])
    body = rec.json()
    assert set(body) == RECORD_KEYS, body
    assert body["replayed"] is False, body
    return body["ghost_id"], stats


# ---------------------------------------------------------------------------
# 1. THE ROUTE TABLE
# ---------------------------------------------------------------------------

def test_exactly_five_ghost_routes_are_mounted(client, main_module):
    """All five exist, are POST-only, and answer a signed-in caller with 200.

    The path list is compared as a SET against the live route table, so a sixth
    ghost route is a red test here rather than an unexamined surface.
    """
    from fastapi.routing import APIRoute

    mounted = {r.path: sorted(getattr(r, "methods", None) or [])
               for r in main_module.app.routes
               if isinstance(r, APIRoute) and r.path.startswith("/ghost")}
    assert set(mounted) == set(GHOST_PATHS), mounted

    body = {"player_id": _fresh("ghost-contract-routecheck")}
    for path in GHOST_PATHS:
        assert mounted[path] == ["POST"], (path, mounted[path])
        # A missing body is 422, never 404: the route is REACHABLE. Asserting
        # 422 rather than "some error" is what distinguishes a mounted route
        # from an absent one.
        r = client.post(path, headers=_session(body["player_id"]), json={})
        assert r.status_code == 422, (
            "%s answered %d to a signed-in caller with no body; expected 422, "
            "which means the route exists and validated the request"
            % (path, r.status_code))


def test_every_ghost_route_is_session_guarded(client, main_module):
    """Anonymous is 401 on all five, and another player's body claim is 403.

    Two assertions, and the second is the one that is easy to fake. A route can
    answer 401 to an anonymous caller purely from a shape check while still
    letting a signed-in player name somebody else; only the 403 proves the
    session is compared against the body.
    """
    from fastapi.routing import APIRoute

    owner, foe = pair_ids()
    victim = _fresh("ghost-contract-victim")

    for path in GHOST_PATHS:
        anon = client.post(path, json={"player_id": victim, "ghost_id": "ghost_x"})
        assert anon.status_code == 401, (
            "%s answered %d to an anonymous caller; it must be 401"
            % (path, anon.status_code))

    # Signed in as `owner`, naming `victim`. The ownership check compares the
    # body claim against the session and must refuse.
    crossed = client.post("/ghost/list", headers=_session(owner),
                          json={"player_id": victim})
    assert crossed.status_code == 403, (
        "a signed-in player named another player and got %d; the body claim "
        "must be compared against the session" % crossed.status_code)

    # And the dependant really carries an auth dependency, read from the route
    # object rather than inferred from the status code above. This is what
    # makes the 401/403 attributable to the guard and not to a stray check.
    import ownership_guard as og

    guarded = [r for r in main_module.app.routes
               if isinstance(r, APIRoute) and r.path in GHOST_PATHS
               and og.has_auth_dependency(r.dependant)]
    assert len(guarded) == len(GHOST_PATHS), (
        "only %d of %d ghost routes carry an auth dependency on the live app"
        % (len(guarded), len(GHOST_PATHS)))


def pair_ids() -> Tuple[str, str]:
    """Two ids used only for the auth test's 403 assertion.

    They need not exist in the store: the guard refuses on the CLAIM before
    any handler runs, so a 403 here proves the comparison happened and a
    handler's own 404 is never reached.
    """
    return _fresh("ghost-contract-session"), _fresh("ghost-contract-other")


# ---------------------------------------------------------------------------
# 2. /ghost/record -- a ghost is earned, never invented
# ---------------------------------------------------------------------------

def test_record_returns_the_documented_keys_and_a_repeat_is_a_replay(client, pair):
    """Capture once is ``replayed: False``; capture again is the SAME ghost.

    The same-ghost-id half is the point. A second capture returning a fresh id
    would mean the owner accumulates one ghost per call for one battle, and
    the scroll bound would be doing the work the route should.
    """
    owner, foe, stats = pair["owner"], pair["foe"], pair["foe_stats"]
    client.post("/pvp/match", headers=pair["owner_headers"],
                json={"player_id": owner, "opponent_id": foe,
                      "opponent_rating": 1000, "enemy_stats": {}})

    first = client.post("/ghost/record", headers=pair["owner_headers"],
                        json={"player_id": owner, "opponent_id": foe,
                              "opponent_rating": 1000, "opponent_stats": stats})
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == RECORD_KEYS, body
    assert body["ok"] is True
    assert body["ghost_id"].startswith("ghost_"), body
    assert body["source_turns"] >= 1, body
    assert body["ghosts"] == 1, body

    second = client.post("/ghost/record", headers=pair["owner_headers"],
                         json={"player_id": owner, "opponent_id": foe,
                               "opponent_rating": 1000, "opponent_stats": stats})
    assert second.status_code == 200, second.text
    again = second.json()
    assert again["ghost_id"] == body["ghost_id"], (
        "recording the same battle twice produced two ghosts: %s then %s"
        % (body["ghost_id"], again["ghost_id"]))
    assert again["replayed"] is True, again
    assert again["ghosts"] == 1, (
        "a replayed record grew the owner's ghost scroll to %s entries"
        % again["ghosts"])


def test_record_refuses_a_stat_line_that_does_not_reproduce_the_battle(client, pair,
                                                                       main_module):
    """An inflated enemy stat line is a 409, and no ghost is created.

    This is the anti-forgery claim in the module's own docstring: ``opponent_stats``
    is accepted as an input and then PROVEN by the log comparison. Inflating
    atk is the cheapest forgery a caller could attempt, so it is the one tested.

    The refusal is checked against the OWNER's stored row, not just the status
    code: a route that answers 409 and writes anyway would pass a reply-only
    check, which is the exact shape of bug this file exists to catch.
    """
    owner, foe, stats = pair["owner"], pair["foe"], pair["foe_stats"]
    client.post("/pvp/match", headers=pair["owner_headers"],
                json={"player_id": owner, "opponent_id": foe,
                      "opponent_rating": 1000, "enemy_stats": {}})

    forged = dict(stats)
    forged["atk"] = int(forged["atk"]) * 50

    r = client.post("/ghost/record", headers=pair["owner_headers"],
                    json={"player_id": owner, "opponent_id": foe,
                          "opponent_rating": 1000, "opponent_stats": forged})
    assert r.status_code == 409, (
        "a stat line that does not reproduce the recorded battle was answered "
        "%d; it must be 409, or a caller can invent a ghost from a fight that "
        "never happened" % r.status_code)
    assert "reproduce" in r.json()["detail"], r.json()

    ghosts = (main_module.players_db.get(owner) or {}).get("ghosts") or []
    assert ghosts == [], (
        "the forged capture was refused with 409 but still stored %d ghost(s): "
        "%s" % (len(ghosts), [g.get("ghost_id") for g in ghosts]))


def test_record_rolls_the_scroll_at_the_declared_bound(client, pair, main_module):
    """The owner's ghost scroll never exceeds ``GHOSTS_PER_PLAYER``.

    Asserted against the module constant rather than a typed-in 20, so a
    deliberate change to the bound moves the test with it and only a
    REGRESSION -- the bound being ignored -- goes red.
    """
    owner = pair["owner"]
    row = dict(main_module.players_db.get(owner) or {})
    ghosts = list(row.get("ghosts") or [])
    assert len(ghosts) <= gb.GHOSTS_PER_PLAYER, (
        "the owner's scroll holds %d ghosts, past the declared bound of %d"
        % (len(ghosts), gb.GHOSTS_PER_PLAYER))
    # The scroll is newest-first: a newly recorded ghost sits at the head.
    if ghosts:
        assert ghosts[0].get("recorded_at", 0) >= max(
            g.get("recorded_at", 0) for g in ghosts[1:]), ghosts[0].get("recorded_at")


def test_record_refusals_for_missing_history_and_missing_stats(client, pair):
    """The three refusals, each with its own status code.

    Kept separate from the 409 above because they fail for unrelated reasons
    and lumping them would report one defect as three.
    """
    owner, foe, stats = pair["owner"], pair["foe"], pair["foe_stats"]

    # No battle against this opponent at all.
    no_history = client.post("/ghost/record", headers=pair["owner_headers"],
                             json={"player_id": owner,
                                   "opponent_id": _fresh("ghost-contract-stranger"),
                                   "opponent_rating": 1000,
                                   "opponent_stats": stats})
    assert no_history.status_code == 404, no_history.text

    # A real battle, but a stat line missing the keys the simulator hard-indexes.
    client.post("/pvp/match", headers=pair["owner_headers"],
                json={"player_id": owner, "opponent_id": foe,
                      "opponent_rating": 1000, "enemy_stats": {}})
    sparse = client.post("/ghost/record", headers=pair["owner_headers"],
                         json={"player_id": owner, "opponent_id": foe,
                               "opponent_rating": 1000,
                               "opponent_stats": {"hp": 1000}})
    assert sparse.status_code == 400, sparse.text
    assert "hp" in sparse.json()["detail"], sparse.json()

    # A player who does not exist.
    ghost_player = _fresh("ghost-contract-nobody")
    missing = client.post("/ghost/record", headers=_session(ghost_player),
                          json={"player_id": ghost_player, "opponent_id": foe,
                                "opponent_rating": 1000,
                                "opponent_stats": stats})
    assert missing.status_code == 404, missing.text


# ---------------------------------------------------------------------------
# 3. /ghost/list and /ghost/fights -- the two read shapes
# ---------------------------------------------------------------------------

def test_list_excludes_your_own_ghosts_and_sorts_by_rating_gap(client, pair,
                                                               recorded_ghost):
    """A player never fights their own ghost, and the list is nearest-rated first.

    The exclusion is the important one: ``/ghost/fight`` refuses a self-fight
    with 400, so a list that included your own ghosts would be offering the
    caller something the next screen rejects.

    Every count here is RELATIVE to this player's own ghost, never an absolute
    number. ``/ghost/list`` reads every player's ghosts in the shared store, so
    a hard-coded "one ghost" passes while this file runs alone and fails the
    moment an earlier test records a second one -- a test whose result depends
    on the tests before it. That was measured, not assumed: recording two
    ghosts and asking a brand-new viewer makes ``len(ghosts) <= 1`` false.
    """
    ghost_id, _stats = recorded_ghost
    owner, foe = pair["owner"], pair["foe"]

    seen_by_foe = client.post("/ghost/list", headers=pair["foe_headers"],
                              json={"player_id": foe}).json()
    assert set(seen_by_foe) == LIST_KEYS, seen_by_foe
    mine = [g for g in seen_by_foe["ghosts"] if g["ghost_id"] == ghost_id]
    assert len(mine) == 1, (
        "the ghost this test just recorded is not in the opponent's list: %s"
        % [g["ghost_id"] for g in seen_by_foe["ghosts"]])
    assert mine[0]["owner_id"] == owner, mine[0]
    assert set(mine[0]) == LIST_ITEM_KEYS, mine[0]
    # `total` counts everything found, which is at least this one ghost.
    assert seen_by_foe["total"] >= 1, seen_by_foe

    seen_by_owner = client.post("/ghost/list", headers=pair["owner_headers"],
                                json={"player_id": owner}).json()
    assert ghost_id not in {g["ghost_id"] for g in seen_by_owner["ghosts"]}, (
        "a player saw their OWN ghost in their own fightable list: %s"
        % [g["ghost_id"] for g in seen_by_owner["ghosts"]])
    assert all(g["owner_id"] != owner for g in seen_by_owner["ghosts"]), (
        "the owner's own ghosts leaked into their own list")

    # Nearest rating first. Asserted as a RELATIONSHIP between neighbours, so a
    # rating change in the game does not fail this.
    gaps = [abs(g["rating_diff"]) for g in seen_by_foe["ghosts"]]
    assert gaps == sorted(gaps), gaps


def test_list_limit_caps_the_page_without_changing_the_total(client, pair,
                                                             recorded_ghost):
    """``limit`` shortens the page; ``total`` still counts everything found."""
    _ghost_id, _stats = recorded_ghost
    foe = pair["foe"]

    full = client.post("/ghost/list", headers=pair["foe_headers"],
                       json={"player_id": foe, "limit": 50}).json()
    assert len(full["ghosts"]) >= 1, full

    one = client.post("/ghost/list", headers=pair["foe_headers"],
                      json={"player_id": foe, "limit": 1}).json()
    assert len(one["ghosts"]) == 1, one
    # The short page must be the FIRST entry of the full page -- same ghost,
    # same order. Asserting it is *my* ghost would be wrong: the list is
    # nearest-rated-first, so with several ghosts in the world another owner's
    # can legitimately outrank mine.
    assert one["ghosts"][0] == full["ghosts"][0], (
        "limiting the page changed which ghost comes first: %s vs %s"
        % (one["ghosts"][0], full["ghosts"][0]))
    # The count of everything found is unchanged by the page size. If `total`
    # were computed after slicing, the two would differ and a client would show
    # "1 of 1" on a world holding many more.
    assert one["total"] == full["total"], (
        "limiting the page changed the reported total: %s vs %s"
        % (one["total"], full["total"]))


def test_fights_is_the_challengers_own_scroll_newest_first(client, pair,
                                                           recorded_ghost):
    """``/ghost/fights`` returns only THIS challenger's fights, newest first.

    The ordering matters because the scroll is bounded to the newest
    ``GHOST_FIGHTS_PER_PLAYER`` entries; a client rendering it in stored order
    would show the fight the player just had as the one that fell off.
    """
    _ghost_id, _stats = recorded_ghost
    foe = pair["foe"]

    empty = client.post("/ghost/fights", headers=pair["foe_headers"],
                        json={"player_id": foe})
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"ok": True, "fights": []}, empty.json()

    client.post("/ghost/fight", headers=_keyed(pair["foe_headers"], "scroll-key"),
                json={"player_id": foe,
                      "ghost_id": recorded_ghost[0]})
    after = client.post("/ghost/fights", headers=pair["foe_headers"],
                        json={"player_id": foe}).json()
    assert set(after) == FIGHT_SCROLL_KEYS, after
    assert len(after["fights"]) == 1, after
    entry = after["fights"][0]
    assert set(entry) == FIGHT_SCROLL_ITEM_KEYS, entry
    assert entry["ghost_id"] == recorded_ghost[0], entry
    assert entry["reward"] in (WIN_REWARD, LOSS_REWARD), entry
    assert entry["owner_id"] == pair["owner"], entry

    # Another player's scroll must not show this fight.
    elsewhere = client.post("/ghost/fights", headers=pair["owner_headers"],
                            json={"player_id": pair["owner"]}).json()
    assert elsewhere["fights"] == [], elsewhere


# ---------------------------------------------------------------------------
# 4. /ghost/preview -- reads the past, writes nothing
# ---------------------------------------------------------------------------

def test_preview_is_deterministic_and_frees_no_payout(client, pair,
                                                      recorded_ghost):
    """Two previews agree exactly, and neither moves the challenger's stones.

    Determinism is the module's reason for existing (re-derive, never store a
    log), so it is asserted as a full-body equality rather than a matching
    winner. And because preview returns another player's frozen stat line and
    a turn log, the "writes nothing" half is asserted against the store.
    """
    ghost_id, _stats = recorded_ghost
    foe, headers = pair["foe"], pair["foe_headers"]

    first = client.post("/ghost/preview", headers=headers,
                        json={"player_id": foe, "ghost_id": ghost_id})
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == PREVIEW_KEYS, body
    assert body["ok"] is True
    assert body["winner"] in ("player", "enemy"), body
    assert body["turns"] >= 1, body
    assert len(body["log"]) >= 1, body

    second = client.post("/ghost/preview", headers=headers,
                         json={"player_id": foe, "ghost_id": ghost_id}).json()
    assert second == body, (
        "two previews of one ghost disagreed; the ghost is re-derived from a "
        "seed, so it must be identical every time")

    # Nothing was paid. Read from the store, since a route that paid nothing
    # and said nothing about it would pass a reply-only check. Measured
    # against the balance this player started with, not a typed-in number.
    import server.main as main_module
    stones_now = _low_grade(main_module.players_db.get(foe))
    assert stones_now == pair["foe_start_stones"], (
        "preview moved the challenger's spirit stones from %d to %d; it must "
        "pay nothing" % (pair["foe_start_stones"], stones_now))

    missing = client.post("/ghost/preview", headers=headers,
                          json={"player_id": foe, "ghost_id": "ghost_not_real"})
    assert missing.status_code == 404, missing.text


# ---------------------------------------------------------------------------
# 5. /ghost/fight -- the money route. One paying fight per ghost per UTC day.
# ---------------------------------------------------------------------------

def test_fight_pays_once_per_ghost_per_day(client, pair, recorded_ghost):
    """THE contract: a second fight pays nothing and says so.

    Three calls, three different keys, one payout. The second and third use
    FRESH keys on purpose: the idempotency receipt only catches a caller that
    REUSES a key, so a farm that mints a new key per call lands here. If only
    the receipt were tested, this file would prove nothing about the bound the
    economy actually needs.
    """
    ghost_id, _stats = recorded_ghost
    foe, headers = pair["foe"], pair["foe_headers"]

    first = client.post("/ghost/fight", headers=_keyed(headers, "day-key-1"),
                        json={"player_id": foe, "ghost_id": ghost_id})
    assert first.status_code == 200, first.text
    a = first.json()
    assert set(a) == FIGHT_KEYS, a
    assert a["ok"] is True
    assert a["replayed"] is False, a
    assert a["already_paid_today"] is False, a
    assert a["reward"] in (WIN_REWARD, LOSS_REWARD), a
    # The payout must match the RESULT, not merely be non-zero: a caller that
    # lost should not collect the win figure.
    assert a["reward"] == (WIN_REWARD if a["winner"] == "player" else LOSS_REWARD), a
    assert len(a["log"]) >= 1, a
    assert a["ghost_id"] == ghost_id, a

    import server.main as main_module
    start = pair["foe_start_stones"]
    after_first = _low_grade(main_module.players_db.get(foe))
    assert after_first == start + a["reward"], (
        "the store shows %d stones after a %d-stone fight from a %d start"
        % (after_first, a["reward"], start))

    # A FRESH key, same ghost, same day. The battle still runs and still
    # answers; only the money is spent. A 4xx here would turn a throttle into
    # a crash loop for a client that re-reads the screen.
    second = client.post("/ghost/fight", headers=_keyed(headers, "day-key-2"),
                         json={"player_id": foe, "ghost_id": ghost_id})
    assert second.status_code == 200, (
        "a repeat fight on the same day answered %d; the practice loop must "
        "survive the payout being spent" % second.status_code)
    b = second.json()
    assert b["reward"] == 0, (
        "a second fight against one ghost today paid %r; the per-day bound is "
        "the whole bound" % b["reward"])
    assert b["already_paid_today"] is True, b
    assert b["replayed"] is False, (
        "a fresh key is a NEW fight, not a replay of the receipt: %r" % b)
    assert len(b["log"]) >= 1, "the repeat fight still has to return its log"
    assert _low_grade(main_module.players_db.get(foe)) == start + a["reward"]

    third = client.post("/ghost/fight", headers=_keyed(headers, "day-key-3"),
                        json={"player_id": foe, "ghost_id": ghost_id})
    assert third.status_code == 200, third.text
    assert third.json()["reward"] == 0, third.json()
    assert _low_grade(main_module.players_db.get(foe)) == start + a["reward"], (
        "three fights against one ghost today paid more than the first")

    # The scroll records all THREE, so the fight history is not throttled away
    # with the payout -- only the money is spent.
    scroll = client.post("/ghost/fights", headers=pair["foe_headers"],
                         json={"player_id": foe}).json()["fights"]
    assert len(scroll) == 3, scroll
    assert scroll[0]["fought_at"] >= scroll[-1]["fought_at"], (
        "the fight scroll is not newest-first")


def test_a_replayed_idempotency_key_returns_the_stored_reply(client, pair,
                                                            recorded_ghost):
    """One key, twice: the second call replays and pays nothing again.

    The reply must be the STORED one -- same reward, same winner, same turns --
    with ``replayed`` flipped to True so a client can tell the difference.
    """
    ghost_id, _stats = recorded_ghost
    foe, headers = pair["foe"], pair["foe_headers"]
    keyed = _keyed(headers, "replay-key")

    first = client.post("/ghost/fight", headers=keyed,
                        json={"player_id": foe, "ghost_id": ghost_id}).json()
    import server.main as main_module
    after_first = _low_grade(main_module.players_db.get(foe))

    second_resp = client.post("/ghost/fight", headers=keyed,
                              json={"player_id": foe, "ghost_id": ghost_id})
    assert second_resp.status_code == 200, second_resp.text
    second = second_resp.json()

    assert second["replayed"] is True, (
        "the second call with one idempotency key reported replayed=%r"
        % second["replayed"])
    for field in ("ghost_id", "winner", "turns", "reward", "log",
                  "challenger_hp_remaining", "ghost_hp_remaining"):
        assert second[field] == first[field], (
            "the replayed reply changed %r: %r then %r"
            % (field, first[field], second[field]))
    assert _low_grade(main_module.players_db.get(foe)) == after_first, (
        "a replayed idempotency key paid a second time")


def test_one_idempotency_key_against_two_ghosts_is_a_conflict(client, pair,
                                                              recorded_ghost):
    """A key already spent on ghost A, replayed against ghost B, is 409.

    Without this the receipt would serve ghost A's stored battle to a call for
    ghost B -- a silent wrong answer, which is worse than a refusal.
    """
    ghost_a, _ = recorded_ghost
    owner, foe = pair["owner"], pair["foe"]

    # A second, genuinely different ghost for the same owner.
    second_foe = _fresh("ghost-contract-foe2")
    made = client.post("/player/create",
                       json={"name": second_foe, "spirit_root": "metal"})
    assert made.status_code == 200, made.text
    import server.main as main_module
    stats_b = _stat_line(second_foe)
    client.post("/pvp/match", headers=pair["owner_headers"],
                json={"player_id": owner, "opponent_id": second_foe,
                      "opponent_rating": 1000, "enemy_stats": {}})
    rec = client.post("/ghost/record", headers=pair["owner_headers"],
                      json={"player_id": owner, "opponent_id": second_foe,
                            "opponent_rating": 1000, "opponent_stats": stats_b})
    assert rec.status_code == 200, rec.text
    ghost_b = rec.json()["ghost_id"]
    assert ghost_b != ghost_a, "the two ghosts should differ"

    shared = _keyed(pair["foe_headers"], "shared-key")
    first = client.post("/ghost/fight", headers=shared,
                        json={"player_id": foe, "ghost_id": ghost_a})
    assert first.status_code == 200, first.text

    conflict = client.post("/ghost/fight", headers=shared,
                           json={"player_id": foe, "ghost_id": ghost_b})
    assert conflict.status_code == 409, (
        "one idempotency key reused against a DIFFERENT ghost was answered %d; "
        "it must be 409, or the receipt serves the wrong battle"
        % conflict.status_code)


def test_fight_refusals(client, pair, recorded_ghost):
    """The three refusals, each with the status that actually ships."""
    ghost_id, _ = recorded_ghost
    owner, foe = pair["owner"], pair["foe"]

    # Your own ghost: 400, not 404. It exists; you may not fight it.
    own = client.post("/ghost/fight", headers=pair["owner_headers"],
                      json={"player_id": owner, "ghost_id": ghost_id})
    assert own.status_code == 400, (
        "fighting your own ghost answered %d; the module specifies 400"
        % own.status_code)

    # An unknown GHOST for a real player is still the handler's own 404. The
    # player exists, so the liveness check passes and this branch is reachable.
    unknown = client.post("/ghost/fight", headers=_keyed(pair["foe_headers"], "k-404"),
                          json={"player_id": foe, "ghost_id": "ghost_not_real"})
    assert unknown.status_code == 404, unknown.text

    # An unknown PLAYER is refused one layer higher: 401, not 404.
    # MEASURED 2026-10-05 against the real app. This arm used to read 404 from
    # the handler's own "Player not found" branch, reached by minting the
    # session FOR the ghost. It no longer reaches the handler --
    # ``auth.authenticate`` confirms the session's player still exists and
    # revokes the token when it does not. That is the deleted-player defense:
    # a token for an account that was never created must stop working, and the
    # status must not reveal whether an id is live. The sibling file
    # ``test_ghost_battle_receipts_contract.py`` already pins the same 401.
    nobody = _fresh("ghost-contract-nobody2")
    missing = client.post("/ghost/fight",
                          headers=_keyed(_session(nobody), "k-404b"),
                          json={"player_id": nobody, "ghost_id": ghost_id})
    assert missing.status_code == 401, missing.text


def test_the_daily_ledger_rolls_over_on_the_utc_day_boundary(pair):
    """``_day_index`` moves with the clock, and yesterday's ids do not carry.

    A ledger that carried its ids across midnight would make a player wait a
    whole extra day; one that never rolled would pay forever. Asserted as a
    relationship between two instants a day apart, so the test never freezes a
    date and never depends on today's calendar.
    """
    now = 1790830740.0                      # 2026-10-01 04:59:00 UTC
    tomorrow = now + 86400
    assert gb._day_index(tomorrow) - gb._day_index(now) == 1

    player: Dict[str, Any] = {}
    paid_today = gb._daily_paid(player, now)
    paid_today.append("ghost_a")
    assert "ghost_a" in gb._daily_paid(player, now), (
        "an id paid today vanished within the same day")

    tomorrow_paid = gb._daily_paid(player, tomorrow)
    assert "ghost_a" not in tomorrow_paid, (
        "yesterday's paid ghost id carried into today, so the player waits an "
        "extra day")
    assert tomorrow is not None


def _low_grade(row: Optional[Dict[str, Any]]) -> int:
    """Low-grade spirit stones, tolerating the scalar legacy shape."""
    stones = (row or {}).get("spirit_stones", 0)
    if isinstance(stones, dict):
        return int(stones.get("low_grade", 0))
    return int(stones or 0)


# ---------------------------------------------------------------------------
# 6. DETERMINISM OF THE SEEDS -- the module's durability claim, actually run
# ---------------------------------------------------------------------------

_SEED_PROBE = (
    "import json, sys\n"
    "sys.path.insert(0, %(server)r)\n"
    "import ghost_battle as g\n"
    "print(json.dumps({\n"
    "    'ghost': g.ghost_seed('owner-x', 'opponent-y:1000:1016'),\n"
    "    'source': g.source_match_seed('owner-x', 'opponent-y'),\n"
    "    'source_with_rating': g.source_match_seed('owner-x', 'opponent-y', 4321),\n"
    "}))\n"
) % {"server": str(SERVER_DIR)}


def _run_seed_probe(hashseed: str) -> Dict[str, Any]:
    """Run the seed script in a child interpreter with this PYTHONHASHSEED."""
    return _run_script(_SEED_PROBE, hashseed=hashseed)


@pytest.mark.parametrize("hashseed", ["0", "1", "12345"])
def test_seeds_are_stable_across_processes_under_different_hashseeds(hashseed):
    """The same ids and log key give the same seed in any process.

    ``ghost_seed``'s docstring says sha256 was chosen because ``hash()`` is
    salted per process. This runs it: three child interpreters, three
    different ``PYTHONHASHSEED`` values, one answer.
    """
    got = _run_seed_probe(hashseed)
    assert got == {"ghost": gb.ghost_seed("owner-x", "opponent-y:1000:1016"),
                   "source": gb.source_match_seed("owner-x", "opponent-y"),
                   # The rating argument is accepted and ignored on purpose,
                   # so an old call site keeps working. Pinned so nobody
                   # "fixes" it into the seed and breaks every replay.
                   "source_with_rating": gb.source_match_seed("owner-x",
                                                              "opponent-y", 4321)}, \
        ("the seeds differ under PYTHONHASHSEED=%s; a ghost seeded with hash() "
         "would replay differently in another process" % hashseed)


def test_source_seed_ignores_the_rating_argument():
    """Same pair, different stated rating, same seed -- stated as a relation."""
    a = gb.source_match_seed("player-1", "player-2")
    b = gb.source_match_seed("player-1", "player-2", 4000)
    c = gb.source_match_seed("player-2", "player-1")
    assert a == b, "the rating argument changed the seed; its docstring says it is ignored"
    assert a != c, "swapping the two players produced the same seed"


# ---------------------------------------------------------------------------
# 7. NEGATIVE CONTROLS -- each reverts ONE rule, in a child interpreter
# ---------------------------------------------------------------------------

# The control app is built in the child, from the same five-line fixture the
# tests above use, so a control that fails to reach the route is distinguishable
# from a control that reached it and found nothing wrong.
_CONTROL_PRELUDE = """
import json, sys
sys.path.insert(0, %(server)r)
import ghost_battle as g
from fastapi import FastAPI
from fastapi.testclient import TestClient
from player_store import PlayerStore
import combat_config, auth
%(sabotage)s
store = PlayerStore()
OWNER, FOE = 'ctl-owner', 'ctl-foe'
store.persist(OWNER, {'id': OWNER, 'name': OWNER, 'spirit_root': 'fire', 'pvp_rating': 1000})
store.persist(FOE, {'id': FOE, 'name': FOE, 'spirit_root': 'water', 'pvp_rating': 1000,
                    'spirit_stones': {'low_grade': 500},
                    'atk': 100, 'def_': 50, 'hp': 1000, 'mp': 500, 'spd': 100})
STATS = {'realm': 'mortal', 'stage': 'early', 'element': 'water',
         'atk': 100, 'def': 50, 'hp': 1000, 'mp': 500, 'spd': 100}
# Play the source battle honestly, so /ghost/record's re-simulation check passes.
_seed = g.source_match_seed(OWNER, FOE)
_res = combat_config.calculate_auto_battle(player_stats=g.freeze_stats(store.get(OWNER)),
                                           enemy_stats=STATS, max_turns=50,
                                           seed=_seed %% (2**32))
_row = store.get(OWNER)
_row['pvp_history'] = [{'opponent_id': FOE, 'winner': _res['winner'],
                        'new_rating': 1000, 'turns': _res['turns'], 'log': _res['log']}]
store.persist(OWNER, _row)
app = FastAPI()
g.register_ghost_battle_routes(app, lambda: store)
with TestClient(app) as c:
    _ho = {'Authorization': 'Bearer ' + auth.issue_session(OWNER)}
    _hf = {'Authorization': 'Bearer ' + auth.issue_session(FOE)}
    _gid = c.post('/ghost/record', headers=_ho, json={'player_id': OWNER,
                  'opponent_id': FOE, 'opponent_rating': 1000,
                  'opponent_stats': STATS}).json()['ghost_id']
"""

def _run_script(script: str, hashseed: str = "") -> Dict[str, Any]:
    """Run a probe script in a child interpreter and read its JSON result line.

    Two things this guarantees that a bare ``json.loads(stdout)`` would not:
    a crashed probe is a loud assertion failure rather than a parse error on
    empty output, and the child's own log lines cannot be mistaken for the
    result. Nothing is written to disk -- the child imports the shipping
    module and monkeypatches it in memory only.
    """
    env = dict(os.environ)
    if hashseed:
        env["PYTHONHASHSEED"] = hashseed
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, env=env, timeout=240)
    assert proc.returncode == 0, (
        "the probe script crashed, so it measured nothing: %s"
        % proc.stderr.strip()[-600:])
    lines = [ln for ln in proc.stdout.strip().splitlines()
             if ln.strip().startswith("{")]
    assert lines, ("the probe printed no JSON result line: %r"
                   % proc.stdout[-300:])
    return json.loads(lines[-1])


def test_control_reverting_the_daily_ledger_pays_twice():
    """Sabotage the per-day ledger; the second FRESH key must pay again.

    This is the control for ``test_fight_pays_once_per_ghost_per_day``. It is
    run on a CHILD interpreter with a monkeypatched ``_daily_paid``, so the
    shipping tree is never edited -- the module this file measures is read-only
    from here.
    """
    script = (_CONTROL_PRELUDE % {"server": str(SERVER_DIR),
                                  "sabotage": "g._daily_paid = lambda player, now: []   # SABOTAGE: forgets every payout"}) + """
    _h = dict(_hf); _h['Idempotency-Key'] = 'ctl-1'
    _a = c.post('/ghost/fight', headers=_h, json={'player_id': FOE, 'ghost_id': _gid}).json()
    _h2 = dict(_hf); _h2['Idempotency-Key'] = 'ctl-2'
    _b = c.post('/ghost/fight', headers=_h2, json={'player_id': FOE, 'ghost_id': _gid}).json()
print(json.dumps({'first_reward': _a['reward'], 'second_fresh_key_reward': _b['reward'],
                  'already_paid_today': _b['already_paid_today'],
                  'stones': store.get(FOE)['spirit_stones']['low_grade']}))
"""
    out = _run_script(script)

    # The sabotaged run pays twice, which is exactly what the shipping tree
    # refuses. If this ever stopped happening the daily-ledger test would be
    # unfalsifiable and its green would mean nothing.
    assert out["first_reward"] > 0, out
    assert out["second_fresh_key_reward"] > 0, (
        "reverting the per-day ledger did NOT pay a second time, so the "
        "daily-ledger test cannot fail and proves nothing")
    assert out["already_paid_today"] is False, out
    assert out["stones"] == 500 + out["first_reward"] + out["second_fresh_key_reward"], out


def test_control_reverting_the_receipt_table_breaks_replay():
    """Sabotage the receipt table; a replayed key must report ``replayed=False``.

    This is the control for ``test_a_replayed_idempotency_key_returns_the_stored_reply``.
    """
    script = (_CONTROL_PRELUDE % {
        "server": str(SERVER_DIR),
        "sabotage": "g._receipts = lambda player: {}   # SABOTAGE: nothing is ever remembered"}) + """
    _h = dict(_hf); _h['Idempotency-Key'] = 'ctl-same'
    _a = c.post('/ghost/fight', headers=_h, json={'player_id': FOE, 'ghost_id': _gid}).json()
    _b = c.post('/ghost/fight', headers=_h, json={'player_id': FOE, 'ghost_id': _gid}).json()
print(json.dumps({'first_replayed': _a['replayed'], 'second_replayed': _b['replayed']}))
"""
    out = _run_script(script)

    assert out["second_replayed"] is False, (
        "reverting the receipt table did NOT break the replay flag, so the "
        "idempotency test cannot fail and proves nothing")


def test_control_reverting_the_seed_to_hash_breaks_cross_process_determinism():
    """Sabotage ``ghost_seed`` to ``hash()``; seeds must then differ per process.

    This is the control for the ``PYTHONHASHSEED`` tests above, and it is the
    one that matters most: the module's docstring justifies sha256 by saying
    ``hash()`` is salted per process. A comment is not evidence. Here the
    replacement is measured in three child interpreters and MUST produce
    different answers -- which is what makes the real implementation's
    stability a real property rather than a lucky accident.
    """
    script = ("import json, sys\n"
              "sys.path.insert(0, %r)\n" % str(SERVER_DIR)
              + "import ghost_battle as g\n"
              + "g.ghost_seed = lambda owner_id, log_key: abs(hash((owner_id, log_key))) % (2**32)\n"
              + "print(json.dumps({'ghost': g.ghost_seed('owner-x', 'opponent-y:1000:1016')}))\n")
    seen = set()
    for hashseed in ("0", "1", "12345"):
        out = _run_script(script, hashseed=hashseed)
        seen.add(out["ghost"])

    assert len(seen) > 1, (
        "the hash()-seeded control produced the SAME seed under three "
        "PYTHONHASHSEED values, so it did not demonstrate the salted-hash "
        "failure the sha256 choice exists to prevent -- and the control is "
        "vacuous")