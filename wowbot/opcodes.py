"""Opcode-Tabelle (Auszug) für WoW 3.3.5a, Build 12340.

Die Werte stammen aus der Opcode-Tabelle von TrinityCore 3.3.5.
"""

from __future__ import annotations

# --- Login-/Auth-Protokoll (Port 3724, keine Header-Verschlüsselung) ---
CMD_AUTH_LOGON_CHALLENGE = 0x00
CMD_AUTH_LOGON_PROOF = 0x01
CMD_REALM_LIST = 0x10

# --- World-Verbindung ---
SMSG_AUTH_CHALLENGE = 0x01EC
CMSG_AUTH_SESSION = 0x01ED
SMSG_AUTH_RESPONSE = 0x01EE

CMSG_CHAR_ENUM = 0x0037
SMSG_CHAR_ENUM = 0x003B
CMSG_PLAYER_LOGIN = 0x003D
SMSG_LOGIN_VERIFY_WORLD = 0x0236
CMSG_LOGOUT_REQUEST = 0x004B
SMSG_LOGOUT_COMPLETE = 0x004D
CMSG_SET_ACTIVE_MOVER = 0x026A

SMSG_UPDATE_OBJECT = 0x00A9
SMSG_COMPRESSED_UPDATE_OBJECT = 0x01F6
SMSG_DESTROY_OBJECT = 0x00AA

MSG_MOVE_START_FORWARD = 0x00B5
MSG_MOVE_STOP = 0x00B7
# Server versetzt den Spieler (auch als Geist zum Friedhof). Wichtig: Fuer den
# Spieler selbst geht das Paket als MSG_MOVE_TELEPORT_ACK (0x0C7) raus - Aufbau
# PackedGuid + Sequenz + MovementInfo (Unit::SendTeleportPacket / MoveTeleport).
# 0x0C5 (MSG_MOVE_TELEPORT) bekommen nur die anderen Spieler in Sichtweite.
# Der Client bestaetigt mit demselben Opcode 0x0C7; bis dahin verwirft der Server
# ALLE Bewegungs-Pakete (IsBeingTeleported()).
MSG_MOVE_TELEPORT = 0x00C5
MSG_MOVE_TELEPORT_ACK = 0x00C7
MSG_MOVE_SET_FACING = 0x00DA
MSG_MOVE_HEARTBEAT = 0x00EE
SMSG_MONSTER_MOVE = 0x00DD

CMSG_USE_ITEM = 0x00AB
CMSG_CAST_SPELL = 0x012E
# 0x012F waere CMSG_CANCEL_CAST - Hinsetzen hat einen eigenen Opcode!
CMSG_STANDSTATECHANGE = 0x0101

# StandState (SharedDefines.h, 3.3.5)
STAND_STATE_STAND = 0
STAND_STATE_SIT = 1
SMSG_CAST_FAILED = 0x0130
SMSG_SPELL_GO = 0x0132
SMSG_SPELL_FAILURE = 0x0133
CMSG_ATTACK_SWING = 0x0141
CMSG_ATTACK_STOP = 0x0142
SMSG_ATTACK_START = 0x0143
SMSG_ATTACK_STOP = 0x0144
SMSG_ATTACK_SWING_NOT_IN_RANGE = 0x0145
SMSG_ATTACK_SWING_BAD_FACING = 0x0146
SMSG_ATTACK_SWING_DEAD_TARGET = 0x0148
SMSG_ATTACK_SWING_CANT_ATTACK = 0x0149
SMSG_ATTACKER_STATE_UPDATE = 0x014A

# Treffer-Flags in SMSG_ATTACKERSTATEUPDATE (UnitDefines.h, 3.3.5)
HITINFO_MISS = 0x00000010
HITINFO_FULL_ABSORB = 0x00000020
HITINFO_PARTIAL_ABSORB = 0x00000040
HITINFO_FULL_RESIST = 0x00000080
HITINFO_PARTIAL_RESIST = 0x00000100
HITINFO_CRITICALHIT = 0x00000200
HITINFO_BLOCK = 0x00002000
HITINFO_GLANCING = 0x00010000
HITINFO_CRUSHING = 0x00020000
HITINFO_RAGE_GAIN = 0x00800000
HITINFO_UNK1 = 0x00000001

# Opfer-Zustand im selben Paket (Unit.h, VictimState)
VICTIMSTATE_INTACT = 0
VICTIMSTATE_HIT = 1
VICTIMSTATE_DODGE = 2
VICTIMSTATE_PARRY = 3
VICTIMSTATE_INTERRUPT = 4
VICTIMSTATE_BLOCKS = 5
VICTIMSTATE_EVADES = 6
VICTIMSTATE_IS_IMMUNE = 7
VICTIMSTATE_DEFLECTS = 8

VICTIM_STATE_NAMES = {
    VICTIMSTATE_INTACT: "verfehlt",
    VICTIMSTATE_HIT: "getroffen",
    VICTIMSTATE_DODGE: "ausgewichen",
    VICTIMSTATE_PARRY: "pariert",
    VICTIMSTATE_INTERRUPT: "unterbrochen",
    VICTIMSTATE_BLOCKS: "geblockt",
    VICTIMSTATE_EVADES: "entkommt",
    VICTIMSTATE_IS_IMMUNE: "immun",
    VICTIMSTATE_DEFLECTS: "abgelenkt",
}
CMSG_SET_SELECTION = 0x013D

CMSG_LOOT = 0x015D
CMSG_LOOT_MONEY = 0x015E
CMSG_LOOT_RELEASE = 0x015F
SMSG_LOOT_RESPONSE = 0x0160
SMSG_LOOT_MONEY_NOTIFY = 0x0163
SMSG_LOOT_LIST = 0x03F9
CMSG_AUTOSTORE_LOOT_ITEM = 0x0108

# Tod / Leichnam / Geistheiler (Opcodes.cpp 3.3.5)
CMSG_REPOP_REQUEST = 0x015A
CMSG_RECLAIM_CORPSE = 0x01D2
SMSG_CORPSE_RECLAIM_DELAY = 0x0269
CMSG_SPIRIT_HEALER_ACTIVATE = 0x021C
SMSG_DEATH_RELEASE_LOC = 0x0378

# Erfahrung (Opcodes.cpp 3.3.5: 0x1D0 / 0x1D4)
SMSG_LOG_XPGAIN = 0x01D0
SMSG_LEVELUP_INFO = 0x01D4

# Kreaturen-Abfrage (QueryPackets: QueryCreature)
CMSG_CREATURE_QUERY = 0x0060
SMSG_CREATURE_QUERY_RESPONSE = 0x0061

# Gegenstands-Abfrage (QueryPackets: QueryItem, 3.3.5a: 0x0056 / 0x0058)
# Der Name steht im Antwortpaket als C-String hinter Entry, 0, Klasse, Unterklasse, Unk.
CMSG_ITEM_QUERY_SINGLE = 0x0056
SMSG_ITEM_QUERY_SINGLE_RESPONSE = 0x0058

CMSG_PING = 0x01DC
SMSG_PONG = 0x01DD
SMSG_TIME_SYNC_REQ = 0x0390
CMSG_TIME_SYNC_RESP = 0x0391
CMSG_KEEP_ALIVE = 0x0407

# --- Update-Object-Typen ---
UPDATETYPE_VALUES = 0
UPDATETYPE_MOVEMENT = 1
UPDATETYPE_CREATE_OBJECT = 2
UPDATETYPE_CREATE_OBJECT2 = 3
UPDATETYPE_OUT_OF_RANGE_OBJECTS = 4
UPDATETYPE_NEAR_OBJECTS = 5

# --- Objekttypen ---
TYPEID_ITEM = 1
TYPEID_CONTAINER = 2
TYPEID_UNIT = 3
TYPEID_PLAYER = 4
TYPEID_GAMEOBJECT = 5
TYPEID_DYNAMICOBJECT = 6
TYPEID_CORPSE = 7

# --- Update-Field-Indizes (3.3.5a, OBJECT_END = 6) ---
FIELD_OBJECT_ENTRY = 0x03
FIELD_UNIT_HEALTH = 0x18
FIELD_UNIT_POWER = 0x19
FIELD_UNIT_MAX_HEALTH = 0x20
FIELD_UNIT_MAX_POWER = 0x21
FIELD_UNIT_LEVEL = 0x36
FIELD_UNIT_FACTION = 0x37
FIELD_UNIT_FLAGS = 0x3B
FIELD_UNIT_NPC_FLAGS = 0x52
FIELD_UNIT_DYNAMIC_FLAGS = 0x4F
# UNIT_FIELD_BYTES_0: Byte 3 ist die Kraftart (Mana/Wut/Energie ...)
FIELD_UNIT_BYTES_0 = 0x17

# Kraftarten (SharedDefines.h, Powers)
POWER_MANA = 0
POWER_RAGE = 1
POWER_FOCUS = 2
POWER_ENERGY = 3
POWER_HAPPINESS = 4
POWER_RUNE = 5
POWER_RUNIC_POWER = 6
POWER_NAMES = {
    POWER_MANA: "Mana",
    POWER_RAGE: "Wut",
    POWER_FOCUS: "Fokus",
    POWER_ENERGY: "Energie",
    POWER_HAPPINESS: "Zufriedenheit",
    POWER_RUNE: "Runen",
    POWER_RUNIC_POWER: "Runenmacht",
}

# Unit-Flags (UnitDefines.h, 3.3.5) - solche Einheiten sind keine Ziele
UNIT_FLAG_EVADING_HOME = 0x00000010
UNIT_FLAG_NON_ATTACKABLE = 0x00000002
UNIT_FLAG_IMMUNE_TO_PC = 0x00000100
UNIT_FLAG_PACIFIED = 0x00020000
UNIT_FLAG_UNINTERACTIBLE = 0x02000000
# Rucksack-Slots des Spielers (16 Slots, je GUID aus zwei Feldern)
FIELD_PLAYER_PACK_SLOT_1 = 370
# Erfahrung (UpdateFields.h: UNIT_END 148 + 0x1E6 / 0x1E7)
FIELD_PLAYER_XP = 634
FIELD_PLAYER_NEXT_LEVEL_XP = 635
# Spielerflags (UpdateFields.h: UNIT_END + 0x0002), PLAYER_FLAGS_GHOST aus Player.h
FIELD_PLAYER_FLAGS = 150
PLAYER_FLAGS_GHOST = 0x00000010
# Leichnam (Corpse): CORPSE_FIELD_OWNER = OBJECT_END + 0x00, zwei Felder (LONG).
# Darueber laesst sich der eigene Leichnam eindeutig erkennen.
FIELD_CORPSE_OWNER_LOW = 0x06
FIELD_CORPSE_OWNER_HIGH = 0x07

# SMSG_AUTH_RESPONSE-Ergebniscodes (WorldResult)
WORLD_RESULT_NAMES = {
    0x00: "RESPONSE_SUCCESS",
    0x01: "RESPONSE_FAILURE",
    0x0D: "AUTH_FAILED",
    0x0E: "AUTH_REJECT",
    0x0F: "AUTH_BAD_SERVER_PROOF",
    0x14: "AUTH_VERSION_MISMATCH",
    0x15: "AUTH_UNKNOWN_ACCOUNT",
    0x16: "AUTH_INCORRECT_PASSWORD",
    0x17: "AUTH_SESSION_EXPIRED",
    0x18: "AUTH_SERVER_SHUTTING_DOWN",
    0x19: "AUTH_ALREADY_LOGGING_IN",
    0x1B: "AUTH_WAIT_QUEUE",
    0x1C: "AUTH_BANNED",
    0x1D: "AUTH_ALREADY_ONLINE",
    0x1E: "AUTH_NO_TIME",
    0x1F: "AUTH_DB_BUSY",
    0x20: "AUTH_SUSPENDED",
}
AUTH_OK = 0x0C

# Login-Fehlercodes (AuthResult aus TrinityCore AuthCodes.h)
LOGON_RESULT_NAMES = {
    0x00: "Erfolg",
    0x03: "Account ist gebannt",
    0x04: "Account existiert nicht",
    0x05: "Falsches Passwort",
    0x06: "Account ist bereits eingeloggt",
    0x07: "Keine Spielzeit mehr vorhanden",
    0x08: "Datenbank beschäftigt",
    0x09: "Falsche Client-Version",
    0x0A: "Client-Update erforderlich",
    0x0B: "Ungültiger Server",
    0x0C: "Account ist gesperrt",
    0x0D: "Kein Zugriff",
    0x0F: "Parental Control aktiv",
    0x10: "Account ist an eine IP gebunden",
    0x11: "Testphase beendet",
    0x12: "Bitte über Battle.net anmelden",
    0x14: "Account abgelaufen",
    0x18: "Account ist gesperrt",
    0x20: "Account-Umstellung erforderlich",
}

# Loot-Slot-Typen (CMSG_AUTOSTORE_LOOT_ITEM funktioniert für alle ausser 8)
LOOT_SLOT_MONEY = 8

# Nur echte Opcode-Namen aufnehmen. Feld-Indizes und Typ-IDs haben dieselben
# Zahlen und würden die Namen sonst verfälschen (z. B. 0x37 = CMSG_CHAR_ENUM).
NAMES = {
    value: name
    for name, value in list(globals().items())
    if isinstance(value, int)
    and name.startswith(("CMSG_", "SMSG_", "MSG_", "CMD_"))
}


def opcode_name(opcode: int) -> str:
    return NAMES.get(opcode, f"0x{opcode:04X}")
