"""Excel export of the monitoring list (Richtlinie Schulgirokonten, Anlage 1)."""
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .textnorm import iban_formatiert

EURO = '#,##0.00 "€";[Red]-#,##0.00 "€"'
DUENN = Side(style="thin", color="000000")
RAHMEN = Border(left=DUENN, right=DUENN, top=DUENN, bottom=DUENN)
KOPF_FUELLUNG = PatternFill("solid", fgColor="F0F0F0")
SALDO_FUELLUNG = PatternFill("solid", fgColor="FAFAFA")
SCHWARZ = PatternFill("solid", fgColor="111111")
SPALTEN = [("Lfd.-Nr.", 7), ("Datum", 11), ("Klasse", 8), ("Empfänger/\nEinzahler", 24),
           ("Zahlungsgrund lt. beigefügter Belege/Rechnungen", 46), ("Einnahme\nin €", 12), ("Ausgabe\nin €", 12),
           ("Buchung\nin €", 12), ("Auszugs-\nNr.:", 10), ("Konto-\nstand in €*¹", 13), ("ggf. Anmerkungen", 34)]


def _setze(ws, zeile, spalte, wert, **stil):
    """Write a cell; text is always stored as text, never as a formula."""
    zelle = ws.cell(row=zeile, column=spalte, value=wert)
    if isinstance(wert, str) and wert.startswith("="):
        zelle.data_type = "s"
    for name, v in stil.items():
        setattr(zelle, name, v)
    return zelle


def _blattname(ul, vergeben):
    name = f"Auszug {ul['auszug']['nummer']}-{ul['auszug']['jahr']}"
    basis, i = name, 2
    while name in vergeben:
        name = f"{basis} ({i})"
        i += 1
    vergeben.add(name)
    return name


def _blatt(ws, ul, cfg, schulanschrift):
    ws.sheet_view.showGridLines = False
    for i, (_titel, breite) in enumerate(SPALTEN, start=1):
        ws.column_dimensions[get_column_letter(i)].width = breite
    _setze(ws, 1, 1, "ANLAGE 1: Überwachungsliste Einnahmen und Ausgaben", font=Font(size=9, underline="single"))
    _setze(ws, 2, 1, "Überwachungsliste (ÜL) für Einnahmen und Ausgaben", font=Font(size=14, bold=True),
           alignment=Alignment(horizontal="center"))
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(SPALTEN))
    kopf = [("Name und Anschrift der Schule:", cfg.get("schulname", "") + (f", {schulanschrift}" if schulanschrift else "")),
            ("Geldinstitut:", ul["konto"]["geldinstitut"] or ""),
            ("IBAN:", iban_formatiert(ul["konto"]["iban"])),
            ("Bereich / Bezeichnung des Kontos (§ 4):", ul["konto"]["bezeichnung"])]
    for i, (label, wert) in enumerate(kopf, start=4):
        _setze(ws, i, 1, label, font=Font(bold=True), alignment=Alignment(horizontal="right"))
        ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=4)
        _setze(ws, i, 5, wert, border=Border(bottom=DUENN))
        ws.merge_cells(start_row=i, start_column=5, end_row=i, end_column=8)
    _setze(ws, 9, 1, "In dieser Überwachungsliste sind sämtliche Kontobewegungen des oben genannten Schulgirokontos für "
                     "den jeweiligen Zahlungsbereich zu erfassen. Die Verwendung der Mittel ist entsprechend "
                     "nachzuweisen bzw. zu erläutern.", font=Font(size=9))
    a = ul["auszug"]
    _setze(ws, 11, 1, f"Kontoauszug Nr. {ul['bezeichnung']}", font=Font(bold=True), border=RAHMEN)
    ws.merge_cells(start_row=11, start_column=1, end_row=11, end_column=3)
    _setze(ws, 11, 4, f"Zeitraum {date.fromisoformat(a['datum_von']):%d.%m.%Y} – "
                      f"{date.fromisoformat(a['datum_bis']):%d.%m.%Y}")

    z = 13
    _setze(ws, z, 8, "Schulgirokonto:", font=Font(bold=True), alignment=Alignment(horizontal="center"), border=RAHMEN)
    ws.merge_cells(start_row=z, start_column=8, end_row=z, end_column=10)
    z += 1
    for i, (titel, _b) in enumerate(SPALTEN, start=1):
        _setze(ws, z, i, titel, font=Font(bold=True, size=9), fill=KOPF_FUELLUNG, border=RAHMEN,
               alignment=Alignment(horizontal="center", vertical="center", wrap_text=True))
    ws.row_dimensions[z].height = 32
    kopfzeile = z
    z += 1
    # opening balance
    for i in range(1, len(SPALTEN) + 1):
        _setze(ws, z, i, None, border=RAHMEN, fill=SALDO_FUELLUNG)
    _setze(ws, z, 5, "Anfangsbestand", font=Font(bold=True), alignment=Alignment(horizontal="right"))
    ws.cell(row=z, column=8).fill = SCHWARZ
    _setze(ws, z, 9, ul["bezeichnung"], alignment=Alignment(horizontal="center"))
    _setze(ws, z, 10, ul["anfangssaldo"] / 100, number_format=EURO, font=Font(bold=True))
    _setze(ws, z, 11, "Startsaldo", font=Font(bold=True))
    z += 1
    for zeile in ul["zeilen"]:
        grund = zeile["grund"]
        if zeile["zweck"] and zeile["zweck"] != grund:
            grund += f"\n{zeile['zweck'][:160]}"
        werte = [zeile["nr"], date.fromisoformat(zeile["datum"]), zeile["klasse"], zeile["name"], grund,
                 zeile["einnahme"] / 100 if zeile["einnahme"] else None,
                 zeile["ausgabe"] / 100 if zeile["ausgabe"] else None,
                 zeile["buchung"] / 100, ul["bezeichnung"], zeile["stand"] / 100, zeile["anmerkung"]]
        for i, wert in enumerate(werte, start=1):
            c = _setze(ws, z, i, wert, border=RAHMEN, alignment=Alignment(vertical="top", wrap_text=i in (4, 5, 11)))
            if i in (6, 7, 8, 10):
                c.number_format = EURO
            elif i == 2:
                c.number_format = "DD.MM.YYYY"
            elif i in (1, 9):
                c.alignment = Alignment(horizontal="center", vertical="top")
        z += 1
    if not ul["zeilen"]:
        _setze(ws, z, 1, "Keine Kontobewegungen in diesem Zeitraum.", border=RAHMEN)
        ws.merge_cells(start_row=z, start_column=1, end_row=z, end_column=len(SPALTEN))
        z += 1
    for i in range(1, len(SPALTEN) + 1):
        _setze(ws, z, i, None, border=Border(left=DUENN, right=DUENN, top=Side(style="medium"), bottom=DUENN),
               font=Font(bold=True))
    _setze(ws, z, 5, "Endbestand:", alignment=Alignment(horizontal="right"))
    _setze(ws, z, 6, ul["eingaenge"] / 100, number_format=EURO)
    _setze(ws, z, 7, ul["ausgaenge"] / 100, number_format=EURO)
    ws.cell(row=z, column=8).fill = SCHWARZ
    _setze(ws, z, 9, len(ul["zeilen"]), alignment=Alignment(horizontal="center"))
    _setze(ws, z, 10, ul["endsaldo"] / 100, number_format=EURO)
    _setze(ws, z, 11, "Endsaldo")
    z += 1
    _setze(ws, z, 1, "*¹ darf nicht negativ werden", font=Font(size=8))
    z += 2
    endsaldo = a["endsaldo_cent"]
    _setze(ws, z, 1, "Endsaldo laut Kontoauszug:")
    ws.merge_cells(start_row=z, start_column=1, end_row=z, end_column=4)
    _setze(ws, z, 5, endsaldo / 100 if endsaldo is not None else "nicht erfasst",
           number_format=EURO, alignment=Alignment(horizontal="left"))
    if endsaldo is not None:
        ok = endsaldo == ul["endsaldo"]
        _setze(ws, z, 6, "✓ stimmt überein" if ok else "✗ Abweichung",
               font=Font(bold=True, color="1E6B3A" if ok else "B00000"))
    _setze(ws, z, 9, "Bestätigung (vollständig u. richtig):", font=Font(bold=True))
    z += 3
    for i in range(9, 12):
        ws.cell(row=z, column=i).border = Border(bottom=DUENN)
    _setze(ws, z + 1, 9, "Datum, Unterschrift der bevollmächtigten Person", font=Font(size=8))
    if a["geprueft_am"]:
        _setze(ws, z + 2, 9, f"In der Schulkasse als geprüft vermerkt von {a['geprueft_von_name']} "
                             f"am {a['geprueft_am'][:10]}.", font=Font(size=8))

    ws.freeze_panes = ws.cell(row=kopfzeile + 1, column=1)
    ws.print_title_rows = f"{kopfzeile}:{kopfzeile}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.oddFooter.left.text = "Überwachungsliste nach § 8 Richtlinie Schulgirokonten"
    ws.oddFooter.right.text = "Seite &P von &N"


def ueberwachungslisten_xlsx(listen, cfg, schulanschrift):
    """One worksheet per statement; returns the file as bytes."""
    wb = Workbook()
    wb.remove(wb.active)
    vergeben = set()
    for ul in listen:
        _blatt(wb.create_sheet(_blattname(ul, vergeben)), ul, cfg, schulanschrift)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
