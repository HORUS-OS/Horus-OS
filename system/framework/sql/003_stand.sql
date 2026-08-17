-- 003_stand.sql — Git-Verteilung an die Knoten (Schritt 5 wandernde-werkbank).
--
-- Der Plan nennt den Schritt "Matrix-Trigger", mit der Begruendung: den Kanal
-- nutzen, der ohnehin steht, kein neuer Port. Das gilt hier weiter -- nur ist
-- der Kanal, der WIRKLICH an jedem Knoten schon offen steht, die
-- Postgres-Verbindung und nicht Matrix.
--
-- Matrix bleibt der AUSLOESER (Chef sagt es im Raum, ein Agent ruft das
-- Werkzeug `stand_holen`). Der Transport zu den Knoten laeuft ueber NOTIFY:
--
--   * kein neuer Port -- dieselbe Verbindung, die vorgaenge.py schon nutzt
--   * kein Timer -- der Knoten haengt blockierend im LISTEN und wacht genau
--     dann auf, wenn etwas ansteht. Genau die Vorgabe aus Runde 03:
--     "ereignisgesteuert, wenn es ein Update gibt. Kein Timer."
--   * kein Matrix-Client auf jedem Knoten, kein Nachrichten-Parsing, kein
--     Raum, in dem eine verlorene Nachricht ein verpasstes Update bedeutet
--
-- Der Haupt-PC ist ausserdem SSH-Client, kein Server -- von aussen anklopfen
-- geht dort ohnehin nicht. Der Knoten muss sich melden, nicht umgekehrt.
--
-- Idempotent.

CREATE SCHEMA IF NOT EXISTS firma;

-- Welcher Knoten steht auf welchem Commit? Beantwortet die Frage, die vorher
-- niemand beantworten konnte: laeuft ueberall dasselbe?
CREATE TABLE IF NOT EXISTS firma.stand (
    knoten        TEXT PRIMARY KEY,
    commit_id     TEXT,                        -- worauf der Knoten steht
    zweig         TEXT,
    gemeldet      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Ergebnis des letzten Abgleichs. Auch der Fehlschlag gehoert hierher:
    -- ein Knoten, der stumm zurueckbleibt, ist gefaehrlicher als einer, der
    -- laut scheitert.
    letzter_lauf  TEXT,
    erfolg        BOOLEAN
);

-- Auftraege zum Abgleich. Eine Zeile je Aufforderung, damit im Nachhinein
-- nachvollziehbar bleibt, wer wann was ausgeloest hat -- ein NOTIFY allein
-- hinterlaesst keine Spur.
CREATE TABLE IF NOT EXISTS firma.stand_auftrag (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ziel          TEXT        NOT NULL DEFAULT 'alle',   -- Knotenname oder 'alle'
    ausgeloest_von TEXT       NOT NULL,                  -- agent_id oder 'chef'
    grund         TEXT,
    erstellt      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stand_auftrag_zeit_idx
    ON firma.stand_auftrag (erstellt DESC);

-- Der Weckruf. Ein Trigger statt eines NOTIFY im Anwendungscode: So kann die
-- Benachrichtigung nicht vergessen werden, egal wer einfuegt -- und sie kommt
-- erst, wenn die Transaktion durch ist. Ein Knoten, der sofort reagiert, findet
-- die Zeile dann auch wirklich vor.
CREATE OR REPLACE FUNCTION firma.stand_wecken() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('firma_stand',
                      json_build_object('id', NEW.id, 'ziel', NEW.ziel,
                                        'von', NEW.ausgeloest_von)::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS stand_auftrag_notify ON firma.stand_auftrag;
CREATE TRIGGER stand_auftrag_notify
    AFTER INSERT ON firma.stand_auftrag
    FOR EACH ROW EXECUTE FUNCTION firma.stand_wecken();

-- Uebersicht: steht ueberall dasselbe? Die Sicht vergleicht gegen den Knoten
-- mit der juengsten Meldung, nicht gegen einen fest verdrahteten Namen.
CREATE OR REPLACE VIEW firma.stand_uebersicht AS
SELECT s.knoten, s.commit_id, s.zweig, s.gemeldet, s.erfolg, s.letzter_lauf,
       s.commit_id = (SELECT commit_id FROM firma.stand
                      ORDER BY gemeldet DESC LIMIT 1) AS aktuell
FROM firma.stand s
ORDER BY s.knoten;
