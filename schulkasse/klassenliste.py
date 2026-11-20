"""Payment status per child of a class across projects (list for class teachers).

The list is handed to third parties (class teachers). Like every export of the
payment status (F8.2) it never shows a BuT/hardship status or waived items:
such cells look exactly like "nothing to pay" ("–").
"""


def projekte_der_klasse(db, klasse_id, mit_abgeschlossenen=False):
    """Projects with incoming items for children of the class."""
    sql = ("SELECT pr.id, pr.bezeichnung, pr.kurzcode, pr.status, pr.typ,"
           " MIN(p.faellig_am) AS faellig_am, COUNT(p.id) AS anzahl"
           " FROM projekt pr JOIN offener_posten p ON p.projekt_id=pr.id JOIN kind k ON k.id=p.kind_id"
           " WHERE k.klasse_id=? AND p.richtung='eingang' AND p.status<>'storniert'")
    if not mit_abgeschlossenen:
        sql += " AND pr.status='offen'"
    return db.all(sql + " GROUP BY pr.id ORDER BY pr.status DESC, pr.erstellt_am, pr.bezeichnung", (klasse_id,))


def klassen_zahlungsstatus(db, klasse_id, projekt_ids=None, mit_abgeschlossenen=False):
    klasse = db.one("SELECT kl.*, sj.bezeichnung AS schuljahr FROM klasse kl JOIN schuljahr sj"
                    " ON sj.id=kl.schuljahr_id WHERE kl.id=?", (klasse_id,))
    if klasse is None:
        return None
    projekte = projekte_der_klasse(db, klasse_id, mit_abgeschlossenen)
    if projekt_ids is not None:
        gewaehlt = set(projekt_ids)
        projekte = [p for p in projekte if p["id"] in gewaehlt]
    kinder = db.all("SELECT * FROM kind WHERE klasse_id=? AND aktiv=1 AND anonymisiert=0"
                    " ORDER BY nachname, vorname", (klasse_id,))
    zellen = {}
    if projekte and kinder:
        pids = [p["id"] for p in projekte]
        for r in db.all(
                "SELECT p.kind_id, p.projekt_id, p.status, p.sollbetrag_cent, k.but_status,"
                " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z WHERE z.offener_posten_id=p.id"
                "   AND z.storniert=0) AS bezahlt"
                " FROM offener_posten p JOIN kind k ON k.id=p.kind_id"
                f" WHERE k.klasse_id=? AND p.richtung='eingang' AND p.projekt_id IN ({','.join('?' * len(pids))})"
                " AND p.status NOT IN ('storniert','erlassen') AND k.but_status=0", [klasse_id] + pids):
            z = zellen.setdefault((r["kind_id"], r["projekt_id"]), {"soll": 0, "bezahlt": 0, "offen_posten": 0})
            z["soll"] += r["sollbetrag_cent"]
            z["bezahlt"] += r["bezahlt"]
            if r["status"] in ("offen", "teilweise"):
                z["offen_posten"] += 1
    zeilen = []
    spalten = {p["id"]: {"bezahlt": 0, "gesamt": 0, "offen_cent": 0} for p in projekte}
    for k in kinder:
        felder = []
        for p in projekte:
            z = zellen.get((k["id"], p["id"]))
            if z is None:
                felder.append({"status": "keiner"})
                continue
            offen = max(z["soll"] - z["bezahlt"], 0) if z["offen_posten"] else 0
            if offen == 0:
                status = "bezahlt"
            elif z["bezahlt"] > 0:
                status = "teilweise"
            else:
                status = "offen"
            felder.append({"status": status, "offen": offen, "soll": z["soll"], "bezahlt": z["bezahlt"]})
            spalten[p["id"]]["gesamt"] += 1
            spalten[p["id"]]["offen_cent"] += offen
            if status == "bezahlt":
                spalten[p["id"]]["bezahlt"] += 1
        zeilen.append({"kind": k, "felder": felder, "offen": sum(f.get("offen", 0) for f in felder)})
    return {"klasse": klasse, "projekte": projekte, "zeilen": zeilen, "spalten": spalten,
            "offen_kinder": sum(1 for z in zeilen if z["offen"])}
