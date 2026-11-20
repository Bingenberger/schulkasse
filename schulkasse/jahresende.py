"""Year-end notice to the city (Richtlinie Schulgirokonten § 6 Abs. 2 and § 9).

Before the Christmas holidays the school confirms the balance of every account
at 31 December. The accounts are pure transit accounts; a balance other than
zero has to be justified. The justification is the composition of the balance
by project funds (plus amounts not belonging to any project).
"""
from .anfangsbestand import berechne, verteilt
from .auswertung import BEWEGUNG_SQL
from .buchung import AKTIV_SQL


def _saldo(db, konto, stichtag):
    gebucht = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                        " AND buchungsdatum <= ?", (konto["id"], stichtag), 0)
    return (konto["eroeffnungssaldo_cent"] or 0) + gebucht


def konto_zum_stichtag(db, konto, stichtag):
    saldo = _saldo(db, konto, stichtag)
    projekte = db.all(
        "SELECT pr.id, pr.bezeichnung, pr.kurzcode, pr.status, pr.typ, pr.abgeschlossen_am, SUM(x.betrag_cent) AS betrag"
        f" FROM ({BEWEGUNG_SQL}) x JOIN projekt pr ON pr.id=x.projekt_id"
        " WHERE x.konto_id=? AND x.datum <= ? GROUP BY pr.id HAVING SUM(x.betrag_cent) <> 0"
        " ORDER BY pr.status DESC, pr.bezeichnung", (konto["id"], stichtag))
    ignoriert = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz u WHERE konto_id=? AND status='BOOK'"
                          f" AND zuordnungsstatus='ignoriert' AND buchungsdatum <= ? AND {AKTIV_SQL}",
                          (konto["id"], stichtag), 0)
    ab = berechne(db, konto)
    nicht_verteilt = 0
    if ab["betrag"] is not None and (not ab["stichtag"] or ab["stichtag"] <= stichtag):
        nicht_verteilt = ab["betrag"] - verteilt(db, konto["id"])
    summe_projekte = sum(p["betrag"] for p in projekte)
    # Whatever is left are transactions not (fully) assigned yet.
    nicht_zugeordnet = saldo - summe_projekte - ignoriert - nicht_verteilt
    sonstige = []
    if ignoriert:
        sonstige.append(("Nicht projektbezogene Umsätze (z. B. Kontoführungsgebühren)", ignoriert))
    if nicht_verteilt:
        sonstige.append(("Anfangsbestand, noch nicht auf Projekte verteilt", nicht_verteilt))
    if nicht_zugeordnet:
        sonstige.append(("Umsätze, die noch keinem Projekt zugeordnet sind", nicht_zugeordnet))
    letzter_import = db.scalar("SELECT MAX(saldo_datum) FROM import WHERE konto_id=? AND saldo_schluss_cent IS NOT NULL",
                               (konto["id"],))
    auszug = db.one("SELECT * FROM kontoauszug WHERE konto_id=? AND datum_von <= ? AND datum_bis >= ?",
                    (konto["id"], stichtag, stichtag))
    return {
        "konto": konto, "saldo": saldo, "projekte": projekte, "sonstige": sonstige,
        "summe_projekte": summe_projekte, "letzter_import": letzter_import,
        "vorlaeufig": not letzter_import or letzter_import < stichtag,
        "auszug": auszug,
        "auszug_passt": bool(auszug and auszug["datum_bis"] == stichtag and auszug["endsaldo_cent"] == saldo),
        "pdng": db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=? AND status='PDNG' AND buchungsdatum <= ?",
                          (konto["id"], stichtag), 0),
    }


def jahresendmitteilung(db, stichtag):
    """All accounts that are active or still hold money at the reference date."""
    konten = []
    for k in db.all("SELECT * FROM konto ORDER BY id"):
        eintrag = konto_zum_stichtag(db, k, stichtag)
        if k["aktiv"] or eintrag["saldo"]:
            konten.append(eintrag)
    return {"stichtag": stichtag, "konten": konten, "gesamt": sum(k["saldo"] for k in konten),
            "vorlaeufig": any(k["vorlaeufig"] for k in konten),
            "mit_bestand": [k for k in konten if k["saldo"]]}
