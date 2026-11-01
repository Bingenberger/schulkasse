"""Letters (F7) and PDF exports."""
from datetime import date, timedelta
from pathlib import Path

from flask import current_app, render_template

from . import epc
from .buchung import BuchungsFehler
from .db import utcnow
from .projekte import KLASSEN_SQL
from .protokoll import protokolliere


def html_zu_pdf(html):
    from weasyprint import HTML
    return HTML(string=html, base_url=str(Path(current_app.root_path) / "static")).write_pdf()


def brief_config():
    """Effective letter settings: web interface > instance/brief.toml > defaults."""
    from .briefvorlage import wirksam
    from .db import get_db
    cfg, _ = wirksam(get_db(), current_app.config["BRIEF_CONFIG"])
    return cfg


def beispielbrief(db, cfg):
    """One sample page with dummy data, used as preview of the letter template."""
    konto = db.one("SELECT * FROM konto WHERE aktiv=1 ORDER BY id LIMIT 1")
    if konto is None:
        konto = {"inhaber": "Beispiel-Schulkonto", "iban": "DE89370400440532013000", "bic": ""}
    projekt = {"bezeichnung": "Klassenfahrt 4a (Beispiel)"}
    posten = {"vorname": "Max", "nachname": "Mustermann", "klasse": "4a", "zahlcode": "BSP-0001",
              "faellig_am": (date.today() + timedelta(days=21)).isoformat()}
    zweck = epc.verwendungszweck("BSP-0001", projekt["bezeichnung"], "Max", "Mustermann")
    try:
        payload = epc.epc_payload(konto["inhaber"], konto["iban"], 4500, zweck, konto["bic"] or "")
    except epc.QRFehler as exc:
        raise BuchungsFehler(f"QR-Code für die Vorschau nicht möglich: {exc}") from exc
    seiten = [{"posten": posten, "betrag": 4500, "zweck": zweck, "qr": epc.qr_svg_data_uri(payload),
               "payload": payload, "konto": konto}]
    html = render_template("pdf/brief.html", projekt=projekt, seiten=seiten, cfg=cfg, text=cfg["standardtext"],
                           erinnerung=False, heute=date.today())
    return html_zu_pdf(html)


def projekt_konto(db, projekt):
    konto = None
    if projekt["konto_id"]:
        konto = db.one("SELECT * FROM konto WHERE id=?", (projekt["konto_id"],))
    if konto is None:
        konto = db.one("SELECT * FROM konto WHERE aktiv=1 ORDER BY id LIMIT 1")
    if konto is None:
        raise BuchungsFehler("Es ist noch kein Konto angelegt. Bitte zuerst unter Verwaltung ein Konto erfassen.")
    return konto


def posten_fuer_brief(db, projekt_id, modus, posten_ids=None, erinnerung=False):
    """F7.3: all children, only open items, or a manual selection."""
    sql = ("SELECT p.*, k.vorname, k.nachname, k.but_status, kl.bezeichnung AS klasse,"
           " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
           "   AND z.storniert=0) AS bezahlt"
           " FROM offener_posten p JOIN kind k ON k.id=p.kind_id JOIN klasse kl ON kl.id=k.klasse_id"
           " WHERE p.projekt_id=? AND p.richtung='eingang' AND p.zahlcode IS NOT NULL"
           " AND p.status NOT IN ('storniert','erlassen') AND k.anonymisiert=0")
    params = [projekt_id]
    if modus == "offen" or erinnerung:
        sql += " AND p.status IN ('offen','teilweise')"
    if erinnerung:
        sql += " AND k.but_status=0"
    if modus == "auswahl":
        ids = [int(i) for i in (posten_ids or [])]
        if not ids:
            raise BuchungsFehler("Bitte mindestens einen Posten auswählen.")
        sql += f" AND p.id IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    return db.all(sql + " ORDER BY kl.bezeichnung, k.nachname, k.vorname", params)


def seiten(db, projekt, posten, erinnerung=False):
    konto = projekt_konto(db, projekt)
    ergebnis = []
    for p in posten:
        betrag = p["sollbetrag_cent"] - p["bezahlt"] if erinnerung else p["sollbetrag_cent"]
        if betrag <= 0:
            continue
        zweck = epc.verwendungszweck(p["zahlcode"], projekt["bezeichnung"], p["vorname"], p["nachname"])
        payload = epc.epc_payload(konto["inhaber"], konto["iban"], betrag, zweck, konto["bic"] or "")
        ergebnis.append({"posten": p, "betrag": betrag, "zweck": zweck, "qr": epc.qr_svg_data_uri(payload),
                         "payload": payload, "konto": konto})
    return ergebnis


def serienbrief(db, projekt_id, modus, benutzer_id, posten_ids=None, text=None, erinnerung=False):
    """Returns (pdf bytes, number of pages). Records the letter date (F7.4)."""
    projekt = db.one("SELECT pr.*, " + KLASSEN_SQL + " AS klasse FROM projekt pr"
                     " WHERE pr.id=?", (projekt_id,))
    if projekt is None:
        raise BuchungsFehler("Das Projekt existiert nicht.")
    if projekt["typ"] != "elternbeitrag":
        raise BuchungsFehler("Serienbriefe gibt es nur für Projekte vom Typ Elternbeitrag.")
    posten = posten_fuer_brief(db, projekt_id, modus, posten_ids, erinnerung)
    s = seiten(db, projekt, posten, erinnerung)
    if not s:
        raise BuchungsFehler("Für die Auswahl gibt es keine Posten, für die ein Brief erzeugt werden kann.")
    cfg = brief_config()
    if text is None:
        text = projekt["brieftext"] or cfg["standardtext"]
    html = render_template("pdf/brief.html", projekt=projekt, seiten=s, cfg=cfg, text=text,
                           erinnerung=erinnerung, heute=date.today())
    pdf = html_zu_pdf(html)
    jetzt = utcnow()
    with db.tx():
        if text is not None and not erinnerung and text != projekt["brieftext"]:
            db.execute("UPDATE projekt SET brieftext=? WHERE id=?", (text, projekt_id))
        db.executemany("UPDATE offener_posten SET letzter_brief_am=? WHERE id=?",
                       [(jetzt, x["posten"]["id"]) for x in s])
        protokolliere(db, "brief.erinnerung" if erinnerung else "brief.erzeugen", "projekt", projekt_id,
                      neu={"anzahl": len(s), "modus": modus, "zahlcodes": [x["posten"]["zahlcode"] for x in s]},
                      benutzer_id=benutzer_id)
    return pdf, len(s)
