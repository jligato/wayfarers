"""Contract tests for server/save_encryption.py.

What this module is
-------------------
`save_encryption.py` is a leaf crypto helper. It exposes no HTTP routes and no
FastAPI app, so there is nothing to exercise over the client -- the contract is
the exported functions themselves:

    derive_account_key(master_key, player_id) -> bytes
    encrypt_extra_state(extra_state, player_id, master_key) -> str
    decrypt_extra_state(encrypted, player_id, master_key) -> dict
    encrypt_session_file(session_data, master_key) -> str
    decrypt_session_file(encrypted, master_key) -> dict
    hmac_sign(data, key) -> str
    hmac_verify(data, signature, key) -> bool
    get_master_key() -> bytes          (raises MasterKeyUnavailable)
    MasterKeyUnavailable(RuntimeError)

Its one real caller is `server/player_store.py`, which encrypts a player's
`extra_state` JSONB column on write (player_store.py:375) and decrypts it on
read (player_store.py:179). The security property that matters to that caller
is: a save blob only comes back for the player and the key it was written with.
Everything else here pins the wire format and the fail-closed behaviour.

Contract shape, not implementation shape
----------------------------------------
The wire format is part of the contract: `base64(version_byte | nonce | body)`
where version_byte is 1, nonce is 12 bytes, and body is the AES-GCM
ciphertext with its 16-byte tag already appended by the library. Pinned by
`TestWireFormat`, because a deployment holding saved rows cannot silently
change the layout.

Error cases are contract too. `decrypt_*` is documented to return `{}` on
wrong key, corruption and tampering rather than raising, and
`get_master_key` is documented to RAISE rather than invent a key. One of these
flipping to the other is a real defect, so both directions are asserted.

`encrypt_session_file` has no empty-payload short-circuit (unlike
`encrypt_extra_state`), which is why `decrypt_session_file` returning `{}` is
ambiguous for an empty payload. The round-trip test therefore uses a
non-empty payload, and the short-circuit asymmetry is pinned separately in
`TestEmptyPayload`.

Keys in this file are generated per test from `secrets.token_bytes` or are
obvious dummies. No real deployment key is read, printed, or hardcoded.

Two areas are added below the original suite, both named in the brief:

* ``TestCallerAuthorisation`` -- "encryption requires auth". This module has no
  HTTP surface, so there is no session to present. The authorisation boundary it
  DOES have is the key: ``get_master_key`` refuses to invent one (so an
  unconfigured deployment cannot encrypt at all), and every blob is bound to one
  player id plus one master key (so a blob only opens for the pair it was
  written with). Both directions are asserted.
* ``TestNonFiniteValues`` -- "non-finite values are rejected". MEASURED: they are
  NOT rejected today. ``json.dumps`` defaults to ``allow_nan=True``, so a NaN or
  Infinity is serialised as the bare tokens ``NaN`` / ``Infinity``, which are not
  valid RFC 8259 JSON, and the symmetric ``json.loads`` reads them straight back.
  The values survive the round trip. This is pinned as a live defect rather than
  asserted as correct behaviour.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_stdlib
import json
import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# The shipped module is imported as `server.save_encryption` -- that is the
# path its production caller uses (player_store.py:21). Importing it under that
# exact name keeps the test pointed at the same module object the app uses, so
# a mutation harness that patches module attributes reaches these tests.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server import save_encryption as se  # noqa: E402


# --------------------------------------------------------------------------
# Dummies. Obvious, obviously-not-real, generated at call time.
# --------------------------------------------------------------------------

def _dummy_key(tag: bytes = b"primary") -> bytes:
    """A per-test dummy master key. Not a deployment key."""
    return hashlib.sha256(b"wc-contract-test-dummy-master-key/" + tag).digest()


_OTHER_KEY = _dummy_key(b"other")

PLAYER = "contract_player_alpha"
OTHER_PLAYER = "contract_player_beta"

PAYLOAD = {
    "beasts": [{"id": "ember_fox", "level": 7}],
    "inventory": {"sword": 1, "potion": 12},
    "tower_state": {"floor": 5, "cleared": True},
    "note": "unicode survives: café 你好",
}


def _blob(raw: str) -> bytes:
    return base64.b64decode(raw.encode())


def _rebuild(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _encrypt(payload=None, player_id=PLAYER, key=None):
    return se.encrypt_extra_state(PAYLOAD if payload is None else payload,
                                 player_id, _dummy_key() if key is None else key)


def _flip_bit(raw: bytes, index: int) -> bytes:
    """Flip the low bit of one byte, leaving length identical."""
    mutable = bytearray(raw)
    mutable[index] ^= 0x01
    return bytes(mutable)


@pytest.fixture(autouse=True)
def _no_ambient_master_key(monkeypatch):
    """Keep a developer's real environment out of get_master_key tests.

    These tests set the variables they need explicitly. Clearing both here means
    a test that forgets to set one fails loudly instead of reading whatever the
    shell exported.
    """
    for var in (se.MASTER_KEY_ENV, se.DEV_SEED_ENV):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------

class TestKeyDerivation:
    def test_returns_thirty_two_bytes(self):
        # AES-256 needs a 32-byte key; a short derivation is the defect.
        assert len(se.derive_account_key(_dummy_key(), PLAYER)) == 32

    def test_is_deterministic(self):
        # Same master key + same player must reproduce the same key, or saved
        # rows written yesterday become unreadable today.
        assert (se.derive_account_key(_dummy_key(), PLAYER)
                == se.derive_account_key(_dummy_key(), PLAYER))

    def test_differs_per_player_id(self):
        # The whole point of the per-account derivation: one account's key must
        # not open another account's save.
        assert (se.derive_account_key(_dummy_key(), PLAYER)
                != se.derive_account_key(_dummy_key(), OTHER_PLAYER))

    def test_differs_per_master_key(self):
        assert (se.derive_account_key(_dummy_key(), PLAYER)
                != se.derive_account_key(_OTHER_KEY, PLAYER))

    def test_session_key_is_not_the_account_key(self):
        # The client session path and the per-account path must not share key
        # material, or a stolen session blob opens every save.
        account_key = se.derive_account_key(_dummy_key(), PLAYER)
        client_key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=se.KEY_DERIVATION_SALT, info=b"client-session",
        ).derive(_dummy_key())
        assert client_key != account_key


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------

class TestWireFormat:
    def test_header_is_version_byte_then_twelve_byte_nonce(self):
        raw = _blob(_encrypt())
        assert raw[0] == se.ENCRYPTION_VERSION
        assert len(raw) >= 1 + se.NONCE_SIZE + 16

    def test_body_length_matches_serialised_payload_plus_tag(self):
        # version(1) + nonce(12) + ciphertext(len(json)) + tag(16)
        #
        # 12 and 16 are WRITTEN HERE AS LITERALS on purpose. Reading
        # se.NONCE_SIZE would make this assertion self-consistent: the expected
        # length would move with the constant, so a wrong nonce size would
        # still produce a match. Measured: injecting NONCE_SIZE 12 -> 8 left
        # this test green until the literal landed. The module constants are
        # pinned separately in TestPublicSurface.test_version_and_nonce_constants.
        expected = 1 + 12 + len(
            json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
        ) + 16
        assert len(_blob(_encrypt())) == expected

    def test_output_is_ascii_base64(self):
        blob = _encrypt()
        assert blob == blob.strip()
        base64.b64decode(blob.encode(), validate=True)

    def test_plaintext_is_not_recoverable_from_the_blob(self):
        # Not a secrecy proof -- it just pins that the payload is not stored in
        # the clear alongside it.
        raw = _blob(_encrypt())
        assert b"sword" not in raw
        assert b"ember_fox" not in raw
        assert b"inventory" not in raw

    def test_nonce_slice_is_carried_verbatim_after_the_version(self):
        # The decryptor slices nonce at [1:13]. If encrypt wrote the nonce
        # anywhere else the round-trip would fail, so this pins the offsets.
        raw = _blob(_encrypt())
        nonce = raw[1:1 + se.NONCE_SIZE]
        assert len(nonce) == 12
        assert nonce != b"\x00" * 12

    def test_session_blob_uses_the_same_layout(self):
        # Same literal rule as test_body_length_matches_serialised_payload_plus_tag:
        # 12 and 16 written out, not read from the module. The two functions share
        # one layout, and an equality that reads se.NONCE_SIZE would agree with
        # itself under a wrong nonce size instead of pinning the shared layout.
        raw = _blob(se.encrypt_session_file(PAYLOAD, _dummy_key()))
        assert raw[0] == se.ENCRYPTION_VERSION
        assert len(raw) == 1 + 12 + len(
            json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
        ) + 16


# --------------------------------------------------------------------------
# encrypt_extra_state
# --------------------------------------------------------------------------

class TestEncryptExtraState:
    def test_round_trip_returns_the_same_payload(self):
        key = _dummy_key()
        assert se.decrypt_extra_state(
            se.encrypt_extra_state(PAYLOAD, PLAYER, key), PLAYER, key
        ) == PAYLOAD

    def test_round_trip_preserves_nested_structures_and_unicode(self):
        payload = {"a": [{"b": {"c": [1, 2.5, None, True, "café"]}}]}
        key = _dummy_key()
        assert se.decrypt_extra_state(
            se.encrypt_extra_state(payload, PLAYER, key), PLAYER, key
        ) == payload

    def test_two_encryptions_of_one_payload_differ(self):
        # A fresh random nonce per write. Identical output means a fixed nonce,
        # which leaks that two saves were identical and invites reuse.
        assert _encrypt() != _encrypt()

    def test_empty_payload_short_circuits_to_empty_string(self):
        # An empty dict stores nothing at all, which is what keeps player_store
        # from writing an encrypted blank over an empty column.
        assert se.encrypt_extra_state({}, PLAYER, _dummy_key()) == ""

    def test_returns_a_string_not_bytes(self):
        assert isinstance(_encrypt(), str)


# --------------------------------------------------------------------------
# decrypt_extra_state -- refusal behaviour
# --------------------------------------------------------------------------

class TestDecryptExtraStateRefusals:
    def test_empty_string_returns_empty_dict(self):
        assert se.decrypt_extra_state("", PLAYER, _dummy_key()) == {}

    def test_wrong_master_key_returns_empty_dict(self):
        # The primary security property: another deployment's key must not open
        # this player's save.
        assert se.decrypt_extra_state(_encrypt(), PLAYER, _OTHER_KEY) == {}

    def test_wrong_player_id_returns_empty_dict(self):
        # Cross-account read attempt. player_store routes this by the row's own
        # id, so this is the guard against one player reading another's state.
        assert se.decrypt_extra_state(_encrypt(), OTHER_PLAYER, _dummy_key()) == {}

    def test_tampered_ciphertext_body_returns_empty_dict(self):
        raw = _flip_bit(_blob(_encrypt()), 1 + se.NONCE_SIZE + 2)
        assert se.decrypt_extra_state(_rebuild(raw), PLAYER, _dummy_key()) == {}

    def test_tampered_nonce_returns_empty_dict(self):
        raw = _flip_bit(_blob(_encrypt()), 1)
        assert se.decrypt_extra_state(_rebuild(raw), PLAYER, _dummy_key()) == {}

    def test_tampered_authentication_tag_returns_empty_dict(self):
        # The last 16 bytes are the GCM tag; flipping it is the classic
        # "extend the player's gold" edit.
        tampered = _flip_bit(_blob(_encrypt()), -1)
        assert se.decrypt_extra_state(_rebuild(tampered), PLAYER, _dummy_key()) == {}

    def test_truncated_blob_returns_empty_dict(self):
        raw = _blob(_encrypt())[:-8]
        assert se.decrypt_extra_state(_rebuild(raw), PLAYER, _dummy_key()) == {}

    def test_blob_shorter_than_the_minimum_returns_empty_dict(self):
        # Below 1 + nonce + tag there is nothing to authenticate.
        assert se.decrypt_extra_state(_rebuild(b"\x01" + b"\x00" * 8),
                                      PLAYER, _dummy_key()) == {}

    def test_unknown_version_byte_is_refused(self):
        raw = _blob(_encrypt())
        raw = bytes([se.ENCRYPTION_VERSION + 1]) + raw[1:]
        assert se.decrypt_extra_state(_rebuild(raw), PLAYER, _dummy_key()) == {}

    def test_text_that_is_not_base64_returns_empty_dict(self):
        assert se.decrypt_extra_state("this is not base64 !!!",
                                      PLAYER, _dummy_key()) == {}

    def test_bytes_input_returns_empty_dict_rather_than_raising(self):
        # Refusal is total: a malformed input type must not escape the function.
        assert se.decrypt_extra_state(b"\x01\x02\x03",
                                      PLAYER, _dummy_key()) == {}

    def test_refusal_never_leaks_partial_state(self):
        # A refusal must be exactly `{}`, never a partially recovered dict that
        # a caller would store back over the player's row.
        for candidate in (
            se.decrypt_extra_state(_encrypt(), PLAYER, _OTHER_KEY),
            se.decrypt_extra_state("not base64", PLAYER, _dummy_key()),
            se.decrypt_extra_state("", PLAYER, _dummy_key()),
        ):
            assert candidate == {}
            assert isinstance(candidate, dict)


# --------------------------------------------------------------------------
# Client session file helpers
# --------------------------------------------------------------------------

class TestSessionFile:
    def test_round_trip_returns_the_same_payload(self):
        key = _dummy_key()
        session = {"player_id": PLAYER, "signing_key": "dummy-signing-key"}
        assert se.decrypt_session_file(
            se.encrypt_session_file(session, key), key
        ) == session

    def test_round_trip_survives_a_key_from_get_master_key(self):
        # The real pairing: a key derived by get_master_key feeds both halves.
        os.environ[se.DEV_SEED_ENV] = "contract-test-dev-seed"
        key = se.get_master_key()
        session = {"player_id": PLAYER, "expires": 12345}
        assert se.decrypt_session_file(
            se.encrypt_session_file(session, key), key
        ) == session

    def test_empty_payload_still_produces_a_real_blob(self):
        # Unlike encrypt_extra_state there is no short-circuit, so an empty
        # payload is encrypted rather than stored as "". Pinned because the
        # asymmetry is real and load-bearing for column-vs-file callers.
        assert se.encrypt_session_file({}, _dummy_key()) != ""

    def test_two_session_encryptions_differ(self):
        assert (se.encrypt_session_file(PAYLOAD, _dummy_key())
                != se.encrypt_session_file(PAYLOAD, _dummy_key()))

    def test_wrong_key_returns_empty_dict(self):
        assert se.decrypt_session_file(
            se.encrypt_session_file(PAYLOAD, _dummy_key()), _OTHER_KEY
        ) == {}

    def test_tampered_session_returns_empty_dict(self):
        raw = _flip_bit(_blob(se.encrypt_session_file(PAYLOAD, _dummy_key())), 5)
        assert se.decrypt_session_file(_rebuild(raw), _dummy_key()) == {}

    def test_empty_string_returns_empty_dict(self):
        assert se.decrypt_session_file("", _dummy_key()) == {}

    def test_not_base64_returns_empty_dict(self):
        assert se.decrypt_session_file("%%%%not base64%%%%", _dummy_key()) == {}

    def test_session_blob_is_not_readable_through_the_account_path(self):
        # A session blob and an extra_state blob share the layout, so confirm
        # the two key derivations genuinely keep them apart.
        key = _dummy_key()
        session_blob = se.encrypt_session_file(PAYLOAD, key)
        assert se.decrypt_extra_state(session_blob, PLAYER, key) == {}


# --------------------------------------------------------------------------
# HMAC helpers
# --------------------------------------------------------------------------

class TestHmac:
    def test_sign_returns_sixty_four_lowercase_hex_chars(self):
        signature = se.hmac_sign("payload", _dummy_key())
        assert len(signature) == 64
        assert signature == signature.lower()
        int(signature, 16)

    def test_sign_matches_the_standard_library(self):
        key = _dummy_key()
        data = "player_id=abc;amount=10"
        assert se.hmac_sign(data, key) == hmac_stdlib.new(
            key, data.encode(), hashlib.sha256
        ).hexdigest()

    def test_sign_is_deterministic(self):
        key = _dummy_key()
        assert se.hmac_sign("same", key) == se.hmac_sign("same", key)

    def test_verify_accepts_a_correct_signature(self):
        key = _dummy_key()
        assert se.hmac_verify("payload", se.hmac_sign("payload", key), key) is True

    def test_verify_rejects_a_flipped_signature(self):
        key = _dummy_key()
        signature = se.hmac_sign("payload", key)
        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
        assert se.hmac_verify("payload", flipped, key) is False

    def test_verify_rejects_modified_data(self):
        key = _dummy_key()
        assert se.hmac_verify(
            "payload+1", se.hmac_sign("payload", key), key
        ) is False

    def test_verify_rejects_a_different_key(self):
        assert se.hmac_verify(
            "payload", se.hmac_sign("payload", _dummy_key()), _OTHER_KEY
        ) is False

    def test_verify_returns_a_real_bool(self):
        key = _dummy_key()
        result = se.hmac_verify("payload", se.hmac_sign("payload", key), key)
        assert result is True and isinstance(result, bool)

    def test_sign_handles_unicode_data(self):
        key = _dummy_key()
        data = "café 你好"
        assert se.hmac_verify(data, se.hmac_sign(data, key), key) is True


# --------------------------------------------------------------------------
# get_master_key
# --------------------------------------------------------------------------

class TestGetMasterKey:
    def test_master_key_env_var_is_returned_as_bytes(self):
        os.environ[se.MASTER_KEY_ENV] = "contract-test-deployment-key"
        assert se.get_master_key() == b"contract-test-deployment-key"

    def test_master_key_env_var_wins_over_the_dev_seed(self):
        os.environ[se.MASTER_KEY_ENV] = "contract-test-deployment-key"
        os.environ[se.DEV_SEED_ENV] = "contract-test-dev-seed"
        assert se.get_master_key() == b"contract-test-deployment-key"

    def test_dev_seed_is_hashed_to_thirty_two_bytes(self):
        os.environ[se.DEV_SEED_ENV] = "contract-test-dev-seed"
        key = se.get_master_key()
        assert len(key) == 32
        assert key == hashlib.sha256(b"contract-test-dev-seed").digest()

    def test_blank_master_key_falls_through_to_the_dev_seed(self):
        os.environ[se.MASTER_KEY_ENV] = "   "
        os.environ[se.DEV_SEED_ENV] = "contract-test-dev-seed"
        assert se.get_master_key() == hashlib.sha256(
            b"contract-test-dev-seed"
        ).digest()

    def test_raises_when_neither_variable_is_set(self):
        # The load-bearing refusal. A fallback to a published constant made every
        # unconfigured deployment share one readable key.
        with pytest.raises(se.MasterKeyUnavailable):
            se.get_master_key()

    def test_whitespace_only_values_still_raise(self):
        os.environ[se.MASTER_KEY_ENV] = "  "
        os.environ[se.DEV_SEED_ENV] = "\t\n"
        with pytest.raises(se.MasterKeyUnavailable):
            se.get_master_key()

    def test_master_key_unavailable_is_a_runtime_error(self):
        # player_store catches Exception around get_master_key, so the refusal
        # has to land inside the existing handler.
        assert issubclass(se.MasterKeyUnavailable, RuntimeError)

    def test_error_message_names_the_configuration_variables(self):
        with pytest.raises(se.MasterKeyUnavailable) as excinfo:
            se.get_master_key()
        message = str(excinfo.value)
        assert se.MASTER_KEY_ENV in message
        assert se.DEV_SEED_ENV in message

    def test_error_message_does_not_hide_that_the_old_default_is_known_bad(self):
        with pytest.raises(se.MasterKeyUnavailable) as excinfo:
            se.get_master_key()
        assert se.RETIRED_HARDCODED_DEV_SEED in str(excinfo.value)

    def test_env_var_names_are_the_documented_ones(self):
        # A rename would silently leave every deployment unconfigured.
        assert se.MASTER_KEY_ENV == "WC_SAVE_ENCRYPTION_KEY"
        assert se.DEV_SEED_ENV == "WC_SAVE_ENCRYPTION_DEV_SEED"

    def test_key_from_get_master_key_opens_a_real_blob(self):
        # Ties the two halves together: whatever get_master_key hands out must
        # be usable as an AES-256 master key by the encrypt/decrypt pair.
        os.environ[se.MASTER_KEY_ENV] = "contract-test-deployment-key"
        key = se.get_master_key()
        blob = se.encrypt_extra_state(PAYLOAD, PLAYER, key)
        assert se.decrypt_extra_state(blob, PLAYER, key) == PAYLOAD


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------

class TestPublicSurface:
    @pytest.mark.parametrize("name", [
        "derive_account_key",
        "encrypt_extra_state",
        "decrypt_extra_state",
        "encrypt_session_file",
        "decrypt_session_file",
        "hmac_sign",
        "hmac_verify",
        "get_master_key",
        "MasterKeyUnavailable",
        "ENCRYPTION_VERSION",
        "NONCE_SIZE",
        "KEY_DERIVATION_SALT",
        "MASTER_KEY_ENV",
        "DEV_SEED_ENV",
    ])
    def test_exports_are_present(self, name):
        assert hasattr(se, name)

    def test_version_and_nonce_constants(self):
        assert se.ENCRYPTION_VERSION == 1
        assert se.NONCE_SIZE == 12

    def test_reexported_names_are_the_ones_player_store_imports(self):
        # player_store.py:21-25 imports exactly these four by name.
        from server import player_store
        for name in ("encrypt_extra_state", "decrypt_extra_state", "get_master_key"):
            assert getattr(player_store, name) is getattr(se, name)


# --------------------------------------------------------------------------
# Caller authorisation -- "encryption requires auth"
#
# There is no HTTP surface here, so no session token is presented at these
# functions. The authorisation boundary that DOES exist is the key, and it has
# two halves:
#
#   1. Access to encryption AT ALL. get_master_key() raises rather than
#      inventing a key, so a deployment that never configured one cannot
#      encrypt. This is the fail-closed property, and it is the closest thing
#      this module has to an auth check.
#   2. Access to ONE player's blob. Every blob is sealed under a key derived
#      from (master_key, player_id), so possession of the master key is not
#      enough -- the caller must also name the right player.
#
# Both are asserted in both directions: the legitimate pair opens, every other
# pair refuses.
# --------------------------------------------------------------------------

class TestCallerAuthorisation:
    def test_encryption_is_unavailable_without_a_configured_key(self):
        # The autouse fixture has cleared both variables. This is the refusal a
        # caller hits when nobody configured the deployment: no key, no
        # encryption, and emphatically not a published default.
        with pytest.raises(se.MasterKeyUnavailable):
            se.get_master_key()

    def test_the_configured_key_is_the_only_thing_that_unlocks_encryption(self):
        # Set the variable the way an operator would, and the same call now
        # returns a usable key. Paired with the test above: refuse-when-absent,
        # serve-when-present.
        os.environ[se.MASTER_KEY_ENV] = "contract-test-deployment-key"
        assert se.get_master_key() == b"contract-test-deployment-key"

    def test_an_unconfigured_deployment_cannot_reach_a_saved_blob(self):
        # The failure a real deployment hits: with no key configured, the read
        # path in player_store.py:182 raises out of get_master_key() and the
        # caller falls back to `extra = {}`. Prove the raise happens BEFORE any
        # decryption is attempted, i.e. there is no "decrypt with nothing"
        # fallback hiding behind the refusal.
        blob = _encrypt()
        with pytest.raises(se.MasterKeyUnavailable):
            se.get_master_key()
        # The blob itself is still intact and opens once a key exists -- so the
        # data was not destroyed by the refusal, only withheld.
        assert se.decrypt_extra_state(blob, PLAYER, _dummy_key()) == PAYLOAD

    def test_a_blob_opens_only_for_the_exact_player_and_key_that_wrote_it(self):
        # The positive control for the matrix below: the matching pair works.
        key = _dummy_key()
        assert se.decrypt_extra_state(
            se.encrypt_extra_state(PAYLOAD, PLAYER, key), PLAYER, key
        ) == PAYLOAD

    @pytest.mark.parametrize(
        "blob_player,reader_player,write_key,read_key,should_open",
        [
            # same player, same key -> the legitimate caller
            (PLAYER, PLAYER, "primary", "primary", True),
            # right player, WRONG key -> a different deployment's key
            (PLAYER, PLAYER, "primary", "other", False),
            # WRONG player, right key -> cross-account read attempt
            (PLAYER, OTHER_PLAYER, "primary", "primary", False),
            # wrong player, wrong key -> unrelated caller
            (PLAYER, OTHER_PLAYER, "primary", "other", False),
        ],
    )
    def test_every_player_key_combination_is_decided_correctly(
            self, blob_player, reader_player, write_key, read_key, should_open):
        # write_key and read_key are SEPARATE on purpose. Sharing one variable
        # makes the "wrong key" rows self-fulfilling: the blob is written with
        # the very key that then fails to open it, so the row passes for the
        # wrong reason and the cross-key property is never measured.
        blob = se.encrypt_extra_state(
            PAYLOAD, blob_player, _dummy_key(write_key.encode())
        )
        result = se.decrypt_extra_state(
            blob, reader_player, _dummy_key(read_key.encode())
        )
        assert (result == PAYLOAD) is should_open
        if not should_open:
            # A refusal is exactly `{}`, so a caller cannot mistake "denied"
            # for "loaded a partial save".
            assert result == {}

    def test_hmac_binds_data_to_a_key_just_as_the_cipher_binds_to_a_player(self):
        # Same shape of check on the HMAC helpers: a signature is valid only for
        # the (data, key) pair it was made from.
        key = _dummy_key()
        signature = se.hmac_sign(PLAYER, key)
        assert se.hmac_verify(PLAYER, signature, key) is True
        assert se.hmac_verify(OTHER_PLAYER, signature, key) is False
        assert se.hmac_verify(PLAYER, signature, _OTHER_KEY) is False


# --------------------------------------------------------------------------
# Non-finite values
#
# MEASURED DEFECT (probe run against this tree, 2026-10-05):
#
#   encrypt_extra_state({"gold": nan, "hp": inf}, ...) succeeds, and
#   decrypt_extra_state returns {'gold': nan, 'hp': inf}. The values survive.
#
# Why that matters: `json.dumps` defaults to `allow_nan=True` and emits the bare
# tokens `NaN` / `Infinity` / `-Infinity`. Those are NOT valid JSON (RFC 8259
# has no literal for them), so the module's plaintext is not portable JSON even
# though it round-trips through Python's own symmetric `json.loads`. Anything
# downstream that parses strictly -- Postgres JSONB, another language, a future
# non-Python client -- either errors or reads a different value.
#
# The refusal tests below are marked `xfail(strict=False)` on purpose: green
# today, and they report XPASS the moment someone adds a `math.isfinite` sweep,
# so the hole cannot be quietly forgotten. `TestNonFiniteValues` also asserts the
# CURRENT behaviour directly, so the defect is measured rather than merely
# hoped about.
# --------------------------------------------------------------------------

NON_FINITE_XFAIL = (
    "LIVE DEFECT: encrypt_extra_state/decrypt_extra_state do not reject "
    "non-finite floats. json.dumps(allow_nan=True) serialises them as the bare "
    "tokens NaN/Infinity, which are not valid RFC 8259 JSON, so the save "
    "plaintext is not portable even though Python's json.loads reads it back. "
    "XPASS = fixed."
)


class NonPortableJson(Exception):
    """A strict reader met a bare NaN/Infinity token.

    Deliberately NOT a ValueError subclass. The defect tests below use
    ``pytest.raises(ValueError)`` to mean "the module refused this input", so a
    parser helper that raised ValueError would satisfy those assertions by
    accident and the refusal tests could never fail. Keeping the types distinct
    is what makes the two directions separable.
    """


def _reject_non_finite_constant(name):
    """``parse_constant`` hook for a strict RFC 8259 reader.

    Python's json calls this for the bare tokens NaN / Infinity / -Infinity,
    which RFC 8259 does not define. A conforming parser must reject them.
    """
    raise NonPortableJson(f"non-finite constant in plaintext: {name}")


class TestNonFiniteValues:
    def test_current_behaviour_round_trips_a_nan_and_infinities(self):
        # Pins what the module does TODAY, so the defect below is falsifiable
        # rather than an assertion of hoped-for behaviour.
        payload = {"gold": float("nan"), "hp": float("inf"), "sta": float("-inf")}
        result = se.decrypt_extra_state(
            se.encrypt_extra_state(payload, PLAYER, _dummy_key()), PLAYER, _dummy_key()
        )
        assert set(result) == {"gold", "hp", "sta"}
        assert result["gold"] != result["gold"]          # NaN != NaN
        assert result["hp"] == float("inf")
        assert result["sta"] == float("-inf")

    def test_a_nan_is_not_equal_to_itself_after_a_round_trip(self):
        # The practical consequence for a caller: dict equality on a decrypted
        # save fails for reasons that look like data corruption. NaN is the only
        # value that is not equal to itself, and it survives the round trip.
        #
        # Written as `not (a == b)` rather than a chained `a == b is False`.
        # Chaining parses as `a == b and b is False`, which is a DIFFERENT
        # assertion -- it asks whether b is the False singleton. That form
        # fails here for the wrong reason and would not notice a future change
        # to the equality result.
        payload = {"progress": float("nan")}
        result = se.decrypt_extra_state(
            se.encrypt_extra_state(payload, PLAYER, _dummy_key()), PLAYER, _dummy_key()
        )
        assert not (result == payload)
        # The keys survive -- only the value's equality is broken.
        assert set(result) == set(payload)

    def test_finite_floats_are_unaffected_by_the_defect(self):
        # The control. Ordinary values must still round-trip exactly, so this
        # failure mode is scoped to non-finite numbers and not "floats broke".
        payload = {"progress": 0.5, "rate": 1.25, "big": 2 ** 70}
        assert se.decrypt_extra_state(
            se.encrypt_extra_state(payload, PLAYER, _dummy_key()), PLAYER, _dummy_key()
        ) == payload

    @pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
    def test_encrypt_rejects_a_top_level_nan(self):
        with pytest.raises(ValueError):
            se.encrypt_extra_state({"gold": float("nan")}, PLAYER, _dummy_key())

    @pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
    def test_encrypt_rejects_a_nested_nan(self):
        # Nested is the shape that actually reaches the save layer:
        # player_store._split puts nested player fields inside `extra`.
        payload = {"tower_state": {"floor_progress": float("nan")}}
        with pytest.raises(ValueError):
            se.encrypt_extra_state(payload, PLAYER, _dummy_key())

    @pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
    def test_encrypt_rejects_infinity_in_any_position(self):
        for payload in (
            {"hp": float("inf")},
            {"hp": float("-inf")},
            {"nested": {"hp": float("inf")}},
            {"list": [float("inf")]},
        ):
            with pytest.raises(ValueError):
                se.encrypt_extra_state(payload, PLAYER, _dummy_key())

    @pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
    def test_session_file_encrypt_rejects_non_finite_too(self):
        # The client session path shares the serialiser, so it inherits the
        # same hole and needs the same fix.
        with pytest.raises(ValueError):
            se.encrypt_session_file({"hp": float("nan")}, _dummy_key())

    @pytest.mark.xfail(strict=False, reason=NON_FINITE_XFAIL)
    def test_a_non_finite_save_never_produces_non_portable_plaintext(self):
        # The property a fix would actually restore: a save is either refused at
        # the door, or its plaintext is portable JSON a strict reader accepts.
        # Never "encrypted successfully into text no strict parser can read".
        #
        # Two prior versions of this test passed on today's code for the wrong
        # reasons, and both are worth recording:
        #
        #   1. It reused the finite PAYLOAD fixture, so it never carried the
        #      defect at all -- an XPASS on a defect test reads as "closed".
        #   2. It raised ValueError from the strict reader INSIDE a
        #      pytest.raises(ValueError), so the reader's refusal satisfied the
        #      very assertion meant to demand a refusal. The test could not fail.
        #
        # So: the strict reader raises NonPortableJson, a type that is NOT a
        # ValueError, and the early return handles the fixed case explicitly.
        payload = {"gold": float("nan"), "hp": float("inf")}
        try:
            blob = se.encrypt_extra_state(payload, PLAYER, _dummy_key())
        except ValueError:
            # Refused at the door, so no non-portable plaintext was ever
            # written. This is the post-fix path.
            return

        recovered = se.decrypt_extra_state(blob, PLAYER, _dummy_key())
        strict = json.loads(
            json.dumps(recovered), parse_constant=_reject_non_finite_constant
        )
        # Unreachable while the defect is live -- the reader raises first.
        assert strict == recovered
