"""Audit log (section 5.4)."""
import json

from flask import g, has_request_context, request

from .db import utcnow


def _json(wert):
    if wert is None:
        return None
    if hasattr(wert, "keys"):
        wert = {k: wert[k] for k in wert.keys()}
    return json.dumps(wert, ensure_ascii=False, default=str, sort_keys=True)


def protokolliere(db, aktion, entitaet, entitaet_id=None, alt=None, neu=None, benutzer_id=None):
    ip = None
    if has_request_context():
        ip = request.remote_addr
        if benutzer_id is None and getattr(g, "user", None):
            benutzer_id = g.user["id"]
    db.execute(
        "INSERT INTO protokoll (zeitpunkt, benutzer_id, aktion, entitaet, entitaet_id, alt_wert, neu_wert, ip)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (utcnow(), benutzer_id, aktion, entitaet, entitaet_id, _json(alt), _json(neu), ip),
    )
