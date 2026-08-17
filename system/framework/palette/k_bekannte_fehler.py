#!/usr/bin/env python3
"""k_bekannte_fehler.py — echte Regressionstests aus den Fehlern der Gespraeche.

F6 = A: keine Checkliste, sondern Tests, die den Fehler ausloesen wuerden, waere
er zurueck. F6.A.3 = a: reine Umgebungsfehler bleiben draussen (ufw-Policy,
overlayroot, Docker-DNAT) -- die haengen an Maschinen, nicht am Code. Der
Portscan in k_security deckt den einen Fall ab, der davon uebrig bleibt.

WAS ALLE DIESE FEHLER GEMEINSAM HATTEN
Keiner war laut. Es gab keinen roten Balken, keine Ausnahme, keinen Absturz --
die Firma lief weiter und tat etwas anderes als gedacht:

  * dict.get(k, 0) gab None zurueck, weil der Schluessel MIT None belegt war
  * SET application_name = %s warf einen Syntaxfehler, den der Aufrufer abfing:
    die Einmal-Sperre war nie aktiv, und niemand haette es bemerkt
  * der Tagesdeckel wurde an zwei Stellen gelesen; die Aenderung wirkte nur an
    einer, der Status meldete weiter den alten Wert
  * erreichbar() prueft SSH:22 -- der Haupt-PC ist SSH-CLIENT und galt als tot

Deshalb warnt diese Kategorie nur (F1.B.3 = b) und sperrt nicht: Sie findet
Rueckfaelle, keine akuten Gefahren. Ein Fehler, der schon einmal gemacht wurde,
haelt den Rollout nicht auf -- das ist bewusst so entschieden (W4).
"""
from __future__ import annotations

import json
import re
import time
import sys
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

REPO = FRAMEWORK.parents[2]


def none_wert_statt_fehlendem_schluessel() -> tuple[bool, str, dict]:
    """Fehler 1: dict.get(k, 0) liefert None, wenn der Schluessel mit None belegt ist.

    Der Standardwert greift nur bei FEHLENDEM Schluessel. Ein Auftrag blieb
    dadurch auf 'laeuft' haengen: int(None) flog, der Griff wurde nicht
    zurueckgenommen.
    """
    fall = {"tokens": None}
    try:
        int(fall.get("tokens", 0))
        naiv_haelt = True
    except TypeError:
        naiv_haelt = False
    sicher = int(fall.get("tokens") or 0)

    # Der Test prueft die LEHRE, nicht die Sprache: Wo im Code auf Zahlen aus
    # Fremddaten zugegriffen wird, muss `or 0` stehen. Faende sich wieder ein
    # int(x.get(..., 0)) auf einem Feld, das None sein kann, waere das der
    # Rueckfall.
    verdacht = []
    hier = Path(__file__).resolve()
    for p in FRAMEWORK.rglob("*.py"):
        # Die eigene Datei bleibt draussen: Sie MUSS den Fehler enthalten, um
        # ihn vorzufuehren. Der erste Lauf meldete drei Treffer in sich selbst,
        # zwei davon in einem Kommentar und einem Ergebnistext.
        if "venv" in p.parts or p.resolve() == hier:
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        for zeile_nr, zeile in enumerate(t.splitlines(), 1):
            code = zeile.split("#", 1)[0]
            # Wortgrenze: Ohne \b traf "int(" auch in "print(" zu, und der Test
            # meldete eine voellig harmlose Zeile. Dritter Fehlalarm dieses
            # Tests -- jeder davon haette ihn ein Stueck unglaubwuerdiger gemacht.
            if (re.search(r"\b(int|float)\(", code) and ".get(" in code
                    and ", 0)" in code and " or " not in code):
                verdacht.append(f"{p.relative_to(REPO)}:{zeile_nr}")
    return (sicher == 0 and not naiv_haelt and not verdacht,
            "kein int(...get(x, 0)) ohne 'or 0' im Framework" if not verdacht
            else f"{len(verdacht)} Stelle(n): " + "; ".join(verdacht[:4]),
            {"verdacht": len(verdacht)})


def sperre_ist_wirklich_aktiv() -> tuple[bool, str, dict]:
    """Fehler 2: SET nimmt keine Bind-Parameter -- die Einmal-Sperre war still tot.

    Der Aufrufer fing die Ausnahme ab. Es gab keinen Fehler zu sehen, nur eine
    Sperre, die nichts sperrte. Der Test greift deshalb NICHT nach dem Code,
    sondern nach der Wirkung: zweimal dieselbe Sperre, das zweite Mal muss
    scheitern.
    """
    import einmalig
    name = "palette-regressionstest"
    erste = einmalig.versuchen(name)
    # Zweiter Versuch aus einer FREMDEN Verbindung -- aus derselben heraus
    # gaebe Postgres die Sperre erneut aus, und der Test waere wertlos.
    import speicher
    c = speicher.verbindung()
    try:
        zweite = c.execute("SELECT pg_try_advisory_lock(%s, %s)",
                           (einmalig.BEREICH, einmalig._schluessel(name))).fetchone()[0]
        if zweite:
            c.execute("SELECT pg_advisory_unlock(%s, %s)",
                      (einmalig.BEREICH, einmalig._schluessel(name)))
    finally:
        c.close()
    einmalig.freigeben(name) if hasattr(einmalig, "freigeben") else None
    return (erste and not zweite,
            "Sperre greift: erster Versuch ja, zweiter nein" if (erste and not zweite)
            else f"Sperre wirkungslos (erster={erste}, zweiter={zweite})",
            {"erster": erste, "zweiter": zweite})


def deckel_hat_nur_eine_quelle() -> tuple[bool, str, dict]:
    """Fehler 3 (16.08.): Der Tagesdeckel wurde an zwei Stellen unabhaengig aus
    der Konfiguration gelesen. Die Aenderung blieb wirkungslos.

    Der Test zaehlt die direkten Zugriffe auf den Konfigurationsschluessel. Es
    darf genau EINE Stelle geben, die ihn liest -- die abgeleitete Funktion.
    """
    broker = FRAMEWORK.parent / "buchhalter" / "broker" / "broker.py"
    t = broker.read_text(encoding="utf-8")
    direkt = [i for i, z in enumerate(t.splitlines(), 1)
              if 'CFG.get("api_deckel_eur_tag"' in z or "CFG.get('api_deckel_eur_tag'" in z]
    return (len(direkt) <= 1,
            f"genau {len(direkt)} direkte Lesestelle" if len(direkt) <= 1
            else f"{len(direkt)} unabhaengige Lesestellen: Zeilen {direkt}",
            {"lesestellen": len(direkt)})


def erreichbarkeit_nutzt_echten_port() -> tuple[bool, str, dict]:
    """Fehler 4: Die Erreichbarkeitspruefung nahm pauschal SSH:22 und erklaerte
    damit ausgerechnet den Haupt-PC fuer tot -- der ist SSH-Client, kein Server.
    """
    import dispatcher
    inv = dispatcher.inventar()
    ohne = [n for n, h in inv.items() if not h.get("pruef_port")]
    quelle = (FRAMEWORK / "dispatcher.py").read_text(encoding="utf-8")
    liest_feld = 'host.get("pruef_port"' in quelle
    return (liest_feld and not ohne,
            "jeder Knoten hat einen pruef_port, und er wird gelesen" if (liest_feld and not ohne)
            else f"pruef_port fehlt bei {ohne} / gelesen={liest_feld}",
            {"ohne_port": len(ohne)})


def vagheitspruefung_kennt_neue_werkzeuge() -> tuple[bool, str, dict]:
    """Fehler 5: Die Vagheitspruefung hatte eine feste Feldnamensliste und kannte
    'version' nicht -- ein neues Werkzeug wirkte 'stur' statt 'kaputt'.

    Geprueft wird an einem Werkzeug, das es damals noch nicht gab: Ein
    ausgefuelltes Pflichtfeld darf nicht als vage gelten.
    """
    import werkzeuge
    akte = json.loads((FRAMEWORK.parent / "buchhalter" / "personalakte.json")
                      .read_text(encoding="utf-8"))
    voll = werkzeuge.braucht_freigabe(
        akte, "konto_erfassen",
        {"liquide_eur": "1000.00", "angelegt_eur": "2000.00", "quelle": "Auszug"})
    leer = werkzeuge.braucht_freigabe(akte, "konto_erfassen", {})
    return (not voll[0] and leer[0],
            "ausgefuelltes Pflichtfeld laeuft durch, leeres wird gebremst"
            if (not voll[0] and leer[0]) else f"voll={voll}, leer={leer}",
            {"voll": voll[0], "leer": leer[0]})


def units_werden_ueberall_gesucht() -> tuple[bool, str, dict]:
    """Fehler 6: Die Installation suchte Units nur in framework/deploy und
    uebersah buchhalter-broker.service -- ein Knoten waere wortlos ohne Broker
    gelaufen. Gefunden hat das die Sandbox, nicht ein Mensch.
    """
    units = sorted(p.name for p in (FRAMEWORK.parent).rglob("*.service")
                   if "venv" not in p.parts)
    skript = (FRAMEWORK / "deploy" / "install.sh").read_text(encoding="utf-8")
    # Sucht das Skript im ganzen Mitarbeiterbaum oder nur in einem Ordner?
    weit = "unit_suchen" in skript or "rglob" in skript or "find" in skript
    return (weit and len(units) >= 2,
            f"{len(units)} Units im Baum, Suche greift weit" if weit
            else "die Suche greift nur in einem Ordner",
            {"units": len(units)})


def ein_agent_denkt_nie_zweimal() -> tuple[bool, str, dict]:
    """Seit dem 16.08. arbeiten mehrere Rechenplaetze parallel (Chef:
    "unterschiedliche gleichzeitig ist in Ordnung, einer mehrfach nicht").

    Damit entsteht eine Gefahr, die es vorher nicht gab: Zwei Arbeiter koennten
    zwei Auftraege DESSELBEN Agenten aufnehmen -- zwei Antworten auf dieselbe
    Frage, zwei Werkzeugaufrufe, zwei Vorgaenge. Die Sperre dagegen ist eine
    einzige Mengenpruefung im Broker, und genau so etwas faellt beim naechsten
    Umbau lautlos heraus. Deshalb steht hier ein Waechter.

    Geprueft wird unter Last, nicht in Ruhe: Nebenlaeufigkeitsfehler zeigen sich
    nicht am Einzelaufruf.
    """
    import threading
    sys.path.insert(0, str(FRAMEWORK.parent / "buchhalter" / "broker"))
    import broker

    for a in ("_regr_a", "_regr_b"):
        broker._loslassen(a)
    gleichzeitig: dict[str, int] = {}
    verletzt: list[str] = []
    sperre = threading.Lock()

    def arbeite(agent: str) -> None:
        if not broker._greifen(agent):
            return
        with sperre:
            gleichzeitig[agent] = gleichzeitig.get(agent, 0) + 1
            if gleichzeitig[agent] > 1:
                verletzt.append(agent)
        time.sleep(0.03)
        with sperre:
            gleichzeitig[agent] -= 1
        broker._loslassen(agent)

    ts = [threading.Thread(target=arbeite, args=(a,))
          for a in ("_regr_a", "_regr_b") * 6]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # Die Gegenprobe: Zwei VERSCHIEDENE Agenten muessen gleichzeitig duerfen.
    a_ok = broker._greifen("_regr_a")
    b_ok = broker._greifen("_regr_b")
    broker._loslassen("_regr_a")
    broker._loslassen("_regr_b")
    gut = not verletzt and a_ok and b_ok
    return (gut,
            "kein Agent doppelt, zwei verschiedene gleichzeitig erlaubt" if gut
            else f"Doppelbelegung: {set(verletzt)} / zwei verschiedene: {a_ok and b_ok}",
            {"doppelt": len(verletzt), "zwei_verschiedene": a_ok and b_ok})


TESTS = [
    {"name": "none_wert_statt_fehlendem_schluessel", "lauf": none_wert_statt_fehlendem_schluessel},
    {"name": "ein_agent_denkt_nie_zweimal", "lauf": ein_agent_denkt_nie_zweimal},
    {"name": "sperre_ist_wirklich_aktiv", "lauf": sperre_ist_wirklich_aktiv},
    {"name": "deckel_hat_nur_eine_quelle", "lauf": deckel_hat_nur_eine_quelle},
    {"name": "erreichbarkeit_nutzt_echten_port", "lauf": erreichbarkeit_nutzt_echten_port},
    {"name": "vagheitspruefung_kennt_neue_werkzeuge", "lauf": vagheitspruefung_kennt_neue_werkzeuge},
    {"name": "units_werden_ueberall_gesucht", "lauf": units_werden_ueberall_gesucht},
]
