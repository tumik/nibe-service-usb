from __future__ import annotations

from pathlib import Path

import pytest

from nibe_service_usb.config import Config, ConfigError, format_config, load_config

REPO = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults(tmp_path: Path) -> None:
    cfg = load_config(None, data_dir=tmp_path)
    assert isinstance(cfg, Config)
    assert cfg.pump.port == "/dev/ttyACM0"
    assert cfg.pump.ri_batch_size == 9
    assert cfg.server.listen_port == 9477
    assert cfg.watchdog.timeout == 300.0
    assert cfg.defaults.hot_water_mode == "normal"
    assert cfg.poll.interval == 600.0
    assert cfg.heating.dm_ram_address == 0x2038AF18
    assert cfg.heating.dm_disable_value == -1
    assert cfg.safety.min_room_temp is None
    assert cfg.safety.min_supply_temp == 20.0
    assert cfg.debug_website.enabled is False
    assert cfg.log.level == "info"
    assert cfg.data_dir == tmp_path


def test_example_file_equals_defaults(tmp_path: Path) -> None:
    assert load_config(REPO / "config.example.toml", data_dir=tmp_path) == load_config(
        None, data_dir=tmp_path
    )


def test_toml_parsing(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
[pump]
port = "/dev/ttyACM1"
offline_after = 90
[server]
listen_port = 1234
[defaults]
hot_water_mode = "eco"
[safety]
min_room_temp = 17
min_outdoor_temp = false
[log]
level = "DEBUG"
""",
    )
    cfg = load_config(path, data_dir=tmp_path)
    assert cfg.pump.port == "/dev/ttyACM1"
    assert cfg.pump.offline_after == 90.0
    assert isinstance(cfg.pump.offline_after, float)
    assert cfg.server.listen_port == 1234
    assert cfg.defaults.hot_water_mode == "eco"
    assert cfg.safety.min_room_temp == 17.0
    assert cfg.safety.min_outdoor_temp is None
    assert cfg.log.level == "debug"


def test_empty_hot_water_mode_is_none(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, '[defaults]\nhot_water_mode = ""\n'), data_dir=tmp_path)
    assert cfg.defaults.hot_water_mode is None


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("bogus = 1\n", "bogus"),
        ("[pump]\nprot = 'x'\n", "pump.prot"),
        ("[nope]\na = 1\n", "nope"),
        ("[heating]\ndm_max_x = 1\n", "heating.dm_max_x"),
    ],
)
def test_unknown_key_names_dotted_key(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, text), data_dir=tmp_path)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[heating]\ndm_disable_value = 5\n", "heating.dm_disable_value"),
        ("[heating]\ndm_disable_value = -20000\n", "heating.dm_disable_value"),
        ("[heating]\ndm_write_method = 'eeprom'\n", "heating.dm_write_method"),
        ("[heating]\ndm_write_policy = 'never'\n", "heating.dm_write_policy"),
        ("[watchdog]\ntimeout = 60\n", "watchdog.timeout"),
        ("[poll]\ninterval = 30\n", "poll.interval"),
        ("[defaults]\nhot_water_mode = 'boil'\n", "defaults.hot_water_mode"),
        ("[log]\nlevel = 'loud'\n", "log.level"),
        ("[pump]\nri_batch_size = 0\n", "pump.ri_batch_size"),
        ("[pump]\nri_batch_size = 11\n", "pump.ri_batch_size"),
        ("[pump]\nmax_line_length = 62\n", "pump.max_line_length"),
        ("[pump]\ndaily_check_time = '3pm'\n", "pump.daily_check_time"),
        ("[server]\nlisten_port = 70000\n", "server.listen_port"),
    ],
)
def test_range_errors(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, text), data_dir=tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "[pump]\nport = 5\n",
        "[pump]\nrts = 'yes'\n",
        "[server]\nlisten_port = 1.5\n",
        "[server]\nlisten_port = true\n",
    ],
)
def test_type_errors(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text), data_dir=tmp_path)


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[pump\n"), data_dir=tmp_path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml", data_dir=tmp_path)


def test_overrides(tmp_path: Path) -> None:
    path = write(tmp_path, "[server]\nlisten_port = 1000\n")
    cfg = load_config(
        path,
        data_dir=tmp_path,
        overrides={"server.listen_port": 2000, "debug_website.enabled": True, "log.level": "warning"},
    )
    assert cfg.server.listen_port == 2000
    assert cfg.debug_website.enabled is True
    assert cfg.log.level == "warning"


def test_overrides_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="server.nope"):
        load_config(None, data_dir=tmp_path, overrides={"server.nope": 1})
    with pytest.raises(ConfigError, match="log.level"):
        load_config(None, data_dir=tmp_path, overrides={"log.level": "loud"})
    with pytest.raises(ConfigError, match="nosection.x"):
        load_config(None, data_dir=tmp_path, overrides={"nosection.x": 1})


def test_hex_address(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, '[heating]\ndm_ram_address = "0x2038af20"\n'), data_dir=tmp_path)
    assert cfg.heating.dm_ram_address == 0x2038AF20
    cfg = load_config(write(tmp_path, "[heating]\ndm_ram_address = 1000\n"), data_dir=tmp_path)
    assert cfg.heating.dm_ram_address == 1000
    with pytest.raises(ConfigError, match="dm_ram_address"):
        load_config(write(tmp_path, '[heating]\ndm_ram_address = "zzz"\n'), data_dir=tmp_path)


def test_data_dir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIBE_SERVICE_USB_DATA_DIR", str(tmp_path / "env"))
    assert load_config(None).data_dir == tmp_path / "env"
    assert load_config(None, data_dir=tmp_path / "arg").data_dir == tmp_path / "arg"
    monkeypatch.delenv("NIBE_SERVICE_USB_DATA_DIR")
    assert load_config(None).data_dir == Path("~/.local/share/nibe-service-usb").expanduser()


def test_format_config_roundtrip_keys(tmp_path: Path) -> None:
    text = format_config(load_config(None, data_dir=tmp_path))
    assert "[heating]" in text
    assert 'dm_ram_address = "0x2038af18"' in text
    assert "# min_room_temp = (unset)" in text
