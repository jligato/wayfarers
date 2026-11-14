"""
Tests for the VIP / Privilege tier system.

Layout note: this file lives under ``server/tests/`` because that is where the
newer subsystem tests live (``test_daily_streak.py`` and the route-coverage
guard), and because it keeps it clear of sibling agents working in
``server/``.

Clock discipline
----------------
Every claim test passes an explicit ``now`` built from an integer day index.
Nothing sleeps, and nothing patches ``time.time`` -- the shared stdlib module
-- which ``server/conftest.py`` documents as having poisoned an entire pytest
run. Where a live clock is unavoidable (the HTTP routes) the store is
monkeypatched through :func:`monkeypatch.setattr` on this module's own
``_now`` seam, never on ``time``.

Database discipline
-------------------
``PlayerStore`` latches PostgreSQL mode from ``WC_PG_HOST`` at CONSTRUCTION,
so the env var is deleted in the fixture body, immediately before the store is
built, not at import time. Every test here runs against the in-memory store;
no test in this file touches PostgreSQL.
"""
from __future__ import annotations

import os
import sys
import copy
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SERVER = _ROOT / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import vip as V  # noqa: E402

DAY = float(V.SECONDS_PER_DAY)


def at(day_index, hour=12):
    """A timestamp partway through UTC day ``day_index``.

    Midday, not midnight, so a test cannot accidentally pass because it sits on
    a day boundary. Same helper idea as ``test_daily_streak.py``.
    """
    return day_index * DAY + hour * 3600.0


def make_player(**extra):
    """A minimal player dict, tier 0 by default."""
    player = {
        "id": "p1",
        "name": "VIP Tester",
        "spirit_stones": {"low_grade": 0},
        "progress": 0.0,
        "inventory": {},
    }
    player.update(extra)
    return player


def stones(player):
    """The free-to-play spirit-stone balance, dict or scalar form."""
    value = player.get("spirit_stones")
    if isinstance(value, dict):
        return int(value.get("low_grade", 0) or 0)
    return int(value or 0)


# --- the tier table is well formed -------------------------------------------

def test_tier_table_validates():
    """The shipped ladder is well formed. Must not raise."""
    V.validate_tier_table()


def test_tier_ids_are_unique_and_strictly_ordered():
    ids = [t["id"] for t in V.VIP_TIERS]
    assert len(set(ids)) == len(ids)
    points = [t["points_required"] for t in V.VIP_TIERS]
    assert points == sorted(points) and len(set(points)) == len(points)
    assert points[0] == 0, "the first tier must be free"


def test_first_tier_is_free_and_pays_nothing_daily():
    free = V.VIP_TIERS[0]
    assert free["cost_spirit_stones"] == 0
    assert V._grant_is_empty(V.tier_benefits(free["id"])["daily_claim"])


# --- THE design constraint: privileges, never power --------------------------

def test_no_tier_declares_a_forbidden_power_benefit():
    """A VIP multiplier anywhere in the table fails here, not in production."""
    for tier in V.VIP_TIERS:
        keys = set(tier["benefits"])
        assert not (keys & set(V.FORBIDDEN_BENEFIT_KEYS)), (
            f"tier {tier['id']!r} grants power: "
            f"{sorted(keys & set(V.FORBIDDEN_BENEFIT_KEYS))}"
        )
        assert keys <= set(V.PERMITTED_BENEFIT_KEYS)


def test_validate_tier_table_rejects_a_power_multiplier():
    """Negative control on the guard itself: a smuggled multiplier must raise.

    Without this, ``validate_tier_table`` could be a function that always
    returns None and every other table test would still pass.
    """
    sabotaged = [
        {"id": "traveler", "display_name": "T", "points_required": 0,
         "cost_spirit_stones": 0, "benefits": {"cultivation_speed": 2.0}},
        {"id": "wanderer", "display_name": "W", "points_required": 1,
         "cost_spirit_stones": 1, "benefits": {"bag_slots": 1}},
    ]
    original = V.VIP_TIERS
    try:
        V.VIP_TIERS = tuple(sabotaged)
        with pytest.raises(V.VipError, match="forbidden benefit"):
            V.validate_tier_table()
    finally:
        V.VIP_TIERS = original
    V.validate_tier_table()  # restored table is still valid


def test_validate_tier_table_rejects_an_unknown_benefit_key():
    sabotaged = [
        {"id": "traveler", "display_name": "T", "points_required": 0,
         "cost_spirit_stones": 0, "benefits": {"lucky_charm": 1}},
    ]
    original = V.VIP_TIERS
    try:
        V.VIP_TIERS = tuple(sabotaged)
        with pytest.raises(V.VipError, match="unknown benefit"):
            V.validate_tier_table()
    finally:
        V.VIP_TIERS = original


def test_validate_tier_table_rejects_duplicate_ids_and_flat_costs():
    for sabotaged, match in (
        (
            [
                {"id": "x", "display_name": "X", "points_required": 0,
                 "cost_spirit_stones": 0, "benefits": {}},
                {"id": "x", "display_name": "X2", "points_required": 1,
                 "cost_spirit_stones": 1, "benefits": {}},
            ],
            "duplicate tier id",
        ),
        (
            [
                {"id": "x", "display_name": "X", "points_required": 0,
                 "cost_spirit_stones": 0, "benefits": {}},
                {"id": "y", "display_name": "Y", "points_required": 0,
                 "cost_spirit_stones": 1, "benefits": {}},
            ],
            "must strictly increase",
        ),
        (
            [
                {"id": "x", "display_name": "X", "points_required": 5,
                 "cost_spirit_stones": 0, "benefits": {}},
            ],
            "must be free",
        ),
    ):
        original = V.VIP_TIERS
        try:
            V.VIP_TIERS = tuple(sabotaged)
            with pytest.raises(V.VipError, match=match):
                V.validate_tier_table()
        finally:
            V.VIP_TIERS = original


# --- cosmetic and item ids are real ------------------------------------------

def test_every_cosmetic_id_exists_in_the_cosmetics_catalog():
    """A typo in a cosmetic id pays out an unrenderable nothing."""
    from cosmetics import COSMETIC_CATALOG

    for tier in V.VIP_TIERS:
        for cosmetic_id in tier["benefits"].get("cosmetics", ()):
            assert cosmetic_id in COSMETIC_CATALOG, (
                f"tier {tier['id']!r} grants unknown cosmetic {cosmetic_id!r}"
            )


def test_every_daily_item_id_exists_in_the_item_catalog():
    from inventory import ITEM_DEFINITIONS

    for tier in V.VIP_TIERS:
        for item_id in (tier["benefits"].get("daily_claim", {}).get("items", {}) or {}):
            assert item_id in ITEM_DEFINITIONS, (
                f"tier {tier['id']!r} grants unknown item {item_id!r}"
            )


# --- the folded benefit ladder -----------------------------------------------

def test_benefits_fold_so_higher_tiers_are_the_sum_of_lower_ones():
    """Data-driven means a hand-copied total cannot drift out of the table."""
    folded = [V.tier_benefits(t["id"]) for t in V.VIP_TIERS]
    for numeric in ("bag_slots", "shop_discount_pct"):
        values = [f[numeric] for f in folded]
        assert values == sorted(values), f"{numeric} is not monotonic: {values}"
    stones = [f["daily_claim"]["spirit_stones"] for f in folded]
    assert stones == sorted(stones)
    assert stones[0] == 0


def test_tier_benefits_rejects_an_unknown_tier():
    with pytest.raises(V.VipError, match="unknown tier id"):
        V.tier_benefits("not_a_tier")


def test_folded_benefits_equal_the_sum_of_every_row_up_to_that_tier():
    """The fold must ADD rows, not take the last one.

    Monotonicity alone is not enough: because the ladder is increasing, a fold
    that overwrites instead of accumulating still looks monotonic. Verified --
    swapping ``+=`` for ``=`` in the fold left the whole file green until this
    test existed.
    """
    for index, tier in enumerate(V.VIP_TIERS):
        rows = V.VIP_TIERS[: index + 1]
        folded = V.tier_benefits(tier["id"])
        for numeric in ("bag_slots", "shop_discount_pct"):
            expected = sum(int(r["benefits"].get(numeric, 0)) for r in rows)
            assert folded[numeric] == expected, (
                f"tier {tier['id']!r} {numeric} is {folded[numeric]}, "
                f"but the sum of its rows is {expected}"
            )
        expected_stones = sum(
            int(r["benefits"].get("daily_claim", {}).get("spirit_stones", 0)) for r in rows
        )
        assert folded["daily_claim"]["spirit_stones"] == expected_stones
        expected_items = {}
        for r in rows:
            for item_id, qty in (r["benefits"].get("daily_claim", {}).get("items", {}) or {}).items():
                expected_items[item_id] = expected_items.get(item_id, 0) + int(qty)
        assert folded["daily_claim"]["items"] == expected_items
        expected_cosmetics = []
        for r in rows:
            for cid in r["benefits"].get("cosmetics", ()):
                if cid not in expected_cosmetics:
                    expected_cosmetics.append(cid)
        assert folded["cosmetics"] == expected_cosmetics


def test_the_top_tier_folds_every_row_not_just_its_own():
    """A concrete multi-row sum, so the fold test cannot pass by coincidence."""
    rows = V.VIP_TIERS
    assert len(rows) >= 3, "the ladder shrank; this arithmetic no longer holds"
    top = V.tier_benefits(rows[-1]["id"])
    assert top["bag_slots"] == sum(r["benefits"].get("bag_slots", 0) for r in rows)
    assert top["bag_slots"] > rows[-1]["benefits"]["bag_slots"], (
        "the top tier reported only its own increment, so lower tiers are lost"
    )


def test_tier_lookup_by_points_is_total_over_corrupt_values():
    assert V.tier_for_points(0)["id"] == V.VIP_TIERS[0]["id"]
    assert V.tier_for_points(-500)["id"] == V.VIP_TIERS[0]["id"]
    assert V.tier_for_points(10 ** 9)["id"] == V.VIP_TIERS[-1]["id"]


# --- status: current tier and progress toward the next -----------------------

def test_status_reports_zero_progress_for_a_new_player_and_mutates_nothing():
    player = make_player()
    status = V.get_vip_status(player, now=at(1000))
    assert status["tier_id"] == V.VIP_TIERS[0]["id"]
    assert status["points"] == 0
    assert status["claimed_today"] is False
    assert V.STATE_KEY not in player, "status() must be pure"


def test_status_reports_progress_toward_the_next_tier():
    player = make_player()
    V.add_vip_points(player, 250)  # halfway from 0 to the first paid tier
    status = V.get_vip_status(player, now=at(1000))
    first_paid = V.VIP_TIERS[1]
    assert status["tier_id"] == V.VIP_TIERS[0]["id"]
    assert status["next_tier_id"] == first_paid["id"]
    assert status["points_to_next"] == first_paid["points_required"] - 250
    assert 0.0 < status["progress_pct"] < 100.0


def test_status_marks_the_top_tier_as_max_with_no_next():
    player = make_player()
    V.add_vip_points(player, 10 ** 6)
    status = V.get_vip_status(player, now=at(1000))
    assert status["is_max_tier"] is True
    assert status["next_tier_id"] is None
    assert status["progress_pct"] == 100.0
    assert status["points_to_next"] == 0


def test_status_tolerates_a_legacy_player_with_no_vip_state():
    status = V.get_vip_status({"id": "old"}, now=at(1000))
    assert status["points"] == 0
    assert status["tier_id"] == V.VIP_TIERS[0]["id"]
    assert status["claimed_today"] is False


def test_status_corrects_a_hand_edited_tier_id():
    player = make_player()
    V.add_vip_points(player, 9000)
    player[V.STATE_KEY]["tier_id"] = "traveler"  # stale/hostile write
    status = V.get_vip_status(player, now=at(1000))
    assert status["tier_id"] == V.VIP_TIERS[-1]["id"]


def test_get_state_itself_corrects_a_hand_edited_tier_id():
    """The self-healing must live in ``get_state``, not only in the view.

    ``_apply_daily_claim`` reads the tier through ``get_state``, so a corrected
    view alone would leave the payout path trusting a hostile string. Verified:
    deleting the correction from ``get_state`` left the suite green until this
    test existed.
    """
    player = _paid_tier_player()
    assert player[V.STATE_KEY]["tier_id"] == V.VIP_TIERS[1]["id"]
    player[V.STATE_KEY]["tier_id"] = "traveler"
    assert V.get_state(player)["tier_id"] == V.VIP_TIERS[1]["id"]


# --- points and tier movement -----------------------------------------------

def test_adding_points_promotes_through_the_ladder():
    player = make_player()
    seen = [V.add_vip_points(player, V.VIP_TIERS[i]["points_required"])["tier_id"]
            for i in range(1, len(V.VIP_TIERS))]
    assert seen == [t["id"] for t in V.VIP_TIERS[1:]]


def test_adding_points_reports_tier_changed_only_on_the_boundary():
    player = make_player()
    first = V.add_vip_points(player, 10)
    assert first["tier_changed"] is False
    crossing = V.add_vip_points(player, V.VIP_TIERS[1]["points_required"] - 10)
    assert crossing["tier_changed"] is True
    assert crossing["previous_tier_id"] == V.VIP_TIERS[0]["id"]


def test_points_never_go_negative():
    player = make_player()
    V.add_vip_points(player, 100)
    V.add_vip_points(player, -9999)
    assert player[V.STATE_KEY]["points"] == 0


# --- claim: pays once per calendar day ---------------------------------------

def _paid_tier_player():
    """A player with enough points to earn a non-empty daily grant."""
    player = make_player()
    V.add_vip_points(player, V.VIP_TIERS[1]["points_required"])
    return player


def test_first_claim_pays_stones_and_items():
    player = _paid_tier_player()
    tier_id = player[V.STATE_KEY]["tier_id"]
    expected = V.tier_benefits(tier_id)["daily_claim"]

    outcome = V._apply_daily_claim(player, now=at(1000))

    assert outcome["already_claimed"] is False
    assert outcome["empty_grant"] is False
    assert stones(player) == expected["spirit_stones"] > 0
    for item_id, qty in expected["items"].items():
        assert player["inventory"][item_id] == qty
    assert player[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == 1000
    assert player[V.STATE_KEY]["total_claims"] == 1


def test_second_claim_the_same_day_pays_nothing():
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000, hour=1))
    balance_after_first = stones(player)
    inventory_after_first = dict(player["inventory"])

    second = V._apply_daily_claim(player, now=at(1000, hour=23))

    assert second["already_claimed"] is True
    assert second["reward"]["spirit_stones"] == 0
    assert second["reward"]["items"] == {}
    assert stones(player) == balance_after_first
    assert player["inventory"] == inventory_after_first
    assert player[V.STATE_KEY]["total_claims"] == 1


def test_claim_the_next_day_pays_again():
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000, hour=6))
    first_total = stones(player)

    second = V._apply_daily_claim(player, now=at(1001, hour=6))

    assert second["already_claimed"] is False
    per_day = V.tier_benefits(player[V.STATE_KEY]["tier_id"])["daily_claim"]["spirit_stones"]
    assert stones(player) == first_total + per_day
    assert player[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == 1001
    assert player[V.STATE_KEY]["total_claims"] == 2


def test_one_hour_across_midnight_is_a_new_day_and_must_pay():
    """23:00 -> 00:00 is one hour, but a different UTC day.

    If this test expected no second payout it would be asserting a bug.
    """
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000, hour=23))
    midnight = V._apply_daily_claim(player, now=at(1000, hour=23) + 3600.0)
    assert midnight["already_claimed"] is False
    assert midnight["utc_day"] == 1001


def test_fifty_repeated_presses_on_one_day_pay_once():
    """The loud version of the bug: many presses before any is observed."""
    player = _paid_tier_player()
    paid = [
        V._apply_daily_claim(player, now=at(1000, hour=h))
        for h in range(0, 24)
    ]
    winners = [r for r in paid if not r["already_claimed"]]
    assert len(winners) == 1
    per_day = V.tier_benefits(player[V.STATE_KEY]["tier_id"])["daily_claim"]
    assert stones(player) == per_day["spirit_stones"]
    assert player[V.STATE_KEY]["total_claims"] == 1


def test_a_backwards_clock_does_not_pay_twice():
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000))
    out = V._apply_daily_claim(player, now=at(999))
    assert out["already_claimed"] is True
    assert player[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == 1000


def test_refused_claim_leaves_the_state_byte_identical():
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000))
    before = dict(player[V.STATE_KEY])
    V._apply_daily_claim(player, now=at(1000, hour=18))
    assert player[V.STATE_KEY] == before


# --- the zero-grant case must not burn the day key ---------------------------

def test_free_tier_claim_pays_nothing_and_keeps_the_day_key_free():
    player = make_player()
    out = V._apply_daily_claim(player, now=at(1000))
    assert out["empty_grant"] is True
    assert out["already_claimed"] is False
    assert out["reward"]["spirit_stones"] == 0
    assert stones(player) == 0
    assert player[V.STATE_KEY].get(V.STATE_KEY_LAST_CLAIM) is None


def test_upgrading_after_a_free_tier_press_still_pays_that_day():
    """A free-tier press must not silently deny the day's resources.

    This is the whole reason the empty grant does not consume the day key.
    """
    player = make_player()
    V._apply_daily_claim(player, now=at(1000, hour=3))
    V.add_vip_points(player, V.VIP_TIERS[1]["points_required"])
    out = V._apply_daily_claim(player, now=at(1000, hour=9))
    assert out["already_claimed"] is False
    assert stones(player) > 0


# --- purity: reading must not farm ------------------------------------------

def test_evaluate_claim_is_pure():
    player = _paid_tier_player()
    V._apply_daily_claim(player, now=at(1000))
    before_stones = stones(player)
    before_state = dict(player[V.STATE_KEY])
    for _ in range(10):
        preview = V.evaluate_claim(player, now=at(1001))
        assert preview["already_claimed"] is False
        assert preview["reward"]["spirit_stones"] > 0
    assert stones(player) == before_stones
    assert player[V.STATE_KEY] == before_state


def test_get_state_rejects_a_non_dict_player():
    with pytest.raises(V.VipError):
        V.get_state(None)


# --- utc_day -----------------------------------------------------------------

def test_utc_day_indexes_days_not_seconds():
    assert V.utc_day(0.0) == 0
    assert V.utc_day(DAY - 1) == 0
    assert V.utc_day(DAY) == 1
    assert V.utc_day(1000 * DAY + 5) == 1000


# --- the store-backed path: transaction, persist, concurrency ---------------

@pytest.fixture
def store():
    """An in-memory PlayerStore with one seeded player.

    ``WC_PG_HOST`` is deleted HERE, at fixture time, immediately before the
    store is constructed. ``PlayerStore.__init__`` latches PG mode once, and a
    sibling test module may have set the env var during collection.
    """
    from player_store import PlayerStore

    os.environ.pop("WC_PG_HOST", None)
    db = PlayerStore()
    assert db._pg is False, "VIP tests must run against the in-memory store"
    with db.transaction():
        db["p1"] = _paid_tier_player()
    return db


def test_claim_daily_persists_through_the_store(store):
    first = V.claim_daily("p1", store, now=at(1000))
    assert first["already_claimed"] is False

    # Re-read from the store: a held handle would be a stale snapshot.
    stored = store["p1"]
    assert stored[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == 1000
    assert stones(stored) == first["reward"]["spirit_stones"]

    second = V.claim_daily("p1", store, now=at(1000, hour=20))
    assert second["already_claimed"] is True
    assert stones(store["p1"]) == first["reward"]["spirit_stones"]


def test_claim_daily_accepts_a_store_provider_callable(store):
    """Routes mount a provider, not a captured store. Both must work."""
    out = V.claim_daily("p1", lambda: store, now=at(1000))
    assert out["already_claimed"] is False
    assert stones(store["p1"]) > 0


class _DetachedReadStore:
    """A store double that models PostgreSQL's two rules, and only those two.

    In memory mode ``PlayerStore.__getitem__`` hands back the LIVE dict, so a
    claim that hoisted its work out of the transaction still "worked" and the
    suite stayed green. That is a blind spot, not a pass. This double removes
    it by reproducing what PostgreSQL actually does:

    1. a read outside a transaction returns a DETACHED COPY, so a mutation to
       that copy is thrown away;
    2. committed state changes only through ``persist`` or through a row staged
       inside ``transaction``.

    It is deliberately minimal. It is not a re-implementation of PlayerStore --
    it is the smallest object that can tell "guarded region" from "no guarded
    region".
    """

    def __init__(self, rows):
        self._rows = {pid: copy.deepcopy(row) for pid, row in rows.items()}
        self.transaction_calls = 0
        self._staged = None

    def __contains__(self, player_id):
        return player_id in self._rows

    def __getitem__(self, player_id):
        if self._staged is not None and player_id in self._staged:
            return self._staged[player_id]
        return copy.deepcopy(self._rows[player_id])

    def persist(self, player_id, player=None):
        if player is not None:
            if self._staged is not None and player_id in self._staged:
                self._staged[player_id] = copy.deepcopy(player)
            else:
                self._rows[player_id] = copy.deepcopy(player)
        return None

    @contextmanager
    def transaction(self, player_id=None):
        self.transaction_calls += 1
        previous, self._staged = self._staged, {}
        try:
            yield self
        finally:
            for pid, row in self._staged.items():
                self._rows[pid] = row
            self._staged = previous


class _TransactionCountingStore:
    """Wraps a real store and records how many transaction scopes were opened.

    Asserting on a collaborator's calls is behaviour, not source inspection:
    an implementation that reads the player, checks the flag and writes the key
    outside ``transaction()`` opens ZERO scopes, which this records.
    """

    def __init__(self, inner):
        self._inner = inner
        self.transaction_calls = 0

    def transaction(self, player_id=None):
        self.transaction_calls += 1
        return self._inner.transaction(player_id)

    def __contains__(self, player_id):
        return player_id in self._inner

    def __getitem__(self, player_id):
        return self._inner[player_id]

    def persist(self, player_id, player=None):
        return self._inner.persist(player_id, player)


def test_claim_daily_runs_inside_a_store_transaction():
    """The guarded region must actually be entered, not merely documented.

    Without this, moving the claim outside the transaction keeps every other
    test green in memory mode, because the in-memory store hands back a live
    reference. Verified: that sabotage left the suite at 40 passed.
    """
    counting = _TransactionCountingStore(_DetachedReadStore({"p1": _paid_tier_player()}))
    V.claim_daily("p1", counting, now=at(1000))
    assert counting.transaction_calls == 1, (
        "claim_daily must open exactly one store.transaction() scope around the "
        f"check and the write; it opened {counting.transaction_calls}"
    )


def test_claim_daily_commits_through_a_store_with_detached_reads():
    """The payment and the day key must both survive a PostgreSQL-shaped store.

    This is the battle-pass claim bug in a new costume: mutate the snapshot,
    return 200, save nothing.
    """
    db = _DetachedReadStore({"p1": _paid_tier_player()})
    out = V.claim_daily("p1", db, now=at(1000))
    assert out["already_claimed"] is False

    committed = db["p1"]
    assert committed[V.STATE_KEY][V.STATE_KEY_LAST_CLAIM] == 1000
    assert stones(committed) == out["reward"]["spirit_stones"]
    for item_id, qty in out["reward"]["items"].items():
        assert committed["inventory"][item_id] == qty

    # And the second press against committed state still pays nothing.
    again = V.claim_daily("p1", db, now=at(1000, hour=19))
    assert again["already_claimed"] is True
    assert stones(db["p1"]) == out["reward"]["spirit_stones"]


def test_claim_daily_raises_for_an_unknown_player(store):
    with pytest.raises(KeyError):
        V.claim_daily("nobody", store, now=at(1000))
    with pytest.raises(V.VipError):
        V.claim_daily("", store, now=at(1000))


def test_concurrent_claims_from_many_threads_pay_once(store):
    """Repeat-press under real threads: the store lock must serialise them.

    In memory mode ``PlayerStore.transaction`` holds an ``RLock`` for the whole
    scope; in PostgreSQL mode it takes ``pg_advisory_xact_lock``. Either way the
    same-day check and the day-key write cannot interleave, so exactly one
    thread wins. The store's ``RLock`` is re-entrant per thread but exclusive
    across threads, which is what makes this a real test of the guard.
    """
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def press():
        try:
            barrier.wait(timeout=5)
            results.append(V.claim_daily("p1", store, now=at(1000)))
        except Exception as exc:  # noqa: BLE001 - surfaced in the assert below
            errors.append(repr(exc))

    threads = [threading.Thread(target=press) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent claims raised: {errors}"
    assert len(results) == 8, f"only {len(results)} of 8 threads returned"
    winners = [r for r in results if not r["already_claimed"]]
    assert len(winners) == 1, f"{len(winners)} threads were paid, expected 1"

    stored = store["p1"]
    assert stored[V.STATE_KEY]["total_claims"] == 1
    assert stones(stored) == winners[0]["reward"]["spirit_stones"]


def test_a_failing_payout_leaves_no_day_key_and_no_payment(monkeypatch):
    """A transient payout error must not strand a player with a paid flag.

    This is why the payment happens BEFORE the day-key write. If the order were
    reversed, the key would already be recorded when the payout raised, and the
    player's one claim for the day would be gone with nothing paid. Verified:
    reversing the order in the source left this file green until this test
    existed -- it is the only assertion that can tell the two orders apart.
    """
    player = _paid_tier_player()

    def boom(target, amount):
        raise RuntimeError("wallet unavailable")

    monkeypatch.setattr(V, "_credit_spirit_stones", boom)
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        V._apply_daily_claim(player, now=at(1000))

    assert player[V.STATE_KEY].get(V.STATE_KEY_LAST_CLAIM) is None, (
        "a failed payout left the day key written: the player would be told "
        "'already claimed' and would never be paid"
    )
    assert player[V.STATE_KEY]["total_claims"] == 0
    assert stones(player) == 0
    assert player["inventory"] == {}


def test_a_failing_payout_rolls_the_whole_claim_back_through_the_store(store):
    """The same guarantee one level up: the store commits nothing on a raise."""
    def boom(target, amount):
        raise RuntimeError("wallet unavailable")

    original = V._credit_spirit_stones
    V._credit_spirit_stones = boom
    try:
        with pytest.raises(RuntimeError):
            V.claim_daily("p1", store, now=at(1000))
    finally:
        V._credit_spirit_stones = original

    committed = store["p1"]
    assert committed[V.STATE_KEY].get(V.STATE_KEY_LAST_CLAIM) is None
    assert stones(committed) == 0

    # And the retry is still allowed, because nothing was consumed.
    retry = V.claim_daily("p1", store, now=at(1000))
    assert retry["already_claimed"] is False
    assert stones(store["p1"]) > 0


def test_a_payout_that_dies_after_the_stones_does_not_consume_the_day_key():
    """A failure LATE in the payout must not leave the day key written.

    The stones credit is the first thing that happens and the day key is the
    last, so a corrupt save that explodes during the item credit is the case
    that separates "key written last" from "key written first". This is the
    only assertion in the file that can tell those two orders apart -- reversing
    the order in the source left the suite green until it existed.
    """
    player = _paid_tier_player()
    # A pre-upgrade save where "inventory" is not a dict at all. Crediting items
    # then raises, AFTER the stones were already credited.
    player["inventory"] = 5

    with pytest.raises(AttributeError):
        V._apply_daily_claim(player, now=at(1000))

    assert player[V.STATE_KEY].get(V.STATE_KEY_LAST_CLAIM) is None, (
        "the day key was written before the payout finished; the player's one "
        "claim for the day is gone and the reward was never fully paid"
    )
    assert player[V.STATE_KEY]["total_claims"] == 0


# --- HTTP routes -------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """A real FastAPI app with the VIP routes mounted on an in-memory store."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from player_store import PlayerStore

    monkeypatch.delenv("WC_PG_HOST", raising=False)
    db = PlayerStore()
    assert db._pg is False
    with db.transaction():
        db["p1"] = _paid_tier_player()

    app = FastAPI()
    V.register_vip_routes(app, lambda: db)
    c = TestClient(app)
    c.players_db = db  # the test asserts the WALLET, not just the response
    c.user_id = "p1"
    return c


def test_routes_register_the_two_documented_paths(client):
    paths = {(r.path, tuple(sorted(r.methods))) for r in client.app.routes}
    assert ("/vip/{player_id}/status", ("GET",)) in paths
    assert ("/vip/{player_id}/claim-daily", ("POST",)) in paths


def test_register_vip_routes_refuses_to_mount_a_power_tier(monkeypatch):
    """The guard runs BEFORE any route is added, so a bad ladder never ships."""
    from fastapi import FastAPI

    sabotaged = [
        {"id": "traveler", "display_name": "T", "points_required": 0,
         "cost_spirit_stones": 0, "benefits": {"qi_tick_multiplier": 1.5}},
    ]
    monkeypatch.setattr(V, "VIP_TIERS", tuple(sabotaged))
    app = FastAPI()
    with pytest.raises(V.VipError, match="forbidden benefit"):
        V.register_vip_routes(app, lambda: None)
    assert [r.path for r in app.routes if getattr(r, "path", "").startswith("/vip")] == []


def test_status_route_returns_the_tier_and_progress(client):
    r = client.get("/vip/p1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["vip"]["tier_id"] == V.VIP_TIERS[1]["id"]
    assert body["vip"]["benefits"]["bag_slots"] == V.tier_benefits(V.VIP_TIERS[1]["id"])["bag_slots"]
    assert body["vip"]["claimed_today"] is False
    assert len(body["vip"]["tiers"]) == len(V.VIP_TIERS)


def test_claim_route_pays_once_and_404s_for_a_stranger(client):
    first = client.post("/vip/p1/claim-daily")
    assert first.status_code == 200, first.text
    assert first.json()["already_claimed"] is False
    paid = first.json()["reward"]["spirit_stones"]
    assert paid > 0

    # Assert the WALLET, not just the response field. A claim route that
    # returns 200 with a reward dict while the balance never moved is the
    # battle-pass bug in a new costume.
    assert stones(client.players_db["p1"]) == paid

    second = client.post("/vip/p1/claim-daily")
    assert second.status_code == 200
    assert second.json()["already_claimed"] is True
    assert second.json()["reward"]["spirit_stones"] == 0
    # The wallet must not have moved on the second press.
    assert stones(client.players_db["p1"]) == paid

    assert client.get("/vip/nobody/status").status_code == 404
    assert client.post("/vip/nobody/claim-daily").status_code == 404


# --- PostgreSQL: the advisory lock, as an ORDERING property ------------------
#
# No skip marker. The test skips ITSELF, at run time, only when there is no
# database to talk to -- so it is collected, reported and can never rot into a
# permanent skip. To run it, point it at a PostgreSQL you own:
#
#     WC_PG_HOST=127.0.0.1 WC_PG_PORT=5432 WC_PG_DB=wayfarers \
#     WC_PG_USER=wayfarers WC_PG_PASSWORD=... \
#         python -m pytest server/tests/test_vip.py -k advisory -v
#
# WHAT CHANGED, and why this was rewritten rather than unskipped.
# The old test ran `SELECT pg_advisory_xact_lock(...)`, read the result, and
# asserted it was truthy. That assertion could NEVER pass. The function returns
# void, and psycopg2 hands a void column back as the empty string -- which is
# falsy. Measured against a real PostgreSQL 16:
#
#     cur.execute("SELECT pg_advisory_xact_lock(%s)", (4242,))
#     cur.fetchone()[0]  ->  ''
#
# So the lock engaged correctly and the test called it a failure anyway. It sat
# behind a skip marker, so nobody saw it. A test that cannot pass is worse than
# no test, because it looks like coverage.
#
# The lock has no return value because it is not a value -- it is a WAIT. Its
# entire meaning is temporal: while one transaction holds it, a second one
# blocks. So the property worth asserting is ORDERING, not truthiness:
#
#   1. a second connection's lock request does not return while the first holds
#      it (measured: it is still blocked after a wait);
#   2. once the first holder releases, the second proceeds.
#
# Both halves are asserted, so a lock that did not block at all fails the test
# just as surely as one that never released.

def _pg_connect():
    """Connect to the configured PostgreSQL, or return None if there is none."""
    import psycopg2

    try:
        return psycopg2.connect(
            host=os.environ.get("WC_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("WC_PG_PORT", 5432)),
            dbname=os.environ.get("WC_PG_DB", "wayfarers"),
            user=os.environ.get("WC_PG_USER", "wayfarers"),
            password=os.environ.get("WC_PG_PASSWORD", ""),
            connect_timeout=5,
        )
    except Exception as exc:  # no server, wrong port, bad credentials
        pytest.skip(f"no PostgreSQL at the configured WC_PG_HOST: {exc}")


def test_pg_advisory_lock_makes_the_daily_claim_process_safe():
    """One holder at a time: a second connection BLOCKS, then proceeds.

    This is what makes claim_daily safe across PROCESSES, which the in-memory
    RLock above cannot demonstrate -- an RLock is per-process and invisible to
    another worker.
    """
    import threading
    import time

    from player_store import GLOBAL_LOCK_ID

    holder = _pg_connect()
    waiter = _pg_connect()  # a separate session: a lock is held per transaction
    released = threading.Event()
    got_lock = threading.Event()
    observations = {}

    def take_the_lock():
        """A second worker asking for the same lock while the first holds it."""
        try:
            waiter.set_session(isolation_level="READ COMMITTED", autocommit=False)
            with waiter.cursor() as cur:
                started = time.monotonic()
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (GLOBAL_LOCK_ID,))
                observations["waited"] = time.monotonic() - started
                observations["acquired_after_release"] = released.is_set()
                got_lock.set()
        except Exception as exc:  # recorded, asserted on by the main thread
            observations["error"] = repr(exc)
            got_lock.set()

    try:
        holder.set_session(isolation_level="READ COMMITTED", autocommit=False)
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (GLOBAL_LOCK_ID,))

        worker = threading.Thread(target=take_the_lock, daemon=True)
        worker.start()

        # The lock is held. A waiter that had not blocked would have finished
        # by now, so this wait IS the assertion: BLOCKING_WINDOW is long enough
        # that a non-blocking lock would blow straight through it.
        BLOCKING_WINDOW = 1.0
        time.sleep(BLOCKING_WINDOW)
        assert not got_lock.is_set(), (
            "the second connection acquired the advisory lock while the first "
            "still held it -- it is not excluding anybody"
        )

        # Release, and the waiter must now be let through.
        holder.rollback()
        released.set()
        worker.join(timeout=BLOCKING_WINDOW + 10.0)
        assert got_lock.is_set(), (
            f"the waiter never acquired the lock after the holder released; "
            f"observations={observations}"
        )
        assert "error" not in observations, observations["error"]
        assert observations["acquired_after_release"] is True, (
            "the waiter acquired the lock but the holder had not released it, "
            "so the two overlapped"
        )
        # And the wait really was a wait, not a scheduling accident.
        assert observations["waited"] >= BLOCKING_WINDOW * 0.5, (
            f"the waiter reported only {observations['waited']:.3f}s of "
            f"blocking, expected at least {BLOCKING_WINDOW * 0.5:.3f}s"
        )
    finally:
        holder.close()
        waiter.close()


# --- a points row that is not a number at all ------------------------------
#
# ``state["points"]`` DECIDES the tier, so this is a read that can take a whole
# status panel and a whole claim offline. Measured on the tree before the fix:
# a row holding ``"abc"`` or ``[]`` raised out of ``get_vip_status`` and both
# ``/vip/<id>/status`` and ``/vip/<id>/claim-daily`` answered 500, with no test
# pinning that. ``_read_stored_int`` answers the default for a value that is not
# a number, so the row reads as zero and the tier ladder still decides.
#
# WHAT IS DELIBERATELY NOT HERE: ``inf`` / ``-inf`` / ``nan`` and ``10 ** 400``.
# ``test_vip_contract.py`` pins that a non-finite total must RAISE (a silent
# zero would resolve a corrupt save to a paid tier) and that an unbounded int is
# a LEGAL top-tier total. Those two cases are the opposite requirement and are
# already covered there; duplicating them here would assert the bug.

NOT_A_NUMBER = [
    ("str", "abc"),
    ("str_digits", "not-a-number-7"),
    ("list", [1]),
    ("dict", {"points": 10}),
    ("tuple", (1,)),
    ("none", None),
    ("set", {1, 2}),
]


@pytest.mark.parametrize("label,value", NOT_A_NUMBER, ids=[c[0] for c in NOT_A_NUMBER])
def test_status_reads_a_points_row_that_is_not_a_number_as_zero(label, value):
    """A points row that is not a number must not crash the tier read."""
    player = make_player()
    player[V.STATE_KEY] = {"points": value}

    status = V.get_vip_status(player, now=at(1000))  # must not raise

    assert status["points"] == 0, f"{label}: {value!r} was not read as the default"
    assert status["tier_id"] == V.VIP_TIERS[0]["id"], (
        f"{label}: an unreadable points row must land on the bottom tier, "
        f"not decide a tier by accident"
    )


@pytest.mark.parametrize("label,value", NOT_A_NUMBER, ids=[c[0] for c in NOT_A_NUMBER])
def test_the_earn_path_survives_a_points_row_that_is_not_a_number(label, value):
    """``add_vip_points`` reads the same row, and it is the money path.

    A corrupt stored total must not stop a player EARNING -- it reads as zero
    and the grant lands normally.
    """
    player = make_player()
    player[V.STATE_KEY] = {"points": value}

    result = V.add_vip_points(player, 250)

    assert result["points_gained"] == 250
    assert player[V.STATE_KEY]["points"] == 250, (
        f"{label}: earning did not recover from an unreadable stored total"
    )


def test_a_last_claim_stamp_that_is_not_a_number_does_not_crash_the_status():
    """The day stamp is stored too, so it gets the same treatment.

    Unreadable is read as ABSENT, which is what a missing key already means.
    That is the safe direction for the player: they are not locked out of a
    grant they have not claimed, and a genuine second press is still stopped
    by the freshly written day stamp.
    """
    player = make_player()
    player[V.STATE_KEY] = {"points": 10, V.STATE_KEY_LAST_CLAIM: "yesterday"}

    status = V.get_vip_status(player, now=at(1000))  # must not raise

    assert status["claimed_today"] is False


def test_the_claim_preview_survives_a_points_row_that_is_not_a_number():
    """``evaluate_claim`` is the read-only twin behind the claim preview."""
    player = make_player()
    player[V.STATE_KEY] = {"points": "abc", V.STATE_KEY_LAST_CLAIM: "abc"}

    preview = V.evaluate_claim(player, now=at(1000))  # must not raise

    assert preview["ok"] is True
    assert preview["already_claimed"] is False


def test_the_guard_reads_the_row_without_rewriting_it():
    """The guard must not silently REPAIR the stored row.

    Writing a repaired value back would be a mutation smuggled into a function
    documented as PURE, and it would destroy the evidence a repair pass needs.
    """
    player = make_player()
    player[V.STATE_KEY] = {"points": "abc"}

    V.get_vip_status(player, now=at(1000))

    assert player[V.STATE_KEY]["points"] == "abc", (
        "get_vip_status rewrote the stored points row; it is documented PURE"
    )


def test_a_legitimate_points_value_still_decides_the_tier():
    """Control: the guard must not swallow a REAL reading.

    Without this, a guard answering the default for everything would pass every
    test above while breaking the feature.
    """
    player = make_player()
    V.add_vip_points(player, 250)

    status = V.get_vip_status(player, now=at(1000))

    assert status["points"] == 250
    assert status["next_tier_id"] == V.VIP_TIERS[1]["id"]
    assert 0.0 < status["progress_pct"] < 100.0


def test_a_numeric_string_still_parses_rather_than_reading_as_zero():
    """The guard must not narrow what a valid stored value may be.

    ``"250"`` parsed to 250 before the guard and still must: a save written by
    an older build as a string is a legitimate reading, not corruption.
    """
    player = make_player()
    player[V.STATE_KEY] = {"points": "250"}

    status = V.get_vip_status(player, now=at(1000))

    assert status["points"] == 250
    assert status["next_tier_id"] == V.VIP_TIERS[1]["id"]
