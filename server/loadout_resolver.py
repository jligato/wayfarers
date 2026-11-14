"""The ONE function that reads both catalogues and produces a stat block.

Why this module exists
----------------------
Equipment (``server/equipment.py``) and artifacts (``server/artifacts.py``)
both make a character stronger and both use the same five multiplier names.
Before this file, nothing combined them and no rule said which won. Whatever
code was written first would have invented the balance, and a second place
would have invented it differently.

The boundary, from ``design/artifact-equipment-boundary-design.md``:

* **equipment** is what you WEAR and IMPROVE. It is slotted, it wears out, it
  can be bought or forged, and its contribution is flat numbers summed across a
  loadout plus set-bonus percentages.
* **artifacts** are what you FIND and SWITCH ON. They are not slotted, cannot
  be improved or repaired, and their whole value is being permanent and rare.

**The two layers MULTIPLY. They never add.**

    final_multiplier = equipment_multiplier x artifact_multiplier

Add them and two 10% bonuses become 20%, a hidden doubling. Multiply and they
become 21%, which is legible and which a player can predict. This function is
the only place that rule is applied, so it can be tested once.

This module is the choke point the design asks for: nothing else in the server
is supposed to read ``EQUIPMENT_CATALOG`` or ``ARTIFACT_CATALOG`` to work out a
number that reaches a fight.

Anti-abuse
----------
Everything in here is derived from the player's own saved state and the two
server-side catalogues. There is no parameter and no request field that lets a
caller supply a magnitude. A client may name WHICH artifact is switched on; the
how-much is always a number in this repository.
"""
from __future__ import annotations

import math
import os
from typing import Any

import artifacts
import equipment

#: The five stats a loadout can move. Names match the two catalogues'
#: ``*_mult`` vocabulary (``atk_mult`` and friends) on the multiplier side and
#: equipment's ``STAT_KEYS`` on the flat side, so one block carries both.
STAT_KEYS: tuple[str, ...] = equipment.STAT_KEYS

#: Multiplier name per flat stat name. ``def_`` is the storage spelling
#: (``def`` is a Python keyword); see ``combat_config.stat_def``.
MULTIPLIER_KEYS: dict[str, str] = {
    "atk": "atk_mult",
    "def_": "def_mult",
    "hp": "hp_mult",
    "mp": "mp_mult",
    "spd": "spd_mult",
}

#: Environment flag. Off by default: turning the resolver on changes real fight
#: outcomes for every player, which is a balance decision and not a refactor.
#: The resolver itself is always available and always testable; only its effect
#: on the combat simulator is gated.
FEATURE_FLAG = "WC_LOADOUT_RESOLVER"


def resolver_enabled() -> bool:
    """True when the resolver is allowed to shape a simulated fight."""
    return os.getenv(FEATURE_FLAG) == "1"


def _coerce_multiplier(raw: Any) -> float | None:
    """Convert one multiplier, or return ``None`` if it is not usable.

    The single guard for the multiplier side, shared by every reader of a
    multiplier in this module. It exists because a multiplier reaches a fight
    by multiplication, and multiplication does not complain about the value it
    is given:

    * ``None`` raised ``TypeError`` and a non-numeric string raised
      ``ValueError`` -- a raw crash in the middle of a stat block;
    * the string ``"not a number"`` could not be read but ``nan`` and ``inf``
      could, and either one silently corrupts every number derived from it;
    * ``True`` read as the number ``1.0``, so a caller-supplied boolean looked
      like a deliberate no-buff multiplier instead of a data bug.

    So: booleans are refused (they are not rates), and the result must be a
    FINITE float. ``nan``/``inf`` are refused for the same reason the stat
    guard exists -- a number nobody can reason about is not a usable input.

    Returns ``None`` rather than raising, so each caller applies the SAME
    documented consequence it already applies to an unusable stat value:
    skip that one stat, leave the rest of the block intact.
    """
    return _coerce_number(raw)


def _coerce_number(raw: Any) -> float | None:
    """Convert one stored or caller-supplied number, or ``None`` if unusable.

    The shared guard for EVERY numeric conversion in this module, on either
    side of the boundary. ``_coerce_multiplier`` delegates here so the
    multiplier rule cannot drift from the stat rule.

    It exists because ``float()`` fails in three different ways and only one of
    them raises:

    * ``None`` raises ``TypeError`` and a non-numeric string raises
      ``ValueError`` -- a raw crash in the middle of a stat block;
    * the string ``"not a number"`` cannot be read, but ``nan`` and ``inf``
      can be, and either one silently corrupts every number derived from it;
    * a whole number too large for a float (``10 ** 400``) raises
      ``OverflowError``, which a guard written before that case existed does
      not catch -- and a reviewer testing only with infinity would conclude
      this site was never broken;
    * ``True`` reads as the number ``1.0``, so a boolean looks like a
      deliberate value instead of a data bug.

    So booleans are refused (they are not rates), all three exceptions are
    caught, and the result must be a FINITE float.

    Returns ``None`` rather than raising, so every caller applies the SAME
    documented consequence it already applies to an unusable value: skip that
    one number, leave the rest of the block intact.
    """
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return value


def identity_multipliers() -> dict[str, float]:
    """The neutral block: every multiplier is 1.0."""
    return {name: 1.0 for name in MULTIPLIER_KEYS.values()}


def equipment_multipliers(player: dict, player_id: str | None = None) -> dict[str, float]:
    """The equipment layer's percentages, from set bonuses on worn pieces.

    Flat numbers are NOT here: they are summed by :func:`equipment_flat_stats`.
    This returns only what equipment contributes as a percentage, which is the
    set-bonus ladder in ``equipment._set_bonuses``.
    """
    state = equipment.ensure_state(player, player_id)
    _active_sets, multipliers = equipment._set_bonuses(state)
    merged = identity_multipliers()
    for name, value in (multipliers or {}).items():
        if name not in merged:
            continue
        coerced = _coerce_multiplier(value)
        if coerced is None:
            # One unusable set-bonus percentage leaves that ONE multiplier at
            # identity. It does not take the whole layer down with it, and it
            # does not become a plausible-looking number.
            continue
        merged[name] = coerced
    return merged


def equipment_flat_stats(player: dict, player_id: str | None = None) -> dict[str, float]:
    """The equipment layer's flat numbers: the worn items' stats, summed.

    Summed inside the layer, because equipment owns the additive rule: five
    swords do not each multiply the character by 1.2, they add their numbers
    together the way a real loadout does. Between the layers is where the
    multiplication happens, and only in :func:`resolve_stat_block`.
    """
    state = equipment.ensure_state(player, player_id)
    totals = {stat: 0.0 for stat in STAT_KEYS}
    for item in equipment._equipped_items(state):
        try:
            view = equipment._item_view(item)
        except Exception:
            # An item id no longer in the catalogue is skipped rather than
            # allowed to take a whole player's stat block down with it.
            continue
        for stat, value in (view.get("stats") or {}).items():
            if stat not in totals:
                continue
            amount = _coerce_number(value)
            if amount is None:
                # One unusable catalogue value leaves that ONE stat at 0.0. It
                # does not take the whole player's stat block down with it,
                # and it does not become a plausible-looking number.
                continue
            running = totals[stat] + amount
            if not math.isfinite(running):
                # Finite inputs whose SUM overflows (1e308 + 1e308 == inf).
                # Stop at the last good total rather than publishing an inf.
                continue
            totals[stat] = running
    return totals


def artifact_multipliers(player: dict, player_id: str | None = None) -> dict[str, float]:
    """The artifact layer's percentages, from the artifacts switched on.

    Reads the artifact state through its own ``ensure_state`` so the cap and
    the v2 shape are applied on the way past, then uses the module's existing
    ``_derive`` so there is exactly one place that turns ``activated`` flags
    into multipliers.
    """
    state = artifacts.ensure_state(player, player_id)
    derived = artifacts._derive(state)
    merged = identity_multipliers()
    for name, value in (derived.get("multipliers") or {}).items():
        if name not in merged:
            continue
        coerced = _coerce_multiplier(value)
        if coerced is None:
            # Same rule as the equipment layer above: skip this ONE multiplier,
            # leave the other four at identity, never invent a number.
            continue
        merged[name] = coerced
    return merged


def combine(equipment_mult: dict[str, float], artifact_mult: dict[str, float]) -> dict[str, float]:
    """Multiply the two layers together, stat by stat.

    The whole point of the module, isolated so a test can prove the rule
    without a player, a store, or a catalogue.

    Deliberately UNROUNDED. A multiplier is a ratio, not a displayed stat,
    and rounding it here would break the one invariant the block exists to
    show: ``multipliers[k] == equipment_multipliers[k] * artifact_multipliers[k]``
    exactly. Do not add a ``round()`` here to tidy the output -- it was tried
    and it turns that identity into an inequality.

    An UNUSABLE multiplier on either side (``None``, a non-numeric string, a
    boolean, ``nan`` or ``inf``) is treated as IDENTITY ``1.0`` for that one
    key -- the same answer a MISSING key gets. It used to raise a raw
    ``TypeError``/``ValueError`` out of the middle of a stat block, which is
    the worst place for a crash to land. Refusing it here means the other four
    stats still resolve, and the bad one reads as "no buff" rather than as a
    corrupt number. See :func:`_coerce_multiplier`.
    """

    combined = identity_multipliers()
    for stat_key, mult_key in MULTIPLIER_KEYS.items():
        equipment_side = _coerce_multiplier(equipment_mult.get(mult_key, 1.0))
        artifact_side = _coerce_multiplier(artifact_mult.get(mult_key, 1.0))
        product = (1.0 if equipment_side is None else equipment_side) * (
            1.0 if artifact_side is None else artifact_side
        )
        if not math.isfinite(product):
            # Each side passed its own guard, but two FINITE numbers can still
            # multiply out to inf (1e308 x 1e308). Publishing that inf is the
            # same defect as accepting one, so this key falls back to identity
            # and the other four still resolve.
            product = 1.0
        combined[mult_key] = product
    return combined


def resolve_stat_block(player: dict, player_id: str | None = None) -> dict[str, Any]:
    """Read both catalogues once and return one stat block for a fight.

    Returns::

        {
          "flat":   {"atk": 42.0, "def_": 30.0, "hp": 640.0, "mp": 300.0, "spd": 18.0},
          "equipment_multipliers": {...},   # the equipment layer alone
          "artifact_multipliers":  {...},   # the artifact layer alone
          "multipliers":           {...},   # the PRODUCT of the two
          "final":                 {...},   # flat scaled by the product
        }

    ``final`` is what a combat simulator should read. It keeps ``flat`` and the
    two layers in the block so a test, or a future tooltip, can show the working
    out rather than just the answer.

    Neither ``player_id`` nor anything else here is client-supplied authority:
    it is used only to stamp/verify binding, and passing ``None`` is the
    read-path default that never re-homes a bound item.
    """
    equip_mult = equipment_multipliers(player, player_id)
    artifact_mult = artifact_multipliers(player, player_id)
    combined = combine(equip_mult, artifact_mult)
    # Round the flat layer ONCE and derive `final` from the rounded value.
    #
    # Deriving it from the raw sum instead makes the returned block
    # self-inconsistent: `flat` and `multipliers` are both printed to 4 dp, so
    # a caller (or a tooltip, or this module's own test) that recomputes
    # `flat * multiplier` gets 44.20434816 while `final` says 44.2043. The
    # block is supposed to show its own working out; it can only do that if
    # every number in it is derived from the numbers printed beside it.
    flat = {stat: round(equipment_flat_stats(player, player_id)[stat], 4) for stat in STAT_KEYS}
    final = {
        stat: round(flat[stat] * combined[MULTIPLIER_KEYS[stat]], 4) for stat in STAT_KEYS
    }
    return {
        "flat": flat,
        "equipment_multipliers": equip_mult,
        "artifact_multipliers": artifact_mult,
        "multipliers": combined,
        "final": final,
    }


def apply_to_stat_line(stat_line: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """Scale a simulator's stat line by the resolver's product.

    Takes the stat line the simulator already works from and returns a NEW dict
    with each loadout-owned stat scaled by the combined multiplier. Nothing else
    in the line is touched: ``realm``, ``stage``, ``element`` and ``aura`` are
    combat-config inputs, not equipment, and multiplying them would silently
    change elemental and realm balance.

    The input dict is never mutated. A caller that ignores the return value gets
    the un-resolved line, not a half-resolved one.

    BAD DATA ON EITHER SIDE IS SKIPPED, NEVER RAISED. The stat value and the
    multiplier are guarded by the SAME rule, because a stat line is built once
    and fed straight into a fight: a multiplier of ``None`` used to raise a raw
    ``TypeError`` and a non-numeric string a raw ``ValueError`` in the middle of
    the calculation, and ``nan``/``inf`` were accepted and silently corrupted
    every number derived from them. Now an unusable value on either side leaves
    that ONE stat untouched and the other four still scale. See
    :func:`_coerce_multiplier`.
    """
    combined = (block or {}).get("multipliers") or identity_multipliers()
    out = dict(stat_line or {})
    for stat_key, mult_key in MULTIPLIER_KEYS.items():
        if stat_key not in out:
            continue
        base = _coerce_number(out[stat_key])
        if base is None:
            # Bad stat, same rule as a bad multiplier below: leave this ONE
            # stat exactly as the caller gave it and keep scaling the rest.
            continue
        multiplier = _coerce_multiplier(combined.get(mult_key, 1.0))
        if multiplier is None:
            # Bad multiplier: leave this stat exactly as the caller gave it
            # rather than multiplying by a guess or dropping the whole line.
            continue
        scaled = base * multiplier
        if not math.isfinite(scaled):
            # Two finite numbers can still multiply out to inf. Publishing
            # that inf is the same defect as accepting one, just one step
            # later, so it is refused at the point it appears.
            continue
        out[stat_key] = scaled
    return out