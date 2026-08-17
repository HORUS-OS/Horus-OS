#!/usr/bin/env python3
"""scheduler.py — die Gegenstelle zum Worker, laeuft auf dem VPS.

Letzter Baustein von Schritt 4 des Plans wandernde-werkbank. Der Broker war
bisher beides in einem: Warteschlange UND Ausfuehrung, beides auf dem Haupt-PC.
Faellt der aus, ist die Firma kopflos.

Die Spaltung:
  * SCHEDULER (hier, VPS)  nimmt Auftraege an, stellt sie in die Queue, wartet
                           auf das Ergebnis und beaufsichtigt, was haengt.
  * WORKER (Haupt-PC)      holt aus der Queue, rechnet auf der GPU, meldet
                           Herzschlag. Das ist der bestehende Broker mit
                           `queue_worker: true`.

Der Scheduler rechnet nichts. Er ueberlebt damit einen PC-Neustart, und ein
Auftrag, der waehrenddessen ankommt, wartet einfach in der Datenbank -- genau
das, was mit einer Warteschlange im Arbeitsspeicher nicht ging.

Die HTTP-Schnittstelle ist absichtlich deckungsgleich mit der des Brokers
(POST /think, GET /status), damit ein Agent nur seine `buchhalter_url` aendern
muss und sonst nichts.

Aufruf:  venv/bin/python scheduler.py     (Port aus scheduler.config.json)
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auftragsqueue as q
import speicher

HIER = Path(__file__).resolve().parent
CFG = json.loads((HIER / "scheduler.config.json").read_text(encoding="utf-8"))
HOST = CFG.get("listen_host", "10.0.0.1")
PORT = int(CFG.get("listen_port", 8910))
PRIOS = CFG.get("prioritaetsklassen", {})


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def _aufsicht():
    """Verwaiste Auftraege einsammeln. Die Zwei-Signal-Regel steckt in
    auftragsqueue.aufraeumen(): veralteter Herzschlag UND unerreichbarer Host.

    Hier ist ihr eigentlicher Platz -- der Scheduler laeuft ausserhalb der
    Maschine, die ausfallen kann. Ein Worker, der sich selbst beaufsichtigt,
    kann seinen eigenen Absturz nicht bemerken.
    """
    takt = int(CFG.get("aufsicht_takt_sekunden", 60))
    while True:
        try:
            for a in q.aufraeumen():
                log(f"Auftrag {a['id']} ({a['agent_id']}) neu aufgelegt — Host "
                    f"{a['host']} still seit {a['stille_s']} s und nicht erreichbar")
        except Exception as e:                              # noqa: BLE001
            log(f"Aufsicht: {e!r}")
        time.sleep(takt)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            try:
                self._send(200, q.stand())
            except Exception as e:                          # noqa: BLE001
                self._send(500, {"error": str(e)})
        elif self.path == "/health":
            self._send(200, {"ok": True, "rolle": "scheduler"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/think":
            return self._send(404, {"error": "not found"})
        try:
            # `or 0` einheitlich, auch wo es harmlos ist: Ein Header kann nicht
            # None sein. Aber eine Regel, die an drei von vier Stellen gilt, ist
            # keine Regel -- und beim naechsten Kopieren dieser Zeile entstuende
            # der Fehler neu.
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or "{}")
        except Exception as e:                              # noqa: BLE001
            return self._send(400, {"error": f"unlesbare Anfrage: {e}"})

        agent = b.get("agent_id") or "_unbekannt"
        # Nur gesetzte Felder ablegen. Ein mitgespeichertes null laesst auf der
        # Worker-Seite jeden int()-Aufruf scheitern — der Default eines
        # dict.get() greift nur bei FEHLENDEM Schluessel, nicht bei None.
        auftrag = {k: b[k] for k in
                   ("system", "messages", "model", "quellen", "klassifiziert",
                    "freigabe", "raum", "passung", "losnummer")
                   if b.get(k) is not None}
        try:
            aid = q.einstellen(agent, auftrag,
                               prioritaet=PRIOS.get(agent, 3),
                               passung=int(b.get("passung") or 0),
                               losnummer=int(b.get("losnummer") or 0))
        except Exception as e:                              # noqa: BLE001
            return self._send(503, {"error": f"Queue nicht erreichbar: {e}"})

        log(f"Auftrag {aid} von {agent} eingestellt (prio {PRIOS.get(agent, 3)})")
        erg = q.ergebnis_abwarten(aid, frist_s=int(CFG.get("auftrag_frist_sekunden", 5400)),
                                  takt_s=1.0)
        if erg is None:
            return self._send(500, {"error": "Auftrag verschwunden"})
        self._send(200 if "error" not in erg else 500, erg)

    def log_message(self, *a):      # stumm
        pass


def main():
    threading.Thread(target=_aufsicht, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"Scheduler hört auf {HOST}:{PORT} — Speicher: "
        f"{'Postgres' if speicher.aktiv() else 'NICHT AKTIV (Schalter prüfen!)'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
