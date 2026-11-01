-- Start of bookkeeping per account (Stichtag). Transactions before it stay
-- stored and searchable but are neither assigned nor part of the control
-- calculation; the balance at that date is the opening balance.
ALTER TABLE konto ADD COLUMN beginn_datum DATE;

-- Distribution of the opening balance to projects ("Töpfe") and, optionally,
-- to items that were already paid before the Stichtag. Like assignments,
-- entries are never deleted, only cancelled.
CREATE TABLE anfangsbestand (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    offener_posten_id INTEGER REFERENCES offener_posten(id),
    betrag_cent       INTEGER NOT NULL CHECK (betrag_cent <> 0),
    notiz             TEXT,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    storniert         INTEGER NOT NULL DEFAULT 0,
    storniert_von     INTEGER REFERENCES benutzer(id),
    storniert_am      TIMESTAMP,
    storno_grund      TEXT
);
CREATE INDEX ix_anfangsbestand_konto ON anfangsbestand(konto_id);
CREATE INDEX ix_anfangsbestand_projekt ON anfangsbestand(projekt_id);
CREATE INDEX ix_anfangsbestand_posten ON anfangsbestand(offener_posten_id);

-- All money that counts for projects and items: non-cancelled assignments of
-- bank transactions plus non-cancelled shares of the opening balance.
-- (storniert is always 0; kept so queries can filter uniformly.)
CREATE VIEW zahlung AS
    SELECT id, 'umsatz' AS art, projekt_id, offener_posten_id, betrag_cent, 0 AS storniert
      FROM zuordnung WHERE storniert = 0
    UNION ALL
    SELECT id, 'anfangsbestand' AS art, projekt_id, offener_posten_id, betrag_cent, 0 AS storniert
      FROM anfangsbestand WHERE storniert = 0;
