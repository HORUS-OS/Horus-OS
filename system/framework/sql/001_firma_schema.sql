-- 001_firma_schema.sql — Bewegungsdaten der Firma nach Postgres.
--
-- Schritt 3 des Plans wandernde-werkbank. Trennlinie ist die AENDERUNGSFREQUENZ:
-- personalakte.json und persoenlichkeit.md bleiben im Git (aendern sich selten,
-- sollen Historie haben), die Journale wandern hierher (aendern sich staendig).
-- Sie erzeugten schon mit EINEM schreibenden Geraet Rauschen im Git-Verlauf.
--
-- Idempotent: laesst sich gefahrlos erneut ausfuehren.

CREATE SCHEMA IF NOT EXISTS firma;

-- --------------------------------------------------------------------------
-- Vorgaenge — echte Spalten, weil hier gefiltert und gezaehlt wird.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firma.vorgaenge (
    id            TEXT PRIMARY KEY,          -- "<unixzeit>-<lfd>", wie bisher
    agent_id      TEXT        NOT NULL,      -- wem der Vorgang gehoert
    anliegen      TEXT        NOT NULL,
    wartet_auf    TEXT,                      -- agent_id der zustaendigen Person
    fuer          TEXT        NOT NULL DEFAULT 'chef',   -- wer das Ergebnis bekommt
    raum          TEXT,
    eroeffnet     TIMESTAMPTZ NOT NULL DEFAULT now(),
    geschlossen   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'offen',
    nachgefasst   BOOLEAN     NOT NULL DEFAULT false,
    pn_offen      BOOLEAN     NOT NULL DEFAULT false,
    dauer_s       DOUBLE PRECISION,

    -- Bewertungsfeld (Plan: Schritt 3). Kostet beim Schema-Entwurf fast nichts
    -- und ist spaeter die Metrik fuer eine moegliche DSPy-Optimierung der
    -- internen Kommunikation. Ohne dieses Feld muesste nachtraeglich gelabelt
    -- werden -- und das geht bei Gespraechsverlaeufen praktisch nicht.
    rueckfragen   INTEGER     NOT NULL DEFAULT 0,   -- wie oft musste nachgefragt werden
    glatt         BOOLEAN GENERATED ALWAYS AS (rueckfragen = 0) STORED,
    CONSTRAINT vorgaenge_status_gueltig
        CHECK (status IN ('offen', 'erledigt', 'abgebrochen', 'aufgegeben'))
);

CREATE INDEX IF NOT EXISTS vorgaenge_agent_idx    ON firma.vorgaenge (agent_id, status);
CREATE INDEX IF NOT EXISTS vorgaenge_wartet_idx   ON firma.vorgaenge (wartet_auf) WHERE status = 'offen';
CREATE INDEX IF NOT EXISTS vorgaenge_eroeffnet_idx ON firma.vorgaenge (eroeffnet DESC);

-- --------------------------------------------------------------------------
-- Taetigkeitsberichte — bisher null Stueck, obwohl die Probezeit-Bewertung
-- darauf aufbaut (Befund aus der Bestandsaufnahme). Gehen zusaetzlich nach
-- Qdrant, damit archiv_suchen sie findet.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firma.berichte (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    tag         DATE        NOT NULL,
    art         TEXT        NOT NULL DEFAULT 'aufgabe',   -- aufgabe | tagesabschluss
    text        TEXT        NOT NULL,
    vorgang_id  TEXT REFERENCES firma.vorgaenge (id) ON DELETE SET NULL,
    erstellt    TIMESTAMPTZ NOT NULL DEFAULT now(),
    vektorisiert BOOLEAN    NOT NULL DEFAULT false   -- schon in Qdrant?
);

CREATE INDEX IF NOT EXISTS berichte_agent_tag_idx ON firma.berichte (agent_id, tag DESC);
CREATE INDEX IF NOT EXISTS berichte_offen_idx     ON firma.berichte (vektorisiert) WHERE NOT vektorisiert;

-- --------------------------------------------------------------------------
-- Kennzahlen und Bilanz — als JSONB. Ihre Struktur ist verschachtelt
-- (kontakt/token_verbrauch/aufgaben/berichte) und aendert sich mit der
-- Bewertungslogik. Starre Spalten waeren hier eine Fessel; gefiltert wird
-- ohnehin nur ueber agent_id und Stand.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firma.kennzahlen (
    agent_id  TEXT        NOT NULL,
    stand     DATE        NOT NULL,
    daten     JSONB       NOT NULL,
    erfasst   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, stand)
);

CREATE TABLE IF NOT EXISTS firma.bilanz (
    agent_id  TEXT PRIMARY KEY,
    daten     JSONB       NOT NULL,
    erfasst   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Blick fuer die Probezeit-Bewertung: was bisher aus kennzahlen.json kam.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW firma.uebersicht AS
SELECT v.agent_id,
       count(*)                                             AS vorgaenge_gesamt,
       count(*) FILTER (WHERE v.status = 'offen')           AS offen,
       count(*) FILTER (WHERE v.status = 'erledigt')        AS erledigt,
       count(*) FILTER (WHERE v.status IN ('abgebrochen','aufgegeben')) AS abgebrochen,
       count(*) FILTER (WHERE v.glatt AND v.status = 'erledigt')        AS ohne_rueckfrage,
       round(avg(v.dauer_s)::numeric, 1)                    AS dauer_schnitt_s,
       max(v.eroeffnet)                                     AS zuletzt
FROM firma.vorgaenge v
GROUP BY v.agent_id;
