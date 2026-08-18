# Horus-OS

**Ein Framework, das Personalakten als laufende Kollegen betreibt.** Jede Akte ist eine
JSON-Datei plus eine Persönlichkeit; die Laufzeit macht daraus einen Prozess, der in einem
Matrix-Raum sitzt, Aufgaben annimmt, Werkzeuge benutzt und Rechenschaft ablegt.

Es ist kein Chatbot-Baukasten. Es ist der Versuch, **Arbeitsteilung** nachzubauen: mehrere
Angestellte mit klaren Zuständigkeiten, ein Budget, Vorgesetzte, Fristen — und die Regel,
dass niemand nach außen wirkt, ohne dass ein Mensch zustimmt.

---

## Wie es aussieht

```
╔════════════════════════════════════════════════════════════════════════════╗
║                              HORUS-OS                                      ║
║           Eine Firma aus Angestellten, auf eigener Hardware                ║
╠════════════════════════════════════════════════════════════════════════════╣
║                                                                            ║
║   Rechenknoten (GPU)                     Steuerknoten (klein, immer an)    ║
║  ┌──────────────────────────────┐       ┌──────────────────────────────┐   ║
║  │  Agenten-Prozesse            │◄─────►│  Matrix-Server               │   ║
║  │    ein Prozess je Akte       │ Mesh  │    die Raeume, in denen       │   ║
║  │                              │       │    gearbeitet wird           │   ║
║  │  Broker                      │       │                              │   ║
║  │    einziges Tor zur GPU      │       │  Postgres                    │   ║
║  │    Budget, Warteschlange     │       │    Vorgaenge, Testergebnisse, │   ║
║  │                              │       │    Staende                   │   ║
║  │  Sprachmodell (lokal)        │       │                              │   ║
║  └──────────────────────────────┘       │  Scheduler                   │   ║
║                                         │    reiht ein, rechnet nicht   │   ║
║   Pruefknoten (ARM, klein)              └──────────────────────────────┘   ║
║  ┌──────────────────────────────┐                                          ║
║  │  traegt keinen Agenten,      │   Die Aufteilung ist nicht Geschmack:     ║
║  │  belegt aber eine            │   Der Steuerknoten muss laufen, wenn      ║
║  │  Architektur fuer die        │   der GPU-Rechner aus ist -- sonst        ║
║  │  Kompatibilitaetspruefung    │   verschwindet die Firma mit ihm.         ║
║  └──────────────────────────────┘                                          ║
╚════════════════════════════════════════════════════════════════════════════╝
```

## Wie eine Anfrage laeuft

```
  Sie schreiben in einen Matrix-Raum
        │
        ▼
  Der zustaendige Agent hoert mit          passung.py entscheidet, WER
        │                                   antwortet -- ohne Modellaufruf,
        │                                   denn die Wahl muss billig sein
        ▼
  Broker: Anfrage einreihen
        │
        ├─ Denkt dieser Agent schon?  ──► zurueck in die Schlange
        │                                  (einer nie zweimal gleichzeitig)
        ├─ Welcher Rechenplatz passt?  ──► Anforderung gegen hosts.json
        │                                  (braucht_gpu, vram_gb, arch)
        ├─ Genug Grafikspeicher frei?  ──► erst raeumen, dann laden
        │                                  (gemessen, nicht geglaubt)
        ▼
  Modell denkt
        │   Abbruch nur bei Stillstand, Gruebeln oder Notbremse --
        │   nicht, weil es lange dauert
        ▼
  Werkzeug gewuenscht?
        │
        ├─ Darf der Agent das?      ──► serverseitig geprueft, nicht im Prompt
        ├─ Wirkt es nach aussen?    ──► Entwurf statt Tat: ein Vorgang,
        │                               der auf Ihre Freigabe wartet
        └─ Sonst                    ──► ausfuehren und protokollieren
        │
        ▼
  Antwort im Raum + Vorgang in der Datenbank
        │
        ▼
  Nichts mehr zu tun? ──► Grafikspeicher nach einer Weile freigeben
```

---

## Warum das anders gebaut ist

**Anforderungen statt Gerätenamen.** Eine Personalakte sagt nicht, *wo* ein Agent läuft,
sondern *was er braucht*: `braucht_gpu`, `vram_gb`, `arch`. Ein `dispatcher` sucht den
passenden Rechner. Der Vorläufer war ein Feld `laeuft_auf`, das vier Geräte nannte, während
alle vier Agenten auf demselben liefen — kein Code las es je aus. Eine Anforderung ist
prüfbar, ein Gerätename ist eine Behauptung.

**Rechte serverseitig, nicht im Prompt.** Was ein Agent darf, entscheidet der Broker
(`werkzeuge.py`), nicht eine Bitte im Systemprompt. Eine Prompt-Regel lässt sich wegreden;
eine Rechteprüfung im Code nicht.

**Abbrechen bei Stillstand, nicht bei Dauer.** Ein 24-GB-Modell mit CPU-Offload *braucht*
Minuten. `denkzeit.py` unterscheidet Stillstand (kein Token), Grübeln (derselbe Abschnitt
wiederholt sich) und eine Notbremse. Die Ladephase hat eine eigene, längere Frist — solange
ein Modell lädt, gibt es prinzipbedingt kein Lebenszeichen, und ein Stillstands-Abbruch
träfe den falschen Fall.

**Eine Testpalette vor dem Rollout.** Neun Kategorien in `palette/`, von denen nur *eine*
sperrt: Security. Alles andere warnt. Der Weg lautet
`Palette → Sandbox-Beta → Rollout`, und jede Stufe hängt am **Commit**, nicht an einer
Versionsnummer — sonst wäre getestet A und ausgerollt B.

---

## Installation

### Der kurze Weg

```bash
bash system/framework/deploy/erstinstallation.sh
```

Das Skript legt die virtuelle Umgebung an, installiert die Pakete, wendet das
Datenbankschema an und **prüft jeden Schritt**. Es startet bewusst nichts: Ein Agent, der
mit Beispielwerten losläuft, schreibt in fremde Matrix-Räume. Am Ende sagt es, was noch
fehlt — oder wie es weitergeht.

`--pruefen` ändert nichts und berichtet nur.

Wer verstehen will, was dabei passiert, findet es unten Schritt für Schritt.

### 1. Voraussetzungen prüfen

Statt einer Liste, die veraltet, fragt ein Prüfer das System:

```bash
python3 system/framework/voraussetzungen.py
```

Er unterscheidet **fehlt** von **ist da, läuft aber nicht** — zwei Zustände, die
verschiedene Handgriffe brauchen. `--anleitung` gibt die nötigen Befehle aus.

Pflicht sind: **Python 3.11+**, **git**, **Ollama**, **systemd (user)** und **Postgres**
(erreichbar, nicht zwingend lokal). Optional: SSH und WireGuard für mehrere Rechner,
`nvidia-smi` für die GPU-Verwaltung.

### 2. Python-Umgebung

```bash
cd system/framework
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Datenbank anlegen

Das Schema ist **idempotent** — zweimal anwenden ändert nichts:

```bash
for f in sql/0*.sql; do psql "$DATENBANK_URL" -f "$f"; done
```

Das Passwort kommt aus dem Keyring oder aus `FIRMA_PG_PASSWORT`, **nie** aus einer Datei im
Repository (siehe `speicher.py`).

### 4. Konfigurieren

Drei Dateien, alle mit Beispielwerten vorbelegt:

| Datei | Was hinein muss |
|---|---|
| `system/framework/firma.config.json` | Matrix-Homeserver, Ollama-Adresse, Ihr eigener Matrix-Name |
| `system/framework/hosts.json` | Ihre Rechner: Kerne, RAM, VRAM, Architektur — **gemessen, nicht geschätzt** |
| `system/mitarbeiter/buchhalter/broker/buchhalter.config.json` | Budget, Kontingente, Zeitgrenzen |

Zu `hosts.json` ein Rat aus Erfahrung: Tragen Sie nur ein, was Sie **gemessen** haben. Bei
einem Raspberry Pi Zero war die Annahme „trägt einen Agenten" falsch — widerlegt erst durch
den Versuch. Und bei einem zweiten PC stand „GTX 1060, 6 GB" im Inventar, verbaut war eine
1050 Ti mit 4 GB.

### 5. Ein Angestellter anlegen

Kopieren Sie `system/mitarbeiter/demo/` und geben Sie dem Ordner einen Namen — der
Ordnername *ist* die `agent_id`:

```bash
cp -r system/mitarbeiter/demo system/mitarbeiter/meine-assistenz
```

Dann `personalakte.json` anpassen (Rolle, Werkzeuge, Anforderungen) und in
`persoenlichkeit.md` den Abschnitt **„System-Prompt (Laufzeit)"** schreiben. Nur dieser
Abschnitt wird dem Modell vorgelegt; alles andere ist für Menschen.

### 6. Starten

```bash
cd system/framework
venv/bin/python mitarbeiter_agent.py meine-assistenz
```

Läuft es, dann als Dienst (`deploy/install.sh` legt die Units an). Für den Dauerbetrieb
lohnt der Broker: Er ist das **einzige Tor** zur GPU, räumt Speicher frei, bevor ein Modell
lädt, und verhindert, dass derselbe Agent zweimal gleichzeitig denkt.

### 7. Prüfen

```bash
venv/bin/python -m palette --zeigen     # was würde laufen, wo
venv/bin/python -m palette --sollwerte  # Prüfsummen der Testdateien hinterlegen
venv/bin/python -m palette              # Lauf
```

---

## Was diese Fassung ist — und was nicht

Dies ist die **öffentliche Ableitung** eines privat betriebenen Systems, erzeugt von
`oeffentliche-version.py`. Der Code ist echt, die Daten sind es nicht:

- **Eine Demo-Personalakte** statt der echten Angestellten. Deren Persönlichkeiten sind aus
  privatem Material destilliert und bleiben privat. Was hier gezeigt wird, ist die
  **Struktur** einer Akte.
- Die Angestellten heißen nach ihrer **Rolle** (`buchhalter`, `technik`, `archiv`) statt
  nach ihren Namen.
- **Beispielkonfigurationen** mit erfundenen Adressen. Das Prinzip ist echt, die Werte nicht.
- Domain, Netzadressen, Benutzerpfade und Rechnernamen sind neutralisiert. Der Inhaber
  heißt durchgehend `chef`.

Sie ist deshalb **nicht ohne Konfiguration lauffähig** — Schritt 4 ist keine Formalie.

---

## Woher die Kommentare kommen

Der Code kommentiert auffällig viel *Warum*, oft mit Datum. Fast jede dieser Notizen steht
für einen Fehler, der wirklich passiert ist und **still** blieb:

- `dict.get(k, 0)` gab `None` zurück, weil der Schlüssel *mit* `None` belegt war — der
  Standardwert greift nur bei *fehlendem* Schlüssel.
- `SET application_name = %s` warf einen Syntaxfehler, den der Aufrufer abfing. Die
  Einmal-Sperre war wochenlang wirkungslos, ohne dass etwas rot wurde.
- Eine Erreichbarkeitsprüfung nahm pauschal SSH auf Port 22 — und erklärte damit einen
  SSH-**Client** für tot.
- Ein Tageslimit wurde an zwei Stellen unabhängig gelesen. Die Änderung wirkte an einer,
  der Status meldete weiter den alten Wert.
- Das Aufräumen des Grafikspeichers entlud das Modell, das es gerade laden wollte.

**`HAUSORDNUNG.md`** fasst die Lehren in zwölf Punkten zusammen und ist zugleich der
Maßstab, gegen den die Clean-Code-Kategorie der Testpalette urteilt. Wer hier mitarbeitet,
findet dort die Regeln — jede mit dem Fehler, aus dem sie entstand.

---

## Aufbau

| Ort | Was |
|---|---|
| `system/framework/mitarbeiter_agent.py` | Die Laufzeit: ein Prozess = ein Angestellter |
| `system/framework/dispatcher.py` | Anforderung → passender Rechner |
| `system/framework/werkzeuge.py` | Werkzeug-Registry samt Rechteprüfung |
| `system/framework/denkzeit.py` | Abbruch bei Stillstand, Grübeln, Notbremse |
| `system/framework/vorgaenge.py` | Gedächtnis für angefangene Arbeit |
| `system/framework/palette/` | Testpalette, neun Kategorien |
| `system/framework/voraussetzungen.py` | Prüft, was zum Betrieb fehlt |
| `system/framework/sql/` | Schema, idempotent |
| `system/mitarbeiter/buchhalter/broker/` | Einziges Tor zu GPU und Budget |

```
system/
├── framework/              die Laufzeit
│   ├── mitarbeiter_agent.py    ein Prozess = ein Angestellter
│   ├── dispatcher.py           Anforderung -> passender Rechner
│   ├── werkzeuge.py            Registry samt Rechtepruefung
│   ├── denkzeit.py             Stillstand, Gruebeln, Notbremse
│   ├── palette/                Testpalette, neun Kategorien
│   ├── sql/                    Schema, idempotent
│   └── deploy/                 systemd-Units, Installation
├── mitarbeiter/
│   ├── demo/                   Muster-Personalakte -- hier anfangen
│   └── buchhalter/broker/      Budget, GPU-Tor, Warteschlange
└── HAUSORDNUNG.md          zwoelf Regeln, jede aus einem echten Fehler
```

---

## Für Mitarbeitende am Projekt

Der Issue-Listener (`issues.py`) holt Issues und Pull Requests beider Projekte und
sortiert sie nach Zustimmung. Er braucht einen GitHub-Token — hinterlegen mit:

```bash
bash system/scripts/token-setzen.sh
```

Der Befehl liest den Token verdeckt ein, **fragt GitHub, ob er gilt**, legt ihn im
Schlüsselbund ab und liest ihn zur Gegenprobe wieder aus. Ein hinterlegter Token, der
nicht funktioniert, ist schlimmer als keiner: Man sucht dann bei den Rechten, beim
Repo-Namen und beim Netz — überall außer an der richtigen Stelle.

Nötige Rechte (fine-grained): **Issues: Read** und **Pull requests: Read**.

## KI-Erstellt

Das Projekt wurde nach meinen Vorgaben und Moderation duch KI erstellt. Die Issues werden von einer KI bearbeitet.
Die Inspirieren kommt von der Firma Everlast AI.

## Mitmachen

Fehlerberichte und Vorschläge sind willkommen — als **Issue**. Sie werden nach
**Zustimmung** priorisiert, nicht nach Eingangsdatum: Daumen hoch zählt dreifach, ein
Kommentar doppelt, Daumen runter negativ. Ein Eingangsdatum sagt, wer zuerst geschrieben
hat — nicht, was gebraucht wird.

## Lizenz

MIT, siehe `LICENSE`.
