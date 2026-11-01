"""Acceptance criteria (Lastenheft section 14), one test per criterion."""
import re
from datetime import date

import pytest
from pypdf import PdfReader

from schulkasse import anonymisierung, epc
from schulkasse.auswertung import erinnerungsliste, kontrollrechnung, projekt_kennzahlen, projekt_umsaetze
from schulkasse.buchung import ignorieren, stornieren, vorschlag_annehmen, zuordnen
from schulkasse.camt import CamtFehler
from schulkasse.projekte import massenanlage, posten_status_setzen, projekt_abschliessen, projekt_anlegen
from schulkasse.stammdaten import kind_anlegen, klasse_holen_oder_anlegen
from schulkasse.textnorm import iban_formatiert

from .conftest import benutzer_anlegen, importiere_datei, login, posten_von


def _import_beide(db, admin_id, dateien):
    importiere_datei(db, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    importiere_datei(db, dateien["camt052_teil2_ueberlappend.xml"], "t2.xml", admin_id)


def test_01_import_und_idempotenz(demo, admin_id, dateien):
    erg, _ = importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    assert erg[0]["neu"] == 14  # 13 booked + 1 pending
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 14
    erg, _ = importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    assert erg[0]["neu"] == 0 and erg[0]["duplikate"] == 14
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 14


def test_02_ueberlappender_import(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    erg, _ = importiere_datei(demo, dateien["camt052_teil2_ueberlappend.xml"], "t2.xml", admin_id)
    assert erg[0]["neu"] == 4 and erg[0]["duplikate"] == 5 and erg[0]["ersetzt"] == 1
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 17
    # F1.5: the pending entry was replaced by the booked one
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz WHERE status='PDNG'") == 0
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz WHERE gegen_name='Richter, Tanja'") == 1


def test_03_exakter_code_automatisch(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    anna = posten_von(demo, "Anna", "Becker")
    assert demo.one("SELECT status FROM offener_posten WHERE id=?", (anna["id"],))[0] == "erledigt"
    z = demo.one("SELECT * FROM zuordnung WHERE offener_posten_id=?", (anna["id"],))
    assert z["quelle"] == "auto" and z["konfidenz"] == 100


def test_04_verstuemmelter_code_r1(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    ben = posten_von(demo, "Ben", "Schmidt")
    u = demo.one("SELECT * FROM bankumsatz WHERE gegen_name='Sandra Schmidt'")
    assert u["verwendungszweck"] == ben["zahlcode"].lower().replace("-", " ") + " klassenfahrt ben"
    z = demo.one("SELECT * FROM zuordnung WHERE bankumsatz_id=?", (u["id"],))
    assert z["offener_posten_id"] == ben["id"] and z["quelle"] == "auto" and z["konfidenz"] == 100
    assert demo.one("SELECT status FROM offener_posten WHERE id=?", (ben["id"],))[0] == "erledigt"


def test_05_sammelzahlung_geschwister(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = demo.one("SELECT * FROM bankumsatz WHERE gegen_name='Mehmet Yilmaz'")
    v = demo.one("SELECT * FROM vorschlag WHERE bankumsatz_id=?", (u["id"],))
    assert v["regel"] == "R3"
    assert demo.scalar("SELECT COUNT(*) FROM vorschlag_teil WHERE vorschlag_id=?", (v["id"],)) == 2
    vorschlag_annehmen(demo, v["id"], admin_id)  # one confirmation
    for vorname in ("Emre", "Elif"):
        p = posten_von(demo, vorname, "Yilmaz")
        assert p["status"] == "erledigt"
    assert demo.one("SELECT zuordnungsstatus FROM bankumsatz WHERE id=?", (u["id"],))[0] == "zugeordnet"


def test_06_teilzahlung_und_restzahlung(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    u = demo.one("SELECT * FROM bankumsatz WHERE gegen_name='Wagner, Julia'")
    v = demo.one("SELECT * FROM vorschlag WHERE bankumsatz_id=?", (u["id"],))
    assert v["regel"] == "R2"
    vorschlag_annehmen(demo, v["id"], admin_id)
    assert posten_von(demo, "David", "Wagner")["status"] == "teilweise"
    importiere_datei(demo, dateien["camt052_teil2_ueberlappend.xml"], "t2.xml", admin_id)
    assert posten_von(demo, "David", "Wagner")["status"] == "erledigt"


def test_07_qr_dekodierbar():
    p = epc.epc_payload("GGS Niederkassel Schulkonto", "DE89370400440532013000", 4550, "KF4A-0042 Klassenfahrt")
    zeilen = epc.dekodiere(p).split("\n")
    assert len(zeilen) == 12
    assert zeilen == ["BCD", "002", "1", "SCT", "", "GGS Niederkassel Schulkonto", "DE89370400440532013000",
                      "EUR45.50", "", "", "KF4A-0042 Klassenfahrt", ""]


def test_08_serienbrief_26_seiten(app, demo, admin_id):
    db = demo
    sj = db.scalar("SELECT id FROM schuljahr WHERE aktiv=1")
    with db.tx():
        kl = klasse_holen_oder_anlegen(db, sj, "3c", admin_id)
        for i in range(26):
            kind_anlegen(db, kl, f"Kind{i:02d}", f"Nachname{chr(65 + i)}", admin_id)
    pid = projekt_anlegen(db, {"schuljahr_id": sj, "bezeichnung": "Zoobesuch 3c", "kurzcode": "ZOO3C",
                               "typ": "elternbeitrag", "klasse_id": kl}, admin_id)
    kinder = [r[0] for r in db.all("SELECT id FROM kind WHERE klasse_id=?", (kl,))]
    massenanlage(db, pid, kinder, 1200, admin_id, faellig_am="2026-10-01")
    from schulkasse.briefe import serienbrief
    with app.app_context():
        pdf, n = serienbrief(db, pid, "alle", admin_id)
    assert n == 26
    reader = PdfReader(__import__("io").BytesIO(pdf))
    assert len(reader.pages) == 26
    codes = []
    for page in reader.pages:
        gefunden = set(re.findall(r"ZOO3C-\d{4}", page.extract_text()))
        assert len(gefunden) == 1
        codes.append(gefunden.pop())
    assert len(set(codes)) == 26
    # F7.4: letter date recorded, short code now frozen
    assert db.scalar("SELECT COUNT(*) FROM offener_posten WHERE projekt_id=? AND letzter_brief_am IS NOT NULL",
                     (pid,)) == 26


def test_09_but_kind_nie_in_massenanlage_und_erinnerung(demo, admin_id):
    moritz = demo.one("SELECT * FROM kind WHERE vorname='Moritz' AND nachname='Köhler'")
    assert moritz["but_status"] == 1
    assert demo.scalar("SELECT COUNT(*) FROM offener_posten WHERE kind_id=?", (moritz["id"],)) == 0
    # even with an item that exists (e.g. BuT status set afterwards) the child never appears
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    from schulkasse.projekte import posten_anlegen
    posten_anlegen(demo, kf, richtung="eingang", kategorie="elternbeitrag", kind_id=moritz["id"],
                   bezeichnung="Nachtrag", sollbetrag_cent=4500, faellig_am="2026-09-01", benutzer_id=admin_id)
    gruppen = erinnerungsliste(demo, "2027-01-01")
    namen = {(r["vorname"], r["nachname"]) for rows in gruppen.values() for r in rows}
    assert ("Moritz", "Köhler") not in namen
    assert ("Anna", "Becker") in namen
    # explicitly included BuT child -> item is 'erlassen', still never listed
    demo.execute("DELETE FROM offener_posten WHERE kind_id=?", (moritz["id"],))
    erg = massenanlage(demo, kf, [], 4500, admin_id, but_kind_ids=[moritz["id"]], but_modus="erlassen",
                       faellig_am="2026-09-01")
    assert erg["erlassen"] == 1
    gruppen = erinnerungsliste(demo, "2027-01-01")
    assert ("Moritz", "Köhler") not in {(r["vorname"], r["nachname"]) for rows in gruppen.values() for r in rows}


def _alles_verbuchen(db, admin_id):
    kopie = db.scalar("SELECT id FROM projekt WHERE kurzcode='KOPIE'")
    spende = db.scalar("SELECT id FROM projekt WHERE kurzcode='SPENDE'")
    for u in db.all("SELECT * FROM bankumsatz WHERE status='BOOK' AND zuordnungsstatus='offen' ORDER BY id"):
        v = db.one("SELECT id FROM vorschlag WHERE bankumsatz_id=? ORDER BY konfidenz DESC, id LIMIT 1", (u["id"],))
        if v:
            vorschlag_annehmen(db, v["id"], admin_id)
        elif "Kopiergeld" in (u["verwendungszweck"] or ""):
            zuordnen(db, u["id"], [{"projekt_id": kopie, "betrag_cent": u["betrag_cent"]}], "manuell", admin_id)
        elif "Spende" in (u["verwendungszweck"] or ""):
            zuordnen(db, u["id"], [{"projekt_id": spende, "betrag_cent": u["betrag_cent"]}], "manuell", admin_id)
        else:
            ignorieren(db, u["id"], "Bankgebühr", admin_id)
    # The opening balance belongs to projects as well (Anfangsbestand).
    from schulkasse.anfangsbestand import berechne, verteilen, verteilt
    for konto in db.all("SELECT * FROM konto"):
        rest = berechne(db, konto)["betrag"] - verteilt(db, konto["id"])
        if rest:
            verteilen(db, konto["id"], [{"projekt_id": spende, "betrag_cent": rest}], admin_id)


def test_10_kontrollrechnung_null(demo, admin_id, dateien):
    _import_beide(demo, admin_id, dateien)
    assert kontrollrechnung(demo)["gesamt"]["differenz"] != 0
    _alles_verbuchen(demo, admin_id)
    k = kontrollrechnung(demo)
    assert k["gesamt"]["offen"] == 0
    assert k["gesamt"]["differenz"] == 0
    assert k["gesamt"]["ignoriert"] == -750
    assert k["gesamt"]["nicht_verteilt"] == 0 and k["gesamt"]["verteilt"] == 125000


def test_11_storno_aus_saldo_aber_sichtbar(demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    vorher = projekt_kennzahlen(demo, kf).saldo
    anna = posten_von(demo, "Anna", "Becker")
    z = demo.one("SELECT * FROM zuordnung WHERE offener_posten_id=?", (anna["id"],))
    stornieren(demo, z["id"], "Falsches Kind", admin_id)
    assert projekt_kennzahlen(demo, kf).saldo == vorher - 4500
    assert posten_von(demo, "Anna", "Becker")["status"] == "offen"
    sichtbar = [r for r in projekt_umsaetze(demo, kf) if r["id"] == z["id"]]
    assert sichtbar and sichtbar[0]["storniert"] == 1
    log = demo.one("SELECT * FROM protokoll WHERE aktion='zuordnung.stornieren' AND entitaet_id=?", (z["id"],))
    assert log is not None and "Falsches Kind" in log["neu_wert"]


def test_12_anonymisierung(demo, admin_id, dateien):
    _import_beide(demo, admin_id, dateien)
    _alles_verbuchen(demo, admin_id)
    kf = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    saldo_vorher = projekt_kennzahlen(demo, kf).saldo
    summe_vorher = demo.scalar("SELECT SUM(betrag_cent) FROM bankumsatz")
    for p in demo.all("SELECT id FROM offener_posten WHERE projekt_id=? AND status IN ('offen','teilweise')", (kf,)):
        posten_status_setzen(demo, p["id"], "erlassen", "Fahrt ausgefallen", admin_id)
    projekt_abschliessen(demo, kf, admin_id, rest_ok=True)  # balance stays on the project here
    demo.execute("UPDATE projekt SET abgeschlossen_am='2023-07-31' WHERE id=?", (kf,))
    k4a = demo.scalar("SELECT id FROM klasse WHERE bezeichnung='4a'")
    demo.execute("UPDATE kind SET aktiv=0 WHERE klasse_id=?", (k4a,))  # left school after year 4

    vorschau = anonymisierung.kandidaten(demo, 730, date(2026, 9, 10))
    assert len(vorschau["kinder"]) == 26
    erg = anonymisierung.ausfuehren(demo, 730, admin_id, date(2026, 9, 10))
    assert erg["kinder"] == 26

    anna = demo.one("SELECT * FROM kind WHERE vorname LIKE 'Gelöscht #%' AND id=(SELECT kind_id FROM offener_posten"
                    " WHERE zahlcode='KF4A-0001')")
    assert anna["anonymisiert"] == 1 and anna["nachname"] == f"Gelöscht #{anna['id']}"
    assert demo.scalar("SELECT COUNT(*) FROM kind WHERE nachname='Becker' AND vorname='Anna'") == 0
    u = demo.one("SELECT * FROM bankumsatz WHERE bank_ref='2026090100011'")
    assert u["gegen_name"] == "anonymisiert" and u["gegen_iban"] == "anonymisiert"
    assert "Anna" not in u["verwendungszweck"] and "KF4A-0001" in u["verwendungszweck"]
    # Tom Becker (2b) is still at school: the Becker family keeps its learned IBANs
    assert demo.scalar("SELECT COUNT(*) FROM kind WHERE nachname='Becker'") == 1
    assert demo.scalar("SELECT SUM(betrag_cent) FROM bankumsatz") == summe_vorher
    assert projekt_kennzahlen(demo, kf).saldo == saldo_vorher
    assert kontrollrechnung(demo)["gesamt"]["differenz"] == 0
    assert demo.one("SELECT * FROM protokoll WHERE aktion='anonymisierung.ausfuehren'") is not None


def test_13_lehrkraft_403(app, demo, admin_id, dateien):
    importiere_datei(demo, dateien["camt052_teil1.xml"], "t1.xml", admin_id)
    lk = benutzer_anlegen(demo, "lehrerin", "lehrkraft")
    eigen = demo.scalar("SELECT id FROM projekt WHERE kurzcode='KF4A'")
    fremd = demo.scalar("SELECT id FROM projekt WHERE kurzcode='TH2B'")
    demo.execute("UPDATE projekt SET verantwortlich_user_id=? WHERE id=?", (lk, eigen))
    c = app.test_client()
    assert login(c, "lehrerin").status_code == 302
    assert c.get(f"/projekte/{eigen}").status_code == 200
    assert c.get(f"/projekte/{eigen}/zahlungsstand").status_code == 200
    assert c.get(f"/projekte/{fremd}").status_code == 403
    assert c.get(f"/projekte/{fremd}/zahlungsstand").status_code == 403
    assert c.get(f"/projekte/{fremd}/briefe").status_code == 403
    assert c.get("/umsaetze").status_code == 403
    assert c.get("/zuordnung").status_code == 403
    assert c.get("/import").status_code == 403
    # no IBANs and no BuT children in the teacher's view
    html = c.get(f"/projekte/{eigen}").get_data(as_text=True)
    assert "Thomas Becker" in html  # own project's payments are visible ...
    for iban in [r[0] for r in demo.all("SELECT gegen_iban FROM bankumsatz WHERE gegen_iban IS NOT NULL")]:
        assert iban not in html and iban_formatiert(iban) not in html  # ... but never IBANs
    assert "Köhler" not in html  # nor BuT children


def test_14_fremde_iban_abgelehnt(app, demo, admin_id, dateien):
    with pytest.raises(CamtFehler) as exc:
        importiere_datei(demo, dateien["camt052_fremdkonto.xml"], "fremd.xml", admin_id)
    assert "nicht" in str(exc.value) and "DE" in str(exc.value)
    assert demo.scalar("SELECT COUNT(*) FROM bankumsatz") == 0
    assert demo.scalar("SELECT COUNT(*) FROM import") == 0
    c = app.test_client()
    login(c)
    from io import BytesIO
    r = c.post("/import", data={"datei": (BytesIO(dateien["camt052_fremdkonto.xml"]), "fremd.xml")},
               content_type="multipart/form-data", follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "nicht (aktiv) angelegt" in text and "Traceback" not in text
