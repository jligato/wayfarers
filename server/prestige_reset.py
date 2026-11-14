"""
Prestige hard reset — the engine, and the safety valve that makes it safe.

Prestige is a HARD RESET: it wipes the progress a player accumulated and
returns a permanent advantage in its place. It is the most destructive
routine in the game, so this module is written to be *narrow* and *undoable*
rather than merely functional.

The three failure modes this is built against
--------------------------------------------
1. A player loses everything because of a bug and cannot get it back. There
   is no undo in an idle game, so :func:`apply_prestige` writes a COMPLETE
   deep-copy snapshot of every field it is about to touch, in the SAME
   transaction as the reset, and :func:`undo_prestige` puts it back whole.
   The snapshot is complete rather than a diff on purpose: a diff replayed
   onto a row that has since changed is a corruption, not a restore.

2. The reset becomes a currency faucet. A player farms, resets, farms again.
   Three guards, from measurement rather than intuition (see
   ``prestige-reset-faucet-audit``):
     * The permanent gain is ADDITIVE and CAPPED (:data:`INSIGHT_CAP`), so it
       saturates. Any ``1.1 ** n`` permanent gain is unbounded by
       construction.
     * The gain is keyed on the player's own ``ascension_count``, never on a
       calendar season.
     * The reset touches an explicit ALLOWLIST of progress fields, so it
       cannot clear a once-only marker by accident. The allowlist fails
       safe: a field nobody thought about survives.

3. The reset makes the game less fun than before. The permanent gain is
   reduced *deviation risk* (:func:`insight_bonus`), not more damage, and it
   makes the REBUILD safer rather than the player stronger. The destination
   content is unchanged.

Why the allowlist is the whole safety argument
----------------------------------------------
A denylist ("clear everything except X") fails OPEN: the next field another
agent adds is wiped by default, and a wipe is unrecoverable in an idle game.
An allowlist fails CLOSED: the next field another agent adds is preserved by
default, and preserving is the safe direction. So this module names the small
set of progress fields it resets and touches nothing else.

The wallet is deliberately never touched
-----------------------------------------
``player["spirit_stones"]`` and the separate ``wallet_store`` both hand back
DETACHED objects on read (``player_store.py`` copy-on-read,
``wallet_defense.py`` deepcopies, ``wallet_store_pg.py`` rebuilds a fresh
``Wallet``). A reset that "cleared the wallet" by assigning to a value it had
just read would write nothing while appearing to work. Rather than rely on
that subtlety, :func:`apply_prestige` never calls the wallet store at all,
and ``test_prestige_hard_reset.py`` asserts the wallet is unchanged across a
prestige.

Store discipline
----------------
Every mutation here runs inside ``db.transaction(pid)`` and is published with
``db.persist(pid, player)``. A write to a value read back outside a
transaction is silently discarded on this tree — the class of bug that once
paid real currency twice. The row is therefore always re-read INSIDE the
transaction, never from the snapshot the caller was handed.
"""

from __future__ import annotations

import math

from stored_numbers import stored_float as _as_finite_float
import secrets
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# The unlock gate
# ---------------------------------------------------------------------------

#: The realm a player must have REACHED before prestige is offered.
#:
#: Not the realm they start at and not the top of the ladder. Core
#: Formation is where the ladder schedules its first paradigm shift, and it
#: is late enough that the player has built something worth trading and hit a
#: wall worth trading it for. Opening earlier burns the arc; opening later
#: means nobody finds the feature.
UNLOCK_REALM: str = "core"

#: The realm and stage a prestige returns the player to.
BASE_REALM: str = "mortal"
BASE_STAGE: str = "early"
BASE_ZONE: str = "mortal_village"

# ---------------------------------------------------------------------------
# The permanent gain: Dao Insight (道痕)
# ---------------------------------------------------------------------------

#: Insight granted per prestige. Additive and flat.
INSIGHT_PER_PRESTIGE: float = 0.03

#: Hard ceiling on the insight bonus. The gain SATURATES here.
#:
#: This is the anti-faucet rule made concrete. A multiplicative permanent
#: gain (1.1 ** n) is unbounded by construction — it was measured at 2478x
#: on this tree when a payout was keyed on the absolute calendar season. A
#: flat additive bonus with a ceiling cannot be farmed past, because the
#: tenth prestige is worth exactly as much as the fifth.
INSIGHT_CAP: float = 0.15

#: The word a player must type to confirm. Deliberately not a button.
CONFIRM_PHRASE: str = "BREAK MY SEALS"

#: How long the undo window stays open, in seconds.
UNDO_WINDOW_SECONDS: float = 24 * 3600.0

#: How many undos a player gets. Once-only: a second undo is a 409.
UNDO_LIMIT: int = 1

#: Stand-in for an undo expiry that cannot be read as a finite number.
#:
#: Direction matters more than the value. A recovery slot whose
#: ``expires_at`` is corrupt must NOT read as expired: ``apply_prestige``
#: treats an expired slot as permission to reset again, and a second
#: prestige OVERWRITES the only snapshot that could undo the first. So
#: unreadable means still-open, which blocks the reset and protects the
#: undo record. The cost is a temporary block on a corrupt row, which
#: ``undo_prestige`` also clears -- cheaper than an unrecoverable wipe.
#:
#: Finite on purpose: ``float("inf")`` also reads as still-open, but
#: ``seconds_remaining`` would then be inf, which is not valid JSON and
#: poisons the very reply this guard exists to protect.
UNDETERMINED_EXPIRY: float = 1.7976931348623157e308  # sys.float_info.max

# ---------------------------------------------------------------------------
# The allowlist — the entire blast radius of a prestige
# ---------------------------------------------------------------------------

#: Progress fields a prestige resets to their base values. This tuple IS the
#: contract: a field not named here survives, and
#: ``test_prestige_hard_reset.py`` names every one of them in a test.
#:
#: Chosen because each is *progress within a life* and nothing else:
#:   cultivation_realm / cultivation_stage — where the player climbed to
#:   progress / qi                          — the bar for the next stage
#:   current_zone                           — where they were standing
#:   breakthrough_state                     — per-life gate pillars, scars
#:                                           and cooldowns; a scar is a
#:                                           memory of THIS life's failures
RESET_FIELDS: Tuple[str, ...] = (
    "cultivation_realm",
    "cultivation_stage",
    "progress",
    "qi",
    "current_zone",
    "breakthrough_state",
)

#: Fields that must survive, named so a test can assert each one. This is
#: documentation that executes: ``SURVIVES_FIELDS`` is iterated by the
#: survive/don't-survive test, so a field added to this tuple that a reset
#: actually destroys turns the suite red.
#:
#: The three groups, and why each one is here:
#:   identity  — a player who resets is still the same person
#:   money     — a prestige that cost a player currency is a bug, always
#:   purchases — cosmetics/inventory/beasts were bought or earned and are
#:               not progress
SURVIVES_FIELDS: Tuple[str, ...] = (
    # identity
    "id", "name", "spirit_root", "path", "signing_key",
    # money — the reset must never touch these
    "spirit_stones", "wallet",
    # purchases and companions
    "cosmetics", "inventory", "beasts", "beast_formation",
    # earned records
    "achievements", "missions", "tower_state", "goal_graph",
    # the death/migration clock is a DIFFERENT system
    "lifecycle", "return_receipt",
    # ascension history and the permanent gain itself
    "legacy", "dao_insight", "prestige",
    # social standing is not progress
    "sect_id", "sect_rank", "sect_contribution", "pvp_rating",
    "dao_companion_id",
    # what the player has MET is knowledge, not progress within this life --
    # a prestige that erased the bestiary would delete a record of play
    "codex",
)


class PrestigeError(ValueError):
    """A refusal that must become a 4xx, not a 500.

    Raised inside the transaction so the caller converts it AFTER the unit
    closes; raising inside a transaction poisons it and would unwind a write
    that should have been committed.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _as_finite_int(value: Any, default: int = 0) -> int:
    """A stored number as an ``int``, or ``default`` if it is not one.

    A stored counter is not worth a 500, and this module's own rule
    is that every refusal is a 4xx -- but these sites raise OUTSIDE any
    ``except``, so ``int()`` here is a 500 rather than a refusal.

    ``int()`` rejects non-finite input by raising, and the two
    exceptions are different: ``inf`` raises ``OverflowError`` while
    ``nan`` raises ``ValueError``. An ``except (TypeError, ValueError)``
    arm therefore catches only one of them, which is why the test
    below is ``math.isfinite`` rather than an exception arm.

    The ``float()`` round trip is what makes the huge-``int`` case safe
    too: ``int(10 ** 400)`` raises nothing and returns a 401-digit
    number, while ``float(10 ** 400)`` raises ``OverflowError`` and is
    caught here. Checking ``math.isfinite`` on the ORIGINAL value
    instead would raise on that input and trade one 500 for another.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


#: ``_as_finite_float`` is ``stored_numbers.stored_float`` under this
#: name: measured behaviourally identical on every non-finite
#: spelling, so this module keeps one implementation. See that
#: module for why the guard needs both a three-exception tuple
#: and an explicit finiteness test.

def insight_points(player: dict) -> float:
    """Raw accumulated insight, ungated. Stored on the player as a float."""
    try:
        points = float(player.get("dao_insight", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # float() does not raise on inf/nan, so the guard above cannot see them.
    # Returning them would write the poison into every later read of the field.
    return points if math.isfinite(points) else 0.0


def insight_bonus(player: dict) -> float:
    """The bonus this player's insight grants, in 0.0-INSIGHT_CAP.

    This is the number ``breakthrough.attempt_chance`` adds. It is a
    REDUCTION IN RISK, not a damage multiplier: the player rebuilds faster
    and fails fewer breakthroughs, but is not stronger at anything. That is
    the genre's own framing (a cultivator who recovers through comprehension
    transcends rather than merely regains) and it is what keeps a prestige
    from making the game less interesting than before.
    """
    return min(INSIGHT_CAP, max(0.0, insight_points(player)))


def _grant_insight(player: dict) -> float:
    """Add one prestige's insight and return the NEW capped bonus.

    The stored total is allowed to exceed the cap; only the *bonus* is
    capped. That keeps the stored value monotonic and auditable (a player can
    see they earned 12 points and that 5 of them are doing nothing) instead
    of silently discarding earned progress.
    """
    player["dao_insight"] = insight_points(player) + INSIGHT_PER_PRESTIGE
    return insight_bonus(player)


# ---------------------------------------------------------------------------
# Realm helpers
# ---------------------------------------------------------------------------


def _realm_order(name: str) -> int:
    """Position of a realm on the configured ladder; -1 if unknown."""
    try:
        from cultivation_config import REALM_MAP
    except ImportError:  # pragma: no cover - import shape differs by entry
        try:
            from server.cultivation_config import REALM_MAP
        except ImportError:
            return -1
    realm = REALM_MAP.get(str(name))
    return int(realm.order) if realm is not None else -1


def has_reached_unlock(player: dict) -> bool:
    """True when this life has climbed to the unlock realm."""
    return _realm_order(str(player.get("cultivation_realm", ""))) >= _realm_order(UNLOCK_REALM)


# ---------------------------------------------------------------------------
# Snapshot / recovery
# ---------------------------------------------------------------------------


def build_snapshot(player: dict, now: float) -> dict:
    """A COMPLETE deep copy of every field the reset will touch.

    Complete, not incremental. A snapshot holding only the reset fields would
    restore a half-updated row if the player's other state moved in between;
    a full copy of the player's persisted shape restores the account to the
    instant before the reset, which is the only thing a player who pressed
    the wrong button actually wants.
    """
    return {
        "version": 1,
        "taken_at": float(now),
        "player": deepcopy(dict(player)),
    }


def recovery_state(player: dict, now: Optional[float] = None) -> Optional[dict]:
    """The player's current recovery slot, or None.

    Returned as a copy so a caller cannot mutate the stored snapshot by
    holding on to the returned dict.
    """
    if now is None:
        now = time.time()
    prestige = player.get("prestige")
    if not isinstance(prestige, dict):
        return None
    rec = prestige.get("recovery")
    if not isinstance(rec, dict) or not rec.get("snapshot"):
        return None
    out = deepcopy(rec)
    # Fails CLOSED, not open -- see UNDETERMINED_EXPIRY. Both fields are
    # guarded because this dict is RETURNED to the caller, so an
    # unguarded non-finite value reaches the JSON reply even when the
    # stored row itself was written correctly.
    out["expires_at"] = _as_finite_float(
        rec.get("expires_at"), UNDETERMINED_EXPIRY
    )
    out["taken_at"] = _as_finite_float(rec.get("taken_at"), float(now))
    out["expired"] = out["expires_at"] <= float(now)
    out["seconds_remaining"] = max(0.0, out["expires_at"] - float(now))
    return out


def recovery_pending(player: dict, now: Optional[float] = None) -> bool:
    """True when a live, unspent recovery slot is holding the reset back.

    This is the guard on the SAFETY VALVE ITSELF: a second prestige while a
    recovery is pending would overwrite the only snapshot that could undo the
    first one. A fresh player has no recovery, so this is False and the
    first prestige is allowed.
    """
    rec = recovery_state(player, now)
    if rec is None or rec.get("expired"):
        return False
    return _as_finite_int(rec.get("undo_count", 0)) < UNDO_LIMIT


def undo_available(player: dict, now: Optional[float] = None) -> bool:
    """True when the player could press Undo right now.

    The mirror image of :func:`recovery_pending`, and deliberately a separate
    function. Collapsing the two is how the first version of this module
    blocked EVERY first prestige: it asked "is there a usable recovery?" and
    a brand-new account correctly answers no, which then read as "recovery
    pending". The plan screen and the reset guard want opposite answers.
    """
    return recovery_pending(player, now)


# ---------------------------------------------------------------------------
# The reset
# ---------------------------------------------------------------------------


def plan_prestige(player: dict, now: Optional[float] = None) -> dict:
    """What a prestige would do, WITHOUT doing it.

    The preview and the reset are computed from the SAME field list, so the
    confirmation screen cannot promise something the reset does not do. A
    client that renders ``plan_prestige``'s output is rendering the truth.
    """
    if now is None:
        now = time.time()

    unlocked = has_reached_unlock(player)
    legacy = player.get("legacy") if isinstance(player.get("legacy"), dict) else {}
    ascensions = _as_finite_int(legacy.get("ascension_count", 0) or 0)
    current_bonus = insight_bonus(player)
    next_bonus = min(INSIGHT_CAP, current_bonus + INSIGHT_PER_PRESTIGE)

    # Only fields that will ACTUALLY CHANGE are listed. A player already at
    # mortal/early/0.0 has nothing to lose in those fields, and a confirmation
    # screen that says "cultivation_realm: mortal -> mortal" teaches the
    # player to skim the one panel that must be read carefully.
    will_reset = []
    for field in RESET_FIELDS:
        if field not in player:
            continue
        base = _base_value_for(field)
        if player[field] == base:
            continue
        will_reset.append({
            "field": field,
            "before": deepcopy(player.get(field)),
            "after": base,
        })

    return {
        "unlocked": unlocked,
        "unlock_realm": UNLOCK_REALM,
        "current_realm": str(player.get("cultivation_realm", "")),
        "current_stage": str(player.get("cultivation_stage", "")),
        # Exactly the fields that will change, with before/after values. The
        # client renders this list; it is not a hand-written summary.
        "will_reset": will_reset,
        # Named explicitly, because "what do I lose" is the question a player
        # actually has, and silence on it is how a hard reset becomes a
        # support ticket.
        "survives": list(SURVIVES_FIELDS),
        "gains": {
            "dao_insight_points": INSIGHT_PER_PRESTIGE,
            "insight_bonus_before": round(current_bonus, 4),
            "insight_bonus_after": round(next_bonus, 4),
            "insight_bonus_capped_at": INSIGHT_CAP,
            "ascension_count_after": ascensions + 1,
            "permanent": True,
        },
        "undo": {
            "available": undo_available(player, now),
            "window_seconds": UNDO_WINDOW_SECONDS,
            "limit": UNDO_LIMIT,
        },
        "confirm_phrase": CONFIRM_PHRASE,
    }


def _base_value_for(field: str) -> Any:
    if field == "cultivation_realm":
        return BASE_REALM
    if field == "cultivation_stage":
        return BASE_STAGE
    if field == "current_zone":
        return BASE_ZONE
    if field == "breakthrough_state":
        return None
    return 0.0


def apply_prestige(player: dict, confirm: str, now: Optional[float] = None) -> dict:
    """Perform the reset. Mutates ``player`` in place; caller persists it.

    Refuses (raises :class:`PrestigeError`) rather than half-doing the work:

    * not unlocked  -> 409
    * wrong phrase  -> 400
    * undo already spent -> 409

    Ordering is deliberate. The snapshot is taken FIRST, before any field is
    touched, so there is no window in which the player has been reset and no
    snapshot exists. A snapshot taken after the reset cannot describe the
    state before it, which is the whole point of the safety valve.
    """
    if now is None:
        now = time.time()

    if not has_reached_unlock(player):
        raise PrestigeError(
            f"prestige requires reaching {UNLOCK_REALM} in this life", status_code=409
        )
    if str(confirm or "").strip().upper() != CONFIRM_PHRASE:
        raise PrestigeError("confirmation phrase does not match", status_code=400)
    if recovery_pending(player, now):
        # Not a lockout — a statement that the previous reset's recovery is
        # spent. Resetting again here would overwrite the only snapshot that
        # could undo it, which is exactly how a safety valve gets destroyed.
        raise PrestigeError(
            "recovery from the previous prestige is still pending; use it or "
            "wait for it to expire before prestiging again",
            status_code=409,
        )

    snapshot = build_snapshot(player, now)

    # ---- the reset, allowlist only -------------------------------------
    for field in RESET_FIELDS:
        if field in player:
            player[field] = _base_value_for(field)

    # The permanent gain. Keyed on the player's own ascension ordinal.
    #
    # ``legacy`` is coerced, not assumed. This runs AFTER the reset fields
    # above have been written, so an exception here does not merely refuse
    # the request: it leaves the player WIPED with no grant, no ordinal and
    # no safety valve, which is the one outcome a hard reset must never
    # produce. A row can hold a non-dict here after a partial write, a
    # schema change or a hand edit, and this module's own rule is that
    # every refusal is a 4xx.
    #
    # The coercion matches how :func:`plan_prestige` reads the same field, and
    # the dict is written BACK onto the row because the ordinal has to land
    # where ``prestige.claim_season_transition`` reads it from.
    #
    # Only a non-dict is replaced. A real dict is left untouched, so its
    # once-only markers survive -- ``carryover_applied_season`` above all,
    # which is what stops the season claim paying twice. A non-dict carries
    # no marker to preserve, so nothing claimable is lost by replacing it.
    legacy = player.get("legacy")
    if not isinstance(legacy, dict):
        legacy = {}
        player["legacy"] = legacy
    legacy["ascension_count"] = _as_finite_int(legacy.get("ascension_count", 0) or 0) + 1
    new_bonus = _grant_insight(player)

    # The safety valve, written in the same unit of work as the reset.
    # Same coercion, same reason: `prestige` holds the undo slot, so a
    # non-dict here would destroy the only record of the pre-reset
    # account. A missing or unreadable slot means there is nothing left
    # to protect, which is the same state as a first reset.
    prestige = player.get("prestige")
    if not isinstance(prestige, dict):
        prestige = {}
        player["prestige"] = prestige
    prestige["count"] = _as_finite_int(prestige.get("count", 0) or 0) + 1
    prestige["last_at"] = float(now)
    prestige["recovery"] = {
        "snapshot": snapshot,
        "taken_at": float(now),
        "expires_at": float(now) + UNDO_WINDOW_SECONDS,
        "undo_count": 0,
    }

    return {
        "ok": True,
        "prestige_count": prestige["count"],
        "ascension_count": legacy["ascension_count"],
        "insight_bonus": round(new_bonus, 4),
        "dao_insight": round(insight_points(player), 4),
        "realm": player.get("cultivation_realm"),
        "stage": player.get("cultivation_stage"),
        "undo_expires_at": prestige["recovery"]["expires_at"],
        "reset_fields": list(RESET_FIELDS),
    }


def undo_prestige(player: dict, now: Optional[float] = None) -> dict:
    """Restore the pre-reset account. Mutates ``player`` in place.

    Restores EVERYTHING the snapshot holds, not just the reset fields, and
    deliberately does NOT grant insight: an undo is a restoration, not a
    payout. If it granted insight it would be a faucet — prestige for the
    gain, undo for the progress, repeatedly.
    """
    if now is None:
        now = time.time()

    rec = recovery_state(player, now)
    if rec is None:
        raise PrestigeError("no prestige to undo", status_code=409)
    if rec.get("expired"):
        raise PrestigeError("the undo window has closed", status_code=409)
    if _as_finite_int(rec.get("undo_count", 0)) >= UNDO_LIMIT:
        raise PrestigeError("undo has already been used for this prestige", status_code=409)

    restored = deepcopy(rec["snapshot"]["player"])

    # The player's ID is identity and is never restored FROM a snapshot —
    # it is the key the row is stored under, and a snapshot that disagreed
    # with it would write the row under the wrong key.
    player_id = player.get("id")
    if player_id is not None:
        restored["id"] = player_id

    # Carry forward anything the store added since the snapshot that is not
    # part of the restored shape, so a field introduced by another agent
    # after the reset is not silently dropped by the restore.
    #
    # ``dao_insight`` is EXCLUDED alongside ``prestige``, and that exclusion is
    # the anti-faucet rule for undo. An undo rolls the reset back completely,
    # so the insight the reset granted must roll back with it; carrying it
    # forward would leave "prestige for the gain, undo for the progress" as a
    # repeatable cycle. A player who had insight from an EARLIER prestige
    # still keeps it, because that value is inside the snapshot.
    UNDO_OWNED = {"prestige", "dao_insight"}
    for key, value in player.items():
        if key not in restored and key not in UNDO_OWNED:
            restored[key] = deepcopy(value)

    # The recovery slot itself: consumed, so a second undo is a 409 and the
    # snapshot cannot be replayed.
    restored["prestige"] = {
        "count": _as_finite_int(player.get("prestige", {}).get("count", 1)),
        "last_undo_at": float(now),
        "last_undo_taken_at": _as_finite_float(rec.get("taken_at", now), float(now)),
        "recovery": None,
    }

    player.clear()
    player.update(restored)

    return {
        "ok": True,
        "restored": True,
        "realm": player.get("cultivation_realm"),
        "stage": player.get("cultivation_stage"),
        "insight_bonus": round(insight_bonus(player), 4),
        "taken_at": rec.get("taken_at"),
    }
