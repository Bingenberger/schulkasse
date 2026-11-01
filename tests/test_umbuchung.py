"""Transfers between projects (Umbuchung)."""
import pytest

from schulkasse import umbuchung as ub
from schulkasse.auswertung import kontrollrechnung, projekt_kennzahlen, projekt_umsaetze
from schulkasse.buchung import BuchungsFehler
from schulkasse.projekte import ProjektFehler, posten_anlegen, projekt_abschliessen

from .conftest import importiere_datei, login, posten_von


def _p(db, code):
    return db.scalar("SELECT id FROM projekt WHERE kurzcode=?", (code,))


@pytest.fixture
def gebucht(demo, admin_id, dateien):
    """Three payments of 45,00 EUR are booked automatically on KF4A."""
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    return demo


def test_verschiebt_saldo_ohne_kontostand_zu_aendern(gebucht, admin_id):
    db = gebucht
    kf, spende = _p(db, "KF4A"), _p(db, "SPENDE")
    vorher = kontrollrechnung(db)["gesamt"]["differenz"]
    ub.anlegen(db, kf, spende, 4500, "Restguthaben", admin_id)
    assert ub.saldo(db, kf) == 2 * 4500 and ub.saldo(db, spende) == 4500
    assert projekt_kennzahlen(db, spende).saldo == 4500
    # the money did not move on the bank account
    assert kontrollrechnung(db)["gesamt"]["differenz"] == vorher
    assert db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM zahlung") == \
        db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM zuordnung WHERE storniert=0")


def test_deckt_offenen_posten_und_storno(gebucht, admin_id):
    db = gebucht
    kf, spende = _p(db, "KF4A"), _p(db, "SPENDE")
    ub.anlegen(db, kf, spende, 10000, "Startkapital", admin_id)
    mia = posten_von(db, "Mia", "Braun")
    uid = ub.anlegen(db, spende, kf, mia["sollbetrag_cent"], "Förderverein übernimmt Beitrag", admin_id,
                     offener_posten_id=mia["id"])
    assert posten_von(db, "Mia", "Braun")["status"] == "erledigt"
    ub.stornieren(db, uid, "Doch nicht übernommen", admin_id)
    assert posten_von(db, "Mia", "Braun")["status"] == "offen"
    assert ub.saldo(db, spende) == 10000
    assert db.one("SELECT * FROM protokoll WHERE aktion='umbuchung.stornieren' AND entitaet_id=?", (uid,))


def test_pruefungen(demo, admin_id):
    db = demo
    kf, spende, th = _p(db, "KF4A"), _p(db, "SPENDE"), _p(db, "TH2B")
    with pytest.raises(BuchungsFehler, match="verschieden"):
        ub.anlegen(db, kf, kf, 100, "Test", admin_id)
    with pytest.raises(BuchungsFehler, match="größer als"):
        ub.anlegen(db, kf, spende, 0, "Test", admin_id)
    with pytest.raises(BuchungsFehler, match="Grund"):
        ub.anlegen(db, kf, spende, 100, "  ", admin_id)
    fremder = posten_von(db, "Elif", "Yilmaz")  # belongs to TH2B, not to KF4A
    with pytest.raises(BuchungsFehler, match="empfangenden Projekt"):
        ub.anlegen(db, spende, kf, 100, "Test", admin_id, offener_posten_id=fremder["id"])
    ausgang = posten_anlegen(db, th, richtung="ausgang", kategorie="rechnung", bezeichnung="Rechnung",
                             sollbetrag_cent=1000, benutzer_id=admin_id)
    with pytest.raises(BuchungsFehler, match="Eingangsposten"):
        ub.anlegen(db, spende, th, 1000, "Test", admin_id, offener_posten_id=ausgang)
    projekt_abschliessen(db, spende, admin_id)
    with pytest.raises(BuchungsFehler, match="abgeschlossen"):
        ub.anlegen(db, kf, spende, 100, "Test", admin_id)
    assert db.scalar("SELECT COUNT(*) FROM umbuchung") == 0


def test_abschluss_verlangt_ausgleich(gebucht, admin_id):
    db = gebucht
    kf, spende = _p(db, "KF4A"), _p(db, "SPENDE")
    for p in db.all("SELECT id FROM offener_posten WHERE projekt_id=? AND status IN ('offen','teilweise')", (kf,)):
        db.execute("UPDATE offener_posten SET status='erlassen', erlass_grund='Test' WHERE id=?", (p["id"],))
    with pytest.raises(ProjektFehler, match="Saldo von 135,00"):
        projekt_abschliessen(db, kf, admin_id)
    ub.anlegen(db, kf, spende, ub.saldo(db, kf), "Restguthaben", admin_id)
    projekt_abschliessen(db, kf, admin_id)          # balance is zero now
    assert db.scalar("SELECT status FROM projekt WHERE id=?", (kf,)) == "abgeschlossen"
    assert ub.saldo(db, spende) == 3 * 4500


def test_in_projektumsaetzen_beidseitig(gebucht, admin_id):
    db = gebucht
    kf, spende = _p(db, "KF4A"), _p(db, "SPENDE")
    ub.anlegen(db, kf, spende, 4500, "Restguthaben", admin_id)
    ab_zeile = [z for z in projekt_umsaetze(db, kf) if z["art"] == "umbuchung"][0]
    zu_zeile = [z for z in projekt_umsaetze(db, spende) if z["art"] == "umbuchung"][0]
    assert ab_zeile["betrag_cent"] == -4500 and "Umbuchung an" in ab_zeile["gegen_name"]
    assert zu_zeile["betrag_cent"] == 4500 and "Umbuchung von" in zu_zeile["gegen_name"]


def test_ueber_die_oberflaeche(app, gebucht, admin_id):
    db = gebucht
    kf, spende = _p(db, "KF4A"), _p(db, "SPENDE")
    c = app.test_client()
    login(c)
    mia = posten_von(db, "Mia", "Braun")
    r = c.post(f"/projekte/{kf}/umbuchung", follow_redirects=True, data={
        "richtung": "ab", "ziel_projekt_id": str(spende), "betrag": "45,00", "grund": "Restguthaben"})
    assert "Umbuchung gespeichert" in r.get_data(as_text=True)
    r = c.post(f"/projekte/{spende}/umbuchung", follow_redirects=True, data={
        "richtung": "ab", "ziel_projekt_id": str(kf), "betrag": "45,00", "grund": "Beitrag übernommen",
        "zahlcode": mia["zahlcode"].lower()})
    assert "Umbuchung gespeichert" in r.get_data(as_text=True)
    assert posten_von(db, "Mia", "Braun")["status"] == "erledigt"
    r = c.post(f"/projekte/{kf}/umbuchung", follow_redirects=True, data={
        "richtung": "ab", "ziel_projekt_id": str(spende), "betrag": "10,00", "grund": "x", "zahlcode": "GIBTS-9999"})
    assert "gehört nicht zum empfangenden Projekt" in r.get_data(as_text=True)
    u = db.one("SELECT id FROM umbuchung ORDER BY id DESC LIMIT 1")
    r = c.post(f"/umbuchungen/{u['id']}/stornieren", data={"grund": "Irrtum", "projekt_id": str(kf)},
               follow_redirects=True)
    assert "Umbuchung storniert" in r.get_data(as_text=True)
    assert posten_von(db, "Mia", "Braun")["status"] == "offen"
    assert c.get(f"/projekte/{kf}").status_code == 200


def test_abgeschlossenes_projekt_ist_ausgeglichen(app, demo, admin_id, dateien):
    """After a transfer the closed project shows a balance of 0 (F2.4)."""
    from schulkasse import umbuchung as ub
    from schulkasse.auswertung import kontenverteilung, projekt_kennzahlen
    from schulkasse.buchung import zuordnen
    from schulkasse.projekte import projekt_abschliessen
    from .conftest import importiere_datei, login
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    ah = demo.scalar("SELECT id FROM projekt WHERE kurzcode='AH26'")
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    u = demo.one("SELECT * FROM bankumsatz WHERE betrag_cent<0 AND status='BOOK' AND zuordnungsstatus='offen'"
                 " ORDER BY id")
    zuordnen(demo, u["id"], [{"projekt_id": ah, "betrag_cent": abs(u["betrag_cent"])}], "manuell", admin_id)
    assert projekt_kennzahlen(demo, ah).saldo == -abs(u["betrag_cent"])
    ub.anlegen(demo, spende, ah, abs(u["betrag_cent"]), "Fehlbetrag ausgeglichen", admin_id)
    demo.execute("UPDATE offener_posten SET status='storniert' WHERE projekt_id=? AND status IN ('offen','teilweise')",
                 (ah,))
    projekt_abschliessen(demo, ah, admin_id)
    assert projekt_kennzahlen(demo, ah).saldo == 0
    # a settled project no longer shows up with zero rows in the per-account report
    assert all(p["id"] != ah for p in kontenverteilung(demo)["projekte"])
    c = app.test_client()
    login(c)
    seite = c.get(f"/projekte/{ah}").get_data(as_text=True)
    assert "ist ausgeglichen" in seite and "Restsaldo" not in seite


def test_umbuchung_konto_nachtragen(app, demo, admin_id):
    from schulkasse import umbuchung as ub
    from .conftest import login
    spende = demo.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    kopie = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KOPIE'")
    uid_ = ub.anlegen(demo, spende, kopie, 5000, "Test", admin_id)
    demo.execute("UPDATE umbuchung SET konto_id=NULL WHERE id=?", (uid_,))
    assert [u["id"] for u in ub.ohne_konto(demo)] == [uid_]
    konto = demo.scalar("SELECT id FROM konto ORDER BY id")
    c = app.test_client()
    login(c)
    assert "Umbuchungen ohne Konto" in c.get("/auswertungen/kontenverteilung").get_data(as_text=True)
    r = c.post(f"/auswertungen/umbuchungen/{uid_}/konto", data={"konto_id": konto}, follow_redirects=True)
    assert "zugeordnet" in r.get_data(as_text=True)
    assert demo.scalar("SELECT konto_id FROM umbuchung WHERE id=?", (uid_,)) == konto
    assert ub.ohne_konto(demo) == []
