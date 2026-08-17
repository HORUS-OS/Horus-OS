# Mitarbeiter-Framework

Die Laufzeit, die **eine Personalakte als lebenden Matrix-Kollegen** betreibt. Ein Prozess pro
Angestelltem, derselbe Code für alle, config-getrieben und pro Gerät deploybar.

```
mitarbeiter_agent.py <agent_id>     # z.B. assistenz | projektleitung | archivar | buchhalter
```

## Was ein Agent tut

1. Lädt `../<agent_id>/personalakte.json` + `persoenlichkeit.md` (Abschnitt „System-Prompt (Laufzeit)").
2. Loggt sich als `@<agent_id>:example.org` ein — Passwort aus dem Firmen-Keyring
   (`matrix-pw-<agent_id>` via `firma-keyring.py`), Token danach in `state_dir/<agent_id>/session.json`.
3. Antwortet in **DMs** und im **Forum nur bei @-Erwähnung** (verhindert Bot-Endlosschleifen).
4. Denkt über das **Backend** (`backends.py` → aktuell lokal qwen3:8b). Die Funktion `get_backend()`
   ist die **Naht für den Buchhalter** — er hängt hier später die Kosten-Kaskade + GPU-Kontingente ein.
5. Zählt Kontakte mit Chef in `kennzahlen.json` → **primäre Probezeit-Kennzahl**.

## Konfiguration

`firma.config.json` — Homeserver, Ollama-URL, erlaubte User/Agenten, Forum-Raum, `state_dir`,
`encryption`. Gilt für alle Agenten gemeinsam.

## Deployment (Grundlage — noch nicht auf die Pis ausgerollt)

Pro Angestelltem, auf dem Zielgerät (Pi 4 = Assistenz, Pi 3 = Archivar, Alt-PC = Projektleitung, Haupt-PC = Buchhalter):

1. Matrix-Konto anlegen (`register_new_matrix_user`), Passwort in den Keyring:
   `printf '%s' '<pw>' | /usr/bin/python3 …/scripts/firma-keyring.py store matrix-pw-<agent_id>`
2. venv + Requirements: `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`
3. Dienst: `cp deploy/mitarbeiter@.service ~/.config/systemd/user/ && systemctl --user enable --now mitarbeiter@<agent_id>`

**E2E-Verschlüsselung:** `firma.config.json` → `"encryption": true`, dazu `matrix-nio[e2e]` +
System-Paket `libolm-dev`. Standard jetzt: aus (Grundlage zuerst).

## Status

Code vollständig; **live erst lauffähig, wenn die Matrix-Konten existieren** (nächster Schritt).
Bis dahin lässt sich alles außer dem Matrix-Login trocken prüfen (Akte laden, System-Prompt, Backend).
