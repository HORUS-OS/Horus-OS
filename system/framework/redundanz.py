#!/usr/bin/env python3
"""Redundanz-Prüfung — verhindert das „Ich packe meinen Koffer"-Bingo.

Im Mehr-Personen-Raum wiederholen kleine Modelle, was schon dasteht: Sie zählen
den Verlauf auf, übernehmen fremde Formulierungen und antworten auf Nachrichten,
die längst erledigt sind. Per Prompt ist das nicht zuverlässig abzustellen —
dieselbe Erfahrung, die schon `saeubere_antwort()` deterministisch gemacht hat.
Deshalb misst diese Datei nach, was das Modell geschrieben hat, bevor es in den
Raum geht.

Zwei Stufen, billig zuerst (des Chefs Vorgabe, dasselbe Muster wie Buchhalters
Kosten-Kaskade):
  1. Exakte Wortfolgen — gemeinsame n-Gramme. Kostet nichts, fängt Nacherzählung.
  2. Embeddings über Archiv' Dienst (:8901) — erkennt dieselbe Aussage in
     anderen Worten. Fällt der Dienst aus, entscheidet Stufe 1 allein: Ein
     stummer Raum wäre schlimmer als eine Antwort zu viel.

Gemessen wird satzweise, entschieden absatzweise: Ein einzelner wiederholter
Gruß soll nicht die Sachaussage daneben mitreißen.

Die Schwellen unten sind Startwerte. Sie werden NICHT geraten, sondern im
Trockenlauf an echten Raumverläufen kalibriert (siehe trockenlauf.py).
"""
from __future__ import annotations

import json
import re
import urllib.request

EMBED_URL = "http://127.0.0.1:8901/embed"

# --- Kalibrierbare Schwellen ------------------------------------------------
# Startwerte, vom Trockenlauf zu bestätigen oder zu korrigieren.
NGRAMM = 6              # ab wie vielen Wörtern eine gemeinsame Folge zählt
SCHWELLE_EXAKT = 0.35   # ab hier gilt ein Satz als wörtlich nacherzählt
SCHWELLE_EMBED = 0.82   # Kosinus-Ähnlichkeit, ab der dieselbe Aussage vorliegt
ZITAT_NGRAMM = 8        # fremde Wortfolge ab dieser Länge braucht Kennzeichnung
ANTEIL_KRITISCH = 0.35  # ab diesem Gewichtsanteil gilt ein Absatz/eine Antwort als verbraucht

_WORT = re.compile(r"[^\wäöüßÄÖÜ]+")
_SATZ_ENDE = re.compile(r"(?<=[.!?…])\s+|\n+")
_ABSATZ = re.compile(r"\n\s*\n")
# Matrix-Zitat: der Client rendert '> …' als fremde Rede. Nur diese Form gilt
# als Kennzeichnung — Anführungszeichen nicht, weil Assistenz sie für Betonungen
# und wörtliche Rede in seinen Geschichten benutzt (im Grilling entschieden).
_ZITATZEILE = re.compile(r"^\s*>.*$", re.M)

# Reine Höflichkeitsfloskeln — Anrede, Gruß, Verabschiedung. Sie werden aus der
# Redundanz-Messung ausgenommen, weil sie naturgemäß in jeder Nachricht gleich
# lauten und sonst kurze Antworten kippen würden ("Hallo Chef! Der Zug fällt
# aus." wäre nach Gewicht zu 60% Wiederholung).
#
# ACHTUNG, enge Auslegung: Das schützt NICHT Assistenzs Marotten und Reime — des Chefs
# Entscheidung dazu war „wirklich alles gleich streng, er muss sich eben neue
# Sprüche ausdenken". Ausgenommen ist nur, was reine Gesprächsform ohne Inhalt
# ist. Ein Satz zählt schon dann wieder mit, wenn er über die Floskel hinaus
# etwas aussagt.
# Wortmenge statt Satzmuster: Grüße kombinieren sich frei („Hallo Chef, schön
# dich zu sehen!"), und jedes feste Muster geht an der nächsten Variante vorbei.
# Ein Satz gilt als Floskel, wenn ALLE seine Wörter hier drinstehen — sobald ein
# einziges Sachwort auftaucht, zählt er wieder voll mit.
_FLOSKEL_WOERTER = {
    "hallo", "hi", "hey", "moin", "servus", "grüß", "gruess", "gruß", "gruss",
    "grüße", "grüsse", "gruesse", "grüßt", "guten", "gute", "morgen", "tag",
    "abend", "nacht", "willkommen", "herzlich", "herzlichen", "schön", "schoen",
    "schönen", "freut", "freue", "mich", "dich", "dir", "euch", "sie", "ihnen",
    "zu", "sehen", "wiederzusehen", "hören", "lesen", "viele", "liebe", "beste",
    "bis", "später", "spaeter", "bald", "dann", "gleich", "wiedersehen",
    "tschüss", "tschuess", "ciao", "danke", "dank", "vielen", "sehr", "gern",
    "gerne", "geschehen", "bitte", "na", "ja", "alles", "klar", "wie", "geht",
    "gehts", "es", "und", "auch", "noch", "wieder", "chef", "zusammen",
    "allerseits", "miteinander", "leute", "ihr", "du", "der", "die", "das",
}


def ist_beauftragung(satz: str, kollegen: list[str]) -> bool:
    """Wendet sich dieser Satz mit einer Bitte an eine namentlich genannte Person?

    Beauftragungen werden von der Redundanzprüfung ausgenommen (Chef: „nur die
    Beauftragung durchlassen"). Grund: Wer einen Kollegen um etwas bittet, sagt
    zwangsläufig noch einmal, worum es geht — sonst wäre der Auftrag unvollständig.
    Diese Wiederholung ist Funktion, nicht Geschwätz.

    Dass DERSELBE Auftrag mehrfach erteilt wird (im Trockenlauf fragten Projektleitung und
    Buchhalter beide Archiv nach denselben Backups), verhindert nicht diese
    Prüfung, sondern die Vorgangsbindung: Wer sieht, dass zu einem Anliegen schon
    ein Vorgang offen ist, fragt nicht noch einmal. Siehe vorgaenge.py."""
    ws = worte(satz)
    if not ws:
        return False
    if not any(k.lower() in ws for k in kollegen):
        return False
    return satz.rstrip().endswith("?") or any(
        w in ws for w in ("kannst", "könntest", "bitte", "schau", "sag", "sieh",
                          "prüf", "prüfe", "frag", "hol", "gib", "melde"))


def ist_floskel(satz: str) -> bool:
    """Reine Höflichkeitsform ohne Sachgehalt?

    Eng ausgelegt: Das nimmt NUR Gruß, Dank und Verabschiedung aus der Messung.
    Assistenzs Marotten und Reime schützt es ausdrücklich nicht — des Chefs Entscheidung
    dazu war „wirklich alles gleich streng, er muss sich eben neue Sprüche
    ausdenken". Ein einziges Sachwort genügt, und der Satz zählt wieder mit."""
    ws = worte(satz)
    return bool(ws) and all(w in _FLOSKEL_WOERTER for w in ws)


def worte(text: str) -> list[str]:
    """Text auf vergleichbare Wörter herunterbrechen (Kleinschreibung, ohne
    Satzzeichen). Umlaute bleiben erhalten — 'für' und 'fur' sind nicht dasselbe
    Wort, und die Agenten schreiben durchgängig korrektes Deutsch."""
    return [w for w in _WORT.split(text.lower()) if w]


def saetze(text: str) -> list[str]:
    return [s.strip() for s in _SATZ_ENDE.split(text) if s.strip()]


def absaetze(text: str) -> list[str]:
    return [a.strip() for a in _ABSATZ.split(text) if a.strip()]


def _ngramme(ws: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


# --- Stufe 1: exakte Wortfolgen --------------------------------------------
def ueberlappung(satz: str, fremd: str, n: int = NGRAMM) -> float:
    """Anteil der Wortfolgen aus `satz`, die auch in `fremd` vorkommen.

    Bewusst asymmetrisch: Uns interessiert, wie viel des NEUEN Satzes schon
    dastand — nicht, wie viel des alten Textes wiederkehrt. Ein kurzer Satz,
    der vollständig in einem langen Vorbeitrag steckt, ist Wiederholung; der
    umgekehrte Fall ist es nicht."""
    a = _ngramme(worte(satz), n)
    if not a:
        return 0.0
    b = _ngramme(worte(fremd), n)
    return len(a & b) / len(a)


def max_ueberlappung(satz: str, fremdtexte: list[str]) -> float:
    return max((ueberlappung(satz, f) for f in fremdtexte), default=0.0)


# --- Stufe 2: Embeddings ----------------------------------------------------
def _embed(texte: list[str]) -> list[list[float]] | None:
    """Vektoren von Archiv' Dienst holen. None = Dienst nicht verfügbar.

    Kein Fehler nach oben: Der Ausfall ist ein erwarteter Zustand, auf den die
    Kaskade mit 'Stufe 1 entscheidet allein' reagiert."""
    try:
        data = json.dumps({"texte": texte}).encode()
        req = urllib.request.Request(EMBED_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("vektoren")
    except Exception:  # noqa: BLE001
        return None


def _kosinus(a: list[float], b: list[float]) -> float:
    """Reines Skalarprodukt — der Dienst liefert bereits normalisierte Vektoren
    (`normalize_embeddings=True`), eine erneute Normierung wäre verschwendet."""
    return sum(x * y for x, y in zip(a, b))


def aehnlichkeiten(saetze_neu: list[str], fremdtexte: list[str]) -> list[float] | None:
    """Je Satz die höchste Bedeutungsähnlichkeit zu einem Vorbeitrag.

    Ein einziger Dienst-Aufruf für alles — jina-clip-v2 läuft auf der CPU, und
    seit dieser Prüfung fragen alle vier Agenten dort an statt nur Archiv."""
    if not saetze_neu or not fremdtexte:
        return None
    v = _embed(saetze_neu + fremdtexte)
    if not v or len(v) != len(saetze_neu) + len(fremdtexte):
        return None
    neu, alt = v[:len(saetze_neu)], v[len(saetze_neu):]
    return [max((_kosinus(x, y) for y in alt), default=0.0) for x in neu]


# --- Zitat-Härtung ----------------------------------------------------------
def zitat_verstoss(entwurf: str, fremdtexte: list[str],
                   chef_texte: list[str] | None = None) -> str | None:
    """Übernimmt der Entwurf fremde Formulierungen ohne Matrix-Zitat?

    des Chefs Regel: „Er darf vor allem nicht im Namen eines anderen sprechen.
    Zitate müssen als solche gekennzeichnet werden." Ungekennzeichnete Übernahme
    kostet die ganze Antwort — deshalb wird nur die eindeutige Form (Matrix-Zitat)
    als Kennzeichnung akzeptiert und die Wortfolge muss lang genug sein, damit
    geteilte Fachbegriffe nicht auslösen.

    Rückgabe: die beanstandete Wortfolge (für das Log) oder None."""
    ungezeichnet = _ZITATZEILE.sub("", entwurf)   # zitierte Zeilen ausnehmen
    eigen = _ngramme(worte(ungezeichnet), ZITAT_NGRAMM)
    # Was Chef gesagt hat, darf weitergereicht werden — auch dann noch, wenn ein
    # Kollege es vor uns weitergereicht hat und es deshalb inzwischen auch in
    # einem Agenten-Beitrag steht. Deshalb wird hier abgezogen statt nur
    # übersprungen.
    for c in (chef_texte or []):
        eigen -= _ngramme(worte(c), ZITAT_NGRAMM)
    if not eigen:
        return None
    for f in fremdtexte:
        treffer = eigen & _ngramme(worte(f), ZITAT_NGRAMM)
        if treffer:
            return " ".join(sorted(treffer)[0])
    return None


def markiere_zitat(entwurf: str, stelle: str) -> str | None:
    """Die übernommene Passage nachträglich als Matrix-Zitat auszeichnen.

    des Chefs Weg durch die Umgewöhnungsphase: Statt die Antwort zu verwerfen, wird
    aus der ungekennzeichneten Übernahme das, was sie hätte sein sollen — ein
    gekennzeichnetes Zitat. Der Beitrag geht raus, die Regel bleibt sichtbar
    verletzt, und im Raum steht transparent, dass hier fremde Worte stehen.

    Der betroffene SATZ wird zur '> '-Zeile, nicht nur die Wortfolge: Ein aus dem
    Satz herausgeschnittenes Fragment ergäbe grammatisch Unsinn.

    Rückgabe: der umgebaute Text, oder None wenn der Satz nicht auffindbar ist
    (dann bleibt es beim Verwerfen)."""
    ziel = worte(stelle)
    if not ziel:
        return None

    def enthaelt(text: str) -> bool:
        """Steckt die Zielfolge zusammenhängend in diesem Text?"""
        ws = worte(text)
        return any(ws[i:i + len(ziel)] == ziel
                   for i in range(len(ws) - len(ziel) + 1))

    def baue(satzweise: bool) -> str | None:
        """satzweise=True markiert nur den betroffenen Satz (fein), sonst die
        ganze Zeile (grob, aber trifft satzübergreifende Übernahmen).

        Beides wird gebraucht: Im Livebetrieb verteilte sich „bin buchhalter der
        buchhalter ich überwache die konten" auf zwei Sätze. Satzweise fand die
        Suche dort nichts, zeilenweise sehr wohl — und ohne Treffer fiele die
        Kennzeichnung aus, obwohl der Verstoß eindeutig ist."""
        umgebaut, getroffen = [], False
        for zeile in entwurf.split("\n"):
            if not zeile.strip() or zeile.lstrip().startswith(">"):
                umgebaut.append(zeile)
                continue
            if not satzweise:
                if enthaelt(zeile):
                    getroffen = True
                    umgebaut.append("> " + zeile.strip())
                else:
                    umgebaut.append(zeile)
                continue
            neue = []
            for satz in saetze(zeile) or [zeile]:
                if enthaelt(satz):
                    getroffen = True
                    neue.append("\n> " + satz.strip())
                else:
                    neue.append(satz)
            umgebaut.append(" ".join(n if n.startswith("\n>") else n.strip()
                                     for n in neue).strip())
        return "\n".join(umgebaut) if getroffen else None

    # Erst fein (nur der betroffene Satz), sonst grob (die ganze Zeile).
    text = baue(True) or baue(False)
    if text is None:
        return None
    # Zitatzeilen sauber trennen: '> ' muss am Zeilenanfang stehen, und was
    # danach kommt, gehört wieder in eine eigene Zeile.
    text = re.sub(r"\n>\s*", "\n> ", text)
    return re.sub(r"(^> .*?)(?<=[.!?…])\s+(?=\S)", r"\1\n", text, flags=re.M).strip()


# --- Urteil -----------------------------------------------------------------
class Urteil:
    """Ergebnis einer Prüfung — trägt auch mit, WARUM, damit der Logeintrag
    aussagekräftig ist und die Schwelle nachjustiert werden kann."""

    def __init__(self, redundant: bool, grund: str, werte: list[float],
                 stufe: str, ueberschneidung: str = ""):
        self.redundant = redundant
        self.grund = grund
        self.werte = werte                    # je Satz der gemessene Höchstwert
        self.stufe = stufe                    # "exakt" | "embed" | "zitat"
        self.ueberschneidung = ueberschneidung  # wörtlich, für den 2. Versuch

    def __repr__(self) -> str:
        return (f"<Urteil {'REDUNDANT' if self.redundant else 'ok'} "
                f"stufe={self.stufe} grund={self.grund!r}>")


def satz_werte(entwurf: str, fremdtexte: list[str]) -> tuple[list[str], list[float], str]:
    """Kaskade auf Satzebene: erst exakt, dann — nur wo nötig — Embeddings.

    Sätze, die Stufe 1 schon als Wiederholung erkannt hat, brauchen keinen
    zweiten Blick. Der Dienst wird also nur für die Zweifelsfälle bemüht."""
    ss = saetze(entwurf)
    if not ss or not fremdtexte:
        return ss, [0.0] * len(ss), "exakt"
    werte = [max_ueberlappung(s, fremdtexte) for s in ss]
    offen = [i for i, w in enumerate(werte) if w < SCHWELLE_EXAKT]
    if not offen:
        return ss, werte, "exakt"
    emb = aehnlichkeiten([ss[i] for i in offen], fremdtexte)
    if emb is None:
        return ss, werte, "exakt"          # Dienst aus → Stufe 1 allein
    for i, sim in zip(offen, emb):
        # Auf die Exakt-Skala umrechnen, damit ein Schwellenwert beide Stufen
        # bedient: Ab SCHWELLE_EMBED gilt derselbe Befund wie bei SCHWELLE_EXAKT.
        if sim >= SCHWELLE_EMBED:
            werte[i] = max(werte[i], SCHWELLE_EXAKT)
    return ss, werte, "embed"


def pruefe(entwurf: str, fremdtexte: list[str],
           chef_texte: list[str] | None = None,
           kollegen: list[str] | None = None) -> Urteil:
    """Vollständige Prüfung eines Antwortentwurfs gegen die Vorbeiträge.

    `fremdtexte` = was Agenten im Raum geschrieben haben (Kollegen UND eigene
    frühere Beiträge — des Chefs Vorgabe: alles gleich streng).

    `chef_texte` = was Chef bzw. ein Mensch gesagt hat. Diese sind von BEIDEN
    Prüfungen ausgenommen, und zwar mit Absicht: Die `WEITERGABE`-Regel im
    System-Prompt verlangt ausdrücklich, des Chefs Anweisungen WÖRTLICH und mit
    Quellenangabe weiterzureichen (damit kein Agent Fristen dazuerfindet). Würde
    die Zitatprüfung auch hier greifen, bestrafte sie genau das Verhalten, das
    der Prompt vorschreibt — im Trockenlauf traf das eine legitime Beauftragung
    („Archiv, kannst du sagen, was zum Thema Backups im Archiv steht?").

    Reihenfolge mit Absicht: Der Zitat-Verstoß wird zuerst geprüft, weil er die
    ganze Antwort kostet. Erst danach lohnt die feinere Redundanz-Messung."""
    if not entwurf.strip():
        return Urteil(True, "leerer Entwurf", [], "exakt")
    if not fremdtexte:
        # Erste Nachricht im Raum: Es gibt nichts, was wiederholt werden könnte.
        return Urteil(False, "nichts zum Vergleichen", [], "exakt")

    chef_texte = chef_texte or []
    kollegen = kollegen or []

    # Beauftragungen aus dem Entwurf herauslösen: Sie dürfen wiederholen, was
    # schon gesagt wurde, weil ein unvollständiger Auftrag nutzlos wäre. Geprüft
    # wird nur der Rest der Nachricht (Chef: „nur die Beauftragung durchlassen").
    alle_saetze = saetze(entwurf)
    auftraege = [s for s in alle_saetze if ist_beauftragung(s, kollegen)]
    rest = " ".join(s for s in alle_saetze if s not in auftraege) if auftraege else entwurf
    if auftraege and not rest.strip():
        return Urteil(False, "reine Beauftragung", [], "exakt")

    stelle = zitat_verstoss(rest, fremdtexte, chef_texte)
    if stelle:
        return Urteil(True, "ungekennzeichnete Übernahme fremder Worte", [],
                      "zitat", stelle)

    ss, werte, stufe = satz_werte(rest, fremdtexte)
    # Sätze, die eine Anweisung von Chef weitergeben, zählen nicht als
    # Wiederholung — sonst blockt die Bremse die Weitergabe, die der Prompt
    # verlangt. Erkannt an der Überlappung mit dem, was Chef selbst sagte.
    if chef_texte:
        for i, s in enumerate(ss):
            if max_ueberlappung(s, chef_texte) >= SCHWELLE_EXAKT:
                werte[i] = 0.0
    redundant = absatz_urteil(rest, ss, werte)
    ueberschnitten = " · ".join(s for s, w in zip(ss, werte) if w >= SCHWELLE_EXAKT)
    return Urteil(redundant,
                  "nichts Neues gegenüber dem Raum" if redundant else "trägt bei",
                  werte, stufe, ueberschnitten)


def _gewichte(teil: list[tuple[str, float]]) -> tuple[int, int]:
    """(Gesamtgewicht, redundantes Gewicht) eines Textstücks.

    Gewichtet wird nach Wortzahl, nicht nach Satzzahl — des Chefs Vorgabe: „Wenn die
    Begrüßung meistens ein Hallo, guten Tag etc. ist, dann soll dadurch nicht die
    Antwort verschwinden." Bei bloßem Satzzählen wäre „Hallo! Der Termin ist am
    Dienstag." zur Hälfte redundant und fiele; nach Wortgewicht sind es 1 von 6
    Wörtern. Kurze Floskeln wiegen damit von selbst leicht, ohne dass irgendwo
    eine Liste von Grußformeln gepflegt werden müsste."""
    gesamt = redundant = 0
    for satz, wert in teil:
        if ist_floskel(satz):      # Gruß und Dank zählen weder oben noch unten
            continue
        n = len(worte(satz)) or 1
        gesamt += n
        if wert >= SCHWELLE_EXAKT:
            redundant += n
    return gesamt, redundant


def absatz_urteil(entwurf: str, ss: list[str], werte: list[float]) -> bool:
    """Aus den Satz-Werten das Urteil über die ganze Antwort bilden.

    des Chefs Regel: „Einzelne Sätze und Gruß-Formeln führen nicht zu einem Blocken
    des Absatzes, erst ab etwa 30% bis 40% wird es kritisch. Gleiches gilt für
    die gesamte Antwort."

    Also derselbe Anteilsmaßstab auf beiden Ebenen: Ein Absatz gilt als
    verbraucht, wenn ANTEIL_KRITISCH seines Gewichts wiederholt ist; die Antwort
    fällt, wenn ANTEIL_KRITISCH ihres Gewichts in verbrauchten Absätzen steckt.
    Im Chat-Normalfall — eine Antwort ohne Leerzeile — fallen beide Ebenen
    zusammen, und die Regel reduziert sich auf die einfache Fassung."""
    if not ss:
        return True
    paare = list(zip(ss, werte))
    bloecke = absaetze(entwurf) or [entwurf]

    gesamt = verbraucht = 0
    i = 0
    for block in bloecke:
        n = len(saetze(block)) or 1
        teil = paare[i:i + n] or paare[i:]      # Rest, falls die Zerlegung driftet
        i += n
        g, r = _gewichte(teil)
        if not g:
            continue
        gesamt += g
        if r / g >= ANTEIL_KRITISCH:            # dieser Absatz ist verbraucht
            verbraucht += g
    if not gesamt:
        # Nach dem Floskel-Abzug ist nichts übrig: Die Antwort besteht aus
        # reinem Gruß. In einem laufenden Gespräch ist das kein Beitrag —
        # Begrüßungen neuer Gäste laufen über _on_member und nicht hier durch.
        return True
    return verbraucht / gesamt >= ANTEIL_KRITISCH
