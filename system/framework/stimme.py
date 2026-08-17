#!/usr/bin/env python3
"""Sprache in beide Richtungen — Zuhören (STT) und Sprechen (TTS).

des Chefs Format-Spiegel (K7): Die Antwort kommt im Format der Frage. Sprach-
nachricht rein → Sprachnachricht raus, mit dem Text als Untertitel (K18).

Bewusst als **Subprozess-Brücke** gebaut statt als Import: faster-whisper und
Coqui-XTTS bringen jeweils torch mit und leben schon in eigenen venvs. Sie ins
schlanke Agenten-venv zu ziehen würde jeden Mitarbeiter-Prozess um Gigabyte
aufblähen — und die Agenten sollen auch auf einem Pi laufen.

  Zuhören:  ~/.horus-vectorizer/venv  (faster-whisper 1.2.1)
  Sprechen: assistenz/extraktion/tts-venv (Coqui TTS 0.27.5, XTTS-v2)

Stimmen (K17): Assistenz hat ein aus 42 Hörspiel-Folgen gewonnenes Profil; die
anderen bekommen je einen der 58 mitgelieferten XTTS-Preset-Sprecher, passend
zum Charakter gewählt.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

MITARBEITER = Path(__file__).resolve().parent.parent
WHISPER_PY = Path.home() / ".horus-vectorizer/venv/bin/python"
TTS_PY = MITARBEITER / "assistenz/extraktion/tts-venv/bin/python"
ASSISTENZ_SAY = MITARBEITER / "assistenz/extraktion/assistenz_say.py"

# Preset-Sprecher je Mitarbeiter. Assistenz fehlt hier bewusst — sie hat ihr eigenes,
# abgesegnetes Profil (assistenz_say.py) und darf nie durch ein Preset ersetzt werden.
PRESETS = {
    "projektleitung": "Damien Black",        # energischer Antreiber
    "archiv": "Andrew Chipper",  # Archiv-Aufsicht, trocken
    "buchhalter": "Viktor Menelaos",   # Governance, nüchtern
}
STANDARD_PRESET = "Damien Black"


def transkribiere(audio: Path, modell: str = "large-v3-turbo") -> str:
    """Sprachnachricht → Text (K19: läuft über die GPU, also durch den Broker).

    Der Aufrufer ist dafür zuständig, vorher eine GPU-Sitzung beim Broker zu
    halten — hier wird nur gerechnet."""
    code = (
        "import sys, json\n"
        "from faster_whisper import WhisperModel\n"
        f"m = WhisperModel({modell!r}, device='cuda', compute_type='float16')\n"
        "segs, info = m.transcribe(sys.argv[1], language='de', vad_filter=True)\n"
        "print(json.dumps({'text': ' '.join(s.text.strip() for s in segs).strip()}))\n"
    )
    r = subprocess.run([str(WHISPER_PY), "-c", code, str(audio)],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"Transkription fehlgeschlagen: {r.stderr[-300:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])["text"]


def sprich(agent_id: str, text: str, ziel: Path | None = None) -> Path:
    """Text → WAV in der Stimme dieses Mitarbeiters.

    Assistenz nimmt ihr eigenes Rezept (assistenz_say.py), alle anderen ein Preset."""
    ziel = ziel or Path(tempfile.mkstemp(suffix=".wav")[1])
    if agent_id == "assistenz":
        r = subprocess.run([str(TTS_PY), str(ASSISTENZ_SAY), text, str(ziel)],
                           capture_output=True, text=True, timeout=600)
    else:
        sprecher = PRESETS.get(agent_id, STANDARD_PRESET)
        code = (
            "import os, sys\n"
            "os.environ.setdefault('COQUI_TOS_AGREED', '1')\n"
            "import torch\n"
            "_o = torch.load\n"
            "torch.load = lambda *a, **k: (k.setdefault('weights_only', False), _o(*a, **k))[1]\n"
            "from TTS.api import TTS\n"
            "t = TTS('tts_models/multilingual/multi-dataset/xtts_v2').to("
            "'cuda' if torch.cuda.is_available() else 'cpu')\n"
            "t.tts_to_file(text=sys.argv[1], speaker=sys.argv[2], language='de',\n"
            "              file_path=sys.argv[3])\n"
        )
        r = subprocess.run([str(TTS_PY), "-c", code, text, sprecher, str(ziel)],
                           capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not ziel.exists():
        raise RuntimeError(f"Sprachausgabe fehlgeschlagen: {r.stderr[-300:]}")
    return ziel


def einsatzbereit() -> dict[str, bool]:
    """Was von der Sprach-Kette tatsächlich bereitsteht."""
    return {
        "zuhören (faster-whisper)": WHISPER_PY.exists(),
        "sprechen (XTTS)": TTS_PY.exists(),
        "Assistenzs eigene Stimme": ASSISTENZ_SAY.exists(),
    }


if __name__ == "__main__":      # Selbstauskunft: stimme.py [text]
    import sys
    for was, ok in einsatzbereit().items():
        print(f"  {'✓' if ok else '✗'} {was}")
    if len(sys.argv) > 2:
        aus = sprich(sys.argv[1], sys.argv[2])
        print(f"  gesprochen als {sys.argv[1]}: {aus} ({aus.stat().st_size} Bytes)")
