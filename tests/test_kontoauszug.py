"""Bank statements, monitoring lists (Richtlinie § 8), class lists and reminder slips."""
import io

import openpyxl
import pytest

from schulkasse import kontoauszug as ka
from schulkasse.buchung import BuchungsFehler
from schulkasse.klassenliste import klassen_zahlungsstatus
from schulkasse.testdaten import importiere_testdateien

from .conftest import benutzer_anlegen, login


@pytest.fixture
def daten(demo, admin_id):
    importiere_testdateien(demo, admin_id)
    return demo


def _auszug(db, admin_id, nummer, von, bis, endsaldo=None):
    return ka.speichern(db, 1, {"nummer": nummer, "datum_von": von, "datum_bis": bis, "endsaldo_cent": endsaldo},
                        admin_id)


def test_ueberwachungsliste_salden_und_abgleich(daten, admin_id):
    a1 = _auszug(daten, admin_id, 1, "2026-09-01", "2026-09-07", 160600)
    a2 = _auszug(daten, admin_id, 2, "2026-09-08", "2026-09-30", 172950)
    ul1 = ka.ueberwachungsliste(daten, a1)
    assert ul1["anfangssaldo"] == 125000 and ul1["endsaldo"] == 160600
    assert [z["nr"] for z in ul1["zeilen"]] == list(range(1, 14))
    assert ul1["zeilen"][-1]["stand"] == 160600
    assert ul1["eingaenge"] - ul1["ausgaenge"] == 160600 - 125000
    assert not [h for h in ul1["hinweise"] if h[0] == "fehler"]
    ul2 = ka.ueberwachungsliste(daten, a2)
    assert ul2["anfangssaldo"] == ul1["endsaldo"] and ul2["endsaldo"] == 172950
    # assigned payment: reason is the project, class comes from the child
    zugeordnet = next(z for z in ul1["zeilen"] if z["umsatz"]["zuordnungsstatus"] == "zugeordnet")
    assert "(" in zugeordnet["grund"] and zugeordnet["klasse"]
    assert ka.ohne_auszug(daten, 1)["anzahl"] == 0


def test_abweichung_luecke_und_ueberschneidung(daten, admin_id):
    a1 = _auszug(daten, admin_id, 1, "2026-09-01", "2026-09-04", 100)
    hinweise = ka.ueberwachungsliste(daten, a1)["hinweise"]
    assert any(s == "fehler" and "weicht" in t for s, t in hinweise)
    a2 = _auszug(daten, admin_id, 2, "2026-09-06", "2026-09-30", 172950)
    hinweise = ka.ueberwachungsliste(daten, a2)["hinweise"]
    assert any("Lücke" in t for _, t in hinweise)
    assert any("Anfangssaldo" in t for _, t in hinweise)  # previous closing balance was entered wrong
    assert ka.ohne_auszug(daten, 1)["anzahl"] == 3  # the three transactions of 2026-09-05
    with pytest.raises(BuchungsFehler, match="überschneidet"):
        _auszug(daten, admin_id, 3, "2026-09-03", "2026-09-05")
    with pytest.raises(BuchungsFehler, match="bereits erfasst"):
        _auszug(daten, admin_id, 1, "2026-10-01", "2026-10-31")


def test_vorschlag_naechster_auszug(daten, admin_id):
    assert ka.vorschlag(daten, 1)["nummer"] == 1
    _auszug(daten, admin_id, 7, "2026-09-01", "2026-09-30")
    v = ka.vorschlag(daten, 1)
    assert v == {"nummer": 8, "datum_von": "2026-10-01", "datum_bis": "2026-10-31"}
    _auszug(daten, admin_id, 8, "2026-10-01", "2026-12-31")
    assert ka.vorschlag(daten, 1)["nummer"] == 1  # new year starts again at 1


def test_pruefung_sperrt_und_erkennt_aenderungen(daten, admin_id):
    a = _auszug(daten, admin_id, 1, "2026-09-01", "2026-09-07")
    with pytest.raises(BuchungsFehler, match="Endsaldo"):
        ka.pruefung_vermerken(daten, a, admin_id)
    ka.speichern(daten, 1, {"nummer": 1, "datum_von": "2026-09-01", "datum_bis": "2026-09-07",
                            "endsaldo_cent": 160600}, admin_id, auszug_id=a)
    ka.pruefung_vermerken(daten, a, admin_id)
    with pytest.raises(BuchungsFehler, match="geprüft"):
        ka.speichern(daten, 1, {"nummer": 1, "datum_von": "2026-09-01", "datum_bis": "2026-09-06",
                                "endsaldo_cent": 160600}, admin_id, auszug_id=a)
    with pytest.raises(BuchungsFehler, match="geprüft"):
        ka.anmerkung_setzen(daten, 2, "Rechnung Bus", admin_id)
    with pytest.raises(BuchungsFehler, match="geprüft"):
        ka.loeschen(daten, a, admin_id)
    assert not ka.liste(daten, 1)[0]["geaendert"]
    # a later assignment changes the list -> shown as changed since the check
    daten.execute("UPDATE bankumsatz SET zuordnungsstatus='ignoriert' WHERE id=9")
    daten.execute("INSERT INTO umsatz_ignoriert (bankumsatz_id, grund, erstellt_von, erstellt_am)"
                  " VALUES (9, 'Kontoführung', ?, '2026-09-10 10:00:00')", (admin_id,))
    assert ka.liste(daten, 1)[0]["geaendert"]
    with pytest.raises(BuchungsFehler, match="Grund"):
        ka.pruefung_aufheben(daten, a, "", admin_id)
    ka.pruefung_aufheben(daten, a, "Gebühr nachträglich erklärt", admin_id)
    assert ka.anmerkung_setzen(daten, 2, "Rechnung Bus", admin_id)
    zeile = next(z for z in ka.ueberwachungsliste(daten, a)["zeilen"] if z["umsatz"]["id"] == 2)
    assert zeile["anmerkung"].startswith("Rechnung Bus")
    assert daten.scalar("SELECT COUNT(*) FROM protokoll WHERE aktion LIKE 'kontoauszug.%'") >= 4


def test_kontoauszug_oberflaeche_und_pdf(app, daten, admin_id):
    c = app.test_client()
    login(c)
    assert c.get("/kontoauszuege/").status_code == 200
    r = c.post("/kontoauszuege/", data={"konto": "1", "nummer": "1", "datum_von": "2026-09-01",
                                        "datum_bis": "2026-09-07", "endsaldo": "1.606,00"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Kontoauszug erfasst" in html and "Überwachungsliste" in html
    a = daten.scalar("SELECT id FROM kontoauszug")
    assert daten.scalar("SELECT endsaldo_cent FROM kontoauszug") == 160600
    for pfad in (f"/kontoauszuege/{a}/bearbeiten", "/umsaetze/2", "/"):
        assert c.get(pfad).status_code == 200, pfad
    r = c.get(f"/kontoauszuege/{a}?format=pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    r = c.get(f"/kontoauszuege/{a}?format=xlsx")
    assert r.mimetype.endswith("spreadsheetml.sheet")
    ws = openpyxl.load_workbook(io.BytesIO(r.data)).active
    werte = [zelle for zeile in ws.iter_rows(values_only=True) for zelle in zeile if zelle is not None]
    assert "Anfangsbestand" in werte and "Endsaldo" in werte and 1606.0 in werte and 1250.0 in werte
    assert c.get("/kontoauszuege/sammel.pdf?konto=1&jahr=2026").mimetype == "application/pdf"
    r = c.get("/kontoauszuege/sammel.xlsx?konto=1&jahr=2026")
    assert openpyxl.load_workbook(io.BytesIO(r.data)).sheetnames == ["Auszug 1-2026"]
    r = c.post(f"/kontoauszuege/{a}/pruefen", follow_redirects=True)
    assert "Prüfung vermerkt" in r.get_data(as_text=True)
    r = c.post("/kontoauszuege/umsatz/2/anmerkung", data={"anmerkung": "x"}, follow_redirects=True)
    assert "Prüfung aufheben" in r.get_data(as_text=True)


def test_lehrkraft_kein_zugriff_auf_kontoauszuege(app, daten):
    benutzer_anlegen(daten, "lehrer", "lehrkraft")
    c = app.test_client()
    login(c, "lehrer")
    assert c.get("/kontoauszuege/").status_code == 403
    assert c.get("/auswertungen/klassenlisten").status_code == 403


def test_klassenliste_ohne_but_und_erlassen(daten):
    klasse = daten.scalar("SELECT id FROM klasse WHERE bezeichnung='4a'")
    kl = klassen_zahlungsstatus(daten, klasse)
    assert kl["projekte"] and kl["zeilen"]
    namen = {z["kind"]["nachname"]: z for z in kl["zeilen"]}
    # every active child is listed; a BuT child only ever shows "no contribution"
    but = daten.one("SELECT * FROM kind WHERE but_status=1 AND klasse_id=?", (klasse,))
    if but is not None:
        assert all(f["status"] == "keiner" for f in namen[but["nachname"]]["felder"])
    stati = {f["status"] for z in kl["zeilen"] for f in z["felder"]}
    assert "bezahlt" in stati and "offen" in stati


def test_klassenliste_pdf(app, daten):
    c = app.test_client()
    login(c)
    assert c.get("/auswertungen/klassenlisten").status_code == 200
    assert c.get("/auswertungen/klassenlisten?klasse=1").status_code == 200
    for pfad in ("/auswertungen/klassenlisten?format=pdf", "/auswertungen/klassenlisten?klasse=1&format=pdf"):
        r = c.get(pfad)
        assert r.status_code == 200 and r.mimetype == "application/pdf", pfad


def test_erinnerungszettel(app, daten):
    c = app.test_client()
    login(c)
    for pro_seite in ("8", "4"):
        r = c.post("/auswertungen/erinnerungen/zettel", data={"stichtag": "2027-01-01", "pro_seite": pro_seite})
        assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert daten.scalar("SELECT COUNT(*) FROM protokoll WHERE aktion='brief.erinnerungszettel'") >= 1
    posten = daten.scalar("SELECT p.id FROM offener_posten p JOIN kind k ON k.id=p.kind_id"
                          " WHERE p.status='offen' AND k.but_status=0 AND p.faellig_am IS NOT NULL LIMIT 1")
    r = c.post("/auswertungen/erinnerungen/zettel", data={"stichtag": "2027-01-01", "posten": [str(posten)],
                                                          "qr": "0"})
    assert r.mimetype == "application/pdf"
    # nothing overdue -> message instead of an empty PDF
    r = c.post("/auswertungen/erinnerungen/zettel", data={"stichtag": "2000-01-01"}, follow_redirects=True)
    assert "keine offenen Beträge" in r.get_data(as_text=True)


def test_phosphor_lokal(app, admin_id):
    c = app.test_client()
    login(c)
    html = c.get("/").get_data(as_text=True)
    assert "/static/vendor/phosphor/phosphor.css" in html and "ph-house" in html
    assert c.get("/static/vendor/phosphor/Phosphor.woff2").status_code == 200


def test_xlsx_text_wird_nie_formel(daten, admin_id):
    from schulkasse.xlsx_export import ueberwachungslisten_xlsx
    a = _auszug(daten, admin_id, 1, "2026-09-01", "2026-09-07", 160600)
    ka.anmerkung_setzen(daten, 2, "=HYPERLINK(\"http://x\")", admin_id)
    daten_xlsx = ueberwachungslisten_xlsx([ka.ueberwachungsliste(daten, a)], {"schulname": "GGS"}, "")
    ws = openpyxl.load_workbook(io.BytesIO(daten_xlsx)).active
    zelle = next(c for zeile in ws.iter_rows() for c in zeile if isinstance(c.value, str) and "HYPERLINK" in c.value)
    assert zelle.data_type == "s"


def test_jahresendmitteilung_zusammensetzung(daten, admin_id):
    from schulkasse import anfangsbestand as ab
    from schulkasse.jahresende import jahresendmitteilung
    v = jahresendmitteilung(daten, "2026-12-31")
    k = v["konten"][0]
    assert k["saldo"] == 172950 and k["vorlaeufig"]  # last import ends 2026-09-11
    assert k["summe_projekte"] + sum(b for _, b in k["sonstige"]) == k["saldo"]
    assert any("Anfangsbestand" in t for t, _ in k["sonstige"])
    spende = daten.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    ab.verteilen(daten, 1, [{"projekt_id": spende, "betrag_cent": 125000}], admin_id)
    k = jahresendmitteilung(daten, "2026-12-31")["konten"][0]
    assert not any("Anfangsbestand" in t for t, _ in k["sonstige"])
    assert any(p["kurzcode"] == "SPENDE" and p["betrag"] >= 125000 for p in k["projekte"])
    assert k["summe_projekte"] + sum(b for _, b in k["sonstige"]) == k["saldo"]
    # before the first transaction only the opening balance counts
    assert jahresendmitteilung(daten, "2025-12-31")["konten"][0]["saldo"] == 125000


def test_jahresendmitteilung_oberflaeche(app, daten):
    c = app.test_client()
    login(c)
    html = c.get("/auswertungen/jahresendmitteilung?jahr=2026").get_data(as_text=True)
    assert "1.729,50" in html and "vorläufig" in html
    r = c.post("/auswertungen/jahresendmitteilung", data={"jahr": "2026", "bemerkung": "Busrechnung folgt"})
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert daten.scalar("SELECT COUNT(*) FROM protokoll WHERE aktion='export.jahresendmitteilung.pdf'") == 1
