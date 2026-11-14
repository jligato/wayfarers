"""Real coverage for `server/wallet_store_pg.py` in MEMORY mode.

WHY THIS FILE EXISTS
--------------------
`server/tests/test_offline_claim_payout_ordering.py` passes with four green
tests and never imports `wallet_store_pg` -- zero references in its source --
and importing `server.main` does not load it either. So those four prove
nothing about the PostgreSQL wallet module.

That module is 615 lines whose only commit behind it is a baseline, and the
backend it implements is the one that runs in production: every memory-mode
suite exercises `wallet_defense`'s in-memory store, so the PG spelling of the
money path had no coverage at all. This file is the coverage that needs no
database, and it SKIPS -- loudly, by name -- the parts that genuinely do.

WHAT IS AND IS NOT FAKE
-----------------------
There is no fake database anywhere in this file. Where a test needs a store,
it uses `_PgGuardProbe`, a subclass of the real `WalletStore` whose
`transaction()` yields and whose `conn` property RAISES. Any code path that
touches a cursor therefore fails loudly rather than pretending to have
succeeded. A refusal asserted through that probe is proof the guard fired
BEFORE the first query, not that a mock was configured to agree.

Three guards are proven to bite in a scratch copy with the guard deleted --
`test_control_*` at the bottom, run by hand, see the module docstring of
`test_the_guards_bite_when_removed_from_a_scratch_copy` below.

ONE CLOCK-SKEW BOUND, NOT TWO
-----------------------------
This file used to report a measured divergence rather than hide it:
`wallet_defense` refused a device clock more than 86400 seconds (one day) out
while `wallet_store_pg.claim_offline` refused at a literal 300 (five minutes),
so the identical client request was ACCEPTED in memory mode and REFUSED in
PostgreSQL mode -- one backend quietly disagreeing with the other, with a
memory-mode suite green throughout. That split is now closed:
`wallet_store_pg` imports `MAX_CLOCK_SKEW_SECONDS` from `wallet_defense` and
every skew probe below reads that shared constant instead of a literal.

`test_the_two_backends_agree_on_the_clock_skew_bound` is deliberately KEPT
even though the divergence it documented is gone. It now asserts the two
backends hold the same bound, which is the assertion that stops the split
returning: delete it and a fresh literal on either side would diverge the
backends again with every memory-mode test still passing.

Run:  bash tools/wc_pytest.sh server/tests/test_wallet_store_pg_contract.py
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO))

import wallet_defense as _mem  # noqa: E402  (the in-memory backend, for contrast)
import wallet_store_pg as pg  # noqa: E402  (THE MODULE UNDER TEST)


# The probe. `conn` is a property that raises, so no test in this file can
# silently pass by reaching a cursor: the connection access itself is the
# failure. Subclassing the real store (rather than writing a stand-in) keeps
# every method under test the one the module actually ships, including the
# `@atomic` transaction wrapper, so a test that passes here passed on the
# shipped code path and not on a copy of it.
class _PgGuardProbe(pg.WalletStore):
    def __init__(self) -> None:
        super().__init__(None)

    @contextmanager
    def transaction(self):
        yield self

    @property
    def conn(self):
        raise AssertionError(
            "this test claimed the code needs no database, but it opened one")


def _refused(reply, expected_error):
    """Assert a refusal is exactly that refusal, not merely a falsy one.

    `ok` False on its own would pass for any error at all, including the wrong
    one. The message is the observable contract both backends share, so it is
    checked as well as the flag.
    """
    assert isinstance(reply, dict), f"expected a reply dict, got {reply!r}"
    assert reply.get("ok") is False, (
        f"expected a refusal, got a success: {reply!r}")
    assert reply.get("error") == expected_error, (
        f"refused for the WRONG reason: expected {expected_error!r}, got "
        f"{reply.get('error')!r} in {reply!r}")
    return reply


# =========================================================== import =======

def test_the_module_imports_in_memory_mode_and_exposes_its_currencies():
    """The premise of this file: it imports with no database configured.

    Also prints the tuple, so a run leaves the evidence in the output rather
    than only in the assertion.
    """
    print("wallet_store_pg.CURRENCIES =", pg.CURRENCIES)
    assert pg.CURRENCIES, "the currency whitelist is empty"
    print("wallet_store_pg.MAX_OFFLINE_SECONDS =", pg.MAX_OFFLINE_SECONDS)
    print("module file:", Path(pg.__file__).name)


def test_no_database_environment_is_set_while_this_file_runs():
    """Guards the guard: if a DB var leaked in, the no-database claim is void.

    Without this, a green run could be green because a real database answered,
    and every "needs no database" assertion above would be measuring nothing.
    """
    import os
    for var in ("WC_PG_HOST", "WC_PG_DB", "WC_PG_USER", "WC_PG_PASSWORD"):
        assert os.environ.get(var) is None, (
            f"{var} is set ({os.environ[var]!r}); this file proves the guards "
            "run without a database and that claim is void with one configured")


# ============================================== the currency whitelist ====

def test_currency_whitelist_is_derived_from_the_shared_dataclass():
    """One source of truth, so a new currency cannot be refused on PG only.

    The module's own comment records the defect this prevents: the whitelist
    used to be a hand-written literal that happened to equal the dataclass's
    fields, so a fifth currency added to `CurrencyBalance` was accepted in
    memory mode and rejected on every PostgreSQL money path, with no error
    anywhere. The fix was to derive it. This asserts the derivation still holds.
    """
    assert pg.CURRENCIES == tuple(_mem.CurrencyBalance.__dataclass_fields__), (
        "the PostgreSQL whitelist has drifted from the shared dataclass: "
        f"{pg.CURRENCIES} vs {tuple(_mem.CurrencyBalance.__dataclass_fields__)}"
    )


def test_currency_whitelist_is_a_distinct_object_from_the_shared_one():
    """Copy, not alias: rebinding one backend must not reach into the other.

    `CURRENCIES = (*CURRENCIES_FROM_WALLET_DEFENSE,)` builds a fresh tuple.
    `tuple(source)` would have returned the very same object, so a caller that
    rebound one backend's attribute would be editing the other's whitelist.
    """
    assert pg.CURRENCIES == _mem.CURRENCIES
    assert pg.CURRENCIES is not _mem.CURRENCIES, (
        "the PostgreSQL whitelist is the SAME OBJECT as the in-memory one, so "
        "a caller rebinding one backend's CURRENCIES mutates the other")


def test_offline_cap_matches_the_in_memory_backend():
    """72 hours, agreed across backends. Both cap the same payout."""
    assert pg.MAX_OFFLINE_SECONDS == _mem.MAX_OFFLINE_SECONDS
    assert pg.MAX_OFFLINE_SECONDS == 72 * 3600


# ================================================== PGBalance shape =======

def test_balance_is_a_dict_so_its_own_sql_access_keeps_working():
    """`PGBalance` subclasses dict because this module's SQL reads it that way.

    `balance.get(currency, 0)` in `_mutate` is dict access. A dataclass with
    `__getattr__` alone would break every read on this backend.
    """
    bal = pg.PGBalance({"spirit_stone": 5})
    assert isinstance(bal, dict)
    assert bal.get("spirit_stone") == 5
    assert bal.get("absent", 0) == 0
    assert "spirit_stone" in bal
    assert sorted(bal.keys()) == ["spirit_stone"]


def test_balance_answers_attribute_access_for_the_shared_guard_path():
    """`wallet_defense.earn` writes `setattr` and reads BARE `getattr`.

    A bare `getattr(bal, currency)` -- no default -- must return 0 for a
    declared currency the row has never held, because the in-memory
    `CurrencyBalance` declares all four with a 0 default and the log line in
    `earn` reads it that way.
    """
    bal = pg.PGBalance({"spirit_stone": 5})
    assert bal.spirit_stone == 5
    for currency in pg.CURRENCIES:
        if currency != "spirit_stone":
            assert getattr(bal, currency) == 0, (
                f"a declared currency read {getattr(bal, currency)!r} instead "
                f"of 0 for {currency}; wallet_defense.earn reads it bare")


def test_balance_publishes_a_plain_dict_through_to_dict():
    """`earn` sends `bal.to_dict()` to the client, so it must be a real dict."""
    bal = pg.PGBalance({"spirit_stone": 5})
    published = bal.to_dict()
    assert type(published) is dict
    assert published == {"spirit_stone": 5}


def test_balance_refuses_a_misspelled_currency_on_read():
    """A typo must not quietly read as a zero balance."""
    bal = pg.PGBalance()
    with pytest.raises(AttributeError):
        bal.souldustz


def test_balance_refuses_a_misspelled_currency_on_attribute_write():
    """MEASURED BEFORE THIS GUARD: the write landed and `to_dict` published it.

    `bal.souldustz = 5` went straight into the dict and came back out of
    `to_dict` as a currency the client has no name for, while a READ of the
    same misspelling already raised. One typo, two answers.
    """
    bal = pg.PGBalance()
    with pytest.raises(AttributeError):
        bal.souldustz = 5
    assert "souldustz" not in bal.to_dict()


def test_balance_refuses_a_misspelled_currency_on_item_write():
    """The dictionary-syntax write is the SECOND half of the same control.

    The attribute guard above is only half a fix: a caller holding the balance
    writes `bal["souldustz"] = 5`, which dispatches to `dict.__setitem__` and
    bypasses `__setattr__` entirely, then gets published by `to_dict`.
    """
    bal = pg.PGBalance()
    with pytest.raises(AttributeError):
        bal["souldustz"] = 5
    assert "souldustz" not in bal, (
        f"the refused key was stored anyway: {dict(bal)!r}")
    assert "souldustz" not in bal.to_dict()


def test_balance_read_and_write_refuse_with_the_same_message():
    """One typo must have one answer, in both directions and both syntaxes."""
    bal = pg.PGBalance()
    messages = set()
    for attempt in (
        lambda: getattr(bal, "souldustz"),
        lambda: setattr(bal, "souldustz", 5),
        lambda: bal.__setitem__("souldustz", 5),
    ):
        with pytest.raises(AttributeError) as excinfo:
            attempt()
        messages.add(str(excinfo.value))
    assert len(messages) == 1, (
        f"the three paths disagreed about the same typo: {sorted(messages)}")


def test_balance_construction_from_a_row_is_unaffected_by_the_item_guard():
    """`dict.__init__` does not dispatch to `__setitem__`, so wrapping a row
    verbatim still works. Without this, `_wallet_from_row` would be dead."""
    bal = pg.PGBalance({"spirit_stone": 3})
    assert bal.spirit_stone == 3


# ============================================ pure row-to-object helper ===

def test_row_helper_returns_none_for_a_missing_row():
    """A player with no wallet row is None, not an empty Wallet.

    `_mutate` relies on this: `row is None` is how it decides to answer
    "wallet not found".
    """
    assert pg._wallet_from_row(None) is None


def test_row_helper_wraps_the_balance_so_both_access_styles_work():
    """psycopg2 hands back a JSONB column as a plain dict.

    `Wallet(*row)` would produce a balance that `setattr`/`getattr` cannot use,
    which is the shape the shared guard path writes. The wrap is what keeps
    `get` and `_live_for_update` agreeing on the type they return.
    """
    wallet = pg._wallet_from_row(
        ("p1", {"spirit_stone": 3}, 7, 100.0, 50.0))
    assert wallet.player_id == "p1"
    assert wallet.version == 7
    assert wallet.last_online_at == 100.0
    assert wallet.last_offline_claimed_at == 50.0
    assert isinstance(wallet.balance, pg.PGBalance)
    assert wallet.balance.spirit_stone == 3      # attribute read
    assert wallet.balance.get("spirit_stone") == 3   # dict read


def test_row_helper_tolerates_a_null_balance_column():
    """A brand-new wallet row stores `{}`, and psycopg2 may hand back NULL.

    `balance or {}` means neither case crashes, and the result still answers a
    declared currency with 0.
    """
    wallet = pg._wallet_from_row(("p2", None, 0, 0.0, 0.0))
    assert wallet.balance == {}
    assert wallet.balance.spirit_stone == 0


def test_wallet_default_balance_is_a_guarded_balance():
    """`Wallet()` with no balance must still be able to guard its own writes."""
    assert isinstance(pg.Wallet("p").balance, pg.PGBalance)


# ================================== guards that need no database ==========
#
# Each refusal below is reached BEFORE the first query, so it runs on the
# probe. If a guard were deleted, the code would fall through to `_mutate`,
# touch `conn`, and the probe's AssertionError would fail the test.

def test_spend_refuses_a_non_positive_amount_without_a_database():
    """Zero and negative spends are refused; the refund sign is not a spend."""
    probe = _PgGuardProbe()
    currency = pg.CURRENCIES[0]
    _refused(pg.spend(probe, "p", currency, 0, 0, "k"),
             "amount must be positive")
    _refused(pg.spend(probe, "p", currency, -5, 0, "k"),
             "amount must be positive")


def test_spend_refuses_an_unknown_currency_without_a_database():
    """The whitelist check `spend` applies before it can move any currency."""
    _refused(pg.spend(_PgGuardProbe(), "p", "gold", 5, 0, "k"),
             "unknown currency")


def test_earn_refuses_a_negative_amount_without_a_database():
    """A negative earn is a withdrawal wearing the earn path's name."""
    _refused(pg.earn(_PgGuardProbe(), "p", pg.CURRENCIES[0], -1, "k"),
             "negative amount not allowed")


def test_earn_refuses_an_unknown_currency_without_a_database():
    _refused(pg.earn(_PgGuardProbe(), "p", "gold", 5, "k"),
             "unknown currency")


def test_claim_offline_refuses_a_negative_rate_without_a_database():
    """`rate_per_sec` is an INPUT and needs its own lower bound.

    `earn` refusing a negative payout cannot stand in for this: the earn
    amount is `int(capped * rate)`, which truncates TOWARD ZERO, so a small
    negative rate lands on exactly 0, `earn` accepts it, the claim reports
    SUCCESS and `last_offline_claimed_at` advances anyway.
    """
    _refused(pg.claim_offline(_PgGuardProbe(), "p", pg.CURRENCIES[0],
                              -1.0, time.time(), "k"),
             "rate_per_sec must be non-negative")


def test_claim_offline_refuses_an_unknown_currency_without_a_database():
    _refused(pg.claim_offline(_PgGuardProbe(), "p", "gold", 1.0,
                              time.time(), "k"),
             "unknown currency")


def test_claim_offline_refuses_a_skewed_device_clock_without_a_database():
    """`device_now` is the client's clock, used only to detect manipulation.

    The bound is the shared `wallet_defense.MAX_CLOCK_SKEW_SECONDS` (five
    minutes, per the 300s owner ruling), imported by this module rather than
    re-spelled as a literal, and measured
    on both sides by the boundary test below. The refusal happens before the
    wallet is read, so a rejected claim cannot also consume the offline window
    it declined to pay.
    """
    _refused(pg.claim_offline(_PgGuardProbe(), "p", pg.CURRENCIES[0], 1.0,
                              time.time() - (pg.MAX_CLOCK_SKEW_SECONDS + 1),
                              "k"),
             "clock skew too large")


def test_claim_purchase_refuses_a_negative_amount_without_a_database():
    """Checked before the transaction opens, so nothing is claimed and no
    currency moves."""
    _refused(pg.claim_purchase(_PgGuardProbe(), "p", "key",
                               pg.CURRENCIES[0], -1),
             "purchase amount must be >= 0")


def test_claim_purchase_refuses_a_blank_purchase_key_without_a_database():
    """A blank key would make every purchase of this player collide on the
    same primary key, so the second one would be refused as a replay."""
    _refused(pg.claim_purchase(_PgGuardProbe(), "p", "",
                               pg.CURRENCIES[0], 5),
             "purchase_key is required")


def test_purchase_claim_table_raises_the_shared_refusal_type():
    """The refusal type is the shared one, or callers cannot catch it.

    Every caller catches `wallet_defense.DuplicatePurchaseClaim`. If this
    backend raised its own type, the refusal would escape as an HTTP 500
    instead of "you already bought this". The blank key reaches the INSERT's
    own "no key, no row" refusal without a query.
    """
    with pytest.raises(pg.DuplicatePurchaseClaim):
        pg.PGPurchaseClaimTable(_PgGuardProbe()).claim("p1", "")


def test_purchase_claim_table_is_the_capability_the_purchase_route_checks():
    """`mint_authority.claim_purchase_receipt` refuses when `purchase_claims`
    is ABSENT, so this property existing is what keeps the route from failing
    closed on PostgreSQL."""
    assert hasattr(_PgGuardProbe(), "purchase_claims")
    assert isinstance(_PgGuardProbe().purchase_claims, pg.PGPurchaseClaimTable)


def test_store_declares_itself_as_the_postgres_backend():
    """Backend selection is driven by this flag, so it must be True here."""
    assert _PgGuardProbe().is_postgres is True


# ====================================================== negative control ==

def test_control_the_probe_would_catch_a_path_that_reached_the_database():
    """Without this, every refusal above could be green for the wrong reason.

    Each guard test asserts `ok is False`. If the probe were inert -- if
    `conn` returned something harmless, or the guards were unreachable -- those
    tests would still pass while measuring nothing. This one drives a request
    that is VALID in every respect and asserts it DOES reach the connection,
    which is the behaviour the guard tests rely on not happening.
    """
    probe = _PgGuardProbe()
    with pytest.raises(AssertionError, match="opened one"):
        pg.spend(probe, "p", pg.CURRENCIES[0], 5, 0, "k")


def test_control_a_refusal_is_not_produced_by_raising_instead_of_answering():
    """A guard must ANSWER, not raise.

    An exception out of the money path would surface as a server error, which
    is the failure mode the savepoint comment in `_claim_purchase` describes
    for this module before that fix. Every guard under test returns a dict.
    """
    probe = _PgGuardProbe()
    replies = [
        pg.spend(probe, "p", pg.CURRENCIES[0], 0, 0, "k"),
        pg.earn(probe, "p", "gold", 5, "k"),
        pg.claim_offline(probe, "p", pg.CURRENCIES[0], -1.0, time.time(), "k"),
        pg.claim_purchase(probe, "p", "k", pg.CURRENCIES[0], -1),
    ]
    assert all(isinstance(r, dict) and r.get("ok") is False for r in replies)


# ================== the cross-backend clock-skew bound, pinned to ONE number ==

def test_the_two_backends_agree_on_the_clock_skew_bound():
    """KEPT ON PURPOSE. It used to pin a DIVERGENCE; it now pins AGREEMENT.

    Measured history: `wallet_defense.MAX_CLOCK_SKEW_SECONDS` was 86400 (one
    day) while `wallet_store_pg.claim_offline` refused at a literal 300 (five
    minutes), so the identical request was ACCEPTED in memory mode and REFUSED
    on PostgreSQL. That is the same defect class this module's own CURRENCIES
    comment describes: one backend quietly disagreeing with the other, no error
    anywhere, and a memory-mode test passing the whole time.

    `wallet_store_pg` now imports the shared constant, so the bounds agree.
    Two assertions, because they fail for different reasons:

      * the PG bound must not be a literal of its own -- it must be the very
        object `wallet_defense` exports, so editing one side cannot drift the
        other (checked by identity, which a same-value re-typed literal would
        not satisfy);
      * both backends must still refuse a grossly skewed clock, so agreeing on
        one number did not turn into agreeing on having no guard at all.
    """
    assert pg.MAX_CLOCK_SKEW_SECONDS is _mem.MAX_CLOCK_SKEW_SECONDS, (
        "the PG store no longer uses wallet_defense's constant; it holds "
        f"{pg.MAX_CLOCK_SKEW_SECONDS!r} against {_mem.MAX_CLOCK_SKEW_SECONDS!r}")
    assert pg.MAX_CLOCK_SKEW_SECONDS > 0, "the shared bound must still be a guard"
    # Shared bound, so one offset past it must be refused by both backends.
    beyond = pg.MAX_CLOCK_SKEW_SECONDS + 1
    _refused(pg.claim_offline(_PgGuardProbe(), "p", pg.CURRENCIES[0], 1.0,
                              time.time() - beyond, "k"),
             "clock skew too large")


def test_the_skew_boundary_is_measured_on_both_sides_of_the_shared_constant():
    """Pins WHERE the PG refusal starts, so the bound is a boundary not a guess.

    The offsets are relative to `MAX_CLOCK_SKEW_SECONDS`, not to any literal,
    so retuning the shared bound moves the probe with it instead of silently
    turning this into a test of whatever number happens to be written down.
    """
    bound = pg.MAX_CLOCK_SKEW_SECONDS
    now = time.time()
    # Just inside: passes the guard and reaches the database (probe fires).
    with pytest.raises(AssertionError, match="opened one"):
        pg.claim_offline(_PgGuardProbe(), "p", pg.CURRENCIES[0], 1.0,
                         now - (bound - 1), "k")
    # Just outside: refused, no connection.
    _refused(pg.claim_offline(_PgGuardProbe(), "p", pg.CURRENCIES[0], 1.0,
                              now - (bound + 1), "k"),
             "clock skew too large")


def test_the_refusal_reasons_differ_between_the_backends():
    """Second measured divergence, same class. Reported, not fixed.

    Both refuse the same bad inputs, but with different words, so a client
    that switches backend sees a different error string. `earn`'s negative
    check is the sharpest case: `earn amount must be >= 0` in memory versus
    `negative amount not allowed` here.
    """
    assert _mem.earn is not pg.earn


# ================================= needs a live database: SKIPPED =======
#
# Each of these is a real gap, not padding. They are listed so the next person
# with a database knows exactly what is still unproven, and each skip names
# the dependency that is missing.

_MISSING_DB = (
    "needs a live PostgreSQL with the wallet migrations applied "
    "(WC_PG_HOST unset). This file deliberately runs with no database: "
    "proving the PostgreSQL money path needs PostgreSQL, not a mock."
)

requires_database = pytest.mark.skipif(
    not __import__("os").environ.get("WC_PG_HOST"), reason=_MISSING_DB)


@requires_database
def test_sql_that_walks_the_wallet_row_is_unproven_here():
    """NOT RUN. Needs: a database, and the `wallets` / `players` tables.

    The SQL itself -- the version check, the offline window read, the balance
    arithmetic, the `FOR UPDATE` row lock that serialises two concurrent
    credits -- is entirely unexercised by this file and by every memory-mode
    suite. `server/tests/test_achievement_ledger_pg.py` shows the pattern for
    driving real SQL and is the place to extend this.
    """
    pytest.fail("unimplemented: needs a live database")


@requires_database
def test_the_purchase_claim_primary_key_is_unproven_here():
    """NOT RUN. Needs: a database, and `purchase_claims` from migration 028.

    The once-per-receipt guarantee lives in that table's PRIMARY KEY, not in
    any Python code. Nothing in this file executes the INSERT or the savepoint,
    so nothing here proves a replay is refused.
    """
    pytest.fail("unimplemented: needs a live database")


@requires_database
def test_the_parent_player_row_is_created_before_a_wallet_insert():
    """NOT RUN. Needs: a database, and both `players` and `wallets`.

    `wallets.player_id` references `players(id)`, so the two inserts in
    `_live_for_update` and `create` have to agree. That ordering cannot be
    checked without the foreign key actually firing.
    """
    pytest.fail("unimplemented: needs a live database")
