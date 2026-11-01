"""Anonymisation run (F11.2). Never runs automatically."""
import re
from datetime import date, timedelta

from .protokoll import protokolliere

PLATZHALTER = "anonymisiert"


def kandidaten(db, frist_tage, stichtag=None):
    """Everything that would be affected, for the preview."""
    stichtag = stichtag or date.today()
    grenze = (stichtag - timedelta(days=frist_tage)).isoformat()
    projekte = db.all("SELECT * FROM projekt WHERE status='abgeschlossen' AND abgeschlossen_am IS NOT NULL"
                      " AND abgeschlossen_am <= ? ORDER BY abgeschlossen_am", (grenze,))
    projekt_ids = {p["id"] for p in projekte}

    kinder = []
    for k in db.all("SELECT k.*, kl.bezeichnung AS klasse FROM kind k JOIN klasse kl ON kl.id=k.klasse_id"
                    " WHERE k.anonymisiert=0 AND k.aktiv=0 ORDER BY k.nachname, k.vorname"):
        eigene = {r[0] for r in db.all("SELECT DISTINCT projekt_id FROM offener_posten WHERE kind_id=?", (k["id"],))}
        if eigene <= projekt_ids:
            kinder.append(k)
    kind_ids = {k["id"] for k in kinder}

    umsaetze = []
    if kind_ids:
        ph = ",".join("?" * len(kind_ids))
        kandidaten_u = db.all(
            "SELECT DISTINCT u.* FROM bankumsatz u JOIN zuordnung z ON z.bankumsatz_id=u.id"
            f" JOIN offener_posten p ON p.id=z.offener_posten_id WHERE p.kind_id IN ({ph})"
            " AND (u.gegen_name IS NULL OR u.gegen_name<>?)", list(kind_ids) + [PLATZHALTER])
        for u in kandidaten_u:
            projekte_u = {r[0] for r in db.all("SELECT DISTINCT projekt_id FROM zuordnung WHERE bankumsatz_id=?",
                                               (u["id"],))}
            if projekte_u <= projekt_ids:
                umsaetze.append(u)

    familien = []
    for fid in {k["familie_id"] for k in kinder if k["familie_id"]}:
        rest = db.scalar("SELECT COUNT(*) FROM kind WHERE familie_id=? AND anonymisiert=0", (fid,), 0)
        betroffen = sum(1 for k in kinder if k["familie_id"] == fid)
        if rest == betroffen:
            familien.append(fid)
    ibans = []
    if familien:
        ibans = db.all(f"SELECT * FROM zahler_iban WHERE familie_id IN ({','.join('?' * len(familien))})", familien)
    return {"grenze": grenze, "projekte": projekte, "kinder": kinder, "umsaetze": umsaetze,
            "familien": familien, "zahler_iban": ibans}


def _schwaerze(text, namen):
    if not text:
        return text
    for n in namen:
        if len(n) >= 2:
            text = re.sub(re.escape(n), "***", text, flags=re.IGNORECASE)
    return text


def ausfuehren(db, frist_tage, benutzer_id, stichtag=None):
    with db.tx():
        k = kandidaten(db, frist_tage, stichtag)
        namen = sorted({n for kind in k["kinder"] for n in (kind["vorname"], kind["nachname"])}, key=len, reverse=True)
        for kind in k["kinder"]:
            db.execute("UPDATE kind SET vorname=?, nachname=?, but_notiz=NULL, anonymisiert=1 WHERE id=?",
                       (f"Gelöscht #{kind['id']}", f"Gelöscht #{kind['id']}", kind["id"]))
            # Names in older audit entries of this child are removed as well.
            for p in db.all("SELECT id, alt_wert, neu_wert FROM protokoll WHERE entitaet='kind' AND entitaet_id=?",
                            (kind["id"],)):
                db.execute("UPDATE protokoll SET alt_wert=?, neu_wert=? WHERE id=?",
                           (_schwaerze(p["alt_wert"], namen), _schwaerze(p["neu_wert"], namen), p["id"]))
        for u in k["umsaetze"]:
            db.execute("UPDATE bankumsatz SET gegen_name=?, gegen_iban=?, verwendungszweck=?,"
                       " verwendungszweck_roh=? WHERE id=?",
                       (PLATZHALTER, PLATZHALTER, _schwaerze(u["verwendungszweck"], namen),
                        _schwaerze(u["verwendungszweck_roh"], namen), u["id"]))
        for fid in k["familien"]:
            db.execute("DELETE FROM zahler_iban WHERE familie_id=?", (fid,))
            db.execute("UPDATE familie SET bezeichnung=? WHERE id=?", (f"Gelöscht #{fid}", fid))
        ergebnis = {"kinder": len(k["kinder"]), "umsaetze": len(k["umsaetze"]),
                    "zahler_iban": len(k["zahler_iban"]), "projekte": [p["kurzcode"] for p in k["projekte"]],
                    "frist_tage": frist_tage}
        protokolliere(db, "anonymisierung.ausfuehren", "anonymisierung", None, neu=ergebnis, benutzer_id=benutzer_id)
    return ergebnis
