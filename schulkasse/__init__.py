"""Schulkasse - contribution and cash book management for GGS Niederkassel."""
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, g, render_template, request, session
from markupsafe import Markup

from . import auth
from .config import load_config
from .db import Database, close_db, get_db, migrate, schema_status
from .geld import format_cent
from .textnorm import iban_formatiert

BERLIN = ZoneInfo("Europe/Berlin")
log = logging.getLogger(__name__)


def _datum(wert):
    if not wert:
        return ""
    s = str(wert)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return s


def _zeit(wert):
    """UTC timestamp from the database -> local time."""
    if not wert:
        return ""
    try:
        dt = datetime.strptime(str(wert)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(wert)
    return dt.astimezone(BERLIN).strftime("%d.%m.%Y %H:%M")


def _euro(cent):
    return Markup(format_cent(cent).replace(" €", "&nbsp;€")) if cent is not None else ""


class TrustedProxyFix:
    """X-Forwarded-For/-Proto only from the configured proxy addresses; other peers stay unchanged."""

    def __init__(self, wsgi_app, proxies):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.plain = wsgi_app
        self.fixed = ProxyFix(wsgi_app, x_for=1, x_proto=1)
        self.proxies = proxies

    def __call__(self, environ, start_response):
        target = self.fixed if environ.get("REMOTE_ADDR") in self.proxies else self.plain
        return target(environ, start_response)


def create_app(overrides=None):
    app = Flask(__name__, instance_relative_config=False)
    app.config.update(load_config(overrides))
    if not app.config["SECRET_KEY"]:
        if not app.config.get("TESTING"):
            raise RuntimeError("SCHULKASSE_SECRET_KEY muss dauerhaft gesetzt sein (mindestens 32 Zeichen).")
        app.config["SECRET_KEY"] = secrets.token_hex(32)
    if not app.config.get("TESTING") and len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SCHULKASSE_SECRET_KEY muss mindestens 32 Zeichen lang sein.")
    if app.config.get("PROXY_IPS"):
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, app.config["PROXY_IPS"])
    elif app.config.get("HINTER_PROXY") and not app.config.get("TESTING"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.session_interface = auth.DbSessionInterface()

    if app.config.get("AUTO_MIGRATE", True):
        db = Database(app.config["DATABASE"])
        try:
            migrate(db)
        finally:
            db.close()

    app.teardown_appcontext(close_db)
    app.jinja_env.filters.update(euro=_euro, datum=_datum, zeit=_zeit, iban=iban_formatiert)
    # Globals (not context processors) so that imported macros can use them too.
    app.jinja_env.globals.update(ROLLEN=auth.ROLLEN, format_cent=format_cent, csrf_token=auth.csrf_token)

    @app.get("/health/live")
    def health_live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready():
        try:
            db = Database(app.config["DATABASE"], create=False)
        except (OSError, sqlite3.Error):
            return {"status": "not-ready"}, 503
        try:
            ready = schema_status(db)[0]
        except sqlite3.Error:
            ready = False
        finally:
            db.close()
        return ({"status": "ready"}, 200) if ready else ({"status": "not-ready"}, 503)

    @app.before_request
    def lade_benutzer():
        g.user = None
        if request.endpoint in ("health_live", "health_ready"):
            return None
        uid = session.get("user_id")
        if uid:
            user = get_db().one("SELECT * FROM benutzer WHERE id=?", (uid,))
            if (user is None or not user["aktiv"] or not user["totp_secret"]
                    or not session.get("mfa_ok") or session.get("auth_version") != user["auth_version"]):
                session.clear()
            else:
                g.user = user
        auth.pruefe_csrf()

    @app.after_request
    def sicherheits_header(resp):
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
                                " frame-ancestors 'none'; form-action 'self'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if g.get("user") is not None or request.blueprint == "auth":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def kontext():
        return {"csrf_token": auth.csrf_token, "ist_kasse": auth.ist_kasse(), "aktueller_user": g.get("user")}

    @app.errorhandler(400)
    def fehler_400(e):
        return render_template("fehler.html", code=400, titel="Ungültige Anfrage",
                               text=getattr(e, "description", "")), 400

    @app.errorhandler(403)
    def fehler_403(_e):
        return render_template("fehler.html", code=403, titel="Keine Berechtigung",
                               text="Für diese Seite fehlt Ihnen die Berechtigung."), 403

    @app.errorhandler(404)
    def fehler_404(_e):
        return render_template("fehler.html", code=404, titel="Nicht gefunden",
                               text="Die angeforderte Seite oder der Datensatz existiert nicht."), 404

    @app.errorhandler(413)
    def fehler_413(_e):
        return render_template("fehler.html", code=413, titel="Datei zu groß",
                               text="Die hochgeladene Datei überschreitet die zulässige Größe."), 413

    from .views import register
    register(app)

    from .cli import register_cli
    register_cli(app)
    return app
