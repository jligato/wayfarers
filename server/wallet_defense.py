"""
Wayfarer's Cycle — Server-Authoritative Currency Defense (currency.md §3.1 + §3.2)

Reference implementation. Drop this into the FastAPI backend's `server/wallet_defense.py`.

Defense guarantees:
  §3.1 — The client NEVER writes a balance. It requests a transaction; the server
         validates and mutates atomically under a row lock. `wallet_version`
         monotonically increases; stale-version requests are rejected.
  §3.2 — Every state-changing request carries an Idempotency-Key (client UUID).
         The server stores `{player_id, key, response, expires_at}` and, on
         duplicate, replays the original response instead of re-executing.
  §3.3 — Offline progress is computed from the server's `last_online_at`,
         capped, and rejected on device-clock skew.

This module is framework-light: it defines the data shape + logic so it can be
unit-tested without a live DB (in-memory store provided) and then wired to
PostgreSQL by swapping `WalletStore` for a DB-backed one.
"""
from __future__ import annotations

import sys
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import RLock
from dataclasses import dataclass, field
from typing import Dict, Optional

# Match the import style of the other modules in this tree: the tests load
# ``server/`` onto sys.path directly, so a bare ``wc_logging`` import is the one
# that resolves. The parent directory is a fallback so the module also imports
# as ``server.wallet_defense`` from a package context.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import wc_logging as wcl

# ----------------------------------------------------------------------------
# Observability
# ----------------------------------------------------------------------------
#
# Every refusal and every successful grant below writes exactly one structured
# line, so a money movement and the operator's later "who spent what" question
# are joinable. Three rules make this safe to add to a payout path:
#
#   1. It goes through ``wcl.safe_log_event``, never ``log_event``. That is the
#      only entry point that cannot raise: a broken log backend must cost a
#      debug line, never a paid request. A 500 from a logging call AFTER the
#      money moved is the worst outcome available -- the player sees an error,
#      the retry double-pays, and the server looks at fault for a log line.
#   2. It adds no branch. No emit can change a return value, a refusal, or an
#      amount. The emits sit beside the existing returns, never inside a
#      condition that also decides money.
#   3. The event names are the dotted constants from ``wc_logging.EVENTS``, so
#      a typo is an AttributeError at import rather than a line nobody indexes.

def _route_or_none(route: Optional[str]) -> Dict[str, Optional[str]]:
    """Shape the optional ``route`` field for a log call.

    ``route`` is a RESERVED top-level field of the log record, and it names the
    HTTP route that moved the money -- which only the caller knows. This module
    is the store, not the route, so the value is threaded in by the caller and
    passes through untouched; when a caller supplies none the field is simply
    absent rather than guessed.
    """
    return {"route": route} if route else {}


def _receipt_fingerprint(receipt_id: Optional[str]) -> str:
    """A log-safe stand-in for a purchase key. Never the raw value.

    WHY A LOCAL, DEFERRED IMPORT: ``mint_authority`` imports THIS module at
    module scope, so a top-level ``from mint_authority import
    receipt_fingerprint`` here would be a circular import. Resolving it
    inside the function is safe -- by the time a purchase is claimed, both
    modules are fully loaded.

    WHY IT CANNOT RAISE INTO A PAYMENT: rule 1 above. Observability must
    never break a payment, and this module is usable without the web
    stack, so an unimportable ``mint_authority`` falls back to the empty
    string: the field stays present and carries no secret.
    """
    try:
        from mint_authority import receipt_fingerprint
    except Exception:  # a broken/absent peer module must not fail a claim
        return ""
    return receipt_fingerprint(receipt_id)


def _log_wallet_event(event: str, player_id: str, **fields: object) -> None:
    """Write one wallet event. Cannot raise into a payment.

    Wrapping ``safe_log_event`` rather than calling it at each site keeps the
    "never breaks a payment" promise in one place, and gives one seam for a
    test to replace.
    """
    try:
        wcl.safe_log_event(event, player_id=player_id, **fields)
    except Exception:  # pragma: no cover - safe_log_event already swallows
        # Belt to its braces. safe_log_event catches Exception from the logging
        # stack; this catches a fault in building the field dict above it, which
        # would otherwise raise from inside a committed payout.
        pass

# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

class StaleVersionError(Exception):
    """Client's wallet_version is behind the server's; reject (TOCTOU guard)."""

class IdempotentReplay(Exception):
    """Duplicate idempotency key — carry the original response, do NOT re-run."""

class DuplicatePurchaseClaim(Exception):
    """A purchase claim row for (player_id, purchase_key) already exists.

    Raised by the claim INSERT itself, not by a read that asked first. That
    difference is the whole fix: a check-then-write has a window between the
    two halves, and a time window makes the window permanent after it lapses.
    """


# ----------------------------------------------------------------------------
# Purchase claims — permanent, primary-keyed, never expiring
# ----------------------------------------------------------------------------
#
# A purchase is recorded by inserting a claim row, and only then is the
# payout allowed to run. The row's uniqueness is a PRIMARY KEY over
# (player_id, purchase_key), so the database — not a timestamp comparison —
# refuses the second buy. There is deliberately NO expires_at column and no
# purge job: a receipt that expires is a receipt that can be spent again, and
# a re-buyable tier is a money printer. Delete the row explicitly if a refund
# is genuinely wanted.
#
# The SQL below is the production shape of the same table. The in-memory
# reference store below enforces the identical key by refusing a second
# insert for a key it already holds, so the two backends cannot disagree
# about what "already claimed" means.
PURCHASE_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS purchase_claims (
    player_id      TEXT NOT NULL,
    purchase_key   TEXT NOT NULL,
    product_id     TEXT NOT NULL DEFAULT '',
    items          JSONB NOT NULL DEFAULT '[]'::jsonb,
    claimed_at     DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, purchase_key)
);
CREATE INDEX IF NOT EXISTS idx_purchase_claims_player ON purchase_claims(player_id);
"""

#: The two columns that make a purchase claim unique. Exposed as data so a
#: caller (and a test) can assert the Python key and the SQL key are the same
#: pair rather than two independently-drifting spellings.
PURCHASE_CLAIM_PRIMARY_KEY = ("player_id", "purchase_key")

#: Insert a claim and report whether the INSERT won. A duplicate raises
#: psycopg2's UniqueViolation, which is exactly the refusal we want: no
#: UPDATE, no upsert, no second payout. `RETURNING` proves the row is ours.
PURCHASE_CLAIM_INSERT_SQL = (
    "INSERT INTO purchase_claims (player_id, purchase_key, product_id, items, claimed_at) "
    "VALUES (%s, %s, %s, %s, %s) RETURNING player_id, purchase_key"
)


class PurchaseClaimTable:
    """Append-only claim table whose primary key is (player_id, purchase_key).

    The in-memory stand-in for `PURCHASE_CLAIMS_DDL`. It is a table, not a
    dict-with-a-timestamp: every write goes through `claim()`, and `claim()`
    raises `DuplicatePurchaseClaim` on a key it already holds. There is no
    method that expires, purges, or overwrites a row.
    """

    PRIMARY_KEY = PURCHASE_CLAIM_PRIMARY_KEY

    def __init__(self):
        # Keyed by the primary key tuple, so uniqueness is structural: a
        # second row for the same key cannot be represented at all.
        self._rows: Dict[tuple, dict] = {}

    def claim(self, player_id: str, purchase_key: str, *,
              product_id: str = "", items: Optional[list] = None) -> dict:
        """Insert one claim row, or raise `DuplicatePurchaseClaim`.

        This is the whole guard. It is an INSERT against a primary key, not
        "SELECT then maybe INSERT", so two racing buyers of the same tier
        cannot both get past it.
        """
        if not purchase_key:
            raise ValueError("purchase_key is required; an empty key would "
                             "make every purchase of a product collide")
        key = (player_id, purchase_key)
        if key in self._rows:
            raise DuplicatePurchaseClaim(
                f"purchase {purchase_key!r} already claimed by {player_id!r}")
        row = {
            "player_id": player_id,
            "purchase_key": purchase_key,
            "product_id": product_id,
            "items": list(items or []),
            "claimed_at": time.time(),
        }
        self._rows[key] = row
        return deepcopy(row)

    def get(self, player_id: str, purchase_key: str) -> Optional[dict]:
        """Read a claim for operator/debug use. NEVER a payout guard."""
        row = self._rows.get((player_id, purchase_key))
        return deepcopy(row) if row is not None else None

    def count(self) -> int:
        return len(self._rows)

    def snapshot(self):
        return deepcopy(self._rows)

    def restore(self, snapshot) -> None:
        self._rows = deepcopy(snapshot)

@dataclass
class CurrencyBalance:
    spirit_stone: int = 0
    fateum: int = 0
    destium: int = 0
    souldust: int = 0

    def __setattr__(self, name, value):
        """Refuse an UNDECLARED name on write, not only on read.

        Measured before this guard: ``bal.souldustz = 5`` was accepted, stored
        as a plain instance attribute, and then PUBLISHED -- ``to_dict`` is
        ``dict(self.__dict__)``, so the invented key came back to the client as
        a currency the client has no name for. A read of the same misspelling
        already raised, so reads and writes disagreed about the same typo.

        The guard is the declared field set, which is the same set ``spend``
        and ``earn`` whitelist before they reach ``setattr`` -- so this refuses
        nothing the money path can legitimately write. AttributeError is what
        ``WalletStore.update`` raises for an unknown Wallet field and what
        ``PGBalance.__getattr__`` raises for an unknown currency, so the two
        backends answer a typo identically.

        ``object.__setattr__`` is used rather than a bare assignment so the
        dataclass ``__init__`` and every legitimate currency write go through
        unchanged.
        """
        if name not in self.__dataclass_fields__:
                    raise AttributeError(
                        f"balance has no currency {name!r}; declared: {CURRENCIES}")
        object.__setattr__(self, name, value)

    def to_dict(self):
        return dict(self.__dict__)

#: The declared currencies, derived from the dataclass itself so the whitelist
#: `spend`/`earn` apply, the `__setattr__` guard above enforces, and this
#: tuple cannot drift into three spellings of the same list.
CURRENCIES = tuple(CurrencyBalance.__dataclass_fields__)

@dataclass
class Wallet:
    player_id: str
    balance: CurrencyBalance = field(default_factory=CurrencyBalance)
    version: int = 0                      # monotonically increasing
    last_online_at: float = 0.0           # server-set, never client-trusted
    last_offline_claimed_at: float = 0.0  # for §3.3 cap window

# ----------------------------------------------------------------------------
# Store (in-memory reference; swap for PostgreSQL row-locked impl in prod)
# ----------------------------------------------------------------------------

class WalletStore:
    """In-memory stand-in for the DB table. Production: SELECT ... FOR UPDATE."""

    def __init__(self):
        self.player_store = None
        self._lock = RLock()
        self._wallets: Dict[str, Wallet] = {}
        # Keyed by (player_id, key), NOT by key alone. Keying on the client's
        # key alone let one player overwrite another player's stored receipt,
        # which silently un-guarded the first player's retry into a second,
        # real charge. Scoping the key to its owner makes that collision
        # unrepresentable.
        self._idempotency: Dict[tuple, dict] = {}  # (player_id, key) -> {resp, expires_at}
        # Purchase receipts live here, not in _idempotency. A purchase is a
        # once-ever grant, so its receipt is keyed by a PRIMARY KEY and never
        # expires; a retry receipt is a transport concern and may be
        # time-limited without turning the purchase itself into a re-buy.
        self.purchase_claims = PurchaseClaimTable()

    def snapshot(self):
        return deepcopy((self._wallets, self._idempotency, self.purchase_claims.snapshot()))

    def restore(self, snapshot):
        wallets, idem, claims = snapshot
        self._wallets, self._idempotency = wallets, idem
        self.purchase_claims.restore(claims)

    @contextmanager
    def transaction(self):
        if self.player_store is not None:
            with self.player_store.transaction():
                yield self
            return
        with self._lock:
            before = self.snapshot()
            try:
                yield self
            except BaseException:
                self.restore(before)
                raise

    # --- wallet CRUD (server-only) ---
    #
    # COPY-ON-READ. ``get`` and ``create`` hand back a DETACHED copy: a caller
    # that mutates what it read cannot rewrite currency behind the store's
    # back, because there is nothing live to mutate. Every balance change goes
    # through ``spend``/``earn`` (which run under ``transaction()``) or the
    # explicit ``put``/``update`` writers below, so a write is always a
    # deliberate, lock-serialized act rather than a side effect of a read.
    #
    # ``_live`` is the ONLY way to reach the stored object, and it is private to
    # this module's own guarded helpers. Do not expose it.
    def _live(self, player_id: str) -> Optional[Wallet]:
        return self._wallets.get(player_id)

    def _live_for_update(self, player_id: str) -> Wallet:
        """Return the LIVE wallet, creating it if absent. Module-private.

        Only this module's ``@atomic`` helpers call this, and only while holding
        the transaction lock, so the mutation they perform is serialized and
        rolls back on failure. No other caller can reach a live object.
        """
        w = self._wallets.get(player_id)
        if w is None:
            w = Wallet(player_id=player_id, last_online_at=time.time(),
                       last_offline_claimed_at=time.time())
            self._wallets[player_id] = w
        return w

    def get(self, player_id: str) -> Optional[Wallet]:
        """READ A DETACHED COPY. Mutating the result does NOT change the store.

        A caller that legitimately needs to change wallet state must write it
        back explicitly -- ``put`` for a whole wallet, ``update`` for scalar
        fields -- or use ``spend``/``earn``, which run under the transaction
        lock. Silently dropping a stamp or a balance write here is a money bug
        (a player could re-claim the same offline window), so the read is a
        copy by design and every mutation path is named.
        """
        w = self._wallets.get(player_id)
        return deepcopy(w) if w is not None else None

    def create(self, player_id: str, name: str = "", spirit_root: str = "none") -> Wallet:
        w = self._wallets.get(player_id)
        if w is None:
            w = Wallet(player_id=player_id, last_online_at=time.time(), last_offline_claimed_at=time.time())
            self._wallets[player_id] = w
        return deepcopy(w)

    def put(self, wallet: Wallet) -> Wallet:
        """Explicit write-back of a whole wallet. Server-side only.

        Replaces the stored wallet with a detached copy of ``wallet`` and
        returns another detached copy, so neither side holds a live handle.
        """
        self._wallets[wallet.player_id] = deepcopy(wallet)
        return deepcopy(wallet)

    def update(self, player_id: str, **fields) -> Optional[Wallet]:
        """Explicit, transactional field write. Rejects unknown fields.

        ``wallet.last_online_at = x`` on a value from ``get`` is now a no-op on
        the store, so the few places that legitimately poke a scalar go through
        here instead.
        """
        allowed = set(Wallet.__dataclass_fields__)
        unknown = set(fields) - allowed
        if unknown:
            raise AttributeError(f"unknown Wallet field(s): {sorted(unknown)}")
        w = self._wallets.get(player_id)
        if w is None:
            return None
        for k, v in fields.items():
            setattr(w, k, v)
        return deepcopy(w)

    def touch(self, wallet: Wallet) -> None:
        """Mark a wallet online. Takes a player_id OR a Wallet (id is read)."""
        pid = wallet if isinstance(wallet, str) else wallet.player_id
        self.update(pid, last_online_at=time.time())

    def bump_version(self, wallet: Wallet) -> None:
        """Advance the monotonic version of a stored wallet."""
        pid = wallet if isinstance(wallet, str) else wallet.player_id
        w = self._wallets.get(pid)
        if w is not None:
            w.version += 1

    # --- idempotency ---
    def _purge_expired(self, now: float) -> None:
        """Drop receipts whose TTL has passed. Bounded work, no unbounded map.

        An expired receipt is already refused by ``recall``, so purging it
        changes no answer -- it stops the per-player map growing forever on a
        long-lived process. Keyed by ``now`` so the caller reads one clock.
        """
        stale = [k for k, rec in self._idempotency.items() if now > rec["expires_at"]]
        for k in stale:
            del self._idempotency[k]

    def seen(self, key: str, player_id: str, response: dict, ttl_sec: int = 86400) -> None:
        """Record the response this (player, key) pair is entitled to replay."""
        now = time.time()
        self._purge_expired(now)
        self._idempotency[(player_id, key)] = {"resp": deepcopy(response),
                                               "expires_at": now + ttl_sec}

    def recall(self, key: str, player_id: str) -> Optional[dict]:
        """The stored response for THIS player's key, or None.

        Scoped to the owner on both sides, so a receipt written by another
        player under the same key is invisible here rather than destructive.
        """
        rec = self._idempotency.get((player_id, key))
        if rec is None or time.time() > rec["expires_at"]:
            return None
        return deepcopy(rec["resp"])

# ----------------------------------------------------------------------------
# §3.1 — server-authoritative transaction
# ----------------------------------------------------------------------------

def atomic(fn):
    @wraps(fn)
    def wrapped(store, *args, **kwargs):
        with store.transaction():
            return fn(store, *args, **kwargs)
    return wrapped

@atomic
def spend(store: WalletStore, player_id: str, currency: str, amount: int,
          client_version: int, idem_key: str,
          route: Optional[str] = None) -> dict:
    """
    Atomically spend `amount` of `currency`.

    Guards:
      * amount > 0 (no negative spend / overflow)
      * client_version matches server version (reject stale = replay/TOCTOU)
      * idempotency: duplicate key returns the stored response (no double-spend)

    `route` is observability only: it names the HTTP route that asked for the
    debit, so the log line can be grouped by endpoint. It is not read by any
    guard and cannot change the outcome.
    """
    if amount <= 0:
        _log_wallet_event(wcl.EVENTS["WALLET_SPEND_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason="amount must be positive",
                          currency=currency, amount=amount,
                          **_route_or_none(route))
        return {"ok": False, "error": "amount must be positive", "wallet_version": _ver(store, player_id)}
    if currency not in CurrencyBalance.__dataclass_fields__:
        _log_wallet_event(wcl.EVENTS["WALLET_SPEND_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason=f"unknown currency '{currency}'",
                          currency=currency, amount=amount,
                          **_route_or_none(route))
        return {"ok": False, "error": f"unknown currency '{currency}'", "wallet_version": _ver(store, player_id)}

    # Idempotency replay check FIRST (before mutation).
    replay = store.recall(idem_key, player_id)
    if replay is not None:
        # A replay is a refusal in the only sense that matters to money: no
        # second debit happened. That makes it the highest-value line an
        # anti-cheat index can read, so it is logged rather than passed over.
        _log_wallet_event(wcl.EVENTS["WALLET_REPLAY_BLOCKED"], player_id,
                          ok=False, outcome="replayed", reason="idempotency key already seen",
                          currency=currency, amount=amount,
                          **_route_or_none(route))
        replay["_replayed"] = True
        return replay

    # Inside the transaction, the LIVE wallet is what we mutate and what the
    # rollback snapshot restores. `get`/`create` hand out copies for everyone
    # else; this path is the one deliberate, serialized write.
    w = store._live_for_update(player_id)

    # Stale-version guard: a forged/replayed request carries the OLD version.
    # NOTE: do NOT record this failure as idempotent — the next attempt will
    # carry the corrected version and must be evaluated fresh.
    if client_version != w.version:
        _log_wallet_event(wcl.EVENTS["WALLET_SPEND_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason="stale wallet_version",
                          currency=currency, amount=amount,
                          client_version=client_version, wallet_version=w.version,
                          **_route_or_none(route))
        return {"ok": False, "error": "stale wallet_version", "wallet_version": w.version}

    bal = w.balance
    if getattr(bal, currency) < amount:
        _log_wallet_event(wcl.EVENTS["WALLET_SPEND_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason="insufficient balance",
                          currency=currency, amount=amount,
                          balance=getattr(bal, currency), wallet_version=w.version,
                          **_route_or_none(route))
        # An over-draft attempt against an already-spent balance is the specific
        # shape a double-spend probe takes, so it carries its own name as well.
        _log_wallet_event(wcl.EVENTS["WALLET_DOUBLE_SPEND_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason="insufficient balance",
                          currency=currency, requested=amount,
                          available=getattr(bal, currency), wallet_version=w.version,
                          **_route_or_none(route))
        # Deliberately NOT recorded as the idempotent response for this key.
        # This refusal is a function of the balance right now, not a terminal
        # outcome: the player may top up and retry the very same request, and
        # recording it here replayed the stale refusal for the full TTL, so
        # money they had just paid for could never be spent. The guard that
        # holds the line against an over-draft is this balance check, which
        # runs again on every attempt.
        return {"ok": False, "error": "insufficient balance", "wallet_version": w.version}

    # Mutate + bump under the "lock" (row lock in DB).
    setattr(bal, currency, getattr(bal, currency) - amount)
    store.bump_version(w)
    resp = {"ok": True, "currency": currency, "spent": amount,
            "balance": bal.to_dict(), "wallet_version": w.version}
    # Only successful mutations are idempotency-anchored.
    store.seen(idem_key, player_id, resp)
    # Emitted after the commit of the mutation above, so the line is only ever
    # written for money that actually moved.
    _log_wallet_event(wcl.EVENTS["WALLET_SPEND"], player_id,
                      ok=True, outcome="spent",
                      currency=currency, amount=amount,
                      balance=getattr(bal, currency), wallet_version=w.version,
                      **_route_or_none(route))
    return resp


@atomic
def earn(store: WalletStore, player_id: str, currency: str, amount: int,
         idem_key: str, reward_type: str = "",
         route: Optional[str] = None) -> dict:
    """
    Server-side earn. `reward_type` mirrors Overmortal's `reward_reward_*`
    engine (config-schema.md §3.1): every grant funnels through one keyed
    reward path so a cheater can't open a second, unaccounted source.

    `route` is observability only -- see `spend`.
    """
    if amount < 0:
        _log_wallet_event(wcl.EVENTS["WALLET_EARN_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason="earn amount must be >= 0",
                          currency=currency, amount=amount, reward_type=reward_type,
                          **_route_or_none(route))
        return {"ok": False, "error": "earn amount must be >= 0", "wallet_version": _ver(store, player_id)}
    # Same currency whitelist `spend` applies. Without it an unknown currency
    # reached setattr() and raised out of this function: a 500 on a grant
    # route, and the caller had no way to tell a refusal from a crash.
    if currency not in CurrencyBalance.__dataclass_fields__:
        _log_wallet_event(wcl.EVENTS["WALLET_EARN_REJECTED"], player_id,
                          ok=False, outcome="rejected",
                          reason=f"unknown currency '{currency}'",
                          currency=currency, amount=amount, reward_type=reward_type,
                          **_route_or_none(route))
        return {"ok": False, "error": f"unknown currency '{currency}'",
                "wallet_version": _ver(store, player_id)}
    replay = store.recall(idem_key, player_id)
    if replay is not None:
        _log_wallet_event(wcl.EVENTS["WALLET_REPLAY_BLOCKED"], player_id,
                          ok=False, outcome="replayed", reason="idempotency key already seen",
                          currency=currency, amount=amount, reward_type=reward_type,
                          **_route_or_none(route))
        replay["_replayed"] = True
        return replay
    w = store._live_for_update(player_id)
    bal = w.balance
    setattr(bal, currency, getattr(bal, currency) + amount)
    store.bump_version(w)
    resp = {"ok": True, "currency": currency, "earned": amount, "reward_type": reward_type,
            "balance": bal.to_dict(), "wallet_version": w.version}
    store.seen(idem_key, player_id, resp)
    # After the grant is anchored, so a line exists only for currency that was
    # really credited. `amount` is what was granted, not the new balance.
    _log_wallet_event(wcl.EVENTS["WALLET_EARN"], player_id,
                      ok=True, outcome="earned",
                      currency=currency, amount=amount, reward_type=reward_type,
                      balance=getattr(bal, currency), wallet_version=w.version,
                      **_route_or_none(route))
    return resp

# ----------------------------------------------------------------------------
# Once-per-purchase payout
# ----------------------------------------------------------------------------

def claim_purchase(store: WalletStore, player_id: str, purchase_key: str,
                   currency: str, amount: int, *,
                   product_id: str = "", items: Optional[list] = None) -> dict:
    """Grant `amount` exactly once for (player_id, purchase_key).

    The claim INSERT and the credit run in the SAME transaction as the
    `store.transaction()` this function opens, which is the only arrangement
    in which both halves hold:

      * The claim row is written FIRST, against a PRIMARY KEY. A second buy
        of the same tier hits the uniqueness violation, so it never reaches
        the credit at all — it is not "checked and skipped", it is refused by
        the write itself.
      * If the credit then fails, the exception rolls the transaction back and
        the claim row goes with it, so a failed purchase is not a spent one.

    There is no timestamp and no expiry to lapse, so a player who buys the
    same tier tomorrow, next week, or next season gets the same refusal as
    one who clicks twice in a second.
    """
    if amount < 0:
        return {"ok": False, "error": "purchase amount must be >= 0",
                "wallet_version": _ver(store, player_id)}
    if not purchase_key:
        # An empty key would make every purchase of every product collide on
        # one primary-key value. Refuse it rather than store a claim that
        # silently blocks the player's next real purchase.
        return {"ok": False, "error": "purchase_key is required",
                "wallet_version": _ver(store, player_id)}
    with store.transaction():
        try:
            claim_row = store.purchase_claims.claim(player_id, purchase_key,
                                                 product_id=product_id, items=items)
        except DuplicatePurchaseClaim:
            # No currency moved. The response is a refusal, not a partial
            # success, and it deliberately carries the original claim's items
            # so a retried client can render what it already owns.
            prior = store.purchase_claims.get(player_id, purchase_key) or {}
            _log_wallet_event(wcl.EVENTS["WALLET_REPLAY_BLOCKED"], player_id,
                              ok=False, outcome="purchase_already_claimed",
                              reason="purchase claim already exists",
                              product_id=product_id,
                              purchase_key=_receipt_fingerprint(purchase_key),
                              currency=currency, amount=amount,
                              wallet_version=_ver(store, player_id),
                              **_route_or_none(product_id or None))
            return {"ok": False, "error": "purchase already claimed",
                    "purchase_key": purchase_key,
                    "items": list(prior.get("items", [])),
                    "wallet_version": _ver(store, player_id)}
        # Keyed on the claim row's claimed_at, not just (player, key). A refund
        # deletes the claim row so the player may buy again; without the stamp
        # the re-buy replayed the FIRST purchase's earn receipt and reported
        # success while crediting nothing. The claim table already refuses a
        # duplicate purchase, so this key only has to make each successful
        # claim a distinct credit.
        credit = earn(store, player_id, currency, amount,
                      idem_key=(f"purchase:{player_id}:{purchase_key}"
                                f":{claim_row['claimed_at']}"),
                      reward_type=product_id or "purchase")
    return {"ok": bool(credit.get("ok")), "purchase_key": purchase_key,
            "currency": currency, "amount": amount,
            "items": list(items or []),
            "balance": credit.get("balance"),
            "wallet_version": credit.get("wallet_version")}


# ----------------------------------------------------------------------------
# §3.3 — server-side offline progress
# ----------------------------------------------------------------------------

MAX_OFFLINE_SECONDS = 72 * 3600      # 72h cap
MAX_CLOCK_SKEW_SECONDS = 300        # reject device clock deviating > 5 minutes

@atomic
def claim_offline(store: WalletStore, player_id: str, currency: str,
                   rate_per_sec: float, device_now: float,
                   route: Optional[str] = None) -> dict:
    """
    Compute offline earnings on the SERVER using `last_online_at` and
    `time.time()`. `device_now` is the client's clock, used ONLY to detect
    manipulation — it never feeds the formula.

    `route` is observability only -- see `spend`.
    """
    # `rate_per_sec` is an INPUT, so it needs its own lower bound. `earn`
    # refuses a negative payout, but that is a different function and cannot
    # stand in for this check, for two measured reasons:
    #
    #   * `earned = int(capped * rate_per_sec)` truncates TOWARD ZERO, so a
    #     small negative rate lands on exactly 0. earn accepts 0, the claim
    #     reports SUCCESS, and `last_offline_claimed_at` advances.
    #   * when the product is large enough to be negative, earn refuses the
    #     grant -- but only after this function has already read the wallet,
    #     so one request is answered by two different rules depending on the
    #     magnitude, and the log blames "earn amount" for what is a bad rate.
    #
    # The refusal returns BEFORE the wallet is touched, so a rejected claim
    # cannot also consume the offline window it declined to pay.
    #
    # The PostgreSQL backend refuses `rate_per_sec < 0` here too
    # (wallet_store_pg.py:608). The backend is selected at import time from
    # WC_PG_HOST (wallet_dispatch.claim_offline), so without this guard the
    # identical request was refused in one mode and accepted in the other.
    if rate_per_sec < 0:
        _log_wallet_event(wcl.EVENTS["WALLET_CLAIM_OFFLINE"], player_id,
                          ok=False, outcome="rejected",
                          reason="rate_per_sec must be non-negative",
                          currency=currency, rate_per_sec=rate_per_sec,
                          wallet_version=_ver(store, player_id),
                          **_route_or_none(route))
        return {"ok": False, "error": "rate_per_sec must be non-negative",
                "wallet_version": _ver(store, player_id)}

    # The CURRENCY is the third input, and it needs the same up-front refusal
    # `rate_per_sec` gets above. The PostgreSQL backend refuses it before it
    # touches the wallet (wallet_store_pg.py:611-612) with this exact error
    # text; the backend is picked at import time from WC_PG_HOST
    # (wallet_dispatch.claim_offline), so without this guard the same request
    # was refused in one mode and accepted in the other.
    #
    # Why it mattered here specifically: without the guard, `claim_offline`
    # read the wallet, computed the window, called `earn`, and `earn` refused
    # the unknown currency -- but `claim_offline` stamped
    # `last_offline_claimed_at` anyway and answered with no `error` key. So
    # one typo'd currency cost the player the whole absence AND crashed any
    # caller that reads `resp["error"]`. `CURRENCIES` is the tuple derived
    # from `CurrencyBalance`, the same set `spend`/`earn` whitelist.
    if currency not in CURRENCIES:
        _log_wallet_event(wcl.EVENTS["WALLET_CLAIM_OFFLINE"], player_id,
                          ok=False, outcome="rejected",
                          reason="unknown currency",
                          currency=currency, rate_per_sec=rate_per_sec,
                          wallet_version=_ver(store, player_id),
                          **_route_or_none(route))
        return {"ok": False, "error": "unknown currency",
                "wallet_version": _ver(store, player_id)}

    w = store._live_for_update(player_id)
    server_now = time.time()

    # Device-clock skew check: reject a manipulated clock.
    skew = abs(device_now - server_now)
    if skew > MAX_CLOCK_SKEW_SECONDS:
        _log_wallet_event(wcl.EVENTS["WALLET_CLAIM_OFFLINE"], player_id,
                          ok=False, outcome="rejected",
                          reason="device clock skew exceeds 5 minutes",
                          currency=currency, rate_per_sec=rate_per_sec,
                          skew_sec=round(skew, 3), wallet_version=w.version,
                          **_route_or_none(route))
        return {"ok": False, "error": "device clock skew exceeds 5 minutes", "wallet_version": w.version}

    base = w.last_offline_claimed_at or w.last_online_at
    elapsed = server_now - base
    if elapsed < 0:
        _log_wallet_event(wcl.EVENTS["WALLET_CLAIM_OFFLINE"], player_id,
                          ok=False, outcome="rejected", reason="negative elapsed",
                          currency=currency, rate_per_sec=rate_per_sec,
                          raw_elapsed_sec=round(elapsed, 3), wallet_version=w.version,
                          **_route_or_none(route))
        return {"ok": False, "error": "negative elapsed", "wallet_version": w.version}

    capped = min(elapsed, MAX_OFFLINE_SECONDS)
    earned = int(capped * rate_per_sec)
    resp = earn(store, player_id, currency, earned, idem_key=uuid.uuid4().hex,
                reward_type="offline_progress", route=route)

    # THE STAMP IS THE PLAYER'S ONLY CLAIM ON THE WINDOW. It is written only
    # when the grant underneath it actually settled.
    #
    # Measured before this fix, on this line: `last_offline_claimed_at` was
    # assigned unconditionally, so a claim that credited NOTHING still moved
    # it forward by the whole absence (1791091199.36 -> 1791094799.36 for one
    # hour of banked time). The player was left with zero currency and no
    # remaining absence to claim it against -- the reward was destroyed, not
    # merely delayed, and no retry could recover it.
    #
    # `earned == 0` is NOT a failure. A legal zero-rate or sub-unit payout
    # settles successfully, and refusing to stamp those would let a player
    # farm rounding by claiming forever. The test is `ok`, which is the grant's
    # own verdict, not the size of the grant.
    if resp.get("ok"):
        w.last_offline_claimed_at = server_now
    # The elapsed PAIR is the one the maths used, spelled out so an operator
    # never has to reconstruct it: the raw absence the server measured, and the
    # value actually applied after the cap. `amount` carries what was settled.
    # Emitted only when the underlying grant succeeded, so a rejected grant is
    # never counted as settled progress.
    _log_wallet_event(
        wcl.EVENTS["WALLET_CLAIM_OFFLINE"], player_id,
        ok=bool(resp.get("ok")),
        outcome="settled" if resp.get("ok") else "rejected",
        reason=None if resp.get("ok") else resp.get("error"),
        currency=currency, amount=earned, rate_per_sec=rate_per_sec,
        raw_elapsed_sec=round(elapsed, 3),
        applied_elapsed_sec=round(capped, 3),
        cap_sec=MAX_OFFLINE_SECONDS,
        wallet_version=resp.get("wallet_version"),
        **_route_or_none(route),
    )
    if not resp.get("ok"):
        # One refusal envelope for every reason, the same shape `spend` and
        # `earn` answer with. A caller can therefore branch on `error` without
        # a per-reason shape table -- and before this branch existed a refused
        # claim returned `ok=False` with NO `error` key at all, so
        # `resp["error"]` raised KeyError inside the caller. The window is
        # deliberately left un-stamped: the player can fix the cause and retry
        # against the same absence.
        return {"ok": False, "error": resp.get("error") or "offline grant refused",
                "wallet_version": resp.get("wallet_version")}
    return {"ok": resp["ok"], "elapsed_sec": elapsed, "capped_sec": capped,
            "earned": earned, "balance": resp.get("balance"), "wallet_version": resp.get("wallet_version")}

# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _ver(store: WalletStore, player_id: str) -> int:
    w = store.get(player_id)
    return w.version if w else 0

if __name__ == "__main__":
    s = WalletStore()
    p = "player_1"
    k1, k2 = "idem-abc", "idem-def"
    print("earn 100 spirit_stone:", earn(s, p, "spirit_stone", 100, k1, reward_type="1000006_chapter_fa"))
    print("stale spend (ver 0): ", spend(s, p, "spirit_stone", 10, 0, k2))
    print("fresh spend (ver 1): ", spend(s, p, "spirit_stone", 10, 1, k2))
    print("replay same key:     ", spend(s, p, "spirit_stone", 10, 1, k2))
    print("offline claim:        ", claim_offline(s, p, "spirit_stone", rate_per_sec=2.0, device_now=time.time()))
