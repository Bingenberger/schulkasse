import base64
import json
import time

import pyotp
import pytest

from schulkasse import zwei_faktor as mfa
from .conftest import PASSWORT, login


def passwort_login(c):
    return c.post('/anmelden', data={'benutzername': 'admin', 'passwort': PASSWORT})


def einrichten(c):
    assert c.get('/2fa/einrichten').status_code == 200
    with c.session_transaction() as s:
        encrypted = s['setup_secret']
    with c.application.app_context():
        secret = mfa.entschluesseln(encrypted)
    code = pyotp.TOTP(secret).now()
    assert c.post('/2fa/einrichten', data={'code': code}).location.endswith('/2fa/codes')
    with c.session_transaction() as s:
        encrypted_codes = s['codes']
    with c.application.app_context():
        codes = json.loads(mfa.entschluesseln(encrypted_codes))
    return secret, code, codes


@pytest.mark.parametrize('zeit,code', [(59, '287082'), (1111111109, '081804'),
                                     (1111111111, '050471'), (1234567890, '005924'),
                                     (2000000000, '279037'), (20000000000, '353130')])
def test_rfc6238_vektoren(monkeypatch, zeit, code):
    secret = base64.b32encode(b'12345678901234567890').decode()
    monkeypatch.setattr(mfa.time, 'time', lambda: zeit)
    assert mfa.passender_schritt(secret, code) == zeit // 30
    assert mfa.passender_schritt(secret, code, zeit // 30) is None


def test_einrichtung_pflicht_und_verschluesselung(app, admin_id, db):
    c = app.test_client()
    assert passwort_login(c).location.endswith('/2fa/einrichten')
    for pfad in ['/', '/projekte', '/verwaltung/benutzer', '/api/projekte/1/posten']:
        assert c.get(pfad).status_code == 302
    assert c.get('/2fa/codes').location.endswith('/2fa')
    secret, code, codes = einrichten(c)
    assert c.get('/').status_code == 302  # codes not yet acknowledged
    page = c.get('/2fa/codes')
    assert page.headers['Cache-Control'] == 'no-store'
    assert all(x in page.get_data(as_text=True) for x in codes)
    assert c.post('/2fa/codes', data={'gesichert': '1'}).status_code == 302
    assert c.get('/').status_code == 200
    assert db.scalar('SELECT totp_secret FROM benutzer WHERE id=?', (admin_id,)) != secret
    assert db.scalar('SELECT COUNT(*) FROM recovery_code') == 10
    persisted = ' '.join(r[0] for r in db.all('SELECT daten FROM sitzung'))
    assert secret not in persisted and all(x not in persisted for x in codes)
    audit = str([dict(r) for r in db.all('SELECT * FROM protokoll')])
    assert secret not in audit and all(x not in audit for x in codes)


def test_totp_und_recovery_nur_einmal(app, admin_id, db, monkeypatch):
    c = app.test_client()
    passwort_login(c)
    secret, used, codes = einrichten(c)
    c.post('/2fa/codes', data={'gesichert': '1'})
    c.post('/abmelden')
    assert passwort_login(c).location.endswith('/2fa')
    assert c.post('/2fa', data={'code': used}).status_code == 401
    now = time.time() + 60
    monkeypatch.setattr(mfa.time, 'time', lambda: now)
    assert c.post('/2fa', data={'code': pyotp.TOTP(secret).at(now)}).status_code == 302
    assert c.get('/').status_code == 200
    other = app.test_client()
    passwort_login(other)
    assert other.post('/2fa', data={'code': codes[0]}).status_code == 302
    third = app.test_client()
    passwort_login(third)
    assert third.post('/2fa', data={'code': codes[0]}).status_code == 401
    assert db.scalar('SELECT COUNT(*) FROM recovery_code') == 9


def test_sperre_bleibt_nach_neuer_passworteingabe(app, admin_id):
    c = app.test_client()
    login(c)
    for _ in range(5):
        passwort_login(c)
        assert c.post('/2fa', data={'code': 'ungueltig'}).status_code == 401
    passwort_login(c)
    assert c.post('/2fa', data={'code': 'ungueltig'}).status_code == 429


def test_abgelaufener_schritt_und_alte_sitzung(app, admin_id):
    c = app.test_client()
    passwort_login(c)
    with c.session_transaction() as s:
        s['preauth_bis'] = 0
    assert c.get('/2fa/einrichten').location.endswith('/anmelden')
    with c.session_transaction() as s:
        s['user_id'] = admin_id  # password-only legacy session
    assert c.get('/').status_code == 302


def test_passwort_reset_widerruft_voranmeldung(app, db, admin_id):
    c = app.test_client()
    passwort_login(c)
    with db.tx():
        mfa.sitzungen_widerrufen(db, admin_id)
    assert c.get('/2fa/einrichten').location.endswith('/anmelden')


def test_cli_reset_erzwingt_neue_einrichtung(app, admin_id, db):
    c = app.test_client()
    login(c)
    result = app.test_cli_runner().invoke(args=['reset-2fa', 'admin'], input='y\n')
    assert result.exit_code == 0, result.output
    assert db.scalar('SELECT totp_secret FROM benutzer WHERE id=?', (admin_id,)) is None
    assert db.scalar('SELECT COUNT(*) FROM recovery_code') == 0
    assert c.get('/').status_code == 302
    assert passwort_login(c).location.endswith('/2fa/einrichten')


def test_sicherheit_codes_erneuern_und_app_wechseln(app, admin_id):
    c = app.test_client()
    login(c)
    codes = app._test_recovery['admin']
    assert c.get('/sicherheit').status_code == 200
    assert c.post('/sicherheit', data={'passwort': PASSWORT, 'code': codes.pop(), 'aktion': 'codes'}).location.endswith('/2fa/codes')
    with c.session_transaction() as s:
        encrypted = s['codes']
    with app.app_context():
        new_codes = json.loads(mfa.entschluesseln(encrypted))
    c.post('/2fa/codes', data={'gesichert': '1'})
    assert c.post('/sicherheit', data={'passwort': PASSWORT, 'code': new_codes[0], 'aktion': 'wechseln'}).location.endswith('/2fa/einrichten')
    einrichten(c)
    assert c.post('/2fa/codes', data={'gesichert': '1'}).status_code == 302
    assert c.get('/').status_code == 200


def test_csrf_auch_zweiter_faktor(app, admin_id):
    c = app.test_client()
    passwort_login(c)
    app.config['CSRF_DEAKTIVIERT'] = False
    assert c.post('/2fa/einrichten', data={'code': '123456'}).status_code == 400
    assert c.post('/2fa', data={'code': '123456'}).status_code == 400
    assert c.post('/2fa/codes', data={'gesichert': '1'}).status_code == 400


def test_einrichtung_mit_echtem_csrf(app, admin_id):
    app.config['CSRF_DEAKTIVIERT'] = False
    c = app.test_client()

    def token(path):
        response = c.get(path)
        assert response.status_code == 200
        return response.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"')[0]

    r = c.post('/anmelden', data={'benutzername': 'admin', 'passwort': PASSWORT,
                                  'csrf_token': token('/anmelden')})
    assert r.location.endswith('/2fa/einrichten')
    csrf = token('/2fa/einrichten')
    assert c.post('/verwaltung/benutzer', data={'csrf_token': csrf}).status_code == 302
    with c.session_transaction() as s:
        encrypted = s['setup_secret']
    with app.app_context():
        code = pyotp.TOTP(mfa.entschluesseln(encrypted)).now()
    r = c.post('/2fa/einrichten', data={'code': code, 'csrf_token': csrf})
    assert r.location.endswith('/2fa/codes')
    assert c.post('/2fa/codes', data={'gesichert': '1', 'csrf_token': token('/2fa/codes')}).status_code == 302
    assert c.get('/').status_code == 200


def test_recovery_parallel_nur_einmal(app, admin_id):
    from concurrent.futures import ThreadPoolExecutor
    from schulkasse.db import Database
    c = app.test_client()
    login(c)
    code = app._test_recovery['admin'][0]

    def verwenden():
        with app.app_context():
            db = Database(app.config['DATABASE'])
            try:
                with db.tx():
                    user = db.one('SELECT * FROM benutzer WHERE id=?', (admin_id,))
                    return mfa.faktor_verbrauchen(db, user, code)
            finally:
                db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: verwenden(), range(2)))
    assert sorted(results) == [False, True]


def test_abgelehnter_cli_reset_erhaelt_zugang(app, admin_id, db):
    c = app.test_client()
    login(c)
    secret = db.scalar('SELECT totp_secret FROM benutzer WHERE id=?', (admin_id,))
    result = app.test_cli_runner().invoke(args=['reset-2fa', 'admin'], input='n\n')
    assert result.exit_code != 0
    assert db.scalar('SELECT totp_secret FROM benutzer WHERE id=?', (admin_id,)) == secret
    assert c.get('/').status_code == 200
