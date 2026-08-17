-- 004_beta.sql — Ergebnisse der Sandbox-Beta (Schritt 6 wandernde-werkbank).
--
-- Chef in Runde 04: "Es wird in einer Sandbox Beta getestet."
-- Der Plan praezisiert zweistufig:
--   1. VPS      — Installation, Dienste, Konfiguration
--   2. Haupt-PC — alles Modellabhaengige, wo die GPU sitzt
--
-- WARUM DAS ERGEBNIS AM COMMIT HAENGT UND NICHT AN DER VERSION
-- Eine Versionsnummer ist ein Etikett, das jemand vergibt; ein Commit ist der
-- Stand selbst. Haengte der Nachweis an "4.0.1", koennte zwischen Test und
-- Rollout noch etwas dazukommen -- getestet waere A, ausgerollt B, und niemand
-- saehe den Unterschied. Deshalb ist commit_id der Schluessel, und rollout.py
-- fragt nach dem Test fuer GENAU den Stand, den es gerade ausrollen will.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.beta_tests (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    commit_id   TEXT        NOT NULL,
    version     TEXT,                        -- nur zur Auskunft, nie als Schluessel
    stufe       TEXT        NOT NULL,        -- 'vps' | 'pc'
    bestanden   BOOLEAN     NOT NULL,
    -- Was genau geprueft wurde, Schritt fuer Schritt. Bei einem Fehlschlag ist
    -- das der einzige Ort, an dem hinterher steht, WORAN es lag.
    protokoll   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    dauer_s     NUMERIC(8,1),
    knoten      TEXT,
    gelaufen    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT beta_stufe_gueltig CHECK (stufe IN ('vps', 'pc'))
);

CREATE INDEX IF NOT EXISTS beta_commit_idx
    ON firma.beta_tests (commit_id, stufe, gelaufen DESC);

-- Beide Stufen bestanden -- und zwar in ihrem juengsten Lauf. Ein spaeterer
-- Fehlschlag hebt ein frueheres Bestehen also auf; sonst koennte man so lange
-- testen, bis es einmal klappt, und sich darauf berufen.
CREATE OR REPLACE VIEW firma.beta_freigabe AS
WITH juengste AS (
    SELECT DISTINCT ON (commit_id, stufe)
           commit_id, stufe, bestanden, gelaufen, version
    FROM firma.beta_tests
    ORDER BY commit_id, stufe, gelaufen DESC
)
SELECT commit_id,
       max(version)                                     AS version,
       bool_and(bestanden)                              AS beide_bestanden,
       count(*)                                         AS stufen,
       max(gelaufen)                                    AS zuletzt
FROM juengste
GROUP BY commit_id
HAVING count(*) = 2;
