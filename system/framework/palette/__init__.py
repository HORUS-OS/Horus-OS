"""palette — die Testpalette, Stufe vor der Sandbox-Beta.

Der Weg zum Rollout lautet:  Palette -> Sandbox-Beta (VPS + PC) -> Rollout.

Ein Kategorie-Modul heisst `k_<name>.py` und stellt genau eines bereit:

    TESTS = [
        {"name": "kurzer_bezeichner",
         "lauf": funktion,               # -> (bestanden: bool, befund: str, messwert: dict)
         "anforderung": {...}},          # optional, ueberschreibt die der Kategorie
    ]

Mehr braucht es nicht. Ein neuer Test ist damit ein Eintrag in einer Liste und
keine Aenderung am Laeufer -- der Rahmen soll nicht jedes Mal angefasst werden,
wenn eine Pruefung dazukommt.
"""
