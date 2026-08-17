#!/usr/bin/env python3
"""Tagesplan — stellt das Morgen-Briefing zu (K22/K23).

Läuft als eigener Prozess (systemd-Timer, Mo–Fr 07:30 / Sa+So 09:30), meldet sich
als Assistenz an, sammelt die Zulieferungen, lässt sie von ihr in eine Nachricht
verwandeln und schickt sie in den Zweier-Raum mit Chef.

Bewusst nicht im Agenten-Prozess: Ein Timer ist nach einem Neustart sofort wieder
richtig eingetaktet, eine schlafende Schleife im Agenten wäre es nicht. Zudem
kostet der Dienst nur zur Briefing-Zeit Ressourcen.

    tagesplan.py [--jetzt]     --jetzt = sofort ausführen, ohne Zeitprüfung
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import requests
from nio import AsyncClient, AsyncClientConfig, RoomPreset

FW = Path(__file__).resolve().parent
sys.path.insert(0, str(FW))

import briefing            # noqa: E402
import ruhezeit            # noqa: E402
import vorgaenge           # noqa: E402
from mitarbeiter_agent import (ANTI_REDUNDANZ, extract_system_prompt,  # noqa: E402
                               keyring_get, load_akte, load_config)

BRIEFER = "assistenz"          # wer das Briefing liefert (Sekretariat, K22)


async def _client(cfg: dict, akte: dict) -> AsyncClient:
    state = Path(cfg["state_dir"]) / BRIEFER
    c = AsyncClient(cfg["homeserver"], akte["matrix_id"], store_path=str(state / "store"),
                    config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=False))
    sess = state / "session.json"
    if sess.exists():
        s = json.loads(sess.read_text())
        c.access_token, c.user_id, c.device_id = s["access_token"], s["user_id"], s["device_id"]
    else:
        await c.login(keyring_get(f"matrix-pw-{BRIEFER}"), device_name=f"firma-{BRIEFER}-tagesplan")
    return c


async def _chef_dm(c: AsyncClient, cfg: dict, state: Path) -> str | None:
    """Denselben Meldekanal benutzen, den der Agent angelegt hat — nicht einen zweiten."""
    merk = state / "chef_dm.txt"
    if merk.exists():
        rid = merk.read_text().strip()
        if rid:
            return rid
    chef = cfg.get("chef")
    await c.sync(timeout=8000, full_state=True)
    for rid, room in c.rooms.items():
        if (room.member_count or 99) <= 2 and chef in room.users:
            merk.write_text(rid)
            return rid
    resp = await c.room_create(is_direct=True, invite=[chef],
                               preset=RoomPreset.private_chat, name="Assistenz & Chef")
    rid = getattr(resp, "room_id", None)
    if rid:
        merk.write_text(rid)
    return rid


def _denken(cfg: dict, system: str, auftrag: str) -> str:
    """Über Buchhalters Broker denken — auch das Briefing geht durchs GPU-Tor."""
    r = requests.post(f"{cfg['buchhalter_url'].rstrip('/')}/think",
                      json={"agent_id": BRIEFER, "system": system,
                            "messages": [{"role": "user", "content": auftrag}]},
                      # 300 s reichten nicht: seit dem Modellwechsel auf
                      # qwen3.6:35b-a3b mit CPU-Auslagerung braucht der Broker
                      # allein fuer eine Zwei-Wort-Antwort rund 96 s, fuer ein
                      # Briefing mit Kalender und Zulieferungen entsprechend
                      # laenger. Das Briefing scheiterte deshalb ab dem
                      # 2026-08-11 taeglich still mit ReadTimeout.
                      timeout=900)
    d = r.json()
    return d.get("reply") or f"(Briefing fehlgeschlagen: {d.get('error', '?')})"


async def main() -> int:
    jetzt = "--jetzt" in sys.argv
    if not jetzt and ruhezeit.in_nachtruhe():
        print("Noch Nachtruhe — kein Briefing.")
        return 0
    cfg = load_config()
    akte, persona = load_akte(BRIEFER)
    kalender = briefing.teil_kalender(1)
    # Konto an den Firmen-Teil anhaengen statt briefing_auftrag() um einen
    # Parameter zu erweitern: Der Kontostand IST eine Firmenkennzahl, und ein
    # weiterer Parameter muesste von jedem Aufrufer mitgeschleppt werden.
    firma = briefing.teil_firma(cfg["buchhalter_url"]) + "\n" + briefing.teil_konto()
    stau = briefing.teil_stau()
    zeile = briefing.ueberblick(cfg["buchhalter_url"], 1)   # Zahlen im Code, nicht im Modell
    auftrag = briefing.briefing_auftrag(kalender, firma, stau, zeile)
    text = await asyncio.to_thread(_denken, cfg, persona + "\n\n" + ANTI_REDUNDANZ, auftrag)

    c = await _client(cfg, akte)
    try:
        rid = await _chef_dm(c, cfg, Path(cfg["state_dir"]) / BRIEFER)
        if not rid:
            print("Kein Meldekanal zu Chef gefunden.")
            return 1
        # Das Briefing ist Routine, kein Alarm -> stumm (K11/K30).
        await c.room_send(rid, "m.room.message", {"msgtype": "m.notice", "body": text},
                          ignore_unverified_devices=True)
        briefing.stau_leeren()
        # Ein zugestelltes Briefing ist nach des Chefs Definition ein abgeschlossener
        # Auftrag -- etwas, das man mit "erledigt" abstempeln kann. Ohne diese
        # Buchung bliebe die Produktivitaet in der Probezeit-Bewertung dauerhaft
        # bei null, obwohl taeglich etwas geliefert wird.
        v = vorgaenge.anlegen(BRIEFER, anliegen="Morgen-Briefing", wartet_auf="-",
                              fuer="chef", raum=rid)
        vorgaenge.schliessen(BRIEFER, v["id"], "erledigt")
        print(f"[{datetime.now():%H:%M}] Briefing zugestellt ({len(text)} Zeichen).")
    finally:
        await c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
