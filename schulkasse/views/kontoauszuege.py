"""Bank statements and monitoring lists (Richtlinie Schulgirokonten, § 8)."""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .. import kontoauszug as ka
from ..auth import rolle
from ..briefe import brief_config, html_zu_pdf
from ..buchung import BuchungsFehler
from ..db import get_db, utcnow
from ..geld import BetragFehler, parse_betrag
from ..protokoll import protokolliere
from .util import datum_or_none, int_or_none, pdf_antwort, uid, zurueck_oder

bp = Blueprint("kontoauszuege", __name__, url_prefix="/kontoauszuege")


def _werte(form):
    endsaldo = (form.get("endsaldo") or "").strip()
    return {"nummer": int_or_none(form.get("nummer")), "datum_von": datum_or_none(form.get("datum_von")),
            "datum_bis": datum_or_none(form.get("datum_bis")),
            "endsaldo_cent": parse_betrag(endsaldo) if endsaldo else None, "notiz": form.get("notiz")}


def _schulanschrift(cfg):
    if (cfg.get("schulanschrift") or "").strip():
        return cfg["schulanschrift"]
    zeilen = [z.strip() for z in (cfg.get("briefkopf") or "").splitlines() if z.strip()]
    # The letterhead usually starts with the name, followed by street and town.
    zeilen = [z for z in zeilen if z != cfg.get("schulname") and "PLATZHALTER" not in z]
    return ", ".join(zeilen[:2])


def _liste_mit_status(db, auszug_id):
    ul = ka.ueberwachungsliste(db, auszug_id)
    ul["geaendert"] = bool(ul["auszug"]["geprueft_am"]) and \
        ul["auszug"]["geprueft_hash"] != ka.fingerabdruck(db, auszug_id, ul)
    return ul


def _pdf(listen, dateiname):
    cfg = brief_config()
    html = render_template("pdf/ueberwachungsliste.html", listen=listen, cfg=cfg, erzeugt=utcnow(), user=g.user,
                           schulanschrift=_schulanschrift(cfg))
    return pdf_antwort(dateiname, html_zu_pdf(html))


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(listen, dateiname):
    from ..xlsx_export import ueberwachungslisten_xlsx
    cfg = brief_config()
    return Response(ueberwachungslisten_xlsx(listen, cfg, _schulanschrift(cfg)), mimetype=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{dateiname}"'})


def _auszug(db, auszug_id):
    a = db.one("SELECT * FROM kontoauszug WHERE id=?", (auszug_id,))
    if a is None:
        abort(404)
    return a


@bp.route("/", methods=["GET", "POST"])
@rolle("admin", "kasse")
def uebersicht():
    db = get_db()
    konten = db.all("SELECT * FROM konto ORDER BY aktiv DESC, id")
    konto_id = int_or_none(request.values.get("konto")) or (konten[0]["id"] if konten else None)
    if request.method == "POST" and konto_id:
        try:
            neu = ka.speichern(db, konto_id, _werte(request.form), uid())
            flash("Kontoauszug erfasst.", "ok")
            return redirect(url_for("kontoauszuege.detail", auszug_id=neu))
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
            vorschlag = dict(request.form)
    else:
        vorschlag = ka.vorschlag(db, konto_id) if konto_id else {}
    jahr = int_or_none(request.args.get("jahr"))
    return render_template(
        "kontoauszuege.html", konten=konten, konto_id=konto_id,
        konto=next((k for k in konten if k["id"] == konto_id), None),
        auszuege=ka.liste(db, konto_id, jahr) if konto_id else [], jahre=ka.jahre(db, konto_id) if konto_id else [],
        jahr=jahr, vorschlag=vorschlag, ohne=ka.ohne_auszug(db, konto_id) if konto_id else None)


@bp.route("/<int:auszug_id>")
@rolle("admin", "kasse")
def detail(auszug_id):
    db = get_db()
    _auszug(db, auszug_id)
    ul = _liste_mit_status(db, auszug_id)
    fmt = request.args.get("format")
    if fmt in ("pdf", "xlsx"):
        protokolliere(db, f"export.ueberwachungsliste.{fmt}", "kontoauszug", auszug_id)
    dateiname = f"Ueberwachungsliste_{ul['auszug']['jahr']}-{ul['auszug']['nummer']:03d}_{ul['konto']['bezeichnung']}"
    dateiname = "".join(c if c.isalnum() or c in "-_" else "_" for c in dateiname)
    if fmt == "pdf":
        return _pdf([ul], dateiname + ".pdf")
    if fmt == "xlsx":
        return _xlsx([ul], dateiname + ".xlsx")
    vorher = db.one("SELECT id FROM kontoauszug WHERE konto_id=? AND datum_bis < ? ORDER BY datum_bis DESC LIMIT 1",
                    (ul["konto"]["id"], ul["auszug"]["datum_von"]))
    nachher = db.one("SELECT id FROM kontoauszug WHERE konto_id=? AND datum_von > ? ORDER BY datum_von LIMIT 1",
                     (ul["konto"]["id"], ul["auszug"]["datum_bis"]))
    return render_template("ueberwachungsliste.html", ul=ul, vorher=vorher, nachher=nachher)


@bp.route("/<int:auszug_id>/bearbeiten", methods=["GET", "POST"])
@rolle("admin", "kasse")
def bearbeiten(auszug_id):
    db = get_db()
    a = _auszug(db, auszug_id)
    werte = dict(a)
    if request.method == "POST":
        try:
            ka.speichern(db, a["konto_id"], _werte(request.form), uid(), auszug_id=auszug_id)
            flash("Kontoauszug gespeichert.", "ok")
            return redirect(url_for("kontoauszuege.detail", auszug_id=auszug_id))
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
            werte = dict(request.form)
    konto = db.one("SELECT * FROM konto WHERE id=?", (a["konto_id"],))
    return render_template("kontoauszug_form.html", a=a, werte=werte, konto=konto)


@bp.route("/<int:auszug_id>/loeschen", methods=["POST"])
@rolle("admin", "kasse")
def loeschen(auszug_id):
    db = get_db()
    try:
        a = ka.loeschen(db, auszug_id, uid())
        flash(f"Kontoauszug {ka.bezeichnung(a)} gelöscht.", "ok")
        return redirect(url_for("kontoauszuege.uebersicht", konto=a["konto_id"]))
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
        return redirect(url_for("kontoauszuege.detail", auszug_id=auszug_id))


@bp.route("/<int:auszug_id>/pruefen", methods=["POST"])
@rolle("admin", "kasse")
def pruefen(auszug_id):
    db = get_db()
    try:
        if request.form.get("aufheben"):
            ka.pruefung_aufheben(db, auszug_id, request.form.get("grund"), uid())
            flash("Prüfvermerk aufgehoben.", "ok")
        else:
            ka.pruefung_vermerken(db, auszug_id, uid())
            flash("Prüfung vermerkt. Bitte die ausgedruckte Liste mit Datum unterschreiben.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("kontoauszuege.detail", auszug_id=auszug_id))


@bp.route("/umsatz/<int:umsatz_id>/anmerkung", methods=["POST"])
@rolle("admin", "kasse")
def anmerkung(umsatz_id):
    db = get_db()
    try:
        if ka.anmerkung_setzen(db, umsatz_id, request.form.get("anmerkung"), uid()):
            flash("Anmerkung gespeichert.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return zurueck_oder(url_for("umsaetze.detail", umsatz_id=umsatz_id))


@bp.route("/sammel.pdf", defaults={"fmt": "pdf"}, endpoint="sammel_pdf")
@bp.route("/sammel.xlsx", defaults={"fmt": "xlsx"}, endpoint="sammel_xlsx")
@rolle("admin", "kasse")
def sammel(fmt):
    """All monitoring lists of one account and year in one file (PDF, or one worksheet each), oldest first."""
    db = get_db()
    konto_id = int_or_none(request.args.get("konto"))
    jahr = int_or_none(request.args.get("jahr"))
    ids = [r["id"] for r in db.all("SELECT id FROM kontoauszug WHERE konto_id=? AND (? IS NULL OR jahr=?)"
                                   " ORDER BY datum_von", (konto_id, jahr, jahr))]
    if not ids:
        flash("Für die Auswahl sind keine Kontoauszüge erfasst.", "fehler")
        return redirect(url_for("kontoauszuege.uebersicht", konto=konto_id or ""))
    protokolliere(db, f"export.ueberwachungsliste.sammel.{fmt}", "konto", konto_id,
                  neu={"jahr": jahr, "anzahl": len(ids)})
    listen = [_liste_mit_status(db, i) for i in ids]
    if fmt == "xlsx":
        return _xlsx(listen, f"Ueberwachungslisten_{jahr or 'alle'}.xlsx")
    return _pdf(listen, f"Ueberwachungslisten_{jahr or 'alle'}.pdf")
