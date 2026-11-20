-- Richtlinie Schulgirokonten (Stadt Niederkassel), § 8: for every bank
-- statement of every account a separate monitoring list ("Überwachungsliste")
-- has to be kept. The CAMT.052 files of the bank carry no statement numbers,
-- so statements are recorded by hand: number, period and the closing balance
-- printed on the statement. Transactions belong to a statement by booking date.

-- Name of the bank, printed in the header of the monitoring list.
ALTER TABLE konto ADD COLUMN geldinstitut TEXT;

CREATE TABLE kontoauszug (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    jahr              INTEGER NOT NULL,
    nummer            INTEGER NOT NULL CHECK (nummer > 0),
    datum_von         DATE NOT NULL,
    datum_bis         DATE NOT NULL,
    endsaldo_cent     INTEGER,               -- closing balance as printed on the statement
    notiz             TEXT,
    geprueft_von      INTEGER REFERENCES benutzer(id),   -- § 8 (1): checked and signed
    geprueft_am       TIMESTAMP,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    UNIQUE (konto_id, jahr, nummer),
    CHECK (datum_von <= datum_bis)
);
CREATE INDEX ix_kontoauszug_konto ON kontoauszug(konto_id, datum_von);

-- Remark per transaction for the column "ggf. Anmerkungen" (e.g. approval of
-- a card payment, § 4 (4)). bankumsatz itself stays immutable.
CREATE TABLE umsatz_anmerkung (
    bankumsatz_id     INTEGER PRIMARY KEY REFERENCES bankumsatz(id),
    text              TEXT NOT NULL,
    geaendert_von     INTEGER NOT NULL REFERENCES benutzer(id),
    geaendert_am      TIMESTAMP NOT NULL
);

-- Fingerprint of the list at the time of the check: later changes (new
-- assignments, remarks) are shown as "changed since the check".
ALTER TABLE kontoauszug ADD COLUMN geprueft_hash TEXT;
