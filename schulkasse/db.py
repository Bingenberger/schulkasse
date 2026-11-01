"""SQLite access, transactions and migrations."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow():
    """UTC timestamp string as stored in the database."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Thin wrapper around sqlite3 with nestable transactions."""

    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def executemany(self, sql, seq):
        return self.conn.executemany(sql, seq)

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql, params=(), default=None):
        row = self.conn.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]

    def insert(self, sql, params=()):
        return self.conn.execute(sql, params).lastrowid

    @contextmanager
    def tx(self):
        """Transaction; nested calls join the outer transaction."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    def close(self):
        self.conn.close()


def migrate(db):
    """Apply numbered SQL migrations idempotently. Returns applied versions."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " version TEXT PRIMARY KEY, angewendet_am TIMESTAMP NOT NULL)"
    )
    done = {r[0] for r in db.all("SELECT version FROM schema_migration")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        script = (
            "BEGIN;\n" + sql + "\n"
            f"INSERT INTO schema_migration (version, angewendet_am) VALUES ('{version}', '{utcnow()}');\n"
            "COMMIT;"
        )
        try:
            db.conn.executescript(script)
        except Exception:
            if db.conn.in_transaction:
                db.conn.execute("ROLLBACK")
            raise
        applied.append(version)
    db.execute("PRAGMA foreign_keys = ON")
    return applied


def get_db():
    if "db" not in g:
        g.db = Database(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
