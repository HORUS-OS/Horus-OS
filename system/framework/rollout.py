#!/usr/bin/env python3
"""rollout.py — Techniks Werkzeug zum Ausrollen von Updates.

Schritt 6 des Plans wandernde-werkbank. Chef am 2026-08-13: "Technik bekommt das
Recht für Update und Patchen des Systems. Diese Werkzeuge müssen noch gebaut
werden." Dies ist das Werkzeug, das den Rollout selbst macht; der Transport zu
den Knoten liegt in standverteiler.py.

DIE HARTE REGEL AUS DEM PLAN
    "Der Job darf selbst committen und ausrollen -- Voraussetzung ist ein
     hinterlegter Rollback. Kein Eintrag im Upgrade-Register, kein Rollout."

Das ist hier kein Hinweis im Kommentar, sondern eine Sperre: `ausrollen()`
bricht ab, wenn im Register kein Eintrag mit ausfuehrbarem Rollback steht.
Ein Agent, der ausrollen darf, darf nicht auch entscheiden duerfen, ob er
dokumentiert -- sonst ist die Pflicht keine.

ZWEI RUECKWEGE, wie in Runde 04 festgelegt:
  1. Git-Tag auf den Vorher-Stand, benannt und im Register vermerkt.
     Der Tag wird VOR dem Rollout gesetzt. Danach waere er wertlos.
  2. Git-Revert des Rollout-Commits.
Bewusst KEIN `git reset --hard`: Ein Revert ist selbst ein Commit und damit
nachvollziehbar. Ein Reset loescht Geschichte auf Rechnern, die vielleicht
schon gezogen haben -- und macht aus einem misslungenen Update ein zweites
Problem.

UEBERWACHUNG
Der Plan sieht dafuer den VPS vor: "Er laeuft ausserhalb der betroffenen
Maschine und hat mit dem Herzschlag aus Schritt 4 bereits das noetige Signal."
Genau daran haengt `beobachten()` -- es fragt nicht die Knoten, ob es ihnen
gut geht (ein abgestuerzter Knoten antwortet nicht ehrlich), sondern sieht in
firma.stand und firma.worker nach, was die Knoten von sich aus gemeldet haben.

Aufruf:
    rollout.py --pruefen 4.0.0            nur sehen, ob ausgerollt werden darf
    rollout.py --ausrollen 4.0.0          Tag, Push, Verteilung, Beobachtung
    rollout.py --zurueck "Grund"          Revert des letzten Rollouts
    rollout.py --stand                    wer steht auf welchem Commit
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
REPO = FRAMEWORK.parents[2]
REGISTER = REPO / "plans" / "upgrade-register.md"
CHANGELOG = REPO / "CHANGELOG.md"

# Wie lange bekommen die Knoten Zeit, den neuen Stand zu melden?
FRIST_S = 300
# Ab wann gilt ein Knoten als still? Grosszuegig -- ein Knoten kann gerade
# rechnen. Dieselbe Ueberlegung wie beim Herzschlag in auftragsqueue.py.
STILLE_S = 600


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                       text=True, timeout=180)
    return p.returncode, (p.stdout + p.stderr).strip()


class Abbruch(Exception):
    """Rollout nicht zulaessig. Traegt den Grund im Klartext."""


# --- Rückweg 0: darf überhaupt ausgerollt werden? -------------------------
def register_eintrag(version: str) -> dict:
    """Sucht den Registereintrag zur Version und prueft ihn auf Brauchbarkeit.

    Geprueft wird nicht nur, DASS etwas dasteht, sondern dass ein Rollback
    dabeisteht. Ein Eintrag "Rollback: siehe oben" oder eine leere Zelle ist
    im Ernstfall wertlos -- und der Ernstfall ist der einzige Fall, in dem
    jemand hier nachschlaegt.
    """
    if not REGISTER.is_file():
        raise Abbruch(f"Upgrade-Register fehlt: {REGISTER}")
    text = REGISTER.read_text(encoding="utf-8")

    # Die Tabellenzeile zur Version finden. Das Register fuehrt sie in einer
    # Markdown-Tabelle mit der Spaltenfolge # | Upgrade | Datum | Status |
    # Wirkung | Rollback.
    for zeile in text.splitlines():
        if not zeile.strip().startswith("|") or version not in zeile:
            continue
        spalten = [s.strip() for s in zeile.strip().strip("|").split("|")]
        if len(spalten) < 6:
            continue
        rollback = spalten[5]
        # Ein Rollback muss ausfuehrbar sein, nicht nur vorhanden. Verweise
        # helfen niemandem um drei Uhr nachts.
        if len(rollback) < 15 or re.fullmatch(r"[-–—\s]*", rollback) or \
                rollback.lower().startswith(("siehe", "s. o", "tbd", "offen")):
            raise Abbruch(
                f"Register kennt '{version}', aber der Rollback taugt nicht: "
                f"'{rollback}'. Er muss ohne Nachdenken ausführbar sein.")
        return {"nummer": spalten[0], "upgrade": spalten[1], "datum": spalten[2],
                "status": spalten[3], "wirkung": spalten[4], "rollback": rollback}

    raise Abbruch(
        f"Kein Eintrag für '{version}' im Upgrade-Register.\n"
        f"  Kein Eintrag im Upgrade-Register, kein Rollout — so steht es im Plan.\n"
        f"  Zuerst {REGISTER.relative_to(REPO)} ergänzen: Wirkung auf die "
        f"Mitarbeiter, Status und ein ausführbarer Rollback.")


def changelog_hat(version: str) -> bool:
    return CHANGELOG.is_file() and f"[{version}]" in CHANGELOG.read_text(encoding="utf-8")


def pruefen(version: str) -> dict:
    """Alles, was VOR einem Rollout stimmen muss. Wirft Abbruch mit Klartext."""
    ergebnis = {}
    # Den Stand zuerst feststellen: Die Beta-Pruefung weiter unten braucht ihn.
    _, _zweig = _git("rev-parse", "--abbrev-ref", "HEAD")
    _, _kopf = _git("rev-parse", "HEAD")
    ergebnis["zweig"] = _zweig.strip()
    ergebnis["commit"] = _kopf.strip()[:12]

    ergebnis["register"] = register_eintrag(version)

    rc, aus = _git("status", "--porcelain")
    stoerend = [z for z in aus.splitlines() if z.strip()]
    if rc != 0 or stoerend:
        raise Abbruch("Arbeitsbaum nicht sauber — ein Rollout aus einem "
                      "unfertigen Stand ist nicht nachvollziehbar:\n  "
                      + "\n  ".join(stoerend[:8]))

    # Dritte Sperre: die Testpalette. Sie steht VOR der Beta, weil sie billiger
    # ist und etwas anderes findet: Die Beta prueft, ob der Stand LAEUFT, die
    # Palette, ob er TAUGT. Ein Secret im Repo laesst sich installieren, ohne
    # dass irgendetwas auffiele.
    #
    # Drei Ausgaenge, und der mittlere ist der wichtige:
    #   bestanden   -> weiter
    #   gescheitert -> der Stand ist schlecht
    #   entwertet   -> ueber den Stand ist NICHTS bekannt (Testdateien geaendert)
    # Beides haelt den Rollout auf, aber nur eines ist ein Befund ueber den Code.
    import speicher
    with speicher.verbindung() as c:
        zeile = c.execute(
            "SELECT ergebnis, abgebrochen_bei FROM firma.palette_freigabe "
            "WHERE commit_id LIKE %s", (f"{ergebnis['commit']}%",)).fetchone()
    if not zeile:
        raise Abbruch(
            f"Für diesen Stand liegt kein Palettenlauf vor.\n"
            f"  Zuerst:  python -m palette\n"
            f"  Die Palette prüft Security, bekannte Fehler, Injection und mehr — "
            f"bevor überhaupt eine Sandbox startet.")
    if zeile[0] != "bestanden":
        wo = f" (abgebrochen bei: {zeile[1]})" if zeile[1] else ""
        grund = ("Die Testdateien wurden geändert — über diesen Stand ist damit "
                 "nichts bekannt." if zeile[0] == "entwertet"
                 else "Ein sperrender Test ist fehlgeschlagen.")
        raise Abbruch(f"Palette: {zeile[0].upper()}{wo}. {grund}\n"
                      f"  Erneut prüfen:  python -m palette")
    ergebnis["palette"] = zeile[0]

    # Vierte Sperre: die Sandbox-Beta. Der Nachweis haengt am COMMIT, nicht an
    # der Versionsnummer -- sonst koennte zwischen Test und Rollout noch etwas
    # dazukommen, und getestet waere A, ausgerollt B.
    import sandbox
    frei = sandbox.freigabe(ergebnis.get("commit") or None)
    if not frei:
        raise Abbruch(
            f"Für diesen Stand liegt keine bestandene Beta vor.\n"
            f"  Zuerst:  sandbox.py --beta {version}\n"
            f"  Beide Stufen müssen durch — VPS (Installation) und "
            f"Haupt-PC (Modell).")
    ergebnis["beta"] = frei

    if not changelog_hat(version):
        raise Abbruch(f"CHANGELOG.md kennt '[{version}]' nicht. Semver "
                      f"fortschreiben, bevor ausgerollt wird.")

    rc, _ = _git("rev-parse", "--verify", f"refs/tags/vorher-{version}")
    ergebnis["tag_schon_da"] = (rc == 0)
    return ergebnis


# --- Rückweg 1: Tag auf den Vorher-Stand ---------------------------------
def tag_setzen(version: str, *, trocken: bool = False) -> str:
    """Benannter Tag auf den JETZIGEN Stand -- also den Zustand VOR dem Update.

    Muss vor dem Rollout passieren. Hinterher zeigt er auf den neuen Stand und
    ist als Rueckweg wertlos.
    """
    name = f"vorher-{version}"
    rc, _ = _git("rev-parse", "--verify", f"refs/tags/{name}")
    if rc == 0:
        log(f"Tag {name} besteht bereits — nicht überschrieben")
        return name
    if trocken:
        log(f"[trocken] würde Tag {name} setzen")
        return name
    rc, aus = _git("tag", "-a", name, "-m",
                   f"Stand vor Rollout {version} — Rückweg 1 laut Plan "
                   f"wandernde-werkbank, Schritt 6")
    if rc != 0:
        raise Abbruch(f"Tag konnte nicht gesetzt werden: {aus}")
    log(f"Tag {name} gesetzt")
    return name


# --- Ausrollen ------------------------------------------------------------
def ausrollen(version: str, *, trocken: bool = False, ziel: str = "alle") -> dict:
    """Der ganze Weg: prüfen, Tag, push, verteilen, beobachten."""
    log(f"Rollout {version} — erst prüfen")
    stand = pruefen(version)
    log(f"  Register #{stand['register']['nummer']}: {stand['register']['upgrade']}")
    log(f"  Rollback hinterlegt: {stand['register']['rollback'][:70]}…")
    log(f"  Stand: {stand['commit']} auf {stand['zweig']}")

    tag = tag_setzen(version, trocken=trocken)

    for entfernt in ("origin", "privat"):
        if trocken:
            log(f"[trocken] würde nach {entfernt} pushen ({stand['zweig']}, {tag})")
            continue
        rc, aus = _git("push", entfernt, stand["zweig"])
        log(f"  push {entfernt}: {'ok' if rc == 0 else aus[:120]}")
        rc, aus = _git("push", entfernt, tag)
        log(f"  push {entfernt} {tag}: {'ok' if rc == 0 else aus[:120]}")

    if trocken:
        log("[trocken] würde Verteilung anfordern und beobachten")
        return {"version": version, "tag": tag, "trocken": True}

    import standverteiler
    aid = standverteiler.anfordern(ziel, "technik", f"Rollout {version}")
    log(f"Verteilung #{aid} angefordert für '{ziel}'")

    bericht = beobachten(stand["commit"], erwartet_ab=time.time())
    bericht.update({"version": version, "tag": tag, "auftrag": aid})
    return bericht


def beobachten(alter_commit: str, *, frist_s: int = FRIST_S,
               erwartet_ab: float | None = None) -> dict:
    """Ziehen die Knoten den neuen Stand? Läuft danach noch, was laufen soll?

    Bewusst passiv: Es wird nicht bei den Knoten angefragt, sondern gelesen,
    was sie von sich aus gemeldet haben (firma.stand, firma.worker). Ein
    Knoten, der nicht mehr kann, meldet nichts -- und genau das ist das Signal.
    """
    import standverteiler
    import auftragsqueue

    ende = time.time() + frist_s
    gemeldet: dict[str, dict] = {}
    while time.time() < ende:
        for k in standverteiler.uebersicht():
            gemeldet[k["knoten"]] = k
        # Fertig, sobald jeder bekannte Knoten erfolgreich und aktuell ist.
        offen = [n for n, k in gemeldet.items()
                 if not (k["erfolg"] and k["aktuell"])]
        if gemeldet and not offen:
            log(f"alle {len(gemeldet)} Knoten auf dem neuen Stand")
            break
        time.sleep(10)
    else:
        log(f"Frist von {frist_s} s abgelaufen — nicht alle Knoten gemeldet")

    fehl = [n for n, k in gemeldet.items() if not k["erfolg"]]
    alt = [n for n, k in gemeldet.items() if k["erfolg"] and not k["aktuell"]]

    # Zweites Signal: Arbeiten die Worker noch? Ein Rollout, nach dem kein
    # Auftrag mehr durchgeht, ist misslungen -- auch wenn git zufrieden ist.
    try:
        q = auftragsqueue.stand()
        haengend = q["queue"].get("laeuft", {}).get("ohne_lebenszeichen", 0)
    except Exception as e:                                  # noqa: BLE001
        haengend = -1
        log(f"Queue nicht lesbar: {e}")

    heil = not fehl and not alt and haengend <= 0
    bericht = {"knoten": gemeldet, "fehlgeschlagen": fehl, "zurueckgeblieben": alt,
               "haengende_auftraege": haengend, "heil": heil,
               "alter_commit": alter_commit}
    log(f"Ergebnis: {'in Ordnung' if heil else 'AUFFÄLLIG'} — "
        f"{len(gemeldet)} Knoten, {len(fehl)} fehlgeschlagen, "
        f"{len(alt)} zurückgeblieben, {haengend} hängende Aufträge")
    return bericht


# --- Rückweg 2: Revert ----------------------------------------------------
def zurueckrollen(grund: str, *, commit: str = "HEAD", trocken: bool = False) -> str:
    """Revert statt Reset. Der Rückweg bleibt selbst nachvollziehbar.

    Ein `reset --hard` würde Geschichte auf Rechnern löschen, die vielleicht
    schon gezogen haben — aus einem misslungenen Update würde ein zweites
    Problem. Ein Revert ist ein neuer Commit: Er verteilt sich auf demselben
    Weg wie das Update und hinterlässt eine Spur.
    """
    if trocken:
        log(f"[trocken] würde {commit} reverten")
        return "trocken"
    rc, aus = _git("revert", "--no-edit", commit)
    if rc != 0:
        raise Abbruch(f"Revert fehlgeschlagen: {aus[:300]}\n"
                      f"  Von Hand prüfen — NICHT mit reset nachhelfen.")
    _, neu = _git("rev-parse", "HEAD")
    log(f"Revert als {neu.strip()[:12]} — Grund: {grund}")

    _, zweig = _git("rev-parse", "--abbrev-ref", "HEAD")
    for entfernt in ("origin", "privat"):
        rc, aus = _git("push", entfernt, zweig.strip())
        log(f"  push {entfernt}: {'ok' if rc == 0 else aus[:120]}")

    import standverteiler
    standverteiler.anfordern("alle", "technik", f"Rückrollung: {grund}")
    log("Rückrollung an alle Knoten verteilt")
    return neu.strip()[:12]


def _hauptprogramm() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pruefen", metavar="VERSION")
    ap.add_argument("--ausrollen", metavar="VERSION")
    ap.add_argument("--zurueck", metavar="GRUND")
    ap.add_argument("--stand", action="store_true")
    ap.add_argument("--trocken", action="store_true")
    ap.add_argument("--ziel", default="alle")
    a = ap.parse_args()

    try:
        if a.stand:
            import standverteiler
            for k in standverteiler.uebersicht():
                z = "✓" if k["aktuell"] and k["erfolg"] else "✗"
                print(f"  {z} {k['knoten']:10} {k['commit'] or '?':14} {k['text'] or ''}")
        elif a.pruefen:
            s = pruefen(a.pruefen)
            print(f"  Rollout von {a.pruefen} wäre zulässig.")
            print(f"    Register #{s['register']['nummer']}: {s['register']['upgrade']}")
            print(f"    Rollback:  {s['register']['rollback'][:100]}")
            print(f"    Stand:     {s['commit']} auf {s['zweig']}")
        elif a.ausrollen:
            b = ausrollen(a.ausrollen, trocken=a.trocken, ziel=a.ziel)
            if not b.get("trocken") and not b.get("heil"):
                print("\n  Der Rollout ist auffällig. Rückwege:")
                print(f"    1. git checkout {b['tag']}")
                print(f"    2. {Path(__file__).name} --zurueck \"<Grund>\"")
                return 1
        elif a.zurueck:
            zurueckrollen(a.zurueck, trocken=a.trocken)
        else:
            ap.print_help()
        return 0
    except Abbruch as e:
        print(f"\n  ABGEBROCHEN: {e}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_hauptprogramm())
