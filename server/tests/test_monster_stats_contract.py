"""The public contract of ``server/monster_stats.py``.

Scope: this file tests the MODULE's own promise -- what a stat line is made of
and what the lookup methods do -- not whether the game's content files are
covered. Two sibling files already own those:

* ``test_monster_stat_table.py`` -- zone/dungeon monster names resolve, row
  consistency against the multiplier ladder, boss outclasses its peers.
* ``test_monster_stats_loader_defects.py`` -- reload REPLACES instead of
  merging, the scale floor, the refusal marker, the malformed-row message.

What is owned here, and why it is stated as SHAPE not NUMBERS:

1. ``MonsterStatTable.REQUIRED_FIELDS`` is the declared contract. Every monster
   the loader serves must carry all of them, and the serialised dict
   (``to_dict``) must expose exactly that set plus the id and
   ``reachable_realms``. Nothing here asserts a specific hit-point number, so a
   balance retune that doubles every monster's health keeps this file green;
   a row that LOSES a required field turns it red.
2. Positive-value invariants: health, attack, defence, mana, speed and both
   rewards are whole positive numbers. A monster with 0 health cannot be
   fought; one with 0 attack cannot fight.
3. Lookup of a known id returns that row and the SAME object from every
   accessor; an unknown or malformed id never returns a row by accident.
4. ``MonsterStatError`` stays a ``KeyError`` subclass, so callers that catch
   ``KeyError`` around this module keep working.
5. ``scaled_for_context`` always says which realm it started from and whether
   it refused; scaling a monster onto its OWN realm returns its authored
   numbers unchanged (a ratio of 1), which is a relationship rather than a
   stored value.
6. The process-wide accessor hands back one manager and ``reload_monsters``
   genuinely drops it, verified with a restore in a ``finally`` block.

Anti-vacuity: ``test_control_a_holed_row_set_fails_the_completeness_check``
runs the same completeness checker over a table with one field deleted and
requires it to complain. If that passes, the completeness check above cannot
fail and every other assertion here is decoration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "server") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "server"))

from monster_stats import (  # noqa: E402
    MonsterStatError,
    MonsterStatManager,
    MonsterStatTable,
    get_monster_stat_manager,
    reload_monsters,
)

# Keys that are attributes rather than data-file fields.
EXTRA_SERIALISED_KEYS = ("id", "reachable_realms")

# Text fields that must be non-empty strings.
TEXT_FIELDS = ("name", "realm", "element", "rarity", "stage", "tier")

# The stats a fight computes against, plus both rewards. All must be > 0.
POSITIVE_FIELDS = ("hp", "atk", "def", "mp", "spd", "spirit_stone", "xp")


@pytest.fixture(scope="module")
def manager() -> MonsterStatManager:
    return MonsterStatManager()


def _missing_required(rows) -> dict:
    """Map row id -> sorted list of required fields it does not carry."""
    missing = {}
    for monster_id, data in rows.items():
        absent = sorted(f for f in MonsterStatTable.REQUIRED_FIELDS if f not in data)
        if absent:
            missing[monster_id] = absent
    return missing


# --------------------------------------------------------------------------
# 1. The declared field contract
# --------------------------------------------------------------------------
def test_required_fields_is_a_usable_declaration():
    fields = MonsterStatTable.REQUIRED_FIELDS
    assert fields, "REQUIRED_FIELDS is empty, so nothing is required"
    assert isinstance(fields, tuple)
    assert len(set(fields)) == len(fields), f"duplicate field names: {fields}"
    assert all(isinstance(f, str) and f for f in fields), fields
    # The data-file spelling of defence is "def"; the attribute is "defense".
    # A row is required to carry "def", so a row carrying only "defense" is
    # malformed and must be reported as such.
    assert "def" in fields, "the data-file defence key 'def' is not required"


def test_every_served_monster_carries_every_required_field(manager):
    rows = {m.id: m.to_dict() for m in manager.get_all_monsters()}
    assert rows, "the manager served no monsters at all"
    missing = _missing_required(rows)
    assert not missing, f"monsters missing required fields: {missing}"


def test_control_a_holed_row_set_fails_the_completeness_check(manager):
    """Negative control: the checker above must reject a row with a hole."""
    rows = {m.id: m.to_dict() for m in manager.get_all_monsters()}
    assert not _missing_required(rows), "the shipped table is already incomplete"

    victim = sorted(rows)[0]
    holed = dict(rows[victim])
    holed.pop("hp")

    reported = _missing_required({victim: holed})
    assert reported == {victim: ["hp"]}, (
        f"a row missing hp was reported as {reported}; the check is vacuous"
    )


def test_an_unusable_number_is_reported_with_the_row_id(tmp_path):
    """A non-numeric stat must raise, naming the row -- and the field.

    A MISSING field is wrapped by ``MonsterStatError`` with the monster id in
    the message; a BAD number used to escape as a bare ``ValueError: invalid
    literal for int()`` from inside the loop, which on a 58-row table does not
    say which monster is broken. This asserts the two malformed-row paths
    report the same shape, so an operator reading a boot traceback is pointed
    at one row either way.
    """
    for field in MonsterStatTable.REQUIRED_FIELDS:
        if field in ("name", "realm", "element", "rarity", "stage", "tier",
                     "is_boss"):
            continue
        broken = _valid_row("string_hp_beast")
        broken[field] = "not-a-number"
        path = _write_json(
            tmp_path, f"bad_{field}.json",
            {"monsters": {"string_hp_beast": broken}},
        )
        with pytest.raises(MonsterStatError) as caught:
            MonsterStatManager(path)
        message = str(caught.value)
        assert "string_hp_beast" in message, (
            f"a non-numeric {field} was reported without the row id: {message!r}"
        )
        assert field in message, (
            f"a non-numeric {field} was reported without naming the field: "
            f"{message!r}"
        )


def test_control_the_row_name_says_which_row_broke(tmp_path):
    """Two different broken rows must not report the same thing.

    Without this the test above could pass on a message that names the row
    only by accident (a fixed string, or the loader's file path).
    """
    messages = []
    for row_id in ("aaa_beast", "zzz_beast"):
        broken = _valid_row(row_id)
        broken["mp"] = "not-a-number"
        path = _write_json(tmp_path, f"{row_id}.json", {"monsters": {row_id: broken}})
        with pytest.raises(MonsterStatError) as caught:
            MonsterStatManager(path)
        messages.append(str(caught.value))
    assert messages[0] != messages[1], (
        f"both rows reported the same thing: {messages}"
    )
    assert "aaa_beast" in messages[0], messages[0]
    assert "zzz_beast" in messages[1], messages[1]
    assert "aaa_beast" not in messages[1], messages[1]
    assert "zzz_beast" not in messages[0], messages[0]


def test_a_non_numeric_stat_keeps_the_key_error_base(tmp_path):
    """The new error must stay catchable by existing ``except KeyError``."""
    broken = _valid_row("string_hp_beast")
    broken["hp"] = "not-a-number"
    path = _write_json(tmp_path, "ke.json", {"monsters": {"string_hp_beast": broken}})
    with pytest.raises(KeyError):
        MonsterStatManager(path)


def test_a_well_formed_row_still_loads_after_the_wrapping(tmp_path):
    """Control: the wrapper did not break the good path."""
    path = _write_json(
        tmp_path, "good.json", {"monsters": {"good_beast": _valid_row("good_beast")}}
    )
    monster = MonsterStatManager(path).require_monster("good_beast")
    assert monster.hp == 100
    assert monster.defense == 5
    assert monster.to_dict()["def"] == 5


def test_every_field_the_data_file_carries_is_declared_required(manager):
    """The declaration must not fall behind the data it guards.

    If a new column appears in ``monsters.json`` and nobody adds it to
    ``REQUIRED_FIELDS``, the loader would keep serving rows that disagree about
    it. Asserted as a relationship between the declaration and the file, so a
    balance retune (values change, columns do not) stays green.
    """
    optional = set(EXTRA_SERIALISED_KEYS)
    undeclared = {}
    for monster in manager.get_all_monsters():
        extra = sorted(
            set(monster.to_dict()) - set(MonsterStatTable.REQUIRED_FIELDS) - optional
        )
        if extra:
            undeclared[monster.id] = extra
    assert not undeclared, (
        "these fields exist on rows but are not in REQUIRED_FIELDS, so the "
        f"loader never checks them: {undeclared}"
    )


def test_serialised_keys_are_exactly_the_contract(manager):
    expected = set(MonsterStatTable.REQUIRED_FIELDS) | set(EXTRA_SERIALISED_KEYS)
    for monster in manager.get_all_monsters():
        keys = set(monster.to_dict())
        assert keys == expected, (
            f"{monster.id}: serialised keys {sorted(keys)} != "
            f"declared {sorted(expected)}"
        )


def test_declared_fields_are_reachable_as_attributes(manager):
    """``def`` is the data-file key; ``defense`` is the attribute. Both work."""
    declared = set(MonsterStatTable.REQUIRED_FIELDS)
    for monster in manager.get_all_monsters():
        for field in declared - {"def"}:
            assert hasattr(monster, field), f"{monster.id} has no attribute {field}"
        assert hasattr(monster, "defense"), f"{monster.id} has no attribute defense"
        # The attribute and the serialised key must be the same value.
        assert monster.defense == monster.to_dict()["def"]
        assert monster.id == monster.to_dict()["id"]


def test_to_dict_returns_an_independent_copy(manager):
    monster = manager.get_all_monsters()[0]
    first = monster.to_dict()
    first["hp"] = -1
    first["reachable_realms"].append("mutated_realm")
    second = monster.to_dict()
    assert second["hp"] > 0, "to_dict shares storage with the monster"
    assert "mutated_realm" not in second["reachable_realms"], (
        "to_dict hands out the live reachable_realms list"
    )


# --------------------------------------------------------------------------
# 2. Positive-value invariants
# --------------------------------------------------------------------------
def test_every_stat_and_reward_is_a_positive_whole_number(manager):
    monsters = manager.get_all_monsters()
    assert monsters, "the manager served no monsters at all"
    offenders = []
    for monster in monsters:
        data = monster.to_dict()
        for field in POSITIVE_FIELDS:
            value = data[field]
            if not isinstance(value, int) or isinstance(value, bool):
                offenders.append(f"{monster.id}.{field} is not an int: {value!r}")
            elif value <= 0:
                offenders.append(f"{monster.id}.{field} is not positive: {value}")
    assert not offenders, offenders


def test_text_fields_are_non_empty_strings(manager):
    offenders = []
    for monster in manager.get_all_monsters():
        data = monster.to_dict()
        for field in TEXT_FIELDS:
            value = data[field]
            if not isinstance(value, str) or not value.strip():
                offenders.append(f"{monster.id}.{field} is not a non-empty str: {value!r}")
    assert not offenders, offenders


def test_boss_flag_is_a_real_boolean(manager):
    for monster in manager.get_all_monsters():
        assert isinstance(monster.is_boss, bool), (
            f"{monster.id}.is_boss is {type(monster.is_boss).__name__}, not bool"
        )


def test_boss_flag_is_coerced_even_when_the_row_is_not_a_real_bool(tmp_path):
    """The row may say ``is_boss: 1``; the attribute must still be a bool.

    Written because the shipped data uses real booleans, so checking the type on
    the live table alone cannot tell a coerced flag from a passthrough one.
    """
    for raw in (1, 0, "yes", "", None):
        row = _valid_row("flag_beast")
        row["is_boss"] = raw
        path = _write_json(
            tmp_path, f"flag_{type(raw).__name__}_{raw}.json",
            {"monsters": {"flag_beast": row}},
        )
        flag = MonsterStatManager(path).require_monster("flag_beast").is_boss
        assert isinstance(flag, bool), (
            f"is_boss came back as {type(flag).__name__} for row value {raw!r}"
        )
        assert flag is bool(raw), f"is_boss={flag} for row value {raw!r}"


def test_a_stat_scaled_far_down_is_floored_not_truncated_to_zero(tmp_path):
    """A tiny stat moved down a long ladder must not land on 0.

    The shipped table happens to stay above the floor on every shipped
    combination, so checking the live table alone cannot see a removed floor.
    This plants the case: authored at a high multiplier, scaled onto a low one,
    where plain truncation would produce 0.
    """
    path = _write_json(tmp_path, "tiny.json", {
        "realm_multipliers": {"low": 1.0, "high": 8.0},
        "monsters": {"tiny_beast": dict(
            _valid_row("tiny_beast"), realm="high", hp=2, atk=2, mp=2, spd=2,
        )},
    })
    manager = MonsterStatManager(path)
    out = manager.scaled_for_context("tiny_beast", "low")
    assert out["scaling_refused"] is False, (
        "control never reached the scaling path; the scale was refused instead"
    )
    for field in ("hp", "atk", "mp", "spd"):
        assert out[field] >= 1, (
            f"{field} truncated to {out[field]} (authored {2}); a 0-stat monster "
            "cannot be fought"
        )


def test_reachable_realms_is_a_list_of_strings(manager):
    for monster in manager.get_all_monsters():
        realms = monster.reachable_realms
        assert isinstance(realms, list), f"{monster.id}.reachable_realms is not a list"
        assert all(isinstance(r, str) and r for r in realms), (
            f"{monster.id}.reachable_realms holds a non-string: {realms}"
        )


# --------------------------------------------------------------------------
# 3. Lookup of a valid monster
# --------------------------------------------------------------------------
def test_known_id_resolves_consistently_across_every_accessor(manager):
    monster_id = manager.get_monster_ids()[0]
    assert manager.has_monster(monster_id) is True

    via_get = manager.get_monster(monster_id)
    via_require = manager.require_monster(monster_id)
    assert via_get is not None, "has_monster said yes but get_monster returned None"
    assert via_get is via_require, "the two lookups returned different objects"
    assert via_get.id == monster_id
    assert manager.require_monster(monster_id) is via_get, (
        "repeated lookups returned different objects"
    )


def test_ids_and_rows_line_up_one_to_one(manager):
    ids = manager.get_monster_ids()
    monsters = manager.get_all_monsters()
    assert len(ids) == len(set(ids)), "duplicate monster ids"
    assert [m.id for m in monsters] == ids, (
        "get_monster_ids and get_all_monsters disagree about the table"
    )
    assert len(monsters) > 1, "the table needs more than one row to be meaningful"
    for monster_id in ids:
        assert manager.get_monster(monster_id) is manager.monsters[monster_id]


def test_boss_and_realm_filters_are_subsets_of_the_table(manager):
    all_ids = set(manager.get_monster_ids())

    bosses = manager.get_bosses()
    assert bosses, "the table declares no bosses at all"
    for boss in bosses:
        assert boss.id in all_ids, f"{boss.id} is not in the table"
        assert boss.is_boss is True, f"{boss.id} came back from get_bosses unflagged"
    # No non-boss may leak into the boss list.
    expected_bosses = {m.id for m in manager.get_all_monsters() if m.is_boss}
    assert {b.id for b in bosses} == expected_bosses, (
        "get_bosses returned a different set than the is_boss flags imply"
    )

    for realm in {m.realm for m in manager.get_all_monsters()}:
        rows = manager.get_by_realm(realm)
        assert rows, f"realm {realm!r} is in use but get_by_realm returned nothing"
        assert {r.id for r in rows} <= all_ids
        assert {r.realm for r in rows} == {realm}, (
            f"get_by_realm({realm!r}) returned a row from another realm"
        )


def test_a_second_manager_reads_the_same_table(manager):
    other = MonsterStatManager()
    assert other.get_monster_ids() == manager.get_monster_ids()
    assert other.realm_multipliers == manager.realm_multipliers


def test_a_narrow_data_file_serves_exactly_its_own_rows(tmp_path, manager):
    """The manager serves what the file declares -- no invented rows."""
    document = {
        "realm_multipliers": {"mortal": 1.0},
        "monsters": {"probe_beast": _valid_row("probe_beast")},
    }
    path = _write_json(tmp_path, "narrow.json", document)
    narrow = MonsterStatManager(path)
    assert narrow.get_monster_ids() == ["probe_beast"]
    assert narrow.has_monster("probe_beast")
    assert narrow.require_monster("probe_beast").id == "probe_beast"


# --------------------------------------------------------------------------
# 4. Unknown and malformed ids
# --------------------------------------------------------------------------
UNKNOWN_IDS = (
    "definitely_not_a_monster",
    "",
    "   ",
    "SPIRIT_WOLF",          # case must matter, not be folded away
    "spirit wolf",          # space instead of underscore
    None,                   # wrong type entirely
    42,
)


@pytest.mark.parametrize("monster_id", UNKNOWN_IDS)
def test_an_unknown_or_malformed_id_never_yields_a_row(manager, monster_id):
    assert manager.has_monster(monster_id) is False, (
        f"{monster_id!r} was reported as present"
    )
    assert manager.get_monster(monster_id) is None, (
        f"{monster_id!r} resolved to a stat line"
    )
    with pytest.raises(MonsterStatError):
        manager.require_monster(monster_id)


def test_an_unhashable_id_fails_loudly_rather_than_returning_a_row(manager):
    """A list is unhashable. It must raise, never look up."""
    with pytest.raises(TypeError):
        manager.has_monster(["spirit_wolf"])
    with pytest.raises(TypeError):
        manager.get_monster(["spirit_wolf"])


def test_the_error_type_stays_catchable_as_key_error(manager):
    assert issubclass(MonsterStatError, KeyError), (
        "callers catching KeyError around this module would break"
    )
    with pytest.raises(KeyError):
        manager.require_monster("definitely_not_a_monster")


def test_a_known_id_does_not_raise(manager):
    assert manager.require_monster(manager.get_monster_ids()[0]).hp > 0


# --------------------------------------------------------------------------
# 5. scaled_for_context shape
# --------------------------------------------------------------------------
def test_scaled_line_always_says_where_it_came_from(manager):
    monster = manager.get_all_monsters()[0]
    for target in list(manager.realm_multipliers) + ["not_a_real_realm"]:
        out = manager.scaled_for_context(monster.id, target)
        for key in ("scaled_from_realm", "scaled_onto_realm", "scaling_refused"):
            assert key in out, (
                f"{monster.id} onto {target}: no {key} in {sorted(out)}"
            )
        assert out["scaled_from_realm"] == monster.realm
        assert out["scaled_onto_realm"] == target
        assert isinstance(out["scaling_refused"], bool)


def test_scaling_onto_its_own_realm_returns_the_authored_numbers(manager):
    """A ratio of 1 must be the identity, not a re-rounded line."""
    for monster in manager.get_all_monsters():
        if monster.realm not in manager.realm_multipliers:
            continue
        out = manager.scaled_for_context(monster.id, monster.realm)
        assert out["scaling_refused"] is False
        assert out["hp"] == monster.hp, (
            f"{monster.id}: hp {monster.hp} became {out['hp']} scaling onto itself"
        )
        assert out["atk"] == monster.atk


def test_an_unknown_realm_refuses_and_leaves_the_stats_alone(manager):
    monster = manager.get_all_monsters()[0]
    out = manager.scaled_for_context(monster.id, "not_a_real_realm")
    assert out["scaling_refused"] is True, (
        "an unknown target realm was reported as a successful scale"
    )
    assert out["hp"] == monster.hp and out["atk"] == monster.atk


def test_a_scaled_line_stays_positive_for_every_known_realm(manager):
    zeroed = []
    for monster in manager.get_all_monsters():
        for realm in manager.realm_multipliers:
            out = manager.scaled_for_context(monster.id, realm)
            for field in ("hp", "atk", "def"):
                if out[field] < 1:
                    zeroed.append(f"{monster.id}@{realm}.{field}={out[field]}")
    assert not zeroed, zeroed


def test_scaling_an_unknown_id_raises(manager):
    with pytest.raises(MonsterStatError):
        manager.scaled_for_context("definitely_not_a_monster", "mortal")


# --------------------------------------------------------------------------
# 6. Process-wide accessor
# --------------------------------------------------------------------------
def test_the_accessor_hands_back_one_manager_and_reload_drops_it():
    reload_monsters()
    try:
        first = get_monster_stat_manager()
        assert first is get_monster_stat_manager(), (
            "the accessor built a second manager"
        )
        assert first.get_monster_ids(), "the shared manager serves nothing"

        reload_monsters()
        second = get_monster_stat_manager()
        assert second is not first, "reload_monsters did not drop the cached manager"
        assert second.get_monster_ids() == first.get_monster_ids(), (
            "the reloaded manager serves a different table"
        )
    finally:
        # Leave the process-wide cache as we found it.
        reload_monsters()


# --------------------------------------------------------------------------
# Malformed input
# --------------------------------------------------------------------------
def test_a_row_with_a_missing_field_is_reported_with_the_row_id(tmp_path):
    broken = _valid_row("broken_beast")
    del broken["atk"]
    path = _write_json(tmp_path, "broken.json", {"monsters": {"broken_beast": broken}})
    with pytest.raises(MonsterStatError) as caught:
        MonsterStatManager(path)
    message = str(caught.value)
    assert "broken_beast" in message, message
    assert "atk" in message, message


def test_a_file_with_no_monsters_key_serves_an_empty_table(tmp_path):
    path = _write_json(tmp_path, "empty.json", {"realm_multipliers": {"mortal": 1.0}})
    empty = MonsterStatManager(path)
    assert empty.get_monster_ids() == []
    assert empty.get_all_monsters() == []
    assert empty.get_bosses() == []
    assert empty.has_monster("spirit_wolf") is False
    assert empty.get_monster("spirit_wolf") is None
    with pytest.raises(MonsterStatError):
        empty.require_monster("spirit_wolf")


# --------------------------------------------------------------------------
# 7. Stats scale with level (the realm ladder)
#
# Stated as a RELATIONSHIP between two pieces of data -- the ladder and the
# stat line -- not as a stored number, so a balance retune of either the
# multipliers or the monsters stays green while a broken scale turns it red.
# --------------------------------------------------------------------------

def test_the_shipped_ladder_is_strictly_increasing(manager):
    """A ladder that is not ordered cannot be called a level ladder.

    This is the assumption every scaling assertion below rests on, so it is
    checked once, here, rather than assumed. If two realms ever share a
    multiplier the monotonic tests still hold (they are non-strict), but an
    unordered or constant ladder would make "higher realm is harder" false.
    """
    values = list(manager.realm_multipliers.values())
    assert values, "the data file declares no realm multipliers at all"
    assert all(isinstance(v, float) and v > 0 for v in values), (
        f"every ladder entry must be a positive float, got {values}"
    )
    assert len(set(values)) == len(values), (
        f"two realms share a multiplier, so 'higher realm' is ambiguous: {values}"
    )


def test_a_higher_realm_scales_every_stat_upward(manager):
    """The core promise: stronger context, tougher monster.

    For every monster and every pair of realms where the target ladder entry
    is strictly larger, all five scaled stats must be at least as large as the
    authored ones. Non-strict because a stat already at the floor of 1 cannot
    rise; strict is asserted separately in the next test.
    """
    ladder = sorted(
        manager.realm_multipliers.items(), key=lambda kv: kv[1]
    )
    regressions = []
    for monster in manager.get_all_monsters():
        if monster.realm not in manager.realm_multipliers:
            continue
        authored = monster.to_dict()
        for target, target_mult in ladder:
            if target_mult <= manager.realm_multipliers[monster.realm]:
                continue
            out = manager.scaled_for_context(monster.id, target)
            for field in ("hp", "atk", "def", "mp", "spd"):
                if out[field] < authored[field]:
                    regressions.append(
                        f"{monster.id}@{target}.{field}: {out[field]} < "
                        f"authored {authored[field]}"
                    )
    assert not regressions, (
        "scaling onto a higher realm made a stat smaller: " + str(regressions[:8])
    )


def test_a_strictly_higher_realm_raises_the_health_stat_not_just_the_floor(manager):
    """Control on the test above: it must be able to see a real increase.

    ``test_a_higher_realm_scales_every_stat_upward`` is non-strict, so it
    would pass on a scale that never raised anything. Here a much higher
    realm must raise health strictly for at least one real row -- proving the
    ladder is actually being read as a scale and not as "always 1".
    """
    ladder = manager.realm_multipliers
    candidates = [
        (m, t) for m in manager.get_all_monsters() if m.realm in ladder
        for t in ladder
        if ladder[t] > ladder[m.realm] * 2
    ]
    assert candidates, "no monster/realm pair is a two-step climb; check the ladder"
    raised = [
        (m.id, t) for m, t in candidates
        if manager.scaled_for_context(m.id, t)["hp"] > m.hp
    ]
    assert raised, (
        "no monster gained health scaling onto a realm 2x or higher; the "
        "ladder is being ignored"
    )


def test_a_lower_realm_scales_every_stat_downward(manager):
    """The mirror image: a weaker context must not make a monster tougher."""
    ladder = manager.realm_multipliers
    inflated = []
    for monster in manager.get_all_monsters():
        if monster.realm not in ladder:
            continue
        for target, target_mult in ladder.items():
            if target_mult >= ladder[monster.realm]:
                continue
            out = manager.scaled_for_context(monster.id, target)
            for field in ("hp", "atk", "def", "mp", "spd"):
                if out[field] > getattr(
                    monster, "defense" if field == "def" else field
                ):
                    inflated.append(
                        f"{monster.id}@{target}.{field}: {out[field]} > "
                        f"authored"
                    )
    assert not inflated, (
        "scaling onto a lower realm made a stat bigger: " + str(inflated[:8])
    )


def test_the_scale_is_exactly_the_multiplier_ratio(manager):
    """One hop must equal the ratio the ladder declares.

    Anchored on absolute integers and a whole-number ratio, so this states a
    relationship (``value * target/base``) rather than freezing a balance
    number. ``int()`` truncates toward zero, so the expectation allows the one
    unit of rounding a truncation can introduce.
    """
    for monster in manager.get_all_monsters():
        base_realm = monster.realm
        if base_realm not in manager.realm_multipliers:
            continue
        for target, target_mult in manager.realm_multipliers.items():
            ratio = target_mult / manager.realm_multipliers[base_realm]
            if not ratio.is_integer() or ratio <= 0:
                continue  # covered by the identity and direction tests
            out = manager.scaled_for_context(monster.id, target)
            assert out["scaling_refused"] is False
            authored = monster.to_dict()
            for field in ("hp", "atk", "def", "mp", "spd"):
                base_value = authored[field]
                expected = int(base_value * ratio)
                assert abs(out[field] - expected) <= 1, (
                    f"{monster.id}@{target}.{field}: scaled to {out[field]}, "
                    f"expected {base_value} * {ratio} = {expected}"
                )


def test_scaling_never_changes_the_rewards(manager):
    """``spirit_stone`` and ``xp`` are NOT on the scaling path.

    The scale loop covers hp/atk/def/mp/spd only. If a reward were folded in,
    the hardest realm would pay out proportionally more, which is a balance
    decision the module does not make. Asserted as a relationship: rewards
    after scaling == rewards authored.
    """
    for monster in manager.get_all_monsters():
        if monster.realm not in manager.realm_multipliers:
            continue
        for target in manager.realm_multipliers:
            out = manager.scaled_for_context(monster.id, target)
            assert out["spirit_stone"] == monster.spirit_stone, (
                f"{monster.id}@{target}: spirit_stone scaled to "
                f"{out['spirit_stone']} from {monster.spirit_stone}"
            )
            assert out["xp"] == monster.xp, (
                f"{monster.id}@{target}: xp scaled to {out['xp']} from {monster.xp}"
            )


def test_a_higher_realm_multiplier_scaling_is_reflectively_consistent(manager):
    """Going up then down returns the authored line (modulo truncation).

    Scaling A onto B and then the RESULT's realm relationship back must land
    on the original numbers rather than compounding. The result of a scale is
    a dict, not a row, so the inverse is checked against the authored row by
    re-scaling the authored monster through the same two hops' ratio.
    """
    ladder = manager.realm_multipliers
    if len(ladder) < 2:
        pytest.skip("the shipped ladder has fewer than two realms")
    ordered = sorted(ladder.items(), key=lambda kv: kv[1])
    low = ordered[0][0]
    high = ordered[-1][0]

    for monster in manager.get_all_monsters():
        if monster.realm not in ladder:
            continue
        up = manager.scaled_for_context(monster.id, high)
        down = manager.scaled_for_context(monster.id, low)
        # Both directions must be reported honestly, never silently refused.
        assert up["scaling_refused"] is False and down["scaling_refused"] is False
        assert up["scaled_onto_realm"] == high
        assert down["scaled_onto_realm"] == low
        # Neither direction may exceed the other when the ladder says so.
        assert up["hp"] >= down["hp"], (
            f"{monster.id}: scaling onto {high} ({up['hp']}) gave LESS health "
            f"than onto {low} ({down['hp']})"
        )


# --------------------------------------------------------------------------
# 8. Determinism: the same question gets the same answer
#
# ``scaled_for_context`` is read by dungeon and rift encounter builders, so a
# line that changes between two identical calls would make an encounter
# non-reproducible for the player and for any replay of it.
# --------------------------------------------------------------------------
def test_repeated_scaling_returns_an_identical_line(manager):
    """Ten calls, ten equal answers -- including the marker keys."""
    for monster in manager.get_all_monsters()[:8]:
        for target in list(manager.realm_multipliers)[:4]:
            answers = [
                manager.scaled_for_context(monster.id, target) for _ in range(10)
            ]
            first = answers[0]
            for attempt, other in enumerate(answers[1:], start=2):
                assert other == first, (
                    f"{monster.id} onto {target}: call {attempt} returned "
                    f"{other} but call 1 returned {first}"
                )


def test_scaling_does_not_mutate_the_stored_row(manager):
    """The scale must not write back through the shared row.

    A scale that edited the stored table in place would make the second call
    different from the first (each call re-scaling an already-scaled line) and
    would permanently change every other reader of that monster.
    """
    for monster in manager.get_all_monsters()[:8]:
        before = monster.to_dict()
        for target in list(manager.realm_multipliers)[:3]:
            manager.scaled_for_context(monster.id, target)
        after = monster.to_dict()
        assert after == before, (
            f"{monster.id} was mutated by scaling: {before} -> {after}"
        )
        # And the stored object itself must be unchanged, not just its copy.
        assert monster.hp == before["hp"]
        assert monster.atk == before["atk"]
        assert monster.defense == before["def"]


def test_a_scaled_line_is_not_shared_storage(manager):
    """Mutating one scaled result must not corrupt the next call.

    The floor arithmetic writes into the dict returned by ``to_dict``. If that
    dict were the live row's storage, scaling once with a hand-edited result
    would leak into every later read of the same monster.
    """
    monster = manager.get_all_monsters()[0]
    target = sorted(
        manager.realm_multipliers.items(), key=lambda kv: kv[1]
    )[-1][0]

    first = manager.scaled_for_context(monster.id, target)
    first["hp"] = -999
    first["reachable_realms"].append("injected_realm")

    second = manager.scaled_for_context(monster.id, target)
    assert second["hp"] > 0, "a hand-edited scaled line leaked into the next call"
    assert "injected_realm" not in second["reachable_realms"]
    assert monster.hp > 0, "a hand-edited scaled line reached the stored row"


def test_two_managers_agree_on_every_scaled_line(manager):
    """Determinism across managers: same file in, same numbers out."""
    ordered = sorted(manager.realm_multipliers.items(), key=lambda kv: kv[1])
    targets = [r for r, _ in ordered]
    assert targets, "the ladder is empty"

    first = MonsterStatManager()
    second = MonsterStatManager()
    for monster_id in first.get_monster_ids()[:8]:
        for target in targets:
            a = first.scaled_for_context(monster_id, target)
            b = second.scaled_for_context(monster_id, target)
            assert a == b, (
                f"{monster_id} onto {target}: two managers disagreed\n{a}\n{b}"
            )


def test_scaling_is_a_pure_function_of_the_row_and_the_ladder(tmp_path, manager):
    """A third manager on a COPY of the ladder reproduces the same numbers.

    Written against a rewritten data file so the module's disk read is part of
    the reproducibility claim, not just its arithmetic.
    """
    ordered = sorted(manager.realm_multipliers.items(), key=lambda kv: kv[1])
    low, high = ordered[0][0], ordered[-1][0]

    document = {
        "realm_multipliers": dict(manager.realm_multipliers),
        "monsters": {m.id: m.to_dict() for m in manager.get_all_monsters()},
    }
    path = _write_json(tmp_path, "copy.json", document)
    copied = MonsterStatManager(path)

    for monster_id in manager.get_monster_ids()[:8]:
        assert copied.scaled_for_context(monster_id, high) == (
            manager.scaled_for_context(monster_id, high)
        )
        assert copied.scaled_for_context(monster_id, low) == (
            manager.scaled_for_context(monster_id, low)
        )


def test_control_the_determinism_check_can_see_a_change(manager):
    """Negative control: the equality above must be able to fail.

    Without this, a determinism test whose comparison was accidentally
    comparing an empty dict to an empty dict would pass forever.
    """
    monster = manager.get_all_monsters()[0]
    target = sorted(
        manager.realm_multipliers.items(), key=lambda kv: kv[1]
    )[-1][0]
    genuine = manager.scaled_for_context(monster.id, target)
    altered = dict(genuine)
    altered["hp"] = genuine["hp"] + 1
    assert altered != genuine, (
        "the comparison used above cannot tell two different lines apart"
    )
    assert genuine == manager.scaled_for_context(monster.id, target)


# --------------------------------------------------------------------------
# 9. Non-finite values are refused at BOTH entry points
#
# The sibling ``test_monster_stats_nonfinite.py`` owns the per-defect crash
# history. What is owned HERE is the contract statement: a non-finite stat is
# refused at load, a non-finite multiplier is refused at scale, and neither
# reaches a caller as a number.
# --------------------------------------------------------------------------
NON_FINITE_TOKENS = ("Infinity", "-Infinity", "NaN")


@pytest.mark.parametrize("token", NON_FINITE_TOKENS)
def test_a_non_finite_token_in_any_stat_is_refused_at_load(tmp_path, token):
    """A hand-written ``Infinity``/``NaN`` in monsters.json must not load.

    Written as raw text rather than through ``json.dumps`` so the poison is
    exactly what a hand-edited data file carries.
    """
    for field in ("hp", "atk", "def", "mp", "spd", "spirit_stone", "xp"):
        row = json.dumps(_valid_row("inf_beast"))
        row = row.replace(f'"{field}": {_valid_row()[field]}', f'"{field}": {token}')
        path = tmp_path / f"{token}_{field}.json"
        path.write_text(
            '{"realm_multipliers": {"mortal": 1.0}, "monsters": '
            f'{{"inf_beast": {row}}}}}',
            encoding="utf-8",
        )
        with pytest.raises(MonsterStatError) as caught:
            MonsterStatManager(str(path))
        message = str(caught.value)
        assert "inf_beast" in message, (
            f"{token} in {field} was reported without the row id: {message!r}"
        )
        assert field in message, (
            f"{token} in {field} was reported without naming the field: "
            f"{message!r}"
        )


@pytest.mark.parametrize("token", NON_FINITE_TOKENS)
def test_a_non_finite_token_in_the_ladder_refuses_the_scale(tmp_path, token):
    """A poisoned multiplier must not escape as a raw OverflowError/ValueError."""
    row = json.dumps(_valid_row("ladder_beast"))
    path = tmp_path / f"ladder_{token}.json"
    path.write_text(
        '{"realm_multipliers": {"mortal": 1.0, "high": ' + token
        + '}, "monsters": {"ladder_beast": ' + row + "}}",
        encoding="utf-8",
    )
    manager = MonsterStatManager(str(path))
    out = manager.scaled_for_context("ladder_beast", "high")
    assert out["scaling_refused"] is True, (
        f"a {token} multiplier produced a scaled line instead of refusing"
    )
    assert out["hp"] == 100 and out["atk"] == 10, (
        "a refused scale still altered the authored stats"
    )
    assert out["scaled_onto_realm"] == "high"


def test_a_non_finite_multiplier_in_the_BASE_realm_also_refuses(tmp_path):
    """The base side is read too, so it must be guarded the same way.

    ``scaled_for_context`` pulls BOTH multipliers before it divides. A poisoned
    base with a good target used to be the asymmetric case: the ratio was
    computed against a nonsense denominator.
    """
    base_row = _valid_row("base_beast")
    base_row["realm"] = "high"
    row = json.dumps(base_row)
    path = tmp_path / "base_inf.json"
    path.write_text(
        '{"realm_multipliers": {"mortal": 1.0, "high": Infinity}, "monsters": '
        f'{{"base_beast": {row}}}}}',
        encoding="utf-8",
    )
    manager = MonsterStatManager(str(path))
    out = manager.scaled_for_context("base_beast", "mortal")
    assert out["scaling_refused"] is True, (
        "an Infinity in the BASE realm was treated as a usable denominator"
    )
    assert out["hp"] == 100


def test_no_scaled_line_the_shipped_table_can_produce_is_non_finite(manager):
    """Every line the shipped table can serve must be JSON-encodable.

    FastAPI's encoder turns a bare ``inf``/``nan`` into a 500 that names no
    field, so this sweeps the real cross-product of monsters x realms rather
    than trusting one hand-picked case.
    """
    import math

    checked = 0
    for monster in manager.get_all_monsters():
        for target in list(manager.realm_multipliers) + ["not_a_real_realm"]:
            out = manager.scaled_for_context(monster.id, target)
            for field in ("hp", "atk", "def", "mp", "spd", "spirit_stone", "xp"):
                value = out[field]
                assert isinstance(value, int) and not isinstance(value, bool), (
                    f"{monster.id}@{target}.{field} is {value!r}, not an int"
                )
                assert math.isfinite(value), (
                    f"{monster.id}@{target}.{field} is not finite: {value}"
                )
            # Must survive a real JSON encode, not just isfinite().
            json.dumps(out, allow_nan=False)
            checked += 1
    assert checked == len(manager.get_all_monsters()) * (
        len(manager.realm_multipliers) + 1
    ), "the sweep did not cover every monster/realm pair"


# --------------------------------------------------------------------------
# 10. Stat boundaries are enforced
#
# The floor is the boundary this module owns: a stat that cannot be fought
# (0) and a stat that cannot be represented (overflow) both land on 1.
# --------------------------------------------------------------------------
def test_the_floor_covers_every_scaled_field_not_just_hp(manager, tmp_path):
    """A planted tiny row must be floored on ALL five fields.

    The shipped table's smallest stat never reaches the floor, so the live
    table cannot see a field dropped out of the scale loop -- ``def`` in
    particular, whose data-file key and attribute name differ.
    """
    tiny = {
        **_valid_row("floor_beast"),
        "realm": "high", "hp": 2, "atk": 2, "mp": 2, "spd": 2, "def": 2,
    }
    path = _write_json(
        tmp_path, "floor.json",
        {"realm_multipliers": {"low": 1.0, "high": 8.0},
         "monsters": {"floor_beast": tiny}},
    )
    manager = MonsterStatManager(path)
    out = manager.scaled_for_context("floor_beast", "low")
    assert out["scaling_refused"] is False, (
        "the control never reached the scaling path; the scale was refused"
    )
    # Sanity: plain truncation would have produced 0 on every field.
    assert int(2 * (1.0 / 8.0)) == 0, "the planted case no longer discriminates"
    for field in ("hp", "atk", "def", "mp", "spd"):
        assert out[field] == 1, (
            f"{field} landed on {out[field]}; plain truncation gives 0, so the "
            "floor is not being applied to this field"
        )


def test_the_floor_applies_on_a_long_downward_climb(manager, tmp_path):
    """A multi-step descent must not compound into a zero stat."""
    ladder = {"r0": 1000.0, "r1": 100.0, "r2": 10.0, "r3": 1.0, "r4": 0.001}
    tiny = {
        **_valid_row("climb_beast"),
        "realm": "r0", "hp": 5, "atk": 3, "mp": 4, "spd": 2, "def": 1,
    }
    path = _write_json(
        tmp_path, "climb.json",
        {"realm_multipliers": ladder, "monsters": {"climb_beast": tiny}},
    )
    manager = MonsterStatManager(path)
    for target in ("r1", "r2", "r3", "r4"):
        out = manager.scaled_for_context("climb_beast", target)
        assert out["scaling_refused"] is False
        for field in ("hp", "atk", "def", "mp", "spd"):
            assert out[field] >= 1, (
                f"climbing {target}: {field} fell to {out[field]}"
            )


def test_an_overflowing_product_floors_instead_of_crashing(tmp_path):
    """Two finite numbers whose product overflows must not raise.

    A finiteness test on the INPUTS alone does not cover this: both operands
    pass it. The answer is the same floor, so the line stays encodable.
    """
    big = {
        **_valid_row("huge_beast"),
        "realm": "low",
        "hp": 10 ** 300, "atk": 10 ** 300, "mp": 10 ** 300, "spd": 10 ** 300,
        "def": 10 ** 300,
    }
    path = _write_json(
        tmp_path, "overflow.json",
        {"realm_multipliers": {"low": 1.0, "high": 1e300},
         "monsters": {"huge_beast": big}},
    )
    manager = MonsterStatManager(path)
    out = manager.scaled_for_context("huge_beast", "high")
    assert out["scaling_refused"] is False, (
        "a finite-but-huge scale was refused instead of floored"
    )
    for field in ("hp", "atk", "def", "mp", "spd"):
        assert out[field] == 1, (
            f"an unrepresentable {field} came back as {out[field]}, not the floor"
        )
    json.dumps(out, allow_nan=False)


def test_a_zero_or_negative_multiplier_is_refused(tmp_path):
    """A ladder entry that cannot divide is refused, not used.

    Zero would be a division by zero and a negative would flip the curve, so
    both are refused the same way a missing realm is.
    """
    for bad in (0, 0.0, -1, -2.5):
        row = json.dumps(_valid_row("sign_beast"))
        path = tmp_path / f"sign_{bad}.json"
        path.write_text(
            '{"realm_multipliers": {"mortal": 1.0, "bad": ' + str(bad)
            + '}, "monsters": {"sign_beast": ' + row + "}}",
            encoding="utf-8",
        )
        manager = MonsterStatManager(str(path))
        out = manager.scaled_for_context("sign_beast", "bad")
        assert out["scaling_refused"] is True, (
            f"a multiplier of {bad} was used as a scale factor"
        )
        assert out["hp"] == 100 and out["atk"] == 10, (
            f"a multiplier of {bad} still altered the authored stats"
        )


def test_numeric_text_in_the_ladder_is_coerced_not_refused(tmp_path):
    """A ladder written as quoted numbers still scales.

    The module coerces numeric text rather than refusing it, matching the
    house float helper: ``float("8.0")`` is 8.0, so a hand-edited ladder still
    scales instead of crashing on a division. Only text with NO numeric value
    is refused -- see the next test. Asserting coercion here keeps a later
    "refuse everything" change from looking like a free win.
    """
    row = json.dumps(_valid_row("text_beast"))
    path = tmp_path / "text_mult.json"
    path.write_text(
        '{"realm_multipliers": {"mortal": 1.0, "high": "8.0"}, "monsters": '
        f'{{"text_beast": {row}}}}}',
        encoding="utf-8",
    )
    manager = MonsterStatManager(str(path))
    out = manager.scaled_for_context("text_beast", "high")
    assert out["scaling_refused"] is False, (
        'numeric text "8.0" was refused instead of coerced'
    )
    assert out["hp"] == 800, (
        f'numeric text "8.0" did not scale by 8.0 (got {out["hp"]})'
    )


@pytest.mark.parametrize(
    "token", ['"not_a_number"', '"nan"', '"inf"', '""', '"1e400"']
)
def test_text_with_no_usable_number_in_the_ladder_is_refused(tmp_path, token):
    """Spelled non-numbers, including the words "nan"/"inf", are refused.

    ``float("nan")`` and ``float("inf")`` parse WITHOUT raising, so quoted text
    is a second route to a non-finite multiplier and needs the same guard as
    the bare tokens. ``1e400`` overflows to infinity for the same reason.
    """
    row = json.dumps(_valid_row("text_beast"))
    path = tmp_path / f"badtext_{token.strip(chr(34))}.json"
    path.write_text(
        '{"realm_multipliers": {"mortal": 1.0, "high": ' + token
        + '}, "monsters": {"text_beast": ' + row + "}}",
        encoding="utf-8",
    )
    manager = MonsterStatManager(str(path))
    out = manager.scaled_for_context("text_beast", "high")
    assert out["scaling_refused"] is True, (
        f"ladder entry {token} was used as arithmetic: {out}"
    )
    assert out["hp"] == 100, "a refused scale still altered the authored stats"


def test_a_null_multiplier_is_refused(tmp_path):
    """``null`` is not zero: it must be refused, not divided by."""
    row = json.dumps(_valid_row("null_beast"))
    path = tmp_path / "null_mult.json"
    path.write_text(
        '{"realm_multipliers": {"mortal": 1.0, "high": null}, "monsters": '
        f'{{"null_beast": {row}}}}}',
        encoding="utf-8",
    )
    manager = MonsterStatManager(str(path))
    out = manager.scaled_for_context("null_beast", "high")
    assert out["scaling_refused"] is True, "a null multiplier was used as arithmetic"
    assert out["hp"] == 100


def test_a_refused_scale_is_distinguishable_from_a_successful_one(manager):
    """The marker must be present AND false on a real scale.

    A caller cannot tell "scaled onto X" from "refused" if the key is missing,
    and cannot tell them apart if every answer says ``True``.
    """
    ladder = manager.realm_multipliers
    successful = 0
    for monster in manager.get_all_monsters():
        if monster.realm not in ladder:
            continue
        for target in ladder:
            out = manager.scaled_for_context(monster.id, target)
            assert "scaling_refused" in out
            if out["scaling_refused"] is False:
                successful += 1
    assert successful > 0, (
        "no shipped monster/realm pair scaled successfully, so the refused "
        "branch is the only one exercised"
    )

    refused = manager.scaled_for_context(
        manager.get_all_monsters()[0].id, "not_a_real_realm"
    )
    assert refused["scaling_refused"] is True
    assert refused["scaled_onto_realm"] == "not_a_real_realm"


def test_control_the_refusal_marker_is_read_not_defaulted(manager):
    """Negative control: the marker assertions above must be able to fail."""
    monster = manager.get_all_monsters()[0]
    out = manager.scaled_for_context(monster.id, "not_a_real_realm")
    assert out["scaling_refused"] is True
    # A dict lookup with a default would answer for a key that is absent.
    assert "scaling_refused" in out, (
        "the marker is absent and only the default made it look present"
    )
    forged = dict(out)
    forged["scaling_refused"] = False
    assert forged != out, "flipping the marker did not change the dict"


def test_control_the_floor_check_can_see_a_zero(manager, tmp_path):
    """Negative control: the >= 1 assertions must reject a 0.

    Guards against the floor tests passing because they compare against a
    value that can never be 0 for an unrelated reason.
    """
    tiny = {
        **_valid_row("ctl_beast"),
        "realm": "high", "hp": 2, "atk": 2, "mp": 2, "spd": 2, "def": 2,
    }
    path = _write_json(
        tmp_path, "ctl.json",
        {"realm_multipliers": {"low": 1.0, "high": 8.0},
         "monsters": {"ctl_beast": tiny}},
    )
    assert MonsterStatManager(path).scaled_for_context("ctl_beast", "low")["hp"] == 1
    # The unfloored value the module would have produced without the guard.
    assert int(2 * (1.0 / 8.0)) == 0, "the control case no longer discriminates"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _valid_row(monster_id: str = "probe_beast") -> dict:
    return {
        "name": monster_id,
        "realm": "mortal",
        "element": "none",
        "rarity": "common",
        "stage": "early",
        "tier": "normal",
        "is_boss": False,
        "hp": 100,
        "atk": 10,
        "def": 5,
        "mp": 50,
        "spd": 10,
        "spirit_stone": 5,
        "xp": 10,
    }


def _write_json(tmp_path: Path, name: str, document: dict) -> str:
    target = tmp_path / name
    target.write_text(json.dumps(document), encoding="utf-8")
    return str(target)
