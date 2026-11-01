-- A transfer between projects does not move money, but the amount still sits on
-- one of the bank accounts. Without the account the per-account view of a
-- project's balance would not add up.
ALTER TABLE umbuchung ADD COLUMN konto_id INTEGER REFERENCES konto(id);
-- With a single account there is nothing to choose.
UPDATE umbuchung SET konto_id = (SELECT id FROM konto) WHERE konto_id IS NULL AND (SELECT COUNT(*) FROM konto) = 1;
