#!/usr/bin/env python3
"""standverteiler.py — holt auf diesem Knoten den aktuellen Git-Stand.

Schritt 5 des Plans wandernde-werkbank: "Git-Verteilung per Matrix-Trigger --
nutzt den Kanal, der ohnehin steht: kein neuer Port, trotzdem sofortige
Reaktion." Und aus Runde 03: "Auslösen dürfen Chef und der Dispatcher --
letzterer ereignisgesteuert, wenn es ein Update gibt. Kein Timer."

Laeuft als kleiner Dienst auf jedem Knoten und haengt blockierend in
`LISTEN firma_stand`. Kein Polling, kein Timer: Die Verbindung schlaeft, bis
in firma.stand_auftrag eine Zeile landet -- dann feuert der Trigger, und die
Verbindung wacht auf. Genau ein Ereignis, genau eine Reaktion.

WARUM DER KNOTEN ZIEHT UND NICHT GESCHOBEN WIRD
Der Haupt-PC ist SSH-Client, kein Server; ein Push von aussen scheitert schon
an der Erreichbarkeit (dieselbe Erkenntnis, die im Dispatcher zu `pruef_port`
gefuehrt hat). Ein ziehender Knoten braucht dagegen nichts Offenes -- nur die
Postgres-Verbindung, die er ohnehin haelt.

VORSICHT VOR DEM EIGENEN ARBEITSBAUM
Auf dem Haupt-PC arbeitet Chef. Ein `git pull`, das ihm etwas ueberschreibt,
waere ein teurer Dienst am falschen Ort. Deshalb gilt hier ausnahmslos:
  * nur `--ff-only` -- niemals ein Merge, niemals ein Rebase
  * nur bei sauberem Arbeitsbaum; sonst wird gemeldet, nicht angefasst
  * niemals `reset`, `checkout -f` oder `clean`
Ein Knoten, der laut zurueckbleibt, ist ungefaehrlich. Einer, der still etwas
ueberschreibt, nicht.

Aufruf:  venv/bin/python standverteiler.py [--einmal]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import speicher

FRAMEWORK = Path(__file__).resolve().parent
REPO = FRAMEWORK.parents[2]              # ~/Horus-OS
KNOTEN = os.environ.get("FIRMA_KNOTEN") or socket.gethostname()

# Wie lange darf ein Git-Aufruf brauchen? Netzwerk kann haengen; hier ist eine
# Uhr richtig, denn git denkt nicht nach -- anders als ein Modell (siehe
# denkzeit.py, wo genau deshalb keine Stoppuhr steht).
GIT_FRIST_S = 120


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _git(*args: str, cwd: Path = REPO) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=GIT_FRIST_S)
    return p.returncode, (p.stdout + p.stderr).strip()


def _kopf() -> tuple[str, str]:
    _, c = _git("rev-parse", "HEAD")
    _, z = _git("rev-parse", "--abbrev-ref", "HEAD")
    return c.strip()[:12], z.strip()


def sauber() -> tuple[bool, str]:
    """Ist der Arbeitsbaum unangetastet? Rueckgabe (ja, was sonst offen ist).

    Laufzeit-Dateien zaehlen nicht mit: vorgaenge/kennzahlen/bilanz sind seit
    Schritt 3 in der Datenbank, aber auf Knoten, die noch nicht umgestellt sind,
    liegen sie als staendig veraenderte JSON-Dateien herum. Sie wuerden jeden
    Abgleich blockieren, obwohl sie niemanden interessieren.
    """
    rc, aus = _git("status", "--porcelain")
    if rc != 0:
        return False, f"git status fehlgeschlagen: {aus}"
    stoerend = [z for z in aus.splitlines()
                if not z.endswith((".json.lock", "kernel_state.db"))
                and "/laufzeit/" not in z]
    return (not stoerend), "\n".join(stoerend[:10])


def melden(commit: str, zweig: str, text: str, erfolg: bool) -> None:
    """Ergebnis in die Datenbank. Auch der Fehlschlag -- ein Knoten, der stumm
    zurueckbleibt, ist gefaehrlicher als einer, der laut scheitert."""
    try:
        with speicher.verbindung() as c:
            c.execute(
                "INSERT INTO firma.stand (knoten, commit_id, zweig, gemeldet, "
                "  letzter_lauf, erfolg) VALUES (%s,%s,%s,now(),%s,%s) "
                "ON CONFLICT (knoten) DO UPDATE SET commit_id=EXCLUDED.commit_id, "
                "  zweig=EXCLUDED.zweig, gemeldet=now(), "
                "  letzter_lauf=EXCLUDED.letzter_lauf, erfolg=EXCLUDED.erfolg",
                (KNOTEN, commit, zweig, text[:500], erfolg))
    except Exception as e:                                  # noqa: BLE001
        log(f"Meldung nicht abgesetzt: {e}")


def abgleichen(grund: str = "") -> tuple[bool, str]:
    """Einmal den Stand holen. Rueckgabe (erfolg, Klartext)."""
    vorher, zweig = _kopf()

    ok, offen = sauber()
    if not ok:
        text = ("uebersprungen — Arbeitsbaum nicht sauber. Nichts angefasst.\n"
                + offen)
        log(f"{text.splitlines()[0]}")
        melden(vorher, zweig, text, False)
        return False, text

    rc, aus = _git("fetch", "--quiet", "origin")
    if rc != 0:
        text = f"fetch fehlgeschlagen: {aus[:300]}"
        log(text)
        melden(vorher, zweig, text, False)
        return False, text

    rc, aus = _git("merge", "--ff-only", f"origin/{zweig}")
    nachher, _ = _kopf()

    if rc != 0:
        # Kein Fast-Forward moeglich: der Knoten ist abgewichen. Das von hier aus
        # zu begradigen hiesse, fremde Arbeit wegzuwerfen -- das entscheidet Chef.
        text = (f"kein Fast-Forward auf origin/{zweig} möglich — der Knoten ist "
                f"abgewichen. Nichts angefasst.\n{aus[:300]}")
        log(text.splitlines()[0])
        melden(vorher, zweig, text, False)
        return False, text

    if vorher == nachher:
        text = f"bereits aktuell auf {nachher} ({zweig})"
        log(text)
        melden(nachher, zweig, text, True)
        return True, text

    _, dateien = _git("diff", "--name-only", f"{vorher}..{nachher}")
    n = len([z for z in dateien.splitlines() if z.strip()])
    text = f"{vorher} → {nachher} ({zweig}), {n} Datei(en)"
    if grund:
        text += f" — Anlass: {grund}"
    log(text)
    melden(nachher, zweig, text, True)
    return True, text


def horchen() -> None:
    """Blockierend auf NOTIFY warten. Kein Timer, kein Polling.

    Bei jedem Verbindungsabriss wird neu verbunden und EINMAL abgeglichen: Was
    waehrend der Unterbrechung angefordert wurde, ist als Ereignis verloren --
    der Abgleich danach holt es nach. Ein verpasster Weckruf darf keinen Knoten
    dauerhaft zuruecklassen.
    """
    log(f"Standverteiler auf '{KNOTEN}' — Repo {REPO}")
    while True:
        try:
            c = speicher.verbindung()
            c.execute("LISTEN firma_stand")
            abgleichen("Start bzw. Wiederverbindung")
            log("horcht auf firma_stand")
            for n in c.notifies():
                try:
                    nutz = json.loads(n.payload)
                except Exception:                           # noqa: BLE001
                    nutz = {}
                ziel = nutz.get("ziel", "alle")
                if ziel not in ("alle", KNOTEN):
                    continue
                log(f"Weckruf #{nutz.get('id', '?')} von {nutz.get('von', '?')}")
                abgleichen(f"angefordert von {nutz.get('von', '?')}")
        except KeyboardInterrupt:
            return
        except Exception as e:                              # noqa: BLE001
            log(f"Verbindung verloren ({e!r}) — neuer Versuch in 30 s")
            time.sleep(30)


def anfordern(ziel: str = "alle", von: str = "chef", grund: str = "") -> int:
    """Auf der Auslöserseite: Zeile einstellen, Trigger weckt die Knoten."""
    with speicher.verbindung() as c:
        return c.execute(
            "INSERT INTO firma.stand_auftrag (ziel, ausgeloest_von, grund) "
            "VALUES (%s,%s,%s) RETURNING id", (ziel, von, grund or None)).fetchone()[0]


def uebersicht() -> list[dict]:
    with speicher.verbindung() as c:
        z = c.execute("SELECT knoten, commit_id, zweig, gemeldet, erfolg, "
                      "letzter_lauf, aktuell FROM firma.stand_uebersicht").fetchall()
    return [{"knoten": r[0], "commit": r[1], "zweig": r[2], "gemeldet": str(r[3]),
             "erfolg": r[4], "text": r[5], "aktuell": r[6]} for r in z]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--einmal", action="store_true", help="einmal abgleichen, dann Schluss")
    ap.add_argument("--anfordern", metavar="ZIEL", nargs="?", const="alle",
                    help="Abgleich anfordern (Knotenname oder 'alle')")
    ap.add_argument("--stand", action="store_true", help="Übersicht aller Knoten")
    a = ap.parse_args()
    if a.stand:
        for k in uebersicht():
            zeichen = "✓" if k["aktuell"] and k["erfolg"] else "•"
            print(f"  {zeichen} {k['knoten']:10} {k['commit'] or '?':14} {k['text'] or ''}")
    elif a.anfordern:
        print(f"  Abgleich #{anfordern(a.anfordern)} angefordert für '{a.anfordern}'")
    elif a.einmal:
        print(" ", abgleichen("manuell")[1])
    else:
        horchen()
