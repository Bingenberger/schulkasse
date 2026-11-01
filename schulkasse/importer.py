"""CAMT import workflow: analysis/preview, write, matching (F1.5-F1.10)."""
from dataclasses import dataclass, field, replace

from .camt import CamtFehler
from .db import utcnow
from .geld import format_cent
from .matching import matching_lauf
from .protokoll import protokolliere
from .textnorm import iban_formatiert, normalisiere

PDNG_FENSTER_TAGE = 14


@dataclass
class Analyse:
    """Preview of one file for one account (F1.9)."""
    datei: object
    konto: object
    iban: str
    umsaetze: list = field(default_factory=list)
    neu: list = field(default_factory=list)
    duplikate: list = field(default_factory=list)
    ersetzt: dict = field(default_factory=dict)   # index in neu -> bankumsatz id of pending entry

    @property
    def zeitraum(self):
        return self.datei.zeitraum

    @property
    def summe_eingaenge(self):
        return sum(u.betrag_cent for u in self.neu if u.betrag_cent > 0 and u.status == "BOOK")

    @property
    def summe_ausgaenge(self):
        return sum(u.betrag_cent for u in self.neu if u.betrag_cent < 0 and u.status == "BOOK")

    @property
    def anzahl_vorgemerkt(self):
        return sum(1 for u in self.neu if u.status == "PDNG")

    @property
    def schlusssaldo(self):
        return self.datei.schlusssaldo


def _pdng_kandidat(db, konto_id, u, schon_ersetzt):
    """Find a pending entry that the booked entry `u` supersedes (F1.5)."""
    rows = db.all(
        "SELECT id, verwendungszweck_roh, verwendungszweck, end_to_end_id, dedup_hash FROM bankumsatz"
        " WHERE konto_id=? AND status='PDNG' AND betrag_cent=? AND COALESCE(gegen_iban,'')=?"
        " AND ABS(julianday(buchungsdatum) - julianday(?)) <= ?",
        (konto_id, u.betrag_cent, u.gegen_iban or "", u.buchungsdatum, PDNG_FENSTER_TAGE))
    zweck = normalisiere(u.verwendungszweck_roh or u.verwendungszweck or "")
    for r in rows:
        if r["id"] in schon_ersetzt:
            continue
        if r["dedup_hash"] == u.dedup_hash:
            return r["id"]
        if u.end_to_end_id and r["end_to_end_id"] == u.end_to_end_id:
            return r["id"]
        if normalisiere(r["verwendungszweck_roh"] or r["verwendungszweck"] or "") == zweck:
            return r["id"]
    return None


def analysiere(db, dateien):
    """Build per-file/per-account previews without writing anything."""
    analysen = []
    gesehen = set()
    ersetzt_ids = set()
    for datei in dateien:
        for iban in datei.ibans:
            konto = db.one("SELECT * FROM konto WHERE iban=?", (iban,))
            if konto is None or not konto["aktiv"]:
                raise CamtFehler(
                    f"Die Datei „{datei.dateiname}“ enthält Umsätze für das Konto {iban_formatiert(iban)}. "
                    "Dieses Konto ist in der Schulkasse nicht (aktiv) angelegt. Der Import wurde abgebrochen, "
                    "es wurden keine Daten übernommen.")
            konto_datei = replace(datei, berichte=[b for b in datei.berichte if b.iban == iban])
            a = Analyse(datei=konto_datei, konto=konto, iban=iban)
            for b in datei.berichte:
                if b.iban == iban:
                    a.umsaetze.extend(b.umsaetze)
            for u in a.umsaetze:
                key = (konto["id"], u.dedup_hash)
                if key in gesehen:
                    a.duplikate.append(u)
                    continue
                vorhanden = db.one("SELECT id, status FROM bankumsatz WHERE konto_id=? AND dedup_hash=?", key)
                if vorhanden is not None and not (vorhanden["status"] == "PDNG" and u.status == "BOOK"):
                    a.duplikate.append(u)
                    gesehen.add(key)
                    continue
                pdng_id = None
                if u.status == "BOOK":
                    pdng_id = vorhanden["id"] if vorhanden is not None else _pdng_kandidat(db, konto["id"], u, ersetzt_ids)
                if pdng_id:
                    ersetzt_ids.add(pdng_id)
                    a.ersetzt[len(a.neu)] = pdng_id
                a.neu.append(u)
                gesehen.add(key)
            analysen.append(a)
    if not analysen:
        raise CamtFehler("Die Datei enthält keine Kontoumsätze.")
    return analysen


def importiere(db, analysen, benutzer_id, schwelle=100):
    """Write analysed files, then run matching over the new transactions."""
    neue_ids = []
    ergebnisse = []
    with db.tx():
        for a in analysen:
            konto = a.konto
            von, bis = a.zeitraum
            s = a.schlusssaldo
            jetzt = utcnow()
            import_id = db.insert(
                "INSERT INTO import (konto_id, dateiname, datei_hash, zeitraum_von, zeitraum_bis, anzahl_gesamt,"
                " anzahl_neu, anzahl_duplikat, saldo_schluss_cent, saldo_datum, importiert_von, importiert_am)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (konto["id"], a.datei.dateiname, a.datei.datei_hash, von, bis, len(a.umsaetze), 0, 0,
                 s.betrag_cent if s else None, s.datum if s else None, benutzer_id, jetzt))
            erster_import = db.scalar("SELECT COUNT(*) FROM bankumsatz WHERE konto_id=?", (konto["id"],), 0) == 0

            neu = dup = 0
            for i, u in enumerate(a.neu):
                pdng_id = a.ersetzt.get(i)
                if pdng_id:
                    alt = db.one("SELECT * FROM bankumsatz WHERE id=?", (pdng_id,))
                    if alt is not None and alt["status"] == "PDNG" and alt["zuordnungsstatus"] == "offen":
                        db.execute("DELETE FROM bankumsatz WHERE id=?", (pdng_id,))
                        protokolliere(db, "bankumsatz.vorgemerkt_ersetzt", "bankumsatz", pdng_id,
                                      alt=alt, benutzer_id=benutzer_id)
                cur = db.execute(
                    "INSERT INTO bankumsatz (konto_id, import_id, buchungsdatum, valutadatum, betrag_cent, waehrung,"
                    " status, gegen_name, gegen_iban, verwendungszweck, verwendungszweck_roh, end_to_end_id,"
                    " bank_ref, geschaeftsvorfall, dedup_hash, zuordnungsstatus, erstellt_am)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'offen',?)"
                    " ON CONFLICT (konto_id, dedup_hash) DO NOTHING",
                    (konto["id"], import_id, u.buchungsdatum, u.valutadatum, u.betrag_cent, u.waehrung, u.status,
                     u.gegen_name, u.gegen_iban, u.verwendungszweck, u.verwendungszweck_roh, u.end_to_end_id,
                     u.bank_ref, u.geschaeftsvorfall, u.dedup_hash, jetzt))
                if cur.rowcount == 1:
                    neu += 1
                    if u.status == "BOOK":
                        neue_ids.append(cur.lastrowid)
                else:
                    dup += 1
            dup += len(a.duplikate)
            db.execute("UPDATE import SET anzahl_neu=?, anzahl_duplikat=? WHERE id=?", (neu, dup, import_id))

            if erster_import and konto["eroeffnungssaldo_cent"] is None:
                anfang = a.datei.anfangssaldo
                if anfang is not None and anfang.typ in ("OPBD", "PRCD"):
                    wert, datum = anfang.betrag_cent, anfang.datum
                elif s is not None:
                    wert = s.betrag_cent - sum(x.betrag_cent for x in a.umsaetze if x.status == "BOOK")
                    datum = von
                else:
                    wert = datum = None
                if wert is not None:
                    db.execute("UPDATE konto SET eroeffnungssaldo_cent=?, eroeffnungssaldo_datum=? WHERE id=?",
                               (wert, datum, konto["id"]))
                    protokolliere(db, "konto.eroeffnungssaldo", "konto", konto["id"],
                                  neu={"eroeffnungssaldo_cent": wert, "datum": datum}, benutzer_id=benutzer_id)

            protokolliere(db, "import.ausfuehren", "import", import_id, neu={
                "dateiname": a.datei.dateiname, "konto": konto["iban"], "zeitraum": [von, bis],
                "anzahl_gesamt": len(a.umsaetze), "anzahl_neu": neu, "anzahl_duplikat": dup,
                "ersetzt_vorgemerkt": len(a.ersetzt),
                "saldo": format_cent(s.betrag_cent) if s else None,
            }, benutzer_id=benutzer_id)
            ergebnisse.append({"import_id": import_id, "dateiname": a.datei.dateiname, "konto": konto,
                               "neu": neu, "duplikate": dup, "ersetzt": len(a.ersetzt)})
    # F1.10: matching over all new transactions.
    stats = matching_lauf(db, benutzer_id, umsatz_ids=neue_ids, schwelle=schwelle)
    return ergebnisse, stats
