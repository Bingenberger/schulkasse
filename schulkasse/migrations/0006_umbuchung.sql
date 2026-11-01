-- Transfer between projects ("Umbuchung"): no money moves on the bank account,
-- an amount only changes the pot it belongs to. Typical case: the remaining
-- balance of a finished project goes to another project.
CREATE TABLE umbuchung (
    id                INTEGER PRIMARY KEY,
    von_projekt_id    INTEGER NOT NULL REFERENCES projekt(id),
    nach_projekt_id   INTEGER NOT NULL REFERENCES projekt(id),
    offener_posten_id INTEGER REFERENCES offener_posten(id),   -- optional: item of the receiving project
    betrag_cent       INTEGER NOT NULL CHECK (betrag_cent > 0),
    grund             TEXT NOT NULL,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    storniert         INTEGER NOT NULL DEFAULT 0,
    storniert_von     INTEGER REFERENCES benutzer(id),
    storniert_am      TIMESTAMP,
    storno_grund      TEXT,
    CHECK (von_projekt_id <> nach_projekt_id)
);
CREATE INDEX ix_umbuchung_von ON umbuchung(von_projekt_id);
CREATE INDEX ix_umbuchung_nach ON umbuchung(nach_projekt_id);
CREATE INDEX ix_umbuchung_posten ON umbuchung(offener_posten_id);

-- Everything that counts for projects and items. A transfer appears twice:
-- negative for the giving project, positive for the receiving one, so the sum
-- over all projects (and therefore the control calculation) stays unchanged.
DROP VIEW zahlung;
CREATE VIEW zahlung AS
    SELECT id, 'umsatz' AS art, projekt_id, offener_posten_id, betrag_cent, 0 AS storniert
      FROM zuordnung WHERE storniert = 0
    UNION ALL
    SELECT id, 'anfangsbestand' AS art, projekt_id, offener_posten_id, betrag_cent, 0 AS storniert
      FROM anfangsbestand WHERE storniert = 0
    UNION ALL
    SELECT id, 'umbuchung_ab' AS art, von_projekt_id, NULL, -betrag_cent, 0 AS storniert
      FROM umbuchung WHERE storniert = 0
    UNION ALL
    SELECT id, 'umbuchung_zu' AS art, nach_projekt_id, offener_posten_id, betrag_cent, 0 AS storniert
      FROM umbuchung WHERE storniert = 0;
