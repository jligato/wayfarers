"""
Wayfarer's Cycle — World Map Configuration

The data table behind the ZONE / WORLD MAP system: what the world is made of,
what each region is shaped by, and what moving between regions costs.

House style, same as ``combat_config.py``, ``cultivation_config.py`` and
``tower_config.py``
- plain module constants, no classes with behaviour
- every non-obvious number carries a comment saying WHY it is that number
- nothing here reads a database or imports another service module

Where the numbers come from
---------------------------
The taxonomy is NOT invented here. It is lifted from the project's own
worldbuilding canon, which is written down in three places:

* ``xianxia-element-terrain-element-qi-density-mapping`` — the element ->
  native terrain -> qi density table. A volcano is PURE fire qi and out-yields
  a campfire; a rift is void qi and is scarce. Density scales with how purely
  the terrain expresses its element.
* ``xcultivation-travel-speed`` — the ambient qi density tiers and their
  resistance multipliers, and the rule that a cultivator's realm reduces qi
  BURN, never the speed ceiling. Higher realm travels the same road for less.
* ``combat_config.ELEMENTS`` / ``ELEMENT_ADVANTAGE`` — this game's own eight
  combat elements. The world map is wider than combat (twelve elements, per
  canon), so a zone may carry an element combat does not model. Those are
  listed in ``COMBAT_ELEMENTS`` and are legal on a zone; they simply do not
  feed the damage table.

The three laws the world obeys
------------------------------
1. TERRAIN MAKES THE ELEMENT. Every zone declares one element and one qi
   density tier. A zone with no element is a civilised place (a village, a
   trade road) where the ambient qi is thin and formless, so it cultivates
   nobody and hosts no encounters worth the name.
2. THE FASTEST ZONE IS NOT THE RICHEST ZONE. Each region exists for one
   reason, stated in ``ZONE_REASON``. A rich vein is dangerous. A safe town is
   poor. A trade road has no qi at all but has the best selling prices. The
   map is a set of trade-offs, not a ladder where the last zone wins
   everything.
3. ARRIVING IS AN EVENT. A journey costs server time (``TRAVEL_BASE_SECONDS``
   scaled by distance and difficulty), and the player is not *in* the target
   zone until the clock says so. This is what makes coming back matter: the
   zone you left is still earning for nobody while you are on the road.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# === Element taxonomy =====================================================
# The twelve canon elements, plus NEUTRAL_ELEMENT for civilised ground that
# sits on no vein at all. Order is stable: it is used to seed the map's
# deterministic colour choice on the client, so appending is safe but
# reordering would repaint saved screenshots.
ELEMENTS: List[str] = [
    "wood", "fire", "earth", "metal", "water", "light", "dark",
    "void", "life", "death", "lightning", "wind", "neutral",
]

#: The eight elements ``combat_config.ELEMENTS`` models. A zone whose element
#: is in this set can be fought with the shipped damage table. The other four
#: (light, life, death, void) and NEUTRAL_ELEMENT are canon-valid terrain
#: labels that combat does not resolve, so they grant affinity and harvest but
#: never a damage multiplier. Silently treating an unmodelled element as
#: "fire" would be worse than admitting combat ignores it.
COMBAT_ELEMENTS: frozenset = frozenset({
    "fire", "water", "earth", "wood", "metal", "wind", "lightning", "void",
})

#: The element a civilised zone carries. Chosen over None so every zone has a
#: real element field and the client never has to handle a null.
NEUTRAL_ELEMENT: str = "neutral"

#: Native terrain per element, copied from the canon density table. This is
#: carried in the data file per zone (``terrain``) rather than derived, because
#: a zone's terrain is a description, not a function of its element — the
#: Desert Wastes are fire-element and stony, the Foundry Gorge is fire-element
#: and metallic. Kept here as the lookup the data file is validated against.
ELEMENT_NATIVE_TERRAIN: Dict[str, str] = {
    "wood": "forest",
    "fire": "volcano",
    "earth": "mountain",
    "metal": "mine",
    "water": "river",
    "light": "high_peak",
    "dark": "cave",
    "void": "rift",
    "life": "wetland",
    "death": "bone_field",
    "lightning": "storm_peak",
    "wind": "steppe",
    NEUTRAL_ELEMENT: "plain",
}

#: Which element a spirit root grants affinity with. ``server.main`` accepts
#: spirit_root values straight off the client's ROOT_IDS list
#: ("pure", "fire", "water", "earth", "wood", "metal", "dual_fire_metal",
#: "none"), so a root that is not itself an element maps to neutral rather
#: than raising. An unmapped root must never inherit an arbitrary element's
#: bonus, which is the failure this mapping exists to prevent.
ROOT_ELEMENT: Dict[str, str] = {
    "pure": NEUTRAL_ELEMENT,
    "fire": "fire",
    "water": "water",
    "earth": "earth",
    "wood": "wood",
    "metal": "metal",
    "none": NEUTRAL_ELEMENT,
    # Dual roots are two elements at once. They are handled as a SET by
    # ``root_elements`` below, not by a single string, so a fire/metal
    # cultivator earns affinity in BOTH the Foundry and the Deep Cavern rather
    # than being handed one arbitrarily.
    "dual_fire_metal": "fire",
}

#: Roots that carry two elements. Kept beside ROOT_ELEMENT rather than inside
#: it so a single-element lookup can never accidentally resolve a dual.
DUAL_ROOT_ELEMENTS: Dict[str, Tuple[str, ...]] = {
    "dual_fire_metal": ("fire", "metal"),
}

#: Affinity multiplier for a spirit root that MATCHES the zone's element.
#: 1.35 and not 2.0 on purpose. Canon is explicit that density scales with how
#: purely the terrain expresses the element, so affinity is a real edge — but
#: the interesting decision is WHERE TO STAND, and a 2x edge makes the
#: matching zone strictly dominant and kills the rest of the map.
AFFINITY_MATCH: float = 1.35

#: Multiplier for a root that does not match. Below 1.0 so an ice-water
#: cultivator standing in a volcano genuinely cultivates worse there, which
#: is what makes travel a decision. Floor is not 0.0: a mismatched zone is
#: still worth standing in for its harvest and its encounters.
AFFINITY_MISMATCH: float = 0.75

#: Multiplier in a zone whose element is NEUTRAL_ELEMENT. Exactly 1.0. A town
#: gives no edge to anyone, which is what makes it a fair fallback and a fair
#: place to be sent home to.
AFFINITY_NEUTRAL: float = 1.0

# === Qi density tiers =====================================================
# The ambient qi density ladder and the drag it puts on a traveller, from
# ``xcultivation-travel-speed``'s density table. TIERS is ordered thin -> rich
# so a zone's index is its cultivation value.
#
# ``resistance`` is the multiplier applied to travel BURN (stamina), not to
# travel time. Dense qi is easier to move through, so a rich-vein region is
# CHEAPER to cross per kilometre than a scarred one — which is counter-intuitive
# if you only think of danger, and is exactly the trade-off canon describes.
QI_DENSITY_TIERS: Dict[str, Dict[str, float]] = {
    "void":     {"index": 0, "resistance": 2.50, "label": "Void"},
    "thin":     {"index": 1, "resistance": 2.00, "label": "Thin"},
    "ambient":  {"index": 2, "resistance": 1.00, "label": "Ambient"},
    "moderate": {"index": 3, "resistance": 0.70, "label": "Moderate"},
    "rich":     {"index": 4, "resistance": 0.30, "label": "Rich"},
    "sacred":   {"index": 5, "resistance": 0.10, "label": "Sacred"},
}

#: Density given to a zone with no declared tier. ``ambient`` and not 0.0,
#: because a missing row must not read as a dead zone: it would make a
#: configuration typo look like a design choice.
DEFAULT_QI_DENSITY: str = "ambient"

#: Density at which a zone is considered scarred. A scarred zone is a canon
#: category of its own (see the xscarred-qi-zones family): the mapping from
#: element to density INVERTS there, so a fire zone with void qi is not a weak
#: fire zone, it is a place the fire was burned out of.
SCARRED_DENSITY_TIERS: frozenset = frozenset({"void", "thin"})

# === Danger tiers =========================================================
# What an encounter in this zone is worth and how likely it is to be a wall.
#
# ``encounter_difficulty`` multiplies the monster's stats; ``reward`` is the
# harvest/encounter payout scalar. They rise together, never one without the
# other: a tier that raised difficulty without raising reward would be a
# punishment, and one that raised reward without difficulty would be the
# faucet this design is told to avoid.
DANGER_TIERS: Dict[int, Dict[str, float]] = {
    0: {"encounter_difficulty": 0.50, "reward": 0.50, "label": "Sanctuary"},
    1: {"encounter_difficulty": 1.00, "reward": 1.00, "label": "Low"},
    2: {"encounter_difficulty": 1.80, "reward": 1.60, "label": "Guarded"},
    3: {"encounter_difficulty": 3.00, "reward": 2.40, "label": "Perilous"},
    4: {"encounter_difficulty": 5.00, "reward": 3.60, "label": "Deadly"},
    5: {"encounter_difficulty": 8.00, "reward": 5.00, "label": "Transcendent"},
}

#: Tier given to a zone with no declared danger. 1, matching the shipped tower
#: floor-1 baseline, so an undeclared zone is a normal place and not a gift.
DEFAULT_DANGER_TIER: int = 1

#: Hard ceiling on the danger tier. The table tops out at 5; a data file
#: claiming 9 must fail loudly at load rather than silently inventing a
#: multiplier no other table knows about.
MAX_DANGER_TIER: int = 5

# === Harvest ==============================================================
#: Seconds before the same node can be gathered again. Long enough that
#: "come back tomorrow" is the natural cadence, short enough that a player who
#: hops between two regions twice in an hour is not wasting their session.
HARVEST_COOLDOWN_SECONDS: int = 900

#: A harvest pays this much cultivation progress, before the zone's danger and
#: affinity scalars. Small on purpose: harvest is a reason to VISIT a zone,
#: not a second cultivation rate. If this number rivaled
#: ``cultivation_config.stage_rate_per_hour`` the best zone would simply be
#: the one with the best harvest, and the map would collapse to one answer.
HARVEST_BASE_PROGRESS: float = 0.10

#: Hard ceiling on one harvest's progress, applied AFTER every scalar.
#:
#: This exists because the scalar stack is multiplicative and a
#: measurement proved the problem real. Measured against a live server: a
#: wood root gathering Spirit Herb in the Spirit Forest -- danger tier 1,
#: affinity 1.35 -- was paid 0.8 x 1.00 x 1.35 = 1.08 progress. A mortal in
#: the early stage earns 2.0 progress per HOUR
#: (``cultivation_config.stage_rate_per_hour``), so that single gather was
#: worth 32 minutes of standing still, and the node comes back every 15. A
#: player who looped the cooldown would out-earn idle cultivation roughly
#: 2.3x, on the safest region on the map.
#:
#: The cap is the honest form of the rule: travelling and gathering may be
#: worth a real slice of a stage, never a stage. It is a constant rather than
#: a derived number because the derived form depends on the current
#: cultivation table, and a cap that silently changes meaning when someone
#: retunes a rate is worse than a number that says what it says.
HARVEST_MAX_PROGRESS: float = 0.5

#: A harvest pays this much of the zone's own element as ``qi_essence``, a
#: per-zone material. It is NOT a currency and has no wallet route: it exists
#: so each region visibly produces its own thing, which is what the map screen
#: shows under "what grows here".
HARVEST_BASE_ESSENCE: int = 1

#: Upper bound on essence held per element. A cap, so a player who farms one
#: region for a week cannot bank an unbounded stock and call it wealth.
MAX_ESSENCE_PER_ELEMENT: int = 500

# === Travel ===============================================================
#: Seconds of travel for a one-hop move inside ambient ground, before distance
#: and difficulty scaling. A hop is not instant: arriving must be an event, or
#: the map is a menu.
TRAVEL_BASE_SECONDS: int = 45

#: Seconds added per unit of map distance between the two zones. The map
#: coordinates in the data file are on a 0.0-1.0 grid, so a full-corner
#: crossing is roughly 1.0 units.
TRAVEL_SECONDS_PER_DISTANCE: int = 90

#: Travel-time scalar by danger tier of the DESTINATION. Climbing into a
#: Deadly region is slow. Time is the cost that scales with danger, because
#: time is the one cost an idle player cannot buy their way out of, and a
#: player who is always on the road is not cultivating.
TRAVEL_DANGER_TIME_SCALAR: Dict[int, float] = {
    0: 0.60,
    1: 1.00,
    2: 1.25,
    3: 1.60,
    4: 2.10,
    5: 2.80,
}

#: Stamina burned per unit of distance, before the qi-density resistance of
#: the region being CROSSED. Stamina is the recurring cost; time is the
#: one-off. Both scale with the road, so a long hop through thin qi is
#: expensive in both currencies and the cheap road really is the cheap road.
TRAVEL_STAMINA_PER_DISTANCE: int = 8

#: Realm mitigation on travel BURN. Canon: realm reduces qi burn, never the
#: speed ceiling — a Nascent Soul crossing the same road as a mortal pays a
#: fraction of the stamina, in the same wall-clock time. Values are the
#: canon resistance modifiers.
REALM_TRAVEL_RESISTANCE: Dict[str, float] = {
    "mortal": 1.00,
    "qi_refining": 0.85,
    "foundation_establishment": 0.70,
    "golden_core": 0.50,
    "nascent_soul": 0.30,
    "spirit_severing": 0.10,
    "dao_merging": 0.05,
    "true_immortal": 0.03,
}

#: Realm with no row in the table above. Falls back to the WORST mitigation
#: (1.00), never the best. Defaulting a missing realm to a discount would
#: hand out free travel to exactly the players the table has not caught up
#: with yet, and the failure would be invisible.
REALM_RESISTANCE_FALLBACK: float = 1.00

#: Ceiling on one journey's travel time, in seconds. Protects the map from a
#: data file that places two zones a whole map apart: without it, one bad
#: coordinate pair would park a player on the road for most of a day.
MAX_TRAVEL_SECONDS: int = 6 * 3600

#: How a player moves. Walking is the default and needs no unlock. Flying
#: needs Nascent Soul (canon: self-flight is a sprint, not a travel method, so
#: it is gated high and it costs more, not less). Teleporting needs a zone
#: with ``teleport_enabled`` and skips the adjacency rule, because that is
#: what a teleport array is for. Each entry is
#: (required_realm, time_scalar, stamina_scalar, needs_adjacency).
TRAVEL_METHODS: Dict[str, Dict[str, object]] = {
    "walk": {
        "required_realm": "mortal",
        "time_scalar": 1.00,
        "stamina_scalar": 1.00,
        "needs_adjacency": True,
    },
    # Self-flight: 0.45x the wall clock and 1.30x the stamina. Faster and
    # dearer, which is the canon trade (a self-flown cultivator arrives
    # combat-ineffective). Gated at Nascent Soul because canon has lower
    # realms walking and taking formation vehicles, not flying.
    "fly": {
        "required_realm": "nascent_soul",
        "time_scalar": 0.45,
        "stamina_scalar": 1.30,
        "needs_adjacency": False,
    },
    # Teleport: 0.20x the wall clock, 2.00x the stamina. The array is
    # expensive to fuel. Gated at Golden Core, and refused outright into a
    # zone whose ``teleport_enabled`` is false.
    "teleport": {
        "required_realm": "golden_core",
        "time_scalar": 0.20,
        "stamina_scalar": 2.00,
        "needs_adjacency": False,
    },
}

#: Method used when a request names one this table does not know. Walking is
#: the safe default and the slowest, so an unknown method is never a shortcut.
DEFAULT_TRAVEL_METHOD: str = "walk"

# === Map presentation =====================================================
#: Grid the data file's ``map_x`` / ``map_y`` live on. Kept here so the server
#: can validate a coordinate and the client can scale it without hard-coding a
#: second number that could drift.
MAP_GRID: float = 1.0

#: A coordinate further from the origin than this is rejected at load. Catches
#: a typo'd ``map_x: 40`` before it becomes a six-hour journey.
#:
#: Deliberately TWICE ``MAP_GRID`` rather than equal to it, and the two are not
#: the same rule. ``MAP_GRID`` is the design grid: the map is authored on a
#: 0.0-1.0 square and every shipped zone sits inside it (measured: the largest
#: is 0.86). ``MAX_MAP_COORD`` is a TYPO GUARD, and it only has to catch a
#: coordinate that was typed wrong by an order of magnitude, which is what
#: ``map_x: 40`` looks like. Tightening it to the grid would reject a legitimate
#: "a bit off the corner" placement in exchange for catching a typo the guard
#: already catches by a factor of twenty.
MAX_MAP_COORD: float = 2.0

# === Strict schema ========================================================
# The declared shape of a region row and of a drop (a harvest node). These are
# the rules the loader enforces, written down once here so the loader and the
# data file cannot disagree about what a field means.
#
# Why a schema at all, when every lookup above answers with a default
# -------------------------------------------------------------------------
# The lookups in this module are TOTAL on purpose: a lookup handed a value off
# a request, a data file or a database row answers with a documented default
# instead of raising. That is the right behaviour for a function called on a
# request path. It is the WRONG behaviour for the one place that decides what
# the world IS, because a default applied while LOADING is not a fallback, it
# is an edit nobody made:
#
#   * a region row with no ``element`` becomes NEUTRAL, which scores 1.0
#     affinity for EVERY player -- the zone reads as a deliberate design choice
#     instead of a typo.
#   * a region row with no ``danger`` becomes tier 1, which quietly makes a
#     Deadly region pay like a Low one.
#   * a region row with no ``map_x`` becomes 0.0, which stacks the zone on the
#     map origin on top of whatever else is already there.
#
# So the defaults stay in the lookups and the schema lives here, and the loader
# (server/zones.py) is the single caller that turns a schema violation into a
# loud ``ZoneDataError``. That split is the whole point: one loud door, and
# every lookup downstream stays total.

#: Fields a region row must carry, with no fallback. Each one is a number the
#: economy reads, so a missing field is not "the world is simpler", it is a
#: value invented at load time. Ordered as the data file orders them.
REQUIRED_ZONE_PARAMETERS: Tuple[str, ...] = (
    "element",
    "qi_density",
    "danger",
    "map_x",
    "map_y",
)

#: Fields a region row may omit, each with the fallback the loader applies.
#:
#: These are kept optional on purpose, and the reasons differ:
#:
#: * ``terrain`` -- ``ELEMENT_NATIVE_TERRAIN`` is a real derived answer, not a
#:   blank. The Desert Wastes are fire and stony, the Foundry Gorge is fire and
#:   metallic, so terrain is a description and cannot be derived from element.
#: * ``reason`` / ``specialty`` -- design prose. A missing one is a typo in a
#:   comment field, which costs nothing at runtime.
#: * ``harvest_nodes`` -- an empty list is a LEGAL region, so the key is
#:   optional. It must still be a list when present: a string here raised
#:   ``AttributeError: 'str' object has no attribute 'get'``, which is a crash
#:   on a load path rather than a refusal.
OPTIONAL_ZONE_PARAMETERS: Tuple[str, ...] = (
    "terrain",
    "reason",
    "specialty",
    "harvest_nodes",
)

#: Fields every drop (harvest node) must carry. ``name`` is in here rather than
#: defaulted to the id because the id is a key the client matches on, not
#: something to show a player; the loader used to fall back to it, which is how
#: a drop reached the map screen labelled ``12345``.
REQUIRED_DROP_FIELDS: Tuple[str, ...] = (
    "id",
    "name",
    "element",
)

#: Fields a drop may omit. ``note`` is flavour text.
OPTIONAL_DROP_FIELDS: Tuple[str, ...] = ("note",)


class ZoneSchemaError(ValueError):
    """A region row, a drop, or this module's own tables break a declared rule.

    A subclass of ``ValueError`` so a caller that already guards against bad
    data values catches it without importing anything new.

    Defined here, next to the schema it enforces, rather than beside the
    loader's own ``ZoneDataError``. The loader converts: a violation of this
    module's schema arrives as a ``ZoneDataError`` at the boundary, so the
    caller that boots the world keeps catching one exception type, and this
    module stays free of any dependency on the loader.
    """


def verify_config_tables() -> None:
    """Refuse to import this module if its own tables contradict each other.

    Runs at import, so a config pack that disagrees with itself stops the
    server at startup instead of answering requests from a number no other
    table agrees with. Everything here is a RELATION between two tables or a
    bound within one table -- no value is pinned, so retuning a number does
    not break this, only breaking the relationship does.

    This is deliberately self-contained: it touches nothing on disk and
    imports nothing, because the module's contract is that it is a pure data
    table (the contract test asserts no module object is bound in this
    namespace, and reading a data file here would break that promise and drag
    a filesystem into every test that touches a balance number).
    """
    # The ladder and the table that scales travel time by tier must have the
    # same rows. A danger tier with no travel scalar silently prices at the
    # caller's fallback, so a new tier is quietly as cheap as an old one.
    ladder = set(DANGER_TIERS)
    travel_rows = set(TRAVEL_DANGER_TIME_SCALAR)
    if ladder != travel_rows:
        raise ZoneSchemaError(
            f"DANGER_TIERS has tiers {sorted(ladder)} but "
            f"TRAVEL_DANGER_TIME_SCALAR has {sorted(travel_rows)}. A tier with "
            "no travel scalar silently falls back to the ordinary zone, so "
            "adding a tier without its travel cost makes the most dangerous "
            "ground the cheapest to cross."
        )
    if ladder != set(range(MAX_DANGER_TIER + 1)):
        raise ZoneSchemaError(
            f"DANGER_TIERS is keyed {sorted(ladder)}, which is not the "
            f"ladder 0..{MAX_DANGER_TIER}. A hole in the ladder means "
            "danger_stats clamps a tier that was supposed to exist."
        )

    # A zone's index in QI_DENSITY_TIERS IS its cultivation value, so the
    # indices have to be a dense range from zero. A duplicate index makes two
    # tiers read as the same place on the ladder.
    indices = sorted(int(tier["index"]) for tier in QI_DENSITY_TIERS.values())
    if indices != list(range(len(QI_DENSITY_TIERS))):
        raise ZoneSchemaError(
            f"QI_DENSITY_TIERS indices are {indices}, which is not a dense "
            f"range 0..{len(QI_DENSITY_TIERS) - 1}. The index is the "
            "cultivation value, so a gap or a duplicate mis-ranks the world."
        )
    # Density is declared thinnest to richest ("void" at 0, "sacred" at 5),
    # and the comments say so, but only the SET of indices was being checked,
    # never the ORDER. Deleting a middle tier leaves a dense index set behind,
    # so the check above passes while the ladder now runs rich, sacred, thin,
    # ambient with nothing wrong in the numbers. The declaration order IS the
    # ladder a reader and a retuner both assume.
    declared = [
        name for name, _ in sorted(
            QI_DENSITY_TIERS.items(), key=lambda item: int(item[1]["index"])
        )
    ]
    resistance_by_index = [
        float(QI_DENSITY_TIERS[name]["resistance"]) for name in declared
    ]
    if resistance_by_index != sorted(resistance_by_index, reverse=True):
        raise ZoneSchemaError(
            f"QI_DENSITY_TIERS lists tiers thin-to-rich as {declared} but "
            f"their travel resistances run {resistance_by_index}, which is "
            "not falling from thinnest to richest. The declaration order is "
            "the ladder this file documents, and the resistance is the payoff "
            "for standing on it."
        )
    # Every tier needs the three fields the map screen reads off it. A tier
    # missing "label" raises KeyError inside ZoneRegion.to_dict, on a read.
    incomplete = sorted(
        name for name, tier in QI_DENSITY_TIERS.items()
        if not {"index", "resistance", "label"} <= set(tier)
    )
    if incomplete:
        raise ZoneSchemaError(
            f"QI_DENSITY_TIERS rows missing index/resistance/label: "
            f"{incomplete}. The map screen reads all three per tier."
        )
    # The scarred set must name real tiers, and the richest tier must not be
    # one of them -- scarred ground is the barren end of this ladder, so a
    # rich scarred tier makes the category mean its opposite.
    unknown_scarred = sorted(SCARRED_DENSITY_TIERS - set(QI_DENSITY_TIERS))
    if unknown_scarred:
        raise ZoneSchemaError(
            f"SCARRED_DENSITY_TIERS names tiers that are not in the ladder: "
            f"{unknown_scarred}. is_scarred would never fire for them."
        )
    if declared:
        richest = max(declared, key=lambda n: int(QI_DENSITY_TIERS[n]["index"]))
        if richest in SCARRED_DENSITY_TIERS:
            raise ZoneSchemaError(
                f"the richest tier on the ladder ({richest!r}) is classified "
                "as SCARRED. Scarred ground is the barren end."
            )

    # DEFAULT_QI_DENSITY and DEFAULT_DANGER_TIER are the fallbacks every
    # unknown value lands on. If the default names a row that was deleted,
    # the fallback raises KeyError on a request path instead of answering.
    if DEFAULT_QI_DENSITY not in QI_DENSITY_TIERS:
        raise ZoneSchemaError(
            f"DEFAULT_QI_DENSITY is {DEFAULT_QI_DENSITY!r}, which is not a "
            f"tier in QI_DENSITY_TIERS. Every unknown density falls back to "
            "it, so deleting that row turns a fallback into a crash."
        )
    if DEFAULT_DANGER_TIER not in DANGER_TIERS:
        raise ZoneSchemaError(
            f"DEFAULT_DANGER_TIER is {DEFAULT_DANGER_TIER!r}, which is not a "
            "row in DANGER_TIERS."
        )
    if DEFAULT_TRAVEL_METHOD not in TRAVEL_METHODS:
        raise ZoneSchemaError(
            f"DEFAULT_TRAVEL_METHOD is {DEFAULT_TRAVEL_METHOD!r}, which is "
            "not a row in TRAVEL_METHODS. An unknown method falls back to it, "
            "so deleting that row turns the safe default into a KeyError."
        )
    if NEUTRAL_ELEMENT not in ELEMENTS:
        raise ZoneSchemaError(
            f"NEUTRAL_ELEMENT {NEUTRAL_ELEMENT!r} is not in ELEMENTS, so a "
            "zone declaring it would be refused by the loader's own check."
        )

    # Every element a zone may declare needs a native terrain, because that
    # table is the fallback for a row that omits one.
    no_terrain = sorted(e for e in ELEMENTS if e not in ELEMENT_NATIVE_TERRAIN)
    if no_terrain:
        raise ZoneSchemaError(
            f"elements with no native terrain in ELEMENT_NATIVE_TERRAIN: "
            f"{no_terrain}. A row omitting terrain would raise KeyError while "
            "loading the world."
        )

    # Combat elements are a claimed subset of the world's elements. A zone
    # carrying a combat element that is not a world element can never be
    # matched against the world's own vocabulary.
    not_world = sorted(e for e in COMBAT_ELEMENTS if e not in ELEMENTS)
    if not_world:
        raise ZoneSchemaError(
            f"COMBAT_ELEMENTS names {not_world}, which are not in ELEMENTS. "
            "The world map is wider than combat, never the other way round."
        )

    # A spirit root maps to an element; a dual root's pair must be real
    # elements or the dual bonus is awarded against nothing.
    bad_roots = sorted(
        root for root, element in ROOT_ELEMENT.items() if element not in ELEMENTS
    )
    if bad_roots:
        raise ZoneSchemaError(
            f"ROOT_ELEMENT maps roots to elements not in ELEMENTS: "
            f"{[(r, ROOT_ELEMENT[r]) for r in bad_roots]}. An unmapped root "
            "would match no zone and score as a mismatch everywhere."
        )
    bad_duals = sorted(
        root for root, pair in DUAL_ROOT_ELEMENTS.items()
        if any(element not in ELEMENTS for element in pair)
    )
    if bad_duals:
        raise ZoneSchemaError(
            f"DUAL_ROOT_ELEMENTS carries non-elements for roots: {bad_duals}"
        )
    # A dual root appears in BOTH tables on purpose: ``root_elements`` reads
    # DUAL_ROOT_ELEMENTS first and answers with the pair, while the
    # ROOT_ELEMENT row is the fallback for a caller doing a plain
    # single-element lookup. That fallback is only correct if it names one
    # of the root's own elements -- a dual root defaulting to NEUTRAL would
    # pay a mismatched rate in both regions its pair was meant to reward.
    bad_dual_fallback = sorted(
        root for root, element in ROOT_ELEMENT.items()
        if root in DUAL_ROOT_ELEMENTS
        and element not in DUAL_ROOT_ELEMENTS[root]
    )
    if bad_dual_fallback:
        raise ZoneSchemaError(
            f"dual roots whose ROOT_ELEMENT fallback is not one of their "
            f"own elements: {[(r, ROOT_ELEMENT[r], DUAL_ROOT_ELEMENTS[r]) for r in bad_dual_fallback]}. "
            "A single-element lookup would resolve the dual root to an "
            "element it does not carry, paying a mismatch in the very "
            "regions the dual was meant to reward."
        )

    # A travel method gated at a realm the resistance table cannot rank pays
    # the fallback, so the gate is real but the discount is invisible.
    unranked = sorted(
        {str(row["required_realm"]) for row in TRAVEL_METHODS.values()}
        - set(REALM_TRAVEL_RESISTANCE)
    )
    if unranked:
        raise ZoneSchemaError(
            f"TRAVEL_METHODS gates on realms with no REALM_TRAVEL_RESISTANCE "
            f"row: {unranked}. Reaching the gate would silently pay the "
            "unmitigated fallback instead of the realm's own rate."
        )

    # Travel must never get cheaper as it gets faster. A row that cuts time
    # while also cutting stamina is a strictly better method, which collapses
    # the choice the map is built on.
    for name, row in sorted(TRAVEL_METHODS.items()):
        if float(row["time_scalar"]) < 1.0 and float(row["stamina_scalar"]) < 1.0:
            raise ZoneSchemaError(
                f"travel method {name!r} is both faster "
                f"({row['time_scalar']}) and cheaper ({row['stamina_scalar']}) "
                "than walking. A method that dominates walking outright has "
                "no reason to exist as a choice."
            )
        if float(row["time_scalar"]) <= 0 or float(row["stamina_scalar"]) <= 0:
            raise ZoneSchemaError(
                f"travel method {name!r} has a non-positive scalar "
                f"({row['time_scalar']}, {row['stamina_scalar']}); a zero or "
                "negative cost is free travel or negative stamina."
            )

    # Difficulty and reward rise together. A tier that raised one without the
    # other is either a punishment or the reward faucet this design avoids.
    previous = None
    for tier in sorted(DANGER_TIERS):
        row = DANGER_TIERS[tier]
        if float(row["encounter_difficulty"]) <= 0 or float(row["reward"]) <= 0:
            raise ZoneSchemaError(
                f"danger tier {tier} has a non-positive difficulty or reward: "
                f"{row}. A zero reward tier is a zone with no harvest payout."
            )
        if previous is not None:
            if float(row["encounter_difficulty"]) < previous[0]:
                raise ZoneSchemaError(
                    f"danger tier {tier} is easier than tier {tier - 1} "
                    f"({row['encounter_difficulty']} < {previous[0]}); the "
                    "ladder must rise with the tier number."
                )
            if float(row["reward"]) < previous[1]:
                raise ZoneSchemaError(
                    f"danger tier {tier} pays less than tier {tier - 1} "
                    f"({row['reward']} < {previous[1]}); the most dangerous "
                    "ground must not be the least rewarding."
                )
        previous = (float(row["encounter_difficulty"]), float(row["reward"]))

    # Affinity has to order mismatch < neutral < match, or travel stops being
    # a decision: a matching root must always beat standing nowhere, and a
    # mismatched root must always lose to it.
    if not AFFINITY_MISMATCH < AFFINITY_NEUTRAL < AFFINITY_MATCH:
        raise ZoneSchemaError(
            f"affinity multipliers are out of order "
            f"(mismatch {AFFINITY_MISMATCH}, neutral {AFFINITY_NEUTRAL}, "
            f"match {AFFINITY_MATCH}). They must satisfy "
            "mismatch < neutral < match."
        )

    # The harvest cap is the fix for a multiplicative scalar stack, and it
    # only works if it sits BELOW the most generous stack. A cap at or above
    # the best ground is decorative: it never fires.
    best_ground = (
        HARVEST_BASE_PROGRESS
        * max(float(row["reward"]) for row in DANGER_TIERS.values())
        * max(AFFINITY_MATCH, AFFINITY_NEUTRAL, AFFINITY_MISMATCH)
    )
    if not HARVEST_MAX_PROGRESS < best_ground:
        raise ZoneSchemaError(
            f"HARVEST_MAX_PROGRESS is {HARVEST_MAX_PROGRESS} but the best "
            f"ground pays {best_ground}. A cap at or above the best stack "
            "never fires, which is the bug the cap was added to stop."
        )
    # And above the ordinary gather, or every zone pays the same number and
    # the map has exactly one answer again.
    ordinary = (
        HARVEST_BASE_PROGRESS
        * float(DANGER_TIERS[DEFAULT_DANGER_TIER]["reward"])
        * AFFINITY_NEUTRAL
    )
    if HARVEST_MAX_PROGRESS < ordinary:
        raise ZoneSchemaError(
            f"HARVEST_MAX_PROGRESS is {HARVEST_MAX_PROGRESS} but an ordinary "
            f"gather pays {ordinary}. Every zone would pay the cap and the "
            "danger tier would stop mattering."
        )

    if HARVEST_COOLDOWN_SECONDS <= 0:
        raise ZoneSchemaError(
            "HARVEST_COOLDOWN_SECONDS must be positive; zero would make every "
            "node gatherable every frame."
        )
    if MAX_ESSENCE_PER_ELEMENT <= 0 or HARVEST_BASE_ESSENCE <= 0:
        raise ZoneSchemaError(
            f"essence bounds must be positive, got MAX_ESSENCE_PER_ELEMENT="
            f"{MAX_ESSENCE_PER_ELEMENT}, HARVEST_BASE_ESSENCE="
            f"{HARVEST_BASE_ESSENCE}."
        )

    # The map grid is the authored square; the coord guard is a typo trap that
    # is deliberately wider. If the guard ever fell below the grid, a zone on
    # the grid's own edge would be unplaceable.
    if not 0.0 < MAP_GRID <= MAX_MAP_COORD:
        raise ZoneSchemaError(
            f"MAP_GRID is {MAP_GRID} and MAX_MAP_COORD is {MAX_MAP_COORD}; the "
            "map grid must be positive and must fit inside the coord guard, or "
            "a zone on the grid's own edge cannot be placed."
        )

    if TRAVEL_BASE_SECONDS <= 0 or TRAVEL_SECONDS_PER_DISTANCE < 0 \
            or TRAVEL_STAMINA_PER_DISTANCE < 0 or MAX_TRAVEL_SECONDS <= 0:
        raise ZoneSchemaError(
            "travel constants must be positive (per-distance costs may be "
            f"zero but not negative): TRAVEL_BASE_SECONDS={TRAVEL_BASE_SECONDS}, "
            f"TRAVEL_SECONDS_PER_DISTANCE={TRAVEL_SECONDS_PER_DISTANCE}, "
            f"TRAVEL_STAMINA_PER_DISTANCE={TRAVEL_STAMINA_PER_DISTANCE}, "
            f"MAX_TRAVEL_SECONDS={MAX_TRAVEL_SECONDS}."
        )


def verify_region_table(rows: object) -> None:
    """Refuse a whole regions table: every row, and every drop, in one pass.

    This is the door the loader opens. It carries one shared id registry
    across all regions, which is the only place a cross-region drop-id
    collision can be seen -- a per-region call would accept the same id in two
    regions and the collision would only appear at gather time, as one
    region's node reporting itself as regrowing after the player visited
    another.

    Raises ``ZoneSchemaError`` naming the zone and field that failed.
    """
    if not isinstance(rows, dict):
        raise ZoneSchemaError(
            f"the regions table is {type(rows).__name__}, not a mapping of "
            "zone id to region fields."
        )
    seen_ids: Dict[str, str] = {}
    for zone_id, row in rows.items():
        validate_zone_parameters(row, str(zone_id))
        nodes = row.get("harvest_nodes") if isinstance(row, dict) else None
        validate_drops(nodes, str(zone_id), seen_ids)



def root_elements(spirit_root: str) -> Tuple[str, ...]:
    """Every element this spirit root carries affinity with.

    A single-element root returns one name; a dual root returns both, so a
    fire/metal cultivator is rewarded in the Foundry AND the Deep Cavern. An
    unknown root returns the neutral element — the same answer as having no
    root, and deliberately not a free pass to some other element's bonus.

    Carrying the neutral element is not a blank cheque. Neutral scores 1.0
    only in a NEUTRAL_ELEMENT zone, where ``affinity_multiplier`` returns
    AFFINITY_NEUTRAL for everybody. On any elemental ground the root matches
    nothing, so an unknown root pays AFFINITY_MISMATCH (0.75) exactly as a
    fire cultivator does in a wood forest. The mapping exists to stop an
    unmapped root inheriting some element's 1.35 bonus, not to hand out a
    neutral bonus of its own.
    """
    if spirit_root in DUAL_ROOT_ELEMENTS:
        return DUAL_ROOT_ELEMENTS[spirit_root]
    return (ROOT_ELEMENT.get(spirit_root, NEUTRAL_ELEMENT),)


def affinity_multiplier(zone_element: str, spirit_root: str) -> float:
    """Cultivation rate scalar for standing in this zone with this root.

    Match is AFFINITY_MATCH, mismatch is AFFINITY_MISMATCH, and a neutral zone
    is exactly 1.0 for everybody. A dual root that matches EITHER of its
    elements gets the match bonus: canon treats a dual root as genuinely
    carrying both, and picking one arbitrarily would make half of the
    dual-root player's good regions read as mismatches.
    """
    if zone_element == NEUTRAL_ELEMENT:
        return AFFINITY_NEUTRAL
    if zone_element in root_elements(spirit_root):
        return AFFINITY_MATCH
    return AFFINITY_MISMATCH


def qi_density_index(density: str) -> int:
    """Ordinal for a density tier. Unknown tiers read as DEFAULT, never 0.

    Index 0 is the Void tier, so a silent fallback to 0 would make a typo'd
    density look like the most barren ground in the world.
    """
    return int(QI_DENSITY_TIERS.get(density, QI_DENSITY_TIERS[DEFAULT_QI_DENSITY])["index"])


def qi_resistance(density: str) -> float:
    """Travel-burn multiplier for a density tier. Unknown tiers read as 1.0."""
    return float(QI_DENSITY_TIERS.get(density, QI_DENSITY_TIERS[DEFAULT_QI_DENSITY])["resistance"])


def is_scarred(density: str) -> bool:
    """True when the zone sits in void or thin qi — canon scarred ground."""
    return density in SCARRED_DENSITY_TIERS


def danger_stats(tier: int) -> Dict[str, float]:
    """Encounter-difficulty and reward scalars for a danger tier.

    An out-of-range tier is CLAMPED to the nearest real row rather than
    rejected here, so one bad number in the data file cannot take the whole
    map down. ``load_region_table`` is where the loud failure belongs, and it
    checks the same range.
    """
    clamped = max(0, min(int(tier), MAX_DANGER_TIER))
    return DANGER_TIERS[clamped]


def realm_resistance(realm: str) -> float:
    """Qi-burn mitigation for a realm. Unknown realms pay the full price."""
    return REALM_TRAVEL_RESISTANCE.get(realm, REALM_RESISTANCE_FALLBACK)


def travel_method(name: str) -> Dict[str, object]:
    """The method row, or the walking default for an unknown name."""
    return TRAVEL_METHODS.get(name, TRAVEL_METHODS[DEFAULT_TRAVEL_METHOD])


# === Schema verification ===================================================
# Pure functions that answer "is this row legal?" and, when it is not, say
# exactly which field is wrong and why it matters. They raise, and they are
# called from the loader, so a bad world table stops the server at startup
# rather than answering requests from invented values.
#
# They take plain dicts and return nothing. That keeps them testable without a
# ZoneManager, and it keeps this module free of the loader's error type: the
# loader owns ``ZoneDataError``, so it wraps the call and re-raises. These
# raise ``ZoneSchemaError``, which the loader converts.

def _require_present(row: Dict[str, object], field: str, where: str) -> object:
    """Return ``row[field]``, refusing a missing or null field.

    Null is refused alongside missing because they are the same defect wearing
    different clothes: a JSON ``null`` reaches the loader as the key being
    ABSENT for every purpose that matters here, and defaulting it is the very
    silent edit the schema exists to prevent.
    """
    if field not in row:
        raise ZoneSchemaError(
            f"{where} is missing the required field {field!r}. This module "
            "declares it required, so the loader has no fallback to apply and "
            "will not invent a value for it."
        )
    value = row[field]
    if value is None:
        raise ZoneSchemaError(
            f"{where} declares {field!r} as null. A null is treated exactly "
            f"like a missing key, which is why it is refused rather than "
            "defaulted."
        )
    return value


def validate_zone_parameters(row: object, zone_id: str) -> None:
    """Refuse a region row that does not match the declared schema.

    Checks, in order: the row is a dict; every field in
    ``REQUIRED_ZONE_PARAMETERS`` is present and non-null; ``element`` and
    ``qi_density`` name real rows; ``danger`` is an integer on the ladder and
    is not a bool; both coordinates are finite real numbers inside
    ``MAX_MAP_COORD``; and ``harvest_nodes``, when present, is a list.

    The finite check is the one that matters most and is the one a bounds
    comparison cannot do. ``abs(nan) > 2.0`` is False, because every
    comparison against nan is False, so a nan coordinate walks straight
    through the guard and detonates much later at the first
    ``int(round(...))`` in a travel quote with a ``ValueError`` -- an HTTP 500
    caused by one token in a data file, on a path that has nothing to do with
    the table. ``json.loads`` accepts bare ``NaN`` and ``Infinity``, so this is
    reachable from the shipped load path and not only from a hand-built file.
    """
    where = f"zone {zone_id!r}"
    if not isinstance(row, dict):
        raise ZoneSchemaError(
            f"{where} is not a region object but {type(row).__name__}. The "
            "regions table maps a zone id to an object of region fields."
        )

    for field in REQUIRED_ZONE_PARAMETERS:
        _require_present(row, field, where)

    element = row["element"]
    if element not in ELEMENTS:
        raise ZoneSchemaError(
            f"{where} declares element {element!r}, which is not one of the "
            f"{len(ELEMENTS)} elements in server/zone_config.ELEMENTS. An "
            "element nothing recognises matches no spirit root, so the zone "
            "reads as a mismatch for every player in the game."
        )

    density = row["qi_density"]
    if density not in QI_DENSITY_TIERS:
        raise ZoneSchemaError(
            f"{where} declares qi_density {density!r}, which is not a tier. "
            f"Valid tiers: {sorted(QI_DENSITY_TIERS)}"
        )

    danger = row["danger"]
    # bool before int: bool IS an int subclass, and True would otherwise pass
    # as tier 1 -- the one value the ladder treats as "an ordinary zone".
    if isinstance(danger, bool) or not isinstance(danger, int):
        raise ZoneSchemaError(
            f"{where} declares danger {danger!r}, which is not an integer "
            f"0..{MAX_DANGER_TIER}. A tier decides both the encounter "
            "difficulty and the reward, so it may not arrive as a string, a "
            "float or a boolean."
        )
    if not 0 <= danger <= MAX_DANGER_TIER:
        raise ZoneSchemaError(
            f"{where} declares danger {danger}, which is outside the ladder "
            f"0..{MAX_DANGER_TIER}. A tier past the top would be a multiplier "
            "no other table knows about."
        )

    for field in ("map_x", "map_y"):
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ZoneSchemaError(
                f"{where} declares {field} {value!r}, which is not a number. "
                "A coordinate is read as a float and must arrive as one."
            )
        if value != value or value in (float("inf"), float("-inf")):
            raise ZoneSchemaError(
                f"{where} declares {field} {value!r}, which is not a finite "
                "number. The world tables are read with json.loads, which "
                "accepts bare NaN and Infinity tokens, and every comparison "
                "against nan is False -- so it would pass the map-grid bounds "
                "check and then raise ValueError when something rounds it to "
                "an integer, which is a 500 rather than a refusal."
            )
        if abs(value) > MAX_MAP_COORD:
            raise ZoneSchemaError(
                f"{where} sits at ({row['map_x']}, {row['map_y']}), off the "
                f"{MAX_MAP_COORD}-unit map grid. A typo'd coordinate becomes a "
                "multi-hour journey."
            )

    if "harvest_nodes" in row:
        nodes = row["harvest_nodes"]
        if nodes is not None and not isinstance(nodes, list):
            raise ZoneSchemaError(
                f"{where} declares harvest_nodes as {type(nodes).__name__}, "
                "not a list. An empty list is a legal region with nothing to "
                "gather; a string here is a typo that used to raise "
                "AttributeError on a load path."
            )


def validate_drops(nodes: object, zone_id: str,
                   seen_ids: Optional[Dict[str, str]] = None) -> None:
    """Refuse drops that are malformed, or an id used twice on the map.

    A drop is one gatherable thing: the material a player gets for standing in
    a region. Three ways it was wrong, all silent, all now refused:

    1. An id that is missing, empty, or not a string. The id is the cooldown
       KEY -- ``zone_harvest_at`` is a flat ``{node_id: epoch}`` map on the
       player, not per region -- so an empty id is a real, functioning,
       gatherable slot that every other empty id on the map shares.
    2. An id used twice. Because the cooldown map is keyed on the id ALONE and
       not on (region, id), two regions sharing an id share one timer:
       gathering in the first region makes the second region's node report
       "regrows in 900s" for a node the player never touched. Measured, not
       guessed.
    3. A ``name`` that is missing or not a string, which used to fall back to
       the id -- so a drop reached the map screen labelled ``12345``.

    ``seen_ids`` maps an id to the zone that claimed it, and is carried across
    calls by the loader. Passing a fresh dict verifies one region's drops in
    isolation, which is what a unit test wants.
    """
    if nodes is None:
        return
    if not isinstance(nodes, list):
        raise ZoneSchemaError(
            f"zone {zone_id!r} declares harvest_nodes as {type(nodes).__name__}, "
            "not a list."
        )

    claimed = seen_ids if seen_ids is not None else {}
    for index, node in enumerate(nodes):
        where = f"harvest node {index} in zone {zone_id!r}"
        if not isinstance(node, dict):
            raise ZoneSchemaError(
                f"{where} is {type(node).__name__}, not an object of drop "
                "fields."
            )
        for field in REQUIRED_DROP_FIELDS:
            _require_present(node, field, where)

        node_id = node["id"]
        if not isinstance(node_id, str):
            raise ZoneSchemaError(
                f"{where} declares id {node_id!r}, which is "
                f"{type(node_id).__name__}, not a string. The id is the "
                "cooldown key and the handle the client requests the drop by."
            )
        if not node_id.strip():
            raise ZoneSchemaError(
                f"{where} declares an empty id. The cooldown is keyed on the "
                "id alone, so every empty id on the map would share one timer."
            )
        owner = claimed.get(node_id)
        if owner is not None:
            raise ZoneSchemaError(
                f"harvest node id {node_id!r} is claimed by both zone "
                f"{owner!r} and zone {zone_id!r}. A node's cooldown is keyed "
                "on the id alone, so two regions sharing an id share one "
                "timer: gathering in the first region makes the second "
                "region's node report itself as regrowing."
            )
        claimed[node_id] = zone_id

        name = node["name"]
        if not isinstance(name, str):
            raise ZoneSchemaError(
                f"{where} declares name {name!r}, which is "
                f"{type(name).__name__}, not a string. The name is what the "
                "map screen shows; falling back to the id put a raw key in "
                "front of a player."
            )

        element = node["element"]
        if element not in ELEMENTS:
            raise ZoneSchemaError(
                f"{where} declares element {element!r}, which is not a known "
                f"element. Valid elements: {sorted(ELEMENTS)}"
            )

        if "note" in node and node["note"] is not None \
                and not isinstance(node["note"], str):
            raise ZoneSchemaError(
                f"{where} declares note {node['note']!r}, which is "
                f"{type(node['note']).__name__}, not a string. The note is "
                "flavour text."
            )


# The self-check runs LAST, after every table and every function above has been
# bound. Called any earlier it would either raise NameError on a name that does
# not exist yet or, worse, quietly pass because it was checking half a module.
verify_config_tables()
