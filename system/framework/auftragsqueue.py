#!/usr/bin/env python3
"""auftragsqueue.py — die Queue zwischen Scheduler (VPS) und Worker (PC).

Schritt 4 des Plans wandernde-werkbank. Bisher hielt der Broker seine
Warteschlange im Arbeitsspeicher: Ein Neustart verwarf alles, was anstand, und
ein Verbindungsabbruch zwischen Agent und Broker liess den Auftrag ins Leere
laufen. Beides faellt hier weg.

Traeger ist Postgres. `SELECT ... FOR UPDATE SKIP LOCKED` gibt genau die
Semantik einer Arbeitsqueue: Zwei Worker greifen nie denselben Auftrag, ein
abgestuerzter Worker blockiert ihn nicht dauerhaft, und nichts geht verloren.
Ein eigener Queue-Dienst waere mehr bewegliche Teile fuer weniger Garantien.

ZWEI-SIGNAL-AUSFALLERKENNUNG (des Chefs Vorgabe aus Runde 03):
Ein Auftrag gilt erst dann als verwaist, wenn **beides** zutrifft --
  1. der Worker meldet keinen Herzschlag mehr (`herzschlag` veraltet), UND
  2. der Host ist im Mesh nicht erreichbar.
Ein langsam denkender Worker sendet weiter Herzschlaege; auf dieser Maschine
laeuft qwen3.6:35b-a3b mit CPU-Offload, lange Antwortzeiten sind der Normalfall.
Nur der Timeout allein wuerde also genau das Denken abwuergen, das man wollte --
derselbe Denkfehler, den denkzeit.py fuer Modellaufrufe behebt.
"""
from __future__ import annotations

import json
import time
from typing import Any

import speicher

# Wie alt darf ein Herzschlag sein, bevor ein Auftrag als verdaechtig gilt?
# Grosszuegig: er wird waehrend der Arbeit alle 30 s erneuert.
HERZSCHLAG_FRIST_S = 180
# Wie oft darf ein Auftrag neu aufgelegt werden, bevor er aufgegeben wird?
MAX_VERSUCHE = 3

_RANG = "ORDER BY prioritaet, passung DESC, losnummer, erstellt"


# --- Einstellen (Scheduler-Seite) ----------------------------------------
def einstellen(agent_id: str, auftrag: dict, *, prioritaet: int = 3,
               passung: int = 0, losnummer: int = 0) -> int:
    with speicher.verbindung() as c:
        return c.execute(
            "INSERT INTO firma.auftraege (agent_id, auftrag, prioritaet, passung, losnummer) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (agent_id, json.dumps(auftrag, ensure_ascii=False),
             prioritaet, passung, losnummer)).fetchone()[0]


def ergebnis_abwarten(auftrag_id: int, frist_s: int = 5400,
                      takt_s: float = 1.0) -> dict | None:
    """Blockiert, bis der Auftrag fertig ist. Rueckgabe: das Ergebnis oder None.

    Die Frist ist bewusst sehr lang und dient nur als letztes Netz -- die
    eigentliche Ausfallerkennung leistet der Herzschlag, nicht diese Uhr.
    """
    ende = time.monotonic() + frist_s
    while time.monotonic() < ende:
        with speicher.verbindung() as c:
            z = c.execute("SELECT status, ergebnis, fehler FROM firma.auftraege WHERE id = %s",
                          (auftrag_id,)).fetchone()
        if not z:
            return None
        status, ergebnis, fehler = z
        if status == "fertig":
            return ergebnis
        if status in ("fehler", "aufgegeben"):
            return {"error": fehler or status}
        time.sleep(takt_s)
    return {"error": f"Zeitueberschreitung nach {frist_s} s"}


# --- Abarbeiten (Worker-Seite) -------------------------------------------
def greifen(host: str) -> dict | None:
    """Naechsten Auftrag holen und als laufend markieren. None, wenn nichts ansteht."""
    with speicher.verbindung() as c:
        z = c.execute(
            "UPDATE firma.auftraege SET status='laeuft', host=%s, geholt=now(), "
            "  herzschlag=now(), versuche=versuche+1 "
            "WHERE id = (SELECT id FROM firma.auftraege WHERE status='wartet' "
            f"           {_RANG} FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, agent_id, auftrag, versuche", (host,)).fetchone()
    if not z:
        return None
    return {"id": z[0], "agent_id": z[1], "auftrag": z[2], "versuche": z[3]}


def herzschlag(auftrag_id: int) -> None:
    """Lebenszeichen waehrend der Arbeit. Solange das kommt, gilt der Worker als
    beschaeftigt -- egal wie lange er denkt."""
    with speicher.verbindung() as c:
        c.execute("UPDATE firma.auftraege SET herzschlag=now() WHERE id=%s", (auftrag_id,))


def fertig(auftrag_id: int, ergebnis: dict) -> None:
    with speicher.verbindung() as c:
        c.execute("UPDATE firma.auftraege SET status='fertig', ergebnis=%s, fertig=now() "
                  "WHERE id=%s", (json.dumps(ergebnis, ensure_ascii=False), auftrag_id))


def fehlgeschlagen(auftrag_id: int, text: str, *, erneut: bool = True) -> None:
    """Fehler vermerken. `erneut` legt den Auftrag zurueck in die Queue, solange
    das Versuchslimit nicht erreicht ist."""
    with speicher.verbindung() as c:
        z = c.execute("SELECT versuche FROM firma.auftraege WHERE id=%s",
                      (auftrag_id,)).fetchone()
        versuche = z[0] if z else MAX_VERSUCHE
        if erneut and versuche < MAX_VERSUCHE:
            c.execute("UPDATE firma.auftraege SET status='wartet', host=NULL, "
                      "  herzschlag=NULL, fehler=%s WHERE id=%s", (text[:500], auftrag_id))
        else:
            c.execute("UPDATE firma.auftraege SET status=%s, fehler=%s, fertig=now() "
                      "WHERE id=%s",
                      ("aufgegeben" if versuche >= MAX_VERSUCHE else "fehler",
                       text[:500], auftrag_id))


def beim_start_freigeben(host: str) -> int:
    """Altlasten eines abgestuerzten Vorgaengers zurueck in die Queue.

    Der Fall, den die Zwei-Signal-Regel NICHT abdeckt: Der Worker startet neu,
    der Host bleibt dabei erreichbar. Aus Sicht der Aufsicht sieht das aus wie
    ein Worker, der lange denkt -- der Auftrag haenge sonst ewig.

    Ein frisch gestarteter Worker hat definitionsgemaess nichts in Arbeit. Alles,
    was in der Datenbank auf seinen Namen laeuft, gehoert einem Prozess, den es
    nicht mehr gibt. Nur er selbst kann das wissen; deshalb steht es hier und
    nicht in der Aufsicht.
    """
    with speicher.verbindung() as c:
        z = c.execute(
            "UPDATE firma.auftraege SET status='wartet', host=NULL, herzschlag=NULL, "
            "  fehler='Worker neu gestartet — Auftrag neu aufgelegt' "
            "WHERE status='laeuft' AND host=%s AND versuche < %s RETURNING id",
            (host, MAX_VERSUCHE)).fetchall()
        # Was das Versuchslimit gerissen hat, wird nicht endlos wiederholt.
        auf = c.execute(
            "UPDATE firma.auftraege SET status='aufgegeben', fertig=now(), "
            "  fehler='Worker neu gestartet, Versuchslimit erreicht' "
            "WHERE status='laeuft' AND host=%s AND versuche >= %s RETURNING id",
            (host, MAX_VERSUCHE)).fetchall()
    return len(z) + len(auf)


def worker_melden(host: str, *, gpu_frei_mb: int | None = None,
                  laeuft: str | None = None, version: str = "") -> None:
    """Praesenz des Workers -- auch im Leerlauf. Ohne das waere 'kein
    Herzschlag' nicht von 'keine Arbeit' zu unterscheiden."""
    with speicher.verbindung() as c:
        c.execute(
            "INSERT INTO firma.worker (host, gesehen, gpu_frei_mb, laeuft, version) "
            "VALUES (%s, now(), %s, %s, %s) "
            "ON CONFLICT (host) DO UPDATE SET gesehen=now(), "
            "  gpu_frei_mb=EXCLUDED.gpu_frei_mb, laeuft=EXCLUDED.laeuft, "
            "  version=EXCLUDED.version",
            (host, gpu_frei_mb, laeuft, version))


# --- Aufsicht (Scheduler-Seite) ------------------------------------------
def verwaiste(frist_s: int = HERZSCHLAG_FRIST_S) -> list[dict]:
    """Laufende Auftraege ohne frischen Herzschlag. ERSTES der beiden Signale --
    ob der Host wirklich weg ist, prueft der Aufrufer (dispatcher.erreichbar)."""
    with speicher.verbindung() as c:
        z = c.execute(
            "SELECT id, agent_id, host, versuche, "
            "       EXTRACT(EPOCH FROM (now() - herzschlag))::int "
            "FROM firma.auftraege WHERE status='laeuft' "
            "  AND herzschlag < now() - make_interval(secs => %s)", (frist_s,)).fetchall()
    return [{"id": r[0], "agent_id": r[1], "host": r[2], "versuche": r[3],
             "stille_s": r[4]} for r in z]


def zurueck_in_die_queue(auftrag_id: int, grund: str) -> None:
    fehlgeschlagen(auftrag_id, f"neu aufgelegt: {grund}", erneut=True)


def aufraeumen(host_erreichbar=None, frist_s: int = HERZSCHLAG_FRIST_S) -> list[dict]:
    """Verwaiste Auftraege einsammeln — die ZWEI-SIGNAL-Regel in Code.

    Ein laufender Auftrag wird nur dann neu aufgelegt, wenn **beides** gilt:
      1. sein Herzschlag ist aelter als `frist_s`, UND
      2. der Host, der ihn haelt, ist nicht erreichbar.

    Fehlt nur das erste Signal, denkt der Worker vermutlich noch -- auf dieser
    Maschine sind 200 s Denkzeit normal. Ihn dann zu unterbrechen waere genau
    der Fehler, den denkzeit.py fuer Modellaufrufe behebt, nur eine Ebene hoeher.

    `host_erreichbar` ist eine Funktion name -> bool; ohne Angabe wird
    dispatcher.erreichbar benutzt.
    """
    if host_erreichbar is None:
        import dispatcher

        def host_erreichbar(name: str) -> bool:
            h = dispatcher.inventar().get(name)
            return bool(h) and dispatcher.erreichbar(h)

    aufgeraeumt = []
    for a in verwaiste(frist_s):
        if a["host"] and host_erreichbar(a["host"]):
            continue          # still, aber am Leben -> in Ruhe lassen
        zurueck_in_die_queue(a["id"], f"Host {a['host']} still seit {a['stille_s']} s")
        aufgeraeumt.append(a)
    return aufgeraeumt


def stand() -> dict[str, Any]:
    with speicher.verbindung() as c:
        zeilen = c.execute("SELECT status, anzahl, aeltester, ohne_lebenszeichen "
                           "FROM firma.warteschlange").fetchall()
        worker = c.execute("SELECT host, gesehen, laeuft, gpu_frei_mb FROM firma.worker "
                           "ORDER BY host").fetchall()
    return {
        "queue": {r[0]: {"anzahl": r[1], "aeltester": str(r[2]) if r[2] else None,
                         "ohne_lebenszeichen": r[3]} for r in zeilen},
        "worker": [{"host": w[0], "gesehen": str(w[1]), "laeuft": w[2],
                    "gpu_frei_mb": w[3]} for w in worker],
    }


if __name__ == "__main__":
    print(json.dumps(stand(), ensure_ascii=False, indent=2))
