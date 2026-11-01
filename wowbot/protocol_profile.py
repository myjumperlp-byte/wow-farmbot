"""Anpassung an andere WoW-Versionen: Build, Opcodes, Feld-Indizes, Flags.

Der Bot ist auf 3.3.5a (Build 12340) gebaut - jede Zahl dafür steht in
`wowbot/opcodes.py`. Andere Versionen und fremde Serverkerne zählen teilweise
anders. Dieser Baustein überschreibt die Zahlen zur Laufzeit mit dem, was in der
Config unter `protocol` steht:

    "protocol": {
      "version": "3.3.5",                          # Kennung im Login
      "build": 12340,                              # Build im Login und in CMSG_AUTH_SESSION
      "os": "Win",                                 # "Win" oder "Mac"
      "platform": "x86",                           # "x86" oder "x64"
      "opcodes": { "SMSG_UPDATE_OBJECT": 169 },    # CMSG_/SMSG_/MSG_/CMD_
      "fields":  { "FIELD_UNIT_HEALTH": 24 },      # FIELD_...
      "flags":   { "UNIT_FLAG_NON_ATTACKABLE": 2 } # alles andere (UNIT_FLAG_, POWER_ ...)
    }

Angepasst werden **Zahlen**, nicht der Aufbau der Pakete: eine andere
Protokollgeneration (etwa 4.x) tauscht zusätzlich Felder und Paketinhalte aus
und braucht echte Code-Änderungen. Für 3.3.5a-Kerne mit abweichenden Nummern
(z. B. eigene Opcode-Tabellen) reicht diese Einstellung.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import auth, opcodes, world
from .i18n import tr

log = logging.getLogger("wowbot.proto")

OPCODE_PREFIXES = ("CMSG_", "SMSG_", "MSG_", "CMD_")
FIELD_PREFIX = "FIELD_"

DEFAULT_VERSION = "3.3.5"
DEFAULT_BUILD = 12340
DEFAULT_OS = "Win"
DEFAULT_PLATFORM = "x86"


def _numeric_constants() -> "dict[str, int]":
    """Alle ganzzahligen Konstanten der Opcode-Tabelle (Stand: 3.3.5a)."""
    return {
        name: value
        for name, value in vars(opcodes).items()
        if isinstance(value, int) and not isinstance(value, bool) and name.isupper()
    }


BASELINE: "dict[str, int]" = _numeric_constants()


def group_of(name: str) -> str:
    """In welche Gruppe der Config ein Name gehört."""
    if name.startswith(OPCODE_PREFIXES):
        return "opcodes"
    if name.startswith(FIELD_PREFIX):
        return "fields"
    return "flags"


def template() -> dict:
    """Alle anpassbaren Werte als Datei zum Bearbeiten."""
    groups: "dict[str, dict[str, int]]" = {"opcodes": {}, "fields": {}, "flags": {}}
    for name, value in BASELINE.items():
        groups[group_of(name)][name] = value
    return {
        "version": DEFAULT_VERSION,
        "build": DEFAULT_BUILD,
        "os": DEFAULT_OS,
        "platform": DEFAULT_PLATFORM,
        **groups,
    }


@dataclass
class Profile:
    """Was der Bot über die Gegenstelle glaubt (nach dem Einlesen der Config)."""

    version: str = DEFAULT_VERSION
    build: int = DEFAULT_BUILD
    os: str = DEFAULT_OS
    platform: str = DEFAULT_PLATFORM
    opcodes: "dict[str, int]" = field(default_factory=dict)
    fields: "dict[str, int]" = field(default_factory=dict)
    flags: "dict[str, int]" = field(default_factory=dict)
    warnings: "list[str]" = field(default_factory=list)

    @property
    def overrides(self) -> int:
        return len(self.opcodes) + len(self.fields) + len(self.flags)

    def summary(self) -> str:
        """Eine Zeile für Log und Fenster."""
        head = f"{self.version} · Build {self.build}"
        if not self.overrides:
            return f"{head} · {tr('keine eigenen Werte (3.3.5a)')}"
        parts = [head]
        for label, values in (
            ("{count} eigene Opcode(s)", self.opcodes),
            ("{count} eigene Feld(er)", self.fields),
            ("{count} eigene Flag(s)", self.flags),
        ):
            if values:
                parts.append(tr(label, count=len(values)))
        return " · ".join(parts)


def version_bytes(version: str) -> "tuple[int, int, int]":
    """„3.3.5a“ -> (3, 3, 5) für das Login-Paket."""
    numbers = [int(part) for part in re.findall(r"\d+", str(version))][:3]
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def _short(value, fallback: str, length: int) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return text[:length]


def _check(name: str, group: str) -> "str | None":
    if name not in BASELINE:
        return f"'{name}' ist kein bekannter Name - ignoriert"
    expected = group_of(name)
    if expected != group:
        return f"'{name}' gehört in '{expected}', nicht in '{group}' - ignoriert"
    return None


def _read_group(section: dict, group: str) -> "dict[str, int]":
    rows = section.get(group)
    values: "dict[str, int]" = {}
    if not isinstance(rows, dict):
        return values
    for raw_name, raw_value in rows.items():
        name = str(raw_name or "").strip().upper()
        if not name:
            continue
        problem = _check(name, group)
        if problem:
            log.warning("[proto] %s", problem)
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            log.warning("[proto] '%s' hat keinen Zahlenwert (%r) - ignoriert", name, raw_value)
            continue
        if not 0 <= value <= 0xFFFFFFFF:
            log.warning("[proto] '%s' = %d passt nicht in 32 Bit - ignoriert", name, value)
            continue
        values[name] = value
    return values


def parse(section) -> Profile:
    """Config-Abschnitt `protocol` prüfen; unbrauchbare Werte fallen auf 3.3.5a zurück."""
    section = section if isinstance(section, dict) else {}
    profile = Profile()

    raw_version = str(section.get("version") or DEFAULT_VERSION).strip()
    if len(re.findall(r"\d+", raw_version)) < 2:
        profile.warnings.append(
            f"Version '{raw_version}' ist unbrauchbar - nehme {DEFAULT_VERSION}"
        )
        profile.version = DEFAULT_VERSION
    else:
        profile.version = ".".join(str(part) for part in version_bytes(raw_version))

    try:
        build = int(section.get("build", DEFAULT_BUILD))
    except (TypeError, ValueError):
        build = 0
    if not 1 <= build <= 99999:
        profile.warnings.append(
            f"Build '{section.get('build')}' ist unbrauchbar - nehme {DEFAULT_BUILD}"
        )
        build = DEFAULT_BUILD
    profile.build = build

    profile.os = _short(section.get("os"), DEFAULT_OS, 4)
    profile.platform = _short(section.get("platform"), DEFAULT_PLATFORM, 4)
    profile.opcodes = _read_group(section, "opcodes")
    profile.fields = _read_group(section, "fields")
    profile.flags = _read_group(section, "flags")
    return profile


def apply(profile: Profile) -> Profile:
    """Zahlen setzen - vorher alles auf 3.3.5a zurück (mehrfach aufrufbar)."""
    for name, value in BASELINE.items():
        setattr(opcodes, name, value)
    for name, value in {**profile.opcodes, **profile.fields, **profile.flags}.items():
        setattr(opcodes, name, value)
    # Namenstabelle neu aufbauen, sonst zeigt das Log weiter die alten Opcodes.
    opcodes.NAMES = {
        value: name
        for name, value in vars(opcodes).items()
        if isinstance(value, int) and name.startswith(OPCODE_PREFIXES)
    }
    auth.CLIENT_BUILD = profile.build
    auth.CLIENT_VERSION = version_bytes(profile.version)
    auth.CLIENT_OS = profile.os
    auth.CLIENT_PLATFORM = profile.platform
    world.CLIENT_BUILD = profile.build
    return profile


def apply_config(config: "dict | None") -> Profile:
    """Aus der Config lesen und anwenden; eine Zeile ins Log."""
    profile = parse((config or {}).get("protocol"))
    for warning in profile.warnings:
        log.warning("[proto] %s", warning)
    apply(profile)
    log.info("[proto] %s", profile.summary())
    return profile
