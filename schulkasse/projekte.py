"""Projects and open items (F2, F3)."""
import re

from .buchung import BuchungsFehler, aktualisiere_posten_status, bezahlt_cent, zuordnen
from .db import utcnow
from .geld import format_cent
from .protokoll import protokolliere

TYPEN = {
    "elternbeitrag": "Elternbeitrag",
    "vorkasse": "Vorkasse",
    "sammeltopf": "Sammeltopf",
    "sonstiges": "Sonstiges",
}
KATEGORIEN = {
    "elternbeitrag": "Elternbeitrag",
    "erstattung": "Erstattung",
    "rechnung": "Rechnung",
    "sonstiges": "Sonstiges",
}
KURZCODE_RE = re.compile(r"^[A-Z0-9]{3,8}$")
JAHRGANG_RE = re.compile(r"^\s*(\d+)")

# Class names of a project ("4a, 4b"), usable as correlated subquery on `pr`.
KLASSEN_SQL = ("(SELECT GROUP_CONCAT(b, ', ') FROM (SELECT kl.bezeichnung AS b FROM projekt_klasse pk"
               " JOIN klasse kl ON kl.id=pk.klasse_id WHERE pk.projekt_id=pr.id"
               " ORDER BY length(kl.bezeichnung), kl.bezeichnung))")


def jahrgang(klasse):
    """Grade of a class name: "4a" -> "4", "10 b" -> "10", "Vorklasse" -> None."""
    m = JAHRGANG_RE.match(klasse or "")
    return m.group(1) if m else None


def projekt_klassen_ids(db, projekt_id):
    return [r[0] for r in db.all("SELECT pk.klasse_id FROM projekt_klasse pk JOIN klasse kl ON kl.id=pk.klasse_id"
                                 " WHERE pk.projekt_id=? ORDER BY length(kl.bezeichnung), kl.bezeichnung",
                                 (projekt_id,))]


class ProjektFehler(BuchungsFehler):
    pass


def _projekt(db, projekt_id):
    p = db.one("SELECT * FROM projekt WHERE id=?", (projekt_id,))
    if p is None:
        raise ProjektFehler("Das Projekt existiert nicht.")
    return p


def pruefe_offen(projekt):
    if projekt["status"] != "offen":
        raise ProjektFehler(f"Das Projekt „{projekt['bezeichnung']}“ ist abgeschlossen und schreibgeschützt.")


def normalisiere_kurzcode(code):
    code = (code or "").strip().upper()
    if not KURZCODE_RE.match(code):
        raise ProjektFehler("Der Kurzcode muss aus 3 bis 8 Zeichen A–Z und 0–9 bestehen.")
    return code


def kurzcode_gesperrt(db, projekt_id):
    """F2.2: code is frozen once a letter has been produced."""
    return db.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND letzter_brief_am IS NOT NULL",
                     (projekt_id,), 0) > 0


def _validiere(db, daten, projekt_id=None):
    bezeichnung = (daten.get("bezeichnung") or "").strip()
    if not bezeichnung:
        raise ProjektFehler("Bitte eine Bezeichnung angeben.")
    typ = daten.get("typ")
    if typ not in TYPEN:
        raise ProjektFehler("Bitte einen gültigen Projekttyp wählen.")
    kurzcode = normalisiere_kurzcode(daten.get("kurzcode"))
    andere = db.one("SELECT id FROM projekt WHERE kurzcode=? AND id<>?", (kurzcode, projekt_id or 0))
    if andere:
        raise ProjektFehler(f"Der Kurzcode {kurzcode} ist bereits vergeben.")
    if not db.one("SELECT id FROM schuljahr WHERE id=?", (daten.get("schuljahr_id"),)):
        raise ProjektFehler("Bitte ein Schuljahr wählen.")
    schuljahr_id = int(daten["schuljahr_id"])
    klassen_ids = _klassen_aus_formular(daten)
    for kid in klassen_ids:
        kl = db.one("SELECT bezeichnung, schuljahr_id FROM klasse WHERE id=?", (kid,))
        if kl is None:
            raise ProjektFehler("Eine der gewählten Klassen existiert nicht.")
        if kl["schuljahr_id"] != schuljahr_id:
            raise ProjektFehler(f"Die Klasse {kl['bezeichnung']} gehört zu einem anderen Schuljahr als das Projekt.")
    return {
        "schuljahr_id": int(daten["schuljahr_id"]),
        "bezeichnung": bezeichnung,
        "kurzcode": kurzcode,
        "typ": typ,
        "klassen_ids": klassen_ids,
        "verantwortlich_user_id": int(daten["verantwortlich_user_id"]) if daten.get("verantwortlich_user_id") else None,
        "konto_id": int(daten["konto_id"]) if daten.get("konto_id") else None,
        "beschreibung": (daten.get("beschreibung") or "").strip() or None,
    }


def _klassen_aus_formular(daten):
    """Class ids from a form (several `klasse_ids`) or a dict (`klassen_ids` list or legacy `klasse_id`)."""
    if hasattr(daten, "getlist"):
        roh = daten.getlist("klasse_ids")
    else:
        roh = daten.get("klassen_ids") or ([daten["klasse_id"]] if daten.get("klasse_id") else [])
    ids = []
    for k in roh:
        k = str(k).strip()
        if not k.isdigit():
            raise ProjektFehler("Ungültige Klassenauswahl.")
        if int(k) not in ids:
            ids.append(int(k))
    return ids


def _klassen_speichern(db, projekt_id, klassen_ids):
    db.execute("DELETE FROM projekt_klasse WHERE projekt_id=?", (projekt_id,))
    db.executemany("INSERT INTO projekt_klasse (projekt_id, klasse_id) VALUES (?,?)",
                   [(projekt_id, k) for k in klassen_ids])


def projekt_anlegen(db, daten, benutzer_id):
    werte = _validiere(db, daten)
    with db.tx():
        pid = db.insert(
            "INSERT INTO projekt (schuljahr_id, bezeichnung, kurzcode, typ, verantwortlich_user_id,"
            " konto_id, beschreibung, status, erstellt_am) VALUES (?,?,?,?,?,?,?,'offen',?)",
            (werte["schuljahr_id"], werte["bezeichnung"], werte["kurzcode"], werte["typ"],
             werte["verantwortlich_user_id"], werte["konto_id"], werte["beschreibung"], utcnow()))
        _klassen_speichern(db, pid, werte["klassen_ids"])
        protokolliere(db, "projekt.anlegen", "projekt", pid, neu=werte, benutzer_id=benutzer_id)
    return pid


def projekt_aendern(db, projekt_id, daten, benutzer_id):
    with db.tx():
        alt = _projekt(db, projekt_id)
        pruefe_offen(alt)
        werte = _validiere(db, daten, projekt_id)
        if werte["kurzcode"] != alt["kurzcode"]:
            if kurzcode_gesperrt(db, projekt_id):
                raise ProjektFehler("Der Kurzcode kann nicht mehr geändert werden, weil bereits Briefe "
                                    "mit Zahlcodes erzeugt wurden.")
            db.execute(
                "UPDATE offener_posten SET zahlcode = ? || substr(zahlcode, ?) WHERE projekt_id=? AND zahlcode IS NOT NULL",
                (werte["kurzcode"], len(alt["kurzcode"]) + 1, projekt_id))
        db.execute(
            "UPDATE projekt SET schuljahr_id=?, bezeichnung=?, kurzcode=?, typ=?,"
            " verantwortlich_user_id=?, konto_id=?, beschreibung=? WHERE id=?",
            (werte["schuljahr_id"], werte["bezeichnung"], werte["kurzcode"], werte["typ"],
             werte["verantwortlich_user_id"], werte["konto_id"], werte["beschreibung"], projekt_id))
        alt = dict(alt) | {"klassen_ids": projekt_klassen_ids(db, projekt_id)}
        _klassen_speichern(db, projekt_id, werte["klassen_ids"])
        protokolliere(db, "projekt.aendern", "projekt", projekt_id, alt=alt, neu=werte, benutzer_id=benutzer_id)


def projekt_abschliessen(db, projekt_id, benutzer_id, rest_ok=False):
    """F2.4: only possible without items in status offen/teilweise.

    A remaining balance should be transferred to another project first
    (Umbuchung); closing with a balance needs `rest_ok`."""
    with db.tx():
        p = _projekt(db, projekt_id)
        pruefe_offen(p)
        n = db.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND status IN ('offen','teilweise')",
                      (projekt_id,), 0)
        if n:
            raise ProjektFehler(f"Das Projekt hat noch {n} offene oder teilweise bezahlte Posten. "
                                "Bitte diese zuerst als erledigt, erlassen oder storniert kennzeichnen.")
        from .umbuchung import saldo as projekt_saldo
        rest = projekt_saldo(db, projekt_id)
        if rest and not rest_ok:
            raise ProjektFehler(
                f"Das Projekt hat noch einen Saldo von {format_cent(rest)}. Bitte diesen Betrag auf ein anderes "
                "Projekt umbuchen – oder das Projekt ausdrücklich mit Restsaldo abschließen.")
        heute = utcnow()[:10]
        db.execute("UPDATE projekt SET status='abgeschlossen', abgeschlossen_am=? WHERE id=?", (heute, projekt_id))
        db.execute("DELETE FROM vorschlag WHERE id IN (SELECT vorschlag_id FROM vorschlag_teil WHERE projekt_id=?)",
                   (projekt_id,))
        protokolliere(db, "projekt.abschliessen", "projekt", projekt_id,
                      alt={"status": "offen"}, neu={"status": "abgeschlossen", "abgeschlossen_am": heute},
                      benutzer_id=benutzer_id)


def projekt_oeffnen(db, projekt_id, benutzer_id):
    with db.tx():
        p = _projekt(db, projekt_id)
        if p["status"] == "offen":
            raise ProjektFehler("Das Projekt ist bereits geöffnet.")
        db.execute("UPDATE projekt SET status='offen', abgeschlossen_am=NULL WHERE id=?", (projekt_id,))
        protokolliere(db, "projekt.oeffnen", "projekt", projekt_id,
                      alt={"status": p["status"], "abgeschlossen_am": p["abgeschlossen_am"]},
                      neu={"status": "offen"}, benutzer_id=benutzer_id)


def naechste_nummer(db, projekt):
    """F3.2: running four-digit number per project."""
    codes = db.all("SELECT zahlcode FROM offener_posten WHERE projekt_id=? AND zahlcode IS NOT NULL",
                   (projekt["id"],))
    hoechste = 0
    for (code,) in codes:
        m = re.search(r"-(\d{4})$", code)
        if m:
            hoechste = max(hoechste, int(m.group(1)))
    return hoechste + 1


def _zahlcode(projekt, nummer):
    if nummer > 9999:
        raise ProjektFehler("Für dieses Projekt sind alle 9999 Zahlcodes vergeben.")
    return f"{projekt['kurzcode']}-{nummer:04d}"


def posten_anlegen(db, projekt_id, *, richtung, kategorie, bezeichnung, sollbetrag_cent, benutzer_id,
                   kind_id=None, faellig_am=None, mit_zahlcode=None, notiz=None, status="offen", erlass_grund=None):
    if richtung not in ("eingang", "ausgang"):
        raise ProjektFehler("Bitte eine Richtung wählen.")
    if kategorie not in KATEGORIEN:
        raise ProjektFehler("Bitte eine Kategorie wählen.")
    if not bezeichnung or not bezeichnung.strip():
        raise ProjektFehler("Bitte eine Bezeichnung angeben.")
    if sollbetrag_cent <= 0:
        raise ProjektFehler("Der Sollbetrag muss größer als 0,00 € sein.")
    if kategorie == "elternbeitrag" and not kind_id:
        raise ProjektFehler("Ein Elternbeitrag muss einem Kind zugeordnet sein.")
    if kategorie != "elternbeitrag":
        kind_id = kind_id or None
    with db.tx():
        p = _projekt(db, projekt_id)
        pruefe_offen(p)
        if mit_zahlcode is None:
            mit_zahlcode = richtung == "eingang" and bool(kind_id)
        code = None
        if mit_zahlcode and status != "erlassen":
            if richtung != "eingang":
                raise ProjektFehler("Zahlcodes gibt es nur für erwartete Zahlungseingänge.")
            code = _zahlcode(p, naechste_nummer(db, p))
        pid = db.insert(
            "INSERT INTO offener_posten (projekt_id, richtung, kategorie, kind_id, bezeichnung, sollbetrag_cent,"
            " faellig_am, zahlcode, status, erlass_grund, notiz, erstellt_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (projekt_id, richtung, kategorie, kind_id, bezeichnung.strip(), sollbetrag_cent, faellig_am or None,
             code, status, erlass_grund, notiz, utcnow()))
        protokolliere(db, "offener_posten.anlegen", "offener_posten", pid, neu={
            "projekt_id": projekt_id, "richtung": richtung, "kategorie": kategorie, "kind_id": kind_id,
            "bezeichnung": bezeichnung, "sollbetrag_cent": sollbetrag_cent, "faellig_am": faellig_am,
            "zahlcode": code, "status": status, "erlass_grund": erlass_grund}, benutzer_id=benutzer_id)
        return pid


def posten_aus_umsatz(db, umsatz_id, *, projekt_id, bezeichnung, kategorie, betrag_cent, benutzer_id,
                      gegenposten=False, notiz=None):
    """Create an item for (part of) a transaction and assign it right away.

    With `gegenposten` a second, open item in the opposite direction is created,
    e.g. the expected refund of an advance payment. Matching can then find the
    incoming refund by its amount. Returns (item id, counter item id or None).
    """
    if kategorie == "elternbeitrag":
        raise ProjektFehler("Elternbeiträge bitte im Projekt pro Kind anlegen.")
    with db.tx():
        u = db.one("SELECT * FROM bankumsatz WHERE id=?", (umsatz_id,))
        if u is None:
            raise ProjektFehler("Der Umsatz existiert nicht.")
        richtung = "eingang" if u["betrag_cent"] > 0 else "ausgang"
        pid = posten_anlegen(db, projekt_id, richtung=richtung, kategorie=kategorie, bezeichnung=bezeichnung,
                             sollbetrag_cent=betrag_cent, benutzer_id=benutzer_id, mit_zahlcode=False, notiz=notiz)
        zuordnen(db, umsatz_id, [{"offener_posten_id": pid, "betrag_cent": betrag_cent}], "manuell", benutzer_id,
                 notiz=notiz)
        gid = None
        if gegenposten:
            datum = "{2}.{1}.{0}".format(*u["buchungsdatum"].split("-"))
            if richtung == "ausgang":
                gegen, text = "eingang", f"Rückzahlung: {bezeichnung.strip()}"
            else:
                gegen, text = "ausgang", f"Auszahlung: {bezeichnung.strip()}"
            gid = posten_anlegen(db, projekt_id, richtung=gegen, kategorie="erstattung", bezeichnung=text,
                                 sollbetrag_cent=betrag_cent, benutzer_id=benutzer_id, mit_zahlcode=False,
                                 notiz=f"Gegenposten zum Umsatz vom {datum} ({format_cent(u['betrag_cent'])})")
        return pid, gid


def kandidaten_massenanlage(db, projekt_id, klassen_ids):
    """Children of the selected classes, marked with BuT status and existing items."""
    if not klassen_ids:
        return []
    ph = ",".join("?" * len(klassen_ids))
    return db.all(
        "SELECT k.id, k.vorname, k.nachname, k.but_status, kl.bezeichnung AS klasse,"
        " EXISTS(SELECT 1 FROM offener_posten p WHERE p.kind_id=k.id AND p.projekt_id=? AND p.status<>'storniert')"
        "   AS hat_posten"
        f" FROM kind k JOIN klasse kl ON kl.id=k.klasse_id WHERE k.klasse_id IN ({ph}) AND k.aktiv=1"
        " AND k.anonymisiert=0 ORDER BY kl.bezeichnung, k.nachname, k.vorname",
        [projekt_id] + list(klassen_ids))


def massenanlage(db, projekt_id, kind_ids, betrag_cent, benutzer_id, *, bezeichnung=None, faellig_am=None,
                 but_kind_ids=(), but_modus="keiner"):
    """F3.1/F3.3: one item per child. BuT children are skipped unless
    explicitly listed in `but_kind_ids`; they then get no item or an
    item with status 'erlassen'."""
    if betrag_cent <= 0:
        raise ProjektFehler("Der Betrag muss größer als 0,00 € sein.")
    if but_modus not in ("keiner", "erlassen"):
        raise ProjektFehler("Ungültige Auswahl für BuT-Kinder.")
    ergebnis = {"angelegt": 0, "erlassen": 0, "uebersprungen_but": 0, "uebersprungen_vorhanden": 0}
    with db.tx():
        p = _projekt(db, projekt_id)
        pruefe_offen(p)
        if p["typ"] != "elternbeitrag":
            raise ProjektFehler("Die Massenanlage ist nur für Projekte vom Typ Elternbeitrag möglich.")
        bezeichnung = (bezeichnung or "").strip() or p["bezeichnung"]
        alle_ids = sorted(set(int(i) for i in kind_ids) | set(int(i) for i in but_kind_ids))
        if not alle_ids:
            raise ProjektFehler("Bitte mindestens ein Kind auswählen.")
        ph = ",".join("?" * len(alle_ids))
        kinder = db.all(
            "SELECT k.*, kl.bezeichnung AS klasse FROM kind k JOIN klasse kl ON kl.id=k.klasse_id"
            f" WHERE k.id IN ({ph}) ORDER BY kl.bezeichnung, k.nachname, k.vorname", alle_ids)
        but_ausdruecklich = set(int(i) for i in but_kind_ids)
        for k in kinder:
            vorhanden = db.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND kind_id=?"
                                  " AND status<>'storniert'", (projekt_id, k["id"]), 0)
            if vorhanden:
                ergebnis["uebersprungen_vorhanden"] += 1
                continue
            if k["but_status"]:
                if k["id"] not in but_ausdruecklich or but_modus == "keiner":
                    ergebnis["uebersprungen_but"] += 1
                    continue
                posten_anlegen(db, projekt_id, richtung="eingang", kategorie="elternbeitrag", kind_id=k["id"],
                               bezeichnung=bezeichnung, sollbetrag_cent=betrag_cent, faellig_am=faellig_am,
                               benutzer_id=benutzer_id, mit_zahlcode=False, status="erlassen",
                               erlass_grund="BuT/Härtefall")
                ergebnis["erlassen"] += 1
                continue
            posten_anlegen(db, projekt_id, richtung="eingang", kategorie="elternbeitrag", kind_id=k["id"],
                           bezeichnung=bezeichnung, sollbetrag_cent=betrag_cent, faellig_am=faellig_am,
                           benutzer_id=benutzer_id, mit_zahlcode=True)
            ergebnis["angelegt"] += 1
    return ergebnis


def ueberzahlung_klaeren(db, posten_id, grund, benutzer_id, aufheben=False):
    """F3.6: mark an overpayment as settled (or undo that). No money is moved."""
    from .buchung import bezahlt_cent
    with db.tx():
        p = db.one("SELECT * FROM offener_posten WHERE id=?", (posten_id,))
        if p is None:
            raise ProjektFehler("Der Posten existiert nicht.")
        if aufheben:
            werte = (None, None, None, None)
        else:
            grund = (grund or "").strip()
            if not grund:
                raise ProjektFehler("Bitte angeben, wie die Überzahlung geklärt wurde.")
            ueber = bezahlt_cent(db, posten_id) - p["sollbetrag_cent"]
            if ueber <= 0:
                raise ProjektFehler("Für diesen Posten gibt es keine Überzahlung.")
            werte = (ueber, grund, benutzer_id, utcnow())
        db.execute("UPDATE offener_posten SET ueberzahlung_geklaert_cent=?, ueberzahlung_grund=?,"
                   " ueberzahlung_geklaert_von=?, ueberzahlung_geklaert_am=? WHERE id=?", werte + (posten_id,))
        protokolliere(db, "offener_posten.ueberzahlung_aufheben" if aufheben else "offener_posten.ueberzahlung_klaeren",
                      "offener_posten", posten_id,
                      alt={"ueberzahlung_geklaert_cent": p["ueberzahlung_geklaert_cent"],
                           "ueberzahlung_grund": p["ueberzahlung_grund"]},
                      neu={"ueberzahlung_geklaert_cent": werte[0], "ueberzahlung_grund": werte[1]},
                      benutzer_id=benutzer_id)


def _posten(db, posten_id):
    p = db.one("SELECT * FROM offener_posten WHERE id=?", (posten_id,))
    if p is None:
        raise ProjektFehler("Der Posten existiert nicht.")
    pruefe_offen(_projekt(db, p["projekt_id"]))
    return p


def posten_aendern(db, posten_id, *, bezeichnung, sollbetrag_cent, faellig_am, notiz, benutzer_id):
    if sollbetrag_cent <= 0:
        raise ProjektFehler("Der Sollbetrag muss größer als 0,00 € sein.")
    if not bezeichnung.strip():
        raise ProjektFehler("Bitte eine Bezeichnung angeben.")
    with db.tx():
        alt = _posten(db, posten_id)
        db.execute("UPDATE offener_posten SET bezeichnung=?, sollbetrag_cent=?, faellig_am=?, notiz=? WHERE id=?",
                   (bezeichnung.strip(), sollbetrag_cent, faellig_am or None, notiz or None, posten_id))
        aktualisiere_posten_status(db, posten_id)
        protokolliere(db, "offener_posten.aendern", "offener_posten", posten_id, alt=alt, neu={
            "bezeichnung": bezeichnung, "sollbetrag_cent": sollbetrag_cent, "faellig_am": faellig_am,
            "notiz": notiz}, benutzer_id=benutzer_id)


def beitragshoehen(db, projekt_id):
    """Distinct target amounts of the parent contributions of a project with their counts."""
    return db.all("SELECT sollbetrag_cent, COUNT(*) AS anzahl,"
                  " SUM(letzter_brief_am IS NOT NULL) AS mit_brief FROM offener_posten"
                  " WHERE projekt_id=? AND kategorie='elternbeitrag' AND status<>'storniert'"
                  " GROUP BY sollbetrag_cent ORDER BY anzahl DESC, sollbetrag_cent", (projekt_id,))


def beitraege_aendern(db, projekt_id, alt_cent, neu_cent, benutzer_id, bezeichnung=None):
    """Change the amount of all parent contributions with amount `alt_cent` at once.

    Payment codes and payments stay; the item status is recalculated, so
    already paid items may become partly paid or overpaid. Returns statistics.
    """
    if neu_cent <= 0:
        raise ProjektFehler("Der neue Betrag muss größer als 0,00 € sein.")
    if neu_cent == alt_cent and not (bezeichnung or "").strip():
        raise ProjektFehler("Der neue Betrag ist gleich dem bisherigen.")
    with db.tx():
        p = _projekt(db, projekt_id)
        pruefe_offen(p)
        ids = [r[0] for r in db.all("SELECT id FROM offener_posten WHERE projekt_id=? AND kategorie='elternbeitrag'"
                                    " AND status<>'storniert' AND sollbetrag_cent=?", (projekt_id, alt_cent))]
        if not ids:
            raise ProjektFehler(f"Es gibt keine Elternbeiträge über {format_cent(alt_cent)}.")
        erg = {"geaendert": 0, "mit_brief": 0, "bezahlt": 0, "ueberzahlt": 0}
        for pid in ids:
            alt = _posten(db, pid)
            neu_bez = (bezeichnung or "").strip() or alt["bezeichnung"]
            db.execute("UPDATE offener_posten SET sollbetrag_cent=?, bezeichnung=? WHERE id=?", (neu_cent, neu_bez, pid))
            aktualisiere_posten_status(db, pid)
            protokolliere(db, "offener_posten.aendern", "offener_posten", pid, alt=alt,
                          neu={"sollbetrag_cent": neu_cent, "bezeichnung": neu_bez, "sammelaenderung": True},
                          benutzer_id=benutzer_id)
            bezahlt = bezahlt_cent(db, pid)
            erg["geaendert"] += 1
            erg["mit_brief"] += alt["letzter_brief_am"] is not None
            erg["bezahlt"] += bezahlt > 0
            erg["ueberzahlt"] += bezahlt > neu_cent
        protokolliere(db, "projekt.beitraege_aendern", "projekt", projekt_id,
                      alt={"sollbetrag_cent": alt_cent}, neu={"sollbetrag_cent": neu_cent, "anzahl": erg["geaendert"]},
                      benutzer_id=benutzer_id)
    return erg


def posten_status_setzen(db, posten_id, status, grund, benutzer_id):
    """Explicit status erlassen/storniert (with reason) or back to computed status."""
    grund = (grund or "").strip()
    with db.tx():
        p = _posten(db, posten_id)
        if status in ("erlassen", "storniert"):
            if not grund:
                raise ProjektFehler("Bitte eine Begründung angeben.")
            if status == "storniert" and bezahlt_cent(db, posten_id) > 0:
                raise ProjektFehler("Der Posten hat Zahlungen. Bitte zuerst die Zuordnungen stornieren "
                                    "oder den Rest erlassen.")
            db.execute("UPDATE offener_posten SET status=?, erlass_grund=? WHERE id=?", (status, grund, posten_id))
            db.execute("DELETE FROM vorschlag WHERE id IN (SELECT vorschlag_id FROM vorschlag_teil"
                       " WHERE offener_posten_id=?)", (posten_id,))
        elif status == "reaktivieren":
            if p["status"] not in ("erlassen", "storniert"):
                raise ProjektFehler("Nur erlassene oder stornierte Posten können reaktiviert werden.")
            db.execute("UPDATE offener_posten SET status='offen', erlass_grund=NULL WHERE id=?", (posten_id,))
            if p["zahlcode"] is None and p["richtung"] == "eingang" and p["kind_id"]:
                projekt = _projekt(db, p["projekt_id"])
                db.execute("UPDATE offener_posten SET zahlcode=? WHERE id=?",
                           (_zahlcode(projekt, naechste_nummer(db, projekt)), posten_id))
            aktualisiere_posten_status(db, posten_id)
        else:
            raise ProjektFehler("Ungültiger Status.")
        neu = db.one("SELECT status, erlass_grund, zahlcode FROM offener_posten WHERE id=?", (posten_id,))
        protokolliere(db, f"offener_posten.{status}", "offener_posten", posten_id,
                      alt={"status": p["status"], "erlass_grund": p["erlass_grund"]}, neu=neu,
                      benutzer_id=benutzer_id)
