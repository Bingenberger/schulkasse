"""Reports and balances (F8)."""
from dataclasses import dataclass

from .buchung import AKTIV_SQL
from .projekte import KLASSEN_SQL


@dataclass
class Kennzahlen:
    saldo: int = 0
    erwartete_eingaenge: int = 0
    erwartete_ausgaenge: int = 0
    bezahlt: int = 0
    gesamt: int = 0
    erlassen: int = 0

    @property
    def prognose(self):
        return self.saldo + self.erwartete_eingaenge - self.erwartete_ausgaenge

    @property
    def quote(self):
        return round(100 * self.bezahlt / self.gesamt) if self.gesamt else None


_KENNZAHLEN_SQL = """
SELECT pr.id,
  (SELECT COALESCE(SUM(z.betrag_cent),0) FROM zahlung z WHERE z.projekt_id=pr.id AND z.storniert=0) AS saldo,
  (SELECT COALESCE(SUM(p.sollbetrag_cent - (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z
       WHERE z.offener_posten_id=p.id AND z.storniert=0)),0)
     FROM offener_posten p WHERE p.projekt_id=pr.id AND p.richtung='eingang' AND p.status IN ('offen','teilweise'))
     AS erw_ein,
  (SELECT COALESCE(SUM(p.sollbetrag_cent - (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z
       WHERE z.offener_posten_id=p.id AND z.storniert=0)),0)
     FROM offener_posten p WHERE p.projekt_id=pr.id AND p.richtung='ausgang' AND p.status IN ('offen','teilweise'))
     AS erw_aus,
  (SELECT COUNT(*) FROM offener_posten p WHERE p.projekt_id=pr.id AND p.kind_id IS NOT NULL
     AND p.status='erledigt') AS bezahlt,
  (SELECT COUNT(*) FROM offener_posten p WHERE p.projekt_id=pr.id AND p.kind_id IS NOT NULL
     AND p.status IN ('offen','teilweise','erledigt')) AS gesamt,
  (SELECT COUNT(*) FROM offener_posten p WHERE p.projekt_id=pr.id AND p.status='erlassen') AS erlassen
FROM projekt pr
"""


def _kz(row):
    return Kennzahlen(saldo=row["saldo"], erwartete_eingaenge=row["erw_ein"], erwartete_ausgaenge=row["erw_aus"],
                      bezahlt=row["bezahlt"], gesamt=row["gesamt"], erlassen=row["erlassen"])


def projekt_kennzahlen(db, projekt_id):
    """F8.1 for a single project."""
    return _kz(db.one(_KENNZAHLEN_SQL + " WHERE pr.id=?", (projekt_id,)))


def projekte_uebersicht(db, schuljahr_id=None, nur_user_id=None, status=None):
    sql = ("SELECT pr.*, " + KLASSEN_SQL + " AS klasse, sj.bezeichnung AS schuljahr, b.anzeigename AS verantwortlich"
           " FROM projekt pr JOIN schuljahr sj ON sj.id=pr.schuljahr_id"
           " LEFT JOIN benutzer b ON b.id=pr.verantwortlich_user_id WHERE 1=1")
    params = []
    if schuljahr_id:
        sql += " AND pr.schuljahr_id=?"
        params.append(schuljahr_id)
    if nur_user_id:
        sql += " AND pr.verantwortlich_user_id=?"
        params.append(nur_user_id)
    if status:
        sql += " AND pr.status=?"
        params.append(status)
    projekte = db.all(sql + " ORDER BY pr.status DESC, pr.bezeichnung", params)
    if not projekte:
        return []
    ids = [p["id"] for p in projekte]
    kz = {r["id"]: _kz(r) for r in db.all(_KENNZAHLEN_SQL + f" WHERE pr.id IN ({','.join('?' * len(ids))})", ids)}
    return [(p, kz[p["id"]]) for p in projekte]


def kontrollrechnung(db):
    """F8.3: balance of last import minus opening balance shares minus
    assignments minus ignored transactions = difference.

    Only transactions from the start of bookkeeping (Stichtag) on count;
    the balance at the Stichtag is the opening balance, which must be
    distributed to projects. The difference is zero once the opening balance
    is fully distributed and every later transaction is assigned or ignored.
    """
    from .anfangsbestand import berechne, verteilt
    zeilen = []
    for k in db.all("SELECT * FROM konto ORDER BY id"):
        imp = db.one("SELECT saldo_schluss_cent, saldo_datum, importiert_am FROM import"
                     " WHERE konto_id=? AND saldo_schluss_cent IS NOT NULL"
                     " ORDER BY saldo_datum DESC, id DESC LIMIT 1", (k["id"],))
        if imp is None:
            continue
        zugeordnet = db.scalar(
            "SELECT COALESCE(SUM(z.betrag_cent),0) FROM zuordnung z JOIN bankumsatz u ON u.id=z.bankumsatz_id"
            f" WHERE u.konto_id=? AND z.storniert=0 AND {AKTIV_SQL}", (k["id"],), 0)
        ignoriert = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz u WHERE konto_id=?"
                              f" AND zuordnungsstatus='ignoriert' AND {AKTIV_SQL}", (k["id"],), 0)
        offen = db.scalar("SELECT COUNT(*) FROM bankumsatz u WHERE konto_id=? AND status='BOOK'"
                          f" AND zuordnungsstatus IN ('offen','teilweise') AND {AKTIV_SQL}", (k["id"],), 0)
        ab = berechne(db, k)
        anfang_verteilt = verteilt(db, k["id"])
        anfangsbestand = ab["betrag"] if ab["betrag"] is not None else 0
        zeilen.append({
            "konto": k, "kontostand": imp["saldo_schluss_cent"], "saldo_datum": imp["saldo_datum"],
            "stichtag": ab["stichtag"], "anfangsbestand": anfangsbestand, "anfangsbestand_hinweis": ab["hinweis"],
            "verteilt": anfang_verteilt, "nicht_verteilt": anfangsbestand - anfang_verteilt,
            "zugeordnet": zugeordnet, "ignoriert": ignoriert,
            "differenz": imp["saldo_schluss_cent"] - anfang_verteilt - zugeordnet - ignoriert,
            "offen": offen,
        })
    gesamt = {key: sum(z[key] for z in zeilen)
              for key in ("kontostand", "anfangsbestand", "verteilt", "nicht_verteilt", "zugeordnet", "ignoriert",
                          "differenz", "offen")}
    return {"konten": zeilen, "gesamt": gesamt, "vorhanden": bool(zeilen)}


# F8.1: every movement of a project with the account it sits on and its date.
# Transfers between projects keep the account, so the sums per account stay
# correct. Opening balance shares are dated at the Stichtag, transfers at the
# day they were booked.
BEWEGUNG_SQL = (
    "SELECT z.projekt_id, u.konto_id, u.buchungsdatum AS datum, z.betrag_cent, 'umsatz' AS art"
    " FROM zuordnung z JOIN bankumsatz u ON u.id=z.bankumsatz_id WHERE z.storniert=0"
    " UNION ALL SELECT a.projekt_id, a.konto_id,"
    " COALESCE(ko.beginn_datum, ko.eroeffnungssaldo_datum, substr(a.erstellt_am, 1, 10)), a.betrag_cent,"
    " 'anfangsbestand' FROM anfangsbestand a JOIN konto ko ON ko.id=a.konto_id WHERE a.storniert=0"
    " UNION ALL SELECT b.von_projekt_id, b.konto_id, substr(b.erstellt_am, 1, 10), -b.betrag_cent, 'umbuchung'"
    " FROM umbuchung b WHERE b.storniert=0"
    " UNION ALL SELECT b.nach_projekt_id, b.konto_id, substr(b.erstellt_am, 1, 10), b.betrag_cent, 'umbuchung'"
    " FROM umbuchung b WHERE b.storniert=0")
_JE_KONTO_SQL = f"SELECT projekt_id, konto_id, betrag_cent FROM ({BEWEGUNG_SQL})"


def projekt_je_konto(db, projekt_id):
    """Balance of one project per account: list of (konto row, amount)."""
    rows = db.all(f"SELECT k.*, x.summe FROM (SELECT konto_id, SUM(betrag_cent) AS summe FROM ({_JE_KONTO_SQL})"
                  " WHERE projekt_id=? GROUP BY konto_id) x LEFT JOIN konto k ON k.id=x.konto_id"
                  " ORDER BY x.konto_id IS NULL, k.id", (projekt_id,))
    return [(r, r["summe"]) for r in rows if r["summe"]]


def kontenverteilung(db, nur_aufgeteilt=False):
    """F8.1: which part of every project's balance sits on which account.

    Projects without any movement are left out. `ohne_konto` counts transfers
    booked before the account was recorded with them.
    """
    kr = {z["konto"]["id"]: z for z in kontrollrechnung(db)["konten"]}
    konten = db.all("SELECT * FROM konto ORDER BY id")
    rows = db.all(
        "SELECT pr.id AS projekt_id, pr.bezeichnung, pr.kurzcode, pr.status, pr.typ, x.konto_id,"
        f" SUM(x.betrag_cent) AS betrag FROM projekt pr JOIN ({_JE_KONTO_SQL}) x ON x.projekt_id=pr.id"
        " GROUP BY pr.id, x.konto_id ORDER BY pr.status, pr.bezeichnung")
    projekte = {}
    for r in rows:
        p = projekte.setdefault(r["projekt_id"], {
            "id": r["projekt_id"], "bezeichnung": r["bezeichnung"], "kurzcode": r["kurzcode"],
            "status": r["status"], "typ": r["typ"], "betraege": {}, "ohne_konto": 0, "summe": 0})
        if r["konto_id"] is None:
            p["ohne_konto"] += r["betrag"]
        else:
            p["betraege"][r["konto_id"]] = p["betraege"].get(r["konto_id"], 0) + r["betrag"]
        p["summe"] += r["betrag"]
    liste = []
    for p in projekte.values():
        p["konten_anzahl"] = sum(1 for b in p["betraege"].values() if b) + (1 if p["ohne_konto"] else 0)
        p["aufgeteilt"] = p["konten_anzahl"] > 1
        # settled projects (everything transferred away) would only add zero rows
        if p["konten_anzahl"] == 0 and p["summe"] == 0:
            continue
        if nur_aufgeteilt and not p["aufgeteilt"]:
            continue
        liste.append(p)
    summen = {k["id"]: sum(p["betraege"].get(k["id"], 0) for p in liste) for k in konten}
    return {
        "konten": konten, "projekte": liste,
        "summen": summen, "ohne_konto": sum(p["ohne_konto"] for p in liste),
        "gesamt": sum(p["summe"] for p in liste),
        "kontostand": {k["id"]: kr[k["id"]]["kontostand"] if k["id"] in kr else None for k in konten},
        "differenz": {k["id"]: kr[k["id"]]["differenz"] if k["id"] in kr else None for k in konten},
        "stand_datum": {k["id"]: kr[k["id"]]["saldo_datum"] if k["id"] in kr else None for k in konten},
        "aufgeteilt_anzahl": sum(1 for p in liste if p["aufgeteilt"]),
    }


# F3.6: an overpayment counts as open until it is settled; a larger
# overpayment after that (another payment) counts again.
OFFENE_UEBERZAHLUNG = ("bezahlt > sollbetrag_cent AND (ueberzahlung_geklaert_cent IS NULL"
                       " OR bezahlt - sollbetrag_cent > ueberzahlung_geklaert_cent)")
_UEBERZAHLUNG_SQL = (
    "SELECT * FROM (SELECT p.*, pr.bezeichnung AS projekt, pr.kurzcode, pr.status AS projekt_status,"
    " k.vorname, k.nachname, kl.bezeichnung AS klasse, b.anzeigename AS geklaert_von,"
    " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
    "   AND z.storniert=0) AS bezahlt"
    " FROM offener_posten p JOIN projekt pr ON pr.id=p.projekt_id LEFT JOIN kind k ON k.id=p.kind_id"
    " LEFT JOIN klasse kl ON kl.id=k.klasse_id LEFT JOIN benutzer b ON b.id=p.ueberzahlung_geklaert_von)")


def ueberzahlungen(db, geklaert=False):
    """F3.6: open overpayments, or the ones already marked as settled."""
    bedingung = (f"NOT ({OFFENE_UEBERZAHLUNG}) AND ueberzahlung_geklaert_cent IS NOT NULL"
                 if geklaert else OFFENE_UEBERZAHLUNG)
    return db.all(f"{_UEBERZAHLUNG_SQL} WHERE {bedingung} ORDER BY projekt, nachname, id")


def anzahl_ueberzahlungen(db):
    return db.scalar(f"SELECT COUNT(*) FROM ({_UEBERZAHLUNG_SQL} WHERE {OFFENE_UEBERZAHLUNG})", default=0)


def zahlungsstand(db, projekt_id, fuer_dritte=False, status=None, sortierung="klasse"):
    """F8.2. With fuer_dritte=True, BuT children and waived items are never listed."""
    sql = ("SELECT p.*, k.vorname, k.nachname, k.but_status, k.anonymisiert, kl.bezeichnung AS klasse,"
           " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
           "   AND z.storniert=0) AS bezahlt,"
           " (SELECT MAX(u.buchungsdatum) FROM zuordnung z JOIN bankumsatz u ON u.id=z.bankumsatz_id"
           "   WHERE z.offener_posten_id=p.id AND z.storniert=0) AS zuletzt_bezahlt"
           " FROM offener_posten p JOIN kind k ON k.id=p.kind_id JOIN klasse kl ON kl.id=k.klasse_id"
           " WHERE p.projekt_id=? AND p.status<>'storniert'")
    params = [projekt_id]
    if fuer_dritte:
        sql += " AND k.but_status=0 AND p.status<>'erlassen'"
    if status:
        sql += " AND p.status=?"
        params.append(status)
    order = {
        "klasse": "kl.bezeichnung, k.nachname, k.vorname",
        "name": "k.nachname, k.vorname",
        "status": "p.status, kl.bezeichnung, k.nachname",
        "code": "p.zahlcode",
        "rest": "(p.sollbetrag_cent - bezahlt) DESC, k.nachname",
    }.get(sortierung, "kl.bezeichnung, k.nachname, k.vorname")
    return db.all(sql + " ORDER BY " + order, params)


def erinnerungsliste(db, stichtag, projekt_id=None, nur_projekt_ids=None):
    """F8.4: overdue items grouped by class. BuT children and waived items
    are never included - not even greyed out."""
    sql = ("SELECT p.*, k.vorname, k.nachname, kl.bezeichnung AS klasse, pr.bezeichnung AS projekt,"
           " pr.kurzcode, (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z"
           "   WHERE z.offener_posten_id=p.id AND z.storniert=0) AS bezahlt"
           " FROM offener_posten p JOIN kind k ON k.id=p.kind_id JOIN klasse kl ON kl.id=k.klasse_id"
           " JOIN projekt pr ON pr.id=p.projekt_id"
           " WHERE p.richtung='eingang' AND p.status IN ('offen','teilweise') AND k.but_status=0"
           " AND k.anonymisiert=0 AND pr.status='offen' AND p.faellig_am IS NOT NULL AND p.faellig_am < ?")
    params = [stichtag]
    if projekt_id:
        sql += " AND p.projekt_id=?"
        params.append(projekt_id)
    if nur_projekt_ids is not None:
        if not nur_projekt_ids:
            return {}
        sql += f" AND p.projekt_id IN ({','.join('?' * len(nur_projekt_ids))})"
        params.extend(nur_projekt_ids)
    rows = db.all(sql + " ORDER BY kl.bezeichnung, k.nachname, k.vorname, pr.bezeichnung", params)
    gruppen = {}
    for r in rows:
        gruppen.setdefault(r["klasse"], []).append(r)
    return gruppen


def zuordnungstext(db, umsatz_id):
    rows = db.all("SELECT z.betrag_cent, pr.kurzcode, p.zahlcode FROM zuordnung z JOIN projekt pr ON pr.id=z.projekt_id"
                  " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id"
                  " WHERE z.bankumsatz_id=? AND z.storniert=0 ORDER BY z.id", (umsatz_id,))
    return rows


def kassenbuch(db, konto_id, von, bis):
    """F8.5: chronological list with opening/closing balance."""
    konto = db.one("SELECT * FROM konto WHERE id=?", (konto_id,))
    eroeffnung = konto["eroeffnungssaldo_cent"] or 0
    vorher = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM bankumsatz WHERE konto_id=? AND status='BOOK'"
                       " AND buchungsdatum < ?", (konto_id, von), 0)
    umsaetze = db.all("SELECT * FROM bankumsatz WHERE konto_id=? AND status='BOOK' AND buchungsdatum BETWEEN ? AND ?"
                      " ORDER BY buchungsdatum, id", (konto_id, von, bis))
    zeilen = []
    for u in umsaetze:
        teile = zuordnungstext(db, u["id"])
        grund = None
        if u["zuordnungsstatus"] == "ignoriert":
            grund = db.scalar("SELECT grund FROM umsatz_ignoriert WHERE bankumsatz_id=? AND aufgehoben=0"
                              " ORDER BY id DESC LIMIT 1", (u["id"],))
        zeilen.append({"umsatz": u, "teile": teile, "ignoriert_grund": grund})
    anfang = eroeffnung + vorher
    eingaenge = sum(u["betrag_cent"] for u in umsaetze if u["betrag_cent"] > 0)
    ausgaenge = sum(u["betrag_cent"] for u in umsaetze if u["betrag_cent"] < 0)
    return {"konto": konto, "von": von, "bis": bis, "anfangssaldo": anfang, "zeilen": zeilen,
            "beginn": konto["beginn_datum"] or "",
            "eingaenge": eingaenge, "ausgaenge": ausgaenge, "endsaldo": anfang + eingaenge + ausgaenge}


def projekt_umsaetze(db, projekt_id, mit_storno=True):
    """Assigned transactions of a project plus its shares of opening balances
    (art='anfangsbestand', dated at the Stichtag) and transfers between projects
    (art='umbuchung', signed from this project's point of view)."""
    storno = "" if mit_storno else " AND z.storniert=0"
    sql = ("SELECT 'umsatz' AS art, z.id, z.bankumsatz_id, z.betrag_cent, z.quelle, z.konfidenz, z.notiz,"
           " z.storniert, z.storno_grund, u.buchungsdatum, u.gegen_name, u.gegen_iban, u.verwendungszweck,"
           " u.betrag_cent AS umsatz_betrag, p.zahlcode, p.bezeichnung AS posten, k.vorname, k.nachname"
           " FROM zuordnung z JOIN bankumsatz u ON u.id=z.bankumsatz_id"
           " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
           f" WHERE z.projekt_id=?{storno}"
           " UNION ALL "
           "SELECT 'anfangsbestand' AS art, z.id, NULL, z.betrag_cent, 'anfangsbestand', NULL, z.notiz,"
           " z.storniert, z.storno_grund, COALESCE(ko.beginn_datum, ko.eroeffnungssaldo_datum),"
           " 'Anfangsbestand ' || ko.bezeichnung, NULL, z.notiz, NULL, p.zahlcode, p.bezeichnung, k.vorname, k.nachname"
           " FROM anfangsbestand z JOIN konto ko ON ko.id=z.konto_id"
           " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
           f" WHERE z.projekt_id=?{storno}"
           " UNION ALL "
           "SELECT 'umbuchung' AS art, z.id, NULL,"
           " CASE WHEN z.von_projekt_id=? THEN -z.betrag_cent ELSE z.betrag_cent END, 'umbuchung', NULL, z.grund,"
           " z.storniert, z.storno_grund, substr(z.erstellt_am, 1, 10),"
           " CASE WHEN z.von_projekt_id=? THEN 'Umbuchung an ' || pn.bezeichnung"
           "      ELSE 'Umbuchung von ' || pv.bezeichnung END,"
           " NULL, z.grund, NULL, p.zahlcode, p.bezeichnung, k.vorname, k.nachname"
           " FROM umbuchung z JOIN projekt pv ON pv.id=z.von_projekt_id JOIN projekt pn ON pn.id=z.nach_projekt_id"
           " LEFT JOIN offener_posten p ON p.id=z.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
           f" WHERE (z.von_projekt_id=? OR z.nach_projekt_id=?){storno}")
    return db.all(f"SELECT * FROM ({sql}) ORDER BY buchungsdatum, art DESC, id",
                  (projekt_id, projekt_id, projekt_id, projekt_id, projekt_id, projekt_id))


def projekt_posten(db, projekt_id):
    return db.all(
        "SELECT p.*, k.vorname, k.nachname, k.but_status, kl.bezeichnung AS klasse,"
        " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
        "   AND z.storniert=0) AS bezahlt"
        " FROM offener_posten p LEFT JOIN kind k ON k.id=p.kind_id LEFT JOIN klasse kl ON kl.id=k.klasse_id"
        " WHERE p.projekt_id=? ORDER BY p.richtung, kl.bezeichnung, k.nachname, k.vorname, p.id", (projekt_id,))
