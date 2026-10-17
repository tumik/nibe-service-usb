"""JSON Lines framing: one UTF-8 JSON object per ``\\n`` terminated line."""

from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_LINE = 65536


class FramingError(Exception):
    """A line could not be turned into a JSON object."""


class LineTooLong(FramingError):
    """A line exceeded the maximum length; the connection must be closed."""


async def read_message(reader: asyncio.StreamReader, max_line: int = MAX_LINE) -> dict | None:
    """Read one message. Returns ``None`` on EOF.

    Raises ``LineTooLong`` for an over-long line and ``FramingError`` for
    invalid UTF-8 / JSON or a JSON value that is not an object.
    """
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError) as err:
        raise LineTooLong("line too long") from err
    if not line:
        return None
    if not line.endswith(b"\n"):
        return None  # connection closed in the middle of a line
    if len(line) > max_line + 1:  # the terminator does not count
        raise LineTooLong("line too long")
    try:
        obj = json.loads(line)
    except ValueError as err:  # includes UnicodeDecodeError and JSONDecodeError
        raise FramingError("invalid JSON") from err
    if not isinstance(obj, dict):
        raise FramingError("message is not a JSON object")
    return obj


def encode_message(obj: Any) -> bytes:
    """Compact JSON followed by a newline."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"


def is_request(msg: dict) -> bool:
    """A request has an integer ``id`` (not a bool), a string ``cmd`` and optional object ``params``."""
    id_ = msg.get("id")
    return (
        isinstance(id_, int)
        and not isinstance(id_, bool)
        and isinstance(msg.get("cmd"), str)
        and isinstance(msg.get("params", {}), dict)
    )


def classify(msg: dict) -> str:
    """``"request"`` or ``"invalid"``."""
    return "request" if is_request(msg) else "invalid"
