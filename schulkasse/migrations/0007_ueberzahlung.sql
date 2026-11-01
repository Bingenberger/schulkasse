-- F3.6: an overpayment can be marked as settled (money is not touched).
-- The settled amount is kept: if more money arrives later, the item shows up
-- in the list of overpayments again.
ALTER TABLE offener_posten ADD COLUMN ueberzahlung_geklaert_cent INTEGER;
ALTER TABLE offener_posten ADD COLUMN ueberzahlung_grund TEXT;
ALTER TABLE offener_posten ADD COLUMN ueberzahlung_geklaert_von INTEGER REFERENCES benutzer(id);
ALTER TABLE offener_posten ADD COLUMN ueberzahlung_geklaert_am TIMESTAMP;
