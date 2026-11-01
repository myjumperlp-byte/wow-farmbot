"""Headless Farming-Bot für World of Warcraft 3.3.5a (Build 12340)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["bot", "auth", "world", "objects", "protocol", "crypto", "opcodes"]
__version__ = "1.0.0"

APP_NAME = "WowFarmBot"


def app_support_dir(*parts: str) -> Path:
    """Ordner für Config, Logs und gelerntes Gelände - pro Betriebssystem.

    macOS: ~/Library/Application Support/WowFarmBot
    Windows: %APPDATA%/WowFarmBot
    sonst: ~/.config/WowFarmBot (bzw. $XDG_CONFIG_HOME)
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base.joinpath(APP_NAME, *parts)
