#!/usr/bin/env python3
"""Minimaler Mock-Server (Login + World) zum Testen des Bots ohne echten Server.

Er implementiert gerade so viel von 3.3.5a, dass der Bot komplett durchläuft:
SRP6-Login, Realm-Liste, Auth-Session mit Header-Verschlüsselung, Char-Enum,
Betreten der Welt, ein paar Mobs, Kampf und Loot.

Start:
    python3 tools/mock_server.py --auth-port 13724 --world-port 18085
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import random
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wowbot import crypto, opcodes  # noqa: E402
from wowbot.objects import (  # noqa: E402
    UPDATEFLAG_HAS_TARGET,
    UPDATEFLAG_LIVING,
    UPDATEFLAG_LOWGUID,
    UPDATEFLAG_POSITION,
    UPDATEFLAG_ROTATION,
    UPDATEFLAG_STATIONARY_POSITION,
)
from wowbot.protocol import (  # noqa: E402
    MOVEFLAG_SPLINE_ENABLED,
    MovementInfo,
    Reader,
    Writer,
    read_movement_info,
    write_movement_info,
)

log = logging.getLogger("mock")

PLAYER_GUID = 0x0000000000000001
MOB_ENTRY = 1234
MOB_MAX_HEALTH = 40
# Namen wie aus creature_template.Name (für CMSG_CREATURE_QUERY)
CREATURE_NAMES = {1234: "Testkriecher", 1235: "Fremder Leichnam", 6491: "Geistheiler"}
# Namen wie aus item_template.name (für CMSG_ITEM_QUERY_SINGLE)
ITEM_NAMES = {2589: "Leinenstoff", 4306: "Seidenstoff"}
HEALER_GUID = 0x0000000000000300
HEALER_ENTRY = 6491
# Test-Heilszauber: der Mock heilt darum herum, damit der Bot den Beweis im Log sieht.
TEST_HEAL_SPELL = 12345
TEST_HEAL_AMOUNT = 150
# Friedhof, an dem der Geist nach dem Freilassen auftaucht
GRAVEYARD = (-8860.0, 720.0, 94.0)


def u32(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


def build_values(fields: dict[int, int]) -> bytes:
    assert fields, "mindestens ein Feld"
    words = max(fields) // 32 + 1
    masks = [0] * words
    for index in fields:
        masks[index // 32] |= 1 << (index % 32)
    out = bytearray([words])
    for mask in masks:
        out += u32(mask)
    for index in sorted(fields):
        out += u32(fields[index])
    return bytes(out)


def spline_data_block() -> bytes:
    """Spline-Block wie CommonMovement::WriteCreateObjectSplineDataBlock (3.3.5)."""
    writer = Writer()
    writer.u32(0)  # spline flags (keine Sonderorientierung)
    writer.i32(0)  # elapsed
    writer.u32(1000)  # duration
    writer.u32(1)  # id
    writer.f32(1.0)  # duration modifier
    writer.f32(1.0)  # next duration modifier
    writer.f32(0.0)  # vertical acceleration
    writer.u32(0)  # effect start time
    writer.u32(2)  # Anzahl Pfadpunkte
    writer.raw(struct.pack("<6f", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    writer.u8(0)  # mode
    writer.f32(0.0), writer.f32(0.0), writer.f32(0.0)  # destination
    return writer.build()


def movement_update_block(
    x: float,
    y: float,
    z: float,
    o: float,
    spline: bool = False,
    has_target: bool = False,
) -> bytes:
    """Create-/Movement-Block ab dem Update-Flags-Feld (Object::BuildMovementUpdate)."""
    writer = Writer()
    flags = UPDATEFLAG_LIVING
    if has_target:
        flags |= UPDATEFLAG_HAS_TARGET
    writer.u16(flags)
    move_flags = MOVEFLAG_SPLINE_ENABLED if spline else 0
    write_movement_info(
        writer, MovementInfo(flags=move_flags, flags2=0, time=0, x=x, y=y, z=z, o=o)
    )
    for _ in range(9):  # walk, run, run_back, swim, swim_back, flight, flight_back, turn, pitch
        writer.f32(7.0)
    if spline:
        writer.raw(spline_data_block())
    if has_target:
        writer.packed_guid(0)  # kein Ziel
    return writer.build()


def create_block(
    guid: int,
    type_id: int,
    x: float,
    y: float,
    z: float,
    o: float,
    fields: dict[int, int],
    spline: bool = False,
    has_target: bool = False,
) -> bytes:
    writer = Writer()
    writer.u8(opcodes.UPDATETYPE_CREATE_OBJECT2)
    writer.packed_guid(guid)
    writer.u8(type_id)
    writer.raw(movement_update_block(x, y, z, o, spline=spline, has_target=has_target))
    writer.raw(build_values(fields))
    return writer.build()


def monster_move_bytes(
    guid: int,
    start: "tuple[float, float, float]",
    destination: "tuple[float, float, float]",
    duration_ms: int,
) -> bytes:
    """SMSG_MONSTER_MOVE wie Unit::SendMonsterMove (3.3.5a, ein Zielpunkt)."""
    writer = Writer()
    writer.packed_guid(guid)
    writer.u8(0)  # unbekannt
    writer.f32(start[0]).f32(start[1]).f32(start[2])
    writer.u32(1)  # Spline-Id
    writer.u8(0)  # Bewegungsart: normal
    writer.u32(0)  # Spline-Flags
    writer.u32(duration_ms)
    writer.u32(1)  # ein Punkt
    writer.f32(destination[0]).f32(destination[1]).f32(destination[2])
    return writer.build()


def corpse_block(guid: int, owner: int, x: float, y: float, z: float, o: float) -> bytes:
    """Leichnam wie Corpse::BuildCreateUpdateBlockForPlayer (3.3.5).

    Update-Flags sind LOWGUID | STATIONARY_POSITION | POSITION; fuer POSITION
    schreibt der Server Nullbyte + 3 Positionen + 2x Orientierung, danach die
    LOWGUID und die Werte (CORPSE_FIELD_OWNER = 0x06/0x07).
    """
    writer = Writer()
    writer.u8(opcodes.UPDATETYPE_CREATE_OBJECT2)
    writer.packed_guid(guid)
    writer.u8(opcodes.TYPEID_CORPSE)
    writer.u16(UPDATEFLAG_LOWGUID | UPDATEFLAG_STATIONARY_POSITION | UPDATEFLAG_POSITION)
    writer.u8(0)  # kein Transport
    writer.f32(x), writer.f32(y), writer.f32(z)
    writer.f32(x), writer.f32(y), writer.f32(z)
    writer.f32(o), writer.f32(o)
    writer.u32(guid & 0xFFFFFFFF)  # LOWGUID
    writer.raw(
        build_values(
            {
                opcodes.FIELD_CORPSE_OWNER_LOW: owner & 0xFFFFFFFF,
                opcodes.FIELD_CORPSE_OWNER_HIGH: (owner >> 32) & 0xFFFFFFFF,
                0x0A: 1,  # CORPSE_FIELD_DISPLAY_ID
                0x20: 0,  # CORPSE_FIELD_FLAGS
            }
        )
    )
    return writer.build()


def item_block(guid: int, entry: int) -> bytes:
    """Item im Rucksack: keine Update-Flags, nur Werte."""
    writer = Writer()
    writer.u8(opcodes.UPDATETYPE_CREATE_OBJECT2)
    writer.packed_guid(guid)
    writer.u8(opcodes.TYPEID_ITEM)
    writer.u16(0)
    writer.raw(build_values({0x03: entry}))
    return writer.build()


def gameobject_block(guid: int, x: float, y: float, z: float, o: float) -> bytes:
    """GameObject: stationäre Position + Rotation, kein MovementInfo."""
    writer = Writer()
    writer.u8(opcodes.UPDATETYPE_CREATE_OBJECT2)
    writer.packed_guid(guid)
    writer.u8(opcodes.TYPEID_GAMEOBJECT)
    writer.u16(UPDATEFLAG_STATIONARY_POSITION | UPDATEFLAG_ROTATION)
    writer.f32(x), writer.f32(y), writer.f32(z), writer.f32(o)
    writer.u64(0)  # gepackte lokale Rotation
    writer.raw(build_values({0x03: 1731}))
    return writer.build()


def attack_state_block(
    attacker: int, victim: int, damage: int, flags: int = 0, victim_state: int = 1
) -> bytes:
    """SMSG_ATTACKERSTATEUPDATE wie CombatLogPackets.cpp (3.3.5) aufbaut."""
    writer = Writer()
    writer.u32(flags)  # HitInfo
    writer.packed_guid(attacker)
    writer.packed_guid(victim)
    writer.u32(damage)
    writer.u32(0)  # Overkill
    writer.u8(1)  # Anzahl Teilschaeden
    writer.u32(1)  # Schadensschule (physisch)
    writer.f32(float(damage))
    writer.u32(damage)
    writer.u8(victim_state)  # VictimState
    writer.u32(0)  # AttackerState
    writer.u32(0)  # MeleeSpellID
    return writer.build()


def movement_info_bytes(x: float, y: float, z: float, o: float, flags: int = 0) -> bytes:
    """Nur der MovementInfo-Block (ohne Update-Flags) fuer den Teleport."""
    writer = Writer()
    write_movement_info(
        writer, MovementInfo(flags=flags, flags2=0, time=0, x=x, y=y, z=z, o=o)
    )
    return writer.build()


def values_block(guid: int, fields: dict[int, int]) -> bytes:
    writer = Writer()
    writer.u8(opcodes.UPDATETYPE_VALUES)
    writer.packed_guid(guid)
    writer.raw(build_values(fields))
    return writer.build()


class MockWorld:
    def __init__(
        self,
        player_x: float,
        player_y: float,
        player_z: float,
        rounds: int,
        spawn_min: float = 8.0,
        spawn_max: float = 14.0,
    ) -> None:
        self.x, self.y, self.z, self.o = player_x, player_y, player_z, 0.0
        self.rounds = rounds
        self.spawn_min = spawn_min
        self.spawn_max = spawn_max
        self.kills = 0
        self.loot_released = 0
        self.mobs: dict[int, dict] = {}
        self.foreign: set[int] = set()
        self.next_mob_guid = 0x0000000000000100
        self.done = asyncio.Event()

    def spawn_mob(self) -> dict:
        guid = self.next_mob_guid
        self.next_mob_guid += 1
        angle = random.uniform(0, 6.283)
        distance = random.uniform(self.spawn_min, self.spawn_max)
        mob = {
            "guid": guid,
            "x": self.x + distance * math.cos(angle),
            "y": self.y + distance * math.sin(angle),
            "z": self.z,
            "health": MOB_MAX_HEALTH,
        }
        mob["home"] = (mob["x"], mob["y"], mob["z"])
        mob["heading"] = random.uniform(0, 6.283)
        self.mobs[guid] = mob
        return mob

    def spawn_corpse(self) -> dict:
        """Tote Leiche eines anderen Spielers - darf nicht gelootet werden."""
        mob = self.spawn_mob()
        mob["health"] = 0
        self.foreign.add(mob["guid"])
        return mob


class MockServer:
    def __init__(
        self,
        username: str,
        password: str,
        char_name: str,
        rounds: int,
        harass: bool = False,
        death_test: bool = False,
        cant_attack: bool = False,
        stop_attack: bool = False,
        slow_regen: bool = False,
        start_hp: int = 1000,
        corpse_blocked: bool = False,
        walking: bool = False,
        spawn_min: float = 8.0,
        spawn_max: float = 14.0,
        deaths: int = 0,
        legacy_teleport: bool = False,
        delay_health: bool = False,
    ) -> None:
        self.harass = harass
        self.walking = walking
        self.sitting = False
        self.cant_attack = cant_attack
        self.death_test = death_test
        self.deaths_planned = max(1, deaths) if death_test else 0
        self.deaths_done = 0
        self.death_running = False
        self.dead = False
        self.ghost = False
        self.corpse_pos: tuple[float, float, float] | None = None
        self.corpse_guid: int | None = None
        self.corpse_visible = False
        # Manche Kerne schicken den Teleport als MSG_MOVE_TELEPORT (0x0C5) ohne Sequenz.
        self.legacy_teleport = legacy_teleport
        # Test: Wiederbelebung kommt nur als Flag-Update, die HP erst spaeter.
        self.delay_health = delay_health
        self.graveyard_pos = GRAVEYARD
        self.corpse_blocked = corpse_blocked
        self.username = username.upper()
        self.password = password.upper()
        self.char_name = char_name
        self.world_state = MockWorld(
            -8800.0, 640.0, 94.0, rounds, spawn_min=spawn_min, spawn_max=spawn_max
        )
        self.salt = os.urandom(32)
        self.verifier = self._verifier()
        self.session_key = b""
        self.player_health = start_hp
        self.player_power = 100
        self.player_xp = 12345
        self.next_level_xp = 40000
        self.level = 70
        self.last_attack_at = 0.0
        self.last_damage_at = 0.0
        self.attack_guid: int | None = None
        self.next_swing_at = 0.0
        self.swings = 0
        self.stop_attack = stop_attack
        self.stop_attack_done = False
        # Wie der echte Server: waehrend eines Teleports werden Bewegungen verworfen.
        self.awaiting_teleport_ack = False
        self.pending_teleport: tuple[float, float, float] | None = None
        self.teleport_seq = 0
        # Langsame Regeneration wie ein echter Charakter (statt Test-Tempo).
        self.regen_per_tick = 15 if slow_regen else 120
        self.facing_checked: set[int] = set()
        self.cant_attack_checked: set[int] = set()
        self.mob_health = MOB_MAX_HEALTH
        self.loot_gold = 137
        self.potions_used = 0

    # ------------------------------------------------------------------ SRP6
    def _verifier(self) -> int:
        x = crypto.le_int(
            crypto.sha1(self.salt, crypto.sha1(f"{self.username}:{self.password}".encode()))
        )
        return pow(crypto.SRP_G, x, crypto.SRP_N)

    # ------------------------------------------------------------------ Auth
    async def handle_auth(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Auth-Verbindung von %s", peer)
        try:
            head = await reader.readexactly(4)
            if head[0] != opcodes.CMD_AUTH_LOGON_CHALLENGE:
                log.warning("Unerwartetes Auth-Paket 0x%02X", head[0])
                return
            size = struct.unpack("<H", head[2:4])[0]
            payload = await reader.readexactly(size)
            name_len = payload[0x1D]
            account = payload[0x1E : 0x1E + name_len].decode()
            log.info("Challenge für Account '%s', Build %d", account, struct.unpack_from("<H", payload, 7)[0])

            b = crypto.le_int(os.urandom(32))
            server_public = (crypto.SRP_K * self.verifier + pow(crypto.SRP_G, b, crypto.SRP_N)) % crypto.SRP_N
            response = (
                bytes([opcodes.CMD_AUTH_LOGON_CHALLENGE, 0, 0])
                + crypto.le_bytes(server_public)
                + bytes([1, crypto.SRP_G, 32])
                + crypto.le_bytes(crypto.SRP_N)
                + self.salt
                + os.urandom(16)
                + b"\x00"
            )
            writer.write(response)
            await writer.drain()

            proof = await reader.readexactly(75)
            a_bytes = proof[1:33]
            m1 = proof[33:53]
            a_int = crypto.le_int(a_bytes)
            u = crypto.le_int(crypto.sha1(a_bytes, crypto.le_bytes(server_public)))
            s_int = pow((a_int * pow(self.verifier, u, crypto.SRP_N)) % crypto.SRP_N, b, crypto.SRP_N)
            session_key = crypto.SRPClient._interleave_session_key(s_int)

            hn = crypto.sha1(crypto.le_bytes(crypto.SRP_N))
            hg = crypto.sha1(bytes([crypto.SRP_G]))
            xor = bytes(x ^ y for x, y in zip(hn, hg))
            expected = crypto.sha1(
                xor,
                crypto.sha1(self.username.encode()),
                self.salt,
                a_bytes,
                crypto.le_bytes(server_public),
                session_key,
            )
            if m1 != expected:
                log.warning("SRP-Proof falsch - Login abgelehnt")
                writer.write(bytes([opcodes.CMD_AUTH_LOGON_PROOF, 0x05]) + b"\x00\x00")
                await writer.drain()
                return

            m2 = crypto.sha1(a_bytes, m1, session_key)
            writer.write(bytes([opcodes.CMD_AUTH_LOGON_PROOF, 0]) + m2 + u32(0) + u32(0) + b"\x00\x00")
            await writer.drain()
            log.info("SRP-Login ok, Sessionkey erzeugt")

            self.session_key = session_key

            realm_request = await reader.readexactly(5)
            if realm_request[0] == opcodes.CMD_REALM_LIST:
                realms = bytearray()
                realms += bytes([0, 0, 0])  # type, lock, flags
                realms += b"MockRealm\x00"
                realms += b"127.0.0.1:18085\x00"
                realms += struct.pack("<f", 1.0)
                realms += bytes([1, 1, 1])  # chars, category, id
                body = u32(0) + struct.pack("<H", 1) + bytes(realms) + b"\x00\x00"
                writer.write(bytes([opcodes.CMD_REALM_LIST]) + struct.pack("<H", len(body)) + body)
                await writer.drain()
                log.info("Realm-Liste gesendet")
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            log.info("Auth-Verbindung beendet: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Fehler in der Auth-Verbindung")
        finally:
            writer.close()

    # ------------------------------------------------------------------ World
    async def handle_world(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("World-Verbindung von %s", writer.get_extra_info("peername"))
        crypt = None
        server_seed = random.getrandbits(32)
        challenge_body = u32(1) + u32(server_seed)
        writer.write(
            struct.pack(">H", len(challenge_body) + 2)
            + struct.pack("<H", opcodes.SMSG_AUTH_CHALLENGE)
            + challenge_body
        )
        await writer.drain()

        # CMSG_AUTH_SESSION ist noch unverschlüsselt
        head = await reader.readexactly(6)
        size = struct.unpack(">H", head[:2])[0]
        opcode = struct.unpack("<I", head[2:6])[0]
        body = await reader.readexactly(size - 4)
        if opcode != opcodes.CMSG_AUTH_SESSION:
            log.warning("Erwartet CMSG_AUTH_SESSION, bekommen 0x%04X", opcode)
            return

        packet = Reader(body)
        build = packet.u32()
        packet.u32()  # login_server_id
        account = packet.cstring()
        packet.u32()  # login_server_type
        client_seed = packet.u32()
        packet.u32()  # region_id
        packet.u32()  # battleground_id
        packet.u32()  # realm_id
        packet.u64()  # dos_response
        digest = packet.raw(20)

        expected = crypto.auth_session_digest(account, client_seed, server_seed, self.session_key)
        if digest != expected:
            log.error("Auth-Session-Digest falsch")
            writer.write(struct.pack(">H", 3) + struct.pack("<H", opcodes.SMSG_AUTH_RESPONSE) + bytes([0x0D]))
            await writer.drain()
            return

        crypt = crypto.HeaderCrypt(self.session_key, server_side=True)
        log.info("Auth-Session ok (Build %d, Account %s)", build, account)

        async def send(op: int, payload: bytes = b"") -> None:
            size = len(payload) + 2
            header = struct.pack(">H", size) + struct.pack("<H", op)
            writer.write(crypt.encrypt(header) + payload)
            await writer.drain()

        async def recv() -> tuple[int, bytes]:
            # Client-Header: 2 Byte Größe (BE) + 4 Byte Opcode (LE)
            first = crypt.decrypt(await reader.readexactly(1))
            if first[0] & 0x80:
                rest = crypt.decrypt(await reader.readexactly(5))
                size = ((first[0] & 0x7F) << 16) | (rest[0] << 8) | rest[1]
                op = int.from_bytes(rest[2:6], "little")
            else:
                rest = crypt.decrypt(await reader.readexactly(5))
                size = (first[0] << 8) | rest[0]
                op = int.from_bytes(rest[1:5], "little")
            # Die Größe enthält den 4-Byte-Opcode
            payload = await reader.readexactly(size - 4)
            return op, payload

        await send(opcodes.SMSG_AUTH_RESPONSE, bytes([opcodes.AUTH_OK]) + u32(0) + u32(0) + u32(0) + bytes([2]))

        try:
            while True:
                op, payload = await recv()
                if op == opcodes.CMSG_CHAR_ENUM:
                    await send(opcodes.SMSG_CHAR_ENUM, self._char_enum())
                elif op == opcodes.CMSG_PLAYER_LOGIN:
                    await self._enter_world(send)
                elif op in (opcodes.MSG_MOVE_START_FORWARD, opcodes.MSG_MOVE_HEARTBEAT, opcodes.MSG_MOVE_STOP, opcodes.MSG_MOVE_SET_FACING):
                    await self._handle_move(send, payload)
                elif op == opcodes.MSG_MOVE_TELEPORT_ACK:
                    self._handle_teleport_ack(payload)
                elif op == opcodes.CMSG_SET_ACTIVE_MOVER:
                    log.info("Activer Mover gesetzt")
                elif op == opcodes.CMSG_SET_SELECTION:
                    log.debug("Auswahl gesetzt: %d", Reader(payload).u64())
                elif op == opcodes.CMSG_CAST_SPELL:
                    await self._handle_cast_spell(send, payload)
                elif op == opcodes.CMSG_STANDSTATECHANGE:
                    state = Reader(payload).u32()
                    self.sitting = state == 1
                    log.info(
                        "StandState %d - Spieler %s",
                        state,
                        "setzt sich hin" if self.sitting else "steht auf",
                    )
                elif op == opcodes.CMSG_USE_ITEM:
                    await self._handle_use_item(send, payload)
                elif op == opcodes.CMSG_ATTACK_SWING:
                    await self._handle_attack(send, payload)
                elif op == opcodes.CMSG_ATTACK_STOP:
                    await self._stop_attack(send, notify=False)
                elif op == opcodes.CMSG_REPOP_REQUEST:
                    await self._handle_repop(send)
                elif op == opcodes.CMSG_RECLAIM_CORPSE:
                    await self._handle_reclaim_corpse(send, payload)
                elif op == opcodes.CMSG_SPIRIT_HEALER_ACTIVATE:
                    await self._handle_spirit_healer(send, payload)
                elif op == opcodes.CMSG_CREATURE_QUERY:
                    await self._handle_creature_query(send, payload)
                elif op == opcodes.CMSG_ITEM_QUERY_SINGLE:
                    await self._handle_item_query(send, payload)
                elif op == opcodes.CMSG_LOOT:
                    guid = Reader(payload).u64()
                    await self._handle_loot(send, guid)
                elif op == opcodes.CMSG_AUTOSTORE_LOOT_ITEM:
                    log.info("Gegenstand gelootet (Slot %d)", Reader(payload).u8())
                elif op == opcodes.CMSG_LOOT_MONEY:
                    log.info("Geld gelootet: %d Kupfer", self.loot_gold)
                    self.loot_gold = 0
                elif op == opcodes.CMSG_LOOT_RELEASE:
                    log.info("Loot-Fenster geschlossen")
                    self.world_state.loot_released += 1
                    await self._maybe_finish(send)
                elif op == opcodes.CMSG_KEEP_ALIVE:
                    log.debug("Keep-Alive")
                elif op == opcodes.CMSG_TIME_SYNC_RESP:
                    log.debug("Time-Sync beantwortet")
                else:
                    log.debug("Unbehandelter Opcode 0x%04X", op)
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("World-Verbindung beendet")
        except Exception:  # noqa: BLE001 - im Mock soll jeder Fehler sichtbar sein
            log.exception("Fehler in der World-Verbindung")
        finally:
            writer.close()

    def _char_enum(self) -> bytes:
        writer = Writer()
        writer.u8(1)
        writer.u64(PLAYER_GUID)
        writer.cstring(self.char_name)
        writer.u8(1)  # race: Mensch
        writer.u8(1)  # class: Krieger
        writer.u8(0)  # gender
        writer.raw(bytes([0, 0, 0, 0, 0]))  # skin, face, hair style, hair color, facial hair
        writer.u8(80)  # level
        writer.u32(0)  # area
        writer.u32(0)  # map
        writer.raw(struct.pack("<fff", self.world_state.x, self.world_state.y, self.world_state.z))
        writer.u32(0)
        writer.u32(0)
        writer.u32(0)
        writer.u8(0)
        writer.u32(0)
        writer.u32(0)
        writer.u32(0)
        writer.raw(bytes(23 * 9))
        return writer.build()

    async def _enter_world(self, send) -> None:
        await send(
            opcodes.SMSG_LOGIN_VERIFY_WORLD,
            u32(0) + struct.pack("<ffff", self.world_state.x, self.world_state.y, self.world_state.z, 0.0),
        )
        potion_guid = 0x0000000000000200
        player = create_block(
            PLAYER_GUID,
            opcodes.TYPEID_PLAYER,
            self.world_state.x,
            self.world_state.y,
            self.world_state.z,
            0.0,
            {
                0x03: 0,
                # Bytes_0: Rasse 1, Klasse 1 (Krieger), Geschlecht 0, Kraftart 1 (Wut).
                # Damit verhaelt sich der Mock wie ein wahrer Krieger: keine Manapause.
                0x17: 1 | (1 << 8) | (0 << 16) | (1 << 24),
                0x18: self.player_health,
                0x19: self.player_power,  # Wut
                0x20: 1000,
                0x21: 100,  # Max-Wut
                0x36: self.level,
                370: potion_guid & 0xFFFFFFFF,  # Rucksack-Slot 0
                371: potion_guid >> 32,
                634: self.player_xp,  # PLAYER_XP
                635: self.next_level_xp,  # PLAYER_NEXT_LEVEL_XP
            },
        )
        item = item_block(potion_guid, 33447)  # Heiltrank
        # Ein GameObject steckt mit im Paket, damit der Parser daran nicht scheitert.
        go = gameobject_block(0x0000000000000099, self.world_state.x + 3, self.world_state.y + 3, self.world_state.z, 1.0)
        mob = self.world_state.spawn_mob()
        mob_block = create_block(
            mob["guid"],
            opcodes.TYPEID_UNIT,
            mob["x"],
            mob["y"],
            mob["z"],
            0.0,
            {0x03: MOB_ENTRY, 0x18: MOB_MAX_HEALTH, 0x20: MOB_MAX_HEALTH, 0x36: 70},
            has_target=True,
        )
        # Zweiter Mob mit Spline-Block (bewegt sich), um das Überspringen zu testen.
        mob2 = self.world_state.spawn_mob()
        mob2_block = create_block(
            mob2["guid"],
            opcodes.TYPEID_UNIT,
            mob2["x"],
            mob2["y"],
            mob2["z"],
            0.0,
            {0x03: MOB_ENTRY, 0x18: MOB_MAX_HEALTH, 0x20: MOB_MAX_HEALTH, 0x36: 70},
            spline=True,
        )
        # Geistheiler am Friedhof (für den automatischen Res nach dem Tod).
        healer_block = create_block(
            HEALER_GUID,
            opcodes.TYPEID_UNIT,
            GRAVEYARD[0],
            GRAVEYARD[1],
            GRAVEYARD[2],
            0.0,
            {
                0x03: HEALER_ENTRY,
                0x18: 100,
                0x20: 100,
                0x36: 80,
                0x3B: 0x00000002,  # UNIT_FLAG_NON_ATTACKABLE
                0x52: 0x20,  # UNIT_NPC_FLAG_SPIRITHEALER
            },
        )
        # Dritte Leiche: fremder Kill (darf der Bot nicht looten).
        corpse = self.world_state.spawn_corpse()
        corpse_block = create_block(
            corpse["guid"],
            opcodes.TYPEID_UNIT,
            self.world_state.x + 5,
            self.world_state.y + 5,
            self.world_state.z,
            0.0,
            {0x03: 1235, 0x18: 0, 0x20: MOB_MAX_HEALTH, 0x36: 70},
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(7)
            + player
            + item
            + go
            + mob_block
            + mob2_block
            + corpse_block
            + healer_block,
        )
        log.info(
            "Spieler, Heiltrank, GameObject, 2 Mobs (%d, %d), fremde Leiche %d und Geistheiler %d in der Welt",
            mob["guid"],
            mob2["guid"],
            corpse["guid"],
            HEALER_GUID,
        )
        asyncio.create_task(self._drain_player(send))
        asyncio.create_task(self._server_swings(send))
        asyncio.create_task(self._walk_mobs(send))

        # Zeit-Sync-Anfrage wie ein echter Server
        await send(opcodes.SMSG_TIME_SYNC_REQ, u32(1))

    def _mobs_in_melee(self) -> list[dict]:
        return [
            mob
            for mob in self.world_state.mobs.values()
            if mob["health"] > 0
            and math.hypot(mob["x"] - self.world_state.x, mob["y"] - self.world_state.y) < 12.0
        ]

    async def _walk_mobs(self, send) -> None:
        """Mit --walking: lebende Mobs laufen umher (nur für die Kartenanzeige).

        Jede Runde ein kurzer Schritt plus SMSG_MONSTER_MOVE - genau das Paket,
        aus dem der Bot die Live-Position interpoliert.
        """
        if not self.walking:
            return
        while True:
            await asyncio.sleep(0.8)
            for mob in list(self.world_state.mobs.values()):
                if mob["health"] <= 0 or mob["guid"] in self.world_state.foreign:
                    continue
                start = (mob["x"], mob["y"], mob["z"])
                home_x, home_y, _ = mob["home"]
                # Nicht zu weit vom Platz weglaufen, sonst wird der Test unbrauchbar
                if math.hypot(start[0] - home_x, start[1] - home_y) > 25.0:
                    mob["heading"] = math.atan2(home_y - start[1], home_x - start[0])
                else:
                    mob["heading"] += random.uniform(-0.6, 0.6)
                step = 2.2
                destination = (
                    start[0] + step * math.cos(mob["heading"]),
                    start[1] + step * math.sin(mob["heading"]),
                    start[2],
                )
                mob["x"], mob["y"], mob["z"] = destination
                await send(
                    opcodes.SMSG_MONSTER_MOVE,
                    monster_move_bytes(mob["guid"], start, destination, 800),
                )

    async def _drain_player(self, send) -> None:
        """Regeneration ausserhalb des Kampfes; mit --harass prügelt ein Mob weiter."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.dead:
                    continue
                melee = self._mobs_in_melee() if self.harass else []
                if melee and self.player_health > 0:
                    self.player_health = max(1, self.player_health - 40)
                    self.last_damage_at = time.time()
                    # Auch das Trefferpaket schicken: daran erkennt der Bot, dass
                    # er geschlagen wird, selbst wenn die HP nicht sinken (bei 1 HP).
                    writer = Writer()
                    writer.u32(0)  # Treffer-Flags
                    writer.packed_guid(melee[0]["guid"])
                    writer.packed_guid(PLAYER_GUID)
                    writer.u32(40)  # Schaden
                    writer.u32(0)  # Overkill
                    writer.u8(1)  # Anzahl Teilschaeden
                    writer.u32(0)  # Schule
                    writer.f32(40.0)
                    writer.u32(40)
                    writer.u8(1)  # Opferzustand: getroffen
                    await send(opcodes.SMSG_ATTACKER_STATE_UPDATE, writer.build())
                    await send(
                        opcodes.SMSG_UPDATE_OBJECT,
                        u32(1) + values_block(PLAYER_GUID, {0x18: self.player_health}),
                    )
                    continue
                if self.player_health <= 0 or self.player_health >= 1000:
                    continue
                if time.time() - self.last_damage_at < 3.0:
                    continue
                self.player_health = min(1000, self.player_health + self.regen_per_tick)
                await send(
                    opcodes.SMSG_UPDATE_OBJECT,
                    u32(1) + values_block(PLAYER_GUID, {0x18: self.player_health}),
                )
        except (ConnectionError, asyncio.CancelledError):
            return

    async def _handle_use_item(self, send, payload: bytes) -> None:
        reader = Reader(payload)
        pack_slot = reader.u8()
        slot = reader.u8()
        reader.u8()  # CastID
        reader.u32()  # SpellID
        item_guid = reader.u64()
        self.potions_used += 1
        self.player_health = 1000
        log.info(
            "[mock] Trank benutzt (Rucksack-Slot %d/%d, Item-GUID %d)",
            pack_slot,
            slot,
            item_guid,
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1) + values_block(PLAYER_GUID, {0x18: self.player_health}),
        )

    async def _handle_cast_spell(self, send, payload: bytes) -> None:
        """CastSpellRequest: CastID u8, SpellID i32, CastFlags u8, Ziel ..."""
        reader = Reader(payload)
        try:
            reader.u8()  # CastID
            spell_id = reader.u32()
            reader.u8()  # CastFlags
            target_flags = reader.u32()
            target_guid = reader.packed_guid() if target_flags else 0
        except Exception:  # noqa: BLE001 - kurzes Paket soll den Test nicht abbrechen
            log.warning("Zauberpaket konnte nicht gelesen werden")
            return
        if spell_id != TEST_HEAL_SPELL:
            log.info("Zauber %d versucht (Mock kennt nur den Test-Heilzauber)", spell_id)
            return
        if target_guid != PLAYER_GUID:
            log.warning(
                "[mock] Heilszauber %d auf Ziel %d statt auf den Spieler %d - abgelehnt",
                spell_id,
                target_guid,
                PLAYER_GUID,
            )
            return
        before = self.player_health
        self.player_health = min(1000, self.player_health + TEST_HEAL_AMOUNT)
        # Ein erfolgreicher Zauber kostet Kraft - daran erkennt der Bot, welche
        # Rank des Heilszaubers der Charakter wirklich kennt.
        self.player_power = max(0, self.player_power - 10)
        log.info(
            "[mock] Heilszauber %d auf Spieler (Ziel-GUID %d): %d -> %d HP, Kraft %d",
            spell_id,
            target_guid,
            before,
            self.player_health,
            self.player_power,
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + values_block(
                PLAYER_GUID, {0x18: self.player_health, 0x19: self.player_power}
            ),
        )

    def _handle_teleport_ack(self, payload: bytes) -> None:
        """Der Client hat den Teleport bestaetigt - erst jetzt gilt die neue Position.

        Aufbau (MoveTeleportAck::Read, 3.3.5): PackedGuid, int32 Sequenz, int32 MoveTime.
        """
        if not self.awaiting_teleport_ack:
            return
        try:
            packet = Reader(payload)
            guid = packet.packed_guid()
            sequence = packet.u32()
        except Exception:  # noqa: BLE001 - kurze Pakete sollen den Test nicht abbrechen
            log.warning("[mock] Teleport-ACK war nicht lesbar - Bewegung bleibt gesperrt")
            return
        if guid != PLAYER_GUID:
            log.warning(
                "[mock] Teleport-ACK mit GUID %d statt %d - Bewegung bleibt gesperrt",
                guid,
                PLAYER_GUID,
            )
            return
        self.awaiting_teleport_ack = False
        if self.pending_teleport is not None:
            self.world_state.x, self.world_state.y, self.world_state.z = self.pending_teleport
            self.pending_teleport = None
        log.info(
            "[mock] Teleport bestaetigt (Sequenz %d) - Spieler steht jetzt bei %.0f/%.0f",
            sequence,
            self.world_state.x,
            self.world_state.y,
        )

    async def _handle_move(self, send, payload: bytes) -> None:
        if self.awaiting_teleport_ack:
            # Echter Server: HandleMovementOpcode kehrt bei IsBeingTeleported() sofort zurueck.
            log.debug("[mock] Bewegung ignoriert: Teleport noch nicht bestaetigt")
            return
        packet = Reader(payload)
        packet.packed_guid()
        info = read_movement_info(packet)
        self.world_state.x, self.world_state.y, self.world_state.z, self.world_state.o = (
            info.x,
            info.y,
            info.z,
            info.o,
        )
        await self._refresh_corpse_visibility(send)

    async def _refresh_corpse_visibility(self, send) -> None:
        """Der Leichnam wird erst wieder geschickt, wenn der Geist in Sichtweite kommt."""
        if not self.dead or not self.ghost or self.corpse_pos is None:
            return
        if self.corpse_visible or self.corpse_guid is None:
            return
        distance = math.hypot(
            self.world_state.x - self.corpse_pos[0],
            self.world_state.y - self.corpse_pos[1],
        )
        if distance > 90.0:
            return
        self.corpse_visible = True
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1) + corpse_block(self.corpse_guid, PLAYER_GUID, *self.corpse_pos, 0.0),
        )
        log.info(
            "[mock] Leichnam %d bei %.0f/%.0f wieder in Sicht (%.0f yd)",
            self.corpse_guid,
            self.corpse_pos[0],
            self.corpse_pos[1],
            distance,
        )

    async def _handle_attack(self, send, payload: bytes = b"") -> None:
        self.last_attack_at = time.time()
        mob = None
        if len(payload) >= 8:
            mob = self.world_state.mobs.get(int.from_bytes(payload[:8], "little"))
        if mob is None or mob["health"] <= 0:
            alive = [m for m in self.world_state.mobs.values() if m["health"] > 0]
            if not alive:
                return
            mob = alive[0]
        # Der echte Server prüft die Blickrichtung - hier einmal pro Mob erzwingen,
        # damit der Bot die Korrektur zeigen muss.
        if mob["guid"] not in self.facing_checked:
            self.facing_checked.add(mob["guid"])
            log.info("Mob %d: falsche Blickrichtung gemeldet", mob["guid"])
            await send(opcodes.SMSG_ATTACK_SWING_BAD_FACING, struct.pack("<Q", mob["guid"]))
            return

        if self.cant_attack and mob["guid"] not in self.cant_attack_checked:
            self.cant_attack_checked.add(mob["guid"])
            log.info("Mob %d: 'kann nicht angegriffen werden' gemeldet (Umweg-Test)", mob["guid"])
            await send(opcodes.SMSG_ATTACK_SWING_CANT_ATTACK, struct.pack("<Q", mob["guid"]))
            return

        if self.dead:
            return

        # Wie der echte Server: nur der Angriffsstart kommt vom Client,
        # die Schläge macht der Server selbst (siehe _server_swings).
        if self.attack_guid == mob["guid"]:
            return
        self.attack_guid = mob["guid"]
        self.next_swing_at = time.time() + 2.0
        log.info("Angriff auf Mob %d gestartet - Server schlägt jetzt selbst zu", mob["guid"])
        await send(
            opcodes.SMSG_ATTACK_START, struct.pack("<QQ", PLAYER_GUID, mob["guid"])
        )

    async def _stop_attack(self, send, notify: bool = True) -> None:
        if self.attack_guid is None:
            return
        guid = self.attack_guid
        self.attack_guid = None
        log.info("Angriff auf Mob %d beendet", guid)
        if notify:
            await send(opcodes.SMSG_ATTACK_STOP, struct.pack("<QQ", PLAYER_GUID, guid))

    async def _server_swings(self, send) -> None:
        """Autohit des Servers: alle 2 s ein Schlag, solange angegriffen wird."""
        try:
            while True:
                await asyncio.sleep(0.2)
                if self.attack_guid is None or self.dead:
                    continue
                mob = self.world_state.mobs.get(self.attack_guid)
                if mob is None or mob["health"] <= 0:
                    await self._stop_attack(send)
                    continue
                if time.time() < self.next_swing_at:
                    continue
                self.next_swing_at = time.time() + 2.0
                self.swings += 1
                # Jeder vierte Schlag geht daneben (testet die Trefferzeilen).
                if self.swings % 4 == 0:
                    await send(
                        opcodes.SMSG_ATTACKER_STATE_UPDATE,
                        attack_state_block(
                            PLAYER_GUID,
                            mob["guid"],
                            0,
                            opcodes.HITINFO_MISS,
                            opcodes.VICTIMSTATE_DODGE,
                        ),
                    )
                    log.info("Server-Schlag: verfehlt (Mob %d weicht aus)", mob["guid"])
                else:
                    mob["health"] = max(0, mob["health"] - 13)
                    await send(
                        opcodes.SMSG_ATTACKER_STATE_UPDATE,
                        attack_state_block(PLAYER_GUID, mob["guid"], 13),
                    )
                    log.info("Server-Schlag: 13 Schaden, Mob %d hat noch %d HP", mob["guid"], mob["health"])
                    await send(
                        opcodes.SMSG_UPDATE_OBJECT,
                        u32(1) + values_block(mob["guid"], {0x18: mob["health"]}),
                    )
                # Der Mob schlägt zurück, solange er lebt.
                if self.player_health > 0:
                    self.player_health = max(1, self.player_health - 220)
                    self.last_damage_at = time.time()
                    await send(
                        opcodes.SMSG_ATTACKER_STATE_UPDATE,
                        attack_state_block(mob["guid"], PLAYER_GUID, 220),
                    )
                    await send(
                        opcodes.SMSG_UPDATE_OBJECT,
                        u32(1) + values_block(PLAYER_GUID, {0x18: self.player_health}),
                    )
                if (
                    self.stop_attack
                    and not self.stop_attack_done
                    and self.swings >= 2
                    and mob["health"] > 0
                ):
                    # Simuliert einen Server, der den Autohit mitten im Kampf beendet
                    # (z. B. kurzer Reichweitenverlust). Der Bot muss neu anmelden.
                    self.stop_attack_done = True
                    log.info("[mock] beende den Autohit absichtlich mitten im Kampf")
                    await self._stop_attack(send)
                    continue
                if mob["health"] <= 0:
                    self.world_state.kills += 1
                    log.info(
                        "Mob %d gestorben (Kill %d)", mob["guid"], self.world_state.kills
                    )
                    await self._stop_attack(send)
                    await self._grant_xp(send, mob["guid"], 62)
        except (ConnectionError, asyncio.CancelledError):
            return

    async def _grant_xp(self, send, victim: int, amount: int) -> None:
        """Schickt SMSG_LOG_XPGAIN wie TrinityCore (Player::SendLogXPGain)."""
        self.player_xp += amount
        await send(
            opcodes.SMSG_LOG_XPGAIN,
            struct.pack("<QIB", victim, amount, 0)  # GUID, EP gesamt, Typ 0 = Kill
            + struct.pack("<If", amount, 1.0)  # EP ohne Bonus, Gruppen-Faktor
            + bytes([0]),  # RaF-Bonus
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1) + values_block(PLAYER_GUID, {634: self.player_xp}),
        )
        log.info(
            "[mock] %d EP vergeben (neu %d/%d)",
            amount,
            self.player_xp,
            self.next_level_xp,
        )

    async def _simulate_death(self, send) -> None:
        """Test-Tod: 0 HP. Wiederbeleben am Leichnam oder am Geistheiler."""
        self.dead = True
        self.player_health = 0
        self.corpse_pos = (self.world_state.x, self.world_state.y, self.world_state.z)
        # Der Server legt an der Todesstelle einen Leichnam an (Besitzer = Spieler).
        self.corpse_guid = 0x0000000000000400 + self.deaths_done
        self.corpse_visible = False
        # Test-Friedhof in der Naehe, damit der Rueckweg nicht Minuten dauert.
        self.graveyard_pos = (
            self.corpse_pos[0] + 55.0,
            self.corpse_pos[1],
            self.corpse_pos[2],
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + values_block(
                PLAYER_GUID, {0x18: 0, opcodes.FIELD_PLAYER_FLAGS: 0}
            ),
        )
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1) + corpse_block(self.corpse_guid, PLAYER_GUID, *self.corpse_pos, 0.0),
        )
        self.corpse_visible = True
        log.info(
            "Test-Tod %d/%d: Spieler auf 0 HP, Leichnam %d bei %.0f/%.0f - warte auf Geist freilassen",
            self.deaths_done,
            self.deaths_planned,
            self.corpse_guid,
            self.corpse_pos[0],
            self.corpse_pos[1],
        )
        # Bei --corpse-blocked laenger warten, damit der Geistheiler-Rueckfall
        # wirklich getestet wird und nicht die Notfall-Wiederbelebung dazwischenkommt.
        ticks = 100 if self.corpse_blocked else 30
        try:
            for _ in range(ticks):
                await asyncio.sleep(2.0)
                if not self.dead:
                    return
            log.warning(
                "Keine Wiederbelebung nach %d s - erzeuge Notfall-Wiederbelebung",
                ticks * 2,
            )
            await self._revive(send, reason="Notfall-Wiederbelebung")
        finally:
            self.death_running = False

    async def _revive(
        self,
        send,
        reason: str = "Geistheiler",
        health: int = 900,
        despawn_corpse: bool = False,
    ) -> None:
        self.dead = False
        self.ghost = False
        self.player_health = health
        self.last_damage_at = time.time()
        if despawn_corpse and self.corpse_guid is not None:
            # Wie ResurrectPlayer/SpawnCorpseBones: der Leichnam verschwindet.
            await send(opcodes.SMSG_DESTROY_OBJECT, struct.pack("<Q", self.corpse_guid))
            self.corpse_visible = False
        if self.delay_health:
            # Test: nur das Flag-Update kommt sofort, die HP erst 3 s spaeter.
            await send(
                opcodes.SMSG_UPDATE_OBJECT,
                u32(1) + values_block(PLAYER_GUID, {opcodes.FIELD_PLAYER_FLAGS: 0}),
            )
            log.info(
                "Wiederbelebung (%s): Geist-Flag weg, HP folgen in 3 s (Position %.0f/%.0f)",
                reason,
                self.world_state.x,
                self.world_state.y,
            )

            async def send_health_later() -> None:
                await asyncio.sleep(3.0)
                await send(
                    opcodes.SMSG_UPDATE_OBJECT,
                    u32(1) + values_block(PLAYER_GUID, {0x18: self.player_health}),
                )
                log.info("HP-Update nachgeliefert: %d HP", self.player_health)

            asyncio.create_task(send_health_later())
            return
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + values_block(
                PLAYER_GUID,
                {0x18: self.player_health, opcodes.FIELD_PLAYER_FLAGS: 0},
            ),
        )
        log.info("Wiederbelebung (%s), %d HP, Position %.0f/%.0f", reason,
                 self.player_health, self.world_state.x, self.world_state.y)

    async def _handle_repop(self, send) -> None:
        """Geist freigelassen: Spieler steht jetzt am Friedhof."""
        if not self.dead or self.ghost:
            return
        self.ghost = True
        # Wie Player::BuildPlayerRepop auf 3.3.5: ein Geist hat HP 1, nicht 0.
        self.player_health = 1
        gx, gy, gz = self.graveyard_pos
        # Der echte Server versetzt den Geist per MSG_MOVE_TELEPORT und wartet auf
        # MSG_MOVE_TELEPORT_ACK. Bis dahin gilt die alte Position und Bewegung wird
        # komplett verworfen.
        self.teleport_seq += 1
        self.awaiting_teleport_ack = True
        self.pending_teleport = (gx, gy, gz)
        if self.legacy_teleport:
            # Variante aelterer Kerne: 0x0C5 ohne Sequenz.
            await send(
                opcodes.MSG_MOVE_TELEPORT,
                Writer()
                .packed_guid(PLAYER_GUID)
                .raw(movement_info_bytes(gx, gy, gz, 0.0))
                .build(),
            )
        else:
            # 3.3.5: Fuer den Spieler selbst geht der Teleport als 0x0C7 raus.
            await send(
                opcodes.MSG_MOVE_TELEPORT_ACK,
                Writer()
                .packed_guid(PLAYER_GUID)
                .u32(self.teleport_seq)
                .raw(movement_info_bytes(gx, gy, gz, 0.0))
                .build(),
            )
        # Der alte Leichnam ist jetzt ausser Sicht (der Geist steht am Friedhof).
        if self.corpse_guid is not None:
            await send(opcodes.SMSG_DESTROY_OBJECT, struct.pack("<Q", self.corpse_guid))
            self.corpse_visible = False
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + values_block(
                PLAYER_GUID,
                {0x18: self.player_health, opcodes.FIELD_PLAYER_FLAGS: opcodes.PLAYER_FLAGS_GHOST},
            ),
        )
        log.info("Geist hat %d HP (wie BuildPlayerRepop)", self.player_health)
        await send(
            opcodes.SMSG_DEATH_RELEASE_LOC, struct.pack("<ifff", 0, gx, gy, gz)
        )
        # Neuer Bereich: der Server schickt die Objekte am Friedhof erneut.
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + create_block(
                HEALER_GUID,
                opcodes.TYPEID_UNIT,
                gx + 6,
                gy + 6,
                gz,
                0.0,
                {0x03: HEALER_ENTRY, 0x18: 100, 0x20: 100, 0x36: 80},
            ),
        )
        log.info("Geist freigelassen - Spieler am Friedhof %.0f/%.0f", gx, gy)

    async def _handle_reclaim_corpse(self, send, payload: bytes) -> None:
        """Wiederbeleben am Leichnam - wie im echten Server nur innerhalb von 39 yd."""
        corpse = self.corpse_pos
        if not self.dead or corpse is None:
            return
        if self.corpse_blocked:
            log.info("Leichnam-Einsammeln absichtlich abgelehnt (Test des Geistheiler-Rückfalls)")
            return
        distance = math.hypot(
            self.world_state.x - corpse[0], self.world_state.y - corpse[1]
        )
        if distance > 39.0:
            log.info(
                "Leichnam-Einsammeln abgelehnt: %.0f yd entfernt (Server erlaubt 39)",
                distance,
            )
            return
        await self._revive(
            send, reason=f"Leichnam, {distance:.0f} yd", health=500, despawn_corpse=True
        )

    async def _handle_spirit_healer(self, send, payload: bytes) -> None:
        if len(payload) < 8:
            return
        guid = int.from_bytes(payload[:8], "little")
        if guid != HEALER_GUID or not self.dead:
            log.info("Geistheiler-Aktivierung ignoriert (GUID %d, tot: %s)", guid, self.dead)
            return
        await self._revive(send)

    async def _handle_item_query(self, send, payload: bytes) -> None:
        """Antwortet wie TrinityCore (QueryPackets: QueryItemResponse::Write)."""
        reader = Reader(payload)
        entry = reader.u32()
        name = ITEM_NAMES.get(entry)
        writer = Writer()
        if name is None:
            writer.u32(entry | 0x80000000)  # Gegenstand unbekannt
            await send(opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE, writer.build())
            log.info("Gegenstand %d unbekannt - Absage geschickt", entry)
            return
        writer.u32(entry)
        writer.u32(0)  # unbekannt
        writer.u32(7)  # Klasse (Handwerksmaterial)
        writer.u32(0)  # Unterklasse
        writer.i32(-1)  # Unk
        writer.cstring(name)
        writer.raw(bytes(4))  # name2..name4
        writer.u32(1)  # Qualität
        writer.u32(1)  # Kaufmenge
        writer.u32(0)  # Kaufpreis
        writer.u32(0)  # Verkaufspreis
        writer.u32(0)  # Inventartyp
        writer.u32(0)  # erlaubte Klasse
        writer.u32(0)  # erlaubte Rasse
        writer.u32(1)  # Itemlevel
        writer.u32(0)  # benötigte Stufe
        await send(opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE, writer.build())
        log.info("Gegenstand %d heisst '%s' (Abfrage beantwortet)", entry, name)

    async def _handle_creature_query(self, send, payload: bytes) -> None:
        """Antwortet wie TrinityCore (QueryPackets: QueryCreatureResponse::Write)."""
        reader = Reader(payload)
        entry = reader.u32()
        reader.u64()  # GUID
        name = CREATURE_NAMES.get(entry)
        writer = Writer()
        if name is None:
            writer.u32(entry | 0x80000000)  # unbekannte Kreatur
            await send(opcodes.SMSG_CREATURE_QUERY_RESPONSE, writer.build())
            log.info("Kreatur %d unbekannt - Absage geschickt", entry)
            return
        writer.u32(entry)
        writer.cstring(name)
        writer.u8(0).u8(0).u8(0)  # name2..name4
        writer.cstring("")  # Titel
        writer.cstring("")  # Icon
        writer.u32(0)  # Typ-Flags
        writer.u32(1)  # Kreaturentyp
        writer.u32(0)  # Familie
        writer.u32(0)  # Rang
        writer.raw(bytes(16))  # Kill-Credit (4 x u32)
        writer.raw(bytes(16))  # Display-IDs (4 x u32)
        writer.f32(1.0).f32(1.0)  # HP-/Energie-Multiplikator
        writer.u8(0)  # Leiter
        writer.raw(bytes(16))  # Quest-Items (4 x u32)
        writer.u32(0)  # Movement-Info
        await send(opcodes.SMSG_CREATURE_QUERY_RESPONSE, writer.build())
        log.info("Kreatur %d heisst '%s' (Abfrage beantwortet)", entry, name)

    async def _handle_loot(self, send, guid: int) -> None:
        if guid in self.world_state.foreign:
            log.info("Loot auf fremder Leiche %d abgelehnt (Code 1)", guid)
            await send(opcodes.SMSG_LOOT_RESPONSE, struct.pack("<QB", guid, 0) + bytes([1]))
            return
        mob = self.world_state.mobs.get(guid)
        if mob is None or mob["health"] > 0:
            # Nichts mehr drin (z. B. schon gelootet)
            await send(opcodes.SMSG_LOOT_RESPONSE, struct.pack("<QB", guid, 0) + bytes([5]))
            return
        writer = Writer()
        writer.u64(guid)
        writer.u8(1)  # LOOT_CORPSE
        writer.u32(self.loot_gold)
        writer.u8(2)
        writer.u8(0)  # Slot
        writer.u32(2589)  # Leinenstoff
        writer.u32(2)  # Menge
        writer.u32(1234)  # Display
        writer.u32(0)
        writer.i32(0)
        writer.u8(0)  # Slot-Typ: normales Item
        writer.u8(1)  # Slot
        writer.u32(4306)  # Seidenstoff
        writer.u32(1)  # Menge
        writer.u32(1235)  # Display
        writer.u32(0)
        writer.i32(0)
        writer.u8(0)  # Slot-Typ: normales Item
        del self.world_state.mobs[guid]
        await send(opcodes.SMSG_LOOT_RESPONSE, writer.build())
        log.info("Loot-Fenster für Mob %d geöffnet", guid)

    async def _maybe_finish(self, send) -> None:
        if self.death_test and not self.death_running and self.deaths_done < self.deaths_planned:
            self.deaths_done += 1
            self.death_running = True
            asyncio.create_task(self._simulate_death(send))
        if self.world_state.loot_released >= self.world_state.rounds:
            log.info("Testdurchlauf fertig (%d Kills) - Server beendet sich", self.world_state.kills)
            self.world_state.done.set()
            return
        mob = self.world_state.spawn_mob()
        await send(
            opcodes.SMSG_UPDATE_OBJECT,
            u32(1)
            + create_block(
                mob["guid"],
                opcodes.TYPEID_UNIT,
                mob["x"],
                mob["y"],
                mob["z"],
                0.0,
                {0x03: MOB_ENTRY, 0x18: MOB_MAX_HEALTH, 0x20: MOB_MAX_HEALTH, 0x36: 70},
            ),
        )
        log.info("Neuer Mob %d gespawnt", mob["guid"])


async def main() -> int:
    parser = argparse.ArgumentParser(description="Mock-Server für den WoW-Bot")
    parser.add_argument("--auth-port", type=int, default=13724)
    parser.add_argument("--world-port", type=int, default=18085)
    parser.add_argument("--username", default="TEST")
    parser.add_argument("--password", default="TEST")
    parser.add_argument("--character", default="Botty")
    parser.add_argument("--rounds", type=int, default=2, help="Anzahl Kills bis der Server stoppt")
    parser.add_argument(
        "--cant-attack",
        action="store_true",
        help="Server lehnt den ersten Schlag pro Mob ab (testet den Umweg um Hindernisse)",
    )
    parser.add_argument(
        "--start-hp",
        type=int,
        default=1000,
        help="Start-Lebenspunkte des Spielers (z. B. 250 fuer den Rast-Test)",
    )
    parser.add_argument(
        "--slow-regen",
        action="store_true",
        help="langsame Regeneration (testet die Rast mit Zwischenmeldungen)",
    )
    parser.add_argument(
        "--stop-attack",
        action="store_true",
        help="Autohit einmal mitten im Kampf beenden (testet das erneute Anmelden)",
    )
    parser.add_argument(
        "--death-test",
        action="store_true",
        help="Spieler nach dem ersten Loot einmal sterben und wiederbeleben lassen",
    )
    parser.add_argument(
        "--deaths",
        type=int,
        default=1,
        help="wie oft der Test-Tod ausgeloest wird (Standard 1, testet das Ueberschreiben der Position)",
    )
    parser.add_argument(
        "--legacy-teleport",
        action="store_true",
        help="Geist-Teleport als MSG_MOVE_TELEPORT (0x0C5) ohne Sequenz schicken",
    )
    parser.add_argument(
        "--delay-health",
        action="store_true",
        help="Wiederbelebung zuerst nur als Flag-Update schicken, HP 3 s spaeter",
    )
    parser.add_argument(
        "--corpse-blocked",
        action="store_true",
        help="Wiederbeleben am Leichnam ablehnen (testet den Rückfall auf den Geistheiler)",
    )
    parser.add_argument(
        "--far-mobs",
        action="store_true",
        help="Mobs 60-100 yd entfernt spawnen (testet das Hinlaufen zum nächsten Ziel)",
    )
    parser.add_argument(
        "--harass",
        action="store_true",
        help="Mobs in Reichweite schlagen dauerhaft zu (testet Abbruch der Pause)",
    )
    parser.add_argument(
        "--walking",
        action="store_true",
        help="Mobs laufen umher (SMSG_MONSTER_MOVE, testet die Live-Position auf der Karte)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    server = MockServer(
        args.username,
        args.password,
        args.character,
        args.rounds,
        harass=args.harass,
        death_test=args.death_test,
        walking=args.walking,
        spawn_min=60.0 if args.far_mobs else 8.0,
        spawn_max=100.0 if args.far_mobs else 14.0,
        cant_attack=args.cant_attack,
        stop_attack=args.stop_attack,
        slow_regen=args.slow_regen,
        start_hp=args.start_hp,
        corpse_blocked=args.corpse_blocked,
        deaths=args.deaths,
        legacy_teleport=args.legacy_teleport,
        delay_health=args.delay_health,
    )
    server.session_key = b""
    auth_server = await asyncio.start_server(server.handle_auth, "127.0.0.1", args.auth_port)
    world_server = await asyncio.start_server(server.handle_world, "127.0.0.1", args.world_port)
    log.info(
        "Mock-Server läuft: Auth :%d, World :%d (Account %s/%s, Char %s)",
        args.auth_port,
        args.world_port,
        args.username,
        args.password,
        args.character,
    )
    async with auth_server, world_server:
        try:
            await asyncio.wait_for(
                server.world_state.done.wait(),
                timeout=180 if server.deaths_planned <= 1 else 360,
            )
        except asyncio.TimeoutError:
            log.warning("Zeitlimit erreicht - beende Mock-Server")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
