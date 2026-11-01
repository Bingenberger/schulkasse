ALTER TABLE benutzer ADD COLUMN totp_secret TEXT;
ALTER TABLE benutzer ADD COLUMN totp_last INTEGER NOT NULL DEFAULT -1;
ALTER TABLE benutzer ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE recovery_code (
    benutzer_id INTEGER NOT NULL REFERENCES benutzer(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    PRIMARY KEY (benutzer_id, code_hash)
);
CREATE TABLE mfa_versuch (
    benutzer_id INTEGER NOT NULL REFERENCES benutzer(id) ON DELETE CASCADE,
    ip TEXT NOT NULL,
    zeitpunkt INTEGER NOT NULL
);
CREATE INDEX ix_mfa_user ON mfa_versuch(benutzer_id, zeitpunkt);
CREATE INDEX ix_mfa_ip ON mfa_versuch(ip, zeitpunkt);
-- Existing password-only sessions must sign in again.
DELETE FROM sitzung;
