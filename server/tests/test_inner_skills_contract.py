"""INNER SKILLS: THE HTTP CONTRACT, PINNED.

WHAT THIS FILE IS FOR
---------------------
``server/inner_skills.py`` exposes four routes -- one read and three actions --
and a client panel draws them. This file answers a different question from the
two existing suites (``server/test_inner_skills.py`` drives the state machine
through the live app; ``check_inner_skills_persistence.py`` checks the row
survives a PostgreSQL restart): what does a client that has never read the
Python actually receive, and what must it send?

Every assertion below was MEASURED by calling the routes, not read off the
return dictionaries. Where a measured answer disagrees with what the module's
comments or docstrings imply, the MEASURED answer is what is written here and
the disagreement is called out at the test. Three of those have since been
FIXED in the module, and the tests that used to pin the broken behaviour now
pin the fixed behaviour instead -- sections 12 and 16. A client written
against the comments below would be wrong:

  1. **A request naming a player that does not exist answers 404 on all four
     routes.** This used to be a bare ``500`` with a non-JSON body on the
     three ACTION routes only, while ``GET /inner-skills/{player_id}``
     answered a clean 404 for the same id: the module contradicted itself.
     The ``_require_player`` 404 branch always existed and always will; the
     action helpers opened ``players_db.transaction(player_id)`` before calling
     it, and that transaction preloads the row and raises ``KeyError`` one
     line earlier with nothing to convert it. Fixed, and pinned in
     section 12 -- including that all four routes now agree.
  2. **``claim`` needs no ``Idempotency-Key``**, only ``comprehend`` and
     ``tupo`` do. A client that sends the header to all three is fine; a
     client that treats its absence as an error on ``claim`` is wrong.
  3. **A refusal for "not enough materials" puts an OBJECT in ``detail``**,
     not a string: ``{"detail": {"ok": false, "error": "...", ...}}``. Every
     other refusal on these routes uses a plain string. A client that renders
     ``detail`` as text will print Python dict syntax on that one path.
  4. **Unknown body fields are ignored, not refused.** ``{"amount": 999}``
     alongside a valid ``player_id``/``skill_id`` answers 200 and charges the
     SERVER's price, because the price is read from the module's own skill
     table and never from the request. Safe, but a client must not expect the
     validator to catch a stray field here.
  5. **A realm stored in the spaced form still works, and the ladder is
     DERIVED from the live cultivation config.** ``_check_realm`` normalises
     ``"Foundation Establishment"`` onto ``REALM_ORDER``, so both that and
     ``"foundation_establishment"`` open every skill. ``REALM_ORDER`` is no
     longer the bare three-name list it was: it is the three declared gate
     tiers (``GATE_TIERS``) followed by every realm
     ``cultivation_config.REALMS`` publishes, so a player stored as
     ``"foundation"`` -- the second realm on the live ladder, and never a
     declared tier -- now ranks above ``"qi_refining"`` and opens every skill
     instead of being floored to ``mortal`` and refused both paid ones. The
     ladder is now the canonical EIGHT rungs (``REALM_ORDER``, matching
     ``ZoneManager.REALM_ORDER`` and ``NPCManager.REALM_ORDER``) plus an alias
     table for the short names the player store writes, so ``"golden_core"`` is
     a rung rather than an unknown. A realm genuinely on neither -- such as
     ``"mahayana"``, which exists nowhere in this tree -- still falls back to
     ``mortal``.
  6. **A stored pool that is not a number is repaired, not fatal.** This used
     to LOCK the player out permanently: ``"corrupt"``, ``null``, ``[]`` and
     ``{}`` read back as a clean ``0`` but made BOTH settlement routes answer
     500, forever, with no API path out -- the pool could only be rewritten by
     the two routes that crashed on it, and because the tier change sat inside
     a transaction, every retry unwound it too. A player who had already paid
     for a comprehension could never settle it. ``tupo`` also took the
     materials charge BEFORE the pool write, so a failed settlement stranded
     the charge with no refund path. Both halves are FIXED and pinned in
     section 16: the read and write paths now share one number conversion, and
     ``tupo`` charges after the write rather than before it.
  7. **The read route swallows the three action paths.** ``GET
     /inner-skills/comprehend`` answers ``404 "Player not found"``, because
     ``/inner-skills/{player_id}`` is registered first and treats the word as
     a player id. A client probing for a route with the wrong method is told
     it does not exist.
  8. **Identifiers are exact.** No trimming, no case folding: ``"QI_SENSE"``
     and ``" qi_sense"`` are unknown skills (404), and a non-string
     ``player_id``/``skill_id`` is a 422 rather than a coerced lookup.

THE TWO RULES A CLIENT MUST NOT BREAK
--------------------------------------
  * Read the price from ``catalog[].learn_cost`` / ``catalog[].tupo_cost``.
    Never send a price. The server ignores whatever is sent.
  * Branch on ``state``, never on the HTTP status. Beginning a skill that is
    already running is a 409, and that is the ONLY thing standing between a
    double-click and a second charge.

WHERE THE AUTH COMES FROM
-------------------------
``register_inner_skill_routes`` declares NO auth dependency of its own -- a
bare mount answers 200 to a callerless request. Auth arrives because
``main.py`` calls ``ownership_guard.install_ownership_guard(app)`` AFTER this
module mounts, and the guard rewrites the routes in place. So the auth contract
is real but it is NOT a property of this module: both halves are asserted here,
because a registration-order change in ``main.py`` would silently reopen all
four routes and no test inside ``inner_skills.py`` could see it.

NEGATIVE CONTROLS
-----------------
Several tests assert a RELATION the code cannot satisfy if a guard is removed,
and carry the control in the same test: the money assertions are checked
against the inventory store's own charge log, so deleting the idempotency
receipt lookup doubles the recorded charges and the test goes red; the
cross-player arm proves 403 rather than merely proving a 200 did not come
back.

Run (memory mode -- unset the Postgres vars or the run HANGS to the timeout
instead of failing):

  env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
      -u PYTHONPATH <python> -m pytest server/tests/test_inner_skills_contract.py -q
"""
from __future__ import annotations

import os

# Popped BEFORE any import so a stray ambient value cannot drag this file into
# a live-Postgres branch. A bare run without this HANGS rather than fails.
for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_var, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
if str(_SERVER.parent) not in sys.path:
    sys.path.insert(0, str(_SERVER.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import inner_skills as isk
import ownership_guard
from player_store import PlayerStore


# --------------------------------------------------------------------------
# The four routes, exactly. This is the whole client-facing surface.
# --------------------------------------------------------------------------
ROUTE_TABLE = {
    ("GET", "/inner-skills/{player_id}"),
    ("POST", "/inner-skills/comprehend"),
    ("POST", "/inner-skills/tupo"),
    ("POST", "/inner-skills/claim"),
}

# The two routes that demand an Idempotency-Key, and the one that does not.
ACTION_ROUTES = ("/inner-skills/comprehend", "/inner-skills/tupo", "/inner-skills/claim")
IDEMPOTENT_ROUTES = ("/inner-skills/comprehend", "/inner-skills/tupo")
NON_IDEMPOTENT_ROUTES = ("/inner-skills/claim",)

# Reply key sets, measured from live replies. Frozen deliberately: a client
# reads these names, so ADDING one is safe but RENAMING one breaks callers.
STATE_KEYS = {"ok", "es_points", "es_max", "skills", "catalog", "server_now"}
CATALOG_ROW_KEYS = {
    "id", "name", "description", "required_realm",
    "learn_cost", "learn_seconds", "tupo_cost", "tupo_seconds",
}
ACTION_REPLY_KEYS = {"ok", "skill_id", "state", "started_at", "required_seconds", "server_now"}
SKILL_ROW_KEYS = {
    "state", "level", "es_points", "started_at", "remaining_seconds", "name", "description",
}

# Body fields a client must never send. The server ignores all of them (the
# price comes from the module's own table), but a route that STARTED reading
# one would let the caller price their own reward.
FORBIDDEN_BODY_FIELDS = ["amount", "quantity", "cost", "es_points", "level", "price", "reward"]


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class RecordingInventory:
    """Stands in for the canonical inventory store.

    Records every charge so a test can assert on what the SERVER asked for,
    which is the whole point: comparing the charge to the module's own skill
    table means rebalancing the economy leaves these tests green, while a route
    that computed its own copy of a price still goes red.

    ``charges`` is the ATTEMPT log -- ``consume_items`` is called before the
    store decides, so a refusal still appears in it. ``granted`` is the log of
    charges the store actually let through, and it is the only one that answers
    "did the player lose materials?". Conflating the two hides a stranded
    charge behind an attempt that the store refused anyway.

    ``add_item`` exists because ``_refund`` calls it, and it was MISSING here.
    The module's refund is a bare ``except Exception: pass``, so a test double
    without ``add_item`` raised AttributeError inside the refund and the
    materials silently vanished -- meaning EVERY test in this file was blind to
    a lost charge. ``refunds`` is the log of what the module handed back, so a
    test can now assert the charge AND its refund instead of assuming.
    """

    def __init__(self, refuse: bool = False, answer: dict | None = None):
        self.refuse = refuse
        self.answer = answer
        self.charges: list[tuple[str, dict]] = []
        self.granted: list[tuple[str, dict]] = []
        self.refunds: list[tuple[str, str, int]] = []
        self.refund_failures: list[str] = []

    def net_charged(self, pid: str) -> int:
        """Materials this player is actually out of pocket for.

        Grants minus refunds. A stranded charge is one that was taken and
        never given back, so this -- not ``granted_count()`` -- is the number
        that answers "did the player lose materials?".
        """
        taken = sum(qty for charged, costs in self.granted if charged == pid
                    for qty in costs.values())
        returned = sum(qty for charged, _item, qty in self.refunds if charged == pid)
        return taken - returned

    def add_item(self, player_id: str, item_id: str, quantity: int = 1) -> dict:
        """The real inventory store's refund entry point (``inventory.py:457``).

        ``fail_refund`` turns this into a store whose own refund is broken, so
        a test can prove the module's best-effort contract: the ORIGINAL error
        still reaches the client instead of being replaced by the refund's.
        """
        if getattr(self, "fail_refund", False):
            self.refund_failures.append(item_id)
            raise RuntimeError("refund store unavailable")
        self.refunds.append((player_id, item_id, quantity))
        return {"ok": True}

    def charge_count(self) -> int:
        return len(self.charges)

    def granted_count(self) -> int:
        """Charges that were actually taken from the player."""
        return len(self.granted)

    def last_charge(self, pid: str) -> dict:
        """The costs dict of the most recent charge for this player."""
        return [costs for charged, costs in self.charges if charged == pid][-1]

    def charged_quantities(self, pid: str) -> list:
        """Every quantity the server asked for, one entry per charge."""
        return [qty for charged, costs in self.charges
                if charged == pid for qty in costs.values()]

    def consume_items(self, player_id: str, costs: dict) -> dict:
        self.charges.append((player_id, dict(costs)))
        if self.answer is not None:
            result = self.answer
        elif self.refuse:
            result = {"ok": False, "error": "insufficient items"}
        else:
            result = {"ok": True}
        if result.get("ok"):
            self.granted.append((player_id, dict(costs)))
        return result


class OrderProbeInventory(RecordingInventory):
    """Records what the STORED player row looked like at the moment of charge.

    This is the only way to observe WHERE in ``begin_tupo`` the charge happens.
    The route's own return value cannot say: by the time it answers, the pool
    has been written either way, and whether the charge preceded or followed
    that write is invisible from the outside.

    The charge used to run FIRST, so a probe at that moment saw the pool as the
    player last stored it -- often a corrupt value that the very next line then
    crashed on, taking the materials with it. A charge that runs LAST sees a
    pool that has already been repaired and credited.
    """

    def __init__(self, player_id: str, **kw):
        super().__init__(**kw)
        self._store = None
        self._pid = player_id
        self.pool_at_charge: object = "never-charged"

    def bind(self, store) -> None:
        """Attach the store AFTER ``_app`` has built it."""
        self._store = store

    def consume_items(self, player_id: str, costs: dict) -> dict:
        if player_id == self._pid and self._store is not None:
            # Read through the store so this is the STORED value, not the copy
            # the transaction is holding.
            with self._store.transaction(player_id):
                self.pool_at_charge = self._store[player_id]["inner_skills"].get("es_points")
        return super().consume_items(player_id, costs)


def _player(pid: str, realm: str = "mortal") -> dict:
    return {"id": pid, "name": pid, "cultivation_realm": realm}


def _app(*players: dict, refuse_inventory: bool = False,
         inventory="default", guarded: bool = False):
    """A throwaway app carrying ONLY the inner-skill routes.

    Not ``main.app``: this file measures the contract of these four routes, not
    whatever else a shared tree happens to have registered today, and
    ``main.py`` is edited by several agents at once.
    """
    app = FastAPI()
    store = PlayerStore()
    with store.transaction():
        for player in players:
            store[player["id"]] = player
    if inventory == "default":
        inventory = RecordingInventory(refuse=refuse_inventory)
    isk.register_inner_skill_routes(app, lambda: store, inventory)
    if guarded:
        ownership_guard.install_ownership_guard(app)
    return app, store, inventory, TestClient(app, raise_server_exceptions=False)


def _one(pid: str = "p1", realm: str = "mortal", **kw):
    return _app(_player(pid, realm), **kw)


def _authed(pid: str) -> dict:
    return {"Authorization": "Bearer %s" % auth.issue_session(pid)}


def _idem(key: str) -> dict:
    return {"Idempotency-Key": key}


def _fast_forward(store, pid: str, skill_id: str, seconds: float) -> None:
    """Move a running skill's clock into the past.

    The timer is read from the row's ``started_at`` against the wall clock, so
    that is the only lever. Written THROUGH the store: ``PlayerStore.__getitem__``
    hands out a copy, so a bare ``store[pid][...][...] = x`` lands on a
    throwaway and the skill can never be completed.
    """
    with store.transaction(pid):
        store[pid]["inner_skills"]["skills"][skill_id]["started_at"] -= seconds + 1


def _comprehend(client, store, pid, skill_id, key):
    """Start comprehension, then jump the clock past the wait."""
    body = {"player_id": pid, "skill_id": skill_id}
    reply = client.post("/inner-skills/comprehend", json=body, headers=_idem(key))
    assert reply.status_code == 200, reply.text
    _fast_forward(store, pid, skill_id, isk.SKILLS[skill_id].learn_seconds)
    return reply


def _drive_to_active(client, store, pid, skill_id) -> None:
    """comprehend -> tupo -> active, fast-forwarding both timers.

    The route contract read from ``register_inner_skill_routes``: the three
    action routes are FLAT (``/inner-skills/{comprehend,tupo,claim}``), not
    nested under the player, and take ``{player_id, skill_id}`` in the body.
    Only the first two carry an ``Idempotency-Key``.
    """
    _comprehend(client, store, pid, skill_id, "c-" + skill_id)
    tupo = client.post("/inner-skills/tupo", json={"player_id": pid, "skill_id": skill_id},
                       headers=_idem("t-" + skill_id))
    assert tupo.status_code == 200, tupo.text
    _fast_forward(store, pid, skill_id, isk.SKILLS[skill_id].tupo_seconds)
    claim = client.post("/inner-skills/claim", json={"player_id": pid, "skill_id": skill_id})
    assert claim.status_code == 200, claim.text
    assert claim.json()["skills"][skill_id]["state"] == "active", claim.text


# ==========================================================================
# 1. The route table
# ==========================================================================

def test_exactly_four_routes_are_mounted():
    """No fifth route, and no method smuggled onto one of the four."""
    app, _, _, _ = _one()
    mounted = {
        (method, route.path)
        for route in app.routes if hasattr(route, "methods")
        for method in route.methods
        # FastAPI adds HEAD to every GET; comparing the raw set would fail on
        # a detail the client never sees.
        if not route.path.startswith(("/docs", "/redoc", "/openapi"))
    }
    assert mounted == ROUTE_TABLE, mounted


def test_the_read_route_is_a_path_param_and_the_actions_are_flat_body_routes():
    """A client must not nest the action calls under the player id."""
    app, _, _, _ = _one()
    by_path = {r.path: sorted(r.methods - {"HEAD"}) for r in app.routes if hasattr(r, "methods")}
    assert by_path["/inner-skills/{player_id}"] == ["GET"]
    for path in ACTION_ROUTES:
        assert by_path[path] == ["POST"], path
    assert not any("{" in path for path in ACTION_ROUTES), (
        "an action route grew a path parameter; the body is the contract"
    )


# ==========================================================================
# 2. The read route: reply shape and refusals
# ==========================================================================

def test_state_reply_carries_exactly_the_published_keys():
    _, _, _, client = _one()
    body = client.get("/inner-skills/p1").json()
    assert set(body) == STATE_KEYS, sorted(body)
    assert body["ok"] is True
    assert body["skills"] == {}
    assert body["es_max"] == isk.MAX_ES_POINTS
    assert body["es_points"] == 0
    assert isinstance(body["server_now"], (int, float))


def test_catalog_publishes_every_skill_with_its_server_side_prices():
    """Prices are READ, never sent. Read them from here to render the shop."""
    _, _, _, client = _one()
    catalog = client.get("/inner-skills/p1").json()["catalog"]
    assert {row["id"] for row in catalog} == set(isk.SKILLS)
    for row in catalog:
        assert set(row) == CATALOG_ROW_KEYS, sorted(row)
        definition = isk.SKILLS[row["id"]]
        assert row["learn_cost"] == {
            "item_id": definition.learn_material,
            "quantity": definition.learn_material_amount,
        }
        assert row["tupo_cost"] == {
            "item_id": definition.tupo_material,
            "quantity": definition.tupo_material_amount,
        }
        assert row["required_realm"] == definition.required_realm


def test_every_declared_realm_is_one_the_ladder_knows():
    """The gate indexes into ``REALM_ORDER``; an unknown rung would refuse all."""
    assert {d.required_realm for d in isk.SKILLS.values()} <= set(isk.REALM_ORDER)


def test_the_ladder_carries_every_realm_the_live_cultivation_config_names():
    """``REALM_ORDER`` is DERIVED, so a live realm can never be a silent rank of nothing.

    The gate refuses by RANK, so a realm missing from the ladder resolves to
    ``mortal`` and every paid skill is closed to it. ``REALM_ORDER`` used to be
    a bare three-name list; ``"foundation"`` -- the SECOND realm on the live
    ladder -- matched no entry, so a player stored that way could never open
    ``body_refinement`` or ``spirit_sense`` and the pool could never fill.

    Pinned as a RELATIONSHIP (every live realm RESOLVES, and every non-mortal
    one ranks above ``mortal``), not as a frozen list, so adding a realm to
    ``cultivation_config`` does not redden this test -- it just has to land on a
    rung, either directly or through ``REALM_ALIASES``. The assertion is on
    ``_resolve_realm`` rather than on ``in REALM_ORDER`` because a live realm
    spelled short ("core") is expected to resolve through the alias table, not
    to be a literal member of the ladder.
    """
    live = sorted(isk._load_cultivation_config().REALMS, key=lambda r: r.order)
    assert live, "the live ladder published no realms at all"
    for realm in live:
        assert realm.name in isk.REALM_ORDER or realm.name in isk.REALM_ALIASES, (
            "live realm %r is on neither the gate ladder %r nor its alias table; "
            "it would resolve to mortal and refuse every paid skill"
            % (realm.name, isk.REALM_ORDER)
        )
        resolved = isk._resolve_realm(realm.name)
        assert resolved in isk.REALM_ORDER and (
            realm.name in isk.REALM_ORDER or resolved != "mortal"
        ), (
            "live realm %r resolves to %r, which is not a rung it may reach "
            "through the alias table" % (realm.name, resolved)
        )
        if realm.name != "mortal":
            # Through _resolve_realm, because that is how every production
            # caller reads a stored realm: _check_realm is written
            # ``_realm_index(_resolve_realm(row[...]))``. _realm_index itself
            # indexes a name it has ALREADY been given and answers -1 for
            # anything else, which is what makes an un-normalised short name
            # score below mortal rather than crash.
            assert isk._realm_index(resolved) > isk._realm_index("mortal"), (
                "live realm %r resolves to %r, which ranks no higher than mortal"
                % (realm.name, resolved)
            )


def test_the_ladder_is_the_canonical_eight_realm_standard():
    """The gate ladder is the SAME eight rungs, in the same order, that
    ``ZoneManager.REALM_ORDER`` and ``NPCManager.REALM_ORDER`` declare.

    This is the relationship the module previously inverted: the ladder was
    built by concatenating the gate tiers with the raw names in
    ``cultivation_config.REALMS``, which produced eight entries that were the
    WRONG eight -- "foundation" and "foundation_establishment" both present as
    separate rungs, and "golden_core", "spirit_severing", "dao_merging" and
    "true_immortal" absent. Every player in the top half of the game therefore
    resolved to "mortal" and was refused all three inner skills.

    Asserted as an equality against the two other declarations rather than a
    frozen literal, so the three modules cannot drift apart again without
    reddening this.
    """
    from server.zones import ZoneManager
    from server.npcs import NPCManager
    assert list(isk.REALM_ORDER) == list(ZoneManager.REALM_ORDER) == list(NPCManager.REALM_ORDER), (
        "inner-skill ladder %r disagrees with zones %r / npcs %r"
        % (list(isk.REALM_ORDER), list(ZoneManager.REALM_ORDER), list(NPCManager.REALM_ORDER))
    )


def test_every_short_name_the_player_store_writes_resolves_to_its_rung():
    """A stored short name must rank where the long name ranks, not at the bottom.

    ``cultivation_config.REALMS`` publishes "foundation", "core",
    "soul_transformation" and "dao_seeking"; the ladder spells those rungs
    "foundation_establishment", "golden_core", "spirit_severing" and
    "dao_merging". Before the alias table every one of them fell through to
    "mortal".
    """
    for short, long_form in isk.REALM_ALIASES.items():
        assert long_form in isk.REALM_ORDER, (
            "alias %r points at %r, which is not a rung" % (short, long_form)
        )
        assert isk._realm_index(isk._resolve_realm(short)) == isk._realm_index(long_form), (
            "stored %r ranks %d but %r ranks %d"
            % (short, isk._realm_index(isk._resolve_realm(short)),
               long_form, isk._realm_index(long_form))
        )


def test_the_declared_gate_tiers_keep_their_ranks():
    """The three tiers lead the ladder, in order, and nothing shadows them.

    ``GATE_TIERS`` names the rows production writes for inner skills; the
    derived tail is appended AFTER them so no stored row re-reads at a
    different rank than it did under the old three-name list.
    """
    assert isk.REALM_ORDER[:len(isk.GATE_TIERS)] == list(isk.GATE_TIERS)
    ranks = [isk._realm_index(tier) for tier in isk.GATE_TIERS]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks), ranks
    assert len(set(isk.REALM_ORDER)) == len(isk.REALM_ORDER), "a rung is listed twice"


def test_read_of_an_unknown_player_is_a_clean_404():
    """The ONE route that answers 404 for a missing player (see file header)."""
    _, _, _, client = _one()
    reply = client.get("/inner-skills/ghost")
    assert reply.status_code == 404
    assert reply.json()["detail"] == "Player not found"


# ==========================================================================
# 3. The request body contract
# ==========================================================================

@pytest.mark.parametrize("path", ACTION_ROUTES)
@pytest.mark.parametrize("body,missing", [
    ({}, "player_id"),
    ({}, "skill_id"),
    ({"player_id": "p1"}, "skill_id"),
    ({"skill_id": "qi_sense"}, "player_id"),
])
def test_action_routes_require_both_identifiers(path, body, missing):
    _, _, _, client = _one()
    reply = client.post(path, json=body)
    assert reply.status_code == 422, reply.text
    assert missing in [e["loc"][-1] for e in reply.json()["detail"]]


@pytest.mark.parametrize("path", ACTION_ROUTES)
@pytest.mark.parametrize("field", ["player_id", "skill_id"])
def test_an_empty_identifier_is_refused_by_the_length_bound(path, field):
    """min_length=1, so "" is a 422 rather than a lookup for a player named ""."""
    _, _, _, client = _one()
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    body[field] = ""
    reply = client.post(path, json=body)
    assert reply.status_code == 422, reply.text
    error = next(e for e in reply.json()["detail"] if e["loc"][-1] == field)
    assert error["type"] == "string_too_short"


@pytest.mark.parametrize("path", IDEMPOTENT_ROUTES)
@pytest.mark.parametrize("field", FORBIDDEN_BODY_FIELDS)
def test_a_client_supplied_price_is_ignored_not_obeyed(path, field):
    """The money assertion for the two PAID actions.

    Scoped to ``comprehend``/``tupo`` because those are the only routes that
    charge. The reply is compared against the inventory's recorded charge, not
    against a literal, and the strays are sent one at a time so a route reading
    any single one shows up.
    """
    _, _, inventory, client = _one()
    skill_id = "qi_sense"
    body = {"player_id": "p1", "skill_id": skill_id, field: 999999}
    key = "price-%s-%s" % (path.rsplit("/", 1)[-1], field)
    reply = client.post(path, json=body, headers=_idem(key))
    if reply.status_code == 409:
        # A refusal is fine as long as it refused BEFORE honouring the number.
        return
    assert reply.status_code == 200, reply.text
    definition = isk.SKILLS[skill_id]
    allowed = {definition.learn_material_amount, definition.tupo_material_amount}
    for _, costs in inventory.charges:
        for item, quantity in costs.items():
            assert quantity in allowed, (
                "route charged %s=%s, which is the CALLER's number" % (item, quantity)
            )


@pytest.mark.parametrize("field", FORBIDDEN_BODY_FIELDS)
def test_claim_charges_nothing_at_all_however_the_body_is_dressed_up(field):
    """``claim`` is the settlement route and must never touch materials.

    It is driven to a genuinely ACTIVE skill first, so this is not passing
    because the route refused early.
    """
    _, store, inventory, client = _one()
    _drive_to_active(client, store, "p1", "qi_sense")
    before = inventory.charge_count()
    reply = client.post("/inner-skills/claim",
                        json={"player_id": "p1", "skill_id": "qi_sense", field: 999999})
    assert reply.status_code == 200, reply.text
    assert inventory.charge_count() == before, "claim charged materials"


# ==========================================================================
# 4. The Idempotency-Key requirement
# ==========================================================================

@pytest.mark.parametrize("path", IDEMPOTENT_ROUTES)
def test_a_paid_action_without_an_idempotency_key_is_refused(path):
    _, _, inventory, client = _one()
    reply = client.post(path, json={"player_id": "p1", "skill_id": "qi_sense"})
    assert reply.status_code == 400
    assert reply.json()["detail"] == "Idempotency-Key required"
    assert inventory.charge_count() == 0, "a refused request still took materials"


@pytest.mark.parametrize("path", IDEMPOTENT_ROUTES)
def test_an_empty_idempotency_key_counts_as_absent(path):
    _, _, _, client = _one()
    reply = client.post(path, json={"player_id": "p1", "skill_id": "qi_sense"},
                        headers={"Idempotency-Key": ""})
    assert reply.status_code == 400
    assert reply.json()["detail"] == "Idempotency-Key required"


@pytest.mark.parametrize("path", NON_IDEMPOTENT_ROUTES)
def test_claim_needs_no_idempotency_key(path):
    """Measured disagreement #2: only the two PAID routes carry the header."""
    _, _, _, client = _one()
    reply = client.post(path, json={"player_id": "p1", "skill_id": "qi_sense"})
    assert reply.status_code == 404, reply.text
    assert reply.json()["detail"] == "Skill state not found"


# ==========================================================================
# 5. Comprehension: the happy path, replay, and key reuse
# ==========================================================================

def test_begin_comprehension_returns_the_published_keys_and_charges_the_server_price():
    _, _, inventory, client = _one()
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("k1"))
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert set(body) == ACTION_REPLY_KEYS, sorted(body)
    assert body["ok"] is True
    assert body["state"] == "comprehending"
    assert body["skill_id"] == "qi_sense"
    definition = isk.SKILLS["qi_sense"]
    assert body["required_seconds"] == definition.learn_seconds
    assert inventory.charges == [("p1", {definition.learn_material: definition.learn_material_amount})]


def test_replaying_the_same_key_charges_once_and_repeats_the_stored_reply():
    """The once-only control. Deleting the receipt lookup doubles ``charges``."""
    _, _, inventory, client = _one()
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    first = client.post("/inner-skills/comprehend", json=body, headers=_idem("same"))
    second = client.post("/inner-skills/comprehend", json=body, headers=_idem("same"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json(), "the replay did not repeat the stored reply"
    assert inventory.charge_count() == 1, "the replay charged again: %s" % (inventory.charges,)


def test_the_internal_replay_marker_never_reaches_the_client():
    _, _, _, client = _one()
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    client.post("/inner-skills/comprehend", json=body, headers=_idem("m"))
    replay = client.post("/inner-skills/comprehend", json=body, headers=_idem("m")).json()
    assert "_replayed" not in replay, "an internal marker leaked onto the wire"
    assert set(replay) == ACTION_REPLY_KEYS


def test_reusing_a_key_with_a_different_skill_is_a_409():
    _, _, _, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("reuse"))
    clash = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "body_refinement"},
                        headers=_idem("reuse"))
    assert clash.status_code == 409
    assert clash.json()["detail"] == "Idempotency key reused with different payload"


def test_one_players_key_never_replays_another_players_receipt():
    """The key is scoped to (player, key), so a shared key is not a shared answer."""
    _, _, inventory, client = _app(_player("p1"), _player("p2"))
    for pid in ("p1", "p2"):
        reply = client.post("/inner-skills/comprehend",
                            json={"player_id": pid, "skill_id": "qi_sense"},
                            headers=_idem("shared"))
        assert reply.status_code == 200, reply.text
        assert reply.json()["skill_id"] == "qi_sense"
    assert inventory.charge_count() == 2, "one player's key swallowed the other's action"
    assert {pid for pid, _ in inventory.charges} == {"p1", "p2"}


def test_beginning_a_running_skill_again_is_refused_and_costs_nothing():
    _, _, inventory, client = _one()
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    assert client.post("/inner-skills/comprehend", json=body, headers=_idem("a")).status_code == 200
    charged_once = inventory.charge_count()
    again = client.post("/inner-skills/comprehend", json=body, headers=_idem("b"))
    assert again.status_code == 409
    assert again.json()["detail"] == "Skill is not unlearned"
    assert inventory.charge_count() == charged_once


def test_comprehending_an_unknown_skill_is_a_404_and_costs_nothing():
    _, _, inventory, client = _one()
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "no_such_skill"},
                        headers=_idem("k"))
    assert reply.status_code == 404
    assert reply.json()["detail"] == "Inner skill not found"
    assert inventory.charge_count() == 0


# ==========================================================================
# 6. The realm gate
# ==========================================================================

def test_the_realm_gate_admits_exactly_the_realms_each_skill_declares():
    """The gate is read from each skill's own ``required_realm``.

    Driven over EVERY rung the ladder publishes, not just the three declared
    tiers: the tiers are what inner skills gate on, but the ladder also carries
    the live cultivation realms and those players reach the same routes. A rung
    the gate mishandled used to look fine here, because only the tiers were
    ever driven.
    """
    for pid, realm in ((r, r) for r in isk.REALM_ORDER):
        for skill_id, definition in isk.SKILLS.items():
            _, _, _, client = _one(pid, realm)
            reply = client.post("/inner-skills/comprehend",
                                json={"player_id": pid, "skill_id": skill_id},
                                headers=_idem("g-" + skill_id))
            admitted = isk.REALM_ORDER.index(realm) >= isk.REALM_ORDER.index(definition.required_realm)
            assert reply.status_code == (200 if admitted else 409), (
                "%s at %s wanted %s -> %s" % (skill_id, realm, definition.required_realm, reply.status_code)
            )
            if not admitted:
                assert reply.json()["detail"] == "Realm requirement not met"


def test_a_live_realm_player_is_ranked_by_the_ladder_not_floored_to_mortal():
    """Every realm the live cultivation config names opens EVERY skill.

    This is the behaviour the derived ladder bought. ``"foundation"`` is the
    second realm on the live ladder but was never a declared tier; under the
    old three-name ladder it resolved to ``mortal`` and was refused both paid
    skills, so the inner-skill pool could never fill for those players. Driven
    over the wire because the module's own ``_realm_index`` agreeing with
    itself proves nothing about the route.
    """
    live = sorted(isk._load_cultivation_config().REALMS, key=lambda r: r.order)
    non_mortal = [r.name for r in live if r.name != "mortal"]
    assert non_mortal, "the live ladder published no realm above mortal"
    for realm in non_mortal:
        pid = "live-" + realm
        _, _, _, client = _one(pid, realm)
        for skill_id in isk.SKILLS:
            reply = client.post("/inner-skills/comprehend",
                                json={"player_id": pid, "skill_id": skill_id},
                                headers=_idem("live-" + realm + "-" + skill_id))
            assert reply.status_code == 200, (
                "live realm %r was refused %s (%s): %s"
                % (realm, skill_id, reply.status_code, reply.text)
            )


def test_the_gate_accepts_a_realm_stored_in_either_spelling():
    """Measured disagreement #5: the spaced production form is normalised."""
    for stored in ("foundation_establishment", "Foundation Establishment"):
        _, _, _, client = _one("p1", stored)
        for skill_id in isk.SKILLS:
            reply = client.post("/inner-skills/comprehend",
                                json={"player_id": "p1", "skill_id": skill_id},
                                headers=_idem("s-" + skill_id))
            assert reply.status_code == 200, (stored, skill_id, reply.text)


def test_an_unrecognised_realm_is_treated_as_the_lowest_one():
    """A realm this build has never heard of must not unlock a paid skill.

    The probe is "mahayana": it is on neither the ladder nor the alias table,
    and it appears nowhere else in the tree. This test used to probe
    "golden_core", which was only unknown because the ladder was the WRONG
    eight -- the defect this file's ladder tests now pin. A probe that is a
    real rung no longer measures the unknown-realm path at all.
    """
    assert "mahayana" not in isk.REALM_ORDER and "mahayana" not in isk.REALM_ALIASES
    _, _, _, client = _one("p1", "mahayana")
    for skill_id, definition in isk.SKILLS.items():
        reply = client.post("/inner-skills/comprehend",
                            json={"player_id": "p1", "skill_id": skill_id},
                            headers=_idem("u-" + skill_id))
        if definition.required_realm != "mortal":
            assert reply.status_code == 409, (skill_id, reply.text)


def test_the_gate_reads_cultivation_realm_the_key_production_writes():
    """A player carrying only ``realm`` must still look like ``mortal``.

    ``_check_realm`` reads ``cultivation_realm``. Seeding the differently-named
    key must not open the gate, because nothing in ``server/`` writes ``realm``.
    """
    app = FastAPI()
    store = PlayerStore()
    with store.transaction():
        store["p1"] = {"id": "p1", "realm": "foundation_establishment"}
    isk.register_inner_skill_routes(app, lambda: store, RecordingInventory())
    client = TestClient(app, raise_server_exceptions=False)
    gated = [sid for sid, d in isk.SKILLS.items() if d.required_realm != "mortal"]
    for skill_id in gated:
        reply = client.post("/inner-skills/comprehend",
                            json={"player_id": "p1", "skill_id": skill_id},
                            headers=_idem("wrongkey-" + skill_id))
        assert reply.status_code == 409, (skill_id, reply.text)
        assert reply.json()["detail"] == "Realm requirement not met"


# ==========================================================================
# 7. Tupo: the second paid action
# ==========================================================================

def test_beginning_tupo_on_a_never_started_skill_is_a_409():
    _, _, inventory, client = _one()
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("t0"))
    assert reply.status_code == 409
    assert reply.json()["detail"] == "Skill is not ready for Tupo"
    assert inventory.charge_count() == 0


def test_beginning_tupo_before_the_comprehension_finishes_is_a_409():
    """A fast client must be told to wait, not silently advanced."""
    _, _, inventory, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("c"))
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("t"))
    assert reply.status_code == 409
    assert reply.json()["detail"] == "Comprehension still in progress"
    assert inventory.charge_count() == 1, "the refused Tupo still charged"


def test_tupo_charges_the_tupo_price_not_the_learn_price():
    _, store, inventory, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("t"))
    assert reply.status_code == 200, reply.text
    definition = isk.SKILLS["qi_sense"]
    assert reply.json()["required_seconds"] == definition.tupo_seconds
    # qi_sense charges the SAME item for both actions, so the two prices cannot
    # be told apart by summing the item. The last charge is the Tupo one.
    assert inventory.last_charge("p1") == {
        definition.tupo_material: definition.tupo_material_amount,
    }


def test_beginning_tupo_twice_is_refused_and_costs_nothing():
    _, store, inventory, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    assert client.post("/inner-skills/tupo", json=body, headers=_idem("t1")).status_code == 200
    charged = inventory.charge_count()
    again = client.post("/inner-skills/tupo", json=body, headers=_idem("t2"))
    assert again.status_code == 409
    assert again.json()["detail"] == "Skill is not ready for Tupo"
    assert inventory.charge_count() == charged


def test_tupo_replaying_the_same_key_does_not_charge_twice():
    _, store, inventory, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    first = client.post("/inner-skills/tupo", json=body, headers=_idem("once"))
    second = client.post("/inner-skills/tupo", json=body, headers=_idem("once"))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    definition = isk.SKILLS["qi_sense"]
    # Exactly two charges: the comprehension, then the Tupo. A replay that
    # charged again would make three.
    assert inventory.charged_quantities("p1") == [
        definition.learn_material_amount, definition.tupo_material_amount,
    ]


def test_tupo_on_an_unknown_skill_is_a_404():
    _, _, inventory, client = _one()
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": "no_such_skill"}, headers=_idem("t"))
    assert reply.status_code == 404
    assert reply.json()["detail"] == "Inner skill not found"
    assert inventory.charge_count() == 0


def test_tupo_can_complete_the_comprehension_itself_on_the_way_through():
    """Tupo does not need a prior ``claim``: it settles the comprehension first.

    Both orderings are legal for a client, so both are pinned.
    """
    _, store, inventory, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("t"))
    assert reply.status_code == 200, reply.text
    assert reply.json()["state"] == "tupo"
    assert client.get("/inner-skills/p1").json()["skills"]["qi_sense"]["state"] == "tupo"


# ==========================================================================
# 8. Claim: the settlement route
# ==========================================================================

def test_claim_of_a_skill_the_player_never_started_is_a_404():
    _, _, _, client = _one()
    reply = client.post("/inner-skills/claim",
                        json={"player_id": "p1", "skill_id": "qi_sense"})
    assert reply.status_code == 404
    assert reply.json()["detail"] == "Skill state not found"


def test_claim_of_an_unknown_skill_is_a_404_with_the_other_message():
    _, _, _, client = _one()
    reply = client.post("/inner-skills/claim",
                        json={"player_id": "p1", "skill_id": "no_such_skill"})
    assert reply.status_code == 404
    assert reply.json()["detail"] == "Inner skill not found"


def test_claim_returns_the_whole_state_not_a_skill_shaped_reply():
    """A client reading ``claim`` can reuse its state parser verbatim."""
    _, store, _, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    body = client.post("/inner-skills/claim", json={"player_id": "p1", "skill_id": "qi_sense"}).json()
    assert set(body) == STATE_KEYS, sorted(body)
    read = client.get("/inner-skills/p1").json()
    assert set(body) == set(read), "claim and the read route disagree on the shape"


def test_claiming_early_leaves_the_skill_running():
    """Claim settles nothing before its clock runs out."""
    _, store, _, client = _one()
    _comprehend_start_only = client.post(
        "/inner-skills/comprehend", json={"player_id": "p1", "skill_id": "qi_sense"},
        headers=_idem("c"))
    assert _comprehend_start_only.status_code == 200
    body = client.post("/inner-skills/claim", json={"player_id": "p1", "skill_id": "qi_sense"}).json()
    assert body["skills"]["qi_sense"]["state"] == "comprehending"
    assert body["es_points"] == 0, "an unfinished comprehension paid ES"


def test_claiming_a_finished_comprehension_settles_it_and_pays_one_point():
    _, store, _, client = _one()
    _comprehend(client, store, "p1", "qi_sense", "c")
    body = client.post("/inner-skills/claim", json={"player_id": "p1", "skill_id": "qi_sense"}).json()
    assert body["skills"]["qi_sense"]["state"] == "comprehend_complete"
    assert body["skills"]["qi_sense"]["level"] == 1
    assert body["es_points"] == 1


def test_claiming_a_finished_tupo_makes_the_skill_active_at_level_two():
    _, store, _, client = _one()
    _drive_to_active(client, store, "p1", "qi_sense")
    row = client.get("/inner-skills/p1").json()["skills"]["qi_sense"]
    assert row["state"] == "active"
    assert row["level"] == 2
    assert row["remaining_seconds"] == 0, "a finished row still advertises a wait"


def test_claiming_the_same_finished_skill_again_pays_nothing_extra():
    """The ES double-pay control. Removing the credit doubles ``es_points``."""
    _, store, _, client = _one()
    _drive_to_active(client, store, "p1", "qi_sense")
    settled = client.get("/inner-skills/p1").json()["es_points"]
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    for _ in range(3):
        client.post("/inner-skills/claim", json=body)
    assert client.get("/inner-skills/p1").json()["es_points"] == settled, (
        "re-claiming a finished skill paid ES again"
    )


def test_a_skill_row_reports_the_same_keys_while_running_and_once_finished():
    _, store, _, client = _one()
    _comprehend_start = client.post(
        "/inner-skills/comprehend", json={"player_id": "p1", "skill_id": "qi_sense"},
        headers=_idem("c"))
    assert _comprehend_start.status_code == 200
    running = client.get("/inner-skills/p1").json()["skills"]["qi_sense"]
    assert set(running) == SKILL_ROW_KEYS, sorted(running)
    _fast_forward(store, "p1", "qi_sense", isk.SKILLS["qi_sense"].learn_seconds)
    finished = client.post("/inner-skills/claim",
                           json={"player_id": "p1", "skill_id": "qi_sense"}).json()["skills"]["qi_sense"]
    assert set(finished) == SKILL_ROW_KEYS, sorted(finished)


def test_remaining_seconds_counts_down_from_the_published_wait():
    """The countdown is measured against the skill's own wait, not a literal."""
    _, _, _, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("c"))
    row = client.get("/inner-skills/p1").json()["skills"]["qi_sense"]
    wait = isk.SKILLS["qi_sense"].learn_seconds
    assert 0 < row["remaining_seconds"] <= wait, row
    assert row["remaining_seconds"] == pytest.approx(wait, abs=5)


def test_a_running_row_never_advertises_a_wait_of_zero_before_it_is_done():
    """A zero here tells the client "done" and the action route then refuses."""
    _, _, _, client = _one()
    for skill_id, definition in isk.SKILLS.items():
        reply = client.post("/inner-skills/comprehend",
                            json={"player_id": "p1", "skill_id": skill_id},
                            headers=_idem("z-" + skill_id))
        if reply.status_code != 200:
            continue  # realm-gated for this player
        row = client.get("/inner-skills/p1").json()["skills"][skill_id]
        assert row["remaining_seconds"] > 0, (skill_id, row)


# ==========================================================================
# 9. The ES pool
# ==========================================================================

def test_the_pool_fills_to_the_published_cap_across_every_skill():
    """Assert the cap is REACHED, not merely respected.

    A pool stranded at 0 also satisfies ``<= MAX_ES_POINTS``, so a ceiling-only
    assertion would let a never-credited pool pass.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    for skill_id in isk.SKILLS:
        _drive_to_active(client, store, "p1", skill_id)
    body = client.get("/inner-skills/p1").json()
    active = [row for row in body["skills"].values() if row["state"] == "active"]
    assert len(active) == len(isk.SKILLS)
    assert body["es_points"] == isk.MAX_ES_POINTS, (
        "%s skills active but the pool is %s of %s"
        % (len(active), body["es_points"], isk.MAX_ES_POINTS)
    )


def test_the_pool_never_exceeds_the_cap_however_many_times_a_skill_is_claimed():
    _, store, _, client = _one("p1", "foundation_establishment")
    for skill_id in isk.SKILLS:
        _drive_to_active(client, store, "p1", skill_id)
        for _ in range(3):
            client.post("/inner-skills/claim", json={"player_id": "p1", "skill_id": skill_id})
    assert client.get("/inner-skills/p1").json()["es_points"] <= isk.MAX_ES_POINTS


def test_comprehension_alone_pays_nothing_to_the_pool():
    """A started-but-unfinished skill must not move the counter."""
    _, _, _, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("c"))
    assert client.get("/inner-skills/p1").json()["es_points"] == 0


def test_tupo_settling_the_comprehension_pays_exactly_one_point():
    """The Tupo route can settle the comprehension on its way through.

    That path carries its own credit (``completed_here``), and it is the ONLY
    one that does -- when the player claimed the comprehension first, Tupo
    finds it already settled and credits nothing. Asserting the delta here is
    what makes that branch observable; nothing else in this file reaches it.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    before = client.get("/inner-skills/p1").json()["es_points"]
    _comprehend(client, store, "p1", skill_id, "c")
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id}, headers=_idem("t"))
    assert reply.status_code == 200, reply.text
    after = client.get("/inner-skills/p1").json()["es_points"]
    assert after - before == 1, (
        "the Tupo route settled the comprehension and moved the pool by %s, not 1"
        % (after - before)
    )


def test_tupo_after_a_claimed_comprehension_credits_nothing_itself():
    """The other half of the same branch: the credit belongs to whoever settled it."""
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    settled = client.post("/inner-skills/claim",
                          json={"player_id": "p1", "skill_id": skill_id}).json()["es_points"]
    assert client.post("/inner-skills/tupo", json={"player_id": "p1", "skill_id": skill_id},
                       headers=_idem("t")).status_code == 200
    after = client.get("/inner-skills/p1").json()["es_points"]
    assert after == settled, "Tupo paid a second point for an already-settled comprehension"


def test_one_skill_alone_moves_the_pool_by_exactly_its_own_two_points():
    """Isolates the credit from the cap.

    With all three skills active the pool lands exactly ON ``MAX_ES_POINTS``, so
    any EXTRA credit a double-pay bug adds is clamped away and invisible. This
    drives ONE skill and measures the delta on its own, which is below the cap
    and therefore observable.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    definition = isk.SKILLS[skill_id]
    before = client.get("/inner-skills/p1").json()["es_points"]

    _comprehend(client, store, "p1", skill_id, "c")
    after_comprehension = client.post(
        "/inner-skills/claim", json={"player_id": "p1", "skill_id": skill_id}).json()["es_points"]
    assert after_comprehension - before == 1, (
        "one settled comprehension moved the pool by %s, not 1"
        % (after_comprehension - before)
    )

    client.post("/inner-skills/tupo", json={"player_id": "p1", "skill_id": skill_id},
                headers=_idem("t"))
    _fast_forward(store, "p1", skill_id, definition.tupo_seconds)
    after_tupo = client.post(
        "/inner-skills/claim", json={"player_id": "p1", "skill_id": skill_id}).json()["es_points"]
    assert after_tupo - after_comprehension == 1, (
        "the Tupo transition moved the pool by %s, not 1"
        % (after_tupo - after_comprehension)
    )


def test_reclaiming_one_active_skill_does_not_move_the_pool_again():
    """The double-pay control BELOW the cap.

    The all-skills cap test cannot catch this: the pool is already at the
    ceiling, so an extra credit is clamped away. One skill leaves headroom.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _drive_to_active(client, store, "p1", skill_id)
    settled = client.get("/inner-skills/p1").json()["es_points"]
    assert settled < isk.MAX_ES_POINTS, "this control needs headroom to be meaningful"
    for _ in range(4):
        client.post("/inner-skills/claim", json={"player_id": "p1", "skill_id": skill_id})
    assert client.get("/inner-skills/p1").json()["es_points"] == settled, (
        "re-claiming an active skill paid ES again"
    )


def test_a_stored_pool_above_the_cap_is_clamped_on_the_way_out():
    """Isolates the CEILING, which the legitimate 3-skill maximum cannot reach.

    Earning the cap costs exactly ``2 * len(SKILLS)`` points, which today IS
    ``MAX_ES_POINTS``, so no amount of honest play ever pushes the stored pool
    past the ceiling. This seeds one and reads it back, which is the only way
    the ceiling is observable at all.
    """
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = {"es_points": isk.MAX_ES_POINTS + 50}
    body = client.get("/inner-skills/p1").json()
    assert body["es_points"] == isk.MAX_ES_POINTS, (
        "a pool of %s was published as %s" % (isk.MAX_ES_POINTS + 50, body["es_points"])
    )


def test_a_stored_pool_below_zero_is_clamped_on_the_way_out():
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = {"es_points": -99}
    assert client.get("/inner-skills/p1").json()["es_points"] == 0


def test_one_players_progress_does_not_touch_anothers_pool():
    _, store_a, _, client = _app(_player("p1", "foundation_establishment"),
                                 _player("p2", "foundation_establishment"))
    _drive_to_active(client, store_a, "p1", "qi_sense")
    assert client.get("/inner-skills/p1").json()["es_points"] > 0
    assert client.get("/inner-skills/p2").json()["es_points"] == 0


# ==========================================================================
# 10. The inventory: unavailable and refusing
# ==========================================================================

def test_a_missing_inventory_store_is_a_503_not_a_500():
    """Without the canonical store the server must refuse loudly, not guess."""
    _, _, _, client = _one(inventory=None)
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("k"))
    assert reply.status_code == 503
    assert reply.json()["detail"] == "Canonical inventory unavailable"


def test_a_refused_charge_is_a_409_and_starts_nothing():
    _, store, inventory, client = _one(refuse_inventory=True)
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("k"))
    assert reply.status_code == 409
    assert client.get("/inner-skills/p1").json()["skills"] == {}, (
        "a refused charge still started the skill"
    )


def test_a_refusal_puts_the_stores_own_object_in_detail_not_a_string():
    """Measured disagreement #3: this one detail is a DICT.

    Every other refusal on these routes uses a plain string, so a client that
    renders ``detail`` as text will print Python dict syntax here.
    """
    answer = {"ok": False, "error": "insufficient items",
              "missing": {"insight_scroll": 2}}
    _, _, _, client = _one(inventory=RecordingInventory(answer=answer))
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("k"))
    assert reply.status_code == 409
    detail = reply.json()["detail"]
    assert isinstance(detail, dict), "the refusal shape changed; update this client note"
    assert detail == answer


def test_a_refused_charge_leaves_the_skill_open_to_retry():
    """A refusal must not burn the player's one chance at that skill.

    The route holds its inventory store by closure, so the retry below is
    still refused -- what is pinned is that it is refused as a MATERIAL
    refusal and not as "Skill is not unlearned", which is what a route that
    started the skill before charging would answer.
    """
    _, _, _, client = _one(refuse_inventory=True)
    body = {"player_id": "p1", "skill_id": "qi_sense"}
    first = client.post("/inner-skills/comprehend", json=body, headers=_idem("k"))
    assert first.status_code == 409
    retry = client.post("/inner-skills/comprehend", json=body, headers=_idem("k2"))
    assert retry.status_code == 409
    assert retry.json()["detail"] != "Skill is not unlearned", (
        "the refused attempt left the skill locked"
    )
    assert client.get("/inner-skills/p1").json()["skills"] == {}


# ==========================================================================
# 11. The auth contract, and where it comes from
# ==========================================================================

@pytest.mark.parametrize("path", ACTION_ROUTES)
def test_a_paid_action_needs_a_session_token(path):
    _, _, _, client = _one(guarded=True)
    reply = client.post(path, json={"player_id": "p1", "skill_id": "qi_sense"})
    assert reply.status_code == 401


def test_the_read_route_needs_a_session_token():
    _, _, _, client = _one(guarded=True)
    assert client.get("/inner-skills/p1").status_code == 401


def test_a_bare_mount_declares_no_auth_of_its_own():
    """Why this file also tests the guard: the module alone is not protected.

    ``main.py`` installs ``ownership_guard`` AFTER this module mounts, and the
    guard rewrites routes in place. If that registration order ever changes,
    every inner-skill route silently becomes callerless.
    """
    app, _, _, _ = _one()
    for route in app.routes:
        if hasattr(route, "path") and route.path.startswith("/inner-skills"):
            assert not route.dependant.dependencies, (
                "%s grew an auth dependency of its own; the guard owns that"
                % route.path
            )


def test_the_guard_attaches_auth_to_all_four_routes():
    """The half a test inside ``inner_skills.py`` could never see."""
    app, _, _, _ = _one(guarded=True)
    guarded = {
        route.path for route in app.routes
        if hasattr(route, "dependant") and route.dependant.dependencies
    }
    assert guarded == {path for _, path in ROUTE_TABLE}, guarded


def test_a_session_may_only_act_on_its_own_player():
    """The cross-player control: proves 403, not merely "not a 200".

    A guard that silently ignored a mismatched body would also stop returning
    200 here, so the status AND the message are both asserted.
    """
    _, _, inventory, client = _app(_player("p1"), _player("p2"), guarded=True)
    for path in ACTION_ROUTES:
        reply = client.post(path, json={"player_id": "p2", "skill_id": "qi_sense"},
                            headers={**_authed("p1"), **_idem("cross-" + path.rsplit("/", 1)[-1])})
        assert reply.status_code == 403, (path, reply.status_code)
        assert reply.json()["detail"] == "player_id does not match the authenticated session"
    read = client.get("/inner-skills/p2", headers=_authed("p1"))
    assert read.status_code == 403
    assert inventory.charge_count() == 0, "a refused cross-player call still charged"


def test_the_auth_check_runs_before_the_idempotency_key_check():
    """Ordering matters: a callerless client must not learn anything at all."""
    _, _, _, client = _one(guarded=True)
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"})
    assert reply.status_code == 401
    assert reply.json()["detail"] == "invalid or missing session token"


def test_an_authenticated_caller_reaches_its_own_routes():
    _, _, inventory, client = _app(_player("p1"), guarded=True)
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"},
                        headers={**_authed("p1"), **_idem("own")})
    assert reply.status_code == 200, reply.text
    assert inventory.charge_count() == 1
    assert client.get("/inner-skills/p1", headers=_authed("p1")).status_code == 200


# ==========================================================================
# 12. A missing player -- the 500 this route USED to answer
# ==========================================================================

@pytest.mark.parametrize("path", ACTION_ROUTES)
def test_a_missing_player_on_an_action_route_answers_the_intended_404(path):
    """FIXED DEFECT, now pinned as ordinary behaviour.

    This used to answer a bare ``500`` with a non-JSON body. The module's
    ``_require_player`` raises ``404 "Player not found"`` and always has, but
    the action helpers opened ``players_db.transaction(player_id)`` BEFORE they
    called it, and that transaction PRELOADS the row -- raising ``KeyError``
    one line earlier, with nothing to convert it. The 404 branch was therefore
    unreachable over HTTP and the read route contradicted the three action
    routes on the same player id.

    The body is asserted as well as the status: the old reply was the bare text
    ``Internal Server Error``, so a client doing ``response.json()["detail"]``
    crashed rather than reading a message. Both halves are the contract now.
    """
    _, _, inventory, client = _one()
    reply = client.post(path, json={"player_id": "ghost", "skill_id": "qi_sense"},
                        headers=_idem("ghost-" + path.rsplit("/", 1)[-1]))
    assert reply.status_code == 404, reply.text
    assert reply.json()["detail"] == "Player not found", reply.text
    assert inventory.charge_count() == 0, "a missing player was still charged"


def test_a_missing_player_answers_the_same_404_on_all_four_routes():
    """The read route already did this; the three action routes now agree.

    Pinned as a relation between the two halves of the module rather than
    against a literal, so a future change to either half that reintroduces the
    disagreement shows up here instead of in a client.
    """
    _, _, _, client = _one()
    read = client.get("/inner-skills/ghost")
    assert read.status_code == 404
    for path in ACTION_ROUTES:
        action = client.post(path, json={"player_id": "ghost", "skill_id": "qi_sense"},
                             headers=_idem("agree-" + path.rsplit("/", 1)[-1]))
        assert action.status_code == read.status_code, path
        assert action.json()["detail"] == read.json()["detail"], path


def test_the_404_branch_the_module_intends_does_exist():
    """Why the 500 above is a defect and not the design."""
    _, store, _, _ = _one()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        isk._require_player(store, "ghost")
    assert caught.value.status_code == 404
    assert caught.value.detail == "Player not found"


def test_a_missing_player_does_not_write_anything():
    _, _, inventory, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "ghost", "skill_id": "qi_sense"}, headers=_idem("g"))
    assert inventory.charge_count() == 0, "a missing player was still charged"


# ==========================================================================
# 13. Stored state the module has to survive
# ==========================================================================

def test_a_block_that_is_not_a_dict_still_answers_the_read():
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = "corrupt"
    reply = client.get("/inner-skills/p1")
    assert reply.status_code == 200, reply.text
    assert reply.json()["skills"] == {}


def test_a_partial_or_wrongly_typed_block_is_repaired_rather_than_500():
    """A row written by an older build must not lock the player out."""
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = {
            "es_points": -99, "skills": "not-a-dict",
            "receipts": "not-a-dict", "receipt_order": "not-a-list",
        }
    reply = client.get("/inner-skills/p1")
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["es_points"] == 0, "a corrupt negative pool reached the client"
    assert body["skills"] == {}


def test_an_action_route_repairs_a_corrupt_block_instead_of_500():
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = {"skills": "not-a-dict"}
    reply = client.post("/inner-skills/comprehend",
                        json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("k"))
    assert reply.status_code == 200, reply.text


def test_a_retired_skill_id_does_not_break_the_whole_read():
    """One unknown id used to take the entire state screen down with it."""
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = {"skills": {
            "retired_skill": {"state": "active", "level": 2},
            "qi_sense": {"state": "active", "level": 2},
        }}
    body = client.get("/inner-skills/p1").json()
    assert "qi_sense" in body["skills"]
    assert "retired_skill" not in body["skills"]


def test_an_unreadable_timer_reports_the_full_wait_not_zero():
    """A corrupt clock must never read as "finished"."""
    _, store, _, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("c"))
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["skills"]["qi_sense"]["started_at"] = "not-a-number"
    row = client.get("/inner-skills/p1").json()["skills"]["qi_sense"]
    assert row["remaining_seconds"] == isk.SKILLS["qi_sense"].learn_seconds


def test_an_unreadable_timer_does_not_settle_the_skill():
    _, store, _, client = _one()
    client.post("/inner-skills/comprehend",
                json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("c"))
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["skills"]["qi_sense"]["started_at"] = "not-a-number"
    body = client.post("/inner-skills/claim",
                       json={"player_id": "p1", "skill_id": "qi_sense"}).json()
    assert body["skills"]["qi_sense"]["state"] == "comprehending"
    assert body["es_points"] == 0, "a corrupt clock paid out ES"
    refused = client.post("/inner-skills/tupo",
                          json={"player_id": "p1", "skill_id": "qi_sense"}, headers=_idem("t"))
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Comprehension still in progress"


def test_the_published_constants_the_client_relies_on():
    """Frozen because ``es_max`` on the wire is compared against these."""
    assert isk.MAX_ES_POINTS == 6
    assert isk.RECEIPT_LIMIT == 50
    assert isk.UNLEARNED == "unlearned"
    assert sorted(isk.SKILLS) == ["body_refinement", "qi_sense", "spirit_sense"]
    assert set(isk.empty_state()) == {"version", "es_points", "skills", "receipts", "receipt_order"}


def test_the_receipt_cap_is_not_reachable_by_one_player_today():
    """Why no test drives the eviction loop: the catalog cannot fill it.

    A player can hold at most one receipt per action per skill, so the bound is
    ``2 * len(SKILLS)``. The eviction code in ``_append_receipt`` is therefore
    unreachable through HTTP today; this test documents that rather than
    pretending to cover it.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    for skill_id in isk.SKILLS:
        _comprehend(client, store, "p1", skill_id, "c-" + skill_id)
        client.post("/inner-skills/tupo", json={"player_id": "p1", "skill_id": skill_id},
                    headers=_idem("t-" + skill_id))
    with store.transaction("p1"):
        receipts = store["p1"]["inner_skills"]["receipts"]
    assert len(receipts) == 2 * len(isk.SKILLS)
    assert len(receipts) < isk.RECEIPT_LIMIT, (
        "the catalog grew enough to reach the cap; add a real eviction test"
    )


# ==========================================================================
# 14. Request-shape edges a real client walks into
#
# Measured, not read off the route table. Section 12 covers a MISSING player;
# this one covers the shapes around it.
# ==========================================================================

def test_the_read_route_swallows_the_three_action_paths():
    """A wrong-method probe on an action path answers 404, not 405.

    ``/inner-skills/{player_id}`` is registered BEFORE the flat action paths,
    so its path parameter matches the literal word ``comprehend`` and the read
    handler runs with that as a player id. A client probing for a route's
    existence with the wrong method is therefore told the route does not
    exist, when it does. Measured and pinned so a future reordering of the two
    registrations shows up here.
    """
    _, _, _, client = _one()
    for path in ACTION_ROUTES:
        reply = client.get(path)
        assert reply.status_code == 404, (path, reply.status_code)
        assert reply.json()["detail"] == "Player not found", path


def test_the_wrong_method_on_the_read_route_is_a_405_with_its_allow_header():
    """The other direction is a clean 405, and it names the one method."""
    _, _, _, client = _one()
    reply = client.post("/inner-skills/p1", json={})
    assert reply.status_code == 405
    assert reply.headers.get("allow") == "GET", reply.headers.get("allow")


def test_posting_to_an_action_path_reaches_that_handler_not_the_read_route():
    """The proof that the 404 above is routing, not absence.

    POST is not a method the read route answers, so FastAPI moves on to the
    next matching route -- the action handler -- and its body validator runs.
    A 422 naming a body field can only come from the action route.
    """
    _, _, _, client = _one()
    reply = client.post("/inner-skills/comprehend", json={})
    assert reply.status_code == 422, reply.text
    assert {e["loc"][-1] for e in reply.json()["detail"]} == {"player_id", "skill_id"}


@pytest.mark.parametrize("path", ACTION_ROUTES)
@pytest.mark.parametrize("bad", [123, None, ["x"], {"a": 1}, True])
def test_a_non_string_identifier_is_refused_rather_than_coerced(path, bad):
    """A number must not become the player id ``123`` by accident.

    ``player_id``/``skill_id`` are declared ``str`` and pydantic v2 will not
    quietly coerce a bool, a list or an object into one. This matters because
    ``True`` is an ``int`` in Python: a route that called ``str(True)`` would
    look up a player literally named ``"True"``.
    """
    _, _, _, client = _one()
    reply = client.post(path, json={"player_id": "p1", "skill_id": bad})
    assert reply.status_code == 422, (path, bad, reply.status_code)


@pytest.mark.parametrize("path", ACTION_ROUTES)
def test_a_body_that_is_not_an_object_is_refused_before_any_lookup(path):
    """A raw list or bare string answers 422, so no handler ever runs."""
    _, _, inventory, client = _one()
    for body in (["p1", "qi_sense"], "qi_sense", 42):
        reply = client.post(path, json=body)
        assert reply.status_code == 422, (path, body, reply.status_code)
    assert inventory.charge_count() == 0


def test_a_body_that_is_not_json_at_all_is_refused():
    _, _, _, client = _one()
    reply = client.post("/inner-skills/comprehend", content="not json at all",
                        headers={"Content-Type": "application/json", **_idem("nj")})
    assert reply.status_code == 422, reply.text


def test_identifiers_are_neither_trimmed_nor_case_folded():
    """Exact match only -- a client must not send sloppy casing and hope.

    Every skill id in the catalog is lower snake case, so ``"QI_SENSE"`` and
    ``" qi_sense"`` are different ids and must be refused as unknown (404),
    never quietly resolved to the real one.
    """
    _, _, _, client = _one()
    for sloppy in ("QI_SENSE", "Qi_Sense", " qi_sense", "qi_sense "):
        reply = client.post("/inner-skills/claim",
                            json={"player_id": "p1", "skill_id": sloppy})
        assert reply.status_code == 404, (sloppy, reply.status_code)
        assert reply.json()["detail"] == "Inner skill not found", sloppy
    for sloppy in ("p1 ", " P1", "P1"):
        assert client.get("/inner-skills/%s" % sloppy).status_code == 404, sloppy


def test_a_trailing_slash_still_resolves_the_read_route():
    """Measured: FastAPI's redirect lands on the same 200 body.

    Worth pinning because a client that appends a slash would otherwise be
    the only caller seeing a redirect status its parser was never written for.
    """
    _, _, _, client = _one()
    reply = client.get("/inner-skills/p1/")
    assert reply.status_code == 200, reply.status_code
    assert set(reply.json()) == STATE_KEYS


def test_an_extra_path_segment_under_the_read_route_is_a_404():
    _, _, _, client = _one()
    assert client.get("/inner-skills/p1/extra").status_code == 404


# ==========================================================================
# 15. The double-click race, observed through the API
#
# What is pinned is the OBSERVABLE outcome -- one payment across concurrent
# callers -- not the presence of a lock. A future backend that serialises these
# routes somewhere other than the transaction keeps these tests green, which is
# the point: the client-facing promise is what is under test.
# ==========================================================================

def _race(fn, workers: int) -> list:
    """Run ``fn`` on ``workers`` threads at once and collect every status."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, i) for i in range(workers)]
        return [f.result() for f in futures]


def test_parallel_comprehends_with_distinct_keys_charge_once():
    """Four clients, four DIFFERENT keys, one player, one skill.

    Distinct keys is the harder case: the idempotency receipt cannot dedupe
    these, so only the stored skill state can. Exactly one 200 and one charge.
    """
    _, _, inventory, client = _one()

    def attempt(i):
        return client.post("/inner-skills/comprehend",
                           json={"player_id": "p1", "skill_id": "qi_sense"},
                           headers=_idem("race-%d" % i)).status_code

    codes = _race(attempt, 6)
    assert codes.count(200) == 1, codes
    assert codes.count(409) == len(codes) - 1, codes
    assert inventory.charge_count() == 1, inventory.charges


def test_parallel_tupo_calls_with_distinct_keys_charge_once():
    """The same race on the second paid action."""
    _, store, inventory, client = _one("p1", "foundation_establishment")
    _comprehend(client, store, "p1", "qi_sense", "c")
    charged_after_comprehension = inventory.charge_count()

    def attempt(i):
        return client.post("/inner-skills/tupo",
                           json={"player_id": "p1", "skill_id": "qi_sense"},
                           headers=_idem("race-%d" % i)).status_code

    codes = _race(attempt, 6)
    assert codes.count(200) == 1, codes
    assert inventory.charge_count() == charged_after_comprehension + 1, (
        "the Tupo race charged %d times"
        % (inventory.charge_count() - charged_after_comprehension)
    )


def test_parallel_claims_of_one_finished_skill_pay_exactly_one_point():
    """The ES double-pay race. Ten simultaneous claims, one point.

    ``claim`` has no idempotency key to lean on -- ten callers legitimately
    send ten different (absent) keys -- so the tier change has to be visible
    to the second caller or all ten credit the pool.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    _comprehend(client, store, "p1", "qi_sense", "c")

    def attempt(_i):
        return client.post("/inner-skills/claim",
                           json={"player_id": "p1", "skill_id": "qi_sense"}).status_code

    codes = _race(attempt, 10)
    assert set(codes) == {200}, codes
    body = client.get("/inner-skills/p1").json()
    assert body["skills"]["qi_sense"]["state"] == "comprehend_complete"
    assert body["es_points"] == 1, (
        "ten concurrent claims paid %s points" % body["es_points"]
    )


def test_parallel_claims_across_two_players_do_not_mix_their_pools():
    """Concurrency in one player must not move another player's counter."""
    _, store, _, client = _app(_player("p1", "foundation_establishment"),
                               _player("p2", "foundation_establishment"))
    for pid in ("p1", "p2"):
        _comprehend(client, store, pid, "qi_sense", "c")

    def attempt(pid):
        return client.post("/inner-skills/claim",
                           json={"player_id": pid, "skill_id": "qi_sense"}).status_code

    _race(lambda i: attempt("p1" if i % 2 else "p2"), 8)
    for pid in ("p1", "p2"):
        assert client.get("/inner-skills/%s" % pid).json()["es_points"] == 1, pid


# ==========================================================================
# 16. FIXED DEFECT: a non-numeric stored pool used to lock the player out
#
# Both halves of this section are now ordinary behaviour. Read them as a pair:
# the first says a corrupt pool settles normally, and the second says a failed
# settlement costs the player nothing. They were written when both were false.
# ==========================================================================

CORRUPT_POOLS = ["corrupt", None, [], {}]


@pytest.mark.parametrize("stored", CORRUPT_POOLS)
def test_a_corrupt_stored_pool_settles_the_comprehension_the_player_paid_for(stored):
    """FIXED DEFECT, now pinned as ordinary behaviour.

    This used to answer 500 on both settlement routes, forever. ``state_view``
    guarded its read with ``int(...)`` in a try/except and published a clean 0,
    but ``_credit_es`` -- the only writer of the pool -- called
    ``int(state.get("es_points", 0))`` with NO guard, so the SAME row read fine
    and then crashed. The severity was the rollback, not the status: the tier
    change happens inside ``players_db.transaction(player_id)``, the exception
    unwound it, and since only those two crashing routes could rewrite the
    pool, one corrupt saved value locked a player out of settling a
    comprehension they had paid for, with no API path out.

    Both paths now share one conversion, so they cannot disagree about what a
    stored pool means. What is asserted is the OUTCOME the player sees: the
    skill advances, and the pool lands on the point the transition earned
    (1 from the comprehension) rather than on an error.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["es_points"] = stored

    assert client.get("/inner-skills/p1").status_code == 200
    settled = client.post("/inner-skills/claim",
                         json={"player_id": "p1", "skill_id": skill_id})
    assert settled.status_code == 200, settled.text
    row = settled.json()["skills"][skill_id]
    assert row["state"] == "comprehend_complete", (stored, row)
    assert row["level"] == 1, (stored, row)
    assert settled.json()["es_points"] == 1, (
        "a corrupt pool of %r settled to %s, not the 1 point earned"
        % (stored, settled.json()["es_points"])
    )


@pytest.mark.parametrize("stored", CORRUPT_POOLS)
def test_a_corrupt_stored_pool_settles_the_tupo_the_player_paid_for(stored):
    """The other settlement route, and the one that charges.

    ``tupo`` is the route a player reaches with the clock already run out, so
    it is the one that most often finds a row an older build wrote. It answers
    200 and takes its materials exactly once.
    """
    _, store, inventory, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["es_points"] = stored
    charged_before = inventory.charge_count()

    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id}, headers=_idem("t"))
    assert reply.status_code == 200, (stored, reply.text)
    assert inventory.charge_count() == charged_before + 1, (
        "a corrupt pool charged %d times" % (inventory.charge_count() - charged_before)
    )
    assert client.get("/inner-skills/p1").json()["skills"][skill_id]["state"] == "tupo"


def test_the_read_and_the_write_path_agree_on_every_corrupt_pool():
    """The defect was a DISAGREEMENT between two halves of one module.

    ``state_view`` answered 0 for a pool that ``_credit_es`` could not read.
    What is pinned now is the relation, not two literals: for every value the
    read publishes, the write must be able to credit from it. A future guard
    added to one path only breaks this.
    """
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = isk.empty_state()
    for stored in CORRUPT_POOLS + ["3", 3, 3.7, True, 0, -99, isk.MAX_ES_POINTS + 50]:
        with store.transaction("p1"):
            store["p1"]["inner_skills"]["es_points"] = stored
        published = client.get("/inner-skills/p1").json()["es_points"]
        assert 0 <= published <= isk.MAX_ES_POINTS, (stored, published)
        # The write path must accept whatever the read path just published.
        with store.transaction("p1"):
            store["p1"]["inner_skills"]["es_points"] = published
            isk._credit_es(store["p1"]["inner_skills"], 1)
            after = store["p1"]["inner_skills"]["es_points"]
        assert after == min(isk.MAX_ES_POINTS, published + 1), (stored, published, after)


def test_a_pool_value_that_converts_settles_normally():
    """The control for the pin above: a convertible pool is not a defect.

    ``"3"`` is a string, ``3.7`` truncates and ``True`` is an ``int`` in
    Python, so all three reach ``_credit_es`` fine. This is what makes the
    failing cases a real gap rather than an unsupported input type.
    """
    for stored, expected in (("3", 4), (3, 4), (3.7, 4), (True, 2)):
        _, store, _, client = _one("p1", "foundation_establishment")
        skill_id = "qi_sense"
        _comprehend(client, store, "p1", skill_id, "c")
        with store.transaction("p1"):
            store["p1"]["inner_skills"]["es_points"] = stored
        reply = client.post("/inner-skills/claim",
                            json={"player_id": "p1", "skill_id": skill_id})
        assert reply.status_code == 200, (stored, reply.status_code)
        assert client.get("/inner-skills/p1").json()["es_points"] == expected, (
            "a convertible pool of %r settled to %s"
            % (stored, client.get("/inner-skills/p1").json()["es_points"])
        )


def test_a_refused_tupo_charge_costs_the_player_nothing():
    """FIXED DEFECT: the charge runs LAST, so a refusal strands nothing.

    ``begin_tupo`` used to charge at the top of its branch, so anything that
    raised after it -- the pool write, the receipt -- took the materials and
    then unwound the tier change, leaving the player short of items with no
    refund path in this module.

    The order is now: write the skill row, credit the pool, THEN charge. So a
    refusal can no longer be the cause of a stranded charge -- the only step
    left that can fail after the charge is the receipt write, and the whole
    unit is one transaction.

    What is pinned is the OUTCOME on a refusal: nothing was charged, and the
    skill is untouched, so the player can retry. That is the property the old
    ordering could not offer.

    The inventory refuses only for the TUPO: the comprehension is bought first
    with a working store, then the same store starts refusing. That way the
    skill genuinely exists and the test measures the refused settlement rather
    than a player who never got started.

    ``granted`` is the log that matters here, not ``charges``: the route DID
    ask the store for the materials, and the store said no. Asserting on
    ``charges`` would fail on a correct refusal.
    """
    _, store, inventory, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    inventory.refuse = True
    granted_before = inventory.granted_count()

    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id}, headers=_idem("t"))
    assert reply.status_code == 409, reply.text
    assert inventory.granted_count() == granted_before, (
        "a refused settlement still took materials: %s" % (inventory.granted,)
    )
    with store.transaction("p1"):
        assert store["p1"]["inner_skills"]["skills"][skill_id]["state"] == "comprehending", (
            "a refused settlement left the skill half-transitioned"
        )


def test_the_tupo_charge_runs_after_the_pool_write_not_before_it():
    """The ORDER, measured at the moment the charge is taken.

    The reorder is not observable from any reply: by the time ``begin_tupo``
    answers, the pool has been written either way. So the inventory probe reads
    the STORED pool at the instant ``consume_items`` is called.

    With the charge running LAST, the pool at that moment is already the
    repaired, credited number. With it running first -- the old order -- the
    probe would see the raw stored value, the very thing the next line used to
    crash on. That is the whole defect: materials left the player before the
    step that could still fail.
    """
    skill_id = "qi_sense"
    probe = OrderProbeInventory("p1")
    _, store, probe, client = _app(_player("p1", "foundation_establishment"), inventory=probe)
    probe.bind(store)

    _comprehend(client, store, "p1", skill_id, "c")
    # Seed the corrupt pool AFTER the comprehension, so only the Tupo charge is
    # measured by the probe and not the comprehension's.
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["es_points"] = "corrupt"
    probe.pool_at_charge = "not-probed-yet"

    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id}, headers=_idem("t"))
    assert reply.status_code == 200, reply.text
    assert probe.pool_at_charge == 1, (
        "the charge ran before the pool write: the stored pool was still %r when "
        "materials were taken" % (probe.pool_at_charge,)
    )


def test_a_corrupt_pool_does_not_strand_a_charge_it_can_no_longer_fail_on():
    """The other half of the reorder, measured rather than asserted.

    This is the exact sequence that used to cost the player both: a corrupt
    pool plus a Tupo. The pool write used to raise AFTER the charge was taken.
    Now the pool is repaired first, the write lands, and the charge is taken
    for a settlement that actually happened -- one charge, one transition,
    and the player keeps their skill.
    """
    _, store, inventory, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["es_points"] = "corrupt"
    charged_before = inventory.charge_count()

    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id}, headers=_idem("g"))
    assert reply.status_code == 200, reply.text
    # Exactly one charge for a settlement that succeeded -- not zero (the
    # player bought it) and not two.
    assert inventory.charge_count() == charged_before + 1, inventory.charges
    with store.transaction("p1"):
        row = store["p1"]["inner_skills"]["skills"][skill_id]
        pool = store["p1"]["inner_skills"]["es_points"]
    assert row["state"] == "tupo", row
    assert pool == 1, "the settlement earned a point and the corrupt pool swallowed it: %r" % (pool,)


# ==========================================================================
# 17. FORCED WRITE FAILURE: the charge must not outlive the write it paid for
#
# Section 16 pinned the fix by REASONING about order. This section forces the
# write to fail and measures what the player is left with. That is the only
# way to see a lost charge: a charge that strands produces no error the client
# can notice, it just quietly takes materials for a skill the player never got.
# ==========================================================================


class FailingWriteInventory(RecordingInventory):
    """An inventory store that charges correctly and refuses to refund.

    Pairs with ``_refund``'s bare ``except Exception: pass``: the module
    promises a best-effort refund so the ORIGINAL failure reaches the client.
    A store that raises inside the refund must not replace that original error.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail_refund = True


def _primed_for_tupo(pid: str = "p1", realm: str = "foundation_establishment",
                     inventory="default"):
    """A player who has paid for, and completed, one comprehension.

    Returns the app/store/inventory/client plus the skill id. The Tupo charge
    is the next thing this player does, so a failure after it is a charge
    taken for work that did not land.
    """
    app, store, inv, client = _app(_player(pid, realm), inventory=inventory)
    skill_id = "qi_sense"
    _comprehend(client, store, pid, skill_id, "c")
    return app, store, inv, client, skill_id


def _seed_pool(store, pid: str, value) -> None:
    with store.transaction(pid):
        store[pid]["inner_skills"]["es_points"] = value


def _stored_skill(store, pid: str, skill_id: str) -> dict:
    with store.transaction(pid):
        return dict(store[pid]["inner_skills"]["skills"][skill_id])


def test_a_failed_pool_write_does_not_leave_the_tupo_charge_standing(monkeypatch):
    """The exact defect, forced: break the pool write and check the charge.

    ``_credit_es`` is the ONLY writer of the ES pool and it is called between
    the skill-row write and the charge. Making it raise is the strongest
    version of "the pool write failed", because it is the step whose failure
    the original ordering could not survive -- the charge was already taken.

    What is asserted is the player's ledger, not the HTTP status: a 500 is
    honest and fine. The defect would be materials gone for a skill the player
    does not have. ``net_charged`` is grants MINUS refunds, so a refund that
    fires and a charge that never happened both read as zero -- and only a
    stranded charge reads as positive.
    """
    _, store, inventory, client, skill_id = _primed_for_tupo()
    _seed_pool(store, "p1", "corrupt")
    before = inventory.net_charged("p1")

    def pool_write_fails(state, amount):
        raise RuntimeError("pool write failed")

    monkeypatch.setattr(isk, "_credit_es", pool_write_fails)
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id},
                        headers=_idem("t"))
    monkeypatch.undo()

    assert reply.status_code == 500, reply.text
    assert inventory.net_charged("p1") == before, (
        "the player lost %d material(s) for a settlement that never landed: "
        "charges=%s refunds=%s" % (inventory.net_charged("p1") - before,
                                   inventory.granted, inventory.refunds)
    )
    # The player-row rollback must also have discarded the skill change, so a
    # retry is possible instead of the skill being stranded half-transitioned.
    assert _stored_skill(store, "p1", skill_id)["state"] == "comprehending"


def test_a_failed_receipt_write_refunds_the_tupo_charge(monkeypatch):
    """The sibling write that CAN still fail after the charge is taken.

    ``begin_tupo`` charges, then writes the receipt. That receipt write is
    inside ``_atomic_charge``, whose whole job is to give the materials back
    on a raise. This test deletes the module's answer -- that the refund
    happens -- and asserts the player is made whole.

    The control is the arm BELOW it: the same injected failure with the
    refund itself broken must still surface the ORIGINAL error, which is the
    best-effort contract stated in ``_refund``'s docstring.
    """
    _, store, inventory, client, skill_id = _primed_for_tupo()
    _seed_pool(store, "p1", "corrupt")
    before = inventory.net_charged("p1")

    def receipt_write_fails(*a, **k):
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(isk, "_append_receipt", receipt_write_fails)
    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id},
                        headers=_idem("t"))
    monkeypatch.undo()

    assert reply.status_code == 500, reply.text
    assert inventory.net_charged("p1") == before, (
        "the receipt write raised AFTER the charge and the materials were "
        "never returned: charges=%s refunds=%s"
        % (inventory.granted, inventory.refunds)
    )
    assert _stored_skill(store, "p1", skill_id)["state"] == "comprehending"


def test_a_broken_refund_store_does_not_replace_the_original_error():
    """CONTROL: a refund that itself fails must not become the client's error.

    Without this arm the arm above could pass for the wrong reason -- a
    ``_refund`` that silently did nothing at all would also leave
    ``net_charged`` positive, but only if the test asserted nothing. Here the
    store is broken on purpose: the charge is still stranded, and the module's
    choice is to report the failure that actually caused it rather than the
    refund's own.
    """
    inv = FailingWriteInventory()
    _, store, broken, client, skill_id = _primed_for_tupo(inventory=inv)
    assert broken is inv

    def receipt_write_fails(*a, **k):
        raise RuntimeError("receipt write failed")

    original = isk._append_receipt
    isk._append_receipt = receipt_write_fails
    try:
        reply = client.post("/inner-skills/tupo",
                            json={"player_id": "p1", "skill_id": skill_id},
                            headers=_idem("t"))
    finally:
        isk._append_receipt = original

    assert reply.status_code == 500, reply.text
    assert "refund store unavailable" not in reply.text, (
        "the refund's own failure replaced the real error: %s" % reply.text
    )
    # Best-effort means best-effort: this store cannot return the materials,
    # and the module documents that it will not try to hide it. The charge
    # being stranded HERE is the store's fault, not the route's -- which is
    # exactly why the arm above is needed to prove the normal path refunds.
    assert inv.refunds == [], "a store that fails every refund cannot refund"
    assert inv.refund_failures, "the refund was never even attempted"


@pytest.mark.parametrize("stored", [float("inf"), float("-inf")])
def test_an_infinite_stored_pool_settles_instead_of_locking_the_player_out(stored):
    """REGRESSION: the numeric hole the string-only test could not reach.

    ``CORRUPT_POOLS`` covers ``"corrupt"``, ``None``, ``[]`` and ``{}`` -- all
    of which raise ``TypeError`` or ``ValueError`` inside ``int()``. An
    infinite float raises ``OverflowError``, which is NOT a ``ValueError``
    subclass, so it walked straight through the two-name except tuple.

    That reproduced the permanent lockout this section was built to end, one
    layer up: ``_pool_value`` is called by BOTH the read and the write path,
    so one stored ``inf`` answered 500 on the state read, on ``claim`` AND on
    ``tupo`` -- with no API path out, because the pool is only ever written by
    the two routes that crashed on it.

    Infinity is REACHABLE, not hypothetical: Python's ``json`` module
    round-trips it as a bare ``Infinity`` literal, and ``float("1e400")``
    parses to it. These values are asserted over HTTP, not by calling the
    helper, so a future refactor that moves the read and the write apart again
    is caught here.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    _seed_pool(store, "p1", stored)

    read = client.get("/inner-skills/p1")
    assert read.status_code == 200, (
        "the state read alone must survive an infinite pool: %s" % read.text
    )
    assert 0 <= read.json()["es_points"] <= isk.MAX_ES_POINTS, read.json()

    settled = client.post("/inner-skills/claim",
                          json={"player_id": "p1", "skill_id": skill_id})
    assert settled.status_code == 200, (
        "an infinite pool locked the player out of settling: %s" % settled.text
    )
    assert settled.json()["skills"][skill_id]["state"] == "comprehend_complete"
    assert settled.json()["es_points"] == 1, (
        "a pool of %r settled to %s, not the 1 point earned"
        % (stored, settled.json()["es_points"])
    )


def test_an_infinite_pool_survives_both_paid_settlement_routes():
    """The other paid route, and the one that charges.

    ``claim`` above proves the read and one write. ``tupo`` is the route that
    takes materials, so it is the one where an unreadable pool costs the
    player something. Exactly one charge, and the skill advances.
    """
    _, store, inventory, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    _seed_pool(store, "p1", float("inf"))
    charged_before = inventory.charge_count()

    reply = client.post("/inner-skills/tupo",
                        json={"player_id": "p1", "skill_id": skill_id},
                        headers=_idem("t"))
    assert reply.status_code == 200, reply.text
    assert inventory.charge_count() == charged_before + 1, inventory.charges
    assert _stored_skill(store, "p1", skill_id)["state"] == "tupo"


def test_the_read_and_the_write_path_agree_on_an_infinite_pool():
    """The relation, not two literals: whatever the read publishes must credit.

    This is the same contract section 16 pins for the string cases, extended to
    the values that raise a different exception. A guard added to only one of
    the two paths breaks this, which is the whole defect class.
    """
    _, store, _, client = _one()
    with store.transaction("p1"):
        store["p1"]["inner_skills"] = isk.empty_state()
    for stored in (float("inf"), float("-inf"), float("nan"), float("1e400")):
        _seed_pool(store, "p1", stored)
        read = client.get("/inner-skills/p1")
        assert read.status_code == 200, (stored, read.text)
        published = read.json()["es_points"]
        assert 0 <= published <= isk.MAX_ES_POINTS, (stored, published)
        with store.transaction("p1"):
            store["p1"]["inner_skills"]["es_points"] = published
            isk._credit_es(store["p1"]["inner_skills"], 1)
            after = store["p1"]["inner_skills"]["es_points"]
        assert after == min(isk.MAX_ES_POINTS, published + 1), (stored, published, after)


def test_a_pool_that_cannot_be_read_is_never_stranded_charge_evidence():
    """No sibling charge path writes the pool before its own charge.

    ``claim`` credits the pool twice and charges NOTHING, so it cannot strand a
    charge. Both paid routes wrap their charge in ``_atomic_charge``. This
    asserts that structurally rather than per-route: every function that calls
    ``_charge`` must also call ``_atomic_charge``, so a future route cannot
    reintroduce a bare ``_charge`` ahead of its writes.
    """
    import inspect
    # _charge is the primitive that actually spends; _atomic_charge is the
    # wrapper that gives it back. BOTH are exempt by name -- exempting only
    # the wrapper made this test fail on the primitive it was written to
    # protect, which is how a structural test becomes a false alarm.
    exempt = {"_charge", "_atomic_charge"}
    charging, compliant = [], []
    for name, fn in vars(isk).items():
        if not (inspect.isfunction(fn) and getattr(fn, "__module__", "") == isk.__name__):
            continue
        body = inspect.getsource(fn)
        if "_charge(" not in body:
            continue
        # A compliant route mentions _atomic_charge only in the `with` line --
        # a bare _charge( call outside the wrapper is what this test hunts.
        # Counting the wrapper's own definition line as compliance is what made
        # an earlier version of this test inspect nothing and pass vacuously.
        uses_wrapper = "with _atomic_charge(" in body
        if name in exempt:
            continue
        (compliant if uses_wrapper else charging).append(name)
        assert uses_wrapper, (
            "%s calls _charge outside _atomic_charge -- a bare charge takes the "
            "materials with no refund if a later write raises" % name
        )
    # Anti-vacuity: the scan is worth nothing unless it actually inspected the
    # two paid routes. A renamed helper would empty the lists and the loop
    # above would pass without looking at anything.
    assert "begin_tupo" in compliant and "begin_comprehension" in compliant, (
        "the structural scan did not inspect both paid routes, so it would "
        "pass vacuously: compliant=%r noncompliant=%r" % (compliant, charging)
    )
    assert not charging, "a route charges outside the refunding wrapper: %r" % (charging,)


# --------------------------------------------------------------------------
# An infinite TIMER, not an infinite pool
# --------------------------------------------------------------------------
# _pool_value already names OverflowError. _started_at did not, and it is the
# other half of the same defect class: it converts a stored number with the
# same two-name except tuple. So one stored Infinity in "started_at" 500'd the
# state read, claim AND tupo -- and the timer is written by the same routes
# that crash on it, so there was no API way out. Identical lockout, one layer
# over, and it survived the pool fix because nothing forced the two converters
# to be read together.

HUGE_INT = 10 ** 400   # a bare integer literal in a save file -- json.loads
                     # ("1" + "0"*400) hands back exactly this int, and
                     # float() of it raises OverflowError, not ValueError.


@pytest.mark.parametrize("stored", [
    HUGE_INT,                 # OverflowError out of float() -- the escaped type
    "1e400",                  # a string literal: float() gives inf, no raise
    "Infinity",               # the bare token Python's json reader accepts back
    float("inf"),             # already an inf float
])
def test_an_infinite_stored_timer_never_500s_a_route(stored):
    """REGRESSION: the timer converter, which the pool tests could not reach.

    The infinity tests above seed ``es_points``. This seeds ``started_at``,
    which goes through ``float()`` in ``_started_at`` -- a DIFFERENT converter
    with the SAME two-name except tuple, so it kept the hole after the pool
    was fixed.

    Two distinct failure modes hide in "infinite", and a test that only seeded
    one would pass while the other still 500'd:

      * ``HUGE_INT`` makes ``float()`` RAISE OverflowError. That is the
        one that escapes a ``(TypeError, ValueError)`` tuple, and it is what
        this test exists for.
      * ``"1e400"`` and ``float("inf")`` do NOT raise -- ``float()`` returns
        ``inf``, and ``inf > 0`` passes the ``started`` guard. So those reach
        the arithmetic as ``now - inf == -inf``, which underflows the elapsed
        wait to zero. That is safe by accident, not by design, and it is why
        this test asserts on the published remaining time rather than only on
        the status code.

    Asserted over HTTP on all three routes, so a refactor that splits the read
    from the writes is caught here rather than in production.
    """
    _, store, _, client = _one("p1", "foundation_establishment")
    skill_id = "qi_sense"
    _comprehend(client, store, "p1", skill_id, "c")
    with store.transaction("p1"):
        store["p1"]["inner_skills"]["skills"][skill_id]["started_at"] = stored

    read = client.get("/inner-skills/p1")
    assert read.status_code == 200, (
        "an infinite timer 500'd the state read: %s" % read.text
    )
    row = read.json()["skills"][skill_id]
    # The reply itself must be encodable. Reading the JSON is the assertion:
    # a raw inf in the row is rejected by the response encoder with a
    # ValueError, which is a 500 that no amount of fixing the converter's
    # except tuple can prevent -- the value has to stop being published raw.
    assert row["started_at"] is None, (
        "the stored clock %r was echoed into the reply instead of the parsed "
        "one" % (stored,)
    )
    # An unreadable clock means "wait the full time", never "already done".
    assert row["remaining_seconds"] == isk.SKILLS[skill_id].learn_seconds, (
        "an infinite timer of %r published %s seconds remaining, not the "
        "full wait" % (stored, row["remaining_seconds"])
    )

    claimed = client.post("/inner-skills/claim",
                          json={"player_id": "p1", "skill_id": skill_id})
    assert claimed.status_code == 200, (
        "an infinite timer locked the player out of settling: %s" % claimed.text
    )
    assert claimed.json()["skills"][skill_id]["state"] == "comprehending", (
        "a timer of %r settled the comprehension for free" % (stored,)
    )
    assert claimed.json()["es_points"] == 0, "a corrupt clock paid out ES"

    refused = client.post("/inner-skills/tupo",
                          json={"player_id": "p1", "skill_id": skill_id},
                          headers=_idem("t"))
    assert refused.status_code == 409, (
        "an infinite timer answered %s on tupo instead of refusing: %s"
        % (refused.status_code, refused.text)
    )


def test_the_timer_and_the_pool_reject_the_same_shapes():
    """Both converters, one contract: they must not disagree about a number.

    The pool fix and the timer fix were landed by two different passes and the
    pool one alone left this hole. Pinning them together means a third
    converter added later cannot quietly reintroduce a narrower except tuple.
    """
    import inspect

    def named(fn, exc):
        src = inspect.getsource(fn)
        return exc in src.split("except (", 1)[1].split(")", 1)[0]

    for fn, label in ((isk._pool_value, "_pool_value"), (isk._started_at, "_started_at")):
        assert named(fn, "OverflowError"), (
            "%s does not name OverflowError, so an infinite stored value "
            "escapes its guard as a 500" % label
        )
