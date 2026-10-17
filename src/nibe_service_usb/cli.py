"""Command line interface: run, show-key, rotate-key, probe, simulate, check-config."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from nibe_service_usb import __version__
from nibe_service_usb.config import (
    ENV_CONFIG,
    LOG_LEVELS,
    Config,
    ConfigError,
    format_config,
    load_config,
)
from nibe_service_usb.keystore import KeyStore, format_key_grouped
from nibe_service_usb.pump.registers import SensorDefError, load_alarm_defs, load_sensor_defs

DEFAULT_CONFIG_PATH = Path("/etc/nibe-service-usb/config.toml")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _debug_website(value: str) -> tuple[str | None, int]:
    """Parse ``[HOST:]PORT`` (``PORT`` 0 picks a free port)."""
    host: str | None = None
    port_text = value
    if ":" in value:
        host, _, port_text = value.rpartition(":")
        host = host.strip("[]") or None
    try:
        port = int(port_text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected [HOST:]PORT, got {value!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range: {port}")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        help=f"config file (default: ${ENV_CONFIG}, then {DEFAULT_CONFIG_PATH})",
    )
    common.add_argument("--data-dir", type=Path, help="directory for auth.key and server_id")
    common.add_argument("--log-level", type=str.lower, choices=LOG_LEVELS, help="override log.level")

    parser = argparse.ArgumentParser(prog="nibe-service-usb", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = sub.add_parser("run", parents=[common], help="run the gateway")
    sim = sub.add_parser("simulate", parents=[common], help="run against the in-memory pump simulator")
    for p in (run, sim):
        p.add_argument(
            "--debug-website",
            type=_debug_website,
            metavar="[HOST:]PORT",
            help="serve the local debug web UI (PORT 0 = pick a free port)",
        )
    sub.add_parser("show-key", parents=[common], help="print the auth key for pasting into Home Assistant")
    sub.add_parser("rotate-key", parents=[common], help="generate a new auth key")
    sub.add_parser("probe", parents=[common], help="read identity, clock and a few registers, then exit")
    sub.add_parser("check-config", parents=[common], help="validate config and sensors.yaml, print values")
    return parser


def _config_path(arg: Path | None) -> Path | None:
    if arg is not None:
        return arg
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None


def _load(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if args.log_level:
        overrides["log.level"] = args.log_level
    debug = getattr(args, "debug_website", None)
    if debug is not None:
        host, port = debug
        overrides["debug_website.enabled"] = True
        overrides["debug_website.port"] = port
        if host:
            overrides["debug_website.host"] = host
    path = _config_path(args.config)
    if args.command == "simulate":
        present = _config_sets(path)
        if "discovery.enabled" not in present:
            overrides["discovery.enabled"] = False
        if "server.listen_port" not in present:
            overrides["server.listen_port"] = 0
    return load_config(path, data_dir=args.data_dir, overrides=overrides)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _print_key(key: bytes) -> None:
    print(f"Key (hex):    {KeyStore.format_key(key)}")
    print(f"Key (base32): {format_key_grouped(key)}")


def _sensors_path(config: Config) -> Path | None:
    return Path(config.poll.sensors_file) if config.poll.sensors_file else None


def cmd_show_key(config: Config) -> int:
    store = KeyStore(config.data_dir)
    if not (config.data_dir / "auth.key").exists():
        # Never create the key here: run as the wrong user (e.g. root) this would leave
        # a key file the service cannot read. The server creates it on its first start.
        print(f"No key in {config.data_dir} yet. Start the server once (it creates the key), "
              "or check --data-dir / the user you run this as.", file=sys.stderr)
        return 1
    _print_key(store.load_or_create())
    return EXIT_OK


def cmd_rotate_key(config: Config) -> int:
    store = KeyStore(config.data_dir)
    store.load_or_create()
    _print_key(store.rotate())
    print("Restart the server to use the new key; clients must then re-authenticate with it.")
    return EXIT_OK


def cmd_check_config(config: Config) -> int:
    sensors = load_sensor_defs(_sensors_path(config))
    alarms = load_alarm_defs(_sensors_path(config))
    print(format_config(config))
    tiers = Counter(s.tier for s in sensors)
    enabled = sum(1 for s in sensors if s.enabled)
    print(f"sensors: {len(sensors)} defined, {enabled} enabled")
    print("tiers: " + ", ".join(f"{tier}={count}" for tier, count in sorted(tiers.items())))
    print(f"alarm flag registers: {len(alarms)}")
    print("configuration OK")
    return EXIT_OK


def _print_probe(result: dict[str, Any]) -> None:
    print(f"port:   {result.get('port')}")
    print(f"online: {result.get('online')}")
    if "error" in result:
        print(f"error:  {result['error']}")
    for title in ("identity", "clock"):
        section = result.get(title)
        if section:
            print(f"{title}:")
            for key, value in section.items():
                print(f"  {key}: {value}")
    registers = result.get("registers")
    if registers:
        print("registers:")
        for register, value in registers.items():
            print(f"  {register}: {value}")


def cmd_probe(config: Config) -> int:
    from nibe_service_usb.app import probe

    result = asyncio.run(probe(config))
    _print_probe(result)
    return EXIT_OK if result.get("online") else EXIT_ERROR


async def _serve(app: Any) -> None:
    loop = asyncio.get_running_loop()
    stop_tasks: list[asyncio.Task] = []

    def request_stop() -> None:
        if not stop_tasks:
            stop_tasks.append(loop.create_task(app.stop()))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)
    try:
        await app.run()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if stop_tasks:
            await asyncio.gather(*stop_tasks)


def _config_sets(config_path: Path | None) -> set[str]:
    """Dotted keys explicitly present in the config file."""
    if config_path is None:
        return set()
    try:
        with open(config_path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    return {f"{sec}.{key}" for sec, table in raw.items() if isinstance(table, dict) for key in table}


def cmd_run(config: Config) -> int:
    from nibe_service_usb.app import App

    asyncio.run(_serve(App(config)))
    return EXIT_OK


def cmd_simulate(config: Config) -> int:
    from nibe_service_usb.app import App
    from nibe_service_usb.pump.simulator import SimulatedPumpClient

    sensors = load_sensor_defs(_sensors_path(config))
    app = App(config, pump=SimulatedPumpClient(sensors, realtime=True))

    async def main_() -> None:
        loop = asyncio.get_running_loop()
        serving = loop.create_task(_serve(app))
        # Wait until the server is up (or serving failed), then tell the user how to connect.
        ready = loop.create_task(app.ready.wait())
        await asyncio.wait({ready, serving}, return_when=asyncio.FIRST_COMPLETED)
        ready.cancel()
        if app.ready.is_set():
            print("Simulator running (no pump attached).")
            _print_key(app.keystore.key)
            print(f"Listening on {config.server.listen_host}:{app.tcp_port}")
            if app.debug_url:
                print(f"Debug website: {app.debug_url}")
            sys.stdout.flush()
        await serving

    asyncio.run(main_())
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load(args)
        if args.command == "check-config":
            return cmd_check_config(config)
        if args.command in ("show-key", "rotate-key"):
            _setup_logging("warning")
            return cmd_show_key(config) if args.command == "show-key" else cmd_rotate_key(config)
        _setup_logging(config.log.level)
        if args.command == "probe":
            return cmd_probe(config)
        if args.command == "simulate":
            return cmd_simulate(config)
        return cmd_run(config)
    except (ConfigError, SensorDefError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
