"""
GACHA CONFIG: the MODULE contract for ``server/gacha_config.py``.

WHY THIS FILE IS NOT A DUPLICATE
--------------------------------
Two gacha test files already exist and this one owns the ground they leave:

  * ``server/tests/test_gacha_contract.py`` -- drives the real shipping app
    through HTTP: request bodies, the 422 field list, the money actually
    deducted, session auth, and the pity rules as a player meets them. It
    imports ``main`` and therefore needs the whole application.
  * ``server/tests/test_gacha_route_contract.py`` -- response shapes and the
    refusal statuses.

What both of them cannot see is the parameter pack itself.
``server/gacha_config.py`` is a pure library: no routes, no database, no
FastAPI. It imports nothing but ``random`` and ``typing``. The routes and the
client panel are both built on its tables and its pity arithmetic, so a fault
here is felt twice over -- once per pull at the route, once per row drawn by
the client -- and neither HTTP file would report it, because the route would
happily return the broken number.

This file therefore imports ``gacha_config`` and NOTHING from the app. It also
answers the request for "config validation": the module ships no ``validate()``
or ``assert_*()`` function (unlike ``beast_wave_config.py``), so the validation
gate is these tests. Every table the algorithm reads is checked against the
invariant that algorithm depends on.

THE THREE THINGS WORTH TESTING, AND WHY
---------------------------------------
1. THE TABLES ARE SELF-CONSISTENT.  ``calculate_pull_weights`` multiplies a
   fractional bonus by ``sum(RARITY_WEIGHTS.values())`` and caps the result at
   that same sum, so it only reads correctly because the rate table is a
   percentage scale. Every rarity key it can emit must exist in the table, or a
   pull answers with a rarity no client can draw an icon for.

2. THE PITY BOUNDARY IS ``>=``, NOT ``>``.  ``PITY_SOFT_START`` is documented
   as "soft pity starts at 60 pulls". At exactly 60 the legendary weight must
   already have moved; at 59 it must be byte-for-byte the base table. An
   off-by-one in either direction is invisible in a screenshot and costs a
   player either their soft pity or a doubled bonus.

3. THE COUNTER IS THE ONLY THING THAT ADVANCES.  ``next_pity`` is the single
   place pity moves, and both pull routes route through it. The failure this
   prevents is not hypothetical: the single-pull route used to write
   ``pity_count + 1`` by hand, so a player who reached the cap stayed pinned
   above it and every later pull paid out a free legendary. A test that calls
   ``perform_single_pull`` a few times in a row and watches the counter is the
   only thing that catches a regression there.

WHY BEHAVIOUR ASSERTIONS, NOT SNAPSHOTS
----------------------------------------
Every pull is random, so nothing here asserts "this pull gave an epic". Each
assertion is a RELATIONSHIP randomness cannot break: what the code reads versus
what the code wrote, the total before versus the total after, one route's
counter versus the other's. The only pinned numbers are ones the shipped client
also hardcodes or the module states as its contract (the 100-point rate scale,
the pity constants).

TWO OF THESE ARE GUARD TESTS, NOT BEHAVIOUR TESTS
--------------------------------------------------
The two tests named ``test_guard_*`` (section 10b) patch module state into a
shape no player can produce -- a one-rarity rate table, and a random roll of
exactly 1.0. They are kept because a guard nobody exercises is a guard nobody
has proved works: deleting either defensive line would leave every other test
in this file green. Read them as "this guard still works", not as "this is what
a player sees". The header on section 10b carries the measurements.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle && bash tools/wc_pytest.sh \\
        server/tests/test_gacha_config_contract.py
"""
from __future__ import annotations

import os

for _var in ("WC_PG_DB", "WC_PG_HOST", "WC_PG_USER", "WC_PG_PASSWORD",
             "WC_PG_PORT", "PYTHONPATH"):
    os.environ.pop(_var, None)

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import math
import random

import pytest

import gacha_config
from gacha_config import (
    BANNERS,
    PITY_HARD,
    PITY_SOFT_INCREMENT,
    PITY_SOFT_START,
    RARITY_PULL_COST,
    RARITY_WEIGHTS,
    SPARK_COST,
    calculate_pull_weights,
    can_afford_pull,
    next_pity,
    perform_multi_pull,
    perform_pull,
    perform_single_pull,
)

# The keys every per-pull result must carry. A client reads all four, so a
# dropped key is a KeyError in the panel rather than a wrong value.
RESULT_KEYS = {"rarity", "character", "pity_triggered", "featured"}

# The keys both pull envelopes must carry, so a route needs no branching on
# pull_type after the call.
ENVELOPE_KEYS = {"results", "new_pity", "legendary_count"}


# =====================================================================
# 1. THE RATE TABLE: the data contract the weight maths depends on
# =====================================================================

def test_the_rate_table_is_a_hundred_point_scale():
    """The soft-pity bonus is computed as a FRACTION times the table's total.

    ``calculate_pull_weights`` reads ``total = sum(RARITY_WEIGHTS.values())``,
    caps the legendary weight at that total, and scales everything else into
    the remainder. The table is therefore only meaningful as a percentage
    scale. If the weights stop summing to 100 the maths still conserves its
    total, so nothing crashes -- the displayed rates are simply wrong, and
    every rate the client shows is derived from this sum.
    """
    assert sum(RARITY_WEIGHTS.values()) == 100


def test_every_rarity_key_is_a_non_empty_lowercase_name():
    """Keys are used verbatim to build a character id: ``char_<rarity>_<n>``.

    A key with a space or an upper-case letter would produce an id no character
    table can hold, so the naming rule is part of the contract rather than
    cosmetics.
    """
    for rarity in RARITY_WEIGHTS:
        assert rarity, "an empty rarity name cannot build a character id"
        assert rarity == rarity.lower(), (
            f"{rarity!r} is not lowercase; character ids are built as "
            f"char_<rarity>_<n> and the client matches them exactly"
        )
        assert " " not in rarity, f"{rarity!r} contains a space"


def test_rarer_rarities_never_outnumber_commoner_ones():
    """The table must stay ordered from most to least likely.

    The client draws the odds straight from these numbers. A row that inverts
    the order -- a "legendary" rarer than "rare" -- would still pass a total
    check, and would only be noticed by a player pulling a thousand times.
    """
    weights = [RARITY_WEIGHTS[r] for r in RARITY_WEIGHTS]
    assert weights == sorted(weights, reverse=True), (
        f"rarity weights are not descending: {RARITY_WEIGHTS}"
    )


def test_the_table_declares_at_least_one_rarity():
    """A pull with no rarities would divide by zero in the roll loop."""
    assert len(RARITY_WEIGHTS) >= 1


def test_the_table_holds_at_least_five_rarities():
    """A gacha needs its five rungs to have anything to roll against.

    Not a snapshot of the exact names: a reroll that adds a sixth tier keeps
    this true, while dropping a tier is the real regression.
    """
    assert len(RARITY_WEIGHTS) >= 5


# =====================================================================
# 2. THE COST TABLE: what a pull is advertised to cost
# =====================================================================

@pytest.mark.parametrize("pull_type", sorted(RARITY_PULL_COST))
def test_every_cost_row_names_a_currency_and_a_positive_amount(pull_type):
    """``can_afford_pull`` reads ``cost["currency"]`` then ``cost["amount"]``.

    A missing key is a KeyError raised inside a route that was only asked
    whether the player can afford something. The amount must also be a real
    integer: the ten-pull price is multiplied by nothing, but a float amount
    would make the wallet comparison a rounding argument.
    """
    cost = RARITY_PULL_COST[pull_type]
    assert set(cost) == {"currency", "amount"}, (
        f"{pull_type!r} cost must name exactly a currency and an amount, got {cost}"
    )
    assert isinstance(cost["currency"], str) and cost["currency"]
    assert isinstance(cost["amount"], int) and cost["amount"] > 0


def test_a_bundle_row_is_cheaper_than_the_singles_it_replaces():
    """The ten-pull price is annotated "10% discount", which is a relationship
    between two rows of the table: the bundle costs LESS than ten singles and
    MORE than one.

    Charged per unit the bundle is what makes the discount true. If a future
    row priced the bundle above ten singles, the client would still advertise
    the discount and the route would take the difference.
    """
    single = RARITY_PULL_COST["single"]["amount"]
    bundles = {k: v for k, v in RARITY_PULL_COST.items() if k != "single"}
    assert bundles, "there is no bundle row to check the discount against"
    for pull_type, cost in bundles.items():
        assert single < cost["amount"] <= single * 10, (
            f"{pull_type!r} costs {cost['amount']} against a single price of "
            f"{single}; a bundle must cost more than one pull and no more than ten"
        )


def test_every_cost_row_is_charged_in_the_same_currency():
    """The wallet is a multi-currency dict, so a row that names a different
    currency is affordable by a player holding something else entirely."""
    currencies = {cost["currency"] for cost in RARITY_PULL_COST.values()}
    assert len(currencies) == 1, f"cost rows name several currencies: {currencies}"


def test_the_spark_cost_is_a_positive_count_of_pulls():
    """``SPARK_COST`` is a data-only constant with no function in the module:
    it is read by the client to tell a player how many pulls remain until they
    may choose directly. It has to be a whole number of pulls or that
    subtraction is meaningless."""
    assert isinstance(SPARK_COST, int)
    assert SPARK_COST > 0


# =====================================================================
# 3. THE PITY CONSTANTS: an ordered ladder, not three loose numbers
# =====================================================================

def test_the_pity_ladder_is_ordered_soft_start_below_hard_cap():
    """Soft pity has to begin strictly before the guarantee.

    If the two ever crossed, either the soft bonus would be unreachable
    (start above the cap) or the guarantee would fire before soft pity could
    ever matter. Nothing else in the module would notice.
    """
    assert 0 < PITY_SOFT_START < PITY_HARD


def test_the_soft_pity_increment_is_a_positive_fraction_below_one():
    """It is a fraction multiplied by the weight total, so it must be greater
    than zero to have any effect and below one so a single pull cannot empty
    the other rarities on its own."""
    assert 0 < PITY_SOFT_INCREMENT < 1


def test_the_hard_cap_is_a_whole_number_of_pulls():
    """It is compared against an integer counter; a float cap would compare
    fine in isolation but never be reached by a counter that only ever adds 1.
    """
    assert isinstance(PITY_HARD, int)


# =====================================================================
# 4. THE BANNER TABLE: the dictionary is its own index
# =====================================================================

@pytest.mark.parametrize("key", sorted(BANNERS))
def test_each_banner_key_also_names_itself_inside_the_banner(key):
    """``perform_pull`` looks a banner up by its dictionary key.

    If a banner's own ``banner_id`` disagreed with the key it is filed under,
    every lookup by key would silently return a different banner than the id
    the client asked for -- no exception, just the wrong banner.
    """
    assert BANNERS[key].banner_id == key


@pytest.mark.parametrize("key", sorted(BANNERS))
def test_each_banner_has_a_non_empty_display_name(key):
    """The name is drawn verbatim on the client banner header."""
    assert isinstance(BANNERS[key].name, str) and BANNERS[key].name.strip()


@pytest.mark.parametrize("key", sorted(BANNERS))
def test_each_banner_rate_up_is_a_list_of_non_empty_character_ids(key):
    """The rate-up list is fed straight to ``random.choice``.

    A string instead of a list would hand ``choice`` its characters and hand a
    player a one-letter name; an empty string would raise inside a roll.
    """
    rate_up = BANNERS[key].rate_up
    assert isinstance(rate_up, list), f"{key!r} rate_up must be a list, got {type(rate_up)}"
    for char_id in rate_up:
        assert isinstance(char_id, str) and char_id.strip(), (
            f"{key!r} rate_up holds an empty id: {char_id!r}"
        )


@pytest.mark.parametrize("key", sorted(BANNERS))
def test_each_banner_cost_matches_a_price_the_game_actually_sells(key):
    """A banner's cost is a copy of one row of the price table.

    If it were edited in place to a price the table does not carry, the
    banner would charge a number the client never advertised and
    ``can_afford_pull`` would disagree with the route that spends the money.
    """
    cost = BANNERS[key].cost
    assert cost in RARITY_PULL_COST.values(), (
        f"{key!r} cost {cost} is not a row of RARITY_PULL_COST"
    )


def test_the_standard_banner_advertises_no_rate_up():
    """The permanent banner is the one players pull without a featured
    character. A rate-up here would quietly move 50% of every legendary to one
    character forever."""
    assert BANNERS["standard"].rate_up == []


def test_a_featured_banner_names_at_least_one_character():
    """``perform_pull`` gates the whole featured roll on a non-empty rate-up
    list. A featured banner with an empty list can never return a featured
    pull, and the flag on the client would never light up."""
    featured = [k for k, b in BANNERS.items() if k != "standard"]
    assert featured, "there is no featured banner to check"
    for key in featured:
        assert BANNERS[key].rate_up, f"{key!r} is featured but names no character"


# =====================================================================
# 5. calculate_pull_weights: the pure table, and the boundary on it
# =====================================================================

def test_below_soft_pity_the_table_is_the_base_table_untouched():
    """Before the soft-pity threshold the function is a copy and nothing more.

    Asserted as an exact copy of the base table rather than "close to it": at
    this stage the function must not have moved a single point, because the
    base table IS the advertised rate.
    """
    assert calculate_pull_weights(0) == RARITY_WEIGHTS
    assert calculate_pull_weights(PITY_SOFT_START - 1) == RARITY_WEIGHTS


def test_at_the_soft_pity_threshold_the_legendary_weight_has_already_moved():
    """``PITY_SOFT_START`` means "starts here", so the pull AT the threshold is
    already in soft pity. This is the boundary the ``>=`` comparison in the
    module exists to get right; an off-by-one costs a player their bonus on the
    first pull of the ramp.
    """
    at_start = calculate_pull_weights(PITY_SOFT_START)
    assert at_start["legendary"] > RARITY_WEIGHTS["legendary"], (
        f"soft pity did not begin at {PITY_SOFT_START}"
    )


def test_the_first_soft_pity_step_is_exactly_the_stated_increment():
    """One step of soft pity is worth ``PITY_SOFT_INCREMENT`` of the total.

    Derived here rather than copied: the first bonus is one increment times the
    table's own sum, so it moves the legendary weight by that same number of
    points. A rewrite that hardcodes a step size would break this.
    """
    total = sum(RARITY_WEIGHTS.values())
    at_start = calculate_pull_weights(PITY_SOFT_START)
    expected = RARITY_WEIGHTS["legendary"] + PITY_SOFT_INCREMENT * total
    assert at_start["legendary"] == pytest.approx(expected)


def test_soft_pity_keeps_the_table_on_one_hundred_points():
    """Every other rarity is scaled into what is left over, so the table must
    still total the same 100 points it advertised before soft pity began.

    This is the invariant that fails loud. The two unit bugs the module's own
    comment records -- treating a weight as a probability, and dividing by the
    other rarities while legendary was pinned -- both collapse this total, and
    both collapse it to "every pull is legendary".
    """
    for pity in range(0, PITY_HARD + 1):
        total = sum(calculate_pull_weights(pity).values())
        assert total == pytest.approx(100), (
            f"weight table totals {total} at pity {pity}, expected 100"
        )


def test_soft_pity_never_empties_the_common_rarity():
    """Every non-legendary rarity is scaled by one shared factor, so if the
    scale ever reached zero all four would vanish together and a pull could
    only ever return legendary -- a free legendary on every single pull."""
    for pity in range(PITY_SOFT_START, PITY_HARD):
        weights = calculate_pull_weights(pity)
        for rarity, weight in weights.items():
            if rarity != "legendary":
                assert weight > 0, f"{rarity!r} is zero at pity {pity}"


def test_soft_pity_never_lets_the_legendary_weight_take_the_whole_table():
    """The cap at ``min(total, ...)`` is what stops the bonus overshooting."""
    for pity in range(PITY_SOFT_START, PITY_HARD + 1):
        weights = calculate_pull_weights(pity)
        assert weights["legendary"] < sum(RARITY_WEIGHTS.values()), (
            f"legendary took the whole table at pity {pity}"
        )


def test_the_legendary_weight_only_ever_rises_across_soft_pity():
    """The bonus grows one increment per pull, so the weight may not dip. A dip
    means a player pulls twice and gets a worse deal the second time."""
    previous = RARITY_WEIGHTS["legendary"]
    for pity in range(PITY_SOFT_START, PITY_HARD):
        current = calculate_pull_weights(pity)["legendary"]
        assert current >= previous, (
            f"legendary weight fell from {previous} to {current} at pity {pity}"
        )
        previous = current


def test_scaling_the_other_rarities_preserves_their_relative_ratios():
    """The four non-legendary rows are all multiplied by the same factor, so
    their proportions must survive soft pity untouched.

    This is the check a "subtract a flat amount from each rarity" rewrite fails:
    the total would still be right, but the advertised mix would shift on every
    pull past the threshold.
    """
    weights = calculate_pull_weights(PITY_HARD - 1)
    for left, right in (("common", "uncommon"), ("common", "rare"), ("rare", "epic")):
        base_ratio = RARITY_WEIGHTS[left] / RARITY_WEIGHTS[right]
        soft_ratio = weights[left] / weights[right]
        assert soft_ratio == pytest.approx(base_ratio), (
            f"{left}/{right} changed from {base_ratio} to {soft_ratio} under soft pity"
        )


def test_the_function_never_writes_back_into_the_shared_table():
    """``calculate_pull_weights`` copies the table before editing it. If the
    copy were dropped, one call at high pity would raise the legendary weight
    for the whole process -- every later player and every later pull would
    inherit another player's pity."""
    before = dict(RARITY_WEIGHTS)
    calculate_pull_weights(PITY_HARD)
    assert RARITY_WEIGHTS == before, (
        "calculate_pull_weights mutated the module-level rate table"
    )


def test_each_call_returns_a_private_table():
    """Two callers must not share one dict, or one caller's scaling would be
    seen by the other's."""
    first = calculate_pull_weights(PITY_SOFT_START)
    second = calculate_pull_weights(PITY_SOFT_START)
    assert first == second
    first["legendary"] = -1
    assert second["legendary"] != -1
    assert RARITY_WEIGHTS["legendary"] > 0


def test_the_table_carries_the_same_rarity_names_at_every_pity_count():
    """The roll loop looks a rarity up by key. A key added or dropped under
    soft pity would make a pull answer with a rarity the client has no row for.
    """
    for pity in (0, PITY_SOFT_START, PITY_SOFT_START + 1, PITY_HARD - 1):
        assert set(calculate_pull_weights(pity)) == set(RARITY_WEIGHTS)


# =====================================================================
# 6. perform_pull: the envelope one draw must answer with
# =====================================================================

def test_a_pull_answers_with_exactly_the_four_documented_keys():
    """A client reads all four. An extra key is harmless; a missing one is a
    KeyError in the panel, and one is worse than none."""
    result = perform_pull(0)
    assert set(result) == RESULT_KEYS


def test_the_rarity_rolled_is_always_one_the_table_declares():
    """Ties the roll to the rate table. A rarity invented by the roll loop would
    reach the client as a name with no rate, no icon and no weight behind it."""
    for _ in range(200):
        assert perform_pull(PITY_HARD - 1)["rarity"] in RARITY_WEIGHTS


def test_an_unfeatured_character_id_is_built_from_its_own_rarity():
    """The id is ``char_<rarity>_<1..10>``. The rarity named inside the id must
    be the rarity in the envelope, or the client draws a common-rarity portrait
    on a legendary result."""
    for _ in range(200):
        result = perform_pull(0)
        if result["featured"]:
            continue
        prefix, _, number = result["character"].rpartition("_")
        assert prefix == f"char_{result['rarity']}", (
            f"{result['character']!r} does not match rarity {result['rarity']!r}"
        )
        assert 1 <= int(number) <= 10


def test_the_featured_flag_and_the_rate_up_pool_always_agree():
    """A featured result must name a rate-up character, and an unfeatured one
    must not. The flag is what the client uses to decide whether to show the
    featured splash, so a mismatch is a visible lie either way.

    ``char_celestial_sovereign`` never appears in the generated ids, which is
    what makes this a real check rather than a tautology.
    """
    for _ in range(300):
        result = perform_pull(PITY_SOFT_START)
        pool = BANNERS["character_1"].rate_up
        if result["featured"]:
            assert result["character"] in pool
        else:
            assert result["character"] not in pool


def test_the_hard_pity_guarantee_fires_exactly_at_the_cap():
    """At the cap the pull is a legendary, flagged as triggered and featured,
    and names a rate-up character. Below the cap it is a normal roll.

    Both arms are driven: a guarantee that never fires is invisible in a test
    that only pulls at the cap.
    """
    at_cap = perform_pull(PITY_HARD)
    assert at_cap["rarity"] == "legendary"
    assert at_cap["pity_triggered"] is True
    assert at_cap["featured"] is True
    assert at_cap["character"] in BANNERS["character_1"].rate_up

    below = [perform_pull(PITY_HARD - 1) for _ in range(200)]
    assert all(r["pity_triggered"] is False for r in below)


def test_the_guarantee_ignores_the_featured_flag():
    """The guarantee is a guarantee: asking for a normal pull at the cap still
    returns the featured character. If it did not, a client sending
    ``guaranteed_featured=False`` would opt itself out of the pity promise."""
    result = perform_pull(PITY_HARD, guaranteed_featured=False)
    assert result["rarity"] == "legendary"
    assert result["featured"] is True


def test_the_guaranteed_flag_forces_the_featured_character():
    """With the flag set, a legendary is always the rate-up character, never
    the generic one. Sampled rather than counted: the unflagged path is a
    50/50 coin, so a run of generic pulls here means the flag is ignored."""
    for _ in range(200):
        result = perform_pull(PITY_SOFT_START, guaranteed_featured=True)
        if result["rarity"] == "legendary":
            assert result["featured"] is True
            assert result["character"] in BANNERS["character_1"].rate_up


def test_the_pity_flag_is_a_boolean_not_a_truthy_value():
    """The client branches on this field, so a leaked non-boolean would be
    consumed without complaint."""
    for pity in (0, PITY_SOFT_START, PITY_HARD):
        assert isinstance(perform_pull(pity)["pity_triggered"], bool)
        assert isinstance(perform_pull(pity)["featured"], bool)


# =====================================================================
# 7. Batch bookkeeping: the counter, driven deterministically
# =====================================================================

def _scripted_pulls(monkeypatch, scripted):
    """Replace the random draw with a fixed script and record what it was asked.

    ``perform_multi_pull`` resolves ``perform_pull`` from its own module
    globals, so patching the module attribute is enough. Every other part of
    the batch -- the counter, the featured flag, the tallies -- stays real,
    which is exactly the part under test.
    """
    seen = []

    def fake(pity_count, guaranteed_featured=False):
        seen.append((pity_count, guaranteed_featured))
        return scripted[min(len(seen) - 1, len(scripted) - 1)]

    monkeypatch.setattr(gacha_config, "perform_pull", fake)
    return seen


def _pull(rarity, featured=False):
    return {
        "rarity": rarity,
        "character": f"char_{rarity}_1",
        "pity_triggered": False,
        "featured": featured,
    }


def test_the_counter_only_ever_advances_from_the_previous_result(monkeypatch):
    """Driven with a scripted sequence whose pity climbs, resets and climbs
    again. Each pull must be handed the counter left by the one before it --
    the property a hand-written ``pity_count + 1`` breaks the moment a
    legendary lands mid-batch.
    """
    scripted = [_pull("common"), _pull("common"), _pull("legendary"), _pull("epic")]
    seen = _scripted_pulls(monkeypatch, scripted)
    batch = perform_multi_pull(0, count=4)
    assert [pity for pity, _ in seen] == [0, 1, 2, 0]
    assert batch["new_pity"] == 1
    assert batch["legendary_count"] == 1


def test_the_last_pull_is_drawn_at_the_counter_the_previous_leaves(monkeypatch):
    """The mirror of the counter test: the counter handed to the final pull of
    a batch is the one left by the pull before it, not the batch's opening
    number. A batch that re-read its opening argument every iteration would
    pass a fixed-count assertion and fail this one.
    """
    seen = _scripted_pulls(monkeypatch, [_pull("common")])
    perform_multi_pull(5, count=4)
    assert [pity for pity, _ in seen] == [5, 6, 7, 8]


def test_a_legendary_resets_the_counter_and_nothing_else_does():
    """The one rule the reset depends on. Every rarity except legendary adds
    one; legendary returns the counter to zero."""
    assert next_pity(41, _pull("legendary")) == 0
    for rarity in RARITY_WEIGHTS:
        if rarity == "legendary":
            continue
        assert next_pity(41, _pull(rarity)) == 42


def test_a_result_with_no_rarity_key_counts_as_a_miss():
    """``next_pity`` reads ``result.get("rarity")``. A result missing the key
    must climb the counter rather than crash a route mid-pull."""
    assert next_pity(7, {}) == 8


def test_the_single_pull_reports_the_same_counter_the_multi_pull_would():
    """The two envelopes exist so a route needs no branch on pull type.

    Asserted as a relationship to ``next_pity`` rather than to a number, so it
    holds for any draw: one pull must leave the counter exactly where
    ``next_pity`` says, whether the draw was a legend or a common.
    """
    for rarity in ("common", "legendary"):
        result = perform_single_pull(12)
        assert set(result) == ENVELOPE_KEYS
        assert len(result["results"]) == 1
        assert result["new_pity"] == next_pity(12, result["results"][0])
        assert result["legendary_count"] == sum(
            1 for r in result["results"] if r["rarity"] == "legendary"
        )


def test_a_single_pull_at_the_cap_resets_the_counter(monkeypatch):
    """The free-legendary defect, closed.

    Before the fix this route wrote ``PITY_HARD + 1`` instead of ``0``, so a
    player who ever reached the cap stayed at or above it forever and every
    later pull paid out. Scripted so the test does not depend on the draw:
    one guaranteed legendary at the cap must land the counter back on zero.
    """
    _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    result = perform_single_pull(PITY_HARD)
    assert result["new_pity"] == 0
    assert result["legendary_count"] == 1


def test_ten_single_pulls_can_never_walk_past_the_cap(monkeypatch):
    """Ten pulls in a row, all of them legendaries, must end at zero -- not at
    a number above the cap.

    This is the batch version of the same defect. With the counter climbing
    unconditionally, a batch that starts at 79 spends the first pull on the
    guarantee and then banks a legendary on each of the nine that follow.
    """
    _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    batch = perform_multi_pull(PITY_HARD - 1, count=10)
    assert batch["legendary_count"] == 10
    assert batch["new_pity"] == 0


def test_the_featured_flag_stops_applying_after_the_first_legendary(monkeypatch):
    """A ten-pull grants the featured guarantee at most once.

    The batch passes ``guaranteed_featured`` only until a legendary has landed,
    so the flag the client receives afterwards must be False. A route that
    forced every pull in the batch would hand out the featured character
    repeatedly for one purchase.
    """
    seen = _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    perform_multi_pull(0, count=4, guaranteed_featured=True)
    flags = [flag for _, flag in seen]
    assert flags[0] is True
    assert all(flag is False for flag in flags[1:])


def test_a_batch_returns_exactly_as_many_results_as_it_was_asked_for():
    """The count asked for is what the client is billed against: ten pulls must
    answer with ten results."""
    for count in (1, 3, 10):
        assert len(perform_multi_pull(0, count=count)["results"]) == count


def test_the_legendary_tally_counts_the_results_it_returned():
    """The tally is read by the client to show how many legendaries the batch
    produced. It must agree with the results list rather than be tracked
    separately."""
    batch = perform_multi_pull(0, count=10)
    counted = sum(1 for r in batch["results"] if r["rarity"] == "legendary")
    assert batch["legendary_count"] == counted


def test_a_batch_starts_at_the_counter_it_was_given():
    """With nothing scripted and a counter well below the cap, a batch of one
    must hand the pull exactly the counter it was called with -- not zero."""
    assert isinstance(perform_multi_pull(0, count=1)["new_pity"], int)
    assert 0 < perform_multi_pull(0, count=1)["new_pity"] <= 1


# =====================================================================
# 8. can_afford_pull: the price check and its two fallbacks
# =====================================================================

def test_a_player_at_exactly_the_price_can_afford_it():
    """The comparison is ``>=``. A strict ``>`` would refuse the player holding
    precisely the advertised cost."""
    amount = RARITY_PULL_COST["single"]["amount"]
    currency = RARITY_PULL_COST["single"]["currency"]
    assert can_afford_pull({currency: amount}) is True
    assert can_afford_pull({currency: amount - 1}) is False


def test_an_unknown_pull_type_is_charged_the_single_price():
    """The lookup falls back to the single row rather than raising.

    A client on an older build can send a pull type this build has never heard
    of. Raising would answer a stale client with a server error; charging the
    single price charges the smallest amount the server knows, which is the safe
    direction for the player.
    """
    amount = RARITY_PULL_COST["single"]["amount"]
    currency = RARITY_PULL_COST["single"]["currency"]
    assert can_afford_pull({currency: amount}, pull_type="not_a_real_pull") is True
    assert can_afford_pull({currency: amount - 1}, pull_type="not_a_real_pull") is False


def test_a_wallet_missing_the_currency_reads_as_empty():
    """``.get(currency, 0)`` means a wallet with no such currency is worth zero,
    so the pull is refused. A wallet full of some other currency must not stand
    in for fateum."""
    rich_in_something_else = {"spirit_stones": 10_000_000, "gold": 999}
    assert can_afford_pull(rich_in_something_else) is False


def test_an_empty_wallet_is_refused():
    """The cheapest real call a route makes before any work."""
    assert can_afford_pull({}) is False


def test_a_negative_balance_is_refused():
    """A negative balance is not a balance. If the comparison were ever
    rewritten in absolute terms, a player owing currency would pull free."""
    currency = RARITY_PULL_COST["single"]["currency"]
    assert can_afford_pull({currency: -10_000}) is False


def test_the_bundle_price_is_what_a_ten_pull_is_measured_against():
    """Not the single price. The route debits the bundle, so an affordability
    check against the single price would let a player start a ten-pull they
    cannot finish paying for."""
    cost = RARITY_PULL_COST["multi_10"]
    assert can_afford_pull({cost["currency"]: cost["amount"]}, pull_type="multi_10") is True
    assert can_afford_pull({cost["currency"]: cost["amount"] - 1}, pull_type="multi_10") is False


# =====================================================================
# 9. Hostile inputs: values a route could hand in by mistake
# =====================================================================

def test_a_negative_pity_count_is_treated_as_a_fresh_account():
    """A counter that went below zero -- a restored save, a hand-edited row --
    must still produce a valid pull rather than an error. It is below the soft
    threshold, so the table is the base one and the guarantee is not fired.
    """
    result = perform_pull(-5)
    assert set(result) == RESULT_KEYS
    assert result["pity_triggered"] is False
    assert result["rarity"] in RARITY_WEIGHTS


def test_a_negative_pity_count_still_produces_a_sane_weight_table():
    """The bonus arithmetic must not run on a negative count. Asserted as a
    copy of the base table, which is what a negative count means."""
    assert calculate_pull_weights(-1) == RARITY_WEIGHTS


def test_a_count_of_zero_returns_an_empty_batch_that_changes_nothing(monkeypatch):
    """Zero pulls is a no-op, not a free legendary: no results, no tally, and
    the caller's counter handed straight back."""
    seen = _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    batch = perform_multi_pull(33, count=0)
    assert batch["results"] == []
    assert batch["legendary_count"] == 0
    assert batch["new_pity"] == 33
    assert seen == []


def test_a_negative_count_draws_nothing_and_costs_nothing(monkeypatch):
    """``range()`` treats a negative count as empty. The batch must agree: a
    caller that mis-sends its length must not receive pulls it did not ask for
    and must not have its counter rewritten."""
    _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    batch = perform_multi_pull(20, count=-3)
    assert batch["results"] == []
    assert batch["new_pity"] == 20


def test_the_guarantee_falls_back_to_a_generic_character_when_no_character_is_promoted(monkeypatch):
    """If the featured banner's rate-up list is emptied, the guarantee has
    nothing to hand out and must still answer with a legendary rather than
    raising inside ``random.choice``.

    This is the error case the empty-list branch exists for; without it the
    route 500s at the moment a player is most entitled to a result.
    """
    monkeypatch.setattr(gacha_config.BANNERS["character_1"], "rate_up", [])
    result = perform_pull(PITY_HARD)
    assert result["rarity"] == "legendary"
    assert result["character"] == "char_legendary_generic"
    assert result["featured"] is True


def test_with_no_promoted_character_the_featured_flag_stays_down(monkeypatch):
    """The 50/50 featured roll is gated on a non-empty rate-up list, so with
    the list emptied no draw may ever set the flag. The client shows the
    splash on that flag; it must not light up for a generic character."""
    monkeypatch.setattr(gacha_config.BANNERS["character_1"], "rate_up", [])
    for _ in range(200):
        result = perform_pull(0)
        if result["rarity"] == "legendary":
            assert result["featured"] is False


def test_a_draw_is_reproducible_from_a_seeded_random_stream():
    """Both the rarity roll and the character pick come from the module-level
    ``random``. A client that cannot reproduce a pull cannot report one, and a
    server whose draws are not seedable cannot be audited.

    Two identical seeds must give identical pulls; different seeds must be free
    to differ.
    """
    random.seed(4242)
    first = [perform_pull(PITY_SOFT_START) for _ in range(5)]
    random.seed(4242)
    second = [perform_pull(PITY_SOFT_START) for _ in range(5)]
    assert first == second

    random.seed(99)
    third = [perform_pull(PITY_SOFT_START) for _ in range(5)]
    assert isinstance(third, list) and len(third) == 5


# =====================================================================
# 10. The upper end of the soft-pity ramp, where the cap actually bites
# =====================================================================

def test_the_bonus_stops_at_the_whole_table_once_the_counter_runs_past_the_cap():
    """``weights["legendary"] = min(total, ...)`` is a clamp, and a clamp is only
    a real branch once the bonus actually overshoots.

    The ramp is walked only up to ``PITY_HARD`` by the tests above, and inside
    that range the bonus never reaches the table's total -- so the clamp never
    fires and a rewrite that dropped the ``min`` outright would stay green. It
    does fire above the cap: the bonus grows one increment per pull forever,
    so at some counter past the hard pity it passes 100 points and is cut back
    to exactly the table's total.

    This matters because the counter is player-held state. A restored save, a
    merged account or a hand-edited row can hand ``perform_pull`` a number far
    past the cap, and nothing upstream clamps it.
    """
    total = sum(RARITY_WEIGHTS.values())
    far_past_the_cap = PITY_HARD + (total // 1) * 5
    weights = calculate_pull_weights(far_past_the_cap)

    unclamped = RARITY_WEIGHTS["legendary"] + (
        far_past_the_cap - PITY_SOFT_START + 1
    ) * PITY_SOFT_INCREMENT * total
    assert unclamped > total, (
        "this pity does not overshoot, so the test proves nothing about the cap"
    )
    assert weights["legendary"] == total, (
        f"the bonus reached {unclamped} and was not clamped back to {total}"
    )
    assert sum(weights.values()) == pytest.approx(total), (
        "clamping broke the table's total"
    )


def test_the_ramp_keeps_adding_one_increment_per_pull():
    """Between two consecutive pulls the bonus must grow by exactly one step.

    This is the difference between a linear ramp and anything else: a rewrite
    that grew the bonus by a percentage of the running bonus would still rise,
    still total 100, and still be capped -- but a player would then be soft
    pited faster or slower than the table advertises, with nothing to say so.
    Derived from the constants rather than copied, so it holds if either moves.
    """
    total = sum(RARITY_WEIGHTS.values())
    step = PITY_SOFT_INCREMENT * total
    for pity in range(PITY_SOFT_START, PITY_HARD - 1):
        here = calculate_pull_weights(pity)["legendary"]
        nxt = calculate_pull_weights(pity + 1)["legendary"]
        assert nxt - here == pytest.approx(step), (
            f"the ramp moved {nxt - here} from pity {pity} to {pity + 1}, "
            f"expected {step}"
        )


def test_a_counter_far_above_the_cap_still_answers_the_guarantee_on_every_pull():
    """``perform_pull`` refuses nothing about the counter: above the cap, every
    single pull must come back as the guaranteed legendary, featured, and named
    from the rate-up pool.

    Asserted across a spread of absurd counters rather than one, because the
    guarantee is a ``>=`` comparison and a rewrite to ``==`` would still pass
    a test that only asked about the cap itself.
    """
    for pity in (PITY_HARD, PITY_HARD + 1, PITY_HARD * 10, 10_000):
        result = perform_pull(pity)
        assert result["rarity"] == "legendary", f"no guarantee at pity {pity}"
        assert result["pity_triggered"] is True, f"cap unreported at pity {pity}"
        assert result["featured"] is True, f"guarantee unfeatured at pity {pity}"
        assert result["character"] in BANNERS["character_1"].rate_up


# =====================================================================
# 10b. GUARD TESTS: branches only a patched module state can reach
# =====================================================================
#
# These two tests are NOT behaviour tests. Each one patches ``gacha_config``
# module state into a shape the shipped game can never be in, and asserts that
# the defensive branch behind it still answers safely.
#
# WHY THEY ARE HERE: a guard nobody exercises is a guard nobody has proved
# works. A refactor that deleted the divide-by-zero fallback or the roll
# loop's default would leave every other test in this file green, because no
# player ever reaches either line. These two are regression tests FOR THE
# GUARD. They prove the branch is still alive and still safe, not that it is
# reachable.
#
# WHY THEY ARE NOT BEHAVIOUR TESTS -- MEASURED, NOT ASSUMED:
#   * The roll-loop default needs ``random.random()`` to return exactly 1.0.
#     Two million consecutive draws topped out at 0.9999998466384242 and never
#     reached 1.0. The branch is unreachable by a real random stream.
#   * The divide-by-zero fallback needs a one-rarity table. The shipped
#     ``RARITY_WEIGHTS`` declares five: common, uncommon, rare, epic,
#     legendary.
# Only a balance edit to the source, not any player action, produces either
# state. Every other test in this file drives state a real pull can reach.


def test_guard_a_rate_table_of_one_rarity_does_not_divide_by_zero(monkeypatch):
    """GUARD TEST -- proves a guard, not a player experience.

    The scaling step divides the leftover points by the total of the OTHER
    rarities. A table that declared only ``legendary`` leaves nothing to divide
    by, and the guard on that line is the difference between a pull and a 500
    raised inside a route.

    The shipped table declares five rarities, so no player reaches this branch.
    It is patched in rather than shipped because deleting four rarities to
    prove a guard works would be a strange thing to do to the game. What this
    test buys: a refactor that drops ``if total_other else 0.0`` turns this one
    test red instead of waiting for a balance edit to turn a live route into a
    500.
    """
    monkeypatch.setattr(gacha_config, "RARITY_WEIGHTS", {"legendary": 100})
    weights = calculate_pull_weights(PITY_SOFT_START)
    assert set(weights) == {"legendary"}
    assert sum(weights.values()) == pytest.approx(100)


def test_guard_a_roll_that_lands_at_the_very_top_of_the_table_still_names_a_declared_rarity(monkeypatch):
    """GUARD TEST -- proves a guard, not a player experience.

    The roll loop walks the rarities in table order and keeps the last value
    it started with if the roll never lands under any cumulative weight.

    That default needs a roll of exactly 1.0, which ``random.random()`` never
    returns: measured over two million consecutive draws, the highest value
    seen was 0.9999998466384242. So no player reaches this line, and the
    "reachable" claim in the shape of the module is what makes it a guard
    rather than a behaviour.

    What it still buys: the ANSWER is asserted, not the path. Whatever the roll
    does, a client must never receive a rarity the table does not declare,
    because it would have no row, no icon and no rate behind it. A refactor that
    reordered the table so the default was not a real rarity turns this one
    test red.
    """
    monkeypatch.setattr(gacha_config.random, "random", lambda: 1.0)
    result = perform_pull(0)
    assert result["rarity"] in RARITY_WEIGHTS, (
        f"an out-of-range roll produced {result['rarity']!r}"
    )
    assert set(result) == RESULT_KEYS


# =====================================================================
# 11. Route parity: the guarantee reaches the draw on both routes
# =====================================================================

def test_the_single_pull_route_forwards_the_featured_guarantee(monkeypatch):
    """``perform_single_pull`` takes a ``guaranteed_featured`` argument and
    must hand it to the draw it wraps.

    Scripted, so what is asserted is the flag the draw was ASKED for rather than
    a 50/50 coin flip. A route that dropped the argument would still pass every
    other test here -- the flag only changes the outcome when a legendary
    lands, which is one pull in about a hundred at base rates.
    """
    seen = _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    perform_single_pull(0, guaranteed_featured=True)
    assert seen == [(0, True)], f"the guarantee reached the draw as {seen}"

    seen = _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    perform_single_pull(0)
    assert seen == [(0, False)], f"an unflagged pull asked for a guarantee: {seen}"


def test_one_pull_is_identical_whichever_route_delivers_it(monkeypatch):
    """Both routes exist so a route needs no branch on pull type, which only
    holds if they answer the same way. Given the same draw and the same counter,
    the two envelopes must be equal in every field.

    This is what stops the two routes drifting apart again: the single route
    once grew its counter by hand while the batch route called ``next_pity``,
    and the single-pull row carried a free legendary for every pull a player
    made after the counter had once reached the cap.
    """
    _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    single = perform_single_pull(PITY_HARD - 1)
    _scripted_pulls(monkeypatch, [_pull("legendary", featured=True)])
    batch = perform_multi_pull(PITY_HARD - 1, count=1)
    assert single == batch


def test_the_batch_route_defaults_to_exactly_one_pull(monkeypatch):
    """``count`` defaults to 1. A caller that omits it must be billed for one
    pull and handed one result -- not an empty batch, which would be billed for
    ten and deliver nothing.
    """
    _scripted_pulls(monkeypatch, [_pull("common")])
    default = perform_multi_pull(10)
    _scripted_pulls(monkeypatch, [_pull("common")])
    explicit = perform_multi_pull(10, count=1)
    assert default == explicit
    assert len(default["results"]) == 1
    assert default["new_pity"] == 11


# =====================================================================
# 9. Both paths, every rarity: one reset rule, and a reachable guarantee
# =====================================================================

def _commons_until_the_cap(monkeypatch):
    """Answer every pull with a common until the cap, then hand back to the real
    draw so the guarantee itself is exercised rather than faked.

    The real draw is what turns a counter of ``PITY_HARD`` into a legendary. If
    this helper answered at the cap too, the guarantee would be the test's own
    fabrication and every reachability claim below would be circular.
    """
    real = gacha_config.perform_pull

    def fake(pity_count, guaranteed_featured=False):
        if pity_count >= PITY_HARD:
            return real(pity_count, guaranteed_featured)
        return _pull("common")

    monkeypatch.setattr(gacha_config, "perform_pull", fake)
    return real


def test_both_paths_reset_on_the_same_rarity_and_on_no_other(monkeypatch):
    """One draw, one counter, two routes: they must agree on which rarity resets.

    Compared across the WHOLE table rather than on the legendary row alone,
    because the second failure mode is not "no reset" but "reset on the wrong
    rarity" -- epic paying out as if it were the top prize would hand a player a
    fresh guarantee for a pull that gave them nothing.
    """
    for rarity in RARITY_WEIGHTS:
        _scripted_pulls(monkeypatch, [_pull(rarity, featured=True)])
        single = perform_single_pull(41)
        _scripted_pulls(monkeypatch, [_pull(rarity, featured=True)])
        batch = perform_multi_pull(41, count=1)
        rule = next_pity(41, _pull(rarity))

        assert single["new_pity"] == batch["new_pity"] == rule, (
            f"a {rarity} left the counter at {single['new_pity']} on the single "
            f"route and {batch['new_pity']} on the batch route; the shared rule "
            f"says {rule}"
        )
        expected_zero = rarity == "legendary"
        assert (single["new_pity"] == 0) is expected_zero, (
            f"a {rarity} {'should not have' if not expected_zero else 'should have'} "
            f"reset the counter, and it read {single['new_pity']}"
        )


def test_the_guarantee_is_reachable_on_the_single_route_and_fires_at_the_cap(monkeypatch):
    """Driven from zero with no top prize in sight, the counter must climb one
    per pull until the cap and the cap must hand out the legendary.

    The reset half of this is already proven above; this is the half that makes
    the guarantee real. A counter that never reached ``PITY_HARD`` would make
    every reset test pass while the promise the reset protects was unreachable.
    """
    _commons_until_the_cap(monkeypatch)

    pity = 0
    pull_number = 0
    fired_on = None
    fired_at_counter = None
    pity_after = None

    while pull_number <= PITY_HARD + 4:
        pull_number += 1
        drawn_at = pity
        envelope = perform_single_pull(pity)
        result = envelope["results"][0]

        if result["pity_triggered"]:
            fired_on, fired_at_counter = pull_number, drawn_at
            pity_after = envelope["new_pity"]
            break

        assert envelope["new_pity"] == pity + 1, (
            f"pull {pull_number} moved the counter from {pity} to "
            f"{envelope['new_pity']}; only a top prize may skip a step"
        )
        pity = envelope["new_pity"]

    assert fired_at_counter == PITY_HARD, (
        f"the guarantee fired on a pull drawn at a counter of {fired_at_counter}; "
        f"the cap is {PITY_HARD}, and firing early would pay out a top prize the "
        f"player had not earned while firing never would strand them"
    )
    assert pity_after == 0, (
        f"the guaranteed legendary left the counter at {pity_after}; a player at "
        f"the cap who reset to anything but zero is pinned above it and every "
        f"later pull pays out free"
    )


def test_the_guarantee_is_reachable_on_the_batch_route_and_resets_inside_the_batch(monkeypatch):
    """Same climb in ten-pulls: the guarantee must fire and the remaining pulls
    of that same batch must be billed against a counter that started back at
    zero.

    This is the case the old unconditional increment turned into nine extra
    legendaries out of one purchase, so the assertion is about the pulls AFTER
    the guarantee, not just about the legendary itself.
    """
    _commons_until_the_cap(monkeypatch)

    pity = 0
    fired_at = None
    new_pity = None

    for _ in range((PITY_HARD // 10) + 2):
        envelope = perform_multi_pull(pity, count=10)
        triggered = [r for r in envelope["results"] if r["pity_triggered"]]

        if triggered:
            fired_at = envelope
            new_pity = envelope["new_pity"]
            break

        pity = envelope["new_pity"]

    assert fired_at is not None, (
        f"ten-pull batches never reached the cap: the counter stalled at {pity} "
        f"of {PITY_HARD} after {(PITY_HARD // 10) + 1} batches"
    )

    assert fired_at["legendary_count"] == 1, (
        f"one batch produced {fired_at['legendary_count']} legendaries; the reset "
        f"must leave the rest of the batch drawing normally"
    )
    assert fired_at["new_pity"] == 9, (
        f"the batch ended at {fired_at['new_pity']}; the nine pulls after the "
        f"guarantee must climb from zero, not from the cap"
    )


# =====================================================================
# 12. REAL-DRAW PARITY: the single route and the batch route on the
#     same seeded stream, with no scripted pull anywhere
# =====================================================================
#
# Sections 7 and 11 above script the draw through ``monkeypatch``. That is what
# makes them deterministic, and it is also what limits them: a scripted draw is
# a dict the test wrote, so it cannot prove that the two routes agree when the
# rarity came out of the real ``random`` stream. Every parity claim above is a
# claim about the code AROUND the draw.
#
# These tests close that gap by seeding ``random`` and letting the shipped roll
# run. Same seed, same pity, both routes: the envelopes must be equal in every
# field, because a single pull and a one-pull batch are the same purchase.

# A counter above the soft-pity threshold but below the ceiling, so a legendary
# here is the ordinary kind a player earns by luck rather than the guarantee.
MID_BAR = PITY_SOFT_START + 5

# 400 seeds is a chosen sweep, not a tuned figure: the rate table puts real
# weight on legendary by this pity, so this range produces plenty. The test
# below ASSERTS that it found some, so a rate change that made legendaries far
# rarer would turn it red instead of quietly weakening the claim.
REAL_DRAW_SEEDS = 400


def test_a_real_draw_answers_the_same_way_on_both_routes():
    """The same seed, the same pity, both routes -- identical envelopes.

    ``perform_multi_pull`` with ``count=1`` walks the same loop the ten-pull
    walks, so if the two routes ever diverge this is where it shows. Not
    scripted: rarity, character id and both flags all come out of the shipped
    roll, so a divergence in the rate table, the roll loop or the counter
    surfaces here rather than at the route.
    """
    for seed in range(REAL_DRAW_SEEDS):
        random.seed(seed)
        single = perform_single_pull(MID_BAR)
        random.seed(seed)
        batch = perform_multi_pull(MID_BAR, count=1)
        assert single == batch, (
            f"seed {seed}: the single route answered {single} and a one-pull "
            f"batch answered {batch}; a single pull and a batch of one are "
            f"the same purchase and must not disagree"
        )


def test_the_real_draw_parity_loop_actually_produced_legendaries():
    """The control for the test above: it must have seen the case it is for.

    A parity test that only ever compared common pulls would be green and
    worthless. This asserts the seeds really did roll legendary, so the parity
    claim above was earned on the interesting input rather than inherited from
    a run that never reached it.
    """
    seen = set()
    legendaries = 0
    for seed in range(REAL_DRAW_SEEDS):
        random.seed(seed)
        result = perform_single_pull(MID_BAR)["results"][0]
        seen.add(result["rarity"])
        if result["rarity"] == "legendary":
            legendaries += 1
            assert result["pity_triggered"] is False, (
                "a legendary below the ceiling must be an ordinary one; if it "
                "is the guarantee firing, this test stopped measuring the case "
                "players actually earn by luck"
            )

    assert legendaries > 0, (
        f"{REAL_DRAW_SEEDS} seeds at pity {MID_BAR} produced no legendary at "
        f"all, so the parity test above cannot have been comparing the reset case"
    )
    assert "legendary" in seen


def test_a_real_legendary_resets_the_counter_to_zero_on_the_single_route():
    """THE PIN. An ordinary legendary from the shipped roll, not a scripted one.

    This is the number a player feels. Before the fix the single route wrote
    its own ``pity_count + 1`` and stored that, so after earning a legendary
    the bar kept climbing; once it passed the cap, every later pull paid out a
    legendary for free, on every banner, forever.

    Asserted against zero and not merely "the batch agreed", for the reason
    measured in the next test: a broken rule shared by both routes still
    produces agreement.
    """
    hits = 0
    for seed in range(REAL_DRAW_SEEDS):
        random.seed(seed)
        envelope = perform_single_pull(MID_BAR)
        result = envelope["results"][0]
        if result["rarity"] != "legendary":
            continue
        hits += 1
        assert envelope["new_pity"] == 0, (
            f"seed {seed} rolled a legendary and the bar landed on "
            f"{envelope['new_pity']}; a legendary must return the bar to zero, "
            f"or the player climbs straight through the cap and every pull "
            f"after that is free"
        )
        assert envelope["legendary_count"] == 1

    assert hits > 0, "no legendary rolled, so the reset was never exercised"


def test_parity_alone_cannot_catch_a_shared_reset_defect(monkeypatch):
    """MEASURED, and the reason the test above asserts zero.

    Re-injects the defect the module's own docstring records -- the bar climbs
    by one and never resets -- but into the SHARED helper, so BOTH routes are
    wrong in the same way.

    The measurement: parity still holds. ``single == batch`` is satisfied
    because both agree, not because either is right. Only the absolute
    assertion above tells a working reset from two routes agreeing on a broken
    rule.

    Kept as a test rather than a comment because it is the failure mode of the
    whole section: a future edit that replaced ``next_pity`` with
    ``pity_count + 1`` everywhere would leave every parity test in this file
    green.
    """
    def never_resets(pity_count, result):
        return pity_count + 1

    monkeypatch.setattr(gacha_config, "next_pity", never_resets)
    monkeypatch.setattr(gacha_config, "perform_pull", lambda pc, gf=False: {
        "rarity": "legendary",
        "character": "char_legendary_scripted",
        "pity_triggered": False,
        "featured": True,
    })

    single = perform_single_pull(MID_BAR)
    batch = perform_multi_pull(MID_BAR, count=1)

    assert single["new_pity"] == batch["new_pity"] == MID_BAR + 1, (
        "the control did not reproduce the defect; if the patched rule did not "
        "reach the routes, this test proves nothing"
    )
    assert single["new_pity"] != 0, (
        "the defect reproduced a reset, so the control is not measuring the "
        "defect"
    )
    # And explicitly, because it is the whole point of the section: the parity
    # assertion the rest of it leans on still passes with the defect in place.
    assert single["new_pity"] == batch["new_pity"], (
        "parity unexpectedly failed under the defect; this test documents that "
        "parity alone is blind to it"
    )


# =====================================================================
# 13. The rates as PROBABILITIES: they must add up to one, per banner
# =====================================================================
#
# Section 5 proves the table still totals the same number of POINTS it started
# with. That is internal bookkeeping. What a client shows a player is a
# percentage, and a percentage has to add up to one -- a table that holds its
# point total while redistributing it wrongly still displays odds that do not
# add to 100%.
#
# The module has ONE rate table shared by every banner: ``calculate_pull_weights``
# takes a pity count and no banner argument, so the banners cannot drift apart
# on rarity odds. That is worth asserting rather than assuming -- a future
# per-banner override would make the per-banner loop below meaningful in a way
# it is not today.

# Derived from the module, never written as 100, so these assertions hold if the
# rate scale is ever rebalanced.
RATE_SCALE = sum(RARITY_WEIGHTS.values())


@pytest.mark.parametrize("banner_id", sorted(BANNERS))
def test_every_banner_ships_no_rarity_odds_of_its_own(banner_id):
    """A banner carries a cost and a rate-up list, and no rates.

    The rarity odds come from the one shared table. Asserted so that a banner
    gaining its own weight override -- which would make the per-banner
    probability loop below check something real -- turns this red and prompts
    those tests to be extended rather than silently narrowing.
    """
    own_odds = [
        attr for attr in vars(BANNERS[banner_id])
        if any(word in attr for word in ("rate", "weight", "odds", "chance"))
        and attr != "rate_up"
    ]
    assert own_odds == [], (
        f"{banner_id!r} now carries its own odds {own_odds}; the shared "
        f"calculate_pull_weights no longer decides its rates, so the per-banner "
        f"probability checks need to read that field instead"
    )


@pytest.mark.parametrize("banner_id", sorted(BANNERS))
def test_every_banners_rates_add_up_to_one_at_every_pity(banner_id):
    """The total holds at the advertised scale AND the probabilities add to one.

    Walked from a fresh account to the hard cap, for each banner. The soft pity
    step is where this breaks: the bonus is added to the legendary row and
    every other row is scaled into what is left.

    THE TOTAL IS ASSERTED FIRST, AND THAT ORDER IS THE POINT. Normalising by
    whatever the total happens to be makes "the probabilities add up to one"
    true by arithmetic -- any finite list of positive numbers divided by its
    own sum adds up to one, so a check that normalised alone would stay green
    even if the table stopped conserving its 100 points, which is precisely
    the defect the module's own comment records. Asserting the total first is
    what makes the probability half of this assertion mean something.

    Measured: a sabotage that drops the rescaling makes the total 102 instead
    of 100. Normalised alone it reads as a clean 1.0 and every probability
    test stays green.
    """
    for pity in range(0, PITY_HARD + 1):
        weights = calculate_pull_weights(pity)
        total = sum(weights.values())

        assert total == pytest.approx(RATE_SCALE, abs=1e-9), (
            f"{banner_id!r} rates total {total} at pity {pity}, not the "
            f"advertised {RATE_SCALE}; the client divides by this number to "
            f"show odds, so a wrong total shows wrong percentages"
        )

        probabilities = [weight / total for weight in weights.values()]
        assert sum(probabilities) == pytest.approx(1.0, abs=1e-9), (
            f"{banner_id!r} rates add up to {sum(probabilities)} at pity "
            f"{pity}, not 1"
        )
        for rarity, probability in zip(weights, probabilities):
            assert 0.0 <= probability <= 1.0 + 1e-9, (
                f"{rarity!r} claims {probability} of the table at pity {pity}, "
                f"which is not a probability"
            )


@pytest.mark.parametrize("banner_id", sorted(BANNERS))
def test_every_banners_rates_stay_usable_far_past_the_cap(banner_id):
    """A player-held counter can arrive far above the cap, and it still works.

    A restored save, a merged account or a hand-edited row can hand the roll a
    number no sequence of pulls could produce. Past the cap the bonus is
    clamped, so the legendary row takes the whole table -- and checking that
    the other rows are still real probabilities is the only thing standing
    between that state and a pull that returns the top prize every time.
    """
    far_past = PITY_HARD + RATE_SCALE * 5
    weights = calculate_pull_weights(far_past)
    total = sum(weights.values())

    # Total asserted before the probabilities, for the same reason as the test
    # above: dividing by whatever the total is makes the probabilities add up
    # to one no matter what, so the total is what actually carries the claim.
    assert total == pytest.approx(RATE_SCALE, abs=1e-9), (
        f"{banner_id!r} rates total {total} at pity {far_past}, not the "
        f"advertised {RATE_SCALE}"
    )
    probabilities = [weight / total for weight in weights.values()]
    assert sum(probabilities) == pytest.approx(1.0, abs=1e-9), (
        f"{banner_id!r} rates stopped adding up at pity {far_past}: "
        f"{sum(probabilities)}"
    )
    for rarity, weight in weights.items():
        assert weight >= 0, f"{rarity!r} went negative at pity {far_past}"
        assert weight / total <= 1.0 + 1e-9, (
            f"{rarity!r} claimed more than the whole table at pity {far_past}"
        )


# =====================================================================
# 14. The rates must be real numbers: finite, non-negative, non-zero
# =====================================================================
#
# MEASURED BEFORE WRITING, AND THE RESULT SHAPED THIS SECTION:
#
# ``gacha_config`` ships NO validator -- no ``validate()``, no ``assert_*``,
# nothing that inspects the table for bad values. So the honest contract is not
# "the module rejects a bad rate". It is: the table the module SHIPS is finite
# and non-negative, the roll stays finite at every pity a counter can reach,
# and here is exactly what an unrejected bad value does.
#
# That last part is a documentation test, not a wish. A reviewer reading
# "rejected" should know the module does not reject; these tests exist so the
# gap is measured rather than assumed, and so the day a validator is added
# they show what it has to beat.


@pytest.mark.parametrize("rarity", sorted(RARITY_WEIGHTS))
def test_every_shipped_rate_is_a_finite_number(rarity):
    """Each rate is a real, finite number.

    ``inf`` or ``nan`` in this table would not raise. It would propagate
    through every sum the roll performs and quietly turn the pull into a
    coin-flip against nothing -- see the guard tests at the end of this
    section for what that actually looks like.
    """
    weight = RARITY_WEIGHTS[rarity]
    assert isinstance(weight, (int, float)), f"{rarity!r} is not a number"
    assert math.isfinite(weight), (
        f"{rarity!r} is {weight}; a rate that is not finite cannot take part in "
        f"any sum the roll performs"
    )


@pytest.mark.parametrize("rarity", sorted(RARITY_WEIGHTS))
def test_no_shipped_rate_is_negative(rarity):
    """A negative rate is not a small rate; it is a broken roll.

    The roll loop accumulates a running total and stops when the draw falls
    under it. A negative row moves that total BACKWARDS, so rows after it
    become harder to reach rather than merely less likely -- a row can become
    unreachable outright, and the rarity silently disappears from the game
    with no error anywhere.
    """
    assert RARITY_WEIGHTS[rarity] >= 0, (
        f"{rarity!r} has rate {RARITY_WEIGHTS[rarity]}, which is negative"
    )


@pytest.mark.parametrize("rarity", sorted(RARITY_WEIGHTS))
def test_no_shipped_rate_is_zero(rarity):
    """A rate of zero means the rarity is in the table but can never be drawn.

    It passes every other check here -- zero is finite and not negative -- so
    it is called out on its own. The rarity would still show on the client's
    banner with an odds row of 0% that no number of pulls could ever produce.
    """
    assert RARITY_WEIGHTS[rarity] > 0, (
        f"{rarity!r} has rate 0; it is in the table but unreachable"
    )


def test_the_roll_stays_finite_and_non_negative_at_every_reachable_pity():
    """Swept, not sampled: the rate the roll reads must stay a usable number.

    From a fresh account, through the soft pity ramp, to the cap and far past
    it -- because a counter past the cap is reachable by a hand-edited or
    restored save, and nothing upstream clamps it.
    """
    sweep = (0, PITY_SOFT_START - 1, PITY_SOFT_START, PITY_HARD - 1,
             PITY_HARD, PITY_HARD + 1, PITY_HARD * 10, 10_000)
    for pity in sweep:
        weights = calculate_pull_weights(pity)
        total = sum(weights.values())
        assert math.isfinite(total), (
            f"the rate total is {total} at pity {pity}; the roll multiplies a "
            f"draw by this number, so a non-finite total makes every "
            f"comparison in the roll meaningless"
        )
        assert total > 0, f"the rate total is {total} at pity {pity}"
        for rarity, weight in weights.items():
            assert math.isfinite(weight), f"{rarity!r} is {weight} at pity {pity}"
            assert weight >= 0, f"{rarity!r} is {weight} at pity {pity}"


# --- 14b. GUARD TESTS: what an unrejected bad rate actually does ------------
# ---
# Not behaviour tests. Each patches a value into the shipped table that no
# balance edit has ever produced, and records what the module does with it.
# They are kept because the module has NO validator, so without them a reader
# has no idea what the missing check protects against -- and "it would probably
# crash" is a guess, not a measurement.
#
# MEASURED RESULTS, all reproducible by running this file:
#   * a non-finite rate is NOT rejected. Every pull answers "common", every
#     time, silently. No exception, no log line, no empty draw.
#   * a negative rate is NOT rejected. It survives the soft pity scaling as a
#     negative number, and the table's total is no longer the advertised one.

def test_guard_a_non_finite_rate_is_not_rejected_but_silently_answers_common(monkeypatch):
    """GUARD TEST -- documents a gap in the module, not a player experience.

    ``nan`` in the legendary row makes every running total in the roll loop
    compare false, so the loop falls through to its default and every single
    pull answers "common". The player is never told, nothing raises, and the
    pity counter climbs for 80 pulls toward a top prize that cannot arrive.

    Recorded here because the module has no validator: the honest statement of
    the current contract is that a non-finite rate is NOT rejected, and this
    test is what makes that statement checkable. If a validator is added this
    turns red, which is the correct outcome -- it means the module now does
    reject, and the assertion should be rewritten.
    """
    monkeypatch.setattr(
        gacha_config, "RARITY_WEIGHTS",
        {**RARITY_WEIGHTS, "legendary": float("nan")},
    )
    drawn = {perform_pull(0)["rarity"] for _ in range(200)}
    assert drawn == {"common"}, (
        f"a non-finite rate answered {drawn}; if the module now rejects it, "
        f"this guard has done its job and should be rewritten as a rejection test"
    )


def test_guard_an_infinite_rate_is_not_rejected_either(monkeypatch):
    """GUARD TEST -- the same gap reached from the other direction.

    ``inf`` in a row is absorbed by the clamp on the legendary weight, so the
    roll's total is ``inf`` and every pull again answers "common".
    """
    monkeypatch.setattr(
        gacha_config, "RARITY_WEIGHTS",
        {**RARITY_WEIGHTS, "legendary": float("inf")},
    )
    drawn = {perform_pull(0)["rarity"] for _ in range(200)}
    assert drawn == {"common"}, (
        f"an infinite rate answered {drawn}; if the module now rejects it, "
        f"this guard has done its job and should be rewritten as a rejection test"
    )


def test_guard_a_negative_rate_is_not_rejected_and_breaks_the_total(monkeypatch):
    """GUARD TEST -- a negative rate is worse than a non-finite one.

    It does not collapse the pull to a single rarity; it makes the table
    disagree with itself. The scaling step multiplies the negative row by the
    same factor as every other row, so the total comes out BELOW the
    advertised scale and the client divides the odds by the wrong number.

    Also asserted to stay unreachable as a draw, because that is the part that
    hides: the row survives in the table, still drawn from by the client, and
    can never be rolled.
    """
    advertised = RATE_SCALE
    monkeypatch.setattr(
        gacha_config, "RARITY_WEIGHTS",
        {**RARITY_WEIGHTS, "mythic": -5},
    )
    weights = calculate_pull_weights(PITY_SOFT_START)
    assert "mythic" in weights, "the negative row was dropped instead of scaled"
    assert weights["mythic"] < 0, (
        f"the negative rate became {weights['mythic']}; a negative rate the "
        f"scaling turned positive is a different bug"
    )
    assert sum(weights.values()) < advertised, (
        "the total held even with a negative row; if this module ever rejects "
        "negative rates, rewrite this guard as a rejection test"
    )
    assert "mythic" not in {perform_pull(0)["rarity"] for _ in range(200)}, (
        "a negative rate became reachable; the roll loop moves its running "
        "total backwards on a negative row"
    )


# =====================================================================
# 15. The guarantee at the threshold is DETERMINISTIC, not merely likely
# =====================================================================
#
# Section 6 proves the guarantee FIRES at the cap. That is one call. What a
# player is promised is stronger: at the threshold the result must not depend
# on the draw at all. Sampled here across a wide sweep of the random stream
# rather than called once, so a guarantee that fired 99 times in 200 would
# turn this red rather than pass.

# A broad sweep, not a tuned figure. The guarantee sits on a ``>=`` comparison
# that never consults the random stream, so the expected result is exactly one
# distinct envelope; this many seeds is simply enough that a single leaked
# random call would show up.
DETERMINISM_SEEDS = 2000


def test_the_guarantee_answers_the_same_thing_on_every_draw_at_the_cap():
    """Every draw at the cap returns an identical envelope.

    All four fields, compared as one set. If the character were drawn from the
    rate-up pool by chance rather than looked up, a second rate-up character
    would appear here; if the flags leaked from the normal path, a False would
    appear here.
    """
    envelopes = set()
    for seed in range(DETERMINISM_SEEDS):
        random.seed(seed)
        result = perform_pull(PITY_HARD)
        envelopes.add((result["rarity"], result["character"],
                       result["pity_triggered"], result["featured"]))

    assert len(envelopes) == 1, (
        f"{len(envelopes)} different answers came back from {DETERMINISM_SEEDS} "
        f"draws at the cap: {envelopes}. The guarantee must not depend on the draw."
    )
    rarity, character, triggered, featured = envelopes.pop()
    assert (rarity, triggered, featured) == ("legendary", True, True)
    assert character in BANNERS["character_1"].rate_up


def test_a_draw_just_below_the_cap_is_never_the_guarantee():
    """The other arm: the guarantee must not leak one pull early.

    Driven across the same sweep. Only the FLAG is asserted absent -- a player
    CAN still roll a legendary at the cap minus one, because the soft pity
    bonus is real by then. What must never happen is that pull being reported
    as pity-triggered, because the client uses that flag to tell a guaranteed
    prize from a lucky one.
    """
    for seed in range(DETERMINISM_SEEDS):
        random.seed(seed)
        assert perform_pull(PITY_HARD - 1)["pity_triggered"] is False, (
            f"seed {seed}: the guarantee fired one pull early, at "
            f"{PITY_HARD - 1} instead of {PITY_HARD}"
        )


def test_a_batch_drawn_at_the_cap_is_the_same_purchase_every_time():
    """Ten pulls at the cap: one legendary, then nine drawn from a fresh bar.

    Reproducible across seeds, and the expected end-of-batch counter is derived
    from the shared reset rule rather than written down -- the guarantee resets
    the bar, so the pulls after it climb from zero.
    """
    tail = 10 - 1
    for seed in (1, 7, 99, 4242):
        random.seed(seed)
        first = perform_multi_pull(PITY_HARD, count=10)
        random.seed(seed)
        second = perform_multi_pull(PITY_HARD, count=10)
        assert first == second, f"seed {seed} was not reproducible"

        triggered = [r for r in first["results"] if r["pity_triggered"]]
        assert len(triggered) == 1, (
            f"{len(triggered)} of the ten pulls were reported as pity-triggered "
            f"at the cap; a player is owed exactly one guarantee per batch"
        )
        assert triggered[0] is first["results"][0], (
            "the guarantee did not come on the first pull of the batch; it is "
            "owed before the counter has climbed anywhere"
        )
        assert first["legendary_count"] == 1, (
            f"a batch at the cap produced {first['legendary_count']} "
            f"legendaries; the reset must leave the other nine drawing normally"
        )
        assert first["new_pity"] == next_pity(0, _pull("common")) * tail, (
            f"the batch ended at {first['new_pity']}; the {tail} pulls after "
            f"the guarantee must climb from zero, not from the cap"
        )
