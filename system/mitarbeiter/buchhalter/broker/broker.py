#!/usr/bin/env python3
"""Buchhalter-Broker — einziges Tor zu GPU und Cloud-Budget der Firma.

- Prioritäts-Warteschlange (0=Chef … 4=Idle), ein Worker JE RECHENPLATZ.
  Verschiedene Agenten denken gleichzeitig, derselbe Agent nie zweimal.
- GPU-Sitzung: hält Bedarf an, entlädt LM Studio EINMAL, arbeitet ab, gibt die GPU
  nach Leerlauf an Hermes zurück (statt teurem Pro-Anfrage-Tausch).
- Buchhaltung: Aufrufe je Agent/Tag gegen Kontingente, Ledger persistent.
- Kosten-Kaskade: aktuell lokal (Ollama). Naht für Mammouth/Claude/API (TODO).

HTTP:  POST /think {agent_id, system, messages}      — lokal, mit Persona
       POST /eskaliere {agent_id, sachfrage, rolle} — Cloud, persona-frei
       GET  /status  ·  GET /praesenz  ·  POST /heartbeat
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import cloud
import privatfilter

CFG = json.loads((Path(__file__).resolve().parent / "buchhalter.config.json").read_text(encoding="utf-8"))
OLLAMA = CFG["ollama_url"].rstrip("/")
PRIOS = CFG["prioritaetsklassen"]
QUOTA = CFG["kontingente_pro_tag"]
GPU = CFG["gpu"]
LMS = os.path.expanduser(GPU["lms_bin"])
PREFLIGHT = os.path.expanduser(GPU["preflight"])
LEDGER = Path(os.path.expanduser(CFG["ledger"]))
IDLE = GPU["idle_release_sekunden"]

_pq: "queue.PriorityQueue" = queue.PriorityQueue()
_seq = 0
_seq_lock = threading.Lock()
_gpu_lock = threading.Lock()
_state = {"firm_holds_gpu": False, "last_activity": 0.0, "current": None, "done": 0}

# --- Rechenplaetze: mehrere Agenten gleichzeitig, jeder aber nur einmal ----
#
# Chef am 16.08.: "Sorge dafuer, dass sich zwei Mitarbeiter gleichzeitig die
# beiden PCs teilen. Unterschiedliche gleichzeitig ist in Ordnung, einer
# mehrfach gleichzeitig nicht."
#
# Das sind ZWEI Regeln, und sie brauchen zwei verschiedene Mechanismen:
#
#   1. GLEICHZEITIG  Ein Arbeiter je Rechenplatz statt einem insgesamt. Ein
#      Rechenplatz ist ein Knoten mit GPU und eigenem Ollama. Bisher gab es
#      genau einen Arbeiter -- die Schlange war seriell, und der zweite PC
#      haette nur zugesehen.
#
#   2. NICHT MEHRFACH  Eine Sperre je Agent. Sie ist noetig, weil zwei Arbeiter
#      sonst zwei Auftraege DESSELBEN Agenten parallel bearbeiten koennten --
#      zwei Antworten auf dieselbe Frage, zwei Werkzeugaufrufe, zwei Vorgaenge.
#      einmalig.py loest das NICHT: Es sperrt den Prozess auf einem Rechner,
#      nicht den Auftrag in der Schlange.
_AKTIVE_PLAETZE: list[dict] = []      # von main() gefuellt
_in_arbeit: set[str] = set()          # Agenten, die gerade denken
_in_arbeit_lock = threading.Lock()


def _greifen(agent_id: str) -> bool:
    """Platz fuer diesen Agenten beanspruchen. False = er denkt schon."""
    with _in_arbeit_lock:
        if agent_id in _in_arbeit:
            return False
        _in_arbeit.add(agent_id)
        return True


def _loslassen(agent_id: str) -> None:
    with _in_arbeit_lock:
        _in_arbeit.discard(agent_id)


_platz_locks: dict[str, threading.Lock] = {}


def _platz_lock(platz: dict) -> threading.Lock:
    """Ein Lock je Rechenplatz -- nicht eines fuer alle.

    Der lokale Platz behaelt bewusst `_gpu_lock`: An dem haengen auch der
    Idle-Waechter und die Uebergabe an LM Studio. Ein fremder Knoten hat damit
    nichts zu tun; wuerde er sich denselben Lock teilen, waeren wir wieder
    seriell -- also genau da, wo wir vorher waren.
    """
    if platz.get("lokal"):
        return _gpu_lock
    return _platz_locks.setdefault(platz["name"], threading.Lock())


_THINK = re.compile(r"<think>.*?</think>\s*", re.S)

# Präsenz: agent_id -> letztes Lebenszeichen (Unix-Zeit). Der Broker ist die
# natürliche Sammelstelle, weil ohnehin jeder Agent — auch auf einem Pi über den
# SSH-Tunnel — für jeden Gedanken hierher spricht.
# 10 Min ohne Zeichen = Ausfall. Untergrenze: das Dreifache des Sendetakts — sonst
# gilt eine gerade nicht gesendete Runde schon als Ausfall und es hagelt Fehlalarme.
_TAKT = CFG.get("heartbeat_sekunden", 120)
PRAESENZ_FRIST = max(CFG.get("praesenz_frist_sekunden", 600), 3 * _TAKT)
_praesenz: dict[str, float] = {}
_START = time.time()   # Startzeitpunkt dieses Brokers
_ausgefallen: set[str] = set()      # schon gemeldet — nicht bei jeder Runde erneut melden


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# --- Ledger / Kontingente ------------------------------------------------
def _load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def _quota(agent: str):
    d = _load_ledger()
    used = d.get(str(date.today()), {}).get(agent, 0)
    limit = QUOTA.get(agent, QUOTA.get("_default", 200))
    return used, limit


def _api_heute() -> float:
    """Heute verbrauchte Euro für externe Anbieter (K43/K44)."""
    return float(_load_ledger().get(str(date.today()), {}).get("_api_eur", 0.0))


def _api_budget_frei() -> tuple[bool, float, float]:
    """(darf noch, verbraucht, Deckel) — lokal kostet nichts, nur Cloud zählt hier.

    Nimmt denselben Deckel wie _budget_zone(): Zwei Stellen, die ihn getrennt
    aus der Konfiguration lesen, waren genau der Grund, warum die Absenkung
    zunaechst wirkungslos blieb."""
    deckel = _tagesdeckel()
    verbraucht = _api_heute()
    return verbraucht < deckel, verbraucht, deckel


def _api_buchen(betrag_eur: float) -> None:
    d = _load_ledger()
    today = str(date.today())
    d.setdefault(today, {})
    d[today]["_api_eur"] = round(d[today].get("_api_eur", 0.0) + betrag_eur, 4)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def _record(agent: str):
    d = _load_ledger()
    today = str(date.today())
    d.setdefault(today, {})
    d[today][agent] = d[today].get(agent, 0) + 1
    d["budget_eur_monat"] = CFG["budget_eur_monat"]
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, ensure_ascii=False, indent=1))


# --- Budget: Ermessen immer, Endspurt ab 20 Prozent (Chef, 2026-08-16) ----
# "Immer Ermessen von Buchhalter, bei Zweifel wegen Interna Frage an Kommunikation,
#  ab <= 20% nur noch fertig machen."
#
# Daraus folgt eine andere Bauform als die erste Fassung mit drei Zonen:
#   * Buchhalter urteilt bei JEDER Anfrage, die nach draussen ginge -- nicht erst,
#     wenn es knapp wird. Das Urteil laeuft lokal auf der eigenen GPU und kostet
#     kein Geld, nur Zeit.
#   * Ist er sich wegen INTERNA unsicher, entscheidet er das nicht selbst,
#     sondern fragt Kommunikation. Budget ist sein Ressort, Vertraulichkeit ihres.
#   * Ab 20 Prozent Restbudget gilt Endspurt: nur noch abschliessen, was schon
#     laeuft. Ein Agent mit offenem Vorgang darf zu Ende bringen, ein neues
#     Anliegen wartet auf morgen.
#   * Bei 0 EUR harter Cut, ohne Urteil.
ERMESSEN_JA, ERMESSEN_NEIN, ERMESSEN_INTERNA = "JA", "NEIN", "INTERNA"


def _fixkosten() -> float:
    """Summe der laufenden Abos. Sie gehen vom Monatsbudget ab, bevor ein Agent
    ueberhaupt etwas ausgeben kann."""
    return sum(float(v) for v in (CFG.get("fixkosten_eur_monat") or {}).values())


def _verfuegbar_monat() -> float:
    """Was diesen Monat wirklich zur Verfuegung steht.

    Bis 2026-08-16 stand budget_eur_monat auf 50 und wurde NUR ueber /status
    gemeldet -- gesteuert hat allein der Tagesdeckel. Der lag bei 0,50 EUR, also
    hochgerechnet 15 EUR im Monat, waehrend nach Abzug der Abos rund 10 EUR frei
    sind. Kein dramatischer, aber ein schleichender Fehlstand: Der Deckel lief
    dem wirklichen Rest um die Haelfte davon, und niemand haette es bemerkt.
    """
    rest = float(CFG.get("budget_eur_monat", 0.0)) - _fixkosten()
    if rest < 0:
        # Unterdeckung: Die laufenden Abos uebersteigen das Budget. Das ist keine
        # Sparlage, sondern ein Fehlstand, den nur Chef beheben kann -- und er
        # faellt sonst nicht auf, weil ein Deckel von 0 einfach wie Ruhe aussieht.
        log(f"UNTERDECKUNG: Fixkosten {_fixkosten():.2f} > Budget "
            f"{CFG.get('budget_eur_monat', 0.0):.2f} EUR — nichts Kostenpflichtiges möglich")
    return max(0.0, rest)


def _tagesdeckel() -> float:
    """Tagesdeckel aus dem verfuegbaren Rest, nicht frei gesetzt.

    So zieht ein neues oder gekuendigtes Abo den Deckel automatisch nach. Ein
    ausdruecklich gesetzter api_deckel_eur_tag gilt weiter als Obergrenze --
    was frei ist, darf trotzdem gedeckelt werden, nur nicht ueberschritten.
    """
    aus_budget = _verfuegbar_monat() / 30.0
    gesetzt = CFG.get("api_deckel_eur_tag")
    return min(aus_budget, float(gesetzt)) if gesetzt is not None else aus_budget


def _budget_zone() -> tuple[str, float, float]:
    """('normal' | 'endspurt' | 'stopp', verbraucht, Deckel)."""
    deckel = _tagesdeckel()
    verbraucht = _api_heute()
    rest = deckel - verbraucht
    if rest <= 0:
        return "stopp", verbraucht, deckel
    anteil = float(CFG.get("endspurt_ab_restanteil", 0.20))
    return ("endspurt" if rest <= deckel * anteil else "normal"), verbraucht, deckel


def _hat_offenen_vorgang(agent_id: str) -> bool:
    """Arbeitet der Agent an etwas Laufendem? Entscheidet im Endspurt darueber,
    ob er noch nach draussen darf."""
    try:
        sys.path.insert(0, str(MITARBEITER_DIR / "framework"))
        import vorgaenge
        return bool(vorgaenge.laden(agent_id))
    except Exception as e:                                  # noqa: BLE001
        log(f"Endspurt: Vorgaenge nicht lesbar ({e}) — als 'nichts offen' gewertet")
        return False


def _frage_modell(prompt: str, max_s: int = 300) -> str:
    """Ein kurzes lokales Urteil einholen. Wirft nichts -- leerer String heisst
    'kein Urteil', und die Aufrufer werten das als Ablehnung."""
    try:
        m = denkzeit.chat(
            f"{OLLAMA}/api/chat",
            {"model": CFG["default_model"],
             "messages": [{"role": "user", "content": prompt}],
             "options": {"num_ctx": 4096}},
            stall_s=CFG.get("stillstand_sekunden", denkzeit.STALL_S),
            max_s=max_s, log=log)
        return _THINK.sub("", m.get("content", "")).strip().upper()
    except Exception as e:                                  # noqa: BLE001
        log(f"Urteil nicht moeglich: {e}")
        return ""


def _kommunikation_fragt(anliegen: str) -> tuple[bool, str]:
    """Kommunikations Urteil zur Vertraulichkeit. Sie hat das letzte Wort, wenn Buchhalter
    wegen Interna zweifelt -- ihr Ressort, nicht seins."""
    antwort = _frage_modell(
        "Du bist Kommunikation, zustaendig fuer Oeffentlichkeitsarbeit und Datensicherheit. "
        "Du erkennst, was vertraulich ist, auch wenn es niemand so genannt hat.\n"
        "Diese Anfrage soll an einen externen Anbieter geschickt werden:\n"
        f"---\n{anliegen[:800]}\n---\n"
        "Stecken darin Interna -- Namen, Pfade, Projektbezeichnungen, Zusammenhaenge, "
        "die das Haus nicht verlassen sollen? Antworte mit genau einem Wort: "
        "UNBEDENKLICH oder VERTRAULICH.")
    if antwort.startswith("UNBEDENKLICH"):
        return True, "Kommunikation: unbedenklich"
    if antwort.startswith("VERTRAULICH"):
        return False, "Kommunikation: vertraulich, bleibt im Haus"
    return False, "Kommunikation: kein klares Urteil — bleibt im Haus"


def _ermessen(anliegen: str, agent_id: str, rest: float, deckel: float) -> tuple[bool, str]:
    """Buchhalters Urteil. (durchlassen, Begruendung).

    Drei moegliche Ausgaenge: JA, NEIN oder INTERNA -- letzteres reicht die
    Entscheidung an Kommunikation weiter. Kein Urteil bedeutet Ablehnung: ein
    Budget-Waechter, der bei eigener Stoerung durchwinkt, waere keiner.
    """
    antwort = _frage_modell(
        "Du bist Buchhalter, der Ressourcen-Broker der Firma. Du wachst ueber ein "
        f"Tagesbudget von {deckel:.2f} EUR fuer externe Anbieter; davon sind noch "
        f"{rest:.2f} EUR uebrig.\n"
        f"Der Agent {agent_id} moechte diese Anfrage nach draussen schicken:\n"
        f"---\n{anliegen[:600]}\n---\n"
        "Antworte mit genau einem Wort:\n"
        "  JA      - der Aufwand lohnt sich\n"
        "  NEIN    - nicht den Rest wert, das geht auch lokal\n"
        "  INTERNA - du bist unsicher, ob Vertrauliches darin steckt")
    if antwort.startswith(ERMESSEN_INTERNA):
        log("Buchhalter zweifelt wegen Interna — Kommunikation wird gefragt")
        return _kommunikation_fragt(anliegen)
    if antwort.startswith(ERMESSEN_JA):
        return True, "Buchhalter: gerechtfertigt"
    if antwort.startswith(ERMESSEN_NEIN):
        return False, "Buchhalter: nicht gerechtfertigt"
    return False, "Buchhalter: kein klares Urteil"


def _urteil(item: dict) -> "privatfilter.Urteil":
    """Darf dieser Gedanke das Haus verlassen? Quelle entscheidet, nicht Inhalt.

    Persönlichkeitsdateien sind grundsätzlich klassifiziert (K58/K65): Der
    System-Prompt IST die Persona. Sobald ein Cloud-Anbieter angeschlossen wird,
    trennt `privatfilter.reisefertig()` Sachfrage und Charakter."""
    # Der Aufrufer ERKLÄRT, ob eine Persona im Prompt steckt — abgeleitet werden darf
    # das nicht: Auch die persona-freie Sachfrage aus privatfilter.reisefertig() hat
    # einen System-Prompt und wäre sonst für immer gesperrt. Vorgabe ist True, damit
    # ein vergessliches Backend im sicheren Zustand landet.
    return privatfilter.pruefe(
        quellen=item.get("quellen") or [],
        klassifiziert=bool(item.get("klassifiziert", True)),
        system=item.get("system", ""),
        messages=item.get("messages") or [],
        freigabe=bool(item.get("freigabe")))


# --- GPU-Sitzung ---------------------------------------------------------
def _vram_free_mib() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return 99999


def _plaetze() -> list[dict]:
    """Die nutzbaren Rechenplaetze, aus hosts.json abgeleitet.

    KEINE zweite Geraeteliste: hosts.json ist bereits die Wahrheit ueber die
    Hardware, und der Dispatcher matcht dagegen. Eine eigene Liste hier waere
    genau die Doppelpflege, die schon den Tagesdeckel gekostet hat.

    Ein Platz braucht dreierlei: eine GPU, ein eigenes Ollama und
    Erreichbarkeit. Der letzte Punkt wird LIVE geprueft -- der zweite PC ist
    heute noch nicht im Mesh, und ein Platz, der nur auf dem Papier existiert,
    laesst Auftraege ins Leere laufen.
    """
    fw = str(MITARBEITER_DIR / "framework")
    if fw not in sys.path:
        sys.path.insert(0, fw)
    import dispatcher
    hier = dispatcher.eigener_knoten()
    aus = []
    for name, h in dispatcher.inventar().items():
        if not h.get("gpu") or not h.get("ollama_url"):
            continue
        if name == hier:
            # Der eigene Knoten: Hier laeuft der Broker, hier sitzt LM Studio,
            # und nur hier darf die GPU-Uebergabe angefasst werden.
            aus.append({"name": name, "url": OLLAMA, "lokal": True,
                        "modell": h.get("modell") or CFG["default_model"]})
        else:
            # Die Adresse wird ERFRAGT, nicht aus der Datei genommen: Ein Knoten
            # kann im Mesh stehen, im LAN, oder gar nicht. PC 2 hatte am 17.08.
            # Ollama, aber noch kein WireGuard -- mit einer festen mesh_ip waere
            # er unerreichbar geblieben, obwohl er lief.
            ip = dispatcher.adresse(h)
            if not ip:
                continue
            url = h.get("ollama_url", "")
            # "localhost" in einer fremden Akte meint DEREN localhost -- vom
            # Broker aus ist es die Adresse, unter der wir ihn gerade erreichen.
            if not url or "localhost" in url or "127.0.0.1" in url:
                url = f"http://{ip}:11434"
            # Das Modell gehoert zum Platz, nicht zur Firma: Auf 4 GB VRAM
            # laeuft qwen3.6:35b-a3b (23,9 GB) nicht. Ohne diese Zeile fragte
            # der Broker PC 2 nach einem Modell, das dort nicht existiert --
            # Ollama antwortete mit 404, und der Auftrag war verloren.
            aus.append({"name": name, "url": url.rstrip("/"), "lokal": False,
                        "modell": h.get("modell") or CFG["default_model"]})
    return aus or [{"name": "lokal", "url": OLLAMA, "lokal": True}]


_modelle_cache: dict[str, tuple[float, set]] = {}


def _modelle_auf(url: str) -> set:
    """Welche Modelle liegen auf diesem Platz? 60 s gepuffert -- die Liste
    aendert sich selten, und jeder Auftrag soll nicht danach fragen muessen."""
    jetzt = time.time()
    alter, namen = _modelle_cache.get(url, (0.0, set()))
    if jetzt - alter < 60:
        return namen
    try:
        r = requests.get(f"{url}/api/tags", timeout=8)
        namen = {m.get("name", "") for m in (r.json().get("models") or [])}
        namen |= {n.split(":")[0] for n in namen}
    except Exception:                                          # noqa: BLE001
        namen = set()
    _modelle_cache[url] = (jetzt, namen)
    return namen


def _modell_fuer(platz: dict, wunsch: str | None) -> str:
    eigen = platz.get("modell") or CFG["default_model"]
    if not wunsch:
        return eigen
    vorhanden = _modelle_auf(platz["url"])
    if not vorhanden or wunsch in vorhanden:
        return wunsch
    log(f"{platz['name']} kennt '{wunsch}' nicht — nimmt '{eigen}'")
    return eigen


# Wie viel VRAM frei sein muss, bevor ein Modell geladen wird. Darunter kriecht
# Ollama in den Arbeitsspeicher, und aus Minuten werden Stunden.
VRAM_NOETIG_MIB = 6000


def _modelle_geladen(url: str = "") -> set:
    """Welche Modelle liegen JETZT im Speicher dieses Ollama?"""
    try:
        r = requests.get(f"{(url or OLLAMA).rstrip('/')}/api/ps", timeout=8)
        return {m.get("name") or m.get("model") or "" 
                for m in (r.json().get("models") or [])}
    except Exception:                                          # noqa: BLE001
        return set()


def _ollama_entladen(url: str = "") -> list:
    """Alle geladenen Modelle freigeben -- nicht nur das Standardmodell.

    keep_alive=0 heisst "sofort vergessen". Wer nur das eine Modell entlaedt,
    laesst ein zweites stehen, das jemand anders geladen hat, und wundert sich
    ueber fehlenden Speicher.
    """
    basis = (url or OLLAMA).rstrip("/")
    raus = []
    try:
        r = requests.get(f"{basis}/api/ps", timeout=8)
        for m in (r.json().get("models") or []):
            name = m.get("name") or m.get("model")
            if not name:
                continue
            requests.post(f"{basis}/api/generate",
                          json={"model": name, "keep_alive": 0}, timeout=30)
            raus.append(name)
    except Exception as e:                                     # noqa: BLE001
        log("Entladen fehlgeschlagen:", repr(e))
    return raus


def _ensure_gpu(modell: str = ""):
    """Platz schaffen, BEVOR geladen wird -- aber nicht das eigene Modell wegräumen.

    Frueher stand hier eine Flag-Abfrage: War firm_holds_gpu gesetzt, tat die
    Funktion nichts. Genau das ging am 17.08. schief -- der Idle-Waechter gab
    die GPU nach 90 s an Hermes zurueck, LM Studio lud 13,5 von 16,4 GB VRAM,
    und beim naechsten Auftrag glaubte der Broker weiter, er halte die Karte.
    Ollama bekam kaum VRAM, wich in den ohnehin vollen Arbeitsspeicher aus und
    lud ueber zwanzig Minuten nicht fertig.
    Deshalb wird jetzt GEMESSEN statt geglaubt -- dasselbe Muster wie beim
    Knotennamen und beim Portscan: Ein Flag sagt, was einmal galt.
    """
    frei = _vram_free_mib()
    if frei >= VRAM_NOETIG_MIB:
        _state["firm_holds_gpu"] = True
        return

    # Ist das gesuchte Modell schon geladen, IST der belegte Speicher das
    # Modell. Ohne diese Abfrage raeumte der Broker sein eigenes Modell weg und
    # lud es neu -- bei 23,9 GB auf einer SATA-SSD kostet das jeden Auftrag
    # Minuten, und zwar unbemerkt: Im Protokoll stand nur "Platz schaffen".
    if modell and modell in _modelle_geladen():
        log(f"{frei} MiB frei, aber '{modell}' ist bereits geladen — nichts raeumen")
        _state["firm_holds_gpu"] = True
        return

    log(f"nur {frei} MiB VRAM frei — Platz schaffen")
    weg = _ollama_entladen()
    if weg:
        log(f"Ollama entladen: {', '.join(weg)}")
    if GPU.get("manage_lmstudio"):
        log("LM Studio entladen")
        subprocess.run([LMS, "unload", "--all"], capture_output=True)
    # Race vermeiden: warten bis der VRAM wirklich frei ist, bevor Ollama lädt.
    deadline = time.time() + 40
    while _vram_free_mib() < VRAM_NOETIG_MIB and time.time() < deadline:
        time.sleep(0.5)
    log(f"VRAM frei: {_vram_free_mib()} MiB")
    _state["firm_holds_gpu"] = True


def _release_gpu():
    if not _state["firm_holds_gpu"]:
        return
    log("GPU an Hermes zurückgeben — qwen entladen, LM Studio laden")
    # ALLE Rechenplaetze, nicht nur der eigene. Nach dem letzten Auftrag hielt
    # PC 2 sein Modell weiter im VRAM -- das Freigeben kannte nur den lokalen
    # Knoten und war damit auf einem Zwei-PC-Aufbau zur Haelfte wirkungslos.
    for platz in (_AKTIVE_PLAETZE or [{"name": "lokal", "url": OLLAMA}]):
        weg = _ollama_entladen(platz["url"])
        if weg:
            log(f"{platz['name']}: freigegeben — {', '.join(weg)}")
    if GPU.get("manage_lmstudio"):
        subprocess.run(["bash", PREFLIGHT], capture_output=True)
    _state["firm_holds_gpu"] = False


# --- Denken (lokal; Naht für Kaskade) ------------------------------------
NUM_CTX = 16384    # siehe framework/backends.py — ohne Angabe nur 4096


MITARBEITER_DIR = Path(__file__).resolve().parents[2]      # …/system/mitarbeiter
sys.path.insert(0, str(MITARBEITER_DIR / "framework"))
import werkzeuge as _wz                                     # noqa: E402
import denkzeit                                             # noqa: E402
import auftragsqueue                                        # noqa: E402

MAX_WERKZEUG_RUNDEN = 3      # harte Grenze gegen Endlosschleifen im Tool-Loop


def _akte(agent_id: str) -> dict:
    """Personalakte VON DER PLATTE lesen — nicht aus dem Request.

    Damit sind die Rechte eines Agenten eine Tatsache und keine Behauptung: Ein
    Agent kann in seiner Anfrage keine Werkzeuge mitschicken, die ihm die Akte
    nicht gibt."""
    p = MITARBEITER_DIR / agent_id / "personalakte.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"agent_id": agent_id}


def _chat(model: str, msgs: list, tools: list | None = None,
          url: str | None = None) -> dict:
    """Denken lassen — abgebrochen wird nur bei Stillstand, Gruebeln oder Notbremse.

    Frueher stand hier ein Gesamt-Timeout von 600 s. Das traf die falsche Sache:
    qwen3.6:35b-a3b laeuft mit CPU-Offload, eine gruendliche Antwort dauert, und
    die Warteschlange ist seriell. Jetzt zaehlt die Zeit *zwischen* zwei Token
    (siehe framework/denkzeit.py) — lange Gedankengaenge duerfen lang sein.
    """
    payload = {"model": model, "messages": msgs, "options": {"num_ctx": NUM_CTX}}
    if tools:
        payload["tools"] = tools
    return denkzeit.chat(
        # `url` ist der Rechenplatz, auf dem dieser Auftrag laeuft. Ohne Angabe
        # der eigene Knoten -- so bleibt jeder Aufrufer gueltig, der die Wahl
        # nicht trifft.
        f"{(url or OLLAMA).rstrip('/')}/api/chat", payload,
        stall_s=CFG.get("stillstand_sekunden", denkzeit.STALL_S),
        max_s=CFG.get("notbremse_sekunden", denkzeit.MAX_S),
        # Eigene Frist bis zum ersten Token: Solange das Modell laedt, gibt es
        # kein Lebenszeichen -- 23,9 GB von der Platte brauchen Minuten.
        lade_s=CFG.get("ladefrist_sekunden", denkzeit.LADE_S),
        gruebel_fenster=CFG.get("gruebel_fenster", denkzeit.GRUEBEL_FENSTER),
        gruebel_max=CFG.get("gruebel_wiederholungen", denkzeit.GRUEBEL_MAX),
        log=log)


def _think(system: str, messages: list, model: str, agent_id: str = "",
           raum: str = "", aktionen: list | None = None,
           url: str | None = None) -> str:
    """Denken — mit Werkzeugen, falls die Personalakte welche freigibt.

    Der Tool-Loop läuft hier und nicht im Agenten: Buchhalter bleibt das einzige Tor,
    und ACL + Vollmacht werden serverseitig geprüft (werkzeuge.ausfuehren)."""
    akte = _akte(agent_id) if agent_id else {}
    tools = _wz.fuer_agent(akte)
    msgs = [{"role": "system", "content": system}, *messages]

    for _ in range(MAX_WERKZEUG_RUNDEN if tools else 1):
        try:
            m = _chat(model, msgs, tools, url=url)
        except denkzeit.Abbruch as ab:
            # Teiltext retten statt leer scheitern: zehn Minuten Denkarbeit sind
            # auch ohne Schlusssatz mehr wert als eine Fehlermeldung.
            log(f"Denken abgebrochen ({agent_id}): {ab.grund}")
            return denkzeit.mit_notiz(_THINK.sub("", ab.teiltext).strip(), ab.grund)
        calls = m.get("tool_calls") or []
        if not calls:
            return _THINK.sub("", m.get("content", "")).strip()
        msgs.append(m)
        for c in calls:
            fn = (c.get("function") or {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            ergebnis = _wz.ausfuehren(akte, name, args,
                                      {"agent_id": agent_id, "raum": raum})
            log(f"Werkzeug {agent_id}.{name}({args}) -> {ergebnis[:90]}")
            if aktionen is not None:
                aktionen.append({"werkzeug": name, "args": args, "ergebnis": ergebnis})
            msgs.append({"role": "tool", "name": name, "content": ergebnis})

    try:
        m = _chat(model, msgs, url=url)      # letzte Runde ohne Werkzeuge: jetzt antworten
    except denkzeit.Abbruch as ab:
        log(f"Denken abgebrochen ({agent_id}): {ab.grund}")
        return denkzeit.mit_notiz(_THINK.sub("", ab.teiltext).strip(), ab.grund)
    return _THINK.sub("", m.get("content", "")).strip()


def _platz_erfuellt(platz: dict, anf: dict) -> bool:
    """Genuegt dieser Rechenplatz der Anforderung? Geprueft wird gegen
    hosts.json -- ueber dieselbe Funktion, die auch Agenten verteilt."""
    fw = str(MITARBEITER_DIR / "framework")
    if fw not in sys.path:
        sys.path.insert(0, fw)
    import dispatcher
    host = dispatcher.inventar().get(platz["name"])
    if not host:
        return True          # unbekannter Platz: nicht aussperren
    ok, _grund = dispatcher.passend(anf, host, agent=False)
    return ok


def _worker(platz: dict | None = None):
    """Ein Arbeiter je Rechenplatz.

    Zwei Dinge unterscheiden ihn von der frueheren einen Schleife:

    1. Er nimmt einen Auftrag NUR an, wenn dessen Agent nicht schon anderswo
       denkt. Sonst legt er ihn zurueck und wartet einen Takt. Zurueckgelegt
       statt verworfen: Der Auftrag ist ja gueltig, er ist nur zu frueh.
    2. Die GPU-Uebergabe (LM Studio entladen) fasst nur der LOKALE Platz an.
       Auf einem fremden Knoten laeuft kein LM Studio dieser Sitzung, und ein
       Fernaufruf haette dort nichts zu greifen.
    """
    platz = platz or {"name": "lokal", "url": OLLAMA, "lokal": True,
                      "modell": CFG["default_model"]}
    while True:
        _prio, _seq_n, item = _pq.get()
        agent_id = item["agent_id"]

        # Erfuellt DIESER Platz, was der Auftrag braucht? Ein Auftrag, der ein
        # 24-GB-Modell verlangt, hat auf einem 4-GB-Knoten nichts zu suchen --
        # bisher nahm einfach der erste freie Platz jeden Auftrag, und eine
        # Kauffaehigkeitspruefung fuer das grosse Modell landete auf dem
        # kleinen. Dieselbe Anforderungssprache wie im dispatcher.
        anf = item.get("anforderung") or {}
        if anf and not _platz_erfuellt(platz, anf):
            _pq.put(((_prio, 0, 0, _seq_n), _seq_n, item))
            _pq.task_done()
            time.sleep(CFG.get("queue_takt_sekunden", 3))
            continue

        # Derselbe Agent darf nicht zweimal gleichzeitig denken.
        if not _greifen(agent_id):
            _pq.put(((_prio, 0, 0, _seq_n), _seq_n, item))
            _pq.task_done()
            time.sleep(CFG.get("queue_takt_sekunden", 3))
            continue

        with _platz_lock(platz):
            _state["current"] = agent_id
            _state.setdefault("laufend", {})[platz["name"]] = agent_id
            _state["last_activity"] = time.time()
            try:
                # Erst wissen, WAS geladen werden soll -- dann aufraeumen.
                # Umgekehrt raeumt man das Modell weg, das man gerade braucht.
                model = _modell_fuer(platz, item.get("model"))
                if platz["lokal"]:
                    _ensure_gpu(model)
                urteil = _urteil(item)
                # Kosten-Kaskade — hier verzweigt sie, sobald ein Anbieter angebunden ist.
                # Drei Bedingungen müssen ALLE erfüllt sein, bevor etwas das Haus
                # verlässt: nicht privat (K46), Tagestopf nicht leer (K43) und die
                # Aufgabe kommt von Chef bzw. hat hohe Priorität (K44). Fehlt eine,
                # bleibt es lokal — kein Stillstand, nur ein einfacheres Ergebnis.
                zone, verbraucht, deckel = _budget_zone()
                agent_id = item["agent_id"]
                vorrang = PRIOS.get(agent_id, 3) <= 1
                cloud_moeglich = (not urteil.privat) and vorrang and zone != "stopp"

                # Endspurt: ab 20 Prozent Rest nur noch abschliessen, was laeuft.
                # Ein Agent ohne offenen Vorgang faengt nichts Neues mehr an.
                if cloud_moeglich and zone == "endspurt" and not _hat_offenen_vorgang(agent_id):
                    cloud_moeglich = False
                    log(f"Endspurt ({verbraucht:.2f}/{deckel:.2f} €): {agent_id} hat nichts "
                        f"Offenes — nur noch Laufendes darf nach draussen")

                # Buchhalter urteilt IMMER, wenn es ueberhaupt nach draussen ginge.
                # Zweifelt er wegen Interna, entscheidet Kommunikation (in _ermessen).
                if cloud_moeglich:
                    letzte = next((m.get("content", "") for m in reversed(item["messages"])
                                   if m.get("role") == "user"), "")
                    cloud_moeglich, warum = _ermessen(letzte, agent_id,
                                                      deckel - verbraucht, deckel)
                    log(f"Ermessen ({zone}, {verbraucht:.2f}/{deckel:.2f} €): "
                        f"{'durchgelassen' if cloud_moeglich else 'abgelehnt'} — {warum}")
                elif not urteil.privat and zone == "stopp":
                    log(f"Cloud gesperrt: Tagestopf leer ({verbraucht:.2f}/{deckel:.2f} €)")
                aktionen: list = []
                agent, raum = item["agent_id"], item.get("raum", "")
                try:
                    reply = _think(item["system"], item["messages"], model,
                                   agent, raum, aktionen, url=platz["url"])
                except Exception as first:            # transientes OOM/Model-Load -> 1x retry
                    log("Denk-Fehler, Wiederholversuch:", repr(first))
                    time.sleep(2)
                    aktionen = []
                    reply = _think(item["system"], item["messages"], model,
                                   agent, raum, aktionen, url=platz["url"])
                item["result"] = {"reply": reply, "backend": f"lokal:{model}@{platz['name']}",
                                  "privat": urteil.privat, "grund": urteil.grund,
                                  "cloud_moeglich": cloud_moeglich,
                                  "aktionen": aktionen}
                _record(item["agent_id"])
                _state["done"] += 1
            except Exception as e:  # noqa: BLE001
                import traceback
                log("DENKFEHLER endgültig:", repr(e))
                traceback.print_exc()
                item["result"] = {"error": str(e)}
            finally:
                _loslassen(agent_id)
                _state["current"] = None
                _state.get("laufend", {}).pop(platz["name"], None)
                _state["last_activity"] = time.time()
                item["event"].set()
        _pq.task_done()


def _idle_watcher():
    while True:
        time.sleep(5)
        # "Niemand arbeitet" heisst mit mehreren Plaetzen: KEIN Platz belegt.
        # Auf _state["current"] allein zu schauen wuerde die GPU zurueckgeben,
        # waehrend der zweite Platz noch denkt.
        if (_state["firm_holds_gpu"] and _pq.empty() and not _state.get("laufend")
                and time.time() - _state["last_activity"] >= IDLE):
            with _gpu_lock:
                if _pq.empty() and not _state.get("laufend"):
                    _release_gpu()


# --- HTTP ----------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/praesenz":
            jetzt = time.time()
            # Wie lange laeuft DIESER Broker schon? Die Praesenzliste liegt nur
            # im Arbeitsspeicher -- nach einem Neustart ist sie leer, und "kein
            # Lebenszeichen" heisst dann nicht "ausgefallen", sondern "ich weiss
            # es noch nicht". Am 17.08. meldete ein Agent zwoelf Minuten nach
            # einem Broker-Neustart prompt einen Ausfall, der keiner war.
            # Der Aufrufer entscheidet mit dieser Zahl, ob er der Liste traut.
            return self._send(200, {
                "frist_s": PRAESENZ_FRIST,
                "broker_alter_s": round(jetzt - _START, 1),
                "verlaesslich": (jetzt - _START) >= PRAESENZ_FRIST,
                "agenten": {
                    a: {"zuletzt": round(jetzt - t, 1),
                        "lebt": (jetzt - t) < PRAESENZ_FRIST}
                    for a, t in sorted(_praesenz.items())}})
        if self.path != "/status":
            return self._send(404, {"error": "unbekannt"})
        darf, verbraucht, deckel = _api_budget_frei()
        self._send(200, {"firm_holds_gpu": _state["firm_holds_gpu"], "queue": _pq.qsize(),
                         "laufend": dict(_state.get("laufend") or {}),
                         "plaetze": [p["name"] for p in _AKTIVE_PLAETZE],
                         "current": _state["current"], "erledigt": _state["done"],
                         "api_eur_heute": verbraucht, "api_deckel_eur": round(deckel, 3),
                         "cloud_offen": darf,
                         "budget_eur_monat": float(CFG.get("budget_eur_monat", 0.0)),
                         "fixkosten_eur_monat": _fixkosten(),
                         "verfuegbar_eur_monat": round(_verfuegbar_monat(), 2),
                         "heute": _load_ledger().get(str(date.today()), {})})

    def _eskaliere(self):
        """Cloud-Stufe der Kaskade (K65/K70/K43).

        Bewusst ein eigener Endpunkt statt eines Flags an /think: Was hier
        hereinkommt, ist etwas grundsätzlich anderes — eine vom Mitarbeiter
        selbst formulierte Sachfrage OHNE Persona und OHNE Gesprächsverlauf.
        /think ist der Persona-Weg und bleibt damit ausnahmslos lokal.

        Drei Tore, alle drei müssen offen sein:
          1. Privat-Filter — Quelle darf nicht privat sein (K46)
          2. Tagesdeckel — 0,50 € pro Tag (K43)
          3. Prioritätsklasse ≤ 1, also des Chefs Anliegen (K44)"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            b = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": f"bad json: {e}"})
        agent = b.get("agent_id", "_default")
        sachfrage = (b.get("sachfrage") or "").strip()
        rolle = (b.get("rolle") or "Du bist ein sachlicher Experte.").strip()
        if not sachfrage:
            return self._send(400, {"error": "sachfrage fehlt"})

        urteil = privatfilter.pruefe(quellen=b.get("quellen") or [],
                                     klassifiziert=False,   # persona-freier Weg
                                     system=rolle, messages=[{"content": sachfrage}],
                                     freigabe=bool(b.get("freigabe")))
        if urteil.privat:
            log(f"Eskalation abgelehnt ({agent}): {urteil.grund}")
            return self._send(403, {"error": "bleibt lokal", "grund": urteil.grund})

        darf, verbraucht, deckel = _api_budget_frei()
        if not darf:
            log(f"Eskalation abgelehnt: Tagestopf leer ({verbraucht:.2f}/{deckel:.2f} €)")
            return self._send(429, {"error": "Tagestopf leer",
                                    "verbraucht": verbraucht, "deckel": deckel})
        if PRIOS.get(agent, 3) > 1:
            return self._send(403, {"error": "Prioritätsklasse zu niedrig für Cloud",
                                    "klasse": PRIOS.get(agent, 3)})
        try:
            antwort, kosten, stufe = cloud.frage_cloud(sachfrage, rolle,
                                                       b.get("stufe", "mammouth"))
        except Exception as e:  # noqa: BLE001
            log(f"Eskalation fehlgeschlagen ({agent}): {e}")
            return self._send(502, {"error": str(e)[:200]})
        if kosten:
            _api_buchen(kosten)
        log(f"Eskalation {agent} -> {stufe} (${kosten:.6f})")
        self._send(200, {"reply": antwort, "backend": f"cloud:{stufe}",
                         "kosten_usd": kosten, "grund": urteil.grund})

    def do_POST(self):
        if self.path == "/heartbeat":
            try:
                n = int(self.headers.get("Content-Length", 0))
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": f"bad json: {e}"})
            agent = b.get("agent_id")
            if not agent:
                return self._send(400, {"error": "agent_id fehlt"})
            _praesenz[agent] = time.time()
            if agent in _ausgefallen:
                _ausgefallen.discard(agent)
                log(f"Präsenz: {agent} ist zurück")
            return self._send(200, {"ok": True})
        if self.path == "/eskaliere":
            return self._eskaliere()
        if self.path != "/think":
            return self._send(404, {"error": "unbekannt"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            return self._send(400, {"error": f"bad json: {e}"})
        agent = body.get("agent_id", "_default")
        used, limit = _quota(agent)
        if used >= limit:
            return self._send(429, {"error": f"Tageskontingent erschöpft ({used}/{limit})"})
        global _seq
        with _seq_lock:
            _seq += 1
            s = _seq
        item = {"agent_id": agent, "system": body.get("system", ""),
                "messages": body.get("messages", []), "model": body.get("model"),
                "quellen": body.get("quellen") or [],      # Herkunft für den Privat-Filter
                "klassifiziert": body.get("klassifiziert", True),  # Persona im Prompt?
                "freigabe": bool(body.get("freigabe")),    # nur ein Chef darf freigeben
                "raum": body.get("raum", ""),              # Kontext für Werkzeuge (Vorgänge)
                "event": threading.Event(), "result": None}
        # Reihenfolge: fachliche Zuständigkeit VOR Budget-Klasse. Die Passung
        # (0–2) rechnet der Agent selbst aus und schickt sie mit — er kennt seine
        # Akte, der Broker soll nur sortieren. Ohne Angabe verhält sich alles wie
        # bisher, damit ältere Aufrufer weiterlaufen.
        # `los` streut den Gleichstand, damit nicht immer derselbe vorn steht;
        # `s` bleibt als letzte Instanz, sonst wäre die Reihenfolge unbestimmt.
        pass_wert = int(body.get("passung", 0))
        los = int(body.get("losnummer", 0))
        anf = body.get("anforderung") or {}
        if anf and not any(_platz_erfuellt(p, anf) for p in _AKTIVE_PLAETZE):
            return self._send(400, {"error":
                f"Kein Rechenplatz erfuellt {anf} — der Auftrag wuerde endlos "
                f"in der Schlange kreisen. Verfuegbar: "
                f"{[p['name'] for p in _AKTIVE_PLAETZE]}"})
        item["anforderung"] = anf
        _pq.put(((-pass_wert, PRIOS.get(agent, 3), los, s), s, item))
        # 900 s statt 300: Die Schlange ist seriell, und mit der Neubewertung je
        # neuer Raumnachricht stehen mehr Aufträge an. Vorher starben die hinteren
        # im Timeout, statt später dranzukommen.
        # Muss ueber der Notbremse liegen: der Auftrag darf in der Schlange warten
        # UND danach lange denken, ohne dass der Handler vorher aufgibt.
        item["event"].wait(timeout=CFG.get("auftrag_frist_sekunden", 5100))
        self._send(200 if item["result"] and "error" not in item["result"] else 500,
                   item["result"] or {"error": "Zeitüberschreitung"})


def _queue_schleife():
    """Zweite Eingangstuer: Auftraege aus der Postgres-Queue statt ueber HTTP.

    Die Denk-Logik wird NICHT verdoppelt -- ein geholter Auftrag wandert in
    dieselbe Prioritaets-Warteschlange wie ein HTTP-Auftrag und durchlaeuft
    denselben _worker(). Das haelt Werkzeuge, Budget-Urteil, GPU-Sitzung und
    Privat-Filter an genau einer Stelle.

    Waehrend gewartet wird, geht alle 30 s ein Herzschlag an die Queue. Das ist
    das erste der beiden Ausfallsignale: Ein Worker, der lange denkt, sendet
    weiter -- nur ein toter schweigt. Auf dieser Maschine laeuft
    qwen3.6:35b-a3b mit CPU-Offload, da sind 200 s Denkzeit normal.
    """
    host = CFG.get("worker_name") or os.uname().nodename
    log(f"Queue-Worker aktiv als {host!r}")
    try:
        n = auftragsqueue.beim_start_freigeben(host)
        if n:
            log(f"{n} Auftrag/Auftraege eines abgestuerzten Vorgaengers neu aufgelegt")
    except Exception as e:                                  # noqa: BLE001
        log(f"Start-Aufraeumen: {e!r}")
    global _seq
    letzte_aufsicht = 0.0
    while True:
        try:
            # Aufsicht: verwaiste Auftraege einsammeln. Gehoert eigentlich zum
            # Scheduler auf dem VPS; bis der steht, macht es der Worker mit --
            # ein liegengebliebener Auftrag ist schlimmer als eine doppelte
            # Zustaendigkeit, und aufraeumen() ist idempotent.
            if time.time() - letzte_aufsicht > CFG.get("aufsicht_takt_sekunden", 120):
                letzte_aufsicht = time.time()
                for a in auftragsqueue.aufraeumen():
                    log(f"Auftrag {a['id']} ({a['agent_id']}) neu aufgelegt — "
                        f"Host {a['host']} still seit {a['stille_s']} s und nicht erreichbar")
            auftragsqueue.worker_melden(
                host, gpu_frei_mb=_vram_free_mib(),
                laeuft=_state.get("current"), version="broker")
            a = auftragsqueue.greifen(host)
            if not a:
                time.sleep(CFG.get("queue_takt_sekunden", 3))
                continue

            b = a["auftrag"] or {}
            agent = a["agent_id"]
            used, limit = _quota(agent)
            if used >= limit:
                auftragsqueue.fehlgeschlagen(
                    a["id"], f"Tageskontingent erschoepft ({used}/{limit})", erneut=False)
                continue

            with _seq_lock:
                _seq += 1
                s_n = _seq
            item = {"agent_id": agent, "system": b.get("system", ""),
                    "messages": b.get("messages", []), "model": b.get("model"),
                    "quellen": b.get("quellen") or [],
                    "klassifiziert": b.get("klassifiziert", True),
                    "freigabe": bool(b.get("freigabe")),
                    "raum": b.get("raum", ""),
                    "event": threading.Event(), "result": None}
            # `or 0` statt eines get-Defaults: Der Scheduler legt alle Felder ab,
            # auch nicht mitgeschickte — dort steht dann null, und ein Default
            # greift nur bei FEHLENDEM Schluessel. int(None) hat hier schon einen
            # Auftrag mitten im Griff sterben lassen.
            pass_wert = int(b.get("passung") or 0)
            los = int(b.get("losnummer") or 0)
            try:
                _pq.put(((-pass_wert, PRIOS.get(agent, 3), los, s_n), s_n, item))
            except Exception:
                # Nicht eingereiht -> Griff zuruecknehmen, sonst haengt der
                # Auftrag als "laeuft" ohne Bearbeiter.
                auftragsqueue.zurueck_in_die_queue(a["id"], "konnte nicht eingereiht werden")
                raise

            # Warten mit Lebenszeichen statt mit einer Stoppuhr.
            while not item["event"].wait(timeout=30):
                auftragsqueue.herzschlag(a["id"])
            ergebnis = item["result"] or {"error": "kein Ergebnis"}
            if "error" in ergebnis:
                auftragsqueue.fehlgeschlagen(a["id"], str(ergebnis["error"]))
            else:
                auftragsqueue.fertig(a["id"], ergebnis)
        except Exception as e:                              # noqa: BLE001
            log(f"Queue-Schleife: {e!r}")
            time.sleep(10)


def main():
    # Ein Arbeiter je Rechenplatz. Ist nur ein Knoten erreichbar, verhaelt sich
    # der Broker exakt wie vorher -- die Aenderung kostet nichts, wo sie nichts
    # bringt.
    _AKTIVE_PLAETZE.extend(_plaetze())
    log("Rechenplaetze: " + ", ".join(
        f"{p['name']}{' (lokal)' if p['lokal'] else ' -> ' + p['url']}"
        for p in _AKTIVE_PLAETZE))
    for platz in _AKTIVE_PLAETZE:
        threading.Thread(target=_worker, args=(platz,), daemon=True).start()
    if CFG.get("queue_worker"):
        threading.Thread(target=_queue_schleife, daemon=True).start()
    threading.Thread(target=_idle_watcher, daemon=True).start()

    # Mehrere Adressen statt einer: localhost fuer die Agenten auf diesem Geraet,
    # zusaetzlich die Mesh-IP fuer den Dispatcher auf dem VPS. Beides zusammen,
    # damit ein liegendes wg0 nicht auch den lokalen Betrieb lahmlegt.
    #
    # Ein fehlgeschlagener Bind ist KEIN Startfehler: liegt das Mesh beim Start
    # noch nicht an, laeuft der Broker trotzdem lokal weiter -- er wuerde sonst
    # genau dann ausfallen, wenn ohnehin schon etwas kaputt ist.
    hosts = CFG.get("listen_hosts") or [CFG["listen_host"]]
    port = CFG["listen_port"]

    gebunden = []
    for host in hosts[1:]:
        try:
            neben = ThreadingHTTPServer((host, port), Handler)
        except OSError as e:
            log(f"WARNUNG: {host}:{port} nicht gebunden ({e}) — laeuft ohne diese Adresse weiter")
            continue
        threading.Thread(target=neben.serve_forever, daemon=True).start()
        gebunden.append(host)

    try:
        srv = ThreadingHTTPServer((hosts[0], port), Handler)
    except OSError as e:
        if not gebunden:
            raise
        log(f"WARNUNG: {hosts[0]}:{port} nicht gebunden ({e}) — nur {', '.join(gebunden)}")
        while True:
            time.sleep(3600)
    gebunden.insert(0, hosts[0])
    log(f"Buchhalter-Broker hört auf {', '.join(f'{h}:{port}' for h in gebunden)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
