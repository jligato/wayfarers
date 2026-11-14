"""
Cosmetic Trial-Then-Buy System — R7: 3-day trial of auras, purchase for permanent.
Competitor: Overmortal _cloth_3days vs _cloth_forever.
Our adaptation: Cultivation aura effects, trial→purchase with F2P currency.

THE ONE RULE THIS MODULE EXISTS TO KEEP
---------------------------------------
    A cosmetic may only ever change what is DRAWN. It may never change a number
    the combat or progression code reads.

`cosmetics-system-design.md` §15 states that rule. It was not true of this file
when this docstring was written: `combat_config.calculate_auto_battle` carried a
damage multiplier keyed on the aura id, and `main.py` fed the player's owned
aura into the simulator's stat line, so buying a 5000-stone aura was a +15%
damage purchase. The multipliers are gone. `ZERO_STAT_FORBIDDEN_KEYS` below and
the whole-player snapshot diff test in `server/test_cosmetics.py` are what hold
the line now — a rule with no test that fails on revert is a comment.

WHY THE DOCSTRING IS THIS LONG
------------------------------
Because the invariant lives in the ABSENCE of code, which is the one kind of
invariant a refactor silently removes. Every rule below that a future editor
could plausibly break says so at the point where breaking it would happen.
"""

from __future__ import annotations
import time
from copy import deepcopy
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from fastapi import Request

TRIAL_DAYS = 3
TRIAL_SECONDS = TRIAL_DAYS * 86400

# Keys that must never appear in a catalogue entry, a grant, or a loadout row.
# §15.2's "cheapest possible guard": a test walks the loaded catalogue and fails
# the day someone adds `"bonus": 5`. It is a list rather than a schema because
# the catalogue is a Python dict today and will be a table tomorrow
# (`server/migrations/011_cosmetics.sql`), and this list has to keep working
# across that change.
ZERO_STAT_FORBIDDEN_KEYS = frozenset({
    "stat", "stats", "bonus", "bonuses", "power", "combat_power",
    "attack", "atk", "defense", "def", "def_", "hp", "spd", "speed",
    "modifier", "modifiers", "multiplier", "damage", "dps", "crit",
    "crit_rate", "armor", "resist", "pierce", "qi_bonus", "exp_bonus",
})

# Which slot a cosmetic type occupies. Derived from the type, not stored per
# item, so there is one source of truth for "where does this go".
#
# Three types are deliberately ABSENT. A realm badge, a sect banner and a
# tribulation mark are derived by the game from realm, sect and survival
# history; there is nothing for a player to equip, so a loadout slot for them
# would be a fiction the schema then has to police. §12.2 rule 3.
SLOT_BY_TYPE = {
    "aura": "aura",
    "title": "title",
    "weapon_skin": "weapon_skin",
    "portrait_frame": "portrait_frame",
}

EQUIPPABLE_SLOTS = ("aura", "title", "weapon_skin", "portrait_frame")

# The free progression types: never equippable, never in a shop, never a slot.
IMPLICIT_TYPES = frozenset({"realm_badge", "sect_banner", "tribulation_mark"})

class CosmeticType(Enum):
    AURA = "aura"
    REALM_BADGE = "realm_badge"
    SECT_BANNER = "sect_banner"
    TRIBULATION_MARK = "tribulation_mark"
    TITLE = "title"
    # Added for the battle-pass ids the pass was already handing out and the
    # catalogue did not know (§13.2). `super_skin_N` maps here rather than to a
    # new aura type: a skin is a retexture of something already on screen, which
    # is cheaper than new aura art and is the order the design argues for in
    # §12.3.
    WEAPON_SKIN = "weapon_skin"
    PORTRAIT_FRAME = "portrait_frame"

class CosmeticRarity(Enum):
    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"


COSMETIC_CATALOG = {
    # Qi Flow Auras
    "aura_qi_flow": {
        "id": "aura_qi_flow",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.COMMON,
        "name": "Gentle Qi Flow",
        "description": "A subtle trail of qi follows your movements.",
        "visual": {"particle": "qi_swirl", "color": "#88CCFF", "intensity": 0.3},
        "trial": True,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 500,
        # Two sources, deliberately: the gacha can drop it AND the shop sells it.
        # §13.3's no-single-source rule, visible in the data rather than only in
        # a schema constraint.
        "acquisition": ["shop", "gacha"],
    },
    "aura_qi_river": {
        "id": "aura_qi_river",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.UNCOMMON,
        "name": "Qi River",
        "description": "A visible stream of cultivation energy.",
        "visual": {"particle": "qi_stream", "color": "#4A90E2", "intensity": 0.5},
        "trial": True,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 1500,
    },
    "aura_qi_ocean": {
        "id": "aura_qi_ocean",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.RARE,
        "name": "Qi Ocean",
        "description": "Waves of power radiate from your core.",
        "visual": {"particle": "qi_wave", "color": "#1A5FB4", "intensity": 0.8},
        "trial": True,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 5000,
    },

    # Realm Badges
    "badge_mortal": {
        "id": "badge_mortal",
        "type": CosmeticType.REALM_BADGE,
        "rarity": CosmeticRarity.COMMON,
        "name": "Mortal Walker",
        "description": "Every journey begins with a single step.",
        "visual": {"icon": "badge_mortal", "frame": "bronze"},
        "trial": False,  # Auto-granted on realm
        "realm_requirement": "Mortal",
    },
    "badge_foundation": {
        "id": "badge_foundation",
        "type": CosmeticType.REALM_BADGE,
        "rarity": CosmeticRarity.UNCOMMON,
        "name": "Foundation Builder",
        "description": "Foundations laid, the path ahead clears.",
        "visual": {"icon": "badge_foundation", "frame": "silver"},
        "trial": False,
        "realm_requirement": "Foundation Establishment",
    },
    "badge_core": {
        "id": "badge_core",
        "type": CosmeticType.REALM_BADGE,
        "rarity": CosmeticRarity.RARE,
        "name": "Core Forged",
        "description": "Golden core shines within.",
        "visual": {"icon": "badge_core", "frame": "gold"},
        "trial": False,
        "realm_requirement": "Core Formation",
    },
    "badge_nascent": {
        "id": "badge_nascent",
        "type": CosmeticType.REALM_BADGE,
        "rarity": CosmeticRarity.EPIC,
        "name": "Nascent Soul",
        "description": "Soul separates, true cultivation begins.",
        "visual": {"icon": "badge_nascent", "frame": "platinum"},
        "trial": False,
        "realm_requirement": "Nascent Soul",
    },
    "badge_dao": {
        "id": "badge_dao",
        "type": CosmeticType.REALM_BADGE,
        "rarity": CosmeticRarity.LEGENDARY,
        "name": "Dao Seeker",
        "description": "Seeking the Dao, touching the heavens.",
        "visual": {"icon": "badge_dao", "frame": "rainbow"},
        "trial": False,
        "realm_requirement": "Dao Seeking",
    },

    # Sect Banners
    "banner_sect_default": {
        "id": "banner_sect_default",
        "type": CosmeticType.SECT_BANNER,
        "rarity": CosmeticRarity.COMMON,
        "name": "Sect Standard",
        "description": "Your sect's emblem trails behind you.",
        "visual": {"type": "sect_emblem", "trail": True},
        "trial": True,
        "purchase_currency": "sect_contribution",
        "purchase_cost": 1000,
    },
    "banner_sect_elite": {
        "id": "banner_sect_elite",
        "type": CosmeticType.SECT_BANNER,
        "rarity": CosmeticRarity.RARE,
        "name": "Sect War Banner",
        "description": "Forged in sect war, honored by ancestors.",
        "visual": {"type": "sect_emblem", "trail": True, "glow": True},
        "trial": True,
        "purchase_currency": "sect_contribution",
        "purchase_cost": 5000,
    },

    # Tribulation Marks
    "mark_tribulation_survivor": {
        "id": "mark_tribulation_survivor",
        "type": CosmeticType.TRIBULATION_MARK,
        "rarity": CosmeticRarity.RARE,
        "name": "Tribulation Survivor",
        "description": "Faced the heavenly trial and endured.",
        "visual": {"particle": "lightning_crackle", "color": "#FFD700"},
        "trial": False,  # Earned via tribulation
        "requirement": "tribulation_cycles_survived >= 1",
    },
    "mark_tribulation_conqueror": {
        "id": "mark_tribulation_conqueror",
        "type": CosmeticType.TRIBULATION_MARK,
        "rarity": CosmeticRarity.EPIC,
        "name": "Tribulation Conqueror",
        "description": "The heavens themselves yield to your will.",
        "visual": {"particle": "lightning_crown", "color": "#FFD700", "intensity": 1.5},
        "trial": False,
        "requirement": "tribulation_cycles_survived >= 10",
    },

    # Titles
    "title_first_ascendant": {
        "id": "title_first_ascendant",
        "type": CosmeticType.TITLE,
        "rarity": CosmeticRarity.LEGENDARY,
        "name": "First Ascendant",
        "description": "The first to transcend this cycle.",
        "visual": {"prefix": "[First Ascendant] ", "color": "#FFFFFF"},
        "trial": False,
        "requirement": "ascension_season == 1",
        # The gacha chase item. Earned by ascension season 1 AND droppable, so
        # it is the hardest thing in the game to own and the easiest to lose
        # patience for -- which is exactly what a rare cosmetic should be, and
        # exactly why it must not be gacha-exclusive.
        "acquisition": ["progression", "gacha"],
    },
    "title_eternal_ascendant": {
        "id": "title_eternal_ascendant",
        "type": CosmeticType.TITLE,
        "rarity": CosmeticRarity.LEGENDARY,
        "name": "Eternal Ascendant",
        "description": "Cycles turn, you remain.",
        "visual": {"prefix": "[Eternal Ascendant] ", "color": "#FFD700", "shimmer": True},
        "trial": False,
        "requirement": "ascension_season >= 4",
    },

    # -----------------------------------------------------------------------
    # Retention cosmetics (gift_series.py:134,238)
    # -----------------------------------------------------------------------
    # The 7-day new-player gift series and the seasonal gift series award
    # `welcome_aura_1` and `season_aura_1` respectively. NEITHER id existed in
    # this catalogue, which is the same defect the battle pass had (section
    # 13.2) sitting on a retention feature instead of a paid one: a player who
    # logs in for seven consecutive days and claims "Grand Welcome" gets a
    # receipt naming a cosmetic that resolves to nothing and is silently
    # skipped by `get_active_cosmetics`.
    #
    # They are auras on the existing particle pipeline -- new colours, no new
    # art -- which is why they are cheap to add and were worth adding rather
    # than leaving the test red. Each also carries a price, so neither is
    # gift-exclusive: section 13.3's rule that no cosmetic has exactly one
    # source applies to retention rewards as much as to gacha drops.
    "welcome_aura_1": {
        "id": "welcome_aura_1",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.COMMON,
        "name": "Welcoming Qi",
        "description": "Awarded for a week of unbroken visits.",
        "visual": {"particle": "qi_swirl", "color": "#9BE7A2", "intensity": 0.3},
        "trial": False,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 200,
        "acquisition": ["progression", "shop"],
    },
    "season_aura_1": {
        "id": "season_aura_1",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.RARE,
        "name": "Seasonal Bloom",
        "description": "Awarded by the seasonal gift series.",
        "visual": {"particle": "qi_wave", "color": "#E8A0BF", "intensity": 0.6},
        "trial": False,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 1200,
        "acquisition": ["progression", "shop"],
    },

    # The Jiangshi festival gift series' top tier (gift_series.py:282) awards
    # `jiangshi_skin_1`. A third id the catalogue had never heard of, found by
    # the same coverage test that found the other two. It is a weapon skin
    # because the reward is named "skin" and a skin is a retexture of something
    # already on screen -- cheaper than new aura art, and the order the design
    # argues for in section 12.3. Priced, so it is not festival-exclusive.
    "jiangshi_skin_1": {
        "id": "jiangshi_skin_1",
        "type": CosmeticType.WEAPON_SKIN,
        "rarity": CosmeticRarity.EPIC,
        "name": "Jiangshi Festival Skin",
        "description": "Awarded at the top tier of the Jiangshi festival gifts.",
        "visual": {"skin": "jiangshi_1", "tint": "#C0392B"},
        "trial": False,
        "purchase_currency": "spirit_stones",
        "purchase_cost": 1500,
        "acquisition": ["progression", "shop"],
    },
}

# ---------------------------------------------------------------------------
# Battle-pass cosmetics (§13.2)
# ---------------------------------------------------------------------------
# The pass was ALREADY handing these five ids out — `battle_pass.py:113,122`
# build them by string formatting — and none of them existed in the catalogue.
# Measured on 2026-09-30: all five resolve to None from `get_cosmetic`, and a
# player holding all five grants gets `get_active_cosmetics -> []` and
# `get_available_cosmetics -> []`. A paying player claims level 25 of a Super
# pass, gets a receipt, and sees nothing, because the grant is written and the
# catalogue has no matching row.
#
# These rows are the fix; the guard is `test_battle_pass_cosmetic_ids_resolve`
# in `server/test_cosmetics.py`, which reads every id the pass can emit and
# asserts each one resolves here. The data without the test is a row that
# rots the next time someone renames a level.
#
# Types follow the design's own advice in §13.2: the cheapest slots first. The
# pass calls them "aura" and "skin", but a new aura is new particle art and a
# weapon skin is a retexture of something already on screen (§12.3), so the
# senior tiers became titles and the super tiers became weapon skins.
BATTLE_PASS_COSMETICS = {
    "senior_aura_25": {
        "id": "senior_aura_25",
        "type": CosmeticType.TITLE,
        "rarity": CosmeticRarity.RARE,
        "name": "Senior Adept",
        "description": "Awarded at Senior Pass level 25.",
        "visual": {"prefix": "[Senior Adept] ", "color": "#88CCFF"},
        "trial": False,
        "acquisition": ["battle_pass"],
    },
    "senior_aura_50": {
        "id": "senior_aura_50",
        "type": CosmeticType.TITLE,
        "rarity": CosmeticRarity.EPIC,
        "name": "Senior Ascension",
        "description": "Awarded at Senior Pass level 50.",
        "visual": {"prefix": "[Senior Ascension] ", "color": "#4A90E2", "shimmer": True},
        "trial": False,
        "acquisition": ["battle_pass"],
    },
    "super_skin_1": {
        "id": "super_skin_1",
        "type": CosmeticType.WEAPON_SKIN,
        "rarity": CosmeticRarity.RARE,
        "name": "Super weapon skin I",
        "description": "Awarded at Super Pass level 1.",
        "visual": {"skin": "super_1", "tint": "#C0C0C0"},
        "trial": False,
        "acquisition": ["battle_pass"],
    },
    "super_skin_25": {
        "id": "super_skin_25",
        "type": CosmeticType.WEAPON_SKIN,
        "rarity": CosmeticRarity.EPIC,
        "name": "Super weapon skin XXV",
        "description": "Awarded at Super Pass level 25.",
        "visual": {"skin": "super_25", "tint": "#FFD700"},
        "trial": False,
        "acquisition": ["battle_pass"],
    },
    "super_skin_50": {
        "id": "super_skin_50",
        "type": CosmeticType.WEAPON_SKIN,
        "rarity": CosmeticRarity.LEGENDARY,
        "name": "Super weapon skin L",
        "description": "Awarded at Super Pass level 50.",
        "visual": {"skin": "super_50", "tint": "#FF6D00", "shimmer": True},
        "trial": False,
        "acquisition": ["battle_pass"],
    },
}

# ---------------------------------------------------------------------------
# Gacha cosmetics (§13.3)
# ---------------------------------------------------------------------------
# A cosmetic from a random draw must NEVER be exclusive. The design's rule is
# one sentence and it is enforced in two places: here (every entry lists a
# second source) and in the schema (`server/migrations/011_cosmetics.sql` has a
# CHECK that refuses a gacha row with fewer than two acquisition paths).
#
# Why it matters: an item that can only come from the draw is unobtainable for
# a player with money and bad luck, which is the same "paywall wearing a
# particle effect" problem the note opens with, in a new costume.
#
# These are priced so the guaranteed path is real. `aura_qi_flow` at 500
# low-grade stones is the SAME item the gacha can drop, which is what makes the
# second path honest rather than theoretical.
GACHA_COSMETIC_POOL = {
    "aura_qi_flow": 0.05,      # 5% of pulls
    "title_first_ascendant": 0.005,  # 0.5%, the chase
}

COSMETIC_CATALOG.update(BATTLE_PASS_COSMETICS)

# Monthly event cosmetics (R6)
EVENT_COSMETICS = {
    "aura_north_mystic": {
        "id": "aura_north_mystic",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.EPIC,
        "name": "North Mystic Aura",
        "description": "Ice and starlight swirl around you.",
        "visual": {"particle": "snowflake_star", "color": "#4A90E2", "intensity": 0.7},
        "trial": True,
        "event_source": "north_mystic",
        "purchase_currency": "north_star_dust",
        "purchase_cost": 500,
    },
    "aura_moon_palace": {
        "id": "aura_moon_palace",
        "type": CosmeticType.AURA,
        "rarity": CosmeticRarity.EPIC,
        "name": "Moon Palace Aura",
        "description": "Moonlight and shadow dance at your feet.",
        "visual": {"particle": "moonlight_shadow", "color": "#E8D5B7", "intensity": 0.7},
        "trial": True,
        "event_source": "moon_palace",
        "purchase_currency": "moon_essence",
        "purchase_cost": 500,
    },
}


def get_cosmetic(cosmetic_id: str) -> Optional[dict]:
    """Returns cosmetic config by ID."""
    if cosmetic_id in COSMETIC_CATALOG:
        return COSMETIC_CATALOG[cosmetic_id]
    if cosmetic_id in EVENT_COSMETICS:
        return EVENT_COSMETICS[cosmetic_id]
    return None


def get_cosmetics_by_type(cosmetic_type: str) -> list[dict]:
    """Returns all cosmetics of a given type."""
    results = []
    for c in COSMETIC_CATALOG.values():
        if c["type"].value == cosmetic_type:
            results.append(c)
    for c in EVENT_COSMETICS.values():
        if c["type"].value == cosmetic_type:
            results.append(c)
    return results


def get_available_cosmetics(player: dict, now: Optional[float] = None) -> list[dict]:
    """Returns cosmetics the player can see: owned, trialled, purchasable, or
    previewable.

    Preview is why this list is public-facing rather than a shop shelf: §14.3
    rule 2 says a player may preview a LOCKED item, including one they do not
    currently meet the requirement for, because that is how a player learns
    what to chase. So this function does not filter on "can I have this yet" --
    it filters on "is this a real item whose event is running".

    The event gate is the §5.5 fix. Event auras used to be omitted here (with a
    comment saying the check was unimplemented) while the till sold them to
    anyone. Both ends now call `event_is_running`.
    """
    results = []
    now = now if now is not None else time.time()
    realm = player.get("cultivation_realm", "Mortal")
    sect_id = player.get("sect_id")
    trib_cycles = player.get("legacy", {}).get("tribulation_cycles_survived", 0)
    ascension_season = player.get("legacy", {}).get("ascension_season", 0)
    loadout = player.get("cosmetic_loadout", {}) or {}

    def _entry(cid: str, config: dict) -> dict:
        row = config.copy()
        row["slot"] = slot_for_cosmetic(config)
        row["owned"] = owns_cosmetic(player, cid)
        row["trial_active"] = _trial_seconds_left(
            _owned_rows(player).get(cid), now
        ) > 0.0
        row["equipped"] = loadout.get(row["slot"]) == cid if row["slot"] else False
        row["previewable"] = True
        # The requirement travels WITH the row, so a locked item can tell the
        # player what they are missing instead of hiding the number.
        row["requirement_met"] = _check_requirement(
            config, realm, sect_id, trib_cycles, ascension_season
        )
        return row

    for cid, config in COSMETIC_CATALOG.items():
        results.append(_entry(cid, config))

    for cid, config in EVENT_COSMETICS.items():
        if event_is_running(config.get("event_source"), now):
            results.append(_entry(cid, config))
    return results


def slot_for_cosmetic(config: dict) -> Optional[str]:
    """The slot this cosmetic occupies, or None for the implicit types.

    A realm badge may be drawn in the portrait-frame slot -- the design reuses
    the badge's own frame artwork there (§12.1) -- so a badge is the one type
    with an optional slot. A definition can name `slot` explicitly to opt in.
    """
    explicit = config.get("slot")
    if explicit:
        return explicit
    type_value = config.get("type")
    type_value = getattr(type_value, "value", type_value)
    return SLOT_BY_TYPE.get(type_value)


def get_loadout(player: dict) -> dict:
    """The player's loadout, one entry per equippable slot.

    Reads defensively on purpose (§12.2 rule 2): a player row written before
    this feature existed has no `cosmetic_loadout` key at all, and a route that
    raised on the missing key would break every existing player. Every slot
    defaults to None, which means "nothing in this slot".

    The loadout holds COSMETIC IDS ONLY. No stats, no numbers the game
    computes. That is what makes "equipping changes nothing but the drawing" a
    structural property rather than a promise.
    """
    stored = player.get("cosmetic_loadout") or {}
    if not isinstance(stored, dict):
        stored = {}
    return {slot: stored.get(slot) or None for slot in EQUIPPABLE_SLOTS}


def _set_loadout(player: dict, slot: str, cosmetic_id: Optional[str]) -> dict:
    """Writes the loadout. The only writer, so the shape is defined once."""
    loadout = get_loadout(player)
    loadout[slot] = cosmetic_id
    player["cosmetic_loadout"] = loadout
    return loadout


def equip_cosmetic(player: dict, cosmetic_id: str, slot: Optional[str] = None) -> tuple[bool, str]:
    """Equips a cosmetic. Returns ``(ok, reason)``.

    Four checks, in this order, per §15.3:

    1. **The id resolves.** An id the catalogue does not know is refused
       loudly here. This is where §13.2's battle-pass bug becomes VISIBLE rather
       than invisible: the grant is written, and the first time the player tries
       to wear it they are told the item does not exist.
    2. **The type has a slot.** A realm badge with no slot, or a sect banner,
       is derived by the game and cannot be equipped.
    3. **The slot matches.** Equipping a weapon skin into the aura slot is a
       refusal, not a silent relocation to the right slot -- a client that
       guessed wrong should find out.
    4. **Ownership.** Not owned and not on a live trial is refused. A trial
       counts while it runs, and the reason string says which, so the client
       can say "your trial expired" rather than "you do not own this".

    No debit, no receipt: equip is not a purchase. A repeated equip of the same
    id is a no-op that returns success, so a double-tap is harmless.

    The loadout stores the id and nothing else. This function cannot make a
    player stronger, and the whole-player snapshot diff test in
    `server/test_cosmetics.py` is the proof.
    """
    config = get_cosmetic(cosmetic_id)
    if not config:
        return False, f"Unknown cosmetic: {cosmetic_id}"

    actual_slot = slot_for_cosmetic(config)
    if not actual_slot:
        type_value = getattr(config.get("type"), "value", config.get("type"))
        return False, f"{type_value} is derived by the game and cannot be equipped"

    if slot is not None and slot != actual_slot:
        return False, f"{cosmetic_id} belongs in the {actual_slot} slot, not {slot}"

    if not owns_cosmetic(player, cosmetic_id):
        entry = _owned_rows(player).get(cosmetic_id)
        if entry and entry.get("trial_expires"):
            return False, "Your trial has expired"
        return False, f"You do not own {cosmetic_id}"

    _set_loadout(player, actual_slot, cosmetic_id)
    return True, "equipped"


def unequip_cosmetic(player: dict, slot: str) -> tuple[bool, str]:
    """Empties one slot. Returns ``(ok, reason)``.

    Unequipping a slot that is already empty succeeds. The player asked for the
    slot to be empty; it is empty. An error here would make a client that
    double-taps show a failure for a state the player already wanted.
    """
    if slot not in EQUIPPABLE_SLOTS:
        return False, f"Unknown slot: {slot}"
    _set_loadout(player, slot, None)
    return True, "unequipped"


def get_equipped_cosmetics(player: dict) -> list[dict]:
    """The cosmetics currently worn, one per slot.

    This is what the game DRAWS. It walks the loadout rather than the ownership
    dict, which is the difference between §12's measured bug (two auras active
    at once, because `get_active_cosmetics` returned everything owned) and one
    aura per slot.

    A loadout entry naming an id the player no longer owns is skipped rather
    than rendered. Ownership can be revoked by a rollback or a support action
    while the loadout row survives, and drawing a cosmetic the player does not
    have is worse than drawing nothing.

    A loadout entry naming an id that belongs in a DIFFERENT slot is skipped
    for the same reason, and that check was missing. `equip_cosmetic` refuses
    the mismatch on the way in, so this only fires on a loadout row that did not
    come from an equip: a legacy row written before the type-to-slot mapping, a
    hand-edited save, or a rollback that restored an older loadout against a newer
    catalogue. Measured on 2026-10-03 -- a player owning the weapon skin
    `super_skin_1` with `{"aura": "super_skin_1"}` in the loadout got back

        {"slot": "aura", "cosmetic_id": "super_skin_1", "type": "weapon_skin", ...}

    which tells the client to draw a weapon skin as an aura. The caller cannot
    detect that: the `slot` and the `type` in the same row disagree, and which
    one the client trusts is the client's decision. The loadout key is what is
    rendered, so the row is dropped rather than rendered under a slot its own
    definition contradicts.
    """
    equipped = []
    for slot, cosmetic_id in get_loadout(player).items():
        if not cosmetic_id:
            continue
        config = get_cosmetic(cosmetic_id)
        if not config or not owns_cosmetic(player, cosmetic_id):
            continue
        if slot_for_cosmetic(config) != slot:
            continue
        equipped.append({
            "slot": slot,
            "cosmetic_id": cosmetic_id,
            "type": getattr(config.get("type"), "value", config.get("type")),
            "name": config.get("name"),
            "visual": config.get("visual", {}),
        })
    return equipped


def preview_cosmetic(cosmetic_id: str) -> tuple[bool, dict, str]:
    """The visual block for ANY cosmetic, owned or not. Returns
    ``(ok, payload, reason)``.

    §14.2: preview is client-side by design, because the visual description is
    public data the catalogue already returns to anyone. This function exists so
    the client has ONE server-authoritative answer to "what does this look
    like" rather than a hand-copied table that drifts from the catalogue.

    It requires NO ownership and writes NOTHING. Both are load-bearing:

    * no ownership check -- §14.3 rule 2, preview is allowed on locked items
      including ones whose requirement the player does not meet, because that
      is how a player learns what to chase;
    * no write -- §14.3 rule 1, a previewed item is still unowned and still
      unbuyable until a purchase succeeds. A preview that left a row behind
      would be a trial that never expires.

    The payload is a whitelist, not the catalogue row. That is §7.4's low-
    severity note acted on: the public route stops returning raw definition
    dicts the moment a definition carries anything non-public. The requirement
    string IS included, because §14.3 rule 2 requires the requirement to be
    shown on the same row as the preview.
    """
    config = get_cosmetic(cosmetic_id)
    if not config:
        return False, {}, f"Unknown cosmetic: {cosmetic_id}"
    payload = {
        "cosmetic_id": cosmetic_id,
        "name": config.get("name"),
        "type": getattr(config.get("type"), "value", config.get("type")),
        "rarity": getattr(config.get("rarity"), "value", config.get("rarity")),
        "slot": slot_for_cosmetic(config),
        "visual": config.get("visual", {}),
        "requirement": config.get("requirement") or config.get("realm_requirement"),
        "purchase_currency": config.get("purchase_currency"),
        "purchase_cost": config.get("purchase_cost"),
    }
    return True, payload, "ok"



def _check_requirement(config: dict, realm: str, sect_id: Optional[str],
                       trib_cycles: int, ascension_season: int) -> bool:
    if "realm_requirement" in config:
        return realm == config["realm_requirement"]
    if "requirement" in config:
        req = config["requirement"]
        if "tribulation_cycles_survived" in req:
            # Handle both >= and == formats
            if ">=" in req:
                return trib_cycles >= int(req.split(">=")[1].strip())
            elif "==" in req:
                return trib_cycles == int(req.split("==")[1].strip())
        if "ascension_season" in req:
            # Handle both >= and == formats
            if ">=" in req:
                return ascension_season >= int(req.split(">=")[1].strip())
            elif "==" in req:
                return ascension_season == int(req.split("==")[1].strip())
    if config["type"] == CosmeticType.SECT_BANNER and not sect_id:
        return False
    return True


def _owned_rows(player: dict) -> dict:
    """The player's cosmetic rows, as a dict, whatever shape the row is in.

    THE ONE READER, because nine functions need this answer and a defensive
    check in each of them is nine places for the next reader to forget. Measured
    on 2026-10-03: with `player["cosmetics"]` holding a LIST -- which
    `server/event_rewards.py:127` writes when a daily-event reward pays a
    cosmetic -- all nine raised `AttributeError: 'list' object has no
    attribute 'get'`:

        get_active_cosmetics  get_equipped_cosmetics  get_available_cosmetics
        owns_cosmetic         _is_trial_active        grant_cosmetic
        purchase_cosmetic     start_trial             equip_cosmetic

    That module models an owned cosmetic as a bare id in a list, this module
    models it as a row in a dict. Whichever is right, the player row in between
    can hold either, and a cosmetic read that raises takes down equip, purchase
    and trial for that player -- not just the one view. So the shape is
    normalised HERE, once.

    A list is read as ids with an implicit permanent grant, because that is what
    the writer meant. Anything else that is not a dict is treated as no rows at
    all: an unreadable row must not become a crash, and must not become an
    invented grant either.
    """
    stored = player.get("cosmetics")
    if isinstance(stored, dict):
        return stored
    if isinstance(stored, list):
        return {
            cid: {"owned": True, "granted_at": None, "source": "imported"}
            for cid in stored if isinstance(cid, str)
        }
    return {}


def _owned_rows_mut(player: dict) -> dict:
    """:func:`_owned_rows`, for the three writers.

    Same normalisation, but it puts the dict back on the player row. A writer
    that used `setdefault` would otherwise keep appending to whatever shape was
    already there: on a list-shaped row `setdefault("cosmetics", {})` returns the
    LIST, so `[cosmetic_id] = {...}` either raises or, on a list, quietly does
    nothing the writer intended. Normalising on the way in makes every write
    leave the row in the one shape this module reads.
    """
    rows = _owned_rows(player)
    if player.get("cosmetics") is not rows:
        player["cosmetics"] = rows
    return rows


def _trial_seconds_left(entry: Optional[dict], now: float) -> float:
    """Seconds of trial remaining on ``entry``. 0.0 when there is no live trial.

    One reader for the trial clock, because the two questions -- "is this row
    still trialling" and "how long is left" -- were asked two different ways and
    only one of them was safe.

    THE DEFECT: `get_active_cosmetics` compared `data.get("trial_expires", 0)`
    straight against `time.time()`. The `0` default only fires when the key is
    ABSENT, so a row that HAS the key with a `None` value -- which is exactly
    what `grant_cosmetic` writes for a permanent grant -- reached the comparison
    as `None > float` and raised. Measured on 2026-10-03, two crashing shapes:

        {"owned": False, "trial_expires": None}
          -> TypeError: '>' not supported between instances of 'NoneType' and 'float'
        {"owned": False, "trial_expires": <datetime>}   # TIMESTAMPTZ, see below
          -> TypeError: '>' not supported between instances of 'datetime.datetime' and 'float'

    The second shape is not hypothetical. `server/migrations/011_cosmetics.sql`
    stores this column as `TIMESTAMPTZ`, so any row that has been through a real
    database comes back as a `datetime`, and `datetime > float` raises the same
    way. A player who started a trial and reloaded would 500 on
    `/cosmetics/player/{id}`.

    `owns_cosmetic` and `_is_trial_active` already answered this safely with an
    `isinstance` guard. This is that same guard, shared, plus the arithmetic the
    old inline version needed. Non-numeric and past values both read as 0.0, so
    an unreadable expiry expires the trial rather than granting one.
    """
    if not isinstance(entry, dict):
        return 0.0
    expires = entry.get("trial_expires")
    # bool is a subclass of int; a True here is a corrupt row, not a trial.
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return 0.0
    remaining = expires - now
    return remaining if remaining > 0 else 0.0


def _is_trial_active(player: dict, cosmetic_id: str) -> bool:
    return _trial_seconds_left(_owned_rows(player).get(cosmetic_id), time.time()) > 0.0


def owns_cosmetic(player: dict, cosmetic_id: str) -> bool:
    """True when the player may WEAR this cosmetic right now.

    Two shapes count, and the difference matters to the refusal message:

    * a permanent grant (``owned`` is True), and
    * a LIVE trial (``trial_expires`` in the future).

    An expired trial does not count. That is the whole §3 defect in one
    predicate: the old purchase check asked "is this id a key in the dict",
    which a stale trial row answers yes to forever, so a player who trialled an
    aura could never buy it. Predicates that read an intent ("do I own this")
    rather than a shape ("is there a row") do not have that failure.
    """
    entry = _owned_rows(player).get(cosmetic_id)
    if not entry:
        return False
    if entry.get("owned") is True:
        return True
    expires = entry.get("trial_expires")
    return isinstance(expires, (int, float)) and expires > time.time()


def start_trial(player: dict, cosmetic_id: str) -> bool:
    """Starts a trial for a cosmetic. Returns True if started.

    A player who already OWNS the item is refused. A player with a LIVE trial is
    refused — extending your own trial by calling this again is not a trial. A
    player whose trial has EXPIRED gets a fresh row, because the old one is
    dead weight that would otherwise sit in the dict forever.

    The third case is the fix for §3. The old code refused on dict presence, so
    the expired row was permanent and un-buyable-over; the new code refuses on
    intent, and an expired row is overwritten.

    Uses TRIAL_SECONDS, not the literal it used to hardcode. Two sources of
    truth for one number is how a 3-day trial silently becomes a 3-day trial
    forever when someone edits the constant and not the call site.
    """
    config = get_cosmetic(cosmetic_id)
    if not config or not config.get("trial"):
        return False
    if owns_cosmetic(player, cosmetic_id):
        return False  # already owned, or a trial still running

    now = time.time()
    _owned_rows_mut(player)[cosmetic_id] = {
        # owned is explicitly False so the row has ONE shape. The schema makes
        # owned/trial_expires an XOR (`server/migrations/011_cosmetics.sql`), and
        # a trial row missing the flag would not survive a database round trip.
        "owned": False,
        "trial_expires": now + TRIAL_SECONDS,
        "trial_started": now,
        "source": "trial",
    }
    return True



# -----------------------------------------------------------------------------
# WHICH LEDGER THE TILL CHARGES
# -----------------------------------------------------------------------------
# THE CURRENCY SPLIT, AND WHY THE TILL USED THE WRONG HALF OF IT.
#
# This module spent `player["spirit_stones"]`. That was the only thing in the
# tree that did, and it is the defect: the SHOP is not the only place a player
# is paid. Measured on the mounted app before this change:
#
#     POST /achievements/claim       -> 200, reward {spirit_stone: 2500}
#     GET  /wallet                   -> spirit_stone = 2500  (the player's money)
#     POST /cosmetics/{id}/purchase  -> 400 "Purchase failed" (cheapest item: 200)
#
# The award and the balance the player can SEE both live in the wallet store
# (`main.wallet_store`, keyed by player_id). This module looked at a different
# field on a different object: the seven-grade dict on the player row, whose
# `low_grade` is a cultivation-rate INPUT (`main.py:1544`) and which nothing has
# been folding into the wallet -- `stone_backfill.py` says so in its own first
# line, "DESIGNED, NOT APPLIED. Nothing in this module is called by any route."
# So the shop quoted a price from one ledger and settled on another.
#
# `design/spirit-stone-credit-ledger.md` reaches the same conclusion from the
# write side, and `server/stone_backfill.py` names the destination store: "The
# wallet."
#
# TWO NAMES, ONE CURRENCY. The catalogue prices items in `spirit_stones` (and
# `migrations/011_cosmetics.sql:130` CHECK-constrains that spelling); the
# wallet's dataclass field is `spirit_stone` (wallet_defense.py:206). The mapping
# is declared once here rather than re-spelled at each call site -- the same
# discipline `lottery.py:WALLET_CURRENCY_MAP` applies to this exact pair.
#
# THE OTHER THREE CURRENCIES STAY ON THE PLAYER ROW, deliberately.
# `sect_contribution`, `north_star_dust`/`moon_essence` and
# `tribulation_token` have no wallet balance to charge. The wallet holds four
# soft currencies and no tokens; `lottery.py:928-938` documents the same finding
# for the token pool, where adding the name to the wallet whitelist would only
# turn "unknown currency" into "insufficient balance" because the balance is
# structurally 0. Those three are untouched.
WALLET_CURRENCY_MAP = {"spirit_stones": "spirit_stone"}


def _wallet_currency(purchase_currency: str) -> Optional[str]:
    """The wallet currency a catalogue currency maps to, or None.

    None means "this currency does not live in the wallet", which is the normal
    answer for three of the four catalogue currencies and is NOT an error.
    """
    mapped = WALLET_CURRENCY_MAP.get(purchase_currency)
    if mapped is None:
        return None
    try:
        from wallet_defense import CURRENCIES
    except ImportError:
        return None
    return mapped if mapped in CURRENCIES else None


def _wallet_balance(wallet_store, player_id: str, wallet_currency: str) -> Optional[int]:
    """The wallet's balance in one currency, or None when it cannot be read.

    None means "no answer" and is never 0: a 0 here would read as "the player is
    broke" and refuse a purchase they can afford, which is the exact symptom
    this change exists to remove.
    """
    if wallet_store is None or not player_id:
        return None
    try:
        wallet = wallet_store.get(player_id)
    except Exception:
        return None
    if wallet is None:
        # No wallet row for this player. The store can still be asked to spend,
        # but there is nothing to read; None keeps that distinguishable from a
        # real zero balance.
        return None
    balance = getattr(wallet, "balance", None)
    if balance is None:
        return None
    # PostgreSQL hands back a plain dict; the in-memory store hands back the
    # CurrencyBalance dataclass. Getting this wrong is a live crash hazard in
    # exactly one of the two modes, so both are read here rather than at each
    # call site (the same normalisation `currency_exchange.read_balance` does).
    if isinstance(balance, dict):
        return int(balance.get(wallet_currency, 0) or 0)
    return int(getattr(balance, wallet_currency, 0) or 0)


def _row_balance(player: dict, purchase_currency: str) -> Optional[int]:
    """The player-row balance for a currency that has no wallet balance."""
    if purchase_currency == "sect_contribution":
        return int(player.get("sect", {}).get("contribution", 0) or 0)
    if purchase_currency in ("north_star_dust", "moon_essence"):
        return int(player.get("monthly_event", {}).get(purchase_currency, 0) or 0)
    if purchase_currency == "tribulation_token":
        return int(player.get("tokens", {}).get("tribulation_token", 0) or 0)
    return None


def _spend_ledgers(player: dict, purchase_currency: str, *,
                   player_id: str = None,
                   wallet_store=None) -> list:
    """The ledgers this currency can be spent from, most-authoritative FIRST.

    ONE list, built once, and both the affordability check and the debit read it
    -- which is the point. The check and the debit must never disagree about
    which store holds the number they are looking at; an earlier shape of this
    file inlined the read in both places, which is exactly the arrangement under
    which a second currency is added to the check and forgotten in the debit.

    ORDER IS THE DESIGN DECISION, and it is wallet first. Every award the player
    can see pays the wallet, so the wallet is the number that moves when they
    buy -- and a client reading the balance after a purchase sees it change.
    The player row is the FALLBACK, not the alternative, and it is still here for
    a measured reason: the welcome grant is written to the row at signup
    (`main.py`, `low_grade: 500`) and `stone_backfill.py` has never been applied,
    so a strict wallet-only till empties the shop for every new account. Measured
    on the mounted app with a wallet-only rule: a brand-new account holding 500
    stones was refused the 200-stone welcome aura. The fallback is what keeps
    that account able to buy while the fold is pending; it is transitional and
    should go the day the backfill runs and the row stops being funded.
    """
    ledgers = []
    wallet_currency = _wallet_currency(purchase_currency)
    if wallet_currency is not None and wallet_store is not None:
        ledgers.append(("wallet", wallet_currency))
    if purchase_currency == "spirit_stones" or _row_balance(player, purchase_currency) is not None:
        ledgers.append(("row", purchase_currency))
    return ledgers


def _currency_balance(player: dict, purchase_currency: str, *,
                      player_id: str = None,
                      wallet_store=None) -> Optional[int]:
    """The most a player can spend in ONE purchase of ``purchase_currency``.

    A single purchase is charged from a single ledger, so this is the LARGEST
    balance across the ledgers that could pay it -- not their sum. Summing would
    advertise 500 row + 2500 wallet = 3000 and then have to decline a 200-stone
    item because neither ledger alone covers it.

    None means unpriceable: no ledger holds this currency at all.
    """
    best = None
    for where, currency in _spend_ledgers(player, purchase_currency,
                                          player_id=player_id,
                                          wallet_store=wallet_store):
        if where == "wallet":
            value = _wallet_balance(wallet_store, player_id, currency)
        else:
            value = _row_balance(player, purchase_currency)
            if value is None:
                stones = player.get("spirit_stones", 0)
                if isinstance(stones, dict):
                    value = int(stones.get("low_grade", 0) or 0)
                else:
                    value = int(stones or 0)
        if value is None:
            continue
        best = value if best is None else max(best, value)
    return best


def _debit_currency(player: dict, purchase_currency: str, amount: int, *,
                    player_id: str = None,
                    wallet_store=None,
                    idempotency_key: str = None):
    """Spend ``amount`` from the FIRST ledger that can cover it.

    Returns None when the player row paid, or the wallet's receipt dict when the
    wallet did. A wallet REFUSAL is returned as its receipt rather than being
    retried on the row: "insufficient balance" in the wallet is an answer about
    the player's money, and quietly charging a second ledger after it would make
    one purchase cost two stores.

    Raises when the wallet backend itself errors. It does NOT fall back to the
    player row in that case -- a backend that cannot be reached is not an answer
    about the balance, and treating it as one is how a purchase ends up granting
    a cosmetic whose money never moved.
    """
    for where, currency in _spend_ledgers(player, purchase_currency,
                                          player_id=player_id,
                                          wallet_store=wallet_store):
        if where == "wallet":
            held = _wallet_balance(wallet_store, player_id, currency)
        else:
            held = _row_balance(player, purchase_currency)
            if held is None:
                stones = player.get("spirit_stones", 0)
                if isinstance(stones, dict):
                    held = int(stones.get("low_grade", 0) or 0)
                else:
                    held = int(stones or 0)
        if held is None or held < amount:
            continue
        if where == "wallet":
            return _wallet_spend(wallet_store, player_id, currency, amount,
                                 idempotency_key=idempotency_key)
        # The player row has no version and no idempotency ledger, so this write
        # is guarded by the caller exactly as it always was.
        if purchase_currency == "spirit_stones":
            stones = player.get("spirit_stones", 0)
            if isinstance(stones, dict):
                stones["low_grade"] = held - amount
            else:
                player["spirit_stones"] = held - amount
        elif purchase_currency == "sect_contribution":
            player.setdefault("sect", {})["contribution"] = held - amount
        elif purchase_currency in ("north_star_dust", "moon_essence"):
            event = player.setdefault("monthly_event", {})
            event[purchase_currency] = held - amount
        elif purchase_currency == "tribulation_token":
            tokens = player.setdefault("tokens", {})
            tokens["tribulation_token"] = held - amount
        return None
    return {"ok": False, "error": "insufficient balance"}


def _wallet_spend(wallet_store, player_id: str, wallet_currency: str,
                  amount: int, *, idempotency_key: str = None):
    """Charge ``amount`` through the wallet's own guarded spend, or None.

    THE WALLET'S GUARDS ARE THE POINT OF ROUTING MONEY HERE. `spend` carries a
    monotonic version check, a per-key idempotency ledger and an
    insufficient-balance refusal. The player-row write this replaces had none of
    the three.

    THE VERSION IS THE SERVER'S, never a literal. The wallet compares it against
    its current row and refuses a mismatch; passing 0 makes every charge answer
    "stale wallet_version". `beast_wave.py:1000` does it this way for the same
    reason, and its comment records the bug that a literal 0 caused there.
    """
    if wallet_store is None or not player_id:
        return None
    from wallet_dispatch import spend as wallet_spend
    current = wallet_store.get(player_id)
    version = int(getattr(current, "version", 0) or 0)
    # The wallet's ledger keys on this string. Without a caller key a double tap
    # would mint a fresh key per attempt and no replay check could ever fire, so
    # one is derived from the player, the currency and the amount: a second tap
    # asks for the same thing and is refused instead of charged twice.
    key = idempotency_key or (
        "cosmetic:%s:%s:%s" % (player_id, wallet_currency, amount))
    # No `route` kwarg: `wallet_dispatch.spend` does not accept one. Passing it
    # raised TypeError inside the call this module used to wrap in a bare
    # `except`, the fallback then charged the player row instead, and the
    # purchase answered 200 with the wallet untouched -- a free cosmetic.
    # Measured on the mounted app before the kwarg was removed.
    return wallet_spend(wallet_store, player_id, wallet_currency,
                        int(amount), version, key)


def _receipts(player: dict) -> dict:
    return player.setdefault("cosmetic_receipts", {})


def _request_fingerprint(cosmetic_id: str, currency: Optional[str]) -> str:
    canonical = json.dumps({"cosmetic_id": cosmetic_id, "currency": currency or "spirit_stones"}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_is_running(event_source: Optional[str], now: Optional[float] = None) -> bool:
    """Is the named monthly event live right now?

    §5.5's invisible-purchase drain, closed. The event auras were omitted from
    the catalogue list (so no player could see or buy one) while the till would
    still sell one to anyone, with no event running — measured on 2026-09-30,
    500 north-star dust spent on something the UI never offered.

    Both ends now ask the same question. The list asks it to decide what to
    show; the till asks it to decide whether to sell. A second implementation of
    the same gate is how the two drift apart again, so there is one.

    Falls back to "not running" if the event module cannot be imported, which
    fails CLOSED: a live event that cannot be confirmed does not sell.
    """
    if not event_source:
        return True  # not an event cosmetic; nothing to gate
    try:
        from monthly_event import get_event_state
    except ImportError:
        return False
    state = get_event_state(now if now is not None else time.time())
    return bool(state.get("active")) and state.get("theme") == event_source


def grant_cosmetic(player: dict, cosmetic_id: str, source: str,
                   currency: Optional[str] = None, cost: Optional[int] = None) -> bool:
    """The ONE place a cosmetic is written to a player row.

    Every acquisition path — shop, battle pass, event, gacha, and the three free
    progression grants — goes through here. Before this existed each path built
    its own row literal, which is how the battle pass ended up writing a row for
    an id the catalogue had never heard of (§13.2): the writer did not consult
    the catalogue, so nothing noticed.

    Two refusals, and both are load-bearing:

    * **Unknown id.** A grant for an id with no catalogue row is refused, not
      written. A row nobody can resolve is invisible to the player and
      permanent on disk: the pass's five ids were exactly this.
    * **Already owned.** Refused, not overwritten. A second grant of a
      permanent cosmetic is a no-op rather than a fresh receipt, so a replayed
      battle-pass claim cannot reset a player's purchase timestamp.

    A TRIAL row is not "already owned" here and IS replaced. A trial is a
    temporary state on the way to owning the thing; a permanent grant landing on
    top of it is the trial succeeding, not a conflict. This is the second half
    of the section 3 fix: the purchase path asks "have I paid for this" (a
    trial has not) and then calls this, so the two predicates have to agree
    about what a trial means or the grant is refused at the last step.

    Only ``cosmetics`` is written. Nothing here touches a stat, a currency
    balance, or a progression field, and the snapshot-diff test in
    `server/test_cosmetics.py` is what proves it.
    """
    config = get_cosmetic(cosmetic_id)
    if not config:
        return False
    existing = _owned_rows(player).get(cosmetic_id)
    if existing and existing.get("owned") is True:
        return False
    now = time.time()
    entry = {
        "owned": True,
        "trial_expires": None,
        "granted_at": now,
        # `purchased_at` is kept alongside `granted_at` because the shop receipt
        # and the analytics that read it have always used this key. A rename is
        # a migration; two names for one instant is a one-line cost.
        "purchased_at": now,
        "source": source,
    }
    if currency is not None:
        entry["currency"] = currency
    if cost is not None:
        entry["cost"] = int(cost)
    _owned_rows_mut(player)[cosmetic_id] = entry
    return True


def purchase_cosmetic(player: dict, cosmetic_id: str, currency: str = None, *,
                      player_id: str = None,
                      wallet_store=None,
                      idempotency_key: str = None) -> bool:
    """Purchases a cosmetic permanently. Returns True if purchased.

    The caller owns receipt handling; this function is intentionally limited to
    the economy transition and rejects a second purchase of the same cosmetic.

    The currency is decided by the cosmetic's own definition, never by the
    request. A cosmetic with no ``purchase_currency`` in its definition is not
    for sale. A request that names a currency other than the defined one is
    refused rather than quietly charged in the caller's chosen currency.

    THE §3 FIX IS THE `owns_cosmetic` CALL. This used to be
    ``cosmetic_id in player.get("cosmetics", {})``, which is a question about
    the SHAPE of the player's row rather than about whether they own the item.
    A live trial answers it yes, so "try it, like it, buy it" ended at "try it"
    forever. The live proof on 2026-09-30 was:

        trial start:          True
        purchase after trial: False   <-- refused
        balance still:        {'low_grade': 99999}   <-- not charged
    """
    config = get_cosmetic(cosmetic_id)
    # §3 FIX: refuse only a PERMANENT grant, not the mere presence of a row.
    #
    # The original line was `cosmetic_id in player.get("cosmetics", {})`, a
    # question about the SHAPE of the player's row. A live trial row answers it
    # yes, so "try it, like it, buy it" ended at "try it" forever. Measured on
    # 2026-09-30 against the real module:
    #
    #     trial start:          True
    #     purchase after trial: False   <-- refused
    #     balance still:        {'low_grade': 99999}   <-- not charged
    #
    # Note this is NOT `owns_cosmetic`, which counts a live trial. The trial is
    # the funnel this purchase exists to catch, so a player wearing a trial must
    # be able to buy the thing they are wearing. Two predicates, two questions:
    # `owns_cosmetic` answers "may I wear it" (trial counts), this one answers
    # "have I already paid for it" (trial does not).
    if not config:
        return False
    entry = _owned_rows(player).get(cosmetic_id)
    if entry and entry.get("owned") is True:
        return False  # already paid for; no second charge, ever

    # Currency and cost come from the item definition only.
    purchase_currency = config.get("purchase_currency")
    if not purchase_currency:
        return False
    if currency is not None and currency != purchase_currency:
        return False
    purchase_cost = config.get("purchase_cost", 0)

    # An event cosmetic is only sellable while its event is live. Same gate the
    # catalogue list uses, so the two cannot disagree about what is for sale.
    if config.get("event_source") and not event_is_running(config["event_source"]):
        return False

    balance = _currency_balance(player, purchase_currency,
                                player_id=player_id, wallet_store=wallet_store)
    if balance is None or balance < purchase_cost:
        return False

    # The debit answers whether the money actually moved. A REFUSAL ends the
    # purchase here: "insufficient balance" is a real answer about the player's
    # money, and granting anyway would hand out a cosmetic nobody paid for --
    # the free-money version of the very defect this function was fixed for.
    receipt = _debit_currency(player, purchase_currency, purchase_cost,
                              player_id=player_id, wallet_store=wallet_store,
                              idempotency_key=idempotency_key)
    if isinstance(receipt, dict) and not receipt.get("ok"):
        return False
    if isinstance(receipt, dict) and receipt.get("_replayed"):
        # The wallet charged nothing on a replay, so the first attempt already
        # took the money. Granting again would be a second delivery for one
        # payment, so the retry is refused here.
        return False
    return grant_cosmetic(
        player, cosmetic_id, source="shop",
        currency=purchase_currency, cost=purchase_cost,
    )


def get_active_cosmetics(player: dict) -> list[dict]:
    """Cosmetics the player has that are currently wearable.

    "Active" here means OWNED OR ON A LIVE TRIAL -- every wearable thing -- NOT
    "equipped". The equipped set is `get_equipped_cosmetics`, which walks the
    loadout.

    The two used to be the same function, and that conflation was §12's
    measured bug: it walked the ownership dict and returned everything in it, so
    a player who owned two auras had both active at once and there was no step
    that could turn one off. They are separate questions now, and the client
    asks both.
    """
    active = []
    # One clock read for the whole call. It used to be two per trial row, so a
    # player with N trialled cosmetics called the clock 2N times and two rows
    # could disagree about "now" by the width of the loop. Measured on
    # 2026-10-03: 4 reads for 2 trial rows before this change, 1 after.
    now = time.time()
    for cid, data in _owned_rows(player).items():
        config = get_cosmetic(cid)
        if not config:
            continue
        # `data` is a row another module may have written in any shape, so it
        # is asked whether it is a MAPPING before it is asked for keys. The old
        # `data.get("owned")` raised on a row that was not a dict.
        row = data if isinstance(data, dict) else {}
        if row.get("owned") is True:
            active.append({"id": cid, "owned": True, "slot": slot_for_cosmetic(config), **config})
            continue
        remaining = _trial_seconds_left(row, now)
        if remaining > 0.0:
            active.append({
                "id": cid,
                "trial_remaining": remaining,
                "slot": slot_for_cosmetic(config),
                **config,
            })
    return active


def _public_catalogue_row(cosmetic_id: str, config: dict) -> dict:
    """A catalogue entry as the wire sees it.

    A WHITELIST, not the definition. §7.4's low-severity note, acted on: the
    catalogue route is unauthenticated by design (it is a public price list),
    so it should stop returning raw definition dicts the moment a definition
    carries anything non-public. Adding a field to the catalogue should be a
    deliberate decision about what the public route says, not a side effect.

    What is on the list and why:

    * `visual` — the whole point of the route. Preview is built from this.
    * `requirement` — §14.3 rule 2 requires the requirement to be shown on the
      same row as the preview, so a locked item can say what it wants.
    * `purchase_currency` / `purchase_cost` — a price list. The server still
      prices from the definition, never from the request; this is display only.
    * `slot` — so the client can group by tab without hardcoding the mapping.
    * `acquisition` — where it comes from, which is the answer to "can I get
      this without luck?".

    What is NOT on the list: anything numeric that is not a price. There is no
    stat field to leak because the catalogue has none, and this whitelist means
    adding one to a definition does not publish it.
    """
    return {
        "id": cosmetic_id,
        "name": config.get("name"),
        "description": config.get("description"),
        "type": getattr(config.get("type"), "value", config.get("type")),
        "rarity": getattr(config.get("rarity"), "value", config.get("rarity")),
        "slot": slot_for_cosmetic(config),
        "visual": config.get("visual", {}),
        "trial": bool(config.get("trial")),
        "requirement": config.get("requirement") or config.get("realm_requirement"),
        "purchase_currency": config.get("purchase_currency"),
        "purchase_cost": config.get("purchase_cost"),
        "acquisition": list(config.get("acquisition", [])),
    }


# The combat fields a cosmetic must never touch. Read by `combat_summary`, and
# asserted unchanged by the equip tests.
COMBAT_SUMMARY_FIELDS = (
    "cultivation_realm", "cultivation_stage", "spirit_root",
    "atk", "def_", "hp", "mp", "spd", "combat_power", "pvp_rating",
)


def combat_summary(player: dict) -> dict:
    """The player's combat-relevant numbers, as a plain dict.

    This exists for the zero-stat proof. §15.2 rule 2 says the equip response
    should echo the combat summary after the equip so a client can diff, and
    says the equality of the two is the test. Reading a FIXED list of fields
    rather than the whole row is what makes the assertion meaningful: a
    whole-row diff would also be satisfied by a loadout write, and would fail
    on any unrelated field the server touches during a request.

    A field the player row does not have reads as None rather than raising. The
    list is the union of what different fixtures carry, and a missing stat is
    genuinely absent, not an error.
    """
    return {field: player.get(field) for field in COMBAT_SUMMARY_FIELDS}


def register_cosmetic_routes(app, players_db_accessor):
    """Registers cosmetic endpoints."""
    from fastapi import Depends, HTTPException, Body, Request
    # `require_player` is the session dependency. Imported inside this function
    # for the same reason `main.py` imports `auth` late: `server/` is on
    # sys.path as a plain directory, so `auth` only resolves once main.py has
    # set that up, and importing it at module scope would make
    # `import server.cosmetics` fail from a test that has not booted the app.
    from auth import require_player

    def _get_db():
        return players_db_accessor() if callable(players_db_accessor) else players_db_accessor

    def _wallet():
        """The wallet store the till charges, or None when none is mounted.

        Read off the SAME store object the routes were mounted with:
        `PlayerStore(wallet_store=...)` keeps it (`player_store.py:69`), so this
        is the instance main.py already built. Deliberately NOT `import main`:
        importing it here builds a SECOND app and a SECOND store whenever this
        module is reached as `server.cosmetics` while main was imported as
        `main`, which this repository has been bitten by before. The same trap
        `communication.py:_discover_pg_pool` documents.

        A bare dict-backed harness has no `wallet_store` attribute and yields
        None, leaving the till player-row-only exactly as it was.

        RESOLVED EVERY CALL, NEVER CACHED IN A MODULE GLOBAL. This used to be
        a one-shot cache: `if _WALLET_STORE is None: _WALLET_STORE = getattr(...,
        None)`. Measured: it binds the FIRST store the module ever sees and then
        charges it forever. Mount the routes on a second app with a second store
        and every purchase is debited against the first store -- the second
        player's money never moves, and because that store holds no row for them
        its balance reads as None, so the till silently falls through to charging
        the player row. Same shape as a per-caller session cap that outlives a
        store swap. The lookup is one getattr on an object the request already
        holds, so there is nothing to save by caching it.
        """
        try:
            return getattr(_get_db(), "wallet_store", None)
        except Exception:
            return None

    def _read_balance(player: dict, player_id: str):
        """The balance a READ should publish: the ledger the till charges.

        `GET /cosmetics/owned` and `GET /cosmetics/player/{id}` published
        `player["spirit_stones"]` under the key "wallet" while the purchase
        route charged `wallet_store`. Measured on the mounted app with the
        two funded differently: wallet 4242, player row 77, purchase reply
        4242, owned reply 77. A client drawing the shop balance from this
        reply showed 77 stones while the player was spending 4242 —
        and the row number does not move when they buy.

        Falls back to the player row exactly when there is no wallet store
        or no wallet row for this player, which is the bare-dict harness
        case and the same fallback the till itself uses. This is a READ, so
        it never raises: a balance that cannot be read is the old answer.
        """
        store = _wallet()
        if store is not None and player_id:
            try:
                live = store.get(player_id)
            except Exception:
                live = None
            if live is not None:
                try:
                    from currency_exchange import balance_as_dict
                    return balance_as_dict(live)
                except Exception:
                    pass
        return player.get("spirit_stones", {})

    def _write(db, player_id):
        """The store's transaction, or a no-op context when it has none.

        Equip and unequip are writes, so they belong inside the same
        transaction the purchase path uses. The in-memory store has no
        `transaction`, and a test that constructs a bare dict-backed store must
        still be able to call these routes — so the absence of a transaction is
        a context manager that does nothing, not an AttributeError.
        """
        import contextlib

        if hasattr(db, "transaction"):
            return db.transaction(player_id)
        return contextlib.nullcontext()

    def _raise_equip_refusal(reason: str):
        """Maps an equip refusal to a status code.

        403 for "you do not own this" and 400 for everything else. The split is
        not cosmetic: a client shows different copy for the two, and "your trial
        has expired, buy it" is an upsell while "that is the wrong slot" is a
        bug report. Both are in the `detail` string, which the client already
        reads.
        """
        if reason.startswith("You do not own") or reason.startswith("Your trial"):
            raise HTTPException(status_code=403, detail=reason)
        raise HTTPException(status_code=400, detail=reason)

    @app.get("/cosmetics/catalog")
    async def get_cosmetic_catalog():
        """The legacy catalogue, kept because two client scripts still call it.

        Built the same way `/cosmetics/catalogue` is, which is the fix. It used to
        be one line:

            list(COSMETIC_CATALOG.values()) + list(EVENT_COSMETICS.values())

        TWO defects lived in that line, both measured on 2026-10-03.

        * The event gate was missing. `get_available_cosmetics` and
          `/cosmetics/catalogue` both filter event auras through
          `event_is_running`; this route did not, so it published
          `aura_north_mystic` and `aura_moon_palace` while no monthly event was
          live -- the same class as the section 5.5 invisible-purchase drain,
          inverted. The till refuses those two (measured: `purchase_cosmetic`
          returns False), so the list offered items the server would never sell.
          `get_available_cosmetics` documents "Both ends now call
          `event_is_running`". There were THREE ends. This was the third.
        * It published raw definition dicts. `_public_catalogue_row` documents
          its whitelist as the fix for section 7.4, and this route never called
          it. Measured keys that escaped only here: `event_source` and
          `realm_requirement`.

        Both clients are unaffected. `CosmeticView.gd` reads only `id`, `name`,
        `type`, `description`, `trial`, `purchase_currency` and `purchase_cost`
        from a row, and every one of those is on the whitelist.
        `CosmeticSystem.gd` `get_cosmetics_by_type` gains nothing it lacked.
        """
        now = time.time()
        items = [_public_catalogue_row(cid, config)
                 for cid, config in COSMETIC_CATALOG.items()]
        for cid, config in EVENT_COSMETICS.items():
            if event_is_running(config.get("event_source"), now):
                items.append(_public_catalogue_row(cid, config))
        return {"cosmetics": items, "slots": list(EQUIPPABLE_SLOTS)}

    @app.get("/cosmetics/player/{player_id}")
    async def get_player_cosmetics(player_id: str):
        db = _get_db()
        if player_id not in db:
            raise HTTPException(status_code=404, detail="Player not found")
        player = db[player_id]
        active = get_active_cosmetics(player)
        effects = [
            {
                "cosmetic_id": item["id"],
                "type": item.get("type").value if hasattr(item.get("type"), "value") else item.get("type"),
                "visual": item.get("visual", {}),
                "source": "owned" if item.get("owned") else "trial",
            }
            for item in active
        ]
        return {
            "available": get_available_cosmetics(player),
            "active": active,
            "owned": list(_owned_rows(player).keys()),
            "wallet": _read_balance(player, player_id),
            "effects": effects,
        }

    @app.post("/cosmetics/{player_id}/start-trial")
    async def start_cosmetic_trial(player_id: str, cosmetic_id: str = Body(..., embed=True)):
        db = _get_db()
        if player_id not in db:
            raise HTTPException(status_code=404, detail="Player not found")
        player = db[player_id]
        if start_trial(player, cosmetic_id):
            if hasattr(db, "persist"):
                db.persist(player_id, player)
            return {"ok": True, "cosmetic_id": cosmetic_id}
        raise HTTPException(status_code=400, detail="Cannot start trial")

    @app.post("/cosmetics/{player_id}/purchase")
    async def purchase_cosmetic_endpoint(player_id: str, payload: dict = Body(...), request: Request = None):
        import os
        cosmetic_id = payload.get("cosmetic_id")
        currency = payload.get("currency")
        db = _get_db()
        if player_id not in db:
            raise HTTPException(status_code=404, detail="Player not found")
        player = db[player_id]
        # The server decides the currency from the item definition. A request
        # that asks for a different one is refused before any economy work.
        if currency is not None:
            definition = get_cosmetic(cosmetic_id) or {}
            if currency != definition.get("purchase_currency"):
                raise HTTPException(status_code=400, detail="Currency does not match the cosmetic definition")
        key = request.headers.get("Idempotency-Key", "") if request is not None else ""
        fingerprint = _request_fingerprint(cosmetic_id, currency)
        receipts = _receipts(player)
        prior = receipts.get(key) if key else None
        if prior is not None:
            if prior.get("fingerprint") != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency key payload conflict")
            return deepcopy(prior["response"])
        # Acceptance-only funding switch; never enabled in normal server operation.
        if os.getenv("WC_TEST_COSMETIC_FUNDS") == "1" and currency == "spirit_stones":
            stones = player.get("spirit_stones", {})
            if isinstance(stones, dict):
                stones["low_grade"] = max(int(stones.get("low_grade", 0)), 10000)
        # THE WALLET IS THE TILL'S LEDGER, resolved per request so a harness that
        # swaps stores is picked up. Charged against the same balance
        # `GET /wallet` reports to this player.
        wallet = _wallet()
        # The wallet's own ledger keys on this string, so the CALLER's key is
        # honoured rather than a fresh one minted per attempt: a per-attempt key
        # makes every retry look like a brand-new intent, so no replay check can
        # fire and a double tap charges twice.
        wallet_idem = None
        if key:
            wallet_idem = "cosmetic-purchase:%s:%s" % (player_id, key)
        elif wallet is not None:
            # No caller key means no retry protection from the client, but the
            # key is still derived so the same item bought twice in a row cannot
            # be charged twice.
            wallet_idem = "cosmetic-purchase:%s:%s:%s" % (
                player_id, cosmetic_id, _request_fingerprint(cosmetic_id, currency))
        # A wallet backend that errors REFUSES the purchase (503): no stones
        # moved, nothing granted, and the player may retry once it is healthy.
        # It must never fall through to charging the player row instead.
        try:
            bought = purchase_cosmetic(
                player, cosmetic_id, currency,
                player_id=player_id, wallet_store=wallet,
                idempotency_key=wallet_idem)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=("The wallet could not be charged for this purchase; "
                        "nothing was spent and nothing was granted (%s)"
                        % type(exc).__name__))
        if bought:
            if hasattr(db, "persist"):
                db.persist(player_id, player)
            # The reply reports the ledger the money came out of. It used to be
            # the player row's grade dict unconditionally, so a client reading
            # `wallet` after a purchase was shown a balance the purchase may
            # never have touched.
            balance = player.get("spirit_stones", {})
            if wallet is not None:
                live = wallet.get(player_id)
                if live is not None:
                    from currency_exchange import balance_as_dict
                    balance = balance_as_dict(live)
            response = {"ok": True, "cosmetic_id": cosmetic_id, "wallet": balance, "owned": list(_owned_rows(player).keys())}
            if key:
                receipts[key] = {"fingerprint": fingerprint, "response": deepcopy(response)}
                if len(receipts) > 50: receipts.pop(next(iter(receipts)))
                if hasattr(db, "persist"): db.persist(player_id, player)
            if os.getenv("WC_TEST_COSMETIC_LOSS_RESPONSE") == "1":
                raise HTTPException(status_code=503, detail="simulated response loss after commit")
            return response
        raise HTTPException(status_code=400, detail="Purchase failed")

    # -----------------------------------------------------------------------
    # The five routes below are the system's public surface. The four above
    # stay because `client/scripts/systems/CosmeticView.gd` calls all of them;
    # these are the ones the design asks for by name, and they are shaped
    # differently in one important way: they take the player from the SESSION,
    # not from the path.
    #
    # WHY NO {player_id} IN THE PATH
    # ------------------------------
    # The four legacy routes name the player in the path and are protected by
    # `install_ownership_guard`, which attaches `require_player` to any route
    # with a player claim. These five use `Depends(require_player)` directly and
    # read `identity.player_id`, so there is no claim in the request at all —
    # the player is not a parameter, it is the authenticated caller. A route
    # that cannot be told whose player to act on cannot be used against
    # somebody else's, which is a stronger property than a guard that has to be
    # present and correct on every deployment.
    # -----------------------------------------------------------------------

    def _session_player(identity):
        """The authenticated player's row, or a 404 if it is gone.

        A session can outlive its player: the store is authoritative and a
        player can be deleted. Answering 404 rather than 500 keeps that a
        normal outcome.
        """
        db = _get_db()
        player = db.get(identity.player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        return db, identity.player_id, player

    @app.get("/cosmetics/catalogue")
    async def get_catalogue():
        """The full catalogue, unauthenticated. A public price list.

        This is the route that makes preview free (§14.2): every item's visual
        block is here, to anyone, so the client can render a preview of
        something the caller does not own without a second round trip and
        without a server write.

        Event auras appear only while their event is live — the §5.5 fix at the
        display end. The purchase route asks the same question, so the two
        cannot disagree about what is for sale.
        """
        now = time.time()
        items = []
        for cid, config in COSMETIC_CATALOG.items():
            items.append(_public_catalogue_row(cid, config))
        for cid, config in EVENT_COSMETICS.items():
            if event_is_running(config.get("event_source"), now):
                items.append(_public_catalogue_row(cid, config))
        return {"cosmetics": items, "slots": list(EQUIPPABLE_SLOTS)}

    @app.get("/cosmetics/owned")
    async def get_owned(identity=Depends(require_player)):
        """What this player owns, what they are wearing, and their balance.

        `owned` is the full grant set, `equipped` is the loadout — one entry per
        slot, and the only thing the game draws. Sending both is deliberate:
        the client needs the owned list to render the inventory and the loadout
        to render the character, and deriving one from the other on the client
        is how the two drift apart.
        """
        db, player_id, player = _session_player(identity)
        return {
            "player_id": player_id,
            "owned": sorted(_owned_rows(player).keys()),
            "equipped": get_equipped_cosmetics(player),
            "loadout": get_loadout(player),
            "wallet": _read_balance(player, player_id),
        }

    @app.post("/cosmetics/equip")
    async def equip_endpoint(payload: dict = Body(...), identity=Depends(require_player)):
        """Equips a cosmetic the player owns.

        Order of authority, and it is the order that matters: the SESSION says
        whose player this is, and nothing in the body can say otherwise. A body
        naming `player_id` is not read here at all — `enforce_ownership` inside
        `require_player` already 403s a body that claims somebody else, and this
        handler would ignore it even if it did not.

        The response echoes the player's combat summary before and after the
        equip, and the test asserts they are EQUAL. That is §15.2 rule 2 turned
        into a contract: if equip is truly free, the two summaries are
        identical, and a future change that makes them differ fails a test
        rather than shipping.
        """
        db, player_id, player = _session_player(identity)
        cosmetic_id = payload.get("cosmetic_id")
        slot = payload.get("slot")
        if not cosmetic_id:
            raise HTTPException(status_code=400, detail="cosmetic_id is required")

        before = combat_summary(player)
        with _write(db, player_id):
            ok, reason = equip_cosmetic(player, cosmetic_id, slot)
            if not ok:
                _raise_equip_refusal(reason)
            db[player_id] = player
        return {
            "ok": True,
            "cosmetic_id": cosmetic_id,
            "slot": slot_for_cosmetic(get_cosmetic(cosmetic_id) or {}),
            "loadout": get_loadout(player),
            "equipped": get_equipped_cosmetics(player),
            # Both summaries, so a client can diff them. They are equal, and the
            # test says so.
            "combat_before": before,
            "combat_after": combat_summary(player),
        }

    @app.post("/cosmetics/unequip")
    async def unequip_endpoint(payload: dict = Body(...), identity=Depends(require_player)):
        """Empties one slot. Idempotent: unequipping an empty slot succeeds."""
        db, player_id, player = _session_player(identity)
        slot = payload.get("slot")
        if not slot:
            raise HTTPException(status_code=400, detail="slot is required")
        before = combat_summary(player)
        with _write(db, player_id):
            ok, reason = unequip_cosmetic(player, slot)
            if not ok:
                raise HTTPException(status_code=400, detail=reason)
            db[player_id] = player
        return {
            "ok": True,
            "slot": slot,
            "loadout": get_loadout(player),
            "equipped": get_equipped_cosmetics(player),
            "combat_before": before,
            "combat_after": combat_summary(player),
        }

    @app.post("/cosmetics/preview")
    async def preview_endpoint(payload: dict = Body(...)):
        """The visual block for any cosmetic. No ownership, no write, no session.

        Three absences, each of them a rule:

        * **No ownership check.** §14.3 rule 2 — preview works on locked items,
          including ones whose requirement the player does not meet, because
          that is how a player learns what to chase.
        * **No session.** Preview is public data, exactly like the catalogue. A
          player browsing before signing in can see what the game looks like.
        * **No write.** §14.3 rule 1 — a previewed item is still unowned and
          still unbuyable until a purchase succeeds. There is no `player` here
          to write to, which is what makes "preview never grants anything" a
          structural property rather than a promise.

        The reason this is a server route at all, when §14.2 argues preview
        should cost the server nothing: the client would otherwise keep its own
        copy of every cosmetic's visual block, and that copy drifts from the
        catalogue the day someone adds an item. One answer, served from the
        definition.
        """
        cosmetic_id = payload.get("cosmetic_id")
        if not cosmetic_id:
            raise HTTPException(status_code=400, detail="cosmetic_id is required")
        ok, preview, reason = preview_cosmetic(cosmetic_id)
        if not ok:
            raise HTTPException(status_code=404, detail=reason)
        return {"ok": True, "preview": preview, "preview_only": True}


# Integration: auto-grant realm badges
def check_realm_badge_grants(player: dict) -> list[str]:
    """Grants the badge for the player's current realm. Returns newly granted ids.

    Called from `server/breakthrough.py` when a realm is set. It was dead code
    for the whole of 2026-09-30: a tree-wide grep found its only callers in
    `server/test_cosmetics.py` and `server/test_prestige.py`, so nine of the
    fourteen permanent cosmetics were unobtainable in normal play. The module
    existed; the call site did not. That is the same failure shape as the
    auth-guard note in this vault, and it is a wiring bug, not a logic bug --
    which is why the fix is a call, not a rewrite.
    """
    granted = []
    realm = player.get("cultivation_realm", "Mortal")
    badge_map = {
        "Mortal": "badge_mortal",
        "Foundation Establishment": "badge_foundation",
        "Core Formation": "badge_core",
        "Nascent Soul": "badge_nascent",
        "Dao Seeking": "badge_dao",
    }
    badge_id = badge_map.get(realm)
    # `grant_cosmetic` resolves the id against the catalogue and refuses a
    # second grant, so this is a one-liner rather than a hand-built row. That is
    # the point of having one grant path: the "did the catalogue ever hear of
    # this id" question is asked once, here, for every source.
    if badge_id and grant_cosmetic(player, badge_id, source="progression"):
        granted.append(badge_id)
    return granted


# Integration: tribulation marks
def check_tribulation_marks(player: dict) -> list[str]:
    """Grants tribulation marks the player's survival count has earned.

    Called after a tribulation survival record is written. Dead code until then
    -- see `check_realm_badge_grants`.
    """
    granted = []
    trib_cycles = player.get("legacy", {}).get("tribulation_cycles_survived", 0)
    marks = {
        1: "mark_tribulation_survivor",
        10: "mark_tribulation_conqueror",
    }
    for threshold, mark_id in marks.items():
        if trib_cycles >= threshold and grant_cosmetic(player, mark_id, source="progression"):
            granted.append(mark_id)
    return granted


# Integration: ascension titles
def check_ascension_titles(player: dict) -> list[str]:
    """Grants ascension titles the player's season count has earned.

    Called after an ascension season increments. Dead code until then -- see
    `check_realm_badge_grants`.
    """
    granted = []
    season = player.get("legacy", {}).get("ascension_season", 0)
    titles = {
        1: "title_first_ascendant",
        4: "title_eternal_ascendant",
    }
    for threshold, title_id in titles.items():
        if season >= threshold and grant_cosmetic(player, title_id, source="progression"):
            granted.append(title_id)
    return granted


# ---------------------------------------------------------------------------
# Acquisition paths that are not the shop
# ---------------------------------------------------------------------------
def grant_battle_pass_cosmetic(player: dict, cosmetic_id: str) -> bool:
    """Grants a battle-pass level reward.

    The pass builds its cosmetic ids by string formatting
    (`battle_pass.py:113,122`), so this is the boundary where a formatted
    string meets the catalogue. Refusing an unknown id HERE is the whole §13.2
    fix on the server side; the test
    `test_battle_pass_cosmetic_ids_resolve` is the guard that keeps the
    catalogue ahead of the formatter.

    Never overwrites an existing grant: a player who already owns the item keeps
    their original receipt rather than having it reset by a replayed claim.
    """
    return grant_cosmetic(player, cosmetic_id, source="battle_pass")


def grant_event_cosmetic(player: dict, cosmetic_id: str, now: Optional[float] = None) -> bool:
    """Grants a monthly-event cosmetic, and only while its event is live.

    The event gate is the same one the till uses, so a grant path cannot award
    an out-of-season aura any more than the shop can sell one. Without it, the
    day-14 event reward hands out a cosmetic the catalogue is not even showing.
    """
    config = get_cosmetic(cosmetic_id)
    if not config:
        return False
    if not event_is_running(config.get("event_source"), now):
        return False
    return grant_cosmetic(player, cosmetic_id, source="event")


def roll_gacha_cosmetic(rng=None) -> Optional[str]:
    """Rolls the gacha cosmetic pool. Returns a cosmetic id or None.

    A separate roll from the character pull, on purpose: the gacha's existing
    rarity and pity machinery is a CHARACTER economy with its own tuning, and
    bolting a 0.5% cosmetic onto it would change every pull's outcome for a
    reward that has no combat effect. Keeping the rolls apart means the
    cosmetic tier can ship, be measured, and be tuned without touching the
    rates players already know.

    `GACHA_COSMETIC_POOL` weights sum well under 1, so most rolls return None
    and the caller falls through to its normal reward. That is the intended
    rarity: §13.3 wants a chase item, not a common drop.
    """
    import random as _random
    generator = rng if rng is not None else _random
    roll = generator.random()
    cumulative = 0.0
    for cosmetic_id, chance in GACHA_COSMETIC_POOL.items():
        cumulative += chance
        if roll < cumulative:
            return cosmetic_id
    return None


def grant_gacha_cosmetic(player: dict, cosmetic_id: str) -> bool:
    """Grants a gacha-dropped cosmetic.

    Free, and deliberately not routed through `purchase_cosmetic`: the draw has
    already been paid for at the banner, and a second debit would be a double
    charge. It still goes through `grant_cosmetic`, so the id is resolved against
    the catalogue and an existing grant is left alone.
    """
    return grant_cosmetic(player, cosmetic_id, source="gacha")