"""Encryption at rest for access tokens (key derived from SESSION_SECRET; tokens never leave the server)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import env


class SecretUnavailable(Exception):
    pass


def _fernet() -> Fernet:
    secret = env().session_secret
    if not secret:
        raise SecretUnavailable("SESSION_SECRET is not set, so access tokens cannot be stored securely")
    key = hashlib.sha256(b"seo-inspector/fixer-token/v1:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretUnavailable("The stored GitHub token can't be read (SESSION_SECRET changed?). "
                                "Reconnect the repository.") from e
