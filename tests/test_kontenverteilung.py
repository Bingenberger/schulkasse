"""Balance of a project split over the bank accounts (F8.1)."""
from schulkasse import umbuchung as ub
from schulkasse.auswertung import kontenverteilung, projekt_je_konto
from schulkasse.buchung import zuordnen
from schulkasse.testdaten import FREMD_IBAN

from .conftest import importiere_datei, login


def _zweites_konto(db, admin_id, dateien):
    """The second account gets the transactions of the 'foreign' CAMT file."""
    db.execute("INSERT INTO konto (bezeichnung, iban, bic, inhaber, aktiv) VALUES (?,?,?,?,1)",
               ("Fördervereinskonto", FREMD_IBAN, "GENODED1XXX", "Förderverein"))
    importiere_datei(db, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    importiere_datei(db, dateien["camt052_fremdkonto.xml"], "fremd.xml", admin_id)
    return db.scalar("SELECT id FROM konto WHERE iban=?", (FREMD_IBAN,))


def test_projekt_auf_zwei_konten(demo, admin_id, dateien):
    konto2 = _zweites_konto(demo, admin_id, dateien)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    # the same payments arrived again on the second account and are booked there
    offen = demo.all("SELECT * FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                     " AND zuordnungsstatus='offen' ORDER BY id", (konto2,))
    assert offen
    zuordnen(demo, offen[0]["id"], [{"projekt_id": kf, "betrag_cent": abs(offen[0]["betrag_cent"])}],
             "manuell", admin_id)
    je_konto = dict((k["bezeichnung"], b) for k, b in projekt_je_konto(demo, kf))
    assert je_konto["Fördervereinskonto"] == abs(offen[0]["betrag_cent"])
    assert je_konto["Schulkonto"] == 3 * 4500  # three payments matched automatically

    v = kontenverteilung(demo)
    zeile = next(p for p in v["projekte"] if p["id"] == kf)
    assert zeile["aufgeteilt"] and zeile["konten_anzahl"] == 2
    assert zeile["summe"] == 3 * 4500 + abs(offen[0]["betrag_cent"])
    assert v["summen"][konto2] == abs(offen[0]["betrag_cent"])
    assert v["gesamt"] == sum(p["summe"] for p in v["projekte"])
    # only projects on several accounts
    nur = kontenverteilung(demo, nur_aufgeteilt=True)
    assert [p["id"] for p in nur["projekte"]] == [kf]


def test_umbuchung_haelt_das_konto_fest(demo, admin_id, dateien):
    konto2 = _zweites_konto(demo, admin_id, dateien)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    assert ub.konto_vorschlag(demo, kf) == demo.scalar("SELECT id FROM konto WHERE bezeichnung='Schulkonto'")
    ub.anlegen(demo, kf, spende, 1000, "Test", admin_id, konto_id=konto2)
    assert dict((k["bezeichnung"], b) for k, b in projekt_je_konto(demo, spende))["Fördervereinskonto"] == 1000
    assert dict((k["bezeichnung"], b) for k, b in projekt_je_konto(demo, kf))["Fördervereinskonto"] == -1000
    v = kontenverteilung(demo)
    assert v["summen"][konto2] == 0 and v["ohne_konto"] == 0  # the transfer nets out per account
    assert demo.scalar("SELECT konto_id FROM umbuchung") == konto2
    assert ub.eintraege(demo, projekt_id=kf)[0]["konto"] == "Fördervereinskonto"


def test_kontenverteilung_ueber_die_oberflaeche(app, demo, admin_id, dateien):
    _zweites_konto(demo, admin_id, dateien)
    c = app.test_client()
    login(c)
    seite = c.get("/auswertungen/kontenverteilung").get_data(as_text=True)
    assert "Fördervereinskonto" in seite and "Schulkonto" in seite
    csv = c.get("/auswertungen/kontenverteilung?format=csv")
    assert csv.headers["Content-Type"].startswith("text/csv") and "Klassenfahrt" in csv.get_data(as_text=True)
    pdf = c.get("/auswertungen/kontenverteilung?format=pdf")
    assert pdf.data[:4] == b"%PDF"
