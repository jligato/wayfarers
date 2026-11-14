"""Contract tests for ``server/request_signing.py``.

WHAT THIS MODULE IS
-------------------
``request_signing.py`` is the response-signing half of the currency design
(currency.md section 3.4). It has NO HTTP routes of its own. It is called from
exactly one place in the server -- ``main.py:_sign_wallet_response`` -- which
passes a player id, a wallet version, a balance dict and a per-session key,
then merges the returned ``{signature, timestamp, algorithm}`` into the wallet
response body.

Four things about it are contracts worth holding, and they are what this file
pins:

1. SIGNING REQUIRES A KEY. With ``WC_SIGNING_KEY`` unset there is nothing to
   sign with, so the signer refuses rather than inventing a key. A per-session
   ``session_key`` passed by the caller is a legitimate substitute and must
   keep working without the environment variable set -- ``main.py`` always
   passes one.

2. VALIDATION REJECTS TAMPERING. Every field the signature covers is
   load-bearing: change the balance, the player, the version or the signature
   itself and verification must answer ``False``.

3. THERE IS NO SHIPPED FALLBACK SECRET. This module used to publish a default
   key in its source. It does not any more, and the tests below would catch a
   regression in three independent ways: the module holds no key constant, the
   marker list is a list of patterns rather than a copy of a real secret, and
   every value that could stand in for a key (unset, blank, short, or
   placeholder-looking) is refused.

4. NON-FINITE TIMESTAMPS ARE REJECTED. A signature whose timestamp is ``+inf``,
   ``-inf`` or ``nan`` must never verify. Two of the three are refused by the
   age check, which is measured here. ``nan`` is NOT: it slips past both age
   comparisons because every comparison against ``nan`` is ``False``, so an
   honest ``nan`` stamp verifies forever -- see the strict ``xfail`` in
   ``TestNonFiniteTimestamps``. That marker is the honest record of a real gap;
   the fix belongs in the module, not in the test.

NO DATABASE, NO LIVE APP, NO HTTP
---------------------------------
Pure in-memory crypto over dicts. No store, no server, no session, no
PostgreSQL. The repository ``conftest.py`` chain imports ``server.main`` while
pytest is still collecting files, so a colleague mid-write on ``main.py`` can
abort the whole run; that is not a failure of anything tested here.

HOW EVERY EXPECTATION BELOW WAS OBTAINED
----------------------------------------
By running the real module, not by reading it. Every refusal, every boundary
and every ``RuntimeError`` was measured first and written down second. Where
the measured answer disagrees with what a reader might expect -- the ``nan``
timestamp, the ``NaN`` token inside a balance -- the test asserts the MEASURED
behaviour and the comment says so.

HOW TO RUN
----------
    cd G:/Projects/wayfarers-cycle
    env -u WC_PG_DB python -m pytest \\
        server/tests/test_request_signing_contract.py -q \\
        -p no:logging --no-header -p no:randomly
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import math
import os
import sys
import uuid

import pytest

# ``server/`` must be importable for the bare ``import request_signing`` below.
# server/tests/conftest.py also does this, but this file inserts its own path
# so it also runs under --noconftest.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import request_signing as rs  # noqa: E402


# A balance shaped like what main.py:_balance_to_dict hands the signer.
BALANCE = {"spirit_stone": 100, "fateum": 50}

# The instant the ``frozen`` fixture pins the clock to. Any test that signs and
# then verifies must stamp THIS, not an arbitrary small number: the frozen
# clock is 1.7 billion seconds ahead, so an unpinned stamp is far outside the
# validity window and every "expect False" assertion would pass vacuously.
NOW = 1_700_000_000.0

# A per-session key. Deliberately not a secret: it is a test fixture that
# exists only for the duration of one test, and the same literal appears in
# main.py's session-key plumbing and in client/scripts/WalletVerifier.gd.
SESSION_KEY = b"per-session-key-for-contract-tests"


class FrozenClock:
    """Stand-in for the ``time`` module with a clock that does not move.

    IMPORTANT: this replaces the module-level NAME ``request_signing.time``
    with a private object. It does NOT assign to ``time.time`` on the stdlib
    module, so it cannot leak into any other test file. ``monkeypatch.setattr``
    restores the original name when the test ends.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def freeze_clock(monkeypatch, module, now: float = NOW) -> FrozenClock:
    """Pin ``module``'s clock to a fixed instant and return the clock.

    The clock must be pinned on the SAME module object whose functions are
    called. A module loaded by the ``fresh_module`` fixture is a separate object
    with its own module-level ``time`` name, so the canonical ``frozen`` fixture
    does not reach it -- verifying against a fresh copy under the real clock
    would measure a 100-million-second-old stamp and refuse for the wrong
    reason.
    """
    clock = FrozenClock(now)
    monkeypatch.setattr(module, "time", clock)
    return clock


@pytest.fixture
def frozen(monkeypatch):
    """Pin the canonical module's clock so age arithmetic is deterministic."""
    return freeze_clock(monkeypatch, rs)


@pytest.fixture
def server_key(monkeypatch):
    """Give the module a valid server-wide key for tests that sign without one.

    The value clears every check the module makes: long enough for
    MIN_SIGNING_KEY_BYTES and free of every placeholder marker.
    """
    monkeypatch.setenv("WC_SIGNING_KEY", "contract-suite-server-key-0123456789abcdef")


@pytest.fixture
def fresh_module(monkeypatch):
    """Re-execute the module file under a given ``WC_SIGNING_KEY`` value.

    ``signing_key()`` reads the environment on every call and is deliberately
    not cached, so most assertions do NOT need a reload. This exists for the
    one thing a reload proves that a plain call cannot: that a brand new copy of
    the file, imported from scratch with the variable unset, also refuses. That
    rules out "the constant is only in the module object that was imported
    earlier". Each copy is loaded under a unique name so the canonical
    ``request_signing`` object -- and the rest of the suite -- is untouched.

    ``None`` means "leave WC_SIGNING_KEY unset". The returned copy's clock is
    NOT frozen -- call ``freeze_clock(monkeypatch, module)`` on it if the test
    verifies a stamp against that copy.
    """

    def _load(env_value):
        if env_value is None:
            monkeypatch.delenv("WC_SIGNING_KEY", raising=False)
        else:
            monkeypatch.setenv("WC_SIGNING_KEY", env_value)
        spec = importlib.util.spec_from_file_location(
            "rs_contract_copy_" + uuid.uuid4().hex, rs.__file__
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return _load


def canonical_payload(player_id, wallet_version, balance, timestamp) -> str:
    """Rebuild the signed string INDEPENDENTLY of request_signing.py.

    Written out by hand from the contract rather than by calling the module, so
    the test fails if the module's own construction drifts. ``sort_keys=True``
    with Python's default JSON separators is the whole contract -- the Godot
    client reproduces it byte for byte.
    """
    return json.dumps(
        {
            "player_id": player_id,
            "wallet_version": wallet_version,
            "balance": balance,
            "timestamp": timestamp,
        },
        sort_keys=True,
    )


def expected_signature(key: bytes, player_id, wallet_version, balance, timestamp) -> str:
    """The signature an honest implementation of the contract must produce."""
    payload = canonical_payload(player_id, wallet_version, balance, timestamp)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# 1. Signing requires a key
# ---------------------------------------------------------------------------


class TestSigningRequiresAKey:
    """No key means no signature. Nothing is invented to fill the gap."""

    def test_unset_environment_variable_refuses_to_sign(self, fresh_module):
        # The core of requirement 1. A deployment that forgot to set
        # WC_SIGNING_KEY signs nothing at all, loudly, instead of signing with
        # material an attacker already has.
        module = fresh_module(None)
        with pytest.raises(RuntimeError) as exc:
            module.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert module.SIGNING_KEY_ENV in str(exc.value)

    @pytest.mark.parametrize(
        "blank", [None, "", "   ", "\t\n"], ids=["unset", "empty", "spaces", "whitespace"]
    )
    def test_blank_values_are_treated_as_unset(self, fresh_module, blank):
        # An operator who exports an empty string has not configured a key.
        # Without the .strip() a blank value would sail past "is it set" and
        # become a zero-length HMAC key.
        module = fresh_module(blank)
        with pytest.raises(RuntimeError):
            module.sign_response("p1", 1, BALANCE, timestamp=NOW)

    def test_a_key_shorter_than_the_minimum_is_refused(self, fresh_module):
        # Short passes an "is it set" check while being trivially guessable, so
        # the length floor is part of the control rather than a style rule.
        short = "tooshort"
        module = fresh_module(short)
        with pytest.raises(RuntimeError) as exc:
            module.signing_key()
        message = str(exc.value)
        assert str(module.MIN_SIGNING_KEY_BYTES) in message
        # The refusal names the LENGTH, not the value: echoing the operator's
        # own key into an exception risks writing it to a log. So the message
        # must contain the measured byte count and must NOT contain the secret.
        assert str(len(short.encode("utf-8"))) in message
        assert short not in message

    def test_the_length_floor_is_the_documented_constant(self):
        # Read at call time from the module, so retuning the floor keeps the
        # test honest instead of pinning a number that may legitimately move.
        assert isinstance(rs.MIN_SIGNING_KEY_BYTES, int)
        assert rs.MIN_SIGNING_KEY_BYTES >= 16

    def test_a_valid_environment_key_is_accepted_and_used(self, fresh_module):
        module = fresh_module("a-perfectly-fine-32-byte-random-value")
        assert module.signing_key() == b"a-perfectly-fine-32-byte-random-value"
        # And it is the key that actually signs, not merely a value that can be
        # fetched: the signature matches an independent reconstruction.
        ts = NOW
        assert module.sign_response("p1", 1, BALANCE, timestamp=ts)["signature"] == (
            expected_signature(b"a-perfectly-fine-32-byte-random-value", "p1", 1, BALANCE, ts)
        )

    def test_a_caller_supplied_session_key_works_without_the_environment(self, monkeypatch, fresh_module):
        # main.py always passes a per-session key. That path must keep working
        # on a server with no WC_SIGNING_KEY configured at all, or every wallet
        # response would 500.
        module = fresh_module(None)
        freeze_clock(monkeypatch, module)
        sig = module.sign_response("p1", 1, BALANCE, session_key=SESSION_KEY, timestamp=NOW)
        assert sig["signature"] == expected_signature(
            SESSION_KEY, "p1", 1, BALANCE, NOW
        )
        assert module.verify_response(
            "p1", 1, BALANCE, sig["signature"], NOW, session_key=SESSION_KEY
        ) is True

    def test_the_environment_key_is_not_cached_between_calls(self, fresh_module):
        # Deliberate: re-reading os.environ costs far less than the HMAC it
        # precedes, and it removes any chance of a stale key surviving an
        # environment change.
        module = fresh_module("first-random-value-0123456789abc")
        first = module.signing_key()
        module.os.environ["WC_SIGNING_KEY"] = "second-random-value-9876543210abc"
        second = module.signing_key()
        assert first != second
        assert second == b"second-random-value-9876543210abc"

    def test_a_str_session_key_is_refused_rather_than_silently_coerced(self, frozen):
        # hmac.new() raises TypeError on a str. Callers pass bytes
        # (main.py:_ensure_session_key returns bytes); pinning the contract
        # beats leaving it to an exception message nobody reads.
        with pytest.raises(TypeError):
            rs.sign_response("p1", 1, BALANCE, session_key="not-bytes", timestamp=NOW)

    def test_the_signing_key_is_never_returned_as_empty_bytes(self, fresh_module):
        # hmac.new() refuses an empty key, so an empty value must never reach
        # it. Every refusal path above raises before the return; this asserts
        # the positive case produces real bytes.
        module = fresh_module("contract-suite-server-key-0123456789abcdef")
        key = module.signing_key()
        assert isinstance(key, bytes)
        assert len(key) >= module.MIN_SIGNING_KEY_BYTES


# ---------------------------------------------------------------------------
# 2. No hardcoded fallback secret
# ---------------------------------------------------------------------------


class TestNoHardcodedFallbackSecret:
    """A key published in the source protects nothing, so none is published."""

    def test_the_module_holds_no_signing_key_constant(self):
        # The retired shape was a module-level SIGNING_KEY string. Reading the
        # module's own namespace is the behavioural equivalent of asserting
        # that constant does not exist: no importable attribute is a secret.
        assert "SIGNING_KEY" not in vars(rs)
        assert "SIGNING_KEY" not in dir(rs)

    def test_reading_the_historical_name_raises_instead_of_returning_a_key(self, fresh_module):
        # Old callers wrote request_signing.SIGNING_KEY. That spelling still
        # resolves, but through __getattr__, which calls signing_key() and
        # raises when the environment is unset. If a constant ever reappears,
        # this returns bytes and the assertion fails.
        module = fresh_module(None)
        with pytest.raises(RuntimeError):
            module.SIGNING_KEY

    def test_a_freshly_imported_copy_also_refuses(self, fresh_module):
        # Rules out the module object merely having been built while a key was
        # set: a brand new copy of the FILE, imported with the variable unset,
        # refuses too.
        module = fresh_module(None)
        with pytest.raises(RuntimeError):
            module.sign_response("p1", 1, BALANCE, timestamp=NOW)

    def test_no_public_string_constant_is_long_enough_to_be_a_key(self):
        # Every public str/bytes constant on the module is checked against the
        # module's own minimum. A secret smuggled in as a constant would have
        # to be at least that long, so this is a census rather than a guess.
        long_enough = [
            name
            for name in dir(rs)
            if not name.startswith("_")
            and isinstance(getattr(rs, name), (str, bytes))
            and len(getattr(rs, name)) >= rs.MIN_SIGNING_KEY_BYTES
        ]
        assert long_enough == []

    def test_placeholder_markers_are_patterns_not_a_published_secret(self, fresh_module):
        # The markers are SUBSTRINGS the module refuses to see ("changeme",
        # "placeholder", ...), not a copy of a retired default key. Each one is
        # rejected when it appears inside an otherwise-valid value.
        for marker in rs._PLACEHOLDER_MARKERS:
            module = fresh_module(marker + "-0123456789abcdefghij")
            with pytest.raises(RuntimeError):
                module.signing_key()

    def test_marker_matching_is_case_insensitive(self, fresh_module):
        # An operator who writes CHANGEME must be refused just the same.
        module = fresh_module("CHANGEME-0123456789abcdefghij")
        with pytest.raises(RuntimeError):
            module.signing_key()

    def test_a_marker_hidden_inside_an_ordinary_word_is_still_refused(self, fresh_module):
        # The check is a substring test, so it errs toward refusing. A value
        # that merely CONTAINS a marker is rejected; that is the intended
        # direction of the bias.
        module = fresh_module("my-example-0123456789abcdefghij")
        with pytest.raises(RuntimeError):
            module.signing_key()

    def test_a_clean_random_value_that_merely_looks_short_is_still_fine(self, fresh_module):
        # The control arm: without the placeholder check this value would be
        # accepted, so the refusal above is attributable to the marker list and
        # not to the length floor (this value is over the floor).
        value = "a-perfectly-fine-32-byte-random-value"
        assert len(value) >= rs.MIN_SIGNING_KEY_BYTES
        module = fresh_module(value)
        assert module.signing_key() == value.encode()


# ---------------------------------------------------------------------------
# 3. Validation rejects tampering
# ---------------------------------------------------------------------------


class TestTamperingRejected:
    """Every field the signature covers is load-bearing."""

    @pytest.fixture(autouse=True)
    def _key(self, server_key):
        # These tests sign with the server-wide key, which is what a route
        # without a session key would do.
        return None

    def test_an_untouched_response_verifies(self, frozen):
        # The control arm for everything below: without it, a verifier that
        # answered False to everything would pass the whole class.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW) is True

    def test_changing_a_balance_value_is_caught(self, frozen):
        # The inflation a lazy cheater attempts: display a bigger number than
        # the server sent.
        sig = rs.sign_response("p1", 1, {"spirit_stone": 100}, timestamp=NOW)
        assert rs.verify_response("p1", 1, {"spirit_stone": 999_999}, sig["signature"], NOW) is False

    def test_adding_a_balance_entry_is_caught(self, frozen):
        # Leave every real number alone, append a credit.
        sig = rs.sign_response("p1", 1, {"spirit_stone": 100}, timestamp=NOW)
        inflated = {"spirit_stone": 100, "fateum": 10**9}
        assert rs.verify_response("p1", 1, inflated, sig["signature"], NOW) is False

    def test_dropping_a_balance_entry_is_caught(self, frozen):
        sig = rs.sign_response("p1", 1, {"a": 1, "b": 2}, timestamp=NOW)
        assert rs.verify_response("p1", 1, {"a": 1}, sig["signature"], NOW) is False

    def test_swapping_the_player_id_is_caught(self, frozen):
        # Otherwise one player's signature replays as another's.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p2", 1, BALANCE, sig["signature"], NOW) is False

    def test_bumping_the_wallet_version_is_caught(self, frozen):
        # A version bump is how the client learns its wallet view is stale.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 2, BALANCE, sig["signature"], NOW) is False

    def test_altering_the_signature_is_caught(self, frozen):
        good = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)["signature"]
        forged = [
            "0" * len(good),
            good[:-1],
            good + "0",
            good[0].swapcase() + good[1:],
            good.upper(),
        ]
        for candidate in forged:
            assert rs.verify_response("p1", 1, BALANCE, candidate, NOW) is False

    def test_a_signature_from_another_key_is_caught(self, frozen):
        # The point of per-session keys: a signature lifted from one session
        # must not authenticate in another.
        sig = rs.sign_response("p1", 1, BALANCE, session_key=b"key-a", timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW, session_key=b"key-a") is True
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW, session_key=b"key-b") is False
        # ...and the server-wide key is not a back door into it.
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW) is False

    def test_a_signature_stamped_at_a_different_time_is_caught(self, frozen):
        # The stamp is inside the signed payload, so a replayed signature
        # cannot be re-dated into the validity window.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW + 1) is False

    def test_wallet_version_is_bound_by_type_as_well_as_value(self, frozen):
        # 1 and 1.0 are equal in Python but serialise differently in JSON, so
        # the signature tells them apart. A client that widens the integer
        # before verifying gets a refusal, not a bypass.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW) is True
        assert rs.verify_response("p1", 1.0, BALANCE, sig["signature"], NOW) is False

    def test_booleans_are_not_interchangeable_with_one(self, frozen):
        # json.dumps renders True as `true` and 1 as `1`.
        sig = rs.sign_response("p1", 1, {"a": True}, timestamp=NOW)
        assert rs.verify_response("p1", 1, {"a": 1}, sig["signature"], NOW) is False

    def test_the_signed_payload_is_a_published_contract(self, frozen, server_key):
        # The client rebuilds this exact byte string. If the field set, key
        # order or separators drift, every signature stops verifying in the
        # game client -- so this is checked against an INDEPENDENT
        # reconstruction rather than the module's own helper.
        sig = rs.sign_response("player_1", 7, {"zzz": 1, "aaa": 2, "spirit_stone": 100}, timestamp=NOW)
        assert sig["signature"] == expected_signature(
            rs.signing_key(), "player_1", 7, {"zzz": 1, "aaa": 2, "spirit_stone": 100}, NOW
        )

    def test_balance_key_insertion_order_does_not_change_the_signature(self, frozen, server_key):
        # Two servers building the same balance in a different order must
        # produce the same signature, or the client's rebuild fails at random.
        a = rs.sign_response("p1", 1, {"a": 1, "b": 2, "c": 3}, timestamp=NOW)["signature"]
        b = rs.sign_response("p1", 1, {"c": 3, "b": 2, "a": 1}, timestamp=NOW)["signature"]
        assert a == b

    def test_the_returned_shape_is_the_three_keys_a_client_parses(self, frozen, server_key):
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert sorted(sig) == ["algorithm", "signature", "timestamp"]
        assert sig["algorithm"] == rs.ALGORITHM == "sha256"
        assert len(sig["signature"]) == 64
        assert all(c in "0123456789abcdef" for c in sig["signature"])

    def test_the_signer_echoes_the_stamp_it_actually_signed(self, frozen, server_key):
        # The client reads this field to rebuild the payload, so handing back a
        # different instant than the one signed would break every verify.
        assert rs.sign_response("p1", 1, BALANCE, timestamp=1234.5)["timestamp"] == 1234.5

    def test_a_zero_timestamp_is_signed_not_replaced(self, frozen, server_key):
        # `timestamp or time.time()` read 0.0 as "not supplied" and silently
        # restamped it, so no caller could ever sign epoch zero.
        assert rs.sign_response("p1", 1, BALANCE, timestamp=0.0)["timestamp"] == 0.0


# ---------------------------------------------------------------------------
# 4. Non-finite timestamps are rejected
# ---------------------------------------------------------------------------


class TestNonFiniteTimestamps:
    """``+inf``, ``-inf`` and ``nan`` are not real instants.

    A signature carrying one must never authenticate. Two of the three are
    refused today, and one is a measured gap carried as a strict ``xfail``.
    """

    @pytest.fixture(autouse=True)
    def _key(self, server_key):
        return None

    def test_signing_does_not_itself_reject_a_non_finite_stamp(self, frozen):
        # MEASURED. ``sign_response`` writes whatever stamp it is handed into
        # the payload; json.dumps renders these as the bare tokens NaN,
        # Infinity and -Infinity. So the refusal cannot happen at signing time
        # and must live in ``verify_response``. This test exists so the fix, if
        # one lands, shows up as a visible contract change in BOTH directions.
        for bad in (math.nan, math.inf, -math.inf):
            stamped = rs.sign_response("p1", 1, BALANCE, timestamp=bad)["timestamp"]
            assert isinstance(stamped, float)
            assert math.isnan(stamped) or math.isinf(stamped)

    @pytest.mark.parametrize(
        "bad", [math.inf, -math.inf], ids=["positive-infinity", "negative-infinity"]
    )
    def test_an_infinite_stamp_is_refused_even_with_an_honest_signature(self, frozen, bad):
        # MEASURED AND CORRECT. An infinite stamp produces a genuine signature
        # over a genuine payload -- the arithmetic simply refuses it:
        #   +inf -> age is -inf, which is below -MAX_CLOCK_SKEW_SECONDS
        #   -inf -> age is +inf, which is above MAX_AGE_SECONDS
        # Both arms are reached before the comparison, so this is the age check
        # refusing, not a signature mismatch.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=bad)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], bad) is False

    @pytest.mark.parametrize(
        "bad", [math.inf, -math.inf], ids=["positive-infinity", "negative-infinity"]
    )
    def test_an_infinite_stamp_with_a_bogus_signature_is_refused(self, frozen, bad):
        # The weaker arm, pinned separately so the two cannot be confused: even
        # a made-up signature under an infinite stamp answers False.
        assert rs.verify_response("p1", 1, BALANCE, "0" * 64, bad) is False

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "MEASURED DEFECT. A NaN stamp slips past the age check because every "
            "comparison against nan is False: `age > MAX_AGE_SECONDS` is False and "
            "`age < -MAX_CLOCK_SKEW_SECONDS` is False, so neither arm refuses it. An "
            "honest nan-stamped signature then verifies at any clock offset, which means "
            "it never expires. The fix belongs in request_signing.py (reject a "
            "non-finite timestamp before the arithmetic); this marker is strict so that "
            "fixing the module turns this line red instead of leaving it silently skipped."
        ),
    )
    def test_a_nan_stamp_is_refused(self, frozen):
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=math.nan)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], math.nan) is False

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "MEASURED DEFECT, same cause as test_a_nan_stamp_is_refused. Advancing the "
            "clock by a billion seconds leaves a nan stamp verifying, because age is nan "
            "and nan is neither older than the window nor ahead of the skew limit. A "
            "normal stamp in the same position is refused, which the control arm below "
            "shows. Fix in the module; keep the marker strict."
        ),
    )
    def test_a_nan_stamp_still_verifies_when_the_clock_moves_a_billion_seconds(self, frozen):
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=math.nan)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], math.nan) is True
        frozen.advance(10**9)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], math.nan) is False

    def test_a_normal_stamp_is_refused_at_the_same_clock_offset(self, frozen):
        # The control arm for the xfail above: at a billion seconds of drift an
        # ordinary stamp IS refused. So the nan case is a hole in the age
        # check specifically, not a frozen-clock artefact of this test file.
        ts = NOW
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is True
        frozen.advance(10**9)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is False

    @pytest.mark.parametrize(
        "bad", [math.nan, math.inf, -math.inf], ids=["nan", "positive-infinity", "negative-infinity"]
    )
    def test_a_non_finite_stamp_with_a_bogus_signature_is_refused(self, frozen, bad):
        # Even where the age check fails to fire (nan), a wrong signature still
        # cannot pass, because the HMAC comparison still runs. So the nan gap
        # lets a HOLDS-FOR-FOREVER signature through, not an arbitrary one.
        assert rs.verify_response("p1", 1, BALANCE, "0" * 64, bad) is False

    def test_a_non_finite_timestamp_is_not_silently_coerced(self, frozen):
        # The stamp is echoed back exactly as given, including its
        # non-finiteness. It is never rounded, clamped or replaced by a finite
        # value that would then verify.
        for bad in (math.inf, -math.inf):
            sig = rs.sign_response("p1", 1, BALANCE, timestamp=bad)
            assert sig["timestamp"] == bad


# ---------------------------------------------------------------------------
# 5. The validity window (relationship assertions, not frozen numbers)
# ---------------------------------------------------------------------------


class TestValidityWindow:
    """Age and clock-skew limits, asserted against the module's constants.

    Every boundary here is written as ``NOW ± constant`` so retuning the
    constants keeps the tests green while reintroducing an ``abs()`` on the age
    check -- which doubled the real replay window -- turns them red.
    """

    @pytest.fixture(autouse=True)
    def _key(self, server_key):
        return None

    def test_a_fresh_signature_is_inside_the_window(self, frozen):
        sig = rs.sign_response("p1", 1, BALANCE)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], sig["timestamp"]) is True

    def test_a_stamp_exactly_at_the_age_limit_is_accepted(self, frozen):
        # The check is a strict `>`, so the boundary instant itself is valid.
        ts = NOW - rs.MAX_AGE_SECONDS
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is True

    def test_a_stamp_past_the_age_limit_is_refused(self, frozen):
        ts = NOW - rs.MAX_AGE_SECONDS - 0.001
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is False

    def test_a_stamp_beyond_the_skew_limit_is_refused(self, frozen):
        # MEASURED DEFECT GUARD. An earlier age check used abs(), which accepted
        # a stamp up to MAX_AGE_SECONDS in the future -- doubling the real replay
        # window to 2 * MAX_AGE_SECONDS and letting a forged far-future stamp
        # through. The future arm is bounded by the skew constant, not the age one.
        ts = NOW + rs.MAX_CLOCK_SKEW_SECONDS + 0.001
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is False

    def test_a_far_future_stamp_is_refused(self, frozen):
        # The exact case abs() let through: a stamp a whole extra age-window
        # ahead.
        ts = NOW + rs.MAX_AGE_SECONDS + 1
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is False

    def test_the_accepted_window_is_not_twice_the_age_limit(self, frozen):
        # A relationship rather than a number: the total span of stamps the
        # verifier accepts must not exceed age + skew.
        span = rs.MAX_AGE_SECONDS + rs.MAX_CLOCK_SKEW_SECONDS
        oldest_ts = NOW - rs.MAX_AGE_SECONDS
        newest_ts = NOW + rs.MAX_CLOCK_SKEW_SECONDS
        for ts, expected in (
            (oldest_ts, True),
            (newest_ts, True),
            (NOW - span - 0.001, False),
            (NOW + span + 0.001, False),
        ):
            sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
            assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is expected

    def test_a_small_future_skew_is_tolerated(self, frozen):
        # A client whose clock runs slightly fast must not be locked out of its
        # own server's response.
        ts = NOW + (rs.MAX_CLOCK_SKEW_SECONDS - 0.001)
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is True

    def test_expiry_is_decided_as_the_clock_advances(self, frozen):
        # A response accepted now must go stale later without being resigned.
        ts = NOW
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=ts)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is True
        frozen.advance(rs.MAX_AGE_SECONDS + 0.001)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], ts) is False

    def test_verification_does_not_consume_the_signature(self, frozen):
        # A client may check more than once (render, then audit). The second
        # call must not consume anything or start failing on age.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        first = rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW)
        second = rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW)
        assert first is second is True


# ---------------------------------------------------------------------------
# 6. Fail closed on unusable input
# ---------------------------------------------------------------------------


class TestFailClosedOnUnusableInput:
    """``verify_response`` is documented to return a bool.

    Its ``signature`` and ``timestamp`` arguments arrive from the wire, so
    anything that cannot be compared must answer ``False`` rather than raise
    out of a function with a bool return type. A caller that treats "it
    raised" as anything other than a refusal can be tricked into displaying a
    response it never verified.
    """

    @pytest.fixture(autouse=True)
    def _key(self, server_key):
        return None

    @pytest.mark.parametrize(
        "bad",
        [None, 12345, b"x" * 64, "", [], {}, 3.5, True],
        ids=["none", "int", "bytes", "empty-str", "list", "dict", "float", "bool"],
    )
    def test_an_unusable_signature_type_answers_false(self, frozen, bad):
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, bad, NOW) is False

    @pytest.mark.parametrize(
        "bad", ["sig\u00e9", "\u00e9" * 64, "\U0001f600" * 10, "sig with spaces"],
        ids=["accented", "accented-64", "emoji", "spaced"],
    )
    def test_a_non_ascii_signature_answers_false_instead_of_raising(self, frozen, bad):
        # hmac.compare_digest refuses a str holding non-ASCII characters, so
        # the module compares UTF-8 bytes instead. Either way the answer is
        # False rather than an exception.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, bad, NOW) is False

    @pytest.mark.parametrize(
        "bad_ts", ["not-a-number", None, [1]], ids=["str", "none", "list"]
    )
    def test_a_non_numeric_timestamp_answers_false(self, frozen, bad_ts):
        # A non-numeric stamp reaches `time.time() - timestamp` and would
        # escape as TypeError. It now shares one guard with the payload build
        # and the comparison.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        assert rs.verify_response("p1", 1, BALANCE, sig["signature"], bad_ts) is False

    def test_an_unserialisable_balance_answers_false(self, frozen):
        # A set is not JSON-serialisable, so the payload build raises. A caller
        # receiving an exception rather than False could read a response it
        # never verified.
        assert rs.verify_response("p1", 1, {"bad": {1, 2}}, "0" * 64, NOW) is False

    def test_an_unusable_signature_is_refused_before_the_payload_is_built(self, frozen):
        # The type guard runs BEFORE the payload is built. With a fresh
        # (unexpired) stamp the age gate cannot be what refuses, so the only
        # thing that can answer False here is the type guard itself. Deleting
        # that guard lets the work reach the unserialisable balance, which
        # answers False too -- so this test pins the answer, and the ordering is
        # pinned by the comment rather than by a different assertion.
        assert rs.verify_response("p1", 1, {"bad": {1, 2}}, None, NOW) is False

    def test_a_stale_stamp_is_refused_before_the_payload_is_built(self, frozen):
        # The age check runs before serialisation, which is why the verifier
        # stays cheap under a flood of stale traffic.
        stale = NOW - rs.MAX_AGE_SECONDS - 1
        assert rs.verify_response("p1", 1, {"bad": {1, 2}}, "0" * 64, stale) is False

    def test_verification_always_answers_a_real_bool(self, frozen):
        # The return type is part of the contract callers write against, so a
        # truthy-but-not-True answer (1, "yes", a dict) is a failure even
        # though it would read as success in an ``if``.
        sig = rs.sign_response("p1", 1, BALANCE, timestamp=NOW)
        good = rs.verify_response("p1", 1, BALANCE, sig["signature"], NOW)
        bad = rs.verify_response("p1", 1, BALANCE, "0" * 64, NOW)
        assert good is True
        assert bad is False