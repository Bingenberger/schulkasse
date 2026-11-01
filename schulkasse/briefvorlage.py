"""Letter template settings (texts and logo), editable in the web interface.

Precedence per field: value stored in the database > instance/brief.toml >
built-in default. The logo is stored in the database as well, so it is part
of the regular backups.
"""
import base64
import io
import logging
import mimetypes
from pathlib import Path

from .buchung import BuchungsFehler
from .config import PLATZHALTER_BRIEF, load_brief_config
from .db import utcnow
from .protokoll import protokolliere

log = logging.getLogger(__name__)

# key -> (label, help text, max length, multiline)
FELDER = {
    "schulname": ("Name der Schule", "Oben links im Brief, falls kein Logo hinterlegt ist, und in der Fußzeile "
                  "der PDF-Listen.", 120, False),
    "briefkopf": ("Briefkopf", "Oben rechts: Anschrift, Telefon, E-Mail – eine Angabe pro Zeile.", 600, True),
    "ort": ("Ort vor dem Datum", "Zum Beispiel „Niederkassel“ – ergibt „Niederkassel, 19.09.2026“.", 60, False),
    "standardtext": ("Standardtext des Elternbriefs", "Vorlage für Projekte ohne eigenen Brieftext. Den Text je "
                     "Projekt passen Sie beim Erzeugen der Elternbriefe an.", 5000, True),
    "hinweis_kostenuebernahme": ("Hinweis zur Kostenübernahme", "Kurzer, nicht stigmatisierender Satz, an wen sich "
                                 "Familien wenden können, wenn die Zahlung schwerfällt.", 800, True),
    "hinweis_ohne_qr": ("Hinweis für Eltern ohne QR-fähige Banking-App", "Steht unter dem QR-Code.", 800, True),
    "unterschrift": ("Grußzeile / Unterschrift", "Zum Beispiel „Die Schulleitung“.", 120, False),
}
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MAX_PX = 1200


def _gespeichert(db):
    return {r["schluessel"]: r["wert"] for r in db.all("SELECT schluessel, wert FROM einstellung"
                                                       " WHERE schluessel LIKE 'brief.%'")}


def datei_werte(pfad):
    """Values from brief.toml (or defaults); never raises on a broken file."""
    try:
        return load_brief_config(pfad), None
    except Exception as exc:  # broken TOML must not stop letter generation
        log.warning("brief.toml konnte nicht gelesen werden: %s", exc)
        return dict(PLATZHALTER_BRIEF), f"Die Datei {pfad} ist fehlerhaft und wird ignoriert ({exc})."


def wirksam(db, pfad):
    """Effective letter settings plus per-field origin ('app', 'datei')."""
    basis, _ = datei_werte(pfad)
    gespeichert = _gespeichert(db)
    cfg, herkunft = dict(basis), {}
    for key in FELDER:
        wert = gespeichert.get(f"brief.{key}")
        if wert is not None:
            cfg[key], herkunft[key] = wert, "app"
        else:
            herkunft[key] = "datei"
    cfg["logo_uri"] = None
    if gespeichert.get("brief.logo"):
        cfg["logo_uri"] = gespeichert["brief.logo"]
        herkunft["logo"] = "app"
    elif basis.get("logo"):
        # Logo file named in brief.toml (path relative to that file).
        datei = Path(basis["logo"])
        if not datei.is_absolute():
            datei = Path(pfad).parent / datei
        if datei.is_file():
            mime = mimetypes.guess_type(str(datei))[0] or "image/png"
            cfg["logo_uri"] = f"data:{mime};base64," + base64.b64encode(datei.read_bytes()).decode("ascii")
            herkunft["logo"] = "datei"
    return vervollstaendige(cfg), herkunft


def vervollstaendige(cfg):
    # Used inside a CSS string in the PDF footer: no quotes or backslashes there.
    cfg["fusszeile"] = cfg.get("schulname", "").replace('"', "").replace("\\", "")
    return cfg


def platzhalter(cfg):
    """Fields that still contain a [PLATZHALTER ...] marker."""
    return [key for key in FELDER if "[PLATZHALTER" in (cfg.get(key) or "")]


def pruefe_werte(werte):
    geprueft = {}
    for key, (label, _hilfe, maxlen, mehrzeilig) in FELDER.items():
        wert = (werte.get(key) or "").replace("\r\n", "\n").strip()
        if not mehrzeilig:
            wert = " ".join(wert.split())
        if len(wert) > maxlen:
            raise BuchungsFehler(f"„{label}“ darf höchstens {maxlen} Zeichen lang sein.")
        geprueft[key] = wert
    return geprueft


def logo_data_uri(daten):
    """Validate an uploaded logo (PNG/JPEG), shrink large images, return a data URI."""
    if not daten:
        raise BuchungsFehler("Die Logo-Datei ist leer.")
    if len(daten) > LOGO_MAX_BYTES:
        raise BuchungsFehler("Das Logo darf höchstens 2 MB groß sein.")
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(daten)) as bild:
            bild.load()
            if bild.format not in ("PNG", "JPEG"):
                raise BuchungsFehler("Bitte ein Logo im Format PNG oder JPG hochladen.")
            if max(bild.size) > LOGO_MAX_PX:
                bild.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX))
            buf = io.BytesIO()
            fmt = bild.format or "PNG"
            if fmt == "JPEG" and bild.mode not in ("RGB", "L"):
                bild = bild.convert("RGB")
            # Re-encoding also strips metadata and anything that is not image data.
            bild.save(buf, format=fmt)
    except (UnidentifiedImageError, OSError) as exc:
        raise BuchungsFehler("Die Datei ist kein gültiges Bild (PNG oder JPG).") from exc
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def speichern(db, werte, benutzer_id, logo=None, logo_entfernen=False):
    """Store the form values. Empty fields fall back to brief.toml/defaults."""
    geprueft = pruefe_werte(werte)
    neues_logo = logo_data_uri(logo) if logo else None
    jetzt = utcnow()
    with db.tx():
        alt = _gespeichert(db)
        aenderungen = {}
        for key, wert in geprueft.items():
            schluessel = f"brief.{key}"
            if wert:
                if alt.get(schluessel) != wert:
                    db.execute("INSERT INTO einstellung (schluessel, wert, geaendert_von, geaendert_am)"
                               " VALUES (?,?,?,?) ON CONFLICT(schluessel) DO UPDATE SET wert=excluded.wert,"
                               " geaendert_von=excluded.geaendert_von, geaendert_am=excluded.geaendert_am",
                               (schluessel, wert, benutzer_id, jetzt))
                    aenderungen[key] = wert
            elif schluessel in alt:
                db.execute("DELETE FROM einstellung WHERE schluessel=?", (schluessel,))
                aenderungen[key] = None
        if neues_logo:
            db.execute("INSERT INTO einstellung (schluessel, wert, geaendert_von, geaendert_am) VALUES (?,?,?,?)"
                       " ON CONFLICT(schluessel) DO UPDATE SET wert=excluded.wert,"
                       " geaendert_von=excluded.geaendert_von, geaendert_am=excluded.geaendert_am",
                       ("brief.logo", neues_logo, benutzer_id, jetzt))
            aenderungen["logo"] = "neu hochgeladen"
        elif logo_entfernen and "brief.logo" in alt:
            db.execute("DELETE FROM einstellung WHERE schluessel='brief.logo'")
            aenderungen["logo"] = "entfernt"
        if aenderungen:
            protokolliere(db, "einstellung.briefvorlage", "einstellung", None,
                          alt={k: alt.get(f"brief.{k}") for k in aenderungen if k != "logo"}, neu=aenderungen,
                          benutzer_id=benutzer_id)
    return aenderungen
