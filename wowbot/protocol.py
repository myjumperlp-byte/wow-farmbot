"""Byte-Reader/Writer und Bewegungsdaten für das 3.3.5a-Protokoll."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Movement-Flags (TrinityCore 3.3.5, UnitDefines.h)
MOVEFLAG_FORWARD = 0x00000001
MOVEFLAG_BACKWARD = 0x00000002
MOVEFLAG_STRAFE_LEFT = 0x00000004
MOVEFLAG_STRAFE_RIGHT = 0x00000008
MOVEFLAG_LEFT = 0x00000010
MOVEFLAG_RIGHT = 0x00000020
MOVEFLAG_PITCH_UP = 0x00000040
MOVEFLAG_PITCH_DOWN = 0x00000080
MOVEFLAG_WALKING = 0x00000100
MOVEFLAG_ONTRANSPORT = 0x00000200
MOVEFLAG_DISABLE_GRAVITY = 0x00000400
MOVEFLAG_ROOT = 0x00000800
MOVEFLAG_FALLING = 0x00001000
MOVEFLAG_FALLING_FAR = 0x00002000
MOVEFLAG_SWIMMING = 0x00200000
MOVEFLAG_ASCENDING = 0x00400000
MOVEFLAG_DESCENDING = 0x00800000
MOVEFLAG_CAN_FLY = 0x01000000
MOVEFLAG_FLYING = 0x02000000
MOVEFLAG_SPLINE_ELEVATION = 0x04000000
MOVEFLAG_SPLINE_ENABLED = 0x08000000
MOVEFLAG_WATERWALKING = 0x10000000
MOVEFLAG_FALLING_SLOW = 0x20000000
MOVEFLAG_HOVER = 0x40000000

MOVEFLAG2_ALWAYS_ALLOW_PITCHING = 0x0020
MOVEFLAG2_INTERPOLATED_MOVEMENT = 0x0400

# SpellCastTargets-Flags
TARGET_FLAG_UNIT = 0x0002


class ProtocolError(Exception):
    """Fehler beim Lesen oder Schreiben eines Pakets."""


class Reader:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def _take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise ProtocolError(
                f"Paket zu kurz: brauche {count} Bytes ab {self.pos}, vorhanden {len(self.data)}"
            )
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def i8(self) -> int:
        return struct.unpack("<b", self._take(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self._take(4))[0]

    def raw(self, count: int) -> bytes:
        return self._take(count)

    def cstring(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise ProtocolError("CString ohne Null-Terminator")
        text = self.data[self.pos : end].decode("utf-8", "replace")
        self.pos = end + 1
        return text

    def packed_guid(self) -> int:
        mask = self.u8()
        guid = 0
        for index in range(8):
            if mask & (1 << index):
                guid |= self.u8() << (index * 8)
        return guid

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos


class Writer:
    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def u8(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<B", value & 0xFF))
        return self

    def i8(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<b", value))
        return self

    def u16(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<H", value & 0xFFFF))
        return self

    def u32(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<I", value & 0xFFFFFFFF))
        return self

    def i32(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<i", value))
        return self

    def u64(self, value: int) -> "Writer":
        self._parts.append(struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))
        return self

    def f32(self, value: float) -> "Writer":
        self._parts.append(struct.pack("<f", value))
        return self

    def raw(self, data: bytes) -> "Writer":
        self._parts.append(data)
        return self

    def cstring(self, text: str) -> "Writer":
        self._parts.append(text.encode("utf-8") + b"\x00")
        return self

    def packed_guid(self, guid: int) -> "Writer":
        mask = 0
        payload = bytearray()
        for index in range(8):
            byte = (guid >> (index * 8)) & 0xFF
            if byte:
                mask |= 1 << index
                payload.append(byte)
        self._parts.append(bytes([mask]) + bytes(payload))
        return self

    def build(self) -> bytes:
        return b"".join(self._parts)


@dataclass
class MovementInfo:
    """Bewegungsdaten, wie sie in MSG_MOVE_*-Paketen stehen."""

    flags: int = 0
    flags2: int = 0
    time: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    o: float = 0.0
    extra: dict = field(default_factory=dict)


def write_movement_info(writer: Writer, info: MovementInfo) -> None:
    """Schreibt MovementInfo. Wir erzeugen nur einfache Bewegung ohne Transport/Fliegen."""
    writer.u32(info.flags).u16(info.flags2).u32(info.time)
    writer.f32(info.x).f32(info.y).f32(info.z).f32(info.o)
    writer.u32(0)  # fallTime
    if info.flags & MOVEFLAG_SPLINE_ELEVATION:
        writer.f32(0.0)


def read_movement_info(reader: Reader) -> MovementInfo:
    """Liest MovementInfo inklusive der optionalen Blöcke (Transport, Schwimmen, Fallen)."""
    info = MovementInfo()
    info.flags = reader.u32()
    info.flags2 = reader.u16()
    info.time = reader.u32()
    info.x = reader.f32()
    info.y = reader.f32()
    info.z = reader.f32()
    info.o = reader.f32()

    if info.flags & MOVEFLAG_ONTRANSPORT:
        info.extra["transport_guid"] = reader.packed_guid()
        info.extra["transport_pos"] = (reader.f32(), reader.f32(), reader.f32(), reader.f32())
        info.extra["transport_time"] = reader.u32()
        info.extra["transport_seat"] = reader.i8()
        if info.flags2 & MOVEFLAG2_INTERPOLATED_MOVEMENT:
            info.extra["transport_time2"] = reader.u32()

    if (
        info.flags & (MOVEFLAG_SWIMMING | MOVEFLAG_FLYING)
        or info.flags2 & MOVEFLAG2_ALWAYS_ALLOW_PITCHING
    ):
        info.extra["pitch"] = reader.f32()

    info.extra["fall_time"] = reader.u32()

    if info.flags & MOVEFLAG_FALLING:
        info.extra["jump"] = (reader.f32(), reader.f32(), reader.f32(), reader.f32())

    if info.flags & MOVEFLAG_SPLINE_ELEVATION:
        info.extra["spline_elevation"] = reader.f32()

    return info
