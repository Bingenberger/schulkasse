import pytest

from schulkasse import create_app
from schulkasse.auth import hash_passwort
from schulkasse.camt import parse_xml
from schulkasse.db import Database, utcnow
from schulkasse.importer import analysiere, importiere
from schulkasse.testdaten import camt_dateien, demo_anlegen

PASSWORT = "ein-sehr-sicheres-passwort"


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "UPLOAD_DIR": str(tmp_path / "belege"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BRIEF_CONFIG": str(tmp_path / "brief.toml"),
        "SECRET_KEY": "test",
        "TESTING": True,
        "SESSION_COOKIE_SECURE": False,
        "CSRF_DEAKTIVIERT": True,
    })
    return app


@pytest.fixture
def db(app):
    d = Database(app.config["DATABASE"])
    yield d
    d.close()


def benutzer_anlegen(db, name, rolle):
    return db.insert("INSERT INTO benutzer (benutzername, anzeigename, passwort_hash, rolle, aktiv, erstellt_am)"
                     " VALUES (?,?,?,?,1,?)", (name, name.title(), hash_passwort(PASSWORT), rolle, utcnow()))


@pytest.fixture
def admin_id(db):
    return benutzer_anlegen(db, "admin", "admin")


@pytest.fixture
def demo(db, admin_id):
    demo_anlegen(db, admin_id)
    return db


@pytest.fixture
def dateien(demo):
    return camt_dateien(demo)


def importiere_datei(db, daten, name, benutzer_id, schwelle=100):
    analysen = analysiere(db, [parse_xml(daten, name)])
    return importiere(db, analysen, benutzer_id, schwelle)


def login(client, name="admin", passwort=PASSWORT):
    """Complete both factors for the existing application workflow tests."""
    import json
    import pyotp
    from schulkasse import zwei_faktor as mfa
    response = client.post("/anmelden", data={"benutzername": name, "passwort": passwort})
    if response.status_code != 302:
        return response
    app = client.application
    if not hasattr(app, '_test_recovery'):
        app._test_recovery = {}
    if response.location.endswith('/2fa/einrichten'):
        client.get('/2fa/einrichten')
        with client.session_transaction() as s:
            encrypted = s['setup_secret']
        with app.app_context():
            code = pyotp.TOTP(mfa.entschluesseln(encrypted)).now()
        assert client.post('/2fa/einrichten', data={'code': code}).status_code == 302
        with client.session_transaction() as s:
            encrypted = s['codes']
        with app.app_context():
            app._test_recovery[name] = json.loads(mfa.entschluesseln(encrypted))
        return client.post('/2fa/codes', data={'gesichert': '1'})
    return client.post('/2fa', data={'code': app._test_recovery[name].pop()})


def posten_von(db, vorname, nachname, kurzcode=None):
    sql = ("SELECT p.* FROM offener_posten p JOIN kind k ON k.id=p.kind_id JOIN projekt pr ON pr.id=p.projekt_id"
           " WHERE k.vorname=? AND k.nachname=?")
    params = [vorname, nachname]
    if kurzcode:
        sql += " AND pr.kurzcode=?"
        params.append(kurzcode)
    return db.one(sql, params)
