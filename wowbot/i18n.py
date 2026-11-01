"""Sprachen der Oberfläche.

Deutsch ist die Ausgangssprache: alle Texte stehen im Code deutsch, `tr()` schlägt
die Übersetzung nach. Fehlt ein Eintrag, bleibt der deutsche Text stehen - es ist
also nie etwas leer, und eine neue Sprache braucht nur einen weiteren Block in
`STRINGS`.

Texte mit Platzhaltern (`{path}`, `{count}` …) werden mit `tr("… {count} …",
count=3)` übersetzt; der Platzhalter muss in beiden Sprachen gleich heißen.
"""

from __future__ import annotations

# (Schlüssel, Name in der Sprache selbst, Länderkürzel für die Flagge)
LANGUAGES: "tuple[tuple[str, str, str], ...]" = (
    ("de", "Deutsch", "DE"),
    ("en", "English", "GB"),
)

DEFAULT_LANGUAGE = "de"

_current = DEFAULT_LANGUAGE


def language() -> str:
    """Aktuell eingestellte Sprache (z. B. "de")."""
    return _current


def set_language(code: "str | None") -> str:
    """Sprache setzen; unbekannte Kürzel werden ignoriert."""
    global _current
    wanted = str(code or "").strip().lower()
    if any(wanted == key for key, _name, _flag in LANGUAGES):
        _current = wanted
    return _current


def language_name(code: str) -> str:
    for key, name, _flag in LANGUAGES:
        if key == code:
            return name
    return code


def tr(text: "str | None", **values) -> str:
    """Text in die aktuelle Sprache übersetzen (fehlende Einträge bleiben deutsch)."""
    original = "" if text is None else str(text)
    table = STRINGS.get(_current) or {}
    translated = table.get(original, original)
    if values:
        try:
            return translated.format(**values)
        except (KeyError, IndexError, ValueError):
            return translated
    return translated


# --------------------------------------------------------------------- Englisch
STRINGS: "dict[str, dict[str, str]]" = {
    "en": {
        # --- Reiter
        "Bot": "Bot",
        "Ingame Karte": "In-game map",
        "Ziele && Beute": "Targets && Loot",
        "WoW-Version": "WoW version",
        # --- Bot-Reiter: Felder und Knöpfe
        "Server": "Server",
        "Port": "Port",
        "Realm": "Realm",
        "Konto": "Account",
        "Passwort": "Password",
        "Charakter": "Character",
        "Server und Konto": "Server and account",
        "Ereignisse": "Events",
        "Mobs in der Nähe": "Nearby units",
        "▶  Start": "▶  Start",
        "■  Stopp": "■  Stop",
        "Log leeren": "Clear log",
        "Logdatei zeigen": "Show log file",
        "Speichern": "Save",
        "Einstellungen speichern": "Save settings",
        "Config-Datei öffnen": "Open config file",
        "Netzwerk-Pakete mitschreiben (DEBUG)": "Record network packets (DEBUG)",
        "keine Einheiten in der Nähe": "no nearby units",
        "Bot nicht verbunden": "Bot not connected",
        "leer = erster Charakter": "empty = first character",
        # --- Ziele & Beute
        "Welche Mobs angreifen": "Which mobs to attack",
        "Regel:": "Rule:",
        "Alle angreifbaren Mobs": "All attackable mobs",
        "Nur die angehakten Mobs": "Only the checked mobs",
        "Alles, was angreifbar ist - außer den Mobs, die unten nicht angehakt sind.":
            "Everything attackable - except the mobs not checked below.",
        "Nur angehakte Mobs werden angegriffen, alles andere bleibt stehen.":
            "Only checked mobs are attacked, everything else is left alone.",
        "angreifen": "attack",
        "Mob": "Mob",
        "Hinzufügen": "Add",
        "Aus Umgebung übernehmen": "Take from surroundings",
        "Alle Einheiten aus „Mobs in der Nähe“ (auch neutrale) in die Tabelle holen.":
            "Copy every unit from “Nearby units” (neutrals too) into the table.",
        "Alle angreifen": "Attack all",
        "Keinen angreifen": "Attack none",
        "Zeile entfernen": "Remove row",
        "Haken = angreifen. Ohne Haken wird der Mob stehen gelassen (gilt auch bei „Alle angreifbaren Mobs“).":
            "Tick = attack. Unticked mobs are left alone (applies to “All attackable mobs” too).",
        "Name (Teilstück) oder Entry-Nummer, z. B. 15648":
            "Name (part of it) or entry number, e.g. 15648",
        "Name (Teilstück) oder Item-Nummer, z. B. 2589":
            "Name (part of it) or item number, e.g. 2589",
        "Namen sind Teilstücke (Groß-/Kleinschreibung egal): „spinne“ trifft „Waldspinne“. Bei „Alle angreifbaren Mobs“ trägt das Fenster gesehene Mobs mit Namen und Stufe selbst ein.":
            "Names are fragments (case-insensitive): “spider” matches “Forest Spider”. "
            "With “All attackable mobs” the window adds seen mobs with name and level itself.",
        "Stufe der Mobs": "Mob level",
        "Alle Stufen": "All levels",
        "Gleiche Stufe wie ich": "Same level as me",
        "Gleiche Stufe und höher": "Same level and higher",
        "Gleiche Stufe und niedriger": "Same level and lower",
        "Eigene Stufe ±": "My level ±",
        "Mobs ohne erkennbare Stufe („??“, Bosse) gelten immer als passend.":
            "Mobs without a visible level (“??”, bosses) always count as matching.",
        "Was eingesammelt wird": "What is picked up",
        "Leichen überhaupt looten": "Loot corpses at all",
        "Aus = der Bot lässt jede Leiche liegen (auch Geld).":
            "Off = the bot leaves every corpse alone (money too).",
        "Alles - außer den Gegenständen ohne Haken.":
            "Everything - except the items without a tick.",
        "Nur die angehakten Gegenstände": "Only the checked items",
        "Alles andere bleibt auf der Leiche liegen.":
            "Everything else stays on the corpse.",
        "Alles aufheben": "Take everything",
        "Nichts aufheben": "Take nothing",
        "aufheben": "take",
        "Gegenstand": "Item",
        "Haken = einpacken. Geld wird immer mitgenommen, solange Looten an ist.":
            "Tick = pick up. Money is always taken as long as looting is on.",
        "Namen erscheinen nach dem ersten Loot automatisch; bis dahin genügt die Item-Nummer. Geld wird immer mitgenommen.":
            "Names appear automatically after the first loot; until then the item number is enough. "
            "Money is always taken.",
        "Änderungen gelten sofort - auch im laufenden Bot. *Speichern* hält sie fest.":
            "Changes apply immediately - even while the bot runs. *Save* makes them permanent.",
        # --- Ingame Karte
        "Norden oben · Mausrad zoomt · Mobs laufen live mit · Maus über einen Mob = Infofenster · Linksklick = dorthin laufen (bricht auch einen Kampf ab), Klick auf einen Mob = den zuerst angreifen, Shift+Klick = Hotspot setzen, Rechtsklick = Hotspot löschen":
            "North up · mouse wheel zooms · mobs move live · hover a mob = info window · "
            "left click = walk there (breaks a fight too), click a mob = attack that one first, "
            "shift+click = set a hotspot, right click = delete a hotspot",
        "Kills": "Kills",
        "Beute": "Loot",
        "Kupfer": "Copper",
        "Objekte": "Objects",
        "Zeit": "Time",
        "Stufe": "Level",
        "{name}  ·  Stufe {level}": "{name}  ·  Level {level}",
        "EP {xp}/{next_xp}   ({pct} %)": "XP {xp}/{next_xp}   ({pct} %)",
        "EP {xp} · Stufe unbekannt": "XP {xp} · level unknown",
        # Kraftarten (die Namen selbst, nicht der Messwert)
        "Mana": "Mana",
        "Wut": "Rage",
        "Fokus": "Focus",
        "Energie": "Energy",
        "Zufriedenheit": "Happiness",
        "Runen": "Runes",
        "Runenmacht": "Runic Power",
        "●  bereit": "●  ready",
        "●  läuft": "●  running",
        "●  verbinde …": "●  connecting …",
        "●  stoppe …": "●  stopping …",
        "●  tot - warte auf Wiederbelebung": "●  dead - waiting to be revived",
        "kein Kartenbefehl": "no map command",
        "Linksklick in die Karte = hinlaufen (auch mitten im Kampf), Klick auf einen Mob = diesen zuerst angreifen":
            "Left click on the map = walk there (mid-fight too), click a mob = attack that one first",
        "Hindernisse: noch keine gelernt": "obstacles: none learned yet",
        "Gelerntes Gelände dieser Karte (im Ordner „nav“ des App-Datenordners)":
            "Terrain learned for this map (in the “nav” folder of the app data directory)",
        "Nur während der Bot läuft: die aktuelle Karte ist sonst nicht bekannt.":
            "Only while the bot runs: otherwise the current map is unknown.",
        "Hindernisse zurücksetzen": "Reset obstacles",
        "Ganze Zone": "Whole zone",
        "Auf die ganze Zonenkarte herauszoomen": "Zoom out to the whole zone map",
        "Lvl": "Lvl",
        "Leben": "Health",
        "yd": "yd",
        "angreifbar": "attackable",
        # --- WoW-Version
        "Client-Kennung": "Client identity",
        "Version:": "Version:",
        "Build:": "Build:",
        "Betriebssystem:": "Operating system:",
        "Plattform:": "Platform:",
        "Der Bot spricht das Protokoll von <b>3.3.5a (Build 12340)</b>. Läuft dein Server mit einer anderen Build-Nummer oder eigenen Opcode-/Feld-Nummern, stellst du das hier ein. Die Werte gelten <b>ab dem nächsten Start</b> des Bots.":
            "The bot speaks the protocol of <b>3.3.5a (build 12340)</b>. If your server uses "
            "another build number or its own opcode/field numbers, set that up here. The values "
            "apply <b>from the next bot start</b>.",
        "z. B. 3.3.5": "e.g. 3.3.5",
        "Wird im Login mitgeschickt. „3.3.5a“ ist auch erlaubt - daraus wird 3.3.5.":
            "Sent with the login. “3.3.5a” is allowed too - that becomes 3.3.5.",
        "Build-Nummer des Clients (3.3.5a = 12340). Steht im Login und in CMSG_AUTH_SESSION; ein falscher Wert wird vom Server abgelehnt.":
            "Client build number (3.3.5a = 12340). Sent in the login and in CMSG_AUTH_SESSION; "
            "a wrong value is rejected by the server.",
        "Client-Ordner (Kartenbilder und Mob-Symbole)": "Client folder (map images and mob icons)",
        "Client-Ordner …": "Client folder …",
        "Ordner mit Wow.exe und Data-Ordner wählen - dort liegen die Kartenbilder":
            "Choose the folder with Wow.exe and the Data folder - the map images live there",
        "Der Ordner mit <tt>Wow.exe</tt> und <tt>Data</tt> (MPQ-Archive). Daraus liest der Bot Zonenkarte, Geländekacheln und Mob-Symbole. Ohne Client läuft der Bot normal weiter - nur ohne Karte.":
            "The folder with <tt>Wow.exe</tt> and <tt>Data</tt> (MPQ archives). The bot reads the "
            "zone map, terrain tiles and mob icons from it. Without a client the bot keeps "
            "working - just without a map.",
        "Opcode- und Feld-Nummern": "Opcode and field numbers",
        "Die Zahlen selbst stehen in der Config unter <tt>protocol</tt> (<tt>opcodes</tt>, <tt>fields</tt>, <tt>flags</tt>). Damit du sie nicht abtippen musst: <b>Vorlage schreiben</b> legt eine Datei mit allen aktuellen Werten an (Name → Zahl), die du in einem Texteditor änderst und mit <b>Protokoll-Datei laden</b> wieder einliest.":
            "The numbers themselves live in the config under <tt>protocol</tt> (<tt>opcodes</tt>, "
            "<tt>fields</tt>, <tt>flags</tt>). So you don't have to type them: <b>Write template</b> "
            "creates a file with all current values (name → number); edit it in a text editor and "
            "read it back with <b>Load protocol file</b>.",
        "Vorlage schreiben …": "Write template …",
        "Schreibt alle bekannten Namen mit ihren aktuellen Nummern als JSON - zum Bearbeiten und späteren Einlesen.":
            "Writes every known name with its current number as JSON - for editing and reading back.",
        "Protokoll-Datei laden …": "Load protocol file …",
        "Liest eine solche Datei ein. Unbekannte Namen und falsche Gruppen werden abgelehnt und im Log gemeldet.":
            "Reads such a file. Unknown names and wrong groups are rejected and reported in the log.",
        "Eigene Nummern löschen": "Delete custom numbers",
        "Zurück auf die Werte von 3.3.5a (Build 12340).": "Back to the 3.3.5a values (build 12340).",
        "Damit passen sich die <b>Nummern</b> an - nicht der Aufbau der Pakete. Eine andere Protokollgeneration (z. B. 4.x) tauscht zusätzlich Felder und Paketinhalte aus und braucht echte Code-Änderungen; dafür ist dieser Reiter nicht gedacht.":
            "This adapts the <b>numbers</b> - not the packet layout. A different protocol "
            "generation (4.x for example) also swaps fields and packet contents and needs real "
            "code changes; that is not what this tab is for.",
        "Vorlage geschrieben": "Template written",
        "Datei nicht lesbar": "File not readable",
        "Datei nicht brauchbar": "File not usable",
        "Erwartet wird ein JSON-Objekt.": "A JSON object is expected.",
        "Protokoll geladen": "Protocol loaded",
        "{count} Wert(e) übernommen.": "{count} value(s) applied.",
        "Nicht übernommen: {names}": "Not applied: {names}",
        # --- dynamische Anzeigen
        "Position {x} / {y}": "position {x} / {y}",
        "Deine Stufe: {level}": "your level: {level}",
        "Deine Stufe: unbekannt": "your level: unknown",
        "Karte {map} · {walls} Hindernisse gespeichert": "map {map} · {walls} obstacles saved",
        "Karte {map} · noch keine Hindernisse gelernt": "map {map} · no obstacles learned yet",
        "Hindernisse: {count} für Map {map} gespeichert": "obstacles: {count} saved for map {map}",
        "Eingestellt: {summary} · gilt ab dem nächsten Start": "set: {summary} · applies from the next start",
        "Client: {path}": "client: {path}",
        "Client-Pfad nicht gefunden: {path} - bitte neu wählen.":
            "client path not found: {path} - please choose again.",
        "Kein Client gefunden - Kartenbilder brauchen den WoW-Ordner (Data mit MPQ-Dateien).":
            "No client found - map images need the WoW folder (Data with MPQ files).",
        "Bot nicht verbunden - starte ihn, dann füllt sich die Karte (und Klicks setzen Hotspots)":
            "Bot not connected - start it and the map fills up (clicks set hotspots)",
        "Karte {map}{zone} · {tiles} Geländekacheln · {walls} Hindernisse{hotspots} · {zoom} yd breit":
            "map {map}{zone} · {tiles} terrain tiles · {walls} obstacles{hotspots} · {zoom} yd wide",
        " · Kartenbild: {error}": " · map image: {error}",
        " · {count} Hotspots": " · {count} hotspots",
        "Befehl: laufe zu {x} / {y}": "command: walking to {x} / {y}",
        "Befehl: greife {name} an": "command: attacking {name}",
        "Bot gestartet - Ereignisse laufen hier ein.": "Bot started - events show up here.",
        "Bot wird gestoppt …": "Stopping the bot …",
        "Bot beendet.": "Bot stopped.",
        "Bot läuft nicht.": "The bot is not running.",
        "Zum Hinlaufen muss der Bot laufen - sonst gibt es kein Ziel in der Welt.":
            "The bot must be running to walk there - otherwise there is no target in the world.",
        "Zum Angreifen muss der Bot laufen.": "The bot must be running to attack.",
        "Zum Setzen von Hotspots muss der Bot laufen - sonst ist keine Position bekannt.":
            "The bot must be running to set hotspots - otherwise the position is unknown.",
        "Kein Hotspot in der Nähe des Klicks (Rechtsklick auf den Punkt).":
            "No hotspot near the click (right click the dot).",
        "Hotspot {index} gesetzt ({x}/{y}) - der Bot läuft ihn ab, wenn kein Mob in Reichweite ist.":
            "Hotspot {index} set ({x}/{y}) - the bot walks it when no mob is in range.",
        "Hotspot entfernt - {count} übrig.": "Hotspot removed - {count} left.",
        "Laufbefehl: {x}/{y} - der Bot ignoriert Ziele und Beute, bis er da ist.":
            "Walk order: {x}/{y} - the bot ignores targets and loot until it arrives.",
        "Angriffsbefehl: {name} wird zuerst angegriffen.": "Attack order: {name} is attacked first.",
        "{added} Mob(s) übernommen - {count} in der Tabelle.":
            "{added} mob(s) added - {count} in the table.",
        "Karten kommen jetzt aus {path}": "maps now come from {path}",
        "Hotspots konnten nicht gespeichert werden: {error}": "hotspots could not be saved: {error}",
        "Client-Pfad konnte nicht gespeichert werden: {error}":
            "client path could not be saved: {error}",
        "Gespeichert: {path}": "saved: {path}",
        "Noch keine Zonenkarte geladen - dafür muss der Bot laufen und ein Client eingestellt sein.":
            "No zone map loaded yet - the bot must run and a client must be set for that.",
        "Zurücksetzen nicht möglich - Bot läuft nicht.": "Cannot reset - the bot is not running.",
        "{count} Hindernisse gelöscht - der Bot lernt die Karte neu.":
            "{count} obstacles deleted - the bot relearns the map.",
        "Der Bot läuft nicht. Starte ihn erst - dann kann ich die Karte zurücksetzen.":
            "The bot is not running. Start it first - then the map can be reset.",
        "Gelernte Hindernisse der aktuellen Karte löschen?\nDer Bot sucht sie danach neu (dauert ein paar Kämpfe).":
            "Delete the learned obstacles of the current map?\n"
            "The bot will search them again afterwards (takes a few fights).",
        "Bitte Kontoname und Passwort eintragen.": "Please enter account name and password.",
        "Speichern fehlgeschlagen:\n{error}": "saving failed:\n{error}",
        "Konfiguration nicht zu öffnen:\n{error}": "cannot open the config:\n{error}",
        "Logdatei nicht zu öffnen:\n{error}": "cannot open the log file:\n{error}",
        "Der Bot läuft noch. Beenden und Spielverbindung trennen?":
            "The bot is still running. Quit and disconnect from the game?",
        "In diesem Ordner liegt kein WoW-Client - gesucht wird ein Data-Ordner mit MPQ-Dateien (z. B. common.MPQ).":
            "This folder holds no WoW client - a Data folder with MPQ files (e.g. common.MPQ) is expected.",
        # --- Karte (gezeichnet)
        "noch keine Hindernisse gelernt": "no obstacles learned yet",
        "Spieler": "Player",
        "Mob angreifbar": "mob, attackable",
        "neutral": "neutral",
        "freundlicher NPC": "friendly NPC",
        "Ziel": "target",
        "Kartenbefehl": "map command",
        "Hotspot": "hotspot",
        "Stufe {level}   ·   Entry {entry}": "Level {level}   ·   Entry {entry}",
        "Leben unbekannt": "health unknown",
        "aktuelles Ziel": "current target",
        "nach Regeln kein Ziel": "not a target by the rules",
        # --- Kleinkram
        "Sprache der Oberfläche": "Interface language",
        "ja": "yes",
        "nein": "no",
        "unbekannt": "unknown",
        "(ohne Namen)": "(no name)",
        " Stufen": " levels",
        "warte auf Ziel": "waiting for target",
        "kein Ziel": "no target",
        "{dist} yd entfernt · Entry {entry}": "{dist} yd away · entry {entry}",
        "{minutes} min im Einsatz · {objects} Objekte":
            "{minutes} min in action · {objects} objects",
        "nicht angreifbar (Vendor, Geistheiler, Freund)":
            "not attackable (vendor, spirit healer, friendly)",
        "Zahlen ändern, dann über „Protokoll-Datei laden“ einlesen.":
            "Change the numbers, then read them back via “Load protocol file”.",
        "Protokoll-Vorlage speichern": "Save protocol template",
        # --- Protokoll-Zusammenfassung
        "keine eigenen Werte (3.3.5a)": "no custom values (3.3.5a)",
        "{count} eigene Opcode(s)": "{count} custom opcode(s)",
        "{count} eigene Feld(er)": "{count} custom field(s)",
        "{count} eigene Flag(s)": "{count} custom flag(s)",
    }
}
