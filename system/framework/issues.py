#!/usr/bin/env python3
"""issues.py — GitHub-Issues als Arbeitsvorrat, priorisiert nach Zustimmung.

Chef am 2026-08-17: "Technik bekommt einen Listener für die Issues. Und setzt sie
für beide Projekte Issues für Issue um. Priorisiert wird nach der Anzahl der
Daumen hoch / der Kommentare."

WARUM NACH ZUSTIMMUNG UND NICHT NACH EINGANGSDATUM
Ein Eingangsdatum sagt, wer zuerst geschrieben hat -- nicht, was gebraucht wird.
Reaktionen und Kommentare sind das einzige Signal, das von den Nutzern selbst
kommt. Es ist manipulierbar, aber bei einem kleinen Projekt ist die Alternative
schlechter: Dann entscheidet, wer am lautesten schreibt.

WIE GEZAEHLT WIRD
    punkte = daumen_hoch * 3 + andere_reaktionen + kommentare * 2
Ein Daumen ist ein bewusstes Votum und zaehlt dreifach. Ein Kommentar zaehlt
doppelt: Er kostet mehr Muehe als ein Klick, ist aber nicht immer Zustimmung --
auch Widerspruch kommentiert. Deshalb nicht hoeher als ein Daumen.

Daumen NACH UNTEN zaehlt negativ: Ein Vorschlag, den mehrere ablehnen, soll
nicht durch Diskussionslaenge nach vorn rutschen.

WAS DIESES MODUL NICHT TUT
Es schreibt nichts auf GitHub -- kein Kommentar, kein Schliessen, kein Label.
Lesen ist harmlos, Schreiben wirkt nach aussen und faellt damit unter den
Grundsatz "alles nach aussen Wirkende geht zu Chef". Was Technik aus einem Issue
macht, legt sie ihm als Entwurf vor.

Aufruf:
    issues.py                    offene Issues beider Projekte, sortiert
    issues.py --holen            in die Datenbank uebernehmen (fuer Techniks Vorrat)
    issues.py --repo owner/name  nur dieses
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
sys.path.insert(0, str(FRAMEWORK))

import speicher                                                # noqa: E402

# Beide Projekte. Das oeffentliche und das private -- Issues koennen in beiden
# stehen, und Technik soll sie in EINEM Vorrat sehen.
REPOS = ["HORUS-OS/HORUS-OS", "HORUS-OS/Horus-OS-Privat"]

API = "https://api.github.com"

# Gewichte, siehe Modulkopf.
GEWICHT_DAUMEN = 3
GEWICHT_KOMMENTAR = 2
GEWICHT_SONSTIGE = 1
# Zuschlag fuer einen Pull Request: Jemand hat schon gearbeitet und wartet.
# Bewusst moderat -- ein PR mit null Zustimmung soll nicht vor einem Issue mit
# zehn Daumen stehen.
BONUS_PULL = 5


def _token() -> str | None:
    """Token aus der Umgebung oder dem Keyring. Nie aus einer Datei im Repo.

    Reihenfolge: GH_TOKEN, GITHUB_TOKEN, dann der Keyring. Ohne Token gehen
    oeffentliche Issues trotzdem (60 Anfragen je Stunde) -- private nicht.
    """
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    for befehl in (["gh", "auth", "token"],
                   ["/usr/bin/python3",
                    os.path.expanduser("~/Horus-OS/system/scripts/horus-github-keyring.py"),
                    "get"]):
        try:
            e = subprocess.run(befehl, capture_output=True, text=True, timeout=15)
            if e.returncode == 0 and e.stdout.strip():
                return e.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


class Gebremst(Exception):
    """GitHub hat uns gebremst. Enthaelt, wie lange zu warten ist."""

    def __init__(self, sekunden: int, grund: str):
        super().__init__(grund)
        self.sekunden = sekunden


def _api(pfad: str, token: str | None) -> list | dict:
    """Eine Anfrage an die API.

    WARUM 429 EINE EIGENE AUSNAHME BEKOMMT
    Am 17.08. meldete GitHub Chef einen 429 -- nicht wegen dieser Anfragen (58
    von 60 waren frei), sondern wegen der Weboberflaeche. Trotzdem zeigte der
    Fall eine Luecke: Bei einer Bremse haette diese Schleife einfach weiter
    angefragt und die Sperre verlaengert. Wer gebremst wird und weiterdruckt,
    wird laenger gebremst.

    GitHub sagt in `Retry-After` oder `X-RateLimit-Reset`, wann es wieder geht.
    Diese Angabe zu ignorieren und selbst eine Zahl zu erfinden waere derselbe
    Fehler wie eine geratene Hardware-Angabe: Die Wahrheit steht in der Antwort.
    """
    kopf = {"Accept": "application/vnd.github+json",
            "User-Agent": "horus-os-issues"}
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(f"{API}{pfad}", headers=kopf)
    try:
        with urllib.request.urlopen(r, timeout=30) as a:
            return json.loads(a.read())
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            warte = 0
            nach = e.headers.get("Retry-After")
            if nach and str(nach).isdigit():
                warte = int(nach)
            else:
                reset = e.headers.get("X-RateLimit-Reset")
                if reset and str(reset).isdigit():
                    warte = max(0, int(reset) - int(time.time()))
            # 403 ohne Rate-Limit-Kopf ist "kein Zugriff", nicht "zu schnell" --
            # dann soll der Aufrufer seinen normalen Weg gehen.
            if warte or e.code == 429:
                raise Gebremst(warte or 3600,
                               f"GitHub bremst ({e.code}), Wartezeit {warte or 3600} s") from None
        raise


def punkte(issue: dict) -> int:
    """Zustimmung in einer Zahl. Siehe Modulkopf fuer die Gewichte."""
    r = issue.get("reactions") or {}
    hoch = int(r.get("+1") or 0)
    runter = int(r.get("-1") or 0)
    sonstige = sum(int(r.get(k) or 0)
                   for k in ("laugh", "hooray", "heart", "rocket", "eyes"))
    kommentare = int(issue.get("comments") or 0)
    return (hoch * GEWICHT_DAUMEN
            - runter * GEWICHT_DAUMEN
            + sonstige * GEWICHT_SONSTIGE
            + kommentare * GEWICHT_KOMMENTAR)


def holen(repos: list[str] | None = None, *, mit_pulls: bool = True) -> list[dict]:
    """Offene Issues UND Pull Requests beider Projekte, beste Zustimmung zuerst.

    Chef am 17.08.: "Ich denke Technik kann ihn fuer ihre Issues und Pulls
    ebenfalls brauchen."

    WARUM EIN PULL REQUEST ANDERS ZAEHLT
    Ein Issue ist ein Wunsch, ein PR ist bereits Arbeit -- jemand hat Zeit
    investiert und wartet. Das verdient Vorrang, unabhaengig von Daumen:
    Ein liegengebliebener PR verliert seinen Wert, weil der Code veraltet,
    waehrend ein Issue geduldig ist. Deshalb bekommt er einen Zuschlag.

    Er wird aber NICHT automatisch bevorzugt behandelt, sondern nur hoeher
    einsortiert -- was damit geschieht, entscheidet immer noch Chef.
    """
    token = _token()
    aus: list[dict] = []
    for repo in (repos or REPOS):
        try:
            daten = _api(f"/repos/{repo}/issues?state=open&per_page=100", token)
        except Gebremst:
            # Nach oben durchreichen: Die Wache muss davon WISSEN, um laenger zu
            # schlafen. Hier abzufangen hiesse, die Bremse zu verschweigen.
            raise
        except urllib.error.HTTPError as e:
            grund = ("kein Zugriff — privates Repo braucht einen Token"
                     if e.code in (401, 403, 404) and not token else str(e))
            print(f"  {repo}: {grund}", file=sys.stderr)
            continue
        except OSError as e:
            print(f"  {repo}: nicht erreichbar ({e})", file=sys.stderr)
            continue
        for i in daten if isinstance(daten, list) else []:
            ist_pull = bool(i.get("pull_request"))
            if ist_pull and not mit_pulls:
                continue
            aus.append({
                "repo": repo,
                "art": "pull" if ist_pull else "issue",
                "nummer": int(i["number"]),
                "titel": i.get("title") or "",
                "koerper": (i.get("body") or "")[:4000],
                "punkte": punkte(i) + (BONUS_PULL if ist_pull else 0),
                "kommentare": int(i.get("comments") or 0),
                "daumen": int((i.get("reactions") or {}).get("+1") or 0),
                "marken": [m.get("name") for m in (i.get("labels") or [])],
                "url": i.get("html_url") or "",
            })
    aus.sort(key=lambda x: (-x["punkte"], x["nummer"]))
    return aus


def uebernehmen(liste: list[dict]) -> tuple[int, int]:
    """In die Datenbank schreiben. Rueckgabe (neu, aktualisiert).

    Der Punktestand wird bei jedem Lauf nachgezogen: Zustimmung ist keine
    Eigenschaft des Issues, sondern eine Momentaufnahme. Ein Vorschlag, der
    heute zehn Daumen hat, hatte gestern vielleicht keinen -- und die Reihenfolge
    muss sich damit aendern duerfen.
    """
    neu = akt = 0
    with speicher.verbindung() as c:
        for i in liste:
            r = c.execute(
                "INSERT INTO firma.issue (repo, nummer, titel, koerper, punkte, "
                "daumen, kommentare, marken, url, art) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (repo, nummer) DO UPDATE SET "
                "titel = EXCLUDED.titel, koerper = EXCLUDED.koerper, "
                "punkte = EXCLUDED.punkte, daumen = EXCLUDED.daumen, "
                "kommentare = EXCLUDED.kommentare, marken = EXCLUDED.marken, "
                "art = EXCLUDED.art, gesehen = now() "
                "RETURNING (xmax = 0) AS eingefuegt",
                (i["repo"], i["nummer"], i["titel"], i["koerper"], i["punkte"],
                 i["daumen"], i["kommentare"], i["marken"], i["url"],
                 i.get("art", "issue"))).fetchone()
            if r and r[0]:
                neu += 1
            else:
                akt += 1
    return neu, akt


def naechstes() -> dict | None:
    """Das Issue mit der hoechsten Zustimmung, das noch offen ist."""
    with speicher.verbindung() as c:
        cur = c.execute(
            "SELECT * FROM firma.issue WHERE bearbeitet IS NULL "
            "ORDER BY punkte DESC, nummer ASC LIMIT 1")
        z = cur.fetchone()
        if not z:
            return None
        return dict(zip([d.name for d in cur.description], z))


def erledigt(repo: str, nummer: int, ergebnis: str) -> None:
    with speicher.verbindung() as c:
        c.execute("UPDATE firma.issue SET bearbeitet = now(), ergebnis = %s "
                  "WHERE repo = %s AND nummer = %s", (ergebnis[:2000], repo, nummer))


def text(liste: list[dict] | None = None) -> str:
    liste = liste if liste is not None else holen()
    if not liste:
        return "Keine offenen Issues (oder kein Zugriff — Token fehlt?)."
    zeilen = ["Offene Issues, nach Zustimmung sortiert:"]
    for i in liste[:15]:
        marke = "PR " if i.get("art") == "pull" else "   "
        zeilen.append(f"  [{i['punkte']:>3}] {marke}{i['repo'].split('/')[-1]}"
                      f"#{i['nummer']} {i['titel'][:66]}")
        zeilen.append(f"        {i['daumen']} Daumen, {i['kommentare']} Kommentare"
                      + (f", {', '.join(i['marken'])}" if i["marken"] else ""))
    return "\n".join(zeilen)


def haupt() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--holen", action="store_true", help="in die Datenbank uebernehmen")
    ap.add_argument("--repo", action="append", help="nur dieses Repo (mehrfach moeglich)")
    a = ap.parse_args()

    liste = holen(a.repo)
    print(text(liste))
    if a.holen and liste:
        neu, akt = uebernehmen(liste)
        print(f"\n{neu} neu, {akt} aktualisiert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(haupt())
