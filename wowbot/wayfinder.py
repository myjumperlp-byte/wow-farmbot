"""Wegfinder für den Bot: Hindernis-Gedächtnis plus A*-Route.

Warum kein echtes Navmesh? Der Server schickt dem Client keine Geländedaten -
keine Heightmap, keine Kollisionsdreiecke, keine Karte. Aus dem Protokoll allein
ist nicht ablesbar, wo eine Wand, ein Fels oder ein Zaun steht. Deshalb merkt
sich der Wegfinder, wo der Charakter tatsächlich hängen bleibt:

* Setzt der Server den Charakter zurück (er lief gegen etwas), wird die ganze
  abgelehnte Strecke als Hindernis gespeichert. Das ist Gelände und bleibt
  **dauerhaft** - pro Karte in einer Datei, damit nach einem Neustart nichts
  vergessen ist.
* Scheitert ein Angriff an der Sichtlinie, wird die Mitte zwischen Charakter und
  Ziel nur **vorübergehend** gemerkt (`memory_seconds`): das kann auch ein Mob
  gewesen sein, der gleich weiterläuft.

Aus diesen Zellen plant A* eine Route herum. Ist auf dem Weg nichts bekannt,
kostet der Wegfinder praktisch nichts: dann kommt sofort die Gerade zurück.

Zellen sind `cell_size` Yard groß (Standard 5). Diagonale Schritte kosten 1.414,
Ecken werden nicht geschnitten.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import time
from pathlib import Path

DIAGONAL = math.sqrt(2.0)
NEIGHBOURS = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)
MAX_WALLS = 20000

log = logging.getLogger("wowbot.weg")


class Wayfinder:
    """Lokale Hinderniskarte mit A*-Planung und Speicher pro Karte."""

    def __init__(
        self,
        cell_size: float = 5.0,
        memory_seconds: float = 600.0,
        storage_dir: "Path | None" = None,
    ) -> None:
        self.cell_size = max(1.0, float(cell_size))
        self.memory_seconds = max(30.0, float(memory_seconds))
        # Gelände: bleibt für immer (bis die Datei gelöscht wird).
        self._walls: dict[tuple[int, int], float] = {}
        # Verdacht (z. B. Sichtlinie): verfällt nach memory_seconds.
        self._blocked: dict[tuple[int, int], float] = {}
        # Zellen, in denen wir gerade selbst stehen oder die das Ziel sind:
        # beim Planen dürfen die nicht als Wand gelten, sonst käme keine Route
        # zustande, sobald ein Hindernis direkt neben uns gemerkt wurde.
        self._ignore: set[tuple[int, int]] = set()
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        # Geländedaten aus dem Client (Klippen, Wasser) - optional
        self.terrain = None
        self.map_id = 0
        self._map_loaded = False
        self._dirty = False
        self._last_save = 0.0
        # Zaehler fuer die Anzeige: aendert sich, wenn sich das Gelaende aendert.
        self._version = 0

    # ------------------------------------------------------------ Hindernisse
    def cell(self, x: float, y: float) -> tuple[int, int]:
        return (int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size)))

    def center(self, cell: tuple[int, int]) -> tuple[float, float]:
        return ((cell[0] + 0.5) * self.cell_size, (cell[1] + 0.5) * self.cell_size)

    def _mark(self, x: float, y: float, radius: float, now: float, permanent: bool) -> int:
        span = int(math.ceil(radius / self.cell_size))
        base = self.cell(x, y)
        cells = 0
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                key = (base[0] + dx, base[1] + dy)
                px, py = self.center(key)
                if math.hypot(px - x, py - y) > radius + self.cell_size * 0.5:
                    continue
                if permanent:
                    if key not in self._walls:
                        self._version += 1
                    self._walls[key] = now
                    self._dirty = True
                else:
                    self._blocked[key] = now + self.memory_seconds
                cells += 1
        if permanent and len(self._walls) > MAX_WALLS:
            # Sehr lange Sitzung: die ältesten Ecken fallen raus.
            keep = sorted(self._walls.items(), key=lambda item: item[1])
            drop = keep[: max(1, len(keep) // 10)]
            for key, _ in drop:
                self._walls.pop(key, None)
            # Die Karte im Fenster haengt an dieser Nummer - sonst zeigt sie
            # geloeschte Zellen weiter an.
            self._version += 1
        return cells

    def block(
        self,
        x: float,
        y: float,
        radius: "float | None" = None,
        permanent: bool = False,
    ) -> int:
        """Merkt sich einen Kreis als Hindernis und liefert die Zellenzahl."""
        now = time.time()
        radius = self.cell_size * 1.2 if radius is None else float(radius)
        cells = self._mark(x, y, radius, now, permanent)
        if cells:
            log.info(
                "[weg] Hindernis bei (%.0f, %.0f) gemerkt: %d Zelle(n), %d bekannt",
                x,
                y,
                cells,
                self.count(),
            )
            self._save_soon()
        return cells

    def block_segment(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        radius: "float | None" = None,
        permanent: bool = True,
    ) -> int:
        """Merkt sich einen ganzen abgelehnten Korridor, nicht nur einen Punkt.

        Wenn der Server uns zurücksetzt, ist nicht ein einzelner Fleck schuld,
        sondern die Strecke, die wir laufen wollten. Nur so plant A* beim
        nächsten Versuch wirklich eine andere Ecke an. Solche Strecken sind
        Gelände und werden dauerhaft gespeichert.
        """
        now = time.time()
        radius = self.cell_size * 0.8 if radius is None else float(radius)
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, int(distance / (self.cell_size * 0.7)))
        cells = 0
        for step in range(steps + 1):
            t = step / steps
            cells += self._mark(
                start[0] + (end[0] - start[0]) * t,
                start[1] + (end[1] - start[1]) * t,
                radius,
                now,
                permanent,
            )
        if cells:
            log.info(
                "[weg] Strecke (%.0f, %.0f) -> (%.0f, %.0f) blockiert: %d Zelle(n), %d bekannt",
                start[0],
                start[1],
                end[0],
                end[1],
                cells,
                self.count(),
            )
            self._save_soon()
        return cells

    def forget_all(self) -> None:
        self._walls.clear()
        self._blocked.clear()
        self._dirty = True
        self._version += 1

    @property
    def walls_version(self) -> int:
        """Steigt bei jeder Änderung am gespeicherten Gelände."""
        return self._version

    def wall_cells(self) -> list[tuple[float, float]]:
        """Alle gespeicherten Hindernisse als Weltkoordinaten (Zellmitten)."""
        # list() ist ein einzelner C-Aufruf: waehrend die GUI das hier liest,
        # darf der Bot-Thread neue Hindernisse eintragen, ohne dass die
        # Iteration mit "dictionary changed size" abbricht.
        return [self.center(cell) for cell in list(self._walls)]

    def count(self) -> int:
        self._expire(time.time())
        return len(self._walls) + len(self._blocked)

    def wall_count(self) -> int:
        return len(self._walls)

    def _expire(self, now: float) -> None:
        if not self._blocked:
            return
        for key in [key for key, expiry in self._blocked.items() if expiry <= now]:
            self._blocked.pop(key, None)

    def _raw_blocked(self, cell: tuple[int, int], now: float) -> bool:
        if cell in self._walls:
            return True
        expiry = self._blocked.get(cell)
        return expiry is not None and expiry > now

    def set_terrain(self, terrain) -> None:
        """Geländedaten (wowbot.terrain.Terrain) anhängen oder entfernen."""
        self.terrain = terrain
        self._version += 1

    def _terrain_blocked(self, cell: tuple[int, int]) -> bool:
        terrain = self.terrain
        if terrain is None:
            return False
        if getattr(terrain, "map_id", self.map_id) != self.map_id:
            return False
        try:
            return terrain.blocked(cell) != 0
        except Exception:  # noqa: BLE001 - Gelände darf den Bot nie stoppen
            return False

    def blocked(self, cell: tuple[int, int], now: "float | None" = None) -> bool:
        if cell in self._ignore:
            return False
        if self._terrain_blocked(cell):
            return True
        now = time.time() if now is None else now
        return self._raw_blocked(cell, now)

    # ------------------------------------------------------- Karten / Speichern
    def set_map(self, map_id: int) -> None:
        """Karte wechseln: alte Karte sichern, Gedächtnis der neuen laden."""
        map_id = int(map_id)
        if map_id == self.map_id and self._map_loaded:
            return
        if self._map_loaded:
            self.save(force=True)
        self._walls.clear()
        self._blocked.clear()
        self.map_id = map_id
        self._map_loaded = True
        self._dirty = False
        self._version += 1
        if self.terrain is not None:
            self.terrain.set_map(map_id)
        self.load()

    def _path(self) -> "Path | None":
        if self.storage_dir is None:
            return None
        return self.storage_dir / f"map-{self.map_id}.json"

    def load(self) -> int:
        path = self._path()
        if path is None or not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[weg] Hindernisdatei %s nicht lesbar (%s) - starte leer", path, exc)
            return 0
        stored_size = float(data.get("cell_size", 0.0) or 0.0)
        if stored_size and abs(stored_size - self.cell_size) > 0.01:
            log.info(
                "[weg] %s wurde mit %.1f-yd-Zellen gespeichert (jetzt %.1f) - ignoriert",
                path.name,
                stored_size,
                self.cell_size,
            )
            return 0
        loaded = 0
        for entry in data.get("walls") or []:
            try:
                cell = (int(entry[0]), int(entry[1]))
            except (TypeError, ValueError, IndexError):
                continue
            self._walls[cell] = 0.0
            loaded += 1
        if loaded:
            self._version += 1
            log.info(
                "[weg] %d bekannte Hindernisse für Map %d geladen (%s)",
                loaded,
                self.map_id,
                path.name,
            )
        return loaded

    def save(self, force: bool = False) -> bool:
        path = self._path()
        if path is None or not self._dirty:
            return False
        now = time.time()
        if not force and now - self._last_save < 5.0:
            return False
        payload = {
            "version": 1,
            "map": self.map_id,
            "cell_size": self.cell_size,
            "walls": sorted([cell[0], cell[1]] for cell in self._walls),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            log.warning("[weg] Hindernisse konnten nicht gespeichert werden: %s", exc)
            return False
        self._dirty = False
        self._last_save = now
        if force:
            log.info(
                "[weg] %d Hindernisse für Map %d gespeichert (%s)",
                len(self._walls),
                self.map_id,
                path.name,
            )
        else:
            log.debug("[weg] %d Hindernisse gesichert", len(self._walls))
        return True

    def _save_soon(self) -> None:
        self.save(force=False)

    # ------------------------------------------------------------------ Planen
    def plan(
        self, start_x: float, start_y: float, goal_x: float, goal_y: float
    ) -> list[tuple[float, float]]:
        """Route von Start zu Ziel als Wegpunkte (letzter Punkt ist das Ziel)."""
        now = time.time()
        self._expire(now)
        start = self.cell(start_x, start_y)
        goal = self.cell(goal_x, goal_y)
        if start == goal:
            return [(goal_x, goal_y)]
        if not self._blocked_near(start, goal, now) and self.line_free(
            (start_x, start_y), (goal_x, goal_y), now
        ):
            return [(goal_x, goal_y)]

        self._ignore = {
            cell
            for cell in (start, goal)
            if self._raw_blocked(cell, now) or self._terrain_blocked(cell)
        }
        try:
            cells = None
            # Erst eng suchen, dann weiter ausholen: ein langes Hindernis braucht
            # einen größeren Suchraum, sonst würde die Route quer durch führen.
            for margin in (8, 20, 40):
                cells = self._search(start, goal, now, margin=margin)
                if cells is not None:
                    break
            if cells is None:
                # Rundherum dicht: notfalls durch die Hindernisse durch (teuer).
                log.info("[weg] kein Weg außen herum gefunden - nehme den kürzesten durch")
                cells = self._search(start, goal, now, margin=40, allow_blocked=True)
            if cells is None:
                return [(goal_x, goal_y)]
            points = [self.center(cell) for cell in cells[1:-1]]
            points.append((goal_x, goal_y))
            # Glätten, solange Start-/Zielzelle noch als frei gelten.
            return self._smooth((start_x, start_y), points, now)
        finally:
            self._ignore = set()

    def _blocked_near(self, a: tuple[int, int], b: tuple[int, int], now: float) -> bool:
        """Liegt im Rechteck um die Gerade a-b irgendein bekanntes Hindernis?"""
        min_x, max_x = min(a[0], b[0]) - 1, max(a[0], b[0]) + 1
        min_y, max_y = min(a[1], b[1]) - 1, max(a[1], b[1]) + 1
        for cx, cy in list(self._walls):
            if min_x <= cx <= max_x and min_y <= cy <= max_y:
                return True
        for (cx, cy), expiry in self._blocked.items():
            if expiry <= now:
                continue
            if min_x <= cx <= max_x and min_y <= cy <= max_y:
                return True
        return False

    @staticmethod
    def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
        dx = abs(a[0] - b[0])
        dy = abs(a[1] - b[1])
        return (dx + dy) + (DIAGONAL - 2.0) * min(dx, dy)

    def _neighbours(
        self, cell: tuple[int, int], now: float, allow_blocked: bool
    ) -> "list[tuple[tuple[int, int], float]]":
        result: list[tuple[tuple[int, int], float]] = []
        for dx, dy in NEIGHBOURS:
            neighbour = (cell[0] + dx, cell[1] + dy)
            if dx and dy:
                # Keine Ecken schneiden: beide Nachbarzellen müssen frei sein.
                if self.blocked((cell[0] + dx, cell[1]), now) or self.blocked(
                    (cell[0], cell[1] + dy), now
                ):
                    continue
            is_blocked = self.blocked(neighbour, now)
            if is_blocked and not allow_blocked:
                continue
            step = DIAGONAL if (dx and dy) else 1.0
            if is_blocked:
                step *= 6.0  # Notweg: lieber außen herum, aber nicht unmöglich
            result.append((neighbour, step))
        return result

    def _search(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        now: float,
        margin: int = 8,
        allow_blocked: bool = False,
    ) -> "list[tuple[int, int]] | None":
        min_x, max_x = min(start[0], goal[0]) - margin, max(start[0], goal[0]) + margin
        min_y, max_y = min(start[1], goal[1]) - margin, max(start[1], goal[1]) + margin
        limit = 220
        if max_x - min_x > limit or max_y - min_y > limit:
            return None

        heap: list[tuple[float, float, tuple[int, int]]] = [
            (self._heuristic(start, goal), 0.0, start)
        ]
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        cost: dict[tuple[int, int], float] = {start: 0.0}
        closed: set[tuple[int, int]] = set()
        expansions = 0

        while heap:
            _, g, current = heapq.heappop(heap)
            if current == goal:
                path = [current]
                while path[-1] in came_from:
                    path.append(came_from[path[-1]])
                path.reverse()
                return path
            if current in closed:
                continue
            closed.add(current)
            expansions += 1
            if expansions > 60000:
                log.warning("[weg] Suche abgebrochen: zu viele Zellen")
                return None
            for neighbour, step in self._neighbours(current, now, allow_blocked):
                if not (min_x <= neighbour[0] <= max_x and min_y <= neighbour[1] <= max_y):
                    continue
                if neighbour in closed:
                    continue
                new_cost = g + step
                if new_cost < cost.get(neighbour, float("inf")):
                    cost[neighbour] = new_cost
                    came_from[neighbour] = current
                    heapq.heappush(
                        heap,
                        (new_cost + self._heuristic(neighbour, goal), new_cost, neighbour),
                    )
        return None

    def _smooth(
        self, start: tuple[float, float], points: list[tuple[float, float]], now: float
    ) -> list[tuple[float, float]]:
        """Wegpunkte überspringen, wenn die Gerade dorthin frei ist."""
        result: list[tuple[float, float]] = []
        current = start
        index = 0
        while index < len(points):
            best = index
            for candidate in range(len(points) - 1, index, -1):
                if self.line_free(current, points[candidate], now):
                    best = candidate
                    break
            result.append(points[best])
            current = points[best]
            index = best + 1
        return result

    def line_free(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        now: "float | None" = None,
    ) -> bool:
        """Ist die Gerade zwischen zwei Punkten frei von bekannten Hindernissen?"""
        now = time.time() if now is None else now
        distance = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, int(distance / (self.cell_size * 0.5)))
        for step in range(steps + 1):
            t = step / steps
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            if self.blocked(self.cell(x, y), now):
                return False
        return True
