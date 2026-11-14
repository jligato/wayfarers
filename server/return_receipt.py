"""Bounded latest-only receipt for changes observed in one player transaction.

Events aggregate by kind/unit across crossed lives, not a reconstructed ledger.
Amounts are signed actual deltas; progress points are never mislabeled as qi.
The caller must commit the player transaction before exposing this staged receipt.
Only this latest transaction summary is stored (at most 16 kind/unit aggregates).
``available_history`` means the prior observation had receipt instrumentation;
it does not promise a retained ledger or reconstruct intervening gameplay.
"""
from copy import deepcopy
import hashlib
import json
import math

from fastapi import HTTPException


def _finite_or_zero(value):
    """``value`` as a finite float, or 0.0 when it cannot be one.

    The last line of defence for the fallback itself. Callers pass `now`,
    `time.time()`, and values already through this module -- but a fallback
    that is itself non-finite would still be written into a committed receipt,
    and `json.dumps` of that fails the whole response.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _as_number(value, fallback):
    """A stored timestamp that is absent, null or non-numeric reads as `fallback`.

    ``last_reconciled_at`` is read straight out of the player's stored row, and
    a stored row is not guaranteed to hold a number: a migration, a restored
    backup or an older writer can leave the key present but null. `__init__`
    then did ``max(now, None)`` and raised TypeError out of the handler.

    This is the same condition `lifecycle.effective_now` already guards at
    lifecycle.py:110-114 with `except (TypeError, ValueError)`, on the same
    field, so the two readers can no longer disagree about whether a player row
    is readable.

    Falling back is not a fabricated measurement: an ABSENT key already fell
    back to `now` here, so a null key is answered exactly as a missing one is.
    It only bounds the START of the receipt's interval -- every amount in the
    receipt is still observed from what the production handlers actually did.
    """
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite stored timestamp")
        return number
    except (TypeError, ValueError, OverflowError):
        return _finite_or_zero(fallback)


def _as_delta(value):
    """A stored amount that is absent, null or non-numeric reads as ``None``.

    The difference from ``_as_number`` is the fallback: there, an unreadable
    stored timestamp is answered as an absent one; here, an unreadable stored
    AMOUNT must contribute nothing at all, because a substituted number would
    be written into a committed receipt as though it had been measured.
    """
    try:
        delta = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # Same as _as_number: float() passes inf/nan through silently. A delta
    # that is not a real amount contributes nothing rather than a lie.
    return delta if math.isfinite(delta) else None


def _as_sequence(value):
    """A stored list that is absent, null or not a list reads as empty.

    A bare STRING is deliberately not a sequence here. ``set.update("abc")``
    shreds it into single characters, so a receipt stored by an older writer
    that held ``"cultivation_progress"`` where a list belongs was written out
    again as ``['_', 'a', 'c', 'e', 'g', 'i', ...]``. Measured on
    POST /player/{id}/offline-progress: HTTP 200, and the committed receipt's
    ``reset`` field held 21 single letters.
    """
    if isinstance(value, (list, tuple, set)):
        return value
    return ()


def _player_status(player):
    """The player's stored lifecycle status, or None when it has none.

    One reader for both phase labels. `player.get("lifecycle") or {}` reads as
    tolerant but is not: a truthy non-dict lifecycle slot reaches `.get` and
    raises AttributeError. `__init__` and `finish()` both need this answer, so
    both call this.
    """
    state = player.get("lifecycle")
    return state.get("status") if isinstance(state, dict) else None


class ReturnReceipt:
    def __init__(self, player, now):
        # `or {}` covers a missing key AND an explicit None, but not a truthy
        # non-dict: `player["lifecycle"] = "active"` left `state` a string and
        # the `.get` below raised AttributeError out of every constructor.
        # Measured on this class directly with lifecycle="active":
        #   AttributeError: 'str' object has no attribute 'get'
        state = player.get("lifecycle")
        if not isinstance(state, dict):
            state = {}
        self.start = _as_number(state.get("last_reconciled_at"), now)
        self.end = max(now, self.start)
        self.before_phase = state.get("status") or "uninitialized"
        self.available_history = player.get("return_receipt") is not None
        self.events = {}
        self.reset = set()

    def adopt(self, previous):
        """Merge an existing transaction observation into this receipt.

        EVERY field read here comes out of the stored player row, and the
        stored row is written by this same class one commit earlier -- so a
        receipt this module wrote can be re-read by a version that assumed a
        shape it does not have, a restored backup can hold a half-written
        record, and a migration can leave a key present but null. The
        ``__init__`` reader above already answers that condition for
        ``last_reconciled_at``; ``adopt`` read five more stored values with
        bare subscripts and no guard at all.

        Measured on POST /player/{id}/offline-progress and on
        POST /cultivation/claim-offline/v2, each with ONE field nulled in a
        receipt this module had itself committed moments earlier:

          interval_start=None -> return_receipt.py:55 TypeError  -> HTTP 500
          interval_end=None   -> return_receipt.py:56 TypeError  -> HTTP 500
          events=None         -> return_receipt.py:59 TypeError  -> HTTP 500
          event without 'label' -> return_receipt.py:60 KeyError -> HTTP 500
          reset=None          -> return_receipt.py:61 TypeError  -> HTTP 500

        The tolerant reads are the same fallbacks already in this file and
        lose nothing that was measured: an unreadable interval bound widens
        nothing (the current bound is kept), and an event that carries no
        amount contributes no amount.
        """
        if not previous:
            return self
        if not isinstance(previous, dict):
            return self
        start = _as_number(previous.get("interval_start"), self.start)
        end = _as_number(previous.get("interval_end"), self.end)
        self.start = min(self.start, start)
        self.end = max(self.end, end)
        self.before_phase = previous.get("before_phase") or self.before_phase
        self.available_history = True
        for event in _as_sequence(previous.get("events")):
            if not isinstance(event, dict):
                continue
            amount = _as_delta(event.get("value"))
            # An event with no readable amount is not an observation; adding
            # it would either raise on the `+=` below or record a 0.0 delta
            # under a label that claims something happened.
            if amount is None:
                continue
            self.add(event.get("kind"), event.get("label") or "", amount,
                     event.get("unit") or "")
        self.reset.update(item for item in _as_sequence(previous.get("reset"))
                          if isinstance(item, str))
        return self

    def add(self, kind, label, value, unit):
        amount = _as_delta(value)
        # A zero delta is what a caller means by "nothing changed", and an
        # UNREADABLE delta is not an observation at all. Both are dropped here
        # rather than stored: `value=None` used to be stored verbatim and then
        # blew up on the `+=` of a second event, or serialised as a null
        # amount inside a receipt that claims to be measured.
        if not amount:
            return
        key = (kind, unit)
        if key not in self.events:
            self.events[key] = {"kind": kind, "label": label, "value": amount, "unit": unit}
        else:
            self.events[key]["value"] += amount

    def resource_change(self, player, before, kind, label):
        for field, unit in (("progress", "progress_points"), ("qi", "qi")):
            if field in before or field in player:
                after = _as_delta(player.get(field))
                prior = _as_delta(before.get(field))
                if after is None or prior is None:
                    continue
                self.add(kind, label, after - prior, unit)

    def offline(self, player, before, result, now):
        """Observe existing production handlers; do not execute or estimate rewards."""
        result = result if isinstance(result, dict) else result.model_dump()
        if result.get("ok") is False:
            return
        # A player whose life has not started has nothing to come back FROM.
        #
        # The lifecycle record is created LAZILY, by reconcile_player(start=True)
        # at lifecycle.py:195-201 -- a signup deliberately has no record at all
        # (main.py:1835: "Don't initialize lifecycle here"). A claim route may
        # therefore start the clock before it settles, in which case the live
        # player HAS a lifecycle while the `before` snapshot taken earlier does
        # not. The lifespan-extension line below differences the two, so on a
        # player who had no life when the snapshot was taken it read
        # before["lifecycle"] and raised KeyError: 'lifecycle'.
        #
        # Measured on POST /cultivation/claim-offline/v2 with a real signup
        # (POST /player/create, life never started): 500 with
        #   File "server/return_receipt.py", line 71, in offline
        #     extension = player["lifecycle"]["dies_at"] - before["lifecycle"]["dies_at"]
        # KeyError: 'lifecycle'
        #
        # This refuses instead of defaulting the extension to 0. A silent zero
        # would be the worse bug: `offline()` is an OBSERVER of what the
        # production handlers already did, so a fabricated 0-second extension
        # is written into a committed receipt as though it had been measured.
        # The same condition is answered 400 "lifecycle not started" at six
        # sites in lifecycle.py, so this is the same refusal in the same words.
        #
        # The refusal must test the FIELDS this method reads, not merely the
        # container type. `isinstance(..., dict)` alone passed for a lifecycle
        # record that is a dict but carries neither `dies_at` nor `born_at`,
        # and the two subscripts below then raised KeyError straight out of the
        # handler -- which is the exact 500 this guard exists to replace. The
        # original crash was a MISSING lifecycle key; a partially written one is
        # the same crash wearing a different hat.
        #
        # Measured on this class directly, before the fix:
        #   both sides {"status": "active"}            -> KeyError: 'dies_at'
        #   both sides {}                              -> KeyError: 'dies_at'
        #   dies_at present, born_at absent on `before` -> KeyError: 'born_at'
        # i.e. HTTP 500 with no status code chosen by this module at all, for a
        # condition the same module can already answer with a clean refusal.
        # The two sides are read ONCE, through the numeric reader, and every
        # value used below comes from those locals. Presence alone is not
        # enough: `{"dies_at": None}` passed the check above and then raised
        # TypeError on the subtraction two lines down -- measured on
        # POST /player/{id}/offline-progress, HTTP 500 at return_receipt.py:219.
        # Same refusal, same words: a lifespan difference this module cannot
        # measure is not a zero-second extension.
        _before_lc = before.get("lifecycle")
        _player_lc = player.get("lifecycle")
        _before_dies = _as_delta(_before_lc.get("dies_at")) if isinstance(_before_lc, dict) else None
        _player_dies = _as_delta(_player_lc.get("dies_at")) if isinstance(_player_lc, dict) else None
        _born_at = _as_delta(_before_lc.get("born_at")) if isinstance(_before_lc, dict) else None
        if _before_dies is None or _player_dies is None or _born_at is None:
            raise HTTPException(status_code=400, detail="lifecycle not started")
        # The claim cursor is the same stored field `__init__` and lifecycle's
        # effective_now already read tolerantly. An unreadable one falls back to
        # `now` for the same reason: a null cursor cannot be differenced, and a
        # null progress balance cannot be differenced either.
        cursor = _as_number(before.get("last_cultivation_time"), now)
        # Existing handlers write raw now. Preserve the monotonic claim cursor on
        # clock rollback so a later recovery cannot credit the same seconds twice.
        player["last_cultivation_time"] = max(
            cursor, _as_number(player.get("last_cultivation_time"), cursor))
        settled_at = player["last_cultivation_time"]
        self.end = max(self.end, settled_at)
        gained = _as_delta(result.get("progress_gained", 0)) or 0.0
        self.add("offline_reward", "Offline progress credited", gained, "progress_points")
        _after = _as_delta(player.get("progress"))
        _prior = _as_delta(before.get("progress"))
        net = 0.0 if _after is None or _prior is None else _after - _prior
        self.add("offline_settlement_adjustment", "Progress consumed or adjusted by offline breakthrough settlement", net - gained, "progress_points")
        for field, label in (("cultivation_realm", "Realm changed during offline settlement"),
                             ("cultivation_stage", "Stage changed during offline settlement")):
            if before.get(field) != player.get(field):
                self.add("offline_" + field, label, 1, "changes")
        extension = _player_dies - _before_dies
        self.add("lifespan_extension", "Lifespan extended during offline settlement", extension, "seconds")
        if gained or net or extension:
            # Cursor predates the latest lifecycle observation when a player read
            # status before claiming. Cover both observed intervals, never pre-birth.
            self.start = min(self.start, max(cursor, _born_at))
            self.add("offline_elapsed", "Server elapsed time used by offline settlement (capped)",
                     max(0, min(settled_at - cursor, 72 * 3600)), "seconds")

    def finish(self, player):
        if not self.events:
            return
        retained = ["identity", "account_seat"]
        for key, label in (("first_session", "practice_progress"), ("inventory", "inventory"),
                           ("beasts", "beasts"), ("wallet", "wallet"),
                           ("spirit_stones", "spirit_stones"), ("legacy", "legacy_history")):
            if key in player:
                retained.append(label)
        # The phase is read the same tolerant way __init__ reads it: a player
        # with no lifecycle record has not started a life, which is exactly
        # what before_phase already reports for them ("uninitialized"). Saying
        # so here is the measured truth, not a fabricated amount.
        #
        # This unguarded subscript was the SECOND KeyError: 'lifecycle' on the
        # same new-player path the offline() guard was written for. Measured by
        # calling the public method directly (a receipt carrying events, no
        # lifecycle record):
        #   File "server/return_receipt.py", line 119, in finish
        #     "after_phase": player["lifecycle"]["status"],
        #   KeyError: 'lifecycle'
        # It is shielded from the claim routes today only because offline()
        # refuses first; every internal caller happens to attach the record
        # before calling finish(). That is an accident of call order, not a
        # guarantee, and a receipt is a stored record a player keeps.
        after_phase = _player_status(player) or self.before_phase
        receipt = {"schema_version": 1, "interval_start": self.start,
                   "interval_end": self.end, "server_observed_at": self.end,
                   "committed": True, "before_phase": self.before_phase,
                   "after_phase": after_phase,
                   "events": list(self.events.values()), "retained": retained,
                   "reset": sorted(self.reset), "available_history": self.available_history}
        # Deterministic transaction replay identity; no growing event/id history.
        payload = json.dumps([player.get("id"), receipt], sort_keys=True, separators=(",", ":"))
        receipt["receipt_id"] = "return-v1-" + hashlib.sha256(payload.encode()).hexdigest()[:32]
        player["return_receipt"] = receipt


def receipt_view(player):
    """Detached rendering only; it must not create a receipt or reconcile time."""
    return deepcopy(player.get("return_receipt"))
