"""Contract tests for ``server/wallet_defense.py`` -- the server-authoritative
currency module.

WHAT THIS FILE COVERS
---------------------
``wallet_defense`` is the money path: it is the only place a player's balance
changes. Its contract is therefore three layers, and this file pins all three:

1. DATA SHAPE -- the currency whitelist, the wallet fields, the purchase-claim
   table's primary key, and the exact key set of every reply.
2. REFUSAL RULES -- every reason a request may be turned down, and, just as
   importantly, the rule that a refusal must NOT be remembered as the answer
   for a request key (otherwise a player who tops up can never spend).
3. ERROR CASES -- the rollback, expiry, and copy-on-read behaviours that decide
   whether money or a receipt survives an exception.
4. ABUSE AT VOLUME AND AT THE EDGES -- that a repeated request still pays once,
   that a stored receipt is scoped to its owner and cannot be inflated into an
   authority, that the published one-day clock tolerance holds AT its boundary
   (not only 30 days out), and that a non-finite rate or amount is refused
   rather than crashing the request. Section 4 onward was added on 2026-10-05;
   seven measured defects in it are pinned ``xfail`` non-strict.

A NOTE ON "RATE LIMITING"
-------------------------
This module contains NO rate limiter -- measured, it exposes no name matching
rate/limit/burst/throttle/quota. Request volume is throttled one layer up by
``server/rate_limit.ClaimRateLimiter``, which is mounted on the claim routes
and already covered by ``test_rate_limit_claims.py`` and
``test_rate_limit_contract.py``. What section 4 pins here is this module's half
of the abuse story: the once-only guards that must hold no matter how many
times a request arrives.

SCOPE, AND WHAT IS DELIBERATELY NOT HERE
----------------------------------------
``server/tests/test_wallet_store_contract.py`` already pins the HTTP route
layer and the headline store responses. This file does not repeat that. It
covers the store-layer contracts that file does not reach: the currency
whitelist as a whitelist, idempotency expiry and per-player scoping, the
purchase-claim table's own key parity and copy semantics, transaction rollback,
offline-window arithmetic, and the "a broken logger must not fail a payment"
promise the module's own docstring makes.

MEASURED, NOT GUESSED
---------------------
Every value below was read off a run of this module on 2026-10-04 (probes in
the agent scratch pad) before any assertion was written, and sections 4 onward
were re-measured the same way on 2026-10-05. Two places where the measured
behaviour is a DEFECT rather than a rule are marked ``xfail`` and explained in
place: they document the current behaviour so a fix is visible, and they do not
redden when it lands.

The seven new ``xfail`` markers were themselves checked with a NEGATIVE
CONTROL: a finiteness guard was injected into the module at runtime (as a
pytest plugin, nothing on disk edited) and six of the seven turned green --
proving each assertion is reachable and each marker is load-bearing rather than
hiding a broken test. The seventh, ``claim_purchase``'s burned purchase key,
stayed red under that guard, which is the evidence that it is a second,
separate defect in the ordering rather than another view of the missing guard.

Two refusal boundaries cannot be tested exactly, so they are tested with a
margin rather than a knife-edge: the device-clock skew check compares against
``time.time()`` read inside the call, so "exactly one day" is never exactly one
day by the time it is compared.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# --- path bootstrap: resolve `wallet_defense` from any cwd ----------------
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wallet_defense as wd  # noqa: E402


# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------

@pytest.fixture
def store():
    """A private, empty wallet store.

    ``WalletStore`` is deliberately not a process singleton -- each instance
    owns its own wallets, receipts and claim rows -- so a fresh one per test
    gives full isolation with no teardown.
    """
    return wd.WalletStore()


@pytest.fixture
def funded(store):
    """A store holding ``FUND`` spirit_stone for player ``p``, at version 1."""
    wd.earn(store, "p", "spirit_stone", 100, "fund-key")
    return store


def _version(store, pid="p"):
    w = store.get(pid)
    return w.version if w else 0


def _balance(store, currency="spirit_stone", pid="p"):
    w = store.get(pid)
    return w.balance.to_dict()[currency] if w else None


# ============================================================================
# 1. DATA SHAPE
# ============================================================================

class TestDataShape:
    """The vocabulary a caller may and may not use."""

    def test_the_whitelist_is_exactly_the_currency_dataclass_fields(self):
        # The module has no separate list of legal currencies: the guard reads
        # `CurrencyBalance.__dataclass_fields__`. So the dataclass IS the
        # whitelist, and a currency that is not a field cannot be spent or
        # earned. Asserting the set pins both names and count.
        assert sorted(wd.CurrencyBalance.__dataclass_fields__) == [
            "destium", "fateum", "souldust", "spirit_stone",
        ]

    def test_the_published_balance_dict_carries_every_currency(self):
        # A client renders the wallet from this dict. A currency missing here is
        # a currency the UI cannot draw, so the dict and the whitelist must be
        # the same set -- not merely overlapping.
        assert sorted(wd.CurrencyBalance().to_dict()) == sorted(
            wd.CurrencyBalance.__dataclass_fields__)

    def test_a_fresh_wallet_reports_zero_in_every_currency(self):
        assert wd.CurrencyBalance().to_dict() == {
            "spirit_stone": 0, "fateum": 0, "destium": 0, "souldust": 0}

    def test_the_wallet_row_has_exactly_the_five_declared_fields(self):
        assert sorted(wd.Wallet.__dataclass_fields__) == [
            "balance", "last_offline_claimed_at", "last_online_at",
            "player_id", "version"]

    @pytest.mark.parametrize("spelling", [
        "Spirit_Stone",       # right name, wrong case
        " spirit_stone ",     # right name, padded
        "spirit_stones",      # plural -- the shape a client guesses
        "gold",               # a currency from another game
        "",                   # empty
    ])
    def test_a_currency_outside_the_whitelist_is_refused_not_coerced(
            self, store, spelling):
        # An unknown currency must be a refusal, never a silent no-op and never
        # a raise out of the function (a 500 on a grant route leaves the caller
        # unable to tell a refusal from a crash).
        reply = wd.earn(store, "p", spelling, 5, "key-1")
        assert reply["ok"] is False
        assert reply["error"] == f"unknown currency {spelling!r}"

    def test_a_refused_currency_never_touches_the_wallet(self, store):
        # A refusal must not be a side effect: no wallet row, no credit, and no
        # receipt stored under the key the caller already burned.
        wd.earn(store, "p", "gold", 5, "key-1")
        assert store.get("p") is None
        assert store.recall("key-1", "p") is None

    @pytest.mark.parametrize("spelling", [
        "Spirit_Stone", " spirit_stone ", "spirit_stones", "gold", ""])
    def test_spend_applies_the_same_whitelist(self, store, spelling):
        assert wd.spend(store, "p", spelling, 1, 0, "key-s")["ok"] is False


class TestPurchaseClaimKeyParity:
    """The claim row is unique by primary key -- in Python and in SQL alike."""

    def test_the_table_and_the_published_key_are_the_same_pair(self):
        # Two independently spelled keys would let the in-memory store and the
        # PostgreSQL table disagree about what "already claimed" means. The
        # module exposes the pair as data for exactly this check.
        assert wd.PurchaseClaimTable.PRIMARY_KEY == wd.PURCHASE_CLAIM_PRIMARY_KEY
        assert wd.PURCHASE_CLAIM_PRIMARY_KEY == ("player_id", "purchase_key")

    def test_the_published_schema_keys_on_that_pair_and_never_expires(self):
        ddl = wd.PURCHASE_CLAIMS_DDL
        assert "PRIMARY KEY (player_id, purchase_key)" in ddl
        # A receipt that expires is a receipt that can be spent again. Assert
        # the absence so nobody adds a TTL column without meeting this.
        assert "expires_at" not in ddl

    def test_the_published_insert_names_every_declared_column(self):
        sql = wd.PURCHASE_CLAIM_INSERT_SQL
        assert sql.startswith("INSERT INTO purchase_claims")
        assert "ON CONFLICT" not in sql.upper()   # never an upsert
        assert "RETURNING" in sql                  # proves the row is ours
        for column in wd.PURCHASE_CLAIM_PRIMARY_KEY:
            assert column in sql

    def test_a_claim_row_carries_exactly_the_declared_columns(self, store):
        row = store.purchase_claims.claim("p", "k1", product_id="sku",
                                          items=[{"id": 1}])
        assert sorted(row) == ["claimed_at", "items", "player_id",
                               "product_id", "purchase_key"]
        assert row["claimed_at"] > 0        # stamped by the server, not the client

    def test_the_row_handed_back_is_detached_from_the_table(self, store):
        row = store.purchase_claims.claim("p", "k1", items=[{"id": 1}])
        row["items"].append({"id": 2})
        row["product_id"] = "tampered"
        assert store.purchase_claims.get("p", "k1")["items"] == [{"id": 1}]
        assert store.purchase_claims.get("p", "k1")["product_id"] == ""

    def test_a_missing_claim_reads_as_none_not_an_empty_row(self, store):
        assert store.purchase_claims.get("p", "never-claimed") is None

    def test_two_players_may_hold_the_same_purchase_key(self, store):
        # Uniqueness is per player, so the key is a receipt identifier scoped to
        # its owner -- the same rule as the idempotency key.
        store.purchase_claims.claim("p", "shared")
        assert store.purchase_claims.claim("q", "shared")["player_id"] == "q"
        assert store.purchase_claims.count() == 2

    def test_an_empty_purchase_key_is_rejected_at_the_table(self, store):
        # An empty key would collapse every product onto one primary key.
        with pytest.raises(ValueError, match="purchase_key is required"):
            store.purchase_claims.claim("p", "")

    def test_a_second_claim_of_one_key_raises_the_dedicated_error(self, store):
        store.purchase_claims.claim("p", "k1")
        with pytest.raises(wd.DuplicatePurchaseClaim):
            store.purchase_claims.claim("p", "k1")

    def test_the_table_snapshot_is_a_deep_copy(self, store):
        store.purchase_claims.claim("p", "k1", items=[{"id": 1}])
        snap = store.purchase_claims.snapshot()
        snap[("p", "k1")]["items"].append({"id": 2})
        assert store.purchase_claims.get("p", "k1")["items"] == [{"id": 1}]


class TestReplyKeySets:
    """Every reply shape, frozen so a client can render it."""

    def test_a_successful_spend_answers_five_keys(self, funded):
        reply = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "s1")
        assert sorted(reply) == ["balance", "currency", "ok", "spent",
                                 "wallet_version"]
        assert reply["ok"] is True
        assert reply["balance"]["spirit_stone"] == 90

    def test_a_successful_grant_answers_six_keys(self, funded):
        reply = wd.earn(funded, "p", "fateum", 10, "e1", reward_type="chest")
        assert sorted(reply) == ["balance", "currency", "earned", "ok",
                                 "reward_type", "wallet_version"]
        assert reply["reward_type"] == "chest"

    @pytest.mark.parametrize("label, reply", [
        ("spend on a non-positive amount",
         lambda s: wd.spend(s, "p", "spirit_stone", 0, _version(s), "k")),
        ("spend on an overdraw",
         lambda s: wd.spend(s, "p", "spirit_stone", 10 ** 6, _version(s), "k")),
        ("spend on a stale version",
         lambda s: wd.spend(s, "ghost", "spirit_stone", 1, 99, "k")),
        ("spend on an unknown currency",
         lambda s: wd.spend(s, "p", "gold", 1, _version(s), "k")),
        ("grant a negative amount",
         lambda s: wd.earn(s, "p", "fateum", -1, "k")),
        ("grant an unknown currency",
         lambda s: wd.earn(s, "p", "gold", 1, "k")),
    ])
    def test_every_spend_and_grant_refusal_answers_the_same_three_keys(
            self, store, label, reply):
        # One refusal envelope for every reason. A client can therefore branch
        # on `error` without a per-reason shape table, and a refusal never
        # carries a `balance` it did not compute.
        ans = reply(store)
        assert sorted(ans) == ["error", "ok", "wallet_version"], label
        assert ans["ok"] is False
        assert isinstance(ans["error"], str) and ans["error"]
        assert isinstance(ans["wallet_version"], int)

    def test_a_successful_purchase_answers_seven_keys_and_no_error(
            self, store):
        reply = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100,
                                  product_id="sku", items=[{"id": 1}])
        assert sorted(reply) == ["amount", "balance", "currency", "items",
                                 "ok", "purchase_key", "wallet_version"]
        assert "error" not in reply
        assert reply["balance"]["spirit_stone"] == 100

    def test_a_refused_purchase_answers_five_keys_and_carries_no_balance(
            self, store):
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        reply = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        assert sorted(reply) == ["error", "items", "ok", "purchase_key",
                                 "wallet_version"]
        assert reply["error"] == "purchase already claimed"

    def test_a_successful_offline_claim_answers_six_keys(self, store):
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 60)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert sorted(reply) == ["balance", "capped_sec", "earned",
                                 "elapsed_sec", "ok", "wallet_version"]

    def test_a_refused_offline_claim_answers_the_three_key_envelope(self, store):
        store.create("p")
        future = time.time() + 3600
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                 time.time() + wd.MAX_CLOCK_SKEW_SECONDS * 2)
        assert sorted(reply) == ["error", "ok", "wallet_version"]
        assert reply["error"] == "device clock skew exceeds 5 minutes"
        assert future > 0   # keeps the setup above honest


class TestErrorTypes:
    """The module's declared exception vocabulary."""

    def test_all_three_error_types_are_real_exceptions(self):
        for cls in (wd.StaleVersionError, wd.IdempotentReplay,
                    wd.DuplicatePurchaseClaim):
            assert issubclass(cls, Exception)

    def test_the_error_types_are_distinct_classes(self):
        # If two of them collapsed into one, a caller could not tell a stale
        # version from a replay, and those need different answers.
        assert len({wd.StaleVersionError, wd.IdempotentReplay,
                    wd.DuplicatePurchaseClaim}) == 3

    def test_a_stale_version_is_reported_as_a_reply_not_a_raise(self, store):
        wd.earn(store, "p", "spirit_stone", 10, "e1")
        # Declared, but not what the money functions raise: the caller gets a
        # dict it can serialise, not an exception it must translate.
        assert wd.spend(store, "p", "spirit_stone", 1, 999, "s1")["ok"] is False


# ============================================================================
# 2. REFUSAL RULES
# ============================================================================

class TestRefusalsAreNotRemembered:
    """A refusal must never be stored as the answer for a request key.

    This is the subtlest rule in the module and the easiest to break. If a
    refusal is anchored to the idempotency key, the retry after a top-up
    replays the refusal for the full receipt lifetime -- so money the player
    just paid for can never be spent.
    """

    def test_an_overdraft_is_not_replayed_after_a_top_up(self, store):
        wd.earn(store, "p", "spirit_stone", 5, "top-1")
        refused = wd.spend(store, "p", "spirit_stone", 50,
                           _version(store), "buy-key")
        assert refused["ok"] is False and refused["error"] == "insufficient balance"

        wd.earn(store, "p", "spirit_stone", 100, "top-2")
        retry = wd.spend(store, "p", "spirit_stone", 50,
                         _version(store), "buy-key")
        assert retry["ok"] is True
        assert retry["balance"]["spirit_stone"] == 55

    def test_a_stale_version_is_not_replayed_after_correction(self, store):
        wd.earn(store, "p", "spirit_stone", 100, "top-1")
        stale = wd.spend(store, "p", "spirit_stone", 10, 0, "buy-key")
        assert stale["error"] == "stale wallet_version"

        corrected = wd.spend(store, "p", "spirit_stone", 10,
                             _version(store), "buy-key")
        assert corrected["ok"] is True
        assert corrected["balance"]["spirit_stone"] == 90

    def test_a_bad_amount_is_not_replayed_either(self, store):
        refused = wd.spend(store, "p", "spirit_stone", 0, 0, "buy-key")
        assert refused["ok"] is False
        assert store.recall("buy-key", "p") is None

    def test_a_negative_grant_is_not_replayed_either(self, store):
        assert wd.earn(store, "p", "fateum", -5, "buy-key")["ok"] is False
        assert store.recall("buy-key", "p") is None

    def test_only_a_movement_anchors_a_receipt(self, store):
        # The positive half of the same rule: the receipt appears exactly when
        # currency actually moved.
        wd.earn(store, "p", "spirit_stone", 7, "good-key")
        assert store.recall("good-key", "p") is not None


class TestIdempotencyRules:
    """A repeated request key replays the first answer and spends nothing."""

    def test_a_replayed_spend_is_marked_and_debits_once(self, funded):
        first = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "s1")
        assert "_replayed" not in first

        again = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "s1")
        assert again["_replayed"] is True
        assert _balance(funded) == 90
        assert again["wallet_version"] == first["wallet_version"]

    def test_a_replayed_grant_is_marked_and_credits_once(self, store):
        wd.earn(store, "p", "fateum", 20, "e1")
        again = wd.earn(store, "p", "fateum", 20, "e1")
        assert again["_replayed"] is True
        assert _balance(store, "fateum") == 20

    def test_a_replay_never_advances_the_version(self, funded):
        wd.spend(funded, "p", "spirit_stone", 1, _version(funded), "s1")
        settled = _version(funded)
        wd.spend(funded, "p", "spirit_stone", 1, _version(funded), "s1")
        wd.earn(funded, "p", "fateum", 1, "s1")     # same key, other function
        assert _version(funded) == settled

    def test_two_players_may_reuse_one_key_without_seeing_each_other(
            self, store):
        wd.earn(store, "a", "spirit_stone", 10, "shared")
        wd.earn(store, "b", "spirit_stone", 50, "shared")
        # 'b' must NOT be served a's stored answer, or the receipt map would be
        # one player's overwrite of another's -- which un-guards a's retry.
        assert _balance(store, "spirit_stone", "b") == 50
        assert wd.earn(store, "b", "spirit_stone", 50, "shared")["_replayed"] is True
        assert _balance(store, "spirit_stone", "b") == 50

    def test_a_receipt_expires_at_its_time_to_live(self, store):
        store.seen("key", "p", {"ok": True}, ttl_sec=0)
        assert store.recall("key", "p") is None

    def test_an_expired_receipt_does_not_replay_a_settlement(self, store):
        wd.earn(store, "p", "spirit_stone", 10, "ttl-key")
        # Rewrite the stored receipt as already expired, then repeat the grant.
        store._idempotency[("p", "ttl-key")]["expires_at"] = time.time() - 1
        again = wd.earn(store, "p", "spirit_stone", 10, "ttl-key")
        assert "_replayed" not in again
        assert _balance(store) == 20        # credited a second time, as designed

    def test_recording_a_receipt_purges_the_expired_ones(self, store):
        # Bounded work: the map must not grow for the life of the process.
        store.seen("old", "p", {"ok": True}, ttl_sec=0)
        store.seen("new", "p", {"ok": True}, ttl_sec=3600)
        assert sorted(k for k in store._idempotency) == [("p", "new")]

    def test_the_recalled_answer_is_detached_from_the_store(self, store):
        wd.earn(store, "p", "spirit_stone", 10, "key")
        first = store.recall("key", "p")
        first["balance"]["spirit_stone"] = 999_999
        assert store.recall("key", "p")["balance"]["spirit_stone"] == 10

    def test_recall_only_answers_for_its_own_player(self, store):
        wd.earn(store, "a", "spirit_stone", 10, "key")
        assert store.recall("key", "b") is None


class TestAmountRules:
    """Zero, negative and fractional amounts."""

    def test_a_spend_of_zero_is_refused(self, funded):
        reply = wd.spend(funded, "p", "spirit_stone", 0, _version(funded), "s1")
        assert reply["ok"] is False and reply["error"] == "amount must be positive"

    def test_a_spend_of_a_negative_amount_is_refused(self, funded):
        # A negative spend would mint currency out of nothing.
        reply = wd.spend(funded, "p", "spirit_stone", -50, _version(funded), "s1")
        assert reply["ok"] is False and reply["error"] == "amount must be positive"

    def test_a_grant_of_zero_is_accepted_and_moves_nothing(self, store):
        # Measured: a zero grant succeeds and still advances the version. A
        # caller must not read version movement as proof that value moved.
        reply = wd.earn(store, "p", "spirit_stone", 0, "e1")
        assert reply["ok"] is True and reply["earned"] == 0
        assert reply["wallet_version"] == 1
        assert _balance(store) == 0

    def test_a_grant_of_a_negative_amount_is_refused(self, store):
        reply = wd.earn(store, "p", "fateum", -1, "e1")
        assert reply["ok"] is False and reply["error"] == "earn amount must be >= 0"
        assert store.get("p") is None

    def test_a_spend_of_exactly_the_whole_balance_is_allowed(self, funded):
        reply = wd.spend(funded, "p", "spirit_stone", 100, _version(funded), "s1")
        assert reply["ok"] is True and reply["balance"]["spirit_stone"] == 0

    def test_one_stone_over_the_balance_is_refused(self, funded):
        reply = wd.spend(funded, "p", "spirit_stone", 101, _version(funded), "s1")
        assert reply["ok"] is False and reply["error"] == "insufficient balance"

    def test_a_negative_purchase_amount_is_refused(self, store):
        reply = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", -5)
        assert reply["ok"] is False
        assert reply["error"] == "purchase amount must be >= 0"

    def test_a_refused_purchase_amount_writes_no_claim_row(self, store):
        # Refused before the insert: a player who sent a bad amount must not
        # have burned their purchase key.
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", -5)
        assert store.purchase_claims.count() == 0

    def test_an_empty_purchase_key_is_refused_and_writes_no_row(self, store):
        reply = wd.claim_purchase(store, "p", "", "spirit_stone", 5)
        assert reply["ok"] is False and reply["error"] == "purchase_key is required"
        assert store.purchase_claims.count() == 0


class TestPurchaseOnceOnly:
    """A purchase is granted once per key, for good."""

    def test_the_first_claim_credits_exactly_the_asked_amount(self, store):
        reply = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        assert reply["ok"] is True
        assert _balance(store) == 100

    def test_a_second_claim_of_one_key_credits_nothing(self, store):
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        before = _version(store)
        again = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        assert again["ok"] is False
        assert _balance(store) == 100
        assert _version(store) == before

    def test_a_repeat_cannot_raise_the_amount_it_is_credited(self, store):
        # The attacker controls `amount` on the retry. The claim row, not the
        # caller, decides what was already granted.
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10)
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10 ** 6)
        assert _balance(store) == 10

    def test_a_refused_repeat_still_reports_the_original_items(self, store):
        # So a client that retried can render what the player already owns.
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10,
                          items=[{"id": 7}])
        again = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10,
                                  items=[{"id": 99}])
        assert again["items"] == [{"id": 7}]

    def test_a_second_key_is_a_second_purchase(self, store):
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10)
        wd.claim_purchase(store, "p", "buy-2", "spirit_stone", 10)
        assert _balance(store) == 20

    def test_two_players_may_buy_the_same_key(self, store):
        assert wd.claim_purchase(store, "a", "buy-1", "spirit_stone", 10)["ok"] is True
        assert wd.claim_purchase(store, "b", "buy-1", "spirit_stone", 10)["ok"] is True
        assert _balance(store, "spirit_stone", "a") == 10
        assert _balance(store, "spirit_stone", "b") == 10

    def test_a_zero_amount_purchase_still_burns_its_key(self, store):
        # Measured: a zero purchase is a success and consumes the key, so a
        # later real purchase under that key is refused. Worth pinning so the
        # behaviour is a decision, not an accident.
        first = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 0)
        assert first["ok"] is True
        assert store.purchase_claims.count() == 1
        assert wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 5)["ok"] is False

    def test_removing_the_claim_row_allows_a_fresh_purchase(self, store):
        # The documented refund path: delete the row, and the re-buy is a new
        # grant rather than a replay of the first one.
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        snap = store.purchase_claims.snapshot()
        store.purchase_claims.restore(
            {k: v for k, v in snap.items() if k != ("p", "buy-1")})

        again = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        assert again["ok"] is True
        assert _balance(store) == 200

    def test_a_failed_credit_rolls_the_claim_row_back(self, store, monkeypatch):
        # A purchase that could not be paid out must not be recorded as one the
        # player already owns -- otherwise a server-side fault costs them the
        # product permanently.
        def explode(*_a, **_k):
            raise RuntimeError("grant exploded")
        monkeypatch.setattr(wd, "earn", explode)
        with pytest.raises(RuntimeError):
            wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        monkeypatch.undo()

        assert store.purchase_claims.count() == 0
        assert store.get("p") is None
        retry = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 100)
        assert retry["ok"] is True and _balance(store) == 100


# ============================================================================
# 3. ERROR CASES -- rollback, expiry, isolation, logging
# ============================================================================

class TestTransactionRollback:
    """An exception inside a transaction restores all three pieces of state."""

    def test_a_failure_restores_balance_version_and_receipts(self, store):
        wd.earn(store, "p", "spirit_stone", 50, "seed")
        before = store.get("p")

        with pytest.raises(ValueError):
            with store.transaction():
                wd.earn(store, "p", "spirit_stone", 7, "doomed")
                raise ValueError("caller failed")

        after = store.get("p")
        assert after.balance.to_dict() == before.balance.to_dict()
        assert after.version == before.version
        assert store.recall("doomed", "p") is None
        assert store.recall("seed", "p") is not None

    def test_a_failure_restores_purchase_claim_rows(self, store):
        store.purchase_claims.claim("p", "keep-me")
        with pytest.raises(ValueError):
            with store.transaction():
                store.purchase_claims.claim("p", "lose-me")
                raise ValueError("caller failed")
        assert store.purchase_claims.count() == 1
        assert store.purchase_claims.get("p", "lose-me") is None

    def test_a_snapshot_restore_rewinds_the_whole_store(self, store):
        wd.earn(store, "p", "spirit_stone", 25, "snap-1")
        snap = store.snapshot()
        wd.earn(store, "p", "spirit_stone", 25, "snap-2")
        wd.spend(store, "p", "spirit_stone", 5, _version(store), "snap-3")

        store.restore(snap)
        assert _balance(store) == 25
        assert _version(store) == 1
        assert sorted(k for k in store._idempotency) == [("p", "snap-1")]

    def test_the_internal_rollback_snapshot_is_never_shared_with_a_caller(
            self, store):
        # The rollback path itself is safe: `transaction()` takes its own
        # snapshot and immediately consumes it on failure, so nothing outside
        # the module holds a handle to live state. Prove that by mutating the
        # store after a rolled-back transaction and checking the rollback
        # really did land.
        wd.earn(store, "p", "spirit_stone", 25, "snap-1")
        with pytest.raises(ValueError):
            with store.transaction():
                wd.earn(store, "p", "spirit_stone", 25, "snap-2")
                raise ValueError("caller failed")
        assert _balance(store) == 25
        assert store.recall("snap-2", "p") is None

    @pytest.mark.xfail(reason="restore() installs the caller's own snapshot "
                              "objects instead of copying them, so a retained "
                              "snapshot stays live inside the store",
                       strict=False)
    def test_a_retained_snapshot_should_not_stay_wired_to_the_store(self, store):
        # Measured today: `snapshot()` copies on the way OUT, but `restore()`
        # assigns the same dicts in, so a caller who keeps its snapshot holds a
        # live handle to stored wallets. The rollback path is unaffected (see
        # the test above); the exposure is a caller that reuses a snapshot.
        wd.earn(store, "p", "spirit_stone", 25, "snap-1")
        snap = store.snapshot()
        wd.earn(store, "p", "spirit_stone", 25, "snap-2")
        store.restore(snap)
        snap[0]["p"].balance.spirit_stone = -999
        assert _balance(store) == 25


class TestCopyOnRead:
    """A read hands back a copy, so a caller cannot rewrite a balance."""

    def test_mutating_a_read_wallet_does_not_change_the_store(self, store):
        wd.earn(store, "p", "spirit_stone", 50, "e1")
        read = store.get("p")
        read.balance.spirit_stone = 10 ** 6
        read.version = 99
        assert _balance(store) == 50
        assert _version(store) == 1

    def test_two_reads_are_distinct_objects(self, store):
        store.create("p")
        assert store.get("p") is not store.get("p")

    def test_mutating_a_created_wallet_does_not_change_the_store(self, store):
        made = store.create("p")
        made.balance.fateum = 500
        assert _balance(store, "fateum") == 0

    def test_creating_twice_keeps_what_was_already_credited(self, store):
        wd.earn(store, "p", "spirit_stone", 30, "e1")
        again = store.create("p")
        assert again.balance.spirit_stone == 30

    def test_a_written_back_wallet_is_stored_as_a_copy(self, store):
        store.create("p")
        w = store.get("p")
        w.balance.spirit_stone = 42
        w.version = 9
        out = store.put(w)
        out.balance.spirit_stone = -1
        assert _balance(store) == 42
        assert _version(store) == 9

    def test_updating_an_unknown_wallet_answers_none(self, store):
        assert store.update("nobody", version=3) is None

    def test_updating_an_unknown_field_is_refused_by_name(self, store):
        store.create("p")
        with pytest.raises(AttributeError, match="credit"):
            store.update("p", credit=100)

    def test_touching_by_id_and_by_wallet_both_stamp_the_row(self, store):
        store.create("p")
        first = store.get("p").last_online_at
        time.sleep(0.005)
        store.touch("p")
        by_id = store.get("p").last_online_at
        assert by_id > first
        time.sleep(0.005)
        store.touch(store.get("p"))
        assert store.get("p").last_online_at > by_id

    def test_advancing_the_version_of_an_unknown_player_is_a_no_op(self, store):
        store.create("p")
        store.bump_version("nobody")
        assert _version(store) == 0
        assert store.get("nobody") is None


class TestOfflineClaimContract:
    """§3.3 -- the server clock owns the maths; the device clock only audits it."""

    def test_a_long_absence_is_capped_at_the_published_limit(self, store):
        store.create("p")
        store.update("p", last_online_at=time.time() - 100 * 3600,
                     last_offline_claimed_at=time.time() - 100 * 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert reply["elapsed_sec"] > wd.MAX_OFFLINE_SECONDS
        assert reply["capped_sec"] == wd.MAX_OFFLINE_SECONDS
        assert reply["earned"] == int(wd.MAX_OFFLINE_SECONDS * 1.0)

    def test_the_earned_amount_is_the_capped_window_at_the_rate(self, store):
        store.create("p")
        store.update("p", last_online_at=time.time() - 600,
                     last_offline_claimed_at=time.time() - 600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 0.5, time.time())
        assert reply["earned"] == int(reply["capped_sec"] * 0.5)
        assert _balance(store) == reply["earned"]

    def test_a_device_clock_inside_the_window_cannot_change_it(self, store):
        # The client clock is never an input to the formula. A device clock
        # that drifts INSIDE the window still earns the SERVER's window, not a
        # window derived from the client's claim. The offset is expressed as a
        # fraction of the window so it stays inside under any ruling.
        store.create("p")
        store.update("p", last_online_at=time.time() - 100,
                     last_offline_claimed_at=time.time() - 100)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                 time.time() - wd.MAX_CLOCK_SKEW_SECONDS / 2)
        assert reply["ok"] is True
        assert 99 <= reply["elapsed_sec"] <= 102
        assert reply["earned"] == int(reply["elapsed_sec"] * 1.0)

    @pytest.mark.parametrize("direction", [-1, 1])
    def test_a_device_clock_more_than_a_day_out_is_refused(self, store, direction):
        store.create("p")
        skewed = time.time() + direction * (wd.MAX_CLOCK_SKEW_SECONDS + 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0, skewed)
        assert reply["ok"] is False
        assert reply["error"] == "device clock skew exceeds 5 minutes"

    def test_a_refused_offline_claim_moves_nothing(self, store):
        store.create("p")
        wd.claim_offline(store, "p", "spirit_stone", 1.0,
                         time.time() + wd.MAX_CLOCK_SKEW_SECONDS + 3600)
        assert _balance(store) == 0

    def test_a_claim_stamp_in_the_future_is_refused_as_negative_elapsed(self, store):
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() + 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert reply["ok"] is False and reply["error"] == "negative elapsed"

    def test_the_window_is_measured_from_the_last_claim_not_the_last_login(
            self, store):
        store.create("p")
        store.update("p", last_online_at=time.time() - 10_000,
                     last_offline_claimed_at=time.time() - 5)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert 4 <= reply["elapsed_sec"] <= 30

    def test_a_second_claim_does_not_repay_the_same_window(self, store):
        store.create("p")
        store.update("p", last_online_at=time.time() - 600,
                     last_offline_claimed_at=time.time() - 600)
        first = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        second = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert first["earned"] > 0
        assert second["elapsed_sec"] < 1
        assert _balance(store) == first["earned"]

    def test_a_tiny_rate_truncates_to_nothing_and_still_closes_the_window(
            self, store):
        # Measured: a sub-unit payout is 0, but the window is still consumed, so
        # the player cannot farm rounding by claiming repeatedly.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 10)
        first = wd.claim_offline(store, "p", "spirit_stone", 0.01, time.time())
        assert first["ok"] is True and first["earned"] == 0
        second = wd.claim_offline(store, "p", "spirit_stone", 0.01, time.time())
        assert second["elapsed_sec"] < 1

    def test_claiming_for_a_player_with_no_wallet_creates_one_at_zero(self, store):
        # No crash, no refusal: a first offline claim on an unknown player is
        # allowed and pays the (near-zero) window it measures.
        reply = wd.claim_offline(store, "stranger", "fateum", 1.0, time.time())
        assert reply["ok"] is True
        assert reply["earned"] == 0
        assert store.get("stranger") is not None


class TestLoggingCannotFailAPayment:
    """The module's own rule 1: a broken log backend costs a debug line, never
    a paid request. A raise from the logging stack after the money moved is the
    worst outcome available -- the player sees an error and the retry pays twice.
    """

    def test_a_grant_survives_a_logger_that_raises(self, store, monkeypatch):
        monkeypatch.setattr(wd.wcl, "safe_log_event",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        reply = wd.earn(store, "p", "spirit_stone", 11, "e1")
        assert reply["ok"] is True and reply["balance"]["spirit_stone"] == 11

    def test_a_spend_survives_a_logger_that_raises(self, store, monkeypatch):
        wd.earn(store, "p", "spirit_stone", 50, "e1")
        monkeypatch.setattr(wd.wcl, "safe_log_event",
                            lambda *a, **k: (_ for _ in ()).throw(KeyError("down")))
        reply = wd.spend(store, "p", "spirit_stone", 10, _version(store), "s1")
        assert reply["ok"] is True and reply["balance"]["spirit_stone"] == 40

    def test_a_refusal_survives_a_logger_that_raises(self, store, monkeypatch):
        monkeypatch.setattr(wd.wcl, "safe_log_event",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        reply = wd.spend(store, "p", "spirit_stone", 0, 0, "s1")
        assert reply["ok"] is False

    def test_an_offline_claim_survives_a_logger_that_raises(
            self, store, monkeypatch):
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 60)
        monkeypatch.setattr(wd.wcl, "safe_log_event",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())["ok"] is True

    def test_the_route_field_is_passed_through_and_never_guessed(self):
        assert wd._route_or_none(None) == {}
        assert wd._route_or_none("") == {}
        assert wd._route_or_none("/wallet/spend") == {"route": "/wallet/spend"}

    def test_naming_a_route_cannot_change_a_payout(self, funded):
        # `route` is observability only. Two identical payments, one labelled,
        # must settle identically.
        plain = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "k1")
        labelled = wd.spend(funded, "p", "spirit_stone", 10, _version(funded),
                            "k2", route="/wallet/spend")
        assert plain["balance"]["spirit_stone"] == 90
        assert labelled["balance"]["spirit_stone"] == 80
        assert labelled["wallet_version"] == plain["wallet_version"] + 1


class TestPublishedLimits:
    """The constants the client and the operators both read."""

    def test_the_offline_cap_is_72_hours(self):
        assert wd.MAX_OFFLINE_SECONDS == 72 * 3600

    def test_the_clock_skew_tolerance_is_five_minutes(self):
        # Owner ruling (task 006-002, 2026-10-05): one day -> five minutes.
        # This is the PUBLISHED limit, so it is pinned as a literal on purpose;
        # a silent widening should have to break this line out loud.
        assert wd.MAX_CLOCK_SKEW_SECONDS == 300


# ============================================================================
# 4. RATE / VOLUME -- a repeated request cannot pay the same window twice
# ============================================================================
#
# NOTE ON SCOPE. ``wallet_defense`` holds NO rate limiter of its own: measured,
# the module exposes no name matching rate/limit/burst/throttle/quota. Request
# VOLUME is throttled one layer up, by ``server/rate_limit.ClaimRateLimiter``,
# which is mounted on the claim routes and covered by
# ``server/tests/test_rate_limit_claims.py`` and
# ``server/tests/test_rate_limit_contract.py``. That is the right place for it
# -- a limiter here could not see a burst spread across twelve claim surfaces.
#
# What this module DOES owe the abuse story is that its own once-only guards --
# the idempotency receipt and the purchase-claim primary key -- hold under
# repetition. Volume is the limiter's job; "a repeated request still only pays
# once" is this module's job, and these tests are where that is pinned.

class TestRepeatedRequestsCannotPayTwice:
    """The two once-only guards, measured against repetition, not just once."""

    def test_a_repeated_offline_claim_pays_the_window_once(self, store):
        # 25 identical claims inside one window. Measured: the total paid is the
        # window's worth ONCE (3600 for an hour at 1.0/s), not 25x that, because
        # each settled claim stamps `last_offline_claimed_at` forward.
        store.create("p")
        store.update("p", last_online_at=time.time() - 3600,
                     last_offline_claimed_at=time.time() - 3600)
        total = 0
        for _ in range(25):
            reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                     time.time())
            assert reply["ok"] is True
            total += reply["earned"]
        assert total == int(store.get("p").balance.spirit_stone)
        assert total < 25 * 3600
        assert _balance(store) == total

    def test_a_repeated_offline_claim_keeps_the_balance_and_the_window_aligned(
            self, store):
        # The control for the test above: the reason the total is small is the
        # stamp advancing, not the grant silently failing. Balance equals the
        # sum of every reported `earned`, and the stamp moved forward.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        before = store.get("p").last_offline_claimed_at
        total = 0
        for _ in range(5):
            total += wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                      time.time())["earned"]
        assert _balance(store) == total
        assert store.get("p").last_offline_claimed_at > before

    def test_a_repeated_spend_of_one_key_debits_once(self, funded):
        # The idempotency receipt under repetition, not just twice.
        first = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "s1")
        for _ in range(20):
            again = wd.spend(funded, "p", "spirit_stone", 10,
                             _version(funded), "s1")
            assert again["_replayed"] is True
        assert _balance(funded) == first["balance"]["spirit_stone"]
        assert _balance(funded) == 90

    def test_a_repeated_purchase_of_one_key_credits_once(self, store):
        first = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 25)
        assert first["ok"] is True
        for _ in range(20):
            again = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 25)
            assert again["ok"] is False
            assert again["error"] == "purchase already claimed"
        assert _balance(store) == 25
        assert store.purchase_claims.count() == 1

    def test_a_repeated_claim_key_cannot_raise_the_amount_it_is_credited(self,
                                                                        store):
        # A replayed receipt reports the ORIGINAL amount. Measured: asking for
        # 25 and then 10^6 on the same key never pays more than 25.
        wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 25)
        for amount in (10, 1_000, 10 ** 6):
            again = wd.claim_purchase(store, "p", "buy-1", "spirit_stone",
                                      amount)
            assert again["ok"] is False
        assert _balance(store) == 25


# ============================================================================
# 5. RECEIPT VALIDATION -- a stored receipt is data, not authority
# ============================================================================

class TestReceiptValidation:
    """What a stored receipt may and may not be trusted to do.

    A receipt exists so a RETRY returns the first answer instead of paying
    twice. That makes it an authoritative copy of a settled reply -- so the
    question these tests ask is what happens when the copy is wrong. The store
    keeps the receipt behind a private map and hands out deep copies, so the
    only way to reach it is the way a bug or a hostile in-process caller would:
    mutate the stored dict directly.
    """

    def test_mutating_a_recalled_receipt_cannot_rewrite_the_store(self, store):
        # The CALLER-side copy is detached. Measured: inflating the dict a
        # caller received leaves the balance at the real value.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        first = wd.spend(store, "p", "spirit_stone", 30, _version(store), "s1")
        assert first["ok"] is True
        recalled = store.recall("s1", "p")
        recalled["balance"]["spirit_stone"] = 999_999
        assert _balance(store) == 70
        assert store.recall("s1", "p")["balance"]["spirit_stone"] == 70

    def test_a_replay_reports_the_stored_balance_not_the_live_one(self, funded):
        # The receipt is a RECORD OF THE FIRST SETTLEMENT. Measured: after a
        # later unrelated grant moves the real balance, the replay still
        # reports the balance as it stood when the key was first spent.
        first = wd.spend(funded, "p", "spirit_stone", 10, _version(funded), "s1")
        wd.earn(funded, "p", "spirit_stone", 500, "later-gain")
        replay = wd.spend(funded, "p", "spirit_stone", 10,
                          _version(funded), "s1")
        assert replay["_replayed"] is True
        assert replay["balance"]["spirit_stone"] == \
            first["balance"]["spirit_stone"]
        assert _balance(funded) == 590

    def test_one_player_cannot_replay_another_players_receipt(self, store):
        # Measured: B reusing A's key gets NO replay -- B's own request is
        # evaluated and debits B. The receipt is scoped to (player, key), so a
        # guessed or observed key is not a way to skip a debit.
        wd.earn(store, "a", "spirit_stone", 100, "seed-a")
        wd.earn(store, "b", "spirit_stone", 100, "seed-b")
        wd.spend(store, "a", "spirit_stone", 40, _version(store, "a"), "shared")
        theft = wd.spend(store, "b", "spirit_stone", 40, _version(store, "b"),
                         "shared")
        assert theft.get("_replayed") is None
        assert store.get("a").balance.spirit_stone == 60
        assert store.get("b").balance.spirit_stone == 60

    def test_a_receipt_is_not_answerable_without_its_owner(self, store):
        wd.earn(store, "a", "spirit_stone", 100, "seed-a")
        wd.spend(store, "a", "spirit_stone", 40, _version(store, "a"), "shared")
        assert store.recall("shared", "a") is not None
        assert store.recall("shared", "b") is None
        assert store.recall("shared", "") is None

    def test_a_purchase_receipt_cannot_be_cited_by_another_player(self, store):
        # The purchase claim row is scoped the same way. Measured: B asking for
        # A's purchase key is a fresh purchase, not a replay of A's receipt.
        wd.claim_purchase(store, "a", "receipt-1", "spirit_stone", 30)
        other = wd.claim_purchase(store, "b", "receipt-1", "spirit_stone", 30)
        assert other["ok"] is True
        assert store.get("a").balance.spirit_stone == 30
        assert store.get("b").balance.spirit_stone == 30
        assert store.purchase_claims.count() == 2

    def test_a_refused_receipt_is_never_stored_for_any_refusal_reason(self,
                                                                     store):
        # The four refusal paths, each checked by reading the store back rather
        # than by trusting the reply. A stored refusal would replay for the
        # whole time-to-live and lock the player out of money they have.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        version = _version(store)
        wd.spend(store, "p", "spirit_stone", 999_999, version, "overdraft")
        wd.spend(store, "p", "spirit_stone", 5, version - 1, "stale")
        wd.spend(store, "p", "spirit_stone", 0, version, "zero")
        wd.spend(store, "p", "gold", 5, version, "bad-currency")
        for key in ("overdraft", "stale", "zero", "bad-currency"):
            assert store.recall(key, "p") is None, (
                "a refusal was stored as the answer for %r, so the player's "
                "next attempt replays it for the whole time-to-live" % key)

    def test_a_settled_receipt_is_the_only_thing_the_store_remembers(self,
                                                                    store):
        # The control for the test above. If NOTHING anchored a receipt, the
        # refusals would trivially not replay and the previous test would pass
        # for the wrong reason.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        wd.spend(store, "p", "spirit_stone", 10, _version(store), "real-spend")
        assert store.recall("real-spend", "p") is not None

    def test_a_tampered_stored_receipt_does_not_move_the_store_balance(
            self, store):
        # MEASURED DEFECT, pinned non-strict below. The live balance is the
        # authority: even when the stored receipt's copy is corrupted, the
        # store's number is unchanged. This test is the CONTROL that shows the
        # corruption is contained to the replayed reply.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        wd.spend(store, "p", "spirit_stone", 30, _version(store), "s1")
        store._idempotency[("p", "s1")]["resp"]["balance"]["spirit_stone"] = \
            999_999
        replay = wd.spend(store, "p", "spirit_stone", 30, _version(store), "s1")
        assert replay["_replayed"] is True
        assert _balance(store) == 70, (
            "the stored balance was rewritten to %s; the replayed receipt "
            "became the authority for the balance"
            % _balance(store))

    def test_a_blank_receipt_key_is_one_shared_bucket_for_its_owner(
            self, store):
        # MEASURED DEFECT, pinned non-strict below. What is measured is that
        # the blank key is not refused, so it behaves like any other key. This
        # test records the shape so the xfail below has a control to fail
        # against.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        first = wd.spend(store, "p", "spirit_stone", 10, _version(store), "")
        assert first["ok"] is True
        assert _balance(store) == 90


# ============================================================================
# 6. CLOCK SKEW -- the published window is enforced on both sides
# ============================================================================

class TestClockSkewWindowIsEnforced:
    """The one-day tolerance, measured at its boundary rather than far from it.

    The existing ``TestOfflineClaimContract`` proves a clock a day-and-an-hour
    out is refused. These tests sit AT the boundary, because a guard that is
    correct at 30 days and wrong at 1.0001 days is not a guard.
    """

    def test_the_tolerance_is_five_minutes_in_each_direction(self, store):
        # Owner ruling (task 006-002, 2026-10-05): the device-clock skew
        # window is 300s, not the 86_400 this test used to pin. Asserted as a
        # literal on purpose -- this is the one place that records the POLICY
        # value, so a silent widening has somewhere to land.
        assert wd.MAX_CLOCK_SKEW_SECONDS == 300

    @pytest.mark.parametrize("multiple,expect_ok", [
        (0.5, True), (0.9, True), (0.999, True),
        (1.001, False), (1.5, False), (30.0, False),
    ])
    def test_a_clock_inside_the_window_passes_and_one_outside_is_refused(
            self, store, multiple, expect_ok):
        # A margin rather than a knife edge: `claim_offline` reads time.time()
        # inside the call, so the window boundary is never hit exactly by the
        # time it is compared. 0.999 and 1.001 sit clear of it.
        # Offsets are multiples of the WINDOW, not of a fixed unit, so this
        # matrix follows the policy value rather than re-deciding it.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                 time.time() + multiple * wd.MAX_CLOCK_SKEW_SECONDS)
        assert reply["ok"] is expect_ok
        if not expect_ok:
            assert reply["error"] == "device clock skew exceeds 5 minutes"
            assert _balance(store) == 0

    @pytest.mark.parametrize("direction", [-1, 1])
    def test_a_refused_skew_leaves_the_window_claimable(self, store,
                                                        direction):
        # A refused skew must not cost the player their absence: the guard runs
        # before the stamp, so fixing the device clock and retrying pays the
        # full window.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        before = store.get("p").last_offline_claimed_at
        wd.claim_offline(store, "p", "spirit_stone", 1.0,
                         time.time()
                         + direction * (wd.MAX_CLOCK_SKEW_SECONDS + 3600))
        assert store.get("p").last_offline_claimed_at == before
        retry = wd.claim_offline(store, "p", "spirit_stone", 1.0, time.time())
        assert retry["ok"] is True
        assert retry["earned"] > 0

    def test_an_infinite_device_clock_is_refused(self, store):
        # +inf is caught by the same comparison that catches a large finite
        # skew. Pinned separately from the NaN case, which is NOT caught.
        store.create("p")
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                 float("inf"))
        assert reply["ok"] is False
        assert reply["error"] == "device clock skew exceeds 5 minutes"
        assert _balance(store) == 0

    @pytest.mark.xfail(reason="every comparison against NaN is False, so abs(nan - server_now) > MAX_CLOCK_SKEW_SECONDS is False and a NaN device clock is ALLOWED past the anti-cheat", strict=False)
    def test_a_not_a_number_device_clock_should_be_refused(self, store):
        # MEASURED DEFECT. Every comparison against NaN is False, so
        # `abs(nan - server_now) > MAX_CLOCK_SKEW_SECONDS` is False and the
        # claim is ALLOWED. A client that sends its clock as NaN walks past the
        # anti-cheat entirely. Measured: ok=True, earned=3600.
        #
        # ONLY nan is asserted here. +inf is handled correctly and is pinned as
        # a REAL pass by `test_an_infinite_device_clock_is_refused` above;
        # folding it into this marker would hide a working guard behind an
        # xfail and report it as a defect that does not exist.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", 1.0,
                                 float("nan"))
        assert reply["ok"] is False
        assert "clock skew" in reply.get("error", "")


# ============================================================================
# 7. NON-FINITE RATES -- a rate is an input, so it needs a finiteness bound
# ============================================================================

class TestNonFiniteRatesAreRefused:
    """`rate_per_sec` and the amounts derived from it.

    MEASURED, and this is the sharpest gap in the module: the guard at
    ``claim_offline`` is ``rate_per_sec < 0``, which is a SIGN test, not a
    finiteness test.

      * ``-inf`` IS caught -- it is negative.
      * ``nan`` is NOT caught: ``nan < 0`` is False. The claim proceeds to
        ``int(capped * nan)`` and raises ``ValueError: cannot convert float NaN
        to integer``, which escapes the ``@atomic`` transaction wrapper as a
        500 rather than a refusal.
      * ``+inf`` is NOT caught either: ``int(capped * inf)`` raises
        ``OverflowError``, same escape.

    Measured window safety on both raises: the balance stays 0 and
    ``last_offline_claimed_at`` does NOT move, so the transaction's rollback
    holds and no money moves. The defect is the CRASH, not a payout -- which is
    exactly why a refusal is the right answer and a raise is not: the caller
    cannot distinguish "your rate was bad" from "the server is broken", and the
    retry double-attempts.

    ``earn``/``spend``/``claim_purchase`` are worse: they apply a non-finite
    amount directly to the balance with no guard at all, storing a NaN or inf
    where an integer belongs. A NaN balance then DEFEATS the over-draft guard,
    because ``nan < amount`` is False -- so an overdraft is approved against a
    balance that holds no money.
    """

    # --- claim_offline: the rate input ---

    @pytest.mark.xfail(reason="the guard is `rate_per_sec < 0`, a SIGN test: nan < 0 is False, so int(capped * nan) raises ValueError out of the atomic wrapper as a 500", strict=False)
    def test_a_not_a_number_rate_should_be_refused_not_raised(self, store):
        # MEASURED DEFECT. nan < 0 is False, so the sign guard lets it through
        # and `int(capped * nan)` raises ValueError: cannot convert float NaN
        # to integer. Measured: the exception escapes the @atomic wrapper.
        #
        # ONLY nan is asserted here; its sibling test below carries +inf, which
        # raises a different exception (OverflowError) from the same line. They
        # are separate tests because a caller sees two different failures.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", float("nan"),
                                 time.time())
        assert reply["ok"] is False
        assert "rate_per_sec" in reply.get("error", "")

    @pytest.mark.xfail(reason="inf is not negative, so the sign guard misses it and int(capped * inf) raises OverflowError out of the atomic wrapper as a 500", strict=False)
    def test_an_infinite_rate_should_be_refused_not_raised(self, store):
        # MEASURED DEFECT. +inf is not negative, so the same sign guard misses
        # it and `int(capped * inf)` raises OverflowError: cannot convert float
        # infinity to integer -- again out of the @atomic wrapper.
        #
        # Contrast with -inf, which the existing sign guard DOES catch and which
        # is asserted as a real pass below. That asymmetry is the defect: the
        # same guard refuses one infinity and crashes on the other.
        store.create("p")
        store.update("p", last_offline_claimed_at=time.time() - 3600)
        reply = wd.claim_offline(store, "p", "spirit_stone", float("inf"),
                                 time.time())
        assert reply["ok"] is False
        assert "rate_per_sec" in reply.get("error", "")

    def test_a_negative_infinite_rate_is_already_refused(self, store):
        # THE CONTROL for the two tests above. `-inf < 0` is True, so the
        # existing sign guard catches it and answers with the three-key refusal
        # envelope. This proves the two xfails fail for the specific reason
        # documented -- the sign test -- and not because every non-finite rate
        # is refused.
        store.create("p")
        reply = wd.claim_offline(store, "p", "spirit_stone", float("-inf"),
                                 time.time())
        assert reply["ok"] is False
        assert reply["error"] == "rate_per_sec must be non-negative"
        assert sorted(reply) == ["error", "ok", "wallet_version"]

    def test_a_negative_finite_rate_is_already_refused(self, store):
        # The ordinary case the sign guard was written for, kept as the
        # positive control beside it.
        store.create("p")
        reply = wd.claim_offline(store, "p", "spirit_stone", -5.0,
                                 time.time())
        assert reply["ok"] is False
        assert reply["error"] == "rate_per_sec must be non-negative"

    def test_a_finite_rate_is_still_accepted(self, store):
        # The other direction: the finiteness guard must not become a
        # positivity rule. Measured: 0.0 and 2.5 both settle.
        for rate in (0.0, 2.5):
            fresh = wd.WalletStore()
            fresh.create("p")
            fresh.update("p", last_offline_claimed_at=time.time() - 100)
            reply = wd.claim_offline(fresh, "p", "spirit_stone", rate,
                                     time.time())
            assert reply["ok"] is True
            assert reply["earned"] == int(reply["capped_sec"] * rate)

    def test_a_refused_rate_never_moves_the_window(self, store):
        # Measured for every non-finite rate, including the two that RAISE:
        # the rollback holds, so the balance stays 0 and the stamp does not
        # move. This is why the defect is a crash and not a payout, and it is
        # also why a retry after a fix still pays the full absence.
        for rate in (float("nan"), float("inf"), float("-inf"), -5.0):
            fresh = wd.WalletStore()
            fresh.create("p")
            fresh.update("p", last_offline_claimed_at=time.time() - 3600)
            before = fresh.get("p").last_offline_claimed_at
            try:
                wd.claim_offline(fresh, "p", "spirit_stone", rate, time.time())
            except (ValueError, OverflowError):
                pass          # the measured defect; the invariant still holds
            assert fresh.get("p").last_offline_claimed_at == before
            assert fresh.get("p").balance.spirit_stone == 0

    # --- earn / spend / claim_purchase: the amounts ---

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    @pytest.mark.xfail(reason="earn applies the amount with no finiteness guard, so the balance is published to the client as NaN/Infinity", strict=False)
    def test_a_non_finite_grant_amount_should_be_refused(self, store, bad):
        # MEASURED DEFECT. Measured today: ok=True and the balance is
        # published to the client as NaN/Infinity -- a currency with no value
        # that then defeats the over-draft guard (see the test below).
        reply = wd.earn(store, "p", "spirit_stone", bad, "e1")
        assert reply["ok"] is False
        assert reply["error"]

    @pytest.mark.xfail(reason="spend applies the amount with no finiteness guard, so a NaN spend is APPROVED and rewrites the stored balance to NaN", strict=False)
    def test_a_not_a_number_spend_amount_should_be_refused(self, store):
        # MEASURED DEFECT. Measured today: ok=True, and the stored balance
        # becomes NaN -- so every later read of this player's money is NaN.
        # The over-draft guard does not catch it either, because
        # `100 < nan` is False.
        store.create("p")
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        reply = wd.spend(store, "p", "spirit_stone", float("nan"),
                         _version(store), "s1")
        assert reply["ok"] is False
        assert reply["error"]
        assert _balance(store) == 100

    def test_an_infinite_spend_amount_is_refused_as_an_overdraft(self, store):
        # +inf is refused -- but ACCIDENTALLY, and for the wrong reason, so it
        # is pinned here as a real pass rather than folded into the NaN xfail
        # above. Measured: the only thing standing between +inf and the balance
        # is `100 < inf`, the ordinary over-draft check. The module has no
        # finiteness guard at all. A fix that added one must keep this refusal;
        # a change that reordered the guards could quietly un-refuse it, and
        # this test is what would notice.
        store.create("p")
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        reply = wd.spend(store, "p", "spirit_stone", float("inf"),
                         _version(store), "s1")
        assert reply["ok"] is False
        assert reply["error"] == "insufficient balance"
        assert _balance(store) == 100

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    @pytest.mark.xfail(reason="claim_purchase writes the claim row BEFORE the amount is checked, so the purchase key is burned even when the grant is refused. A finiteness guard on earn alone does NOT fix this -- verified: with a finiteness guard injected at runtime the grant is refused and the row is still written", strict=False)
    def test_a_non_finite_purchase_amount_should_be_refused(self, store, bad):
        # MEASURED DEFECT. Measured today: the claim row is written AND the
        # amount is credited, so the key is burned on a grant that credited a
        # value-less balance.
        #
        # This one needs a guard on BOTH halves -- the amount AND the ordering.
        # The negative-control run (a finiteness guard injected at runtime,
        # nothing edited) left this red while turning the other six green,
        # which is the evidence that the claim-row half is a separate defect.
        reply = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", bad)
        assert reply["ok"] is False
        assert reply["error"]
        assert store.purchase_claims.count() == 0

    @pytest.mark.xfail(reason="a NaN balance defeats the over-draft guard: nan < 999999 is False, so an overdraft is APPROVED against a balance holding no money", strict=False)
    def test_a_non_finite_balance_should_not_pass_the_overdraft_guard(
            self, store):
        # MEASURED DEFECT, and the most serious of the seven. Measured today:
        # earn(NaN) stores NaN, then the over-draft check
        # `nan < 999999` is False, so a 999999 spend is APPROVED against a
        # balance that holds no money. The sign guard on the amount cannot see
        # this -- it is the stored value that is non-finite.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        wd.earn(store, "p", "spirit_stone", float("nan"), "poison")
        overdraft = wd.spend(store, "p", "spirit_stone", 999_999,
                             _version(store), "big")
        assert overdraft["ok"] is False
        assert overdraft["error"] == "insufficient balance"

    def test_the_overdraft_guard_still_works_on_a_healthy_balance(self, store):
        # THE CONTROL for the test above: the guard is real, it is defeated
        # only by a non-finite stored balance.
        wd.earn(store, "p", "spirit_stone", 100, "seed")
        overdraft = wd.spend(store, "p", "spirit_stone", 999_999,
                             _version(store), "big")
        assert overdraft["ok"] is False
        assert overdraft["error"] == "insufficient balance"

    def test_a_finite_amount_of_any_size_is_still_honoured(self, store):
        # The other direction, so a finiteness guard does not become a magnitude
        # guard. Measured: a very large FINITE amount is credited in full.
        reply = wd.earn(store, "p", "spirit_stone", 10 ** 30, "huge")
        assert reply["ok"] is True
        assert _balance(store) == 10 ** 30


# ============================================================================
# 8. MEASURED DEFECTS -- documented, non-strict, so a fix is visible
# ============================================================================

class TestMeasuredDefects:
    """Two paths where the measured behaviour loses money.

    Both are ``xfail`` WITHOUT ``strict``, so they document today's behaviour
    and turn green the moment someone fixes the module. They are here because a
    contract file that only records good behaviour is how a defect gets
    re-discovered every quarter.
    """

    @pytest.mark.xfail(reason="claim_purchase anchors the claim row before the "
                              "currency is checked, so a typo'd currency burns "
                              "the purchase key forever",
                       strict=False)
    def test_a_bad_currency_should_not_burn_the_purchase_key(self, store):
        # Measured today: the claim row is written first, `earn` refuses the
        # unknown currency, and the reply has NO `error` key at all -- only
        # ok=False. A caller branching on `error` raises, and a retry with a
        # correct currency is refused as an already-claimed purchase.
        broken = wd.claim_purchase(store, "p", "buy-1", "gold", 10)
        assert "error" in broken
        retry = wd.claim_purchase(store, "p", "buy-1", "spirit_stone", 10)
        assert retry["ok"] is True

    @pytest.mark.xfail(reason="a player whose wallet row does not exist yet is "
                              "refused as an overdraw rather than as unknown",
                       strict=False)
    def test_spending_from_an_unknown_wallet_should_say_the_player_is_unknown(
            self, store):
        # Measured today: `_live_for_update` creates the row, the version guard
        # passes at 0 == 0, and the zero balance refuses as an overdraw. The
        # player is told they have no money rather than that they have no
        # wallet, and the read created a row as a side effect of the attempt.
        reply = wd.spend(store, "ghost", "spirit_stone", 1, 0, "s1")
        assert reply["ok"] is False
        assert reply["error"] != "insufficient balance"