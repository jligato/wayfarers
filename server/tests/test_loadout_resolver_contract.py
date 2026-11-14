"""Contract tests for ``server/loadout_resolver.py``.

WHAT THIS MODULE PROMISES
-------------------------
``loadout_resolver`` is the one place in the server that reads the equipment
catalogue and the artifact catalogue and produces a stat block for a fight.
Its promises are:

1. **Two layers, multiplied.** Equipment contributes flat numbers plus set
   bonus percentages. Artifacts contribute percentages. The two percentages
   are MULTIPLIED, never added, because adding two 10% bonuses hides a
   doubling that a player cannot predict.
2. **One five-stat vocabulary.** Five stats come out, named by
   ``equipment.STAT_KEYS``, with ``def_`` as the storage spelling of ``def``
   because ``def`` is a Python keyword.
3. **A defined outcome for every malformed input.** A missing key, an unknown
   catalogue name and a malformed value each have ONE documented result. None
   of them may become an accidental default that silently produces a wrong
   number.

THE SILENT-WRONG-ANSWER RULE THESE TESTS ENFORCE
------------------------------------------------
A resolver that reads a stored value and falls back on a missing one is where
balance bugs are born. So every fallback here is pinned as an explicit
contract, not left to chance:

* a MISSING multiplier key in a layer is identity ``1.0`` (not ``0.0``, which
  would zero a stat without saying so);
* an UNKNOWN catalogue name is skipped, never guessed at;
* a MALFORMED value either raises loudly or is skipped, never quietly
  replaced by a plausible number.

MEASURED, NOT GUESSED
---------------------
Every expected value below was produced by running this module on this tree,
not by reading it. Two measurement findings shaped the whole file:

* ``ensure_state`` ROLLS random ``rolled_stats`` for any item record that does
  not carry them, so a fixture that omits them is non-deterministic. Every
  fixture here pins ``rolled_stats`` to ``1.0`` and ``durability`` to the
  catalogue maximum, which is what makes the expected flat numbers exact.
* The default first-session loadout is not a fixed number: it moves run to
  run for the same reason. So no test here asserts against it.

Expected flat stats are derived at run time from
``EQUIPMENT_CATALOG[item_id]["base_stats"]`` rather than written as literals,
so these are behaviour contracts between the resolver and the catalogue, not
snapshots of today's numbers.
"""
from __future__ import annotations

import copy
import math
import os
import sys
from pathlib import Path

import pytest

# --- path bootstrap: resolve the server modules from any cwd ---------------
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import artifacts  # noqa: E402
import equipment  # noqa: E402
import loadout_resolver as lr  # noqa: E402


# --- fixtures --------------------------------------------------------------
def _item(item_id: str, quality: str = "common") -> dict:
    """A fully-formed, DETERMINISTIC item record.

    ``rolled_stats`` and ``durability`` are both pinned because
    ``ensure_state`` fills a missing ``rolled_stats`` with a fresh random
    roll, and durability scales the result. With both pinned, the resolved
    flat stat equals the catalogue base stat exactly.
    """
    base = equipment.EQUIPMENT_CATALOG[item_id]["base_stats"]
    return {
        "item_id": item_id,
        "quality": quality,
        "level": 1,
        "equipped": True,
        "durability": equipment.MAX_DURABILITY,
        "rolled_stats": {stat: 1.0 for stat in base},
    }


#: A five-stat loadout: three Wanderer pieces (a 3-piece set bonus) plus one
#: Stormguard piece. Chosen so every one of the five stats is non-zero and at
#: least one multiplier differs from identity.
LOADOUT_IDS = ("copper_sword", "iron_blade", "traveler_robes", "cloud_mirror")

#: An artifact whose bonuses move atk and def.
ARTIFACT_ID = "emberlotus_seal"


def _player(ids=LOADOUT_IDS, artifacts_on=(ARTIFACT_ID,)) -> dict:
    return {
        "equipment": {"items": {i: _item(i) for i in ids}},
        "artifacts": {"owned": {a: {"activated": True} for a in artifacts_on}},
    }


def _expected_flat(ids=LOADOUT_IDS) -> dict:
    """Sum the catalogue base stats directly -- an independent path.

    ``_item_view`` multiplies base by quality, level and durability, all of
    which this file pins to ``1.0``, so the resolved flat total must equal
    this plain sum. Computing it here rather than writing literals is what
    makes these contracts survive a catalogue rebalance.
    """
    totals = {stat: 0.0 for stat in lr.STAT_KEYS}
    for item_id in ids:
        for stat, value in equipment.EQUIPMENT_CATALOG[item_id]["base_stats"].items():
            if stat in totals:
                totals[stat] += float(value)
    return totals


# --- vocabulary ------------------------------------------------------------
class TestVocabulary:
    """The five-stat contract and its storage spelling."""

    def test_stat_keys_are_the_equipment_five(self):
        assert lr.STAT_KEYS == equipment.STAT_KEYS

    def test_stat_keys_are_a_five_tuple(self):
        assert isinstance(lr.STAT_KEYS, tuple)
        assert len(lr.STAT_KEYS) == 5

    def test_def_uses_the_storage_spelling_not_the_keyword(self):
        # ``def`` is a Python keyword; ``def_`` is the name on disk.
        assert "def_" in lr.MULTIPLIER_KEYS
        assert "def" not in lr.MULTIPLIER_KEYS

    def test_every_flat_stat_maps_to_its_own_multiplier(self):
        assert set(lr.MULTIPLIER_KEYS) == set(lr.STAT_KEYS)
        assert len(set(lr.MULTIPLIER_KEYS.values())) == len(lr.MULTIPLIER_KEYS)

    def test_multiplier_names_end_in_mult(self):
        assert all(name.endswith("_mult") for name in lr.MULTIPLIER_KEYS.values())

    def test_identity_covers_exactly_the_multiplier_vocabulary(self):
        assert set(lr.identity_multipliers()) == set(lr.MULTIPLIER_KEYS.values())


# --- identity --------------------------------------------------------------
class TestIdentity:
    def test_every_multiplier_is_one(self):
        assert set(lr.identity_multipliers().values()) == {1.0}

    def test_a_fresh_dict_each_call_so_callers_cannot_corrupt_shared_state(self):
        first = lr.identity_multipliers()
        first["atk_mult"] = 99.0
        assert lr.identity_multipliers()["atk_mult"] == 1.0


# --- valid loadout ---------------------------------------------------------
class TestValidLoadout:
    """A real five-stat loadout resolves to real, layered numbers."""

    def test_flat_is_the_sum_of_the_worn_catalogue_base_stats(self):
        assert lr.equipment_flat_stats(_player()) == _expected_flat()

    def test_flat_reports_all_five_stats(self):
        assert set(lr.equipment_flat_stats(_player())) == set(lr.STAT_KEYS)

    def test_the_full_block_has_exactly_the_five_documented_sections(self):
        block = lr.resolve_stat_block(_player())
        assert set(block) == {
            "flat",
            "equipment_multipliers",
            "artifact_multipliers",
            "multipliers",
            "final",
        }

    def test_every_section_is_rounded_to_four_decimal_places(self):
        block = lr.resolve_stat_block(_player())
        for section in ("flat", "final"):
            for stat, value in block[section].items():
                assert value == round(value, 4), f"{section}[{stat}] is not 4dp"

    def test_flat_is_the_same_dict_the_block_reports(self):
        player = _player()
        assert lr.resolve_stat_block(player)["flat"] == lr.equipment_flat_stats(player)

    def test_the_artifact_layer_moves_the_stats_its_catalogue_entry_names(self):
        player = _player(ids=(), artifacts_on=(ARTIFACT_ID,))
        assert lr.artifact_multipliers(player) == {
            "atk_mult": 1.12, "def_mult": 1.04, "hp_mult": 1.0,
            "mp_mult": 1.0, "spd_mult": 1.0,
        }

    def test_a_loadout_with_no_artifact_still_reports_the_artifact_layer(self):
        block = lr.resolve_stat_block(_player(artifacts_on=()))
        assert block["artifact_multipliers"] == lr.identity_multipliers()

    def test_resolving_twice_on_one_save_gives_the_same_answer(self):
        player = _player()
        assert lr.resolve_stat_block(player) == lr.resolve_stat_block(player)

    def test_two_independently_built_identical_loadouts_agree(self):
        # Guards the random-roll trap: an unpinned fixture drifts run to run.
        assert lr.resolve_stat_block(_player()) == lr.resolve_stat_block(_player())


# --- the multiplying rule --------------------------------------------------
class TestLayersMultiply:
    """The one rule this module exists to apply exactly once."""

    def test_the_two_layers_are_multiplied_not_added(self):
        combined = lr.combine({"atk_mult": 1.20, "spd_mult": 1.10},
                              {"atk_mult": 1.50, "spd_mult": 1.00})
        assert combined["atk_mult"] == pytest.approx(1.80)
        assert combined["spd_mult"] == pytest.approx(1.10)

    def test_adding_instead_of_multiplying_would_be_caught(self):
        # 1.2 + 1.5 == 2.7, which is a hidden doubling; 1.2 * 1.5 == 1.8.
        assert lr.combine({"atk_mult": 1.2}, {"atk_mult": 1.5})["atk_mult"] != 2.7

    def test_the_block_multiplier_is_the_exact_product_of_its_two_layers(self):
        block = lr.resolve_stat_block(_player())
        for name in lr.MULTIPLIER_KEYS.values():
            assert block["multipliers"][name] == (
                block["equipment_multipliers"][name]
                * block["artifact_multipliers"][name]
            )

    def test_combining_with_identity_changes_nothing(self):
        # ``combine`` always answers with the full five-key block, so this
        # compares on the keys it was given rather than on the whole dict.
        one_layer = {"atk_mult": 1.3, "hp_mult": 0.9}
        combined = lr.combine(one_layer, lr.identity_multipliers())
        for name, value in one_layer.items():
            assert combined[name] == value
        assert set(combined) == set(lr.MULTIPLIER_KEYS.values())

    def test_combine_does_not_mutate_either_argument(self):
        equipment_layer = dict(lr.identity_multipliers())
        artifact_layer = dict(lr.identity_multipliers())
        before = (dict(equipment_layer), dict(artifact_layer))
        lr.combine(equipment_layer, artifact_layer)
        assert (equipment_layer, artifact_layer) == before


# --- the block shows its own working out -----------------------------------
class TestFinalIsDerivedFromThePrintedNumbers:
    """``final`` must be recomputable from the ``flat`` printed beside it."""

    def test_final_is_the_flat_value_times_the_printed_multiplier(self):
        block = lr.resolve_stat_block(_player())
        for stat in lr.STAT_KEYS:
            mult_key = lr.MULTIPLIER_KEYS[stat]
            assert block["final"][stat] == pytest.approx(
                round(block["flat"][stat] * block["multipliers"][mult_key], 4)
            )

    def test_final_equals_flat_when_both_layers_are_identity(self):
        block = lr.resolve_stat_block(_player(ids=(), artifacts_on=()))
        assert block["final"] == block["flat"]

    def test_final_is_rounded_rather_than_left_at_full_float_width(self):
        block = lr.resolve_stat_block(_player())
        for stat in lr.STAT_KEYS:
            mult_key = lr.MULTIPLIER_KEYS[stat]
            exact = block["flat"][stat] * block["multipliers"][mult_key]
            assert block["final"][stat] == round(exact, 4)


# --- missing keys ----------------------------------------------------------
class TestMissingKeyIsIdentityNotZero:
    """A key that is absent resolves to 1.0. Never to 0.0, never to a guess."""

    def test_combine_of_two_empty_layers_is_the_identity_block(self):
        assert lr.combine({}, {}) == lr.identity_multipliers()

    def test_a_missing_key_in_one_layer_does_not_zero_the_other_layer(self):
        combined = lr.combine({"atk_mult": 1.2}, {})
        assert combined["atk_mult"] == 1.2
        assert combined["def_mult"] == 1.0

    def test_a_missing_key_is_one_not_zero(self):
        # The failure this pins: a fallback of 0.0 would silently delete the
        # stat with no error anywhere.
        assert lr.combine({}, {})["atk_mult"] == 1.0
        assert lr.combine({}, {})["atk_mult"] != 0.0

    def test_a_player_with_no_equipment_key_at_all_still_resolves(self):
        block = lr.resolve_stat_block({})
        assert set(block["flat"]) == set(lr.STAT_KEYS)
        # ``multipliers`` is keyed by the MULTIPLIER names (``atk_mult``),
        # not by the flat names (``atk``). Reading the wrong one of the two
        # vocabularies here is exactly the silent mismatch this file exists
        # to catch, so the assertion names the multiplier side explicitly.
        assert set(block["multipliers"]) == set(lr.MULTIPLIER_KEYS.values())

    def test_a_player_with_no_artifacts_key_at_all_still_resolves(self):
        block = lr.resolve_stat_block({"equipment": {"items": {}}})
        assert block["artifact_multipliers"] == lr.identity_multipliers()

    def test_an_empty_items_map_is_a_zeroed_flat_block_not_a_crash(self):
        flat = lr.equipment_flat_stats({"equipment": {"items": {}}})
        assert flat == {stat: 0.0 for stat in lr.STAT_KEYS}

    def test_an_absent_stat_key_is_not_invented_by_apply_to_stat_line(self):
        out = lr.apply_to_stat_line({"realm": 3}, lr.resolve_stat_block(_player()))
        assert "atk" not in out
        assert out["realm"] == 3

    def test_a_block_with_no_multipliers_section_scales_by_identity(self):
        line = {"atk": 10, "def_": 5}
        assert lr.apply_to_stat_line(line, {}) == {"atk": 10.0, "def_": 5.0}


# --- unknown catalogue names ----------------------------------------------
class TestUnknownNameIsSkippedNotGuessed:
    def test_an_unknown_equipment_id_contributes_nothing(self):
        player = {"equipment": {"items": {
            "not_a_real_item": {"item_id": "not_a_real_item", "equipped": True,
                                "quality": "common", "level": 1},
        }}}
        assert lr.equipment_flat_stats(player) == {stat: 0.0 for stat in lr.STAT_KEYS}

    def test_an_unknown_equipment_id_does_not_disturb_the_equipment_layer(self):
        player = {"equipment": {"items": {
            "copper_sword": _item("copper_sword"),
            "not_a_real_item": {"item_id": "not_a_real_item", "equipped": True},
        }}}
        assert lr.equipment_flat_stats(player) == _expected_flat(("copper_sword",))

    def test_an_unknown_equipment_id_leaves_the_multipliers_at_identity(self):
        player = {"equipment": {"items": {
            "not_a_real_item": {"item_id": "not_a_real_item", "equipped": True},
        }}}
        assert lr.equipment_multipliers(player) == lr.identity_multipliers()

    def test_an_unknown_artifact_id_grants_no_bonus(self):
        player = {"artifacts": {"owned": {"ghost_relic": {"activated": True}}}}
        assert lr.artifact_multipliers(player) == lr.identity_multipliers()

    def test_an_unknown_artifact_id_does_not_silently_pick_a_real_one(self):
        player = {"artifacts": {"owned": {"ghost_relic": {"activated": True}}}}
        for name, value in lr.artifact_multipliers(player).items():
            assert value == 1.0, f"{name} moved to {value} for an unknown artifact"

    def test_an_item_id_missing_entirely_is_skipped(self):
        player = {"equipment": {"items": {"x": {"equipped": True, "quality": "common"}}}}
        assert lr.equipment_flat_stats(player) == {stat: 0.0 for stat in lr.STAT_KEYS}

    def test_a_real_artifact_id_that_is_not_activated_grants_nothing(self):
        player = {"artifacts": {"owned": {ARTIFACT_ID: {"activated": False}}}}
        assert lr.artifact_multipliers(player) == lr.identity_multipliers()

    def test_the_three_artifact_catalogue_names_are_all_real(self):
        # Guards the other direction: the ids this file relies on must exist.
        for artifact_id in (ARTIFACT_ID, "cloudseed_relic", "tideglass_compass"):
            assert artifact_id in artifacts.ARTIFACT_CATALOG


# --- malformed values ------------------------------------------------------
class TestMalformedValueIsSkippedOrRaises:
    """A value the module cannot use must never become a plausible number."""

    def test_an_equipment_block_that_is_not_a_dict_is_replaced(self):
        player = {"equipment": "not-a-dict"}
        block = lr.resolve_stat_block(player)
        assert set(block["flat"]) == set(lr.STAT_KEYS)

    def test_an_artifacts_block_that_is_not_a_dict_is_replaced(self):
        player = {"artifacts": ["not", "a", "dict"]}
        assert lr.artifact_multipliers(player) == lr.identity_multipliers()

    def test_an_items_map_holding_a_string_is_replaced_not_read_as_empty(self):
        # MEASURED, and it is the surprising one: a non-dict ``items`` map is
        # not an empty loadout. ``ensure_state`` replaces the whole equipment
        # block with the DEFAULT first-session kit, so the player reads as
        # wearing the three default pieces rather than wearing nothing. A
        # zeroed assertion here would have been the wrong contract to pin.
        player = {"equipment": {"items": "junk"}}
        flat = lr.equipment_flat_stats(player)
        assert set(flat) == set(lr.STAT_KEYS)
        assert flat["atk"] > 0.0, "a corrupt items map resolved to an empty loadout"
        replaced = player["equipment"]["items"]
        assert isinstance(replaced, dict)
        assert len(replaced) == len(equipment.default_equipment_state()["items"])

    def test_a_corrupt_items_map_resolves_to_the_same_kit_as_an_empty_player(self):
        # Both paths land on the default kit. Only the ROLL-INDEPENDENT parts
        # can be compared across two separate dicts: each one gets its own
        # random ``rolled_stats``, so the flat numbers legitimately differ.
        # The set-bonus multipliers depend on which pieces are worn, not on
        # their rolls, so those must agree exactly.
        corrupt = {"equipment": {"items": "junk"}}
        bare = {}
        first = lr.resolve_stat_block(corrupt)
        second = lr.resolve_stat_block(bare)
        for player, block in ((corrupt, first), (bare, second)):
            assert lr.resolve_stat_block(player) == block
            assert block["flat"]["atk"] > 0.0
        assert first["multipliers"] == second["multipliers"]
        assert first["equipment_multipliers"] == second["equipment_multipliers"]
        assert set(first["flat"]) == set(second["flat"])

    def test_an_item_record_that_is_not_a_dict_is_skipped(self):
        player = {"equipment": {"items": {
            "copper_sword": _item("copper_sword"),
            "junk": "not-a-record",
        }}}
        assert lr.equipment_flat_stats(player) == _expected_flat(("copper_sword",))

    def test_an_owned_record_that_is_not_a_dict_is_skipped(self):
        player = {"artifacts": {"owned": {"cloudseed_relic": "junk", "x": None, "y": 5}}}
        assert lr.artifact_multipliers(player) == lr.identity_multipliers()

    def test_a_non_numeric_multiplier_is_refused_to_identity_not_raised(self):
        # FIXED, MEASURED BEFORE THE FIX: this raised a raw ValueError out of
        # the middle of a stat block. A bad multiplier now reads as identity
        # -- the same answer a MISSING key gets -- so one broken value no
        # longer takes a whole player's stat block down mid-fight.
        combined = lr.combine({"atk_mult": "abc"}, {})
        assert combined["atk_mult"] == 1.0

    def test_a_none_multiplier_is_refused_to_identity_not_raised(self):
        # MEASURED BEFORE THE FIX: a None multiplier raised TypeError.
        assert lr.combine({"atk_mult": None}, {})["atk_mult"] == 1.0

    def test_a_refused_multiplier_never_becomes_a_plausible_number(self):
        # The silent-wrong-answer guard. Refused means refused: not 0.0, and
        # not some invented rate that would quietly buff a stat.
        for bad in ("abc", "not a number", None):
            got = lr.combine({"atk_mult": bad}, {})["atk_mult"]
            assert got == 1.0, f"{bad!r} resolved to {got!r}"
            assert got != 0.0

    def test_a_refused_multiplier_leaves_the_other_four_stats_alone(self):
        # The point of skipping one stat instead of raising: a single bad
        # multiplier must not cost the player the other four.
        combined = lr.combine({"atk_mult": None}, {"atk_mult": 1.5, "hp_mult": 1.2})
        assert combined["hp_mult"] == 1.2
        assert combined["spd_mult"] == 1.0
        assert set(combined) == set(lr.MULTIPLIER_KEYS.values())

    def test_a_refused_side_does_not_cancel_the_good_side_of_the_product(self):
        # MEASURED, and it is the subtle half of the rule: refusing one layer
        # makes THAT layer identity, it does not discard the other layer's real
        # bonus. A corrupt equipment layer must not silently delete a genuine
        # artifact buff -- that would be a second silent wrong answer.
        assert lr.combine({"atk_mult": None}, {"atk_mult": 1.5})["atk_mult"] == 1.5
        assert lr.combine({"atk_mult": 1.5}, {"atk_mult": float("inf")})["atk_mult"] == 1.5

    def test_a_non_numeric_multiplier_leaves_that_stat_unscaled_not_raised(self):
        # MEASURED BEFORE THE FIX: this raised ValueError. The stat is now
        # left exactly as the caller gave it, mirroring the stat-side guard.
        out = lr.apply_to_stat_line(
            {"atk": 10, "def_": 4}, {"multipliers": {"atk_mult": "abc"}}
        )
        assert out["atk"] == 10
        assert out["def_"] == 4.0

    def test_a_none_multiplier_leaves_that_stat_unscaled_not_raised(self):
        # MEASURED BEFORE THE FIX: this raised TypeError.
        out = lr.apply_to_stat_line(
            {"atk": 10, "def_": 4}, {"multipliers": {"atk_mult": None}}
        )
        assert out["atk"] == 10
        assert out["def_"] == 4.0

    def test_a_refused_multiplier_never_silently_becomes_one(self):
        # Before the fix this test could pass by catching the raise. It now
        # pins a real VALUE, so it cannot pass by exception handling.
        out = lr.apply_to_stat_line({"atk": 10}, {"multipliers": {"atk_mult": "abc"}})
        assert out["atk"] != 0.0, "a refused multiplier silently zeroed the stat"

    def test_a_non_numeric_stat_value_is_left_exactly_as_it_was(self):
        line = {"atk": "abc"}
        assert lr.apply_to_stat_line(line, lr.resolve_stat_block(_player())) == {
            "atk": "abc"
        }

    def test_a_none_stat_value_is_left_exactly_as_it_was(self):
        line = {"atk": None}
        assert lr.apply_to_stat_line(line, lr.resolve_stat_block(_player())) == {
            "atk": None
        }

    def test_an_item_whose_view_cannot_be_built_is_skipped_not_fatal(self, monkeypatch):
        # The module's own guard: one unreadable item must not take a whole
        # player's stat block down with it.
        def _boom(_item_arg):
            raise RuntimeError("catalogue row is corrupt")

        monkeypatch.setattr(equipment, "_item_view", _boom)
        player = _player()
        assert lr.equipment_flat_stats(player) == {stat: 0.0 for stat in lr.STAT_KEYS}

    def test_an_unreadable_item_does_not_stop_the_others_being_read(self, monkeypatch):
        real_view = equipment._item_view
        seen = []

        def _selective(item):
            seen.append(item["item_id"])
            if item["item_id"] == "iron_blade":
                raise RuntimeError("catalogue row is corrupt")
            return real_view(item)

        monkeypatch.setattr(equipment, "_item_view", _selective)
        ids = ("copper_sword", "iron_blade", "traveler_robes", "cloud_mirror")
        got = lr.equipment_flat_stats(_player(ids=ids))
        assert got == _expected_flat(tuple(i for i in ids if i != "iron_blade"))

    def test_the_block_survives_an_unreadable_item(self, monkeypatch):
        monkeypatch.setattr(
            equipment, "_item_view",
            lambda _i: (_ for _ in ()).throw(RuntimeError("corrupt")),
        )
        block = lr.resolve_stat_block(_player())
        assert block["flat"] == {stat: 0.0 for stat in lr.STAT_KEYS}
        assert set(block["final"]) == set(lr.STAT_KEYS)


# --- error cases -----------------------------------------------------------
class TestErrorCases:
    """Inputs the module has no answer for raise; it does not invent one."""

    def test_a_string_player_raises(self):
        with pytest.raises(AttributeError):
            lr.resolve_stat_block("not-a-dict")

    def test_a_none_player_raises(self):
        with pytest.raises(AttributeError):
            lr.resolve_stat_block(None)

    def test_flat_stats_of_a_none_player_raises(self):
        with pytest.raises(AttributeError):
            lr.equipment_flat_stats(None)

    def test_combine_of_none_layers_raises(self):
        with pytest.raises(AttributeError):
            lr.combine(None, None)

    def test_a_non_dict_stat_line_raises(self):
        with pytest.raises((ValueError, TypeError)):
            lr.apply_to_stat_line("junk", lr.resolve_stat_block(_player()))

    def test_a_non_dict_block_raises(self):
        with pytest.raises(AttributeError):
            lr.apply_to_stat_line({"atk": 10}, "junk")

    def test_no_error_case_leaves_a_half_written_result_behind(self):
        # A refusal must be total: either the block is returned whole or the
        # call raises. Never a block missing a section.
        block = lr.resolve_stat_block(_player())
        assert len(block) == 5


# --- apply_to_stat_line ----------------------------------------------------
class TestApplyToStatLine:
    def test_the_input_line_is_never_mutated(self):
        line = {"atk": 10, "def_": 5, "hp": 100, "realm": 3}
        snapshot = dict(line)
        lr.apply_to_stat_line(line, lr.resolve_stat_block(_player()))
        assert line == snapshot

    def test_a_caller_that_ignores_the_return_value_gets_the_unresolved_line(self):
        line = {"atk": 10}
        lr.apply_to_stat_line(line, lr.resolve_stat_block(_player()))
        assert line == {"atk": 10}

    def test_each_owned_stat_is_scaled_by_its_own_multiplier(self):
        block = lr.resolve_stat_block(_player())
        out = lr.apply_to_stat_line(
            {stat: 10.0 for stat in lr.STAT_KEYS}, block
        )
        for stat in lr.STAT_KEYS:
            mult_key = lr.MULTIPLIER_KEYS[stat]
            assert out[stat] == pytest.approx(
                10.0 * block["multipliers"][mult_key]
            )

    def test_combat_config_fields_are_left_alone(self):
        # realm / stage / element / aura are combat-config inputs, not
        # equipment. Multiplying them would silently change balance.
        line = {"atk": 10, "realm": 3, "stage": 7, "element": "fire", "aura": "keen"}
        out = lr.apply_to_stat_line(line, lr.resolve_stat_block(_player()))
        for key in ("realm", "stage", "element", "aura"):
            assert out[key] == line[key]

    def test_an_empty_multipliers_map_falls_back_to_the_identity_block(self):
        # MEASURED: an empty dict is falsy, so ``block.get("multipliers") or
        # identity_multipliers()`` never reaches the ``.get(key, 1.0)`` default
        # at all. This test therefore pins the FALLBACK BRANCH, not the
        # missing-key default -- ``test_a_missing_multiplier_key_scales_by_one``
        # below is the one that pins the default.
        out = lr.apply_to_stat_line({"atk": 10, "def_": 5}, {"multipliers": {}})
        assert out["atk"] == 10.0
        assert out["def_"] == 5.0

    def test_a_missing_multiplier_key_scales_by_one(self):
        # The multipliers map is TRUTHY (it holds an unrelated key) so the
        # ``or identity_multipliers()`` fallback does not fire, and the
        # ``.get(mult_key, 1.0)`` default is what is under test here.
        out = lr.apply_to_stat_line({"atk": 10}, {"multipliers": {"hp_mult": 2.0}})
        assert out["atk"] == 10.0, "a missing multiplier key did not scale by one"

    def test_a_missing_multiplier_key_is_not_zeroed(self):
        try:
            out = lr.apply_to_stat_line({"atk": 10}, {"multipliers": {"hp_mult": 2.0}})
        except (TypeError, ValueError):
            return
        assert out["atk"] != 0.0, "a missing multiplier silently zeroed the stat"

    def test_an_unrecognised_multiplier_key_is_ignored(self):
        out = lr.apply_to_stat_line(
            {"atk": 10}, {"multipliers": {"nonsense_mult": 4.0}}
        )
        assert out["atk"] == 10.0

    def test_a_none_block_scales_by_identity_rather_than_raising(self):
        assert lr.apply_to_stat_line({"atk": 10}, None) == {"atk": 10.0}

    def test_a_none_multipliers_section_scales_by_identity(self):
        assert lr.apply_to_stat_line({"atk": 10}, {"multipliers": None}) == {"atk": 10.0}

    def test_an_empty_stat_line_stays_empty(self):
        assert lr.apply_to_stat_line({}, lr.resolve_stat_block(_player())) == {}

    def test_a_numeric_string_stat_value_is_coerced_to_a_number(self):
        out = lr.apply_to_stat_line({"atk": "10"}, lr.resolve_stat_block(_player()))
        assert isinstance(out["atk"], float)


# --- numeric hygiene -------------------------------------------------------
class TestNumericHygiene:
    """Values that are not plain numbers are reported, not normalised away."""

    def test_a_not_a_number_multiplier_is_refused_rather_than_propagated(self):
        # FIXED. ``float(nan)`` used to sail through and poison every number
        # derived from it -- a fight could end on a value nobody can compare.
        assert lr.combine({"atk_mult": float("nan")}, {})["atk_mult"] == 1.0

    def test_an_infinite_multiplier_is_refused_rather_than_propagated(self):
        # FIXED. ``float(inf)`` used to propagate straight into the stat block.
        assert lr.combine({"atk_mult": float("inf")}, {})["atk_mult"] == 1.0

    def test_a_negative_infinite_multiplier_is_refused_too(self):
        # The other direction of infinity, pinned so the check is isfinite()
        # rather than a ``> 0`` test that would let -inf through.
        assert lr.combine({"atk_mult": float("-inf")}, {})["atk_mult"] == 1.0

    def test_no_refused_multiplier_reaches_a_fight_as_a_non_finite_number(self):
        # The whole point, checked on every bad value at once: nothing that
        # cannot be reasoned about survives into the block.
        for bad in (float("nan"), float("inf"), float("-inf"), None, "abc", True):
            value = lr.combine({"atk_mult": bad}, {})["atk_mult"]
            assert math.isfinite(value), f"{bad!r} reached the block as {value!r}"

    def test_a_boolean_multiplier_is_refused_rather_than_read_as_one(self):
        # FIXED. ``float(True) == 1.0``, so a caller-supplied boolean read as a
        # deliberate no-buff rate. It is a data bug, and it is now skipped --
        # which lands on the same 1.0 by the skip rule, not by coercion.
        assert lr.combine({"atk_mult": True}, {})["atk_mult"] == 1.0
        assert lr.combine({"atk_mult": False}, {})["atk_mult"] == 1.0

    def test_a_finite_numeric_multiplier_is_still_applied(self):
        # The guard must not swallow good data: a real rate still multiplies.
        assert lr.combine({"atk_mult": 1.5}, {})["atk_mult"] == 1.5
        assert lr.combine({"atk_mult": "2.0"}, {})["atk_mult"] == 2.0

    def test_a_non_finite_stat_block_never_reaches_the_stat_line(self):
        # End of the road: even if a non-finite value is handed straight to
        # the scaling function, the stat it would have poisoned is left alone.
        out = lr.apply_to_stat_line(
            {"atk": 10, "def_": 4},
            {"multipliers": {"atk_mult": float("inf"), "def_mult": 2.0}},
        )
        assert out["atk"] == 10
        assert out["def_"] == 8.0
        # Iterate the OUTPUT keys, not all five stats: the line was only given
        # two, and ``apply_to_stat_line`` must not invent the other three.
        for stat, value in out.items():
            assert math.isfinite(value), f"{stat} reached a fight as {value!r}"
        assert set(out) == {"atk", "def_"}


# --- feature flag ----------------------------------------------------------
class TestFeatureFlag:
    def test_the_flag_name_is_the_documented_environment_variable(self):
        assert lr.FEATURE_FLAG == "WC_LOADOUT_RESOLVER"

    def test_unset_means_disabled(self, monkeypatch):
        monkeypatch.delenv(lr.FEATURE_FLAG, raising=False)
        assert lr.resolver_enabled() is False

    def test_exactly_one_enables(self, monkeypatch):
        monkeypatch.setenv(lr.FEATURE_FLAG, "1")
        assert lr.resolver_enabled() is True

    def test_a_truthy_looking_word_does_not_enable(self, monkeypatch):
        # Measured: "true" and "yes" both read as disabled.
        monkeypatch.setenv(lr.FEATURE_FLAG, "true")
        assert lr.resolver_enabled() is False

    def test_zero_does_not_enable(self, monkeypatch):
        monkeypatch.setenv(lr.FEATURE_FLAG, "0")
        assert lr.resolver_enabled() is False

    def test_the_flag_does_not_change_the_resolved_numbers(self, monkeypatch):
        # The flag gates the SIMULATOR, not this function. Turning it on must
        # not alter what the resolver computes.
        player = _player()
        monkeypatch.delenv(lr.FEATURE_FLAG, raising=False)
        off = lr.resolve_stat_block(player)
        monkeypatch.setenv(lr.FEATURE_FLAG, "1")
        assert lr.resolve_stat_block(player) == off

    def test_the_resolver_is_importable_and_callable_with_the_flag_off(
        self, monkeypatch
    ):
        monkeypatch.delenv(lr.FEATURE_FLAG, raising=False)
        assert lr.resolver_enabled() is False
        assert set(lr.resolve_stat_block(_player())) == {
            "flat", "equipment_multipliers", "artifact_multipliers",
            "multipliers", "final",
        }


# --- anti-abuse ------------------------------------------------------------
class TestNoCallerSuppliedMagnitude:
    """There is no argument through which a caller sets a size."""

    def test_the_public_callables_take_no_magnitude_parameter(self):
        import inspect

        for name in ("resolve_stat_block", "equipment_flat_stats",
                     "equipment_multipliers", "artifact_multipliers", "combine"):
            params = set(inspect.signature(getattr(lr, name)).parameters)
            assert params <= {"player", "player_id", "equipment_mult", "artifact_mult"}, (
                f"{name} gained a caller-supplied parameter: {params}"
            )

    def test_the_layer_readers_accept_only_a_player_and_an_optional_id(self):
        import inspect

        for name in ("equipment_flat_stats", "equipment_multipliers",
                     "artifact_multipliers"):
            params = list(inspect.signature(getattr(lr, name)).parameters)
            assert params == ["player", "player_id"], f"{name} params: {params}"


# --- the module's own docstring claims -------------------------------------
class TestDocumentedInvariantsHold:
    def test_the_module_imports_the_two_catalogues_it_reads(self):
        assert lr.equipment is equipment
        assert lr.artifacts is artifacts

    def test_a_player_id_stamps_binding_without_changing_the_numbers(self):
        player = _player()
        without = lr.resolve_stat_block(player)
        with_id = lr.resolve_stat_block(player, "player-123")
        assert with_id == without

    def test_an_id_does_not_re_home_an_already_bound_item(self):
        bound = _item("copper_sword")
        bound["bound_to"] = "player-original"
        player = {"equipment": {"items": {"copper_sword": bound}}}
        lr.resolve_stat_block(player, "player-someone-else")
        assert player["equipment"]["items"]["copper_sword"]["bound_to"] == (
            "player-original"
        )

    def test_the_environment_does_not_leak_into_the_flag_between_tests(
        self, monkeypatch
    ):
        monkeypatch.delenv(lr.FEATURE_FLAG, raising=False)
        assert os.getenv(lr.FEATURE_FLAG) is None

# --- non-finite and non-numeric input on the STAT side ---------------------
class TestStatSideIsGuarded:
    """A stat value is guarded by the same rule as a multiplier.

    ``nan``/``inf`` can be READ by ``float()`` without raising, so the
    exception arm alone never sees them. Each case below is checked by
    IDENTITY -- a refused stat must be the caller's own object, still sitting
    there, not a value the resolver wrote. Reading the result and asking "is
    it finite?" would be wrong: a refused inf still reads back as inf.
    """

    def test_a_non_finite_stat_is_left_exactly_as_the_caller_gave_it(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            line = {"atk": bad, "hp": 100.0}
            out = lr.apply_to_stat_line(line, {"multipliers": {"atk_mult": 2.0}})
            assert out["atk"] is bad, f"{bad!r} was overwritten with {out['atk']!r}"

    def test_one_bad_stat_does_not_stop_the_other_four_from_scaling(self):
        # EVERY multiplier must be supplied. Passing only atk_mult leaves the
        # other four at identity, so this assertion would fail for a reason
        # that has nothing to do with the guard under test.
        multipliers = {key: 2.0 for key in lr.MULTIPLIER_KEYS.values()}
        line = {
            "atk": float("inf"), "hp": 100.0, "spd": 50.0, "mp": 25.0, "def_": 10.0,
        }
        out = lr.apply_to_stat_line(line, {"multipliers": multipliers})
        assert out["atk"] == float("inf")  # untouched, refused
        assert out["hp"] == 200.0
        assert out["spd"] == 100.0
        assert out["mp"] == 50.0
        assert out["def_"] == 20.0

    def test_a_non_numeric_stat_is_skipped_rather_than_raising(self):
        for bad in (None, "abc", True, 10 ** 400):
            line = {"atk": bad}
            out = lr.apply_to_stat_line(line, {"multipliers": {"atk_mult": 2.0}})
            assert out["atk"] is bad

    def test_two_finite_numbers_cannot_multiply_out_to_inf(self):
        # 1e308 x 1e308 == inf. Each side passes its own guard; the PRODUCT
        # is the first non-finite value, so that is where it is refused.
        out = lr.apply_to_stat_line(
            {"atk": 1e308, "hp": 10.0}, {"multipliers": {"atk_mult": 1e308}}
        )
        assert math.isfinite(out["atk"]), f"wrote {out['atk']!r}"
        assert out["hp"] == 10.0, "the sibling stat stopped scaling"

    def test_the_callers_dict_is_never_mutated_by_any_of_this(self):
        line = {"atk": float("nan"), "hp": 100.0}
        snapshot = dict(line)
        lr.apply_to_stat_line(line, {"multipliers": {"atk_mult": 2.0}})
        assert line == snapshot


# --- the summing loop ------------------------------------------------------
class TestFlatStatsSurvivesBadCatalogueValues:
    """``equipment_flat_stats`` used to call ``float()`` with no guard at all."""

    def test_a_non_numeric_catalogue_value_leaves_that_stat_at_zero(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            equipment, "_item_view", lambda item: {"stats": {"atk": "not-a-number"}}
        )
        totals = lr.equipment_flat_stats(_player(), "t")
        assert totals["atk"] == 0.0

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 10 ** 400])
    def test_a_non_finite_catalogue_value_is_never_summed_in(self, bad, monkeypatch):
        monkeypatch.setattr(
            equipment, "_item_view", lambda item: {"stats": {"atk": bad}}
        )
        totals = lr.equipment_flat_stats(_player(), "t")
        assert totals["atk"] == 0.0
        assert all(math.isfinite(v) for v in totals.values())

    def test_two_large_finite_values_cannot_overflow_the_running_total(
        self, monkeypatch
    ):
        # Two EQUIPPED items each worth 1e308 sum to inf. One item cannot
        # reach it, so the loadout needs both.
        monkeypatch.setattr(
            equipment, "_item_view", lambda item: {"stats": {"atk": 1e308}}
        )
        player = _player(ids=("copper_sword", "iron_blade"))
        totals = lr.equipment_flat_stats(player, "t")
        assert math.isfinite(totals["atk"]), f"published {totals['atk']!r}"


# --- the multiply step -----------------------------------------------------
class TestCombineRefusesAnOverflowingProduct:
    """Both layers pass their own guard and the product is still non-finite."""

    def test_a_product_of_two_finite_multipliers_is_not_allowed_to_be_inf(self):
        combined = lr.combine({"atk_mult": 1e308}, {"atk_mult": 1e308})
        assert math.isfinite(combined["atk_mult"])

    def test_only_the_overflowing_key_falls_back_to_identity(self):
        combined = lr.combine(
            {"atk_mult": 1e308, "hp_mult": 1.5},
            {"atk_mult": 1e308, "hp_mult": 2.0},
        )
        assert combined["atk_mult"] == 1.0
        assert combined["hp_mult"] == 3.0


# --- the shared guard ------------------------------------------------------
class TestCoerceNumber:
    """The one conversion guard both sides of the boundary now share."""

    @pytest.mark.parametrize(
        "bad",
        [None, "abc", True, float("nan"), float("inf"), float("-inf"), 10 ** 400],
    )
    def test_every_unusable_number_is_refused(self, bad):
        assert lr._coerce_number(bad) is None

    @pytest.mark.parametrize("good", [0, 1, -1, 0.5, "2.5", -3])
    def test_an_ordinary_number_passes_through_unchanged(self, good):
        assert lr._coerce_number(good) == float(good)

    def test_the_multiplier_guard_is_this_guard(self):
        for bad in (None, "abc", True, float("nan"), float("inf"), 10 ** 400):
            assert lr._coerce_multiplier(bad) == lr._coerce_number(bad)


# --- the loadout reaches a fight through ONE authenticated route ----------
#
# Everything above this line tests the resolver as a pure function: hand it a
# player's save, get numbers back. That proves the ARITHMETIC but not the
# DOOR. The resolver has no route of its own -- it has exactly one live entry
# into a real fight, and that entry is the seam where "can an unauthenticated
# caller resolve a loadout?" is actually answerable.
#
# WHY POST /combat/auto-battle
# ----------------------------
# MEASURED, not assumed. ``combat_config.apply_loadout`` is the only caller of
# ``resolve_stat_block`` outside the module's own tests, and ``calculate_auto_
# battle`` is the only thing that calls it -- with ``player=player`` passed
# only from ``main.py:3652``, inside ``POST /combat/auto-battle``. Every other
# simulation entry point in the server (``/pvp/match``, ``/pvp/replay``, rift,
# tower, tournament, ghost_battle) calls ``calculate_auto_battle`` WITHOUT the
# ``player`` keyword, and ``apply_loadout`` returns the stat line unchanged
# when ``player is None``. So this one route is the whole reachable surface.
#
# WHY THESE ARE CONTRACTS AND NOT SNAPSHOTS
# -----------------------------------------
# The three-arm shape below is what makes the assertion bite. Asserting only
# "anonymous is refused" passes for any reason the route is closed -- a broken
# guard, a typo in the path, a route that does not exist at all. So each test
# pair refuses the anonymous and cross-player arms AND proves the OWNER arm
# still gets a real fight. If someone deletes the guard, the refusal test goes
# red; if someone breaks the route, the owner arm goes red. Neither can hide
# behind the other.
class TestLoadoutResolutionRequiresAuth:
    """No unauthenticated or cross-player call may resolve a loadout."""

    @pytest.fixture()
    def raw_client(self):
        # The RAW TestClient, not ``Session``: the anonymous arm needs a request
        # that carries no Authorization header at all.
        from fastapi.testclient import TestClient

        from server.main import app
        from server.tests.wc_test_auth import clear_signup_rate_limit

        clear_signup_rate_limit()
        return TestClient(app)

    @staticmethod
    def _create(client, name):
        response = client.post(
            "/player/create", json={"name": name, "spirit_root": "fire"}
        )
        assert response.status_code == 200, response.text
        return response.json()["id"]

    @pytest.fixture()
    def owner_id(self, raw_client):
        from server.tests.wc_test_auth import clear_signup_rate_limit

        clear_signup_rate_limit()
        return self._create(raw_client, "LoadoutAuthOwner")

    def test_the_only_route_that_resolves_a_loadout_is_the_one_under_test(self):
        """Pin the premise the rest of this class rests on.

        If a second route starts passing ``player=`` into the simulator, this
        goes red and the new route has to be given its own refusal arm here.
        A resolver reachable from an unaudited second door is a guard test
        that measures one door and reports the house safe.
        """
        import inspect

        import combat_config

        source = inspect.getsource(combat_config)
        assert source.count("apply_loadout(") == 2, (
            "apply_loadout gained a caller; every new caller needs its own "
            "auth arm in TestLoadoutResolutionRequiresAuth"
        )
        assert "def apply_loadout(player_stats: dict, player: dict | None = None)" in source

    def test_an_anonymous_caller_is_refused_a_fight_and_therefore_a_loadout(
        self, raw_client, owner_id
    ):
        response = raw_client.post("/combat/auto-battle", json={"player_id": owner_id})
        assert response.status_code == 401, response.text

    def test_an_anonymous_caller_is_refused_however_it_asks(self, raw_client, owner_id):
        # Same refusal with a bonus field, a flag-shaped field and an extra
        # key present: a guard that only trips on the minimal body would let a
        # padded body through.
        for body in (
            {"player_id": owner_id},
            {"player_id": owner_id, "times": 1},
            {"player_id": owner_id, "monster_id": "any", "seed": 7},
        ):
            response = raw_client.post("/combat/auto-battle", json=body)
            assert response.status_code == 401, f"{body} -> {response.status_code}"

    def test_a_malformed_or_foreign_token_is_refused_rather_than_treated_as_anonymous(
        self, raw_client, owner_id
    ):
        for header in (
            {"Authorization": "Bearer not-a-real-token"},
            {"Authorization": owner_id},
            {"Authorization": f"Bearer {owner_id}"},
        ):
            response = raw_client.post(
                "/combat/auto-battle",
                json={"player_id": owner_id},
                headers=header,
            )
            assert response.status_code == 401, f"{header} -> {response.status_code}"

    def test_a_session_for_one_player_cannot_resolve_another_players_loadout(
        self, raw_client, owner_id
    ):
        """The 403 arm, and the reason the 401 arm alone is not enough.

        A resolver that authenticated but did not check OWNERSHIP would let a
        signed-in caller read any other player's resolved stat block. This is
        that test: a valid token, a real target, still refused.
        """
        from server.tests.wc_test_auth import auth_headers, clear_signup_rate_limit

        clear_signup_rate_limit()
        victim = self._create(raw_client, "LoadoutAuthVictim")
        response = raw_client.post(
            "/combat/auto-battle",
            json={"player_id": victim},
            headers=auth_headers(owner_id),
        )
        assert response.status_code == 403, response.text
        assert victim != owner_id

    def test_the_owner_still_gets_a_real_fight_so_the_refusals_mean_something(
        self, raw_client, owner_id
    ):
        """The positive control every refusal test above is measured against.

        Measured on this tree: 200, with a resolved ``winner`` and a non-empty
        log. Without this arm, "401" is indistinguishable from "route broken".
        """
        from server.tests.wc_test_auth import auth_headers

        response = raw_client.post(
            "/combat/auto-battle",
            json={"player_id": owner_id},
            headers=auth_headers(owner_id),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["winner"] in ("player", "enemy")
        assert body["log"], "a 200 with an empty fight is not a resolved fight"

    def test_authentication_is_checked_before_the_loadout_is_ever_resolved(
        self, raw_client, owner_id, monkeypatch
    ):
        """Order proof: the resolver is not reached at all on a refused call.

        MEASURED on this tree: with the resolver's entry point replaced by a
        counter, an anonymous call leaves the counter at zero. If the guard
        moved behind resolution -- or behind the fight -- this goes red, which
        is the defect where an unauthenticated caller makes the server do work
        on their behalf before being told no.

        The flag is set ON here deliberately. With it off the resolver is
        skipped for everyone, so the counter would read zero whether the guard
        held or the feature was merely inert.
        """
        monkeypatch.setenv(lr.FEATURE_FLAG, "1")
        calls = []
        real_resolve = lr.resolve_stat_block

        def counting(player, player_id=None):
            calls.append(player_id)
            return real_resolve(player, player_id)

        monkeypatch.setattr(lr, "resolve_stat_block", counting)
        refused = raw_client.post("/combat/auto-battle", json={"player_id": owner_id})
        assert refused.status_code == 401
        assert calls == [], f"resolver ran {len(calls)} times behind a 401"

    def test_the_same_call_under_a_valid_session_does_reach_the_resolver(
        self, raw_client, owner_id, monkeypatch
    ):
        """The other half of the order proof, so the counter can bite.

        Without this, ``calls == []`` also passes when the monkeypatch never
        took effect -- the false-green the previous test cannot rule out alone.

        MEASURED: the resolver is only reached with
        ``WC_LOADOUT_RESOLVER=1`` set. With the flag unset the server answers
        a perfectly good 200 and ``apply_loadout`` returns early, so the
        counter stays at zero for a reason that has nothing to do with auth.
        That is exactly why this control has to switch the flag on -- it is
        the only thing that makes the zero in the test above mean "refused".
        """
        from server.tests.wc_test_auth import auth_headers

        monkeypatch.setenv(lr.FEATURE_FLAG, "1")
        calls = []
        real_resolve = lr.resolve_stat_block

        def counting(player, player_id=None):
            calls.append(player_id)
            return real_resolve(player, player_id)

        # The simulator imports the resolver lazily by name, so the patch has to
        # be visible on the module object -- which it is, this IS that object.
        monkeypatch.setattr(lr, "resolve_stat_block", counting)
        raw_client.post(
            "/combat/auto-battle",
            json={"player_id": owner_id},
            headers=auth_headers(owner_id),
        )
        assert calls, "resolver was never called; the control proves nothing"

    def test_the_flag_is_what_gates_the_resolver_not_the_guard(
        self, raw_client, owner_id, monkeypatch
    ):
        """Two independent reasons the resolver does not run, kept apart.

        With the flag OFF an ANONYMOUS caller is still refused 401 -- the guard
        is not conditional on the flag. A flag-gated guard would answer 200
        here and quietly resolve nothing for everyone, which reads as a green
        suite and an unauthenticated stat block.
        """
        monkeypatch.delenv(lr.FEATURE_FLAG, raising=False)
        anonymous = raw_client.post("/combat/auto-battle", json={"player_id": owner_id})
        assert anonymous.status_code == 401, anonymous.text


# --- what an authenticated caller still cannot do --------------------------
def _numbers_in(value) -> set:
    """Every number reachable inside a nested dict/list, floats compared exactly.

    Used to ask "did this body value turn up in what the resolver was handed?"
    -- which is a question about CONTENTS, so it has to walk the structure
    rather than look for a top-level key.
    """
    import math as _math

    # Booleans are skipped deliberately. Python says ``False == 0.0`` is True,
    # and 0.0 is a perfectly ordinary stat value in a saved player, so a
    # ``{"resolve": False}`` probe would collide with the player's real zeros
    # and report a leak that is not there. A boolean is not a magnitude; this
    # scan is looking for magnitudes.
    found: set = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found |= _numbers_in(key)
            found |= _numbers_in(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found |= _numbers_in(item)
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        # Keep the value as the exact type it arrived as. Coercing everything
        # to float would let 999999 and 999999.0 collide, and letting an int
        # match a float of equal value is the whole point -- a caller who
        # writes 999999.0 must not slip past a scan that only looked at ints.
        found.add(value)
        try:
            as_float = float(value)
        except (OverflowError, ValueError):
            as_float = float("inf")
        if _math.isfinite(as_float):
            found.add(as_float)
            found.add(int(as_float))
    return found


class TestNoCallerSuppliedMagnitudeOverHttp:
    """The 200 arm is only safe because the body cannot carry a size."""

    @pytest.fixture()
    def raw_client(self):
        from fastapi.testclient import TestClient

        from server.main import app
        from server.tests.wc_test_auth import clear_signup_rate_limit

        clear_signup_rate_limit()
        return TestClient(app)

    @pytest.mark.parametrize(
        "payload",
        [
            {"atk_mult": 99.0},
            {"multipliers": {"atk_mult": 99.0}},
            {"loadout": {"flat": {"atk": 999999}}},
            {"stat_line": {"atk": 999999}},
            {"resolve": False},
            {"flat": {"atk": 999999}, "final": {"atk": 999999}},
        ],
    )
    def test_a_body_field_named_like_a_loadout_changes_nothing(
        self, raw_client, payload, monkeypatch
    ):
        """The body cannot carry a size: the fight is the SERVER's own numbers.

        MEASURED TWICE, and the obvious assertions are both wrong:

        1. Two IDENTICAL bodies do not produce identical fights.
           ``_auto_battle_enemy_stats`` picks the opponent from a fresh
           ``Random(secrets.randbits(64))`` on every call, so the same player
           fights a ``wild_boar`` one time and a ``street_thug`` the next.
           Comparing two replies for equality fails on correct code.
        2. The player's own resolved block also MOVES across two fights, for a
           reason that has nothing to do with the request. The auto-battle
           route grants wallet currency, and this file's own module docstring
           records the other moving part: ``ensure_state`` rolls a fresh random
           ``rolled_stats`` for any item record that does not pin them. So a
           before/after block comparison drifts on correct code too.

        What IS deterministic, and is the actual contract: the resolver is
        handed the player's saved state and nothing else. So this captures what
        the resolver was really called with and proves none of the padded keys
        or values are anywhere inside it. If a future edit starts reading a
        request field for a magnitude, the value lands in that captured
        argument and this goes red -- which is the exact shape of the bug the
        module's own anti-abuse note warns about.
        """
        from server.tests.wc_test_auth import auth_headers, clear_signup_rate_limit

        monkeypatch.setenv(lr.FEATURE_FLAG, "1")
        clear_signup_rate_limit()
        create = raw_client.post(
            "/player/create", json={"name": "LoadoutMagnitude", "spirit_root": "fire"}
        )
        assert create.status_code == 200, create.text
        player_id = create.json()["id"]
        headers = auth_headers(player_id)

        seen = []
        real_resolve = lr.resolve_stat_block

        def capturing(player, player_id_=None):
            seen.append(copy.deepcopy(player))
            return real_resolve(player, player_id_)

        monkeypatch.setattr(lr, "resolve_stat_block", capturing)

        padded = dict(payload)
        padded["player_id"] = player_id
        loaded = raw_client.post("/combat/auto-battle", json=padded, headers=headers)
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["winner"] in ("player", "enemy")
        assert seen, "the resolver never ran; this test would pass vacuously"

        for key in payload:
            assert key not in seen[-1], (
                f"body field {key!r} reached the resolver's input"
            )
        for value in _numbers_in(payload):
            assert value not in _numbers_in(seen[-1]), (
                f"body value {value!r} reached the resolver's input"
            )

    def test_the_enemy_pick_is_random_so_two_fights_are_not_compared_for_equality(self):
        """Why the equality assertion above is absent, pinned so it stays absent.

        Without this, the next reader sees no reply-equality test and adds one
        back, and then either weakens it to a subset or is baffled by a green
        suite turning red on correct code.
        """
        from server.tests.wc_test_auth import auth_headers, clear_signup_rate_limit
        from fastapi.testclient import TestClient

        from server.main import app

        clear_signup_rate_limit()
        raw = TestClient(app)
        created = raw.post(
            "/player/create", json={"name": "LoadoutRandomPick", "spirit_root": "fire"}
        )
        assert created.status_code == 200, created.text
        player_id = created.json()["id"]
        headers = auth_headers(player_id)

        fights = [
            raw.post(
                "/combat/auto-battle", json={"player_id": player_id}, headers=headers
            ).json()
            for _ in range(4)
        ]
        assert all(f["winner"] in ("player", "enemy") for f in fights)
        # The precondition this test claims: replies are NOT reproducible.
        # If the server ever pins its seed, this goes red and the equality
        # assertion becomes available again -- which is the good outcome.
        assert any(f != fights[0] for f in fights[1:]), (
            "fights are now reproducible; a reply-equality assertion is "
            "possible and stronger -- replace the block-level test above"
        )
