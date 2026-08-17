#!/usr/bin/env python3
"""k_stabilitaet.py — haelt die Firma eine Stunde Grundlast durch?

F8 = C, F8.C.3 = b: eine Stunde Grundlast. Nicht Spitzenlast -- die verkraftet
fast jedes System kurz. Gesucht wird das, was erst mit der Zeit auftritt:
Speicher, der nicht zurueckgegeben wird, ein Dienst, der leise abstirbt, eine
Sperre, die haengen bleibt.

WARUM DIESE KATEGORIE GANZ AM ENDE STEHT (F24 = A)
Sie ist die einzige, deren Abbruch nichts kostet: Ist alles davor durch, ist
eine Stunde Messzeit gut angelegt. Umgekehrt waere sie verbrannt -- faellt
Security in Minute zwei, hat die Stunde davor nichts bewiesen.

WAS SIE BEWUSST NICHT TUT
Sie erzeugt keine kuenstliche Last mit Denkaufgaben. Ein Modelllauf je Minute
ueber eine Stunde kostet die GPU eine Stunde und misst am Ende die Denkzeit,
nicht die Stabilitaet. Beobachtet wird der LAUFENDE Betrieb: Dienste, Speicher,
Praesenz, haengende Auftraege. Wer messen will, ob ein Dauerlauf schadet, darf
den Dauerlauf nicht selbst verursachen.

DAUER IST EINSTELLBAR
Eine Stunde ist die Vorgabe. Fuer einen Zwischenlauf genuegt weniger --
FIRMA_STABIL_MINUTEN setzt es herunter, damit die Palette nicht bei jeder
Kleinigkeit eine Stunde blockiert. Der Wert landet im Messwert: Eine
Zehn-Minuten-Messung ist kein Stundenbeweis, und das soll sichtbar bleiben.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

CFG = json.loads((FRAMEWORK / "firma.config.json").read_text(encoding="utf-8"))

MINUTEN = int(os.environ.get("FIRMA_STABIL_MINUTEN") or 60)
TAKT_S = 30

# Ab wann ein Speicherzuwachs als Leck gilt. Prozesse wachsen normal ein wenig
# (Puffer, Caches); ein Leck waechst STETIG. Geprueft wird deshalb der Zuwachs
# ueber die ganze Messzeit, nicht der Ausschlag zwischen zwei Messungen.
LECK_MB = 300

# Um so viele Vorgaenge darf der Stau waehrend der Messung wachsen, bevor es ein
# Befund ist. Ein einzelner neuer Vorgang ist normaler Betrieb.
STAU_ZUWACHS = 3


def _dienste() -> dict[str, str]:
    e = subprocess.run(
        ["systemctl", "--user", "show", "-p", "Id,ActiveState", "--value",
         "buchhalter-broker.service"], capture_output=True, text=True)
    zustand = {"buchhalter-broker": (e.stdout.strip().splitlines() or ["?"])[-1]}
    for a in sorted(p.parent.name for p in
                    (FRAMEWORK.parent).glob("*/personalakte.json")):
        e = subprocess.run(["systemctl", "--user", "is-active",
                            f"mitarbeiter@{a}"], capture_output=True, text=True)
        zustand[a] = e.stdout.strip() or "?"
    return zustand


def _rss_mb() -> dict[str, float]:
    """Speicher der Firmenprozesse, je Agent."""
    aus = {}
    e = subprocess.run(["ps", "-eo", "rss,args", "--no-headers"],
                       capture_output=True, text=True)
    for zeile in e.stdout.splitlines():
        teile = zeile.strip().split(None, 1)
        if len(teile) != 2:
            continue
        rss, args = teile
        if "mitarbeiter_agent.py" in args:
            name = args.rsplit(None, 1)[-1]
            aus[name] = round(int(rss) / 1024, 1)
        elif "broker.py" in args:
            aus["broker"] = round(int(rss) / 1024, 1)
    return aus


def grundlast_ueberdauern() -> tuple[bool, str, dict]:
    """Beobachten, nicht belasten. Rueckgabe (stabil?, Befund, Messwerte)."""
    start = time.time()
    ende = start + MINUTEN * 60
    erste_rss = _rss_mb()
    ausfaelle: list[str] = []
    haenger: list[str] = []
    offen_zuerst: int | None = None
    proben = 0

    import speicher

    while time.time() < ende:
        proben += 1
        # 1. Laufen alle Dienste noch?
        for name, zustand in _dienste().items():
            if zustand not in ("active", "activating"):
                eintrag = f"{name}: {zustand}"
                if eintrag not in ausfaelle:
                    ausfaelle.append(eintrag)

        # 2. WAECHST der Rueckstand? Nicht die Zahl offener Vorgaenge zaehlt --
        #    der erste Entwurf meldete 13 Stueck und lag damit fachlich richtig,
        #    aber am falschen Ort: Das ist ein Rueckstand, keine Instabilitaet.
        #    Ein Vorgang, der auf Chef wartet, wartet zu Recht. Instabil ist
        #    ein Stau, der WAEHREND der Messung anwaechst, ohne dass etwas
        #    abgearbeitet wird.
        try:
            with speicher.verbindung() as c:
                z = c.execute(
                    "SELECT count(*) FROM firma.vorgaenge WHERE status = 'offen' "
                    "AND wartet_auf <> 'chef'").fetchone()
            offen_jetzt = int(z[0]) if z else 0
            if offen_zuerst is None:
                offen_zuerst = offen_jetzt
            elif offen_jetzt > offen_zuerst + STAU_ZUWACHS:
                eintrag = (f"Rueckstand waechst: {offen_zuerst} -> {offen_jetzt} "
                           f"Vorgaenge an Agenten, nichts abgearbeitet")
                if eintrag not in haenger:
                    haenger.append(eintrag)
        except Exception as e:                                 # noqa: BLE001
            haenger.append(f"Datenbank nicht lesbar: {e!r}")

        time.sleep(min(TAKT_S, max(1, ende - time.time())))

    letzte_rss = _rss_mb()
    zuwachs = {k: round(letzte_rss[k] - erste_rss.get(k, letzte_rss[k]), 1)
               for k in letzte_rss}
    lecks = [f"{k}: +{v} MB" for k, v in zuwachs.items() if v > LECK_MB]

    befunde = ausfaelle + haenger + lecks
    mess = {"minuten": MINUTEN, "proben": proben, "zuwachs_mb": zuwachs,
            "offen_an_agenten": offen_zuerst, "vollstaendig": MINUTEN >= 60}
    if befunde:
        return False, "; ".join(befunde[:5]), mess
    return True, (f"{MINUTEN} Minuten Grundlast ohne Ausfall, ohne haengende "
                  f"Vorgaenge, ohne Speicherleck ({proben} Proben)"), mess


TESTS = [
    {"name": "grundlast_ueberdauern", "lauf": grundlast_ueberdauern},
]
