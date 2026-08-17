#!/usr/bin/env python3
"""sandbox.py — die Beta-Sandbox vor jedem Rollout.

Letzter offener Punkt aus Schritt 6 des Plans wandernde-werkbank. Chef in
Runde 04: "Es wird in einer Sandbox Beta getestet." Der Plan teilt das auf:

  Stufe 1 — VPS:      Installation, Dienste, Konfiguration
  Stufe 2 — Haupt-PC: alles Modellabhaengige, wo die GPU sitzt

Die Teilung ist keine Foermlichkeit. Auf dem VPS laesst sich pruefen, ob ein
Stand ueberhaupt installierbar ist, ohne dass eine GPU im Spiel waere; am PC
laesst sich pruefen, ob danach noch gedacht werden kann. Ein Fehler der ersten
Art faellt auf jedem Rechner auf, einer der zweiten nur dort, wo das Modell
laeuft -- und genau der wuerde beim Rollout die ganze Firma stillegen.

WAS DIE SANDBOX NICHT ANFASST
  * keine Produktions-Units (systemctl bleibt aussen vor)
  * keine Produktionsdaten -- geschrieben wird allein in firma.beta_tests
  * kein Matrix, keine Raeume, keine Nachrichten
  * der Klon liegt in einem eigenen Verzeichnis und wird danach entfernt
Eine Sandbox, die im Fehlerfall die Produktion beschaedigt, macht das Problem
groesser statt kleiner.

DAS ERGEBNIS HAENGT AM COMMIT, NICHT AN DER VERSION
Eine Versionsnummer vergibt jemand, ein Commit ist der Stand selbst. Haengte
der Nachweis am Etikett, koennte zwischen Test und Rollout etwas dazukommen --
getestet waere A, ausgerollt B. rollout.py fragt deshalb nach dem Test fuer
genau den Stand, den es ausrollen will.

Aufruf:
    sandbox.py --stufe vps       nur die erste Stufe
    sandbox.py --stufe pc        nur die zweite
    sandbox.py --beta 4.0.1      beide, mit Eintrag in die Freigabe
    sandbox.py --freigabe        zeigt, welche Staende freigegeben sind
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import speicher

FRAMEWORK = Path(__file__).resolve().parent
REPO = FRAMEWORK.parents[2]
VPS = "mein-server"
# Auf dem VPS: eigenes Verzeichnis, nichts Bestehendes wird beruehrt.
VPS_SANDBOX = "/opt/firma-sandbox-beta"

# Ein einzelner Pruefschritt darf haengen -- dann ist er fehlgeschlagen. Hier
# ist eine Uhr richtig: Diese Schritte denken nicht nach (anders als ein Modell,
# siehe denkzeit.py), sie laufen oder sie haengen.
SCHRITT_FRIST_S = 300
# Ausnahme: der Denkvorgang in Stufe 2. Er DARF lange dauern -- 200 s sind auf
# dieser Maschine normal. Zu kurz angesetzt wuerde die Sandbox genau das als
# Fehler melden, was denkzeit.py als richtig erkannt hat.
DENK_FRIST_S = 900


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


class Protokoll:
    """Sammelt jeden Schritt mit Ergebnis. Auch die gelungenen -- bei einem
    Fehlschlag will man wissen, wie weit es kam."""

    def __init__(self, stufe: str):
        self.stufe = stufe
        self.schritte: list[dict] = []
        self.beginn = time.monotonic()

    def schritt(self, name: str, ok: bool, ausgabe: str = "") -> bool:
        self.schritte.append({"schritt": name, "ok": ok,
                              "ausgabe": ausgabe[:800]})
        log(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f" — {ausgabe[:200]}"))
        return ok

    @property
    def bestanden(self) -> bool:
        return bool(self.schritte) and all(s["ok"] for s in self.schritte)

    @property
    def dauer(self) -> float:
        return round(time.monotonic() - self.beginn, 1)


def _lauf(befehl: list[str], *, cwd: Path | None = None,
          frist: int = SCHRITT_FRIST_S) -> tuple[bool, str]:
    try:
        p = subprocess.run(befehl, cwd=cwd, capture_output=True, text=True,
                           timeout=frist)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"hängt seit {frist} s — abgebrochen"
    except Exception as e:                                  # noqa: BLE001
        return False, repr(e)


def _kopf() -> str:
    p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    return p.stdout.strip()[:12]


# --- Stufe 1: VPS ---------------------------------------------------------
def stufe_vps(zweig: str = "") -> Protokoll:
    """Installierbarkeit auf einem Rechner ohne GPU.

    Geprueft wird gegen den EINGECHECKTEN Stand (git archive), nicht gegen das
    Arbeitsverzeichnis. Nur so faellt auf, wenn eine Datei bloss lokal existiert
    und nie eingecheckt wurde -- genau dieser Fall trat beim Bau des install.sh
    auf: drei systemd-Units liefen seit Monaten und fehlten im Repo.
    """
    p = Protokoll("vps")
    if not zweig:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=REPO, capture_output=True, text=True, timeout=30)
        zweig = r.stdout.strip()

    erreichbar, aus = _lauf(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                             VPS, "echo da"], frist=40)
    if not p.schritt("VPS erreichbar", erreichbar, aus):
        return p

    # Der Stand kommt per `git archive` -- NICHT per Klon von GitHub.
    #
    # Ein Klon setzte einen Deploy-Key auf dem VPS voraus (der erste Versuch
    # scheiterte genau daran: "Host key verification failed"). `git archive HEAD`
    # traegt exakt die EINGECHECKTEN Dateien und nichts sonst -- es prueft also
    # dieselbe Sache wie ein frischer Klon: Was nur lokal existiert und nie
    # eingecheckt wurde, fehlt im Archiv und faellt hier auf.
    #
    # Nebenbei wird damit der Stand getestet, der wirklich vorliegt, statt des
    # Standes auf GitHub. Fuer eine Beta VOR dem Rollout ist das genauer.
    ok, aus = _lauf(["bash", "-c",
                     f"git -C {REPO} archive --format=tar HEAD | "
                     f"ssh -o BatchMode=yes {VPS} "
                     f"'rm -rf {VPS_SANDBOX} && mkdir -p {VPS_SANDBOX} && "
                     f" tar -x -C {VPS_SANDBOX} && echo uebertragen'"], frist=300)
    if not p.schritt("eingecheckter Stand auf den VPS übertragen", ok, aus):
        return p

    ok, aus = _lauf(["ssh", "-o", "BatchMode=yes", VPS,
                     f"test -f {VPS_SANDBOX}/system/mitarbeiter/framework/"
                     f"deploy/install.sh && echo da"])
    if not p.schritt("Sandbox vollständig entpackt", ok, aus):
        return p
    p.schritt(f"getestet wird {_kopf()}", True)

    ok, aus = _lauf(["ssh", "-o", "BatchMode=yes", VPS,
                     f"bash -n {VPS_SANDBOX}/system/mitarbeiter/framework/deploy/install.sh"])
    p.schritt("install.sh syntaktisch in Ordnung", ok, aus)

    ok, aus = _lauf(["ssh", "-o", "BatchMode=yes", VPS,
                     f"cd {VPS_SANDBOX} && python3 -m compileall -q "
                     f"system/mitarbeiter/framework/*.py"])
    p.schritt("Framework-Module übersetzen", ok, aus)

    # Sind alle Units da, auf die install.sh verweist? Der Fall "Unit fehlt im
    # Repo" faellt sonst erst auf dem neuen Rechner auf.
    ok, aus = _lauf([
        "ssh", "-o", "BatchMode=yes", VPS,
        f"cd {VPS_SANDBOX}/system/mitarbeiter/framework/deploy && "
        f"for r in rechenknoten agenten leichtgewicht; do "
        f"  while read -r d; do "
        f"    find {VPS_SANDBOX}/system/mitarbeiter -name \"$d.service\" "
        f"      | grep -q . || echo \"FEHLT: $d ($r)\"; done "
        f"  < <(bash -c 'source <(sed -n \"/^dienste_fuer_rolle/,/^}}/p\" install.sh); "
        f"       dienste_fuer_rolle '$r); done"])
    p.schritt("alle Units aus install.sh vorhanden", ok and "FEHLT" not in aus, aus)

    ok, aus = _lauf(["ssh", "-o", "BatchMode=yes", VPS,
                     f"cd {VPS_SANDBOX} && python3 -c \"import json,glob,sys; "
                     f"[json.load(open(f)) for f in "
                     f"glob.glob('system/mitarbeiter/*/personalakte.json')"
                     f" + glob.glob('system/mitarbeiter/framework/*.json')]; "
                     f"print('ok')\""])
    p.schritt("Konfiguration und Personalakten lesbar", ok, aus)

    # SQL-Dateien: Sie muessen idempotent sein, weil install.sh sie auf jedem
    # Knoten anwendet. Ein CREATE ohne IF NOT EXISTS faellt beim zweiten Lauf um.
    ok, aus = _lauf(["ssh", "-o", "BatchMode=yes", VPS,
                     f"cd {VPS_SANDBOX}/system/mitarbeiter/framework/sql && "
                     f"grep -LiE 'if not exists|or replace|drop .* if exists' *.sql || true"])
    p.schritt("SQL-Dateien idempotent", not aus.strip(),
              f"ohne IF NOT EXISTS / OR REPLACE: {aus.strip()}")

    _lauf(["ssh", "-o", "BatchMode=yes", VPS, f"rm -rf {VPS_SANDBOX}"], frist=60)
    p.schritt("Sandbox auf dem VPS aufgeräumt", True)
    return p


# --- Stufe 2: Haupt-PC ----------------------------------------------------
def stufe_pc() -> Protokoll:
    """Alles Modellabhaengige. Laeuft dort, wo die GPU sitzt.

    Der Kern ist ein echter Denkvorgang -- kein Attrappen-Aufruf. Ob ein Stand
    noch denken kann, zeigt sich nur, wenn wirklich gedacht wird.
    """
    p = Protokoll("pc")

    ok, aus = _lauf([str(FRAMEWORK / "venv/bin/python"), "-c",
                     "import sys; sys.path.insert(0,'.'); "
                     "import speicher, auftragsqueue, dispatcher, standverteiler, "
                     "einmalig, denkzeit, werkzeuge, rollout; print('ok')"],
                    cwd=FRAMEWORK)
    if not p.schritt("alle Kernmodule ladbar", ok, aus):
        return p

    ok, aus = _lauf(["curl", "-sS", "--max-time", "20",
                     "http://127.0.0.1:11434/api/tags"], frist=30)
    p.schritt("Ollama antwortet", ok and "models" in aus, aus[:200])

    try:
        modelle = [m["name"] for m in json.loads(aus).get("models", [])]
    except Exception:                                       # noqa: BLE001
        modelle = []
    cfg = json.loads((FRAMEWORK / "firma.config.json").read_text(encoding="utf-8"))
    gewuenscht = cfg.get("default_model", "")
    p.schritt(f"Modell '{gewuenscht}' vorhanden",
              any(gewuenscht in m for m in modelle),
              f"vorhanden: {', '.join(modelle[:6])}")

    ok, aus = _lauf(["curl", "-sS", "--max-time", "20",
                     "http://127.0.0.1:8900/status"], frist=30)
    p.schritt("Broker antwortet", ok, aus[:200])

    # Der eigentliche Test: wirklich denken lassen. Grosszuegige Frist, weil
    # lange Denkzeit hier ausdruecklich erlaubt ist.
    frage = {"agent_id": "_sandbox", "model": gewuenscht,
             "messages": [{"role": "user",
                           "content": "Antworte mit genau einem Wort: Sandbox"}]}
    ok, aus = _lauf(["curl", "-sS", "--max-time", str(DENK_FRIST_S),
                     "-X", "POST", "http://127.0.0.1:8900/think",
                     "-H", "Content-Type: application/json",
                     "-d", json.dumps(frage)], frist=DENK_FRIST_S + 30)
    antwort = ""
    try:
        antwort = (json.loads(aus).get("reply") or "")[:200]
    except Exception:                                       # noqa: BLE001
        pass
    p.schritt("echter Denkvorgang läuft durch", ok and bool(antwort.strip()),
              antwort or aus[:200])

    ok, aus = _lauf([str(FRAMEWORK / "venv/bin/python"), "dispatcher.py"],
                    cwd=FRAMEWORK)
    p.schritt("Dispatcher findet einen Host je Agent",
              ok and "kein geeigneter Host" not in aus, aus[-400:])

    ok, aus = _lauf([str(FRAMEWORK / "venv/bin/python"), "standverteiler.py",
                     "--stand"], cwd=FRAMEWORK)
    p.schritt("Knotenübersicht abrufbar", ok, aus[:300])
    return p


# --- Ablage und Freigabe --------------------------------------------------
def eintragen(p: Protokoll, version: str = "") -> None:
    import socket
    import os
    with speicher.verbindung() as c:
        c.execute(
            "INSERT INTO firma.beta_tests (commit_id, version, stufe, bestanden, "
            "  protokoll, dauer_s, knoten) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (_kopf(), version or None, p.stufe, p.bestanden,
             json.dumps(p.schritte, ensure_ascii=False), p.dauer,
             os.environ.get("FIRMA_KNOTEN") or socket.gethostname()))


def freigabe(commit: str = "") -> dict | None:
    """Ist dieser Stand durch beide Stufen? None, wenn nicht."""
    commit = commit or _kopf()
    with speicher.verbindung() as c:
        z = c.execute(
            "SELECT commit_id, version, beide_bestanden, stufen, zuletzt "
            "FROM firma.beta_freigabe WHERE commit_id = %s", (commit,)).fetchone()
    if not z or not z[2]:
        return None
    return {"commit": z[0], "version": z[1], "stufen": z[3], "zuletzt": str(z[4])}


def beta(version: str = "") -> dict:
    """Beide Stufen nacheinander. Die zweite laeuft auch, wenn die erste
    scheitert -- ein vollstaendiges Bild ist mehr wert als ein schneller
    Abbruch, und niemand wartet hier auf Sekunden."""
    log(f"Beta-Test für {version or 'den aktuellen Stand'} ({_kopf()})")
    log("Stufe 1 — VPS: Installation, Dienste, Konfiguration")
    v = stufe_vps()
    eintragen(v, version)
    log("Stufe 2 — Haupt-PC: alles Modellabhängige")
    c = stufe_pc()
    eintragen(c, version)

    bestanden = v.bestanden and c.bestanden
    log(f"Ergebnis: {'BESTANDEN' if bestanden else 'DURCHGEFALLEN'} "
        f"(VPS {'ok' if v.bestanden else 'nein'}, PC {'ok' if c.bestanden else 'nein'})")
    return {"commit": _kopf(), "version": version, "bestanden": bestanden,
            "vps": v.schritte, "pc": c.schritte,
            "dauer_s": round(v.dauer + c.dauer, 1)}


def _hauptprogramm() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stufe", choices=("vps", "pc"))
    ap.add_argument("--beta", metavar="VERSION", nargs="?", const="")
    ap.add_argument("--freigabe", action="store_true")
    a = ap.parse_args()

    if a.freigabe:
        f = freigabe()
        print(f"  {_kopf()}: {'freigegeben — ' + str(f) if f else 'NICHT freigegeben'}")
        return 0 if f else 1
    if a.stufe == "vps":
        p = stufe_vps(); eintragen(p)
    elif a.stufe == "pc":
        p = stufe_pc(); eintragen(p)
    elif a.beta is not None:
        return 0 if beta(a.beta)["bestanden"] else 1
    else:
        ap.print_help(); return 0
    return 0 if p.bestanden else 1


if __name__ == "__main__":
    raise SystemExit(_hauptprogramm())
