"""Encryption at rest for upstream credentials (Fernet = AES-128-CBC + HMAC).

The key comes from the environment (a Kubernetes Secret in the cluster), never
from the database or source code. Lose the key and stored credentials are
unreadable; they would have to be re-entered.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "GATEWAY_SECRET_KEY"


class SecretBoxError(RuntimeError):
    pass


def generate_key() -> str:
    """Create a new key. Run once and store it in a Secret: python -c 'from gateway.registry.crypto import generate_key; print(generate_key())'"""
    return Fernet.generate_key().decode()


class SecretBox:
    def __init__(self, key: str | None = None):
        key = key or os.environ.get(KEY_ENV)
        if not key:
            raise SecretBoxError(f"{KEY_ENV} is not set; generate one with generate_key()")
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise SecretBoxError(f"{KEY_ENV} is not a valid Fernet key") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretBoxError("cannot decrypt credential: wrong key or corrupted data") from exc
