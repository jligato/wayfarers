"""Dependency-aware goal tracking: why am I blocked, and what unblocks me.

Implements roadmap item C2. The tutorial sandbox in ``first_session.py`` already
proved the goal contract (goal_id / requirements / prerequisite_steps /
eligible_action). That contract was hardcoded to one practice stage. This module
generalises it to live player state.

Design rule, enforced by ``test_goal_graph.py``: every requirement a goal
reports must be readable out of a real source. Costs come from the real
consumption tables (``inner_skills.SKILLS``), never from a literal invented here.
An unresolvable requirement raises rather than guessing, so the panel can never
promise a route to a goal that does not exist.

The second half of that rule is stricter than it looks: an action may only be
advertised when the real route would ACCEPT it. ``inner_skills`` writes a skill
row the instant learning *starts* (state ``comprehending``), not when it ends,
and refuses ``begin_tupo`` until the row reaches ``comprehend_complete``. So the
stage is read from the stored ``state`` field plus the skill's real duration
table -- never from bare key presence, which offered a breakthrough to a player
whose comprehension had not finished and collected HTTP 409 from the very button
the panel highlighted.

The whole module is read-only. It never spends, grants, or mutates.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from inner_skills import REALM_ORDER, SKILLS
from mission_board import BOARD_OBJECTIVES, TIER_MATERIAL

_ITEMS_PATH = Path(__file__).resolve().parent.parent / "shared" / "items" / "items.json"
# Imported from the gate module, not re-typed. A second copy of the ladder is a
# second answer to "which realms exist", and it silently diverges the moment a
# realm is added to inner_skills.py.
_REALM_ORDER: list[str] = list(REALM_ORDER)

# Stage names this module distinguishes. They mirror the ``state`` values
# inner_skills writes, which are the only thing the real routes branch on.
_UNLEARNED = "unlearned"
_LEARNING = "learning"
_READY_TUPO = "ready_tupo"
_BREAKING = "breaking"
_ACTIVE = "active"
#: The breakthrough is the only goal this skill has, and the real route accepts
#: NO action for the stored row behind it. It is a stage of its own because
#: neither existing rung is honest here: reporting it ready offers a button that
#: 409s, and reporting it still-breaking reports a timer that has already run
#: out (or was never readable) as still counting down.
_BLOCKED_TUPO = "blocked_tupo"


# Which verb a stage is reported under: "learn" until the breakthrough has
# started, "tupo" from then on. ONE table, read by BOTH the goal list and the
# item route, because the item route's whole promise is that every id it names
# is an id the goal list actually contains. Two hand-written copies of this
# rule would drift apart the first time a stage is added, and the drift would
# be invisible: every id would still look well formed.
_STAGE_VERB: dict[str, str] = {
    _UNLEARNED: "learn",
    _LEARNING: "learn",
    _BREAKING: "tupo",
    _READY_TUPO: "tupo",
    _BLOCKED_TUPO: "tupo",
    _ACTIVE: "tupo",
}


def _started_at(raw: object) -> float | None:
    """When this row's timer began, or None when the stored value cannot say.

    The same reading ``inner_skills._started_at`` makes, for the same reason. A
    missing, unparseable or non-positive value means the elapsed time is
    UNKNOWN. This module used to read it as ``float(raw.get("started_at") or 0)``
    instead, which is the opposite answer twice over: a row with no start time
    looked like a run that began at the epoch, so its wait read as already over
    and the graph offered the breakthrough -- which the real route refuses with
    HTTP 409 precisely because it knows nothing about the elapsed time.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("started_at")
    if value is None:
        return None
    try:
        started = float(value)
    except (TypeError, ValueError):
        return None
    return started if started > 0 else None


def _remaining(required: float, started_at: float | None, now: float) -> float:
    """Seconds left on a real timer, or the FULL wait when the start is unknown.

    An unreadable timer reports the full wait, never zero, because ``claim`` --
    the only route that advances a finished timer -- also declines to move a row
    whose start time it cannot read. Reporting zero told the panel "done" on
    exactly the rows nothing can finish.
    """
    if started_at is None:
        return required
    return required - (now - started_at)


def _goal_id(skill_id: str, stage: str) -> str:
    """The single goal id this skill is reported as, at this stage.

    Raises on a stage outside the ladder rather than defaulting: a wrong guess
    here names a goal that does not exist, which is the one thing this module
    is not allowed to do.
    """
    return f"inner_skill_{_STAGE_VERB[stage]}:{skill_id}"


def _stored_skills(player: dict) -> dict:
    """The skill rows this player actually has stored, or none when unreadable.

    Shared by the goal list and the item route so both read the block the same
    way.

    Both levels are checked BY TYPE. This read used to be
    ``player.get("inner_skills") or {}`` and then ``.get("skills")``: a truthy
    non-mapping block (a string, a list, a number) has no ``.get``, so the read
    raised AttributeError and every goal read answered HTTP 500 for that
    player, with no way for the player to clear it through the API. The
    one-level-down corruption (``{"skills": "not a dict"}``) was already read as
    empty, so the 500 turned on the block's TYPE and nothing else -- which is
    what made it a missing type check rather than a deliberate refusal.

    ``inner_skills._player_state`` already repairs this exact shape and says why:
    "A block that is not a dict (a list, a string) has no .get, so reading it
    as a dict raised AttributeError and answered 500 on the whole state read."
    Reading an unreadable block as "no skills stored" is also the honest
    answer rather than a guess: the module's standing rule is that a requirement
    must be readable out of a real source, and an empty block makes every skill
    read ``_UNLEARNED`` -- the same answer a brand-new player gets, derived from
    the real table rather than invented here. It costs the player their panel
    detail for a corrupt row, and a panel detail is recoverable; an HTTP 500 is
    not.
    """
    state = player.get("inner_skills")
    if not isinstance(state, dict):
        return {}
    skills = state.get("skills")
    return skills if isinstance(skills, dict) else {}


def _resolve_realm(raw: object) -> str:
    """Map a stored realm string onto a _REALM_ORDER member.

    Production writes BOTH conventions: "Foundation Establishment" / "Mortal"
    and "foundation_establishment" / "mortal". An exact lookup therefore misses
    the spaced form, the caller's realm gate falls through to False, and every
    gate reports unmet -- a Foundation player is told to "Reach mortal".
    Normalise before comparing.
    """
    if not isinstance(raw, str) or not raw:
        return "mortal"
    text = raw.strip().lower()
    if text in _REALM_ORDER:
        return text
    slug = "_".join(text.split())
    return slug if slug in _REALM_ORDER else "mortal"


def _realm_index(realm: str) -> int:
    """Position on the ladder, or -1 for a realm the ladder does not name.

    A bare ``list.index`` raised ValueError on an unknown realm name and took the
    whole goal list down with it. The real gate answers the same unknown name
    with -1, so this comparison stays total.
    """
    try:
        return _REALM_ORDER.index(realm)
    except ValueError:
        return -1


def _catalog() -> dict[str, dict[str, Any]]:
    """Item catalog keyed by id. Fail loudly rather than serving an empty map."""
    with _ITEMS_PATH.open(encoding="utf-8") as handle:
        items = json.load(handle)["items"]
    return {item["id"]: item for item in items}


def _material_sources(item_id: str) -> list[str]:
    """Real, existing ways to obtain ``item_id``, read off the mission board.

    A goal used to hardcode ``["shop"]`` as the source of its learning
    material. The shop catalogue sells no insight_scroll at all, so the panel
    pointed a new player at a shelf that does not carry the item, and the first
    real goal looked impossible.

    Every learning material the inner-skill table names IS paid by a real board
    objective: ``mission_board.TIER_MATERIAL`` maps each cycle to a real
    catalog item, and ``BOARD_OBJECTIVES`` is the list of rows that pay it. So
    the honest source list is the objective ids on that board whose tier
    material is this item -- derived, never typed, so it cannot drift from the
    board the way the literal "shop" did.

    An item the board does not pay raises rather than returning a guess: the
    module's standing rule is that a requirement must be readable out of a real
    source.
    """
    sources = [f"mission_board:{row['objective_id']}"
               for row in BOARD_OBJECTIVES
               if TIER_MATERIAL.get(row.get("cycle", "")) == item_id]
    if not sources:
        raise KeyError(f"no existing source pays material {item_id!r}")
    return sources




def _requirement(item_id: str, required: int, owned: int, source_ids: list[str],
                 catalog: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    table = _catalog() if catalog is None else catalog
    if item_id not in table:
        raise KeyError(f"goal requirement references unknown item {item_id!r}")
    if required <= 0:
        raise ValueError(f"requirement {item_id!r} must be positive, got {required}")
    return {
        "id": item_id,
        "label": table[item_id]["name"],
        "required": required,
        "owned": owned,
        "unit": item_id,
        "satisfied": owned >= required,
        "source_ids": list(source_ids),
    }


def _goal(goal_id: str, scope: str, requirements: list[dict[str, Any]],
          prerequisite_steps: list[dict[str, Any]], completed: bool) -> dict[str, Any]:
    # A goal whose costs OR prerequisite gates are unmet names no action,
    # mirroring the tutorial: an empty string means "nothing is legal yet",
    # not "anything goes". Both halves matter: the realm gate lives in
    # prerequisite_steps, so a cost-only check advertised a learn action the
    # server refuses with HTTP 409.
    actionable = (not completed
                  and all(step["completed"] for step in prerequisite_steps)
                  and all(requirement["satisfied"] for requirement in requirements))
    return {
        "goal_id": goal_id,
        "scope": scope,
        "definition_version": 1,
        "requirements": requirements,
        "prerequisite_steps": prerequisite_steps,
        "eligible_action": goal_id if actionable else "",
        "completed": completed,
    }


def _stage(raw: object, definition: Any, now: float) -> str:
    """Where this skill really is, from its stored row and the real duration.

    A missing row is unlearned. A row whose ``state`` the ladder does not know
    is ALSO unlearned rather than a guess: inner_skills itself defaults an
    absent skill to ``{"state": "unlearned"}``, so that is the honest reading,
    and it is the only reading that cannot advertise an action the route refuses.

    Every branch here answers the same question the real route asks, field for
    field: ``begin_tupo`` requires ``state == "comprehend_complete"`` AND
    ``level == 1``, it promotes a ``comprehending`` row itself only when a
    readable start time proves the wait is over, and it refuses a ``tupo`` row
    outright (only ``claim`` moves that). Reading fewer fields than the route
    reads is what turned this into three separate 409s.
    """
    if not isinstance(raw, dict):
        return _UNLEARNED
    state = raw.get("state")
    if state == _ACTIVE:
        return _ACTIVE
    if state == "tupo":
        # begin_tupo refuses any row that is not comprehend_complete; only
        # claim() promotes a tupo row, so there is never a breakthrough to
        # press. A tupo row whose timer has ALREADY run out is a different
        # answer from a running one: the wait is over and only the claim
        # remains. Reporting it ready_tupo advertised a second breakthrough
        # that collects HTTP 409 for the rest of the wait.
        started = _started_at(raw)
        return _BLOCKED_TUPO if (started is not None
                                 and now - started >= definition.tupo_seconds) \
            else _BREAKING
    if state == "comprehend_complete":
        # begin_tupo also insists on level == 1. A row carrying the state but
        # not the level is refused with "Skill is not ready for Tupo", so the
        # state alone cannot mean "ready".
        return _READY_TUPO if raw.get("level") == 1 else _BLOCKED_TUPO
    if state == "comprehending":
        # begin_tupo promotes a comprehending row itself once learn_seconds have
        # passed, so a FINISHED comprehension is already actionable. Read the
        # duration from the same table the route charges against. An UNREADABLE
        # start time means the elapsed wait is unknown, so it is NOT finished:
        # the route refuses it for exactly that reason, and reading it as zero
        # elapsed believed the wait was over precisely when the server said it
        # knew nothing.
        started = _started_at(raw)
        if started is not None and now - started >= definition.learn_seconds:
            return _READY_TUPO
        return _LEARNING
    return _UNLEARNED


def _timer_step(step_id: str, label: str, remaining: float) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "completed": remaining <= 0,
        "remaining_seconds": int(max(0, remaining)),
    }


def inner_skill_goals(player: dict, owned: Callable[[str], int],
                      now: float | None = None) -> list[dict[str, Any]]:
    """One goal per real inner skill, with costs taken from the real gate table."""
    now = time.time() if now is None else now
    skills = _stored_skills(player)
    # The canonical realm key is "cultivation_realm" (9 writers across
    # server/). "realm" is written by no production code, so reading it here
    # pinned every player to "mortal" and locked the goal panel at its first
    # realm forever.
    realm = _resolve_realm(player.get("cultivation_realm"))
    # One read of the shared item file per call. The requirement helper used to
    # re-read and re-parse the whole catalog once per requirement per goal.
    catalog = _catalog()
    goals = []
    for skill_id, definition in SKILLS.items():
        raw = skills.get(skill_id)
        stage = _stage(raw, definition, now)
        # Upstream: the realm gate is checked by the real skill code, so the
        # prerequisite step is reported from that same ordering.
        realm_step = {"id": f"realm:{definition.required_realm}",
                      "label": f"Reach {definition.required_realm}",
                      "completed": _realm_index(realm) >= _realm_index(definition.required_realm)}
        learn_step = {"id": f"inner_skill_learn:{skill_id}",
                      "label": f"Learn {definition.name}",
                      "completed": True}
        started_at = _started_at(raw)

        if stage == _UNLEARNED:
            goals.append(_goal(
                _goal_id(skill_id, stage), "inner_skill",
                [_requirement(definition.learn_material, definition.learn_material_amount,
                              owned(definition.learn_material),
                              _material_sources(definition.learn_material), catalog)],
                [realm_step], False))
        elif stage == _LEARNING:
            goals.append(_goal(
                _goal_id(skill_id, stage), "inner_skill",
                [_requirement(definition.learn_material, definition.learn_material_amount,
                              owned(definition.learn_material),
                              _material_sources(definition.learn_material), catalog)],
                [realm_step, learn_step,
                 _timer_step(f"comprehending:{skill_id}", f"Comprehending {definition.name}",
                             _remaining(definition.learn_seconds, started_at, now))], False))
        elif stage == _BREAKING:
            # The breakthrough material's sources are read off the mission board
            # like every other item's. They used to be the literal
            # ``[f"inner_skill_learn:{skill_id}"]``, which named the LEARN goal
            # -- and at this stage the reply carries no learn goal for the skill,
            # so every breakthrough goal pointed at an id the same reply did not
            # contain. A derivation cannot name a goal at all, let alone a
            # missing one.
            goals.append(_goal(
                _goal_id(skill_id, stage), "inner_skill",
                [_requirement(definition.tupo_material, definition.tupo_material_amount,
                              owned(definition.tupo_material),
                              _material_sources(definition.tupo_material), catalog)],
                [realm_step, learn_step,
                 _timer_step(f"tupo:{skill_id}", f"Breaking through {definition.name}",
                             _remaining(definition.tupo_seconds, started_at, now))], False))
        elif stage == _BLOCKED_TUPO:
            # The breakthrough stage, and the real route accepts NO action for
            # this row. Two different stored rows land here, blocked for
            # different reasons, so the step names the gate the server checks
            # and this row has not met. Without it the goal looked actionable:
            # a timer at zero is a COMPLETED step, so every gate read "met".
            if raw.get("state") == "tupo":
                # The wait is over; only ``claim`` records a finished
                # breakthrough, and this module advertises no claim action.
                blocker = {"id": f"inner_skill_claim:{skill_id}",
                           "label": f"Claim {definition.name}", "completed": False}
            else:
                # A comprehend_complete row whose level never landed: nothing
                # the panel can press satisfies ``level == 1``.
                blocker = {"id": f"inner_skill_level:{skill_id}",
                           "label": f"Breakthrough level missing for {definition.name}",
                           "completed": False}
            goals.append(_goal(
                _goal_id(skill_id, stage), "inner_skill",
                [_requirement(definition.tupo_material, definition.tupo_material_amount,
                              owned(definition.tupo_material),
                              _material_sources(definition.tupo_material), catalog)],
                [realm_step, learn_step,
                 _timer_step(f"tupo:{skill_id}", f"Breaking through {definition.name}",
                             _remaining(definition.tupo_seconds, started_at, now)),
                 blocker], False))
        else:
            # _READY_TUPO offers the breakthrough; _ACTIVE is finished forever.
            goals.append(_goal(
                _goal_id(skill_id, stage), "inner_skill",
                [_requirement(definition.tupo_material, definition.tupo_material_amount,
                              owned(definition.tupo_material),
                              _material_sources(definition.tupo_material), catalog)],
                [realm_step, learn_step], stage == _ACTIVE))
    return goals


def goal_view(player: dict, owned: Callable[[str], int],
              now: float | None = None) -> dict[str, Any]:
    """The single most actionable unblocked goal, or the blocked goal to explain.

    Prefers the first goal the server would accept, so the highlighted button
    works. Failing that, returns the first blocked goal, so a blocked player
    always gets an explanation rather than an empty panel.
    """
    goals = inner_skill_goals(player, owned, now)
    if not goals:
        return {"goals": [], "active": None, "blocked": []}
    ready = [g for g in goals if g["eligible_action"]]
    # Every goal that is not ready is blocked, for either reason: an unmet realm
    # gate, an unmet material cost, or a duration still running. All are the
    # "why is this locked" answer the panel exists to give, so all must be
    # explained rather than hidden.
    blocked = [g for g in goals if not g["eligible_action"]]
    active = ready[0] if ready else (blocked[0] if blocked else goals[0])
    return {"goals": goals, "active": active, "blocked": blocked}


def item_uses(item_id: str, player: dict | None = None,
              now: float | None = None) -> list[dict[str, Any]]:
    """What else this item is for. The 'why is this locked' answer, inverted.

    ``player`` is the row this answer is FOR, and it is what makes every id
    here followable. Without it the function still reports every consumer in
    the skill table, which is right for "what could this item ever be for" but
    wrong for a player standing in front of the panel.

    Why the row is required for the real answer
    --------------------------------------------
    ``inner_skill_goals`` emits exactly ONE goal per skill: the learn goal until
    the breakthrough has started, the breakthrough goal from then on (see
    ``_STAGE_VERB``). This function used to name BOTH halves for an item that
    paid both -- so on a fresh player, whose reply carries only learn goals,
    every skill item advertised a breakthrough goal id that existed nowhere in
    the reply the same client had just been given. Measured: all three skill
    items on a fresh player. A client that follows the id from the item panel to
    highlight the prerequisite follows it into nothing.

    So when a row is supplied, the stage is read with the SAME ``_stage`` the
    goal list uses and the id is built with the SAME ``_goal_id`` helper it
    uses. Both sides therefore derive from one table, and this function cannot
    name a goal the goal list did not emit.

    ``now`` is the same clock read the goals route uses for that player. A
    caller passing one keeps the two replies in step across a comprehension
    boundary instead of straddling it.

    ``player=None`` keeps the old unfiltered answer, so the legacy route that
    does not thread a row keeps working instead of raising.
    """
    present: set[str] | None = None
    if player is not None:
        now = time.time() if now is None else now
        skills = _stored_skills(player)
        # The same stage read the goal list performs, so the id set this
        # filters against is the id set the goal list will actually emit.
        present = {_goal_id(skill_id, _stage(skills.get(skill_id), definition, now))
                   for skill_id, definition in SKILLS.items()}

    uses = []
    for skill_id, definition in SKILLS.items():
        if definition.learn_material == item_id:
            goal_id = f"inner_skill_learn:{skill_id}"
            if present is None or goal_id in present:
                uses.append({"goal_id": goal_id,
                             "label": f"Learn {definition.name}",
                             "required": definition.learn_material_amount})
        if definition.tupo_material == item_id:
            goal_id = f"inner_skill_tupo:{skill_id}"
            if present is None or goal_id in present:
                uses.append({"goal_id": goal_id,
                             "label": f"Break through {definition.name}",
                             "required": definition.tupo_material_amount})
    return uses
