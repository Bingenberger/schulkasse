"""Login and logout."""
import json
import time

import pyotp
from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from ..auth import AuthFehler, anmelden, hash_passwort, login_erforderlich, pruefe_passwort
from ..db import get_db, utcnow
from ..protokoll import protokolliere
from .. import zwei_faktor as mfa
from ..epc import qr_svg_data_uri

bp = Blueprint("auth", __name__)


def _sicheres_ziel(ziel):
    if ziel and ziel.startswith("/") and not ziel.startswith("//") and "\\" not in ziel:
        return ziel
    return url_for("start.dashboard")


@bp.route("/anmelden", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        try:
            user = anmelden(db, request.form.get("benutzername"), request.form.get("passwort"), request.remote_addr)
        except AuthFehler as exc:
            flash(str(exc), "fehler")
            return render_template("login.html", benutzername=request.form.get("benutzername", "")), 401
        _voranmelden(user, request.args.get("next"))
        return redirect(url_for("auth.zweiter_faktor" if user["totp_secret"] else "auth.einrichten"))
    return render_template("login.html", benutzername="")


def _voranmelden(user, ziel=None, wechseln=False):
    session.clear()
    session.update(preauth_id=user['id'], preauth_version=user['auth_version'],
                   preauth_bis=time.time() + mfa.ANMELDEFRIST, ziel=_sicheres_ziel(ziel),
                   wechseln=wechseln, _neu_id=True)


def _wartender_benutzer(db):
    user = db.one('SELECT * FROM benutzer WHERE id=?', (session.get('preauth_id'),))
    if (user is None or not user['aktiv'] or time.time() > session.get('preauth_bis', 0)
            or user['auth_version'] != session.get('preauth_version')):
        session.clear()
        return None
    return user


def _abschliessen(db, user):
    ziel = _sicheres_ziel(session.get('ziel'))
    session.clear()
    session.update(user_id=user['id'], mfa_ok=True, auth_version=user['auth_version'], _neu_id=True)
    db.execute('UPDATE benutzer SET letzter_login=? WHERE id=?', (utcnow(), user['id']))
    protokolliere(db, 'benutzer.anmelden', 'benutzer', user['id'], benutzer_id=user['id'])
    return redirect(ziel)


@bp.route('/2fa', methods=['GET', 'POST'])
def zweiter_faktor():
    db = get_db()
    with db.tx():
        user = _wartender_benutzer(db)
        if user is None:
            return redirect(url_for('auth.login'))
        if not user['totp_secret']:
            return redirect(url_for('auth.einrichten'))
        if request.method == 'POST':
            try:
                mfa.pruefe_sperre(db, user['id'], request.remote_addr)
            except AuthFehler as exc:
                flash(str(exc), 'fehler')
                return render_template('zwei_faktor.html', modus='pruefen'), 429
            if mfa.faktor_verbrauchen(db, user, request.form.get('code')):
                return _abschliessen(db, user)
            mfa.fehlversuch(db, user['id'], request.remote_addr)
            flash('Code ungültig oder bereits verwendet. Bitte den nächsten Code abwarten.', 'fehler')
            return render_template('zwei_faktor.html', modus='pruefen'), 401
    return render_template('zwei_faktor.html', modus='pruefen')


@bp.route('/2fa/einrichten', methods=['GET', 'POST'])
def einrichten():
    db = get_db()
    with db.tx():
        user = _wartender_benutzer(db)
        if user is None:
            return redirect(url_for('auth.login'))
        if session.get('codes'):
            return redirect(url_for('auth.recovery_codes'))
        if user['totp_secret'] and not session.get('wechseln'):
            return redirect(url_for('auth.zweiter_faktor'))
        if not session.get('setup_secret'):
            session['setup_secret'] = mfa.verschluesseln(pyotp.random_base32())
        secret = mfa.entschluesseln(session['setup_secret'])
        uri = pyotp.TOTP(secret).provisioning_uri(user['benutzername'], issuer_name='Schulkasse')
        status = 200
        if request.method == 'POST':
            try:
                mfa.pruefe_sperre(db, user['id'], request.remote_addr)
            except AuthFehler as exc:
                flash(str(exc), 'fehler')
                return render_template('zwei_faktor.html', modus='gesperrt'), 429
            schritt = mfa.passender_schritt(secret, request.form.get('code'))
            if schritt is not None:
                db.execute('UPDATE benutzer SET totp_secret=?, totp_last=? WHERE id=?',
                           (session.pop('setup_secret'), schritt, user['id']))
                mfa.sitzungen_widerrufen(db, user['id'])
                session['preauth_version'] = user['auth_version'] + 1
                session['codes'] = mfa.neue_recovery_codes(db, user['id'])
                session['wechseln'] = False
                session['_neu_id'] = True
                protokolliere(db, 'benutzer.2fa_einrichten', 'benutzer', user['id'], benutzer_id=user['id'])
                return redirect(url_for('auth.recovery_codes'))
            mfa.fehlversuch(db, user['id'], request.remote_addr)
            flash('Code ungültig. Bitte prüfen Sie die Uhrzeit Ihrer Authenticator-App.', 'fehler')
            status = 401
    return render_template('zwei_faktor.html', modus='einrichten', secret=secret,
                           qr=qr_svg_data_uri(uri)), status


@bp.route('/2fa/codes', methods=['GET', 'POST'])
def recovery_codes():
    db = get_db()
    with db.tx():
        user = _wartender_benutzer(db)
        if user is None:
            return redirect(url_for('auth.login'))
        if not session.get('codes'):
            return redirect(url_for('auth.zweiter_faktor'))
        if request.method == 'POST' and request.form.get('gesichert') == '1':
            return _abschliessen(db, user)
        return render_template('zwei_faktor.html', modus='codes',
                               codes=json.loads(mfa.entschluesseln(session['codes'])))


@bp.route('/sicherheit', methods=['GET', 'POST'])
@login_erforderlich
def sicherheit():
    db = get_db()
    if request.method == 'POST':
        with db.tx():
            user = db.one('SELECT * FROM benutzer WHERE id=?', (g.user['id'],))
            if user['auth_version'] != session.get('auth_version'):
                session.clear()
                return redirect(url_for('auth.login'))
            try:
                mfa.pruefe_sperre(db, user['id'], request.remote_addr)
            except AuthFehler as exc:
                flash(str(exc), 'fehler')
                return render_template('zwei_faktor.html', modus='verwalten'), 429
            if (pruefe_passwort(user['passwort_hash'], request.form.get('passwort', ''))
                    and mfa.faktor_verbrauchen(db, user, request.form.get('code'))):
                if request.form.get('aktion') == 'wechseln':
                    _voranmelden(user, wechseln=True)
                    return redirect(url_for('auth.einrichten'))
                codes = mfa.neue_recovery_codes(db, user['id'])
                mfa.sitzungen_widerrufen(db, user['id'])
                _voranmelden(db.one('SELECT * FROM benutzer WHERE id=?', (user['id'],)))
                session['codes'] = codes
                protokolliere(db, 'benutzer.2fa_recovery', 'benutzer', user['id'], benutzer_id=user['id'])
                return redirect(url_for('auth.recovery_codes'))
            mfa.fehlversuch(db, user['id'], request.remote_addr)
            flash('Passwort oder Sicherheitscode ungültig.', 'fehler')
            return render_template('zwei_faktor.html', modus='verwalten'), 401
    return render_template('zwei_faktor.html', modus='verwalten')


@bp.route("/abmelden", methods=["POST"])
def logout():
    session.clear()
    flash("Sie wurden abgemeldet.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/passwort", methods=["GET", "POST"])
@login_erforderlich
def passwort():
    from flask import g
    if request.method == "POST":
        db = get_db()
        if not pruefe_passwort(g.user["passwort_hash"], request.form.get("alt", "")):
            flash("Das bisherige Passwort ist falsch.", "fehler")
        elif request.form.get("neu") != request.form.get("neu2"):
            flash("Die neuen Passwörter stimmen nicht überein.", "fehler")
        else:
            try:
                h = hash_passwort(request.form.get("neu"))
            except AuthFehler as exc:
                flash(str(exc), "fehler")
            else:
                with db.tx():
                    db.execute("UPDATE benutzer SET passwort_hash=? WHERE id=?", (h, g.user["id"]))
                    mfa.sitzungen_widerrufen(db, g.user["id"])
                    protokolliere(db, "benutzer.passwort", "benutzer", g.user["id"])
                session["_neu_id"] = True
                session['auth_version'] = g.user['auth_version'] + 1
                flash("Das Passwort wurde geändert.", "ok")
                return redirect(url_for("start.dashboard"))
    return render_template("passwort.html")
