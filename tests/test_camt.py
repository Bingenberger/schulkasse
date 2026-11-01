"""CAMT.052 parser (F1.2-F1.8)."""
import io
import zipfile

import pytest

from schulkasse.camt import CamtFehler, lies_upload, parse_xml, zerlege_sepa_praefixe

IBAN = "DE89370400440532013000"


def xml(version, ntries, bal=None):
    bal = bal if bal is not None else (
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">100.00</Amt>'
        '<CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-07</Dt></Dt></Bal>')
    return (f'<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.{version}">'
            f"<BkToCstmrAcctRpt><Rpt><Acct><Id><IBAN>{IBAN}</IBAN></Id></Acct>{bal}{ntries}</Rpt>"
            "</BkToCstmrAcctRpt></Document>").encode()


def ntry(amt="12.50", ind="CRDT", sts="<Sts><Cd>BOOK</Cd></Sts>", ref="<AcctSvcrRef>R1</AcctSvcrRef>",
         ustrd=("KF4A-0001 Klassen", "fahrt"), party="<Dbtr><Pty><Nm>Max Muster</Nm></Pty></Dbtr>"
         "<DbtrAcct><Id><IBAN>DE02120300000000202051</IBAN></Id></DbtrAcct>", e2e="NOTPROVIDED",
         datum="<BookgDt><Dt>2026-09-01</Dt></BookgDt>", extra=""):
    u = "".join(f"<Ustrd>{x}</Ustrd>" for x in ustrd)
    return (f'<Ntry><Amt Ccy="EUR">{amt}</Amt><CdtDbtInd>{ind}</CdtDbtInd>{sts}{datum}'
            f"<ValDt><Dt>2026-09-02</Dt></ValDt>{ref}"
            "<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn></BkTxCd>"
            f"<NtryDtls><TxDtls><Refs><EndToEndId>{e2e}</EndToEndId></Refs><RltdPties>{party}</RltdPties>"
            f"<RmtInf>{u}</RmtInf></TxDtls></NtryDtls>{extra}</Ntry>")


@pytest.mark.parametrize("version", ["02", "04", "08"])
def test_namespace_agnostisch(version):
    d = parse_xml(xml(version, ntry()))
    assert d.ibans == [IBAN]
    assert len(d.umsaetze) == 1


def test_felder_eingang():
    u = parse_xml(xml("08", ntry())).umsaetze[0]
    assert u.betrag_cent == 1250
    assert u.status == "BOOK"
    assert u.buchungsdatum == "2026-09-01"
    assert u.valutadatum == "2026-09-02"
    assert u.gegen_name == "Max Muster"
    assert u.gegen_iban == "DE02120300000000202051"
    assert u.bank_ref == "R1"
    assert u.end_to_end_id is None  # NOTPROVIDED counts as empty
    assert u.verwendungszweck == "KF4A-0001 Klassenfahrt"  # blocks joined without separator
    assert u.geschaeftsvorfall == "PMNT-RCDT-ESCT"


def test_ausgang_negativ_und_cdtr():
    party = "<Cdtr><Nm>Verlag GmbH</Nm></Cdtr><CdtrAcct><Id><IBAN>DE02120300000000202051</IBAN></Id></CdtrAcct>"
    u = parse_xml(xml("02", ntry(amt="312.40", ind="DBIT", party=party))).umsaetze[0]
    assert u.betrag_cent == -31240
    assert u.gegen_name == "Verlag GmbH"


def test_status_textknoten_und_code():
    assert parse_xml(xml("02", ntry(sts="<Sts>PDNG</Sts>"))).umsaetze[0].status == "PDNG"
    assert parse_xml(xml("08", ntry(sts="<Sts><Cd>PDNG</Cd></Sts>"))).umsaetze[0].status == "PDNG"


def test_buchungsdatum_als_datetime():
    u = parse_xml(xml("08", ntry(datum="<BookgDt><DtTm>2026-09-03T10:11:12</DtTm></BookgDt>"))).umsaetze[0]
    assert u.buchungsdatum == "2026-09-03"


def test_mehrfach_leerzeichen_zusammengefasst():
    u = parse_xml(xml("08", ntry(ustrd=("KF4A-0001   Klassen", "fahrt  4a")))).umsaetze[0]
    assert u.verwendungszweck == "KF4A-0001 Klassenfahrt 4a"


def test_sepa_praefixe():
    u = parse_xml(xml("08", ntry(ustrd=("EREF+ABC123 SVWZ+KF4A-0002 Klas", "senfahrt KREF+X")))).umsaetze[0]
    assert u.verwendungszweck == "KF4A-0002 Klassenfahrt"
    assert u.end_to_end_id == "ABC123"
    assert u.verwendungszweck_roh.startswith("EREF+ABC123")
    assert zerlege_sepa_praefixe("normaler Text") == {}


def test_saldo_clbd_und_itbd():
    d = parse_xml(xml("08", ntry()))
    assert d.schlusssaldo.betrag_cent == 10000 and d.schlusssaldo.typ == "CLBD"
    itbd = ('<Bal><Tp><CdOrPrtry><Cd>ITBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">5.00</Amt>'
            '<CdtDbtInd>DBIT</CdtDbtInd><Dt><Dt>2026-09-07</Dt></Dt></Bal>')
    d = parse_xml(xml("08", ntry(), bal=itbd))
    assert d.schlusssaldo.typ == "ITBD" and d.schlusssaldo.betrag_cent == -500


def test_dedup_prioritaet():
    a = parse_xml(xml("08", ntry(ref="<AcctSvcrRef>REF9</AcctSvcrRef>", e2e="E2E"))).umsaetze[0]
    assert a.dedup_hash == "ref:REF9"
    b = parse_xml(xml("08", ntry(ref="", e2e="E2E-1"))).umsaetze[0]
    assert b.dedup_hash == "e2e:E2E-1"
    c = parse_xml(xml("08", ntry(ref="") + ntry(ref=""))).umsaetze
    assert c[0].dedup_hash.startswith("sha:")
    assert c[0].dedup_hash != c[1].dedup_hash  # running number for identical entries on the same day


def test_dedup_hash_stabil_ueber_dateien():
    einzel = parse_xml(xml("08", ntry(ref=""))).umsaetze[0]
    wieder = parse_xml(xml("02", ntry(ref="", sts="<Sts>BOOK</Sts>"))).umsaetze[0]
    assert einzel.dedup_hash == wieder.dedup_hash


def test_zip_im_speicher():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.xml", xml("08", ntry()))
        zf.writestr("liesmich.txt", "ignorieren")
        zf.writestr("unter/b.xml", xml("02", ntry(ref="<AcctSvcrRef>R2</AcctSvcrRef>")))
    dateien = lies_upload(buf.getvalue(), "export.zip")
    assert [d.dateiname for d in dateien] == ["a.xml", "b.xml"]


@pytest.mark.parametrize("daten,name", [(b"<kaputt", "x.xml"), (b"PK-nope", "x.zip"), (b"hallo", "x.pdf"),
                                        (b"<Document/>", "x.xml")])
def test_fehler_fachlich(daten, name):
    with pytest.raises(CamtFehler) as exc:
        lies_upload(daten, name)
    assert "Traceback" not in str(exc.value)


def test_info_eintraege_werden_ignoriert():
    d = parse_xml(xml("08", ntry(sts="<Sts><Cd>INFO</Cd></Sts>")))
    assert d.umsaetze == []
