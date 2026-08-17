#!/usr/bin/env bash
# erstinstallation.sh — richtet Horus-OS auf einem frischen Rechner ein.
#
# Chef am 2026-08-17: "Bitte erweitere ausserdem die Requirements, nachdem du
# eine Installation simulierst, um sie zu testen, am besten noch einmal als
# install-Datei."
#
# WARUM DIESES SKRIPT NICHTS BEHAUPTET
# Es prueft jeden Schritt, statt ihn anzunehmen. Eine Installationsanleitung,
# die nur Befehle aneinanderreiht, laesst den Nutzer im Regen stehen, sobald
# einer nicht durchgeht -- und meldet Erfolg, obwohl nichts steht. Hier bricht
# jeder Schritt mit einer Erklaerung ab, was fehlt und was zu tun ist.
#
# WAS ES NICHT TUT
#   * Es installiert kein Ollama und keinen Postgres. Beides sind Dienste mit
#     eigenen Entscheidungen (Modellwahl, Speicherort, Zugangsdaten) -- die
#     gehoeren dem Betreiber, nicht einem Skript.
#   * Es startet keine Dienste. Erst konfigurieren, dann starten; ein Agent, der
#     mit Beispielwerten loslaeuft, schreibt in fremde Matrix-Raeume.
#   * Es fasst nichts an, was schon da ist. Zweimal ausfuehren ist harmlos.
#
# Aufruf:
#     bash deploy/erstinstallation.sh          einrichten und pruefen
#     bash deploy/erstinstallation.sh --pruefen  nur pruefen, nichts aendern
set -euo pipefail

FRAMEWORK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$FRAMEWORK/venv"
NUR_PRUEFEN=0
[[ "${1:-}" == "--pruefen" ]] && NUR_PRUEFEN=1

rot=$'\e[31m'; gruen=$'\e[32m'; gelb=$'\e[33m'; klar=$'\e[0m'
schritt() { printf '\n%s==>%s %s\n' "$gelb" "$klar" "$1"; }
gut()     { printf '  %s✓%s %s\n' "$gruen" "$klar" "$1"; }
schlecht(){ printf '  %s✗%s %s\n' "$rot" "$klar" "$1"; }

fehler=0
melde_fehler() { schlecht "$1"; fehler=$((fehler + 1)); }

# --- 1. Python ------------------------------------------------------------
schritt "Python pruefen"
if ! command -v python3 >/dev/null; then
  melde_fehler "python3 fehlt. Installieren: apt install python3 python3-venv"
else
  version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  # 3.11 ist die Untergrenze: Der Code nutzt `X | None` in Signaturen.
  if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    gut "Python $version"
  else
    melde_fehler "Python $version ist zu alt — 3.11 oder neuer wird gebraucht."
  fi
fi

# --- 2. Virtuelle Umgebung -------------------------------------------------
schritt "Virtuelle Umgebung"
if [[ -x "$VENV/bin/python" ]]; then
  gut "venv besteht schon — wird nicht angetastet"
elif [[ $NUR_PRUEFEN -eq 1 ]]; then
  schlecht "venv fehlt (wird bei --pruefen nicht angelegt)"
else
  if python3 -m venv "$VENV" 2>/dev/null; then
    gut "venv angelegt"
  else
    melde_fehler "venv liess sich nicht anlegen. Fehlt python3-venv?"
  fi
fi

# --- 3. Python-Pakete ------------------------------------------------------
schritt "Python-Pakete"
if [[ -x "$VENV/bin/pip" && $NUR_PRUEFEN -eq 0 ]]; then
  if "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 \
     && "$VENV/bin/pip" install -q -r "$FRAMEWORK/requirements.txt"; then
    gut "aus requirements.txt installiert"
  else
    melde_fehler "pip install ist fehlgeschlagen — Ausgabe oben lesen."
  fi
fi
if [[ -x "$VENV/bin/python" ]]; then
  # Nicht pip freeze vergleichen, sondern IMPORTIEREN: Ein Paket kann
  # installiert und trotzdem unbrauchbar sein (fehlende System-Bibliothek).
  fehlend=$("$VENV/bin/python" - <<'PY'
import importlib.util
noetig = {"nio": "matrix-nio", "requests": "requests", "psycopg": "psycopg[binary]",
          "caldav": "caldav", "icalendar": "icalendar"}
fehlt = [p for m, p in noetig.items() if importlib.util.find_spec(m) is None]
print(" ".join(fehlt))
PY
)
  if [[ -z "$fehlend" ]]; then
    gut "alle Pakete importierbar"
  else
    melde_fehler "nicht importierbar: $fehlend"
  fi
fi

# --- 4. Externe Programme --------------------------------------------------
schritt "Externe Programme"
if [[ -x "$VENV/bin/python" ]]; then
  # Der Pruefer ist die eine Wahrheit darueber, was gebraucht wird -- dieses
  # Skript wiederholt die Liste nicht, es fragt ihn.
  "$VENV/bin/python" "$FRAMEWORK/voraussetzungen.py" --kurz || fehler=$((fehler + 1))
fi

# --- 5. Datenbank ----------------------------------------------------------
schritt "Datenbank"
if [[ -x "$VENV/bin/python" ]]; then
  if "$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$FRAMEWORK')
import speicher
with speicher.verbindung() as c:
    c.execute('SELECT 1')
" 2>/dev/null; then
    gut "Postgres erreichbar"
    if [[ $NUR_PRUEFEN -eq 0 ]]; then
      # Idempotent: zweimal anwenden aendert nichts. Deshalb ist es sicher,
      # das bei jeder Installation zu tun.
      for f in "$FRAMEWORK"/sql/0*.sql; do
        "$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$FRAMEWORK')
import speicher
with speicher.verbindung() as c:
    c.execute(open('$f').read())
" && gut "$(basename "$f")"
      done
    fi
  else
    melde_fehler "Postgres nicht erreichbar. Zugangsdaten kommen aus dem Keyring
      oder aus FIRMA_PG_PASSWORT — siehe speicher.py. Host und Datenbank stehen
      in firma.config.json."
  fi
fi

# --- 6. Konfiguration ------------------------------------------------------
schritt "Konfiguration"
for datei in firma.config.json hosts.json; do
  pfad="$FRAMEWORK/$datei"
  if [[ ! -f "$pfad" ]]; then
    melde_fehler "$datei fehlt."
  elif grep -q "example.org\|10\.0\.0\.\|beispiel-pc" "$pfad" 2>/dev/null; then
    schlecht "$datei enthaelt noch BEISPIELWERTE — bitte anpassen."
    printf '      Ein Agent mit Beispielwerten schreibt in fremde Raeume.\n'
    fehler=$((fehler + 1))
  else
    gut "$datei angepasst"
  fi
done

# --- 7. Ergebnis -----------------------------------------------------------
schritt "Ergebnis"
if [[ $fehler -eq 0 ]]; then
  gut "Alles bereit."
  cat <<'ENDE'

  Naechste Schritte:
    1. Einen Angestellten anlegen:
         cp -r ../mitarbeiter/demo ../mitarbeiter/meine-assistenz
       Dann personalakte.json anpassen und in persoenlichkeit.md den
       Abschnitt "System-Prompt (Laufzeit)" schreiben.

    2. Von Hand starten, um zuzusehen:
         venv/bin/python mitarbeiter_agent.py meine-assistenz

    3. Erst danach als Dienst einrichten:
         bash deploy/install.sh
ENDE
  exit 0
fi
schlecht "$fehler Punkt(e) offen — oben steht, was fehlt."
printf '  Nichts wurde gestartet. Das ist Absicht: Erst konfigurieren, dann starten.\n'
exit 1
