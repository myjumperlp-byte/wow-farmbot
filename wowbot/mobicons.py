"""Mob-Symbole aus dem Client (Interface/Icons/*.blp).

Der Server schickt nur Name, Stufe und Leben einer Kreatur - ein Portrait wie
im Spiel steckt nicht in den Daten (das ist ein 3D-Render des Modells). Damit
im Tooltip trotzdem ein Bild steht, holt dieses Modul ein passendes Symbol aus
dem Client: die Kreaturenart wird aus dem Namen erraten (Wolf, Spinne, Skelett
...) und dafür ein Icon aus dem Archiv gelesen, einmal entpackt und als PNG
zwischengespeichert.

Fehlt der Client oder das Symbol, ist das kein Fehler: die Oberfläche zeichnet
dann ein Medaillon mit den Anfangsbuchstaben.
"""

from __future__ import annotations

import threading
from pathlib import Path

from wowbot.blp import BLPError, decode_blp

# Reihenfolge zählt: das erste passende Stichwort gewinnt. „Moderknochen-
# marodeur“ ist damit untot (moder/knochen) und nicht menschlich (marodeur).
FAMILIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "untot",
        (
            "skelett", "skelet", "knochen", "moder", "untot", "zombie", "ghul",
            "gruft", "kadaver", "verwes", "gespenst", "gespens", "geist", "schemen",
            "seel", "schatten", "banshee", "leiche", "grab", "bone", "undead",
            "ghost", "spirit", "wight", "skeleton", "flesh",
        ),
        ("INV_Misc_Bone_01", "Spell_Shadow_RaiseDead", "INV_Misc_Head_Undead_01"),
    ),
    (
        "drachenfalke",
        ("drachenfalke", "dragonhawk", "falke", "falcon", "hawk"),
        ("Ability_Hunter_Pet_Dragonhawk", "INV_Misc_Head_Dragon_01"),
    ),
    (
        "drache",
        (
            "drache", "drachen", "dragon", "wyrm", "welpe", "whelp", "drakonid",
            "wyvern", "windschlange", "serpent", "schlange", "naga",
        ),
        ("INV_Misc_Head_Dragon_01", "Ability_Hunter_Pet_WindSerpent"),
    ),
    (
        "wolf",
        ("wolf", "warg", "worg", "hund", "köter", "koter", "hyäne", "hyane",
         "hound", "hyena", "jackal", "schakal"),
        ("Ability_Hunter_Pet_Wolf", "Ability_Hunter_Pet_Hyena"),
    ),
    (
        "bär",
        ("bär", "baer", "bear", "ursa"),
        ("Ability_Hunter_Pet_Bear",),
    ),
    (
        "spinne",
        ("spinne", "spider", "weber", "netz", "skitter"),
        ("Ability_Hunter_Pet_Spider",),
    ),
    (
        "eber",
        ("eber", "keiler", "boar", "schwein", "sau", "borste"),
        ("Ability_Hunter_Pet_Boar",),
    ),
    (
        "raubkatze",
        ("katze", "löwe", "loewe", "tiger", "panther", "säbler", "saebler",
         "lion", "cat", "sabre", "lynx", "luchs"),
        ("Ability_Hunter_Pet_Cat", "Ability_Druid_CatForm"),
    ),
    (
        "raptor",
        ("raptor", "saurier", "saurus", "echse", "kroko", "crocolisk",
         "krokilisk", "kriech"),
        ("Ability_Hunter_Pet_Raptor", "Ability_Hunter_Pet_Crocolisk"),
    ),
    (
        "fledermaus",
        ("fledermaus", "bat", "vampir"),
        ("Ability_Hunter_Pet_Bat",),
    ),
    (
        "dämon",
        ("dämon", "daemon", "demon", "teufel", "höll", "hoell", "satyr", "fel",
         "imp", "succubus", "schrecken", "wichtel", "teufl"),
        ("Spell_Shadow_SummonFelguard", "Spell_Shadow_SummonImp"),
    ),
    (
        "elementar",
        ("elementar", "golem", "kristall", "wirbel", "sturm", "flamme", "feuer",
         "wasser", "erde", "frost", "blitz", "mote"),
        ("INV_Misc_Orb_03", "Spell_Fire_Fireball02"),
    ),
    (
        "humanoid",
        ("mensch", "marodeur", "räuber", "raeuber", "bandit", "kultist", "ketzer",
         "magier", "hexer", "schamane", "krieger", "wache", "soldat", "söldner",
         "soeldner", "arbeiter", "bauer", "pirat", "forscher", "priester",
         "zauberer", "hexenmeister", "humanoid", "kult", "menschlich"),
        ("INV_Misc_Head_Human_01", "Ability_Hunter_Pet_Gorilla"),
    ),
    (
        "bestie",
        ("tier", "bestie", "beast", "kriecher", "wild", "klaue", "zahn"),
        ("Ability_Hunter_Pet_Cat",),
    ),
)

DEFAULT_FAMILY = "unbekannt"
DEFAULT_ICONS: tuple[str, ...] = ("INV_Misc_QuestionMark",)
# Freundliche NPCs (Händler, Questgeber), deren Name keine Tierart verrät:
# eine Notiz statt des Fragezeichens - das liest sich als "redet mit dir".
NPC_FAMILY = "npc"
NPC_ICONS: tuple[str, ...] = ("INV_Misc_Note_01", "INV_Misc_Book_09")


def family_for(name: str) -> str:
    """Kreaturenart aus dem Namen raten (deutsch und englisch)."""
    lowered = (name or "").lower()
    for key, keywords, _icons in FAMILIES:
        if any(word in lowered for word in keywords):
            return key
    return DEFAULT_FAMILY


def icons_for(family: str) -> tuple[str, ...]:
    """Symbol-Kandidaten einer Art - der erste vorhandene gewinnt."""
    if family == NPC_FAMILY:
        return NPC_ICONS
    for key, _keywords, icons in FAMILIES:
        if key == family:
            return icons
    return DEFAULT_ICONS


def icon_names() -> tuple[str, ...]:
    """Alle Symbole, die dieses Modul je braucht (für Tests)."""
    names: list[str] = []
    for _key, _keywords, icons in FAMILIES:
        names.extend(icons)
    names.extend(DEFAULT_ICONS)
    names.extend(NPC_ICONS)
    return tuple(dict.fromkeys(names))


class MobIcons:
    """Holt Symbole aus dem Client und legt sie als PNG im Zwischenspeicher ab.

    Wird aus dem Karten-Thread benutzt; alle Zugriffe sind serialisiert, damit
    zwei gleichzeitige Anfragen nicht dasselbe Archiv doppelt öffnen.
    """

    def __init__(self, client_dir: "str | Path | None", cache_dir: Path) -> None:
        self.client_dir = Path(client_dir) if client_dir else None
        self.cache_dir = Path(cache_dir) / "icons"
        self.error: "str | None" = None
        self._maps = None
        self._by_family: dict[str, "Path | None"] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ Suche
    def icon_for(self, name: str, npc: bool = False) -> "Path | None":
        """PNG-Pfad zum Symbol dieser Kreatur (oder None).

        `npc` = freundliche Einheit: verrät der Name keine Kreaturenart, gibt
        es eine Notiz statt des Fragezeichens.
        """
        family = family_for(name)
        if npc and family == DEFAULT_FAMILY:
            family = NPC_FAMILY
        with self._lock:
            if family in self._by_family:
                return self._by_family[family]
            path = self._resolve(family)
            self._by_family[family] = path
            return path

    def _resolve(self, family: str) -> "Path | None":
        for icon in icons_for(family):
            cached = self.cache_dir / f"{icon}.png"
            if cached.exists():
                return cached
            data = self._read_icon(icon)
            if not data:
                continue
            try:
                image = decode_blp(data).convert("RGBA")
            except (BLPError, OSError, ValueError):
                continue
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temp = cached.with_name(f".{icon}.tmp")
                image.save(temp, "PNG")
                temp.replace(cached)
            except OSError as exc:
                self.error = str(exc)
                return None
            return cached
        return None

    def _read_icon(self, icon: str) -> "bytes | None":
        # Lose Dateien im Client-Ordner haben Vorrang vor dem Archiv.
        if self.client_dir is not None:
            for folder in ("Icons", "ICONS", "icons"):
                loose = self.client_dir / "Interface" / folder / f"{icon}.blp"
                try:
                    if loose.is_file():
                        return loose.read_bytes()
                except OSError:
                    break
        maps = self._open()
        if maps is None:
            return None
        return maps.read(f"Interface\\ICONS\\{icon}.blp")

    def _open(self):
        if self._maps is not None:
            return self._maps if getattr(self._maps, "ready", False) else None
        if self.client_dir is None:
            return None
        from wowbot.mapdata import ClientMaps  # noqa: PLC0415 - hält den Import klein

        maps = ClientMaps(self.client_dir, self.cache_dir.parent / "cache")
        if not maps.open():
            self.error = maps.error
            self._maps = maps
            return None
        self._maps = maps
        return maps
