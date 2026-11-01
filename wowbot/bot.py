"""Der eigentliche Farming-Bot.

Ablauf: einloggen -> Charakter betreten -> Farm-Schleife
(Ziel suchen, hinlaufen, angreifen, looten, ausruhen, Waypoints ablaufen).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import app_support_dir, opcodes
from .auth import AuthClient, Realm
from .filters import LootRules, MobRules
from .mapdata import ClientMaps, find_client_dir
from .objects import ObjectManager, WoWObject
from .protocol import (
    MOVEFLAG_FORWARD,
    TARGET_FLAG_UNIT,
    MovementInfo,
    ProtocolError,
    Reader,
    Writer,
    read_movement_info,
    write_movement_info,
)
from .terrain import Terrain
from .wayfinder import Wayfinder
from .world import WorldClient, WorldError

log = logging.getLogger("wowbot.bot")

RUN_SPEED = 7.0  # Yard pro Sekunde (Standard-Lauftempo ohne Sonderfertigkeiten)
# So weit reicht die Kartenansicht. Namen werden bis hierher abgefragt, sonst
# steht im Tooltip "Entry 1234" und es gibt kein Symbol dazu.
MAP_VIEW_RADIUS = 150.0


def _plausible_name(raw: bytes) -> str:
    """Erster lesbarer Text in einem Paket - Notnagel fuer fremde Item-Antworten."""
    segment = bytearray()
    for byte in raw:
        if byte == 0:
            text = segment.decode("utf-8", "ignore").strip()
            if len(text) >= 3 and text.isprintable():
                return text
            segment.clear()
            continue
        segment.append(byte)
        if len(segment) > 120:
            segment.clear()
    return ""


@dataclass
class Character:
    guid: int
    name: str
    level: int


class FarmBot:
    def __init__(self, config: dict, trace: bool = False) -> None:
        self.config = config
        self.server_cfg = config.get("server", {})
        self.account_cfg = config.get("account", {})
        self.behavior = config.get("behavior", {})
        self.auth = AuthClient()
        self.world = WorldClient()
        self.objects = ObjectManager()
        self.self_guid: int | None = None
        self.position: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.map_id = 0
        self.started_at = time.time()
        self.move_time_ms = 0
        self.hotspot_index = 0
        # Befehl aus der Karte: "goto" (Punkt erreichen) oder "attack" (Mob zuerst).
        # Gesetzt aus dem GUI-Thread, abgearbeitet im Tick.
        self._command: "dict | None" = None
        self._command_logged = False
        self._command_tries = 0
        self.blacklist: dict[int, float] = {}
        self._face_requested = False
        self._facing_fixes = 0
        self._last_fight_guid: int | None = None
        self._finish_fight_warned = False
        self._death_position: tuple[float, float, float] | None = None
        self._death_map = 0
        self._was_dead = False
        self._return_attempts = 0
        self._corpse_guid: int | None = None
        self._graveyard_pos: tuple[float, float, float] | None = None
        self._graveyard_walked = False
        self._reclaim_delay_until = 0.0
        self._rest_after_revive = False
        self._detour_requested = False
        self._detour_side = 1
        self._detour_attempts = 0
        self._force_approach = False
        self._blocked_here = False
        self._cast_failures: list[float] = []
        self._heal_missing_warned = False
        self._combat_heals_used = 0
        self._heal_after_fight = False
        self._heal_spell: int | None = None
        self._heal_probe_done = False
        self._spell_rejected: set[int] = set()
        self._cast_failed_logged: dict[tuple[int, int], tuple[int, float]] = {}
        self._probe_wait_until = 0.0
        self._saw_ghost = False
        self._ghost_cleared_at = 0.0
        self._waiting_health = False
        self._attack_target: int | None = None
        self._attack_logged_for: int | None = None
        self._attack_restarts = 0
        self._chase_guid: int | None = None
        self._chase_started = 0.0
        self._sitting = False
        self._last_attacker = 0
        self._healer_entry_set = {
            int(entry) for entry in (self.behavior.get("spirit_healer_entries") or [6491])
        }
        self._attack_confirmed = True
        self._attack_sent_at = 0.0
        self._attack_rejected = 0
        self._abandon_target = False
        self._last_target_hp: int | None = None
        self._last_damage_seen_at = 0.0
        self.target_guid: int | None = None
        self.last_spell_at = 0.0
        self.last_attack_at = 0.0
        self.looted = 0
        self.copper = 0
        self.kills = 0
        self.xp_session = 0
        self._xp_at_fight_start = 0
        self._fight_damage = 0
        self._swing_hits = 0
        self._swing_misses = 0
        self._last_swing_at = 0.0
        # Schaetzung der Mob-HP: das Trefferpaket kommt vor dem Update der Felder.
        self._target_hp_est = 0
        self._xp_shown = 0
        # Zeitpunkt des letzten Schadens am eigenen Charakter (fuer den Rast-Abbruch).
        self._last_damage_at = 0.0
        self._cast_until = 0.0
        self._last_level = 0
        self._loot_queue: list[int] = []
        self._looted_guids: set[int] = set()
        self._loot_attempts: dict[int, int] = {}
        self.creature_names: dict[int, str] = {}
        self._name_requests: dict[int, float] = {}
        # Regeln aus dem Reiter „Ziele & Beute" - einmal gebaut, dann fest
        self._mob_rules = MobRules.from_behavior(self.behavior)
        self._loot_rules = LootRules.from_behavior(self.behavior)
        self.item_names: dict[int, str] = {
            int(entry): str(name)
            for entry, name in (self.behavior.get("item_names") or {}).items()
            if str(name).strip()
        }
        self._item_requests: dict[int, float] = {}
        self._filter_logged = False
        # Regeln aus dem Fenster koennen im Betrieb nachgeliefert werden
        self._rules_dirty = False
        self._rule_abort = False
        self._engaging = False
        self._tasks: set = set()
        self.running = True
        self._last_status = 0.0
        self._last_progress = 0.0
        self._engage_started = 0.0
        self._engage_swings = 0
        self._scan_stats: tuple[int, int, int, int, tuple[float, int] | None] = (0, 0, 0, 0, None)
        self._spell_ready: dict[int, float] = {}
        self._map_walls: list[tuple[float, float]] = []
        self._map_walls_version = -1
        self._in_sidestep = False
        self._sidestep_side = 1
        self.wayfinder = Wayfinder(
            cell_size=float(self.behavior.get("path_cell_size", 5.0)),
            memory_seconds=float(self.behavior.get("path_memory_seconds", 600)),
            storage_dir=self._nav_directory(),
        )
        # Gelände (Klippen, Wasser) aus dem Client - wird im Hintergrund geladen
        self.terrain: "Terrain | None" = None
        self._terrain_started = False
        self._terrain_tiles = 0
        self._potion_ready_at = 0.0
        self._dead_since = 0.0
        self.world.trace = trace
        self.objects.on_health_change = self._on_health_change

        self.world.on(opcodes.SMSG_UPDATE_OBJECT, self.objects.handle_update_object)
        self.world.on(
            opcodes.SMSG_COMPRESSED_UPDATE_OBJECT, self.objects.handle_compressed_update_object
        )
        self.world.on(opcodes.SMSG_DESTROY_OBJECT, self.objects.handle_destroy_object)
        self.world.on(opcodes.SMSG_MONSTER_MOVE, self.objects.handle_monster_move)
        self.world.on(opcodes.SMSG_TIME_SYNC_REQ, self._on_time_sync)
        self.world.on(opcodes.MSG_MOVE_TELEPORT, self._on_move_teleport_broadcast)
        self.world.on(opcodes.MSG_MOVE_TELEPORT_ACK, self._on_move_teleport_ack)
        self.world.on(opcodes.SMSG_ATTACKER_STATE_UPDATE, self._on_attacker_state_update)
        self.world.on(opcodes.SMSG_ATTACK_SWING_CANT_ATTACK, self._on_attack_error)
        self.world.on(opcodes.SMSG_ATTACK_SWING_NOT_IN_RANGE, self._on_attack_not_in_range)
        self.world.on(opcodes.SMSG_ATTACK_SWING_BAD_FACING, self._on_attack_bad_facing)
        self.world.on(opcodes.SMSG_ATTACK_START, self._on_attack_start)
        self.world.on(opcodes.SMSG_ATTACK_STOP, self._on_attack_stop)
        self.world.on(opcodes.SMSG_SPELL_FAILURE, self._on_spell_failure)
        self.world.on(opcodes.SMSG_CAST_FAILED, self._on_cast_failed)
        self.world.on(opcodes.SMSG_LOG_XPGAIN, self._on_log_xp_gain)
        self.world.on(
            opcodes.SMSG_CREATURE_QUERY_RESPONSE, self._on_creature_query_response
        )
        self.world.on(
            opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE, self._on_item_query_response
        )
        self.world.on(opcodes.SMSG_LEVELUP_INFO, self._on_level_up)
        self.world.on(opcodes.SMSG_DEATH_RELEASE_LOC, self._on_death_release_loc)
        self.world.on(
            opcodes.SMSG_CORPSE_RECLAIM_DELAY, self._on_corpse_reclaim_delay
        )

    # --------------------------------------------------------------- Verbindung
    async def connect(self) -> Character:
        auth_host = self.server_cfg.get("host", "127.0.0.1")
        auth_port = int(self.server_cfg.get("auth_port", 3724))
        username = self.account_cfg["username"]
        password = self.account_cfg["password"]

        await self.auth.connect(auth_host, auth_port)
        await self.auth.login(
            username,
            password,
            locale=self.config.get("locale", "deDE"),
            timezone_bias=int(self.config.get("timezone_bias", -60)),
        )
        realms = await self.auth.realmlist()
        realm = self._pick_realm(realms)

        world_host = self.server_cfg.get("world_host") or realm.host
        world_port = int(self.server_cfg.get("world_port") or realm.port)
        await self.world.connect(world_host, world_port)
        await self.world.handshake(username, self.auth.session_key, realm.id)

        characters = await self.enum_characters()
        character = self._pick_character(characters)
        log.info("Betrete Welt als %s (Level %d)", character.name, character.level)
        await self.enter_world(character)
        return character

    def _pick_realm(self, realms: list[Realm]) -> Realm:
        if not realms:
            raise WorldError("Der Login-Server hat keine Realms geliefert")
        wanted = self.server_cfg.get("realm")
        if wanted:
            for realm in realms:
                if realm.name.lower() == wanted.lower():
                    return realm
            log.warning("Realm '%s' nicht gefunden, nehme '%s'", wanted, realms[0].name)
        return realms[0]

    async def enum_characters(self) -> list[Character]:
        await self.world.send(opcodes.CMSG_CHAR_ENUM)
        body = await self.world.wait_for(opcodes.SMSG_CHAR_ENUM, timeout=15.0)
        return _parse_char_enum(body)

    def _pick_character(self, characters: list[Character]) -> Character:
        if not characters:
            raise WorldError("Auf dem Account gibt es keinen Charakter")
        wanted = self.config.get("character")
        if wanted:
            for character in characters:
                if character.name.lower() == wanted.lower():
                    return character
            raise WorldError(
                f"Charakter '{wanted}' nicht gefunden (vorhanden: "
                + ", ".join(c.name for c in characters)
                + ")"
            )
        return max(characters, key=lambda c: c.level)

    async def enter_world(self, character: Character) -> None:
        self.self_guid = character.guid
        self.objects.self_guid = character.guid

        writer = Writer().u64(character.guid)
        await self.world.send(opcodes.CMSG_PLAYER_LOGIN, writer.build())

        body = await self.world.wait_for(opcodes.SMSG_LOGIN_VERIFY_WORLD, timeout=60.0)
        reader = Reader(body)
        self.map_id = reader.u32()
        self.position = (reader.f32(), reader.f32(), reader.f32(), reader.f32())
        log.info(
            "In der Welt: Map %d bei (%.1f, %.1f, %.1f)", self.map_id, *self.position[:3]
        )

        # Der Server muss wissen, dass wir unseren eigenen Charakter bewegen.
        await self.world.send(
            opcodes.CMSG_SET_ACTIVE_MOVER, Writer().u64(character.guid).build()
        )
        # Direkt nach dem Login ist der Charakter manchmal noch nicht in der Map,
        # dann wird der Mover abgelehnt und ALLE Bewegungspakete werden verworfen.
        self._spawn(self._resend_active_mover(1.0))
        self._spawn(self._resend_active_mover(3.0))

    async def _resend_active_mover(self, delay: float | None = None) -> None:
        """Meldet den Charakter erneut als aktiven Mover an (nach Login/resync)."""
        if delay:
            await asyncio.sleep(delay)
        if self.self_guid is None:
            return
        try:
            await self.world.send(
                opcodes.CMSG_SET_ACTIVE_MOVER, Writer().u64(self.self_guid).build()
            )
        except WorldError:
            return

    # ------------------------------------------------------------------ Farming
    async def run(self) -> None:
        session_limit = float(self.behavior.get("max_session_minutes", 480)) * 60
        break_every = float(self.behavior.get("break_every_minutes", 50)) * 60
        break_length = float(self.behavior.get("break_minutes", 5)) * 60
        next_break = time.time() + break_every

        try:
            while self.running:
                if not self.world.connected:
                    log.warning("Verbindung weg - beende Farm-Schleife")
                    return
                if time.time() - self.started_at > session_limit:
                    log.info("Session-Limit erreicht, Bot beendet sich")
                    return
                if time.time() >= next_break:
                    log.info("Pause für %.0f Minuten", break_length / 60)
                    await self._idle(break_length)
                    next_break = time.time() + break_every
                    continue

                await self._tick()
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        self._start_terrain()
        # Karte kann sich durch Teleport/Portal ändern - Hindernisse hängen an ihr.
        self.wayfinder.set_map(self.map_id)
        self._update_terrain()
        # Laufende Kreaturen mitwandern lassen (Reichweite, Blickrichtung, Karte)
        self.objects.advance_splines()
        self.objects.forget_stale(
            float(self.behavior.get("object_timeout_seconds", 120))
        )
        me = self.objects.me
        if me is not None and me.has_position:
            self.position = (me.x, me.y, me.z, me.o)
        if me is not None and me.level:
            if self._last_level and me.level != self._last_level:
                log.info("[level] Stufe %d erreicht!", me.level)
            self._last_level = me.level

        self._request_names()
        self._apply_rules()

        if not self._filter_logged and me is not None and me.level:
            self._filter_logged = True
            log.info(
                "[filter] Ziele: %s | Beute: %s",
                self._mob_rules.summary(me.level),
                self._loot_rules.summary(),
            )

        if me is not None and me.is_ghost:
            self._saw_ghost = True

        if me is not None and me.max_health and not self._revived():
            if not self._was_dead:
                # Neuer Tod: Position sofort merken (ueberschreibt den letzten Tod).
                self._was_dead = True
                self._save_death_position()
                self._clear_command("gestorben - Kartenbefehl verworfen")
            await self._wait_for_resurrection()
            return
        self._was_dead = False
        self._dead_since = 0.0
        self._waiting_health = False

        if self._death_position is not None:
            # Nach dem Tod zuerst zur Todesstelle zurücklaufen, dann weiterfarmen.
            await self._return_to_death_spot()
            self._status()
            return

        # Klick auf der Karte: erst diesen Befehl abarbeiten, dann normal weiter.
        if await self._run_command(me):
            self._status()
            return

        if not self._heal_probe_done:
            # Einmal beim Start die passende Rank des Heilszaubers ermitteln.
            self._heal_probe_done = await self._probe_heal_spell()
            if self._heal_probe_done:
                return

        if self._rest_after_revive:
            self._rest_after_revive = False
            log.info("[tod] wiederbelebt - heile mich zuerst, dann farme ich weiter")
            if await self._rest():
                return

        if me is not None and me.max_health:
            rest_threshold = float(self.behavior.get("rest_hp_pct", 30))
            if self._heal_after_fight:
                # Nach einem Kampf mit Heilzaubern erst hochheilen, statt mit
                # halben HP direkt den naechsten Mob anzugreifen.
                rest_threshold = max(rest_threshold, self._combat_heal_pct())
            if me.health_percent < rest_threshold:
                still_fighting = self._last_target_alive()
                if still_fighting is not None:
                    # Erst den laufenden Kampf beenden, danach heilen.
                    if not self._finish_fight_warned:
                        self._finish_fight_warned = True
                        log.info(
                            "[kampf] HP %.0f %% - aber %s lebt noch: erst zu Ende kämpfen, dann Pause",
                            me.health_percent,
                            self._short_name(still_fighting),
                        )
                else:
                    self._finish_fight_warned = False
                    self._heal_after_fight = False
                    if await self._rest():
                        return
                    # Pause abgebrochen (angegriffen) - unten weiterkämpfen.
            if me.uses_mana and me.power_percent < float(
                self.behavior.get("rest_mana_pct", 20)
            ):
                # Nur Manaklassen warten auf Mana. Wut/Energie fuellen sich im
                # Kampf auf - ein Warten darauf wuerde nur Zeit kosten.
                await self._rest()
                return
            await self._try_heal()
            await self._try_potion(me)

        target = self._choose_target()
        if target is None:
            self._loot_nearby()
            if self._loot_queue:
                await self._process_loot_queue()
                self._status()
                return
            await self._try_drink(me)
            # Nichts im Suchradius? Dann zum nächsten bekannten Mob laufen.
            if await self._walk_to_nearest_mob():
                self._status()
                return
            await self._roam()
            return

        await self._engage(target)
        self._status()

    def snapshot(self) -> dict:
        """Zustand für die GUI.

        Wird aus dem GUI-Thread gelesen: alle Werte sind einfache Attribute,
        es wird nichts verändert (deshalb bewusst nicht `_revived()`, das
        setzt den Geist-Zustand).
        """
        me = self.objects.me
        target = self.objects.get(self.target_guid) if self.target_guid else None
        next_xp = me.next_level_xp if me else 0
        radius = float(
            self.behavior.get(
                "nearby_radius", self.behavior.get("chase_radius", 120)
            )
        )
        limit = int(self.behavior.get("nearby_list_count", 5))
        nearby = [
            {
                "guid": obj.guid,
                "name": self.creature_names.get(obj.entry) or f"Entry {obj.entry}",
                "entry": obj.entry,
                "level": obj.level,
                "hp": obj.health,
                "max_hp": obj.max_health,
                "hp_pct": obj.health_percent,
                "dist": self._distance_to(obj),
                "attackable": obj.is_attackable,
                "target": obj.guid == self.target_guid,
            }
            for obj in self._nearby_units(radius, max(limit, 1))
        ]
        return {
            "hp": me.health if me else 0,
            "max_hp": me.max_health if me else 0,
            "hp_pct": me.health_percent if me else 0.0,
            "power": {
                "name": me.power_name if me else "",
                "value": me.power if me else 0,
                "max": me.max_power if me else 0,
                "pct": me.power_percent if me else 0.0,
            },
            "level": me.level if me else 0,
            "xp": me.xp if me else 0,
            "next_xp": next_xp,
            "xp_pct": (100.0 * me.xp / next_xp) if me and next_xp else 0.0,
            "kills": self.kills,
            "loot": self.looted,
            "copper": self.copper,
            "objects": len(self.objects.objects),
            "pos": (self.position[0], self.position[1]),
            "minutes": (time.time() - self.started_at) / 60.0,
            "target": self._short_name(target) if target is not None else "",
            "target_info": (
                {
                    "name": self.creature_names.get(target.entry)
                    or f"Entry {target.entry}",
                    "entry": target.entry,
                    "level": target.level,
                    "hp": target.health,
                    "max_hp": target.max_health,
                    "hp_pct": target.health_percent,
                    "dist": self._distance_to(target),
                }
                if target is not None and target.has_position
                else {}
            ),
            "target_hp": (
                f"{target.health}/{target.max_health}"
                if target is not None and target.max_health
                else ""
            ),
            "target_dist": (
                self._distance_to(target)
                if target is not None and target.has_position
                else 0.0
            ),
            "dead": bool(me and me.max_health and (me.is_ghost or me.health <= 0)),
            "nearby": nearby,
            "items": dict(self.item_names),
            "map": self.map_id,
            "walls": self.wayfinder.wall_count(),
        }

    def _status(self) -> None:
        now = time.time()
        interval = float(self.behavior.get("status_seconds", 15))
        if interval <= 0 or now - self._last_status < interval:
            return
        self._last_status = now
        me = self.objects.me
        hp = (
            f"{me.health}/{me.max_health} ({me.health_percent:.0f} %)" if me else "?"
        )
        minutes = (now - self.started_at) / 60.0
        target = self.objects.get(self.target_guid) if self.target_guid else None
        target_info = ""
        if target is not None:
            target_info = (
                f" | Ziel: {self._short_name(target)} "
                f"{target.health}/{target.max_health} HP"
            )
        mana = self._power_text(me)
        xp = ""
        if me is not None and me.next_level_xp:
            xp = (
                f" | EP {me.xp}/{me.next_level_xp}"
                f" ({100.0 * me.xp / me.next_level_xp:.1f} %)"
            )
        log.info(
            "[status] HP %s%s%s | Kills %d | Loot %d | Kupfer %d | Objekte %d | Pos %.1f/%.1f | %.0f min%s",
            hp,
            mana,
            xp,
            self.kills,
            self.looted,
            self.copper,
            len(self.objects.objects),
            self.position[0],
            self.position[1],
            minutes,
            target_info,
        )
        if log.isEnabledFor(logging.DEBUG):
            nearby = sorted(
                (
                    obj
                    for obj in self.objects.objects.values()
                    if obj.is_mob and obj.has_position and obj.alive
                ),
                key=self._distance_to,
            )[:3]
            for obj in nearby:
                log.debug(
                    "  in der Nähe: Entry %d, Level %d, %d/%d HP, %.1f yd",
                    obj.entry,
                    obj.level,
                    obj.health,
                    obj.max_health,
                    self._distance_to(obj),
                )

    def _estimated_hp_text(self, obj: WoWObject) -> str:
        """HP eines Mobs inklusive des gerade angekommenen Schlags."""
        if not obj.max_health:
            return "HP unbekannt"
        return (
            f"{self._target_hp_est}/{obj.max_health} HP "
            f"({100.0 * self._target_hp_est / obj.max_health:.0f} %)"
        )

    def _power_text(self, me: WoWObject | None, with_percent: bool = False) -> str:
        """Kraft des Charakters mit richtigem Namen (Mana, Wut, Energie ...)."""
        if me is None or not me.max_power:
            return ""
        percent = f" ({me.power_percent:.0f} %)" if with_percent else ""
        return f" | {me.power_name} {me.power}/{me.max_power}{percent}"

    def _own_level(self) -> int:
        me = self.objects.me
        return me.level if me is not None and me.level else 0

    def _mob_allowed(self, obj: WoWObject, own_level: int = 0) -> bool:
        """Entscheidet der Reiter „Ziele & Beute“ diesen Mob?"""
        return self._mob_rules.allows(
            obj.entry,
            self.creature_names.get(obj.entry),
            obj.level,
            own_level or self._own_level(),
        )

    def _nearby_mobs(self, radius: float, limit: int) -> list[WoWObject]:
        """Angreifbare, lebende Mobs in der Nähe - nach Entfernung sortiert."""
        mobs = [
            obj
            for obj in self.objects.objects.values()
            if obj.is_mob
            and obj.guid != self.self_guid
            and obj.alive
            and obj.has_position
            and obj.is_attackable
            and obj.entry not in self._healer_entry_set
            and self._distance_to(obj) <= radius
        ]
        mobs.sort(key=self._distance_to)
        return mobs[:limit]

    def _describe_mob(self, obj: WoWObject) -> str:
        """Name, Level, HP und Entfernung eines Mobs fürs Log."""
        hp = (
            f"{obj.health}/{obj.max_health} HP ({obj.health_percent:.0f} %)"
            if obj.max_health
            else "HP unbekannt"
        )
        text = f"{self._describe(obj)} - {hp}, {self._distance_to(obj):.0f} yd"
        if not self._mob_allowed(obj):
            text += " · nach Regeln kein Ziel"
        return text

    def log_nearby(self, label: str = "umgebung") -> None:
        """Listet die Mobs in der Nähe mit Level und HP auf."""
        limit = int(self.behavior.get("nearby_list_count", 5))
        if limit <= 0:
            return
        radius = float(
            self.behavior.get(
                "nearby_radius", self.behavior.get("chase_radius", 120)
            )
        )
        mobs = self._nearby_mobs(radius, limit)
        if not mobs:
            log.info("[%s] keine Mobs in %.0f yd", label, radius)
            return
        log.info("[%s] %d Mob(s) in %.0f yd:", label, len(mobs), radius)
        for mob in mobs:
            log.info("  [mob] %s", self._describe_mob(mob))

    def _nearby_units(self, radius: float, limit: int) -> list[WoWObject]:
        """Alle Einheiten in der Nähe, auch neutrale und freundliche.

        Anders als `_nearby_mobs` (nur angreifbare Ziele) dient das der Anzeige:
        Vendor, Geistheiler & Co. tauchen so mit auf, markiert als nicht
        angreifbar. Tote Einheiten bleiben draußen, das sind Leichen.
        """
        units = [
            obj
            # Auch aus dem GUI-Thread gelesen: erst eine Momentaufnahme machen.
            for obj in list(self.objects.objects.values())
            if obj.is_mob
            and obj.guid != self.self_guid
            and obj.alive
            and obj.has_position
            and self._distance_to(obj) <= radius
        ]
        units.sort(key=self._distance_to)
        return units[: max(limit, 1)]

    def _nearest_mob(self, radius: float) -> WoWObject | None:
        """Nächster angreifbarer Mob innerhalb von `radius` Yard (nach Regeln)."""
        healer = set(self.behavior.get("spirit_healer_entries") or [])
        own_level = self._own_level()
        now = time.time()
        best: tuple[float, WoWObject] | None = None
        for obj in self.objects.objects.values():
            if not obj.is_mob or obj.guid == self.self_guid:
                continue
            if not obj.alive or not obj.has_position:
                continue
            if not obj.is_attackable or obj.entry in healer:
                continue
            if self.blacklist.get(obj.entry, 0) > now:
                continue
            if not self._mob_allowed(obj, own_level):
                continue
            distance = self._distance_to(obj)
            if distance > radius:
                continue
            if best is None or distance < best[0]:
                best = (distance, obj)
        return best[1] if best else None

    async def _walk_to_nearest_mob(self) -> bool:
        """Läuft zum nächsten Mob, auch wenn er ausserhalb von max_target_distance steht."""
        radius = float(self.behavior.get("chase_radius", 120))
        max_target = float(self.behavior.get("max_target_distance", 40))
        mob = self._nearest_mob(radius)
        if mob is None:
            self._chase_guid = None
            return False

        now = time.time()
        if self._chase_guid != mob.guid:
            self._chase_guid = mob.guid
            self._chase_started = now
        elif now - self._chase_started > float(
            self.behavior.get("chase_timeout_seconds", 90)
        ):
            log.info(
                "[suche] %s nicht erreichbar - 60 s ignorieren",
                self._short_name(mob),
            )
            self.blacklist[mob.entry] = now + 60
            self._chase_guid = None
            return False

        log.info(
            "[suche] kein Ziel in %.0f yd - laufe zum nächsten: %s in %.0f yd",
            max_target,
            self._short_name(mob),
            self._distance_to(mob),
        )
        arrived = await self._approach(
            mob,
            float(self.behavior.get("melee_range", 4.5)) * 0.8,
            abort=self._command_pending,
        )
        if arrived:
            self._chase_guid = None
            return True
        if self._blocked_here:
            # Hindernis auf dem Weg: nicht ewig gegen dieselbe Ecke laufen.
            log.info(
                "[suche] %s nicht erreichbar (Hindernis) - 60 s ignorieren",
                self._short_name(mob),
            )
            self.blacklist[mob.entry] = now + 60
            self._chase_guid = None
            return False
        return True

    def _choose_target(self) -> WoWObject | None:
        max_distance = float(self.behavior.get("max_target_distance", 40))
        own_level = self._own_level()
        now = time.time()
        best: tuple[float, WoWObject] | None = None
        units = alive = with_position = in_range = 0
        nearest: tuple[float, int] | None = None

        for obj in self.objects.objects.values():
            if not obj.is_mob or obj.guid == self.self_guid:
                continue
            units += 1
            if obj.alive:
                alive += 1
            if obj.has_position:
                with_position += 1
            else:
                continue
            distance = self._distance_to(obj)
            if nearest is None or distance < nearest[0]:
                nearest = (distance, obj.entry)
            if not obj.alive:
                continue
            if not obj.is_attackable or obj.entry in self._healer_entry_set:
                continue
            if not self._mob_allowed(obj, own_level):
                continue
            if distance > max_distance:
                continue
            in_range += 1
            if self.blacklist.get(obj.entry, 0.0) > now:
                continue
            if best is None or distance < best[0]:
                best = (distance, obj)

        self._scan_stats = (units, alive, with_position, in_range, nearest)
        return best[1] if best else None

    # ------------------------------------------------------------------ Kampf
    async def _engage(self, target: WoWObject) -> None:
        leash = float(self.behavior.get("leash_range", 70))
        melee_range = float(self.behavior.get("melee_range", 4.5))
        use_melee = bool(self.behavior.get("melee", True))
        deadline = time.time() + 120

        if self.target_guid != target.guid:
            self.target_guid = target.guid
            self._last_fight_guid = target.guid
            self._finish_fight_warned = False
            self._engage_started = time.time()
            self._engage_swings = 0
            self._fight_damage = 0
            self._swing_hits = 0
            self._swing_misses = 0
            self._last_swing_at = 0.0
            self._target_hp_est = target.health
            self._xp_at_fight_start = self.xp_session
            self._detour_attempts = 0
            self._detour_requested = False
            self._force_approach = False
            self._combat_heals_used = 0
            self._attack_target = None
            self._attack_restarts = 0
            self._last_target_hp = target.health
            self._last_damage_seen_at = time.time()
            await self.world.send(
                opcodes.CMSG_SET_SELECTION, Writer().u64(target.guid).build()
            )
            if target.entry not in self.creature_names:
                # Kurz auf den Namen warten, damit die Zeile lesbar ist.
                await self._fetch_name(target.entry, target.guid)
            log.info(
                "[ziel] %s - %d/%d HP, %.1f yd entfernt",
                self._describe(target),
                target.health,
                target.max_health,
                self._distance_to(target),
            )

        def command_wants_other() -> bool:
            """Will ein Kartenbefehl gerade nicht diesen Mob? Dann Kampf beenden.

            Klick auf die Karte = hinlaufen: bricht den Kampf sofort ab.
            Klick auf einen anderen Mob: er soll zuerst dran sein.
            """
            command = self._command
            if command is None:
                return False
            if command.get("kind") == "goto":
                return True
            if command.get("kind") == "attack":
                return int(command.get("guid") or 0) != target.guid
            return False

        self._engaging = True
        self._rule_abort = False
        try:
            while self.running and target.alive and time.time() < deadline:
                if not self.world.connected:
                    return
                # Auch im Kampf mitlaufen: ein fliehender Mob bleibt so in Reichweite
                self.objects.advance_splines()
                if command_wants_other():
                    log.info(
                        "[befehl] %s - breche den Kampf mit %s ab (Ziel hat noch %d HP)",
                        "Kartenklick (hinlaufen)"
                        if self._goto_pending()
                        else "anderer Mob angeklickt",
                        self._short_name(target),
                        max(target.health, 0),
                    )
                    break
                # Der Server schickt unser eigenes Objekt laufend mit aktuellen Werten.
                current = self.objects.get(target.guid)
                if current is not None:
                    target = current
                me = self.objects.me
                if me is not None and me.max_health and me.is_dead_player:
                    log.error("[kampf] gestorben - Kampf wird abgebrochen")
                    break
                if self._rule_abort:
                    self._rule_abort = False
                    log.info(
                        "[filter] %s ist nach den neuen Regeln kein Ziel mehr - Kampf beendet",
                        self._short_name(target),
                    )
                    break
                self._apply_rules()
                if target.is_evading:
                    log.info(
                        "[kampf] %s entkommt (Evade) - ich lasse es und suche ein neues Ziel",
                        self._short_name(target),
                    )
                    self.blacklist[target.entry] = time.time() + 30
                    break
                distance = self._distance_to(target)
                if distance > leash:
                    log.info("Ziel zu weit weg (%.0f yd) - breche ab", distance)
                    break

                if self._face_requested:
                    self._face_requested = False
                    self._facing_fixes += 1
                    await self._face(target)
                    log.info(
                        "[dreh] Blickrichtung zu Entry %d korrigiert (%.0f°)",
                        target.entry,
                        math.degrees(self.position[3]) % 360,
                    )
                    await asyncio.sleep(self._jitter(0.2))

                if self._force_approach:
                    # Server sagt "ausser Reichweite", obwohl wir es anders sehen.
                    self._force_approach = False
                    await self._approach(target, melee_range * 0.5)

                if self._abandon_target:
                    self._abandon_target = False
                    log.info(
                        "[ziel] %s 60 s ignorieren und anderes Ziel suchen",
                        self._short_name(target),
                    )
                    self.blacklist[target.entry] = time.time() + 60
                    break

                if (
                    not self._attack_confirmed
                    and self._attack_target == target.guid
                    and time.time() - self._attack_sent_at > 2.5
                ):
                    # Kein SMSG_ATTACKSTART: der Server kennt unsere Position/Blickrichtung
                    # nicht (oder den Mover). Position neu senden und Angriff erneut anmelden.
                    self._attack_restarts += 1
                    if self._attack_restarts > int(self.behavior.get("attack_restarts", 3)):
                        log.warning(
                            "[!] Angriff wird nicht angenommen - ignoriere Entry %d 60 s",
                            target.entry,
                        )
                        self.blacklist[target.entry] = time.time() + 60
                        self._attack_restarts = 0
                        break
                    log.info(
                        "[!] Server hat den Angriff nicht bestätigt - sende Position neu und "
                        "melde den Angriff erneut an (%d/%d)",
                        self._attack_restarts,
                        int(self.behavior.get("attack_restarts", 3)),
                    )
                    await self._resync_movement()
                    self._attack_target = None
                    self._attack_confirmed = True
                    await asyncio.sleep(0.3)
                    continue

                if self._blocked_here:
                    self._blocked_here = False
                    self._detour_requested = True

                if self._detour_requested:
                    self._detour_requested = False
                    self._detour_attempts += 1
                    max_detours = int(self.behavior.get("detour_attempts", 3))
                    if self._detour_attempts > max_detours:
                        log.info(
                            "[umweg] %s nach %d Umwegen nicht erreichbar - ignoriere Entry %d 60 s",
                            self._short_name(target),
                            max_detours,
                            target.entry,
                        )
                        self.blacklist[target.entry] = time.time() + 60
                        break
                    if not await self._detour_around(
                        target, abort=command_wants_other
                    ):
                        break
                    continue

                # Kommt weder ein Trefferpaket noch eine HP-Aenderung, stimmt der
                # Serverzustand nicht (typisch nach "falsche Blickrichtung").
                if self._attack_target == target.guid:
                    if (
                        self._last_swing_at >= self._attack_sent_at
                        or target.health != self._last_target_hp
                    ):
                        self._last_target_hp = target.health
                        self._last_damage_seen_at = time.time()
                    elif time.time() - self._last_damage_seen_at > float(
                        self.behavior.get("attack_no_damage_seconds", 8)
                    ):
                        await self._recover_attack(target)
                        continue

                spell = self._pick_spell(target, distance)
                if spell is not None:
                    spell_range = float(spell.get("range", 30))
                    if distance > spell_range * 0.9:
                        await self._approach(
                            target, spell_range * 0.9, abort=command_wants_other
                        )
                    await self._face(target)
                    await self._cast(
                        int(spell["id"]), target.guid, float(spell.get("cooldown_ms", 2500))
                    )
                elif use_melee:
                    if distance > melee_range:
                        await self._approach(
                            target, melee_range * 0.8, abort=command_wants_other
                        )
                    await self._face(target)
                    await self._attack(target)
                else:
                    await self._approach(
                        target, melee_range, abort=command_wants_other
                    )

                # Im Kampf hoechstens N Heilzauber (Standard 2) - danach erst zu Ende
                # kaempfen und anschliessend heilen.
                if await self._combat_heal():
                    await asyncio.sleep(self._jitter(0.3))
                    continue
                if me is not None and me.max_health:
                    await self._try_potion(me)

                await asyncio.sleep(self._jitter(0.3))
                self.objects.forget_stale(
                    float(self.behavior.get("object_timeout_seconds", 120)),
                    keep={target.guid},
                )
        finally:
            self._engaging = False

        if self._attack_target is not None:
            self._attack_target = None
            try:
                await self.world.send(opcodes.CMSG_ATTACK_STOP)
            except WorldError:
                pass

        if not target.alive:
            if self._swing_hits or self._fight_damage:
                self.kills += 1
                duration = (
                    time.time() - self._engage_started if self._engage_started else 0.0
                )
                log.info(
                    "[kill] %s tot nach %.1f s, %d Treffer%s, %d Schaden (Kills gesamt: %d)",
                    self._short_name(target),
                    duration,
                    self._swing_hits,
                    f", {self._swing_misses} verfehlt" if self._swing_misses else "",
                    self._fight_damage,
                    self.kills,
                )
            else:
                # Ein anderer Spieler war schneller: kein Kill, keine EP, kein Report.
                duration = 0.0
                log.info(
                    "[ziel] %s war schon tot - zählt nicht als mein Kill",
                    self._short_name(target),
                )
            if self.behavior.get("loot", True):
                if target.guid not in self._loot_queue:
                    self._loot_queue.append(target.guid)
                self._loot_nearby()
                await self._process_loot_queue()
            if duration:
                # Kurz warten, damit das Erfahrungs-Paket des Servers noch ankommt.
                await asyncio.sleep(0.4)
                self._fight_report(target, duration)
        if self._combat_heals_used:
            # In diesem Kampf wurde geheilt - nach dem Kampf erst hochheilen.
            self._heal_after_fight = True
        self.target_guid = None

    async def _attack(self, target: WoWObject) -> None:
        """Startet den Autohit - einmal pro Ziel, denn die Schläge macht der Server."""
        if self._attack_target == target.guid:
            return
        self._attack_target = target.guid
        self.last_attack_at = time.time()
        self._last_target_hp = target.health
        self._last_damage_seen_at = time.time()
        await self._resync_movement()
        self._attack_confirmed = False
        self._attack_sent_at = time.time()
        if self._attack_logged_for == target.guid:
            log.info(
                "[angriff] Angriff auf %s erneut angemeldet (%.1f yd)",
                self._short_name(target),
                self._distance_to(target),
            )
        else:
            self._attack_logged_for = target.guid
            log.info(
                "[angriff] Angriff auf %s gestartet (%.1f yd) - Server schlägt automatisch zu",
                self._short_name(target),
                self._distance_to(target),
            )
        await self.world.send(
            opcodes.CMSG_ATTACK_SWING, Writer().u64(target.guid).build()
        )

    async def _resync_movement(self) -> None:
        """Position und Blickrichtung erneut senden, damit der Server sie sicher kennt."""
        if self.self_guid is None:
            return
        await self._resend_active_mover()
        try:
            await self._send_move(opcodes.MSG_MOVE_HEARTBEAT, 0)
        except WorldError:
            self.running = False

    async def _recover_attack(self, target: WoWObject) -> None:
        """Ziel nimmt keinen Schaden: Angriff stoppen, neu ausrichten, neu starten."""
        self._attack_restarts += 1
        max_restarts = int(self.behavior.get("attack_restarts", 3))
        log.info(
            "[!] %s nimmt keinen Schaden (HP %d/%d) - Angriff neu starten (%d/%d)",
            self._short_name(target),
            target.health,
            target.max_health,
            self._attack_restarts,
            max_restarts,
        )
        try:
            await self.world.send(opcodes.CMSG_ATTACK_STOP)
        except WorldError:
            self.running = False
            return
        self._attack_target = None
        self._last_target_hp = target.health
        self._last_damage_seen_at = time.time()
        self._last_swing_at = 0.0
        await asyncio.sleep(0.4)
        await self._face(target)
        await self._approach(
            target, float(self.behavior.get("melee_range", 4.5)) * 0.5
        )
        await self._attack(target)
        if self._attack_restarts > max_restarts:
            self._attack_restarts = 0
            log.info("[umweg] Angriff kommt nicht durch - versuche anderen Winkel")
            self._detour_requested = True

    def _rotation(self) -> list[dict]:
        spells = self.behavior.get("spells") or []
        if spells:
            return spells
        legacy = int(self.behavior.get("spell_id") or 0)
        if legacy:
            return [
                {
                    "id": legacy,
                    "range": float(self.behavior.get("spell_range", 30)),
                    "cooldown_ms": float(self.behavior.get("spell_interval_ms", 2500)),
                }
            ]
        return []

    def _pick_spell(self, target: WoWObject, distance: float) -> dict | None:
        now = time.time()
        for spell in self._rotation():
            spell_id = int(spell.get("id") or 0)
            if not spell_id:
                continue
            if distance > float(spell.get("range", 30)):
                continue
            if now < self._spell_ready.get(spell_id, 0.0):
                continue
            return spell
        return None

    async def _try_heal(self, threshold: float | None = None) -> bool:
        """Heilzauber, wenn die HP unter der Schwelle liegen. True = gewirkt.

        Ohne `threshold` gilt `heal_hp_pct` (Pause); im Kampf wird
        `combat_heal_hp_pct` (Standard 50) uebergeben.
        """
        me = self.objects.me
        if me is None or not me.max_health:
            return False
        if me.is_dead_player:
            # Ein Toter/Geist kann nicht zaubern - Zauber wuerden still scheitern.
            return False
        limit = (
            float(self.behavior.get("heal_hp_pct", 45))
            if threshold is None
            else threshold
        )
        if me.health_percent >= limit:
            return False
        heal_id = self._heal_spell_id()
        if not heal_id or self.self_guid is None:
            if not self._heal_missing_warned:
                self._heal_missing_warned = True
                log.warning(
                    "[heil] HP %.0f %% - kein Heilszauber verfuegbar/gesetzt "
                    "(behavior.heal_spell_ids / heal_spell_id)",
                    me.health_percent,
                )
            return False
        if time.time() < self._spell_ready.get(heal_id, 0.0):
            return False
        if time.time() < self._cast_until:
            # Gerade laeuft noch ein anderer Zauber (gemeinsame Abklingzeit).
            return False
        log.info(
            "[heil] Heilszauber %d auf mich (HP %d/%d, %.0f %%)",
            heal_id,
            me.health,
            me.max_health,
            me.health_percent,
        )
        await self._cast_self(heal_id, 2000.0)
        return True

    async def _cast_self(self, spell_id: int, cooldown_ms: float = 2000.0) -> None:
        """Zauber auf den eigenen Charakter - nie auf das aktuelle Ziel."""
        if self.self_guid is None:
            return
        await self._cast(spell_id, self.self_guid, cooldown_ms)

    async def _combat_heal(self) -> bool:
        """Heilzauber im Kampf - begrenzt auf `combat_heal_max_casts` pro Kampf.

        Der Kampf geht vor: nur wenn die HP unter `combat_heal_hp_pct` fallen, wird
        geheilt, und das hoechstens so oft wie erlaubt (Standard 2). Danach wird der
        Kampf zu Ende gefuehrt und erst in der Pause wieder geheilt.
        """
        me = self.objects.me
        if me is None or not me.max_health or me.is_dead_player:
            return False
        limit = int(self.behavior.get("combat_heal_max_casts", 2))
        if limit <= 0:
            return False
        if self._combat_heals_used >= limit:
            return False
        if me.health_percent >= self._combat_heal_pct():
            return False
        if not await self._try_heal(self._combat_heal_pct()):
            return False
        self._combat_heals_used += 1
        remaining = limit - self._combat_heals_used
        if remaining > 0:
            log.info(
                "[heil] Kampf-Heilung %d/%d gewirkt (HP %.0f %%) - %d noch moeglich",
                self._combat_heals_used,
                limit,
                me.health_percent,
                remaining,
            )
        else:
            log.info(
                "[heil] Kampf-Heilung %d/%d gewirkt (HP %.0f %%) - kaempfe zu Ende, "
                "heile danach",
                self._combat_heals_used,
                limit,
                me.health_percent,
            )
        return True

    def _combat_heal_pct(self) -> float:
        """HP-Schwelle fuer den Heilszauber im Kampf (Standard 50 %)."""
        return float(self.behavior.get("combat_heal_hp_pct", 50))

    def _heal_candidates(self) -> list[int]:
        """Moegliche Heilszauber: Liste aus der Config plus der einzelne `heal_spell_id`."""
        candidates: list[int] = []
        raw = self.behavior.get("heal_spell_ids")
        if isinstance(raw, list):
            for value in raw:
                try:
                    spell_id = int(value)
                except (TypeError, ValueError):
                    continue
                if spell_id > 0 and spell_id not in candidates:
                    candidates.append(spell_id)
        single = int(self.behavior.get("heal_spell_id") or 0)
        if single > 0 and single not in candidates:
            candidates.append(single)
        return candidates

    async def _probe_heal_spell(self) -> bool:
        """Einmal beim Start die hoechste bekannte Rank des Heilszaubers finden.

        Der Server ignoriert Zauber, die der Charakter nicht kennt, ohne jede
        Antwort. Ein erfolgreicher Cast verbraucht dagegen Kraft (Mana), deshalb
        wird jede Rank einmal probiert und die erste wirksame gemerkt. Das laeuft
        nur beim Start, nie mitten im Kampf.

        Rueckgabe: True = Pruefung abgeschlossen (auch wenn nichts gefunden wurde).
        """
        candidates = [
            spell_id
            for spell_id in self._heal_candidates()
            if spell_id not in self._spell_rejected
        ]
        if not candidates:
            if self._heal_candidates():
                log.warning(
                    "[heil] Server hat alle Heilszauber aus behavior.heal_spell_ids "
                    "abgelehnt - trage die richtige ID in behavior.heal_spell_id ein"
                )
            return True
        me = self.objects.me
        if self.self_guid is None or me is None or not me.max_power:
            # Charakter ist noch nicht vollstaendig in der Welt - naechster Tick.
            return False
        if me.power <= 0:
            # Ohne Kraft laesst sich nicht pruefen, ob ein Zauber wirkt: jeder Cast
            # scheitert dann, und eine Rank waere faelschlich "unbekannt".
            if time.time() >= self._probe_wait_until:
                self._probe_wait_until = time.time() + 15.0
                log.info("[heil] keine Kraft zum Pruefen - teste die Ranks spaeter erneut")
            return False
        if time.time() < self._probe_wait_until:
            return False
        log.info(
            "[heil] pruefe %d moegliche Heilszauber-Ranks einmalig beim Start",
            len(candidates),
        )
        for spell_id in candidates:
            me = self.objects.me
            if me is None or not me.max_power:
                return False
            if me.power <= 0:
                self._probe_wait_until = time.time() + 15.0
                log.info("[heil] Kraft aufgebraucht - teste die restlichen Ranks spaeter")
                return False
            before = me.power
            await self._cast_self(spell_id, 2600.0)
            await asyncio.sleep(2.8)
            me = self.objects.me
            rejected = spell_id in self._spell_rejected
            if me is not None and me.power < before and not rejected:
                self._heal_spell = spell_id
                log.info(
                    "[heil] Heilszauber %d erkannt (%s %d -> %d) - im Kampf ab %.0f %% HP",
                    spell_id,
                    me.power_name,
                    before,
                    me.power,
                    self._combat_heal_pct(),
                )
                return True
            if rejected:
                log.info("[heil] Zauber %d kennt der Charakter nicht (Server-Ablehnung)", spell_id)
            else:
                log.info("[heil] Zauber %d ohne Wirkung - naechste Rank", spell_id)
        # Nichts bestaetigt (z. B. weil der Server keine Ablehnung schickt): die
        # niedrigste Rank kennt jeder Charakter, der den Zauber ueberhaupt hat.
        fallback = candidates[-1]
        self._heal_spell = fallback
        log.warning(
            "[heil] keine Rank bestaetigt - versuche es mit %d (niedrigste Rank); "
            "im Log steht bei jedem Fehlversuch die Ablehnung des Servers",
            fallback,
        )
        return True

    def _heal_spell_id(self) -> int:
        """Der benutzte Heilszauber: geprueft beim Start, sonst der Config-Wert."""
        if self._heal_spell:
            return self._heal_spell
        configured = int(self.behavior.get("heal_spell_id") or 0)
        if configured:
            return configured
        # Noch keine bestaetigte Rank: die niedrigste konfigurierte Rank ist die
        # sicherste Wahl, weil sie jeder Charakter mit diesem Zauber kennt.
        for spell_id in reversed(self._heal_candidates()):
            if spell_id not in self._spell_rejected:
                return spell_id
        return 0

    async def _set_sitting(self, sit: bool, quiet: bool = False) -> None:
        """Hinsetzen (schnellere Regeneration) oder wieder aufstehen."""
        if sit == self._sitting:
            return
        self._sitting = sit
        if not quiet:
            log.info(
                "[rast] %s",
                "setze mich hin - Regeneration läuft schneller"
                if sit
                else "stehe wieder auf",
            )
        try:
            await self.world.send(
                opcodes.CMSG_STANDSTATECHANGE,
                Writer()
                .u32(opcodes.STAND_STATE_SIT if sit else opcodes.STAND_STATE_STAND)
                .build(),
            )
        except WorldError:
            self.running = False

    async def _try_potion(self, me: WoWObject) -> None:
        entry = int(self.behavior.get("potion_entry") or 0)
        if not entry:
            return
        if me.health_percent >= float(self.behavior.get("potion_hp_pct", 35)):
            return
        if time.time() < self._potion_ready_at:
            return
        if await self._use_item(entry, "Heiltrank"):
            self._potion_ready_at = time.time() + float(
                self.behavior.get("potion_cooldown_ms", 60000)
            ) / 1000.0

    async def _try_drink(self, me: WoWObject | None) -> None:
        if me is None:
            return
        drink = int(self.behavior.get("drink_entry") or 0)
        if drink and me.uses_mana and me.power_percent < float(
            self.behavior.get("drink_mana_pct", 35)
        ):
            if await self._use_item(drink, "Trank"):
                await self._regenerate(
                    "Mana",
                    lambda: me.power_percent,
                    float(self.behavior.get("rest_until_mana_pct", 80)),
                )
                return
            # Kein Trank im Rucksack: trotzdem noch das Essen versuchen.
        food = int(self.behavior.get("food_entry") or 0)
        if food and me.health_percent < float(self.behavior.get("food_hp_pct", 50)):
            if await self._use_item(food, "Essen"):
                await self._regenerate("HP", lambda: me.health_percent, float(self.behavior.get("rest_until_hp_pct", 95)))

    async def _regenerate(self, label: str, value, until: float) -> None:
        """Wartet, während Essen/Trinken regeneriert."""
        deadline = time.time() + max(60.0, float(self.behavior.get("max_rest_seconds", 300)))
        last_hp: int | None = None
        while self.running and time.time() < deadline:
            if value() >= until:
                break
            me = self.objects.me
            if me is not None and me.max_health:
                if last_hp is not None and me.health < last_hp - max(1, int(me.max_health * 0.01)):
                    log.info("[rast] werde angegriffen - unterbreche %s", label)
                    return
                last_hp = me.health
            await self._send_move(opcodes.MSG_MOVE_HEARTBEAT, 0)
            await asyncio.sleep(2.0)
        log.info("[rast] %s wieder bei %.0f %%", label, value())

    async def _use_item(self, entry: int, label: str) -> bool:
        if not entry:
            return False
        found = self.objects.find_pack_item(entry)
        if found is None:
            log.info("[item] %s (Item %d) nicht im Rucksack gefunden", label, entry)
            return False
        slot, guid = found
        log.info("[item] benutze %s (Item %d, Rucksack-Slot %d)", label, entry, slot)
        writer = (
            Writer()
            .u8(255)  # PackSlot: Rucksack
            .u8(23 + slot)  # Slot im Rucksack (23 = INVENTORY_SLOT_ITEM_START)
            .u8(0)  # CastID
            .u32(0)  # SpellID (0 = aus dem Item)
            .u64(guid)  # Item-GUID
            .u8(0)  # Misc
            .u8(0)  # Cast-Flags
            .u32(0)  # Ziele: keine
        )
        await self.world.send(opcodes.CMSG_USE_ITEM, writer.build())
        return True

    async def _cast(self, spell_id: int, target_guid: int, cooldown_ms: float = 2500.0) -> None:
        self._spell_ready[spell_id] = time.time() + cooldown_ms / 1000.0
        # Waehrend der Zauber laeuft, nicht wieder hinsetzen (sonst bricht er ab).
        self._cast_until = time.time() + 1.5
        if target_guid == self.self_guid:
            log.info("[zauber] Zauber %d auf mich (GUID %d)", spell_id, target_guid)
        else:
            log.info("[zauber] Zauber %d auf Ziel %d", spell_id, target_guid)
        writer = (
            Writer()
            .u8(0)  # cast_count
            .u32(spell_id)
            .u8(0)  # cast_flags: keine Extradaten
            .u32(TARGET_FLAG_UNIT)
            .packed_guid(target_guid)
        )
        await self.world.send(opcodes.CMSG_CAST_SPELL, writer.build())

    # ------------------------------------------------------------------- Looten
    def _loot_nearby(self) -> None:
        """Sammelt jede Leiche im Umkreis in die Loot-Liste - auch fremde Kills."""
        if not self.behavior.get("loot", True):
            return
        radius = float(self.behavior.get("loot_radius", 25))
        found = 0
        for obj in self.objects.objects.values():
            if not obj.is_mob or obj.alive or not obj.has_position:
                continue
            if obj.guid in self._looted_guids or obj.guid in self._loot_queue:
                continue
            if self._distance_to(obj) > radius:
                continue
            self._loot_queue.append(obj.guid)
            found += 1
        if found:
            log.info(
                "[loot] %d %s im Umkreis aufgesammelt (%d offen)",
                found,
                "Leiche" if found == 1 else "Leichen",
                len(self._loot_queue),
            )

    async def _process_loot_queue(self) -> None:
        """Arbeitet die Loot-Liste ab, bis nichts mehr offen ist."""
        if not self.behavior.get("loot", True):
            self._loot_queue.clear()
            return
        max_distance = float(self.behavior.get("loot_max_distance", 40))
        attempts_allowed = max(1, int(self.behavior.get("loot_attempts", 2)))
        while self._loot_queue and self.running:
            guid = self._loot_queue[0]
            corpse = self.objects.get(guid)
            if corpse is None or not corpse.has_position or corpse.alive:
                self._loot_queue.pop(0)
                self._looted_guids.add(guid)
                continue
            distance = self._distance_to(corpse)
            if distance > max_distance:
                log.info(
                    "[loot] Leiche %d ist %.0f yd entfernt - lasse sie liegen",
                    guid,
                    distance,
                )
                self._loot_queue.pop(0)
                self._looted_guids.add(guid)
                continue
            if self._command_pending():
                log.info("[loot] Kartenbefehl steht an - breche das Looten ab")
                return
            if distance > 5:
                await self._approach(
                    corpse, 3.0, allow_dead=True, abort=self._command_pending
                )
            ok, retry = await self._loot_corpse(corpse)
            if ok:
                self._looted_guids.add(guid)
                self._loot_queue.pop(0)
                continue
            attempt = self._loot_attempts.get(guid, 0) + 1
            self._loot_attempts[guid] = attempt
            if not retry or attempt >= attempts_allowed:
                self._looted_guids.add(guid)
                self._loot_queue.pop(0)
                continue
            log.info(
                "[loot] Leiche %d - neuer Versuch (%d/%d) in 2 s",
                guid,
                attempt + 1,
                attempts_allowed,
            )
            await asyncio.sleep(2.0)

    async def _loot_corpse(self, corpse: WoWObject) -> tuple[bool, bool]:
        """Lootet eine Leiche vollständig. Rueckgabe: (erfolgreich, neuer Versuch sinnvoll)."""
        try:
            await self.world.send(opcodes.CMSG_LOOT, Writer().u64(corpse.guid).build())
            body = await self.world.wait_for(opcodes.SMSG_LOOT_RESPONSE, timeout=4.0)
        except asyncio.TimeoutError:
            log.info(
                "[loot] Leiche %d: keine Antwort vom Server (zu weit weg?)", corpse.guid
            )
            return False, True

        reader = Reader(body)
        try:
            loot_guid = reader.u64()
            method = reader.u8()
        except ProtocolError:
            log.warning("[loot] Loot-Antwort des Servers war unlesbar - übersprungen")
            return False, False
        if method == 0:
            error = reader.u8() if reader.remaining else 0
            # 1 = fremder Kill, 5 = nichts drin -> nicht weiter versuchen
            if error in (1, 5):
                log.info(
                    "[loot] Leiche %d leer oder nicht von uns (Code %d) - übersprungen",
                    corpse.guid,
                    error,
                )
                return False, False
            log.info("[loot] Server verweigert Loot (Fehlercode %d)", error)
            return False, True

        try:
            gold = reader.u32()
        except ProtocolError:
            log.warning("[loot] Loot-Antwort ohne Geldbetrag - übersprungen")
            return False, False
        slots = []
        try:
            count = reader.u8()
            for _ in range(count):
                slot = reader.u8()
                item_entry = reader.u32()
                item_count = reader.u32()
                reader.u32()  # display id
                reader.u32()  # random suffix
                reader.i32()  # random property
                reader.u8()  # slot type
                slots.append((slot, item_entry, item_count))
        except ProtocolError:
            # Anderer Kern/anderes Spielfeld-Layout: die schon gelesenen Slots
            # trotzdem einpacken, statt die ganze Sitzung abzubrechen.
            log.warning(
                "[loot] Leiche %d: Loot-Paket unvollständig - nehme %d gelesene Slots",
                corpse.guid,
                len(slots),
            )

        # Beuteregeln aus dem Reiter „Ziele & Beute“: nur passende Slots einpacken.
        wanted_slots = []
        skipped = 0
        for slot, item_entry, item_count in slots:
            name = self.item_names.get(item_entry)
            if name is None and self._loot_rules.needs_names:
                # Namensfilter aktiv - Namen erst vom Server holen, sonst rät der Bot.
                name = await self._fetch_item_name(item_entry)
            if not self._loot_rules.wants(item_entry, name):
                skipped += 1
                log.info(
                    "[loot] %s nicht aufgehoben (Filter)",
                    self._item_label(item_entry, name),
                )
                continue
            wanted_slots.append((slot, item_entry, item_count))

        if gold:
            await self.world.send(opcodes.CMSG_LOOT_MONEY)
            self.copper += gold
            log.info("[loot] %d Kupfer (gesamt %d)", gold, self.copper)
        for slot, item_entry, item_count in wanted_slots:
            log.info(
                "[loot] %s x%d (Slot %d)",
                self._item_label(item_entry, self.item_names.get(item_entry)),
                item_count,
                slot,
            )
            await self.world.send(
                opcodes.CMSG_AUTOSTORE_LOOT_ITEM, Writer().u8(slot).build()
            )
            await asyncio.sleep(self._jitter(0.25))
        if wanted_slots:
            self.looted += len(wanted_slots)
        # Namen unbekannter Gegenstände nachtragen (für Log und Fenster).
        self._request_item_names([entry for _, entry, _ in slots])

        await self.world.send(opcodes.CMSG_LOOT_RELEASE, Writer().u64(loot_guid).build())
        extra = f", {skipped} nach Filter liegen gelassen" if skipped else ""
        log.info(
            "[loot] Leiche %d geschlossen (%d %s%s, %d offen)",
            loot_guid,
            len(wanted_slots),
            "Gegenstand" if len(wanted_slots) == 1 else "Gegenstände",
            extra,
            max(0, len(self._loot_queue) - 1),
        )
        return True, False

    # ---------------------------------------------------------------- Bewegung
    async def _roam(self) -> None:
        hotspots = self.behavior.get("hotspots") or []
        if hotspots:
            spot = hotspots[self.hotspot_index % len(hotspots)]
            self.hotspot_index += 1
            log.info(
                "Kein Ziel in Reichweite - laufe zu Waypoint %d (%.1f, %.1f)",
                self.hotspot_index,
                float(spot["x"]),
                float(spot["y"]),
            )
            await self._move_to(
                float(spot["x"]),
                float(spot["y"]),
                float(spot.get("z", self.position[2])),
                abort=self._command_pending,
            )
        else:
            # Ohne Waypoints im Umkreis herumstreifen, damit Mobs in Sichtweite kommen.
            angle = random.uniform(0, 2 * math.pi)
            radius = random.uniform(10, 40)
            units, alive, with_position, in_range, nearest = self._scan_stats
            nearest_text = (
                f", nächster: {self._nearest_label(nearest)} in {nearest[0]:.0f} yd"
                if nearest
                else ""
            )
            log.info(
                "[suche] kein Ziel in Sicht (Mobs: %d, lebend: %d, mit Position: %d, "
                "in %.0f yd: %d%s) - streife %.0f yd nach %s",
                units,
                alive,
                with_position,
                float(self.behavior.get("max_target_distance", 40)),
                in_range,
                nearest_text,
                radius,
                _compass(angle),
            )
            self.log_nearby("suche")
            await self._move_to(
                self.position[0] + math.cos(angle) * radius,
                self.position[1] + math.sin(angle) * radius,
                self.position[2],
                abort=self._command_pending,
            )
        await asyncio.sleep(self._jitter(0.5))

    async def _approach(
        self,
        target: WoWObject,
        stop_distance: float,
        allow_dead: bool = False,
        abort: "callable | None" = None,
    ) -> bool:
        if not allow_dead and not target.alive:
            return False
        if not target.has_position:
            return False
        log.info(
            "[nah] laufe zu %s (%.1f yd, soll %.1f yd)",
            self._short_name(target),
            self._distance_to(target),
            stop_distance,
        )
        return await self._move_to(
            target.x, target.y, target.z, stop_distance, abort=abort
        )

    async def _detour_around(
        self, target: WoWObject, abort: "callable | None" = None
    ) -> bool:
        """Läuft seitlich um das Ziel herum, um ein Hindernis zu umlaufen."""
        if not target.has_position or not target.alive:
            return False
        cur_x, cur_y, _, _ = self.position
        to_target = math.atan2(target.y - cur_y, target.x - cur_x)
        self._detour_side *= -1
        # Punkt auf einem Kreis um das Ziel: gleicher Abstand, nur anderer Winkel.
        radius = max(self._distance_to(target), float(self.behavior.get("detour_distance", 8)))
        angle = to_target + self._detour_side * math.radians(70)
        dest_x = target.x - math.cos(angle) * radius
        dest_y = target.y - math.sin(angle) * radius
        distance = math.hypot(dest_x - cur_x, dest_y - cur_y)
        log.info(
            "[umweg] Hindernis erkannt - laufe %.0f yd %s um %s herum (%d/%d)",
            distance,
            "links" if self._detour_side > 0 else "rechts",
            self._short_name(target),
            self._detour_attempts,
            int(self.behavior.get("detour_attempts", 3)),
        )
        await self._move_to(
            dest_x,
            dest_y,
            target.z,
            stop_distance=1.5,
            max_seconds=20.0,
            abort=abort,
        )
        await self._approach(
            target, float(self.behavior.get("melee_range", 4.5)) * 0.8, abort=abort
        )
        return True

    def _plan_path(self, x: float, y: float) -> list[tuple[float, float]]:
        """Route zum Ziel: gerade, wenn nichts bekannt ist, sonst um Hindernisse."""
        path = self.wayfinder.plan(self.position[0], self.position[1], x, y)
        if len(path) > 1:
            log.info(
                "[weg] Route mit %d Wegpunkten um %d bekannte Hindernisse herum",
                len(path),
                self.wayfinder.count(),
            )
        return path

    async def _move_to(
        self,
        x: float,
        y: float,
        z: float,
        stop_distance: float = 1.0,
        max_seconds: float = 45.0,
        abort: "callable | None" = None,
    ) -> bool:
        """Läuft zu (x, y). `abort` wird in jeder Runde gefragt (z. B. neuer Befehl)."""
        arrived = False
        self._blocked_here = False
        deadline = time.time() + max_seconds
        max_replans = int(self.behavior.get("path_replans", 4))
        replans = 0
        sidestep_done = False
        path = self._plan_path(x, y)
        index = 0
        await self._send_move(opcodes.MSG_MOVE_START_FORWARD, MOVEFLAG_FORWARD)
        try:
            while time.time() < deadline:
                if abort is not None and abort():
                    log.info("[befehl] neuer Auftrag - breche den alten Weg ab")
                    break
                cur_x, cur_y, cur_z, _ = self.position
                # Meldet der Server eine andere Position, sind wir gegen etwas gelaufen.
                me = self.objects.me
                if me is not None and me.has_position:
                    drift = math.hypot(me.x - cur_x, me.y - cur_y)
                    if drift > 5.0:
                        log.info(
                            "[weg] Server setzt mich um %.0f yd zurück (Hindernis) - "
                            "merke mir die Stelle und plane neu",
                            drift,
                        )
                        # Nicht nur ein Punkt: die ganze Strecke, die der Server
                        # abgelehnt hat, ist für uns zu.
                        self.wayfinder.block_segment((me.x, me.y), (cur_x, cur_y))
                        self.position = (me.x, me.y, me.z, me.o)
                        replans += 1
                        if replans > max_replans:
                            if not sidestep_done and not self._in_sidestep:
                                # Klassischer Ausweg (Bug-Algorithmus): seitlich
                                # versetzen, dann neu anlaufen.
                                sidestep_done = True
                                await self._try_sidestep(x, y, z)
                                path = self._plan_path(x, y)
                                index = 0
                                continue
                            self._blocked_here = True
                            break
                        path = self._plan_path(x, y)
                        index = 0
                        continue
                # Wegpunkte, die schon erreicht sind, überspringen.
                while index < len(path) - 1 and math.hypot(
                    path[index][0] - cur_x, path[index][1] - cur_y
                ) <= max(stop_distance, 2.0):
                    index += 1
                way_x, way_y = path[index]
                dx = way_x - cur_x
                dy = way_y - cur_y
                distance = math.hypot(x - cur_x, y - cur_y)
                if time.time() - self._last_progress >= 5.0:
                    self._last_progress = time.time()
                    log.info(
                        "[lauf] noch %.1f yd bis (%.1f, %.1f) | Pos %.1f/%.1f",
                        distance,
                        x,
                        y,
                        cur_x,
                        cur_y,
                    )
                if distance <= stop_distance:
                    log.info("[lauf] Ziel erreicht (%.1f yd)", distance)
                    arrived = True
                    break
                step = min(RUN_SPEED * 0.2, max(math.hypot(dx, dy), 0.0))
                if step <= 0.0:
                    index += 1
                    continue
                heading = math.atan2(dy, dx)
                new_x = cur_x + math.cos(heading) * step
                new_y = cur_y + math.sin(heading) * step
                # Höhe grob mitziehen, echte Geländehöhen kennt der Client hier nicht.
                new_z = cur_z + (z - cur_z) * min(1.0, step / max(distance, 0.001))
                self.position = (new_x, new_y, new_z, heading)
                self._sync_self_object()
                await self._send_move(opcodes.MSG_MOVE_HEARTBEAT, MOVEFLAG_FORWARD)
                await asyncio.sleep(self._jitter(0.2))
        except WorldError:
            self.running = False
        finally:
            try:
                await self._send_move(opcodes.MSG_MOVE_STOP, 0)
            except WorldError:
                self.running = False
        return arrived

    async def _try_sidestep(self, goal_x: float, goal_y: float, z: float) -> None:
        """Seitwärts um ein Hindernis herum, wenn keine Route mehr gefunden wird.

        Wie beim Bug-Algorithmus: eine Strecke quer zur Zielrichtung laufen,
        dann neu planen. Die Seite mit der freieren Umgebung wird bevorzugt.
        """
        cur_x, cur_y, _, _ = self.position
        heading = math.atan2(goal_y - cur_y, goal_x - cur_x)
        distance = self.wayfinder.cell_size * 3.0

        def known_obstacles(angle: float) -> int:
            """Wie viele bekannte Hindernisse liegen auf den nächsten Zellen?"""
            found = 0
            for step in range(1, 7):
                px = cur_x + math.cos(angle) * self.wayfinder.cell_size * step
                py = cur_y + math.sin(angle) * self.wayfinder.cell_size * step
                if self.wayfinder.blocked(self.wayfinder.cell(px, py)):
                    found += 1
            return found

        # Die Seite nehmen, auf der weniger bekanntes Hindernis liegt.
        candidates = []
        for side, label in ((1, "links"), (-1, "rechts")):
            angle = heading + side * math.pi / 2.0
            candidates.append((known_obstacles(angle), side, label, angle))
        candidates.sort(key=lambda item: item[0])
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            # Gleichstand: beim nächsten Mal die andere Seite probieren.
            chosen = next(item for item in candidates if item[1] == self._sidestep_side)
        else:
            chosen = candidates[0]
        self._sidestep_side = -chosen[1]
        _, side, label, angle = chosen
        target = (
            cur_x + math.cos(angle) * distance,
            cur_y + math.sin(angle) * distance,
            label,
        )
        log.info(
            "[weg] Weg versperrt - laufe %.0f yd %s zur Seite und versuche es erneut",
            distance,
            target[2],
        )
        self._in_sidestep = True
        try:
            await self._move_to(target[0], target[1], z, stop_distance=2.0, max_seconds=10.0)
        finally:
            self._in_sidestep = False

    async def _face(self, target: WoWObject) -> None:
        cur_x, cur_y, cur_z, _ = self.position
        heading = math.atan2(target.y - cur_y, target.x - cur_x)
        self.position = (cur_x, cur_y, cur_z, heading)
        self._sync_self_object()
        try:
            await self._send_move(opcodes.MSG_MOVE_SET_FACING, 0)
        except WorldError:
            self.running = False

    def _sync_self_object(self) -> None:
        me = self.objects.me
        if me is not None:
            me.x, me.y, me.z, me.o = self.position
            me.has_position = True

    def _on_move_teleport_ack(self, body: bytes) -> None:
        """Server versetzt uns (Tod -> Friedhof) per MSG_MOVE_TELEPORT_ACK (0x0C7).

        Aufbau (MoveTeleport::Write, 3.3.5): PackedGuid, u32 Sequenz, MovementInfo.
        Bis zur Bestaetigung verwirft der Server jede Bewegung.
        """
        self._handle_teleport(body, with_sequence=True)

    def _on_move_teleport_broadcast(self, body: bytes) -> None:
        """Aeltere Kerne schicken den Teleport an den Spieler als 0x0C5.

        Aufbau (MoveUpdateTeleport::Write): PackedGuid, MovementInfo - ohne Sequenz.
        """
        self._handle_teleport(body, with_sequence=False)

    @staticmethod
    def _valid_position(x: float, y: float, z: float, o: float) -> bool:
        return (
            math.isfinite(x)
            and math.isfinite(y)
            and math.isfinite(z)
            and math.isfinite(o)
            and -17066.0 < x < 17066.0
            and -17066.0 < y < 17066.0
            and -2500.0 < z < 6000.0
        )

    def _parse_teleport(self, body: bytes, with_sequence: bool):
        """Liest ein Teleport-Paket; None, wenn Aufbau/Position nicht passen."""
        try:
            reader = Reader(body)
            guid = reader.packed_guid()
            sequence = reader.u32() if with_sequence else 0
            info = read_movement_info(reader)
        except (ProtocolError, struct.error):
            return None
        if not self._valid_position(info.x, info.y, info.z, info.o):
            return None
        return guid, sequence, info

    def _handle_teleport(self, body: bytes, with_sequence: bool) -> None:
        """Teleport uebernehmen und bestaetigen - egal welchen der beiden Aufbauten
        der Server benutzt (0x0C7 mit Sequenz, 0x0C5 ohne)."""
        parsed = self._parse_teleport(body, with_sequence=with_sequence)
        if parsed is None:
            parsed = self._parse_teleport(body, with_sequence=not with_sequence)
        if parsed is None:
            log.warning(
                "[teleport] Paket nicht lesbar (0x0C%d) - bestaetige trotzdem",
                7 if with_sequence else 5,
            )
            # Trotzdem bestaetigen, sonst bleibt der Server im Teleport-Zustand.
            self._spawn(self._ack_teleport(0))
            return

        guid, sequence, info = parsed
        if self.self_guid is not None and guid and guid != self.self_guid:
            return

        self.position = (info.x, info.y, info.z, info.o)
        self._sync_self_object()
        log.info(
            "[teleport] Server versetzt mich nach %.1f/%.1f/%.1f (Sequenz %d) - bestätige den Teleport",
            info.x,
            info.y,
            info.z,
            sequence,
        )
        self._spawn(self._ack_teleport(sequence))

    async def _ack_teleport(self, sequence: int) -> None:
        """MSG_MOVE_TELEPORT_ACK: erst danach nimmt der Server Bewegung wieder an."""
        if self.self_guid is None:
            return
        try:
            await self.world.send(
                opcodes.MSG_MOVE_TELEPORT_ACK,
                Writer()
                .packed_guid(self.self_guid)
                .u32(sequence)
                .u32(self.move_time_ms)
                .build(),
            )
        except WorldError:
            self.running = False

    async def _send_move(self, opcode: int, flags: int) -> None:
        if self.self_guid is None:
            return
        self.move_time_ms = (self.move_time_ms + 100) & 0xFFFFFFFF
        info = MovementInfo(
            flags=flags,
            flags2=0,
            time=self.move_time_ms,
            x=self.position[0],
            y=self.position[1],
            z=self.position[2],
            o=self.position[3],
        )
        writer = Writer().packed_guid(self.self_guid)
        write_movement_info(writer, info)
        await self.world.send(opcode, writer.build())

    async def _rest(self) -> bool:
        """Pause ausserhalb des Kampfes: heilt, bis HP (und Mana) wieder reichen.

        Rückgabe: True = Pause beendet, False = wurde unterbrochen (Gegner).
        """
        me = self.objects.me
        until_hp = float(self.behavior.get("rest_until_hp_pct", 95))
        until_mana = float(self.behavior.get("rest_until_mana_pct", 80))
        # Nur Manaklassen muessen auf ihre Ressource warten. Wut/Energie/Runenmacht
        # fuellen sich nicht durch Sitzen und wuerden die Pause nur verlaengern.
        wait_mana = me is not None and me.uses_mana
        start_percent = me.health_percent if me is not None and me.max_health else 0.0
        mana_goal = (
            f" / {until_mana:.0f} % {me.power_name}" if wait_mana and me is not None else ""
        )
        log.info(
            "[rast] HP bei %.0f %% - Pause, heile bis %.0f %% HP%s (%s%s)",
            start_percent,
            until_hp,
            mana_goal,
            f"{me.health}/{me.max_health}" if me else "?",
            f", {me.power}/{me.max_power} {me.power_name}" if wait_mana and me is not None else "",
        )
        if not await self._use_item(int(self.behavior.get("food_entry") or 0), "Essen"):
            await self._use_item(int(self.behavior.get("drink_entry") or 0), "Trank")
        # Sitzen beschleunigt die Regeneration auf dem Server deutlich.
        await self._set_sitting(True)

        # Die Pause endet erst bei erreichtem Ziel; max_rest_seconds ist nur eine
        # Notbremse (z. B. wenn der Server nicht regeneriert).
        started = time.time()
        end = started + max(60.0, float(self.behavior.get("max_rest_seconds", 300)))
        report_every = float(self.behavior.get("rest_report_seconds", 10))
        last_report = started
        last_hp: int | None = None
        last_gain = started
        last_gain_hp = me.health if me is not None else 0
        stalled = False
        attacked = False
        order_pending = False
        while self.running and time.time() < end:
            me = self.objects.me
            if me is None or not me.max_health:
                break
            if me.is_dead_player:
                # Tot/Geist: Pause abbrechen, der Todesablauf uebernimmt.
                break
            if self._goto_pending():
                # Klick auf die Karte hat Vorrang: Pause beenden und loslaufen.
                log.info("[rast] Kartenklick - beende die Pause und laufe los")
                order_pending = True
                break
            hp_drop = last_hp is not None and me.health < last_hp - max(
                1, int(me.max_health * 0.01)
            )
            # Nur Schaden, der WAEHREND der Pause ankommt, bricht sie ab. Ein Mob,
            # der nur in der Naehe steht, tut nichts - frueher wurde die Pause
            # dadurch staendig abgebrochen und der Charakter heilte nie hoch.
            in_combat = self._last_damage_at >= started
            if hp_drop or in_combat:
                log.info(
                    "[rast] werde angegriffen (%d HP%s) - Pause abgebrochen, kämpfe weiter",
                    me.health,
                    f", vorher {last_hp}" if hp_drop and last_hp is not None else "",
                )
                attacked = True
                break
            last_hp = me.health
            if me.health > last_gain_hp:
                last_gain_hp = me.health
                last_gain = time.time()
                stalled = False
            hp_ok = me.health_percent >= until_hp
            mana_ok = not wait_mana or me.power_percent >= until_mana
            if hp_ok and mana_ok:
                break
            if not stalled and not hp_ok and time.time() - last_gain >= 20.0:
                # Der Server regeneriert nicht (oder der Zauber wirkt nicht):
                # einmal deutlich melden und das Sitzen erneut senden.
                stalled = True
                heal_id = self._heal_spell_id()
                log.warning(
                    "[rast] HP steigen seit 20 s nicht (%.0f %%) - %s",
                    me.health_percent,
                    f"Heilszauber {heal_id} wirkt nicht"
                    if heal_id
                    else "kein Heilszauber erkannt (behavior.heal_spell_id)",
                )
                await self._set_sitting(False, quiet=True)
                await self._set_sitting(True, quiet=True)
            # Auch in der Pause: Heiltrank und Heilzauber nutzen, wenn vorhanden.
            if await self._try_heal():
                # Ein Zauber unterbricht das Sitzen - danach still wieder hinsetzen.
                self._sitting = False
            await self._try_potion(me)
            if time.time() >= self._cast_until:
                # Erst wenn der Zauber durch ist, wieder hinsetzen.
                await self._set_sitting(True, quiet=True)
            if report_every > 0 and time.time() - last_report >= report_every:
                last_report = time.time()
                log.info(
                    "[rast] %.0f %% HP (%d/%d)%s | seit %.0f s Pause - heile bis "
                    "%.0f %% HP%s",
                    me.health_percent,
                    me.health,
                    me.max_health,
                    f", %.0f %% {me.power_name} (%d/%d)"
                    % (me.power_percent, me.power, me.max_power)
                    if wait_mana
                    else "",
                    time.time() - started,
                    until_hp,
                    mana_goal,
                )
            await self._send_move(opcodes.MSG_MOVE_HEARTBEAT, 0)
            await asyncio.sleep(2.0)
        await self._set_sitting(False)
        if order_pending:
            return True  # Kartenbefehl übernimmt im nächsten Tick
        me = self.objects.me
        if me is None or attacked or (me.max_health and me.is_dead_player):
            return False
        if me.health_percent < until_hp:
            log.warning(
                "[rast] Notbremse nach %.0f s: nur %.0f %% HP - ich kämpfe trotzdem weiter",
                time.time() - started,
                me.health_percent,
            )
        else:
            log.info(
                "[rast] Pause beendet (%.0f %% HP%s) - greife wieder an",
                me.health_percent,
                f", %.0f %% {me.power_name}" % me.power_percent if wait_mana else "",
            )
        return True

    def _find_spirit_healer(self) -> WoWObject | None:
        """Sucht den Geistheiler am Friedhof (Entry 6491 oder über den Namen)."""
        entries = {
            int(entry) for entry in (self.behavior.get("spirit_healer_entries") or [6491])
        }
        best: tuple[float, WoWObject] | None = None
        for obj in self.objects.objects.values():
            if not obj.is_mob or not obj.has_position or not obj.alive:
                continue
            name = self.creature_names.get(obj.entry, "").lower()
            if (
                obj.entry not in entries
                and "geistheiler" not in name
                and "spirit healer" not in name
            ):
                continue
            distance = self._distance_to(obj)
            if best is None or distance < best[0]:
                best = (distance, obj)
        return best[1] if best is not None else None

    # ------------------------------------------------------- Tod und Leichnam
    def _own_corpse(
        self,
        reference: tuple[float, float, float] | None = None,
        max_distance: float | None = None,
    ) -> WoWObject | None:
        """Der eigene Leichnam (Typ CORPSE).

        Der Server schickt im Leichnam den Besitzer mit (CORPSE_FIELD_OWNER) -
        damit ist der eigene Leichnam eindeutig, auch wenn daneben ein anderer
        Spieler gestorben ist. Ohne Besitzerfeld (aeltere Kerne) zaehlt die
        Naehe zur Todesstelle bzw. zu uns.
        """
        best: tuple[float, WoWObject] | None = None
        for obj in self.objects.objects.values():
            if obj.type_id != opcodes.TYPEID_CORPSE or not obj.has_position:
                continue
            if reference is not None:
                distance = math.hypot(obj.x - reference[0], obj.y - reference[1])
            else:
                distance = self._distance_to(obj)
            if max_distance is not None and distance > max_distance:
                continue
            if obj.owner_guid and self.self_guid:
                if obj.owner_guid != self.self_guid:
                    continue
                # Besitzer passt: eindeutig unserer (der Abstand ist bereits geprueft).
                return obj
            if best is None or distance < best[0]:
                best = (distance, obj)
        return best[1] if best is not None else None

    def _revived(self) -> bool:
        """Erkennt die Wiederbelebung ohne eigenes Bestaetigungspaket.

        Der Server bestaetigt `CMSG_RECLAIM_CORPSE` nicht separat - der neue
        Zustand kommt als Update. Wichtig: Ein Geist hat auf 3.3.5 **HP 1**
        (`Player::BuildPlayerRepop`), nicht 0. `HP > 0` als Lebendigkeits-Test
        haette den Geist sofort als wiederbelebt gewertet. Deshalb gilt nur ein
        echter HP-Wert ueber 1 als lebendig, und das Geist-Flag blockiert immer.
        """
        me = self.objects.me
        if me is None or not me.max_health:
            return False
        if me.is_ghost:
            self._saw_ghost = True
            self._ghost_cleared_at = 0.0
            return False
        if me.health <= 0:
            return False
        if me.health > 1:
            self._saw_ghost = False
            self._ghost_cleared_at = 0.0
            return True
        # 1 HP: entweder wirklich mit 1 HP aus einem Kampf gekommen (lebendig)
        # oder das Geist-Minimum, dessen HP-Update noch fehlt. Letzteres nur,
        # wenn wir den Geist in dieser Sitzung gesehen haben.
        if not self._saw_ghost:
            return True
        now = time.time()
        if not self._ghost_cleared_at:
            self._ghost_cleared_at = now
            log.info("[tod] Wiederbelebung erkannt - warte auf das HP-Update")
            return False
        if now - self._ghost_cleared_at < 10.0:
            return False
        log.warning(
            "[tod] Geist-Flag weg, aber kein HP-Update (HP %d/%d) - mache weiter",
            me.health,
            me.max_health,
        )
        self._saw_ghost = False
        self._ghost_cleared_at = 0.0
        return True

    def _drop_corpse(self) -> None:
        """Leichnam lokal vergessen (beim Wiederbeleben bleibt nur Gebeine zurueck)."""
        if self._corpse_guid is not None:
            self.objects.objects.pop(self._corpse_guid, None)
            self._corpse_guid = None

    def _save_death_position(self) -> None:
        """Merkt sich die Position VOR dem Geistfreilassen.

        Jeder Tod ueberschreibt den gespeicherten Punkt - von dort laeuft der
        Geist spaeter zurueck und belebt sich wieder.
        """
        # Der Leichnam des vorherigen Todes existiert nicht mehr - sonst wuerde
        # er beim naechsten Tod als "eigener" erkannt.
        self._drop_corpse()
        self._death_position = (self.position[0], self.position[1], self.position[2])
        self._death_map = self.map_id
        self._graveyard_pos = None
        self._graveyard_walked = False
        self._reclaim_delay_until = 0.0
        self._return_attempts = 0
        log.error(
            "[tod] letzte Position gespeichert: %.1f/%.1f/%.1f (Map %d)",
            self._death_position[0],
            self._death_position[1],
            self._death_position[2],
            self._death_map,
        )

    def _refresh_corpse_guid(self) -> int | None:
        """Sucht den eigenen Leichnam neu.

        Der Server schickt den Leichnam eines entfernten Toten erst, wenn man
        wieder in Sichtweite kommt. Deshalb wird die GUID waehrend des
        Rueckwegs mehrfach neu gesucht - nicht nur einmal am Friedhof.
        """
        corpse = self._own_corpse(self._death_position, max_distance=60.0)
        if corpse is not None:
            if self._corpse_guid != corpse.guid:
                log.info(
                    "[tod] eigener Leichnam in Sicht (GUID %d, %.0f yd)",
                    corpse.guid,
                    self._distance_to(corpse),
                )
            self._corpse_guid = corpse.guid
            return corpse.guid
        if self._corpse_guid is not None and self.objects.get(self._corpse_guid) is None:
            self._corpse_guid = None
        return self._corpse_guid

    def _corpse_target(self) -> tuple[float, float, float] | None:
        """Ziel zum Wiederbeleben: die gemerkte letzte Position."""
        if self._death_position is not None:
            return self._death_position
        corpse = self._own_corpse()
        if corpse is not None:
            self._corpse_guid = corpse.guid
            return (corpse.x, corpse.y, corpse.z)
        return None

    def _reclaim_walk_target(self) -> tuple[float, float, float]:
        """Ziel fuer den Geist: bevorzugt der echte Leichnam, sonst die Todesstelle.

        Der Server prueft die 39 Yard gegen den Leichnam - liegt der anders als
        die gemerkte Position (z. B. nach Rueckstoss beim Tod), laeuft der Bot
        lieber zum Korpsobjekt.
        """
        if self._corpse_guid is not None:
            corpse = self.objects.get(self._corpse_guid)
            if corpse is not None and corpse.has_position:
                return (corpse.x, corpse.y, corpse.z)
        if self._death_position is not None:
            return self._death_position
        return (self.position[0], self.position[1], self.position[2])

    def _corpse_distance(self) -> float:
        x, y, _ = self._reclaim_walk_target()
        return math.hypot(x - self.position[0], y - self.position[1])

    async def _wait_for_resurrection(self) -> None:
        """Stirbt der Charakter: Geist freilassen, zum Leichnam laufen, dort wiederbeleben."""
        if self._dead_since == 0.0:
            self._dead_since = time.time()
            if self._death_position is None:
                # Sicherheitsnetz (z. B. Tod waehrend eines anderen Ablaufs).
                self._save_death_position()
            log.error(
                "[tod] Charakter ist gestorben (Pos %.1f/%.1f) - warte bis zu %.0f min auf Wiederbelebung",
                self.position[0],
                self.position[1],
                float(self.behavior.get("death_wait_minutes", 5)),
            )

        auto_release = bool(self.behavior.get("auto_release", True))
        release_after = float(self.behavior.get("release_delay_seconds", 10))
        run_back = bool(self.behavior.get("corpse_run_back", True))
        wait_cap = float(self.behavior.get("death_wait_minutes", 5)) * 60
        if run_back:
            # Leichnam suchen + zum Friedhof zuruecklaufen braucht Zeit; sonst
            # gibt der Bot auf, waehrend er noch unterwegs ist.
            wait_cap = max(wait_cap, 600.0)
        deadline = self._dead_since + wait_cap
        max_healer_tries = max(1, int(self.behavior.get("spirit_healer_attempts", 3)))
        released = False
        corpse_handled = False
        healer_tries = 0
        last_healer_try = 0.0

        while self.running and time.time() < deadline:
            me = self.objects.me
            if self._revived():
                log.info("[tod] wiederbelebt - mache weiter")
                self._dead_since = 0.0
                return
            if me is not None and me.has_position:
                # Nach dem Freilassen steht der Geist am Friedhof.
                self.position = (me.x, me.y, me.z, me.o)
            self._request_names()

            now = time.time()
            if auto_release and not released and now - self._dead_since >= release_after:
                saved = self._death_position
                log.info(
                    "[tod] Geist freilassen (letzte Position %.1f/%.1f gemerkt)",
                    saved[0] if saved else 0.0,
                    saved[1] if saved else 0.0,
                )
                try:
                    await self.world.send(opcodes.CMSG_REPOP_REQUEST, b"\x00")
                except WorldError:
                    self.running = False
                    return
                released = True
                await asyncio.sleep(1.5)
                continue

            if released and not corpse_handled:
                corpse_handled = True
                if run_back:
                    if await self._reclaim_at_corpse():
                        self._dead_since = 0.0
                        return
                    log.warning(
                        "[tod] Wiederbeleben am Leichnam nicht möglich - gehe zum Geistheiler"
                    )
                continue

            if released:
                healer = self._find_spirit_healer()
                if healer is None and self._graveyard_pos is not None and not self._graveyard_walked:
                    # Nach einem gescheiterten Leichnam-Versuch steht der Geist oft
                    # weit weg vom Friedhof - der Heiler ist dann noch nicht in Sicht.
                    gy = self._graveyard_pos
                    distance = math.hypot(
                        gy[0] - self.position[0], gy[1] - self.position[1]
                    )
                    if distance > 10:
                        self._graveyard_walked = True
                        log.info(
                            "[tod] kein Geistheiler in Sicht - laufe %.0f yd zum Friedhof",
                            distance,
                        )
                        await self._move_to(
                            gy[0],
                            gy[1],
                            gy[2],
                            stop_distance=8.0,
                            max_seconds=min(600.0, distance / 3.0 + 30.0),
                        )
                        continue
                if healer is None:
                    log.info("[tod] warte auf den Geistheiler am Friedhof")
                elif self._distance_to(healer) > 4.0:
                    await self._approach(healer, 3.0)
                elif now - last_healer_try >= 5.0 and healer_tries < max_healer_tries:
                    healer_tries += 1
                    last_healer_try = now
                    log.info(
                        "[tod] Geistheiler %s aktivieren (Versuch %d/%d)",
                        self._short_name(healer),
                        healer_tries,
                        max_healer_tries,
                    )
                    try:
                        await self.world.send(
                            opcodes.CMSG_SPIRIT_HEALER_ACTIVATE,
                            Writer().u64(healer.guid).build(),
                        )
                    except WorldError:
                        self.running = False
                        return
            await asyncio.sleep(2.0)

        log.error("[tod] keine Wiederbelebung möglich - Bot stoppt")
        self.running = False

    async def _reclaim_at_corpse(self) -> bool:
        """Als Geist zur gemerkten letzten Position laufen und dort wiederbeleben.

        Rückgabe: True = wiederbelebt, False = aufgegeben (Geistheiler übernimmt).
        """
        saved = self._death_position
        if saved is None:
            log.info("[tod] keine gespeicherte Position - nutze den Geistheiler")
            return False
        if self._death_map and self.map_id and self.map_id != self._death_map:
            log.info(
                "[tod] andere Karte (Map %d statt %d) - Leichnam nicht erreichbar",
                self.map_id,
                self._death_map,
            )
            return False

        x, y, z = saved
        budget = max(30.0, float(self.behavior.get("corpse_reclaim_seconds", 240)))
        end_all = time.time() + budget
        attempts = max(1, int(self.behavior.get("corpse_attempts", 2)))
        stop = float(self.behavior.get("corpse_reclaim_range", 8))
        server_range = float(self.behavior.get("corpse_server_range", 39))

        log.info(
            "[tod] Todesstelle %.1f/%.1f/%.1f - laufe als Geist dorthin zurück",
            x,
            y,
            z,
        )

        for attempt in range(1, attempts + 1):
            if not self.running or time.time() >= end_all:
                return False
            self._refresh_corpse_guid()
            x, y, z = self._reclaim_walk_target()
            distance = self._corpse_distance()
            if distance > stop:
                log.info(
                    "[tod] laufe als Geist %.0f yd zur letzten Position (Versuch %d/%d)",
                    distance,
                    attempt,
                    attempts,
                )
                await self._move_to(
                    x,
                    y,
                    z,
                    stop_distance=stop,
                    max_seconds=min(budget, distance / 2.0 + 30.0),
                )
                # Erst jetzt schickt der Server den Leichnam (Sichtweite).
                self._refresh_corpse_guid()
                x, y, z = self._reclaim_walk_target()
                distance = self._corpse_distance()

            if distance > server_range:
                log.info(
                    "[tod] noch %.0f yd von der Todesstelle entfernt (Server erlaubt %.0f yd)",
                    distance,
                    server_range,
                )
                continue

            if self._corpse_guid is None:
                log.info(
                    "[tod] Server hat kein Korpsobjekt geschickt - versuche Wiederbelebung trotzdem"
                )
            # Pro Anlauf bis zu 90 s: manche Kerne verlangen eine Wartezeit
            # (SMSG_CORPSE_RECLAIM_DELAY), die laenger als 45 s sein kann.
            if await self._try_reclaim_corpse(
                min(90.0, max(20.0, end_all - time.time()))
            ):
                log.info(
                    "[tod] an der letzten Position wiederbelebt (%.0f yd) - weiter geht's",
                    distance,
                )
                # Wir stehen schon an der Todesstelle: kein Rückweg nötig.
                self._drop_corpse()
                self._death_position = None
                self._return_attempts = 0
                self._rest_after_revive = True
                return True
        return False

    async def _try_reclaim_corpse(self, budget: float) -> bool:
        """Schickt CMSG_RECLAIM_CORPSE, bis der Server den Geist wiederbelebt."""
        end = time.time() + budget
        tries = 0
        while self.running and time.time() < end:
            me = self.objects.me
            if self._revived():
                return True
            # Nach mehreren Toden kurz hintereinander verlangt der Server eine Wartezeit.
            wait = self._reclaim_delay_until - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            tries += 1
            self_guid = self.self_guid or 0
            # Manche Kerne erwarten den Leichnam, andere die Spieler-GUID - deshalb
            # wird abwechselnd gesendet. Kerne, die die GUID ignorieren, stört das nicht.
            if self._corpse_guid and (tries % 2 == 1 or not self_guid):
                guid = self._corpse_guid
            else:
                guid = self_guid or self._corpse_guid or 0
            log.info(
                "[tod] Wiederbeleben am Leichnam (Versuch %d, %.0f yd, GUID %d%s)",
                tries,
                self._corpse_distance(),
                guid,
                "" if self._corpse_guid else " - Korpsobjekt unbekannt",
            )
            try:
                await self.world.send(
                    opcodes.CMSG_RECLAIM_CORPSE, Writer().u64(guid).build()
                )
            except WorldError:
                self.running = False
                return False
            for _ in range(10):
                await asyncio.sleep(0.5)
                if self._revived():
                    return True
            if tries % 3 == 0:
                me = self.objects.me
                log.info(
                    "[tod] Server hat nicht bestätigt (HP %d/%d, Geist-Flag %s, GUID %d)",
                    me.health if me else 0,
                    me.max_health if me else 0,
                    "an" if me is not None and me.is_ghost else "aus",
                    guid,
                )
            # Der Server sieht uns evtl. etwas anders stehen: Position und Mover neu senden.
            await self._resync_movement()
        return False

    async def _return_to_death_spot(self) -> None:
        """Läuft nach der Wiederbelebung zur Stelle zurück, an der der Char starb."""
        if self._death_position is None:
            return
        if self._death_map and self.map_id and self.map_id != self._death_map:
            log.info(
                "[zurück] andere Karte (Map %d statt %d) - zurücklaufen nicht möglich",
                self.map_id,
                self._death_map,
            )
            self._death_position = None
            return

        x, y, z = self._death_position
        distance = math.hypot(x - self.position[0], y - self.position[1])
        if distance <= 15:
            log.info("[zurück] stehe schon an der Todesstelle (%.0f yd)", distance)
            self._death_position = None
            self._return_attempts = 0
            return

        self._return_attempts += 1
        log.info(
            "[zurück] wiederbelebt - laufe %.0f yd zur Todesstelle (%.1f, %.1f) | Pos %.1f/%.1f",
            distance,
            x,
            y,
            self.position[0],
            self.position[1],
        )
        await self._move_to(
            x,
            y,
            z,
            stop_distance=4.0,
            max_seconds=min(900.0, distance / 3.0 + 30.0),
        )

        remaining = math.hypot(x - self.position[0], y - self.position[1])
        if remaining <= 15:
            log.info("[zurück] Todesstelle erreicht (%.0f yd übrig) - farme weiter", remaining)
            self._death_position = None
            self._return_attempts = 0
            return
        max_attempts = int(self.behavior.get("return_attempts", 3))
        if self._return_attempts >= max_attempts:
            log.warning(
                "[zurück] Todesstelle nicht erreichbar (%.0f yd übrig, %d Versuche) - farme hier weiter",
                remaining,
                self._return_attempts,
            )
            self._death_position = None
            self._return_attempts = 0
            return
        log.info(
            "[zurück] noch %.0f yd entfernt - versuche es erneut (%d/%d)",
            remaining,
            self._return_attempts,
            max_attempts,
        )

    async def _idle(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and self.running:
            await self._send_move(opcodes.MSG_MOVE_HEARTBEAT, 0)
            await asyncio.sleep(10.0)

    def _distance_to(self, obj: WoWObject) -> float:
        """Entfernung in der Ebene.

        Die Hoehe bleibt bewusst aussen vor: ein Mob auf einem Huegel ist fuer
        Nahkampf erreichbar, auch wenn 8 yd Hoehenunterschied dazwischen liegen.
        Sonst laeuft der Bot unter dem Mob stehen und „kommt nie heran".
        """
        cur_x, cur_y, _, _ = self.position
        return math.hypot(obj.x - cur_x, obj.y - cur_y)

    def _jitter(self, seconds: float) -> float:
        if not self.behavior.get("humanize", True):
            return seconds
        return seconds * random.uniform(0.8, 1.2)

    # --------------------------------------------------------- Kreaturen-Namen
    def _short_name(self, obj: WoWObject) -> str:
        name = self.creature_names.get(obj.entry)
        return f"{name} (Entry {obj.entry})" if name else f"Entry {obj.entry}"

    def _describe(self, obj: WoWObject) -> str:
        name = self.creature_names.get(obj.entry)
        if name:
            return f"{name} (Entry {obj.entry}, Level {obj.level})"
        return f"Entry {obj.entry}, Level {obj.level}"

    def _last_target_alive(self) -> WoWObject | None:
        """Läuft der zuletzt angegriffene Mob noch? Dann ist der Kampf nicht vorbei."""
        if self._last_fight_guid is None:
            return None
        obj = self.objects.get(self._last_fight_guid)
        if obj is None or not obj.alive or not obj.has_position:
            return None
        if self._distance_to(obj) > float(self.behavior.get("leash_range", 70)):
            return None
        return obj

    def _nearest_label(self, nearest: tuple[float, int] | None) -> str:
        if nearest is None:
            return "?"
        entry = nearest[1]
        name = self.creature_names.get(entry)
        return f"{name} (Entry {entry})" if name else f"Entry {entry}"

    def _request_names(self) -> None:
        """Holt fehlende Kreaturen-Namen einmal pro Entry vom Server.

        Der Umkreis deckt die ganze Kartenansicht ab: sonst bleiben Einheiten
        am Rand (Geistheiler, Händler) als "Entry 6491" und ohne Symbol.
        """
        limit = max(
            float(self.behavior.get("max_target_distance", 40)) + 20,
            MAP_VIEW_RADIUS,
        )
        now = time.time()
        pending = 0
        for obj in self.objects.objects.values():
            if not obj.is_mob or not obj.entry:
                continue
            if obj.entry in self.creature_names:
                continue
            if obj.has_position and self._distance_to(obj) > limit:
                continue
            requested = self._name_requests.get(obj.entry)
            if requested is not None and now - requested < 60:
                continue
            if pending >= 10:
                break
            self._name_requests[obj.entry] = now
            pending += 1
            self._spawn(self._query_creature(obj.entry, obj.guid))

    async def _fetch_name(
        self, entry: int, guid: int, timeout: float = 1.0
    ) -> str | None:
        """Holt den Namen und wartet kurz auf die Antwort des Servers."""
        if entry in self.creature_names:
            return self.creature_names.get(entry) or None
        if entry not in self._name_requests:
            self._name_requests[entry] = time.time()
            await self._query_creature(entry, guid)
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            if entry in self.creature_names:
                return self.creature_names.get(entry) or None
            await asyncio.sleep(0.02)
        return None

    async def _query_creature(self, entry: int, guid: int) -> None:
        try:
            await self.world.send(
                opcodes.CMSG_CREATURE_QUERY, Writer().u32(entry).u64(guid).build()
            )
        except WorldError:
            self.running = False

    def _on_creature_query_response(self, body: bytes) -> None:
        try:
            reader = Reader(body)
            raw_entry = reader.u32()
        except ProtocolError:
            return
        entry = raw_entry & 0x7FFFFFFF
        self._name_requests.pop(entry, None)
        if raw_entry & 0x80000000:  # Server kennt die Kreatur nicht
            self.creature_names[entry] = ""
            return
        try:
            name = reader.cstring()
        except ProtocolError:
            return
        if name:
            self.creature_names[entry] = name
            log.debug("[name] Entry %d heisst '%s'", entry, name)

    # ---------------------------------------------------- Gegenstands-Namen
    def _item_label(self, entry: int, name: "str | None" = None) -> str:
        """Gegenstand fuer Log und Fenster lesbar machen."""
        if name is None:
            name = self.item_names.get(entry)
        if name:
            return f"{name} (Item {entry})"
        return f"Item {entry}"

    async def _query_item(self, entry: int) -> None:
        try:
            await self.world.send(
                opcodes.CMSG_ITEM_QUERY_SINGLE, Writer().u32(entry).u64(0).build()
            )
        except WorldError:
            self.running = False

    def _request_item_names(self, entries) -> None:
        """Holt fehlende Gegenstands-Namen im Hintergrund (max. 1 Abfrage pro Minute)."""
        now = time.time()
        for entry in entries:
            if not entry or entry in self.item_names:
                continue
            requested = self._item_requests.get(entry)
            if requested is not None and now - requested < 60:
                continue
            self._item_requests[entry] = now
            self._spawn(self._query_item(entry))

    async def _fetch_item_name(
        self, entry: int, timeout: float = 1.5
    ) -> "str | None":
        """Wartet kurz auf den Namen eines Gegenstands (nur bei Namensfiltern)."""
        if entry in self.item_names:
            return self.item_names.get(entry) or None
        self._item_requests.setdefault(entry, time.time())
        await self._query_item(entry)
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            if entry in self.item_names:
                return self.item_names.get(entry) or None
            await asyncio.sleep(0.02)
        return None

    def _on_item_query_response(self, body: bytes) -> None:
        """SMSG_ITEM_QUERY_SINGLE_RESPONSE: Entry, 0, Klasse, Unterklasse, Unk, Name.

        Der Name steht als C-String hinter 5 DWORDs. Antwortet ein Server anders
        (andere Kern-Version), wird der erste lesbare Text im Paket genommen.
        """
        try:
            raw_entry = Reader(body).u32()
        except ProtocolError:
            return
        if not raw_entry:
            return
        if raw_entry & 0x80000000:  # Server kennt den Gegenstand nicht
            entry = raw_entry & 0x7FFFFFFF
            self.item_names[entry] = ""
            self._item_requests.pop(entry, None)
            return
        entry = raw_entry
        self._item_requests.pop(entry, None)
        name = ""
        try:
            reader = Reader(body)
            reader.u32()  # Entry
            reader.u32()  # 0
            reader.u32()  # Klasse
            reader.u32()  # Unterklasse
            reader.i32()  # Unk
            name = reader.cstring().strip()
        except ProtocolError:
            name = ""
        if not name:
            name = _plausible_name(body[8:])
        self.item_names[entry] = name
        if name:
            log.debug("[item] %d heisst '%s'", entry, name)

    # ------------------------------------------------------------- Callbacks
    def _on_time_sync(self, body: bytes) -> None:
        self._spawn(self.world.answer_time_sync(body))

    def _on_health_change(self, guid: int, previous: int, current: int, obj: WoWObject) -> None:
        delta = current - previous
        if guid == self.self_guid:
            percent = 100.0 * current / obj.max_health if obj.max_health else 0.0
            log.info("[hp] %+d -> %d/%d (%.0f%%)", delta, current, obj.max_health, percent)
            if delta < 0:
                self._last_damage_at = time.time()
            return
        # Lebenspunkte von Mobs stehen in den Trefferzeilen; hier nur im Debug-Log,
        # sonst ueberfluten die Zeilen die Kampfübersicht.
        if log.isEnabledFor(logging.DEBUG):
            label = self.creature_names.get(obj.entry) or f"Entry {obj.entry}"
            log.debug("[hp] %s %+d -> %d/%d", label, delta, current, obj.max_health)

    def _on_attacker_state_update(self, body: bytes) -> None:
        """SMSG_ATTACKERSTATEUPDATE: der echte Schlag des Servers.

        Aufbau (CombatLogPackets.cpp, 3.3.5): Flags, Angreifer, Opfer, Schaden,
        Overkill, Anzahl Teilschaeden, Teilschaeden, Absorb/Resist, Opferzustand,
        Angreiferzustand, Zauber-ID, optional Block/Wut/Trefferchance-Werte.
        """
        try:
            reader = Reader(body)
            flags = reader.u32()
            attacker = reader.packed_guid()
            victim = reader.packed_guid()
            damage = reader.u32()
            reader.u32()  # Overkill
            sub_count = reader.u8()
            for _ in range(sub_count):
                reader.u32()  # Schadensschule
                reader.f32()  # Schaden als Fliesskomma
                reader.u32()  # Schaden
            if flags & (opcodes.HITINFO_FULL_ABSORB | opcodes.HITINFO_PARTIAL_ABSORB):
                for _ in range(sub_count):
                    reader.u32()
            if flags & (opcodes.HITINFO_FULL_RESIST | opcodes.HITINFO_PARTIAL_RESIST):
                for _ in range(sub_count):
                    reader.u32()
            victim_state = reader.u8()
        except ProtocolError:
            log.debug("Trefferpaket war nicht lesbar", exc_info=True)
            return

        if victim == self.self_guid:
            # UNS hat jemand geschlagen. Das zaehlt als Kampf, auch wenn die HP
            # dabei nicht sinken (absorbierter Treffer, schon bei 1 HP, Schild):
            # sonst sitzt der Bot die Pause weiter, waehrend er verpruegelt wird.
            now = time.time()
            self._last_damage_at = now
            self._last_attacker = attacker
            if not flags & opcodes.HITINFO_MISS:
                log.debug(
                    "[kampf] Treffer von Entry %d erhalten (%d Schaden)",
                    self.objects.get(attacker).entry if self.objects.get(attacker) else 0,
                    damage,
                )
            return

        if attacker != self.self_guid:
            return

        obj = self.objects.get(victim)
        target = obj if obj is not None else self.objects.get(self.target_guid or 0)
        if target is None:
            return
        self._last_swing_at = time.time()
        self._last_damage_seen_at = self._last_swing_at
        self._attack_confirmed = True

        if not self._target_hp_est:
            self._target_hp_est = target.health
        # Das Trefferpaket kommt vor dem Feld-Update: den Schaden selbst abziehen,
        # ausser der Server hat ihn schon verbucht (dann gilt sein Wert).
        if 0 <= target.health < self._target_hp_est:
            self._target_hp_est = target.health
        elif not flags & opcodes.HITINFO_MISS:
            self._target_hp_est = max(0, self._target_hp_est - damage)
        hp_text = self._estimated_hp_text(target)
        if flags & opcodes.HITINFO_MISS:
            self._swing_misses += 1
            self._engage_swings += 1
            reason = opcodes.VICTIM_STATE_NAMES.get(victim_state, "verfehlt")
            log.info(
                "[hit] Schlag #%d %s - %s (noch %s)",
                self._engage_swings,
                reason,
                self._short_name(target),
                hp_text,
            )
            return

        self._swing_hits += 1
        self._engage_swings += 1
        self._fight_damage += damage
        extras = []
        if flags & opcodes.HITINFO_CRITICALHIT:
            extras.append("kritisch")
        if flags & opcodes.HITINFO_CRUSHING:
            extras.append("Wucht")
        if flags & opcodes.HITINFO_GLANCING:
            extras.append("Streichler")
        if flags & opcodes.HITINFO_BLOCK:
            extras.append("geblockt")
        if victim_state == opcodes.VICTIMSTATE_EVADES:
            extras.append("Ziel entkommt")
        suffix = f" ({', '.join(extras)})" if extras else ""
        log.info(
            "[hit] Schlag #%d - %d Schaden%s - %s (noch %s)",
            self._engage_swings,
            damage,
            suffix,
            self._short_name(target),
            hp_text,
        )

    def _on_log_xp_gain(self, body: bytes) -> None:
        """SMSG_LOG_XPGAIN: GUID, Gesamt-EP, Typ (0 = Kill), Basis-EP, Gruppen-Faktor, RaF."""
        try:
            reader = Reader(body)
            reader.u64()  # GUID des Opfers
            total = reader.u32()
            kind = reader.u8() if reader.remaining else 0
            base = total
            if kind == 0 and reader.remaining >= 4:
                base = reader.u32()
        except ProtocolError:
            log.debug("Erfahrungs-Paket nicht lesbar")
            return

        self.xp_session += total
        # Die EP im Charakterfeld kommen direkt vom Server (PLAYER_XP); das Feld
        # wird bewusst nicht selbst veraendert. Fuer die Anzeige zaehlen wir den
        # gerade vergebenen Betrag hinzu, weil dieses Paket vor dem Feld-Update kommt.
        me = self.objects.me
        label = "Kill" if kind == 0 else "Quest/Event"
        bonus = total - base
        bonus_info = f", davon {bonus} Bonus" if bonus > 0 else ""
        percent = ""
        if me is not None and me.next_level_xp:
            self._xp_shown = min(me.next_level_xp, me.xp + total)
            percent = " | %.1f %% bis Stufe %d (%d/%d EP)" % (
                100.0 * self._xp_shown / me.next_level_xp,
                me.level + 1,
                self._xp_shown,
                me.next_level_xp,
            )
        log.info("[xp] +%d EP (%s%s)%s", total, label, bonus_info, percent)

    def _on_level_up(self, body: bytes) -> None:
        if len(body) < 4:
            return
        level = int.from_bytes(body[:4], "little")
        log.info("[level] Stufe %d erreicht!", level)
        me = self.objects.me
        if me is not None:
            me.level = level
        self._last_level = level
        self._xp_shown = 0

    def _on_death_release_loc(self, body: bytes) -> None:
        """Server teilt mit, wo der Geist freigelassen wurde (Friedhof)."""
        if len(body) < 16:
            return
        map_id = struct.unpack_from("<i", body, 0)[0]
        x, y, z = struct.unpack_from("<fff", body, 4)
        self._graveyard_pos = (x, y, z)
        log.info(
            "[tod] Geist am Friedhof freigelassen (Map %d, %.1f/%.1f/%.1f)",
            map_id,
            x,
            y,
            z,
        )

    def _on_corpse_reclaim_delay(self, body: bytes) -> None:
        """Server meldet die Restzeit, bis der Leichnam wiederbelebt werden darf."""
        if len(body) < 4:
            return
        remaining = int.from_bytes(body[:4], "little") / 1000.0
        self._reclaim_delay_until = time.time() + remaining
        if remaining > 1:
            log.info("[tod] Server: Wiederbeleben am Leichnam erst nach %.0f s", remaining)

    def _fight_report(self, target: WoWObject, duration: float) -> None:
        """Status-Block nach jedem Kampf."""
        me = self.objects.me
        gained = self.xp_session - self._xp_at_fight_start
        average = self._fight_damage / self._swing_hits if self._swing_hits else 0.0
        log.info(
            "[kampf] ---- %s besiegt: %.1f s, %d Treffer%s, %d Schaden%s ----",
            self._describe(target),
            duration,
            self._swing_hits,
            f" / {self._swing_misses} verfehlt" if self._swing_misses else "",
            self._fight_damage,
            f" (Ø {average:.0f})" if self._swing_hits else "",
        )
        if me is not None and me.next_level_xp:
            log.info(
                "[kampf] Erfahrung +%d EP | Stufe %d: %d/%d EP (%.1f %%)",
                gained,
                me.level,
                me.xp,
                me.next_level_xp,
                100.0 * me.xp / me.next_level_xp,
            )
        else:
            log.info(
                "[kampf] Erfahrung +%d EP (Gesamt %d EP, Stufe noch unbekannt)",
                gained,
                self.xp_session,
            )
        if me is not None and me.max_health:
            mana = self._power_text(me, with_percent=True)
            xp = (
                f" | EP {me.xp}/{me.next_level_xp}"
                f" ({100.0 * me.xp / me.next_level_xp:.1f} %)"
                if me.next_level_xp
                else f" | EP gesamt {self.xp_session}"
            )
            log.info(
                "[kampf] Status: HP %d/%d (%.0f %%)%s | Stufe %d%s | Kills %d | Loot %d "
                "| Kupfer %d | Pos %.1f/%.1f | %.0f min",
                me.health,
                me.max_health,
                me.health_percent,
                mana,
                me.level,
                xp,
                self.kills,
                self.looted,
                self.copper,
                self.position[0],
                self.position[1],
                (time.time() - self.started_at) / 60.0,
            )
        self.log_nearby("kampf")

    def _on_attack_start(self, body: bytes) -> None:
        attacker = victim = 0
        if len(body) >= 16:
            attacker, victim = struct.unpack_from("<QQ", body, 0)
        if victim == self.self_guid and attacker != self.self_guid:
            # Ein Gegner hat uns angegriffen.
            obj = self.objects.get(attacker)
            if obj is not None:
                log.info("[kampf] %s greift mich an", self._short_name(obj))
            return
        self._attack_confirmed = True
        self._attack_rejected = 0
        log.info("[kampf] Server hat den Angriff übernommen")

    def _on_attack_stop(self, body: bytes) -> None:
        target = self.objects.get(self.target_guid) if self.target_guid else None
        # Wichtig: der Server beendet den Autohit auch bei Reichweiten- oder
        # Sichtlinienproblemen. Ohne das Zuruecksetzen von _attack_target wuerde
        # der Bot nie wieder CMSG_ATTACK_SWING senden und nur noch dastehen.
        self._attack_target = None
        if time.time() - self._attack_sent_at < 2.0 and target is not None and target.alive:
            self._attack_rejected += 1
            log.info(
                "[!] Server hat den Angriff verworfen (%d/3) - Ziel evtl. unangreifbar",
                self._attack_rejected,
            )
            if self._attack_rejected >= 3:
                self._attack_rejected = 0
                self._abandon_target = True
            return
        log.info("[kampf] Server: Angriff beendet")

    def _on_cast_failed(self, body: bytes) -> None:
        """Server lehnt einen Zauber ab - z. B. unbekannte Rank oder kein Mana.

        Aufbau (3.3.5): cast_count u8, spellId u32, result u8.
        """
        spell_id = 0
        result = -1
        if len(body) >= 6:
            spell_id = int.from_bytes(body[1:5], "little")
            result = body[5]
        elif len(body) >= 5:
            spell_id = int.from_bytes(body[0:4], "little")
            result = body[4]
        if not spell_id:
            log.info("[!] Server lehnt einen Zauber ab (Grund %d)", result)
            return
        key = (spell_id, result)
        now = time.time()
        count, last = self._cast_failed_logged.get(key, (0, 0.0))
        count += 1
        self._cast_failed_logged[key] = (count, now)
        heal = spell_id in self._heal_candidates()
        requested = heal or any(
            int(spell.get("id") or 0) == spell_id for spell in self._rotation()
        )
        # Nur die erste Ablehnung melden, danach hoechstens jede Minute: manche
        # Kerne schicken dasselbe Paket mehrfach hintereinander.
        if count > 1 and now - last < 60.0:
            log.debug("[!] Zauber %d erneut abgelehnt (Grund %d)", spell_id, result)
            return
        suffix = f" ({count}x)" if count > 1 else ""
        if heal:
            log.warning(
                "[heil] Server lehnt Heilszauber %d ab (Grund %d)%s - andere Rank "
                "ueber behavior.heal_spell_id setzen",
                spell_id,
                result,
                suffix,
            )
        elif requested:
            log.warning(
                "[!] Server lehnt Zauber %d ab (Grund %d)%s", spell_id, result, suffix
            )
        else:
            log.info(
                "[!] Server lehnt Zauber %d ab (Grund %d)%s - nicht vom Bot angefordert "
                "(kommt vom Server oder einem anderen Client)",
                spell_id,
                result,
                suffix,
            )

    def _on_spell_failure(self, body: bytes) -> None:
        if len(body) >= 13:
            spell_id = int.from_bytes(body[8:12], "little")
            error = body[12]
            log.info("[!] Zauber %d fehlgeschlagen (Code %d)", spell_id, error)
        else:
            log.info("[!] Zauber fehlgeschlagen")
        # Zwei Fehlschläge kurz hintereinander: oft steht eine Wand in der Sichtlinie.
        now = time.time()
        self._cast_failures = [t for t in self._cast_failures if now - t < 10.0]
        self._cast_failures.append(now)
        target = self.objects.get(self.target_guid) if self.target_guid else None
        if len(self._cast_failures) >= 2 and target is not None:
            if self._distance_to(target) > float(self.behavior.get("melee_range", 4.5)):
                self._cast_failures.clear()
                log.info("[umweg] Zauber scheitern am Ziel - versuche anderen Winkel")
                self._detour_requested = True

    def _on_attack_error(self, body: bytes) -> None:
        # Typisch, wenn eine Wand/Fels zwischen uns und dem Ziel liegt.
        log.info("[!] Server: Ziel kann nicht angegriffen werden - suche einen Umweg")
        target = self.objects.get(self.target_guid) if self.target_guid else None
        if target is not None and target.has_position and self._distance_to(target) > 12.0:
            # Nicht der Mob selbst ist im Weg, sondern etwas dazwischen: die Mitte
            # der Sichtlinie als Hindernis merken. Auf Nahkampfdistanz wäre die
            # Mitte der Mob selbst - dann lieber nichts merken, das endet sonst im
            # Kreisen um das Ziel.
            self.wayfinder.block(
                (self.position[0] + target.x) / 2.0,
                (self.position[1] + target.y) / 2.0,
                radius=self.wayfinder.cell_size,
            )
        self._detour_requested = True
        self._attack_target = None

    def _on_attack_not_in_range(self, body: bytes) -> None:
        log.info("[!] Server: Ziel ausser Reichweite - gehe dichter heran")
        self._force_approach = True
        self._attack_target = None

    def _on_attack_bad_facing(self, body: bytes) -> None:
        # Kein Blacklist-Eintrag: der Bot dreht sich einfach zum Ziel und schlägt erneut zu.
        log.info("[!] Server: falsche Blickrichtung - drehe mich zum Ziel")
        self._face_requested = True
        # Angriff neu anmelden, sobald wir richtig stehen.
        self._attack_target = None

    def _spawn(self, coro) -> None:
        """Hintergrund-Task mit starker Referenz.

        `asyncio.create_task` allein haelt den Task nicht fest - der GC darf ihn
        an einem await-Punkt einsammeln, dann faellt z.B. eine Namensabfrage
        stillschweigend aus.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _apply_rules(self) -> None:
        """Übernimmt geänderte Ziele-/Beuteregeln aus dem Fenster (nur bei Änderung)."""
        if not self._rules_dirty:
            return
        self._rules_dirty = False
        mob_rules = MobRules.from_behavior(self.behavior)
        loot_rules = LootRules.from_behavior(self.behavior)
        if mob_rules == self._mob_rules and loot_rules == self._loot_rules:
            return
        self._mob_rules = mob_rules
        self._loot_rules = loot_rules
        log.info(
            "[filter] Regeln übernommen: %s | Beute: %s",
            self._mob_rules.summary(self._own_level()),
            self._loot_rules.summary(),
        )
        # Läuft gerade ein Kampf (auch noch beim Hinlaufen) gegen ein Ziel, das
        # jetzt ausgeschlossen ist? Dann wird der Kampf beendet.
        if self._engaging:
            target = self.objects.get(self.target_guid) if self.target_guid else None
            # Ein angeklickter Mob ist ein ausdrücklicher Befehl - Regeln gelten
            # für ihn nicht, sonst würde der Bot den eigenen Auftrag abbrechen.
            ordered = (
                self._command is not None
                and self._command.get("kind") == "attack"
                and self._command.get("guid") == (target.guid if target else None)
            )
            if (
                target is not None
                and target.alive
                and not ordered
                and not self._mob_allowed(target)
            ):
                self._rule_abort = True

    def set_rules(self, behavior_patch: dict) -> None:
        """Ziele-/Beuteregeln aus dem Fenster übernehmen - wirkt im nächsten Tick."""
        if not isinstance(behavior_patch, dict):
            return
        for key, value in behavior_patch.items():
            self.behavior[key] = value
        self._rules_dirty = True

    def set_command(self, command: "dict | None") -> None:
        """Befehl aus der Karte übernehmen (wirkt im nächsten Tick).

        ``{"kind": "goto", "x": ..., "y": ...}`` läuft den Punkt an und
        ignoriert bis dahin Ziele und Beute, ``{"kind": "attack", "guid": ...}``
        greift genau diesen Mob zuerst an. ``None`` löscht den Auftrag.
        """
        if not command:
            self._command = None
            self._command_logged = False
            self._command_tries = 0
            return
        kind = command.get("kind")
        try:
            if kind == "goto":
                self._command = {
                    "kind": "goto",
                    "x": float(command["x"]),
                    "y": float(command["y"]),
                }
            elif kind == "attack":
                self._command = {"kind": "attack", "guid": int(command["guid"])}
            else:
                return
        except (KeyError, TypeError, ValueError):
            return
        self._command_logged = False
        self._command_tries = 0

    def _command_pending(self) -> bool:
        """Steht irgendein Kartenbefehl an? (Er hat Vorrang vor der Routine.)"""
        return self._command is not None

    def _goto_pending(self) -> bool:
        """Steht ein Kartenklick „hinlaufen“ an? Der bricht sogar einen Kampf ab."""
        command = self._command
        return command is not None and command.get("kind") == "goto"

    def _clear_command(self, reason: str = "") -> None:
        if self._command is None:
            return
        self._command = None
        self._command_logged = False
        self._command_tries = 0
        if reason:
            log.info("[befehl] %s", reason)

    async def _run_command(self, me: "WoWObject | None") -> bool:
        """Kartenbefehl abarbeiten. True = dieser Tick ist damit verbraucht."""
        command = self._command
        if command is None:
            return False
        if me is None or not me.has_position:
            self._clear_command("keine eigene Position - Befehl verworfen")
            return False

        if command["kind"] == "goto":
            x, y = command["x"], command["y"]
            dist = math.hypot(x - self.position[0], y - self.position[1])
            if not self._command_logged:
                self._command_logged = True
                log.info(
                    "[befehl] laufe zum Kartenpunkt (%.1f, %.1f) - %.0f yd, "
                    "ignoriere bis dahin Ziele und Beute",
                    x,
                    y,
                    dist,
                )
            if dist <= 2.5:
                if self._command is command:
                    self._clear_command(
                        "Punkt erreicht - heile mich bei Bedarf und suche weiter"
                    )
                return False  # nächster Tick: Rast/Heilung/Zielsuche wie immer
            # In Etappen laufen, damit der Tick (Status, Tod, Karte) lebendig bleibt.
            arrived = await self._move_to(
                x,
                y,
                self.position[2],
                stop_distance=2.0,
                max_seconds=25.0,
                abort=lambda: self._command is not command,
            )
            if self._command is not command:
                # Inzwischen ein neuer Klick: der kommt im nächsten Tick dran.
                return True
            if arrived:
                self._clear_command("Punkt erreicht - heile mich bei Bedarf und suche weiter")
                return False
            return True

        if command["kind"] == "attack":
            target = self.objects.get(int(command["guid"]))
            if target is None or not target.alive or not target.has_position:
                self._clear_command("Mob ist nicht mehr da - mache normal weiter")
                return False
            if not self._command_logged:
                self._command_logged = True
                log.info("[befehl] greife zuerst %s an", self._describe(target))
            await self._engage(target)
            if self._command is not command:
                return True  # schon ein neuer Auftrag gesetzt
            if not target.alive:
                self._clear_command("Mob erledigt - mache normal weiter")
                return False
            self._command_tries += 1
            if self._command_tries >= 3:
                self._clear_command("Mob nicht erreichbar - Befehl verworfen")
                return False
            return True

        self._command = None
        return False

    def set_hotspots(self, hotspots: list) -> None:
        """Hotspots aus der GUI übernehmen (der Bot läuft sie beim Suchen ab)."""
        cleaned = []
        for spot in hotspots or []:
            try:
                cleaned.append({"x": float(spot["x"]), "y": float(spot["y"])})
            except (KeyError, TypeError, ValueError):
                continue
        self.behavior["hotspots"] = cleaned
        log.info("[weg] %d Hotspot(s) übernommen", len(cleaned))

    def map_view(self) -> dict:
        """Daten für die Kartenansicht der GUI (Hindernisse, Spieler, Mobs)."""
        target = self.objects.get(self.target_guid) if self.target_guid else None
        if self._map_walls_version != self.wayfinder.walls_version:
            self._map_walls = self.wayfinder.wall_cells()
            self._map_walls_version = self.wayfinder.walls_version
        # list(): die GUI liest das waehrend der Bot Objekte anlegt/entfernt -
        # ein Python-Loop direkt ueber das Dict kann dann abstuerzen.
        now = time.time()
        me_x, me_y = self.position[0], self.position[1]
        mobs = []
        for obj in list(self.objects.objects.values()):
            if (
                not obj.is_mob
                or obj.guid == self.self_guid
                or not obj.alive
                or not obj.has_position
            ):
                continue
            # Laufende Kreaturen (SMSG_MONSTER_MOVE) mitwandern lassen
            x, y = obj.live_position(now)
            dist = math.hypot(x - me_x, y - me_y)
            if dist > MAP_VIEW_RADIUS:
                continue
            mobs.append(
                {
                    "guid": obj.guid,
                    "entry": obj.entry,
                    "name": self.creature_names.get(obj.entry) or f"Entry {obj.entry}",
                    "level": obj.level,
                    "hp": obj.health,
                    "max_hp": obj.max_health,
                    "hp_pct": obj.health_percent,
                    "x": x,
                    "y": y,
                    "attackable": obj.is_attackable,
                    # Freundlicher NPC = unterhält sich (Händler, Lehrer,
                    # Questgeber, Geistheiler). Neutrale Kreaturen ohne
                    # Dialog landen weiter in Grau.
                    "friendly": bool(obj.npc_flags),
                    "target": obj.guid == self.target_guid,
                    "dist": dist,
                    "allowed": self._mob_allowed(obj),
                }
            )
        command = self._command
        command_view = None
        if command is not None and command.get("kind") == "goto":
            command_view = {"kind": "goto", "x": command["x"], "y": command["y"]}
        elif command is not None and command.get("kind") == "attack":
            ordered = self.objects.get(int(command["guid"]))
            if ordered is not None and ordered.has_position:
                command_view = {
                    "kind": "attack",
                    "x": ordered.x,
                    "y": ordered.y,
                    "name": self.creature_names.get(ordered.entry)
                    or f"Entry {ordered.entry}",
                }
        return {
            "map": self.map_id,
            "pos": (self.position[0], self.position[1]),
            "command": command_view,
            "heading": self.position[3],
            "walls": self._map_walls,
            "cell_size": self.wayfinder.cell_size,
            "target": (
                (target.x, target.y)
                if target is not None and target.has_position
                else None
            ),
            "mobs": mobs,
        }

    def forget_obstacles(self) -> int:
        """Gelernte Hindernisse der aktuellen Karte verwerfen und Datei leeren."""
        count = self.wayfinder.wall_count()
        self.wayfinder.forget_all()
        self.wayfinder.save(force=True)
        log.info(
            "[weg] %d Hindernisse für Map %d zurückgesetzt - lerne die Karte neu",
            count,
            self.map_id,
        )
        return count

    # ----------------------------------------------------------------- Gelände
    def _start_terrain(self) -> None:
        """Geländedaten (Klippen, Wasser) aus dem Client im Hintergrund laden."""
        if self._terrain_started:
            return
        self._terrain_started = True
        if not self.behavior.get("terrain_aware", True):
            log.info("[gelände] ausgeschaltet (terrain_aware=false) - lerne nur aus Fehlern")
            return
        configured = (self.config.get("map") or {}).get("client")
        client = find_client_dir(configured)
        if client is None:
            log.info(
                "[gelände] kein Client gefunden - Klippen und Wasser werden nicht "
                "vorab erkannt (map.client in der Config setzen)"
            )
            return
        cache = app_support_dir("maps")
        cell_size = float(self.behavior.get("path_cell_size", 5.0))
        max_slope = float(self.behavior.get("terrain_max_slope", 1.6))
        water_depth = float(self.behavior.get("terrain_water_depth", 1.2))
        use_water = bool(self.behavior.get("terrain_water", True))

        def work() -> None:
            maps = ClientMaps(client, cache)
            if not maps.open():
                log.warning("[gelände] Client-Karten nicht lesbar: %s", maps.error)
                return
            terrain = Terrain(
                maps,
                cell_size=cell_size,
                max_slope=max_slope,
                water_depth=water_depth,
                use_water=use_water,
            )
            self.terrain = terrain
            self.wayfinder.set_terrain(terrain)
            log.info(
                "[gelände] Client-Daten bereit (%s) - Klippen ab %.1f Höhenmeter/Yard, "
                "Wasser ab %.1f Yard Tiefe",
                client,
                max_slope,
                water_depth,
            )

        threading.Thread(target=work, name="terrain-setup", daemon=True).start()

    def _update_terrain(self) -> None:
        """Aktuelle Karte melden und die Kachel um den Charakter bauen lassen."""
        terrain = self.terrain
        if terrain is None:
            return
        if terrain.map_id != self.map_id or not terrain.ready:
            if terrain.set_map(self.map_id):
                self._terrain_tiles = 0
        me = self.objects.me
        if me is not None and me.has_position:
            terrain.prefetch(me.x, me.y)
            stats = terrain.stats()
            if stats["tiles"] != self._terrain_tiles:
                self._terrain_tiles = stats["tiles"]
                log.info(
                    "[gelände] %d Kachel(n) geladen, %d Hinderniszellen (Klippen/Wasser)",
                    stats["tiles"],
                    stats["cells"],
                )

    def _nav_directory(self) -> Path:
        """Wo die gelernten Hindernisse liegen (pro Karte eine Datei)."""
        configured = str(self.behavior.get("path_storage") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return app_support_dir("nav")

    async def close(self) -> None:
        self.running = False
        self.wayfinder.save(force=True)
        await self.world.close()
        await self.auth.close()


def _compass(angle: float) -> str:
    """Bewegungsrichtung als Himmelsrichtung (0 rad = Osten, y wächst nach Norden)."""
    directions = ["Osten", "Nordosten", "Norden", "Nordwesten", "Westen", "Südwesten", "Süden", "Südosten"]
    index = int((angle % (2 * math.pi)) / (math.pi / 4) + 0.5) % 8
    return directions[index]


def _parse_char_enum(body: bytes) -> list[Character]:
    reader = Reader(body)
    count = reader.u8()
    characters: list[Character] = []
    for _ in range(count):
        try:
            guid = reader.u64()
            name = reader.cstring()
            reader.u8()  # race
            reader.u8()  # class
            reader.u8()  # gender
            reader.raw(5)  # skin, face, hair style/color, facial hair
            level = reader.u8()
            reader.u32()  # area
            reader.u32()  # map
            reader.raw(12)  # position
            reader.u32()  # guild id
            reader.u32()  # flags
            reader.u32()  # recustomization flags
            reader.u8()  # first login
            reader.u32()  # pet display
            reader.u32()  # pet level
            reader.u32()  # pet family
            reader.raw(23 * 9)  # Ausrüstung
        except ProtocolError:
            break
        characters.append(Character(guid=guid, name=name, level=level))
    return characters
