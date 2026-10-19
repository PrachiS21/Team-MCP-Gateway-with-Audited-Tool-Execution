"""API key format, generation and hashing.

Key:  wmcp_<key_id>_<secret>
  key_id  8 hex chars, public. Lets us find the row without scanning every hash.
  secret  64 hex chars (256 bits of randomness). Never stored.
We store only sha256(whole key). The key is long and random, so a fast hash is
safe here; slow hashes (bcrypt) exist to protect human-chosen passwords.
Hex (not base64) keeps "_" out of the secret, so splitting on "_" is unambiguous.
"""

import hashlib
import hmac
import secrets

PREFIX = "wmcp"


def generate_key() -> tuple[str, str]:
    """Return (key_id, full_key). The full key is shown to the user once."""
    key_id = secrets.token_hex(4)
    return key_id, f"{PREFIX}_{key_id}_{secrets.token_hex(32)}"


def parse_key(key: str) -> str | None:
    """Return the key_id if `key` has the right shape, else None."""
    parts = key.split("_")
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    _, key_id, secret = parts
    if len(key_id) != 8 or len(secret) != 64:
        return None
    try:
        int(key_id, 16), int(secret, 16)
    except ValueError:
        return None
    return key_id


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hashes_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)  # constant-time
