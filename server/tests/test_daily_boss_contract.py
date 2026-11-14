"""CONTRACT tests for ``server/daily_boss.py`` and the two routes it backs.

WHAT THIS FILE PINS, AND WHY IT IS NOT A DUPLICATE OF THE SIBLING DAILY-BOSS FILES
----------------------------------------------------------------------------------
Four sibling files already exist and none of them pins the WIRE:

  * ``test_daily_boss_grief.py``         -- the griefing fix and its detector.
  * ``test_daily_boss_strike_xp_is_capped.py`` -- the battle-pass faucet bound.
  * ``test_daily_boss_corrupt_save_types.py``  -- corrupt saved-record repair.
  * ``test_daily_boss_restart_durability.py``  -- does the share survive a restart.
  * ``test_daily_boss_routes_contract.py``     -- the LADDER routes
    (``/boss-ladder/...``, served by ``daily_boss_routes.py``). A DIFFERENT
    module with a DIFFERENT reply shape.

This file pins the half nobody else does: the shape of what
``GET /daily-boss/{player_id}`` and ``POST /daily-boss/{player_id}/strike``
actually return, who is allowed to ask, and what a refusal looks like.

  1. THE ROUTE TABLE. The two paths this module backs, their methods, and the
     rule that a new ``/daily-boss/...`` path is a new contract.
  2. THE REQUEST. There is NO request model. A forged body is answered, not
     refused -- and the one body field that IS meaningful (``player_id``) is
     caught by the ownership guard, not by this module.
  3. THE REPLY KEY SETS, FROZEN. A client indexes these by name. Adding a key
     is safe; RENAMING one breaks every caller.
  4. AUTH. Anonymous, four bad-credential spellings, a path naming another
     player, and a body naming another player.
  5. THE ERROR ARMS. 403 for a foreign or unknown player, 405 for the wrong
     verb. Note what is NOT here: there is no 404 on these paths, because the
     ownership guard answers before the handler's own existence check runs.
  6. THE REFUSAL VOCABULARY. Every zero-payout reason is reachable and each one
     names itself in ``no_payout_reason``.

HOW THE FIGURES WERE OBTAINED
-----------------------------
Every number, key set and status code below was measured by driving the real
``server.main`` app over the in-memory store and by calling ``daily_boss``
directly, on 2026-10-03 -- not read off the source. Constants are written as
``daily_boss.<NAME>`` or ``main.<NAME>`` rather than frozen numbers, so a
deliberate rebalance moves the bound with it.

WHY THE REAL ``main`` APP
-------------------------
Unlike the ladder contract file (which mounts a bare app), these two routes are
defined inline in ``server/main.py`` with no ``register_*`` entry point, so a
bare app could not measure the shipped wiring. The cost is the shared-tree
dependency: a peer's half-finished edit to ``main.py`` aborts collection for
this file too. When that happens, run it with ``--noconftest`` -- the module
imports its own paths, so nothing here needs the repository conftest.

RUN
---
    tools/wc_pytest.sh server/tests/test_daily_boss_contract.py
"""
from __future__ import annotations

import os
import sys

# Force the in-memory store BEFORE anything imports the store module.
for _var in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT"):
    os.environ.pop(_var, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

import pathlib  # noqa: E402

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import main  # noqa: E402

# `main.py` imports the boss as a TOP-LEVEL module (`import daily_boss`), so the
# test must hold that same module object. `from server import daily_boss` would be
# a SECOND object with its own `_SHARED` dict, and every shared-pool assertion
# below would then be reading a pool the route never touches.
import daily_boss  # noqa: E402

import auth  # noqa: E402  (BARE name: the app resolves tokens against this store)


# ---------------------------------------------------------------------------
# The two paths and their methods, as measured off the live route table.
# ---------------------------------------------------------------------------

READ_PATH = "/daily-boss/{player_id}"
STRIKE_PATH = "/daily-boss/{player_id}/strike"

#: Every ``/daily-boss`` path on the app today, measured. Three of these belong
#: to ``daily_boss_tables.py`` (a different module, a different contract); they
#: are listed so a collision between the two modules' prefixes is visible rather
#: than silent.
ALL_DAILY_BOSS_PATHS = {
    "/daily-boss/claim": ["POST"],
    "/daily-boss/enter": ["POST"],
    "/daily-boss/info": ["GET"],
    READ_PATH: ["GET"],
    STRIKE_PATH: ["POST"],
}

#: Frozen reply key sets. Read off live 200 replies, then cross-checked against
#: ``daily_boss.view``'s own return dict and the ``_reply`` wrapper in
#: ``strike``. Adding is safe; renaming breaks a client.
VIEW_KEYS = {
    "boss", "bp_xp_per_strike", "cleared", "day_index", "enabled",
    "player_bp_xp_remaining_today", "player_bp_xp_reserved_today",
    "player_strike_cap", "player_strikes_today", "remaining_pod", "server_now",
    "shared_bp_xp_remaining_today", "strike_points_by_realm",
    "tribulation_cycle_day",
}

#: What the STRIKE route adds on top of the read reply. ``battle_pass`` comes
#: from ``main.post_daily_boss_strike``; the rest from ``daily_boss._reply``.
STRIKE_ONLY_KEYS = {"battle_pass", "granted_legacy_points", "ok", "reason"}

#: The nested battle-pass receipt a paying press carries.
BATTLE_PASS_KEYS = {"leveled_up", "new_level", "ok", "source", "total_xp",
                    "xp_gained"}

#: The four keys of the ``boss`` block a client draws text from.
BOSS_KEYS = {"name", "reward_label", "reward_legacy_points", "title"}

T0 = 2000000000.0  # a fixed instant, so day math is deterministic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_shared_boss():
    """Every test starts from an empty pod, a full budget and today's day.

    The pod is ONE shared, once-a-day resource for the whole server, so a test
    that ran after another cleared it would see its "first" press short-circuit
    and prove nothing. Restore on the way OUT as well as in: this file clears
    the pod repeatedly, and leaving it dead poisons every later file in the run.
    """
    saved = dict(daily_boss._SHARED)
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(T0)
    yield
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(saved)


@pytest.fixture(scope="module")
def client():
    """The REAL app. ``raise_server_exceptions=False`` so a 500 is a status."""
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _new_player(client, label: str) -> str:
    """Create a player through the real route and mint them a session.

    The player id comes from the session, never a body, so the row must exist or
    every call fails for a reason unrelated to the boss.
    """
    r = client.post("/player/create", json={"name": "dbc-%s" % label,
                                            "spirit_root": "none"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    return pid


def _headers(pid: str) -> dict:
    return {"Authorization": "Bearer %s" % auth.issue_session(pid)}


@pytest.fixture
def player(client):
    return _new_player(client, "solo")


def _force_pod_almost_empty(presses: int = 1) -> None:
    """Put the shared pod ``presses`` mortal presses away from empty.

    Used for the clearing press: the last of those presses is the one that
    empties the pod, so it answers ``boss_cleared``. Pass ``presses=2`` when the
    test needs one non-clearing press BEFORE the clearing one.
    """
    daily_boss._SHARED["total_strikes"] = (
        daily_boss.POD_STRIKES - presses * daily_boss._STRIKE_POINTS["mortal"])
    daily_boss._SHARED["cleared"] = False


def _force_pod_empty() -> None:
    """Put the shared pod ALREADY empty and flagged cleared.

    Used for the "press against a downed boss" arms, where nobody empties it.
    """
    daily_boss._SHARED["total_strikes"] = daily_boss.POD_STRIKES
    daily_boss._SHARED["cleared"] = True


class _Receipt(list):
    """The receipt hook is an object with ``add(key, label, amount, kind)``.

    It is NOT ``list.append``: ``strike`` calls a named method with four
    arguments, so a bare list method raises ``AttributeError`` on the press that
    grants the bonus -- which is exactly the press a test cares about.
    """

    def add(self, key, label, amount, kind):
        self.append((key, label, amount, kind))


# ===========================================================================
# 1. THE ROUTE TABLE
# ===========================================================================

def test_the_two_paths_backed_by_this_module_are_mounted_once(client):
    """``GET {player_id}`` and ``POST {player_id}/strike``, one each.

    A second route with the same method and path shadows the first and the
    shadowed one silently stops running, so the count matters as much as the
    presence.
    """
    found: dict = {}
    for r in client.app.routes:
        path = getattr(r, "path", "")
        methods = sorted(getattr(r, "methods", []) or [])
        if path in (READ_PATH, STRIKE_PATH):
            found.setdefault(path, []).append(methods)
    assert found == {READ_PATH: [["GET"]], STRIKE_PATH: [["POST"]]}, (
        "the daily-boss route table changed for the paths this module backs; a "
        "client routing on them breaks. Measured: %r" % (found,))


def test_no_other_daily_boss_path_was_added(client):
    """The whole ``/daily-boss`` prefix, measured -- three other routes included.

    ``/daily-boss/info``, ``/enter`` and ``/claim`` belong to
    ``daily_boss_tables.py``, a different module with a different reply shape.
    They are frozen here too, because a fourth path added under this prefix is a
    new contract and must be declared deliberately.
    """
    found = {}
    for r in client.app.routes:
        path = getattr(r, "path", "")
        if path.startswith("/daily-boss"):
            found.setdefault(path, []).extend(sorted(getattr(r, "methods", []) or []))
    found = {p: sorted(set(m)) for p, m in found.items()}
    assert found == ALL_DAILY_BOSS_PATHS, (
        "the /daily-boss prefix changed. Unexpected: %r / missing: %r"
        % (sorted(set(found) - set(ALL_DAILY_BOSS_PATHS)),
           sorted(set(ALL_DAILY_BOSS_PATHS) - set(found))))


@pytest.mark.parametrize("method,path", [
    ("HEAD", READ_PATH), ("POST", READ_PATH),
    ("GET", STRIKE_PATH), ("DELETE", STRIKE_PATH), ("PUT", STRIKE_PATH),
])
def test_the_wrong_verb_on_each_path_is_405(client, player, method, path):
    """A client probing the wrong verb is TOLD 405, never silently served.

    ``HEAD`` on the read route is the one worth pinning: the general FastAPI
    folklore is that every GET accepts HEAD automatically. These routes do not,
    so a client that health-checks with HEAD gets a 405 rather than an empty 200.
    """
    call = {"GET": client.get, "HEAD": client.head, "POST": client.post,
            "PUT": client.put, "DELETE": client.delete}[method]
    r = call("/daily-boss/%s" % player if path == READ_PATH
             else "/daily-boss/%s/strike" % player,
             headers=_headers(player))
    assert r.status_code == 405, "%s %s answered %d" % (method, path, r.status_code)
    if method != "HEAD":
        # A HEAD reply carries no body by definition, so only the other verbs
        # can be asked to name the failure.
        assert r.json()["detail"] == "Method Not Allowed", r.text


# ===========================================================================
# 2. THE REQUEST
# ===========================================================================

def test_the_strike_route_declares_no_request_model(client, player):
    """The handler takes NO body parameters at all.

    Measured: the route's signature is the path parameter and nothing else. That
    is the anti-abuse design -- there is no field for a caller to name a damage
    number, a realm, an amount or a reward, so a forged body can only be ignored.
    Adding a body model is a security change and must break this test.
    """
    route = [r for r in client.app.routes if getattr(r, "path", "") == STRIKE_PATH
             ][0]
    assert route.body_field is None, (
        "the strike route grew a request body: %r. Every caller-supplied field "
        "on a payout route is a place a forged value can enter." % (route.body_field,))


@pytest.mark.parametrize("forged", [
    {"damage": 10 ** 9}, {"damage_dealt": 10 ** 9}, {"amount": 999_999},
    {"legacy_points": 10 ** 9}, {"realm": "immortal"}, {"points": 1600},
    {"reward_legacy_points": 10 ** 9}, {"amount_legacy_points": 10 ** 9},
    {"granted_legacy_points": 10 ** 9}, {"player_strike_cap": 10_000},
])
def test_a_forged_body_is_answered_and_the_pod_spends_the_servers_own_number(
        client, player, forged):
    """A forged number changes nothing, and the amount spent is measurable.

    The expected spend is read at run time out of the reply's OWN
    ``strike_points_by_realm`` table -- the server's source of truth -- rather
    than a literal. A self-consistent check ("the pod fell by whatever the reply
    claims") would pass even with the damage hardcoded, which is exactly the
    defect this test exists to catch.
    """
    h = _headers(player)
    before = client.get("/daily-boss/%s" % player, headers=h).json()
    table = before["strike_points_by_realm"]
    realm = main.players_db[player].get("cultivation_realm", "mortal")
    assert realm in table, (
        "the reply's damage table no longer names the %r realm this player has: "
        "%r" % (realm, table))

    r = client.post("/daily-boss/%s/strike" % player, headers=h, json=dict(forged))
    assert r.status_code == 200, (forged, r.text)
    after = r.json()
    assert after["reason"] in ("strike_recorded", "boss_cleared"), (forged, after)
    assert after["granted_legacy_points"] == 0 or after["reason"] == "boss_cleared"

    spent = before["remaining_pod"] - after["remaining_pod"]
    assert spent == table[realm], (
        "a forged body of %r moved the pod by %d points, but this player's "
        "realm (%s) is worth %d in the server's own table: the press is "
        "spending something other than the server's figure"
        % (forged, spent, realm, table[realm]))
    assert spent != 10 ** 9


def test_a_body_naming_another_player_is_403(client, player):
    """A ``player_id`` in the BODY is caught by the ownership guard.

    This route's own handler never reads the body, so the 403 comes from the
    session layer, not from ``daily_boss``. That is the point: the field exists
    in the reply's vocabulary but is refused at the edge rather than trusted.
    """
    r = client.post("/daily-boss/%s/strike" % player, headers=_headers(player),
                    json={"player_id": "somebody-else-entirely"})
    assert r.status_code == 403, (
        "a body naming another player was answered %d; it must be 403"
        % r.status_code)
    assert r.json()["detail"] == "player_id does not match the authenticated session"


def test_no_body_at_all_is_accepted(client, player):
    """The press takes no body, so omitting one is not an error.

    Measured: 200. A client that sends no body and a client that sends ``{}``
    must be indistinguishable.
    """
    r = client.post("/daily-boss/%s/strike" % player, headers=_headers(player))
    assert r.status_code == 200, r.text
    assert r.json()["reason"] in ("strike_recorded", "boss_cleared")


# ===========================================================================
# 3. THE REPLY KEY SETS
# ===========================================================================

def test_the_read_reply_key_set_is_frozen(client, player):
    r = client.get("/daily-boss/%s" % player, headers=_headers(player))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == VIEW_KEYS, (
        "the read reply's key set changed. Added: %r  Removed: %r. A client "
        "indexing by name breaks on a removal."
        % (sorted(set(body) - VIEW_KEYS), sorted(VIEW_KEYS - set(body))))
    assert body["enabled"] is True


def test_the_nested_blocks_carry_their_own_key_sets(client, player):
    """``boss`` and ``strike_points_by_realm`` are objects a client indexes."""
    body = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    assert set(body["boss"]) == BOSS_KEYS, sorted(body["boss"])
    assert isinstance(body["strike_points_by_realm"], dict)
    assert body["strike_points_by_realm"], "the realm damage table is empty"
    assert "mortal" in body["strike_points_by_realm"]
    # Every entry is a positive whole number of points, or the pod math is wrong.
    for realm, points in body["strike_points_by_realm"].items():
        assert isinstance(points, int) and not isinstance(points, bool), (realm, points)
        assert points > 0, (realm, points)


def test_the_strike_reply_is_the_read_reply_plus_the_strike_fields(client, player):
    """One drawer can render both. The strike reply EXTENDS the read reply."""
    h = _headers(player)
    view_keys = set(client.get("/daily-boss/%s" % player, headers=h).json())
    body = client.post("/daily-boss/%s/strike" % player, headers=h).json()
    assert set(body) == view_keys | STRIKE_ONLY_KEYS, (
        "unexpected: %r / missing: %r"
        % (sorted(set(body) - view_keys - STRIKE_ONLY_KEYS),
           sorted((view_keys | STRIKE_ONLY_KEYS) - set(body))))
    assert body["ok"] is True


def test_a_paying_press_carries_a_battle_pass_receipt_and_no_no_payout_reason(
        client, player):
    """A press that pays: full receipt, no refusal marker.

    ``no_payout_reason`` is the client's signal that nothing was paid, so a
    PAYING press carrying it (or a refusing press omitting it) both break the
    branch.
    """
    h = _headers(player)
    body = client.post("/daily-boss/%s/strike" % player, headers=h).json()
    assert body["reason"] == "strike_recorded", body
    assert "no_payout_reason" not in body, body
    receipt = body["battle_pass"]
    assert set(receipt) == BATTLE_PASS_KEYS, sorted(receipt)
    assert receipt["ok"] is True
    assert receipt["source"] == "daily_boss_strike", receipt
    assert receipt["xp_gained"] == daily_boss.BP_XP_PER_STRIKE, receipt


def test_the_reply_echoes_the_servers_own_configuration(client, player):
    """The limits come from the server, so a phone cannot draw its own clock.

    A client computing its own midnight for the daily reset draws the WRONG
    boundary. These keys are how it draws the server's instead.
    """
    body = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    assert body["player_strike_cap"] == daily_boss.PLAYER_STRIKE_CAP, body
    assert body["bp_xp_per_strike"] == daily_boss.BP_XP_PER_STRIKE, body
    assert body["remaining_pod"] == daily_boss.POD_STRIKES, body
    assert body["shared_bp_xp_remaining_today"] == daily_boss.DAILY_BP_XP_BUDGET
    assert body["boss"]["reward_legacy_points"] == \
        daily_boss.BOSS["reward_legacy_points"], body
    # Compared against the SAME instant the reply reports, not a frozen T0:
    # the route reads the real server clock, so pinning a literal would fail
    # every day but one.
    assert body["tribulation_cycle_day"] == \
        daily_boss.tribulation_cycle_day(body["server_now"])
    assert body["server_now"] > 0
    assert isinstance(body["day_index"], int)


def test_the_numeric_fields_a_client_draws_have_the_right_types(client, player):
    """Types are the contract too: a float where an int belongs breaks equality."""
    body = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    for key in ("day_index", "remaining_pod", "player_strikes_today",
                "player_strike_cap", "bp_xp_per_strike",
                "player_bp_xp_reserved_today", "player_bp_xp_remaining_today",
                "shared_bp_xp_remaining_today", "tribulation_cycle_day"):
        assert isinstance(body[key], int) and not isinstance(body[key], bool), (
            "%s is %r, not a whole number; a client indexing on it breaks"
            % (key, body[key]))
    for key in ("enabled", "cleared"):
        assert isinstance(body[key], bool), (key, body[key])
    assert isinstance(body["server_now"], float)


def test_the_view_hands_out_copies_so_a_caller_cannot_rewrite_the_server():
    """A caller editing the reply must not become everyone's payout.

    Measured: writing ``reward_legacy_points = -5000`` through the reply left
    ``daily_boss.BOSS`` at 100. One caller's edit through the shared module dict
    used to make the next genuine clear credit a NEGATIVE bonus.
    """
    view = daily_boss.view({"cultivation_realm": "mortal"}, T0)
    view["boss"]["reward_legacy_points"] = -5000
    assert daily_boss.BOSS["reward_legacy_points"] > 0, (
        "a caller rewrote the module's boss constant through the reply; every "
        "later player's clear bonus is now attacker-controlled")

    view = daily_boss.view({"cultivation_realm": "mortal"}, T0)
    view["strike_points_by_realm"]["mortal"] = -1
    assert daily_boss._STRIKE_POINTS["mortal"] > 0, (
        "a caller rewrote the module's realm damage table through the reply")


# ===========================================================================
# 4. AUTH
# ===========================================================================

@pytest.mark.parametrize("method", ["GET", "POST"])
def test_anonymous_is_refused_401_on_both_routes(client, player, method):
    """No session, no data -- on the read AND the write."""
    url = ("/daily-boss/%s" % player if method == "GET"
           else "/daily-boss/%s/strike" % player)
    r = client.request(method, url)
    assert r.status_code == 401, (
        "%s answered %d to an anonymous caller" % (method, r.status_code))
    assert r.json()["detail"] == "invalid or missing session token"


@pytest.mark.parametrize("header", [
    {"Authorization": "Bearer "},                  # empty token
    {"Authorization": "Bearer totally-made-up"},    # unknown token
    {"Authorization": "Basic totally-made-up"},     # wrong scheme
    {"Authorization": "totally-made-up"},           # no scheme at all
])
def test_a_bad_credential_is_401_with_the_same_vague_detail(client, player, header):
    """Every credential failure answers 401 with the SAME string.

    Distinguishing "no token" from "wrong token" tells an attacker which half
    they got right. The four failures are deliberately indistinguishable.
    """
    r = client.get("/daily-boss/%s" % player, headers=header)
    assert r.status_code == 401, (header, r.status_code)
    assert r.json()["detail"] == "invalid or missing session token"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_path_naming_another_player_is_403(client, player, method):
    """A signed-in player asking about somebody else is refused, not served.

    Both routes name a player in the PATH, so this is the shape the ownership
    guard exists for.
    """
    victim = _new_player(client, "victim")
    other = _new_player(client, "other")
    url = ("/daily-boss/%s" % victim if method == "GET"
           else "/daily-boss/%s/strike" % victim)
    r = client.request(method, url, headers=_headers(other))
    assert r.status_code == 403, (
        "%s for another player's path answered %d; it must be 403"
        % (method, r.status_code))
    assert r.json()["detail"] == "player_id does not match the authenticated session"


def test_a_refused_request_moves_nothing(client, player):
    """A cross-player press costs the victim no press and the pool no points."""
    h = _headers(player)
    before = client.get("/daily-boss/%s" % player, headers=h).json()
    attacker = _new_player(client, "attacker")

    r = client.post("/daily-boss/%s/strike" % player,
                    headers=_headers(attacker))
    assert r.status_code == 403, r.text

    after = client.get("/daily-boss/%s" % player, headers=h).json()
    assert after["remaining_pod"] == before["remaining_pod"], (
        "a refused press moved the shared pod")
    assert after["player_strikes_today"] == before["player_strikes_today"], (
        "a refused press cost the victim a press")
    assert after["shared_bp_xp_remaining_today"] == \
        before["shared_bp_xp_remaining_today"], (
        "a refused press reserved budget out of the shared pool")


# ===========================================================================
# 5. THE ERROR ARMS
# ===========================================================================

@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_unknown_player_answers_403_not_404(client, method):
    """MEASURED: an unknown id is 403, and the handler's 404 is unreachable.

    ``main.get_daily_boss`` / ``main.post_daily_boss_strike`` both raise
    ``HTTPException(404, "Player not found")`` when the id is absent -- but the
    ownership guard runs FIRST and compares the path id against the session, so
    a signed-in player asking about a player who does not exist is told 403
    before the existence check runs.

    Pinned as the shipped wire, not as an endorsement. It is the honest reading
    anyway: "not yours" and "not there" are the same answer to a caller who may
    not know whether the id exists, and it leaks nothing. If a peer ever makes
    the 404 reachable, this test goes red and the change is then deliberate.
    """
    ghost = _new_player(client, "ghost")  # a real session for a real player
    url = ("/daily-boss/dbc-ghost-never-created" if method == "GET"
           else "/daily-boss/dbc-ghost-never-created/strike")
    r = client.request(method, url, headers=_headers(ghost))
    assert r.status_code == 403, (
        "%s for an unknown player answered %d. If the 404 became reachable, "
        "update this test and the handler together." % (method, r.status_code))
    assert "Player not found" not in r.text, (
        "the handler's 404 detail leaked into the reply: %r" % r.text[:200])


def test_an_unknown_path_under_the_prefix_is_404(client, player):
    r = client.get("/daily-boss/nope/nope", headers=_headers(player))
    assert r.status_code == 404, r.text


# ===========================================================================
# 6. THE REFUSAL VOCABULARY
# ===========================================================================

def test_every_declared_no_payout_reason_names_itself_in_the_reply():
    """Each refusal carries ``no_payout_reason`` equal to its own ``reason``.

    Measured, at the module level, by forcing each bound:
      * ``daily_strike_cap_reached``  -- a player past their own allowance.
      * ``daily_bp_budget_exhausted`` -- the server's whole day spent.
    ``boss_already_cleared`` is listed but is NOT emitted by this module on the
    paths that reach the route's zero-payout branch, and it deliberately does not
    carry the extra key (the route keys on it directly).
    """
    seen = {}

    # (a) the player's own allowance, spent.
    player = {"cultivation_realm": "mortal"}
    for _ in range(daily_boss.PLAYER_STRIKE_CAP):
        daily_boss.strike(player, T0)
    seen["daily_strike_cap_reached"] = daily_boss.strike(player, T0)

    # (b) the shared budget, drawn down to one press's worth.
    daily_boss._SHARED["bp_xp_remaining"] = daily_boss.BP_XP_PER_STRIKE
    fresh = {"cultivation_realm": "mortal"}
    daily_boss.strike(fresh, T0)
    seen["daily_bp_budget_exhausted"] = daily_boss.strike(fresh, T0)

    for reason, body in seen.items():
        assert reason in daily_boss.NO_PAYOUT_REASONS, reason
        assert body["reason"] == reason, body
        assert body["no_payout_reason"] == reason, (
            "a refusing press that does not name itself cannot be told apart "
            "from a paying one that paid zero: %r" % (body,))
        assert body["ok"] is True, "a refusal is still a 200 with ok True"
        assert body["granted_legacy_points"] == 0, body

    assert "boss_already_cleared" in daily_boss.NO_PAYOUT_REASONS


def test_the_reasons_never_overlap_a_paying_press():
    """No paying reason appears in the refusal vocabulary.

    If ``strike_recorded`` ever joined ``NO_PAYOUT_REASONS``, the route's own
    zero-payout branch would start refusing ordinary presses.
    """
    for reason in ("strike_recorded", "boss_cleared"):
        assert reason not in daily_boss.NO_PAYOUT_REASONS, (
            "%s pays, so it must not be listed as a no-payout reason" % reason)


def test_a_refused_press_is_a_press_and_reaches_the_routes_cap_branch(client, player):
    """The counter goes to exactly CAP + 1, and the route stops paying.

    Measured through the real route: presses 1..CAP answer ``strike_recorded``
    with battle-pass points granted; press CAP+1 and every press after it answer
    ``daily_strike_cap_reached`` with ``xp_gained == 0`` and the counter pinned
    at CAP+1 forever.

    Two things are pinned here. First, a refusal COSTS a press -- it has to
    advance the counter past the cap or the route's own cap branch would pay a
    press that did nothing. Second, the counter stops growing at CAP+1 rather
    than running to 300+, because that number is persisted into the player's
    saved record for good.
    """
    cap = main.DAILY_BOSS_BP_STRIKE_CAP
    h = _headers(player)
    paid = [client.post("/daily-boss/%s/strike" % player, headers=h).json()
            for _ in range(cap)]
    assert [p["player_strikes_today"] for p in paid] == list(range(1, cap + 1)), \
        [p["player_strikes_today"] for p in paid]
    for p in paid:
        assert p["reason"] == "strike_recorded", p
        assert p["battle_pass"]["xp_gained"] == daily_boss.BP_XP_PER_STRIKE, p
        assert "no_payout_reason" not in p, p

    refused = [client.post("/daily-boss/%s/strike" % player, headers=h).json()
               for _ in range(3)]
    for body in refused:
        assert body["reason"] == "daily_strike_cap_reached", body
        assert body["no_payout_reason"] == body["reason"], body
        assert body["battle_pass"]["xp_gained"] == 0, body
        assert body["battle_pass"]["reason"] == "daily_strike_cap_reached", body
        # Pinned, not growing: CAP + 1 is the whole truth the contract needs.
        assert body["player_strikes_today"] == cap + 1, (
            "the refused-press counter reached %d; it is persisted into the "
            "player's saved record forever and must stop at cap+1"
            % body["player_strikes_today"])


def test_a_refused_press_still_spends_no_pod_and_no_budget(client, player):
    """The refusal is free: no pod damage, no budget reservation."""
    h = _headers(player)
    for _ in range(main.DAILY_BOSS_BP_STRIKE_CAP):
        client.post("/daily-boss/%s/strike" % player, headers=h)
    before = client.get("/daily-boss/%s" % player, headers=h).json()
    client.post("/daily-boss/%s/strike" % player, headers=h)
    after = client.get("/daily-boss/%s" % player, headers=h).json()
    assert after["remaining_pod"] == before["remaining_pod"], (
        "a press past the allowance still damaged the shared boss")
    assert after["shared_bp_xp_remaining_today"] == \
        before["shared_bp_xp_remaining_today"], (
        "a press past the allowance still reserved budget out of the shared pool")


def test_an_empty_pod_refuses_the_press_instead_of_paying_for_no_damage():
    """A press against a DRAINED boss does no damage, so it is refused.

    RE-MEASURED 2026-10-04 after the module gained a zero-contribution bound
    (``MIN_CONTRIBUTION_POINTS``): a press on a pod somebody else already emptied
    is now ``no_contribution_shared_boss``, not a paying ``strike_recorded``.

    This SUPERSEDES the older contract, which paid a press that dealt zero
    points. Paying it was a real drain: one zero-contribution player reserved a
    whole day's battle-pass share out of the shared pool while adding nothing to
    the boss, so a fleet of them could empty the day's budget for every honest
    player. The refusal is the fix; this test pins the fixed behaviour.

    The press is still RECORDED (the counter advances) so it cannot be re-run as
    if it never happened, and it costs the player no battle-pass points.
    """
    _force_pod_empty()
    player = {"cultivation_realm": "mortal"}
    budget_before = daily_boss._SHARED["bp_xp_remaining"]

    first = daily_boss.strike(player, T0)
    assert first["reason"] == "no_contribution_shared_boss", first
    assert first["no_payout_reason"] == "no_contribution_shared_boss", first
    assert first["cleared"] is True, first
    assert first["remaining_pod"] == 0, first
    assert first["granted_legacy_points"] == 0, first
    assert first["ok"] is True, "a refusal is still a 200 carrying ok True"
    # The press was RECORDED -- a refusal that did not count could be replayed.
    assert first["player_strikes_today"] >= daily_boss.PLAYER_STRIKE_CAP + 1, (
        "a refused press left the counter at %r, under the cap; the route pays "
        "on `strikes_today <= CAP`, so it would hand out battle-pass points for "
        "a press that dealt no damage" % (first["player_strikes_today"],))
    # ...and it reserved NOTHING out of the shared budget. This is the drain the
    # bound exists to stop, so it is asserted directly.
    assert daily_boss._SHARED["bp_xp_remaining"] == budget_before, (
        "a zero-damage press still reserved %r out of the shared pool"
        % (budget_before - daily_boss._SHARED["bp_xp_remaining"],))
    assert first["player_bp_xp_reserved_today"] == 0, first

    # Repeating it keeps refusing, and the SECOND refusal names the cap --
    # because the first one already pushed the counter past the cap, and the cap
    # check comes first. Both are refusals and both pay nothing, which is what a
    # client cares about; the reason is simply the outermost bound that fired.
    second = daily_boss.strike(player, T0)
    assert second["reason"] == "daily_strike_cap_reached", second
    assert second["no_payout_reason"] == "daily_strike_cap_reached", second
    assert second["granted_legacy_points"] == 0, second
    assert second["player_strikes_today"] == daily_boss.PLAYER_STRIKE_CAP + 1, second
    # The player record was NOT touched by a press on a drained boss.
    assert player.get("legacy_bonus") is None, player


def test_a_press_that_deals_no_points_is_listed_as_a_no_payout_reason():
    """The new bound joins the refusal vocabulary the route reads.

    ``main`` keys its zero-payout branch on ``boss_already_cleared`` alone and
    reaches the others via ``no_payout_reason`` plus an over-cap counter, so a
    reason that is NOT in this tuple would pay silently.
    """
    assert "no_contribution_shared_boss" in daily_boss.NO_PAYOUT_REASONS, (
        "the zero-contribution bound is not in NO_PAYOUT_REASONS; the route's "
        "zero-payout branch will not see it: %r" % (daily_boss.NO_PAYOUT_REASONS,))
    # It must not overlap a reason that pays.
    for paying in ("strike_recorded", "boss_cleared"):
        assert paying not in daily_boss.NO_PAYOUT_REASONS, paying


def test_the_clearing_press_pays_the_clear_bonus_exactly_once():
    """The bonus belongs to whoever empties the pod, once, ever that day."""
    _force_pod_almost_empty()
    player = {"cultivation_realm": "mortal", "legacy_bonus": 5.0}
    clearing = daily_boss.strike(player, T0)
    assert clearing["reason"] == "boss_cleared", clearing
    assert clearing["granted_legacy_points"] == daily_boss.BOSS["reward_legacy_points"]
    assert player["legacy_bonus"] == 5.0 + daily_boss.BOSS["reward_legacy_points"]
    assert player["legacy"]["daily_boss_clears"] == 1, player
    assert player["legacy"]["last_daily_boss_at"] == T0

    # A different player pressing the now-empty pod is refused, not paid: the
    # press deals zero points, so there is nothing to pay for (the zero-
    # contribution bound, section 6).
    other = {"cultivation_realm": "immortal"}
    after = daily_boss.strike(other, T0)
    assert after["reason"] == "no_contribution_shared_boss", after
    assert after["granted_legacy_points"] == 0, after
    assert other.get("legacy_bonus") is None, other


def test_the_clearing_press_calls_the_receipt_callback_once():
    """The receipt hook fires only on the press that cleared the pod.

    Measured: over a run of presses the receipt list gained exactly one entry,
    on the press that emptied the pod.
    """
    _force_pod_almost_empty(presses=2)
    receipts = _Receipt()
    # A press that does real work against a live pod writes nothing.
    first = daily_boss.strike({"cultivation_realm": "mortal"}, T0, receipt=receipts)
    assert first["reason"] == "strike_recorded", first
    assert receipts == [], "a non-clearing press wrote a receipt"

    # The press that empties it writes exactly one, with the four-argument
    # ``add(key, label, amount, kind)`` shape the route's receipt list uses.
    clearing = daily_boss.strike({"cultivation_realm": "mortal"}, T0,
                                 receipt=receipts)
    assert clearing["reason"] == "boss_cleared", clearing
    assert len(receipts) == 1, receipts
    key, label, amount, kind = receipts[0]
    assert key == "daily_boss_reward", receipts
    assert amount == daily_boss.BOSS["reward_legacy_points"], receipts
    assert kind == "legacy_points", receipts
    assert isinstance(label, str) and label, receipts

    # And a press after the clear adds no second receipt -- it is refused
    # outright, because a drained boss absorbs no points.
    after = daily_boss.strike({"cultivation_realm": "immortal"}, T0,
                              receipt=receipts)
    assert after["reason"] == "no_contribution_shared_boss", after
    assert len(receipts) == 1, receipts


def test_a_receipt_of_none_is_fine():
    """The route passes no receipt, so the hook must be optional."""
    _force_pod_almost_empty()
    body = daily_boss.strike({"cultivation_realm": "mortal"}, T0)
    assert body["reason"] == "boss_cleared", body


def test_a_shared_budget_exhaustion_is_named_not_silent():
    """The server's whole day spent is a LOUD refusal, not a silent zero.

    Measured: with the budget cut to one press's worth, the first press pays and
    the second answers ``daily_bp_budget_exhausted`` -- and it advances the
    press counter past the cap, which is what makes ``main``'s own cap branch
    refuse it rather than paying a press that did nothing.
    """
    cap = daily_boss.PLAYER_STRIKE_CAP
    daily_boss._SHARED["bp_xp_remaining"] = daily_boss.BP_XP_PER_STRIKE
    player = {"cultivation_realm": "mortal"}
    first = daily_boss.strike(player, T0)
    assert first["reason"] == "strike_recorded", first
    assert first["player_bp_xp_reserved_today"] == daily_boss.BP_XP_PER_STRIKE

    second = daily_boss.strike(player, T0)
    assert second["reason"] == "daily_bp_budget_exhausted", second
    assert second["no_payout_reason"] == "daily_bp_budget_exhausted", second
    assert second["player_strikes_today"] == cap + 1, (
        "the refusal did not advance the press counter past the cap, so a "
        "route that pays on `strikes_today <= CAP` would still pay it: %r"
        % (second,))


def test_a_players_reserved_share_survives_another_accounts_presses():
    """One account cannot spend another's reserved share. This is the fix.

    Measured: player A reserves their whole day; player B presses until the
    server's budget is nearly gone; A's remaining allowance is untouched.
    """
    a = {"cultivation_realm": "mortal"}
    reserved = daily_boss.strike(a, T0)["player_bp_xp_reserved_today"]
    assert reserved == daily_boss.PLAYER_BP_XP_SHARE

    # Drain most of the server's day with other accounts.
    for _ in range(main.DAILY_BOSS_BP_STRIKE_CAP):
        b = {"cultivation_realm": "mortal"}
        for _ in range(main.DAILY_BOSS_BP_STRIKE_CAP):
            daily_boss.strike(b, T0)
        b2 = {"cultivation_realm": "mortal"}
        for _ in range(main.DAILY_BOSS_BP_STRIKE_CAP):
            daily_boss.strike(b2, T0)
        if daily_boss._SHARED["bp_xp_remaining"] < reserved:
            break

    # A reserved their whole day and immediately spent one press out of it, so
    # what is left is one press short of the share.
    left = reserved - daily_boss.BP_XP_PER_STRIKE
    view = daily_boss.view(a, T0)
    assert view["player_bp_xp_reserved_today"] == reserved, (
        "another account's presses changed this player's reserved share")
    assert view["player_bp_xp_remaining_today"] == left, (
        "another account's presses spent this player's reserved allowance")
    # ...and A can still spend the rest of it, press by press, right to the cap.
    # The reserving press was press 1, so CAP - 1 more remain. The pod WAS
    # emptied by the other accounts above, so under the zero-contribution bound
    # the FIRST of these is refused as contributing nothing -- and every one
    # after it is refused by the CAP, because that first refusal already pushed
    # the counter past it. Neither refusal has anything to do with another
    # account touching A's money, which is exactly what this test is for.
    for _ in range(2, main.DAILY_BOSS_BP_STRIKE_CAP + 1):
        body = daily_boss.strike(a, T0)
        assert body["reason"] in ("no_contribution_shared_boss",
                                  "daily_strike_cap_reached"), body
        assert body["no_payout_reason"] == body["reason"], body
        assert body["player_bp_xp_reserved_today"] == reserved, (
            "another account's presses changed this player's reserved share: %r"
            % (body,))


# ===========================================================================
# 7. THE CLOCK
# ===========================================================================

def test_a_new_utc_day_resets_the_pod_the_budget_and_the_player_counters():
    """Midnight UTC is server-owned: no client input sets it.

    Measured: a player at the press cap with their share fully spent, on a pod
    that is cleared, reads a full day three days later -- presses 0, pod full,
    share unreserved, shared budget whole.
    """
    day = daily_boss._day_index(T0)
    spent = {"cultivation_realm": "mortal"}
    for _ in range(daily_boss.PLAYER_STRIKE_CAP):
        daily_boss.strike(spent, T0)
    daily_boss._SHARED["total_strikes"] = daily_boss.POD_STRIKES
    daily_boss._SHARED["cleared"] = True
    daily_boss._SHARED["bp_xp_remaining"] = 0

    later = T0 + 3 * daily_boss.DAY
    view = daily_boss.view(spent, later)
    assert view["day_index"] == day + 3, view
    assert view["cleared"] is False, view
    assert view["remaining_pod"] == daily_boss.POD_STRIKES, view
    assert view["player_strikes_today"] == 0, view
    assert view["player_bp_xp_reserved_today"] == 0, view
    assert view["player_bp_xp_remaining_today"] == 0, view
    assert view["shared_bp_xp_remaining_today"] == daily_boss.DAILY_BP_XP_BUDGET, view
    # And a press on the new day is a real press again.
    after = daily_boss.strike(spent, later)
    assert after["reason"] == "strike_recorded", after
    assert after["player_strikes_today"] == 1, after


def test_the_tribulation_cycle_day_is_one_through_twenty_one():
    """The R5 cadence clock is 1..21 and never 0 or 22.

    A 0 would read as "no day" to a client checking truthiness, and 22 would put
    a calendar out of range. The clock is derived from server time only.
    """
    span = daily_boss.TRIBULATION_CYCLE_DAYS * daily_boss.DAY
    seen = {daily_boss.tribulation_cycle_day(t) for t in range(0, span, 997)}
    assert min(seen) == 1 and max(seen) == daily_boss.TRIBULATION_CYCLE_DAYS, seen
    assert 0 not in seen and daily_boss.TRIBULATION_CYCLE_DAYS + 1 not in seen
    # It repeats exactly every 21 days.
    for offset in (0, 12345, span // 2):
        assert daily_boss.tribulation_cycle_day(offset) == \
            daily_boss.tribulation_cycle_day(offset + span), offset


# ===========================================================================
# 8. THE TWO MODULES' CONSTANTS AGREE
# ===========================================================================

def test_the_strike_routes_own_constants_match_this_module():
    """The route's bounds are read from here; drift between them is invisible.

    ``main.DAILY_BOSS_BP_STRIKE_CAP`` recomputes the cap from the pod size and
    ``main.DAILY_BOSS_BP_XP_PER_STRIKE`` restates the per-press figure. Both are
    what the route compares the reply against, so if either drifts from the
    module that produced the reply, a press silently stops paying or starts
    paying past its bound.
    """
    assert main.DAILY_BOSS_BP_STRIKE_CAP == daily_boss.PLAYER_STRIKE_CAP, (
        "the route's strike cap (%r) no longer matches this module's (%r)"
        % (main.DAILY_BOSS_BP_STRIKE_CAP, daily_boss.PLAYER_STRIKE_CAP))
    assert main.DAILY_BOSS_BP_XP_PER_STRIKE == daily_boss.BP_XP_PER_STRIKE, (
        "the route's per-strike figure (%r) no longer matches this module's (%r)"
        % (main.DAILY_BOSS_BP_XP_PER_STRIKE, daily_boss.BP_XP_PER_STRIKE))


def test_the_budget_is_sized_from_the_share_and_the_headroom():
    """The economy bounds are relationships, not frozen numbers.

    A player who reserves 0 because the day's budget is empty gets a silent
    zero, which is the failure the headroom exists to prevent; these are the
    relationships that keep a deliberate rebalance coherent.
    """
    assert daily_boss.PLAYER_BP_XP_SHARE == \
        daily_boss.PLAYER_STRIKE_CAP * daily_boss.BP_XP_PER_STRIKE
    assert daily_boss.DAILY_BP_XP_BUDGET == (
        daily_boss.DESIGN_PLAYERS * daily_boss.BUDGET_DAYS_OF_HEADROOM
        * daily_boss.PLAYER_BP_XP_SHARE)
    assert daily_boss.BUDGET_DAYS_OF_HEADROOM > 1, (
        "exactly one day of headroom means player 401 of 400 reserves zero and "
        "is refused -- a silent zero with no error anywhere")
    # A reservation is always a whole number of presses, or the reply could not
    # express what it paid.
    assert daily_boss.PLAYER_BP_XP_SHARE % daily_boss.BP_XP_PER_STRIKE == 0
    assert daily_boss.POD_STRIKES % daily_boss._STRIKE_POINTS["mortal"] == 0

# ===========================================================================
# 9. THE REFUSAL RECEIPT IS A DIFFERENT OBJECT
# ===========================================================================
#
# MEASURED, and the point of this section: ``battle_pass`` is NOT one shape.
#
#   * A PAYING press carries ``battle_pass.grant_xp``'s SIX-key receipt:
#     ok / source / xp_gained / new_level / leveled_up / total_xp.
#   * A REFUSING press past the cap carries a hand-written FOUR-key receipt:
#     xp_gained / leveled_up / reason / cap. No ``ok``, no ``source``, no
#     ``new_level``, no ``total_xp``.
#
# They are built in two different places (``main.post_daily_boss_strike`` lines
# 2613 and 2619), so they can drift apart without either changing shape
# silently. Section 3 froze the paying shape. This freezes the refusal shape,
# because the two are NOT subsets of one another: a drawer that reads
# ``receipt["total_xp"]`` -- correct on the press that paid -- raises KeyError
# on the very next press, which is the one the player is looking at.

REFUSING_RECEIPT_KEYS = {"cap", "leveled_up", "reason", "xp_gained"}

#: Keys present on a paying receipt and ABSENT on a refusing one. Named so the
#: asymmetry is a checked fact rather than something a reader has to diff.
PAYING_ONLY_RECEIPT_KEYS = {"new_level", "ok", "source", "total_xp"}


def _press_until_refused(client, player):
    """Press to the cap and hand back one refusal reply from the real route."""
    h = _headers(player)
    for _ in range(main.DAILY_BOSS_BP_STRIKE_CAP):
        client.post("/daily-boss/%s/strike" % player, headers=h)
    return client.post("/daily-boss/%s/strike" % player, headers=h).json()


def test_a_refusing_press_carries_the_four_key_receipt_not_the_six_key_one(
        client, player):
    """The refusal receipt has its OWN key set, and it is NOT the paying one.

    Frozen as the shipped wire. If a peer ever unifies the two shapes this test
    goes red and the change is then a deliberate client-contract edit -- which is
    also the moment every client drawer needs revisiting.
    """
    body = _press_until_refused(client, player)
    assert body["reason"] == "daily_strike_cap_reached", body
    receipt = body["battle_pass"]
    assert set(receipt) == REFUSING_RECEIPT_KEYS, (
        "the REFUSING receipt's key set changed. Added: %r  Removed: %r. This "
        "shape is built separately from the paying one, so a change here is not "
        "visible from a passing-press test."
        % (sorted(set(receipt) - REFUSING_RECEIPT_KEYS),
           sorted(REFUSING_RECEIPT_KEYS - set(receipt))))
    assert receipt["xp_gained"] == 0, receipt
    assert receipt["reason"] == body["reason"], (
        "the receipt and the top-level reason disagree about WHY nothing was "
        "paid: %r" % (body,))
    assert receipt["cap"] == main.DAILY_BOSS_BP_STRIKE_CAP, receipt
    assert receipt["leveled_up"] is False, receipt
    # The asymmetry, stated as a fact rather than left for a reader to diff.
    assert PAYING_ONLY_RECEIPT_KEYS & set(receipt) == set(), (
        "a paying-only receipt key appeared on a refusing press; the two shapes "
        "have been unified and every client drawer needs revisiting: %r"
        % (receipt,))


def test_the_two_receipt_shapes_are_disjoint_and_both_reachable(client):
    """Both shapes are pinned from the SAME live route, minutes apart in a run.

    Read separately, each frozen set could be satisfied by a route that only ever
    produces that one shape. Driving one press each way proves both arms of the
    branch in ``main.post_daily_boss_strike`` are live.
    """
    payer = _new_player(client, "shapes-pay")
    paying = client.post("/daily-boss/%s/strike" % payer,
                         headers=_headers(payer)).json()
    assert paying["reason"] == "strike_recorded", paying
    assert set(paying["battle_pass"]) == BATTLE_PASS_KEYS, sorted(paying["battle_pass"])
    # The two shapes share only the two keys any XP receipt must carry: the
    # amount and whether it crossed a level. Everything else differs.
    assert set(paying["battle_pass"]) & set(REFUSING_RECEIPT_KEYS) == {
        "leveled_up", "xp_gained"}, (
        "the two receipt shapes now overlap on keys beyond the shared "
        "amount/flag pair: %r" % (paying["battle_pass"],))

    refuser = _press_until_refused(client, _new_player(client, "shapes-refuse"))
    assert set(refuser["battle_pass"]) & PAYING_ONLY_RECEIPT_KEYS == set(), refuser


def test_a_refused_press_reply_is_the_paying_reply_plus_only_the_refusal_marker(
        client, player):
    """Top level, the refusal adds exactly ONE key: ``no_payout_reason``.

    The nested receipt changes shape (section 9), but the top-level key set is
    the paying set plus this single marker -- so a client can branch on one
    field and read everything else identically in both cases.
    """
    h = _headers(player)
    view_keys = set(client.get("/daily-boss/%s" % player, headers=h).json())
    body = _press_until_refused(client, player)
    assert set(body) == view_keys | STRIKE_ONLY_KEYS | {"no_payout_reason"}, (
        "unexpected: %r / missing: %r"
        % (sorted(set(body) - view_keys - STRIKE_ONLY_KEYS - {"no_payout_reason"}),
           sorted((view_keys | STRIKE_ONLY_KEYS | {"no_payout_reason"}) - set(body))))
    assert body["no_payout_reason"] == body["reason"], body
    assert body["ok"] is True, "a refusal is still a 200 carrying ok True"
    assert body["granted_legacy_points"] == 0, body


# ===========================================================================
# 10. THE QUERY STRING IS PART OF THE REQUEST SURFACE
# ===========================================================================
#
# Section 2 pins the BODY. The query string is the other place a caller can put
# a value, and MEASURED it is NOT uniformly ignored: the ownership guard reads a
# ``player_id`` out of it and refuses a foreign one, while every other query
# field is silently dropped. A client that assumed "this route ignores query
# strings" would get a 403 it cannot explain.

@pytest.mark.parametrize("method", ["GET", "POST"])
def test_a_query_string_naming_another_player_is_403(client, player, method):
    """``?player_id=<somebody else>`` is refused exactly like the body field.

    MEASURED: the guard logs ``reason=query`` and answers 403 with the same
    detail the body and path violations get. The handler itself reads no query
    parameter at all, so this is the guard, not ``daily_boss`` -- pinned because
    the query string is a third place the same claim can arrive and all three
    must answer alike.
    """
    url = ("/daily-boss/%s?player_id=somebody-else-entirely" % player if method == "GET"
           else "/daily-boss/%s/strike?player_id=somebody-else-entirely" % player)
    r = client.request(method, url, headers=_headers(player))
    assert r.status_code == 403, ("%s with a foreign ?player_id= answered %d; it "
                                  "must be 403" % (method, r.status_code))
    assert r.json()["detail"] == "player_id does not match the authenticated session"


def test_a_query_string_naming_the_callers_own_player_is_answered(client, player):
    """The matching ``?player_id`` is not a refusal -- it is simply redundant.

    Pinned so the guard cannot grow into refusing a client that helpfully echoes
    its own id back, which would be a 403 with no attacker in it.
    """
    h = _headers(player)
    for url in ("/daily-boss/%s?player_id=%s" % (player, player),
                "/daily-boss/%s/strike?player_id=%s" % (player, player)):
        method = client.get if "/strike" not in url else client.post
        r = method(url, headers=h)
        assert r.status_code == 200, (url, r.text)


def test_every_other_query_field_is_ignored_and_the_pod_spends_the_servers_number(
        client, player):
    """A forged QUERY STRING moves nothing -- same guarantee the body gets.

    Expected spend is read out of the reply's OWN ``strike_points_by_realm``
    table rather than a literal, so this cannot pass with the damage hardcoded.
    """
    h = _headers(player)
    before = client.get("/daily-boss/%s" % player, headers=h).json()
    table = before["strike_points_by_realm"]
    realm = main.players_db[player].get("cultivation_realm", "mortal")
    assert realm in table, (realm, table)

    qs = ("realm=immortal&damage=999999999&amount=999999999&legacy_points=999999999"
          "&points=999999999&reward_legacy_points=999999999&player_strike_cap=99999"
          "&bp_xp_per_strike=999999999")
    r = client.post("/daily-boss/%s/strike?%s" % (player, qs), headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] in ("strike_recorded", "boss_cleared"), body
    spent = before["remaining_pod"] - body["remaining_pod"]
    assert spent == table[realm], (
        "a forged query string moved the pod by %d points; this player's realm "
        "(%s) is worth %d in the server's own table" % (spent, realm, table[realm]))
    # ...and the echoed configuration is the server's, not the query string's.
    assert body["player_strike_cap"] == daily_boss.PLAYER_STRIKE_CAP, body
    assert body["bp_xp_per_strike"] == daily_boss.BP_XP_PER_STRIKE, body
    assert body["boss"]["reward_legacy_points"] == \
        daily_boss.BOSS["reward_legacy_points"], body


def test_a_forged_query_string_does_not_move_the_read_reply_either(client, player):
    """The READ route has the same guarantee: query fields are not settings."""
    h = _headers(player)
    clean = client.get("/daily-boss/%s" % player, headers=h).json()
    forged = client.get("/daily-boss/%s?realm=immortal&player_strike_cap=99999"
                        "&remaining_pod=999999&bp_xp_per_strike=999999999"
                        "&enabled=false" % player, headers=h).json()
    for key in ("player_strike_cap", "bp_xp_per_strike", "remaining_pod"):
        assert forged[key] == clean[key], (
            "the query string changed the read reply's %r from %r to %r"
            % (key, clean[key], forged[key]))
    assert forged["enabled"] is True, forged
    assert set(forged) == VIEW_KEYS, sorted(forged)


def test_an_unparseable_body_is_refused_closed_rather_than_500_or_200(client, player):
    """A body the guard cannot read is 403 -- not a 500, and not a free press.

    MEASURED: a ``text/plain`` body answers 403 ``request body could not be
    verified for ownership``. The guard refuses to guess, which is the right
    default for a payout route: a client with the wrong content type must not
    silently get a press it did not earn.

    The second half is the part that matters: the refusal costs the caller no
    press and the shared pod no points.
    """
    h = dict(_headers(player))
    h["Content-Type"] = "text/plain"
    r = client.post("/daily-boss/%s/strike" % player, headers=h,
                    content=b'{"damage": 999999}')
    assert r.status_code == 403, ("an unreadable body answered %d; it must be "
                                  "403, never a 500 and never a paid press"
                                  % r.status_code)
    assert r.json()["detail"] == "request body could not be verified for ownership"

    after = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    assert after["player_strikes_today"] == 0, (
        "a refused unreadable body still cost the caller a press: %r" % after)
    assert after["remaining_pod"] == daily_boss.POD_STRIKES, (
        "a refused unreadable body still damaged the shared pod: %r" % after)


def test_a_json_body_that_is_not_an_object_is_answered_as_an_ordinary_press(
        client, player):
    """``[1, 2, 3]`` is a valid JSON document, so it is read and found empty.

    MEASURED: 200 with the ordinary paying reply. Pinned because it is the arm
    a strict ``dict`` check in the guard would turn into a 422 or 500 -- and the
    caller should get the same press a caller with no body gets.
    """
    r = client.post("/daily-boss/%s/strike" % player, headers=_headers(player),
                    json=[1, 2, 3])
    assert r.status_code == 200, r.text
    assert r.json()["reason"] in ("strike_recorded", "boss_cleared"), r.text


# ===========================================================================
# 11. A REALM THE SERVER DOES NOT KNOW
# ===========================================================================

def test_an_unknown_or_unreadable_realm_is_priced_at_the_mortal_floor():
    """A realm the table does not name costs MORTAL points -- not zero, not immortal.

    MEASURED at the module level for three shapes the server can actually be
    handed by a corrupt or older save: an unknown realm STRING, a non-string
    realm, and a null realm. All three spend the mortal figure.

    The floor is the whole contract. Zero would let a corrupt save make a press
    free of consequence; falling back to the TOP of the table would let a forged
    realm value in a save buy a third of the day.
    """
    floor = daily_boss._STRIKE_POINTS["mortal"]
    top = max(daily_boss._STRIKE_POINTS.values())
    assert floor < top, "the floor and the top are the same number; this proves nothing"

    for label, realm in (("unknown string", "god_emperor_tyrant"),
                         ("non-string", 7),
                         ("null", None)):
        daily_boss._SHARED.clear()
        daily_boss._SHARED.update(daily_boss._fresh_shared())
        daily_boss._roll_day(T0)
        body = daily_boss.strike({"cultivation_realm": realm}, T0)
        spent = daily_boss.POD_STRIKES - body["remaining_pod"]
        assert spent == floor, (
            "a %s realm spent %d points; the floor is %d. Zero means a corrupt "
            "save makes a press free; the top (%d) means a forged realm buys a "
            "third of the day." % (label, spent, floor, top))
        assert body["reason"] == "strike_recorded", body
        # The reply does not invent an entry for a realm it does not know.
        assert realm not in body["strike_points_by_realm"], body


def test_a_realm_the_server_does_not_know_is_not_echoed_as_if_it_did(client, player):
    """The realm damage table names only realms the server can price.

    A client drawing damage text from this table must not find an entry it
    cannot pay for, so the reply's table and the server's own are the same set.
    """
    body = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    assert set(body["strike_points_by_realm"]) == set(daily_boss._STRIKE_POINTS), (
        "the reply's realm table and the server's own have drifted: %r vs %r"
        % (sorted(body["strike_points_by_realm"]), sorted(daily_boss._STRIKE_POINTS)))


# ===========================================================================
# 12. A SAVE THAT IS ALREADY OVER THE CAP
# ===========================================================================
#
# The corrupt-save sibling file covers REPAIR. What is pinned here is the WIRE
# consequence of a repaired record that repairs to "over the cap": the player is
# refused, truthfully, and the counter is clamped rather than left at whatever
# the save said -- because that number is persisted into their record for good.

def _seed_daily_boss_record(player: str, record) -> None:
    """Write a saved ``daily_boss`` record the way a real save would land.

    MEASURED: ``players_db`` is COPY-ON-READ -- ``players_db[pid]`` hands back a
    FRESH dict each time, so assigning into that object and moving on writes
    nothing at all (a probe read the marker straight back as absent). The store's
    own ``persist`` is the only way a seed reaches the record the route will read.

    Without this, a test that seeds a corrupt save looks like it passed for the
    wrong reason: the press it then drives hits a PRISTINE record, so the repair
    path under test never ran.
    """
    rec = main.players_db[player]
    rec["daily_boss"] = record
    main.players_db.persist(player, rec)


def test_a_saved_counter_far_past_the_cap_is_refused_and_clamped(client, player):
    """A save claiming 99 presses today gets ``daily_strike_cap_reached``.

    MEASURED through the real route after writing the record directly. The reply
    says CAP + 1, not 99: the module clamps on the way through, so a
    hand-edited or corrupted save cannot leave a player carrying a four-digit
    press count into every future day.
    """
    cap = main.DAILY_BOSS_BP_STRIKE_CAP
    # The route reads the real clock, so the day key must be TODAY's or the
    # module treats the record as belonging to a previous day and resets it.
    import time as _time
    _seed_daily_boss_record(player, {
        "day": daily_boss._day_index(_time.time()),
        "strikes_today": 99, "bp_xp_reserved": None, "bp_xp_spent_today": 0,
    })
    assert main.players_db[player]["daily_boss"]["strikes_today"] == 99, (
        "the seed did not reach the store; this test would pass against a "
        "pristine record and prove nothing about repair")

    r = client.post("/daily-boss/%s/strike" % player, headers=_headers(player))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "daily_strike_cap_reached", body
    assert body["no_payout_reason"] == "daily_strike_cap_reached", body
    assert body["player_strikes_today"] == cap + 1, (
        "the repaired counter is %r, not cap+1 (%d): a save claiming 99 presses "
        "left its number in the player's record for good"
        % (body["player_strikes_today"], cap + 1))
    assert body["battle_pass"]["xp_gained"] == 0, body


def test_a_non_dict_daily_boss_record_does_not_turn_a_press_into_a_500(client, player):
    """A ``daily_boss`` key holding a string is repaired, and the press is paid.

    MEASURED. Every field on the hot path is read with ``[]``, so a record whose
    shape is wrong used to raise KeyError and answer HTTP 500 on an ordinary
    press. The caller should get a press, and the record should come back a dict.
    """
    _seed_daily_boss_record(player, "not-a-dict-at-all")
    assert main.players_db[player]["daily_boss"] == "not-a-dict-at-all", (
        "the corrupt seed did not reach the store; this test would have driven "
        "a PRISTINE record and proved nothing about the repair path")
    r = client.post("/daily-boss/%s/strike" % player, headers=_headers(player))
    assert r.status_code == 200, ("a corrupt saved record answered %d; a plain "
                                  "press must never 500" % r.status_code)
    body = r.json()
    assert body["reason"] == "strike_recorded", body
    repaired = main.players_db[player]["daily_boss"]
    assert isinstance(repaired, dict), repaired
    assert repaired["strikes_today"] == 1, repaired
    assert repaired["bp_xp_reserved"] == daily_boss.PLAYER_BP_XP_SHARE, repaired


# ===========================================================================
# 13. THE RESERVATION IS DRAWN ONCE
# ===========================================================================

def test_a_second_press_draws_nothing_further_from_the_shared_budget():
    """The shared pool is debited on press ONE only.

    MEASURED: the first press moves ``bp_xp_remaining`` down by the player's
    whole share; the second press moves it by exactly nothing. This is the
    mechanic the whole griefing fix rests on -- the share is DRAWN once and is
    then the player's own -- so it is pinned directly rather than only through
    its visible effect on a third party's allowance.
    """
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(T0)
    before = daily_boss._SHARED["bp_xp_remaining"]

    player = {"cultivation_realm": "mortal"}
    daily_boss.strike(player, T0)
    after_first = daily_boss._SHARED["bp_xp_remaining"]
    daily_boss.strike(player, T0)
    after_second = daily_boss._SHARED["bp_xp_remaining"]

    assert before - after_first == daily_boss.PLAYER_BP_XP_SHARE, (
        "the first press drew %d, not the player's whole share (%d)"
        % (before - after_first, daily_boss.PLAYER_BP_XP_SHARE))
    assert after_first - after_second == 0, (
        "the second press drew %d more from the shared pool; a reservation is "
        "drawn ONCE, so a second press on the same day must cost the server "
        "nothing" % (after_first - after_second))
    assert after_second == before - daily_boss.PLAYER_BP_XP_SHARE, (
        "the shared pool drifted by a second share over two presses: %d -> %d"
        % (before, after_second))


def test_viewing_the_boss_spends_nothing_at_all():
    """A GET is not a press: the shared state is byte-identical across a read.

    Cheap to assert and it closes a real hole -- a poll loop that re-read the
    boss every second would otherwise be an undetected drain on the budget.
    """
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(T0)
    player = {"cultivation_realm": "mortal"}
    daily_boss.strike(player, T0)          # get off the pristine state first

    before = dict(daily_boss._SHARED)
    for _ in range(5):
        daily_boss.view(player, T0)
    assert dict(daily_boss._SHARED) == before, (
        "reading the boss changed shared server state: %r -> %r"
        % (before, dict(daily_boss._SHARED)))


# ===========================================================================
# 14. THE CYCLE CLOCK AT ITS EXACT BOUNDARIES
# ===========================================================================

def test_the_tribulation_cycle_day_is_exact_at_both_edges_of_the_cycle():
    """First instant of the cycle is 1; the last is 21; the next cycle is 1.

    The stride test in section 7 can step over a boundary; these four instants
    cannot be stepped over, so an off-by-one at either edge is caught here.
    """
    span = daily_boss.TRIBULATION_CYCLE_DAYS * daily_boss.DAY
    assert daily_boss.tribulation_cycle_day(0) == 1
    assert daily_boss.tribulation_cycle_day(span - 1) == daily_boss.TRIBULATION_CYCLE_DAYS
    assert daily_boss.tribulation_cycle_day(span) == 1
    assert daily_boss.tribulation_cycle_day(2 * span - 1) == daily_boss.TRIBULATION_CYCLE_DAYS
    # The cycle WRAPS: the last day is followed by the first of the next cycle,
    # not by a 22nd day. That is the property a client calendar depends on --
    # drawing a day past the end of the cycle would be an out-of-range label.
    assert daily_boss.tribulation_cycle_day(span - 1) == daily_boss.TRIBULATION_CYCLE_DAYS
    assert daily_boss.tribulation_cycle_day(span) == 1
    # ...and no instant in the cycle ever yields 0 or 22.
    for probe in (0, span - 1, span, 2 * span, span // 2, span // 2 + span):
        day = daily_boss.tribulation_cycle_day(probe)
        assert 1 <= day <= daily_boss.TRIBULATION_CYCLE_DAYS, (probe, day)


# ===========================================================================
# 15. THE REWARD IS A FLAT RATE, AND THE DAMAGE IS NOT
# ===========================================================================
#
# WHAT THIS SECTION CORRECTS, MEASURED 2026-10-05
#
# The task description for this file said the shared pool pays "proportional
# rewards". Read as a claim about the MONEY, that is not what the module does,
# and pinning it as written would have frozen a false contract:
#
#     realm          damage dealt   battle-pass XP earned
#     mortal               50                 50
#     core                200                 50
#     immortal          1600                 50
#
# A 32x spread in damage buys a 1x spread in XP. The battle-pass rate is FLAT
# per press (``BP_XP_PER_STRIKE``) and does NOT scale with the realm's damage.
# What IS proportional is the damage, and the RESERVATION -- every realm
# reserves the same whole day, so the share is also flat.
#
# This is pinned as the shipped contract rather than restated as the brief
# describes it, because a client drawing a payout estimate from its own realm
# would over-predict an immortal's press by 32x. The tests below therefore pin
# the FLATNESS explicitly -- the flat rate is the load-bearing fact, and a
# future change that made XP scale with damage would change the economy.

def _press_once(realm: str, now: float = T0) -> tuple:
    """One press on a fresh day. Returns (damage, xp_spent, reserved).

    Resets the shared pod first, because a press inherits whatever the pod had
    left -- a test that ran second would otherwise measure a refusal.
    """
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(now)
    player = {"cultivation_realm": realm}
    reply = daily_boss.strike(player, now)
    assert reply["reason"] == "strike_recorded", (
        "%s did not get a paid press on a live pod: %r" % (realm, reply))
    state = player["daily_boss"]
    return (daily_boss.POD_STRIKES - reply["remaining_pod"],
            state["bp_xp_spent_today"],
            state["bp_xp_reserved"])


def test_the_reward_is_a_flat_rate_and_does_not_scale_with_damage():
    """The battle-pass rate is per PRESS, not per point of damage.

    This is the honest reading of the economy, measured across the WHOLE realm
    table rather than two hand-picked realms: damage rises 32x from mortal to
    immortal while the experience earned stays identical. If this ever stops
    being true the economy changed, and that must be a deliberate edit here
    rather than a silent drift.
    """
    measured = {realm: _press_once(realm) for realm in daily_boss._STRIKE_POINTS}
    damages = {realm: row[0] for realm, row in measured.items()}
    xp = {realm: row[1] for realm, row in measured.items()}

    # Damage really does vary -- otherwise "flat reward" proves nothing.
    assert max(damages.values()) > min(damages.values()), (
        "every realm dealt identical damage; this test would pass whether the "
        "reward scaled or not: %r" % (damages,))
    # ...and the reward does NOT follow it.
    assert set(xp.values()) == {daily_boss.BP_XP_PER_STRIKE}, (
        "the battle-pass rate is no longer flat: %r. A client estimating a "
        "payout from its own realm assumes a flat per-press rate; scaling it "
        "with damage silently over-predicts a high realm's press by the full "
        "damage ratio." % (xp,))
    # The strongest press earns the same as the weakest despite dealing 32x.
    assert xp["immortal"] == xp["mortal"] == daily_boss.BP_XP_PER_STRIKE
    assert damages["immortal"] >= 32 * damages["mortal"], (
        "the damage ratio this test relies on has changed: %r vs %r -- re-measure "
        "before reading the flat-rate claim as still load-bearing"
        % (damages["immortal"], damages["mortal"]))


def test_the_reservation_is_flat_across_realms_too():
    """A player reserves a whole DAY, whatever their realm.

    The other half of "proportional". Damage is proportional to realm; the
    day's reservation is not, so a high realm does not reserve more of the
    shared pool than a mortal does. This is what stops one strong player from
    drawing down the budget faster than the designed population allows.
    """
    reserved = {realm: _press_once(realm)[2]
                for realm in daily_boss._STRIKE_POINTS}
    assert set(reserved.values()) == {daily_boss.PLAYER_BP_XP_SHARE}, (
        "the reservation now varies by realm: %r. The shared budget is sized in "
        "WHOLE PLAYER-DAYS, so a realm-scaled share silently changes how many "
        "players a day's budget actually serves." % (reserved,))


def test_the_reply_reports_the_same_rate_a_client_should_plan_against(client, player):
    """The WIRE says the rate is flat, so a client cannot mis-plan on its realm.

    The read reply carries the rate the route will pay. A client computing
    "my realm deals 1600, so I earn 1600-ish" is wrong, and the only thing that
    tells it so is this key. Pinned through the real route because the number
    a client reads must be the number the route uses.
    """
    body = client.get("/daily-boss/%s" % player, headers=_headers(player)).json()
    assert body["bp_xp_per_strike"] == daily_boss.BP_XP_PER_STRIKE, body
    # The realm table is published alongside it -- a client CAN see its own
    # damage figure -- so the two keys together are the whole contract: damage
    # varies by realm, the rate does not.
    assert body["strike_points_by_realm"], body
    assert set(body["strike_points_by_realm"]) == set(daily_boss._STRIKE_POINTS)


def test_the_threshold_is_applied_before_any_money_moves_at_the_boundary():
    """ORDERING: the gate is reached BEFORE the budget is drawn.

    The gate is not merely checked eventually -- it is checked before
    ``_reserve_bp_share`` runs at all. Proved here by driving the two sides of
    the same gate with the SAME realm and the SAME fresh day, so the only
    difference is what the press was worth:

      * at exactly ``MIN_CONTRIBUTION_POINTS``  -> paid, share reserved, XP spent
      * one point below it                     -> refused, NOTHING reserved

    If the ordering were inverted (reserve first, gate second), the below-gate
    arm would still end with ``granted_legacy_points == 0`` and would pass a
    test that only checked the payout. That is why the pool balance itself is
    asserted here and not just the reason string.
    """
    gate = daily_boss.MIN_CONTRIBUTION_POINTS

    # --- at the gate: paid, and the pool moves -----------------------------
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(T0)
    daily_boss._STRIKE_POINTS["dbc_at_gate"] = gate          # exactly at
    pool_before = daily_boss._SHARED["bp_xp_remaining"]
    at_gate = {"cultivation_realm": "dbc_at_gate"}
    paid = daily_boss.strike(at_gate, T0)
    assert paid["reason"] == "strike_recorded", (
        "a press worth exactly the threshold was refused; the gate is `<`, so "
        "exactly the minimum must be paid: %r" % (paid,))
    assert daily_boss._SHARED["bp_xp_remaining"] == \
        pool_before - daily_boss.PLAYER_BP_XP_SHARE, (
        "a press at the gate did not draw its share; the positive arm of this "
        "test is not doing its job")
    assert at_gate["daily_boss"]["bp_xp_spent_today"] == daily_boss.BP_XP_PER_STRIKE

    # --- one point below the gate: refused, and the pool does NOT move -----
    daily_boss._SHARED.clear()
    daily_boss._SHARED.update(daily_boss._fresh_shared())
    daily_boss._roll_day(T0)
    daily_boss._STRIKE_POINTS["dbc_below_gate"] = gate - 1    # just below
    pool_before = daily_boss._SHARED["bp_xp_remaining"]
    below = {"cultivation_realm": "dbc_below_gate"}
    refused = daily_boss.strike(below, T0)
    assert refused["reason"] == "no_contribution_shared_boss", refused
    assert refused["no_payout_reason"] == "no_contribution_shared_boss", refused
    # THE ORDERING ASSERTION. The pool is byte-identical, which is only
    # possible if the gate fired before the reservation was drawn.
    assert daily_boss._SHARED["bp_xp_remaining"] == pool_before, (
        "a press one point below the threshold still drew %r out of the shared "
        "pool. The gate is being applied AFTER the money moves, so a "
        "zero-contribution press is misattributed the budget anyway -- which "
        "is the exact drain the threshold exists to stop."
        % (pool_before - daily_boss._SHARED["bp_xp_remaining"],))
    assert below["daily_boss"]["bp_xp_reserved"] is None, (
        "a below-threshold press recorded a reservation: %r"
        % (below["daily_boss"],))
    assert below["daily_boss"]["bp_xp_spent_today"] == 0
    assert below["daily_boss"].get("pod_points_contributed_today", 0) == 0, (
        "a below-threshold press was credited with pod damage it never dealt")

    # Restore the module's own table: these probe realms must not survive into
    # a later file that reads the published realm set.
    daily_boss._STRIKE_POINTS.pop("dbc_at_gate", None)
    daily_boss._STRIKE_POINTS.pop("dbc_below_gate", None)
    assert set(daily_boss._STRIKE_POINTS) == {
        "mortal", "foundation", "core", "nascent_soul", "dao_seeking",
        "immortal"}, sorted(daily_boss._STRIKE_POINTS)


def test_a_zero_contribution_press_costs_the_player_and_the_pool_nothing():
    """Requirements 1 and 3 together, on the REAL shipped pod state.

    The other zero-contribution files reach this by draining the pod with a
    fleet of accounts. This reaches it the cheap way -- the pod is put where a
    clear leaves it -- and checks the whole bill at once, on the player's own
    saved record rather than on the reply:

      * the press costs zero experience
      * the press reserves zero out of the shared pool
      * the press attributes zero pod damage
      * the press is still RECORDED, so it cannot be replayed for free
    """
    _force_pod_empty()
    player = {"cultivation_realm": "immortal"}   # the strongest realm: the
    # cheapest possible excuse for a refusal, so "the realm was too weak"
    # cannot explain it.
    pool_before = daily_boss._SHARED["bp_xp_remaining"]

    body = daily_boss.strike(player, T0)
    assert body["reason"] == "no_contribution_shared_boss", body
    assert body["granted_legacy_points"] == 0, body
    assert daily_boss._SHARED["bp_xp_remaining"] == pool_before
    state = player["daily_boss"]
    assert state["bp_xp_reserved"] is None, state
    assert state["bp_xp_spent_today"] == 0, state
    assert state.get("pod_points_contributed_today", 0) == 0, state
    assert player.get("legacy_bonus") is None, player
    # Still counted as a press -- a refusal the caller could re-run forever
    # would be a free farm of "presses" that each cost nothing but let them
    # probe state.
    assert state["strikes_today"] >= daily_boss.PLAYER_STRIKE_CAP + 1, (
        "the refusal left the counter at %r, under the cap" % (state,))


def test_a_real_contributor_is_paid_the_same_flat_rate_the_wire_advertises():
    """The positive control for every zero-contribution assertion above.

    If the module refused everybody, sections 15 and 6 would all be green and
    the boss would pay nobody. This drives one genuine press on a live pod and
    asserts it is paid, at exactly the rate the read reply advertised.
    """
    player = {"cultivation_realm": "immortal"}
    advertised = daily_boss.view(player, T0)["bp_xp_per_strike"]
    body = daily_boss.strike(player, T0)
    assert body["reason"] == "strike_recorded", (
        "a contributing press on a live pod was refused; every zero-payout "
        "assertion in this file would then pass for the wrong reason: %r"
        % (body,))
    assert "no_payout_reason" not in body, body
    assert body["granted_legacy_points"] == 0, body
    assert player["daily_boss"]["bp_xp_spent_today"] == advertised, (
        "a real contributor was paid %r, not the %r the reply advertised"
        % (player["daily_boss"]["bp_xp_spent_today"], advertised))
    # And the damage really landed, or "contributed" means nothing.
    assert player["daily_boss"]["pod_points_contributed_today"] == \
        daily_boss._STRIKE_POINTS["immortal"], player["daily_boss"]
    assert daily_boss._STRIKE_POINTS["immortal"] >= daily_boss.MIN_CONTRIBUTION_POINTS
