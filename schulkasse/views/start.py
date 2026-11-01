"""Dashboard and search."""
from flask import Blueprint, g, render_template, request

from ..auswertung import anzahl_ueberzahlungen, kontrollrechnung, projekte_uebersicht
from ..auth import login_erforderlich, rolle
from ..buchung import AKTIV_SQL
from ..db import get_db
from ..geld import BetragFehler, parse_betrag
from ..stammdaten import aktives_schuljahr

bp = Blueprint("start", __name__)


@bp.route("/")
@login_erforderlich
def dashboard():
    db = get_db()
    sj = aktives_schuljahr(db)
    if g.user["rolle"] == "lehrkraft":
        projekte = projekte_uebersicht(db, nur_user_id=g.user["id"], status="offen")
        return render_template("dashboard_lehrkraft.html", projekte=projekte, schuljahr=sj)
    kontrolle = kontrollrechnung(db)
    zahlen = {
        "offen": db.scalar("SELECT COUNT(*) FROM bankumsatz u WHERE status='BOOK' AND zuordnungsstatus='offen'"
                           f" AND {AKTIV_SQL}", default=0),
        "teilweise": db.scalar("SELECT COUNT(*) FROM bankumsatz u WHERE status='BOOK' AND zuordnungsstatus='teilweise'"
                               f" AND {AKTIV_SQL}",
                               default=0),
        "vorschlaege": db.scalar("SELECT COUNT(DISTINCT bankumsatz_id) FROM vorschlag", default=0),
        "vorgemerkt": db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE status='PDNG'", default=0),
        "ueberzahlungen": anzahl_ueberzahlungen(db),
    }
    importe = db.all("SELECT i.*, k.bezeichnung AS konto FROM import i JOIN konto k ON k.id=i.konto_id"
                     " ORDER BY i.id DESC LIMIT 5")
    projekte = projekte_uebersicht(db, schuljahr_id=sj["id"] if sj else None, status="offen")
    return render_template("dashboard.html", kontrolle=kontrolle, zahlen=zahlen, importe=importe,
                           projekte=projekte, schuljahr=sj,
                           konten=db.scalar("SELECT COUNT(*) FROM konto", default=0))


@bp.route("/suche")
@rolle("admin", "kasse")
def suche():
    """F5.7: full text search over remittance text, name and amount."""
    db = get_db()
    q = (request.args.get("q") or "").strip()
    treffer = []
    if q:
        bedingungen = ["u.verwendungszweck LIKE ?", "u.verwendungszweck_roh LIKE ?", "u.gegen_name LIKE ?",
                       "u.gegen_iban LIKE ?"]
        muster = f"%{q}%"
        params = [muster, muster, muster, muster.replace(" ", "")]
        try:
            cent = parse_betrag(q)
            bedingungen.append("ABS(u.betrag_cent)=?")
            params.append(abs(cent))
        except BetragFehler:
            pass
        treffer = db.all(
            "SELECT u.*, k.bezeichnung AS konto, (u.buchungsdatum < COALESCE(k.beginn_datum, '')) AS vor_beginn FROM bankumsatz u JOIN konto k ON k.id=u.konto_id"
            f" WHERE {' OR '.join(bedingungen)} ORDER BY u.buchungsdatum DESC, u.id DESC LIMIT 200", params)
        posten = db.all(
            "SELECT p.*, pr.bezeichnung AS projekt, k.vorname, k.nachname FROM offener_posten p"
            " JOIN projekt pr ON pr.id=p.projekt_id LEFT JOIN kind k ON k.id=p.kind_id"
            " WHERE p.zahlcode LIKE ? OR (k.vorname || ' ' || k.nachname) LIKE ? OR k.nachname LIKE ?"
            " ORDER BY p.zahlcode LIMIT 50", (muster, muster, muster))
    else:
        posten = []
    return render_template("suche.html", q=q, treffer=treffer, posten=posten)
