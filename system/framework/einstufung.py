#!/usr/bin/env python3
"""einstufung.py — einen GEMESSENEN Leistungswert je Knoten hinterlegen.

Der dispatcher waehlt unter den geeigneten Knoten den mit der hoechsten
`einstufung` (siehe dispatcher.py, knoten_fuer). Dieses Skript ist der
vorgesehene Weg, den Wert nach einem Benchmark in hosts.json zu schreiben. Von
Hand in der JSON zu editieren ginge auch -- aber dann verrutscht leicht die
Einrueckung oder ein Komma fehlt, ohne dass etwas rot wird, und der Dispatcher
liest still den alten Wert weiter.

Absichtlich formatunabhaengig: Es nimmt eine fertige ZAHL entgegen, keine
3DMark-Datei. Welcher Benchmark die Zahl liefert (3DMark-GPU-Score, ein
LLM-Durchsatz in tokens/s, ...), bleibt dir ueberlassen -- Bedingung ist nur:
ueber ALLE Knoten dieselbe Messgroesse. Ein 3DMark-Score auf dem einen und
tokens/s auf dem anderen Knoten ergaeben eine Reihenfolge, die nichts misst.

    venv/bin/python einstufung.py --zeigen
    venv/bin/python einstufung.py rechner-eins 9800
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HOSTS_DATEI = Path(__file__).resolve().parent / "hosts.json"


def _laden() -> dict:
    return json.loads(HOSTS_DATEI.read_text(encoding="utf-8"))


def _speichern(daten: dict) -> None:
    # Mit abschliessendem Newline und ohne ASCII-Escapes: so bleibt die Datei
    # lesbar und der Diff klein, wenn sich nur eine Zahl aendert.
    HOSTS_DATEI.write_text(
        json.dumps(daten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def setzen(knoten: str, wert: float) -> None:
    daten = _laden()
    hosts = daten.get("hosts", {})
    if knoten not in hosts:
        # Ein Tippfehler im Knotennamen legte sonst still ein NEUES, nirgends
        # erreichbares Feld an -- der Dispatcher merkt davon nichts, die Messung
        # waere verloren. Also lieber laut abbrechen und die bekannten Namen
        # nennen.
        bekannt = ", ".join(hosts) or "(keine)"
        raise SystemExit(f"Kein Knoten '{knoten}' im Inventar. Bekannt: {bekannt}")
    hosts[knoten]["einstufung"] = wert
    _speichern(daten)
    print(f"  {knoten}: einstufung = {wert:g}")


def zeigen() -> None:
    for name, h in _laden().get("hosts", {}).items():
        wert = h.get("einstufung")
        stand = f"{wert:g}" if wert else "nicht eingestuft"
        print(f"  {name:12} {stand}")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--zeigen":
        zeigen()
    elif len(sys.argv) == 3:
        try:
            wert = float(sys.argv[2])
        except ValueError:
            raise SystemExit(f"'{sys.argv[2]}' ist keine Zahl.")
        if wert < 0:
            raise SystemExit("Eine Einstufung ist nie negativ.")
        # Ganze Zahlen ganz lassen (9800 statt 9800.0) -- haelt hosts.json und
        # den Diff sauber.
        setzen(sys.argv[1], int(wert) if wert.is_integer() else wert)
    else:
        raise SystemExit(__doc__)
