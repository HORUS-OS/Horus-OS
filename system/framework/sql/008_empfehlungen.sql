-- 008_empfehlungen.sql — Buchhalters Empfehlungen als Gedaechtnis.
--
-- Chef am 2026-08-17: "Buchhalter soll seine Kauf-/Verkauf-Empfehlungen speichern
-- und mir melden, damit er sie als Referenz behaelt. Sie sind dann eben als
-- Geschaeftsgeheimnisse klassifiziert."
--
-- WARUM EINE EMPFEHLUNG OHNE GEDAECHTNIS WERTLOS IST
-- Bisher endete jede Empfehlung in einem Vorgang, der irgendwann geschlossen
-- wurde. Damit fehlte das Wichtigste: Was ist daraus geworden? Ein Buchhalter,
-- der nicht weiss, wie seine letzten fuenf Ratschlaege ausgegangen sind, gibt
-- den sechsten mit derselben Sicherheit wie den ersten. Erst der Abgleich
-- zwischen Empfehlung und Ausgang macht aus Meinung Erfahrung.
--
-- KLASSIFIZIERUNG
-- `geheim` ist keine Zierde: privatfilter.py entscheidet, ob ein Gedanke das
-- Haus verlassen darf. Eine Empfehlung nennt Betraege und Absichten zum
-- Privatvermoegen -- das geht keinen Cloud-Anbieter etwas an. Der Vermerk steht
-- an der Zeile, nicht in einer Regel anderswo: Wer die Daten liest, sieht sofort,
-- woran er ist.
--
-- WAS HIER NICHT STEHT
-- Kein Wertpapiername, keine ISIN, keine Depotnummer. Eine Empfehlung wird als
-- BETRAG und RICHTUNG festgehalten, nicht als Anlagetipp -- die Auswahl von
-- Wertpapieren ist Anlageberatung und ausdruecklich nicht Teil dieses Plans.
-- Wo Chef selbst ein Papier nennt, steht es in `notiz`; erfunden wird keines.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.empfehlung (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    erstellt      TIMESTAMPTZ NOT NULL DEFAULT now(),
    von           TEXT        NOT NULL DEFAULT 'buchhalter',
    -- 'kaufen' | 'verkaufen' | 'umschichten' | 'halten' | 'einzahlen'
    richtung      TEXT        NOT NULL,
    betrag_eur    NUMERIC(12,2),
    -- Die Begruendung in Buchhalters Worten. Sie ist der eigentliche Wert des
    -- Eintrags: Ein Betrag ohne Begruendung laesst sich spaeter nicht bewerten.
    begruendung   TEXT        NOT NULL,
    -- Die Zahlen, auf denen die Empfehlung beruht (Quote, Stand, Reichweite).
    -- Frei als JSON, weil jede Empfehlungsart auf anderen Groessen fusst.
    grundlage     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Geschaeftsgeheimnis (Chef, 17.08.). Bewusst mit Vorgabe TRUE: Wer eine
    -- Ausnahme will, muss sie hinschreiben -- nicht umgekehrt.
    geheim        BOOLEAN     NOT NULL DEFAULT TRUE,

    -- Was daraus wurde. NULL heisst: noch offen. Genau dieses Feld macht aus
    -- der Sammlung eine Referenz statt eines Archivs.
    ausgang       TEXT,                   -- 'befolgt' | 'abgelehnt' | 'verfallen'
    ausgang_am    TIMESTAMPTZ,
    notiz         TEXT,                   -- des Chefs Anmerkung, falls er eine gibt

    CONSTRAINT empfehlung_richtung_gueltig
        CHECK (richtung IN ('kaufen', 'verkaufen', 'umschichten', 'halten', 'einzahlen')),
    CONSTRAINT empfehlung_ausgang_gueltig
        CHECK (ausgang IS NULL OR ausgang IN ('befolgt', 'abgelehnt', 'verfallen'))
);

CREATE INDEX IF NOT EXISTS empfehlung_offen_idx
    ON firma.empfehlung (erstellt DESC) WHERE ausgang IS NULL;

-- Buchhalters Bilanz ueber sich selbst. Ohne diese Sicht wuesste er nicht, ob
-- seine Ratschlaege ueberhaupt angenommen werden -- und ein Rat, der nie
-- befolgt wird, ist ein Rat, der falsch gestellt ist.
CREATE OR REPLACE VIEW firma.empfehlung_bilanz AS
SELECT richtung,
       count(*)                                          AS gesamt,
       count(*) FILTER (WHERE ausgang IS NULL)            AS offen,
       count(*) FILTER (WHERE ausgang = 'befolgt')        AS befolgt,
       count(*) FILTER (WHERE ausgang = 'abgelehnt')      AS abgelehnt,
       count(*) FILTER (WHERE ausgang = 'verfallen')      AS verfallen,
       min(erstellt)                                      AS erste,
       max(erstellt)                                      AS letzte
FROM firma.empfehlung
GROUP BY richtung;
