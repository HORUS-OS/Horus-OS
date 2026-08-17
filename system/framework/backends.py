#!/usr/bin/env python3
"""Denk-Backends für die Mitarbeiter-Agenten.

Aktuell: lokales Ollama (qwen3.6:35b-a3b, MoE). Die Funktion get_backend() ist die NAHT, an
der später der Buchhalter-Broker die Kosten-Kaskade (lokal -> Mammouth -> Claude
-> API) einklinkt — die Agenten selbst bleiben unverändert.
"""
from __future__ import annotations

import re

import requests

import denkzeit

import passung

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class Backend:
    def generate(self, system_prompt: str, history: list[dict],
                 quellen: list[str] | None = None, raum: str = "") -> str:
        """`quellen` = woraus der Prompt zusammengesetzt ist (Kalender, Archiv, …).
        Der Privat-Filter im Broker urteilt danach, ob der Gedanke das Haus
        verlassen darf — die Herkunft kennt nur der Agent, der sie eingehängt hat."""
        raise NotImplementedError


# Ollama fällt ohne diese Angabe auf 4096 Token zurück — der System-Prompt
# (Persona + Kodex + Kalender) lief damit über und die Anti-Redundanz-Regel wurde
# abgeschnitten, bevor das Modell sie las.
#
# Seit dem Umstieg auf Qwen3.6-35B-A3B (MoE, 40 Layer, Q4_K_M) am 2026-08-10
# getestet: 5 Kombinationen aus num_gpu (wie viele Layer im VRAM bleiben) und
# num_ctx gegeneinander gemessen (VRAM-Peak + Tokens/s, RTX 4060 Ti 16 GB).
# num_gpu=999 (alle 41 Layer forciert) und num_gpu=28 bei ctx=16384 liefen beide
# in ein CUDA-OOM — das markiert die reale Obergrenze. Gewählt wurde der
# Kompromiss aus 24 GPU-Layern + 32768 Kontext: praktisch dieselbe Tokens/s-Rate
# wie Ollamas eigene Auto-Aufteilung (14,6 vs. 14,65 Tok/s), aber 8x mehr Kontext
# als die bisherigen 4096. Kostet fast das gesamte VRAM (15,6/16,38 GB) — lässt
# kaum Puffer für gleichzeitiges ComfyUI, siehe Schritt 13 im Archiv-Plan.
NUM_CTX = 32768
NUM_GPU = 24


class LocalOllama(Backend):
    """Direkt gegen Ollama, ohne Broker.

    Kein Gesamt-Timeout mehr (frueher 120 s): abgebrochen wird nur bei Stillstand,
    Gruebeln oder Notbremse — siehe denkzeit.py. Ein langer Gedankengang ist kein
    Fehler, sondern auf dieser Maschine der Normalfall.
    """

    def __init__(self, url: str, model: str = "qwen3.6:35b-a3b",
                 stall_s: int = denkzeit.STALL_S, max_s: int = denkzeit.MAX_S,
                 num_ctx: int = NUM_CTX, num_gpu: int = NUM_GPU):
        self.url = url.rstrip("/")
        self.model = model
        # Diese beiden gehoeren zum KNOTEN, nicht zur Firma. Die Vorgaben sind
        # fuer 16 GB VRAM gemessen; auf einem 4-GB-Knoten sprengen sie den
        # Speicher, und Ollama antwortet mit einem nackten 500er -- der Aufrufer
        # sieht dann "Verbindung zum Modell" und sucht am falschen Ende.
        self.num_ctx = num_ctx
        self.num_gpu = num_gpu
        self.stall_s = stall_s
        self.max_s = max_s

    def generate(self, system_prompt: str, history: list[dict],
                 quellen: list[str] | None = None, raum: str = "") -> str:
        messages = [{"role": "system", "content": system_prompt}, *history]
        try:
            m = denkzeit.chat(
                f"{self.url}/api/chat",
                {"model": self.model, "messages": messages,
                 "options": {"num_ctx": self.num_ctx, "num_gpu": self.num_gpu}},
                stall_s=self.stall_s, max_s=self.max_s)
        except denkzeit.Abbruch as ab:
            # Teiltext behalten — er ist mehr wert als eine leere Fehlermeldung.
            return denkzeit.mit_notiz(_THINK.sub("", ab.teiltext).strip(), ab.grund)
        return _THINK.sub("", m.get("content", "")).strip()  # qwen3-Denkspuren entfernen


class BuchhalterBackend(Backend):
    """Routet das Denken über den Buchhalter-Broker (GPU-Tor + Kontingente)."""

    # Timeout großzügig: Der Broker arbeitet die Warteschlange seriell ab, und mit
    # der Neubewertung bei jeder neuen Raumnachricht stehen bei vier Agenten
    # deutlich mehr Aufträge an. Bei 320 s liefen die hinteren in einen Fehler,
    # statt einfach später dranzukommen (Chef: „dann dauert es eben").
    #
    # Seit denkzeit.py bricht der Broker selbst nur noch bei Stillstand, Grübeln
    # oder Notbremse ab. Dieser Wert muss deshalb ÜBER der brokerseitigen
    # Notbremse liegen plus Wartezeit in der Schlange — sonst gibt der Agent auf,
    # während der Broker noch sauber arbeitet.
    #
    # ABGELEITET statt fest eingetragen: Hier stand 5400, passend zu einer
    # Notbremse von 3600. Als diese am 16.08. auf 4 Stunden stieg, wäre der Wert
    # still falsch geworden — der Agent hätte nach 90 Minuten aufgegeben, obwohl
    # der Broker noch 2,5 Stunden Zeit gehabt hätte. Genau diese Sorte stiller
    # Kopplung hat schon den Tagesdeckel gekostet.
    def __init__(self, url: str, agent_id: str,
                 timeout: int = denkzeit.MAX_S + 1800,
                 akte: dict | None = None):
        self.url = url.rstrip("/")
        self.agent_id = agent_id
        self.timeout = timeout
        self.akte = akte or {"agent_id": agent_id}

    def _passung(self, history: list[dict]) -> tuple[int, int]:
        """Fachliche Passung zur letzten Frage — bestimmt den Platz in Buchhalters
        Warteschlange. Hier berechnet und nicht im Agenten, damit jeder Aufrufer
        automatisch davon profitiert, ohne seine Signatur zu ändern."""
        frage = next((m.get("content", "") for m in reversed(history)
                      if m.get("role") == "user"), "")
        if not frage:
            return 0, 0
        return passung.passung(frage, self.akte), passung.losnummer(frage, self.agent_id)

    def generate(self, system_prompt: str, history: list[dict],
                 quellen: list[str] | None = None, raum: str = "") -> str:
        pass_wert, los = self._passung(history)
        r = requests.post(
            f"{self.url}/think",
            json={"agent_id": self.agent_id, "system": system_prompt,
                  "messages": history, "quellen": quellen or [],
                  "klassifiziert": True,   # Agenten-Prompts tragen immer die Persona
                  "raum": raum,            # Werkzeuge brauchen den Raum (Vorgänge)
                  "passung": pass_wert, "losnummer": los},
            timeout=self.timeout,
        )
        try:
            d = r.json()   # Broker liefert auch bei Fehler ein JSON mit 'error'
        except Exception:
            return f"(Buchhalter nicht erreichbar: HTTP {r.status_code})"
        if "reply" not in d:
            return f"(Buchhalter meldet: {d.get('error', 'unbekannt')})"
        # Hat der Agent gehandelt, aber nichts darüber gesagt? Dann das Ergebnis
        # anhängen — sonst bleibt die Tat unsichtbar und Chef weiß nicht, ob sie
        # wirklich passiert ist.
        reply = d["reply"]
        for a in d.get("aktionen") or []:
            erg = a.get("ergebnis", "")
            if erg and erg[:40] not in reply:
                reply = f"{reply}\n\n_({erg})_" if reply else erg
        return reply


def get_backend(cfg: dict, akte: dict) -> Backend:
    """Ist ein Buchhalter-Broker konfiguriert, läuft alles über ihn (einziges GPU-Tor).
    Sonst direkt lokal (z.B. Trockenbetrieb ohne Broker)."""
    url = cfg.get("buchhalter_url")
    if url:
        return BuchhalterBackend(url, akte.get("agent_id", "_default"), akte=akte)
    model = akte.get("laufzeit_modell") or cfg.get("default_model", "qwen3.6:35b-a3b")
    return LocalOllama(cfg["ollama_url"], model)
