"""Minimap-Kacheln aus dem Client: das echte Geländebild je 533-Yard-Feld.

Der Client legt für jedes Feld der Welt ein 256×256-Bild ab (Wege, Gebäude,
Wasser, Boden). Die Namen sind gehasht und stehen in
`textures\\minimap\\md5translate.trs`; das Raster ist dasselbe wie bei den
Geländekacheln:

    Feld (tx, ty) deckt  y: [17066.67 - (tx+1)*533.33 … 17066.67 - tx*533.33]
                         x: [17066.67 - (ty+1)*533.33 … 17066.67 - ty*533.33]

Der Index folgt also der West-Ost-Achse zuerst (y), dann der Nord-Süd-Achse (x)
- genau wie bei den ADT-Dateien, geprüft über die Naht der Höhenraster.

Damit sitzt jede Position genau auf ihrem Bildpunkt - ohne Schätzungen aus
Kartengrenzen. Die Bilder werden einmal entpackt und als PNG zwischengespeichert.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from wowbot.blp import BLPError, decode_blp

log = logging.getLogger("wowbot.karte")

TILE_SIZE = 533.3333333
WORLD_HALF = 17066.6666667
TRANSLATE = "textures\\minimap\\md5translate.trs"


def tile_rect(tx: int, ty: int) -> tuple[float, float, float, float]:
    """Weltrechteck eines Feldes: (y_ost, y_west, x_nord, x_süd)."""
    east = WORLD_HALF - tx * TILE_SIZE
    north = WORLD_HALF - ty * TILE_SIZE
    return east, east - TILE_SIZE, north, north - TILE_SIZE


def tile_of(x: float, y: float) -> tuple[int, int]:
    """Feld, in dem eine Weltposition liegt: (tx entlang y, ty entlang x)."""
    return (
        int((WORLD_HALF - y) // TILE_SIZE),
        int((WORLD_HALF - x) // TILE_SIZE),
    )


def _placeholder(image) -> bool:
    """Erkennt die leeren Platzhalterkacheln des Clients (fast reines Weiß)."""
    grey = image.convert("L").resize((8, 8))
    values = list(grey.getdata())
    return min(values) > 225 and max(values) - min(values) < 12


class MinimapTiles:
    """Kacheln aus den MPQs holen, entpacken und auf der Platte halten."""

    def __init__(self, maps, cache_dir: Path) -> None:
        self.maps = maps  # wowbot.mapdata.ClientMaps
        self.cache_dir = Path(cache_dir) / "minimap"
        self.error: "str | None" = None
        self._index: "dict[tuple[str, str], str] | None" = None
        self._lock = threading.Lock()
        self._absent: set[tuple[str, int, int]] = set()
        self._decoded = 0
        self._bytes = 0

    # -- Verzeichnis der Namen
    def _load_index(self) -> bool:
        if self._index is not None:
            return True
        with self._lock:
            if self._index is not None:
                return True
            raw = self.maps.read(TRANSLATE)
            if not raw:
                self.error = "md5translate.trs fehlt im Client"
                self._index = {}
                return False
            index: dict[tuple[str, str], str] = {}
            for line in raw.decode("latin1").splitlines():
                if "\t" not in line:
                    continue
                name, hashed = line.split("\t")
                parts = name.split("\\")
                if len(parts) != 2:
                    continue
                index[(parts[0].lower(), parts[1])] = hashed.strip()
            self._index = index
            self._bytes = len(raw)
            return bool(index)

    # -- Kacheln
    def path(self, map_id: int, tx: int, ty: int) -> "Path | None":
        """PNG-Pfad der Kachel - aus dem Zwischenspeicher oder frisch entpackt."""
        directory = self.maps.map_directory(map_id)
        if not directory:
            return None
        key = (directory.lower(), int(tx), int(ty))
        cached = self.cache_dir / f"{key[0]}_{tx}_{ty}.png"
        if cached.exists():
            return cached
        if key in self._absent:
            return None
        if not self._load_index():
            return None
        hashed = (self._index or {}).get((key[0], f"map{tx}_{ty}.blp"))
        if not hashed:
            self._absent.add(key)
            return None
        data = self.maps.read(f"Textures\\Minimap\\{hashed}")
        if not data:
            self._absent.add(key)
            return None
        try:
            image = decode_blp(data).convert("RGB")
        except (BLPError, OSError, ValueError) as exc:
            log.debug("[karte] Kachel %s/%s/%s nicht lesbar (%s)", directory, tx, ty, exc)
            self._absent.add(key)
            return None
        if _placeholder(image):
            # Der Client legt für unbearbeitete Felder ein leeres weißes Bild ab.
            self._absent.add(key)
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_suffix(".tmp.png")
        image.save(temporary, format="PNG")
        temporary.replace(cached)
        self._decoded += 1
        return cached

    def stats(self) -> dict:
        return {"decoded": self._decoded, "index": len(self._index or {}), "error": self.error}
