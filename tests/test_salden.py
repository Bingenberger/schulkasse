"""Item status and balance calculations (F3.5, F3.6, F8.1, F8.3)."""
import pytest

from schulkasse.auswertung import kontrollrechnung, projekt_kennzahlen, ueberzahlungen
from schulkasse.buchung import BuchungsFehler, berechne_posten_status, stornieren, zuordnen
from schulkasse.geld import BetragFehler, format_cent, parse_betrag
from schulkasse.projekte import ProjektFehler, projekt_abschliessen

from .conftest import importiere_datei, posten_von


@pytest.mark.parametrize("soll,bezahlt,alt,neu", [
    (4500, 0, "offen", "offen"), (4500, 100, "offen", "teilweise"), (4500, 4500, "teilweise", "erledigt"),
    (4500, 5000, "offen", "erledigt"), (4500, 0, "erledigt", "offen"), (4500, 0, "erlassen", "erlassen"),
    (4500, 4500, "storniert", "storniert")])
def test_posten_status(soll, bezahlt, alt, neu):
    assert berechne_posten_status(soll, bezahlt, alt) == neu


def test_betraege_format_und_parse():
    assert format_cent(123456) == "1.234,56 €"
    assert format_cent(-5) == "-0,05 €"
    assert format_cent(0) == "0,00 €"
    assert parse_betrag("1.234,56") == 123456
    assert parse_betrag("12,5") == 1250
    assert parse_betrag("12.50") == 1250
    assert parse_betrag("45") == 4500
    assert parse_betrag("1.234") == 123400
    with pytest.raises(BetragFehler):
        parse_betrag("12,345")


def _umsatz(db, name):
    return db.one("SELECT * FROM bankumsatz WHERE gegen_name=? ORDER BY id", (name,))


def test_kennzahlen_projekt(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    kz = projekt_kennzahlen(demo, kf)
    # three R1 payments of 45,00 were booked automatically
    assert kz.saldo == 3 * 4500
    assert kz.gesamt == 25 and kz.bezahlt == 3
    assert kz.erwartete_eingaenge == 22 * 4500
    assert kz.prognose == kz.saldo + kz.erwartete_eingaenge - kz.erwartete_ausgaenge

    ah = demo.scalar("SELECT id FROM projekt WHERE kurzcode='AH26'")
    kz = projekt_kennzahlen(demo, ah)
    assert kz.erwartete_ausgaenge == 31240 and kz.erwartete_eingaenge == 31240 and kz.prognose == 0


def test_teilzahlung_und_ueberzahlung(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    emma = posten_von(demo, "Emma", "Hoffmann")
    u = _umsatz(demo, "Kai Hoffmann")
    zuordnen(demo, u["id"], [{"offener_posten_id": emma["id"], "betrag_cent": 5000}], "manuell", admin_id)
    assert demo.one("SELECT status FROM offener_posten WHERE id=?", (emma["id"],))["status"] == "erledigt"
    ueber = ueberzahlungen(demo)
    assert [(r["id"], r["bezahlt"] - r["sollbetrag_cent"]) for r in ueber] == [(emma["id"], 500)]


def test_aufteilung_darf_umsatz_nicht_ueberschreiten(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = _umsatz(demo, "Hannelore Weber")  # 50,00 donation
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    kopie = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KOPIE'")
    with pytest.raises(BuchungsFehler):
        zuordnen(demo, u["id"], [{"projekt_id": spende, "betrag_cent": 3000},
                                 {"projekt_id": kopie, "betrag_cent": 2001}], "manuell", admin_id)
    zuordnen(demo, u["id"], [{"projekt_id": spende, "betrag_cent": 3000}], "manuell", admin_id)
    assert demo.one("SELECT zuordnungsstatus FROM bankumsatz WHERE id=?", (u["id"],))[0] == "teilweise"
    zuordnen(demo, u["id"], [{"projekt_id": kopie, "betrag_cent": 2000}], "manuell", admin_id)
    assert demo.one("SELECT zuordnungsstatus FROM bankumsatz WHERE id=?", (u["id"],))[0] == "zugeordnet"


def test_vorgemerkt_nicht_zuordenbar(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    pdng = demo.one("SELECT * FROM bankumsatz WHERE status='PDNG'")
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    with pytest.raises(BuchungsFehler):
        zuordnen(demo, pdng["id"], [{"projekt_id": kf, "betrag_cent": 4500}], "manuell", admin_id)


def test_storno_braucht_grund(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    z = demo.one("SELECT id FROM zuordnung LIMIT 1")
    with pytest.raises(BuchungsFehler):
        stornieren(demo, z["id"], "  ", admin_id)


def test_abschluss_nur_ohne_offene_posten(demo, admin_id):
    th = demo.scalar("SELECT id FROM projekt WHERE kurzcode='TH2B'")
    with pytest.raises(ProjektFehler):
        projekt_abschliessen(demo, th, admin_id)
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    projekt_abschliessen(demo, spende, admin_id)
    assert demo.one("SELECT status FROM projekt WHERE id=?", (spende,))[0] == "abgeschlossen"


def test_kontrollrechnung_ohne_import(demo):
    assert kontrollrechnung(demo)["vorhanden"] is False


def test_kontrollrechnung_zaehlt_offene(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    k = kontrollrechnung(demo)["gesamt"]
    assert k["anfangsbestand"] == 125000 and k["nicht_verteilt"] == 125000
    offen_summe = demo.scalar("SELECT SUM(betrag_cent) FROM bankumsatz WHERE status='BOOK' AND zuordnungsstatus='offen'")
    assert k["differenz"] == offen_summe + k["nicht_verteilt"]
    assert k["offen"] > 0


def _ueberzahlen(db, admin_id, dateien, betrag=5000):
    """Emma's item (45,00 EUR) is paid with 50,00 EUR."""
    importiere_datei(db, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    emma = posten_von(db, "Emma", "Hoffmann")
    u = db.one("SELECT * FROM bankumsatz WHERE gegen_name='Kai Hoffmann'")
    zuordnen(db, u["id"], [{"offener_posten_id": emma["id"], "betrag_cent": betrag}], "manuell", admin_id)
    return emma


def test_ueberzahlung_klaeren(demo, admin_id, dateien):
    from schulkasse.auswertung import anzahl_ueberzahlungen, ueberzahlungen
    from schulkasse.projekte import ueberzahlung_klaeren
    emma = _ueberzahlen(demo, admin_id, dateien)
    assert anzahl_ueberzahlungen(demo) == 1
    with pytest.raises(BuchungsFehler, match="geklärt"):
        ueberzahlung_klaeren(demo, emma["id"], "  ", admin_id)
    ueberzahlung_klaeren(demo, emma["id"], "Als Spende behalten", admin_id)
    assert anzahl_ueberzahlungen(demo) == 0 and ueberzahlungen(demo) == []
    geklaert = ueberzahlungen(demo, geklaert=True)
    assert len(geklaert) == 1 and geklaert[0]["ueberzahlung_geklaert_cent"] == 500
    # amounts are untouched
    assert geklaert[0]["bezahlt"] == 5000 and posten_von(demo, "Emma", "Hoffmann")["status"] == "erledigt"
    assert demo.one("SELECT * FROM protokoll WHERE aktion='offener_posten.ueberzahlung_klaeren'") is not None
    ueberzahlung_klaeren(demo, emma["id"], "", admin_id, aufheben=True)
    assert anzahl_ueberzahlungen(demo) == 1


def test_weitere_ueberzahlung_taucht_wieder_auf(demo, admin_id, dateien):
    from schulkasse.auswertung import anzahl_ueberzahlungen
    from schulkasse.projekte import ueberzahlung_klaeren
    emma = _ueberzahlen(demo, admin_id, dateien)
    ueberzahlung_klaeren(demo, emma["id"], "Als Spende behalten", admin_id)
    assert anzahl_ueberzahlungen(demo) == 0
    u = demo.one("SELECT * FROM bankumsatz WHERE gegen_name='Hannelore Weber'")  # another 50,00 EUR
    zuordnen(demo, u["id"], [{"offener_posten_id": emma["id"], "betrag_cent": 1000}], "manuell", admin_id)
    assert anzahl_ueberzahlungen(demo) == 1  # now 15,00 EUR too much, only 5,00 EUR were settled


def test_ueberzahlung_ohne_ueberzahlung(demo, admin_id, dateien):
    from schulkasse.projekte import ueberzahlung_klaeren
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    anna = posten_von(demo, "Anna", "Becker")
    with pytest.raises(BuchungsFehler, match="keine Überzahlung"):
        ueberzahlung_klaeren(demo, anna["id"], "Test", admin_id)


def test_ueberzahlung_ueber_die_oberflaeche(app, demo, admin_id, dateien):
    from .conftest import login
    emma = _ueberzahlen(demo, admin_id, dateien)
    c = app.test_client()
    login(c)
    assert "5,00" in c.get("/auswertungen/ueberzahlungen").get_data(as_text=True)
    assert ">1<" in c.get("/").get_data(as_text=True)
    r = c.post(f"/posten/{emma['id']}/ueberzahlung", follow_redirects=True,
               data={"grund": "Rest als Spende", "zurueck": "/auswertungen/ueberzahlungen"})
    text = r.get_data(as_text=True)
    assert "als geklärt vermerkt" in text and "Keine offenen Überzahlungen" in text and "Rest als Spende" in text
    r = c.post(f"/posten/{emma['id']}/ueberzahlung", data={"aktion": "aufheben"}, follow_redirects=True)
    assert "wieder als offen" in r.get_data(as_text=True)
