"""Security primitives: credential encryption, password hashing, sessions, rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypts exchange API credentials at rest with Fernet (AES-128-CBC + HMAC).

    The key comes from ``ARBIES_SECRET_KEY``. Decrypted credentials are only
    handed to the live exchange client; they are never returned by the API and
    never exposed to AI agents.
    """

    def __init__(self, key: str | None):
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, creds: dict) -> str:
        if self._fernet is None:
            raise RuntimeError("ARBIES_SECRET_KEY is not configured; cannot store credentials")
        return self._fernet.encrypt(json.dumps(creds).encode()).decode()

    def decrypt(self, blob: str) -> dict:
        if self._fernet is None:
            raise RuntimeError("ARBIES_SECRET_KEY is not configured")
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except InvalidToken as e:
            raise RuntimeError("credential decryption failed (wrong ARBIES_SECRET_KEY?)") from e

    @staticmethod
    def fingerprint(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """In-memory sliding-window rate limiter for sensitive endpoints."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True
