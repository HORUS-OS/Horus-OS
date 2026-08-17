#!/usr/bin/env python3
"""Fachliche Passung — wie gut fällt eine Frage in das Ressort eines Agenten?

Buchhalters Warteschlange war bisher allein nach Budget-Prioritätsklasse sortiert
(`PRIOS`). Bei einer Kalenderfrage konnte damit Buchhalter vor Assistenz denken — und
weil der Broker seriell arbeitet, antwortet dann der Falsche zuerst und der
Zuständige darf nur noch ergänzen. des Chefs Vorgabe: „Zuständigkeit zuerst, bei
Gleichstand rotierender Zufall."

Bewusst deterministisch und ohne Modell-Aufruf: Diese Zahl wird VOR dem Denken
gebraucht, um die Reihenfolge festzulegen. Ein LLM-Aufruf zur Sortierung von
LLM-Aufrufen wäre ein teurer Zirkelschluss.

Die Schlagwörter hängen an denselben `kontext`-Werten, die auch `_dynamic_context`
und `zustaendigkeiten()` benutzen — ein neuer Werkzeug-Kontext wird hier ergänzt
und wirkt dann überall.
"""
from __future__ import annotations

import hashlib
import re

# kontext-Wert -> Wörter, die auf dieses Ressort hindeuten
RESSORT = {
    "kalender": {
        "termin", "termine", "wann", "uhr", "uhrzeit", "datum", "tag", "woche",
        "morgen", "übermorgen", "heute", "montag", "dienstag", "mittwoch",
        "donnerstag", "freitag", "samstag", "sonntag", "kalender", "treffen",
        "verabredung", "besprechung", "meeting", "frist", "deadline", "erinnere",
        "erinnerung", "planen", "verschieben", "absagen", "eintragen",
    },
    "archiv": {
        "archiv", "gedächtnis", "erinnerst", "früher", "damals", "vergangen",
        "dokument", "dokumente", "datei", "dateien", "unterlagen", "notiz",
        "notizen", "protokoll", "bericht", "berichte", "recherche", "suchen",
        "nachschlagen", "nachsehen", "backup", "backups", "gespeichert",
        "abgelegt", "wissen", "quelle", "quellen", "beleg",
    },
    "orchestrator": {
        "auftrag", "aufträge", "projekt", "projekte", "aufgabe", "aufgaben",
        "team", "teams", "koordinieren", "organisieren", "planung", "umsetzen",
        "starten", "beauftragen", "verteilen", "zuständig", "übernehmen",
        "fortschritt", "status", "erledigen",
    },
    # Buchhalter hat keinen kontext-Eintrag (er IST der Broker) — seine Themen
    # stehen deshalb unter seiner agent_id, siehe passung().
    "recherche": {
        "internet", "netz", "web", "online", "webseite", "website", "seite",
        "url", "link", "quelle", "quellen", "wikipedia", "aktuell", "aktuelle",
        "neueste", "news", "nachrichten", "hersteller", "anbieter", "preis",
        "preise", "vergleich", "vergleichen", "bewertung", "anleitung",
        "nachschlagen", "herausfinden", "nachlesen", "recherchieren",
    },
    "projekt": {
        "auftrag", "auftraggeber", "projekt", "projekte", "vorhaben", "angebot",
        "anfrage", "machbar", "machbarkeit", "umsetzbar", "aufwand", "schätzung",
        "abschätzen", "kalkulieren", "annehmen", "ablehnen", "auftragen",
        "beauftragen", "abnahme", "abnehmen", "lieferung", "ergebnis",
    },
    "aussen": {
        "veröffentlichen", "veröffentlichung", "publizieren", "posten", "beitrag",
        "blog", "artikel", "newsletter", "social", "presse", "pressemitteilung",
        "öffentlich", "öffentlichkeit", "extern", "nach", "außen", "aussen",
        "entwurf", "entwerfen", "formulieren", "texten", "ankündigung",
        "datenschutz", "vertraulich", "vertraulichkeit", "geheim", "sensibel",
        "weitergeben", "preisgeben", "freigabe", "freigeben", "sperrvermerk",
        "leak", "anonymisieren", "schwärzen",
    },
    "_buchhalter": {
        "budget", "kosten", "geld", "euro", "ausgaben", "kontingent", "gpu",
        "grafikkarte", "vram", "speicher", "auslastung", "modell", "modelle",
        "kaskade", "abo", "rechnung", "verbrauch", "limit", "sparen",
    },
}

_WORT = re.compile(r"[^\wäöüßÄÖÜ]+")

KLAR = 2        # mehrere Treffer im eigenen Ressort
TEILWEISE = 1   # ein Treffer
KEINE = 0       # nichts erkennbar — jeder darf, niemand drängelt


def passung(frage: str, akte: dict) -> int:
    """Wie klar fällt `frage` in das Ressort dieses Agenten? 0, 1 oder 2.

    Ohne erkennbares Thema liefert die Funktion 0 für ALLE — dann entscheidet
    wieder die bisherige Prioritätsklasse, und niemand wird bevorzugt. Das ist
    der häufige Fall bei Small Talk und genau richtig: Dort gibt es keine
    fachliche Zuständigkeit, die eine Reihenfolge rechtfertigen würde."""
    ws = {w for w in _WORT.split(frage.lower()) if w}
    if not ws:
        return KEINE
    schluessel = list(akte.get("kontext") or [])
    eigen = f"_{akte.get('agent_id', '')}"
    if eigen in RESSORT:
        schluessel.append(eigen)
    treffer = set()
    for k in schluessel:
        treffer |= ws & RESSORT.get(k, set())
    if len(treffer) >= 2:
        return KLAR
    return TEILWEISE if treffer else KEINE


def losnummer(frage: str, agent_id: str) -> int:
    """Stabile Streuung für den Gleichstand (Chef: „bei Gleichstand rotierender
    Zufall"). Dasselbe Muster wie `_pick_greeters`: aus der Frage abgeleitet,
    also je Frage eine andere Reihenfolge, aber für alle Beteiligten konsistent —
    ohne dass sie sich absprechen müssten."""
    roh = f"{frage[:200]}|{agent_id}".encode()
    return int(hashlib.sha1(roh).hexdigest(), 16) % 1000
