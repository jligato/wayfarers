"""Contract tests for ``server/receipt_routes.py``.

WHAT THIS FILE IS
-----------------
``receipt_routes.py`` is named like a route module and is not one. It exports
no ``register_routes``, holds no router, and adds nothing to a web app. What it
does export is three plain functions. Their contracts are pinned here:

* ``create_receipt_item`` -- append a receipt entry to a view dict.
* ``get_receipt_view``    -- read a player's stored receipt.
* ``init_purchase_receipts_db`` -- make the purchase-dedup store.

THEREFORE, AND MEASURED, NOT ASSUMED
------------------------------------
There is no request body, no status code and no session check to pin, because
there is no HTTP surface to reach one through. ``test_this_module_registers_no_
http_surface`` proves that by inspecting the module's exported entry points.
A reviewer expecting route tests here should read that arm first.

Two of these contracts were DEFECTS, pinned here first as shipped behaviour and
now pinned as FIXED. The arms below keep both stories, so neither defect can
come back unnoticed:

1. ``create_receipt_item`` could not build a receipt. It called
   ``ReturnReceipt(id=..., category=..., ...)``; the class was rewritten to take
   ``(player, now)``, so every call raised ``TypeError`` before it appended
   anything. It now builds a plain five-field dict instead -- the class cannot
   be built from what this helper receives, because it gets no player row and
   no clock. Pinned by ``test_creating_a_receipt_item_appends_the_item_it_
   returns`` and the two arms after it.
2. ``get_receipt_view`` was annotated ``-> dict`` and its docstring promised a
   one-key ``available`` flag, while the body returned the player's whole
   stored receipt, or ``None`` when there is none. The body was always right
   and the docstring described a different function, so the docstring and the
   annotation were corrected instead. Pinned by ``test_the_read_helper_
   documents_the_record_it_actually_answers``.


WHY THIS IS NOT A DUPLICATE OF ``test_return_receipt_contract.py``
-----------------------------------------------------------------
That file owns ``return_receipt.py`` (the stored record and the routes that
serve it) and mentions this module only to report one broken helper. This file
owns ``receipt_routes.py`` and pins all three of its functions, including the
read helper's answer for every stored shape and the placeholder store's
freshness. The one overlapping arm names this file as its counterpart.

RUN
---
    tools/wc_pytest.sh server/tests/test_receipt_routes_contract.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("WC_STORAGE_BACKEND", "memory")
for _var in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_var, None)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _root in (_REPO_ROOT, os.path.join(_REPO_ROOT, "server")):
    if _root not in sys.path:
        sys.path.insert(0, _root)

import receipt_routes  # noqa: E402
from return_receipt import ReturnReceipt  # noqa: E402

#: A fixed clock. The stored receipt's timestamps are derived from it, so no arm
#: here depends on the wall clock.
NOW = 2_000_000_000.0

#: Every key a stored receipt carries. Frozen deliberately: these are the names
#: a client reads, so adding one is safe and renaming one breaks every caller.
RECEIPT_KEYS = {
    "schema_version", "interval_start", "interval_end", "server_observed_at",
    "committed", "before_phase", "after_phase", "events", "retained", "reset",
    "available_history", "receipt_id",
}

#: Every key a receipt ITEM carries -- the five-field record a lifecycle receipt
#: list holds, which is NOT the twelve-field transaction record a
#: ``ReturnReceipt`` commits. Frozen deliberately: these are the names a caller
#: reads back off ``view["receipts"]``.
ITEM_KEYS = {"id", "category", "amount", "earned_at", "claimed_at"}

#: The three callables this module is responsible for. A fourth export means a
#: new public helper arrived and this file has not decided about it yet.
PUBLIC_HELPERS = {"create_receipt_item", "get_receipt_view", "init_purchase_receipts_db"}


def _player(pid: str = "p1", **extra) -> dict:
    """A row shaped like the one a signup writes."""
    row = {"id": pid, "name": f"Player {pid}", "spirit_root": "fire",
           "progress": 0.0, "qi": 0.0, "lifecycle": {"status": "active"}}
    row.update(extra)
    return row


def _player_with_receipt(pid: str = "p1", **extra) -> dict:
    """A row carrying one committed receipt, written by the record module."""
    player = _player(pid, **extra)
    receipt = ReturnReceipt(player, NOW)
    receipt.add("offline_reward", "Offline progress credited", 25.0, "progress_points")
    receipt.finish(player)
    return player


# --------------------------------------------------------------------------
# 1. THE BOUNDARY: there is no HTTP surface here
# --------------------------------------------------------------------------

def test_this_module_registers_no_http_surface():
    """No ``register_routes``, no router, no app, no store handle.

    The filename promises a set of endpoints. Measured: the module exports
    three plain functions and none of the four conventions a FastAPI route
    module is mounted through. So there is no status code, no reply body and no
    session requirement reachable through this file, and a test asserting one
    would be asserting fiction.

    Pinned because the only defence against this filename being believed is a
    test that keeps measuring it. If a router is ever added, this arm is the
    first one that must change, and the new endpoints need their own file.
    """
    for entry_point in ("register_routes", "router", "app", "_players_db"):
        assert not hasattr(receipt_routes, entry_point), entry_point


def test_the_module_exports_exactly_the_three_helpers_it_documents():
    """No helper is added or dropped without this file being updated.

    Imported names are excluded: they belong to ``return_receipt``, not to this
    module. This is a contract on the module's own surface, so an unowned
    helper cannot arrive silently.
    """
    own = {n for n in dir(receipt_routes) if not n.startswith("_")}
    # ``ReturnReceipt`` left this set when the write helper stopped calling it;
    # a name that is no longer imported is not part of this module's surface.
    imported = {"Any", "Optional", "receipt_view"}
    assert own - imported == PUBLIC_HELPERS


# --------------------------------------------------------------------------
# 2. THE READ HELPER: get_receipt_view
# --------------------------------------------------------------------------

def test_a_player_with_no_receipt_reads_as_nothing_at_all():
    """No ``return_receipt`` key answers ``None``, not an empty receipt.

    ``None`` is how a caller tells "this player was never observed" from
    "observed and saw nothing". An empty dict would collapse those two.
    """
    assert receipt_routes.get_receipt_view(_player()) is None


def test_a_stored_receipt_comes_back_with_its_whole_key_set():
    """The helper hands back the stored record, key for key.

    Frozen key set, because these are the names a client renders. The
    ``schema_version`` and ``committed`` flags are frozen with it: they tell a
    client whether the document it is drawing is a final record.
    """
    view = receipt_routes.get_receipt_view(_player_with_receipt())
    assert isinstance(view, dict)
    assert set(view) == RECEIPT_KEYS
    assert view["schema_version"] == 1
    assert view["committed"] is True
    assert view["receipt_id"].startswith("return-v1-")


def test_the_read_helper_hands_back_a_copy_the_caller_cannot_edit_the_store_with():
    """Editing the returned view must leave the stored record alone.

    A helper that returned the live dict would let one caller rewrite the
    committed record another caller reads. Two things are checked: the view is
    a different object, and an edit nested inside it -- not just a top-level key
    -- does not reach the store.
    """
    player = _player_with_receipt()
    stored_before = len(player["return_receipt"]["events"])

    view = receipt_routes.get_receipt_view(player)
    assert view is not player["return_receipt"]
    view["events"].append({"kind": "injected", "value": 999.0})
    view["interval_start"] = 0.0

    assert len(player["return_receipt"]["events"]) == stored_before
    assert player["return_receipt"]["interval_start"] != 0.0
    assert receipt_routes.get_receipt_view(player) == player["return_receipt"]


def test_the_read_helper_passes_a_foreign_stored_shape_through_unchanged():
    """Whatever is under the key is what comes back, deep-copied.

    Measured across five stored shapes: absent and null both answer ``None``,
    while an empty dict, the number zero and a bare string all come back as
    themselves. A client therefore cannot assume the answer is a dict.

    The empty-dict and zero arms are the sharp ones. The record module treats
    either of them as "something was instrumented before" (its own
    ``available_history`` reads True for both), so this helper hands a caller a
    document that means the opposite of the flag inside it. Pinned as shipped
    behaviour: a client must read ``available_history`` from the record, not
    infer it from whether this helper answered something.
    """
    answers = {
        "absent": ({"id": "p1"}, None),
        "explicitly null": ({"id": "p1", "return_receipt": None}, None),
        "empty dict": ({"id": "p1", "return_receipt": {}}, {}),
        "zero": ({"id": "p1", "return_receipt": 0}, 0),
        "a bare string": ({"id": "p1", "return_receipt": "oops"}, "oops"),
    }
    for label, (row, expected) in answers.items():
        assert receipt_routes.get_receipt_view(row) == expected, label
        assert type(receipt_routes.get_receipt_view(row)) is type(expected), label


def test_the_read_helper_documents_the_record_it_actually_answers():
    """The one-key availability promise is gone from docstring and annotation.

    DEFECT, FIXED, and pinned so it cannot come back. The signature said
    ``-> dict`` and the docstring promised an availability indicator, while the
    body has always returned the player's whole stored receipt and ``None`` when
    there is none. That promise belonged to a different function --
    ``lifecycle.receipt_view`` (lifecycle.py:178-179), which is what
    ``POST /lifecycle/{pid}/view_recap`` serves. So the docstring described the
    route rather than this helper, and a caller who trusted it would read a
    per-player record believing they had asked for a flag.

    The body was chosen to stay and the documentation to move, because four
    other arms here already pin the record behaviour as the useful answer and
    the flag use-case is served by another function. Three things are pinned:

    * the answer is still the whole record and never a bare flag;
    * the annotation no longer claims a dict-shaped status object;
    * the docstring no longer promises the one-key literal.
    """
    import inspect

    player = _player_with_receipt()
    view = receipt_routes.get_receipt_view(player)
    assert isinstance(view, dict)
    assert "available" not in view
    assert set(view) == RECEIPT_KEYS

    # And the narrow reading stays unavailable: a player who HAS a record is not
    # distinguishable from one who does not by any flag this helper adds.
    assert set(receipt_routes.get_receipt_view(_player()) or {}) != {"available"}

    # The annotation is read off the live function object, never from source.
    assert receipt_routes.get_receipt_view.__annotations__["return"] is not dict

    # And the old one-key promise is gone from the prose: a docstring that
    # re-advertises it would send the next caller back into the same trap.
    doc = inspect.getdoc(receipt_routes.get_receipt_view) or ""
    assert '{"available": bool}' not in doc
    assert "available_history" in doc

# --------------------------------------------------------------------------
# 3. THE PLACEHOLDER STORE: init_purchase_receipts_db
# --------------------------------------------------------------------------

def test_the_purchase_store_starts_empty_and_is_a_fresh_object_every_call():
    """Each call returns a new empty dict that shares nothing with the last.

    Purchase-receipt deduplication does not live here -- the module's own
    docstring says so and points at the validator in ``main.py``, which keeps
    its own store. So this store must never accumulate: a shared object would
    grow across calls in a process and look like real deduplication state that
    nothing reads.
    """
    first = receipt_routes.init_purchase_receipts_db()
    second = receipt_routes.init_purchase_receipts_db()

    assert first == {} and second == {}
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first is not second

    # A write into one must be invisible to the next call.
    first["rcpt_abc"] = {"seen": True}
    assert receipt_routes.init_purchase_receipts_db() == {}
    assert second == {}


def test_the_purchase_store_is_not_shared_with_anything_the_module_holds():
    """The placeholder aliases no dict the module itself carries.

    Purchase deduplication lives elsewhere -- the module docstring says so and
    points at the validator in ``main.py``, which keeps its own store. The risk
    this pins is aliasing: if the module held a module-level dict and handed it
    back, a caller writing into the returned store would be writing into module
    state that the next call also sees.

    Checked on live objects rather than on the module's text, so it measures
    behaviour. Every mutable dict the module exposes at import time is compared
    by identity against a fresh call's answer; none of them may be the object
    handed back.
    """
    module_dicts = {
        name: value for name, value in vars(receipt_routes).items()
        if isinstance(value, dict)
    }
    store = receipt_routes.init_purchase_receipts_db()
    assert store is not None
    assert not any(store is held for held in module_dicts.values()), module_dicts


# --------------------------------------------------------------------------
# 4. THE WRITE HELPER: create_receipt_item
# --------------------------------------------------------------------------

def test_creating_a_receipt_item_appends_the_item_it_returns():
    """One call appends exactly one item, and the return value is that item.

    DEFECT, FIXED, and pinned so it cannot come back. This arm used to assert
    that the call raises ``TypeError``: the helper called
    ``ReturnReceipt(id=..., category=..., amount=..., earned_at=...,
    claimed_at=...)`` while the class had been rewritten to take
    ``(player, now)``, so every keyword was unexpected and every call raised
    before appending anything. No argument change could have fixed that -- the
    helper receives no player row and no clock, which is exactly what the class
    needs -- so the item is now a plain five-field dict, the same shape
    ``POST /lifecycle/{pid}/receipt`` already stores (lifecycle.py:973-980).

    Nothing in the shipped tree calls this helper, which is exactly why the
    break went unnoticed. The one overlapping arm is
    ``test_a_receipt_item_helper_that_calls_the_old_signature_raises`` in
    ``test_return_receipt_contract.py``; that file owns the record class, this
    one owns the call.

    Pinned: the view gains exactly one entry, the return value is the same
    object as the appended one, the key set is frozen, and the advertised
    arguments land in the item where a caller will read them.
    """
    view: dict = {}
    item = receipt_routes.create_receipt_item(
        view, category="test", amount=10.0, earned_at=NOW, claimed_at=NOW)

    assert view["receipts"] == [item]
    assert item is view["receipts"][0]
    assert set(item) == ITEM_KEYS
    assert item["category"] == "test"
    assert item["amount"] == 10.0
    assert item["earned_at"] == NOW
    assert item["claimed_at"] == NOW
    assert item["id"].startswith("rcpt_")


def test_the_write_helper_runs_the_example_in_its_own_docstring():
    """The documented example executes instead of raising.

    DEFECT, FIXED, and pinned separately from the arm above because a fix that
    left the documented usage broken would still satisfy it: the docstring said
    ``view = guard(request)`` then
    ``create_receipt_item(view, category="test", amount=10.0)``, and that exact
    call was the one that raised ``TypeError``.

    The example is executed here rather than read, so a docstring that drifts
    away from the code reds here instead of misleading the next caller.
    """
    view: dict = {}
    item = receipt_routes.create_receipt_item(view, category="test", amount=10.0)

    assert view["receipts"] == [item]
    assert item["category"] == "test"
    assert item["amount"] == 10.0
    # The two omitted arguments keep their advertised defaults, so the
    # documented call cannot quietly lose the other half of the item.
    assert item["earned_at"] == 0
    assert item["claimed_at"] is None


def test_the_write_helper_builds_an_item_from_every_argument_it_advertises():
    """Every REMAINING argument combination is well-formed.

    DEFECT, FIXED, and pinned. This arm used to assert a raise per case,
    because every combination reached the broken call -- so a caller could not
    use the helper even by omitting arguments.

    The two cases that omitted ``amount`` are GONE, and that is the later fix,
    not a loss of coverage: ``amount`` no longer has a default, so those calls
    raise TypeError at the call site instead of writing a silent 0.0 into a
    payout record. ``test_the_write_helper_refuses_a_call_that_omits_the_amount``
    is the arm that pins that now.

    ``claimed_at`` still defaults to ``None`` and is stored as ``None``, not
    coerced to ``0``: a fabricated claim time would read as "claimed" to
    anything that checks truthiness. ``amount`` is the one argument whose
    default was removed, because it is the one that sizes money.
    """
    cases = {
        "amount only": ({"amount": 10.0},
                        {"category": "offline_progress", "amount": 10.0,
                         "earned_at": 0, "claimed_at": None}),
        "every argument": ({"category": "test", "amount": 10.0,
                            "earned_at": NOW, "claimed_at": NOW},
                           {"category": "test", "amount": 10.0,
                            "earned_at": NOW, "claimed_at": NOW}),
        # A deliberate zero is still accepted -- this is about the DEFAULT
        # being gone, not about zero being illegal.
        "zero on purpose": ({"amount": 0.0},
                            {"category": "offline_progress", "amount": 0.0,
                             "earned_at": 0, "claimed_at": None}),
    }
    for label, (kwargs, expected) in cases.items():
        view: dict = {}
        item = receipt_routes.create_receipt_item(view, **kwargs)

        assert view["receipts"] == [item], label
        assert set(item) == ITEM_KEYS, label
        for key, value in expected.items():
            assert item[key] == value, f"{label}: {key}"


def test_the_write_helper_signs_are_what_this_file_pins():
    """The advertised parameters are frozen; a rename breaks every caller.

    Asserted from the live objects, not from source text.

    ONE default was removed, deliberately: ``amount`` used to default to
    ``0.0``. The parameter NAME and its position in the list are unchanged, so
    every existing caller's call still resolves -- but a caller that omitted
    the amount now fails loudly instead of pricing a receipt at zero.
    """
    import inspect

    signature = inspect.signature(receipt_routes.create_receipt_item)
    assert list(signature.parameters) == [
        "view", "category", "amount", "earned_at", "claimed_at"]
    assert signature.parameters["category"].default == "offline_progress"
    # THE FIX: ``amount`` has NO default and is keyword-only, so no call can
    # silently write a caller-chosen 0.0 into a payout record.
    assert signature.parameters["amount"].default is inspect.Parameter.empty
    assert (signature.parameters["amount"].kind
            is inspect.Parameter.KEYWORD_ONLY)
    assert signature.parameters["earned_at"].default == 0
    assert signature.parameters["claimed_at"].default is None


def test_the_write_helper_refuses_a_call_that_omits_the_amount():
    """The silent-zero path is GONE: omitting ``amount`` raises, it does not pay 0.

    THE FIX. ``amount`` used to be ``amount: float = 0.0``, so any caller who
    forgot it -- or who copied an example that left it out -- got a well-formed
    receipt priced at ZERO, appended silently to the list. On a helper whose only
    future callers are payout routes, a default of 0.0 is a caller-chosen payout
    amount wearing a default, which is the shape the mint-authority rule forbids.

    It is now required and keyword-only. Three separate refusals are asserted,
    because each one is a different way the old default could be reached:

    1. no arguments at all -- the "forgot it" case;
    2. ``category`` supplied but no amount -- the "copied the example" case;
    3. amount passed POSITIONALLY -- keyword-only, so a positional figure cannot
       bind to it by accident once a caller reorders arguments.

    Each must raise TypeError AND leave the view untouched: a refusal that still
    appended a 0.0 receipt would be the same defect wearing a different hat.
    """
    calls = (
        ("no arguments", ()),
        ("category but no amount", ((), {"category": "test"})),
    )
    for label, args, kwargs in calls:
        view: dict = {}
        with pytest.raises(TypeError):
            receipt_routes.create_receipt_item(view, *args, **kwargs)
        assert view == {}, f"{label}: refused call still wrote {view}"

    # Keyword-only: a positional amount cannot bind.
    view = {}
    with pytest.raises(TypeError):
        receipt_routes.create_receipt_item(view, 10.0)
    assert view == {}, "positional amount was accepted"

    # And the control: the SAME helper still builds an item when the caller
    # states the amount. A refusal-everything helper would pass all of the above.
    view = {}
    item = receipt_routes.create_receipt_item(view, amount=10.0)
    assert view["receipts"] == [item]
    assert item["amount"] == 10.0
    assert set(item) == ITEM_KEYS
