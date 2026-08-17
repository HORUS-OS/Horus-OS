# Hausordnung für den Code der Firma

Maßstab für die Clean-Code-Prüfung der Testpalette. Chef hat entschieden: **kein Linter**
(F5 = A), sondern **Techniks Urteil gegen eine geschriebene Ordnung** (F14 = B), übernommen
aus einem gängigen Stilleitfaden (F14.B.4 = b).

Basis ist **PEP 8** und der [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
Was dort steht, gilt hier, ohne es abzuschreiben. Dieses Dokument nennt nur die Punkte,
bei denen die Firma **abweicht oder nachschärft** — und jeder davon stammt aus einem
Fehler, der wirklich passiert ist.

## Warum es diese Ordnung überhaupt gibt

Ein Urteil ohne Maßstab schwankt von Lauf zu Lauf. Dasselbe Diff könnte zweimal
verschieden bewertet werden, und niemand könnte einer Bewertung widersprechen. Die
Ordnung macht das Urteil **anfechtbar**: Technik muss sagen, gegen welchen Punkt etwas
verstößt.

---

## 1. Sprache

- **Kommentare und Namen auf Deutsch.** Die Firma denkt deutsch, ihre Angestellten
  sprechen deutsch mit Chef. Englische Bezeichner mitten in deutschen Sätzen sind
  schwerer zu lesen, nicht leichter.
- **Fachbegriffe bleiben im Original:** `commit`, `Timeout`, `Token`, `Backend`.
- **In Python-Quelltext keine Umlaute in Bezeichnern**, in Kommentaren nach Möglichkeit
  `ae/oe/ue` — mehrere Werkzeuge im Baum lesen Dateien ohne Kodierungsangabe.

## 2. Kommentare erklären das WARUM, nicht das WAS

Ein Kommentar, der den Code nachspricht, ist Ballast:

```python
# schlecht — sagt nur, was dasteht
i += 1                      # i um eins erhoehen

# gut — sagt, warum es so sein muss
i += 1                      # Der Zaehler steht auf dem letzten gelesenen Satz,
                            # nicht auf dem naechsten -- sonst wird einer uebersprungen.
```

**Regel:** Bei jeder nicht offensichtlichen Entscheidung steht dabei, welche Alternative
verworfen wurde und warum. Wer das später ändern will, soll wissen, was er aufgibt.

## 3. Eine Zahl, eine Quelle

Der teuerste Fehler der Firma bisher. Der Tagesdeckel wurde an **zwei** Stellen
unabhängig aus der Konfiguration gelesen; eine Änderung wirkte nur an einer, und der
Status meldete weiter den alten Wert.

- Ein Wert, der an mehreren Stellen gebraucht wird, hat **eine** Funktion, die ihn liefert.
- Ein abgeleiteter Wert wird **berechnet**, nicht abgeschrieben.
  Beispiel: `BuchhalterBackend.timeout` ist `denkzeit.MAX_S + 1800` — vorher stand dort
  `5400`, passend zu einer Notbremse von 3600, und wurde still falsch, als die stieg.
- Steht ein Wert doppelt, weil es technisch nicht anders geht (Konfiguration überschreibt
  Vorgabe), muss **an beiden Stellen** ein Hinweis darauf stehen.

## 4. `or 0` statt Standardwert bei Fremddaten

```python
d.get("x", 0)        # liefert None, wenn der Schluessel MIT None belegt ist
d.get("x") or 0      # richtig
```

Der Standardwert greift nur bei **fehlendem** Schlüssel. JSON kennt `null`, also kommt
das vor. Gilt auch dort, wo es harmlos ist: Eine Regel, die an drei von vier Stellen gilt,
ist keine Regel.

## 5. Zustand messen, nicht glauben

Ein Flag sagt, was **einmal** galt.

- `_ensure_gpu()` prüfte `firm_holds_gpu` und tat nichts, während LM Studio längst 13,5 GB
  VRAM hielt. Jetzt wird der freie Speicher gemessen.
- `erreichbar()` nahm pauschal SSH:22 und erklärte den Haupt-PC für tot — der ist
  SSH-Client, kein Server.
- Der Portscan muss von **außen** kommen: Lokal meldete `ufw` „dicht", während Docker-DNAT
  den Port ins Internet stellte.

**Regel:** Wo eine Tatsache prüfbar ist, wird sie geprüft. Ein gespeicherter Zustand ist
ein Hinweis, kein Beweis.

## 6. Unwissen ist kein Befund

„Ich weiß es nicht" darf nie wie „es ist kaputt" aussehen.

- Nach einem Broker-Neustart war die Präsenzliste leer, und ein Agent meldete prompt einen
  Ausfall. `/praesenz` liefert deshalb `verlaesslich`.
- Ein Palettenlauf über geänderte Testdateien ist `entwertet`, nicht `gescheitert`.

## 7. Ausnahmen nicht stumm schlucken

```python
try:
    ...
except Exception:
    pass                    # verboten, wenn dahinter eine Wirkung erwartet wird
```

Die Einmal-Sperre war wochenlang wirkungslos, weil der Aufrufer die Ausnahme abfing. Es
gab nichts zu sehen — nur eine Sperre, die nicht sperrte.

**Regel:** Ein `except` ohne Protokollierung ist nur dort erlaubt, wo der Fehlschlag
**folgenlos** ist, und das muss im Kommentar stehen.

## 8. Was zum Gerät gehört, gehört nicht in die Firma

Dreimal derselbe Fehler an einem Tag, als der zweite PC dazukam:

| Wurde firmenweit gesetzt | Gehört zum Knoten |
|---|---|
| `default_model` | Ein 4-GB-Knoten trägt kein 24-GB-Modell |
| `num_ctx`, `num_gpu` | Für 16 GB gemessen, sprengen 4 GB |
| Entladen beim Freigeben | Kannte nur den lokalen Knoten |

**Regel:** Alles, was von der Hardware abhängt, steht in `hosts.json` und wird von dort
gelesen.

## 9. Anforderungen statt Gerätenamen

`laeuft_auf: "haupt-pc"` nannte vier verschiedene Geräte, während alle vier Agenten auf
demselben Rechner liefen — kein Code las das Feld je aus. Eine Anforderung
(`braucht_gpu`, `vram_gb`, `arch`) ist prüfbar, ein Gerätename ist eine Behauptung.

## 10. Namen

- Funktionen sind **Verben** (`pruefen`, `entladen`, `abgleichen`), Werte sind
  **Substantive** (`ergebnis`, `frist_s`).
- Einheiten in den Namen: `frist_s`, `ram_mb`, `betrag_eur`. `timeout = 90` ist eine
  Einladung zum Missverständnis.
- Abkürzungen nur, wenn sie im Haus üblich sind (`cfg`, `akte`, `pq`).

## 11. Modulkopf

Jedes Modul beginnt mit einem Docstring, der drei Fragen beantwortet:

1. **Was** tut es (ein Satz)?
2. **Warum** gibt es es — welches Problem war vorher da?
3. **Was tut es bewusst NICHT**, und warum nicht?

Der dritte Punkt ist der wichtigste. `sandbox.py` sagt ausdrücklich, dass es keine
Produktions-Units anfasst: Eine Sandbox, die im Fehlerfall die Produktion beschädigt,
macht das Problem größer statt kleiner.

## 12. Was diese Ordnung NICHT verlangt

- **Keine Zeilenlängen-Pedanterie.** 88 Zeichen sind ein Ziel, kein Gesetz.
- **Keine Typannotationen überall.** Wo sie helfen, gern; als Pflicht wären sie Lärm.
- **Keine englischen Docstrings.** Siehe Punkt 1.
- **Keine Tests um der Abdeckung willen.** Ein Test muss einen Fehler fangen können, der
  wirklich vorkam — siehe `palette/k_bekannte_fehler.py`.

---

## Für Technik: wie zu urteilen ist

Du bekommst **Diff und vollständige Datei** (F15 = b) und urteilst **Datei für Datei**,
am Ende mit einer Zusammenfassung (F15.b.4 = a).

- Nenne bei jedem Befund die **Nummer** des Punktes, gegen den er verstößt. Ein Befund
  ohne Fundstelle in dieser Ordnung ist kein Befund, sondern Geschmack.
- Diese Kategorie **warnt nur** (F1.B.3 = b). Sie hält keinen Rollout auf, also gibt es
  keinen Grund, einen Verstoß zu erfinden — und keinen, einen zu verschweigen.
- Du darfst diese Datei **nicht ändern.** Du wählst, welche Prüfungen laufen, nicht was
  sie prüfen (F21). Der Prüfsummen-Schutz erzwingt das: Ein Lauf über eine geänderte
  Ordnung gilt nicht.
