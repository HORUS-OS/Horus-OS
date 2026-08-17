#!/usr/bin/env python3
"""k_clean_code.py — Techniks Urteil gegen die geschriebene Hausordnung.

F5 = A: KEIN Linter. Chef hat gegen meine Empfehlung entschieden, und die
Konsequenzen wurden dabei festgehalten: Das Urteil schwankt von Lauf zu Lauf,
es kostet jedes Mal Modellzeit, und es gibt keinen roten Balken, an dem sich
ein Verstoss festmachen liesse. Genau deshalb warnt diese Kategorie nur.

F14 = B, F14.B.4 = b: Der Massstab ist HAUSORDNUNG.md, aufgebaut auf PEP 8 und
dem Google-Styleguide. Ohne einen geschriebenen Massstab waere das Urteil nicht
anfechtbar -- Technik muss die Nummer nennen, gegen die etwas verstoesst. Ein
Befund ohne Fundstelle ist Geschmack, kein Befund.

F15 = b: Sie bekommt Diff UND vollstaendige Datei. Der Diff allein zeigt nicht,
ob eine geloeschte Zeile woanders noch gebraucht wird -- genau so verschwanden
am 14.08. fuenf Funktionen bei einem Bereichs-Edit, ohne dass der Code aufhoerte
zu laufen.

F15.b.4 = a: Datei fuer Datei, am Ende eine Zusammenfassung.

WARUM NICHT ALLES GEPRUEFT WIRD
Das Framework hat ueber 8000 Zeilen. Sie bei jedem Lauf vorzulegen kostet
Stunden Modellzeit und liefert beim zwanzigsten Mal dieselbe Antwort. Geprueft
wird deshalb, was sich GEAENDERT hat -- dort entstehen neue Verstoesse.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

REPO = FRAMEWORK.parents[2]
CFG = json.loads((FRAMEWORK / "firma.config.json").read_text(encoding="utf-8"))
ORDNUNG = FRAMEWORK.parent / "HAUSORDNUNG.md"

# Wie viele Dateien je Lauf. Mehr kostet Modellzeit, ohne mehr zu finden: Die
# groessten Aenderungen tragen die meisten neuen Verstoesse.
MAX_DATEIEN = 3
# Ein Diff, der laenger ist als das, ist eine Umstrukturierung -- die beurteilt
# man nicht Zeile fuer Zeile.
MAX_DIFF_ZEICHEN = 12000
BROKER_FRIST_S = 3600


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True).stdout


def geaenderte_dateien(gegen: str = "HEAD~1") -> list[Path]:
    """Python-Dateien, die sich gegenueber `gegen` geaendert haben, groesste
    Aenderung zuerst."""
    zeilen = [z.split("\t") for z in
              _git("diff", "--numstat", gegen, "HEAD").splitlines() if z.strip()]
    treffer = []
    for teile in zeilen:
        if len(teile) < 3 or not teile[2].endswith(".py"):
            continue
        try:
            umfang = int(teile[0]) + int(teile[1])
        except ValueError:
            continue                      # Binaerdatei
        p = REPO / teile[2]
        if p.is_file():
            treffer.append((umfang, p))
    treffer.sort(reverse=True, key=lambda t: t[0])
    return [p for _u, p in treffer[:MAX_DATEIEN]]


def _frage(datei: Path, diff: str, inhalt: str) -> str:
    return (
        f"Pruefe die Datei `{datei.relative_to(REPO)}` gegen die Hausordnung.\n\n"
        f"=== HAUSORDNUNG ===\n{ORDNUNG.read_text(encoding='utf-8')}\n\n"
        f"=== GEAENDERT (Diff) ===\n{diff[:MAX_DIFF_ZEICHEN]}\n\n"
        f"=== VOLLSTAENDIGE DATEI ===\n{inhalt[:40000]}\n\n"
        f"Nenne jeden Verstoss in einer Zeile, im Format:\n"
        f"  PUNKT <Nummer>: <Fundstelle> — <was falsch ist>\n"
        f"Findest du keinen, antworte mit genau einer Zeile: KEIN BEFUND\n"
        f"Erfinde nichts. Diese Pruefung haelt keinen Rollout auf, es gibt also "
        f"keinen Grund, streng zu wirken.")


def _urteilen(frage: str) -> tuple[str, str]:
    """Technik fragen -- ueber den Broker, nicht direkt an Ollama.

    Der Broker ist das einzige GPU-Tor: Er raeumt den Speicher frei, waehlt den
    Rechenplatz und verhindert, dass sie zweimal gleichzeitig denkt.
    """
    import urllib.request
    ziel = f"{CFG.get('buchhalter_url', 'http://127.0.0.1:8900').rstrip('/')}/think"
    d = json.dumps({
        "agent_id": "technik",
        "system": ("Du bist Technik und pruefst Code gegen eine geschriebene "
                   "Hausordnung. Antworte knapp und nur im verlangten Format."),
        "messages": [{"role": "user", "content": frage}],
        # Ein Urteil ueber Code braucht das starke Modell: Die Aufgabe verlangt,
        # eine ganze Datei im Kontext zu halten.
        "anforderung": {"braucht_gpu": True, "vram_gb": 10},
    }).encode()
    r = urllib.request.Request(ziel, d, {"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=BROKER_FRIST_S) as a:
        res = json.load(a)
    return (res.get("reply") or res.get("error") or ""), (res.get("backend") or "?")


def _befunde(antwort: str) -> list[str]:
    return [z.strip() for z in antwort.splitlines()
            if z.strip().upper().startswith("PUNKT")]


def urteil_ueber_aenderungen() -> tuple[bool, str, dict]:
    """Datei fuer Datei pruefen, am Ende zusammenfassen (F15.b.4 = a)."""
    if not ORDNUNG.is_file():
        return False, f"Hausordnung fehlt: {ORDNUNG}", {}
    dateien = geaenderte_dateien()
    if not dateien:
        # Nichts geaendert ist kein Fehlschlag -- es ist nichts zu urteilen.
        return True, "keine geaenderten Python-Dateien", {"dateien": 0}

    alle: list[str] = []
    herkunft = "?"
    for d in dateien:
        diff = _git("diff", "HEAD~1", "HEAD", "--", str(d.relative_to(REPO)))
        antwort, herkunft = _urteilen(_frage(d, diff, d.read_text(encoding="utf-8")))
        for b in _befunde(antwort):
            alle.append(f"{d.name}: {b}")

    mess = {"dateien": len(dateien), "befunde": len(alle), "herkunft": herkunft}
    if not alle:
        return True, f"{len(dateien)} Datei(en) geprueft, kein Befund", mess
    return False, f"{len(alle)} Befund(e): " + " | ".join(alle[:6]), mess


TESTS = [
    {"name": "urteil_ueber_aenderungen", "lauf": urteil_ueber_aenderungen},
]
