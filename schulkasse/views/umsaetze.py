"""Transactions: import, work list, assignment, cancellation (F1, F5)."""
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from ..auth import rolle
from ..buchung import (AKTIV_SQL, BuchungsFehler, ignorieren, ignorieren_aufheben, stornieren, vorschlag_annehmen,
                       zuordnen)
from ..camt import CamtFehler, lies_upload
from ..db import get_db, utcnow
from ..geld import BetragFehler, parse_betrag
from ..importer import analysiere, importiere
from ..matching import matching_lauf, vorschlaege_auffrischen
from ..projekte import KATEGORIEN, posten_aus_umsatz
from .util import betrag_or_none, datum_or_none, int_or_none, schwelle, seite, uid

bp = Blueprint("umsaetze", __name__)
PRO_SEITE = 100


def _filter(args, basis):
    sql, params = basis, []
    if int_or_none(args.get("konto")):
        sql += " AND u.konto_id=?"
        params.append(int(args["konto"]))
    if datum_or_none(args.get("von")):
        sql += " AND u.buchungsdatum>=?"
        params.append(args["von"])
    if datum_or_none(args.get("bis")):
        sql += " AND u.buchungsdatum<=?"
        params.append(args["bis"])
    if args.get("richtung") == "ein":
        sql += " AND u.betrag_cent>0"
    elif args.get("richtung") == "aus":
        sql += " AND u.betrag_cent<0"
    if betrag_or_none(args.get("min")) is not None:
        sql += " AND ABS(u.betrag_cent)>=?"
        params.append(abs(betrag_or_none(args["min"])))
    if betrag_or_none(args.get("max")) is not None:
        sql += " AND ABS(u.betrag_cent)<=?"
        params.append(abs(betrag_or_none(args["max"])))
    return sql, params


def _vorschlaege(db, umsatz_ids):
    if not umsatz_ids:
        return {}
    ph = ",".join("?" * len(umsatz_ids))
    rows = db.all(f"SELECT * FROM vorschlag WHERE bankumsatz_id IN ({ph}) ORDER BY konfidenz DESC, id", umsatz_ids)
    teile = {}
    if rows:
        vph = ",".join("?" * len(rows))
        for t in db.all(
                "SELECT t.*, pr.bezeichnung AS projekt, pr.kurzcode, p.zahlcode, p.bezeichnung AS posten,"
                " k.vorname, k.nachname, kl.bezeichnung AS klasse FROM vorschlag_teil t"
                " JOIN projekt pr ON pr.id=t.projekt_id LEFT JOIN offener_posten p ON p.id=t.offener_posten_id"
                " LEFT JOIN kind k ON k.id=p.kind_id LEFT JOIN klasse kl ON kl.id=k.klasse_id"
                f" WHERE t.vorschlag_id IN ({vph}) ORDER BY t.id", [r["id"] for r in rows]):
            teile.setdefault(t["vorschlag_id"], []).append(t)
    ergebnis = {}
    for r in rows:
        ergebnis.setdefault(r["bankumsatz_id"], []).append({"v": r, "teile": teile.get(r["id"], [])})
    return ergebnis


@bp.route("/zuordnung")
@rolle("admin", "kasse")
def arbeitsliste():
    """F5.1-F5.3: open and partially assigned transactions with suggestions."""
    db = get_db()
    sql, params = _filter(request.args, "SELECT u.*, k.bezeichnung AS konto,"
                          " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zuordnung z WHERE z.bankumsatz_id=u.id"
                          "  AND z.storniert=0) AS verteilt"
                          " FROM bankumsatz u JOIN konto k ON k.id=u.konto_id"
                          f" WHERE u.status='BOOK' AND u.zuordnungsstatus IN ('offen','teilweise') AND {AKTIV_SQL}")
    if request.args.get("nur") == "vorschlag":
        sql += " AND EXISTS (SELECT 1 FROM vorschlag v WHERE v.bankumsatz_id=u.id)"
    elif request.args.get("nur") == "ohne":
        sql += " AND NOT EXISTS (SELECT 1 FROM vorschlag v WHERE v.bankumsatz_id=u.id)"
    gesamt = db.scalar(f"SELECT COUNT(*) FROM ({sql})", params, 0)
    s = seite()
    umsaetze = db.all(sql + " ORDER BY u.buchungsdatum DESC, u.id DESC LIMIT ? OFFSET ?",
                      params + [PRO_SEITE, (s - 1) * PRO_SEITE])
    vorgemerkt = db.all("SELECT u.*, k.bezeichnung AS konto FROM bankumsatz u JOIN konto k ON k.id=u.konto_id"
                        " WHERE u.status='PDNG' ORDER BY u.buchungsdatum DESC")
    return render_template("zuordnung.html", umsaetze=umsaetze, vorschlaege=_vorschlaege(db, [u["id"] for u in umsaetze]),
                           vorgemerkt=vorgemerkt, konten=db.all("SELECT * FROM konto ORDER BY bezeichnung"),
                           gesamt=gesamt, seite=s, seiten=max(1, -(-gesamt // PRO_SEITE)), args=request.args)


def _zurueck(standard):
    ziel = request.form.get("zurueck")
    if ziel and ziel.startswith("/") and not ziel.startswith("//"):
        return redirect(ziel)
    return redirect(standard)


@bp.route("/vorschlag/<int:vorschlag_id>/annehmen", methods=["POST"])
@rolle("admin", "kasse")
def vorschlag_uebernehmen(vorschlag_id):
    db = get_db()
    try:
        umsatz_id, ids = vorschlag_annehmen(db, vorschlag_id, uid())
        posten = [r[0] for r in db.all(f"SELECT offener_posten_id FROM zuordnung WHERE id IN ({','.join('?' * len(ids))})", ids)]
        vorschlaege_auffrischen(db, uid(), posten)
        flash("Vorschlag übernommen.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return _zurueck(url_for("umsaetze.arbeitsliste"))


@bp.route("/vorschlaege/sammel", methods=["POST"])
@rolle("admin", "kasse")
def sammelbestaetigung():
    """F5.3: accept the selected suggestions in one go."""
    db = get_db()
    ok, fehler, posten = 0, [], []
    for vid in request.form.getlist("vorschlag"):
        vid = int_or_none(vid)
        if not vid:
            continue
        try:
            _, ids = vorschlag_annehmen(db, vid, uid())
            posten += [r[0] for r in db.all(f"SELECT offener_posten_id FROM zuordnung WHERE id IN ({','.join('?' * len(ids))})", ids)]
            ok += 1
        except BuchungsFehler as exc:
            fehler.append(str(exc))
    vorschlaege_auffrischen(db, uid(), posten)
    if ok:
        flash(f"{ok} Vorschläge übernommen.", "ok")
    for f in fehler:
        flash(f, "fehler")
    if not ok and not fehler:
        flash("Bitte mindestens einen Vorschlag auswählen.", "fehler")
    return _zurueck(url_for("umsaetze.arbeitsliste"))


@bp.route("/umsaetze")
@rolle("admin", "kasse")
def liste():
    db = get_db()
    sql, params = _filter(request.args, "SELECT u.*, k.bezeichnung AS konto, (u.buchungsdatum < COALESCE(k.beginn_datum, '')) AS vor_beginn FROM bankumsatz u"
                          " JOIN konto k ON k.id=u.konto_id WHERE 1=1")
    status = request.args.get("status")
    if status in ("offen", "teilweise", "zugeordnet", "ignoriert"):
        sql += f" AND u.zuordnungsstatus=? AND u.status='BOOK' AND {AKTIV_SQL}"
        params.append(status)
    elif status == "vorbeginn":
        sql += f" AND u.status='BOOK' AND NOT ({AKTIV_SQL})"
    elif status == "vorgemerkt":
        sql += " AND u.status='PDNG'"
    q = (request.args.get("q") or "").strip()
    if q:
        m = f"%{q}%"
        sql += " AND (u.verwendungszweck LIKE ? OR u.gegen_name LIKE ? OR u.verwendungszweck_roh LIKE ?"
        params += [m, m, m]
        try:
            cent = parse_betrag(q)
        except BetragFehler:
            cent = None
        if cent is not None:
            sql += " OR ABS(u.betrag_cent)=?"
            params.append(abs(cent))
        sql += ")"
    gesamt = db.scalar(f"SELECT COUNT(*) FROM ({sql})", params, 0)
    s = seite()
    umsaetze = db.all(sql + " ORDER BY u.buchungsdatum DESC, u.id DESC LIMIT ? OFFSET ?",
                      params + [PRO_SEITE, (s - 1) * PRO_SEITE])
    return render_template("umsaetze.html", umsaetze=umsaetze, konten=db.all("SELECT * FROM konto ORDER BY bezeichnung"),
                           gesamt=gesamt, seite=s, seiten=max(1, -(-gesamt // PRO_SEITE)), args=request.args)


@bp.route("/umsaetze/<int:umsatz_id>")
@rolle("admin", "kasse")
def detail(umsatz_id):
    db = get_db()
    u = db.one("SELECT u.*, k.bezeichnung AS konto, k.beginn_datum, (u.buchungsdatum < COALESCE(k.beginn_datum, '')) AS vor_beginn, i.dateiname FROM bankumsatz u JOIN konto k ON k.id=u.konto_id"
               " JOIN import i ON i.id=u.import_id WHERE u.id=?", (umsatz_id,))
    if u is None:
        abort(404)
    zuordnungen = db.all(
        "SELECT z.*, pr.bezeichnung AS projekt, pr.kurzcode, pr.status AS projekt_status, p.zahlcode,"
        " p.bezeichnung AS posten, k.vorname, k.nachname, b.anzeigename AS von, s.anzeigename AS storno_von"
        " FROM zuordnung z JOIN projekt pr ON pr.id=z.projekt_id LEFT JOIN offener_posten p ON p.id=z.offener_posten_id"
        " LEFT JOIN kind k ON k.id=p.kind_id JOIN benutzer b ON b.id=z.erstellt_von"
        " LEFT JOIN benutzer s ON s.id=z.storniert_von WHERE z.bankumsatz_id=? ORDER BY z.id", (umsatz_id,))
    verteilt = sum(abs(z["betrag_cent"]) for z in zuordnungen if not z["storniert"])
    ignoriert = db.all("SELECT i.*, b.anzeigename AS von FROM umsatz_ignoriert i JOIN benutzer b ON b.id=i.erstellt_von"
                       " WHERE i.bankumsatz_id=? ORDER BY i.id", (umsatz_id,))
    projekte = db.all("SELECT id, bezeichnung, kurzcode FROM projekt WHERE status='offen' ORDER BY bezeichnung")
    belege = db.all("SELECT b.* FROM beleg b JOIN zuordnung z ON z.id=b.zuordnung_id WHERE z.bankumsatz_id=?",
                    (umsatz_id,))
    return render_template("umsatz_detail.html", u=u, zuordnungen=zuordnungen, verteilt=verteilt,
                           rest=abs(u["betrag_cent"]) - verteilt, vorschlaege=_vorschlaege(db, [umsatz_id]).get(umsatz_id, []),
                           projekte=projekte, ignoriert=ignoriert, belege=belege,
                           kategorien={k: v for k, v in KATEGORIEN.items() if k != "elternbeitrag"})


@bp.route("/umsaetze/<int:umsatz_id>/zuordnen", methods=["POST"])
@rolle("admin", "kasse")
def zuordnen_manuell(umsatz_id):
    """F5.4: split over any number of projects/items."""
    db = get_db()
    teile = []
    try:
        for projekt, posten, betrag in zip(request.form.getlist("projekt_id"), request.form.getlist("posten_id"),
                                           request.form.getlist("betrag")):
            if not betrag.strip() and not projekt and not posten:
                continue
            if not projekt and not posten:
                raise BuchungsFehler("Bitte für jeden Teilbetrag ein Projekt wählen.")
            teile.append({"projekt_id": int_or_none(projekt), "offener_posten_id": int_or_none(posten),
                          "betrag_cent": parse_betrag(betrag)})
        ids = zuordnen(db, umsatz_id, teile, "manuell", uid(), notiz=(request.form.get("notiz") or "").strip() or None)
        vorschlaege_auffrischen(db, uid(), [t["offener_posten_id"] for t in teile])
        flash(f"{len(ids)} Zuordnung(en) gespeichert.", "ok")
    except (BuchungsFehler, BetragFehler) as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("umsaetze.detail", umsatz_id=umsatz_id))


@bp.route("/umsaetze/<int:umsatz_id>/posten", methods=["POST"])
@rolle("admin", "kasse")
def posten_anlegen_zuordnen(umsatz_id):
    """Create an item from the transaction, optionally with the expected counter payment."""
    db = get_db()
    f = request.form
    try:
        projekt_id = int_or_none(f.get("projekt_id"))
        if not projekt_id:
            raise BuchungsFehler("Bitte ein Projekt wählen.")
        pid, gid = posten_aus_umsatz(db, umsatz_id, projekt_id=projekt_id, bezeichnung=f.get("bezeichnung", ""),
                                     kategorie=f.get("kategorie"), betrag_cent=parse_betrag(f.get("betrag")),
                                     benutzer_id=uid(), gegenposten=f.get("gegenposten") == "1",
                                     notiz=(f.get("notiz") or "").strip() or None)
        if gid:
            # an already imported refund may now match the new open item
            matching_lauf(db, uid(), auto=False)
            flash("Posten angelegt und zugeordnet. Der offene Gegenposten wartet auf die Zahlung.", "ok")
        else:
            flash("Posten angelegt und zugeordnet.", "ok")
    except (BuchungsFehler, BetragFehler) as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("umsaetze.detail", umsatz_id=umsatz_id))


@bp.route("/zuordnungen/<int:zuordnung_id>/stornieren", methods=["POST"])
@rolle("admin", "kasse")
def storno(zuordnung_id):
    db = get_db()
    z = db.one("SELECT bankumsatz_id FROM zuordnung WHERE id=?", (zuordnung_id,))
    if z is None:
        abort(404)
    try:
        stornieren(db, zuordnung_id, request.form.get("grund"), uid())
        matching_lauf(db, uid(), umsatz_ids=[z["bankumsatz_id"]], auto=False)
        flash("Zuordnung storniert.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return _zurueck(url_for("umsaetze.detail", umsatz_id=z["bankumsatz_id"]))


@bp.route("/umsaetze/<int:umsatz_id>/ignorieren", methods=["POST"])
@rolle("admin", "kasse")
def ignorieren_view(umsatz_id):
    try:
        ignorieren(get_db(), umsatz_id, request.form.get("grund"), uid())
        flash("Umsatz wird ignoriert.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return _zurueck(url_for("umsaetze.detail", umsatz_id=umsatz_id))


@bp.route("/umsaetze/<int:umsatz_id>/ignorieren-aufheben", methods=["POST"])
@rolle("admin", "kasse")
def ignorieren_aufheben_view(umsatz_id):
    db = get_db()
    try:
        ignorieren_aufheben(db, umsatz_id, uid())
        matching_lauf(db, uid(), umsatz_ids=[umsatz_id], auto=False)
        flash("Der Umsatz ist wieder offen.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("umsaetze.detail", umsatz_id=umsatz_id))


@bp.route("/ignoriert")
@rolle("admin", "kasse")
def ignoriert_liste():
    db = get_db()
    rows = db.all("SELECT u.*, i.grund, i.erstellt_am AS ignoriert_am, b.anzeigename AS von FROM bankumsatz u"
                  " JOIN umsatz_ignoriert i ON i.bankumsatz_id=u.id AND i.aufgehoben=0"
                  " JOIN benutzer b ON b.id=i.erstellt_von WHERE u.zuordnungsstatus='ignoriert'"
                  " ORDER BY u.buchungsdatum DESC")
    return render_template("ignoriert.html", umsaetze=rows)


@bp.route("/matching", methods=["POST"])
@rolle("admin", "kasse")
def matching_erneut():
    stats = matching_lauf(get_db(), uid(), schwelle=schwelle())
    flash(f"Matching: {stats['geprueft']} Umsätze geprüft, {stats['automatisch']} automatisch zugeordnet, "
          f"{stats['vorschlaege']} mit Vorschlag.", "ok")
    return redirect(url_for("umsaetze.arbeitsliste"))


@bp.route("/api/projekte/<int:projekt_id>/posten")
@rolle("admin", "kasse")
def api_posten(projekt_id):
    db = get_db()
    richtung = "ausgang" if request.args.get("richtung") == "aus" else "eingang"
    rows = db.all(
        "SELECT p.id, p.zahlcode, p.bezeichnung, p.sollbetrag_cent, p.status, k.vorname, k.nachname,"
        " kl.bezeichnung AS klasse, (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z"
        "  WHERE z.offener_posten_id=p.id AND z.storniert=0) AS bezahlt"
        " FROM offener_posten p LEFT JOIN kind k ON k.id=p.kind_id LEFT JOIN klasse kl ON kl.id=k.klasse_id"
        " WHERE p.projekt_id=? AND p.richtung=? AND p.status NOT IN ('storniert')"
        " ORDER BY p.status IN ('erledigt','erlassen'), kl.bezeichnung, k.nachname, p.id", (projekt_id, richtung))
    from ..geld import format_cent
    return jsonify([{
        "id": r["id"],
        "text": " · ".join(x for x in (
            r["zahlcode"], f"{r['vorname']} {r['nachname']} ({r['klasse']})" if r["vorname"] else r["bezeichnung"],
            f"offen {format_cent(r['sollbetrag_cent'] - r['bezahlt'])}" if r["status"] in ("offen", "teilweise")
            else r["status"]) if x),
        "rest_cent": max(0, r["sollbetrag_cent"] - r["bezahlt"]),
    } for r in rows])


@bp.route("/api/zahlcode")
@rolle("admin", "kasse")
def api_zahlcode():
    from ..textnorm import normalisiere
    db = get_db()
    code = normalisiere(request.args.get("code", ""))
    for r in db.all("SELECT p.id, p.projekt_id, p.zahlcode FROM offener_posten p JOIN projekt pr ON pr.id=p.projekt_id"
                    " WHERE p.zahlcode IS NOT NULL AND pr.status='offen'"):
        if normalisiere(r["zahlcode"]) == code:
            return jsonify({"posten_id": r["id"], "projekt_id": r["projekt_id"], "zahlcode": r["zahlcode"]})
    return jsonify({}), 404


# ------------------------------------------------------------------- import

def _alte_vorschauen_loeschen(db):
    grenze = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM import_vorschau WHERE erstellt_am < ?", (grenze,))


@bp.route("/import", methods=["GET", "POST"])
@rolle("admin", "kasse")
def import_start():
    """F1.1/F1.9: upload and preview. Nothing is written to bankumsatz yet."""
    db = get_db()
    if request.method == "POST":
        datei = request.files.get("datei")
        if not datei or not datei.filename:
            flash("Bitte eine Datei auswählen.", "fehler")
            return redirect(url_for("umsaetze.import_start"))
        daten = datei.read()
        try:
            analysen = analysiere(db, lies_upload(daten, datei.filename))
        except CamtFehler as exc:
            flash(str(exc), "fehler")
            return redirect(url_for("umsaetze.import_start"))
        _alte_vorschauen_loeschen(db)
        token = secrets.token_urlsafe(24)
        db.execute("INSERT INTO import_vorschau (token, benutzer_id, art, dateiname, daten, erstellt_am)"
                   " VALUES (?,?,?,?,?,?)", (token, uid(), "camt", datei.filename, daten, utcnow()))
        return render_template("import_vorschau.html", analysen=analysen, token=token, dateiname=datei.filename)
    importe = db.all("SELECT i.*, k.bezeichnung AS konto, b.anzeigename AS von FROM import i"
                     " JOIN konto k ON k.id=i.konto_id JOIN benutzer b ON b.id=i.importiert_von"
                     " ORDER BY i.id DESC LIMIT 50")
    return render_template("import.html", importe=importe,
                           konten=db.all("SELECT * FROM konto WHERE aktiv=1 ORDER BY bezeichnung"))


@bp.route("/import/bestaetigen", methods=["POST"])
@rolle("admin", "kasse")
def import_bestaetigen():
    db = get_db()
    v = db.one("SELECT * FROM import_vorschau WHERE token=? AND benutzer_id=?", (request.form.get("token"), uid()))
    if v is None:
        flash("Die Vorschau ist abgelaufen. Bitte die Datei erneut hochladen.", "fehler")
        return redirect(url_for("umsaetze.import_start"))
    db.execute("DELETE FROM import_vorschau WHERE token=?", (v["token"],))
    try:
        analysen = analysiere(db, lies_upload(v["daten"], v["dateiname"]))
        ergebnisse, stats = importiere(db, analysen, uid(), schwelle())
    except CamtFehler as exc:
        flash(str(exc), "fehler")
        return redirect(url_for("umsaetze.import_start"))
    return render_template("import_ergebnis.html", ergebnisse=ergebnisse, stats=stats)


@bp.route("/import/abbrechen", methods=["POST"])
@rolle("admin", "kasse")
def import_abbrechen():
    get_db().execute("DELETE FROM import_vorschau WHERE token=? AND benutzer_id=?", (request.form.get("token"), uid()))
    flash("Import abgebrochen, es wurden keine Daten übernommen.", "info")
    return redirect(url_for("umsaetze.import_start"))
