"""Regression checks for account isolation, password changes and CSV exports."""
import csv
import io

import pytest

from schulkasse.camt import Bericht, CamtDatei, Saldo, Umsatz
from schulkasse.importer import analysiere, importiere
from schulkasse.views.util import csv_antwort
from .conftest import PASSWORT, benutzer_anlegen, login


@pytest.mark.parametrize('mit_anfang', [True, False])
def test_mehrere_konten_salden_und_zeitraeume(db, admin_id, mit_anfang):
    berichte = []
    for iban, anfang, ende, tag in [('DE111', 8000, 10000, '2026-09-01'),
                                    ('DE222', 85000, 90000, '2026-09-02')]:
        db.execute('INSERT INTO konto (bezeichnung, iban, inhaber, aktiv) VALUES (?,?,?,1)',
                   (iban, iban, 'Test'))
        salden = [Saldo('CLBD', ende, tag)]
        if mit_anfang:
            salden.append(Saldo('OPBD', anfang, tag))
        berichte.append(Bericht(iban, [Umsatz(tag, ende-anfang, 'BOOK', dedup_hash=iban)],
                                salden, tag, tag))
    datei = CamtDatei('mehrere.xml', 'test', berichte)
    analysen = analysiere(db, [datei])
    assert [a.schlusssaldo.betrag_cent for a in analysen] == [10000, 90000]
    assert [a.zeitraum for a in analysen] == [('2026-09-01', '2026-09-01'),
                                             ('2026-09-02', '2026-09-02')]
    assert len(datei.berichte) == 2
    importiere(db, analysen, admin_id)
    assert [r[0] for r in db.all('SELECT eroeffnungssaldo_cent FROM konto ORDER BY iban')] == [8000, 85000]
    assert [r[0] for r in db.all('SELECT saldo_schluss_cent FROM import ORDER BY konto_id')] == [10000, 90000]


def test_passwortwechsel_beendet_andere_sitzungen(app, db, admin_id):
    benutzer_anlegen(db, 'andere', 'kasse')
    aktuell, zweite, fremd = [app.test_client() for _ in range(3)]
    login(aktuell)
    login(zweite)
    login(fremd, name='andere')
    cookie_alt = aktuell.get_cookie('session').value
    neu = 'mein-neues-langes-passwort'
    assert aktuell.post('/passwort', data={'alt': PASSWORT, 'neu': neu, 'neu2': neu}).status_code == 302
    assert aktuell.get_cookie('session').value != cookie_alt
    assert aktuell.get('/').status_code == 200
    assert zweite.get('/').status_code == 302
    assert fremd.get('/').status_code == 200
    assert login(app.test_client()).status_code == 401
    assert login(app.test_client(), passwort=neu).status_code == 302


@pytest.mark.parametrize('wert', ['=1+1', '+SUM(A1:A2)', '-1+2', '@SUM(A1:A2)',
                                 '  =1+1', '\t=1+1', '\r=1+1', '\n=1+1',
                                 '=HYPERLINK("https://example.invalid";"Text")'])
def test_csv_formeln_als_text(wert):
    response = csv_antwort('test.csv', [wert], [[wert]])
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip('\ufeff')), delimiter=';'))
    assert rows == [["'" + wert], ["'" + wert]]


def test_csv_betraege_und_normale_texte_unveraendert():
    werte = ['-1234,56', '1234,56', '-7.50', 'Müller; Meier', 'Text "Zitat"', '', None, 42]
    response = csv_antwort('test.csv', ['Werte'], [werte])
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip('\ufeff')), delimiter=';'))
    assert rows[1] == ['-1234,56', '1234,56', '-7.50', 'Müller; Meier', 'Text "Zitat"', '', '', '42']
