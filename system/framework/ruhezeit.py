#!/usr/bin/env python3
"""Ruhezeit & Durchbruch-Regeln — eine Wahrheit für alle, die anklopfen wollen.

des Chefs Regeln aus dem Grilling (K12, K24, K51, K23):
- Nachtruhe Mo–Fr 00:00–07:00, Sa/So 00:00–09:00. Zwischen 22:00 und 00:00 ist er
  ansprechbar.
- In der Nachtruhe bricht NUR der Termin-Ruf durch. Budget-Grenze und Ausfall
  warten bis zum Morgen.
- Tagsüber sind Termin, Budget und Ausfall Durchbrüche (laut, m.text); alles
  andere bleibt stumm (m.notice).
- Morgen-Briefing 30 Minuten nach Ende der Ruhezeit: Mo–Fr 07:30, Sa/So 09:30.

Wird von mitarbeiter_agent.py, tagesplan.py und Buchhalters Präsenz-Wächter genutzt.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

# Ende der Nachtruhe je Wochentag (Mo=0 … So=6). Beginn ist immer Mitternacht.
RUHE_ENDE = {0: dtime(7, 0), 1: dtime(7, 0), 2: dtime(7, 0), 3: dtime(7, 0),
             4: dtime(7, 0), 5: dtime(9, 0), 6: dtime(9, 0)}
BRIEFING_VERZUG = timedelta(minutes=30)

# Was überhaupt laut sein darf. 'immer' = auch nachts, 'tag' = nur außerhalb der
# Ruhezeit. Alles, was hier nicht steht, ist stumm.
DURCHBRUCH_ARTEN = {"termin": "immer", "budget": "tag", "ausfall": "tag"}


def ruhe_ende_am(tag: date) -> dtime:
    return RUHE_ENDE[tag.weekday()]


def in_nachtruhe(jetzt: datetime | None = None) -> bool:
    """Zwischen Mitternacht und dem Ruhe-Ende des jeweiligen Tages."""
    jetzt = jetzt or datetime.now()
    return jetzt.time() < ruhe_ende_am(jetzt.date())


def darf_durchbrechen(art: str, jetzt: datetime | None = None) -> bool:
    """Darf eine Meldung dieser Art laut zugestellt werden (m.text statt m.notice)?"""
    regel = DURCHBRUCH_ARTEN.get(art)
    if regel is None:
        return False
    if regel == "immer":
        return True
    return not in_nachtruhe(jetzt)


def briefing_zeitpunkt(tag: date) -> datetime:
    """Wann das Morgen-Briefing dieses Tages fällig ist."""
    return datetime.combine(tag, ruhe_ende_am(tag)) + BRIEFING_VERZUG


def naechstes_briefing(jetzt: datetime | None = None) -> datetime:
    jetzt = jetzt or datetime.now()
    heute = briefing_zeitpunkt(jetzt.date())
    return heute if jetzt < heute else briefing_zeitpunkt(jetzt.date() + timedelta(days=1))


if __name__ == "__main__":   # kleine Selbstauskunft: ruhezeit.py
    n = datetime.now()
    print(f"jetzt:            {n:%a %d.%m. %H:%M}")
    print(f"Nachtruhe:        {'JA' if in_nachtruhe(n) else 'nein'} "
          f"(endet {ruhe_ende_am(n.date()):%H:%M})")
    for art in DURCHBRUCH_ARTEN:
        print(f"  {art:8s} laut? {'JA' if darf_durchbrechen(art, n) else 'nein'}")
    print(f"nächstes Briefing: {naechstes_briefing(n):%a %d.%m. %H:%M}")
