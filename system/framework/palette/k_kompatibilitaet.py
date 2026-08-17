#!/usr/bin/env python3
"""k_kompatibilitaet.py — laeuft das Ganze ueberall, wo es laufen soll?

F17 = B: der venv-Aufbau je Architektur. des Chefs Anspruch in der Anmerkung:
"Laeuft auf all meiner verwendeten Hardware."

WARUM DIESE KATEGORIE LANGE EINE FORMALIE WAR
Solange alles auf x86-64 lief, konnte sie nichts finden. Erst mit einem zweiten
Befehlssatz wird sie ernst: Fuer ARM liefert psycopg[binary] nicht immer ein
fertiges Rad, dann wird aus dem Quelltext uebersetzt, dann fehlen Header. Beim
Pi Zero war die Annahme "traegt einen Agenten" schlicht falsch und wurde durch
Messung widerlegt (agenten_tauglich: false).

DER DRITTE TEST IST DER EIGENTLICHE
Er beantwortet die Frage, die sich sonst niemand stellt: Auf welchen
Architekturen wurde ueberhaupt je gemessen? Moeglich ist das nur, weil jeder
Messwert seinen Knoten mitfuehrt (F22.B.4 = b). Ohne diese Spalte waere die
Antwort eine Vermutung -- mit ihr ist sie eine Abfrage.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

import dispatcher                                              # noqa: E402
import speicher                                                # noqa: E402

# Module, die absichtlich nicht fuer sich allein importierbar sind oder beim
# Import Arbeit anstossen wuerden.
AUSGENOMMEN = {"setup_forum", "migrate_json_nach_pg"}


def module_importierbar() -> tuple[bool, str, dict]:
    """Jedes Framework-Modul laesst sich laden.

    Faengt genau die Klasse Fehler, die sonst erst beim Rollout auffaellt: ein
    fehlender Import, ein Syntaxfehler in einem selten benutzten Zweig, eine
    Abhaengigkeit, die auf diesem Knoten fehlt.
    """
    kaputt = []
    for p in sorted(FRAMEWORK.glob("*.py")):
        name = p.stem
        if name in AUSGENOMMEN or name.startswith("_"):
            continue
        try:
            importlib.import_module(name)
        except Exception as e:                                 # noqa: BLE001
            kaputt.append(f"{name}: {type(e).__name__}")
    return (not kaputt,
            "alle Framework-Module importierbar" if not kaputt
            else f"{len(kaputt)}: " + "; ".join(kaputt[:4]),
            {"kaputt": len(kaputt)})


def abhaengigkeiten_stimmig() -> tuple[bool, str, dict]:
    """pip check — passen die installierten Pakete zueinander?"""
    e = subprocess.run([sys.executable, "-m", "pip", "check"],
                       capture_output=True, text=True, timeout=120)
    ok = e.returncode == 0
    return (ok, "Abhaengigkeiten stimmig" if ok else e.stdout.strip()[:200],
            {"rc": e.returncode})


def architekturen_abgedeckt() -> tuple[bool, str, dict]:
    """Auf welchen Architekturen wurde je gemessen -- und auf welchen nicht?

    Meldet eine Luecke als Fehlschlag. Nicht weil etwas kaputt waere, sondern
    weil ueber diese Architektur nichts bekannt ist. Ein ungeprueftes ARM ist
    kein bestandenes ARM.
    """
    inv = dispatcher.inventar()
    soll = {h.get("arch") for h in inv.values() if h.get("arch")}
    knoten_arch = {n: h.get("arch") for n, h in inv.items()}
    with speicher.verbindung() as c:
        gemessen_auf = {z[0] for z in c.execute(
            "SELECT DISTINCT knoten FROM firma.palette_ergebnis").fetchall()}
    ist = {knoten_arch.get(k) for k in gemessen_auf if knoten_arch.get(k)}
    fehlt = sorted(a for a in soll if a not in ist)
    return (not fehlt,
            f"gemessen auf {sorted(ist)}" if not fehlt
            else f"nie gemessen auf: {fehlt} (bekannt: {sorted(ist)})",
            {"abgedeckt": sorted(ist), "offen": fehlt})


TESTS = [
    {"name": "module_importierbar", "lauf": module_importierbar},
    {"name": "abhaengigkeiten_stimmig", "lauf": abhaengigkeiten_stimmig},
    {"name": "architekturen_abgedeckt", "lauf": architekturen_abgedeckt},
]
