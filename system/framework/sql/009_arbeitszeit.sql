-- 009_arbeitszeit.sql — des Chefs Arbeitszeiten, erfasst durch Nachfragen.
--
-- Chef am 2026-08-17: "Assistenz soll mich regelmaessig unter der Woche um 14 Uhr
-- fragen, ob ich schon Feierabend gemacht habe, um meine Arbeitszeiten zu
-- erfassen."
--
-- WARUM GEFRAGT UND NICHT GEMESSEN WIRD
-- Anwesenheit am Rechner ist nicht Arbeitszeit: Chef arbeitet auswaerts, und
-- der PC laeuft auch, wenn er es nicht tut. Jede automatische Messung waere
-- eine Schaetzung, die sich fuer eine Tatsache ausgibt. Eine Frage kostet ihn
-- fuenf Sekunden und liefert die Wahrheit.
--
-- WARUM UM 14 UHR
-- des Chefs Vorgabe. Nach der Frage ist die Antwort belastbar: Wer um 14 Uhr sagt
-- "noch nicht", arbeitet noch; wer "seit 13 Uhr" sagt, erinnert sich an eine
-- Stunde, nicht an einen Tag.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.arbeitszeit (
    tag           DATE        PRIMARY KEY,
    gefragt_um    TIMESTAMPTZ,
    -- NULL heisst: gefragt, aber (noch) keine Antwort. Das ist etwas anderes
    -- als "nicht gearbeitet" -- Unwissen ist kein Befund.
    feierabend    TIME,
    arbeitsende_bekannt BOOLEAN GENERATED ALWAYS AS (feierabend IS NOT NULL) STORED,
    notiz         TEXT,
    erfasst       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ueberblick fuer das Briefing: Wie viele Tage sind erfasst, wie viele offen?
CREATE OR REPLACE VIEW firma.arbeitszeit_bilanz AS
SELECT count(*)                                        AS tage,
       count(*) FILTER (WHERE feierabend IS NOT NULL)   AS mit_feierabend,
       count(*) FILTER (WHERE feierabend IS NULL)       AS ohne_antwort,
       min(tag)                                        AS erster_tag,
       max(tag)                                        AS letzter_tag,
       avg(feierabend::interval) FILTER (WHERE feierabend IS NOT NULL) AS mittel
FROM firma.arbeitszeit;
