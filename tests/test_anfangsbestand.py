"""Start of bookkeeping (Stichtag) and distribution of the opening balance."""
import pytest

from schulkasse import anfangsbestand as ab
from schulkasse.auswertung import kontrollrechnung, projekt_kennzahlen, projekt_umsaetze
from schulkasse.buchung import AKTIV_SQL, BuchungsFehler, ignorieren, vorschlag_annehmen, zuordnen
from schulkasse.matching import matching_lauf
from schulkasse.projekte import projekt_abschliessen

from .conftest import benutzer_anlegen, importiere_datei, login, posten_von

STICHTAG = "2026-09-04"
# Booked entries of part 1 before 04.09.: Becker, Westermann, Schmidt, Fischer, Yilmaz, Wagner
VOR_STICHTAG = 4500 - 31240 + 4500 + 4500 + 5350 + 2000
ANFANGSBESTAND = 125000 + VOR_STICHTAG


def _konto(db):
    return db.one("SELECT * FROM konto")


def _projekt(db, code):
    return db.scalar("SELECT id FROM projekt WHERE kurzcode=?", (code,))


@pytest.fixture
def mit_stichtag(demo, admin_id, dateien):
    ab.stichtag_setzen(demo, _konto(demo)["id"], STICHTAG, admin_id)
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    return demo


def test_anfangsbestand_berechnet(mit_stichtag):
    werte = ab.berechne(mit_stichtag, _konto(mit_stichtag))
    assert werte == {"stichtag": STICHTAG, "betrag": ANFANGSBESTAND, "hinweis": None}
    assert ab.saldenabgleich(mit_stichtag, _konto(mit_stichtag)) == []


def test_ohne_stichtag_ist_eroeffnungssaldo_der_anfangsbestand(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    werte = ab.berechne(demo, _konto(demo))
    assert werte["betrag"] == 125000 and werte["stichtag"] == "2026-09-01"


def test_umsaetze_vor_stichtag_werden_nicht_bearbeitet(mit_stichtag, admin_id):
    db = mit_stichtag
    anna = db.one("SELECT * FROM bankumsatz WHERE gegen_name='Thomas Becker'")
    # no automatic assignment and no suggestion before the Stichtag ...
    assert anna["zuordnungsstatus"] == "offen"
    assert db.scalar("SELECT COUNT(*) FROM vorschlag WHERE bankumsatz_id=?", (anna["id"],)) == 0
    assert posten_von(db, "Anna", "Becker")["status"] == "offen"
    # ... and it cannot be assigned or ignored by hand
    with pytest.raises(BuchungsFehler, match="vor dem Beginn der Kontoführung"):
        zuordnen(db, anna["id"], [{"projekt_id": _projekt(db, "SPENDE"), "betrag_cent": 4500}], "manuell", admin_id)
    with pytest.raises(BuchungsFehler, match="vor dem Beginn"):
        ignorieren(db, anna["id"], "Test", admin_id)
    # later transactions are matched as usual
    koch = db.one("SELECT * FROM bankumsatz WHERE gegen_name='Koch, Stefanie'")
    assert db.scalar("SELECT regel FROM vorschlag WHERE bankumsatz_id=?", (koch["id"],)) == "R6"
    assert matching_lauf(db, admin_id)["geprueft"] == db.scalar(
        f"SELECT COUNT(*) FROM bankumsatz u WHERE status='BOOK' AND zuordnungsstatus='offen' AND {AKTIV_SQL}")


def test_posten_aus_anfangsbestand_bezahlen(mit_stichtag, admin_id):
    db = mit_stichtag
    konto = _konto(db)["id"]
    anna = posten_von(db, "Anna", "Becker")
    emre, elif_ = posten_von(db, "Emre", "Yilmaz"), posten_von(db, "Elif", "Yilmaz")
    ab.verteilen(db, konto, [{"offener_posten_id": anna["id"], "betrag_cent": 4500},
                             {"offener_posten_id": emre["id"], "betrag_cent": 4500},
                             {"offener_posten_id": elif_["id"], "betrag_cent": 850, "notiz": "Juli"}], admin_id)
    assert [posten_von(db, *n)["status"] for n in (("Anna", "Becker"), ("Emre", "Yilmaz"))] == ["erledigt"] * 2
    kf = _projekt(db, "KF4A")
    kz = projekt_kennzahlen(db, kf)
    assert kz.saldo == 2 * 4500 and kz.bezahlt == 2
    zeilen = [r for r in projekt_umsaetze(db, kf) if r["art"] == "anfangsbestand"]
    assert len(zeilen) == 2 and zeilen[0]["buchungsdatum"] == STICHTAG
    assert ab.verteilt(db, konto) == 4500 + 4500 + 850


def test_verteilung_wird_geprueft(mit_stichtag, admin_id):
    db = mit_stichtag
    konto = _konto(db)["id"]
    spende = _projekt(db, "SPENDE")
    with pytest.raises(BuchungsFehler, match="mehr verteilt als vorhanden"):
        ab.verteilen(db, konto, [{"projekt_id": spende, "betrag_cent": ANFANGSBESTAND + 1}], admin_id)
    anna = posten_von(db, "Anna", "Becker")
    with pytest.raises(BuchungsFehler, match="positivem Betrag"):
        ab.verteilen(db, konto, [{"offener_posten_id": anna["id"], "betrag_cent": -4500}], admin_id)
    with pytest.raises(BuchungsFehler, match="anderen Projekt"):
        ab.verteilen(db, konto, [{"projekt_id": spende, "offener_posten_id": anna["id"], "betrag_cent": 4500}],
                     admin_id)
    projekt_abschliessen(db, spende, admin_id)
    with pytest.raises(BuchungsFehler, match="abgeschlossen"):
        ab.verteilen(db, konto, [{"projekt_id": spende, "betrag_cent": 100}], admin_id)
    # negative shares are possible, e.g. a project that owes money at the Stichtag
    ab.verteilen(db, konto, [{"projekt_id": _projekt(db, "AH26"), "betrag_cent": -31240}], admin_id)
    assert projekt_kennzahlen(db, _projekt(db, "AH26")).saldo == -31240
    assert ab.verteilt(db, konto) == -31240
    assert db.scalar("SELECT COUNT(*) FROM anfangsbestand") == 1  # failed attempts left nothing behind


def test_storno_oeffnet_posten_wieder(mit_stichtag, admin_id):
    db = mit_stichtag
    anna = posten_von(db, "Anna", "Becker")
    (aid,) = ab.verteilen(db, _konto(db)["id"], [{"offener_posten_id": anna["id"], "betrag_cent": 4500}], admin_id)
    with pytest.raises(BuchungsFehler):
        ab.stornieren(db, aid, "", admin_id)
    ab.stornieren(db, aid, "Doch nicht bezahlt", admin_id)
    assert posten_von(db, "Anna", "Becker")["status"] == "offen"
    assert ab.verteilt(db, _konto(db)["id"]) == 0
    assert db.one("SELECT * FROM protokoll WHERE aktion='anfangsbestand.stornieren' AND entitaet_id=?", (aid,))


def test_kontrollrechnung_null_mit_stichtag(mit_stichtag, admin_id, dateien):
    db = mit_stichtag
    importiere_datei(db, dateien["camt052_teil2_ueberlappend.xml"], "t2.xml", admin_id)
    konto = _konto(db)["id"]
    kopie, spende = _projekt(db, "KOPIE"), _projekt(db, "SPENDE")
    for u in db.all(f"SELECT * FROM bankumsatz u WHERE status='BOOK' AND zuordnungsstatus='offen' AND {AKTIV_SQL}"):
        v = db.one("SELECT id FROM vorschlag WHERE bankumsatz_id=? ORDER BY konfidenz DESC, id LIMIT 1", (u["id"],))
        if v:
            vorschlag_annehmen(db, v["id"], admin_id)
        elif "Kopiergeld" in u["verwendungszweck"] or "Spende" in u["verwendungszweck"]:
            ziel = kopie if "Kopiergeld" in u["verwendungszweck"] else spende
            zuordnen(db, u["id"], [{"projekt_id": ziel, "betrag_cent": u["betrag_cent"]}], "manuell", admin_id)
        else:
            ignorieren(db, u["id"], "Bankgebühr", admin_id)
    k = kontrollrechnung(db)["konten"][0]
    assert k["offen"] == 0
    assert k["differenz"] == k["nicht_verteilt"] == ANFANGSBESTAND
    # the pre-Stichtag payments are part of the opening balance; the rest goes to the donation pot
    anna = posten_von(db, "Anna", "Becker")
    ab.verteilen(db, konto, [{"offener_posten_id": anna["id"], "betrag_cent": 4500},
                             {"projekt_id": _projekt(db, "AH26"), "betrag_cent": -31240},
                             {"projekt_id": spende, "betrag_cent": ANFANGSBESTAND - 4500 + 31240}], admin_id)
    k = kontrollrechnung(db)["konten"][0]
    assert k["nicht_verteilt"] == 0
    assert k["differenz"] == 0


def test_stichtag_nicht_nach_zuordnungen(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)  # Becker (01.09.) booked automatically
    konto = _konto(demo)["id"]
    with pytest.raises(BuchungsFehler, match="bereits 3 zugeordnete"):
        ab.stichtag_setzen(demo, konto, STICHTAG, admin_id)
    with pytest.raises(BuchungsFehler, match="vor dem ersten importierten"):
        ab.stichtag_setzen(demo, konto, "2026-08-01", admin_id)
    ab.stichtag_setzen(demo, konto, "2026-09-01", admin_id)
    assert _konto(demo)["beginn_datum"] == "2026-09-01"


def test_stichtag_vorverlegen_aktiviert_umsaetze(mit_stichtag, admin_id):
    db = mit_stichtag
    aktive = ab.stichtag_setzen(db, _konto(db)["id"], "2026-09-01", admin_id)
    matching_lauf(db, admin_id, umsatz_ids=aktive)
    assert posten_von(db, "Anna", "Becker")["status"] == "erledigt"
    assert ab.berechne(db, _konto(db))["betrag"] == 125000


def test_saldenabgleich_meldet_luecken(mit_stichtag):
    db = mit_stichtag
    db.execute("UPDATE konto SET eroeffnungssaldo_cent=eroeffnungssaldo_cent+100")
    abweichung = ab.saldenabgleich(db, _konto(db))
    assert len(abweichung) == 1 and abweichung[0]["berechnet"] - abweichung[0]["bank"] == 100


def test_web_rollen_und_ablauf(app, mit_stichtag, admin_id):
    db = mit_stichtag
    konto = _konto(db)["id"]
    benutzer_anlegen(db, "sekretariat", "kasse")
    admin = app.test_client()
    login(admin)
    seite = admin.get(f"/verwaltung/konten/{konto}/anfangsbestand").get_data(as_text=True)
    assert "Anfangsbestand zum 04.09.2026" in seite and "1.146,10" in seite
    assert "noch nicht auf Projekte verteilt" in admin.get("/").get_data(as_text=True)

    kasse = app.test_client()
    login(kasse, "sekretariat")
    assert kasse.post(f"/verwaltung/konten/{konto}/anfangsbestand",
                      data={"aktion": "stichtag", "beginn_datum": "2026-09-01"}).status_code == 403
    anna = posten_von(db, "Anna", "Becker")
    r = kasse.post(f"/verwaltung/konten/{konto}/anfangsbestand", follow_redirects=True, data={
        "aktion": "verteilen", "projekt_id": ["", str(_projekt(db, "SPENDE"))], "zahlcode": [anna["zahlcode"].lower(), ""],
        "betrag": ["45,00", "100,00"], "notiz": ["", "Spendenstand Juli"]})
    assert "2 Anteil(e) des Anfangsbestands gespeichert" in r.get_data(as_text=True)
    assert posten_von(db, "Anna", "Becker")["status"] == "erledigt"

    ben = posten_von(db, "Ben", "Schmidt")
    r = kasse.post(f"/posten/{ben['id']}/anfangsbestand", data={"konto_id": str(konto), "betrag": "45,00"},
                   follow_redirects=True)
    assert "Aus dem Anfangsbestand verbucht" in r.get_data(as_text=True)
    assert posten_von(db, "Ben", "Schmidt")["status"] == "erledigt"

    vorher = db.one("SELECT id FROM bankumsatz WHERE gegen_name='Thomas Becker'")
    detail = admin.get(f"/umsaetze/{vorher['id']}").get_data(as_text=True)
    assert "vor dem Beginn der Kontoführung" in detail and "Zuordnen / aufteilen" not in detail
    liste = admin.get("/umsaetze?status=vorbeginn").get_data(as_text=True)
    assert "Thomas Becker" in liste and "Koch, Stefanie" not in liste
    assert "Thomas Becker" not in admin.get("/zuordnung").get_data(as_text=True)

    koch = db.one("SELECT v.id FROM vorschlag v JOIN bankumsatz u ON u.id=v.bankumsatz_id"
                  " WHERE u.gegen_name='Koch, Stefanie'")
    vorschlag_annehmen(db, koch["id"], admin_id)  # payment of 04.09.
    r = admin.post(f"/verwaltung/konten/{konto}/anfangsbestand", follow_redirects=True,
                   data={"aktion": "stichtag", "beginn_datum": "2026-09-10"})
    assert "bereits 1 zugeordnete" in r.get_data(as_text=True)  # blocked: would count the money twice
    assert _konto(db)["beginn_datum"] == STICHTAG
    for pfad in (f"/posten/{anna['id']}", f"/projekte/{_projekt(db, 'KF4A')}", "/projekte/1/jahresabschluss.pdf",
                 "/auswertungen/kassenbuch?von=2026-09-01&bis=2026-09-30",
                 "/auswertungen/kassenbuch?von=2026-09-01&bis=2026-09-30&format=csv", "/verwaltung/konten"):
        assert admin.get(pfad).status_code == 200, pfad
    csv = admin.get("/auswertungen/kassenbuch?von=2026-09-01&bis=2026-09-30&format=csv").get_data(as_text=True)
    assert "vor Beginn der Kontoführung" in csv


def _saldo_datei(buchungen, von, bis, anfang, msg):
    from schulkasse.testdaten import SCHUL_IBAN, camt_xml
    return camt_xml(buchungen, iban=SCHUL_IBAN, von=von, bis=bis, anfangssaldo=anfang, msg_id=msg)


def test_saldenabgleich_tagesgrenze_mitten_am_tag(demo, admin_id):
    """Banks split exports within a day: file A ends with part of 10.06., file B brings the rest."""
    from schulkasse.testdaten import Buchung
    a = [Buchung("2026-06-09", 1000, "a1", ref="A1"), Buchung("2026-06-10", 2000, "a2", ref="A2")]
    b = [Buchung("2026-06-10", 17000, "b1", ref="B1"), Buchung("2026-06-11", -500, "b2", ref="B2")]
    datei_a = _saldo_datei(a, "2026-06-09", "2026-06-10", 100000, "A")
    datei_b = _saldo_datei(b, "2026-06-10", "2026-06-11", 103000, "B")
    for daten, name in ((datei_a, "a.xml"), (datei_b, "b.xml"), (datei_a, "a.xml")):  # A imported twice
        importiere_datei(demo, daten, name, admin_id)
    assert ab.saldenabgleich(demo, _konto(demo)) == []


def test_saldenabgleich_erkennt_fehlenden_auszug(demo, admin_id):
    from schulkasse.testdaten import Buchung
    a = [Buchung("2026-06-09", 1000, "a1", ref="A1")]
    # the statement for 10.06.-11.06. (+170,00 EUR) is missing
    c = [Buchung("2026-06-12", 500, "c1", ref="C1")]
    importiere_datei(demo, _saldo_datei(a, "2026-06-09", "2026-06-09", 100000, "A"), "a.xml", admin_id)
    importiere_datei(demo, _saldo_datei(c, "2026-06-12", "2026-06-12", 101000 + 17000, "C"), "c.xml", admin_id)
    abweichung = ab.saldenabgleich(demo, _konto(demo))
    assert len(abweichung) == 1
    assert abweichung[0]["bank"] - abweichung[0]["berechnet"] == 17000
