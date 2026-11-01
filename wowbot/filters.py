"""Ziel- und Beuteregeln aus dem Reiter „Ziele & Beute".

Alles, was der Bot angreift oder aufhebt, laeuft durch diese zwei Regelsaetze.
Sie werden einmal beim Start aus `behavior` gebaut und sind danach unveraenderlich
(eine Aenderung im Fenster wirkt nach dem naechsten Start).

Aufbau in der Config::

    "mob_filter_mode": "all",            # all = alle, only = nur markierte
    "mob_rules": [                       # 1:1 die Tabelle aus dem Fenster
        {"entry": 15648, "name": "Managespenst", "level": 6, "attack": true},
        {"entry": 0,     "name": "Drache",                    "attack": false},
    ],
    "mob_level_mode": "all",             # all|same|same_or_higher|same_or_lower|range
    "mob_level_spread": 2,               # nur fuer "range"
    "loot_mode": "all",                  # all = alles, only = nur markierte
    "loot_rules": [{"entry": 2589, "name": "Leinenstoff", "keep": false}],
    "item_names": {"2589": "Leinenstoff"},
"""

from __future__ import annotations

from dataclasses import dataclass

MOB_LEVEL_MODES = ("all", "same", "same_or_higher", "same_or_lower", "range")

MOB_LEVEL_LABELS = {
    "all": "alle Stufen",
    "same": "nur gleiche Stufe",
    "same_or_higher": "gleiche Stufe und höher",
    "same_or_lower": "gleiche Stufe und niedriger",
    "range": "eigene Stufe ± N",
}


# ------------------------------------------------------------------ Hilfsmittel
def _entries(values) -> frozenset[int]:
    """Entries als Menge - kaputte Werte werden still uebersprungen."""
    result = set()
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.add(number)
    return frozenset(result)


def _patterns(values) -> tuple[str, ...]:
    """Namensmuster: Teilstueck, Gross-/Kleinschreibung egal."""
    result: list[str] = []
    for value in values or []:
        text = str(value).strip().lower()
        if text:
            result.append(text)
    return tuple(result)


def _flag(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "nein", "no", "")
    return bool(value)


def name_matches(patterns: tuple[str, ...], name: "str | None") -> bool:
    """Steckt eines der Muster im Namen?"""
    if not patterns or not name:
        return False
    lowered = str(name).lower()
    return any(pattern in lowered for pattern in patterns)


def _rows(behavior: dict, key: str) -> "list[dict] | None":
    value = behavior.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


# ------------------------------------------------------------------------ Mobs
@dataclass(frozen=True)
class MobRules:
    """Wen der Bot angreift."""

    mode: str = "all"
    entries: frozenset[int] = frozenset()
    patterns: tuple[str, ...] = ()
    ignore_entries: frozenset[int] = frozenset()
    ignore_patterns: tuple[str, ...] = ()
    level_mode: str = "all"
    level_spread: int = 2

    @classmethod
    def from_behavior(cls, behavior: "dict | None") -> "MobRules":
        behavior = behavior or {}
        rows = _rows(behavior, "mob_rules")
        if rows is not None:
            # Fehlt "attack", gilt der Eintrag als Ziel - ein handgeschriebener
            # Eintrag soll nicht stillschweigend zum Ignorieren werden.
            attacked = [row for row in rows if row.get("attack", True)]
            ignored = [row for row in rows if not row.get("attack", True)]
            entries = {row["entry"] for row in attacked if row.get("entry")}
            names = [row.get("name") for row in attacked if not row.get("entry")]
            ignore_entries = {row["entry"] for row in ignored if row.get("entry")}
            ignore_names = [row.get("name") for row in ignored if not row.get("entry")]
        else:
            # Alte Configs ohne Tabelle: mob_entries/ignore_entries gelten weiter.
            entries = set(_entries(behavior.get("mob_entries")))
            names = list(behavior.get("mob_names") or [])
            ignore_entries = set(_entries(behavior.get("ignore_entries")))
            ignore_names = list(behavior.get("ignore_names") or [])

        mode = str(behavior.get("mob_filter_mode") or "").strip().lower()
        if mode not in ("all", "only"):
            mode = "only" if (entries or _patterns(names)) else "all"
        if mode == "all":
            # Im Modus „alle“ entscheidet nur die Ignorierliste - die markierten
            # Einträge sind bedeutungslos und sollen kein Regel-Update auslösen.
            entries = set()
            names = []

        level_mode = str(behavior.get("mob_level_mode") or "all").strip().lower()
        if level_mode not in MOB_LEVEL_MODES:
            level_mode = "all"
        try:
            spread = int(behavior.get("mob_level_spread", 2))
        except (TypeError, ValueError):
            spread = 2

        return cls(
            mode=mode,
            entries=frozenset(entries),
            patterns=_patterns(names),
            ignore_entries=frozenset(ignore_entries),
            ignore_patterns=_patterns(ignore_names),
            level_mode=level_mode,
            level_spread=max(0, min(10, spread)),
        )

    def level_allows(self, level: int, own_level: int) -> bool:
        """Stufenfilter. Unbekannte Stufen (0 / -1 = „??“) bleiben erlaubt."""
        if self.level_mode == "all":
            return True
        if level is None or level <= 0 or not own_level or own_level <= 0:
            return True
        if self.level_mode == "same":
            return level == own_level
        if self.level_mode == "same_or_higher":
            return level >= own_level
        if self.level_mode == "same_or_lower":
            return level <= own_level
        return abs(level - own_level) <= self.level_spread

    def allows(self, entry: int, name: "str | None", level: int, own_level: int) -> bool:
        if entry and entry in self.ignore_entries:
            return False
        if name_matches(self.ignore_patterns, name):
            return False
        if self.mode == "only":
            by_entry = bool(entry) and entry in self.entries
            if not by_entry and not name_matches(self.patterns, name):
                return False
        return self.level_allows(level, own_level)

    def summary(self, own_level: int = 0) -> str:
        if self.mode == "only":
            count = len(self.entries) + len(self.patterns)
            text = f"nur {count} markierte(r) Mob(s)"
        else:
            text = "alle angreifbaren Mobs"
        ignored = len(self.ignore_entries) + len(self.ignore_patterns)
        if ignored:
            text += f", {ignored} ignoriert"
        level = MOB_LEVEL_LABELS.get(self.level_mode, self.level_mode)
        if self.level_mode == "range":
            level = f"Stufe {own_level}±{self.level_spread}"
        return f"{text}, Stufe: {level}"


# ----------------------------------------------------------------------- Beute
@dataclass(frozen=True)
class LootRules:
    """Was der Bot von einer Leiche mitnimmt."""

    enabled: bool = True
    mode: str = "all"
    entries: frozenset[int] = frozenset()
    patterns: tuple[str, ...] = ()
    skip_entries: frozenset[int] = frozenset()
    skip_patterns: tuple[str, ...] = ()

    @classmethod
    def from_behavior(cls, behavior: "dict | None") -> "LootRules":
        behavior = behavior or {}
        rows = _rows(behavior, "loot_rules")
        if rows is not None:
            kept = [row for row in rows if row.get("keep", True)]
            skipped = [row for row in rows if not row.get("keep", True)]
            keep_entries = {row["entry"] for row in kept if row.get("entry")}
            keep_names = [row.get("name") for row in kept if not row.get("entry")]
            skip_entries = {row["entry"] for row in skipped if row.get("entry")}
            skip_names = [row.get("name") for row in skipped if not row.get("entry")]
        else:
            keep_entries = set(_entries(behavior.get("loot_entries")))
            keep_names = list(behavior.get("loot_names") or [])
            skip_entries = set(_entries(behavior.get("loot_skip_entries")))
            skip_names = list(behavior.get("loot_skip_names") or [])

        mode = str(behavior.get("loot_mode") or "").strip().lower()
        if mode not in ("all", "only"):
            mode = "only" if (keep_entries or _patterns(keep_names)) else "all"
        if mode == "all":
            keep_entries = set()
            keep_names = []

        return cls(
            enabled=_flag(behavior.get("loot"), True),
            mode=mode,
            entries=frozenset(keep_entries),
            patterns=_patterns(keep_names),
            skip_entries=frozenset(skip_entries),
            skip_patterns=_patterns(skip_names),
        )

    @property
    def needs_names(self) -> bool:
        """Nur wenn nach Namen gefiltert wird, muss der Item-Name bekannt sein."""
        return bool(self.patterns or self.skip_patterns)

    def wants(self, entry: int, name: "str | None" = None) -> bool:
        if not self.enabled:
            return False
        if entry and entry in self.skip_entries:
            return False
        if name_matches(self.skip_patterns, name):
            return False
        if self.mode == "only":
            by_entry = bool(entry) and entry in self.entries
            return by_entry or name_matches(self.patterns, name)
        return True

    def summary(self) -> str:
        if not self.enabled:
            return "aus"
        if self.mode == "only":
            count = len(self.entries) + len(self.patterns)
            text = f"nur {count} markierte(r) Gegenstand/Gegenstände"
        else:
            text = "alles"
        skipped = len(self.skip_entries) + len(self.skip_patterns)
        if skipped:
            text += f", {skipped} nicht aufheben"
        return text
