#!/usr/bin/env python3
"""speicher.py — Postgres-Anbindung fuer die Bewegungsdaten der Firma.

Schritt 3 des Plans wandernde-werkbank: Die Journale (vorgaenge, bilanz,
kennzahlen, berichte) wandern aus dem Git in die Datenbank auf dem VPS. Im Git
bleibt, was Historie braucht und sich selten aendert -- personalakte.json und
persoenlichkeit.md. Trennlinie ist die Aenderungsfrequenz.

**Rueckfallebene:** `firma.config.json` traegt den Schluessel
`vorgaenge_speicher` ("postgres" oder "json"). Steht dort etwas anderes als
"postgres", oder fehlt der Schluessel, laeuft alles wie bisher ueber die
JSON-Dateien. Damit ist der Umbau jederzeit ohne Code-Aenderung umkehrbar --
ein Schalter, kein Rollback.

**Kein stiller Datenverlust.** Faellt die Datenbank aus, wirft dieses Modul eine
Ausnahme, statt so zu tun, als sei gespeichert worden. Der Aufrufer bekommt den
Fehler als Werkzeug-Ergebnis zu sehen. Lieber eine sichtbare Stoerung als ein
Vorgang, den niemand mehr findet.

Zugang laeuft ueber das WireGuard-Mesh (10.0.0.1); der Port ist vom Internet aus
gesperrt. Das Passwort steht im Keyring (`postgres-firma-pw`), nie in der Config.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parent
CFG_PFAD = FRAMEWORK / "firma.config.json"
KEYRING = "/home/nutzer/Horus-OS/system/scripts/firma-keyring.py"

_cfg_cache: dict | None = None
_pw_cache: str | None = None


def _cfg() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        try:
            _cfg_cache = json.loads(CFG_PFAD.read_text(encoding="utf-8"))
        except Exception:
            _cfg_cache = {}
    return _cfg_cache


def aktiv() -> bool:
    """Laeuft die Speicherung ueber Postgres? Sonst bleibt alles bei JSON."""
    return _cfg().get("vorgaenge_speicher") == "postgres"


def _passwort() -> str:
    """Passwort in dieser Reihenfolge: Umgebungsvariable, Datei, Keyring.

    Auf dem Haupt-PC ist der GNOME-Keyring die Quelle -- dort liegen alle
    Firmen-Secrets. Auf dem VPS gibt es keinen Keyring und keine Desktop-Sitzung;
    dort kommt das Passwort aus einer Datei mit Rechten 600, die per systemd als
    EnvironmentFile eingelesen wird. Beides bleibt ausserhalb des Repos.
    """
    global _pw_cache
    if _pw_cache is not None:
        return _pw_cache
    pw = os.environ.get("FIRMA_PG_PASSWORT", "").strip()
    if not pw:
        datei = os.environ.get("FIRMA_PG_PASSWORT_DATEI", "")
        if datei and Path(datei).is_file():
            pw = Path(datei).read_text(encoding="utf-8").strip()
    if not pw and Path(KEYRING).is_file():
        r = subprocess.run(["/usr/bin/python3", KEYRING, "get", "postgres-firma-pw"],
                           capture_output=True, text=True, timeout=15)
        pw = r.stdout.strip()
    if not pw:
        raise RuntimeError("Kein Postgres-Passwort: weder FIRMA_PG_PASSWORT noch "
                           "FIRMA_PG_PASSWORT_DATEI noch Keyring")
    _pw_cache = pw
    return _pw_cache


def verbindung():
    """Neue Verbindung. Zwei Versuche — ein kurzer Aussetzer im Mesh soll nicht
    gleich einen Vorgang kosten."""
    import psycopg
    c = _cfg().get("postgres", {})
    letzter = None
    for versuch in range(2):
        try:
            return psycopg.connect(
                host=c.get("host", "10.0.0.1"), port=c.get("port", 32768),
                dbname=c.get("dbname", "firma"), user=c.get("user", "firma"),
                password=_passwort(), connect_timeout=c.get("timeout", 8),
                autocommit=True)
        except Exception as e:      # noqa: BLE001 — Fehlerart ist hier egal
            letzter = e
            if versuch == 0:
                time.sleep(1.5)
    raise RuntimeError(f"Datenbank nicht erreichbar: {letzter}")


# --- Vorgaenge ------------------------------------------------------------
# Anders als in der JSON-Fassung bleiben geschlossene Vorgaenge in der Tabelle
# stehen; `offen` ist nur noch ein Filter. Genau deshalb kann die Bilanz aus
# denselben Zeilen berechnet werden und nicht mehr auseinanderlaufen.

_SPALTEN = ("id", "agent_id", "anliegen", "wartet_auf", "fuer", "raum",
            "eroeffnet", "geschlossen", "status", "nachgefasst", "pn_offen",
            "dauer_s", "rueckfragen")


def _zeile_zu_dict(z: tuple) -> dict:
    d = dict(zip(_SPALTEN, z))
    # Die Aufrufer rechnen mit Unixzeit, so wie es die JSON-Fassung tat.
    if d.get("eroeffnet") is not None:
        d["eroeffnet"] = d["eroeffnet"].timestamp()
    d.pop("geschlossen", None)
    d.pop("dauer_s", None)
    return {k: v for k, v in d.items() if v is not None or k in ("wartet_auf", "raum")}


def offene_laden(agent_id: str) -> list[dict]:
    with verbindung() as c:
        z = c.execute(
            f"SELECT {', '.join(_SPALTEN)} FROM firma.vorgaenge "
            "WHERE agent_id = %s AND status = 'offen' ORDER BY eroeffnet",
            (agent_id,)).fetchall()
    return [_zeile_zu_dict(r) for r in z]


def einfuegen(agent_id: str, v: dict) -> None:
    with verbindung() as c:
        c.execute(
            "INSERT INTO firma.vorgaenge "
            "(id, agent_id, anliegen, wartet_auf, fuer, raum, eroeffnet, status, "
            " nachgefasst, pn_offen) "
            "VALUES (%s,%s,%s,%s,%s,%s, to_timestamp(%s), %s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING",
            (v["id"], agent_id, v["anliegen"], v.get("wartet_auf"), v.get("fuer", "chef"),
             v.get("raum"), v["eroeffnet"], v.get("status", "offen"),
             bool(v.get("nachgefasst")), bool(v.get("pn_offen"))))


def felder_setzen(agent_id: str, vorgang_id: str, felder: dict) -> None:
    erlaubt = {"nachgefasst", "pn_offen", "wartet_auf", "fuer", "raum",
               "anliegen", "rueckfragen"}
    setz = {k: v for k, v in felder.items() if k in erlaubt}
    if not setz:
        return
    teile = ", ".join(f"{k} = %s" for k in setz)
    with verbindung() as c:
        c.execute(f"UPDATE firma.vorgaenge SET {teile} WHERE id = %s AND agent_id = %s",
                  (*setz.values(), vorgang_id, agent_id))


def schliessen(agent_id: str, vorgang_id: str, status: str) -> dict | None:
    with verbindung() as c:
        z = c.execute(
            "UPDATE firma.vorgaenge SET status = %s, geschlossen = now(), "
            "  dauer_s = EXTRACT(EPOCH FROM (now() - eroeffnet)) "
            "WHERE id = %s AND agent_id = %s AND status = 'offen' "
            f"RETURNING {', '.join(_SPALTEN)}",
            (status, vorgang_id, agent_id)).fetchone()
    return _zeile_zu_dict(z) if z else None


def bilanz_lesen(agent_id: str) -> dict:
    """Aus denselben Zeilen berechnet, aus denen die Vorgaenge kommen — die
    Bilanz kann damit nicht mehr von der Wirklichkeit abweichen."""
    with verbindung() as c:
        zeilen = c.execute(
            "SELECT status, count(*), coalesce(sum(dauer_s),0) FROM firma.vorgaenge "
            "WHERE agent_id = %s AND status <> 'offen' GROUP BY status",
            (agent_id,)).fetchall()
        seit = c.execute("SELECT min(eroeffnet)::date FROM firma.vorgaenge WHERE agent_id = %s",
                         (agent_id,)).fetchone()[0]
        zuletzt = c.execute("SELECT max(geschlossen) FROM firma.vorgaenge WHERE agent_id = %s",
                            (agent_id,)).fetchone()[0]
        alt = c.execute("SELECT daten FROM firma.bilanz WHERE agent_id = %s",
                        (agent_id,)).fetchone()
    b = {"seit": str(seit) if seit else time.strftime("%Y-%m-%d"),
         "nach_status": {s: n for s, n, _ in zeilen},
         "dauer_summe_s": round(sum(float(d) for _, _, d in zeilen), 1)}
    if zuletzt:
        b["zuletzt"] = zuletzt.strftime("%Y-%m-%d %H:%M")
    # Historische Korrekturen (rekonstruiert/korrigiert) aus der JSONB-Tabelle
    # mitfuehren: sie stammen aus der Zeit vor der Datenbank und liessen sich
    # nicht aus Zeilen herleiten.
    if alt and isinstance(alt[0], dict):
        for k in ("rekonstruiert", "korrigiert"):
            if k in alt[0]:
                b[k] = alt[0][k]
    return b
