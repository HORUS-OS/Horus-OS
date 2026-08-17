#!/usr/bin/env python3
"""Vorgangsakte — das Gedächtnis für angefangene Arbeit (K60/K66/K67).

Agenten sind ereignisgetrieben: Wer einen Kollegen fragt, ist mit seiner Aufgabe
fertig, bevor die Antwort kommt — und wüsste beim Eintreffen nicht mehr, wofür er
gefragt hat. Diese Datei hält den Faden fest.

Bewusst getrennt von personalakte.json: Stammdaten bleiben stabil, Laufendes
ändert sich ständig. Ablage: system/mitarbeiter/<agent_id>/vorgaenge.json

Fristen (des Chefs Regel): nach 30 Minuten ohne Antwort einmal nachfassen, bleibt es
still, an den Chef melden und als gescheitert schließen.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import speicher

MITARBEITER = Path(__file__).resolve().parent.parent
FRIST_NACHFASSEN = 30 * 60      # 30 Min bis zur Erinnerung
FRIST_AUFGEBEN = 60 * 60        # danach nochmal 30 Min bis zum Scheitern


def _pfad(agent_id: str) -> Path:
    return MITARBEITER / agent_id / "vorgaenge.json"


def laden(agent_id: str) -> list[dict]:
    """Offene Vorgaenge. Quelle ist Postgres, solange der Schalter steht —
    sonst die JSON-Datei wie bisher (siehe speicher.aktiv())."""
    if speicher.aktiv():
        return speicher.offene_laden(agent_id)
    try:
        return json.loads(_pfad(agent_id).read_text(encoding="utf-8")).get("offen", [])
    except Exception:
        return []


def speichern(agent_id: str, offen: list[dict]) -> None:
    """Nur noch fuer den JSON-Weg. Unter Postgres schreibt jede Operation ihre
    eigene Zeile — eine ganze Liste zurueckzuschreiben waere dort ein Rueckschritt
    (und wuerde parallele Schreiber ueberfahren)."""
    if speicher.aktiv():
        return
    p = _pfad(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"stand": time.strftime("%Y-%m-%d %H:%M"), "offen": offen},
                            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def anlegen(agent_id: str, *, anliegen: str, wartet_auf: str, fuer: str,
            raum: str, **extra) -> dict:
    """Neuen Vorgang eröffnen. `fuer` = wer die Antwort am Ende bekommt.

    `extra` nimmt Zusatzfelder auf, die nur bestimmte Vorgangsarten brauchen —
    aktuell `pn_offen` (die Frage muss dem Kollegen noch als DM zugestellt
    werden). Die Zustellung passiert im Agenten, nicht hier: Werkzeuge laufen im
    Broker-Prozess, der keinen Matrix-Client besitzt. Der Vorgang ist damit das
    Postfach zwischen beiden Prozessen."""
    offen = laden(agent_id)
    v = {"id": f"{int(time.time())}-{len(offen)}", "anliegen": anliegen,
         "wartet_auf": wartet_auf, "fuer": fuer, "raum": raum,
         "eroeffnet": time.time(), "nachgefasst": False, "status": "offen",
         **extra}
    if speicher.aktiv():
        speicher.einfuegen(agent_id, v)
        return v
    offen.append(v)
    speichern(agent_id, offen)
    return v


def aktualisieren(agent_id: str, vorgang_id: str, **felder) -> None:
    """Felder eines offenen Vorgangs ändern (z.B. pn_offen nach dem Versand)."""
    if speicher.aktiv():
        speicher.felder_setzen(agent_id, vorgang_id, felder)
        return
    offen = laden(agent_id)
    for v in offen:
        if v["id"] == vorgang_id:
            v.update(felder)
    speichern(agent_id, offen)


def zuzustellen(agent_id: str) -> list[dict]:
    """Vorgänge, deren Frage noch als PN raus muss."""
    return [v for v in laden(agent_id)
            if v["status"] == "offen" and v.get("pn_offen")]


def _bilanz_pfad(agent_id: str) -> Path:
    return MITARBEITER / agent_id / "bilanz.json"


def bilanz(agent_id: str) -> dict:
    """Laufende Summe geschlossener Vorgaenge nach Status.

    Getrennt von vorgaenge.json, weil dort nur Offenes steht: schliessen()
    entfernt den Vorgang aus der Liste, sonst waere sie nach Wochen unlesbar.
    Ohne diese Summe bliebe „erledigt" in kennzahlen.json dauerhaft auf 0 —
    genau der Zustand, der die Probezeit-Bewertung grundlos machte."""
    if speicher.aktiv():
        return speicher.bilanz_lesen(agent_id)
    try:
        return json.loads(_bilanz_pfad(agent_id).read_text(encoding="utf-8"))
    except Exception:
        return {"seit": time.strftime("%Y-%m-%d"), "nach_status": {}, "dauer_summe_s": 0}


def _bilanz_fortschreiben(agent_id: str, status: str, dauer_s: float) -> None:
    b = bilanz(agent_id)
    b["nach_status"][status] = b["nach_status"].get(status, 0) + 1
    b["dauer_summe_s"] = round(b.get("dauer_summe_s", 0) + max(0.0, dauer_s), 1)
    b["zuletzt"] = time.strftime("%Y-%m-%d %H:%M")
    p = _bilanz_pfad(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(b, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def schliessen(agent_id: str, vorgang_id: str, status: str = "erledigt") -> dict | None:
    if speicher.aktiv():
        # Die Bilanz wird unter Postgres nicht fortgeschrieben, sondern aus den
        # Zeilen berechnet — sie kann so gar nicht erst auseinanderlaufen.
        return speicher.schliessen(agent_id, vorgang_id, status)
    offen = laden(agent_id)
    rest, weg = [], None
    for v in offen:
        if v["id"] == vorgang_id:
            v["status"] = status
            weg = v
        else:
            rest.append(v)
    speichern(agent_id, rest)
    if weg is not None:
        _bilanz_fortschreiben(agent_id, status, time.time() - weg.get("eroeffnet", time.time()))
    return weg


def wartend_auf(agent_id: str, kollege: str) -> dict | None:
    """Ältester offener Vorgang, der auf diesen Kollegen wartet — so findet eine
    eintreffende Antwort ihren Vorgang wieder."""
    treffer = [v for v in laden(agent_id)
               if v["status"] == "offen" and v["wartet_auf"] == kollege]
    return sorted(treffer, key=lambda v: v["eroeffnet"])[0] if treffer else None


def faellig(agent_id: str, jetzt: float | None = None) -> tuple[list[dict], list[dict]]:
    """(nachzufassen, aufzugeben) — beides nach des Chefs Fristen."""
    jetzt = jetzt or time.time()
    nach, auf = [], []
    for v in laden(agent_id):
        if v["status"] != "offen":
            continue
        alter = jetzt - v["eroeffnet"]
        if alter >= FRIST_AUFGEBEN:
            auf.append(v)
        elif alter >= FRIST_NACHFASSEN and not v.get("nachgefasst"):
            nach.append(v)
    return nach, auf


def markiere_nachgefasst(agent_id: str, vorgang_id: str) -> None:
    if speicher.aktiv():
        speicher.felder_setzen(agent_id, vorgang_id, {"nachgefasst": True})
        return
    offen = laden(agent_id)
    for v in offen:
        if v["id"] == vorgang_id:
            v["nachgefasst"] = True
    speichern(agent_id, offen)


if __name__ == "__main__":      # Selbstauskunft: vorgaenge.py [agent_id]
    import sys
    aid = sys.argv[1] if len(sys.argv) > 1 else "projektleitung"
    offen = laden(aid)
    print(f"{aid}: {len(offen)} offene Vorgänge")
    for v in offen:
        alter = int((time.time() - v["eroeffnet"]) // 60)
        print(f"  [{v['id']}] seit {alter} Min – wartet auf {v['wartet_auf']}: {v['anliegen'][:60]}")
