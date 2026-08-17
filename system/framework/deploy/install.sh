#!/usr/bin/env bash
# install.sh — bringt einen frischen Rechner als Firmenknoten ans Netz.
#
# Schritt 6 des Plans wandernde-werkbank, nach des Chefs Vorgabe aus Runde 04:
# "Benötigtes in einer Install.sh zusammenfassen und die zusätzlichen in ein
# Kopiere-nach-Ordner." Die Geheimnisse bleiben bewusst draussen -- was hier
# nicht automatisch geht, steht in kopiere-nach/LIESMICH.md.
#
# Aufruf auf dem NEUEN Rechner:
#   git clone git@github.com:HORUS-OS/Horus-OS-Privat.git ~/Horus-OS
#   ~/Horus-OS/system/mitarbeiter/framework/deploy/install.sh --knoten alt-pc
#
# Ohne Repo-Zugang (noch kein SSH-Schluessel auf dem Rechner) geht auch:
#   bash <(curl -fsSL <rohe-url>/install.sh) --knoten alt-pc --klonen
#
# GRUNDREGELN
#   * idempotent -- zweimal laufen lassen aendert nichts und schadet nicht
#   * nichts Zerstoerendes: kein rm, kein Ueberschreiben vorhandener Configs,
#     kein `git reset`. Was schon da ist, bleibt und wird nur gemeldet.
#   * sudo nur fuer Systempakete und WireGuard, sonst alles im Benutzerkontext
#
# Was NICHT passiert und auch nicht passieren soll: Secrets einspielen. Der
# Keyring wird von Hand gefuellt (siehe kopiere-nach/LIESMICH.md). Ein Skript,
# das Passwoerter verteilt, ist die eine Datei, die man nicht im Git haben will.

set -euo pipefail

REPO="${HOME}/Horus-OS"
FRAMEWORK="${REPO}/system/mitarbeiter/framework"
DEPLOY="${FRAMEWORK}/deploy"
UNITS="${HOME}/.config/systemd/user"
KNOTEN=""
ROLLE="agenten"
KLONEN=0
TROCKEN=0

rot()  { printf '\033[31m  %s\033[0m\n' "$*"; }
gruen(){ printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
kopf() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

hilfe() {
    cat <<'ENDE'
  install.sh --knoten NAME [--rolle ROLLE] [--klonen] [--trocken]

    --knoten NAME   Name dieses Knotens, muss zu framework/hosts.json passen
                    (z. B. haupt-pc, alt-pc, vps). Pflicht.
    --rolle ROLLE   agenten (Standard) | rechenknoten | leichtgewicht
    --klonen        Repo vorher von 'privat' klonen, falls noch nicht da
    --trocken       nur zeigen, was geschaehe
ENDE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --knoten)  KNOTEN="${2:-}"; shift 2 ;;
        --rolle)   ROLLE="${2:-}";  shift 2 ;;
        --klonen)  KLONEN=1; shift ;;
        --trocken) TROCKEN=1; shift ;;
        -h|--help) hilfe; exit 0 ;;
        *) rot "unbekannt: $1"; hilfe; exit 2 ;;
    esac
done

[ -n "$KNOTEN" ] || { rot "--knoten fehlt"; hilfe; exit 2; }

tu() {   # führt aus, oder zeigt nur
    if [ "$TROCKEN" = 1 ]; then printf '  [trocken] %s\n' "$*"; else "$@"; fi
}


# --- Welche Dienste gehoeren zu welcher Rolle? ----------------------------
# Ein Knoten traegt nicht alles. Der Haupt-PC haelt die GPU und damit den
# Broker; ein Leichtgewicht wie der Pi soll gerade so viel tragen, dass es sich
# meldet. Die Zuordnung steht hier an EINER Stelle, damit ein neuer Rechner
# nicht durch Abschreiben von einem alten entsteht -- so sind die vier
# unterschiedlichen `laeuft_auf`-Eintraege in den Personalakten entstanden, die
# der Dispatcher spaeter ersetzen musste.
dienste_fuer_rolle() {
    local rolle="$1"

    # JE KNOTEN — dürfen und sollen überall laufen. Jeder gleicht seinen
    # eigenen Arbeitsbaum ab bzw. tunnelt für sich selbst; ein zweiter richtet
    # keinen Schaden an.
    local je_knoten="firma-standverteiler"

    # GENAU EINMAL, firmenweit. Ein zweiter firma-briefing schriebe Chef jeden
    # Morgen zwei Briefings, ein zweiter berichte-index schöbe dieselben
    # Berichte doppelt nach Qdrant. Diese Dienste bleiben deshalb am Haupt-PC
    # und werden auf keinem weiteren Knoten hinterlegt.
    #
    # Die AGENTEN selbst (mitarbeiter@…) stehen bewusst nicht in dieser Liste:
    # Bei ihnen genügt keine Konvention. Sie nehmen beim Start eine Sperre in
    # der Datenbank (framework/einmalig.py) und beenden sich, wenn der Platz
    # belegt ist — das wirkt auch dann, wenn jemand eine Unit von Hand auf dem
    # falschen Rechner aktiviert.
    local nur_einmal="firma-briefing firma-bewertung firma-berichte-index archiv-embed"

    # TUNNEL — pro Knoten, der die Dienste braucht. Sie binden lokal auf
    # 127.0.0.1; zwei Knoten stören sich nicht, aber zweimal auf DEMSELBEN
    # Knoten kollidiert am Port.
    local tunnel="firma-radicale-tunnel firma-searxng-tunnel"

    case "$rolle" in
        rechenknoten)
            # Trägt die GPU-Last. Der Broker ist Queue-Worker: Ein zweiter ist
            # hier ausdrücklich erwünscht — SKIP LOCKED sorgt dafür, dass zwei
            # Worker verschiedene Aufträge greifen, nie denselben. Das ist der
            # eigentliche Zweck der Queue aus Schritt 4.
            printf '%s\n' $je_knoten buchhalter-broker $tunnel
            ;;
        agenten)
            # Der Haupt-PC: trägt zusätzlich alles, was es firmenweit nur
            # einmal geben darf.
            printf '%s\n' $je_knoten buchhalter-broker $tunnel $nur_einmal
            ;;
        leichtgewicht)
            # Pi Zero und Ähnliches: meldet sich, mehr nicht. 474 MB RAM
            # tragen keinen Agenten — hosts.json sagt dazu agenten_tauglich:false.
            printf '%s\n' $je_knoten
            ;;
        *) rot "unbekannte Rolle: $rolle"; return 1 ;;
    esac
}


# --- 1. Systempakete ------------------------------------------------------
kopf "Systempakete"
FEHLEND=()
for p in git python3-venv python3-pip wireguard-tools curl jq; do
    dpkg -s "$p" >/dev/null 2>&1 || FEHLEND+=("$p")
done
if [ ${#FEHLEND[@]} -gt 0 ]; then
    info "fehlen: ${FEHLEND[*]}"
    tu sudo apt-get update -qq
    tu sudo apt-get install -y "${FEHLEND[@]}"
else
    gruen "alle vorhanden"
fi


# --- 2. Repo --------------------------------------------------------------
kopf "Repo"
if [ -d "$REPO/.git" ]; then
    gruen "liegt bereits unter $REPO — nicht angefasst"
elif [ "$KLONEN" = 1 ]; then
    tu git clone git@github.com:HORUS-OS/Horus-OS-Privat.git "$REPO"
else
    rot "$REPO fehlt. Erst klonen oder --klonen benutzen."; exit 1
fi


# --- 3. Git-Hooks ---------------------------------------------------------
# Schreiben das Herkunftsgeraet in jeden Commit. Auf einem zweiten Rechner ist
# das keine Spielerei mehr, sondern die einzige Spur, wer was beigetragen hat.
kopf "Git-Hooks"
if [ -x "$REPO/system/git-hooks/install.sh" ]; then
    tu "$REPO/system/git-hooks/install.sh"
else
    info "kein Hook-Installer gefunden — übersprungen"
fi


# --- 4. venv --------------------------------------------------------------
kopf "Python-Umgebung"
if [ -x "$FRAMEWORK/venv/bin/python" ]; then
    gruen "venv vorhanden"
else
    tu python3 -m venv "$FRAMEWORK/venv"
fi
tu "$FRAMEWORK/venv/bin/pip" install -q --upgrade pip
tu "$FRAMEWORK/venv/bin/pip" install -q -r "$FRAMEWORK/requirements.txt"
gruen "Abhängigkeiten aktuell"


# --- 5. Mesh --------------------------------------------------------------
kopf "WireGuard-Mesh"
if ip link show wg0 >/dev/null 2>&1; then
    gruen "wg0 läuft bereits"
elif [ -f "$HOME/.wireguard-firma/wg0.conf" ]; then
    tu sudo "$DEPLOY/mesh-install.sh"
else
    rot "~/.wireguard-firma/wg0.conf fehlt — siehe kopiere-nach/LIESMICH.md"
    info "Der Knoten läuft ohne Mesh nicht: Postgres hängt an 10.0.0.1."
fi


# --- 6. systemd-Units -----------------------------------------------------
kopf "Dienste für Rolle '$ROLLE'"
mkdir -p "$UNITS"
GESETZT=0
# Units liegen nicht alle am selben Ort: buchhalter-broker.service wohnt bei
# seinem Dienst (buchhalter/broker/deploy/), nicht bei den Framework-Units. Die
# Sandbox-Beta hat das gefunden — ohne diese Suchliste wäre ein neuer
# Rechenknoten ohne Broker geblieben, und das wäre erst aufgefallen, wenn er
# nichts rechnet.
unit_suchen() {
    local name="$1" ort
    for ort in "$DEPLOY" "$REPO/system/mitarbeiter"/*/deploy "$REPO/system/mitarbeiter"/*/*/deploy; do
        [ -f "$ort/${name}.service" ] && { echo "$ort/${name}.service"; return 0; }
    done
    return 1
}

while read -r dienst; do
    [ -n "$dienst" ] || continue
    if ! quelle="$(unit_suchen "$dienst")"; then
        rot "$dienst: Unit-Datei nirgends gefunden"; continue
    fi
    tu cp "$quelle" "$UNITS/"
    # Timer mitnehmen, wo es einen gibt -- eine Unit ohne ihren Timer laeuft
    # genau einmal und dann nie wieder.
    quelle_timer="${quelle%.service}.timer"
    [ -f "$quelle_timer" ] && tu cp "$quelle_timer" "$UNITS/"
    GESETZT=$((GESETZT+1))
done < <(dienste_fuer_rolle "$ROLLE")

if [ "$GESETZT" -gt 0 ]; then
    tu systemctl --user daemon-reload
    gruen "$GESETZT Unit(s) hinterlegt"
    info "Aktivieren mit:  systemctl --user enable --now <dienst>"
    info "(bewusst nicht automatisch — ohne Secrets im Keyring scheitern sie)"
else
    info "keine Dienste für diese Rolle"
fi


# --- 7. Knotenname --------------------------------------------------------
# FIRMA_KNOTEN muss zum Namen in hosts.json passen, sonst meldet sich der
# Rechner unter seinem Hostnamen und taucht in der Uebersicht doppelt auf.
kopf "Knotenname"
if grep -q "\"$KNOTEN\"" "$FRAMEWORK/hosts.json" 2>/dev/null; then
    gruen "'$KNOTEN' steht in hosts.json"
else
    rot "'$KNOTEN' fehlt in framework/hosts.json — der Dispatcher findet ihn nicht"
fi
if [ -f "$UNITS/firma-standverteiler.service" ] && [ "$TROCKEN" != 1 ]; then
    sed -i "s/^Environment=FIRMA_KNOTEN=.*/Environment=FIRMA_KNOTEN=$KNOTEN/" \
        "$UNITS/firma-standverteiler.service"
    systemctl --user daemon-reload
    gruen "Standverteiler meldet sich als '$KNOTEN'"
fi


# --- 8. Was von Hand bleibt ----------------------------------------------
kopf "Von Hand"
cat <<ENDE
  Die Geheimnisse werden bewusst nicht von einem Skript verteilt.
  Es fehlen noch:

    1. ~/.wireguard-firma/wg0.conf      (Mesh-Schlüssel für '$KNOTEN')
    2. Keyring füllen — mindestens:
         postgres-firma-pw   für den Datenbankzugang
       über: python3 ~/Horus-OS/system/scripts/firma-keyring.py store <name>
    3. SSH-Schlüssel für GitHub, falls noch nicht vorhanden

  Alles Weitere steht in: $DEPLOY/kopiere-nach/LIESMICH.md

  Prüfen, ob es getan hat, was es soll:
    $FRAMEWORK/venv/bin/python $FRAMEWORK/standverteiler.py --einmal
    $FRAMEWORK/venv/bin/python $FRAMEWORK/dispatcher.py
ENDE
