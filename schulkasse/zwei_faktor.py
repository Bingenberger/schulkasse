"""Mandatory TOTP, encrypted secrets, atomic replay protection and recovery."""
import base64
import hashlib
import json
import re
import secrets
import time

import pyotp
from cryptography.fernet import Fernet
from flask import current_app

from .auth import AuthFehler

ANMELDEFRIST = 600


def verschluesseln(text):
    return _cipher().encrypt(text.encode()).decode()


def entschluesseln(text):
    return _cipher().decrypt(text.encode()).decode()


def _cipher():
    # Domain-separated key derived from the persistent, random app secret.
    key = hashlib.sha256(b'schulkasse-mfa-v1\0' + current_app.config['SECRET_KEY'].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def passender_schritt(secret, code, letzter=-1):
    code = (code or '').strip()
    if not re.fullmatch(r'[0-9]{6}', code):
        return None
    jetzt = int(time.time()) // 30
    totp = pyotp.TOTP(secret)
    for schritt in (jetzt, jetzt - 1, jetzt + 1):
        if schritt > letzter and secrets.compare_digest(totp.at(schritt * 30), code):
            return schritt
    return None


def code_hash(code):
    code = (code or '').strip().replace('-', '').replace(' ', '').upper()
    return hashlib.sha256(code.encode()).hexdigest()


def neue_recovery_codes(db, uid):
    codes = []
    db.execute('DELETE FROM recovery_code WHERE benutzer_id=?', (uid,))
    for _ in range(10):
        raw = secrets.token_hex(10).upper()
        code = '-'.join(raw[i:i+5] for i in range(0, 20, 5))
        db.execute('INSERT INTO recovery_code VALUES (?,?)', (uid, code_hash(code)))
        codes.append(code)
    return verschluesseln(json.dumps(codes))


def pruefe_sperre(db, uid, ip):
    grenze = int(time.time()) - 900
    db.execute('DELETE FROM mfa_versuch WHERE zeitpunkt < ?', (grenze,))
    for spalte, wert in (('benutzer_id', uid), ('ip', ip)):
        if db.scalar(f'SELECT COUNT(*) FROM mfa_versuch WHERE {spalte}=? AND zeitpunkt>=?',
                     (wert, grenze), 0) >= 5:
            raise AuthFehler('Zu viele Fehlversuche. Bitte nach 15 Minuten erneut versuchen.')


def fehlversuch(db, uid, ip):
    db.execute('INSERT INTO mfa_versuch VALUES (?,?,?)', (uid, ip, int(time.time())))


def faktor_verbrauchen(db, user, code):
    """Caller holds db.tx(): consume a TOTP step or a single recovery code."""
    if not user['totp_secret']:
        return False
    schritt = passender_schritt(entschluesseln(user['totp_secret']), code, user['totp_last'])
    if schritt is not None:
        return db.execute('UPDATE benutzer SET totp_last=? WHERE id=? AND totp_last<?',
                          (schritt, user['id'], schritt)).rowcount == 1
    return db.execute('DELETE FROM recovery_code WHERE benutzer_id=? AND code_hash=?',
                      (user['id'], code_hash(code))).rowcount == 1


def sitzungen_widerrufen(db, uid):
    db.execute('UPDATE benutzer SET auth_version=auth_version+1 WHERE id=?', (uid,))
    db.execute("DELETE FROM sitzung WHERE json_extract(daten, '$.user_id')=?"
               " OR json_extract(daten, '$.preauth_id')=?", (uid, uid))
