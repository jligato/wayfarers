"""
Monster stat table for Wayfarer's Cycle.

Fills the gap between the monster NAMES the content already declares
(``shared/zones/zones.json`` ``features.combat_encounters`` and the floor
``enemies``/``boss`` lists in the templates built by
``server/dungeons.py:_create_default_templates``) and the actual NUMBERS a fight
needs. Until this module existed, every monster name in the game resolved to
nothing: there was no hp, no attack, no defence anywhere in the repository.

Loading mirrors ``server/zones.py``: a JSON file beside the other shared data,
read at construction, exposed through a small manager class with
``get_*``/``get_all_*`` accessors. The numbers themselves are not invented here
-- they are read from ``shared/monsters/monsters.json``, whose ``derivation``
block names the source of every multiplier.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional


class MonsterStatError(KeyError):
    """Raised when a name is asked for that the table does not carry."""


def _finite_or_none(value: Any) -> Optional[float]:
    """``value`` as a finite float, or None if it cannot be one.

    Two different shapes have to be refused and only ONE of them raises inside
    the conversion, so a bare ``except`` is not enough:

      * ``float(inf)`` and ``float(nan)`` convert WITHOUT raising. A wider
        except clause cannot see them, so the explicit ``math.isfinite`` test is
        the only defence. Returning either one is worse than refusing: both
        poison every later sum, and FastAPI's JSON encoder turns them into a
        500 that names no field ("Out of range float values are not JSON
        compliant").
      * ``float(10 ** 400)`` raises ``OverflowError``, which is neither
        ``TypeError`` nor ``ValueError``.

    The finiteness test lives AFTER a successful ``float()`` rather than before
    it, because ``math.isfinite(10 ** 400)`` raises ``OverflowError`` on its own.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class MonsterStatTable:
    """One monster's resolved stat line."""

    __slots__ = (
        "id", "name", "realm", "element", "rarity", "stage", "tier",
        "is_boss", "hp", "atk", "defense", "mp", "spd",
        "spirit_stone", "xp", "reachable_realms",
    )

    #: Every field a data-file row must carry. Checked in one place so a
    #: malformed row names the monster that broke; a bare ``KeyError('def')``
    #: on a 58-row table is not actionable at boot.
    REQUIRED_FIELDS = (
        "name", "realm", "element", "rarity", "stage", "tier", "is_boss",
        "hp", "atk", "def", "mp", "spd", "spirit_stone", "xp",
    )

    def __init__(self, monster_id: str, data: Dict[str, Any]):
        missing = [f for f in self.REQUIRED_FIELDS if f not in data]
        if missing:
            raise MonsterStatError(
                f"monster {monster_id!r} is missing required field(s): "
                + ", ".join(missing)
            )
        self.id = monster_id
        self.name = data["name"]
        self.realm = data["realm"]
        self.element = data["element"]
        self.rarity = data["rarity"]
        self.stage = data["stage"]
        self.tier = data["tier"]
        self.is_boss = bool(data["is_boss"])
        # A non-numeric stat is the same class of content bug as a MISSING
        # field, so it is reported the same way: named by row. A bare
        # ``ValueError: invalid literal for int()`` from a 58-row table does
        # not say which monster is broken, which field is bad, or what the bad
        # value was -- the same reason REQUIRED_FIELDS is checked above.
        #
        # The except tuple needs OverflowError as well as TypeError and
        # ValueError: ``int(float("inf"))`` raises OverflowError, which is
        # neither of the other two, so an Infinity in the data file escaped
        # this handler and aborted the whole load with a bare traceback.
        for field, attribute in (
            ("hp", "hp"), ("atk", "atk"), ("def", "defense"),
            ("mp", "mp"), ("spd", "spd"),
            ("spirit_stone", "spirit_stone"), ("xp", "xp"),
        ):
            raw = data[field]
            if _finite_or_none(raw) is None:
                raise MonsterStatError(
                    f"monster {monster_id!r} has a non-numeric {field!r}: "
                    f"{raw!r} (not a finite number)"
                ) from None
            try:
                value = int(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MonsterStatError(
                    f"monster {monster_id!r} has a non-numeric {field!r}: "
                    f"{raw!r} ({exc})"
                ) from None
            setattr(self, attribute, value)
        self.reachable_realms = list(data.get("reachable_realms", []))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "realm": self.realm,
            "element": self.element,
            "rarity": self.rarity,
            "stage": self.stage,
            "tier": self.tier,
            "is_boss": self.is_boss,
            "hp": self.hp,
            "atk": self.atk,
            "def": self.defense,
            "mp": self.mp,
            "spd": self.spd,
            "spirit_stone": self.spirit_stone,
            "xp": self.xp,
            "reachable_realms": list(self.reachable_realms),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MonsterStatTable {self.id} hp={self.hp} atk={self.atk}>"


class MonsterStatManager:
    """Loads and serves the monster stat table."""

    def __init__(self, monsters_path: Optional[str] = None):
        self.monsters: Dict[str, MonsterStatTable] = {}
        self.realm_multipliers: Dict[str, float] = {}
        self.derivation: Dict[str, Any] = {}
        self._load_monsters(monsters_path)

    def _load_monsters(self, monsters_path: Optional[str] = None) -> None:
        if monsters_path is None:
            monsters_path = Path(__file__).parent.parent / "shared" / "monsters" / "monsters.json"
        with open(monsters_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # REPLACE, never merge. Loading into the live dict left rows that had
        # been deleted from the data file alive in the manager: a 58-row table
        # that reloaded a one-row file still answered has_monster() for the
        # other 58 names.
        self.monsters = {}
        self.realm_multipliers = dict(data.get("realm_multipliers", {}))
        self.derivation = dict(data.get("derivation", {}))
        for monster_id, monster_data in data.get("monsters", {}).items():
            try:
                self.monsters[monster_id] = MonsterStatTable(monster_id, monster_data)
            except MonsterStatError as exc:
                raise MonsterStatError(
                    f"{exc} (while loading {monsters_path})"
                ) from None

    def get_monster(self, monster_id: str) -> Optional[MonsterStatTable]:
        """The stat line for this name, or None if the table does not carry it."""
        return self.monsters.get(monster_id)

    def require_monster(self, monster_id: str) -> MonsterStatTable:
        """Same, but loud. Used where a missing row is a content bug."""
        table = self.monsters.get(monster_id)
        if table is None:
            raise MonsterStatError(monster_id)
        return table

    def has_monster(self, monster_id: str) -> bool:
        return monster_id in self.monsters

    def get_all_monsters(self) -> List[MonsterStatTable]:
        return list(self.monsters.values())

    def get_monster_ids(self) -> List[str]:
        return list(self.monsters.keys())

    def get_bosses(self) -> List[MonsterStatTable]:
        return [m for m in self.monsters.values() if m.is_boss]

    def get_by_realm(self, realm: str) -> List[MonsterStatTable]:
        return [m for m in self.monsters.values() if m.realm == realm]

    def scaled_for_context(self, monster_id: str, context_realm: str) -> Dict[str, Any]:
        """Stat line re-based onto a different realm ladder.

        Used when a monster declared in one place is met somewhere harder (a
        zone encounter reused as a dungeon boss, say). The ratio between the two
        ladder positions is taken from the same ``realm_multipliers`` block that
        produced the base numbers, so a scaled line stays on the same curve
        rather than being re-invented.
        """
        table = self.require_monster(monster_id)
        # Every realm multiplier comes straight out of the data file, so each
        # one is UNTRUSTED: it can be Infinity, NaN, a zero that divides by
        # zero, or text that has no arithmetic at all. Measured on the shipped
        # module, each of these escaped as a raw exception out of a read-only
        # lookup: an Infinity target multiplier raised OverflowError inside
        # int(), a NaN target raised ValueError, and a text multiplier raised
        # TypeError on the division itself. So the ladder is converted and
        # tested for finiteness BEFORE it is used as arithmetic, and a refused
        # multiplier is treated exactly like a missing one: the authored line
        # comes back, MARKED, instead of a traceback.
        base_mult = _finite_or_none(self.realm_multipliers.get(table.realm))
        target_mult = _finite_or_none(self.realm_multipliers.get(context_realm))
        if base_mult is None or target_mult is None or base_mult <= 0 or target_mult <= 0:
            # Unknown realm on either side, or a multiplier that is not a usable
            # positive number: return the authored line unchanged rather than
            # inventing a scale factor. It is still MARKED, so a caller can tell
            # "scaled onto X" from "scaling refused" -- the previous shape
            # omitted the key entirely and the two were indistinguishable.
            out = table.to_dict()
            out["scaled_from_realm"] = table.realm
            out["scaled_onto_realm"] = context_realm
            out["scaling_refused"] = True
            return out
        ratio = target_mult / base_mult
        out = table.to_dict()
        for field in ("hp", "atk", "def", "mp", "spd"):
            scaled = out[field] * ratio
            # Two finite numbers can still multiply to Infinity (a large stat
            # against a large ratio), and int() of that raises OverflowError
            # naming no field. Measured. The floor is the deliberate answer
            # here, not a silent one: an unrepresentable stat is "cannot be
            # fought", which is the same failure the floor already prevents,
            # and returning 1 keeps the line JSON-encodable so FastAPI's
            # encoder cannot turn this read into a fieldless 500.
            #
            # int() truncates TOWARD ZERO, so a small stat scaled down a long
            # way lands on 0 -- a monster with 0 hp cannot be fought and one
            # with 0 atk cannot fight. The floor is unconditional, not "only
            # when the shipped table happens to stay above it".
            out[field] = max(1, int(scaled)) if math.isfinite(scaled) else 1
        out["scaled_from_realm"] = table.realm
        out["scaled_onto_realm"] = context_realm
        out["scaling_refused"] = False
        return out


_manager: Optional[MonsterStatManager] = None


def get_monster_stat_manager() -> MonsterStatManager:
    """Process-wide accessor, mirroring ``server/dungeons.py:get_dungeon_manager``."""
    global _manager
    if _manager is None:
        _manager = MonsterStatManager()
    return _manager


def reload_monsters() -> None:
    """Drop the cached manager so the next access re-reads the JSON."""
    global _manager
    _manager = None
