#!/usr/bin/env python3
"""WowFarmBot - grafische Oberfläche für den 3.3.5a-Farming-Bot.

Ein Fenster mit Start/Stopp, Zugangsdaten, Live-Status und allen Ereignissen.
Der Bot läuft in einem eigenen Thread mit eigenem Event-Loop, damit das Fenster
nie blockiert.

Als Mac-Programm packen (im Projektordner):
    .venv/bin/python -m PyInstaller --windowed --name WowFarmBot \
        --osx-bundle-identifier de.wowfarmbot.app gui.py

Das gepackte Programm liest und schreibt seine Config unter
~/Library/Application Support/WowFarmBot/config.json.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wowbot.auth import LogonError
from wowbot.bot import FarmBot
from wowbot.filters import LootRules, MobRules
from wowbot.mapdata import ClientMaps, find_client_dir
from wowbot.minimap import TILE_SIZE, WORLD_HALF, MinimapTiles, tile_of
from wowbot.i18n import LANGUAGES, language, set_language, tr
from wowbot.mobicons import DEFAULT_FAMILY, MobIcons, family_for
from wowbot.protocol_profile import (
    DEFAULT_OS,
    DEFAULT_PLATFORM,
    DEFAULT_VERSION,
    Profile,
    apply_config,
    parse,
    template,
)
from wowbot.world import WorldError

APP_NAME = "WowFarmBot"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATE = "%H:%M:%S"
MAX_LOG_LINES = 4000

COLOR_INFO = "#d8dee9"
COLOR_DEBUG = "#7d8590"
COLOR_WARN = "#e5b567"
COLOR_ERROR = "#e06c75"
COLOR_KILL = "#98c379"
COLOR_LOOT = "#61afef"


# ---------------------------------------------------------------------- Pfade
def support_dir() -> Path:
    """Ordner für Config und Logdateien (überlebt Updates der App)."""
    path = Path.home() / "Library" / "Application Support" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_path(name: str) -> Path | None:
    """Datei, die mit in das App-Bündel gepackt wurde."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate
    candidate = Path(__file__).resolve().parent / name
    return candidate if candidate.exists() else None


def seed_candidates() -> list[Path]:
    """Vorhandene config.json, die beim ersten Start übernommen wird."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        # …/WowFarmBot.app/Contents/MacOS/WowFarmBot
        # parents[3] = Ordner mit der App (dist), parents[4] = Projektordner
        exe = Path(sys.executable).resolve()
        for depth in (3, 4):
            if len(exe.parents) > depth:
                candidates.append(exe.parents[depth] / "config.json")
    candidates.append(Path(__file__).resolve().parent / "config.json")
    candidates.append(Path.cwd() / "config.json")
    return candidates


class ConfigFile:
    """config.json im Benutzerordner, beim ersten Start aus der App gefüllt."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                # Kaputte Datei sichern statt ueberschreiben: darin stehen Konto
                # und alle Einstellungen, die man von Hand retten kann.
                backup = self.path.with_suffix(
                    self.path.suffix + f".defekt-{time.strftime('%Y%m%d-%H%M%S')}"
                )
                try:
                    self.path.replace(backup)
                except OSError:
                    backup = None
                logging.getLogger("wowbot").warning(
                    "Config %s ist nicht lesbar (%s) - %s",
                    self.path,
                    exc,
                    f"alte Datei gesichert als {backup.name}" if backup else "benutze eine neue",
                )
        data = self._seed()
        self.save(data)
        return data

    def _seed(self) -> dict:
        for candidate in seed_candidates():
            if candidate == self.path or not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
        example = bundled_path("config.example.json")
        if example is not None:
            try:
                with example.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "server": {"host": "127.0.0.1", "auth_port": 3724, "realm": ""},
            "account": {"username": "", "password": ""},
            "character": "",
            "behavior": {},
        }

    def save(self, data: dict) -> None:
        """Schreibt die Config atomar: erst daneben, dann ersetzen.

        Bricht das Programm oder der Rechner mitten im Schreiben ab, bleibt die
        alte Datei heil statt halb geschrieben (das wuerde die Config unlesbar
        machen - inklusive Konto).
        """
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


# -------------------------------------------------------------------- Logging
class QueueLogHandler(logging.Handler):
    """Schiebt jede Logzeile in eine Queue, die das Fenster abholt."""

    def __init__(self, sink: "queue.Queue") -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait((record.levelno, self.format(record)))
        except Exception:  # noqa: BLE001 - Logging darf das Fenster nicht sprengen
            pass


# ----------------------------------------------------------------- Bot-Thread
class BotRunner:
    """Führt den Bot in einem eigenen Thread mit eigenem Event-Loop aus."""

    def __init__(self) -> None:
        self.thread: "threading.Thread | None" = None
        self.loop = None
        self.task = None
        self.bot = None
        self._config: dict = {}
        self._trace = False
        self._stop_requested = False

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, config: dict, trace: bool = False) -> None:
        self._config = copy.deepcopy(config)
        self._trace = trace
        self._stop_requested = False
        self.thread = threading.Thread(target=self._thread_main, name="wowbot", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Sanft stoppen: Flag setzen und die Farm-Schleife abbrechen."""
        self._stop_requested = True
        bot = self.bot
        if bot is not None:
            bot.running = False
        loop = self.loop
        if loop is None:
            return

        def _cancel() -> None:
            if self.task is not None and not self.task.done():
                self.task.cancel()

        try:
            loop.call_soon_threadsafe(_cancel)
        except RuntimeError:
            pass  # Loop ist schon geschlossen

    def map_view(self) -> "dict | None":
        """Daten für die Kartenansicht (nie eine Ausnahme nach außen)."""
        bot = self.bot
        if bot is None:
            return None
        try:
            return bot.map_view()
        except Exception:  # noqa: BLE001 - Anzeige darf nie abbrechen
            return None

    def snapshot(self) -> "dict | None":
        """Live-Werte für die Anzeige (nie eine Ausnahme nach außen)."""
        bot = self.bot
        if bot is None:
            return None
        try:
            return bot.snapshot()
        except Exception:  # noqa: BLE001 - Anzeige darf nie abbrechen
            return None

    def set_rules(self, patch: dict) -> None:
        """Geänderte Ziele-/Beuteregeln an den laufenden Bot geben (still, wenn er steht)."""
        bot = self.bot
        if bot is None:
            return
        try:
            bot.set_rules(patch)
        except Exception:  # noqa: BLE001
            pass

    def set_hotspots(self, hotspots: "list[dict]") -> None:
        """Hotspots an den laufenden Bot weitergeben (still, wenn er nicht läuft)."""
        bot = self.bot
        if bot is None:
            return
        try:
            bot.set_hotspots(hotspots)
        except Exception:  # noqa: BLE001
            pass

    def set_command(self, command: "dict | None") -> None:
        """Kartenbefehl (hinlaufen/angreifen) an den laufenden Bot geben."""
        bot = self.bot
        if bot is None:
            return
        try:
            bot.set_command(command)
        except Exception:  # noqa: BLE001
            pass

    def reset_obstacles(self) -> int:
        """Hindernisse der aktuellen Karte löschen; -1, wenn der Bot nicht läuft."""
        bot = self.bot
        if bot is None:
            return -1
        try:
            return bot.forget_obstacles()
        except Exception:  # noqa: BLE001 - GUI darf nicht sterben
            return -1

    # ------------------------------------------------------------------ Thread
    def _thread_main(self) -> None:
        log = logging.getLogger("wowbot")
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            # Protokoll-Einstellungen (Version, Build, Opcodes) gelten ab hier
            apply_config(self._config)
            self.bot = FarmBot(self._config, trace=self._trace)
            self.task = loop.create_task(self._connect_and_run())
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            log.info("Bot gestoppt")
        except LogonError as exc:
            log.error("%s", exc)
        except WorldError as exc:
            log.warning("Verbindung beendet: %s", exc)
        except Exception as exc:  # noqa: BLE001 - Fehler soll im Fenster stehen
            log.error("Bot abgebrochen: %s", exc, exc_info=True)
        finally:
            try:
                if self.bot is not None:
                    loop.run_until_complete(self.bot.close())
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self.loop = None
            self.task = None
            self.bot = None
            log.info("Bot beendet")

    async def _connect_and_run(self) -> None:
        assert self.bot is not None
        await self.bot.connect()
        if self._stop_requested:
            return
        await self.bot.run()


# ------------------------------------------------------------------------ Karte
class MapWorker:
    """Baut die Zonenbilder aus dem Client - im Hintergrund, MPQ-Lesen dauert.

    Die Oberfläche schickt nur Karte und Position, zurück kommt das fertige
    Bild samt Kalibrierung. Gebaut wird pro Zone genau einmal, danach liegt es
    als PNG im Zwischenspeicher.
    """

    def __init__(self, client_dir: "str | Path | None", cache_dir: Path) -> None:
        self.client_dir = client_dir
        self.cache_dir = cache_dir
        self.requests: "queue.Queue" = queue.Queue()
        self.tiles: "queue.Queue" = queue.Queue()
        self.icons: "queue.Queue" = queue.Queue()
        self.results: "queue.Queue" = queue.Queue()
        self._icons: "MobIcons | None" = None
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Thread beenden (z. B. wenn ein anderer Client-Ordner gewaehlt wird)."""
        self._stopped = True

    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stopped

    def request(self, map_id: int, x: float, y: float) -> None:
        # Immer nur die neueste Position - ältere Anfragen verwirft der Bot nicht
        while True:
            try:
                self.requests.get_nowait()
            except queue.Empty:
                break
        self.requests.put((int(map_id), float(x), float(y)))

    def request_tile(self, map_id: int, tx: int, ty: int) -> None:
        self.tiles.put((int(map_id), int(tx), int(ty)))

    def request_icon(self, entry: int, name: str, npc: bool = False) -> None:
        """Symbol für eine Kreatur holen (aus dem Client, sonst Platzhalter)."""
        self.icons.put((int(entry), str(name or ""), bool(npc)))

    def _run(self) -> None:
        maps = ClientMaps(self.client_dir, self.cache_dir)
        minimap: "MinimapTiles | None" = None
        while not self._stopped:
            kind = "zone"
            try:
                map_id, x, y = self.requests.get_nowait()
            except queue.Empty:
                kind = "icon"
                try:
                    entry, mob_name, mob_npc = self.icons.get_nowait()
                except queue.Empty:
                    kind = "tile"
                    try:
                        map_id, x, y = self.tiles.get(timeout=0.3)
                    except queue.Empty:
                        continue
            try:
                if kind == "icon":
                    if self._icons is None:
                        self._icons = MobIcons(self.client_dir, self.cache_dir)
                    path = self._icons.icon_for(mob_name, mob_npc)
                    self.results.put(
                        ("icon", (int(entry), str(path) if path is not None else None))
                    )
                    continue
                if not maps.ready and not maps.open():
                    self.results.put(("fehler", maps.error or "Karten nicht lesbar"))
                    continue
                if kind == "tile":
                    if minimap is None:
                        minimap = MinimapTiles(maps, self.cache_dir)
                    key = (int(map_id), int(x), int(y))
                    path = minimap.path(int(map_id), int(x), int(y))
                    if path is None:
                        self.results.put(("kachel_leer", key))
                    else:
                        self.results.put(("kachel", (key, str(path))))
                    continue
                texture = maps.texture_for(int(map_id), x, y)
                if texture is None:
                    self.results.put(("leer", map_id))
                else:
                    self.results.put(("bild", texture.view()))
            except Exception as exc:  # noqa: BLE001 - Oberfläche darf nie sterben
                self.results.put(("fehler", str(exc)))


# ------------------------------------------------------------- WoW-Bausteine
# Farben wie im Spiel: Leben grün, Mana blau, Erfahrung violett, Ziel rot.
HP_COLORS = ("#8ef07e", "#28b028", "#0a5c0a")
POWER_COLORS = ("#8fd0ff", "#2a6fd0", "#0b2f62")
XP_COLORS = ("#e3aaff", "#9a34cf", "#4a1364")
TARGET_COLORS = ("#ff8f8f", "#c81e1e", "#5e0b0b")
BAR_EMPTY = "#0b0a09"
GOLD = "#c8a86a"
GOLD_BRIGHT = "#ffd100"
PARCHMENT = "#f3ead6"
PARCHMENT_DIM = "#a8967a"
# Freundliche NPCs (Händler, Lehrer, Questgeber, Geistheiler) auf der Karte:
# Blau, weil Grün schon den Spieler markiert.
NPC_FRIENDLY = "#5fb3ff"

# Leder und Gold: das Grundgerüst der übrigen Widgets (Reiter, Knöpfe, Tabellen).
WOW_STYLE = """
QMainWindow, QWidget { background: #12100d; color: #e8dfcc; }
QTabWidget::pane { border: 1px solid #3b2c19; top: -1px; }
QTabBar::tab {
    background: #1a1410; color: #bda57e; border: 1px solid #3b2c19;
    border-bottom: none; padding: 6px 16px; margin-right: 2px;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background: #2b2116; color: #ffd100; }
QTabBar::tab:hover { color: #ffd100; }
QGroupBox {
    border: 1px solid #3b2c19; border-radius: 4px; margin-top: 10px;
    padding-top: 6px; color: #c8a86a; font-weight: bold;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #3a2c1c, stop:1 #1d1710);
    border: 1px solid #6b5433; border-radius: 3px; color: #f3ead6; padding: 4px 12px;
}
QPushButton:hover { border-color: #c8a86a; color: #ffd100; }
QPushButton:pressed { background: #17110c; }
QPushButton:disabled { color: #6d6152; border-color: #3b2c19; }
QLineEdit, QSpinBox {
    background: #0d0b08; border: 1px solid #3b2c19; border-radius: 3px;
    color: #f3ead6; padding: 3px 5px; selection-background-color: #4a3a1f;
}
QTableWidget {
    background: #0d0b08; alternate-background-color: #161210;
    gridline-color: #2a2118; color: #e8dfcc; border: 1px solid #3b2c19;
}
QHeaderView::section {
    background: #241c13; color: #c8a86a; border: 0;
    border-right: 1px solid #3b2c19; border-bottom: 1px solid #3b2c19; padding: 3px;
}
QCheckBox, QRadioButton { color: #e8dfcc; }
QScrollBar:vertical { background: #12100d; width: 12px; }
QScrollBar::handle:vertical { background: #3a2c1c; border-radius: 6px; min-height: 20px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip { background: #0d0b08; color: #f3ead6; border: 1px solid #c8a86a; }
"""


def paint_panel(
    painter: QPainter, rect: QRectF, radius: float = 6.0, dark: bool = False
) -> None:
    """Dunkler Rahmen mit Goldkante - der Grundton der ganzen Oberfläche."""
    grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
    if dark:
        grad.setColorAt(0.0, QColor("#241c14"))
        grad.setColorAt(1.0, QColor("#0e0b08"))
    else:
        grad.setColorAt(0.0, QColor("#342819"))
        grad.setColorAt(0.55, QColor("#241c13"))
        grad.setColorAt(1.0, QColor("#120e0a"))
    painter.setPen(QPen(QColor("#080605"), 1))
    painter.setBrush(QBrush(grad))
    painter.drawRoundedRect(rect, radius, radius)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(GOLD), 1))
    painter.drawRoundedRect(rect.adjusted(1.2, 1.2, -1.2, -1.2), radius - 1, radius - 1)
    # feine Lichtkante innen, damit der Rahmen nicht flach wirkt
    painter.setPen(QPen(QColor(255, 240, 200, 26), 1))
    painter.drawRoundedRect(rect.adjusted(2.6, 2.6, -2.6, -2.6), radius - 2, radius - 2)


def _initials(name: "str | None") -> str:
    """Anfangsbuchstaben für das Ersatz-Portrait."""
    text = (name or "").strip()
    if not text or text.lower().startswith("entry"):
        return "?"
    parts = [part for part in text.split() if part]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return text[:2].upper()


def flag_pixmap(code: str, width: int = 22, height: int = 15) -> QPixmap:
    """Kleine Landesflagge zeichnen (keine Bilddateien nötig)."""
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    rect = QRectF(0, 0, width, height)
    if code == "DE":
        third = height / 3.0
        for index, color in enumerate(("#000000", "#dd0000", "#ffce00")):
            painter.fillRect(QRectF(0, index * third, width, third), QColor(color))
    elif code == "GB":
        painter.fillRect(rect, QColor("#012169"))
        painter.setPen(QPen(QColor("#ffffff"), max(2.0, height / 5.0)))
        painter.drawLine(QPointF(0, 0), QPointF(width, height))
        painter.drawLine(QPointF(0, height), QPointF(width, 0))
        painter.setPen(QPen(QColor("#c8102e"), max(1.5, height / 7.0)))
        painter.drawLine(QPointF(0, 0), QPointF(width, height))
        painter.drawLine(QPointF(0, height), QPointF(width, 0))
        painter.setPen(QPen(QColor("#ffffff"), max(2.0, height / 5.0)))
        painter.drawLine(QPointF(width / 2, 0), QPointF(width / 2, height))
        painter.drawLine(QPointF(0, height / 2), QPointF(width, height / 2))
        painter.setPen(QPen(QColor("#c8102e"), max(1.5, height / 7.0)))
        painter.drawLine(QPointF(width / 2, 0), QPointF(width / 2, height))
        painter.drawLine(QPointF(0, height / 2), QPointF(width, height / 2))
    else:
        painter.fillRect(rect, QColor("#3b2c19"))
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 160), 1))
    painter.drawRect(QRectF(0.5, 0.5, width - 1, height - 1))
    painter.end()
    return pixmap


def _unit_state(mob: dict) -> "tuple[str, str]":
    """Zustand einer Einheit auf der Karte: Text (übersetzt) und Farbe."""
    if mob.get("attackable"):
        return (tr("angreifbar"), "#e06c75")
    if mob.get("friendly"):
        return (tr("freundlicher NPC"), NPC_FRIENDLY)
    return (tr("neutral"), "#8f98a3")


def _elide(painter: QPainter, text: str, width: float) -> str:
    """Text auf die verfügbare Breite kürzen."""
    metrics = painter.fontMetrics()
    if metrics.horizontalAdvance(text) <= width:
        return text
    return metrics.elidedText(text, Qt.ElideRight, int(max(10.0, width)))


def _paint_bar(
    painter: QPainter,
    rect: QRectF,
    pct: float,
    colors: "tuple[str, str, str]",
    text: str,
    base_font: QFont,
) -> None:
    """Eine Leiste direkt auf den Painter zeichnen (für Tooltip und Karten)."""
    painter.setPen(QPen(QColor("#060505"), 1))
    painter.setBrush(QColor(BAR_EMPTY))
    painter.drawRoundedRect(rect, 2.0, 2.0)
    inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
    pct = max(0.0, min(100.0, pct))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#070606"))
    painter.drawRect(inner)
    if pct > 0.4:
        fill = QRectF(inner.left(), inner.top(), inner.width() * pct / 100.0, inner.height())
        grad = QLinearGradient(0, fill.top(), 0, fill.bottom())
        grad.setColorAt(0.0, QColor(colors[0]))
        grad.setColorAt(0.5, QColor(colors[1]))
        grad.setColorAt(1.0, QColor(colors[2]))
        painter.setBrush(QBrush(grad))
        painter.drawRect(fill)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(GOLD), 1))
    painter.drawRoundedRect(rect, 2.0, 2.0)
    if text:
        font = QFont(base_font)
        font.setPointSizeF(9.0)
        font.setBold(True)
        painter.setFont(font)
        text_rect = QRectF(rect.left(), rect.top() - 2.0, rect.width(), rect.height() + 4.0)
        painter.setPen(QColor(0, 0, 0, 200))
        painter.drawText(text_rect.translated(0, 1), Qt.AlignCenter, text)
        painter.setPen(QColor(PARCHMENT))
        painter.drawText(text_rect, Qt.AlignCenter, text)


class WowPanel(QWidget):
    """Leerer Container im Goldrahmen (für eigene Inhalte per Layout)."""

    def __init__(self, parent: "QWidget | None" = None, padding: int = 10) -> None:
        super().__init__(parent)
        self.setContentsMargins(padding, padding, padding, padding)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        paint_panel(painter, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class WowBar(QWidget):
    """Lebens-/Mana-/EP-Leiste im Spielstil: Verlauf, Goldrand, Schattentext."""

    def __init__(
        self,
        colors: "tuple[str, str, str]" = HP_COLORS,
        parent: "QWidget | None" = None,
        text_size: float = 9.5,
    ) -> None:
        super().__init__(parent)
        self.colors = colors
        self.text_size = text_size
        self._pct = 0.0
        self._text = ""
        self.setMinimumHeight(16)

    def set_value(self, pct: float, text: str = "") -> None:
        pct = max(0.0, min(100.0, float(pct)))
        if abs(pct - self._pct) < 0.05 and text == self._text:
            return
        self._pct, self._text = pct, text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        painter.setPen(QPen(QColor("#060505"), 1))
        painter.setBrush(QColor(BAR_EMPTY))
        painter.drawRoundedRect(rect, 2.0, 2.0)

        inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#070606"))
        painter.drawRect(inner)

        if self._pct > 0.4 and inner.width() > 1:
            fill = QRectF(
                inner.left(),
                inner.top(),
                inner.width() * self._pct / 100.0,
                inner.height(),
            )
            grad = QLinearGradient(0, fill.top(), 0, fill.bottom())
            grad.setColorAt(0.0, QColor(self.colors[0]))
            grad.setColorAt(0.5, QColor(self.colors[1]))
            grad.setColorAt(1.0, QColor(self.colors[2]))
            painter.setBrush(QBrush(grad))
            painter.drawRect(fill)
            painter.setBrush(QColor(255, 255, 255, 38))
            painter.drawRect(
                QRectF(fill.left(), fill.top(), fill.width(), max(1.0, fill.height() * 0.3))
            )

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(GOLD), 1))
        painter.drawRoundedRect(rect, 2.0, 2.0)

        if self._text:
            font = self.font()
            font.setPointSizeF(self.text_size)
            font.setBold(True)
            painter.setFont(font)
            # Etwas höher als die Leiste, sonst schneidet Qt die Oberkanten ab
            text_rect = QRectF(
                rect.left(), rect.top() - 2.0, rect.width(), rect.height() + 4.0
            )
            painter.setPen(QColor(0, 0, 0, 200))
            painter.drawText(text_rect.translated(0, 1), Qt.AlignCenter, self._text)
            painter.setPen(QColor(PARCHMENT))
            painter.drawText(text_rect, Qt.AlignCenter, self._text)


class UnitPortrait(QWidget):
    """Rundes Portrait mit Goldring und Stufe in der Ecke."""

    def __init__(self, side: int = 52, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self.side = side
        self.setFixedSize(side, side)
        self._pixmap: "QPixmap | None" = None
        self._level = 0
        self._initials = "?"

    def set_portrait(
        self, pixmap: "QPixmap | None", level: int = 0, initials: str = "?"
    ) -> None:
        level = int(level or 0)
        initials = (initials or "?")[:2]
        if pixmap is self._pixmap and level == self._level and initials == self._initials:
            return  # nichts geändert - kein Neuzeichnen nötig
        self._pixmap = pixmap
        self._level = level
        self._initials = initials
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        side = float(min(self.width(), self.height()))
        disc = QRectF(1.5, 1.5, side - 3.0, side - 3.0)

        gradient = QLinearGradient(0, disc.top(), 0, disc.bottom())
        gradient.setColorAt(0.0, QColor("#3b2f1f"))
        gradient.setColorAt(1.0, QColor("#0d0a07"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(disc)

        if self._pixmap is not None and not self._pixmap.isNull():
            path = QPainterPath()
            path.addEllipse(disc.adjusted(2.0, 2.0, -2.0, -2.0))
            painter.setClipPath(path)
            painter.drawPixmap(
                disc.adjusted(2.0, 2.0, -2.0, -2.0).toRect(), self._pixmap
            )
            painter.setClipping(False)
        else:
            font = self.font()
            font.setPointSizeF(side * 0.34)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(GOLD))
            painter.drawText(disc, Qt.AlignCenter, self._initials)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#0a0806"), 2))
        painter.drawEllipse(disc)
        painter.setPen(QPen(QColor(GOLD), 1.5))
        painter.drawEllipse(disc.adjusted(1.2, 1.2, -1.2, -1.2))

        if self._level:
            font = self.font()
            font.setPointSizeF(8.0)
            font.setBold(True)
            painter.setFont(font)
            badge = QRectF(disc.right() - 15, disc.bottom() - 15, 16, 14)
            painter.setBrush(QColor("#0b0906"))
            painter.setPen(QPen(QColor(GOLD), 1))
            painter.drawRoundedRect(badge, 3, 3)
            painter.setPen(QColor(GOLD_BRIGHT))
            painter.drawText(badge, Qt.AlignCenter, str(self._level))


class UnitFrame(WowPanel):
    """Einheiten-Fenster oben im Karten-Reiter (Spieler oder Ziel)."""

    def __init__(
        self,
        title: str,
        hp_colors: "tuple[str, str, str]" = HP_COLORS,
        power_colors: "tuple[str, str, str] | None" = POWER_COLORS,
        with_xp: bool = False,
        parent: "QWidget | None" = None,
    ) -> None:
        super().__init__(parent, padding=9)
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 8, 10, 8)
        row.setSpacing(9)

        self.portrait = UnitPortrait(54)
        row.addWidget(self.portrait, 0, Qt.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(3)
        # Inhalt mittig halten, wenn das Feld (wegen der Zähler) höher wird
        column.addStretch(0)
        self.name_label = QLabel(title)
        name_font = self.name_label.font()
        name_font.setBold(True)
        name_font.setPointSizeF(name_font.pointSizeF() + 1.0)
        self.name_label.setFont(name_font)
        self.name_label.setStyleSheet(f"color: {GOLD_BRIGHT};")
        column.addWidget(self.name_label)

        self.hp = WowBar(hp_colors)
        self.hp.setFixedHeight(19)
        column.addWidget(self.hp)

        self.power: "WowBar | None" = None
        if power_colors is not None:
            self.power = WowBar(power_colors)
            self.power.setFixedHeight(16)
            column.addWidget(self.power)

        self.xp: "WowBar | None" = None
        if with_xp:
            self.xp = WowBar(XP_COLORS, text_size=8.0)
            self.xp.setFixedHeight(16)
            column.addWidget(self.xp)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet(f"color: {PARCHMENT_DIM};")
        column.addWidget(self.detail_label)
        column.addStretch(0)
        row.addLayout(column, 1)

    def set_identity(
        self,
        name: str,
        level: int,
        pixmap: "QPixmap | None" = None,
        initials: "str | None" = None,
    ) -> None:
        text = name if not level else tr("{name}  ·  Stufe {level}", name=name, level=level)
        if self.name_label.text() != text:
            self.name_label.setText(text)
        self.portrait.set_portrait(pixmap, level, initials or _initials(name))


class MapCanvas(QWidget):
    """Zeichnet die Client-Karte samt gelernten Hindernissen, Spieler und Mobs."""

    GRID = 25.0
    MAX_ZOOM = 40000.0  # reicht für die Kontinentkarte (Azeroth ~40.000 Yard)
    # Linksklick auf freie Fläche = hinlaufen, auf einen Mob = angreifen,
    # Shift+Linksklick = Hotspot setzen, Rechtsklick = Hotspot löschen.
    goto_clicked = Signal(float, float)
    mob_clicked = Signal(int)
    hotspot_clicked = Signal(float, float)
    right_clicked = Signal(float, float)

    def __init__(self, parent: "QWidget | None" = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        self.view: "dict | None" = None
        self.hotspots: "list[dict]" = []
        self.zoom = 800.0  # sichtbare Breite in Yard (Kacheln bleiben hier scharf)
        self.texture: "QPixmap | None" = None
        self.zone: "dict | None" = None
        # Geländekacheln: (tx, ty) -> Bild. Liegt deckungsgleich auf der Welt.
        self.tiles: "dict[tuple[int, int], QPixmap]" = {}
        # Symbol je Mob-Entry (aus dem Client) und der gerade überfahrene Mob
        self.icons: "dict[int, QPixmap]" = {}
        self._hover: "dict | None" = None
        self._hover_pos = QPointF(0.0, 0.0)
        self.setMouseTracking(True)

    def set_view(self, view: "dict | None") -> None:
        self.view = view
        # Der überfahrene Mob wird jede Runde neu gesucht: er kann weglaufen,
        # sterben oder die Karte gewechselt haben.
        if self._hover is not None:
            guid = self._hover.get("guid")
            self._hover = None
            for mob in (view or {}).get("mobs") or ():
                if mob.get("guid") == guid:
                    self._hover = mob
                    break
            self.setCursor(
                Qt.PointingHandCursor if self._hover else Qt.ArrowCursor
            )
        self.update()

    def set_tiles(self, tiles: "dict[tuple[int, int], QPixmap]") -> None:
        self.tiles = tiles
        self.update()

    def set_hotspots(self, hotspots: "list[dict]") -> None:
        self.hotspots = list(hotspots)
        self.update()

    def set_texture(self, texture: "QPixmap | None", zone: "dict | None") -> None:
        self.texture = texture
        self.zone = zone
        self.update()

    def set_icons(self, icons: "dict[int, QPixmap]") -> None:
        """Symbole der Mobs übernehmen (kommen aus dem Karten-Thread)."""
        self.icons = icons
        self.update()

    def _to_world(self, screen_x: float, screen_y: float) -> "tuple[float, float] | None":
        """Bildschirm- zurück in Weltkoordinaten (unabhängig vom letzten Zeichnen)."""
        view = self.view
        width, height = self.width(), self.height()
        if not view or not width or not height:
            return None
        scale = width / self.zoom
        pos_x, pos_y = view["pos"]
        return (
            pos_x + (height / 2 - screen_y) / scale,
            pos_y - (screen_x - width / 2) / scale,
        )

    def _screen_of(self, x: float, y: float) -> "tuple[float, float] | None":
        """Weltkoordinaten auf Bildschirmpunkte (wie im Zeichnen)."""
        view = self.view
        width, height = self.width(), self.height()
        if not view or not width:
            return None
        scale = width / self.zoom
        pos_x, pos_y = view["pos"]
        return (width / 2 + (pos_y - y) * scale, height / 2 - (x - pos_x) * scale)

    def _mob_at(self, screen_x: float, screen_y: float) -> "dict | None":
        """Nächster Mob unter dem Zeiger (höchstens 13 Punkte daneben)."""
        view = self.view
        if not view:
            return None
        best: "dict | None" = None
        best_dist = 13.0
        for mob in view.get("mobs") or ():
            point = self._screen_of(mob["x"], mob["y"])
            if point is None:
                continue
            dist = math.hypot(point[0] - screen_x, point[1] - screen_y)
            if dist <= best_dist:
                best, best_dist = mob, dist
        return best

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        pos = event.position()
        mob = self._mob_at(pos.x(), pos.y())
        if mob is self._hover:
            return
        self._hover = mob
        self.setCursor(Qt.PointingHandCursor if mob is not None else Qt.ArrowCursor)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        if self._hover is not None:
            self._hover = None
            self.unsetCursor()
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        world = self._to_world(event.position().x(), event.position().y())
        if world is None:
            return
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ShiftModifier:
                self.hotspot_clicked.emit(world[0], world[1])
                return
            mob = self._mob_at(event.position().x(), event.position().y())
            if mob is not None:
                self.mob_clicked.emit(int(mob["guid"]))
                return
            self.goto_clicked.emit(world[0], world[1])
        elif event.button() == Qt.RightButton:
            self.right_clicked.emit(world[0], world[1])

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        delta = event.angleDelta().y()
        if not delta:
            return
        factor = 1.2 if delta > 0 else 0.83
        self.zoom = max(60.0, min(self.MAX_ZOOM, self.zoom / factor))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt-Signatur
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#12141a"))
        view = self.view
        if not view:
            painter.setPen(QColor("#7d8590"))
            painter.drawText(self.rect(), Qt.AlignCenter, tr("Bot nicht verbunden"))
            return

        width, height = self.width(), self.height()
        pos_x, pos_y = view["pos"]
        scale = width / self.zoom

        def to_screen(x: float, y: float) -> tuple[float, float]:
            # Wie die Weltkarte: horizontal ist die West-Ost-Achse (y),
            # vertikal die Nord-Süd-Achse (x, oben = Norden).
            return (width / 2 + (pos_y - y) * scale, height / 2 - (x - pos_x) * scale)

        half_y = self.zoom / 2.0
        half_x = half_y * height / width if width else half_y

        # Zonenkarte aus dem Client als Hintergrund - nur der sichtbare Teil
        if self.texture is not None and self.zone:
            zone = self.zone
            left_top = to_screen(zone["top"], zone["left"])
            right_bottom = to_screen(zone["bottom"], zone["right"])
            area = QRectF(
                min(left_top[0], right_bottom[0]),
                min(left_top[1], right_bottom[1]),
                abs(right_bottom[0] - left_top[0]),
                abs(right_bottom[1] - left_top[1]),
            )
            visible = area.intersected(QRectF(self.rect()))
            if visible.width() > 0 and visible.height() > 0 and area.width() > 0:
                tex_w, tex_h = self.texture.width(), self.texture.height()
                source = QRectF(
                    (visible.left() - area.left()) / area.width() * tex_w,
                    (visible.top() - area.top()) / area.height() * tex_h,
                    visible.width() / area.width() * tex_w,
                    visible.height() / area.height() * tex_h,
                )
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                # Nah am Boden ist die Zonenkarte nur eine Farbe (ein Pixel ~ 5 Yard),
                # deshalb wird sie mit dem Herauszoomen kräftiger.
                strength = max(0.0, min(1.0, (self.zoom - 300.0) / 700.0))
                painter.setOpacity(0.4 + 0.55 * strength)
                painter.drawPixmap(visible, self.texture, source)
                painter.setOpacity(1.0)
                # Etwas abdunkeln, damit Markierungen auf dem Pergament lesbar bleiben
                painter.fillRect(visible, QColor(8, 10, 16, 70))

        # Geländekacheln aus dem Client (256 px je 533 Yard) - deckungsgleich
        # auf der Welt, dadurch sitzt jede Position pixelgenau auf ihrem Bild.
        if self.tiles:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            window = QRectF(self.rect())
            for (tx, ty), pixmap in self.tiles.items():
                east = WORLD_HALF - tx * TILE_SIZE  # Achse y (West-Ost)
                north = WORLD_HALF - ty * TILE_SIZE  # Achse x (Nord-Süd)
                x0, y0 = to_screen(north, east)
                x1, y1 = to_screen(north - TILE_SIZE, east - TILE_SIZE)
                area = QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
                if not area.intersects(window):
                    continue
                painter.drawPixmap(
                    area, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height())
                )

        # Raster alle 25 Yard - nur solange kein Geländebild darunter liegt
        has_ground = bool(self.tiles) or self.texture is not None
        painter.setPen(QPen(QColor(255, 255, 255, 18) if has_ground else QColor("#1c2029"), 1))
        line_y = math.floor((pos_y - half_y) / self.GRID) * self.GRID
        while line_y <= pos_y + half_y:
            sx, _ = to_screen(pos_x, line_y)
            painter.drawLine(int(sx), 0, int(sx), height)
            line_y += self.GRID
        line_x = math.floor((pos_x - half_x) / self.GRID) * self.GRID
        while line_x <= pos_x + half_x:
            _, sy = to_screen(line_x, pos_y)
            painter.drawLine(0, int(sy), width, int(sy))
            line_x += self.GRID

        # Hindernisse (ohne Glättung, sonst entstehen Nähte zwischen den Zellen)
        cell = float(view.get("cell_size", 5.0))
        side = max(2.0, cell * scale)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#c9553d"))
        for wall_x, wall_y in view["walls"]:
            if abs(wall_x - pos_x) > half_x + cell or abs(wall_y - pos_y) > half_y + cell:
                continue
            sx, sy = to_screen(wall_x, wall_y)
            painter.drawRect(QRectF(sx - side / 2, sy - side / 2, side, side))

        # Hotspots und die Route dazwischen
        if self.hotspots:
            points = [to_screen(spot["x"], spot["y"]) for spot in self.hotspots]
            if len(points) > 1:
                painter.setPen(QPen(QColor("#3fa7d6"), 1, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawPolyline(
                    QPolygonF([QPointF(px, py) for px, py in points + [points[0]]])
                )
            for index, (sx, sy) in enumerate(points, start=1):
                painter.setPen(QPen(QColor("#12141a"), 1))
                painter.setBrush(QColor("#3fa7d6"))
                painter.drawEllipse(QPointF(sx, sy), 6.0, 6.0)
                painter.setPen(QColor("#0d1117"))
                painter.drawText(
                    QRectF(sx - 6, sy - 6, 12, 12), Qt.AlignCenter, str(index)
                )

        painter.setRenderHint(QPainter.Antialiasing, True)

        # Kartenbefehl: Zielkreuz, beim Hinlaufen zusätzlich die Linie dorthin
        command = view.get("command")
        if command:
            cx, cy = to_screen(command["x"], command["y"])
            befehlsfarbe = (
                QColor(GOLD_BRIGHT) if command.get("kind") == "goto" else QColor("#ff8b6b")
            )
            if command.get("kind") == "goto":
                px, py = to_screen(pos_x, pos_y)
                painter.setPen(QPen(befehlsfarbe, 1, Qt.DashLine))
                painter.drawLine(QPointF(px, py), QPointF(cx, cy))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(befehlsfarbe, 2))
            painter.drawEllipse(QPointF(cx, cy), 7.0, 7.0)
            painter.drawLine(QPointF(cx - 11, cy), QPointF(cx + 11, cy))
            painter.drawLine(QPointF(cx, cy - 11), QPointF(cx, cy + 11))

        # Einheiten: rot = angreifbar, grau = neutral, blau = freundlicher NPC
        hover_guid = self._hover.get("guid") if self._hover else None
        for mob in view["mobs"]:
            sx, sy = to_screen(mob["x"], mob["y"])
            state, state_color = _unit_state(mob)
            hovered = hover_guid is not None and mob.get("guid") == hover_guid
            dot = QColor(state_color)
            if mob.get("attackable"):
                label_color = PARCHMENT
            elif mob.get("friendly"):
                label_color = dot.lighter(125).name()
            else:
                label_color = "#c3c9d1"
            painter.setPen(QPen(QColor("#0b0906"), 1))
            painter.setBrush(dot)
            painter.drawEllipse(QPointF(sx, sy), 4.5 if hovered else 3.0, 4.5 if hovered else 3.0)
            if hovered:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(GOLD_BRIGHT), 1.5))
                painter.drawEllipse(QPointF(sx, sy), 8.5, 8.5)
            # Nah herangezoomt: Name neben dem Punkt, damit man sieht, wer da läuft
            if self.zoom <= 130.0:
                label = str(mob.get("name") or "")
                if label and not label.lower().startswith("entry"):
                    painter.save()
                    font = self.font()
                    font.setPointSizeF(7.5)
                    font.setBold(hovered)
                    painter.setFont(font)
                    painter.setPen(QColor(0, 0, 0, 200))
                    painter.drawText(QPointF(sx + 9, sy - 5), label)
                    painter.setPen(
                        QColor(GOLD_BRIGHT) if hovered else QColor(label_color)
                    )
                    painter.drawText(QPointF(sx + 8, sy - 6), label)
                    painter.restore()

        # Ziel
        if view["target"]:
            sx, sy = to_screen(*view["target"])
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#e5b567"), 2))
            painter.drawEllipse(QPointF(sx, sy), 7.0, 7.0)

        # Spieler als Pfeil in Blickrichtung
        heading = float(view["heading"])
        forward_x, forward_y = math.cos(heading), -math.sin(heading)
        left_x, left_y = -forward_y, forward_x
        sx, sy = to_screen(pos_x, pos_y)
        painter.setBrush(QColor("#98c379"))
        painter.setPen(QPen(QColor("#12141a"), 1))
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(sx + forward_x * 10, sy + forward_y * 10),
                    QPointF(
                        sx - forward_x * 6 + left_x * 6,
                        sy - forward_y * 6 + left_y * 6,
                    ),
                    QPointF(
                        sx - forward_x * 6 - left_x * 6,
                        sy - forward_y * 6 - left_y * 6,
                    ),
                ]
            )
        )

        if not view["walls"]:
            painter.setPen(QColor("#7d8590"))
            painter.drawText(
                self.rect().adjusted(0, 0, 0, -24),
                Qt.AlignHCenter | Qt.AlignBottom,
                tr("noch keine Hindernisse gelernt"),
            )

        # Legende unten rechts
        legend = (
            ("#98c379", tr("Spieler")),
            ("#e06c75", tr("Mob angreifbar")),
            ("#8f98a3", tr("neutral")),
            (NPC_FRIENDLY, tr("freundlicher NPC")),
            ("#e5b567", tr("Ziel")),
            ("#ffd100", tr("Kartenbefehl")),
            ("#3fa7d6", tr("Hotspot")),
        )
        text_y = height - 12 - (len(legend) - 1) * 14
        for color, label in legend:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRect(width - 148, text_y - 7, 9, 9)
            painter.setPen(QColor("#9aa3ad"))
            painter.drawText(width - 133, text_y, label)
            text_y += 14

        self._paint_tooltip(painter, to_screen)

    # ------------------------------------------------- Mob-Tooltip (Hover)
    def _paint_tooltip(self, painter: QPainter, to_screen) -> None:
        """Spielstil-Tooltip für den Mob unter dem Zeiger."""
        mob = self._hover
        if not mob:
            return
        anchor = to_screen(mob["x"], mob["y"])
        # Breite reicht für die längste Fußzeile ("… · freundlicher NPC ·
        # aktuelles Ziel") - sonst würde der Zustand abgeschnitten.
        width, height = 300.0, 120.0
        left = anchor[0] + 16.0
        top = anchor[1] - height / 2.0
        left = min(max(6.0, left), max(6.0, self.width() - width - 6.0))
        top = min(max(6.0, top), max(6.0, self.height() - height - 6.0))
        panel = QRectF(left, top, width, height)

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 130))
        painter.drawRoundedRect(panel.translated(2.0, 3.0), 6, 6)
        paint_panel(painter, panel, radius=6.0, dark=True)

        icon_rect = QRectF(panel.left() + 10, panel.top() + 10, 48, 48)
        entry = int(mob.get("entry") or 0)
        pixmap = self.icons.get(entry)
        if pixmap is not None and not pixmap.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(icon_rect, 3, 3)
            painter.save()
            painter.setClipPath(clip)
            painter.drawPixmap(icon_rect.toRect(), pixmap)
            painter.restore()
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#1b1510"))
            painter.drawRoundedRect(icon_rect, 3, 3)
            font = self.font()
            font.setBold(True)
            font.setPointSizeF(15.0)
            painter.setFont(font)
            painter.setPen(QColor(GOLD))
            painter.drawText(icon_rect, Qt.AlignCenter, _initials(mob.get("name")))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(GOLD), 1))
        painter.drawRoundedRect(icon_rect, 3, 3)

        text_left = icon_rect.right() + 10
        text_width = panel.right() - 10 - text_left
        name = str(mob.get("name") or f"Entry {entry}")
        name_rect = QRectF(text_left, panel.top() + 8, text_width, 22)
        font = self.font()
        font.setBold(True)
        font.setPointSizeF(11.5)
        painter.setFont(font)
        painter.setPen(QColor(0, 0, 0, 200))
        painter.drawText(
            name_rect.translated(0, 1), Qt.AlignLeft | Qt.AlignVCenter, _elide(painter, name, text_width)
        )
        state, state_color = _unit_state(mob)
        if mob.get("attackable"):
            painter.setPen(QColor(GOLD_BRIGHT))
        elif state == "freundlicher NPC":
            painter.setPen(QColor(NPC_FRIENDLY))
        else:
            painter.setPen(QColor("#cfd4da"))
        painter.drawText(
            name_rect, Qt.AlignLeft | Qt.AlignVCenter, _elide(painter, name, text_width)
        )

        font.setBold(False)
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(QColor(PARCHMENT_DIM))
        level = mob.get("level") or 0
        painter.drawText(
            QRectF(text_left, name_rect.bottom() + 1, text_width, 14),
            Qt.AlignLeft | Qt.AlignVCenter,
            tr("Stufe {level}   ·   Entry {entry}", level=level if level else "?", entry=entry),
        )

        bar = QRectF(panel.left() + 10, panel.top() + 66, panel.width() - 20, 18)
        max_hp = int(mob.get("max_hp") or 0)
        hp_text = (
            f"{int(mob.get('hp') or 0)}/{max_hp}   ({mob.get('hp_pct', 0.0):.0f} %)"
            if max_hp
            else tr("Leben unbekannt")
        )
        _paint_bar(painter, bar, float(mob.get("hp_pct") or 0.0), TARGET_COLORS, hp_text, self.font())

        # Fußzeile: Entfernung, dann der Zustand in seiner Kartenfarbe
        foot = QRectF(bar.left(), bar.bottom() + 4, bar.width(), 16)
        pieces: "list[tuple[str, str]]" = [
            (f"{float(mob.get('dist') or 0.0):.0f} yd", PARCHMENT_DIM),
            (state, state_color),
        ]
        if mob.get("target"):
            pieces.append((tr("aktuelles Ziel"), PARCHMENT_DIM))
        elif mob.get("attackable") and not mob.get("allowed", True):
            # Nur angreifbare Einheiten können ein Ziel sein - bei NPCs wäre
            # der Hinweis nur verwirrend (und würde die Zeile sprengen).
            pieces.append((tr("nach Regeln kein Ziel"), PARCHMENT_DIM))
        font.setPointSizeF(9.0)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        def line_width(items: "list[tuple[str, str]]") -> float:
            return sum(
                metrics.horizontalAdvance(("   ·   " if index else "") + text)
                for index, (text, _) in enumerate(items)
            )

        while len(pieces) > 2 and line_width(pieces) > foot.width():
            pieces.pop()
        cursor_x = foot.left()
        for index, (text, color) in enumerate(pieces):
            chunk = ("   ·   " if index else "") + text
            painter.setPen(QColor(color))
            painter.drawText(
                QRectF(cursor_x, foot.top(), foot.right() - cursor_x, foot.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                chunk,
            )
            cursor_x += metrics.horizontalAdvance(chunk)


# ---------------------------------------------------------------------- Fenster
class WowFarmBotWindow(QMainWindow):
    def __init__(self, config: ConfigFile) -> None:
        super().__init__()
        self.config = config
        # Sprache vor dem Aufbau setzen, damit alle Texte gleich richtig kommen
        set_language(config.data.get("language"))
        self.runner = BotRunner()
        # Start/Stopp gibt es in zwei Reitern - beide Sätze werden mitgezogen
        self._start_buttons: "list[QPushButton]" = []
        self._stop_buttons: "list[QPushButton]" = []
        self.log_queue: "queue.Queue" = queue.Queue()
        self.log_file = support_dir() / "logs" / time.strftime("bot-%Y%m%d-%H%M%S.log")
        self._was_running = False
        self.hotspots = [
            {"x": float(spot["x"]), "y": float(spot["y"])}
            for spot in (config.data.get("behavior", {}) or {}).get("hotspots") or []
            if isinstance(spot, dict) and "x" in spot and "y" in spot
        ]
        # Karten-Texturen aus dem WoW-Client (optional)
        map_config = (config.data.get("map", {}) or {})
        self.map_cache_dir = support_dir() / "maps"
        self.map_client = find_client_dir(map_config.get("client"))
        self.map_worker: "MapWorker | None" = None
        self.map_zone: "dict | None" = None
        self.map_pixmaps: "dict[str, QPixmap]" = {}
        # Geländekacheln: (Karte, tx, ty) -> Bild; dazu offene und fehlende Felder
        self.ground_pixmaps: "dict[tuple[int, int, int], QPixmap]" = {}
        self._ground_pending: "set[tuple[int, int, int]]" = set()
        self._ground_absent: "set[tuple[int, int, int]]" = set()
        self.map_error: "str | None" = None
        self.map_message = ""
        self._map_request = 0.0
        # Reiter „Ziele & Beute“: bekannte Namen und die letzte Umgebungsliste
        self._mob_catalog: "dict[int, str]" = {}
        self._item_names: "dict[int, str]" = {}
        self._last_mobs: "list[dict]" = []
        # Mob-Symbole aus dem Client (Entry -> Bild), vom Karten-Thread geholt
        self.mob_icons: "dict[int, QPixmap]" = {}
        self._last_map_mobs: "list[dict]" = []
        self._icon_pending: "set[int]" = set()
        self._icon_absent: "set[int]" = set()
        self._icon_family: "dict[int, str]" = {}
        # bewusst gelöschte Zeilen nicht sofort wieder einblenden
        self._hidden_mobs: "set[int]" = set()
        # zuletzt an den Bot gegebene Regeln (nur bei Änderung senden)
        self._rules_sent: "str | None" = None

        self.setWindowTitle(APP_NAME)
        self.setStyleSheet(WOW_STYLE)
        self.resize(1020, 740)
        self._build()
        self._i18n_collect()
        self._retranslate()
        self._setup_logging()
        self._start_map_worker()

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    # ------------------------------------------------------------- Sprachen
    def _i18n_collect(self) -> None:
        """Deutsche Originaltexte merken - Grundlage für den Sprachwechsel.

        Die Oberfläche wird einmal deutsch aufgebaut; jedes Widget merkt sich
        seinen Text als Eigenschaft. `_retranslate` tauscht danach nur die
        Beschriftung aus, ohne die Oberfläche neu zu bauen.
        """
        self._tab_keys = [self.tabs.tabText(i) for i in range(self.tabs.count())]
        # Eingabefelder zeigen Nutzerdaten - ihr Inhalt darf nie übersetzt werden.
        editable = (QLineEdit, QPlainTextEdit)
        for widget in self.findChildren(QWidget):
            if not isinstance(widget, editable):
                # Nur Widgets mit echter Beschriftung (QSpinBox hat text(), aber
                # kein setText).
                text = getattr(widget, "text", None)
                if callable(text) and hasattr(widget, "setText"):
                    value = text()
                    if value:
                        widget.setProperty("i18n_text", value)
            title = getattr(widget, "title", None)
            if callable(title) and hasattr(widget, "setTitle"):
                value = title()
                if value:
                    widget.setProperty("i18n_title", value)
            placeholder = getattr(widget, "placeholderText", None)
            if callable(placeholder) and hasattr(widget, "setPlaceholderText"):
                value = placeholder()
                if value:
                    widget.setProperty("i18n_placeholder", value)
            tip = widget.toolTip()
            if tip:
                widget.setProperty("i18n_tip", tip)
        for table in self.findChildren(QTableWidget):
            keys = []
            for column in range(table.columnCount()):
                item = table.horizontalHeaderItem(column)
                keys.append(item.text() if item is not None else "")
            table.setProperty("i18n_headers", "\u0001".join(keys))

    def _retranslate(self) -> None:
        """Alle gemerkten Texte in die aktuelle Sprache setzen."""
        for index, key in enumerate(getattr(self, "_tab_keys", [])):
            if index < self.tabs.count():
                self.tabs.setTabText(index, tr(key))
        for widget in self.findChildren(QWidget):
            key = widget.property("i18n_text")
            if isinstance(key, str) and key and hasattr(widget, "setText"):
                widget.setText(tr(key))
            title = widget.property("i18n_title")
            if isinstance(title, str) and title and hasattr(widget, "setTitle"):
                widget.setTitle(tr(title))
            placeholder = widget.property("i18n_placeholder")
            if isinstance(placeholder, str) and placeholder and hasattr(
                widget, "setPlaceholderText"
            ):
                widget.setPlaceholderText(tr(placeholder))
            tip = widget.property("i18n_tip")
            if isinstance(tip, str) and tip:
                widget.setToolTip(tr(tip))
        for table in self.findChildren(QTableWidget):
            keys = table.property("i18n_headers")
            if isinstance(keys, str) and keys:
                for column, key in enumerate(keys.split("\u0001")):
                    item = table.horizontalHeaderItem(column)
                    if item is not None and key:
                        item.setText(tr(key))
        if hasattr(self, "lang_combo"):
            self.lang_combo.setToolTip(tr("Sprache der Oberfläche"))

    def _change_language(self, _index: int) -> None:
        """Sprache wechseln: merken, Texte tauschen, Anzeigen auffrischen."""
        code = self.lang_combo.currentData()
        if not code or code == language():
            return
        set_language(code)
        config = copy.deepcopy(self.config.data)
        config["language"] = code
        try:
            self.config.save(config)
        except OSError:
            pass
        self._retranslate()
        # Anzeigen, die ihren Text selbst zusammenbauen, sofort neu beschriften
        self._proto_from_config()
        self._update_client_label()
        self._update_map()
        if self._last_mobs:
            self._update_nearby(self._last_mobs)
        snapshot = self.runner.snapshot()
        if snapshot is not None:
            self._update_status()

    # ------------------------------------------------------------------- Aufbau
    def _build(self) -> None:
        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)

        bot_page = QWidget(self.tabs)
        layout = QVBoxLayout(bot_page)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(12)
        self.tabs.addTab(bot_page, "Bot")

        # --- Sprache (oben links): Flagge + Name der Sprache
        language_row = QHBoxLayout()
        self.lang_combo = QComboBox()
        self.lang_combo.setFixedWidth(170)
        for code, name, flag in LANGUAGES:
            self.lang_combo.addItem(QIcon(flag_pixmap(flag)), name, code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(language())))
        self.lang_combo.setToolTip("Sprache der Oberfläche")
        self.lang_combo.currentIndexChanged.connect(self._change_language)
        language_row.addWidget(self.lang_combo)
        language_row.addStretch(1)
        layout.addLayout(language_row)

        # --- Knopfleiste
        bar = QHBoxLayout()
        bar.addWidget(self._make_control_buttons())

        clear_btn = QPushButton("Log leeren")
        clear_btn.setAutoDefault(False)
        clear_btn.clicked.connect(self._clear_log)
        bar.addSpacing(12)
        bar.addWidget(clear_btn)

        log_btn = QPushButton("Logdatei zeigen")
        log_btn.setAutoDefault(False)
        log_btn.clicked.connect(self._reveal_log)
        bar.addWidget(log_btn)

        bar.addStretch(1)
        layout.addLayout(bar)

        # --- Zugang
        login_box = QGroupBox("Server und Konto")
        login_grid = QGridLayout(login_box)
        login_grid.setHorizontalSpacing(10)
        login_grid.setVerticalSpacing(8)

        data = self.config.data
        server = data.get("server", {})
        account = data.get("account", {})

        self.host_edit = QLineEdit(str(server.get("host", "")))
        self.port_edit = QLineEdit(str(server.get("auth_port", 3724)))
        self.port_edit.setMaximumWidth(90)
        self.realm_edit = QLineEdit(str(server.get("realm", "")))
        self.user_edit = QLineEdit(str(account.get("username", "")))
        self.pass_edit = QLineEdit(str(account.get("password", "")))
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setMaximumWidth(160)
        self.char_edit = QLineEdit(str(data.get("character", "")))
        self.char_edit.setPlaceholderText("leer = erster Charakter")

        login_grid.addWidget(QLabel("Server"), 0, 0)
        login_grid.addWidget(self.host_edit, 0, 1)
        login_grid.addWidget(QLabel("Port"), 0, 2)
        login_grid.addWidget(self.port_edit, 0, 3)
        login_grid.addWidget(QLabel("Realm"), 0, 4)
        login_grid.addWidget(self.realm_edit, 0, 5)
        login_grid.addWidget(QLabel("Konto"), 1, 0)
        login_grid.addWidget(self.user_edit, 1, 1)
        login_grid.addWidget(QLabel("Passwort"), 1, 2)
        login_grid.addWidget(self.pass_edit, 1, 3)
        login_grid.addWidget(QLabel("Charakter"), 1, 4)
        login_grid.addWidget(self.char_edit, 1, 5)
        login_grid.setColumnStretch(1, 1)
        login_grid.setColumnStretch(5, 1)

        buttons_row = QHBoxLayout()
        save_btn = QPushButton("Speichern")
        save_btn.setAutoDefault(False)
        save_btn.clicked.connect(self._save_config_from_fields)
        buttons_row.addWidget(save_btn)
        open_btn = QPushButton("Config-Datei öffnen")
        open_btn.setAutoDefault(False)
        open_btn.clicked.connect(self._open_config)
        buttons_row.addWidget(open_btn)
        self.trace_check = QCheckBox("Netzwerk-Pakete mitschreiben (DEBUG)")
        buttons_row.addSpacing(12)
        buttons_row.addWidget(self.trace_check)
        buttons_row.addStretch(1)
        login_grid.addLayout(buttons_row, 2, 0, 1, 6)
        layout.addWidget(login_box)

        # --- Mobs in der Nähe
        nearby_box = QGroupBox("Mobs in der Nähe")
        nearby_layout = QVBoxLayout(nearby_box)
        nearby_layout.setContentsMargins(8, 8, 8, 8)
        nearby_layout.setSpacing(4)

        self.nearby_table = QTableWidget(0, 5)
        self.nearby_table.setHorizontalHeaderLabels(
            ["Mob", "Lvl", "Leben", "yd", "angreifbar"]
        )
        self.nearby_table.verticalHeader().setVisible(False)
        self.nearby_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.nearby_table.setSelectionMode(QTableWidget.NoSelection)
        self.nearby_table.setShowGrid(False)
        self.nearby_table.setAlternatingRowColors(True)
        self.nearby_table.setFixedHeight(148)
        header = self.nearby_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
        self.nearby_table.setColumnWidth(1, 50)
        self.nearby_table.setColumnWidth(2, 140)
        self.nearby_table.setColumnWidth(3, 60)
        self.nearby_table.setColumnWidth(4, 95)
        nearby_layout.addWidget(self.nearby_table)

        self.nearby_empty = QLabel("keine Einheiten in der Nähe")
        self.nearby_empty.setStyleSheet("color: #6a737d;")
        nearby_layout.addWidget(self.nearby_empty)
        self.nearby_table.hide()
        layout.addWidget(nearby_box)

        # --- Ereignisse
        log_box = QGroupBox("Ereignisse")
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(8, 8, 8, 8)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(MAX_LOG_LINES)
        self.log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_text.setFont(QFont("Menlo", 11))
        self.log_text.setStyleSheet(
            "QPlainTextEdit { background: #12141a; color: #d8dee9; border: none;"
            " padding: 6px; selection-background-color: #2f3b52; }"
        )
        log_layout.addWidget(self.log_text)
        layout.addWidget(log_box, 1)

        # --- Fußzeile
        foot = QHBoxLayout()
        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #6a737d;")
        foot.addWidget(self.hint_label)
        foot.addStretch(1)
        path_label = QLabel(f"Log: {self.log_file}")
        path_label.setStyleSheet("color: #6a737d;")
        foot.addWidget(path_label)
        layout.addLayout(foot)

        # --- Reiter "Ingame Karte"
        map_page = QWidget(self.tabs)
        map_layout = QVBoxLayout(map_page)
        map_layout.setContentsMargins(12, 12, 12, 10)
        map_layout.setSpacing(8)

        # Der Status wohnt jetzt hier: Spieler, Ziel und Zähler über der Karte.
        self.me_frame = UnitFrame(
            self._character_name(), HP_COLORS, POWER_COLORS, with_xp=True
        )
        self.target_frame = UnitFrame(tr("kein Ziel"), TARGET_COLORS, None)
        hud = QHBoxLayout()
        hud.setSpacing(8)
        hud.addWidget(self.me_frame, 5)
        hud.addWidget(self.target_frame, 5)
        hud.addWidget(self._build_stats_panel(), 4)
        map_layout.addLayout(hud)

        self.map_info = QLabel("Bot nicht verbunden")
        self.map_info.setStyleSheet(f"color: {PARCHMENT_DIM};")
        map_layout.addWidget(self.map_info)

        map_bar = QHBoxLayout()
        map_bar.addWidget(self._make_control_buttons())
        # Client-Ordner steht im Reiter „WoW-Version“ (gehört zur Client-Kennung)
        map_bar.addStretch(1)
        zone_btn = QPushButton("Ganze Zone")
        zone_btn.setAutoDefault(False)
        zone_btn.setToolTip("Auf die ganze Zonenkarte herauszoomen")
        zone_btn.clicked.connect(self._zoom_zone)
        map_bar.addWidget(zone_btn)
        map_layout.addLayout(map_bar)

        self.map_canvas = MapCanvas()
        self.map_canvas.set_hotspots(self.hotspots)
        self.map_canvas.goto_clicked.connect(self._goto_point)
        self.map_canvas.mob_clicked.connect(self._attack_mob)
        self.map_canvas.hotspot_clicked.connect(self._add_hotspot)
        self.map_canvas.right_clicked.connect(self._remove_hotspot)
        map_layout.addWidget(self.map_canvas, 1)
        map_hint = QLabel(
            "Norden oben · Mausrad zoomt · Mobs laufen live mit · Maus über einen Mob = Infofenster · "
            "Linksklick = dorthin laufen (bricht auch einen Kampf ab), Klick auf einen Mob = den zuerst angreifen, "
            "Shift+Klick = Hotspot setzen, Rechtsklick = Hotspot löschen"
        )
        map_hint.setWordWrap(True)
        map_hint.setStyleSheet(f"color: {PARCHMENT_DIM};")
        map_layout.addWidget(map_hint)
        self.tabs.addTab(map_page, "Ingame Karte")

        # --- Reiter „Ziele & Beute" (&& zeigt ein einfaches & im Reiter)
        self.tabs.addTab(self._build_targets_tab(), "Ziele && Beute")
        # --- Reiter „WoW-Version" (Client-Kennung und Protokollzahlen)
        self.tabs.addTab(self._build_protocol_tab(), "WoW-Version")

    # ------------------------------------------------------- HUD im Karten-Reiter
    def _make_control_buttons(self) -> QWidget:
        """Start/Stopp als eigenes Feld - steht im Bot- und im Karten-Reiter.

        Beide Sätze hängen an denselben Methoden und werden gemeinsam
        freigegeben/gesperrt, damit nie zwei widersprüchliche Knöpfe dastehen.
        """
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        start = QPushButton("▶  Start")
        start.setAutoDefault(False)
        start.clicked.connect(self.start_bot)
        row.addWidget(start)

        stop = QPushButton("■  Stopp")
        stop.setAutoDefault(False)
        stop.setEnabled(False)
        stop.clicked.connect(self.stop_bot)
        row.addWidget(stop)

        self._start_buttons.append(start)
        self._stop_buttons.append(stop)
        return holder

    def _set_running_buttons(self, running: bool, stopping: bool = False) -> None:
        for button in self._start_buttons:
            button.setEnabled(not running)
        for button in self._stop_buttons:
            button.setEnabled(running and not stopping)

    def _character_name(self) -> str:
        configured = str(self.config.data.get("character") or "").strip()
        return configured or "Spieler"

    def _build_stats_panel(self) -> QWidget:
        """Kills, Beute, Zeit und Hindernisse - links neben der Karte."""
        panel = WowPanel(self, padding=9)
        grid = QGridLayout(panel)
        grid.setContentsMargins(9, 7, 9, 7)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)

        self.state_label = QLabel("●  bereit")
        state_font = self.state_label.font()
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        self.state_label.setStyleSheet("color: #6a737d;")
        grid.addWidget(self.state_label, 0, 0, 1, 3)

        def add(row: int, column: int, caption: str) -> QLabel:
            caption_label = QLabel(caption)
            caption_font = caption_label.font()
            caption_font.setPointSizeF(max(7.5, caption_font.pointSizeF() - 2.0))
            caption_label.setFont(caption_font)
            caption_label.setStyleSheet(f"color: {PARCHMENT_DIM};")
            value = QLabel("–")
            value_font = value.font()
            value_font.setBold(True)
            value.setFont(value_font)
            value.setStyleSheet(f"color: {PARCHMENT};")
            holder = QVBoxLayout()
            holder.setSpacing(0)
            holder.addWidget(caption_label)
            holder.addWidget(value)
            grid.addLayout(holder, row, column)
            return value

        self.kills_value = add(1, 0, "Kills")
        self.loot_value = add(1, 1, "Beute")
        self.copper_value = add(1, 2, "Kupfer")
        self.objects_value = add(2, 0, "Objekte")
        self.minutes_value = add(2, 1, "Zeit")
        self.level_value = add(2, 2, "Stufe")

        self.pos_value = QLabel("–")
        self.pos_value.setStyleSheet(f"color: {PARCHMENT_DIM};")
        grid.addWidget(self.pos_value, 3, 0, 1, 3)

        self.command_label = QLabel("kein Kartenbefehl")
        self.command_label.setStyleSheet(f"color: {GOLD};")
        self.command_label.setToolTip(
            "Linksklick in die Karte = hinlaufen (auch mitten im Kampf), "
            "Klick auf einen Mob = diesen zuerst angreifen"
        )
        grid.addWidget(self.command_label, 4, 0, 1, 3)

        self.obstacle_label = QLabel("Hindernisse: noch keine gelernt")
        self.obstacle_label.setStyleSheet(f"color: {PARCHMENT_DIM};")
        self.obstacle_label.setToolTip(
            "Gelerntes Gelände dieser Karte (siehe ~/Library/Application Support/WowFarmBot/nav)"
        )
        grid.addWidget(self.obstacle_label, 5, 0, 1, 3)

        self.reset_btn = QPushButton("Hindernisse zurücksetzen")
        self.reset_btn.setAutoDefault(False)
        self.reset_btn.setEnabled(False)
        self.reset_btn.setToolTip(
            "Nur während der Bot läuft: die aktuelle Karte ist sonst nicht bekannt."
        )
        self.reset_btn.clicked.connect(self._reset_obstacles)
        grid.addWidget(self.reset_btn, 6, 0, 1, 3)
        grid.setRowStretch(7, 1)
        return panel

    # ---------------------------------------------------- Reiter Ziele & Beute
    def _build_targets_tab(self) -> QWidget:
        """Reiter „Ziele & Beute“: Mobs, Stufenfilter, Beutefilter auf einen Blick."""
        behavior = self.config.data.get("behavior", {}) or {}
        page = QWidget(self.tabs)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # ---------------------------------------------------------------- Mobs
        mob_box = QGroupBox("Welche Mobs angreifen")
        mob_layout = QVBoxLayout(mob_box)
        mob_layout.setSpacing(6)

        mob_mode_row = QHBoxLayout()
        mob_mode_row.addWidget(QLabel("Regel:"))
        self.mob_mode_all = QRadioButton("Alle angreifbaren Mobs")
        self.mob_mode_all.setToolTip(
            "Alles, was angreifbar ist - außer den Mobs, die unten nicht angehakt sind."
        )
        self.mob_mode_only = QRadioButton("Nur die angehakten Mobs")
        self.mob_mode_only.setToolTip(
            "Nur angehakte Mobs werden angegriffen, alles andere bleibt stehen."
        )
        mob_group = QButtonGroup(mob_box)
        mob_group.addButton(self.mob_mode_all)
        mob_group.addButton(self.mob_mode_only)
        mob_mode_row.addWidget(self.mob_mode_all)
        mob_mode_row.addWidget(self.mob_mode_only)
        mob_mode_row.addStretch(1)
        mob_layout.addLayout(mob_mode_row)

        self.mob_table = self._make_rule_table(
            ["angreifen", "Mob", "Stufe", "Entry"], 190, widths=(80, None, 60, 70)
        )
        self.mob_table.setToolTip(
            "Haken = angreifen. Ohne Haken wird der Mob stehen gelassen "
            "(gilt auch bei „Alle angreifbaren Mobs“)."
        )
        mob_layout.addWidget(self.mob_table, 1)

        mob_add_row = QHBoxLayout()
        self.mob_input = QLineEdit()
        self.mob_input.setPlaceholderText("Name (Teilstück) oder Entry-Nummer, z. B. 15648")
        self.mob_input.returnPressed.connect(self._add_mob_from_input)
        mob_add_row.addWidget(self.mob_input, 1)
        mob_add_btn = QPushButton("Hinzufügen")
        mob_add_btn.setAutoDefault(False)
        mob_add_btn.clicked.connect(self._add_mob_from_input)
        mob_add_row.addWidget(mob_add_btn)
        mob_layout.addLayout(mob_add_row)

        mob_bulk_row = QHBoxLayout()
        nearby_btn = QPushButton("Aus Umgebung übernehmen")
        nearby_btn.setAutoDefault(False)
        nearby_btn.setToolTip(
            "Alle Einheiten aus „Mobs in der Nähe“ (auch neutrale) in die Tabelle holen."
        )
        nearby_btn.clicked.connect(self._add_mobs_from_nearby)
        mob_bulk_row.addWidget(nearby_btn)
        mob_all_btn = QPushButton("Alle angreifen")
        mob_all_btn.setAutoDefault(False)
        mob_all_btn.clicked.connect(lambda: self._check_all(self.mob_table, True))
        mob_bulk_row.addWidget(mob_all_btn)
        mob_none_btn = QPushButton("Keinen angreifen")
        mob_none_btn.setAutoDefault(False)
        mob_none_btn.clicked.connect(lambda: self._check_all(self.mob_table, False))
        mob_bulk_row.addWidget(mob_none_btn)
        mob_del_btn = QPushButton("Zeile entfernen")
        mob_del_btn.setAutoDefault(False)
        mob_del_btn.clicked.connect(lambda: self._remove_selected(self.mob_table))
        mob_bulk_row.addWidget(mob_del_btn)
        mob_bulk_row.addStretch(1)
        mob_hint = QLabel(
            "Namen sind Teilstücke (Groß-/Kleinschreibung egal): „spinne“ trifft „Waldspinne“. "
            "Bei „Alle angreifbaren Mobs“ trägt das Fenster gesehene Mobs mit Namen und "
            "Stufe selbst ein."
        )
        mob_hint.setWordWrap(True)
        mob_hint.setStyleSheet("color: #6a737d;")
        mob_layout.addWidget(mob_hint)
        mob_layout.addLayout(mob_bulk_row)
        outer.addWidget(mob_box, 1)

        # --------------------------------------------------------------- Stufe
        level_box = QGroupBox("Stufe der Mobs")
        level_grid = QGridLayout(level_box)
        level_grid.setHorizontalSpacing(18)
        level_grid.setVerticalSpacing(6)
        self.level_radios: "dict[str, QRadioButton]" = {}
        level_group = QButtonGroup(level_box)
        entries = [
            ("all", "Alle Stufen"),
            ("same", "Gleiche Stufe wie ich"),
            ("same_or_higher", "Gleiche Stufe und höher"),
            ("same_or_lower", "Gleiche Stufe und niedriger"),
        ]
        for index, (value, label) in enumerate(entries):
            radio = QRadioButton(label)
            level_group.addButton(radio)
            self.level_radios[value] = radio
            level_grid.addWidget(radio, 0, index)

        range_row = QHBoxLayout()
        range_radio = QRadioButton("Eigene Stufe ±")
        level_group.addButton(range_radio)
        self.level_radios["range"] = range_radio
        range_row.addWidget(range_radio)
        self.level_spread = QSpinBox()
        self.level_spread.setRange(0, 10)
        self.level_spread.setValue(int(behavior.get("mob_level_spread", 2) or 2))
        self.level_spread.setSuffix(tr(" Stufen"))
        range_row.addWidget(self.level_spread)
        self.level_now = QLabel("Deine Stufe: unbekannt")
        self.level_now.setStyleSheet("color: #6a737d;")
        range_row.addSpacing(16)
        range_row.addWidget(self.level_now)
        range_row.addStretch(1)
        level_grid.addLayout(range_row, 1, 0, 1, 4)
        level_note = QLabel(
            "Mobs ohne erkennbare Stufe („??“, Bosse) gelten immer als passend."
        )
        level_note.setStyleSheet("color: #6a737d;")
        level_grid.addWidget(level_note, 2, 0, 1, 4)
        outer.addWidget(level_box)

        # --------------------------------------------------------------- Beute
        loot_box = QGroupBox("Was eingesammelt wird")
        loot_layout = QVBoxLayout(loot_box)
        loot_layout.setSpacing(6)

        loot_mode_row = QHBoxLayout()
        self.loot_check = QCheckBox("Leichen überhaupt looten")
        self.loot_check.setToolTip("Aus = der Bot lässt jede Leiche liegen (auch Geld).")
        loot_mode_row.addWidget(self.loot_check)
        loot_mode_row.addSpacing(18)
        loot_mode_row.addWidget(QLabel("Regel:"))
        self.loot_mode_all = QRadioButton("Alles aufheben")
        self.loot_mode_all.setToolTip("Alles - außer den Gegenständen ohne Haken.")
        self.loot_mode_only = QRadioButton("Nur die angehakten Gegenstände")
        self.loot_mode_only.setToolTip("Alles andere bleibt auf der Leiche liegen.")
        loot_group = QButtonGroup(loot_box)
        loot_group.addButton(self.loot_mode_all)
        loot_group.addButton(self.loot_mode_only)
        loot_mode_row.addWidget(self.loot_mode_all)
        loot_mode_row.addWidget(self.loot_mode_only)
        loot_mode_row.addStretch(1)
        loot_layout.addLayout(loot_mode_row)

        self.loot_table = self._make_rule_table(
            ["aufheben", "Gegenstand", "Entry"], 150, widths=(85, None, 80)
        )
        self.loot_table.setToolTip(
            "Haken = einpacken. Geld wird immer mitgenommen, solange Looten an ist."
        )
        loot_layout.addWidget(self.loot_table, 1)

        loot_add_row = QHBoxLayout()
        self.loot_input = QLineEdit()
        self.loot_input.setPlaceholderText("Name (Teilstück) oder Item-Nummer, z. B. 2589")
        self.loot_input.returnPressed.connect(self._add_item_from_input)
        loot_add_row.addWidget(self.loot_input, 1)
        loot_add_btn = QPushButton("Hinzufügen")
        loot_add_btn.setAutoDefault(False)
        loot_add_btn.clicked.connect(self._add_item_from_input)
        loot_add_row.addWidget(loot_add_btn)
        loot_layout.addLayout(loot_add_row)

        loot_bulk_row = QHBoxLayout()
        loot_all_btn = QPushButton("Alles aufheben")
        loot_all_btn.setAutoDefault(False)
        loot_all_btn.clicked.connect(lambda: self._check_all(self.loot_table, True))
        loot_bulk_row.addWidget(loot_all_btn)
        loot_none_btn = QPushButton("Nichts aufheben")
        loot_none_btn.setAutoDefault(False)
        loot_none_btn.clicked.connect(lambda: self._check_all(self.loot_table, False))
        loot_bulk_row.addWidget(loot_none_btn)
        loot_del_btn = QPushButton("Zeile entfernen")
        loot_del_btn.setAutoDefault(False)
        loot_del_btn.clicked.connect(lambda: self._remove_selected(self.loot_table))
        loot_bulk_row.addWidget(loot_del_btn)
        loot_bulk_row.addStretch(1)
        loot_hint = QLabel(
            "Namen erscheinen nach dem ersten Loot automatisch; bis dahin genügt die "
            "Item-Nummer. Geld wird immer mitgenommen."
        )
        loot_hint.setWordWrap(True)
        loot_hint.setStyleSheet("color: #6a737d;")
        loot_layout.addWidget(loot_hint)
        loot_layout.addLayout(loot_bulk_row)
        outer.addWidget(loot_box, 1)

        # ------------------------------------------------------------- Fußzeile
        foot = QHBoxLayout()
        save_btn = QPushButton("Einstellungen speichern")
        save_btn.setAutoDefault(False)
        save_btn.clicked.connect(self._save_config_from_fields)
        foot.addWidget(save_btn)
        foot.addSpacing(12)
        hint = QLabel(
            "Änderungen gelten sofort - auch im laufenden Bot. *Speichern* hält sie fest."
        )
        hint.setStyleSheet("color: #6a737d;")
        foot.addWidget(hint)
        foot.addStretch(1)
        outer.addLayout(foot)

        # ---------------------------------------------------------------- Füllen
        item_names = {
            int(key): str(value)
            for key, value in (behavior.get("item_names") or {}).items()
            if str(value).strip()
        }
        self._item_names.update(item_names)

        mob_rows = behavior.get("mob_rules")
        if isinstance(mob_rows, list) and mob_rows:
            for row in mob_rows:
                if not isinstance(row, dict):
                    continue
                self._add_rule_row(
                    self.mob_table,
                    bool(row.get("attack", True)),
                    str(row.get("name") or ""),
                    int(row.get("entry") or 0),
                    int(row.get("level") or 0),
                )
        else:
            for entry in behavior.get("mob_entries") or []:
                self._add_rule_row(self.mob_table, True, "", self._int(entry), 0)
            for entry in behavior.get("ignore_entries") or []:
                self._add_rule_row(self.mob_table, False, "", self._int(entry), 0)

        loot_rows = behavior.get("loot_rules")
        if isinstance(loot_rows, list) and loot_rows:
            for row in loot_rows:
                if not isinstance(row, dict):
                    continue
                self._add_rule_row(
                    self.loot_table,
                    bool(row.get("keep", True)),
                    str(row.get("name") or ""),
                    int(row.get("entry") or 0),
                )

        if str(behavior.get("mob_filter_mode") or "").lower() == "only":
            self.mob_mode_only.setChecked(True)
        else:
            self.mob_mode_all.setChecked(True)
        if str(behavior.get("loot_mode") or "").lower() == "only":
            self.loot_mode_only.setChecked(True)
        else:
            self.loot_mode_all.setChecked(True)
        self.loot_check.setChecked(bool(behavior.get("loot", True)))
        level_mode = str(behavior.get("mob_level_mode") or "all").lower()
        self.level_radios.get(level_mode, self.level_radios["all"]).setChecked(True)
        for radio in self.level_radios.values():
            radio.toggled.connect(self._sync_level_controls)
        self._sync_level_controls()
        self._refresh_item_names()
        return page

    # --------------------------------------------------- Reiter WoW-Version
    def _build_protocol_tab(self) -> QWidget:
        """Client-Kennung und Protokollzahlen (für andere Versionen/Kerne)."""
        page = QWidget(self.tabs)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        intro = QLabel(
            "Der Bot spricht das Protokoll von <b>3.3.5a (Build 12340)</b>. Läuft dein "
            "Server mit einer anderen Build-Nummer oder eigenen Opcode-/Feld-Nummern, "
            "stellst du das hier ein. Die Werte gelten <b>ab dem nächsten Start</b> des Bots."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        outer.addWidget(intro)

        client_box = QGroupBox("Client-Kennung")
        form = QGridLayout(client_box)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.proto_version = QLineEdit()
        self.proto_version.setPlaceholderText("z. B. 3.3.5")
        self.proto_version.setFixedWidth(110)
        self.proto_version.setToolTip(
            "Wird im Login mitgeschickt. „3.3.5a“ ist auch erlaubt - daraus wird 3.3.5."
        )
        form.addWidget(QLabel("Version:"), 0, 0)
        form.addWidget(self.proto_version, 0, 1)

        self.proto_build = QSpinBox()
        self.proto_build.setRange(1, 99999)
        self.proto_build.setFixedWidth(110)
        self.proto_build.setToolTip(
            "Build-Nummer des Clients (3.3.5a = 12340). Steht im Login und in "
            "CMSG_AUTH_SESSION; ein falscher Wert wird vom Server abgelehnt."
        )
        form.addWidget(QLabel("Build:"), 0, 2)
        form.addWidget(self.proto_build, 0, 3)

        self.proto_os = QComboBox()
        self.proto_os.setEditable(True)
        self.proto_os.addItems(["Win", "Mac"])
        self.proto_os.setFixedWidth(90)
        form.addWidget(QLabel("Betriebssystem:"), 1, 0)
        form.addWidget(self.proto_os, 1, 1)

        self.proto_platform = QComboBox()
        self.proto_platform.setEditable(True)
        self.proto_platform.addItems(["x86", "x64"])
        self.proto_platform.setFixedWidth(90)
        form.addWidget(QLabel("Plattform:"), 1, 2)
        form.addWidget(self.proto_platform, 1, 3)

        self.proto_status = QLabel()
        self.proto_status.setStyleSheet(f"color: {PARCHMENT_DIM};")
        form.addWidget(self.proto_status, 2, 0, 1, 5)
        # Leere Spalte am Ende nimmt die Breite auf, damit die Felder links bleiben
        form.setColumnStretch(4, 1)
        form.setColumnMinimumWidth(2, 110)
        outer.addWidget(client_box)

        # --- Client-Ordner: hier, weil er zur Client-Kennung gehört
        folder_box = QGroupBox("Client-Ordner (Kartenbilder und Mob-Symbole)")
        folder_layout = QVBoxLayout(folder_box)
        folder_layout.setSpacing(6)
        folder_row = QHBoxLayout()
        self.client_label = QLabel("")
        self.client_label.setStyleSheet(f"color: {PARCHMENT_DIM};")
        self.client_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.client_label.setWordWrap(True)
        folder_row.addWidget(self.client_label, 1)
        self.client_btn = QPushButton("Client-Ordner …")
        self.client_btn.setAutoDefault(False)
        self.client_btn.setToolTip(
            "Ordner mit Wow.exe und Data-Ordner wählen - dort liegen die Kartenbilder"
        )
        self.client_btn.clicked.connect(self._choose_client)
        folder_row.addWidget(self.client_btn)
        folder_layout.addLayout(folder_row)
        folder_hint = QLabel(
            "Der Ordner mit <tt>Wow.exe</tt> und <tt>Data</tt> (MPQ-Archive). Daraus liest "
            "der Bot Zonenkarte, Geländekacheln und Mob-Symbole. Ohne Client läuft der Bot "
            "normal weiter - nur ohne Karte."
        )
        folder_hint.setWordWrap(True)
        folder_hint.setTextFormat(Qt.RichText)
        folder_hint.setStyleSheet("color: #6a737d;")
        folder_layout.addWidget(folder_hint)
        outer.addWidget(folder_box)

        numbers_box = QGroupBox("Opcode- und Feld-Nummern")
        numbers_layout = QVBoxLayout(numbers_box)
        numbers_layout.setSpacing(8)
        numbers_note = QLabel(
            "Die Zahlen selbst stehen in der Config unter <tt>protocol</tt> "
            "(<tt>opcodes</tt>, <tt>fields</tt>, <tt>flags</tt>). Damit du sie nicht "
            "abtippen musst: <b>Vorlage schreiben</b> legt eine Datei mit allen "
            "aktuellen Werten an (Name → Zahl), die du in einem Texteditor änderst "
            "und mit <b>Protokoll-Datei laden</b> wieder einliest."
        )
        numbers_note.setWordWrap(True)
        numbers_note.setTextFormat(Qt.RichText)
        numbers_layout.addWidget(numbers_note)

        button_row = QHBoxLayout()
        write_btn = QPushButton("Vorlage schreiben …")
        write_btn.setAutoDefault(False)
        write_btn.setToolTip(
            "Schreibt alle bekannten Namen mit ihren aktuellen Nummern als JSON - "
            "zum Bearbeiten und späteren Einlesen."
        )
        write_btn.clicked.connect(self._write_protocol_template)
        button_row.addWidget(write_btn)

        load_btn = QPushButton("Protokoll-Datei laden …")
        load_btn.setAutoDefault(False)
        load_btn.setToolTip(
            "Liest eine solche Datei ein. Unbekannte Namen und falsche Gruppen werden "
            "abgelehnt und im Log gemeldet."
        )
        load_btn.clicked.connect(self._load_protocol_file)
        button_row.addWidget(load_btn)

        reset_btn = QPushButton("Eigene Nummern löschen")
        reset_btn.setAutoDefault(False)
        reset_btn.setToolTip("Zurück auf die Werte von 3.3.5a (Build 12340).")
        reset_btn.clicked.connect(self._reset_protocol_values)
        button_row.addWidget(reset_btn)
        button_row.addStretch(1)
        numbers_layout.addLayout(button_row)
        outer.addWidget(numbers_box)

        hint = QLabel(
            "Damit passen sich die <b>Nummern</b> an - nicht der Aufbau der Pakete. Eine "
            "andere Protokollgeneration (z. B. 4.x) tauscht zusätzlich Felder und "
            "Paketinhalte aus und braucht echte Code-Änderungen; dafür ist dieser Reiter "
            "nicht gedacht."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        hint.setStyleSheet("color: #6a737d;")
        outer.addWidget(hint)
        outer.addStretch(1)

        self._proto_from_config()
        self._update_client_label()
        for widget in (self.proto_os, self.proto_platform):
            widget.currentTextChanged.connect(self._proto_changed)
        self.proto_build.valueChanged.connect(self._proto_changed)
        self.proto_version.editingFinished.connect(self._proto_changed)
        return page

    def _proto_section(self) -> dict:
        section = self.config.data.get("protocol")
        if not isinstance(section, dict):
            section = {}
            self.config.data["protocol"] = section
        return section

    def _proto_from_config(self) -> None:
        """Formular aus der Config füllen (auch nach dem Laden einer Datei)."""
        profile = parse(self.config.data.get("protocol"))
        self.proto_version.setText(profile.version)
        self.proto_build.setValue(int(profile.build))
        self.proto_os.setCurrentText(profile.os)
        self.proto_platform.setCurrentText(profile.platform)
        self._proto_status(profile)

    def _proto_status(self, profile: "Profile | None" = None) -> None:
        profile = profile or parse(self.config.data.get("protocol"))
        self.proto_status.setText(
            tr(
                "Eingestellt: {summary} · gilt ab dem nächsten Start",
                summary=profile.summary(),
            )
        )

    def _proto_changed(self) -> None:
        """Formular in die Config schreiben (Nummern bleiben unberührt)."""
        section = self._proto_section()
        section["version"] = self.proto_version.text().strip() or DEFAULT_VERSION
        section["build"] = int(self.proto_build.value())
        section["os"] = self.proto_os.currentText().strip() or DEFAULT_OS
        section["platform"] = self.proto_platform.currentText().strip() or DEFAULT_PLATFORM
        self.config.save(self.config.data)
        self._proto_from_config()

    def _write_protocol_template(self) -> None:
        start = str(Path.home() / f"wowfarmbot-protokoll-{self.proto_version.text().strip() or DEFAULT_VERSION}.json")
        path, _filter =        QFileDialog.getSaveFileName(
            self, tr("Protokoll-Vorlage speichern"), start, "JSON (*.json)"
        )
        if not path:
            return
        data = template()
        section = self.config.data.get("protocol") or {}
        data["version"] = self.proto_version.text().strip() or DEFAULT_VERSION
        data["build"] = int(self.proto_build.value())
        data["os"] = self.proto_os.currentText().strip() or DEFAULT_OS
        data["platform"] = self.proto_platform.currentText().strip() or DEFAULT_PLATFORM
        # Eigene Werte stehen schon drin - sie sollen in der Vorlage sichtbar sein
        for group in ("opcodes", "fields", "flags"):
            own = section.get(group)
            if isinstance(own, dict):
                data[group].update({str(k).upper(): v for k, v in own.items()})
        try:
            target = Path(path)
            target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
        except OSError as exc:
            QMessageBox.warning(self, tr("Datei nicht lesbar"), str(exc))
            return
        QMessageBox.information(
            self,
            tr("Vorlage geschrieben"),
            f"{target}\n\n{len(data['opcodes'])} Opcodes, {len(data['fields'])} Felder, "
            f"{len(data['flags'])} Flags. "
            + tr("Zahlen ändern, dann über „Protokoll-Datei laden“ einlesen."),
        )

    def _load_protocol_file(self) -> None:
        path, _filter =        QFileDialog.getOpenFileName(
            self, tr("Protokoll-Datei laden …"), str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, tr("Datei nicht lesbar"), str(exc))
            return
        if not isinstance(raw, dict):
            QMessageBox.warning(
                self, tr("Datei nicht brauchbar"), tr("Erwartet wird ein JSON-Objekt.")
            )
            return
        section = self._proto_section()
        for key in ("version", "build", "os", "platform"):
            if key in raw:
                section[key] = raw[key]
        loaded = 0
        rejected: "list[str]" = []
        for group in ("opcodes", "fields", "flags"):
            rows = raw.get(group)
            if not isinstance(rows, dict):
                continue
            # Einmal durch die Prüfung schicken: nur gültige Namen landen in der Config
            probe = parse({group: rows})
            section[group] = dict(getattr(probe, group))
            loaded += len(section[group])
            kept = section[group]
            for name in rows:
                key = str(name).strip().upper()
                if key and key not in kept:
                    rejected.append(key)
        self.config.save(self.config.data)
        self._proto_from_config()
        text = tr("{count} Wert(e) übernommen.", count=loaded)
        if rejected:
            text += "\n\n" + tr("Nicht übernommen: {names}", names=", ".join(sorted(set(rejected))))
        QMessageBox.information(self, tr("Protokoll geladen"), text)

    def _reset_protocol_values(self) -> None:
        section = self._proto_section()
        for group in ("opcodes", "fields", "flags"):
            section.pop(group, None)
        self.config.save(self.config.data)
        self._proto_from_config()

    @staticmethod
    def _int(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _make_rule_table(
        self, headers: "list[str]", height: int, widths: tuple
    ) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setMinimumHeight(height)
        header = table.horizontalHeader()
        for column, width in enumerate(widths):
            if width is None:
                header.setSectionResizeMode(column, QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(column, QHeaderView.Fixed)
                table.setColumnWidth(column, width)
        return table

    def _add_rule_row(
        self, table: QTableWidget, checked: bool, name: str, entry: int, level: int = 0
    ) -> bool:
        """Eine Zeile anlegen - doppelte (gleicher Entry oder gleicher Name) bleiben weg."""
        for row in self._rule_rows(table):
            if entry and row["entry"] == entry:
                return False
            if not entry and row["name"].lower() == name.strip().lower():
                return False
        if not entry and not name.strip():
            return False
        row = table.rowCount()
        table.insertRow(row)
        head = QTableWidgetItem()
        head.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        head.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        head.setData(
            Qt.UserRole, {"entry": entry, "name": name.strip(), "level": level}
        )
        table.setItem(row, 0, head)
        table.setItem(row, 1, QTableWidgetItem(name.strip() or tr("(ohne Namen)")))
        if table is self.loot_table:
            table.setItem(row, 2, QTableWidgetItem(str(entry) if entry else "–"))
        else:
            level_item = QTableWidgetItem(str(level) if level else "–")
            level_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row, 2, level_item)
            entry_item = QTableWidgetItem(str(entry) if entry else "–")
            entry_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row, 3, entry_item)
        return True

    def _rule_rows(self, table: QTableWidget) -> "list[dict]":
        rows = []
        for row in range(table.rowCount()):
            head = table.item(row, 0)
            if head is None:
                continue
            data = dict(head.data(Qt.UserRole) or {})
            # Fehlende Felder ergänzen: Zeilendaten müssen immer vollständig sein.
            data.setdefault("entry", 0)
            data.setdefault("name", "")
            data.setdefault("level", 0)
            data["checked"] = head.checkState() == Qt.Checked
            rows.append(data)
        return rows

    def _check_all(self, table: QTableWidget, checked: bool) -> None:
        for row in range(table.rowCount()):
            head = table.item(row, 0)
            if head is not None:
                head.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _remove_selected(self, table: QTableWidget) -> None:
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        for row in rows:
            head = table.item(row, 0)
            data = dict(head.data(Qt.UserRole) or {}) if head is not None else {}
            if table is self.mob_table and self._int(data.get("entry")):
                self._hidden_mobs.add(self._int(data.get("entry")))
            table.removeRow(row)

    def _add_mob_from_input(self) -> None:
        text = self.mob_input.text().strip()
        if not text:
            return
        if text.isdigit():
            entry = int(text)
            name = self._mob_catalog.get(entry, "")
            self._hidden_mobs.discard(entry)
            self._add_rule_row(self.mob_table, True, name, entry, 0)
        else:
            self._add_rule_row(self.mob_table, True, text, 0, 0)
        self.mob_input.clear()

    def _add_item_from_input(self) -> None:
        text = self.loot_input.text().strip()
        if not text:
            return
        if text.isdigit():
            entry = int(text)
            self._add_rule_row(
                self.loot_table, True, self._item_names.get(entry, ""), entry
            )
        else:
            self._add_rule_row(self.loot_table, True, text, 0)
        self.loot_input.clear()
        self._refresh_item_names()

    def _add_mobs_from_nearby(self) -> None:
        added = 0
        for mob in self._last_mobs:
            entry = self._int(mob.get("entry"))
            name = str(mob.get("name") or "")
            if entry or name:
                if self._add_rule_row(
                    self.mob_table, True, name, entry, self._int(mob.get("level"))
                ):
                    added += 1
        self.hint_label.setText(
            tr(
                "{added} Mob(s) übernommen - {count} in der Tabelle.",
                added=added,
                count=self.mob_table.rowCount(),
            )
        )

    def _sync_level_controls(self) -> None:
        self.level_spread.setEnabled(self.level_radios["range"].isChecked())

    def _level_mode(self) -> str:
        for value, radio in self.level_radios.items():
            if radio.isChecked():
                return value
        return "all"

    def _refresh_item_names(self) -> None:
        """Namen nachtragen, sobald der Bot einen Gegenstand kennt."""
        for row, data in enumerate(self._rule_rows(self.loot_table)):
            if not data.get("entry") or data.get("name"):
                continue
            name = self._item_names.get(data["entry"])
            if not name:
                continue
            data["name"] = name
            head = self.loot_table.item(row, 0)
            if head is not None:
                head.setData(Qt.UserRole, data)
            cell = self.loot_table.item(row, 1)
            if cell is not None:
                cell.setText(name)

    def _merge_targets(self, snap: dict) -> None:
        """Neue Mobs und Item-Namen aus dem laufenden Bot in die Tabellen holen."""
        changed_items = False
        for entry, name in (snap.get("items") or {}).items():
            try:
                entry = int(entry)
            except (TypeError, ValueError):
                continue
            name = str(name or "").strip()
            if name and self._item_names.get(entry) != name:
                self._item_names[entry] = name
                changed_items = True
        if changed_items:
            self._refresh_item_names()

        known: "dict[int, tuple[int, dict]]" = {
            row["entry"]: (index, row)
            for index, row in enumerate(self._rule_rows(self.mob_table))
            if row.get("entry")
        }
        for mob in self._last_mobs:
            entry = self._int(mob.get("entry"))
            name = str(mob.get("name") or "")
            if name.startswith("Entry "):
                name = ""
            level = self._int(mob.get("level"))
            if entry and name:
                self._mob_catalog[entry] = name
            if not entry or not name or entry in self._hidden_mobs:
                continue
            if entry not in known:
                # Nur im Modus „alle“ mitschreiben: im Modus „nur markierte“
                # wuerde eine automatische (nicht angehakte) Zeile den Mob
                # stillschweigend auf die Ignorierliste setzen.
                if self.mob_mode_only.isChecked():
                    continue
                if self._add_rule_row(
                    self.mob_table, True, name, entry, level
                ):
                    known[entry] = (
                        self.mob_table.rowCount() - 1,
                        {
                            "entry": entry,
                            "name": name,
                            "level": level,
                            "checked": True,
                        },
                    )
                continue
            # Namen und Stufe nachtragen, sobald der Bot sie kennt.
            index, row = known[entry]
            changed = False
            if name and not row.get("name"):
                row["name"] = name
                cell = self.mob_table.item(index, 1)
                if cell is not None:
                    cell.setText(name)
                changed = True
            if level and self._int(row.get("level")) != level:
                row["level"] = level
                cell = self.mob_table.item(index, 2)
                if cell is not None:
                    cell.setText(str(level))
                changed = True
            if changed:
                head = self.mob_table.item(index, 0)
                if head is not None:
                    head.setData(Qt.UserRole, row)

    def _push_rules(self) -> None:
        """Änderungen im Reiter „Ziele & Beute“ sofort an den laufenden Bot geben."""
        if not self.runner.running():
            return
        patch: dict = {}
        self._targets_into_behavior(patch)
        key = json.dumps(patch, sort_keys=True, ensure_ascii=False)
        if key == self._rules_sent:
            return
        self._rules_sent = key
        self.runner.set_rules(patch)

    def _targets_into_behavior(self, behavior: dict) -> None:
        """Tabellen und Schalter zurück in die Config schreiben."""
        mob_rows = []
        for row in self._rule_rows(self.mob_table):
            mob_rows.append(
                {
                    "entry": self._int(row.get("entry")),
                    "name": str(row.get("name") or "").strip(),
                    "level": self._int(row.get("level")),
                    "attack": bool(row["checked"]),
                }
            )
        behavior["mob_rules"] = mob_rows
        behavior["mob_filter_mode"] = (
            "only" if self.mob_mode_only.isChecked() else "all"
        )
        behavior["mob_level_mode"] = self._level_mode()
        behavior["mob_level_spread"] = int(self.level_spread.value())

        loot_rows = []
        for row in self._rule_rows(self.loot_table):
            loot_rows.append(
                {
                    "entry": self._int(row.get("entry")),
                    "name": str(row.get("name") or "").strip(),
                    "keep": bool(row["checked"]),
                }
            )
        behavior["loot_rules"] = loot_rows
        behavior["loot_mode"] = "only" if self.loot_mode_only.isChecked() else "all"
        behavior["loot"] = self.loot_check.isChecked()
        # Alte Schluessel entfernen: die Tabellen sind jetzt die einzige Quelle.
        for legacy in (
            "mob_entries",
            "mob_names",
            "ignore_entries",
            "ignore_names",
            "loot_entries",
            "loot_names",
            "loot_skip_entries",
            "loot_skip_names",
        ):
            behavior.pop(legacy, None)
        behavior["item_names"] = {
            str(entry): name for entry, name in self._item_names.items() if name
        }

    # ------------------------------------------------------------------ Logging
    def _setup_logging(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("wowbot")
        logger.setLevel(logging.INFO)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.addHandler(QueueLogHandler(self.log_queue))
        file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE))
        logger.addHandler(file_handler)
        logger.propagate = False

    # -------------------------------------------------------------------- Start
    def start_bot(self) -> None:
        if self.runner.running():
            return
        config = self._config_from_fields()
        if config is None:
            return

        self.config.save(config)
        # Der Bot startet mit genau diesen Regeln - erst Änderungen werden gesendet.
        initial: dict = {}
        self._targets_into_behavior(initial)
        self._rules_sent = json.dumps(initial, sort_keys=True, ensure_ascii=False)
        trace = self.trace_check.isChecked()
        logging.getLogger("wowbot").setLevel(logging.DEBUG if trace else logging.INFO)
        logging.getLogger("wowbot.world").setLevel(
            logging.DEBUG if trace else logging.WARNING
        )

        self.runner.start(config, trace=trace)
        self._was_running = True
        self._set_running_buttons(True)
        self._set_state(tr("●  verbinde …"), "#6a737d")
        self.hint_label.setText(tr("Bot gestartet - Ereignisse laufen hier ein."))

    def stop_bot(self) -> None:
        if not self.runner.running():
            return
        self._set_state(tr("●  stoppe …"), "#6a737d")
        self._set_running_buttons(True, stopping=True)
        self.hint_label.setText(tr("Bot wird gestoppt …"))
        self.runner.stop()

    # ------------------------------------------------------------------ Anzeige
    def _poll(self) -> None:
        self._drain_logs()
        self._drain_map()
        self._update_status()
        self._update_map()
        self._push_rules()
        if self._was_running and not self.runner.running():
            self._was_running = False
            self._set_running_buttons(False)
            self.reset_btn.setEnabled(False)
            self._set_state(tr("●  bereit"), "#6a737d")
            self.hint_label.setText(tr("Bot beendet."))

    def _drain_logs(self) -> None:
        scroll = self.log_text.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 4
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        drained = 0
        while drained < 500:
            try:
                levelno, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._color_for(levelno, line)))
            cursor.insertText(line + "\n", fmt)
            drained += 1
        if drained and at_bottom:
            scroll.setValue(scroll.maximum())

    def _color_for(self, levelno: int, line: str) -> str:
        if "[tod]" in line or levelno >= logging.ERROR:
            return COLOR_ERROR
        if "[kill]" in line or "[level]" in line:
            return COLOR_KILL
        if levelno >= logging.WARNING:
            return COLOR_WARN
        if "[loot]" in line or "[heil]" in line or "[zauber]" in line:
            return COLOR_LOOT
        if levelno < logging.INFO:
            return COLOR_DEBUG
        return COLOR_INFO

    def _update_status(self) -> None:
        snap = self.runner.snapshot()
        if not snap:
            return
        # --- Spielerfenster über der Karte
        self.me_frame.set_identity(self._character_name(), snap["level"])
        self.me_frame.hp.set_value(
            snap["hp_pct"], f"{snap['hp']}/{snap['max_hp']}   ({snap['hp_pct']:.0f} %)"
        )
        power = snap["power"]
        if self.me_frame.power is not None:
            power_name = tr(power["name"] or "Mana")
            self.me_frame.power.set_value(
                power["pct"],
                f"{power_name}   {power['value']}/{power['max']}   ({power['pct']:.0f} %)"
                if power["max"]
                else power_name,
            )
        if self.me_frame.xp is not None:
            if snap["next_xp"]:
                self.me_frame.xp.set_value(
                    snap["xp_pct"],
                    tr(
                        "EP {xp}/{next_xp}   ({pct} %)",
                        xp=snap["xp"],
                        next_xp=snap["next_xp"],
                        pct=f"{snap['xp_pct']:.1f}",
                    ),
                )
            else:
                self.me_frame.xp.set_value(
                    0.0, tr("EP {xp} · Stufe unbekannt", xp=snap["xp"])
                )
        self.me_frame.detail_label.setText(
            tr(
                "{minutes} min im Einsatz · {objects} Objekte",
                minutes=f"{snap['minutes']:.0f}",
                objects=snap["objects"],
            )
        )

        # --- Zielfenster
        info = snap.get("target_info") or {}
        if info:
            self.target_frame.set_identity(
                str(info.get("name") or f"Entry {info.get('entry', 0)}"),
                int(info.get("level") or 0),
                self.mob_icons.get(int(info.get("entry") or 0)),
            )
            max_hp = int(info.get("max_hp") or 0)
            self.target_frame.hp.set_value(
                float(info.get("hp_pct") or 0.0),
                f"{info.get('hp', 0)}/{max_hp}   ({float(info.get('hp_pct') or 0.0):.0f} %)"
                if max_hp
                else tr("Leben unbekannt"),
            )
            self.target_frame.detail_label.setText(
                tr(
                    "{dist} yd entfernt · Entry {entry}",
                    dist=f"{float(info.get('dist') or 0.0):.0f}",
                    entry=info.get("entry", 0),
                )
            )
        else:
            self.target_frame.set_identity(tr("kein Ziel"), 0, None, "—")
            self.target_frame.hp.set_value(0.0, tr("warte auf Ziel"))
            self.target_frame.detail_label.setText("")

        # --- Zähler
        self.kills_value.setText(str(snap["kills"]))
        self.loot_value.setText(str(snap["loot"]))
        self.copper_value.setText(str(snap["copper"]))
        self.objects_value.setText(str(snap["objects"]))
        self.minutes_value.setText(f"{snap['minutes']:.0f} min")
        self.level_value.setText(str(snap["level"] or "?"))
        self.pos_value.setText(
            tr(
                "Position {x} / {y}",
                x=f"{snap['pos'][0]:.1f}",
                y=f"{snap['pos'][1]:.1f}",
            )
        )

        self._last_mobs = snap.get("nearby") or []
        self._update_nearby(self._last_mobs)
        self.level_now.setText(
            tr("Deine Stufe: {level}", level=snap["level"])
            if snap.get("level")
            else tr("Deine Stufe: unbekannt")
        )
        self._merge_targets(snap)

        map_id = snap.get("map", 0)
        walls = snap.get("walls", 0)
        self.obstacle_label.setText(
            tr("Karte {map} · {walls} Hindernisse gespeichert", map=map_id, walls=walls)
            if walls
            else tr("Karte {map} · noch keine Hindernisse gelernt", map=map_id)
        )
        self.reset_btn.setEnabled(bool(walls) and self.runner.running())

        if snap["dead"]:
            self._set_state(tr("●  tot - warte auf Wiederbelebung"), COLOR_ERROR)
        elif self.runner.running():
            self._set_state(tr("●  läuft"), COLOR_KILL)

    # ------------------------------------------------------- Karte aus dem Client
    def _start_map_worker(self) -> None:
        """Hintergrund-Leser für die Client-Karten (neu) starten."""
        if self.map_worker is not None:
            self.map_worker.stop()
        self.map_worker = MapWorker(self.map_client, self.map_cache_dir)
        self.map_zone = None
        self.map_error = None
        self.map_pixmaps.clear()
        self.ground_pixmaps.clear()
        self._ground_pending.clear()
        self._ground_absent.clear()
        self.map_canvas.set_texture(None, None)
        self.map_canvas.set_tiles({})
        # Symbole kommen aus dem gewählten Client - neu holen
        self.mob_icons.clear()
        self._icon_pending.clear()
        self._icon_absent.clear()
        self._icon_family.clear()
        self.map_canvas.set_icons(self.mob_icons)
        self._update_client_label()

    def _update_client_label(self) -> None:
        """Pfad des Clients im Reiter „WoW-Version“ anzeigen."""
        configured = (self.config.data.get("map", {}) or {}).get("client")
        if self.map_client:
            self.client_label.setText(tr("Client: {path}", path=self.map_client))
        elif configured:
            self.client_label.setText(
                tr("Client-Pfad nicht gefunden: {path} - bitte neu wählen.", path=configured)
            )
        else:
            self.client_label.setText(
                tr("Kein Client gefunden - Kartenbilder brauchen den WoW-Ordner (Data mit MPQ-Dateien).")
            )

    def _zoom_zone(self) -> None:
        zone = self.map_zone
        if not zone:
            self.hint_label.setText(
                tr(
                    "Noch keine Zonenkarte geladen - dafür muss der Bot laufen und ein Client "
                    "eingestellt sein."
                )
            )
            return
        width = abs(float(zone["left"]) - float(zone["right"]))
        self.map_canvas.zoom = max(60.0, min(MapCanvas.MAX_ZOOM, width))
        self.map_canvas.update()

    def _choose_client(self) -> None:
        start = str(self.map_client or Path.home())
        folder = QFileDialog.getExistingDirectory(self, "WoW-Client-Ordner wählen", start)
        if not folder:
            return
        client = find_client_dir(folder)
        if client is None:
            QMessageBox.warning(
                self,
                APP_NAME,
                tr(
                    "In diesem Ordner liegt kein WoW-Client - gesucht wird ein Data-Ordner "
                    "mit MPQ-Dateien (z. B. common.MPQ)."
                ),
            )
            return
        self.map_client = client
        config = copy.deepcopy(self.config.data)
        config.setdefault("map", {})["client"] = str(client)
        try:
            self.config.save(config)
        except OSError as exc:
            self.hint_label.setText(
                tr("Client-Pfad konnte nicht gespeichert werden: {error}", error=exc)
            )
        self._start_map_worker()
        self.hint_label.setText(tr("Karten kommen jetzt aus {path}", path=client))

    def _drain_map(self) -> None:
        worker = self.map_worker
        if worker is None:
            return
        while True:
            try:
                kind, payload = worker.results.get_nowait()
            except queue.Empty:
                return
            if kind == "bild":
                path = payload.get("path", "")
                pixmap = self.map_pixmaps.get(path)
                if pixmap is None:
                    pixmap = QPixmap(path)
                    if pixmap.isNull():
                        self.map_error = f"Kartenbild {path} ist nicht lesbar"
                        continue
                    self.map_pixmaps[path] = pixmap
                if self.map_zone is None or self.map_zone.get("path") != path:
                    self.map_canvas.set_texture(pixmap, payload)
                self.map_zone = payload
                self.map_error = None
                self.map_message = ""
            elif kind == "kachel":
                key, path = payload
                self._ground_pending.discard(key)
                pixmap = self.ground_pixmaps.get(key)
                if pixmap is None:
                    pixmap = QPixmap(path)
                    if pixmap.isNull():
                        self._ground_absent.add(key)
                        continue
                    self.ground_pixmaps[key] = pixmap
                    self._prune_ground(key[0])
            elif kind == "kachel_leer":
                self._ground_pending.discard(payload)
                self._ground_absent.add(payload)
            elif kind == "icon":
                entry, path = payload
                self._icon_pending.discard(int(entry))
                if not path:
                    self._icon_absent.add(int(entry))
                    continue
                pixmap = self.map_pixmaps.get(path)
                if pixmap is None:
                    pixmap = QPixmap(path)
                    if pixmap.isNull():
                        self._icon_absent.add(int(entry))
                        continue
                    self.map_pixmaps[path] = pixmap
                self.mob_icons[int(entry)] = pixmap
                self.map_canvas.set_icons(self.mob_icons)
            elif kind == "leer":
                if self.map_zone is not None:
                    self.map_canvas.set_texture(None, None)
                self.map_zone = None
                self.map_message = "keine Kartenkacheln für diese Zone im Client"
                self.map_error = None
            else:
                if self.map_zone is not None:
                    self.map_canvas.set_texture(None, None)
                self.map_zone = None
                self.map_error = payload
                self.map_message = ""

    def _request_icons(self, view: dict) -> None:
        """Fehlende Mob-Symbole beim Karten-Thread anfordern."""
        worker = self.map_worker
        if worker is None:
            return
        for mob in view.get("mobs") or ():
            entry = int(mob.get("entry") or 0)
            if not entry:
                continue
            name = str(mob.get("name") or "")
            npc = not mob.get("attackable") and bool(mob.get("friendly"))
            family = family_for(name)
            if npc and family == DEFAULT_FAMILY:
                family = "npc"
            # Schon ein Symbol? Nur neu holen, wenn der Name jetzt eine andere
            # Kreaturenart verrät (erst "Entry 1234", später "Waldspinne").
            if entry in self.mob_icons and self._icon_family.get(entry) == family:
                continue
            if entry in self._icon_pending or entry in self._icon_absent:
                continue
            self._icon_pending.add(entry)
            self._icon_family[entry] = family
            worker.request_icon(entry, name, npc)

    def _update_map(self) -> None:
        view = self.runner.map_view()
        self.map_canvas.set_view(view)
        if view:
            self._last_map_mobs = list(view.get("mobs") or [])
            self._request_icons(view)
            self._update_command_label(view.get("command"))
        if not view:
            self.map_info.setText(
                tr(
                    "Bot nicht verbunden - starte ihn, dann füllt sich die Karte "
                    "(und Klicks setzen Hotspots)"
                )
            )
            return

        # Zonenbild nachladen (höchstens alle 2,5 s - die Zone wechselt selten)
        now = time.monotonic()
        if self.map_worker is not None and now - self._map_request > 2.5:
            self._map_request = now
            pos_x, pos_y = view["pos"]
            self.map_worker.request(view["map"], pos_x, pos_y)

        self._request_ground(view)
        ground = self._ground_for_map(int(view["map"]))
        self.map_canvas.set_tiles(ground)

        zone_name = f" · {self.map_zone['label']}" if self.map_zone else ""
        hotspots = (
            tr(" · {count} Hotspots", count=len(self.hotspots)) if self.hotspots else ""
        )
        karte = f"Karte {view['map']}{zone_name}"
        if self.map_error and not ground:
            karte += tr(" · Kartenbild: {error}", error=self.map_error)
        elif self.map_message and not ground:
            karte += f" · {tr(self.map_message)}"
        self.map_info.setText(
            tr(
                "Karte {map}{zone} · {tiles} Geländekacheln · {walls} Hindernisse{hotspots} · {zoom} yd breit",
                map=view["map"],
                zone=zone_name,
                tiles=len(ground),
                walls=len(view["walls"]),
                hotspots=hotspots,
                zoom=f"{self.map_canvas.zoom:.0f}",
            )
        )

    def _update_command_label(self, command: "dict | None") -> None:
        if not command:
            self.command_label.setText(tr("kein Kartenbefehl"))
            return
        if command.get("kind") == "goto":
            self.command_label.setText(
                tr(
                    "Befehl: laufe zu {x} / {y}",
                    x=f"{float(command['x']):.0f}",
                    y=f"{float(command['y']):.0f}",
                )
            )
        else:
            self.command_label.setText(
                tr("Befehl: greife {name} an", name=command.get("name") or "Mob")
            )

    # ---------------------------------------------------- Geländekacheln (1:1)
    def _ground_for_map(self, map_id: int) -> "dict[tuple[int, int], QPixmap]":
        """Sichtbare Kacheln der aktuellen Karte als (tx, ty) -> Bild."""
        return {
            (tx, ty): pixmap
            for (map_key, tx, ty), pixmap in self.ground_pixmaps.items()
            if map_key == map_id
        }

    def _prune_ground(self, map_id: int) -> None:
        """Speicher begrenzen: alte Kacheln anderer Karten wegwerfen."""
        if len(self.ground_pixmaps) <= 220:
            return
        for key in [key for key in self.ground_pixmaps if key[0] != map_id]:
            self.ground_pixmaps.pop(key, None)

    def _request_ground(self, view: dict) -> None:
        """Kacheln für den sichtbaren Ausschnitt nachladen (gedrosselt)."""
        worker = self.map_worker
        if worker is None or self.map_client is None:
            return
        map_id = int(view["map"])
        pos_x, pos_y = view["pos"]
        width, height = self.map_canvas.width(), self.map_canvas.height()
        zoom = self.map_canvas.zoom
        half_y = zoom / 2.0 + TILE_SIZE
        half_x = (zoom * height / width / 2.0 if width else half_y) + TILE_SIZE
        tx_a, ty_a = tile_of(pos_x + half_x, pos_y + half_y)  # Nordwest-Ecke
        tx_b, ty_b = tile_of(pos_x - half_x, pos_y - half_y)  # Südost-Ecke
        tx_a, tx_b = min(tx_a, tx_b), max(tx_a, tx_b)
        ty_a, ty_b = min(ty_a, ty_b), max(ty_a, ty_b)
        if (tx_b - tx_a + 1) * (ty_b - ty_a + 1) > 400:
            return  # viel zu weit herausgezoomt - Karte wäre ohnehin nur ein Fleck

        wanted: "list[tuple[int, int, int]]" = []
        # Von der Mitte nach außen, damit zuerst das Wichtige da ist
        for ty in range(ty_a, ty_b + 1):
            for tx in range(tx_a, tx_b + 1):
                key = (map_id, tx, ty)
                if key in self.ground_pixmaps or key in self._ground_pending:
                    continue
                if key in self._ground_absent:
                    continue
                wanted.append(key)
        if not wanted:
            return
        center = tile_of(pos_x, pos_y)
        wanted.sort(key=lambda key: abs(key[1] - center[0]) + abs(key[2] - center[1]))
        for key in wanted[:10]:
            self._ground_pending.add(key)
            worker.request_tile(*key)

    # ------------------------------------------------------------------ Hotspots
    def _push_hotspots(self) -> None:
        """Hotspots im Config-File sichern und den laufenden Bot informieren."""
        config = copy.deepcopy(self.config.data)
        behavior = config.setdefault("behavior", {})
        behavior["hotspots"] = [dict(spot) for spot in self.hotspots]
        try:
            self.config.save(config)
        except OSError as exc:
            self.hint_label.setText(
                tr("Hotspots konnten nicht gespeichert werden: {error}", error=exc)
            )
        self.map_canvas.set_hotspots(self.hotspots)
        self.runner.set_hotspots(self.hotspots)

    def _goto_point(self, x: float, y: float) -> None:
        """Linksklick in die Karte: dorthin laufen, alles andere bis dahin ignorieren."""
        if not self.runner.running():
            self.hint_label.setText(
                tr("Zum Hinlaufen muss der Bot laufen - sonst gibt es kein Ziel in der Welt.")
            )
            return
        self.runner.set_command({"kind": "goto", "x": round(float(x), 1), "y": round(float(y), 1)})
        self.hint_label.setText(
            tr(
                "Laufbefehl: {x}/{y} - der Bot ignoriert Ziele und Beute, bis er da ist.",
                x=f"{x:.1f}",
                y=f"{y:.1f}",
            )
        )

    def _attack_mob(self, guid: int) -> None:
        """Klick auf einen Mob: den zuerst angreifen."""
        if not self.runner.running():
            self.hint_label.setText(tr("Zum Angreifen muss der Bot laufen."))
            return
        guid = int(guid)
        self.runner.set_command({"kind": "attack", "guid": guid})
        mob = next((m for m in self._last_map_mobs if m.get("guid") == guid), None)
        name = str(mob.get("name")) if mob else f"GUID {guid}"
        self.hint_label.setText(
            tr("Angriffsbefehl: {name} wird zuerst angegriffen.", name=name)
        )

    def _add_hotspot(self, x: float, y: float) -> None:
        if self.map_canvas.view is None:
            self.hint_label.setText(
                tr("Zum Setzen von Hotspots muss der Bot laufen - sonst ist keine Position bekannt.")
            )
            return
        self.hotspots.append({"x": round(float(x), 1), "y": round(float(y), 1)})
        self._push_hotspots()
        self.hint_label.setText(
            tr(
                "Hotspot {index} gesetzt ({x}/{y}) - der Bot läuft ihn ab, wenn kein Mob in Reichweite ist.",
                index=len(self.hotspots),
                x=f"{x:.1f}",
                y=f"{y:.1f}",
            )
        )

    def _remove_hotspot(self, x: float, y: float) -> None:
        if not self.hotspots:
            return
        nearest = min(
            range(len(self.hotspots)),
            key=lambda i: math.hypot(self.hotspots[i]["x"] - x, self.hotspots[i]["y"] - y),
        )
        spot = self.hotspots[nearest]
        if math.hypot(spot["x"] - x, spot["y"] - y) > 15.0:
            self.hint_label.setText(
                tr("Kein Hotspot in der Nähe des Klicks (Rechtsklick auf den Punkt).")
            )
            return
        self.hotspots.pop(nearest)
        self._push_hotspots()
        self.hint_label.setText(
            tr("Hotspot entfernt - {count} übrig.", count=len(self.hotspots))
        )

    def _update_nearby(self, mobs: list) -> None:
        """Tabelle der Einheiten in der Nähe, inklusive Freundlicher."""
        if not mobs:
            self.nearby_table.hide()
            self.nearby_table.setRowCount(0)
            self.nearby_empty.show()
            return
        self.nearby_empty.hide()
        self.nearby_table.show()

        if self.nearby_table.rowCount() != len(mobs):
            self.nearby_table.setRowCount(len(mobs))
            for row in range(len(mobs)):
                for column in range(5):
                    item = QTableWidgetItem("")
                    if column:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.nearby_table.setItem(row, column, item)

        plain = QColor(PARCHMENT)
        for row, mob in enumerate(mobs):
            hp = (
                f"{mob['hp']}/{mob['max_hp']} ({mob['hp_pct']:.0f} %)"
                if mob["max_hp"]
                else tr("unbekannt")
            )
            values = (
                ("→ " if mob["target"] else "") + mob["name"],
                str(mob["level"] or "?"),
                hp,
                f"{mob['dist']:.0f}",
                tr("ja") if mob["attackable"] else tr("nein"),
            )
            for column, text in enumerate(values):
                item = self.nearby_table.item(row, column)
                if item is None:
                    continue
                item.setText(text)
                item.setForeground(plain if mob["attackable"] else QColor("#8a8a8a"))
                item.setToolTip(
                    ""
                    if mob["attackable"]
                    else tr("nicht angreifbar (Vendor, Geistheiler, Freund)")
                )
                font = item.font()
                font.setBold(bool(mob["target"]))
                item.setFont(font)

    def _set_state(self, text: str, color: str = "#6a737d") -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------- Konfiguration
    def _config_from_fields(self) -> "dict | None":
        config = copy.deepcopy(self.config.data)
        config.setdefault("server", {})
        config.setdefault("account", {})
        config["server"]["host"] = self.host_edit.text().strip() or "127.0.0.1"
        try:
            config["server"]["auth_port"] = int(self.port_edit.text().strip() or "3724")
        except ValueError:
            QMessageBox.critical(self, APP_NAME, "Der Port muss eine Zahl sein (z. B. 3724).")
            return None
        config["server"]["realm"] = self.realm_edit.text().strip()
        config["account"]["username"] = self.user_edit.text().strip()
        config["account"]["password"] = self.pass_edit.text()
        config["character"] = self.char_edit.text().strip()
        if not config["account"]["username"] or not config["account"]["password"]:
            QMessageBox.critical(
                self, APP_NAME, tr("Bitte Kontoname und Passwort eintragen.")
            )
            return None
        behavior = config.setdefault("behavior", {})
        self._targets_into_behavior(behavior)
        return config

    def _save_config_from_fields(self) -> None:
        config = self._config_from_fields()
        if config is None:
            return
        try:
            self.config.save(config)
        except OSError as exc:
            QMessageBox.critical(
                self, APP_NAME, tr("Speichern fehlgeschlagen:\n{error}", error=exc)
            )
            return
        self.hint_label.setText(tr("Gespeichert: {path}", path=self.config.path))

    def _open_config(self) -> None:
        try:
            self.config.save(self.config.data)
            subprocess.run(["open", "-e", str(self.config.path)], check=False)
        except OSError as exc:
            QMessageBox.critical(
                self, APP_NAME, tr("Konfiguration nicht zu öffnen:\n{error}", error=exc)
            )

    def _reveal_log(self) -> None:
        try:
            subprocess.run(["open", "-R", str(self.log_file)], check=False)
        except OSError as exc:
            QMessageBox.critical(
                self, APP_NAME, tr("Logdatei nicht zu öffnen:\n{error}", error=exc)
            )

    def _clear_log(self) -> None:
        self.log_text.clear()

    def _reset_obstacles(self) -> None:
        if not self.runner.running():
            QMessageBox.information(
                self,
                APP_NAME,
                tr(
                    "Der Bot läuft nicht. Starte ihn erst - dann kann ich die Karte "
                    "zurücksetzen."
                ),
            )
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            tr(
                "Gelernte Hindernisse der aktuellen Karte löschen?\n"
                "Der Bot sucht sie danach neu (dauert ein paar Kämpfe)."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        removed = self.runner.reset_obstacles()
        if removed < 0:
            self.hint_label.setText(tr("Zurücksetzen nicht möglich - Bot läuft nicht."))
            return
        self.hint_label.setText(
            tr("{count} Hindernisse gelöscht - der Bot lernt die Karte neu.", count=removed)
        )

    # ------------------------------------------------------------------- Ende
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt-Signatur
        if self.runner.running():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                tr("Der Bot läuft noch. Beenden und Spielverbindung trennen?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.stop()
            deadline = time.time() + 4.0
            while self.runner.running() and time.time() < deadline:
                time.sleep(0.1)
        event.accept()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="WowFarmBot - grafische Oberfläche")
    parser.add_argument(
        "--config",
        type=Path,
        help="eigene config.json benutzen (Standard: ~/Library/Application Support/WowFarmBot)",
    )
    parser.add_argument(
        "--selftest",
        type=float,
        default=0.0,
        metavar="SEKUNDEN",
        help="ohne Fenster starten, kurz laufen lassen und wieder beenden (Prüfung)",
    )
    args = parser.parse_args(argv)

    path = args.config.expanduser().resolve() if args.config else support_dir() / "config.json"
    config = ConfigFile(path)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    window = WowFarmBotWindow(config)

    if args.selftest:
        window.hide()
        QTimer.singleShot(400, window.start_bot)

        def _finish() -> None:
            snap = window.runner.snapshot() or {}
            print(f"SELFTEST laeuft={window.runner.running()}")
            if snap:
                print(
                    "SELFTEST status:"
                    f" HP {snap.get('hp')}/{snap.get('max_hp')}"
                    f" ({snap.get('hp_pct', 0):.0f} %),"
                    f" Stufe {snap.get('level')},"
                    f" EP {snap.get('xp')}/{snap.get('next_xp')}"
                    f" ({snap.get('xp_pct', 0):.1f} %),"
                    f" Kills {snap.get('kills')},"
                    f" Loot {snap.get('loot')},"
                    f" Pos {snap.get('pos')},"
                    f" Ziel {snap.get('target') or '-'}"
                )
            print(
                f"SELFTEST karte={snap.get('map')} hindernisse={snap.get('walls')}"
                f" | Knopf aktiv={window.reset_btn.isEnabled()}"
            )
            view = window.runner.map_view() or {}
            print(
                f"SELFTEST kartenansicht: karte={view.get('map')}"
                f" hindernisse={len(view.get('walls') or [])}"
                f" mobs={len(view.get('mobs') or [])}"
                f" pos={view.get('pos')}"
                f" ziel={'ja' if view.get('target') else 'nein'}"
            )
            window.map_canvas.resize(700, 420)
            window.map_canvas.repaint()
            print("SELFTEST karte gezeichnet:", window.map_canvas.grab().size().toTuple())
            rules = MobRules.from_behavior(
                (window._config_from_fields() or {}).get("behavior", {})
            )
            loot_rules = LootRules.from_behavior(
                (window._config_from_fields() or {}).get("behavior", {})
            )
            print(
                f"SELFTEST regeln: ziele={rules.summary(snap.get('level') or 0)}"
                f" | beute={loot_rules.summary()}"
                f" | tabellenzeilen={window.mob_table.rowCount()}"
                f"/{window.loot_table.rowCount()}"
            )
            mobs = snap.get("nearby") or []
            print(f"SELFTEST mops_nah={len(mobs)}")
            for mob in mobs[:3]:
                print(
                    f"SELFTEST   {mob['name']} Lvl {mob['level']}"
                    f" {mob['hp']}/{mob['max_hp']} HP ({mob['hp_pct']:.0f} %)"
                    f" {mob['dist']:.0f} yd"
                    f" angreifbar={'ja' if mob['attackable'] else 'nein'}"
                    + (" <- Ziel" if mob['target'] else "")
                )
            zone = window.map_zone
            if zone:
                print(
                    f"SELFTEST zonenkarte={zone['label']} ({zone['name']})"
                    f" {zone['width']}x{zone['height']} px"
                    f" grenzen=({zone['left']:.0f},{zone['right']:.0f},{zone['top']:.0f},{zone['bottom']:.0f})"
                    f" datei={Path(zone['path']).name}"
                )
            else:
                print(
                    f"SELFTEST zonenkarte=nein fehler={window.map_error}"
                    f" hinweis={window.map_message}"
                )
            print(
                f"SELFTEST gelendekacheln={len(window.map_canvas.tiles)}"
                f" sichtbar={window.map_canvas.zoom:.0f} yd"
            )
            print(f"SELFTEST client={window.map_client}")
            print(f"SELFTEST logzeilen={window.log_text.blockCount()}")
            print(
                f"SELFTEST hud: spieler={window.me_frame.name_label.text()!r}"
                f" ziel={window.target_frame.name_label.text()!r}"
                f" zaehler={window.kills_value.text()}/{window.loot_value.text()}"
                f" symbole={len(window.mob_icons)}"
                f" kartenfenster={window.map_canvas.icons is window.mob_icons}"
            )
            window.stop_bot()
            QTimer.singleShot(2500, app.quit)

        QTimer.singleShot(int(args.selftest * 1000), _finish)
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
