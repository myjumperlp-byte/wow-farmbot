"""Echte Karten-Texturen aus dem WoW-Client (3.3.5a).

Der Server schickt keine Kartendaten, der Client hat sie aber: unter
`Data/<locale>/locale-xxXX.MPQ` liegen die Weltkarten als BLP-Kacheln
(`Interface\\WorldMap\\<Zone>\\<Zone>1..12.blp`) und in
`DBFilesClient\\WorldMapArea.dbc` steht für jede Zone, welcher Weltausschnitt
auf dem Bild zu sehen ist.

Dieses Modul setzt daraus ein Bild pro Zone zusammen und rechnet zwischen
Weltkoordinaten und Bildpunkten um. Alles wird einmal gebaut und als PNG
zwischengespeichert, damit der Bot nicht bei jedem Start die MPQs liest.
"""

from __future__ import annotations

import json
import math
import os
import struct
import threading
from pathlib import Path

from PIL import Image

from wowbot.blp import BLPError, decode_blp

TILE = 256
CACHE_VERSION = 3  # 3: BLP-Palette korrekt gelesen (palettierte Zonen wie Elwynn)
COLUMN_CANDIDATES = (4, 3, 2, 5, 6, 8)
# Anteil heller Bildpunkte, ab dem eine Zeile/Spalte noch als "bemalt" gilt
PAINT_SHARE = 0.15
PAINT_LEVEL = 24


class MapError(Exception):
    """Kartendaten nicht lesbar (Client fehlt, MPQ defekt, ...)."""


# ----------------------------------------------------------------------- Zone
class Zone:
    """Ein Eintrag aus WorldMapArea.dbc: Name, Karte und Weltausschnitt."""

    __slots__ = ("name", "label", "map_id", "area_id", "left", "right", "top", "bottom")

    def __init__(
        self,
        name: str,
        map_id: int,
        area_id: int,
        left: float,
        right: float,
        top: float,
        bottom: float,
    ) -> None:
        self.name = name
        self.label = name
        self.map_id = map_id
        self.area_id = area_id
        self.left = left
        self.right = right
        self.top = top
        self.bottom = bottom

    # -- Geometrie
    @property
    def width(self) -> float:
        return abs(self.left - self.right)

    @property
    def height(self) -> float:
        return abs(self.top - self.bottom)

    @property
    def area(self) -> float:
        return self.width * self.height

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        """Liegt die Position in der Zone?

        Die Kartenkanten aus WorldMapArea liefern **y** (links/rechts, West-Ost),
        **x** kommt aus oben/unten (Nord-Süd) - so ist die Weltkarte nordorientiert.
        """
        dx = self.height * margin
        dy = self.width * margin
        return (
            min(self.top, self.bottom) - dx <= x <= max(self.top, self.bottom) + dx
            and min(self.left, self.right) - dy <= y <= max(self.left, self.right) + dy
        )

    def center_distance(self, x: float, y: float) -> float:
        cx = (self.top + self.bottom) / 2.0
        cy = (self.left + self.right) / 2.0
        return math.hypot(x - cx, y - cy)

    def to_pixel(self, x: float, y: float, image_width: int, image_height: int):
        """Weltkoordinate -> Bildpunkt (0..width, 0..height)."""
        span_y = self.left - self.right
        span_x = self.top - self.bottom
        if not span_x or not span_y:
            return None
        u = (self.left - y) / span_y * image_width
        v = (self.top - x) / span_x * image_height
        return u, v

    def key(self) -> str:
        return f"{self.map_id}-{self.name.lower()}"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "map": self.map_id,
            "area": self.area_id,
            "left": self.left,
            "right": self.right,
            "top": self.top,
            "bottom": self.bottom,
        }

    def __repr__(self) -> str:  # pragma: no cover - nur für Logs
        return f"Zone({self.name}, map {self.map_id})"


class MapTexture:
    """Fertiges Zonenbild plus Umrechnung Welt <-> Bildpunkt."""

    __slots__ = ("zone", "path", "width", "height")

    def __init__(self, zone: Zone, path: Path, width: int, height: int) -> None:
        self.zone = zone
        self.path = path
        self.width = width
        self.height = height

    def to_pixel(self, x: float, y: float):
        return self.zone.to_pixel(x, y, self.width, self.height)

    def view(self) -> dict:
        """Alles, was die Oberfläche zum Zeichnen braucht."""
        data = self.zone.as_dict()
        data.update({"path": str(self.path), "width": self.width, "height": self.height})
        return data


# ------------------------------------------------------------------ Client-Ordner
def _looks_like_client(path: Path) -> bool:
    data = path / "Data"
    if not data.is_dir():
        return False
    return any(child.suffix.lower() == ".mpq" for child in data.glob("*.MPQ"))


def find_client_dir(hint: "str | Path | None" = None) -> "Path | None":
    """Client-Ordner suchen: erst der Hinweis, dann übliche Orte."""
    if hint:
        candidate = Path(hint).expanduser()
        if _looks_like_client(candidate):
            return candidate
        # Vielleicht wurde der Data-Ordner selbst gewählt
        if candidate.name.lower() == "data" and _looks_like_client(candidate.parent):
            return candidate.parent
        return None

    guesses: list[Path] = []
    for pattern in (
        "Applications/World of Warcraft*",
        "Applications/Games/World of Warcraft*",
        "Downloads/WorldOfWarcraft*",
        "Downloads/World of Warcraft*",
        "Games/World of Warcraft*",
        "World of Warcraft*",
    ):
        guesses.extend(sorted(Path.home().glob(pattern)))
    for guess in guesses:
        for depth in (guess, *sorted(guess.glob("*"))[:8]):
            if depth.is_dir() and _looks_like_client(depth):
                return depth
    return None


# ------------------------------------------------------------------ MPQ + DBC
def _archive_rank(path: Path) -> int:
    name = path.name.lower()
    if "speech" in name or name.startswith("backup"):
        return 99
    if name.startswith("patch"):
        return 0
    if name.startswith("locale") or "locale-" in name:
        return 1
    if name.startswith(("expansion", "lichking")):
        return 2
    if name.startswith("common"):
        return 3
    return 4


class _Dbc:
    """Minimaler WDBC-Leser (Kopf, Datensätze, Zeichenblock)."""

    def __init__(self, data: bytes) -> None:
        magic, records, fields, size, string_size = struct.unpack_from("<4s4I", data, 0)
        if magic != b"WDBC":
            raise MapError("DBC hat einen unbekannten Kopf")
        self.records = records
        self.fields = fields
        self.size = size
        self.body = 20
        self.raw = data
        self.strings = data[self.body + records * size: self.body + records * size + string_size]

    def row(self, index: int) -> tuple:
        return struct.unpack_from(f"<{self.fields}I", self.raw, self.body + index * self.size)

    def text(self, offset: int) -> str:
        if not offset or offset >= len(self.strings):
            return ""
        end = self.strings.find(b"\0", offset)
        if end < 0:
            end = len(self.strings)
        return self.strings[offset:end].decode("utf-8", "replace")


def _as_float(value: int) -> float:
    return struct.unpack("<f", struct.pack("<I", value))[0]


class ClientMaps:
    """Weltkarten aus dem Client lesen, zusammensetzen und zwischenspeichern."""

    def __init__(self, client_dir: "str | Path | None", cache_dir: Path) -> None:
        self.client_dir = Path(client_dir) if client_dir else None
        self.cache_dir = Path(cache_dir)
        self.error: "str | None" = None
        self._archives: list = []
        self._archive_of: dict[str, object] = {}
        self._zones: list[Zone] = []
        self._area_labels: dict[int, str] = {}
        self._map_dirs: dict[int, str] = {}
        self._ready = False
        self._lock = threading.Lock()

    # -- Aufbau
    @property
    def ready(self) -> bool:
        return self._ready

    def open(self) -> bool:
        """Archive und DBCs einlesen. Gibt False zurück, wenn es nicht geht."""
        with self._lock:
            if self._ready:
                return True
            try:
                self._open()
                self._ready = True
                self.error = None
            except (MapError, OSError, ValueError, struct.error) as exc:
                self.error = str(exc)
                self._ready = False
            return self._ready

    def _open(self) -> None:
        if self.client_dir is None:
            raise MapError("kein Client-Ordner eingestellt")
        if not _looks_like_client(self.client_dir):
            raise MapError(f"kein WoW-Client in {self.client_dir}")
        try:
            import mpyq  # noqa: PLC0415 - nur nötig, wenn Karten gebraucht werden
        except ImportError as exc:  # pragma: no cover - Paket fehlt
            raise MapError("Python-Paket mpyq fehlt (pip install mpyq)") from exc

        data_dir = self.client_dir / "Data"
        files = list(data_dir.glob("*.MPQ")) + list(data_dir.glob("*/*.MPQ"))
        files = [f for f in files if _archive_rank(f) < 99]
        if not files:
            raise MapError("keine MPQ-Archive im Data-Ordner")
        files.sort(key=lambda f: (_archive_rank(f), f.name.lower()))
        for path in files:
            try:
                self._archives.append(mpyq.MPQArchive(str(path), listfile=False))
            except Exception:  # noqa: BLE001 - kaputtes Archiv überspringen
                continue
        if not self._archives:
            raise MapError("kein MPQ-Archiv lesbar")

        world_map = self._dbc("WorldMapArea.dbc")
        for index in range(world_map.records):
            row = world_map.row(index)
            if len(row) < 8:
                continue
            name = world_map.text(row[3])
            if not name:
                continue
            self._zones.append(
                Zone(
                    name=name,
                    map_id=row[1],
                    area_id=row[2],
                    left=_as_float(row[4]),
                    right=_as_float(row[5]),
                    top=_as_float(row[6]),
                    bottom=_as_float(row[7]),
                )
            )
        if not self._zones:
            raise MapError("WorldMapArea.dbc enthält keine Zonen")

        self._load_area_labels()
        for zone in self._zones:
            if zone.area_id in self._area_labels:
                zone.label = self._area_labels[zone.area_id]

    def _dbc(self, name: str) -> _Dbc:
        data = self._read(f"DBFilesClient\\{name}")
        if not data:
            raise MapError(f"{name} fehlt im Client")
        return _Dbc(data)

    def _load_area_labels(self) -> None:
        """Deutsche Zonennamen aus AreaTable (nur für die Anzeige)."""
        try:
            area_table = self._dbc("AreaTable.dbc")
        except MapError:
            return
        for index in range(area_table.records):
            row = area_table.row(index)
            if len(row) < 15:
                continue
            # Feld 14 ist der Name der eingestellten Sprache (3.3.5a: 16 Slots ab 11)
            label = area_table.text(row[14]) or area_table.text(row[11])
            if label:
                self._area_labels[row[0]] = label

    # -- Lesen
    def _read(self, name: str) -> "bytes | None":
        archive = self._archive_of.get(name)
        if archive is not None:
            try:
                data = archive.read_file(name)
            except Exception:  # noqa: BLE001 - defekten Eintrag ignorieren
                return None
            if data:
                return data
        for candidate in self._archives:
            try:
                data = candidate.read_file(name)
            except Exception:  # noqa: BLE001
                continue
            if data:
                self._archive_of[name] = candidate
                return data
        return None

    def read(self, name: str) -> "bytes | None":
        """Datei aus den MPQs lesen (auch für die Geländekacheln)."""
        return self._read(name)

    def map_directory(self, map_id: int) -> "str | None":
        """Verzeichnis der Weltkacheln dieser Karte (Map.dbc, Feld 1)."""
        if not self._map_dirs:
            self.load_map_directories()
        return self._map_dirs.get(int(map_id))

    def load_map_directories(self) -> int:
        try:
            table = self._dbc("Map.dbc")
        except MapError as exc:
            self.error = str(exc)
            return 0
        for index in range(table.records):
            row = table.row(index)
            if len(row) < 2:
                continue
            directory = table.text(row[1])
            if directory:
                self._map_dirs[row[0]] = directory
        return len(self._map_dirs)

    # -- Zonen
    def candidates(self, map_id: int, x: float, y: float) -> list[Zone]:
        """Passende Zonen für eine Position, kleinste zuerst."""
        rows = [zone for zone in self._zones if zone.map_id == map_id and zone.area]
        if not rows:
            return []
        inside = [zone for zone in rows if zone.contains(x, y)]
        inside.sort(key=lambda zone: zone.area)
        result = inside[:3]
        if not result:
            rows.sort(key=lambda zone: zone.center_distance(x, y))
            result = rows[:2]
        # Kontinentkarte als letzter Ausweg (grober Überblick)
        for zone in rows:
            if zone.area_id == 0 and zone.area > (result[-1].area if result else 0):
                if all(zone.key() != other.key() for other in result):
                    result.append(zone)
                break
        return result

    def zone_for(self, map_id: int, x: float, y: float) -> "Zone | None":
        for zone in self.candidates(map_id, x, y):
            if self._has_tiles(zone.name):
                return zone
        return None

    def texture_for(self, map_id: int, x: float, y: float) -> "MapTexture | None":
        for zone in self.candidates(map_id, x, y):
            texture = self.texture(zone)
            if texture is not None:
                return texture
        return None

    # -- Texturen
    def _tile_names(self, name: str) -> list[str]:
        return [f"Interface\\WorldMap\\{name}\\{name}{index}.blp" for index in range(1, 65)]

    def _has_tiles(self, name: str) -> bool:
        return bool(self._read(self._tile_names(name)[0]))

    def texture(self, zone: Zone) -> "MapTexture | None":
        """Bild der Zone - aus dem Zwischenspeicher oder frisch gebaut."""
        png = self.cache_dir / f"{zone.key()}.png"
        meta_path = self.cache_dir / f"{zone.key()}.json"
        meta = self._load_meta(meta_path)
        if meta and png.exists():
            meta["zone"]["label"] = zone.label
            return MapTexture(
                _zone_from_dict(meta["zone"]), png, int(meta["width"]), int(meta["height"])
            )
        if not self._ready and not self.open():
            return None
        return self._render(zone, png, meta_path)

    def _load_meta(self, path: Path) -> "dict | None":
        try:
            with path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if meta.get("version") != CACHE_VERSION:
            return None
        if not meta.get("width") or not meta.get("height") or "zone" not in meta:
            return None
        return meta

    def _render(self, zone: Zone, png: Path, meta_path: Path) -> "MapTexture | None":
        tiles: list[Image.Image] = []
        misses = 0
        for name in self._tile_names(zone.name):
            data = self._read(name)
            if not data:
                misses += 1
                if misses >= 2:
                    break
                continue
            misses = 0
            try:
                tiles.append(decode_blp(data).convert("RGB"))
            except (BLPError, OSError, ValueError):
                break
        if not tiles:
            return None

        columns = self._columns(zone, len(tiles))
        rows = (len(tiles) + columns - 1) // columns
        width = columns * TILE
        height = rows * TILE
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        for index, tile in enumerate(tiles):
            canvas.paste(tile, ((index % columns) * TILE, (index // columns) * TILE))

        cropped = self._trim(canvas)
        png.parent.mkdir(parents=True, exist_ok=True)
        temporary = png.with_suffix(".tmp.png")
        cropped.save(temporary, format="PNG")
        os.replace(temporary, png)

        meta = {
            "version": CACHE_VERSION,
            "zone": zone.as_dict(),
            "width": cropped.width,
            "height": cropped.height,
            "tiles": len(tiles),
            "columns": columns,
            "canvas": [width, height],
        }
        temporary_meta = meta_path.with_suffix(".tmp.json")
        with temporary_meta.open("w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_meta, meta_path)
        return MapTexture(zone, png, cropped.width, cropped.height)

    @staticmethod
    def _columns(zone: Zone, count: int) -> int:
        """Kachelraster bestimmen: fast immer 4 Spalten, sonst nach Seitenverhältnis."""
        if count % 4 == 0:
            return 4
        target = zone.width / zone.height if zone.height else 1.0
        best = 4
        best_error = None
        for columns in COLUMN_CANDIDATES:
            if columns > count:
                continue
            rows = (count + columns - 1) // columns
            ratio = columns / rows
            error = abs(math.log(ratio / target)) if target else 0.0
            if best_error is None or error < best_error:
                best, best_error = columns, error
        return best

    @staticmethod
    def _trim(canvas: Image.Image) -> Image.Image:
        """Schwarze Füllränder der letzten Kachelreihe abschneiden.

        Die bemalten Karten sind 3:2, die Kacheln liefern aber Vielfache von
        256 - der Rest der letzten Kachelreihe ist schwarz. Gesucht wird die
        letzte Zeile/Spalte, in der noch ein nennenswerter Teil bemalt ist
        (einzelne Bildpunkte am Rand reichen nicht).
        """
        grey = canvas.convert("L")
        pixels = grey.load()
        width, height = grey.size
        step_x = max(1, width // 256)
        step_y = max(1, height // 256)

        def row_painted(y: int) -> bool:
            samples = range(0, width, step_x)
            painted = sum(1 for x in samples if pixels[x, y] > PAINT_LEVEL)
            return painted >= len(samples) * PAINT_SHARE

        def column_painted(x: int) -> bool:
            samples = range(0, height, step_y)
            painted = sum(1 for y in samples if pixels[x, y] > PAINT_LEVEL)
            return painted >= len(samples) * PAINT_SHARE

        right = next((x for x in range(width - 1, -1, -1) if column_painted(x)), 0)
        bottom = next((y for y in range(height - 1, -1, -1) if row_painted(y)), 0)
        right += 1
        bottom += 1
        if right < width * 0.5 or bottom < height * 0.5:
            return canvas
        if (right, bottom) == (width, height):
            return canvas
        return canvas.crop((0, 0, right, bottom))


def _zone_from_dict(data: dict) -> Zone:
    return Zone(
        name=data.get("name", ""),
        map_id=int(data.get("map", 0)),
        area_id=int(data.get("area", 0)),
        left=float(data.get("left", 0.0)),
        right=float(data.get("right", 0.0)),
        top=float(data.get("top", 0.0)),
        bottom=float(data.get("bottom", 0.0)),
    )
