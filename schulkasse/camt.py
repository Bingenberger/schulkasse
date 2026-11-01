"""CAMT.052 parser (F1). Independent of Flask and the database.

Elements are addressed by local name only, so every camt.052 version
(001.02 to 001.08) as well as camt.053 statements are accepted.
"""
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .geld import BetragFehler, decimal_to_cent
from .textnorm import iban_kompakt, normalisiere


class CamtFehler(ValueError):
    """Domain error with a message suitable for end users (N6)."""


@dataclass
class Umsatz:
    buchungsdatum: str
    betrag_cent: int
    status: str
    valutadatum: str | None = None
    waehrung: str = "EUR"
    gegen_name: str | None = None
    gegen_iban: str | None = None
    verwendungszweck: str | None = None
    verwendungszweck_roh: str | None = None
    end_to_end_id: str | None = None
    bank_ref: str | None = None
    geschaeftsvorfall: str | None = None
    dedup_hash: str = ""


@dataclass
class Saldo:
    typ: str
    betrag_cent: int
    datum: str | None


@dataclass
class Bericht:
    """One Rpt/Stmt block of a file."""
    iban: str
    umsaetze: list = field(default_factory=list)
    salden: list = field(default_factory=list)
    zeitraum_von: str | None = None
    zeitraum_bis: str | None = None


@dataclass
class CamtDatei:
    dateiname: str
    datei_hash: str
    berichte: list = field(default_factory=list)

    @property
    def ibans(self):
        return sorted({b.iban for b in self.berichte})

    @property
    def umsaetze(self):
        return [u for b in self.berichte for u in b.umsaetze]

    @property
    def zeitraum(self):
        von = [d for b in self.berichte for d in [b.zeitraum_von] if d]
        bis = [d for b in self.berichte for d in [b.zeitraum_bis] if d]
        daten = [u.buchungsdatum for u in self.umsaetze]
        von = min(von or daten or [None], key=lambda x: x or "")
        bis = max(bis or daten or [None], key=lambda x: x or "")
        return von, bis

    def _saldo(self, typen, spaetester=True):
        kandidaten = []
        for b in self.berichte:
            for s in b.salden:
                if s.typ in typen:
                    kandidaten.append(s)
        if not kandidaten:
            return None
        kandidaten.sort(key=lambda s: (s.datum or "", typen.index(s.typ) * (-1 if spaetester else 1)))
        return kandidaten[-1] if spaetester else kandidaten[0]

    @property
    def schlusssaldo(self):
        """F1.8: CLBD, otherwise ITBD; latest date wins."""
        for typ in ("CLBD", "ITBD"):
            s = self._saldo([typ], spaetester=True)
            if s:
                return s
        return None

    @property
    def anfangssaldo(self):
        for typ in ("OPBD", "PRCD", "ITBD"):
            s = self._saldo([typ], spaetester=False)
            if s:
                return s
        return None


def _ln(el):
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kinder(el, name):
    return [c for c in el if _ln(c) == name]


def _find(el, pfad):
    """Follow a slash separated path of local names; first match wins."""
    if el is None:
        return None
    aktuell = [el]
    for teil in pfad.split("/"):
        naechste = []
        for e in aktuell:
            naechste.extend(_kinder(e, teil))
        if not naechste:
            return None
        aktuell = naechste
    return aktuell[0]


def _findall(el, pfad):
    if el is None:
        return []
    aktuell = [el]
    for teil in pfad.split("/"):
        naechste = []
        for e in aktuell:
            naechste.extend(_kinder(e, teil))
        aktuell = naechste
    return aktuell


def _text(el, pfad=None):
    if pfad:
        el = _find(el, pfad)
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _datum(el, pfad):
    """Date from <X><Dt> or <X><DtTm>; returns ISO date string."""
    basis = _find(el, pfad)
    if basis is None:
        return None
    wert = _text(basis, "Dt") or _text(basis, "DtTm")
    if not wert:
        return None
    return wert[:10]


_SEPA_TAGS = ("EREF", "KREF", "MREF", "CRED", "DEBT", "SVWZ", "ABWA", "ABWE", "COAM", "OAMT", "IBAN", "BIC")
_SEPA_RE = re.compile(r"(" + "|".join(_SEPA_TAGS) + r")\+")


def zerlege_sepa_praefixe(text):
    """F1.4: split 'EREF+...SVWZ+...' into a dict. Returns {} if no structure found."""
    if not text:
        return {}
    treffer = list(_SEPA_RE.finditer(text))
    # Structured if SVWZ+ is present or the text starts with a known tag.
    if not treffer or ("SVWZ+" not in text and treffer[0].start() != 0):
        return {}
    teile = {}
    for i, m in enumerate(treffer):
        ende = treffer[i + 1].start() if i + 1 < len(treffer) else len(text)
        teile.setdefault(m.group(1), text[m.end():ende].strip())
    return teile


def _leer_e2e(wert):
    if not wert or wert.strip().upper() == "NOTPROVIDED":
        return None
    return wert.strip()


def _ntry_umsatz(ntry):
    amt_el = _find(ntry, "Amt")
    if amt_el is None or not (amt_el.text or "").strip():
        raise CamtFehler("Ein Umsatz in der Datei enthält keinen Betrag.")
    try:
        betrag = decimal_to_cent(amt_el.text)
    except (BetragFehler, ArithmeticError) as exc:
        raise CamtFehler(f"Ungültiger Betrag „{amt_el.text}“ in der Datei.") from exc
    ind = _text(ntry, "CdtDbtInd")
    if ind not in ("CRDT", "DBIT"):
        raise CamtFehler("Ein Umsatz hat keine gültige Soll/Haben-Kennung (CdtDbtInd).")
    if ind == "DBIT":
        betrag = -betrag
    # Reversal entries (RvslInd=true) flip the booking direction as indicated
    # by CdtDbtInd already; nothing else to do.

    sts_el = _find(ntry, "Sts")
    status = None
    if sts_el is not None:
        status = _text(sts_el, "Cd") or _text(sts_el, "Prtry") or _text(sts_el)
    status = (status or "BOOK").upper()
    if status not in ("BOOK", "PDNG", "INFO"):
        status = "BOOK"

    buchungsdatum = _datum(ntry, "BookgDt") or _datum(ntry, "ValDt")
    if not buchungsdatum:
        raise CamtFehler("Ein Umsatz in der Datei hat kein Buchungsdatum.")

    tx = _find(ntry, "NtryDtls/TxDtls")
    eingang = betrag > 0
    partei = "Dbtr" if eingang else "Cdtr"
    gegen_name = None
    gegen_iban = None
    e2e = None
    if tx is not None:
        rp = _find(tx, "RltdPties")
        if rp is not None:
            gegen_name = _text(rp, f"{partei}/Nm") or _text(rp, f"{partei}/Pty/Nm")
            gegen_iban = _text(rp, f"{partei}Acct/Id/IBAN")
        e2e = _leer_e2e(_text(tx, "Refs/EndToEndId"))

    ustrd = [u.text or "" for u in _findall(ntry, "NtryDtls/TxDtls/RmtInf/Ustrd")]
    roh = "".join(ustrd)
    if not roh.strip():
        roh = _text(ntry, "AddtlNtryInf") or ""
    roh = re.sub(r"\s{2,}", " ", roh).strip()
    zweck = roh
    sepa = zerlege_sepa_praefixe(roh)
    if sepa:
        zweck = sepa.get("SVWZ", "").strip() or roh
        if not e2e:
            e2e = _leer_e2e(sepa.get("EREF"))
        if not gegen_name and sepa.get("ABWA"):
            gegen_name = sepa["ABWA"]

    btc = _find(ntry, "BkTxCd")
    gv = None
    if btc is not None:
        gv = _text(btc, "Prtry/Cd")
        if not gv:
            domn = _text(btc, "Domn/Cd")
            fmly = _text(btc, "Domn/Fmly/Cd")
            sub = _text(btc, "Domn/Fmly/SubFmlyCd")
            gv = "-".join(x for x in (domn, fmly, sub) if x) or None

    return Umsatz(
        buchungsdatum=buchungsdatum,
        valutadatum=_datum(ntry, "ValDt"),
        betrag_cent=betrag,
        waehrung=(amt_el.get("Ccy") or "EUR").upper(),
        status=status,
        gegen_name=gegen_name,
        gegen_iban=iban_kompakt(gegen_iban) if gegen_iban else None,
        verwendungszweck=zweck or None,
        verwendungszweck_roh=roh or None,
        end_to_end_id=e2e,
        bank_ref=_text(ntry, "AcctSvcrRef"),
        geschaeftsvorfall=gv,
    )


def dedup_schluessel(u, laufnummer):
    """F1.6 priority: AcctSvcrRef, EndToEndId, then content hash."""
    if u.bank_ref:
        return "ref:" + u.bank_ref
    if u.end_to_end_id:
        return "e2e:" + u.end_to_end_id
    basis = "|".join([
        u.buchungsdatum,
        str(u.betrag_cent),
        u.gegen_iban or "",
        normalisiere(u.verwendungszweck_roh or u.verwendungszweck or ""),
        str(laufnummer),
    ])
    return "sha:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _vergib_dedup(umsaetze):
    zaehler = {}
    for u in umsaetze:
        if u.bank_ref or u.end_to_end_id:
            u.dedup_hash = dedup_schluessel(u, 0)
            continue
        key = (u.buchungsdatum, u.betrag_cent, u.gegen_iban or "",
               normalisiere(u.verwendungszweck_roh or u.verwendungszweck or ""))
        zaehler[key] = zaehler.get(key, 0) + 1
        u.dedup_hash = dedup_schluessel(u, zaehler[key])


def parse_xml(daten, dateiname="datei.xml"):
    try:
        root = ET.fromstring(daten)
    except ET.ParseError as exc:
        raise CamtFehler(f"Die Datei „{dateiname}“ ist keine gültige XML-Datei.") from exc

    datei = CamtDatei(dateiname=dateiname, datei_hash=hashlib.sha256(daten).hexdigest())
    bloecke = [el for el in root.iter() if _ln(el) in ("Rpt", "Stmt", "Ntfctn")]
    if not bloecke:
        raise CamtFehler(f"Die Datei „{dateiname}“ ist keine CAMT-Kontoumsatzdatei (kein Rpt-Element gefunden).")

    for block in bloecke:
        iban = _text(block, "Acct/Id/IBAN")
        if not iban:
            raise CamtFehler(f"In der Datei „{dateiname}“ fehlt die IBAN des Kontos.")
        bericht = Bericht(iban=iban_kompakt(iban))
        frto = _find(block, "FrToDt")
        if frto is not None:
            von = _text(frto, "FrDtTm") or _text(frto, "FrDt")
            bis = _text(frto, "ToDtTm") or _text(frto, "ToDt")
            bericht.zeitraum_von = von[:10] if von else None
            bericht.zeitraum_bis = bis[:10] if bis else None
        for bal in _kinder(block, "Bal"):
            typ = _text(bal, "Tp/CdOrPrtry/Cd") or _text(bal, "BalTp/CdOrPrtry/Cd") or _text(bal, "Tp/CdOrPrtry/Prtry")
            amt = _text(bal, "Amt")
            if not typ or not amt:
                continue
            try:
                cent = decimal_to_cent(amt)
            except (BetragFehler, ArithmeticError):
                continue
            if _text(bal, "CdtDbtInd") == "DBIT":
                cent = -cent
            bericht.salden.append(Saldo(typ=typ.upper(), betrag_cent=cent, datum=_datum(bal, "Dt")))
        for ntry in _kinder(block, "Ntry"):
            u = _ntry_umsatz(ntry)
            if u.status == "INFO":
                continue
            bericht.umsaetze.append(u)
        datei.berichte.append(bericht)
    # Running numbers are counted per account across the whole file.
    for iban in datei.ibans:
        _vergib_dedup([u for b in datei.berichte if b.iban == iban for u in b.umsaetze])
    return datei


def lies_upload(daten, dateiname):
    """F1.1: accept .xml or .zip (unpacked in memory). Returns list of CamtDatei."""
    name = dateiname.lower()
    if name.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(daten))
        except zipfile.BadZipFile as exc:
            raise CamtFehler(f"Die Datei „{dateiname}“ ist kein gültiges ZIP-Archiv.") from exc
        dateien = []
        with zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                if info.file_size > 50 * 1024 * 1024:
                    raise CamtFehler(f"Die Datei „{info.filename}“ im ZIP ist zu groß.")
                dateien.append(parse_xml(zf.read(info), info.filename.rsplit("/", 1)[-1]))
        if not dateien:
            raise CamtFehler(f"Das ZIP-Archiv „{dateiname}“ enthält keine XML-Dateien.")
        return dateien
    if name.endswith(".xml"):
        return [parse_xml(daten, dateiname)]
    raise CamtFehler("Bitte eine CAMT-Datei im Format .xml oder .zip hochladen.")
