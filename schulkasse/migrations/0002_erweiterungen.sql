-- Additions required by functional requirements that the binding schema
-- does not cover. Existing columns are left untouched.

-- F9.2: pupils leaving after year 4 become inactive.
ALTER TABLE kind ADD COLUMN aktiv INTEGER NOT NULL DEFAULT 1;

-- F7.4: date of the last letter per item.
ALTER TABLE offener_posten ADD COLUMN letzter_brief_am TIMESTAMP;

-- F7.2: editable letter text, stored per project for reuse.
ALTER TABLE projekt ADD COLUMN brieftext TEXT;

-- F6: account whose IBAN is printed in the QR code (school vs. Förderverein).
ALTER TABLE projekt ADD COLUMN konto_id INTEGER REFERENCES konto(id);

-- F1.4: raw remittance text is kept in addition to the extracted SVWZ part.
ALTER TABLE bankumsatz ADD COLUMN verwendungszweck_roh TEXT;

-- F8.3: balance before the first imported transaction. Without it the
-- control calculation can only be zero for accounts opened at 0,00 EUR.
ALTER TABLE konto ADD COLUMN eroeffnungssaldo_cent INTEGER;
ALTER TABLE konto ADD COLUMN eroeffnungssaldo_datum DATE;

-- F5.5: reason for ignoring a transaction (bankumsatz itself stays immutable).
CREATE TABLE umsatz_ignoriert (
    id                INTEGER PRIMARY KEY,
    bankumsatz_id     INTEGER NOT NULL REFERENCES bankumsatz(id),
    grund             TEXT NOT NULL,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    aufgehoben        INTEGER NOT NULL DEFAULT 0,
    aufgehoben_von    INTEGER REFERENCES benutzer(id),
    aufgehoben_am     TIMESTAMP
);
CREATE INDEX ix_ignoriert_umsatz ON umsatz_ignoriert(bankumsatz_id);

-- F4: matching suggestions awaiting confirmation. One transaction can have
-- several alternative suggestions, each consisting of one or more parts.
CREATE TABLE vorschlag (
    id                INTEGER PRIMARY KEY,
    bankumsatz_id     INTEGER NOT NULL REFERENCES bankumsatz(id) ON DELETE CASCADE,
    regel             TEXT NOT NULL,
    konfidenz         INTEGER NOT NULL,
    hinweis           TEXT,
    erstellt_am       TIMESTAMP NOT NULL
);
CREATE INDEX ix_vorschlag_umsatz ON vorschlag(bankumsatz_id);

CREATE TABLE vorschlag_teil (
    id                INTEGER PRIMARY KEY,
    vorschlag_id      INTEGER NOT NULL REFERENCES vorschlag(id) ON DELETE CASCADE,
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    offener_posten_id INTEGER REFERENCES offener_posten(id),
    betrag_cent       INTEGER NOT NULL
);
CREATE INDEX ix_vorschlag_teil_vorschlag ON vorschlag_teil(vorschlag_id);
CREATE INDEX ix_vorschlag_teil_posten ON vorschlag_teil(offener_posten_id);

-- F1.9: uploaded file kept between preview and confirmation (never unpacked to disk).
CREATE TABLE import_vorschau (
    token             TEXT PRIMARY KEY,
    benutzer_id       INTEGER NOT NULL REFERENCES benutzer(id),
    art               TEXT NOT NULL,          -- camt | csv
    dateiname         TEXT NOT NULL,
    daten             BLOB NOT NULL,
    erstellt_am       TIMESTAMP NOT NULL
);

-- F10.2: server-side sessions.
CREATE TABLE sitzung (
    id                TEXT PRIMARY KEY,
    daten             TEXT NOT NULL,
    zuletzt_aktiv     TIMESTAMP NOT NULL
);

-- F10.3: login attempts for lockout.
CREATE TABLE login_versuch (
    id                INTEGER PRIMARY KEY,
    zeitpunkt         TIMESTAMP NOT NULL,
    benutzername      TEXT NOT NULL,
    ip                TEXT NOT NULL,
    erfolgreich       INTEGER NOT NULL
);
CREATE INDEX ix_login_versuch_ip ON login_versuch(ip, zeitpunkt);
CREATE INDEX ix_login_versuch_name ON login_versuch(benutzername, zeitpunkt);
