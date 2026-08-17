#!/usr/bin/env python3
"""konto.py — das Geschaeftskonto nachhalten, ohne es anzufassen.

Arbeitsteilung (Schritt 3 des Kontoplans):

    Chef exportiert  ->  Buchhalter rechnet  ->  Buchhalter legt vor  ->  Chef fuehrt aus

Buchhalter nimmt das Nachhalten ab, nicht das Klicken. Er sieht nie Zugangsdaten,
und in diesem Modul gibt es bewusst keine Funktion, die eine Order ausloesen
koennte -- auch keine auskommentierte.

WAS DER TRANSAKTIONSEXPORT HERGIBT -- UND WAS NICHT
Der CSV-Export ist eine Liste von BEWEGUNGEN, keine Vermoegensuebersicht.
Daraus exakt ableitbar sind: Einzahlungen, Gebuehren, Ertraege, Steuern und der
Cash-Saldo. NICHT ableitbar ist der angelegte Teil -- dafuer braeuchte es die
aktuellen Kurse der Positionen, die im Export nicht stehen. Wer den Kaufpreis
als Bestand einsetzt, rechnet mit einem Wert von gestern und merkt es nicht.
Deshalb verlangt das Einlesen den angelegten Teil ausdruecklich, statt ihn zu
schaetzen.

WARUM VOR DEM SPEICHERN GEPRUEFT WIRD
Ein Tippfehler in einer Summe waere sonst eine falsche
Umschichtungsempfehlung. Dieselbe Pruefung ist zugleich die Abwehr gegen einen
praeparierten Export: Der CSV ist eine Datei von aussen, die in ein
schreibendes Werkzeug laeuft.

Aufruf:
    konto.py                                  Lage anzeigen
    konto.py --setzen liquide=1000 angelegt=2000
    konto.py --einlesen auszug.csv --angelegt 2000
    konto.py --gebuehren auszug.csv           Gebuehrenbilanz, ohne zu speichern
    konto.py --abfluss                        Fixkosten aus der Konfiguration abgleichen
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
sys.path.insert(0, str(FRAMEWORK))

import speicher                                                # noqa: E402

MITARBEITER = FRAMEWORK.parent
BUCHHALTER_CFG = MITARBEITER / "buchhalter" / "broker" / "buchhalter.config.json"

# Ab wann ein Sprung nachgefragt statt uebernommen wird: mehr als die Haelfte
# des bisherigen Werts UND mehr als dieser Betrag. Zwei Bedingungen, weil bei
# 20 EUR Gesamtvermoegen eine Verdopplung noch normal ist -- eine reine
# Prozentschwelle wuerde dort staendig anschlagen und dadurch abstumpfen.
SPRUNG_ANTEIL = Decimal("0.5")
SPRUNG_EUR = Decimal("25")


# --- Abfluesse ------------------------------------------------------------

def fixkosten() -> dict[str, Decimal]:
    """Die Fixkosten aus Buchhalters Konfiguration -- der EINEN Pflegestelle.

    Eine eigene Liste hier waere die zweite Wahrheit ueber dieselbe Zahl. Genau
    daran ist am 16.08. der Tagesdeckel gescheitert: an zwei Stellen unabhaengig
    gelesen, eine Aenderung wirkungslos, der Status meldete weiter den alten Wert.
    """
    d = json.loads(BUCHHALTER_CFG.read_text(encoding="utf-8"))
    return {k: Decimal(str(v)) for k, v in (d.get("fixkosten_eur_monat") or {}).items()}


def abfluss_abgleichen() -> Decimal:
    """Tabelle aus der Konfiguration nachziehen. Rueckgabe: Summe je Monat."""
    posten = fixkosten()
    with speicher.verbindung() as c:
        c.execute("DELETE FROM firma.konto_abfluss WHERE posten <> ALL(%s)",
                  (list(posten) or [""],))
        for name, betrag in posten.items():
            c.execute(
                "INSERT INTO firma.konto_abfluss (posten, betrag_eur) VALUES (%s, %s) "
                "ON CONFLICT (posten) DO UPDATE SET betrag_eur = EXCLUDED.betrag_eur, "
                "abgeglichen = now()", (name, betrag))
    return sum(posten.values(), Decimal(0))


# --- Lage -----------------------------------------------------------------

def lage() -> dict | None:
    """Die aktuelle Lage oder None, wenn noch kein Stand erfasst ist."""
    abfluss_abgleichen()
    with speicher.verbindung() as c:
        # psycopg 3 liefert den Cursor aus execute() zurueck -- die Verbindung
        # selbst kann nicht fetchen.
        cur = c.execute("SELECT * FROM firma.konto_lage")
        z = cur.fetchone()
        if not z:
            return None
        return dict(zip([d.name for d in cur.description], z))


def lage_text() -> str:
    """Die Lage in Worten. Jede Auskunft nennt den Stichtag -- und sagt es
    ausdruecklich, wenn er alt ist."""
    l = lage()
    if not l:
        return ("Noch kein Kontostand erfasst. Chef muesste einen Stand melden "
                "(konto.py --setzen liquide=… angelegt=…).")

    gesamt = l["gesamt_eur"]
    zeilen = [
        f"Stand vom {l['stichtag']} ({l['alter_tage']} Tage alt"
        + (", VERALTET" if l["veraltet"] else "") + f", Quelle: {l['quelle']})",
        f"  liquide  {l['liquide_eur']:>10.2f} EUR",
        f"  angelegt {l['angelegt_eur']:>10.2f} EUR",
        f"  gesamt   {gesamt:>10.2f} EUR",
    ]
    if l.get("krypto_eur"):
        # K8: sichtbar, aber ohne Grenze -- Chef nannte eine Einschaetzung
        # ("spekulativer"), keine Regel.
        zeilen.append(f"  davon Krypto {l['krypto_eur']:.2f} EUR "
                      f"({(l['krypto_anteil'] or 0) * 100:.1f} % des Gesamtvermoegens, "
                      f"spekulativer Teil)")
    if l["ist_anteil"] is not None:
        zeilen.append(f"  liquide-Anteil {l['ist_anteil'] * 100:.1f} % "
                      f"(Ziel {l['soll_anteil'] * 100:.1f} %, "
                      f"Toleranz {l['toleranz'] * 100:.1f} Prozentpunkte)")
    if l["reichweite_monate"] is not None:
        zeilen.append(f"  Reichweite {l['reichweite_monate']:.1f} Monate bei "
                      f"{l['abfluss_monat_eur']:.2f} EUR Fixkosten "
                      f"(Untergrenze {l['mindest_reichweite_mon']:.0f})")
    g = gebuehren_bilanz()
    if g and g["orders"]:
        zeilen.append(f"  Gebuehren seit Beginn: {g['gebuehren_eur']:.2f} EUR auf "
                      f"{g['volumen_eur']:.2f} EUR Ordervolumen "
                      f"({(g['quote_gesamt'] or 0) * 100:.1f} %, {g['orders']} Orders)")
    zeilen.append("  " + bewertung(l))
    return "\n".join(zeilen)


def gebuehren_bilanz() -> dict | None:
    """Kumulierte Gebuehren aus der Datenbank. Die Zahl macht sichtbar, was
    einzelne 1-EUR-Posten verbergen: 10,00 EUR auf 1000,00 EUR Einzahlung sind 1 %, und das faellt bei fuenf Einzelbuchungen niemandem auf."""
    with speicher.verbindung() as c:
        cur = c.execute("SELECT * FROM firma.konto_gebuehren")
        z = cur.fetchone()
        if not z:
            return None
        return dict(zip([d.name for d in cur.description], z))


def bewertung(l: dict) -> str:
    """Was aus der Lage folgt. Reihenfolge ist Absicht: erst die Schwelle, dann
    die Liquiditaet, dann die Quote.

    Untergrenze VOR Quote, weil eine korrekte Drittelquote nichts nuetzt, wenn
    die Abos nicht bezahlt werden koennen.
    """
    if l["gesamt_eur"] < l["mindest_gesamt_eur"]:
        noetig = l["mindest_gesamt_eur"]
        return (f"Regel ruht: Gesamtvermoegen {l['gesamt_eur']:.2f} EUR unter der "
                f"Schwelle von {noetig:.2f} EUR. Jede Umschichtung kostet 1,00 EUR "
                f"Gebuehr — bei diesem Betrag mehr, als sie ausgleicht.")
    if (l["reichweite_monate"] is not None
            and l["reichweite_monate"] < l["mindest_reichweite_mon"]):
        fehlt = (l["mindest_reichweite_mon"] * l["abfluss_monat_eur"]) - l["liquide_eur"]
        return (f"Liquiditaet zu knapp: {l['reichweite_monate']:.1f} Monate statt "
                f"{l['mindest_reichweite_mon']:.0f}. Es fehlen {fehlt:.2f} EUR im "
                f"liquiden Teil — unabhaengig davon, ob die Quote stimmt.")
    abw = abs(l["ist_anteil"] - l["soll_anteil"]) if l["ist_anteil"] is not None else 0
    if abw <= l["toleranz"]:
        return f"Im Toleranzband (Abweichung {abw * 100:.1f} Prozentpunkte). Kein Handlungsbedarf."
    betrag = abs(l["umschichtung_eur"])
    quote = Decimal(1) / betrag if betrag > 0 else Decimal(1)
    richtung = ("vom liquiden in den angelegten Teil" if l["umschichtung_eur"] > 0
                else "vom angelegten in den liquiden Teil")
    if quote > l["max_gebuehrenquote"]:
        return (f"Abweichung {abw * 100:.1f} Prozentpunkte, aber die Umschichtung "
                f"({betrag:.2f} EUR) haette eine Gebuehrenquote von {quote * 100:.2f} % "
                f"— ueber der Grenze von {l['max_gebuehrenquote'] * 100:.2f} %. "
                f"Kein Vorschlag.")
    return (f"Vorschlag: {betrag:.2f} EUR {richtung} "
            f"(Ist {l['ist_anteil'] * 100:.1f} %, Ziel {l['soll_anteil'] * 100:.1f} %, "
            f"Gebuehrenquote {quote * 100:.2f} %).")


# --- Erfassen -------------------------------------------------------------

def pruefen(liquide: Decimal, angelegt: Decimal) -> str | None:
    """Plausibilitaet gegen den letzten Stand. Rueckgabe: Grund oder None.

    Nicht der Wert wird geprueft, sondern der SPRUNG -- ein absoluter Betrag
    kann jederzeit richtig sein, ein Sprung um das Zehnfache ueber Nacht
    praktisch nie.
    """
    with speicher.verbindung() as c:
        z = c.execute("SELECT liquide_eur, angelegt_eur FROM firma.konto_stand "
                      "ORDER BY stichtag DESC LIMIT 1").fetchone()
    if not z:
        return None
    for name, alt, neu in (("liquide", z[0], liquide), ("angelegt", z[1], angelegt)):
        d = abs(neu - alt)
        if d > SPRUNG_EUR and (alt == 0 or d > alt * SPRUNG_ANTEIL):
            return (f"{name} springt von {alt:.2f} auf {neu:.2f} EUR "
                    f"({d:.2f} EUR Unterschied)")
    return None


def setzen(liquide: Decimal, angelegt: Decimal, *, quelle: str,
           krypto: Decimal = Decimal(0),
           stichtag: date | None = None, trotzdem: bool = False) -> str:
    # Harte Ablehnung, nicht verhandelbar: Ein negativer Bestand ist kein
    # ungewoehnlicher Wert, sondern ein unmoeglicher. `--trotzdem` ist fuer
    # Werte gedacht, die ueberraschen und trotzdem stimmen koennen -- diese
    # koennen nicht stimmen, und die Datenbank wiese sie ohnehin zurueck.
    if krypto > angelegt:
        # K8: Krypto ist ein TEIL des angelegten Betrags, nicht eine dritte
        # Summe daneben. Mehr Krypto als angelegt waere in sich widersprüchlich.
        return (f"NICHT GESPEICHERT — Krypto ({krypto:.2f}) kann nicht groesser "
                f"sein als der angelegte Teil ({angelegt:.2f}): Es ist ein Teil "
                f"davon, keine zusaetzliche Summe.")
    if liquide < 0 or angelegt < 0 or krypto < 0:
        return ("NICHT GESPEICHERT — negative Betraege sind kein moeglicher "
                "Kontostand. Das ist keine Ermessensfrage.")
    grund = pruefen(liquide, angelegt)
    if grund and not trotzdem:
        return (f"NICHT GESPEICHERT — unplausibel: {grund}. Wenn der Wert stimmt, "
                f"noch einmal mit --trotzdem.")
    stichtag = stichtag or date.today()
    with speicher.verbindung() as c:
        c.execute(
            "INSERT INTO firma.konto_stand (stichtag, liquide_eur, angelegt_eur, "
            "krypto_eur, quelle) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (stichtag) DO UPDATE SET "
            "liquide_eur = EXCLUDED.liquide_eur, angelegt_eur = EXCLUDED.angelegt_eur, "
            "krypto_eur = EXCLUDED.krypto_eur, quelle = EXCLUDED.quelle, "
            "erfasst = now()",
            (stichtag, liquide, angelegt, krypto, quelle))
    warnung = f"  (Hinweis: {grund}, auf Anweisung uebernommen)\n" if grund else ""
    return f"Stand {stichtag} gespeichert.\n{warnung}{lage_text()}"


# --- Auszug lesen ---------------------------------------------------------

def _zahl(s: str) -> Decimal:
    s = (s or "").strip()
    return Decimal(s) if s else Decimal(0)


def gebuehren_auswerten(pfad: Path) -> dict:
    """Was der Transaktionsexport WIRKLICH hergibt: Bewegungen.

    Die Gebuehrenquote ist hier die entscheidende Zahl -- bei einem Festbetrag
    von 1,00 EUR je Order haengt sie allein am Volumen. Der Auszug wird gelesen
    und NICHT aufbewahrt; die Datei bleibt, wo Chef sie hingelegt hat.
    """
    ein = gebuehr = ertrag = steuer = Decimal(0)
    orders: list[dict] = []
    with pfad.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            betrag, g, st = (_zahl(r.get("amount")), _zahl(r.get("fee")),
                             _zahl(r.get("tax")))
            art = (r.get("type") or "").upper()
            gebuehr += abs(g)
            steuer += abs(st)
            if art == "TRANSFER_INBOUND":
                ein += betrag
            elif art in ("DIVIDEND", "INTEREST_PAYMENT"):
                ertrag += betrag
            elif art in ("BUY", "SELL"):
                volumen = abs(betrag)
                orders.append({
                    "datum": r.get("date", ""), "name": r.get("name", ""),
                    "volumen_eur": volumen, "gebuehr_eur": abs(g),
                    "quote": (abs(g) / volumen) if volumen else None})
    return {"einzahlungen_eur": ein, "gebuehren_eur": gebuehr,
            "ertraege_eur": ertrag, "steuern_eur": steuer,
            "orders": orders,
            "cash_saldo_eur": ein + ertrag - steuer - gebuehr
                              - sum(o["volumen_eur"] for o in orders)}


def gebuehren_text(pfad: Path) -> str:
    a = gebuehren_auswerten(pfad)
    z = [f"Auszug {pfad.name} — {len(a['orders'])} Orders",
         f"  Einzahlungen {a['einzahlungen_eur']:>8.2f} EUR",
         f"  Gebuehren    {a['gebuehren_eur']:>8.2f} EUR",
         f"  Ertraege     {a['ertraege_eur']:>8.2f} EUR",
         f"  Steuern      {a['steuern_eur']:>8.2f} EUR"]
    if a["einzahlungen_eur"] > 0:
        anteil = a["gebuehren_eur"] / a["einzahlungen_eur"] * 100
        z.append(f"  Seit Kontoeroeffnung {a['gebuehren_eur']:.2f} EUR Gebuehren auf "
                 f"{a['einzahlungen_eur']:.2f} EUR Einzahlung ({anteil:.1f} %).")
    for o in a["orders"]:
        if o["quote"] is not None:
            z.append(f"    {o['datum']}  {o['name'][:22]:<22} {o['volumen_eur']:>7.2f} EUR "
                     f"→ Gebuehr {o['quote'] * 100:5.2f} %")
    return "\n".join(z)


def plan_rechnen(einzahlung: Decimal, je_order: Decimal,
                 gebuehr: Decimal = Decimal(1)) -> str:
    """Was eine geplante Einzahlung mit der Gebuehrenquote macht.

    Chef am 17.08.: "sobald ich wieder fluessig bin, 150 EUR einzahlen und bei
    jeder Order, die ich bereits habe, 25 EUR nachkaufen."

    Diese Rechnung ist der Kern der ganzen Depot-Frage, und sie ist erfreulich:
    Ein FESTBETRAG als Gebuehr laesst sich nicht wegverhandeln, aber vollstaendig
    verduennen. Wer bei 4 EUR Volumen kauft, zahlt 25 %; bei 25 EUR sind es 4 %,
    bei 100 EUR ein Prozent.

    Die Aufteilung selbst bleibt des Chefs Sache. Gerechnet wird sie, damit der
    Preis der Streuung sichtbar ist -- welche Papiere es sein sollen, ist
    Anlageberatung und ausdruecklich nicht Teil dieses Plans.
    """
    g = gebuehren_bilanz()
    l = lage()
    zeilen = []
    if g and g["orders"]:
        alt_quote = (g["gebuehren_eur"] / g["volumen_eur"]) if g["volumen_eur"] else 0
        anzahl = int(g["orders"])
        zeilen.append(f"Heute: {anzahl} Orders, {g['volumen_eur']:.2f} EUR Volumen, "
                      f"{g['gebuehren_eur']:.2f} EUR Gebuehren = {alt_quote * 100:.1f} %")
    else:
        anzahl, alt_quote = 0, Decimal(0)
        zeilen.append("Heute: keine Orders erfasst")

    brutto = anzahl * je_order
    if brutto > einzahlung:
        zeilen.append(f"ACHTUNG: {anzahl} x {je_order:.2f} = {brutto:.2f} EUR "
                      f"uebersteigt die Einzahlung von {einzahlung:.2f} EUR.")
        brutto = einzahlung
        anzahl = int(brutto / je_order) if je_order else 0
    kosten = anzahl * gebuehr
    zeilen.append(f"Plan: {einzahlung:.2f} EUR einzahlen, {anzahl} x {je_order:.2f} EUR "
                  f"nachkaufen")
    if je_order:
        zeilen.append(f"  je Order {gebuehr / je_order * 100:.1f} % Gebuehr "
                      f"({gebuehr:.2f} von {je_order:.2f} EUR), "
                      f"netto investiert {anzahl * (je_order - gebuehr):.2f} EUR")
    zeilen.append(f"  bleibt liquide: {einzahlung - brutto:.2f} EUR")

    if g and g["volumen_eur"]:
        neu_vol = g["volumen_eur"] + brutto
        neu_geb = g["gebuehren_eur"] + kosten
        zeilen.append(f"Danach kumuliert: {neu_geb:.2f} EUR auf {neu_vol:.2f} EUR "
                      f"= {neu_geb / neu_vol * 100:.1f} % (heute {alt_quote * 100:.1f} %)")

    # Was die Drittelregel danach sagt -- ohne die Schwelle zu verschweigen.
    if l:
        neu_gesamt = l["gesamt_eur"] + einzahlung
        schwelle = l["mindest_gesamt_eur"]
        if neu_gesamt < schwelle:
            zeilen.append(f"Drittelregel bleibt in Ruhe: {neu_gesamt:.2f} EUR unter "
                          f"der Schwelle von {schwelle:.2f} EUR.")
        else:
            zeilen.append(f"Drittelregel greift danach: {neu_gesamt:.2f} EUR ueber "
                          f"der Schwelle von {schwelle:.2f} EUR.")

    # Der Preis der Streuung, in Zahlen statt in Meinung.
    if brutto and je_order:
        zeilen.append("Dieselbe Summe anders aufgeteilt:")
        for teile in (anzahl, max(1, anzahl // 2), 1):
            if teile < 1 or (teile != anzahl and teile == anzahl):
                continue
            q = (teile * gebuehr) / brutto
            zeilen.append(f"  auf {teile} Order(s): {teile * gebuehr:.2f} EUR auf "
                          f"{brutto:.2f} EUR = {q * 100:.2f} % "
                          f"(je Order {brutto / teile:.2f} EUR)")
    return "\n".join(zeilen)


def empfehlung_merken(richtung: str, begruendung: str, *,
                      betrag: Decimal | None = None,
                      grundlage: dict | None = None) -> int:
    """Eine Empfehlung festhalten. Rueckgabe: ihre Nummer.

    Chef am 17.08.: Buchhalter soll seine Empfehlungen behalten, "damit er sie als
    Referenz behaelt". Der Wert liegt nicht im Betrag, sondern im AUSGANG: Erst
    der Abgleich zwischen Rat und Folge macht aus Meinung Erfahrung. Deshalb ist
    `ausgang` anfangs leer und wird nachgetragen, statt den Eintrag zu schliessen.

    Als Geschaeftsgeheimnis klassifiziert (geheim = TRUE als Vorgabe): Eine
    Empfehlung nennt Betraege und Absichten zum Privatvermoegen. Wer eine
    Ausnahme will, muss sie hinschreiben.
    """
    with speicher.verbindung() as c:
        z = c.execute(
            "INSERT INTO firma.empfehlung (richtung, betrag_eur, begruendung, grundlage) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (richtung, betrag, begruendung.strip(),
             json.dumps(grundlage or {}, default=str))).fetchone()
    return int(z[0])


def empfehlungen_lesen(nur_offene: bool = False, limit: int = 10) -> list[dict]:
    """Die juengsten Empfehlungen -- Buchhalters Gedaechtnis."""
    wo = "WHERE ausgang IS NULL" if nur_offene else ""
    with speicher.verbindung() as c:
        cur = c.execute(f"SELECT * FROM firma.empfehlung {wo} "
                        f"ORDER BY erstellt DESC LIMIT %s", (limit,))
        spalten = [d.name for d in cur.description]
        return [dict(zip(spalten, z)) for z in cur.fetchall()]


def empfehlung_ausgang(nummer: int, ausgang: str, notiz: str = "") -> str:
    """Was aus einer Empfehlung wurde. Nur Chef kann das wissen -- Buchhalter
    sieht das Konto nicht."""
    if ausgang not in ("befolgt", "abgelehnt", "verfallen"):
        return "Ausgang muss 'befolgt', 'abgelehnt' oder 'verfallen' sein."
    with speicher.verbindung() as c:
        n = c.execute(
            "UPDATE firma.empfehlung SET ausgang = %s, ausgang_am = now(), "
            "notiz = nullif(%s, '') WHERE id = %s AND ausgang IS NULL",
            (ausgang, notiz, nummer)).rowcount
    return (f"Empfehlung {nummer}: {ausgang}." if n
            else f"Empfehlung {nummer} nicht gefunden oder schon bewertet.")


def empfehlungen_text(nur_offene: bool = False) -> str:
    e = empfehlungen_lesen(nur_offene)
    if not e:
        return "Keine Empfehlungen gespeichert."
    zeilen = ["Empfehlungen (GESCHAEFTSGEHEIM — nicht nach draussen):"]
    for v in e:
        stand = v["ausgang"] or "offen"
        betrag = f"{v['betrag_eur']:.2f} EUR " if v["betrag_eur"] is not None else ""
        zeilen.append(f"  #{v['id']} {v['erstellt']:%Y-%m-%d} {v['richtung']} "
                      f"{betrag}[{stand}] — {v['begruendung'][:110]}")
        if v.get("notiz"):
            zeilen.append(f"        Notiz: {v['notiz'][:100]}")
    return "\n".join(zeilen)


def orders_speichern(pfad: Path) -> int:
    """Orders aus dem Auszug in die Datenbank. Nur Datum, Volumen und Gebuehr --
    kein Wertpapiername, keine Stueckzahl. Was nicht gespeichert wird, kann auch
    nicht preisgegeben werden.

    Doppeltes Einlesen zaehlt nicht doppelt (UNIQUE auf datum/volumen/gebuehr).
    """
    a = gebuehren_auswerten(pfad)
    neu = 0
    with speicher.verbindung() as c:
        for o in a["orders"]:
            if not o["volumen_eur"]:
                continue
            r = c.execute(
                "INSERT INTO firma.konto_order (datum, volumen_eur, gebuehr_eur, quelle) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (o["datum"][:10], o["volumen_eur"], o["gebuehr_eur"], pfad.name))
            neu += r.rowcount or 0
    return neu


def einlesen(pfad: Path, angelegt: Decimal, *, krypto: Decimal = Decimal(0),
             trotzdem: bool = False) -> str:
    """Stand aus dem Auszug erfassen.

    `angelegt` ist Pflicht und wird bewusst nicht geschaetzt: Der Export
    enthaelt Kaufpreise, keine Kurswerte (siehe Modulkopf).
    """
    a = gebuehren_auswerten(pfad)
    # ERST den Stand pruefen, DANN die Orders speichern. Die andere Reihenfolge
    # war ein Konstruktionsmangel: Ein praeparierter Auszug haette seine Orders
    # in der Datenbank hinterlassen, obwohl der Stand abgelehnt wurde -- und die
    # Gebuehrenbilanz waere von Daten verfaelscht, die nie akzeptiert wurden.
    # Gefunden hat das die Testpalette, nicht ich.
    text = setzen(a["cash_saldo_eur"], angelegt, krypto=krypto,
                  quelle=pfad.name, trotzdem=trotzdem)
    if text.startswith("NICHT GESPEICHERT"):
        return text
    neu = orders_speichern(pfad)
    return f"{neu} neue Order(s) erfasst.\n{text}"


# --- CLI ------------------------------------------------------------------

def haupt(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--setzen", nargs="+", metavar="liquide=… angelegt=…")
    ap.add_argument("--einlesen", type=Path)
    ap.add_argument("--angelegt", type=Decimal)
    ap.add_argument("--krypto", type=Decimal, default=Decimal(0),
                    help="Teil des angelegten Betrags, der in Krypto steckt (K8)")
    ap.add_argument("--gebuehren", type=Path)
    ap.add_argument("--stichtag")
    ap.add_argument("--abfluss", action="store_true")
    ap.add_argument("--plan", nargs="+", metavar="einzahlung=150 je_order=25",
                    help="Wirkung einer geplanten Einzahlung auf die Gebuehrenquote")
    ap.add_argument("--trotzdem", action="store_true")
    a = ap.parse_args(argv)

    if a.plan:
        w = dict(s.split("=", 1) for s in a.plan)
        print(plan_rechnen(Decimal(w.get("einzahlung", 0)),
                           Decimal(w.get("je_order", 0)),
                           Decimal(w.get("gebuehr", 1))))
        return 0
    if a.gebuehren:
        print(gebuehren_text(a.gebuehren))
        return 0
    if a.abfluss:
        print(f"Abfluesse abgeglichen: {abfluss_abgleichen():.2f} EUR/Monat aus "
              f"{BUCHHALTER_CFG.name}")
        return 0
    if a.einlesen:
        if a.angelegt is None:
            print("--angelegt fehlt. Der Export enthaelt keine Kurswerte, der "
                  "angelegte Teil laesst sich daraus nicht ableiten.")
            return 2
        print(einlesen(a.einlesen, a.angelegt, krypto=a.krypto,
                       trotzdem=a.trotzdem))
        return 0
    if a.setzen:
        w = dict(s.split("=", 1) for s in a.setzen)
        st = datetime.strptime(a.stichtag, "%Y-%m-%d").date() if a.stichtag else None
        print(setzen(Decimal(w["liquide"]), Decimal(w["angelegt"]),
                     krypto=Decimal(w.get("krypto", 0)),
                     quelle="von Hand", stichtag=st, trotzdem=a.trotzdem))
        return 0

    print(lage_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(haupt())
