#!/usr/bin/env python3
"""Privat-Filter — entscheidet, ob ein Gedanke das Haus verlassen darf (K46/K47/K59/K65/K70).

Sitzt im Broker, weil dort als einziger Stelle Prompt und Anbieter zusammentreffen:
Jeder Gedanke jedes Mitarbeiters läuft durch `/think`.

des Chefs Regeln, in dieser Reihenfolge:

1. **Quelle als harte Regel.** Berührt eine Anfrage Chat-Verlauf, Archiv, Kalender,
   Personalakten oder als klassifiziert markierte Dateien, ist sie privat. Bewertet
   wird die Herkunft, nicht der Inhalt — der Agent meldet sie beim Aufruf mit.
2. **Persönlichkeitsdateien sind grundsätzlich lokal.** Der System-Prompt IST die
   Persona; wer sie in die Cloud schickt, gibt sie preis.
3. **Das Modell darf nie freigeben, nur eskalieren.** Hält es eine gesperrte Anfrage
   für unkritisch, entsteht ein Bericht an den Chef — erst dessen Ja öffnet den Weg.
4. **Privat + lokal reicht nicht → bleibt lokal**, notfalls mit schlechterem Ergebnis.
   Keine Anonymisierungs-Hintertür.
5. **In die Cloud geht nur eine eigenständig formulierte Sachfrage** plus eine
   nüchterne Rollenbeschreibung — kein Verlauf, keine Persona, keine Namen und Pfade.

Solange es keine Cloud-Anbieter gibt, sagt der Filter immer "lokal". Er muss trotzdem
JETZT stehen: Wird später ein Anbieter angeschlossen, ist die Grenze schon da, statt
nachträglich eingezogen werden zu müssen.
"""
from __future__ import annotations

import re

# Quellen, die eine Anfrage automatisch privat machen (K46).
PRIVATE_QUELLEN = {"chat", "verlauf", "archiv", "kalender", "personalakte",
                   "klassifiziert", "vorgaenge", "dm",
                   # Chef am 17.08.: Buchhalters Empfehlungen sind
                   # Geschaeftsgeheimnisse. Sie nennen Betraege und Absichten
                   # zum Privatvermoegen -- das geht keinen Anbieter etwas an.
                   "konto", "empfehlung", "depot"}

# Muster, die auf Interna hindeuten, auch wenn keine Quelle gemeldet wurde.
# Zweite Verteidigungslinie, keine erste: Die Quellenangabe ist maßgeblich.
INTERNA_MUSTER = [
    re.compile(r"@\w+:example.org"),              # Matrix-Kennungen der Firma
    re.compile(r"/home/nutzer|~/Horus-OS"),           # Pfade aus dem System
    re.compile(r"!\w{18}:example.org"),           # Raum-IDs
    re.compile(r"\b(keyring|passwor|token|secret)", re.I),
    # Kontodaten und Empfehlungen. Die zweite Verteidigungslinie greift auch
    # dann, wenn jemand die Quelle nicht meldet -- und eine Empfehlung wandert
    # leicht in einen Satz, der harmlos aussieht.
    re.compile(r"\bDE\d{2}\s?(\d{4}\s?){4}\d{2}\b"),      # IBAN
    re.compile(r"\b(depot|gebuehrenquote|gebührenquote|umschicht)", re.I),
]


class Urteil:
    """Warum etwas lokal bleibt oder reisen darf — nachvollziehbar im Log."""

    def __init__(self, privat: bool, grund: str):
        self.privat = privat
        self.grund = grund

    def __repr__(self) -> str:
        return f"<{'privat' if self.privat else 'frei'}: {self.grund}>"


def pruefe(*, quellen: list[str] | None = None, klassifiziert: bool = False,
           system: str = "", messages: list | None = None,
           freigabe: bool = False) -> Urteil:
    """Darf diese Anfrage zu einem externen Anbieter?

    `freigabe` = ein Chef hat den Einzelfall ausdrücklich erlaubt (K59). Nur das
    hebt eine Sperre auf — nie ein Modellurteil."""
    quellen = quellen or []
    if klassifiziert:
        return Urteil(True, "klassifizierte Persönlichkeitsdatei")
    getroffen = sorted(set(q.lower() for q in quellen) & PRIVATE_QUELLEN)
    if getroffen:
        if freigabe:
            return Urteil(False, f"Chef-Freigabe trotz Quelle: {', '.join(getroffen)}")
        return Urteil(True, f"private Quelle: {', '.join(getroffen)}")
    text = system + "\n" + "\n".join(
        m.get("content", "") for m in (messages or []) if isinstance(m, dict))
    for muster in INTERNA_MUSTER:
        if muster.search(text):
            if freigabe:
                return Urteil(False, "Chef-Freigabe trotz Internamuster")
            return Urteil(True, f"Internamuster erkannt ({muster.pattern[:24]})")
    return Urteil(False, "keine private Quelle erkennbar")


def reisefertig(sachfrage: str, rolle: str) -> tuple[str, list[dict]]:
    """Was ein externer Anbieter tatsächlich zu sehen bekommt (K70/K71).

    Nur die vom Mitarbeiter selbst formulierte Sachfrage und eine nüchterne
    Rollenbeschreibung — kein Gesprächsverlauf, keine Persona. Die Antwort wird
    danach lokal wieder in den Charakter gekleidet."""
    system = (f"Du bist {rolle}. Antworte sachlich und knapp auf die folgende Frage. "
              f"Du hast keinen weiteren Kontext und sollst keinen erfinden.")
    return system, [{"role": "user", "content": sachfrage}]


if __name__ == "__main__":      # Selbstauskunft: privatfilter.py
    proben = [
        (dict(quellen=["kalender"]), "Kalenderdaten"),
        (dict(quellen=["chat", "web"]), "Chatverlauf"),
        (dict(quellen=["web"]), "nur Websuche"),
        (dict(klassifiziert=True), "Assistenzs Persona"),
        (dict(quellen=[], system="Frag @assistenz:example.org"), "Matrix-ID im Text"),
        (dict(quellen=["archiv"], freigabe=True), "Archiv MIT Chef-Freigabe"),
        (dict(quellen=[], messages=[{"content": "Wie kocht man Reis?"}]), "harmlos"),
    ]
    for kwargs, name in proben:
        u = pruefe(**kwargs)
        print(f"  {name:26s} -> {'LOKAL ' if u.privat else 'frei  '} ({u.grund})")
