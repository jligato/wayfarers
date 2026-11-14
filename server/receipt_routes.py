"""
G3 Return Receipt Utilities (extracted from main.py for modularity).

This module provides:
- Receipt item creation (the five-field record a lifecycle receipt list holds)
- Receipt view extraction (the player's whole stored return receipt)
- G3 receipt data for lifecycle view() integration

The purchase receipt validation logic lives in main.py validate_purchase()
and is NOT duplicated here — it serves a different purpose (wallet defense §3.3).

Called from:
- lifecycle.py view() — for R3 receipt visibility
- main.py routes — for receipt creation in guard views
- test_return_receipt.py — for receipt inspection

This module was extracted from main.py per modularity improvements plan (2026-09-22).
"""
from typing import Any, Optional

from return_receipt import receipt_view


def create_receipt_item(
    view: dict,
    category: str = "offline_progress",
    *,
    amount: float,
    earned_at: float = 0,
    claimed_at: Optional[float] = None,
) -> dict:
    """Create one receipt item and append it to view's receipts list.

    The item is a plain five-field dict. It is deliberately NOT a
    ``ReturnReceipt``, and it cannot be one.

    MEASURED DEFECT, FIXED HERE. This helper used to call
    ``ReturnReceipt(id=..., category=..., amount=..., earned_at=...,
    claimed_at=...)``. That class was rewritten to take ``(player, now)`` -- it
    observes ONE committed player transaction from a player row
    (return_receipt.py:82-97), it is not a five-field item -- so every keyword
    above was unexpected and every call raised ``TypeError`` before appending
    anything. No argument change repairs that: this helper receives no player
    row and no clock, and the class needs both to build anything at all.

    So the item is built as a dict, which is the shape
    ``POST /lifecycle/{pid}/receipt`` already stores (lifecycle.py:1343-1353) and
    the shape its sibling read route already tolerates
    (``r.to_dict() if hasattr(r, "to_dict") else r``, lifecycle.py:1322).
    That
    route shipped this exact fix for this exact break.

    Args:
        view: Guard view dict (mutated in place).
        category: Receipt category label.
        amount: Monetary/point amount. REQUIRED, keyword-only, and
            with no default -- see "AMOUNT IS NOT CALLER-CHOSEN" below.
        earned_at: When the reward was earned.
        claimed_at: When the reward was claimed. Stored exactly as passed, so
            the ``None`` default keeps meaning "not claimed" instead of
            becoming a fabricated claim time.

    Returns:
        The created receipt item dict -- the same object now at the end of
        view["receipts"].

    Example:
        view = guard(request)  # or view from lifecycle.view()
        create_receipt_item(view, category="test", amount=10.0)  # amount is required

    PERSISTENCE, STATED PLAINLY: ``view`` is the guard's DETACHED view dict, so
    an append here is visible to whoever called this function and is written
    nowhere. The sibling route appends inside a transaction that owns the row
    instead (lifecycle.py:1353) for exactly that reason. Nothing in the
    shipped tree calls this helper; the first caller that must persist the item
    has to persist it the way that route does.

    AMOUNT IS NOT CALLER-CHOSEN, AND HAS NO DEFAULT. This parameter used
    to be ``amount: float = 0.0``, so a caller could omit it entirely and
    silently mint a receipt priced at zero. A receipt item is a payout
    record, and a payout record whose amount defaults to 0.0 is a payout
    amount chosen by whoever happened to call the helper -- exactly the
    shape the mint-authority rule forbids. Making the parameter REQUIRED
    (keyword-only, no default) removes the silent-zero path: every call
    site now states its amount on purpose, or fails with a TypeError at
    the call rather than writing 0.0 to a record.

    The amount cannot be derived inside this helper: it receives no player
    row, no clock, and no pricing input, so the caller must hand it a
    figure the SERVER computed. Keeping it required is what forces that.
    """
    import secrets, time
    receipt = {
        "id": f"rcpt_{secrets.token_hex(8)}_{int(time.time())}",
        "category": category,
        "amount": amount,
        "earned_at": earned_at,
        "claimed_at": claimed_at,
    }
    view.setdefault("receipts", []).append(receipt)
    return receipt


def get_receipt_view(player: dict) -> Any:
    """Return the player's whole stored return receipt, detached.

    Args:
        player: Player dict from players_db.

    Returns:
        A deep copy of whatever is stored under ``player["return_receipt"]``:
        normally the whole committed receipt, key for key, and ``None`` when the
        key is absent or null.

        ``Any`` is the honest annotation rather than a shrug. The record is
        written by ``ReturnReceipt.finish`` as a dict, so a caller normally
        gets a dict, but a legacy row holding something else is passed through
        as-is rather than discarded -- so a caller must not assume a dict
        without looking. Presence is NOT availability: an empty dict or a bare
        ``0`` under the key comes back as itself, while the record's own
        ``available_history`` flag reads True for both. Read that flag from the
        record; do not infer it from whether this answered something.

    Example:
        player = players_db[pid]
        receipt = get_receipt_view(player)
        # receipt is the whole stored record, or None

    MEASURED DEFECT, FIXED HERE. This docstring used to promise a one-key
    ``available`` status object. That promise belonged to a different function:
    ``lifecycle.receipt_view`` returns exactly that (lifecycle.py:178-179), and
    ``POST /lifecycle/{pid}/view_recap`` is the route that serves it
    (lifecycle.py:986-988). This helper has always answered the record, so the
    old docstring described the route rather than the helper, and a caller who
    trusted it would read a whole per-player record believing they had asked for
    a flag. Nothing in the shipped tree calls this helper.
    """
    return receipt_view(player)


def init_purchase_receipts_db() -> dict:
    """Initialize purchase receipts deduplication store.

    Note: This is a placeholder for G3 receipt persistence.
    The actual purchase receipt validation lives in main.py
    validate_purchase() (wallet defense §3.3) and uses a separate
    purchase_receipts_db dict defined in main.py module scope.

    Returns:
        Empty dict for purchase receipt tracking.
    """
    return {}
