"""Geländedaten aus dem WoW-Client (3.3.5a): Höhen, Klippen und Wasser.

Der Server schickt keine Kollisionsdaten. Der Client hat sie aber: jede
Weltkachel (`World\\Maps\\<Karte>\\<Karte>_<x>_<y>.adt`) enthält 16×16 Gelände-
blöcke mit je 9×9 Höhenpunkten (MCVT) und, wo es Wasser gibt, einen
Flüssigkeitsblock (MCLQ) mit der Wasseroberfläche.

Daraus wird pro Kachel eine Zellebene gebaut, die genau auf dem Raster des
Wegfinders liegt:

* **Klippe** - die Steigung zwischen zwei Höhenpunkten ist zu groß zum
  Hochlaufen (Standard 1,6 Höhenmeter pro Yard ≈ 58°).
* **Wasser** - das Gelände liegt mehr als `water_depth` unter der
  Wasseroberfläche des Blocks, man würde also schwimmen.

Der Wegfinder meidet diese Zellen von vornherein, statt erst nach einem
fehlgeschlagenen Versuch zu lernen.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger("wowbot.gelaende")

TILE_SIZE = 533.3333333
CHUNK_SIZE = TILE_SIZE / 16.0  # 33.3333 Yard
GRID = 8  # Höhenpunkte je Blockkante - 1
STEP = CHUNK_SIZE / GRID  # 4.1667 Yard
WORLD_HALF = 17066.6666667  # Weltursprung im 3.3.5a-Client
GRID_POINTS = 16 * GRID + 1  # 129 Höhenpunkte je Kachelkante

FREE = 0
CLIFF = 1
WATER = 2
MAX_TILES = 48  # Speichergrenze: ~50 Kacheln ≈ 2.000.000 Zellen im Hinterkopf

_REASON = {CLIFF: "Klippe", WATER: "Wasser"}


def reason(code: int) -> str:
    return _REASON.get(code, "?")


# ---------------------------------------------------------------------- Kachel
class TerrainTile:
    """Eine 533-Yard-Kachel mit den Hinderniszellen für den Wegfinder."""

    __slots__ = ("tx", "ty", "cells", "cliffs", "water", "seconds")

    def __init__(self, tx: int, ty: int) -> None:
        self.tx = tx
        self.ty = ty
        self.cells: dict[tuple[int, int], int] = {}
        self.cliffs = 0
        self.water = 0
        self.seconds = 0.0

    def __repr__(self) -> str:  # pragma: no cover - nur für Logs
        return f"TerrainTile({self.tx},{self.ty}, {len(self.cells)} Zellen)"


# ------------------------------------------------------------------- ADT lesen
def _chunks(data: bytes):
    """Top-Level-Chunks. Die Kennung steht im Client umgekehrt im File."""
    pos = 0
    while pos + 8 <= len(data):
        size = struct.unpack_from("<I", data, pos + 4)[0]
        yield data[pos:pos + 4][::-1], pos, pos + 8, size
        pos += 8 + size


def _heights(raw: bytes):
    """Höhenraster 129×129 (Weltkoordinaten) und Wasseroberflächen je Block.

    Rückgabe: (heights, surfaces) - `heights[iy*129+ix]` ist die absolute Höhe
    (Blockbasis + MCVT), `surfaces[(ix,iy)]` die Wasseroberfläche des Blocks
    oder None.
    """
    tops = {magic: (start, data_start, size) for magic, start, data_start, size in _chunks(raw)}
    mcin = tops.get(b"MCIN")
    if mcin is None:
        raise ValueError("MCIN fehlt")
    mcin_start = mcin[1]

    heights: list = [None] * (GRID_POINTS * GRID_POINTS)
    surfaces: dict[tuple[int, int], float] = {}

    for index in range(256):
        chunk_off, _chunk_size = struct.unpack_from("<2I", raw, mcin_start + index * 16)
        if raw[chunk_off:chunk_off + 4] != b"KNCM":
            continue
        header = chunk_off + 8
        ix, iy = struct.unpack_from("<2I", raw, header + 4)
        if ix > 15 or iy > 15:
            continue
        ofs_height = struct.unpack_from("<I", raw, header + 0x14)[0]
        ofs_liquid, size_liquid = struct.unpack_from("<2I", raw, header + 0x60)
        _py, _px, base_z = struct.unpack_from("<3f", raw, header + 0x68)

        mcvt = chunk_off + ofs_height
        if raw[mcvt:mcvt + 4] not in (b"TVCM", b"MCVT"):
            continue
        values = struct.unpack_from("<145f", raw, mcvt + 8)
        # 9 äußere Punkte, dann 8 innere, dann wieder 9 ... (Reihe für Reihe)
        for row in range(9):
            start = row * 17
            for col in range(9):
                gx = ix * GRID + col
                gy = iy * GRID + row
                heights[gy * GRID_POINTS + gx] = base_z + values[start + col]

        if size_liquid > 100:
            block = chunk_off + ofs_liquid + 8
            if block + 8 <= len(raw):
                _low, high = struct.unpack_from("<2f", raw, block)
                if -5000.0 < high < 5000.0:
                    # Die Wasserblöcke stehen in umgekehrter Zeilenfolge zum
                    # Höhenraster (im Client gegen die Kachelmitte gespiegelt) -
                    # gemessen an den Minimap-Bildern: 80 % Treffer statt 0 %.
                    surfaces[(ix, 15 - iy)] = high
    return heights, surfaces


def _vertex_codes(
    heights: list,
    surfaces: dict,
    max_slope: float,
    water_depth: float,
    use_water: bool,
) -> list:
    """Pro Höhenpunkt: frei, Klippe oder Wasser."""
    codes = [FREE] * (GRID_POINTS * GRID_POINTS)
    limit = max_slope * STEP
    for gy in range(GRID_POINTS):
        for gx in range(GRID_POINTS):
            index = gy * GRID_POINTS + gx
            height = heights[index]
            if height is None:
                continue
            steepest = 0.0
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = gx + dx, gy + dy
                if not (0 <= nx < GRID_POINTS and 0 <= ny < GRID_POINTS):
                    continue
                other = heights[ny * GRID_POINTS + nx]
                if other is None:
                    continue
                drop = abs(other - height)
                if drop > steepest:
                    steepest = drop
            code = FREE
            if steepest > limit:
                code = CLIFF
            if use_water:
                surface = surfaces.get((gx // GRID, gy // GRID))
                if surface is not None and height < surface - water_depth:
                    code = WATER
            codes[index] = code
    return codes


def _blocked_cells(
    codes: list, tx: int, ty: int, cell_size: float
) -> dict[tuple[int, int], int]:
    """Hinderniszellen des Wegfinder-Rasters für eine Kachel.

    Der Kachelindex folgt der West-Ost-Achse (tx → y) und dann der
    Nord-Süd-Achse (ty → x); im Höhenraster ist die Spalte `ix` die
    West-Ost-Richtung und die Zeile `iy` die Nord-Süd-Richtung.
    """
    east = WORLD_HALF - tx * TILE_SIZE  # Weltachse y
    north = WORLD_HALF - ty * TILE_SIZE  # Weltachse x
    cells: dict[tuple[int, int], int] = {}
    first_x = int(math.floor((north - TILE_SIZE) / cell_size))
    last_x = int(math.floor(north / cell_size))
    first_y = int(math.floor((east - TILE_SIZE) / cell_size))
    last_y = int(math.floor(east / cell_size))
    for cx in range(first_x, last_x + 1):
        x = (cx + 0.5) * cell_size
        vi = (north - x) / STEP  # Zeile im Höhenraster
        if vi < -0.5 or vi > GRID_POINTS - 0.5:
            continue
        for cy in range(first_y, last_y + 1):
            y = (cy + 0.5) * cell_size
            ui = (east - y) / STEP  # Spalte im Höhenraster
            if ui < -0.5 or ui > GRID_POINTS - 0.5:
                continue
            worst = FREE
            for ov in (0.0, 1.0):
                iy = int(round(vi + ov))
                if not 0 <= iy < GRID_POINTS:
                    continue
                for ou in (0.0, 1.0):
                    ix = int(round(ui + ou))
                    if not 0 <= ix < GRID_POINTS:
                        continue
                    code = codes[iy * GRID_POINTS + ix]
                    if code and (worst == FREE or code < worst):
                        worst = code
            if worst:
                cells[(cx, cy)] = worst
    return cells


# ------------------------------------------------------------------- Gelände
class Terrain:
    """Geländezellen für den Wegfinder, kachelweise im Hintergrund geladen."""

    def __init__(
        self,
        maps,
        cell_size: float = 5.0,
        max_slope: float = 1.6,
        water_depth: float = 1.2,
        use_water: bool = True,
    ) -> None:
        self.maps = maps  # wowbot.mapdata.ClientMaps
        self.cell_size = max(1.0, float(cell_size))
        self.max_slope = max(0.2, float(max_slope))
        self.water_depth = max(0.0, float(water_depth))
        self.use_water = bool(use_water)
        self.map_id = 0
        self.ready = False
        self.error: "str | None" = None
        self._tiles: dict[tuple[int, int], "TerrainTile | None"] = {}
        self._pending: set[tuple[int, int]] = set()
        self._lock = threading.Lock()
        self._directory: "str | None" = None
        self._missing_logged = False
        self._missing_tile_logged = False
        self._cells_blocked = 0
        self._tiles_loaded = 0
        self._seconds = 0.0

    # -- Kartenname / Kacheln
    def set_map(self, map_id: int) -> bool:
        """Karte wechseln; False, wenn es dafür kein Gelände gibt."""
        map_id = int(map_id)
        if map_id == self.map_id and self.ready:
            return self._directory is not None
        self.map_id = map_id
        self._tiles.clear()
        self._pending.clear()
        self._cells_blocked = 0
        self._tiles_loaded = 0
        self._seconds = 0.0
        self._missing_logged = False
        self._missing_tile_logged = False
        try:
            self._directory = self.maps.map_directory(map_id)
        except Exception as exc:  # noqa: BLE001 - Gelände ist optional
            self.error = str(exc)
            self._directory = None
        self.ready = True
        if not self._directory:
            if not self._missing_logged:
                log.info("[gelände] keine Kartendaten für Map %d - lerne weiter aus Fehlern", map_id)
                self._missing_logged = True
            return False
        return True

    @staticmethod
    def tile_of(x: float, y: float) -> tuple[int, int]:
        tx = int(math.floor((WORLD_HALF - y) / TILE_SIZE))
        ty = int(math.floor((WORLD_HALF - x) / TILE_SIZE))
        return tx, ty

    def _adt_name(self, tx: int, ty: int) -> "str | None":
        if self._directory is None:
            return None
        return f"World\\Maps\\{self._directory}\\{self._directory}_{tx}_{ty}.adt"

    # -- Laden
    def _load(self, tx: int, ty: int) -> "TerrainTile | None":
        name = self._adt_name(tx, ty)
        if name is None:
            return None
        started = time.monotonic()
        raw = self.maps._read(name)
        if not raw:
            if not self._missing_tile_logged:
                self._missing_tile_logged = True
                log.info(
                    "[gelände] keine Kachel %s im Client - hier wird nichts vorab erkannt",
                    name.split("\\")[-1],
                )
            return None
        heights, surfaces = _heights(raw)
        codes = _vertex_codes(heights, surfaces, self.max_slope, self.water_depth, self.use_water)
        tile = TerrainTile(tx, ty)
        tile.cells = _blocked_cells(codes, tx, ty, self.cell_size)
        for code in tile.cells.values():
            if code == CLIFF:
                tile.cliffs += 1
            elif code == WATER:
                tile.water += 1
        tile.seconds = time.monotonic() - started
        return tile

    def load_now(self, tx: int, ty: int) -> "TerrainTile | None":
        """Kachel sofort bauen (Tests, oder wenn Zeit keine Rolle spielt)."""
        with self._lock:
            if (tx, ty) in self._tiles:
                return self._tiles[(tx, ty)]
        try:
            tile = self._load(tx, ty)
        except (ValueError, struct.error, OSError) as exc:
            log.warning("[gelände] Kachel %d/%d nicht lesbar (%s)", tx, ty, exc)
            tile = None
        with self._lock:
            self._tiles[(tx, ty)] = tile
            self._pending.discard((tx, ty))
            if tile is not None:
                self._tiles_loaded += 1
                self._cells_blocked += len(tile.cells)
                self._seconds += tile.seconds
            # Speicher begrenzen: älteste Kacheln fallen raus (werden neu gebaut)
            while len(self._tiles) > MAX_TILES:
                self._tiles.pop(next(iter(self._tiles)))
        return tile

    def prefetch(self, x: float, y: float) -> None:
        """Kachel für eine Position im Hintergrund bauen (blockiert nie)."""
        if not self.ready or self._directory is None:
            return
        tx, ty = self.tile_of(x, y)
        with self._lock:
            if (tx, ty) in self._tiles or (tx, ty) in self._pending:
                return
            self._pending.add((tx, ty))
        thread = threading.Thread(
            target=self.load_now, args=(tx, ty), name=f"terrain-{tx}-{ty}", daemon=True
        )
        thread.start()

    def wait_for(self, x: float, y: float, timeout: float = 5.0) -> bool:
        """Warten, bis die Kachel einer Position fertig ist (Tests)."""
        tx, ty = self.tile_of(x, y)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if (tx, ty) in self._tiles:
                    return True
                pending = (tx, ty) in self._pending
            if not pending:
                self.load_now(tx, ty)
                return True
            time.sleep(0.02)
        return False

    # -- Abfragen
    def blocked(self, cell: tuple[int, int]) -> int:
        """0 = frei, sonst CLIFF/WATER. Unbekannte Kacheln gelten als frei."""
        if self._directory is None:
            return FREE
        center_x = (cell[0] + 0.5) * self.cell_size
        center_y = (cell[1] + 0.5) * self.cell_size
        tx, ty = self.tile_of(center_x, center_y)
        with self._lock:
            known = (tx, ty) in self._tiles
            tile = self._tiles.get((tx, ty))
        if not known:
            self.prefetch(center_x, center_y)
            return FREE
        if tile is None:
            return FREE
        return tile.cells.get(cell, FREE)

    def stats(self) -> dict:
        with self._lock:
            return {
                "map": self.map_id,
                "directory": self._directory,
                "tiles": self._tiles_loaded,
                "cells": self._cells_blocked,
                "seconds": round(self._seconds, 2),
            }
