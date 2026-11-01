"""Letter template settings page (texts, logo, preview)."""
import io

import pytest
from PIL import Image
from pypdf import PdfReader

from schulkasse.briefvorlage import wirksam

from .conftest import benutzer_anlegen, login


def _bild(fmt="PNG", groesse=(400, 120)):
    buf = io.BytesIO()
    Image.new("RGB", groesse, (31, 95, 139)).save(buf, format=fmt)
    return buf.getvalue()


def _formular(**werte):
    daten = {"schulname": "GGS Niederkassel", "briefkopf": "Hauptstraße 1\n53859 Niederkassel", "ort": "Niederkassel",
             "standardtext": "Liebe Eltern,\n\nbitte überweisen Sie den Betrag.",
             "hinweis_kostenuebernahme": "Sprechen Sie uns vertraulich an.",
             "hinweis_ohne_qr": "Bitte die Angaben rechts abtippen.", "unterschrift": "Die Schulleitung",
             "aktion": "speichern"}
    daten.update(werte)
    return daten


def _cfg(app, db):
    return wirksam(db, app.config["BRIEF_CONFIG"])[0]


def test_rechte(app, db, admin_id):
    benutzer_anlegen(db, "kasse", "kasse")
    benutzer_anlegen(db, "lehrerin", "lehrkraft")
    for name, status in (("admin", 200), ("kasse", 200), ("lehrerin", 403)):
        c = app.test_client()
        login(c, name)
        assert c.get("/verwaltung/briefvorlage").status_code == status, name


def test_standard_zeigt_platzhalter(app, admin_id):
    c = app.test_client()
    login(c)
    html = c.get("/verwaltung/briefvorlage").get_data(as_text=True)
    assert "Noch nicht angepasst" in html and "Platzhalter" in html


def test_speichern_und_zuruecksetzen(app, db, admin_id):
    c = app.test_client()
    login(c)
    r = c.post("/verwaltung/briefvorlage", data=_formular(), follow_redirects=True)
    assert "Briefvorlage gespeichert" in r.get_data(as_text=True)
    cfg = _cfg(app, db)
    assert cfg["schulname"] == "GGS Niederkassel" and cfg["briefkopf"] == "Hauptstraße 1\n53859 Niederkassel"
    assert "Noch nicht angepasst" not in c.get("/verwaltung/briefvorlage").get_data(as_text=True)
    assert db.one("SELECT * FROM protokoll WHERE aktion='einstellung.briefvorlage'") is not None
    # unchanged form -> nothing to do
    r = c.post("/verwaltung/briefvorlage", data=_formular(), follow_redirects=True)
    assert "Keine Änderungen" in r.get_data(as_text=True)
    # empty field -> back to the default
    c.post("/verwaltung/briefvorlage", data=_formular(unterschrift=""))
    assert _cfg(app, db)["unterschrift"] == "Die Schulleitung"
    assert db.scalar("SELECT COUNT(*) FROM einstellung WHERE schluessel='brief.unterschrift'") == 0


def test_zu_lang(app, db, admin_id):
    c = app.test_client()
    login(c)
    r = c.post("/verwaltung/briefvorlage", data=_formular(ort="x" * 61), follow_redirects=True)
    assert "höchstens 60 Zeichen" in r.get_data(as_text=True)
    assert db.scalar("SELECT COUNT(*) FROM einstellung") == 0


def test_logo_hochladen_verkleinern_entfernen(app, db, admin_id):
    c = app.test_client()
    login(c)
    daten = _formular(logo=(io.BytesIO(_bild(groesse=(3000, 900))), "logo.png"))
    c.post("/verwaltung/briefvorlage", data=daten, content_type="multipart/form-data")
    uri = _cfg(app, db)["logo_uri"]
    assert uri.startswith("data:image/png;base64,")
    import base64
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as bild:
        assert max(bild.size) == 1200
    c.post("/verwaltung/briefvorlage", data=_formular(logo_entfernen="1"))
    assert _cfg(app, db)["logo_uri"] is None


@pytest.mark.parametrize("inhalt,name", [(b"kein bild", "logo.png"), (None, "logo.gif")])
def test_logo_ungueltig(app, db, admin_id, inhalt, name):
    c = app.test_client()
    login(c)
    daten = _formular(logo=(io.BytesIO(inhalt or _bild("GIF")), name))
    r = c.post("/verwaltung/briefvorlage", data=daten, content_type="multipart/form-data", follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "PNG oder JPG" in text
    assert db.scalar("SELECT COUNT(*) FROM einstellung") == 0  # nothing saved, form keeps its values
    assert "GGS Niederkassel" in text


def test_vorschau_ohne_speichern(app, db, admin_id):
    c = app.test_client()
    login(c)
    daten = _formular(aktion="vorschau", schulname='Schule "Am Park"',
                      logo=(io.BytesIO(_bild("JPEG")), "logo.jpg"))
    r = c.post("/verwaltung/briefvorlage", data=daten, content_type="multipart/form-data")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    text = PdfReader(io.BytesIO(r.data)).pages[0].extract_text()
    assert "Max Mustermann" in text and "Sprechen Sie uns vertraulich an." in text and "BSP-0001" in text
    assert db.scalar("SELECT COUNT(*) FROM einstellung") == 0


def test_briefe_nutzen_vorlage(app, demo, admin_id):
    c = app.test_client()
    login(c)
    html = c.get("/projekte/1/briefe").get_data(as_text=True)
    assert "Die Briefvorlage enthält noch Platzhalter" in html
    c.post("/verwaltung/briefvorlage", data=_formular(), follow_redirects=True)
    html = c.get("/projekte/1/briefe").get_data(as_text=True)
    assert "Die Briefvorlage enthält noch Platzhalter" not in html
    r = c.post("/projekte/1/briefe", data={"modus": "offen", "text": "Projekttext"})
    text = PdfReader(io.BytesIO(r.data)).pages[0].extract_text()
    assert "Hauptstraße 1" in text and "Sprechen Sie uns vertraulich an." in text
    # project keeps its own text; the page offers to take over the current standard text
    assert "Aktuellen Standardtext übernehmen" in c.get("/projekte/1/briefe").get_data(as_text=True)


def test_kaputte_brief_toml(app, db, admin_id):
    with open(app.config["BRIEF_CONFIG"], "w", encoding="utf-8") as fh:
        fh.write('schulname = "fehlendes Anführungszeichen\n')
    c = app.test_client()
    login(c)
    html = c.get("/verwaltung/briefvorlage").get_data(as_text=True)
    assert "fehlerhaft und wird ignoriert" in html
    r = c.post("/verwaltung/briefvorlage", data=_formular(aktion="vorschau"))
    assert r.mimetype == "application/pdf"
