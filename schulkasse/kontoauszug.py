"""Bank statements and monitoring lists (Richtlinie Schulgirokonten, § 8).

The CAMT.052 files of the bank contain no statement numbers, so statements are
recorded by hand (number, period, closing balance as printed). Every booked
transaction whose booking date lies in the period belongs to the statement.
The monitoring list ("Überwachungsliste", Anlage 1) is derived from it.
"""
import hashlib
import json
from datetime import date, timedelta

from .buchung import BuchungsFehler
from .db import utcnow
from .projekte import KLASSEN_SQL
from .protokoll import protokolliere


def _naechster_tag(datum):
    return (date.fromisoformat(datum) + timedelta(days=1)).isoformat()


def _monatsende(datum):
    d = date.fromisoformat(datum)
    erster_naechster = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (erster_naechster - timedelta(days=1)).isoformat()


def bezeichnung(auszug):
    return f"{auszug['nummer']}/{auszug['jahr']}"


def _saldo_vor(db, konto, datum):
    """Account balance at the start of `datum` (opening balance + booked transactions before)."""
    vorher = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                       " AND buchungsdatum < ?", (konto["id"], datum), 0)
    return (konto["eroeffnungssaldo_cent"] or 0) + vorher


def _summen(db, konto_id, von, bis):
    r = db.one("SELECT COUNT(*) AS anzahl,"
               " COALESCE(SUM(CASE WHEN betrag_cent > 0 THEN betrag_cent END),0) AS ein,"
               " COALESCE(SUM(CASE WHEN betrag_cent < 0 THEN betrag_cent END),0) AS aus"
               " FROM bankumsatz WHERE konto_id=? AND status='BOOK' AND buchungsdatum BETWEEN ? AND ?",
               (konto_id, von, bis))
    return r["anzahl"], r["ein"], r["aus"]


def _vorgaenger(db, auszug):
    return db.one("SELECT * FROM kontoauszug WHERE konto_id=? AND datum_bis < ? ORDER BY datum_bis DESC LIMIT 1",
                  (auszug["konto_id"], auszug["datum_von"]))


def pruefungen(db, konto, auszug, anfang, ende):
    """Consistency checks of one statement, as list of (level, text)."""
    hinweise = []
    if auszug["endsaldo_cent"] is None:
        hinweise.append(("warn", "Endsaldo laut Kontoauszug noch nicht eingetragen."))
    elif auszug["endsaldo_cent"] != ende:
        hinweise.append(("fehler", f"Endsaldo laut Kontoauszug weicht um "
                                   f"{_euro(auszug['endsaldo_cent'] - ende)} vom berechneten Endsaldo ab – "
                                   "fehlen Umsätze oder stimmt der Zeitraum nicht?"))
    vor = _vorgaenger(db, auszug)
    if vor is not None:
        if _naechster_tag(vor["datum_bis"]) != auszug["datum_von"]:
            luecke = db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                               " AND buchungsdatum > ? AND buchungsdatum < ?",
                               (konto["id"], vor["datum_bis"], auszug["datum_von"]), 0)
            text = f"Lücke zum vorherigen Auszug {bezeichnung(vor)} (endet {_de(vor['datum_bis'])})"
            hinweise.append(("fehler" if luecke else "info",
                             text + (f": {luecke} Umsätze liegen in keinem Auszug." if luecke else ".")))
        if vor["endsaldo_cent"] is not None and vor["endsaldo_cent"] != anfang:
            hinweise.append(("fehler", f"Anfangssaldo {_euro(anfang)} entspricht nicht dem Endsaldo des Auszugs "
                                       f"{bezeichnung(vor)} ({_euro(vor['endsaldo_cent'])})."))
    vorgemerkt = db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=? AND status='PDNG'"
                           " AND buchungsdatum BETWEEN ? AND ?", (konto["id"], auszug["datum_von"],
                                                                  auszug["datum_bis"]), 0)
    if vorgemerkt:
        hinweise.append(("warn", f"{vorgemerkt} vorgemerkte (noch nicht gebuchte) Umsätze im Zeitraum – "
                                 "erst nach der Buchung Teil der Liste."))
    letzter_import = db.scalar("SELECT MAX(zeitraum_bis) FROM import WHERE konto_id=?", (konto["id"],))
    if not letzter_import or letzter_import < auszug["datum_bis"]:
        hinweise.append(("warn", "Für das Ende des Zeitraums wurden noch keine Umsätze importiert."))
    return hinweise


def _euro(cent):
    from .geld import format_cent
    return format_cent(cent)


def _de(datum):
    return date.fromisoformat(datum).strftime("%d.%m.%Y")


def liste(db, konto_id, jahr=None):
    """Statements of an account with computed balances and status."""
    konto = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
    if konto is None:
        return []
    sql = ("SELECT a.*, b.anzeigename AS geprueft_von_name FROM kontoauszug a"
           " LEFT JOIN benutzer b ON b.id=a.geprueft_von WHERE a.konto_id=?")
    params = [konto_id]
    if jahr:
        sql += " AND a.jahr=?"
        params.append(jahr)
    ergebnis = []
    for a in db.all(sql + " ORDER BY a.datum_von DESC, a.id DESC", params):
        anfang = _saldo_vor(db, konto, a["datum_von"])
        anzahl, ein, aus = _summen(db, konto_id, a["datum_von"], a["datum_bis"])
        ende = anfang + ein + aus
        hinweise = pruefungen(db, konto, a, anfang, ende)
        geaendert = bool(a["geprueft_am"]) and a["geprueft_hash"] != fingerabdruck(db, a["id"])
        ergebnis.append({"auszug": a, "anfang": anfang, "ende": ende, "eingaenge": ein, "ausgaenge": aus,
                         "anzahl": anzahl, "hinweise": hinweise, "geaendert": geaendert,
                         "fehler": any(h[0] == "fehler" for h in hinweise)})
    return ergebnis


def jahre(db, konto_id):
    return [r[0] for r in db.all("SELECT DISTINCT jahr FROM kontoauszug WHERE konto_id=? ORDER BY jahr DESC",
                                 (konto_id,))]


def ohne_auszug(db, konto_id):
    """Booked transactions from the first recorded statement on that are covered by none."""
    erster = db.scalar("SELECT MIN(datum_von) FROM kontoauszug WHERE konto_id=?", (konto_id,))
    if not erster:
        return {"anzahl": 0, "erstes": None}
    r = db.one("SELECT COUNT(*) AS anzahl, MIN(u.buchungsdatum) AS erstes FROM bankumsatz u"
               " WHERE u.konto_id=? AND u.status='BOOK' AND u.buchungsdatum >= ?"
               " AND NOT EXISTS (SELECT 1 FROM kontoauszug a WHERE a.konto_id=u.konto_id"
               "   AND u.buchungsdatum BETWEEN a.datum_von AND a.datum_bis)", (konto_id, erster))
    return {"anzahl": r["anzahl"], "erstes": r["erstes"]}


def vorschlag(db, konto_id):
    """Prefill for the next statement: number + 1, period after the last one."""
    letzter = db.one("SELECT * FROM kontoauszug WHERE konto_id=? ORDER BY datum_bis DESC, id DESC LIMIT 1",
                     (konto_id,))
    if letzter is None:
        konto = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
        von = (konto["beginn_datum"] if konto else None) or db.scalar(
            "SELECT MIN(buchungsdatum) FROM bankumsatz WHERE konto_id=? AND status='BOOK'", (konto_id,)) \
            or date.today().replace(day=1).isoformat()
        bis = _monatsende(von)
        return {"nummer": 1, "datum_von": von, "datum_bis": bis}
    von = _naechster_tag(letzter["datum_bis"])
    bis = _monatsende(von)
    nummer = letzter["nummer"] + 1 if bis[:4] == str(letzter["jahr"]) else 1
    return {"nummer": nummer, "datum_von": von, "datum_bis": bis}


def _gesperrt(auszug):
    if auszug["geprueft_am"]:
        raise BuchungsFehler(f"Der Kontoauszug {bezeichnung(auszug)} ist als geprüft vermerkt. "
                             "Bitte zuerst die Prüfung aufheben.")


def speichern(db, konto_id, werte, benutzer_id, auszug_id=None):
    """Create or change a statement. `werte`: nummer, datum_von, datum_bis, endsaldo_cent, notiz."""
    nummer, von, bis = werte.get("nummer"), werte.get("datum_von"), werte.get("datum_bis")
    if not nummer or nummer < 1:
        raise BuchungsFehler("Bitte die Nummer des Kontoauszugs angeben.")
    if not von or not bis:
        raise BuchungsFehler("Bitte den Zeitraum (von/bis) des Kontoauszugs angeben.")
    if von > bis:
        raise BuchungsFehler("Das Ende des Zeitraums liegt vor dem Anfang.")
    jahr = int(bis[:4])
    notiz = (werte.get("notiz") or "").strip() or None
    with db.tx():
        if db.one("SELECT id FROM konto WHERE id=?", (konto_id,)) is None:
            raise BuchungsFehler("Das Konto existiert nicht.")
        ueberschneidung = db.one("SELECT * FROM kontoauszug WHERE konto_id=? AND id<>? AND datum_von <= ?"
                                 " AND datum_bis >= ?", (konto_id, auszug_id or 0, bis, von))
        if ueberschneidung is not None:
            raise BuchungsFehler(f"Der Zeitraum überschneidet sich mit Kontoauszug {bezeichnung(ueberschneidung)} "
                                 f"({_de(ueberschneidung['datum_von'])} – {_de(ueberschneidung['datum_bis'])}).")
        doppelt = db.one("SELECT id FROM kontoauszug WHERE konto_id=? AND jahr=? AND nummer=? AND id<>?",
                         (konto_id, jahr, nummer, auszug_id or 0))
        if doppelt is not None:
            raise BuchungsFehler(f"Kontoauszug {nummer}/{jahr} ist für dieses Konto bereits erfasst.")
        neu = {"nummer": nummer, "jahr": jahr, "datum_von": von, "datum_bis": bis,
               "endsaldo_cent": werte.get("endsaldo_cent"), "notiz": notiz}
        if auszug_id:
            alt = db.one("SELECT * FROM kontoauszug WHERE id=? AND konto_id=?", (auszug_id, konto_id))
            if alt is None:
                raise BuchungsFehler("Der Kontoauszug existiert nicht.")
            _gesperrt(alt)
            db.execute("UPDATE kontoauszug SET nummer=?, jahr=?, datum_von=?, datum_bis=?, endsaldo_cent=?, notiz=?"
                       " WHERE id=?", (nummer, jahr, von, bis, neu["endsaldo_cent"], notiz, auszug_id))
            protokolliere(db, "kontoauszug.aendern", "kontoauszug", auszug_id, alt=alt, neu=neu,
                          benutzer_id=benutzer_id)
            return auszug_id
        neue_id = db.insert("INSERT INTO kontoauszug (konto_id, jahr, nummer, datum_von, datum_bis, endsaldo_cent,"
                            " notiz, erstellt_von, erstellt_am) VALUES (?,?,?,?,?,?,?,?,?)",
                            (konto_id, jahr, nummer, von, bis, neu["endsaldo_cent"], notiz, benutzer_id, utcnow()))
        protokolliere(db, "kontoauszug.anlegen", "kontoauszug", neue_id, neu=dict(neu, konto_id=konto_id),
                      benutzer_id=benutzer_id)
        return neue_id


def loeschen(db, auszug_id, benutzer_id):
    with db.tx():
        a = db.one("SELECT * FROM kontoauszug WHERE id=?", (auszug_id,))
        if a is None:
            raise BuchungsFehler("Der Kontoauszug existiert nicht.")
        _gesperrt(a)
        db.execute("DELETE FROM kontoauszug WHERE id=?", (auszug_id,))
        protokolliere(db, "kontoauszug.loeschen", "kontoauszug", auszug_id, alt=a, benutzer_id=benutzer_id)
        return a


def pruefung_vermerken(db, auszug_id, benutzer_id):
    """§ 8 (1): the list was checked for correctness and completeness."""
    with db.tx():
        a = db.one("SELECT * FROM kontoauszug WHERE id=?", (auszug_id,))
        if a is None:
            raise BuchungsFehler("Der Kontoauszug existiert nicht.")
        if a["geprueft_am"]:
            raise BuchungsFehler("Die Prüfung ist bereits vermerkt.")
        if a["endsaldo_cent"] is None:
            raise BuchungsFehler("Bitte zuerst den Endsaldo laut Kontoauszug eintragen.")
        ul = ueberwachungsliste(db, auszug_id)
        if ul["endsaldo"] != a["endsaldo_cent"]:
            raise BuchungsFehler("Der berechnete Endsaldo stimmt nicht mit dem Endsaldo laut Kontoauszug überein. "
                                 "So kann die Liste nicht als richtig bestätigt werden.")
        jetzt = utcnow()
        db.execute("UPDATE kontoauszug SET geprueft_von=?, geprueft_am=?, geprueft_hash=? WHERE id=?",
                   (benutzer_id, jetzt, fingerabdruck(db, auszug_id, ul), auszug_id))
        protokolliere(db, "kontoauszug.pruefen", "kontoauszug", auszug_id,
                      neu={"auszug": bezeichnung(a), "endsaldo_cent": ul["endsaldo"]}, benutzer_id=benutzer_id)


def pruefung_aufheben(db, auszug_id, grund, benutzer_id):
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Bitte einen Grund angeben, warum die Prüfung aufgehoben wird.")
    with db.tx():
        a = db.one("SELECT * FROM kontoauszug WHERE id=?", (auszug_id,))
        if a is None or not a["geprueft_am"]:
            raise BuchungsFehler("Für diesen Kontoauszug ist keine Prüfung vermerkt.")
        db.execute("UPDATE kontoauszug SET geprueft_von=NULL, geprueft_am=NULL, geprueft_hash=NULL WHERE id=?",
                   (auszug_id,))
        protokolliere(db, "kontoauszug.pruefung_aufheben", "kontoauszug", auszug_id,
                      alt={"geprueft_von": a["geprueft_von"], "geprueft_am": a["geprueft_am"]},
                      neu={"grund": grund}, benutzer_id=benutzer_id)


def anmerkung_setzen(db, umsatz_id, text, benutzer_id):
    text = (text or "").strip()
    if len(text) > 500:
        raise BuchungsFehler("Die Anmerkung darf höchstens 500 Zeichen lang sein.")
    with db.tx():
        u = db.one("SELECT * FROM bankumsatz WHERE id=?", (umsatz_id,))
        if u is None:
            raise BuchungsFehler("Der Umsatz existiert nicht.")
        gesperrt = db.one("SELECT * FROM kontoauszug WHERE konto_id=? AND ? BETWEEN datum_von AND datum_bis"
                          " AND geprueft_am IS NOT NULL", (u["konto_id"], u["buchungsdatum"]))
        if gesperrt is not None and u["status"] == "BOOK":
            _gesperrt(gesperrt)
        alt = db.scalar("SELECT text FROM umsatz_anmerkung WHERE bankumsatz_id=?", (umsatz_id,))
        if (alt or "") == text:
            return False
        if text:
            db.execute("INSERT INTO umsatz_anmerkung (bankumsatz_id, text, geaendert_von, geaendert_am)"
                       " VALUES (?,?,?,?) ON CONFLICT(bankumsatz_id) DO UPDATE SET text=excluded.text,"
                       " geaendert_von=excluded.geaendert_von, geaendert_am=excluded.geaendert_am",
                       (umsatz_id, text, benutzer_id, utcnow()))
        else:
            db.execute("DELETE FROM umsatz_anmerkung WHERE bankumsatz_id=?", (umsatz_id,))
        protokolliere(db, "umsatz.anmerkung", "bankumsatz", umsatz_id, alt={"text": alt}, neu={"text": text or None},
                      benutzer_id=benutzer_id)
        return True


def auszug_zum_umsatz(db, umsatz):
    return db.one("SELECT * FROM kontoauszug WHERE konto_id=? AND ? BETWEEN datum_von AND datum_bis",
                  (umsatz["konto_id"], umsatz["buchungsdatum"]))


def _eindeutig(werte):
    gesehen = []
    for w in werte:
        for teil in (w or "").split(", "):
            if teil and teil not in gesehen:
                gesehen.append(teil)
    return ", ".join(gesehen)


def ueberwachungsliste(db, auszug_id):
    """All data of the monitoring list (Anlage 1) for one statement."""
    a = db.one("SELECT a.*, b.anzeigename AS geprueft_von_name FROM kontoauszug a"
               " LEFT JOIN benutzer b ON b.id=a.geprueft_von WHERE a.id=?", (auszug_id,))
    if a is None:
        raise BuchungsFehler("Der Kontoauszug existiert nicht.")
    konto = db.one("SELECT * FROM konto WHERE id=?", (a["konto_id"],))
    anfang = _saldo_vor(db, konto, a["datum_von"])
    umsaetze = db.all("SELECT u.*, an.text AS anmerkung,"
                      " (SELECT grund FROM umsatz_ignoriert i WHERE i.bankumsatz_id=u.id AND i.aufgehoben=0"
                      "   ORDER BY i.id DESC LIMIT 1) AS ignoriert_grund"
                      " FROM bankumsatz u LEFT JOIN umsatz_anmerkung an ON an.bankumsatz_id=u.id"
                      " WHERE u.konto_id=? AND u.status='BOOK' AND u.buchungsdatum BETWEEN ? AND ?"
                      " ORDER BY u.buchungsdatum, u.id", (konto["id"], a["datum_von"], a["datum_bis"]))
    ids = [u["id"] for u in umsaetze]
    teile, belege = {}, {}
    if ids:
        marken = ",".join("?" * len(ids))
        for t in db.all(
                "SELECT z.bankumsatz_id, z.betrag_cent, z.notiz, z.quelle, pr.bezeichnung AS projekt, pr.kurzcode,"
                " p.zahlcode, p.bezeichnung AS posten, kl.bezeichnung AS kind_klasse, "
                + KLASSEN_SQL + " AS projekt_klassen"
                " FROM zuordnung z JOIN projekt pr ON pr.id=z.projekt_id"
                " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
                " LEFT JOIN klasse kl ON kl.id=k.klasse_id"
                f" WHERE z.storniert=0 AND z.bankumsatz_id IN ({marken}) ORDER BY z.id", ids):
            teile.setdefault(t["bankumsatz_id"], []).append(t)
        for b in db.all("SELECT z.bankumsatz_id, b.dateiname FROM beleg b JOIN zuordnung z ON z.id=b.zuordnung_id"
                        f" WHERE z.storniert=0 AND z.bankumsatz_id IN ({marken}) ORDER BY b.id", ids):
            belege.setdefault(b["bankumsatz_id"], []).append(b["dateiname"])
    beginn = konto["beginn_datum"] or ""
    zeilen, stand, negativ = [], anfang, False
    for nr, u in enumerate(umsaetze, start=1):
        tz = teile.get(u["id"], [])
        stand += u["betrag_cent"]
        negativ = negativ or stand < 0
        if u["ignoriert_grund"]:
            grund = f"Ignoriert: {u['ignoriert_grund']}"
        elif tz:
            texte = []
            for t in tz:
                text = t["projekt"]
                if t["zahlcode"]:
                    text += f" ({t['zahlcode']})"
                elif t["posten"] and t["posten"] != t["projekt"]:
                    text += f" – {t['posten']}"
                if len(tz) > 1:
                    text += f": {_euro(abs(t['betrag_cent']))}"
                texte.append(text)
            grund = "; ".join(texte)
        elif u["buchungsdatum"] < beginn:
            grund = "vor Beginn der Kontoführung (im Anfangsbestand)"
        else:
            grund = "noch nicht zugeordnet"
        anmerkungen = [u["anmerkung"]] if u["anmerkung"] else []
        # Notes of automatic assignments only explain the matching rule.
        anmerkungen += [t["notiz"] for t in tz if t["notiz"] and t["quelle"] == "manuell"]
        if belege.get(u["id"]):
            anzahl = len(belege[u["id"]])
            anmerkungen.append(f"{anzahl} Beleg{'e' if anzahl > 1 else ''} in der Schulkasse")
        offen = u["zuordnungsstatus"] in ("offen", "teilweise") and u["buchungsdatum"] >= beginn
        zeilen.append({
            "nr": nr, "umsatz": u, "datum": u["buchungsdatum"],
            "klasse": _eindeutig(t["kind_klasse"] or t["projekt_klassen"] for t in tz),
            "name": u["gegen_name"] or "", "grund": grund, "zweck": u["verwendungszweck"] or "",
            "einnahme": u["betrag_cent"] if u["betrag_cent"] > 0 else None,
            "ausgabe": -u["betrag_cent"] if u["betrag_cent"] < 0 else None,
            "buchung": u["betrag_cent"], "stand": stand, "anmerkung": "; ".join(anmerkungen),
            "eigene_anmerkung": u["anmerkung"] or "", "offen": offen,
        })
    eingaenge = sum(z["einnahme"] or 0 for z in zeilen)
    ausgaenge = sum(z["ausgabe"] or 0 for z in zeilen)
    ende = anfang + eingaenge - ausgaenge
    return {
        "auszug": a, "konto": konto, "bezeichnung": bezeichnung(a), "anfangssaldo": anfang, "zeilen": zeilen,
        "eingaenge": eingaenge, "ausgaenge": ausgaenge, "endsaldo": ende, "negativ": negativ,
        "nicht_zugeordnet": sum(1 for z in zeilen if z["offen"]),
        "hinweise": pruefungen(db, konto, a, anfang, ende),
    }


def fingerabdruck(db, auszug_id, ul=None):
    """Hash over everything printed in the list; detects changes after the check."""
    ul = ul or ueberwachungsliste(db, auszug_id)
    inhalt = {"a": [ul["auszug"]["datum_von"], ul["auszug"]["datum_bis"], ul["auszug"]["endsaldo_cent"],
                    ul["anfangssaldo"]],
              "z": [[z["datum"], z["klasse"], z["name"], z["grund"], z["buchung"], z["anmerkung"]]
                    for z in ul["zeilen"]]}
    return hashlib.sha256(json.dumps(inhalt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def status_uebersicht(db):
    """For the dashboard: per account unchecked statements, deviations, uncovered transactions."""
    ergebnis = []
    for k in db.all("SELECT * FROM konto WHERE aktiv=1 ORDER BY id"):
        auszuege = liste(db, k["id"])
        letzter = auszuege[0]["auszug"] if auszuege else None
        ergebnis.append({
            "konto": k, "anzahl": len(auszuege), "letzter": letzter,
            "ungeprueft": sum(1 for x in auszuege if not x["auszug"]["geprueft_am"]),
            "fehler": sum(1 for x in auszuege if x["fehler"] or x["geaendert"]),
            "ohne_auszug": ohne_auszug(db, k["id"])["anzahl"],
        })
    return ergebnis
