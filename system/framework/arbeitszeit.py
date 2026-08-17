#!/usr/bin/env python3
"""arbeitszeit.py — des Chefs Arbeitsende festhalten, indem man ihn fragt.

Chef am 2026-08-17: "Assistenz soll mich regelmaessig unter der Woche um 14 Uhr
fragen, ob ich schon Feierabend gemacht habe, um meine Arbeitszeiten zu
erfassen."

WARUM GEFRAGT UND NICHT GEMESSEN WIRD
Anwesenheit am Rechner ist nicht Arbeitszeit. Chef arbeitet auswaerts, und der
PC laeuft auch, wenn er es nicht tut. Jede automatische Messung waere eine
Schaetzung, die sich fuer eine Tatsache ausgibt -- und eine falsche Zahl in
einer Zeiterfassung ist schlimmer als eine fehlende.

WARUM HOECHSTENS EINMAL AM TAG GEFRAGT WIRD
Eine Frage, die zweimal kommt, wird beim dritten Mal ignoriert. Der Eintrag
`gefragt_um` ist deshalb die Sperre: Steht er, ruht die Wache bis morgen --
auch wenn der Agent zwischendurch neu startet.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
sys.path.insert(0, str(FRAMEWORK))

import speicher                                                # noqa: E402


def schon_gefragt(tag: date | None = None) -> bool:
    tag = tag or date.today()
    with speicher.verbindung() as c:
        z = c.execute("SELECT gefragt_um FROM firma.arbeitszeit WHERE tag = %s",
                      (tag,)).fetchone()
    return bool(z and z[0])


def frage_vermerken(tag: date | None = None) -> None:
    """Vor dem Fragen eintragen, nicht danach.

    Wer erst nach der Zustellung vermerkt, fragt bei einem Fehler zwischendurch
    zweimal -- und die zweite Frage ist die, die nervt.
    """
    tag = tag or date.today()
    with speicher.verbindung() as c:
        c.execute("INSERT INTO firma.arbeitszeit (tag, gefragt_um) VALUES (%s, now()) "
                  "ON CONFLICT (tag) DO UPDATE SET gefragt_um = now() "
                  "WHERE firma.arbeitszeit.gefragt_um IS NULL", (tag,))


def erfassen(feierabend: str | None, notiz: str = "",
             tag: date | None = None) -> str:
    """Antwort festhalten. `feierabend` als 'HH:MM' oder None fuer 'noch nicht'."""
    tag = tag or date.today()
    zeit: time | None = None
    if feierabend:
        t = feierabend.strip().replace(".", ":")
        for fmt in ("%H:%M", "%H:%M:%S", "%H"):
            try:
                zeit = datetime.strptime(t, fmt).time()
                break
            except ValueError:
                continue
        if zeit is None:
            return f"Zeitangabe nicht lesbar: {feierabend!r} (erwartet HH:MM)"
    with speicher.verbindung() as c:
        # coalesce beim Feierabend: Ein spaeterer Aufruf OHNE Uhrzeit ("noch
        # nicht") darf eine bereits erfasste Zeit nicht loeschen. Eine
        # Zeiterfassung, die Erfasstes wieder vergisst, ist schlimmer als keine
        # -- der Fehler faellt erst auf, wenn die Zahl gebraucht wird.
        # Zum Korrigieren gibt es `--korrigieren`, das ausdruecklich ueberschreibt.
        c.execute(
            "INSERT INTO firma.arbeitszeit (tag, feierabend, notiz) VALUES (%s, %s, %s) "
            "ON CONFLICT (tag) DO UPDATE SET "
            "feierabend = coalesce(EXCLUDED.feierabend, firma.arbeitszeit.feierabend), "
            "notiz = coalesce(nullif(EXCLUDED.notiz, ''), firma.arbeitszeit.notiz), "
            "erfasst = now()",
            (tag, zeit, notiz.strip()))
    return (f"Arbeitsende {tag}: {zeit:%H:%M} erfasst." if zeit
            else f"{tag}: noch kein Feierabend — vermerkt.")


def korrigieren(feierabend: str | None, tag: date | None = None) -> str:
    """Ausdrueckliches Ueberschreiben, auch mit NULL. Getrennt von erfassen(),
    damit ein versehentliches "noch nicht" nichts loescht."""
    tag = tag or date.today()
    with speicher.verbindung() as c:
        c.execute("UPDATE firma.arbeitszeit SET feierabend = NULL, erfasst = now() "
                  "WHERE tag = %s", (tag,))
    return erfassen(feierabend, tag=tag) if feierabend else f"{tag}: Zeit geloescht."


def bilanz_text(tage: int = 14) -> str:
    with speicher.verbindung() as c:
        cur = c.execute(
            "SELECT tag, feierabend, notiz FROM firma.arbeitszeit "
            "WHERE tag > current_date - %s ORDER BY tag DESC", (tage,))
        zeilen = cur.fetchall()
    if not zeilen:
        return "Noch keine Arbeitszeiten erfasst."
    aus = [f"Arbeitsende der letzten {tage} Tage:"]
    for tag, fa, notiz in zeilen:
        aus.append(f"  {tag:%a %d.%m.}  " + (f"{fa:%H:%M}" if fa else "— keine Antwort")
                   + (f"   ({notiz[:40]})" if notiz else ""))
    return "\n".join(aus)


if __name__ == "__main__":
    print(bilanz_text())
