"""Automatic matching of transactions to open items (F4)."""
import re
from dataclasses import dataclass, field
from itertools import combinations

from .buchung import AKTIV_SQL, zuordnen
from .db import utcnow
from .geld import format_cent
from .textnorm import normalisiere

CODE_RE = re.compile(r"[A-Z0-9]{3,8}[0-9]{4}")
MAX_R5_POSTEN = 14
MAX_ALTERNATIVEN = 5


@dataclass
class Teil:
    projekt_id: int
    posten_id: int | None
    betrag_cent: int


@dataclass
class Treffer:
    regel: str
    konfidenz: int
    teile: list
    hinweis: str = ""


@dataclass
class Posten:
    id: int
    projekt_id: int
    richtung: str
    kind_id: int | None
    familie_id: int | None
    nachname_norm: str
    zahlcode: str | None
    soll: int
    status: str
    bezeichnung: str
    bezahlt: int = 0

    @property
    def rest(self):
        return self.soll - self.bezahlt

    @property
    def zahlbar(self):
        return self.status in ("offen", "teilweise")


@dataclass
class Kontext:
    """In-memory snapshot of all items in open projects."""
    posten: dict = field(default_factory=dict)
    codes: dict = field(default_factory=dict)
    familien_iban: dict = field(default_factory=dict)

    @classmethod
    def lade(cls, db):
        ctx = cls()
        rows = db.all(
            "SELECT p.id, p.projekt_id, p.richtung, p.kind_id, k.familie_id, k.nachname, p.zahlcode,"
            " p.sollbetrag_cent, p.status, p.bezeichnung,"
            " (SELECT COALESCE(SUM(ABS(z.betrag_cent)),0) FROM zahlung z"
            "   WHERE z.offener_posten_id=p.id AND z.storniert=0) AS bezahlt"
            " FROM offener_posten p JOIN projekt pr ON pr.id=p.projekt_id"
            " LEFT JOIN kind k ON k.id=p.kind_id"
            " WHERE pr.status='offen' AND p.status<>'storniert'")
        for r in rows:
            p = Posten(id=r["id"], projekt_id=r["projekt_id"], richtung=r["richtung"], kind_id=r["kind_id"],
                       familie_id=r["familie_id"], nachname_norm=normalisiere(r["nachname"] or ""),
                       zahlcode=r["zahlcode"], soll=r["sollbetrag_cent"], status=r["status"],
                       bezeichnung=r["bezeichnung"], bezahlt=r["bezahlt"])
            ctx.posten[p.id] = p
            if p.zahlcode:
                ctx.codes[normalisiere(p.zahlcode)] = p
        for r in db.all("SELECT iban, familie_id FROM zahler_iban"):
            ctx.familien_iban.setdefault(r["iban"], set()).add(r["familie_id"])
        return ctx

    def verbucht(self, posten_id, betrag):
        p = self.posten.get(posten_id)
        if p:
            p.bezahlt += abs(betrag)
            if p.zahlbar:
                p.status = "erledigt" if p.bezahlt >= p.soll else "teilweise"


def finde_codes(text_norm, codes):
    """F4.4: find known payment codes in the normalised text.

    Every window that matches the code pattern is checked against the known
    codes, so leading words glued to the code (after removing blanks) do not
    hide it. Overlapping shorter hits inside a longer hit are dropped.
    """
    treffer = []
    n = len(text_norm)
    for i in range(n):
        for laenge in range(12, 6, -1):
            if i + laenge > n:
                continue
            kandidat = text_norm[i:i + laenge]
            if kandidat in codes and CODE_RE.fullmatch(kandidat):
                treffer.append((i, i + laenge, codes[kandidat]))
    ergebnis = []
    for a, b, p in treffer:
        if any(a2 <= a and b <= b2 and (b2 - a2) > (b - a) for a2, b2, _ in treffer):
            continue
        if p not in ergebnis:
            ergebnis.append(p)
    return ergebnis


def _hinweis_abweichung(p, betrag):
    if not p.zahlbar:
        if p.status == "erledigt":
            return f"Posten ist bereits bezahlt – mögliche Doppelzahlung ({format_cent(betrag)})."
        if p.status == "erlassen":
            return "Posten ist erlassen – Zahlung trotzdem zuordnen?"
    if betrag > p.rest:
        return f"Überzahlung um {format_cent(betrag - p.rest)} (erwartet {format_cent(p.rest)})."
    return f"Unterzahlung: {format_cent(betrag)} von {format_cent(p.rest)} – Posten bleibt teilweise offen."


def bewerte(umsatz, ctx):
    """Apply rule cascade R1..R6 to one transaction. Returns list of Treffer
    (alternatives), best first. Empty list means R7."""
    betrag = abs(umsatz["betrag_cent"])
    richtung = "eingang" if umsatz["betrag_cent"] > 0 else "ausgang"
    text = normalisiere(" ".join(x for x in (umsatz["verwendungszweck_roh"], umsatz["verwendungszweck"]) if x))
    name = normalisiere(umsatz["gegen_name"] or "")

    gefunden = [p for p in finde_codes(text, ctx.codes) if p.richtung == richtung]

    if len(gefunden) == 1:
        p = gefunden[0]
        if p.zahlbar and betrag == p.rest:
            return [Treffer("R1", 100, [Teil(p.projekt_id, p.id, betrag)], f"Zahlcode {p.zahlcode}, Betrag passt.")]
        return [Treffer("R2", 80, [Teil(p.projekt_id, p.id, betrag)],
                        f"Zahlcode {p.zahlcode}. " + _hinweis_abweichung(p, betrag))]

    if len(gefunden) > 1:
        if sum(p.soll for p in gefunden) == betrag:
            teile = [Teil(p.projekt_id, p.id, p.soll) for p in gefunden]
        elif all(p.rest > 0 for p in gefunden) and sum(p.rest for p in gefunden) == betrag:
            teile = [Teil(p.projekt_id, p.id, p.rest) for p in gefunden]
        else:
            return []
        codes = ", ".join(p.zahlcode for p in gefunden)
        return [Treffer("R3", 90, teile, f"Sammelzahlung für {codes}.")]

    offene = [p for p in ctx.posten.values() if p.richtung == richtung and p.zahlbar and p.rest > 0]

    if richtung == "eingang":
        familien = ctx.familien_iban.get(umsatz["gegen_iban"] or "", set())
        if familien:
            fam_posten = [p for p in offene if p.familie_id in familien]
            passend = [p for p in fam_posten if p.rest == betrag]
            if len(passend) == 1:
                p = passend[0]
                return [Treffer("R4", 70, [Teil(p.projekt_id, p.id, betrag)],
                                "Absender-IBAN ist der Familie bekannt, Betrag passt zu einem offenen Posten.")]
            if 2 <= len(fam_posten) <= MAX_R5_POSTEN:
                loesungen = []
                for k in range(2, len(fam_posten) + 1):
                    for combo in combinations(fam_posten, k):
                        if sum(p.rest for p in combo) == betrag:
                            loesungen.append(combo)
                            if len(loesungen) > 1:
                                break
                    if len(loesungen) > 1:
                        break
                if len(loesungen) == 1:
                    teile = [Teil(p.projekt_id, p.id, p.rest) for p in loesungen[0]]
                    return [Treffer("R5", 60, teile,
                                    "Absender-IBAN ist der Familie bekannt, Betrag entspricht mehreren Posten.")]

        kandidaten = [p for p in offene if p.kind_id and p.rest == betrag and len(p.nachname_norm) >= 3
                      and (p.nachname_norm in name or p.nachname_norm in text)]
        if kandidaten:
            return [Treffer("R6", 50, [Teil(p.projekt_id, p.id, betrag)],
                            "Nachname des Kindes kommt im Absender oder Verwendungszweck vor, Betrag passt.")
                    for p in kandidaten[:MAX_ALTERNATIVEN]]

    # Items without a child (refunds, invoices): exact amount match only.
    ohne_kind = [p for p in offene if not p.kind_id and p.rest == betrag]
    if 1 <= len(ohne_kind) <= MAX_ALTERNATIVEN:
        return [Treffer("R6", 50, [Teil(p.projekt_id, p.id, betrag)],
                        f"Betrag entspricht dem offenen Posten „{p.bezeichnung}“.") for p in ohne_kind]
    return []


def _speichere_vorschlaege(db, umsatz_id, treffer):
    jetzt = utcnow()
    for t in treffer:
        vid = db.insert("INSERT INTO vorschlag (bankumsatz_id, regel, konfidenz, hinweis, erstellt_am)"
                        " VALUES (?,?,?,?,?)", (umsatz_id, t.regel, t.konfidenz, t.hinweis, jetzt))
        db.executemany("INSERT INTO vorschlag_teil (vorschlag_id, projekt_id, offener_posten_id, betrag_cent)"
                       " VALUES (?,?,?,?)",
                       [(vid, x.projekt_id, x.posten_id, x.betrag_cent) for x in t.teile])


def matching_lauf(db, benutzer_id, umsatz_ids=None, schwelle=100, auto=True):
    """Run the cascade over open booked transactions. Returns statistics."""
    stats = {"geprueft": 0, "automatisch": 0, "vorschlaege": 0, "ohne_treffer": 0}
    with db.tx():
        sql = f"SELECT * FROM bankumsatz u WHERE status='BOOK' AND zuordnungsstatus='offen' AND {AKTIV_SQL}"
        params = []
        if umsatz_ids is not None:
            if not umsatz_ids:
                return stats
            sql += f" AND id IN ({','.join('?' * len(umsatz_ids))})"
            params = list(umsatz_ids)
        umsaetze = db.all(sql + " ORDER BY buchungsdatum, id", params)
        ctx = Kontext.lade(db)
        for u in umsaetze:
            stats["geprueft"] += 1
            db.execute("DELETE FROM vorschlag WHERE bankumsatz_id=?", (u["id"],))
            treffer = bewerte(u, ctx)
            if not treffer:
                stats["ohne_treffer"] += 1
                continue
            beste = treffer[0]
            eindeutig = len([t for t in treffer if t.konfidenz == beste.konfidenz]) == 1
            if auto and eindeutig and beste.konfidenz >= schwelle:
                zuordnen(db, u["id"],
                         [{"projekt_id": t.projekt_id, "offener_posten_id": t.posten_id,
                           "betrag_cent": t.betrag_cent} for t in beste.teile],
                         "auto", benutzer_id, konfidenz=beste.konfidenz,
                         notiz=f"Automatisch ({beste.regel}): {beste.hinweis}")
                for t in beste.teile:
                    if t.posten_id:
                        ctx.verbucht(t.posten_id, t.betrag_cent)
                stats["automatisch"] += 1
            else:
                _speichere_vorschlaege(db, u["id"], treffer)
                stats["vorschlaege"] += 1
    return stats


def vorschlaege_auffrischen(db, benutzer_id, posten_ids):
    """Re-evaluate suggestions of other open transactions touching these items."""
    posten_ids = [p for p in posten_ids if p]
    if not posten_ids:
        return
    ph = ",".join("?" * len(posten_ids))
    ids = [r[0] for r in db.all(
        "SELECT DISTINCT v.bankumsatz_id FROM vorschlag v JOIN vorschlag_teil t ON t.vorschlag_id=v.id"
        f" WHERE t.offener_posten_id IN ({ph})", posten_ids)]
    if ids:
        matching_lauf(db, benutzer_id, umsatz_ids=ids, auto=False)
