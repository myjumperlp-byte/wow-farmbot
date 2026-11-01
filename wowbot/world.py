"""World-Verbindung: Header-Verschlüsselung, Paket-Framing und Dispatch.

Der World-Server benutzt zwei verschiedene Header:
  * Client -> Server: 2 Byte Größe (Big-Endian) + 4 Byte Opcode (Little-Endian)
  * Server -> Client: 2 Byte Größe (Big-Endian) + 2 Byte Opcode (Little-Endian),
    bei großen Paketen 3 Byte Größe (erstes Byte mit 0x80 markiert) + 2 Byte Opcode.

Verschlüsselt wird nur der Header (RC4), nie der Body.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time

from . import crypto, opcodes

log = logging.getLogger("wowbot.world")

CLIENT_BUILD = 12340
CLIENT_HEADER_SIZE = 6


class WorldError(Exception):
    """Verbindungs- oder Handshake-Fehler."""


class WorldClient:
    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self.crypt: crypto.HeaderCrypt | None = None
        self.handlers: dict[int, list] = {}
        self._waiters: dict[int, list[asyncio.Future]] = {}
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self.trace = False  # jedes Paket mitschreiben
        self.server_seed = 0
        self.client_seed = random.getrandbits(32)
        self.packets_received = 0

    # ------------------------------------------------------------------ Aufbau
    async def connect(self, host: str, port: int, timeout: float = 15.0) -> None:
        log.info("Verbinde mit World-Server %s:%s", host, port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            import socket as _socket

            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)

    async def handshake(self, account: str, session_key: bytes, realm_id: int) -> int:
        """Führt SMSG_AUTH_CHALLENGE / CMSG_AUTH_SESSION / SMSG_AUTH_RESPONSE durch."""
        opcode, body = await self._read_packet(encrypted=False)
        if opcode != opcodes.SMSG_AUTH_CHALLENGE:
            raise WorldError(f"Erwartet SMSG_AUTH_CHALLENGE, bekommen 0x{opcode:04X}")
        # Body: u32 unknown, u32 server_seed, danach 32 Füllbytes
        if len(body) < 8:
            raise WorldError("SMSG_AUTH_CHALLENGE ist zu kurz")
        self.server_seed = struct.unpack_from("<I", body, 4)[0]
        log.info("Auth-Challenge erhalten (Server-Seed 0x%08X)", self.server_seed)

        account_name = account.upper()
        digest = crypto.auth_session_digest(
            account_name, self.client_seed, self.server_seed, session_key
        )
        payload = (
            struct.pack("<I", CLIENT_BUILD)  # client_build
            + struct.pack("<I", 0)  # login_server_id
            + account_name.encode()
            + b"\x00"
            + struct.pack("<I", 0)  # login_server_type
            + struct.pack("<I", self.client_seed)
            + struct.pack("<I", 0)  # region_id
            + struct.pack("<I", 0)  # battleground_id
            + struct.pack("<I", realm_id)
            + struct.pack("<Q", 0)  # dos_response
            + digest
            + struct.pack("<I", 0)  # Addon-Info: Länge 0
        )
        await self.send(opcodes.CMSG_AUTH_SESSION, payload, encrypted=False)

        # Ab jetzt ist der Client-Header verschlüsselt, und auch der Server
        # verschlüsselt ab dem nächsten Paket.
        self.crypt = crypto.HeaderCrypt(session_key)
        self._start_tasks()

        body = await self.wait_for(opcodes.SMSG_AUTH_RESPONSE, timeout=20.0)
        result = body[0]
        if result != opcodes.AUTH_OK:
            raise WorldError(
                f"World-Auth fehlgeschlagen: {opcodes.WORLD_RESULT_NAMES.get(result, result)}"
            )
        log.info("World-Auth erfolgreich")
        return result

    def _start_tasks(self) -> None:
        self._tasks = [
            asyncio.create_task(self._read_loop(), name="world-reader"),
            asyncio.create_task(self._keepalive_loop(), name="world-keepalive"),
        ]

    # ------------------------------------------------------------------ Pakete
    async def send(self, opcode: int, body: bytes = b"", encrypted: bool = True) -> None:
        if self._writer is None:
            raise WorldError("World-Verbindung ist nicht offen")
        size = len(body) + 4  # Opcode (4 Byte) + Body
        header = struct.pack(">H", size) + struct.pack("<I", opcode)
        if encrypted and self.crypt is not None:
            header = self.crypt.encrypt(header)
        if self.trace:
            log.debug("-> %s (%d B)", opcodes.opcode_name(opcode), len(body))
        async with self._send_lock:
            try:
                self._writer.write(header + body)
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                self._closed = True
                raise WorldError("Verbindung zum World-Server verloren") from exc

    async def _read_packet(self, encrypted: bool = True) -> tuple[int, bytes]:
        assert self._reader is not None
        reader = self._reader
        first = await reader.readexactly(1)
        if encrypted and self.crypt is not None:
            first = self.crypt.decrypt(first)

        if first[0] & 0x80:
            rest = await reader.readexactly(4)
            if encrypted and self.crypt is not None:
                rest = self.crypt.decrypt(rest)
            size = ((first[0] & 0x7F) << 16) | (rest[0] << 8) | rest[1]
            opcode = rest[2] | (rest[3] << 8)
        else:
            rest = await reader.readexactly(3)
            if encrypted and self.crypt is not None:
                rest = self.crypt.decrypt(rest)
            size = (first[0] << 8) | rest[0]
            opcode = rest[1] | (rest[2] << 8)

        body_len = size - 2
        if body_len < 0:
            raise WorldError(f"Ungültige Paketgröße {size} für Opcode 0x{opcode:04X}")
        body = await reader.readexactly(body_len) if body_len else b""
        return opcode, body

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                opcode, body = await self._read_packet()
            except (asyncio.IncompleteReadError, ConnectionError, WorldError) as exc:
                if not self._closed:
                    log.warning("World-Verbindung beendet: %s", exc or "EOF")
                self._closed = True
                return

            self.packets_received += 1
            if self.trace:
                log.debug("<- %s (%d B)", opcodes.opcode_name(opcode), len(body))
            try:
                self._dispatch(opcode, body)
            except Exception:  # noqa: BLE001 - ein kaputtes Paket darf den Bot nicht töten
                log.exception("Fehler beim Verarbeiten von %s", opcodes.opcode_name(opcode))

            waiters = self._waiters.pop(opcode, [])
            for future in waiters:
                if not future.done():
                    future.set_result(body)

    def _dispatch(self, opcode: int, body: bytes) -> None:
        for handler in self.handlers.get(opcode, ()):
            handler(body)

    def on(self, opcode: int, handler) -> None:
        self.handlers.setdefault(opcode, []).append(handler)

    async def wait_for(self, opcode: int, timeout: float = 10.0) -> bytes:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(opcode, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._waiters[opcode] = [f for f in self._waiters.get(opcode, []) if f is not future]
            raise

    @property
    def connected(self) -> bool:
        return not self._closed and self._writer is not None

    # -------------------------------------------------------------- Hintergrund
    async def _keepalive_loop(self) -> None:
        """Hält die Verbindung offen und beantwortet Zeit-Sync-Anfragen des Servers."""
        while not self._closed:
            await asyncio.sleep(25.0)
            if self._closed:
                return
            try:
                await self.send(opcodes.CMSG_KEEP_ALIVE)
            except Exception:  # noqa: BLE001
                return

    async def answer_time_sync(self, body: bytes) -> None:
        if len(body) < 4:
            return
        request_id = struct.unpack_from("<I", body, 0)[0]
        ticks = int(time.time() * 1000) & 0xFFFFFFFF
        await self.send(opcodes.CMSG_TIME_SYNC_RESP, struct.pack("<II", request_id, ticks))

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None
