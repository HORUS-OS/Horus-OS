#!/usr/bin/env python3
"""dispatcher.py — wo laeuft welcher Agent?

Schritt 4 des Plans wandernde-werkbank. Loest ab, was frueher als `laeuft_auf`
in der Personalakte stand: statt eines Geraetenamens traegt eine Akte jetzt
`anforderungen` (braucht_gpu, vram_gb) — was der Agent BRAUCHT, nicht wo er
steht. Dieses Modul matcht das gegen hosts.json.

Warum das eine Verbesserung ist: `laeuft_auf` nannte vier verschiedene Geraete,
waehrend in Wirklichkeit alle vier Agenten auf dem Haupt-PC liefen. Kein Code
las das Feld je aus. Eine Anforderung dagegen ist pruefbar.

Bewusst OHNE Modell-Aufruf und ohne Zustand: Die Wahl muss VOR dem Denken
feststehen und billig sein — dieselbe Ueberlegung wie in passung.py, wo ein
LLM-Aufruf zur Sortierung von LLM-Aufrufen als teurer Zirkelschluss verworfen
wurde.

Selbstauskunft:  venv/bin/python dispatcher.py [agent_id]
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
MITARBEITER = FRAMEWORK.parent
HOSTS_DATEI = FRAMEWORK / "hosts.json"

# Reihenfolge der Rollen bei sonst gleicher Eignung. Ein Notfallknoten wird nur
# genommen, wenn nichts Besseres da ist -- er rechnet ohne GPU und ist langsam.
ROLLEN_RANG = {"rechenknoten": 0, "leichtgewicht": 1, "dispatcher, notfallknoten": 2}


def inventar() -> dict:
    return json.loads(HOSTS_DATEI.read_text(encoding="utf-8")).get("hosts", {})


def akte(agent_id: str) -> dict:
    return json.loads((MITARBEITER / agent_id / "personalakte.json").read_text(encoding="utf-8"))


def adresse(host: dict, timeout: float = 2.0) -> str | None:
    """Unter welcher Adresse ist dieser Knoten JETZT zu erreichen?

    Das Mesh hat Vorrang: Es funktioniert auch, wenn ein Geraet an einem fremden
    Router haengt. Ist es dort nicht angemeldet, gilt ersatzweise die
    LAN-Adresse -- besser ein Knoten im Heimnetz als gar keiner. Genau dieser
    Fall trat am 17.08. ein: PC 2 hatte SSH und Ollama, aber noch kein
    WireGuard, und waere sonst wochenlang ungenutzt geblieben.

    Rueckgabe: die brauchbare Adresse oder None.
    """
    port = int(host.get("pruef_port", 22))
    for schluessel in ("mesh_ip", "lan_ip"):
        ip = host.get(schluessel)
        if not ip:
            continue
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            continue
    return None


def erreichbar(host: dict, timeout: float = 2.0) -> bool:
    """Live-Korrektur des Inventars: Ein Knoten, der nicht antwortet, kommt
    nicht in die Auswahl — egal was die Datei behauptet.

    Geprueft wird ein Port, den der Host WIRKLICH anbietet (`pruef_port`). Der
    erste Entwurf nahm pauschal SSH:22 und erklaerte damit ausgerechnet den
    Haupt-PC fuer tot: Der ist SSH-Client, kein Server."""
    return adresse(host, timeout) is not None


def eigener_knoten() -> str:
    """Der Name DIESES Rechners im Inventar.

    Rechnername und Knotenname sind nicht dasselbe -- der Haupt-PC heisst
    beispiel-pc. Wer beides gleichsetzt, sucht sich selbst
    vergeblich im Inventar und haelt sich fuer einen fremden Knoten.

    FIRMA_KNOTEN geht vor, damit ein Container sich ausweisen kann.
    """
    gesetzt = os.environ.get("FIRMA_KNOTEN")
    if gesetzt:
        return gesetzt
    rechner = socket.gethostname()
    for name, host in inventar().items():
        if host.get("hostname") == rechner or name == rechner:
            return name
    return rechner


def passend(anf: dict, host: dict, *, agent: bool = True) -> tuple[bool, str]:
    """Erfuellt der Host die Anforderungen? Rueckgabe (ja/nein, Begruendung).

    `agent=False` fuer Arbeit, die kein vollstaendiger Agent ist -- ein
    Prueflauf der Testpalette etwa laeuft auch dort, wo kein Agent leben kann.
    Genau deshalb ist der Pi Zero fuer den ARM-Test brauchbar und fuer Projektleitung
    nicht.
    """
    # "Braucht keine GPU" heisst nicht "laeuft ueberall": Ein Agent braucht
    # Matrix-Client, Prompt-Kontext und Datenbankzugriff. Der Pi Zero erfuellt
    # formal jede GPU-freie Anforderung und wuerde daran trotzdem scheitern.
    if agent and not host.get("agenten_tauglich", True):
        return False, "traegt keine vollstaendigen Agenten"
    if anf.get("braucht_gpu") and not host.get("gpu"):
        return False, "keine GPU"
    noetig = float(anf.get("vram_gb") or 0)
    if noetig and float(host.get("vram_gb") or 0) < noetig:
        return False, f"VRAM {host.get('vram_gb', 0)} < {noetig:g} GB"
    # Der Portscan muss von aussen kommen. Lokal gemessen war der Port "dicht",
    # waehrend Docker-DNAT ihn am ufw vorbei ins Internet stellte -- der Fehler
    # vom 16.08., der genau deshalb still war.
    if anf.get("braucht_aussensicht") and not host.get("aussensicht"):
        return False, "steht im selben Netz, sieht die Firma nicht von aussen"
    verlangt = anf.get("arch")
    if verlangt and host.get("arch") != verlangt:
        return False, f"Architektur {host.get('arch') or 'unbekannt'} != {verlangt}"
    return True, "erfuellt"


def knoten_fuer(anf: dict, *, agent: bool = True,
                pruefe_erreichbarkeit: bool = True) -> list[tuple[str, str]]:
    """Geeignete Hosts fuer FREIE Anforderungen, beste Wahl zuerst.

    Damit koennen auch Auftraggeber ohne Personalakte den Dispatcher nutzen --
    die Testpalette etwa. Eine zweite Zuordnungslogik daneben waere genau der
    Fehler, der schon einmal teuer war: zwei Wahrheiten fuer dieselbe Frage.
    """
    treffer = []
    for name, host in inventar().items():
        ok, grund = passend(anf, host, agent=agent)
        if not ok:
            continue
        if pruefe_erreichbarkeit and not erreichbar(host):
            continue
        treffer.append((name, host, grund))
    treffer.sort(key=lambda t: (ROLLEN_RANG.get(t[1].get("rolle", ""), 9),
                                -float(t[1].get("vram_gb") or 0),
                                -int(t[1].get("kerne") or 0)))
    return [(n, g) for n, _h, g in treffer]


def kandidaten(agent_id: str, **kw) -> list[tuple[str, str]]:
    """Alle geeigneten Hosts fuer einen Agenten, beste Wahl zuerst."""
    return knoten_fuer(akte(agent_id).get("anforderungen") or {}, **kw)


def waehle(agent_id: str, **kw) -> str | None:
    k = kandidaten(agent_id, **kw)
    return k[0][0] if k else None


def _bericht() -> None:
    inv = inventar()
    print(f"Hosts im Inventar: {', '.join(inv)}\n")
    for name, h in inv.items():
        zustand = "erreichbar" if erreichbar(h) else "NICHT erreichbar"
        gpu = f"GPU {h.get('vram_gb')} GB" if h.get("gpu") else "keine GPU"
        print(f"  {name:10} {h.get('mesh_ip'):10} {gpu:14} {h.get('kerne')} Kerne  {zustand}")
    print()
    for p in sorted(MITARBEITER.glob("*/personalakte.json")):
        aid = p.parent.name
        a = json.loads(p.read_text(encoding="utf-8"))
        anf = a.get("anforderungen") or {}
        k = kandidaten(aid)
        wahl = k[0][0] if k else "— kein geeigneter Host"
        rest = f"  (auch moeglich: {', '.join(n for n, _ in k[1:])})" if len(k) > 1 else ""
        print(f"  {aid:10} braucht gpu={str(bool(anf.get('braucht_gpu'))):5} "
              f"vram={anf.get('vram_gb', 0):>2} GB  ->  {wahl}{rest}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        aid = sys.argv[1]
        for n, g in kandidaten(aid):
            print(f"  {n}: {g}")
        print(f"\n  Wahl: {waehle(aid)}")
    else:
        _bericht()
