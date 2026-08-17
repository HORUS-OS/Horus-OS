-- 007_konto_details.sql — Krypto sichtbar machen, Gebuehren mitfuehren.
--
-- ENTSCHEIDUNG K8 (Chef, 2026-08-17)
--     "Krypto ist auch eine Anlageform, von der ich mir langfristig Rendite
--      verspreche, aber spekulativer als die anderen."
--
-- Daraus folgt genau zwei Dinge, und nicht mehr:
--   1. Krypto zaehlt zum ANGELEGTEN Teil. Es ist kein Tagesgeld, also gehoert es
--      nicht in die liquide Haelfte der Drittelregel.
--   2. Es wird SEPARAT mitgefuehrt. "Spekulativer" ist keine Zahl, aus der man
--      eine Grenze ableiten koennte -- aber eine Groesse, die niemand sieht,
--      kann auch niemand im Blick behalten. Der Anteil steht deshalb in jeder
--      Auskunft.
--
-- Bewusst KEINE Obergrenze fuer Krypto: Chef hat eine Einschaetzung genannt,
-- keine Regel. Eine Schwelle zu erfinden waere Anlageberatung -- und die ist
-- ausdruecklich nicht Teil dieses Plans.
--
-- ENTSCHEIDUNG K6: Gebuehrenquote je Order. Bei 1,00 EUR Festgebuehr haengt sie
-- allein am Volumen. Der gemessene Ausgangszustand war eindeutig: 5 Orders zu je
-- rund 4 EUR, jede mit 25 % Quote -- der Verlust von 4,79 EUR bestand zu 104 %
-- aus Gebuehren, die Kurse standen leicht im Plus.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

-- Krypto als Teilmenge des angelegten Betrags. NICHT als dritte Summe neben
-- liquide und angelegt: Sonst gaebe es zwei Wahrheiten darueber, was "angelegt"
-- heisst, und die Drittelregel muesste wissen, welche gemeint ist.
ALTER TABLE firma.konto_stand
    ADD COLUMN IF NOT EXISTS krypto_eur NUMERIC(12,2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN firma.konto_stand.krypto_eur IS
    'Teil von angelegt_eur, der in Krypto steckt (K8: gehoert zum angelegten '
    'Teil, wird aber separat gezeigt, weil spekulativer).';

-- Orders, soweit sie aus einem Auszug hervorgehen. Nur, was fuer die
-- Gebuehrenfrage gebraucht wird -- kein Wertpapiername, keine Stueckzahl, keine
-- Depotnummer. Was hier nicht steht, kann auch nicht preisgegeben werden.
CREATE TABLE IF NOT EXISTS firma.konto_order (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    datum         DATE          NOT NULL,
    volumen_eur   NUMERIC(12,2) NOT NULL,
    gebuehr_eur   NUMERIC(10,2) NOT NULL,
    -- Berechnet und gespeichert: So laesst sich ueber Zeit vergleichen, ohne die
    -- Rechnung an jeder Auswertungsstelle zu wiederholen.
    quote         NUMERIC(6,4)  GENERATED ALWAYS AS
                  (CASE WHEN volumen_eur > 0 THEN gebuehr_eur / volumen_eur END) STORED,
    quelle        TEXT          NOT NULL,
    erfasst       TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Ein Auszug wird mehrfach eingelesen; dieselbe Order darf nicht doppelt
    -- zaehlen, sonst waechst die Gebuehrensumme mit jedem Einlesen.
    CONSTRAINT konto_order_einmalig UNIQUE (datum, volumen_eur, gebuehr_eur)
);

-- Was die Gebuehren seit Kontoeroeffnung gekostet haben. Die Zahl macht
-- sichtbar, was einzelne 1-EUR-Posten verbergen.
CREATE OR REPLACE VIEW firma.konto_gebuehren AS
SELECT count(*)                              AS orders,
       coalesce(sum(volumen_eur), 0)         AS volumen_eur,
       coalesce(sum(gebuehr_eur), 0)         AS gebuehren_eur,
       CASE WHEN sum(volumen_eur) > 0
            THEN sum(gebuehr_eur) / sum(volumen_eur) END AS quote_gesamt,
       max(quote)                            AS quote_schlechteste,
       min(datum)                            AS erste,
       max(datum)                            AS letzte
FROM firma.konto_order;

-- konto_lage um Krypto erweitern. Die Sicht wird ersetzt, nicht ergaenzt --
-- zwei Sichten auf dieselbe Lage waeren zwei Wahrheiten.
--
-- DROP statt CREATE OR REPLACE: Postgres laesst bei REPLACE nur ANHAENGEN zu,
-- keine neue Spalte in der Mitte ("cannot change name of view column"). Eine
-- Sicht enthaelt keine Daten, ihr Wegfall kostet also nichts -- anders als bei
-- einer Tabelle, wo DROP der falsche Weg waere.
DROP VIEW IF EXISTS firma.konto_lage;
CREATE VIEW firma.konto_lage AS
WITH stand AS (
    SELECT * FROM firma.konto_stand ORDER BY stichtag DESC LIMIT 1
), ziel AS (
    SELECT * FROM firma.konto_ziel ORDER BY gueltig_ab DESC LIMIT 1
), ab AS (
    SELECT coalesce(sum(betrag_eur), 0) AS monat FROM firma.konto_abfluss
)
SELECT
    s.stichtag,
    s.quelle,
    s.liquide_eur,
    s.angelegt_eur,
    s.krypto_eur,
    (s.liquide_eur + s.angelegt_eur)                        AS gesamt_eur,
    z.liquide_anteil                                        AS soll_anteil,
    z.toleranz,
    ab.monat                                                AS abfluss_monat_eur,
    (CURRENT_DATE - s.stichtag)                             AS alter_tage,
    ((CURRENT_DATE - s.stichtag) > z.veraltet_ab_tage)       AS veraltet,
    CASE WHEN (s.liquide_eur + s.angelegt_eur) > 0
         THEN s.liquide_eur / (s.liquide_eur + s.angelegt_eur) END
                                                            AS ist_anteil,
    -- Anteil des spekulativen Teils am GESAMTvermoegen, nicht nur am angelegten:
    -- Die Frage "wie viel steht im Risiko" bezieht sich auf alles, was da ist.
    CASE WHEN (s.liquide_eur + s.angelegt_eur) > 0
         THEN s.krypto_eur / (s.liquide_eur + s.angelegt_eur) END
                                                            AS krypto_anteil,
    (s.liquide_eur - (s.liquide_eur + s.angelegt_eur) * z.liquide_anteil)
                                                            AS umschichtung_eur,
    CASE WHEN ab.monat > 0 THEN s.liquide_eur / ab.monat END
                                                            AS reichweite_monate,
    z.mindest_gesamt_eur,
    z.max_gebuehrenquote,
    z.mindest_reichweite_mon,
    z.veraltet_ab_tage
FROM stand s CROSS JOIN ziel z CROSS JOIN ab;
