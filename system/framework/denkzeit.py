#!/usr/bin/env python3
"""denkzeit.py — Abbrechen, wenn nichts mehr passiert. Nicht, wenn es lange dauert.

Bisher stand ueber jedem Modell-Aufruf ein Gesamt-Timeout: 120 s im Agenten,
600 s im Broker. Das bestraft die falsche Sache. Auf dieser Maschine laeuft
qwen3.6:35b-a3b (23,9 GB) mit CPU-Offload auf 16 GB VRAM — eine gruendliche
Antwort *dauert*, und der Broker arbeitet seriell, wer wartet, wartet auch auf
die Vordermaenner. Ein pauschales Limit killt genau den langen Gedankengang, den
man haben wollte, und laesst zugleich einen haengenden Prozess bis zum Ende
stehen.

des Chefs Vorgabe: abbrechen nur bei **Festfahren**, **Gruebeln** (Gedankenschleife,
die keinem Ziel mehr folgt) oder einem **Prozess ohne Rueckmeldung**. Lange
Gedankengaenge und intensive Recherche bleiben erlaubt.

Daraus folgen drei Wachhunde statt einer Stoppuhr:

  1. STILLSTAND  Kein neues Token seit `stall_s`. Das ist das Festfahren: der
                 Prozess antwortet nicht mehr. Solange Token fliessen — auch
                 Denk-Token in <think> — laeuft die Uhr immer wieder neu an.
  2. GRUEBELN    Derselbe Textabschnitt taucht wieder und wieder auf. Eine
                 Schleife, die kein Ziel mehr verfolgt, erzeugt Text, aber keinen
                 Fortschritt — Stillstand allein erkennt das nicht.
  3. NOTBREMSE   Absolute Obergrenze `max_s`, sehr hoch angesetzt. Letztes Netz
                 gegen Faelle, die weder still stehen noch sich wiederholen.

**Bei jedem Abbruch wird der bis dahin erzeugte Text zurueckgegeben**, versehen
mit einer Notiz. Ein abgebrochener Gedanke ist mehr wert als eine leere
Fehlermeldung — die Arbeit von zehn Minuten soll nicht verschwinden, nur weil
das Ende fehlt.
"""
from __future__ import annotations

import json
import time

import requests

# Voreinstellungen. Bewusst grosszuegig: sie sollen Haenger fangen, nicht Denken.
# WARUM DIE LADEPHASE IHRE EIGENE FRIST BRAUCHT (16.08.2026)
# Bis hierher galt eine einzige Frist ab dem Absenden. Das bestrafte einen Fall,
# der gar kein Haenger ist: Bevor das erste Token kommt, muss Ollama das Modell
# laden -- 23,9 GB von der Platte, mit CPU-Offload. Waehrenddessen gibt es
# prinzipbedingt kein Lebenszeichen. Die Testpalette brach deshalb ab, bevor das
# Modell ueberhaupt bereit war, und meldete "Stillstand" fuer etwas, das
# ordnungsgemaess arbeitete.
#
# Getrennt gemessen wird jetzt:
#   LADE_S   bis zum ERSTEN Token -- grosszuegig, hier wird geladen
#   STALL_S  zwischen zwei Token -- enger, hier wird gedacht
# Ein Modell, das nach 20 Minuten noch laedt, ist normal; eines, das mitten im
# Satz 10 Minuten schweigt, steht.
LADE_S = 1800        # 30 min bis zum ersten Token -> Modell laedt
STALL_S = 600        # 10 min ohne ein Token, NACHDEM es lief -> der Prozess steht
MAX_S = 14400        # 4 h absolute Obergrenze -> Notbremse
GRUEBEL_FENSTER = 200   # Zeichen, die als "Abschnitt" gelten
GRUEBEL_MAX = 4         # so oft darf derselbe Abschnitt auftauchen
# Erst ab dieser Laenge ueberhaupt auf Schleifen pruefen. Im Zweifel weiterlaufen
# lassen: eine faelschlich abgebrochene Recherche kostet mehr als eine Schleife,
# die zehn Sekunden laenger dreht. Echte Gruebelschleifen werden ohnehin lang.
GRUEBEL_AB_ZEICHEN = 1500


class Abbruch(Exception):
    """Traegt den Teiltext mit — er geht nicht verloren."""

    def __init__(self, grund: str, teiltext: str):
        super().__init__(grund)
        self.grund = grund
        self.teiltext = teiltext


def _gruebelt(text: str, fenster: int, maximal: int) -> bool:
    """Wiederholt sich der letzte Abschnitt schon zu oft im bisherigen Text?

    Bewusst simpel und ohne Modell: Diese Pruefung laeuft im Token-Takt, sie muss
    billig sein. Ein LLM zur Ueberwachung eines LLM waere derselbe Zirkelschluss,
    den passung.py ausdruecklich vermeidet.
    """
    if len(text) < max(GRUEBEL_AB_ZEICHEN, fenster * (maximal + 1)):
        return False
    letzter = text[-fenster:]
    if not letzter.strip():
        return False
    return text.count(letzter) > maximal


def chat(url: str, payload: dict, *, stall_s: int = STALL_S, max_s: int = MAX_S,
         lade_s: int = LADE_S,
         gruebel_fenster: int = GRUEBEL_FENSTER, gruebel_max: int = GRUEBEL_MAX,
         log=None) -> dict:
    """Wie ein /api/chat-Aufruf mit stream=False — nur mit den drei Wachhunden.

    Rueckgabe ist die uebliche `message`-Struktur (role/content und ggf.
    tool_calls), damit Aufrufer nichts weiter anpassen muessen.

    `stall_s` wird als **Read-Timeout** an requests gegeben: er gilt zwischen
    zwei Chunks, nicht fuer die Gesamtdauer. Genau das ist der Unterschied.
    """
    payload = {**payload, "stream": True}
    start = time.monotonic()
    inhalt: list[str] = []
    message: dict = {"role": "assistant"}
    tool_calls: list = []

    def bisher() -> str:
        return "".join(inhalt)

    def melde(t: str) -> None:
        if log:
            log(t)

    # Der Read-Timeout von requests laesst sich mitten im Stream nicht mehr
    # aendern. Er wird deshalb auf die GROESSERE der beiden Fristen gesetzt --
    # die Ladephase muss hindurchpassen -- und der engere Stillstand nach dem
    # ersten Token danach selbst gemessen.
    letzte_regung = start
    erstes_token = False

    try:
        r = requests.post(url, json=payload, stream=True,
                          timeout=(15, max(lade_s, stall_s)))
        r.raise_for_status()
        for zeile in r.iter_lines(decode_unicode=True):
            jetzt = time.monotonic()
            # Auch eine leere Zeile ist ein Lebenszeichen: Der Server sendet,
            # also haengt er nicht.
            if erstes_token and jetzt - letzte_regung > stall_s:
                melde(f"Abbruch: {stall_s}s ohne Token nach dem Anlaufen")
                raise Abbruch(
                    f"Stillstand — {stall_s // 60} Minuten kein Token, nachdem das "
                    f"Modell bereits geantwortet hatte", bisher())
            letzte_regung = jetzt
            if not zeile:
                continue
            erstes_token = True
            try:
                d = json.loads(zeile)
            except json.JSONDecodeError:
                continue
            if d.get("error"):
                raise Abbruch(f"Modellfehler: {d['error']}", bisher())

            m = d.get("message") or {}
            if m.get("content"):
                inhalt.append(m["content"])
            if m.get("tool_calls"):
                tool_calls.extend(m["tool_calls"])
            if m.get("role"):
                message["role"] = m["role"]

            if _gruebelt(bisher(), gruebel_fenster, gruebel_max):
                melde("Abbruch: Gedankenschleife erkannt")
                raise Abbruch("Gedankenschleife — derselbe Abschnitt wiederholt sich, "
                              "ohne dass die Antwort vorankommt", bisher())
            if time.monotonic() - start > max_s:
                melde(f"Abbruch: Notbremse nach {max_s}s")
                raise Abbruch(f"Notbremse nach {max_s // 60} Minuten Gesamtdauer", bisher())

            if d.get("done"):
                break
    except requests.exceptions.ReadTimeout:
        # Unterscheidbar machen: Wer nie ein Token gesehen hat, hat auf das
        # LADEN gewartet -- eine andere Diagnose als ein Haenger mittendrin.
        if not erstes_token:
            melde(f"Abbruch: {lade_s}s ohne erstes Token")
            raise Abbruch(
                f"Modell wurde in {lade_s // 60} Minuten nicht bereit — es laedt "
                f"noch, oder der Arbeitsspeicher reicht nicht", bisher()) from None
        melde(f"Abbruch: {stall_s}s ohne Lebenszeichen")
        raise Abbruch(f"Stillstand — {stall_s} Sekunden kein Lebenszeichen vom Modell",
                      bisher()) from None
    except requests.exceptions.RequestException as e:
        raise Abbruch(f"Verbindung zum Modell: {e}", bisher()) from None

    message["content"] = bisher()
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def mit_notiz(teiltext: str, grund: str) -> str:
    """Teiltext plus Hinweis — nie eine leere Antwort zurueckgeben."""
    teiltext = (teiltext or "").strip()
    if not teiltext:
        return f"(abgebrochen: {grund})"
    return f"{teiltext}\n\n_(abgebrochen: {grund})_"
