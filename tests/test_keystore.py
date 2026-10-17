from __future__ import annotations

import logging
import stat
import uuid
from pathlib import Path

import pytest

from nibe_service_usb.keystore import KeyStore, format_key_grouped, parse_key


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_creation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data = tmp_path / "data"
    with caplog.at_level(logging.WARNING):
        key = KeyStore(data).load_or_create()
    assert len(key) == 32
    text = (data / "auth.key").read_text().strip()
    assert text == key.hex()
    assert len(text) == 64
    assert any(
        r.levelno == logging.WARNING and "Generated a new auth key" in r.getMessage()
        and key.hex() not in r.getMessage()
        for r in caplog.records
    )
    uuid.UUID((data / "server_id").read_text().strip())


def test_permissions(tmp_path: Path) -> None:
    data = tmp_path / "data"
    KeyStore(data).load_or_create()
    assert mode(data) == 0o700
    assert mode(data / "auth.key") == 0o600
    assert mode(data / "server_id") == 0o600


def test_persistence_and_single_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    first = KeyStore(tmp_path / "d")
    key = first.load_or_create()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        second = KeyStore(tmp_path / "d")
        assert second.load_or_create() == key
    assert not any("Generated a new auth key" in r.getMessage() for r in caplog.records)
    assert second.key == key
    assert second.server_id == first.server_id


def test_rotate(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = KeyStore(tmp_path / "d")
    old = store.load_or_create()
    server_id = store.server_id
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        new = store.rotate()
    assert new != old
    assert len(new) == 32
    assert store.key == new
    assert KeyStore(tmp_path / "d").load_or_create() == new
    assert store.server_id == server_id
    assert mode(tmp_path / "d" / "auth.key") == 0o600
    assert new.hex() not in caplog.text


def test_reload(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "d")
    store.load_or_create()
    new = KeyStore(tmp_path / "d").rotate()
    assert store.reload() == new


def test_server_id_stable(tmp_path: Path) -> None:
    a = KeyStore(tmp_path / "d")
    sid = a.server_id
    assert KeyStore(tmp_path / "d").server_id == sid
    a.rotate()
    assert KeyStore(tmp_path / "d").server_id == sid


def test_property_creates_lazily(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "d")
    assert len(store.key) == 32
    assert (tmp_path / "d" / "auth.key").exists()


def test_loose_permissions_are_fixed(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "d")
    store.load_or_create()
    (tmp_path / "d" / "auth.key").chmod(0o644)
    KeyStore(tmp_path / "d").load_or_create()
    assert mode(tmp_path / "d" / "auth.key") == 0o600


def test_corrupt_key_rejected(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "d")
    store.load_or_create()
    (tmp_path / "d" / "auth.key").write_text("abcd\n")
    with pytest.raises(ValueError, match="auth.key"):
        KeyStore(tmp_path / "d").load_or_create()


def test_format_key() -> None:
    key = bytes(range(32))
    assert KeyStore.format_key(key) == key.hex()
    grouped = format_key_grouped(key)
    assert all(len(g) <= 8 for g in grouped.split("-"))
    assert all(len(g) == 8 for g in grouped.split("-")[:-1])
    assert grouped == grouped.upper()
    assert parse_key(grouped) == key
    assert parse_key(grouped.lower()) == key
    assert parse_key(key.hex()) == key


def test_parse_key_errors() -> None:
    with pytest.raises(ValueError):
        parse_key("not a key")
    with pytest.raises(ValueError):
        parse_key("AAAA-AAAA")
