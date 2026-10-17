"""Mutual HMAC-SHA256 challenge/response (see ``protocol.md`` and ``security.md``).

``proof        = HMAC(key, server_nonce || client_nonce)`` (hex)
``server_proof = HMAC(key, client_nonce || server_nonce)`` (hex)

Nonces travel base64 encoded and are concatenated as raw bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable

SUPPORTED_PROTO = {1}
NONCE_BYTES = 32
MIN_CLIENT_NONCE_BYTES = 16


def make_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(NONCE_BYTES)).decode("ascii")


def decode_nonce(nonce_b64: str) -> bytes:
    """Decode a base64 nonce; raises ``ValueError`` when malformed."""
    if not isinstance(nonce_b64, str):
        raise ValueError("nonce must be a string")
    try:
        return base64.b64decode(nonce_b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("invalid base64 nonce") from err


def _mac(key: bytes, first_b64: str, second_b64: str) -> str:
    data = decode_nonce(first_b64) + decode_nonce(second_b64)
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def compute_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str) -> str:
    """Client proof: HMAC over ``server_nonce || client_nonce``."""
    return _mac(key, server_nonce_b64, client_nonce_b64)


def compute_server_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str) -> str:
    """Server proof: HMAC over ``client_nonce || server_nonce``."""
    return _mac(key, client_nonce_b64, server_nonce_b64)


def _equal(a: object, b: str) -> bool:
    if not isinstance(a, str):
        return False
    return hmac.compare_digest(a.lower().encode("utf-8"), b.encode("utf-8"))


def verify_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str, proof: object) -> bool:
    try:
        expected = compute_proof(key, server_nonce_b64, client_nonce_b64)
    except ValueError:
        return False
    return _equal(proof, expected)


def verify_server_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str, proof: object) -> bool:
    try:
        expected = compute_server_proof(key, server_nonce_b64, client_nonce_b64)
    except ValueError:
        return False
    return _equal(proof, expected)


class AuthLimiter:
    """Per source IP failure counter with a temporary lockout.

    After ``max_failures`` failures within ``window`` seconds the IP is locked
    for ``lockout`` seconds; the server answers ``auth_failed`` for it without
    checking the proof, after ``delay`` seconds.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 600.0,
        lockout: float = 600.0,
        delay: float = 5.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.delay = delay
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def is_locked(self, ip: str) -> bool:
        until = self._locked_until.get(ip)
        if until is None:
            return False
        if self._clock() >= until:
            del self._locked_until[ip]
            self._failures.pop(ip, None)
            return False
        return True

    def record_failure(self, ip: str) -> bool:
        """Count a failure; returns True when this failure locks the IP."""
        now = self._clock()
        failures = self._failures[ip]
        failures.append(now)
        while failures and now - failures[0] > self.window:
            failures.popleft()
        if len(failures) >= self.max_failures and ip not in self._locked_until:
            self._locked_until[ip] = now + self.lockout
            return True
        return False

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)
