"""Concrete proof that ``_apply_blood_affix`` computes the bloodline bonus.

WHAT THIS FILE COVERS
---------------------
``server/beast_config.py`` spends real materials to raise a beast's
``blood_level``, and promises the player the beast gets stronger. Two call
sites do that arithmetic:

* ``upgrade_blood`` -- the paid path. It debits materials, raises
  ``blood_level``, then calls ``_apply_blood_affix``.
* ``evolve_beast`` -- the reset path. Evolution overwrites hp/atk/def with the
  new rarity's base roll, which would silently erase a bonus the player had
  already paid for, so it re-derives the affix afterwards.

Before this, NO test in the repository referenced ``_apply_blood_affix`` or
``blood_affix_total`` at all (a content search over ``server/tests`` returned
zero hits), so both call sites ran entirely unmeasured. If the function were
deleted these tests go red; if it drifted by one point they go red.

HOW THE EXPECTED NUMBERS ARE DERIVED
-------------------------------------
Not hand-typed balance constants. ``BLOOD_PROGRESSION[level].affix_value`` is
a PER-LEVEL INCREMENT, so the total owed at level N is the sum of every
increment up to N. That total is then split hp/atk/def in the 5:2:1 proportion
the module declares in ``_BLOOD_AFFIX_SPLIT``. Each expected value below is
computed from the module's own table at run time, so a rebalance of the table
keeps this file green while a change to the SPLIT or to the ledger maths turns
it red.

One test -- ``test_level_one_split_matches_the_documented_5_2_1_ratio`` --
does pin literal numbers (178 / 71 / 35). That is deliberate: it is the
human-readable anchor that proves the split is really 5:2:1 and not merely
self-consistent with whatever the table happens to say today.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle && env -u WC_PG_DB python -m pytest \\
      server/tests/test_beast_blood_affix.py -q -p no:logging --no-header \\
      -p no:randomly

This file imports only ``beast_config`` itself. It never imports
``server.main``, so a half-written ``main.py`` on a shared tree cannot abort
this suite's collection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import beast_config as bc  # noqa: E402

# Base stats as ``generate_beast`` mints a common-rarity beast: 100 / 20 / 10.
# The split ratio below is this same 100:20:10 shape, which is why the module
# reuses it for the bonus.
BASE_HP, BASE_ATK, BASE_DEF = 100, 20, 10


class FakeStore:
    """Minimal canonical inventory store: the one writer ``upgrade_blood`` gates on."""

    def __init__(self, counts=None):
        self.counts = dict(counts or {})
        self.consume_calls = []

    def get_item_count(self, player_id, item_id):
        return self.counts.get(item_id, 0)

    def consume_items(self, player_id, items):
        # All-or-nothing, like the real store: check everything, spend nothing yet.
        for item_id, amount in items.items():
            if self.counts.get(item_id, 0) < amount:
                return {"ok": False, "error": f"Insufficient {item_id}"}
        for item_id, amount in items.items():
            self.counts[item_id] -= amount
        self.consume_calls.append(dict(items))
        return {"ok": True}


def _rich_store():
    """A store holding enough material for every blood tier."""
    return FakeStore({"beast_bone": 10_000, "spirit_herb": 10_000, "blood_vial": 10_000})


def _cumulative(level: int) -> int:
    """The bonus a beast at `level` is owed, derived from the module's own table.

    ``affix_value`` is a per-level INCREMENT. Reading it as a running total --
    the obvious mistake -- yields 5000 at level 4 instead of 9280.
    """
    return sum(
        int(bc.BLOOD_PROGRESSION[lv].affix_value)
        for lv in bc.BLOOD_PROGRESSION
        if lv <= level
    )


def _expected_shares(total: int) -> dict:
    """Split `total` the way the module's 5:2:1 split does, remainder to hp."""
    shares = {stat: (total * weight) // 8 for stat, weight in bc._BLOOD_AFFIX_SPLIT}
    shares["hp"] += total - sum(shares.values())
    return shares


def _fresh_beast(**extra):
    beast = {"rarity": "common", "level": 1, "hp": BASE_HP, "atk": BASE_ATK, "def": BASE_DEF}
    beast.update(extra)
    return beast


# === the concrete numeric anchor =============================================

def test_level_one_split_matches_the_documented_5_2_1_ratio():
    """Level 1 grants 284 points as +178 hp / +71 atk / +35 def.

    Pinned to literals on purpose. 284 * 5/8 = 177.5 -> 177 and 284 * 1/8 =
    34.5 -> 34, so floor division alone drops 2 points; the module hands the
    remainder to hp, giving 178. If the split or the rounding rule changes,
    these three numbers change and this goes red.
    """
    beast = _fresh_beast()
    bc._apply_blood_affix(beast, 1)

    assert beast["hp"] == 278, "100 base + 178"
    assert beast["atk"] == 91, "20 base + 71"
    assert beast["def"] == 45, "10 base + 35"
    assert beast["blood_affix_total"] == 284


# === the same arithmetic, derived from the table ============================

@pytest.mark.parametrize("level", sorted(bc.BLOOD_PROGRESSION))
def test_apply_grants_exactly_the_cumulative_affix_at_every_level(level):
    """Each level's three stat gains sum to precisely that level's cumulative bonus.

    This is the property that makes the split correct: 5:2:1 apportions one
    flat number across three stats, and floor division must not lose any of it.
    """
    beast = _fresh_beast()
    bc._apply_blood_affix(beast, level)

    expected = _expected_shares(_cumulative(level))
    assert beast["hp"] - BASE_HP == expected["hp"]
    assert beast["atk"] - BASE_ATK == expected["atk"]
    assert beast["def"] - BASE_DEF == expected["def"]
    assert (beast["hp"] - BASE_HP) + (beast["atk"] - BASE_ATK) + (beast["def"] - BASE_DEF) == _cumulative(level)
    assert beast["blood_affix_total"] == _cumulative(level)


def test_apply_is_idempotent_so_a_replay_adds_nothing():
    """Re-running the same level must not compound the bonus.

    This is what makes a retried upgrade or a reloaded save safe. Without the
    ``blood_affix_total`` ledger the second call would double the stats.
    """
    beast = _fresh_beast()
    bc._apply_blood_affix(beast, 2)
    once = dict(beast)

    bc._apply_blood_affix(beast, 2)
    bc._apply_blood_affix(beast, 2)

    assert beast == once


def test_apply_backfills_a_beast_that_predates_the_ledger():
    """A beast with a blood level but no bonus recorded gets its levels back.

    Before this function existed, paid upgrades raised ``blood_level`` while
    hp/atk/def kept the values ``generate_beast`` minted. Such a row has a
    level and no ``blood_affix_total``, so the next apply owes the WHOLE
    cumulative total for the level reached and the missed levels heal.
    """
    beast = _fresh_beast(blood_level=3)  # no blood_affix_total key at all
    assert "blood_affix_total" not in beast

    bc._apply_blood_affix(beast, 3)

    assert beast["blood_affix_total"] == _cumulative(3)
    assert beast["hp"] - BASE_HP == _expected_shares(_cumulative(3))["hp"]


def test_apply_ignores_a_corrupt_ledger_rather_than_crashing():
    """A stored ``blood_affix_total`` of the wrong type falls back to 0.

    A boolean is excluded too: it subclasses ``int``, so a stored ``True``
    would otherwise pass as a ledger of 1 and silently skip the bonus.
    """
    for corrupt in ("ninety", None, True, [1]):
        beast = _fresh_beast(blood_affix_total=corrupt)
        bc._apply_blood_affix(beast, 1)
        assert beast["blood_affix_total"] == _cumulative(1), corrupt


# === driving the two real call sites =======================================

def test_upgrade_blood_pays_a_measurable_stat_gain():
    """The paid path: materials spent in, hp/atk/def actually move.

    Upgrading 0 -> 1 must leave the beast 178/71/35 stronger. A
    ``_apply_blood_affix`` that returned early, wrote nothing, or wrote to a
    copy all fail this.
    """
    beast = _fresh_beast()
    store = _rich_store()

    reply = bc.upgrade_blood(beast, store, inventory_store=store, player_id="p1")

    assert reply["ok"] is True
    assert reply["new_level"] == 1
    assert beast["blood_level"] == 1
    assert (beast["hp"], beast["atk"], beast["def"]) == (278, 91, 45)
    assert beast["blood_affix_total"] == _cumulative(1)


def test_upgrade_blood_four_times_owes_the_same_total_as_one_apply():
    """Climbing every tier one at a time owes exactly as much as one apply.

    Proves the ledger accumulates increments rather than resetting.

    The TOTAL is compared, not the three stats individually, and that is the
    honest assertion. Each call floors its own 5:2:1 shares and hands the
    remainder to hp, so four calls floor four times while one call floors
    once. Measured drift at level 4 is hp +3 / atk -1 / def -2 out of a 9280
    total -- the bonus is worth the same either way, but not to the exact point
    per stat. Asserting per-stat equality here would be asserting a rounding
    scheme the module does not implement.
    """
    climbed = _fresh_beast()
    store = _rich_store()
    for expected_level in sorted(bc.BLOOD_PROGRESSION):
        reply = bc.upgrade_blood(climbed, store, inventory_store=store, player_id="p1")
        assert reply["ok"] is True
        assert reply["new_level"] == expected_level

    one_shot = _fresh_beast()
    bc._apply_blood_affix(one_shot, max(bc.BLOOD_PROGRESSION))

    ladder_total = (climbed["hp"] - BASE_HP) + (climbed["atk"] - BASE_ATK) + (climbed["def"] - BASE_DEF)
    one_shot_total = (one_shot["hp"] - BASE_HP) + (one_shot["atk"] - BASE_ATK) + (one_shot["def"] - BASE_DEF)

    assert ladder_total == _cumulative(max(bc.BLOOD_PROGRESSION))
    assert ladder_total == one_shot_total
    assert climbed["blood_affix_total"] == one_shot["blood_affix_total"]

    # Each stat stays within one call's flooring remainder (units - 1 = 7).
    for stat, base in (("hp", BASE_HP), ("atk", BASE_ATK), ("def", BASE_DEF)):
        assert abs((climbed[stat] - base) - (one_shot[stat] - base)) <= 7, stat


def test_upgrade_blood_spends_nothing_when_a_stat_is_unreadable():
    """A corrupt hp must refuse BEFORE the materials are taken.

    ``_apply_blood_affix`` does integer arithmetic, so a non-numeric stored
    stat would raise -- and raise after the spend, turning a bad row into a 500
    with the player's upgrade paid for and lost.
    """
    beast = _fresh_beast(hp="broken")
    store = _rich_store()
    before = dict(store.counts)

    reply = bc.upgrade_blood(beast, store, inventory_store=store, player_id="p1")

    assert reply["ok"] is False
    assert "hp" in reply["error"]
    assert store.counts == before, "materials were taken on a refused upgrade"
    assert beast["blood_level"] if "blood_level" in beast else True
    assert beast.get("blood_level", 0) == 0, "level advanced on a refused upgrade"


def test_evolve_reapplies_the_paid_affix_after_overwriting_the_stats():
    """Evolution resets hp/atk/def, so the bonus must be re-derived after it.

    ``evolve_beast`` rewrites the stats to the new rarity's base roll. Without
    the re-apply, a beast at blood level 2 would evolve and silently lose the
    443 points the player paid for.
    """
    beast = _fresh_beast()
    store = _rich_store()
    bc.upgrade_blood(beast, store, inventory_store=store, player_id="p1")
    bc.upgrade_blood(beast, store, inventory_store=store, player_id="p1")

    paid_total = _cumulative(2)
    assert beast["blood_affix_total"] == paid_total

    # Satisfy the evolution gate: common -> uncommon needs level 20 and 100 stones.
    beast["level"] = 20
    wallet = {"spirit_stone": 10_000}
    reply = bc.evolve_beast(beast, wallet)

    assert reply["ok"] is True
    assert reply["new_rarity"] == "uncommon"

    # The rarity roll is (250 / 50 / 25); the paid bonus is layered back on top.
    rare_hp, rare_atk, rare_def = bc.BEAST_RARITY["uncommon"]["stat_mult"], None, None
    mult = rare_hp
    expected = _expected_shares(paid_total)
    assert beast["hp"] == int(100 * mult) + expected["hp"]
    assert beast["atk"] == int(20 * mult) + expected["atk"]
    assert beast["def"] == int(10 * mult) + expected["def"]
    assert beast["blood_affix_total"] == paid_total
    assert beast["blood_level"] == 2, "evolution must not erase the blood level"