-- Settings edited in the web interface (letter template). Values here take
-- precedence over instance/brief.toml; a missing key means "use the file/default".
CREATE TABLE einstellung (
    schluessel        TEXT PRIMARY KEY,
    wert              TEXT NOT NULL,
    geaendert_von     INTEGER REFERENCES benutzer(id),
    geaendert_am      TIMESTAMP NOT NULL
);
