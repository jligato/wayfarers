"""Contract tests for server/trading_market.py.

These tests pin the SHAPE of the nine market addresses: which ones demand a
signed-in session, which ones demand an Idempotency-Key header, what a request
body is allowed to carry, which keys a response carries, and which error code
each failure produces. They are deliberately about the contract rather than
about the economy numbers -- test_trading_market_probe.py owns the payout and
cap behaviour.

Every test drives a real FastAPI route through TestClient against an in-memory
player store, so the assertion is on an HTTP reply, not on a function call.

Run: tools/wc_pytest.sh server/tests/test_trading_market_contract.py
"""
import json
import os
import time

os.environ.pop("WC_PG_HOST", None)

import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import trading_market
from inventory import InventoryStore
from player_store import PlayerStore

#: The nine market addresses this module mounts, with the HTTP method and the
#: keys a caller MUST send. Used by the shape tests below.
MONEY_ROUTES = [
    ("POST", "/trade/request"),
    ("POST", "/trade/accept"),
    ("POST", "/trade/mail"),
    ("POST", "/trade/mail/accept"),
    ("POST", "/market/sect/list"),
    ("POST", "/auction/list"),
    ("POST", "/auction/bid"),
    ("POST", "/auction/buyout"),
]
AUTHED_ROUTES = MONEY_ROUTES + [
    ("POST", "/trade/cancel"),
    ("GET", "/market/sect"),
    ("GET", "/auction/listings"),
]

OFFER_VIEW_KEYS = {
    "trade_id", "method", "initiator_id", "counterparty_id", "state",
    "offered_items", "requested_items", "offered_stones", "requested_stones",
    "created_at", "expires_at",
}
LISTING_VIEW_KEYS = {
    "listing_id", "board", "seller_id", "sect_id", "item_id", "quantity",
    "state", "buyout_price", "current_bid", "bidder_id", "expires_at",
    "auto_relist_used",
}

# A body the caller may legitimately send on every money route: enough to get
# past shape validation, refused later for a reason of the test's choosing.
MINIMAL_BODY = {
    "POST /trade/request": {"player_id": "P", "counterparty_id": "C"},
    "POST /trade/accept": {"player_id": "P", "trade_id": "t"},
    "POST /trade/mail": {"player_id": "P", "recipient_id": "C"},
    "POST /trade/mail/accept": {"player_id": "P", "trade_id": "t"},
    "POST /market/sect/list": {"player_id": "P", "item_id": "talisman_return",
                               "quantity": 1},
    "POST /auction/list": {"player_id": "P", "item_id": "talisman_return",
                           "quantity": 1, "starting_price": 500},
    "POST /auction/bid": {"player_id": "P", "listing_id": "l", "amount": 500},
    "POST /auction/buyout": {"player_id": "P", "listing_id": "l"},
    "POST /trade/cancel": {"player_id": "P", "trade_id": "t"},
}

#: Routes that must NOT be handed an Idempotency-Key header: /trade/cancel
#: moves no money and takes no key.
NO_KEY_ROUTES = ["POST /trade/cancel"]


@pytest.fixture
def env(monkeypatch):
    """A mounted trading app with real, authenticated players and no database."""
    monkeypatch.delenv("WC_PG_HOST", raising=False)
    players = PlayerStore(None)
    assert players._pg is False, "fixture needs the in-memory PlayerStore"
    inv = InventoryStore(pg_pool=None, player_store=players)
    app = FastAPI()
    store = trading_market.register_trading_routes(app, lambda: players, inv, None)
    counter = {"n": 0}

    def make(name: str, stones: int = 0) -> str:
        counter["n"] += 1
        pid = f"{name}-{counter['n']}"
        players.create_account(
            pid, {"id": pid, "name": name, "spirit_root": "fire"}, b"k" * 32
        )
        with players.transaction(pid):
            players[pid]["spirit_stones"] = {"low_grade": stones}
        return pid

    yield {"client": TestClient(app), "players": players, "inv": inv,
           "store": store, "make": make}
    trading_market._INVENTORY[0] = None
    trading_market._SECT_STORE[0] = None


def _tok(pid: str) -> dict:
    return {"Authorization": f"Bearer {auth.issue_session(pid)}"}


def _idem(key: str) -> dict:
    return {"Idempotency-Key": key}


def _body(env, key: str, pid: str):
    """MINIMAL_BODY for a route, with the real player id substituted in."""
    body = dict(MINIMAL_BODY[key])
    body["player_id"] = pid
    return body


# --------------------------------------------------------------------------
# CONTROL: the harness reaches a real, live route.
# --------------------------------------------------------------------------
def test_control_harness_reaches_a_live_route(env):
    c = env["client"]
    seller = env["make"]("ctl_seller", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    key = "POST /auction/list"
    r = c.post("/auction/list", json=_body(env, key, seller),
               headers={**_tok(seller), **_idem("ctl")})
    assert r.status_code == 200, r.text
    assert r.json()["listing"]["state"] == "held"


# --------------------------------------------------------------------------
# The nine addresses are all mounted
# --------------------------------------------------------------------------
def test_all_nine_market_addresses_are_mounted(env):
    table = {(route.path, tuple(sorted(route.methods)))
             for route in env["client"].app.routes
             if getattr(route, "methods", None)}
    for method, path in AUTHED_ROUTES:
        assert (path, (method,)) in table, f"{method} {path} is not mounted"


# --------------------------------------------------------------------------
# Auth: every address refuses an anonymous caller
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", AUTHED_ROUTES,
                         ids=[f"{m}{p}" for m, p in AUTHED_ROUTES])
def test_every_address_requires_a_session(env, method, path):
    """No market address answers an anonymous caller."""
    c = env["client"]
    body = _body(env, f"{method} {path}", "nobody") if method == "POST" else {}
    call = c.get if method == "GET" else c.post
    r = call(path, json=body) if method == "POST" else call(path)
    assert r.status_code in (401, 403), (
        f"{method} {path} answered an anonymous caller: {r.status_code} {r.text}")


@pytest.mark.parametrize("method,path", AUTHED_ROUTES,
                         ids=[f"{m}{p}" for m, p in AUTHED_ROUTES])
def test_every_address_rejects_a_bogus_token(env, method, path):
    c = env["client"]
    headers = {"Authorization": "Bearer not-a-real-session"}
    body = _body(env, f"{method} {path}", "nobody") if method == "POST" else {}
    call = c.get if method == "GET" else c.post
    r = call(path, json=body, headers=headers) if method == "POST" \
        else call(path, headers=headers)
    assert r.status_code in (401, 403), (
        f"{method} {path} accepted a forged session: {r.status_code} {r.text}")


# --------------------------------------------------------------------------
# The Idempotency-Key header is part of the contract on eight routes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", MONEY_ROUTES,
                         ids=[f"{m}{p}" for m, p in MONEY_ROUTES])
def test_money_route_requires_an_idempotency_key(env, method, path):
    """The server refuses to mint the key itself; a missing one is a 400."""
    c = env["client"]
    pid = env["make"]("nokey")
    r = c.post(path, json=_body(env, f"{method} {path}", pid), headers=_tok(pid))
    assert r.status_code == 400, (
        f"{path} served a money request with no Idempotency-Key: {r.text}")
    assert "Idempotency-Key" in r.json()["detail"]


@pytest.mark.parametrize("method,path", MONEY_ROUTES,
                         ids=[f"{m}{p}" for m, p in MONEY_ROUTES])
def test_blank_idempotency_key_is_refused(env, method, path):
    c = env["client"]
    pid = env["make"]("blankkey")
    r = c.post(path, json=_body(env, f"{method} {path}", pid),
               headers={**_tok(pid), **_idem("   ")})
    assert r.status_code == 400, f"{path} accepted a blank key: {r.text}"


def test_trade_cancel_needs_no_idempotency_key(env):
    """/trade/cancel moves no money, so the contract does not ask for a key."""
    c = env["client"]
    pid = env["make"]("canceller")
    r = c.post("/trade/cancel", json={"player_id": pid, "trade_id": "nope"},
               headers=_tok(pid))
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------
# Request bodies are extra="forbid": a server-computed field is a 422
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", MONEY_ROUTES + [("POST", "/trade/cancel")],
                         ids=[f"{m}{p}" for m, p in MONEY_ROUTES + [("POST", "/trade/cancel")]])
@pytest.mark.parametrize("smuggled", ["price", "seller_net_stones", "tax_stones",
                                      "flag_reason", "seller_id", "world_id"])
def test_body_refuses_a_server_computed_field(env, method, path, smuggled):
    """A caller naming the tax or the flag is refused, not silently ignored."""
    c = env["client"]
    pid = env["make"]("smuggle")
    body = _body(env, f"{method} {path}", pid)
    body[smuggled] = 1
    headers = {**_tok(pid), **_idem("sm")} if path != "/trade/cancel" else _tok(pid)
    r = c.post(path, json=body, headers=headers)
    assert r.status_code == 422, (
        f"{path} accepted a caller-supplied {smuggled!r}: {r.text}")


@pytest.mark.parametrize("method,path", MONEY_ROUTES + [("POST", "/trade/cancel")],
                         ids=[f"{m}{p}" for m, p in MONEY_ROUTES + [("POST", "/trade/cancel")]])
def test_body_refuses_any_unknown_field(env, method, path):
    c = env["client"]
    pid = env["make"]("extra")
    body = _body(env, f"{method} {path}", pid)
    body["totally_made_up"] = "x"
    headers = {**_tok(pid), **_idem("ex")} if path != "/trade/cancel" else _tok(pid)
    r = c.post(path, json=body, headers=headers)
    assert r.status_code == 422, f"{path} ignored an unknown field: {r.text}"


@pytest.mark.parametrize("method,path", MONEY_ROUTES,
                         ids=[f"{m}{p}" for m, p in MONEY_ROUTES])
def test_body_requires_player_id_and_rejects_empty(env, method, path):
    c = env["client"]
    pid = env["make"]("fields")
    body = _body(env, f"{method} {path}", pid)
    del body["player_id"]
    r = c.post(path, json=body, headers={**_tok(pid), **_idem("no-pid")})
    assert r.status_code == 422, f"{path} accepted a body with no player_id: {r.text}"

    body = _body(env, f"{method} {path}", pid)
    body["player_id"] = ""
    r = c.post(path, json=body, headers={**_tok(pid), **_idem("empty-pid")})
    assert r.status_code == 422, f"{path} accepted an empty player_id: {r.text}"


# --------------------------------------------------------------------------
# Response shapes
# --------------------------------------------------------------------------
def test_trade_request_returns_the_offer_view(env):
    c = env["client"]
    seller = env["make"]("shape_seller", stones=100000)
    buyer = env["make"]("shape_buyer", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("s1")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == OFFER_VIEW_KEYS, set(body) ^ OFFER_VIEW_KEYS
    assert body["method"] == "direct" and body["state"] == "pending"
    assert body["initiator_id"] == seller and body["counterparty_id"] == buyer
    assert body["offered_items"] == [{"item_id": "talisman_return", "quantity": 1}]
    assert body["requested_items"] == []


def test_trade_accept_returns_the_settlement_receipt(env):
    c = env["client"]
    seller = env["make"]("rc_seller", stones=0)
    buyer = env["make"]("rc_buyer", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("rc1")})
    tid = r.json()["trade_id"]
    r = c.post("/trade/accept", json={"player_id": buyer, "trade_id": tid},
               headers={**_tok(buyer), **_idem("rc2")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "trade_id", "state", "price_stones", "tax_stones", "flag"}
    assert body["ok"] is True and body["state"] == "settled"
    assert body["price_stones"] == 500 and body["tax_stones"] == 0
    assert body["flag"] == {}, "a like-for-like direct trade is not flagged"


def test_listing_response_carries_the_listing_view_and_the_fee(env):
    c = env["client"]
    seller = env["make"]("ls_seller", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("ls1")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"listing", "list_fee_stones", "price_bounds"}
    assert set(body["listing"]) == LISTING_VIEW_KEYS, (
        set(body["listing"]) ^ LISTING_VIEW_KEYS)
    assert body["listing"]["board"] == "auction"
    assert body["listing"]["state"] == "held"
    assert body["list_fee_stones"] == trading_market.list_fee_stones("talisman_return")
    assert set(body["price_bounds"]) >= {"basis", "floor_stones", "ceiling_stones"}


def test_sect_board_listing_shows_the_listing_view(env):
    c = env["client"]
    seller = env["make"]("sb_seller", stones=100000)
    with env["players"].transaction(seller):
        env["players"][seller]["sect_id"] = "sect-1"
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/market/sect/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "buyout_price": 900},
               headers={**_tok(seller), **_idem("sb1")})
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing"]["listing_id"]

    r = c.get("/market/sect", params={"player_id": seller}, headers=_tok(seller))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["sect_id"] == "sect-1"
    assert set(body) == {"ok", "sect_id", "listings", "list_fee_by_rarity",
                         "sale_tax_permille"}
    assert len(body["listings"]) == 1
    assert set(body["listings"][0]) == LISTING_VIEW_KEYS
    assert body["listings"][0]["listing_id"] == listing_id
    assert body["listings"][0]["sect_id"] == "sect-1"


def test_sect_board_for_a_player_with_no_sect_is_empty_not_another_sects(env):
    c = env["client"]
    member = env["make"]("sb_member", stones=100000)
    outsider = env["make"]("sb_outsider", stones=100000)
    with env["players"].transaction(member):
        env["players"][member]["sect_id"] = "sect-9"
    env["inv"].add_item(member, "talisman_return", 1)
    r = c.post("/market/sect/list",
               json={"player_id": member, "item_id": "talisman_return",
                     "quantity": 1, "buyout_price": 900},
               headers={**_tok(member), **_idem("sb2")})
    assert r.status_code == 200, r.text

    r = c.get("/market/sect", params={"player_id": outsider}, headers=_tok(outsider))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sect_id"] is None and body["listings"] == []
    assert body["note"] == "Not in a sect"


def test_sect_board_requires_the_player_id_query_param(env):
    c = env["client"]
    pid = env["make"]("sb_noparam")
    r = c.get("/market/sect", headers=_tok(pid))
    assert r.status_code == 422, r.text


def test_auction_listings_shape_and_item_filter(env):
    c = env["client"]
    seller = env["make"]("al_seller", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    # talisman_escape is the second item because both players in this file are
    # realm `mortal`; cultivation_pill_med needs qi_refining and is refused 403.
    env["inv"].add_item(seller, "talisman_escape", 1)
    for i, item in enumerate(("talisman_return", "talisman_escape")):
        r = c.post("/auction/list",
                   json={"player_id": seller, "item_id": item, "quantity": 1,
                         "starting_price": 500},
                   headers={**_tok(seller), **_idem(f"al{i}")})
        assert r.status_code == 200, r.text

    r = c.get("/auction/listings", headers=_tok(seller))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "listings", "single_server", "note"}
    assert body["single_server"] is True
    assert len(body["listings"]) == 2
    entry = body["listings"][0]
    assert set(entry) == LISTING_VIEW_KEYS | {"price_bounds"}

    r = c.get("/auction/listings", params={"item_id": "talisman_return"},
              headers=_tok(seller))
    assert r.status_code == 200, r.text
    filtered = r.json()["listings"]
    assert len(filtered) == 1 and filtered[0]["item_id"] == "talisman_return"


def test_a_buyer_need_not_already_hold_the_goods_they_are_buying(env):
    """A buyer must be able to win an auction for a good they do not hold.

    This was a real defect: /auction/bid and /auction/buyout used to run
    _check_tradable() on the BUYER for the listing's own item, so it refused
    with 409 "Does not hold" before any payment happened, making the auction
    house unsellable to anyone who did not already own a copy. That holding
    check is now optional on these two routes. The test carries no xfail
    marker: it must fail again if the buyer-side check ever comes back.
    """
    c = env["client"]
    seller = env["make"]("bh_s", stones=100000)
    buyer = env["make"]("bh_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("bh1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bh2")})
    assert r.status_code == 200, (
        f"a buyer with 100000 stones and no copy was refused the buyout: {r.text}")
    assert env["inv"].get_item_count(buyer, "talisman_return") == 1


def test_bid_response_declares_it_does_not_auto_win(env):
    c = env["client"]
    seller = env["make"]("bid_seller", stones=100000)
    bidder = env["make"]("bid_bidder", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500},
               headers={**_tok(seller), **_idem("bd1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("bd2")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "listing_id", "current_bid", "bidder_id",
                         "auto_wins", "expires_at"}
    assert body["auto_wins"] is False
    assert body["current_bid"] == 600 and body["bidder_id"] == bidder


def test_buyout_response_reports_the_tax_and_where_it_went(env):
    c = env["client"]
    seller = env["make"]("bo_seller", stones=100000)
    buyer = env["make"]("bo_buyer", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("bo1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bo2")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "listing_id", "trade_id", "state", "price_stones",
                         "tax_stones", "seller_net_stones", "tax_destination", "flag"}
    assert body["state"] == "sold"
    assert body["tax_stones"] == trading_market.sale_tax_stones(900)
    assert body["seller_net_stones"] == 900 - body["tax_stones"]
    assert body["tax_destination"] in ("sink", "sect")


# --------------------------------------------------------------------------
# Error cases, by contract
# --------------------------------------------------------------------------
def test_unknown_player_is_404(env):
    c = env["client"]
    pid = env["make"]("e404")
    r = c.post("/trade/cancel", json={"player_id": pid, "trade_id": "nope"},
               headers=_tok(pid))
    assert r.status_code == 404, r.text


def test_unknown_trade_id_is_404_on_every_id_route(env):
    c = env["client"]
    pid = env["make"]("e404b", stones=100000)
    for path in ("/trade/accept", "/trade/cancel", "/trade/mail/accept"):
        headers = {**_tok(pid), **_idem(f"x{path}")}
        r = c.post(path, json={"player_id": pid, "trade_id": "trade_does_not_exist"},
                   headers=headers)
        assert r.status_code == 404, f"{path} answered {r.status_code}: {r.text}"


def test_unknown_listing_id_is_404(env):
    c = env["client"]
    pid = env["make"]("e404c", stones=100000)
    for body, path in (({"listing_id": "l", "amount": 500}, "/auction/bid"),
                       ({"listing_id": "l"}, "/auction/buyout")):
        r = c.post(path, json={"player_id": pid, **body},
                   headers={**_tok(pid), **_idem(f"y{path}")})
        assert r.status_code == 404, f"{path} answered {r.status_code}: {r.text}"


def test_self_trade_and_self_mail_are_400(env):
    c = env["client"]
    pid = env["make"]("self", stones=100000)
    env["inv"].add_item(pid, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": pid, "counterparty_id": pid,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}]},
               headers={**_tok(pid), **_idem("s1")})
    assert r.status_code == 400, r.text

    r = c.post("/trade/mail",
               json={"player_id": pid, "recipient_id": pid,
                     "items": [{"item_id": "talisman_return", "quantity": 1}]},
               headers={**_tok(pid), **_idem("s2")})
    assert r.status_code == 400, r.text


def test_an_empty_offer_is_400(env):
    c = env["client"]
    seller = env["make"]("empty_s", stones=100000)
    buyer = env["make"]("empty_b", stones=100000)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer},
               headers={**_tok(seller), **_idem("e1")})
    assert r.status_code == 400, r.text

    r = c.post("/trade/mail", json={"player_id": seller, "recipient_id": buyer},
               headers={**_tok(seller), **_idem("e2")})
    assert r.status_code == 400, r.text


def test_unknown_item_is_400_not_500(env):
    c = env["client"]
    seller = env["make"]("ui", stones=100000)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "no_such_item",
                     "quantity": 1, "starting_price": 500},
               headers={**_tok(seller), **_idem("ui1")})
    assert r.status_code == 400, f"unknown item answered {r.status_code}: {r.text}"


def test_bad_quantity_inside_an_item_line_is_422(env):
    """A zero or negative quantity is refused, not clamped."""
    c = env["client"]
    seller = env["make"]("bq", stones=100000)
    buyer = env["make"]("bq2", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    for bad in (0, -1):
        r = c.post("/trade/request",
                   json={"player_id": seller, "counterparty_id": buyer,
                         "offered_items": [{"item_id": "talisman_return",
                                            "quantity": bad}]},
                   headers={**_tok(seller), **_idem(f"bq{bad}")})
        assert r.status_code == 422, f"quantity={bad} answered {r.text}"


def test_stone_amounts_out_of_range_are_422(env):
    c = env["client"]
    seller = env["make"]("sr", stones=100000)
    buyer = env["make"]("sr2", stones=100000)
    for bad in (-1, 10_000_001):
        r = c.post("/trade/request",
                   json={"player_id": seller, "counterparty_id": buyer,
                         "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                         "requested_stones": bad},
                   headers={**_tok(seller), **_idem(f"sr{bad}")})
        assert r.status_code == 422, f"requested_stones={bad} answered {r.text}"


def test_listing_quantity_out_of_range_is_422(env):
    c = env["client"]
    seller = env["make"]("lq", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 5)
    for bad in (0, 1000):
        r = c.post("/auction/list",
                   json={"player_id": seller, "item_id": "talisman_return",
                         "quantity": bad, "starting_price": 500},
                   headers={**_tok(seller), **_idem(f"lq{bad}")})
        assert r.status_code == 422, f"quantity={bad} answered {r.text}"


def test_price_outside_the_bounds_is_400(env):
    c = env["client"]
    seller = env["make"]("pb", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    for bad in (1, 100_000_000):
        r = c.post("/auction/list",
                   json={"player_id": seller, "item_id": "talisman_return",
                         "quantity": 1, "starting_price": bad},
                   headers={**_tok(seller), **_idem(f"pb{bad}")})
        assert r.status_code == 400, f"starting_price={bad} answered {r.text}"


def test_only_the_named_counterparty_may_accept_a_direct_trade(env):
    c = env["client"]
    seller = env["make"]("np_s", stones=0)
    buyer = env["make"]("np_b", stones=100000)
    stranger = env["make"]("np_x", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("np1")})
    tid = r.json()["trade_id"]
    r = c.post("/trade/accept", json={"player_id": stranger, "trade_id": tid},
               headers={**_tok(stranger), **_idem("np2")})
    assert r.status_code == 403, f"a third party accepted the trade: {r.text}"
    assert env["inv"].get_item_count(stranger, "talisman_return") == 0


def test_only_the_named_recipient_may_claim_mail(env):
    c = env["client"]
    sender = env["make"]("mr_s", stones=0)
    recipient = env["make"]("mr_r", stones=100000)
    stranger = env["make"]("mr_x", stones=100000)
    env["inv"].add_item(sender, "talisman_return", 1)
    r = c.post("/trade/mail",
               json={"player_id": sender, "recipient_id": recipient,
                     "items": [{"item_id": "talisman_return", "quantity": 1}],
                     "price_stones": 500},
               headers={**_tok(sender), **_idem("mr1")})
    tid = r.json()["trade_id"]
    r = c.post("/trade/mail/accept", json={"player_id": stranger, "trade_id": tid},
               headers={**_tok(stranger), **_idem("mr2")})
    assert r.status_code == 403, f"a third party claimed the mail: {r.text}"
    assert env["inv"].get_item_count(stranger, "talisman_return") == 0


def test_only_a_party_may_cancel_a_direct_trade(env):
    c = env["client"]
    seller = env["make"]("cn_s", stones=0)
    buyer = env["make"]("cn_b", stones=100000)
    stranger = env["make"]("cn_x", stones=0)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}]},
               headers={**_tok(seller), **_idem("cn1")})
    tid = r.json()["trade_id"]
    r = c.post("/trade/cancel", json={"player_id": stranger, "trade_id": tid},
               headers=_tok(stranger))
    assert r.status_code == 403, r.text
    # The named counterparty may.
    r = c.post("/trade/cancel", json={"player_id": buyer, "trade_id": tid},
               headers=_tok(buyer))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "trade_id": tid, "state": "cancelled"}


def test_trade_id_body_answered_on_the_wrong_method_is_400(env):
    """A direct trade id handed to the mail claim is refused by method."""
    c = env["client"]
    seller = env["make"]("wm_s", stones=0)
    buyer = env["make"]("wm_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("wm1")})
    tid = r.json()["trade_id"]
    r = c.post("/trade/mail/accept", json={"player_id": buyer, "trade_id": tid},
               headers={**_tok(buyer), **_idem("wm2")})
    assert r.status_code == 400, f"a direct trade id was claimable as mail: {r.text}"


def test_expired_direct_offer_is_410(env):
    c = env["client"]
    seller = env["make"]("ex_s", stones=0)
    buyer = env["make"]("ex_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": "talisman_return", "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("ex1")})
    tid = r.json()["trade_id"]
    # Pull the clock forward past the 60 second lifetime.
    store = env["store"]
    with store._lock:
        store.offers[tid]["expires_at"] = 1.0
    r = c.post("/trade/accept", json={"player_id": buyer, "trade_id": tid},
               headers={**_tok(buyer), **_idem("ex2")})
    assert r.status_code == 410, f"an expired offer answered {r.text}"
    assert env["inv"].get_item_count(buyer, "talisman_return") == 0


def test_expired_listing_is_410(env):
    c = env["client"]
    seller = env["make"]("el_s", stones=100000)
    bidder = env["make"]("el_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500},
               headers={**_tok(seller), **_idem("el1")})
    lid = r.json()["listing"]["listing_id"]
    with env["store"]._lock:
        env["store"].listings[lid]["expires_at"] = 1.0
    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("el2")})
    assert r.status_code == 410, f"an expired listing answered {r.text}"


def test_cannot_bid_or_buy_your_own_listing(env):
    c = env["client"]
    seller = env["make"]("own_s", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("ow1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/bid",
               json={"player_id": seller, "listing_id": lid, "amount": 600},
               headers={**_tok(seller), **_idem("ow2")})
    assert r.status_code == 400, r.text
    r = c.post("/auction/buyout", json={"player_id": seller, "listing_id": lid},
               headers={**_tok(seller), **_idem("ow3")})
    assert r.status_code == 400, r.text


def test_buyout_without_a_buyout_price_is_409(env):
    c = env["client"]
    seller = env["make"]("nb_s", stones=100000)
    buyer = env["make"]("nb_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500},
               headers={**_tok(seller), **_idem("nb1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("nb2")})
    assert r.status_code == 409, r.text


def test_a_bid_must_beat_the_current_bid_and_stay_below_the_buyout(env):
    c = env["client"]
    seller = env["make"]("bb_s", stones=100000)
    first = env["make"]("bb_1", stones=100000)
    second = env["make"]("bb_2", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 700},
               headers={**_tok(seller), **_idem("bb1")})
    lid = r.json()["listing"]["listing_id"]

    r = c.post("/auction/bid",
               json={"player_id": first, "listing_id": lid, "amount": 600},
               headers={**_tok(first), **_idem("bb2")})
    assert r.status_code == 200, r.text

    r = c.post("/auction/bid",
               json={"player_id": second, "listing_id": lid, "amount": 600},
               headers={**_tok(second), **_idem("bb3")})
    assert r.status_code == 409, f"an equal bid was accepted: {r.text}"

    r = c.post("/auction/bid",
               json={"player_id": second, "listing_id": lid, "amount": 700},
               headers={**_tok(second), **_idem("bb4")})
    assert r.status_code == 409, f"a bid at the buyout price was accepted: {r.text}"


def test_a_bidder_without_the_stones_is_409(env):
    c = env["client"]
    seller = env["make"]("bs_s", stones=100000)
    bidder = env["make"]("bs_b", stones=1)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500},
               headers={**_tok(seller), **_idem("bs1")})
    lid = r.json()["listing"]["listing_id"]
    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 5000},
               headers={**_tok(bidder), **_idem("bs2")})
    assert r.status_code == 409, r.text


def test_a_seller_who_does_not_hold_the_goods_is_409(env):
    c = env["client"]
    seller = env["make"]("nh_s", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 5, "starting_price": 500},
               headers={**_tok(seller), **_idem("nh1")})
    assert r.status_code == 409, f"a seller listed goods it lacks: {r.text}"


def test_listing_without_a_sect_is_409(env):
    c = env["client"]
    seller = env["make"]("ns", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/market/sect/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "buyout_price": 900},
               headers={**_tok(seller), **_idem("ns1")})
    assert r.status_code == 409, r.text


def test_a_refused_attempt_burns_its_key_and_the_replay_is_409(env):
    """A key spent by a refused attempt must not replay as a bare success."""
    c = env["client"]
    seller = env["make"]("burn", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    body = {"player_id": seller, "item_id": "talisman_return",
            "quantity": 1, "buyout_price": 900}
    headers = {**_tok(seller), **_idem("burn-key")}
    first = c.post("/market/sect/list", json=body, headers=headers)
    assert first.status_code == 409, f"the first attempt should be refused: {first.text}"
    second = c.post("/market/sect/list", json=body, headers=headers)
    assert second.status_code == 409, (
        f"a replay of a refused attempt answered {second.status_code}: {second.text}")
    assert env["inv"].get_item_count(seller, "talisman_return") == 1, (
        "the goods left the seller on a refused attempt")


def test_a_settled_replay_returns_the_first_reply(env):
    """A key replayed after success returns the stored answer, not a 409."""
    c = env["client"]
    seller = env["make"]("rep_s", stones=100000)
    buyer = env["make"]("rep_b", stones=100000)
    env["inv"].add_item(seller, "talisman_return", 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": "talisman_return",
                     "quantity": 1, "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("rp1")})
    lid = r.json()["listing"]["listing_id"]
    body = {"player_id": buyer, "listing_id": lid}
    headers = {**_tok(buyer), **_idem("rp2")}
    first = c.post("/auction/buyout", json=body, headers=headers)
    assert first.status_code == 200, first.text
    second = c.post("/auction/buyout", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == first.json(), "the replay did not return the first reply"


# --------------------------------------------------------------------------
# §5.4 THE BUYER DOES NOT ALREADY HOLD THE GOODS.
#
# The seller-side rule in _check_tradable is "you must already own `quantity`
# of this item". Applied to a buyer that rule is backwards: an auction sells
# the SELLER's copy to the bidder, so demanding the bidder already hold one
# meant the house could only sell to somebody who had no reason to buy, and
# every buyout and every bid was refused with HTTP 409
# "Does not hold 1 x talisman_return (has 0)".
#
# The fix is require_holding=False at the two buyer call sites, which keeps
# every OTHER restriction (bound / quest / no-valuation / realm) in force.
#
# These tests pin BOTH halves of that claim, because either half alone is
# satisfiable by accident:
#   * the positive arms  -- a buyer holding none of the item is served;
#   * the negative arms  -- the guards that must SURVIVE the relaxation
#                           (realm gate, bound item, seller-side holding)
#                           still refuse. A blanket "skip _check_tradable"
#                           fix would pass the positive arms and fail these.
# --------------------------------------------------------------------------

#: An item with a shop price in spirit stones and no realm gate above `mortal`,
#: so a fresh account can list it and buy it. talisman_return: uncommon,
#: 500 stones, required_realm "mortal".
TRADEABLE = "talisman_return"
#: The same item one realm up: uncommon, 500 stones, required_realm
#: "qi_refining". A `mortal` account must still be refused it, which is the
#: guard that proves the relaxation is narrow.
REALM_GATED = "cultivation_pill_med"
#: For the multi-unit arm. talisman_return has a stack ceiling of 3, so a
#: five-unit listing of it cannot exist at all -- measured from
#: inventory.ITEM_DEFINITIONS, not assumed. cultivation_pill_low: common,
#: 100 stones, stack 99, required_realm "mortal".
MULTI = "cultivation_pill_low"


def _list_for_auction(c, env, seller, *, item=TRADEABLE, quantity=1,
                      starting_price=500, buyout_price=None, key="l"):
    """Put one item on the auction house as ``seller`` and return the listing id.

    The seller is credited generously so the list fee never confuses a test
    about the buyer's holding rule with a test about the seller's balance.
    """
    body = {"player_id": seller, "item_id": item, "quantity": quantity,
            "starting_price": starting_price}
    if buyout_price is not None:
        body["buyout_price"] = buyout_price
    r = c.post("/auction/list", json=body,
               headers={**_tok(seller), **_idem(f"{key}-list")})
    assert r.status_code == 200, f"the seller could not list: {r.text}"
    return r.json()["listing"]["listing_id"]


def test_buyout_serves_a_buyer_who_holds_none_of_the_item(env):
    """The regression itself: the buyer holds zero and is still served.

    Before the fix this answered 409 "Does not hold 1 x talisman_return
    (has 0)" and the auction house could not sell anything to anybody who
    did not already own the goods.
    """
    c = env["client"]
    seller = env["make"]("bh_s", stones=100000)
    buyer = env["make"]("bh_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="bh")

    assert env["inv"].get_item_count(buyer, TRADEABLE) == 0, (
        "the fixture is wrong: the buyer already holds the item, so this "
        "test cannot tell a served buyer from a refused one")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bh-buy")})
    assert r.status_code == 200, f"a buyer holding none was refused: {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "sold"

    # The goods actually moved. A 200 with an empty bag would be the same
    # class of defect one layer down.
    assert env["inv"].get_item_count(buyer, TRADEABLE) == 1, (
        "the buyout answered 200 but the buyer did not receive the item")
    assert env["inv"].get_item_count(seller, TRADEABLE) == 0, (
        "the escrow copy came back to the seller")


def test_buyout_of_a_multi_unit_listing_serves_a_buyer_who_holds_none(env):
    """The holding rule was quantity-relative, so prove it on quantity > 1.

    A fix that only relaxed the rule for a single unit would pass the
    one-unit test above and still refuse this one.
    """
    c = env["client"]
    seller = env["make"]("bm_s", stones=100000)
    buyer = env["make"]("bm_b", stones=100000)
    env["inv"].add_item(seller, MULTI, 5)
    assert env["inv"].get_item_count(seller, MULTI) == 5, (
        "the fixture could not stock five units, so this test would prove "
        "nothing about quantity")
    lid = _list_for_auction(c, env, seller, item=MULTI, quantity=5,
                            starting_price=250, buyout_price=450, key="bm")

    assert env["inv"].get_item_count(buyer, MULTI) == 0
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bm-buy")})
    assert r.status_code == 200, f"a buyer holding none of 5 was refused: {r.text}"
    assert env["inv"].get_item_count(buyer, MULTI) == 5, (
        "the buyer did not receive the whole lot")


def test_a_bid_is_accepted_from_a_bidder_who_holds_none_of_the_item(env):
    """The bid arm of the same rule, evaluated before any money moves.

    A bid never auto-wins, so this arm has no settlement to observe; the
    observable is the recorded bid itself.
    """
    c = env["client"]
    seller = env["make"]("bd_s", stones=100000)
    bidder = env["make"]("bd_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="bd")

    assert env["inv"].get_item_count(bidder, TRADEABLE) == 0, (
        "the fixture is wrong: the bidder already holds the item")

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("bd-bid")})
    assert r.status_code == 200, f"a bidder holding none was refused: {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert body["current_bid"] == 600
    assert body["bidder_id"] == bidder
    assert body["auto_wins"] is False, "a bid must never settle on its own"

    # The bid is durable, not just echoed.
    with env["store"]._lock:
        row = env["store"].listings[lid]
    assert int(row["current_bid"]) == 600
    assert row["bidder_id"] == bidder
    assert row["state"] == "held", "a bid must leave the listing open"


def test_a_second_bid_from_another_non_holder_is_accepted(env):
    """Two buyers who both hold none: the rule cannot be a one-buyer quirk."""
    c = env["client"]
    seller = env["make"]("b2_s", stones=100000)
    first = env["make"]("b2_1", stones=100000)
    second = env["make"]("b2_2", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="b2")

    r = c.post("/auction/bid",
               json={"player_id": first, "listing_id": lid, "amount": 600},
               headers={**_tok(first), **_idem("b2-b1")})
    assert r.status_code == 200, f"first non-holder was refused: {r.text}"
    r = c.post("/auction/bid",
               json={"player_id": second, "listing_id": lid, "amount": 700},
               headers={**_tok(second), **_idem("b2-b2")})
    assert r.status_code == 200, f"second non-holder was refused: {r.text}"
    assert r.json()["current_bid"] == 700


def test_the_relaxation_did_not_open_the_realm_gate_to_buyers(env):
    """NEGATIVE ARM. require_holding=False must not relax the realm gate.

    cultivation_pill_med needs `qi_refining`; both players here are `mortal`
    (the default on a fresh account). The seller must be raised to list it,
    and then the low-realm BUYER must still be refused.
    """
    c = env["client"]
    seller = env["make"]("rg_s", stones=100000)
    buyer = env["make"]("rg_b", stones=100000)
    with env["players"].transaction(seller):
        env["players"][seller]["cultivation_realm"] = "qi_refining"
    env["inv"].add_item(seller, REALM_GATED, 1)
    lid = _list_for_auction(c, env, seller, item=REALM_GATED,
                            starting_price=500, buyout_price=900, key="rg")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("rg-buy")})
    assert r.status_code == 403, (
        f"a buyer below the realm gate was served: {r.text}")
    assert "qi_refining" in r.text, (
        f"the refusal was not the realm gate: {r.text}")
    assert env["inv"].get_item_count(buyer, REALM_GATED) == 0, (
        "a refused buyer still received the goods")


def test_the_relaxation_did_not_open_the_realm_gate_to_bidders(env):
    """NEGATIVE ARM, bid side. Same gate, the other buyer call site.

    Without this, a fix that only touched /auction/buyout would pass every
    test above while the bid arm stayed wide open.
    """
    c = env["client"]
    seller = env["make"]("rqb_s", stones=100000)
    bidder = env["make"]("rqb_b", stones=100000)
    with env["players"].transaction(seller):
        env["players"][seller]["cultivation_realm"] = "qi_refining"
    env["inv"].add_item(seller, REALM_GATED, 1)
    lid = _list_for_auction(c, env, seller, item=REALM_GATED,
                            starting_price=500, buyout_price=900, key="rqb")

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("rqb-bid")})
    assert r.status_code == 403, (
        f"a bidder below the realm gate was served: {r.text}")
    assert "qi_refining" in r.text, (
        f"the refusal was not the realm gate: {r.text}")
    with env["store"]._lock:
        assert env["store"].listings[lid]["bidder_id"] is None, (
            "a refused bidder was recorded on the listing")


def test_a_bound_item_is_still_refused_to_a_buyer_who_holds_none(env):
    """NEGATIVE ARM. The bound-item gate survives the relaxation.

    The buyer's own `bound_items` list names the item, which is one of the
    two sources `is_bound_item` reads. A blanket skip of _check_tradable
    would serve this buyer.
    """
    c = env["client"]
    seller = env["make"]("bd2_s", stones=100000)
    buyer = env["make"]("bd2_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="bd2")
    with env["players"].transaction(buyer):
        env["players"][buyer]["bound_items"] = [TRADEABLE]

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bd2-buy")})
    assert r.status_code == 403, f"a bound item was sold anyway: {r.text}"
    assert "Bound item" in r.text, f"the refusal was not the bound rule: {r.text}"


# --------------------------------------------------------------------------
# §5.4 THE QUEST AND NO-VALUATION GATES, BUYER SIDE.
#
# The bound arm above needed only one trick: the buyer holds the item in
# `bound_items`. The two gates here cannot be reached that way, and both
# facts below were MEASURED against the tree, not assumed:
#
#   * The catalogue holds ZERO quest items today, so there is no real quest
#     item to trade. The gate is proved on a row injected into
#     ITEM_DEFINITIONS and removed again by the monkeypatch.
#   * /auction/list refuses BOTH kinds before a listing exists ("Quest item
#     cannot be traded" / "Item has no market valuation"), so the buyer arm
#     cannot be reached by listing normally. _seed_listing mints the row the
#     way _create_listing does, minus the seller-side gate. Without that
#     shortcut these tests would be testing the SELLER rule a second time.
#
# Each arm then names WHICH guard refuses, because the two are not the same
# guard and one of them is not _check_tradable at all:
#
#   * quest  -> _check_tradable's is_quest_item branch.
#   * unvalued -> _check_price_bounds, NOT _check_tradable. Both buyer call
#     sites pass allow_unpriced=True, so the valuation branch inside
#     _check_tradable is deliberately skipped for them and the price-bounds
#     check is what refuses. Proved by control further down: disabling
#     _check_tradable leaves the unvalued 403 standing, and disabling
#     _check_price_bounds turns it into a 200.
# --------------------------------------------------------------------------

#: A quest item that does not exist in the shipped catalogue. Injected only
#: for the duration of the tests that name it.
QUEST_ITEM = "quest_ashen_seal"
#: A real catalogue item whose shop price is 0 (drop-only crafting input), so
#: item_value_stones returns None and price_bounds returns no_valuation.
#: measured: beast_bone, common, no required_realm, price amount 0.
UNVALUED = "beast_bone"


def _seed_listing(env, seller, item_id, *, quantity=1, buyout_price=900,
                  board="auction"):
    """Mint an open listing for `item_id` without running the seller gate.

    Both item kinds under test are refused by /auction/list, so the only way
    to reach the BUYER call site is to open the listing directly. This writes
    the same offer + listing pair _create_listing writes, so the buyer path
    sees a normally-shaped row (state held, trade_id present, unexpired).
    """
    store, players = env["store"], env["players"]
    now = time.time()
    expires = now + trading_market.MAX_LISTING_SECONDS
    lid = store._mint("listing", seller, item_id, board)
    tid = store._mint("trade", seller, board)
    store.create_offer(players, {
        "trade_id": tid, "method": board, "initiator_id": seller,
        "counterparty_id": None, "state": "accepted",
        "offered_items": [{"item_id": item_id, "quantity": quantity}],
        "requested_items": [], "offered_stones": 0, "requested_stones": 0,
        "created_at": now, "expires_at": expires,
    })
    store.create_listing(players, {
        "listing_id": lid, "board": board, "seller_id": seller, "sect_id": None,
        "item_id": item_id, "quantity": quantity, "state": "held",
        "buyout_price": buyout_price, "current_bid": 0, "bidder_id": None,
        "expires_at": expires, "auto_relist_used": False, "created_at": now,
        "trade_id": tid,
    })
    return lid


def _quest_row() -> dict:
    """A catalogue row that is_quest_item accepts, with a real price.

    Priced and realm-free on purpose: the ONLY thing that may refuse this
    item is the quest rule, so a test failure cannot be blamed on the realm
    or valuation gates.
    """
    return {"rarity": "uncommon", "quest_item": True,
            "price": {"currency": "spirit_stone", "amount": 500},
            "required_realm": "mortal"}


def test_the_catalogue_ships_no_quest_item_so_the_gate_is_unreachable_by_default(env):
    """Why the quest rows below inject an item instead of naming one.

    Zero of the catalogue's items satisfy is_quest_item today. Without this,
    a reader could assume the gate is dead code and drop the arms.
    """
    catalogue = [i for i in trading_market.ITEM_DEFINITIONS
                 if trading_market.is_quest_item(i)]
    assert catalogue == [], (
        "the catalogue now ships quest items, so the injection-based arms "
        f"below should name a real one instead: {catalogue}")


def test_the_seller_cannot_list_a_quest_item_which_forces_the_seeded_fixture(env, monkeypatch):
    """Why the buyer arm needs a seeded listing rather than a real one."""
    c = env["client"]
    seller = env["make"]("qsl_s", stones=100000)
    monkeypatch.setitem(trading_market.ITEM_DEFINITIONS, QUEST_ITEM, _quest_row())
    env["inv"].add_item(seller, QUEST_ITEM, 1)
    assert env["inv"].get_item_count(seller, QUEST_ITEM) == 1, (
        "the fixture could not stock the item, so this arm would be vacuous")

    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": QUEST_ITEM, "quantity": 1,
                     "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("qsl-list")})
    assert r.status_code == 403, (
        f"a quest item became listable, so the seeded fixture below is "
        f"unnecessary and the arms would be measuring the wrong layer: {r.text}")


def test_a_quest_item_is_still_refused_to_a_buyer_who_holds_none(env, monkeypatch):
    """NEGATIVE ARM, quest rule, buyout side.

    Mirrors the bound arm above: the buyer holds none of the item, so the
    holding rule cannot be what refuses, and require_holding=False cannot be
    what is being tested.
    """
    c = env["client"]
    seller = env["make"]("q_b", stones=100000)
    buyer = env["make"]("q_bb", stones=100000)
    monkeypatch.setitem(trading_market.ITEM_DEFINITIONS, QUEST_ITEM, _quest_row())
    env["inv"].add_item(seller, QUEST_ITEM, 1)
    lid = _seed_listing(env, seller, QUEST_ITEM)

    assert env["inv"].get_item_count(buyer, QUEST_ITEM) == 0, (
        "the fixture is wrong: the buyer already holds the item")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("q-buy")})
    assert r.status_code == 403, f"a quest item was sold anyway: {r.text}"
    assert "Quest item" in r.text, f"the refusal was not the quest rule: {r.text}"
    assert env["inv"].get_item_count(buyer, QUEST_ITEM) == 0, (
        "a refused buyer still received the goods")
    with env["store"]._lock:
        assert env["store"].listings[lid]["state"] == "held", (
            "a refused buyout closed the listing")


def test_a_quest_item_is_still_refused_to_a_bidder(env, monkeypatch):
    """NEGATIVE ARM, quest rule, bid side.

    Without this a fix that only touched /auction/buyout would pass every
    quest test while the bid arm stayed wide open.
    """
    c = env["client"]
    seller = env["make"]("q_bd", stones=100000)
    bidder = env["make"]("q_bdb", stones=100000)
    monkeypatch.setitem(trading_market.ITEM_DEFINITIONS, QUEST_ITEM, _quest_row())
    env["inv"].add_item(seller, QUEST_ITEM, 1)
    lid = _seed_listing(env, seller, QUEST_ITEM, buyout_price=None)

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("q-bid")})
    assert r.status_code == 403, f"a quest item took a bid anyway: {r.text}"
    assert "Quest item" in r.text, f"the refusal was not the quest rule: {r.text}"
    with env["store"]._lock:
        assert env["store"].listings[lid]["bidder_id"] is None, (
            "a refused bidder was recorded on the listing")
        assert env["store"].listings[lid]["current_bid"] == 0


def test_the_catalogue_ships_unvalued_items_which_are_the_real_fixtures(env):
    """Why UNVALUED names a real item while the quest arms inject one."""
    assert trading_market.item_value_stones(UNVALUED) is None, (
        f"{UNVALUED} now has a shop price, so it no longer tests the "
        "no-valuation gate")
    assert trading_market.is_quest_item(UNVALUED) is False, (
        f"{UNVALUED} is now a quest item, so this arm would be measuring the "
        "quest rule instead")


def test_an_unvalued_item_is_still_refused_to_a_buyer_who_holds_none(env):
    """NEGATIVE ARM, no-valuation rule, buyout side.

    The refusal here comes from _check_price_bounds, not _check_tradable:
    both buyer call sites pass allow_unpriced=True, so the valuation branch
    inside _check_tradable is skipped for them. The message is asserted so a
    later change that starts refusing at a different layer is visible.
    """
    c = env["client"]
    seller = env["make"]("u_b", stones=100000)
    buyer = env["make"]("u_bb", stones=100000)
    env["inv"].add_item(seller, UNVALUED, 1)
    lid = _seed_listing(env, seller, UNVALUED)

    assert env["inv"].get_item_count(buyer, UNVALUED) == 0, (
        "the fixture is wrong: the buyer already holds the item")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("u-buy")})
    assert r.status_code == 403, f"an unvalued item was sold anyway: {r.text}"
    assert "no market valuation" in r.text, (
        f"the refusal was not the no-valuation rule: {r.text}")
    assert env["inv"].get_item_count(buyer, UNVALUED) == 0, (
        "a refused buyer still received the goods")
    with env["store"]._lock:
        assert env["store"].listings[lid]["state"] == "held", (
            "a refused buyout closed the listing")


def test_an_unvalued_item_is_still_refused_to_a_bidder(env):
    """NEGATIVE ARM, no-valuation rule, bid side."""
    c = env["client"]
    seller = env["make"]("u_bd", stones=100000)
    bidder = env["make"]("u_bdb", stones=100000)
    env["inv"].add_item(seller, UNVALUED, 1)
    lid = _seed_listing(env, seller, UNVALUED, buyout_price=None)

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("u-bid")})
    assert r.status_code == 403, f"an unvalued item took a bid anyway: {r.text}"
    assert "no market valuation" in r.text, (
        f"the refusal was not the no-valuation rule: {r.text}")
    with env["store"]._lock:
        assert env["store"].listings[lid]["bidder_id"] is None, (
            "a refused bidder was recorded on the listing")


def test_the_seller_cannot_list_an_unvalued_item_which_forces_the_seeded_fixture(env):
    """Why the unvalued buyer arm also needs a seeded listing."""
    c = env["client"]
    seller = env["make"]("u_sl", stones=100000)
    env["inv"].add_item(seller, UNVALUED, 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": UNVALUED, "quantity": 1,
                     "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("u-sl-list")})
    assert r.status_code == 403, (
        f"an unvalued item became listable, so the seeded fixture below is "
        f"unnecessary and the arms would measure the wrong layer: {r.text}")


# -- controls: name the guard, do not assume it ---------------------------
def test_control_the_quest_arms_are_refused_by_check_tradable(env, monkeypatch):
    """The quest 403 must DISAPPEAR when _check_tradable is taken away.

    If it survives, the refusal is coming from some other rule and every
    quest assertion above is measuring the wrong layer.
    """
    c = env["client"]
    seller = env["make"]("cq_s", stones=100000)
    buyer = env["make"]("cq_b", stones=100000)
    monkeypatch.setitem(trading_market.ITEM_DEFINITIONS, QUEST_ITEM, _quest_row())
    env["inv"].add_item(seller, QUEST_ITEM, 1)
    lid = _seed_listing(env, seller, QUEST_ITEM)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cq-before")})
    assert r.status_code == 403, (
        f"the un-sabotaged buyout answered {r.status_code}: {r.text}")

    monkeypatch.setattr(trading_market, "_check_tradable", lambda *a, **k: None)
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cq-after")})
    assert r.status_code == 200, (
        f"with _check_tradable disabled a quest item was still refused "
        f"({r.status_code} {r.text}). Something OTHER than _check_tradable is "
        "answering 403, so the quest arms are measuring the wrong rule.")


def test_control_the_unvalued_arms_survive_check_tradable_and_name_price_bounds(env, monkeypatch):
    """The unvalued 403 is NOT _check_tradable's valuation branch.

    Both buyer call sites pass allow_unpriced=True, so disabling
    _check_tradable must leave the refusal standing -- and disabling
    _check_price_bounds must remove it. Two controls, because either alone
    is satisfiable by accident.
    """
    c = env["client"]
    seller = env["make"]("cu_s", stones=100000)
    buyer = env["make"]("cu_b", stones=100000)
    env["inv"].add_item(seller, UNVALUED, 1)
    lid = _seed_listing(env, seller, UNVALUED)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cu-before")})
    assert r.status_code == 403, f"the un-sabotaged buyout answered {r.status_code}: {r.text}"

    # Control A: _check_tradable is NOT what refuses. The 403 must stand.
    monkeypatch.setattr(trading_market, "_check_tradable", lambda *a, **k: None)
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cu-after-tradable")})
    assert r.status_code == 403, (
        f"with _check_tradable disabled an unvalued item was served "
        f"({r.status_code} {r.text}). The buyer path is NOT passing "
        "allow_unpriced=True, so the arms above are crediting the wrong guard.")

    # Control B: _check_price_bounds IS what refuses. The 403 must go.
    monkeypatch.setattr(trading_market, "_check_price_bounds", lambda *a, **k: {})
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cu-after-bounds")})
    assert r.status_code == 200, (
        f"with _check_price_bounds disabled an unvalued item was still refused "
        f"({r.status_code} {r.text}). Something OTHER than _check_price_bounds "
        "is answering 403, so the unvalued arms are measuring the wrong rule.")


def test_the_seller_side_holding_rule_is_still_in_force(env):
    """NEGATIVE ARM. Only the BUYER arm was relaxed.

    A seller listing goods it does not hold must still be refused, so the
    escrow cannot be opened out of thin air. This is the arm that catches a
    fix which flipped the default rather than passing the keyword at the two
    buyer call sites.
    """
    c = env["client"]
    seller = env["make"]("sh_s", stones=100000)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": TRADEABLE, "quantity": 1,
                     "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("sh-list")})
    assert r.status_code == 409, f"a seller listed goods it lacks: {r.text}"
    assert "Does not hold" in r.text, (
        f"the refusal was not the holding rule: {r.text}")


def test_a_non_holding_seller_is_still_refused_on_the_sect_board(env):
    """NEGATIVE ARM, sibling board. The relaxation did not leak to /market/sect.

    The sect board lists through the same shared function and reaches the
    same buyer/settle path, so it is the most likely place for an over-broad
    fix to show up.
    """
    c = env["client"]
    seller = env["make"]("sb2_s", stones=100000)
    with env["players"].transaction(seller):
        env["players"][seller]["sect_id"] = "sect-9"
    r = c.post("/market/sect/list",
               json={"player_id": seller, "item_id": TRADEABLE, "quantity": 1,
                     "buyout_price": 900},
               headers={**_tok(seller), **_idem("sb2-list")})
    assert r.status_code == 409, f"a non-holding seller listed on sect: {r.text}"
    assert "Does not hold" in r.text, (
        f"the refusal was not the holding rule: {r.text}")


def test_a_buyer_without_the_stones_is_still_refused_at_buyout(env):
    """NEGATIVE ARM. Relaxing the holding rule did not relax the payment.

    Same buyer as the happy path -- holds none of the item -- but cannot
    pay. The 409 must be about the stones, so the two rules stay distinct.
    """
    c = env["client"]
    seller = env["make"]("bp_s", stones=100000)
    buyer = env["make"]("bp_b", stones=1)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="bp")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("bp-buy")})
    assert r.status_code == 409, f"a buyer who cannot pay was served: {r.text}"
    assert "stones" in r.text.lower(), (
        f"the refusal was not about payment: {r.text}")
    assert env["inv"].get_item_count(buyer, TRADEABLE) == 0, (
        "a refused buyer received the goods anyway")
    # The listing is untouched, so the seller can still sell it.
    with env["store"]._lock:
        assert env["store"].listings[lid]["state"] == "held", (
            "a refused buyout closed the listing")


def test_the_buyer_pays_the_buyout_price_and_the_seller_receives_the_net(env):
    """The served buyout moves money correctly, not just goods.

    Closes the loop on the happy path: a relaxation that let a buyer take
    the item for free would pass every status-code assertion above.
    """
    c = env["client"]
    seller = env["make"]("mv_s", stones=100000)
    buyer = env["make"]("mv_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="mv")

    def stones_of(pid):
        with env["players"].transaction(pid):
            return int(env["players"][pid]["spirit_stones"]["low_grade"])

    seller_before = stones_of(seller)
    buyer_before = stones_of(buyer)
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("mv-buy")})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["price_stones"] == 900
    assert stones_of(buyer) == buyer_before - 900, (
        "the buyer was not charged the buyout price")
    # §5.2 the 5% sale tax is withheld, so the seller nets the rest.
    assert body["tax_stones"] == 45, f"the 5% sale tax is wrong: {body}"
    assert stones_of(seller) == seller_before + body["seller_net_stones"], (
        "the seller was not credited the net price")


# ===========================================================================
# SECTION 2 -- MOUNTED IN THE REAL APP, AND REFUSE-BAD / SERVE-GOOD ON EVERY
#               ADDRESS
#
# Everything above builds its OWN FastAPI app and calls
# register_trading_routes() on it. That proves the module works. It does NOT
# prove the module is reachable in production, and that is exactly the gap the
# defect lived in: eleven addresses were fully implemented and fully green
# against a private app, and were never registered on server.main -- so all
# eleven answered 404 for a real caller while this file passed.
#
# So the two halves below are deliberately different in kind:
#
#   * MOUNTED_* reads the REAL app's route table, in a clean subprocess. The
#     subprocess matters for the reason test_auth_route_coverage_live_table.py
#     gives: server.main.app is a module-level singleton and sibling test files
#     re-run register_*() helpers against it, so an in-process read sees
#     duplicates that do not exist in the shipped app.
#
#   * the WALK drives the private app again, but records BOTH an unauthorised
#     answer and a served answer for every address, and asserts the two key
#     sets are identical. A new address cannot join the surface without also
#     joining the walk -- that is what stops the file drifting back to "the
#     module is fine, the wiring is not".
# ===========================================================================

#: The eleven market addresses, as (method, path). Hand-maintained on purpose:
#: it is the list the mount tests check AGAINST, and the extra-address test
#: below fails when the surface grows past it, so a new route cannot be added
#: silently -- it has to be added here, where the coverage duty is written down.
MOUNTED_MARKET_ROUTES = [
    ("POST", "/trade/request"),
    ("POST", "/trade/accept"),
    ("POST", "/trade/cancel"),
    ("POST", "/trade/mail"),
    ("POST", "/trade/mail/accept"),
    ("GET", "/market/sect"),
    ("POST", "/market/sect/list"),
    ("GET", "/auction/listings"),
    ("POST", "/auction/list"),
    ("POST", "/auction/bid"),
    ("POST", "/auction/buyout"),
]

#: Prefixes that name the trading surface. Used to find "every market address"
#: without a list, so the extra-address test can prove the list above is not
#: quietly missing one.
_MARKET_PREFIXES = ("/trade", "/market/sect", "/auction")

_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SERVER_DIR = os.path.join(_REPO_ROOT, "server")

# Reads server.main.app and prints (method, path) for every APIRoute. Kept as
# a string because it runs in a subprocess where this module is not importable.
_MOUNT_PROBE = '''
import json, sys
sys.path.insert(0, @SERVER_DIR@)
sys.path.insert(0, @REPO_ROOT@)
from fastapi.routing import APIRoute
import server.main as m
pairs = sorted({(meth, route.path) for route in m.app.routes
                if isinstance(route, APIRoute)
                for meth in (route.methods or set())
                if meth in ("GET", "POST")})
sys.stderr.write("@@JSON@@" + json.dumps(pairs))
'''.replace("@SERVER_DIR@", repr(_SERVER_DIR)).replace(
    "@REPO_ROOT@", repr(_REPO_ROOT))

_MOUNT_MARKER = "@@JSON@@"
_MOUNT_CACHE: dict = {}


def real_app_route_pairs() -> set:
    """(method, path) for every route on the REAL server.main app, as a set.

    Cached: the probe costs one application start, and the shipped route table
    cannot change within a single pytest run.
    """
    if "pairs" in _MOUNT_CACHE:
        return _MOUNT_CACHE["pairs"]
    import subprocess
    import sys as _sys
    import tempfile

    env = dict(os.environ)
    # An inherited PG DSN points this at a database that may not exist on this
    # host and makes the app fail at import. The route table does not need it.
    for var in ("PYTHONPATH", "WC_PG_DB", "WC_PG_HOST", "WC_PG_USER",
                "WC_PG_PASSWORD", "WC_PG_PORT"):
        env.pop(var, None)

    fd, probe_path = tempfile.mkstemp(suffix="_wc_trading_mount_probe.py",
                                      prefix="wc_trading_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_MOUNT_PROBE)
        proc = subprocess.run(
            [_sys.executable, probe_path], capture_output=True, text=True,
            timeout=300, cwd=_REPO_ROOT, env=env,
        )
    finally:
        try:
            os.unlink(probe_path)
        except OSError:
            pass

    blob = proc.stderr or ""
    if proc.returncode != 0 or _MOUNT_MARKER not in blob:
        raise AssertionError(
            "could not read the live route table in a clean subprocess.\n"
            f"  returncode: {proc.returncode}\n"
            f"  stdout tail: {(proc.stdout or '')[-800:]!r}\n"
            f"  stderr tail: {blob[-1500:]!r}"
        )
    pairs = {(str(meth), str(path))
             for meth, path in json.loads(blob.split(_MOUNT_MARKER, 1)[1].strip())}
    _MOUNT_CACHE["pairs"] = pairs
    return pairs


def _walk_unauthorised(c, key, body) -> int:
    """One anonymous call per address. Returns the status code it answered."""
    method, path = key.split(" ", 1)
    if method == "GET":
        if path == "/market/sect":
            return c.get(path, params={"player_id": "nobody"}).status_code
        return c.get(path).status_code
    return c.post(path, json=body).status_code


def _serve_every_address(c, env) -> dict:
    """Drive all eleven addresses to a SUCCESSFUL answer, in dependency order.

    Returns {key: status_code}. One walk rather than eleven fixtures, because
    the market is a chain: a bid needs a listing, a listing needs a seller who
    holds the goods, and a mail claim needs a mail. Splitting them would mean
    re-deriving that chain eleven times.
    """
    served: dict = {}
    seller = env["make"]("wk_seller", stones=100000)
    buyer = env["make"]("wk_buyer", stones=100000)
    member = env["make"]("wk_member", stones=100000)
    with env["players"].transaction(member):
        env["players"][member]["sect_id"] = "sect-walk"

    # Two units of TRADEABLE for the seller: one leaves by direct trade, one by
    # mail, and the cancelled offer moves nothing either way. MULTI is the
    # auction item so the two boards do not compete for the same stack.
    env["inv"].add_item(seller, TRADEABLE, 2)
    env["inv"].add_item(seller, MULTI, 3)
    env["inv"].add_item(member, TRADEABLE, 1)

    # -- §3.1 direct trade: request -> accept -> (second request) -> cancel
    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": TRADEABLE, "quantity": 1}],
                     "requested_stones": 500},
               headers={**_tok(seller), **_idem("wk-req-1")})
    served["POST /trade/request"] = r.status_code
    assert r.status_code == 200, f"the walk could not open a trade: {r.text}"
    tid = r.json()["trade_id"]

    r = c.post("/trade/accept", json={"player_id": buyer, "trade_id": tid},
               headers={**_tok(buyer), **_idem("wk-acc-1")})
    served["POST /trade/accept"] = r.status_code
    assert r.status_code == 200, f"the walk could not accept a trade: {r.text}"

    r = c.post("/trade/request",
               json={"player_id": seller, "counterparty_id": buyer,
                     "offered_items": [{"item_id": TRADEABLE, "quantity": 1}]},
               headers={**_tok(seller), **_idem("wk-req-2")})
    assert r.status_code == 200, f"the walk could not open a 2nd trade: {r.text}"
    tid2 = r.json()["trade_id"]
    r = c.post("/trade/cancel", json={"player_id": buyer, "trade_id": tid2},
               headers=_tok(buyer))
    served["POST /trade/cancel"] = r.status_code
    assert r.status_code == 200, f"the walk could not cancel a trade: {r.text}"

    # -- §3.2 mail: send -> accept. The goods leave the sender's bag at send,
    # which is why the seller needed a second unit.
    r = c.post("/trade/mail",
               json={"player_id": seller, "recipient_id": buyer,
                     "items": [{"item_id": TRADEABLE, "quantity": 1}],
                     "price_stones": 500},
               headers={**_tok(seller), **_idem("wk-mail-1")})
    served["POST /trade/mail"] = r.status_code
    assert r.status_code == 200, f"the walk could not send mail: {r.text}"
    mid = r.json()["trade_id"]
    r = c.post("/trade/mail/accept", json={"player_id": buyer, "trade_id": mid},
               headers={**_tok(buyer), **_idem("wk-mail-2")})
    served["POST /trade/mail/accept"] = r.status_code
    assert r.status_code == 200, f"the walk could not claim mail: {r.text}"

    # -- §3.3 sect board: list -> read the board back
    r = c.post("/market/sect/list",
               json={"player_id": member, "item_id": TRADEABLE, "quantity": 1,
                     "buyout_price": 900},
               headers={**_tok(member), **_idem("wk-sect-1")})
    served["POST /market/sect/list"] = r.status_code
    assert r.status_code == 200, f"the walk could not list on the board: {r.text}"

    r = c.get("/market/sect", params={"player_id": member}, headers=_tok(member))
    served["GET /market/sect"] = r.status_code
    assert r.status_code == 200, f"the walk could not read the board: {r.text}"

    # -- §3.4 auction house: list -> bid -> buyout
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": MULTI, "quantity": 3,
                     "starting_price": 500, "buyout_price": 900},
               headers={**_tok(seller), **_idem("wk-auc-1")})
    served["POST /auction/list"] = r.status_code
    assert r.status_code == 200, f"the walk could not list at auction: {r.text}"
    lid = r.json()["listing"]["listing_id"]

    r = c.get("/auction/listings", headers=_tok(buyer))
    served["GET /auction/listings"] = r.status_code
    assert r.status_code == 200, f"the walk could not read the house: {r.text}"

    r = c.post("/auction/bid",
               json={"player_id": buyer, "listing_id": lid, "amount": 600},
               headers={**_tok(buyer), **_idem("wk-bid-1")})
    served["POST /auction/bid"] = r.status_code
    assert r.status_code == 200, f"the walk could not bid: {r.text}"

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("wk-buy-1")})
    served["POST /auction/buyout"] = r.status_code
    assert r.status_code == 200, f"the walk could not buy out: {r.text}"

    return served


def _mount_keys() -> set:
    """The eleven addresses as 'METHOD path' strings."""
    return {f"{method} {path}" for method, path in MOUNTED_MARKET_ROUTES}


def missing_addresses(pairs: set) -> list:
    """Declared addresses absent from ``pairs``. Pure, so it can be controlled.

    Takes the pair set as an argument rather than reading the probe itself, so
    a negative control can drive it with a deliberately-incomplete table and
    show the check actually names the hole.
    """
    return sorted(f"{method} {path}" for method, path in MOUNTED_MARKET_ROUTES
                  if (method, path) not in pairs)


def undeclared_addresses(pairs: set) -> list:
    """Market addresses on the app that this file does not declare. Pure."""
    declared = set(MOUNTED_MARKET_ROUTES)
    on_app = {(method, path) for method, path in pairs
              if path.startswith(_MARKET_PREFIXES)}
    return sorted(f"{m} {p}" for m, p in on_app if (m, p) not in declared)


def test_the_real_app_mounts_every_market_address():
    """The mount proof. This is the assertion the whole defect would have failed.

    Reads server.main's real route table in a clean subprocess and requires all
    eleven addresses to be present with the right HTTP method. Before the
    wiring landed, every one of these answered 404 in production while this file
    was green, because every other test here drives a private app.
    """
    pairs = real_app_route_pairs()
    # Anti-vacuity: the probe must have seen a real app, not an empty one.
    assert len(pairs) > 150, (
        f"the live route table holds only {len(pairs)} routes. The probe is not "
        "seeing server.main's routers, so 'every address is mounted' would be "
        "vacuous.")
    missing = missing_addresses(pairs)
    assert not missing, (
        f"{len(missing)} market address(es) are NOT on server.main.app, so they "
        "answer 404 for every real caller. A green test file cannot catch this: "
        "the tests above drive a private app. Fix: call register_trading_routes("
        "app, ...) from server/main.py.\n"
        + "\n".join(f"    {k}" for k in missing))


def test_the_real_app_mounts_no_market_address_outside_the_declared_eleven():
    """Anti-drift, and the reason MOUNTED_MARKET_ROUTES is hand-written.

    A twelfth market address on the real app that is not in the list above is a
    route no refusal-versus-serving pair exists for. Failing here forces the
    author to add it to the list, which is where the coverage duty lives.
    """
    undeclared = undeclared_addresses(real_app_route_pairs())
    assert not undeclared, (
        "server.main.app mounts market address(es) this file does not cover:\n"
        + "\n".join(f"    {k}" for k in undeclared)
        + "\n\n  Add each one to MOUNTED_MARKET_ROUTES and to _serve_every_address,"
          "\n  so it gets a refusal arm and a serving arm like every other.")


def test_every_market_address_is_refused_without_a_session(env):
    """REFUSAL ARM, measured on the private app for all eleven addresses.

    One parameterised case per address, driven with a well-formed body and no
    Authorization header at all. A 2xx here would mean a stranger reached the
    handler and got a successful answer.
    """
    c = env["client"]
    for method, path in MOUNTED_MARKET_ROUTES:
        key = f"{method} {path}"
        body = dict(MINIMAL_BODY[key]) if method == "POST" else {}
        status = _walk_unauthorised(c, key, body)
        assert status in (401, 403), (
            f"{key} answered a caller with no session: {status}")


def test_every_market_address_serves_a_valid_call(env):
    """SERVING ARM, measured on the private app for all eleven addresses.

    The refusal arm above is only half a contract: a route that refuses
    everything passes it. This walks the whole market once and requires a 2xx
    from every address, so "closed" and "usable" are both asserted on the same
    set of keys.
    """
    served = _serve_every_address(env["client"], env)
    expected = {f"{m} {p}" for m, p in MOUNTED_MARKET_ROUTES}
    assert set(served) == expected, (
        f"the serving walk covered {len(served)} address(es) but the surface has "
        f"{len(expected)}. Missing: {sorted(expected - set(served))}. "
        f"Unexpected: {sorted(set(served) - expected)}")
    not_served = sorted(f"{k} -> {v}" for k, v in served.items()
                        if not 200 <= int(v) < 300)
    assert not not_served, (
        "these market addresses refused a valid, well-formed, funded call:\n"
        + "\n".join(f"    {k}" for k in not_served))


def test_the_two_arms_cover_the_same_addresses(env):
    """The refusal arm and the serving arm must describe ONE surface.

    Without this, the two tests above could each be edited to cover a
    different subset and both stay green -- which is the shape of the original
    defect, where a route was implemented and tested but never reachable.
    """
    served = set(_serve_every_address(env["client"], env))
    refused = _mount_keys()
    assert served == refused, (
        "the refusal arm and the serving arm disagree about the surface.\n"
        f"  refusal only: {sorted(refused - served)}\n"
        f"  serving only : {sorted(served - refused)}")


# ===========================================================================
# NEGATIVE CONTROLS for the mount checks themselves
#
# The mount proof is the assertion the original defect would have failed, so it
# is exactly the assertion that must not be quietly broken into always-passing.
# These drive the two pure helpers with tables built to be wrong on purpose.
# ===========================================================================
def test_control_the_mount_check_names_an_unmounted_address():
    """Drop three addresses from the table: the check must name exactly those.

    A mount check that returned [] always would pass the real test while the
    defect it exists to catch came straight back.
    """
    pairs = {(method, path) for method, path in MOUNTED_MARKET_ROUTES}
    for dropped in (("POST", "/auction/buyout"), ("POST", "/auction/bid"),
                    ("GET", "/market/sect")):
        pairs.discard(dropped)
    missing = missing_addresses(pairs)
    assert missing == ["GET /market/sect", "POST /auction/bid",
                       "POST /auction/buyout"], (
        f"the mount check named {missing!r}. It must name exactly the three "
        "addresses that were removed, sorted, and nothing else.")

    # And the whole set present must produce nothing, or the helper is stuck on.
    assert missing_addresses(set(MOUNTED_MARKET_ROUTES)) == [], (
        "with every address present the mount check still reported a gap")


def test_control_the_undeclared_check_fires_on_a_twelfth_route():
    """A new market address nobody declared must be reported.

    This is the check that stops the surface growing past the eleven without a
    refusal-versus-serving pair being written for it.
    """
    pairs = set(MOUNTED_MARKET_ROUTES) | {
        ("POST", "/auction/escrow"),
        # Not the trading surface: the prefix filter must skip these, or the
        # check would fire on unrelated routes.
        ("POST", "/wallet/convert"),
        ("POST", "/tournament/clock/tick"),
    }
    assert undeclared_addresses(pairs) == ["POST /auction/escrow"], (
        "the undeclared check must name the one new market address and skip "
        "the unrelated ones. It returned %r" % (undeclared_addresses(pairs),))

    assert undeclared_addresses(set(MOUNTED_MARKET_ROUTES)) == [], (
        "with only the eleven declared addresses present the undeclared check "
        "still reported something")


# ===========================================================================
# §5.4, the two addresses that were changed: sharper arms on the AUCTION paths
#
# The section above already pins the relaxation. These two add what it could
# not: the bid side at quantity > 1, and the state a buyout must LEAVE behind.
# A relaxation that returned the goods to the seller, or left the listing open,
# would pass every status-code assertion in the file above.
# ===========================================================================
def test_a_multi_unit_bid_is_accepted_from_a_bidder_who_holds_none(env):
    """The bid arm at quantity > 1, mirroring the buyout's multi-unit test.

    The holding rule was quantity-relative, so relaxing it only for a single
    unit would pass the one-unit bid test and still refuse this one.
    """
    c = env["client"]
    seller = env["make"]("mq_s", stones=100000)
    bidder = env["make"]("mq_b", stones=100000)
    env["inv"].add_item(seller, MULTI, 5)
    assert env["inv"].get_item_count(seller, MULTI) == 5, (
        "the fixture could not stock five units, so this test would prove "
        "nothing about quantity")
    lid = _list_for_auction(c, env, seller, item=MULTI, quantity=5,
                            starting_price=250, buyout_price=450, key="mq")
    assert env["inv"].get_item_count(bidder, MULTI) == 0, (
        "the fixture is wrong: the bidder already holds the item")

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 300},
               headers={**_tok(bidder), **_idem("mq-bid")})
    assert r.status_code == 200, f"a bidder holding none of 5 was refused: {r.text}"
    assert r.json()["current_bid"] == 300
    with env["store"]._lock:
        row = env["store"].listings[lid]
    assert int(row["current_bid"]) == 300 and row["bidder_id"] == bidder
    assert row["state"] == "held", "a bid must leave the listing open"


def test_a_served_buyout_closes_the_listing_and_a_later_bid_is_refused(env):
    """What a buyout must LEAVE behind, not just the 200 it returned.

    Three things, each of which a 200 can hide: the listing is closed, the
    escrow copy is gone from the seller, and a bid arriving afterwards is
    refused rather than recorded against a sold listing.
    """
    c = env["client"]
    seller = env["make"]("cl_s", stones=100000)
    buyer = env["make"]("cl_b", stones=100000)
    late = env["make"]("cl_l", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="cl")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cl-buy")})
    assert r.status_code == 200, r.text

    with env["store"]._lock:
        row = env["store"].listings[lid]
    assert row["state"] == "sold", f"the listing is still {row['state']!r}"
    assert env["inv"].get_item_count(seller, TRADEABLE) == 0, (
        "the escrow copy came back to the seller after the sale")
    assert env["inv"].get_item_count(buyer, TRADEABLE) == 1

    r = c.post("/auction/bid",
               json={"player_id": late, "listing_id": lid, "amount": 1000},
               headers={**_tok(late), **_idem("cl-late")})
    assert r.status_code == 409, (
        f"a bid was accepted against a sold listing: {r.text}")
    with env["store"]._lock:
        assert env["store"].listings[lid]["bidder_id"] is None, (
            "a refused bidder was recorded on a sold listing")


# ===========================================================================
# NEGATIVE CONTROL -- the realm gate on the buyout path is load-bearing
#
# A guard that has never been seen to fail is indistinguishable from a guard
# that cannot fail. This replaces _check_tradable with a no-op and asserts the
# refusal the negative-arm tests rely on DISAPPEARS. If it does not disappear,
# something other than _check_tradable is refusing, and every negative-arm
# assertion in this file is measuring the wrong rule.
# ===========================================================================
def test_control_the_realm_gate_is_what_refuses_a_low_realm_buyer(env, monkeypatch):
    """Sabotage _check_tradable in memory: the 403 must become a 200.

    This is the in-file half of the scratch-copy proof. It pins that the realm
    refusal measured by test_the_relaxation_did_not_open_the_realm_gate_to_buyers
    comes from _check_tradable and not from some other rule that happens to
    answer 403.
    """
    c = env["client"]
    seller = env["make"]("ctlg_s", stones=100000)
    buyer = env["make"]("ctlg_b", stones=100000)
    with env["players"].transaction(seller):
        env["players"][seller]["cultivation_realm"] = "qi_refining"
    env["inv"].add_item(seller, REALM_GATED, 1)
    lid = _list_for_auction(c, env, seller, item=REALM_GATED,
                            starting_price=500, buyout_price=900, key="ctlg")

    # First, un-sabotaged: the low-realm buyer is refused.
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("ctlg-before")})
    assert r.status_code == 403, (
        f"the un-sabotaged buyout answered {r.status_code}: {r.text}")

    # Now take the guard away and the refusal must go with it.
    monkeypatch.setattr(trading_market, "_check_tradable",
                        lambda *a, **k: None)
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("ctlg-after")})
    assert r.status_code == 200, (
        f"with _check_tradable disabled the buyer was still refused "
        f"({r.status_code} {r.text}). Something OTHER than _check_tradable is "
        "answering 403, so the negative-arm tests are measuring the wrong rule.")


# ===========================================================================
# SECTION 3 -- THE FOUR REQUIRED BEHAVIOURS, MEASURED
#
# Everything above was already in the file. The four requirements this section
# answers are (1) auction creation requires auth, (2) bidding requires
# sufficient funds, (3) buyout requires a holding check, (4) escrow rolls back
# on a partial failure. The first three already had arms scattered above; what
# was missing were the BOUNDARIES and the ATTRIBUTION -- the arm that proves
# WHICH rule refused, not merely that something did.
#
# ONE FIXTURE DIFFERENCE, AND IT IS LOAD-BEARING
# ----------------------------------------------
# `env` above builds InventoryStore but never assigns
# ``players_db.inventory_store``. server/main.py:978 DOES, in BOTH the
# database and the no-database mode, and player_store.transaction() only
# snapshots the inventory when that attribute is set (player_store.py:118).
# So under the `env` fixture a mid-settlement failure rolls back the MONEY and
# leaves the goods moved. Measured, not assumed: the same injected failure with
# the production line present rolls back both.
#
# `env_production` below sets it, because a rollback test run against a
# fixture that omits the production wiring would pass for the wrong reason.
# ===========================================================================

#: Reason recorded on the two live-defect pins in this section. Both were
#: measured against this tree before being written down.
ROLLBACK_LISTING_NOT_REVERTED = (
    "MEASURED DEFECT: a failure inside _settle_listing_sale (after the goods "
    "land and the money moves) rolls back the player balances and the "
    "inventory, but the TradeStore listing/offer dicts are NOT inside the "
    "PlayerStore transaction, so the listing is left marked 'sold' with a "
    "500. The escrow copy is then in nobody's bag, the listing can never be "
    "claimed again, and a retry under a fresh key is refused 409 'Listing is "
    "sold, not open'. The goods are permanently destroyed."
)
ESCROW_COPY_NOT_REVERIFIED = (
    "MEASURED DEFECT: _settle_listing_sale trusts the listing row and never "
    "re-checks that the seller still does NOT hold the escrowed copy, so if "
    "that copy reappears in the seller's bag the buyout still succeeds and "
    "the item is duplicated: measured 2 copies of talisman_return in the "
    "world from 1. Not directly reachable through a route today -- it needs "
    "the escrow copy to come back by some other means -- which is why this is "
    "a hardening pin and not a live exploit claim."
)


@pytest.fixture
def env_production(monkeypatch):
    """`env`, plus the one production wiring line `env` omits.

    Identical to `env` apart from ``players.inventory_store = inv``, which is
    what server/main.py:978 does in both modes. Without it the transaction
    takes no inventory snapshot and a partial failure leaves goods moved.
    """
    monkeypatch.delenv("WC_PG_HOST", raising=False)
    players = PlayerStore(None)
    assert players._pg is False, "fixture needs the in-memory PlayerStore"
    inv = InventoryStore(pg_pool=None, player_store=players)
    players.inventory_store = inv          # main.py:978
    app = FastAPI()
    store = trading_market.register_trading_routes(app, lambda: players, inv, None)
    counter = {"n": 0}

    def make(name: str, stones: int = 0) -> str:
        counter["n"] += 1
        pid = f"{name}-{counter['n']}"
        players.create_account(
            pid, {"id": pid, "name": name, "spirit_root": "fire"}, b"k" * 32
        )
        with players.transaction(pid):
            players[pid]["spirit_stones"] = {"low_grade": stones}
        return pid

    yield {"client": TestClient(app, raise_server_exceptions=False),
           "players": players, "inv": inv, "store": store, "make": make}
    trading_market._INVENTORY[0] = None
    trading_market._SECT_STORE[0] = None


def _stones(env, pid):
    with env["players"].transaction(pid):
        return int(env["players"][pid]["spirit_stones"]["low_grade"])


def _non_raising(env):
    """A TestClient for the same app that returns 500 instead of raising.

    The `env` fixture's client propagates server exceptions, which is right for
    normal arms and wrong for the sabotage arms below: an injected failure
    would escape the test rather than answer 500, and the assertions are about
    the HTTP reply. `env_production` already builds one; this gives the same
    capability to an `env` fixture without changing it.
    """
    return TestClient(env["client"].app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# (1) AUCTION CREATION REQUIRES AUTH
#
# The anonymous and forged-token arms live above. What was missing is the
# SECOND half of "requires auth": being signed in is not enough, the session
# must be the player named in the body. A signed-in stranger naming somebody
# else is the actual hole, and the anonymous arm cannot see it.
# --------------------------------------------------------------------------
def test_the_env_fixture_omits_the_production_inventory_wiring(env, env_production):
    """Why there are two fixtures here, stated as an assertion.

    If `env` ever gains the line, this turns red and the two fixtures can be
    merged. Until then a rollback test under `env` would be measuring a
    fixture artefact rather than the module.
    """
    assert env["players"].inventory_store is None, (
        "the `env` fixture now sets players.inventory_store, so it no longer "
        "differs from `env_production`; merge the two fixtures")
    assert env_production["players"].inventory_store is env_production["inv"], (
        "the `env_production` fixture lost the production wiring it exists for")


@pytest.mark.parametrize("route,body", [
    ("/auction/list", {"item_id": TRADEABLE, "quantity": 1,
                       "starting_price": 500}),
    ("/auction/bid", {"listing_id": "listing_x", "amount": 600}),
    ("/auction/buyout", {"listing_id": "listing_x"}),
], ids=["auction-list", "auction-bid", "auction-buyout"])
def test_a_signed_in_stranger_cannot_act_as_another_player(env, route, body):
    """OWNERSHIP. Requirement (1), the half the anonymous arm cannot see.

    `require_player` reads the body, and every market body carries a
    `player_id`. Without the ownership check, any session could open a listing
    that spends ANOTHER player's goods and ANOTHER player's list fee. This
    drives a valid session for A against a body naming B.
    """
    c = env["client"]
    a = env["make"]("own_attacker", stones=100000)
    b = env["make"]("own_victim", stones=100000)
    env["inv"].add_item(b, TRADEABLE, 1)
    victim_stones = _stones(env, b)
    victim_items = env["inv"].get_item_count(b, TRADEABLE)
    assert victim_items == 1, "the fixture must actually stock the victim"

    r = c.post(route, json={"player_id": b, **body},
               headers={**_tok(a), **_idem("own-" + route)})
    assert r.status_code == 403, (
        f"a session for {a} was allowed to act as {b} on {route}: "
        f"{r.status_code} {r.text}")
    assert "does not match" in r.text, (
        f"the refusal was not the ownership check: {r.text}")

    # Nothing was spent on the victim's behalf.
    assert _stones(env, b) == victim_stones, (
        "the victim paid a fee for a listing they never asked for")
    assert env["inv"].get_item_count(b, TRADEABLE) == victim_items, (
        "the victim's goods left their bag without their consent")


def test_the_listing_belongs_to_the_session_that_opened_it(env):
    """The positive half: a session may act as ITSELF.

    Without this, a fix that made every body-differing request 403 would pass
    the ownership arm above while breaking the whole module.
    """
    c = env["client"]
    seller = env["make"]("own_self", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": TRADEABLE, "quantity": 1,
                     "starting_price": 500},
               headers={**_tok(seller), **_idem("own-self")})
    assert r.status_code == 200, r.text
    assert r.json()["listing"]["seller_id"] == seller


# --------------------------------------------------------------------------
# (2) BIDDING REQUIRES SUFFICIENT FUNDS
#
# The arm above (`test_a_bidder_without_the_stones_is_409`) bids 5000 with a
# balance of 1 -- a gap so wide that any threshold at all would refuse it.
# What it could not distinguish is WHICH rule refused, and the funds check is
# the fourth of five checks on the bid path, so a bid can be refused for a
# reason that has nothing to do with money.
#
# MEASURED, and this is the trap: the route checks "must beat the current bid"
# BEFORE it checks the balance. So a short bidder against an already-bid
# listing is refused with "Bid must beat the current bid" and the funds rule
# never runs. Asserting only the 409 would credit the funds check for a
# refusal the funds check did not produce. Every arm below therefore uses a
# FRESH listing and asserts on the MESSAGE.
# --------------------------------------------------------------------------
def test_a_bidder_with_exactly_the_bid_amount_is_served(env):
    """Boundary, funded side. `>= amount` is enough, not `> amount`."""
    c = env["client"]
    seller = env["make"]("fund_ok_s", stones=100000)
    bidder = env["make"]("fund_ok_b", stones=600)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="fundok")
    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("fundok-bid")})
    assert r.status_code == 200, (
        f"a bidder holding exactly the bid amount was refused: {r.text}")
    assert r.json()["current_bid"] == 600


def test_a_bidder_one_stone_short_is_refused_by_the_funds_check(env):
    """Boundary, unfunded side, at ONE stone short.

    `amount - 1` rather than a round number, so an off-by-one in the
    comparison (`<` vs `<=`) turns this red instead of hiding.
    """
    c = env["client"]
    seller = env["make"]("fund_no_s", stones=100000)
    bidder = env["make"]("fund_no_b", stones=599)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="fundno")
    assert 0 == env["store"].get_listing(lid)["current_bid"], (
        "the fixture must start from an unbid listing, or the funds check is "
        "masked by the 'must beat the current bid' check")

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 600},
               headers={**_tok(bidder), **_idem("fundno-bid")})
    assert r.status_code == 409, r.text
    assert "stones" in r.text.lower(), (
        f"the refusal was NOT the funds check: {r.text}. The 'must beat the "
        "current bid' check runs first, so a listing that already carries a "
        "bid would produce this message for an unfunded bidder.")


def test_a_refused_bid_records_nothing_at_all(env):
    """A refused bid must leave no trace: no bidder, no amount, no log row.

    A bid that answers 409 but still writes the bidder onto the listing would
    let a player who could not pay freeze the auction.

    The amount is 800, not the 5000 an earlier revision used. Measured: at 5000
    this request is refused by the BUYOUT-PRICE rule ("Bid is at or above the
    buyout price"), which the route checks BEFORE the balance, so the funds
    path never ran and this test was not witnessing what it names. 800 is over
    the 25 floor and under the 900 buyout, so the funds check is the rule that
    refuses -- asserted on the message so it cannot silently drift back.
    """
    c = env["client"]
    seller = env["make"]("fund_tr_s", stones=100000)
    bidder = env["make"]("fund_tr_b", stones=1)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="fundtr")
    log_before = len(env["store"].log_rows())

    r = c.post("/auction/bid",
               json={"player_id": bidder, "listing_id": lid, "amount": 800},
               headers={**_tok(bidder), **_idem("fundtr-bid")})
    assert r.status_code == 409, r.text
    assert "stones" in r.text.lower(), (
        f"the refusal was NOT the funds check, so this arm is not measuring "
        f"the path it names: {r.text}")

    row = env["store"].get_listing(lid)
    assert row["bidder_id"] is None, f"a refused bidder was recorded: {row}"
    assert int(row["current_bid"]) == 0, f"a refused amount was recorded: {row}"
    assert row["state"] == "held", f"a refused bid closed the listing: {row}"
    assert _stones(env, bidder) == 1, "a refused bid moved the bidder's stones"
    assert len(env["store"].log_rows()) == log_before, (
        "a refused bid wrote a trade_log row; the log is append-only and is "
        "what the daily caps and the 90-day sweep are computed from")


def test_the_funds_check_is_not_what_refuses_an_already_beaten_bid(env):
    """CONTROL for the attribution. The other 409 must NOT say 'stones'.

    Proves the message assertion above discriminates: if the bid path refused
    everything with the same text, the funds arm would be unfalsifiable.
    """
    c = env["client"]
    seller = env["make"]("attr_s", stones=100000)
    first = env["make"]("attr_1", stones=100000)
    second = env["make"]("attr_2", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="attr")
    r = c.post("/auction/bid",
               json={"player_id": first, "listing_id": lid, "amount": 600},
               headers={**_tok(first), **_idem("attr-1")})
    assert r.status_code == 200, r.text

    r = c.post("/auction/bid",
               json={"player_id": second, "listing_id": lid, "amount": 600},
               headers={**_tok(second), **_idem("attr-2")})
    assert r.status_code == 409, r.text
    assert "beat the current bid" in r.text, (
        f"expected the tie to be refused as a non-winning bid: {r.text}")
    assert "stones" not in r.text.lower(), (
        f"a fully funded tie was refused for lack of stones: {r.text}")


# --------------------------------------------------------------------------
# (3) BUYOUT REQUIRES A HOLDING CHECK
#
# "The buyer need not already hold the goods" is pinned at length above -- that
# is the relaxation, and the negative arms there cover the realm, quest, bound
# and seller-side holding gates. What is NOT pinned anywhere is the ESCROW
# side: the listing claims the seller handed the goods over, and the buyout
# pays on that claim without ever confirming the seller still does not hold a
# copy.
# --------------------------------------------------------------------------
def test_the_escrow_holds_the_goods_out_of_the_sellers_bag_before_any_bid(env):
    """The holding rule on the way IN: the goods leave the seller at listing."""
    c = env["client"]
    seller = env["make"]("esc_s", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    assert env["inv"].get_item_count(seller, TRADEABLE) == 1, "fixture is wrong"

    lid = _list_for_auction(c, env, seller, buyout_price=900, key="esc")
    assert env["inv"].get_item_count(seller, TRADEABLE) == 0, (
        "the listing left the goods in the seller's bag, so escrow holds "
        "nothing and the seller could sell the same item twice")
    assert env["store"].get_listing(lid)["state"] == "held"


def test_a_served_buyout_moves_exactly_one_copy_and_destroys_none(env):
    """Conservation. One copy in before, one copy out, one copy total.

    The item count across BOTH players is the assertion that a duplication
    bug cannot hide from: a 200 with two copies somewhere is the same defect
    one layer down, and a per-player assertion would call that a pass.
    """
    c = env["client"]
    seller = env["make"]("cons_s", stones=100000)
    buyer = env["make"]("cons_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="cons")

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("cons-buy")})
    assert r.status_code == 200, r.text
    assert env["inv"].get_item_count(buyer, TRADEABLE) == 1
    total = (env["inv"].get_item_count(seller, TRADEABLE)
             + env["inv"].get_item_count(buyer, TRADEABLE))
    assert total == 1, (
        f"the world now holds {total} copies of {TRADEABLE} after one sale "
        "from one; the buyout duplicated or destroyed the item")


@pytest.mark.xfail(strict=False, reason=ESCROW_COPY_NOT_REVERIFIED)
def test_a_buyout_reverifies_that_the_seller_still_holds_no_copy(env):
    """Requirement (3), escrow side. LIVE DEFECT -- currently xfails.

    `_settle_listing_sale` calls `_require_player` for the seller and then
    hands the goods straight to the buyer. It never re-checks the seller's
    bag. Put the escrowed copy back and the sale still settles: measured 2
    copies in the world from 1.
    """
    c = env["client"]
    seller = env["make"]("dup_s", stones=100000)
    buyer = env["make"]("dup_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="dup")
    assert env["inv"].get_item_count(seller, TRADEABLE) == 0, (
        "fixture is wrong: escrow did not take the goods")

    # Simulate the escrow copy coming back (a refund path, a rollback, a
    # duplicated stack). Nothing in a route does this today.
    env["inv"].add_item(seller, TRADEABLE, 1)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("dup-buy")})
    assert r.status_code == 409, (
        f"the buyout settled although the seller still held the escrowed copy "
        f"({r.status_code} {r.text}) -- the item has been duplicated")
    total = (env["inv"].get_item_count(seller, TRADEABLE)
             + env["inv"].get_item_count(buyer, TRADEABLE))
    assert total == 1, (
        f"the world now holds {total} copies of {TRADEABLE}; the seller was "
        "paid and the buyer was given a second copy")


# --------------------------------------------------------------------------
# (4) ESCROW ROLLS BACK ON A PARTIAL FAILURE
#
# The route bodies wrap settlement in `players_db.transaction()`, and that
# transaction snapshots the wallet AND the inventory (player_store.py:117-119)
# -- but only when `players_db.inventory_store` is set, which main.py:978 does
# and the `env` fixture above does not. So these arms use `env_production`.
#
# The failure is injected at `_settle_listing_sale`'s LAST step (the log write)
# deliberately: by then the goods have landed in the buyer's bag and the stones
# have moved, so a rollback that does not reach the TradeStore dicts is
# visible rather than theoretical.
# --------------------------------------------------------------------------
def _fail_on_nth_log_write(env, monkeypatch, nth=1):
    """Make the nth trade_log write explode, so settlement dies mid-flight.

    Returns a counter so the caller can assert the injection actually fired.
    A control that silently skips would make every arm below vacuous.
    """
    real = trading_market.TradeStore.log_event
    seen = {"n": 0}

    def flaky(self, *a, **k):
        seen["n"] += 1
        if seen["n"] == nth:
            raise RuntimeError("injected mid-settlement failure")
        return real(self, *a, **k)

    monkeypatch.setattr(trading_market.TradeStore, "log_event", flaky)
    return seen


def test_a_mid_settlement_failure_rolls_back_the_money_and_the_goods(
        env_production, monkeypatch):
    """The half of the rollback that WORKS. No marker on this one.

    Injected at the log write, which is after the grant and after the debit.
    Measured on this tree: both balances are restored and the buyer keeps
    nothing.
    """
    c = env_production["client"]
    seller = env_production["make"]("rb_s", stones=100000)
    buyer = env_production["make"]("rb_b", stones=100000)
    env_production["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env_production, seller, buyout_price=900, key="rb")

    seller_before = _stones(env_production, seller)
    buyer_before = _stones(env_production, buyer)
    # nth=1: the patch is installed AFTER the listing's own log row was
    # written, so the first call it intercepts is the settlement one.
    seen = _fail_on_nth_log_write(env_production, monkeypatch, nth=1)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("rb-buy")})
    assert r.status_code == 500, f"the injection did not fail the route: {r.text}"
    assert seen["n"] == 1, f"the injection fired {seen['n']} times, not once"

    assert _stones(env_production, seller) == seller_before, (
        "the seller's stones moved on a settlement that failed")
    assert _stones(env_production, buyer) == buyer_before, (
        "the buyer was charged for a sale that never completed")
    assert env_production["inv"].get_item_count(buyer, TRADEABLE) == 0, (
        "the buyer kept the goods from a settlement that failed")


def test_the_env_fixture_without_production_wiring_leaves_goods_moved(env, monkeypatch):
    """Why the two fixtures exist, as a measured fact rather than a claim.

    Same injection, same route, same assertion -- and under `env` the goods
    are NOT rolled back, because that fixture never sets
    `players_db.inventory_store` and the transaction therefore takes no
    inventory snapshot. If this ever turns green the fixtures have converged.
    """
    c = _non_raising(env)
    seller = env["make"]("nowire_s", stones=100000)
    buyer = env["make"]("nowire_b", stones=100000)
    env["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env, seller, buyout_price=900, key="nowire")
    _fail_on_nth_log_write(env, monkeypatch, nth=1)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("nowire-buy")})
    assert r.status_code == 500, r.text
    assert env["inv"].get_item_count(buyer, TRADEABLE) == 1, (
        "the `env` fixture now rolls the inventory back too, so it sets the "
        "production wiring after all -- merge it with `env_production`")


@pytest.mark.xfail(strict=False, reason=ROLLBACK_LISTING_NOT_REVERTED)
def test_a_mid_settlement_failure_leaves_the_listing_claimable_again(
        env_production, monkeypatch):
    """Requirement (4), the half that DOES NOT work. LIVE DEFECT -- xfails.

    Money and inventory roll back (proved above). The TradeStore's own
    `listings` and `offers` dicts are plain attributes of a module-level
    object, not part of the PlayerStore transaction, so `state="sold"` set by
    `_settle_listing_sale` survives the rollback.

    The consequence is worse than a stale flag: the escrow copy was removed
    from the seller at listing time, and the rollback returns it to nobody, so
    the item is destroyed and the listing can never be claimed again.
    """
    c = env_production["client"]
    seller = env_production["make"]("dead_s", stones=100000)
    buyer = env_production["make"]("dead_b", stones=100000)
    env_production["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env_production, seller, buyout_price=900, key="dead")
    _fail_on_nth_log_write(env_production, monkeypatch, nth=1)

    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("dead-buy")})
    assert r.status_code == 500, r.text

    row = env_production["store"].get_listing(lid)
    assert row["state"] == "held", (
        f"the listing is {row['state']!r} after a settlement that rolled back; "
        "it can never be claimed again and the escrowed goods are in nobody's "
        "bag")

    # And the practical consequence: a fresh key cannot rescue it either.
    monkeypatch.undo()
    r = c.post("/auction/buyout", json={"player_id": buyer, "listing_id": lid},
               headers={**_tok(buyer), **_idem("dead-retry")})
    assert r.status_code == 200, (
        f"the buyer cannot retry a rolled-back buyout: {r.status_code} {r.text}")
    assert env_production["inv"].get_item_count(buyer, TRADEABLE) == 1, (
        "the retry did not deliver the goods")


def test_a_failed_settlement_burns_its_idempotency_key_rather_than_paying_twice(
        env_production, monkeypatch):
    """The replay guard survives the rollback -- and that is the design.

    `reserve()` runs before the money moves and stores a placeholder, so a
    failed attempt leaves a spent key. Replaying it must NOT return a bare
    200 `{"ok": true}`: the caller never got the goods, so a fake success
    would be a lie. Asserted here because the file above proves this only for
    the /market/sect/list path, and the buyout path is the one that moves a
    purchase.
    """
    c = env_production["client"]
    seller = env_production["make"]("burn_s", stones=100000)
    buyer = env_production["make"]("burn_b", stones=100000)
    env_production["inv"].add_item(seller, TRADEABLE, 1)
    lid = _list_for_auction(c, env_production, seller, buyout_price=900, key="burn")
    key = "burn-key"
    headers = {**_tok(buyer), **_idem(key)}
    body = {"player_id": buyer, "listing_id": lid}

    _fail_on_nth_log_write(env_production, monkeypatch, nth=1)
    first = c.post("/auction/buyout", json=body, headers=headers)
    assert first.status_code == 500, first.text
    monkeypatch.undo()

    second = c.post("/auction/buyout", json=body, headers=headers)
    assert second.status_code == 409, (
        f"a replay of a failed settlement answered {second.status_code}: "
        f"{second.text}. A 200 here would tell the buyer their purchase "
        "succeeded when they hold nothing.")
    assert env_production["inv"].get_item_count(buyer, TRADEABLE) == 0, (
        "the replay delivered the goods without taking payment")


def test_a_failed_listing_returns_the_goods_and_the_list_fee(
        env_production, monkeypatch):
    """Escrow on the way IN also rolls back -- this half works.

    `_create_listing` removes the goods, debits the list fee, writes the offer
    and THEN writes the listing. Failing the listing write is therefore the
    deepest reachable point, and both the goods and the fee must come back.

    Runs on `env_production` rather than `env` for the reason documented at
    the top of this section: the `env` fixture takes no inventory snapshot,
    so a failure here would strand the goods for a fixture reason. The
    unbalanced-fee half of this claim does not need the wiring, which is why
    the fee assertion is kept alongside the goods assertion rather than split
    into a second test.
    """
    c = env_production["client"]
    fee = trading_market.list_fee_stones(TRADEABLE)
    assert fee > 0, "the list fee is zero, so the refund assertion below is vacuous"

    # POSITIVE CONTROL FIRST: prove a SUCCESSFUL listing really charges the
    # fee. Without this the refund assertion would also pass if the fee were
    # never charged at all -- which is the state a mutation that deletes the
    # debit would produce.
    payer = env_production["make"]("in_ok", stones=100000)
    env_production["inv"].add_item(payer, TRADEABLE, 1)
    payer_before = _stones(env_production, payer)
    ok = c.post("/auction/list",
                json={"player_id": payer, "item_id": TRADEABLE, "quantity": 1,
                      "starting_price": 500},
                headers={**_tok(payer), **_idem("in-ok")})
    assert ok.status_code == 200, ok.text
    assert _stones(env_production, payer) == payer_before - fee, (
        f"a successful listing did not charge the {fee}-stone list fee, so the "
        "refund assertion in this test could not fail")

    seller = env_production["make"]("in_s", stones=100000)
    env_production["inv"].add_item(seller, TRADEABLE, 1)
    stones_before = _stones(env_production, seller)

    def boom(self, *a, **k):
        raise RuntimeError("injected after escrow took the goods")

    monkeypatch.setattr(trading_market.TradeStore, "create_listing", boom)
    r = c.post("/auction/list",
               json={"player_id": seller, "item_id": TRADEABLE, "quantity": 1,
                     "starting_price": 500},
               headers={**_tok(seller), **_idem("in-list")})
    assert r.status_code == 500, r.text
    assert env_production["inv"].get_item_count(seller, TRADEABLE) == 1, (
        "the goods stayed in escrow after the listing write failed; the seller "
        "cannot get them back because no listing exists")
    assert _stones(env_production, seller) == stones_before, (
        f"the {fee}-stone list fee was not refunded on a failed listing")


def test_a_mail_claim_that_cannot_deliver_keeps_the_escrow_intact(env):
    """The one partial failure reachable without sabotage: a full stack.

    `accept_mail` grants to the recipient before charging, and the grant is
    refused when the recipient's stack has no room. The mail must stay pending
    with the goods in escrow -- NOT be marked settled, and NOT leave the
    sender's copy destroyed.
    """
    c = env["client"]
    # cultivation_pill_low: stack ceiling 99 (measured, used above as MULTI).
    sender = env["make"]("ml_s", stones=0)
    recipient = env["make"]("ml_r", stones=100000)
    env["inv"].add_item(recipient, MULTI, 99)
    env["inv"].add_item(sender, MULTI, 1)

    r = c.post("/trade/mail",
               json={"player_id": sender, "recipient_id": recipient,
                     "items": [{"item_id": MULTI, "quantity": 1}],
                     "price_stones": 0},
               headers={**_tok(sender), **_idem("ml-send")})
    assert r.status_code == 200, r.text
    tid = r.json()["trade_id"]
    assert env["inv"].get_item_count(sender, MULTI) == 0, (
        "the goods never entered escrow at send time")

    r = c.post("/trade/mail/accept", json={"player_id": recipient, "trade_id": tid},
               headers={**_tok(recipient), **_idem("ml-claim")})
    assert r.status_code == 409, r.text
    assert "stack full" in r.text, f"wrong refusal: {r.text}"
    assert env["inv"].get_item_count(recipient, MULTI) == 99, (
        "the recipient's stack grew past its ceiling")
    assert env["store"].get_offer(tid)["state"] == "pending", (
        "a failed claim settled the mail, destroying the escrowed goods")
    # And the escrow is still claimable once there is room.
    env["inv"].remove_item(recipient, MULTI, 99)
    r = c.post("/trade/mail/accept", json={"player_id": recipient, "trade_id": tid},
               headers={**_tok(recipient), **_idem("ml-claim2")})
    assert r.status_code == 200, (
        f"the mail is unclaimable after a full stack: {r.status_code} {r.text}")
    assert env["inv"].get_item_count(recipient, MULTI) == 1, (
        "the retry did not deliver the escrowed goods")
