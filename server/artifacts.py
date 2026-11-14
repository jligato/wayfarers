"""Original Gubao/Fabao artifact state and activation rules for Wayfarer's Cycle.

Artifacts are separate from ordinary equipment. The boundary, fixed by
``design/artifact-equipment-boundary-design.md``, is one question: does the
item go into a slot the player fills with something else?

  * YES -> equipment (``server/equipment.py``). Slotted, improvable, wears out.
  * NO  -> artifact (this file). NOT slotted, not improvable, never wears out.

Artifacts are therefore non-slotted as of the v2 state. The old private-slot
catalogue field (``relic_1/2/3``) is gone: a numbered slot list cannot answer
what happens to the fourth artifact, and it collided by name with equipment's
own ``relic`` slot. How many artifacts a player may have switched on at once is
one number, :data:`DEFAULT_ARTIFACT_CAP`, stored per player as ``cap``.

The server owns ownership, activation, binding, and replay receipts. Clients
receive derived views and may name WHICH artifact, never HOW MUCH: the
activation body below has three fields and adding a magnitude field to it would
be the exploit.
"""
import math
from copy import deepcopy
from typing import Any, Callable
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

#: How many artifacts a player may have switched on at once. Replaces the
#: three-item slot list. The design recommends 2 for a fresh player (the
#: measured starter spike is +42.7% attack with all three); 3 is the shipped
#: default and the cap is per-player, so it can be tuned without a code change.
DEFAULT_ARTIFACT_CAP = 3

#: Catalogue entries deliberately carry NO ``slot`` key. Read
#: ``ARTIFACT_CATALOG`` entries with ``.get("slot")`` and you now get None,
#: which is the point: the field is gone, not renamed.
ARTIFACT_CATALOG: dict[str, dict[str, Any]] = {
    "cloudseed_relic": {
        "id": "cloudseed_relic", "name": "Cloudseed Relic", "kind": "gubao",
        "description": "A quiet vessel that gathers the first breath of wind.",
        "bonuses": {"hp_mult": 1.10, "mp_mult": 1.08},
    },
    "emberlotus_seal": {
        "id": "emberlotus_seal", "name": "Emberlotus Seal", "kind": "fabao",
        "description": "A warm seal that steadies the flame within.",
        "bonuses": {"atk_mult": 1.12, "def_mult": 1.04},
    },
    "tideglass_compass": {
        "id": "tideglass_compass", "name": "Tideglass Compass", "kind": "fabao",
        "description": "A glass compass that points toward distant roads.",
        "bonuses": {"spd_mult": 1.15, "mp_mult": 1.05},
    },
}
STAT_KEYS = ("atk_mult", "def_mult", "hp_mult", "mp_mult", "spd_mult")


class ArtifactActivateRequest(BaseModel):
    """The activation body, and the WHOLE of it: three fields.

    There is no ``power``, ``magnitude``, ``target`` or ``roll_value``, and
    there must never be one. Anything the player chooses is a *which*; how much
    it gives is the server's number in ``ARTIFACT_CATALOG``. A request that
    says "activate this for 500% attack" is the exploit, and the cheapest way
    to stop it is for the body to have no field that could carry it.
    """

    player_id: str
    artifact_id: str
    idempotency_key: str = Field(min_length=1, max_length=128)


def resolve_store(accessor: Callable):
    value = accessor() if callable(accessor) else accessor
    return value() if callable(value) else value


def default_artifact_state() -> dict[str, Any]:
    return {
        "version": 2,
        "owned": {
            artifact_id: {
                "artifact_id": artifact_id,
                "activated": False,
                "bound_to": None,
                "charges": 0,
                "cooldown_until": 0,
            }
            for artifact_id in ARTIFACT_CATALOG
        },
        "receipts": {},
        "cap": DEFAULT_ARTIFACT_CAP,
    }


def ensure_state(player: dict, owner: str | None = None) -> dict[str, Any]:
    """Return this player's artifact state, migrating it in place, idempotently.

    v1 -> v2 migration, every step guarded on absence so a second call
    changes nothing:

    * ``version`` becomes 2.
    * ``receipts`` is replaced when it is not a dict.
    * ``cap`` is created, and replaced when it is not a usable count. This is
      the whole of the "artifacts stop being slotted" change: one number
      replaces the three-item slot list.
    * each owned record gains ``bound_to`` / ``charges`` / ``cooldown_until``.

    Three of those steps replace a value the module cannot use rather than
    filling an absent key. The difference is load-bearing: ``setdefault``
    leaves a key that is PRESENT alone, so a half-written save carrying a
    list where a map belongs survived the migration and crashed the first
    route that actually used it, while the read route kept answering 200
    for the same row.

    ``bound_to`` is stamped with ``owner`` when one is given, the same rule and
    the same reason equipment does it: a read passes ``None`` so a read can
    never silently re-home a bound artifact onto whoever is holding the save.
    """
    state = player.get("artifacts")
    if not isinstance(state, dict) or not isinstance(state.get("owned"), dict):
        state = default_artifact_state()
        player["artifacts"] = state
    state["version"] = 2
    # ``receipts`` is repaired, not merely backfilled. ``setdefault`` only
    # fills an ABSENT key, so a save holding a list, a string, a number or
    # null kept that value and every reader of the ledger then crashed on
    # it: the activate route's ``state["receipts"].get(key)`` raised
    # AttributeError and answered a plain-text 500, while that same
    # player's own GET answered 200 for the same row because the read
    # never touches the ledger. Same rule as ``cap`` below and for the
    # same reason: a shape the module cannot use is replaced, not trusted.
    # Repaired HERE, in the one place every route already calls, so no
    # reader has to grow its own guard.
    if not isinstance(state.get("receipts"), dict):
        state["receipts"] = {}
    # MEASURED: the map check above is not enough on its own, because a
    # dict can hold a value of the wrong type. ``activate_artifact`` reads
    # ``prior.get("artifact_id")`` on whatever the key holds, so a save
    # whose receipts map carried a string, a number or a list under one
    # key raised AttributeError and answered a plain-text 500 on EVERY
    # activate -- while that same player's own GET answered 200, because
    # the read never touches the ledger. The sibling cases already agreed
    # on one answer -- a key holding None or [] is falsy, so ``if prior``
    # treats it as "no receipt here" and the action proceeds as fresh --
    # and this line makes a string, a number and a list agree with them
    # instead of crashing. Nothing real is lost: the only writer in this
    # module stores a dict (``state["receipts"][key] = deepcopy(result)``),
    # so a non-dict value was never a receipt. Same rule, same reason, and
    # the same measured evidence as ``equipment.ensure_state``.
    for _key in [k for k, v in state["receipts"].items() if not isinstance(v, dict)]:
        state["receipts"].pop(_key, None)
    cap = state.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        state["cap"] = DEFAULT_ARTIFACT_CAP
    for owned in state["owned"].values():
        if not isinstance(owned, dict):
            continue
        if "charges" not in owned:
            owned["charges"] = 0
        if "cooldown_until" not in owned:
            owned["cooldown_until"] = 0
        if not owned.get("bound_to") and owner:
            owned["bound_to"] = owner
    return state


def _coerce_bonus(value: Any, default: float) -> float:
    """``float(value)`` or ``default`` for anything that is not a finite float.

    MEASURED on this module before this helper existed: a catalogue bonus
    holding ``inf`` or ``nan`` (as a number OR as the text ``"inf"``/
    ``"nan"``) converted WITHOUT raising, so the multiplier came back as
    ``inf``/``nan`` and the reply body carried a bare ``Infinity``/``NaN``
    token -- which is not valid JSON, so a client parsing the response
    breaks on a value the server considers a successful read. Catching the
    three conversion exceptions is NOT enough here; the finiteness test is
    what closes it. A 401-digit int raises ``OverflowError`` on conversion
    and again inside ``math.isfinite``, so both are guarded.

    Refuses ``bool``: ``float(True)`` is a usable ``1.0``, but a rate that is
    really a boolean is a data bug, not a multiplier.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    try:
        if not math.isfinite(number):
            return default
    except (TypeError, ValueError, OverflowError):
        return default
    return number


def _coerce_cap(value: Any, default: int) -> int:
    """``int(value)`` or ``default`` for anything that is not a usable cap.

    MEASURED on this module before this helper existed: a stored ``cap`` of
    ``inf``, ``-inf`` or ``nan`` -- as a number or as text -- made the bare
    ``int()`` raise ``OverflowError`` or ``ValueError``, and a ``None`` or a
    list made it raise ``TypeError``. All three are a plain-text 500 on the
    activate route. Python uses three exception classes here, so an
    ``except (TypeError, ValueError)`` would leave the third one open.

    ``math.isfinite`` is guarded too: it raises ``OverflowError`` on a
    401-digit integer, which would trade a caught error for an uncaught one.
    Refuses ``bool`` for the same reason ``ensure_state`` does -- ``int(True)``
    is a silent ``1``, i.e. "evict everything already on".
    """
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    try:
        if not math.isfinite(number):
            return default
    except (TypeError, ValueError, OverflowError):
        return default
    return number


def _derive(state: dict[str, Any]) -> dict[str, Any]:
    active = []
    multipliers = {key: 1.0 for key in STAT_KEYS}
    for artifact_id, owned in state.get("owned", {}).items():
        definition = ARTIFACT_CATALOG.get(artifact_id)
        # ``ensure_state`` skips a non-dict record rather than crashing on it,
        # so every reader has to agree: a half-written save must render as
        # "not active", never as a 500.
        if not isinstance(owned, dict):
            continue
        if not definition or not owned.get("activated"):
            continue
        active.append({**deepcopy(definition), "activated": True})
        # Only the five STAT_KEYS are accumulated. ``multipliers`` is seeded
        # from STAT_KEYS alone, so a catalogue entry naming a bonus key
        # outside that vocabulary raised KeyError on the very next line --
        # which the activate route's own ``except KeyError`` reported as 404
        # "Player not found" for a player who plainly exists.
        # ``loadout_resolver.artifact_multipliers`` already ignores keys it
        # does not recognise; this reader now agrees with it on purpose
        # instead of agreeing with it by accident.
        for key, value in definition["bonuses"].items():
            if key in multipliers:
                multipliers[key] *= _coerce_bonus(value, 1.0)
    return {"active": active, "active_count": len(active), "multipliers": multipliers}


def state_view(player: dict, player_id: str | None = None) -> dict[str, Any]:
    state = ensure_state(player, player_id)
    derived = _derive(state)
    return {
        "ok": True, "version": state["version"],
        # `slots` is gone and is NOT replaced by a similar-looking key. There
        # is no slot list any more; the limit is one number, `cap`.
        "cap": state["cap"],
        "catalog": [deepcopy(definition) for definition in ARTIFACT_CATALOG.values()],
        # Same rule as ``ensure_state`` and ``_derive``: a non-dict record is
        # skipped, not passed through. Copying it verbatim put a bare string
        # into a list where every other element is an object, so a client
        # reading ``owned[i].activated`` breaks on the corrupt entry while
        # the route still answers 200 and reports nothing wrong.
        "owned": [deepcopy(item) for item in state["owned"].values()
                  if isinstance(item, dict)],
        **derived,
    }


class ArtifactNotBound(HTTPException):
    """A record exists for this artifact but is stamped to a different player."""

    def __init__(self, artifact_id: str) -> None:
        super().__init__(status_code=403, detail=f"Artifact {artifact_id} is bound to another player")


def _owned_artifact(state: dict[str, Any], player_id: str, artifact_id: str) -> dict[str, Any]:
    """Fetch one of THIS player's artifacts, or refuse.

    404 when the player simply does not own it; 403 when a record exists but
    its ``bound_to`` stamp names somebody else. The two codes are deliberately
    different, copied from equipment's ``_owned_item``: a 404 for a stolen item
    would tell an attacker the id is real somewhere.

    A non-dict record is answered 404 as well. ``ensure_state``, ``_derive``,
    ``state_view`` and ``_enforce_cap`` all skip one, so it is not a usable
    artifact by any of their rules; this reader raised ``AttributeError`` on
    ``.get`` instead and the activate route turned that into a plain-text 500
    for a player whose own GET route had just answered 200. Skipping it here
    is what makes all five agree.
    """
    owned = state["owned"].get(artifact_id)
    if not owned or not isinstance(owned, dict):
        raise HTTPException(status_code=404, detail="Player does not own artifact")
    bound_to = owned.get("bound_to")
    if bound_to and bound_to != player_id:
        raise ArtifactNotBound(artifact_id)
    if not bound_to:
        owned["bound_to"] = player_id
    return owned


def _enforce_cap(state: dict[str, Any], incoming_id: str) -> list[str]:
    """Make room for ``incoming_id`` and switch the oldest active off if needed.

    This replaces the old "clear whatever shares my slot" loop. Under a cap,
    activating artifact number four when the cap is three must deactivate the
    oldest, inside the same transaction as the activation.

    Returns the ids that were switched off, so a caller or a test can see the
    eviction rather than infer it from a count.

    Two crashes used to live here and both became a 500 on a live route:

    * a non-dict ``owned`` record raised ``AttributeError`` on ``.get``. The
      record survives ``ensure_state`` (which skips it), so it reaches this
      function; it is skipped here for the same reason.
    * ``cap`` below 1 drove the eviction loop until ``active`` was empty and
      ``min()`` raised ``ValueError`` on nothing. A cap of 0 or 1 means
      "evict everything already on", which is a list, not a crash.
    """
    cap = _coerce_cap(state.get("cap", DEFAULT_ARTIFACT_CAP), DEFAULT_ARTIFACT_CAP)
    owned_state = state["owned"]
    # Stable order for "oldest": catalogue order, which is fixed and does not
    # depend on dict insertion order in the save file.
    catalogue_order = {artifact_id: index for index, artifact_id in enumerate(ARTIFACT_CATALOG)}
    # ``artifact_id in ARTIFACT_CATALOG`` is load-bearing. ``_derive`` counts
    # an artifact only when the catalogue defines it, so a record left behind
    # by a removed or renamed entry used to hold a cap slot here while
    # granting no bonus at all: with cap 3 and one such record the player
    # could only ever switch TWO real artifacts on, because the third
    # activation evicted a perfectly good artifact to make room for nothing.
    # Counting exactly what ``_derive`` counts is the whole fix.
    active = [
        artifact_id
        for artifact_id, owned in owned_state.items()
        if (isinstance(owned, dict)
            and owned.get("activated")
            and artifact_id in ARTIFACT_CATALOG
            and artifact_id != incoming_id)
    ]
    evicted: list[str] = []
    # `active` shrinking is the loop's real exit condition. Without it, a cap
    # smaller than the number of active artifacts walks off the end of the list.
    while active and len(active) + 1 > cap:
        oldest = min(active, key=lambda aid: catalogue_order.get(aid, len(catalogue_order)))
        owned_state[oldest]["activated"] = False
        evicted.append(oldest)
        active.remove(oldest)
    return evicted


def activate_artifact(players_db: Callable, player_id: str, artifact_id: str, key: str) -> dict[str, Any]:
    store = resolve_store(players_db)
    if artifact_id not in ARTIFACT_CATALOG:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # The 404 is raised HERE, before the body runs, instead of by catching
    # KeyError around the whole transaction. That blanket catch also swallowed
    # the KeyError from a receipt missing ``artifact_id`` and the one from a
    # catalogue bonus key outside STAT_KEYS, then answered 404 "Player not
    # found" for a player who demonstrably exists -- a false statement that
    # sent every reader hunting the wrong bug. ``__contains__`` is the
    # store's own existence check; whatever goes wrong inside the body now
    # surfaces as the 500 it actually is instead of wearing a 404.
    if player_id not in store:
        raise HTTPException(status_code=404, detail="Player not found")
    with store.transaction(player_id):
        player = store[player_id]
        state = ensure_state(player, player_id)
        prior = state["receipts"].get(key)
        if prior:
            if prior.get("artifact_id") != artifact_id:
                raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
            # The receipt's DECISION is final and replayed verbatim: which
            # artifact, which key, which artifacts were evicted. Its
            # `artifacts` snapshot is not a decision, it is a report of
            # state that has moved on since, so it is re-derived here.
            # Returning the stored snapshot made a replay claim an
            # active_count the player's own GET route no longer agreed
            # with, which is a lie in the one response that promises to
            # be the truth.
            replay = {k: deepcopy(v) for k, v in prior.items() if k != "artifacts"}
            replay["artifacts"] = state_view(player, player_id)
            return replay
        owned = _owned_artifact(state, player_id, artifact_id)
        evicted = _enforce_cap(state, artifact_id)
        owned["activated"] = True
        result = {
            "ok": True, "artifact_id": artifact_id, "idempotency_key": key,
            "evicted": evicted, "artifacts": state_view(player, player_id),
        }
        state["receipts"][key] = deepcopy(result)
        if len(state["receipts"]) > 50:
            for old_key in list(state["receipts"])[:-50]:
                state["receipts"].pop(old_key, None)
        return result


def register_artifact_routes(app: FastAPI, players_db: Callable) -> None:
    @app.get("/artifacts/{player_id}")
    def artifacts_view(player_id: str):
        store = resolve_store(players_db)
        # Raised before the transaction opens, for the same reason as the
        # activate route: a blanket ``except KeyError`` around this body also
        # caught the KeyError from a malformed owned record and answered 404
        # for a player who exists.
        if player_id not in store:
            raise HTTPException(status_code=404, detail="Player not found")
        with store.transaction(player_id):
            player = store[player_id]
            # ``None`` as the owner, deliberately. This is a read: it may
            # repair the SHAPE of the block (version, cap, charges) and the
            # write below persists that repair, but it must not stamp
            # `bound_to`. Passing the id here made every bare GET claim
            # ownership of all three artifacts and write that claim to the
            # save, which is exactly what ensure_state's docstring says a
            # read must never do.
            result = state_view(player)
            store[player_id] = player
        return result

    @app.post("/artifacts/activate")
    def activate(req: ArtifactActivateRequest):
        return activate_artifact(players_db, req.player_id, req.artifact_id, req.idempotency_key)
