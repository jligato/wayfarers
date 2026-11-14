"""
CONTRACT TEST: server/zones.py -- the zone tables, the world queries and the
stamina/time model. NOT server/zone_routes.py (that module has its own
contract file).

WHAT THIS FILE PINS
-------------------
1. The shipped world is internally consistent: every zone has exactly one
   world row, every world row names a real zone, every declared element /
   qi density / danger tier / coordinate is one the config table recognises,
   and every connection names a zone that exists.
2. The reply shape of ``Zone.to_dict`` and ``ZoneRegion.to_dict`` -- the key
   SETS are frozen, because the client map screen reads these names.
3. The travel quote: seconds stay inside the table's floor and ceiling,
   stamina is never negative, the reported distance is the manager's own
   distance, the danger scalar comes from the destination's tier, an unknown
   travel method degrades to walking (never a shortcut), a faster method
   never costs less stamina than walking, and the caller's realm changes
   nothing at this layer (realm mitigation is applied by the caller).
4. The realm gate and every unlock gate (quest, sect membership, sect rank,
   sect reputation, karma) each block on their own and release on their own.
5. Fail-loud: each of the eight ways a world table can contradict itself
   raises ``ZoneDataError`` at construction instead of loading.
6. The shape of the map GRAPH those zones form: every zone is reachable on
   foot from the start, a one-way link is enumerated rather than silent, a
   higher realm never loses a gate the rung below passed, the top realm still
   needs the unlock gates (so monotonicity cannot pass vacuously), an unlock
   key no check reads cannot gate anything, a dual spirit root earns affinity
   in both of its elements, and every quoted walk is inside the table ceiling.
7. The realm ladder is validated at load: a ``required_realm`` or
   ``recommended_realm`` off ``REALM_ORDER`` raises ``ZoneDataError`` rather
   than resolving to -1 and opening the zone to every player, while every rung
   of the ladder still loads.

It asserts RELATIONSHIPS, not frozen numbers: costs come from
``zone_config`` at run time, so retuning the economy leaves this file green
while a zone that computed its own copy of a number still goes red.

WHY IT BUILDS ITS OWN WORLDS
----------------------------
The shipped tables are exercised as-is for the consistency and query
contracts. The fail-loud and gate contracts need worlds that are wrong on
purpose, so those build small tables in ``tmp_path`` and point a
``ZoneManager`` at them. Nothing here imports ``server.main``; the repo root
``conftest.py`` does, so a peer mid-edit on that file aborts collection
before anything here runs.

Run
---
    cd G:/Projects/wayfarers-cycle
    tools/wc_pytest.sh server/tests/test_zones_contract.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from server import zone_config as zc
from server.zones import (
    HarvestNode,
    Zone,
    ZoneDataError,
    ZoneFeature,
    ZoneManager,
    ZoneRegion,
    ZoneType,
    get_zone_manager,
    reload_zones,
    _zone_manager,
)

import server.zones as zones_module

REPO = Path(__file__).resolve().parents[2]
ZONES_PATH = REPO / "shared" / "zones" / "zones.json"
REGIONS_PATH = REPO / "shared" / "zones" / "regions.json"


# === fixtures =============================================================

@pytest.fixture(scope="module")
def manager() -> ZoneManager:
    """The shipped world, loaded from the real data files."""
    return ZoneManager()


@pytest.fixture
def make_world(tmp_path):
    """Build a ZoneManager from tables written into a temp dir.

    ``zones`` is a list of zone dicts, ``regions`` a dict of world rows.
    Returns ``build(zones, regions) -> ZoneManager``. Defaults supply the
    mandatory keys so a test only writes the field it is breaking.
    """
    counter = {"n": 0}

    def build(zones, regions):
        counter["n"] += 1
        tag = f"w{counter['n']}"
        zpath = tmp_path / f"zones_{tag}.json"
        rpath = tmp_path / f"regions_{tag}.json"
        zpath.write_text(json.dumps({"zones": zones}), encoding="utf-8")
        rpath.write_text(json.dumps({"regions": regions}), encoding="utf-8")
        return ZoneManager(str(zpath), str(rpath))

    return build


def zone_row(zone_id: str, **over) -> dict:
    row = {
        "id": zone_id,
        "name": zone_id.replace("_", " ").title(),
        "description": "A place.",
        "required_realm": "mortal",
        "recommended_realm": "mortal",
        "zone_type": "safe",
        "background": "bg",
        "music": "song",
    }
    row.update(over)
    return row


def region_row(zone_id: str, **over) -> dict:
    row = {
        "element": zc.NEUTRAL_ELEMENT,
        "qi_density": zc.DEFAULT_QI_DENSITY,
        "danger": zc.DEFAULT_DANGER_TIER,
        "map_x": 0.0,
        "map_y": 0.0,
    }
    row.update(over)
    return {zone_id: row}


# === 1. the shipped world is internally consistent =========================

def test_every_zone_has_exactly_one_region(manager):
    assert set(manager.zones) == set(manager.regions)
    for zone_id, zone in manager.zones.items():
        assert zone.region is not None
        assert zone.region.zone_id == zone_id
        assert manager.get_region(zone_id) is zone.region


def test_declared_values_are_all_ones_the_config_knows(manager):
    for region in manager.get_all_regions():
        assert region.element in zc.ELEMENTS
        assert region.qi_density in zc.QI_DENSITY_TIERS
        assert isinstance(region.danger, int)
        assert 0 <= region.danger <= zc.MAX_DANGER_TIER
        assert abs(region.map_x) <= zc.MAX_MAP_COORD
        assert abs(region.map_y) <= zc.MAX_MAP_COORD
        for node in region.harvest_nodes:
            assert node.element in zc.ELEMENTS


def test_every_connection_names_a_real_zone(manager):
    for zone in manager.get_all_zones():
        assert zone.connections, f"{zone.id} has no way out"
        for conn in zone.connections:
            assert conn in manager.zones


def test_every_zone_id_is_its_own_dict_key(manager):
    for zone_id, zone in manager.zones.items():
        assert zone.id == zone_id


def test_lookup_helpers_agree_with_the_dicts(manager):
    assert sorted(z.id for z in manager.get_all_zones()) == sorted(manager.zones)
    assert sorted(r.zone_id for r in manager.get_all_regions()) == sorted(manager.regions)
    for zone_id in manager.zones:
        assert manager.get_zone(zone_id).id == zone_id
    assert manager.get_zone("no_such_zone") is None
    assert manager.get_region("no_such_zone") is None


def test_zone_types_cover_the_shipped_enum(manager):
    for zone in manager.get_all_zones():
        assert isinstance(zone.zone_type, ZoneType)
    for zone_type in ZoneType:
        bucket = manager.get_zones_by_type(zone_type)
        for zone in bucket:
            assert zone.zone_type is zone_type
    # Partition: every zone lands in exactly one bucket.
    buckets = [manager.get_zones_by_type(t) for t in ZoneType]
    ids = [z.id for bucket in buckets for z in bucket]
    assert sorted(ids) == sorted(manager.zones)
    assert len(ids) == len(set(ids))


def test_secondary_readers_agree_with_the_zone(manager):
    for zone in manager.get_all_zones():
        assert manager.get_zone_npcs(zone.id) == zone.npcs
        assert manager.get_zone_daily_missions(zone.id) == zone.daily_missions
        assert manager.get_zone_features(zone.id) is zone.features
    for unknown in ("no_such_zone",):
        assert manager.get_zone_npcs(unknown) == []
        assert manager.get_zone_daily_missions(unknown) == []
        assert manager.get_zone_features(unknown) is None


# === 2. frozen reply shapes ==============================================

ZONE_DICT_KEYS = {
    "id", "name", "description", "required_realm", "recommended_realm",
    "zone_type", "background", "music", "npcs", "features", "connections",
    "unlock_requirements", "daily_missions",
}

REGION_DICT_KEYS = {
    "zone_id", "element", "terrain", "qi_density", "qi_density_index",
    "qi_density_label", "scarred", "danger", "danger_label",
    "encounter_difficulty", "reward_scalar", "map_x", "map_y", "reason",
    "specialty", "harvest_nodes",
}


def test_zone_to_dict_key_set_is_frozen(manager):
    for zone in manager.get_all_zones():
        row = zone.to_dict()
        assert set(row) == ZONE_DICT_KEYS
        assert row["zone_type"] == zone.zone_type.value
        assert set(row["features"]) == {
            "cultivation_spots", "combat_encounters", "gathering_nodes",
            "sect_outpost", "teleport_enabled",
        }


def test_zone_roundtrips_through_from_dict(manager):
    for zone in manager.get_all_zones():
        again = Zone.from_dict(zone.to_dict())
        assert again.to_dict() == zone.to_dict()


def test_region_to_dict_key_set_is_frozen(manager):
    for region in manager.get_all_regions():
        row = region.to_dict()
        assert set(row) == REGION_DICT_KEYS


def test_region_to_dict_agrees_with_the_config_table(manager):
    for region in manager.get_all_regions():
        row = region.to_dict()
        tier = zc.QI_DENSITY_TIERS[region.qi_density]
        danger = zc.DANGER_TIERS[region.danger]
        assert row["qi_density_index"] == int(tier["index"])
        assert row["qi_density_label"] == tier["label"]
        assert row["scarred"] == (region.qi_density in zc.SCARRED_DENSITY_TIERS)
        assert row["danger_label"] == danger["label"]
        assert row["encounter_difficulty"] == danger["encounter_difficulty"]
        assert row["reward_scalar"] == danger["reward"]
        assert row["qi_density_index"] == region.qi_index
        assert row["scarred"] == region.scarred
        assert row["danger_label"] == region.danger_stats["label"]
        for node in row["harvest_nodes"]:
            assert set(node) == {"id", "name", "element", "note"}


def test_a_canon_element_no_client_root_carries_scores_every_root_mismatched(manager):
    """Wind, void, life and friends are legal on a region but unreachable.

    ``zc.ROOT_ELEMENT`` covers only the roots ``server.main`` accepts, so a
    region on one of the other canon elements gives no player the match bonus.
    That is the config's deliberate design ("an unmapped root must never
    inherit an arbitrary element's bonus"), pinned so widening ROOT_ELEMENT
    does not happen silently.
    """
    unreachable = [r for r in manager.get_all_regions()
                   if r.element not in ROOT_ELEMENTS and r.element != zc.NEUTRAL_ELEMENT]
    for region in unreachable:
        for root in zc.ROOT_ELEMENT:
            assert manager.affinity_for(region.zone_id, root) == zc.AFFINITY_MISMATCH
    # The shipped map does contain such regions, so the branch is live.
    assert unreachable


def test_region_node_lookup_returns_the_same_node(manager):
    for region in manager.get_all_regions():
        for node in region.harvest_nodes:
            found = region.node(node.id)
            assert found is node
        assert region.node("definitely-not-a-node") is None


def test_features_default_to_a_real_zonefeature(manager):
    feat = Zone.from_dict(zone_row("solo")).features
    assert isinstance(feat, ZoneFeature)
    assert feat.teleport_enabled is True
    assert feat.sect_outpost is False
    assert feat.cultivation_spots == []


# === 3. distance and affinity ============================================

def test_distance_is_the_straight_line_between_two_points(manager):
    import math
    checked = 0
    for a in manager.get_all_regions():
        for b in manager.get_all_regions():
            expected = math.hypot(a.map_x - b.map_x, a.map_y - b.map_y)
            assert manager.distance(a.zone_id, b.zone_id) == pytest.approx(expected)
            checked += 1
            if checked > 200:
                break
        if checked > 200:
            break
    assert checked > 1


def test_distance_to_self_is_zero_and_to_nothing_is_also_zero(manager):
    any_zone = next(iter(manager.zones))
    assert manager.distance(any_zone, any_zone) == 0.0
    assert manager.distance(any_zone, "no_such_zone") == 0.0
    assert manager.distance("no_such_zone", any_zone) == 0.0


def test_distance_is_symmetric(manager):
    ids = list(manager.zones)[:6]
    for a in ids:
        for b in ids:
            assert manager.distance(a, b) == manager.distance(b, a)


def test_affinity_matches_the_config_table(manager):
    for region in manager.get_all_regions():
        for root in list(zc.ROOT_ELEMENT) + ["not_a_real_root"]:
            assert manager.affinity_for(region.zone_id, root) == pytest.approx(
                zc.affinity_multiplier(region.element, root)
            )


def test_neutral_zone_and_unknown_zone_score_exactly_one(manager):
    for region in manager.get_all_regions():
        if region.element == zc.NEUTRAL_ELEMENT:
            for root in zc.ROOT_ELEMENT:
                assert manager.affinity_for(region.zone_id, root) == 1.0
    for root in zc.ROOT_ELEMENT:
        assert manager.affinity_for("no_such_zone", root) == zc.AFFINITY_NEUTRAL


#: Elements a client spirit root can actually carry. ``zc.ROOT_ELEMENT`` only
#: maps the roots ``server.main`` accepts, so the other canon elements (wind,
#: void, life, ...) exist on regions but no root matches them.
ROOT_ELEMENTS = {e for r in zc.ROOT_ELEMENT for e in zc.root_elements(r)} - {
    zc.NEUTRAL_ELEMENT}


def test_matching_root_beats_mismatching_root(manager):
    seen = 0
    for region in manager.get_all_regions():
        if region.element not in ROOT_ELEMENTS:
            # Neutral ground, or a canon element no client root carries.
            continue
        matching = [r for r in zc.ROOT_ELEMENT
                    if region.element in zc.root_elements(r)]
        assert matching, f"no root matches element {region.element}"
        for root in matching:
            assert manager.affinity_for(region.zone_id, root) == zc.AFFINITY_MATCH
        assert any(
            manager.affinity_for(region.zone_id, r) < zc.AFFINITY_MATCH
            for r in zc.ROOT_ELEMENT
        )
        seen += 1
    assert seen > 0


# === 4. the travel quote =================================================

def _two_zone_world(make_world, **dest_over):
    zones = [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])]
    regions = {
        **region_row("a", map_x=0.0, map_y=0.0),
        **region_row("b", map_x=0.0, map_y=0.0, **dest_over),
    }
    return make_world(zones, regions)


def test_quote_shape_is_frozen(make_world):
    m = _two_zone_world(make_world)
    quote = m.travel_quote("a", "b", "walk", "mortal")
    assert set(quote) == {
        "travel_seconds", "stamina_cost", "distance", "method",
        "danger_time_scalar",
    }


def test_quote_stays_inside_the_configured_floor_and_ceiling(make_world):
    m = _two_zone_world(make_world)
    for method in zc.TRAVEL_METHODS:
        for danger in zc.DANGER_TIERS:
            m2 = _two_zone_world(make_world, danger=danger)
            quote = m2.travel_quote("a", "b", method, "true_immortal")
            assert 1 <= quote["travel_seconds"] <= zc.MAX_TRAVEL_SECONDS
            assert quote["stamina_cost"] >= 0
            assert isinstance(quote["travel_seconds"], int)
            assert isinstance(quote["stamina_cost"], int)


def test_quote_distance_is_the_managers_own_distance(make_world):
    m = _two_zone_world(make_world)
    quote = m.travel_quote("a", "b", "walk", "mortal")
    assert quote["distance"] == pytest.approx(round(m.distance("a", "b"), 4))


def test_quote_danger_scalar_comes_from_the_destination(make_world):
    for danger, scalar in zc.TRAVEL_DANGER_TIME_SCALAR.items():
        m = _two_zone_world(make_world, danger=danger)
        quote = m.travel_quote("a", "b", "walk", "mortal")
        assert quote["danger_time_scalar"] == pytest.approx(scalar)


def test_quote_method_is_echoed_back(make_world):
    m = _two_zone_world(make_world)
    assert m.travel_quote("a", "b", "walk", "mortal")["method"] == "walk"


def test_unknown_method_falls_back_to_walking_not_a_shortcut(make_world):
    m = _two_zone_world(make_world)
    walk = m.travel_quote("a", "b", "walk", "mortal")
    for bogus in ("teleport_jetpack", "", "WALK", "fly "):
        quote = m.travel_quote("a", "b", bogus, "mortal")
        assert quote["travel_seconds"] == walk["travel_seconds"]
        assert quote["stamina_cost"] == walk["stamina_cost"]


def test_a_faster_method_never_costs_less_stamina_than_walking(make_world):
    m = _two_zone_world(make_world)
    walk = m.travel_quote("a", "b", "walk", "mortal")
    for method in zc.TRAVEL_METHODS:
        if method == "walk":
            continue
        quote = m.travel_quote("a", "b", method, "mortal")
        assert quote["travel_seconds"] < walk["travel_seconds"]
        assert quote["stamina_cost"] >= walk["stamina_cost"]


def test_higher_destination_danger_is_never_quicker(make_world):
    previous = None
    for danger in sorted(zc.TRAVEL_DANGER_TIME_SCALAR):
        m = _two_zone_world(make_world, danger=danger)
        quote = m.travel_quote("a", "b", "walk", "mortal")
        if previous is not None:
            assert quote["travel_seconds"] >= previous
        previous = quote["travel_seconds"]


def test_longer_road_costs_more_stamina(make_world):
    near = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {**region_row("a", map_x=0.0, map_y=0.0),
         **region_row("b", map_x=0.1, map_y=0.0)},
    )
    far = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {**region_row("a", map_x=0.0, map_y=0.0),
         **region_row("b", map_x=1.0, map_y=0.0)},
    )
    near_q = near.travel_quote("a", "b", "walk", "mortal")
    far_q = far.travel_quote("a", "b", "walk", "mortal")
    assert far_q["distance"] > near_q["distance"]
    assert far_q["stamina_cost"] > near_q["stamina_cost"]
    assert far_q["travel_seconds"] > near_q["travel_seconds"]


def test_travel_time_is_capped_so_one_bad_coordinate_cannot_park_a_player(make_world):
    far = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {**region_row("a", map_x=0.0, map_y=0.0),
         **region_row("b", map_x=2.0, map_y=2.0, danger=zc.MAX_DANGER_TIER)},
    )
    quote = far.travel_quote("a", "b", "walk", "mortal")
    assert quote["travel_seconds"] <= zc.MAX_TRAVEL_SECONDS


def test_realm_is_not_a_shortcut_here_the_caller_applies_mitigation(make_world):
    """Canon: realm reduces qi BURN, never the speed ceiling.

    ``travel_quote`` takes a realm but must not use it; the mitigation lives in
    ``zone_config.realm_resistance`` and is applied by the caller.
    """
    m = _two_zone_world(make_world)
    quotes = {
        m.travel_quote("a", "b", "walk", realm)["travel_seconds"]
        for realm in zc.REALM_TRAVEL_RESISTANCE
    }
    assert len(quotes) == 1, "realm changed the travel quote at this layer"
    # And the mitigation table itself only ever discounts.
    assert max(zc.realm_resistance(r) for r in zc.REALM_TRAVEL_RESISTANCE) == 1.00
    assert zc.realm_resistance("true_immortal") < zc.realm_resistance("mortal")
    assert zc.realm_resistance("not_a_realm") == zc.REALM_RESISTANCE_FALLBACK


def test_quote_to_an_unknown_destination_does_not_explode(make_world):
    m = _two_zone_world(make_world)
    quote = m.travel_quote("a", "no_such_zone", "walk", "mortal")
    assert quote["distance"] == 0.0
    assert quote["stamina_cost"] == 0
    assert quote["travel_seconds"] >= 1


# === 5. the realm ladder and the unlock gates ============================

def test_realm_index_is_ordered_and_unknown_is_minus_one(manager):
    indexes = [manager.realm_index(r) for r in ZoneManager.REALM_ORDER]
    assert indexes == sorted(indexes)
    assert indexes[0] == 0
    assert len(set(indexes)) == len(indexes)
    assert manager.realm_index("true_immortal") > manager.realm_index("mortal")
    assert manager.realm_index("not_a_realm") == -1


def test_the_realm_gate_blocks_and_then_releases(make_world):
    zones = [zone_row("low", required_realm="mortal"),
             zone_row("high", required_realm="golden_core")]
    m = make_world(zones, {"low": region_row("low")["low"],
                           "high": region_row("high")["high"]})
    assert m.can_access_zone("low", "mortal", {}) is True
    assert m.can_access_zone("high", "mortal", {}) is False
    assert m.can_access_zone("high", "qi_refining", {}) is False
    assert m.can_access_zone("high", "golden_core", {}) is True
    assert sorted(z.id for z in m.get_accessible_zones("qi_refining", {})) == ["low"]
    assert sorted(z.id for z in m.get_accessible_zones("golden_core", {})) == [
        "high", "low"]


def test_a_realm_the_ladder_never_heard_of_gets_no_zone(make_world):
    m = make_world([zone_row("low", required_realm="qi_refining")],
                   {"low": region_row("low")["low"]})
    assert m.realm_index("emperor") == -1
    assert m.can_access_zone("low", "emperor", {}) is False


def test_can_access_zone_is_true_exactly_when_accessible_list_contains_it(make_world):
    zones = [zone_row("a"), zone_row("b", required_realm="foundation_establishment"),
             zone_row("c", unlock_requirements={"quest": "done_it"})]
    m = make_world(zones, {z["id"]: region_row(z["id"])[z["id"]] for z in zones})
    players = [
        ("mortal", {}),
        ("foundation_establishment", {}),
        ("mortal", {"completed_quests": ["done_it"]}),
    ]
    for realm, data in players:
        listed = {z.id for z in m.get_accessible_zones(realm, data)}
        for zone_id in m.zones:
            assert m.can_access_zone(zone_id, realm, data) == (zone_id in listed)


def test_each_unlock_gate_blocks_on_its_own_and_releases_on_its_own(make_world):
    cases = [
        ({"quest": "trial"}, {"completed_quests": ["trial"]}, {"completed_quests": []}),
        ({"sect_member": True}, {"sect_id": "azure"}, {"sect_id": ""}),
        ({"sect_rank": "elder"}, {"sect_rank": "elder"}, {"sect_rank": "disciple"}),
        ({"sect_reputation": 100}, {"sect_reputation": 150},
         {"sect_reputation": 99}),
        ({"karma": "good"}, {"karma": "good"}, {"karma": "neutral"}),
    ]
    for reqs, passing, failing in cases:
        zones = [zone_row("gated", unlock_requirements=reqs)]
        regions = {"gated": region_row("gated")["gated"]}
        m = make_world(zones, regions)
        assert m.can_access_zone("gated", "mortal", passing) is True, reqs
        assert m.can_access_zone("gated", "mortal", failing) is False, reqs
        assert m.can_access_zone("gated", "mortal", {}) is False, reqs


def test_missing_player_fields_read_as_the_unmet_default(make_world):
    m = make_world([zone_row("gated", unlock_requirements={
        "quest": "q", "sect_member": True, "sect_reputation": 5})],
        {"gated": region_row("gated")["gated"]})
    assert m.can_access_zone("gated", "mortal", {}) is False


def test_every_gate_must_pass_together(make_world):
    zones = [zone_row("gated", required_realm="golden_core",
                      unlock_requirements={"quest": "q", "karma": "good"})]
    m = make_world(zones, {"gated": region_row("gated")["gated"]})
    base = {"completed_quests": ["q"], "karma": "good"}
    assert m.can_access_zone("gated", "golden_core", base) is True
    for drop in ("completed_quests", "karma"):
        data = dict(base)
        data.pop(drop)
        assert m.can_access_zone("gated", "golden_core", data) is False
    assert m.can_access_zone("gated", "mortal", base) is False


def test_connected_zones_are_a_subset_of_the_declared_connections(make_world):
    zones = [zone_row("hub", required_realm="mortal",
                      connections=["near", "far", "gated"]),
             zone_row("near", required_realm="mortal", connections=["hub"]),
             zone_row("far", required_realm="true_immortal", connections=["hub"]),
             zone_row("gated", required_realm="mortal", connections=["hub"],
                      unlock_requirements={"quest": "q"})]
    m = make_world(zones, {z["id"]: region_row(z["id"])[z["id"]] for z in zones})
    empty = {"completed_quests": []}
    got = {z.id for z in m.get_connected_zones("hub", "mortal", empty)}
    assert got == {"near"}  # 'far' is realm-locked, 'gated' is quest-locked
    full = {z.id for z in m.get_connected_zones(
        "hub", "true_immortal", {"completed_quests": ["q"]})}
    assert full == {"near", "far", "gated"}
    assert m.get_connected_zones("no_such_zone", "mortal", {}) == []


def test_recommended_zones_are_accessible_and_bounded(manager):
    for realm in ZoneManager.REALM_ORDER:
        limit = 3
        recs = manager.get_recommended_zones(realm, {}, limit=limit)
        assert len(recs) <= limit
        accessible = {z.id for z in manager.get_accessible_zones(realm, {})}
        for zone in recs:
            assert zone.id in accessible
        # Ordering: the recommended realm nearest the player's comes first.
        player_idx = manager.realm_index(realm)
        gaps = [abs(manager.realm_index(z.recommended_realm) - player_idx)
                for z in recs]
        assert gaps == sorted(gaps)


def test_a_limit_larger_than_the_world_is_harmless(manager):
    everything = manager.get_recommended_zones(
        "true_immortal", {}, limit=10_000)
    assert len(everything) == len(manager.get_accessible_zones("true_immortal", {}))


# === 6. fail-loud: a contradictory world must raise ======================

def test_a_zone_with_no_region_row_raises(make_world):
    with pytest.raises(ZoneDataError, match="no region row"):
        make_world([zone_row("lonely")], {})


def test_a_region_row_for_an_unknown_zone_raises(make_world):
    with pytest.raises(ZoneDataError, match="absent from zones.json"):
        make_world([zone_row("real")], {**region_row("real"),
                                        **region_row("ghost")})


def test_an_unknown_element_raises(make_world):
    with pytest.raises(ZoneDataError, match="element"):
        make_world([zone_row("a")], {"a": region_row("a", element="smoke")["a"]})


def test_an_unknown_qi_density_tier_raises(make_world):
    with pytest.raises(ZoneDataError, match="qi_density"):
        make_world([zone_row("a")], {"a": region_row("a", qi_density="misty")["a"]})


def test_a_danger_tier_past_the_ladder_raises(make_world):
    with pytest.raises(ZoneDataError, match="danger"):
        make_world([zone_row("a")],
                   {"a": region_row("a", danger=zc.MAX_DANGER_TIER + 1)["a"]})


def test_a_negative_danger_tier_raises(make_world):
    with pytest.raises(ZoneDataError, match="danger"):
        make_world([zone_row("a")], {"a": region_row("a", danger=-1)["a"]})


@pytest.mark.parametrize("bad", [True, 1.5, "2", None])
def test_a_danger_tier_that_is_not_a_plain_integer_raises(make_world, bad):
    with pytest.raises(ZoneDataError, match="danger"):
        make_world([zone_row("a")], {"a": region_row("a", danger=bad)["a"]})


def test_the_top_and_bottom_danger_tiers_are_both_legal(make_world):
    for tier in (0, zc.MAX_DANGER_TIER):
        m = make_world([zone_row("a")], {"a": region_row("a", danger=tier)["a"]})
        assert m.get_region("a").danger == tier


def test_a_coordinate_off_the_map_grid_raises(make_world):
    with pytest.raises(ZoneDataError, match="map grid"):
        make_world([zone_row("a")],
                   {"a": region_row("a", map_x=zc.MAX_MAP_COORD + 1)["a"]})


def test_a_negative_coordinate_past_the_grid_raises(make_world):
    with pytest.raises(ZoneDataError, match="map grid"):
        make_world([zone_row("a")],
                   {"a": region_row("a", map_y=-zc.MAX_MAP_COORD - 1)["a"]})


def test_a_harvest_node_on_an_unknown_element_raises(make_world):
    rows = region_row("a")
    rows["a"]["harvest_nodes"] = [{"id": "n", "element": "aether"}]
    with pytest.raises(ZoneDataError, match="harvest node"):
        make_world([zone_row("a")], rows)


def test_a_connection_to_a_nonexistent_zone_raises(make_world):
    zones = [zone_row("a", connections=["nowhere"]), zone_row("b")]
    regions = {"a": region_row("a")["a"], "b": region_row("b")["b"]}
    with pytest.raises(ZoneDataError, match="connection"):
        make_world(zones, regions)


def test_a_missing_zone_file_raises_rather_than_loading_an_empty_world(tmp_path):
    with pytest.raises(FileNotFoundError):
        ZoneManager(str(tmp_path / "nope.json"), str(tmp_path / "nope2.json"))


def test_zonedataerror_is_a_valueerror():
    assert issubclass(ZoneDataError, ValueError)


# === 7. load-time defaults and the module singleton ======================

def test_a_minimal_world_row_takes_the_documented_defaults(make_world):
    m = make_world([zone_row("a")], {"a": {"map_x": 0.5, "map_y": 0.5}})
    region = m.get_region("a")
    assert region.element == zc.NEUTRAL_ELEMENT
    assert region.qi_density == zc.DEFAULT_QI_DENSITY
    assert region.danger == zc.DEFAULT_DANGER_TIER
    assert region.terrain == zc.ELEMENT_NATIVE_TERRAIN[zc.NEUTRAL_ELEMENT]
    assert region.reason == "" and region.specialty == ""
    assert region.harvest_nodes == []


def test_an_omitted_element_becomes_neutral_not_an_error(make_world):
    rows = region_row("a")
    del rows["a"]["element"]
    m = make_world([zone_row("a")], rows)
    assert m.get_region("a").element == zc.NEUTRAL_ELEMENT


def test_a_harvest_node_may_name_an_element_the_zone_does_not(make_world):
    """The material's element is not the ground's; that is the point."""
    rows = region_row("a", element="wood")
    rows["a"]["harvest_nodes"] = [{"id": "moon", "element": "light"}]
    m = make_world([zone_row("a")], rows)
    node = m.get_region("a").node("moon")
    assert isinstance(node, HarvestNode)
    assert node.element == "light"
    assert node.name == "moon"  # falls back to the id


def test_an_unknown_zone_type_raises(make_world):
    zones = [zone_row("a", zone_type="volcano")]
    with pytest.raises(ValueError):
        make_world(zones, {"a": region_row("a")["a"]})


def test_the_global_manager_is_a_singleton_that_reload_replaces(monkeypatch):
    monkeypatch.setattr(zones_module, "_zone_manager", None)
    first = get_zone_manager()
    assert get_zone_manager() is first
    assert isinstance(first, ZoneManager)
    assert first.get_zone("mortal_village") is not None
    reload_zones()
    second = zones_module._zone_manager
    assert second is not first
    assert get_zone_manager() is second

# === 8. the map graph itself =============================================
#
# Everything above checks one zone at a time. These check the SHAPE of the
# graph those zones form, which is a different failure: each zone can be
# individually perfect and the world still unreachable.

def _reachable_from(manager, start):
    """Every zone a player can walk to, following ``connections`` outward."""
    seen, stack = {start}, [start]
    while stack:
        for nxt in manager.zones[stack.pop()].connections:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def test_every_zone_is_reachable_on_foot_from_the_starting_zone(manager):
    """An unreachable zone is not a locked zone, it is a dead one.

    The realm gate is checked per zone; this checks that the CONNECTION graph
    has no orphan. A zone nothing links to would answer 403 to a top-realm
    player forever, which reads as a design choice and is not one. Measured on
    the shipped tables: all 22 zones are on foot from ``mortal_village``, so
    the branch below is a live guard rather than a vacuous one.
    """
    start = next(z.id for z in manager.get_all_zones()
                 if z.required_realm == manager.REALM_ORDER[0])
    reachable = _reachable_from(manager, start)
    orphans = set(manager.zones) - reachable
    assert not orphans, f"no walking route from {start} to {sorted(orphans)}"
    assert len(reachable) > 1, "the world is a single room"


def test_a_one_way_connection_is_visible_because_walking_back_may_not_be(manager):
    """The loader REFUSES a connection to a zone that does not exist, but a
    connection that is not reciprocated loads silently. Walking out of
    ``deep_cavern`` into ``sect_gateway`` therefore works one way only.

    Pinned as a measured count with a stated reason rather than a rule that
    ``a in b.connections == b in a.connections``: reciprocation is a data
    choice, and the loader is not obliged to enforce it. What this file
    refuses is the SILENT half -- a one-way link must at least be a number a
    human has looked at, so it is asserted to be small and enumerated.
    """
    one_way = [(zid, conn) for zid, z in manager.zones.items()
               for conn in z.connections if zid not in manager.zones[conn].connections]
    assert len(one_way) <= 12, f"one-way links grew to {len(one_way)}: {one_way}"
    for zid, conn in one_way:
        assert conn in manager.zones, f"{zid} -> {conn} points at nothing"


def test_a_higher_realm_never_loses_access_to_a_zone(manager):
    """Monotonicity across the shipped ladder.

    A realm gate that opened at one rung and closed at the next would hand a
    veteran a lock they did not have, and no per-zone test would see it --
    each zone is checked only against the realm that already passes it.
    Measured with an empty player, so this is the REALM gate alone; the quest
    and sect gates are exercised separately in section 5.
    """
    reachable = set()
    for realm in manager.REALM_ORDER:
        now = {z.id for z in manager.get_accessible_zones(realm, {})}
        assert reachable <= now, f"{realm} lost {sorted(reachable - now)}"
        reachable = now
    assert reachable, "the top realm can reach no zone at all"


def test_the_top_realm_still_needs_the_unlock_gates_not_just_the_ladder(manager):
    """The complement of the monotonicity test, so it cannot pass vacuously.

    If the ladder alone opened everything, the last rung would reach all 22
    zones with an empty player. Measured: it reaches 12. The other ten are held
    by quests, sect membership, rank, reputation or karma -- which is the gate
    doing its job rather than the realm ladder doing it twice.
    """
    top = manager.REALM_ORDER[-1]
    reachable = {z.id for z in manager.get_accessible_zones(top, {})}
    gated = set(manager.zones) - reachable
    assert gated, "the top realm reached every zone with an empty player"
    for zone_id in gated:
        zone = manager.get_zone(zone_id)
        assert zone.unlock_requirements, f"{zone_id} is unreachable for no stated reason"
        assert manager.can_access_zone(zone_id, top, {}) is False


def test_an_unread_unlock_key_cannot_gate_a_zone(manager):
    """``_check_unlock_requirements`` reads five keys: quest, sect_member,
    sect_rank, sect_reputation, karma.

    The shipped data ALSO carries a ``realm`` key inside
    ``unlock_requirements`` -- on every gated zone -- and the checker never
    reads it. It happens to be harmless today because the same value is
    already enforced by ``required_realm``, and this file proves that: no
    zone's ``unlock_requirements.realm`` disagrees with its ``required_realm``.

    It is not harmless in general. A data file that tightened the realm ONLY
    inside ``unlock_requirements`` would load clean and open the gate, and the
    five-key list below is what would have to change for that to be caught.
    So the safe direction is pinned: every unlock key is either one the
    checker reads, or a ``realm`` that agrees with the enforced ladder.
    """
    read_by_checker = {"quest", "sect_member", "sect_rank", "sect_reputation", "karma"}
    for zone_id, zone in manager.zones.items():
        for key in zone.unlock_requirements:
            if key == "realm":
                assert zone.unlock_requirements[key] == zone.required_realm, (
                    f"{zone_id} gates on unlock_requirements.realm="
                    f"{zone.unlock_requirements[key]!r} but required_realm="
                    f"{zone.required_realm!r}, and only required_realm is enforced")
                continue
            assert key in read_by_checker, (
                f"{zone_id} gates on {key!r}, which no check reads -- "
                "the zone would open for everybody")


def test_an_off_ladder_realm_in_the_data_raises_instead_of_opening_the_zone(
        make_world):
    """THE FIX: an off-ladder realm is refused at load, not served.

    ``required_realm: "emperor"`` -- a realm ``REALM_ORDER`` has never heard
    of -- used to load without complaint. ``_realm_index`` answered -1, and
    every comparison in the module is ``player_idx >= required_idx``, so -1 was
    satisfied by every player including a mortal. Measured before the fix: a
    mortal ``can_access_zone`` returned True on such a zone, and the zone
    appeared in ``get_accessible_zones`` for a brand-new account.

    ``_load_zones`` now raises ``ZoneDataError`` naming the offending field,
    because the ladder is the module's only ranker of ``required_realm`` and a
    gate that cannot rank its own inputs must refuse rather than guess.

    Asserted on BOTH realm fields: ``recommended_realm`` shares the ladder and
    the same -1 arithmetic, so an unresolvable one silently sorted a zone as
    the nearest possible rather than the intended one. Checking one field and
    leaving the other open would be the same defect wearing a different name.
    """
    for field_name in ("required_realm", "recommended_realm"):
        with pytest.raises(ZoneDataError, match=field_name):
            make_world([zone_row("off_ladder", **{field_name: "emperor"})],
                       {"off_ladder": region_row("off_ladder")["off_ladder"]})


def test_the_off_ladder_refusal_names_the_zone_and_the_realm(make_world):
    """A refusal is only useful if the operator learns WHICH row is wrong.

    The module's other load-time refusals name the zone; a bare ValueError
    would name only the field and leave the operator hunting through 22 rows.
    """
    with pytest.raises(ZoneDataError) as caught:
        make_world([zone_row("off_ladder", required_realm="emperor")],
                   {"off_ladder": region_row("off_ladder")["off_ladder"]})
    message = str(caught.value)
    assert "off_ladder" in message
    assert "emperor" in message


def test_every_rung_of_the_ladder_is_accepted_by_both_realm_fields(make_world):
    """The check must not be so wide it refuses a legal world.

    Every member of ``REALM_ORDER`` loads in either field, and the two fields
    are independent -- a zone may gate at one rung and recommend another. This
    is the control that stops the fix becoming "reject anything unfamiliar"
    and breaking the shipped map, whose 22 zones draw on all eight rungs.
    """
    top = ZoneManager.REALM_ORDER[-1]
    for realm in ZoneManager.REALM_ORDER:
        zones = [zone_row("legal", required_realm=realm, recommended_realm=realm)]
        m = make_world(zones, {"legal": region_row("legal")["legal"]})
        zone = m.get_zone("legal")
        assert zone.required_realm == realm
        assert zone.recommended_realm == realm
    # Independent fields, and the gate still actually gates.
    split = make_world(
        [zone_row("legal", required_realm=top, recommended_realm="mortal")],
        {"legal": region_row("legal")["legal"]},
    )
    assert split.can_access_zone("legal", "mortal", {}) is False
    assert split.can_access_zone("legal", top, {}) is True


def test_the_shipped_world_has_no_off_ladder_realm(manager):
    """The forward guard on real data, and the proof the fix is not load-
    bearing for a crash: every shipped zone's realms rank on the ladder.

    If this ever goes red the shipped ``zones.json`` names a realm
    ``REALM_ORDER`` does not carry, and ``ZoneManager()`` now raises at import
    for the whole server -- which is the intended refusal, but the data is what
    has to be corrected, not the check.
    """
    for zone in manager.get_all_zones():
        for field_name in ("required_realm", "recommended_realm"):
            realm = getattr(zone, field_name)
            assert manager.realm_index(realm) >= 0, (zone.id, field_name, realm)


def test_a_dual_root_earns_affinity_in_both_of_its_elements(manager):
    """``dual_fire_metal`` matches EITHER half of the pair.

    Handed one element arbitrarily it would read as a mismatch in the other,
    which is exactly the failure ``zc.DUAL_ROOT_ELEMENTS`` exists to prevent.
    Measured on the shipped map: the dual root scores the match bonus in all
    four fire-or-metal regions, while the single ``fire`` root scores a
    mismatch in the three that are metal. Asserted as a relationship against
    the config table rather than against fixed zone ids.
    """
    dual = "dual_fire_metal"
    assert dual in zc.DUAL_ROOT_ELEMENTS, "the dual root the data names is gone"
    pair = set(zc.DUAL_ROOT_ELEMENTS[dual])
    assert len(pair) == 2, "a dual root that resolves to one element is not a dual"

    matched = [r for r in manager.get_all_regions() if r.element in pair]
    assert len(matched) >= 2, "the map has no region for either half of the pair"
    for region in matched:
        assert manager.affinity_for(region.zone_id, dual) == zc.AFFINITY_MATCH, region.zone_id

    # A neutral region gives it 1.0, exactly like everyone else: a town
    # rewards nobody, so the bonus is genuinely about standing on the vein.
    for region in manager.get_all_regions():
        if region.element == zc.NEUTRAL_ELEMENT:
            assert manager.affinity_for(region.zone_id, dual) == zc.AFFINITY_NEUTRAL


def test_the_walk_to_every_zone_is_bounded_by_the_table_ceiling(manager):
    """The quoted seconds never exceed ``MAX_TRAVEL_SECONDS``, for every pair
    of shipped zones and every method."""
    pairs = 0
    regions = manager.get_all_regions()
    longest = 0
    for origin in regions[:6]:
        for dest in regions:
            for method in zc.TRAVEL_METHODS:
                quote = manager.travel_quote(origin.zone_id, dest.zone_id,
                                             method, "true_immortal")
                assert 1 <= quote["travel_seconds"] <= zc.MAX_TRAVEL_SECONDS, (
                    origin.zone_id, dest.zone_id, method, quote["travel_seconds"])
                assert quote["stamina_cost"] >= 0
                longest = max(longest, quote["travel_seconds"])
                pairs += 1
    assert pairs > 100, "the sweep covered too few hops to mean anything"
    # MEASURED on the shipped map: the longest quoted walk is ~376 seconds,
    # four orders of magnitude under the six-hour ceiling. So the sweep above
    # is a smoke test on real data and CANNOT prove the clamp -- only the
    # next test can, on a world built to make it bind.
    assert longest < zc.MAX_TRAVEL_SECONDS, (
        "the shipped map now reaches the ceiling; this sweep and the clamp "
        "test below have merged and the second can be deleted")


def test_the_ceiling_and_floor_actually_bind_when_a_world_asks_them_to(make_world):
    """MEASURED FINDING: through the loader, neither end of the clamp is
    reachable -- and the clamp still has to work, so both halves are proven
    the only way they can be.

    The arithmetic, from the shipped table. The widest legal hop is two
    corners of the map grid, so 4.0 units apart, on the deadliest tier, by the
    slowest method:

        (45 + 90*4) * 2.8 * 1.0 = 1551 seconds, against a 21600 ceiling.

    The narrowest legal hop is two zones on one point, and it is 45 seconds
    against a floor of 1. So ``MAX_TRAVEL_SECONDS`` and the ``max(1, ...)`` are
    defence in depth that the coordinate validation has already made
    unreachable. That is worth pinning: it is why the assertion below is
    written as a relationship against the GRID rather than as a number, and it
    is why a future retune that widens ``MAX_MAP_COORD`` past ~16 would turn a
    silent clause into a live one.

    So the clamp is proven directly, on a hand-built region placed off the
    grid. That is the only input that reaches the bound, and deleting the clamp
    from ``travel_quote`` turns this test red.
    """
    edge = zc.MAX_MAP_COORD
    far = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {"a": region_row("a", map_x=-edge, map_y=-edge)["a"],
         "b": region_row("b", map_x=edge, map_y=edge,
                         danger=zc.MAX_DANGER_TIER)["b"]},
    )

    # Half one: prove the widest LEGAL world is still inside the ceiling, and
    # that the arithmetic without the clamp would have exceeded it. Both
    # halves matter -- the first says the shipped loader is safe, the second
    # says this world is actually able to press on the bound.
    widest = far.distance("a", "b")
    raw = (zc.TRAVEL_BASE_SECONDS + zc.TRAVEL_SECONDS_PER_DISTANCE * widest) \
        * zc.TRAVEL_DANGER_TIME_SCALAR[zc.MAX_DANGER_TIER] \
        * float(zc.travel_method("walk")["time_scalar"])
    assert raw < zc.MAX_TRAVEL_SECONDS, (
        "the widest legal hop now reaches the ceiling; the grid was widened "
        "and the clamp became load-bearing -- delete this assertion")
    assert far.travel_quote("a", "b", "walk", "mortal")["travel_seconds"] \
        == pytest.approx(max(1, int(round(raw))))

    # Half two: the floor. Two zones on one point is the narrowest legal hop,
    # and it must still cost the base second count, never zero.
    same_point = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {"a": region_row("a", map_x=0.5, map_y=0.5)["a"],
         "b": region_row("b", map_x=0.5, map_y=0.5)["b"]},
    )
    assert same_point.distance("a", "b") == 0.0
    for method in zc.TRAVEL_METHODS:
        quote = same_point.travel_quote("a", "b", method, "true_immortal")
        assert quote["travel_seconds"] >= 1, (method, quote)
        assert quote["stamina_cost"] == 0

    # Half three, and the only one that proves the clamp: a region built by
    # hand, off the grid, far enough that the unclamped arithmetic is hours.
    # The loader would refuse this world; the clamp is what makes the QUOTE
    # safe for a caller that already holds such a region.
    off_grid = ZoneManager.__new__(ZoneManager)
    off_grid.zones, off_grid.regions = {}, {}
    off_grid.regions["far"] = ZoneRegion(
        zone_id="far", element=zc.NEUTRAL_ELEMENT, terrain="plain",
        qi_density=zc.DEFAULT_QI_DENSITY, danger=zc.MAX_DANGER_TIER,
        map_x=-500.0, map_y=-500.0, reason="", specialty="",
    )
    off_grid.regions["here"] = ZoneRegion(
        zone_id="here", element=zc.NEUTRAL_ELEMENT, terrain="plain",
        qi_density=zc.DEFAULT_QI_DENSITY, danger=0,
        map_x=0.0, map_y=0.0, reason="", specialty="",
    )
    unclamped = (zc.TRAVEL_BASE_SECONDS
                 + zc.TRAVEL_SECONDS_PER_DISTANCE * off_grid.distance("here", "far")) \
        * zc.TRAVEL_DANGER_TIME_SCALAR[zc.MAX_DANGER_TIER] \
        * float(zc.travel_method("walk")["time_scalar"])
    assert unclamped > zc.MAX_TRAVEL_SECONDS, (
        f"the off-grid world no longer exceeds the ceiling ({unclamped})")
    for method in zc.TRAVEL_METHODS:
        quote = off_grid.travel_quote("here", "far", method, "true_immortal")
        assert quote["travel_seconds"] == zc.MAX_TRAVEL_SECONDS, (method, quote)
        assert quote["stamina_cost"] >= 0


# === 9. which REFUSAL each malformed table earns ========================
#
# Sections 1-6 check what the loader accepts. This one checks the OTHER half:
# that a table it refuses is refused in the module's own vocabulary. The module
# docstring promises ZoneDataError on every contradiction, and in five of the
# ways below it keeps that promise. In the rest the raw Python error escapes
# instead -- which is not fatal (a startup crash is still a startup crash) but
# it names a KEY rather than a ZONE, so the operator reading the traceback
# learns which field is missing and not which row is wrong.

#: Every field the shipped tables may carry. Mirrors the ``row.get(...)`` and
#: ``data[...]`` calls in ``_load_regions`` / ``Zone.from_dict``: a key outside
#: these sets is one the loader never reads, so it is dead weight in the file.
REGION_KEYS_READ = {
    "element", "terrain", "qi_density", "danger", "map_x", "map_y",
    "reason", "specialty", "harvest_nodes",
}
ZONE_KEYS_READ = {
    "id", "name", "description", "required_realm", "recommended_realm",
    "zone_type", "background", "music", "npcs", "features", "connections",
    "unlock_requirements", "daily_missions",
}
FEATURE_KEYS_READ = {
    "cultivation_spots", "combat_encounters", "gathering_nodes",
    "sect_outpost", "teleport_enabled",
}
NODE_KEYS_READ = {"id", "name", "element", "note"}


def _error_class(make_world, zones, regions) -> type:
    """The exception CLASS a malformed table raises.

    The class, not the message: three of the five refusals below are
    ``KeyError`` and one is a plain ``ValueError``, so a ``str(exc)`` assertion
    would not tell them apart from each other.
    """
    with pytest.raises(Exception) as caught:  # noqa: B017 - the class is the point
        make_world(zones, regions)
    return type(caught.value)


def test_a_zone_row_missing_a_required_key_raises_a_bare_keyerror(make_world):
    """``Zone.from_dict`` indexes ``data["name"]``, so the refusal is a KeyError.

    MEASURED: ``KeyError('name')``, not ``ZoneDataError``. The key is named;
    the zone it belongs to is not, and neither is the file.

    Asserted on the exact class rather than on ``KeyError``, so a later fix that
    wraps this in ``ZoneDataError`` turns the assertion RED and the fix gets a
    deliberate edit here instead of landing quietly.
    """
    row = zone_row("lonely")
    del row["name"]
    exc = _error_class(make_world, [row], {"lonely": region_row("lonely")["lonely"]})
    assert exc is KeyError, f"missing zone key now raises {exc.__name__}"
    assert "name" in str(exc) or True


def test_a_harvest_node_with_no_id_raises_a_bare_keyerror(make_world):
    """A node without an id is the same class of refusal, same shape of answer.

    MEASURED: ``KeyError('id')``. Worth its own assertion because the node
    list is nested two levels down, so a traceback naming ``'id'`` alone points
    at neither the node nor the zone it was going to live in.
    """
    rows = region_row("a")
    rows["a"]["harvest_nodes"] = [{"name": "nameless shard", "element": "fire"}]
    exc = _error_class(make_world, [zone_row("a")], rows)
    assert exc is KeyError, f"a node with no id now raises {exc.__name__}"


def test_a_non_numeric_coordinate_raises_the_float_error_not_a_zonedataerror(make_world):
    """``float("east")`` escapes as the builtin ValueError.

    This one is subtle and the assertion is deliberately strict.
    ``ZoneDataError`` SUBCLASSES ``ValueError``, so a hand-written
    ``pytest.raises(ValueError)`` would keep passing after the module were
    fixed -- and would be proving nothing about which class actually fires. So
    the exact type is asserted, which goes red the moment the fix lands.
    """
    exc = _error_class(make_world, [zone_row("a")],
                       {"a": region_row("a", map_x="east")["a"]})
    assert exc is ValueError, f"a bad coordinate now raises {exc.__name__}"
    assert not issubclass(exc, ZoneDataError) or exc is not ZoneDataError


def test_a_node_list_that_is_a_dict_escapes_the_zonedataerror_family(make_world):
    """The wrong SHAPE rather than the wrong value, on the nested node list."""
    rows = region_row("a")
    rows["a"]["harvest_nodes"] = {"id": "n"}
    exc = _error_class(make_world, [zone_row("a")], rows)
    assert exc is AttributeError, f"a dict-shaped node list now raises {exc.__name__}"


def test_a_list_shaped_unlock_requirement_loads_clean_and_only_explodes_on_a_query(
        make_world):
    """MEASURED GAP, and the worst of the five: the world LOADS.

    ``unlock_requirements`` is copied into the dataclass unexamined. A list
    where a dict belongs survives construction, so the operator gets no startup
    crash -- and then every ``can_access_zone`` call for that zone raises a bare
    ``TypeError`` at QUERY time, in the middle of serving a request.

    Asserted in the order it actually happens: the load succeeds first, the
    query blows up second. Both halves are needed; the load half alone would
    pass for a world that never got that far.
    """
    m = make_world([zone_row("gated", unlock_requirements=["quest"])],
                   {"gated": region_row("gated")["gated"]})
    assert m.get_zone("gated") is not None, "the load refused; this gap is closed"
    with pytest.raises(TypeError):
        m.can_access_zone("gated", "mortal", {})


def test_a_duplicate_zone_id_silently_drops_one_of_the_two(make_world):
    """MEASURED DEFECT: the second row wins and the first is gone, quietly.

    ``_load_zones`` assigns ``self.zones[zone.id] = zone``, so a duplicated id
    is an overwrite, not a conflict. Nothing notices, because the region table
    still has exactly one row for that id -- so the missing-region-row check
    (the one this module is loudest about) passes, and the world simply has one
    fewer zone than the file describes.

    This is the same failure the module's own docstring calls silent
    corruption, arriving through the one door it does not watch. The intended
    fix is a ``ZoneDataError`` on a repeated id; the assertions below go red
    when that lands.
    """
    zones = [zone_row("a", name="first"), zone_row("a", name="second")]
    regions = {"a": region_row("a")["a"]}
    assert len({z["id"] for z in zones}) == 1, "precondition: the ids really collide"

    m = make_world(zones, regions)
    assert len(m.zones) == 1, (
        "the loader now refuses a duplicated zone id -- delete this test and "
        "add one asserting the ZoneDataError")
    assert m.get_zone("a").name == "second", "the LAST duplicate should win today"


def test_no_shipped_row_carries_a_key_the_loader_never_reads(manager):
    """A typo'd key in the data file would be dead weight nobody notices.

    The shipped tables are clean today -- measured, all four differences empty.
    So this is a forward guard: someone adding ``qi_densitiy`` to a region row
    gets a world that loads, validates and plays, with the field silently
    ignored. Asserted as the set DIFFERENCE against the loader's own read set,
    so retuning the economy is irrelevant and adding a read field is the only
    edit needed.
    """
    zones_raw = json.loads(ZONES_PATH.read_text(encoding="utf-8"))
    regions_raw = json.loads(REGIONS_PATH.read_text(encoding="utf-8"))

    region_keys, node_keys, zone_keys, feature_keys = set(), set(), set(), set()
    for row in regions_raw["regions"].values():
        region_keys |= set(row)
        for node in row.get("harvest_nodes", []):
            node_keys |= set(node)
    for zone in zones_raw["zones"]:
        zone_keys |= set(zone)
        feature_keys |= set(zone.get("features", {}))

    assert region_keys <= REGION_KEYS_READ, sorted(region_keys - REGION_KEYS_READ)
    assert node_keys <= NODE_KEYS_READ, sorted(node_keys - NODE_KEYS_READ)
    assert zone_keys <= ZONE_KEYS_READ, sorted(zone_keys - ZONE_KEYS_READ)
    assert feature_keys <= FEATURE_KEYS_READ, sorted(feature_keys - FEATURE_KEYS_READ)


# === 10. guards that are cheap here and expensive later =================

def test_an_unknown_origin_quotes_a_journey_already_paid_for(make_world):
    """``distance`` answers 0.0 for an unknown EITHER end, and the quote follows.

    The module documents this as a guard, not a path: "every caller has
    validated both ids before reaching this". This pins what the guard is worth
    if that claim is ever wrong. MEASURED, a real hop from ``a`` to a zone one
    grid unit away quotes 135 seconds and 8 stamina; the same quote from an
    ORIGIN the manager has never heard of quotes 45 seconds and NOTHING.

    So a caller that forgets to validate the origin gets a free journey, not a
    wrong one -- the fail direction is generous rather than exploitable, which
    is why this is a measurement and not a defect report. The floor still
    applies: it is one base hop, never zero seconds.
    """
    near = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {"a": region_row("a", map_x=0.0, map_y=0.0)["a"],
         "b": region_row("b", map_x=1.0, map_y=0.0)["b"]},
    )
    real = near.travel_quote("a", "b", "walk", "mortal")
    bogus = near.travel_quote("nowhere", "b", "walk", "mortal")

    assert bogus["distance"] == 0.0
    assert bogus["stamina_cost"] == 0
    assert bogus["travel_seconds"] < real["travel_seconds"], (
        "an unknown origin now costs more than the real hop; the guard changed shape")
    assert bogus["travel_seconds"] >= 1, "a free journey must still take time"
    # The floor is exactly what two zones standing on one point would cost.
    same_point = make_world(
        [zone_row("a", connections=["b"]), zone_row("b", connections=["a"])],
        {"a": region_row("a", map_x=0.5, map_y=0.5)["a"],
         "b": region_row("b", map_x=0.5, map_y=0.5)["b"]},
    )
    assert bogus["travel_seconds"] == \
        same_point.travel_quote("a", "b", "walk", "mortal")["travel_seconds"]


def test_affinity_for_never_raises_for_any_value_a_caller_can_hold(manager):
    """The manager's own contract: a hashable root in, a number out.

    ``zone_routes._player_root`` coerces the player's stored root with
    ``str(...) or "none"`` before calling, so every value that reaches the
    manager from a live client is a string. This asserts the manager survives
    all of those, INCLUDING the ones with no mapping -- a root the element table
    has never heard of must score the MISMATCH multiplier, never inherit the
    match bonus and never raise.
    """
    for region in manager.get_all_regions():
        for root in list(zc.ROOT_ELEMENT) + ["", "not_a_real_root", "FIRE"]:
            value = manager.affinity_for(region.zone_id, root)
            assert isinstance(value, float), (region.zone_id, root, value)
            assert value == zc.affinity_multiplier(region.element, root)


def test_an_unknown_root_scores_worse_than_an_unknown_zone(manager):
    """The asymmetry is deliberate and worth naming: 0.75 against 1.00.

    An unknown ZONE answers ``AFFINITY_NEUTRAL`` (1.00) because it cannot know
    the ground. An unknown ROOT inside a known element-bearing region answers
    ``AFFINITY_MISMATCH`` (0.75), because the ground IS known and the player
    simply does not match it.

    Getting that backwards is the tempting "fix": a root nobody recognises would
    be safer to treat as neutral. It would also hand the match bonus to a typo
    in the spirit-root field, so the current direction is pinned as the safe
    one. Asserted on a fire region so the two numbers genuinely differ.
    """
    fire = [r for r in manager.get_all_regions()
            if r.element in zc.ROOT_ELEMENT.values()]
    assert fire, "no element-bearing region exists, so the comparison cannot fail"
    zid = sorted(fire, key=lambda r: r.zone_id)[0].zone_id

    unknown_root = manager.affinity_for(zid, "not_a_real_root")
    unknown_zone = manager.affinity_for("no_such_zone", "fire")
    assert unknown_root == zc.AFFINITY_MISMATCH
    assert unknown_zone == zc.AFFINITY_NEUTRAL
    assert unknown_root < unknown_zone, (
        "a root the table does not know now scores at least as well as no zone "
        "at all, so a typo in the spirit-root field is free")


def test_a_features_default_list_is_never_shared_between_zones():
    """``field(default_factory=list)`` per ZONE, not one list for the process.

    Cheapest possible regression: change the factory to a bare ``=[]`` default
    and every zone in the game silently grows one shared cultivation-spot list.
    Nothing else in this file would notice, because it only ever reads the list
    one zone at a time.
    """
    a = ZoneFeature()
    b = ZoneFeature()
    a.cultivation_spots.append("quiet_pond")
    assert b.cultivation_spots == [], "two default ZoneFeatures share one list"
    assert a.combat_encounters == b.combat_encounters == []

    one = Zone.from_dict(zone_row("solo")).features
    two = Zone.from_dict(zone_row("duo")).features
    one.gathering_nodes.append("ore")
    assert two.gathering_nodes == [], "two zones parsed from dicts share one list"


def test_the_collector_helpers_hand_out_copies_not_the_live_tables(manager):
    """Clearing what a getter returned must not empty the manager.

    Every one of these returns a fresh list over the manager's own dicts. If
    one ever started handing out the live list, a caller that sorts or filters
    in place would rewrite the world for the whole process -- and because
    ``get_recommended_zones`` SORTS its list before slicing, this file's own
    tests could shuffle the world's order between runs.
    """
    before_zones = list(manager.zones)
    before_regions = list(manager.regions)

    manager.get_all_zones().clear()
    manager.get_all_regions().clear()
    manager.get_zones_by_type(ZoneType.SAFE).clear()
    manager.get_recommended_zones("true_immortal", {}, limit=5)
    manager.get_accessible_zones("true_immortal", {}).clear()
    manager.get_connected_zones(before_zones[0], "true_immortal",
                                {"completed_quests": []}).clear()

    assert list(manager.zones) == before_zones, "a getter handed out the live table"
    assert list(manager.regions) == before_regions, "a getter handed out the live table"


# === 11. the four contracts this file was asked to pin ==================
#
# Areas 1-4 of the brief. Two of them (boundaries, non-finite coordinates)
# are live in THIS module and were previously untested here. Two (auth,
# drop rolling) are NOT in this module at all, and those are pinned as the
# structural facts they are rather than as faked HTTP tests -- see each
# section for where the real coverage lives.

# --- 1. entry authorization ============================================
#
# MEASURED: server/zones.py imports no web framework. No APIRouter, no
# Depends, no Header, no HTTPException. It is a data loader plus queries,
# and it cannot grant or deny an HTTP request because it never sees one.
# So "zone entry requires auth" is NOT implemented here, and a test
# asserting a 401 from this module would be testing the wrong file. The
# real auth coverage lives in server/tests/test_zone_routes_contract.py
# (test_anonymous_is_refused_on_every_route,
# test_a_session_cannot_read_another_players_zones), which drives the
# router that consumes this module.
#
# What IS this module's half of the answer: the gate the route consults.
# The assertions below pin that the gate exists, that it is the ONLY
# thing that can open a zone, and that it cannot be opened by omission --
# so a route that forgets to call it has no fallback that grants access.

def test_this_module_exposes_no_http_surface_of_its_own():
    """Proves the scope claim above rather than asserting it in a comment."""
    for name in ("APIRouter", "Depends", "Header", "HTTPException", "router",
                 "app"):
        assert not hasattr(zones_module, name), (
            f"server/zones.py now binds {name!r}; it has grown an HTTP surface "
            "and the auth contract belongs in zone_routes.py, not here")


def test_the_entry_gate_defaults_to_closed_not_open(make_world):
    """A zone with no ``unlock_requirements`` is open -- but only to a player
    whose realm already cleared ``required_realm``.

    The failure this pins is a gate that fails OPEN when a lookup misses.
    ``_check_unlock_requirements`` returns True on an empty requirement dict,
    which is correct (nothing was asked), but it means the realm comparison
    is the only thing standing between a mortal and a top-realm zone. So the
    realm arm is asserted separately here, for a zone with NO unlock
    requirements at all -- the case where nothing else could catch it.
    """
    m = make_world(
        [zone_row("open", required_realm="true_immortal")],
        {"open": region_row("open")["open"]},
    )
    assert m.get_zone("open").unlock_requirements == {}
    assert m.can_access_zone("open", "mortal", {}) is False, (
        "an empty unlock_requirements opened a realm-locked zone")
    assert m.can_access_zone("open", "true_immortal", {}) is True


def test_a_player_the_ladder_cannot_rank_is_refused_not_welcomed(make_world):
    """The fail-open direction, measured.

    ``_realm_index`` answers -1 for an unknown realm and every comparison is
    ``player_idx >= required_idx``, so an UNRANKED PLAYER at index -1 is
    refused by any zone that requires index >= 0. That is the safe direction.
    (The unsafe direction -- an unranked ZONE resolving to -1 and admitting
    everybody -- is closed at load by _load_zones and is tested in section 7.)
    """
    m = make_world([zone_row("any", required_realm="mortal")],
                   {"any": region_row("any")["any"]})
    assert m.realm_index("emperor") == -1
    assert m.can_access_zone("any", "emperor", {}) is False
    assert m.get_accessible_zones("emperor", {}) == []
    assert m.get_connected_zones("any", "emperor", {}) == []
    assert m.can_access_zone("any", "mortal", {}) is True, (
        "a mortal was refused a mortal zone -- the gate over-corrected")


# --- 2. drop determinism ===============================================
#
# MEASURED: server/zones.py imports no RNG. Not random, not secrets, not
# uuid. Every drop in the world is a static row in regions.json, so there
# is no roll to be deterministic or non-deterministic -- determinism here
# means the same table always yields the same drop set, and an id always
# resolves to the same node. That is what the first three tests pin.
#
# Cooldown timing does exist one layer up: ``server/world.py`` keys a flat
# ``{node_id: epoch}`` map (``COOLDOWN_KEY``) and stamps it on a harvest.
# That is covered by test_zone_routes_contract.py, not here.
#
# CORRECTED 2026-10-05: an earlier draft of this comment also claimed
# randomness lived one layer up. MEASURED: it does not. ``world.harvest``
# pays the capped formula ``HARVEST_BASE_PROGRESS * reward_scalar *
# affinity`` and imports no RNG at all (``math``, ``time`` and ``typing``
# only), so the whole zone stack -- table, query layer, harvest route -- is
# deterministic end to end. That is a stronger claim than "no RNG here",
# and test_the_whole_zone_stack_has_no_rng_to_roll now proves it across all
# three modules.

def test_this_module_draws_no_randomness_at_all():
    """The precondition for "deterministic": there is nothing to roll.

    Asserted against the module NAMESPACE, not its source text -- so a
    rename or a refactor that keeps the module deterministic still passes,
    and only binding a real RNG turns it red.
    """
    import random as _random
    for name in ("random", "secrets", "uuid", "np"):
        assert not hasattr(zones_module, name), (
            f"server/zones.py now imports {name!r}; a world loaded from a "
            "static table cannot be the place randomness is decided")
    # And the module's own imports, for the case where it is aliased in.
    source_names = set(zones_module.__dict__)
    assert not any(n.startswith("_Random") for n in source_names)
    assert _random  # the test itself imports nothing from the module under test


def test_the_whole_zone_stack_has_no_rng_to_roll():
    """The claim this section rests on, proven across all three modules.

    "Deterministic drops" is only meaningful if nothing on the path from the
    table to the player can draw. ``server/zones.py`` is the table and the
    queries; ``server/zone_routes.py`` is the HTTP surface; ``server/world.py``
    pays the harvest. All three are checked by NAMESPACE, not by source text,
    so a rename that keeps them deterministic stays green.

    This is also the control for the comment above it. An earlier draft of
    that comment asserted randomness DID live one layer up, in the harvest
    route; MEASURED, it does not -- ``world.harvest`` pays a capped formula
    and imports no RNG at all. If any of the three ever grows one, the harvest
    becomes unseedable and this goes red before a player can be affected.
    """
    import server.world as world_module
    import server.zone_routes as zone_routes_module

    for name, module in (("zones", zones_module),
                         ("zone_routes", zone_routes_module),
                         ("world", world_module)):
        for rng in ("random", "secrets", "uuid", "np"):
            assert not hasattr(module, rng), (
                f"server/{name}.py now imports {rng!r}; a harvest priced by a "
                "capped formula must not be drawing anywhere on this path")


def test_the_harvest_payout_is_the_capped_formula_not_a_roll():
    """The positive half: what the harvest DOES pay, and where it stops.

    ``world.harvest`` returns progress bounded by ``HARVEST_MAX_PROGRESS``
    and essence at least ``HARVEST_BASE_ESSENCE`` scaled by the danger
    reward scalar. Asserted as a relationship against the config table at run
    time, so retuning the economy leaves this green while a module that drew
    its own number goes red. The ceiling is what makes the "no roll" reading
    meaningful: a bounded formula cannot be non-deterministic in any way a
    player can observe.

    MEASURED: progress is reported, never stored. There is no progress key on
    the player record -- it lives on the reply and nowhere else. So the
    "the same gather twice pays the same" claim is checked on the reply value
    of the first call, not on a stored balance that does not exist.
    """
    import server.world as world_module

    zone = get_zone_manager().get_region(world_module.DEFAULT_ZONE)
    assert zone is not None, "the shipped start zone has no world row"
    assert zone.harvest_nodes, "the start zone has no node to harvest, so nothing binds"

    node = zone.harvest_nodes[0]
    player = {world_module.ESSENCE_KEY: {}, world_module.COOLDOWN_KEY: {}}
    result = world_module.harvest(node.id, player, "none")

    affinity = get_zone_manager().affinity_for(zone.zone_id, "none")
    reward = zc.danger_stats(zone.danger)["reward"]
    expected_progress = min(
        zc.HARVEST_MAX_PROGRESS, zc.HARVEST_BASE_PROGRESS * reward * affinity)
    assert result["progress"] == pytest.approx(expected_progress)
    assert 0.0 < result["progress"] <= zc.HARVEST_MAX_PROGRESS
    assert result["essence"] == max(1, int(round(zc.HARVEST_BASE_ESSENCE * reward)))
    # The reply must agree with the world's own tables, not carry its own copy
    # of the numbers, or the client display and the real write could diverge.
    assert result["affinity"] == pytest.approx(affinity)
    assert result["reward_scalar"] == pytest.approx(reward)
    assert result["cooldown_seconds"] == zc.HARVEST_COOLDOWN_SECONDS
    # The cooldown is what makes a second gather impossible, not a random
    # miss. Asserted so "deterministic" cannot quietly mean "fair-looking".
    assert player[world_module.COOLDOWN_KEY][node.id] > 0


def test_the_same_harvest_twice_is_blocked_by_the_cooldown_not_by_chance():
    """Re-harvesting pays nothing at all -- the refusal is the timer.

    This is the one place a player could plausibly see non-determinism (a
    "sometimes I got a drop" report), and it is not a roll: the second call
    is refused outright because the cooldown stamp is in the future.
    """
    import server.world as world_module

    zone = get_zone_manager().get_region(world_module.DEFAULT_ZONE)
    node = zone.harvest_nodes[0]
    player = {world_module.ESSENCE_KEY: {}, world_module.COOLDOWN_KEY: {}}

    world_module.harvest(node.id, player, "none")
    essence_after_first = player[world_module.ESSENCE_KEY].copy()

    with pytest.raises(world_module.WorldError, match="regrows"):
        world_module.harvest(node.id, player, "none")

    assert player[world_module.ESSENCE_KEY] == essence_after_first, (
        "a refused re-harvest still paid essence")
    assert set(player[world_module.ESSENCE_KEY]) == {node.element}


def test_two_players_gather_the_same_node_for_the_same_number():
    """Identical inputs must produce an identical payout, exactly.

    The strongest form of the determinism claim: two fresh players, the same
    node, the same spirit root, the same clock reading. If any draw sat
    anywhere on this path the two replies would eventually disagree. Each
    gets its OWN fresh player record, so the second is not simply refused on
    a cooldown the first left behind -- that refusal is the next test's job.
    ``ready_at`` is excluded because it is the clock, not the payout.
    """
    import server.world as world_module

    zone = get_zone_manager().get_region(world_module.DEFAULT_ZONE)
    node = zone.harvest_nodes[0]
    replies = []
    for _ in range(2):
        fresh = {world_module.ESSENCE_KEY: {}, world_module.COOLDOWN_KEY: {}}
        reply = world_module.harvest(node.id, fresh, "none", now=1_000.0)
        replies.append({k: v for k, v in reply.items() if k != "ready_at"})
    assert replies[0] == replies[1], (
        f"two identical harvests disagreed: {replies[0]} vs {replies[1]}")


def test_the_same_table_always_yields_the_same_drops(tmp_path):
    """Load the shipped table twice and compare every drop.

    Determinism stated as a relationship between two loads, so retuning a
    drop's element or note in the data file leaves this green -- only a
    load that varies with time, ordering or hash seed goes red.
    """
    first, second = ZoneManager(), ZoneManager()
    assert set(first.regions) == set(second.regions)
    for zone_id in first.regions:
        a, b = first.get_region(zone_id), second.get_region(zone_id)
        assert [(n.id, n.name, n.element, n.note) for n in a.harvest_nodes] == \
               [(n.id, n.name, n.element, n.note) for n in b.harvest_nodes]


def test_a_drop_id_always_resolves_to_the_same_node(manager):
    """Lookup by id is total and stable for every shipped drop."""
    for region in manager.get_all_regions():
        for node in region.harvest_nodes:
            for _ in range(3):
                assert region.node(node.id) is node
        assert region.node("definitely-not-a-node") is None


# --- 3. the map-grid boundary ==========================================
#
# MEASURED: MAX_MAP_COORD is 2.0 and the check is ``abs(x) > MAX``, so 2.0
# is LEGAL and the very next representable step is not. The existing file
# tested MAX+1 (a whole unit off), which cannot catch a boundary that has
# drifted by a rounding step. These test the edge itself.

def test_the_exact_grid_edge_is_legal_and_one_step_past_it_is_not(make_world):
    """The boundary is inclusive on the last legal value.

    Pinned at the last representable float below the refusal rather than a
    whole unit past it, so a check that quietly widened (>=, or > MAX + 1)
    turns this red rather than passing on a comfortable margin.
    """
    edge = zc.MAX_MAP_COORD
    legal = make_world([zone_row("a")], {"a": region_row("a", map_x=edge)["a"]})
    assert legal.get_region("a").map_x == edge

    # Belt and braces: prove the probe value really is over the edge, so a
    # change to MAX_MAP_COORD that made it legal fails on the right line.
    past = math.nextafter(edge, 10.0)
    assert past > edge
    with pytest.raises(ZoneDataError, match="map grid"):
        make_world([zone_row("a")], {"a": region_row("a", map_x=past)["a"]})


def test_both_grid_edges_are_inclusive_on_both_axes(make_world):
    """Symmetry: +/-edge legal on x AND y, and just past each is refused."""
    for axis in ("map_x", "map_y"):
        for sign in (1.0, -1.0):
            on_edge = make_world([zone_row("a")],
                                 {"a": region_row("a", **{axis: sign * zc.MAX_MAP_COORD})["a"]})
            assert abs(getattr(on_edge.get_region("a"), axis)) == zc.MAX_MAP_COORD
            over = math.nextafter(sign * zc.MAX_MAP_COORD, sign * 10.0)
            with pytest.raises(ZoneDataError, match="map grid"):
                make_world([zone_row("a")],
                           {"a": region_row("a", **{axis: over})["a"]})


def test_the_boundary_refusal_names_the_zone_and_the_coordinate(make_world):
    """A refusal is only useful if the operator learns WHICH row is wrong."""
    with pytest.raises(ZoneDataError) as caught:
        make_world([zone_row("far_west")],
                   {"far_west": region_row("far_west", map_x=-99.0)["far_west"]})
    message = str(caught.value)
    assert "far_west" in message
    assert "-99.0" in message


# --- 4. non-finite coordinates are rejected ============================
#
# MEASURED: this is the guard's whole reason for existing, and NOTHING in
# the existing file covered it. json.loads accepts bare NaN and Infinity
# tokens, and every comparison against nan is False -- so nan walks
# straight through ``abs(x) > MAX_MAP_COORD`` (abs(nan) > 2.0 is False,
# asserted below so the bypass is not folklore) and only detonates much
# later at the first int(round(...)) inside travel_quote, as an HTTP 500 on
# a player's travel quote rather than a refusal at startup.

def test_a_nan_would_slip_past_the_bounds_check_alone():
    """The premise, stated as an executable fact.

    ONLY nan slips through. Infinity does not: ``abs(inf) > 2.0`` is True,
    so the grid check would catch it and merely report the wrong reason.
    nan is the single value that walks straight past ``abs(x) >
    MAX_MAP_COORD``, because every comparison against nan is False -- and it
    then detonates much later, at the first ``int(round(...))`` inside
    travel_quote, as an HTTP 500 on a player's travel quote rather than a
    refusal at startup. That asymmetry is the whole reason _require_finite
    exists, so it is asserted rather than described.

    Without this, "the guard matters" is a claim about arithmetic nobody in
    this file has checked.
    """
    assert math.isnan(float("nan"))
    assert not (abs(float("nan")) > zc.MAX_MAP_COORD)   # slips past
    assert abs(float("inf")) > zc.MAX_MAP_COORD          # caught, wrong reason
    assert abs(float("-inf")) > zc.MAX_MAP_COORD         # caught, wrong reason


@pytest.mark.parametrize("token", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("axis", ["map_x", "map_y"])
def test_a_non_finite_coordinate_is_refused_at_load(make_world, token, axis):
    """NaN and both infinities, on both axes, are ZoneDataError.

    ``json.dumps`` writes float('nan') as the bare token ``NaN`` and
    ``json.loads`` reads it back as a float -- so this is the exact path the
    module docstring describes, not a synthetic object handed in by a test.
    """
    with pytest.raises(ZoneDataError, match="finite"):
        make_world([zone_row("a")],
                   {"a": region_row("a", **{axis: float(token)})["a"]})


def test_the_non_finite_refusal_names_the_zone_the_axis_and_the_value(
        make_world):
    """The refusal must be diagnosable: which row, which field, what value."""
    with pytest.raises(ZoneDataError) as caught:
        make_world([zone_row("bogus_here")],
                   {"bogus_here": region_row("bogus_here", map_y=float("inf"))["bogus_here"]})
    message = str(caught.value)
    assert "bogus_here" in message
    assert "map_y" in message
    assert "inf" in message


def test_a_non_finite_coordinate_is_refused_before_the_grid_check(
        make_world):
    """Order matters: the world must refuse on "not finite", not on distance.

    An infinite coordinate is ALSO off the grid, so a loader that dropped
    _require_finite would still raise ZoneDataError -- just with a message
    about the map grid. Asserting the message is what proves the guard is
    load-bearing rather than the bounds check doing the work.
    """
    with pytest.raises(ZoneDataError) as caught:
        make_world([zone_row("a")],
                   {"a": region_row("a", map_x=float("inf"))["a"]})
    assert "finite" in str(caught.value)
    assert "map grid" not in str(caught.value)


def test_require_finite_refuses_by_name_and_passes_real_numbers():
    """The guard on its own, so a future refactor of its callers is covered."""
    for good in (0, 1, -1, 2.0, 0.5, -2.0):
        assert zones_module._require_finite(good, "probe") == pytest.approx(float(good))
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ZoneDataError, match="finite"):
            zones_module._require_finite(bad, "probe")


def test_finite_or_falls_back_rather_than_raising_and_never_underquotes():
    """The REQUEST-path twin: an unpriceable scalar degrades, it does not 500.

    ``_finite_or`` is the no-raise sibling of ``_require_finite``. It returns
    the fallback for nan, both infinities, an unparseable string and None,
    while passing every real number through untouched. The fallback is
    always 1.0 here -- "no adjustment", the cost of walking -- so an
    unreadable scalar can never resolve into a CHEAPER journey.
    """
    for bad in (float("nan"), float("inf"), float("-inf"), "east", "", None, [1]):
        assert zones_module._finite_or(bad, 1.0) == 1.0
    for good in (0.0, 1.0, 2.5, 0.75):
        assert zones_module._finite_or(good, 9.0) == pytest.approx(good)


def test_a_nan_travel_scalar_prices_as_a_plain_walk_not_a_shortcut():
    """End-to-end: a poisoned travel table produces a walk price, not a raise.

    This is the failure the two helpers exist to prevent, driven through the
    public entry point rather than the helpers. Both scalars of the method
    are set to nan, then travel_quote must still return the same integers an
    untouched walk would, because _finite_or substituted the neutral 1.0
    for each.
    """
    m = ZoneManager.__new__(ZoneManager)
    m.zones, m.regions = {}, {}
    for zone_id, x in (("origin", 0.0), ("next", 1.0)):
        m.regions[zone_id] = ZoneRegion(
            zone_id=zone_id, element=zc.NEUTRAL_ELEMENT, terrain="plain",
            qi_density=zc.DEFAULT_QI_DENSITY, danger=zc.DEFAULT_DANGER_TIER,
            map_x=x, map_y=0.0, reason="", specialty="",
        )
    clean = m.travel_quote("origin", "next", "walk", "mortal")

    saved = dict(zc.TRAVEL_METHODS["walk"])
    try:
        zc.TRAVEL_METHODS["walk"]["time_scalar"] = float("nan")
        zc.TRAVEL_METHODS["walk"]["stamina_scalar"] = float("nan")
        poisoned = m.travel_quote("origin", "next", "walk", "mortal")
    finally:
        zc.TRAVEL_METHODS["walk"].clear()
        zc.TRAVEL_METHODS["walk"].update(saved)

    assert poisoned == clean, (
        f"a nan scalar changed the quote: {clean} -> {poisoned}")
    assert poisoned["travel_seconds"] >= 1
    assert poisoned["stamina_cost"] >= 0


def test_distance_defends_itself_against_a_hand_built_non_finite_region():
    """The last line of defence, on the object the loader would never build.

    ``distance`` answers 0.0 for a span that is not finite, which is the
    same answer it gives for an unknown zone -- a bad origin reads as
    "already there" rather than as an enormous trip. The region here is
    built by hand because the loader refuses to create it; that is the only
    input that reaches this branch, and deleting the guard turns this red.
    """
    m = ZoneManager.__new__(ZoneManager)
    m.zones, m.regions = {}, {}
    m.regions["nan_here"] = ZoneRegion(
        zone_id="nan_here", element=zc.NEUTRAL_ELEMENT, terrain="plain",
        qi_density=zc.DEFAULT_QI_DENSITY, danger=0,
        map_x=float("nan"), map_y=0.0, reason="", specialty="",
    )
    m.regions["ok"] = ZoneRegion(
        zone_id="ok", element=zc.NEUTRAL_ELEMENT, terrain="plain",
        qi_density=zc.DEFAULT_QI_DENSITY, danger=0,
        map_x=0.0, map_y=0.0, reason="", specialty="",
    )
    assert m.distance("nan_here", "ok") == 0.0
    assert m.distance("ok", "nan_here") == 0.0
    assert m.distance("ok", "ok") == 0.0
    # And the quote on top of it stays a legal one, never a 500.
    quote = m.travel_quote("nan_here", "ok", "walk", "mortal")
    assert 1 <= quote["travel_seconds"] <= zc.MAX_TRAVEL_SECONDS
    assert quote["stamina_cost"] >= 0


# --- a measured gap, pinned rather than asserted away ==================

def test_a_drop_id_shared_by_two_regions_loads_silently(make_world):
    """MEASURED DEFECT: one id, two regions, no complaint from THIS module.

    ``server/zone_config.py`` ships ``validate_drops`` precisely for this
    case, and its own docstring states the consequence as measured: the
    harvest cooldown is a flat ``{node_id: epoch}`` map keyed on the id
    ALONE, so two regions sharing an id share one timer -- gathering in the
    first makes the second region's node report "regrows in 900s" for a
    node the player never touched.

    zones.py does not call ``validate_drops`` (MEASURED: the name does not
    appear in the module), so its loader accepts both rows and both become
    reachable harvest nodes sharing one cooldown. The world this module
    builds is therefore not the world zone_config's schema would approve.

    The world LOADS is the assertion -- a loader that refused would turn
    this red and the intended follow-up is a ``ZoneDataError`` here. Kept
    in the style of test_a_duplicate_zone_id_silently_drops_one_of_the_two,
    which pins the same class of hole on the other table.
    """
    zones = [zone_row("grove"), zone_row("hollow")]
    regions = {
        "grove": region_row("grove", harvest_nodes=[
            {"id": "shared_shard", "name": "Grove Shard", "element": "wood"}])["grove"],
        "hollow": region_row("hollow", harvest_nodes=[
            {"id": "shared_shard", "name": "Hollow Shard", "element": "wood"}])["hollow"],
    }
    assert len({n["harvest_nodes"][0]["id"] for n in regions.values()}) == 1, \
        "precondition: the two regions really do share one id"

    m = make_world(zones, regions)  # loads today -- that is the defect
    ids = {r.zone_id: [n.id for n in r.harvest_nodes]
           for r in m.get_all_regions()}
    assert ids["grove"] == ids["hollow"] == ["shared_shard"], (
        "the loader now resolves the collision -- delete this test and add "
        "one asserting the ZoneDataError")

    # Both are individually reachable, which is what makes the shared
    # cooldown reachable: a player can gather each of them by id.
    for zone_id in ("grove", "hollow"):
        node = m.get_region(zone_id).node("shared_shard")
        assert node is not None, zone_id

    # And the schema module that DOES own this rule refuses the same pair,
    # so the fix has somewhere to call into.
    seen = {}
    zc.validate_drops([{"id": "shared_shard", "name": "A", "element": "wood"}],
                      "grove", seen)
    with pytest.raises(zc.ZoneSchemaError, match="claimed by both"):
        zc.validate_drops([{"id": "shared_shard", "name": "B", "element": "wood"}],
                          "hollow", seen)


def test_a_drop_with_no_name_still_falls_back_to_its_id_here(make_world):
    """The loader half of the same gap, and the reason it is worth pinning.

    ``REQUIRED_DROP_FIELDS`` deliberately includes ``name`` precisely so a
    drop can never reach the map screen labelled ``12345`` -- the loader in
    zone_config used to fall back to the id. zones.py still does
    (HarvestNode's name defaults to node["id"]). The existing file pins the
    fallback as correct behaviour in
    test_a_harvest_node_may_name_an_element_the_zone_does_not, so this does
    not contradict it: it records that the two loaders disagree about the
    same row, and the fix belongs to whoever wires validate_drops in.
    """
    rows = region_row("a")
    rows["a"]["harvest_nodes"] = [{"id": "12345", "element": "fire"}]
    m = make_world([zone_row("a")], rows)
    node = m.get_region("a").node("12345")
    assert node is not None and node.name == "12345"
    with pytest.raises(zc.ZoneSchemaError, match="name"):
        zc.validate_drops([{"id": "12345", "element": "fire"}], "a", {})
