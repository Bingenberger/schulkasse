"""Authentication, sessions, roles and CSRF (F10)."""
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import abort, current_app, g, redirect, request, session, url_for
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

from .db import Database, utcnow

ROLLEN = {"admin": "Administration", "kasse": "Kasse", "lehrkraft": "Lehrkraft"}
MIN_PASSWORT = 12
SPERRE_VERSUCHE = 10
SPERRE_MINUTEN = 15

_ph = PasswordHasher()  # Argon2id by default


class AuthFehler(ValueError):
    pass


def hash_passwort(passwort):
    if len(passwort or "") < MIN_PASSWORT:
        raise AuthFehler(f"Das Passwort muss mindestens {MIN_PASSWORT} Zeichen lang sein.")
    return _ph.hash(passwort)


def pruefe_passwort(hash_, passwort):
    try:
        return _ph.verify(hash_, passwort)
    except (VerificationError, InvalidHashError):
        return False


# ------------------------------------------------------- server-side sessions

class DbSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, neu=False):
        def on_update(s):
            s.modified = True
        super().__init__(initial, on_update)
        self.sid = sid
        self.new = neu
        self.modified = False


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class DbSessionInterface(SessionInterface):
    """Sessions stored in the database, cookie carries only a random id."""

    def _db(self, app):
        return Database(app.config["DATABASE"])

    def open_session(self, app, request):
        sid = request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session"))
        if sid:
            db = self._db(app)
            try:
                grenze = _ts(datetime.now(timezone.utc) - timedelta(seconds=app.config["SESSION_TIMEOUT_SEKUNDEN"]))
                if secrets.randbelow(100) == 0:
                    db.execute("DELETE FROM sitzung WHERE zuletzt_aktiv < ?", (grenze,))
                row = db.one("SELECT daten, zuletzt_aktiv FROM sitzung WHERE id=?", (sid,))
                if row:
                    # F10.2: automatic logout after 8 hours of inactivity.
                    if row["zuletzt_aktiv"] >= grenze:
                        return DbSession(json.loads(row["daten"]), sid=sid)
                    db.execute("DELETE FROM sitzung WHERE id=?", (sid,))
            finally:
                db.close()
        return DbSession(sid=secrets.token_urlsafe(32), neu=True)

    def save_session(self, app, session, response):
        name = app.config.get("SESSION_COOKIE_NAME", "session")
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        if not session and session.new:
            return  # nothing stored, nothing to delete: do not even open (or create) the database
        db = self._db(app)
        try:
            if not session:
                if not session.new:
                    db.execute("DELETE FROM sitzung WHERE id=?", (session.sid,))
                    response.delete_cookie(name, domain=domain, path=path)
                return
            if session.get("_neu_id"):
                # Session fixation protection after login.
                db.execute("DELETE FROM sitzung WHERE id=?", (session.sid,))
                session.sid = secrets.token_urlsafe(32)
                session.pop("_neu_id")
            db.execute("INSERT INTO sitzung (id, daten, zuletzt_aktiv) VALUES (?,?,?)"
                       " ON CONFLICT(id) DO UPDATE SET daten=excluded.daten, zuletzt_aktiv=excluded.zuletzt_aktiv",
                       (session.sid, json.dumps(dict(session)), utcnow()))
        finally:
            db.close()
        response.set_cookie(name, session.sid, httponly=True, secure=app.config["SESSION_COOKIE_SECURE"],
                            samesite="Lax", domain=domain, path=path)


# ----------------------------------------------------------------- lockout

def gesperrt(db, benutzername, ip):
    """F10.3: 10 failures within 15 minutes, per IP and per account."""
    grenze = _ts(datetime.now(timezone.utc) - timedelta(minutes=SPERRE_MINUTEN))
    for spalte, wert in (("ip", ip), ("benutzername", benutzername.lower())):
        letzter_erfolg = db.scalar(f"SELECT MAX(zeitpunkt) FROM login_versuch WHERE {spalte}=? AND erfolgreich=1",
                                   (wert,), "")
        n = db.scalar(f"SELECT COUNT(*) FROM login_versuch WHERE {spalte}=? AND erfolgreich=0"
                      " AND zeitpunkt >= ? AND zeitpunkt > ?", (wert, grenze, letzter_erfolg), 0)
        if n >= SPERRE_VERSUCHE:
            return True
    return False


def anmelden(db, benutzername, passwort, ip):
    benutzername = (benutzername or "").strip()
    if gesperrt(db, benutzername, ip):
        raise AuthFehler(f"Zu viele Fehlversuche. Die Anmeldung ist für {SPERRE_MINUTEN} Minuten gesperrt.")
    user = db.one("SELECT * FROM benutzer WHERE lower(benutzername)=lower(?)", (benutzername,))
    ok = user is not None and user["aktiv"] and pruefe_passwort(user["passwort_hash"], passwort or "")
    db.execute("INSERT INTO login_versuch (zeitpunkt, benutzername, ip, erfolgreich) VALUES (?,?,?,?)",
               (utcnow(), benutzername.lower(), ip, 1 if ok else 0))
    db.execute("DELETE FROM login_versuch WHERE zeitpunkt < ?",
               (_ts(datetime.now(timezone.utc) - timedelta(days=30)),))
    if not ok:
        raise AuthFehler("Benutzername oder Passwort ist falsch.")
    if _ph.check_needs_rehash(user["passwort_hash"]):
        db.execute("UPDATE benutzer SET passwort_hash=? WHERE id=?", (_ph.hash(passwort), user["id"]))
    return user


# ------------------------------------------------------------ decorators

def login_erforderlich(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)
    return wrapper


def rolle(*rollen):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
            if g.user["rolle"] not in rollen:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


def ist_kasse():
    user = g.get("user")
    return user is not None and user["rolle"] in ("admin", "kasse")


def darf_projekt_sehen(projekt):
    user = g.get("user")
    if user is None:
        return False
    if user["rolle"] in ("admin", "kasse"):
        return True
    return projekt["verantwortlich_user_id"] == user["id"]


def pruefe_projekt(projekt):
    if projekt is None:
        abort(404)
    if not darf_projekt_sehen(projekt):
        abort(403)


# ------------------------------------------------------------------- CSRF

def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def pruefe_csrf():
    """F10.4: every state-changing request needs a valid token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if current_app.config.get("CSRF_DEAKTIVIERT"):
        return
    gesendet = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    erwartet = session.get("_csrf")
    if not erwartet or not gesendet or not secrets.compare_digest(gesendet, erwartet):
        abort(400, description="Das Formular ist abgelaufen. Bitte die Seite neu laden und erneut absenden.")
