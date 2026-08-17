-- 002_auftraege.sql — Auftragsqueue zwischen Scheduler (VPS) und Worker (PC).
--
-- Schritt 4 des Plans wandernde-werkbank: "Queue zwischen beiden statt HTTP --
-- sie uebersteht Verbindungsabbrueche und puffert ueber einen PC-Neustart hinweg."
--
-- Warum Postgres und nicht Redis/NATS: Die Datenbank laeuft ohnehin, ist ueber
-- das Mesh von beiden Seiten erreichbar und bietet mit
--   SELECT ... FOR UPDATE SKIP LOCKED
-- genau die Semantik, die eine Arbeitsqueue braucht -- transaktional, ohne
-- doppelte Zustellung, ohne verlorene Auftraege bei Absturz. Ein zusaetzlicher
-- Dienst waere mehr bewegliche Teile fuer weniger Garantien. Robustheit schlaegt
-- Einfachheit, und hier faellt beides zusammen.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.auftraege (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id      TEXT        NOT NULL,
    -- Der komplette Denkauftrag, so wie ihn /think heute im Speicher haelt:
    -- system, messages, quellen, raum, klassifiziert, passung, losnummer.
    auftrag       JSONB       NOT NULL,

    -- Reihenfolge: erst Prioritaetsklasse (0 = Chef), dann fachliche Passung
    -- (hoeher ist besser), dann Losnummer gegen Verhungern bei Gleichstand.
    prioritaet    SMALLINT    NOT NULL DEFAULT 3,
    passung       SMALLINT    NOT NULL DEFAULT 0,
    losnummer     INTEGER     NOT NULL DEFAULT 0,

    status        TEXT        NOT NULL DEFAULT 'wartet',
    host          TEXT,                       -- welcher Worker hat ihn geholt
    versuche      SMALLINT    NOT NULL DEFAULT 0,
    ergebnis      JSONB,
    fehler        TEXT,

    erstellt      TIMESTAMPTZ NOT NULL DEFAULT now(),
    geholt        TIMESTAMPTZ,
    -- Lebenszeichen des Workers WAEHREND der Arbeit. Zusammen mit der
    -- Erreichbarkeit des Hosts bildet das die Zwei-Signal-Ausfallerkennung:
    -- ein langsam denkender Worker sendet weiter, ein toter nicht.
    herzschlag    TIMESTAMPTZ,
    fertig        TIMESTAMPTZ,

    CONSTRAINT auftraege_status_gueltig
        CHECK (status IN ('wartet', 'laeuft', 'fertig', 'fehler', 'aufgegeben'))
);

-- Der Griff des Workers: aeltester wartender Auftrag in Rangfolge.
CREATE INDEX IF NOT EXISTS auftraege_warteschlange_idx
    ON firma.auftraege (prioritaet, passung DESC, losnummer, erstellt)
    WHERE status = 'wartet';

-- Aufsicht ueber Laufendes: findet haengende Auftraege ueber den Herzschlag.
CREATE INDEX IF NOT EXISTS auftraege_laufend_idx
    ON firma.auftraege (herzschlag)
    WHERE status = 'laeuft';

CREATE INDEX IF NOT EXISTS auftraege_agent_idx ON firma.auftraege (agent_id, erstellt DESC);

-- Praesenz der Worker, unabhaengig von einzelnen Auftraegen. Ein Worker meldet
-- sich auch dann, wenn er gerade nichts zu tun hat -- sonst waere "kein
-- Herzschlag" nicht von "keine Arbeit" zu unterscheiden.
CREATE TABLE IF NOT EXISTS firma.worker (
    host          TEXT PRIMARY KEY,
    gesehen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    gpu_frei_mb   INTEGER,
    laeuft        TEXT,                        -- agent_id des aktuellen Auftrags
    version       TEXT
);

CREATE OR REPLACE VIEW firma.warteschlange AS
SELECT status,
       count(*)                                              AS anzahl,
       min(erstellt)                                         AS aeltester,
       count(*) FILTER (WHERE herzschlag < now() - interval '3 minutes'
                          AND status = 'laeuft')             AS ohne_lebenszeichen
FROM firma.auftraege
GROUP BY status;
