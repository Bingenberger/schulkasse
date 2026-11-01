"""Administration: users, accounts, audit log, anonymisation (F10, F11)."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import anfangsbestand as ab
from .. import briefvorlage as bv
from .. import anonymisierung
from ..zwei_faktor import sitzungen_widerrufen
from ..auth import ROLLEN, AuthFehler, hash_passwort, rolle
from ..buchung import BuchungsFehler
from ..db import get_db, utcnow
from ..geld import BetragFehler, parse_betrag
from ..matching import matching_lauf, vorschlaege_auffrischen
from ..protokoll import protokolliere
from ..textnorm import iban_gueltig, iban_kompakt, normalisiere
from .util import datum_or_none, int_or_none, pdf_antwort, schwelle, seite, uid

bp = Blueprint("verwaltung", __name__, url_prefix="/verwaltung")


@bp.route("/benutzer", methods=["GET", "POST"])
@rolle("admin")
def benutzer():
    db = get_db()
    if request.method == "POST":
        f = request.form
        name = (f.get("benutzername") or "").strip()
        try:
            if not name or not (f.get("anzeigename") or "").strip():
                raise AuthFehler("Bitte Benutzername und Anzeigename angeben.")
            if f.get("rolle") not in ROLLEN:
                raise AuthFehler("Bitte eine Rolle wählen.")
            if db.one("SELECT id FROM benutzer WHERE lower(benutzername)=lower(?)", (name,)):
                raise AuthFehler("Der Benutzername ist bereits vergeben.")
            h = hash_passwort(f.get("passwort"))
            with db.tx():
                bid = db.insert("INSERT INTO benutzer (benutzername, anzeigename, passwort_hash, rolle, aktiv, erstellt_am)"
                                " VALUES (?,?,?,?,1,?)", (name, f["anzeigename"].strip(), h, f["rolle"], utcnow()))
                protokolliere(db, "benutzer.anlegen", "benutzer", bid,
                              neu={"benutzername": name, "anzeigename": f["anzeigename"], "rolle": f["rolle"]})
            flash("Benutzer angelegt.", "ok")
            return redirect(url_for("verwaltung.benutzer"))
        except AuthFehler as exc:
            flash(str(exc), "fehler")
    rows = db.all("SELECT * FROM benutzer ORDER BY aktiv DESC, anzeigename")
    return render_template("benutzer.html", benutzer=rows, werte=request.form)


@bp.route("/benutzer/<int:bid>", methods=["GET", "POST"])
@rolle("admin")
def benutzer_bearbeiten(bid):
    db = get_db()
    b = db.one("SELECT * FROM benutzer WHERE id=?", (bid,))
    if b is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("rolle") not in ROLLEN:
                raise AuthFehler("Bitte eine Rolle wählen.")
            aktiv = 1 if f.get("aktiv") == "1" else 0
            if bid == uid() and (not aktiv or f.get("rolle") != "admin"):
                raise AuthFehler("Sie können sich nicht selbst deaktivieren oder die Administratorrolle entziehen.")
            with db.tx():
                db.execute("UPDATE benutzer SET anzeigename=?, rolle=?, aktiv=? WHERE id=?",
                           ((f.get("anzeigename") or b["anzeigename"]).strip(), f["rolle"], aktiv, bid))
                if f.get("passwort"):
                    db.execute("UPDATE benutzer SET passwort_hash=? WHERE id=?", (hash_passwort(f["passwort"]), bid))
                if f.get("passwort") or not aktiv:
                    sitzungen_widerrufen(db, bid)
                neu = db.one("SELECT benutzername, anzeigename, rolle, aktiv FROM benutzer WHERE id=?", (bid,))
                protokolliere(db, "benutzer.aendern", "benutzer", bid,
                              alt={"anzeigename": b["anzeigename"], "rolle": b["rolle"], "aktiv": b["aktiv"]},
                              neu={**dict(neu), "passwort_geaendert": bool(f.get("passwort"))})
            flash("Gespeichert.", "ok")
            return redirect(url_for("verwaltung.benutzer"))
        except AuthFehler as exc:
            flash(str(exc), "fehler")
    return render_template("benutzer_form.html", b=b)


@bp.route("/konten", methods=["GET", "POST"])
@rolle("admin")
def konten():
    db = get_db()
    if request.method == "POST":
        f = request.form
        kid = int_or_none(f.get("id"))
        iban = iban_kompakt(f.get("iban"))
        try:
            if not (f.get("bezeichnung") or "").strip() or not (f.get("inhaber") or "").strip():
                raise ValueError("Bitte Bezeichnung und Kontoinhaber angeben.")
            if not iban_gueltig(iban):
                raise ValueError("Die IBAN ist ungültig (Prüfziffer).")
            if len(f.get("inhaber").strip()) > 70:
                raise ValueError("Der Kontoinhaber darf höchstens 70 Zeichen lang sein (QR-Code).")
            eroeffnung = parse_betrag(f.get("eroeffnungssaldo")) if (f.get("eroeffnungssaldo") or "").strip() else None
            werte = (f["bezeichnung"].strip(), iban, (f.get("bic") or "").strip().upper() or None, f["inhaber"].strip(),
                     1 if f.get("aktiv", "1") == "1" else 0, eroeffnung, datum_or_none(f.get("eroeffnungssaldo_datum")))
            with db.tx():
                if db.one("SELECT id FROM konto WHERE iban=? AND id<>?", (iban, kid or 0)):
                    raise ValueError("Diese IBAN ist bereits angelegt.")
                if kid:
                    alt = db.one("SELECT * FROM konto WHERE id=?", (kid,))
                    if alt["iban"] != iban and db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=?", (kid,), 0):
                        raise ValueError("Die IBAN kann nicht geändert werden, weil bereits Umsätze importiert wurden.")
                    db.execute("UPDATE konto SET bezeichnung=?, iban=?, bic=?, inhaber=?, aktiv=?,"
                               " eroeffnungssaldo_cent=?, eroeffnungssaldo_datum=? WHERE id=?", werte + (kid,))
                    protokolliere(db, "konto.aendern", "konto", kid, alt=alt, neu=dict(zip(
                        ("bezeichnung", "iban", "bic", "inhaber", "aktiv", "eroeffnungssaldo_cent", "datum"), werte)))
                else:
                    kid = db.insert("INSERT INTO konto (bezeichnung, iban, bic, inhaber, aktiv, eroeffnungssaldo_cent,"
                                    " eroeffnungssaldo_datum) VALUES (?,?,?,?,?,?,?)", werte)
                    protokolliere(db, "konto.anlegen", "konto", kid, neu=dict(zip(
                        ("bezeichnung", "iban", "bic", "inhaber", "aktiv", "eroeffnungssaldo_cent", "datum"), werte)))
            flash("Konto gespeichert.", "ok")
            return redirect(url_for("verwaltung.konten"))
        except (ValueError, BetragFehler) as exc:
            flash(str(exc), "fehler")
    rows = db.all("SELECT k.*, (SELECT COUNT(*) FROM bankumsatz WHERE konto_id=k.id) AS umsaetze FROM konto k ORDER BY id")
    bearbeiten = db.one("SELECT * FROM konto WHERE id=?", (int_or_none(request.args.get("id")) or 0,))
    return render_template("konten.html", konten=rows, k=bearbeiten)


def _posten_zum_code(db, code):
    ziel = normalisiere(code)
    for r in db.all("SELECT id, projekt_id, zahlcode FROM offener_posten WHERE zahlcode IS NOT NULL"):
        if normalisiere(r["zahlcode"]) == ziel:
            return r
    raise BuchungsFehler(f"Zahlcode „{code}“ nicht gefunden.")


@bp.route("/konten/<int:konto_id>/anfangsbestand", methods=["GET", "POST"])
@rolle("admin", "kasse")
def anfangsbestand(konto_id):
    """Start of bookkeeping (admin) and distribution of the opening balance (admin, kasse)."""
    db = get_db()
    k = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
    if k is None:
        abort(404)
    if request.method == "POST":
        aktion = request.form.get("aktion")
        try:
            if aktion == "stichtag":
                if g.user["rolle"] != "admin":
                    abort(403)
                roh = (request.form.get("beginn_datum") or "").strip()
                datum = datum_or_none(roh)
                if roh and not datum:
                    raise BuchungsFehler("Bitte ein gültiges Datum angeben.")
                aktive = ab.stichtag_setzen(db, konto_id, datum, uid())
                # Transactions that became active (earlier Stichtag) are matched now.
                matching_lauf(db, uid(), umsatz_ids=aktive, schwelle=schwelle())
                flash("Beginn der Kontoführung gespeichert." if datum else "Stichtag entfernt.", "ok")
            elif aktion == "verteilen":
                teile, posten_ids = [], []
                f = request.form
                for projekt, code, betrag, notiz in zip(f.getlist("projekt_id"), f.getlist("zahlcode"),
                                                        f.getlist("betrag"), f.getlist("notiz")):
                    if not (projekt or code.strip() or betrag.strip()):
                        continue
                    posten_id = None
                    if code.strip():
                        posten = _posten_zum_code(db, code)
                        posten_id, projekt = posten["id"], projekt or posten["projekt_id"]
                        posten_ids.append(posten_id)
                    if not betrag.strip():
                        raise BuchungsFehler("Bitte für jede Zeile einen Betrag angeben.")
                    teile.append({"projekt_id": int_or_none(projekt), "offener_posten_id": posten_id,
                                  "betrag_cent": parse_betrag(betrag), "notiz": notiz})
                ids = ab.verteilen(db, konto_id, teile, uid())
                vorschlaege_auffrischen(db, uid(), posten_ids)
                flash(f"{len(ids)} Anteil(e) des Anfangsbestands gespeichert.", "ok")
            elif aktion == "stornieren":
                eintrag = db.one("SELECT * FROM anfangsbestand WHERE id=? AND konto_id=?",
                                 (int_or_none(request.form.get("id")), konto_id))
                if eintrag is None:
                    abort(404)
                ab.stornieren(db, eintrag["id"], request.form.get("grund"), uid())
                flash("Anteil storniert.", "ok")
            else:
                abort(400)
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
        return redirect(url_for("verwaltung.anfangsbestand", konto_id=konto_id))
    k = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
    berechnet = ab.berechne(db, k)
    verteilt = ab.verteilt(db, konto_id)
    zeitraum = db.one("SELECT MIN(buchungsdatum) AS von, MAX(buchungsdatum) AS bis, COUNT(*) AS anzahl,"
                      " SUM(CASE WHEN buchungsdatum < ? THEN 1 ELSE 0 END) AS vorher"
                      " FROM bankumsatz WHERE konto_id=? AND status='BOOK'", (k["beginn_datum"] or "", konto_id))
    return render_template("anfangsbestand.html", k=k, ab=berechnet, verteilt=verteilt,
                           rest=(berechnet["betrag"] - verteilt) if berechnet["betrag"] is not None else None,
                           eintraege=ab.eintraege(db, konto_id=konto_id), abweichungen=ab.saldenabgleich(db, k),
                           zeitraum=zeitraum,
                           projekte=db.all("SELECT id, bezeichnung, kurzcode FROM projekt WHERE status='offen'"
                                           " ORDER BY bezeichnung"))


@bp.route("/briefvorlage", methods=["GET", "POST"])
@rolle("admin", "kasse")
def briefvorlage():
    """Letter texts and logo, editable in the browser, with PDF preview."""
    from ..briefe import beispielbrief
    db = get_db()
    pfad = current_app.config["BRIEF_CONFIG"]
    werte = None
    if request.method == "POST":
        datei = request.files.get("logo")
        logo = datei.read() if datei and datei.filename else None
        entfernen = request.form.get("logo_entfernen") == "1"
        try:
            if request.form.get("aktion") == "vorschau":
                # Preview the form as entered, without saving anything.
                cfg, _ = bv.wirksam(db, pfad)
                basis, _ = bv.datei_werte(pfad)
                for key, wert in bv.pruefe_werte(request.form).items():
                    cfg[key] = wert or basis.get(key, "")
                if logo:
                    cfg["logo_uri"] = bv.logo_data_uri(logo)
                elif entfernen:
                    cfg["logo_uri"] = None
                return pdf_antwort("Beispielbrief.pdf", beispielbrief(db, bv.vervollstaendige(cfg)), inline=True)
            aenderungen = bv.speichern(db, request.form, uid(), logo=logo, logo_entfernen=entfernen)
            flash("Briefvorlage gespeichert." if aenderungen else "Keine Änderungen.", "ok")
            return redirect(url_for("verwaltung.briefvorlage"))
        except BuchungsFehler as exc:
            flash(str(exc), "fehler")
            werte = request.form
    cfg, herkunft = bv.wirksam(db, pfad)
    _, dateifehler = bv.datei_werte(pfad)
    return render_template("briefvorlage.html", felder=bv.FELDER, werte=werte or cfg, cfg=cfg, herkunft=herkunft,
                           platzhalter=bv.platzhalter(cfg), dateifehler=dateifehler, pfad=pfad)


@bp.route("/protokoll")
@rolle("admin", "kasse")
def protokoll():
    db = get_db()
    sql = ("SELECT p.*, b.anzeigename FROM protokoll p LEFT JOIN benutzer b ON b.id=p.benutzer_id WHERE 1=1")
    params = []
    if request.args.get("aktion"):
        sql += " AND p.aktion LIKE ?"
        params.append(request.args["aktion"] + "%")
    if request.args.get("entitaet"):
        sql += " AND p.entitaet=?"
        params.append(request.args["entitaet"])
    if int_or_none(request.args.get("id")):
        sql += " AND p.entitaet_id=?"
        params.append(int(request.args["id"]))
    s = seite()
    rows = db.all(sql + " ORDER BY p.id DESC LIMIT 200 OFFSET ?", params + [(s - 1) * 200])
    return render_template("protokoll.html", rows=rows, args=request.args, seite=s)


@bp.route("/anonymisierung", methods=["GET", "POST"])
@rolle("admin")
def anonymisierung_view():
    """F11.2: shows what would be affected, runs only after explicit confirmation."""
    db = get_db()
    frist = current_app.config["ANONYMISIERUNG_FRIST_TAGE"]
    if request.method == "POST":
        if request.form.get("bestaetigung") != "ANONYMISIEREN":
            flash("Bitte zur Bestätigung ANONYMISIEREN eingeben.", "fehler")
        else:
            erg = anonymisierung.ausfuehren(db, frist, uid())
            flash(f"Anonymisierung abgeschlossen: {erg['kinder']} Kinder, {erg['umsaetze']} Umsätze, "
                  f"{erg['zahler_iban']} gelernte IBANs.", "ok")
            return redirect(url_for("verwaltung.anonymisierung_view"))
    return render_template("anonymisierung.html", k=anonymisierung.kandidaten(db, frist), frist=frist)
