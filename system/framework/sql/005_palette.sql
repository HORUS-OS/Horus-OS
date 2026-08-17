-- 005_palette.sql — die Testpalette, Stufe VOR der Sandbox-Beta.
--
-- Der Weg zum Rollout lautet ab jetzt:
--     Palette  ->  Sandbox-Beta (VPS + PC)  ->  Rollout
--
-- WARUM DER KNOTENNAME AM MESSWERT HAENGT (F22.B.4 = b)
-- Die Palette bindet sich nicht an einen Rechner: Jeder Test laeuft dort, wo er
-- etwas beweisen kann (F2, F22 = B). Damit ist ein nackter Messwert wertlos --
-- 96 s Denkzeit auf dem Haupt-PC und 96 s auf dem Pi sind nicht dieselbe
-- Aussage. Der Knoten steht deshalb NEBEN dem Wert und nicht am Lauf.
--
-- WARUM ES EINE ZWEITE TABELLE NUR FUER PRUEFSUMMEN GIBT (F26 = B)
-- Technik darf waehlen, WELCHE Tests laufen, aber nicht, WAS sie pruefen. Eine
-- Absprache reicht dafuer nicht -- genau das wurde beim Upgrade-Register schon
-- einmal bewusst vermieden. palette_datei haelt den Sollwert an einem Ort, den
-- ihre system_*-Werkzeuge nicht erreichen. Eine geaenderte Testdatei hindert
-- sie nicht am Lauf, sie ENTWERTET ihn: das Ergebnis zaehlt dann nicht mehr
-- als Nachweis.
--
-- WARUM 'entwertet' KEIN SYNONYM FUER 'gescheitert' IST
-- Gescheitert heisst: der Stand ist schlecht. Entwertet heisst: ueber den Stand
-- ist nichts bekannt. Der Rollout verweigert beides, aber nur das eine ist ein
-- Befund ueber den Code.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.palette_lauf (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    commit_id       TEXT        NOT NULL,
    -- 'laeuft' ist der Anfangszustand. Ein Lauf, der in diesem Zustand stehen
    -- bleibt, ist abgestuerzt -- ohne ihn saehe ein Absturz aus wie "nie
    -- gelaufen" (derselbe blinde Fleck wie beim haengenden Auftrag am 16.08.).
    ergebnis        TEXT        NOT NULL DEFAULT 'laeuft',
    -- Welche Kategorien angefordert waren. NULL heisst: alle. Das ist Techniks
    -- einzige Stellschraube und wird deshalb mitprotokolliert.
    nur             TEXT[],
    -- Kategorie, nach der abgebrochen wurde (F23 = B: abgebrochen wird
    -- ZWISCHEN Kategorien, nie mitten in einer).
    abgebrochen_bei TEXT,
    gestartet       TIMESTAMPTZ NOT NULL DEFAULT now(),
    beendet         TIMESTAMPTZ,

    CONSTRAINT palette_ergebnis_gueltig
        CHECK (ergebnis IN ('laeuft', 'bestanden', 'gescheitert', 'entwertet'))
);

CREATE INDEX IF NOT EXISTS palette_lauf_commit_idx
    ON firma.palette_lauf (commit_id, gestartet DESC);

CREATE TABLE IF NOT EXISTS firma.palette_ergebnis (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lauf_id     BIGINT      NOT NULL REFERENCES firma.palette_lauf (id)
                            ON DELETE CASCADE,
    kategorie   TEXT        NOT NULL,
    test        TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    -- 'sperrt' | 'warnt' -- zum Zeitpunkt des Laufs, nicht zum Zeitpunkt der
    -- Auswertung. Wird die Haerte spaeter geaendert, bleibt nachvollziehbar,
    -- wonach damals entschieden wurde.
    haerte      TEXT        NOT NULL,
    befund      TEXT,
    -- Zahlen zum Vergleichen (Dauer, Durchsatz, Trefferzahl). Frei, weil jede
    -- Kategorie etwas anderes misst.
    messwert    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Siehe Kopf: ohne Herkunft ist der Messwert nicht vergleichbar.
    knoten      TEXT        NOT NULL,
    dauer_s     NUMERIC(9,2),
    gelaufen    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT palette_status_gueltig
        CHECK (status IN ('bestanden', 'fehlgeschlagen', 'uebersprungen')),
    CONSTRAINT palette_haerte_gueltig
        CHECK (haerte IN ('sperrt', 'warnt'))
);

CREATE INDEX IF NOT EXISTS palette_ergebnis_lauf_idx
    ON firma.palette_ergebnis (lauf_id, kategorie);

-- Vergleichbarkeit ueber Laeufe hinweg: derselbe Test auf demselben Knoten.
CREATE INDEX IF NOT EXISTS palette_ergebnis_verlauf_idx
    ON firma.palette_ergebnis (kategorie, test, knoten, gelaufen DESC);

CREATE TABLE IF NOT EXISTS firma.palette_datei (
    pfad        TEXT        PRIMARY KEY,     -- relativ zum Repo-Wurzelverzeichnis
    pruefsumme  TEXT        NOT NULL,        -- sha256, hex
    hinterlegt  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Wer den Sollwert gesetzt hat. Ein Sollwert ohne Herkunft ist kein Schutz,
    -- sondern nur eine zweite Kopie desselben Zustands.
    von         TEXT        NOT NULL DEFAULT 'chef'
);

-- Der juengste Lauf je Commit. Ein spaeterer Fehlschlag hebt ein frueheres
-- Bestehen auf -- sonst koennte man so lange laufen lassen, bis es einmal
-- klappt, und sich darauf berufen. Dieselbe Regel wie bei beta_freigabe.
CREATE OR REPLACE VIEW firma.palette_freigabe AS
SELECT DISTINCT ON (commit_id)
       commit_id,
       ergebnis,
       (ergebnis = 'bestanden')  AS bestanden,
       nur,
       abgebrochen_bei,
       gestartet,
       beendet
FROM firma.palette_lauf
ORDER BY commit_id, gestartet DESC;
