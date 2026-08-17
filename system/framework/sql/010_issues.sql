-- 010_issues.sql — GitHub-Issues als Arbeitsvorrat.
--
-- Chef am 2026-08-17: "Technik bekommt einen Listener für die Issues. Und setzt
-- sie für beide Projekte Issues für Issue um. Priorisiert wird nach der Anzahl
-- der Daumen hoch / der Kommentare."
--
-- WARUM DIE PUNKTE GESPEICHERT WERDEN UND NICHT NUR BERECHNET
-- Zustimmung ist eine Momentaufnahme. Ein Vorschlag mit heute zehn Daumen hatte
-- gestern vielleicht keinen. Wer nur zur Laufzeit rechnet, kann hinterher nicht
-- sagen, WARUM etwas vorgezogen wurde -- und genau das ist bei einer
-- Priorisierung die interessante Frage.
--
-- WARUM (repo, nummer) DER SCHLUESSEL IST
-- Issue-Nummern sind je Repository fortlaufend: Es gibt #1 zweimal. Nur die
-- Nummer als Schluessel zu nehmen wuerde die beiden Projekte vermischen.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.issue (
    repo        TEXT        NOT NULL,
    nummer      INTEGER     NOT NULL,
    titel       TEXT        NOT NULL,
    koerper     TEXT,
    -- daumen*3 - daumen_runter*3 + sonstige + kommentare*2, siehe issues.py
    punkte      INTEGER     NOT NULL DEFAULT 0,
    daumen      INTEGER     NOT NULL DEFAULT 0,
    kommentare  INTEGER     NOT NULL DEFAULT 0,
    marken      TEXT[]      NOT NULL DEFAULT '{}',
    url         TEXT,
    gesehen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = noch im Vorrat. Gesetzt = Technik hat daran gearbeitet; was dabei
    -- herauskam, steht in `ergebnis`. Bewusst kein Loeschen: Ein abgearbeitetes
    -- Issue ist die Begruendung dafuer, dass etwas im Code steht.
    bearbeitet  TIMESTAMPTZ,
    ergebnis    TEXT,

    PRIMARY KEY (repo, nummer)
);

CREATE INDEX IF NOT EXISTS issue_vorrat_idx
    ON firma.issue (punkte DESC, nummer ASC) WHERE bearbeitet IS NULL;

-- Was liegt an, und was ist erledigt? Getrennt je Projekt, weil das
-- oeffentliche Repo andere Erwartungen weckt als das private.
CREATE OR REPLACE VIEW firma.issue_vorrat AS
SELECT repo,
       count(*)                                        AS gesamt,
       count(*) FILTER (WHERE bearbeitet IS NULL)       AS offen,
       count(*) FILTER (WHERE bearbeitet IS NOT NULL)   AS erledigt,
       max(punkte) FILTER (WHERE bearbeitet IS NULL)    AS beste_punkte,
       sum(daumen)                                     AS daumen_gesamt
FROM firma.issue
GROUP BY repo;

-- Nachtrag 2026-08-17: Pull Requests kommen dazu (Chef: "fuer ihre Issues und
-- Pulls"). Ein PR ist bereits Arbeit -- jemand hat Zeit investiert und wartet,
-- und sein Code veraltet, waehrend ein Issue geduldig ist. Deshalb ein
-- moderater Zuschlag in issues.py, aber dieselbe Tabelle: Es ist EIN Vorrat.
ALTER TABLE firma.issue
    ADD COLUMN IF NOT EXISTS art TEXT NOT NULL DEFAULT 'issue';

COMMENT ON COLUMN firma.issue.art IS 'issue | pull';
