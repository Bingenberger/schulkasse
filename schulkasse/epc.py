"""EPC QR code (Girocode) according to EPC069-12 (F6)."""
import base64
import io

import segno

from .textnorm import iban_gueltig, iban_kompakt, sepa_zeichen

MAX_BYTES = 331
MAX_CENT = 99_999_999_999
PROJEKT_KURZ = 40


class QRFehler(ValueError):
    pass


def betrag_epc(cent):
    return f"EUR{cent // 100}.{cent % 100:02d}"


def epc_payload(empfaenger, iban, betrag_cent, verwendungszweck, bic=""):
    """Build the twelve-line payload. Raises QRFehler instead of truncating."""
    name = sepa_zeichen(empfaenger)
    if not name:
        raise QRFehler("Der Empfängername fehlt.")
    if len(name) > 70:
        raise QRFehler("Der Empfängername ist länger als 70 Zeichen.")
    iban = iban_kompakt(iban)
    if not iban_gueltig(iban):
        raise QRFehler(f"Die IBAN {iban} ist ungültig.")
    if not isinstance(betrag_cent, int) or not 1 <= betrag_cent <= MAX_CENT:
        raise QRFehler("Der Betrag muss zwischen 0,01 € und 999.999.999,99 € liegen.")
    zweck = sepa_zeichen(verwendungszweck)
    if len(zweck) > 140:
        raise QRFehler("Der Verwendungszweck ist länger als 140 Zeichen.")
    bic = sepa_zeichen(bic or "").replace(" ", "").upper()
    zeilen = ["BCD", "002", "1", "SCT", bic, name, iban, betrag_epc(betrag_cent), "", "", zweck, ""]
    payload = "\n".join(zeilen)
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise QRFehler("Die QR-Daten überschreiten 331 Byte.")
    return payload


def verwendungszweck(zahlcode, projekt_bezeichnung, vorname, nachname):
    """F6.4: payment code first, then shortened project name and child name."""
    projekt = sepa_zeichen(projekt_bezeichnung)
    if len(projekt) > PROJEKT_KURZ:
        projekt = projekt[:PROJEKT_KURZ].rstrip()
    teile = [zahlcode, projekt, sepa_zeichen(vorname), sepa_zeichen(nachname)]
    text = " ".join(t for t in teile if t)
    return text[:140].rstrip()


def qr_code(payload):
    return segno.make_qr(payload, error="m", boost_error=False, encoding="utf-8")


def qr_svg_data_uri(payload):
    """SVG (lossless scaling in PDF) with a 4-module quiet zone."""
    buf = io.BytesIO()
    qr_code(payload).save(buf, kind="svg", border=4, scale=10, xmldecl=False, dark="#000", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def dekodiere(payload):
    """F6.6: render the code and decode it again (test-only dependencies)."""
    import zxingcpp
    from PIL import Image

    buf = io.BytesIO()
    qr_code(payload).save(buf, kind="png", border=4, scale=8)
    buf.seek(0)
    ergebnisse = zxingcpp.read_barcodes(Image.open(buf))
    if not ergebnisse:
        raise QRFehler("QR-Code konnte nicht dekodiert werden.")
    return ergebnisse[0].text
