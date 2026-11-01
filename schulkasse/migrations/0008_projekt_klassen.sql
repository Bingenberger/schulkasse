-- F2.1: a project can concern several classes (e.g. a whole grade).
CREATE TABLE projekt_klasse (
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    klasse_id         INTEGER NOT NULL REFERENCES klasse(id),
    PRIMARY KEY (projekt_id, klasse_id)
);
INSERT INTO projekt_klasse (projekt_id, klasse_id) SELECT id, klasse_id FROM projekt WHERE klasse_id IS NOT NULL;
-- projekt.klasse_id is no longer used.
UPDATE projekt SET klasse_id = NULL;
