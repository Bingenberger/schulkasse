"""Money handling. Amounts are always integer cents (never float)."""
import re
from decimal import Decimal, InvalidOperation


class BetragFehler(ValueError):
    pass


def format_cent(cent, waehrung=True):
    """Format cents as '1.234,56 €' (N7)."""
    if cent is None:
        return ""
    cent = int(cent)
    vorzeichen = "-" if cent < 0 else ""
    cent = abs(cent)
    euro, rest = divmod(cent, 100)
    euro_str = f"{euro:,}".replace(",", ".")
    s = f"{vorzeichen}{euro_str},{rest:02d}"
    return s + " €" if waehrung else s


def format_cent_csv(cent):
    """Plain German decimal without thousands separator for spreadsheets."""
    cent = int(cent)
    vorzeichen = "-" if cent < 0 else ""
    euro, rest = divmod(abs(cent), 100)
    return f"{vorzeichen}{euro},{rest:02d}"


def parse_betrag(text):
    """Parse user input like '12,50', '1.234,56', '12.50', '45' into cents."""
    if text is None:
        raise BetragFehler("Bitte einen Betrag angeben.")
    s = str(text).strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    if not s:
        raise BetragFehler("Bitte einen Betrag angeben.")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif "." in s:
        teile = s.split(".")
        if len(teile) > 2 or len(teile[-1]) == 3:
            s = s.replace(".", "")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", s):
        raise BetragFehler(f"„{text}“ ist kein gültiger Betrag.")
    try:
        return int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise BetragFehler(f"„{text}“ ist kein gültiger Betrag.") from exc


def decimal_to_cent(text):
    """Convert an XML decimal string (e.g. '45.00') to cents exactly."""
    d = Decimal(text.strip())
    cent = d * 100
    if cent != cent.to_integral_value():
        raise BetragFehler(f"Betrag {text} hat mehr als zwei Nachkommastellen.")
    return int(cent)
