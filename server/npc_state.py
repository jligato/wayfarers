"""
NPC conversation state: the part that has to survive a restart.

The bug class this module exists to not repeat
---------------------------------------------
``server/player_store.py`` hands out COPIES on read (copy-on-read, outside a
transaction) and only writes a row back when a writer path runs. A handler that
does::

    player = players_db[pid]          # a detached copy
    player["npc_state"] = something    # written into the copy
    return ...                         # copy discarded

has changed nothing. The symptom is a conversation that remembers for exactly
as long as the request does. This is the same shape as the money bug, so the
rules here are the same:

* every mutation happens INSIDE ``store.transaction(pid)``, where ``store[pid]``
  is the live staged row and the store compares-then-upserts on exit;
* nothing is written to a dict returned by a read and then dropped;
* :func:`load_state` and :func:`save_state` are the only two doors, and
  ``save_state`` takes the row object, not a copy.

What is stored
--------------
One dict under the ``npc_state`` key of the player row::

    {
      "standing":  {npc_id: int},                 # signed, clamped
      "met":       {npc_id: {"visits": int, "last_seen": float}},
      "offers":    {favour_id: {"first_offered": float}},
      "favours":   {favour_id: {"status", "accepted_at", "due_at", "resolved_at"}},
      "leads":     [str, ...],                    # information, never currency
    }

It rides in ``extra_state`` like every other non-column field, so it needs no
migration and no new table. An absent or malformed ``npc_state`` reads as a
first meeting, never as a crash: a returning player on an old row must still be
able to talk to somebody.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from stored_numbers import stored_float as _stored_number

from npc_roster_config import (
    FAVOUR_ORDER,
    FAVOURS,
    NPC_ROSTER,
    STANDING_DISTRUSTED,
    STANDING_TRUSTED,
    clamp_standing,
    favour_deadline_hours,
)

#: The key this state lives under inside the player row.
STATE_KEY = "npc_state"

#: Favour lifecycle. ACCEPTED is the only state in which a deadline is running.
STATUS_OFFERED = "offered"
STATUS_ACCEPTED = "accepted"
STATUS_REFUSED = "refused"
STATUS_SETTLED = "settled"
STATUS_FAILED = "failed"

#: Terminal states. A favour in one of these is never offered again, so a
#: player cannot farm the same request by reloading the screen.
TERMINAL_STATUSES = frozenset({STATUS_REFUSED, STATUS_SETTLED, STATUS_FAILED})


def _now() -> float:
    return time.time()


#: ``_stored_number`` is ``stored_numbers.stored_float`` under this
#: name: it was measured behaviourally identical across every
#: non-finite spelling, so this module keeps one implementation.
#: See that module for why the three-exception tuple AND the
#: explicit finiteness test are both required.


def _readable_deadline(due_at: Any) -> Optional[float]:
    """A saved deadline as a finite number, or ``None`` when unreadable.

    ``_stored_number`` is the ONE door onto a stored number in this
    module, so the deadline goes through it too. Measured: the deadline
    used to read the row with a bare ``isinstance(due_at, (int, float))``,
    which lets ``inf`` and ``nan`` straight through. Standard JSON has no
    literal for either -- ``allow_nan=False`` refuses to encode them -- so
    the roster reply could not be serialised at all, and no clock ever
    reaches ``inf``, so the countdown never finished. The player was
    locked out of that request permanently.

    ``default=None`` is the unreadable marker: a real deadline of 0.0 is
    a number and still reads as 0.0, which is what keeps it distinct
    from a deadline that cannot be read.
    """
    return _stored_number(due_at, default=None)


def load_state(player: Dict[str, Any]) -> Dict[str, Any]:
    """Return the NPC state dict, normalised.

    Returns the dict that is ALREADY INSIDE ``player`` when it is well formed,
    so a caller inside a transaction can mutate it in place. A malformed value is
    replaced with a fresh dict and written back through ``player`` -- which is
    also only safe inside a transaction, and every caller here is.
    """
    raw = player.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        player[STATE_KEY] = raw

    standing = raw.get("standing")
    raw["standing"] = standing if isinstance(standing, dict) else {}
    met = raw.get("met")
    raw["met"] = met if isinstance(met, dict) else {}
    offers = raw.get("offers")
    raw["offers"] = offers if isinstance(offers, dict) else {}
    favours = raw.get("favours")
    raw["favours"] = favours if isinstance(favours, dict) else {}
    leads = raw.get("leads")
    raw["leads"] = list(leads) if isinstance(leads, list) else []
    return raw


def save_state(player: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Write the state back onto the row.

    ``player`` MUST be the live staged row from inside
    ``store.transaction(pid)``. This function does not copy and does not call
    the store: the transaction's compare-then-upsert is the writer, and adding a
    second write path here would be the exact bug this module documents.
    """
    player[STATE_KEY] = state


# ---------------------------------------------------------------------------
# Standing
# ---------------------------------------------------------------------------


def get_standing(state: Dict[str, Any], npc_id: str) -> int:
    value = state.get("standing", {}).get(npc_id, 0)
    return clamp_standing(int(_stored_number(value)))


def adjust_standing(state: Dict[str, Any], npc_id: str, delta: int) -> int:
    """Move one NPC's standing by ``delta`` and return the new value."""
    standing = state.setdefault("standing", {})
    new_value = clamp_standing(get_standing(state, npc_id) + int(delta))
    standing[npc_id] = new_value
    return new_value


def standing_band(state: Dict[str, Any], npc_id: str) -> str:
    """``trusted`` / ``neutral`` / ``distrusted`` -- which greeting to speak."""
    value = get_standing(state, npc_id)
    if value >= STANDING_TRUSTED:
        return "trusted"
    if value <= STANDING_DISTRUSTED:
        return "distrusted"
    return "neutral"


# ---------------------------------------------------------------------------
# Meeting
# ---------------------------------------------------------------------------


def first_meeting(state: Dict[str, Any], npc_id: str) -> bool:
    """True if this NPC has not spoken with this player before.

    Read BEFORE :func:`note_meeting` increments the counter, or it is always
    False. The route below calls it in that order deliberately: the observation
    of the player is what a first meeting is FOR, and losing it makes every
    greeting identical.
    """
    record = state.get("met", {}).get(npc_id)
    if not isinstance(record, dict):
        return True
    # A record that exists but cannot be read is still a meeting that HAPPENED.
    # Defaulting the unreadable counter to 0 here would replay the first-meeting
    # observation at a player who has already met this NPC; the counter is
    # rewritten to a real number by note_meeting a moment later either way.
    return int(_stored_number(record.get("visits", 0), default=1)) <= 0


def note_meeting(
    state: Dict[str, Any], npc_id: str, player: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Record a conversation, and what this NPC last saw the player be.

    ``observed_realm`` is the field that makes a RETURNING player interesting.
    An NPC that only comments on a first meeting rewards a new account; an NPC
    that notices you have come back stronger is what an idle game actually
    wants from its cast.
    """
    met = state.setdefault("met", {})
    record = met.get(npc_id)
    if not isinstance(record, dict):
        record = {"visits": 0, "last_seen": 0.0, "observed_realm": None, "observed_stage": None}
        met[npc_id] = record
    record["visits"] = int(_stored_number(record.get("visits", 0))) + 1
    record["last_seen"] = _now()
    if player is not None:
        record["observed_realm"] = str(player.get("cultivation_realm", "mortal"))
        record["observed_stage"] = str(player.get("cultivation_stage", "early"))
    return record


def changed_since_last_meeting(state: Dict[str, Any], npc_id: str, player: Dict[str, Any]) -> bool:
    """True when the player is not what this NPC last saw.

    Deliberately realm-and-stage only. Progress moves every minute in an idle
    game, so including it would make the NPC comment on breathing.
    """
    record = state.get("met", {}).get(npc_id)
    if not isinstance(record, dict) or record.get("observed_realm") is None:
        return False
    return (
        str(record.get("observed_realm")) != str(player.get("cultivation_realm", "mortal"))
        or str(record.get("observed_stage")) != str(player.get("cultivation_stage", "early"))
    )


# ---------------------------------------------------------------------------
# Favours
# ---------------------------------------------------------------------------


def available_favour(state: Dict[str, Any]) -> Optional[str]:
    """The one favour this NPC could offer right now, or ``None``.

    One at a time on purpose. A list of four requests is a menu; a single
    request with a real cost is a decision.
    """
    if state.get("active_favour_id"):
        return None
    for favour_id in FAVOUR_ORDER:
        favour = FAVOURS.get(favour_id)
        if not favour:
            continue
        record = state.setdefault("favours", {}).get(favour_id)
        if isinstance(record, dict) and record.get("status") in TERMINAL_STATUSES:
            continue
        return favour_id
    return None


def favour_view(
    state: Dict[str, Any],
    favour_id: str,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """What the client needs to draw one favour, including what it COSTS.

    ``cost`` is reported as a named NPC and a signed step, never as a number of
    spirit stones, because there is no price. A player who cannot see who they
    are about to disappoint cannot make the decision the design is built on.
    """
    favour = FAVOURS.get(favour_id)
    if not favour:
        return None
    record = state.setdefault("favours", {}).get(favour_id)
    record = record if isinstance(record, dict) else {}
    status = record.get("status", STATUS_OFFERED)
    now = _now() if now is None else now
    due_at = record.get("due_at")
    return {
        "favour_id": favour_id,
        "npc_id": favour["npc"],
        "ask": favour["ask"],
        "status": status,
        "cost_npc_id": favour["cost_npc"],
        "cost_amount": int(favour["cost_amount"]),
        "cost_standing": get_standing(state, favour["cost_npc"]),
        "gain_standing": int(favour["gain"]),
        "due_at": _readable_deadline(due_at),
        "hours_left": _hours_left(due_at, now),
        "settled_line": favour["on_settle"],
    }


def _hours_left(due_at: Any, now: float) -> Optional[float]:
    """Hours until the deadline, or ``None`` when there is no readable one.

    Reads through :func:`_readable_deadline` so the subtraction can never
    produce ``inf`` or ``nan``: the reply is JSON, and neither has a
    literal there. ``max(0.0, ...)`` still floors a deadline that has
    already passed at zero rather than reporting negative hours.
    """
    deadline = _readable_deadline(due_at)
    if deadline is None:
        return None
    return max(0.0, (deadline - now) / 3600.0)


def accept_favour(state: Dict[str, Any], favour_id: str) -> Dict[str, Any]:
    """Accept a favour: charge the cost, start the clock, remember the lead.

    The cost is charged AT ACCEPTANCE, not at settlement. That is deliberate:
    the moment of agreeing is the moment the player gave something up, and a cost
    that materialises later reads as a bug rather than a choice.
    """
    favour = FAVOURS.get(favour_id)
    if not favour:
        raise KeyError(favour_id)
    now = _now()
    favours = state.setdefault("favours", {})
    existing = favours.get(favour_id)
    # ACCEPTED has to be refused here too, not only the terminal states.
    # Blocking only the terminal ones let a double-tap on ACCEPT -- or a replayed
    # request -- charge the cost a second time and restart the clock. Measured:
    # two accepts on one favour took the third party's standing to -2.
    if isinstance(existing, dict) and existing.get("status") in TERMINAL_STATUSES:
        raise ValueError("this favour is already resolved")
    if isinstance(existing, dict) and existing.get("status") == STATUS_ACCEPTED:
        raise ValueError("this favour is already accepted")

    # The deadline is READ before the cost is charged, not after.
    #
    # This ordering is what makes the read a guard rather than a
    # repair. Measured: with the charge on the line above, a helper
    # that REFUSES a bad value refuses it AFTER the player was
    # debited -- standing went 5 -> 2 for an acceptance that never
    # happened, and the route turned it into a 409 the player could
    # not clear. A/B against these bytes: original order charged,
    # this order leaves the player whole.
    #
    # The read stays ABOVE the charge on purpose, under either design
    # for `favour_deadline_hours`: one shape widens a bad config value
    # to the module default, the other refuses it loudly. The fix is
    # correct for both, and it stops mattering which one ships.
    #
    # `_readable_deadline` stays on the result, and it is not
    # redundant with whatever the config helper does: a perfectly
    # FINITE config value can still overflow once multiplied out to
    # seconds (`1e308 * 3600.0` is `inf`), and no check on the hours
    # alone can see that. The hours are checked for being a number;
    # the SUM is checked for being usable.
    due_at = _readable_deadline(now + favour_deadline_hours(favour_id) * 3600.0)
    adjust_standing(state, favour["cost_npc"], -int(favour["cost_amount"]))
    record = {
        "status": STATUS_ACCEPTED,
        "accepted_at": now,
        # The deadline is COMPUTED here, so this is the only place a
        # non-finite number can be born. Measured: `favour_deadline_hours`
        # is a bare `float(favour.get("deadline_hours", ...))`, so an
        # `inf` or `nan` in the config table made this line store
        # `now + inf = inf` straight onto the player row, and Python's
        # `json` refuses to encode it -- a row that cannot be dumped,
        # exported or replicated. Routing the read back through
        # `_readable_deadline` removes it at the SOURCE rather than at the
        # reply, which is where the earlier fix caught it: the reply was
        # safe and the database still held the infinity.
        #
        # The guard goes on the computed SUM, not on the hours before it,
        # because a perfectly finite config value can still overflow:
        # `1e308 * 3600.0` is `inf` too.
        #
        # Unreadable is stored as `None` rather than refused, and that is
        # this module's existing contract rather than a new policy:
        # `refuse_favour` already writes `"due_at": None` as a legitimate
        # value, `favour_view` already reports `None` for it, and
        # `expire_favours` already closes a request whose deadline reads
        # as 0.0 -- so a `None` here behaves exactly like the `"soon"`
        # string the suite already pins: the request closes and the next
        # one opens. Raising instead would turn a config typo into a
        # permanent 409 for the player, on a favour they never did
        # anything wrong on, and would leave the cost charged for an
        # acceptance that did not happen. The same "widen, never reject"
        # rule as `_stored_number` and `clamp_standing`.
        "due_at": due_at,
        "resolved_at": None,
    }
    favours[favour_id] = record
    state["active_favour_id"] = favour_id
    state.setdefault("offers", {})[favour_id] = {"first_offered": now}
    return record


def refuse_favour(state: Dict[str, Any], favour_id: str) -> Dict[str, Any]:
    """Refuse: the asker loses one step of standing and the offer is closed.

    No cost is charged. Refusing has to be genuinely available or the whole
    request/accept/refuse shape is theatre.
    """
    favour = FAVOURS.get(favour_id)
    if not favour:
        raise KeyError(favour_id)
    existing = state.setdefault("favours", {}).get(favour_id)
    if isinstance(existing, dict) and existing.get("status") in TERMINAL_STATUSES:
        raise ValueError("this favour is already resolved")
    adjust_standing(state, favour["npc"], -1)
    record = {
        "status": STATUS_REFUSED,
        "accepted_at": None,
        "due_at": None,
        "resolved_at": _now(),
    }
    state["favours"][favour_id] = record
    state.pop("active_favour_id", None)
    return record


def settle_favour(state: Dict[str, Any], favour_id: str) -> Dict[str, Any]:
    """Resolve an accepted favour in time: the asker warms, the lead is kept."""
    favour = FAVOURS.get(favour_id)
    if not favour:
        raise KeyError(favour_id)
    record = state.setdefault("favours", {}).get(favour_id)
    if not isinstance(record, dict) or record.get("status") != STATUS_ACCEPTED:
        raise ValueError("no accepted favour with that id")
    adjust_standing(state, favour["npc"], int(favour["gain"]))
    leads = state.setdefault("leads", [])
    if favour["lead"] not in leads:
        leads.append(favour["lead"])
    record["status"] = STATUS_SETTLED
    record["resolved_at"] = _now()
    state.pop("active_favour_id", None)
    return record


def fail_favour(state: Dict[str, Any], favour_id: str) -> Dict[str, Any]:
    """Fail an accepted favour: the asker cools by the same step they would have gained."""
    favour = FAVOURS.get(favour_id)
    if not favour:
        raise KeyError(favour_id)
    record = state.setdefault("favours", {}).get(favour_id)
    if not isinstance(record, dict) or record.get("status") != STATUS_ACCEPTED:
        raise ValueError("no accepted favour with that id")
    adjust_standing(state, favour["npc"], -int(favour["gain"]))
    record["status"] = STATUS_FAILED
    record["resolved_at"] = _now()
    state.pop("active_favour_id", None)
    return record


def expire_favours(state: Dict[str, Any], now: Optional[float] = None) -> List[str]:
    """Fail every accepted favour whose deadline has passed. Returns their ids.

    Called on every roster read, which is the only place a lazy deadline can be
    evaluated without a scheduler. Expiring is a real state change, so callers
    must be inside a transaction -- which every route that reads the roster is.
    """
    now = _now() if now is None else now
    expired: List[str] = []
    for favour_id, record in list(state.get("favours", {}).items()):
        if not isinstance(record, dict) or record.get("status") != STATUS_ACCEPTED:
            continue
        due_at = record.get("due_at")
        if _stored_number(due_at) <= now:
            if favour_id in FAVOURS:
                fail_favour(state, favour_id)
            else:
                # The config dropped this request after the player accepted it,
                # so fail_favour cannot run: there is no asker and no gain left
                # to charge. It still must not sit in `accepted` forever --
                # available_favour reads `accepted` as an open request, so the
                # player would never be offered another one. Closing it here
                # costs no standing, which is the honest outcome for a request
                # that no longer exists.
                record["status"] = STATUS_FAILED
                record["resolved_at"] = now
                if state.get("active_favour_id") == favour_id:
                    state.pop("active_favour_id", None)
            expired.append(favour_id)
    return expired


# ---------------------------------------------------------------------------
# What the NPC says
# ---------------------------------------------------------------------------


def observe_player(player: Dict[str, Any]) -> str:
    """One clause about what this NPC can see the player has become.

    Built from fields the player row already carries -- realm, stage, progress,
    sect -- so it can never claim something the player has not done. An NPC that
    reacts to progress is the whole reason to come back; an NPC that reacts to a
    number that does not exist is worse than silence.
    """
    realm = str(player.get("cultivation_realm", "mortal"))
    stage = str(player.get("cultivation_stage", "early"))
    sect_id = player.get("sect_id")
    try:
        progress = float(player.get("progress", 0.0))
    except (TypeError, ValueError):
        progress = 0.0

    if sect_id:
        where = f"under {sect_id}'s colours"
    else:
        where = "with no sect behind you"
    if progress >= 100.0:
        closeness = "and standing at the edge of a breakthrough"
    elif progress >= 50.0:
        closeness = "and further along the stage than most"
    else:
        closeness = "and early in it, though not careless"
    return f"You are {realm}, {stage}, {where} {closeness}."


def greeting_for(
    state: Dict[str, Any],
    npc_id: str,
    player: Dict[str, Any],
    is_first_meeting: Optional[bool] = None,
    changed: Optional[bool] = None,
) -> str:
    """The opening line: voice first, then what the NPC has noticed about you.

    ``is_first_meeting`` is passed in rather than derived, because the route has
    already incremented the visit count by the time it wants to speak. Deriving
    it here (the first version did) means the observation line is unreachable.
    """
    entry = NPC_ROSTER.get(npc_id) or {}
    band = standing_band(state, npc_id)
    opener = entry.get("greeting", "")
    if band == "trusted":
        opener = entry.get("warm", opener)
    elif band == "distrusted":
        opener = entry.get("cold", opener)
    if is_first_meeting is None:
        is_first_meeting = first_meeting(state, npc_id)
    if changed is None:
        changed = changed_since_last_meeting(state, npc_id, player)
    # ``changed`` is passed in rather than derived, for the same reason
    # ``is_first_meeting`` is: the route has already called note_meeting, which
    # overwrites observed_realm, so deriving it here always answers False.
    if is_first_meeting or changed:
        # First meeting, or a return after a change: the NPC reads the player
        # before saying anything else. This is the design intent in one line --
        # an NPC who notices what you have become is worth more than a reward.
        return f"{opener}\n\n{observe_player(player)}"
    return opener


def request_line(state: Dict[str, Any], favour_id: str) -> str:
    return FAVOURS[favour_id]["ask"]


def respond_line(favour_id: str, event: str) -> str:
    """``event`` is one of accept / refuse / settle / fail."""
    favour = FAVOURS.get(favour_id) or {}
    return {
        "accept": favour.get("on_accept", ""),
        "refuse": favour.get("on_refuse", ""),
        "settle": favour.get("on_settle", ""),
        "fail": favour.get("on_fail", ""),
    }.get(event, "")


def roster_entry(state: Dict[str, Any], npc_id: str, now: Optional[float] = None) -> Dict[str, Any]:
    """One row of the roster list the client draws.

    Includes everything needed to decide WITHOUT opening a conversation:
    who they are, which faction, what they want, how they feel about you, and
    whether something is at stake right now.
    """
    entry = NPC_ROSTER.get(npc_id)
    if not entry:
        return {}
    now = _now() if now is None else now
    meeting = state.get("met", {}).get(npc_id)
    favour_id = available_favour(state)
    at_stake = None
    if favour_id and FAVOURS[favour_id]["npc"] == npc_id:
        at_stake = favour_view(state, favour_id, now)
    elif state.get("active_favour_id"):
        active = FAVOURS.get(state["active_favour_id"])
        if active and active["npc"] == npc_id:
            at_stake = favour_view(state, state["active_favour_id"], now)
    return {
        "id": npc_id,
        "name": entry["name"],
        "title": entry["title"],
        "faction": entry["faction"],
        "wants": entry["wants"],
        "voice": entry["voice"],
        "standing": get_standing(state, npc_id),
        "band": standing_band(state, npc_id),
        "visits": int(_stored_number(meeting.get("visits", 0))) if isinstance(meeting, dict) else 0,
        "last_seen": _stored_number(meeting.get("last_seen", 0.0)) if isinstance(meeting, dict) else 0.0,
        "at_stake": at_stake,
        "has_unmet_lead": bool(state.get("active_favour_id")),
    }


def npc_ids() -> Tuple[str, ...]:
    return tuple(NPC_ROSTER.keys())
