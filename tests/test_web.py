"""Web layer: login, CSRF, sessions, page smoke tests, exports."""
from io import BytesIO

import pytest

from schulkasse import create_app
from schulkasse.testdaten import KLASSENLISTE, importiere_testdateien

from .conftest import PASSWORT, benutzer_anlegen, login


def test_anmeldung_erforderlich(app):
    r = app.test_client().get("/")
    assert r.status_code == 302 and "/anmelden" in r.headers["Location"]


def test_login_und_cookie(app, admin_id):
    c = app.test_client()
    r = login(c)
    assert r.status_code == 302
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert c.get("/").status_code == 200


def test_secure_cookie_standard(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "s.sqlite3"), "TESTING": True})
    r = app.test_client().get("/anmelden")
    assert "Secure" in r.headers["Set-Cookie"]


def test_sperre_nach_fehlversuchen(app, admin_id):
    c = app.test_client()
    for _ in range(10):
        assert login(c, passwort="falsch-falsch-falsch").status_code == 401
    r = login(c)  # correct password, but locked
    assert r.status_code == 401 and "gesperrt" in r.get_data(as_text=True)


def test_csrf_pflicht(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "c.sqlite3"), "TESTING": True, "SESSION_COOKIE_SECURE": False})
    from schulkasse.db import Database
    db = Database(app.config["DATABASE"])
    benutzer_anlegen(db, "admin", "admin")
    db.close()
    c = app.test_client()
    assert c.post("/anmelden", data={"benutzername": "admin", "passwort": PASSWORT}).status_code == 400
    html = c.get("/anmelden").get_data(as_text=True)
    token = html.split('name="csrf_token" value="')[1].split('"')[0]
    r = c.post("/anmelden", data={"benutzername": "admin", "passwort": PASSWORT, "csrf_token": token})
    assert r.status_code == 302


def test_inaktivitaet_abmeldung(app, admin_id, db):
    c = app.test_client()
    login(c)
    db.execute("UPDATE sitzung SET zuletzt_aktiv='2000-01-01 00:00:00'")
    assert c.get("/").status_code == 302


def test_passwort_mindestlaenge(app, admin_id):
    c = app.test_client()
    login(c)
    r = c.post("/verwaltung/benutzer", data={"benutzername": "kurz", "anzeigename": "K", "rolle": "kasse",
                                            "passwort": "zu-kurz"}, follow_redirects=True)
    assert "mindestens 12 Zeichen" in r.get_data(as_text=True)


@pytest.fixture
def voll(app, demo, admin_id):
    importiere_testdateien(demo, admin_id)
    c = app.test_client()
    login(c)
    return c


SEITEN = ["/", "/zuordnung", "/zuordnung?nur=vorschlag&richtung=ein", "/umsaetze", "/umsaetze?q=Becker",
          "/umsaetze?status=ignoriert", "/import", "/projekte", "/projekte/neu", "/projekte/1", "/projekte/1/massenanlage",
          "/projekte/1/posten/neu", "/projekte/1/briefe", "/projekte/1/zahlungsstand", "/projekte/1/bearbeiten",
          "/projekte/3", "/posten/1", "/umsaetze/1", "/umsaetze/5", "/stammdaten/kinder", "/stammdaten/kinder/1",
          "/stammdaten/kinder/neu", "/stammdaten/klassen", "/stammdaten/schuljahre", "/stammdaten/import",
          "/stammdaten/hochschreiben?von=1&nach=1", "/auswertungen/kassenbuch", "/auswertungen/erinnerungen?stichtag=2027-01-01",
          "/auswertungen/ueberzahlungen", "/ignoriert", "/verwaltung/protokoll", "/verwaltung/benutzer",
          "/verwaltung/konten", "/verwaltung/konten?id=1", "/verwaltung/anonymisierung", "/suche?q=45,00",
          "/suche?q=KF4A", "/passwort", "/api/projekte/1/posten", "/api/zahlcode?code=kf4a0002"]


@pytest.mark.parametrize("pfad", SEITEN)
def test_seiten_laden(voll, pfad):
    r = voll.get(pfad)
    assert r.status_code == 200, r.get_data(as_text=True)[:500]


def test_exporte(voll):
    for pfad, typ in [("/projekte/1/zahlungsstand?format=csv", "text/csv"),
                      ("/projekte/1/zahlungsstand?format=pdf", "application/pdf"),
                      ("/auswertungen/kassenbuch?format=csv&von=2026-01-01&bis=2026-12-31", "text/csv"),
                      ("/auswertungen/kassenbuch?format=pdf&von=2026-01-01&bis=2026-12-31", "application/pdf"),
                      ("/projekte/1/jahresabschluss.pdf", "application/pdf"),
                      ("/auswertungen/erinnerungen?stichtag=2027-01-01&format=csv", "text/csv")]:
        r = voll.get(pfad)
        assert r.status_code == 200 and r.mimetype == typ, pfad
    csv = voll.get("/projekte/1/zahlungsstand?format=csv").get_data(as_text=True)
    assert "Köhler" not in csv  # BuT child never in exports
    kb = voll.get("/auswertungen/kassenbuch?format=csv&von=2026-01-01&bis=2026-12-31").get_data(as_text=True)
    assert "Anfangssaldo" in kb and "1250,00" in kb and "Endsaldo" in kb


def test_brief_und_erinnerung_pdf(voll):
    r = voll.post("/projekte/1/briefe", data={"modus": "offen", "text": "Liebe Eltern"})
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    r = voll.post("/auswertungen/erinnerungen/briefe", data={"projekt": "1", "stichtag": "2027-01-01"})
    assert r.status_code == 200 and r.mimetype == "application/pdf"


def test_workflow_ueber_oberflaeche(voll, demo):
    v = demo.one("SELECT v.id, v.bankumsatz_id FROM vorschlag v WHERE regel='R3'")
    r = voll.post(f"/vorschlag/{v['id']}/annehmen", follow_redirects=True)
    assert "Vorschlag übernommen" in r.get_data(as_text=True)
    kopie = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KOPIE'")
    u = demo.one("SELECT id FROM bankumsatz WHERE verwendungszweck='Kopiergeld' ORDER BY id LIMIT 1")
    r = voll.post(f"/umsaetze/{u['id']}/zuordnen", data={"projekt_id": [str(kopie)], "posten_id": [""],
                                                          "betrag": ["5,00"]}, follow_redirects=True)
    assert "1 Zuordnung(en) gespeichert" in r.get_data(as_text=True)
    fee = demo.one("SELECT id FROM bankumsatz WHERE betrag_cent=-750")
    voll.post(f"/umsaetze/{fee['id']}/ignorieren", data={"grund": "Kontoführung"})
    assert demo.one("SELECT zuordnungsstatus FROM bankumsatz WHERE id=?", (fee["id"],))[0] == "ignoriert"
    ids = [str(r[0]) for r in demo.all("SELECT id FROM vorschlag")]
    r = voll.post("/vorschlaege/sammel", data={"vorschlag": ids}, follow_redirects=True)
    assert "übernommen" in r.get_data(as_text=True)


def test_import_ueber_oberflaeche(app, demo, admin_id, dateien):
    c = app.test_client()
    login(c)
    r = c.post("/import", data={"datei": (BytesIO(dateien["camt052_teil1.xml"]), "teil1.xml")},
               content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert "Vorschau" in html and demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 0
    token = html.split('name="token" value="')[1].split('"')[0]
    r = c.post("/import/bestaetigen", data={"token": token})
    assert "Import abgeschlossen" in r.get_data(as_text=True)
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 14


def test_csv_import_windows1252(app, db, admin_id):
    from schulkasse.stammdaten import schuljahr_anlegen
    sj = schuljahr_anlegen(db, "2026/27", "2026-08-01", "2027-07-31", admin_id, aktiv=True)
    c = app.test_client()
    login(c)
    daten = KLASSENLISTE.replace(";Familie Becker", ";").replace(";Familie Yilmaz", ";").encode("cp1252")
    r = c.post("/stammdaten/import", data={"schuljahr_id": str(sj), "datei": (BytesIO(daten), "liste.csv")},
               content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert "Schröder" in html and "Geschwister bestätigen" in html
    token = html.split('name="token" value="')[1].split('"')[0]
    c.post("/stammdaten/import", data={"schritt": "bestaetigen", "token": token, "gruppe": ["BECKER"]})
    assert db.scalar("SELECT COUNT(*) FROM kind") == 34
    fam = {r[0] for r in db.all("SELECT familie_id FROM kind WHERE nachname='Becker'")}
    assert len(fam) == 1
    fam = {r[0] for r in db.all("SELECT familie_id FROM kind WHERE nachname='Yilmaz'")}
    assert len(fam) == 2  # not confirmed -> separate families


def test_hochschreiben(app, demo, admin_id):
    from schulkasse.stammdaten import hochschreiben, hochschreiben_vorschau, schuljahr_anlegen
    alt = demo.scalar("SELECT id FROM schuljahr WHERE aktiv=1")
    neu = schuljahr_anlegen(demo, "2027/28", "2027-08-01", "2028-07-31", admin_id)
    v = {z["klasse"]["bezeichnung"]: z for z in hochschreiben_vorschau(demo, alt, neu)}
    assert v["4a"]["aktion"] == "abgang" and v["2b"]["ziel"] == "3b"
    stats = hochschreiben(demo, alt, neu, admin_id)
    assert stats == {"aufgerueckt": 8, "abgang": 26, "unklar": 0}
    assert demo.scalar("SELECT aktiv FROM schuljahr WHERE id=?", (neu,)) == 1
    assert demo.scalar("SELECT COUNT(*) FROM kind k JOIN klasse kl ON kl.id=k.klasse_id WHERE kl.bezeichnung='3b'"
                       " AND kl.schuljahr_id=?", (neu,)) == 8


def test_backup(app, demo, tmp_path):
    import tarfile

    from schulkasse.cli import erstelle_backup
    archiv = erstelle_backup(app, tmp_path / "bk")
    with tarfile.open(archiv) as tar:
        assert "schulkasse.sqlite3" in tar.getnames()


def test_cli_create_admin_und_import(app, demo, admin_id, dateien, tmp_path, monkeypatch):
    pfad = tmp_path / "t1.xml"
    pfad.write_bytes(dateien["camt052_teil1.xml"])
    runner = app.test_cli_runner()
    r = runner.invoke(args=["import-camt", str(pfad), "--ja"])
    assert r.exit_code == 0, r.output
    assert "14 neu" in r.output
    monkeypatch.setattr("getpass.getpass", lambda _p="": "noch-ein-langes-passwort")
    r = runner.invoke(args=["create-admin", "--benutzername", "chef", "--anzeigename", "Chefin"])
    assert r.exit_code == 0, r.output
    assert demo.scalar("SELECT rolle FROM benutzer WHERE benutzername='chef'") == "admin"
    r = runner.invoke(args=["migrate"])
    assert "keine" in r.output


def test_cli_create_admin_fragt_erneut(app, db, monkeypatch):
    eingaben = iter(["zu-kurz", "zu-kurz",                                   # too short
                     "ein-langes-passwort", "ein-anderes-passwort",          # mismatch
                     "ein-langes-passwort", "ein-langes-passwort"])          # valid
    monkeypatch.setattr("getpass.getpass", lambda _p="": next(eingaben))
    r = app.test_cli_runner().invoke(args=["create-admin", "--benutzername", "chefin", "--anzeigename", "Chefin"])
    assert r.exit_code == 0, r.output
    assert "mindestens 12 Zeichen" in r.output and "stimmen nicht überein" in r.output
    assert db.scalar("SELECT COUNT(*) FROM benutzer WHERE benutzername='chefin'") == 1


def test_cli_create_admin_gibt_nach_fuenf_versuchen_auf(app, db, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _p="": "kurz")
    r = app.test_cli_runner().invoke(args=["create-admin", "--benutzername", "chefin", "--anzeigename", "Chefin"])
    assert r.exit_code != 0 and "5 Versuchen" in r.output
    assert db.scalar("SELECT COUNT(*) FROM benutzer") == 0
