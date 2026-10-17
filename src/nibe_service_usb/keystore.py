"""Authentication key and server identity storage (see ``docs/security.md``)."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

KEY_BYTES = 32
_GROUP = 8


def format_key_grouped(key: bytes) -> str:
    """Return ``key`` as base32 in dash-separated groups of 8 characters.

    Easier to read out or type than 64 hex characters, e.g.
    ``ABCDEFGH-IJKLMNOP-...``. Padding is dropped; ``parse_key`` reverses it.
    """
    text = base64.b32encode(key).decode("ascii").rstrip("=")
    return "-".join(text[i : i + _GROUP] for i in range(0, len(text), _GROUP))


def parse_key(text: str) -> bytes:
    """Decode a key given as 64 hex characters or as dash-grouped base32."""
    cleaned = text.strip()
    try:
        if len(cleaned) == KEY_BYTES * 2:
            key = bytes.fromhex(cleaned)
        else:
            b32 = cleaned.replace("-", "").replace(" ", "").upper()
            b32 += "=" * (-len(b32) % 8)
            key = base64.b32decode(b32)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("not a valid key") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    return key


class KeyStore:
    """Holds ``<data_dir>/auth.key`` (hex, mode 0600) and ``<data_dir>/server_id``."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir)
        self._key_path = self._dir / "auth.key"
        self._id_path = self._dir / "server_id"
        self._key: bytes | None = None
        self._server_id: str | None = None

    def load_or_create(self) -> bytes:
        """Return the key, generating it (and the server id) on first use."""
        self._ensure_dir()
        if self._key_path.exists():
            self._key = self._read_key()
        else:
            self._key = secrets.token_bytes(KEY_BYTES)
            self._write_private(self._key_path, self.format_key(self._key) + "\n")
            logger.warning("Generated a new auth key; run `nibe-service-usb show-key` to display it")
        self._load_server_id()
        return self._key

    def reload(self) -> bytes:
        """Re-read the key from disk (used after another process rotated it)."""
        self._key = self._read_key()
        return self._key

    def rotate(self) -> bytes:
        """Write a new random key and return it. The key is not logged."""
        self._ensure_dir()
        self._key = secrets.token_bytes(KEY_BYTES)
        self._write_private(self._key_path, self.format_key(self._key) + "\n")
        logger.warning("Auth key rotated")
        self._load_server_id()
        return self._key

    @property
    def key(self) -> bytes:
        if self._key is None:
            self.load_or_create()
        assert self._key is not None
        return self._key

    @property
    def server_id(self) -> str:
        if self._server_id is None:
            self._ensure_dir()
            self._load_server_id()
        assert self._server_id is not None
        return self._server_id

    @staticmethod
    def format_key(key: bytes) -> str:
        """64 lowercase hex characters."""
        return key.hex()

    @staticmethod
    def format_key_grouped(key: bytes) -> str:
        return format_key_grouped(key)

    # -- internals -------------------------------------------------------

    def _ensure_dir(self) -> None:
        if not self._dir.exists():
            self._dir.mkdir(mode=0o700, parents=True)
            os.chmod(self._dir, 0o700)  # mkdir mode is masked by umask

    def _read_key(self) -> bytes:
        mode = self._key_path.stat().st_mode & 0o777
        if mode & 0o077:
            logger.warning("Fixing permissions of %s (was %o)", self._key_path, mode)
            os.chmod(self._key_path, 0o600)
        text = self._key_path.read_text(encoding="ascii").strip()
        try:
            key = bytes.fromhex(text)
        except ValueError:
            raise ValueError(f"{self._key_path} does not contain a hex key") from None
        if len(key) != KEY_BYTES:
            raise ValueError(f"{self._key_path} must hold {KEY_BYTES} bytes ({KEY_BYTES * 2} hex characters)")
        return key

    def _load_server_id(self) -> None:
        if self._id_path.exists():
            text = self._id_path.read_text(encoding="ascii").strip()
            try:
                self._server_id = str(uuid.UUID(text))
                return
            except ValueError:
                logger.warning("Invalid server_id in %s, generating a new one", self._id_path)
        self._server_id = str(uuid.uuid4())
        self._write_private(self._id_path, self._server_id + "\n")

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        """Atomically write ``text`` to ``path`` with mode 0600."""
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
