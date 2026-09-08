#!/usr/bin/env python3
"""durchsatz.py — misst den LLM-Durchsatz eines Knotens in Tokens/s.

Fuer KI-Last ist nicht der reine Grafik-Score (3DMark) entscheidend, sondern wie
schnell das Modell auf diesem Knoten TOKENS erzeugt. Dieses Skript misst genau
das ueber Ollama und kann das Ergebnis als `einstufung` in hosts.json schreiben
-- dieselbe Zahl, die der dispatcher zur Knotenwahl nutzt (siehe dispatcher.py).

Gemessen wird nicht die Wanduhr, sondern Ollamas eigene Felder `eval_count` und
`eval_duration` aus /api/generate: nur die REINE Erzeugungszeit, ohne die
Modell-Ladezeit. Ein erster Aufruf waermt das Modell (Laden zaehlt nicht mit),
danach mehrere Messlaeufe, deren Median die Ausreisser daempft.

Wichtig -- wie bei jeder Einstufung: ueber ALLE Knoten DASSELBE Modell messen,
sonst vergleicht die Sortierung im dispatcher Aepfel mit Birnen (3DMark-Score auf
dem einen, Tokens/s auf dem anderen ergaebe eine Reihenfolge, die nichts misst).

    venv/bin/python durchsatz.py                        # lokal messen, nur anzeigen
    venv/bin/python durchsatz.py --setzen rechner-eins  # messen und in hosts.json
    venv/bin/python durchsatz.py --url http://10.0.0.3:11434 --modell qwen3:8b
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import requests

import einstufung

FRAMEWORK = Path(__file__).resolve().parent
CONFIG = FRAMEWORK / "firma.config.json"

# Fester Arbeitsumfang je Messlauf: gleich viele Token zu erzeugen macht die
# Laeufe untereinander UND zwischen Knoten vergleichbar. Zu kurz waere verrauscht.
NUM_PREDICT = 256

# Ein Prompt, der zuverlaessig genug Text erzeugt, um NUM_PREDICT auszuschoepfen,
# und der weder Werkzeuge noch Kontext braucht -- gemessen wird die Maschine,
# nicht die Klugheit der Antwort.
PROMPT = "Zaehle langsam von 1 aufwaerts und schreibe zu jeder Zahl einen kurzen Satz."

# Grosszuegig, weil das erste Laden eines grossen Modells mit CPU-Offload Minuten
# dauern kann (siehe backends.py). Das ist KEIN Stillstand, nur langsam -- ein
# knappes Timeout wuerde ausgerechnet den langsamsten (und damit interessantesten)
# Knoten faelschlich fuer tot erklaeren.
LADE_TIMEOUT_S = 900


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _ein_lauf(url: str, modell: str, optionen: dict) -> float:
    """Ein Messlauf. Rueckgabe: Tokens/s aus Ollamas eigenen Zaehlern."""
    r = requests.post(
        f"{url.rstrip('/')}/api/generate",
        json={"model": modell, "prompt": PROMPT, "stream": False,
              "options": {"num_predict": NUM_PREDICT, **optionen}},
        timeout=LADE_TIMEOUT_S,
    )
    r.raise_for_status()
    d = r.json()
    n = int(d.get("eval_count") or 0)
    dauer_ns = int(d.get("eval_duration") or 0)
    if n <= 0 or dauer_ns <= 0:
        # Ohne diese Felder laesst sich nichts messen -- lieber laut abbrechen als
        # eine erfundene Zahl in die Einstufung schreiben.
        raise SystemExit("Ollama lieferte keine eval_count/eval_duration -- "
                         "Modell/URL pruefen.")
    return n / (dauer_ns / 1_000_000_000)


def messen(url: str, modell: str, laeufe: int, optionen: dict) -> float:
    """Warmlauf + `laeufe` Messungen, Rueckgabe: Median in Tokens/s (1 Dezimale)."""
    print(f"  Waerme {modell} auf {url} vor (Laden zaehlt nicht mit) ...")
    _ein_lauf(url, modell, optionen)
    raten = []
    for i in range(laeufe):
        rate = _ein_lauf(url, modell, optionen)
        print(f"  Lauf {i + 1}/{laeufe}: {rate:.1f} Tok/s")
        raten.append(rate)
    return round(statistics.median(raten), 1)


def main() -> None:
    cfg = _cfg()
    p = argparse.ArgumentParser(
        description="LLM-Durchsatz eines Knotens messen (Tokens/s).")
    p.add_argument("--url", default=cfg.get("ollama_url", "http://localhost:11434"),
                   help="Ollama-URL des zu messenden Knotens.")
    p.add_argument("--modell", default=cfg.get("default_model", "qwen3:8b"),
                   help="Modell -- ueber alle Knoten DASSELBE waehlen.")
    p.add_argument("--laeufe", type=int, default=3, help="Anzahl Messlaeufe (Median).")
    p.add_argument("--num-ctx", type=int, default=None,
                   help="Optionaler Kontext; ohne Angabe der Ollama-Standard.")
    p.add_argument("--num-gpu", type=int, default=None,
                   help="Optionale GPU-Layer; ohne Angabe Ollamas Auto-Aufteilung.")
    p.add_argument("--setzen", metavar="KNOTEN", default=None,
                   help="Ergebnis als einstufung dieses Knotens in hosts.json schreiben.")
    a = p.parse_args()

    optionen = {}
    if a.num_ctx is not None:
        optionen["num_ctx"] = a.num_ctx
    if a.num_gpu is not None:
        optionen["num_gpu"] = a.num_gpu

    rate = messen(a.url, a.modell, a.laeufe, optionen)
    print(f"\n  Durchsatz (Median): {rate:.1f} Tok/s")
    if a.setzen:
        # Ueber denselben Schreiber wie die 3DMark-Zahl -- eine Wahrheit, ein Weg
        # in hosts.json, dieselbe Pruefung auf unbekannte Knotennamen.
        einstufung.setzen(a.setzen, rate)


if __name__ == "__main__":
    main()
