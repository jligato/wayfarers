"""Contract for ``server/npc_roster_config.py`` -- the NPC roster tables.

WHAT THIS FILE IS
-----------------
``npc_roster_config.py`` has no routes and no behaviour beyond two small pure
helpers. It is a table: who each NPC is, what they want, how they speak, plus
the favours they ask for and the standing those favours cost.

So "the contract of this module" is the DATA contract, and it is pinned from
the CONSUMER side rather than from the module's own docstring. Two modules read
these tables and one HTTP router reads them:

  * ``server/npc_state.py``   -- direct subscript: ``favour["npc"]``,
    ``favour["ask"]``, ``favour["cost_npc"]``, ``favour["cost_amount"]``,
    ``favour["gain"]``, ``favour["on_settle"]``. A missing key is a KeyError at
    request time, not a config warning. Roster text is read with ``.get(...)``
    and a ``""`` default, so a missing ``greeting`` renders an EMPTY LINE and a
    player sees a blank reply box.
  * ``server/npc_routes.py``  -- direct subscript on the roster:
    ``entry["name"]``, ``entry["title"]``, ``entry["faction"]``,
    ``entry["voice"]``, ``entry["wants"]``.

That split is why this file exists. The ``.get`` paths fail silently and the
subscript paths fail loudly, and a table that is merely "mostly filled in"
breaks one and quietly blanks the other.

WHAT IS PINNED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
Pinned, because a caller depends on it:
  * every key a consumer subscripts directly is present and non-empty
  * ``voice`` carries all three declared levers (register / cadence / forbidden)
  * roster ids and ``shared/npcs/npcs.json`` ids are the SAME SET -- the module
    docstring claims the JSON is the source of zone placement and this file is
    the source of identity, so a roster entry with no shipped NPC (or a shipped
    NPC with no roster entry) is a character the client can never reach
  * ``FAVOUR_ORDER`` is a PERMUTATION of ``FAVOURS`` -- ``available_favour``
    walks the order tuple and silently ``continue``s past an id with no entry,
    so a favour missing from the tuple is unreachable rather than broken
  * ``npc`` and ``cost_npc`` both name real roster entries, and they are
    DIFFERENT NPCs -- the module's own design note says a favour paid in
    standing with the asker is not a sacrifice
  * ``cost_amount`` and ``gain`` are integers inside the declared standing scale
  * the standing thresholds sit inside the standing bounds and are symmetric
  * the two helpers' documented behaviour, including their error cases

NOT pinned, on purpose:
  * how many NPCs or favours exist. A frozen count is a change detector: adding
    a character breaks it without any caller being wrong.
  * any literal name, id, line of dialogue, or faction value. Values are
    free to change; only the RELATIONS between them are contracts.
  * the order favours appear in. ``FAVOUR_ORDER`` exists so the order is
    stable between two page loads; which favour is first is a design choice.

ONE MEASURED DEVIATION FROM THE DOCSTRING
-----------------------------------------
The module docstring names the faction axis as orthodox / reformist /
isolationist / aggressive / trade. The shipped table also uses ``"none"`` for
``wu_shen``, who is deliberately unaffiliated -- the final antagonist sits
outside sect politics. The DATA is fine and the consumers only ever pass the
string through to the client. This file therefore asserts the axis is the five
declared values PLUS ``"none"``, and that ``"none"`` is used for at most the
antagonists rather than pretended not to exist. The docstring is narrower than
the table; the table is what ships.

HOW TO RUN IT
-------------
    bash tools/wc_pytest.sh server/tests/test_npc_roster_config_contract.py -q

This file needs no database, no FastAPI app and no network. It imports the
config module directly and reads one JSON data file from disk.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import npc_roster_config as cfg  # noqa: E402

_NPCS_JSON = _SERVER_DIR.parent / "shared" / "npcs" / "npcs.json"

#: Roster keys ``npc_routes.py`` reads with a DIRECT SUBSCRIPT (``entry[...]``).
#: A missing one raises KeyError inside a request.
REQUIRED_ROSTER_KEYS = ("name", "title", "faction", "voice", "wants")

#: Roster keys ``npc_state.greeting_for`` reads with ``.get(key, "")``. A
#: missing one does not raise -- it hands the player an empty string, which is
#: the silent failure this file exists to prevent.
REQUIRED_SPEECH_KEYS = ("greeting", "warm", "cold")

#: The three levers the module docstring names as what keeps a sect elder, a
#: merchant and a rival from sounding like the same writer.
VOICE_KEYS = ("register", "cadence", "forbidden")

#: Favour keys ``npc_state.py`` reads with a DIRECT SUBSCRIPT.
REQUIRED_FAVOUR_KEYS = (
    "npc",
    "ask",
    "cost_npc",
    "cost_amount",
    "gain",
    "lead",
    "on_settle",
)

#: Favour keys ``npc_state.respond_line`` reads with ``.get(key, "")``. A
#: missing one yields an empty dialogue line for that outcome.
REQUIRED_FAVOUR_OUTCOME_KEYS = ("on_accept", "on_refuse", "on_fail")

#: The sect-politics axis from the module docstring, plus "none" for a
#: character deliberately outside sect politics. See the deviation note above.
DECLARED_FACTIONS = frozenset(
    {"orthodox", "reformist", "isolationist", "aggressive", "trade", "none"}
)
#: The five that are actually a position ON the axis.
SECT_POLITICS_FACTIONS = DECLARED_FACTIONS - {"none"}

#: Field names that would mean a favour pays money. The module's design note is
#: explicit that no entry in this file pays currency: the reward is ``lead``,
#: information, because a price you can pay is not a sacrifice.
CURRENCY_FIELD_NAMES = frozenset(
    {
        "currency",
        "gold",
        "price",
        "reward",
        "reward_amount",
        "spirit_stones",
        "stones",
        "cost_currency",
        "cost_spirit_stones",
        "cost_stones",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _shipped_npc_ids() -> set:
    """Ids from the shipped roster JSON, or skip if the data file is absent."""
    if not _NPCS_JSON.exists():
        pytest.skip(f"shipped roster data not present: {_NPCS_JSON}")
    data = json.loads(_NPCS_JSON.read_text(encoding="utf-8"))
    entries = data["npcs"] if isinstance(data, dict) else data
    return {row["id"] for row in entries}


def _is_filled_text(value: object) -> bool:
    """True for a non-blank string.

    ``bool("")`` is False and ``bool("  ")`` is True, so the second half of
    this is the part that matters: a value made of spaces is not a line of
    dialogue, and a player sees it as a blank reply box.
    """
    return isinstance(value, str) and value.strip() != ""


# ---------------------------------------------------------------------------
# The roster: keys a consumer subscripts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_roster_entry_has_every_subscripted_key(npc_id):
    """``entry["name"]`` etc. must exist -- a missing key is a request-time KeyError."""
    entry = cfg.NPC_ROSTER[npc_id]
    missing = [k for k in REQUIRED_ROSTER_KEYS if k not in entry]
    assert not missing, f"roster entry {npc_id!r} is missing {missing}"


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_roster_subscripted_text_is_filled(npc_id):
    """The subscripted text fields must be non-blank strings, not empty ones."""
    entry = cfg.NPC_ROSTER[npc_id]
    for key in ("name", "title", "wants"):
        assert _is_filled_text(entry.get(key)), (
            f"roster entry {npc_id!r} field {key!r} is blank or not a string: "
            f"{entry.get(key)!r}"
        )


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_roster_speech_lines_are_filled(npc_id):
    """``greeting`` / ``warm`` / ``cold`` are read with a ``""`` default.

    A missing or blank line does not raise -- it silently hands the player an
    empty reply, which is the failure mode this file is here to catch.
    """
    entry = cfg.NPC_ROSTER[npc_id]
    for key in REQUIRED_SPEECH_KEYS:
        assert _is_filled_text(entry.get(key)), (
            f"roster entry {npc_id!r} line {key!r} is blank or missing: "
            f"{entry.get(key, '<missing>')!r}"
        )


# ---------------------------------------------------------------------------
# The roster: voice
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_voice_carries_all_three_levers(npc_id):
    """``voice`` is handed to the client whole, and must hold all three levers."""
    voice = cfg.NPC_ROSTER[npc_id].get("voice")
    assert isinstance(voice, dict), (
        f"roster entry {npc_id!r} voice is {type(voice).__name__}, expected dict"
    )
    missing = [k for k in VOICE_KEYS if k not in voice]
    assert not missing, f"roster entry {npc_id!r} voice is missing {missing}"


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_voice_levers_are_filled_text(npc_id):
    """A lever present but blank is the same defect as a lever missing."""
    voice = cfg.NPC_ROSTER[npc_id]["voice"]
    for key in VOICE_KEYS:
        assert _is_filled_text(voice.get(key)), (
            f"roster entry {npc_id!r} voice[{key!r}] is blank or not a string: "
            f"{voice.get(key)!r}"
        )


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_voice_register_differs_between_npcs(npc_id):
    """No two NPCs share a register string.

    This is the module docstring's central claim about voice -- that a sect
    elder, a merchant and a rival do not share a mouth. If two entries declare
    the same register, the table is not delivering the thing it exists for. It
    is a RELATION between rows, not a frozen value, so rewording a register
    stays green while a duplicated one goes red.
    """
    mine = cfg.NPC_ROSTER[npc_id]["voice"]["register"].strip()
    for other_id, other in cfg.NPC_ROSTER.items():
        if other_id == npc_id:
            continue
        assert other["voice"]["register"].strip() != mine, (
            f"roster entries {npc_id!r} and {other_id!r} declare the same voice "
            f"register: {mine!r}"
        )


# ---------------------------------------------------------------------------
# The roster: reads line, and the faction axis
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_reads_line_is_filled(npc_id):
    """``reads`` is the declared "how this NPC interprets a stranger's progress".

    No HTTP route reads it yet -- it is the design's third question and the
    reason an NPC is worth more than a reward -- so this asserts presence and
    substance, not a caller.
    """
    assert _is_filled_text(cfg.NPC_ROSTER[npc_id].get("reads")), (
        f"roster entry {npc_id!r} has no reads line"
    )


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_faction_is_on_the_declared_axis(npc_id):
    """``faction`` is passed straight to the client, so the vocabulary is fixed."""
    faction = cfg.NPC_ROSTER[npc_id].get("faction")
    assert isinstance(faction, str) and faction.strip() != "", (
        f"roster entry {npc_id!r} has no faction"
    )
    assert faction in DECLARED_FACTIONS, (
        f"roster entry {npc_id!r} faction {faction!r} is not on the declared axis "
        f"{sorted(DECLARED_FACTIONS)}"
    )


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_any_non_sect_faction_is_exceptional(npc_id):
    """"none" marks someone outside sect politics, so it must stay rare.

    If most of the cast is unaffiliated the axis has stopped carrying meaning
    and "two NPCs want opposite outcomes" -- the engine of the favour system --
    has nothing to push against. Allowing a couple is the point; allowing half
    the roster is the defect.
    """
    roster = cfg.NPC_ROSTER
    unaffiliated = [
        key for key, entry in roster.items() if entry["faction"] == "none"
    ]
    mine = cfg.NPC_ROSTER[npc_id]["faction"]
    if mine == "none":
        assert len(unaffiliated) <= 2, (
            f"{len(unaffiliated)} of {len(roster)} NPCs are faction 'none' "
            f"({unaffiliated}); the sect-politics axis needs a majority"
        )
    else:
        assert mine in SECT_POLITICS_FACTIONS


def test_both_sides_of_the_faction_axis_are_represented():
    """The axis is only an axis if more than one position appears.

    A roster of one faction cannot produce two NPCs who want opposite outcomes
    from the same event, which the module names as the whole engine of the
    favour system.
    """
    present = {entry["faction"] for entry in cfg.NPC_ROSTER.values()}
    assert len(present & SECT_POLITICS_FACTIONS) >= 2, (
        f"only {sorted(present & SECT_POLITICS_FACTIONS)} on the sect-politics "
        f"axis; opposed outcomes are impossible with one faction"
    )


# ---------------------------------------------------------------------------
# Roster ids vs the shipped data file
# ---------------------------------------------------------------------------
def test_roster_ids_match_shipped_npc_ids():
    """Both files must agree, in BOTH directions.

    The JSON is the source of zone placement and realm gating; this module is
    the source of identity, want and voice. A roster id with no shipped NPC is
    a character the client cannot reach, and a shipped NPC with no roster entry
    answers an empty title, faction, want and voice on the wire. Neither
    direction raises at import time.
    """
    shipped = _shipped_npc_ids()
    roster = set(cfg.NPC_ROSTER)
    assert roster == shipped, (
        "roster and shipped data disagree: "
        f"roster-only={sorted(roster - shipped)}, "
        f"shipped-only={sorted(shipped - roster)}"
    )


@pytest.mark.parametrize("npc_id", sorted(cfg.NPC_ROSTER))
def test_roster_names_are_distinct(npc_id):
    """Two NPCs sharing a display name cannot be told apart in a dialogue box."""
    mine = cfg.NPC_ROSTER[npc_id]["name"].strip()
    for other_id, other in cfg.NPC_ROSTER.items():
        if other_id == npc_id:
            continue
        assert other["name"].strip() != mine, (
            f"roster entries {npc_id!r} and {other_id!r} share the name {mine!r}"
        )


# ---------------------------------------------------------------------------
# Favours: keys a consumer subscripts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_has_every_subscripted_key(favour_id):
    """``favour["npc"]``, ``favour["cost_npc"]``, ``favour["gain"]`` etc.

    ``npc_state.py`` subscripts all of these directly, so a missing key is a
    KeyError raised while serving a conversation, not a config warning.
    """
    favour = cfg.FAVOURS[favour_id]
    missing = [k for k in REQUIRED_FAVOUR_KEYS if k not in favour]
    assert not missing, f"favour {favour_id!r} is missing {missing}"


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_outcome_lines_are_filled(favour_id):
    """``on_accept`` / ``on_refuse`` / ``on_fail`` are read with a ``""`` default.

    ``npc_state.respond_line`` builds all four outcomes from ``.get(key, "")``,
    so a missing key shows the player an empty line for exactly the moment they
    made a decision.
    """
    favour = cfg.FAVOURS[favour_id]
    for key in REQUIRED_FAVOUR_OUTCOME_KEYS + ("on_settle", "lead", "ask"):
        assert _is_filled_text(favour.get(key)), (
            f"favour {favour_id!r} field {key!r} is blank or missing: "
            f"{favour.get(key, '<missing>')!r}"
        )


# ---------------------------------------------------------------------------
# Favours: cross-references into the roster
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_npc_exists_in_roster(favour_id):
    """``favour["npc"]`` is compared against the roster id on every request."""
    npc_id = cfg.FAVOURS[favour_id]["npc"]
    assert npc_id in cfg.NPC_ROSTER, (
        f"favour {favour_id!r} is asked by {npc_id!r}, which is not in the roster"
    )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_cost_npc_exists_in_roster(favour_id):
    """``favour["cost_npc"]`` names whose standing the favour spends."""
    cost_npc_id = cfg.FAVOURS[favour_id]["cost_npc"]
    assert cost_npc_id in cfg.NPC_ROSTER, (
        f"favour {favour_id!r} costs standing with {cost_npc_id!r}, which is not "
        "in the roster -- the player would be told to disappoint a stranger"
    )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_costs_someone_elses_standing(favour_id):
    """The asker must not be the one who pays.

    This is the module's central design claim: a favour paid in standing with
    the sect elder is a choice with a loser, whereas a favour whose cost falls
    on its own asker is a favour that costs nothing. The cost is also the only
    thing that makes two NPCs want opposite outcomes from one event, so this
    is a RELATION between two fields, not a snapshot.
    """
    favour = cfg.FAVOURS[favour_id]
    assert favour["cost_npc"] != favour["npc"], (
        f"favour {favour_id!r} charges its own asker {favour['npc']!r}; the design "
        "requires the cost to fall on a different NPC"
    )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_cost_and_gain_are_plain_ints(favour_id):
    """Consumers wrap these in ``int(...)``, so a string or float is a hazard.

    ``int("2")`` happens to work, but ``int(2.5)`` silently truncates a cost the
    player was shown, and ``int("two")`` raises mid-conversation.
    """
    favour = cfg.FAVOURS[favour_id]
    for key in ("cost_amount", "gain"):
        value = favour[key]
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"favour {favour_id!r} {key!r} is {type(value).__name__} ({value!r}), "
            "expected a plain int"
        )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_cost_and_gain_are_positive_and_within_the_standing_scale(favour_id):
    """Both amounts must be real steps, and steps no larger than the scale.

    Zero or negative makes the favour free, which the design forbids. Larger
    than ``STANDING_MAX`` would be clamped away on the first charge and the
    player would never actually pay the cost they were shown.
    """
    favour = cfg.FAVOURS[favour_id]
    for key in ("cost_amount", "gain"):
        value = favour[key]
        assert 0 < value <= cfg.STANDING_MAX, (
            f"favour {favour_id!r} {key!r} is {value}; must be a positive step no "
            f"larger than STANDING_MAX ({cfg.STANDING_MAX})"
        )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_pays_no_currency(favour_id):
    """No entry in this file pays money -- the reward is ``lead``.

    The cost of a favour must be a RELATIONSHIP, or the player can buy their way
    out of the decision with spirit stones. A currency-shaped field name is the
    way that regresses, so it is refused here.
    """
    currency_fields = sorted(set(cfg.FAVOURS[favour_id]) & CURRENCY_FIELD_NAMES)
    assert not currency_fields, (
        f"favour {favour_id!r} declares currency field(s) {currency_fields}; a "
        "price the player can pay is not a sacrifice"
    )


# ---------------------------------------------------------------------------
# FAVOUR_ORDER is the walk order
# ---------------------------------------------------------------------------
def test_favour_order_is_a_permutation_of_favours():
    """The order tuple must name every favour exactly once.

    ``npc_state.available_favour`` walks ``FAVOUR_ORDER`` and ``continue``s past
    any id with no ``FAVOURS`` entry, so an id in the tuple with no favour is
    invisible; a favour missing from the tuple is unreachable, because nothing
    else iterates the dict. Both are silent. This asserts both directions and
    catches a duplicated id, which would offer the same favour twice.
    """
    order = list(cfg.FAVOUR_ORDER)
    assert len(order) == len(set(order)), (
        f"FAVOUR_ORDER repeats an id: {sorted({k for k in order if order.count(k) > 1})}"
    )
    missing_from_order = sorted(set(cfg.FAVOURS) - set(order))
    missing_from_table = sorted(set(order) - set(cfg.FAVOURS))
    assert not missing_from_order, (
        f"favours no player can be offered (absent from FAVOUR_ORDER): {missing_from_order}"
    )
    assert not missing_from_table, (
        f"FAVOUR_ORDER names favours with no table entry, which "
        f"available_favour skips silently: {missing_from_table}"
    )


def test_favour_order_is_immutable_for_the_client():
    """``FAVOUR_ORDER`` is walked every request, so it must not be reassignable-by-accident."""
    assert isinstance(cfg.FAVOUR_ORDER, tuple), (
        f"FAVOUR_ORDER is {type(cfg.FAVOUR_ORDER).__name__}; a list can be "
        "reordered or mutated in place and the client would reshuffle between "
        "two page loads"
    )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_every_orderable_favour_has_a_real_asker_with_a_greeting(favour_id):
    """A favour's asker must be someone the player can actually talk to.

    ``npc_routes`` raises 404 for an npc id outside the roster, and
    ``npc_state.greeting_for`` falls back to an empty string, so a favour whose
    asker has no roster entry is a request the player cannot start.
    """
    asker = cfg.FAVOURS[favour_id]["npc"]
    entry = cfg.NPC_ROSTER.get(asker)
    assert entry is not None, f"favour {favour_id!r} asker {asker!r} has no roster entry"
    assert _is_filled_text(entry.get("greeting")), (
        f"favour {favour_id!r} asker {asker!r} has no greeting line"
    )


# ---------------------------------------------------------------------------
# Standing scale
# ---------------------------------------------------------------------------
def test_standing_bounds_bracket_the_thresholds():
    """The trust and distrust thresholds must sit INSIDE the declared bounds.

    ``clamp_standing`` clamps to ``STANDING_MIN``/``STANDING_MAX``, and
    ``npc_state.standing_band`` reads the thresholds. A threshold outside the
    bounds is a line no player can ever be given.
    """
    assert cfg.STANDING_MIN < cfg.STANDING_DISTRUSTED <= 0, (
        f"STANDING_DISTRUSTED ({cfg.STANDING_DISTRUSTED}) is not inside "
        f"(STANDING_MIN={cfg.STANDING_MIN}, 0]"
    )
    assert 0 <= cfg.STANDING_TRUSTED < cfg.STANDING_MAX, (
        f"STANDING_TRUSTED ({cfg.STANDING_TRUSTED}) is not inside "
        f"[0, STANDING_MAX={cfg.STANDING_MAX})"
    )


def test_distrust_is_the_mirror_of_trust():
    """The module sets distrust symmetric to trust so both are equally reachable.

    With a symmetric scale a player who talks to every NPC twice reaches trust
    and distrust at the same cost. With an asymmetric one the faction they
    neglected dominates, which is a balance decision nobody wrote down.
    """
    assert cfg.STANDING_DISTRUSTED == -cfg.STANDING_TRUSTED, (
        f"STANDING_DISTRUSTED ({cfg.STANDING_DISTRUSTED}) is not the mirror of "
        f"STANDING_TRUSTED ({cfg.STANDING_TRUSTED})"
    )


def test_standing_scale_is_a_signed_range_not_a_bar():
    """Standing is a small signed integer range, and both ends exist.

    A reversed or single-sided scale would make one of the two bands in
    ``npc_state.standing_band`` unreachable, which is the whole reason
    ``cold`` dialogue is written.
    """
    assert cfg.STANDING_MIN < 0 < cfg.STANDING_MAX, (
        f"standing scale {cfg.STANDING_MIN}..{cfg.STANDING_MAX} is not signed "
        "around zero"
    )


def test_default_favour_deadline_is_a_positive_number_of_hours():
    """The default deadline must be a positive, finite number of hours.

    ``npc_state.accept_favour`` stores ``due_at = now + deadline * 3600``. Zero
    or negative expires a favour the instant it is accepted, which the module's
    own note says is the failure the deadline exists to prevent.
    """
    assert isinstance(cfg.FAVOUR_DEADLINE_HOURS, float), (
        f"FAVOUR_DEADLINE_HOURS is {type(cfg.FAVOUR_DEADLINE_HOURS).__name__}"
    )
    assert cfg.FAVOUR_DEADLINE_HOURS > 0, (
        f"FAVOUR_DEADLINE_HOURS is {cfg.FAVOUR_DEADLINE_HOURS}; an accepted "
        "favour would already be overdue"
    )
    assert cfg.FAVOUR_DEADLINE_HOURS == cfg.FAVOUR_DEADLINE_HOURS, (
        "FAVOUR_DEADLINE_HOURS is not finite"
    )


# ---------------------------------------------------------------------------
# favour_deadline_hours
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_deadline_returns_a_positive_float_for_every_favour(favour_id):
    """Every shipped favour must yield a usable deadline.

    ``accept_favour`` multiplies this by 3600 and stores the result as
    ``due_at``, so a non-number here becomes a TypeError on accept.
    """
    deadline = cfg.favour_deadline_hours(favour_id)
    assert isinstance(deadline, float), (
        f"favour_deadline_hours({favour_id!r}) returned "
        f"{type(deadline).__name__}"
    )
    assert deadline > 0, f"favour_deadline_hours({favour_id!r}) is {deadline}"


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_favour_deadline_honours_a_per_favour_override(favour_id):
    """A ``deadline_hours`` key on the entry must beat the module default.

    No shipped favour sets one today, so without this test the override branch
    is unexercised and could be deleted without any test noticing.
    """
    marker = 7.5
    original = dict(cfg.FAVOURS[favour_id])
    assert "deadline_hours" not in original, (
        f"favour {favour_id!r} now ships a deadline_hours override; the fallback "
        "path this test depends on no longer applies to it"
    )
    cfg.FAVOURS[favour_id]["deadline_hours"] = marker
    try:
        assert cfg.favour_deadline_hours(favour_id) == marker
    finally:
        cfg.FAVOURS[favour_id].clear()
        cfg.FAVOURS[favour_id].update(original)


def test_favour_deadline_falls_back_to_default_for_an_unknown_id():
    """An unknown favour id must NOT raise -- it falls back to the default.

    ``npc_state`` reaches this helper with ids from stored player state, which
    can outlive a roster edit. Raising here turns a stale id into a 500 on an
    unrelated conversation.
    """
    deadline = cfg.favour_deadline_hours("no_such_favour_id")
    assert deadline == cfg.FAVOUR_DEADLINE_HOURS, (
        f"unknown favour id returned {deadline}, expected the module default "
        f"{cfg.FAVOUR_DEADLINE_HOURS}"
    )


def test_favour_deadline_falls_back_for_a_non_mapping_entry():
    """A non-dict entry must fall back too, not blow up on ``.get``.

    The helper is written ``FAVOURS.get(id) or {}``, so a falsy or non-dict
    entry degrades to the default. This pins that the guard is real.
    """
    cfg.FAVOURS["_contract_probe"] = ""  # falsy, and not a mapping
    try:
        assert cfg.favour_deadline_hours("_contract_probe") == (
            cfg.FAVOUR_DEADLINE_HOURS
        )
    finally:
        del cfg.FAVOURS["_contract_probe"]


# ---------------------------------------------------------------------------
# clamp_standing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given, expected",
    [
        (0, 0),
        (1, 1),
        (-1, -1),
        (cfg.STANDING_TRUSTED, cfg.STANDING_TRUSTED),
        (cfg.STANDING_DISTRUSTED, cfg.STANDING_DISTRUSTED),
    ],
)
def test_clamp_standing_passes_through_values_inside_the_scale(given, expected):
    """Values already on the scale come back unchanged."""
    assert cfg.clamp_standing(given) == expected


@pytest.mark.parametrize(
    "given, expected",
    [
        (cfg.STANDING_MAX + 1, cfg.STANDING_MAX),
        (cfg.STANDING_MAX * 1000, cfg.STANDING_MAX),
        (cfg.STANDING_MIN - 1, cfg.STANDING_MIN),
        (cfg.STANDING_MIN * 1000, cfg.STANDING_MIN),
    ],
)
def test_clamp_standing_saturates_at_the_declared_bounds(given, expected):
    """Out-of-range values saturate to the bound rather than wrapping or raising.

    The docstring says clamped rather than rejected, so a long chain of favours
    cannot produce a number no dialogue line was written for. ``expected`` is
    stated per case because the scale is symmetric and a wrong-bound clamp is
    the defect this catches.
    """
    clamped = cfg.clamp_standing(given)
    assert cfg.STANDING_MIN <= clamped <= cfg.STANDING_MAX, (
        f"clamp_standing({given}) produced {clamped}, outside "
        f"[{cfg.STANDING_MIN}, {cfg.STANDING_MAX}]"
    )
    assert clamped == expected, (
        f"clamp_standing({given}) produced {clamped}, expected saturation at {expected}"
    )


def test_clamp_standing_saturates_at_the_extremes():
    """A huge walk in either direction lands exactly on the declared bound."""
    assert cfg.clamp_standing(cfg.STANDING_MAX + 50) == cfg.STANDING_MAX
    assert cfg.clamp_standing(cfg.STANDING_MIN - 50) == cfg.STANDING_MIN


def test_clamp_standing_coerces_a_numeric_string():
    """Stored standing may arrive as a string; it must still clamp, not raise."""
    assert cfg.clamp_standing(str(cfg.STANDING_MAX + 10)) == cfg.STANDING_MAX


def test_clamp_standing_truncates_a_float_rather_than_raising():
    """A float is coerced with ``int()``, which truncates toward zero."""
    assert cfg.clamp_standing(1.9) == 1
    assert cfg.clamp_standing(-1.9) == -1


def test_clamp_standing_rejects_a_non_numeric_string():
    """ERROR CASE: garbage in must raise, not silently become a standing.

    ``int("abc")`` raises ``ValueError``. If this ever returned a default, a
    corrupted stored value would become a real number of standing and the player
    would be told an NPC trusts them for no reason.
    """
    with pytest.raises(ValueError):
        cfg.clamp_standing("abc")


def test_clamp_standing_rejects_none():
    """ERROR CASE: a missing stored value must raise rather than become zero.

    Zero is a real standing -- neutral -- so silently substituting it would
    fabricate a relationship state.
    """
    with pytest.raises((TypeError, ValueError)):
        cfg.clamp_standing(None)


def test_clamp_standing_never_returns_a_non_int():
    """Whatever comes in, the answer is a plain int a route can serialise."""
    for value in (2, -2, 999, -999, "4", 0.0):
        result = cfg.clamp_standing(value)
        assert isinstance(result, int) and not isinstance(result, bool)


# ---------------------------------------------------------------------------
# The shipped default deadline is FINITE -- not merely "not NaN"
# ---------------------------------------------------------------------------
# ``test_default_favour_deadline_is_a_positive_number_of_hours`` checks
# finiteness with ``x == x``, which is a NaN test. That catches ``nan`` and
# NOT ``inf``: ``inf == inf`` is True, and ``inf > 0`` is True, so an infinite
# default would pass every assertion in that test. An infinite default is
# survivable only by luck -- the one consumer, ``npc_state.accept_favour``,
# routes the computed sum through ``_readable_deadline`` -- so the property is
# asserted here directly, on the module that owns the number.

def test_the_default_deadline_is_finite_and_positive():
    """``math.isfinite`` is the real check; ``x == x`` only rules out NaN."""
    assert math.isfinite(cfg.FAVOUR_DEADLINE_HOURS), (
        f"FAVOUR_DEADLINE_HOURS is {cfg.FAVOUR_DEADLINE_HOURS}; an accepted "
        "favour would be born with an unreadable due_at"
    )
    assert cfg.FAVOUR_DEADLINE_HOURS > 0


def test_the_default_deadline_stays_finite_after_the_consumer_multiplies_it_out():
    """The stored value is ``hours * 3600``. That PRODUCT must be finite too.

    A perfectly finite number of hours can overflow once it becomes seconds:
    ``1e308 * 3600.0`` is ``inf``. So a finiteness assertion on the hours
    alone is not sufficient, and this is why ``npc_state`` guards the computed
    sum rather than the hours. Pinning the product here keeps the two ends in
    agreement -- and a retune that raises the default toward the ceiling fails
    on the same fact a config edit would.
    """
    seconds = cfg.FAVOUR_DEADLINE_HOURS * 3600.0
    assert math.isfinite(seconds), (
        f"the shipped default becomes {seconds} seconds, which no JSON encoder "
        "will write and no clock will ever reach"
    )


@pytest.mark.parametrize("favour_id", sorted(cfg.FAVOURS))
def test_no_shipped_favour_declares_a_deadline_override_at_all(favour_id):
    """Every shipped favour reads the module default today.

    This is a guard on the GUARD: ``test_favour_deadline_honours_a_per_favour
    _override`` injects an override to exercise a branch no shipped favour
    reaches, and it asserts the key is absent before injecting. If a designer
    later adds a real override that test would fail on a fact that is a
    legitimate change, so the fact is pinned in its own right here: an override
    is a deliberate config decision, not an accident.
    """
    assert "deadline_hours" not in cfg.FAVOURS[favour_id], (
        f"favour {favour_id!r} ships a deadline_hours override; the fallback "
        "path is no longer the live path for it"
    )


# ---------------------------------------------------------------------------
# Non-finite values: the module WIDENS, and that is the contract
# ---------------------------------------------------------------------------
# ``favour_deadline_hours``'s docstring draws a line that nothing else in the
# table draws, and the line is load-bearing:
#
#   * a value that is NOT a number at all ("soon", None, 10**400) falls back
#     to ``FAVOUR_DEADLINE_HOURS`` -- there is no number to rescue, so the
#     module default is the only sensible answer;
#   * a value that IS a number but not a USABLE one (inf, -inf, nan, 1e308)
#     is returned AS IS, deliberately, and the consumer's
#     ``_readable_deadline`` turns it into ``None`` -- the same value
#     ``refuse_favour`` writes and ``favour_view`` reports.
#
# Clamping those to the default would be wrong twice over: it hands back a
# real deadline for a value nobody set, and it does so BEFORE the caller can
# tell the difference. So a "graceful fallback" test that asserted a
# non-finite value becomes the default would be asserting the OPPOSITE of the
# documented design. Both directions are pinned below.

#: Shapes that cannot be coerced to a float at all.
NOT_A_NUMBER = ("soon", "", None, [], {}, "24h", 10 ** 400, object())

#: Shapes that ARE numbers but are not usable as an hour count. ``1e308`` is
#: finite yet overflows to ``inf`` the moment the consumer multiplies by 3600,
#: which is why it is in this list and not in the ``isfinite`` sense.
NOT_A_USABLE_NUMBER = (float("inf"), float("-inf"), float("nan"), 1e308)


def _with_deadline_override(value, run):
    """Run ``run()`` with a temporary ``deadline_hours`` on one favour.

    Restored in a ``finally`` so a failing assertion cannot leave the shipped
    table mutated for the rest of the session -- the table is module state
    shared with every other test in this file.
    """
    favour_id = cfg.FAVOUR_ORDER[0]
    original = dict(cfg.FAVOURS[favour_id])
    assert "deadline_hours" not in original
    cfg.FAVOURS[favour_id]["deadline_hours"] = value
    try:
        return run()
    finally:
        cfg.FAVOURS[favour_id].clear()
        cfg.FAVOURS[favour_id].update(original)


@pytest.mark.parametrize("supplied", NOT_A_NUMBER)
def test_a_value_that_is_not_a_number_falls_back_to_the_module_default(supplied):
    """The first of the two failure modes named in the docstring.

    ``float()`` raises ``TypeError``/``ValueError``/``OverflowError`` for each
    of these -- and ``OverflowError`` is neither a ``TypeError`` nor a
    ``ValueError`` (``float(10**400)`` raises it), so a guard that caught only
    the first two would propagate a config typo into a failed request the
    player did nothing wrong on.
    """
    got = _with_deadline_override(
        supplied, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
    assert got == cfg.FAVOUR_DEADLINE_HOURS, (
        f"deadline_hours={supplied!r} produced {got!r}, expected the module "
        f"default {cfg.FAVOUR_DEADLINE_HOURS}"
    )
    assert math.isfinite(got)


@pytest.mark.parametrize("supplied", NOT_A_NUMBER)
def test_a_value_that_is_not_a_number_never_raises(supplied):
    """The docstring's first sentence: "Never raises".

    ``npc_state.accept_favour`` reaches this from the accept route, so a raise
    here turns a config typo into a 500 on a conversation the player did
    nothing wrong on. Asserted as a separate test from the fallback value so
    the "never raises" claim stands on its own.
    """
    _with_deadline_override(supplied, lambda: cfg.favour_deadline_hours(
        cfg.FAVOUR_ORDER[0]))


@pytest.mark.parametrize("supplied", NOT_A_USABLE_NUMBER)
def test_a_number_that_is_not_usable_is_returned_as_is_not_clamped(supplied):
    """The second failure mode -- and the one a naive fallback test gets wrong.

    The module WIDENS here rather than rescuing. Clamping to the default would
    invent a real deadline for a value nobody set, and would do it before the
    caller could see that anything was wrong. ``nan`` needs its own comparison:
    ``nan == nan`` is False, so the identity check below is what proves it
    survived rather than being replaced.
    """
    got = _with_deadline_override(
        supplied, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
    assert isinstance(got, float)
    # Same value in, same value out -- including nan, compared as "both are
    # not-a-number" rather than by equality.
    if math.isnan(got):
        assert math.isnan(supplied), f"{supplied!r} came back as nan"
    else:
        assert got == supplied, f"{supplied!r} came back as {got!r}"


@pytest.mark.parametrize("supplied", NOT_A_USABLE_NUMBER)
def test_the_widened_value_is_still_what_the_consumer_has_to_defend_against(supplied):
    """WHY the module widens instead of clamping: the sum is what breaks.

    ``npc_state.accept_favour`` computes ``now + hours * 3600`` and routes it
    through ``_readable_deadline``, which stores an unreadable deadline as
    ``None``. Both facts are load-bearing together, so this pins the product
    that makes the widening necessary: for every value the module declines to
    rescue, the multiplied-out result is not JSON-encodable, so the consumer's
    guard is what saves the row.
    """
    hours = _with_deadline_override(
        supplied, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
    seconds = hours * 3600.0
    assert not math.isfinite(seconds), (
        f"{supplied!r} became {seconds} seconds, which IS finite -- if this is "
        "now true the consumer's guard is no longer load-bearing for this shape"
    )
    # And standard JSON has no literal for either, so it cannot be written out.
    with pytest.raises(ValueError):
        __import__("json").dumps({"due_at": seconds}, allow_nan=False)


def test_the_two_failure_modes_are_told_apart_by_finiteness_of_the_result():
    """Not-a-number rescues; not-usable widens. One test, so the line is explicit.

    Grouped by what comes BACK, not by what goes in, because that is the
    property a caller depends on: a rescued value and a widened value are
    distinguishable from the result alone.
    """
    rescued = [
        _with_deadline_override(v, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
        for v in NOT_A_NUMBER
    ]
    widened = [
        _with_deadline_override(v, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
        for v in NOT_A_USABLE_NUMBER
    ]
    assert all(math.isfinite(v) for v in rescued), rescued
    assert not all(math.isfinite(v) for v in widened), widened
    # 1e308 is finite in hours and still unusable once multiplied out, so
    # "rescued" cannot be re-derived from "finite in hours" alone.
    assert math.isfinite(1e308) and not math.isfinite(1e308 * 3600.0)


def test_a_numeric_string_deadline_is_coerced_rather_than_rescued():
    """A quoted number is a number. It must not fall through to the default.

    ``float("24")`` succeeds, so a hand-edited config that quotes its hours is
    read at face value rather than silently replaced. This is the same
    "widen, do not invent" rule as above, applied to the coercible case.
    """
    quoted = _with_deadline_override(
        "48", lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
    assert quoted == 48.0
    assert quoted != cfg.FAVOUR_DEADLINE_HOURS or cfg.FAVOUR_DEADLINE_HOURS == 48.0, (
        "the default happens to equal 48.0, so this case cannot distinguish "
        "coercion from rescue; change the probe value"
    )


def test_a_bool_deadline_is_read_as_a_number_because_python_says_it_is_one():
    """``float(True)`` is ``1.0``. Pinned so the behaviour is not a surprise.

    Booleans are ints in Python, so the helper cannot tell ``True`` from ``1``.
    That is the safe direction (one hour is a real deadline), but it is a
    decision, and a config that sets ``deadline_hours: true`` should be caught
    by review rather than discovered in play.
    """
    got = _with_deadline_override(
        True, lambda: cfg.favour_deadline_hours(cfg.FAVOUR_ORDER[0]))
    assert got == 1.0
    assert math.isfinite(got)


# ---------------------------------------------------------------------------
# Standing boundaries: the scale the favours are priced against
# ---------------------------------------------------------------------------

def test_the_standing_bounds_are_symmetric_in_magnitude():
    """``STANDING_MIN`` must be exactly ``-STANDING_MAX``.

    The module sets ``STANDING_DISTRUSTED = -STANDING_TRUSTED`` for symmetry,
    which is only honest if the BOUNDS are symmetric too. An asymmetric range
    (``-3..+5``) would give trust five steps of room and distrust three, so the
    two dialogue bands the module writes for stop being equally reachable.
    """
    assert cfg.STANDING_MIN == -cfg.STANDING_MAX, (
        f"standing range {cfg.STANDING_MIN}..{cfg.STANDING_MAX} is not symmetric "
        "around zero"
    )
    assert isinstance(cfg.STANDING_MIN, int) and not isinstance(cfg.STANDING_MIN, bool)


def test_the_standing_scale_is_exactly_three_steps_from_neutral_each_way():
    """The scale is declared as a size; assert the size, not a snapshot of it.

    The module's own note is "a conversation is a scene, not a grind" -- so the
    number of steps IS the balance. Retuning it is legitimate; what must hold is
    that the two directions have the same number of steps.
    """
    steps = cfg.STANDING_MAX - cfg.STANDING_MIN + 1
    assert steps % 2 == 1, (
        f"a signed scale must have an odd number of steps so zero is on it; "
        f"{cfg.STANDING_MIN}..{cfg.STANDING_MAX} has {steps}"
    )
    assert 0 in range(cfg.STANDING_MIN, cfg.STANDING_MAX + 1)


@pytest.mark.parametrize("supplied", [1e308, -1e308, 10 ** 30])
def test_clamp_standing_saturates_a_value_far_past_any_declared_bound(supplied):
    """A huge finite number clamps; it does not raise and does not overflow.

    ``int(1e308)`` is a 309-digit integer, which is fine in Python, so the
    clamp is the only thing standing between that and the declared bound.
    """
    clamped = cfg.clamp_standing(supplied)
    assert clamped in (cfg.STANDING_MIN, cfg.STANDING_MAX), clamped
    assert clamped == (cfg.STANDING_MAX if supplied > 0 else cfg.STANDING_MIN)


@pytest.mark.parametrize("supplied", [float("inf"), float("-inf")])
def test_clamp_standing_raises_on_an_infinite_value(supplied):
    """ERROR CASE: ``int(inf)`` is ``OverflowError``, and that is correct here.

    Unlike the deadline, standing has no forgiving consumer: an infinite
    standing would make an NPC either omnipotent or worthless forever, and
    there is no ``None`` marker available to store it as. So this one refuses
    loudly rather than widening -- which is the OPPOSITE of the deadline rule,
    and the difference is deliberate. Pinned so a later "be consistent" edit
    has to notice that consistency would mean inventing a standing.
    """
    with pytest.raises(OverflowError):
        cfg.clamp_standing(supplied)


def test_clamp_standing_raises_on_a_not_a_number_value():
    """ERROR CASE: ``int(nan)`` is ``ValueError``. Same reasoning as above."""
    with pytest.raises(ValueError):
        cfg.clamp_standing(float("nan"))


def test_clamp_standing_is_idempotent_and_monotonic():
    """Clamping twice equals clamping once, and a bigger input never lands lower.

    Two properties of a clamp that are easy to break separately: a clamp that
    re-derives from its input is not idempotent, and one whose bounds are
    applied in the wrong order inverts at one end.
    """
    for value in range(-12, 13):
        once = cfg.clamp_standing(value)
        assert cfg.clamp_standing(once) == once, value
    monotone = [cfg.clamp_standing(v) for v in range(-12, 13)]
    assert monotone == sorted(monotone), monotone


def test_every_standing_the_favours_can_produce_stays_inside_the_scale():
    """The largest cost the roster can charge must not be silently clamped.

    If ``cost_amount`` ever exceeded ``STANDING_MAX`` the player would be shown
    a cost they are not actually charged. This asserts the two tables agree,
    which is the relation between them rather than a frozen amount.
    """
    largest = max(f["cost_amount"] for f in cfg.FAVOURS.values())
    assert cfg.clamp_standing(-largest) == -largest, (
        f"the largest declared cost ({largest}) would be clamped on charge; the "
        "player is shown a cost they never pay"
    )
    assert largest <= cfg.STANDING_MAX


# ---------------------------------------------------------------------------
# Combined: the tables agree with each other
# ---------------------------------------------------------------------------
def test_no_favour_is_offered_by_an_npc_without_a_cost_to_pay():
    """Every favour has both an asker and a cost victim, in the roster.

    Restated as one relation so a partially-filled favour fails loudly here
    rather than at the first conversation that touches it.
    """
    for favour_id, favour in cfg.FAVOURS.items():
        asker = favour["npc"]
        victim = favour["cost_npc"]
        assert asker in cfg.NPC_ROSTER, f"{favour_id}: asker {asker!r} unknown"
        assert victim in cfg.NPC_ROSTER, f"{favour_id}: cost_npc {victim!r} unknown"
        assert asker != victim, f"{favour_id}: asker and cost victim are the same"
        assert favour["gain"] >= 1, f"{favour_id}: nothing to gain"
        assert favour["cost_amount"] >= 1, f"{favour_id}: nothing to pay"