"""Configuration from environment variables / .env file (F12.1)."""
import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

PLATZHALTER_BRIEF = {
    "schulname": "[PLATZHALTER: Name der Schule]",
    "briefkopf": "[PLATZHALTER: Briefkopf, Anschrift, Kontaktdaten]",
    "logo": "",
    "ort": "Niederkassel",
    "standardtext": "[PLATZHALTER: Standardtext des Elternbriefs]",
    "hinweis_kostenuebernahme": "[PLATZHALTER: Hinweistext zur Kostenübernahme bei finanzieller Belastung]",
    "hinweis_ohne_qr": (
        "Ihre Banking-App kann keine QR-Codes lesen? Dann überweisen Sie bitte mit den "
        "Angaben rechts neben dem Code. Wichtig: Übernehmen Sie den Verwendungszweck "
        "unverändert, besonders den Zahlcode am Anfang."
    ),
    "unterschrift": "Die Schulleitung",
}


def _bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "ja", "yes", "on")


def load_config(overrides=None):
    load_dotenv(BASE_DIR / ".env")
    env = os.environ
    cfg = {
        "DATABASE": env.get("SCHULKASSE_DB", str(BASE_DIR / "instance" / "schulkasse.sqlite3")),
        "SECRET_KEY": env.get("SCHULKASSE_SECRET_KEY", ""),
        "UPLOAD_DIR": env.get("SCHULKASSE_UPLOAD_DIR", str(BASE_DIR / "instance" / "belege")),
        "BACKUP_DIR": env.get("SCHULKASSE_BACKUP_DIR", str(BASE_DIR / "instance" / "backups")),
        "MAX_CONTENT_LENGTH": int(env.get("SCHULKASSE_MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        "MATCHING_SCHWELLE": int(env.get("SCHULKASSE_MATCHING_SCHWELLE", "100")),
        "ANONYMISIERUNG_FRIST_TAGE": int(env.get("SCHULKASSE_ANONYMISIERUNG_FRIST_TAGE", "730")),
        "BRIEF_CONFIG": env.get("SCHULKASSE_BRIEF_CONFIG", str(BASE_DIR / "instance" / "brief.toml")),
        "SESSION_COOKIE_SECURE": _bool(env.get("SCHULKASSE_COOKIE_SECURE"), True),
        # Behind nginx: take client IP/scheme from X-Forwarded-* (needed for the IP lockout).
        "HINTER_PROXY": _bool(env.get("SCHULKASSE_HINTER_PROXY"), True),
        "SESSION_TIMEOUT_SEKUNDEN": 8 * 3600,
        "TESTING": False,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def load_brief_config(path):
    """Letter settings (placeholders from Lastenheft section 16)."""
    cfg = dict(PLATZHALTER_BRIEF)
    p = Path(path)
    if p.is_file():
        with p.open("rb") as fh:
            data = tomllib.load(fh)
        cfg.update({k: v for k, v in data.items() if isinstance(v, str)})
    return cfg
