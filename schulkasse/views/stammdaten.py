"""Master data views (F9)."""
import json
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..auth import rolle
from ..buchung import BuchungsFehler
from ..db import get_db, utcnow
from ..protokoll import protokolliere
from ..stammdaten import (StammdatenFehler, aktives_schuljahr, csv_importieren, csv_vorschau,
                          hochschreiben, hochschreiben_vorschau, kind_aendern, kind_anlegen, kind_loeschen,
                          klasse_holen_oder_anlegen, lies_klassenliste, schuljahr_aktivieren, schuljahr_anlegen)
from .util import int_or_none, uid

bp = Blueprint("stammdaten", __name__, url_prefix="/stammdaten")


@bp.route("/schuljahre", methods=["GET", "POST"])
@rolle("admin", "kasse")
def schuljahre():
    db = get_db()
    if request.method == "POST":
        try:
            schuljahr_anlegen(db, request.form.get("bezeichnung"), request.form.get("beginn"),
                              request.form.get("ende"), uid(), aktiv=request.form.get("aktiv") == "1")
            flash("Schuljahr angelegt.", "ok")
            return redirect(url_for("stammdaten.schuljahre"))
        except StammdatenFehler as exc:
            flash(str(exc), "fehler")
    rows = db.all("SELECT sj.*, (SELECT COUNT(*) FROM klasse WHERE schuljahr_id=sj.id) AS klassen,"
                  " (SELECT COUNT(*) FROM kind k JOIN klasse kl ON kl.id=k.klasse_id WHERE kl.schuljahr_id=sj.id"
                  "  AND k.aktiv=1) AS kinder FROM schuljahr sj ORDER BY sj.beginn DESC")
    return render_template("schuljahre.html", schuljahre=rows)


@bp.route("/schuljahre/<int:sj_id>/aktivieren", methods=["POST"])
@rolle("admin", "kasse")
def schuljahr_aktiv(sj_id):
    schuljahr_aktivieren(get_db(), sj_id, uid())
    flash("Aktives Schuljahr gewechselt.", "ok")
    return redirect(url_for("stammdaten.schuljahre"))


@bp.route("/klassen", methods=["GET", "POST"])
@rolle("admin", "kasse")
def klassen():
    db = get_db()
    sj = aktives_schuljahr(db)
    sj_id = int_or_none(request.values.get("schuljahr")) or (sj["id"] if sj else None)
    if request.method == "POST":
        bez = (request.form.get("bezeichnung") or "").strip()
        if not bez or not sj_id:
            flash("Bitte Schuljahr und Bezeichnung angeben.", "fehler")
        else:
            with db.tx():
                klasse_holen_oder_anlegen(db, sj_id, bez, uid())
            flash("Klasse gespeichert.", "ok")
        return redirect(url_for("stammdaten.klassen", schuljahr=sj_id))
    rows = db.all("SELECT kl.*, (SELECT COUNT(*) FROM kind k WHERE k.klasse_id=kl.id AND k.aktiv=1) AS kinder,"
                  " (SELECT COUNT(*) FROM kind k WHERE k.klasse_id=kl.id AND k.aktiv=1 AND k.but_status=1) AS but"
                  " FROM klasse kl WHERE kl.schuljahr_id=? ORDER BY kl.bezeichnung", (sj_id or 0,))
    return render_template("klassen.html", klassen=rows, sj_id=sj_id,
                           schuljahre=db.all("SELECT * FROM schuljahr ORDER BY beginn DESC"))


@bp.route("/kinder")
@rolle("admin", "kasse")
def kinder():
    db = get_db()
    sj = aktives_schuljahr(db)
    sql = ("SELECT k.*, kl.bezeichnung AS klasse, f.bezeichnung AS familie,"
           " (SELECT COUNT(*) FROM kind k2 WHERE k2.familie_id=k.familie_id) AS geschwister"
           " FROM kind k JOIN klasse kl ON kl.id=k.klasse_id LEFT JOIN familie f ON f.id=k.familie_id WHERE 1=1")
    params = []
    klasse_id = int_or_none(request.args.get("klasse"))
    if klasse_id:
        sql += " AND k.klasse_id=?"
        params.append(klasse_id)
    elif sj and request.args.get("alle") != "1":
        sql += " AND kl.schuljahr_id=? AND k.aktiv=1"
        params.append(sj["id"])
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (k.vorname LIKE ? OR k.nachname LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if request.args.get("but") == "1":
        sql += " AND k.but_status=1"
    rows = db.all(sql + " ORDER BY kl.bezeichnung, k.nachname, k.vorname LIMIT 1000", params)
    klassen = db.all("SELECT kl.*, sj.bezeichnung AS schuljahr FROM klasse kl JOIN schuljahr sj ON sj.id=kl.schuljahr_id"
                     " ORDER BY sj.beginn DESC, kl.bezeichnung")
    return render_template("kinder.html", kinder=rows, klassen=klassen, args=request.args)


def _klassen_familien(db):
    return (db.all("SELECT kl.*, sj.bezeichnung AS schuljahr FROM klasse kl JOIN schuljahr sj ON sj.id=kl.schuljahr_id"
                   " ORDER BY sj.beginn DESC, kl.bezeichnung"),
            db.all("SELECT f.*, (SELECT group_concat(vorname, ', ') FROM kind WHERE familie_id=f.id) AS kinder"
                   " FROM familie f WHERE f.bezeichnung NOT LIKE 'Gelöscht #%' ORDER BY f.bezeichnung"))


@bp.route("/kinder/neu", methods=["GET", "POST"])
@rolle("admin", "kasse")
def kind_neu():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            familie = f.get("familie_id")
            kid = kind_anlegen(db, int(f.get("klasse_id") or 0), f.get("vorname"), f.get("nachname"), uid(),
                               familie_id=int(familie) if familie and familie.isdigit() else None,
                               but_status=f.get("but_status") == "1", but_notiz=f.get("but_notiz") or None)
            flash("Kind angelegt.", "ok")
            return redirect(url_for("stammdaten.kind", kind_id=kid))
        except (StammdatenFehler, ValueError) as exc:
            flash(str(exc) if isinstance(exc, StammdatenFehler) else "Bitte eine Klasse wählen.", "fehler")
    klassen, familien = _klassen_familien(db)
    return render_template("kind_form.html", k=None, werte=request.form, klassen=klassen, familien=familien)


@bp.route("/kinder/<int:kind_id>", methods=["GET", "POST"])
@rolle("admin", "kasse")
def kind(kind_id):
    db = get_db()
    k = db.one("SELECT k.*, kl.bezeichnung AS klasse FROM kind k JOIN klasse kl ON kl.id=k.klasse_id WHERE k.id=?",
               (kind_id,))
    if k is None:
        abort(404)
    if request.method == "POST":
        try:
            # F9.3: BuT status only by kasse/admin (this view is restricted to them).
            kind_aendern(db, kind_id, request.form, uid(), darf_but=True)
            flash("Gespeichert.", "ok")
            return redirect(url_for("stammdaten.kind", kind_id=kind_id))
        except StammdatenFehler as exc:
            flash(str(exc), "fehler")
    posten = db.all("SELECT p.*, pr.bezeichnung AS projekt, pr.kurzcode,"
                    " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
                    "  AND z.storniert=0) AS bezahlt FROM offener_posten p JOIN projekt pr ON pr.id=p.projekt_id"
                    " WHERE p.kind_id=? ORDER BY p.erstellt_am DESC", (kind_id,))
    geschwister = db.all("SELECT k.*, kl.bezeichnung AS klasse FROM kind k JOIN klasse kl ON kl.id=k.klasse_id"
                         " WHERE k.familie_id=? AND k.id<>?", (k["familie_id"], kind_id))
    ibans = db.all("SELECT * FROM zahler_iban WHERE familie_id=? ORDER BY treffer_anzahl DESC", (k["familie_id"],))
    klassen, familien = _klassen_familien(db)
    return render_template("kind_form.html", k=k, werte=request.form or k, klassen=klassen, familien=familien,
                           posten=posten, geschwister=geschwister, ibans=ibans)


@bp.route("/kinder/<int:kind_id>/loeschen", methods=["POST"])
@rolle("admin", "kasse")
def kind_entfernen(kind_id):
    try:
        kind_loeschen(get_db(), kind_id, uid())
        flash("Kind gelöscht.", "ok")
        return redirect(url_for("stammdaten.kinder"))
    except StammdatenFehler as exc:
        flash(str(exc), "fehler")
        return redirect(url_for("stammdaten.kind", kind_id=kind_id))


@bp.route("/zahler-iban/<int:zid>/loeschen", methods=["POST"])
@rolle("admin", "kasse")
def zahler_iban_loeschen(zid):
    db = get_db()
    z = db.one("SELECT * FROM zahler_iban WHERE id=?", (zid,))
    if z is None:
        abort(404)
    with db.tx():
        db.execute("DELETE FROM zahler_iban WHERE id=?", (zid,))
        protokolliere(db, "zahler_iban.loeschen", "zahler_iban", zid, alt=z)
    flash("Gelernte IBAN entfernt.", "ok")
    return redirect(request.form.get("zurueck") or url_for("stammdaten.kinder"))


@bp.route("/import", methods=["GET", "POST"])
@rolle("admin", "kasse")
def csv_import():
    """F9.1: class lists as CSV with preview and sibling confirmation."""
    db = get_db()
    schuljahre = db.all("SELECT * FROM schuljahr ORDER BY beginn DESC")
    if request.method == "POST" and request.form.get("schritt") == "bestaetigen":
        v = db.one("SELECT * FROM import_vorschau WHERE token=? AND benutzer_id=? AND art='csv'",
                   (request.form.get("token"), uid()))
        if v is None:
            flash("Die Vorschau ist abgelaufen. Bitte die Datei erneut hochladen.", "fehler")
            return redirect(url_for("stammdaten.csv_import"))
        meta = json.loads(v["dateiname"])
        try:
            zeilen = lies_klassenliste(v["daten"])
            angelegt, dup = csv_importieren(db, zeilen, meta["schuljahr_id"], request.form.getlist("gruppe"), uid())
        except StammdatenFehler as exc:
            flash(str(exc), "fehler")
            return redirect(url_for("stammdaten.csv_import"))
        db.execute("DELETE FROM import_vorschau WHERE token=?", (v["token"],))
        flash(f"{angelegt} Kinder angelegt, {dup} bereits vorhanden und übersprungen.", "ok")
        return redirect(url_for("stammdaten.kinder"))
    if request.method == "POST":
        datei = request.files.get("datei")
        sj_id = int_or_none(request.form.get("schuljahr_id"))
        if not datei or not datei.filename or not sj_id:
            flash("Bitte Schuljahr und Datei auswählen.", "fehler")
            return redirect(url_for("stammdaten.csv_import"))
        daten = datei.read()
        try:
            zeilen = lies_klassenliste(daten)
        except StammdatenFehler as exc:
            flash(str(exc), "fehler")
            return redirect(url_for("stammdaten.csv_import"))
        token = secrets.token_urlsafe(24)
        db.execute("INSERT INTO import_vorschau (token, benutzer_id, art, dateiname, daten, erstellt_am)"
                   " VALUES (?,?,?,?,?,?)",
                   (token, uid(), "csv", json.dumps({"name": datei.filename, "schuljahr_id": sj_id}), daten, utcnow()))
        return render_template("csv_vorschau.html", v=csv_vorschau(db, zeilen, sj_id), token=token,
                               schuljahr=db.one("SELECT * FROM schuljahr WHERE id=?", (sj_id,)))
    sj = aktives_schuljahr(db)
    return render_template("csv_import.html", schuljahre=schuljahre, sj_id=sj["id"] if sj else None)


@bp.route("/hochschreiben", methods=["GET", "POST"])
@rolle("admin", "kasse")
def hochschreiben_view():
    """F9.2: school year change with preview."""
    db = get_db()
    schuljahre = db.all("SELECT * FROM schuljahr ORDER BY beginn DESC")
    von = int_or_none(request.values.get("von"))
    nach = int_or_none(request.values.get("nach"))
    vorschau = None
    if von and nach:
        try:
            vorschau = hochschreiben_vorschau(db, von, nach)
        except BuchungsFehler as exc:
            flash(str(exc), "fehler")
    if request.method == "POST" and vorschau is not None and request.form.get("bestaetigt") == "1":
        stats = hochschreiben(db, von, nach, uid())
        flash(f"{stats['aufgerueckt']} Kinder aufgerückt, {stats['abgang']} als Abgänger inaktiv gesetzt, "
              f"{stats['unklar']} nicht zugeordnet. Neue Klassen 1 bitte per CSV importieren.", "ok")
        return redirect(url_for("stammdaten.csv_import"))
    return render_template("hochschreiben.html", schuljahre=schuljahre, von=von, nach=nach, vorschau=vorschau)
