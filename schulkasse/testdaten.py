"""Synthetic test data (F12.4): demo master data and matching CAMT.052 files.

The CAMT files reference payment codes that `demo_anlegen` creates. Because
the demo data is deterministic, the files written by `schreibe_testdateien`
match every database that was filled with `demo_anlegen`.
"""
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .db import Database, migrate, utcnow
from .projekte import massenanlage, posten_anlegen, projekt_anlegen
from .stammdaten import csv_importieren, lies_klassenliste, schuljahr_anlegen
from .textnorm import iban_mit_pruefziffer

SCHUL_IBAN = iban_mit_pruefziffer("DE", "370695200012345678")
SCHUL_BIC = "GENODED1NKS"
FREMD_IBAN = iban_mit_pruefziffer("DE", "370501980099887766")


def _iban(n):
    return iban_mit_pruefziffer("DE", f"3705019800{n:08d}")


KLASSENLISTE = """klasse;nachname;vorname;familie
4a;Becker;Anna;Familie Becker
4a;Schmidt;Ben;
4a;Fischer;Clara;
4a;Wagner;David;
4a;Hoffmann;Emma;
4a;Yilmaz;Emre;Familie Yilmaz
4a;Koch;Finn;
4a;Richter;Greta;
4a;Klein;Hannah;
4a;Wolf;Jonas;
4a;Schröder;Lea;
4a;Neumann;Leon;
4a;Schwarz;Lina;
4a;Zimmermann;Luca;
4a;Braun;Mia;
4a;Krüger;Noah;
4a;Hofmann;Paul;
4a;Hartmann;Sophie;
4a;Lange;Tim;
4a;Schmitt;Lara;
4a;Werner;Elias;
4a;Krause;Marie;
4a;Meier;Jan;
4a;Lehmann;Nele;
4a;Köhler;Moritz;
4a;Kaya;Aylin;
2b;Yilmaz;Elif;Familie Yilmaz
2b;Becker;Tom;Familie Becker
2b;Özdemir;Can;
2b;Schulz;Ida;
2b;Vogel;Jana;
2b;Jung;Max;
2b;Hahn;Lotta;
2b;Frank;Ole;
"""

BUT_KIND = ("Moritz", "Köhler")


def demo_anlegen(db, benutzer_id):
    """Deterministic demo data: account, school year, 34 children, projects, items."""
    with db.tx():
        db.execute("INSERT INTO konto (bezeichnung, iban, bic, inhaber, aktiv) VALUES (?,?,?,?,1)",
                   ("Schulkonto", SCHUL_IBAN, SCHUL_BIC, "GGS Niederkassel Schulkonto"))
        sj = schuljahr_anlegen(db, "2026/27", "2026-08-01", "2027-07-31", benutzer_id, aktiv=True)
        zeilen = lies_klassenliste(KLASSENLISTE.encode("utf-8"))
        csv_importieren(db, zeilen, sj, [], benutzer_id)
        db.execute("UPDATE kind SET but_status=1, but_notiz='BuT-Bescheid liegt vor' WHERE vorname=? AND nachname=?",
                   BUT_KIND)
        k4a = db.scalar("SELECT id FROM klasse WHERE bezeichnung='4a' AND schuljahr_id=?", (sj,))
        k2b = db.scalar("SELECT id FROM klasse WHERE bezeichnung='2b' AND schuljahr_id=?", (sj,))

        kf = projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Klassenfahrt 4a Nettersheim",
                                  "kurzcode": "KF4A", "typ": "elternbeitrag", "klasse_id": k4a,
                                  "beschreibung": "Klassenfahrt vom 12. bis 14. Oktober 2026"}, benutzer_id)
        th = projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Theaterbesuch 2b", "kurzcode": "TH2B",
                                  "typ": "elternbeitrag", "klasse_id": k2b}, benutzer_id)
        ah = projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Arbeitshefte Vorkasse", "kurzcode": "AH26",
                                  "typ": "vorkasse"}, benutzer_id)
        projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Kopiergeld", "kurzcode": "KOPIE",
                             "typ": "sammeltopf"}, benutzer_id)
        projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Spenden", "kurzcode": "SPENDE",
                             "typ": "sammeltopf"}, benutzer_id)

        kinder_4a = [r[0] for r in db.all("SELECT id FROM kind WHERE klasse_id=?", (k4a,))]
        kinder_2b = [r[0] for r in db.all("SELECT id FROM kind WHERE klasse_id=?", (k2b,))]
        massenanlage(db, kf, kinder_4a, 4500, benutzer_id, faellig_am="2026-09-30")
        massenanlage(db, th, kinder_2b, 850, benutzer_id, faellig_am="2026-09-20")
        posten_anlegen(db, ah, richtung="ausgang", kategorie="rechnung", bezeichnung="Rechnung Westermann 2026-4711",
                       sollbetrag_cent=31240, benutzer_id=benutzer_id)
        posten_anlegen(db, ah, richtung="eingang", kategorie="erstattung",
                       bezeichnung="Erstattung Stadtkasse Bestellung 4711", sollbetrag_cent=31240,
                       benutzer_id=benutzer_id, mit_zahlcode=False)
    return {"schuljahr_id": sj, "kinder": len(kinder_4a) + len(kinder_2b)}


# ----------------------------------------------------------------- CAMT

@dataclass
class Buchung:
    datum: str
    betrag_cent: int          # signed
    zweck: str
    name: str | None = None
    iban: str | None = None
    ref: str | None = None
    e2e: str | None = None
    status: str = "BOOK"
    gvc: str = "166"
    zusatz: str | None = None


def _ustrd(text):
    """German banks split remittance text into 35-character blocks."""
    return "".join(f"<Ustrd>{escape(text[i:i + 35])}</Ustrd>" for i in range(0, len(text), 35))


def _betrag(cent):
    return f"{abs(cent) // 100}.{abs(cent) % 100:02d}"


def _bal(typ, cent, datum):
    ind = "CRDT" if cent >= 0 else "DBIT"
    return (f"<Bal><Tp><CdOrPrtry><Cd>{typ}</Cd></CdOrPrtry></Tp><Amt Ccy=\"EUR\">{_betrag(cent)}</Amt>"
            f"<CdtDbtInd>{ind}</CdtDbtInd><Dt><Dt>{datum}</Dt></Dt></Bal>")


def _ntry(b, version):
    eingang = b.betrag_cent > 0
    ind = "CRDT" if eingang else "DBIT"
    sts = f"<Sts><Cd>{b.status}</Cd></Sts>" if version == "08" else f"<Sts>{b.status}</Sts>"
    rolle = "Dbtr" if eingang else "Cdtr"
    parteien = ""
    if b.name or b.iban:
        nm = ""
        if b.name:
            nm = (f"<{rolle}><Pty><Nm>{escape(b.name)}</Nm></Pty></{rolle}>" if version == "08"
                  else f"<{rolle}><Nm>{escape(b.name)}</Nm></{rolle}>")
        konto = f"<{rolle}Acct><Id><IBAN>{b.iban}</IBAN></Id></{rolle}Acct>" if b.iban else ""
        parteien = f"<RltdPties>{nm}{konto}</RltdPties>"
    refs = f"<Refs><EndToEndId>{escape(b.e2e or 'NOTPROVIDED')}</EndToEndId></Refs>"
    rmt = f"<RmtInf>{_ustrd(b.zweck)}</RmtInf>" if b.zweck else ""
    domn = ("<Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn>" if eingang else
            "<Domn><Cd>PMNT</Cd><Fmly><Cd>ICDT</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn>")
    return (
        f"<Ntry><Amt Ccy=\"EUR\">{_betrag(b.betrag_cent)}</Amt><CdtDbtInd>{ind}</CdtDbtInd>{sts}"
        f"<BookgDt><Dt>{b.datum}</Dt></BookgDt><ValDt><Dt>{b.datum}</Dt></ValDt>"
        + (f"<AcctSvcrRef>{escape(b.ref)}</AcctSvcrRef>" if b.ref else "")
        + f"<BkTxCd>{domn}<Prtry><Cd>NTRF+{b.gvc}</Cd><Issr>DK</Issr></Prtry></BkTxCd>"
        f"<NtryDtls><TxDtls>{refs}{parteien}{rmt}</TxDtls></NtryDtls>"
        + (f"<AddtlNtryInf>{escape(b.zusatz)}</AddtlNtryInf>" if b.zusatz else "")
        + "</Ntry>"
    )


def camt_xml(buchungen, *, iban, von, bis, anfangssaldo, version="08", msg_id="MSG1"):
    ns = f"urn:iso:std:iso:20022:tech:xsd:camt.052.001.{version}"
    gebucht = sum(b.betrag_cent for b in buchungen if b.status == "BOOK")
    ntries = "".join(_ntry(b, version) for b in sorted(buchungen, key=lambda x: x.datum))
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{ns}">'
        f"<BkToCstmrAcctRpt><GrpHdr><MsgId>{msg_id}</MsgId><CreDtTm>{bis}T18:00:00</CreDtTm></GrpHdr>"
        f"<Rpt><Id>{msg_id}-1</Id><CreDtTm>{bis}T18:00:00</CreDtTm>"
        f"<FrToDt><FrDtTm>{von}T00:00:00</FrDtTm><ToDtTm>{bis}T23:59:59</ToDtTm></FrToDt>"
        f"<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>EUR</Ccy><Svcr><FinInstnId><BICFI>{SCHUL_BIC}</BICFI>"
        f"</FinInstnId></Svcr></Acct>"
        + _bal("OPBD", anfangssaldo, von)
        + _bal("CLBD", anfangssaldo + gebucht, bis)
        + ntries + "</Rpt></BkToCstmrAcctRpt></Document>\n"
    ).encode("utf-8")


ANFANGSSALDO = 125000


def _codes(db):
    rows = db.all("SELECT p.zahlcode, k.vorname, k.nachname FROM offener_posten p JOIN kind k ON k.id=p.kind_id"
                  " WHERE p.zahlcode IS NOT NULL")
    return {(r["vorname"], r["nachname"]): r["zahlcode"] for r in rows}


def buchungen(db):
    """The scenario of F12.4. Returns (teil1, teil2)."""
    c = _codes(db)
    kf = lambda v, n: c[(v, n)]  # noqa: E731
    richter_e2e = "E2E-RICHTER-20260907"
    fee = Buchung("2026-09-05", -750, "", zusatz="Kontoführung 08/2026", ref="2026090500071", gvc="805")
    erstattung = Buchung("2026-09-05", 31240, "Erstattung Bestellung 4711 Arbeitshefte 2026",
                         "Stadtkasse Niederkassel", _iban(9001), ref="2026090500072", e2e="STADT-4711")
    spende = Buchung("2026-09-05", 5000, "Spende Leseförderung", "Hannelore Weber", _iban(9002),
                     ref="2026090500073")
    kopie_a = Buchung("2026-09-06", 500, "Kopiergeld", "Petra Lange", _iban(1019))
    kopie_b = Buchung("2026-09-06", 500, "Kopiergeld", "Petra Lange", _iban(1019))
    teil1 = [
        # correct code, exact amount -> R1
        Buchung("2026-09-01", 4500, f"{kf('Anna', 'Becker')} Klassenfahrt 4a Nettersheim Anna Becker",
                "Thomas Becker", _iban(1001), ref="2026090100011", e2e="BECKER-KF-1"),
        # mangled code: lower case, blank instead of hyphen, no references -> R1 via normalisation
        Buchung("2026-09-02", 4500, f"{kf('Ben', 'Schmidt').lower().replace('-', ' ')} klassenfahrt ben",
                "Sandra Schmidt", _iban(1002)),
        # SEPA prefix structure in the remittance text
        Buchung("2026-09-02", 4500, f"EREF+NOTPROVIDED SVWZ+{kf('Clara', 'Fischer')} Klassenfahrt Clara Fischer",
                "Fischer, Martin", _iban(1003), ref="2026090200013"),
        # collective payment for two siblings -> R3 split suggestion
        Buchung("2026-09-03", 5350, f"{kf('Emre', 'Yilmaz')} und {kf('Elif', 'Yilmaz')} Klassenfahrt + Theater",
                "Mehmet Yilmaz", _iban(1006), ref="2026090300014"),
        # partial payment -> R2
        Buchung("2026-09-03", 2000, f"{kf('David', 'Wagner')} erste Rate", "Wagner, Julia", _iban(1004),
                ref="2026090300015"),
        # overpayment -> R2
        Buchung("2026-09-04", 5000, f"{kf('Emma', 'Hoffmann')} Klassenfahrt", "Kai Hoffmann", _iban(1005),
                ref="2026090400016"),
        # no code at all, surname in sender name -> R6
        Buchung("2026-09-04", 4500, "Klassenfahrt Finn 4a", "Koch, Stefanie", _iban(1007), ref="2026090400017"),
        # outgoing payment to supplier (Vorkasse)
        Buchung("2026-09-01", -31240, "Rechnung 2026-4711 Kundennr 5566", "Westermann Bildungsmedien GmbH",
                _iban(9003), ref="2026090100018", gvc="116"),
        fee, erstattung, spende, kopie_a, kopie_b,
        # pending entry, booked later in part 2
        Buchung("2026-09-07", 4500, f"{kf('Greta', 'Richter')} Klassenfahrt Greta", "Richter, Tanja",
                _iban(1008), e2e=richter_e2e, status="PDNG"),
    ]
    teil2 = [
        fee, erstattung, spende, kopie_a, kopie_b,
        Buchung("2026-09-08", 4500, f"{kf('Greta', 'Richter')} Klassenfahrt Greta", "Richter, Tanja",
                _iban(1008), e2e=richter_e2e, ref="2026090800021"),
        # remaining amount after the partial payment -> R1
        Buchung("2026-09-08", 2500, f"{kf('David', 'Wagner')} Restzahlung", "Wagner, Julia", _iban(1004),
                ref="2026090800022"),
        Buchung("2026-09-09", 4500, f"{kf('Hannah', 'Klein')} Klassenfahrt", "Klein, Oliver", _iban(1009),
                ref="2026090900023"),
        Buchung("2026-09-10", 850, f"{kf('Ida', 'Schulz')} Theater Ida", "Schulz, Andrea", _iban(1010),
                ref="2026091000024"),
    ]
    return teil1, teil2


def camt_dateien(db):
    teil1, teil2 = buchungen(db)
    xml1 = camt_xml(teil1, iban=SCHUL_IBAN, von="2026-09-01", bis="2026-09-07", anfangssaldo=ANFANGSSALDO,
                    version="08", msg_id="CAMT-TEIL1")
    saldo_vor_teil2 = ANFANGSSALDO + sum(b.betrag_cent for b in teil1 if b.status == "BOOK" and b.datum < "2026-09-05")
    xml2 = camt_xml(teil2, iban=SCHUL_IBAN, von="2026-09-05", bis="2026-09-11", anfangssaldo=saldo_vor_teil2,
                    version="02", msg_id="CAMT-TEIL2")
    fremd = camt_xml(teil1[:2], iban=FREMD_IBAN, von="2026-09-01", bis="2026-09-07", anfangssaldo=0,
                     msg_id="CAMT-FREMD")
    return {"camt052_teil1.xml": xml1, "camt052_teil2_ueberlappend.xml": xml2, "camt052_fremdkonto.xml": fremd}


def _demo_db():
    db = Database(":memory:")
    migrate(db)
    uid = db.insert("INSERT INTO benutzer (benutzername, anzeigename, passwort_hash, rolle, erstellt_am)"
                    " VALUES ('generator','Generator','-','admin',?)", (utcnow(),))
    demo_anlegen(db, uid)
    return db


def schreibe_testdateien(ziel):
    ziel = Path(ziel)
    ziel.mkdir(parents=True, exist_ok=True)
    db = _demo_db()
    pfade = []
    for name, daten in camt_dateien(db).items():
        (ziel / name).write_bytes(daten)
        pfade.append(ziel / name)
    (ziel / "klassenliste.csv").write_bytes(KLASSENLISTE.encode("cp1252"))
    pfade.append(ziel / "klassenliste.csv")
    db.close()
    return pfade


def importiere_testdateien(db, benutzer_id, schwelle=100):
    from .camt import parse_xml
    from .importer import analysiere, importiere
    stats = []
    for name, daten in list(camt_dateien(db).items())[:2]:
        _, s = importiere(db, analysiere(db, [parse_xml(daten, name)]), benutzer_id, schwelle)
        stats.append(s)
    return stats
