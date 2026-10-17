"""Command allowlist: schemas, handlers and the dispatcher.

There is deliberately no generic register access here. Every command is a
fixed entry in ``COMMANDS`` with a closed parameter schema.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import CommandError
from ..pump.interface import PumpOffline, PumpTimeout, WriteFailed
from .session import Session

logger = logging.getLogger(__name__)

BOOST_MIN_INTERVAL = 600.0
REFRESH_MIN_INTERVAL = 60.0


@dataclass
class CommandContext:
    config: Any
    state: Any
    pump: Any
    poller: Any
    heating: Any
    hot_water: Any
    watchdog: Any
    keystore: Any
    bus: Any
    # Extensions (not part of the base contract): server start time and the
    # monotonic timestamps of the last rate limited commands.
    started: float = field(default_factory=time.monotonic)
    last_called: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ParamSpec:
    """``type`` is one of ``str``, ``int``, ``float``, ``bool``, ``list[str]``."""

    type: str
    choices: Sequence | None = None
    required: bool = True


@dataclass
class CommandSpec:
    handler: Callable[[CommandContext, Session, dict], Awaitable[dict]]
    params: dict[str, ParamSpec]
    auth_required: bool = True


def _type_ok(spec: ParamSpec, value: Any) -> bool:
    match spec.type:
        case "str":
            return isinstance(value, str)
        case "int":
            return isinstance(value, int) and not isinstance(value, bool)
        case "float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "bool":
            return isinstance(value, bool)
        case "list[str]":
            return isinstance(value, list) and all(isinstance(v, str) for v in value)
    raise ValueError(f"unknown param type {spec.type!r}")


def validate_params(spec: CommandSpec, params: Any) -> dict:
    """Check ``params`` against the schema; raises ``invalid_params`` naming the field."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise CommandError("invalid_params", "params must be an object")
    for key in params:
        if key not in spec.params:
            raise CommandError("invalid_params", f"unknown parameter: {key}")
    for name, pspec in spec.params.items():
        if name not in params:
            if pspec.required:
                raise CommandError("invalid_params", f"missing parameter: {name}")
            continue
        value = params[name]
        if not _type_ok(pspec, value):
            raise CommandError("invalid_params", f"parameter {name} must be of type {pspec.type}")
        if pspec.choices is not None and value not in pspec.choices:
            choices = "|".join(str(c) for c in pspec.choices)
            raise CommandError("invalid_params", f"parameter {name} must be one of {choices}")
    return dict(params)


def _rate_limit(ctx: CommandContext, name: str, interval: float) -> None:
    last = ctx.last_called.get(name)
    if last is not None:
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            raise CommandError(
                "rate_limited", f"{name} is limited to once per {interval:g} s, retry in {wait:.0f} s"
            )


_background: set[asyncio.Task] = set()


def _spawn(coro: Awaitable[None], name: str) -> None:
    async def runner() -> None:
        try:
            await coro
        except Exception:
            logger.exception("Background task %s failed", name)

    task = asyncio.get_running_loop().create_task(runner(), name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------- handlers


async def _auth(ctx: CommandContext, session: Session, params: dict) -> dict:
    # The handshake is performed by the connection handler before dispatch.
    raise CommandError("invalid_params", "already authenticated")


async def _ping(ctx: CommandContext, session: Session, params: dict) -> dict:
    state = ctx.state
    return {
        "uptime": round(time.monotonic() - ctx.started, 1),
        "pump_online": bool(state.pump.online),
        "ha_alive": bool(state.watchdog.ha_alive),
    }


async def _subscribe(ctx: CommandContext, session: Session, params: dict) -> dict:
    names = params.get("sensors", ["*"])
    wanted = {"*"} if "*" in names else set(names)
    try:
        await ctx.poller.ensure_fresh()
    except Exception:
        # Offline pump etc.: answer with what we have, values follow as events.
        logger.warning("ensure_fresh failed during subscribe", exc_info=True)
    snapshot = ctx.state.sensor_snapshot(None if "*" in wanted else sorted(wanted))
    session.subscriptions = wanted
    return {"poll_interval": int(ctx.config.poll.interval), "sensors": snapshot}


async def _unsubscribe(ctx: CommandContext, session: Session, params: dict) -> dict:
    session.subscriptions = None
    return {}


async def _list_sensors(ctx: CommandContext, session: Session, params: dict) -> dict:
    snapshot = ctx.state.sensor_snapshot(None)
    return {
        "sensors": {
            name: {k: v for k, v in meta.items() if k not in ("value", "ts", "failures")}
            for name, meta in snapshot.items()
        }
    }


async def _get_status(ctx: CommandContext, session: Session, params: dict) -> dict:
    status = dict(ctx.state.snapshot()["status"])
    status.setdefault("uptime", round(time.monotonic() - ctx.started, 1))
    status["clients"] = ctx.state.clients
    return status


async def _set_hot_water_mode(ctx: CommandContext, session: Session, params: dict) -> dict:
    return await ctx.hot_water.set_mode(params["mode"])


async def _trigger_boost(ctx: CommandContext, session: Session, params: dict) -> dict:
    _rate_limit(ctx, "trigger_hot_water_boost", BOOST_MIN_INTERVAL)
    ctx.last_called["trigger_hot_water_boost"] = time.monotonic()
    try:
        return await ctx.hot_water.trigger_periodic_increase()
    except BaseException:
        # A trigger that did not start does not use up the quota.
        ctx.last_called.pop("trigger_hot_water_boost", None)
        raise


async def _set_heating_request(ctx: CommandContext, session: Session, params: dict) -> dict:
    return await ctx.heating.set_mode(params["mode"])


async def _refresh(ctx: CommandContext, session: Session, params: dict) -> dict:
    _rate_limit(ctx, "refresh", REFRESH_MIN_INTERVAL)
    ctx.last_called["refresh"] = time.monotonic()
    _spawn(ctx.poller.poll_now(), "refresh")
    return {"queued": True}


COMMANDS: dict[str, CommandSpec] = {
    "auth": CommandSpec(
        _auth,
        {
            "proto": ParamSpec("int"),
            "client": ParamSpec("str"),
            "client_nonce": ParamSpec("str"),
            "proof": ParamSpec("str"),
        },
        auth_required=False,
    ),
    "ping": CommandSpec(_ping, {}),
    "subscribe": CommandSpec(_subscribe, {"sensors": ParamSpec("list[str]", required=False)}),
    "unsubscribe": CommandSpec(_unsubscribe, {}),
    "list_sensors": CommandSpec(_list_sensors, {}),
    "get_status": CommandSpec(_get_status, {}),
    "set_hot_water_mode": CommandSpec(
        _set_hot_water_mode, {"mode": ParamSpec("str", choices=("eco", "normal", "lux"))}
    ),
    "trigger_hot_water_boost": CommandSpec(_trigger_boost, {}),
    "set_heating_request": CommandSpec(
        _set_heating_request, {"mode": ParamSpec("str", choices=("auto", "enable", "disable"))}
    ),
    "refresh": CommandSpec(_refresh, {}),
}


async def dispatch(ctx: CommandContext, session: Session, cmd: str, params: dict | None) -> dict:
    """Validate and run one command; raises ``CommandError``."""
    spec = COMMANDS.get(cmd)
    if spec is None:
        raise CommandError("unknown_command", cmd)
    if spec.auth_required and not session.authenticated:
        raise CommandError("auth_required")
    clean = validate_params(spec, params)
    if session.authenticated:
        ctx.watchdog.heartbeat()
    session.touch()
    try:
        return await spec.handler(ctx, session, clean)
    except CommandError:
        raise
    except PumpOffline as err:
        raise CommandError("pump_offline", str(err)) from err
    except PumpTimeout as err:
        raise CommandError("pump_timeout", str(err)) from err
    except WriteFailed as err:
        raise CommandError("write_failed", str(err)) from err
    except Exception as err:
        logger.exception("Command %s failed", cmd)
        raise CommandError("internal", "internal error") from err
