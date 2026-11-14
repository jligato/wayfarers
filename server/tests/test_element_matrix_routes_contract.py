"""Contract test for ``server/element_matrix_routes.py`` -- the five ``/elements`` routes.

WHY THIS FILE EXISTS
--------------------
The element matrix is one of the headless features: five live routes and no
client view anywhere in the repository. Nothing reads these responses, so the
wire shapes below have never been checked by anything except the module's own
docstrings. This file pins them, so the first Godot panel written against these
routes is written against measurement instead of against a guess.

Every status code, key set and ``detail`` string here was MEASURED on the live
app with ``fastapi.testclient.TestClient`` before this file was written. Nothing
is a prediction from reading the source. Reproduce the measurements with the two
commands under REPRODUCING THIS FILE.

WHAT IS PINNED, IN ONE LINE EACH
--------------------------------
* the five (method, path) pairs must stay mounted;
* the exact response key set of each route, and of every nested object;
* the exact request field names, and the fact that NO route accepts a
  caller-supplied cost, bonus, level, multiplier or amount;
* every refusal status, plus the rule that a refusal moves no stones;
* the maths: prices are a function of the server's own element list and of the
  level the SERVER holds, so a caller can neither pick a cheap element nor bid
  a level.

THE RELATION, NOT THE SNAPSHOT
------------------------------
Where a number could reasonably be retuned, this file asserts a relationship
instead of a literal. ``forge_cost`` is not pinned to "fire costs 250"; it is
pinned to be strictly increasing across the server's element list, anchored once
at ``BASE_FORGE_COST``. Adding a ninth element does not break this file. The
response key sets ARE frozen -- a key appearing or disappearing is a wire change
a mounted panel reads as a null -- but the values inside them are not.

KNOWN DEFECT PINNED: A RETRIED UPGRADE IS FREE
-----------------------------------------------
``POST /elements/upgrade`` retried with the SAME ``Idempotency-Key`` advances
the element's level a second time and debits nothing. Measured: one upgrade at
level 1 with key ``K`` moves the level 1 -> 2 and charges 240 stones; the
identical retry with key ``K`` moves it 2 -> 3, the wallet balance is UNCHANGED,
and the route still answers 200 with ``{"from_level": 2, "to_level": 3,
"cost": 360}``. ``stone_spent`` records 600 while the wallet was only ever
charged 240.

Mechanism: ``wallet_defense.spend`` returns the ORIGINAL stored response plus
``_replayed: True`` for a repeated key, and that stored response carries
``ok: True``. ``_spend`` in the route checks only ``result.get("ok")``, so a
replay passes the affordability gate and the route's own state write proceeds.

This is pinned as the CURRENT behaviour on purpose, exactly as the sibling
dungeon contract file pins its unknown-realm 500. The fix is one line in
``_spend`` (``if result.get("_replayed"): raise``). When it lands, the test below
goes red on purpose and the assertion is updated deliberately -- rather than the
fix slipping in under a ``!= 409`` style assertion that would have passed either
way. ``POST /elements/forge`` does NOT have this hole: a replayed forge is
refused, but by the ALREADY-FORGED state check, not by the key.

WHAT THE ORDERING CLAIM IS AND IS NOT WORTH -- MEASURED, NOT ASSUMED
-------------------------------------------------------------------
The module orders the ``_replayed`` check ABOVE the ``ok`` check, and its
docstring says the order is load-bearing. As a TEST property that claim is not
provable through this file, and the docstring used to assert otherwise. A
control that moved the ``_replayed`` branch physically below the ``ok`` check
left every test here GREEN (2 passed, 73 deselected). Reason: on a replay both
``ok`` and ``_replayed`` are true, so both orders reach the raise. The arm
proves the flag is READ; it cannot prove which read came first. Nothing is lost:
the two orders are observationally equivalent for the shapes the wallet actually
produces, and ``test_a_refused_charge_does_not_burn_the_callers_key`` pins the
wallet property that makes the differing shape unreachable.

KNOWN DEFECT PINNED: A CORRUPT STONE_SPEND IS A 500
---------------------------------------------------
``normalize_matrix`` coerces its three container fields defensively -- ``elements``,
``pairings`` and ``taught`` all degrade to empty rather than raising -- but feeds
``stone_spent`` and ``forged_at`` straight into ``int()``. A value no number can
be read from raises ``ValueError`` inside the route, so every one of the five
routes answers a bare 500 ``Internal Server Error``. The module's own docstring
promises that "a missing or corrupt block coerces to a usable empty state rather
than crashing a route"; that promise holds for the containers and is FALSE for
these two numbers. A string int() can read ("12") is accepted, so the defect is
specifically an unreadable value, not string-vs-number.

KNOWN DEFECT PINNED: THE ONE MUTUAL PAIR WINS BOTH ORDERS
--------------------------------------------------------
``element_relation`` checks the attacker's row before the defender's. For the
single mutual pair in the shipped table -- wind and lightning, each naming the
other -- both rows match, so the first check always wins and BOTH orders answer
``advantage`` with multiplier 1.3.

The module's own docstring for that function states the opposite: "a mutual pair
produces one advantage cell and one disadvantage cell, and checking the wrong row
first returns 1.3 twice", and names wind and lightning as the measured example.
The shipped code returns 1.3 twice.

Reachable? NO -- and the test proves why rather than assuming it.
``rule_resonance`` rejects a pair whose REVERSE is already stored, so the second
spelling never lands and the bonus cannot be collected twice. The defect is in a
PUBLIC helper, so it matters to whatever else calls it -- a combat formula, a
panel that wants to render a matchup -- not to this route's payout.

ORDERING HAZARD -- READ BEFORE ADDING A TEST
--------------------------------------------
Every arm builds its own player. Nothing here shares a matrix, a wallet or a
companion, so no arm can be poisoned by a neighbouring one. Do not refactor
these into module-level fixtures without re-measuring.

REPRODUCING THIS FILE
---------------------
    cd G:/Projects/wayfarers-cycle
    WC_STORAGE_BACKEND=memory WC_PG_HOST= python -m pytest \\
        server/tests/test_element_matrix_routes_contract.py -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.main import app, players_db, wallet_store  # noqa: E402
import auth as auth_module  # noqa: E402
import element_matrix_routes as emr  # noqa: E402
import wallet_dispatch  # noqa: E402
from server.tests.wc_test_auth import clear_signup_rate_limit  # noqa: E402

# `import auth` BARE, not `server.auth`. server/main.py binds the bare module,
# and the session store is a per-module singleton, so a package-qualified import
# mints tokens into a store the routes never read and every owner arm 401s while
# the refusal arms stay green. Same reason as the comment in
# server/tests/test_dungeon_routes_contract.py.
assert auth_module is sys.modules["auth"], (
    "bare `auth` and the module main.py bound must be the same object, or every "
    "minted session 401s"
)

RUN_TAG = uuid.uuid4().hex[:8]

# ---------------------------------------------------------------------------
# The five routes, as they must appear on the mounted app.
# ---------------------------------------------------------------------------
EXPECTED_ROUTES = {
    ("GET", "/elements/state"),
    ("POST", "/elements/forge"),
    ("POST", "/elements/resonance"),
    ("POST", "/elements/upgrade"),
    ("POST", "/elements/inheritance"),
}

#: The request-body field names. These are the contract a client codes against;
#: a rename breaks every caller silently.
BODY_FIELDS = {
    "POST /elements/forge": {"element"},
    "POST /elements/resonance": {"first", "second"},
    "POST /elements/upgrade": {"element"},
    "POST /elements/inheritance": {"element", "companion_id"},
}

#: Fields no route accepts. Every model is ``extra="forbid"``, so a caller that
#: sends one gets 422 rather than a silently ignored bonus. Measured.
CALLER_SUPPLIED_FIELDS = ("amount", "bonus", "cost", "level", "multiplier",
                          "power", "player_id", "now")

# ---------------------------------------------------------------------------
# Response key sets, measured from live bodies.
# ---------------------------------------------------------------------------
STATE_KEYS = {
    "ok", "version", "server_now", "elements", "non_elemental_tags",
    "multipliers", "max_level", "forged", "pairings", "total_bonus", "taught",
    "stone_spent",
}
#: `forged` is a LIST here. `POST /elements/forge` adds its own `forged_element`
#: string beside it -- the module's docstring records a shipped collision where
#: that key overwrote the list with the string "fire".
#: ``beats`` and ``beaten_by`` publish the SAME list under two names.
#: ``beaten_by`` is the older, MISLEADING name -- it holds the elements this
#: one beats, not the ones that beat it. It is kept so the shipped panel keeps
#: rendering; see the module docstring. Both are asserted equal in the arm below.
FORGED_ENTRY_KEYS = {"element", "level", "multiplier_vs_neutral", "counters",
                     "beats", "beaten_by", "upgrade_cost"}
PAIRING_ENTRY_KEYS = {"elements", "relation", "multiplier", "bonus"}
TAUGHT_ENTRY_KEYS = {"element", "companion_id", "bonus", "taught_at"}

FORGE_KEYS = STATE_KEYS | {"forged_element", "cost"}
RESONANCE_KEYS = STATE_KEYS | {"resonance"}
UPGRADE_KEYS = STATE_KEYS | {"upgrade"}
INHERITANCE_KEYS = STATE_KEYS | {"inheritance"}

RESONANCE_DECISION_KEYS = {"pair", "relation", "multiplier", "bonus"}
UPGRADE_DECISION_KEYS = {"element", "from_level", "to_level", "cost"}
INHERITANCE_DECISION_KEYS = {"element", "companion_id", "bonus"}

#: The refusal `detail` strings that are not a validation error. Each one NAMES
#: the thing the caller asked for, so they are templates, not fixed literals --
#: a test that hardcoded "fire is already forged" and then forged metal would be
#: asserting the wrong element's name.
REFUSAL_TEMPLATES = {
    "already_forged": "%s is already forged",
    "non_elemental": "%s is a non-elemental tag and cannot be forged",
    "not_forged": "%s is not forged yet",
    "self_resonance": "an element cannot resonate with itself",
    "already_paired": "%s and %s are already paired",
    "at_max_level": "%s is already at the maximum level %d",
    "not_your_companion": "that companion is not yours",
    "already_taught": "%s is already taught to that companion",
    "insufficient": "insufficient balance",
}

#: The one refusal raised by ``_spend`` for a REPEATED idempotency key, as
#: opposed to a wallet that simply said no. Kept as a named constant because two
#: arms assert it and a client matches on it: a 409 is already used by every
#: other refusal in this file, so the string is the only thing that
#: distinguishes "your key was already spent" from "you cannot afford this".
REPLAY_REFUSAL = (
    "this idempotency key was already used for this purchase; "
    "retry with a fresh key"
)


# ======================================================================================
# HARNESS
# ======================================================================================
@pytest.fixture(scope="module")
def client():
    """The real mounted app, under its lifespan.

    ``raise_server_exceptions=False`` so a genuine 500 is OBSERVED as a 500
    rather than exploding out of the test as a traceback.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _session(player_id: str) -> dict:
    """Bearer headers for one named player.

    Opt-in per call site on purpose: a blanket session would make this file
    structurally unable to observe the anonymous case, which is a third of the
    contract.
    """
    return {"Authorization": f"Bearer {auth_module.issue_session(player_id)}"}


def _idem(tag: str) -> dict:
    """A fresh Idempotency-Key. Never a module constant: a shared key would make
    a second identical request look like a replay and be refused for the wrong
    reason."""
    return {"Idempotency-Key": f"emcontract-{RUN_TAG}-{tag}-{uuid.uuid4().hex[:8]}"}


def _new_player(client, role: str) -> str:
    """One fresh player, created through the real route.

    ``clear_signup_rate_limit`` first: ``/player/create`` refuses more than five
    signups from one address inside a minute, and this file creates many more
    than that.
    """
    clear_signup_rate_limit()
    made = client.post("/player/create", json={
        "name": f"EmContract{role}{RUN_TAG}{uuid.uuid4().hex[:4]}",
        "spirit_root": "fire"})
    assert made.status_code == 200, made.text
    return made.json()["id"]


def _stones(player_id: str) -> int:
    """The wallet balance, read from the STORE.

    Never read the amount out of a response body. A route that reported a cost it
    never charged would pass every status-code assertion in this file.
    """
    account = wallet_store.get(player_id)
    balance = getattr(account, "balance", account)
    if hasattr(balance, "to_dict"):
        balance = balance.to_dict()
    return balance.get("spirit_stone", 0)


def _fund(player_id: str, amount: int) -> None:
    """Put spendable stones on the player."""
    result = wallet_dispatch.earn(wallet_store, player_id, "spirit_stone",
                                  amount, f"emfund-{uuid.uuid4().hex}")
    assert result.get("ok"), result


def _stored(player_id: str) -> dict:
    """The player's persisted matrix block, read inside a transaction."""
    with players_db.transaction(player_id):
        return dict(players_db[player_id].get(emr.MATRIX_KEY) or {})


def _seed_matrix(player_id: str, **fields) -> None:
    """Write a matrix block straight into the store, so a test does not have to
    forge and upgrade its way to the state it wants to observe."""
    state = emr.blank_matrix()
    state.update(fields)
    with players_db.transaction(player_id):
        row = players_db[player_id]
        row[emr.MATRIX_KEY] = state
        players_db.persist(player_id, row)


def _coerced(player_id: str, field: str) -> object:
    """The stored value of one counter field AFTER ``normalize_matrix`` ran.

    Reads the raw block and passes it through the module's own normaliser, so
    this reports what the server would read rather than what was written. Used
    for ``forged_at``, which is deliberately not published on the wire.
    """
    return emr.normalize_matrix(_stored(player_id))[field]


def _add_companion(player_id: str, beasts: dict) -> None:
    with players_db.transaction(player_id):
        row = players_db[player_id]
        row["beasts"] = beasts
        players_db.persist(player_id, row)


def _rich(client, role: str, *, stones: int = 100_000, **matrix) -> str:
    """A funded player with a seeded matrix. One call per arm."""
    pid = _new_player(client, role)
    _fund(pid, stones)
    if matrix:
        _seed_matrix(pid, **matrix)
    return pid


# ======================================================================================
# 1. THE MOUNT
# ======================================================================================
def test_all_five_routes_are_mounted(client):
    """A dropped or renamed route reds here rather than silently shrinking the
    surface a client panel can reach."""
    live = {
        (m, r.path)
        for r in client.app.routes
        for m in (getattr(r, "methods", None) or ())
        if r.path.startswith("/elements")
    }
    assert live == EXPECTED_ROUTES


def test_registration_reports_the_paths_it_mounted():
    """``register_element_matrix_routes`` returns its own paths so a caller can
    assert the mount landed without re-walking the route table."""
    from fastapi import FastAPI

    probe_app = FastAPI()
    result = emr.register_element_matrix_routes(probe_app, lambda: None, lambda: None)
    assert set(result["registered"]) == {p for _, p in EXPECTED_ROUTES}


def test_the_five_routes_appear_in_the_openapi_spec(client):
    """The request models are declared at MODULE scope on purpose: a model
    declared inside the register function breaks the whole spec."""
    spec = client.get("/openapi.json")
    assert spec.status_code == 200, spec.text[:400]
    paths = {p for p in spec.json()["paths"] if p.startswith("/elements")}
    assert paths == {p for _, p in EXPECTED_ROUTES}


# ======================================================================================
# 2. AUTH -- declared by hand, so the shape-based guard cannot cover them
# ======================================================================================
def test_every_route_refuses_an_anonymous_caller(client):
    """These routes name no player, so the central shape-matching ownership guard
    skips all five. The hand-written Depends is the only thing between them and
    the open internet."""
    bodies = {
        "/elements/forge": {"element": "fire"},
        "/elements/resonance": {"first": "fire", "second": "water"},
        "/elements/upgrade": {"element": "fire"},
        "/elements/inheritance": {"element": "fire", "companion_id": "c1"},
    }
    for path in ("/elements/state",):
        assert client.get(path).status_code == 401
    for path, body in bodies.items():
        headers = _idem("anon") if path in ("/elements/forge", "/elements/upgrade") else {}
        got = client.post(path, json=body, headers=headers)
        assert got.status_code == 401, (path, got.status_code, got.text)
        assert got.json()["detail"] == "invalid or missing session token"


def test_a_garbage_token_is_refused_on_every_route(client):
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/elements/state", headers=bad).status_code == 401
    assert client.post("/elements/forge", json={"element": "fire"},
                       headers={**bad, **_idem("bad")}).status_code == 401
    assert client.post("/elements/resonance", json={"first": "fire", "second": "water"},
                       headers=bad).status_code == 401
    assert client.post("/elements/upgrade", json={"element": "fire"},
                       headers={**bad, **_idem("bad2")}).status_code == 401
    assert client.post("/elements/inheritance",
                       json={"element": "fire", "companion_id": "c1"},
                       headers=bad).status_code == 401


def test_a_session_for_a_player_that_does_not_exist_is_refused(client):
    """A valid token over a missing row is 401, not 404 and not 500.

    MEASURED 2026-10-05 against the real app. ``auth.authenticate`` confirms
    the session's player still exists and revokes the token when it does not,
    so this answers 401 before either route runs. That is the deleted-player
    defense: a token for an account that was never created (or has since been
    deleted) must stop working, and the refusal must not tell a caller whether
    the id is live.

    Paired with ``test_a_real_player_reads_their_own_matrix`` below, so this
    cannot be satisfied by refusing everyone.
    """
    ghost = f"no_such_player_{uuid.uuid4().hex[:8]}"
    state = client.get("/elements/state", headers=_session(ghost))
    assert state.status_code == 401
    forged = client.post("/elements/forge", json={"element": "fire"},
                         headers={**_session(ghost), **_idem("ghost")})
    assert forged.status_code == 401


def test_a_player_id_supplied_by_the_caller_cannot_redirect_the_state_route(client):
    """The central ownership guard reads a query or body field named
    ``player_id``. Presenting one here is refused before the route runs, so the
    answer can never be another player's matrix."""
    pid = _rich(client, "queryclaim", stones=0, elements={"fire": 1})
    other = _rich(client, "queryowner", stones=0, elements={"water": 4})
    got = client.get("/elements/state", params={"player_id": other},
                     headers=_session(pid))
    assert got.status_code == 403, got.text
    assert got.json()["detail"] == "player_id does not match the authenticated session"


def test_one_player_cannot_read_another_players_matrix(client):
    """The two matrices are independent. Measured against the store, not against
    a reply body."""
    owner = _rich(client, "isoowner", stones=0, elements={"fire": 3})
    stranger = _rich(client, "isostanger", stones=0, elements={"void": 7})
    got = client.get("/elements/state", headers=_session(stranger))
    assert got.status_code == 200
    assert [e["element"] for e in got.json()["forged"]] == ["void"]
    assert client.get("/elements/state", headers=_session(owner)).json()[
        "forged"][0]["level"] == 3


# ======================================================================================
# 3. THE STATE ROUTE
# ======================================================================================
def test_state_key_set_on_a_brand_new_player(client):
    pid = _rich(client, "fresh", stones=0)
    got = client.get("/elements/state", headers=_session(pid))
    assert got.status_code == 200
    assert set(got.json()) == STATE_KEYS


def test_state_is_empty_but_usable_on_a_brand_new_player(client):
    pid = _rich(client, "freshshape", stones=0)
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert body["ok"] is True
    assert body["forged"] == [] and body["pairings"] == [] and body["taught"] == []
    assert body["stone_spent"] == 0 and body["total_bonus"] == 0.0


def test_state_publishes_the_server_owned_element_list_not_a_caller_choice(client):
    """The element list, the tags and the multiplier ladder are the server's. A
    client is told what exists; it is never asked to supply it."""
    pid = _rich(client, "catalogue", stones=0)
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert body["elements"] == emr.ELEMENTS
    assert body["non_elemental_tags"] == sorted(emr.NON_ELEMENTAL_TAGS)
    assert body["multipliers"] == dict(emr.ELEMENT_MULTIPLIER)
    assert body["version"] == emr.MATRIX_VERSION
    assert body["max_level"] == emr.MAX_ELEMENT_LEVEL


def test_every_forged_entry_carries_the_full_key_set_and_server_maths(client):
    """``counters`` and ``beaten_by`` are the two halves of the advantage table
    and must be inverse to each other -- a forged element is not "uncounterable"
    by accident, and ``beaten_by`` cannot be empty for an element with an entry
    in the table."""
    pid = _rich(client, "forgedentry", stones=0,
                elements={"fire": 2, "metal": 5, "void": 1})
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert len(body["forged"]) == 3
    for entry in body["forged"]:
        assert set(entry) == FORGED_ENTRY_KEYS
        assert 1 <= entry["level"] <= emr.MAX_ELEMENT_LEVEL
        assert entry["multiplier_vs_neutral"] == \
            float(emr.ELEMENT_MULTIPLIER["neutral"])
        if entry["element"] in emr.ELEMENT_ADVANTAGE:
            assert entry["beaten_by"] == [emr.ELEMENT_ADVANTAGE[entry["element"]]]
        else:
            assert entry["beaten_by"] == []
        assert set(entry["counters"]) == {
            a for a in emr.ELEMENTS
            if emr.ELEMENT_ADVANTAGE.get(a) == entry["element"]}
    # and the levels really are the ones stored
    assert {e["element"]: e["level"] for e in body["forged"]} == \
        {"fire": 2, "metal": 5, "void": 1}


def test_an_element_at_the_level_cap_reports_no_next_price(client):
    """``upgrade_cost`` is null at the cap. A client that renders it as "0" would
    offer a free upgrade button."""
    pid = _rich(client, "capped", stones=0,
                elements={"fire": emr.MAX_ELEMENT_LEVEL})
    entry = client.get("/elements/state", headers=_session(pid)).json()["forged"][0]
    assert entry["level"] == emr.MAX_ELEMENT_LEVEL
    assert entry["upgrade_cost"] is None


def test_the_price_of_the_next_level_is_quoted_before_the_caller_buys_it(client):
    pid = _rich(client, "nextprice", stones=0, elements={"fire": 3})
    entry = client.get("/elements/state", headers=_session(pid)).json()["forged"][0]
    assert entry["upgrade_cost"] == emr.upgrade_cost(3)


def test_total_bonus_is_the_sum_of_the_pairing_bonuses(client):
    """Not a literal -- a client sums these itself, so a route that reported a
    different total would draw a number the pairing list does not support."""
    pid = _rich(client, "totalbonus", stones=0,
                elements={"fire": 1, "metal": 1, "earth": 1, "wood": 1})
    client.post("/elements/resonance", json={"first": "fire", "second": "metal"},
                headers=_session(pid))
    client.post("/elements/resonance", json={"first": "wood", "second": "earth"},
                headers=_session(pid))
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert len(body["pairings"]) == 2
    for pairing in body["pairings"]:
        assert set(pairing) == PAIRING_ENTRY_KEYS
    assert body["total_bonus"] == pytest.approx(
        sum(p["bonus"] for p in body["pairings"]), abs=1e-6)


def test_taught_entries_carry_the_full_key_set(client):
    pid = _rich(client, "taughtshape", stones=0, elements={"fire": 4})
    _add_companion(pid, {"w1": {"id": "w1"}})
    got = client.post("/elements/inheritance",
                      json={"element": "fire", "companion_id": "w1"},
                      headers=_session(pid))
    assert got.status_code == 200
    taught = got.json()["taught"]
    assert len(taught) == 1
    assert set(taught[0]) == TAUGHT_ENTRY_KEYS
    assert taught[0]["element"] == "fire" and taught[0]["companion_id"] == "w1"


# ======================================================================================
# 4. FORGE -- the caller names WHICH element, never what it costs
# ======================================================================================
def test_forge_key_set_and_the_list_string_collision_is_avoided(client):
    """``forged`` stays a LIST and the newly forged name arrives under its own
    key. A shipped collision once made ``forged`` the string "fire"."""
    pid = _rich(client, "forgekeys")
    got = client.post("/elements/forge", json={"element": "fire"},
                      headers={**_session(pid), **_idem("fk")})
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == FORGE_KEYS
    assert isinstance(body["forged"], list)
    assert body["forged_element"] == "fire"
    assert body["cost"] == emr.forge_cost("fire")


def test_forge_debits_the_wallet_and_the_matrix_records_the_same_spend(client):
    """Both halves proven against the STORE. The reply's ``cost`` is cross-checked
    but is never the only evidence."""
    pid = _rich(client, "forgemoney")
    before = _stones(pid)
    got = client.post("/elements/forge", json={"element": "fire"},
                      headers={**_session(pid), **_idem("fm")})
    assert got.status_code == 200, got.text
    price = emr.forge_cost("fire")
    assert _stones(pid) == before - price, "the wallet was not debited by the price"
    state = _stored(pid)
    assert state["elements"] == {"fire": 1}
    assert state["stone_spent"] == price
    assert state["forged_at"] > 0


def test_the_price_of_a_forge_is_a_function_of_the_server_element_list(client):
    """Not "fire costs 250" -- the whole ladder is strictly increasing, so a ninth
    element cannot be cheaper than the eighth, and the cheapest element's price is
    the declared base."""
    costs = [emr.forge_cost(e) for e in emr.ELEMENTS]
    assert costs == sorted(costs) and len(set(costs)) == len(costs)
    assert all(b > a for a, b in zip(costs, costs[1:]))
    assert costs[0] == emr.BASE_FORGE_COST
    assert all(cost == emr.BASE_FORGE_COST * (i + 1)
               for i, cost in enumerate(costs))


def test_a_more_expensive_element_costs_more_over_http(client):
    """The ladder is not a module detail a route could ignore."""
    pid = _rich(client, "ladder")
    costs = {}
    for index, element in enumerate(("fire", "earth", "void")):
        got = client.post("/elements/forge", json={"element": element},
                          headers={**_session(pid), **_idem(f"ld{index}")})
        assert got.status_code == 200, got.text
        costs[element] = got.json()["cost"]
    assert costs == {e: emr.forge_cost(e) for e in ("fire", "earth", "void")}
    assert costs["fire"] < costs["earth"] < costs["void"]


def test_a_forge_requires_an_idempotency_key_the_server_will_not_mint(client):
    """No key -> 400. The server minting one is what double-charges a retry."""
    pid = _rich(client, "forgeidem")
    got = client.post("/elements/forge", json={"element": "fire"},
                      headers=_session(pid))
    assert got.status_code == 400, got.text
    assert got.json()["detail"].startswith("Idempotency-Key header required")
    assert "element-forge" in got.json()["detail"]


def test_a_blank_idempotency_key_is_refused_the_same_way(client):
    """A present-but-empty header is falsy, so it must not fall through."""
    pid = _rich(client, "forgeblank")
    got = client.post("/elements/forge", json={"element": "fire"},
                      headers={**_session(pid), "Idempotency-Key": "   "})
    assert got.status_code == 400
    assert got.json()["detail"].startswith("Idempotency-Key header required")


def test_an_oversized_idempotency_key_is_refused(client):
    """Bound measured at 128 characters by idem_gate.MAX_IDEMPOTENCY_KEY_LEN."""
    pid = _rich(client, "forgelong")
    got = client.post("/elements/forge", json={"element": "fire"},
                      headers={**_session(pid),
                               "Idempotency-Key": "x" * (emr.__dict__.get("_", 0) or 200)})
    assert got.status_code == 400
    assert "too long" in got.json()["detail"]


def test_a_replayed_forge_is_refused_and_charges_nothing(client):
    """The second identical forge is caught by the ALREADY-FORGED state check, so
    the key is not what saves it. Asserted separately from the upgrade replay
    below, which is NOT caught."""
    pid = _rich(client, "forgereplay")
    key = _idem("fr")
    first = client.post("/elements/forge", json={"element": "fire"},
                        headers={**_session(pid), **key})
    assert first.status_code == 200, first.text
    after_first = _stones(pid)
    second = client.post("/elements/forge", json={"element": "fire"},
                         headers={**_session(pid), **key})
    assert second.status_code == 409
    assert second.json()["detail"] == "fire is already forged"
    assert _stones(pid) == after_first


def test_a_retried_upgrade_with_the_same_key_is_refused(client):
    """A retried upgrade with the SAME key is refused, and moves nothing.

    This was a KNOWN DEFECT pinned red. The wallet's replay response carries
    ``ok: True`` from the first, genuinely-paid charge, so an ``_spend`` that
    checked only ``ok`` let the replay through: the level advanced a second time
    for free and ``stone_spent`` recorded a spend that never happened.
    MEASURED before the fix: replay 200, level 1 -> 2 -> 3, wallet balance
    unchanged, ``stone_spent`` 600 against 240 actually paid.

    ``_spend`` now checks the wallet's own ``_replayed`` flag BEFORE ``ok``,
    because ``_replayed`` is the statement that no money moved. This arm now
    pins the FIXED shape, and the negative control at the bottom of this file
    proves the arm can still go red when the guard is removed.
    """
    pid = _rich(client, "upgreplay", elements={"fire": 1})
    key = _idem("ur")
    first = client.post("/elements/upgrade", json={"element": "fire"},
                        headers={**_session(pid), **key})
    assert first.status_code == 200, first.text
    charged_after_first = _stones(pid)
    assert first.json()["upgrade"]["to_level"] == 2
    # The one real purchase moved the level and charged exactly once.
    assert charged_after_first < 100_000

    replay = client.post("/elements/upgrade", json={"element": "fire"},
                         headers={**_session(pid), **key})
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"] == REPLAY_REFUSAL
    # A refusal moves no stones and does not advance the level.
    assert _stones(pid) == charged_after_first
    assert _stored(pid)["elements"]["fire"] == 2
    # And the two books agree: the matrix counted exactly what was charged.
    recorded = _stored(pid)["stone_spent"]
    actually_paid = 100_000 - _stones(pid)
    assert recorded == actually_paid, (recorded, actually_paid)

    # A FRESH key is a genuine second purchase -- the guard refuses the KEY,
    # not the action.
    again = client.post("/elements/upgrade", json={"element": "fire"},
                        headers={**_session(pid), **_idem("ur2")})
    assert again.status_code == 200, again.text
    assert again.json()["upgrade"]["to_level"] == 3
    assert _stones(pid) < charged_after_first


def test_the_replay_guard_is_checked_before_the_ok_flag(client, monkeypatch):
    """The negative control for the arm above: the guard reads ``_replayed``.

    Replaces the wallet's ``spend`` with one that answers the ORIGINAL stored
    response plus ``_replayed: True`` -- exactly what a repeated key returns --
    and ``ok: True``, because that stored response came from a real charge. A
    guard that checked ONLY ``ok`` would let this through and hand the level out
    again for free; a guard that checks ``_replayed`` refuses it.

    The docstring here used to claim this arm also pinned the ORDER of the two
    checks ("``_spend`` checks ``_replayed`` BEFORE ``ok``"). MEASURED, and that
    claim is FALSE as a test property: a control that moved the ``_replayed``
    branch physically below the ``ok`` check left every test in this file GREEN
    (2 passed, 73 deselected, byte-identical apart from the reordering). The
    reason is that ``ok: True`` and ``_replayed: True`` are BOTH true on a
    replay, so both orders reach the raise -- the arm proves the flag is READ,
    never which read came first.

    The ordering is not observable from outside either, and that is a property
    of the wallet rather than a gap here. MEASURED: replaying a key whose
    original charge was REFUSED (empty wallet) does not answer with the wallet's
    stored refusal, so there is no ``ok: False`` + ``_replayed: True`` shape for
    the two orders to disagree about. See
    ``test_a_refused_charge_does_not_burn_the_callers_key`` for the measured
    behaviour of a refused-then-retried key.
    """
    monkeypatch.setattr(wallet_dispatch, "spend",
                        lambda *a, **k: {"ok": True, "_replayed": True})
    pid = _rich(client, "replayctl", elements={"fire": 1})
    got = client.post("/elements/upgrade", json={"element": "fire"},
                      headers={**_session(pid), **_idem("ctl")})
    assert got.status_code == 409, (
        "a replayed charge was accepted -- the guard reads ok and ignores "
        "_replayed, which is the original defect")
    assert got.json()["detail"] == REPLAY_REFUSAL
    assert _stored(pid)["elements"] == {"fire": 1}, "the level advanced on a replay"


def test_a_refused_charge_does_not_burn_the_callers_key(client):
    """A key the wallet REFUSED can be used again once the player can afford it.

    MEASURED, because the arm above cannot observe the order of the two checks
    and this is the observable question instead. An empty-wallet forge answers
    409 "insufficient balance"; the wallet stores no charge for a refusal, so
    funding the player and retrying with the SAME key is a genuine first
    purchase and answers 200.

    This is the property that makes the refused-replay shape unreachable: there
    is no ``ok: False`` + ``_replayed: True`` reply for a refused key to produce.
    Pinning it also stops a future "fix" that starts recording refusals from
    silently locking a player out of a key they legitimately hold.
    """
    clear_signup_rate_limit()
    pid = client.post("/player/create", json={
        "name": f"EmRefusedKey{RUN_TAG}{uuid.uuid4().hex[:4]}",
        "spirit_root": "fire"}).json()["id"]
    assert _stones(pid) == 0, "this arm measures the empty-wallet refusal"

    key = _idem("refused")
    refused = client.post("/elements/forge", json={"element": "fire"},
                          headers={**_session(pid), **key})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == REFUSAL_TEMPLATES["insufficient"]

    _fund(pid, 100_000)
    retried = client.post("/elements/forge", json={"element": "fire"},
                          headers={**_session(pid), **key})
    assert retried.status_code == 200, retried.text
    assert retried.json()["forged_element"] == "fire"
    assert _stored(pid)["elements"] == {"fire": 1}

    # And the key IS then spent: replaying it a second time is refused, so the
    # key was stored by the successful charge and not by the refusal.
    again = client.post("/elements/forge", json={"element": "water"},
                        headers={**_session(pid), **key})
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == REPLAY_REFUSAL


def test_forging_an_element_twice_is_refused_and_moves_no_stones(client):
    pid = _rich(client, "forgedup")
    client.post("/elements/forge", json={"element": "metal"},
                headers={**_session(pid), **_idem("d1")})
    after = _stones(pid)
    again = client.post("/elements/forge", json={"element": "metal"},
                        headers={**_session(pid), **_idem("d2")})
    assert again.status_code == 409
    assert again.json()["detail"] == "metal is already forged"
    assert _stones(pid) == after


def test_element_names_are_normalised_rather_than_guessed_at(client):
    """``"  METAL "`` resolves as metal. A client with a formatting bug gets a
    clear result, not a silent neutral."""
    pid = _rich(client, "casefold")
    got = client.post("/elements/forge", json={"element": "  METAL "},
                      headers={**_session(pid), **_idem("cf")})
    assert got.status_code == 200, got.text
    assert got.json()["forged_element"] == "metal"
    assert list(_stored(pid)["elements"]) == ["metal"]


def test_an_unknown_element_is_a_400_naming_the_field_and_the_table(client):
    """Never a silent 1.0. The message must name the offending field so a client
    developer can find it."""
    pid = _rich(client, "unknown")
    got = client.post("/elements/forge", json={"element": "aether"},
                      headers={**_session(pid), **_idem("un")})
    assert got.status_code == 400
    detail = got.json()["detail"]
    assert detail.startswith("element='aether' is not a combat element")
    for element in emr.ELEMENTS:
        assert element in detail
    assert _stored(pid).get("elements", {}) == {}


def test_a_non_elemental_tag_resolves_but_cannot_be_forged(client):
    """The game's own vocabulary for "no element" is accepted by the validator and
    then refused by the rule -- a 409, distinct from the unknown-name 400."""
    pid = _rich(client, "tag")
    for tag in sorted(emr.NON_ELEMENTAL_TAGS):
        got = client.post("/elements/forge", json={"element": tag},
                          headers={**_session(pid), **_idem(f"tg{tag}")})
        assert got.status_code == 409, (tag, got.status_code, got.text)
        assert got.json()["detail"] == \
            REFUSAL_TEMPLATES["non_elemental"] % tag


def test_an_unaffordable_forge_is_refused_and_moves_no_stones(client):
    pid = _new_player(client, "poor")
    assert _stones(pid) == 0
    got = client.post("/elements/forge", json={"element": "metal"},
                      headers={**_session(pid), **_idem("pf")})
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["insufficient"]
    assert _stones(pid) == 0
    assert _stored(pid).get("elements", {}) == {}


# ======================================================================================
# 5. UPGRADE -- the level bought is the server's
# ======================================================================================
def test_upgrade_key_set_and_the_three_numbers_the_server_owns(client):
    pid = _rich(client, "upgradekeys", elements={"fire": 3})
    got = client.post("/elements/upgrade", json={"element": "fire"},
                      headers={**_session(pid), **_idem("uk")})
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == UPGRADE_KEYS
    assert set(body["upgrade"]) == UPGRADE_DECISION_KEYS
    assert body["upgrade"]["from_level"] == 3
    assert body["upgrade"]["to_level"] == 4


def test_an_upgrade_debits_exactly_the_server_quoted_price(client):
    pid = _rich(client, "upgrademoney", elements={"fire": 3})
    before = _stones(pid)
    got = client.post("/elements/upgrade", json={"element": "fire"},
                      headers={**_session(pid), **_idem("um")})
    price = got.json()["upgrade"]["cost"]
    assert price == emr.upgrade_cost(3)
    assert _stones(pid) == before - price
    state = _stored(pid)
    assert state["elements"]["fire"] == 4
    assert state["stone_spent"] == price


def test_the_price_rises_with_the_level_the_server_holds(client):
    """The caller cannot bid a level, so the ladder is the only thing that can
    move the price. Strictly increasing, anchored once at the base."""
    ladder = [emr.upgrade_cost(level) for level in range(0, emr.MAX_ELEMENT_LEVEL)]
    assert all(b > a for a, b in zip(ladder, ladder[1:]))
    assert ladder[0] == emr.UPGRADE_COST_BASE
    assert all(cost == emr.UPGRADE_COST_BASE * (level + 1)
               for level, cost in enumerate(ladder))

    pid = _rich(client, "ladderlive", elements={"fire": 1})
    seen = []
    for _ in range(3):
        got = client.post("/elements/upgrade", json={"element": "fire"},
                          headers={**_session(pid), **_idem("ul")})
        assert got.status_code == 200, got.text
        seen.append(got.json()["upgrade"]["cost"])
    assert seen == sorted(seen) and len(set(seen)) == len(seen)


def test_a_distinct_upgrade_key_is_a_genuine_second_purchase(client):
    """The control for the replay arm above: a fresh key really does charge
    again, so the free level there is the replay and not the route."""
    pid = _rich(client, "upgradefresh", elements={"fire": 1})
    before = _stones(pid)
    first = client.post("/elements/upgrade", json={"element": "fire"},
                        headers={**_session(pid), **_idem("uf1")})
    second = client.post("/elements/upgrade", json={"element": "fire"},
                         headers={**_session(pid), **_idem("uf2")})
    assert first.status_code == second.status_code == 200
    assert second.json()["upgrade"]["to_level"] == 3
    paid = (first.json()["upgrade"]["cost"] + second.json()["upgrade"]["cost"])
    assert _stones(pid) == before - paid
    assert _stored(pid)["elements"]["fire"] == 3


def test_an_upgrade_requires_an_idempotency_key(client):
    pid = _rich(client, "upgradeidem", elements={"fire": 1})
    got = client.post("/elements/upgrade", json={"element": "fire"},
                      headers=_session(pid))
    assert got.status_code == 400
    assert "element-upgrade" in got.json()["detail"]


def test_upgrading_an_unforged_element_is_refused_and_moves_no_stones(client):
    pid = _rich(client, "upgradeunforged", elements={"fire": 1})
    before = _stones(pid)
    got = client.post("/elements/upgrade", json={"element": "wind"},
                      headers={**_session(pid), **_idem("uu")})
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["not_forged"] % "wind"
    assert _stones(pid) == before


def test_upgrading_past_the_cap_is_refused_and_moves_no_stones(client):
    pid = _rich(client, "upgradecap",
                elements={"fire": emr.MAX_ELEMENT_LEVEL})
    before = _stones(pid)
    got = client.post("/elements/upgrade", json={"element": "fire"},
                      headers={**_session(pid), **_idem("uc")})
    assert got.status_code == 409
    assert got.json()["detail"] == \
        REFUSAL_TEMPLATES["at_max_level"] % ("fire", emr.MAX_ELEMENT_LEVEL)
    assert _stones(pid) == before
    assert _stored(pid)["elements"]["fire"] == emr.MAX_ELEMENT_LEVEL


def test_an_unaffordable_upgrade_is_refused_and_moves_no_stones(client):
    """A forged element the player cannot afford to raise. Seeded directly so the
    balance starts at zero without a forge charge."""
    pid = _new_player(client, "poorup")
    _seed_matrix(pid, elements={"metal": 1})
    got = client.post("/elements/upgrade", json={"element": "metal"},
                      headers={**_session(pid), **_idem("pu")})
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["insufficient"]
    assert _stones(pid) == 0
    assert _stored(pid)["elements"]["metal"] == 1


# ======================================================================================
# 6. RESONANCE -- two names in, a computed bonus out
# ======================================================================================
def test_resonance_key_set_on_an_advantage_pairing(client):
    pid = _rich(client, "reskeys", elements={"fire": 1, "metal": 1})
    got = client.post("/elements/resonance", json={"first": "fire",
                                                    "second": "metal"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == RESONANCE_KEYS
    assert set(body["resonance"]) == RESONANCE_DECISION_KEYS
    assert body["resonance"]["pair"] == ["fire", "metal"]
    assert body["resonance"]["relation"] == "advantage"


def test_a_resonance_bonus_is_one_third_of_the_advantage_spread(client):
    """A relation, not a literal: the bonus must stay derived from the shipped
    multiplier table, and must never reach the advantage multiplier itself -- a
    cosmetic pairing must not become a second damage multiplier."""
    pid = _rich(client, "resmath", elements={"fire": 1, "metal": 1})
    got = client.post("/elements/resonance", json={"first": "fire",
                                                    "second": "metal"},
                      headers=_session(pid))
    resonance = got.json()["resonance"]
    spread = (emr.ELEMENT_MULTIPLIER["advantage"]
              - emr.ELEMENT_MULTIPLIER["neutral"])
    assert resonance["bonus"] == pytest.approx(round(spread / 3.0, 4), abs=1e-6)
    assert 0 < resonance["bonus"] < resonance["multiplier"] - 1.0
    assert resonance["multiplier"] == emr.ELEMENT_MULTIPLIER["advantage"]


def test_a_disadvantage_pairing_is_stored_and_multiplier_is_the_penalty(client):
    """Reversed order turns the advantage into a disadvantage, and the pairing is
    still accepted -- the relation is computed, not assumed."""
    pid = _rich(client, "resdis", elements={"fire": 1, "metal": 1})
    got = client.post("/elements/resonance", json={"first": "metal",
                                                    "second": "fire"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    resonance = got.json()["resonance"]
    assert resonance["relation"] == "disadvantage"
    assert resonance["multiplier"] == emr.ELEMENT_MULTIPLIER["disadvantage"]
    assert resonance["bonus"] == 0.0


def test_a_neutral_pairing_is_stored_and_grants_nothing(client):
    """The measured neutral pair on the shipped table. Stored so the player can
    see it, worth zero so it is cosmetic only."""
    neutral_a, neutral_b = "metal", "void"
    assert emr.element_relation(neutral_a, neutral_b) == "neutral"
    pid = _rich(client, "resneutral", elements={neutral_a: 1, neutral_b: 1})
    got = client.post("/elements/resonance", json={"first": neutral_a,
                                                    "second": neutral_b},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    resonance = got.json()["resonance"]
    assert resonance["relation"] == "neutral"
    assert resonance["multiplier"] == emr.ELEMENT_MULTIPLIER["neutral"]
    assert resonance["bonus"] == 0.0
    assert got.json()["total_bonus"] == 0.0


def test_every_relation_is_reachable_and_only_three_exist(client):
    """The table is symmetric enough that all three relations occur; a client
    switches on exactly these three strings."""
    relations = {
        emr.element_relation(a, b)
        for a in emr.ELEMENTS for b in emr.ELEMENTS if a != b}
    assert relations == {"advantage", "disadvantage", "neutral"}


def test_resonance_needs_no_idempotency_key_because_it_moves_no_currency(client):
    """No key is required, and no key is needed to make it work."""
    pid = _rich(client, "resnokey", elements={"fire": 1, "metal": 1})
    before = _stones(pid)
    got = client.post("/elements/resonance", json={"first": "fire",
                                                    "second": "metal"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    assert _stones(pid) == before
    assert _stored(pid)["pairings"] == [["fire", "metal"]]


def test_an_element_cannot_resonate_with_itself(client):
    """Whitespace and case are normalised first, so these are the SAME element."""
    pid = _rich(client, "resself", elements={"fire": 1})
    for pair in ({"first": "fire", "second": "fire"},
                 {"first": " fire", "second": "FIRE"}):
        got = client.post("/elements/resonance", json=pair, headers=_session(pid))
        assert got.status_code == 409, got.text
        assert got.json()["detail"] == REFUSAL_TEMPLATES["self_resonance"]
    assert _stored(pid)["pairings"] == []


def test_pairing_an_unforged_element_is_refused_and_stores_nothing(client):
    pid = _rich(client, "resunforged", elements={"fire": 1})
    got = client.post("/elements/resonance", json={"first": "fire",
                                                    "second": "earth"},
                      headers=_session(pid))
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["not_forged"] % "earth"
    assert _stored(pid)["pairings"] == []


def test_a_pairing_cannot_be_duplicated_in_either_order(client):
    pid = _rich(client, "resdup", elements={"fire": 1, "water": 1})
    first = client.post("/elements/resonance", json={"first": "fire",
                                                     "second": "water"},
                        headers=_session(pid))
    assert first.status_code == 200, first.text
    reversed_pair = client.post("/elements/resonance",
                               json={"first": "water", "second": "fire"},
                               headers=_session(pid))
    assert reversed_pair.status_code == 409
    assert reversed_pair.json()["detail"] == \
        REFUSAL_TEMPLATES["already_paired"] % ("water", "fire")
    assert len(_stored(pid)["pairings"]) == 1


# ======================================================================================
# 7. INHERITANCE -- the companion is a reference, not a target
# ======================================================================================
def test_inheritance_key_set_and_the_server_scaled_bonus(client):
    """The bonus is a server constant times the element's OWN level. The caller
    supplies neither number."""
    pid = _rich(client, "inhkeys", elements={"fire": 3})
    _add_companion(pid, {"w1": {"id": "w1"}})
    got = client.post("/elements/inheritance",
                      json={"element": "fire", "companion_id": "w1"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == INHERITANCE_KEYS
    assert set(body["inheritance"]) == INHERITANCE_DECISION_KEYS
    assert body["inheritance"]["bonus"] == pytest.approx(0.05 * 3, abs=1e-6)


def test_the_teaching_bonus_rises_with_the_element_level(client):
    """A relationship between the level the server holds and the bonus it grants."""
    seen = []
    for level in (1, 4, emr.MAX_ELEMENT_LEVEL):
        pid = _rich(client, f"inhlvl{level}", elements={"earth": level})
        _add_companion(pid, {"c1": {"id": "c1"}})
        got = client.post("/elements/inheritance",
                          json={"element": "earth", "companion_id": "c1"},
                          headers=_session(pid))
        assert got.status_code == 200, got.text
        seen.append(got.json()["inheritance"]["bonus"])
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    assert seen[-1] <= 0.05 * emr.MAX_ELEMENT_LEVEL


def test_inheritance_needs_no_idempotency_key_because_it_moves_no_currency(client):
    pid = _rich(client, "inhnokey", elements={"fire": 2})
    _add_companion(pid, {"c1": {"id": "c1"}})
    before = _stones(pid)
    got = client.post("/elements/inheritance",
                      json={"element": "fire", "companion_id": "c1"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    assert _stones(pid) == before


def test_a_companion_the_caller_does_not_own_is_refused(client):
    pid = _rich(client, "inhnotmine", elements={"fire": 2})
    _add_companion(pid, {"mine": {"id": "mine"}})
    got = client.post("/elements/inheritance",
                      json={"element": "fire", "companion_id": "not-mine"},
                      headers=_session(pid))
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["not_your_companion"]
    assert _stored(pid)["taught"] == []


def test_another_players_companion_id_is_refused(client):
    """The roster is per-player. Naming a companion that exists on SOME OTHER row
    buys nothing -- there is no field here that could redirect the effect."""
    owner = _rich(client, "inhowner", elements={"fire": 2})
    thief = _rich(client, "inhthief", elements={"fire": 2})
    _add_companion(owner, {"w1": {"id": "w1"}})
    got = client.post("/elements/inheritance",
                      json={"element": "fire", "companion_id": "w1"},
                      headers=_session(thief))
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["not_your_companion"]
    assert _stored(thief)["taught"] == []
    assert _stored(owner)["taught"] == []


def test_a_roster_stored_as_a_list_is_read_too(client):
    """The roster has two persisted shapes. A client panel reading only the dict
    shape would miss this player entirely."""
    pid = _rich(client, "inhlist", elements={"earth": 2})
    _add_companion(pid, [{"id": "l1", "level": 4}])
    got = client.post("/elements/inheritance",
                      json={"element": "earth", "companion_id": "l1"},
                      headers=_session(pid))
    assert got.status_code == 200, got.text
    assert got.json()["inheritance"]["companion_id"] == "l1"


def test_teaching_the_same_element_to_the_same_companion_twice_is_refused(client):
    pid = _rich(client, "inhdup", elements={"fire": 2})
    _add_companion(pid, {"w1": {"id": "w1"}, "w2": {"id": "w2"}})
    first = client.post("/elements/inheritance",
                        json={"element": "fire", "companion_id": "w1"},
                        headers=_session(pid))
    assert first.status_code == 200, first.text
    again = client.post("/elements/inheritance",
                        json={"element": "fire", "companion_id": "w1"},
                        headers=_session(pid))
    assert again.status_code == 409
    assert again.json()["detail"] == REFUSAL_TEMPLATES["already_taught"] % "fire"
    assert len(_stored(pid)["taught"]) == 1
    # A DIFFERENT companion is still allowed, so the guard is per pairing.
    other = client.post("/elements/inheritance",
                        json={"element": "fire", "companion_id": "w2"},
                        headers=_session(pid))
    assert other.status_code == 200, other.text


def test_teaching_an_unforged_element_is_refused(client):
    pid = _rich(client, "inhunforged", elements={"fire": 2})
    _add_companion(pid, {"w1": {"id": "w1"}})
    got = client.post("/elements/inheritance",
                      json={"element": "wind", "companion_id": "w1"},
                      headers=_session(pid))
    assert got.status_code == 409
    assert got.json()["detail"] == REFUSAL_TEMPLATES["not_forged"] % "wind"
    assert _stored(pid)["taught"] == []


# ======================================================================================
# 8. THE REQUEST SHAPE IS THE ANTI-ABUSE CONTROL
# ======================================================================================
def test_no_route_accepts_a_caller_supplied_number(client):
    """Every model is ``extra="forbid"``, so a caller posting a cost, bonus, level
    or multiplier gets 422 rather than a quietly ignored bonus. This is the loud
    version of the module's central rule: the server computes every bonus it
    returns."""
    pid = _rich(client, "extra", elements={"fire": 2})
    _add_companion(pid, {"c1": {"id": "c1"}})
    bodies = {
        "/elements/forge": {"element": "water", "cost": 1, "amount": 5,
                            "multiplier": 9.0, "power": 999, "player_id": pid,
                            "now": 0},
        "/elements/resonance": {"first": "fire", "second": "metal",
                                 "bonus": 9.9, "multiplier": 4.0},
        "/elements/upgrade": {"element": "fire", "level": 9, "cost": 1},
        "/elements/inheritance": {"element": "fire", "companion_id": "c1",
                                  "level": 99, "bonus": 5.0},
    }
    for path, body in bodies.items():
        headers = _session(pid)
        if path in ("/elements/forge", "/elements/upgrade"):
            headers.update(_idem("extra"))
        got = client.post(path, json=body, headers=headers)
        assert got.status_code == 422, (path, got.status_code, got.text)
        rejected = {entry["loc"][-1] for entry in got.json()["detail"]
                    if entry["type"] == "extra_forbidden"}
        assert rejected == (set(body) & set(CALLER_SUPPLIED_FIELDS)), (path, rejected)
    assert _stored(pid)["elements"] == {"fire": 2}


def test_a_missing_or_malformed_body_field_is_a_422_locating_the_field(client):
    """A 422 must say WHICH field, or a client developer is guessing."""
    pid = _rich(client, "malformed")
    cases = [
        ("/elements/forge", {}, "element", "missing"),
        ("/elements/forge", {"element": ""}, "element", "string_too_short"),
        ("/elements/forge", {"element": None}, "element", "string_type"),
        ("/elements/forge", {"element": "f" * 40}, "element", "string_too_long"),
        ("/elements/resonance", {"first": "fire"}, "second", "missing"),
        ("/elements/inheritance", {"element": "fire"}, "companion_id", "missing"),
    ]
    for path, body, field, kind in cases:
        headers = _session(pid)
        if path == "/elements/forge":
            headers.update(_idem("mal"))
        got = client.post(path, json=body, headers=headers)
        assert got.status_code == 422, (path, body, got.status_code, got.text)
        entries = got.json()["detail"]
        assert any(entry["loc"] == ["body", field] and entry["type"] == kind
                   for entry in entries), (path, body, entries)


def test_the_max_element_length_is_the_declared_bound(client):
    """32 characters. The validator lowercases and looks the value up, so an
    unbounded string is only a way to send a big body."""
    pid = _rich(client, "bound")
    at_bound = client.post("/elements/forge", json={"element": "f" * 32},
                           headers={**_session(pid), **_idem("b1")})
    assert at_bound.status_code in (400, 409), at_bound.text
    over = client.post("/elements/forge", json={"element": "f" * 33},
                       headers={**_session(pid), **_idem("b2")})
    assert over.status_code == 422


# ======================================================================================
# 9. A CORRUPT OR STALE SAVE COERCES INSTEAD OF CRASHING
# ======================================================================================
def test_a_missing_or_broken_container_becomes_a_playable_empty_matrix(client):
    """An old save, a hand-edited record, and a save from before this feature
    existed must all come out playable. The module's own docstring promises
    exactly this, and the shapes below are the ones it names."""
    pid = _new_player(client, "corrupt")
    for corrupt in (None, "not-a-dict", [], {"elements": "nope"},
                    {"pairings": "nope"}, {"taught": "nope"},
                    {"stone_spent": None}, {"forged_at": None},
                    {"elements": {"fire": "x"}}, {"elements": {"fire": None}}):
        with players_db.transaction(pid):
            row = players_db[pid]
            row[emr.MATRIX_KEY] = corrupt
            players_db.persist(pid, row)
        got = client.get("/elements/state", headers=_session(pid))
        assert got.status_code == 200, (corrupt, got.status_code, got.text)
        body = got.json()
        assert body["forged"] == [] and body["pairings"] == []
        assert body["stone_spent"] == 0


def test_a_numeric_string_stone_spend_is_still_accepted(client):
    """The counter to the 500 below: a string that ``int()`` can read coerces, so
    the defect is specifically a value no number can be read from."""
    pid = _new_player(client, "numericstr")
    _seed_matrix(pid, elements={"fire": 1}, stone_spent="12", forged_at="7")
    got = client.get("/elements/state", headers=_session(pid))
    assert got.status_code == 200, got.text
    assert got.json()["stone_spent"] == 12


def test_a_corrupt_stone_spend_is_coerced_to_zero_not_a_500(client):
    """An unreadable ``stone_spent`` / ``forged_at`` degrades to 0, and the
    player's matrix stays reachable.

    This was a KNOWN DEFECT pinned red. ``normalize_matrix`` coerced its three
    CONTAINER fields defensively but fed those two numbers straight into
    ``int()``, so a hand-edited row, an old save or a half-written field raised
    ``ValueError`` inside the route and all five routes answered a bare 500 --
    one corrupt number took the whole matrix offline until something else
    repaired the row. MEASURED before the fix: 500 on the read route AND on a
    mutating route.

    ``_coerce_count`` now applies the degrade-don't-crash rule to both. A
    numeric string is still read, because ``int("12")`` succeeds -- the boundary
    is an unreadable value, not string-vs-number, which is what the sibling arm
    above pins.
    """
    # Funded, so the mutating arm below fails on the COERCION and not on an
    # empty wallet -- otherwise a 409 "insufficient balance" would mask the thing
    # this arm is actually measuring.
    pid = _rich(client, "corruptspend")
    for field in ("stone_spent", "forged_at"):
        for value in ("nope", None, {"a": 1}, [1, 2], object()):
            _seed_matrix(pid, **{field: value})
            state = client.get("/elements/state", headers=_session(pid))
            assert state.status_code == 200, (
                f"{field}={value!r} still 500s; the coercion was removed. "
                "Restore _coerce_count on both fields.")
            # ``forged_at`` is deliberately not on the wire, so it is read back
            # from the store; ``stone_spent`` is published and asserted there.
            if field == "stone_spent":
                assert state.json()[field] == 0, (value, state.json()[field])
            else:
                assert _coerced(pid, field) == 0, (value, _coerced(pid, field))
            # A MUTATING route reads the same block, so it must be reachable too.
            _seed_matrix(pid, **{field: value})
            forged = client.post("/elements/forge", json={"element": "fire"},
                                 headers={**_session(pid), **_idem(f"c{field}")})
            assert forged.status_code == 200, (field, value, forged.text)
            # NOT zero. A successful forge legitimately WRITES both fields: it
            # adds the quoted price to ``stone_spent`` and stamps ``forged_at``
            # with the current second. MEASURED: both fields held the corrupt
            # value on the way in, and after the forge ``stone_spent`` read 250
            # (the forged cost of fire) and ``forged_at`` read a real epoch.
            #
            # This arm previously asserted ``== 0`` after the forge, which is
            # wrong twice over. It is not what the route writes, AND it could not
            # tell a corrupt value that was coerced-and-then-correctly-updated
            # from a route that never touched the field at all -- so it would
            # have passed a route that did nothing.
            if field == "stone_spent":
                # The corrupt base coerced to 0, then the forge added the cost
                # the SERVER quoted on this very response.
                assert _coerced(pid, field) == forged.json()["cost"], (
                    field, value, _coerced(pid, field), forged.json()["cost"])
            else:
                # ``forged_at`` is stamped fresh by the forge, so the property
                # is that it is no longer the corrupt input: a real epoch, not 0.
                assert _coerced(pid, field) > 0, (field, value, _coerced(pid, field))

    # A negative value floors at zero rather than becoming a credit.
    _seed_matrix(pid, stone_spent=-500)
    got = client.get("/elements/state", headers=_session(pid))
    assert got.status_code == 200
    assert got.json()["stone_spent"] == 0

    # And a real number still reads as itself, on the same block the corrupt
    # values above used -- so the coercion did not flatten good data too.
    #
    # ``forged_at=99`` is seeded as well and must NOT be reset by a read, so it
    # is checked through the store (``_coerced``), not off the wire: the route
    # deliberately does not publish ``forged_at``.
    _seed_matrix(pid, stone_spent=4321, forged_at=99)
    good = client.get("/elements/state", headers=_session(pid))
    assert good.status_code == 200
    assert good.json()["stone_spent"] == 4321
    assert _coerced(pid, "forged_at") == 99, _coerced(pid, "forged_at")
    # The forged ELEMENT survives the coercion of the two counters beside it.
    # This arm seeds an explicit element because ``_seed_matrix`` builds from a
    # BLANK matrix, so a good counter alone leaves nothing forged -- and an
    # empty ``forged`` list here would pass on a route that had dropped every
    # element instead of having coerced nothing.
    _seed_matrix(pid, elements={"fire": 2}, stone_spent=4321, forged_at=99)
    kept = client.get("/elements/state", headers=_session(pid))
    assert kept.status_code == 200
    assert [e["element"] for e in kept.json()["forged"]] == ["fire"], (
        "coercing the counters must not drop a forged element beside them")
    assert kept.json()["stone_spent"] == 4321


def test_an_element_the_current_table_cannot_resolve_is_dropped_not_resurrected(client):
    """``light`` is the measured example: it was a real element in an earlier build
    and is not one now. Resurrecting it would hand a player an element nothing can
    match against."""
    pid = _new_player(client, "stale")
    _seed_matrix(pid, elements={"light": 5, "fire": 2, "water": "x"})
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert [e["element"] for e in body["forged"]] == ["fire"]


def test_a_level_above_the_cap_is_clamped_not_trusted(client):
    pid = _new_player(client, "clamp")
    _seed_matrix(pid, elements={"fire": 999})
    entry = client.get("/elements/state", headers=_session(pid)).json()["forged"][0]
    assert entry["level"] == emr.MAX_ELEMENT_LEVEL


def test_a_pairing_survives_only_when_both_of_its_elements_are_still_forged(client):
    """A hand-edited record pairing an element the player no longer holds is
    dropped rather than shown as a bonus nobody can use."""
    pid = _new_player(client, "stalepair")
    _seed_matrix(pid, elements={"fire": 1, "metal": 1}, pairings=[
        ["fire", "metal"],           # kept: both forged
        ["metal", "fire"],           # kept: also both forged (no dedupe here)
        ["fire", "fire"],            # dropped: an element with itself
        ["fire", "light"],           # dropped: `light` is not an element
        ["fire"],                    # dropped: not a pair
        ["fire", "earth"],           # dropped: earth is not forged
    ])
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert [p["elements"] for p in body["pairings"]] == \
        [["fire", "metal"], ["metal", "fire"]]


def test_a_taught_entry_survives_only_while_its_element_is_still_forged(client):
    pid = _new_player(client, "staletaught")
    _seed_matrix(pid, elements={"fire": 1}, taught=[
        {"element": "fire", "companion_id": "c1", "bonus": 0.1, "taught_at": 1},
        {"element": "wind", "companion_id": "c1", "bonus": 0.1, "taught_at": 1},
    ])
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert [t["element"] for t in body["taught"]] == ["fire"]


def test_a_negative_stone_spend_is_floored_at_zero(client):
    pid = _new_player(client, "negative")
    _seed_matrix(pid, elements={"fire": 1}, stone_spent=-500, forged_at=-1)
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert body["stone_spent"] == 0


def test_the_element_table_is_a_closed_canon_ring_with_one_named_exception(client):
    """Two measured properties of the shipped advantage table, which this module
    IMPORTS rather than restates.

    1. Every element beats something, so no player choice is worthless.
    2. Exactly one element is beaten by nothing, and it is ``void`` -- whose row
       the table's own comment names as deliberate ("void has no weakness but
       beats fire"). Pinned as an EXCEPTION, not as a general property: the
       general claim "0 uncounterable" is FALSE for void, and a test asserting it
       would be asserting something the shipped table does not do.

    Also pinned: wind and lightning are each other's counter -- the one mutual
    pair, and the one place the relation table is not antisymmetric.
    """
    assert all(emr.counters_for_for(e) for e in emr.ELEMENTS), \
        "an element that beats nothing"
    uncountered = [e for e in emr.ELEMENTS if not emr.counters_for(e)]
    assert uncountered == ["void"], uncountered

    mutual = sorted((a, b) for a in emr.ELEMENTS for b in emr.ELEMENTS
                    if a < b and emr.ELEMENT_ADVANTAGE.get(a) == b
                    and emr.ELEMENT_ADVANTAGE.get(b) == a)
    assert mutual == [("lightning", "wind")], mutual


def test_a_one_way_pairing_reverses_to_the_penalty(client):
    """The control for the mutual-pair defect below: on every one-way pair,
    reversing the two names turns the advantage into the penalty."""
    one_way = [("fire", "metal"), ("metal", "wood"), ("wood", "earth"),
               ("earth", "water"), ("water", "fire"), ("void", "fire")]
    for winner, loser in one_way:
        assert emr.ELEMENT_ADVANTAGE[winner] == loser
        assert emr.element_relation(winner, loser) == "advantage"
        assert emr.element_relation(loser, winner) == "disadvantage", (winner, loser)


def test_known_defect_the_one_mutual_pair_is_advantage_in_BOTH_orders(client):
    """KNOWN DEFECT, pinned on purpose.

    ``element_relation`` checks the attacker's row before the defender's. For the
    single mutual pair in the shipped table -- wind and lightning, each naming the
    other -- both rows match, so the first check always wins and BOTH orders answer
    ``advantage`` with multiplier 1.3.

    The module docstring for ``element_relation`` states the opposite: "a mutual pair
    produces one advantage cell and one disadvantage cell, and checking the wrong
    row first returns 1.3 twice", and names wind and lightning as the measured
    example. The shipped code returns 1.3 twice.

    It is NOT reachable through the resonance route, and the test below proves
    why rather than assuming it: ``rule_resonance`` rejects a pair whose REVERSE
    is already stored, so the second spelling never lands. The defect is in a
    PUBLIC helper, so it matters to whatever else calls it -- a combat formula, a
    panel that wants to render a matchup -- not to this route's payout today.
    """
    wind, lightning = "wind", "lightning"
    assert emr.element_relation(wind, lightning) == "advantage"
    assert emr.element_relation(lightning, wind) == "advantage", (
        "the mutual pair is no longer advantage in both orders; the row order in "
        "element_relation was changed. Update this assertion to whatever the new "
        "answer is, and re-read the two route arms below.")
    assert emr.element_multiplier(wind, lightning) == \
        emr.element_multiplier(lightning, wind) == \
        emr.ELEMENT_MULTIPLIER["advantage"]

    pid = _rich(client, "mutual", elements={wind: 1, lightning: 1})
    first = client.post("/elements/resonance",
                        json={"first": wind, "second": lightning},
                        headers=_session(pid))
    assert first.status_code == 200, first.text
    assert first.json()["resonance"]["relation"] == "advantage"

    # The guard is symmetric, so the reverse spelling is refused and the bonus
    # cannot be collected twice. Measured: 409, and the store holds one pairing.
    reverse = client.post("/elements/resonance",
                          json={"first": lightning, "second": wind},
                          headers=_session(pid))
    assert reverse.status_code == 409, reverse.text
    assert reverse.json()["detail"] == \
        REFUSAL_TEMPLATES["already_paired"] % (lightning, wind)
    body = client.get("/elements/state", headers=_session(pid)).json()
    assert [p["elements"] for p in body["pairings"]] == [[wind, lightning]]
    assert body["total_bonus"] == pytest.approx(
        emr.resonance_bonus([wind, lightning]), abs=1e-6)


def test_no_pairing_can_be_double_collected_in_either_order(client):
    """The property that makes the defect above harmless here: whichever way a
    pair is first spelled, its reverse is refused, so one pair is worth its bonus
    once. Asserted on a one-way pair and on the mutual pair."""
    for label, (first_el, second_el) in {
        "one-way": ("fire", "metal"),
        "mutual": ("wind", "lightning"),
    }.items():
        pid = _rich(client, f"once{label}", elements={first_el: 1, second_el: 1})
        assert client.post("/elements/resonance",
                           json={"first": first_el, "second": second_el},
                           headers=_session(pid)).status_code == 200
        repeat = client.post("/elements/resonance",
                             json={"first": first_el, "second": second_el},
                             headers=_session(pid))
        reverse = client.post("/elements/resonance",
                              json={"first": second_el, "second": first_el},
                              headers=_session(pid))
        assert repeat.status_code == 409, (label, repeat.text)
        assert reverse.status_code == 409, (label, reverse.text)
        body = client.get("/elements/state", headers=_session(pid)).json()
        assert len(body["pairings"]) == 1, (label, body["pairings"])
        assert body["total_bonus"] == pytest.approx(
            emr.resonance_bonus([first_el, second_el]), abs=1e-6)


# ======================================================================================
# 9b. THE TWO NAMES ON ONE LIST, AND THE TWO OWNERS OF THE TABLE
# ======================================================================================
def test_beats_and_beaten_by_publish_the_same_list_and_the_truthful_one(client):
    """``beaten_by`` used to hold the OPPOSITE of what its name says.

    ``counters_for_for(name)`` returns the elements ``name`` BEATS, and that was
    published under ``beaten_by``. MEASURED before this change: for ``metal`` it
    published ``["wood"]``, but metal is beaten BY ``fire`` -- it published
    metal's own victim under a name reading "who beat me".

    ``beats`` now carries the list under a name that matches it. ``beaten_by`` is
    kept so ``client/scenes/game/ElementMatrixPanel.gd`` keeps rendering, and
    this arm pins that the two can never drift apart.
    """
    pid = _rich(client, "beatsname", stones=0,
                elements={"fire": 1, "metal": 1, "void": 1, "wind": 1,
                          "lightning": 1})
    body = client.get("/elements/state", headers=_session(pid)).json()
    entries = {e["element"]: e for e in body["forged"]}

    for element, entry in entries.items():
        assert entry["beats"] == entry["beaten_by"], (
            f"{element}: beats={entry['beats']} but beaten_by="
            f"{entry['beaten_by']} -- the two keys must never disagree")
        # And the value is the truth: the element this one beats.
        assert entry["beats"] == (
            [emr.ELEMENT_ADVANTAGE[element]] if element in emr.ELEMENT_ADVANTAGE
            else [])
        # ...which is NOT the same list as "who beats me". That inversion is the
        # defect, so it is asserted rather than assumed away.
        beaten_by_truth = emr.counters_for(element)
        if not (emr.ELEMENT_ADVANTAGE.get(element) in beaten_by_truth
                and len(emr.ELEMENT_ADVANTAGE) > 0):
            assert entry["beats"] != beaten_by_truth or element == "void", element

    # The two halves of the table together: for a one-way pair they are disjoint.
    metal = entries["metal"]
    assert metal["beats"] == ["wood"]
    assert metal["counters"] == ["fire"]
    assert metal["beats"] != metal["counters"]

    # The uncounterable element is genuinely uncounterable, and the two mutual
    # counters are each other's -- so those two pairs DO match in both directions.
    assert entries["void"]["counters"] == []
    assert entries["wind"]["counters"] == ["lightning"]
    assert entries["lightning"]["counters"] == ["wind"]


def test_the_matrix_helper_agrees_with_the_shipped_combat_multiplier(client):
    """``element_multiplier`` must not become a second combat system.

    It is a re-implementation of ``combat_config.calculate_element_multiplier``
    over the same table. The two agreeing everywhere is the property that keeps
    the matrix panel from drawing a matchup the damage formula does not resolve
    that way. MEASURED across all 56 ordered pairs: no disagreement.
    """
    from combat_config import calculate_element_multiplier
    for attacker in emr.ELEMENTS:
        for defender in emr.ELEMENTS:
            if attacker == defender:
                continue
            assert emr.element_multiplier(attacker, defender) == \
                calculate_element_multiplier(attacker, defender), (attacker, defender)

    # The one pair where a reader would expect disagreement, pinned explicitly
    # so a future table change has to be a deliberate edit to this arm.
    assert emr.element_multiplier("wind", "lightning") == 1.3
    assert emr.element_multiplier("lightning", "wind") == 1.3


def test_the_mutual_pair_is_not_splittable_by_reordering_the_row_check():
    """A docstring used to claim the mutual pair was fixable by checking the
    defender's row first. It is not, and the claim would have invited an edit
    that made the matrix disagree with the combat engine.

    MEASURED: swapping the order flips BOTH directions to ``disadvantage``, so
    the pair stays symmetric either way. The table is the only place this can be
    settled, and it is another owner's file.
    """
    # ``emr`` imports the names it needs, not the module, so read the table
    # off the module it actually re-exports.
    advantage = emr.ELEMENT_ADVANTAGE

    def defender_first(attacker: str, defender: str) -> str:
        if advantage.get(defender) == attacker:
            return "disadvantage"
        if advantage.get(attacker) == defender:
            return "advantage"
        return "neutral"

    assert advantage.get("wind") == "lightning"
    assert advantage.get("lightning") == "wind"
    # Both orders match under either rule, so neither rule splits the pair.
    for pair in (("wind", "lightning"), ("lightning", "wind")):
        assert emr.element_relation(*pair) == "advantage", pair
        assert defender_first(*pair) == "disadvantage", pair


# ======================================================================================
# 10. NEGATIVE CONTROL -- the wallet assertion is not vacuous
# ======================================================================================
def test_the_wallet_assertions_can_actually_fail(client, monkeypatch):
    """Replaces the wallet debit with a function that reports success and moves no
    money. The refusal arms below still pass on their status codes, so this is the
    only thing standing between a caller-supplied price and a green suite: it
    proves the balance assertions in section 4 and 5 detect a route that reports
    a charge it never made.

    The forged element still appears in the matrix (the state write is the
    route's own), which is exactly the shape of a lie the response would carry
    back to a client.
    """
    monkeypatch.setattr(wallet_dispatch, "spend",
                        lambda *a, **k: {"ok": True, "balance": {}, "wallet_version": 0})

    pid = _rich(client, "negativecontrol", stones=0, elements={"fire": 1})
    before = _stones(pid)
    forged = client.post("/elements/forge", json={"element": "water"},
                         headers={**_session(pid), **_idem("nc1")})
    assert forged.status_code == 200, forged.text
    assert forged.json()["cost"] > 0, "the reply still claims a price"
    assert _stones(pid) == before, (
        "the control did not bite: with the debit stubbed the balance must not "
        "move, so this assertion is the evidence that the balance assertions "
        "elsewhere are real")
    upgraded = client.post("/elements/upgrade", json={"element": "fire"},
                           headers={**_session(pid), **_idem("nc2")})
    assert upgraded.status_code == 200
    assert _stones(pid) == before