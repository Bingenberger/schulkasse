# Schulkasse

Beitrags- und Kassenbuchverwaltung für die GGS Niederkassel – umgesetzt nach `Lastenheft_Schulkasse.md` (Version 1.0).

Die Anwendung

- erzeugt für jeden Elternbeitrag einen individuellen Überweisungs-QR-Code (Girocode) im Elternbrief,
- liest Kontoumsätze aus CAMT.052-Dateien der Hausbank (XML oder ZIP),
- ordnet Zahlungen automatisch offenen Posten zu (Regelkaskade R1–R7) und zeigt, wer noch nicht gezahlt hat,
- führt ein projektbezogenes Kassenbuch mit Kontrollrechnung („Differenz 0,00 € – jeder Umsatz ist verbucht“).

Es fließt kein Geld durch das System, es gibt keinen Bankzugriff, kein Elternportal und keinen E-Mail-Versand.

## Technik

Python 3.12, Flask/Jinja2, SQLite (WAL, Fremdschlüssel), `segno` für QR-Codes, WeasyPrint für PDF, `argon2-cffi`
für Passwörter, gunicorn hinter nginx. Kein Build-Schritt, kein npm, keine externen CDN-Aufrufe.

```
schulkasse/
  camt.py          CAMT.052-Parser (ohne Flask testbar)
  importer.py      Vorschau, Dedup, Ersetzen vorgemerkter Umsätze, Import
  matching.py      Regelkaskade R1–R6
  buchung.py       Zuordnen, Aufteilen, Storno, Ignorieren, Lernen der Zahler-IBAN
  projekte.py      Projekte, offene Posten, Massenanlage, Zahlcodes
  epc.py           EPC-QR-Code (EPC069-12)
  briefe.py        Serienbrief und PDF-Erzeugung
  auswertung.py    Kennzahlen, Kontrollrechnung, Kassenbuch, Erinnerungsliste
  anonymisierung.py  Anonymisierungslauf (F11.2)
  stammdaten.py    Schuljahre, Klassen, Kinder, CSV-Import, Schuljahreswechsel
  testdaten.py     Demo-Daten und Generator für synthetische CAMT-Dateien (F12.4)
  migrations/      nummerierte SQL-Migrationen (werden beim Start angewendet)
deploy/            systemd-Units, nginx-Beispiel
docs/              Verzeichnis von Verarbeitungstätigkeiten (Vorlage)
testdaten/         erzeugte CAMT-Testdateien und Klassenliste
tests/             pytest, inkl. aller 14 Abnahmekriterien
```

## Installation von diesem Rechner aus (ssh + rsync)

`deploy/remote.sh` installiert die Schulkasse vom eigenen Rechner aus auf einem Server: Es führt die Tests
lokal aus, überträgt die Projektkopie per **rsync** nach `~/schulkasse-quelle` auf dem Server und startet dort per
**ssh** `deploy/install.sh` mit sudo. Die App läuft danach als systemd-Dienst auf `127.0.0.1:5015`; das auf dem
Server vorhandene **nginx** nimmt HTTPS-Anfragen für die Domain an und leitet sie an die App weiter. Das
Zertifikat wird bei **Let's Encrypt** angefordert und von certbot automatisch erneuert.

Voraussetzungen:

- Server mit Ubuntu 24.04 oder Debian 13 (Python ab 3.12), systemd, laufendem oder installierbarem nginx
- ssh-Zugang mit einem Benutzer, der `sudo` darf (oder root), `rsync` auf beiden Rechnern
- DNS-Eintrag der Domain zeigt auf den Server; Port 80 und 443 sind aus dem Internet erreichbar
  (Port 80 wird für die Zertifikatsprüfung und -erneuerung gebraucht)
- Port 5015 ist auf dem Server frei (sonst `--port` angeben)

```bash
# Probelauf: Verbindung, Übertragung (ohne Schreiben) und Plan
deploy/remote.sh install --host admin@server.schule.de --domain kasse.schule.de --email it@schule.de --dry-run

# Installation
deploy/remote.sh install --host admin@server.schule.de --domain kasse.schule.de --email it@schule.de
```

Weitere Optionen:

| Option | Bedeutung |
|---|---|
| `--port 5015` | lokaler Port der App (Standard 5015) |
| `--allow 192.168.178.0/24` | nur diese Netze/IPs dürfen die App aufrufen, mehrfach möglich. Ohne `--allow` ist die App aus dem Internet erreichbar (geschützt durch Passwort und 2FA). |
| `--le-staging` | Test-Zertifikat von Let's Encrypt, um einen Probelauf ohne Rate-Limits zu machen |
| `--ssh-port 2222`, `--ssh-option "-i ~/.ssh/schule"` | ssh-Einstellungen |
| `--ohne-tests` | lokale Tests überspringen |

Während der Installation fragt das Terminal nach dem sudo-Passwort (falls nötig) und nach Benutzername und
Passwort des ersten Administrators. Das Skript

1. prüft Server, freien Port, DNS und dass nginx noch keine Site für die Domain hat,
2. installiert Systempakete (inkl. certbot), legt eine temporäre nginx-Site für die Zertifikatsprüfung an,
   testet sie lokal und fordert das Zertifikat an (bei Fehlern wird die temporäre Site wieder entfernt),
3. richtet Dienstbenutzer, Python-Umgebung, `.env` mit zufälligem Schlüssel, Datenbank und Administrator ein,
4. startet den Dienst und den täglichen Sicherungs-Timer,
5. schreibt die endgültige nginx-Site `/etc/nginx/sites-available/schulkasse` (HTTP→HTTPS, TLS, Proxy auf 5015)
   und prüft die ganze Kette.

Andere nginx-Sites bleiben unverändert. Bestehende Installationen werden nicht überschrieben – dafür gibt es das
Update.

**Abgebrochene Erstinstallation:** Bricht die Installation unterwegs ab (z. B. Netzwerkfehler), entfernt

```bash
deploy/remote.sh aufraeumen --host admin@server.schule.de
```

die halbfertige Installation (Anwendungsverzeichnis, Dienstbenutzer, systemd-Units, nginx-Site). Danach die
Installation einfach neu starten. Ein bereits ausgestelltes Zertifikat bleibt erhalten und wird wiederverwendet.
Enthält die Datenbank schon Benutzer, verweigert der Befehl das Aufräumen – eine fertige Installation wird damit
nie gelöscht. Beim Anlegen des Administrators wird bei zu kurzem Passwort oder Tippfehler erneut gefragt. Direkt auf dem Server lässt sich `deploy/install.sh` genauso aufrufen (`--help` zeigt die Optionen);
mit `--cert/--key` wird statt Let's Encrypt ein vorhandenes Zertifikat verwendet.

Nach der Installation die fachliche **Erstkonfiguration ab Schritt 4** unten durchführen und
unter *Verwaltung → Briefvorlage* Briefkopf, Logo und Texte eintragen. Die `.env` mit dem zufälligen Schlüssel separat sichern.

## Update einer bestehenden Installation

Von diesem Rechner aus (Tests, Übertragung und Update in einem Schritt):

```bash
deploy/remote.sh update --host admin@server.schule.de            # fragt vor dem Austausch nach
deploy/remote.sh update --host admin@server.schule.de --ja       # ohne Rückfrage
```

Oder direkt auf dem Server mit einer Projektkopie **außerhalb** von `/opt/schulkasse`
(z. B. `~/schulkasse-quelle`):

```bash
sudo bash /root/schulkasse-neu/deploy/update.sh --dry-run   # Plan: Versionen, geänderte Units, neue .env-Einträge
sudo bash /root/schulkasse-neu/deploy/update.sh             # fragt vor dem Austausch nach; --ja ohne Rückfrage
```

Ablauf:

1. **Vorbereitung bei laufender App:** Quellen kopieren, alle Python-Pakete und das Projekt selbst
   vorab bauen. Scheitert das (Netz, fehlerhafte Paketdefinition), bleibt alles unverändert.
2. **Austausch, die App ist kurz nicht erreichbar:** Dienst und Sicherungs-Timer stoppen,
   Sicherung mit `schulkasse backup`, Kopie der Datenbank, Code und venv austauschen
   (Pakete offline aus Schritt 1), Migrationen, systemd-Units aktualisieren, Dienst starten.
3. **Funktionsprüfung:** Der Dienst muss innerhalb von 30 Sekunden auf `/anmelden` antworten.

Schlägt ein Schritt in Phase 2 oder 3 fehl, rollt das Skript **automatisch** zurück: alter Code,
alte venv, alte Units und die Datenbank vom Zeitpunkt direkt vor dem Update. Da der Dienst in dieser
Zeit gestoppt ist, gehen dabei keine Buchungen verloren.

Nicht verändert werden `.env`, `instance/` (Datenbank bis auf Migrationen, Belege, `brief.toml`,
Sicherungen) und die nginx-Konfiguration. Neue Einstellungen aus `.env.example`, die in der `.env`
fehlen, zeigt das Skript an; bis zur Übernahme gelten die Standardwerte. Die installierte Version
steht in `/opt/schulkasse/VERSION`.

Die Vorversion bleibt bis zum nächsten Update in `/opt/schulkasse/.vorversion`: Code, venv, Units und
die Datenbankkopie aus `db/`. Ein **manueller Rückschritt Tage später** stellt auch diese
Datenbankkopie wieder her. Alles, was seit dem Update gebucht wurde, wäre dann verloren. Daher nur in
Absprache und nach einer aktuellen Sicherung (`schulkasse backup`).

## Manuelle Installation (Ubuntu-Server)

```bash
sudo apt install python3.12-venv libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core
sudo useradd --system --home /opt/schulkasse --shell /usr/sbin/nologin schulkasse
sudo mkdir -p /opt/schulkasse && sudo cp -r . /opt/schulkasse && cd /opt/schulkasse
sudo python3.12 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt && sudo .venv/bin/pip install --no-deps -e .
sudo mkdir -p instance && sudo chown -R schulkasse:schulkasse instance
```

WeasyPrint braucht die Systembibliothek Pango; die DejaVu-Schriften sorgen für ein einheitliches Schriftbild im PDF.

## Erstkonfiguration

1. Konfiguration anlegen:
   ```bash
   sudo cp .env.example .env && sudo chmod 600 .env && sudo chown schulkasse .env
   python3 -c "import secrets; print(secrets.token_hex(32))"   # als SCHULKASSE_SECRET_KEY eintragen
   sudo cp brief.example.toml instance/brief.toml              # Briefkopf und Texte anpassen
   ```
   Alle Stellen `[PLATZHALTER: …]` in der `.env` durch reale Werte ersetzen. Briefkopf, Logo und Brieftexte
   werden später in der App unter *Verwaltung → Briefvorlage* gepflegt; `instance/brief.toml` liefert nur
   die Grundeinstellungen, falls dort nichts eingetragen ist.
2. Datenbank anlegen und ersten Administrator erstellen (nur per CLI, F10.5):
   ```bash
   sudo -u schulkasse .venv/bin/schulkasse migrate
   sudo -u schulkasse .venv/bin/schulkasse create-admin
   ```
3. Dienst und Webserver einrichten:
   ```bash
   sudo cp deploy/schulkasse.service deploy/schulkasse-backup.* /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now schulkasse schulkasse-backup.timer
   sudo cp deploy/nginx-schulkasse.conf /etc/nginx/sites-available/schulkasse   # Servername, Zertifikat, IP-Bereiche anpassen
   sudo ln -s /etc/nginx/sites-available/schulkasse /etc/nginx/sites-enabled/ && sudo nginx -t && sudo systemctl reload nginx
   ```
4. In der Weboberfläche als Administrator:
   - **Verwaltung → Konten**: Schulkonto (IBAN, BIC, Kontoinhaber = Empfängername im QR-Code) anlegen, ggf. Förderverein.
   - **Verwaltung → Benutzer**: Konten für Kasse und Lehrkräfte anlegen (Passwort mind. 12 Zeichen).
   - **Stammdaten → Schuljahre**: Schuljahr anlegen und aktivieren, dann **Klassenliste importieren** (CSV).

## Arbeitsablauf

### Verpflichtende Zwei-Faktor-Anmeldung

Alle Rollen benötigen Passwort **und** einen zweiten Faktor. Beim ersten Login richtet jeder
Benutzer seine Authenticator-App per QR-Code oder manuellem Schlüssel ein und bestätigt einen
sechsstelligen Code. Erst danach und nach Bestätigung der gesicherten Wiederherstellungscodes
wird die App freigeschaltet. Bestehende Benutzer richten 2FA beim nächsten Login ein;
alte Sitzungen ohne 2FA werden beim Update ungültig.

- Unterstützt werden zeitbasierte Authenticator-Codes (TOTP, 30 Sekunden). Technische Grundlage:
  [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238) und [PyOTP](https://pyauth.github.io/pyotp/).
- Zehn Wiederherstellungscodes werden nach der Einrichtung angezeigt. Jeden Code nur einmal,
  zusammen mit dem Passwort, verwenden. Sicher aufbewahren, beispielsweise im Passwortmanager.
- Unter **Benutzermenü → Zwei-Faktor-Sicherheit** lassen sich neue Wiederherstellungscodes
  erzeugen oder eine neue Authenticator-App einrichten. Dazu sind erneut Passwort und ein
  gültiger Authenticator- oder Wiederherstellungscode nötig. Neue Wiederherstellungscodes
  ersetzen die alten vollständig. Beim App-Wechsel bleibt die alte App gültig, bis die neue
  Einrichtung bestätigt wurde; danach werden andere Sitzungen abgemeldet.
- Nach fünf falschen Sicherheitscodes innerhalb von 15 Minuten ist die Prüfung gesperrt,
  sowohl je Benutzer als auch je IP-Adresse. Erneutes Eingeben des Passworts umgeht die Sperre nicht.
  Ein Anmeldevorgang muss innerhalb von zehn Minuten abgeschlossen sein.
- Bereits verwendete Authenticator-Codes werden abgelehnt. Bei einer weiteren Anmeldung direkt
  danach den nächsten Code abwarten. Server und Smartphone benötigen eine korrekte Uhrzeit
  (auf dem Server NTP/Zeitsynchronisation einrichten).

Wenn Smartphone **und** Wiederherstellungscodes verloren sind, kann die Serveradministration
nach Identitätsprüfung die 2FA zurücksetzen:

```bash
cd /opt/schulkasse
sudo -u schulkasse .venv/bin/schulkasse reset-2fa BENUTZERNAME
```

Der Befehl verlangt eine Bestätigung, widerruft alle Sitzungen und Wiederherstellungscodes
dieses Benutzers und erzwingt beim nächsten Passwort-Login eine neue Einrichtung.
Es gibt keinen Schalter zum Abschalten der 2FA in der Weboberfläche.

Die Authenticator-Schlüssel werden mit Fernet verschlüsselt gespeichert; Wiederherstellungscodes
liegen dauerhaft nur als Hash vor. **`SCHULKASSE_SECRET_KEY` muss dauerhaft unverändert bleiben**
und mindestens 32 Zeichen lang sein. Das Installationsskript erzeugt diesen Schlüssel bereits.
Die `.env` separat geschützt sichern: Das reguläre Datenbank-/Beleg-Backup enthält sie nicht.
Zum Wiederherstellen wird derselbe Schlüssel benötigt; bei Verlust müssen die betroffenen
Authenticator-Einrichtungen über die Server-Konsole zurückgesetzt werden.

### Beiträge und Zahlungen

1. **Projekt anlegen** (z. B. „Klassenfahrt 4a Nettersheim“, Kurzcode `KF4A`, Typ Elternbeitrag). Es können
   mehrere Klassen gewählt werden; die Knöpfe „Jahrgang 4“ usw. wählen alle Klassen einer Jahrgangsstufe
   (erkannt an der Zahl am Anfang des Klassennamens). Die Klassen sind beim Anlegen der Beiträge vorausgewählt.
2. **Beiträge anlegen**: Klassen wählen, Betrag und Frist eintragen. BuT-/Härtefallkinder werden automatisch übersprungen.
   Falscher Betrag? Im Projekt unter „Betrag der Elternbeiträge für alle ändern …“ lässt sich der Sollbetrag aller
   Beiträge auf einmal korrigieren. Zahlcodes und Zahlungen bleiben erhalten; bereits erzeugte Briefe danach neu drucken.
3. **Elternbriefe** als PDF erzeugen (eine Seite je Kind mit QR-Code) und in die Postmappen verteilen.
4. **Kontoauszug importieren**: CAMT.052-Datei der Bank hochladen, Vorschau prüfen, bestätigen.
   Überlappende Zeiträume sind unkritisch – Duplikate werden erkannt.
5. **Zuordnung**: Vorschläge mit einem Klick oder gesammelt übernehmen, Rest manuell zuordnen, aufteilen oder ignorieren.
6. **Übersicht**: Die Kontrollrechnung zeigt, ob jeder Umsatz verbucht ist.

### Beginn der Kontoführung und Anfangsbestand

Beim Start mit der Schulkasse liegt meist schon Geld auf dem Konto. Unter
**Verwaltung → Konten → Kontoführung & Anfangsbestand** wird das je Konto eingerichtet:

1. Kontoauszüge ruhig mit Vorlauf importieren (z. B. ab Anfang des Vormonats).
2. **Stichtag** festlegen (nur Administration): erster Tag, ab dem Umsätze in der Schulkasse zugeordnet werden.
   Frühere Umsätze bleiben gespeichert und über Suche und Umsatzliste auffindbar (Status „vor Beginn“),
   erscheinen aber nicht in der Zuordnung, nicht im Matching und nicht in der Kontrollrechnung.
3. Die App berechnet den **Anfangsbestand** zum Stichtag (Kontostand vor dem ersten importierten Auszug plus alle
   gebuchten Umsätze davor). Bitte mit dem Kontoauszug der Bank vergleichen. Passen die importierten Salden nicht
   lückenlos zusammen, weist die Seite darauf hin.
4. Den Anfangsbestand **auf Projekte („Töpfe“) verteilen**, z. B. 800 € Spenden, 350 € Kopiergeld. Wurde ein Beitrag
   schon vor dem Stichtag bezahlt, in der Zeile zusätzlich seinen Zahlcode angeben – der Posten gilt dann als bezahlt.
   Alternativ direkt am Posten: *Wurde vor Beginn der Kontoführung bezahlt …*. Negative Anteile sind möglich,
   etwa wenn ein Projekt zum Stichtag noch Geld schuldet.

Anteile werden nie gelöscht, nur storniert (mit Begründung, protokolliert). Ein Stichtag lässt sich nur setzen oder
nach hinten verschieben, solange davor keine Umsätze zugeordnet oder ignoriert sind – sonst würde dasselbe Geld
doppelt gezählt. Ohne Stichtag beginnt die Kontoführung mit dem ersten importierten Umsatz; der Kontostand davor
ist dann der Anfangsbestand und muss ebenfalls verteilt werden.

### Überzahlungen

Wurde auf einen Posten mehr gezahlt als verlangt, erscheint er unter *Auswertungen → Überzahlungen* und zählt auf der
Übersicht mit. Nach der Klärung – zurückerstattet, als Spende behalten, per Umbuchung in einen anderen Topf gebucht
oder Sollbetrag angepasst – wird der Fall dort oder direkt am Posten mit einer Pflichtbegründung **als geklärt
markiert**. An den Beträgen ändert das nichts, der Vorgang wird protokolliert und bleibt unter „Geklärt“ sichtbar.
Kommt später noch mehr Geld herein, erscheint der Posten wieder in der offenen Liste.

### Abgeschlossene Projekte und Restsalden

Ein abgeschlossenes Projekt zeigt auf seiner Seite ausdrücklich, ob es **ausgeglichen** ist (Saldo 0,00 €) oder mit
einem **Restsaldo** abgeschlossen wurde; in der Projektliste steht dann ein Kennzeichen „Restsaldo“, und die Spalten
für erwartete Beträge bleiben leer. Ausgeglichene Projekte tauchen in *Geld je Projekt und Konto* nicht mehr auf.

Stimmt ein Saldo nicht mit der Erwartung überein, schlüsselt

```bash
cd /opt/schulkasse && runuser -u schulkasse -- .venv/bin/schulkasse pruefe-salden
```

ihn je Projekt nach Umsätzen, Anfangsbestand und Umbuchungen auf. Der Befehl schreibt nichts, prüft zusätzlich, ob
die Datenbanksicht `zahlung` die Umbuchungen enthält, und listet Umbuchungen ohne hinterlegtes Konto.

### Monatsbericht für den Kontoinhaber

*Auswertungen → Monatsbericht* erzeugt den Bericht, den die Stadt als Kontoinhaberin monatlich bekommt – als PDF
und als zwei CSV-Dateien (Buchungen, Projekte). Er enthält

* je Konto den Auszug des Monats: Anfangs- und Endstand, Eingänge, Ausgänge und **zu jeder Buchung das Projekt**
  (Kurzcode, Projektname und, bei Elternbeiträgen, den Zahlcode). Nicht oder nur teilweise zugeordnete Umsätze und
  ignorierte Umsätze sind als solche gekennzeichnet;
* die **Verteilung des Monats-Endstands auf die Projekte**: Stand am Monatsanfang, verteilter Anfangsbestand,
  Eingänge, Ausgänge, Umbuchungen, Stand am Monatsende – bei mehreren Konten zusätzlich je Konto;
* eine Kontrollzeile: Summe der Projektstände + „noch nicht auf Projekte verteilt“ = Kontostand am Monatsende.

**Namen von Kindern enthält der Bericht nicht.** Beiträge erscheinen nur mit ihrem Zahlcode, damit er ohne
Datenschutzbedenken weitergegeben werden kann. Ohne Angabe zeigt die Seite den Vormonat; Monat und Konto lassen
sich frei wählen. Jeder Export wird protokolliert. Umbuchungen zwischen Projekten zählen in dem Monat, in dem sie
gebucht wurden.

### Mehrere Konten: Geld je Projekt und Konto

*Auswertungen → Geld je Projekt und Konto* zeigt je Projekt, welcher Teilbetrag auf welchem Konto liegt (als
Tabelle, PDF und CSV). Grundlage sind die zugeordneten Umsätze, die verteilten Anfangsbestände und die
Umbuchungen. Ein Filter zeigt nur Projekte, die auf mehreren Konten liegen. Die Fußzeile vergleicht die Summe je
Konto mit dem letzten Kontostand; „noch nicht verteilt / zugeordnet“ entspricht der Differenz aus der
Kontrollrechnung.

Soll ein Projekt nur noch auf einem Konto liegen, den Teilbetrag per **Banküberweisung** vom einen auf das andere
Konto überweisen, beide Kontoauszüge importieren und **beide Umsätze demselben Projekt** zuordnen (ohne Posten,
Ausgang auf dem einen, Eingang auf dem anderen Konto). Der Gesamtsaldo des Projekts bleibt gleich, die Verteilung
wandert. Eine Umbuchung zwischen Projekten führt kein Geld zwischen Konten über; sie merkt sich nur, auf welchem
Konto der Betrag liegt (Feld „Konto, auf dem das Geld liegt“, vorbelegt mit dem Konto mit dem größten Guthaben).

### Projekt abschließen und Restbeträge umbuchen

Beim Abschließen dürfen keine offenen oder teilweise bezahlten Posten mehr vorhanden sein, und das Projekt sollte
einen Saldo von 0,00 € haben. Bleibt ein Rest, zeigt die App ihn an und verlangt eine Entscheidung:

- **Umbuchen (empfohlen):** Im Projekt unter *Umbuchungen* den Restbetrag auf ein anderes Projekt buchen, etwa in
  einen Sammeltopf. Bei einem Fehlbetrag läuft es andersherum: Ein anderes Projekt gleicht ihn aus.
- **Mit Restsaldo abschließen:** möglich, muss aber ausdrücklich bestätigt werden. Der Betrag bleibt dann auf dem
  abgeschlossenen Projekt liegen.

Eine **Umbuchung** verschiebt nur den Topf, auf dem das Geld liegt. Auf dem Bankkonto passiert nichts, und die
Kontrollrechnung bleibt unverändert: Was ein Projekt abgibt, bekommt ein anderes. Jede Umbuchung braucht einen Grund,
wird protokolliert und erscheint in beiden Projekten sowie im Jahresabschluss-PDF. Gelöscht wird sie nie, nur
storniert. Optional lässt sich ein Zahlcode angeben, dann gilt der entsprechende Beitrag als bezahlt – zum Beispiel,
wenn der Förderverein den Beitrag eines Kindes übernimmt.

Umgebucht wird nur zwischen **offenen** Projekten. Ein abgeschlossenes Projekt muss dafür kurz wieder geöffnet werden.

### Vorfinanzierung und Erstattung

Zahlt die Schule etwas vor und bekommt es später erstattet (z. B. von der Stadtkasse), beide Umsätze **demselben
Projekt** zuordnen, am besten vom Typ *Vorkasse*: einen Ausgangsposten (Rechnung) und einen Eingangsposten
(Erstattung) anlegen. Das Matching schlägt beide Umsätze bei passendem Betrag vor. Bis zur Erstattung zeigt das
Projekt einen negativen Saldo und die erwartete Erstattung, danach steht der Saldo auf 0,00 €.

Gibt es den Posten noch nicht, lässt er sich direkt beim Umsatz anlegen: *Umsatz → „Neuen Posten anlegen und
zuordnen …“*. Bei einer Abbuchung entsteht ein Ausgangsposten, dem der Umsatz sofort zugeordnet wird. Mit dem
Haken „Das Geld kommt zurück“ wird zusätzlich ein offener Eingangsposten „Rückzahlung: …“ über denselben Betrag
angelegt, dem das Matching die spätere Erstattung zuordnen kann (bei Eingängen entsprechend ein Ausgangsposten
„Auszahlung: …“).

## CLI-Befehle

```bash
schulkasse migrate                      # Migrationen anwenden (passiert auch beim Start)
schulkasse create-admin                 # ersten Administrator anlegen
schulkasse backup [--ziel DIR]          # konsistenter Snapshot (VACUUM INTO) + Belege als .tar.gz
schulkasse import-camt DATEI [--ja]     # CAMT-Import ohne Weboberfläche
schulkasse testdaten [--ziel testdaten] # synthetische CAMT-Dateien + Klassenliste erzeugen
schulkasse demo [--mit-import]          # Demo-Daten in eine LEERE Datenbank laden (nur Testsysteme)
schulkasse pruefe-salden                # Salden je Projekt aufschlüsseln (nur lesend, zur Fehlersuche)
```

Ohne Installation als Paket: `.venv/bin/python -m schulkasse <befehl>` bzw. `flask --app schulkasse <befehl>`.

## Sicherung und Wiederherstellung

`schulkasse backup` schreibt `instance/backups/schulkasse-backup-JJJJMMTT-HHMMSS.tar.gz` (Rechte 600) mit einem
konsistenten SQLite-Snapshot und dem Belegverzeichnis. Die mitgelieferte Timer-Unit sichert täglich um 02:30 Uhr.
Die Archive sollten zusätzlich auf ein anderes System kopiert werden (z. B. per `rsync` aus einem Cronjob).

Wiederherstellen:

```bash
sudo systemctl stop schulkasse
tar -xzf schulkasse-backup-….tar.gz -C /tmp/restore
sudo -u schulkasse cp /tmp/restore/schulkasse.sqlite3 /opt/schulkasse/instance/schulkasse.sqlite3
sudo -u schulkasse rm -f /opt/schulkasse/instance/schulkasse.sqlite3-wal /opt/schulkasse/instance/schulkasse.sqlite3-shm
sudo -u schulkasse cp -r /tmp/restore/belege /opt/schulkasse/instance/
sudo systemctl start schulkasse
```

## Tests und Testdaten

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Die Tests decken Parser, Matching und Saldenberechnung ab und enthalten jedes der 14 Abnahmekriterien
(`tests/test_abnahme.py`). `testdaten/` enthält die mit `schulkasse testdaten` erzeugten Dateien:

| Datei | Inhalt |
|---|---|
| `camt052_teil1.xml` | camt.052.001.08: korrekter Code, verstümmelter Code, SEPA-Präfixe, Sammelzahlung zweier Geschwister, Teilzahlung, Überzahlung, Zahlung ohne Code, Bankgebühr, Erstattung der Stadtkasse, Lieferantenzahlung, Spende, zwei identische Kopiergeld-Zahlungen, ein vorgemerkter Umsatz |
| `camt052_teil2_ueberlappend.xml` | camt.052.001.02 (Status als Textknoten), überlappt mit Teil 1, enthält den nun gebuchten vorgemerkten Umsatz und die Restzahlung |
| `camt052_fremdkonto.xml` | Umsätze eines fremden Kontos (muss abgelehnt werden) |
| `klassenliste.csv` | Klassenliste in Windows-1252 |

Die Zahlcodes in den Dateien passen zu den Daten aus `schulkasse demo`. Schnell ausprobieren:

```bash
export SCHULKASSE_DB=/tmp/demo.sqlite3 SCHULKASSE_COOKIE_SECURE=0 SCHULKASSE_HINTER_PROXY=0
# Zusätzlich einen dauerhaften zufälligen SCHULKASSE_SECRET_KEY in .env setzen (siehe Erstkonfiguration).
.venv/bin/python -m schulkasse create-admin && .venv/bin/python -m schulkasse demo
.venv/bin/python -m flask --app schulkasse run     # http://127.0.0.1:5000, dann testdaten/*.xml importieren
```

## Hinweise zur Umsetzung

Festlegungen, die über das Lastenheft hinausgehen oder es auslegen:

- **Anfangsbestand in der Kontrollrechnung.** Die Formel aus F8.3 ergibt nur dann 0, wenn das Konto bei 0,00 €
  begonnen hat. Deshalb gibt es je Konto einen Stichtag und einen Anfangsbestand, der auf Projekte verteilt wird.
  Kontrollrechnung: Kontostand − verteilter Anfangsbestand − Zuordnungen − ignorierte Umsätze (jeweils ab Stichtag)
  = Differenz. Sie ist 0, wenn der Anfangsbestand vollständig verteilt und jeder spätere Umsatz verbucht ist.
  Der Kontostand vor dem ersten importierten Umsatz wird beim ersten Import aus der Datei (OPBD/PRCD) übernommen
  und ist unter *Verwaltung → Konten* änderbar.
- **Restzahlung über R1.** Ein Umsatz mit korrektem Zahlcode, dessen Betrag genau dem noch offenen Rest eines
  teilweise bezahlten Postens entspricht, wird wie R1 behandelt (Abnahmekriterium 6). Zahlungen auf bereits
  erledigte Posten werden nie automatisch gebucht, sondern als R2-Vorschlag mit Hinweis „mögliche Doppelzahlung“.
- **Posten ohne Kind** (Erstattung Stadtkasse, Lieferantenrechnung) erhalten einen Vorschlag mit Konfidenz 50,
  wenn der Betrag genau einem offenen Posten entspricht (analog R6, auch für Ausgänge nach F4.6).
- **Lernen der Zahler-IBAN** (F4.5) erfolgt bei jeder Zuordnung zu einem Kinderposten, also auch bei bestätigten
  Vorschlägen und automatischen R1-Buchungen.
- **Zahlcode-Erkennung.** Zusätzlich zum regulären Ausdruck werden alle Teilstrings des normalisierten Zwecks gegen
  die bekannten Codes geprüft. So wird ein Code auch erkannt, wenn nach dem Entfernen der Leerzeichen ein Wort direkt
  davor klebt („ZahlungKF4A0042“).
- **Exporte ohne BuT-Kinder.** CSV-/PDF-Exporte des Zahlungsstands und die Ansicht der Lehrkräfte enthalten weder
  BuT-Kinder noch erlassene Posten, weil diese Listen die Kasse verlassen können (F3.3).
- **Anonymisierung** erfasst nur Kinder, die die Schule verlassen haben (inaktiv) und in keinem jüngeren Projekt
  vorkommen. Kindernamen im Verwendungszweck und in Protokolleinträgen zum Kind werden mit geschwärzt.
- **CSV-Beträge** werden als `1234,56` ohne Tausenderpunkt und Währungszeichen ausgegeben, damit Tabellenprogramme
  damit rechnen können. In der Oberfläche und in PDFs gilt durchgängig `1.234,56 €`.
- **XML-Parsing** mit `xml.etree.ElementTree` (Standardbibliothek), daher ist `lxml` nicht nötig.
- **PDF mit WeasyPrint** wie im Lastenheft vorgesehen.

## Offene Punkte vor Inbetriebnahme (Lastenheft Abschnitt 16)

- IBAN, BIC und Kontoinhaber des Schulkontos, ggf. weitere Konten → *Verwaltung → Konten*
- Briefkopf, Logo, Standardtext des Elternbriefs, Hinweistext zur Kostenübernahme → *Verwaltung → Briefvorlage*
  (mit PDF-Vorschau; Felder mit Platzhaltern sind dort markiert)
- Anonymisierungsfrist nach Abstimmung mit dem Datenschutzbeauftragten → `SCHULKASSE_ANONYMISIERUNG_FRIST_TAGE`
- Eine echte, anonymisierte CAMT.052-Beispieldatei der VR-Bank, um den Parser gegen das reale Format zu prüfen
- Verzeichnis von Verarbeitungstätigkeiten ausfüllen: `docs/Verzeichnis_Verarbeitungstaetigkeiten.md`
