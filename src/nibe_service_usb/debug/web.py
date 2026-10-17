"""Debug website: one HTML page, a Server-Sent Events stream and a command endpoint.

Stdlib only. A minimal HTTP/1.1 implementation on ``asyncio.start_server``
serves three routes (plus ``/state`` for the initial page load):

* ``GET /``        the single-page UI (``index.html`` from the package)
* ``GET /state``   JSON ``{status, sensors, commands, ...}``
* ``GET /events``  ``text/event-stream`` with ``snapshot``, ``sensor_update``,
                   ``status``, ``pump_state`` and ``shutdown`` events
* ``POST /cmd``    JSON ``{cmd, params}`` through the same dispatcher and
                   allowlist as the TCP API

There is no authentication; the server binds to loopback by default. As a
cheap defence against other web pages talking to it from the owner's browser,
``POST /cmd`` requires ``Content-Type: application/json`` (which forces a CORS
preflight that is never answered) and rejects a cross-origin ``Origin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import time
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from ..api.commands import COMMANDS, CommandContext, dispatch
from ..api.session import Session
from ..core.errors import CommandError

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10.0
WRITE_TIMEOUT = 10.0
KEEPALIVE_INTERVAL = 15.0
MAX_STREAMS = 16
STREAM_QUEUE_SIZE = 200

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}

PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)

BUS_TOPICS = {
    "sensor_changed": "sensor_update",
    "status_changed": "status",
    "pump_state": "pump_state",
    "shutdown": "shutdown",
}


class _HttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or STATUS_TEXT.get(status, ""))
        self.status = status
        self.message = message or STATUS_TEXT.get(status, "")


class _NoWatchdog:
    """Stands in for the watchdog so debug commands do not count as an HA heartbeat."""

    def heartbeat(self) -> None:
        return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _format_peer(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer) if peer else "?"


class DebugWebServer:
    def __init__(
        self,
        config: Any,
        ctx: CommandContext,
        bus: Any,
        *,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.config = config
        self.ctx = ctx
        self.bus = bus
        self.host = host if host is not None else getattr(config, "host", "127.0.0.1")
        self.port = port if port is not None else getattr(config, "port", 0)
        self.feeds_watchdog = bool(getattr(config, "feeds_watchdog", False))
        self.keepalive_interval = KEEPALIVE_INTERVAL
        self._cmd_ctx = ctx if self.feeds_watchdog else dataclasses.replace(ctx, watchdog=_NoWatchdog())
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()
        self._streams = 0
        self._session_ids = 0

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        if not _is_loopback(self.host):
            logger.warning(
                "Debug website is bound to %s and has no authentication: anyone who can reach it "
                "can change hot water and heating settings",
                self.host,
            )
        self._server = await asyncio.start_server(
            self._on_connect, self.host, self.port, limit=MAX_REQUEST_BYTES
        )
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        logger.info(
            "Debug website listening on %s (feeds_watchdog=%s)",
            self.url,
            "yes" if self.feeds_watchdog else "no",
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
        logger.info("Debug website stopped")

    # ------------------------------------------------------------ connection

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError):
            pass
        except Exception:
            logger.exception("Debug website request failed")
        finally:
            if task is not None:
                self._connections.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _format_peer(writer)
        try:
            method, path, headers, body = await asyncio.wait_for(self._read_request(reader), REQUEST_TIMEOUT)
        except _HttpError as err:
            await self._send_error(writer, err.status, err.message)
            return
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return

        route = {"/": ("GET",), "/state": ("GET",), "/events": ("GET",), "/cmd": ("POST",)}.get(path)
        if route is None:
            await self._send_error(writer, 404, "not found")
            return
        if method not in route:
            await self._send_error(writer, 405, "method not allowed", {"Allow": route[0]})
            return

        if path == "/":
            await self._serve_page(writer)
        elif path == "/state":
            await self._send(writer, 200, "application/json", _dumps(self._snapshot()).encode())
        elif path == "/events":
            await self._serve_events(reader, writer, peer)
        else:
            await self._serve_cmd(writer, peer, headers, body)

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        total = 0
        try:
            line = await reader.readline()
        except ValueError as err:
            raise _HttpError(431, "request line too long") from err
        if not line:
            raise ConnectionResetError
        total += len(line)
        parts = line.decode("latin-1").strip().split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise _HttpError(400, "malformed request line")
        method, target, _version = parts
        path = urlsplit(target).path or "/"

        headers: dict[str, str] = {}
        while True:
            try:
                line = await reader.readline()
            except ValueError as err:
                raise _HttpError(431, "header line too long") from err
            if not line:
                raise ConnectionResetError
            total += len(line)
            if total > MAX_REQUEST_BYTES:
                raise _HttpError(431, "request too large")
            if line in (b"\r\n", b"\n"):
                break
            name, sep, value = line.decode("latin-1").partition(":")
            if not sep:
                raise _HttpError(400, "malformed header")
            headers[name.strip().lower()] = value.strip()

        if "transfer-encoding" in headers:
            raise _HttpError(501, "chunked request bodies are not supported")
        body = b""
        if method == "POST":
            if "content-length" not in headers:
                raise _HttpError(411, "Content-Length required")
            try:
                length = int(headers["content-length"])
            except ValueError as err:
                raise _HttpError(400, "bad Content-Length") from err
            if length < 0:
                raise _HttpError(400, "bad Content-Length")
            if total + length > MAX_REQUEST_BYTES:
                raise _HttpError(413, "request too large")
            body = await reader.readexactly(length)
        return method, path, headers, body

    # ------------------------------------------------------------ responses

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
        extra: dict[str, str] | None = None,
    ) -> None:
        head = [
            f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'Status')}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
        ]
        for key, value in (extra or {}).items():
            head.append(f"{key}: {value}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)

    async def _send_error(
        self, writer: asyncio.StreamWriter, status: int, message: str, extra: dict[str, str] | None = None
    ) -> None:
        body = _dumps({"ok": False, "error": {"code": "http_error", "message": message}}).encode()
        with contextlib.suppress(ConnectionError, OSError, TimeoutError):
            await self._send(writer, status, "application/json", body, extra)

    async def _serve_page(self, writer: asyncio.StreamWriter) -> None:
        try:
            html = resources.files(__package__).joinpath("index.html").read_bytes()
        except (OSError, FileNotFoundError):
            logger.exception("index.html is missing from the package")
            await self._send_error(writer, 500, "index.html missing")
            return
        await self._send(
            writer,
            200,
            "text/html; charset=utf-8",
            html,
            {"Content-Security-Policy": PAGE_CSP, "Referrer-Policy": "no-referrer"},
        )

    # ------------------------------------------------------------ /state, /events

    def _snapshot(self) -> dict:
        snap = self.ctx.state.snapshot()
        status = dict(snap.get("status", {}))
        status.setdefault("uptime", round(time.monotonic() - self.ctx.started, 1))
        return {
            "status": status,
            "sensors": snap.get("sensors", {}),
            "commands": sorted(COMMANDS),
            "feeds_watchdog": self.feeds_watchdog,
            "now": time.time(),
        }

    async def _serve_events(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str
    ) -> None:
        if self._streams >= MAX_STREAMS:
            await self._send_error(writer, 503, "too many event streams")
            return
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue(STREAM_QUEUE_SIZE)

        def make_callback(event: str):
            def callback(topic: str, data: dict) -> None:
                payload = dict(data) if isinstance(data, dict) else {"value": data}
                if event == "sensor_update":
                    payload["ts"] = time.time()
                try:
                    queue.put_nowait((event, payload))
                except asyncio.QueueFull:
                    # Consumer is hopelessly behind: end the stream, the browser reconnects.
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.get_nowait()
                    queue.put_nowait(None)

            return callback

        unsubscribers = [
            self.bus.subscribe(topic, make_callback(event)) for topic, event in BUS_TOPICS.items()
        ]
        self._streams += 1
        logger.info("Debug website event stream opened by %s", peer)
        eof = asyncio.ensure_future(reader.read(1))  # returns b"" when the client goes away
        try:
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream; charset=utf-8\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: close\r\n"
                "X-Accel-Buffering: no\r\n"
                "X-Content-Type-Options: nosniff\r\n\r\n"
            )
            writer.write(head.encode("latin-1"))
            await self._write_event(writer, "snapshot", self._snapshot(), retry=3000)
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _pending = await asyncio.wait(
                    {getter, eof}, timeout=self.keepalive_interval, return_when=asyncio.FIRST_COMPLETED
                )
                if getter not in done:
                    getter.cancel()
                if eof in done:
                    return
                if getter not in done:
                    writer.write(b": keep-alive\n\n")
                    await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
                    continue
                item = getter.result()
                if item is None:
                    return
                event, payload = item
                await self._write_event(writer, event, payload)
                if event == "shutdown":
                    return
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()
            eof.cancel()
            self._streams -= 1
            logger.info("Debug website event stream closed (%s)", peer)

    @staticmethod
    async def _write_event(
        writer: asyncio.StreamWriter, event: str, data: Any, *, retry: int | None = None
    ) -> None:
        text = f"retry: {retry}\n" if retry is not None else ""
        text += f"event: {event}\ndata: {_dumps(data)}\n\n"
        writer.write(text.encode())
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)

    # ------------------------------------------------------------ POST /cmd

    async def _serve_cmd(
        self, writer: asyncio.StreamWriter, peer: str, headers: dict[str, str], body: bytes
    ) -> None:
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            await self._send_error(writer, 415, "Content-Type must be application/json")
            return
        origin = headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != headers.get("host", ""):
            await self._send_error(writer, 403, "cross-origin request refused")
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            await self._send_error(writer, 400, "request body is not valid JSON")
            return

        cmd = request.get("cmd") if isinstance(request, dict) else None
        params = request.get("params") if isinstance(request, dict) else None
        if not isinstance(cmd, str):
            result: dict = {
                "ok": False,
                "error": {"code": "invalid_params", "message": "body must be an object with a string 'cmd'"},
            }
            logger.info("Debug website command from %s rejected: malformed request", peer)
        else:
            self._session_ids += 1
            session = Session(-self._session_ids, peer)
            session.authenticated = True
            session.client = "debug-web"
            try:
                data = await dispatch(self._cmd_ctx, session, cmd, params)
                result = {"ok": True, "data": data}
                logger.info("Debug website command %s %s from %s: ok", cmd, _dumps(params or {}), peer)
            except CommandError as err:
                result = {"ok": False, "error": {"code": err.code, "message": err.message}}
                logger.info(
                    "Debug website command %s %s from %s: error %s", cmd, _dumps(params or {}), peer, err.code
                )
        await self._send(writer, 200, "application/json", _dumps(result).encode())
