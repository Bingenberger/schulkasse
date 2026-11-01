"""Text normalisation helpers (F4.1, F6.3) and IBAN utilities."""
import re
import unicodedata

_UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"}


def transliteriere(text):
    return "".join(_UMLAUTE.get(ch, ch) for ch in text)


def normalisiere(text):
    """F4.1: upper case, umlauts resolved, only A-Z0-9 kept."""
    if not text:
        return ""
    s = transliteriere(text).upper()
    # Strip remaining accents (é -> E) before discarding non-ASCII characters.
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Z0-9]", "", s)


_SEPA_ERLAUBT = re.compile(r"[^a-zA-Z0-9/\-?:().,'+ ]")


def sepa_zeichen(text):
    """F6.3: reduce to the SEPA character set, transliterating umlauts."""
    s = transliteriere(text or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _SEPA_ERLAUBT.sub("", s)
    return re.sub(r" {2,}", " ", s).strip()


def iban_kompakt(iban):
    return re.sub(r"\s+", "", iban or "").upper()


def iban_gueltig(iban):
    s = iban_kompakt(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    umgestellt = s[4:] + s[:4]
    ziffern = "".join(str(int(ch, 36)) for ch in umgestellt)
    return int(ziffern) % 97 == 1


def iban_mit_pruefziffer(land, bban):
    """Build a valid IBAN from country code and BBAN (used for test data)."""
    ziffern = "".join(str(int(ch, 36)) for ch in (bban + land + "00"))
    pz = 98 - int(ziffern) % 97
    return f"{land}{pz:02d}{bban}"


def iban_formatiert(iban):
    s = iban_kompakt(iban)
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))
