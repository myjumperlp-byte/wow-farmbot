#!/usr/bin/env python3
"""Startpunkt für den Farming-Bot.

Beispiel:
    python3 run_bot.py config.json
    python3 run_bot.py config.json --log-level DEBUG

Das Passwort wird, wenn es in der Config leer ist, interaktiv abgefragt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from pathlib import Path

from wowbot.auth import LogonError
from wowbot.bot import FarmBot
from wowbot.protocol_profile import apply_config
from wowbot.world import WorldError


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Config '{path}' nicht gefunden.\n"
            "Lege sie einmalig an mit:  cp config.example.json config.json\n"
            "und trage dort Passwort (und optional den Charakternamen) ein."
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Config '{path}' ist kein gültiges JSON (Zeile {exc.lineno}, Spalte {exc.colno}): {exc.msg}"
        ) from exc
    if "account" not in config or not config["account"].get("username"):
        raise SystemExit("Config braucht einen 'account'-Block mit username/password")
    if not config["account"].get("password"):
        config["account"]["password"] = getpass.getpass(
            f"Passwort für {config['account'].get('username', '?')}: "
        )
    return config


async def run(config: dict, trace: bool = False) -> int:
    # Client-Version, Opcodes und Felder aus der Config übernehmen
    apply_config(config)
    bot = FarmBot(config, trace=trace)
    try:
        await bot.connect()
        await bot.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.getLogger("wowbot").info("Abbruch durch Benutzer")
    except LogonError as exc:
        logging.getLogger("wowbot").error("%s", exc)
        return 1
    except WorldError as exc:
        logging.getLogger("wowbot").warning("Verbindung beendet: %s", exc)
        return 0
    except Exception as exc:  # noqa: BLE001 - Fehler sollen lesbar im Log landen
        logging.getLogger("wowbot").error("Bot abgebrochen: %s", exc, exc_info=True)
        return 1
    finally:
        await bot.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WoW 3.3.5a Farming-Bot")
    parser.add_argument("config", type=Path, help="Pfad zur JSON-Config")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Ausführlichkeit der Ausgabe",
    )
    parser.add_argument(
        "--log-packets",
        action="store_true",
        help="zusätzlich jedes gesendete und empfangene Netzwerk-Paket protokollieren",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-13s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.log_packets:
        logging.getLogger("wowbot.world").setLevel(logging.DEBUG)
    config = load_config(args.config)
    try:
        return asyncio.run(run(config, trace=args.log_packets))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
