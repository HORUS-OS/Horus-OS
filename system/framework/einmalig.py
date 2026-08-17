#!/usr/bin/env python3
"""einmalig.py — verhindert, dass derselbe Mitarbeiter zweimal laeuft.

des Chefs Vorgabe vom 2026-08-16, als der zweite PC dazukam:
"Jeder Mitarbeiter darf nur einmal laufen, nicht 2 mal simultan."

Bis jetzt gab es genau einen Rechner, der Agenten trug -- die Frage stellte
sich nicht. Mit einem zweiten Knoten stellt sie sich sofort, und die Folgen
waeren still statt laut: Zwei Instanzen von Projektleitung haengen im selben
Matrix-Raum, beantworten dieselbe Nachricht doppelt, oeffnen zwei Vorgaenge
fuer ein Anliegen und schliessen jeweils den des anderen nicht.

WARUM EINE DATENBANKSPERRE UND KEINE PID-DATEI
Eine Datei in /run oder ein Eintrag in hosts.json wirkt nur auf DEM Rechner,
der sie schreibt. Der Doppellauf entsteht aber gerade zwischen zwei Rechnern.
Postgres ist das einzige, was beide sicher sehen -- und `pg_advisory_lock`
haengt an der VERBINDUNG: Stirbt der Prozess, faellt die Verbindung, und die
Sperre ist sofort frei. Kein verwaister Eintrag, den jemand von Hand aufraeumen
muss, kein Aufraeumer, der selbst ausfallen kann.

Das ist dieselbe Ueberlegung wie beim Herzschlag in auftragsqueue.py, nur
haerter: Dort wird ein Ausfall ERKANNT, hier kann der Doppellauf gar nicht
erst entstehen.

Benutzung -- eine Zeile am Anfang des Agentenprozesses:

    import einmalig
    einmalig.beanspruchen("projektleitung")     # beendet den Prozess, wenn schon belegt

Oder als Kontext, wenn ordentlich aufgeraeumt werden soll:

    with einmalig.platz("projektleitung"):
        ...
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import sys

import speicher

# Eigener Zahlenraum fuer die Sperren dieser Firma. pg_advisory_lock nimmt zwei
# 32-Bit-Zahlen; die erste trennt uns von allem anderen, was in dieser Datenbank
# je Sperren nehmen koennte.
BEREICH = 0x484F5255            # "HORU"

# Die Verbindung MUSS am Leben bleiben, solange die Sperre gelten soll --
# deshalb ein Modulzustand und keine lokale Variable, die der Garbage Collector
# einsammelt. Genau daran scheitern die meisten Advisory-Lock-Versuche.
_halter: dict[str, object] = {}


def _schluessel(name: str) -> int:
    """Stabile 32-Bit-Zahl aus dem Agentennamen. Muss auf jedem Knoten dieselbe
    sein -- deshalb ein Hash und nicht Pythons hash(), der pro Prozess gesalzen
    wird und damit auf zwei Rechnern verschieden ausfaellt."""
    h = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def wer_haelt(name: str) -> str | None:
    """Wer haelt den Platz gerade? None, wenn frei.

    Nur zur Auskunft -- zwischen dieser Frage und einem Beanspruchen kann sich
    die Lage aendern. Verlassen darf man sich allein auf den Rueckgabewert von
    `beanspruchen`.
    """
    try:
        with speicher.verbindung() as c:
            z = c.execute(
                "SELECT a.application_name FROM pg_locks l "
                "JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype='advisory' AND l.classid=%s AND l.objid=%s "
                "  AND l.granted",
                (BEREICH, _schluessel(name))).fetchone()
        return z[0] if z else None
    except Exception:                                       # noqa: BLE001
        return None


def versuchen(name: str) -> bool:
    """Platz beanspruchen, ohne zu warten. True = bekommen.

    Die Verbindung wird bewusst offen gehalten; sie IST die Sperre.
    """
    if name in _halter:
        return True
    c = speicher.verbindung()
    # Der Name taucht in pg_stat_activity auf -- so ist im Zweifel zu sehen,
    # WELCHER Rechner den Platz haelt, statt nur DASS er belegt ist.
    # set_config statt "SET application_name = %s": SET nimmt in Postgres keine
    # Bind-Parameter, der Platzhalter kaeme als Syntaxfehler zurueck. Das waere
    # hier besonders tueckisch, weil der Aufrufer die Ausnahme abfaengt und
    # trotzdem startet -- die Sperre waere dann still nie aktiv gewesen.
    c.execute("SELECT set_config('application_name', %s, false)",
              (f"firma:{name}@{os.environ.get('FIRMA_KNOTEN') or socket.gethostname()}",))
    ok = c.execute("SELECT pg_try_advisory_lock(%s, %s)",
                   (BEREICH, _schluessel(name))).fetchone()[0]
    if ok:
        _halter[name] = c
        return True
    c.close()
    return False


def freigeben(name: str) -> None:
    c = _halter.pop(name, None)
    if c is not None:
        with contextlib.suppress(Exception):
            c.close()          # Verbindung zu, Sperre weg


def beanspruchen(name: str, *, hart: bool = True) -> bool:
    """Platz nehmen -- oder sich beenden, wenn er belegt ist.

    `hart=False` gibt nur False zurueck, statt den Prozess zu beenden; das ist
    fuer Aufrufer gedacht, die selbst entscheiden wollen (etwa ein Werkzeug, das
    nur warnen soll).
    """
    if versuchen(name):
        return True
    halter = wer_haelt(name) or "einem anderen Knoten"
    hinweis = (f"'{name}' läuft bereits auf {halter}. "
               f"Zwei Instanzen desselben Mitarbeiters sind nicht zulässig — "
               f"sie beantworten dieselbe Nachricht doppelt.")
    if not hart:
        print(f"  {hinweis}", file=sys.stderr)
        return False
    # Beenden mit 0, nicht mit einem Fehlercode: Das hier ist kein Fehlschlag,
    # sondern der gewollte Ausgang. Mit Restart=on-failure in der Unit wuerde
    # ein Fehlercode eine endlose Neustartschleife ausloesen, waehrend der
    # rechtmaessige Halter friedlich weiterlaeuft.
    print(f"  {hinweis}\n  Dieser Prozess beendet sich.", file=sys.stderr)
    raise SystemExit(0)


@contextlib.contextmanager
def platz(name: str):
    beanspruchen(name)
    try:
        yield
    finally:
        freigeben(name)


def belegung() -> list[tuple[str, str]]:
    """Alle gehaltenen Firmen-Plaetze — (Sperrzahl, Halter)."""
    with speicher.verbindung() as c:
        z = c.execute(
            "SELECT l.objid, a.application_name FROM pg_locks l "
            "JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype='advisory' AND l.classid=%s AND l.granted "
            "ORDER BY a.application_name", (BEREICH,)).fetchall()
    return [(str(r[0]), r[1] or "?") for r in z]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        n = sys.argv[1]
        h = wer_haelt(n)
        print(f"  {n}: {'belegt von ' + h if h else 'frei'}")
    else:
        b = belegung()
        print("  Belegte Plätze:" if b else "  Kein Platz belegt.")
        for _z, halter in b:
            print(f"    {halter}")
