"""
Wayfarer's Cycle — Save Encryption + HMAC (Tier 4 Anti-Cheat)

AES-256-GCM per-account encryption for the `extra_state` JSONB column
and client-side session.json. Primary defense: server authority.
This layer catches trivial save-file editing by lazy cheaters.
"""
from __future__ import annotations

import json
import os
import secrets
import base64
from typing import Optional, Tuple, Dict, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# Encryption version for forward compatibility
ENCRYPTION_VERSION = 1
KEY_DERIVATION_SALT = b"wayfarers-cycle-v1-key-salt"
NONCE_SIZE = 12  # 96-bit nonce for AES-GCM


def derive_account_key(master_key: bytes, player_id: str) -> bytes:
    """
    Derive a per-account encryption key from master key + player ID.
    
    Uses HKDF-SHA256 with a fixed salt and player_id as info.
    This ensures each account has a unique key, so compromising one
    account's key doesn't help with others.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,  # 256 bits
        salt=KEY_DERIVATION_SALT,
        info=player_id.encode(),
    )
    return hkdf.derive(master_key)


def encrypt_extra_state(
    extra_state: Dict[str, Any],
    player_id: str,
    master_key: bytes,
) -> str:
    """
    Encrypt extra_state dict to a base64 string for storage.
    
    Format: v1 | nonce (12 bytes) | ciphertext | tag (16 bytes)
    All concatenated and base64 encoded.
    """
    if not extra_state:
        return ""
    
    account_key = derive_account_key(master_key, player_id)
    aesgcm = AESGCM(account_key)
    
    # Generate random nonce
    nonce = secrets.token_bytes(NONCE_SIZE)
    
    # Serialize and encrypt
    plaintext = json.dumps(extra_state, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    
    # Format: version(1 byte) | nonce(12) | ciphertext | tag(16)
    # AESGCM returns ciphertext+tag concatenated
    # ciphertext[:-16] is the actual ciphertext, ciphertext[-16:] is the tag
    version_byte = bytes([ENCRYPTION_VERSION])
    combined = version_byte + nonce + ciphertext
    
    return base64.b64encode(combined).decode()


def decrypt_extra_state(
    encrypted: str,
    player_id: str,
    master_key: bytes,
) -> Dict[str, Any]:
    """
    Decrypt base64 encrypted string back to extra_state dict.
    
    Returns empty dict on failure (corruption, wrong key, tampering).
    """
    if not encrypted:
        return {}
    
    try:
        combined = base64.b64decode(encrypted.encode())
        
        # Check version
        if len(combined) < 1 + NONCE_SIZE + 16:
            return {}
        
        version = combined[0]
        if version != ENCRYPTION_VERSION:
            # Future: handle migration
            return {}
        
        nonce = combined[1:1 + NONCE_SIZE]
        ciphertext = combined[1 + NONCE_SIZE:]
        
        account_key = derive_account_key(master_key, player_id)
        aesgcm = AESGCM(account_key)
        
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode())
    
    except Exception:
        # Any decryption failure (wrong key, tampering, corruption) -> empty
        return {}


def encrypt_session_file(
    session_data: Dict[str, Any],
    master_key: bytes,
) -> str:
    """
    Encrypt client session.json file.
    
    Uses a fixed derivation for client-side (no per-player key on client).
    Format: v1 | nonce | ciphertext+tag (base64)
    """
    # For client, derive a fixed key from master
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=KEY_DERIVATION_SALT,
        info=b"client-session",
    )
    client_key = hkdf.derive(master_key)
    aesgcm = AESGCM(client_key)
    
    nonce = secrets.token_bytes(NONCE_SIZE)
    plaintext = json.dumps(session_data, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    
    version_byte = bytes([ENCRYPTION_VERSION])
    combined = version_byte + nonce + ciphertext
    
    return base64.b64encode(combined).decode()


def decrypt_session_file(
    encrypted: str,
    master_key: bytes,
) -> Dict[str, Any]:
    """Decrypt client session.json file."""
    if not encrypted:
        return {}
    
    try:
        combined = base64.b64decode(encrypted.encode())
        
        if len(combined) < 1 + NONCE_SIZE + 16:
            return {}
        
        version = combined[0]
        if version != ENCRYPTION_VERSION:
            return {}
        
        nonce = combined[1:1 + NONCE_SIZE]
        ciphertext = combined[1 + NONCE_SIZE:]
        
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=KEY_DERIVATION_SALT,
            info=b"client-session",
        )
        client_key = hkdf.derive(master_key)
        aesgcm = AESGCM(client_key)
        
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode())
    
    except Exception:
        return {}


def hmac_sign(data: str, key: bytes) -> str:
    """HMAC-SHA256 for additional integrity verification."""
    import hmac
    import hashlib
    return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


def hmac_verify(data: str, signature: str, key: bytes) -> bool:
    """Verify HMAC signature."""
    import hmac
    expected = hmac_sign(data, key)
    return hmac.compare_digest(signature, expected)


# Master key management
MASTER_KEY_ENV = "WC_SAVE_ENCRYPTION_KEY"
DEV_SEED_ENV = "WC_SAVE_ENCRYPTION_DEV_SEED"

# The seed the dev fallback used to invent for itself when the env var was
# unset. Kept only so the refusal can name it: an operator who sees this string
# in an error knows a previous build derived production data encryption keys
# from a value that is published in this file, and that data is not protected.
# It is never accepted as a seed.
RETIRED_HARDCODED_DEV_SEED = "dev-only-change-in-production"


class MasterKeyUnavailable(RuntimeError):
    """No usable master key, and this process refuses to invent one.

    Raised rather than returned-as-a-fallback so the failure surfaces at the
    call that needed the key. The callers are player_store's encrypt/decrypt
    paths, which already treat an unusable key as "cannot process extra_state",
    so a raised error lands in the same place a wrong key would.
    """


def get_master_key() -> bytes:
    """
    Get the master encryption key for save-state encryption.

    Two ways to get one, and no third:

    1. ``WC_SAVE_ENCRYPTION_KEY`` -- the deployment's real key. Production uses
       this, from a secret manager or the environment.
    2. ``WC_SAVE_ENCRYPTION_DEV_SEED`` -- an explicit opt-in for a local
       developer box, hashed to 32 bytes.

    If neither is set, this RAISES. It does not fall back to a hardcoded seed.
    That fallback is the bug this closes: with the env var unset, every
    deployment in the world -- a staging box, a CI job, a container that
    forgot one variable -- silently derived the SAME key from a literal sitting
    in this file. Anyone who read the file could decrypt every account's
    extra_state on all of them. Failing open is what turned "no key configured"
    from a startup error into a quiet, total compromise of the save layer.

    The same reasoning as ``_require_internal_mint_token`` in server/main.py:
    an ABSENT variable means there is nothing valid to use, so the operation is
    refused, and an operator has to opt in deliberately. The deployment that
    forgets to configure is visibly broken rather than silently open.

    Note the failure is at USE time, not import time. A process that never
    encrypts (in-memory player store, most of the test suite) never asks for a
    key and so never trips this -- which is the right time to fail, since
    demanding a key at import would break every read-only and test boot.
    """
    env_key = os.environ.get(MASTER_KEY_ENV) or ""
    if env_key.strip():
        return env_key.encode()

    dev_seed = os.environ.get(DEV_SEED_ENV) or ""
    if dev_seed.strip():
        digest = hashes.Hash(hashes.SHA256())
        digest.update(dev_seed.encode())
        return digest.finalize()

    raise MasterKeyUnavailable(
        f"no master encryption key configured: set {MASTER_KEY_ENV} to the "
        f"deployment key, or set {DEV_SEED_ENV} to an explicit local-development "
        f"seed. Refusing to derive a key from a built-in default -- that default "
        f"({RETIRED_HARDCODED_DEV_SEED!r}) was a published constant, so any "
        f"process left without a configured key used key material an attacker "
        f"could read straight out of this file."
    )


if __name__ == "__main__":
    # Quick self-test. Sets an explicit dev seed rather than relying on a
    # default, because get_master_key() now refuses to invent one.
    import os as _os
    _os.environ.setdefault(DEV_SEED_ENV, "self-test-only-seed")

    master = get_master_key()
    pid = "test_player_123"
    
    # Test extra_state encryption
    extra = {"beasts": [], "inventory": {"sword": 1}, "tower_state": {"floor": 5}}
    encrypted = encrypt_extra_state(extra, pid, master)
    print(f"Encrypted: {encrypted[:50]}...")
    
    decrypted = decrypt_extra_state(encrypted, pid, master)
    print(f"Decrypted: {decrypted}")
    assert decrypted == extra
    
    # Test tampering detection
    tampered = encrypted[:-5] + "XXXXX"
    decrypted_tampered = decrypt_extra_state(tampered, pid, master)
    print(f"Tampered result: {decrypted_tampered}")
    assert decrypted_tampered == {}
    
    # Test session encryption
    session = {"player_id": pid, "signing_key": "abc123"}
    enc_session = encrypt_session_file(session, master)
    dec_session = decrypt_session_file(enc_session, master)
    print(f"Session round-trip: {dec_session == session}")
    assert dec_session == session
    
    print("All tests passed!")