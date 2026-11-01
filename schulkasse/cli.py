"""Command line interface (F10.5, F12.2, F12.3)."""
import getpass
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import click
from flask.cli import FlaskGroup, with_appcontext

from .auth import AuthFehler, hash_passwort
from .db import Database, get_db, migrate, utcnow
from .geld import format_cent
from .protokoll import protokolliere


def register_cli(app):
    app.cli.add_command(migrate_cmd)
    app.cli.add_command(pruefe_salden)
    app.cli.add_command(create_admin)
    app.cli.add_command(backup)
    app.cli.add_command(import_camt)
    app.cli.add_command(testdaten_cmd)
    app.cli.add_command(demo_cmd)
    app.cli.add_command(reset_2fa)


@click.command('reset-2fa')
@click.argument('benutzername')
@with_appcontext
def reset_2fa(benutzername):
    """2FA nach Identitätsprüfung zurücksetzen (nur Server-Konsole)."""
    from .zwei_faktor import sitzungen_widerrufen
    db = get_db()
    user = db.one('SELECT * FROM benutzer WHERE lower(benutzername)=lower(?)', (benutzername,))
    if user is None:
        raise click.ClickException('Benutzer nicht gefunden.')
    click.confirm(f'Identität von {benutzername} geprüft? Authenticator, Wiederherstellungscodes und alle '
                  'Sitzungen werden ungültig. Fortfahren?', abort=True)
    with db.tx():
        db.execute('UPDATE benutzer SET totp_secret=NULL, totp_last=-1 WHERE id=?', (user['id'],))
        db.execute('DELETE FROM recovery_code WHERE benutzer_id=?', (user['id'],))
        sitzungen_widerrufen(db, user['id'])
        protokolliere(db, 'benutzer.2fa_reset_cli', 'benutzer', user['id'], neu={'quelle': 'cli'})
    click.echo('2FA zurückgesetzt. Beim nächsten Login mit Passwort muss eine neue Authenticator-App eingerichtet werden.')


@click.command("migrate")
@with_appcontext
def migrate_cmd():
    """Ausstehende Datenbank-Migrationen anwenden."""
    applied = migrate(get_db())
    click.echo("Migrationen angewendet: " + (", ".join(applied) if applied else "keine (aktuell)"))


@click.command("pruefe-salden")
@with_appcontext
def pruefe_salden():
    """Salden der Projekte aufschlüsseln (nur lesend) – zeigt, woraus ein Saldo entsteht."""
    db = get_db()
    versionen = [r[0] for r in db.all("SELECT version FROM schema_migration ORDER BY version")]
    click.echo("Migrationen: " + ", ".join(versionen))
    sicht = db.scalar("SELECT sql FROM sqlite_master WHERE type='view' AND name='zahlung'") or ""
    click.echo("Sicht zahlung enthält Umbuchungen: " + ("ja" if "umbuchung" in sicht else "NEIN – Migration 0006 fehlt!"))
    click.echo("")
    kopf = f"{'Projekt':34} {'Status':13} {'Umsätze':>12} {'Anf.best.':>12} {'Umbuch.':>12} {'Saldo':>12}"
    click.echo(kopf)
    for p in db.all("SELECT id, bezeichnung, kurzcode, status FROM projekt ORDER BY status DESC, bezeichnung"):
        zu = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM zuordnung WHERE projekt_id=? AND storniert=0",
                       (p["id"],), 0)
        ab = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM anfangsbestand WHERE projekt_id=? AND storniert=0",
                       (p["id"],), 0)
        ub = db.scalar("SELECT COALESCE(SUM(CASE WHEN nach_projekt_id=? THEN betrag_cent ELSE -betrag_cent END),0)"
                       " FROM umbuchung WHERE (von_projekt_id=? OR nach_projekt_id=?) AND storniert=0",
                       (p["id"], p["id"], p["id"]), 0)
        sicht_saldo = db.scalar("SELECT COALESCE(SUM(betrag_cent),0) FROM zahlung WHERE projekt_id=?", (p["id"],), 0)
        if not (zu or ab or ub or sicht_saldo):
            continue
        name = f"{p['bezeichnung'][:26]} ({p['kurzcode']})"
        click.echo(f"{name:34} {p['status']:13} {format_cent(zu):>12} {format_cent(ab):>12}"
                   f" {format_cent(ub):>12} {format_cent(sicht_saldo):>12}")
        if zu + ab + ub != sicht_saldo:
            click.echo("   ACHTUNG: Die Summe der Spalten passt nicht zum Saldo der Sicht „zahlung“.")
    ohne = db.all("SELECT u.id, u.betrag_cent, u.grund, pv.kurzcode AS von, pn.kurzcode AS nach FROM umbuchung u"
                  " JOIN projekt pv ON pv.id=u.von_projekt_id JOIN projekt pn ON pn.id=u.nach_projekt_id"
                  " WHERE u.konto_id IS NULL AND u.storniert=0 ORDER BY u.id")
    if ohne:
        click.echo("")
        click.echo(f"{len(ohne)} Umbuchung(en) ohne hinterlegtes Konto (nur für die Kontoauswertungen wichtig):")
        for u in ohne:
            click.echo(f"   #{u['id']}: {u['von']} → {u['nach']} {format_cent(u['betrag_cent'])} – {u['grund']}")


@click.command("create-admin")
@click.option("--benutzername", prompt="Benutzername")
@click.option("--anzeigename", prompt="Anzeigename")
@with_appcontext
def create_admin(benutzername, anzeigename):
    """Administrator-Konto anlegen (Passwort wird abgefragt)."""
    db = get_db()
    if db.one("SELECT id FROM benutzer WHERE lower(benutzername)=lower(?)", (benutzername,)):
        raise click.ClickException(f"Der Benutzer {benutzername} existiert bereits.")
    # Ask again on typos instead of aborting (the installer runs this interactively).
    for _ in range(5):
        pw = getpass.getpass("Passwort (mind. 12 Zeichen): ")
        if pw != getpass.getpass("Passwort wiederholen: "):
            click.echo("Die Passwörter stimmen nicht überein. Bitte erneut eingeben.", err=True)
            continue
        try:
            h = hash_passwort(pw)
            break
        except AuthFehler as exc:
            click.echo(f"{exc} Bitte erneut eingeben.", err=True)
    else:
        raise click.ClickException("Kein gültiges Passwort eingegeben – Abbruch nach 5 Versuchen.")
    with db.tx():
        uid = db.insert("INSERT INTO benutzer (benutzername, anzeigename, passwort_hash, rolle, aktiv, erstellt_am)"
                        " VALUES (?,?,?,'admin',1,?)", (benutzername, anzeigename, h, utcnow()))
        protokolliere(db, "benutzer.anlegen", "benutzer", uid,
                      neu={"benutzername": benutzername, "rolle": "admin", "quelle": "cli"})
    click.echo(f"Administrator {benutzername} wurde angelegt.")


def erstelle_backup(app, ziel_dir=None):
    ziel = Path(ziel_dir or app.config["BACKUP_DIR"])
    ziel.mkdir(parents=True, exist_ok=True)
    stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
    archiv = ziel / f"schulkasse-backup-{stempel}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "schulkasse.sqlite3"
        db = Database(app.config["DATABASE"])
        try:
            db.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            db.close()
        with tarfile.open(archiv, "w:gz") as tar:
            tar.add(snapshot, arcname="schulkasse.sqlite3")
            belege = Path(app.config["UPLOAD_DIR"])
            if belege.is_dir():
                tar.add(belege, arcname="belege")
    archiv.chmod(0o600)
    return archiv


@click.command("backup")
@click.option("--ziel", type=click.Path(file_okay=False), help="Zielverzeichnis (Standard: SCHULKASSE_BACKUP_DIR)")
@with_appcontext
def backup(ziel):
    """Konsistente Sicherung (VACUUM INTO) samt Belegen als datiertes Archiv."""
    from flask import current_app
    archiv = erstelle_backup(current_app, ziel)
    click.echo(f"Sicherung geschrieben: {archiv}")


@click.command("import-camt")
@click.argument("datei", type=click.Path(exists=True, dir_okay=False))
@click.option("--benutzer", "benutzername", help="Benutzer, dem der Import zugeschrieben wird (Standard: erster Admin)")
@click.option("--ja", is_flag=True, help="Ohne Rückfrage importieren")
@with_appcontext
def import_camt(datei, benutzername, ja):
    """CAMT.052-Datei (.xml oder .zip) importieren."""
    from flask import current_app

    from .camt import CamtFehler, lies_upload
    from .importer import analysiere, importiere

    db = get_db()
    if benutzername:
        user = db.one("SELECT * FROM benutzer WHERE benutzername=?", (benutzername,))
    else:
        user = db.one("SELECT * FROM benutzer WHERE rolle='admin' AND aktiv=1 ORDER BY id LIMIT 1")
    if user is None:
        raise click.ClickException("Kein passender Benutzer gefunden. Bitte zuerst create-admin ausführen.")
    pfad = Path(datei)
    try:
        analysen = analysiere(db, lies_upload(pfad.read_bytes(), pfad.name))
    except CamtFehler as exc:
        raise click.ClickException(str(exc)) from exc
    for a in analysen:
        von, bis = a.zeitraum
        s = a.schlusssaldo
        click.echo(f"{a.datei.dateiname} – Konto {a.konto['bezeichnung']} ({a.iban})")
        click.echo(f"  Zeitraum: {von} bis {bis}")
        click.echo(f"  Neu: {len(a.neu)} (davon vorgemerkt: {a.anzahl_vorgemerkt}), Duplikate: {len(a.duplikate)}")
        click.echo(f"  Eingänge: {format_cent(a.summe_eingaenge)}, Ausgänge: {format_cent(a.summe_ausgaenge)}")
        click.echo(f"  Schlusssaldo: {format_cent(s.betrag_cent) if s else '–'}")
    if not ja and not click.confirm("Import ausführen?"):
        click.echo("Abgebrochen.")
        sys.exit(1)
    ergebnisse, stats = importiere(db, analysen, user["id"], current_app.config["MATCHING_SCHWELLE"])
    for e in ergebnisse:
        click.echo(f"{e['dateiname']}: {e['neu']} neu, {e['duplikate']} Duplikate")
    click.echo(f"Matching: {stats['automatisch']} automatisch zugeordnet, {stats['vorschlaege']} mit Vorschlag, "
               f"{stats['ohne_treffer']} ohne Treffer")


@click.command("testdaten")
@click.option("--ziel", default="testdaten", type=click.Path(file_okay=False), show_default=True)
@with_appcontext
def testdaten_cmd(ziel):
    """Synthetische CAMT.052-Testdateien und Klassenliste schreiben.

    Die Zahlcodes in den Dateien passen zu den Daten aus `demo`.
    """
    from .testdaten import schreibe_testdateien
    for pfad in schreibe_testdateien(Path(ziel)):
        click.echo(f"geschrieben: {pfad}")


@click.command("demo")
@click.option("--mit-import", is_flag=True, help="Testdateien gleich importieren")
@with_appcontext
def demo_cmd(mit_import):
    """Demo-Stammdaten in eine leere Datenbank laden (nur Testsysteme)."""
    from flask import current_app

    from .testdaten import demo_anlegen, importiere_testdateien
    db = get_db()
    if db.scalar("SELECT COUNT(*) FROM kind", default=0):
        raise click.ClickException("Die Datenbank enthält bereits Kinder. Demo-Daten nur in eine leere Datenbank laden.")
    admin = db.one("SELECT id FROM benutzer WHERE rolle='admin' ORDER BY id LIMIT 1")
    if admin is None:
        raise click.ClickException("Bitte zuerst create-admin ausführen.")
    info = demo_anlegen(db, admin["id"])
    click.echo(f"Demo-Daten angelegt: {info}")
    if mit_import:
        stats = importiere_testdateien(db, admin["id"], current_app.config["MATCHING_SCHWELLE"])
        click.echo(f"Testdateien importiert: {stats}")


def main():
    from . import create_app
    cli = FlaskGroup(create_app=lambda: create_app(), help="Schulkasse – Verwaltungsbefehle")
    cli.main(prog_name="schulkasse")
