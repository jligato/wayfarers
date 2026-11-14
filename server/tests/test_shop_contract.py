"""Contract tests for the SHOP surface: ``POST /shop/buy`` and ``GET /shop/products``.

Where the code actually lives (the brief said ``server/shop.py``)
---------------------------------------------------------------
There is no ``server/shop.py`` in this tree. The shop is not a module: it is two
route handlers in ``server/main.py`` (the buy handler at ``main.shop_buy``, the
price list at ``main.get_shop_products``) plus the price table ``SHOP_PRODUCTS``
in ``server/inventory.py``. So this file imports what exists and drives the two
routes over real HTTP. Nothing here is written against a filename the brief
named; every claim below was measured against the live app.

What this file is for
---------------------
A contract test pins the SHAPE of a request and a reply -- which fields exist,
which status code comes back, what an error body says -- because that is what a
client screen is written against. The rules (the stack ceiling, the refund on a
failed delivery, the replay key) are proved elsewhere; this file records what a
player can observe from outside.

What was MEASURED, not assumed
------------------------------
Every status code and every body shape below came from running the live app
under ``TestClient`` on 2026-10-04. Four things a reader would otherwise guess
wrong, and did:

* ``GET /shop/products`` is NOT public. Its docstring calls it "a public price
  list", but the ownership guard owns it and an anonymous call answers **401**.
  The docstring is stale; the guard is what runs.
* A cross-player buy is refused **403** ("player_id does not match the
  authenticated session"), not 404. The body names a *session*, so the check is
  against the token, not against whether the row exists.
* A quantity below one is **400**, not 422. The request model declares no lower
  bound on ``quantity``, so the guard is in the handler and a client cannot
  predict 422 from the schema.
* A stale ``client_version`` is refused with the wallet's OWN dict
  (``{"ok": false, "error": "stale wallet_version", ...}``) rather than the
  shop's usual ``error``-keyed refusal shape. Two different error vocabularies
  share one route.

Relations, not frozen numbers
-----------------------------
Prices, limits and the capacity ceiling can all move with a balance retune, so
the money assertions compare the RECEIPT against the WALLET'S OWN BALANCE
CHANGE, and the capacity assertions compare the refusal's numbers against the
module's own constant. A retune does not break these; a receipt that lies does.

Per-arm isolation
-----------------
Every test builds its own player. The ownership guard refuses a foreign
``player_id`` before the handler runs, so a shared player would make one arm's
refusal land in another arm's test.

Run
---
    cd G:/Projects/wayfarers-cycle
    tools/wc_pytest.sh server/tests/test_shop_contract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_DIR = _REPO_ROOT / "server"
for _path in (str(_REPO_ROOT), str(_SERVER_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from server import main as mainmod  # noqa: E402
from server.main import app, inventory_store, players_db, wallet_store  # noqa: E402
from server import inventory as inv  # noqa: E402
import wallet_dispatch as wd  # noqa: E402
from server.tests.wc_test_auth import Session, clear_signup_rate_limit  # noqa: E402

client = Session(TestClient(app))

BUY = "/shop/buy"
PRODUCTS = "/shop/products"

#: The exact keys of a successful purchase receipt. A subset assertion would let
#: a client break silently on a RENAME, which is the failure this file exists to
#: catch. Additive growth is allowed -- see ``test_the_receipt_may_gain_fields``.
RECEIPT_KEYS = {"ok", "product_id", "quantity", "requested", "spent", "granted"}

#: The exact keys of one ``GET /shop/products`` row in the ordinary case. The
#: capacity arm adds ``disabled`` and ``reason``; that is pinned separately.
ROW_KEYS = {"product_id", "name", "cost"}

#: MEASURED: the anonymous refusal body is a plain string, not the dict shape
#: the authenticated refusals use.
ANON_DETAIL = "invalid or missing session token"

#: MEASURED: one wording for the cross-player refusal, used by BOTH the buy body
#: and the products query. Pinned so a rename shows up here rather than in a
#: client's error banner.
MISMATCH_DETAIL = "player_id does not match the authenticated session"


#: MEASURED on 2026-10-04: how many summon tokens each token product delivers.
#: Nothing in ``SHOP_PRODUCTS`` records this -- the grant is a hard-coded branch
#: in the handler, so the reply is the only place the number exists. It is a
#: grant table, not a price: a price retune does not touch it.
TOKEN_GRANTS = {"starter_pack": 1, "summon_bundle_10": 10}


def _detail(r) -> object:
    return r.json().get("detail")


def _new_player(tag: str) -> str:
    """Create a player and sign the shared client in as them.

    ``clear_signup_rate_limit`` is required, not cosmetic: ``/player/create``
    refuses more than a handful of signups from one address inside its window,
    so a file that makes many players trips the limit and every later creation
    returns 429 instead of a player.
    """
    clear_signup_rate_limit()
    r = client.post("/player/create", json={"name": f"shop_{tag}", "spirit_root": "fire"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    client.sign_in(pid)
    return pid


def _other_player(tag: str) -> tuple:
    """A SECOND player, leaving the shared client signed in as them."""
    clear_signup_rate_limit()
    r = client.post("/player/create", json={"name": f"shopx_{tag}", "spirit_root": "water"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _funded(tag: str, *currencies: str, amount: int = 100000) -> str:
    """Create a player holding money, so a paid route can be measured green."""
    pid = _new_player(tag)
    for currency in currencies or ("fateum", "spirit_stone"):
        result = wd.earn(wallet_store, pid, currency, amount, f"shop-seed-{pid}-{currency}")
        assert result.get("ok") is True, f"funding {currency} failed: {result}"
    return pid


def _key(name: str) -> dict:
    return {"Idempotency-Key": name}


def _wallet_version(player_id: str) -> int:
    return wallet_store.get(player_id).version


def _balance(player_id: str, currency: str) -> int:
    return getattr(wallet_store.get(player_id).balance, currency, 0)


def _buy(pid: str, product_id: str, quantity: int = 1, key: str = "k1", version=None):
    """POST /shop/buy as ``pid``, with a fresh idempotency key by default.

    ``version`` defaults to the player's CURRENT wallet version because the
    wallet refuses a stale one; a caller that wants the stale path passes one.
    """
    body = {
        "player_id": pid,
        "product_id": product_id,
        "quantity": quantity,
        "client_version": _wallet_version(pid) if version is None else version,
    }
    return client.post(BUY, json=body, headers=_key(key))


# ------------------------------------------------------------------
# The routes exist, once each, under the names a client would call
# ------------------------------------------------------------------

class TestRouteSurface:
    """The premise: both paths are registered, exactly once, on the live app.

    A duplicate registration is not a cosmetic problem here. ``/shop/buy`` had
    two copies at one point, and they disagreed on an unknown product (400 vs
    404), so which copy answers depended on table order. Asserting the count is
    what stops a second copy from shipping silently.
    """

    def test_the_two_shop_paths_are_registered_exactly_once(self):
        rows = [
            (sorted(rt.methods), rt.path)
            for rt in app.routes
            if getattr(rt, "path", "").startswith("/shop")
        ]
        assert rows == [(["POST"], BUY), (["GET"], PRODUCTS)], sorted(rows)

    def test_a_buy_is_a_post_and_a_price_list_is_a_get(self):
        """MEASURED: the other verb answers 405, not 404.

        Both paths exist; asking with the wrong verb is a method problem, and a
        client that guesses the verb wrong should see that distinction rather
        than a "no such route" that reads like a typo.
        """
        pid = _funded("verbs")
        assert client.get(BUY).status_code == 405
        assert client.post(PRODUCTS).status_code == 405
        assert players_db[pid] is not None  # the fixture really made a player

    def test_the_buy_request_model_is_published_with_the_fields_a_client_sends(self):
        """The OpenAPI document is the contract a generated client is built from.

        MEASURED: ``player_id`` and ``product_id`` are REQUIRED;
        ``quantity`` defaults to 1 and ``client_version`` to 0. There is no
        declared bound on ``quantity`` -- the lower-bound guard is in the
        handler -- which is why the bad-quantity refusal is a 400 and not a 422.
        """
        schema = app.openapi()["components"]["schemas"]["InventoryBuyRequest"]
        assert set(schema["required"]) == {"player_id", "product_id"}, schema
        defaults = {name: prop.get("default")
                    for name, prop in schema["properties"].items()}
        assert defaults == {"player_id": None, "product_id": None,
                            "quantity": 1, "client_version": 0}, defaults


# ------------------------------------------------------------------
# Auth: who may reach the shop at all
# ------------------------------------------------------------------

class TestAuth:
    """Both routes need a session. MEASURED: anonymous is 401 with a string body.

    A separate, non-signed-in client is used rather than dropping the header,
    so the test observes the route's own refusal instead of a client-side
    header-building mistake.
    """

    def test_an_anonymous_price_list_read_is_refused(self):
        anon = TestClient(app)
        r = anon.get(PRODUCTS)
        assert r.status_code == 401, r.text
        assert _detail(r) == ANON_DETAIL, r.text

    def test_a_session_is_the_only_thing_that_makes_the_price_list_readable(self):
        """The 401 above, paired with the 200 the SAME request turns into.

        A refusal on its own cannot tell "the guard works" from "this route is
        broken and refuses everyone". Signing in on the same path is what makes
        the status code a statement about the caller rather than about the
        route. The docstring on the route calls this "a public price list"; the
        guard is what runs, and this is the test that says so.
        """
        anon = TestClient(app)
        pid = _funded("pair")

        refused = anon.get(PRODUCTS)
        assert refused.status_code == 401, refused.text
        assert _detail(refused) == ANON_DETAIL, refused.text

        # Same path, same helpers, one session's worth of difference.
        served = client.get(PRODUCTS)
        assert served.status_code == 200, served.text
        assert served.json()["products"], "a signed-in call got an empty price list"
        assert players_db[pid] is not None  # the fixture really made a player

    def test_naming_a_player_in_the_query_does_not_unlock_an_anonymous_price_list(self):
        """The optional ``player_id`` is a scope, not a credential.

        The guard answers before the handler ever reads the parameter, so
        handing over a real, existing player id buys nothing. This is the arm
        that would catch a future change treating a caller-supplied id as a way
        around the session -- the failure would be silent, because the reply
        would still look like a price list.
        """
        pid = _funded("anon_scope")
        anon = TestClient(app)
        for params in ({}, {"player_id": pid}, {"player_id": "no_such_player"}):
            r = anon.get(PRODUCTS, params=params)
            assert r.status_code == 401, (params, r.status_code, r.text)
            assert _detail(r) == ANON_DETAIL, (params, r.text)

    def test_an_anonymous_purchase_is_refused_and_nothing_is_charged(self):
        anon = TestClient(app)
        pid = _funded("anon_buy")  # a REAL player, so only auth is missing
        before = _balance(pid, "fateum")
        r = anon.post(BUY, json={"player_id": pid, "product_id": "starter_pack",
                                 "client_version": _wallet_version(pid)},
                      headers=_key("anon"))
        assert r.status_code == 401, r.text
        assert _detail(r) == ANON_DETAIL, r.text
        assert _balance(pid, "fateum") == before, "an anonymous call was charged"

    def test_a_forged_bearer_token_is_refused_the_same_way(self):
        """A bad token must not be a softer failure than a missing one."""
        pid = _funded("bad_tok")
        r = client.post(BUY, json={"player_id": pid, "product_id": "starter_pack",
                                   "client_version": _wallet_version(pid)},
                        headers={**_key("bad"), "Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401, r.text
        assert _detail(r) == ANON_DETAIL, r.text

    def test_a_purchase_naming_another_player_is_refused_403_and_moves_no_money(self):
        """MEASURED: 403, naming the SESSION -- not 404.

        The body claims an id the token does not own. Both players are real and
        both are funded, so the only thing wrong is the ownership, and the
        assertion that BOTH balances are untouched is what makes this an
        anti-theft test rather than a status-code test.
        """
        buyer = _funded("owner", "fateum")
        victim = _other_player("victim")
        wd.earn(wallet_store, victim, "fateum", 5000, f"shop-victim-{victim}")
        client.sign_in(buyer)
        buyer_before = _balance(buyer, "fateum")
        victim_before = _balance(victim, "fateum")

        r = _buy(victim, "starter_pack", key="cross")
        assert r.status_code == 403, r.text
        assert _detail(r) == MISMATCH_DETAIL, r.text
        assert _balance(buyer, "fateum") == buyer_before, "the buyer was charged"
        assert _balance(victim, "fateum") == victim_before, "the victim's wallet moved"

    def test_a_price_list_scoped_to_another_player_is_refused_403(self):
        """The optional ``player_id`` is scoped to the session, not free text.

        Without it the reply is a price list carrying no player data. With it,
        the caller may only scope it to itself -- so an unknown id is refused
        rather than quietly treated as "no player named".
        """
        pid = _funded("scope")
        stranger = _other_player("stranger")
        client.sign_in(pid)
        assert client.get(PRODUCTS, params={"player_id": stranger}).status_code == 403
        assert client.get(PRODUCTS, params={"player_id": "no_such_player"}).status_code == 403
        assert _detail(client.get(PRODUCTS, params={"player_id": stranger})) == MISMATCH_DETAIL


# ------------------------------------------------------------------
# GET /shop/products -- the price list a client renders
# ------------------------------------------------------------------

class TestPriceList:
    """What a shop screen draws before the player presses buy."""

    def test_the_reply_is_exactly_one_products_list(self):
        pid = _funded("list_shape")
        r = client.get(PRODUCTS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"products"}, sorted(body)
        assert isinstance(body["products"], list) and body["products"], body

    def test_every_row_carries_the_three_fields_a_row_needs_to_render(self):
        pid = _funded("row_keys")
        for row in client.get(PRODUCTS).json()["products"]:
            assert set(row) == ROW_KEYS, (row["product_id"], sorted(row))
            assert isinstance(row["name"], str) and row["name"], row
            assert isinstance(row["cost"], dict) and row["cost"], row

    # RESOLVED 2026-10-04: this row was advertised on the price list and priced
    # in SHOP_PRODUCTS, but the handler had no grant branch for it and it was not
    # an inventory item, so the till answered 400 ``unknown_product`` and
    # refunded. A refund is not a purchase -- the player still could not buy the
    # row they were shown. The handler now grants the pack's spirit stones
    # through ``wallet_earn`` (``SPIRIT_STONE_PACK_AMOUNT`` in server/main.py),
    # so the row buys for real. EMPTY on purpose: it was
    # ``{"spirit_stone_pack_small"}`` while the grant was missing, and the entry
    # was REMOVED rather than flipped so a future regression surfaces as a
    # failing sweep below instead of a marker nobody reads.
    KNOWN_UNBUYABLE_BUT_ADVERTISED: set = set()

    def test_the_once_unbuyable_row_is_still_advertised_at_the_table_price(self):
        """The ADVERTISED half, still pinned, plus the skip that used to cover it.

        A skip is only evidence if what it skips is real. This reads the row off
        the live price list and matches its cost against the table the till
        prices from, so the row cannot be quietly deleted. The skip list is
        asserted EMPTY, so a row that is quietly re-skipped would hide a
        regression this file exists to catch.
        """
        pid = _funded("gap_advertised", "fateum")
        product = "spirit_stone_pack_small"
        rows = {row["product_id"]: row
                for row in client.get(PRODUCTS).json()["products"]}
        assert product not in self.KNOWN_UNBUYABLE_BUT_ADVERTISED, (
            f"{product} is back in the skip list; the grant landed, so the row must "
            "be measured by the buyability sweep rather than skipped by it"
        )
        assert product in rows, f"{product} is no longer advertised"
        table = inv.SHOP_PRODUCTS[product]
        assert rows[product]["cost"] == {table["currency"]: table["price"]}, rows[product]
        # A name the client can draw, so the row is renderable and not a stub.
        assert isinstance(rows[product]["name"], str) and rows[product]["name"], rows[product]

    def test_the_once_unbuyable_row_is_now_bought_at_the_advertised_price(self):
        """The ADVERTISED-THEN-REFUSED pair, inverted: the till now delivers.

        This is the player-visible fix. The price list shows the row, the player
        buys it, and the till answers 200, charges exactly the advertised cost,
        and credits the stones the receipt names. The charge is compared against
        the table the till prices from and against the wallet's own balance
        change, so a price retune does not break it and a receipt that lies does.
        """
        pid = _funded("gap_bought", "fateum", "spirit_stone")
        product = "spirit_stone_pack_small"
        table = inv.SHOP_PRODUCTS[product]
        currency = table["currency"]

        advertised = client.get(PRODUCTS).json()["products"]
        assert product in {row["product_id"] for row in advertised}, (
            "the advertised half of this gap is gone"
        )
        before = _balance(pid, currency)
        stones_before = _balance(pid, "spirit_stone")
        r = _buy(pid, product, key="gap-bought")

        assert r.status_code == 200, (
            f"{product} is advertised at {currency}:{table['price']} but buying it "
            f"answered {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body["ok"] is True and body["product_id"] == product, body
        assert body["spent"] == {currency: table["price"]}, body
        # The payout is the server's own constant rather than a number copied
        # here, so a retune of the grant does not need this test edited.
        assert body["granted"] == {"spirit_stone": mainmod.SPIRIT_STONE_PACK_AMOUNT}, body
        assert _balance(pid, currency) - before == -table["price"], (
            "the receipt charged a price the wallet did not move by"
        )
        assert _balance(pid, "spirit_stone") - stones_before == mainmod.SPIRIT_STONE_PACK_AMOUNT, (
            "the receipt named spirit stones the wallet did not receive"
        )

    def test_every_advertised_row_is_priced_from_the_one_table_the_till_uses(self):
        """``GET /shop/products`` and ``/shop/buy`` must agree on price.

        The list is hand-written in the route; the till prices from
        ``SHOP_PRODUCTS``. Two sources means drift, and a row whose advertised
        price is not the charged price is a receipt the player cannot reconcile.
        """
        pid = _funded("list_price")
        for row in client.get(PRODUCTS).json()["products"]:
            product_id = row["product_id"]
            assert product_id in inv.SHOP_PRODUCTS, (
                f"{product_id} is advertised but absent from SHOP_PRODUCTS"
            )
            table = inv.SHOP_PRODUCTS[product_id]
            assert row["cost"] == {table["currency"]: table["price"]}, (
                f"{product_id} advertises {row['cost']} but the till charges "
                f"{table['currency']}:{table['price']}"
            )

    def test_every_advertised_row_is_actually_buyable_at_the_till(self):
        """An advertised row the till refuses is the "I paid for nothing" report.

        Skips only the one row measured as a known gap, and says so out loud, so
        a NEW mismatch fails here rather than being absorbed by the skip.
        """
        pid = _funded("list_buyable", "fateum", "spirit_stone")
        for row in client.get(PRODUCTS).json()["products"]:
            product_id = row["product_id"]
            if product_id in self.KNOWN_UNBUYABLE_BUT_ADVERTISED:
                continue
            r = _buy(pid, product_id, key=f"list-{product_id}")
            assert r.status_code == 200, (
                f"{product_id} is advertised at {row['cost']} but buying it "
                f"answered {r.status_code}: {r.text}"
            )

    def test_a_price_list_scoped_to_the_calling_player_matches_the_plain_list(self):
        """Scoping the query to yourself adds no rows and changes no prices.

        The parameter exists to mark one row unbuyable; when nothing is at the
        ceiling the reply is the same document, so a client that always sends
        the id needs no separate code path.
        """
        pid = _funded("scoped_same")
        plain = client.get(PRODUCTS).json()
        scoped = client.get(PRODUCTS, params={"player_id": pid}).json()
        assert scoped == plain, "scoping to yourself changed the price list"

    def test_the_expansion_scroll_is_marked_unbuyable_at_the_capacity_ceiling(self):
        """MEASURED: at the ceiling the row gains ``disabled`` and ``reason``.

        The server refuses this purchase up front, so the list must say so
        before the player pays. Capacity is stubbed rather than filled because
        the ceiling is a module constant that a retune can move -- and stubbing
        it proves the wiring without hard-coding today's number.
        """
        pid = _funded("ceiling")
        original = inventory_store.get_player_capacity
        inventory_store.get_player_capacity = lambda _pid: mainmod.MAX_INVENTORY_SLOTS
        try:
            rows = {row["product_id"]: row
                    for row in client.get(PRODUCTS, params={"player_id": pid}).json()["products"]}
            scroll = rows[mainmod.EXPANSION_SCROLL_ITEM]
            assert scroll.get("disabled") is True, scroll
            assert isinstance(scroll.get("reason"), str) and scroll["reason"], scroll
            marked = [pid_ for pid_, row in rows.items() if "disabled" in row]
            assert marked == [mainmod.EXPANSION_SCROLL_ITEM], marked
        finally:
            inventory_store.get_player_capacity = original


# ------------------------------------------------------------------
# POST /shop/buy -- the success receipt
# ------------------------------------------------------------------

class TestBuyReceipt:
    """A purchase that works, and a receipt that matches the wallet.

    The success keys are pinned EXACTLY. A client draws the receipt from this
    body, so a rename is a broken screen with nothing red; additive growth is
    allowed and has its own test below.
    """

    #: One of each grant KIND, so a grant-shape change in one branch cannot hide
    #: behind another branch still working. MEASURED: starter_pack grants a
    #: summon token, battle_pass grants the premium flag plus an experience
    #: result, and an inventory item grants an item row.
    SPECIAL_CASES = [
        ("starter_pack", "fateum"),
        ("battle_pass", "fateum"),
        ("summon_bundle_10", "fateum"),
    ]

    def test_an_inventory_purchase_answers_the_full_receipt_and_the_wallet_agrees(self):
        pid = _funded("receipt", "spirit_stone")
        product = "beast_food_basic"
        table = inv.SHOP_PRODUCTS[product]
        before = _balance(pid, table["currency"])

        r = _buy(pid, product, quantity=2, key="r1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == RECEIPT_KEYS, sorted(body)
        assert body["ok"] is True
        assert body["product_id"] == product
        assert body["quantity"] == body["requested"] == 2
        assert body["spent"] == {table["currency"]: table["price"] * 2}, body
        assert body["granted"]["item"] == {"item_id": product, "quantity": 2}, body

        charged = before - _balance(pid, table["currency"])
        assert charged == table["price"] * 2, (
            "the receipt says one price and the wallet took another"
        )
        assert inventory_store.get_item_count(pid, product) == 2

    @pytest.mark.parametrize("product_id,currency", SPECIAL_CASES)
    def test_each_special_product_grants_its_own_thing(self, product_id, currency):
        """Every non-inventory product delivers something different.

        These three are hard-coded grant branches, not a table lookup, so a
        merge that keeps two of them right and breaks the third is invisible to
        a single-product test. The grant key is what a client renders.
        """
        pid = _funded("special_" + product_id, currency)
        r = _buy(pid, product_id, key=f"sp-{product_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == RECEIPT_KEYS, sorted(body)
        assert body["spent"] == {currency: inv.SHOP_PRODUCTS[product_id]["price"]}, body
        granted = body["granted"]
        assert granted, f"{product_id} granted nothing: {body}"
        if product_id == "battle_pass":
            assert granted["battle_pass_premium"] is True, body
        else:
            # The two token packs are the same KIND of grant, so the grant KEY is
            # the contract and the count is the payload. The key is asserted as
            # the only key; the count is asserted against the measured grant
            # table below, because nothing in SHOP_PRODUCTS records how many
            # tokens a bundle contains -- the reply is the only place that
            # number exists, which is exactly why a client reads it from there.
            assert list(granted) == ["summon_token"], body
            assert granted["summon_token"] == TOKEN_GRANTS[product_id], body
            assert granted["summon_token"] >= 1, body

    def test_the_battle_pass_purchase_also_writes_the_flag_on_the_player(self):
        """The receipt claims premium; the player row must agree.

        A receipt that reports an unlock nobody received is worse than a
        refusal, because the player believes they own it.
        """
        pid = _funded("bp_flag", "fateum")
        assert players_db[pid].get("battle_pass_premium") in (False, None)
        r = _buy(pid, "battle_pass", key="bp-flag")
        assert r.status_code == 200, r.text
        assert r.json()["granted"]["battle_pass_premium"] is True
        assert players_db[pid].get("battle_pass_premium") is True

    def test_the_receipt_may_gain_fields_without_breaking_the_client(self):
        """Additive growth is allowed; a RENAME is not.

        This is the counterpart to the exact-key assertion: it states which
        direction of change is safe, so a future author adding a field knows the
        test will still pass while a client author knows it will not have to
        re-read the old one.
        """
        pid = _funded("additive", "spirit_stone")
        body = _buy(pid, "beast_food_basic", key="add").json()
        assert RECEIPT_KEYS <= set(body), "a documented field disappeared"

    def test_a_replay_returns_a_bare_ack_and_neither_charges_nor_delivers_twice(self):
        """MEASURED: the replay body is exactly ``{"ok", "_replayed"}``.

        No ``spent``, no ``granted``. So a client that renders a receipt from the
        reply must keep the FIRST reply or draw an empty receipt on a retry.
        Pinning the absence is what tells the client author that.
        """
        pid = _funded("replay", "spirit_stone")
        product = "beast_food_basic"
        currency = inv.SHOP_PRODUCTS[product]["currency"]
        assert _buy(pid, product, key="same").status_code == 200
        held_money = _balance(pid, currency)
        held_item = inventory_store.get_item_count(pid, product)

        replay = _buy(pid, product, key="same")
        assert replay.status_code == 200, replay.text
        assert set(replay.json()) == {"ok", "_replayed"}, sorted(replay.json())
        assert replay.json()["_replayed"] is True
        assert _balance(pid, currency) == held_money, "a replay charged twice"
        assert inventory_store.get_item_count(pid, product) == held_item, (
            "a replay delivered the goods twice"
        )

    def test_two_genuine_purchases_need_two_different_keys(self):
        """The key is per attempt, so buying twice with one key is one purchase.

        This is the other half of the replay contract: if a client reused one key
        for two real buys, the second would deliver nothing while reporting
        success. The reply is what makes that visible.
        """
        pid = _funded("two_keys", "spirit_stone")
        product = "beast_food_basic"
        first = _buy(pid, product, key="attempt-1")
        second = _buy(pid, product, key="attempt-2")
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        # A swallowed second purchase answers as a REPLAY: a bare ack with no
        # receipt fields at all. So the check is the key set, not a flag.
        assert set(second.json()) == RECEIPT_KEYS, (
            "a second, differently-keyed purchase was swallowed as a replay: "
            f"{sorted(second.json())}"
        )
        assert inventory_store.get_item_count(pid, product) == 2


# ------------------------------------------------------------------
# POST /shop/buy -- the refusals
# ------------------------------------------------------------------

class TestBuyRefusals:
    """Every way the till says no, and what it takes with it.

    Each arm also asserts the balance did not move. A refusal that charges
    anyway is the worst outcome here: the player has paid for nothing and has
    no way to tell from the reply.
    """

    def test_an_unknown_product_is_refused_400_naming_the_field(self):
        """MEASURED: 400 with the plain string "Product not found".

        Not a 404. The product catalogue is a table, not a resource, so "no such
        id" is a bad request against a known endpoint.
        """
        pid = _funded("unknown", "fateum")
        before = _balance(pid, "fateum")
        r = _buy(pid, "not_a_real_product", key="unk")
        assert r.status_code == 400, r.text
        assert _detail(r) == "Product not found", r.text
        assert _balance(pid, "fateum") == before

    def test_a_quantity_below_one_is_refused_400_before_any_price_is_computed(self):
        """MEASURED: 400 "Quantity must be at least 1" -- NOT a 422.

        The model declares no bound, so the guard is in the handler. A client
        cannot predict this status from the schema; it has to handle the route's
        own answer, which is what this records.
        """
        pid = _funded("quantity", "spirit_stone")
        before = _balance(pid, "spirit_stone")
        for bad in (0, -1):
            r = _buy(pid, "beast_food_basic", quantity=bad, key=f"q{bad}")
            assert r.status_code == 400, f"quantity={bad} -> {r.status_code} {r.text}"
            assert _detail(r) == "Quantity must be at least 1", r.text
        assert inventory_store.get_item_count(pid, "beast_food_basic") == 0
        assert _balance(pid, "spirit_stone") == before

    def test_a_quantity_above_the_products_per_transaction_limit_is_refused(self):
        """The limit is read from the product's own row, not restated here."""
        pid = _funded("limit", "spirit_stone")
        product = "beast_food_basic"
        limit = inv.SHOP_PRODUCTS[product]["limit"]
        before = _balance(pid, "spirit_stone")
        r = _buy(pid, product, quantity=limit + 1, key="over")
        assert r.status_code == 400, r.text
        detail = _detail(r)
        assert str(limit) in detail, f"the refusal does not name the limit: {detail!r}"
        assert inventory_store.get_item_count(pid, product) == 0
        assert _balance(pid, "spirit_stone") == before

    def test_a_purchase_with_no_idempotency_key_is_refused_and_the_server_mints_none(self):
        """MEASURED: 400, and the message explains why the server will not help.

        A body-derived key collapsed two genuine purchases into one "replay", so
        the second paid nothing. The server refusing to invent a key is the
        point; a client that retries without one gets nothing twice.
        """
        pid = _funded("nokey", "fateum")
        before = _balance(pid, "fateum")
        r = client.post(BUY, json={"player_id": pid, "product_id": "starter_pack",
                                   "client_version": _wallet_version(pid)})
        assert r.status_code == 400, r.text
        detail = _detail(r)
        assert isinstance(detail, str) and "Idempotency-Key header required" in detail, detail
        assert _balance(pid, "fateum") == before

    def test_insufficient_funds_names_the_currency_and_exactly_how_much_is_short(self):
        """The four refusal numbers must agree with each other.

        ``missing`` is the difference between what the price is and what the
        player has; asserting that relation catches a stale copy of any one of
        them, which is how a client ends up telling a player to top up by the
        wrong amount.
        """
        pid = _new_player("broke")  # deliberately unfunded
        product = "beast_food_basic"
        table = inv.SHOP_PRODUCTS[product]
        r = _buy(pid, product, key="broke")
        assert r.status_code == 400, r.text
        detail = _detail(r)
        assert detail["error"] == "insufficient_funds", detail
        assert detail["currency"] == table["currency"], detail
        assert detail["required"] == table["price"], detail
        assert detail["available"] == _balance(pid, table["currency"]), detail
        assert detail["missing"] == detail["required"] - detail["available"], detail
        assert isinstance(detail.get("message"), str) and detail["message"], detail
        assert inventory_store.get_item_count(pid, product) == 0

    def test_a_stale_client_version_is_refused_with_the_wallets_own_vocabulary(self):
        """MEASURED: the refusal is the WALLET's dict, not the shop's.

        Two error vocabularies on one route. A client that only knows the shop's
        ``error``-keyed shape still finds ``error`` here, but it must not assume
        the shop's sibling fields (``currency``, ``required``, ``missing``)
        exist.
        """
        pid = _funded("stale", "fateum")
        before = _balance(pid, "fateum")
        r = _buy(pid, "starter_pack", key="stale", version=_wallet_version(pid) + 99)
        assert r.status_code == 400, r.text
        detail = _detail(r)
        assert detail.get("ok") is False, detail
        assert detail.get("error") == "stale wallet_version", detail
        assert _balance(pid, "fateum") == before

    def test_an_unknown_player_in_the_body_is_refused_before_the_handler_runs(self):
        """MEASURED: 403, because the id is checked against the SESSION.

        The handler's own "Player not found" 404 is unreachable over HTTP: the
        guard compares the body's id with the token first, so a stranger always
        gets the 403. Pinning 403 is what tells a client author not to write a
        404 branch that can never fire.
        """
        _funded("unknown_player", "fateum")  # a real session, no matching body id
        # ``version`` is passed explicitly: reading the wallet row of a player
        # that does not exist would raise here, which would test the helper
        # instead of the route.
        r = _buy("no_such_player_at_all", "starter_pack", key="ghost", version=0)
        assert r.status_code == 403, r.text
        assert _detail(r) == MISMATCH_DETAIL, r.text

    def test_the_handler_also_checks_the_player_itself_over_http(self):
        """The 404 branch is SHADOWED over HTTP, so this pins it where it runs.

        Two layers guard this route. The ownership dependency compares the body's
        ``player_id`` with the session and answers 403, so over HTTP the
        handler's own "Player not found" 404 is unreachable -- deleting it changes
        nothing an HTTP client can see. A mutation control proved that: removing
        the handler's check left every test in this file green.

        This calls the handler directly, which is how that check earns its keep
        (an in-process caller, or a future route mounted without the dependency).
        Asserting it here is what makes the deletion of a shadowed guard visible
        instead of free.
        """
        from fastapi import HTTPException

        from inventory import InventoryBuyRequest

        request = InventoryBuyRequest(player_id="no_such_player_at_all",
                                      product_id="starter_pack")
        with pytest.raises(HTTPException) as caught:
            # ``None`` for the raw Request is safe and deliberate: the player
            # check runs first, so the handler never reads it. If that check is
            # removed this raises AttributeError instead -- which is still a
            # failure, and the failure message names the removed guard.
            mainmod.shop_buy(request, None)
        assert caught.value.status_code == 404, caught.value.status_code
        assert caught.value.detail == "Player not found", caught.value.detail

    def test_a_malformed_quantity_is_a_422_naming_the_field(self):
        """A wrong TYPE is a validation error; a wrong VALUE is the handler's 400.

        Both are worth distinguishing: 422 means "this request was not
        understood", 400 means "understood and refused".
        """
        pid = _funded("malformed", "fateum")
        r = client.post(BUY, json={"player_id": pid, "product_id": "starter_pack",
                                   "quantity": "two", "client_version": 0},
                        headers=_key("mal"))
        assert r.status_code == 422, r.text
        located = {error["loc"][-1] for error in r.json()["detail"]}
        assert "quantity" in located, r.text

    @pytest.mark.parametrize("missing", ["player_id", "product_id"])
    def test_a_missing_required_field_is_a_422_naming_the_field(self, missing):
        pid = _funded("required_" + missing, "fateum")
        payload = {"player_id": pid, "product_id": "starter_pack"}
        payload.pop(missing)
        r = client.post(BUY, json=payload, headers=_key(f"req-{missing}"))
        assert r.status_code == 422, r.text
        assert missing in {error["loc"][-1] for error in r.json()["detail"]}, r.text

    def test_an_omitted_quantity_defaults_to_one_and_buys_one(self):
        """The default is part of the contract: a client may send two fields."""
        pid = _funded("default_qty", "fateum")
        r = client.post(BUY, json={"player_id": pid, "product_id": "starter_pack",
                                   "client_version": _wallet_version(pid)},
                        headers=_key("dflt"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["quantity"] == body["requested"] == 1, body

    def test_the_expansion_scroll_is_refused_at_the_capacity_ceiling_with_numbers(self):
        """MEASURED: ``at_capacity_ceiling`` carrying slots and max_slots.

        Paying would buy nothing, so the route refuses BEFORE the charge rather
        than refunding after it. The numbers come from the module's own
        constant so a retune does not break the assertion.
        """
        pid = _funded("scroll_ceiling", "fateum")
        original = inventory_store.get_player_capacity
        inventory_store.get_player_capacity = lambda _pid: mainmod.MAX_INVENTORY_SLOTS
        try:
            before = _balance(pid, "fateum")
            r = _buy(pid, mainmod.EXPANSION_SCROLL_ITEM, key="ceil")
            assert r.status_code == 400, r.text
            detail = _detail(r)
            assert detail["error"] == "at_capacity_ceiling", detail
            assert detail["slots"] == detail["max_slots"] == mainmod.MAX_INVENTORY_SLOTS, detail
            assert isinstance(detail.get("message"), str) and detail["message"], detail
            assert _balance(pid, "fateum") == before, "a refused scroll was charged"
        finally:
            inventory_store.get_player_capacity = original

    def test_a_delivery_failure_returns_the_money_and_says_how_much(self):
        """MEASURED: ``delivery_failed`` with a ``refunded`` map, net zero.

        This arm used the un-buyable pack as its way in, but that row now buys,
        so the failure is FORCED: the pack's wallet grant is stubbed to refuse
        while the refund that failure triggers runs for real. The point of the arm
        is the ARITHMETIC -- the player's balance must be exactly where it
        started, and a replay of the same key must not refund a second time --
        and those hold whichever refusal produced them.
        """
        pid = _funded("refund", "fateum", "spirit_stone")
        product = "spirit_stone_pack_small"
        currency = inv.SHOP_PRODUCTS[product]["currency"]
        before = _balance(pid, currency)
        price = inv.SHOP_PRODUCTS[product]["price"]

        # Only the PURCHASE grant is refused. The refund the branch then issues
        # must still go through, or this arm would be measuring a failed refund
        # instead of a failed delivery.
        original_earn = mainmod.wallet_earn

        def refuse_the_grant(store, player_id, cur, amount, key, reward_type=""):
            if reward_type == "shop_purchase":
                return {"ok": False, "error": "grant refused for this test"}
            return original_earn(store, player_id, cur, amount, key, reward_type)

        # Patched through the module the running handler resolves the name in,
        # and asserted, so a second import identity cannot make this a no-op.
        assert mainmod is sys.modules["server.main"]
        mainmod.wallet_earn = refuse_the_grant
        try:
            r = _buy(pid, product, key="refund-1")
            assert r.status_code == 400, r.text
            detail = _detail(r)
            assert detail["error"] == "delivery_failed", detail
            assert detail["refunded"] == {currency: price}, detail
            assert _balance(pid, currency) - before == 0, (
                "a rolled-back purchase left the player's balance changed"
            )

            replay = _buy(pid, product, key="refund-1")
            assert replay.status_code == 400, replay.text
            assert _detail(replay)["refunded"] == {currency: price}, replay.text
            assert _balance(pid, currency) - before == 0, (
                "a replayed rolled-back purchase refunded twice"
            )
        finally:
            mainmod.wallet_earn = original_earn


# ------------------------------------------------------------------
# POST /shop/buy -- a price the server cannot do arithmetic with
# ------------------------------------------------------------------

class TestNonFinitePrice:
    """A price row that is not a number must be REFUSED, not spent.

    Why this class exists
    ---------------------
    Every other arm in this file buys or refuses a product whose price the
    server itself minted. This one is different: ``SHOP_PRODUCTS`` is DATA, and
    a bad row in data reaches the reply as JSON. The three non-finite values
    ``inf``, ``-inf`` and ``nan`` are not merely wrong numbers -- they are
    values no honest arithmetic survives. Measured before this class was
    written: ``int(inf)`` raises ``OverflowError`` and ``int(nan)`` raises
    ``ValueError``, and both escape the handler as a bare 500 with no field
    named. So the contract is not "the purchase succeeds" -- it is "the server
    says no, names the price, and moves no money".

    Measured, not assumed
    ---------------------
    Every status and body below came from running the live app on 2026-10-05.
    The refusal is 400 with the plain string ``"Product price is not available"``
    -- the SAME string a zero or negative price earns, because the guard is
    ``price_per <= 0`` after the finite conversion collapses every one of them
    to the same value. A client therefore cannot tell "misconfigured price" from
    "free item" by status alone, and this file says so rather than implying the
    two are distinguishable.

    What is deliberately NOT asserted
    ----------------------------------
    The refused row is not restored by the server -- the TEST restores it in a
    ``finally``. If the route mutated the table, that would be a finding worth
    a new arm; the guard reads the row and returns, so nothing here needs it.

    Run
    ---
        cd G:/Projects/wayfarers-cycle
        tools/wc_pytest.sh server/tests/test_shop_contract.py -k NonFinite
    """

    #: MEASURED 2026-10-05: every one of these answers 400 with the plain
    #: string below. The three non-finite floats are the arithmetic bombs; the
    #: rest are the neighbours of the same ``<= 0`` guard, included so a future
    #: change that widens or narrows the guard shows up here as a status flip
    #: rather than as a silent behaviour change.
    #:
    #: ``"inf"`` and ``None`` are included because they are what a bad config
    #: loader or a missing column actually produce -- a string that looks
    #: numeric, and nothing at all.
    UNUSABLE_PRICES = [
        pytest.param(float("inf"), id="pos_inf"),
        pytest.param(float("-inf"), id="neg_inf"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param("abc", id="not_a_number"),
        pytest.param(None, id="missing"),
        pytest.param("inf", id="numeric_string"),
    ]

    #: MEASURED: the one refusal string for every row in ``UNUSABLE_PRICES``.
    UNUSABLE_DETAIL = "Product price is not available"

    def _with_price(self, product: str, price):
        """Swap one product's price for the duration of a test.

        Context-manager shaped by hand rather than with ``contextlib`` because
        the restore has to be unconditional: a price row left at ``inf`` would
        make every LATER test in this file fail for a reason that has nothing
        to do with what it is testing. This is the same discipline the
        ``wallet_earn`` stub in ``TestBuyRefusals`` uses.
        """
        return _PriceOverride(inv.SHOP_PRODUCTS, product, price)

    @pytest.mark.parametrize("price", UNUSABLE_PRICES)
    def test_a_price_the_server_cannot_do_arithmetic_with_is_refused(self, price):
        """The core arm: an unusable price is a 400 naming the price.

        The refusal happens BEFORE the charge, so the balance delta is exactly
        zero rather than "zero after a refund". Those are different states and a
        player can tell them apart if a rollback is ever dropped.
        """
        pid = _funded("nonfinite", "spirit_stone")
        product = "beast_food_basic"
        currency = inv.SHOP_PRODUCTS[product]["currency"]
        before = _balance(pid, currency)

        with self._with_price(product, price):
            r = _buy(pid, product, key=f"nf-{price!r}")

        assert r.status_code == 400, (
            f"price={price!r} answered {r.status_code} instead of refusing: {r.text}"
        )
        assert _detail(r) == self.UNUSABLE_DETAIL, r.text
        assert _balance(pid, currency) - before == 0, (
            f"price={price!r} moved the player's balance"
        )

    @pytest.mark.parametrize("price", UNUSABLE_PRICES)
    def test_a_refused_price_delivers_nothing_to_the_inventory(self, price):
        """The refusal half of the contract a player experiences: no goods.

        A refusal that still credited the item would be the "paid for nothing"
        report inverted -- the player got the item and kept their money. Both
        stores are read back because they are separate stores, and a route that
        refunded the wallet while keeping the item would slip past a
        wallet-only assertion.
        """
        pid = _funded("nonfinite_inv", "spirit_stone")
        product = "beast_food_basic"

        with self._with_price(product, price):
            r = _buy(pid, product, key=f"nfi-{price!r}")

        assert r.status_code == 400, r.text
        assert inventory_store.get_item_count(pid, product) == 0, (
            f"price={price!r} credited the item anyway"
        )
        assert players_db[pid] is not None  # the fixture really made a player

    def test_a_rejected_price_leaves_no_replay_receipt_behind(self, price=float("nan")):
        """A refused price must not BURN the idempotency key.

        This is the arm that catches a ledger write placed before the price
        guard. If the key were consumed by the refusal, the player's retry after
        a price fix would be swallowed as a replay and answer ``{"ok": true}``
        while delivering nothing -- the silent-success failure the receipt shape
        in ``TestBuyReceipt`` exists to make visible. So the retry must get the
        SAME 400 while the price is still bad, and a real purchase once it is
        restored.
        """
        pid = _funded("nonfinite_replay", "spirit_stone")
        product = "beast_food_basic"
        key = "nf-replay"

        with self._with_price(product, price):
            first = _buy(pid, product, key=key)
            second = _buy(pid, product, key=key)
        assert first.status_code == second.status_code == 400, (first.text, second.text)
        assert _detail(second) == self.UNUSABLE_DETAIL, (
            "the retry was answered as a replay instead of the same refusal: "
            f"{second.text}"
        )

        # The price is sane again: the SAME key must now buy for real. This is
        # what proves the key was not spent by either refusal.
        healed = _buy(pid, product, key=key)
        assert healed.status_code == 200, (
            f"the key was burned by the refusals, so a retried purchase is "
            f"swallowed: {healed.text}"
        )
        assert set(healed.json()) == RECEIPT_KEYS, sorted(healed.json())
        assert inventory_store.get_item_count(pid, product) == 1

    def test_the_price_guard_is_a_finite_check_and_not_merely_a_sign_check(self):
        """A sign-only guard would pass ``inf`` and ``nan`` straight through.

        ``inf > 0`` and ``nan > 0`` is False while ``nan < 0`` is also False, so
        a guard written as ``price > 0`` or as ``price <= 0`` on the RAW value
        behaves differently per value. Asserting that ``inf`` and ``nan`` reach
        the SAME refusal as a negative price is what pins the conversion, rather
        than pinning that "some bad price is refused" -- a statement a
        sign-only guard would also satisfy for the negative case alone.
        """
        pid = _funded("nonfinite_pair", "spirit_stone")
        product = "beast_food_basic"

        replies = {}
        for price in (float("inf"), float("nan"), -1):
            with self._with_price(product, price):
                r = _buy(pid, product, key=f"pair-{price!r}")
            replies[repr(price)] = (r.status_code, _detail(r))

        assert len(set(replies.values())) == 1, (
            f"inf, nan and a negative price answered differently, so the guard is "
            f"not one finite check: {replies}"
        )
        assert list(replies.values())[0] == (400, self.UNUSABLE_DETAIL), replies

    def test_a_non_finite_price_is_refused_for_every_product_not_just_one(self):
        """The guard reads the row, so it applies to the whole catalogue.

        One product would pass a test that merely proved the guard fires. A
        branch that hard-coded a product id -- a special case for the one row
        someone once broke -- would survive that. Every advertised row is driven
        with an ``inf`` price here.
        """
        pid = _funded("nonfinite_sweep", "fateum", "spirit_stone")
        advertised = [row["product_id"]
                      for row in client.get(PRODUCTS).json()["products"]]
        assert advertised, "the price list was empty, so this arm would prove nothing"

        for product_id in advertised:
            with self._with_price(product_id, float("inf")):
                r = _buy(pid, product_id, key=f"sweep-{product_id}")
            assert r.status_code == 400, (
                f"{product_id} was advertised and an inf price did not stop it: "
                f"{r.status_code} {r.text}"
            )
            assert _detail(r) == self.UNUSABLE_DETAIL, (product_id, r.text)

    def test_a_non_finite_price_is_refused_even_with_a_stale_free_offer_otherwise(self):
        """Ordering: the price guard runs before the money is ever read.

        The other refusal arms in this file are all about money -- a stale
        wallet version, an empty balance. If one of those fired first the
        non-finite price would never be examined and this class would be
        measuring the wrong guard. A funded player with a CURRENT wallet
        version removes every earlier refusal, so the 400 that comes back can
        only be the price one.
        """
        pid = _funded("nonfinite_order", "spirit_stone")
        product = "beast_food_basic"
        table = inv.SHOP_PRODUCTS[product]
        currency = table["currency"]

        # The player can afford a normal purchase, so funds are not the refusal.
        assert _balance(pid, currency) >= table["price"], "the fixture underfunded the player"
        before = _balance(pid, currency)

        with self._with_price(product, float("inf")):
            r = _buy(pid, product, key="nf-order")  # version read fresh by ``_buy``

        assert r.status_code == 400, r.text
        assert _detail(r) == self.UNUSABLE_DETAIL, r.text
        assert _balance(pid, currency) - before == 0


class _PriceOverride:
    """Set one product's price and put the old one back, unconditionally.

    Written as a small class rather than a fixture because the value differs
    per test and the restore must be unconditional. ``__exit__`` restores even
    when the body raises, which is the case that matters: a failing assertion
    inside the ``with`` block would otherwise leave the shared ``SHOP_PRODUCTS``
    table holding ``inf`` for every test that runs after it.
    """

    def __init__(self, table: dict, product_id: str, price):
        self._table = table
        self._product_id = product_id
        self._price = price
        self._saved = None

    def __enter__(self):
        self._saved = self._table[self._product_id]["price"]
        self._table[self._product_id]["price"] = self._price
        return self

    def __exit__(self, *_exc):
        self._table[self._product_id]["price"] = self._saved
        return False  # never swallow an exception from the test body

# ------------------------------------------------------------------
# POST /shop/buy -- the INVENTORY write is atomic with the charge
# ------------------------------------------------------------------

class TestInventoryAtomicity:
    """The bag and the wallet are two stores; a purchase touches both.

    What the other classes already prove
    -------------------------------------
    ``TestBuyRefusals.test_a_delivery_failure_returns_the_money_and_says_how_much``
    forces a failure in the WALLET grant branch (the spirit-stone pack). That
    proves the refund machinery runs, but it says nothing about the branch an
    inventory item actually takes, which is a different function with its own
    refund call. This class covers the inventory half: a partial delivery and a
    hard delivery failure.

    Why atomicity is the contract
    -----------------------------
    A purchase charges first and delivers second, so between those two steps
    the player can have paid for goods they do not hold. The refund is what
    closes that window. The failure this class exists to catch is a refund that
    is dropped, capped, or bypassed -- which reads to the player as "I paid and
    got nothing", and which no status-code assertion would notice.

    Measured, not assumed
    ---------------------
    Both bodies below were measured against the live app on 2026-10-05:

    * A partial fill answers **200** with two EXTRA receipt keys, ``refunded``
      and ``warning: "stack_full"``. It is not an error -- the player did get
      part of what they asked for. ``quantity`` reports what LANDED (2) while
      ``requested`` reports what was asked for (5), so a client reading the
      wrong one of the two over-reports the delivery.
    * A hard delivery failure answers **400** ``delivery_failed`` with a
      ``refunded`` map, and leaves both stores exactly where they started.

    Relations, not frozen numbers
    -----------------------------
    The ceiling is read from ``ITEM_DEFINITIONS`` and the price from
    ``SHOP_PRODUCTS``, and the assertions compare the receipt against the
    WALLET'S OWN DELTA. A balance retune moves the prices and the ceiling
    without editing this file; a receipt that lies still fails.
    """

    #: MEASURED 2026-10-05: a partially-filled purchase carries two keys the
    #: full-delivery receipt does not. Pinned because ``RECEIPT_KEYS`` is an
    #: EXACT set for the ordinary case, and a client that destructures the
    #: ordinary shape would drop the refund on the floor.
    PARTIAL_KEYS = RECEIPT_KEYS | {"refunded", "warning"}

    #: A product that can be PARTLY served. Read from the tables at run time
    #: rather than hard-coded, so a catalogue retune cannot leave this arm
    #: testing a ceiling that no longer exists.
    #:
    #: Two things must hold: the per-transaction limit must be at least 2 (or
    #: an order of 2 could not be placed at all) and the stack ceiling at least
    #: 2 (or nothing could be short). Note what this does NOT require: the
    #: ceiling to be BELOW the limit. Measured on 2026-10-05, a partial fill
    #: depends on how much the player ALREADY HOLDS against the ceiling --
    #: ``battle_potion_buff`` has limit 5 and ceiling 10, and ordering 5 while
    #: holding 8 delivered 2 and refunded 3. An earlier version of this helper
    #: asserted ``ceiling < limit`` and therefore picked a single-unit-limit
    #: product whose order could never be partly served.
    @staticmethod
    def _a_partly_fillable_product() -> str:
        for product_id, row in inv.SHOP_PRODUCTS.items():
            item_def = inv.ITEM_DEFINITIONS.get(product_id)
            if item_def is None:
                continue
            if row["limit"] >= 2 and item_def.get("stack_size", 99) >= 2:
                return product_id
        pytest.skip("no shop product can be ordered 2 units at a time")

    def test_a_partly_delivered_order_refunds_exactly_the_units_never_landed(self):
        """The core atomicity arm: pay-for-five, land two, get three back.

        Three numbers have to agree or the player is wrong somewhere: the
        charge, the refund, and what is actually in the bag. They are asserted
        as RELATIONS -- the net cost is the delivered units' price and not one
        unit more -- so this catches a dropped refund, a mis-sized refund, or a
        receipt that reports the wrong delivery count.
        """
        product = self._a_partly_fillable_product()
        table = inv.SHOP_PRODUCTS[product]
        currency = table["currency"]
        ceiling = inv.ITEM_DEFINITIONS[product].get("stack_size", 99)
        # One unit of headroom, then order two: exactly one fits, so exactly one
        # is short. Building the shortfall from the headroom rather than from
        # the ceiling keeps the arm valid for any ceiling/limit pairing.
        held = ceiling - 1
        quantity = 2
        assert table["limit"] >= quantity, (
            f"{product} allows {table['limit']} per transaction, so an order of "
            f"{quantity} cannot be placed"
        )

        pid = _funded("partial", currency)
        assert inventory_store.add_item(pid, product, held).get("ok") is True
        assert inventory_store.get_item_count(pid, product) == held
        before = _balance(pid, currency)

        r = _buy(pid, product, quantity=quantity, key="partial-1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == self.PARTIAL_KEYS, sorted(body)
        assert body["requested"] == quantity, body
        delivered = ceiling - held  # 1
        assert body["quantity"] == delivered, (
            f"the receipt claims {body['quantity']} delivered but only {delivered} "
            f"could fit: the player held {held} under a ceiling of {ceiling}"
        )
        assert body["granted"]["item"] == {"item_id": product, "quantity": delivered}, body
        assert body["warning"] == "stack_full", body

        # The refund is the whole price of the units that did not land, and the
        # wallet agrees to the unit. Asserted against the wallet's own delta so
        # a retune of either table does not need this test edited.
        short = quantity - delivered
        assert body["refunded"] == {currency: short * table["price"]}, body
        assert body["spent"] == {currency: delivered * table["price"]}, body
        assert _balance(pid, currency) - before == -delivered * table["price"], (
            "the wallet moved by neither the charge nor the charge less the refund"
        )
        assert inventory_store.get_item_count(pid, product) == ceiling, (
            "a partly-filled order pushed the stack past its own ceiling"
        )

    def test_a_partly_delivered_order_does_not_overfill_the_stack(self):
        """The bag is bounded; a paid order must not be the thing that breaks it.

        The refund makes the partial fill honest about MONEY. This arm is about
        the other half: the ceiling itself must still hold afterwards, or the
        refund is paying for goods nobody can hold.
        """
        product = self._a_partly_fillable_product()
        ceiling = inv.ITEM_DEFINITIONS[product].get("stack_size", 99)
        quantity = 2
        pid = _funded("ceiling_hold", inv.SHOP_PRODUCTS[product]["currency"])
        assert inv.SHOP_PRODUCTS[product]["limit"] >= quantity
        inventory_store.add_item(pid, product, ceiling - 1)

        r = _buy(pid, product, quantity=quantity, key="ceiling-hold")
        assert r.status_code == 200, r.text
        assert inventory_store.get_item_count(pid, product) == ceiling, (
            f"the stack holds {inventory_store.get_item_count(pid, product)} but "
            f"its ceiling is {ceiling}"
        )
        assert r.json()["granted"]["item"]["quantity"] == r.json()["quantity"], (
            "the receipt's two counts disagree, so a client cannot tell what landed"
        )

    def test_a_full_stack_is_never_shrunk_by_a_purchase_that_cannot_fit(self):
        """A stack at its ceiling keeps every unit it already holds.

        The refund machinery exists to return MONEY for goods that did not
        land. It must never become the thing that deletes goods already in the
        bag: the units were paid for by an earlier purchase and are owed to the
        player regardless of whether this one fit.

        MEASURED 2026-10-05, and a premise correction: an earlier version of
        this arm tried to hold ``ceiling + 3`` units, assuming a stack could
        START above its own ceiling. It cannot -- ``add_item`` refuses to grow
        a stack past the ceiling, so that state is unreachable through the
        store and the arm was testing an impossible setup. The reachable and
        stronger statement is the one below: a purchase made with no headroom
        changes the count not at all.
        """
        product = self._a_partly_fillable_product()
        table = inv.SHOP_PRODUCTS[product]
        ceiling = inv.ITEM_DEFINITIONS[product].get("stack_size", 99)
        pid = _funded("full_stack", table["currency"])
        assert inventory_store.add_item(pid, product, ceiling).get("ok") is True
        assert inventory_store.get_item_count(pid, product) == ceiling

        before = _balance(pid, table["currency"])
        r = _buy(pid, product, quantity=min(table["limit"], 2), key="full-stack")
        assert r.status_code == 200, r.text
        assert inventory_store.get_item_count(pid, product) == ceiling, (
            f"a purchase against a full stack left "
            f"{inventory_store.get_item_count(pid, product)} units, not {ceiling}"
        )
        # Nothing was delivered, so the whole charge comes back: the net spend
        # is zero even though the reply is a 200.
        body = r.json()
        assert body["quantity"] == 0, body
        assert _balance(pid, table["currency"]) - before == 0, (
            "a fully-refunded order still cost the player money"
        )

    def test_a_failed_inventory_write_rolls_the_money_back_and_delivers_nothing(self):
        """The hard failure: the bag refuses, so the wallet must not keep the charge.

        ``inventory_store.add_item`` is stubbed to refuse while the refund that
        failure triggers runs for real -- the same forcing shape the wallet
        branch uses, aimed at the branch an inventory item actually takes. The
        point is the ARITHMETIC across BOTH stores: net zero money, zero goods.
        Reading only the reply would pass a refund that never landed.
        """
        pid = _funded("addfail", "spirit_stone")
        product = "beast_food_basic"
        table = inv.SHOP_PRODUCTS[product]
        currency = table["currency"]
        before = _balance(pid, currency)

        original = inventory_store.add_item
        inventory_store.add_item = lambda *_a, **_k: {"ok": False, "error": "stubbed add failure"}
        try:
            assert mainmod is sys.modules["server.main"], (
                "the patch would target a second import identity and be a no-op"
            )
            r = _buy(pid, product, quantity=2, key="addfail-1")
        finally:
            inventory_store.add_item = original

        assert r.status_code == 400, r.text
        detail = _detail(r)
        assert detail["error"] == "delivery_failed", detail
        assert detail["refunded"] == {currency: table["price"] * 2}, detail
        assert detail["message"] == "stubbed add failure", detail
        assert _balance(pid, currency) - before == 0, (
            "a rolled-back purchase left the player's balance changed"
        )
        assert inventory_store.get_item_count(pid, product) == 0, (
            "a refused delivery still credited the item"
        )

    def test_a_failed_inventory_write_refunds_once_and_the_retry_still_refuses(self):
        """A replay of a rolled-back purchase must not refund a second time.

        The wallet's idempotency key is what makes this safe, so it is worth
        its own arm: a refund keyed only by player would pay the player back on
        every retry, turning a failure into a money printer. The balance is
        read once and must not move across the replay.
        """
        pid = _funded("addfail_replay", "spirit_stone")
        product = "beast_food_basic"
        table = inv.SHOP_PRODUCTS[product]
        currency = table["currency"]
        before = _balance(pid, currency)
        key = "addfail-replay"

        original = inventory_store.add_item
        inventory_store.add_item = lambda *_a, **_k: {"ok": False, "error": "stubbed add failure"}
        try:
            first = _buy(pid, product, quantity=2, key=key)
            second = _buy(pid, product, quantity=2, key=key)
        finally:
            inventory_store.add_item = original

        assert first.status_code == second.status_code == 400, (first.text, second.text)
        assert _detail(second)["refunded"] == {currency: table["price"] * 2}, second.text
        assert _balance(pid, currency) - before == 0, (
            "a replayed rolled-back purchase refunded twice"
        )

        # The replay re-raises the SAME recorded failure rather than re-running
        # the delivery. That is deliberate: the wallet already charged this key,
        # so running the grant again would deliver goods for a second time
        # against a single charge. MEASURED 2026-10-05, and a premise correction:
        # an earlier version of this arm asserted the key was NOT burned and a
        # retry would succeed once the store recovered. That is true of a
        # refusal taken BEFORE the charge (``TestNonFinitePrice`` proves it) and
        # false here, so the arm now pins the behaviour the handler really has.
        assert _detail(second) == _detail(first), (
            "a replay reported a different failure than the original attempt"
        )

        # A DIFFERENT key is a different purchase, so the recovered store serves
        # it for real -- which is the shape a client needs after a failed buy.
        healed = _buy(pid, product, quantity=2, key=key + "-retry")
        assert healed.status_code == 200, (
            f"a new key after a recovered delivery failure was swallowed: {healed.text}"
        )
        assert set(healed.json()) == RECEIPT_KEYS, sorted(healed.json())
        assert inventory_store.get_item_count(pid, product) == 2
        assert _balance(pid, currency) - before == -table["price"] * 2, (
            "the successful retry did not charge exactly one order's price"
        )

