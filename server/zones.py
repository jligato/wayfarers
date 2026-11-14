"""
Zone system for Wayfarer's Cycle.

Two tables, one owner each:

* ``shared/zones/zones.json``  -- identity and topology. Zone names, realm
  gates, connections, npcs. Owned by the original zone work.
* ``shared/zones/regions.json`` -- world shape. The element each region sits
  on, its qi density, its danger tier, what grows there, and where it is on
  the map grid. Added by the world-map work; validated here.

``server/zone_config.py`` owns the MEANING of every field. This module loads
the two tables, refuses to load a world that is internally inconsistent, and
answers questions about it.

Loading is fail-loud on purpose
------------------------------
A missing region row, an element the config table has never heard of, a
danger tier past the top of the ladder, or a coordinate off the map grid all
raise at construction. Every one of those is a silent-corruption bug if it is
tolerated: a zone with no element scores 1.0 affinity for everyone and looks
like a design choice, and a zone with no danger tier becomes a free reward
faucet. A loud failure at startup is cheaper than either.
"""
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from server import zone_config as zc


#: Scalar used when a travel method's own scalar cannot be read as a finite
#: number. 1.0 is "no adjustment", which is what plain walking costs.
_NEUTRAL_SCALAR: float = 1.0

#: Last-resort stamina cap for the same case, so an unreadable scalar cannot
#: produce a number with no bound above it.
#:
#: MEASURED, not guessed. Measured from the live config on 2026-10-05:
#:   TRAVEL_STAMINA_PER_DISTANCE (8)
#:   x largest distance the LOADER permits, hypot over the +-MAX_MAP_COORD
#:     square it accepts: hypot(4, 4) = 5.656854
#:   x largest stamina_scalar in TRAVEL_METHODS (2.0, teleport)
#:   = 8 * 5.656854 * 2.0 = 90.5097
#: Measured against the shipped 22-zone map for reference: the dearest real
#: quote is 16 (teleport across the longest leg, 0.990353 grid units).
#:
#: The bound used is the LOADER-permitted ceiling, not the shipped max,
#: because this cap exists to survive bad data: a map that grew a zone at
#: a wider coordinate is legitimate, and a cap below its cost would quote
#: the trip CHEAPER than it is -- the one failure a fallback must not make.
#: 128.0 is the next power of two above the measured 90.5097, giving 1.41x
#: headroom so a later MAX_MAP_COORD or travel-method change cannot silently
#: turn this guard into an under-quote. This mirrors MAX_TRAVEL_SECONDS,
#: which carries 13.9x headroom over its own loader-permitted maximum.
_NEUTRAL_STAMINA_CAP: float = 128.0


def _require_finite(value: Any, what: str) -> float:
    """Convert to float and refuse nan or inf by name.

    ``json.loads`` accepts bare ``NaN`` and ``Infinity`` tokens, so a data
    file can hand a loader something that is not a number at all. Every
    comparison against nan is False, so a nan walks straight through a
    ``abs(x) > MAX`` bounds check and only detonates much later at the first
    ``int(round(...))`` with a ValueError -- one wrong token in a table turns
    a travel quote into an HTTP 500. Refuse at the conversion instead, where
    the offending zone id is still known and the message can name it.
    """
    out = float(value)
    if not math.isfinite(out):
        raise ZoneDataError(
            f"{what} is {out!r}, which is not a finite number. The world "
            "tables are read with json.loads, which accepts bare NaN and "
            "Infinity tokens. Every comparison against nan is False, so it "
            "would pass the map-grid bounds check and then raise ValueError "
            "when something tries to round it to an integer, which is an "
            "HTTP 500 rather than a refusal."
        )
    return out


def _finite_or(value: Any, fallback: float) -> float:
    """Convert to float on a request path that must not raise.

    A value that cannot be read as a finite number returns ``fallback``
    instead of raising. The fallback is a module constant rather than
    anything read out of the config table: deriving it from the table
    would mean a table carrying a nan produced a nan fallback and the
    guard handed the poison straight through. Callers pair this with
    an existing upper cap, so a fallback can never make a cost come out
    cheaper.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return out if math.isfinite(out) else fallback


class ZoneType(Enum):
    SAFE = "safe"
    WILD = "wild"
    TRADE = "trade"
    CULTIVATION = "cultivation"
    SECT = "sect"
    TRANSCENDENT = "transcendent"
    DUNGEON = "dungeon"
    ENDGAME = "endgame"


class ZoneDataError(ValueError):
    """Raised when the world tables contradict each other. Never caught."""


@dataclass
class ZoneFeature:
    cultivation_spots: List[str] = field(default_factory=list)
    combat_encounters: List[str] = field(default_factory=list)
    gathering_nodes: List[str] = field(default_factory=list)
    sect_outpost: bool = False
    teleport_enabled: bool = True


@dataclass
class HarvestNode:
    """One gatherable thing in one region.

    ``element`` is the element of the MATERIAL, which is not always the zone's
    element: the Moonstone Shard in a wood forest is light qi. That is what
    makes a region's harvest worth travelling for even to a root that
    mismatches its ground.
    """

    id: str
    name: str
    element: str
    note: str = ""


@dataclass
class ZoneRegion:
    """The world shape of one zone: what it is made of, not what is in it."""

    zone_id: str
    element: str
    terrain: str
    qi_density: str
    danger: int
    map_x: float
    map_y: float
    reason: str
    specialty: str
    harvest_nodes: List[HarvestNode] = field(default_factory=list)

    @property
    def qi_index(self) -> int:
        return zc.qi_density_index(self.qi_density)

    @property
    def scarred(self) -> bool:
        return zc.is_scarred(self.qi_density)

    @property
    def danger_stats(self) -> Dict[str, float]:
        return zc.danger_stats(self.danger)

    def node(self, node_id: str) -> Optional[HarvestNode]:
        for n in self.harvest_nodes:
            if n.id == node_id:
                return n
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "element": self.element,
            "terrain": self.terrain,
            "qi_density": self.qi_density,
            "qi_density_index": self.qi_index,
            "qi_density_label": zc.QI_DENSITY_TIERS[self.qi_density]["label"],
            "scarred": self.scarred,
            "danger": self.danger,
            "danger_label": self.danger_stats["label"],
            "encounter_difficulty": self.danger_stats["encounter_difficulty"],
            "reward_scalar": self.danger_stats["reward"],
            "map_x": self.map_x,
            "map_y": self.map_y,
            "reason": self.reason,
            "specialty": self.specialty,
            "harvest_nodes": [
                {"id": n.id, "name": n.name, "element": n.element, "note": n.note}
                for n in self.harvest_nodes
            ],
        }


@dataclass
class Zone:
    id: str
    name: str
    description: str
    required_realm: str
    recommended_realm: str
    zone_type: ZoneType
    background: str
    music: str
    npcs: List[str] = field(default_factory=list)
    features: ZoneFeature = field(default_factory=ZoneFeature)
    connections: List[str] = field(default_factory=list)
    unlock_requirements: Dict[str, Any] = field(default_factory=dict)
    daily_missions: List[str] = field(default_factory=list)
    #: Filled in by ZoneManager from regions.json. Optional on the dataclass
    #: only because Zone.from_dict runs before the region table is read; in a
    #: loaded world it is always set, and _load_regions refuses to finish
    #: without one for every zone.
    region: Optional[ZoneRegion] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Zone":
        features_data = data.get("features", {})
        features = ZoneFeature(
            cultivation_spots=features_data.get("cultivation_spots", []),
            combat_encounters=features_data.get("combat_encounters", []),
            gathering_nodes=features_data.get("gathering_nodes", []),
            sect_outpost=features_data.get("sect_outpost", False),
            teleport_enabled=features_data.get("teleport_enabled", True),
        )
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            required_realm=data["required_realm"],
            recommended_realm=data["recommended_realm"],
            zone_type=ZoneType(data["zone_type"]),
            background=data["background"],
            music=data["music"],
            npcs=data.get("npcs", []),
            features=features,
            connections=data.get("connections", []),
            unlock_requirements=data.get("unlock_requirements", {}),
            daily_missions=data.get("daily_missions", []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "required_realm": self.required_realm,
            "recommended_realm": self.recommended_realm,
            "zone_type": self.zone_type.value,
            "background": self.background,
            "music": self.music,
            "npcs": self.npcs,
            "features": {
                "cultivation_spots": self.features.cultivation_spots,
                "combat_encounters": self.features.combat_encounters,
                "gathering_nodes": self.features.gathering_nodes,
                "sect_outpost": self.features.sect_outpost,
                "teleport_enabled": self.features.teleport_enabled,
            },
            "connections": self.connections,
            "unlock_requirements": self.unlock_requirements,
            "daily_missions": self.daily_missions,
        }


class ZoneManager:
    """Manages zone data and navigation."""

    REALM_ORDER = [
        "mortal",
        "qi_refining",
        "foundation_establishment",
        "golden_core",
        "nascent_soul",
        "spirit_severing",
        "dao_merging",
        "true_immortal",
    ]

    def __init__(self, zones_path: Optional[str] = None,
                 regions_path: Optional[str] = None):
        self.zones: Dict[str, Zone] = {}
        self.regions: Dict[str, ZoneRegion] = {}
        self._load_zones(zones_path)
        self._load_regions(regions_path)

    def _load_zones(self, zones_path: Optional[str] = None) -> None:
        """Read zones.json and refuse any realm the ladder cannot rank.

        Raises ZoneDataError on a zone whose ``required_realm`` or
        ``recommended_realm`` is not a member of ``REALM_ORDER``.

        This is the one door the realm ladder does not watch. ``_realm_index``
        answers -1 for a realm it has never heard of, and every gate in this
        module is written ``player_idx >= required_idx``. A zone asking for a
        realm off the ladder therefore resolved to -1, which EVERY player
        satisfies -- a brand-new mortal was handed the top zone, so a gate
        meant to keep content out became one that let everybody in. The same
        arithmetic on ``recommended_realm`` sorts an unresolvable value as the
        NEAREST possible zone rather than the intended one, so the
        "recommended for your realm" list mis-ranked in silence.

        A gate that cannot rank its own inputs must refuse rather than guess,
        and the loader is the only place that is cheap to notice. Checking here
        rather than at each query also means the four sibling gates
        (``can_access_zone``, ``get_accessible_zones``, ``get_connected_zones``
        and ``get_recommended_zones``) inherit the fix from one check instead
        of needing four.
        """
        if zones_path is None:
            zones_path = Path(__file__).parent.parent / "shared" / "zones" / "zones.json"
        with open(zones_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        known_realms = set(self.REALM_ORDER)
        for zone_data in data.get("zones", []):
            zone = Zone.from_dict(zone_data)
            for field_name in ("required_realm", "recommended_realm"):
                realm = getattr(zone, field_name)
                if realm not in known_realms:
                    raise ZoneDataError(
                        f"zone {zone.id!r} declares {field_name} {realm!r}, which "
                        f"is not one of the {len(self.REALM_ORDER)} realms in "
                        f"ZoneManager.REALM_ORDER. The ladder ranks realms by "
                        f"position and answers -1 for one it cannot find, and "
                        f"every gate here reads player_index >= zone_index, so a "
                        f"{field_name} off the ladder is satisfied by every "
                        f"player including a mortal. The zone would open to "
                        f"everybody instead of being locked."
                    )
            self.zones[zone.id] = zone

    def _load_regions(self, regions_path: Optional[str] = None) -> None:
        """Read regions.json and bind one region to every zone.

        Raises ZoneDataError on any of: a zone with no world row, a world row
        for a zone that does not exist, an unknown element, an unknown qi
        density tier, a danger tier outside the ladder, a coordinate off the
        map grid, a harvest node on an unknown element, or a connection naming
        a zone that is not in the table.
        """
        if regions_path is None:
            regions_path = Path(__file__).parent.parent / "shared" / "zones" / "regions.json"
        with open(regions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("regions", {})

        missing = sorted(set(self.zones) - set(rows))
        if missing:
            raise ZoneDataError(
                f"zones with no region row in {regions_path}: {missing}. Every "
                "zone needs an element, a qi density and a danger tier; a zone "
                "missing one would silently score 1.0 affinity for everyone or "
                "become a free reward."
            )
        orphan = sorted(set(rows) - set(self.zones))
        if orphan:
            raise ZoneDataError(
                f"region rows for zones absent from zones.json: {orphan}"
            )

        for zone_id, row in rows.items():
            element = row.get("element", zc.NEUTRAL_ELEMENT)
            if element not in zc.ELEMENTS:
                raise ZoneDataError(
                    f"zone {zone_id!r} declares element {element!r}, which is not "
                    f"one of the {len(zc.ELEMENTS)} elements in "
                    "server/zone_config.ELEMENTS. An element nothing recognises "
                    "matches no spirit root, so the zone reads as a mismatch for "
                    "every player in the game."
                )
            density = row.get("qi_density", zc.DEFAULT_QI_DENSITY)
            if density not in zc.QI_DENSITY_TIERS:
                raise ZoneDataError(
                    f"zone {zone_id!r} declares qi_density {density!r}, which is "
                    f"not a tier. Valid tiers: {sorted(zc.QI_DENSITY_TIERS)}"
                )
            danger = row.get("danger", zc.DEFAULT_DANGER_TIER)
            if not isinstance(danger, int) or isinstance(danger, bool) \
                    or not 0 <= danger <= zc.MAX_DANGER_TIER:
                raise ZoneDataError(
                    f"zone {zone_id!r} declares danger {danger!r}, which is not an "
                    f"integer 0..{zc.MAX_DANGER_TIER}. The ladder tops out at "
                    f"{zc.MAX_DANGER_TIER}; a tier past it would be a multiplier "
                    "no other table knows about."
                )
            map_x = _require_finite(
                row.get("map_x", 0.0), f"zone {zone_id!r} map_x"
            )
            map_y = _require_finite(
                row.get("map_y", 0.0), f"zone {zone_id!r} map_y"
            )
            if abs(map_x) > zc.MAX_MAP_COORD or abs(map_y) > zc.MAX_MAP_COORD:
                raise ZoneDataError(
                    f"zone {zone_id!r} sits at ({map_x}, {map_y}), off the "
                    f"{zc.MAP_GRID}-unit map grid. A typo'd coordinate becomes a "
                    "multi-hour journey."
                )
            nodes: List[HarvestNode] = []
            for node in row.get("harvest_nodes", []):
                node_element = node.get("element", element)
                if node_element not in zc.ELEMENTS:
                    raise ZoneDataError(
                        f"harvest node {node.get('id')!r} in zone {zone_id!r} "
                        f"declares element {node_element!r}, which is not a known element"
                    )
                nodes.append(HarvestNode(
                    id=node["id"],
                    name=node.get("name", node["id"]),
                    element=node_element,
                    note=node.get("note", ""),
                ))
            self.regions[zone_id] = ZoneRegion(
                zone_id=zone_id,
                element=element,
                terrain=row.get("terrain", zc.ELEMENT_NATIVE_TERRAIN[element]),
                qi_density=density,
                danger=danger,
                map_x=map_x,
                map_y=map_y,
                reason=row.get("reason", ""),
                specialty=row.get("specialty", ""),
                harvest_nodes=nodes,
            )

        for zone_id, zone in self.zones.items():
            zone.region = self.regions[zone_id]
        for zone_id, zone in self.zones.items():
            for conn in zone.connections:
                if conn not in self.zones:
                    raise ZoneDataError(
                        f"zone {zone_id!r} lists a connection to {conn!r}, which "
                        "is not a zone. Walking to it would fail on a None lookup."
                    )

    # --- world queries ---------------------------------------------------

    def get_region(self, zone_id: str) -> Optional[ZoneRegion]:
        return self.regions.get(zone_id)

    def get_all_regions(self) -> List[ZoneRegion]:
        return list(self.regions.values())

    def distance(self, from_zone_id: str, to_zone_id: str) -> float:
        """Straight-line map distance between two zones, in grid units.

        0.0 when either zone is unknown, so a bad origin reads as "already
        there" rather than as an enormous trip. Every caller has validated both
        ids before reaching this, so that branch is a guard, not a path.
        """
        a = self.regions.get(from_zone_id)
        b = self.regions.get(to_zone_id)
        if a is None or b is None:
            return 0.0
        span = math.hypot(a.map_x - b.map_x, a.map_y - b.map_y)
        if not math.isfinite(span):
            # A non-finite coordinate is a bad origin, which is exactly what
            # the unknown-zone branch above already answers as "already
            # there" rather than as an enormous trip.
            return 0.0
        return span

    def affinity_for(self, zone_id: str, spirit_root: str) -> float:
        """Cultivation rate scalar for this spirit root in this zone."""
        region = self.regions.get(zone_id)
        if region is None:
            return zc.AFFINITY_NEUTRAL
        return zc.affinity_multiplier(region.element, spirit_root)

    def travel_quote(
        self,
        from_zone_id: str,
        to_zone_id: str,
        method: str,
        player_realm: str,
    ) -> Dict[str, Any]:
        """What one journey costs, in wall-clock seconds and stamina.

        Three inputs, all of them real:

        * DISTANCE from the map grid. A neighbour is cheap, a corner is not.
        * DESTINATION danger, on the time axis. Climbing into Deadly ground is
          slow, because time is the one cost an idle player cannot buy past.
        * REALM mitigation, applied to stamina only by the caller. Canon is
          explicit that realm reduces qi burn and never the speed ceiling, so a
          Nascent Soul crosses the same road as a mortal in the same
          wall-clock time and pays a fraction of the stamina.

        The crossed qi density is applied by ``world.travel_stamina``, which
        mixes the two endpoints, because a road is only as good as its worse
        half.
        """
        method_row = zc.travel_method(method)
        distance = self.distance(from_zone_id, to_zone_id)
        target = self.regions.get(to_zone_id)

        danger_time = zc.TRAVEL_DANGER_TIME_SCALAR.get(
            target.danger if target else zc.DEFAULT_DANGER_TIER, 1.0
        )
        # Fall back to the neutral scalar, NOT to the dearest one in the
        # table: reading the fallback out of TRAVEL_METHODS means a table
        # carrying a nan produces a nan fallback, and the guard then hands
        # the poison straight to int(). A method whose scalar cannot be read
        # is priced as an ordinary walk, and seconds is separately capped at
        # MAX_TRAVEL_SECONDS, so no leg of this can come out under-quoted.
        seconds = zc.TRAVEL_BASE_SECONDS + zc.TRAVEL_SECONDS_PER_DISTANCE * distance
        seconds *= danger_time * _finite_or(method_row["time_scalar"], _NEUTRAL_SCALAR)
        # A nan reaches here only if distance itself is nan, which the loader
        # and distance() now both refuse. Clamping the cap instead of trusting
        # that keeps the max()/min() pair below from passing a nan straight
        # through to int(), where it would raise instead of pricing.
        seconds = max(1, min(int(round(_finite_or(seconds, zc.MAX_TRAVEL_SECONDS))),
                            zc.MAX_TRAVEL_SECONDS))

        stamina = zc.TRAVEL_STAMINA_PER_DISTANCE * distance
        stamina *= _finite_or(method_row["stamina_scalar"], _NEUTRAL_SCALAR)
        stamina_cost = max(0, int(round(_finite_or(stamina, _NEUTRAL_STAMINA_CAP))))
        return {
            "travel_seconds": seconds,
            "stamina_cost": stamina_cost,
            "distance": round(distance, 4),
            "method": method,
            "danger_time_scalar": danger_time,
        }

    def get_zone(self, zone_id: str) -> Optional[Zone]:
        return self.zones.get(zone_id)

    def get_all_zones(self) -> List[Zone]:
        return list(self.zones.values())

    def get_zones_by_type(self, zone_type: ZoneType) -> List[Zone]:
        return [z for z in self.zones.values() if z.zone_type == zone_type]

    def get_accessible_zones(self, player_realm: str, player_data: Dict[str, Any]) -> List[Zone]:
        """Get zones accessible to a player based on realm and unlock requirements."""
        player_realm_idx = self._realm_index(player_realm)
        accessible = []

        for zone in self.zones.values():
            required_idx = self._realm_index(zone.required_realm)
            if player_realm_idx < required_idx:
                continue

            # Check unlock requirements
            if not self._check_unlock_requirements(zone, player_data):
                continue

            accessible.append(zone)

        return accessible

    def get_connected_zones(self, zone_id: str, player_realm: str, player_data: Dict[str, Any]) -> List[Zone]:
        """Get zones connected to the current zone that the player can access."""
        zone = self.get_zone(zone_id)
        if not zone:
            return []

        connected = []
        for conn_id in zone.connections:
            conn_zone = self.get_zone(conn_id)
            if not conn_zone:
                continue
            required_idx = self._realm_index(conn_zone.required_realm)
            player_idx = self._realm_index(player_realm)
            if player_idx >= required_idx and self._check_unlock_requirements(conn_zone, player_data):
                connected.append(conn_zone)
        return connected

    def can_access_zone(self, zone_id: str, player_realm: str, player_data: Dict[str, Any]) -> bool:
        """Check if player can access a specific zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return False
        required_idx = self._realm_index(zone.required_realm)
        player_idx = self._realm_index(player_realm)
        if player_idx < required_idx:
            return False
        return self._check_unlock_requirements(zone, player_data)

    def get_recommended_zones(self, player_realm: str, player_data: Dict[str, Any], limit: int = 5) -> List[Zone]:
        """Get recommended zones for the player's current realm."""
        accessible = self.get_accessible_zones(player_realm, player_data)
        player_idx = self._realm_index(player_realm)

        # Sort by how close the recommended realm is to player's realm
        def sort_key(z: Zone) -> int:
            rec_idx = self._realm_index(z.recommended_realm)
            return abs(rec_idx - player_idx)

        accessible.sort(key=sort_key)
        return accessible[:limit]

    def get_zone_daily_missions(self, zone_id: str) -> List[str]:
        """Get daily missions available in a zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return []
        return zone.daily_missions

    def get_zone_npcs(self, zone_id: str) -> List[str]:
        """Get NPCs present in a zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return []
        return zone.npcs

    def get_zone_features(self, zone_id: str) -> Optional[ZoneFeature]:
        """Get features available in a zone."""
        zone = self.get_zone(zone_id)
        if not zone:
            return None
        return zone.features

    def _realm_index(self, realm: str) -> int:
        try:
            return self.REALM_ORDER.index(realm)
        except ValueError:
            return -1

    def realm_index(self, realm: str) -> int:
        """Ordinal for a realm. -1 when the realm is not in the ladder."""
        return self._realm_index(realm)

    def _check_unlock_requirements(self, zone: Zone, player_data: Dict[str, Any]) -> bool:
        reqs = zone.unlock_requirements
        if not reqs:
            return True

        # Check quest completion
        if "quest" in reqs:
            completed_quests = player_data.get("completed_quests", [])
            if reqs["quest"] not in completed_quests:
                return False

        # Check sect membership
        if reqs.get("sect_member", False):
            if not player_data.get("sect_id"):
                return False

        # Check sect rank
        if "sect_rank" in reqs:
            player_rank = player_data.get("sect_rank", "")
            if player_rank != reqs["sect_rank"]:
                return False

        # Check sect reputation
        if "sect_reputation" in reqs:
            player_rep = player_data.get("sect_reputation", 0)
            if player_rep < reqs["sect_reputation"]:
                return False

        # Check karma
        if "karma" in reqs:
            player_karma = player_data.get("karma", "neutral")
            if player_karma != reqs["karma"]:
                return False

        return True


# Global instance
_zone_manager: Optional[ZoneManager] = None


def get_zone_manager() -> ZoneManager:
    global _zone_manager
    if _zone_manager is None:
        _zone_manager = ZoneManager()
    return _zone_manager


def reload_zones() -> None:
    global _zone_manager
    _zone_manager = ZoneManager()
