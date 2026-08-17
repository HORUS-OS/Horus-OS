#!/usr/bin/env python3
"""laeufer.py — fuehrt die Testpalette aus.

Position 1 des Plans testpalette-und-depot. Hier steht der RAHMEN, kein Test:
Reihenfolge, Haerte, Knotenwahl, Pruefsummen, Abbruch, Vorgaenge.

DIE VIER REGELN, DIE DEN RAHMEN AUSMACHEN

1. Reihenfolge: billig und sperrend zuerst (F24 = A). Faellt Security in Minute
   zwei, ist die Stunde Dauerlast gespart.
2. Abbruch ZWISCHEN Kategorien, nie mitten in einer (F11 = A + F23 = B). Bricht
   man beim ersten Fehler ab, kennt man am Ende genau einen Fehler; laesst man
   alles laufen, kennt man das Bild. Der Kompromiss ist die Kategorie.
3. Kein Knotenname, nur Anforderungen (F22 = B). Den Knoten sucht der
   dispatcher -- dieselbe Sprache wie in den Personalakten.
4. Keine Uhr (F9 = C, "Sicherheit vor Zeit"). Ist kein passender Knoten da,
   wird gewartet (F13 = C), nicht abgekuerzt.

WARUM EINE GEAENDERTE TESTDATEI DEN LAUF ENTWERTET STATT IHN ZU VERHINDERN
Technik darf waehlen, WELCHE Tests laufen, aber nicht, WAS sie pruefen (F21). Ein
Verbot waere hier schwach: Sie hat Schreibrechte im Repo, ein `chmod` haelt sie
nicht auf. Die Pruefsumme nimmt ihr deshalb nicht die Moeglichkeit, sondern den
NUTZEN -- ein Lauf ueber veraenderte Testdateien zaehlt nicht als Nachweis, und
rollout.py fragt nach dem Nachweis. Das ist dieselbe Bauart wie beim
Upgrade-Register: nicht verbieten, sondern wertlos machen.

Aufruf:
    python -m palette                      alle aktiven Kategorien
    python -m palette --nur security       nur diese (Techniks Stellschraube)
    python -m palette --zeigen             Reihenfolge und Knotenwahl, ohne Lauf
    python -m palette --sollwerte          Pruefsummen der Testdateien neu hinterlegen
    python -m palette --lokal              nicht auf fremde Knoten ausweichen
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
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

import dispatcher                                              # noqa: E402
import speicher                                                # noqa: E402
import vorgaenge                                               # noqa: E402

REPO = FRAMEWORK.parents[2]
CFG = json.loads((PAKET / "palette.config.json").read_text(encoding="utf-8"))
FIRMA_CFG = json.loads((FRAMEWORK / "firma.config.json").read_text(encoding="utf-8"))

# Wie lange zwischen zwei Versuchen gewartet wird, wenn kein passender Knoten
# erreichbar ist. Keine Obergrenze -- siehe Regel 4 im Kopf.
WARTE_S = 30


# --- Pruefsummen ----------------------------------------------------------

def testdateien() -> list[Path]:
    """Alles, was den Ausgang eines Laufs beeinflusst: die Kategorie-Module,
    die Konfiguration und dieser Laeufer selbst. Wer nur die Module prueft,
    laesst die Haerte-Zuordnung ungeschuetzt -- und die entscheidet, ob ein
    Fehlschlag sperrt."""
    return sorted([p for p in PAKET.glob("k_*.py")]
                  + [PAKET / "palette.config.json", Path(__file__).resolve()])


def _summe(pfad: Path) -> str:
    return hashlib.sha256(pfad.read_bytes()).hexdigest()


def _rel(pfad: Path) -> str:
    return str(pfad.relative_to(REPO))


def sollwerte_setzen(von: str = "chef") -> int:
    """Aktuellen Zustand als Sollwert hinterlegen. Bewusst ein eigener Aufruf
    und kein Nebeneffekt des Laufs: Wuerde der Laeufer die Summen beilaeufig
    mitschreiben, waere der Schutz genau so viel wert wie keiner."""
    with speicher.verbindung() as c:
        c.execute("DELETE FROM firma.palette_datei")
        for p in testdateien():
            c.execute("INSERT INTO firma.palette_datei (pfad, pruefsumme, von) "
                      "VALUES (%s, %s, %s)", (_rel(p), _summe(p), von))
    return len(testdateien())


def pruefsummen_stimmen() -> tuple[bool, list[str]]:
    """(alles unveraendert?, Liste der Abweichungen). Ohne hinterlegte Sollwerte
    gilt der Lauf als ungeschuetzt -- das ist eine Abweichung, keine Freigabe."""
    with speicher.verbindung() as c:
        soll = dict(c.execute("SELECT pfad, pruefsumme FROM firma.palette_datei").fetchall())
    if not soll:
        return False, ["keine Sollwerte hinterlegt (palette --sollwerte)"]
    abweichung = []
    for p in testdateien():
        r = _rel(p)
        if r not in soll:
            abweichung.append(f"{r}: neu, kein Sollwert")
        elif soll[r] != _summe(p):
            abweichung.append(f"{r}: geaendert")
    for r in soll:
        if not (REPO / r).is_file():
            abweichung.append(f"{r}: fehlt")
    return not abweichung, abweichung


# --- Kategorien -----------------------------------------------------------

def kategorien(nur: list[str] | None = None) -> list[tuple[str, dict]]:
    """Aktive Kategorien in Laufreihenfolge. `nur` ist Techniks einzige
    Stellschraube (F21) -- sie waehlt aus, sie aendert nichts."""
    aus = [(n, k) for n, k in CFG["kategorien"].items()
           if k.get("aktiv", True) and (not nur or n in nur)]
    return sorted(aus, key=lambda t: t[1].get("reihenfolge", 999))


def modul_laden(name: str):
    """Das Modul einer Kategorie oder None, wenn es noch nicht existiert.
    Fehlend heisst `geplant`, nicht `kaputt` -- sonst waere der Rahmen so lange
    rot, bis die letzte Kategorie gebaut ist."""
    try:
        return importlib.import_module(f"palette.k_{name}")
    except ModuleNotFoundError:
        return None


def knoten_waehlen(anf: dict, *, warten: bool = True) -> str | None:
    """Passender Knoten fuer diese Anforderung. Wartet, statt auszuweichen."""
    while True:
        treffer = dispatcher.knoten_fuer(anf, agent=False)
        if treffer:
            return treffer[0][0]
        if not warten:
            return None
        print(f"    … kein Knoten fuer {anf or 'beliebig'} erreichbar, warte "
              f"{WARTE_S}s", flush=True)
        time.sleep(WARTE_S)


# --- Lauf -----------------------------------------------------------------

def commit_id() -> str:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _test_ausfuehren(test: dict, kat: str, knoten: str, hier: str) -> dict:
    """Einen einzelnen Test ausfuehren. Faellt er mit einer Ausnahme um, ist das
    ein Fehlschlag und kein Absturz des Laufs: Ein Test, der den Laeufer mit in
    den Abgrund zieht, verhindert alle uebrigen Befunde."""
    begonnen = time.time()
    if knoten != hier:
        # Der Test gehoert woandershin. Ausgefuehrt wird er dort mit derselben
        # CLI -- kein zweiter Weg fuer den Fernfall.
        ok, befund, mess = _fern(knoten, kat, test["name"])
    else:
        try:
            ok, befund, mess = test["lauf"]()
        except Exception as e:                                 # noqa: BLE001
            ok, befund, mess = False, f"Ausnahme: {e!r}", {}
    return {"test": test["name"], "status": "bestanden" if ok else "fehlgeschlagen",
            "befund": befund, "messwert": mess, "knoten": knoten,
            "dauer_s": round(time.time() - begonnen, 2)}


def _fern(knoten: str, kat: str, test: str) -> tuple[bool, str, dict]:
    """Einen Test auf einem anderen Knoten ausfuehren."""
    host = dispatcher.inventar().get(knoten, {})
    ziel = host.get("ssh") or host.get("mesh_ip")
    if not ziel:
        return False, f"kein SSH-Ziel fuer {knoten}", {}
    befehl = (f"cd {REPO}/system/mitarbeiter/framework && "
              f"venv/bin/python -m palette --lokal --einzeln {kat}:{test} --json")
    e = subprocess.run(["ssh", "-o", "BatchMode=yes", ziel, befehl],
                       capture_output=True, text=True)
    if e.returncode != 0:
        return False, f"fern gescheitert: {(e.stderr or e.stdout).strip()[:300]}", {}
    try:
        d = json.loads(e.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"unlesbare Antwort von {knoten}: {e.stdout.strip()[:200]}", {}
    return bool(d.get("bestanden")), str(d.get("befund", "")), dict(d.get("messwert") or {})


def _marke(kat: str, test: str) -> str:
    """Wiedererkennungszeichen im Anliegen. Damit ist ein Vorgang einem
    bestimmten Test zuzuordnen, ohne eine zweite Tabelle dafuer zu fuehren."""
    return f"[{kat}/{test}]"


def _offener_vorgang(fuer_wen: str, kat: str, test: str) -> dict | None:
    marke = _marke(kat, test)
    for v in vorgaenge.laden(fuer_wen):
        if v.get("status") == "offen" and marke in (v.get("anliegen") or ""):
            return v
    return None


def _vorgang_anlegen(kat: str, erg: dict, haerte: str) -> None:
    """Aus jedem Fehlschlag wird ein Vorgang fuer Technik (F18 = B). Ein Bericht,
    den niemand aufmacht, aendert nichts -- ein Vorgang taucht in ihrer Liste
    auf, bis er geschlossen ist.

    ABER NUR EINER JE TEST. Der erste Entwurf legte bei JEDEM Lauf einen neuen
    an; nach 22 Laeufen lagen 32 Vorgaenge, die meisten Dubletten desselben
    Befunds -- darunter laengst behobene. Ein Arbeitsvorrat, der schneller
    waechst als er abgearbeitet wird, wird irgendwann pauschal weggeklickt, und
    dann geht auch der echte Befund mit.
    """
    fuer_wen = CFG.get("vorgaenge_fuer", "technik")
    if _offener_vorgang(fuer_wen, kat, erg["test"]):
        print(f"      (Vorgang zu {kat}/{erg['test']} liegt schon offen)", flush=True)
        return
    anliegen = (f"Testpalette {_marke(kat, erg['test'])} fehlgeschlagen ({haerte}) "
                f"auf {erg['knoten']} — {erg['befund'][:300]}")
    try:
        vorgaenge.anlegen(fuer_wen, anliegen=anliegen, wartet_auf=fuer_wen,
                          fuer="chef", raum=FIRMA_CFG.get("forum_room", ""))
    except Exception as e:                                     # noqa: BLE001
        # Ein Vorgang, der nicht angelegt werden kann, darf den Lauf nicht
        # kippen -- der Befund selbst steht bereits in der Datenbank.
        print(f"    ! Vorgang nicht angelegt: {e!r}", flush=True)


def _vorgang_erledigt(kat: str, test: str) -> None:
    """Ein Test, der wieder besteht, schliesst seinen eigenen Vorgang.

    Das haelt den Vorrat von allein aktuell: Wer einen Fehler behebt, muss nicht
    daran denken, den Zettel wegzuwerfen. Ohne das lagen Befunde herum, die
    langst erledigt waren -- und ein Vorrat aus Altlasten sagt nichts mehr
    darueber, was zu tun ist.
    """
    fuer_wen = CFG.get("vorgaenge_fuer", "technik")
    v = _offener_vorgang(fuer_wen, kat, test)
    if not v:
        return
    try:
        vorgaenge.schliessen(fuer_wen, v["id"], "erledigt")
        print(f"      (Vorgang {v['id']} geschlossen — Test besteht wieder)", flush=True)
    except Exception as e:                                     # noqa: BLE001
        print(f"    ! Vorgang nicht geschlossen: {e!r}", flush=True)


def lauf(nur: list[str] | None = None, *, lokal: bool = False) -> dict:
    hier = dispatcher.eigener_knoten()
    commit = commit_id()
    geschuetzt, abweichungen = pruefsummen_stimmen()

    with speicher.verbindung() as c:
        lauf_id = c.execute(
            "INSERT INTO firma.palette_lauf (commit_id, nur) VALUES (%s, %s) "
            "RETURNING id", (commit, nur)).fetchone()[0]

    print(f"Palette — Commit {commit[:10]}, Knoten {hier}, Lauf {lauf_id}")
    if not geschuetzt:
        print(f"  ! ENTWERTET: {'; '.join(abweichungen)}")

    gesperrt, abgebrochen_bei, alle = False, None, []

    for kat, k in kategorien(nur):
        modul = modul_laden(kat)
        if modul is None:
            print(f"  · {kat}: geplant, noch kein Modul — uebersprungen")
            with speicher.verbindung() as c:
                c.execute(
                    "INSERT INTO firma.palette_ergebnis "
                    "(lauf_id, kategorie, test, status, haerte, befund, knoten) "
                    "VALUES (%s, %s, %s, 'uebersprungen', %s, %s, %s)",
                    (lauf_id, kat, "(kein Modul)", k.get("haerte", "warnt"),
                     "Kategorie geplant, Modul fehlt", hier))
            continue

        haerte = k.get("haerte", "warnt")
        print(f"  › {kat} ({haerte})")
        kat_fehler = 0

        for test in getattr(modul, "TESTS", []):
            anf = test.get("anforderung", k.get("anforderung") or {})
            knoten = hier if lokal else (knoten_waehlen(anf) or hier)
            erg = _test_ausfuehren(test, kat, knoten, hier)
            alle.append({**erg, "kategorie": kat})
            zeichen = "✓" if erg["status"] == "bestanden" else "✗"
            print(f"    {zeichen} {erg['test']} [{erg['knoten']}, "
                  f"{erg['dauer_s']}s] {erg['befund'][:90]}")

            with speicher.verbindung() as c:
                c.execute(
                    "INSERT INTO firma.palette_ergebnis (lauf_id, kategorie, test, "
                    "status, haerte, befund, messwert, knoten, dauer_s) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (lauf_id, kat, erg["test"], erg["status"], haerte,
                     erg["befund"], json.dumps(erg["messwert"]), erg["knoten"],
                     erg["dauer_s"]))

            if erg["status"] == "fehlgeschlagen":
                kat_fehler += 1
                _vorgang_anlegen(kat, erg, haerte)
            elif erg["status"] == "bestanden":
                _vorgang_erledigt(kat, erg["test"])

        # Erst HIER wird abgebrochen -- die Kategorie ist vollstaendig gelaufen.
        if kat_fehler and haerte == "sperrt":
            gesperrt, abgebrochen_bei = True, kat
            print(f"  ! {kat} sperrt ({kat_fehler} Fehlschlag/-schlaege) — Abbruch")
            break

    if not geschuetzt:
        ergebnis = "entwertet"
    elif gesperrt:
        ergebnis = "gescheitert"
    else:
        ergebnis = "bestanden"

    with speicher.verbindung() as c:
        c.execute("UPDATE firma.palette_lauf SET ergebnis = %s, beendet = now(), "
                  "abgebrochen_bei = %s WHERE id = %s",
                  (ergebnis, abgebrochen_bei, lauf_id))

    fehler = sum(1 for e in alle if e["status"] == "fehlgeschlagen")
    print(f"\nErgebnis: {ergebnis.upper()} — {len(alle) - fehler} bestanden, "
          f"{fehler} fehlgeschlagen")
    return {"lauf_id": lauf_id, "ergebnis": ergebnis, "commit": commit,
            "ergebnisse": alle}


# --- CLI ------------------------------------------------------------------

def _einzeln(spec: str) -> int:
    """Ein einzelner Test, Ergebnis als JSON auf der letzten Zeile. Das ist der
    Weg, ueber den ein Fernaufruf zurueckmeldet."""
    kat, _, name = spec.partition(":")
    modul = modul_laden(kat)
    if modul is None:
        print(json.dumps({"bestanden": False, "befund": f"Kategorie {kat} fehlt"}))
        return 1
    for t in getattr(modul, "TESTS", []):
        if t["name"] == name:
            try:
                ok, befund, mess = t["lauf"]()
            except Exception as e:                             # noqa: BLE001
                ok, befund, mess = False, f"Ausnahme: {e!r}", {}
            print(json.dumps({"bestanden": ok, "befund": befund, "messwert": mess}))
            return 0 if ok else 1
    print(json.dumps({"bestanden": False, "befund": f"Test {spec} unbekannt"}))
    return 1


def _zeigen(nur: list[str] | None) -> None:
    hier = dispatcher.eigener_knoten()
    geschuetzt, abweichungen = pruefsummen_stimmen()
    print(f"Knoten: {hier}   Pruefsummen: "
          f"{'in Ordnung' if geschuetzt else '; '.join(abweichungen)}\n")
    for kat, k in kategorien(nur):
        modul = modul_laden(kat)
        anf = k.get("anforderung") or {}
        ziel = knoten_waehlen(anf, warten=False) or "— keiner erreichbar"
        stand = f"{len(getattr(modul, 'TESTS', []))} Tests" if modul else "geplant"
        print(f"  {k.get('reihenfolge'):>3}  {kat:<16} {k.get('haerte'):<7} "
              f"{stand:<10} -> {ziel}")


def haupt(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nur", help="Kategorien, mit Komma getrennt")
    ap.add_argument("--zeigen", action="store_true")
    ap.add_argument("--sollwerte", action="store_true")
    ap.add_argument("--lokal", action="store_true")
    ap.add_argument("--einzeln", help="kategorie:test")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    nur = [s.strip() for s in a.nur.split(",")] if a.nur else None

    if a.einzeln:
        return _einzeln(a.einzeln)
    if a.sollwerte:
        n = sollwerte_setzen(os.environ.get("USER", "chef"))
        print(f"{n} Sollwerte hinterlegt")
        return 0
    if a.zeigen:
        _zeigen(nur)
        return 0

    e = lauf(nur, lokal=a.lokal)
    if a.json:
        print(json.dumps(e, default=str))
    return 0 if e["ergebnis"] == "bestanden" else 1


if __name__ == "__main__":
    raise SystemExit(haupt())
