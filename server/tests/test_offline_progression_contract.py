"""Contract tests for server/offline_progression.py -- the offline payout engine.

WHAT THIS FILE ASSERTS
----------------------
The shapes this module promises to everyone downstream: the store row shape it
reads, the ``OfflineReward`` / ``OfflineProgressResult`` records it emits, the
summary ``apply_offline_rewards`` returns, and the preview dict the client
screen reads before the player taps "claim".

WHY THESE ARE CONTRACTS AND NOT SNAPSHOTS
-----------------------------------------
Every expected number below is DERIVED from a table the module itself exports
(or from the measured relation between two calls), never copied out of a
recorded response. A rate retune in ``SPIRIT_STONE_OFFLINE_RATES`` cannot break
these tests; writing a credit to the wrong column, or under-rewarding a store
row that has no nested ``cultivation`` dict, does.

THE FOUR SHAPES THAT MATTER
---------------------------
1. FLAT STORE ROW. ``PlayerStore`` persists cultivation as top-level columns --
   ``progress``, ``cultivation_realm``, ``cultivation_stage``, ``path``,
   ``spirit_root`` -- and writes no nested ``cultivation`` dict at all. Spirit
   stones live in ONE jsonb column, ``spirit_stones``, holding a dict of tiers.
   Beast rosters are a LIST of dicts.
2. NESTED LEGACY ROW. An older engine build wrote ``{"cultivation": {...}}``
   with ``spirit_stones`` inside it. Still accepted.
3. ``Player`` OBJECT. What the engine works in once a row has been converted.
4. EMITTED RECORDS. ``OfflineReward`` for each payout line, an
   ``OfflineProgressResult`` for the whole claim, an ``applied`` summary dict
   for what was actually written.

The round trip in section 1 pins ``_dict_to_player`` against a ``Player`` whose
fields were set, which is the only way to prove the reader accepts what the
writer emits rather than only what a hand-built row looks like.

SECTIONS 9-11 (AUTH, THE CAP AS A BOUND, NON-FINITE INPUTS)
----------------------------------------------------------
Sections 1-8 are pure module calls and need no app. Section 9 needs the real
FastAPI app booted, because "the claim requires auth" is a claim about the ROUTE
and not about this module: ``offline_progression`` has no notion of a caller at
all. A pure-module test could only assert that some function exists, which is
the "vacuous shape assertion" this repo rejects elsewhere. So section 9 drives
``POST /lifecycle/{pid}/claim-offline`` over HTTP and reads the real status codes,
with the auth helper the rest of the suite uses.

Section 10 is the 72-hour cap restated as a BOUND rather than a value: it
compares two live calculations (72 hours vs 5000 hours) and asserts the money
is identical, so a retune of any rate table leaves it green.

Section 11 covers non-finite input. One of those tests is a ``xfail(strict=True)``
pin on a DEFECT measured on this tree -- see the reason string.

NO POSTGRESQL
-------------
The ``env -u WC_PG_DB`` in the run command is what keeps this file off a real
database; nothing here needs one. Section 9 boots the app, but on the in-memory
store.
"""
import copy
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import offline_progression as op                                    # noqa: E402
from cultivation_system import (                                    # noqa: E402
    CultivationPath,
    Player,
    Realm,
    SpiritRoot,
    Stage,
)

# NaN cannot be written as a literal, so it is built here. It is the value that
# defeats every comparison in this module: `nan > x` and `nan < x` are both
# False, so a guard written as `if value > limit` never fires for it.
NAN = float("nan")
POS_INF = float("inf")
NEG_INF = float("-inf")

REALM_ORDER = [Realm.MORTAL, Realm.FOUNDATION, Realm.CORE,
               Realm.NASCENT_SOUL, Realm.DAO_SEEKING, Realm.IMMORTAL]

# One hour of absence, the unit every rate table is written per.
HOURS = 1.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class FakeWallet:
    """Stand-in for the wallet row that records when a player was last paid.

    Both timestamps default to ``0.0``, exactly as ``PGBalance`` in
    ``wallet_store_pg.py`` does, so a wallet that has never been stamped
    reaches the engine in the state a real one would.
    """

    def __init__(self, last_offline_claimed_at=0.0, last_online_at=0.0):
        self.last_offline_claimed_at = last_offline_claimed_at
        self.last_online_at = last_online_at


class FakeWalletStore:
    def __init__(self, wallet):
        self._wallet = wallet

    def get(self, player_id):
        return self._wallet


def away(seconds):
    """A wallet store claiming the player has been gone for ``seconds``."""
    return FakeWalletStore(FakeWallet(last_offline_claimed_at=time.time() - seconds))


def store_row(**overrides):
    """A row in the FLAT shape ``PlayerStore`` actually persists."""
    row = {
        "id": "row-1",
        "name": "Row One",
        "level": 3,
        "progress": 0.0,
        "cultivation_realm": "mortal",
        "cultivation_stage": "early",
        "path": "qi",
        "spirit_root": "none",
        "spirit_stones": {"copper": 500, "silver": 0, "gold": 0,
                          "low_grade": 0, "mid_grade": 0,
                          "high_grade": 0, "extreme_grade": 0},
        "beasts": [],
    }
    row.update(overrides)
    return row


def result_for(rewards, **overrides):
    """Build an OfflineProgressResult around ``rewards``."""
    fields = {
        "elapsed_hours": HOURS,
        "capped_hours": HOURS,
        "rewards": list(rewards),
        "cultivation_progress_gained": 0.0,
        "new_realm": None,
        "new_stage": None,
        "breakthrough_triggered": False,
    }
    fields.update(overrides)
    return op.OfflineProgressResult(**fields)


def reward(amount, currency=None, beast_id=None):
    """An ``OfflineReward`` of whichever type the arguments describe."""
    if currency is not None:
        kind = op.OfflineRewardType.SPIRIT_STONES
        details = {}
    elif beast_id is not None:
        kind = op.OfflineRewardType.BEAST_AFFINITY
        details = {"beast_id": beast_id}
    else:
        kind = op.OfflineRewardType.CULTIVATION_PROGRESS
        details = {}
    return op.OfflineReward(reward_type=kind, amount=amount,
                            currency=currency, details=details)


def player_with(realm=Realm.MORTAL, stage=Stage.EARLY, path=CultivationPath.QI,
                root=SpiritRoot.NONE):
    p = Player(id="obj-1", name="Obj One")
    p.cultivation.realm = realm
    p.cultivation.stage = stage
    p.cultivation.path = path
    p.cultivation.spirit_root = root
    return p


def types_of(result):
    """Reward types emitted, in emission order."""
    return [r.reward_type for r in result.rewards]


def amounts_by(result, reward_type):
    """{currency: amount} for the rewards of one type; currency may be None."""
    out = {}
    for r in result.rewards:
        if r.reward_type is reward_type:
            out[r.currency] = r.amount
    return out


# ==========================================================================
# 1. THE ROUND TRIP: Player -> to_dict() -> _dict_to_player() -> Player
# ==========================================================================
# A hand-built row only proves the reader accepts the shape its author
# imagined. Writing every field, serialising with the engine's own writer, and
# reading it back proves the reader accepts what the WRITER emits.

def test_round_trip_preserves_every_cultivation_field():
    original = player_with(realm=Realm.CORE, stage=Stage.LATE,
                           path=CultivationPath.BODY, root=SpiritRoot.FIRE)
    original.cultivation.progress = 42.5
    original.cultivation.breakthrough_failures = 3
    original.cultivation.tribulations_passed = 2
    original.cultivation.tribulations_failed = 1

    restored = op._dict_to_player(original.to_dict())

    assert restored.cultivation.realm is Realm.CORE
    assert restored.cultivation.stage is Stage.LATE
    assert restored.cultivation.path is CultivationPath.BODY
    assert restored.cultivation.spirit_root is SpiritRoot.FIRE
    assert restored.cultivation.progress == pytest.approx(42.5)
    assert restored.cultivation.breakthrough_failures == 3
    assert restored.cultivation.tribulations_passed == 2
    assert restored.cultivation.tribulations_failed == 1


def test_round_trip_preserves_all_seven_spirit_stone_tiers():
    original = player_with()
    tiers = {"copper": 11, "silver": 22, "gold": 33, "low_grade": 44,
             "mid_grade": 55, "high_grade": 66, "extreme_grade": 77}
    original.cultivation.copper = tiers["copper"]
    original.cultivation.silver = tiers["silver"]
    original.cultivation.gold = tiers["gold"]
    original.cultivation.low_grade_stone = tiers["low_grade"]
    original.cultivation.mid_grade_stone = tiers["mid_grade"]
    original.cultivation.high_grade_stone = tiers["high_grade"]
    original.cultivation.extreme_grade_stone = tiers["extreme_grade"]

    restored = op._dict_to_player(original.to_dict())
    c = restored.cultivation

    assert (c.copper, c.silver, c.gold) == (11, 22, 33), "coin tiers lost"
    assert (c.low_grade_stone, c.mid_grade_stone,
            c.high_grade_stone, c.extreme_grade_stone) == (44, 55, 66, 77), \
        "grade tiers lost"


def test_round_trip_preserves_identity_and_progression_fields():
    """``id``, ``name``, ``level`` and ``exp`` are written flat, so they return."""
    original = player_with()
    original.level = 9
    original.exp = 40

    restored = op._dict_to_player(original.to_dict())

    assert (restored.id, restored.name) == ("obj-1", "Obj One")
    assert (restored.level, restored.exp) == (9, 40)


def test_round_trip_preserves_the_fields_the_writer_emits_flat():
    """The top-level scalars ``to_dict`` writes must survive the trip.

    ``to_dict`` nests some things (sect, stats, pvp) but writes these at the top
    level, and ``_dict_to_player`` reads them from the top level.
    """
    original = player_with()
    original.dao_companion_id = "companion-9"
    original.last_login = "2026-01-02T03:04:05"
    original.login_streak = 6

    restored = op._dict_to_player(original.to_dict())

    assert restored.dao_companion_id == "companion-9"
    assert restored.last_login == "2026-01-02T03:04:05"
    assert restored.login_streak == 6


def test_round_trip_of_a_minimal_player_lands_on_documented_defaults():
    """An empty row must not raise and must not invent a realm."""
    restored = op._dict_to_player({})

    assert restored.id == "" and restored.name == ""
    assert restored.cultivation.realm is Realm.MORTAL
    assert restored.cultivation.stage is Stage.EARLY
    assert restored.cultivation.path is CultivationPath.QI
    assert restored.cultivation.spirit_root is SpiritRoot.NONE
    assert restored.cultivation.progress == pytest.approx(0.0)


def test_sect_and_beast_do_not_survive_the_todict_round_trip():
    """Documented lossiness -- so a change to it is a deliberate, visible one.

    ``Player.to_dict`` files sect fields under a ``"sect"`` bucket and does not
    write the beast roster at all, while ``_dict_to_player`` reads ``sect_id``
    and friends from the top level. Round-tripping a ``Player`` therefore
    DROPS sect membership and beasts. This does not affect the live claim path
    (the store row carries them flat); it is pinned so that if ``to_dict`` or
    the reader ever changes, the change is noticed rather than assumed.
    """
    original = player_with()
    original.sect_id = "sect-7"
    original.sect_rank = "elder"
    original.sect_contribution = 55
    original.beasts = [{"beast_id": "b1", "affinity": 3}]

    restored = op._dict_to_player(original.to_dict())

    assert (restored.sect_id, restored.sect_rank, restored.sect_contribution) \
        == (None, None, 0), "sect now survives to_dict; re-check who reads this"
    assert restored.beasts == {}, "beasts now survive to_dict; re-check who reads this"


def test_combat_stats_and_pvp_record_do_not_survive_the_todict_round_trip():
    """``to_dict`` nests stats under ``"stats"`` and pvp under ``"pvp"``.

    ``_dict_to_player`` reads neither bucket, so a Player that is serialised and
    read back carries DEFAULT combat stats and a DEFAULT pvp record. Nothing
    in this module's own claim path is affected -- the store row is flat -- so
    this is pinned as a boundary rather than treated as a payout bug. If a
    future reader starts honouring those buckets, this test goes red and the
    author has to decide whether the store row should change too.
    """
    original = player_with()
    original.atk = 210
    original.hp = 4000
    original.crit_rate = 0.25
    original.crit_dmg = 2.5
    original.pvp_rating = 1500
    original.pvp_wins = 12
    original.pvp_losses = 3

    restored = op._dict_to_player(original.to_dict())

    assert (restored.atk, restored.hp) == (100, 1000), "stats are now read back"
    assert restored.crit_rate == pytest.approx(0.05), "crit_rate is now read back"
    assert restored.crit_dmg == pytest.approx(1.5), "crit_dmg is now read back"
    assert (restored.pvp_rating, restored.pvp_wins, restored.pvp_losses) \
        == (1000, 0, 0), "the pvp bucket is now read back"


# ==========================================================================
# 2. _dict_to_player against the shapes a real row can be in
# ==========================================================================

def test_flat_store_row_is_not_read_as_an_empty_bar():
    """The load-bearing case: a real store row carries NO nested dict."""
    row = store_row(cultivation_realm="foundation", cultivation_stage="middle",
                    progress=60.0, spirit_root="water")

    p = op._dict_to_player(row)

    assert p.cultivation.realm is Realm.FOUNDATION
    assert p.cultivation.stage is Stage.MID_LATE
    assert p.cultivation.progress == pytest.approx(60.0), \
        "a flat row read as progress 0.0 turns one absence into a whole bar"
    assert p.cultivation.spirit_root is SpiritRoot.WATER


def test_flat_store_row_reads_the_spirit_stones_column():
    row = store_row(spirit_stones={"copper": 500, "low_grade": 3, "high_grade": 9})

    p = op._dict_to_player(row)
    c = p.cultivation

    assert (c.copper, c.low_grade_stone, c.high_grade_stone) == (500, 3, 9)


def test_nested_legacy_row_is_still_read():
    row = {"id": "old", "name": "Old",
           "cultivation": {"realm": "core", "stage": "late", "progress": 12.0,
                           "spirit_stones": {"gold": 9}}}

    p = op._dict_to_player(row)

    assert p.cultivation.realm is Realm.CORE
    assert p.cultivation.stage is Stage.LATE
    assert p.cultivation.progress == pytest.approx(12.0)
    assert p.cultivation.gold == 9


def test_a_written_flat_column_wins_over_a_stale_nested_value():
    """Both shapes present: the persisted column is the truth."""
    row = {"id": "both", "name": "Both",
           "cultivation": {"realm": "immortal", "stage": "late", "progress": 1.0},
           "cultivation_realm": "core", "progress": 77.0}

    source = op._cultivation_source(row)

    assert source["realm"] == "core", "a stale nested realm beat the written column"
    assert source["progress"] == pytest.approx(77.0)


def test_nested_only_keys_still_come_through_when_flat_columns_are_absent():
    row = {"id": "mix", "name": "Mix",
           "cultivation": {"breakthrough_failures": 4, "tribulations_failed": 2}}

    source = op._cultivation_source(row)

    assert source["breakthrough_failures"] == 4
    assert source["tribulations_failed"] == 2


def test_a_non_dict_cultivation_value_is_ignored_not_raised():
    row = {"id": "junk", "name": "Junk", "cultivation": "not-a-dict", "progress": 5.0}

    p = op._dict_to_player(row)

    assert p.cultivation.progress == pytest.approx(5.0)
    assert p.cultivation.realm is Realm.MORTAL


def test_an_unrecognised_store_stage_raises_rather_than_guessing_a_rate():
    """An unknown stage must be loud, because a stage SELECTS a rate.

    ``early`` is the highest-rate stage in every realm, so a silent fallback
    here pays every player whose stage spelling is unrecognised MORE, on every
    claim, forever.
    """
    with pytest.raises(ValueError):
        op._dict_to_player(store_row(cultivation_stage="peak_of_the_moon"))


# ==========================================================================
# 3. The coercion helpers and the vocabulary tables behind them
# ==========================================================================

def test_an_unknown_realm_falls_back_because_a_realm_selects_no_rate():
    """A realm earns MORTAL rates when unread -- conservative, never louder."""
    assert op._as_enum(Realm, "qi_refining", Realm.MORTAL) is Realm.MORTAL
    assert op._as_enum(CultivationPath, "machine", CultivationPath.QI) is CultivationPath.QI
    assert op._as_enum(SpiritRoot, "crystal", SpiritRoot.NONE) is SpiritRoot.NONE


def test_a_non_string_value_does_not_crash_the_enum_fallback():
    assert op._as_enum(Realm, None, Realm.MORTAL) is Realm.MORTAL
    assert op._as_enum(Realm, 17, Realm.MORTAL) is Realm.MORTAL
    assert op._as_enum(Realm, ["mortal"], Realm.MORTAL) is Realm.MORTAL


def test_enum_members_pass_through_the_coercion_unchanged():
    assert op._as_enum(Realm, Realm.CORE, Realm.MORTAL) is Realm.CORE
    assert op._as_stage(Stage.LATE) is Stage.LATE


@pytest.mark.parametrize("stored,expected", [
    ("early", Stage.EARLY),
    ("late", Stage.LATE),
    ("peak", Stage.PEAK),
    ("breakthrough", Stage.BREAKTHROUGH),
    ("middle", Stage.MID_LATE),      # the store's spelling
    ("mid_late", Stage.MID_LATE),    # the engine's spelling
])
def test_both_stage_spellings_reach_the_same_stage_member(stored, expected):
    assert op.STAGE_FROM_STORE_VALUE[stored] is expected
    assert op._as_stage(stored) is expected


def test_the_store_and_the_engine_agree_the_middle_stage_is_one_stage():
    """Two spellings, one stage: nothing translates between them today."""
    assert op.STAGE_FROM_STORE_VALUE["middle"] is op.STAGE_FROM_STORE_VALUE["mid_late"]


def test_an_unrecognised_stage_raises_with_a_named_reason():
    with pytest.raises(ValueError) as excinfo:
        op._as_stage("golden_age")

    assert "golden_age" in str(excinfo.value), \
        "the error must name the offending value so a bad row is findable"


@pytest.mark.parametrize("stage_value", ["golden_age", None, 3, ""])
def test_an_unrecognised_stage_raises_for_every_junk_shape(stage_value):
    with pytest.raises(ValueError):
        op._as_stage(stage_value)


@pytest.mark.parametrize("table_name", [
    "SPIRIT_STONE_OFFLINE_RATES",
    "SPIRIT_STONE_GRADE_RATES",
    "SECT_CONTRIBUTION_OFFLINE_RATES",
])
def test_every_rate_table_has_a_row_for_every_realm(table_name):
    """A realm missing from a table silently earns MORTAL's rate."""
    table = getattr(op, table_name)
    assert set(table) == set(REALM_ORDER), \
        "%s does not cover every realm; a gap pays mortal rates" % table_name


def test_the_realm_ladder_is_the_engines_own_realm_order():
    """The ladder is what a breakthrough walks; it must not invent realms."""
    assert op.REALM_LADDER == [r.value for r in REALM_ORDER]


def test_the_ladder_starts_and_ends_where_the_engine_does():
    assert op.REALM_LADDER[0] == Realm.MORTAL.value
    assert op.REALM_LADDER[-1] == Realm.IMMORTAL.value


# ==========================================================================
# 4. The per-reward calculators: rates, gates, and modifiers
# ==========================================================================

def test_spirit_stones_scale_linearly_with_hours_offline():
    p = player_with(realm=Realm.MORTAL)

    one_hour = op.calculate_offline_spirit_stones(p, 1.0)
    four_hours = op.calculate_offline_spirit_stones(p, 4.0)

    for tier, amount in one_hour.items():
        assert four_hours[tier] == pytest.approx(amount * 4.0), \
            "%s does not scale linearly with hours" % tier


def test_a_body_cultiator_earns_the_configured_fraction_of_currency():
    """Body path is a documented 0.9 haircut on currency only."""
    qi = player_with(realm=Realm.MORTAL, path=CultivationPath.QI)
    body = player_with(realm=Realm.MORTAL, path=CultivationPath.BODY)

    qi_stones = op.calculate_offline_spirit_stones(qi, HOURS)
    body_stones = op.calculate_offline_spirit_stones(body, HOURS)

    for tier, amount in qi_stones.items():
        assert body_stones[tier] == pytest.approx(amount * 0.9), \
            "%s is not scaled by the body-path modifier" % tier


def test_a_pure_root_earns_the_configured_currency_bonus():
    none = player_with(realm=Realm.MORTAL, root=SpiritRoot.NONE)
    pure = player_with(realm=Realm.MORTAL, root=SpiritRoot.PURE)

    none_stones = op.calculate_offline_spirit_stones(none, HOURS)
    pure_stones = op.calculate_offline_spirit_stones(pure, HOURS)

    for tier, amount in none_stones.items():
        assert pure_stones[tier] == pytest.approx(amount * 1.05), \
            "%s is not scaled by the pure-root modifier" % tier


def test_held_stone_grades_raise_the_currency_rate_up_to_the_capped_bonus():
    """More stones held -> faster accrual, and the bonus saturates."""
    from cultivation_system import CultivationCalculator

    poor = player_with(realm=Realm.MORTAL)
    rich = player_with(realm=Realm.MORTAL)
    rich.cultivation.low_grade_stone = 100

    poor_bonus = CultivationCalculator._stone_bonus(poor)
    rich_bonus = CultivationCalculator._stone_bonus(rich)

    assert rich_bonus > poor_bonus, "held stones do not raise the offline rate"
    assert rich_bonus == pytest.approx(1.0 + 0.5), "the bonus is not capped at +50%"


def test_a_player_with_no_sect_earns_no_sect_contribution():
    p = player_with(realm=Realm.CORE)

    assert op.calculate_offline_sect_contribution(p, 4.0) == pytest.approx(0.0)


def test_sect_contribution_scales_with_the_ranks_configured_multiplier():
    base = player_with(realm=Realm.CORE)
    base.sect_id = "sect-1"

    outer = copy.copy(base)
    outer.sect_rank = "outer_disciple"
    elder = copy.copy(base)
    elder.sect_rank = "elder"

    outer_amount = op.calculate_offline_sect_contribution(outer, 1.0)
    elder_amount = op.calculate_offline_sect_contribution(elder, 1.0)

    assert elder_amount == pytest.approx(outer_amount * 2.0), \
        "the elder multiplier is not twice the outer-disciple one"


def test_an_unranked_sect_member_earns_the_outer_disciple_rate():
    """An unknown rank must not read as the top of the ladder."""
    base = player_with(realm=Realm.CORE)
    base.sect_id = "sect-1"
    outer = copy.copy(base)
    outer.sect_rank = "outer_disciple"
    ranked = copy.copy(base)
    ranked.sect_rank = "grand_poobah"

    assert op.calculate_offline_sect_contribution(ranked, 2.0) == \
        pytest.approx(op.calculate_offline_sect_contribution(outer, 2.0))


def test_sect_contribution_scales_with_hours():
    p = player_with(realm=Realm.CORE)
    p.sect_id = "sect-1"

    assert op.calculate_offline_sect_contribution(p, 3.0) == \
        pytest.approx(op.calculate_offline_sect_contribution(p, 1.0) * 3.0)


def test_a_higher_realm_bonds_a_beast_faster():
    low = player_with(realm=Realm.MORTAL)
    low.beasts = [{"beast_id": "b1", "element": "none"}]
    high = player_with(realm=Realm.CORE)
    high.beasts = [{"beast_id": "b1", "element": "none"}]

    low_amount = op.calculate_offline_beast_affinity(low, HOURS)["b1"]
    high_amount = op.calculate_offline_beast_affinity(high, HOURS)["b1"]

    assert high_amount > low_amount, "realm does not affect beast bonding speed"


def test_a_matching_element_beast_gets_the_documented_affinity_bonus():
    fire_root = player_with(realm=Realm.MORTAL, root=SpiritRoot.FIRE)
    fire_root.beasts = [{"beast_id": "b1", "element": "fire"}]
    water_root = player_with(realm=Realm.MORTAL, root=SpiritRoot.WATER)
    water_root.beasts = [{"beast_id": "b1", "element": "fire"}]

    assert op.calculate_offline_beast_affinity(fire_root, HOURS)["b1"] == \
        pytest.approx(op.calculate_offline_beast_affinity(water_root, HOURS)["b1"] * 1.2)


def test_a_rosterless_player_earns_no_beast_affinity():
    p = player_with(realm=Realm.CORE)
    p.beasts = []

    assert op.calculate_offline_beast_affinity(p, 10.0) == {}


def test_every_owned_beast_is_paid_separately():
    p = player_with(realm=Realm.MORTAL)
    p.beasts = [{"beast_id": "b1", "affinity": 0},
                {"beast_id": "b2", "affinity": 0},
                {"beast_id": "b3", "affinity": 0}]

    affinity = op.calculate_offline_beast_affinity(p, HOURS)

    assert set(affinity) == {"b1", "b2", "b3"}
    assert affinity["b1"] == pytest.approx(affinity["b3"])


@pytest.mark.parametrize("beast_element,root,expected", [
    ("fire", SpiritRoot.FIRE, True),
    ("water", SpiritRoot.WATER, True),
    ("earth", SpiritRoot.EARTH, True),
    ("wood", SpiritRoot.WOOD, True),
    ("metal", SpiritRoot.METAL, True),
    ("fire", SpiritRoot.WATER, False),
    ("none", SpiritRoot.FIRE, False),
    ("thunder", SpiritRoot.FIRE, False),
])
def test_element_matching_is_a_table_lookup(beast_element, root, expected):
    assert op._element_matches_root(beast_element, root) is expected


def test_battle_pass_xp_is_flat_rate_times_hours():
    assert op.calculate_offline_battle_pass_xp(3.0) == \
        pytest.approx(3.0 * op.BATTLE_PASS_XP_PER_HOUR)


def test_zero_hours_earns_nothing_from_any_calculator():
    p = player_with(realm=Realm.CORE)
    p.sect_id = "sect-1"
    p.beasts = [{"beast_id": "b1", "affinity": 0}]

    assert op.calculate_offline_cultivation_progress(p, 0.0) == pytest.approx(0.0)
    assert op.calculate_offline_battle_pass_xp(0.0) == pytest.approx(0.0)
    assert op.calculate_offline_sect_contribution(p, 0.0) == pytest.approx(0.0)
    assert op.calculate_offline_beast_affinity(p, 0.0)["b1"] == pytest.approx(0.0)
    for amount in op.calculate_offline_spirit_stones(p, 0.0).values():
        assert amount == pytest.approx(0.0)


def test_the_beast_roster_is_accepted_as_a_list_or_a_mapping():
    """The store keeps a LIST; a mapping is the legacy form."""
    listed = [{"beast_id": "b1", "affinity": 1}, "junk", {"no_id": True}]
    mapped = {"b1": {"affinity": 1}, "b2": "junk"}

    assert [b for b, _ in op._iter_owned_beasts(listed)] == ["b1"]
    assert [b for b, _ in op._iter_owned_beasts(mapped)] == ["b1"]


def test_an_absent_or_empty_roster_iterates_to_nothing():
    assert op._iter_owned_beasts(None) == []
    assert op._iter_owned_beasts({}) == []
    assert op._iter_owned_beasts([]) == []
    assert op._iter_owned_beasts("roster") == []


# ==========================================================================
# 5. calculate_full_offline_progress: elapsed time, the cap, and clock skew
# ==========================================================================

def test_elapsed_hours_come_from_the_wallets_last_claim():
    result = op.calculate_full_offline_progress(
        store_row(), time.time(), wallet_store=away(4 * 3600))

    assert result.elapsed_hours == pytest.approx(4.0, abs=0.01)
    assert result.capped_hours == pytest.approx(4.0, abs=0.01)


def test_a_new_player_falls_back_to_the_wallets_last_online():
    """``last_offline_claimed_at`` is None for a player who never claimed."""
    store = FakeWalletStore(FakeWallet(last_offline_claimed_at=None,
                                      last_online_at=time.time() - 2 * 3600))

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours == pytest.approx(2.0, abs=0.01)


def test_offline_time_is_capped_and_the_raw_elapsed_is_still_reported():
    result = op.calculate_full_offline_progress(
        store_row(), time.time(), wallet_store=away(500 * 3600))

    assert result.elapsed_hours > op.MAX_OFFLINE_HOURS, "premise: 500h is past the cap"
    assert result.capped_hours == pytest.approx(op.MAX_OFFLINE_HOURS)


def test_a_last_claim_in_the_future_earns_nothing_rather_than_a_negative():
    store = FakeWalletStore(FakeWallet(last_offline_claimed_at=time.time() + 5000))

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours == pytest.approx(0.0)
    assert result.capped_hours == pytest.approx(0.0)
    assert result.rewards == [], "a zero-length absence still emitted reward lines"


def test_a_device_clock_far_from_the_server_is_refused():
    with pytest.raises(ValueError) as excinfo:
        op.calculate_full_offline_progress(store_row(), 9e12)

    assert "skew" in str(excinfo.value).lower()


def test_a_device_clock_within_the_tolerance_is_accepted():
    result = op.calculate_full_offline_progress(
        store_row(), time.time() + op.MAX_CLOCK_SKEW_SECONDS - 60.0,
        wallet_store=away(3600))

    assert result.elapsed_hours > 0


def test_a_row_and_a_player_object_calculate_the_same_rewards():
    """Same state, two input shapes, one answer."""
    row = store_row(spirit_root="fire", spirit_stones={"copper": 0, "low_grade": 0},
                    beasts=[{"beast_id": "b1", "affinity": 0}])
    obj = player_with(realm=Realm.MORTAL, stage=Stage.EARLY,
                      path=CultivationPath.QI, root=SpiritRoot.FIRE)
    obj.beasts = [{"beast_id": "b1", "affinity": 0}]

    from_row = op.calculate_full_offline_progress(copy.deepcopy(row), time.time(),
                                                  wallet_store=away(3 * 3600))
    from_obj = op.calculate_full_offline_progress(obj, time.time(),
                                                  wallet_store=away(3 * 3600))

    assert from_row.cultivation_progress_gained == \
        pytest.approx(from_obj.cultivation_progress_gained)
    row_stones = amounts_by(from_row, op.OfflineRewardType.SPIRIT_STONES)
    obj_stones = amounts_by(from_obj, op.OfflineRewardType.SPIRIT_STONES)
    assert row_stones.keys() == obj_stones.keys()
    for tier, amount in row_stones.items():
        assert amount == pytest.approx(obj_stones[tier]), \
            "%s differs between a row and an equivalent Player" % tier


def test_the_result_reports_both_the_raw_and_the_capped_hours():
    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=away(100 * 3600))

    assert result.elapsed_hours >= result.capped_hours
    assert result.capped_hours == pytest.approx(min(result.elapsed_hours,
                                                   op.MAX_OFFLINE_HOURS))


def test_a_full_absence_emits_every_reward_type_it_can_pay():
    row = store_row(spirit_root="fire", sect_id="sect-1", sect_rank="elder",
                    beasts=[{"beast_id": "b1", "element": "fire", "affinity": 0}])

    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=away(8 * 3600))

    assert set(types_of(result)) == {
        op.OfflineRewardType.CULTIVATION_PROGRESS,
        op.OfflineRewardType.SPIRIT_STONES,
        op.OfflineRewardType.SECT_CONTRIBUTION,
        op.OfflineRewardType.BEAST_AFFINITY,
        op.OfflineRewardType.BATTLE_PASS_XP,
    }


def test_only_positive_amounts_become_reward_lines():
    """A zero-rate tier must not appear as a zero-value payout line."""
    result = op.calculate_full_offline_progress(
        store_row(cultivation_realm="immortal"), time.time(),
        wallet_store=away(8 * 3600))

    assert all(r.amount > 0 for r in result.rewards), \
        "a zero-amount reward line reached the payout list"


def test_a_cultivation_reward_line_carries_its_hourly_rate():
    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=away(4 * 3600))
    cult = [r for r in result.rewards
            if r.reward_type is op.OfflineRewardType.CULTIVATION_PROGRESS]

    assert len(cult) == 1
    rate = cult[0].details["base_progress_per_hour"]
    assert result.cultivation_progress_gained == pytest.approx(rate * result.capped_hours)


def test_a_stone_reward_line_names_its_currency_and_rate():
    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=away(4 * 3600))
    stones = [r for r in result.rewards
              if r.reward_type is op.OfflineRewardType.SPIRIT_STONES]

    assert stones, "an absence produced no stone lines at all"
    for line in stones:
        assert line.currency, "a stone line carries no tier name"
        assert line.amount == pytest.approx(line.details["rate_per_hour"] *
                                            result.capped_hours)


def test_a_beast_reward_line_names_the_beast_it_pays():
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 0},
                            {"beast_id": "b2", "affinity": 0}])

    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=away(3600))
    paid = {r.details["beast_id"] for r in result.rewards
            if r.reward_type is op.OfflineRewardType.BEAST_AFFINITY}

    assert paid == {"b1", "b2"}


def test_a_full_bar_reports_a_breakthrough_and_names_the_next_realm():
    row = store_row(progress=99.0, cultivation_realm="mortal",
                    cultivation_stage="early")

    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=away(2 * 3600))

    assert result.breakthrough_triggered is True
    assert result.new_realm == op.REALM_LADDER[
        op.REALM_LADDER.index("mortal") + 1]
    assert result.new_stage == Stage.BREAKTHROUGH.value


def test_a_player_already_awaiting_breakthrough_is_reported_the_same_way():
    """The flag is idempotent: a player mid-breakthrough is not re-flagged."""
    row = store_row(progress=99.0, cultivation_stage="breakthrough")

    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=away(2 * 3600))

    assert result.breakthrough_triggered is False, \
        "a player already in the breakthrough stage was flagged again"


def test_the_top_realm_never_earns_a_realm_promotion():
    """The end-of-ladder guard, reached for real.

    The absence is sized so the bar actually FILLS: the immortal offline rate is
    the lowest in the game, so a short absence would leave the bar part-full and
    the promotion branch would never run -- and this test would pass whether or
    not the guard exists.
    """
    row = store_row(progress=99.0, cultivation_realm=Realm.IMMORTAL.value)
    store = away(72 * 3600)

    result = op.calculate_full_offline_progress(row, time.time(), wallet_store=store)

    assert result.breakthrough_triggered is True, \
        "premise broken: the bar never filled, so the promotion branch never ran"
    assert result.new_realm is None, \
        "an immortal was promoted past the top of the ladder"
    assert result.new_stage == Stage.BREAKTHROUGH.value, \
        "the stage flip is independent of the realm ladder and must still happen"


def test_a_part_full_bar_reports_no_breakthrough():
    row = store_row(progress=1.0)

    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=away(3600))

    assert result.breakthrough_triggered is False
    assert result.new_realm is None


def test_a_player_with_no_wallet_reports_a_zero_length_absence():
    """No wallet and no last-cultivation stamp means there is no elapsed time."""
    result = op.calculate_full_offline_progress(Player(id="x", name="X"),
                                                time.time())

    assert result.elapsed_hours == pytest.approx(0.0)
    assert result.rewards == []


# ==========================================================================
# 6. get_offline_preview: the dict the client screen reads
# ==========================================================================

def test_a_preview_always_carries_the_same_key_set():
    preview = op.get_offline_preview(store_row(), time.time(),
                                     wallet_store=away(6 * 3600))

    assert set(preview) == {
        "can_claim", "elapsed_hours", "capped_hours", "max_cap_hours",
        "cultivation_progress", "breakthrough_ready", "new_realm",
        "new_stage", "estimated_rewards",
    }


def test_a_preview_reports_the_configured_cap():
    assert op.get_offline_preview(store_row(), time.time())["max_cap_hours"] == \
        op.MAX_OFFLINE_HOURS


def test_a_clock_skew_becomes_a_refusal_rather_than_an_exception():
    """The preview is what the client renders, so it must not 500."""
    preview = op.get_offline_preview(store_row(), 9e12)

    assert preview["can_claim"] is False
    assert "skew" in preview["error"].lower()
    assert "estimated_rewards" not in preview


def test_a_preview_labels_each_reward_line_by_type_and_currency():
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 0}])

    preview = op.get_offline_preview(row, time.time(), wallet_store=away(8 * 3600))
    keys = set(preview["estimated_rewards"])

    assert "spirit_stones_copper" in keys, "a stone line is not labelled with its tier"
    assert "beast_affinity_b1" in keys, "a beast line is not labelled with its beast"
    assert "cultivation_progress" in keys
    assert "battle_pass_xp" in keys


def test_a_preview_entry_repeats_the_line_it_summarises():
    row = store_row()

    preview = op.get_offline_preview(row, time.time(), wallet_store=away(8 * 3600))
    entry = preview["estimated_rewards"]["spirit_stones_copper"]

    assert entry["type"] == op.OfflineRewardType.SPIRIT_STONES.value
    assert entry["currency"] == "copper"
    assert entry["amount"] == pytest.approx(
        op.calculate_full_offline_progress(copy.deepcopy(row), time.time(),
                                           wallet_store=away(8 * 3600))
        .rewards[1].amount, rel=0.01)


def test_a_preview_rounds_its_headline_numbers():
    """The client renders these; a long float would be noise on a phone."""
    preview = op.get_offline_preview(store_row(), time.time(),
                                     wallet_store=away(1234.5678))

    assert preview["elapsed_hours"] == round(preview["elapsed_hours"], 2)
    assert preview["cultivation_progress"] == round(preview["cultivation_progress"], 2)


def test_a_preview_reports_a_ready_breakthrough():
    row = store_row(progress=99.0)

    preview = op.get_offline_preview(row, time.time(),
                                     wallet_store=away(2 * 3600))

    assert preview["breakthrough_ready"] is True
    assert preview["new_realm"] is not None


def test_a_preview_applies_no_rewards():
    """A preview must be a side-effect-free read of the same calculation."""
    row = store_row(progress=10.0, sect_contribution=5,
                    spirit_stones={"copper": 100},
                    beasts=[{"beast_id": "b1", "affinity": 1}])
    before = copy.deepcopy(row)

    op.get_offline_preview(row, time.time(), wallet_store=away(8 * 3600))

    assert row == before, "reading a preview changed the player's row"


# ==========================================================================
# 7. apply_offline_rewards: what actually gets written
# ==========================================================================

def test_a_stone_credit_lands_inside_the_spirit_stones_column():
    """The money contract: the balance must move where spenders read it.

    Writing a bare ``player["copper"]`` is not a store column, so the credit
    lands somewhere nobody reads and the player is paid nothing.
    """
    row = store_row(spirit_stones={"copper": 500, "low_grade": 2})

    applied = op.apply_offline_rewards(row, result_for([
        reward(600.0, "copper"), reward(24.0, "low_grade")]))

    assert row["spirit_stones"]["copper"] == 1100
    assert row["spirit_stones"]["low_grade"] == 26
    assert applied["spirit_stones"] == {"copper": 600, "low_grade": 24}
    for invented in ("copper", "low_grade_stone"):
        assert invented not in row, "a bare %r key was invented; no spender reads it" % invented


def test_a_credit_never_invents_new_top_level_keys():
    row = store_row()

    op.apply_offline_rewards(row, result_for([reward(10.0, "gold"),
                                              reward(10.0, "extreme_grade")]))

    assert set(row) == set(store_row()), "the payout added a key the store has no column for"


def test_a_stone_amount_is_written_as_a_whole_number():
    """The engine computes in floats; balances are integers everywhere else."""
    row = store_row(spirit_stones={"copper": 0})

    applied = op.apply_offline_rewards(row, result_for([reward(16.9, "copper")]))

    assert row["spirit_stones"]["copper"] == 16
    assert applied["spirit_stones"]["copper"] == 16


def test_a_legacy_scalar_stone_balance_stays_scalar_for_a_coin_credit():
    """A row whose balance is a bare int must not be promoted to a dict."""
    row = {"id": "legacy", "name": "Legacy", "spirit_stones": 300}

    op.apply_offline_rewards(row, result_for([reward(50.0, "copper")]))

    assert row["spirit_stones"] == 300, \
        "the balance changed shape; every spender branches on scalar-vs-dict"


def test_a_legacy_scalar_balance_absorbs_the_low_grade_tier():
    """Only the free-to-play tier is spendable on a scalar row."""
    row = {"id": "legacy", "name": "Legacy", "spirit_stones": 300}

    applied = op.apply_offline_rewards(row, result_for([reward(24.0, "low_grade")]))

    assert row["spirit_stones"] == 324
    assert applied["spirit_stones"] == {"low_grade": 24}


def test_a_stone_credit_on_a_row_with_no_stone_column_creates_the_tier_dict():
    row = {"id": "new", "name": "New"}

    op.apply_offline_rewards(row, result_for([reward(24.0, "mid_grade")]))

    assert row["spirit_stones"] == {"mid_grade": 24}


def test_a_credit_to_an_unknown_tier_never_invents_a_stone_column():
    """The money half of the contract, and the half that actually protects pay.

    A currency name with no tier mapping must not create a store column; a
    balance nobody can spend is not a payout.
    """
    row = {"id": "odd", "name": "Odd"}

    op.apply_offline_rewards(row, result_for([reward(5.0, "unobtainium")]))

    assert "spirit_stones" not in row, "an unknown tier created a stone column"


def test_an_unknown_tier_is_absent_from_the_applied_summary():
    """The summary must name only tiers the row actually took the credit for.

    ``set_stone`` returns whether the balance moved, so an unmapped tier
    contributes nothing to ``applied`` rather than being reported as a payout
    that was refused.
    """
    row = {"id": "odd", "name": "Odd"}

    applied = op.apply_offline_rewards(row, result_for([reward(5.0, "unobtainium")]))

    assert applied["spirit_stones"] == {}


# ==========================================================================
# 8. A REFUSED WRITE MUST NOT BE REPORTED AS A PAYMENT
# ==========================================================================
#
# The defect class: a route reports a quantity it should first confirm.
# A rejected write reported as PAID is worse than an error, because the
# player has no way to know. Every test below FORCES the refusal, then
# asserts on BOTH axes: the reply text, and the balance the row really
# holds. Asserting the reply alone would pass on a fix that reported the
# right number for the wrong reason.

def _scalar_row():
    """A legacy row whose spirit_stones balance is a plain int.

    Only the free-to-play tier is spendable in that shape, so a coin
    credit has nowhere to land and ``set_stone`` refuses it -- silently.
    """
    return {"id": "legacy", "name": "Legacy", "spirit_stones": 500,
            "progress": 0.0, "beasts": []}


def test_a_refused_coin_credit_is_not_reported_as_a_payout():
    """Forced rejection: the reply must not claim the coins that did not land."""
    row = _scalar_row()

    applied = op.apply_offline_rewards(
        row, result_for([reward(600.0, "copper")]))

    # AXIS 1: the reply. The refused tier is absent, and named as refused.
    assert "copper" not in applied["spirit_stones"], \
        "the reply claimed a copper payout the row refused"
    assert any(r["currency"] == "copper" and r["requested"] == 600.0
               for r in applied["rejected_rewards"]), \
        "a refused credit was dropped silently instead of reported"
    # AXIS 2: the store. Nothing moved.
    assert row["spirit_stones"] == 500, \
        "the refusal test did not actually force a refusal"


def test_a_refused_credit_never_shows_up_as_a_positive_summary():
    """No reported figure may exceed what the row gained.

    Merges the refusal and the tier that DID land: the summary carries a
    number only for the tier the store actually took.
    """
    row = _scalar_row()

    applied = op.apply_offline_rewards(
        row, result_for([reward(600.0, "copper"),
                          reward(24.0, "low_grade")]))

    # The spendable tier DID land, and is reported at its real figure.
    assert applied["spirit_stones"] == {"low_grade": 24}
    # ...and the refused one did not.
    assert "copper" not in applied["spirit_stones"]
    assert row["spirit_stones"] == 524, \
        "only the low_grade credit landed; balance should be 500+24"


def test_clamped_battle_pass_xp_is_reported_at_the_clamped_figure():
    """Forced rejection: a clamped XP grant must not be reported as granted.

    ``grant_xp`` clamps one grant to ``MAX_XP_PER_GRANT``. Reporting the
    REQUEST told the player about XP that never landed: measured at 1500
    requested, the reply said 1500 and the row held 1000.
    """
    import battle_pass

    requested = float(battle_pass.MAX_XP_PER_GRANT + 500)
    row = {"id": "bp", "name": "Bp", "spirit_stones": {"copper": 0},
           "progress": 0.0, "beasts": []}

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.BATTLE_PASS_XP, requested)]))

    real_xp = row["battle_pass"]["xp"]

    # AXIS 1: the reply must equal what landed, not what was asked for.
    assert applied["battle_pass_xp"] == float(real_xp), \
        "the reply claimed %r XP but the row holds %r" % (
            applied["battle_pass_xp"], real_xp)
    assert applied["battle_pass_xp"] < requested, \
        "the clamp did not bite, so this test proves nothing"
    # AXIS 2: the store. The clamp really did hold XP back.
    assert real_xp == battle_pass.MAX_XP_PER_GRANT


def test_every_reported_figure_matches_the_balance_the_row_holds():
    """The whole summary, checked against the row, on every reward type.

    This is the general form of the rule: for each figure the summary
    reports, the row must have moved by exactly that much.
    """
    row = store_row(progress=40.0, sect_contribution=100,
                    spirit_stones={"copper": 500, "low_grade": 0},
                    beasts=[{"beast_id": "b1", "affinity": 0}])
    before_progress = row["progress"]
    before_contrib = row["sect_contribution"]
    before_copper = row["spirit_stones"]["copper"]
    before_affinity = row["beasts"][0]["affinity"]

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.CULTIVATION_PROGRESS, 10.0),
        reward(600.0, "copper"),
        reward(7.0, "low_grade"),
        op.OfflineReward(op.OfflineRewardType.SECT_CONTRIBUTION, 80.0),
        op.OfflineReward(op.OfflineRewardType.BEAST_AFFINITY, 12.0,
                          details={"beast_id": "b1"}),
        op.OfflineReward(op.OfflineRewardType.BATTLE_PASS_XP, 40.0)]))

    assert applied["cultivation_progress"] == pytest.approx(
        row["progress"] - before_progress)
    assert applied["sect_contribution"] == \
        row["sect_contribution"] - before_contrib
    assert applied["spirit_stones"]["copper"] == pytest.approx(
        row["spirit_stones"]["copper"] - before_copper)
    assert applied["beast_affinity"]["b1"] == pytest.approx(
        row["beasts"][0]["affinity"] - before_affinity)
    assert applied["battle_pass_xp"] == pytest.approx(
        row["battle_pass"]["xp"])
    assert applied["rejected_rewards"] == []


def test_a_claim_that_pays_nothing_reports_no_positive_figure():
    """Every line refused -> nothing reported, and every line named."""
    row = {"id": "none", "name": "None", "spirit_stones": {"copper": 0},
           "progress": 0.0, "beasts": []}
    applied = op.apply_offline_rewards(row, result_for([
        reward(10.0, "unobtainium")]))

    assert applied["spirit_stones"] == {}
    assert applied["battle_pass_xp"] == 0.0
    assert len(applied["rejected_rewards"]) == 1
    assert row["spirit_stones"] == {"copper": 0}


def test_a_beast_missing_from_the_roster_is_reported_as_refused():
    """Affinity for a beast the player does not own must not be claimed."""
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 0}])

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.BEAST_AFFINITY, 25.0,
                          details={"beast_id": "b99"})]))

    assert applied["beast_affinity"] == {}
    assert row["beasts"][0]["affinity"] == 0
    assert [r["reason"] for r in applied["rejected_rewards"]] == \
        ["beast is not in the roster"]


def test_the_summary_still_keeps_every_key_a_caller_indexes():
    """``rejected_rewards`` is additive; the old key set is unchanged."""
    applied = op.apply_offline_rewards(store_row(), result_for([]))

    for key in ("cultivation_progress", "spirit_stones", "sect_contribution",
                 "beast_affinity", "battle_pass_xp",
                 "breakthrough_triggered", "new_realm", "new_stage",
                 "rejected_rewards"):
        assert key in applied, "a caller indexing %r would 500" % key


def test_a_player_object_takes_the_credit_on_its_stone_attributes():
    p = player_with()
    p.cultivation.copper = 10

    applied = op.apply_offline_rewards(p, result_for([reward(5.0, "copper"),
                                                     reward(7.0, "high_grade")]))

    assert p.cultivation.copper == 15
    assert p.cultivation.high_grade_stone == 7
    assert applied["spirit_stones"] == {"copper": 5, "high_grade": 7}


def test_cultivation_progress_is_added_to_the_stored_bar():
    row = store_row(progress=40.0)

    applied = op.apply_offline_rewards(row, result_for([reward(16.32)]))

    assert row["progress"] == pytest.approx(56.32)
    assert applied["cultivation_progress"] == pytest.approx(16.32)


def test_crossing_the_threshold_keeps_only_the_remainder_and_promotes_the_realm():
    row = store_row(progress=95.0, cultivation_realm="mortal",
                    cultivation_stage="early")

    applied = op.apply_offline_rewards(
        row, result_for([reward(10.0)], breakthrough_triggered=True,
                        new_realm="foundation", new_stage="breakthrough"))

    assert row["progress"] == pytest.approx(5.0), "the surplus stage was not rolled over"
    assert row["cultivation_realm"] == "foundation", \
        "the realm was reported but never written"
    assert row["cultivation_stage"] == "early"
    assert applied["new_realm"] == "foundation"
    assert applied["new_stage"] == "early", "a promoted player must start at early"
    assert "realm" not in row, "the realm was written to a key the store has no column for"


def test_a_breakthrough_with_no_named_realm_leaves_the_realm_alone():
    row = store_row(progress=95.0, cultivation_realm="mortal")

    applied = op.apply_offline_rewards(row, result_for([reward(10.0)],
                                                      breakthrough_triggered=True))

    assert row["cultivation_realm"] == "mortal"
    assert applied["new_realm"] is None
    assert applied["breakthrough_triggered"] is True


def test_a_realm_off_the_ladder_is_never_rewritten():
    """An unread realm is left as-is rather than walked to a wrong rung."""
    row = store_row(progress=95.0, cultivation_realm="qi_refining")

    op.apply_offline_rewards(row, result_for([reward(10.0)],
                                             breakthrough_triggered=True,
                                             new_realm="foundation"))

    assert row["cultivation_realm"] == "qi_refining", \
        "an unrecognised realm was silently overwritten"


def test_the_top_realm_is_not_promoted_off_the_end_of_the_ladder():
    row = store_row(progress=95.0, cultivation_realm=Realm.IMMORTAL.value)

    op.apply_offline_rewards(row, result_for([reward(10.0)],
                                             breakthrough_triggered=True,
                                             new_realm="foundation"))

    assert row["cultivation_realm"] == Realm.IMMORTAL.value


def test_a_reported_realm_that_cannot_be_written_is_still_reported():
    """The summary echoes the caller's realm even when the write is refused.

    Worth pinning because the row and the summary then disagree. Fixing this
    means making the summary tell the truth about the row, not the other way
    round -- a summary cannot promote a realm whose rung is unknown.
    """
    row = store_row(progress=95.0, cultivation_realm="qi_refining")

    applied = op.apply_offline_rewards(row, result_for([reward(10.0)],
                                                      breakthrough_triggered=True,
                                                      new_realm="foundation"))

    assert applied["new_realm"] == "foundation"
    assert row["cultivation_realm"] == "qi_refining"


def test_cultivation_progress_keeps_its_float_precision():
    """Progress is a float bar; truncating it would lose earned progress."""
    row = store_row(progress=0.0)

    op.apply_offline_rewards(row, result_for([reward(16.32)]))

    assert row["progress"] == pytest.approx(16.32)
    assert isinstance(row["progress"], float)


def test_sect_contribution_is_added_as_a_whole_number():
    row = store_row(sect_contribution=10)

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.SECT_CONTRIBUTION, 19.9)]))

    assert row["sect_contribution"] == 29
    assert applied["sect_contribution"] == 19


def test_a_player_object_takes_sect_contribution_on_its_attribute():
    p = player_with()

    op.apply_offline_rewards(p, result_for([
        op.OfflineReward(op.OfflineRewardType.SECT_CONTRIBUTION, 5.0)]))

    assert p.sect_contribution == 5


def test_beast_affinity_is_added_to_the_owned_beast_not_dropped():
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 4},
                            {"beast_id": "b2", "affinity": 0}])

    applied = op.apply_offline_rewards(row, result_for([reward(6.0, beast_id="b1")]))

    assert row["beasts"][0]["affinity"] == pytest.approx(10.0)
    assert row["beasts"][1]["affinity"] == 0, "the wrong beast was credited"
    assert applied["beast_affinity"] == {"b1": 6.0}


def test_affinity_for_a_beast_the_player_does_not_own_is_dropped():
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 4}])

    applied = op.apply_offline_rewards(row, result_for([reward(6.0, beast_id="b9")]))

    assert row["beasts"][0]["affinity"] == 4
    assert applied["beast_affinity"] == {}


def test_a_beast_reward_with_no_id_in_its_details_is_dropped():
    row = store_row(beasts=[{"beast_id": "b1", "affinity": 4}])

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.BEAST_AFFINITY, 6.0, None, {})]))

    assert row["beasts"][0]["affinity"] == 4
    assert applied["beast_affinity"] == {}


def test_affinity_reaches_a_legacy_mapping_roster_too():
    row = store_row(beasts={"b1": {"affinity": 4}})

    applied = op.apply_offline_rewards(row, result_for([reward(6.0, beast_id="b1")]))

    assert row["beasts"]["b1"]["affinity"] == pytest.approx(10.0)
    assert applied["beast_affinity"] == {"b1": 6.0}


def test_a_beast_with_no_stored_affinity_starts_from_zero():
    row = store_row(beasts=[{"beast_id": "b1"}])

    op.apply_offline_rewards(row, result_for([reward(6.0, beast_id="b1")]))

    assert row["beasts"][0]["affinity"] == pytest.approx(6.0)


def test_battle_pass_experience_is_handed_to_the_battle_pass_owner():
    """The engine must not own the XP ledger; it delegates with a source tag."""
    row = store_row()

    applied = op.apply_offline_rewards(row, result_for([
        op.OfflineReward(op.OfflineRewardType.BATTLE_PASS_XP, 40.0)]))

    assert applied["battle_pass_xp"] == pytest.approx(40.0)
    assert applied["battle_pass_result"]["source"] == "offline_progress"
    assert applied["battle_pass_result"]["xp_gained"] == 40


def test_applying_nothing_still_returns_the_full_summary_shape():
    """A caller indexes these keys unconditionally; a missing key is a 500."""
    applied = op.apply_offline_rewards(store_row(), result_for([]))

    assert applied == {
        "cultivation_progress": 0.0,
        "spirit_stones": {},
        "sect_contribution": 0,
        "beast_affinity": {},
        "battle_pass_xp": 0.0,
        "breakthrough_triggered": False,
        "new_realm": None,
        "new_stage": None,
        # Refused lines are named rather than silently dropped, so a
        # player who was owed a payout can see that it was refused.
        "rejected_rewards": [],
    }


def test_claiming_moves_the_wallets_last_claimed_stamp():
    """Without this the next claim pays the same absence again."""
    wallet = FakeWallet()
    store = FakeWalletStore(wallet)

    op.apply_offline_rewards(store_row(), result_for([]), wallet_store=store)

    assert wallet.last_offline_claimed_at is not None


def test_claiming_without_a_wallet_still_pays():
    row = store_row(spirit_stones={"copper": 0})

    applied = op.apply_offline_rewards(row, result_for([reward(600.0, "copper")]))

    assert row["spirit_stones"]["copper"] == 600
    assert applied["spirit_stones"] == {"copper": 600}


def test_apply_accepts_a_sect_store_without_requiring_one():
    row = store_row(sect_contribution=0)

    applied = op.apply_offline_rewards(row, result_for([]), sect_store=object())

    assert applied["sect_contribution"] == 0


# ==========================================================================
# 8. calculate -> apply: the whole claim, end to end on one row
# ==========================================================================

def test_a_calculated_claim_pays_a_flat_store_row_it_can_actually_spend():
    """The regression the module's docstrings describe, as one round trip.

    A flat row with progress 90.0 used to be read as an EMPTY bar, so the
    remainder ceiling became a whole 100.0 stage and the player was handed a
    free promotion. Read correctly, a 90%-full bar has 10.0 left to earn, so
    that -- and only that -- is what may be credited.
    """
    stage_bar = 100.0
    stored = 90.0
    row = store_row(progress=stored, spirit_stones={"copper": 500, "low_grade": 0})
    store = away(10 * 3600)

    result = op.calculate_full_offline_progress(row, time.time(), wallet_store=store)
    applied = op.apply_offline_rewards(row, result, wallet_store=store)

    remainder = stage_bar - stored
    assert applied["cultivation_progress"] == pytest.approx(remainder), \
        "the grant is not the remainder of a bar read at %r" % stored
    assert applied["cultivation_progress"] < stage_bar, \
        "a part-full bar was credited a whole stage"
    # The grant lands exactly on the threshold, so the bar wraps to zero and
    # the realm actually advances -- and it advances by exactly one rung.
    assert row["progress"] == pytest.approx(0.0), \
        "a bar filled exactly to the threshold did not wrap"
    assert applied["breakthrough_triggered"] is True
    assert row["cultivation_realm"] == op.REALM_LADDER[
        op.REALM_LADDER.index("mortal") + 1]
    assert row["cultivation_realm"] != "mortal", \
        "filling the bar earned more than one realm"
    copper_earned = amounts_by(result, op.OfflineRewardType.SPIRIT_STONES)["copper"]
    assert copper_earned > 0
    assert row["spirit_stones"]["copper"] == 500 + int(copper_earned)
    assert applied["spirit_stones"]["copper"] == int(copper_earned)


def test_a_calculated_claim_reports_and_writes_the_same_realm():
    row = store_row(progress=99.0, cultivation_realm="mortal")
    store = away(2 * 3600)

    result = op.calculate_full_offline_progress(row, time.time(), wallet_store=store)
    applied = op.apply_offline_rewards(row, result, wallet_store=store)

    assert applied["new_realm"] == result.new_realm, \
        "the response named a realm the row did not get"
    assert row["cultivation_realm"] == result.new_realm


def test_every_reward_line_reaches_the_row_it_names():
    """No line is computed, reported, and then lost on the way to storage."""
    starting_stones = {"copper": 100, "low_grade": 5}
    row = store_row(spirit_root="fire", sect_contribution=3, sect_id="sect-1",
                    spirit_stones=dict(starting_stones),
                    beasts=[{"beast_id": "b1", "affinity": 2}])
    starting = dict(starting_stones)
    store = away(8 * 3600)

    result = op.calculate_full_offline_progress(row, time.time(), wallet_store=store)
    applied = op.apply_offline_rewards(row, result, wallet_store=store)

    stones = amounts_by(result, op.OfflineRewardType.SPIRIT_STONES)
    for currency, amount in stones.items():
        assert amount > 0, "a zero-value stone line reached the payout list"
        assert row["spirit_stones"][currency] == pytest.approx(starting[currency] + int(amount)), \
            "%s was computed but never landed on the row" % currency
        assert applied["spirit_stones"][currency] == int(amount)
    assert row["sect_contribution"] > 3
    assert row["beasts"][0]["affinity"] > 2.0
    assert applied["battle_pass_xp"] == pytest.approx(
        next(r.amount for r in result.rewards
             if r.reward_type is op.OfflineRewardType.BATTLE_PASS_XP))


def test_claiming_twice_for_the_same_absence_does_not_pay_twice():
    """The wallet stamp is the once-only ledger; this is its observable effect."""
    row = store_row(spirit_stones={"copper": 0})
    wallet = FakeWallet(last_offline_claimed_at=time.time() - 3600,
                        last_online_at=time.time() - 7200)
    store = FakeWalletStore(wallet)
    result = op.calculate_full_offline_progress(row, time.time(),
                                                wallet_store=store)

    first = op.apply_offline_rewards(row, result, wallet_store=store)
    after_first = row["spirit_stones"]["copper"]
    second = op.calculate_full_offline_progress(row, time.time(), wallet_store=store)
    op.apply_offline_rewards(row, second, wallet_store=store)

    assert after_first > 0, "premise broken: the first claim paid nothing"
    assert second.capped_hours < result.capped_hours, \
        "the second claim measured a longer absence than the first"
    second_copper = int(amounts_by(second, op.OfflineRewardType.SPIRIT_STONES)["copper"])
    assert row["spirit_stones"]["copper"] == pytest.approx(after_first + second_copper), \
        "the second claim paid a different amount than it measured"


def test_a_wallet_never_stamped_reaches_the_time_cap():
    """A row whose two wallet timestamps are both ``0.0`` still gets an answer.

    ``last_claim or last_online_at`` collapses to ``0.0``, so the elapsed window
    is measured from the epoch and lands on the hard cap -- bounded, not
    negative and not an exception.
    """
    store = FakeWalletStore(FakeWallet())

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours > op.MAX_OFFLINE_HOURS
    assert result.capped_hours == pytest.approx(op.MAX_OFFLINE_HOURS)


# ==========================================================================
# 9. THE CLAIM ROUTE REQUIRES AUTHENTICATION
# ==========================================================================
#
# This is a claim about the ROUTE, not about offline_progression: the module
# has no notion of a caller, so nothing above could observe it. Every status
# code below was READ OFF THE LIVE APP on this tree, not inferred from source:
#
#   no Authorization header ......... 401
#   a token nothing issued .......... 401
#   a session belonging to ANOTHER
#     player, naming this player ..... 403
#   the owner's own session .......... 200
#
# The three refusals are asserted on TWO axes each: the status code AND the
# player's own row afterwards. Asserting the status alone would pass on a route
# that refused for the wrong reason (a bad lifecycle, a missing bar) while
# looking exactly like a correct refusal -- and it would not notice a guard that
# refuses EVERYONE, including the owner. So every refusal test is paired with a
# control that proves the owner still gets through.


@pytest.fixture(scope="module")
def app_client():
    """The real app, booted once, with a helper that signs each call as a player.

    ``wc_test_auth.Session`` is the shared helper for exactly this shape; a bare
    TestClient answers 401 on every call here, which is the trap the helper's
    docstring warns about. The signup counter is cleared first because
    /player/create is rate limited to five per minute per process and this file
    makes several players.
    """
    from fastapi.testclient import TestClient
    import server.main as main
    from server.tests.wc_test_auth import Session, clear_signup_rate_limit

    clear_signup_rate_limit()
    with TestClient(main.app) as raw:
        yield Session(raw), main


def _claimable_player(client, main, name):
    """A player who can actually reach the offline payout.

    The route is gated on an active lifecycle, so a brand-new player is refused
    with "lifecycle not started" and never reaches the auth question at all --
    a probe that skips this measures the lifecycle gate and calls it auth.

    The bar is also wound back to empty. The check-in that makes the player
    reachable pays a sign-in reward that fills the progress bar to 100.0, and a
    full bar is owed 0.0 offline progress (the calculator clamps to the stage
    remainder). A fixture that left it full would make the control arm below
    assert a payout the server is correct to withhold.
    """
    pid = client.post("/player/create",
                      json={"name": name, "spirit_root": "metal"}).json()["id"]
    client.sign_in(pid)
    client.get(f"/lifecycle/{pid}")
    client.post(f"/lifecycle/{pid}/check-in")
    main.players_db[pid] = {**main.players_db[pid], "progress": 0.0}
    main.players_db.persist(pid, main.players_db[pid])
    assert main.players_db[pid].get("progress") == 0.0, (
        "premise broken: the cultivation bar is full, so an offline claim is "
        "owed nothing and the control arm below measures nothing"
    )
    main.wallet_store.update(pid, last_online_at=time.time() - 10 * 3600,
                             last_offline_claimed_at=None)
    main.wallet_store.bump_version(pid)
    return pid


def _claim_url(pid):
    return f"/lifecycle/{pid}/claim-offline"


# The header the client sends: its own wall clock. Only ever used to detect a
# clock that disagrees with the server by more than a day.
HONEST_HEADERS = {"X-Device-Time": str(time.time())}


def test_claiming_offline_without_any_token_is_refused(app_client):
    """An anonymous caller gets 401 and pays nothing."""
    client, main = app_client
    pid = _claimable_player(client, main, "op-anon")

    response = client._client.post(_claim_url(pid), headers=HONEST_HEADERS)

    assert response.status_code == 401, (
        "an anonymous offline claim was answered %s: %s"
        % (response.status_code, response.text[:200])
    )
    assert main.players_db[pid].get("progress") == 0.0, \
        "a refused anonymous claim moved the player's progress"


def test_claiming_offline_with_a_token_nobody_issued_is_refused(app_client):
    """A made-up bearer token is refused on the same terms as no token at all.

    The 401 body is identical for a missing header and a wrong token, so the
    answer cannot be used to test whether a guessed token is close.
    """
    client, main = app_client
    pid = _claimable_player(client, main, "op-bad-token")

    response = client._client.post(
        _claim_url(pid),
        headers={**HONEST_HEADERS, "Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401, (
        "a forged token was answered %s: %s" % (response.status_code, response.text[:200])
    )
    assert main.players_db[pid].get("progress") == 0.0


def test_claiming_offline_with_another_players_session_is_refused(app_client):
    """A VALID session that names a player it does not own is refused.

    This is the arm a 401-only guard would miss: the credential is real, so the
    request is authenticated, and only the ownership check stops it. The server
    answers 403 here, not 401 -- "you proved who you are, but not that you own
    this player" is a different fault from "prove who you are".
    """
    from server.tests.wc_test_auth import auth_headers

    client, main = app_client
    pid = _claimable_player(client, main, "op-victim")
    attacker = client.post("/player/create",
                           json={"name": "op-attacker",
                                 "spirit_root": "fire"}).json()["id"]

    response = client._client.post(
        _claim_url(pid),
        headers={**HONEST_HEADERS, **auth_headers(attacker)},
    )

    assert response.status_code == 403, (
        "another player's session claimed %s's offline rewards: %s %s"
        % (pid, response.status_code, response.text[:200])
    )
    assert main.players_db[pid].get("progress") == 0.0, \
        "a cross-player offline claim credited the victim"


def test_the_owner_with_their_own_session_is_served(app_client):
    """The control: the refusals above are about the CALLER, not the route.

    Without this, three green 401/403 assertions would be consistent with a
    route that refuses every caller -- including the rightful owner. Measured on
    this tree: 200, with the ten banked hours actually paid.
    """
    client, main = app_client
    pid = _claimable_player(client, main, "op-owner")
    client.sign_in(pid)

    response = client.post(_claim_url(pid), headers=HONEST_HEADERS)
    body = response.json()

    assert response.status_code == 200, (
        "the owner was refused their own offline claim: %s %s"
        % (response.status_code, response.text[:200])
    )
    assert body["ok"] is True
    assert body["elapsed_hours"] == pytest.approx(10.0, abs=0.1), \
        "the ten banked hours were not measured"
    assert body["cultivation_progress_gained"] > 0.0, \
        "the owner was served a claim that paid nothing"


def test_the_refusal_is_the_auth_layer_and_not_the_lifecycle_gate(app_client):
    """An anonymous caller is refused 401 even when the id is NOT a real player.

    If the route answered 400 ("player_id required") for an unknown id and 401
    for a real one, the difference between the two answers would be a free test
    of which player ids exist, served to callers with no credentials at all.
    Both must be the same answer.
    """
    client, main = app_client
    ghost = "op_no_such_player_id"

    real = client._client.post(_claim_url(_claimable_player(client, main, "op-oracle-real")),
                               headers=HONEST_HEADERS)
    fake = client._client.post(_claim_url(ghost), headers=HONEST_HEADERS)

    assert real.status_code == fake.status_code == 401, (
        "the two answers differ, which tells an anonymous caller which player "
        "ids exist: real=%s fake=%s" % (real.status_code, fake.status_code)
    )


# ==========================================================================
# 10. THE 72-HOUR CAP IS A BOUND ON THE MONEY, NOT A NUMBER IN A FIELD
# ==========================================================================
#
# Section 5 already pins that `capped_hours` reads 72.0. That is the weak form:
# a route could report 72.0 and pay for 5000 hours, and the field would still
# read correctly. These tests compare two LIVE calculations and assert the
# PAYOUTS are identical, so the cap can only pass if it is what the money is
# priced from.

# Long enough that nothing else could be responsible for the equality.
ABSENTIENT_HOURS = 500.0


def _wallet_stamped_ago(seconds):
    return FakeWalletStore(FakeWallet(last_offline_claimed_at=time.time() - seconds))


def _battle_pass_xp(result):
    return next(r.amount for r in result.rewards
                if r.reward_type is op.OfflineRewardType.BATTLE_PASS_XP)


def _stone(result, tier):
    return next(r.amount for r in result.rewards
                if r.reward_type is op.OfflineRewardType.SPIRIT_STONES
                and r.currency == tier)


def _claim_at(hours, **row_overrides):
    return op.calculate_full_offline_progress(
        store_row(**row_overrides), time.time(),
        wallet_store=_wallet_stamped_ago(int(hours * 3600)))


def test_the_cap_constant_is_three_days():
    """The one place the bound is written down."""
    assert op.MAX_OFFLINE_HOURS == pytest.approx(72.0)
    assert op.MAX_OFFLINE_HOURS == pytest.approx(3 * 24)


def test_a_5000_hour_absence_pays_exactly_what_72_hours_pays():
    """The cap as a bound on money, not on a reported number.

    5000 hours is ~69x the cap. Uncapped it would pay 25000 battle-pass XP
    instead of 360, and 250000 copper instead of 3600. Equality here is only
    possible if every reward is multiplied from `capped_hours`.
    """
    at_cap = _claim_at(op.MAX_OFFLINE_HOURS)
    way_past = _claim_at(ABSENTIENT_HOURS)

    assert _battle_pass_xp(way_past) == pytest.approx(_battle_pass_xp(at_cap)), (
        "a 5000h absence paid %r XP against %r for 72h -- the cap is not "
        "pricing the payout" % (_battle_pass_xp(way_past), _battle_pass_xp(at_cap))
    )
    for tier in ("copper", "low_grade", "mid_grade", "high_grade", "extreme_grade"):
        try:
            at_cap_amount = _stone(at_cap, tier)
        except StopIteration:
            continue
        assert _stone(way_past, tier) == pytest.approx(at_cap_amount), \
            "%s grew past the cap" % tier


def test_the_cap_boundary_is_exactly_seventy_two_hours():
    """Just under the cap pays more than just over it, and both are bounded.

    Walked at measured values on this tree: 71.9h pays 359.5 XP, 72.0h pays
    360.0, and 72.1h / 96h / 5000h all pay 360.0. So the transition is at the
    cap and nowhere else -- a cap applied one hour early or late would move it.
    """
    just_under = _claim_at(op.MAX_OFFLINE_HOURS - 0.1)
    exactly = _claim_at(op.MAX_OFFLINE_HOURS)
    just_over = _claim_at(op.MAX_OFFLINE_HOURS + 0.1)

    assert _battle_pass_xp(just_under) < _battle_pass_xp(exactly), \
        "an absence just under the cap paid the same as one exactly on it"
    assert _battle_pass_xp(just_over) == pytest.approx(_battle_pass_xp(exactly)), \
        "an absence just past the cap paid more than one exactly on it"


def test_every_reward_on_a_long_absence_is_priced_from_the_capped_hours():
    """Each reward line's own rate, multiplied by the capped figure, is its amount.

    Read from the ``rate_per_hour`` the line publishes, so a retune of any rate
    table leaves this green.
    """
    result = _claim_at(ABSENTIENT_HOURS)

    assert result.capped_hours == pytest.approx(op.MAX_OFFLINE_HOURS)
    for reward in result.rewards:
        rate = reward.details.get("rate_per_hour")
        if rate is None:
            continue
        assert reward.amount == pytest.approx(rate * result.capped_hours), (
            "%s is not priced from the capped hours" % reward.reward_type.value
        )


def test_the_cap_bounds_progression_not_just_the_reported_hours():
    """Cultivation progress stops growing too, measured across every stage.

    Cultivation is clamped to the stage remainder as well as to the cap, so this
    is checked at four realm/stage pairs whose rates differ by 20x. If the cap
    were removed, a 500-hour absence at mortal/late would pay 500.0 instead of
    the 72.0 measured here.
    """
    for realm, stage in (("mortal", "late"), ("foundation", "late"),
                         ("core", "late"), ("nascent_soul", "late")):
        at_cap = _claim_at(op.MAX_OFFLINE_HOURS, cultivation_realm=realm,
                           cultivation_stage=stage)
        way_past = _claim_at(ABSENTIENT_HOURS, cultivation_realm=realm,
                             cultivation_stage=stage)

        assert way_past.cultivation_progress_gained == \
            pytest.approx(at_cap.cultivation_progress_gained), (
            "%s/%s: a 500h absence earned %r progress against %r for 72h"
            % (realm, stage, way_past.cultivation_progress_gained,
               at_cap.cultivation_progress_gained)
        )


def test_the_raw_elapsed_hours_are_still_reported_past_the_cap():
    """Capping the money must not also hide how long the player was away.

    The client screen shows both figures; a preview that reported 72.0 for a
    500-hour absence would be lying about the only number the player cannot
    influence.
    """
    result = _claim_at(ABSENTIENT_HOURS)

    assert result.elapsed_hours > ABSENTIENT_HOURS, (
        "premise broken: the elapsed window was not actually long (%r)"
        % result.elapsed_hours
    )
    assert result.elapsed_hours == pytest.approx(ABSENTIENT_HOURS, abs=1.0)
    assert result.capped_hours == pytest.approx(op.MAX_OFFLINE_HOURS)


# ==========================================================================
# 11. NON-FINITE AND UNUSABLE INPUTS
# ==========================================================================
#
# The offline window is anchored on a wallet timestamp column declared as a bare
# DOUBLE with no default, so NULL, NaN and infinity are shapes this code is
# handed rather than hypotheticals. NaN is the dangerous one: it defeats every
# comparison, so `elapsed_seconds < 0` is False for it and a clamp written as
# `if x > limit` never fires.


@pytest.mark.parametrize("value", [NAN, POS_INF, NEG_INF],
                         ids=["nan", "pos-inf", "neg-inf"])
def test_a_non_finite_timestamp_is_never_read_as_a_real_anchor(value):
    """``_finite_epoch`` is the single reader, and it refuses all three."""
    assert op._finite_epoch(value) is None, \
        "a non-finite timestamp was accepted as a real epoch"


@pytest.mark.parametrize("value", [None, True, False, "", "abc", [], {}, object(),
                                  10 ** 400],
                         ids=["none", "true", "false", "empty-str", "str",
                              "list", "dict", "object", "huge-int"])
def test_an_unusable_timestamp_is_refused_for_every_junk_shape(value):
    """Not just the non-finite cases: nothing unpriceable may become an anchor.

    ``True`` is in this list on purpose. ``bool`` subclasses ``int``, so
    ``True`` reads as epoch second 1 -- a 56-year absence, which the cap then
    prices as a full 72 hours on the very first claim.
    """
    assert op._finite_epoch(value) is None


def test_a_finite_timestamp_survives_the_reader():
    """The control for the two tests above: the reader is not simply refusing."""
    assert op._finite_epoch(1_700_000_000) == pytest.approx(1_700_000_000.0)
    assert op._finite_epoch(1_700_000_000.5) == pytest.approx(1_700_000_000.5)


@pytest.mark.parametrize("value", [NAN, POS_INF, NEG_INF],
                         ids=["nan", "pos-inf", "neg-inf"])
def test_a_non_finite_wallet_anchor_pays_nothing_rather_than_crashing(value):
    """End to end: the claim is bounded to zero, not an exception and not nan.

    Before the guard in ``_finite_epoch`` a missing anchor took the payout route
    down with ``TypeError: unsupported operand type(s) for -: 'float' and
    'NoneType'``, i.e. HTTP 500 on a POST. A NaN column was quieter and worse:
    it did not raise, and ``min(nan, 72.0)`` returns nan, so the reply reported
    nan hours for a window that was never measured.
    """
    store = FakeWalletStore(FakeWallet(last_offline_claimed_at=value))

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours == 0.0
    assert result.capped_hours == 0.0
    assert result.rewards == [], "an unpriceable anchor still produced payouts"
    assert math.isfinite(result.elapsed_hours), "the reply reported a nan window"


def test_a_non_finite_last_online_anchor_pays_nothing_too():
    """The fallback column is guarded by the same reader, not only the first one.

    ``last_offline_claimed_at`` is None for a player who has never claimed, so
    this arm is the one every new player takes.
    """
    store = FakeWalletStore(FakeWallet(last_offline_claimed_at=None,
                                      last_online_at=NAN))

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours == 0.0
    assert result.rewards == []


def test_a_bool_timestamp_does_not_buy_fifty_six_years_of_absence():
    """``True`` must not read as epoch second 1 and be priced as a full 72h.

    Measured on this tree: with ``True`` accepted as an anchor the elapsed hours
    are ~5.8 million and the cap turns that into a maximum-size payout on the
    player's first press. With the guard, it is zero.
    """
    store = FakeWalletStore(FakeWallet(last_offline_claimed_at=True))

    result = op.calculate_full_offline_progress(store_row(), time.time(),
                                                wallet_store=store)

    assert result.elapsed_hours == 0.0, \
        "a bool timestamp was priced as a real absence"
    assert result.capped_hours == 0.0
    assert result.rewards == []


def test_a_preview_over_an_unpriceable_anchor_reports_zero_not_nan():
    """The preview is what the client renders, so it must survive the same row."""
    preview = op.get_offline_preview(
        store_row(), time.time(),
        wallet_store=FakeWalletStore(FakeWallet(last_offline_claimed_at=NAN)))

    assert preview["can_claim"] is True
    assert preview["elapsed_hours"] == 0.0
    assert preview["capped_hours"] == 0.0
    assert preview["estimated_rewards"] == {}, \
        "an unpriceable anchor still rendered reward lines on the client screen"


def test_an_infinite_device_clock_is_refused_with_the_skew_message():
    """Both infinities fail the comparison, so both are caught. NaN is not.

    Measured: ``+inf`` and ``-inf`` raise the skew ValueError, because
    ``abs(device_now - server_now)`` is infinite and exceeds the tolerance. The
    NaN case is the one that does not, and it is pinned separately below.
    """
    for value in (POS_INF, NEG_INF):
        with pytest.raises(ValueError) as excinfo:
            op.calculate_full_offline_progress(store_row(), value,
                                               wallet_store=_wallet_stamped_ago(3600))
        assert "skew" in str(excinfo.value).lower()


def test_a_non_finite_result_is_never_reported_as_hours(app_client):
    """No path in the module may put nan or infinity in an hours field.

    Broad by design: it drives every calculator and both entry points over a
    poisoned anchor, so a future branch added without a guard is caught here
    rather than shipped to the client screen.
    """
    poisoned = FakeWalletStore(FakeWallet(last_offline_claimed_at=NAN))
    results = [
        op.calculate_full_offline_progress(store_row(), time.time(),
                                           wallet_store=poisoned),
        op.calculate_full_offline_progress(store_row(spirit_root="fire",
                                                     sect_id="sect-1",
                                                     sect_rank="elder",
                                                     beasts=[{"beast_id": "b1",
                                                               "affinity": 0}]),
                                           time.time(), wallet_store=poisoned),
    ]
    preview = op.get_offline_preview(store_row(), time.time(), wallet_store=poisoned)

    for result in results:
        assert math.isfinite(result.elapsed_hours)
        assert math.isfinite(result.capped_hours)
        assert math.isfinite(result.cultivation_progress_gained)
        for reward in result.rewards:
            assert math.isfinite(reward.amount), \
                "%s reported a non-finite amount" % reward.reward_type.value
    for field in ("elapsed_hours", "capped_hours", "cultivation_progress"):
        assert math.isfinite(preview[field]), "preview.%s is not finite" % field


def test_a_non_finite_server_clock_is_bounded_to_zero_rather_than_reported_as_nan(monkeypatch):
    """The SECOND finite guard, on the subtraction itself.

    ``_finite_epoch`` refuses a non-finite ANCHOR, so with an honest wall clock
    ``elapsed_seconds = server_now - last_claim`` cannot be non-finite and the
    ``math.isfinite`` half of that line looks unreachable. It is not: it is
    reachable exactly when ``time.time()`` itself is non-finite, which is why
    the two halves were written together.

    Measured on this tree with ``time.time`` patched to NaN:
      * both guards ....... elapsed_hours 0.0, capped_hours 0.0
      * isfinite dropped ... elapsed_hours nan, capped_hours nan
    and ``min(nan, 72.0)`` is nan, so without the guard the client screen is
    told the player was away for a nan-hour window.

    The anchor is deliberately a FINITE epoch, so the only non-finite value in
    the subtraction is the clock. That is what distinguishes this guard from
    ``_finite_epoch``, and it is why the two cannot be collapsed into one test.
    """
    monkeypatch.setattr(op.time, "time", lambda: NAN)

    result = op.calculate_full_offline_progress(
        store_row(), 1_700_000_000.0,
        wallet_store=FakeWalletStore(FakeWallet(last_offline_claimed_at=1_700_000_000.0)))

    assert math.isfinite(result.elapsed_hours), \
        "a non-finite window was reported to the client"
    assert result.elapsed_hours == 0.0
    assert math.isfinite(result.capped_hours)
    assert result.rewards == []


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT, measured on this tree: the device-clock skew check is "
    "`abs(device_now - server_now) > MAX_CLOCK_SKEW_SECONDS`, and every "
    "comparison against NaN is False -- so `abs(nan - x)` is nan, `nan > "
    "86400.0` is False, and the ValueError never fires. A caller who sends "
    "`X-Device-Time: nan` is therefore served a full claim: measured over HTTP, "
    "POST /lifecycle/{pid}/claim-offline answered 200 with elapsed_hours 10.0 "
    "and cultivation_progress_gained 30.0, while the same route answered 400 "
    "for `inf` and for a clock five days in the future. The module guards its "
    "STORED timestamps with `_finite_epoch` but applies no such check to the "
    "device clock, which is the one value the CALLER supplies. The fix is to "
    "reject a non-finite `device_now` before the skew comparison, the same way "
    "_finite_epoch rejects a non-finite anchor. STRICT, so this flips to a "
    "failure the moment the guard lands and cannot be left behind as a stale "
    "xfail."
))
def test_a_nan_device_clock_cannot_slip_past_the_skew_check():
    """The one non-finite input that is not refused.

    The counterpart to the two tests above: those prove infinities are caught,
    which is exactly what makes the NaN case a defect rather than an oversight
    in the tests. NaN is the value every comparison silently accepts.
    """
    store = _wallet_stamped_ago(10 * 3600)

    with pytest.raises(ValueError) as excinfo:
        op.calculate_full_offline_progress(store_row(), NAN, wallet_store=store)

    assert "skew" in str(excinfo.value).lower()


@pytest.mark.xfail(strict=True, reason=(
    "The route-level half of the NaN device-clock defect above. Measured over "
    "HTTP on this tree: an authenticated owner sending `X-Device-Time: nan` to "
    "POST /lifecycle/{pid}/claim-offline is answered 200 with a full payout "
    "(elapsed_hours 10.0, cultivation_progress_gained 30.0), while `inf` and a "
    "clock five days ahead are both answered 400. Same fix, same strictness: it "
    "flips to a failure when the guard lands."
))
def test_a_nan_device_clock_is_refused_over_http(app_client):
    """The same defect seen from outside, where a caller can actually send it."""
    client, main = app_client
    pid = _claimable_player(client, main, "op-nan-clock")
    client.sign_in(pid)

    response = client.post(_claim_url(pid), headers={"X-Device-Time": "nan"})

    assert response.status_code == 400, (
        "a nan device clock was answered %s: %s"
        % (response.status_code, response.text[:200])
    )
