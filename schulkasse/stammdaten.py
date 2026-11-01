"""Master data: school years, classes, children, CSV import, year change (F9)."""
import csv
import io
import re

from .buchung import BuchungsFehler
from .db import utcnow
from .protokoll import protokolliere
from .textnorm import normalisiere


class StammdatenFehler(BuchungsFehler):
    pass


def aktives_schuljahr(db):
    return db.one("SELECT * FROM schuljahr WHERE aktiv=1 ORDER BY beginn DESC LIMIT 1")


def schuljahr_anlegen(db, bezeichnung, beginn, ende, benutzer_id, aktiv=False):
    bezeichnung = (bezeichnung or "").strip()
    if not bezeichnung or not beginn or not ende:
        raise StammdatenFehler("Bitte Bezeichnung, Beginn und Ende angeben.")
    if ende <= beginn:
        raise StammdatenFehler("Das Ende muss nach dem Beginn liegen.")
    if db.one("SELECT id FROM schuljahr WHERE bezeichnung=?", (bezeichnung,)):
        raise StammdatenFehler(f"Das Schuljahr {bezeichnung} existiert bereits.")
    with db.tx():
        sid = db.insert("INSERT INTO schuljahr (bezeichnung, beginn, ende, aktiv) VALUES (?,?,?,0)",
                        (bezeichnung, beginn, ende))
        protokolliere(db, "schuljahr.anlegen", "schuljahr", sid,
                      neu={"bezeichnung": bezeichnung, "beginn": beginn, "ende": ende}, benutzer_id=benutzer_id)
        if aktiv:
            schuljahr_aktivieren(db, sid, benutzer_id)
    return sid


def schuljahr_aktivieren(db, schuljahr_id, benutzer_id):
    with db.tx():
        db.execute("UPDATE schuljahr SET aktiv = CASE WHEN id=? THEN 1 ELSE 0 END", (schuljahr_id,))
        protokolliere(db, "schuljahr.aktivieren", "schuljahr", schuljahr_id, benutzer_id=benutzer_id)


def klasse_holen_oder_anlegen(db, schuljahr_id, bezeichnung, benutzer_id):
    bezeichnung = bezeichnung.strip()
    row = db.one("SELECT id FROM klasse WHERE schuljahr_id=? AND bezeichnung=?", (schuljahr_id, bezeichnung))
    if row:
        return row["id"]
    kid = db.insert("INSERT INTO klasse (schuljahr_id, bezeichnung) VALUES (?,?)", (schuljahr_id, bezeichnung))
    protokolliere(db, "klasse.anlegen", "klasse", kid, neu={"schuljahr_id": schuljahr_id, "bezeichnung": bezeichnung},
                  benutzer_id=benutzer_id)
    return kid


def familie_anlegen(db, bezeichnung):
    return db.insert("INSERT INTO familie (bezeichnung) VALUES (?)", (bezeichnung,))


def kind_anlegen(db, klasse_id, vorname, nachname, benutzer_id, familie_id=None, but_status=0, but_notiz=None):
    vorname, nachname = (vorname or "").strip(), (nachname or "").strip()
    if not vorname or not nachname:
        raise StammdatenFehler("Bitte Vor- und Nachname angeben.")
    with db.tx():
        if familie_id is None:
            familie_id = familie_anlegen(db, f"Familie {nachname}")
        kid = db.insert(
            "INSERT INTO kind (klasse_id, vorname, nachname, familie_id, but_status, but_notiz, erstellt_am)"
            " VALUES (?,?,?,?,?,?,?)",
            (klasse_id, vorname, nachname, familie_id, 1 if but_status else 0, but_notiz, utcnow()))
        protokolliere(db, "kind.anlegen", "kind", kid, neu={
            "klasse_id": klasse_id, "vorname": vorname, "nachname": nachname, "familie_id": familie_id,
            "but_status": but_status}, benutzer_id=benutzer_id)
    return kid


def kind_aendern(db, kind_id, daten, benutzer_id, darf_but=False):
    with db.tx():
        alt = db.one("SELECT * FROM kind WHERE id=?", (kind_id,))
        if alt is None:
            raise StammdatenFehler("Das Kind existiert nicht.")
        if alt["anonymisiert"]:
            raise StammdatenFehler("Anonymisierte Datensätze können nicht bearbeitet werden.")
        vorname = (daten.get("vorname") or "").strip()
        nachname = (daten.get("nachname") or "").strip()
        if not vorname or not nachname:
            raise StammdatenFehler("Bitte Vor- und Nachname angeben.")
        klasse_id = int(daten.get("klasse_id") or alt["klasse_id"])
        familie_id = daten.get("familie_id")
        if familie_id == "neu":
            familie_id = familie_anlegen(db, f"Familie {nachname}")
        familie_id = int(familie_id) if familie_id else alt["familie_id"]
        aktiv = 1 if daten.get("aktiv", alt["aktiv"]) in (1, "1", True, "on") else 0
        but_status, but_notiz = alt["but_status"], alt["but_notiz"]
        if darf_but:
            but_status = 1 if daten.get("but_status") in (1, "1", True, "on") else 0
            but_notiz = (daten.get("but_notiz") or "").strip() or None
        db.execute("UPDATE kind SET vorname=?, nachname=?, klasse_id=?, familie_id=?, aktiv=?, but_status=?,"
                   " but_notiz=? WHERE id=?",
                   (vorname, nachname, klasse_id, familie_id, aktiv, but_status, but_notiz, kind_id))
        neu = db.one("SELECT * FROM kind WHERE id=?", (kind_id,))
        protokolliere(db, "kind.aendern", "kind", kind_id, alt=alt, neu=neu, benutzer_id=benutzer_id)
        if alt["but_status"] != but_status:
            protokolliere(db, "kind.but_status", "kind", kind_id, alt={"but_status": alt["but_status"]},
                          neu={"but_status": but_status}, benutzer_id=benutzer_id)


def kind_loeschen(db, kind_id, benutzer_id):
    """F11.3: not possible while assignments reference the child."""
    with db.tx():
        k = db.one("SELECT * FROM kind WHERE id=?", (kind_id,))
        if k is None:
            raise StammdatenFehler("Das Kind existiert nicht.")
        n = db.scalar("SELECT COUNT(*) FROM zuordnung z JOIN offener_posten p ON p.id=z.offener_posten_id"
                      " WHERE p.kind_id=?", (kind_id,), 0)
        if n:
            raise StammdatenFehler("Das Kind kann nicht gelöscht werden, weil Zahlungen zugeordnet sind. "
                                   "Die Daten werden später über den Anonymisierungslauf entfernt.")
        posten = db.all("SELECT id FROM offener_posten WHERE kind_id=?", (kind_id,))
        for p in posten:
            db.execute("DELETE FROM vorschlag WHERE id IN (SELECT vorschlag_id FROM vorschlag_teil"
                       " WHERE offener_posten_id=?)", (p["id"],))
            db.execute("DELETE FROM offener_posten WHERE id=?", (p["id"],))
            protokolliere(db, "offener_posten.loeschen", "offener_posten", p["id"], benutzer_id=benutzer_id)
        db.execute("DELETE FROM kind WHERE id=?", (kind_id,))
        protokolliere(db, "kind.loeschen", "kind", kind_id, alt=k, benutzer_id=benutzer_id)
        if k["familie_id"] and not db.scalar("SELECT COUNT(*) FROM kind WHERE familie_id=?", (k["familie_id"],), 0):
            db.execute("DELETE FROM zahler_iban WHERE familie_id=?", (k["familie_id"],))
            db.execute("DELETE FROM familie WHERE id=?", (k["familie_id"],))


# ---------------------------------------------------------------- CSV import

def dekodiere_csv(daten):
    """F9.1: UTF-8 (with/without BOM) or Windows-1252."""
    try:
        return daten.decode("utf-8-sig")
    except UnicodeDecodeError:
        return daten.decode("cp1252")


def lies_klassenliste(daten):
    text = dekodiere_csv(daten)
    reader = csv.reader(io.StringIO(text), delimiter=";")
    zeilen = [r for r in reader if any(c.strip() for c in r)]
    if not zeilen:
        raise StammdatenFehler("Die CSV-Datei ist leer.")
    kopf = [c.strip().lower() for c in zeilen[0]]
    if "klasse" not in kopf or "nachname" not in kopf or "vorname" not in kopf:
        raise StammdatenFehler("Die CSV-Datei braucht die Spalten klasse;nachname;vorname;familie "
                               "(Trennzeichen Semikolon, erste Zeile als Überschrift).")
    idx = {name: kopf.index(name) for name in ("klasse", "nachname", "vorname", "familie") if name in kopf}
    ergebnis = []
    for nr, r in enumerate(zeilen[1:], start=2):
        def feld(name):
            i = idx.get(name)
            return r[i].strip() if i is not None and i < len(r) else ""
        eintrag = {"zeile": nr, "klasse": feld("klasse"), "nachname": feld("nachname"),
                   "vorname": feld("vorname"), "familie": feld("familie")}
        if not eintrag["klasse"] or not eintrag["nachname"] or not eintrag["vorname"]:
            raise StammdatenFehler(f"Zeile {nr}: Klasse, Nachname und Vorname müssen ausgefüllt sein.")
        ergebnis.append(eintrag)
    return ergebnis


def csv_vorschau(db, zeilen, schuljahr_id):
    """Preview: new classes, duplicates and proposed sibling groups."""
    klassen_vorhanden = {r["bezeichnung"] for r in db.all("SELECT bezeichnung FROM klasse WHERE schuljahr_id=?",
                                                         (schuljahr_id,))}
    neue_klassen = sorted({z["klasse"] for z in zeilen} - klassen_vorhanden)
    duplikate, neu = [], []
    for z in zeilen:
        dup = db.one("SELECT k.id FROM kind k JOIN klasse kl ON kl.id=k.klasse_id WHERE kl.schuljahr_id=?"
                     " AND kl.bezeichnung=? AND k.vorname=? AND k.nachname=?",
                     (schuljahr_id, z["klasse"], z["vorname"], z["nachname"]))
        (duplikate if dup else neu).append(z)

    gruppen = {}
    for z in neu:
        if not z["familie"]:
            gruppen.setdefault(normalisiere(z["nachname"]), []).append(z)
    vorschlaege = []
    for key, mitglieder in sorted(gruppen.items()):
        vorhandene = db.all("SELECT k.id, k.vorname, k.nachname, k.familie_id, kl.bezeichnung AS klasse FROM kind k"
                            " JOIN klasse kl ON kl.id=k.klasse_id WHERE k.aktiv=1 AND k.anonymisiert=0")
        vorhandene = [v for v in vorhandene if normalisiere(v["nachname"]) == key]
        familien = {v["familie_id"] for v in vorhandene if v["familie_id"]}
        if len(mitglieder) + len(vorhandene) >= 2 and len(familien) <= 1:
            vorschlaege.append({"schluessel": key, "nachname": mitglieder[0]["nachname"], "neu": mitglieder,
                                "vorhanden": vorhandene, "familie_id": next(iter(familien), None)})
    familien_spalte = sorted({z["familie"] for z in neu if z["familie"]})
    return {"neue_klassen": neue_klassen, "neu": neu, "duplikate": duplikate, "vorschlaege": vorschlaege,
            "familien_spalte": familien_spalte}


def csv_importieren(db, zeilen, schuljahr_id, bestaetigte_gruppen, benutzer_id):
    vorschau = csv_vorschau(db, zeilen, schuljahr_id)
    bestaetigt = set(bestaetigte_gruppen)
    angelegt = 0
    with db.tx():
        familie_nach_spalte = {}
        familie_nach_gruppe = {}
        for v in vorschau["vorschlaege"]:
            if v["schluessel"] in bestaetigt:
                familie_nach_gruppe[v["schluessel"]] = v["familie_id"] or familie_anlegen(db, f"Familie {v['nachname']}")
        for z in vorschau["neu"]:
            klasse_id = klasse_holen_oder_anlegen(db, schuljahr_id, z["klasse"], benutzer_id)
            if z["familie"]:
                if z["familie"] not in familie_nach_spalte:
                    vorhanden = db.one("SELECT id FROM familie WHERE bezeichnung=?", (z["familie"],))
                    familie_nach_spalte[z["familie"]] = vorhanden["id"] if vorhanden else familie_anlegen(db, z["familie"])
                familie_id = familie_nach_spalte[z["familie"]]
            else:
                familie_id = familie_nach_gruppe.get(normalisiere(z["nachname"]))
            kind_anlegen(db, klasse_id, z["vorname"], z["nachname"], benutzer_id, familie_id=familie_id)
            angelegt += 1
        protokolliere(db, "kind.csv_import", "schuljahr", schuljahr_id,
                      neu={"angelegt": angelegt, "duplikate": len(vorschau["duplikate"])}, benutzer_id=benutzer_id)
    return angelegt, len(vorschau["duplikate"])


# ------------------------------------------------------------- year change

_STUFE_RE = re.compile(r"^\s*(\d)(.*)$")


def hochschreiben_vorschau(db, von_id, nach_id):
    """F9.2 preview: class mapping for all active children of the old year."""
    if von_id == nach_id:
        raise StammdatenFehler("Bitte zwei verschiedene Schuljahre wählen.")
    klassen = db.all("SELECT kl.*, (SELECT COUNT(*) FROM kind k WHERE k.klasse_id=kl.id AND k.aktiv=1) AS anzahl"
                     " FROM klasse kl WHERE kl.schuljahr_id=? ORDER BY kl.bezeichnung", (von_id,))
    zuordnung = []
    for kl in klassen:
        m = _STUFE_RE.match(kl["bezeichnung"])
        if not m:
            zuordnung.append({"klasse": kl, "ziel": None, "aktion": "unklar"})
        elif int(m.group(1)) >= 4:
            zuordnung.append({"klasse": kl, "ziel": None, "aktion": "abgang"})
        else:
            zuordnung.append({"klasse": kl, "ziel": f"{int(m.group(1)) + 1}{m.group(2)}", "aktion": "aufruecken"})
    return zuordnung


def hochschreiben(db, von_id, nach_id, benutzer_id):
    vorschau = hochschreiben_vorschau(db, von_id, nach_id)
    stats = {"aufgerueckt": 0, "abgang": 0, "unklar": 0}
    with db.tx():
        for z in vorschau:
            kinder = db.all("SELECT id FROM kind WHERE klasse_id=? AND aktiv=1", (z["klasse"]["id"],))
            if z["aktion"] == "aufruecken":
                ziel_id = klasse_holen_oder_anlegen(db, nach_id, z["ziel"], benutzer_id)
                db.execute("UPDATE kind SET klasse_id=? WHERE klasse_id=? AND aktiv=1", (ziel_id, z["klasse"]["id"]))
                stats["aufgerueckt"] += len(kinder)
            elif z["aktion"] == "abgang":
                db.execute("UPDATE kind SET aktiv=0 WHERE klasse_id=? AND aktiv=1", (z["klasse"]["id"],))
                stats["abgang"] += len(kinder)
            else:
                stats["unklar"] += len(kinder)
        schuljahr_aktivieren(db, nach_id, benutzer_id)
        protokolliere(db, "schuljahr.hochschreiben", "schuljahr", nach_id,
                      neu={"von": von_id, "nach": nach_id, **stats}, benutzer_id=benutzer_id)
    return stats
