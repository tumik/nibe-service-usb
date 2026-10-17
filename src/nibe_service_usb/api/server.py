"""asyncio TCP server speaking the JSON Lines protocol of ``protocol.md``."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import socket
import time

from .. import __version__
from ..core.errors import CommandError
from . import auth
from .commands import COMMANDS, CommandContext, dispatch, validate_params
from .framing import FramingError, LineTooLong, classify, read_message
from .session import Session

logger = logging.getLogger(__name__)

SERVER_NAME = "nibe-service-usb"
MAX_LINE = 65536
MAX_MALFORMED = 3
HANDSHAKE_TIMEOUT = 10.0
AUTH_FAIL_DELAY = 1.0


def _result_ok(id_: int, data: dict) -> dict:
    return {"id": id_, "ok": True, "data": data}


def _result_err(id_: int | None, code: str, message: str = "") -> dict:
    return {"id": id_, "ok": False, "error": {"code": code, "message": message}}


def _event(name: str, data: dict) -> dict:
    return {"event": name, "ts": time.time(), "data": data}


class TcpServer:
    def __init__(
        self,
        config,
        ctx: CommandContext,
        *,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        auth_fail_delay: float = AUTH_FAIL_DELAY,
        limiter: auth.AuthLimiter | None = None,
        send_timeout: float = 10.0,
    ) -> None:
        self._config = config
        self._server_config = config.server
        self._ctx = ctx
        self._handshake_timeout = handshake_timeout
        self._auth_fail_delay = auth_fail_delay
        self._limiter = limiter if limiter is not None else auth.AuthLimiter()
        self._send_timeout = send_timeout
        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[int, Session] = {}
        self._writers: dict[int, asyncio.StreamWriter] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        self._unsubs: list = []
        self._stopping = False

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        cfg = self._server_config
        self._server = await asyncio.start_server(
            self._handle_connection, cfg.listen_host, cfg.listen_port, limit=MAX_LINE + 1
        )
        bus = self._ctx.bus
        self._unsubs = [
            bus.subscribe("sensor_changed", self._on_sensor_changed),
            bus.subscribe("status_changed", self._on_status_changed),
            bus.subscribe("shutdown", self._on_shutdown),
        ]
        logger.info("API listening on %s:%d", cfg.listen_host, self.port)

    async def stop(self, reason: str = "shutdown") -> None:
        if self._stopping:
            return
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._server is not None:
            self._server.close()
        await self._close_sessions(reason)
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=5.0)
            for task in pending:
                task.cancel()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5.0)
            self._server = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self._server_config.listen_port
        return self._server.sockets[0].getsockname()[1]

    @property
    def client_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.authenticated and not s.dead)

    # ------------------------------------------------------------ broadcast

    async def _close_sessions(self, reason: str) -> None:
        sessions = [s for s in self._sessions.values() if s.authenticated]
        await asyncio.gather(*(s.send(_event("shutdown", {"reason": reason})) for s in sessions))
        writers = list(self._writers.values())
        for session in list(self._sessions.values()):
            session.close()
        for writer in writers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2.0)

    async def _broadcast(self, sessions: list[tuple[Session, dict]]) -> None:
        if sessions:
            await asyncio.gather(*(s.send(msg) for s, msg in sessions))

    async def _on_sensor_changed(self, topic: str, data: dict) -> None:
        values = data.get("values") or {}
        out = []
        for session in list(self._sessions.values()):
            if not session.authenticated or session.dead:
                continue
            chosen = {k: v for k, v in values.items() if session.wants_sensor(k)}
            if chosen:
                out.append((session, _event("sensor_update", {"values": chosen})))
        await self._broadcast(out)

    async def _on_status_changed(self, topic: str, data: dict) -> None:
        payload = dict(data)
        payload["clients"] = self.client_count
        msg = _event("status", payload)
        await self._broadcast(
            [(s, msg) for s in list(self._sessions.values()) if s.authenticated and not s.dead]
        )

    async def _on_shutdown(self, topic: str, data: dict) -> None:
        await self._close_sessions(str(data.get("reason", "shutdown")))

    # ------------------------------------------------------------ connection

    def _update_clients(self) -> None:
        self._ctx.state.clients = self.client_count

    @staticmethod
    def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)
        except OSError:
            logger.debug("Could not enable TCP keepalive", exc_info=True)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peername[0])
        peer = f"{ip}:{peername[1]}"
        max_clients = self._server_config.max_clients
        if self._stopping or len(self._sessions) >= 2 * max_clients or self.client_count >= max_clients:
            logger.warning("Connection from %s refused: too many connections", peer)
            writer.close()
            return
        self._enable_keepalive(writer)
        session = Session(next(self._ids), peer, writer, send_timeout=self._send_timeout)
        self._sessions[session.id] = session
        self._writers[session.id] = writer
        logger.info("Connection from %s", peer)
        try:
            await self._serve(session, ip, reader)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Connection %s failed", peer)
        finally:
            was_authenticated = session.authenticated
            session.close()
            self._sessions.pop(session.id, None)
            self._writers.pop(session.id, None)
            if was_authenticated:
                self._update_clients()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            logger.info("Connection from %s closed", peer)

    async def _serve(self, session: Session, ip: str, reader: asyncio.StreamReader) -> None:
        nonce = auth.make_nonce()
        await session.send(
            _event(
                "hello",
                {
                    "server": SERVER_NAME,
                    "version": __version__,
                    "proto": 1,
                    "server_id": self._ctx.keystore.server_id,
                    "nonce": nonce,
                    "auth": "hmac-sha256",
                },
            )
        )
        try:
            ok = await asyncio.wait_for(self._handshake(session, ip, reader, nonce), self._handshake_timeout)
        except TimeoutError:
            logger.warning("Handshake with %s timed out", session.peer)
            return
        if not ok:
            return
        await self._request_loop(session, reader)

    async def _read(
        self, session: Session, reader: asyncio.StreamReader, malformed: list[int]
    ) -> dict | None:
        """Read the next valid request; handles malformed lines. ``None`` ends the connection."""
        while True:
            try:
                msg = await read_message(reader, MAX_LINE)
            except LineTooLong:
                logger.warning("Line too long from %s, closing", session.peer)
                return None
            except FramingError as err:
                malformed[0] += 1
                await session.send(_result_err(None, "invalid_params", str(err)))
                if malformed[0] >= MAX_MALFORMED:
                    logger.warning("Too many malformed lines from %s, closing", session.peer)
                    return None
                continue
            if msg is None:
                return None
            if classify(msg) != "request":
                malformed[0] += 1
                await session.send(_result_err(None, "invalid_params", "not a request"))
                if malformed[0] >= MAX_MALFORMED:
                    return None
                continue
            return msg

    async def _handshake(
        self, session: Session, ip: str, reader: asyncio.StreamReader, server_nonce: str
    ) -> bool:
        malformed = [0]
        msg = await self._read(session, reader, malformed)
        if msg is None:
            return False
        id_ = msg["id"]
        if msg["cmd"] != "auth":
            await session.send(_result_err(id_, "auth_required", "authenticate first"))
            return False

        async def fail(code: str, message: str = "", *, delay: float | None = None) -> bool:
            await asyncio.sleep(self._auth_fail_delay if delay is None else delay)
            await session.send(_result_err(id_, code, message))
            return False

        if self._limiter.is_locked(ip):
            logger.warning("Auth attempt from locked out address %s", ip)
            return await fail("auth_failed", "too many failures", delay=self._limiter.delay)
        try:
            params = validate_params(COMMANDS["auth"], msg.get("params"))
        except CommandError as err:
            self._auth_failure(session, ip, f"invalid params: {err.message}")
            return await fail(err.code, err.message)
        if params["proto"] not in auth.SUPPORTED_PROTO:
            self._auth_failure(session, ip, f"unsupported proto {params['proto']}")
            return await fail("auth_failed", "unsupported proto")
        client_nonce = params["client_nonce"]
        try:
            nonce_len = len(auth.decode_nonce(client_nonce))
        except ValueError:
            nonce_len = 0
        key = self._ctx.keystore.key
        if nonce_len < auth.MIN_CLIENT_NONCE_BYTES or not auth.verify_proof(
            key, server_nonce, client_nonce, params["proof"]
        ):
            self._auth_failure(session, ip, "bad proof")
            return await fail("auth_failed", "authentication failed")
        if self.client_count >= self._server_config.max_clients:
            logger.warning("Too many clients, refusing %s", session.peer)
            return False
        self._limiter.record_success(ip)
        session.authenticated = True
        session.client = params["client"][:100]
        self._update_clients()
        self._ctx.watchdog.heartbeat()
        logger.info("Client %s authenticated (%s)", session.peer, session.client)
        await session.send(
            _result_ok(
                id_,
                {"server_proof": auth.compute_server_proof(key, server_nonce, client_nonce)},
            )
        )
        return True

    def _auth_failure(self, session: Session, ip: str, why: str) -> None:
        locked = self._limiter.record_failure(ip)
        suffix = " (now locked out)" if locked else ""
        logger.warning("Authentication failed for %s: %s%s", session.peer, why, suffix)

    async def _request_loop(self, session: Session, reader: asyncio.StreamReader) -> None:
        malformed = [0]
        idle = self._server_config.idle_timeout
        while not session.dead:
            try:
                msg = await asyncio.wait_for(self._read(session, reader, malformed), idle)
            except TimeoutError:
                logger.info("Closing idle connection %s", session.peer)
                return
            if msg is None:
                return
            task = asyncio.get_running_loop().create_task(self._run_request(session, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_request(self, session: Session, msg: dict) -> None:
        id_ = msg["id"]
        cmd = msg["cmd"]
        params = msg.get("params") or {}
        logger.info("Command %s from %s (%s) params=%s", cmd, session.peer, session.client, params)
        try:
            data = await dispatch(self._ctx, session, cmd, params)
            reply = _result_ok(id_, data)
        except CommandError as err:
            reply = _result_err(id_, err.code, err.message)
        except Exception:
            logger.exception("Unhandled error in command %s", cmd)
            reply = _result_err(id_, "internal", "internal error")
        await session.send(reply)
