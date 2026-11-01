"""Login-Server-Client für WoW 3.3.5a (Port 3724).

Ablauf: CMD_AUTH_LOGON_CHALLENGE -> SRP6 -> CMD_AUTH_LOGON_PROOF -> CMD_REALM_LIST.
Ergebnis ist der 40-Byte-Sessionkey plus die Realm-Liste; beides braucht man für
die Verbindung zum World-Server.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass

from . import crypto, opcodes

log = logging.getLogger("wowbot.auth")

# Kennung, die der Client beim Login mitschickt. Andere Versionen setzen das
# über `protocol` in der Config (wowbot/protocol_profile.py).
CLIENT_BUILD = 12340
CLIENT_VERSION = (3, 3, 5)
CLIENT_OS = "Win"
CLIENT_PLATFORM = "x86"


class LogonError(Exception):
    """Login am Auth-Server fehlgeschlagen."""


@dataclass
class Realm:
    id: int
    name: str
    host: str
    port: int
    population: float
    characters: int


class AuthClient:
    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.session_key: bytes = b""
        self.username: str = ""

    async def connect(self, host: str, port: int = 3724, timeout: float = 15.0) -> None:
        log.info("Verbinde mit Login-Server %s:%s", host, port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )

    async def login(
        self,
        username: str,
        password: str,
        locale: str = "deDE",
        timezone_bias: int = -60,
    ) -> bytes:
        if self._reader is None or self._writer is None:
            raise LogonError("Nicht mit dem Login-Server verbunden")

        self.username = username.upper()
        await self._send_challenge(self.username, locale, timezone_bias)

        # Antwort: cmd, unk, result
        cmd = await self._read_exact(1)
        if cmd[0] != opcodes.CMD_AUTH_LOGON_CHALLENGE:
            raise LogonError(f"Unerwarteter Antwort-Cmd 0x{cmd[0]:02X}")
        head = await self._read_exact(2)
        result = head[1]
        if result != 0:
            reason = opcodes.LOGON_RESULT_NAMES.get(result, f"Fehlercode {result}")
            raise LogonError(f"Challenge abgelehnt: {reason}")

        body = await self._read_exact(116)
        server_public = body[0:32]
        g_len = body[32]
        offset = 33 + g_len
        n_len = body[offset]
        offset += 1
        salt = body[offset + n_len : offset + n_len + 32]

        # Flag-Byte am Ende der Challenge: 0x04 = Account verlangt einen
        # Authenticator-Code. Den koennen wir nicht liefern.
        flags_index = offset + n_len + 32 + 16
        if flags_index < len(body) and body[flags_index] & 0x04:
            log.warning(
                "Dieser Account verlangt einen Authenticator-Code (TOTP) - "
                "das unterstuetzt der Bot nicht"
            )

        srp = crypto.SRPClient(username, password)
        a_bytes, m1 = srp.process(server_public, salt)
        crc = srp.proof_crc(a_bytes)

        proof = (
            bytes([opcodes.CMD_AUTH_LOGON_PROOF])
            + a_bytes
            + m1
            + crc
            + b"\x00"  # Anzahl Key-Telemetrie-Einträge
            + b"\x00"  # Security-Flags
        )
        self._writer.write(proof)
        await self._writer.drain()

        cmd = await self._read_exact(1)
        if cmd[0] != opcodes.CMD_AUTH_LOGON_PROOF:
            raise LogonError(f"Unerwarteter Antwort-Cmd 0x{cmd[0]:02X} auf Proof")

        # Bei Erfolg sind es 30 weitere Bytes (M2 + Account-Flags + Umfrage + Flags),
        # bei einer Ablehnung nur ein kurzes Paket. Deshalb erst das Ergebnisfeld lesen.
        result = (await self._read_exact(1))[0]
        if result != 0:
            await self._drain(timeout=1.5)
            if result == 0x04:
                # TrinityCore meldet einen falschen SRP-Proof bewusst als
                # "unknown account", damit man keine Accounts erraten kann.
                raise LogonError(
                    "Login abgelehnt: Passwort falsch "
                    "(der Server meldet dafür 'Account existiert nicht')"
                )
            reason = opcodes.LOGON_RESULT_NAMES.get(result, f"Fehlercode {result}")
            raise LogonError(f"Login abgelehnt: {reason}")

        rest = await self._read_exact(30)
        server_proof = rest[0:20]
        if server_proof != srp.server_proof:
            raise LogonError("Server-Proof stimmt nicht (Passwort oder Protokoll)")

        self.session_key = srp.session_key
        log.info("Login erfolgreich als %s", self.username)
        return self.session_key

    async def realmlist(self) -> list[Realm]:
        if self._writer is None:
            raise LogonError("Nicht verbunden")
        self._writer.write(bytes([opcodes.CMD_REALM_LIST]) + struct.pack("<I", 0))
        await self._writer.drain()

        cmd = await self._read_exact(1)
        if cmd[0] != opcodes.CMD_REALM_LIST:
            raise LogonError(f"Unerwarteter Antwort-Cmd 0x{cmd[0]:02X} auf Realm-Liste")
        size = struct.unpack("<H", await self._read_exact(2))[0]
        body = await self._read_exact(size)

        # body: u32 unk, u16 count, dann die Realms, am Ende 2 Füllbytes
        count = struct.unpack_from("<H", body, 4)[0]
        offset = 6
        realms: list[Realm] = []
        for _ in range(count):
            offset += 3  # type, lock, flags
            end = body.index(b"\x00", offset)
            name = body[offset:end].decode("utf-8", "replace")
            offset = end + 1
            end = body.index(b"\x00", offset)
            address = body[offset:end].decode("utf-8", "replace")
            offset = end + 1
            population = struct.unpack_from("<f", body, offset)[0]
            offset += 4
            characters = body[offset]
            offset += 1
            offset += 1  # Kategorie
            realm_id = body[offset]
            offset += 1

            host, _, port = address.partition(":")
            realms.append(
                Realm(
                    id=realm_id,
                    name=name,
                    host=host,
                    port=int(port) if port else 8085,
                    population=population,
                    characters=characters,
                )
            )
        log.info("Realms: %s", ", ".join(f"{r.name} (id {r.id}, {r.host}:{r.port})" for r in realms))
        return realms

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 - Verbindung darf ruhig schon tot sein
                pass
        self._writer = None
        self._reader = None

    async def _send_challenge(self, username: str, locale: str, timezone_bias: int) -> None:
        assert self._writer is not None
        name = username.encode()
        payload = bytearray()
        payload += b"WoW\x00"
        payload += bytes(CLIENT_VERSION[:3]).ljust(3, b"\x00")[:3]
        payload += struct.pack("<H", CLIENT_BUILD)
        payload += _client_tag(CLIENT_PLATFORM)  # "x86" -> "68x\x00"
        payload += _client_tag(CLIENT_OS)  # "Win" -> "niW\x00"
        payload += locale.encode()[:4].ljust(4, b"\x00")[::-1][:4]  # Locale umgedreht
        payload += struct.pack("<i", timezone_bias)
        payload += _local_ip_bytes()
        payload += bytes([len(name)])
        payload += name

        packet = bytes([opcodes.CMD_AUTH_LOGON_CHALLENGE, 8]) + struct.pack(
            "<H", len(payload)
        ) + bytes(payload)
        self._writer.write(packet)
        await self._writer.drain()

    async def _drain(self, timeout: float = 1.0) -> None:
        """Liest weg, was noch im Puffer liegt (kurze Fehlerpakete enden ohne Größenangabe)."""
        if self._reader is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                chunk = await asyncio.wait_for(self._reader.read(64), timeout=remaining)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return
            if not chunk:
                return

    async def _read_exact(self, count: int) -> bytes:
        assert self._reader is not None
        try:
            return await asyncio.wait_for(self._reader.readexactly(count), timeout=20.0)
        except asyncio.IncompleteReadError as exc:
            raise LogonError("Login-Server hat die Verbindung geschlossen") from exc


def _client_tag(text: str) -> bytes:
    """Kurzkennung wie im Original: umgedreht und mit Nullen aufgefüllt."""
    raw = (text or "")[:4].encode("ascii", "ignore")[::-1]
    return raw.ljust(4, b"\x00")[:4]


def _local_ip_bytes() -> bytes:
    """Eigene IP ermitteln; der Login-Server nutzt sie nur zur Anzeige/Logging."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))
            ip = sock.getsockname()[0]
        return socket.inet_aton(ip)
    except OSError:
        return socket.inet_aton("127.0.0.1")
