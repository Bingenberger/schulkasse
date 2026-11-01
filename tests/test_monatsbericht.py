"""Monthly report for the account holder."""
import pytest

from schulkasse.buchung import BuchungsFehler
from schulkasse.monatsbericht import monat_aus_text, monatsbericht

from .conftest import importiere_datei, login


def test_monat_aus_text_fehler():
    with pytest.raises(BuchungsFehler):
        monat_aus_text("September 2026")
    with pytest.raises(BuchungsFehler):
        monat_aus_text("2026-13")


def test_monatsbericht_zahlen(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    v = monatsbericht(demo, 2026, 9)
    assert v["monatsname"] == "September" and v["von"] == "2026-09-01" and v["bis"] == "2026-09-30"
    assert v["vortag"] == "2026-08-31"
    k = v["konten"][0]
    # the statement of the month and its balances match the cash book
    assert k["kb"]["anfangssaldo"] + k["kb"]["eingaenge"] + k["kb"]["ausgaenge"] == k["kb"]["endsaldo"]
    assert k["kb"]["zeilen"] and all("projekte" in z for z in k["kb"]["zeilen"])
    # three parent contributions were matched automatically
    kf = next(p for p in v["projekte"] if p["kurzcode"] == "KF4A")
    assert kf["stand_anfang"] == 0 and kf["eingang"] == 3 * 4500 and kf["stand_ende"] == 3 * 4500
    # projects plus the part that is not distributed yet equals the account balance
    assert v["summen"]["stand_ende"] + v["nicht_verteilt_gesamt"] == v["kontostand_gesamt"]
    assert k["verteilt"] == v["je_konto_summen"][k["konto"]["id"]]
    # a month without any transaction is empty but still adds up
    leer = monatsbericht(demo, 2026, 7)
    assert all(not kk["kb"]["zeilen"] for kk in leer["konten"])
    assert leer["summen"]["stand_ende"] + leer["nicht_verteilt_gesamt"] == leer["kontostand_gesamt"]


def test_monatsbericht_umbuchung_und_anfangsbestand(demo, admin_id, dateien):
    from schulkasse import anfangsbestand as ab
    from schulkasse import umbuchung as ub
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    konto = demo.one("SELECT * FROM konto ORDER BY id")
    demo.execute("UPDATE konto SET beginn_datum='2026-09-01' WHERE id=?", (konto["id"],))
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    kopie = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KOPIE'")
    ab.verteilen(demo, konto["id"], [{"projekt_id": spende, "betrag_cent": 100000}], admin_id)
    ub.anlegen(demo, spende, kopie, 25000, "Kopierkosten aus Spenden", admin_id)
    v = monatsbericht(demo, 2026, 9)
    sp = next(p for p in v["projekte"] if p["kurzcode"] == "SPENDE")
    ko = next(p for p in v["projekte"] if p["kurzcode"] == "KOPIE")
    assert sp["umbuchung"] == -25000 and ko["umbuchung"] == 25000
    assert sp["anfangsbestand"] == 100000 and sp["eingang"] == 0  # the share is not an incoming payment
    assert sp["stand_ende"] == (sp["stand_anfang"] + sp["anfangsbestand"] + sp["eingang"] + sp["ausgang"]
                                + sp["umbuchung"])
    assert v["summen"]["stand_ende"] + v["nicht_verteilt_gesamt"] == v["kontostand_gesamt"]


def test_monatsbericht_ueber_die_oberflaeche(app, demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    c = app.test_client()
    login(c)
    seite = c.get("/auswertungen/monatsbericht?monat=2026-09").get_data(as_text=True)
    assert "Monatsbericht September 2026" in seite and "Klassenfahrt" in seite
    assert "noch nicht auf Projekte verteilt" in seite
    pdf = c.get("/auswertungen/monatsbericht?monat=2026-09&format=pdf")
    assert pdf.data[:4] == b"%PDF" and "Monatsbericht_2026-09.pdf" in pdf.headers["Content-Disposition"]
    csv = c.get("/auswertungen/monatsbericht?monat=2026-09&format=csv").get_data(as_text=True)
    assert "Projekt / Zuordnung" in csv and "KF4A" in csv
    csvp = c.get("/auswertungen/monatsbericht?monat=2026-09&format=csv-projekte").get_data(as_text=True)
    assert "Stand 2026-08-31" in csvp and "KF4A" in csvp
    assert demo.scalar("SELECT COUNT(*) FROM protokoll WHERE aktion LIKE 'export.monatsbericht%'") == 3
    # an invalid month lands back on the form with a message
    r = c.get("/auswertungen/monatsbericht?monat=2026-99", follow_redirects=True)
    assert "Format 2026-09" in r.get_data(as_text=True)
