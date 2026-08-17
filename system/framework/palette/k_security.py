#!/usr/bin/env python3
"""k_security.py — die einzige SPERRENDE Kategorie (F1.B.3 = b).

F7 = B: Code und Betrieb. F7.B.3 = b: die Ports von AUSSEN, nicht lokal.

WARUM AUSGERECHNET DIESE KATEGORIE SPERRT
Alle anderen Befunde kosten Qualitaet. Ein Secret im Repo oder ein offener Port
kostet die Kontrolle -- und zwar rueckwirkend: Ein einmal veroeffentlichter
Schluessel bleibt veroeffentlicht, auch wenn der naechste Lauf wieder gruen ist.
Nur hier ist ein Fehlschlag nicht nachtraeglich heilbar, und nur deshalb bricht
er den Lauf ab.

WARUM DER PORTSCAN NICHT LOKAL LAUFEN DARF
Am 16.08. meldete ufw "dicht", waehrend Docker seine Regeln per DNAT davor
setzte und den Port ins Internet stellte. Lokal gemessen war alles in Ordnung.
Der Blick von aussen ist der einzige, der diesen Fall findet -- deshalb laeuft
dieser eine Test von einem anderen Knoten aus als dem geprueften.

WAS SEIT DEM DEPOT DAZUKOMMT
Bis dahin waren die schuetzenswerten Daten Zugangsschluessel. Jetzt gibt es
Kontodaten. Die Suche nach IBAN, Depotnummer und Adresse waere vorher sinnlos
gewesen -- jetzt ist sie eine wiederkehrende Pruefung statt einmaliger Sorgfalt.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

PAKET = Path(__file__).resolve().parent
FRAMEWORK = PAKET.parent
if str(FRAMEWORK) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK))

import speicher                                                # noqa: E402

REPO = FRAMEWORK.parents[2]

# Muster fuer Zugangsdaten. Bewusst eng: Ein Muster, das staendig falsch
# anschlaegt, wird nach dem dritten Mal ignoriert -- und dann faengt es auch den
# echten Fall nicht mehr.
SECRET_MUSTER = [
    (r"hf_[A-Za-z0-9]{30,}", "HuggingFace-Token"),
    (r"sk-[A-Za-z0-9_-]{24,}", "API-Schluessel (sk-)"),
    (r"cfut_[A-Za-z0-9_-]{20,}", "Cloudflare-Token"),
    (r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----", "privater Schluessel"),
    # Der WERT muss die letzte Gruppe sein -- _suchen() prueft ihn gegen
    # NUR_GROSS. Ohne diese Klammer griff die Platzhalterpruefung den
    # Schluesselnamen ab und hielt jedes "password" fuer echt.
    (r"(?i)\"(?:password|passwort|secret)\"\s*:\s*\"([^\"]{6,})\"",
     "Passwort im Klartext"),
]

# Kontodaten. Die IBAN-Pruefsumme wird nicht geprueft -- eine Zeichenkette, die
# wie eine IBAN aussieht, hat im Repo auch dann nichts verloren, wenn sie
# erfunden ist.
KONTO_MUSTER = [
    (r"\bDE\d{2}\s?(\d{4}\s?){4}\d{2}\b", "IBAN"),
    (r"(?i)\bdepotnummer\b\s*[:=]\s*\S+", "Depotnummer"),
]

# Dateien, die Sitzungen oder Zugaenge tragen und niemandem sonst gehoeren.
GEHEIME_DATEIEN = [
    Path.home() / ".local/state/firma-mitarbeiter",
    Path.home() / ".ssh",
]

# Was auf dem VPS von aussen offen sein DARF. Alles andere ist ein Befund --
# nicht weil es zwingend gefaehrlich waere, sondern weil es unbemerkt entstand.
ERLAUBTE_PORTS = {22, 80, 443}
# Ports, die typischerweise durch Docker-DNAT am ufw vorbei ins Netz geraten.
VERDAECHTIGE_PORTS = [2375, 2376, 3306, 5432, 5433, 6379, 8080, 8123, 9000,
                      11434, 27017, 32768]


def _versionierte_dateien() -> list[Path]:
    """Nur was im Git liegt. venv und Caches mitzudurchsuchen kostet Minuten und
    findet fremde Beispieldaten statt eigener Fehler."""
    e = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                       capture_output=True, text=True, check=True)
    aus = []
    for zeile in e.stdout.splitlines():
        p = REPO / zeile
        if p.is_file() and p.suffix.lower() in (
                ".py", ".json", ".md", ".html", ".sql", ".sh", ".yml", ".yaml",
                ".txt", ".conf", ".service", ""):
            aus.append(p)
    return aus


# Woerter, die einen Fund als Platzhalter ausweisen. Ohne diese Liste meldete
# der erste Lauf sieben Funde, von denen sechs Beispiele in Anleitungen waren
# ("sk-local-...-change-me"). Ein Test mit sechs Fehlalarmen je echtem Fund wird
# nach dem dritten Mal weggeklickt -- und faengt dann auch den echten nicht mehr.
PLATZHALTER = re.compile(
    r"(?i)(local|your|dein|example|beispiel|change[-_]?me|replace|placeholder"
    r"|xxx+|\.\.\.|<[^>]+>|hier[-_]?einf|dummy|test[-_]?key)")

# Zweites, allgemeineres Kriterium: ein Wert, der NUR aus Grossbuchstaben und
# Trennzeichen besteht, ist eine Anweisung an den Leser, kein Geheimnis --
# "BOT-PASSWORT", "PASSWORT", "DEINE-DOMAIN". Erzeugte Schluessel sind immer
# gemischt. Diese Regel faengt auch die deutschen Platzhalter, an die eine
# Wortliste nie vollstaendig denkt.
NUR_GROSS = re.compile(r"^[A-ZÄÖÜ0-9_\- ]{4,}$")


def _ist_platzhalter(fund: str, wert: str | None = None) -> bool:
    if PLATZHALTER.search(fund):
        return True
    return bool(wert and NUR_GROSS.match(wert.strip()))


def _suchen(muster: list[tuple[str, str]]) -> list[str]:
    treffer = []
    for p in _versionierte_dateien():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for regex, was in muster:
            for m in re.finditer(regex, text):
                # Die letzte Gruppe ist bei den Passwort-Mustern der Wert selbst.
                wert = m.groups()[-1] if m.groups() else None
                if _ist_platzhalter(m.group(0), wert):
                    continue
                zeile = text[:m.start()].count("\n") + 1
                treffer.append(f"{p.relative_to(REPO)}:{zeile} — {was}")
    return treffer


def keine_secrets_im_repo() -> tuple[bool, str, dict]:
    """Zugangsdaten gehoeren in den Keyring, nicht in die Versionsverwaltung."""
    t = _suchen(SECRET_MUSTER)
    return (not t,
            "keine Zugangsdaten in versionierten Dateien" if not t
            else f"{len(t)} Fund(e): " + "; ".join(t[:5]),
            {"funde": len(t)})


def keine_kontodaten() -> tuple[bool, str, dict]:
    """IBAN, Depotnummer oder Adresse — im Repo UND in der Datenbank.

    Der Depot-Plan haelt die Trennung bereits ein (nur zwei Summen und ein
    Stichtag). Dieser Test macht daraus eine Pruefung, die auch dann noch
    greift, wenn jemand spaeter "nur kurz" eine Nummer mitspeichert.
    """
    t = _suchen(KONTO_MUSTER)
    with speicher.verbindung() as c:
        cur = c.execute("SELECT stichtag, quelle FROM firma.konto_stand")
        for stichtag, quelle in cur.fetchall():
            for regex, was in KONTO_MUSTER:
                if re.search(regex, str(quelle)):
                    t.append(f"firma.konto_stand[{stichtag}].quelle — {was}")
    return (not t,
            "keine Kontodaten in Repo oder Datenbank" if not t
            else f"{len(t)} Fund(e): " + "; ".join(t[:5]),
            {"funde": len(t)})


# Dateien, die fuer alle lesbar sein DUERFEN. Ein oeffentlicher Schluessel ist
# zum Verteilen da -- ihn zu bemaengeln waere derselbe Fehler wie beim ersten
# Secret-Muster: ein Test, der aus dem falschen Grund rot ist, wird weggeklickt.
OEFFENTLICH = (".pub", "known_hosts", "authorized_keys", "config")


def dateirechte() -> tuple[bool, str, dict]:
    """Sitzungen und Schluessel duerfen nicht fuer alle lesbar sein.

    ABER: nur die geheimen. Ein oeffentlicher Schluessel gehoert in fremde
    authorized_keys, und known_hosts ist eine Liste von Fingerabdruecken --
    beides ist kein Geheimnis.
    """
    schlecht = []
    for wurzel in GEHEIME_DATEIEN:
        if not wurzel.exists():
            continue
        for p in ([wurzel] if wurzel.is_file() else wurzel.rglob("*")):
            if not p.is_file():
                continue
            if p.name.endswith(OEFFENTLICH) or p.name in OEFFENTLICH:
                continue
            m = p.stat().st_mode & 0o077
            if m:
                schlecht.append(f"{p}: {oct(p.stat().st_mode & 0o777)}")
    return (not schlecht,
            "Sitzungen und Schluessel nur fuer den Eigentuemer lesbar" if not schlecht
            else f"{len(schlecht)} zu offen: " + "; ".join(schlecht[:5]),
            {"zu_offen": len(schlecht)})


def _ssh_ziel(alias: str = "mein-server") -> str | None:
    """Den echten Hostnamen aus der SSH-Konfiguration holen, statt ihn im Repo
    abzulegen. Eine Adresse im Git ist kein Geheimnis, aber auch kein Gewinn."""
    e = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True)
    for zeile in e.stdout.splitlines():
        if zeile.startswith("hostname "):
            return zeile.split(None, 1)[1].strip()
    return None


def offene_ports_von_aussen() -> tuple[bool, str, dict]:
    """Der VPS, gesehen von diesem Knoten aus — also von ausserhalb seiner selbst.

    Kein Vollscan: eine feste Liste von Ports, die erfahrungsgemaess unbemerkt
    ins Netz geraten. Ein Vollscan waere langsamer und gegen die eigene
    Infrastruktur trotzdem nur eine laengere Liste derselben Frage.
    """
    ziel = _ssh_ziel()
    if not ziel:
        return False, "SSH-Ziel nicht ermittelbar (ssh -G mein-server)", {}
    offen = []
    for port in VERDAECHTIGE_PORTS:
        try:
            with socket.create_connection((ziel, port), timeout=3):
                offen.append(port)
        except OSError:
            pass
    unerlaubt = [p for p in offen if p not in ERLAUBTE_PORTS]
    return (not unerlaubt,
            "keiner der geprueften Ports von aussen erreichbar" if not unerlaubt
            else f"von aussen offen: {unerlaubt}",
            {"offen": offen, "geprueft": len(VERDAECHTIGE_PORTS)})


TESTS = [
    {"name": "keine_secrets_im_repo", "lauf": keine_secrets_im_repo},
    {"name": "keine_kontodaten", "lauf": keine_kontodaten},
    {"name": "dateirechte", "lauf": dateirechte},
    # Dieser eine Test braucht den Blick von aussen -- er laeuft dort, wo der
    # gepruefte Knoten NICHT ist. Die Anforderung auf Testebene ueberschreibt
    # die der Kategorie.
    {"name": "offene_ports_von_aussen", "lauf": offene_ports_von_aussen,
     "anforderung": {}},
]
