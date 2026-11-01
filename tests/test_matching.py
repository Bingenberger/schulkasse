"""Matching cascade (F4)."""
from schulkasse.buchung import zuordnen
from schulkasse.matching import Kontext, bewerte, finde_codes, matching_lauf
from schulkasse.textnorm import normalisiere

from .conftest import posten_von


def umsatz(betrag, zweck, name=None, iban=None):
    return {"betrag_cent": betrag, "verwendungszweck": zweck, "verwendungszweck_roh": zweck,
            "gegen_name": name, "gegen_iban": iban}


def test_normalisierung():
    assert normalisiere("Müller-Lüdenscheidt, Straße 5!") == "MUELLERLUEDENSCHEIDTSTRASSE5"
    assert normalisiere("kf4a 0042") == "KF4A0042"


def test_code_mit_vorangestelltem_wort():
    codes = {"KF4A0042": "p"}
    assert finde_codes(normalisiere("Zahlung KF4A-0042 Ben"), codes) == ["p"]
    assert finde_codes(normalisiere("ZahlungKF4A0042"), codes) == ["p"]
    assert finde_codes(normalisiere("KF4A-0043"), codes) == []


def test_kuerzerer_code_in_laengerem_wird_verworfen():
    codes = {"KF4A0042": "lang", "F4A0042": "kurz"}
    assert finde_codes("KF4A0042", codes) == ["lang"]


def test_r1_bis_r3(demo):
    ctx = Kontext.lade(demo)
    anna = posten_von(demo, "Anna", "Becker")
    t = bewerte(umsatz(4500, f"{anna['zahlcode']} Klassenfahrt"), ctx)
    assert t[0].regel == "R1" and t[0].konfidenz == 100
    t = bewerte(umsatz(4000, anna["zahlcode"]), ctx)
    assert t[0].regel == "R2" and t[0].konfidenz == 80 and "Unterzahlung" in t[0].hinweis
    t = bewerte(umsatz(5000, anna["zahlcode"]), ctx)
    assert t[0].regel == "R2" and "Überzahlung" in t[0].hinweis
    emre = posten_von(demo, "Emre", "Yilmaz")
    elif_ = posten_von(demo, "Elif", "Yilmaz")
    t = bewerte(umsatz(5350, f"{emre['zahlcode']} {elif_['zahlcode']}"), ctx)
    assert t[0].regel == "R3" and t[0].konfidenz == 90
    assert sorted(x.betrag_cent for x in t[0].teile) == [850, 4500]
    assert bewerte(umsatz(9999, f"{emre['zahlcode']} {elif_['zahlcode']}"), ctx) == []


def test_r4_r5_nach_lernen(demo, admin_id):
    """F4.5: a manual assignment teaches the payer IBAN of the family."""
    from schulkasse.db import utcnow
    db = demo
    konto = db.scalar("SELECT id FROM konto")
    imp = db.insert("INSERT INTO import (konto_id, dateiname, datei_hash, anzahl_gesamt, anzahl_neu, anzahl_duplikat,"
                    " importiert_von, importiert_am) VALUES (?,?,?,?,?,?,?,?)", (konto, "x", "x", 0, 0, 0, admin_id, utcnow()))
    iban = "DE02120300000000202051"
    uid = db.insert("INSERT INTO bankumsatz (konto_id, import_id, buchungsdatum, betrag_cent, status, gegen_name,"
                    " gegen_iban, verwendungszweck, dedup_hash, erstellt_am) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (konto, imp, "2026-09-01", 1000, "BOOK", "Oma Yilmaz", iban, "Taschengeld", "h1", utcnow()))
    emre = posten_von(db, "Emre", "Yilmaz")
    zuordnen(db, uid, [{"offener_posten_id": emre["id"], "betrag_cent": 1000}], "manuell", admin_id)
    assert db.one("SELECT * FROM zahler_iban WHERE iban=?", (iban,))["treffer_anzahl"] == 1

    ctx = Kontext.lade(db)
    t = bewerte(umsatz(850, "Theater", "Oma", iban), ctx)  # only Elif's item matches 8,50
    assert t[0].regel == "R4" and t[0].konfidenz == 70
    t = bewerte(umsatz(3500 + 850, "Rest fuer beide", "Oma", iban), ctx)
    assert t[0].regel == "R5" and t[0].konfidenz == 60 and len(t[0].teile) == 2


def test_r6_name(demo):
    ctx = Kontext.lade(demo)
    t = bewerte(umsatz(4500, "Klassenfahrt", "Koch, Stefanie"), ctx)
    assert t[0].regel == "R6" and t[0].konfidenz == 50
    koch = posten_von(demo, "Finn", "Koch")
    assert t[0].teile[0].posten_id == koch["id"]
    assert bewerte(umsatz(4400, "Klassenfahrt", "Koch, Stefanie"), ctx) == []


def test_ausgang_nur_gegen_ausgangsposten(demo):
    ctx = Kontext.lade(demo)
    t = bewerte(umsatz(-31240, "Rechnung 4711", "Westermann"), ctx)
    assert t and all(ctx.posten[x.posten_id].richtung == "ausgang" for x in t[0].teile)


def test_schwelle_konfigurierbar(demo, admin_id, dateien):
    from .conftest import importiere_datei
    _, stats = importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id, schwelle=90)
    # R3 (90) is now booked automatically as well
    emre = posten_von(demo, "Emre", "Yilmaz")
    assert demo.one("SELECT status FROM offener_posten WHERE id=?", (emre["id"],))["status"] == "erledigt"
    assert stats["automatisch"] == 4


def test_matching_performance(demo, admin_id):
    """N2: 500 transactions including matching well below 10 seconds."""
    import time

    from schulkasse.camt import parse_xml
    from schulkasse.importer import analysiere, importiere
    from schulkasse.testdaten import SCHUL_IBAN, Buchung, camt_xml
    buchungen = [Buchung("2026-09-%02d" % (1 + i % 28), 100 + i, f"Zahlung {i} ohne Code", f"Name {i}",
                         None, ref=f"PERF{i}") for i in range(500)]
    daten = camt_xml(buchungen, iban=SCHUL_IBAN, von="2026-09-01", bis="2026-09-28", anfangssaldo=0)
    start = time.perf_counter()
    importiere(demo, analysiere(demo, [parse_xml(daten, "perf.xml")]), admin_id)
    assert time.perf_counter() - start < 10
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 500
    assert matching_lauf(demo, admin_id)["geprueft"] == 500
