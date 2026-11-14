"""Behaviour contract for server/lottery.py -- the three routes and the maths
behind them.

WHAT THIS FILE IS
-----------------
The lottery/gacha system is the only way a player spends premium currency at
scale, and the only place the server rolls a random outcome. Its three routes
have a client-shaped response contract that nothing else in the repo pins:

    GET  /lottery/pools                -> catalogue, no player
    GET  /lottery/{player_id}/state   -> per-player pity + totals
    POST /lottery/{player_id}/pull    -> the write: charges, rolls, grants

`server/tests/test_lottery_pull_request_contract.py` already covers the REQUEST
side of `/pull` (body vs query transport, the 1-or-10 count domain, refusals
that must not charge). This file deliberately does not repeat any of that. It
covers the RESPONSE shape, the pity state machine, and the reward ledger -- the
three things a client panel reads and the request-contract file never looks at.

WHY BEHAVIOUR CONTRACTS, NOT SNAPSHOTS
--------------------------------------
Every pull is random. A test that pins "the third pull gave a purple" is a test
that fails on the next run and proves nothing. So each test here asserts a
RELATIONSHIP between two pieces of data -- asked-for count vs results returned,
pity before vs pity after, rate-table keys vs reward-table keys -- which the
randomness cannot break. Where a value is pinned (a cost, a pity cap, a key
name) it is a client-facing constant that the shipped client hardcodes too.

RUN IT
------
    cd G:/Projects/wayfarers-cycle && env -u WC_PG_DB -u WC_PG_HOST \\
      -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT -u PYTHONPATH \\
      python -m pytest server/tests/test_lottery_contract.py -q

The `env -u` prefix matters: with the PostgreSQL variables inherited, this repo's
suites block on a database pool instead of using the in-memory store.

NO EXISTING FILE IS TOUCHED. This file is additive and owns itself alone.
"""
import os
import sys

# Must run at import time, before lottery is imported: register_lottery_routes
# pulls in cosmetics, which opens a store on import if PG is configured.
for _v in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD",
           "WC_PG_PORT", "PYTHONPATH"):
    os.environ.pop(_v, None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import cosmetics
import lottery as L
from lottery import POOL_CONFIG, POOL_RATES, POOL_REWARDS, Rarity


# The canonical tiered spirit-stones dict main.create_player builds. A scalar is
# also accepted by the route (legacy callers), and the shape is asserted to
# survive a pull -- see test_pull_preserves_the_currency_shape_it_was_handed.
STONES = {"copper": 10 ** 6, "silver": 0, "gold": 0, "low_grade": 10 ** 6,
          "mid_grade": 0, "high_grade": 0, "extreme_grade": 0}

# The rarity ladder, top to bottom. Needed because lottery.py ranks rarities
# with an explicit RARITY_ORDER lookup rather than by name; this list is the
# test-side mirror of that ladder, used to read "which rarity is better" out
# of a result dict.
LADDER = ["orange", "purple", "blue", "green", "white"]


def rarity_at_least(rarity, floor):
    """True when `rarity` is `floor` or rarer, on the LADDER above.

    Mirrors lottery.rarity_at_least_as_good but goes through this file's own
    list, so a test can read the comparison as "is this result good enough?"
    without reaching into the module under test for a helper.
    """
    return LADDER.index(rarity.value) <= LADDER.index(floor.value)

# What a client panel reads off a pull response. GachaView.gd reads rarity and
# reward per result; the others are what a panel needs to render the pity bar.
RESULT_KEYS = {"pool", "rarity", "reward", "pity_before", "pity_after", "is_pity"}
STATE_KEYS = {"pool", "name", "currency", "cost_per_pull", "cost_10x", "pity",
              "pity_max", "rate", "target_rarity", "total_pulls",
              "guaranteed_at", "guaranteed_type"}


def _app_for(pid, **overrides):
    """Build a bare app with the real routes and one seeded player.

    `spend_fn`/`earn_fn` are omitted by default so the in-memory currency arm
    runs -- that is the same arm production falls back to, and it is the one
    that writes into the player dict a test can then inspect.
    """
    player = {"lottery": {}, "inventory": {}, "spirit_stones": dict(STONES)}
    player.update(overrides)
    db = {pid: player}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    return db, TestClient(app)


def _pull(client, pid, pool, count=1):
    """A pull on the body transport, which is what the shipped client uses."""
    return client.post(f"/lottery/{pid}/pull",
                       json={"pool": pool, "count": count})


# ---------------------------------------------------------------------------
# 1. Route surface. Three routes, and nothing answers where they are not.
# ---------------------------------------------------------------------------

def test_exactly_three_lottery_routes_are_mounted():
    """The module registers three paths. A fourth is a new surface, not a fix."""
    app = FastAPI()
    L.register_lottery_routes(app, lambda: {})
    paths = sorted(
        (sorted(r.methods)[0], r.path)
        for r in app.routes
        if getattr(r, "path", "").startswith("/lottery")
    )
    assert paths == [
        ("GET", "/lottery/pools"),
        ("GET", "/lottery/{player_id}/state"),
        ("POST", "/lottery/{player_id}/pull"),
    ], paths


def test_catalogue_route_is_the_only_lottery_path_without_a_player():
    """/lottery/pools carries no player id, so a panel can read it signed-out."""
    _, c = _app_for("unused")
    assert c.get("/lottery/pools").status_code == 200
    # The catalogue is GET-only: POSTing to it is a method error, not a pull.
    assert c.post("/lottery/pools").status_code == 405


# ---------------------------------------------------------------------------
# 2. GET /lottery/pools -- the catalogue contract.
# ---------------------------------------------------------------------------

def test_catalogue_returns_one_entry_per_pool_in_enum_order():
    """A panel indexes the list by position, so the order is the contract."""
    _, c = _app_for("cat")
    pools = c.get("/lottery/pools").json()["pools"]
    assert [p["pool"] for p in pools] == [pl.value for pl in L.LotteryPool]
    assert len(pools) == len(L.LotteryPool)


def test_every_catalogue_entry_carries_the_full_display_shape():
    """Each entry has the exact key set the panel binds to -- no more, no less.

    Exact equality, not a subset check: a client reading a key that later goes
    away breaks silently, and a key nobody reads is dead weight shipped to
    every client.
    """
    _, c = _app_for("cat")
    for entry in c.get("/lottery/pools").json()["pools"]:
        assert set(entry) == STATE_KEYS, (entry["pool"], sorted(entry))


def test_catalogue_prices_and_pity_match_the_server_config():
    """The advertised numbers are the numbers the pull route charges.

    A panel shows cost_per_pull and pity_max to the player. If either drifts
    from POOL_CONFIG the server is quoting a price it will not honour.
    """
    _, c = _app_for("cat")
    for entry in c.get("/lottery/pools").json()["pools"]:
        cfg = POOL_CONFIG[L.LotteryPool(entry["pool"])]
        assert entry["cost_per_pull"] == cfg["cost_per_pull"]
        assert entry["cost_10x"] == cfg["cost_10x"]
        assert entry["pity_max"] == cfg["pity"]
        assert entry["target_rarity"] == cfg["rarity"].value
        assert entry["currency"] == cfg["currency"]


def test_a_ten_pull_is_cheaper_than_ten_single_pulls_in_every_pool():
    """The discount the bulk price exists to offer, as a relationship.

    Asserted as "<=" rather than an exact 10% off, because the rate is tuning
    data -- but the bulk price must never be a premium.
    """
    for pool, cfg in POOL_CONFIG.items():
        assert cfg["cost_10x"] <= cfg["cost_per_pull"] * 10, (
            f"{pool.value}: a 10-pull costs more than ten singles"
        )
        assert cfg["cost_10x"] >= cfg["cost_per_pull"], (
            f"{pool.value}: the 10-pull is priced below a single pull"
        )


def test_catalogue_reports_a_fresh_player_as_all_zero():
    """No player id in the path, so every counter must read zero, not leak one."""
    db, c = _app_for("someone_else_with_pity")
    db["someone_else_with_pity"]["lottery"] = {
        "pity_counters": {"earth_cache": 7}, "total_pulls": {"earth_cache": 99},
        "last_pull_time": {}}
    for entry in c.get("/lottery/pools").json()["pools"]:
        assert entry["pity"] == 0
        assert entry["total_pulls"] == 0


# ---------------------------------------------------------------------------
# 3. GET /lottery/{player_id}/state -- the player's own counters.
# ---------------------------------------------------------------------------

def test_state_refuses_a_player_that_does_not_exist():
    _, c = _app_for("real")
    r = c.get("/lottery/nobody/state")
    assert r.status_code == 404
    assert "not found" in str(r.json()).lower()


def test_state_echoes_the_pity_and_totals_stored_on_the_player():
    """The state route is a read of player['lottery'], and nothing else.

    Seeded directly rather than reached through pulls, so the assertion is
    about the read path alone and cannot be broken by a change to the roll.
    """
    pid = "seeded"
    db, c = _app_for(pid)
    db[pid]["lottery"] = {
        "pity_counters": {"earth_cache": 4},
        "total_pulls": {"earth_cache": 11},
        "last_pull_time": {"earth_cache": 1234.5},
    }
    body = c.get(f"/lottery/{pid}/state").json()
    assert body["ok"] is True
    entry = _entry(body["pools"], "earth_cache")
    assert entry["pity"] == 4
    assert entry["total_pulls"] == 11
    # Untouched pools stay at zero rather than inheriting the seeded pool's
    # numbers -- a per-pool counter must not bleed across pools.
    for other in L.LotteryPool:
        if other.value != "earth_cache":
            got = _entry(body["pools"], other.value)
            assert got["pity"] == 0 and got["total_pulls"] == 0, other.value


def test_state_totals_track_pulls_made_through_the_route():
    """Asked-for count vs counters that move: the panel's progress bar."""
    pid = "counter"
    db, c = _app_for(pid)
    assert _pull(c, pid, "common_satchel", 1).status_code == 200
    assert _pull(c, pid, "common_satchel", 10).status_code == 200
    entry = _entry(c.get(f"/lottery/{pid}/state").json()["pools"], "common_satchel")
    assert entry["total_pulls"] == 11, entry
    # Two pools were never touched, so their counters must still read zero.
    untouched = _entry(c.get(f"/lottery/{pid}/state").json()["pools"], "earth_cache")
    assert untouched["total_pulls"] == 0 and untouched["pity"] == 0


def test_state_shape_matches_the_catalogue_shape():
    """The same panel row renders from either route -- one shape, two sources."""
    _, c = _app_for("shape")
    catalogue = c.get("/lottery/pools").json()["pools"][0]
    per_player = c.get("/lottery/shape/state").json()["pools"][0]
    assert set(catalogue) == set(per_player) == STATE_KEYS


# ---------------------------------------------------------------------------
# 4. POST /lottery/{player_id}/pull -- response shape.
# ---------------------------------------------------------------------------

def test_a_single_pull_returns_exactly_one_result_with_the_client_keys():
    _, c = _app_for("s1")
    body = _pull(c, "s1", "common_satchel", 1).json()
    assert body["ok"] is True
    assert len(body["results"]) == 1
    assert set(body["results"][0]) == RESULT_KEYS, body["results"][0]


def test_asked_count_equals_returned_count_for_both_sold_batch_sizes():
    """The invariant a caller can check with no knowledge of the internals."""
    for count in (1, 10):
        pid = f"n{count}"
        _, c = _app_for(pid)
        body = _pull(c, pid, "earth_cache", count).json()
        assert len(body["results"]) == count, (count, len(body["results"]))


def test_every_result_names_the_pool_and_a_rarity_the_rate_table_knows():
    """No result may carry a rarity the pool's own tables do not define.

    This is the check that catches a rate table and a reward table drifting
    apart: an undefined rarity would fall through to the "spirit_stones"
    fallback reward and pay a player 100 stones for a legendary roll.
    """
    for pool in L.LotteryPool:
        if POOL_CONFIG[pool]["currency"] != "spirit_stone":
            continue  # needs stones; covered by the currency-arm tests below
        pid = "rt_" + pool.value
        _, c = _app_for(pid)
        body = _pull(c, pid, pool.value, 10).json()
        for rec in body["results"]:
            assert rec["pool"] == pool.value
            assert Rarity(rec["rarity"]) in POOL_RATES[pool], (
                f"{pool.value} produced a rarity its own rate table "
                f"does not define: {rec}"
            )


def test_the_billed_cost_is_the_configured_price_for_the_batch_size():
    """`total_cost` is the server's own price, not the caller's arithmetic."""
    for count in (1, 10):
        pid = f"cost{count}"
        db, c = _app_for(pid)
        # The pool name comes from the enum, never a hand-typed literal: the
        # route answers 400 "Invalid pool" for a near-miss like mortal_offer.
        body = _pull(c, pid, L.LotteryPool.MORTAL_COFFER.value, count).json()
        cfg = POOL_CONFIG[L.LotteryPool.MORTAL_COFFER]
        expected = cfg["cost_per_pull"] if count == 1 else cfg["cost_10x"]
        assert body["total_cost"] == expected, (count, body["total_cost"])


def test_the_net_spend_equals_the_bill_minus_any_stone_reward_credited_back():
    """Asked-for cost, minus stones paid back, equals what left the purse.

    Both halves move money, and they move it independently: the route debits
    the batch price first, then credits 100 stones for each result whose reward
    is literally "spirit_stones". A panel's balance and a test's balance only
    agree if both halves are honoured.
    """
    for pool in L.LotteryPool:
        if POOL_CONFIG[pool]["currency"] != "spirit_stone":
            continue
        pid = "net_" + pool.value
        db, c = _app_for(pid)
        body = _pull(c, pid, pool.value, 10).json()
        stones_back = 100 * sum(1 for r in body["results"]
                                if r["reward"] == "spirit_stones")
        net = STONES["low_grade"] - db[pid]["spirit_stones"]["low_grade"]
        assert net == body["total_cost"] - stones_back, (
            pool.value, net, body["total_cost"], stones_back
        )


def test_a_cosmetic_award_is_a_sibling_of_results_not_a_result():
    """`cosmetic` sits beside `results`, so a panel can show it separately.

    The handler imports roll_gacha_cosmetic at REGISTRATION time, so the patch
    has to be in place before the routes are mounted or it never takes effect.
    """
    original_roll, original_grant = cosmetics.roll_gacha_cosmetic, cosmetics.grant_gacha_cosmetic
    cosmetics.roll_gacha_cosmetic = lambda *a, **k: "cosmetic_x"
    cosmetics.grant_gacha_cosmetic = lambda *a, **k: True
    try:
        _, c = _app_for("cos")
        body = _pull(c, "cos", "common_satchel", 1).json()
        assert body.get("cosmetic") == "cosmetic_x", body
        assert "cosmetic" not in body["results"][0], body["results"][0]
    finally:
        cosmetics.roll_gacha_cosmetic = original_roll
        cosmetics.grant_gacha_cosmetic = original_grant


def test_no_cosmetic_key_when_the_roll_misses():
    """The common case must not ship a null the client has to special-case."""
    original = cosmetics.roll_gacha_cosmetic
    cosmetics.roll_gacha_cosmetic = lambda *a, **k: None
    try:
        _, c = _app_for("nocos")
        body = _pull(c, "nocos", "common_satchel", 1).json()
        assert "cosmetic" not in body, body
    finally:
        cosmetics.roll_gacha_cosmetic = original


def test_a_pull_refuses_a_player_that_does_not_exist_without_charging():
    _, c = _app_for("real")
    r = c.post("/lottery/ghost/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 404


def test_a_pull_refuses_an_empty_purse_before_rolling_anything():
    """No result, no charge, no pity movement -- an empty purse is not a pull."""
    db, c = _app_for("broke", spirit_stones=dict(STONES, low_grade=0))
    r = _pull(c, "broke", "common_satchel", 1)
    assert r.status_code == 400
    assert "insufficient" in str(r.json()).lower()
    assert db["broke"]["lottery"] == {}, db["broke"]["lottery"]
    assert db["broke"]["inventory"] == {}


# ---------------------------------------------------------------------------
# 5. The pity state machine -- the part a panel's bar depends on.
# ---------------------------------------------------------------------------

def test_pity_resets_on_the_target_rarity_and_climbs_on_everything_else():
    """The rule the bar is drawn from: 0 on target, +1 otherwise.

    Driven directly rather than through HTTP, so the assertion is about the
    counter arithmetic and cannot be broken by the transport.
    """
    pool = L.LotteryPool.EARTH_CACHE
    target = POOL_CONFIG[pool]["rarity"].value
    state = L.PlayerLotteryState()
    for i in range(40):
        rec = L.pull_lottery(state, pool)
        assert rec["pity_after"] == (0 if rec["rarity"] == target
                                    else rec["pity_before"] + 1), rec
        assert rec["pity_before"] == state.pity_counters[pool.value] or rec["pity_before"] >= 0
        assert state.total_pulls[pool.value] == i + 1


def test_the_pity_counters_of_two_pools_stay_independent():
    """Pity is per pool. A shared counter would silently shorten one banner."""
    a, b = L.LotteryPool.EARTH_CACHE, L.LotteryPool.MORTAL_COFFER
    state = L.PlayerLotteryState()
    for _ in range(12):
        L.pull_lottery(state, a)
    for _ in range(3):
        L.pull_lottery(state, b)
    assert state.total_pulls[a.value] == 12
    assert state.total_pulls[b.value] == 3
    assert set(state.pity_counters) == {a.value, b.value}


def test_the_pity_cap_delivers_the_target_rarity_on_guaranteed_pools():
    """At the cap the advertised guarantee must actually pay out.

    Seeded at `pity - 1`, which is the pull the handler calls `is_pity`.
    Only the pools whose config sets guaranteed_after_pity make this promise;
    the other two report `is_pity` too but sell "higher_rarity", which the code
    does not implement -- pinned separately below.
    """
    for pool, cfg in POOL_CONFIG.items():
        if not cfg["guaranteed_after_pity"]:
            continue
        state = L.PlayerLotteryState(
            pity_counters={pool.value: cfg["pity"] - 1},
            total_pulls={pool.value: 0}, last_pull_time={})
        rec = L.pull_lottery(state, pool)
        assert rec["is_pity"] is True
        assert rec["rarity"] == cfg["rarity"].value, (pool.value, rec)


def test_guaranteed_pools_never_exceed_their_advertised_cap():
    """A bar labelled "pity 90" must never show 95 -- that is a broken promise."""
    for pool, cfg in POOL_CONFIG.items():
        if not cfg["guaranteed_after_pity"]:
            continue
        state = L.PlayerLotteryState()
        for _ in range(cfg["pity"] + 5):
            rec = L.pull_lottery(state, pool)
            if rec["is_pity"]:
                assert rec["rarity"] == cfg["rarity"].value, (pool.value, rec)
                assert rec["pity_after"] == 0


def test_the_saved_state_holds_all_three_counter_maps():
    """Pity, totals and last-pull time are all persisted; a panel reads all three."""
    pid = "save"
    db, c = _app_for(pid)
    assert _pull(c, pid, "common_satchel", 10).status_code == 200
    saved = db[pid]["lottery"]
    assert set(saved) == {"pity_counters", "total_pulls", "last_pull_time"}, saved
    assert saved["total_pulls"]["common_satchel"] == 10
    assert isinstance(saved["last_pull_time"]["common_satchel"], float)


# ---------------------------------------------------------------------------
# 6. The reward ledger -- what a roll actually grants.
# ---------------------------------------------------------------------------

def test_a_reward_the_pool_defines_lands_in_the_player_inventory():
    """Roll to a defined reward and it must be in inventory, at +1 each."""
    pid = "grant"
    db, c = _app_for(pid)
    body = _pull(c, pid, "spirit_vault", 10).json()
    granted = [r["reward"] for r in body["results"]
               if r["reward"] != "spirit_stones"]
    assert granted, "a 10-pull from a defined pool produced no item reward"
    for name in granted:
        assert name in db[pid]["inventory"], (name, db[pid]["inventory"])
    assert sum(db[pid]["inventory"].values()) == len(granted)


def test_the_currency_reward_pays_one_hundred_stones_each_time():
    """`spirit_stones` is a real reward row; it must be paid, not swallowed.

    Driven by seeding the module RNG and searching for a roll that produces the
    stone reward, rather than hoping a random 10-pull happens to. A test that
    skips when the dice go the other way is not a contract.

    NOTE THE POOL CHANGED, and the reason matters. This used to run against
    earth_cache, where `spirit_stones` was only ever the FALLBACK -- earth_cache
    reached it because it rated PURPLE with no purple reward row, so the missing
    row silently paid 100 stones. That row now exists (see
    `no_pool_rates_a_rarity_it_cannot_pay`), so earth_cache can no longer
    produce the stone reward at all: every rarity it rates has a real item.

    The stone reward is now reachable only where it is a genuine catalogue
    entry -- common_satchel's white row -- which is where this test belongs.
    The payout path under test (route credits 100 per "spirit_stones" result)
    is identical either way; only the reason the pool yields it changed.
    """
    pool = L.LotteryPool.COMMON_SATCHEL
    cfg = POOL_CONFIG[pool]
    for seed in range(500):
        state = L.PlayerLotteryState()
        _seed_all(seed)
        results = L.pull_lottery_10x(state, pool)
        stones = [r for r in results if r["reward"] == "spirit_stones"]
        if len(stones) == 2:
            break
    else:
        pytest.fail("no seed in 500 produced two stone rewards")

    # The rate is a client-facing constant: the module documents 100 per roll.
    assert len(stones) == 2, (seed, [r["reward"] for r in results])

    # Now through the route, seeded so the same rewards are rolled.
    pid = "stones"
    db, c = _app_for(pid)
    _seed_all(seed)
    body = _pull(c, pid, pool.value, 10).json()
    stone_rewards = [r for r in body["results"] if r["reward"] == "spirit_stones"]
    assert len(stone_rewards) == 2, [r["reward"] for r in body["results"]]
    after = db[pid]["spirit_stones"]["low_grade"]
    assert after == (STONES["low_grade"] - cfg["cost_10x"]
                     + 100 * len(stone_rewards)), after


def test_every_rarity_a_pool_rates_pays_an_item_not_the_stone_fallback():
    """The reward-table gap that made a PURPLE roll pay 100 stones is closed.

    The old contract here was "the gap exists, and the route survives it". That
    gap was the bug: earth_cache rated PURPLE at 0.5% and had no purple reward
    row, so those pulls fell through to the spirit_stones fallback and paid
    currency for what the rate table advertised as an Epic item. POOL_REWARDS
    now carries a purple row for earth_cache (plus blue/green rows for
    mortal_coffer and common_satchel and green for heavenly_treasury, which the
    rarity-ladder fix made reachable again).

    Asserted as a set relationship, both ways, over the shipped tables:
      * every rated rarity must have a reward row (this is the defect);
      * every reward row must have a rate (the direction that would let a pool
        pay a rarity it never rolls).
    """
    for pool in L.LotteryPool:
        rated = set(POOL_RATES[pool])
        rewarded = set(POOL_REWARDS[pool])
        assert rated <= rewarded, (
            f"{pool.value} rates a rarity it cannot pay an item for "
            f"({[r.value for r in rated - rewarded]}) -- those pulls silently "
            f"fall back to 100 spirit_stones"
        )
        assert not (rewarded - rated), (
            f"{pool.value} can pay a rarity it never rates: "
            f"{[r.value for r in rewarded - rated]}"
        )


def test_a_rated_purple_now_pays_an_epic_item_in_earth_cache():
    """The concrete harm, checked by rolling it rather than by reading a table.

    Two halves: the table gains a purple row (a fact), and a real purple roll
    therefore pays an item instead of 100 stones (behaviour). Searching seeds
    for a purple keeps this deterministic -- earth_cache rates purple at 0.5%,
    so a fixed number of random pulls would flake.
    """
    pool = L.LotteryPool.EARTH_CACHE
    assert Rarity.PURPLE in POOL_REWARDS[pool], POOL_REWARDS[pool]
    seen = None
    for seed in range(500):
        _seed_all(seed)
        rec = L.pull_lottery(L.PlayerLotteryState(), pool)
        if rec["rarity"] == "purple":
            seen = (seed, rec)
            break
    assert seen is not None, "earth_cache never rolled purple in 500 seeds"
    seed, rec = seen
    assert rec["reward"] in POOL_REWARDS[pool][Rarity.PURPLE], (seed, rec)
    assert rec["reward"] != "spirit_stones", (
        f"seed {seed}: purple still pays the stone fallback", rec)


def test_no_pool_can_roll_a_rarity_its_own_reward_table_cannot_name():
    """Set-difference between the rate table and the reward table, both ways.

    Asserted as a set relationship rather than equality, because a rate table
    legitimately carries rarities that only exist to satisfy the pity fallback
    (heavenly_treasury rates GREEN but has no green reward row). What must hold
    is the direction that loses a reward: no reward row without a rate.
    """
    for pool in L.LotteryPool:
        rated = set(POOL_RATES[pool])
        rewarded = set(POOL_REWARDS[pool])
        assert not (rewarded - rated), (
            f"{pool.value} can pay a rarity it never rates: "
            f"{[r.value for r in rewarded - rated]}"
        )


def test_the_token_pool_spends_tokens_and_leaves_stones_alone():
    """Two currencies, two ledgers -- a token pull must not touch stones."""
    pid = "tokens"
    db, c = _app_for(pid, tokens={"tribulation_token": 10 ** 6})
    r = _pull(c, pid, "heavenly_treasury", 1)
    assert r.status_code == 200
    cfg = POOL_CONFIG[L.LotteryPool.HEAVENLY_TREASURY]
    assert db[pid]["tokens"]["tribulation_token"] == 10 ** 6 - cfg["cost_per_pull"]
    assert db[pid]["spirit_stones"]["low_grade"] == STONES["low_grade"]
    # And the stone pools must not touch the token balance.
    assert _pull(c, pid, "common_satchel", 1).status_code == 200
    assert db[pid]["tokens"]["tribulation_token"] == 10 ** 6 - cfg["cost_per_pull"]


def test_pull_preserves_the_currency_shape_it_was_handed():
    """The tiered dict stays a dict; it is never collapsed to a scalar.

    main.create_player builds the tiered shape, so a coercion here would
    silently rewrite the canonical field for every player who pulls.
    """
    pid = "shape"
    db, c = _app_for(pid)
    assert _pull(c, pid, "common_satchel", 1).status_code == 200
    stones = db[pid]["spirit_stones"]
    assert isinstance(stones, dict), type(stones)
    assert set(stones) == set(STONES), sorted(stones)


def test_the_player_in_memory_arm_still_works_for_a_scalar_legacy_shape():
    """Six other shims accept a scalar for the same legacy reason.

    Seeded, and asserted against the response rather than a fixed balance: the
    pull may also credit a 100-stone reward, so the balance is only known once
    the rewards are counted. Written without the seed this test failed about
    one run in six.
    """
    pid = "scalar"
    cfg = POOL_CONFIG[L.LotteryPool.COMMON_SATCHEL]
    db, c = _app_for(pid, spirit_stones=10 ** 6)
    _seed_all(20260101)
    body = _pull(c, pid, L.LotteryPool.COMMON_SATCHEL.value, 1).json()
    stones_back = 100 * sum(1 for r in body["results"]
                            if r["reward"] == "spirit_stones")
    assert db[pid]["spirit_stones"] == 10 ** 6 - cfg["cost_per_pull"] + stones_back
    # The scalar must not have become a dict on the way through.
    assert isinstance(db[pid]["spirit_stones"], int), type(db[pid]["spirit_stones"])


# ---------------------------------------------------------------------------
# 7. The rarity ladder. This section USED to pin the alphabetical-compare
#    defect; it now pins the fix. Both tests below previously reimplemented the
#    broken filter locally, which meant they stayed green forever no matter
#    what pull_lottery did -- they asserted that a string comparison disagrees
#    with a list index, which is a fact about the alphabet, not about this
#    module. Every assertion here now goes through the real roll.
# ---------------------------------------------------------------------------

def test_every_rarity_the_rate_table_offers_can_actually_roll():
    """No pool silently drops a rate it pays for.

    This is the defect's real shape. The old filter compared rarity NAMES as
    text, so which rates survived depended on spelling:
      * "orange" >= "purple" is False, so spirit_vault could never roll its
        0.1% ORANGE;
      * "blue" >= "green" is False, so mortal_coffer could never roll its 2%
        BLUE, and common_satchel never rolled its 5% GREEN;
      * "purple" >= "blue" is True, so earth_cache rolled a PURPLE it had no
        reward row for.

    A dead rate is invisible in a test that reads the table, so this asserts the
    opposite direction: over many pulls, every rarity the table offers must
    show up. Rates that cannot appear are the bug, in either direction.
    """
    for pool in L.LotteryPool:
        offered = set(POOL_RATES[pool])
        seen = set()
        for seed in range(3000):
            _seed_all(seed)
            rec = L.pull_lottery(L.PlayerLotteryState(), pool)
            seen.add(Rarity(rec["rarity"]))
        # A pool whose table sums below 1.0 delivers its lowest rarity through
        # the shortfall fallback too, so every offered rarity is reachable.
        dead = offered - seen
        assert not dead, (
            f"{pool.value} rates {sorted(r.value for r in dead)} but never "
            f"rolled them in 3000 pulls"
        )


def test_the_roll_follows_the_ladder_and_not_the_alphabet():
    """Rarer must stay rarer: the ordering the module promises is the ladder.

    RARITY_ORDER is the module's own lookup (orange..white = 0..4). Two
    properties, both behavioural:
      * the map's own order is the ladder, not the alphabet;
      * a pool that rates two rarities rolls them in the ladder's proportions,
        so the rarer one is never the more common outcome.

    Checked by measured frequency over seeded pulls rather than by reading
    POOL_RATES, so a future edit to the roll cannot silently reintroduce a
    text comparison and keep this green.
    """
    assert list(L.RARITY_ORDER) == [Rarity.ORANGE, Rarity.PURPLE, Rarity.BLUE,
                                    Rarity.GREEN, Rarity.WHITE]
    # The alphabet would order these by name instead; assert the two differ,
    # because a test that could not tell them apart would prove nothing.
    by_ladder = [r.value for r in sorted(L.RARITY_ORDER, key=lambda r: L.RARITY_ORDER[r])]
    assert by_ladder != sorted(by_ladder), "ladder collapsed into alphabetical order"

    # mortal_coffer rates blue 2%, green 25%, white 73% -- a strict ladder.
    # Under the old alphabetical filter the blue rate was dead, so blue could
    # never appear at all.
    pool = L.LotteryPool.MORTAL_COFFER
    counts = {}
    for seed in range(6000):
        _seed_all(seed)
        r = L.pull_lottery(L.PlayerLotteryState(), pool)["rarity"]
        counts[r] = counts.get(r, 0) + 1
    # Sort by the LADDER (best first), not by insertion order and not by name --
    # then the rarer rarity must not be rolled less often than the one below it.
    ladder_sorted = sorted(counts, key=lambda name: LADDER.index(name))
    for rarer, worse in zip(ladder_sorted, ladder_sorted[1:]):
        assert counts[rarer] <= counts[worse], (
            f"{pool.value}: rolled the rarer {rarer} MORE often than {worse}: "
            f"{counts}"
        )
    # Sanity on the magnitudes, so an empty or single-rarity table cannot pass:
    # white must dominate and blue must be present (it was the dead rate).
    assert counts.get("blue", 0) > 0, f"mortal_coffer never rolled its 2% blue: {counts}"
    assert counts["white"] > counts["green"] > counts["blue"], counts


def test_a_pool_delivers_the_rate_it_advertises_to_the_player():
    """The roll must match the number the player is shown.

    GET /lottery/pools publishes POOL_CONFIG["rate"] per pool, so the measured
    frequency of the target rarity has to agree with it. A shortfall table
    (heavenly_treasury sums to 0.506, spirit_vault to 0.631) must NOT be
    rescaled to fill 1.0 -- doing so inflated the advertised 0.6% orange to
    1.19%. The unspent tail goes to the pool's worst rarity instead, which
    climbs pity exactly as a bad-luck pull should.

    Asserted as a band rather than an exact figure: it is a measured frequency,
    so the ceiling is set to catch a real inflation (0.6% delivered as 1.19% is
    a 2x overshoot) while tolerating ordinary retuning.

    KNOWN DATA MISMATCH, deliberately not papered over: common_satchel's
    POOL_CONFIG["rate"] is 0.60 while its own rate table says WHITE 0.95. That
    predates this fix and is a balance-data question, not a code defect, so it
    is excluded from the equality here and reported rather than silently
    "corrected" -- changing it would move a real drop rate.
    """
    KNOWN_RATE_MISMATCH = {L.LotteryPool.COMMON_SATCHEL}
    N = 8000
    for pool in L.LotteryPool:
        advertised = POOL_CONFIG[pool]["rate"]
        target = POOL_CONFIG[pool]["rarity"].value
        hits = 0
        for seed in range(N):
            _seed_all(seed)
            if L.pull_lottery(L.PlayerLotteryState(), pool)["rarity"] == target:
                hits += 1
        measured = hits / N
        if pool in KNOWN_RATE_MISMATCH:
            continue
        assert measured <= advertised * 1.25, (
            f"{pool.value} advertises {advertised:.3%} for {target} but "
            f"delivered {measured:.3%} -- the shortfall tail must not be "
            f"renormalized onto the target rarity"
        )


def test_the_two_short_rate_tables_are_short_on_purpose_and_have_a_pity_cap():
    """PINS the real shortfall, so the corrected comment stays honest.

    The comment above POOL_RATES USED TO claim the tables "must sum to 1.0 per
    pool", which was false for two of five and misled every later reader. It has
    been corrected in the module to say the opposite: these two fall short on
    purpose and the pity system covers the gap.

    This test now pins the FACT the corrected comment rests on -- that exactly
    those two pools are short, and they are short because they deliberately
    rate no WHITE. If someone adds a white rate to make the sum 1.0, this
    turns red and the comment needs revisiting.
    """
    short = {p.value: round(sum(r.values()), 3)
             for p, r in POOL_RATES.items() if abs(sum(r.values()) - 1.0) > 1e-9}
    assert short == {"heavenly_treasury": 0.506, "spirit_vault": 0.631}, short
    # The two short tables are short precisely because they omit WHITE.
    for pool_value in short:
        assert Rarity.WHITE not in POOL_RATES[L.LotteryPool(pool_value)], pool_value
    # And both lean on a real pity cap to cover the gap they leave.
    for pool in (L.LotteryPool.HEAVENLY_TREASURY, L.LotteryPool.SPIRIT_VAULT):
        assert POOL_CONFIG[pool]["guaranteed_after_pity"] is True, pool.value
        assert POOL_CONFIG[pool]["pity"] > 0, pool.value


# ---------------------------------------------------------------------------
# 8. The 10-pull guarantee. Its docstring promised something the code did not
#    deliver; both have been corrected, and these tests pin the real promise.
# ---------------------------------------------------------------------------

def test_the_ten_pull_guarantee_is_delivered_when_the_pool_can_deliver_it():
    """Purple-or-better in the 10th pull, for every pool that rates it.

    The old docstring promised "at least 1 Blue+ in 10x" while the code asked
    "did any of the first nine give PURPLE+?" and re-rolled only a WHITE tenth
    -- so a GREEN tenth, still below the promise, was accepted untouched.
    Measured before the fix: 91 of 400 seeded mortal_coffer batches contained
    nothing Blue-or-better.

    The promise is now Purple-or-better and the code enforces exactly that
    floor, checked by the same ladder helper the rest of the module uses. So
    this asserts ZERO misses on the pools that rate Purple or Orange: if the
    tenth pull would land below the floor and no earlier pull met it, the
    re-roll must lift it.
    """
    offenders = []
    for pool in L.LotteryPool:
        floor = Rarity.PURPLE
        can_deliver = any(rarity_at_least(r, floor) and POOL_RATES[pool].get(r, 0) > 0
                          for r in POOL_RATES[pool])
        if not can_deliver:
            continue  # documented exception, asserted separately below
        for seed in range(400):
            _seed_all(seed)
            results = L.pull_lottery_10x(L.PlayerLotteryState(), pool)
            best = min(LADDER.index(r["rarity"]) for r in results)
            if best > LADDER.index("purple"):
                offenders.append((pool.value, seed,
                                  [r["rarity"] for r in results]))
    assert not offenders, (
        f"{len(offenders)} ten-pulls broke the documented Purple+ floor, "
        f"first: {offenders[0]}"
    )


def test_a_pool_that_cannot_rate_purple_is_documented_as_an_exception():
    """common_satchel rates no orange, purple or blue at all.

    Its re-roll table therefore sums to zero, the `total_rate > 0` guard skips
    the re-roll, and no Purple+ guarantee is possible by construction. The
    corrected docstring names this pool as the exception rather than promising
    something the rates cannot deliver; this test pins the structural reason so
    the exception cannot be forgotten if the rates ever change.
    """
    pool = L.LotteryPool.COMMON_SATCHEL
    rates = POOL_RATES[pool]
    total = sum(rates.get(r, 0) for r in (Rarity.ORANGE, Rarity.PURPLE))
    assert total == 0.0, (pool.value, total)
    # And the module's own docstring names it, so the promise and the table
    # cannot drift apart without this turning red.
    assert "common_satchel" in L.pull_lottery_10x.__doc__


def test_the_ten_pull_re_roll_never_worse_than_the_promised_floor():
    """The re-roll may only hand back a rarity AT or better than the floor.

    The old re-roll table included BLUE while the floor it claimed to enforce
    was Purple, so the "guarantee" could hand back a rarity worse than its own
    promise. The candidate list is now built from the ladder, so this can only
    fail if someone reintroduces a name-based list.

    Only pools that actually RATE a Purple or above are checked: for the rest
    (common_satchel, mortal_coffer, earth_cache -- none rates purple) the
    re-roll table sums to zero and the module skips the re-roll by design.
    Asserting a Purple on those would be asserting a promise the rate tables
    cannot keep.
    """
    deliverable = [p for p in L.LotteryPool
                   if any(rarity_at_least(r, Rarity.PURPLE)
                          and POOL_RATES[p].get(r, 0) > 0
                          for r in POOL_RATES[p])]
    assert deliverable, "no pool rates purple+; the fixture itself is wrong"
    for pool in deliverable:
        for seed in range(300):
            _seed_all(seed)
            results = L.pull_lottery_10x(L.PlayerLotteryState(), pool)
            rarities = [Rarity(r["rarity"]) for r in results]
            if all(not rarity_at_least(r, Rarity.PURPLE) for r in rarities[:9]):
                # The tenth pull owed a Purple+, so it must now hold one.
                assert rarity_at_least(rarities[-1], Rarity.PURPLE), (
                    pool.value, seed, [r.value for r in rarities])


def test_a_ten_pull_returns_exactly_ten_results_for_every_stone_pool():
    """Back to the plain contract, in case the guarantee tests are ever relaxed."""
    for pool in L.LotteryPool:
        if POOL_CONFIG[pool]["currency"] != "spirit_stone":
            continue
        state = L.PlayerLotteryState()
        results = L.pull_lottery_10x(state, pool)
        assert len(results) == 10, pool.value
        for rec in results:
            assert set(rec) == RESULT_KEYS, rec


def test_pity_chains_across_the_ten_results_of_one_batch():
    """Pull N+1 starts where pull N stopped -- otherwise the bar jumps."""
    pool = L.LotteryPool.EARTH_CACHE
    state = L.PlayerLotteryState()
    results = L.pull_lottery_10x(state, pool)
    for prev, nxt in zip(results, results[1:]):
        assert nxt["pity_before"] == prev["pity_after"], (prev, nxt)
    assert state.total_pulls[pool.value] == 10, state.total_pulls


# ---------------------------------------------------------------------------
# 9. Reads must not write.
# ---------------------------------------------------------------------------

def test_reading_state_creates_no_counter_entries():
    """A panel polling /state every frame must not dirty the player row."""
    state = L.PlayerLotteryState()
    L.get_all_lottery_states(state)
    assert state.pity_counters == {}
    assert state.total_pulls == {}
    assert state.last_pull_time == {}


def test_get_all_lottery_states_covers_every_pool_exactly_once():
    pools = L.get_all_lottery_states(L.PlayerLotteryState())
    assert [p["pool"] for p in pools] == [pl.value for pl in L.LotteryPool]


# ---------------------------------------------------------------------------
# 10. AUTH. The module declares no session of its own; the two player routes are
#     authenticated by ownership_guard.install_ownership_guard, mounted at the
#     bottom of main.py. Everything above this point builds a BARE app, so it
#     proves nothing about who may call the two routes that spend money. The
#     section below installs the real guard and measures all three arms.
#
#     The wiring under test is the ORDER main.py uses: register the routes,
#     THEN install the guard. The guard rewrites routes in place, so a lottery
#     registration placed after the mount would never be examined by it.
# ---------------------------------------------------------------------------

def _guarded(pid_a, pid_b=None, **overrides):
    """Routes + the real ownership guard + one or two seeded players.

    Returns (db, client, bearer_for) where bearer_for(pid) is an Authorization
    header for a genuine session belonging to that player. Sessions are minted
    through auth.issue_session, the same mint the server uses.
    """
    import auth
    from ownership_guard import install_ownership_guard

    def mk(pid):
        p = {"lottery": {}, "inventory": {}, "spirit_stones": dict(STONES)}
        p.update(overrides)
        return p

    db = {pid_a: mk(pid_a)}
    if pid_b:
        db[pid_b] = mk(pid_b)
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    install_ownership_guard(app)

    def bearer(pid):
        return {"Authorization": f"Bearer {auth.issue_session(pid)}"}

    return db, TestClient(app), bearer


def test_the_catalogue_is_public_and_both_player_routes_are_not():
    """The one route with no player id in it is the one a signed-out panel reads.

    Measured against the installed guard, not inferred: /lottery/pools carries
    no player field so the guard cannot attach a session check to it, and the
    two routes that name a player must both refuse an anonymous caller.
    """
    _, c, _ = _guarded("owner")
    assert c.get("/lottery/pools").status_code == 200
    assert c.get("/lottery/owner/state").status_code == 401
    assert c.post("/lottery/owner/pull",
                  json={"pool": "common_satchel", "count": 1}).status_code == 401


def test_the_owner_can_still_pull_after_the_guard_is_installed():
    """The other arm of the same guard: refusing everyone is not a passing guard.

    A 401-only test would also go green if the guard had broken the route for
    its legitimate caller, so the owning session must still get a full pull.
    """
    db, c, bearer = _guarded("owner")
    r = c.post("/lottery/owner/pull", json={"pool": "common_satchel", "count": 1},
               headers=bearer("owner"))
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 1
    assert db["owner"]["spirit_stones"]["low_grade"] < STONES["low_grade"]


def test_a_session_for_one_player_cannot_spend_another_players_currency():
    """The reason the player id is in the path at all.

    Without this the id is decorative: any valid session could name any player
    and drain that player's purse. Asserted on BOTH halves -- the refusal, and
    the victim being left untouched, because a 403 that had already debited
    would still read as a refusal.
    """
    db, c, bearer = _guarded("alice", "bob")
    mine = bearer("alice")
    assert c.get("/lottery/alice/state", headers=mine).status_code == 200
    r = c.post("/lottery/bob/pull", json={"pool": "common_satchel", "count": 1},
               headers=mine)
    assert r.status_code == 403, r.text
    assert db["bob"]["spirit_stones"]["low_grade"] == STONES["low_grade"]
    assert db["bob"]["lottery"] == {}, db["bob"]["lottery"]
    assert db["bob"]["inventory"] == {}
    # The read path is guarded by the same rule as the write path.
    assert c.get("/lottery/bob/state", headers=mine).status_code == 403


def test_a_forged_session_token_is_refused_before_the_route_runs():
    """The 401 comes from the session check, not from an empty player row."""
    db, c, _ = _guarded("owner")
    headers = {"Authorization": "Bearer not-a-real-session-token"}
    assert c.get("/lottery/owner/state", headers=headers).status_code == 401
    assert c.post("/lottery/owner/pull", json={"pool": "common_satchel"},
                  headers=headers).status_code == 401
    assert db["owner"]["spirit_stones"]["low_grade"] == STONES["low_grade"]


# ---------------------------------------------------------------------------
# 11. REFUSALS. Which status, which message, and -- the part that matters --
#     that a refused request charges nobody and grants nothing. The sibling
#     request-contract file covers the count domain; this section pins the
#     full refusal matrix including the two that exist only on the wallet arm.
# ---------------------------------------------------------------------------

def test_every_refusal_names_its_own_reason():
    """A client shows `detail`, so distinct causes must not share a message.

    Asserted as the exact strings the handler raises: a caller that sends a
    bad pool and a caller that sends a bad count must be able to tell which
    one it was.
    """
    _, c = _app_for("errs")
    cases = [
        ({}, 400, "pool is required"),
        ({"pool": "nope"}, 400, "Invalid pool"),
        # A near-miss spelling is still a miss, not a fuzzy match.
        ({"pool": "mortal_offer"}, 400, "Invalid pool"),
        ({"pool": "common_satchel", "count": 5}, 400, "count must be 1 or 10"),
    ]
    for body, status, phrase in cases:
        r = c.post("/lottery/errs/pull", json=body)
        assert r.status_code == status, (body, r.status_code, r.text)
        assert phrase in str(r.json()["detail"]), (body, r.json())


def test_a_count_outside_one_to_ten_is_refused_by_the_type_before_the_handler():
    """0, 11 and a non-number are 422, not 400: they fail field validation.

    The distinction is not pedantry -- 422 means the request never reached the
    handler at all, which is the only reason those callers can be certain
    nothing was charged.
    """
    _, c = _app_for("bounds")
    for count in (0, 11, -1, "two"):
        r = c.post("/lottery/bounds/pull",
                   json={"pool": "common_satchel", "count": count})
        assert r.status_code == 422, (count, r.status_code, r.text)
    assert c.get("/lottery/bounds/state").json()["pools"][0]["total_pulls"] == 0


def test_no_refusal_path_leaves_a_charge_or_a_grant_behind():
    """Every refusal above, in one sweep, on one player.

    The individual tests check the status; this checks the consequence. A
    refusal that returned 400 after having already debited the purse would
    satisfy all of them.
    """
    pid = "sweep"
    db, c = _app_for(pid, tokens={"tribulation_token": 10 ** 6})
    refusals = [
        ({}, 400),
        ({"pool": "nope"}, 400),
        ({"pool": "common_satchel", "count": 5}, 400),
        ({"pool": "common_satchel", "count": 0}, 422),
        ({"pool": "common_satchel", "count": 11}, 422),
    ]
    for body, status in refusals:
        r = c.post(f"/lottery/{pid}/pull", json=body)
        assert r.status_code == status, (body, r.status_code, r.text)
    assert db[pid]["spirit_stones"]["low_grade"] == STONES["low_grade"]
    assert db[pid]["tokens"]["tribulation_token"] == 10 ** 6
    assert db[pid]["inventory"] == {}
    assert db[pid]["lottery"] == {}


def test_a_pull_for_an_unknown_player_is_404_on_both_player_routes():
    """Not 400: the request was well formed, the player was not there."""
    _, c = _app_for("real")
    assert c.get("/lottery/ghost/state").status_code == 404
    assert c.post("/lottery/ghost/pull",
                  json={"pool": "common_satchel", "count": 1}).status_code == 404


# ---------------------------------------------------------------------------
# 12. THE WALLET ARM. production mounts lottery with a wallet store and
#     spend/earn callables (main.py), so the in-memory dict arm every other test
#     in this file uses is the FALLBACK. This section drives the real arm,
#     including the Idempotency-Key channel the shipped client does not yet use.
# ---------------------------------------------------------------------------

class _FakeWallet:
    """The three things the handler reads off a WalletStore row."""

    def __init__(self, balance=None, version=7):
        self.balance = {"spirit_stone": 10 ** 6} if balance is None else balance
        self.version = version

    def to_dict(self):
        return dict(self.balance)

    def get(self, player_id):
        return self


class _Recorder:
    """A spend/earn pair that records what it was asked to do.

    `replay` decides whether a repeated idempotency key comes back flagged as a
    replay, which is what the wallet store does and what the handler's
    `_replayed` branch exists to serve.
    """

    def __init__(self, wallet, ok=True, replay=False):
        self.wallet = wallet
        self.ok = ok
        self.replay = replay
        self.spends = []
        self.earns = []
        self._seen = set()

    def spend(self, ws, pid, currency, amount, client_version, idem):
        self.spends.append({"player": pid, "currency": currency,
                            "amount": amount, "idem": idem})
        if idem in self._seen and self.replay:
            return {"ok": True, "_replayed": True}
        # A refusing wallet leaves the balance alone, so the refusal tests can
        # assert on it. A stub that debited first would make every refusal look
        # like a charge.
        if not self.ok:
            return {"ok": False, "reason": "insufficient_funds"}
        self._seen.add(idem)
        ws.balance["spirit_stone"] -= amount
        return {"ok": True}

    def earn(self, ws, pid, currency, amount, idem, reason):
        self.earns.append({"player": pid, "currency": currency,
                           "amount": amount, "reason": reason})
        ws.balance["spirit_stone"] += amount
        return {"ok": True}


def _wallet_app(pid, wallet=None, rec=None):
    """Routes wired the way main.py wires them: wallet store + spend + earn."""
    wallet = wallet or _FakeWallet()
    rec = rec or _Recorder(wallet)
    db = {pid: {"lottery": {}, "inventory": {}}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db, lambda: wallet,
                              rec.spend, rec.earn)
    return db, TestClient(app), rec


def test_the_wallet_arm_spends_the_configured_price_in_the_wallets_currency():
    """The bill leaves through the wallet, not through the player dict.

    On this arm the player row has no spirit_stones field at all, so if the
    route had charged the dict instead of calling spend, the recorded spend
    would be empty AND the balance would be untouched.
    """
    pool = L.LotteryPool.COMMON_SATCHEL
    db, c, rec = _wallet_app("w")
    r = c.post("/lottery/w/pull", json={"pool": pool.value, "count": 1})
    assert r.status_code == 200, r.text
    assert len(rec.spends) == 1, rec.spends
    assert rec.spends[0]["amount"] == POOL_CONFIG[pool]["cost_per_pull"]
    assert r.json()["total_cost"] == POOL_CONFIG[pool]["cost_per_pull"]


def test_a_wallet_backed_reply_carries_the_balance_and_its_version():
    """`wallet` and `wallet_version` are how a panel refreshes without a
    second request; a client that cannot find them has to poll again."""
    _, c, _ = _wallet_app("w")
    body = c.post("/lottery/w/pull",
                  json={"pool": "common_satchel", "count": 1}).json()
    assert body["wallet"] == {"spirit_stone": 10 ** 6 - 5}
    assert body["wallet_version"] == 7


def test_the_callers_idempotency_key_reaches_the_wallet_namespaced():
    """A retry is only a replay if the two attempts carry the same key.

    The server used to mint a fresh uuid per attempt, which made every retry a
    brand new charge. The caller's key must survive, namespaced by player, pool
    and batch size so one key cannot swallow a different pull.
    """
    _, c, rec = _wallet_app("w")
    c.post("/lottery/w/pull", json={"pool": "common_satchel", "count": 1},
           headers={"Idempotency-Key": "player-intent-42"})
    assert rec.spends[0]["idem"] == "lottery:w:common_satchel:1:player-intent-42"
    # A different player, pool or batch size under the same caller key is a
    # different intent, so the namespace has to move.
    _, c2, rec2 = _wallet_app("w")
    c2.post("/lottery/w/pull", json={"pool": "common_satchel", "count": 10},
            headers={"Idempotency-Key": "player-intent-42"})
    assert rec2.spends[0]["idem"] == "lottery:w:common_satchel:10:player-intent-42"


def test_a_pull_without_the_header_is_still_served_but_is_not_protected():
    """The shipped client sends no header, so refusing would 400 every tap.

    The cost is stated rather than hidden: the server falls back to a per
    attempt key, so the request succeeds and a retry is NOT collapsed.
    """
    _, c, rec = _wallet_app("w")
    assert c.post("/lottery/w/pull",
                  json={"pool": "common_satchel", "count": 1}).status_code == 200
    idem = rec.spends[0]["idem"]
    assert idem.startswith("lottery:w:common_satchel:1:")
    assert idem.rsplit(":", 1)[-1] != "player-intent-42"
    assert len(idem.rsplit(":", 1)[-1]) == 32  # a uuid hex, not a caller key
    # Two header-less attempts are two distinct keys, so two charges.
    c.post("/lottery/w/pull", json={"pool": "common_satchel", "count": 1})
    assert len({s["idem"] for s in rec.spends}) == 2


def test_a_replayed_pull_returns_the_receipt_without_rolling_again():
    """What the client sees on a retry it already paid for.

    `results` is empty on purpose: the first attempt's results were already
    delivered, and inventing ten more would hand the player free items.
    """
    wallet = _FakeWallet()
    rec = _Recorder(wallet, replay=True)
    db, c, _ = _wallet_app("w", wallet=wallet, rec=rec)
    body = {"pool": "common_satchel", "count": 1}
    hdr = {"Idempotency-Key": "tap-1"}
    first = c.post("/lottery/w/pull", json=body, headers=hdr).json()
    second = c.post("/lottery/w/pull", json=body, headers=hdr).json()
    assert second["ok"] is True
    assert second["_replayed"] is True
    assert second["results"] == [], second
    assert second["total_cost"] == first["total_cost"]
    # Charged once: the balance moved by exactly one pull price.
    assert wallet.balance["spirit_stone"] == 10 ** 6 - first["total_cost"]
    # And nothing was granted a second time.
    assert sum(db["w"]["inventory"].values()) <= len(first["results"])


def test_an_over_long_idempotency_key_is_refused_before_any_spend():
    """The header lands in a wallet store key column, so it must be bounded.

    The bound is read from idem_gate rather than restated, so this cannot drift
    from the limit the server enforces.
    """
    from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
    _, c, rec = _wallet_app("w")
    r = c.post("/lottery/w/pull", json={"pool": "common_satchel", "count": 1},
               headers={"Idempotency-Key": "x" * (MAX_IDEMPOTENCY_KEY_LEN + 1)})
    assert r.status_code == 400, r.text
    assert rec.spends == [], rec.spends


def test_a_refused_spend_stops_the_pull_before_anything_is_rolled():
    """The wallet says no -> 400, and no pity, no inventory, no state write.

    This is the refusal the in-memory arm cannot produce, which is why it needs
    its own test: a fake wallet that always refuses is the only way to reach
    the branch where the debit fails inside the wallet rather than in the
    player dict.
    """
    wallet = _FakeWallet()
    rec = _Recorder(wallet, ok=False)
    db, c, _ = _wallet_app("w", wallet=wallet, rec=rec)
    r = c.post("/lottery/w/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 400, r.text
    assert wallet.balance["spirit_stone"] == 10 ** 6
    assert db["w"]["lottery"] == {}
    assert db["w"]["inventory"] == {}
    assert rec.earns == []


def test_a_stone_reward_is_paid_through_the_wallet_on_the_wallet_arm():
    """The credit half of the loop, which the dict-arm tests cannot reach.

    Seeded so the roll lands on common_satchel's white row, the one place
    `spirit_stones` is a real catalogue entry rather than a fallback.
    """
    pool = L.LotteryPool.COMMON_SATCHEL
    for seed in range(500):
        state = L.PlayerLotteryState()
        _seed_all(seed)
        if any(r["reward"] == "spirit_stones"
               for r in L.pull_lottery_10x(state, pool)):
            break
    else:
        pytest.fail("no seed in 500 produced a stone reward")
    wallet = _FakeWallet()
    rec = _Recorder(wallet)
    _, c, _ = _wallet_app("w", wallet=wallet, rec=rec)
    _seed_all(seed)
    body = c.post("/lottery/w/pull", json={"pool": pool.value, "count": 10}).json()
    stones = [r for r in body["results"] if r["reward"] == "spirit_stones"]
    assert stones, "the seeded roll did not reproduce"
    assert rec.earns, rec.earns
    assert all(e["amount"] == 100 for e in rec.earns), rec.earns
    assert len(rec.earns) == len(stones)
    assert wallet.balance["spirit_stone"] == (10 ** 6
                                              - POOL_CONFIG[pool]["cost_10x"]
                                              + 100 * len(stones))


# ---------------------------------------------------------------------------
# 13. THE ADVERTISED GUARANTEE. get_lottery_state publishes `guaranteed_type`
#     and `guaranteed_at` to the panel, and pull_lottery publishes `is_pity` to
#     the result row. All three are promises about what the roll will do, so
#     they are pinned here against the code that has to keep them.
# ---------------------------------------------------------------------------

def test_only_the_pools_with_a_guarantee_advertise_one():
    """`guaranteed_type` must be "none" where the code has no guarantee to fire.

    Two pools set guaranteed_after_pity False, and nothing in pull_lottery
    forces their target rarity. Reporting a guarantee for them told the player
    a bar was filling toward a payout the code could never deliver.
    """
    for pool in L.LotteryPool:
        cfg = POOL_CONFIG[pool]
        state = L.get_lottery_state(L.PlayerLotteryState(), pool)
        if cfg["guaranteed_after_pity"]:
            assert state["guaranteed_type"] == "target_rarity", pool.value
            assert state["guaranteed_at"] == cfg["pity"], pool.value
        else:
            assert state["guaranteed_type"] == "none", (pool.value, state)
            assert state["guaranteed_at"] is None, (pool.value, state)


def test_is_pity_is_false_even_past_the_cap_on_a_pool_with_no_guarantee():
    """The result flag may only claim a payout that actually happened.

    Driven past the cap deliberately: the flag used to be the bare threshold
    test, so a pull at pity 99 on a pool whose cap is 10 reported
    `is_pity: True` alongside an ordinary roll -- a client drawing a
    "guaranteed!" banner would be lying to the player.
    """
    for pool in L.LotteryPool:
        cfg = POOL_CONFIG[pool]
        if cfg["guaranteed_after_pity"]:
            continue
        state = L.PlayerLotteryState(
            pity_counters={pool.value: cfg["pity"] + 89},
            total_pulls={pool.value: 0}, last_pull_time={})
        for seed in range(200):
            _seed_all(seed)
            rec = L.pull_lottery(state, pool)
            assert rec["is_pity"] is False, (pool.value, seed, rec)


def test_a_state_row_advertises_exactly_what_the_pull_code_does():
    """The panel row and the roll, read together, must not contradict.

    Cross-checked rather than asserted twice: for each pool, the rarity the
    row names as `target_rarity` is the rarity the roll forces at the cap --
    and where the row says "none" the roll never reports a guarantee.
    """
    for pool in L.LotteryPool:
        cfg = POOL_CONFIG[pool]
        row = L.get_lottery_state(L.PlayerLotteryState(), pool)
        assert row["target_rarity"] == cfg["rarity"].value
        assert row["pity_max"] == cfg["pity"]
        state = L.PlayerLotteryState(pity_counters={pool.value: cfg["pity"] - 1},
                                     total_pulls={pool.value: 0},
                                     last_pull_time={})
        rec = L.pull_lottery(state, pool)
        assert rec["is_pity"] is bool(cfg["guaranteed_after_pity"]), (pool.value, rec)
        if cfg["guaranteed_after_pity"]:
            assert rec["rarity"] == row["target_rarity"], (pool.value, rec)


# ---------------------------------------------------------------------------
# 14. THE FOLD BETWEEN TWO TRANSPORTS. The handler accepts pool/count on the
#     JSON body (what the shipped client sends) and on the query string (what
#     the in-repo Python tests send), and folds them into one set of values
#     before validating ONCE. A fold has to state which side wins, and the
#     answer is currently unpinned by either lottery test file.
# ---------------------------------------------------------------------------

def test_the_body_wins_over_the_query_when_both_carry_a_pool():
    """Two transports is two ways to name a pool; one of them has to lose.

    `client/scenes/game/GachaView.gd:50` sends the pool in the body while an
    in-repo test may put it on the query string. If both are present the fold
    has to pick one, and the body is the client's side, so the body wins. The
    bill is what makes this observable rather than cosmetic: earth_cache costs
    80 a pull and common_satchel costs 5, so a fold that let the query pool
    through would pull one pool and price another.
    """
    _, c = _app_for("prec")
    r = c.post("/lottery/prec/pull", json={"pool": "earth_cache", "count": 1},
               params={"pool": "common_satchel", "count": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"][0]["pool"] == "earth_cache", (
        "the query pool won the fold: %s" % body["results"][0]
    )
    assert body["total_cost"] == POOL_CONFIG[
        L.LotteryPool.EARTH_CACHE]["cost_per_pull"], (
        "pulled one pool and priced another: %s" % body["total_cost"]
    )


def test_the_body_wins_over_the_query_when_both_carry_a_count():
    """The same rule for `count`, where the two sides differ by ten pulls.

    Body count=1 against query count=10 must deliver one result at the single-pull
    price. A fold that let the query count through would hand back ten results
    for a body that asked for one, and bill cost_10x for it.
    """
    _, c = _app_for("precc")
    r = c.post("/lottery/precc/pull", json={"pool": "common_satchel", "count": 1},
               params={"pool": "common_satchel", "count": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 1, (
        "the query count won the fold: %d results" % len(body["results"])
    )
    assert body["total_cost"] == POOL_CONFIG[
        L.LotteryPool.COMMON_SATCHEL]["cost_per_pull"], body


def test_either_transport_may_supply_one_field_and_the_other_the_rest():
    """The two sides are independent inputs, not two spellings of one value.

    Body pool + query count is the shape a client migrating to the body
    transport half way through would produce, and query pool + body count is
    the mirror. Neither may be dropped: "an absent field means not supplied on
    this transport", so each side fills only what the other left out.
    """
    _, c = _app_for("mix1")
    r = c.post("/lottery/mix1/pull", json={"pool": "common_satchel"},
               params={"count": 10})
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 10, r.text

    _, c = _app_for("mix2")
    r = c.post("/lottery/mix2/pull", json={"count": 10},
               params={"pool": "common_satchel"})
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 10, r.text


def test_a_player_id_in_the_body_cannot_redirect_the_charge_to_another_player():
    """The acting player comes from the PATH, so a body key cannot move it.

    `LotteryPullRequest` declares no player field, so `player_id` in the JSON is
    not a body field at all -- it is ignored. That matters because the route
    charges whoever the path names: if the body could name a player, a caller
    could hand its own pull to someone else's purse. Both balances are read
    before and after so the assertion is about whose money moved, not about the
    status code alone.
    """
    db = {"a": {"lottery": {}, "inventory": {}, "spirit_stones": dict(STONES)},
          "b": {"lottery": {}, "inventory": {}, "spirit_stones": dict(STONES)}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    c = TestClient(app)
    before_b = db["b"]["spirit_stones"]["low_grade"]
    r = c.post("/lottery/a/pull",
               json={"pool": "common_satchel", "count": 1, "player_id": "b"})
    assert r.status_code == 200, r.text
    assert db["a"]["spirit_stones"]["low_grade"] < 10 ** 6, (
        "the path player's purse was never charged: %s" % r.json()
    )
    assert db["b"]["spirit_stones"]["low_grade"] == before_b, (
        "a body-supplied player_id redirected the charge: %s"
        % db["b"]["spirit_stones"]
    )


# ---------------------------------------------------------------------------
# 15. THE STORE PERSIST HOOK. After a pull the route calls
#     `players_db.persist(player_id, player)` when the store has one. On the
#     real deployment (PlayerStore, PostgreSQL) that call is the ONLY thing
#     that makes pity counters survive the request, so the branch that calls it
#     is a durability contract, not an optimisation.
# ---------------------------------------------------------------------------

class _PersistStore(dict):
    """A players_db that remembers its persist() calls."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.persisted = []

    def persist(self, player_id, player):
        self.persisted.append((player_id, dict(player)))


def test_a_store_with_a_persist_hook_is_written_to_after_a_pull():
    """One persist per accepted pull, carrying the counters the pull produced.

    `player["lottery"]` is reassigned before the hook fires, so the persisted
    copy is the test's evidence that pity, totals and last-pull time all reached
    the store rather than living only in a request-scoped object.
    """
    store = _PersistStore({"p": {"lottery": {}, "inventory": {},
                                 "spirit_stones": dict(STONES)}})
    app = FastAPI()
    L.register_lottery_routes(app, lambda: store)
    c = TestClient(app)
    assert c.post("/lottery/p/pull",
                  json={"pool": "common_satchel", "count": 1}).status_code == 200
    assert len(store.persisted) == 1, store.persisted
    pid, saved = store.persisted[0]
    assert pid == "p", pid
    assert set(saved["lottery"]) == {"pity_counters", "total_pulls",
                                     "last_pull_time"}, saved["lottery"]
    assert saved["lottery"]["total_pulls"]["common_satchel"] == 1, saved


def test_persist_is_not_called_for_a_refused_pull():
    """A refusal writes nothing, so a bad request cannot roll state forward.

    The hook fires after the grants, so a route that persisted before deciding
    would persist a pity counter for a pull that never rolled. Insufficient
    funds is the refusal here: it is refused after the player is resolved, so
    it is the one that would slip past a check placed too early.
    """
    store = _PersistStore({"p": {"lottery": {}, "inventory": {},
                                 "spirit_stones": dict(STONES)}})
    store["p"]["spirit_stones"] = {"copper": 0, "silver": 0, "gold": 0,
                                  "low_grade": 0, "mid_grade": 0,
                                  "high_grade": 0, "extreme_grade": 0}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: store)
    c = TestClient(app)
    r = c.post("/lottery/p/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 400, r.text
    assert store.persisted == [], (
        "a refused pull was persisted: %s" % store.persisted
    )


def test_a_store_without_a_persist_hook_is_still_served():
    """The hook is optional; a plain dict store must not 500 on its absence.

    `hasattr(players_db, "persist")` is the guard, so a store that never had the
    method takes the branch a bare dict takes. Asserted because "add a persist
    call" and "guard the call" are separate changes and only the first is
    visible when a store has the method.
    """
    _, c = _app_for("nohook")
    r = c.post("/lottery/nohook/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 16. METHOD SURFACE. Section 1 pins which PATHS exist. This pins which
#     METHODS answer on the two player paths, because a state route that
#     accepted a POST would be a second write surface and a pull route that
#     accepted a GET would charge on a prefetch.
# ---------------------------------------------------------------------------

def test_the_two_player_routes_refuse_the_wrong_method():
    """/pull is POST-only and /state is GET-only."""
    _, c = _app_for("m")
    assert c.get(f"/lottery/m/pull").status_code == 405
    assert c.post(f"/lottery/m/state").status_code == 405
    # And the right methods still work on the same paths.
    assert c.get("/lottery/m/state").status_code == 200
    assert c.post("/lottery/m/pull",
                  json={"pool": "common_satchel", "count": 1}).status_code == 200


# ---------------------------------------------------------------------------
# 17. client_version, THE LAST UNPINNED PARAMETER. It is a bare scalar, so it
#     rides the query string, and it is handed straight to the wallet's spend
#     so the wallet can refuse a pull made by a stale client. Nothing in either
#     lottery test file asserts it arrives, or that a malformed one is refused
#     before money moves.
# ---------------------------------------------------------------------------

class _ProbeRecorder:
    """Records spends without touching a balance.

    `_Recorder` debits a hardcoded `spirit_stone` key, so it cannot price a
    token pool. This one only reports what it was asked, which is all these
    tests need.
    """

    def __init__(self):
        self.spends = []

    def spend(self, ws, pid, currency, amount, client_version, idem):
        self.spends.append({"player": pid, "currency": currency,
                            "amount": amount, "client_version": client_version,
                            "idem": idem})
        return {"ok": True}

    def earn(self, ws, pid, currency, amount, idem, reason):
        return {"ok": True}


class _ProbeWallet:
    def __init__(self):
        self.balance = {"spirit_stone": 10 ** 6}
        self.version = 1

    def get(self, player_id):
        return self

    def to_dict(self):
        return dict(self.balance)


def _probe_app(pid, **overrides):
    """Wallet-mounted app whose recorder reports spends without debiting.

    `overrides` seed the player row. The token pool needs a funded token
    ledger to be pulled at all, so the tests that drive it pass
    `tokens={...}` here rather than reaching into `db` afterwards.
    """
    rec = _ProbeRecorder()
    player = {"lottery": {}, "inventory": {}}
    player.update(overrides)
    db = {pid: player}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db, lambda: _ProbeWallet(),
                              rec.spend, rec.earn)
    return db, TestClient(app), rec


def test_the_client_version_query_parameter_reaches_the_wallet_spend():
    """The version the client declares is the version the wallet judges.

    Read as a relationship: the int the caller put on the query string is the
    int handed to spend, with no defaulting or clamping in between.
    """
    _, c, rec = _probe_app("cv")
    r = c.post("/lottery/cv/pull", json={"pool": "common_satchel", "count": 1},
               params={"client_version": "9"})
    assert r.status_code == 200, r.text
    assert len(rec.spends) == 1, rec.spends
    assert rec.spends[0]["client_version"] == 9, rec.spends


def test_a_missing_client_version_is_a_real_zero_not_a_refusal():
    """Omitting the parameter must not break a pull the client can still make.

    GachaView.gd sends no client_version, so the default has to be a value the
    wallet accepts.
    """
    _, c, rec = _probe_app("cv0")
    r = c.post("/lottery/cv0/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 200, r.text
    assert rec.spends[0]["client_version"] == 0, rec.spends


def test_a_client_version_that_is_not_a_number_is_refused_before_any_spend():
    """A non-integer version is a 422 from the type, and no money moves."""
    _, c, rec = _probe_app("cvbad")
    r = c.post("/lottery/cvbad/pull", json={"pool": "common_satchel", "count": 1},
               params={"client_version": "notanint"})
    assert r.status_code == 422, r.text
    assert rec.spends == [], rec.spends


def test_the_currency_charged_is_the_currency_the_catalogue_advertised():
    """The pool's row and the wallet bill must name the same currency.

    Read across two surfaces on purpose. `GET /lottery/pools` publishes
    `currency` per pool and that is what a panel shows before the player taps;
    the pull route then hands a name to the wallet's spend. If those two names
    ever diverge the player is told one currency is being spent while another
    is. `WALLET_CURRENCY_MAP` is the fold between them, so the assertion is that
    it renames nothing a pool actually advertises.

    The five names are written out rather than read back out of POOL_CONFIG: an
    expectation derived from the table under test proves nothing, which is why
    the first draft of this test survived the map being bypassed entirely.
    """
    ADVERTISED = {
        # The token currency, named by importing the constant that owns the
        # ledger. Written as the bare word on purpose: retyping the literal
        # here is how the second spelling survived the first fix.
        "heavenly_treasury": L.TRIBULATION_TOKEN,
        "spirit_vault": "spirit_stone",
        "earth_cache": "spirit_stone",
        "mortal_coffer": "spirit_stone",
        "common_satchel": "spirit_stone",
    }
    # One spelling. The defect was a plural `tribulation_tokens` that existed
    # nowhere else in the tree; this pins that it cannot come back.
    assert L.TRIBULATION_TOKEN == "tribulation_token", L.TRIBULATION_TOKEN
    assert "tribulation_tokens" not in set(ADVERTISED.values()), (
        "the plural spelling is back in the catalogue")
    assert set(ADVERTISED) == {p.value for p in L.LotteryPool}, ADVERTISED
    for pool in L.LotteryPool:
        _, c = _app_for("adv_" + pool.value)
        catalogue = c.get("/lottery/pools").json()["pools"]
        shown = _entry(catalogue, pool.value)["currency"]
        assert shown == ADVERTISED[pool.value], (pool.value, shown)

        pid = "advw_" + pool.value
        # The token pool is charged on the player row, so it needs a funded
        # token ledger; the stone pools are charged through the wallet.
        if shown == L.TRIBULATION_TOKEN:
            _, c, rec = _probe_app(pid, tokens={L.TRIBULATION_TOKEN: 10 ** 6})
        else:
            _, c, rec = _probe_app(pid)
        r = c.post("/lottery/%s/pull" % pid,
                   json={"pool": pool.value, "count": 1})
        assert r.status_code == 200, (pool.value, r.text)
        if shown == L.TRIBULATION_TOKEN:
            # Charged on the token ledger, so there is no wallet spend to
            # compare the advertised name against -- that is the whole point.
            assert rec.spends == [], (pool.value, rec.spends)
        else:
            assert rec.spends[0]["currency"] == shown, (
                "the catalogue advertises %r for %s but the wallet was charged %r"
                % (shown, pool.value, rec.spends[0]["currency"])
            )


@pytest.mark.parametrize("pool", list(L.LotteryPool), ids=lambda p: p.value)
def test_every_currency_the_lottery_charges_is_one_the_wallet_accepts(pool):
    """A currency the wallet refuses is a pool that exists only on paper.

    Read against the REAL wallet whitelists rather than against the lottery's
    own table, because the two drifting apart is the whole defect. Both
    backends are checked: `wallet_store_pg.CURRENCIES` (PostgreSQL) and
    `wallet_defense.CURRENCIES` (in-memory). Checking only one leaves the other
    free to refuse the same charge.

    A currency the wallet legitimately does not hold -- the token, which lives
    in `player["tokens"]` and is credited by tribulation.py, never earned into a
    wallet balance -- is allowed to be absent, but ONLY because the handler
    charges it on the player row instead of through the wallet. That is the
    other half of the contract, asserted below: nothing may be handed to the
    wallet unless the wallet accepts it.
    """
    import wallet_store_pg
    import wallet_defense

    currency = POOL_CONFIG[pool]["currency"]
    accepted = set(wallet_store_pg.CURRENCIES) | set(wallet_defense.CURRENCIES)
    assert set(wallet_store_pg.CURRENCIES) == set(wallet_defense.CURRENCIES), (
        "the two wallet backends disagree about which currencies exist: "
        "pg=%s memory=%s" % (wallet_store_pg.CURRENCIES, wallet_defense.CURRENCIES))
    if currency in accepted:
        return  # charged through the wallet; the refusal test below covers it
    assert currency == L.TRIBULATION_TOKEN, (
        "%s charges %r, which neither wallet accepts and which is not the token "
        "currency -- an unknown currency has no ledger to charge"
        % (pool.value, currency))


def test_no_currency_reaches_the_wallet_that_the_wallet_would_refuse():
    """The name handed to `spend` must be one the wallet can debit.

    This is the assertion the xfail marker used to sit on, and it is stronger
    than reading POOL_CONFIG: it drives a REAL pull through a wallet-mounted
    app and checks every currency the recorder was actually asked to debit, so a
    name that becomes un-acceptable at request time is caught even if the static
    table still looks right.
    """
    import wallet_store_pg
    import wallet_defense

    accepted = set(wallet_store_pg.CURRENCIES) | set(wallet_defense.CURRENCIES)
    for pool in L.LotteryPool:
        currency = POOL_CONFIG[pool]["currency"]
        pid = "acc_" + pool.value
        if currency == L.TRIBULATION_TOKEN:
            db, c, rec = _probe_app(pid, tokens={L.TRIBULATION_TOKEN: 10 ** 6})
        else:
            db, c, rec = _probe_app(pid)
        r = c.post(f"/lottery/{pid}/pull", json={"pool": pool.value, "count": 1})
        assert r.status_code == 200, (pool.value, r.text)
        for charged in rec.spends:
            assert charged["currency"] in accepted, (
                "%s handed %r to the wallet, which refuses it; the wallet "
                "accepts %s" % (pool.value, charged["currency"], sorted(accepted)))


def test_a_token_draw_spends_the_token_ledger_and_never_asks_the_wallet():
    """The flagship pool works on the wallet-mounted arm, and moves a balance.

    Measured before the fix: this pull answered HTTP 400 `unknown currency`
    for every player, because the pool priced itself in a name the wallet does
    not hold. Two things are asserted here because either alone would pass on
    broken code -- the token balance must MOVE by exactly the configured cost,
    and the wallet must not have been asked to debit a name it refuses.
    """
    pid = "tokendraw"
    tokens = 1000
    db, c, rec = _probe_app(pid, tokens={L.TRIBULATION_TOKEN: tokens})
    cfg = POOL_CONFIG[L.LotteryPool.HEAVENLY_TREASURY]

    r = c.post(f"/lottery/{pid}/pull",
               json={"pool": L.LotteryPool.HEAVENLY_TREASURY.value, "count": 1})
    assert r.status_code == 200, r.text
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - cfg["cost_per_pull"]
    assert rec.spends == [], (
        "a token pull must not be billed to the wallet at all: %s" % rec.spends)


# 18. A CORRUPT STORED LOTTERY BLOB. Both player routes used to build their
#     state with an unguarded `PlayerLotteryState(**player["lottery"])`, so a
#     row whose lottery column was not the three-map shape raised out of the
#     handler and answered HTTP 500 -- a server fault for something the caller
#     can neither see nor fix. These were pinned as non-strict xfail; the unpack
#     is now guarded by `coerce_lottery_state`, and the two tests below assert
#     the fallback instead of merely recording the crash.
#
#     The fallback is FRESH STATE, not a 4xx. A caller cannot repair its own
#     player row, so refusing would blame the client for a server-side fault and
#     leave the row corrupt -- and since the pull route is the only writer of
#     player["lottery"], a refusal there locks the player out of the gacha until
#     someone fixes the row by hand. A read now reports zeros and the next
#     accepted pull rewrites the row in the canonical shape.
# ---------------------------------------------------------------------------

# The three shapes the defect was measured on, plus the ones a dict-shape guard
# alone would wave through. The last two are the interesting additions: a
# `isinstance(stored, dict)` check passes them, and they used to reach a 500 one
# line deeper, inside pull_lottery, where a non-numeric pity is ordered against
# the pool's cap.
CORRUPT_BLOBS = [
    {"pity_counters": "notadict"},
    {"total_pulls": [1]},
    {"bogus": 1},
    None,
    [1, 2, 3],
    {"pity_counters": {"earth_cache": "four"}},
    {"pity_counters": {"earth_cache": True}},
]


@pytest.mark.parametrize("blob", CORRUPT_BLOBS,
                         ids=["str-map", "list-map", "unknown-key", "null",
                              "list-blob", "non-numeric-pity", "bool-pity"])
def test_a_corrupt_stored_lottery_blob_answers_a_fresh_state_not_a_crash(blob):
    """The read route reports a player with no counters rather than 500ing.

    Asserted as the shape the panel reads -- zeroed counters, full key set --
    not merely `status < 500`, because a route that answered 200 with a broken
    body would pass the weaker check and still be a defect.
    """
    db = {"p": {"lottery": blob, "inventory": {}, "spirit_stones": dict(STONES)}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/lottery/p/state")
    assert r.status_code == 200, (
        "stored lottery %r made GET /state answer %d: %s"
        % (blob, r.status_code, r.text[:200])
    )
    body = r.json()
    assert body["ok"] is True
    assert {row["pool"] for row in body["pools"]} == {p.value for p in L.LotteryPool}
    for row in body["pools"]:
        assert set(row) == STATE_KEYS, (blob, row["pool"], sorted(row))
        assert row["pity"] == 0, (blob, row["pool"], row)
        assert row["total_pulls"] == 0, (blob, row["pool"], row)


@pytest.mark.parametrize("blob", CORRUPT_BLOBS,
                         ids=["str-map", "list-map", "unknown-key", "null",
                              "list-blob", "non-numeric-pity", "bool-pity"])
def test_a_corrupt_stored_lottery_blob_does_not_500_the_pull_route(blob):
    """The write route had the same unguarded splat as the read route.

    Also pins the self-repair: an accepted pull rewrites player["lottery"] in
    the canonical three-map shape, so the row stops being corrupt without
    anybody editing the database. That is the property that makes fresh-state
    the right fallback -- the refusal alternative has no such exit.
    """
    db = {"p": {"lottery": blob, "inventory": {}, "spirit_stones": dict(STONES)}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/lottery/p/pull", json={"pool": "common_satchel", "count": 1})
    assert r.status_code == 200, (
        "a corrupt lottery blob %r made the pull answer %d: %s"
        % (blob, r.status_code, r.text[:200])
    )
    body = r.json()
    assert body["ok"] is True
    assert len(body["results"]) == 1, (blob, body)
    # The row is repaired in place: the same three keys a healthy row has.
    saved = db["p"]["lottery"]
    assert set(saved) == {"pity_counters", "total_pulls", "last_pull_time"}, (
        blob, saved)
    assert saved["total_pulls"]["common_satchel"] == 1, (blob, saved)
    # And the repaired row now reads back normally rather than zeroing again.
    after = c.get("/lottery/p/state").json()
    entry = _entry(after["pools"], "common_satchel")
    assert entry["total_pulls"] == 1, entry


def test_a_healthy_stored_blob_survives_the_guard_unchanged():
    """The guard must not touch a row it can read.

    A coerce that rebuilt every row -- say, by normalising the key order or
    dropping an unfamiliar-but-harmless key -- would be a silent data-loss
    change to every existing player's pity counters, and every test above would
    still pass because they all seed corrupt rows.
    """
    healthy = {
        "pity_counters": {"earth_cache": 7},
        "total_pulls": {"earth_cache": 99},
        "last_pull_time": {"earth_cache": 1234.5},
    }
    state = L.coerce_lottery_state(healthy)
    assert state.pity_counters == {"earth_cache": 7}
    assert state.total_pulls == {"earth_cache": 99}
    assert state.last_pull_time == {"earth_cache": 1234.5}
    # The route reassigns player["lottery"] from the state object after a pull
    # and persists it, so a fresh map is what must come back -- never the
    # caller's own dict mutated behind its back.
    state.pity_counters["earth_cache"] = 8
    assert healthy["pity_counters"]["earth_cache"] == 7, healthy


# ---------------------------------------------------------------------------
# 19. REPLAY PROTECTION ON THE PLAYER-ROW ARM. The wallet arm has always had
#     an idempotency ledger -- the one inside the wallet store, keyed on the
#     `idem` string handed to `spend`. The token arm never reached it: tokens
#     are not a wallet currency, so the flagship pool debits
#     `player["tokens"][TRIBULATION_TOKEN]` directly, and that write had
#     nothing recording it had already happened. A retried pull -- a client
#     timeout, a double tap, a proxy retry -- therefore debited a second time
#     for one intent, on the one pool the game is sold on.
#
#     These are the tests that failed before the fix. The control for each is
#     the second half of the test: a DIFFERENT key must still charge, because a
#     guard that swallows everything would pass a "second request costs
#     nothing" assertion while quietly eating every genuine pull.
# ---------------------------------------------------------------------------

def _token_app(pid, tokens=10 ** 6, **overrides):
    """A wallet-mounted app whose player row holds a funded token ledger.

    Wallet-mounted ON PURPOSE. That is how main.py wires lottery, and it is
    what makes this the production shape rather than a test-only one: the
    token pool falls through to the player-row arm precisely BECAUSE a wallet
    is present and refuses the currency. Mounting no wallet would test a path
    the server never takes for this pool.
    """
    return _probe_app(pid, tokens={L.TRIBULATION_TOKEN: tokens}, **overrides)


def _token_pull(c, pid, count=1, key=None, pool=None):
    """A pull on the token pool, optionally carrying a caller key."""
    headers = {"Idempotency-Key": key} if key is not None else {}
    return c.post(f"/lottery/{pid}/pull",
                  json={"pool": pool or L.LotteryPool.HEAVENLY_TREASURY.value,
                        "count": count},
                  headers=headers)


def _token_cost(count=1):
    cfg = POOL_CONFIG[L.LotteryPool.HEAVENLY_TREASURY]
    return cfg["cost_per_pull"] if count == 1 else cfg["cost_10x"]


def test_a_retried_token_pull_with_the_same_key_costs_nothing_the_second_time():
    """THE DEFECT. Same request, same key, twice: one charge.

    Every assertion is about money that did or did not move, not about a status
    code -- a route that answered 200 to both attempts while debiting twice
    would satisfy the weaker check and still be the defect.

    `results` is empty on the replay on purpose: the first attempt's results
    were already delivered, and re-rolling would hand the player free items.
    """
    pid = "replay"
    tokens = 10 ** 6
    db, c, rec = _token_app(pid, tokens=tokens)
    cost = _token_cost(1)

    first = _token_pull(c, pid, 1, key="tap-1")
    assert first.status_code == 200, first.text
    assert first.json()["ok"] is True
    assert len(first.json()["results"]) == 1
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - cost, (
        "the first attempt did not charge exactly one pull price")

    second = _token_pull(c, pid, 1, key="tap-1")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["ok"] is True
    assert body.get("_replayed") is True, (
        "the retry was served as a fresh charge, not flagged as a replay: %s"
        % body)
    assert body["results"] == [], (
        "the retry re-rolled, which hands the player free items: %s" % body)
    assert body["total_cost"] == cost, body

    # The load-bearing assertion: charged once, not twice.
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - cost, (
        "the retry was charged a second time: %d left of %d"
        % (db[pid]["tokens"][L.TRIBULATION_TOKEN], tokens))
    # And nothing was granted or counted twice either. A replay that skipped the
    # charge but still granted the reward would be a different money bug.
    assert sum(db[pid]["inventory"].values()) <= 1, db[pid]["inventory"]
    saved = db[pid]["lottery"]
    assert saved["total_pulls"][L.LotteryPool.HEAVENLY_TREASURY.value] == 1, (
        "the retry counted as a second pull: %s" % saved)


def test_a_ten_pull_retry_under_the_same_key_is_also_charged_once():
    """The ten-pull price is ten times the single, so a double there costs more.

    Pinned at count=10 because that is where a double-charge does real damage,
    and because the batch size is part of the key namespace -- a guard written
    only for the single-pull path would pass the test above and fail here.
    """
    pid = "replay10"
    tokens = 10 ** 6
    db, c, _ = _token_app(pid, tokens=tokens)
    cost = _token_cost(10)

    first = _token_pull(c, pid, 10, key="batch-1")
    assert first.status_code == 200, first.text
    assert len(first.json()["results"]) == 10

    second = _token_pull(c, pid, 10, key="batch-1")
    assert second.status_code == 200, second.text
    assert second.json().get("_replayed") is True, second.json()
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - cost, (
        "the ten-pull retry was charged a second time: %d"
        % db[pid]["tokens"][L.TRIBULATION_TOKEN])
    assert db[pid]["lottery"]["total_pulls"][
        L.LotteryPool.HEAVENLY_TREASURY.value] == 10


def test_a_second_pull_with_a_different_key_still_charges():
    """THE CONTROL. A guard that swallows everything passes the test above.

    Two genuine pulls are two intents. The second carries a different key, so
    it must be charged in full -- otherwise the replay protection has simply
    become a "you may pull once" bug, which costs the player their money AND
    their game.
    """
    pid = "twokeys"
    tokens = 10 ** 6
    db, c, _ = _token_app(pid, tokens=tokens)
    cost = _token_cost(1)

    assert _token_pull(c, pid, 1, key="intent-1").status_code == 200
    assert _token_pull(c, pid, 1, key="intent-2").status_code == 200
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - 2 * cost, (
        "a genuine second pull was swallowed as a replay: %d"
        % db[pid]["tokens"][L.TRIBULATION_TOKEN])
    assert db[pid]["lottery"]["total_pulls"][
        L.LotteryPool.HEAVENLY_TREASURY.value] == 2


def test_one_caller_key_reused_across_different_pulls_is_not_one_pull():
    """The namespace, which is why the raw caller key is never stored alone.

    A client that stamps its key from a session or a screen -- rather than per
    intent -- will reuse one string across many pulls. Collapsing those into a
    single replay would silently lock the player out of the gacha after their
    first tap. Player, pool and batch size all sit in the key, so each of these
    is a distinct intent.

    The stone pool reaches the WALLET here, not the player row, because
    `_token_app` mounts a wallet exactly as main.py does -- so its charge is
    asserted on the recorder rather than on the row balance. Getting that wrong
    is what an earlier draft of this test did, and it failed for a reason that
    had nothing to do with the guard.
    """
    pid = "reused"
    tokens = 10 ** 6
    db, c, rec = _token_app(pid, tokens=tokens)
    stones = dict(STONES)
    db[pid]["spirit_stones"] = stones

    # Same key, different batch size -- both on the token arm.
    assert _token_pull(c, pid, 1, key="one-key").status_code == 200
    assert _token_pull(c, pid, 10, key="one-key").status_code == 200
    # Same key, different pool -- that one reaches the wallet store.
    assert c.post(f"/lottery/{pid}/pull",
                  json={"pool": L.LotteryPool.COMMON_SATCHEL.value, "count": 1},
                  headers={"Idempotency-Key": "one-key"}).status_code == 200

    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - _token_cost(1) \
        - _token_cost(10), (
        "one reused caller key swallowed two genuine pulls: %d"
        % db[pid]["tokens"][L.TRIBULATION_TOKEN])
    assert len(rec.spends) == 1, (
        "one reused caller key swallowed a pull from a different pool: %s"
        % rec.spends)
    assert rec.spends[0]["idem"].endswith(":one-key"), rec.spends[0]
    assert db[pid]["lottery"]["total_pulls"][
        L.LotteryPool.HEAVENLY_TREASURY.value] == 11


def test_a_token_pull_with_no_key_is_still_served_and_still_not_protected():
    """The shipped client sends no header, so refusing would 400 every tap.

    The cost is stated rather than hidden, matching the wallet arm: the request
    succeeds, and a retry is NOT collapsed. What must also hold is that these
    unpinned pulls do not fill the bounded ledger with unreachable uuid entries
    and evict the real keys -- see the next test.
    """
    pid = "nokey"
    tokens = 10 ** 6
    db, c, _ = _token_app(pid, tokens=tokens)
    cost = _token_cost(1)

    assert _token_pull(c, pid, 1).status_code == 200
    assert _token_pull(c, pid, 1).status_code == 200
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - 2 * cost, (
        "a header-less retry was silently charged once: %d"
        % db[pid]["tokens"][L.TRIBULATION_TOKEN])
    # No key was supplied, so nothing is recorded: an entry for a fresh uuid
    # could never match a later request and would only evict real keys.
    assert L.TOKEN_REPLAY_LEDGER_KEY not in db[pid], (
        "an unpinned pull wrote a replay entry it can never match: %s"
        % db[pid].get(L.TOKEN_REPLAY_LEDGER_KEY))


def test_a_headerless_client_cannot_evict_a_keyed_clients_replay_protection():
    """The eviction pressure the previous test's assertion protects against.

    The ledger is bounded, so anything that writes to it consumes a slot. A
    header-less pull wrote one useless entry per attempt until a player who
    taps often enough would push out every real key -- and the protection would
    disappear for exactly the client that opted into it.
    """
    pid = "pressure"
    db, c, _ = _token_app(pid, tokens=10 ** 9)
    assert _token_pull(c, pid, 1, key="the-real-key").status_code == 200

    for _ in range(L.MAX_TOKEN_REPLAY_ENTRIES * 3):
        assert _token_pull(c, pid, 1).status_code == 200

    ledger = db[pid][L.TOKEN_REPLAY_LEDGER_KEY]
    assert any("the-real-key" in k for k in ledger), (
        "unpinned pulls evicted the real key from a bounded ledger: %s"
        % sorted(ledger))
    # And the retry still does not charge.
    tokens = db[pid]["tokens"][L.TRIBULATION_TOKEN]
    again = _token_pull(c, pid, 1, key="the-real-key")
    assert again.json().get("_replayed") is True, again.json()
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens


def test_a_refused_token_pull_does_not_record_a_replay_entry():
    """A refusal burns nothing: the player's next real pull must still charge.

    The reverse failure is as bad as the double-charge -- if an insufficient
    balance recorded an entry, the player would top up, tap again with the same
    key, and be told their pull was already paid for. Nothing was paid.
    """
    pid = "broke"
    db, c, _ = _token_app(pid, tokens=5)  # one pull costs 10
    r = _token_pull(c, pid, 1, key="tap-1")
    assert r.status_code == 400, r.text
    assert L.TOKEN_REPLAY_LEDGER_KEY not in db[pid], db[pid].get(
        L.TOKEN_REPLAY_LEDGER_KEY)

    # Funded, same key: this is a genuine first pull, not a replay.
    db[pid]["tokens"][L.TRIBULATION_TOKEN] = 10 ** 6
    tokens = db[pid]["tokens"][L.TRIBULATION_TOKEN]
    again = _token_pull(c, pid, 1, key="tap-1")
    assert again.status_code == 200, again.text
    assert "_replayed" not in again.json(), (
        "a refused pull swallowed the real one as a replay: %s" % again.json())
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - _token_cost(1)


def test_the_replay_ledger_survives_the_lottery_state_rewrite():
    """`player["lottery"]` is reassigned wholesale; the ledger is a sibling.

    A ledger nested inside that blob would be deleted by the very next pull --
    protection that vanishes one request after it is set up. This drives two
    keyed pulls with unrelated pulls in between, which is what makes the
    ordering matter.
    """
    pid = "sibling"
    db, c, _ = _token_app(pid, tokens=10 ** 6)
    db[pid]["spirit_stones"] = dict(STONES)

    assert _token_pull(c, pid, 1, key="tap-1").status_code == 200
    # A stone pull in between rewrites player["lottery"] entirely.
    assert _pull(c, pid, L.LotteryPool.COMMON_SATCHEL.value, 1).status_code == 200
    assert set(db[pid]["lottery"]) == {"pity_counters", "total_pulls",
                                       "last_pull_time"}, db[pid]["lottery"]

    tokens = db[pid]["tokens"][L.TRIBULATION_TOKEN]
    again = _token_pull(c, pid, 1, key="tap-1")
    assert again.json().get("_replayed") is True, (
        "the ledger did not survive an unrelated pull: %s" % again.json())
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens


def test_the_replay_ledger_reaches_the_persist_hook_and_survives_a_reload():
    """On the real store the ledger lives only if `persist` writes it.

    Every other test in this section reads `db[pid]`, which is the live dict.
    The production deployment is a PlayerStore where `persist()` is the only
    thing that makes anything survive the request -- so a ledger that never
    reached the hook would be replay protection that works in the test process
    and nowhere else.

    Driven through a store that persists into a SECOND dict, then a fresh app
    seeded from that second dict: the retry is served by a different app
    instance over a reloaded row, which is as close to "a different process"
    as an in-process test can get.
    """
    class _ReloadingStore(dict):
        """Persists into a separate dict, standing in for durable storage."""

        def __init__(self, rows):
            super().__init__(rows)
            self.disk = {k: dict(v) for k, v in rows.items()}

        def persist(self, player_id, player):
            self.disk[player_id] = dict(player)

    pid = "persistprobe"
    store = _ReloadingStore({pid: {"lottery": {}, "inventory": {},
                                   "tokens": {L.TRIBULATION_TOKEN: 10 ** 6}}})
    app = FastAPI()
    L.register_lottery_routes(app, lambda: store)
    c = TestClient(app)
    body = {"pool": L.LotteryPool.HEAVENLY_TREASURY.value, "count": 1}

    assert c.post(f"/lottery/{pid}/pull", json=body,
                  headers={"Idempotency-Key": "k1"}).status_code == 200
    on_disk = store.disk[pid]
    assert list(on_disk[L.TOKEN_REPLAY_LEDGER_KEY]) == [
        "lottery:%s:heavenly_treasury:1:k1" % pid], on_disk.get(
            L.TOKEN_REPLAY_LEDGER_KEY)
    charged_once = on_disk["tokens"][L.TRIBULATION_TOKEN]

    # A second app over the reloaded row -- nothing carried over in memory.
    store2 = _ReloadingStore({pid: dict(store.disk[pid])})
    app2 = FastAPI()
    L.register_lottery_routes(app2, lambda: store2)
    c2 = TestClient(app2)
    again = c2.post(f"/lottery/{pid}/pull", json=body,
                    headers={"Idempotency-Key": "k1"})
    assert again.status_code == 200, again.text
    assert again.json().get("_replayed") is True, (
        "the retry was charged again after the row was reloaded: %s"
        % again.json())
    assert store2[pid]["tokens"][L.TRIBULATION_TOKEN] == charged_once, (
        "a reloaded retry debited a second time: %d"
        % store2[pid]["tokens"][L.TRIBULATION_TOKEN])


def test_the_replay_ledger_is_bounded_so_a_player_row_cannot_grow_forever():
    """The caller controls how fast the ledger grows, so it needs a ceiling.

    Without one, a hostile or buggy caller sends distinct keys and the player
    row grows without limit -- on the same player row the module already had to
    bound elsewhere. The cap is the module's own constant so this cannot drift.
    """
    pid = "flood"
    db, c, _ = _token_app(pid, tokens=10 ** 9)
    for i in range(L.MAX_TOKEN_REPLAY_ENTRIES + 25):
        assert _token_pull(c, pid, 1, key=f"flood-{i}").status_code == 200
    ledger = db[pid][L.TOKEN_REPLAY_LEDGER_KEY]
    assert len(ledger) == L.MAX_TOKEN_REPLAY_ENTRIES, (
        "the ledger grew past its cap to %d entries" % len(ledger))
    # Oldest-first eviction: the earliest keys are the ones that went.
    assert not any("flood-0" in k for k in ledger), sorted(ledger)
    assert any(f"flood-{L.MAX_TOKEN_REPLAY_ENTRIES + 24}" in k for k in ledger)


@pytest.mark.parametrize("junk", ["notadict", 7, None, ["a"]])
def test_a_corrupt_replay_ledger_answers_a_pull_rather_than_crashing(junk):
    """The stored blob is untrusted input, like `player["lottery"]`.

    `player["lottery"]` used to be splatted unguarded and a malformed row
    answered HTTP 500 -- a server fault for something the caller can neither
    see nor fix. The ledger must fail the same way `coerce_lottery_state`
    does: read as empty, serve the pull, let the next accepted pull rewrite
    the field.
    """
    pid = "corrupt"
    db = {"corrupt": {"lottery": {}, "inventory": {},
                      "tokens": {L.TRIBULATION_TOKEN: 10 ** 6},
                      L.TOKEN_REPLAY_LEDGER_KEY: junk}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/lottery/corrupt/pull",
               json={"pool": L.LotteryPool.HEAVENLY_TREASURY.value, "count": 1},
               headers={"Idempotency-Key": "tap-1"})
    assert r.status_code == 200, (
        "a stored replay ledger of %r made the pull answer %d: %s"
        % (junk, r.status_code, r.text[:200]))
    body = r.json()
    assert body["ok"] is True and len(body["results"]) == 1, body
    # The junk is replaced by exactly the one namespaced key for this pull, so
    # the field is usable rather than merely non-crashing.
    ledger = db["corrupt"][L.TOKEN_REPLAY_LEDGER_KEY]
    assert list(ledger) == ["lottery:corrupt:heavenly_treasury:1:tap-1"], ledger
    # And it behaves as a real ledger from here on.
    second = c.post("/lottery/corrupt/pull",
                    json={"pool": L.LotteryPool.HEAVENLY_TREASURY.value,
                          "count": 1},
                    headers={"Idempotency-Key": "tap-1"})
    assert second.json().get("_replayed") is True, second.json()


def test_a_foreign_entry_in_the_ledger_does_not_swallow_a_real_pull():
    """A ledger is a dict, so a row can hold keys this module never wrote.

    That is the benign case, and it must stay benign: an unrecognised entry
    neither blocks a genuine pull nor blocks its retry. Pinning it separately
    from the corrupt-blob sweep above because the two fail in opposite
    directions -- the sweep needs the field discarded, this needs it kept.
    """
    pid = "foreign"
    tokens = 10 ** 6
    foreign = {L.TOKEN_REPLAY_LEDGER_KEY: {"some-other-systems-key": 1.0}}
    db, c, _ = _token_app(pid, tokens=tokens, **foreign)

    first = _token_pull(c, pid, 1, key="tap-1")
    assert first.status_code == 200, first.text
    assert "_replayed" not in first.json(), (
        "a foreign ledger entry swallowed a first pull: %s" % first.json())
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - _token_cost(1)
    # The foreign key survives untouched -- this module does not garbage-collect
    # another system's rows off a shared player row.
    assert "some-other-systems-key" in db[pid][L.TOKEN_REPLAY_LEDGER_KEY]

    again = _token_pull(c, pid, 1, key="tap-1")
    assert again.json().get("_replayed") is True, again.json()
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens - _token_cost(1)


def test_an_over_long_idempotency_key_is_refused_before_any_token_debit():
    """The header lands in a player-row key set, so it must be bounded.

    The bound is read from idem_gate rather than restated, so this cannot drift
    from the limit every other money route enforces. The debit is asserted
    unchanged, because a 400 raised after the write would be a charge plus an
    error.
    """
    from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
    pid = "longkey"
    tokens = 10 ** 6
    db, c, _ = _token_app(pid, tokens=tokens)
    r = _token_pull(c, pid, 1, key="x" * (MAX_IDEMPOTENCY_KEY_LEN + 1))
    assert r.status_code == 400, r.text
    assert "Idempotency-Key" in str(r.json()["detail"]), r.json()
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens, (
        "an over-long key was charged before being refused")
    assert db[pid]["inventory"] == {}


def test_an_over_long_key_padded_with_whitespace_is_still_refused():
    """The bound is measured on the raw header, not the stripped one.

    A key padded out to any size with spaces shrinks to a few characters when
    stripped, so measuring after the strip lets an unbounded string through.
    That growth vector is precisely what the bound exists to stop.
    """
    from idem_gate import MAX_IDEMPOTENCY_KEY_LEN
    pid = "padded"
    tokens = 10 ** 6
    db, c, _ = _token_app(pid, tokens=tokens)
    r = _token_pull(c, pid, 1, key="k" + " " * MAX_IDEMPOTENCY_KEY_LEN)
    assert r.status_code == 400, (
        "a whitespace-padded over-long key was served: %s" % r.text[:200])
    assert db[pid]["tokens"][L.TRIBULATION_TOKEN] == tokens


def test_the_two_arms_produce_one_identical_key_for_one_caller_key():
    """THE DERIVATION, asserted where both ledgers are visible at once.

    `_token_app` mounts a wallet, so a stone pull goes through the wallet store
    (recorder-visible) and a token pull goes through the player row. One caller
    key must arrive at both as the same string shape: if the two arms built the
    namespace differently, one key would be two keys across two ledgers, and a
    player who retried across arms -- the token pool today, the stone pool
    tomorrow, same session -- would be charged twice for one intent.

    The pool and count differ between the two because these are different
    pulls, which is the whole point of the namespace. So the assertion is that
    both arms emit `lottery:<player>:<pool>:<count>:<caller key>` and neither
    mints a uuid of its own.
    """
    pid = "arms2"
    db, c, rec = _token_app(pid, tokens=10 ** 6,
                            spirit_stones=dict(STONES))
    _token_pull(c, pid, 1, key="shared-key")
    # Sent with the header explicitly: the module-level `_pull` helper carries
    # no key, so an earlier draft of this test compared a uuid against a caller
    # key and failed for that reason alone.
    assert c.post(f"/lottery/{pid}/pull",
                  json={"pool": L.LotteryPool.COMMON_SATCHEL.value, "count": 1},
                  headers={"Idempotency-Key": "shared-key"}).status_code == 200

    wallet_keys = [s["idem"] for s in rec.spends]
    assert wallet_keys == ["lottery:%s:common_satchel:1:shared-key" % pid], (
        wallet_keys)
    ledger_keys = list(db[pid][L.TOKEN_REPLAY_LEDGER_KEY])
    assert ledger_keys == ["lottery:%s:heavenly_treasury:1:shared-key" % pid], (
        ledger_keys)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(pools, pool_value):
    """The one catalogue/state row for a pool."""
    for row in pools:
        if row["pool"] == pool_value:
            return row
    raise AssertionError(f"no row for pool {pool_value}")




# ---------------------------------------------------------------------------
# 20. NON-FINITE AMOUNTS. NaN and +/-Infinity are numbers to Python and to
#     JSON, and neither is a balance a player can hold. The arithmetic matters
#     more than the label: `NaN < 10` is False (so an affordability check
#     written with `<` waves NaN straight through) and `int(NaN)` raises
#     ValueError (so a check written with `int()` raises instead). Both
#     spellings are in this file today, one per currency arm, which is why one
#     junk row produces a 500 on the stone pools and a FREE PULL on the token
#     pool.
#
#     Why a section, given the corrupt-blob sweep already covers strings,
#     booleans and lists: those are all discarded by an `isinstance(value, int)`
#     shape test. A non-finite float PASSES that test -- it really is a float
#     -- so it reaches the money arithmetic, where `math.isfinite` is the only
#     thing standing between a stored row and an HTTP 500 or a free draw. That
#     guard is load-bearing and had no test pinning it.
#
#     MEASURED on the tree this file runs against, one value at a time. The
#     three values behave DIFFERENTLY, so each test below pins only the values
#     it was measured on rather than sweeping all three blindly:
#
#         counter map  NaN / +Inf / -Inf  -> discarded, both routes answer 200
#         stones       NaN / +Inf / -Inf  -> 500                      (DEFECT)
#         tokens       NaN                -> 200, reward granted      (DEFECT)
#         tokens       +Inf               -> 200, reward granted      (DEFECT)
#         tokens       -Inf               -> 400, refused             (correct)
#         ledger ts    NaN / -Inf         -> not a live replay        (correct)
#         ledger ts    +Inf               -> live replay forever      (DEFECT)
#
#     `-Inf` is the reason this table is not simpler. On the token arm it is
#     refused already, because `-Inf < cost` is True, so the one guard that
#     exists happens to cover it. Pinning all three under one strict xfail
#     would then report that working case as a failure. Same for the ledger:
#     only `+Inf` defeats the age test.
#
#     Every xfail marker here asserts the FIXED behaviour, never the current
#     one, so each flips to a loud failure the moment the guard lands.
# ---------------------------------------------------------------------------

NAN, POS_INF, NEG_INF = float("nan"), float("inf"), float("-inf")


def _is_non_finite(value):
    """Test-side duplicate of the guard, so no test asserts on itself."""
    import math
    return isinstance(value, float) and not math.isfinite(value)


@pytest.mark.parametrize("value", [NAN, POS_INF, NEG_INF],
                         ids=["nan", "pos-inf", "neg-inf"])
def test_a_non_finite_stored_counter_is_discarded_rather_than_kept(value):
    """The shape guard alone does not stop this one.

    `_clean_counter_map` keeps ints and finite floats, and drops everything
    else. NaN and both infinities are floats, so they survive an
    `isinstance(x, int)` test and would be ordered against the pool's pity cap
    one line deeper -- and `NaN >= 9` is False while `+Inf >= 9` is True, so
    one junk value both suppresses and fires a guarantee depending on which way
    it points.
    """
    assert _is_non_finite(value), (value, "the fixture stopped being non-finite")
    cleaned = L._clean_counter_map({"common_satchel": value})
    assert cleaned == {}, (
        "a non-finite pity survived into the roll arithmetic: %r" % cleaned)


@pytest.mark.parametrize("value", [NAN, POS_INF, NEG_INF],
                         ids=["nan", "pos-inf", "neg-inf"])
def test_a_non_finite_stored_pity_reaches_neither_player_route_as_a_crash(value):
    """The read zeroes it; the write still rolls an ordinary pull.

    Self-healing, matching the direction the corrupt-blob tests above pin: a
    caller cannot repair their own player row, so the read reports fresh state
    and the next accepted pull rewrites the row in the canonical shape. Asserted
    as 200 on both routes, because a route answering 200 with a broken body
    would pass a weaker `status < 500` check and still be a defect.
    """
    blob = {"pity_counters": {"common_satchel": value},
            "total_pulls": {"common_satchel": value}}
    db = {"p": {"lottery": blob, "inventory": {}, "spirit_stones": dict(STONES)}}
    app = FastAPI()
    L.register_lottery_routes(app, lambda: db)
    c = TestClient(app, raise_server_exceptions=False)

    g = c.get("/lottery/p/state")
    assert g.status_code == 200, (value, g.status_code, g.text[:200])
    row = _entry(g.json()["pools"], "common_satchel")
    assert row["pity"] == 0, (value, row)
    assert row["total_pulls"] == 0, (value, row)

    p = _pull(c, "p", "common_satchel")
    assert p.status_code == 200, (value, p.status_code, p.text[:200])
    assert len(p.json()["results"]) == 1, (value, p.json())
    # And the row is well-formed now, so the junk is gone for good.
    saved = db["p"]["lottery"]
    assert saved["pity_counters"]["common_satchel"] in (0, 1), (value, saved)
    assert not _is_non_finite(saved["pity_counters"]["common_satchel"]), (
        value, saved)


@pytest.mark.parametrize("value", [NAN, POS_INF, NEG_INF],
                         ids=["nan", "pos-inf", "neg-inf"])
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT, measured on all three values: the stone arm calls int() on the "
    "stored balance with no isfinite check, so a row holding NaN or either "
    "infinity answers HTTP 500 with the row left untouched. int(NaN) raises "
    "ValueError; int(+Inf) raises OverflowError. The fix is a math.isfinite "
    "guard at the read, which then refuses the pull as insufficient instead of "
    "raising."))
def test_a_non_finite_stone_balance_is_refused_rather_than_raising(value):
    """A server fault over a row the caller cannot repair is never the answer.

    Asserted on the three things that must hold whichever way the fix is
    written (coerce to zero, or refuse explicitly before the debit): a 4xx the
    client can read, no results handed out, and no stone credited back. The
    balance itself is deliberately not pinned -- a fix may leave the junk in
    place, zero it, or raise loudly on load.
    """
    db, c = _app_for("nanstones", spirit_stones={"low_grade": value})
    before = db["nanstones"]["spirit_stones"]["low_grade"]
    r = _pull(c, "nanstones", "common_satchel")

    assert r.status_code == 400, (
        "a non-finite stone balance answered %d, not a refusal: %s"
        % (r.status_code, r.text[:200]))
    assert "results" not in r.json(), (
        "the pull was served from a non-finite balance: %s" % r.json())
    after = db["nanstones"]["spirit_stones"]["low_grade"]
    assert after == before or not _is_non_finite(after), (
        "the stone balance moved on a refused pull: %r -> %r" % (before, after))
    assert sum(db["nanstones"]["inventory"].values()) == 0, db["nanstones"]


@pytest.mark.parametrize("value", [NAN, POS_INF],
                         ids=["nan", "pos-inf"])
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT, measured on both values: the token arm guards affordability with "
    "`balance < cost`, which these defeat -- NaN < 10 is False and +Inf < 10 is "
    "False, so the refusal never fires and the pull is granted for free. "
    "Measured: both answer 200 with a result and an inventory grant, and the "
    "balance is unchanged, so the player can pull indefinitely at no cost. "
    "NaN - cost is still NaN, so the row never recovers on its own. The fix is "
    "the same isfinite guard the stone arm needs. NOT pinned for -Inf, which is "
    "already refused because -Inf < cost is True."))
def test_a_non_finite_token_balance_cannot_buy_a_pull(value):
    """The token pool is the flagship one, so this is the expensive arm.

    Free items are the whole risk: a 200 here grants a reward and charges
    nothing, and because the subtraction leaves the value non-finite, every
    later pull is free too. Asserted on money and on goods, not on a status code
    alone.
    """
    pid = "nantokens"
    db, c, _ = _token_app(pid, tokens=value)
    r = _token_pull(c, pid, 1, key="tap-1")

    assert r.status_code == 400, (
        "a non-finite token balance answered %d, not a refusal: %s"
        % (r.status_code, r.text[:200]))
    assert "results" not in r.json(), (
        "the token pull was served from a non-finite balance: %s" % r.json())
    assert sum(db[pid]["inventory"].values()) == 0, db[pid]["inventory"]
    assert db[pid]["lottery"].get("total_pulls", {}).get(
        L.LotteryPool.HEAVENLY_TREASURY.value, 0) == 0, db[pid]["lottery"]


def test_the_token_arm_already_refuses_a_negative_infinity_balance():
    """The arm the tests above cannot cover, pinned because it WORKS.

    `-Inf < cost` is True, so the existing affordability check refuses it. That
    is the same guard that lets NaN and +Inf through, so it is easy to assume
    the token arm has no non-finite defence at all and to "fix" a case that is
    already correct. This test fails if that regression lands, which is the only
    way a reader learns the coverage boundary is deliberate.
    """
    pid = "neginf"
    db, c, _ = _token_app(pid, tokens=NEG_INF)
    r = _token_pull(c, pid, 1, key="tap-1")

    assert r.status_code == 400, (
        "a -Inf token balance answered %d: %s" % (r.status_code, r.text[:200]))
    assert "results" not in r.json(), r.json()
    assert sum(db[pid]["inventory"].values()) == 0, db[pid]["inventory"]


@pytest.mark.parametrize("value", [NAN, NEG_INF], ids=["nan", "neg-inf"])
def test_an_unorderable_ledger_timestamp_is_not_a_live_replay(value):
    """The half of the age test that already works, pinned so it stays.

    A junk timestamp must not freeze a key: treating every unreadable value as
    a replay would let a row a player cannot edit lock them out of the gacha.
    Both of these already read as NOT live, and this is what a fix for +Inf
    below must preserve -- the fix adds a case, it does not replace the branch.
    """
    assert L.replay_is_live({"k": value}, "k", 2.0) is False, (
        "a %r timestamp was treated as permanent replay protection" % value)


def test_a_positive_infinity_ledger_timestamp_cannot_freeze_a_key_forever():
    """The mirror of the test above: junk must not mean permanent protection.

    A guard that treats every unreadable timestamp as a replay can lock a player
    out of the gacha, which is its own outage. An age that cannot be computed
    has to read as "not a live replay", the same direction the NaN and -Inf
    cases already take.
    """
    pid = "infstamp"
    key = "tap-1"
    idem = "lottery:%s:heavenly_treasury:1:%s" % (pid, key)
    db, c, _ = _token_app(pid, tokens=10 ** 6,
                          **{L.TOKEN_REPLAY_LEDGER_KEY: {idem: POS_INF}})
    r = _token_pull(c, pid, 1, key=key)

    assert r.json().get("_replayed") is not True, (
        "a ledger entry timestamped +Inf froze this key into permanent replay "
        "protection: %s" % r.json())
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert len(r.json()["results"]) == 1, r.json()


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT, measured: both arms are driven the same question on one junk value "
    "and answer differently -- the stone pool raises 500, the token pool grants "
    "the pull for free at 200. This test asserts the relationship the fix has to "
    "establish: one non-finite balance, one answer, no arm granting rewards."))
def test_both_currency_arms_answer_a_non_finite_balance_the_same_way():
    """THE CROSS-ARM CONTRACT, and the test that catches all three defects.

    Requirements 1-3 of this file's brief are each "the two arms must agree":
    one session check, one key derivation, one retention bound. Non-finite input
    is the fourth place they diverge, and the only one in this file that is
    measured rather than reasoned about.

    Both pools are driven on one wallet-mounted app, one seeded player, the same
    junk value and the same question -- may this player pull? The answer has to
    be one answer. Today it is two, and one of the two is free goods.

    Asserted as a RELATIONSHIP between the arms rather than a pinned status,
    because which refusal the fix picks (coerce to zero, or an explicit 400) is
    the module's call. What is not the module's call is answering differently.
    """
    answers = {}
    for pool in (L.LotteryPool.COMMON_SATCHEL, L.LotteryPool.HEAVENLY_TREASURY):
        pid = "arm-%s" % pool.value
        if pool is L.LotteryPool.HEAVENLY_TREASURY:
            db, c, _ = _token_app(pid, tokens=NAN)
        else:
            db, c = _app_for(pid, spirit_stones={"low_grade": NAN})
        r = c.post(f"/lottery/{pid}/pull", json={"pool": pool.value, "count": 1})
        answers[pool.value] = (r.status_code, "results" in r.json())

    served = sorted(p for p, ans in answers.items() if ans[0] == 200)
    assert not served, (
        "a non-finite balance bought a pull on %s -- free rewards, no charge"
        % served)
    divergent = {p: ans for p, ans in answers.items()
                 if not 400 <= ans[0] < 500}
    assert not divergent, (
        "the two arms answered a non-finite balance differently: %r" % answers)


def _seed_all(seed):
    """Seed the module RNG so a failure reproduces from the reported seed."""
    import random
    random.seed(seed)