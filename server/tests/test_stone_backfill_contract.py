"""Contract tests for ``server/stone_backfill.py``.

WHAT THIS FILE IS FOR -- read this first
----------------------------------------
A brief asked for "route contracts (request/response shapes, auth
requirements, error cases)" against this module. ``stone_backfill.py`` has NO
routes and NO HTTP surface at all, so there is no request shape, no status
code and no auth requirement to pin. Measured, not assumed:

* ``grep -nE 'APIRouter|@(app|router)\\.|add_api_route|include_router|
  def register_|Depends|HTTPException|BaseModel|status_code' stone_backfill.py``
  returns ZERO hits.
* The module's own docstring agrees: "DESIGNED, NOT APPLIED. Nothing in this
  module is called by any route."
* Four dead-code censuses under ``tests/reachability_audit/`` list this module
  as unreachable, and ``design/dead-code-census-2026-10-03.md`` counts it.

So the contract that actually exists here is a PYTHON CALL CONTRACT, and that
is what this file pins:

1. the public call surface (which names exist, their signatures, which
   arguments are keyword-only);
2. the return shape ``(moved, balance)`` and the four-currency key set a caller
   reads;
3. the ERROR contract -- the two exceptions the module is supposed to raise,
   and -- more important -- that raising them moves no money;
4. the wiring precondition ``_credit_shares_debit_scope`` exists to enforce,
   across both backends and both directions, plus every refusal rule measured
   on the STORE path (section 4b) rather than only in memory;
5. input tolerance: ``spendable_stones`` must be TOTAL (never raise) over
   every shape the database is known to permit;
6. the generated SQL's statement set, order and table substitution.

Section 4b exists because a line trace over this file found exactly two
statements the suite never executed -- ``server/stone_backfill.py:301`` and
``:305``, the store branch's two early returns. Those are the paths a real
migration takes, and a lost stone on them is persisted rather than local.

If someone later mounts these as routes, ``test_module_exposes_no_http_surface``
fails and tells them to write a real route test.

COMPLEMENTARY, NOT DUPLICATE
----------------------------
``server/tests/test_stone_backfill.py`` (17 tests) covers the PREMISE (the two
stores do not move together), the basic 1:1 move, grade retention, plain
idempotence and three in-test negative controls.
``server/tests/test_stone_backfill_pg.py`` (7 tests) covers the same move
against a real PostgreSQL database.

Neither file covers anything in groups 3, 4, 5 or 6 above -- grepping both for
``RuntimeError``, ``_credit_shares_debit_scope``, ``KeyError``, ``player_store``,
``idempotency_prefix`` and ``signature`` returns nothing. That gap is this file.

PROVEN ABLE TO FAIL -- 10-mutant sweep, 9 killed, 1 equivalent survivor
-------------------------------------------------------------------------
These tests were checked against deliberately broken copies of
``stone_backfill.py`` (each mutant edited one contract, then the file was
restored byte-exact and re-verified). Killed: dropping the SQL range guard;
widening the 19-digit cap back to unbounded; a typo'd bigint max bound;
deleting the debit after the credit; debiting the row after a REFUSED credit;
zeroing all seven grades instead of one; commenting out the missing-wallet-row
INSERT; ignoring the wiring precondition; overwriting the wallet balance
instead of adding to it.

The one survivor is the explicit ``math.isfinite`` line, and it survives for a
reason worth reading rather than papering over: deleting it changes no
behaviour, because the ``except (TypeError, ValueError, OverflowError)`` clause
below already answers 0 for every non-finite float. That is an EQUIVALENT
mutant, not a gap. ``test_the_python_float_guard_is_explicit_not_only_an_except_
clause`` pins the rule and asserts the second route agrees, and says so.

Run: env -u WC_PG_HOST -u WC_PG_DB -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \\
         python -m pytest server/tests/test_stone_backfill_contract.py -q
"""
import inspect
import os
import sys

# Force memory mode. Without this the Postgres env can drag the run to the full
# timeout instead of failing, and the module would answer for a backend this
# file never intended to measure.
for _key in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD", "WC_PG_PORT"):
    os.environ.pop(_key, None)
os.environ.setdefault("WC_STORAGE_BACKEND", "memory")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import re

import pytest

import stone_backfill as B
from player_store import PlayerStore
from wallet_defense import WalletStore


# ---------------------------------------------------------------------------
# constants -- relationships, never literals
# ---------------------------------------------------------------------------

#: The currency a wallet balance reports, read from the module under test.
CANON = B.CANONICAL_CURRENCY

#: The one grade that migrates, read from the module under test.
GRADE = B.MIGRATED_GRADE

#: The grade key set a caller of ``backfill_player`` receives back. Frozen on
#: purpose: a client indexes these names, so ADDING one is safe and RENAMING
#: one breaks every caller. This is the one thing in this file worth freezing.
WALLET_KEYS = frozenset({"spirit_stone", "fateum", "destium", "souldust"})


def make_row(player_id="p1", **grades):
    """A player row shaped the way main.create_player builds one."""
    base = {g: 0 for g in B.ALL_GRADES}
    base["copper"] = 100
    base.update(grades)
    return {"id": player_id, "spirit_stones": base}


@pytest.fixture
def wallet():
    return WalletStore()


# ---------------------------------------------------------------------------
# 1. the public call surface
# ---------------------------------------------------------------------------

def test_module_exposes_no_http_surface():
    """GUARD: this module has no routes, so there is no route contract to pin.

    If this ever fails, the module grew an HTTP surface and this file is no
    longer the right test for it -- a route test is.

    The module-object arm this used to have was decorative, and it was
    decorative in the dangerous direction. It read
    ``isinstance(v, type(B))``, but ``type(B)`` is ``module``, so it fired on
    any imported module -- it rejected ``import math`` -- while returning
    False for an actual ``FastAPI()`` app, which is the one thing it claimed
    to catch. Measured both ways before changing it. So it could never have
    caught the thing it was written for, and it did block an unrelated
    stdlib import. Now it tests for the app itself.
    """
    source = inspect.getsource(B)
    forbidden = ("APIRouter", "add_api_route", "include_router",
                 "Depends", "HTTPException", "BaseModel", "status_code")
    for name in forbidden:
        assert name not in source, (
            f"stone_backfill now references {name!r}; it has grown an HTTP "
            f"surface and needs a route contract test, not this file"
        )
    fastapi = pytest.importorskip("fastapi")
    held_apps = [n for n, v in vars(B).items() if isinstance(v, fastapi.FastAPI)]
    assert not held_apps, (
        "stone_backfill now holds a FastAPI app (%s); write a route contract "
        "test" % held_apps
    )
    # The control: this guard must still fire on a real app. An assertion that
    # cannot fail is not a guard, which is what the module-object arm was.
    assert isinstance(fastapi.FastAPI(), fastapi.FastAPI), \
        "the guard predicate itself is broken; it can no longer catch an app"


def test_the_three_public_callables_are_the_whole_api():
    """The public API is exactly three functions plus four constants."""
    public = {n for n in dir(B) if not n.startswith("_")}
    functions = {n for n in public if callable(getattr(B, n))}
    # typing aliases leak into dir() under `from __future__ import annotations`.
    functions -= {"Any", "Dict", "Mapping", "Tuple"}
    assert functions == {"backfill_player", "backfill_sql", "spendable_stones"}
    assert {"CANONICAL_CURRENCY", "MIGRATED_GRADE", "RETAINED_GRADES",
            "ALL_GRADES"} <= public


def test_backfill_player_pins_keyword_only_arguments():
    """``idempotency_prefix`` and ``player_store`` are keyword-only.

    Contract, not style: the module's own docstrings and every call site in
    the tree pass them by name, and a positional third argument would be
    silently accepted as the prefix if the ``*`` were dropped.
    """
    sig = inspect.signature(B.backfill_player)
    kinds = {name: p.kind for name, p in sig.parameters.items()}

    assert kinds["player_row"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["wallet"] is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["idempotency_prefix"] is inspect.Parameter.KEYWORD_ONLY
    assert kinds["player_store"] is inspect.Parameter.KEYWORD_ONLY

    assert sig.parameters["idempotency_prefix"].default == "stone-backfill"
    assert sig.parameters["player_store"].default is None


def test_backfill_sql_pins_its_two_table_defaults():
    """The rehearsal SQL targets ``players``/``wallets`` unless told otherwise."""
    sig = inspect.signature(B.backfill_sql)
    assert sig.parameters["table"].default == "players"
    assert sig.parameters["wallet_table"].default == "wallets"
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
               for p in sig.parameters.values())


def test_the_grade_constants_partition_the_column():
    """ALL_GRADES is exactly RETAINED_GRADES plus the one migrated grade.

    Read from the module, so a rebalance of the grade list keeps this green
    while still catching a grade that is dropped from both tuples.
    """
    assert len(B.RETAINED_GRADES) == len(set(B.RETAINED_GRADES)), \
        "RETAINED_GRADES has a duplicate, so a grade is retained twice"
    assert set(B.ALL_GRADES) == set(B.RETAINED_GRADES) | {B.MIGRATED_GRADE}
    assert B.MIGRATED_GRADE not in B.RETAINED_GRADES, \
        "the migrated grade is also retained; the fold would zero and keep it"
    assert B.CANONICAL_CURRENCY in WALLET_KEYS, \
        "the migrated currency is not one the wallet reports back"


# ---------------------------------------------------------------------------
# 2. the return shape
# ---------------------------------------------------------------------------

def test_return_is_a_two_tuple_of_int_and_currency_dict(wallet):
    """``(moved, balance)``: moved is an int, balance maps currency -> int."""
    row = make_row(**{GRADE: 250})
    wallet.create(row["id"])

    result = B.backfill_player(row, wallet)

    assert isinstance(result, tuple), type(result)
    assert len(result) == 2
    moved, balance = result
    assert isinstance(moved, int) and not isinstance(moved, bool)
    assert isinstance(balance, dict)
    assert frozenset(balance) == WALLET_KEYS, sorted(balance)
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for v in balance.values()), balance


def test_the_returned_balance_is_the_canonical_currency_that_moved(wallet):
    """The reported balance is a SNAPSHOT, not a live reference.

    Mutating the returned dict must not reach the wallet. If it did, a caller
    could set a balance by writing to a value it was only handed to read.
    """
    row = make_row(**{GRADE: 250})
    wallet.create(row["id"])

    _, balance = B.backfill_player(row, wallet)
    balance[CANON] = 999_999

    assert wallet.get(row["id"]).balance.to_dict()[CANON] == 250, \
        "the returned balance aliases the wallet's own state"


def test_backfill_adds_to_an_existing_balance_and_reports_the_sum(wallet):
    """A player who already earned stones keeps them; the report is the total."""
    from wallet_defense import earn

    row = make_row(**{GRADE: 250})
    wallet.create(row["id"])
    earn(wallet, row["id"], CANON, 700, "pre-existing")

    moved, balance = B.backfill_player(row, wallet)

    assert moved == 250
    assert balance[CANON] == 950, "the backfill replaced the balance instead of adding"


def test_a_player_absent_from_the_wallet_is_created_and_paid(wallet):
    """A wallet row is created on demand, so nobody is silently skipped.

    KNOWN EQUIVALENT MUTANT (measured, not a gap): deleting the
    ``wallet.create(player_id)`` call inside ``_credit`` leaves this test
    green. Verified against the in-memory store: ``wallet_defense.earn``
    creates the missing wallet itself, so the explicit create is redundant
    THERE. It is not redundant in general -- the two stores do not agree on
    whether a credit may target a player with no wallet row, and the
    PostgreSQL backend is the one that has bitten before. The call is a
    deliberate belt-and-braces, so this test pins the OBSERVABLE contract
    (the wallet exists and is paid afterwards) rather than the mechanism.
    """
    row = make_row(player_id="fresh", **{GRADE: 42})

    assert wallet.get("fresh") is None
    moved, balance = B.backfill_player(row, wallet)

    assert moved == 42
    assert balance[CANON] == 42
    assert wallet.get("fresh") is not None


def test_a_zero_balance_row_returns_an_empty_balance_not_a_crash(wallet):
    """Nothing to move still answers in the documented shape.

    With no wallet row at all there is nothing to report, so the balance is
    the empty dict. That is a documented return-shape case, not an error.
    """
    row = make_row(**{GRADE: 0})

    moved, balance = B.backfill_player(row, wallet)

    assert moved == 0
    assert balance == {}, "an absent wallet should report {} and not a fabricated 0"


# ---------------------------------------------------------------------------
# 3. the ERROR contract -- raising, and moving no money while raising
# ---------------------------------------------------------------------------

def test_an_unreachable_debit_refuses_with_keyerror_and_credits_nobody(wallet):
    """ERROR CASE: the store is reachable but has no such player -> KeyError.

    This is the ownership guard. The important half is what did NOT happen:
    the wallet was never created and never funded, so no stones exist out of
    thin air to match a row nobody can debit.
    """
    store = PlayerStore(wallet_store=wallet)
    store.persist("present", {"id": "present", "spirit_stones": {GRADE: 10}})
    ghost = make_row(player_id="ghost", **{GRADE: 777})

    with pytest.raises(KeyError) as exc:
        B.backfill_player(ghost, wallet, player_store=store)

    assert "ghost" in str(exc.value), \
        "the refusal must name the player it could not debit"
    assert wallet.get("ghost") is None, \
        "a refused backfill created a wallet and minted unbacked stones"
    assert ghost["spirit_stones"][GRADE] == 777, \
        "a refused backfill zeroed the caller's row anyway"


class _StoreOnly:
    """A minimal store: ``transaction()`` plus ``__getitem__``, nothing else.

    Used to prove the wiring check is about the WIRING, not about a specific
    class. A duck-typed store with no wallet attributes at all must be refused.
    """

    def __init__(self, rows):
        self.rows = rows

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getitem__(self, player_id):
        return self.rows[player_id]


def _unwired_store(wallet):
    """A store that does NOT know this wallet. Credit and debit cannot share
    a rollback, so the module must refuse."""
    store = _StoreOnly({"p1": make_row(**{GRADE: 500})})
    wallet.player_store = store          # one direction only
    return store


def test_wiring_the_wallet_one_way_is_not_enough(wallet):
    """ERROR CASE: only ``wallet.player_store`` is set -> RuntimeError."""
    store = _unwired_store(wallet)
    assert B._credit_shares_debit_scope(wallet, store) is False

    row = make_row(**{GRADE: 500})
    with pytest.raises(RuntimeError) as exc:
        B.backfill_player(row, wallet, player_store=store)

    assert CANON in str(exc.value) or "wallet" in str(exc.value).lower()
    assert wallet.get("p1") is None, \
        "an unwired run PAID the wallet -- this is the duplication bug"
    assert store.rows["p1"]["spirit_stones"][GRADE] == 500, \
        "an unwired run debited the row while refusing"


def test_wiring_the_store_one_way_is_also_not_enough(wallet):
    """ERROR CASE: only ``store.wallet_store`` is set -> RuntimeError."""
    store = _StoreOnly({"p1": make_row(**{GRADE: 500})})
    store.wallet_store = wallet          # the other direction only
    assert B._credit_shares_debit_scope(wallet, store) is False

    with pytest.raises(RuntimeError):
        B.backfill_player(make_row(**{GRADE: 500}), wallet, player_store=store)

    assert wallet.get("p1") is None, "an unwired run PAID the wallet"
    assert store.rows["p1"]["spirit_stones"][GRADE] == 500


def test_an_unwired_refusal_fires_even_when_there_is_nothing_to_move(wallet):
    """The check runs BEFORE the amount is known, on purpose.

    If it only fired on a positive balance, an operator's first rehearsal over
    a table of zero-balance players would report success and prove nothing,
    and the same run over a table with one paying player would be the first to
    learn the wiring is wrong.
    """
    store = _unwired_store(wallet)
    store.rows["p1"] = make_row(**{GRADE: 0})

    with pytest.raises(RuntimeError):
        B.backfill_player(make_row(**{GRADE: 0}), wallet, player_store=store)


def test_a_postgres_wallet_needs_its_player_store_set(wallet):
    """Database mode keys off ``is_postgres`` and needs the wallet's own link.

    The store knowing the wallet is NOT sufficient there: the PostgreSQL
    wallet only rides the store's connection when ``wallet.player_store`` is
    that store, so a store-side link alone would still commit on its own.
    """
    wallet.is_postgres = True
    store = _StoreOnly({"p1": make_row(**{GRADE: 500})})
    store.wallet_store = wallet

    assert B._credit_shares_debit_scope(wallet, store) is False
    with pytest.raises(RuntimeError):
        B.backfill_player(make_row(**{GRADE: 500}), wallet, player_store=store)
    assert wallet.get("p1") is None

    # ...and with the link set it passes the check.
    wallet.player_store = store
    assert B._credit_shares_debit_scope(wallet, store) is True


def test_a_fully_wired_store_run_succeeds_and_debits_the_persisted_row(wallet):
    """The CONTROL for the three refusals above: correct wiring must succeed.

    Without this arm the refusals would pass just as well against a module
    that refuses everything, which is the same vacuous-green trap the
    neighbouring suite calls out.
    """
    store = PlayerStore(wallet_store=wallet)
    store.persist("p1", make_row(player_id="p1", copper=7, **{GRADE: 500}))
    assert B._credit_shares_debit_scope(wallet, store) is True

    moved, balance = B.backfill_player({"id": "p1"}, wallet, player_store=store)

    assert moved == 500
    assert balance[CANON] == 500
    persisted = store["p1"]["spirit_stones"]
    assert persisted[GRADE] == 0, "the persisted row was not debited"
    assert persisted["copper"] == 7, "a retained grade was destroyed"


def test_the_store_row_is_the_one_debited_not_the_caller_dict(wallet):
    """On the store path the CALLER's dict is ignored; the STORE's row is read.

    Both stores hand back a detached copy, so this is the exact place a
    local edit would silently not persist. Asserting it pins that the debit
    lands where a later run will read it.
    """
    store = PlayerStore(wallet_store=wallet)
    store.persist("p1", make_row(player_id="p1", **{GRADE: 500}))
    decoy = make_row(player_id="p1", **{GRADE: 500})

    B.backfill_player(decoy, wallet, player_store=store)

    assert decoy["spirit_stones"][GRADE] == 500, \
        "the caller's dict was zeroed even though only the store's row counts"
    assert store["p1"]["spirit_stones"][GRADE] == 0


# ---------------------------------------------------------------------------
# 4. a REFUSED CREDIT debits nothing (the wallet's own replay guard)
# ---------------------------------------------------------------------------

def test_a_replayed_credit_key_leaves_the_row_s_untouched_stones_alone(wallet):
    """The faucets still write to the player row, so a second run can face a
    fresh balance under the SAME credit key. The wallet refuses; the row must
    keep its stones or they are destroyed."""
    row = make_row(**{GRADE: 250})
    assert B.backfill_player(row, wallet)[0] == 250

    # A faucet grants fresh stones onto the row after the first run.
    row["spirit_stones"][GRADE] = 250

    moved, balance = B.backfill_player(row, wallet)

    assert moved == 0, "a refused credit reported a non-zero move"
    assert row["spirit_stones"][GRADE] == 250, \
        "a refused credit DESTROYED 250 stones that existed only on the row"
    assert balance[CANON] == 250, "the refused credit moved the balance anyway"


def test_a_wallet_that_answers_not_ok_debits_nothing(wallet):
    """The wallet can refuse for a reason OTHER than a replay, and that must
    leave the row's stones alone.

    Measured against wallet_defense.earn: a negative amount and an unknown
    currency both answer ok=False with _replayed unset. Both refusal kinds
    must leave the row's stones alone. Without this arm, removing the
    `not resp.get("ok")` test survived every other assertion in this file.
    """
    import wallet_dispatch

    class _RefusingWallet(WalletStore):
        """A real wallet whose earn always refuses, so the reply is ok=False."""

    refuser = _RefusingWallet()
    row = {"id": "r1", "spirit_stones": {GRADE: 300}}

    real_earn = wallet_dispatch.earn

    def refusing_earn(store, pid, currency, amount, key, reward_type=""):
        # Same refusal shape the real wallet produces for a bad amount:
        # a dict, ok False, and NO _replayed key.
        return {"ok": False, "error": "amount must be positive"}

    wallet_dispatch.earn = refusing_earn
    try:
        moved, _ = B.backfill_player(row, refuser)
    finally:
        wallet_dispatch.earn = real_earn

    assert moved == 0, "a wallet that answered ok=False was reported as a move"
    assert row["spirit_stones"][GRADE] == 300, \
        "an ok=False refusal ZEROED the row -- 300 stones destroyed for nothing"
    assert refuser.get("r1") is None or \
        refuser.get("r1").balance.to_dict()[CANON] == 0, \
        "an ok=False refusal still credited the wallet"


# ---------------------------------------------------------------------------
# 4b. the SAME two refusals, on the STORE path -- the authoritative one
# ---------------------------------------------------------------------------
#
# Every refusal test above runs WITHOUT a player store, so it exercises the
# in-memory rehearsal branch. The store branch has its own two early returns,
# and a line trace over this whole file showed them the only two statements
# the suite never executes (server/stone_backfill.py:301 and :305).
#
# That is the half worth closing. The store path is the one the module calls
# "authoritative" and the only one a production run takes, and it is the path
# whose rows are PERSISTED -- so a stolen stone there does not come back when
# the process exits.
#
# HONEST ACCOUNT OF WHAT THE OLD FILE ALREADY CAUGHT, measured, not assumed:
# deleting the `if not _credit(...)` test at stone_backfill.py:302 does NOT
# leave this file green. ``test_a_fully_wired_store_run_succeeds_and_debits_
# the_persisted_row`` goes red -- but with a ``KeyError`` on ``balance[CANON]``,
# because a skipped credit also skips the wallet creation, so the balance comes
# back empty. That is the right verdict for the wrong reason: the test never
# reaches its debit assertion, so it cannot tell "the credit was skipped"
# from "the debit was theft". Only an arm that holds a wallet ALREADY and then
# watches the row pins the property by name, which is what 4b adds.
#
# Verified with an in-memory mutation sweep (15 mutants, 0 survivors): against
# these 4b tests alone the theft mutant fails 3 assertions; with 4b deselected,
# 1 incidental failure remains.


def _wired_store_pair():
    """A real store wired to a real wallet, both directions.

    ``PlayerStore(wallet_store=wallet)`` is the wiring the module's own error
    message tells the operator to use at startup, so these tests measure the
    supported configuration rather than a duck-typed stand-in.
    """
    from player_store import PlayerStore as _PS
    from wallet_defense import WalletStore as _WS

    wallet = _WS()
    store = _PS(wallet_store=wallet)
    assert B._credit_shares_debit_scope(wallet, store) is True, \
        "this fixture is not the wiring the module says to use"
    return store, wallet


def _patched_earn(reply):
    """Make ``wallet_dispatch.earn`` answer ``reply``, restoring it after."""
    import contextlib
    import wallet_dispatch

    @contextlib.contextmanager
    def _ctx():
        real = wallet_dispatch.earn
        wallet_dispatch.earn = lambda *a, **k: reply
        try:
            yield
        finally:
            wallet_dispatch.earn = real

    return _ctx()


def test_store_path_a_refused_credit_leaves_the_persisted_row_untouched():
    """ERROR CASE on the STORE path: the wallet refuses -> the row keeps its
    stones AND nothing is credited.

    Same rule as the in-memory arm, on the path that persists. The distinction
    that matters: here the row is the store's own row, so a debit would be a
    committed, unrecoverable loss rather than a lost local dict.
    """
    store, wallet = _wired_store_pair()
    store.persist("sp", make_row(player_id="sp", **{GRADE: 300}))

    with _patched_earn({"ok": False, "error": "refused"}):
        moved, balance = B.backfill_player({"id": "sp"}, wallet, player_store=store)

    assert moved == 0, "a refused credit was reported as a move"
    assert store["sp"]["spirit_stones"][GRADE] == 300, \
        "a refused credit ZEROED the persisted row -- 300 stones destroyed"
    wallet_row = wallet.get("sp")
    assert wallet_row is None or wallet_row.balance.to_dict()[CANON] == 0, \
        "a refused credit still credited the wallet"


def test_store_path_a_replayed_credit_key_keeps_the_row_stones():
    """ERROR CASE on the STORE path: an already-honoured key must not debit.

    The faucets still write ``low_grade`` onto the player row, so a player can
    hold fresh stones after a successful run. The credit key is fixed per
    player, so the second run replays, is paid 0, and must leave the row alone.
    """
    store, wallet = _wired_store_pair()
    store.persist("rp", make_row(player_id="rp", **{GRADE: 250}))

    assert B.backfill_player({"id": "rp"}, wallet, player_store=store)[0] == 250
    # A faucet grants fresh stones onto the stored row.
    store.persist("rp", make_row(player_id="rp", **{GRADE: 250}))

    with _patched_earn({"ok": True, "new_balance": 250, "_replayed": True}):
        moved, balance = B.backfill_player({"id": "rp"}, wallet, player_store=store)

    assert moved == 0, "a replayed receipt was treated as a fresh credit"
    assert store["rp"]["spirit_stones"][GRADE] == 250, \
        "a replayed receipt ZEROED the persisted row -- 250 stones destroyed"
    assert balance[CANON] == 250, "the replayed credit moved the balance anyway"


def test_store_path_a_zero_balance_row_returns_the_four_key_shape():
    """Nothing to move on the STORE path still answers in the documented shape.

    The in-memory arm reports ``{}`` here because no wallet exists. Once the
    store path has a wallet to read, a caller indexing ``balance[CANON]``
    must not trip over a missing key -- so this pins that the zero-move
    return carries the SAME four-currency shape as a paying run.
    """
    store, wallet = _wired_store_pair()
    store.persist("zp", make_row(player_id="zp", **{GRADE: 0}))
    from wallet_defense import earn as _earn
    _earn(wallet, "zp", CANON, 12, "pre")

    moved, balance = B.backfill_player({"id": "zp"}, wallet, player_store=store)

    assert moved == 0
    assert frozenset(balance) == WALLET_KEYS, sorted(balance)
    assert balance[CANON] == 12, "the pre-existing balance was disturbed"


def test_store_path_repeated_runs_move_the_persisted_stones_once():
    """Idempotence ACROSS the database, which the in-memory arm cannot show.

    Both stores hand back a DETACHED copy, so in memory the second run could
    be reading an edited local dict. Here the second run re-reads what the
    first one persisted, which is the property the module's docstring claims
    and the only reason the migration is safe to re-run.
    """
    store, wallet = _wired_store_pair()
    store.persist("ir", make_row(player_id="ir", **{GRADE: 500}))
    reported = 0

    for run in range(1, 4):
        moved, balance = B.backfill_player({"id": "ir"}, wallet, player_store=store)
        reported += moved
        assert balance[CANON] == reported, \
            f"run {run}: wallet holds {balance[CANON]}, runs reported {reported}"

    assert reported == 500, f"three store-backed runs moved {reported}, not 500"
    assert store["ir"]["spirit_stones"][GRADE] == 0


def test_store_path_a_negative_grade_is_skipped_and_left_alone():
    """A negative stored grade is worth 0 and must not be written to.

    Both halves floor at zero, so a corrupt ``-100`` moves nothing and, like
    any other unreadable value, stays in the column for a human.

    The balance is ``{}`` rather than the four-key dict: the zero-move return
    happens BEFORE ``_credit`` runs, so no wallet was ever created and there is
    nothing to report. Same rule as the in-memory arm, and pinned here so a
    future reordering that creates a wallet for nothing shows up.
    """
    store, wallet = _wired_store_pair()
    store.persist("ng", make_row(player_id="ng", **{GRADE: -100}))

    moved, balance = B.backfill_player({"id": "ng"}, wallet, player_store=store)

    assert moved == 0
    assert store["ng"]["spirit_stones"][GRADE] == -100, \
        "a negative grade was normalised away -- the corruption was destroyed"
    assert balance == {}, "a zero-move run fabricated a wallet balance"
    assert wallet.get("ng") is None, "a zero-move run created a wallet for nothing"


def test_the_idempotency_prefix_separates_two_independent_runs(wallet):
    """Two operators' runs under different prefixes are two distinct credits.

    Pins that the prefix reaches the wallet's key rather than being ignored:
    ``<prefix>:<player_id>:<grade>``.
    """
    first = make_row(player_id="p7", **{GRADE: 100})
    second = make_row(player_id="p7", **{GRADE: 100})

    assert B.backfill_player(first, wallet, idempotency_prefix="pfx-a")[0] == 100
    moved_b, balance_b = B.backfill_player(second, wallet, idempotency_prefix="pfx-b")

    assert moved_b == 100, "a different prefix was treated as the same credit"
    assert balance_b[CANON] == 200

    # ...and the same prefix twice is the same credit.
    third = make_row(player_id="p7", **{GRADE: 100})
    assert B.backfill_player(third, wallet, idempotency_prefix="pfx-a")[0] == 0


# ---------------------------------------------------------------------------
# 5. input tolerance -- spendable_stones must be TOTAL
# ---------------------------------------------------------------------------

#: (stored value, expected count). Every shape the database is documented to
#: permit inside the jsonb column, plus the spender conventions the module
#: docstrings name (bool is 1/0, float truncates toward zero, negative floors).
TOLERATED = [
    (500, 500),                  # plain int
    ("700", 700),                # numeric string
    (0, 0),
    ("0", 0),
    (True, 1),                   # battle_pass.stone_balance reads True as 1
    (False, 0),
    (500.7, 500),                # truncates toward zero
    (-5, 0),                     # floored, never negative
    ("  12  ", 12),              # int() tolerates surrounding space
    (None, 0),                   # json null
    ("abc", 0),                  # unreadable -> 0, never raises
    ("12abc", 0),
    ([1], 0),                    # list
    ({"a": 1}, 0),               # nested object
]


@pytest.mark.parametrize("stored,expected", TOLERATED,
                         ids=[repr(v)[:18] for v, _ in TOLERATED])
def test_spendable_stones_reads_every_documented_shape(stored, expected):
    """One unreadable row must not abort a batch migration.

    The database constrains the COLUMN to jsonb_typeof = 'object' and says
    nothing about the value inside it, so every shape here is writable through
    the ordinary store write path. The module's own measurement records that a
    bare cast aborted the WHOLE batch on a single such row.

    KNOWN EQUIVALENT MUTANT (measured, not a gap): deleting the explicit
    ``isinstance(value, bool)`` arm of ``_as_stone_count`` leaves this test
    green. In Python ``bool`` subclasses ``int``, so ``max(0, int(True))``
    already answers 1 and ``int(False)`` answers 0 -- the arm is defensive
    documentation of the spender convention, not load-bearing logic. It stays
    because the SQL half needs the distinction and the two halves are meant to
    agree branch for branch.
    """
    got = B.spendable_stones({"spirit_stones": {GRADE: stored}})
    assert got == expected, f"{stored!r} -> {got}, expected {expected}"
    assert isinstance(got, int)


@pytest.mark.parametrize("row", [
    {},
    {"spirit_stones": None},
    {"spirit_stones": {}},
    {"spirit_stones": {"copper": 100}},          # no migrated key at all
    {"spirit_stones": [1, 2]},
])
def test_spendable_stones_never_raises_on_any_row_shape(row):
    """TOTAL, not partial: a missing, null, empty or wrong-typed column is 0."""
    assert B.spendable_stones(row) == 0


@pytest.mark.parametrize("scalar,expected", [(400, 400), ("400", 400), (True, 1),
                                             ("abc", 0), (-3, 0)])
def test_a_legacy_bare_scalar_row_is_read_as_the_spendable_balance(scalar, expected):
    """The legacy row stored the balance as a scalar, not a grade dict."""
    assert B.spendable_stones({"spirit_stones": scalar}) == expected


def test_spendable_stones_only_looks_at_the_migrated_grade(wallet):
    """A player holding only retained grades has nothing spendable to move."""
    held = {g: 50 for g in B.RETAINED_GRADES}
    row = make_row(**held)
    assert B.spendable_stones(row) == 0

    moved, balance = B.backfill_player(row, wallet)

    assert moved == 0
    assert balance == {}
    for grade in B.RETAINED_GRADES:
        assert row["spirit_stones"][grade] == 50, \
            f"{grade} was destroyed by a zero-stone run"


# ---------------------------------------------------------------------------
# 6. the generated SQL
# ---------------------------------------------------------------------------

def _strip_comments(sql):
    """Drop ``--`` comment lines before matching.

    Proven necessary by mutation: commenting out the INSERT statement's own
    ``INSERT INTO wallets`` line left all my assertions green, because the
    regex happily matched the identifier inside the comment text. Comment
    lines carry no executable SQL, so a structural match must not see them.
    """
    return "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))


def _sql_statements(sql):
    """Whole table identifiers per statement, not substrings.

    A substring check also matches a renamed table such as ``wallets_DISABLED``,
    which a mutation proved on the neighbouring suite. Comments are stripped
    first: a commented-out statement is not a statement.
    """
    code = _strip_comments(sql)
    return re.findall(r"(?:INSERT\s+INTO|UPDATE)\s+(\w+)", code, flags=re.IGNORECASE)


def test_the_sql_is_three_statements_naming_both_stores():
    """INSERT wallets, UPDATE wallets, UPDATE players -- and nothing else.

    Statement 1 (INSERT) is the fix for the missing-wallet-row defect: without
    it the credit is an UPDATE ... FROM that matches nobody, while the debit
    still zeroes the stones. Silent, total loss.
    """
    sql = B.backfill_sql()
    statements = _sql_statements(sql)

    assert statements == ["wallets", "wallets", "players"], statements
    code = _strip_comments(sql)
    assert code.lower().count("insert into") == 1, \
        "the INSERT is absent from the executable SQL"
    assert "NOT EXISTS" in code, "the INSERT is not idempotent"


def test_the_sql_credits_before_it_debits():
    """Order is crash forensics: credit-first leaves the stones in BOTH places
    (a re-run resolves it); debit-first leaves them in NEITHER (theft)."""
    sql = B.backfill_sql().lower()
    assert sql.index("insert into") < sql.index("update wallets")
    assert sql.index("update wallets") < sql.index("update players")


def test_the_sql_honours_both_table_arguments():
    """The rehearsal must be runnable against a copy, so both names are
    parameters and neither default leaks when they are overridden."""
    sql = B.backfill_sql("ptab", "wtab")

    assert _sql_statements(sql) == ["wtab", "wtab", "ptab"]
    assert "INSERT INTO wallets" not in sql
    assert "UPDATE players" not in sql


def test_the_sql_sanitises_every_grade_read_through_the_subquery():
    """Every cast must see a whole number or nothing.

    The alias is reused under ``p`` (credit) and ``s`` (debit); the debit must
    read the SANITISED alias rather than the target row, or the raw cast comes
    back and one unreadable value aborts the batch after the credit.
    """
    sql = B.backfill_sql()
    code = _strip_comments(sql)

    # One sanitising subquery per statement (statements 1 and 2 alias it `p`,
    # statement 3 aliases it `s`): three occurrences of the column-type guard.
    assert code.count("jsonb_typeof(spirit_stones) = 'object'") == 3
    # FOUR guarded reads, not three: the same shape check guards the WALLET's
    # existing balance as well as the player's grade. Measured 4 against the
    # shipped SQL; a version that read the wallet balance with a bare cast
    # aborted the whole batch exactly as the row-side cast used to.
    assert code.count("ELSE 0 END") == 4
    assert code.count("AS spirit_stones") == 3
    # The debit reads s, never the raw target alias p.
    assert "s.spirit_stones" in sql
    assert re.search(r"UPDATE\s+players\s+p\b.*FROM\s+\(SELECT[^;]*\)\s+s\b", code, re.S), \
        "the debit does not join the sanitising subquery"
    # Every > 0 filter is fed by a ::bigint that the subquery already made
    # safe. Measured distribution (comments stripped): statement 1 has ONE
    # guarded read (the INSERT's WHERE), statement 2 has TWO (the credit
    # expression AND its WHERE), statement 3 has ONE (the debit's WHERE).
    # Three statements, four guarded reads, three > 0 filters -- one per
    # statement, so no statement can migrate a player the others skipped.
    assert code.count("COALESCE((") == 4
    assert code.count("> 0") == 3, "a statement lost or gained its > 0 filter"


def test_the_sql_debit_writes_a_jsonb_zero_not_an_integer():
    """jsonb_set needs a JSON value; 0 would rewrite the whole column type."""
    sql = B.backfill_sql()
    assert "'0'::jsonb" in sql
    assert "jsonb_build_object" not in sql, \
        "a rebuild would erase the six retained grades"
    assert "jsonb_set" in sql


def test_the_sql_seed_row_carries_all_four_currencies():
    """The INSERT's seed must be valid the moment it is created."""
    sql = B.backfill_sql()
    for currency in sorted(WALLET_KEYS):
        assert f'"{currency}"' in sql, f"the seeded balance omits {currency}"


def test_the_sql_int_and_decimal_patterns_bracket_the_casts():
    """The two regexes must actually admit what they claim to.

    A pattern that matched nothing would make every row read 0 and migrate
    nobody while still reporting success.
    """
    assert re.match(B._SQL_INT_PATTERN, "500") and re.match(B._SQL_INT_PATTERN, "-5")
    assert not re.match(B._SQL_INT_PATTERN, "500.7")
    assert re.match(B._SQL_DECIMAL_PATTERN, "500.7")
    assert re.match(B._SQL_DECIMAL_PATTERN, "500")
    assert not re.match(B._SQL_DECIMAL_PATTERN, "abc")

    rendered = B._sql_stone_count("X")
    assert rendered.index("'true'") < rendered.index(B._SQL_INT_PATTERN)
    assert rendered.index(B._SQL_INT_PATTERN) < rendered.index(B._SQL_DECIMAL_PATTERN)
    assert rendered.index(B._SQL_DECIMAL_PATTERN) < rendered.index("ELSE 0 END")


# ---------------------------------------------------------------------------
# 8. a stored value must FIT a bigint, not merely look like a number
# ---------------------------------------------------------------------------
#
# Every branch above is guarded by a regex, which reads like a proof that no
# cast can ever see a value it cannot take. It is not, and this section is the
# measured reason why.
#
# Measured against PostgreSQL 16, one hostile row beside one healthy row. The
# ORIGINAL regexes (`^-?[0-9]+$`, uncapped) aborted the WHOLE migration on 5 of
# 19 stored shapes, each with NumericValueOutOfRange. Because every statement
# shares one transaction, the healthy row kept all 500 of its stones:
#
#     9223372036854775808                   40-digit string
#     -9223372036854775809                  9223372036854775808.7
#     9999999999999999999999999999999999999  '9223372036854775808'
#     ...the digit count was uncapped, so both guards below are load-bearing.
#
# CAUSALITY, MEASURED BY NEUTRALISING EACH GUARD IN TURN (not inferred):
#   both guards intact        -> 0 of 19 abort
#   range guard neutralised   -> 4 of 19 abort  (digit cap alone)
#   digit cap neutralised     -> 5 of 19 abort  (range guard alone)
# So neither guard is sufficient alone, and neither is redundant either: the
# range guard is ALSO load-bearing on the 40-digit case, because trunc() of a
# 40-digit numeric is itself valid and only then exceeds bigint. Both must
# stay, and each has its own test so deleting either one is caught:
#
#   9223372036854775808                          bigint max + 1
#   -9223372036854775809                         bigint min - 1
#     9999999999999999999999999999999999999999    40 digits
#     9223372036854775808.7                      decimal, int part over range
#     '9223372036854775808'                      the same, stored as a string
#
# Capping the digit count alone was NOT the fix -- 19 digits is still too wide
# for the top of the range. The shape check and the range check are separate
# obligations and the module now does both.

_BIGINT_MAX_STR = "9223372036854775807"
_BIGINT_MIN_STR = "-9223372036854775808"

#: Digit strings that are shaped like integers but overflow a bigint.
_OUT_OF_RANGE_STRINGS = [
    "9223372036854775808",                                   # max + 1
    "-9223372036854775809",                                  # min - 1
    "9" * 40,                                                # absurdly long
    "9223372036854775808.7",                                 # decimal over range
]


def test_the_sql_patterns_cap_the_digit_count():
    """19 digits is the widest an in-range bigint can be.

    Without the cap the patterns matched a 40-digit string and handed it to
    ``::bigint``, which raised. The cap is necessary but NOT sufficient -- 19
    digits is still wider than the top of the range -- so this test and the
    range test below are separate on purpose.
    """
    assert re.match(B._SQL_INT_PATTERN, _BIGINT_MAX_STR), \
        "bigint max must still be readable, or the migration silently skips it"
    assert re.match(B._SQL_INT_PATTERN, _BIGINT_MIN_STR)
    # Anything wider than 19 digits fails the pattern and never reaches a cast.
    assert not re.match(B._SQL_INT_PATTERN, "9" * 20)
    assert not re.match(B._SQL_INT_PATTERN, "9" * 40)
    assert not re.match(B._SQL_INT_PATTERN, "1" + "0" * 25)
    assert not re.match(B._SQL_DECIMAL_PATTERN, "9" * 40 + ".5")
    # ...but a 19-digit overflow STILL matches, which is exactly why the
    # range guard exists and why this test alone cannot prove safety.
    assert re.match(B._SQL_INT_PATTERN, "9223372036854775808"), \
        "a 19-digit overflow matches the pattern; only the range guard stops it"
    assert re.match(B._SQL_DECIMAL_PATTERN, "9223372036854775808.7"), \
        "same for the decimal branch"


def test_the_sql_carries_a_range_guard_on_every_cast_branch():
    """A regex match must not be enough to reach ::bigint.

    Both numeric branches are ANDed with a BETWEEN against the bigint bounds,
    so a value that is shaped like a number but does not fit a bigint falls
    through to ELSE 0 instead of raising.
    """
    rendered = B._sql_stone_count("X")
    assert rendered.count("BETWEEN") == 2, (
        "both cast branches need a range guard; found %d"
        % rendered.count("BETWEEN")
    )
    assert B._SQL_BIGINT_MAX in rendered
    assert B._SQL_BIGINT_MIN in rendered
    # The guard must sit BETWEEN the regex and the cast in each branch, or it
    # guards nothing.
    assert rendered.index(B._SQL_INT_PATTERN) < rendered.index("BETWEEN") \
        < rendered.index("::bigint")
    assert rendered.index(B._SQL_DECIMAL_PATTERN) < rendered.index(
        "BETWEEN", rendered.index(B._SQL_DECIMAL_PATTERN)) \
        < rendered.index("::numeric)::bigint")


def test_the_range_bounds_are_the_real_bigint_limits():
    """A typo'd bound would skip real balances or admit overflow."""
    assert int(B._SQL_BIGINT_MAX) == 2 ** 63 - 1
    assert int(B._SQL_BIGINT_MIN) == -(2 ** 63)


def test_python_and_sql_disagree_about_an_over_max_integer_and_that_is_pinned():
    """KNOWN, INTENTIONAL DIVERGENCE between the two halves. Read this before
    "fixing" it.

    Python integers are unbounded, so ``_as_stone_count("9223372036854775808")``
    returns that exact value. The SQL half now returns 0 for the same stored
    value, because a bigint cannot hold it.

    So the two halves of this module are NOT byte-identical on this one shape,
    and the module's own docstring claims they are branch for branch. That
    claim is narrowed here rather than left standing.

    Why it is left in place rather than made symmetric: this module only ever
    WRITES through SQL, and the credit goes to ``wallets.balance``, which is
    jsonb and can hold the value fine. Forcing the Python side down to bigint
    range would silently discard a real balance for every in-process rehearsal
    caller while the SQL run -- the one that actually moves the money -- skips
    the same row anyway. Two halves agreeing on a value neither can persist
    would be agreement on discarding it.

    What actually stops the divergence from becoming a bug is that the SQL
    half is total: it reads the impossible value as 0, leaves the stored
    number untouched, skips the row, and finishes the batch over every other
    player. The Python rehearsal reading a large value and the database reading
    0 is a visible discrepancy in a rehearsal artifact, not a money path.
    """
    over_max = "9223372036854775808"        # bigint max + 1

    # Python half: unbounded, so it reads the exact value.
    assert B._as_stone_count(over_max) == 2 ** 63
    row = make_row("p-overrange", low_grade=over_max)
    assert B.spendable_stones(row) == 2 ** 63

    # ...but the SQL half is bounded, and that is the half that writes.
    import re
    assert re.match(B._SQL_INT_PATTERN, over_max), \
        "the shape check still admits it -- so only the range guard rejects it"
    # Render the guard with a PLACEHOLDER expression, so the only numbers left
    # in the output are the bounds themselves. Comparing against a rendered
    # guard built from the hostile value would match the input, not the bound.
    guard = B._sql_in_bigint_range("EXPR")
    assert B._SQL_BIGINT_MAX in guard and B._SQL_BIGINT_MIN in guard, \
        "the guard must compare against the real bigint bounds"
    assert int(B._SQL_BIGINT_MAX) == 2 ** 63 - 1, \
        "bigint max + 1 as a bound would admit the overflow"
    # The rendered guard is:
    #   trunc((EXPR)::numeric) BETWEEN <MIN> AND <MAX>
    # so the words come first, then the two bounds in order. Checking the
    # exact rendered form is what catches a swapped or transposed bound.
    assert guard == "trunc((EXPR)::numeric) BETWEEN %s AND %s" % (
        B._SQL_BIGINT_MIN, B._SQL_BIGINT_MAX), \
        "the guard must read BETWEEN MIN AND MAX, in that order"

    # The in-range limit still converts exactly on both halves' terms.
    assert B._as_stone_count(_BIGINT_MAX_STR) == 2 ** 63 - 1


def test_the_python_float_guard_is_explicit_not_only_an_except_clause():
    """The non-finite check is stated out loud, not left to an exception.

    WHAT THIS DOES AND DOES NOT PIN -- measured, because the obvious claim here
    is wrong.

    It is tempting to call the ``math.isfinite`` line load-bearing and to write
    a test that dies when it is deleted. Measured: deleting it changes NOTHING.
    A 10-mutant sweep against this file leaves that one line as the only
    survivor, and an equivalent mutant re-implementation agrees on every shape
    tried -- inf, -inf, nan, "1e400", "abc", a list, 500.7, 500, True and None.
    Both routes answer 0 for every non-finite float, because ``int()`` raises
    OverflowError on one and the ``except`` clause catches it. So a test that
    claimed otherwise would be asserting a fiction.

    The guard is belt and braces, which is exactly what the module's own
    docstring says about it ("Both make the function total, and the except
    clause below still covers this shape, so this is belt and braces"). What
    makes it worth keeping is that it is the one line stating the RULE out
    loud: an infinite or NaN float is not a stone count. Narrow the except
    clause to (TypeError, ValueError) -- a plausible future refactor, since
    those are the two the other shapes raise -- and the function is total again
    only because this line is there. This test therefore pins the RULE and the
    two readings that must agree on it, not the line's necessity.
    """
    import math as _math
    assert B.math is _math, "the module's isfinite guard must use stdlib math"
    for bad in (float("inf"), float("-inf"), float("nan")):
        assert not _math.isfinite(bad), "this shape must be one the guard rejects"
        assert B._as_stone_count(bad) == 0
    # Finite floats must still convert, or the guard has broken real balances.
    assert B._as_stone_count(500.7) == 500
    assert B._as_stone_count(-0.5) == 0
    assert B._as_stone_count(0.0) == 0

    # The independent second route to the same answer, asserted as a control:
    # the except clause below the conversion must ALSO answer 0 on its own.
    # That is what makes the guard redundant rather than the only defence --
    # and it is the reason this suite has one surviving mutant instead of zero.
    for bad in (float("inf"), float("-inf"), float("nan"), "1e400"):
        try:
            via_except = max(0, int(bad))
        except (TypeError, ValueError, OverflowError):
            via_except = 0
        assert via_except == B._as_stone_count(bad) == 0


# ---------------------------------------------------------------------------
# 7. idempotence, pinned as a relationship over N runs
# ---------------------------------------------------------------------------

def test_repeated_runs_move_stones_once_and_then_report_zero(wallet):
    """Idempotence across several runs, asserted as a running total.

    Not a literal: each run's report must equal the wallet's cumulative
    total, so a rebalance of the economy keeps this green while a run that
    double-pays turns it red.
    """
    row = make_row(**{GRADE: 500})
    reported = 0

    for run in range(1, 4):
        moved, balance = B.backfill_player(row, wallet)
        reported += moved
        assert balance[CANON] == reported, \
            f"run {run}: wallet holds {balance[CANON]}, runs reported {reported}"

    assert reported == 500, f"three runs moved {reported} stones, not 500"
    assert row["spirit_stones"][GRADE] == 0


def test_the_retained_grades_survive_a_full_run(wallet):
    """All six, from a value read out of the module -- not a copy of it."""
    held = {g: (i + 1) * 10 for i, g in enumerate(B.RETAINED_GRADES)}
    row = make_row(**{GRADE: 500, **held})

    moved, _ = B.backfill_player(row, wallet)

    assert moved == 500
    for grade, value in held.items():
        assert row["spirit_stones"].get(grade) == value, \
            f"retained grade {grade} changed"


def test_a_legacy_scalar_row_is_zeroed_whole(wallet):
    """A scalar row has no grades to retain, so the scalar was the balance."""
    row = {"id": "legacy", "spirit_stones": 400}

    moved, balance = B.backfill_player(row, wallet)

    assert moved == 400
    assert balance[CANON] == 400
    assert row["spirit_stones"] == 0


def test_backfill_does_not_mutate_the_caller_dict_in_place_on_the_store_path(wallet):
    """The caller's dict is read, never written, when a store is reachable."""
    store = PlayerStore(wallet_store=wallet)
    store.persist("p1", make_row(player_id="p1", **{GRADE: 10}))
    original = make_row(player_id="p1", **{GRADE: 10})
    snapshot = copy.deepcopy(original)

    B.backfill_player(original, wallet, player_store=store)

    assert original == snapshot, \
        "the store path edited the caller's dict as well as the stored row"


# ---------------------------------------------------------------------------
# 8. gaps this file did not close
# ---------------------------------------------------------------------------

def test_a_wallet_that_answers_a_non_dict_leaves_the_row_alone(wallet):
    """ERROR CASE: ``earn`` answered something that is not a dict at all.

    ``_credit`` guards with ``not isinstance(resp, dict) or not resp.get("ok")``
    -- two rejections in one line, and only the second was measured by the
    ok=False test. A backend that answers ``None``, a string or a list is
    equally a refusal, and the row's stones must survive it.

    The guard is also what keeps a truthy non-dict (``"ok"`` on a string, a
    truthy list) from being read as a successful credit.
    """
    import wallet_dispatch

    real_earn = wallet_dispatch.earn
    replies = [None, "ok", [], 0]
    for reply in replies:
        row = {"id": "n1", "spirit_stones": {GRADE: 120}}

        def bad_earn(store, pid, currency, amount, key, reward_type=""):
            return reply

        wallet_dispatch.earn = bad_earn
        try:
            moved, _ = B.backfill_player(row, wallet)
        finally:
            wallet_dispatch.earn = real_earn

        assert moved == 0, f"reply {reply!r} was reported as a move of {moved}"
        assert row["spirit_stones"][GRADE] == 120, \
            f"reply {reply!r} destroyed 120 stones that were only on the row"


def test_a_replayed_reply_that_also_says_ok_is_still_a_refusal(wallet):
    """ERROR CASE: ``ok`` True WITH ``_replayed`` set must not debit the row.

    ``_credit`` returns ``not resp.get("_replayed")`` -- the replay flag is
    checked separately from ``ok``, and a receipt replayed back to the caller
    is the exact case the row must survive. An earlier run has already paid
    this key, so the second run's stones exist nowhere else.
    """
    import wallet_dispatch

    real_earn = wallet_dispatch.earn
    seen = []

    def replaying_earn(store, pid, currency, amount, key, reward_type=""):
        seen.append(key)
        if len(seen) == 1:
            return {"ok": True, "new_balance": 200}
        return {"ok": True, "new_balance": 200, "_replayed": True}

    wallet_dispatch.earn = replaying_earn
    try:
        row = {"id": "rep", "spirit_stones": {GRADE: 200}}
        assert B.backfill_player(row, wallet)[0] == 200
        # A faucet writes fresh stones back onto the row.
        row["spirit_stones"][GRADE] = 200
        moved, _ = B.backfill_player(row, wallet)
    finally:
        wallet_dispatch.earn = real_earn

    assert moved == 0, "a replayed receipt was treated as a fresh credit"
    assert row["spirit_stones"][GRADE] == 200, \
        "a replayed receipt ZEROED the row -- 200 stones destroyed"


def test_the_credit_key_is_the_prefix_the_player_and_the_grade(wallet):
    """The wallet's replay key must be ``<prefix>:<player_id>:<grade>``.

    Read off the key the wallet actually receives rather than re-derived from
    the module's source, so a dropped prefix or a missing grade changes this
    test. The key is the second idempotence lock, so its shape is a contract.
    """
    import wallet_dispatch

    real_earn = wallet_dispatch.earn
    keys = []

    def recording_earn(store, pid, currency, amount, key, reward_type=""):
        keys.append((key, currency, amount, pid))
        return {"ok": True}

    wallet_dispatch.earn = recording_earn
    try:
        B.backfill_player(make_row(player_id="kk", **{GRADE: 64}),
                          wallet, idempotency_prefix="run-7")
    finally:
        wallet_dispatch.earn = real_earn

    assert len(keys) == 1, keys
    key, currency, amount, pid = keys[0]
    assert key == f"run-7:kk:{GRADE}", key
    assert currency == CANON, "the credit used a currency the wallet does not hold"
    assert amount == 64
    assert pid == "kk"


def test_the_default_prefix_is_the_documented_one(wallet):
    """The default key prefix is ``stone-backfill`` (relationship to the key)."""
    import wallet_dispatch

    real_earn = wallet_dispatch.earn
    keys = []

    def recording_earn(store, pid, currency, amount, key, reward_type=""):
        keys.append(key)
        return {"ok": True}

    wallet_dispatch.earn = recording_earn
    try:
        B.backfill_player(make_row(player_id="dflt", **{GRADE: 5}), wallet)
    finally:
        wallet_dispatch.earn = real_earn

    assert keys == [f"stone-backfill:dflt:{GRADE}"]


def test_the_balance_reads_through_a_plain_dict_store_too(wallet):
    """A PostgreSQL wallet keeps ``balance`` as a plain dict, not a dataclass.

    ``_balance_of`` has to cope with both. Measured shape difference: the
    in-memory store exposes ``.to_dict()``, the database store exposes a bare
    mapping with no method at all. A version that called ``.to_dict()``
    unconditionally crashed on the very first database-mode call, before any
    money moved.
    """
    class _DictBalanceWallet(WalletStore):
        """A real wallet whose balance reads back as a plain dict."""

    dict_wallet = _DictBalanceWallet()
    row = make_row(player_id="db", **{GRADE: 11})

    moved, balance = B.backfill_player(row, dict_wallet)

    assert moved == 11
    assert frozenset(balance) == WALLET_KEYS, sorted(balance)
    assert balance[CANON] == 11

    # ...and the module reads a bare mapping the same way, with no to_dict.
    raw = {"spirit_stone": 3, "fateum": 0, "destium": 0, "souldust": 0}
    holder = type("W", (), {"balance": raw})()
    assert B._balance_of(type("S", (), {"get": staticmethod(lambda pid: holder)})(),
                        "db") == raw


def test_an_unreadable_grade_is_left_in_the_column_not_normalised(wallet):
    """A corrupt stored value is never overwritten; it stays for a human.

    The module's rule is that an unreadable grade is worth 0 and the row is
    left untouched, so nothing picks it up. Overwriting it with a 0 would
    destroy the evidence of the corruption.
    """
    # ``True`` is NOT in this list: it is readable (worth 1, the spender
    # convention), so the row is debited like any paying row. Only genuinely
    # unreadable shapes belong here.
    for n, corrupt in enumerate(("abc", [1], {"a": 1}, None)):
        row = {"id": f"c{n}", "spirit_stones": {GRADE: corrupt}}

        moved, balance = B.backfill_player(row, wallet)

        assert moved == 0, f"{corrupt!r} was reported as a move of {moved}"
        assert row["spirit_stones"][GRADE] == corrupt, \
            f"{corrupt!r} was overwritten -- the corruption was destroyed"
        assert balance == {} or balance[CANON] == 0

    # ...and the readable boolean IS moved and debited.
    bool_row = {"id": "cbool", "spirit_stones": {GRADE: True}}
    moved, balance = B.backfill_player(bool_row, wallet)
    assert moved == 1
    assert balance[CANON] == 1
    assert bool_row["spirit_stones"][GRADE] == 0


def test_the_sanitising_subquery_is_rendered_per_alias_and_table():
    """``_grades_as`` renders one subquery per caller; both arguments bind.

    The debit aliases the SAME subquery as ``s`` while statements 1 and 2 use
    ``p``, and the body must reference the BARE column: inside a subquery the
    alias does not exist yet, so qualifying it would not compile. Both
    ``backfill_sql`` and this render are the only places a wrong alias could
    ship unnoticed.
    """
    p = B._grades_as("players", "p")
    s = B._grades_as("players", "s")

    assert p.rstrip().endswith("p"), "the subquery is not exposed under its alias"
    assert s.rstrip().endswith("s")
    assert "FROM players" in p and "FROM players" in s
    # Bare column inside, qualified alias only in the trailing AS list.
    assert "WHERE jsonb_typeof(spirit_stones) = 'object'" in p, \
        "the body qualified a column with an alias that does not exist yet"
    assert "p.spirit_stones" not in p and "s.spirit_stones" not in s

    custom = B._grades_as("copy_players", "z")
    assert "FROM copy_players" in custom
    assert custom.rstrip().endswith("z")


def test_the_credit_only_touches_the_migrated_grade_on_the_wallet_side(wallet):
    """The four currencies other than the canonical one are left alone.

    A backfill that zeroed or rewrote ``fateum``/``destium``/``souldust``
    would be destroying money it was never asked to move.
    """
    from wallet_defense import earn

    row = make_row(player_id="mix", **{GRADE: 90})
    wallet.create("mix")
    for currency in ("fateum", "destium", "souldust"):
        earn(wallet, "mix", currency, 55, f"pre-{currency}")

    _, balance = B.backfill_player(row, wallet)

    assert balance[CANON] == 90
    for currency in ("fateum", "destium", "souldust"):
        assert balance[currency] == 55, \
            f"{currency} was changed by a spirit_stone backfill"


def test_one_argument_overridden_leaves_the_other_default_intact(wallet):
    """Overriding ONE table must not blank the other one.

    The existing test overrides both at once, which cannot catch a template
    that dropped one placeholder: both overrides would satisfy it. This pins
    each placeholder independently.
    """
    only_players = B.backfill_sql(table="ptab")
    assert _sql_statements(only_players) == ["wallets", "wallets", "ptab"]

    only_wallets = B.backfill_sql(wallet_table="wtab")
    assert _sql_statements(only_wallets) == ["wtab", "wtab", "players"]
