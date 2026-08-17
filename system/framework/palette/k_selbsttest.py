#!/usr/bin/env python3
"""k_selbsttest.py — prueft den RAHMEN, nicht den Code der Firma.

Diese Kategorie laeuft als erste, weil ein kaputter Laeufer sonst als
bestandener Lauf durchginge. Ein gruener Balken, hinter dem nichts gemessen
wurde, ist schaedlicher als ein roter: Der rote fuehrt zu einer Untersuchung,
der gruene zu einem Rollout.

Genau dieser Fehler ist der Firma schon einmal passiert -- die Einmal-Sperre
(einmalig.py) war wochenlang wirkungslos, weil der Aufrufer die Ausnahme
abfing. Nichts war rot. Es wurde nur nichts gesperrt.
"""
from __future__ import annotations

import sys
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

import dispatcher                                              # noqa: E402
import speicher                                                # noqa: E402


def schema_vorhanden() -> tuple[bool, str, dict]:
    """Liegen die drei Tabellen des Laufs wirklich in der Datenbank?

    Ohne sie schriebe der Laeufer ins Leere -- und weil er seine Befunde dort
    ablegt, waere ein Lauf ohne Schema ein Lauf ohne Gedaechtnis.
    """
    noetig = {"palette_lauf", "palette_ergebnis", "palette_datei"}
    with speicher.verbindung() as c:
        da = {z[0] for z in c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'firma'").fetchall()}
    fehlt = noetig - da
    return (not fehlt,
            "alle Tabellen vorhanden" if not fehlt else f"fehlt: {', '.join(sorted(fehlt))}",
            {"tabellen_gefunden": len(noetig - fehlt)})


def knoten_erkannt() -> tuple[bool, str, dict]:
    """Weiss dieser Rechner, wer er im Inventar ist?

    Rechnername und Knotenname sind verschieden. Faellt die Erkennung auf den
    Rechnernamen zurueck, laufen alle Messwerte unter einer Herkunft, die es im
    Inventar nicht gibt -- und werden damit unvergleichbar, ohne dass es
    auffaellt.
    """
    hier = dispatcher.eigener_knoten()
    bekannt = hier in dispatcher.inventar()
    return (bekannt,
            f"Knoten '{hier}' {'im Inventar' if bekannt else 'NICHT im Inventar'}",
            {"knoten": hier})


# TODO(human): dritter Selbsttest — der Fehlschlag-Weg
#
# Die beiden Tests oben pruefen, ob der Rahmen laeuft. Ungeprueft bleibt der
# Weg, der bei einem Fehlschlag beschritten wird: Ergebnis speichern, Vorgang
# fuer Technik anlegen, Haerte auswerten. Der wird nur betreten, wenn wirklich
# etwas fehlschlaegt -- und genau deshalb ist er der am wenigsten erprobte Teil
# des Laeufers.
#
# Schreibe eine Funktion `probe()` mit derselben Signatur wie die beiden oben:
#     def probe() -> tuple[bool, str, dict]:
#         return bestanden, befund, messwert
# und trage sie unten in TESTS ein.
#
# Zu entscheiden ist, WANN sie fehlschlaegt. Denk daran: Sie laeuft bei JEDEM
# Lauf mit.


TESTS = [
    {"name": "schema_vorhanden", "lauf": schema_vorhanden},
    {"name": "knoten_erkannt", "lauf": knoten_erkannt},
]
