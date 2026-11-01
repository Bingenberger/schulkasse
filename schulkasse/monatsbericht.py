"""Monthly report for the account holder (extension beyond F8.5).

One report per month: for every account the statement of the month with the
project each transaction belongs to, and the month's closing balance split over
the projects. No personal data of children is included – items appear with
their payment code only.
"""
from calendar import monthrange
from datetime import date, timedelta

from .auswertung import BEWEGUNG_SQL, kassenbuch
from .buchung import AKTIV_SQL, BuchungsFehler

MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")


def monatsgrenzen(jahr, monat):
    if not 1 <= monat <= 12:
        raise BuchungsFehler("Bitte einen Monat von 1 bis 12 wählen.")
    letzter = monthrange(jahr, monat)[1]
    return date(jahr, monat, 1).isoformat(), date(jahr, monat, letzter).isoformat()


def monat_aus_text(text, heute=None):
    """Parse "2026-09"; defaults to the month before the current one."""
    heute = heute or date.today()
    if text:
        try:
            jahr, monat = (int(x) for x in text.split("-")[:2])
            monatsgrenzen(jahr, monat)
            return jahr, monat
        except (ValueError, TypeError) as exc:
            raise BuchungsFehler("Bitte den Monat im Format 2026-09 angeben.") from exc
    return (heute.year - 1, 12) if heute.month == 1 else (heute.year, heute.month - 1)


def _projekttext(db, umsatz_id):
    """Projects (and payment codes) of one transaction, without child names."""
    return db.all("SELECT z.betrag_cent, pr.bezeichnung AS projekt, pr.kurzcode, p.zahlcode"
                  " FROM zuordnung z JOIN projekt pr ON pr.id=z.projekt_id"
                  " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id"
                  " WHERE z.bankumsatz_id=? AND z.storniert=0 ORDER BY z.id", (umsatz_id,))


def _konto_zeile(db, konto, von, bis):
    kb = kassenbuch(db, konto["id"], von, bis)
    for z in kb["zeilen"]:
        z["projekte"] = _projekttext(db, z["umsatz"]["id"])
    verteilt = db.scalar(f"SELECT COALESCE(SUM(betrag_cent),0) FROM ({BEWEGUNG_SQL})"
                         " WHERE konto_id=? AND datum<=?", (konto["id"], bis), 0)
    ignoriert = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz u WHERE u.konto_id=?"
                          f" AND u.zuordnungsstatus='ignoriert' AND u.buchungsdatum<=? AND {AKTIV_SQL}",
                          (konto["id"], bis), 0)
    offen = db.scalar("SELECT COUNT(*) FROM bankumsatz u WHERE u.konto_id=? AND u.status='BOOK'"
                      f" AND u.zuordnungsstatus IN ('offen','teilweise') AND u.buchungsdatum<=? AND {AKTIV_SQL}",
                      (konto["id"], bis), 0)
    return {
        "konto": konto, "kb": kb, "verteilt": verteilt, "ignoriert": ignoriert, "offen_anzahl": offen,
        "nicht_verteilt": kb["endsaldo"] - verteilt,
    }


def _projektzeilen(db, von, bis, konto_ids=None):
    """`konto_ids` None means all accounts; movements without an account are
    then kept, so the figures match the projects' balances."""
    ph = ",".join("?" * len(konto_ids or ()))
    konto_filter = f" AND x.konto_id IN ({ph})" if konto_ids else ""
    sql = (
        "SELECT pr.id, pr.bezeichnung, pr.kurzcode, pr.status, x.konto_id, x.art, x.datum, x.betrag_cent"
        f" FROM ({BEWEGUNG_SQL}) x JOIN projekt pr ON pr.id=x.projekt_id"
        f" WHERE x.datum<=?{konto_filter} ORDER BY pr.bezeichnung")
    zeilen = {}
    for r in db.all(sql, [bis] + list(konto_ids or ())):
        p = zeilen.setdefault(r["id"], {
            "id": r["id"], "bezeichnung": r["bezeichnung"], "kurzcode": r["kurzcode"], "status": r["status"],
            "stand_anfang": 0, "anfangsbestand": 0, "eingang": 0, "ausgang": 0, "umbuchung": 0,
            "stand_ende": 0, "je_konto": {}})
        p["stand_ende"] += r["betrag_cent"]
        p["je_konto"][r["konto_id"]] = p["je_konto"].get(r["konto_id"], 0) + r["betrag_cent"]
        if r["datum"] < von:
            p["stand_anfang"] += r["betrag_cent"]
        elif r["art"] == "anfangsbestand":
            p["anfangsbestand"] += r["betrag_cent"]
        elif r["art"] == "umbuchung":
            p["umbuchung"] += r["betrag_cent"]
        elif r["betrag_cent"] > 0:
            p["eingang"] += r["betrag_cent"]
        else:
            p["ausgang"] += r["betrag_cent"]
    liste = [p for p in zeilen.values() if p["stand_anfang"] or p["stand_ende"] or p["eingang"]
             or p["ausgang"] or p["umbuchung"] or p["anfangsbestand"]]
    liste.sort(key=lambda p: (p["status"] != "offen", p["bezeichnung"]))
    return liste


def monatsbericht(db, jahr, monat, konto_id=None):
    """All figures of one month: per account the statement, per project the
    movements and the closing balance (also split per account)."""
    von, bis = monatsgrenzen(jahr, monat)
    if konto_id:
        konten = db.all("SELECT * FROM konto WHERE id=?", (konto_id,))
        if not konten:
            raise BuchungsFehler("Das Konto existiert nicht.")
    else:
        konten = db.all("SELECT * FROM konto ORDER BY id")
    if not konten:
        raise BuchungsFehler("Es ist noch kein Konto angelegt.")
    kontozeilen = [_konto_zeile(db, k, von, bis) for k in konten]
    projekte = _projektzeilen(db, von, bis, [k["id"] for k in konten] if konto_id else None)
    summen = {feld: sum(p[feld] for p in projekte)
              for feld in ("stand_anfang", "anfangsbestand", "eingang", "ausgang", "umbuchung", "stand_ende")}
    return {
        "jahr": jahr, "monat": monat, "monatsname": MONATE[monat - 1], "von": von, "bis": bis,
        "vortag": (date.fromisoformat(von) - timedelta(days=1)).isoformat(),
        "konten": kontozeilen, "projekte": projekte, "summen": summen,
        "mit_anfangsbestand": any(p["anfangsbestand"] for p in projekte),
        "je_konto_summen": {k["konto"]["id"]: sum(p["je_konto"].get(k["konto"]["id"], 0) for p in projekte)
                            for k in kontozeilen},
        "kontostand_gesamt": sum(k["kb"]["endsaldo"] for k in kontozeilen),
        "nicht_verteilt_gesamt": sum(k["nicht_verteilt"] for k in kontozeilen),
        "ohne_konto": sum(p["je_konto"].get(None, 0) for p in projekte),
    }
