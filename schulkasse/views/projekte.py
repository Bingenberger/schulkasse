"""Projects, items, letters, payment status, receipts (F2, F3, F7, F8.2, F8.6)."""
import mimetypes
import uuid
from datetime import date
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file,
                   url_for)

from .. import anfangsbestand as ab
from .. import umbuchung as ub
from ..auswertung import (projekt_je_konto, projekt_kennzahlen, projekt_posten, projekt_umsaetze,
                          projekte_uebersicht, zahlungsstand)
from ..auth import ist_kasse, login_erforderlich, pruefe_projekt, rolle
from ..briefe import brief_config, html_zu_pdf, posten_fuer_brief, serienbrief
from ..buchung import BuchungsFehler, bezahlt_cent
from ..db import get_db, utcnow
from ..geld import BetragFehler, format_cent_csv, parse_betrag
from ..matching import vorschlaege_auffrischen
from ..projekte import (KATEGORIEN, KLASSEN_SQL, TYPEN, beitraege_aendern, beitragshoehen, jahrgang, kandidaten_massenanlage, kurzcode_gesperrt,
                        massenanlage, posten_aendern, posten_anlegen, posten_status_setzen, projekt_abschliessen,
                        projekt_aendern, projekt_anlegen, projekt_klassen_ids, projekt_oeffnen,
                        ueberzahlung_klaeren)
from ..protokoll import protokolliere
from ..stammdaten import aktives_schuljahr
from .util import csv_antwort, datum_or_none, int_or_none, pdf_antwort, uid, zurueck_oder

bp = Blueprint("projekte", __name__)
ERLAUBTE_BELEGE = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _projekt(projekt_id):
    db = get_db()
    p = db.one("SELECT pr.*, " + KLASSEN_SQL + " AS klasse, sj.bezeichnung AS schuljahr, b.anzeigename AS verantwortlich"
               " FROM projekt pr JOIN schuljahr sj ON sj.id=pr.schuljahr_id"
               " LEFT JOIN benutzer b ON b.id=pr.verantwortlich_user_id WHERE pr.id=?", (projekt_id,))
    pruefe_projekt(p)
    return p


def _klassen_gruppen(db):
    """Classes per school year and grade for the multi-select in the project form."""
    gruppen = []
    for sj in db.all("SELECT * FROM schuljahr ORDER BY beginn DESC"):
        klassen = db.all("SELECT kl.*, (SELECT COUNT(*) FROM kind k WHERE k.klasse_id=kl.id AND k.aktiv=1) AS anzahl"
                         " FROM klasse kl WHERE kl.schuljahr_id=? ORDER BY length(kl.bezeichnung), kl.bezeichnung",
                         (sj["id"],))
        if not klassen:
            continue
        jahrgaenge = {}
        for kl in klassen:
            j = jahrgang(kl["bezeichnung"])
            if j:
                jahrgaenge.setdefault(j, []).append(kl["id"])
        gruppen.append({"schuljahr": sj, "klassen": [dict(kl) | {"jahrgang": jahrgang(kl["bezeichnung"])} for kl in klassen],
                        "jahrgaenge": sorted((j for j, ids in jahrgaenge.items() if len(ids) > 1), key=int)})
    return gruppen


def _formular_daten(db, werte):
    if hasattr(werte, "getlist"):
        gewaehlt = [int(k) for k in werte.getlist("klasse_ids") if k.isdigit()]
    else:
        gewaehlt = werte.get("klassen_ids") or []
    return {
        "schuljahre": db.all("SELECT * FROM schuljahr ORDER BY beginn DESC"),
        "klassen_gruppen": _klassen_gruppen(db),
        "klassen_gewaehlt": gewaehlt,
        "benutzer": db.all("SELECT id, anzeigename, rolle FROM benutzer WHERE aktiv=1 ORDER BY anzeigename"),
        "konten": db.all("SELECT * FROM konto WHERE aktiv=1 ORDER BY id"),
        "typen": TYPEN,
    }


@bp.route("/projekte")
@login_erforderlich
def liste():
    db = get_db()
    sj_id = int_or_none(request.args.get("schuljahr"))
    if sj_id is None and "schuljahr" not in request.args:
        sj = aktives_schuljahr(db)
        sj_id = sj["id"] if sj else None
    status = request.args.get("status") if request.args.get("status") in ("offen", "abgeschlossen") else None
    nur = None if ist_kasse() else g.user["id"]
    projekte = projekte_uebersicht(db, schuljahr_id=sj_id, nur_user_id=nur, status=status)
    return render_template("projekte.html", projekte=projekte, schuljahre=db.all("SELECT * FROM schuljahr ORDER BY beginn DESC"),
                           sj_id=sj_id, status=status, typen=TYPEN)


@bp.route("/projekte/neu", methods=["GET", "POST"])
@rolle("admin", "kasse")
def neu():
    db = get_db()
    if request.method == "POST":
        try:
            pid = projekt_anlegen(db, request.form, uid())
            flash("Projekt angelegt.", "ok")
            return redirect(url_for("projekte.detail", projekt_id=pid))
        except BuchungsFehler as exc:
            flash(str(exc), "fehler")
    sj = aktives_schuljahr(db)
    werte = request.form or {"schuljahr_id": sj["id"] if sj else ""}
    return render_template("projekt_form.html", p=None, werte=werte, gesperrt=False, **_formular_daten(db, werte))


@bp.route("/projekte/<int:projekt_id>/bearbeiten", methods=["GET", "POST"])
@rolle("admin", "kasse")
def bearbeiten(projekt_id):
    db = get_db()
    p = _projekt(projekt_id)
    if request.method == "POST":
        try:
            projekt_aendern(db, projekt_id, request.form, uid())
            flash("Projekt gespeichert.", "ok")
            return redirect(url_for("projekte.detail", projekt_id=projekt_id))
        except BuchungsFehler as exc:
            flash(str(exc), "fehler")
    werte = request.form or dict(p) | {"klassen_ids": projekt_klassen_ids(db, projekt_id)}
    return render_template("projekt_form.html", p=p, werte=werte,
                           gesperrt=kurzcode_gesperrt(db, projekt_id), **_formular_daten(db, werte))


@bp.route("/projekte/<int:projekt_id>")
@login_erforderlich
def detail(projekt_id):
    db = get_db()
    p = _projekt(projekt_id)
    kz = projekt_kennzahlen(db, projekt_id)
    kasse = ist_kasse()
    if kasse:
        posten = projekt_posten(db, projekt_id)
    else:
        # Teachers see the class list: never BuT children or waived items (F3.3).
        posten = zahlungsstand(db, projekt_id, fuer_dritte=True)
    umsaetze = projekt_umsaetze(db, projekt_id)
    belege = db.all("SELECT b.*, u.anzeigename AS von FROM beleg b JOIN benutzer u ON u.id=b.hochgeladen_von"
                    " WHERE b.projekt_id=? OR b.zuordnung_id IN (SELECT id FROM zuordnung WHERE projekt_id=?)"
                    " ORDER BY b.hochgeladen_am", (projekt_id, projekt_id))
    ziele = db.all("SELECT id, bezeichnung, kurzcode FROM projekt WHERE status='offen' AND id<>? ORDER BY bezeichnung",
                   (projekt_id,)) if kasse else []
    return render_template("projekt_detail.html", p=p, kz=kz, posten=posten, umsaetze=umsaetze, belege=belege,
                           typen=TYPEN, kategorien=KATEGORIEN, gesperrt=kurzcode_gesperrt(db, projekt_id),
                           saldo=ub.saldo(db, projekt_id), umbuchungen=ub.eintraege(db, projekt_id=projekt_id),
                           ziele=ziele, beitraege=beitragshoehen(db, projekt_id) if kasse else [],
                           je_konto=projekt_je_konto(db, projekt_id) if kasse else [],
                           konten=db.all("SELECT * FROM konto WHERE aktiv=1 ORDER BY id") if kasse else [],
                           konto_vorschlag=ub.konto_vorschlag(db, projekt_id) if kasse else None)


@bp.route("/projekte/<int:projekt_id>/beitraege-aendern", methods=["POST"])
@rolle("admin", "kasse")
def beitraege_aendern_view(projekt_id):
    db = get_db()
    _projekt(projekt_id)
    alt = int_or_none(request.form.get("alt_cent")) or 0
    try:
        erg = beitraege_aendern(db, projekt_id, alt, parse_betrag(request.form.get("betrag")), uid(),
                                bezeichnung=request.form.get("bezeichnung"))
        text = f"{erg['geaendert']} Elternbeiträge geändert."
        if erg["ueberzahlt"]:
            text += f" {erg['ueberzahlt']} davon sind jetzt überzahlt (siehe Auswertungen → Überzahlungen)."
        flash(text, "ok")
        if erg["mit_brief"]:
            flash(f"{erg['mit_brief']} Eltern haben bereits einen Brief mit dem alten Betrag im QR-Code erhalten. "
                  "Bitte die Briefe neu erzeugen und austeilen – die Zahlcodes bleiben gleich.", "info")
    except (BuchungsFehler, BetragFehler) as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.detail", projekt_id=projekt_id))


@bp.route("/projekte/<int:projekt_id>/abschliessen", methods=["POST"])
@rolle("admin", "kasse")
def abschliessen(projekt_id):
    try:
        projekt_abschliessen(get_db(), projekt_id, uid(), rest_ok=request.form.get("mit_rest") == "1")
        flash("Projekt abgeschlossen.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.detail", projekt_id=projekt_id))


@bp.route("/projekte/<int:projekt_id>/oeffnen", methods=["POST"])
@rolle("admin", "kasse")
def oeffnen(projekt_id):
    try:
        projekt_oeffnen(get_db(), projekt_id, uid())
        flash("Projekt wieder geöffnet.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.detail", projekt_id=projekt_id))


@bp.route("/projekte/<int:projekt_id>/umbuchung", methods=["POST"])
@rolle("admin", "kasse")
def umbuchung_anlegen(projekt_id):
    """Move an amount to or from another project (no money on the bank account)."""
    db = get_db()
    _projekt(projekt_id)
    f = request.form
    ziel = int_or_none(f.get("ziel_projekt_id"))
    try:
        if not ziel:
            raise BuchungsFehler("Bitte ein anderes Projekt wählen.")
        betrag = abs(parse_betrag(f.get("betrag")))
        von, nach = (projekt_id, ziel) if f.get("richtung") == "ab" else (ziel, projekt_id)
        posten_id = None
        if (f.get("zahlcode") or "").strip():
            from ..textnorm import normalisiere
            ziel_code = normalisiere(f["zahlcode"])
            treffer = [r for r in db.all("SELECT id, zahlcode FROM offener_posten WHERE projekt_id=?", (nach,))
                       if r["zahlcode"] and normalisiere(r["zahlcode"]) == ziel_code]
            if not treffer:
                raise BuchungsFehler(f"Zahlcode „{f['zahlcode']}“ gehört nicht zum empfangenden Projekt.")
            posten_id = treffer[0]["id"]
        ub.anlegen(db, von, nach, betrag, f.get("grund"), uid(), offener_posten_id=posten_id,
                   konto_id=int_or_none(f.get("konto_id")))
        vorschlaege_auffrischen(db, uid(), [posten_id])
        flash("Umbuchung gespeichert.", "ok")
    except (BuchungsFehler, BetragFehler) as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.detail", projekt_id=projekt_id))


@bp.route("/umbuchungen/<int:umbuchung_id>/stornieren", methods=["POST"])
@rolle("admin", "kasse")
def umbuchung_stornieren(umbuchung_id):
    db = get_db()
    try:
        u = ub.stornieren(db, umbuchung_id, request.form.get("grund"), uid())
        if u["offener_posten_id"]:
            vorschlaege_auffrischen(db, uid(), [u["offener_posten_id"]])
        flash("Umbuchung storniert.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
        u = db.one("SELECT von_projekt_id FROM umbuchung WHERE id=?", (umbuchung_id,))
    return redirect(url_for("projekte.detail", projekt_id=int_or_none(request.form.get("projekt_id"))
                            or u["von_projekt_id"]))


@bp.route("/projekte/<int:projekt_id>/massenanlage", methods=["GET", "POST"])
@rolle("admin", "kasse")
def massenanlage_view(projekt_id):
    """F3.1/F3.3: bulk creation of parent contributions."""
    db = get_db()
    p = _projekt(projekt_id)
    if request.method == "POST":
        try:
            betrag = parse_betrag(request.form.get("betrag"))
            erg = massenanlage(db, projekt_id, request.form.getlist("kind"), betrag, uid(),
                               bezeichnung=request.form.get("bezeichnung"),
                               faellig_am=datum_or_none(request.form.get("faellig_am")),
                               but_kind_ids=request.form.getlist("but_kind"),
                               but_modus=request.form.get("but_modus", "keiner"))
            flash(f"{erg['angelegt']} Posten angelegt, {erg['erlassen']} als erlassen, "
                  f"{erg['uebersprungen_but']} BuT-Kinder übersprungen, "
                  f"{erg['uebersprungen_vorhanden']} bereits vorhanden.", "ok")
            return redirect(url_for("projekte.detail", projekt_id=projekt_id))
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
    klassen_ids = [int(k) for k in request.values.getlist("klasse") if k.isdigit()]
    if not klassen_ids and "klasse" not in request.values:
        klassen_ids = projekt_klassen_ids(db, projekt_id)
    klassen = db.all("SELECT kl.*, (SELECT COUNT(*) FROM kind k WHERE k.klasse_id=kl.id AND k.aktiv=1) AS anzahl"
                     " FROM klasse kl WHERE kl.schuljahr_id=? ORDER BY kl.bezeichnung", (p["schuljahr_id"],))
    kinder = kandidaten_massenanlage(db, projekt_id, klassen_ids)
    return render_template("massenanlage.html", p=p, klassen=klassen, klassen_ids=klassen_ids, kinder=kinder,
                           werte=request.form)


@bp.route("/projekte/<int:projekt_id>/posten/neu", methods=["GET", "POST"])
@rolle("admin", "kasse")
def posten_neu(projekt_id):
    db = get_db()
    p = _projekt(projekt_id)
    if request.method == "POST":
        f = request.form
        try:
            posten_anlegen(db, projekt_id, richtung=f.get("richtung"), kategorie=f.get("kategorie"),
                           bezeichnung=f.get("bezeichnung", ""), sollbetrag_cent=parse_betrag(f.get("betrag")),
                           kind_id=int_or_none(f.get("kind_id")), faellig_am=datum_or_none(f.get("faellig_am")),
                           mit_zahlcode=f.get("zahlcode") == "1", notiz=f.get("notiz") or None, benutzer_id=uid())
            flash("Posten angelegt.", "ok")
            return redirect(url_for("projekte.detail", projekt_id=projekt_id))
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
    kinder = db.all("SELECT k.id, k.vorname, k.nachname, kl.bezeichnung AS klasse FROM kind k"
                    " JOIN klasse kl ON kl.id=k.klasse_id WHERE kl.schuljahr_id=? AND k.aktiv=1 AND k.anonymisiert=0"
                    " ORDER BY kl.bezeichnung, k.nachname, k.vorname", (p["schuljahr_id"],))
    return render_template("posten_form.html", p=p, kinder=kinder, kategorien=KATEGORIEN, werte=request.form)


@bp.route("/posten/<int:posten_id>", methods=["GET", "POST"])
@login_erforderlich
def posten_detail(posten_id):
    db = get_db()
    po = db.one("SELECT p.*, k.vorname, k.nachname, k.but_status, kl.bezeichnung AS klasse FROM offener_posten p"
                " LEFT JOIN kind k ON k.id=p.kind_id LEFT JOIN klasse kl ON kl.id=k.klasse_id WHERE p.id=?", (posten_id,))
    if po is None:
        abort(404)
    p = _projekt(po["projekt_id"])
    if not ist_kasse():
        abort(403)
    if request.method == "POST":
        try:
            posten_aendern(db, posten_id, bezeichnung=request.form.get("bezeichnung", ""),
                           sollbetrag_cent=parse_betrag(request.form.get("betrag")),
                           faellig_am=datum_or_none(request.form.get("faellig_am")),
                           notiz=request.form.get("notiz"), benutzer_id=uid())
            flash("Posten gespeichert.", "ok")
            return redirect(url_for("projekte.posten_detail", posten_id=posten_id))
        except (BuchungsFehler, BetragFehler) as exc:
            flash(str(exc), "fehler")
    zuordnungen = db.all("SELECT z.*, u.buchungsdatum, u.gegen_name, u.verwendungszweck FROM zuordnung z"
                         " JOIN bankumsatz u ON u.id=z.bankumsatz_id WHERE z.offener_posten_id=? ORDER BY z.id",
                         (posten_id,))
    konten = db.all("SELECT * FROM konto WHERE aktiv=1 ORDER BY id")
    return render_template("posten_detail.html", po=po, p=p, zuordnungen=zuordnungen,
                           bezahlt=bezahlt_cent(db, posten_id), anfang=ab.eintraege(db, posten_id=posten_id),
                           umbuchungen=ub.eintraege(db, posten_id=posten_id),
                           konten=konten, konto_vorwahl=p["konto_id"] or (konten[0]["id"] if konten else None))


@bp.route("/posten/<int:posten_id>/anfangsbestand", methods=["POST"])
@rolle("admin", "kasse")
def posten_anfangsbestand(posten_id):
    """Item already paid before the Stichtag: cover it from the opening balance."""
    db = get_db()
    po = db.one("SELECT * FROM offener_posten WHERE id=?", (posten_id,))
    if po is None:
        abort(404)
    _projekt(po["projekt_id"])
    try:
        betrag = abs(parse_betrag(request.form.get("betrag")))
        if po["richtung"] == "ausgang":
            betrag = -betrag
        ab.verteilen(db, int_or_none(request.form.get("konto_id")),
                     [{"projekt_id": po["projekt_id"], "offener_posten_id": posten_id, "betrag_cent": betrag,
                       "notiz": request.form.get("notiz") or "Vor dem Stichtag bezahlt"}], uid())
        vorschlaege_auffrischen(db, uid(), [posten_id])
        flash("Aus dem Anfangsbestand verbucht.", "ok")
    except (BuchungsFehler, BetragFehler) as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.posten_detail", posten_id=posten_id))


@bp.route("/posten/<int:posten_id>/ueberzahlung", methods=["POST"])
@rolle("admin", "kasse")
def posten_ueberzahlung(posten_id):
    """Mark an overpayment as settled; the amounts stay untouched."""
    try:
        ueberzahlung_klaeren(get_db(), posten_id, request.form.get("grund"), uid(),
                             aufheben=request.form.get("aktion") == "aufheben")
        flash("Überzahlung als geklärt vermerkt." if request.form.get("aktion") != "aufheben"
              else "Die Überzahlung gilt wieder als offen.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return zurueck_oder(url_for("projekte.posten_detail", posten_id=posten_id))


@bp.route("/posten/<int:posten_id>/status", methods=["POST"])
@rolle("admin", "kasse")
def posten_status(posten_id):
    try:
        posten_status_setzen(get_db(), posten_id, request.form.get("status"), request.form.get("grund"), uid())
        flash("Status geändert.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(url_for("projekte.posten_detail", posten_id=posten_id))


# ------------------------------------------------------------------ letters

@bp.route("/projekte/<int:projekt_id>/briefe", methods=["GET", "POST"])
@login_erforderlich
def briefe(projekt_id):
    """F7: letters for parents, also available to the responsible teacher."""
    db = get_db()
    p = _projekt(projekt_id)
    if p["typ"] != "elternbeitrag":
        flash("Serienbriefe gibt es nur für Projekte vom Typ Elternbeitrag.", "fehler")
        return redirect(url_for("projekte.detail", projekt_id=projekt_id))
    cfg = brief_config()
    if request.method == "POST":
        modus = request.form.get("modus", "alle")
        if modus not in ("alle", "offen", "auswahl"):
            abort(400)
        try:
            pdf, _ = serienbrief(db, projekt_id, modus, uid(), posten_ids=request.form.getlist("posten"),
                                 text=request.form.get("text"))
            return pdf_antwort(f"Elternbriefe_{p['kurzcode']}_{date.today().isoformat()}.pdf", pdf)
        except BuchungsFehler as exc:
            flash(str(exc), "fehler")
    posten = posten_fuer_brief(db, projekt_id, "alle")
    if not ist_kasse():
        posten = [x for x in posten if not x["but_status"]]
    from ..briefvorlage import platzhalter
    text = p["brieftext"] or cfg["standardtext"]
    offen = platzhalter(cfg) + (["brieftext"] if "[PLATZHALTER" in text else [])
    return render_template("briefe.html", p=p, posten=posten, text=text, cfg=cfg, platzhalter=offen)


# ------------------------------------------------------- payment status F8.2

@bp.route("/projekte/<int:projekt_id>/zahlungsstand")
@login_erforderlich
def zahlungsstand_view(projekt_id):
    db = get_db()
    p = _projekt(projekt_id)
    fmt = request.args.get("format")
    status = request.args.get("status") or None
    sortierung = request.args.get("sort", "klasse")
    # Exports may leave the office: never contain BuT children or waived items.
    fuer_dritte = fmt in ("csv", "pdf") or not ist_kasse()
    rows = zahlungsstand(db, projekt_id, fuer_dritte=fuer_dritte, status=status, sortierung=sortierung)
    if fmt == "csv":
        return csv_antwort(f"Zahlungsstand_{p['kurzcode']}.csv",
                           ["Klasse", "Nachname", "Vorname", "Zahlcode", "Soll (EUR)", "Bezahlt (EUR)", "Offen (EUR)",
                            "Status", "Fällig am", "Letzter Brief"],
                           [[r["klasse"], r["nachname"], r["vorname"], r["zahlcode"] or "",
                             format_cent_csv(r["sollbetrag_cent"]), format_cent_csv(r["bezahlt"]),
                             format_cent_csv(max(0, r["sollbetrag_cent"] - r["bezahlt"])), r["status"],
                             r["faellig_am"] or "", (r["letzter_brief_am"] or "")[:10]] for r in rows])
    if fmt == "pdf":
        html = render_template("pdf/zahlungsstand.html", p=p, rows=rows, kz=projekt_kennzahlen(db, projekt_id),
                               erzeugt=utcnow(), user=g.user, cfg=brief_config())
        return pdf_antwort(f"Zahlungsstand_{p['kurzcode']}.pdf", html_zu_pdf(html))
    return render_template("zahlungsstand.html", p=p, rows=rows, status=status, sortierung=sortierung,
                           kz=projekt_kennzahlen(db, projekt_id))


@bp.route("/projekte/<int:projekt_id>/jahresabschluss.pdf")
@rolle("admin", "kasse")
def jahresabschluss(projekt_id):
    """F8.6: document for the cash audit."""
    db = get_db()
    p = _projekt(projekt_id)
    belege = db.all("SELECT b.*, u.anzeigename AS von FROM beleg b JOIN benutzer u ON u.id=b.hochgeladen_von"
                    " WHERE b.projekt_id=? OR b.zuordnung_id IN (SELECT id FROM zuordnung WHERE projekt_id=?)"
                    " ORDER BY b.hochgeladen_am", (projekt_id, projekt_id))
    html = render_template("pdf/jahresabschluss.html", p=p, kz=projekt_kennzahlen(db, projekt_id),
                           umsaetze=projekt_umsaetze(db, projekt_id), posten=projekt_posten(db, projekt_id),
                           belege=belege, erzeugt=utcnow(), user=g.user, cfg=brief_config())
    protokolliere(db, "export.jahresabschluss", "projekt", projekt_id)
    return pdf_antwort(f"Jahresabschluss_{p['kurzcode']}.pdf", html_zu_pdf(html))


# ----------------------------------------------------------------- receipts

@bp.route("/belege/hochladen", methods=["POST"])
@rolle("admin", "kasse")
def beleg_hochladen():
    db = get_db()
    projekt_id = int_or_none(request.form.get("projekt_id"))
    zuordnung_id = int_or_none(request.form.get("zuordnung_id"))
    ziel = request.form.get("zurueck") or url_for("start.dashboard")
    datei = request.files.get("datei")
    if not datei or not datei.filename:
        flash("Bitte eine Datei auswählen.", "fehler")
        return redirect(ziel)
    endung = Path(datei.filename).suffix.lower()
    if endung not in ERLAUBTE_BELEGE:
        flash("Zulässig sind PDF, JPG und PNG.", "fehler")
        return redirect(ziel)
    if zuordnung_id:
        z = db.one("SELECT projekt_id FROM zuordnung WHERE id=?", (zuordnung_id,))
        if z is None:
            abort(404)
        projekt_id = projekt_id or z["projekt_id"]
    if not projekt_id or not db.one("SELECT id FROM projekt WHERE id=?", (projekt_id,)):
        abort(404)
    ordner = Path(current_app.config["UPLOAD_DIR"])
    ordner.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{endung}"
    pfad = ordner / name
    datei.save(pfad)
    with db.tx():
        bid = db.insert("INSERT INTO beleg (projekt_id, zuordnung_id, dateiname, pfad, mimetype, groesse_bytes,"
                        " hochgeladen_von, hochgeladen_am) VALUES (?,?,?,?,?,?,?,?)",
                        (projekt_id, zuordnung_id, Path(datei.filename).name, name, ERLAUBTE_BELEGE[endung],
                         pfad.stat().st_size, uid(), utcnow()))
        protokolliere(db, "beleg.hochladen", "beleg", bid, neu={"dateiname": datei.filename, "projekt_id": projekt_id,
                                                              "zuordnung_id": zuordnung_id})
    flash("Beleg gespeichert.", "ok")
    return redirect(ziel if ziel.startswith("/") and not ziel.startswith("//") else url_for("start.dashboard"))


@bp.route("/belege/<int:beleg_id>")
@login_erforderlich
def beleg(beleg_id):
    db = get_db()
    b = db.one("SELECT * FROM beleg WHERE id=?", (beleg_id,))
    if b is None:
        abort(404)
    _projekt(b["projekt_id"])
    pfad = Path(current_app.config["UPLOAD_DIR"]) / Path(b["pfad"]).name
    if not pfad.is_file():
        abort(404)
    return send_file(pfad, mimetype=b["mimetype"] or mimetypes.guess_type(b["dateiname"])[0],
                     download_name=b["dateiname"], as_attachment=False)
