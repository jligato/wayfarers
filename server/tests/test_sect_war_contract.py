"""
CONTRACT TEST: server/sect_war.py -- the six routes it registers, and the
pure settlement layer underneath them.

WHAT THIS FILE PINS
-------------------
``sect_war.register_sect_war_routes`` is the ONE registrar for the war surface
that the battle view polls and that pays real stones. This file freezes, as
measured over HTTP and as measured by calling the pure functions directly:

* the SIX (path, method) pairs this module mounts, compared as a set;
* the exact reply KEY SET of every success shape, including the two shapes that
  differ from their sibling route (no-war, settled-preview);
* that every route demands a session BY HAND, and that the four state-changing
  POSTs demand an idempotency key while the two reads do not;
* every refusal, with its status code AND its ``detail`` string;
* the payout table: the floor, the per-side role derivation, the caps, and the
  once-only receipt -- the properties that decide whether a war can mint stones;
* the settlement job's once-only war lease and once-only payout receipt, and the
  dry run that must write nothing.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
``server/sect_war_routes.py`` adds two reads (the leaderboard and the banner
history) on top of these six; those are covered by
``test_sect_war_routes_contract.py``. This file is about ``sect_war.py``.

WHY IT BUILDS ITS OWN APP
-------------------------
``server/main.py`` is 3000+ lines and several agents edit it at once, so a suite
that imports it measures whatever a peer is mid-write. This mounts
``register_sect_war_routes`` onto a bare ``FastAPI()`` with the REAL
``SectStore``, the REAL ``PlayerStore``/``WalletStore`` and the REAL
``auth.require_player``, so a defect in a route body is still caught and the
session check under test is the shipped one.

``TestClient(raise_server_exceptions=False)`` is required: one route branch
raises, and a raising client turns that 500 into a traceback rather than a
status code.

Run
---
    cd G:/Projects/wayfarers-cycle
    tools/wc_pytest.sh server/tests/test_sect_war_contract.py
"""
from __future__ import annotations

import copy as _copy
import json as _json
import math
import os
import pathlib
import sys
import uuid
from typing import Any, Dict, Iterator, Set

_THIS = pathlib.Path(__file__).resolve()
SERVER_DIR = _THIS.parents[1]
REPO_ROOT = _THIS.parents[2]
for _p in (str(REPO_ROOT), str(SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Force memory mode. Without the scrub a single-file run tries to reach a
# PostgreSQL server that is not there, and the wall of errors reads like a
# product defect that does not exist.
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _pg in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_pg, None)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # bare, NOT server.auth -- main.py binds the same module object
import sect_war as sw
import wallet_dispatch
from player_store import PlayerStore
from sect import SectStore
from wallet_dispatch import InMemoryWalletStore

# --------------------------------------------------------------------------
# The declared surface, as a SET of (path, method) pairs.
#
# Compared as a set, never as a count: a registrar that dropped one route and
# mounted another under the same prefix satisfies a count check. FastAPI adds
# ``HEAD`` beside every ``GET``, so the GET arms carry both methods.
# --------------------------------------------------------------------------
DECLARED: Set[tuple] = {
    ("/sect-war/declare", "POST"),
    ("/sect-war/current", "GET"),
    ("/sect-war/{war_id}", "GET"),
    ("/sect-war/{war_id}/battle", "POST"),
    ("/sect-war/{war_id}/preview", "POST"),
    ("/sect-war/{war_id}/contribute", "POST"),
}

#: The POSTs that move state or price it, and therefore refuse a request with
#: no ``Idempotency-Key``. The two GET reads do not.
IDEMPOTENCY_REQUIRED = ("declare", "/battle", "/preview", "/contribute")

#: The JSON body each state-changing route needs, so a request that is missing
#: ONLY the idempotency key still reaches the handler instead of being turned
#: away by body validation first. MEASURED on this tree: without a body,
#: `/battle` and `/contribute` answer 422 (FastAPI refuses the missing body
#: before the handler runs), and `/preview` -- the one route that declares no
#: body -- reached the gate and answered 400. A 422 is not evidence that the
#: key is required.
#:
#: Every body here is one that WOULD SUCCEED with the key present, which is the
#: load-bearing part. A body that is refused for some other reason (a
#: contribution type that is not paid out, a second war between the same two
#: sects) answers 400 too, and this assertion accepts any 400 -- so a slack body
#: makes the arm green even with the gate deleted. MEASURED: neutering
#: `idem_gate.require_idempotency_key` left the `/contribute` arm GREEN until
#: the body was changed to a real `funds` contribution.
IDEMPOTENCY_BODY = {
    "/battle": {"war_id": "ignored-by-this-arm"},
    "/preview": None,          # declares no request model
    "/contribute": {"war_id": "ignored-by-this-arm",
                    "contribution_type": "funds", "value": 1},
}

#: Exact reply key sets, MEASURED. A client reads these names, so adding one is
#: safe but renaming or dropping one breaks every caller.
DECLARE_KEYS = {"ok", "war_id", "battle"}
BATTLE_KEYS = {"ok", "battle", "preview"}
CONTRIBUTE_KEYS = {
    "ok", "war_id", "stones_spent", "your_paid", "attacker_score",
    "defender_score", "your_side",
}
BATTLE_SIM_KEYS = {
    "attacker_score", "defender_score", "attacker_share", "rounds",
    "predicted_winner", "lead_changes", "contested", "decided_early",
}
#: ``war_view`` FLATTENS when a war exists: no ``war`` key, no ``ok`` key.
VIEW_KEYS = {
    "war_id", "phase", "attacker_sect_id", "defender_sect_id", "attacker_score",
    "defender_score", "winner", "your_sect_id", "your_side", "your_paid",
    "server_now", "battle",
}
CURRENT_NO_WAR_KEYS = {"ok", "war", "server_now"}
PREVIEW_KEYS = {
    "ok", "settled", "war_id", "phase", "predicted_role", "attacker_score",
    "defender_score", "currency", "min_stake_for_bonus", "daily_cap",
    "season_cap", "total_payout", "payout_count", "payouts", "server_now",
}
#: A settled war short-circuits the preview body. Six keys fewer.
PREVIEW_SETTLED_KEYS = {"ok", "settled", "war_id", "winner", "payouts", "note"}

#: One refusal string, shared by every member-scoped war route, deliberately
#: identical for "no such war" and "not your war" so neither is an oracle.
NOT_A_MEMBER = "Not a member of either warring sect"

#: A day far enough out that no clock in the process can reach it, for the
#: settlement-job tests. Written as arithmetic so the boundary under it is
#: legible instead of a magic constant.
DAY = 86_400 * 20_000
AFTER_BOUNDARY = DAY + (sw.SETTLE_HOUR_UTC + 1) * 3600
BEFORE_BOUNDARY = DAY + (sw.SETTLE_HOUR_UTC - 1) * 3600


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

class CreditWallet(InMemoryWalletStore):
    """The real in-memory wallet, plus the ``earn`` VERB as a method.

    ``sect_war._credit`` calls ``wallet_store.earn(player, currency, amount, key,
    reward_type=...)`` -- a METHOD. Neither shipped wallet store defines one:
    ``wallet_defense`` and ``wallet_store_pg`` both expose ``earn`` as a MODULE
    function reached through ``wallet_dispatch``. On the shipped stores the
    credit raises ``AttributeError`` and every settlement reports ``failed: 1``
    while moving nothing.

    Rather than let that silently gut every payout assertion below, this
    subclass forwards to the real ``wallet_dispatch.earn`` -- same ledger, same
    idempotency key handling, one line of adapter -- so the rest of the file
    measures sect_war's OWN arithmetic. The gap itself is pinned by
    ``test_the_shipped_wallet_stores_expose_no_earn_method``.
    """

    def earn(self, player_id: str, currency: str, amount: int, key: str,
             reward_type: str = "") -> Dict[str, Any]:
        return wallet_dispatch.earn(self, player_id, currency, amount, key,
                                    reward_type=reward_type)


def _new_player(world: Dict[str, Any], tag: str,
                stones: int = 50_000) -> Dict[str, Any]:
    """One signed-in player with a known purse.

    The row is written THROUGH ``persist``: ``PlayerStore.__getitem__`` hands out
    a copy, so an assignment that skips it lands on a throwaway.
    """
    pid = "sectwarc-%s-%s" % (tag, uuid.uuid4().hex[:10])
    row = {"id": pid, "name": pid}
    world["players_db"][pid] = row
    world["players_db"].persist(pid, row)
    if stones:
        wallet_dispatch.earn(world["wallet"], pid, "spirit_stone", stones,
                             "seed:" + pid)
    return {"player_id": pid,
            "headers": {"Authorization": "Bearer " + auth.issue_session(pid)}}


def _found_sect(world: Dict[str, Any], session: Dict[str, Any],
                name: str) -> str:
    """Create a sect whose founder is a patriarch (the role the store writes)."""
    ok, sect_id = world["sect_store"].create_sect(
        session["player_id"], "%s-%s" % (name, uuid.uuid4().hex[:8]))
    assert ok, sect_id
    return sect_id


def _key() -> str:
    return "sectwarc-%s" % uuid.uuid4().hex[:12]


def _declare(world: Dict[str, Any], attacker: Dict[str, Any],
             defender_sect_id: str) -> Any:
    return world["client"].post("/sect-war/declare",
                                json={"defender_sect_id": defender_sect_id},
                                headers={**attacker["headers"],
                                         "Idempotency-Key": _key()})


def _make_war(world: Dict[str, Any]) -> Dict[str, Any]:
    """ONE live war, declared through the shipped route.

    Exactly one war for the attacker: ``/sect-war/current`` answers with the most
    RECENTLY STARTED war the caller is involved in, so a second war would make
    every assertion about ``current`` fail for the wrong reason.
    """
    atk = _new_player(world, "atk")
    dfn = _new_player(world, "def")
    out = _new_player(world, "out")
    atk_sect = _found_sect(world, atk, "Atk")
    def_sect = _found_sect(world, dfn, "Def")
    made = _declare(world, atk, def_sect)
    assert made.status_code == 200, made.text
    return {"atk": atk, "def": dfn, "out": out, "atk_sect": atk_sect,
            "def_sect": def_sect, "war": made.json()["war_id"]}


def _another_sect(world: Dict[str, Any], tag: str = "spare") -> Dict[str, Any]:
    """A fresh sect, not yet at war.

    ``SectStore`` refuses a second war between the same two sects, so any test
    that needs a second war needs a third sect.
    """
    member = _new_player(world, tag)
    return {"player_id": member["player_id"], "headers": member["headers"],
            "sect_id": _found_sect(world, member, tag)}


def _contribute(world: Dict[str, Any], session: Dict[str, Any], war_id: str,
                value: int, ctype: str = "funds") -> Any:
    return world["client"].post(
        "/sect-war/%s/contribute" % war_id,
        json={"war_id": war_id, "contribution_type": ctype, "value": value},
        headers={**session["headers"], "Idempotency-Key": _key()})


def _stones(world: Dict[str, Any], player_id: str) -> int:
    """The player's spendable low-grade spirit stones.

    ``Wallet.balance`` is a per-currency object, not a number; reading it as one
    raises, so the currency is named here rather than at every call site.
    """
    wallet = world["wallet"].get(player_id)
    return 0 if wallet is None else int(wallet.balance.spirit_stone)


def _fund(world: Dict[str, Any], war: Dict[str, Any], *pairs) -> None:
    """Fund the war over HTTP, through the shipped route, and assert it landed.

    Every amount is checked against ``sect_war.contributed_to``, the same reader
    the settlement uses, so a player who paid nothing cannot masquerade as a
    contributor -- the property the whole payout rests on.
    """
    world["sect_store"].wars[war["war"]].phase = "active"
    for session, amount in pairs:
        r = _contribute(world, session, war["war"], amount)
        assert r.status_code == 200, "contribute refused: %s %s" % (
            r.status_code, r.text[:200])
    for session, amount in pairs:
        recorded = sw.contributed_to(world["sect_store"], war["war"],
                                     session["player_id"])
        assert recorded >= amount, (
            "%s paid %d but only %d is on record" % (
                session["player_id"], amount, recorded))


def _funded_war(world: Dict[str, Any]) -> Dict[str, Any]:
    """A war with one winning payer and one losing payer, plus a stranger."""
    war = _make_war(world)
    _fund(world, war, (war["atk"], 3000), (war["def"], 700))
    return war


def _detail(response) -> str:
    try:
        detail = response.json()["detail"]
    except Exception:
        return ""
    return detail if isinstance(detail, str) else _json.dumps(detail,
                                                             sort_keys=True)


def assert_keys(response, expected: Set[str], *, where: str) -> Dict[str, Any]:
    """Assert the reply carries EXACTLY ``expected``.

    An extra key is as much a break as a missing one: a client that starts
    reading a key nobody promised is reading a field nothing guarantees.
    """
    assert response.status_code == 200, "%s -> %s %s" % (
        where, response.status_code, response.text[:200])
    body = response.json()
    assert isinstance(body, dict), "%s returned %s, not an object" % (
        where, type(body).__name__)
    missing = sorted(expected - set(body))
    extra = sorted(set(body) - expected)
    assert not missing, "%s is MISSING %s; it carries %s" % (
        where, missing, sorted(body))
    assert not extra, "%s gained UNPROMISED keys %s" % (where, extra)
    return body


@pytest.fixture
def world() -> Iterator[Dict[str, Any]]:
    """A mounted app, a fresh store, a fresh wallet, one per test.

    Function-scoped on purpose: ``sect_war`` keeps PROCESS-WIDEL ledgers (the
    paid-stake map, the claimed-war set, the settled-day memo, the payout
    receipts). War ids and player ids are minted per test, so no row can
    collide, and ``reset_due_memo(clear_ledger=True)`` empties all four
    afterwards regardless.
    """
    sw.reset_due_memo(clear_ledger=True)
    wallet = CreditWallet()
    players_db = PlayerStore(wallet_store=wallet)
    sect_store = SectStore()
    app = FastAPI()
    sw.register_sect_war_routes(app, lambda: players_db, sect_store)
    world: Dict[str, Any] = {
        "app": app,
        "client": TestClient(app, raise_server_exceptions=False),
        "players_db": players_db,
        "wallet": wallet,
        "sect_store": sect_store,
    }
    try:
        yield world
    finally:
        sw.reset_due_memo(clear_ledger=True)


@pytest.fixture
def war(world: Dict[str, Any]) -> Dict[str, Any]:
    return _make_war(world)


# ==========================================================================
# 1. THE MOUNT -- six paths, and the session dependency, declared by hand
# ==========================================================================

def test_register_mounts_exactly_the_six_declared_paths(world: Dict[str, Any]):
    live = set()
    for route in world["app"].routes:
        path = getattr(route, "path", "")
        if not path.startswith("/sect-war"):
            continue
        for method in (getattr(route, "methods", set()) or set()):
            if method == "HEAD":
                continue
            live.add((path, method))
    assert live == DECLARED, "mounted %s, declared %s" % (
        sorted(live), sorted(DECLARED))


def test_every_route_declares_the_session_dependency_by_hand(
        world: Dict[str, Any]):
    """``Depends(require_player)`` written on the signature, not inferred.

    A shape-based guard cannot see a path that names no player, so the hand-wired
    dependency IS the protection. Asserting the NAME it is wired to (rather than
    merely that some dependency exists) is what catches a global guard being
    removed from these six.
    """
    for route in world["app"].routes:
        if not getattr(route, "path", "").startswith("/sect-war"):
            continue
        dependant = getattr(route, "dependant", None)
        assert dependant is not None, "%s has no dependant tree" % route.path
        wired = {getattr(getattr(d.call, "__name__", ""), "lower", lambda: "")()
                 for d in (dependant.dependencies or [])}
        assert "require_player" in wired, (
            "%s does not hand-wire require_player; it wires %s" % (
                route.path, sorted(wired)))


def test_the_two_reads_are_the_only_ones_that_take_no_body(
        world: Dict[str, Any]) -> None:
    """The GET arms declare no request model; the POST arms all do.

    A GET that grew a body would take a shape where the body is the only place a
    limit can travel -- worth pinning before it changes by accident.
    """
    for route in world["app"].routes:
        path = getattr(route, "path", "")
        if path not in ("/sect-war/current", "/sect-war/{war_id}"):
            continue
        assert route.body_field is None, "%s grew a request body" % path


@pytest.mark.parametrize("suffix", ["", "/battle", "/preview", "/contribute"])
def test_no_route_answers_without_a_session(world: Dict[str, Any],
                                            war: Dict[str, Any],
                                            suffix: str) -> None:
    """A missing session is 401 on every one of the six, reads included.

    The reads are guarded too: a war read tells a player their own paid stake,
    which is per-player state, so an anonymous poll is not a public read.
    """
    if suffix == "":
        r = world["client"].get("/sect-war/%s" % war["war"])
    else:
        r = world["client"].post("/sect-war/%s%s" % (war["war"], suffix),
                                 headers={"Idempotency-Key": _key()})
    assert r.status_code == 401, "anon %r -> %s %s" % (
        suffix, r.status_code, r.text[:160])


@pytest.mark.parametrize("suffix", ["", "/battle", "/preview", "/contribute"])
def test_a_bogus_token_is_refused(world: Dict[str, Any], war: Dict[str, Any],
                                  suffix: str) -> None:
    headers = {"Authorization": "Bearer not-a-real-session",
               "Idempotency-Key": _key()}
    if suffix == "":
        r = world["client"].get("/sect-war/%s" % war["war"], headers=headers)
    else:
        r = world["client"].post("/sect-war/%s%s" % (war["war"], suffix),
                                 headers=headers)
    assert r.status_code == 401, r.text[:200]


@pytest.mark.parametrize("suffix", IDEMPOTENCY_REQUIRED)
def test_every_state_changing_route_requires_an_idempotency_key(
        world: Dict[str, Any], war: Dict[str, Any], suffix: str) -> None:
    """No caller-supplied key, no state change: a missing key is REFUSED.

    The alternative -- minting a fallback key server-side -- makes a retry look
    like a new request, and the retry is charged again.

    Each arm targets something that WOULD otherwise succeed, so a 400 can only
    come from the missing key. `declare` names a spare sect because the `war`
    fixture already declared between `atk_sect` and `def_sect`, and the store
    refuses a second war between the same pair ("War already exists", 400) --
    which this assertion would otherwise have accepted as proof the gate fired.
    MEASURED: with the gate deleted, that arm stayed green until it named a
    fresh pair.
    """
    if suffix == "declare":
        spare = _another_sect(world)
        r = world["client"].post("/sect-war/declare",
                                 json={"defender_sect_id": spare["sect_id"]},
                                 headers=war["atk"]["headers"])
    else:
        body = IDEMPOTENCY_BODY[suffix]
        r = world["client"].post("/sect-war/%s%s" % (war["war"], suffix),
                                 json=body, headers=war["atk"]["headers"])
    assert r.status_code in (400, 409, 428), "%s answered %s" % (suffix,
                                                                r.status_code)


def test_the_two_reads_do_not_require_an_idempotency_key(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    assert world["client"].get("/sect-war/current",
                               headers=war["atk"]["headers"]).status_code == 200
    assert world["client"].get("/sect-war/%s" % war["war"],
                               headers=war["atk"]["headers"]).status_code == 200


def test_a_body_naming_another_player_is_refused(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The caller's identity wins over a ``player_id`` in the body.

    The body field would be IGNORED for the payout's sake -- a caller must not be
    able to price somebody else's war -- but the ownership check still fires,
    because a body that claims another player is an attempt, not a typo.
    """
    r = world["client"].post(
        "/sect-war/%s/contribute" % war["war"],
        json={"war_id": war["war"], "contribution_type": "funds", "value": 100,
              "player_id": war["out"]["player_id"]},
        headers={**war["atk"]["headers"], "Idempotency-Key": _key()})
    assert r.status_code == 403, r.text[:200]


# ==========================================================================
# 2. THE REPLY SHAPES -- measured key sets, including the two odd ones
# ==========================================================================

def test_declare_returns_the_war_id_and_a_simulated_battle(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    spare = _another_sect(world)
    body = assert_keys(_declare(world, war["atk"], spare["sect_id"]),
                       DECLARE_KEYS, where="declare")
    assert body["ok"] is True and isinstance(body["war_id"], str)
    assert set(body["battle"]) == BATTLE_SIM_KEYS
    assert body["battle"]["attacker_score"] == 0, (
        "a fresh war starts at zero: the simulation reads the STORED scores")


def test_current_flattens_the_view_when_a_war_exists(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    body = assert_keys(world["client"].get("/sect-war/current",
                                           headers=war["atk"]["headers"]),
                       VIEW_KEYS, where="current")
    assert body["war_id"] == war["war"]
    assert body["your_side"] == "attacker"
    assert body["your_sect_id"] == war["atk_sect"]


def test_current_carries_a_war_key_only_when_there_is_no_war(
        world: Dict[str, Any]) -> None:
    loner = _new_player(world, "loner")
    body = assert_keys(world["client"].get("/sect-war/current",
                                           headers=loner["headers"]),
                       CURRENT_NO_WAR_KEYS, where="current(no war)")
    assert body["ok"] is True and body["war"] is None
    assert isinstance(body["server_now"], (int, float))


def test_the_war_read_and_current_agree_key_for_key(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    one = assert_keys(world["client"].get("/sect-war/%s" % war["war"],
                                          headers=war["def"]["headers"]),
                      VIEW_KEYS, where="read")
    assert one["your_side"] == "defender"
    cur = world["client"].get("/sect-war/current",
                              headers=war["def"]["headers"]).json()
    for key in ("phase", "attacker_score", "defender_score", "your_side",
                "winner", "attacker_sect_id", "defender_sect_id"):
        assert one[key] == cur[key], "%s disagrees: %r vs %r" % (
            key, one[key], cur[key])


def test_current_reports_the_viewers_own_paid_stake_by_player_not_by_sect(
        world: Dict[str, Any]) -> None:
    """``your_paid`` is keyed on the PLAYER.

    Handing ``war_view`` the sect id instead prices every paying member at zero,
    and the member never learns why: the war screen simply shows nothing.
    """
    sect = _make_war(world)
    _fund(world, sect, (sect["atk"], 2500))
    body = world["client"].get("/sect-war/current",
                               headers=sect["atk"]["headers"]).json()
    assert body["your_paid"] >= 2500, body


def test_a_viewer_outside_both_sects_is_refused_not_silently_empty(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = world["client"].get("/sect-war/%s" % war["war"],
                            headers=war["out"]["headers"])
    assert r.status_code == 403 and _detail(r) == NOT_A_MEMBER


def test_an_unknown_war_and_a_foreign_war_answer_the_same_thing(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A 404 here would be an oracle: it would tell an outsider that a war
    exists."""
    real = world["client"].get("/sect-war/%s" % war["war"],
                               headers=war["out"]["headers"])
    fake = world["client"].get("/sect-war/war_no_such_id",
                               headers=war["out"]["headers"])
    assert real.status_code == fake.status_code == 403
    assert _detail(real) == _detail(fake) == NOT_A_MEMBER


def test_battle_answers_a_simulation_and_a_null_preview_by_default(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = world["client"].post("/sect-war/%s/battle" % war["war"],
                             json={"war_id": war["war"]},
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()})
    body = assert_keys(r, BATTLE_KEYS, where="battle")
    assert body["preview"] is None
    assert set(body["battle"]) == BATTLE_SIM_KEYS


def test_battle_with_preview_true_is_a_measured_500(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A MEASURED DEFECT, pinned as-is so a fix is visible.

    The battle handler calls ``preview_settlement(..., players_db)`` naming a
    ``players_db`` that is not in that scope, so ``{"preview": true}`` raises and
    the client sees a 500. The same payload is available and correct from
    ``POST /sect-war/{war_id}/preview``.

    Asserting the observed 500 means repairing the handler turns this test RED
    until the expectation is updated; and the dedicated route's 200 in the same
    test proves the payload is reachable rather than simply missing.
    """
    _fund(world, war, (war["atk"], 1500))
    crashed = world["client"].post("/sect-war/%s/battle" % war["war"],
                                   json={"war_id": war["war"], "preview": True},
                                   headers={**war["atk"]["headers"],
                                            "Idempotency-Key": _key()})
    assert crashed.status_code == 500, (
        "battle/preview no longer 500s -- the NameError is fixed; update this "
        "test and the client rule together")
    working = assert_keys(
        world["client"].post("/sect-war/%s/preview" % war["war"],
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()}),
        PREVIEW_KEYS, where="dedicated preview")
    assert working["payout_count"] >= 1


def test_battle_refuses_an_unknown_war_with_404(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = world["client"].post("/sect-war/%s/battle" % war["war"],
                             json={"war_id": "war_no_such_id"},
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()})
    assert r.status_code == 404, r.text[:200]


def test_battle_refuses_a_war_the_caller_is_not_in(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = world["client"].post("/sect-war/%s/battle" % war["war"],
                             json={"war_id": war["war"]},
                             headers={**war["out"]["headers"],
                                      "Idempotency-Key": _key()})
    assert r.status_code == 403 and _detail(r) == NOT_A_MEMBER


def test_battle_reads_the_war_id_from_the_body_not_the_path(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The battle route reads the BODY's war id.

    A body id naming another real war the caller belongs to wins over the path.
    That is the shipped contract; the preview route is the one keyed on the path.
    """
    spare = _another_sect(world)
    other = _declare(world, war["atk"], spare["sect_id"]).json()["war_id"]
    r = world["client"].post("/sect-war/%s/battle" % war["war"],
                             json={"war_id": other},
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()})
    assert r.status_code == 200 and r.json()["battle"]["attacker_score"] == 0, (
        "the body named the second war, whose scores are still zero")
    same = world["client"].post("/sect-war/%s/battle" % war["war"],
                                json={"war_id": war["war"]},
                                headers={**war["atk"]["headers"],
                                         "Idempotency-Key": _key()})
    assert same.status_code == 200


def test_preview_answers_the_settled_preview_keys_when_the_war_is_settled(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 2000))
    report = sw.settle_war(world["players_db"], world["sect_store"],
                           world["sect_store"].wars[war["war"]], AFTER_BOUNDARY)
    assert report["ok"], report
    body = assert_keys(
        world["client"].post("/sect-war/%s/preview" % war["war"],
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()}),
        PREVIEW_SETTLED_KEYS, where="preview(settled)")
    assert body["settled"] is True and body["payouts"] == []
    assert body["note"] == "Already settled"


def test_contribute_answers_exactly_the_declared_keys(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 1200))
    before = _stones(world, war["atk"]["player_id"])
    body = assert_keys(_contribute(world, war["atk"], war["war"], 800),
                       CONTRIBUTE_KEYS, where="contribute")
    assert body["ok"] is True and body["stones_spent"] == 800
    assert body["your_paid"] >= 2000
    assert body["your_side"] == "attacker"
    assert before - _stones(world, war["atk"]["player_id"]) == 800, (
        "the reply says 800 and the purse must agree: a free contribution is a "
        "free point on the scoreboard")


def test_contribute_adds_a_second_stake_rather_than_replacing_it(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 1000))
    body = _contribute(world, war["atk"], war["war"], 500).json()
    assert body["your_paid"] >= 1500, body


# ==========================================================================
# 3. THE REFUSALS -- authorization first, then the shape, then the money
# ==========================================================================

def test_declare_refuses_a_player_in_no_sect_with_400(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = _declare(world, war["out"], war["atk_sect"])
    assert r.status_code == 400 and _detail(r) == "Not in a sect"


def test_declare_refuses_a_disciple_with_403(world: Dict[str, Any],
                                            war: Dict[str, Any]) -> None:
    world["sect_store"].get_sect(war["atk_sect"])["members"][
        war["atk"]["player_id"]]["role"] = "Disciple"
    r = _declare(world, war["atk"], war["def_sect"])
    assert r.status_code == 403 and _detail(r) == (
        "Only a patriarch may declare war")


@pytest.mark.parametrize("body", [
    {},                                        # missing
    {"defender_sect_id": ""},                  # empty
    {"defender_sect_id": "x" * 65},            # over the declared length
    {"defender_sect_id": 7},                   # wrong type
])
def test_declare_rejects_a_bad_body_before_the_handler(
        world: Dict[str, Any], war: Dict[str, Any], body: Dict[str, Any]) -> None:
    r = world["client"].post("/sect-war/declare", json=body,
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()})
    assert r.status_code == 422, "%r -> %s %s" % (body, r.status_code,
                                                  r.text[:160])


def test_a_body_carrying_a_result_is_ignored_rather_than_honoured(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """An unknown field does not 422 -- the model drops it -- and it changes
    nothing.

    The load-bearing half is the second half: a posted ``winner`` or
    ``attacker_score`` must not reach the reply. The 200 is only acceptable
    because the STORED scores are what come back.

    The war is declared against a THIRD sect, not ``def_sect``: the ``war``
    fixture already declared between ``atk_sect`` and ``def_sect``, and
    ``SectStore`` refuses a second war between the same pair ("War already
    exists", 400). That refusal is correct behaviour, and it meant this arm
    never reached the assertion it exists to make.
    """
    spare = _another_sect(world)
    r = world["client"].post(
        "/sect-war/declare",
        json={"defender_sect_id": spare["sect_id"], "winner": "attacker",
              "attacker_score": 10 ** 9},
        headers={**war["atk"]["headers"], "Idempotency-Key": _key()})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["battle"]["attacker_score"] == 0, (
        "the posted attacker_score did not become the stored score")
    assert r.json().get("winner") is None, (
        "a caller must not be able to post the outcome into the reply")


def test_declare_carries_the_stores_own_refusal_string(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    r = _declare(world, war["atk"], "sect_does_not_exist")
    assert r.status_code == 400
    assert _detail(r) == "Defender sect not found", _detail(r)


def test_contribute_refuses_a_stranger_before_consulting_the_wallet(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Authorization first: a stranger learns nothing about their own balance.

    A player in no sect with an EMPTY purse must get the membership refusal. If
    the affordability check came first they would get "not enough stones" -- a
    fact about a wallet they never opened for anyone else.
    """
    pauper = _new_player(world, "pauper", stones=0)
    r = _contribute(world, pauper, war["war"], 100)
    assert r.status_code == 403 and _detail(r) == NOT_A_MEMBER


def test_contribute_refuses_a_member_who_cannot_pay_with_409(
        world: Dict[str, Any]) -> None:
    """A member who cannot afford the stake gets 409, not 403 and not 500."""
    atk = _new_player(world, "poor-atk")
    dfn = _new_player(world, "poor-def")
    atk_sect = _found_sect(world, atk, "PoorAtk")
    def_sect = _found_sect(world, dfn, "PoorDef")
    made = _declare(world, atk, def_sect)
    assert made.status_code == 200, made.text
    poor = _new_player(world, "poor", stones=0)
    world["sect_store"].join_sect(poor["player_id"], atk_sect)
    r = _contribute(world, poor, made.json()["war_id"], 100)
    assert r.status_code == 409, r.text[:200]


@pytest.mark.parametrize("ctype", ["materials", "combat", "gold", "", "FUNDS"])
def test_only_a_funds_contribution_is_paid_out(
        world: Dict[str, Any], war: Dict[str, Any], ctype: str) -> None:
    """A free contribution in the paid-stake ledger hands back the exact hole
    the per-player map exists to close."""
    r = _contribute(world, war["atk"], war["war"], 500, ctype=ctype)
    assert r.status_code == 400 and _detail(r) == (
        "Only a funds contribution is paid out")
    assert sw.contributed_to(world["sect_store"], war["war"],
                             war["atk"]["player_id"]) == 0


@pytest.mark.parametrize("value", [0, -1, -10**9, 10_001])
def test_contribute_refuses_a_value_outside_the_declared_bounds(
        world: Dict[str, Any], war: Dict[str, Any], value: int) -> None:
    r = _contribute(world, war["atk"], war["war"], value)
    assert r.status_code == 422, "%d -> %s" % (value, r.status_code)


@pytest.mark.parametrize("value", [1, 10_000])
def test_contribute_accepts_exactly_the_declared_bounds(
        world: Dict[str, Any], war: Dict[str, Any], value: int) -> None:
    world["sect_store"].wars[war["war"]].phase = "active"
    body = _contribute(world, war["atk"], war["war"], value)
    assert body.status_code == 200, body.text[:200]
    assert body.json()["stones_spent"] == value


def test_contribute_refuses_a_war_that_is_not_active(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    world["sect_store"].wars[war["war"]].phase = "settled"
    r = _contribute(world, war["atk"], war["war"], 500)
    assert r.status_code == 400, r.text[:200]


def test_contribute_takes_the_war_id_from_the_path_not_the_body(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Two real wars, one body id, one path id: the PATH decides.

    A contributor who names another war in the body must still fund the war they
    addressed, or a replay against a stale body buys a point on the wrong war.
    """
    spare = _another_sect(world)
    other = _declare(world, war["atk"], spare["sect_id"]).json()["war_id"]
    world["sect_store"].wars[war["war"]].phase = "active"
    world["sect_store"].wars[other].phase = "active"
    r = world["client"].post(
        "/sect-war/%s/contribute" % war["war"],
        json={"war_id": other, "contribution_type": "funds", "value": 600},
        headers={**war["atk"]["headers"], "Idempotency-Key": _key()})
    assert r.status_code == 200 and r.json()["war_id"] == war["war"], r.text
    assert sw.contributed_to(world["sect_store"], war["war"],
                             war["atk"]["player_id"]) >= 600
    assert sw.contributed_to(world["sect_store"], other,
                             war["atk"]["player_id"]) == 0


def test_preview_refuses_an_outsider_and_a_made_up_war_alike(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    real = world["client"].post("/sect-war/%s/preview" % war["war"],
                                headers={**war["out"]["headers"],
                                         "Idempotency-Key": _key()})
    fake = world["client"].post("/sect-war/war_no_such_id/preview",
                                headers={**war["out"]["headers"],
                                         "Idempotency-Key": _key()})
    assert real.status_code == fake.status_code == 403
    assert _detail(real) == _detail(fake) == NOT_A_MEMBER


# ==========================================================================
# 4. THE PAYOUT TABLE -- the floor, the role, the caps, and the invariant
# ==========================================================================

def test_the_settlement_table_can_never_mint_stones() -> None:
    """Swept at run time over roles, stakes and opponent totals.

    ``worst_ratio`` is the largest payout-to-stake ratio found. It must stay at
    or below 1.0: a war that pays out more than was staked creates stones.
    """
    report = sw.verify_settlement_invariant()
    assert report["ok"] is True
    assert report["cases"] > 100
    assert report["worst_ratio"] <= 1.0, report


def test_a_zero_or_negative_stake_is_skipped_and_burns_no_receipt() -> None:
    for stake in (0, -1, -10_000):
        result = sw.payout_for("winner", stake, stake, 50_000)
        assert result["amount"] == 0, (stake, result)
        assert result["skipped"] == "never_contributed_paid"
        assert "medal" in result and result["medal"] is False


def test_below_the_floor_a_stake_earns_no_flat_bonus() -> None:
    """One stone must not return 450. The floor is the whole anti-abuse rule."""
    small = sw.payout_for("winner", 1, 1, 50_000)
    assert small["amount"] == 0, small
    floored = sw.payout_for("winner", sw.MIN_STAKE_FOR_BONUS - 1,
                            sw.MIN_STAKE_FOR_BONUS - 1, 50_000)
    assert floored["gross"] == int((sw.MIN_STAKE_FOR_BONUS - 1)
                                   * sw.WINNER_RETURN), floored


def test_a_winning_stake_at_or_above_the_floor_is_the_exact_figure() -> None:
    """The exact number, not a plausible one.

    An inverted role produces a number that also looks reasonable, so only the
    exact amount catches it.
    """
    won = sw.payout_for("winner", 3000, 3000, 5000)
    expected = int(3000 * sw.WINNER_RETURN) + sw.WINNER_BONUS
    if won["medal"]:
        expected += sw.MEDAL_BONUS
    assert won["amount"] == expected, (won, expected)
    assert won["medal"] is True, "3000 of an 8000 total is a 37.5% share, >= 25%"


def test_a_losing_stake_gets_the_loser_return_and_no_bonus() -> None:
    lost = sw.payout_for("loser", 700, 700, 3000)
    assert lost["amount"] == int(700 * sw.LOSER_RETURN), lost
    assert lost["medal"] is False


def test_the_bonus_floor_applies_to_both_roles() -> None:
    for role in ("winner", "loser"):
        below = sw.payout_for(role, 500, 500, 500)
        above = sw.payout_for(role, 2000, 2000, 500)
        assert above["amount"] > below["amount"], (role, above, below)


def test_the_medal_needs_the_winning_side_the_floor_and_the_share() -> None:
    """Three separate conditions; dropping any one hands out a holding."""
    assert sw.payout_for("loser", 9000, 9000, 100)["medal"] is False, (
        "the losing side never takes the medal, however large its stake")
    assert sw.payout_for("winner", sw.MIN_STAKE_FOR_BONUS - 1,
                         sw.MIN_STAKE_FOR_BONUS - 1, 1)["medal"] is False, (
        "below the floor there is no medal, whatever the share")
    assert sw.payout_for("winner", 4000, 4000, 40_000)["medal"] is False, (
        "a 9% share is under the 25% medal share even though the stake is large")


def test_the_medal_is_decided_by_effort_share_not_by_a_raw_total() -> None:
    """A small sect that out-funds a big one still has to earn the share.

    A raw-total comparison would hand the medal to whichever sect happens to have
    more members, before anyone acts -- which is exactly why ``effort_share``
    replaces the raw comparison.
    """
    assert sw.effort_share(4000, 40_000) < sw.MEDAL_SHARE
    assert sw.effort_share(4000, 5000) >= sw.MEDAL_SHARE
    assert sw.payout_for("winner", 4000, 4000, 5000)["medal"] is True


def test_the_daily_and_season_caps_clamp_and_say_so() -> None:
    """A cap that silently truncates reports success while paying less than
    promised, so ``capped`` is part of the contract.

    The stake is chosen so the GROSS exceeds the daily cap; otherwise the cap
    would never bite and the test would pass without measuring anything.
    """
    stake = 10_000
    gross = int(stake * sw.WINNER_RETURN) + sw.WINNER_BONUS + sw.MEDAL_BONUS
    assert gross > sw.DAILY_CAP, "this case needs a stake the daily cap bites"
    daily = sw.payout_for("winner", stake, stake, stake,
                          paid_today=sw.DAILY_CAP - 10)
    assert daily["amount"] == 10 and daily["capped"] is True, daily
    assert daily["gross"] == gross, daily
    season = sw.payout_for("winner", stake, stake, stake,
                           paid_today=0, paid_season=sw.SEASON_CAP)
    assert season["amount"] == 0 and season["capped"] is True, season


def test_effort_share_is_a_share_not_a_difference() -> None:
    assert sw.effort_share(0, 0) == 0.0
    assert sw.effort_share(500, 500) == 0.5
    assert sw.effort_share(3000, 1000) == 0.75
    assert abs(sw.effort_share(700, 2100)
               + sw.effort_share(2100, 700) - 1.0) < 1e-9


def test_the_payout_is_a_return_on_the_paid_stake_not_a_prize_pool() -> None:
    """Three times the stake does not earn three times the prize per member.

    This is the property a reviewer should check first: it is what stops a rich
    sect from buying itself a bigger prize. Compared on GROSS, because the daily
    cap truncates both figures and would hide the ratio.
    """
    small = sw.payout_for("winner", 1000, 1000, 2000)
    rich = sw.payout_for("winner", 3000, 3000, 2000)
    assert rich["gross"] > small["gross"]
    assert rich["gross"] == int(3000 * sw.WINNER_RETURN) + sw.WINNER_BONUS \
        + sw.MEDAL_BONUS, rich
    assert rich["gross"] / 3000 < small["gross"] / 1000 * 3


# ==========================================================================
# 5. THE BATTLE SIMULATION -- deterministic, and decided by the scoreboard
# ==========================================================================

def test_the_simulation_is_deterministic_on_its_seed() -> None:
    one = sw.simulate_battle(5000, 1200, seed="war_fixed")
    two = sw.simulate_battle(5000, 1200, seed="war_fixed")
    assert one == two, "a client must not be able to re-roll the war by asking"
    assert set(one) == BATTLE_SIM_KEYS
    assert len(one["rounds"]) == sw.BATTLE_ROUNDS


def test_a_lopsided_scoreboard_decides_the_battle() -> None:
    result = sw.simulate_battle(50_000, 100, seed="war_lopsided")
    assert result["predicted_winner"] == "attacker"
    assert result["attacker_share"] > 0.99
    assert result["lead_changes"] == 0
    assert result["contested"] is False


def test_contested_is_exactly_the_flag_the_log_implies() -> None:
    """``contested`` is not a free-floating opinion.

    It is true when the lead changed hands at least twice or nobody led at the
    end, so a client can explain the flag from the rounds it already has.
    Asserted as the relationship, so the two cannot drift apart.
    """
    for seed in ("war_a", "war_b", "war_c", "war_d", "war_e"):
        result = sw.simulate_battle(3000, 3000, seed=seed)
        expected = result["lead_changes"] >= 2 or result["predicted_winner"] is None
        assert result["contested"] is expected, (seed, result)


def test_a_different_seed_produces_a_different_log() -> None:
    one = sw.simulate_battle(3000, 3000, seed="war_one")
    two = sw.simulate_battle(3000, 3000, seed="war_two")
    assert one["rounds"] != two["rounds"]


def test_a_zero_score_war_is_simulated_without_dividing_by_zero() -> None:
    result = sw.simulate_battle(0, 0, seed="war_empty")
    assert result["attacker_share"] == 0.5
    assert len(result["rounds"]) == sw.BATTLE_ROUNDS


def test_the_round_log_carries_the_four_declared_fields() -> None:
    for row in sw.simulate_battle(900, 400, seed="war_shape")["rounds"]:
        assert set(row) == {"round", "attacker_power", "defender_power", "leader"}
        assert row["leader"] in ("attacker", "defender", None)
        assert isinstance(row["round"], int)


# ==========================================================================
# 6. THE PAID-STAKE LEDGER -- recorded by max, side captured at payment
# ==========================================================================

def test_the_paid_ledger_is_idempotent_by_max_not_by_sum() -> None:
    """A retry must not inflate a player's own payout."""
    sw.record_war_payment("war_ledger", "p1", 3000, side="attacker")
    sw.record_war_payment("war_ledger", "p1", 3000, side="attacker")
    assert sw.contributed_to(None, "war_ledger", "p1") == 3000
    sw.record_war_payment("war_ledger", "p1", 1000, side="attacker")
    assert sw.contributed_to(None, "war_ledger", "p1") == 3000


def test_a_zero_or_unusable_stake_is_not_recorded_at_all() -> None:
    for bad in (0, -50, None, "not-a-number"):
        sw.record_war_payment("war_ledger2", "p2", bad, side="attacker")
    assert sw.contributed_to(None, "war_ledger2", "p2") == 0


def test_the_side_is_captured_at_payment_not_read_at_payout_time() -> None:
    """Membership is not participation.

    A player who paid for the attacking sect and then left would otherwise be
    priced against neither side, and a 1950 payment would silently become zero --
    for the loser too, since the consolation exists so nobody who took part is
    left with nothing.
    """
    sw.record_war_payment("war_side", "p3", 3000, side="attacker")
    assert sw.paid_side("war_side", "p3") == "attacker"
    assert sw.paid_side("war_side", "unknown") == ""


def test_contributed_to_answers_zero_rather_than_raising_on_a_bare_store() -> None:
    class Bare:
        pass

    assert sw.contributed_to(Bare(), "war_x", "p") == 0
    assert sw.contributed_to(Bare(), "war_x", None) == 0

    class Exploding:
        def contributed_to_war(self, wid, pid):
            raise RuntimeError("no such method on this branch")

    sw.record_war_payment("war_x", "p", 700)
    assert sw.contributed_to(Exploding(), "war_x", "p") == 700


# ==========================================================================
# 7. PRICING AND THE PREVIEW -- pure, once per contributor, no state change
# ==========================================================================

def test_preview_prices_the_war_from_stored_rows(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 3000), (war["def"], 700))
    body = assert_keys(
        world["client"].post("/sect-war/%s/preview" % war["war"],
                             headers={**war["atk"]["headers"],
                                      "Idempotency-Key": _key()}),
        PREVIEW_KEYS, where="preview")
    assert body["settled"] is False
    assert body["currency"] == "spirit_stone"
    assert body["min_stake_for_bonus"] == sw.MIN_STAKE_FOR_BONUS
    assert body["daily_cap"] == sw.DAILY_CAP
    assert body["season_cap"] == sw.SEASON_CAP
    assert body["payout_count"] == 2, (
        "ONE payout per contributor, not one per contributor per side")
    assert body["total_payout"] == sum(p["amount"] for p in body["payouts"])


def test_preview_prices_each_contributor_on_their_own_side(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The winner's figure and the loser's must not be swapped.

    Both wrong numbers look individually plausible, so only the exact expected
    amount catches it.
    """
    _fund(world, war, (war["atk"], 3000), (war["def"], 700))
    body = world["client"].post("/sect-war/%s/preview" % war["war"],
                                headers={**war["atk"]["headers"],
                                         "Idempotency-Key": _key()}).json()
    by_player = {p["player_id"]: p for p in body["payouts"]}
    winner = by_player[war["atk"]["player_id"]]
    loser = by_player[war["def"]["player_id"]]
    assert winner["role"] == "winner" and winner["side"] == "attacker"
    assert loser["role"] == "loser" and loser["side"] == "defender"
    assert loser["amount"] == int(700 * sw.LOSER_RETURN), loser
    assert winner["amount"] > loser["amount"]


def test_preview_changes_nothing_however_often_it_is_called(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Three calls, one answer, no write, and -- the load-bearing half -- the
    once-only settlement key is NOT consumed. Burning it on a read would deny a
    real payout later, and the player would never learn why."""
    _fund(world, war, (war["atk"], 2000))
    row = world["sect_store"].wars[war["war"]]
    before = (row.phase, row.attacker_score, row.defender_score)
    purse = _stones(world, war["atk"]["player_id"])
    receipts = set(sw._MEM_RECEIPTS)
    totals = set()
    for _ in range(3):
        r = world["client"].post("/sect-war/%s/preview" % war["war"],
                                 headers={**war["atk"]["headers"],
                                          "Idempotency-Key": _key()})
        assert r.status_code == 200
        totals.add(r.json()["total_payout"])
    assert len(totals) == 1, "a preview must be pure: %s" % totals
    assert (row.phase, row.attacker_score, row.defender_score) == before
    assert set(sw._MEM_RECEIPTS) == receipts, (
        "a preview must not burn the once-only settlement key")
    assert _stones(world, war["atk"]["player_id"]) == purse


def test_price_for_member_answers_none_for_a_player_who_funded_neither_side(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 2000))
    priced = sw.price_for_member(world["sect_store"].wars[war["war"]],
                                 world["sect_store"],
                                 war["out"]["player_id"], 2000)
    assert priced is None


def test_the_preview_stops_at_the_declared_member_ceiling(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Bounded work: a preview of a very large sect is a list of at most
    PREVIEW_MAX_MEMBERS entries, not an unbounded one."""
    members = []
    for i in range(3):
        session = _new_player(world, "m%d" % i)
        world["sect_store"].join_sect(session["player_id"], war["atk_sect"])
        members.append(session)
    _fund(world, war, *[(m, 1200) for m in members])
    body = world["client"].post("/sect-war/%s/preview" % war["war"],
                                headers={**war["atk"]["headers"],
                                         "Idempotency-Key": _key()}).json()
    assert body["payout_count"] <= sw.PREVIEW_MAX_MEMBERS
    assert len({p["player_id"] for p in body["payouts"]}) == body["payout_count"]


def test_the_preview_total_is_what_the_job_then_pays(
        world: Dict[str, Any]) -> None:
    """What the player was told and what the player gets are the same figure.

    This is the arm that catches a preview which recomputes, loops both sides, or
    prices a contributor against the wrong side's totals: each of those produces
    individually plausible numbers for a payout nobody then receives.
    """
    war = _funded_war(world)
    sw.reset_due_memo()
    promised = world["client"].post("/sect-war/%s/preview" % war["war"],
                                    headers={**war["atk"]["headers"],
                                             "Idempotency-Key": _key()}).json()
    started = {war["atk"]["player_id"]: _stones(world, war["atk"]["player_id"]),
               war["def"]["player_id"]: _stones(world, war["def"]["player_id"])}
    report = sw.settle_war(world["players_db"], world["sect_store"],
                           world["sect_store"].wars[war["war"]], AFTER_BOUNDARY)
    assert report["ok"] and report["failed"] == 0, report
    for row in promised["payouts"]:
        pid = row["player_id"]
        assert _stones(world, pid) - started[pid] == row["amount"], (
            "%s was promised %d" % (pid, row["amount"]))


# ==========================================================================
# 8. THE ONCE-ONLY LEDGER -- receipt before money, one war lease
# ==========================================================================

def test_the_settlement_key_is_derived_from_the_event_not_from_a_request() -> None:
    """No caller-supplied key column exists, so there is nothing to replay with."""
    assert sw._settlement_key("war_a", "p1") == "war_a:p1:settle"
    assert sw._settlement_key("war_a", "p1") != sw._settlement_key("war_a", "p2")


def test_a_payout_is_claimed_once(world: Dict[str, Any]) -> None:
    """The second claim answers False, so the caller must not touch the purse."""
    receipt = {"war_id": "war_claim", "player_id": "p", "amount": 100}
    key = sw._settlement_key("war_claim", "p")
    assert sw.claim_payout(key, world["players_db"], AFTER_BOUNDARY,
                           receipt) is True
    assert sw.claim_payout(key, world["players_db"], AFTER_BOUNDARY,
                           receipt) is False


def test_a_war_is_leased_to_one_caller_and_released_on_demand(
        world: Dict[str, Any]) -> None:
    assert sw.claim_war("war_lease", world["players_db"], AFTER_BOUNDARY) is True
    assert sw.claim_war("war_lease", world["players_db"], AFTER_BOUNDARY) is False
    sw._release_war_claim("war_lease")
    assert sw.claim_war("war_lease", world["players_db"], AFTER_BOUNDARY) is True


def test_a_dry_run_settles_nothing_and_writes_no_receipt(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 3000))
    before = _stones(world, war["atk"]["player_id"])
    receipts = set(sw._MEM_RECEIPTS)
    report = sw.settle_war(world["players_db"], world["sect_store"],
                           world["sect_store"].wars[war["war"]],
                           AFTER_BOUNDARY, dry_run=True)
    assert report["dry_run"] is True and report["failed"] == 0, report
    assert report["banner"] == {"ok": True, "awarded": False, "reason": "dry_run"}
    assert set(sw._MEM_RECEIPTS) == receipts
    assert _stones(world, war["atk"]["player_id"]) == before
    assert world["sect_store"].wars[war["war"]].phase != "settled"


def test_settling_a_second_time_pays_nobody(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    _fund(world, war, (war["atk"], 3000), (war["def"], 700))
    row = world["sect_store"].wars[war["war"]]
    before = {war["atk"]["player_id"]: _stones(world, war["atk"]["player_id"]),
              war["def"]["player_id"]: _stones(world, war["def"]["player_id"])}
    first = sw.settle_war(world["players_db"], world["sect_store"], row,
                          AFTER_BOUNDARY)
    assert first["ok"] and first["paid"] == 2 and first["failed"] == 0, first
    for pid, was in before.items():
        assert _stones(world, pid) > was, "%s was not paid" % pid
    second = sw.settle_war(world["players_db"], world["sect_store"], row,
                           AFTER_BOUNDARY)
    assert second["ok"] is False and second["error"] == "Already settled"


def test_a_replayed_war_object_still_pays_nobody_twice(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Once-only is a property of the KEY, not of the phase column.

    A second settlement for the same war id finds its receipts already claimed,
    so it reports skipped rather than paid -- which is what stops a caller that
    retries with a fresh row object from being paid again.
    """
    _fund(world, war, (war["atk"], 3000))
    store = world["sect_store"]
    row = store.wars[war["war"]]
    sw.settle_war(world["players_db"], store, row, AFTER_BOUNDARY)
    paid = _stones(world, war["atk"]["player_id"])
    clone = _copy.copy(row)
    clone.phase = "active"          # pretend the phase was rolled back
    second = sw.settle_war(world["players_db"], store, clone, AFTER_BOUNDARY)
    assert second["paid"] == 0 and second["skipped"] >= 1, second
    assert _stones(world, war["atk"]["player_id"]) == paid


def test_a_settlement_of_no_war_and_of_a_settled_war_are_both_refusals(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    assert sw.settle_war(world["players_db"], world["sect_store"], None,
                         AFTER_BOUNDARY) == {"ok": False,
                                             "error": "War not found"}
    world["sect_store"].wars[war["war"]].phase = "settled"
    assert sw.settle_war(world["players_db"], world["sect_store"],
                         world["sect_store"].wars[war["war"]],
                         AFTER_BOUNDARY)["error"] == "Already settled"


def test_a_credit_failure_is_counted_and_the_receipt_stays_spent(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A payout that cannot move is reported as ``failed``, never as paid.

    The receipt was written BEFORE the money, so the key is spent afterwards --
    this arm pins the ORDER, which is what removes the window where two callers
    both mark and both pay.
    """
    _fund(world, war, (war["atk"], 3000))

    class NoWallet:
        wallet_store = None

    report = sw.settle_war(NoWallet(), world["sect_store"],
                           world["sect_store"].wars[war["war"]],
                           AFTER_BOUNDARY)
    assert report["failed"] == 1 and report["paid"] == 0, report
    assert sw.claim_payout(sw._settlement_key(war["war"],
                                              war["atk"]["player_id"]),
                           world["players_db"], AFTER_BOUNDARY,
                           {"war_id": war["war"],
                            "player_id": war["atk"]["player_id"],
                            "amount": 1}) is False


def test_the_shipped_wallet_stores_expose_no_earn_method() -> None:
    """A MEASURED GAP, pinned so a repair turns it RED.

    ``sect_war._credit`` calls ``wallet_store.earn(...)`` as a METHOD. Neither
    shipped wallet store defines one -- ``wallet_defense.WalletStore`` and
    ``wallet_store_pg.WalletStore`` both expose ``earn`` as a module function
    reached through ``wallet_dispatch``. On the shipped wiring every settlement
    credit therefore raises, is caught, and is reported as ``failed: 1`` while
    moving nothing: the worst shape a money path can have, because it looks like
    a working run.

    This file's own harness supplies the method through ``CreditWallet`` so the
    rest of the suite measures sect_war's arithmetic rather than this gap.
    """
    assert not hasattr(InMemoryWalletStore(), "earn"), (
        "the in-memory wallet now HAS an earn method: _credit stops raising, so "
        "update this test and re-check the settlement job end to end")


# ==========================================================================
# 9. THE DAILY JOB -- a catch-up predicate, one run per day
# ==========================================================================

def test_the_day_boundary_is_todays_midnight_plus_the_settle_hour() -> None:
    boundary = sw.day_index(AFTER_BOUNDARY) * 86_400 + sw.SETTLE_HOUR_UTC * 3600
    assert boundary == AFTER_BOUNDARY - 3600, (
        "the boundary is one hour before the probe; if this ever drifts, the "
        "job fires at the wrong hour")


def test_the_job_is_a_catch_up_predicate_not_a_moment(
        world: Dict[str, Any]) -> None:
    """A server that boots at 04:01 must still settle; one booting before the
    hour must not. Testing ``now == boundary`` does the opposite of both."""
    sw.reset_due_memo()
    assert sw.settle_due(BEFORE_BOUNDARY) is False
    sw.reset_due_memo()
    assert sw.settle_due(AFTER_BOUNDARY) is True
    assert sw.settle_due(AFTER_BOUNDARY) is False, (
        "already claimed today; asking again must not re-arm it")


def test_a_day_that_failed_partway_is_released_for_the_next_tick() -> None:
    """The memo is claimed as a side effect of being asked, so a failed run has
    to give it back -- otherwise the remaining wars are never paid and the next
    tick reports nothing wrong."""
    sw.reset_due_memo()
    assert sw._is_due(AFTER_BOUNDARY) is True
    sw._release_day(sw.day_index(AFTER_BOUNDARY))
    assert sw._is_due(AFTER_BOUNDARY) is True


def test_the_job_does_nothing_before_the_boundary(world: Dict[str, Any]) -> None:
    war = _funded_war(world)
    sw.reset_due_memo()
    report = sw.run_settlement(world["players_db"], world["sect_store"],
                               BEFORE_BOUNDARY)
    assert report["due"] is False and report["settled"] == 0, report
    assert world["sect_store"].wars[war["war"]].phase == "active"


def test_the_job_settles_a_war_once_and_reports_the_count(
        world: Dict[str, Any]) -> None:
    war = _funded_war(world)
    sw.reset_due_memo()
    report = sw.run_settlement(world["players_db"], world["sect_store"],
                               AFTER_BOUNDARY)
    assert report["due"] is True and report["settled"] == 1, report
    assert report["errors"] == [], report
    assert world["sect_store"].wars[war["war"]].phase == "settled"
    sw.reset_due_memo()
    again = sw.run_settlement(world["players_db"], world["sect_store"],
                              AFTER_BOUNDARY)
    assert again["settled"] == 0


def test_the_job_catches_a_bad_war_and_releases_its_lease(
        world: Dict[str, Any]) -> None:
    """One malformed war must not cost every other member their settlement --
    and its own lease has to be given back, or the retry skips it forever."""

    class BadWar:
        war_id = "war_bad"
        phase = "active"

    class BoomStore:
        wars = {"war_bad": BadWar()}

        def settle_war(self, wid):
            raise RuntimeError("malformed war row")

    sw.reset_due_memo()
    report = sw.run_settlement(world["players_db"], BoomStore(), AFTER_BOUNDARY)
    assert report["errors"], report
    assert any(e["war_id"] == "war_bad" for e in report["errors"]), report
    assert "war_bad" not in sw._MEM_CLAIMED_WARS, (
        "the lease was consumed and never released; a retry would skip it "
        "forever and nothing would say why")


def test_the_job_reports_an_unreadable_war_table_instead_of_raising(
        world: Dict[str, Any]) -> None:
    class BoomStore:
        @property
        def wars(self):
            raise RuntimeError("war table unreachable")

    sw.reset_due_memo()
    report = sw.run_settlement(world["players_db"], BoomStore(), AFTER_BOUNDARY)
    assert report["errors"] and report["errors"][0]["war_id"] == "*", report
    assert report["settled"] == 0


def test_reset_due_memo_keeps_the_paid_ledger_by_default() -> None:
    """The ledger is the record that a player PAID. A helper that forgets to
    settle a war must not also erase the evidence that they funded it: the
    measured failure was paid:0, skipped:0, failed:0 -- a settlement that
    reported success and moved no money."""
    sw.record_war_payment("war_memo", "p", 3000, side="attacker")
    sw.reset_due_memo()
    assert sw.contributed_to(None, "war_memo", "p") == 3000
    sw.reset_due_memo(clear_ledger=True)
    assert sw.contributed_to(None, "war_memo", "p") == 0


def test_day_index_is_the_whole_day_count() -> None:
    assert sw.day_index(0) == 0
    assert sw.day_index(86_399) == 0
    assert sw.day_index(86_400) == 1

# ==========================================================================
# 8. NON-FINITE AND OVERFLOWING NUMBERS -- measured, with the 500 explained
# ==========================================================================
#
# MEASURED 2026-10-05 on this tree, pydantic 2.13.4:
#
#   value=1e400 / 1e309 / -1e400 / NaN  ->  HTTP 500
#   value=0.0 / 0.5 / 2.5 / 10001       ->  HTTP 422 (a CLEAN, correct refusal)
#   value="NaN" / "Infinity" (strings) ->  HTTP 422
#   value=1 (in bounds)                ->  HTTP 200, one stone debited
#
# The 500 is NOT a sect_war defect and this file does not pretend it is one. The
# request model DECLARES `value: int = Field(ge=1, le=10000)`, and pydantic does
# reject a non-finite float with the RIGHT error:
#
#   {'type': 'finite_number', 'loc': ('body','value'), 'msg': 'Input should be a
#    finite number', 'input': inf}
#
# That error then dies in FastAPI's own 422 handler:
#
#   fastapi.exception_handlers.request_validation_exception_handler
#     -> JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
#     -> json.dumps(..., allow_nan=False)
#     -> ValueError: Out of range float values are not JSON compliant: inf
#
# REPRODUCED ON A 6-LINE BARE APP with no sect_war in it: any FastAPI route with
# a `float`/`int` body field answers 500 for a non-finite JSON literal. Every
# OTHER validation error on the same field (out of range, fractional, unparsable
# string) answers 422 correctly, because only `finite_number` carries the raw
# `inf` back into the response body.
#
# WHAT SECT_WAR OWNS, AND WHAT IS PINNED BELOW: whatever the status code, a
# non-finite value must never reach a wallet or a ledger. These tests assert
# the MONEY AND LEDGER invariant (unchanged balance, unchanged contribution,
# unchanged score) rather than the status code, because the status code is
# FastAPI's to give or take and asserting 500 would freeze a framework bug into
# this module's contract.
#
# WHY IT STILL MATTERS even though the money is safe: a 500 is an unhandled
# server fault. It is the wrong shape for a malformed request, it defeats the
# client's retry/backoff logic (a 500 reads as "try again later", so a client
# may hammer it), and it means the one input class an attacker would fuzz
# first -- the numbers -- is the one class that produces stack traces.


def _non_finite_bodies(war_id: str) -> list:
    """Raw JSON bodies carrying a non-finite ``value``.

    Sent as RAW BYTES, not ``json=``: httpx encodes with ``allow_nan=False``
    and raises ``ValueError`` client-side on ``float("inf")``, so a ``json=``
    call never reaches the route and would look like a clean refusal.
    """
    head = '"war_id":"%s","contribution_type":"funds"' % war_id
    return [
        ("1e400", "{%s,\"value\":1e400}" % head),
        ("1e309", "{%s,\"value\":1e309}" % head),
        ("-1e400", "{%s,\"value\":-1e400}" % head),
        ("NaN", "{%s,\"value\":NaN}" % head),
    ]


def test_a_non_finite_value_never_reaches_a_wallet_or_a_ledger(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The invariant that matters: no charge, no ledger row, no score.

    Asserted on the WALLET and the LEDGER, not the status code. A 500 that
    moved no money is a framework wart; a 500 that moved money is a defect, and
    this is the assertion that would catch it.
    """
    store = world["sect_store"]
    session = war["atk"]
    pid = session["player_id"]
    wid = war["war"]
    store.wars[wid].phase = "active"

    for label, raw in _non_finite_bodies(wid):
        stones_before = _stones(world, pid)
        score_before = store.wars[wid].attacker_score
        paid_before = sw.contributed_to(store, wid, pid)

        response = world["client"].post(
            "/sect-war/%s/contribute" % wid, content=raw.encode("utf-8"),
            headers={**session["headers"], "Content-Type": "application/json",
                     "Idempotency-Key": _key()})

        # NOT a success. MEASURED today this is a 500 (the framework bug
        # described above), not the 400/422 a clean refusal would look like --
        # so the assertion is deliberately "never a 2xx" rather than a status
        # literal. Pinning 500 here would freeze a framework defect into this
        # module's contract; pinning 422 would go red the moment someone
        # fixes it upstream. What must hold either way is everything below.
        assert not (200 <= response.status_code < 300), (
            "%s was ACCEPTED with status %s; a non-finite value must never "
            "succeed" % (label, response.status_code))
        assert _stones(world, pid) == stones_before, (
            "%s moved the wallet: %d -> %d" % (
                label, stones_before, _stones(world, pid)))
        assert sw.contributed_to(store, wid, pid) == paid_before, (
            "%s wrote a ledger row" % label)
        assert store.wars[wid].attacker_score == score_before, (
            "%s advanced the war score" % label)


def test_a_non_finite_value_is_refused_before_the_handler_and_not_after(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The refusal comes from BODY VALIDATION, not from a route branch.

    Pinned by asking the REQUEST MODEL directly, which is the seam that decides
    the answer. ``SectWarContributeRequest`` declares
    ``value: int = Field(ge=1, le=10000)``, and pydantic rejects all four
    non-finite literals with a validation-layer error -- no route code runs,
    no idempotency key is needed, no handler state is consulted.

    Asserting the model's own verdict rather than the HTTP status is
    deliberate. The status code is FastAPI's to give (see the section comment
    above), but the MODEL is this module's declaration, and a validation-layer
    error is what proves the rejection happens before any handler state is
    touched -- a route's own 400 could not prove that.
    """
    request_model = sw.SectWarContributeRequest
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(Exception) as caught:
            request_model(war_id=war["war"], contribution_type="funds",
                          value=bad)
        errors = caught.value.errors()
        assert errors, "a non-finite stake was accepted by the request model"
        assert any(item.get("loc") == ("value",) for item in errors), (
            "%r was refused for %s, not for the declared value field"
            % (bad, [item.get("loc") for item in errors]))

    # And the in-bounds value IS accepted, so the test above is discriminating
    # rather than passing on a model that refuses everything.
    assert request_model(war_id=war["war"], contribution_type="funds",
                         value=1).value == 1


def test_a_planted_non_finite_number_is_dropped_where_no_field_is_declared(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Only ``contribute`` declares a number, and only it must refuse one.

    MEASURED, and this is the correct shape of the contract rather than a
    weakened one. The three request models declare:

    =============  ==================================
    route          declared fields
    =============  ==================================
    ``declare``    ``defender_sect_id``
    ``battle``     ``war_id``, ``preview``
    ``contribute`` ``war_id``, ``contribution_type``, ``value``
    =============  ==================================

    So a non-finite literal planted in ``declare`` or ``battle`` lands on a
    field the model does not declare and is DROPPED. Both routes answer 200 --
    correctly, and identically to the same request without the planted key,
    which is what these tests compare rather than a status code.

    Why this is the property worth pinning: a dropped field cannot become a
    stake. A 500 on these routes would be noise about a value nothing reads,
    but a route that HONOURED a planted number would be a money bug. Comparing
    the whole reply body against the unplanted request is what proves the
    planted field changed nothing at all.
    """
    wid = war["war"]
    store = world["sect_store"]
    store.wars[wid].phase = "active"

    # Model level: the planted key is simply not there afterwards.
    battle_model = sw.SectWarBattleRequest(war_id=wid, value=math.nan)
    assert battle_model.model_dump() == {"war_id": wid, "preview": False}, (
        "battle kept an undeclared field: %s" % battle_model.model_dump())
    declare_model = sw.SectWarDeclareRequest(defender_sect_id="s",
                                             value=math.inf)
    assert declare_model.model_dump() == {"defender_sect_id": "s"}, (
        "declare kept an undeclared field: %s" % declare_model.model_dump())

    # HTTP level: identical replies to the same request without it.
    for path, plain_body, planted_body in [
        ("/sect-war/%s/battle" % wid, {"war_id": wid},
         '{"war_id":"%s","value":NaN}' % wid),
        ("/sect-war/%s/preview" % wid, None, '{"value":NaN}'),
    ]:
        headers = {**war["atk"]["headers"], "Idempotency-Key": _key()}
        if plain_body is None:
            clean = world["client"].post(path, headers=headers)
        else:
            clean = world["client"].post(path, json=plain_body, headers=headers)
        planted = world["client"].post(
            path, content=planted_body.encode("utf-8"),
            headers={**headers, "Content-Type": "application/json",
                     "Idempotency-Key": _key()})
        assert clean.status_code == planted.status_code == 200, (
            "%s: clean %s vs planted %s"
            % (path, clean.status_code, planted.status_code))
        # ``server_now`` is a live clock reading, so it differs between any two
        # calls and carries no information about the planted field. It is
        # dropped; every other key must match exactly.
        clean_body = {k: v for k, v in clean.json().items()
                      if k != "server_now"}
        planted_body_out = {k: v for k, v in planted.json().items()
                            if k != "server_now"}
        assert clean_body == planted_body_out, (
            "%s answered differently when a non-finite number was planted in "
            "a field it does not declare: %s vs %s"
            % (path, clean_body, planted_body_out))


def test_no_route_turns_a_non_finite_body_into_a_money_write(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The end-to-end invariant: a non-finite body moves no money anywhere.

    Every state-changing route is driven with a non-finite literal, then the
    wallets, the war table and the paid ledger are all read back. Status codes
    are deliberately not asserted -- ``contribute``'s 500 is FastAPI's to give
    (see the section comment) and the other two correctly drop the planted
    field -- so this test states the one thing that must be true regardless of
    which way any of them is fixed.
    """
    wid = war["war"]
    store = world["sect_store"]
    store.wars[wid].phase = "active"
    payers = (war["atk"]["player_id"], war["def"]["player_id"])
    before_stones = {pid: _stones(world, pid) for pid in payers}
    before_ledger = {pid: sw.contributed_to(store, wid, pid) for pid in payers}

    probes = [
        ("declare", "/sect-war/declare",
         '{"defender_sect_id":%s,"value":1e400}' % _json.dumps(war["def_sect"])),
        ("battle", "/sect-war/%s/battle" % wid,
         '{"war_id":"%s","value":NaN}' % wid),
        ("preview", "/sect-war/%s/preview" % wid, '{"value":NaN}'),
        ("contribute", "/sect-war/%s/contribute" % wid,
         '{"war_id":"%s","contribution_type":"funds","value":NaN}' % wid),
    ]
    for label, path, raw in probes:
        world["client"].post(
            path, content=raw.encode("utf-8"),
            headers={**war["atk"]["headers"], "Content-Type": "application/json",
                     "Idempotency-Key": _key()})

    for pid in payers:
        assert _stones(world, pid) == before_stones[pid], (
            "a non-finite body moved %s's wallet" % pid)
        assert sw.contributed_to(store, wid, pid) == before_ledger[pid], (
            "a non-finite body wrote a ledger row for %s" % pid)


# --------------------------------------------------------------------------
# The PURE layer. These DO raise, and that is the finding.
# --------------------------------------------------------------------------

def test_the_stake_recorder_rejects_what_it_cannot_convert() -> None:
    """The measured contract of ``record_war_payment``, value by value.

    MEASURED 2026-10-05 on this tree:

    ==========  =========================================================
    input       outcome
    ==========  =========================================================
    ``inf``     RAISES OverflowError
    ``-inf``    RAISES OverflowError
    ``nan``     silently ignored (records nothing)
    ``"abc"``   silently ignored
    ``None``    silently ignored
    ``1.9``     TRUNCATED to 1
    ==========  =========================================================

    The asymmetry is the finding, and it is a real one rather than a stylistic
    complaint. The recorder catches ``(TypeError, ValueError)`` around
    ``int(stones)``. ``int(float("nan"))`` raises ``ValueError`` so it is
    swallowed; ``int(float("inf"))`` raises ``OverflowError`` -- a DIFFERENT
    exception that the same ``except`` does not name -- so it escapes.

    Why the silent cases are correct and must stay: this recorder is the input
    gate for the payout ledger, and "record nothing" is the safe answer. A
    row that stored an unusable number would be read back by the settlement as
    a stake and could pay out. The tests below pin the IGNORE behaviour so a
    later edit that starts storing a junk value reds here.

    Why the OverflowError matters: this function is called from the settle
    path, not only from the validated HTTP body. A stored contribution row that
    reached it as a non-finite number would take the whole settlement down
    rather than skipping one member.
    """
    try:
        sw.record_war_payment("war_nf", "p_nan", math.nan, side="attacker")
    except (TypeError, ValueError, OverflowError):
        pass
    assert sw.contributed_to(None, "war_nf", "p_nan") == 0, (
        "a NaN stake was recorded; it would be read back as a payable stake")

    for junk in ("abc", None, "", [], {}):
        try:
            sw.record_war_payment("war_junk", "p_junk", junk, side="attacker")
        except (TypeError, ValueError, OverflowError):
            continue
        assert sw.contributed_to(None, "war_junk", "p_junk") == 0, (
            "an unusable stake %r was recorded" % (junk,))

    # A side is captured ONLY alongside a stake that was actually recorded.
    # A junk value must not leave a half-written side behind that a later read
    # would mistake for proof of participation.
    assert sw.paid_side("war_junk", "p_junk") == "", (
        "an unusable stake still captured a participation side")


def test_a_non_finite_stake_raises_rather_than_being_recorded(
        world: Dict[str, Any]) -> None:
    """The OverflowError, pinned as MEASURED BEHAVIOUR with its cause.

    This is a defect pin, not an endorsement. ``record_war_payment`` is the
    one function in this module whose whole job is to be total -- it swallows
    every other unusable input -- and it is the one input class it lets
    through as an exception.

    The test asserts the RAISE, so it is green today and states plainly what
    happens. The comment records the reason a reviewer should fix it: the
    ledger must not be able to take a settlement down. When the fix lands --
    adding ``OverflowError`` to the existing ``except`` tuple -- this test goes
    RED, which is the intended signal, and the fix belongs in
    ``sect_war.py`` rather than in this assertion.
    """
    with pytest.raises(OverflowError):
        sw.record_war_payment("war_ovf", "p_ovf", math.inf, side="attacker")

    # Nothing was recorded on the way out of the raise.
    assert sw.contributed_to(None, "war_ovf", "p_ovf") == 0
    assert sw.paid_side("war_ovf", "p_ovf") == ""


def test_the_payout_arithmetic_refuses_a_non_finite_stake() -> None:
    """``payout_for`` is the function that decides how many stones to mint.

    MEASURED: ``inf`` and ``-inf`` raise ``OverflowError``; ``nan`` raises
    ``ValueError``. All three are the same failure to a caller -- the settlement
    stops -- and all three are pinned here because the two exception TYPES
    differ and a fix for one would not fix the other.
    """
    for bad in (math.inf, -math.inf):
        with pytest.raises(OverflowError):
            sw.payout_for("winner", bad, 3000, 0)
    with pytest.raises(ValueError):
        sw.payout_for("winner", math.nan, 3000, 0)


def test_effort_share_refuses_a_non_finite_sector_total() -> None:
    """The effort share feeds the medal, so it must not divide by infinity.

    MEASURED: ``effort_share(inf, 0)``, ``effort_share(inf, inf)`` and
    ``effort_share(nan, 1)`` all raise rather than returning a share. A silent
    ``inf / inf -> nan`` here would reach the medal test as a comparison
    against ``MEDAL_SHARE``, and ``nan >= x`` is False -- so the safe-looking
    answer would be an arbitrary coin-flip on who won a medal.
    """
    with pytest.raises(OverflowError):
        sw.effort_share(math.inf, 0)
    with pytest.raises(OverflowError):
        sw.effort_share(math.inf, math.inf)
    with pytest.raises(ValueError):
        sw.effort_share(math.nan, 1)


def test_the_day_clock_refuses_a_non_finite_now() -> None:
    """The settlement clock reads a caller-supplied ``now`` in tests and jobs.

    A non-finite clock raises rather than settling every war or none. Pinned
    because a silent answer here is the worst kind: ``day_index(inf)`` folding
    to a real day index would make a whole day's settlement fire on one tick.
    """
    with pytest.raises((ValueError, OverflowError)):
        sw.day_index(math.inf)
    with pytest.raises((ValueError, OverflowError)):
        sw._is_due(math.nan)


def test_a_stored_non_finite_contribution_is_answered_as_not_paid(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A non-finite number in the STORE cannot become a payable stake.

    This is the money-path version of the test above, and it is the one that
    matters most: the HTTP body is validated by pydantic, but the store's own
    ``contributed_to_war`` is a plain method a caller cannot constrain. If an
    infinity stored there were read back as a stake, the settlement would price
    it -- and ``payout_for`` raises on exactly that value, so the failure
    would be a downed settlement rather than a skipped member.

    MEASURED: the poisoned reader raises inside ``contributed_to``, whose bare
    ``except`` answers the module's own ledger instead. The answer is 0 -- the
    SAFE one, because "paid nothing" prices at zero and pays nothing.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"

    class PoisonedStore:
        """The real store, except its contribution reader returns infinity."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def contributed_to_war(self, war_id: str, player_id: str) -> float:
            return math.inf

    poisoned = PoisonedStore(store)
    # First: with nothing legitimately paid, the poisoned reader must not win.
    assert sw.contributed_to(poisoned, wid, war["atk"]["player_id"]) == 0, (
        "an infinite stored contribution was reported as a real stake")

    # Second: once a REAL payment is on the module's own ledger, that ledger is
    # what the answer comes from -- not the infinity.
    sw.record_war_payment(wid, war["atk"]["player_id"], 3000, side="attacker")
    assert sw.contributed_to(poisoned, wid, war["atk"]["player_id"]) == 3000


# ==========================================================================
# 9. THE REPLAY MARKER -- measured across a real retry, with a control
# ==========================================================================
#
# `sect_war_contribute` checks `spent.get("_replayed")` BEFORE
# `spent.get("ok")`, and that ORDER is the entire fix. A replayed key returns
# the ORIGINAL stored receipt plus `_replayed: True`, and the stored receipt
# carries `ok: True` from the genuinely-paid first charge -- so a route that
# asks only `ok` reads a replay as a fresh success and applies the
# contribution a second time.
#
# The sibling file `test_sect_war_replay_regression.py` covers this at the
# wallet seam. What follows is the same property asserted at the ROUTE seam,
# where the money and the war score are both observable, plus the in-file
# control that proves these assertions can actually fail.


def test_a_replayed_contribution_key_charges_once_and_records_once(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """One key, one charge -- measured over HTTP, in one world.

    MEASURED on the shipped code: the second call answers 200 with an extra
    ``_replayed: true`` key, the wallet is unchanged by it, and the war score
    and the paid ledger both stay at the first call's figure. Deleting the
    ``_replayed`` branch inflates the score and the ledger to 6000 on one
    3000-stake payment -- proven by the control below.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    session = war["atk"]
    pid = session["player_id"]
    key = _key()
    body = {"war_id": wid, "contribution_type": "funds", "value": 3000}

    opening = _stones(world, pid)
    first = world["client"].post("/sect-war/%s/contribute" % wid, json=body,
                                 headers={**session["headers"],
                                          "Idempotency-Key": key})
    assert first.status_code == 200, first.text[:200]
    stones_after_first = _stones(world, pid)
    paid_after_first = sw.contributed_to(store, wid, pid)
    score_after_first = store.wars[wid].attacker_score

    assert stones_after_first == opening - 3000, (
        "the first charge moved %d, not 3000" % (opening - stones_after_first))
    assert "_replayed" not in first.json(), (
        "a genuinely fresh charge reported itself as a replay")

    second = world["client"].post("/sect-war/%s/contribute" % wid, json=body,
                                  headers={**session["headers"],
                                           "Idempotency-Key": key})
    assert second.status_code == 200, second.text[:200]

    # The reply is truth, not a fresh success: it SAYS it was a replay.
    assert second.json().get("_replayed") is True, (
        "the retry was answered as a fresh charge: %s" % second.json())

    # And the truth holds on the only two things that matter.
    assert _stones(world, pid) == stones_after_first, (
        "the replay moved the wallet again: %d -> %d"
        % (stones_after_first, _stones(world, pid)))
    assert sw.contributed_to(store, wid, pid) == paid_after_first, (
        "the replay recorded a second stake: %d -> %d"
        % (paid_after_first, sw.contributed_to(store, wid, pid)))
    assert store.wars[wid].attacker_score == score_after_first, (
        "the replay advanced the war score: %d -> %d"
        % (score_after_first, store.wars[wid].attacker_score))


def test_a_replay_reports_the_stored_amount_not_the_retried_one(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A retry carrying a DIFFERENT value reports what the wallet really moved.

    MEASURED: the first call pays 3000. A retry under the same key asking for
    9999 -- four times the declared ceiling's worth -- still answers
    ``stones_spent: 3000`` and still leaves the wallet where the first call
    put it. Reporting the request instead would tell the player they just paid
    9999 for a 3000 payment.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    session = war["atk"]
    pid = session["player_id"]
    key = _key()

    first = world["client"].post(
        "/sect-war/%s/contribute" % wid,
        json={"war_id": wid, "contribution_type": "funds", "value": 3000},
        headers={**session["headers"], "Idempotency-Key": key})
    assert first.status_code == 200, first.text[:200]
    after_first = _stones(world, pid)

    retry = world["client"].post(
        "/sect-war/%s/contribute" % wid,
        json={"war_id": wid, "contribution_type": "funds", "value": 9999},
        headers={**session["headers"], "Idempotency-Key": key})
    assert retry.status_code == 200, retry.text[:200]

    assert retry.json()["stones_spent"] == first.json()["stones_spent"] == 3000, (
        "the retry reported %s for a 3000-stone payment"
        % retry.json()["stones_spent"])
    assert _stones(world, pid) == after_first, (
        "the retry moved the wallet by %d" % (after_first - _stones(world, pid)))


def test_the_replay_marker_guard_bites_when_the_marker_is_removed(
        world: Dict[str, Any], war: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """THE CONTROL. Without this, the three tests above prove nothing.

    A green replay test is only evidence if the suite goes RED when the guard
    is deleted. This strips ``_replayed`` at the wallet -- the exact seam the
    route reads -- and asserts the defect COMES BACK.

    Measured on both arms:

    * guard intact  -> paid 3000, score 3000, wallet 50000 -> 47000
    * marker hidden -> paid 6000, score 6000, wallet 50000 -> 47000

    That is the vulnerability in one line: the score and the paid ledger --
    both payout inputs -- double on a payment that was charged once.

    WHY THE PATCH IS INSTALLED THE WAY IT IS. ``sect_war_contribute`` does
    ``from wallet_dispatch import earn, spend`` INSIDE the handler, so the
    name is resolved on every call and a monkeypatch on the bare
    ``wallet_dispatch`` module reaches the object the route actually calls.
    (This tree carries two module objects: bare ``wallet_dispatch`` and
    ``server.wallet_dispatch`` are DIFFERENT objects, MEASURED. Patching the
    qualified name would change nothing and this control would read as a
    pass.) The patch is scoped by ``monkeypatch``, and the assertion is on
    the DEFECT RETURNING -- not on the guard holding -- so this test stays
    green whether or not the defect exists.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    session = war["atk"]
    pid = session["player_id"]

    real_spend = wallet_dispatch.spend

    def hide_marker(store_arg, player_id, currency, amount, version, idem_key):
        """The real charge, with the wallet's own replay statement removed."""
        receipt = real_spend(store_arg, player_id, currency, amount, version,
                             idem_key)
        if isinstance(receipt, dict):
            receipt.pop("_replayed", None)
        return receipt

    monkeypatch.setattr(wallet_dispatch, "spend", hide_marker)

    key = _key()
    body = {"war_id": wid, "contribution_type": "funds", "value": 3000}
    world["client"].post("/sect-war/%s/contribute" % wid, json=body,
                         headers={**session["headers"], "Idempotency-Key": key})
    paid = sw.contributed_to(store, wid, pid)
    world["client"].post("/sect-war/%s/contribute" % wid, json=body,
                         headers={**session["headers"], "Idempotency-Key": key})
    inflated = sw.contributed_to(store, wid, pid)

    # The defect is EXPECTED here, so assert its shape, not its absence.
    assert paid == 3000, (
        "the first charge recorded %d, not the 3000 that was paid" % paid)
    assert inflated == 6000, (
        "hiding _replayed did NOT reproduce the defect (ledger reads %d, "
        "expected it to double to 6000) -- this control proves nothing and "
        "the three replay tests above are unproven" % inflated)
    assert store.wars[wid].attacker_score == 6000, (
        "the war score did not double either (reads %d)"
        % store.wars[wid].attacker_score)


def test_a_distinct_key_is_a_distinct_charge(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The guard must not over-fire: two keys, two charges.

    A replay guard that also blocked a SECOND, genuinely different payment
    would be a money bug of its own -- the player pays and nothing is recorded.

    MEASURED, and the ledger figure is worth stating plainly because it is not
    what the docstring on ``record_war_payment`` suggests. Two DISTINCT keys
    paying 500 then 2000 leave the ledger reading **2500** -- the sum -- not the
    2000 that the "idempotent by MAX" comment implies. That comment describes
    the retry case (the same payment recorded twice, which must not inflate),
    and it holds for that case; it does not describe two genuinely separate
    payments, which must both count.

    So the assertion here is the SUM, and the comment above it exists so a
    future reader does not "fix" it to 2000 against the neighbouring MAX test:
    the two tests pin different properties of the same function.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    session = war["atk"]
    pid = session["player_id"]

    opening = _stones(world, pid)
    for amount in (500, 2000):
        response = _contribute(world, session, wid, amount)
        assert response.status_code == 200, response.text[:200]
        assert response.json().get("_replayed") is not True, (
            "a distinct key was answered as a replay: %s" % response.json())

    assert _stones(world, pid) == opening - 2500, (
        "two distinct keys moved %d, not 2500"
        % (opening - _stones(world, pid)))
    assert sw.contributed_to(store, wid, pid) == 2500, (
        "the paid ledger reads %s; two genuinely separate payments must both "
        "count" % sw.contributed_to(store, wid, pid))
    assert store.wars[wid].attacker_score == 2500


# ==========================================================================
# 10. DECLARE'S AUTH BOUNDARY -- measured per header shape
# ==========================================================================
#
# The section above and `test_no_route_answers_without_a_session` prove the
# routes need A session. What follows pins the specific header shapes a caller
# actually sends, and the invariant that a refused declaration leaves the war
# table untouched -- a refusal that still created a war would be a state write
# behind a 401.


def test_declare_refuses_every_shape_of_missing_or_bad_credential(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Four credential shapes, one identical refusal.

    MEASURED -- all four answer 401 with the SAME detail string
    ``"invalid or missing session token"``:

    * no ``Authorization`` header at all
    * an empty header value
    * a bearer token that was never issued
    * a valid session token sent with the ``Bearer`` scheme stripped

    The IDENTICAL detail is the point. A caller who probes with a real token
    and a fake one cannot tell "you are not signed in" from "that token is
    wrong", so the 401 is not a session-existence oracle. A per-shape message
    ("missing header" vs "unknown token") would leak which sessions exist.
    """
    target = war["def_sect"]
    valid_token = war["atk"]["headers"]["Authorization"].split(" ", 1)[1]

    probes = [
        ("no header", {}),
        ("empty value", {"Authorization": ""}),
        ("never-issued token", {"Authorization": "Bearer wc_not_a_real_session"}),
        ("scheme stripped", {"Authorization": valid_token}),
        ("wrong scheme", {"Authorization": "Token " + valid_token}),
        ("bearer prefix only", {"Authorization": "Bearer "}),
    ]
    details = set()
    for label, header in probes:
        response = world["client"].post(
            "/sect-war/declare", json={"defender_sect_id": target},
            headers={**header, "Idempotency-Key": _key()})
        assert response.status_code == 401, (
            "%s answered %s, not 401" % (label, response.status_code))
        details.add(_detail(response))

    assert len(details) == 1, (
        "the 401 detail differs by credential shape (%d distinct messages), so "
        "a caller can tell an absent session from a wrong one" % len(details))


def test_a_refused_declare_writes_nothing(world: Dict[str, Any],
                                         war: Dict[str, Any]) -> None:
    """A 401 is a refusal of the REQUEST; the war table must prove it.

    Asserted on the store, not the reply. A handler that validated the caller
    after building the war would answer 401 and still leave a row behind, and
    the reply alone cannot tell those two apart.
    """
    store = world["sect_store"]
    before = set(store.wars)

    world["client"].post("/sect-war/declare",
                         json={"defender_sect_id": war["def_sect"]})
    assert set(store.wars) == before, (
        "an unauthenticated declare created %d war(s)"
        % len(set(store.wars) - before))


def test_declare_refuses_before_the_idempotency_key_is_consulted(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Auth first, then the key -- so an unauthenticated caller learns nothing
    about whether a key was valid.

    Two arms, same body, same path: one with no credential, one with a
    credential and no key. The first must answer 401 regardless of whether the
    key is present, which is what makes "you are not signed in" the only thing
    an anonymous probe can learn.
    """
    target = war["def_sect"]

    with_key_no_auth = world["client"].post(
        "/sect-war/declare", json={"defender_sect_id": target},
        headers={"Idempotency-Key": _key()})
    without_key_no_auth = world["client"].post(
        "/sect-war/declare", json={"defender_sect_id": target})

    assert with_key_no_auth.status_code == 401, with_key_no_auth.text[:200]
    assert without_key_no_auth.status_code == 401, (
        without_key_no_auth.text[:200])
    assert _detail(with_key_no_auth) == _detail(without_key_no_auth), (
        "the presence of an idempotency key changed the refusal an "
        "anonymous caller received")


# ==========================================================================
# 11. CONTRIBUTION TRACKING -- whose stake, whose side, whose answer
# ==========================================================================
#
# The ledger is keyed on (war_id, player_id), never on the sect. That is what
# stops one player's payment from becoming another player's payout input, and
# it is why every assertion below reads the store's own per-player answer
# rather than a sect-wide total.


def test_a_contribution_is_recorded_against_the_payer_and_nobody_else(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The stake lands on the PAYER's row and only the payer's.

    MEASURED: after the attacker pays 3000 and the defender 900, each player's
    own ``contributed_to`` reads their own figure, and each player's side is
    their own. A sect-wide total would read 3900 for both.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"

    atk_pid = war["atk"]["player_id"]
    def_pid = war["def"]["player_id"]

    paid_atk = _contribute(world, war["atk"], wid, 3000)
    assert paid_atk.status_code == 200, paid_atk.text[:200]
    paid_def = _contribute(world, war["def"], wid, 900)
    assert paid_def.status_code == 200, paid_def.text[:200]

    assert sw.contributed_to(store, wid, atk_pid) == 3000
    assert sw.contributed_to(store, wid, def_pid) == 900
    assert sw.paid_side(wid, atk_pid) == "attacker"
    assert sw.paid_side(wid, def_pid) == "defender"

    # Each player's own reply agrees with the store, and neither sees the
    # other's figure as their own.
    assert paid_atk.json()["your_paid"] == 3000
    assert paid_def.json()["your_paid"] == 900
    assert paid_atk.json()["your_side"] == "attacker"
    assert paid_def.json()["your_side"] == "defender"


def test_the_view_reads_back_the_viewers_own_stake_not_the_sects(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """``GET /sect-war/current`` reports YOUR paid stake, per player.

    MEASURED: with the attacker on 2000 and the defender on 900, each caller's
    ``your_paid`` is their own figure and their ``your_side`` is theirs. This
    is the read a client uses to show "you have contributed X", and a sect
    total there would tell the attacker they funded 2900 when they funded 2000.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    _contribute(world, war["atk"], wid, 2000)
    _contribute(world, war["def"], wid, 900)

    atk_view = world["client"].get("/sect-war/current",
                                   headers=war["atk"]["headers"])
    def_view = world["client"].get("/sect-war/current",
                                   headers=war["def"]["headers"])
    assert atk_view.status_code == 200, atk_view.text[:200]
    assert def_view.status_code == 200, def_view.text[:200]

    assert atk_view.json()["your_paid"] == 2000
    assert def_view.json()["your_paid"] == 900
    assert atk_view.json()["your_side"] == "attacker"
    assert def_view.json()["your_side"] == "defender"
    assert atk_view.json()["war_id"] == wid


def test_the_server_records_the_debited_amount_not_the_requested_one(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The ledger is written from the WALLET's number, so a body cannot lie.

    The request model has no ``paid`` field and the route records the value it
    debited. Pinned by asserting the ledger equals the balance DELTA for the
    same call: if the two ever disagree, one of them came from the client.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    session = war["atk"]
    pid = session["player_id"]

    opening = _stones(world, pid)
    response = _contribute(world, session, wid, 1234)
    assert response.status_code == 200, response.text[:200]

    debited = opening - _stones(world, pid)
    assert debited == 1234, "the wallet moved %d for a 1234 request" % debited
    assert response.json()["stones_spent"] == 1234
    assert sw.contributed_to(store, wid, pid) == 1234, (
        "the ledger recorded %d for a 1234 debit"
        % sw.contributed_to(store, wid, pid))
    assert store.wars[wid].attacker_score == 1234


def test_a_body_naming_another_player_is_refused_and_records_nothing(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """A forged ``player_id`` is refused, and the refusal writes nothing.

    MEASURED: 403, and the detail is ``"player_id does not match the
    authenticated session"`` -- the shipped ``auth`` ownership guard, NOT this
    module's own ``NOT_A_MEMBER`` string. The guard matches on the SESSION and
    fires before the handler, so naming another player in the body never
    reaches the route's membership check at all. Asserting the guard's string
    rather than the route's is what makes the test discriminating: if the
    guard were removed, the same body would answer with ``NOT_A_MEMBER`` and
    go red here.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    stranger = war["out"]
    pid = stranger["player_id"]
    opening = _stones(world, pid)

    response = world["client"].post(
        "/sect-war/%s/contribute" % wid,
        json={"war_id": wid, "contribution_type": "funds", "value": 500,
              "player_id": war["atk"]["player_id"]},
        headers={**stranger["headers"], "Idempotency-Key": _key()})

    assert response.status_code == 403, response.text[:200]
    # The OWNERSHIP guard's message, not the route's -- see the docstring.
    assert _detail(response) == "player_id does not match the authenticated session"
    assert _stones(world, pid) == opening, "a refused caller was charged"
    assert sw.contributed_to(store, wid, pid) == 0, (
        "a refused caller got a ledger row")


def test_a_refused_stranger_gets_the_routes_own_membership_refusal(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """With no forged field, the stranger reaches the ROUTE and gets ITS refusal.

    The counterpart to the test above, and the one that proves the route's own
    membership check is still load-bearing once the ownership guard has let the
    body through. MEASURED: 403 ``"Not a member of either warring sect"`` --
    the route's string, identical to the unknown-war and foreign-war refusals
    so none of the three is an oracle.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    stranger = war["out"]
    pid = stranger["player_id"]
    opening = _stones(world, pid)

    response = _contribute(world, stranger, wid, 500)
    assert response.status_code == 403, response.text[:200]
    assert _detail(response) == NOT_A_MEMBER
    assert _stones(world, pid) == opening
    assert sw.contributed_to(store, wid, pid) == 0


def test_a_declared_side_in_the_body_cannot_move_a_payment_to_the_other_side(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """The side is read from MEMBERSHIP, never from the body.

    MEASURED: an attacker who sends ``side: "defender"`` alongside a real
    500-stone payment still lands on the attacker's own row and the attacker's
    own side. The war score moves on the attacker's side. A forged side that
    took would let a player weight the war they are fighting.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    pid = war["atk"]["player_id"]

    response = world["client"].post(
        "/sect-war/%s/contribute" % wid,
        json={"war_id": wid, "contribution_type": "funds", "value": 500,
              "side": "defender"},
        headers={**war["atk"]["headers"], "Idempotency-Key": _key()})

    assert response.status_code == 200, response.text[:200]
    assert response.json()["your_side"] == "attacker"
    assert sw.paid_side(wid, pid) == "attacker"
    assert sw.contributed_to(store, wid, pid) == 500
    assert store.wars[wid].attacker_score == 500
    assert store.wars[wid].defender_score == 0


def test_a_rejected_stake_type_never_enters_the_payout_ledger(
        world: Dict[str, Any], war: Dict[str, Any]) -> None:
    """Only a PAID stake is recorded, because only a paid stake is paid out.

    MEASURED: ``contribution_type`` of ``materials``, ``combat``, ``"NaN"`` and
    ``"inf"`` are all refused 400 -- including the two that LOOK numeric,
    because the check is on the exact string, not on whether it parses as a
    number. A free contribution admitted to the ledger would be handed back as
    a payout, which is the hole the per-player map exists to close.
    """
    store = world["sect_store"]
    wid = war["war"]
    store.wars[wid].phase = "active"
    pid = war["atk"]["player_id"]
    opening = _stones(world, pid)

    for kind in ("materials", "combat", "NaN", "inf", "Infinity", "FUNDS"):
        response = _contribute(world, war["atk"], wid, 500, ctype=kind)
        assert response.status_code == 400, (
            "contribution_type %r answered %s, not 400"
            % (kind, response.status_code))
        assert sw.contributed_to(store, wid, pid) == 0, (
            "contribution_type %r wrote a ledger row" % kind)

    assert _stones(world, pid) == opening, (
        "a rejected stake type was charged")
