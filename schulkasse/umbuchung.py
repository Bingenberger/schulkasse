"""Transfers between projects (F8.1 follow-up).

No money moves on the bank account: an amount only changes the project it
belongs to. Typical cases are the remaining balance of a finished project and
a project covering another project's shortfall. Transfers are never deleted,
only cancelled.
"""
from .buchung import BuchungsFehler, aktualisiere_posten_status
from .db import utcnow
from .geld import format_cent
from .protokoll import protokolliere


def saldo(db, projekt_id):
    """Current balance of a project (assignments, opening balance, transfers)."""
    return db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM zahlung WHERE projekt_id=?", (projekt_id,), 0)


def _offenes_projekt(db, projekt_id, rolle):
    p = db.one("SELECT * FROM projekt WHERE id=?", (projekt_id,))
    if p is None:
        raise BuchungsFehler(f"Das {rolle} existiert nicht.")
    if p["status"] != "offen":
        raise BuchungsFehler(f"Das Projekt „{p['bezeichnung']}“ ist abgeschlossen und schreibgeschützt. "
                             "Zum Umbuchen bitte zuerst wieder öffnen.")
    return p


def konto_vorschlag(db, projekt_id):
    """Account holding most of the project's money; falls back to the only account."""
    from .auswertung import projekt_je_konto
    salden = [(k, betrag) for k, betrag in projekt_je_konto(db, projekt_id) if k["id"] is not None]
    if salden:
        return max(salden, key=lambda x: x[1])[0]["id"]
    return db.scalar("SELECT id FROM konto WHERE aktiv=1 ORDER BY id LIMIT 1")


def anlegen(db, von_projekt_id, nach_projekt_id, betrag_cent, grund, benutzer_id, offener_posten_id=None,
            konto_id=None):
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Bitte einen Grund für die Umbuchung angeben.")
    betrag_cent = int(betrag_cent)
    if betrag_cent <= 0:
        raise BuchungsFehler("Der Betrag muss größer als 0,00 € sein.")
    with db.tx():
        von = _offenes_projekt(db, von_projekt_id, "abgebende Projekt")
        nach = _offenes_projekt(db, nach_projekt_id, "empfangende Projekt")
        if von["id"] == nach["id"]:
            raise BuchungsFehler("Abgebendes und empfangendes Projekt müssen verschieden sein.")
        if offener_posten_id:
            p = db.one("SELECT * FROM offener_posten WHERE id=?", (offener_posten_id,))
            if p is None:
                raise BuchungsFehler("Der offene Posten existiert nicht.")
            if p["projekt_id"] != nach["id"]:
                raise BuchungsFehler(f"Der Posten gehört nicht zum empfangenden Projekt „{nach['bezeichnung']}“.")
            if p["richtung"] != "eingang":
                raise BuchungsFehler("Eine Umbuchung kann nur einen Eingangsposten decken.")
            if p["status"] == "storniert":
                raise BuchungsFehler(f"Der Posten „{p['bezeichnung']}“ ist storniert.")
        konto_id = int(konto_id) if konto_id else konto_vorschlag(db, von["id"])
        if konto_id and not db.one("SELECT id FROM konto WHERE id=?", (konto_id,)):
            raise BuchungsFehler("Das gewählte Konto existiert nicht.")
        jetzt = utcnow()
        uid = db.insert("INSERT INTO umbuchung (von_projekt_id, nach_projekt_id, offener_posten_id, betrag_cent,"
                        " grund, konto_id, erstellt_von, erstellt_am) VALUES (?,?,?,?,?,?,?,?)",
                        (von["id"], nach["id"], offener_posten_id, betrag_cent, grund, konto_id, benutzer_id, jetzt))
        protokolliere(db, "umbuchung.anlegen", "umbuchung", uid, neu={
            "von_projekt_id": von["id"], "nach_projekt_id": nach["id"], "offener_posten_id": offener_posten_id,
            "betrag_cent": betrag_cent, "grund": grund, "konto_id": konto_id}, benutzer_id=benutzer_id)
        if offener_posten_id:
            aktualisiere_posten_status(db, offener_posten_id)
        return uid


def stornieren(db, umbuchung_id, grund, benutzer_id):
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Für ein Storno ist eine Begründung erforderlich.")
    with db.tx():
        u = db.one("SELECT * FROM umbuchung WHERE id=?", (umbuchung_id,))
        if u is None:
            raise BuchungsFehler("Die Umbuchung existiert nicht.")
        if u["storniert"]:
            raise BuchungsFehler("Die Umbuchung ist bereits storniert.")
        _offenes_projekt(db, u["von_projekt_id"], "abgebende Projekt")
        _offenes_projekt(db, u["nach_projekt_id"], "empfangende Projekt")
        db.execute("UPDATE umbuchung SET storniert=1, storniert_von=?, storniert_am=?, storno_grund=? WHERE id=?",
                   (benutzer_id, utcnow(), grund, umbuchung_id))
        protokolliere(db, "umbuchung.stornieren", "umbuchung", umbuchung_id, alt={"storniert": 0},
                      neu={"storniert": 1, "storno_grund": grund}, benutzer_id=benutzer_id)
        if u["offener_posten_id"]:
            aktualisiere_posten_status(db, u["offener_posten_id"])
        return u


def konto_setzen(db, umbuchung_id, konto_id, benutzer_id):
    """Record (or correct) the account a transfer sits on.

    No money moves, so this is allowed for closed projects as well: it only
    fixes the per-account reports.
    """
    with db.tx():
        u = db.one("SELECT * FROM umbuchung WHERE id=?", (umbuchung_id,))
        if u is None:
            raise BuchungsFehler("Die Umbuchung existiert nicht.")
        if u["storniert"]:
            raise BuchungsFehler("Die Umbuchung ist storniert.")
        konto = db.one("SELECT * FROM konto WHERE id=?", (konto_id,)) if konto_id else None
        if konto is None:
            raise BuchungsFehler("Bitte ein Konto wählen.")
        db.execute("UPDATE umbuchung SET konto_id=? WHERE id=?", (konto["id"], umbuchung_id))
        protokolliere(db, "umbuchung.konto_setzen", "umbuchung", umbuchung_id,
                      alt={"konto_id": u["konto_id"]}, neu={"konto_id": konto["id"]}, benutzer_id=benutzer_id)
        return konto


def ohne_konto(db):
    """Transfers that have no account yet – they blur the per-account reports."""
    return db.all("SELECT u.*, pv.bezeichnung AS von_projekt, pv.kurzcode AS von_kurzcode,"
                  " pn.bezeichnung AS nach_projekt, pn.kurzcode AS nach_kurzcode"
                  " FROM umbuchung u JOIN projekt pv ON pv.id=u.von_projekt_id"
                  " JOIN projekt pn ON pn.id=u.nach_projekt_id"
                  " WHERE u.konto_id IS NULL AND u.storniert=0 ORDER BY u.id")


def eintraege(db, projekt_id=None, posten_id=None):
    sql = ("SELECT u.*, pv.bezeichnung AS von_projekt, pv.kurzcode AS von_kurzcode, pn.bezeichnung AS nach_projekt,"
           " pn.kurzcode AS nach_kurzcode, p.zahlcode, p.bezeichnung AS posten, k.vorname, k.nachname,"
           " b.anzeigename AS von, s.anzeigename AS storno_von, kt.bezeichnung AS konto"
           " FROM umbuchung u JOIN projekt pv ON pv.id=u.von_projekt_id JOIN projekt pn ON pn.id=u.nach_projekt_id"
           " LEFT JOIN offener_posten p ON p.id=u.offener_posten_id LEFT JOIN kind k ON k.id=p.kind_id"
           " JOIN benutzer b ON b.id=u.erstellt_von LEFT JOIN benutzer s ON s.id=u.storniert_von"
           " LEFT JOIN konto kt ON kt.id=u.konto_id WHERE 1=1")
    params = []
    if projekt_id is not None:
        sql += " AND (u.von_projekt_id=? OR u.nach_projekt_id=?)"
        params += [projekt_id, projekt_id]
    if posten_id is not None:
        sql += " AND u.offener_posten_id=?"
        params.append(posten_id)
    return db.all(sql + " ORDER BY u.storniert, u.id DESC", params)


def restuebertrag_text(projekt, rest):
    """Suggested reason when a remaining balance is transferred."""
    richtung = "Restguthaben" if rest > 0 else "Fehlbetrag"
    return f"{richtung} {format_cent(abs(rest))} aus „{projekt['bezeichnung']}“ ({projekt['kurzcode']})"
