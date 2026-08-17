#!/usr/bin/env python3
"""
horus-github-keyring.py — GitHub-Token im GNOME Keyring

Verwendung:
  python3 horus-github-keyring.py store    # Token einmalig hinterlegen
  python3 horus-github-keyring.py get      # Token ausgeben (fuer issues.py)
  python3 horus-github-keyring.py delete   # Eintrag loeschen

Der Token gehoert NICHT in eine Datei im Repo und nicht in die Shell-Historie.
Benoetigte Rechte (fine-grained): Issues lesen fuer beide Repos; fuer das
Freischalten zusaetzlich Administration. Lesen genuegt fuer den Listener --
issues.py schreibt nichts auf GitHub.
"""
import sys
import gi
gi.require_version('Secret', '1')
from gi.repository import Secret

SCHEMA = Secret.Schema.new(
    "ai.horus-os.sudo",
    Secret.SchemaFlags.NONE,
    {"service": Secret.SchemaAttributeType.STRING}
)

ATTRIBUTES = {"service": "horus-github"}
LABEL = "Horus-OS sudo password"


def store():
    """Token ablegen. Mit --stdin liest es von der Standardeingabe.

    WARUM NICHT ALS ARGUMENT
    Ein Token in argv steht in der Prozessliste und ist fuer jeden Nutzer des
    Systems lesbar, solange der Aufruf laeuft. stdin ist nicht sichtbar.
    """
    import getpass
    if "--stdin" in sys.argv:
        password = sys.stdin.read().strip()
        if not password:
            print("Nichts auf der Standardeingabe.")
            raise SystemExit(1)
    else:
        password = getpass.getpass("GitHub-Token eingeben (wird nicht angezeigt): ")
    Secret.password_store_sync(
        SCHEMA, ATTRIBUTES, Secret.COLLECTION_DEFAULT,
        LABEL, password, None
    )
    print("Token sicher im GNOME Keyring gespeichert.")


def get():
    password = Secret.password_lookup_sync(SCHEMA, ATTRIBUTES, None)
    if password is None:
        sys.stderr.write("Kein Passwort gefunden. Bitte zuerst: horus-keyring.py store\n")
        sys.exit(1)
    print(password, end="")


def delete():
    removed = Secret.password_clear_sync(SCHEMA, ATTRIBUTES, None)
    if removed:
        print("Eintrag aus GNOME Keyring gelöscht.")
    else:
        print("Kein Eintrag gefunden.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "store":
        store()
    elif cmd == "get":
        get()
    elif cmd == "delete":
        delete()
    else:
        print(__doc__)
        sys.exit(1)
