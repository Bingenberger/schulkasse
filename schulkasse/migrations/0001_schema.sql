-- Initial schema as specified in Lastenheft section 5 (binding).

CREATE TABLE benutzer (
    id                INTEGER PRIMARY KEY,
    benutzername      TEXT NOT NULL UNIQUE,
    anzeigename       TEXT NOT NULL,
    passwort_hash     TEXT NOT NULL,
    rolle             TEXT NOT NULL,          -- admin | kasse | lehrkraft
    aktiv             INTEGER NOT NULL DEFAULT 1,
    letzter_login     TIMESTAMP,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE konto (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL,
    iban              TEXT NOT NULL UNIQUE,
    bic               TEXT,
    inhaber           TEXT NOT NULL,
    aktiv             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE schuljahr (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL UNIQUE,
    beginn            DATE NOT NULL,
    ende              DATE NOT NULL,
    aktiv             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE klasse (
    id                INTEGER PRIMARY KEY,
    schuljahr_id      INTEGER NOT NULL REFERENCES schuljahr(id),
    bezeichnung       TEXT NOT NULL,
    UNIQUE (schuljahr_id, bezeichnung)
);

CREATE TABLE familie (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL
);

CREATE TABLE kind (
    id                INTEGER PRIMARY KEY,
    klasse_id         INTEGER NOT NULL REFERENCES klasse(id),
    vorname           TEXT NOT NULL,
    nachname          TEXT NOT NULL,
    familie_id        INTEGER REFERENCES familie(id),
    but_status        INTEGER NOT NULL DEFAULT 0,
    but_notiz         TEXT,
    anonymisiert      INTEGER NOT NULL DEFAULT 0,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE zahler_iban (
    id                INTEGER PRIMARY KEY,
    familie_id        INTEGER NOT NULL REFERENCES familie(id),
    iban              TEXT NOT NULL,
    zahler_name       TEXT,
    treffer_anzahl    INTEGER NOT NULL DEFAULT 1,
    zuletzt_gesehen   DATE,
    UNIQUE (familie_id, iban)
);

CREATE TABLE projekt (
    id                INTEGER PRIMARY KEY,
    schuljahr_id      INTEGER NOT NULL REFERENCES schuljahr(id),
    bezeichnung       TEXT NOT NULL,
    kurzcode          TEXT NOT NULL UNIQUE,
    typ               TEXT NOT NULL,          -- elternbeitrag | vorkasse | sammeltopf | sonstiges
    klasse_id         INTEGER REFERENCES klasse(id),
    verantwortlich_user_id INTEGER REFERENCES benutzer(id),
    beschreibung      TEXT,
    status            TEXT NOT NULL DEFAULT 'offen',  -- offen | abgeschlossen
    abgeschlossen_am  DATE,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE offener_posten (
    id                INTEGER PRIMARY KEY,
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    richtung          TEXT NOT NULL,          -- eingang | ausgang
    kategorie         TEXT NOT NULL,          -- elternbeitrag | erstattung | rechnung | sonstiges
    kind_id           INTEGER REFERENCES kind(id),
    bezeichnung       TEXT NOT NULL,
    sollbetrag_cent   INTEGER NOT NULL CHECK (sollbetrag_cent > 0),
    faellig_am        DATE,
    zahlcode          TEXT UNIQUE,
    status            TEXT NOT NULL DEFAULT 'offen',
                      -- offen | teilweise | erledigt | erlassen | storniert
    erlass_grund      TEXT,
    notiz             TEXT,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE import (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    dateiname         TEXT NOT NULL,
    datei_hash        TEXT NOT NULL,
    zeitraum_von      DATE,
    zeitraum_bis      DATE,
    anzahl_gesamt     INTEGER NOT NULL,
    anzahl_neu        INTEGER NOT NULL,
    anzahl_duplikat   INTEGER NOT NULL,
    saldo_schluss_cent INTEGER,
    saldo_datum       DATE,
    importiert_von    INTEGER NOT NULL REFERENCES benutzer(id),
    importiert_am     TIMESTAMP NOT NULL
);

CREATE TABLE bankumsatz (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    import_id         INTEGER NOT NULL REFERENCES import(id),
    buchungsdatum     DATE NOT NULL,
    valutadatum       DATE,
    betrag_cent       INTEGER NOT NULL,
    waehrung          TEXT NOT NULL DEFAULT 'EUR',
    status            TEXT NOT NULL,          -- BOOK | PDNG
    gegen_name        TEXT,
    gegen_iban        TEXT,
    verwendungszweck  TEXT,
    end_to_end_id     TEXT,
    bank_ref          TEXT,
    geschaeftsvorfall TEXT,
    dedup_hash        TEXT NOT NULL,
    zuordnungsstatus  TEXT NOT NULL DEFAULT 'offen',
                      -- offen | teilweise | zugeordnet | ignoriert
    erstellt_am       TIMESTAMP NOT NULL,
    UNIQUE (konto_id, dedup_hash)
);

CREATE TABLE zuordnung (
    id                INTEGER PRIMARY KEY,
    bankumsatz_id     INTEGER NOT NULL REFERENCES bankumsatz(id),
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    offener_posten_id INTEGER REFERENCES offener_posten(id),
    betrag_cent       INTEGER NOT NULL,
    quelle            TEXT NOT NULL,          -- auto | manuell | regel
    konfidenz         INTEGER,
    notiz             TEXT,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    storniert         INTEGER NOT NULL DEFAULT 0,
    storniert_von     INTEGER REFERENCES benutzer(id),
    storniert_am      TIMESTAMP,
    storno_grund      TEXT
);

CREATE TABLE beleg (
    id                INTEGER PRIMARY KEY,
    projekt_id        INTEGER REFERENCES projekt(id),
    zuordnung_id      INTEGER REFERENCES zuordnung(id),
    dateiname         TEXT NOT NULL,
    pfad              TEXT NOT NULL,
    mimetype          TEXT NOT NULL,
    groesse_bytes     INTEGER NOT NULL,
    hochgeladen_von   INTEGER NOT NULL REFERENCES benutzer(id),
    hochgeladen_am    TIMESTAMP NOT NULL
);

CREATE TABLE protokoll (
    id                INTEGER PRIMARY KEY,
    zeitpunkt         TIMESTAMP NOT NULL,
    benutzer_id       INTEGER REFERENCES benutzer(id),
    aktion            TEXT NOT NULL,
    entitaet          TEXT NOT NULL,
    entitaet_id       INTEGER,
    alt_wert          TEXT,
    neu_wert          TEXT,
    ip                TEXT
);

CREATE INDEX ix_kind_klasse ON kind(klasse_id);
CREATE INDEX ix_kind_familie ON kind(familie_id);
CREATE INDEX ix_zahler_iban_iban ON zahler_iban(iban);
CREATE INDEX ix_projekt_schuljahr ON projekt(schuljahr_id);
CREATE INDEX ix_posten_projekt ON offener_posten(projekt_id);
CREATE INDEX ix_posten_kind ON offener_posten(kind_id);
CREATE INDEX ix_umsatz_datum ON bankumsatz(buchungsdatum);
CREATE INDEX ix_umsatz_status ON bankumsatz(zuordnungsstatus, status);
CREATE INDEX ix_zuordnung_umsatz ON zuordnung(bankumsatz_id);
CREATE INDEX ix_zuordnung_projekt ON zuordnung(projekt_id);
CREATE INDEX ix_zuordnung_posten ON zuordnung(offener_posten_id);
CREATE INDEX ix_protokoll_zeit ON protokoll(zeitpunkt);
