"""Objekt-Manager: wertet SMSG_UPDATE_OBJECT aus und hält Positionen/Leben aktuell.

Damit weiss der Bot, welche Mobs in der Nähe sind, wo sie stehen und ob sie noch
leben. Alles, was hier fehlt, wird vom Server über die Update-Pakete nachgeliefert.
"""

from __future__ import annotations

import logging
import time
import zlib
from dataclasses import dataclass, field

from . import opcodes
from .protocol import (
    MOVEFLAG_SPLINE_ENABLED,
    ProtocolError,
    Reader,
    read_movement_info,
)

log = logging.getLogger("wowbot.objects")


# Update-Flags eines Create-/Movement-Blocks (UpdateData.h, 3.3.5)
UPDATEFLAG_SELF = 0x0001
UPDATEFLAG_TRANSPORT = 0x0002
UPDATEFLAG_HAS_TARGET = 0x0004
UPDATEFLAG_UNKNOWN = 0x0008
UPDATEFLAG_LOWGUID = 0x0010
UPDATEFLAG_LIVING = 0x0020
UPDATEFLAG_STATIONARY_POSITION = 0x0040
UPDATEFLAG_VEHICLE = 0x0080
UPDATEFLAG_POSITION = 0x0100
UPDATEFLAG_ROTATION = 0x0200

# Spline-Flags (MoveSplineFlag.h) für den optionalen Spline-Block
SPLINEFLAG_FINAL_POINT = 0x00008000
SPLINEFLAG_FINAL_TARGET = 0x00010000
SPLINEFLAG_FINAL_ANGLE = 0x00020000


class SkipUpdate(Exception):
    """Wird benutzt, wenn ein Blocktyp nicht unterstützt wird."""


@dataclass
class WoWObject:
    guid: int
    type_id: int
    entry: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    o: float = 0.0
    health: int = 0
    max_health: int = 0
    power: int = 0
    max_power: int = 0
    level: int = 0
    power_type: int = 0  # Kraftart des Spielers (0 = Mana, 1 = Wut, 3 = Energie ...)
    xp: int = 0
    next_level_xp: int = 0
    player_flags: int = 0
    faction: int = 0
    flags: int = 0
    npc_flags: int = 0
    dynamic_flags: int = 0
    owner_guid: int = 0  # bei Leichnamen: Besitzer (CORPSE_FIELD_OWNER)
    has_position: bool = False
    last_seen: float = field(default_factory=time.time)
    raw: dict[int, int] = field(default_factory=dict)
    # Laufweg aus SMSG_MONSTER_MOVE: von wo nach wo, in welcher Zeit. Nur die
    # Kartenanzeige interpoliert daraus - die Spiellogik rechnet mit x/y oben.
    spline_from: "tuple[float, float] | None" = None
    spline_to: "tuple[float, float] | None" = None
    spline_start: float = 0.0
    spline_duration: float = 0.0

    def live_position(self, now: "float | None" = None) -> "tuple[float, float]":
        """Position samt laufendem Weg (für die Karte)."""
        if (
            self.spline_from is None
            or self.spline_to is None
            or self.spline_duration <= 0.0
        ):
            return (self.x, self.y)
        moment = time.time() if now is None else now
        progress = (moment - self.spline_start) / self.spline_duration
        if progress <= 0.0:
            return self.spline_from
        if progress >= 1.0:
            return self.spline_to
        (from_x, from_y), (to_x, to_y) = self.spline_from, self.spline_to
        return (
            from_x + (to_x - from_x) * progress,
            from_y + (to_y - from_y) * progress,
        )

    @property
    def health_percent(self) -> float:
        return 100.0 * self.health / self.max_health if self.max_health else 0.0

    @property
    def power_percent(self) -> float:
        return 100.0 * self.power / self.max_power if self.max_power else 0.0

    @property
    def is_unit(self) -> bool:
        return self.type_id in (opcodes.TYPEID_UNIT, opcodes.TYPEID_PLAYER)

    @property
    def is_player(self) -> bool:
        return self.type_id == opcodes.TYPEID_PLAYER

    @property
    def power_name(self) -> str:
        return opcodes.POWER_NAMES.get(self.power_type, "Ressource")

    @property
    def uses_mana(self) -> bool:
        """Nur echte Manaklassen muessen auf Mana warten (Wut/Energie fuellen sich auf)."""
        return self.is_player and self.power_type == opcodes.POWER_MANA

    @property
    def is_ghost(self) -> bool:
        return bool(self.player_flags & opcodes.PLAYER_FLAGS_GHOST)

    @property
    def is_dead_player(self) -> bool:
        """Spieler tot? Geist-Flag oder 0 HP.

        3.3.5-Kerne setzen beim Freilassen des Geistes die HP auf 1
        (`Player::BuildPlayerRepop`), ein Geist hat also HP > 0 und gilt trotzdem
        als tot - deshalb zaehlt hier das Flag. Umgekehrt kann ein Lebender mit
        1 HP aus einem Kampf kommen; der ist nicht tot.
        """
        if not self.is_player:
            return self.health <= 0
        return self.is_ghost or self.health <= 0

    @property
    def is_mob(self) -> bool:
        return self.type_id == opcodes.TYPEID_UNIT

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def is_attackable(self) -> bool:
        """Kein Freund-NPC, Vendor, Geistheiler oder sonst unangreifbar."""
        if self.npc_flags:
            # Jede Einheit mit Dialogen (Vendor, Trainer, Geistheiler ...) ist kein Farmziel.
            return False
        blocked = (
            opcodes.UNIT_FLAG_NON_ATTACKABLE
            | opcodes.UNIT_FLAG_IMMUNE_TO_PC
            | opcodes.UNIT_FLAG_PACIFIED
            | opcodes.UNIT_FLAG_UNINTERACTIBLE
        )
        return not (self.flags & blocked)

    @property
    def is_evading(self) -> bool:
        """Mob laeuft zu seinem Spawnpunkt zurueck - nicht mehr hinterherlaufen."""
        return bool(self.flags & opcodes.UNIT_FLAG_EVADING_HOME)

    @property
    def position(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class ObjectManager:
    def __init__(self, self_guid: int | None = None) -> None:
        self.objects: dict[int, WoWObject] = {}
        self.self_guid = self_guid
        self.updates_seen = 0
        # Wird bei jeder Health-Änderung aufgerufen: callback(guid, alt, neu, objekt)
        self.on_health_change = None

    # ------------------------------------------------------------------ Abfragen
    def get(self, guid: int) -> WoWObject | None:
        return self.objects.get(guid)

    def find_pack_item(self, entry: int) -> tuple[int, int] | None:
        """Sucht ein Item mit dieser Entry im Rucksack. Liefert (slot, item_guid)."""
        for slot in range(16):
            guid = self.pack_slot_item(slot)
            if not guid:
                continue
            item = self.objects.get(guid)
            if item is not None and item.entry == entry:
                return slot, guid
        return None

    def pack_slot_item(self, slot: int) -> int | None:
        """Item-GUID im Rucksack (Slot 0..15) oder None."""
        me = self.me
        if me is None:
            return None
        index = opcodes.FIELD_PLAYER_PACK_SLOT_1 + 2 * slot
        low = me.raw.get(index, 0)
        high = me.raw.get(index + 1, 0)
        guid = low | (high << 32)
        return guid or None

    @property
    def me(self) -> WoWObject | None:
        if self.self_guid is None:
            return None
        return self.objects.get(self.self_guid)

    def forget_stale(self, max_age_seconds: float = 45.0, keep: frozenset[int] | set[int] = frozenset()) -> None:
        """Objekte, die lange kein Update mehr hatten, gelten als weg.

        `keep` schuetzt Objekte (z. B. das aktuelle Ziel), die der Server nicht
        laufend neu schickt, die aber weiter gebraucht werden.
        """
        now = time.time()
        stale = [
            guid
            for guid, obj in self.objects.items()
            if guid != self.self_guid
            and guid not in keep
            and now - obj.last_seen > max_age_seconds
        ]
        for guid in stale:
            del self.objects[guid]

    # ------------------------------------------------------------------ Pakete
    def handle_update_object(self, body: bytes) -> None:
        self._parse(body)

    def handle_compressed_update_object(self, body: bytes) -> None:
        if len(body) < 4:
            return
        declared = int.from_bytes(body[:4], "little")
        try:
            payload = zlib.decompress(body[4:])
        except zlib.error as exc:
            log.warning("Update-Paket liess sich nicht entpacken: %s", exc)
            return
        if len(payload) != declared:
            log.debug("Entpackte Grösse %d weicht von %d ab", len(payload), declared)
        self._parse(payload)

    def handle_destroy_object(self, body: bytes) -> None:
        if len(body) < 8:
            return
        guid = int.from_bytes(body[:8], "little")
        self.objects.pop(guid, None)

    def advance_splines(self, now: "float | None" = None) -> None:
        """Laufwege weiterrechnen: Kreaturen wandern auch für die Spiellogik mit.

        Ohne das würde eine Kreatur, die 30 Yard weiterläuft, für Reichweite und
        Blickrichtung immer noch an ihrer alten Stelle stehen.
        """
        moment = time.time() if now is None else now
        for obj in list(self.objects.values()):
            if obj.spline_to is None:
                continue
            if obj.spline_duration <= 0.0:
                obj.spline_from = obj.spline_to = None
                continue
            x, y = obj.live_position(moment)
            obj.x, obj.y = x, y
            if moment - obj.spline_start >= obj.spline_duration:
                # Angekommen: Ziel übernehmen und den Weg vergessen
                obj.spline_from = obj.spline_to = None
                obj.spline_duration = 0.0

    def handle_monster_move(self, body: bytes) -> None:
        """Laufweg einer Kreatur (SMSG_MONSTER_MOVE, 3.3.5a).

        Aufbau: gepackte GUID, uint8 unbekannt, Startposition (3 Floats),
        Spline-Id, Bewegungsart (+ Blickdaten), Flags, Dauer in ms, Anzahl
        Punkte, erster Punkt (3 Floats). Mehrere Punkte sind danach gepackt -
        die brauchen wir nicht, das Ziel liegt im ersten Punkt.

        Für das Spiel zählt der Startpunkt (dort steht die Kreatur gerade);
        den Weg dorthin merkt sich das Objekt, damit die Karte sie laufen sieht.
        """
        reader = Reader(body)
        try:
            guid = reader.packed_guid()
            reader.u8()  # unbekannt, immer 0
            start = (reader.f32(), reader.f32(), reader.f32())
            reader.u32()  # Spline-Id
            move_type = reader.u8()
            if move_type == 2:  # Blick auf einen Punkt
                reader.f32(), reader.f32(), reader.f32()
            elif move_type == 3:  # Blick auf ein Ziel
                reader.u64()
            elif move_type == 4:  # Blickwinkel
                reader.f32()
            reader.u32()  # Spline-Flags
            duration_ms = reader.u32()
            points = reader.u32()
            destination = None
            if points:
                destination = (reader.f32(), reader.f32(), reader.f32())
        except ProtocolError:
            return

        obj = self.objects.get(guid)
        if obj is None:
            return  # noch nicht bekannt - das Create-Paket kommt gleich

        if not self._plausible(start):
            return
        obj.x, obj.y, obj.z = start
        obj.has_position = True
        obj.last_seen = time.time()

        moving = (
            move_type == 0
            and destination is not None
            and self._plausible(destination)
            and self._plausible_step(start, destination)
            and 50 <= duration_ms <= 30_000
        )
        if not moving:
            obj.spline_from = obj.spline_to = None
            obj.spline_duration = 0.0
            return

        obj.spline_from = (start[0], start[1])
        obj.spline_to = (destination[0], destination[1])
        obj.spline_start = time.time()
        obj.spline_duration = duration_ms / 1000.0

    @staticmethod
    def _plausible(point: "tuple[float, float, float]") -> bool:
        x, y, z = point
        return (
            abs(x) < 20000.0
            and abs(y) < 20000.0
            and abs(z) < 5000.0
            and x == x  # NaN abweisen
            and y == y
            and z == z
        )

    @staticmethod
    def _plausible_step(
        start: "tuple[float, float, float]", destination: "tuple[float, float, float]"
    ) -> bool:
        """Nur Sprünge bis 300 Yard glauben - sonst ist das Paket falsch gelesen."""
        dx = destination[0] - start[0]
        dy = destination[1] - start[1]
        dz = destination[2] - start[2]
        return dx * dx + dy * dy <= 300.0 * 300.0 and abs(dz) <= 200.0

    # ------------------------------------------------------------------ Parsing
    def _parse(self, data: bytes) -> None:
        self.updates_seen += 1
        reader = Reader(data)
        try:
            count = reader.u32()
        except ProtocolError:
            return

        for _ in range(count):
            try:
                self._read_block(reader)
            except SkipUpdate:
                # Nicht unterstützter Block (z. B. GameObject): Rest des Pakets
                # ignorieren, statt falsch weiterzulesen.
                return
            except ProtocolError as exc:
                log.debug("Update-Block unvollständig: %s", exc)
                return

    def _read_block(self, reader: Reader) -> None:
        update_type = reader.u8()
        guid = reader.packed_guid()

        if update_type == opcodes.UPDATETYPE_VALUES:
            obj = self.objects.get(guid) or WoWObject(guid=guid, type_id=0)
            self.objects[guid] = obj
            self._read_values(reader, obj)
            obj.last_seen = time.time()
            return

        if update_type == opcodes.UPDATETYPE_MOVEMENT:
            obj = self.objects.get(guid)
            position = self._read_movement_update(reader)
            if obj is not None and position is not None:
                self._apply_position(obj, position)
            return

        if update_type in (opcodes.UPDATETYPE_CREATE_OBJECT, opcodes.UPDATETYPE_CREATE_OBJECT2):
            type_id = reader.u8()
            obj = self.objects.get(guid)
            if obj is None:
                obj = WoWObject(guid=guid, type_id=type_id)
                self.objects[guid] = obj
            else:
                obj.type_id = type_id

            position = self._read_movement_update(reader)
            if position is not None:
                self._apply_position(obj, position)
            self._read_values(reader, obj)
            obj.last_seen = time.time()
            return

        if update_type in (opcodes.UPDATETYPE_OUT_OF_RANGE_OBJECTS, opcodes.UPDATETYPE_NEAR_OBJECTS):
            count = reader.u32()
            for _ in range(count):
                gone = reader.packed_guid()
                if update_type == opcodes.UPDATETYPE_OUT_OF_RANGE_OBJECTS:
                    self.objects.pop(gone, None)
            return

        raise SkipUpdate

    @staticmethod
    def _apply_position(obj: WoWObject, position: tuple[float, float, float, float]) -> None:
        obj.x, obj.y, obj.z, obj.o = position
        obj.has_position = True
        # Der Server hat die Einheit direkt gesetzt: alten Laufweg verwerfen
        obj.spline_from = obj.spline_to = None
        obj.spline_duration = 0.0

    def _read_movement_update(
        self, reader: Reader
    ) -> tuple[float, float, float, float] | None:
        """Liest einen Movement-/Create-Block ab dem Flag-Feld.

        Aufbau (TrinityCore Object::BuildMovementUpdate):
        uint16 updateFlags, dann je nach Flags MovementInfo + Geschwindigkeiten
        (+ Spline), oder eine reine Positionsangabe, danach optionale Zusatzfelder.
        """
        flags = reader.u16()
        position: tuple[float, float, float, float] | None = None

        if flags & UPDATEFLAG_LIVING:
            info = read_movement_info(reader)
            position = (info.x, info.y, info.z, info.o)
            # 9 Geschwindigkeiten/Raten: walk, run, run_back, swim, swim_back,
            # flight, flight_back, turn_rate, pitch_rate
            for _ in range(9):
                reader.f32()
            if info.flags & MOVEFLAG_SPLINE_ENABLED:
                self._skip_spline(reader)
        elif flags & UPDATEFLAG_POSITION:
            reader.packed_guid()  # Transport-GUID oder ein einzelnes Null-Byte
            x, y, z = reader.f32(), reader.f32(), reader.f32()
            reader.f32(), reader.f32(), reader.f32()  # Transport-Offset oder Position
            orientation = reader.f32()
            reader.f32()  # Orientierung nochmal
            position = (x, y, z, orientation)
        elif flags & UPDATEFLAG_STATIONARY_POSITION:
            position = (reader.f32(), reader.f32(), reader.f32(), reader.f32())

        if flags & UPDATEFLAG_UNKNOWN:
            reader.u32()
        if flags & UPDATEFLAG_LOWGUID:
            reader.u32()
        if flags & UPDATEFLAG_HAS_TARGET:
            reader.packed_guid()  # Ziel-GUID oder ein einzelnes Null-Byte
        if flags & UPDATEFLAG_TRANSPORT:
            reader.u32()
        if flags & UPDATEFLAG_VEHICLE:
            reader.u32()
            reader.f32()
        if flags & UPDATEFLAG_ROTATION:
            reader.u64()

        return position

    @staticmethod
    def _skip_spline(reader: Reader) -> None:
        """Überspringt den Spline-Block (CommonMovement::WriteCreateObjectSplineDataBlock)."""
        spline_flags = reader.u32()
        if spline_flags & SPLINEFLAG_FINAL_ANGLE:
            reader.f32()
        elif spline_flags & SPLINEFLAG_FINAL_TARGET:
            reader.u64()
        elif spline_flags & SPLINEFLAG_FINAL_POINT:
            reader.f32(), reader.f32(), reader.f32()
        reader.i32()  # elapsed
        reader.u32()  # duration
        reader.u32()  # id
        reader.f32()  # duration modifier
        reader.f32()  # next duration modifier
        reader.f32()  # vertical acceleration
        reader.u32()  # effect start time
        points = reader.u32()
        reader.raw(points * 12)
        reader.u8()  # mode
        reader.f32(), reader.f32(), reader.f32()  # destination

    def _read_values(self, reader: Reader, obj: WoWObject) -> None:
        block_count = reader.u8()
        masks = [reader.u32() for _ in range(block_count)]
        for word_index, mask in enumerate(masks):
            if mask == 0:
                continue
            for bit in range(32):
                if not mask & (1 << bit):
                    continue
                value = reader.u32()
                index = word_index * 32 + bit
                obj.raw[index] = value
                if index == opcodes.FIELD_OBJECT_ENTRY:
                    obj.entry = value
                elif index == opcodes.FIELD_UNIT_HEALTH:
                    previous = obj.health
                    obj.health = value
                    if previous and previous != value and self.on_health_change is not None:
                        try:
                            self.on_health_change(obj.guid, previous, value, obj)
                        except Exception:  # noqa: BLE001 - Logging darf das Parsen nicht stoppen
                            log.debug("Health-Callback fehlgeschlagen", exc_info=True)
                elif index == opcodes.FIELD_UNIT_MAX_HEALTH:
                    obj.max_health = value
                elif index == opcodes.FIELD_CORPSE_OWNER_LOW:
                    obj.owner_guid = (obj.owner_guid & 0xFFFFFFFF00000000) | value
                elif index == opcodes.FIELD_CORPSE_OWNER_HIGH:
                    obj.owner_guid = (obj.owner_guid & 0xFFFFFFFF) | (value << 32)
                elif index == opcodes.FIELD_UNIT_POWER:
                    obj.power = value
                elif index == opcodes.FIELD_UNIT_MAX_POWER:
                    obj.max_power = value
                elif index == opcodes.FIELD_UNIT_BYTES_0:
                    if obj.is_player:
                        obj.power_type = (value >> 24) & 0xFF
                elif index == opcodes.FIELD_UNIT_LEVEL:
                    obj.level = value
                elif index == opcodes.FIELD_PLAYER_XP:
                    obj.xp = value
                elif index == opcodes.FIELD_PLAYER_NEXT_LEVEL_XP:
                    obj.next_level_xp = value
                elif index == opcodes.FIELD_PLAYER_FLAGS:
                    obj.player_flags = value
                elif index == opcodes.FIELD_UNIT_FACTION:
                    obj.faction = value
                elif index == opcodes.FIELD_UNIT_FLAGS:
                    obj.flags = value
                elif index == opcodes.FIELD_UNIT_NPC_FLAGS:
                    obj.npc_flags = value
                elif index == opcodes.FIELD_UNIT_DYNAMIC_FLAGS:
                    obj.dynamic_flags = value
