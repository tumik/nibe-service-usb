from __future__ import annotations

from pathlib import Path

import pytest

from nibe_service_usb.cli import main
from nibe_service_usb.keystore import KeyStore

REPO = Path(__file__).resolve().parent.parent


def test_check_config_defaults(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[pump]" in out
    assert "listen_port = 9477" in out
    assert "alarm flag registers: 53" in out
    assert "configuration OK" in out


def test_check_config_example(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["check-config", "--config", str(REPO / "config.example.toml"), "--data-dir", str(tmp_path)])
        == 0
    )
    assert "configuration OK" in capsys.readouterr().out


def test_check_config_log_level_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--data-dir", str(tmp_path), "--log-level", "DEBUG"]) == 0
    assert 'level = "debug"' in capsys.readouterr().out


def test_check_config_bad_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("[pump]\nprot = 1\n")
    assert main(["check-config", "--config", str(cfg), "--data-dir", str(tmp_path)]) == 1
    assert "pump.prot" in capsys.readouterr().err


def test_check_config_bad_sensors_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sensors = tmp_path / "s.yaml"
    sensors.write_text("sensors:\n  - name: a\n    label: A\n    bogus: 1\n    register: 1\n")
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[poll]\nsensors_file = "{sensors}"\n')
    assert main(["check-config", "--config", str(cfg), "--data-dir", str(tmp_path)]) == 1
    assert "bogus" in capsys.readouterr().err


def test_show_key_and_rotate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    assert main(["show-key", "--data-dir", str(data)]) == 1  # never creates a key
    assert not (data / "auth.key").exists()
    capsys.readouterr()
    KeyStore(data).load_or_create()  # what the server does on its first start
    assert main(["show-key", "--data-dir", str(data)]) == 0
    first = capsys.readouterr().out
    key_hex = (data / "auth.key").read_text().strip()
    assert key_hex in first
    assert "base32" in first

    assert main(["show-key", "--data-dir", str(data)]) == 0
    assert capsys.readouterr().out == first

    assert main(["rotate-key", "--data-dir", str(data)]) == 0
    rotated = capsys.readouterr().out
    new_hex = (data / "auth.key").read_text().strip()
    assert new_hex != key_hex
    assert new_hex in rotated


def test_data_dir_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NIBE_SERVICE_USB_DATA_DIR", str(tmp_path / "envdir"))
    assert main(["rotate-key"]) == 0
    assert (tmp_path / "envdir" / "auth.key").exists()
    assert main(["show-key"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("command", ["run", "simulate", "probe"])
def test_not_implemented_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str) -> None:
    try:
        import nibe_service_usb.app  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        pytest.skip("app module exists")
    assert main([command, "--data-dir", str(tmp_path)]) == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_debug_website_option_parsed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from nibe_service_usb.cli import _debug_website, build_parser

    assert _debug_website("8080") == (None, 8080)
    assert _debug_website("0.0.0.0:0") == ("0.0.0.0", 0)
    assert _debug_website("[::1]:9") == ("::1", 9)
    args = build_parser().parse_args(["run", "--debug-website", "127.0.0.1:8099"])
    assert args.debug_website == ("127.0.0.1", 8099)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--debug-website", "abc"])
    capsys.readouterr()


def test_requires_command() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
