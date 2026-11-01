"""Assignments of bank transactions to projects and items (F3.5, F4.5, F5)."""
from .db import utcnow
from .geld import format_cent
from .protokoll import protokolliere


class BuchungsFehler(ValueError):
    """Domain error with a user-facing message."""


# Transactions on or after the account's start of bookkeeping (Stichtag).
# Earlier ones stay stored but are neither assigned nor counted.
AKTIV_SQL = "u.buchungsdatum >= COALESCE((SELECT kb.beginn_datum FROM konto kb WHERE kb.id=u.konto_id), '')"


def vor_beginn(db, umsatz):
    """Stichtag of the account if the transaction lies before it, else None."""
    beginn = db.scalar("SELECT beginn_datum FROM konto WHERE id=?", (umsatz["konto_id"],))
    return beginn if beginn and umsatz["buchungsdatum"] < beginn else None


def _pruefe_beginn(db, umsatz):
    beginn = vor_beginn(db, umsatz)
    if beginn:
        d = "{2}.{1}.{0}".format(*beginn.split("-"))
        raise BuchungsFehler(f"Der Umsatz liegt vor dem Beginn der Kontoführung ({d}). Sein Betrag ist im "
                             "Anfangsbestand enthalten und wird dort auf die Projekte verteilt.")


def bezahlt_cent(db, posten_id):
    """Absolute sum of non-cancelled assignments of an item."""
    return db.scalar(
        "SELECT COALESCE(SUM(ABS(betrag_cent)),0) FROM zahlung WHERE offener_posten_id=?",
        (posten_id,), 0)


def berechne_posten_status(soll, bezahlt, aktueller_status):
    """F3.5: status derived from payments, except explicit erlassen/storniert."""
    if aktueller_status in ("erlassen", "storniert"):
        return aktueller_status
    if bezahlt <= 0:
        return "offen"
    if bezahlt < soll:
        return "teilweise"
    return "erledigt"


def aktualisiere_posten_status(db, posten_id):
    p = db.one("SELECT id, sollbetrag_cent, status FROM offener_posten WHERE id=?", (posten_id,))
    if p is None:
        return None
    neu = berechne_posten_status(p["sollbetrag_cent"], bezahlt_cent(db, posten_id), p["status"])
    if neu != p["status"]:
        db.execute("UPDATE offener_posten SET status=? WHERE id=?", (neu, posten_id))
    return neu


def aktualisiere_umsatz_status(db, umsatz_id):
    u = db.one("SELECT id, betrag_cent, zuordnungsstatus FROM bankumsatz WHERE id=?", (umsatz_id,))
    if u["zuordnungsstatus"] == "ignoriert":
        return "ignoriert"
    summe = db.scalar(
        "SELECT COALESCE(SUM(ABS(betrag_cent)),0) FROM zuordnung WHERE bankumsatz_id=? AND storniert=0",
        (umsatz_id,), 0)
    if summe == 0:
        neu = "offen"
    elif summe < abs(u["betrag_cent"]):
        neu = "teilweise"
    else:
        neu = "zugeordnet"
    if neu != u["zuordnungsstatus"]:
        db.execute("UPDATE bankumsatz SET zuordnungsstatus=? WHERE id=?", (neu, umsatz_id))
    return neu


def lerne_iban(db, umsatz, posten_id):
    """F4.5: remember which family pays from which IBAN."""
    if not umsatz["gegen_iban"] or umsatz["betrag_cent"] <= 0:
        return
    row = db.one(
        "SELECT k.familie_id FROM offener_posten p JOIN kind k ON k.id=p.kind_id WHERE p.id=?",
        (posten_id,))
    if row is None or row["familie_id"] is None:
        return
    db.execute(
        "INSERT INTO zahler_iban (familie_id, iban, zahler_name, treffer_anzahl, zuletzt_gesehen)"
        " VALUES (?,?,?,1,?)"
        " ON CONFLICT (familie_id, iban) DO UPDATE SET treffer_anzahl=treffer_anzahl+1,"
        " zahler_name=COALESCE(excluded.zahler_name, zahler_name),"
        " zuletzt_gesehen=MAX(COALESCE(zuletzt_gesehen,''), excluded.zuletzt_gesehen)",
        (row["familie_id"], umsatz["gegen_iban"], umsatz["gegen_name"], umsatz["buchungsdatum"]))


def zuordnen(db, umsatz_id, teile, quelle, benutzer_id, konfidenz=None, notiz=None):
    """Create assignments. `teile` is a list of dicts with projekt_id,
    offener_posten_id (optional) and betrag_cent (absolute or signed)."""
    if quelle not in ("auto", "manuell", "regel"):
        raise ValueError(quelle)
    with db.tx():
        u = db.one("SELECT * FROM bankumsatz WHERE id=?", (umsatz_id,))
        if u is None:
            raise BuchungsFehler("Der Umsatz existiert nicht.")
        if u["status"] != "BOOK":
            raise BuchungsFehler("Vorgemerkte Umsätze können erst nach der Buchung zugeordnet werden.")
        if u["zuordnungsstatus"] == "ignoriert":
            raise BuchungsFehler("Der Umsatz ist als ignoriert markiert. Bitte zuerst die Markierung aufheben.")
        _pruefe_beginn(db, u)
        if not teile:
            raise BuchungsFehler("Bitte mindestens einen Teilbetrag angeben.")
        vorzeichen = 1 if u["betrag_cent"] > 0 else -1
        bereits = db.scalar(
            "SELECT COALESCE(SUM(ABS(betrag_cent)),0) FROM zuordnung WHERE bankumsatz_id=? AND storniert=0",
            (umsatz_id,), 0)
        geprueft = []
        for t in teile:
            betrag = abs(int(t["betrag_cent"]))
            if betrag == 0:
                raise BuchungsFehler("Teilbeträge müssen größer als 0,00 € sein.")
            posten_id = t.get("offener_posten_id") or None
            projekt_id = t.get("projekt_id")
            if posten_id:
                p = db.one("SELECT * FROM offener_posten WHERE id=?", (posten_id,))
                if p is None:
                    raise BuchungsFehler("Der offene Posten existiert nicht.")
                if p["status"] == "storniert":
                    raise BuchungsFehler(f"Der Posten „{p['bezeichnung']}“ ist storniert.")
                erwartet = "eingang" if vorzeichen > 0 else "ausgang"
                if p["richtung"] != erwartet:
                    raise BuchungsFehler(
                        "Ein Zahlungseingang kann nur einem Eingangsposten zugeordnet werden, "
                        "eine Zahlung nur einem Ausgangsposten.")
                projekt_id = p["projekt_id"]
            pr = db.one("SELECT * FROM projekt WHERE id=?", (projekt_id,))
            if pr is None:
                raise BuchungsFehler("Bitte ein Projekt auswählen.")
            if pr["status"] != "offen":
                raise BuchungsFehler(f"Das Projekt „{pr['bezeichnung']}“ ist abgeschlossen und schreibgeschützt.")
            geprueft.append((projekt_id, posten_id, betrag))
        summe = bereits + sum(b for _, _, b in geprueft)
        if summe > abs(u["betrag_cent"]):
            rest = abs(u["betrag_cent"]) - bereits
            raise BuchungsFehler(
                f"Die Teilbeträge übersteigen den Umsatz. Noch zu verteilen sind {format_cent(rest)}.")
        jetzt = utcnow()
        ids = []
        for projekt_id, posten_id, betrag in geprueft:
            zid = db.insert(
                "INSERT INTO zuordnung (bankumsatz_id, projekt_id, offener_posten_id, betrag_cent, quelle,"
                " konfidenz, notiz, erstellt_von, erstellt_am) VALUES (?,?,?,?,?,?,?,?,?)",
                (umsatz_id, projekt_id, posten_id, vorzeichen * betrag, quelle,
                 konfidenz if quelle == "auto" else None, notiz, benutzer_id, jetzt))
            ids.append(zid)
            protokolliere(db, "zuordnung.anlegen", "zuordnung", zid, neu={
                "bankumsatz_id": umsatz_id, "projekt_id": projekt_id, "offener_posten_id": posten_id,
                "betrag_cent": vorzeichen * betrag, "quelle": quelle, "konfidenz": konfidenz, "notiz": notiz,
            }, benutzer_id=benutzer_id)
            if posten_id:
                aktualisiere_posten_status(db, posten_id)
                lerne_iban(db, u, posten_id)
        aktualisiere_umsatz_status(db, umsatz_id)
        db.execute("DELETE FROM vorschlag WHERE bankumsatz_id=?", (umsatz_id,))
        return ids


def stornieren(db, zuordnung_id, grund, benutzer_id):
    """F5.6: assignments are never deleted, only cancelled."""
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Für ein Storno ist eine Begründung erforderlich.")
    with db.tx():
        z = db.one("SELECT * FROM zuordnung WHERE id=?", (zuordnung_id,))
        if z is None:
            raise BuchungsFehler("Die Zuordnung existiert nicht.")
        if z["storniert"]:
            raise BuchungsFehler("Die Zuordnung ist bereits storniert.")
        pr = db.one("SELECT status, bezeichnung FROM projekt WHERE id=?", (z["projekt_id"],))
        if pr["status"] != "offen":
            raise BuchungsFehler(f"Das Projekt „{pr['bezeichnung']}“ ist abgeschlossen und schreibgeschützt.")
        jetzt = utcnow()
        db.execute(
            "UPDATE zuordnung SET storniert=1, storniert_von=?, storniert_am=?, storno_grund=? WHERE id=?",
            (benutzer_id, jetzt, grund, zuordnung_id))
        protokolliere(db, "zuordnung.stornieren", "zuordnung", zuordnung_id,
                      alt={"storniert": 0}, neu={"storniert": 1, "storno_grund": grund},
                      benutzer_id=benutzer_id)
        if z["offener_posten_id"]:
            aktualisiere_posten_status(db, z["offener_posten_id"])
        aktualisiere_umsatz_status(db, z["bankumsatz_id"])


def ignorieren(db, umsatz_id, grund, benutzer_id):
    """F5.5: mark a transaction as not belonging to any project."""
    grund = (grund or "").strip()
    if not grund:
        raise BuchungsFehler("Bitte eine Begründung angeben, warum der Umsatz ignoriert wird.")
    with db.tx():
        u = db.one("SELECT * FROM bankumsatz WHERE id=?", (umsatz_id,))
        if u is None:
            raise BuchungsFehler("Der Umsatz existiert nicht.")
        if u["status"] != "BOOK":
            raise BuchungsFehler("Vorgemerkte Umsätze können nicht bearbeitet werden.")
        if u["zuordnungsstatus"] != "offen":
            raise BuchungsFehler("Nur vollständig offene Umsätze können ignoriert werden. "
                                 "Bitte bestehende Zuordnungen zuerst stornieren.")
        _pruefe_beginn(db, u)
        db.insert("INSERT INTO umsatz_ignoriert (bankumsatz_id, grund, erstellt_von, erstellt_am) VALUES (?,?,?,?)",
                  (umsatz_id, grund, benutzer_id, utcnow()))
        db.execute("UPDATE bankumsatz SET zuordnungsstatus='ignoriert' WHERE id=?", (umsatz_id,))
        db.execute("DELETE FROM vorschlag WHERE bankumsatz_id=?", (umsatz_id,))
        protokolliere(db, "bankumsatz.ignorieren", "bankumsatz", umsatz_id,
                      alt={"zuordnungsstatus": "offen"}, neu={"zuordnungsstatus": "ignoriert", "grund": grund},
                      benutzer_id=benutzer_id)


def ignorieren_aufheben(db, umsatz_id, benutzer_id):
    with db.tx():
        u = db.one("SELECT * FROM bankumsatz WHERE id=?", (umsatz_id,))
        if u is None or u["zuordnungsstatus"] != "ignoriert":
            raise BuchungsFehler("Der Umsatz ist nicht als ignoriert markiert.")
        db.execute("UPDATE umsatz_ignoriert SET aufgehoben=1, aufgehoben_von=?, aufgehoben_am=?"
                   " WHERE bankumsatz_id=? AND aufgehoben=0", (benutzer_id, utcnow(), umsatz_id))
        db.execute("UPDATE bankumsatz SET zuordnungsstatus='offen' WHERE id=?", (umsatz_id,))
        protokolliere(db, "bankumsatz.ignorieren_aufheben", "bankumsatz", umsatz_id,
                      alt={"zuordnungsstatus": "ignoriert"}, neu={"zuordnungsstatus": "offen"},
                      benutzer_id=benutzer_id)


def vorschlag_annehmen(db, vorschlag_id, benutzer_id):
    """F5.3: accept a suggestion with one click."""
    with db.tx():
        v = db.one("SELECT * FROM vorschlag WHERE id=?", (vorschlag_id,))
        if v is None:
            raise BuchungsFehler("Der Vorschlag ist nicht mehr aktuell. Bitte die Seite neu laden.")
        teile = [dict(r) for r in db.all(
            "SELECT projekt_id, offener_posten_id, betrag_cent FROM vorschlag_teil WHERE vorschlag_id=?",
            (vorschlag_id,))]
        notiz = f"Vorschlag {v['regel']} ({v['konfidenz']} %) bestätigt"
        ids = zuordnen(db, v["bankumsatz_id"], teile, "regel", benutzer_id, notiz=notiz)
        return v["bankumsatz_id"], ids
