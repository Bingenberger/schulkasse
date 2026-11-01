"""Start of bookkeeping (Stichtag) and distribution of the opening balance.

The opening balance is the account balance at the start of the Stichtag:
the balance before the first imported transaction plus all booked
transactions before the Stichtag. It is distributed to projects ("Töpfe")
and optionally to items that were already paid before the Stichtag.
"""
from .buchung import AKTIV_SQL, BuchungsFehler, aktualisiere_posten_status
from .db import utcnow
from .geld import format_cent
from .protokoll import protokolliere


def _datum(iso):
    return "{2}.{1}.{0}".format(*iso.split("-")) if iso else ""


def _konto(db, konto_id):
    k = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
    if k is None:
        raise BuchungsFehler("Das Konto existiert nicht.")
    return k


def berechne(db, konto):
    """Opening balance at the start of bookkeeping.

    Returns a dict with stichtag, betrag (None if it cannot be determined yet)
    and an explanatory hinweis.
    """
    eroeffnung = konto["eroeffnungssaldo_cent"]
    beginn = konto["beginn_datum"]
    if eroeffnung is None:
        return {"stichtag": beginn, "betrag": None,
                "hinweis": "Noch kein Kontoauszug mit Saldo importiert – der Anfangsbestand ist noch unbekannt."}
    if not beginn:
        return {"stichtag": konto["eroeffnungssaldo_datum"], "betrag": eroeffnung, "hinweis": None}
    if konto["eroeffnungssaldo_datum"] and beginn < konto["eroeffnungssaldo_datum"]:
        return {"stichtag": beginn, "betrag": None,
                "hinweis": f"Der Stichtag liegt vor dem ersten importierten Kontoauszug "
                           f"({_datum(konto['eroeffnungssaldo_datum'])}). Bitte ältere Auszüge importieren "
                           "oder einen späteren Stichtag wählen."}
    vorher = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                       " AND buchungsdatum < ?", (konto["id"], beginn), 0)
    return {"stichtag": beginn, "betrag": eroeffnung + vorher, "hinweis": None}


def verteilt(db, konto_id):
    return db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM anfangsbestand WHERE konto_id=? AND storniert=0",
                     (konto_id,), 0)


def saldenabgleich(db, konto):
    """Compare each imported closing balance with opening balance plus booked
    transactions. Deviations indicate missing statements (gaps).

    Banks may split statements in the middle of a day: the closing balance of
    one file then contains only part of that day's bookings, the rest comes
    with the next file. On the balance date itself only the bookings brought
    by the same file (same content hash) are therefore certain; a balance is
    accepted if it matches either the whole day or that file's share of it.
    A file imported several times is checked once.
    """
    if konto["eroeffnungssaldo_cent"] is None:
        return []
    abweichungen = []
    geprueft = set()
    for imp in db.all("SELECT dateiname, datei_hash, saldo_datum, saldo_schluss_cent FROM import WHERE konto_id=?"
                      " AND saldo_schluss_cent IS NOT NULL AND saldo_datum IS NOT NULL ORDER BY saldo_datum, id",
                      (konto["id"],)):
        schluessel = (imp["datei_hash"], imp["saldo_datum"], imp["saldo_schluss_cent"])
        if schluessel in geprueft:
            continue
        geprueft.add(schluessel)
        vorher = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                           " AND buchungsdatum < ?", (konto["id"], imp["saldo_datum"]), 0)
        ganzer_tag = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=?"
                               " AND status='BOOK' AND buchungsdatum = ?", (konto["id"], imp["saldo_datum"]), 0)
        anteil_datei = db.scalar(
            "SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
            " AND buchungsdatum = ? AND import_id IN (SELECT id FROM import WHERE konto_id=? AND datei_hash=?)",
            (konto["id"], imp["saldo_datum"], konto["id"], imp["datei_hash"]), 0)
        basis = konto["eroeffnungssaldo_cent"] + vorher
        if imp["saldo_schluss_cent"] not in (basis + ganzer_tag, basis + anteil_datei):
            abweichungen.append({"dateiname": imp["dateiname"], "datum": imp["saldo_datum"],
                                 "bank": imp["saldo_schluss_cent"], "berechnet": basis + ganzer_tag})
    return abweichungen


def stichtag_setzen(db, konto_id, datum, benutzer_id):
    """Set (or clear with datum=None) the start of bookkeeping."""
    with db.tx():
        k = _konto(db, konto_id)
        if datum:
            if k["eroeffnungssaldo_datum"] and datum < k["eroeffnungssaldo_datum"]:
                raise BuchungsFehler(
                    f"Der Stichtag darf nicht vor dem ersten importierten Kontoauszug "
                    f"({_datum(k['eroeffnungssaldo_datum'])}) liegen.")
            n = db.scalar("SELECT COUNT(DISTINCT u.id) FROM bankumsatz u JOIN zuordnung z ON z.bankumsatz_id=u.id"
                          " WHERE u.konto_id=? AND u.buchungsdatum < ? AND z.storniert=0", (konto_id, datum), 0)
            m = db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=? AND buchungsdatum < ?"
                          " AND zuordnungsstatus='ignoriert'", (konto_id, datum), 0)
            if n or m:
                raise BuchungsFehler(
                    f"Vor dem {_datum(datum)} gibt es bereits {n} zugeordnete und {m} ignorierte Umsätze. "
                    "Ihr Geld wäre sonst doppelt gezählt (im Anfangsbestand und in den Projekten). Bitte diese "
                    "Zuordnungen zuerst stornieren bzw. das Ignorieren aufheben oder einen früheren Stichtag wählen.")
            db.execute("DELETE FROM vorschlag WHERE bankumsatz_id IN (SELECT id FROM bankumsatz"
                       " WHERE konto_id=? AND buchungsdatum < ?)", (konto_id, datum))
        db.execute("UPDATE konto SET beginn_datum=? WHERE id=?", (datum or None, konto_id))
        protokolliere(db, "konto.beginn_datum", "konto", konto_id, alt={"beginn_datum": k["beginn_datum"]},
                      neu={"beginn_datum": datum or None}, benutzer_id=benutzer_id)
    return [r[0] for r in db.all(
        f"SELECT u.id FROM bankumsatz u WHERE u.konto_id=? AND u.status='BOOK' AND u.zuordnungsstatus='offen'"
        f" AND {AKTIV_SQL}", (konto_id,))]


def verteilen(db, konto_id, teile, benutzer_id):
    """Distribute (part of) the opening balance. `teile`: dicts with projekt_id,
    offener_posten_id (optional), betrag_cent (signed) and notiz."""
    if not teile:
        raise BuchungsFehler("Bitte mindestens einen Betrag angeben.")
    with db.tx():
        k = _konto(db, konto_id)
        ab = berechne(db, k)
        if ab["betrag"] is None:
            raise BuchungsFehler(ab["hinweis"])
        geprueft = []
        for t in teile:
            betrag = int(t["betrag_cent"])
            if betrag == 0:
                raise BuchungsFehler("Beträge müssen ungleich 0,00 € sein.")
            posten_id = t.get("offener_posten_id") or None
            projekt_id = t.get("projekt_id")
            if posten_id:
                p = db.one("SELECT * FROM offener_posten WHERE id=?", (posten_id,))
                if p is None:
                    raise BuchungsFehler("Der offene Posten existiert nicht.")
                if p["status"] == "storniert":
                    raise BuchungsFehler(f"Der Posten „{p['bezeichnung']}“ ist storniert.")
                if (p["richtung"] == "eingang") != (betrag > 0):
                    raise BuchungsFehler(
                        "Ein vor dem Stichtag eingegangener Beitrag wird mit positivem Betrag verbucht, "
                        "eine vor dem Stichtag geleistete Zahlung (Ausgangsposten) mit negativem Betrag.")
                if projekt_id and int(projekt_id) != p["projekt_id"]:
                    raise BuchungsFehler("Der Posten gehört zu einem anderen Projekt.")
                projekt_id = p["projekt_id"]
            pr = db.one("SELECT * FROM projekt WHERE id=?", (projekt_id,))
            if pr is None:
                raise BuchungsFehler("Bitte für jeden Betrag ein Projekt wählen.")
            if pr["status"] != "offen":
                raise BuchungsFehler(f"Das Projekt „{pr['bezeichnung']}“ ist abgeschlossen und schreibgeschützt.")
            geprueft.append((projekt_id, posten_id, betrag, (t.get("notiz") or "").strip() or None))
        neu = verteilt(db, konto_id) + sum(b for _, _, b, _ in geprueft)
        if (ab["betrag"] >= 0 and neu > ab["betrag"]) or (ab["betrag"] < 0 and neu < ab["betrag"]):
            raise BuchungsFehler(
                f"Damit würde mehr verteilt als vorhanden: Anfangsbestand {format_cent(ab['betrag'])}, "
                f"noch zu verteilen {format_cent(ab['betrag'] - verteilt(db, konto_id))}.")
        jetzt = utcnow()
        ids = []
        for projekt_id, posten_id, betrag, notiz in geprueft:
            aid = db.insert("INSERT INTO anfangsbestand (konto_id, projekt_id, offener_posten_id, betrag_cent, notiz,"
                            " erstellt_von, erstellt_am) VALUES (?,?,?,?,?,?,?)",
                            (konto_id, projekt_id, posten_id, betrag, notiz, benutzer_id, jetzt))
            ids.append(aid)
            protokolliere(db, "anfangsbestand.anlegen", "anfangsbestand", aid, neu={
                "konto_id": konto_id, "projekt_id": projekt_id, "offener_posten_id": posten_id,
                "betrag_cent": betrag, "notiz": notiz, "stichtag": ab["stichtag"]}, benutzer_id=benutzer_id)
            if posten_id:
                aktualisiere_posten_status(db, posten_id)
        return ids


def stornieren(db, anfangsbestand_id, grund, benutzer_id):
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Für ein Storno ist eine Begründung erforderlich.")
    with db.tx():
        a = db.one("SELECT * FROM anfangsbestand WHERE id=?", (anfangsbestand_id,))
        if a is None:
            raise BuchungsFehler("Der Eintrag existiert nicht.")
        if a["storniert"]:
            raise BuchungsFehler("Der Eintrag ist bereits storniert.")
        pr = db.one("SELECT status, bezeichnung FROM projekt WHERE id=?", (a["projekt_id"],))
        if pr["status"] != "offen":
            raise BuchungsFehler(f"Das Projekt „{pr['bezeichnung']}“ ist abgeschlossen und schreibgeschützt.")
        db.execute("UPDATE anfangsbestand SET storniert=1, storniert_von=?, storniert_am=?, storno_grund=?"
                   " WHERE id=?", (benutzer_id, utcnow(), grund, anfangsbestand_id))
        protokolliere(db, "anfangsbestand.stornieren", "anfangsbestand", anfangsbestand_id,
                      alt={"storniert": 0}, neu={"storniert": 1, "storno_grund": grund}, benutzer_id=benutzer_id)
        if a["offener_posten_id"]:
            aktualisiere_posten_status(db, a["offener_posten_id"])
    return a


def eintraege(db, konto_id=None, projekt_id=None, posten_id=None):
    sql = ("SELECT a.*, ko.bezeichnung AS konto, COALESCE(ko.beginn_datum, ko.eroeffnungssaldo_datum) AS stichtag,"
           " pr.bezeichnung AS projekt, pr.kurzcode, p.zahlcode, p.bezeichnung AS posten, k.vorname, k.nachname,"
           " b.anzeigename AS von, s.anzeigename AS storno_von"
           " FROM anfangsbestand a JOIN konto ko ON ko.id=a.konto_id JOIN projekt pr ON pr.id=a.projekt_id"
           " LEFT JOIN offener_posten p ON p.id=a.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
           " JOIN benutzer b ON b.id=a.erstellt_von LEFT JOIN benutzer s ON s.id=a.storniert_von WHERE 1=1")
    params = []
    for spalte, wert in (("a.konto_id", konto_id), ("a.projekt_id", projekt_id), ("a.offener_posten_id", posten_id)):
        if wert is not None:
            sql += f" AND {spalte}=?"
            params.append(wert)
    return db.all(sql + " ORDER BY a.storniert, a.id", params)
