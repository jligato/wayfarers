"""Migration: fold the player-row ``spirit_stones`` balance into the wallet.

DESIGNED, NOT APPLIED. Nothing in this module is called by any route. It is
the backfill that [[two-balances-settled]] asks for, written as a pure function
plus a SQL generator so it can be rehearsed against a copy of the data before
anyone runs it for real.

THE TWO STORES (measured, see server/tests/test_stone_backfill.py)
-------------------------------------------------------------------
1. ``players.spirit_stones`` -- jsonb dict of SEVEN grades:
   copper, silver, gold, low_grade, mid_grade, high_grade, extreme_grade.
2. ``wallets.balance`` -- jsonb dict of FOUR currencies:
   spirit_stone, fateum, destium, souldust.

They do not move together. Granted 250 through the battle pass and the player
row moved; the wallet stayed at 0. Earned 700 through the wallet and the player
row stayed put.

WHICH STORE BECOMES CANONICAL
-----------------------------
The wallet. Three measured reasons:

* Every spender that can be reached without a route change already talks to
  the wallet: ``/sect/contribute`` (main.py:553), lottery (lottery.py:440 via
  the spend_fn main.py:525 wires), stamina refill (stamina.py:196), missions
  (missions.py:428), gift series (gift_series.py:452).
* The wallet has the machinery a balance needs: a per-currency ledger, an
  idempotency key and a monotonic version column (wallet_defense.py:109-116).
  The player row has none of it, and hand-rolled idempotency is bolted onto
  sect donation receipts instead.
* The player row's remaining job after the fold is NOT a balance. main.py:1544
  reads the six non-spendable grades as a cultivation-rate bonus:
  ``(low_grade + mid_grade*2 + high_grade*5 + extreme_grade*10) / 200``,
  capped at +50%. Killing the row outright would delete a progression input.

WHAT THE BACKFILL DOES
----------------------
``low_grade`` is the ONLY grade any spender reads anywhere in the tree
(battle_pass.py:313, cosmetics.py:334, sect.py:228, sect_wall.py:128,
vip.py:627, ghost_battle.py:240, lottery.py:461). So:

* ``low_grade`` moves 1:1 into ``wallets.balance.spirit_stone``.
* ``copper``/``silver``/``gold``/``mid_grade``/``high_grade``/
  ``extreme_grade`` STAY on the player row. No spender reads them, and
  main.py:1544 does. They become a grade-holdings record, not a balance.
* The row's ``low_grade`` is zeroed only after the wallet is credited, inside
  the same transaction, so a crash between the two cannot mint or destroy.

WHAT A PLAYER WHO HOLDS NON-CANONICAL VALUE GETS
-------------------------------------------------
Nothing is confiscated. The 1:1 move is exact. The six grades that are not
migrated were never spendable, so no player loses buying power. The one real
loss is the cosmetic floor: a new player holds 100 ``copper`` and 0
``low_grade`` today, which is why a new player cannot afford a 500-stone
cosmetic. After the fold that is still true, deliberately -- see the
anti-inflation note in the design note.

REPLAY SAFETY
-------------
``backfill_player`` is idempotent by construction: it reads the row's
low_grade, credits exactly that amount, and zeroes it in the same call. Run
it twice and the second run moves 0. That is the property the tests pin.

THE MISSING WALLET ROW (the defect this SQL used to have)
---------------------------------------------------------
``backfill_player`` calls ``wallet.create`` for a player who has no wallet, so
the in-memory rehearsal pays that player. The SQL did NOT. Statement 2 is an
``UPDATE wallets ... FROM players``, which matches no row for a player who has
none -- so the old SQL paid nobody while statement 3 still zeroed their
low_grade. Every such player lost every stone they had, silently, with no
error anywhere. Measured against a real database: a player holding 400
low_grade stones finished the run holding 0 in the wallet and 0 on the row.

Statement 1 is the fix. It creates the missing wallet row FIRST, seeded with
the four currencies at zero (the shape ``CurrencyBalance`` defaults to), so
statement 2 then credits it by the same arithmetic as every other player. It
is ``WHERE NOT EXISTS``, so it is idempotent, and it runs before the credit, so
a crash between the two leaves the player holding their stones in BOTH places
-- recoverable by a re-run -- rather than in neither, which is theft.

ONE TRANSACTION, OR NEITHER (measured against a real database, 2026-10-03)
--------------------------------------------------------------------------
The docstring above used to claim the Python path was safe for the same reason
the SQL is: the credit and the debit share one commit, so a crash between them
cannot duplicate. That claim was FALSE, and it was false on BOTH backends.

``backfill_player`` opens the player store's transaction and then calls
``_credit``, which reaches the wallet. Whether that credit lands INSIDE the
store's rollback depends entirely on wiring the module does not control:

* Database mode. ``wallet_store_pg.WalletStore.conn`` returns the store's
  connection only when ``wallet.player_store`` is set. Built unwired, the
  credit runs on the wallet's own connection and COMMITS on its own.
* Memory mode. ``PlayerStore.transaction`` snapshots and restores only
  ``self.wallet_store``. Set that in one direction only (``wallet.player_store
  = store``, which is all ``PlayerStore(wallet_store=...)`` does) and the
  wallet's own dict is not part of the unit of work at all.

So a crash between the two halves committed the credit and rolled back the
debit. Measured, same seed, 500 low_grade stones, crash injected right after
the credit:

    wired   (wallet.player_store = store)        row 500, wallet None   ok
    unwired (store passed as a kwarg only)       row 500, wallet 500    DUPLICATED
    in-memory wallet, either wiring              row 500, wallet 500    DUPLICATED

Duplicated stones are the more dangerous half of this bug, not the milder one.
The next run pays them a second time, so every crash is free currency for one
player, and the row still says the stones are there for good.

The fix is to stop trusting the wiring and CHECK it, before a single stone
moves. ``_credit_shares_debit_scope`` proves the wallet's write is inside the
store's rollback, and the authoritative path now refuses loudly when it is
not. Refusing is right, and it is what this module already does for a player
missing from the store: a debit that cannot be persisted is a credit that must
not happen. Silently returning 0 would look to an operator like "nothing to
migrate", which is worse -- the run would report success over every player.

It is deliberately NOT fixed by assigning ``store.wallet_store = wallet``.
That attribute makes ``PlayerStore.transaction`` take a wallet snapshot on
EVERY transaction in the process, and the PostgreSQL wallet's snapshot is a
full ``SELECT`` over ``purchase_claims``. One money-moving backfill is not
worth a table scan on every unrelated request. The caller wires it once, at
startup, where the cost belongs.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Tuple

#: The wallet currency that becomes the one spendable balance.
CANONICAL_CURRENCY = "spirit_stone"

#: The one player-row grade that is spendable and therefore migrates.
MIGRATED_GRADE = "low_grade"

#: Grades left on the player row. No spender reads these; main.py:1544 does.
RETAINED_GRADES = (
    "copper", "silver", "gold", "mid_grade", "high_grade", "extreme_grade",
)

#: Every grade the column can hold, for shape assertions.
ALL_GRADES = (MIGRATED_GRADE,) + RETAINED_GRADES


def _as_stone_count(value: Any) -> int:
    """The whole number of stones ``value`` denotes, floored at 0, or 0.

    This is the ONE numeric-reading rule, and ``backfill_sql`` implements the
    same one in SQL, so the two halves of this module agree on every row shape.

    Why it cannot raise, measured against PostgreSQL 16. The only constraint
    the database puts on the column is ``jsonb_typeof(spirit_stones) =
    'object'`` -- nothing at all about the value inside it. So a stored
    ``low_grade`` can be an integer, a numeric string, a JSON boolean, a
    float, a list, a nested object or null, and all of them are writable
    through the ordinary ``store.transaction()`` write path. Bare ``int()``
    raises on three of those (``ValueError`` on ``'abc'``, ``TypeError`` on a
    list and on an object), which for a bulk migration means one corrupt row
    aborts the run and leaves every other player exactly where they were.
    That is not hypothetical: the SQL half had precisely that failure, and it
    took the whole batch with it. See A CAST CAN ABORT THE WHOLE MIGRATION in
    ``backfill_sql``'s docstring for the measurement.

    An unreadable grade is therefore worth 0 here. The row is left untouched
    and no run picks it up, which is the honest outcome: the value was never
    spendable, because every spender in the tree would raise on it too.

    The two shapes that are readable but not integers keep the spender
    convention rather than being second-guessed: ``True`` is 1 and ``False``
    is 0, exactly as ``battle_pass.stone_balance`` and its five siblings read
    them, and a float truncates toward zero as ``int()`` does.

    OverflowError is caught for the same reason as its two siblings in
    ``daily_streak._coerce_day`` / ``_coerce_streak``: ``int()`` raises it on
    a non-finite float, and this column is jsonb holding an unconstrained
    value, so ``{"low_grade": Infinity}`` is writable through the ordinary
    store write path. The docstring above already argues that one unreadable
    row must not abort a batch of migrating players; an uncaught
    OverflowError would do exactly that. Measured: this escaped to the caller
    before the catch existed. Note ``int()`` itself does NOT raise on a huge
    Python int -- those are unbounded -- so the infinite-float case is the
    one that reaches OverflowError here.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        # Checked BEFORE the conversion rather than by catching OverflowError.
        # Both make the function total, and the except clause below still
        # covers this shape, so this is belt and braces -- but it is also the
        # only site in the module that says out loud what it means: an
        # infinite or NaN float is not a stone count. Relying on an exception
        # that a future refactor might narrow to TypeError/ValueError would
        # silently reopen the batch-abort hole this line exists to close.
        return 0
    if isinstance(value, (int, float, str)):
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0
    return 0


def spendable_stones(player_row: Mapping[str, Any]) -> int:
    """Return the player row's spendable low-grade balance.

    Tolerates the legacy bare-int row the way every spender does
    (battle_pass.stone_balance and the five siblings): a scalar is a
    low_grade balance, not a crash. Every other shape goes through
    ``_as_stone_count``, which is total -- see there for why a migration must
    not raise on one unreadable row.
    """
    stones = player_row.get("spirit_stones")
    if isinstance(stones, dict):
        return _as_stone_count(stones.get(MIGRATED_GRADE))
    return _as_stone_count(stones)


def backfill_player(
    player_row: Dict[str, Any],
    wallet: Any,
    *,
    idempotency_prefix: str = "stone-backfill",
    player_store: Any = None,
) -> Tuple[int, Dict[str, int]]:
    """Move the player row's ``low_grade`` into the wallet. Returns (moved, balance).

    ``wallet`` is any object exposing ``.get(player_id) -> Wallet|None`` and a
    ``create(player_id)``. The credit goes through ``earn`` semantics via the
    store's own ledger, so the move lands in the same idempotent path a normal
    reward uses rather than mutating a balance in place.

    IDEMPOTENCE -- measured against a real PostgreSQL database
    -----------------------------------------------------------
    Idempotent: a second call moves 0, because the first call zeroed the grade
    it moved.

    That sentence was a CLAIM, not a fact, until it was measured against a
    database, and against one it was FALSE. Both stores hand back a DETACHED
    copy (wallet_defense.py "COPY-ON-READ", player_store.py the same), so
    zeroing ``player_row`` in place changed a local dict and nothing else. The
    database kept ``low_grade = 500`` forever, so a second call read 500 again
    and asked for 500 again. Measured: wallet 500, then 1000.

    What actually held the line was the fixed idempotency key
    ``stone-backfill:<player_id>:low_grade``, and that receipt lives only 24
    hours (wallet_store_pg._record writes ``NOW() + INTERVAL '24 hours'``).
    Aging it out the way the clock would and re-running gave 1000. So the old
    "idempotent by construction" was a 24-hour reprieve, not a guarantee.

    The fix is to stop trusting the detached dict. When a player store is
    reachable the whole move runs inside ONE transaction, on the row the store
    itself hands back, so the debit commits with the credit and the next run
    reads 0 from the database rather than from memory. The credit's own
    idempotency key stays as the second lock, not the only one.

    With no store reachable (the in-memory rehearsal, ``player_store is None``)
    the old in-place zeroing still applies: there is no database to lie to, and
    the dict IS the row.

    A REFUSED CREDIT DEBITS NOTHING (measured in memory, 2026-10-03)
    -----------------------------------------------------------------
    The faucets still write to the player row -- ``battle_pass.grant_stones``
    and its five siblings do exactly that -- so the row can acquire fresh
    ``low_grade`` stones AFTER the migration has run once for that player.
    The credit key was fixed at ``stone-backfill:<player_id>:low_grade``, so
    the wallet's replay guard answered that second, genuinely new balance with
    "idempotency key already seen" and credited 0. The code did not look:
    it zeroed the row anyway. Measured, in memory:

        run 1 -> moved 250, wallet 250, row 0
        faucet writes 250 back onto the row
        run 2 -> wallet.replay_blocked, moved 250 (a lie), wallet 250, row 0

    250 stones existed on the row and existed nowhere else. A refused credit
    must therefore leave the stones exactly where they are, and report 0
    moved. They are picked up by a later run once the receipt ages out --
    delayed, never destroyed.
    """
    player_id = player_row.get("id")
    store = player_store if player_store is not None else getattr(wallet, "player_store", None)

    if store is not None and hasattr(store, "transaction"):
        # Authoritative path: the row comes from the store, inside the store's
        # own transaction, so the zeroed grade is a committed fact rather than
        # a local edit. The wallet store's transaction joins this same scope, so
        # the credit and the debit share one commit and one rollback.
        #
        # ...only when that last sentence is actually true of the wiring. It is
        # NOT a property of this module: see ONE TRANSACTION, OR NEITHER in the
        # module docstring. Check it here, inside the open transaction, because
        # "the wallet happens to be wired this way" is exactly the kind of
        # assumption that is true in the tests and false in production.
        with store.transaction():
            if not _credit_shares_debit_scope(wallet, store):
                # Raising, not returning 0. Returning 0 would report "this
                # player has nothing to migrate" and the run would finish
                # claiming success over every player in the table, while each
                # one of them still holds stones in a row nobody debited.
                raise RuntimeError(
                    "wallet store %r is not inside the transaction opened on "
                    "player store %r, so crediting it and debiting the row would "
                    "not share one commit; a failure between them duplicates the "
                    "stones instead of rolling them back. Wire it at startup "
                    "with PlayerStore(wallet_store=wallet) before running the "
                    "backfill." % (type(wallet).__name__, type(store).__name__)
                )
            try:
                row = store[player_id]
            except KeyError:
                # The store is reachable but has no such player. Crediting here
                # would move money against a row we cannot debit, so a debit we
                # cannot persist is exactly the failure mode this fix exists to
                # remove. Refuse loudly instead.
                raise KeyError(
                    "player %r is not in the player store, so backfill_player "
                    "cannot debit the row it credits; refusing to move money "
                    "against an unpersistable debit" % (player_id,)
                )
            moved = spendable_stones(row)
            if moved <= 0:
                return 0, _balance_of(wallet, player_id)
            if not _credit(wallet, player_id, moved, idempotency_prefix):
                # Refused or replayed: the wallet did NOT receive the money, so
                # the debit below would be theft. Leave the row alone.
                return 0, _balance_of(wallet, player_id)
            _zero_row(row)
            return moved, _balance_of(wallet, player_id)

    moved = spendable_stones(player_row)
    if moved <= 0:
        return 0, _balance_of(wallet, player_id)

    if not _credit(wallet, player_id, moved, idempotency_prefix):
        # Same rule, in-memory rehearsal. The faucets still write low_grade
        # onto the player row, so a player CAN acquire fresh stones after a
        # successful migration. The fixed credit key then replays and pays 0;
        # zeroing the row would destroy those stones outright.
        return 0, _balance_of(wallet, player_id)
    _zero_row(player_row)

    return moved, _balance_of(wallet, player_id)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _credit_shares_debit_scope(wallet: Any, store: Any) -> bool:
    """Does a write to ``wallet`` roll back when ``store``'s transaction fails?

    This is the check that makes the credit-and-debit atomicity claim a fact
    rather than an assumption. Both backends can put the wallet OUTSIDE the
    store's rollback, and they get there differently:

    * PostgreSQL. ``WalletStore.conn`` hands back the player store's connection
      only when ``wallet.player_store`` is that store; otherwise the wallet
      opens its own and a failed store rollback cannot reach the credit. The
      wallet store is marked with ``is_postgres``.
    * Memory. ``PlayerStore.transaction`` snapshots and restores
      ``self.wallet_store`` -- the attribute on the STORE. Pointing only
      ``wallet.player_store`` at the store does not set it, and the in-memory
      wallet's own ``transaction`` joins the store's scope, so either direction
      of wiring is required and one is not enough.

    Deliberately not fixed by assigning ``store.wallet_store`` here: that makes
    EVERY ``PlayerStore.transaction`` in the process take a wallet snapshot,
    and the PostgreSQL wallet's snapshot is a full table SELECT. See the module
    docstring.
    """
    if getattr(wallet, "is_postgres", False):
        # Database mode: the credit rides the store's connection only when the
        # wallet is pointed at that store.
        return getattr(wallet, "player_store", None) is store

    # Memory mode: the wallet's dict is only inside the unit of work when the
    # STORE knows about it and the wallet can reach the store's scope.
    return (getattr(store, "wallet_store", None) is wallet
            and getattr(wallet, "player_store", None) is store)


def _credit(wallet: Any, player_id: str, amount: int, prefix: str) -> bool:
    """Credit ``amount`` of the canonical currency. Returns True only if it landed.

    Import is deferred so this module stays importable with no wallet
    dependency, which keeps the shape tests cheap to run.

    The import is ``wallet_dispatch``, NOT ``wallet_defense``, because the two
    stores have different ledgers. ``wallet_dispatch.earn`` picks the backend
    from the store it is handed, the same one-shot import site main.py uses.
    Calling the in-memory ``earn`` directly made this a database-mode crash:
    it reaches for ``store.recall(...)``, and the PostgreSQL store keeps its
    receipts in the ``idempotency_keys`` table instead, so it has no such
    attribute at all.

    The return value is the whole point of this function. ``earn`` REFUSES
    rather than raises: it answers ``ok=False`` for a bad amount or an unknown
    currency, and ``_replayed=True`` for a key it has already honoured. Both
    backends do this (wallet_defense.earn returns the stored receipt with
    ``_replayed`` set; wallet_store_pg._replay returns it the same way). The
    old code discarded that answer and debited the player row regardless, so
    every refusal turned a not-paid credit into a destroyed balance. Callers
    must debit only on True.
    """
    from wallet_dispatch import earn  # local import; see module docstring

    if wallet.get(player_id) is None:
        wallet.create(player_id)
    resp = earn(wallet, player_id, CANONICAL_CURRENCY, amount,
                f"{prefix}:{player_id}:{MIGRATED_GRADE}")
    if not isinstance(resp, dict) or not resp.get("ok"):
        return False
    return not resp.get("_replayed")


def _zero_row(player_row: Dict[str, Any]) -> None:
    """Zero only the migrated grade, keeping the six retained ones intact."""
    stones = player_row.get("spirit_stones")
    if isinstance(stones, dict):
        stones[MIGRATED_GRADE] = 0
    else:
        # A legacy scalar row has no grades to retain, so the whole scalar was
        # the spendable balance and it all moved.
        player_row["spirit_stones"] = 0


def _balance_of(wallet: Any, player_id: str) -> Dict[str, int]:
    """Return the wallet's four currencies as a plain dict.

    The two stores do not agree on what ``Wallet.balance`` is. The in-memory
    store uses a ``CurrencyBalance`` dataclass, so ``.to_dict()`` works; the
    PostgreSQL store keeps the JSONB column as a plain ``dict`` and has no
    method on it at all. Reading it with ``.to_dict()`` unconditionally was a
    database-mode crash on the very first call, before any money moved.
    """
    w = wallet.get(player_id)
    if w is None:
        return {}
    balance = w.balance
    to_dict = getattr(balance, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return dict(balance)


# ---------------------------------------------------------------------------
# SQL generation -- the rehearsal artifact
# ---------------------------------------------------------------------------

#: The largest stone count a ``bigint`` column can hold, as text. Postgres's
#: integer range is -9223372036854775808 .. 9223372036854775807.
_SQL_BIGINT_MIN = "-9223372036854775808"
_SQL_BIGINT_MAX = "9223372036854775807"

#: A plain integer of at most 19 digits. The digit COUNT is what matters here,
#: not the value: 19 digits is the widest any in-range bigint can be, so
#: anything longer is already out of range before a single digit is compared.
#: Without this bound the pattern below matched a 40-digit string and handed it
#: straight to ::bigint, which raised.
_SQL_INT_PATTERN = r"^-?[0-9]{1,19}$"

#: A decimal whose integer part is at most 19 digits, same reasoning.
_SQL_DECIMAL_PATTERN = r"^-?[0-9]{1,19}(\.[0-9]+)?$"


def _sql_in_bigint_range(expr: str) -> str:
    """Render SQL that is TRUE only when ``expr``'s digits fit a ``bigint``.

    A 19-digit string is not automatically in range -- 9999999999999999999 is
    19 digits and still overflows -- so the digit-count bound above is not
    sufficient on its own. This compares the value against the two bounds
    BEFORE any cast runs, so ``::bigint`` is only ever handed something it can
    take.

    Both operands are ``numeric``, which is arbitrary precision, so this
    comparison cannot itself overflow. It runs only on strings that already
    matched one of the two patterns above, so the cast to ``numeric`` is safe
    too.
    """
    return ("trunc(({e})::numeric) BETWEEN {lo} AND {hi}").format(
        e=expr, lo=_SQL_BIGINT_MIN, hi=_SQL_BIGINT_MAX)


def _sql_stone_count(expr: str) -> str:
    """Render SQL that reads ``expr`` (a jsonb value) as a stone count.

    This is ``_as_stone_count`` in SQL, branch for branch, so the generated
    migration and ``backfill_player`` accept exactly the same row shapes. The
    reason it exists at all is A CAST CAN ABORT THE WHOLE MIGRATION below: a
    bare ``(x)::bigint`` raises on a JSON boolean, a non-numeric string, a
    float, a list or an object, and because all the statements share one
    transaction, that single raise rolls back every other player's migration.

    ONE DELIBERATE DIVERGENCE FROM ``_as_stone_count``
    -------------------------------------------------
    "Branch for branch" holds for every shape both halves can represent. It
    does not hold for an integer above the bigint maximum, because Python
    integers are unbounded and ``bigint`` is not: ``_as_stone_count`` returns
    9223372036854775808 exactly, while this function reads it as 0.

    That asymmetry is left standing deliberately. This module only ever writes
    through SQL, and the credit lands in ``wallets.balance``, which is jsonb
    and could hold the value fine -- so clamping the Python side down to 64
    bits would discard a real balance from every in-process rehearsal while
    the run that actually moves money skips the same row anyway. The SQL half
    is the total one, and totality is what the batch depends on.

    OUT OF RANGE IS THE SAME ABORT, AND IT SURVIVED THE SHAPE CHECK (measured
    against PostgreSQL 16, 2026-10-05)
    ------------------------------------------------------------------
    Capping the digit count was NOT enough, and the first fix shipped here was
    exactly that mistake. Every branch below is guarded by a regex, so it was
    reasonable to conclude a cast could never see a value it could not take --
    and yet 5 of 19 stored shapes still aborted the whole batch, each with
    ``NumericValueOutOfRange``, taking every healthy player in the run down
    with it:

        9223372036854775808               (bigint max + 1)
        -9223372036854775809              (bigint min - 1)
        9999999999999999999999999999999999999999   (40 digits)
        9223372036854775808.7             (decimal, int part over range)
        '9223372036854775808'             (the same, stored as a string)

    All five are digit strings the old patterns happily matched. ``::bigint``
    takes 64 signed bits and these do not fit in 64 signed bits, so the cast
    raised -- and because every statement shares one transaction, the healthy
    row sitting beside the hostile one kept all 500 of its stones.

    So the shape check and the range check are separate obligations, and this
    function now does both: the regexes decide the value's SHAPE, and
    ``_sql_in_bigint_range`` decides whether it FITS.

    A value out of range reads as 0, the same as any other unreadable grade.
    It is left untouched in the column for a human to look at, and every
    ``> 0`` filter skips it, so the run finishes over everybody else.
    """
    int_branch = ("WHEN {e} ~ '{i}' AND {rng} THEN ({e})::bigint ").format(
        e=expr, i=_SQL_INT_PATTERN, rng=_sql_in_bigint_range(expr))
    dec_branch = (
        "WHEN {e} ~ '{d}' AND {rng} THEN trunc(({e})::numeric)::bigint "
    ).format(e=expr, d=_SQL_DECIMAL_PATTERN, rng=_sql_in_bigint_range(expr))
    return (
        "CASE WHEN {e} = 'true' THEN 1 "
        "WHEN {e} = 'false' THEN 0 "
        "{i}"
        "{d}"
        "ELSE 0 END"
    ).format(e=expr, i=int_branch, d=dec_branch)


def _grades_as(table: str, alias: str) -> str:
    """Render a FROM source whose ``low_grade`` is always safe to cast.

    One sanitising subquery, exposed under ``alias``, so the statements keep
    reading ``<alias>.spirit_stones->>'low_grade'`` and keep comparing it to 0
    -- the arithmetic is unchanged and now just cannot raise. ``to_jsonb``
    puts the result back as a JSON number, so the surviving ``::bigint`` at
    each use site is safe by construction.

    A row whose grade cannot be read becomes 0 here, which means every
    statement's ``> 0`` filter leaves it alone. Nothing is overwritten: the
    unreadable value stays in the column for a human to look at, rather than
    being silently normalised away by this module.

    The body references the bare column, not ``<alias>`` -- inside the
    subquery the alias does not exist yet, so qualifying those references would
    not compile.
    """
    return (
        "(SELECT id, jsonb_set(COALESCE(spirit_stones, '{{}}'::jsonb), "
        "'{{{g}}}', to_jsonb({count}), true) AS spirit_stones "
        "FROM {t} WHERE jsonb_typeof(spirit_stones) = 'object') {a}"
    ).format(
        g=MIGRATED_GRADE,
        count=_sql_stone_count("spirit_stones->>'%s'" % MIGRATED_GRADE),
        t=table,
        a=alias,
    )


def backfill_sql(table: str = "players", wallet_table: str = "wallets") -> str:
    """Return the one-shot SQL that performs the same move in the database.

    Four statements, all inside ONE transaction:

    0. READ every grade through a sanitising subquery, so no stored value can
       raise out of a cast and take the whole batch with it.
    1. CREATE the wallet row for any player who has stones on the player row
       but no wallet row at all. Without this the credit below matches
       nothing for them and their stones are destroyed by statement 3.
    2. CREDIT the wallet from the row's low_grade.
    3. DEBIT the player row.

    Order matters for crash forensics only: if you die between any two of them
    the player holds their stones in BOTH places, which a re-run resolves,
    rather than in neither, which is theft.

    A CAST CAN ABORT THE WHOLE MIGRATION (measured, 2026-10-03)
    -----------------------------------------------------------
    Every grade used to be read as ``(p.spirit_stones->>'low_grade')::bigint``
    behind a ``COALESCE(..., 0)``. The COALESCE looks like it makes the cast
    safe and it does not: it only replaces a NULL, and a non-NULL value that
    is not an integer raises before COALESCE is ever consulted. PostgreSQL
    16, same three rows, one transaction:

        good row low_grade 500     migrated
        good row low_grade 250     migrated
        one  row low_grade 'abc'   raises InvalidTextRepresentation

    and the rollback took all three, so the batch reported failure and left
    every player holding stones in both places. Measured blast radius with a
    single unreadable row among healthy ones: 0 of 3 players migrated. The
    same cast on the wallet side aborts it too, so a wallet whose
    ``spirit_stone`` is not an integer was enough on its own.

    Which shapes actually reach it: the database constrains the COLUMN to
    ``jsonb_typeof(spirit_stones) = 'object'`` and says nothing about the
    value inside it, so ``true``, ``false``, ``'abc'``, ``500.7``, ``[1]`` and
    ``{"a": 1}`` are all writable through the ordinary store write path.
    Measured, one shape alone in an otherwise empty table, and every one of
    them aborted the run. ``500``, ``'700'``, ``0``, a missing key and JSON
    null were fine.

    The fix is to read through ``_sql_stone_count``, which decides the value's
    shape BEFORE casting and never hands a cast something it cannot take. It
    is ``_as_stone_count`` translated branch for branch, so the rehearsal and
    the Python path now accept and skip the same rows. A row that cannot be
    read reads as 0, every ``> 0`` filter leaves it alone, and the run finishes
    over everybody else.
    """
    grades = _grades_as(table, "p")
    row_grade = "{a}.spirit_stones->>'{g}'".format(a="p", g=MIGRATED_GRADE)
    wallet_stone = "{w}.balance->>'{c}'".format(w="w", c=CANONICAL_CURRENCY)
    return f"""
-- Rehearsal copy only. Take a backup, then run ALL STATEMENTS inside ONE
-- explicit transaction.
--
-- The player's own grade is read through a sanitising subquery aliased `p`, so
-- every cast below sees a whole number or nothing at all. See the module's
-- backfill_sql docstring: without it, one unreadable stored value raised out
-- of a cast and rolled back every other player's migration in the batch.

-- Statement 1 -- BACKFILL the missing wallet row. A player can hold
-- low_grade stones and have no wallets row at all. The credit in statement 2
-- is an UPDATE ... FROM, which matches no row for such a player, so without
-- this statement they are paid nothing AND statement 3 zeroes their stones:
-- silent, total loss. WHERE NOT EXISTS keeps it idempotent. The balance is
-- seeded with the four currencies at zero, the shape CurrencyBalance
-- defaults to, so the row is valid the moment it is created.
INSERT INTO {wallet_table} (player_id, balance, version)
SELECT p.id,
       '{{"spirit_stone": 0, "fateum": 0, "destium": 0, "souldust": 0}}'::jsonb,
       0
  FROM {grades}
 WHERE COALESCE(({row_grade})::bigint, 0) > 0
   AND NOT EXISTS (SELECT 1 FROM {wallet_table} w WHERE w.player_id = p.id);

-- Statement 2 -- CREDIT the wallet from the row's low_grade.
-- The wallet's existing balance is read through the same shape check, because
-- a wallet whose spirit_stone is not an integer aborted the whole run just as
-- readily as a player row whose low_grade was not.
UPDATE {wallet_table} w
   SET balance = jsonb_set(
           COALESCE(w.balance, '{{}}'::jsonb),
           '{{{CANONICAL_CURRENCY}}}',
           to_jsonb({_sql_stone_count(wallet_stone)}
                    + COALESCE(({row_grade})::bigint, 0)),
           true),
       version = COALESCE(w.version, 0) + 1
  FROM {grades}
 WHERE w.player_id = p.id
   AND COALESCE(({row_grade})::bigint, 0) > 0;

-- Statement 3 -- DEBIT the player row, keeping the six retained grades.
-- The grade is read from the sanitising subquery joined on p.id, NOT from p
-- itself: reading it off the target row would put the raw cast back and one
-- unreadable value would abort the batch again, this time after the credit.
UPDATE {table} p
   SET spirit_stones = jsonb_set(
           COALESCE(p.spirit_stones, '{{}}'::jsonb),
           '{{{MIGRATED_GRADE}}}',
           '0'::jsonb,
           true)
  FROM {_grades_as(table, "s")}
 WHERE p.id = s.id
   AND COALESCE((s.spirit_stones->>'{MIGRATED_GRADE}')::bigint, 0) > 0;
""".strip()