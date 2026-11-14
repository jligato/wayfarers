"""Contract tests for the PURE LOGIC of ``server/spirit_beasts.py``.

WHAT IS ALREADY COVERED ELSEWHERE (do not duplicate it here)
------------------------------------------------------------
* ``server/tests/test_beast_routes_contract.py`` — the four HTTP routes behind
  ``register_spirit_beast_routes``: reply key sets, the ownership guard, the
  daily cap, the 404/409/400 arms.
* ``server/tests/test_spirit_beast_companion.py`` — the same feature driven
  through the real ``server.main`` app as a signed-in player.

Neither file calls the module's functions directly. Every rule the docstring
states — the contract-stage ladder, the level ceiling, the growth loop, the
neglect grace day, the loyalty floor, the severance bill, the environment
multiplier, the attack cap — is a FUNCTION here, and a function whose only
test is "a route returned 200" is untested.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle && tools/wc_pytest.sh \
        server/tests/test_spirit_beasts_contract.py

RULES OF THIS FILE
------------------
* **No HTTP, no database, no ``server.main`` import.** These are unit contracts
  on dicts in and dicts out, so a peer editing ``main.py`` cannot break them.
* **No snapshot numbers.** Every threshold, ceiling and multiplier is read from
  the module (or from ``beast_config`` / ``cultivation_config``, which the module
  itself reads) at run time. Rebalancing the game leaves this file green; a
  function that quietly grows a private copy of a number goes red.
* **No source reading.** Everything is asserted by calling the module.
* **The clock is injected, never slept on.** ``apply_neglect`` takes ``now``;
  ``_day_index`` is derived from ``time.time() // 86400`` computed HERE so a
  change to the module's day boundary is caught instead of absorbed.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVER_DIR.parent
for _p in (str(_SERVER_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import beast_config  # noqa: E402
import spirit_beasts as sb  # noqa: E402

#: The UTC day index the module must agree with — computed the way
#: ``server/missions.py`` computes it, not by calling ``sb._day_index``.
TODAY = int(time.time() // 86400)
ONE_DAY = 86400


def _player(**over) -> dict:
    """A minimal player row. Realm/stage default to the ladder's first rung."""
    row = {
        "id": "p_contract_test",
        "cultivation_realm": "mortal",
        "cultivation_stage": "early",
        "atk": 100,
        "def_": 50,
        "hp": 1000,
        "spd": 100,
        "beasts": [],
        "beast_formation": [None, None, None],
    }
    row.update(over)
    return row


def _beast(player=None, **over) -> dict:
    """A real beast, minted by ``beast_config.grant_beast``.

    Not hand-built: a hand-built shape could carry a field the server never
    produces and the test would then pass on a shape the game never has.
    """
    beast = beast_config.grant_beast(player if player is not None else _player())
    beast["rarity"] = over.pop("rarity", beast.get("rarity", "common"))
    beast["level"] = over.pop("level", beast.get("level", 1))
    beast["hp"] = over.pop("hp", beast.get("hp", 100))
    beast["atk"] = over.pop("atk", beast.get("atk", 20))
    beast["def"] = over.pop("def", beast.get("def", 10))
    beast.update(over)
    return beast


def _contracted(beast: dict, **over) -> dict:
    """Give ``beast`` a live contract at the starting loyalty, return it."""
    contract = sb.new_contract({"id": "p_contract_test", "cultivation_realm": "mortal"}, beast)
    contract["kind"] = over.pop("kind", "bargain")
    contract["stage"] = over.pop("stage", sb.CONTRACT_STAGES[0])
    contract["bond"] = over.pop("bond", sb.BOND_ON_TAME)
    contract.update(over)
    beast["contract"] = contract
    return contract


# ==========================================================================
# Tables: internal consistency, and that they agree with the shipped tables
# ==========================================================================


def test_contract_stages_and_their_realm_gates_cover_each_other():
    """Every stage has a realm gate and every gate names a real stage.

    A one-sided mismatch is a KeyError waiting to happen: ``contract_stage_for``
    indexes ``CONTRACT_STAGE_REALM`` with every stage, and nothing else checks
    the two lists still agree.
    """
    assert set(sb.CONTRACT_STAGES) == set(sb.CONTRACT_STAGE_REALM)


def test_contract_stage_gates_point_at_real_realms():
    from cultivation_config import REALMS

    names = {entry.name for entry in REALMS}
    for stage in sb.CONTRACT_STAGES:
        realm = sb.CONTRACT_STAGE_REALM[stage]
        assert realm in names, f"stage {stage} gates on unknown realm {realm!r}"


def test_contract_stage_gates_are_monotonic_along_the_ladder():
    """A deeper stage must never be reachable at a lower realm than the one
    before it, or the ladder would hand out stages out of order."""
    order = [sb._realm_order(sb.CONTRACT_STAGE_REALM[s]) for s in sb.CONTRACT_STAGES]
    assert order == sorted(order)


def test_contract_kinds_are_fully_specified():
    """Every kind needs the four fields the module reads off it."""
    for name, spec in sb.CONTRACT_KINDS.items():
        for field in ("label", "bond_gain", "bond_decay", "severance_multiplier", "description"):
            assert field in spec, f"contract kind {name} is missing {field}"


def test_contract_kind_weights_sum_is_a_whole_roll():
    """The weighted roll in ``new_contract`` only covers every outcome if the
    weights sum to a positive number; a zero or negative total would leave the
    fallback kind silent."""
    assert sum(sb.CONTRACT_KIND_WEIGHTS.values()) > 0
    assert set(sb.CONTRACT_KIND_WEIGHTS) == set(sb.CONTRACT_KINDS)


def test_level_ceiling_is_read_from_the_evolution_table_not_invented():
    """The ceiling IS ``EVOLUTION_REQUIREMENTS[rarity]["max_level"]``.

    That identity is the whole anti-runaway claim: growth stops exactly where
    the evolution gate opens. If it ever drifts, this goes red.
    """
    for rarity, req in beast_config.EVOLUTION_REQUIREMENTS.items():
        assert sb.level_ceiling(rarity) == int(req["max_level"])


def test_unknown_rarity_ceiling_is_the_safe_floor_not_a_crash():
    assert sb.level_ceiling("no-such-rarity") == 1


def test_xp_to_next_level_is_a_positive_increasing_ramp():
    """Zero marks to level up would spin the level-up loop forever."""
    assert sb.xp_to_next_level(1) >= 1
    assert sb.xp_to_next_level(5) >= 1
    assert sb.xp_to_next_level(5) > sb.xp_to_next_level(1)
    assert sb.xp_to_next_level(0) >= 1, "level 0 must not cost zero marks"


# ==========================================================================
# Contract creation
# ==========================================================================


def test_new_contract_starts_at_the_taming_loyalty_never_at_full():
    """A tame is a beginning. Starting at full would make the whole loyalty
    mechanic unreachable on a fresh beast."""
    contract = sb.new_contract(_player(), _beast())
    assert contract["bond"] == sb.BOND_ON_TAME
    assert 0 < sb.BOND_ON_TAME < sb.BOND_MAX


def test_new_contract_status_is_active_and_stamped_with_the_player():
    contract = sb.new_contract(_player(id="p_owner"), _beast())
    assert contract["status"] == "active"
    assert contract["bonds_with"] == "p_owner"
    assert contract["severed_at"] is None


def test_new_contract_rolls_a_kind_that_is_always_one_the_module_knows():
    """The roll is over a weights table, so the outcome must always be a key of
    ``CONTRACT_KINDS`` — an unknown kind would silently fall back to ``bargain``
    behaviour everywhere downstream."""
    for _ in range(300):
        contract = sb.new_contract(_player(), _beast())
        assert contract["kind"] in sb.CONTRACT_KINDS


def test_new_contract_stage_follows_the_player_realm_not_a_roll():
    mortal = sb.new_contract(_player(cultivation_realm="mortal"), _beast())
    high = sb.new_contract(_player(cultivation_realm="nascent_soul"), _beast())
    assert mortal["stage"] == sb.contract_stage_for("mortal")
    assert high["stage"] == sb.contract_stage_for("nascent_soul")
    assert sb.CONTRACT_STAGES.index(high["stage"]) > sb.CONTRACT_STAGES.index(mortal["stage"])


def test_contract_of_invents_a_usable_contract_for_a_pre_contract_beast():
    """A beast minted before contracts existed must still fight and still obey
    the loyalty floor — the default is a normal starting contract, not an
    exemption."""
    beast = _beast()
    beast.pop("contract", None)
    contract = sb.contract_of(beast)
    assert isinstance(contract, dict)
    assert contract["status"] == "active"
    assert contract["bond"] == sb.BOND_ON_TAME
    assert sb.will_fight(beast) is True


def test_contract_of_does_not_overwrite_an_existing_contract():
    beast = _beast()
    contract = _contracted(beast, bond=77)
    assert sb.contract_of(beast) is contract
    assert contract["bond"] == 77


def test_contract_of_replaces_a_corrupt_non_dict_contract():
    beast = _beast()
    beast["contract"] = "not-a-dict"
    assert isinstance(sb.contract_of(beast), dict)


# ==========================================================================
# Contract stage ladder (evolution of the BOND, not of the beast)
# ==========================================================================


def test_contract_stage_is_the_deepest_gate_the_realm_has_cleared():
    """A mortal holds exactly the first stage and never a deeper one."""
    assert sb.contract_stage_for("mortal") == sb.CONTRACT_STAGES[0]


@pytest.mark.parametrize("realm", ["mortal", "foundation", "core", "nascent_soul"])
def test_contract_stage_is_never_deeper_than_the_realm_allows(realm):
    """The ladder is monotonic in the shipped realm order: a player cannot hold
    a stage whose gate sits above their realm."""
    order = sb._realm_order(realm)
    stage = sb.contract_stage_for(realm)
    assert order >= sb._realm_order(sb.CONTRACT_STAGE_REALM[stage])


def test_contract_stage_advances_with_a_breakthrough():
    from cultivation_config import REALMS

    previous_index = -1
    for entry in REALMS:
        index = sb.CONTRACT_STAGES.index(sb.contract_stage_for(entry.name))
        assert index >= previous_index, "the stage ladder went backwards with the realm ladder"
        previous_index = index


def test_unknown_realm_sorts_lowest_rather_than_raising_the_stage():
    """An unrecognised realm must not be read as a high one."""
    assert sb.contract_stage_for("no-such-realm") == sb.CONTRACT_STAGES[0]


# ==========================================================================
# Neglect
# ==========================================================================


def test_neglect_charges_nothing_inside_the_grace_day():
    """One missed evening is not mistreatment."""
    beast = _beast()
    contract = _contracted(beast, bond=80, kind="dominion")
    contract["bond_updated_at"] = time.time() - (ONE_DAY + 60)
    assert sb.apply_neglect(beast, now=time.time()) == 0
    assert contract["bond"] == 80


def test_neglect_charges_one_day_after_the_grace_day():
    beast = _beast()
    contract = _contracted(beast, bond=80, kind="dominion")
    now = time.time()
    contract["bond_updated_at"] = now - (sb.BOND_NEGLECT_GRACE_DAYS * ONE_DAY) - ONE_DAY
    lost = sb.apply_neglect(beast, now=now)
    assert lost == int(sb.BOND_DECAY_PER_IDLE_DAY * sb.CONTRACT_KINDS["dominion"]["bond_decay"])
    assert contract["bond"] == 80 - lost


def test_neglect_scales_with_the_days_actually_elapsed():
    """A player away for a week is charged a week, not a day — the whole reason
    elapsed time is computed rather than a scheduler tick."""
    def bond_after(days: int) -> int:
        beast = _beast()
        contract = _contracted(beast, bond=80, kind="dominion")
        now = time.time()
        contract["bond_updated_at"] = now - (days * ONE_DAY)
        sb.apply_neglect(beast, now=now)
        return contract["bond"]

    assert bond_after(2) > bond_after(3) > bond_after(5)


def test_neglect_never_drives_the_bond_below_zero():
    beast = _beast()
    contract = _contracted(beast, bond=5, kind="dominion")
    now = time.time()
    contract["bond_updated_at"] = now - (400 * ONE_DAY)
    sb.apply_neglect(beast, now=now)
    assert contract["bond"] == 0


def test_a_bargain_bond_does_not_decay_at_all():
    """A bargain has no long claim, so its ``bond_decay`` is zero — and the
    module must not charge it anyway."""
    beast = _beast()
    contract = _contracted(beast, bond=60, kind="bargain")
    now = time.time()
    contract["bond_updated_at"] = now - (30 * ONE_DAY)
    assert sb.apply_neglect(beast, now=now) == 0
    assert contract["bond"] == 60


def test_neglect_ignores_a_severed_contract():
    beast = _beast()
    contract = _contracted(beast, bond=60, status=sb.SEVERED)
    contract["bond_updated_at"] = time.time() - (30 * ONE_DAY)
    assert sb.apply_neglect(beast, now=time.time()) == 0


def test_neglect_of_an_unknown_kind_falls_back_to_the_no_decay_kind():
    """A corrupt kind must not import the harshest decay in the table."""
    beast = _beast()
    contract = _contracted(beast, bond=60, kind="dominion")
    contract["kind"] = "corrupted"
    now = time.time()
    contract["bond_updated_at"] = now - (30 * ONE_DAY)
    assert sb.apply_neglect(beast, now=now) == 0
    assert contract["bond"] == 60


# ==========================================================================
# Growth
# ==========================================================================


def test_a_won_fight_adds_one_mark_and_never_levels_on_its_own():
    """At level 1 the cost of the next level is 1 mark, so the very first win
    does level the beast — but from level 2 up a single win must not."""
    beast = _beast(level=2)
    contract = _contracted(beast)
    result = sb.award_growth(beast, won=True)
    assert result["levels_gained"] == 0
    assert beast["level"] == 2
    assert contract["growth_xp"] == 1


def test_a_lost_fight_awards_no_growth():
    beast = _beast(level=3)
    contract = _contracted(beast)
    before = dict(beast)
    result = sb.award_growth(beast, won=False)
    assert result["levels_gained"] == 0
    assert result["growth_xp"] == 0
    assert contract["growth_xp"] == 0
    assert beast["level"] == before["level"]


def test_accumulated_marks_level_the_beast_up():
    beast = _beast(level=3)
    contract = _contracted(beast)
    contract["growth_xp"] = sb.xp_to_next_level(3) - 1
    result = sb.award_growth(beast, won=True)
    assert result["levels_gained"] == 1
    assert beast["level"] == 4
    assert result["level"] == 4


def test_growth_stops_at_the_rarity_evolution_ceiling():
    """The central anti-runaway claim: a beast cannot out-level the gate that
    decides when it evolves."""
    for rarity, req in beast_config.EVOLUTION_REQUIREMENTS.items():
        ceiling = sb.level_ceiling(rarity)
        beast = _beast(rarity=rarity, level=ceiling)
        _contracted(beast)
        result = sb.award_growth(beast, won=True)
        assert result["levels_gained"] == 0, f"{rarity} levelled past its ceiling"
        assert beast["level"] == ceiling
        assert result["level_ceiling"] == int(req["max_level"])


def test_a_beast_at_the_ceiling_accumulates_no_banked_marks():
    """Banked marks past the ceiling would fire as a burst of level-ups the
    moment the ceiling moved."""
    beast = _beast(rarity="common", level=sb.level_ceiling("common"))
    contract = _contracted(beast)
    sb.award_growth(beast, won=True)
    assert contract["growth_xp"] == 0


def test_growth_reports_xp_to_next_from_the_level_it_lands_on():
    beast = _beast(level=3)
    contract = _contracted(beast)
    contract["growth_xp"] = sb.xp_to_next_level(3) - 1
    result = sb.award_growth(beast, won=True)
    assert result["xp_to_next"] == sb.xp_to_next_level(result["level"])


def test_level_up_restates_the_stat_line_upward():
    """Stats are derived, not stored: a level that did not raise them would make
    the growth loop cosmetic."""
    beast = _beast(rarity="common", level=1)
    # Put the beast on the level-1 stat line BEFORE reading the baseline:
    # ``grant_beast`` mints its own rolled rarity's numbers, so the freshly
    # minted hp is not the level-1 "common" hp and comparing against it would
    # measure the rarity override, not the level-up.
    sb._scale_to_level(beast)
    before_hp = beast["hp"]
    contract = sb.contract_of(beast)
    contract["growth_xp"] = sb.xp_to_next_level(1)
    sb.award_growth(beast, won=True)
    assert beast["level"] == 2
    assert beast["hp"] > before_hp


def test_a_higher_rarity_at_the_same_level_has_a_stronger_stat_line():
    low = _beast(rarity="common", level=10)
    high = _beast(rarity="legendary", level=10)
    sb._scale_to_level(low)
    sb._scale_to_level(high)
    assert high["atk"] > low["atk"]


def test_stat_scaling_survives_a_beast_with_a_missing_or_zero_level():
    """A row written before the level field existed, or with it zeroed, must
    still yield a usable beast rather than raising inside a fight.

    Only the shapes a stored player row can actually hold are covered: the
    server writes ``level`` itself (``beast_config.generate_beast``), so an
    absent, null, zero or negative value is the whole set of reachable oddities.
    A non-numeric string is NOT covered: nothing in the server writes one, so
    pinning that shape here would freeze ``int()``'s ValueError as a contract.
    """
    for junk in (None, 0, -5):
        beast = _beast(rarity="common", level=1)
        if junk is None:
            beast.pop("level", None)
        else:
            beast["level"] = junk
        assert sb.beast_level(beast) >= 1
        sb._scale_to_level(beast)
        assert beast["hp"] >= 1 and beast["atk"] >= 1 and beast["def"] >= 1


def test_a_brand_new_beast_with_no_level_field_still_scales():
    beast = _beast(rarity="common", level=1)
    beast.pop("level", None)
    sb._scale_to_level(beast)
    assert beast["level"] if "level" in beast else True


# ==========================================================================
# Loyalty
# ==========================================================================


def test_a_full_bond_beast_wills_to_fight():
    beast = _beast()
    _contracted(beast, bond=sb.REFUSING_BOND)
    assert sb.will_fight(beast) is True
    assert sb.refusal_reason(beast) is None


def test_the_bond_floor_is_a_refusal_and_says_which_reason():
    """Below the floor the beast has its own agenda — the line that separates a
    bond from a summon."""
    beast = _beast()
    _contracted(beast, bond=sb.REFUSING_BOND - 1)
    assert sb.will_fight(beast) is False
    assert sb.refusal_reason(beast) == "bond_too_low"


def test_a_wounded_beast_refuses_for_a_whole_day():
    beast = _beast()
    contract = _contracted(beast, bond=sb.BOND_ON_TAME, wounded_until_day=TODAY + 1)
    assert sb.will_fight(beast) is False
    assert sb.refusal_reason(beast) == "wounded"
    contract["wounded_until_day"] = TODAY
    assert sb.will_fight(beast) is True, "the wound must expire with the day"


def test_a_severed_contract_refuses_and_says_so():
    beast = _beast()
    _contracted(beast, bond=sb.BOND_MAX, status=sb.SEVERED)
    assert sb.will_fight(beast) is False
    assert sb.refusal_reason(beast) == "severed"


def test_refusal_reasons_are_mutually_exclusive_in_a_fixed_order():
    """Severance outranks the wound, and the wound outranks the bond, so a
    player is told the most fundamental thing that is wrong."""
    beast = _beast()
    contract = _contracted(beast, bond=0, wounded_until_day=TODAY + 5, status=sb.SEVERED)
    assert sb.refusal_reason(beast) == "severed"
    contract["status"] = "active"
    assert sb.refusal_reason(beast) == "wounded"
    contract["wounded_until_day"] = 0
    assert sb.refusal_reason(beast) == "bond_too_low"


def test_a_win_raises_the_bond_by_the_modules_own_number():
    beast = _beast()
    _contracted(beast, bond=50, kind="bargain")
    expected = 50 + int(round(sb.BOND_PER_WIN * 1.0 * sb.CONTRACT_KINDS["bargain"]["bond_gain"]))
    assert sb.apply_battle_bond(beast, True, 1.0) == expected


def test_a_defeat_costs_the_fixed_chunk_and_wounds_until_tomorrow():
    """The expensive direction: a fixed loyalty bill plus a day out of the
    fight."""
    beast = _beast()
    contract = _contracted(beast, bond=sb.BOND_MAX)
    bond = sb.apply_battle_bond(beast, False, 1.0)
    assert bond == sb.BOND_MAX - sb.BOND_LOSS_ON_DEFEAT
    assert contract["wounded_until_day"] == TODAY + 1


def test_the_environment_multiplier_scales_bond_gain_but_never_the_defeat():
    """A rich vein pays more loyalty; a hostile place cannot claw back what was
    already earned."""
    def bond_after_wins(multiplier: float) -> int:
        beast = _beast()
        _contracted(beast, bond=50, kind="bargain")
        return sb.apply_battle_bond(beast, True, multiplier)

    assert bond_after_wins(sb.QI_BOND_MULTIPLIER["scarred"]) < bond_after_wins(1.0)
    assert bond_after_wins(sb.QI_BOND_MULTIPLIER["sacred"]) > bond_after_wins(1.0)

    def bond_after_defeat(multiplier: float) -> int:
        beast = _beast()
        _contracted(beast, bond=sb.BOND_MAX, kind="bargain")
        return sb.apply_battle_bond(beast, False, multiplier)

    assert bond_after_defeat(sb.QI_BOND_MULTIPLIER["scarred"]) == bond_after_defeat(1.0)


def test_the_bond_never_exceeds_the_maximum():
    beast = _beast()
    _contracted(beast, bond=sb.BOND_MAX)
    assert sb.apply_battle_bond(beast, True, sb.QI_BOND_MULTIPLIER["sacred"]) == sb.BOND_MAX


def test_the_bond_never_goes_below_zero():
    beast = _beast()
    _contracted(beast, bond=1)
    assert sb.apply_battle_bond(beast, False, 1.0) == 0


def test_a_dominion_bond_grows_faster_than_a_bargain_bond():
    """The kind roll is not decoration: it changes the numbers."""
    def gain(kind: str) -> int:
        beast = _beast()
        _contracted(beast, bond=10, kind=kind)
        return sb.apply_battle_bond(beast, True, 1.0) - 10

    assert gain("dominion") > gain("bargain")


def test_battle_stamps_the_fight_time_on_the_contract():
    beast = _beast()
    contract = _contracted(beast, bond=50)
    contract["last_fought_at"] = 0
    sb.apply_battle_bond(beast, True, 1.0)
    assert contract["last_fought_at"] > 0


# ==========================================================================
# Severance
# ==========================================================================


def test_the_severance_bill_is_never_zero():
    """A bill of zero means the cheapest way to replace a beast you dislike is
    to break the bond and tame again for free — the exact menu behaviour the
    feature exists to remove."""
    for stage in sb.CONTRACT_STAGES:
        for kind in sb.CONTRACT_KINDS:
            beast = _beast()
            _contracted(beast, stage=stage, kind=kind)
            cost = sb.severance_cost(_player(), beast)
            assert cost["cultivation_progress"] > 0, f"{stage}/{kind} severs for free"


def test_a_deeper_contract_costs_more_to_break():
    """The deeper the bond, the worse the betrayal."""
    def cost_for(stage: str) -> int:
        beast = _beast()
        _contracted(beast, stage=stage, kind="bargain")
        return sb.severance_cost(_player(), beast)["cultivation_progress"]

    ladder = [cost_for(s) for s in sb.CONTRACT_STAGES]
    assert ladder == sorted(ladder)
    assert ladder[-1] > ladder[0]


def test_an_inheritance_bond_costs_double_a_bargain_at_the_same_stage():
    beast_bargain = _beast()
    _contracted(beast_bargain, stage="bonded", kind="bargain")
    beast_inherit = _beast()
    _contracted(beast_inherit, stage="bonded", kind="inheritance")
    player = _player()
    assert (
        sb.severance_cost(player, beast_inherit)["cultivation_progress"]
        == 2 * sb.severance_cost(player, beast_bargain)["cultivation_progress"]
    )


def test_the_bill_reports_the_numbers_it_was_built_from():
    beast = _beast()
    _contracted(beast, stage="bonded", kind="dominion")
    cost = sb.severance_cost(_player(), beast)
    assert cost["stages_lost"] == sb.CONTRACT_STAGES.index("bonded") + 1
    assert cost["contract_stage"] == "bonded"
    assert cost["contract_kind"] == "dominion"
    assert cost["severance_multiplier"] == sb.CONTRACT_KINDS["dominion"]["severance_multiplier"]
    assert cost["cultivation_progress"] == cost["progress_per_stage"] * cost["stages_lost"] * cost["severance_multiplier"]


def test_the_bill_reads_the_players_own_stage_requirement():
    """The bill is ``progress_required`` from ``cultivation_config`` — the same
    number the player's own breakthroughs are made of."""
    from cultivation_config import get_stage

    beast = _beast()
    _contracted(beast, stage="bonded")
    player = _player()
    expected = float(get_stage(player["cultivation_realm"], player["cultivation_stage"]).progress_required)
    assert sb.severance_cost(player, beast)["progress_per_stage"] == int(expected)


def test_a_corrupt_stage_bills_as_the_shallowest_contract_not_a_crash():
    beast = _beast()
    _contracted(beast, stage="not-a-stage")
    cost = sb.severance_cost(_player(), beast)
    assert cost["contract_stage_index"] == 0
    assert cost["cultivation_progress"] > 0


def test_apply_severance_zeroes_the_bond_and_drops_the_beast_from_formation():
    beast = _beast()
    contract = _contracted(beast, bond=80)
    beast["in_formation"] = True
    player = _player(
        cultivation_progress=500.0,
        beast_formation=[beast["beast_id"], None, None],
    )
    outcome = sb.apply_severance(player, beast)
    assert contract["status"] == sb.SEVERED
    assert contract["bond"] == 0
    assert contract["severed_at"] is not None
    assert beast["in_formation"] is False
    assert player["beast_formation"][0] is None
    assert outcome["cultivation_progress_before"] == 500.0


def test_apply_severance_charges_exactly_the_quoted_bill():
    beast = _beast()
    _contracted(beast, stage="bonded", kind="inheritance")
    player = _player(cultivation_progress=1000.0)
    before = sb.severance_cost(player, beast)["cultivation_progress"]
    outcome = sb.apply_severance(player, beast)
    assert outcome["cultivation_progress_after"] == 1000.0 - before


def test_apply_severance_never_drives_progress_below_zero():
    beast = _beast()
    _contracted(beast, stage="transcendent", kind="inheritance")
    player = _player(cultivation_progress=1.0)
    outcome = sb.apply_severance(player, beast)
    assert outcome["cultivation_progress_after"] == 0.0


def test_apply_severance_tolerates_a_corrupt_progress_value():
    """A non-numeric progress must be treated as zero, not raise inside the
    write."""
    beast = _beast()
    _contracted(beast)
    player = _player(cultivation_progress="not-a-number")
    outcome = sb.apply_severance(player, beast)
    assert outcome["cultivation_progress_after"] == 0.0
    assert isinstance(player["cultivation_progress"], float)


def test_apply_severance_leaves_a_missing_formation_untouched():
    beast = _beast()
    _contracted(beast)
    player = _player(cultivation_progress=100.0)
    player.pop("beast_formation")
    sb.apply_severance(player, beast)
    assert "beast_formation" not in player or player["beast_formation"] is None


# ==========================================================================
# Environment multiplier
# ==========================================================================


@pytest.mark.parametrize("zone", sorted(sb.QI_BOND_MULTIPLIER))
def test_every_declared_zone_resolves_to_its_own_multiplier(zone):
    assert sb.qi_bond_multiplier(zone) == sb.QI_BOND_MULTIPLIER[zone]


def test_a_zone_name_is_matched_without_regard_to_case_or_padding():
    assert sb.qi_bond_multiplier("  SACRED ") == sb.QI_BOND_MULTIPLIER["sacred"]


def test_an_unknown_or_missing_zone_is_ordinary_ground_not_scarred():
    """A lookup miss must not silently impose the harshest multiplier in the
    table."""
    for probe in (None, "", "somewhere-new", 0):
        assert sb.qi_bond_multiplier(probe) == sb.DEFAULT_QI_BOND_MULTIPLIER
        assert sb.qi_bond_multiplier(probe) != sb.QI_BOND_MULTIPLIER["scarred"]


def test_scarred_qi_is_the_harshest_and_sacred_the_kindest():
    values = sb.QI_BOND_MULTIPLIER.values()
    assert max(values) == sb.QI_BOND_MULTIPLIER["sacred"]
    assert min(values) == sb.QI_BOND_MULTIPLIER["scarred"]


# ==========================================================================
# Companion stat block (the beast's contribution to a fight)
# ==========================================================================


def test_the_companion_swings_at_least_one_attack():
    """A zero-attack companion would silently drop out of the turn loop."""
    for bond in (0, sb.REFUSING_BOND, sb.BOND_ON_TAME, sb.BOND_MAX):
        beast = _beast()
        _contracted(beast, bond=bond)
        assert sb.companion_stat_block(beast, _player())["atk"] >= 1


def test_a_stronger_bond_makes_a_stronger_swing():
    """The attack is scaled by the bond, so the bond is not a collectible."""
    def atk_for(bond: int) -> int:
        beast = _beast(rarity="common", level=5)
        _contracted(beast, bond=bond)
        return sb.companion_stat_block(beast, _player())["atk"]

    assert atk_for(sb.BOND_MAX) > atk_for(0)


def test_the_attack_is_derived_from_the_player_not_from_a_fixed_number():
    """The rival in the fight is derived from the player, so the companion must
    be too — otherwise a levelled player fights with a paper-thin ally."""
    def atk_for(player_atk: int) -> int:
        beast = _beast(rarity="common", level=1)
        _contracted(beast, bond=sb.BOND_ON_TAME)
        return sb.companion_stat_block(beast, _player(atk=player_atk))["atk"]

    assert atk_for(400) > atk_for(100)


def test_the_attack_is_capped_as_a_share_of_the_player():
    """The safety rail that stops an over-invested legendary bloodline companion
    trivialising the fight it stands in."""
    ceiling_share = sb.COMPANION_ATTACK_CAP_SHARE
    for rarity in beast_config.BEAST_RARITY:
        beast = _beast(rarity=rarity, level=100, blood_level=4)
        _contracted(beast, bond=sb.BOND_MAX)
        player = _player(atk=100)
        atk = sb.companion_stat_block(beast, player)["atk"]
        assert atk <= int(ceiling_share * player["atk"])


def test_rarity_enters_sublinearly_not_as_the_raw_multiplier():
    """A common companion must not be a rounding error next to a legendary one;
    the square-root exponent is what stops the 3x spread compounding."""
    def atk_for(rarity: str) -> int:
        beast = _beast(rarity=rarity, level=1)
        _contracted(beast, bond=sb.BOND_ON_TAME)
        return sb.companion_stat_block(beast, _player())["atk"]

    assert atk_for("legendary") > atk_for("common")
    raw_spread = beast_config.BEAST_RARITY["legendary"]["stat_mult"] / beast_config.BEAST_RARITY["common"]["stat_mult"]
    measured_spread = atk_for("legendary") / max(1, atk_for("common"))
    assert measured_spread < raw_spread


def test_level_growth_is_sublinear():
    """Doubling the level must not double the swing."""
    def atk_for(level: int) -> int:
        beast = _beast(rarity="common", level=level)
        _contracted(beast, bond=sb.BOND_ON_TAME)
        return sb.companion_stat_block(beast, _player())["atk"]

    # Levels 1 -> 5 is a 5x level jump; a linear table would give a 5x swing.
    assert atk_for(5) < atk_for(1) * 5


def test_bloodline_attunement_moves_the_swing_upward():
    def atk_for(blood: int) -> int:
        beast = _beast(rarity="rare", level=1, blood_level=blood)
        _contracted(beast, bond=sb.BOND_MAX)
        return sb.companion_stat_block(beast, _player())["atk"]

    assert atk_for(1) > atk_for(0)


def test_a_corrupt_element_or_rarity_does_not_crash_the_stat_block():
    beast = _beast(rarity="not-a-rarity", level=1, element=None, blood_level=None)
    _contracted(beast, bond=sb.BOND_ON_TAME)
    block = sb.companion_stat_block(beast, _player(atk=0))
    assert block["atk"] >= 1
    assert block["hp"] >= 1
    assert isinstance(block["element"], str)


def test_the_stat_block_reports_the_contract_it_was_built_from():
    beast = _beast(rarity="rare", level=7, blood_level=2)
    _contracted(beast, bond=60)
    block = sb.companion_stat_block(beast, _player())
    assert block["level"] == 7
    assert block["bond"] == 60
    assert block["rarity"] == "rare"


# ==========================================================================
# Presentation
# ==========================================================================


def test_the_companion_view_publishes_the_thresholds_it_enforces():
    """A panel must read the server's numbers, not re-derive them."""
    beast = _beast(rarity="common", level=1)
    _contracted(beast, bond=sb.BOND_ON_TAME)
    view = sb.companion_view(beast, _player())
    assert view["contract"]["bond_max"] == sb.BOND_MAX
    assert view["contract"]["refusing_bond"] == sb.REFUSING_BOND
    assert view["growth"]["level_ceiling"] == sb.level_ceiling("common")


def test_the_companion_view_will_fight_flag_agrees_with_the_refusal():
    """Two answers to the same question must never disagree."""
    for bond, status in (
        (sb.BOND_ON_TAME, "active"),
        (sb.REFUSING_BOND - 1, "active"),
        (sb.BOND_MAX, sb.SEVERED),
    ):
        beast = _beast()
        _contracted(beast, bond=bond, status=status)
        view = sb.companion_view(beast, _player())
        assert view["contract"]["will_fight"] is sb.will_fight(beast)


def test_the_companion_view_reports_at_ceiling_at_the_gate():
    beast = _beast(rarity="common", level=1)
    _contracted(beast)
    assert sb.companion_view(beast, _player())["growth"]["at_ceiling"] is False
    beast["level"] = sb.level_ceiling("common")
    assert sb.companion_view(beast, _player())["growth"]["at_ceiling"] is True


def test_a_breakthrough_deepens_the_bond_the_player_already_holds():
    """The stored stage is not trusted on a read; it is re-derived from the
    realm, so reaching a new realm deepens the existing contract instead of
    leaving it at "initial" forever."""
    beast = _beast()
    _contracted(beast, stage=sb.CONTRACT_STAGES[0])
    view = sb.companion_view(beast, _player(cultivation_realm="nascent_soul"))
    assert view["contract"]["stage"] == sb.contract_stage_for("nascent_soul")
    assert beast["contract"]["stage"] == sb.contract_stage_for("nascent_soul")


def test_a_severed_contract_keeps_the_stage_it_died_at():
    """Re-deriving the stage upward after a breakthrough would be a second
    charge for the same betrayal."""
    beast = _beast()
    _contracted(beast, stage="deepened", status=sb.SEVERED)
    view = sb.companion_view(beast, _player(cultivation_realm="nascent_soul"))
    assert view["contract"]["stage"] == "deepened"


def test_the_severance_bill_in_the_view_is_the_same_bill_the_release_charges():
    beast = _beast()
    _contracted(beast, stage="bonded", kind="inheritance")
    player = _player()
    assert sb.companion_view(beast, player)["severance"] == sb.severance_cost(player, beast)


def test_the_roster_lists_live_and_severed_companions_and_publishes_the_cap():
    player = _player()
    live = _beast(player)
    _contracted(live)
    dead = _beast(player)
    _contracted(dead, status=sb.SEVERED)
    player["beasts"] = [live, dead]
    roster = sb.roster_view(player)
    assert len(roster["companions"]) == 2
    assert roster["formation_slots"] == beast_config.FORMATION_SLOTS
    assert roster["refusing_bond"] == sb.REFUSING_BOND
    assert roster["contract_stages"] == sb.CONTRACT_STAGES


def test_the_roster_of_a_player_with_no_beasts_is_empty_not_absent():
    roster = sb.roster_view(_player())
    assert roster["companions"] == []
    assert roster["formation"] == [None, None, None]


def test_the_roster_tolerates_a_missing_or_empty_beast_list():
    """A player row written before beasts existed must not 500 the panel."""
    assert sb.roster_view(_player(beasts=None))["companions"] == []
    assert sb.roster_view(_player(beasts=[]))["companions"] == []
    assert sb.roster_view({"id": "p_x"})["companions"] == []


def test_a_stored_row_that_is_a_dict_but_not_a_real_beast_still_renders():
    """``roster_view`` filters with ``isinstance(b, dict)``, so ANY dict in the
    roster is rendered. That is deliberate rather than an oversight: a dict with
    no beast id or no contract is a row written by an older migration, and it
    must come back as a readable companion on a starting bond rather than
    crash the panel or silently vanish.

    The non-dict case (a bare string or int in the roster) raises inside
    ``apply_neglect`` before the filter runs. Nothing in the server writes one,
    so that shape is deliberately NOT pinned here.
    """
    legacy = {"no": "id", "no": "contract"}
    roster = sb.roster_view(_player(beasts=[legacy]))
    assert len(roster["companions"]) == 1
    view = roster["companions"][0]
    assert view["beast_id"] is None
    assert view["contract"]["bond"] == sb.BOND_ON_TAME
    assert view["contract"]["status"] == "active"


def test_reading_the_roster_applies_neglect_so_a_stale_bond_is_not_published():
    """A panel that shows a bond nobody has charged is a lie."""
    player = _player()
    beast = _beast(player)
    contract = _contracted(beast, bond=90, kind="dominion")
    contract["bond_updated_at"] = time.time() - (40 * ONE_DAY)
    player["beasts"] = [beast]
    roster = sb.roster_view(player)
    assert roster["companions"][0]["contract"]["bond"] < 90
    assert contract["bond"] == roster["companions"][0]["contract"]["bond"]


# ==========================================================================
# The named companion
# ==========================================================================


def test_a_beast_name_is_stable_across_calls():
    beast = _beast()
    assert sb._beast_name(beast) == sb._beast_name(beast)


def test_a_beast_name_comes_from_the_declared_name_pool():
    beast = _beast()
    assert sb._beast_name(beast) in sb.BEAST_NAMES


def test_different_beast_ids_land_on_different_names_often_enough():
    """A deterministic digest that never varied would make every companion the
    same character."""
    names = {sb._beast_name({"beast_id": f"beast_{i}"}) for i in range(200)}
    assert len(names) > 1


def test_a_beast_with_no_id_still_gets_a_name():
    assert sb._beast_name({}) in sb.BEAST_NAMES


# ==========================================================================
# The exported surface
# ==========================================================================


def test_everything_in_dunder_all_actually_exists():
    """A stale export is an ImportError waiting for the next caller."""
    for name in sb.__all__:
        assert hasattr(sb, name), f"__all__ advertises missing name {name}"


def test_the_taming_request_cannot_name_a_rarity():
    """The client must not be able to buy a high rarity — the single most
    important server-authority claim in the module."""
    fields = set(sb.BeastTameCompanionRequest.model_fields)
    assert "rarity" not in fields
    assert "beast_id" not in fields
    assert "player_id" in fields


def test_the_fight_and_release_requests_require_a_player_and_a_beast():
    for model in (sb.BeastBondRequest, sb.BeastReleaseRequest):
        fields = set(model.model_fields)
        assert {"player_id", "beast_id"} <= fields


def test_the_release_request_does_not_accept_a_confirmed_flag():
    """Breaking a contract is irreversible; a body flag must not be able to
    wave it through."""
    assert "confirm" not in sb.BeastReleaseRequest.model_fields


# ==========================================================================
# Request bodies: what the server ACCEPTS, and what it silently discards
#
# The suites above pin the numbers; these pin the SHAPE of what a client may
# send. A request model is the only place a client can reach the server, so
# two things matter: a field the model does not declare cannot influence the
# outcome, and a value the model rejects never reaches the handler at all.
# ==========================================================================


def test_a_body_may_not_carry_an_undeclared_field_into_the_model():
    """An extra body field is DROPPED, not honoured.

    This is the mechanism behind every server-authority claim in the module:
    a client that sends ``rarity`` gets a request object that has never heard
    of rarity, so the field cannot reach the taming roll.
    """
    model = sb.BeastReleaseRequest(
        player_id="p_owner", beast_id="b_1", rarity="legendary", bond=999, confirm=True
    )
    dumped = model.model_dump()
    assert set(dumped) == set(sb.BeastReleaseRequest.model_fields)
    assert "rarity" not in dumped
    assert "bond" not in dumped
    assert "confirm" not in dumped


def test_the_taming_body_drops_an_attempted_rarity_and_target():
    """The named-version of the server-authority claim, on the body itself."""
    model = sb.BeastTameCompanionRequest(
        player_id="p_owner", items_used=2, rarity="legendary", beast_id="someone_elses"
    )
    assert model.model_dump() == {"player_id": "p_owner", "items_used": 2}


@pytest.mark.parametrize("model", [sb.BeastTameCompanionRequest, sb.BeastBondRequest, sb.BeastReleaseRequest])
def test_every_request_body_rejects_a_missing_player_id(model):
    """A body with no player to act for must fail validation, not default to
    some other player."""
    payload = {"beast_id": "b_1"} if model is not sb.BeastTameCompanionRequest else {"items_used": 1}
    with pytest.raises(Exception):
        model(**payload)


@pytest.mark.parametrize("bad_player_id", [5, None, 2.7])
def test_a_player_id_that_is_not_a_string_is_rejected(bad_player_id):
    """``player_id: str`` is a real constraint here, not a documentation note.

    A numeric id would be coerced by some pydantic configurations and then used
    as a dictionary key, which is exactly the shape that lets one player's row
    be addressed by another's number.
    """
    with pytest.raises(Exception):
        sb.BeastBondRequest(player_id=bad_player_id, beast_id="b_1")


def test_an_empty_player_id_passes_the_model_because_the_model_is_only_a_type_check():
    """Measured, and deliberately recorded rather than asserted away.

    ``player_id: str`` rejects a number but happily accepts ``""`` — a type
    check is not an emptiness check. The empty id is stopped one layer out, by
    the store lookup that answers "Player not found", not by the body model.
    So this file pins the boundary honestly: a missing, null or wrong-TYPED
    player id is refused at validation, and a wrong-VALUED one is not.
    """
    assert sb.BeastBondRequest(player_id="", beast_id="b_1").player_id == ""
    assert sb.BeastTameCompanionRequest(player_id="").player_id == ""


@pytest.mark.parametrize("model", [sb.BeastBondRequest, sb.BeastReleaseRequest])
def test_the_body_routes_require_a_beast_id_too(model):
    with pytest.raises(Exception):
        model(player_id="p_owner")


def test_the_taming_item_count_defaults_to_zero_and_needs_no_body_field():
    """``items_used`` is optional and lands on zero, which the route refuses.

    The refusal lives in the route; what this pins is that an OMITTED count
    cannot silently mean "as many as you like".
    """
    assert sb.BeastTameCompanionRequest(player_id="p_owner").items_used == 0


@pytest.mark.parametrize("bad_count", [None, 2.7, "not-a-number"])
def test_a_non_integer_item_count_is_rejected_by_the_model(bad_count):
    with pytest.raises(Exception):
        sb.BeastTameCompanionRequest(player_id="p_owner", items_used=bad_count)


def test_the_model_does_not_clamp_the_item_count_because_the_route_does():
    """The model is a TYPE check, not a limit.

    ``-5`` and ``10**9`` both survive model validation here; the route is what
    clamps them to ``[0, TAMING_MAX_ITEMS]``. Pinning that split matters: a
    future change that adds ``Field(ge=0, le=3)`` would turn today's
    route-level 400 into a model-level 422, and this test is what notices.
    """
    assert sb.BeastTameCompanionRequest(player_id="p_owner", items_used=-5).items_used == -5
    assert sb.BeastTameCompanionRequest(player_id="p_owner", items_used=10**9).items_used == 10**9


def test_the_release_body_carries_a_client_version_that_defaults_to_zero():
    """Present and defaulted: the field the release route declares."""
    assert sb.BeastReleaseRequest(player_id="p_owner", beast_id="b_1").client_version == 0


# ==========================================================================
# Degraded rows: values a stored player row can hold that are not the happy
# shape. Each of these was MEASURED against the live module before being
# written down, so none of them freezes a guess.
# ==========================================================================


def test_a_roster_whose_formation_key_is_null_reports_the_empty_formation():
    """``beast_formation`` set to ``None`` is NOT the same as the key missing.

    ``player.get("beast_formation", [None, None, None])`` substitutes the
    default only when the key is ABSENT, so a stored null used to be
    published as ``None`` while a missing key published three slots. A panel
    that indexes ``formation[0]`` raises on one and not the other, so the two
    rows were indistinguishable to the server and fatal to half of them.

    The published view now applies the same ``isinstance(..., list)`` guard the
    module's own ``_active`` helper already used, so both degraded rows answer
    with the empty formation, and the number of empty slots is read from
    ``beast_config.FORMATION_SLOTS`` — the same number the reply publishes
    beside it as ``formation_slots``, so a retune cannot desynchronise them.
    """
    empty = [None] * beast_config.FORMATION_SLOTS
    player = _player(beasts=[])
    player["beast_formation"] = None
    roster = sb.roster_view(player)
    assert roster["formation"] == empty
    assert len(roster["formation"]) == roster["formation_slots"]

    player.pop("beast_formation")
    assert sb.roster_view(player)["formation"] == empty


def test_a_formation_stored_as_something_other_than_a_list_reads_as_empty():
    """The guard is on the TYPE, not on ``None`` alone.

    A row written by an older or corrupt writer can hold a string or a dict in
    this field. The internal ``_active`` helper refuses every non-list, so the
    published view does too — otherwise a dict formation would reach a panel
    that indexes slots and get subscripted on a key the player never typed.
    """
    for stored in ("beast_1", {"0": "beast_1"}, 3, (None, None, None)):
        player = _player(beasts=[])
        player["beast_formation"] = stored
        roster = sb.roster_view(player)
        assert roster["formation"] == [None] * beast_config.FORMATION_SLOTS


def test_a_real_formation_is_published_untouched():
    """The guard replaces junk, never a genuine list of slots."""
    held = [None, None, None]
    player = _player(beasts=[])
    beast = _beast(player)
    _contracted(beast)
    held[0] = beast["beast_id"]
    player["beast_formation"] = held
    roster = sb.roster_view(player)
    assert roster["formation"] == held
    assert roster["formation"][0] == beast["beast_id"]


def test_an_unknown_rarity_beast_is_pinned_at_level_one_and_reports_it_is_at_the_ceiling():
    """A corrupt rarity cannot grant free growth.

    ``level_ceiling`` returns 1 for an unrecognised rarity, so the growth loop
    has nowhere to go and the view says the beast is already at its ceiling —
    a broken row stops growing rather than growing without limit.
    """
    beast = _beast(rarity="not-a-real-rarity", level=1)
    _contracted(beast)
    assert sb.level_ceiling("not-a-real-rarity") == 1

    result = sb.award_growth(beast, won=True)
    assert result["levels_gained"] == 0
    assert result["level_ceiling"] == 1
    assert sb.companion_view(beast, _player())["growth"]["at_ceiling"] is True


def test_a_corrupt_contract_kind_presents_as_the_no_decay_kind():
    """A kind the module does not know must present with a real label, not a
    KeyError in the middle of building a panel."""
    beast = _beast()
    contract = _contracted(beast, kind="dominion")
    contract["kind"] = "corrupted"
    block = sb.companion_view(beast, _player())["contract"]
    assert block["kind"] == "corrupted"
    assert block["kind_label"] == sb.CONTRACT_KINDS["bargain"]["label"]
    assert sb.severance_cost(_player(), beast)["cultivation_progress"] > 0


def test_a_player_row_with_no_cultivation_progress_key_is_charged_as_zero():
    """The absent key and the corrupt-string key behave the same way, and both
    write a real float back so the next severance reads a number."""
    beast = _beast()
    _contracted(beast, stage="bonded")
    player = _player()
    player.pop("cultivation_progress", None)
    outcome = sb.apply_severance(player, beast)
    assert outcome["cultivation_progress_after"] == 0.0
    assert isinstance(player["cultivation_progress"], float)


def test_a_beast_row_with_a_zero_or_null_hp_still_yields_a_usable_fighter():
    """``hp`` is written by the server, so zero and null are the reachable
    oddities; neither may reach the simulator as a zero-health companion."""
    for junk in (0, None):
        beast = _beast()
        _contracted(beast, bond=sb.BOND_ON_TAME)
        beast["hp"] = junk
        beast["atk"] = junk
        block = sb.companion_stat_block(beast, _player(atk=0))
        assert block["hp"] >= 1
        assert block["atk"] >= 1


def test_a_player_row_with_a_zero_attack_still_produces_a_companion_attack():
    """A stored ``atk`` of zero must not zero the companion's swing."""
    beast = _beast(rarity="common", level=1)
    _contracted(beast, bond=sb.BOND_ON_TAME)
    assert sb.companion_stat_block(beast, _player(atk=0))["atk"] >= 1


def test_a_truthy_non_boolean_in_formation_flag_publishes_as_true():
    """``in_formation`` is read with ``bool(...)``, so a row holding ``1`` or
    ``"yes"`` from an older writer still publishes a real boolean — the view's
    declared type is not something the stored value can break."""
    for stored in (1, "yes"):
        player = _player()
        beast = _beast(player)
        _contracted(beast)
        beast["in_formation"] = stored
        player["beasts"] = [beast]
        player["beast_formation"] = [beast["beast_id"], None, None]
        view = sb.roster_view(player)["companions"][0]
        assert view["in_formation"] is True


# ==========================================================================
# Non-finite stored values
#
# A beast row is JSON on disk, so a single hand-edited or corrupt field must
# not reach the simulator. The dangerous spellings are NOT the ones that raise:
# ``float("inf")`` and ``float("nan")`` both succeed silently and then poison
# every arithmetic step downstream. The module's two readers (``_stored_float``
# and ``_stored_int``) are the only thing standing between a corrupt field and
# a 500 — and they had no test at all.
#
# Every value below was MEASURED against the live module before being written
# down. Each one reads as its documented default rather than as the poison
# value, which is what "fails closed" means here.
# ==========================================================================

NON_FINITE = (float("nan"), float("inf"), float("-inf"))


def test_stored_float_answers_the_default_for_every_non_finite_spelling():
    """``float()`` returns ``inf``/``nan`` without raising, so the guard has to
    be an explicit finiteness test rather than an ``except`` clause."""
    for value in NON_FINITE + ("inf", "-inf", "nan", 1e400):
        assert sb._stored_float(value, -1.0) == -1.0


def test_stored_int_answers_the_default_for_every_non_finite_spelling():
    """``int(inf)`` raises ``OverflowError`` and ``int(nan)`` raises
    ``ValueError`` — two different exceptions, neither already handled by a bare
    ``int()``. And ``1e400`` is finite yet still too large for ``int()``."""
    for value in NON_FINITE + ("inf", "-inf", "nan", 1e400):
        assert sb._stored_int(value, -1) == -1


def test_stored_helpers_keep_the_default_for_unreadable_values_too():
    """The default is what a missing key already gets, so a corrupt field and an
    absent one read the same. Fail closed: the row survives for the player to
    see and a repair to rewrite."""
    for value in (None, "not-a-number", object()):
        assert sb._stored_float(value, -1.0) == -1.0
        assert sb._stored_int(value, -1) == -1


def test_stored_helpers_pass_a_real_number_through_unchanged():
    """A guard that swallowed good data would be as broken as one that let the
    poison through."""
    assert sb._stored_float(12.5, -1.0) == 12.5
    assert sb._stored_int(12.9, -1) == 12


@pytest.mark.parametrize("junk", NON_FINITE)
def test_a_non_finite_bond_makes_the_beast_refuse_rather_than_swing(junk):
    """A corrupt loyalty must not read as an enormous one.

    Fails closed and visibly: the beast declines to fight and says why, instead
    of attacking with an infinite bond or vanishing into a 500.
    """
    beast = _beast()
    _contracted(beast, bond=junk)
    assert sb.will_fight(beast) is False
    assert sb.refusal_reason(beast) == "bond_too_low"


@pytest.mark.parametrize("junk", NON_FINITE)
def test_a_non_finite_bond_is_published_as_zero_and_not_as_the_poison_value(junk):
    """The panel reads the bond straight off the contract, so a corrupt value
    would otherwise be published verbatim to the client."""
    beast = _beast()
    _contracted(beast, bond=junk)
    published = sb.companion_view(beast, _player())["contract"]["bond"]
    assert published == 0
    assert math.isfinite(published)


@pytest.mark.parametrize("junk", NON_FINITE)
def test_a_non_finite_growth_bank_awards_no_free_levels(junk):
    """Banked marks are read through the guard, so a corrupt bank reads as zero
    and the beast earns its next mark the honest way."""
    beast = _beast(rarity="common", level=3)
    contract = _contracted(beast)
    contract["growth_xp"] = junk
    result = sb.award_growth(beast, won=True)
    assert result["levels_gained"] == 0
    assert beast["level"] == 3
    assert math.isfinite(result["growth_xp"])


@pytest.mark.parametrize("junk", NON_FINITE)
def test_a_non_finite_level_pins_the_beast_to_the_first_level(junk):
    """A corrupt level is read as 1 — the floor — so a broken row stops growing
    instead of growing without limit or raising inside the fight."""
    beast = _beast(rarity="common", level=1)
    beast["level"] = junk
    assert sb.beast_level(beast) == 1


@pytest.mark.parametrize("junk", NON_FINITE)
def test_restating_a_beast_with_a_non_finite_level_yields_a_finite_stat_line(junk):
    """The restatement writes ``hp``/``atk``/``def`` back onto the row, so a
    non-finite level reaching it would write non-finite stats onto the row that
    the next read publishes."""
    beast = _beast(rarity="common", level=1)
    beast["level"] = junk
    sb._scale_to_level(beast)
    for field in ("hp", "atk", "def"):
        assert math.isfinite(beast[field]), f"{field} went non-finite"
        assert beast[field] >= 1


@pytest.mark.parametrize("junk", NON_FINITE)
def test_a_non_finite_progress_is_charged_as_zero_not_written_back(junk):
    """``max(0.0, inf - cost)`` is ``inf``, so without the guard the poison value
    would be written straight back onto the player's saved row."""
    beast = _beast()
    _contracted(beast, stage="bonded")
    player = _player(cultivation_progress=junk)
    outcome = sb.apply_severance(player, beast)
    assert outcome["cultivation_progress_after"] == 0.0
    assert math.isfinite(player["cultivation_progress"])


def test_a_non_finite_bond_timestamp_charges_no_neglect():
    """A corrupt timestamp reads as "no time has passed" rather than as a 500
    or as a centuries-old neglect bill."""
    beast = _beast()
    contract = _contracted(beast, bond=90, kind="dominion")
    contract["bond_updated_at"] = float("inf")
    assert sb.apply_neglect(beast, now=time.time()) == 0
    assert contract["bond"] == 90


def test_a_non_finite_bond_timestamp_in_the_roster_does_not_empty_the_roster():
    """The roster applies neglect on read, so this is the path a panel actually
    takes — the corrupt value must not cost the player their companion."""
    player = _player()
    beast = _beast(player)
    contract = _contracted(beast, bond=90, kind="dominion")
    contract["bond_updated_at"] = float("nan")
    player["beasts"] = [beast]
    roster = sb.roster_view(player)
    assert len(roster["companions"]) == 1
    assert roster["companions"][0]["contract"]["bond"] == 90


# ==========================================================================
# Determinism
#
# "Deterministic" here means: the same inputs always produce the same output,
# with no dependence on wall-clock time, row order, or a previous call. That is
# what makes a stored row reproducible and a client's prediction trustworthy.
# The rarity is PINNED in these tests because ``grant_beast`` rolls it.
# ==========================================================================


@pytest.mark.parametrize("rarity", sorted(beast_config.BEAST_RARITY))
def test_restating_a_beast_is_deterministic_for_a_pinned_rarity(rarity):
    """Two beasts with the same rarity and level must restate to the same line.

    The one input a caller does not control is ``rarity`` — it is rolled — so
    pinning it is what makes this a determinism test rather than a rarity test.
    """
    first = _beast(rarity=rarity, level=7)
    second = _beast(rarity=rarity, level=7)
    sb._scale_to_level(first)
    sb._scale_to_level(second)
    for field in ("hp", "atk", "def"):
        assert first[field] == second[field], f"{field} is not deterministic for {rarity}"


@pytest.mark.parametrize("rarity", sorted(beast_config.BEAST_RARITY))
def test_restating_a_beast_twice_changes_nothing(rarity):
    """Restatement is a restatement, not an increment: running it again must not
    compound. This is what lets a caller re-derive stats without fear of drift.
    """
    beast = _beast(rarity=rarity, level=7)
    sb._scale_to_level(beast)
    before = {field: beast[field] for field in ("hp", "atk", "def")}
    sb._scale_to_level(beast)
    assert {field: beast[field] for field in ("hp", "atk", "def")} == before


def test_awarding_growth_is_deterministic_from_the_same_starting_state():
    """The growth loop has exactly one input that could vary (the ``won`` flag)
    and one that could drift (the banked marks). Same start, same answer."""
    def grow() -> dict:
        beast = _beast(rarity="common", level=2)
        _contracted(beast)
        return sb.award_growth(beast, won=True)

    assert grow() == grow()


def test_the_level_ceiling_is_the_same_number_every_time_it_is_asked():
    """The ceiling gates growth. A caller that read two different numbers from it
    would produce two different beasts."""
    for rarity in beast_config.EVOLUTION_REQUIREMENTS:
        answers = {sb.level_ceiling(rarity) for _ in range(5)}
        assert len(answers) == 1


def test_a_corrupt_kind_charges_the_same_bill_every_time():
    """The corrupt-kind fallback must be stable, not re-rolled per call."""
    def bill() -> int:
        beast = _beast()
        contract = _contracted(beast, stage="bonded", kind="dominion")
        contract["kind"] = "corrupted"
        return sb.severance_cost(_player(), beast)["cultivation_progress"]

    assert len({bill() for _ in range(5)}) == 1


def test_a_beast_name_depends_only_on_its_id_and_not_on_the_clock():
    """The name is a digest of the id, so it is stable across calls, across
    processes, and across days — a client may show it before the row loads."""
    beast = _beast()
    first = sb._beast_name(beast)
    assert all(sb._beast_name(beast) == first for _ in range(5))
    assert sb._beast_name({"beast_id": beast["beast_id"]}) == first


# ==========================================================================
# Persistence
#
# A beast lives inside the player's saved row as JSON, so "persistent" means
# two things: the module writes its changes into the row it was handed (rather
# than into a copy that is thrown away), and those changes survive the
# round-trip the storage layer performs.
# ==========================================================================


def test_a_bond_change_is_written_into_the_caller_s_row_not_into_a_copy():
    """If the module returned a new dict instead of mutating in place, every
    battle would silently discard the loyalty it just earned."""
    beast = _beast()
    contract = _contracted(beast, bond=50, kind="bargain")
    assert sb.contract_of(beast) is contract, "the module copied the contract"
    sb.apply_battle_bond(beast, True, 1.0)
    assert contract["bond"] > 50
    assert beast["contract"]["bond"] == contract["bond"]


def test_a_bond_survives_the_json_round_trip_that_saves_the_row():
    """The row is serialised to JSON and read back on the next request. A value
    that only existed in memory has not been saved."""
    beast = _beast(rarity="common", level=1)
    contract = _contracted(beast, kind="bargain")
    sb.apply_battle_bond(beast, True, 1.0)
    sb.apply_battle_bond(beast, True, 1.0)
    reloaded = json.loads(json.dumps({"beasts": [beast]}))["beasts"][0]
    assert sb.contract_of(reloaded)["bond"] == contract["bond"] > sb.BOND_ON_TAME


def test_a_severance_survives_the_json_round_trip_and_stays_irreversible():
    """Breaking a contract must still be broken after the row is saved — this is
    the whole claim of the severance bill."""
    beast = _beast(rarity="common", level=1)
    _contracted(beast, bond=80)
    sb.apply_severance(_player(cultivation_progress=1000.0), beast)
    reloaded = json.loads(json.dumps({"beasts": [beast]}))["beasts"][0]
    contract = sb.contract_of(reloaded)
    assert contract["status"] == sb.SEVERED
    assert contract["bond"] == 0
    assert sb.will_fight(reloaded) is False


def test_a_bond_read_back_from_storage_is_the_number_that_was_written():
    """Not merely "a plausible bond" — the same one. This is what makes the
    client's cached view safe to trust."""
    beast = _beast(rarity="common", level=1)
    contract = _contracted(beast, kind="bargain")
    sb.apply_battle_bond(beast, True, 1.0)
    written = contract["bond"]
    reloaded = json.loads(json.dumps({"beasts": [beast]}))["beasts"][0]
    assert sb.contract_of(reloaded)["bond"] == written
    assert sb.roster_view(_player(beasts=[reloaded]))["companions"][0]["contract"]["bond"] == written


def test_a_fresh_read_does_not_re_roll_the_contract_a_row_already_carries():
    """``new_contract`` rolls a kind. If a read ever called it on a beast that
    already had a contract, the player's bond would silently change kind —
    changing its gain, its decay and its severance bill."""
    beast = _beast()
    contract = _contracted(beast, bond=77)
    original_kind = contract["kind"]
    for _ in range(50):
        sb.contract_of(beast)
        sb.roster_view(_player(beasts=[beast]))
        sb.companion_view(beast, _player())
    assert sb.contract_of(beast)["kind"] == original_kind
    assert sb.contract_of(beast)["bond"] == 77


def test_a_persisted_bond_is_bounded_after_a_round_trip():
    """The stored value must still be inside the published range, or a panel
    would render a bar wider than its own track."""
    beast = _beast(rarity="common", level=1)
    _contracted(beast, bond=sb.BOND_MAX, kind="bargain")
    sb.apply_battle_bond(beast, True, sb.QI_BOND_MULTIPLIER["sacred"])
    reloaded = json.loads(json.dumps({"beasts": [beast]}))["beasts"][0]
    bond = sb.contract_of(reloaded)["bond"]
    assert 0 <= bond <= sb.BOND_MAX
    assert math.isfinite(bond)


# ==========================================================================
# Authorization, at the layer this file can actually reach
#
# The session guard and the 401 live in the mounted app and are already covered
# by ``test_beast_routes_contract.py`` and ``test_spirit_beast_companion.py``.
# What is left for THIS file is the pure layer's half of the same claim:
#
# * a body that names no player cannot be built, so there is no "act as anyone"
#   request to hand to a handler; and
# * the ownership lookup returns the same answer for "not yours" as for "does
#   not exist", which is what stops a caller probing for someone else's beast
#   by guessing ids.
# ==========================================================================


@pytest.mark.parametrize("model", [sb.BeastTameCompanionRequest, sb.BeastBondRequest, sb.BeastReleaseRequest])
def test_no_write_body_can_be_built_without_naming_a_player(model):
    """The player is named in the BODY, not taken from the URL and not defaulted.

    So a request the module is willing to act on always names its actor, and the
    handler's ownership check has a subject to check.
    """
    payload = {"beast_id": "b_1"}
    if model is sb.BeastTameCompanionRequest:
        payload = {"items_used": 1}
    with pytest.raises(Exception):
        model(**payload)


def test_a_new_contract_stamps_the_acting_player_and_takes_no_other_actor():
    """The contract's owner is read off the player row the server loaded. There
    is no field anywhere in it a client could have supplied.
    """
    contract = sb.new_contract(_player(id="p_acting"), _beast())
    assert contract["bonds_with"] == "p_acting"
    assert "player_id" not in contract
    assert "owner" not in contract


def test_a_player_row_with_no_id_yields_a_contract_that_names_nobody():
    """Measured, and recorded rather than asserted away.

    A player row with no ``id`` produces a contract whose ``bonds_with`` is
    ``None``. The route cannot reach that state — it 404s an unknown player
    before minting anything — but the pure function has no guard of its own, so
    this file pins the honest boundary: the ``None`` is visible in the contract
    rather than silently filled with some other player's id.
    """
    contract = sb.new_contract({"cultivation_realm": "mortal"}, _beast())
    assert contract["bonds_with"] is None


def test_an_absent_beast_and_a_foreign_beast_are_indistinguishable():
    """The anti-enumeration property: a caller guessing ids learns nothing,
    because "no such beast" and "not yours" are the same answer.

    A handler that told the two apart would confirm the existence of another
    player's companion to anyone who asked.
    """
    owner = _player(id="p_owner")
    foreign = _beast(owner)
    foreign["owner_id"] = "p_owner"
    mine = _beast(_player(id="p_me"))
    mine["owner_id"] = "p_me"
    roster = [foreign, mine]

    absent = beast_config.find_owned_beast(roster, "no-such-beast", "p_me")
    stolen = beast_config.find_owned_beast(roster, foreign["beast_id"], "p_me")
    assert absent is None
    assert stolen is None
    assert absent is stolen


def test_an_owner_can_still_reach_their_own_beast():
    """The guard above is only acceptable if it refuses the thief and not the
    owner. Without this arm the refusal proves nothing."""
    mine = _beast(_player(id="p_me"))
    mine["owner_id"] = "p_me"
    roster = [mine]
    assert beast_config.find_owned_beast(roster, mine["beast_id"], "p_me") is mine
    assert beast_config.find_owned_beast([mine], "no-such-beast", "p_me") is None


def test_the_contract_carrying_a_beast_is_never_reachable_through_another_roster():
    """The end-to-end shape of the two tests above: a contract exists on the
    owner's row and no lookup from another player's roster can produce it."""
    owner = _player(id="p_owner")
    owned = _beast(owner)
    _contracted(owned, bond=90)
    owned["owner_id"] = "p_owner"
    stranger = _player(id="p_stranger")
    assert beast_config.find_owned_beast([owned], owned["beast_id"], "p_stranger") is None
    # ...and the stranger's own roster, which has no beasts, is empty rather than
    # an error: the refusal is the same shape as an empty account.
    assert sb.roster_view(stranger)["companions"] == []


# ==========================================================================
# ONE meaning for a corrupt bond
#
# The DECISION this file pins: a corrupt beast bond means 0. That is what an
# absent bond already means, and 0 sits below REFUSING_BOND, so a corrupt row
# reads as a beast that declines to fight and says why.
#
# It is pinned in one reader (``sb._bond_of``) and every read AND write path
# routes through it. Before that, the read paths answered 0 and the two write
# paths answered BOND_ON_TAME, so one corrupt field read as "will not fight"
# on the panel and was then silently rewritten to a healthy tame bond by the
# next fight -- a free loyalty refill for a broken row.
#
# A social route published the same field through a bare ``int()`` that raised
# on a hand-edited row and took the whole visit screen down, so the arm below
# compares the two modules directly rather than trusting either alone.
# ==========================================================================

#: Every way a stored bond can be unreadable or out of the range the server
#: itself writes. 0..BOND_MAX is the only range a writer produces.
CORRUPT_BONDS = (
    None,                       # absent
    "corrupt",                  # a string
    {"nested": 1},              # a dict
    float("nan"),
    float("inf"),
    float("-inf"),
    1e400,                      # finite, still too large for int()
    -50,                        # below the floor: no writer can produce it
    sb.BOND_MAX + 1,            # above the ceiling: ditto
)


def test_a_corrupt_bond_reads_as_zero_in_every_module_that_publishes_it():
    """One meaning, measured across the modules rather than asserted twice."""
    import social_routes

    for junk in CORRUPT_BONDS:
        beast = _beast()
        _contracted(beast, bond=junk)
        published = sb.companion_view(beast, _player())["contract"]["bond"]
        on_a_visit = social_routes.SocialStore._bond_of(beast)
        assert published == 0, f"{junk!r} published as {published}"
        assert on_a_visit == 0, f"{junk!r} published as {on_a_visit} on a visit"


@pytest.mark.parametrize("junk", CORRUPT_BONDS)
def test_a_corrupt_bond_never_buys_its_way_over_the_refusing_floor(junk):
    """The write paths are where the old bug actually paid out.

    A won fight used to read a corrupt bond as BOND_ON_TAME, so the row came
    back as a fighting beast and the corrupt field silently became a real
    loyalty. From 0 the same fight cannot clear the floor: the bond has to be
    earned back one win at a time.
    """
    beast = _beast()
    _contracted(beast, bond=junk)
    after = sb.apply_battle_bond(beast, True, 1.0)
    assert after < sb.REFUSING_BOND, f"{junk!r} won a fight it had not earned: {after}"
    assert sb.refusal_reason(beast) == "bond_too_low"


def test_a_corrupt_bond_charges_no_neglect_against_an_invented_starting_point():
    """Neglect is the other write path, and it also used to start at
    BOND_ON_TAME -- so a corrupt row was reported as having lost marks it
    never had."""
    beast = _beast()
    # "dominion", not the default "bargain": a bargain has bond_decay 0.0,
    # so neglect returns early and never reaches the bond at all.
    contract = _contracted(beast, bond=float("inf"), kind="dominion")
    assert sb.CONTRACT_KINDS["dominion"]["bond_decay"] > 0
    contract["bond_updated_at"] = time.time() - (sb.BOND_NEGLECT_GRACE_DAYS + 5) * 86400
    lost = sb.apply_neglect(beast)
    assert lost == 0, f"a corrupt bond was charged {lost} marks"
    assert contract["bond"] == 0


@pytest.mark.parametrize("bond", [0, 19, sb.REFUSING_BOND, sb.BOND_ON_TAME, sb.BOND_MAX])
def test_an_intact_bond_is_never_touched_by_the_guard(bond):
    """A guard that swallowed good data would be as broken as one that let the
    poison through: every value the server itself writes must pass through
    unchanged."""
    assert sb._bond_of({"bond": bond}) == bond
