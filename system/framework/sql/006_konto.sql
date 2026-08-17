-- 006_konto.sql — das Geschaeftskonto als Daten.
--
-- Chef bringt sein Trade-Republic-Konto als Geschaeftskonto ein, mit der
-- Vorgabe: rund ein Drittel liquide, zwei Drittel langfristig angelegt. Die
-- Abos sollen kuenftig von diesem Konto abgehen.
--
-- WAS HIER BEWUSST NICHT STEHT
-- Keine IBAN, keine Depotnummer, keine Adresse, keine Einzelpositionen. Fuer
-- die Drittelregel genuegen ZWEI SUMMEN und ein Stichtag. Je weniger
-- Kontodaten hier liegen, desto weniger ist zu schuetzen -- und desto weniger
-- kann eine kuenftige Auskunft versehentlich preisgeben.
--
-- WARUM BUCHHALTER NICHT HANDELT
-- Trade Republic hat keine offizielle Schnittstelle fuer Privatkunden. Die
-- nachgebauten Pakete verlangen Zugangsdaten samt 2FA-PIN im Klartext und
-- verstossen gegen die Nutzungsbedingungen. Unabhaengig davon gilt der
-- Grundsatz, der in buchhalter/personalakte.json seit jeher steht:
--     "immer_zu_chef": ["Geld und Ausgaben", ...]
-- Die Arbeitsteilung lautet deshalb: Chef exportiert, Buchhalter rechnet und
-- legt vor, Chef fuehrt aus. Buchhalter nimmt das Nachhalten ab, nicht das
-- Klicken.
--
-- WARUM DIE ABFLUESSE EINE ABGELEITETE TABELLE SIND UND KEINE GEPFLEGTE
-- Die Fixkosten stehen bereits in buchhalter/broker/buchhalter.config.json und
-- steuern dort den Tagesdeckel. Wuerden sie hier ein zweites Mal gepflegt,
-- waere das genau der Fehler vom 16.08.: Der Tagesdeckel wurde an zwei Stellen
-- unabhaengig gelesen, eine Aenderung blieb wirkungslos, und der Status meldete
-- weiter den alten Wert. konto.py schreibt diese Tabelle deshalb AUS der
-- Konfiguration; von Hand wird hier nichts eingetragen.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

CREATE TABLE IF NOT EXISTS firma.konto_stand (
    -- Ein Datensatz je Stichtag. Zwei Staende fuer denselben Tag waeren zwei
    -- Wahrheiten ueber dieselbe Sache; der spaetere gewinnt (Upsert).
    stichtag        DATE        PRIMARY KEY,
    liquide_eur     NUMERIC(12,2) NOT NULL,
    angelegt_eur    NUMERIC(12,2) NOT NULL,
    -- Woher die Zahlen stammen: Dateiname des Exports oder 'von Hand'. Ohne
    -- Herkunft laesst sich ein Tippfehler spaeter nicht mehr aufklaeren.
    quelle          TEXT        NOT NULL,
    erfasst         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT konto_betraege_nicht_negativ
        CHECK (liquide_eur >= 0 AND angelegt_eur >= 0)
);

-- Die Vorgabe als Daten, nicht als Zahl im Code. Eine geaenderte Vorgabe ist
-- damit nachvollziehbar statt stillschweigend -- und es bleibt sichtbar, ab
-- wann nach welcher Regel gerechnet wurde.
CREATE TABLE IF NOT EXISTS firma.konto_ziel (
    gueltig_ab      DATE        PRIMARY KEY,
    liquide_anteil  NUMERIC(5,4) NOT NULL,      -- 0.3333 = "rund ein Drittel"
    toleranz        NUMERIC(5,4) NOT NULL,      -- Abweichung, ab der ein Vorschlag kommt
    -- Unterhalb dieses Gesamtvermoegens ruht die Regel: Bei 20 EUR kostet jede
    -- Umschichtung 1 EUR, also 5 % des Ganzen. Eine Empfehlung, die mehr kostet
    -- als sie ausgleicht, ist keine.
    mindest_gesamt_eur      NUMERIC(12,2) NOT NULL DEFAULT 300.00,
    -- Hoechste vertretbare Gebuehrenquote je Order. Bei 1 EUR Fixgebuehr
    -- bedeutet 1 % ein Mindestvolumen von 100 EUR.
    max_gebuehrenquote      NUMERIC(5,4) NOT NULL DEFAULT 0.0100,
    -- Wie viele Monate Fixkosten der liquide Teil mindestens tragen muss.
    mindest_reichweite_mon  NUMERIC(5,1) NOT NULL DEFAULT 6.0,
    -- Ab wann ein Stand als veraltet gilt. Ein alter Stand, der wie ein
    -- aktueller aussieht, ist schlimmer als gar keiner.
    veraltet_ab_tage        INTEGER      NOT NULL DEFAULT 45,
    bemerkung       TEXT,

    CONSTRAINT konto_anteil_gueltig CHECK (liquide_anteil > 0 AND liquide_anteil < 1),
    CONSTRAINT konto_toleranz_gueltig CHECK (toleranz >= 0 AND toleranz < 1)
);

-- Die monatlichen Abfluesse. ABGELEITET aus buchhalter.config.json (siehe Kopf) --
-- geschrieben allein von konto.py, nie von Hand.
CREATE TABLE IF NOT EXISTS firma.konto_abfluss (
    posten          TEXT        PRIMARY KEY,
    betrag_eur      NUMERIC(10,2) NOT NULL,
    quelle          TEXT        NOT NULL DEFAULT 'buchhalter.config.json:fixkosten_eur_monat',
    abgeglichen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Die Lage in einer Zeile: juengster Stand, geltendes Ziel, Abfluesse.
-- Bewusst eine Sicht und keine Tabelle -- eine gespeicherte Auswertung waere
-- nach dem naechsten Stichtag falsch, ohne dass jemand es merkt.
CREATE OR REPLACE VIEW firma.konto_lage AS
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
    (s.liquide_eur + s.angelegt_eur)                        AS gesamt_eur,
    z.liquide_anteil                                        AS soll_anteil,
    z.toleranz,
    ab.monat                                                AS abfluss_monat_eur,
    (CURRENT_DATE - s.stichtag)                             AS alter_tage,
    ((CURRENT_DATE - s.stichtag) > z.veraltet_ab_tage)      AS veraltet,
    CASE WHEN (s.liquide_eur + s.angelegt_eur) > 0
         THEN s.liquide_eur / (s.liquide_eur + s.angelegt_eur) END
                                                            AS ist_anteil,
    -- Positiv = zu viel liquide (in den angelegten Teil umschichten),
    -- negativ = zu wenig liquide.
    (s.liquide_eur - (s.liquide_eur + s.angelegt_eur) * z.liquide_anteil)
                                                            AS umschichtung_eur,
    CASE WHEN ab.monat > 0 THEN s.liquide_eur / ab.monat END
                                                            AS reichweite_monate,
    z.mindest_gesamt_eur,
    z.max_gebuehrenquote,
    z.mindest_reichweite_mon,
    z.veraltet_ab_tage
FROM stand s CROSS JOIN ziel z CROSS JOIN ab;

-- Die Vorgabe von Chef, sofern noch keine hinterlegt ist. Die Schwellenwerte
-- sind EMPFEHLUNGEN aus dem Plan und noch nicht von Chef bestaetigt (K1, K2,
-- K4, K6, K7) -- die Spalte bemerkung sagt das, damit sie niemand fuer
-- entschieden haelt.
INSERT INTO firma.konto_ziel (gueltig_ab, liquide_anteil, toleranz, bemerkung)
SELECT DATE '2026-08-16', 0.3333, 0.0500,
       'Anteil von Chef (rund ein Drittel liquide). Schwellenwerte vorlaeufig: '
       'Empfehlungen K1/K2/K4/K6/K7 aus dem Plan, noch nicht bestaetigt.'
WHERE NOT EXISTS (SELECT 1 FROM firma.konto_ziel);
