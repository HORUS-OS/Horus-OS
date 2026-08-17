#!/usr/bin/env python3
"""voraussetzungen.py — prueft, was zum Betrieb fehlt, statt es zu behaupten.

Chef am 2026-08-17: "Was ebenfalls fehlt, ist ein Tool oder zumindest eine
Liste, die die benoetigten Programme aufzeigt."

WARUM EIN PRUEFER UND KEINE LISTE
Eine Liste in einem README veraltet ab dem Tag, an dem sie geschrieben wird --
sie steht neben dem Code, nicht darin. Dieses Modul FRAGT das System: Ist Python
neu genug? Antwortet Ollama? Steht Postgres? Damit ist die Antwort immer aktuell,
und sie unterscheidet zwischen "fehlt" und "ist da, laeuft aber nicht" -- zwei
Zustaende, die verschiedene Handgriffe brauchen.

PFLICHT ODER KUER
Nicht alles ist noetig. Ohne GPU laeuft alles, nur langsamer; ohne WireGuard
laeuft alles auf einem Rechner. Was Pflicht ist, steht in `PFLICHT` -- und nur
das laesst diesen Pruefer mit einem Fehler enden.

Aufruf:
    voraussetzungen.py            pruefen und Bericht ausgeben
    voraussetzungen.py --kurz     nur die Fehlenden
    voraussetzungen.py --anleitung  Installationsbefehle fuer das Fehlende
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent

# (Name, Pflicht?, Pruefung, Wofuer, Installationshinweis)
# Die Pruefung ist ein Aufruf, nicht eine Vermutung: `shutil.which` sagt, ob ein
# Programm da ist, ein Verbindungsversuch sagt, ob ein Dienst antwortet.
PROGRAMME = [
    ("python3 >= 3.11", True, lambda: sys.version_info >= (3, 11),
     "Die Laufzeit nutzt `X | None`-Typen und match-Ausdruecke.",
     "apt install python3"),
    ("git", True, lambda: bool(shutil.which("git")),
     "Der Stand wird ueber Git verteilt (standverteiler.py).",
     "apt install git"),
    ("postgresql-client", False, lambda: bool(shutil.which("psql")),
     "Nur fuer Wartung von Hand; die Agenten sprechen ueber psycopg.",
     "apt install postgresql-client"),
    ("ollama", True, lambda: bool(shutil.which("ollama")),
     "Lokales Sprachmodell. Ohne Ollama denkt niemand.",
     "curl -fsSL https://ollama.com/install.sh | sh"),
    ("systemd (user)", True,
     lambda: _befehl_ok(["systemctl", "--user", "is-system-running"], erlaube_fehler=True),
     "Ein Dienst je Angestelltem, plus Timer.",
     "gehoert zu jeder gaengigen Linux-Distribution"),
    ("ssh", False, lambda: bool(shutil.which("ssh")),
     "Fuer Tests auf anderen Knoten und die Sandbox-Beta.",
     "apt install openssh-client"),
    ("wireguard", False, lambda: bool(shutil.which("wg")),
     "Verbindet Rechner ueber Netzgrenzen. Auf einem Einzelrechner unnoetig.",
     "apt install wireguard"),
    ("nvidia-smi", False, lambda: bool(shutil.which("nvidia-smi")),
     "Nur fuer die GPU-Verwaltung des Brokers (VRAM messen, freiraeumen).",
     "Treiberpaket der Distribution, z.B. nvidia-driver-580"),
]

PYTHON_PAKETE = [
    ("nio", True, "matrix-nio", "Matrix-Client — ohne ihn kein Kollege im Raum."),
    ("requests", True, "requests", "HTTP zu Ollama und zum Broker."),
    ("psycopg", True, "psycopg[binary]",
     "Postgres. Zustand, Vorgaenge, Testergebnisse."),
]

DIENSTE = [
    ("Ollama", "http", "127.0.0.1", 11434,
     "Antwortet das Modell-Backend?", "systemctl start ollama"),
    ("Broker", "http", "127.0.0.1", 8900,
     "Das GPU-Tor der Firma (optional, aber empfohlen).",
     "systemctl --user start buchhalter-broker"),
]


def _befehl_ok(befehl: list[str], erlaube_fehler: bool = False) -> bool:
    try:
        e = subprocess.run(befehl, capture_output=True, timeout=10)
        return True if erlaube_fehler else e.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _port_offen(host: str, port: int, frist: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=frist):
            return True
    except OSError:
        return False


def pruefen() -> dict:
    """Rueckgabe: {"programme": [...], "pakete": [...], "dienste": [...]}"""
    ergebnis: dict[str, list[dict]] = {"programme": [], "pakete": [], "dienste": []}

    for name, pflicht, pruefung, wofuer, hinweis in PROGRAMME:
        try:
            da = bool(pruefung())
        except Exception:                                      # noqa: BLE001
            da = False
        ergebnis["programme"].append(
            {"name": name, "pflicht": pflicht, "da": da,
             "wofuer": wofuer, "hinweis": hinweis})

    for modul, pflicht, paket, wofuer in PYTHON_PAKETE:
        try:
            __import__(modul)
            da = True
        except ImportError:
            da = False
        ergebnis["pakete"].append(
            {"name": paket, "pflicht": pflicht, "da": da, "wofuer": wofuer,
             "hinweis": f"pip install {paket}"})

    for name, _art, host, port, wofuer, hinweis in DIENSTE:
        ergebnis["dienste"].append(
            {"name": f"{name} ({host}:{port})", "pflicht": False,
             "da": _port_offen(host, port), "wofuer": wofuer, "hinweis": hinweis})

    return ergebnis


def bericht(e: dict, kurz: bool = False) -> str:
    zeilen = []
    for gruppe, titel in (("programme", "Programme"),
                          ("pakete", "Python-Pakete"),
                          ("dienste", "Laufende Dienste")):
        eintraege = [x for x in e[gruppe] if not (kurz and x["da"])]
        if not eintraege:
            continue
        zeilen.append(f"\n{titel}:")
        for x in eintraege:
            zeichen = "✓" if x["da"] else ("✗" if x["pflicht"] else "·")
            art = "" if x["pflicht"] else "  (optional)"
            zeilen.append(f"  {zeichen} {x['name']}{art}")
            if not x["da"]:
                zeilen.append(f"      {x['wofuer']}")
                zeilen.append(f"      → {x['hinweis']}")
    fehlt_pflicht = [x for g in e.values() for x in g
                     if x["pflicht"] and not x["da"]]
    zeilen.append("")
    if fehlt_pflicht:
        zeilen.append(f"{len(fehlt_pflicht)} Pflicht-Voraussetzung(en) fehlen: "
                      + ", ".join(x["name"] for x in fehlt_pflicht))
    else:
        zeilen.append("Alle Pflicht-Voraussetzungen erfuellt.")
    return "\n".join(zeilen)


def anleitung(e: dict) -> str:
    """Nur die Befehle, die noetig sind -- copy-und-paste-fertig."""
    fehlt = [x for g in e.values() for x in g if not x["da"] and x["pflicht"]]
    if not fehlt:
        return "# Nichts zu tun — alle Pflicht-Voraussetzungen sind erfuellt."
    zeilen = ["# Fehlende Pflicht-Voraussetzungen installieren:"]
    for x in fehlt:
        zeilen.append(f"# {x['name']}: {x['wofuer']}")
        zeilen.append(x["hinweis"])
    return "\n".join(zeilen)


def haupt() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kurz", action="store_true", help="nur was fehlt")
    ap.add_argument("--anleitung", action="store_true",
                    help="Installationsbefehle ausgeben")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    e = pruefen()
    if a.json:
        print(json.dumps(e, ensure_ascii=False, indent=2))
    elif a.anleitung:
        print(anleitung(e))
    else:
        print(bericht(e, a.kurz))
    fehlt = [x for g in e.values() for x in g if x["pflicht"] and not x["da"]]
    return 1 if fehlt else 0


if __name__ == "__main__":
    raise SystemExit(haupt())
