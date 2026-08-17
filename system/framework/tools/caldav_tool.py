#!/usr/bin/env python3
"""CalDAV-Werkzeug — Zugang zum Firmen-Kalender (Radicale via SSH-Tunnel localhost:5232).

Auth aus dem Keyring (radicale-firma). briefing_text() liefert kommende Termine als
Klartext für Assistenzs Kontext; add_event() legt Termine an.

CLI:
  caldav_tool.py [tage]                          -> Termine auflisten
  caldav_tool.py add "Titel" "YYYY-MM-DD HH:MM" "YYYY-MM-DD HH:MM"
"""
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import caldav
from icalendar import Calendar as ICalendar, Event

URL = "http://localhost:5232/"
USER = "firma"
CAL_NAME = "firma"
SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"   # …/system/scripts


def _pw() -> str:
    r = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "firma-keyring.py"),
                        "get", "radicale-firma"], capture_output=True, text=True)
    return r.stdout


def _calendar():
    client = caldav.DAVClient(url=URL, username=USER, password=_pw())
    principal = client.principal()
    cals = principal.calendars()
    for c in cals:
        if (c.name or "") == CAL_NAME or CAL_NAME in str(c.url):
            return c
    return cals[0] if cals else principal.make_calendar(name=CAL_NAME)


def add_event(summary: str, start: datetime, end: datetime, cal=None) -> None:
    cal = cal or _calendar()
    ev = Event()
    ev.add("uid", str(uuid.uuid4()))
    ev.add("dtstamp", datetime.now())
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("summary", summary)
    doc = ICalendar()
    doc.add("prodid", "-//Firma//Assistenz//DE")
    doc.add("version", "2.0")
    doc.add_component(ev)
    cal.save_event(doc.to_ical().decode())


def _all_calendars():
    client = caldav.DAVClient(url=URL, username=USER, password=_pw())
    return client.principal().calendars()


def list_events(days: int = 7, cal=None):
    """Termine über ALLE Collections (firma + google …), Serientermine aufgelöst."""
    now = datetime.now()
    cals = [cal] if cal else _all_calendars()
    out = []
    for c in cals:
        try:
            found = c.search(start=now, end=now + timedelta(days=days), event=True, expand=True)
        except Exception:
            try:
                found = c.date_search(start=now, end=now + timedelta(days=days))
            except Exception:
                continue
        for e in found:
            try:
                comp = e.icalendar_component
                out.append((comp.get("dtstart").dt, str(comp.get("summary"))))
            except Exception:
                continue
    out.sort(key=lambda x: str(x[0]))
    return out


def briefing_text(days: int = 1) -> str:
    evs = list_events(days)
    if not evs:
        return "Keine Termine im angefragten Zeitraum."
    lines = []
    for start, summ in evs:
        t = start.strftime("%d.%m. %H:%M") if hasattr(start, "strftime") else str(start)
        lines.append(f"- {t}: {summ}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "add" and len(argv) >= 4:
        fmt = "%Y-%m-%d %H:%M"
        add_event(argv[1], datetime.strptime(argv[2], fmt), datetime.strptime(argv[3], fmt))
        print("Termin angelegt.")
    else:
        print(briefing_text(int(argv[0]) if argv else 7))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
