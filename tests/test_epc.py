"""EPC QR code (F6)."""
import pytest

from schulkasse import epc
from schulkasse.textnorm import sepa_zeichen

IBAN = "DE89370400440532013000"


def test_zwoelf_zeilen():
    p = epc.epc_payload("GGS Niederkassel", IBAN, 1250, "KF4A-0042 Klassenfahrt", "COBADEFFXXX")
    zeilen = p.split("\n")
    assert len(zeilen) == 12
    assert zeilen[:4] == ["BCD", "002", "1", "SCT"]
    assert zeilen[4] == "COBADEFFXXX"
    assert zeilen[6] == IBAN
    assert zeilen[7] == "EUR12.50"
    assert zeilen[8] == zeilen[9] == zeilen[11] == ""
    assert zeilen[10] == "KF4A-0042 Klassenfahrt"


@pytest.mark.parametrize("cent,text", [(1, "EUR0.01"), (4500, "EUR45.00"), (123456789, "EUR1234567.89"),
                                       (99_999_999_999, "EUR999999999.99")])
def test_betragsformat(cent, text):
    assert epc.betrag_epc(cent) == text


@pytest.mark.parametrize("cent", [0, -1, 100_000_000_000])
def test_betragsgrenzen(cent):
    with pytest.raises(epc.QRFehler):
        epc.epc_payload("Schule", IBAN, cent, "X")


def test_fehler_statt_abschneiden():
    with pytest.raises(epc.QRFehler):
        epc.epc_payload("Schule", IBAN, 100, "x" * 141)
    with pytest.raises(epc.QRFehler):
        epc.epc_payload("N" * 71, IBAN, 100, "x")
    with pytest.raises(epc.QRFehler):
        epc.epc_payload("Schule", "DE00370400440532013000", 100, "x")


def test_sepa_zeichensatz():
    assert sepa_zeichen("Grundschule Größenwahn & Co. <ä>") == "Grundschule Groessenwahn Co. ae"
    assert sepa_zeichen("Straße/Nr.: 5 (hinten) 'A' +?") == "Strasse/Nr.: 5 (hinten) 'A' +?"


def test_verwendungszweck_code_zuerst():
    z = epc.verwendungszweck("KF4A-0042", "Klassenfahrt 4a Nettersheim mit sehr sehr langem Namen und mehr", "Jürgen",
                             "Müller-Lüdenscheidt")
    assert z.startswith("KF4A-0042 ")
    assert "Juergen Mueller-Luedenscheidt" in z
    assert len(z) <= 140
    lang = epc.verwendungszweck("KF4A-0042", "P" * 40, "V" * 60, "N" * 60)
    assert len(lang) == 140 and lang.startswith("KF4A-0042")


def test_selbsttest_dekodieren():
    """F6.6 / acceptance 7: decode the generated code and compare."""
    p = epc.epc_payload("GGS Niederkassel Schulkonto", IBAN, 4500,
                        epc.verwendungszweck("KF4A-0001", "Klassenfahrt 4a", "Anna", "Becker"), "GENODED1NKS")
    assert epc.dekodiere(p) == p
    zeilen = epc.dekodiere(p).split("\n")
    assert len(zeilen) == 12 and zeilen[7] == "EUR45.00"


def test_fehlerkorrektur_m_und_svg():
    q = epc.qr_code("BCD\n002\n1\nSCT\n\nX\n" + IBAN + "\nEUR1.00\n\n\nX\n")
    assert q.error == "M"
    uri = epc.qr_svg_data_uri("test")
    assert uri.startswith("data:image/svg+xml;base64,")
