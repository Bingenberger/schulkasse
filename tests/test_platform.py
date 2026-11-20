"""Plattformbetrieb: Migration nur explizit, Readiness, Proxy-Vertrauen, Schlüssel als Datei."""
import sqlite3

import pytest

from schulkasse import create_app
from schulkasse.config import load_config
from schulkasse.db import Database, migrate


def container_app(tmp_path, **extra):
    return create_app({
        "DATABASE": str(tmp_path / "data" / "schulkasse.sqlite3"),
        "UPLOAD_DIR": str(tmp_path / "data" / "belege"),
        "BACKUP_DIR": str(tmp_path / "data" / "backups"),
        "BRIEF_CONFIG": str(tmp_path / "data" / "brief.toml"),
        "SECRET_KEY": "k" * 40, "AUTO_MIGRATE": False, **extra})


def run_migrate(app):
    return app.test_cli_runner().invoke(args=["migrate"])


def test_no_schema_on_start_and_not_ready_until_migrated(tmp_path):
    app = container_app(tmp_path)
    client = app.test_client()
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    assert not (tmp_path / "data" / "schulkasse.sqlite3").exists()
    result = run_migrate(app)
    assert result.exit_code == 0, result.output
    assert client.get("/health/ready").status_code == 200
    assert oct((tmp_path / "data" / "schulkasse.sqlite3").stat().st_mode & 0o777) == "0o600"
    assert "keine (aktuell)" in run_migrate(app).output


def test_newer_database_is_refused(tmp_path):
    app = container_app(tmp_path)
    run_migrate(app)
    db = Database(app.config["DATABASE"])
    db.execute("INSERT INTO schema_migration VALUES ('0099_zukunft', '2030-01-01 00:00:00')")
    with pytest.raises(RuntimeError):
        migrate(db)
    db.close()
    assert app.test_client().get("/health/ready").status_code == 503
    assert run_migrate(app).exit_code != 0


def test_forwarded_headers_only_from_configured_proxy(tmp_path):
    app = container_app(tmp_path, PROXY_IPS=frozenset({"10.231.13.2"}))
    seen = {}

    @app.get("/_ip")
    def _ip():
        from flask import request
        seen["ip"], seen["scheme"] = request.remote_addr, request.scheme
        return "ok"

    client = app.test_client()
    client.get("/_ip", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"},
               environ_base={"REMOTE_ADDR": "10.231.13.2"})
    assert seen == {"ip": "203.0.113.7", "scheme": "https"}
    client.get("/_ip", headers={"X-Forwarded-For": "203.0.113.7"}, environ_base={"REMOTE_ADDR": "10.9.9.9"})
    assert seen["ip"] == "10.9.9.9"


def test_secret_key_from_file_and_exclusive(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_text("s" * 40 + "\n")
    monkeypatch.delenv("SCHULKASSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SCHULKASSE_SECRET_KEY_FILE", str(key))
    assert load_config()["SECRET_KEY"] == "s" * 40
    monkeypatch.setenv("SCHULKASSE_SECRET_KEY", "t" * 40)
    with pytest.raises(RuntimeError):
        load_config()
    monkeypatch.delenv("SCHULKASSE_SECRET_KEY")
    monkeypatch.setenv("SCHULKASSE_PROXY_IPS", "*")
    with pytest.raises(ValueError):
        load_config()


def test_legacy_native_install_still_migrates_on_start(tmp_path):
    app = container_app(tmp_path, AUTO_MIGRATE=True)
    assert app.test_client().get("/health/ready").status_code == 200
    con = sqlite3.connect(tmp_path / "data" / "schulkasse.sqlite3")
    assert con.execute("SELECT count(*) FROM schema_migration").fetchone()[0] >= 9
