"""Shared helpers for views."""
import csv
import io
import re
from datetime import date

from flask import Response, current_app, g, redirect, request

from ..geld import BetragFehler, parse_betrag


def int_or_none(wert):
    try:
        return int(wert) if wert not in (None, "") else None
    except (TypeError, ValueError):
        return None


def datum_or_none(wert):
    if not wert:
        return None
    try:
        return date.fromisoformat(wert).isoformat()
    except ValueError:
        return None


def betrag_or_none(wert):
    if wert in (None, ""):
        return None
    try:
        return parse_betrag(wert)
    except BetragFehler:
        return None


def _csv_feld(wert):
    """Neutralise spreadsheet formulas while keeping plain amounts numeric."""
    if isinstance(wert, str):
        text = wert.lstrip()
        if text.startswith(("=", "+", "-", "@")) or wert.startswith(("\t", "\r", "\n")):
            if not re.fullmatch(r"-?[0-9]+(?:[,.][0-9]+)?", wert):
                return "'" + wert
    return wert


def zurueck_oder(standard):
    """Redirect to the page the form came from (field "zurueck"), else to `standard`."""
    ziel = request.form.get("zurueck")
    if ziel and ziel.startswith("/") and not ziel.startswith("//"):
        return redirect(ziel)
    return redirect(standard)


def csv_antwort(dateiname, kopf, zeilen):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([_csv_feld(wert) for wert in kopf])
    w.writerows([_csv_feld(wert) for wert in zeile] for zeile in zeilen)
    # BOM so that spreadsheet programs detect UTF-8.
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{dateiname}"'})


def pdf_antwort(dateiname, pdf, inline=False):
    art = "inline" if inline else "attachment"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'{art}; filename="{dateiname}"'})


def schwelle():
    return current_app.config["MATCHING_SCHWELLE"]


def uid():
    return g.user["id"]


def seite(standard=1):
    return max(1, int_or_none(request.args.get("seite")) or standard)
