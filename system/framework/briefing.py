#!/usr/bin/env python3
"""Morgen-Briefing und Nachtstau (K2, K22, K23, K25).

Zwei Dinge, die zusammengehören:

1. **Nachtstau** — was während der Nachtruhe anfällt, wird nicht zugestellt,
   sondern hier gesammelt (nur der Termin-Ruf bricht durch, siehe ruhezeit.py).
2. **Briefing** — Assistenz liefert EINE Nachricht kurz nach Ende der Ruhezeit:
   Termine aus dem Kalender, Zulieferungen der Kollegen, und der Nachtstau
   gebündelt statt einzeln nachgeliefert.

Assistenz formuliert es in ihrem Ton; die Rohdaten kommen aus den vorhandenen
Werkzeugen (caldav_tool) und von Buchhalters Broker (/praesenz, /status).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

FW = Path(__file__).resolve().parent
STAU = Path.home() / ".local/state/firma-mitarbeiter/nachtstau.json"


# --- Nachtstau -----------------------------------------------------------
def stau_anhaengen(absender: str, text: str) -> None:
    """Eine Meldung zurückstellen, statt sie nachts zuzustellen."""
    try:
        eintraege = json.loads(STAU.read_text(encoding="utf-8"))
    except Exception:
        eintraege = []
    eintraege.append({"zeit": time.strftime("%H:%M"), "von": absender, "text": text})
    STAU.parent.mkdir(parents=True, exist_ok=True)
    STAU.write_text(json.dumps(eintraege, ensure_ascii=False, indent=1), encoding="utf-8")


def stau_lesen() -> list[dict]:
    try:
        return json.loads(STAU.read_text(encoding="utf-8"))
    except Exception:
        return []


def stau_leeren() -> None:
    STAU.unlink(missing_ok=True)


# --- Zulieferungen einsammeln --------------------------------------------
def teil_kalender(tage: int = 1) -> str:
    try:
        import sys
        sys.path.insert(0, str(FW / "tools"))
        import caldav_tool
        return caldav_tool.briefing_text(tage)
    except Exception as e:  # noqa: BLE001
        return f"(Kalender nicht erreichbar: {e})"


def teil_firma(broker_url: str) -> str:
    """Buchhalters Zulieferung: wer lebt, was lief, was kostet."""
    import urllib.request
    zeilen = []
    for pfad, titel in (("/praesenz", "Belegschaft"), ("/status", "Betrieb")):
        try:
            # 10 s waren zu knapp: der Broker bedient eine Anfrage nach der anderen,
            # eine laufende Modellantwort blockiert die Zulieferung.
            with urllib.request.urlopen(f"{broker_url.rstrip('/')}{pfad}", timeout=120) as r:
                d = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            zeilen.append(f"{titel}: nicht erreichbar ({e})")
            continue
        if pfad == "/praesenz":
            tot = [a for a, v in d.get("agenten", {}).items() if not v.get("lebt")]
            zeilen.append(f"{titel}: {len(d.get('agenten', {}))} gemeldet"
                          + (f", ausgefallen: {', '.join(tot)}" if tot else ", alle da"))
        else:
            zeilen.append(f"{titel}: {d.get('erledigt', 0)} Aufgaben erledigt, "
                          f"Warteschlange {d.get('queue', 0)}")
    return "\n".join(zeilen)


def teil_konto() -> str:
    """Das Geschaeftskonto im Briefing (K3: monatlich, ohne neuen Dienst).

    WARUM DIE ERINNERUNG HIER STEHT UND NICHT IN EINEM TIMER
    Buchhalter hat keinen Zugriff auf das Konto -- er braucht einen Anstoss von
    Chef, sonst rechnet er mit altem Stand. Und ein alter Stand, der wie ein
    aktueller aussieht, ist schlimmer als keiner. Das Briefing erreicht Chef
    ohnehin jeden Morgen; ein eigener Dienst waere ein zweiter Ort, an dem etwas
    schiefgehen kann, fuer denselben Satz.

    Gemeldet wird nur, wenn es etwas zu melden gibt: Ein Briefing, das jeden Tag
    dieselbe Zeile traegt, wird ueberlesen -- und dann auch an dem Tag, an dem
    sie sich aendert.
    """
    try:
        import konto
        l = konto.lage()
    except Exception as e:                                     # noqa: BLE001
        return f"Konto: nicht lesbar ({e})"
    if not l:
        return "Konto: noch kein Stand erfasst"

    zeilen = [f"Konto: {l['gesamt_eur']:.2f} EUR gesamt "
              f"({l['liquide_eur']:.2f} liquide), Stand vom {l['stichtag']}"]
    # Nur bei Handlungsbedarf mehr sagen.
    if l["veraltet"]:
        zeilen.append(f"  → Stand ist {l['alter_tage']} Tage alt und gilt als "
                      f"VERALTET. Bitte einen neuen Auszug exportieren, sonst "
                      f"rechnet Buchhalter mit Zahlen von vorgestern.")
    urteil = konto.bewertung(l)
    if not urteil.startswith("Im Toleranzband"):
        zeilen.append(f"  → {urteil}")
    g = konto.gebuehren_bilanz()
    if g and g["orders"] and (g["quote_gesamt"] or 0) > (l["max_gebuehrenquote"] or 1):
        zeilen.append(f"  → Gebuehren bisher {g['gebuehren_eur']:.2f} EUR auf "
                      f"{g['volumen_eur']:.2f} EUR Volumen "
                      f"({(g['quote_gesamt'] or 0) * 100:.1f} %) — ueber der "
                      f"Grenze von {(l['max_gebuehrenquote'] or 0) * 100:.2f} %.")
    return "\n".join(zeilen)


def zaehle_termine(tage: int = 1) -> int:
    try:
        import sys
        sys.path.insert(0, str(FW / "tools"))
        import caldav_tool
        return len(caldav_tool.list_events(tage))
    except Exception:
        return -1                       # -1 = nicht ermittelbar, nicht "keine"


def ueberblick(broker_url: str, tage: int = 1) -> str:
    """Die Zahlen-Zeile — im Code gerechnet, nicht vom Modell geschätzt.

    Ein Sprachmodell zählt nicht zuverlässig und übernimmt Beispielzahlen aus dem
    Auftrag als Ergebnis (im ersten Testlauf genau so passiert). Was man ausrechnen
    kann, rechnet man aus."""
    import urllib.request
    teile = []
    n = zaehle_termine(tage)
    teile.append("Termine unbekannt" if n < 0 else
                 "keine Termine" if n == 0 else f"{n} Termin{'e' if n > 1 else ''}")
    try:
        with urllib.request.urlopen(f"{broker_url.rstrip('/')}/praesenz", timeout=10) as r:
            agenten = json.loads(r.read()).get("agenten", {})
        tot = [a for a, v in agenten.items() if not v.get("lebt")]
        teile.append("keine Störung" if not tot else
                     f"{len(tot)} Ausfall" + ("" if len(tot) == 1 else "e"))
    except Exception:
        teile.append("Betriebslage unbekannt")
    stau = len(stau_lesen())
    teile.append("nichts über Nacht" if stau == 0 else
                 f"{stau} Meldung{'en' if stau > 1 else ''} über Nacht")
    return " · ".join(teile)


def teil_stau() -> str:
    eintraege = stau_lesen()
    if not eintraege:
        return ""
    return "\n".join(f"{e['zeit']} {e['von']}: {e['text']}" for e in eintraege)


# --- Der Auftrag an Assistenz ------------------------------------------------
def briefing_auftrag(kalender: str, firma: str, stau: str, ueberblick_zeile: str = "") -> str:
    """Was Assistenz aus den Rohdaten machen soll — ihr Arbeitsauftrag für das Briefing.

    Die Rohdaten stehen als Blöcke bereit; dieser Text bestimmt, was daraus wird:
    Aufbau, Reihenfolge, Länge und der Umgang mit leeren Abschnitten."""
    rohdaten = (f"ÜBERBLICK (wörtlich zu übernehmen):\n{ueberblick_zeile}\n\n"
                f"TERMINE HEUTE:\n{kalender or '(keine)'}\n\n"
                f"STAND DER FIRMA:\n{firma or '(nichts zu melden)'}\n\n"
                f"WÄHREND DER NACHT AUFGELAUFEN:\n{stau or '(nichts)'}")
    auftrag = (
        "MORGEN-BRIEFING — schreibe Chef die erste Nachricht des Tages. Aufbau in "
        "genau dieser Reihenfolge:\n"
        "1. Eine kurze Begrüßung. Ein Satz, in deinem Ton.\n"
        "2. Die ÜBERBLICK-Zeile aus den Rohdaten, WÖRTLICH und unverändert. Ändere "
        "keine Zahl, erfinde keine hinzu, lass keine weg.\n"
        "3. DETAILS: danach die Einzelheiten zu genau den Punkten aus dem Überblick, "
        "in derselben Reihenfolge. Was im Überblick eine Null war, wird hier nicht "
        "wiederholt.\n\n"
        "Regeln: Termine mit Uhrzeit zuerst. Störungen vor Terminen, wenn es welche "
        "gibt. Alle Zahlen und Fakten stammen ausschließlich aus den Rohdaten — steht "
        "etwas nicht dort, gibt es das nicht. Keine Nachfragen, keine Aufgabenliste am "
        "Ende, kein Schlusswort. Insgesamt höchstens zehn Sätze."
    )
    return auftrag + "\n\n" + rohdaten
