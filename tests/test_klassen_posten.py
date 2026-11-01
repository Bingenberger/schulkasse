"""Projects with several classes (F2.1) and items created from a transaction (F5.4)."""
import pytest

from schulkasse.buchung import BuchungsFehler
from schulkasse.projekte import jahrgang, posten_aus_umsatz, projekt_aendern, projekt_anlegen, projekt_klassen_ids
from schulkasse.stammdaten import klasse_holen_oder_anlegen

from .conftest import importiere_datei, login


def _klassen(db, admin_id):
    sj = db.scalar("SELECT id FROM schuljahr WHERE aktiv=1")
    ids = {name: klasse_holen_oder_anlegen(db, sj, name, admin_id) for name in ("4a", "4b", "4c", "2b")}
    return sj, ids


def test_jahrgang():
    assert jahrgang("4a") == "4" and jahrgang("10 b") == "10" and jahrgang("Vorklasse") is None


def test_projekt_mit_mehreren_klassen(demo, admin_id):
    sj, k = _klassen(demo, admin_id)
    pid = projekt_anlegen(demo, {"schuljahr_id": sj, "bezeichnung": "Zoo Jahrgang 4", "kurzcode": "ZOO4",
                                 "typ": "elternbeitrag", "klassen_ids": [k["4a"], k["4b"], k["4c"]]}, admin_id)
    assert sorted(projekt_klassen_ids(demo, pid)) == sorted([k["4a"], k["4b"], k["4c"]])
    projekt_aendern(demo, pid, {"schuljahr_id": sj, "bezeichnung": "Zoo", "kurzcode": "ZOO4",
                                "typ": "elternbeitrag", "klassen_ids": [k["2b"]]}, admin_id)
    assert projekt_klassen_ids(demo, pid) == [k["2b"]]


def test_klasse_aus_anderem_schuljahr(demo, admin_id):
    anderes = demo.insert("INSERT INTO schuljahr (bezeichnung, beginn, ende) VALUES ('2030/31','2030-08-01','2031-07-31')")
    fremd = klasse_holen_oder_anlegen(demo, anderes, "4a", admin_id)
    sj = demo.scalar("SELECT id FROM schuljahr WHERE aktiv=1")
    with pytest.raises(BuchungsFehler, match="anderen Schuljahr"):
        projekt_anlegen(demo, {"schuljahr_id": sj, "bezeichnung": "X", "kurzcode": "XX1", "typ": "sonstiges",
                               "klassen_ids": [fremd]}, admin_id)


def test_klassen_ueber_die_oberflaeche(app, demo, admin_id):
    sj, k = _klassen(demo, admin_id)
    c = app.test_client()
    login(c)
    formular = c.get("/projekte/neu").get_data(as_text=True)
    assert 'data-jahrgang="4"' in formular and 'name="klasse_ids"' in formular
    r = c.post("/projekte/neu", follow_redirects=True, data={
        "bezeichnung": "Theater Jg. 4", "kurzcode": "THJ4", "typ": "elternbeitrag", "schuljahr_id": sj,
        "klasse_ids": [k["4a"], k["4b"]]})
    text = r.get_data(as_text=True)
    assert "Klassen 4a, 4b" in text
    pid = demo.scalar("SELECT id FROM projekt WHERE kurzcode='THJ4'")
    assert "4a, 4b" in c.get("/projekte").get_data(as_text=True)
    bearbeiten = c.get(f"/projekte/{pid}/bearbeiten").get_data(as_text=True)
    assert bearbeiten.count("checked") >= 2
    # the mass creation preselects all classes of the project
    mass = c.get(f"/projekte/{pid}/massenanlage").get_data(as_text=True)
    assert f'value="{k["4a"]}" checked' in mass and f'value="{k["4b"]}" checked' in mass


def _abbuchung(db):
    return db.one("SELECT * FROM bankumsatz WHERE betrag_cent<0 AND status='BOOK' AND zuordnungsstatus='offen'"
                  " ORDER BY id")


def test_posten_aus_abbuchung_mit_rueckzahlung(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = _abbuchung(demo)
    assert u is not None
    betrag = abs(u["betrag_cent"])
    projekt = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    pid, gid = posten_aus_umsatz(demo, u["id"], projekt_id=projekt, bezeichnung="Anzahlung Herberge",
                                 kategorie="erstattung", betrag_cent=betrag, benutzer_id=admin_id, gegenposten=True)
    p = demo.one("SELECT * FROM offener_posten WHERE id=?", (pid,))
    assert p["richtung"] == "ausgang" and p["status"] == "erledigt" and p["zahlcode"] is None
    g = demo.one("SELECT * FROM offener_posten WHERE id=?", (gid,))
    assert g["richtung"] == "eingang" and g["status"] == "offen" and g["sollbetrag_cent"] == betrag
    assert g["bezeichnung"] == "Rückzahlung: Anzahlung Herberge"
    assert demo.one("SELECT zuordnungsstatus FROM bankumsatz WHERE id=?", (u["id"],))[0] == "zugeordnet"


def test_posten_aus_umsatz_rollt_zurueck(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = _abbuchung(demo)
    projekt = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    vorher = demo.scalar("SELECT COUNT(*) FROM offener_posten")
    with pytest.raises(BuchungsFehler, match="übersteigen"):
        posten_aus_umsatz(demo, u["id"], projekt_id=projekt, bezeichnung="Zu viel", kategorie="rechnung",
                          betrag_cent=abs(u["betrag_cent"]) + 1, benutzer_id=admin_id, gegenposten=True)
    assert demo.scalar("SELECT COUNT(*) FROM offener_posten") == vorher
    with pytest.raises(BuchungsFehler, match="Elternbeiträge"):
        posten_aus_umsatz(demo, u["id"], projekt_id=projekt, bezeichnung="X", kategorie="elternbeitrag",
                          betrag_cent=100, benutzer_id=admin_id)


def test_posten_aus_umsatz_ueber_die_oberflaeche(app, demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = _abbuchung(demo)
    projekt = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    c = app.test_client()
    login(c)
    seite = c.get(f"/umsaetze/{u['id']}").get_data(as_text=True)
    assert "Neuen Posten anlegen und zuordnen" in seite and "Das Geld kommt zurück" in seite
    r = c.post(f"/umsaetze/{u['id']}/posten", follow_redirects=True, data={
        "projekt_id": projekt, "kategorie": "erstattung", "bezeichnung": "Auslage Bastelmaterial",
        "betrag": f"{abs(u['betrag_cent']) / 100:.2f}".replace(".", ","), "gegenposten": "1"})
    assert "Gegenposten wartet" in r.get_data(as_text=True)
    assert demo.scalar("SELECT COUNT(*) FROM offener_posten WHERE bezeichnung='Rückzahlung: Auslage Bastelmaterial'"
                       " AND richtung='eingang' AND status='offen'") == 1


def test_beitraege_sammelaenderung(demo, admin_id, dateien):
    from schulkasse.auswertung import anzahl_ueberzahlungen
    from schulkasse.projekte import beitraege_aendern, beitragshoehen
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    codes = {r[0] for r in demo.all("SELECT zahlcode FROM offener_posten WHERE projekt_id=?", (kf,))}
    assert [(b["sollbetrag_cent"], b["anzahl"]) for b in beitragshoehen(demo, kf)] == [(4500, 25)]
    erg = beitraege_aendern(demo, kf, 4500, 5000, admin_id)
    assert erg["geaendert"] == 25 and erg["bezahlt"] == 3 and erg["ueberzahlt"] == 0
    assert demo.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND status='teilweise'", (kf,)) == 3
    assert {r[0] for r in demo.all("SELECT zahlcode FROM offener_posten WHERE projekt_id=?", (kf,))} == codes
    erg = beitraege_aendern(demo, kf, 5000, 4000, admin_id)
    assert erg["ueberzahlt"] == 3 and anzahl_ueberzahlungen(demo) >= 3
    with pytest.raises(BuchungsFehler, match="keine Elternbeiträge"):
        beitraege_aendern(demo, kf, 4500, 4000, admin_id)
    assert demo.scalar("SELECT COUNT(*) FROM protokoll WHERE aktion='projekt.beitraege_aendern'") == 2


def test_beitraege_aendern_ueber_die_oberflaeche(app, demo, admin_id):
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    c = app.test_client()
    login(c)
    assert "Betrag der Elternbeiträge für alle ändern" in c.get(f"/projekte/{kf}").get_data(as_text=True)
    r = c.post(f"/projekte/{kf}/beitraege-aendern", data={"alt_cent": "4500", "betrag": "38,50"},
               follow_redirects=True)
    assert "25 Elternbeiträge geändert" in r.get_data(as_text=True)
    assert demo.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND sollbetrag_cent=3850", (kf,)) == 25
