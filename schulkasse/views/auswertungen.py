"""Reports: overpayments, reminders, cash book (F3.6, F8.4, F8.5)."""
from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from ..auswertung import erinnerungsliste, kassenbuch, kontenverteilung, ueberzahlungen
from ..auth import ist_kasse, login_erforderlich, rolle
from ..briefe import brief_config, html_zu_pdf, serienbrief
from ..buchung import BuchungsFehler
from ..db import get_db, utcnow
from ..geld import format_cent_csv
from ..monatsbericht import monat_aus_text, monatsbericht
from ..umbuchung import konto_setzen, ohne_konto
from ..protokoll import protokolliere
from .util import csv_antwort, datum_or_none, int_or_none, pdf_antwort, uid

bp = Blueprint("auswertungen", __name__, url_prefix="/auswertungen")


@bp.route("/ueberzahlungen")
@rolle("admin", "kasse")
def ueberzahlungen_view():
    db = get_db()
    return render_template("ueberzahlungen.html", rows=ueberzahlungen(db), geklaert=ueberzahlungen(db, geklaert=True))


@bp.route("/kontenverteilung")
@rolle("admin", "kasse")
def kontenverteilung_view():
    """F8.1: which part of every project's balance sits on which account."""
    db = get_db()
    nur_aufgeteilt = request.args.get("nur") == "aufgeteilt"
    v = kontenverteilung(db, nur_aufgeteilt=nur_aufgeteilt)
    if request.args.get("format") == "csv":
        kopf = ["Projekt", "Kurzcode", "Status"] + [k["bezeichnung"] for k in v["konten"]]
        if v["ohne_konto"]:
            kopf.append("ohne Konto")
        kopf.append("Summe")
        zeilen = []
        for p in v["projekte"]:
            zeile = [p["bezeichnung"], p["kurzcode"], p["status"]]
            zeile += [format_cent_csv(p["betraege"].get(k["id"], 0)) for k in v["konten"]]
            if v["ohne_konto"]:
                zeile.append(format_cent_csv(p["ohne_konto"]))
            zeile.append(format_cent_csv(p["summe"]))
            zeilen.append(zeile)
        summe = ["Summe", "", ""] + [format_cent_csv(v["summen"][k["id"]]) for k in v["konten"]]
        if v["ohne_konto"]:
            summe.append(format_cent_csv(v["ohne_konto"]))
        zeilen.append(summe + [format_cent_csv(v["gesamt"])])
        return csv_antwort(f"Kontenverteilung_{date.today().isoformat()}.csv", kopf, zeilen)
    if request.args.get("format") == "pdf":
        html = render_template("pdf/kontenverteilung.html", v=v, cfg=brief_config(), heute=date.today(),
                               nur_aufgeteilt=nur_aufgeteilt)
        return pdf_antwort(f"Kontenverteilung_{date.today().isoformat()}.pdf", html_zu_pdf(html))
    return render_template("kontenverteilung.html", v=v, nur_aufgeteilt=nur_aufgeteilt,
                           ohne_konto=ohne_konto(db), konten=db.all("SELECT * FROM konto ORDER BY id"))


@bp.route("/umbuchungen/<int:umbuchung_id>/konto", methods=["POST"])
@rolle("admin", "kasse")
def umbuchung_konto(umbuchung_id):
    """Add the account to a transfer booked before accounts were recorded."""
    db = get_db()
    try:
        konto = konto_setzen(db, umbuchung_id, int_or_none(request.form.get("konto_id")), uid())
        flash(f"Umbuchung dem Konto „{konto['bezeichnung']}“ zugeordnet.", "ok")
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
    return redirect(request.form.get("zurueck") or url_for("auswertungen.kontenverteilung_view"))


def _bericht_buchungen_csv(v):
    """One row per transaction of the month, with account and projects."""
    zeilen = []
    for k in v["konten"]:
        for z in k["kb"]["zeilen"]:
            u = z["umsatz"]
            if z["ignoriert_grund"]:
                zuordnung = f"ignoriert: {z['ignoriert_grund']}"
            elif u["buchungsdatum"] < (k["kb"]["beginn"] or ""):
                zuordnung = "vor Beginn der Kontoführung (im Anfangsbestand)"
            else:
                zuordnung = "; ".join(f"{t['kurzcode']} {t['projekt']}"
                                      f"{' / ' + t['zahlcode'] if t['zahlcode'] else ''}: "
                                      f"{format_cent_csv(t['betrag_cent'])}" for t in z["projekte"])
                if u["zuordnungsstatus"] in ("offen", "teilweise"):
                    zuordnung = (zuordnung + " " if zuordnung else "") + "(noch nicht vollständig zugeordnet)"
            zeilen.append([k["konto"]["bezeichnung"], u["buchungsdatum"], u["gegen_name"] or "",
                           u["gegen_iban"] or "", u["verwendungszweck"] or "",
                           format_cent_csv(u["betrag_cent"]), zuordnung])
    return zeilen


@bp.route("/monatsbericht")
@rolle("admin", "kasse")
def monatsbericht_view():
    """Monthly report for the account holder (city): statement plus projects."""
    db = get_db()
    konten = db.all("SELECT * FROM konto ORDER BY id")
    konto_id = int_or_none(request.args.get("konto"))
    fmt = request.args.get("format")
    try:
        jahr, monat = monat_aus_text(request.args.get("monat"))
        v = monatsbericht(db, jahr, monat, konto_id=konto_id)
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
        return redirect(url_for("auswertungen.monatsbericht_view"))
    monatstext = f"{v['jahr']}-{v['monat']:02d}"
    if fmt:
        protokolliere(db, f"export.monatsbericht.{fmt}", "konto", konto_id, neu={"monat": monatstext})
    if fmt == "csv":
        return csv_antwort(f"Monatsbericht_{monatstext}_Buchungen.csv",
                           ["Konto", "Buchungsdatum", "Gegenseite", "IBAN", "Verwendungszweck", "Betrag (EUR)",
                            "Projekt / Zuordnung"], _bericht_buchungen_csv(v))
    if fmt == "csv-projekte":
        kopf = ["Projekt", "Kurzcode", "Status", f"Stand {v['vortag']}", "Anfangsbestand", "Eingänge", "Ausgänge",
                "Umbuchungen", f"Stand {v['bis']}"] + [k["konto"]["bezeichnung"] for k in v["konten"]]
        zeilen = [[p["bezeichnung"], p["kurzcode"], p["status"], format_cent_csv(p["stand_anfang"]),
                   format_cent_csv(p["anfangsbestand"]), format_cent_csv(p["eingang"]),
                   format_cent_csv(p["ausgang"]), format_cent_csv(p["umbuchung"]),
                   format_cent_csv(p["stand_ende"])]
                  + [format_cent_csv(p["je_konto"].get(k["konto"]["id"], 0)) for k in v["konten"]]
                  for p in v["projekte"]]
        return csv_antwort(f"Monatsbericht_{monatstext}_Projekte.csv", kopf, zeilen)
    if fmt == "pdf":
        html = render_template("pdf/monatsbericht.html", v=v, cfg=brief_config(), erzeugt=utcnow(), user=g.user)
        return pdf_antwort(f"Monatsbericht_{monatstext}.pdf", html_zu_pdf(html))
    return render_template("monatsbericht.html", v=v, konten=konten, konto_id=konto_id, monat=monatstext)


def _eigene_projekte(db):
    if ist_kasse():
        return None
    return [r[0] for r in db.all("SELECT id FROM projekt WHERE verantwortlich_user_id=?", (g.user["id"],))]


@bp.route("/erinnerungen")
@login_erforderlich
def erinnerungen():
    """F8.4: overdue items, never including BuT children or waived items."""
    db = get_db()
    stichtag = datum_or_none(request.args.get("stichtag")) or date.today().isoformat()
    projekt_id = int_or_none(request.args.get("projekt"))
    eigene = _eigene_projekte(db)
    gruppen = erinnerungsliste(db, stichtag, projekt_id, nur_projekt_ids=eigene)
    sql = "SELECT id, bezeichnung, kurzcode FROM projekt WHERE status='offen' AND typ='elternbeitrag'"
    params = []
    if eigene is not None:
        sql += f" AND id IN ({','.join('?' * len(eigene)) or 'NULL'})"
        params = eigene
    projekte = db.all(sql + " ORDER BY bezeichnung", params)
    if request.args.get("format") == "csv":
        zeilen = [[r["klasse"], r["nachname"], r["vorname"], r["projekt"], r["zahlcode"],
                   format_cent_csv(r["sollbetrag_cent"] - r["bezahlt"]), r["faellig_am"]]
                  for rows in gruppen.values() for r in rows]
        return csv_antwort(f"Erinnerungsliste_{stichtag}.csv",
                           ["Klasse", "Nachname", "Vorname", "Projekt", "Zahlcode", "Offen (EUR)", "Fällig am"], zeilen)
    return render_template("erinnerungen.html", gruppen=gruppen, stichtag=stichtag, projekte=projekte,
                           projekt_id=projekt_id)


@bp.route("/erinnerungen/briefe", methods=["POST"])
@login_erforderlich
def erinnerungsbriefe():
    """Reminder letters reuse the letter template (F7) with the open remainder."""
    db = get_db()
    projekt_id = int_or_none(request.form.get("projekt"))
    eigene = _eigene_projekte(db)
    if not projekt_id or (eigene is not None and projekt_id not in eigene):
        flash("Bitte ein Projekt wählen.", "fehler")
        return redirect(url_for("auswertungen.erinnerungen"))
    stichtag = datum_or_none(request.form.get("stichtag")) or date.today().isoformat()
    ids = [r["id"] for rows in erinnerungsliste(db, stichtag, projekt_id).values() for r in rows]
    try:
        pdf, _ = serienbrief(db, projekt_id, "auswahl", uid(), posten_ids=ids, erinnerung=True,
                             text=request.form.get("text") or None)
    except BuchungsFehler as exc:
        flash(str(exc), "fehler")
        return redirect(url_for("auswertungen.erinnerungen", projekt=projekt_id, stichtag=stichtag))
    return pdf_antwort(f"Erinnerungen_{stichtag}.pdf", pdf)


@bp.route("/kassenbuch")
@rolle("admin", "kasse")
def kassenbuch_view():
    """F8.5"""
    db = get_db()
    konten = db.all("SELECT * FROM konto ORDER BY id")
    konto_id = int_or_none(request.args.get("konto")) or (konten[0]["id"] if konten else None)
    heute = date.today()
    von = datum_or_none(request.args.get("von")) or date(heute.year, 1, 1).isoformat()
    bis = datum_or_none(request.args.get("bis")) or heute.isoformat()
    if konto_id is None:
        return render_template("kassenbuch.html", kb=None, konten=konten)
    kb = kassenbuch(db, konto_id, von, bis)
    fmt = request.args.get("format")
    if fmt in ("csv", "pdf"):
        protokolliere(db, f"export.kassenbuch.{fmt}", "konto", konto_id, neu={"von": von, "bis": bis})
    if fmt == "csv":
        zeilen = [["", "Anfangssaldo", "", "", "", format_cent_csv(kb["anfangssaldo"]), ""]]
        for z in kb["zeilen"]:
            u = z["umsatz"]
            if z["ignoriert_grund"]:
                zuordnung = f"ignoriert: {z['ignoriert_grund']}"
            else:
                zuordnung = "; ".join(f"{t['kurzcode']}{' ' + t['zahlcode'] if t['zahlcode'] else ''}: "
                                      f"{format_cent_csv(t['betrag_cent'])}" for t in z["teile"])
                if u["buchungsdatum"] < kb["beginn"]:
                    zuordnung = "vor Beginn der Kontoführung (im Anfangsbestand)"
                elif u["zuordnungsstatus"] in ("offen", "teilweise"):
                    zuordnung = (zuordnung + " " if zuordnung else "") + "(nicht vollständig zugeordnet)"
            zeilen.append([u["buchungsdatum"], u["gegen_name"] or "", u["gegen_iban"] or "", u["verwendungszweck"] or "",
                           u["geschaeftsvorfall"] or "", format_cent_csv(u["betrag_cent"]), zuordnung])
        zeilen += [["", "Summe Eingänge", "", "", "", format_cent_csv(kb["eingaenge"]), ""],
                   ["", "Summe Ausgänge", "", "", "", format_cent_csv(kb["ausgaenge"]), ""],
                   ["", "Endsaldo", "", "", "", format_cent_csv(kb["endsaldo"]), ""],
                   ["", f"Erzeugt am {utcnow()} UTC von {g.user['anzeigename']}", "", "", "", "", ""]]
        return csv_antwort(f"Kassenbuch_{von}_{bis}.csv",
                           ["Buchungsdatum", "Gegenseite", "IBAN", "Verwendungszweck", "Vorgang", "Betrag (EUR)",
                            "Zuordnung"], zeilen)
    if fmt == "pdf":
        html = render_template("pdf/kassenbuch.html", kb=kb, erzeugt=utcnow(), user=g.user, cfg=brief_config())
        return pdf_antwort(f"Kassenbuch_{von}_{bis}.pdf", html_zu_pdf(html))
    return render_template("kassenbuch.html", kb=kb, konten=konten, konto_id=konto_id, von=von, bis=bis)
