# Lastenheft: Schulkasse

Beitrags- und Kassenbuchverwaltung für die GGS Niederkassel

Version 1.0 · Stand: 09.09.2026

---

## 0. Hinweise für den Coding-Agenten

Dieses Dokument beschreibt **was** die Anwendung leisten muss, nicht Zeile für Zeile **wie**. Implementierungsdetails, die hier festgelegt sind (Datenmodell, Parser-Verhalten, QR-Format, Matching-Regeln), sind verbindlich, weil sie fachlich begründet oder durch externe Standards vorgegeben sind. Alles andere ist Umsetzungsfreiheit.

Alle mit `[PLATZHALTER: ...]` markierten Stellen müssen vor Inbetriebnahme durch reale Werte ersetzt werden. Sie gehören in eine Konfigurationsdatei, nicht in den Quellcode.

Die Anwendung ist **deutschsprachig**. Sämtliche Oberflächentexte, Fehlermeldungen, PDF-Ausgaben und E-Mail-Texte sind auf Deutsch. Code, Kommentare und Commit-Nachrichten auf Englisch.

---

## 1. Zweck und Ausgangslage

Die Schule verwaltet ein Girokonto, über das sehr unterschiedliche Geldflüsse laufen:

- Elternbeiträge für Klassenfahrten, Ausflüge, Theaterbesuche
- Sammelbestellungen (Arbeitshefte, Kopiergeld, Werkmaterial)
- Vorkasse-Zahlungen der Schule an Lieferanten, die später von der Stadtkasse erstattet werden
- Zuwendungen, Spenden, Rückerstattungen, Bankgebühren

Heute wird das in Tabellen und auf Papier geführt. Der Abgleich, wer bezahlt hat, erfolgt manuell anhand von Kontoauszügen.

**Ziel der Anwendung:**

1. Für jeden Elternbeitrag einen individuellen Überweisungs-QR-Code erzeugen, der auf einem gedruckten Elternbrief mitgegeben wird.
2. Kontoumsätze aus CAMT.052-Exporten der Hausbank einlesen.
3. Eingehende Zahlungen automatisch den offenen Beiträgen zuordnen und offene Posten sichtbar machen.
4. Darüber hinaus als vollständiges, projektbezogenes Kassenbuch dienen, in dem **jeder** Umsatz des Kontos einem Projekt zugeordnet wird.

**Erfolgskriterium in einem Satz:** Zu jedem Zeitpunkt muss die Anwendung beantworten können, wie viel Geld auf welchem Projekt liegt, wer noch nicht gezahlt hat und welche Umsätze noch nicht zugeordnet sind.

---

## 2. Abgrenzung: Was die Anwendung ausdrücklich NICHT tut

Diese Punkte sind bewusst ausgeschlossen und dürfen nicht implementiert werden.

| Nicht im Umfang | Begründung |
|---|---|
| Verarbeitung, Verwahrung oder Weiterleitung von Geld | Die Anwendung erzeugt nur Zahlungsinformationen. Es fließt kein Geld durch das System. Damit greift keine Erlaubnispflicht nach ZAG. |
| Auslösen von Überweisungen oder Lastschriften | Ausschließlich lesende Verarbeitung von Kontoauszugsdateien. |
| Automatischer Bankzugriff (FinTS, HBCI, PSD2/XS2A) | In Version 1.0 ausgeschlossen. Der Import erfolgt manuell per Datei-Upload. Keine Bankzugangsdaten werden gespeichert. |
| Elternportal, Elternlogins, Eltern-Accounts | Eltern erhalten einen gedruckten Brief. Es gibt keine nach außen offene Nutzerschnittstelle. Das hält die datenschutzrechtliche Angriffsfläche minimal. |
| Anbindung an Schulverwaltungssoftware (z. B. SchILD-NRW) | Klassenlisten werden per CSV importiert. |
| Doppelte Buchführung, Bilanzierung, Umsatzsteuer | Es handelt sich um eine Einnahmen-Ausgaben-Rechnung mit Projektzuordnung. |
| E-Mail-Versand an Eltern | Version 1.0 gibt ausschließlich PDF zum Ausdruck aus. |

---

## 3. Nutzer und Rollen

Die Anwendung wird von einer sehr kleinen Nutzergruppe verwendet (Richtwert: 3 bis 8 Personen). Es gibt drei Rollen:

**`admin`**
Alles. Benutzerverwaltung, Konfiguration, Löschläufe, Kontoverwaltung.

**`kasse`**
Umsätze importieren, zuordnen, stornieren. Projekte anlegen und schließen. Offene Posten anlegen. Briefe erzeugen. Exporte ziehen. Dies ist die Hauptrolle (Schulleitung, Sekretariat).

**`lehrkraft`**
Nur lesend, und nur auf Projekte, denen die Person als verantwortlich zugeordnet ist. Sieht dort den Zahlungsstand der eigenen Klasse und kann den Serienbrief für das eigene Projekt erzeugen. Sieht **keine** Kontoumsätze, die nicht dem eigenen Projekt zugeordnet sind, und keine IBANs.

Es gibt keine Selbstregistrierung. Benutzer werden ausschließlich durch `admin` angelegt.

---

## 4. Technische Rahmenbedingungen

Die Zielumgebung ist ein bestehender Ubuntu-Server mit nginx als Reverse Proxy und systemd zur Prozessverwaltung. Die Anwendung liegt unter `/opt/schulkasse`.

| Aspekt | Vorgabe |
|---|---|
| Sprache | Python 3.12 |
| Framework | Flask, Jinja2-Templates, serverseitig gerendert |
| Datenbank | SQLite, WAL-Modus aktiviert, Fremdschlüssel aktiviert (`PRAGMA foreign_keys = ON`) |
| Migrationen | Nummerierte SQL-Migrationsdateien, die beim Start idempotent angewendet werden. Kein ORM-Automigrationsmagie. |
| ORM | SQLAlchemy Core oder direktes `sqlite3` mit Row-Factory. Kein schweres ORM erforderlich. |
| Frontend | Kein Build-Schritt. Kein npm. Vanilla JS plus optional htmx für Teilaktualisierungen. CSS handgeschrieben oder als einzelne Datei ausgeliefert. |
| QR-Erzeugung | `segno` (reines Python, keine Bildbibliothek nötig) |
| PDF-Erzeugung | `WeasyPrint` (HTML/CSS nach PDF). Alternativ ReportLab, dann aber begründen. |
| XML-Parsing | `lxml` oder `xml.etree.ElementTree`, namespace-agnostisch (siehe 6.1) |
| Passwörter | `argon2-cffi` |
| Deployment | gunicorn hinter nginx, systemd-Unit-Datei wird mitgeliefert |
| Tests | `pytest`. Testabdeckung verpflichtend für Parser, Matching und Saldenberechnung. |
| Zeitzone | Europe/Berlin. Alle Zeitstempel in der Datenbank UTC, Anzeige lokal. |
| Beträge | **Ausschließlich Integer in Cent.** Niemals Float. Formatierung erst in der Anzeigeschicht. |

Externe Netzwerkzugriffe zur Laufzeit sind nicht vorgesehen. Die Anwendung muss vollständig offline funktionieren.

---

## 5. Datenmodell

Verbindliches Schema. Spaltennamen und Beziehungen sind einzuhalten.

### 5.1 Stammdaten

```sql
CREATE TABLE konto (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL,          -- "Schulkonto", "Fördervereinskonto"
    iban              TEXT NOT NULL UNIQUE,
    bic               TEXT,
    inhaber           TEXT NOT NULL,          -- Empfängername für den QR-Code
    aktiv             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE schuljahr (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL UNIQUE,   -- "2026/27"
    beginn            DATE NOT NULL,
    ende              DATE NOT NULL,
    aktiv             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE klasse (
    id                INTEGER PRIMARY KEY,
    schuljahr_id      INTEGER NOT NULL REFERENCES schuljahr(id),
    bezeichnung       TEXT NOT NULL,          -- "3b"
    UNIQUE (schuljahr_id, bezeichnung)
);

CREATE TABLE kind (
    id                INTEGER PRIMARY KEY,
    klasse_id         INTEGER NOT NULL REFERENCES klasse(id),
    vorname           TEXT NOT NULL,
    nachname          TEXT NOT NULL,
    familie_id        INTEGER REFERENCES familie(id),
    but_status        INTEGER NOT NULL DEFAULT 0,  -- 1 = Kosten werden über BuT/Härtefall getragen
    but_notiz         TEXT,
    anonymisiert      INTEGER NOT NULL DEFAULT 0,
    erstellt_am       TIMESTAMP NOT NULL
);

-- Geschwisterkinder gehören zu einer Familie. Zahlungen kommen häufig
-- als Sammelbetrag für mehrere Kinder einer Familie.
CREATE TABLE familie (
    id                INTEGER PRIMARY KEY,
    bezeichnung       TEXT NOT NULL           -- "Familie Müller"
);

-- Gelernte Zuordnung: von welcher IBAN zahlt welche Familie.
-- Wird beim manuellen Zuordnen automatisch befüllt und beim
-- nächsten Import als Matching-Hinweis genutzt.
CREATE TABLE zahler_iban (
    id                INTEGER PRIMARY KEY,
    familie_id        INTEGER NOT NULL REFERENCES familie(id),
    iban              TEXT NOT NULL,
    zahler_name       TEXT,
    treffer_anzahl    INTEGER NOT NULL DEFAULT 1,
    zuletzt_gesehen   DATE,
    UNIQUE (familie_id, iban)
);
```

### 5.2 Projekte und offene Posten

```sql
CREATE TABLE projekt (
    id                INTEGER PRIMARY KEY,
    schuljahr_id      INTEGER NOT NULL REFERENCES schuljahr(id),
    bezeichnung       TEXT NOT NULL,          -- "Klassenfahrt 4a Nettersheim"
    kurzcode          TEXT NOT NULL UNIQUE,   -- "KF4A" - geht in den Verwendungszweck
    typ               TEXT NOT NULL,          -- elternbeitrag | vorkasse | sammeltopf | sonstiges
    klasse_id         INTEGER REFERENCES klasse(id),
    verantwortlich_user_id INTEGER REFERENCES benutzer(id),
    beschreibung      TEXT,
    status            TEXT NOT NULL DEFAULT 'offen',  -- offen | abgeschlossen
    abgeschlossen_am  DATE,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE offener_posten (
    id                INTEGER PRIMARY KEY,
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    richtung          TEXT NOT NULL,          -- eingang | ausgang
    kategorie         TEXT NOT NULL,          -- elternbeitrag | erstattung | rechnung | sonstiges
    kind_id           INTEGER REFERENCES kind(id),   -- nur bei elternbeitrag
    bezeichnung       TEXT NOT NULL,          -- bei erstattung z.B. "Erstattung Stadtkasse Bestellung 4711"
    sollbetrag_cent   INTEGER NOT NULL CHECK (sollbetrag_cent > 0),
    faellig_am        DATE,
    zahlcode          TEXT UNIQUE,            -- "KF4A-0427", nur bei richtung=eingang
    status            TEXT NOT NULL DEFAULT 'offen',
                      -- offen | teilweise | erledigt | erlassen | storniert
    erlass_grund      TEXT,
    notiz             TEXT,
    erstellt_am       TIMESTAMP NOT NULL
);
```

### 5.3 Bankumsätze und Zuordnung

```sql
-- Unveränderlich. Nach dem Import wird an einem bankumsatz-Datensatz
-- niemals etwas geändert oder gelöscht.
CREATE TABLE bankumsatz (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    import_id         INTEGER NOT NULL REFERENCES import(id),
    buchungsdatum     DATE NOT NULL,
    valutadatum       DATE,
    betrag_cent       INTEGER NOT NULL,       -- vorzeichenbehaftet: Eingang positiv, Ausgang negativ
    waehrung          TEXT NOT NULL DEFAULT 'EUR',
    status            TEXT NOT NULL,          -- BOOK | PDNG
    gegen_name        TEXT,
    gegen_iban        TEXT,
    verwendungszweck  TEXT,                   -- alle Ustrd-Elemente zusammengefügt
    end_to_end_id     TEXT,
    bank_ref          TEXT,                   -- AcctSvcrRef
    geschaeftsvorfall TEXT,                   -- BkTxCd, z.B. Zahlungsart
    dedup_hash        TEXT NOT NULL,
    zuordnungsstatus  TEXT NOT NULL DEFAULT 'offen',
                      -- offen | teilweise | zugeordnet | ignoriert
    erstellt_am       TIMESTAMP NOT NULL,
    UNIQUE (konto_id, dedup_hash)
);

-- n:m. Ein Umsatz kann auf mehrere Projekte/Posten aufgeteilt werden,
-- ein Posten kann durch mehrere Umsätze gedeckt werden (Ratenzahlung).
CREATE TABLE zuordnung (
    id                INTEGER PRIMARY KEY,
    bankumsatz_id     INTEGER NOT NULL REFERENCES bankumsatz(id),
    projekt_id        INTEGER NOT NULL REFERENCES projekt(id),
    offener_posten_id INTEGER REFERENCES offener_posten(id),
    betrag_cent       INTEGER NOT NULL,       -- gleiches Vorzeichen wie der Umsatz
    quelle            TEXT NOT NULL,          -- auto | manuell | regel
    konfidenz         INTEGER,                -- 0-100, nur bei quelle=auto
    notiz             TEXT,
    erstellt_von      INTEGER NOT NULL REFERENCES benutzer(id),
    erstellt_am       TIMESTAMP NOT NULL,
    storniert         INTEGER NOT NULL DEFAULT 0,
    storniert_von     INTEGER REFERENCES benutzer(id),
    storniert_am      TIMESTAMP,
    storno_grund      TEXT
);

CREATE TABLE import (
    id                INTEGER PRIMARY KEY,
    konto_id          INTEGER NOT NULL REFERENCES konto(id),
    dateiname         TEXT NOT NULL,
    datei_hash        TEXT NOT NULL,
    zeitraum_von      DATE,
    zeitraum_bis      DATE,
    anzahl_gesamt     INTEGER NOT NULL,
    anzahl_neu        INTEGER NOT NULL,
    anzahl_duplikat   INTEGER NOT NULL,
    saldo_schluss_cent INTEGER,               -- CLBD/ITBD aus der Datei
    saldo_datum       DATE,
    importiert_von    INTEGER NOT NULL REFERENCES benutzer(id),
    importiert_am     TIMESTAMP NOT NULL
);
```

### 5.4 Belege, Benutzer, Protokoll

```sql
CREATE TABLE beleg (
    id                INTEGER PRIMARY KEY,
    projekt_id        INTEGER REFERENCES projekt(id),
    zuordnung_id      INTEGER REFERENCES zuordnung(id),
    dateiname         TEXT NOT NULL,
    pfad              TEXT NOT NULL,
    mimetype          TEXT NOT NULL,
    groesse_bytes     INTEGER NOT NULL,
    hochgeladen_von   INTEGER NOT NULL REFERENCES benutzer(id),
    hochgeladen_am    TIMESTAMP NOT NULL
);

CREATE TABLE benutzer (
    id                INTEGER PRIMARY KEY,
    benutzername      TEXT NOT NULL UNIQUE,
    anzeigename       TEXT NOT NULL,
    passwort_hash     TEXT NOT NULL,
    rolle             TEXT NOT NULL,          -- admin | kasse | lehrkraft
    aktiv             INTEGER NOT NULL DEFAULT 1,
    letzter_login     TIMESTAMP,
    erstellt_am       TIMESTAMP NOT NULL
);

CREATE TABLE protokoll (
    id                INTEGER PRIMARY KEY,
    zeitpunkt         TIMESTAMP NOT NULL,
    benutzer_id       INTEGER REFERENCES benutzer(id),
    aktion            TEXT NOT NULL,          -- z.B. zuordnung.anlegen
    entitaet          TEXT NOT NULL,
    entitaet_id       INTEGER,
    alt_wert          TEXT,                   -- JSON
    neu_wert          TEXT,                   -- JSON
    ip                TEXT
);
```

Protokollpflichtig sind sämtliche schreibenden Vorgänge an `zuordnung`, `offener_posten`, `projekt`, `kind`, `benutzer` sowie jeder Import und jeder Löschlauf.

---

## 6. Funktionale Anforderungen

### F1 — CAMT.052-Import

**F1.1** Der Nutzer lädt über ein Formular eine Datei hoch. Zulässig sind `.zip` und `.xml`. Bei einem ZIP werden alle enthaltenen `.xml`-Dateien verarbeitet; andere Einträge werden ignoriert. Das ZIP wird im Speicher entpackt (`zipfile`), nicht auf die Platte geschrieben.

**F1.2 Namespace-Behandlung.** Die Banken liefern je nach Institut unterschiedliche CAMT-Versionen (`camt.052.001.02` bis `.08`). Der Parser darf sich **nicht** auf einen festen Namespace festlegen. Elemente sind über den lokalen Namen zu adressieren (bei `lxml` über `local-name()`, bei ElementTree durch Abschneiden des Namespace-Präfixes).

**F1.3 Auszulesende Felder je `Ntry`-Element:**

| Ziel | Quelle | Hinweis |
|---|---|---|
| `betrag_cent` | `Amt` in Verbindung mit `CdtDbtInd` | `CRDT` → positiv, `DBIT` → negativ. Der Betrag im XML ist immer vorzeichenlos. |
| `status` | `Sts` | In camt.052.001.02 ist `Sts` ein Textknoten, ab `.08` liegt der Code in `Sts/Cd`. **Beide Varianten müssen unterstützt werden.** |
| `buchungsdatum` | `BookgDt/Dt` oder `BookgDt/DtTm` | |
| `valutadatum` | `ValDt/Dt` | |
| `bank_ref` | `AcctSvcrRef` | |
| `end_to_end_id` | `NtryDtls/TxDtls/Refs/EndToEndId` | Wert `NOTPROVIDED` als leer behandeln |
| `gegen_name` | bei Eingang `RltdPties/Dbtr/Nm`, bei Ausgang `RltdPties/Cdtr/Nm` | |
| `gegen_iban` | bei Eingang `RltdPties/DbtrAcct/Id/IBAN`, bei Ausgang `CdtrAcct/Id/IBAN` | |
| `verwendungszweck` | **alle** `RmtInf/Ustrd`-Elemente | Deutsche Institute zerlegen den Verwendungszweck in mehrere `Ustrd`-Blöcke zu je 35 Zeichen. Diese sind **ohne Trennzeichen** zu konkatenieren, anschließend Mehrfach-Leerzeichen zusammenfassen. |
| `geschaeftsvorfall` | `BkTxCd/Prtry/Cd` oder `Domn/Cd` | |

**F1.4 SEPA-Präfixe.** Manche Institute füllen den Verwendungszweck mit Steuerzeichen der Form `EREF+...SVWZ+...KREF+...MREF+...ABWA+...`. Erkennt der Parser diese Struktur, extrahiert er den Inhalt hinter `SVWZ+` als Verwendungszweck und `EREF+` als End-to-End-ID. Der Rohtext bleibt zusätzlich erhalten.

**F1.5 Vorgemerkte Umsätze.** Einträge mit `Sts = PDNG` werden importiert, aber deutlich als „vorgemerkt" gekennzeichnet und **können nicht zugeordnet werden**. Erscheint derselbe Umsatz in einem späteren Import als `BOOK`, ersetzt der gebuchte Eintrag den vorgemerkten (der vorgemerkte wird gelöscht, da er nie zugeordnet war).

**F1.6 Idempotenz.** Der wöchentliche Download überlappt sich zwangsläufig. Der Dedup-Schlüssel `dedup_hash` wird nach dieser Priorität gebildet:

1. `AcctSvcrRef`, falls vorhanden und nicht leer
2. sonst `EndToEndId`, falls vorhanden und nicht `NOTPROVIDED`
3. sonst SHA-256 über die Verkettung von `buchungsdatum | betrag_cent | gegen_iban | normalisierter Verwendungszweck | laufende Nummer des Eintrags innerhalb desselben Tages mit identischen Werten`

Der Import erfolgt gegen den Unique-Index als `INSERT ... ON CONFLICT DO NOTHING`. Duplikate werden gezählt und im Importprotokoll ausgewiesen, aber nicht als Fehler behandelt.

**F1.7 Kontoprüfung.** Aus `Rpt/Acct/Id/IBAN` wird das Zielkonto ermittelt. Stimmt sie mit keinem konfigurierten Konto überein, bricht der Import mit klarer Fehlermeldung ab. Es darf nicht möglich sein, Umsätze eines fremden Kontos zu importieren.

**F1.8 Saldo.** Der Schlusssaldo wird aus dem `Bal`-Element mit `BalTp/CdOrPrtry/Cd` gleich `CLBD` gelesen, ersatzweise `ITBD`. Er wird im `import`-Datensatz abgelegt und dient der Kontrollrechnung (F7.3).

**F1.9 Vorschau.** Vor dem endgültigen Schreiben zeigt die Anwendung eine Zusammenfassung: Zeitraum, Anzahl neuer Umsätze, Anzahl Duplikate, Summe Eingänge, Summe Ausgänge, Schlusssaldo. Der Nutzer bestätigt. Erst dann wird geschrieben.

**F1.10** Nach dem Schreiben läuft automatisch der Matching-Lauf (F4) über alle neuen Umsätze.

---

### F2 — Projektverwaltung

**F2.1** Projekte anlegen, bearbeiten, abschließen. Ein Projekt gehört zu einem Schuljahr und optional zu einer Klasse.

**F2.2** Der `kurzcode` ist frei wählbar, muss aber systemweit eindeutig sein, aus 3 bis 8 Zeichen (`A-Z`, `0-9`) bestehen und wird in Großbuchstaben normalisiert. Er ist Bestandteil aller Zahlcodes des Projekts und daher nach der ersten Brieferzeugung **nicht mehr änderbar**.

**F2.3 Projekttypen:**

- `elternbeitrag` — es werden Beiträge von Eltern erwartet, Serienbrieffunktion aktiv
- `vorkasse` — die Schule zahlt vor und erwartet Erstattung (typischerweise durch die Stadtkasse)
- `sammeltopf` — dauerhafter Topf ohne definiertes Ende (z. B. Kopiergeld, Spendenkonto)
- `sonstiges`

**F2.4 Abschluss.** Ein Projekt kann nur abgeschlossen werden, wenn keine offenen Posten mit Status `offen` oder `teilweise` mehr existieren. Sind noch welche vorhanden, muss der Nutzer sie zuvor auf `erledigt`, `erlassen` oder `storniert` setzen. Abgeschlossene Projekte sind schreibgeschützt, können aber wieder geöffnet werden (protokollpflichtig).

---

### F3 — Offene Posten

**F3.1 Anlage von Elternbeiträgen als Massenvorgang.** Der Nutzer wählt ein Projekt, eine Klasse (oder mehrere Klassen, oder einzelne Kinder) und einen Betrag. Das System legt für jedes ausgewählte Kind einen offenen Posten mit eigenem `zahlcode` an.

**F3.2 Zahlcode-Format.** `{KURZCODE}-{NNNN}`, wobei `NNNN` eine je Projekt fortlaufende, vierstellige Zahl ist. Beispiel: `KF4A-0042`. Der Code enthält **keine** personenbezogenen Bestandteile und keine Namensfragmente. Er ist über die gesamte Datenbank eindeutig.

**F3.3 BuT- und Härtefallkinder.** Kinder mit `but_status = 1` werden bei der Massenanlage standardmäßig **übersprungen**. Der Nutzer kann sie über eine ausdrückliche Auswahl doch einbeziehen. Für diese Kinder wird wahlweise gar kein Posten oder ein Posten mit Status `erlassen` erzeugt.

**Diese Kinder dürfen in keinem Erinnerungs- oder Mahnlauf und in keiner Liste erscheinen, die an Dritte oder in die Klasse gegeben wird (F8.4).** Das ist eine harte Anforderung, keine Komforteinstellung.

**F3.4 Erwartete Erstattungen.** Bei Projekten vom Typ `vorkasse` können offene Posten mit `richtung = ausgang` (die eigene Zahlung) und `richtung = eingang, kategorie = erstattung` (die erwartete Rückzahlung) angelegt werden. Diese haben kein `kind_id` und in der Regel keinen `zahlcode`.

**F3.5 Teilzahlung.** Der Status eines Postens ergibt sich aus der Summe der nicht stornierten Zuordnungen:

- Summe = 0 → `offen`
- 0 < Summe < Sollbetrag → `teilweise`
- Summe ≥ Sollbetrag → `erledigt`

Der Status wird bei jeder Änderung an den Zuordnungen neu berechnet, nicht manuell gesetzt. Ausnahmen sind `erlassen` und `storniert`, die ausdrücklich gesetzt werden und einen Grund erfordern.

**F3.6 Überzahlung.** Ist die Summe größer als der Sollbetrag, wird der Posten als `erledigt` geführt und zusätzlich in einer eigenen Liste „Überzahlungen" ausgewiesen, mit Differenzbetrag. Automatische Rückerstattung findet nicht statt.

---

### F4 — Automatische Zuordnung (Matching)

Der Matching-Lauf verarbeitet alle Umsätze mit `zuordnungsstatus = offen` und `status = BOOK`.

**F4.1 Normalisierung.** Vor jedem Vergleich werden Verwendungszweck und Namen normalisiert: Großbuchstaben, Umlaute aufgelöst (ä→AE, ö→OE, ü→UE, ß→SS), sämtliche Zeichen außer `A-Z0-9` entfernt.

**F4.2 Regelkaskade.** Die Regeln werden in dieser Reihenfolge geprüft. Die erste zutreffende gewinnt.

| Nr. | Bedingung | Konfidenz | Aktion |
|---|---|---|---|
| R1 | Genau ein bekannter Zahlcode im Verwendungszweck gefunden **und** Betrag stimmt exakt mit dem Sollbetrag überein | 100 | Automatisch zuordnen |
| R2 | Genau ein Zahlcode gefunden, Betrag weicht ab | 80 | Vorschlag zur Bestätigung, mit Hinweis auf Über- bzw. Unterzahlung |
| R3 | Mehrere Zahlcodes im Verwendungszweck gefunden, Summe der zugehörigen Sollbeträge entspricht dem Umsatzbetrag | 90 | Vorschlag: automatische Aufteilung auf die genannten Posten |
| R4 | Kein Zahlcode, aber die Absender-IBAN ist über `zahler_iban` einer Familie bekannt, und es existiert genau ein offener Posten dieser Familie mit passendem Betrag | 70 | Vorschlag |
| R5 | Kein Zahlcode, IBAN bekannt, Betrag entspricht der Summe mehrerer offener Posten derselben Familie | 60 | Vorschlag mit Aufteilung |
| R6 | Kein Zahlcode, aber der Nachname eines Kindes mit offenem Posten kommt im normalisierten Absendernamen **oder** Verwendungszweck vor, und der Betrag passt exakt | 50 | Vorschlag |
| R7 | Nichts davon | — | Bleibt offen, erscheint in der Zuordnungsansicht |

**F4.3 Schwelle.** Nur Konfidenz 100 wird ohne Rückfrage gebucht. Alles darunter ist ein Vorschlag, der bestätigt werden muss. Der Schwellwert ist konfigurierbar, Voreinstellung 100.

**F4.4 Zahlcode-Erkennung.** Der Code wird per regulärem Ausdruck über dem normalisierten Verwendungszweck gesucht: `[A-Z0-9]{3,8}[0-9]{4}`. Kandidaten werden gegen die Tabelle `offener_posten` geprüft. Falsch geschriebene Codes (fehlende Trennstriche, eingefügte Leerzeichen) werden durch die Normalisierung mit abgedeckt.

**F4.5 Lernen.** Wird ein Umsatz manuell einem Posten mit `kind_id` zugeordnet, legt das System für die Absender-IBAN und die Familie des Kindes einen Eintrag in `zahler_iban` an bzw. erhöht `treffer_anzahl`. Das verbessert R4 und R5 über die Schuljahre hinweg.

**F4.6 Ausgänge.** Umsätze mit negativem Betrag durchlaufen dieselbe Kaskade gegen offene Posten mit `richtung = ausgang`. In der Praxis werden sie meist manuell zugeordnet.

---

### F5 — Zuordnungsansicht

Dies ist der Bildschirm, an dem am meisten Zeit verbracht wird. Er verdient besondere Sorgfalt.

**F5.1** Eine Arbeitsliste aller Umsätze mit `zuordnungsstatus` gleich `offen` oder `teilweise`, absteigend nach Buchungsdatum. Filter nach Konto, Zeitraum, Richtung und Betragsbereich.

**F5.2** Je Zeile werden angezeigt: Datum, Betrag, Absender/Empfänger, Verwendungszweck (vollständig, umbrochen), sowie die Matching-Vorschläge mit Konfidenz.

**F5.3 Ein-Klick-Bestätigung.** Ein Vorschlag muss mit einem einzigen Klick angenommen werden können. Bei mehreren offenen Umsätzen mit Vorschlägen muss eine Mehrfachauswahl mit Sammelbestätigung möglich sein.

**F5.4 Aufteilung.** Ein Umsatz muss auf beliebig viele Projekte und Posten aufgeteilt werden können. Die Oberfläche zeigt dabei permanent den noch nicht verteilten Restbetrag. Eine Aufteilung kann erst gespeichert werden, wenn die Summe der Teilbeträge den Umsatzbetrag nicht überschreitet. Ein Rest darf bestehen bleiben, der Umsatz erhält dann `zuordnungsstatus = teilweise`.

**F5.5 Ignorieren.** Umsätze, die keinem Projekt zuzuordnen sind (Bankgebühren, Zinsen, Fehlbuchungen), können mit Pflichtbegründung auf `ignoriert` gesetzt werden. Sie zählen dann für die Vollständigkeitsprüfung als erledigt, erscheinen aber in einer eigenen Liste.

**F5.6 Storno.** Zuordnungen werden **niemals gelöscht**. Ein Storno setzt `storniert = 1` mit Zeitstempel, Benutzer und Pflichtbegründung. Stornierte Zuordnungen bleiben sichtbar, zählen aber nicht in Salden.

**F5.7 Suche.** Volltextsuche über Verwendungszweck, Gegenname und Betrag, um bei Elternrückfragen („ich habe doch überwiesen") schnell nachsehen zu können.

---

### F6 — EPC-QR-Code

**F6.1 Format.** Erzeugt wird ein EPC-QR-Code (Girocode) nach EPC069-12. Der Nutzdateninhalt besteht aus genau zwölf Zeilen, getrennt durch `\n`:

```
 1  BCD                     Service Tag, konstant
 2  002                     Version
 3  1                       Zeichensatz: 1 = UTF-8
 4  SCT                     SEPA Credit Transfer
 5  {BIC}                   optional bei Version 002, wenn bekannt ausfüllen
 6  {Empfängername}         max. 70 Zeichen
 7  {IBAN}                  ohne Leerzeichen
 8  EUR{Betrag}             z.B. EUR12.50, Punkt als Dezimaltrenner, kein Tausendertrenner
 9                          Purpose Code, leer lassen
10                          Structured Reference, leer lassen
11  {Verwendungszweck}      max. 140 Zeichen, hier steht der Zahlcode
12                          Beneficiary to Originator, leer lassen
```

Zeile 10 und 11 schließen sich gegenseitig aus. Wir verwenden ausschließlich Zeile 11.

**F6.2 Grenzen.** Die Gesamtlänge der Nutzdaten darf 331 Byte nicht überschreiten. Der Betrag muss zwischen `0.01` und `999999999.99` liegen. Überschreitungen führen zu einem Fehler beim Erzeugen, nicht zu einem stillschweigend abgeschnittenen Code.

**F6.3 Zeichensatz.** Obwohl UTF-8 deklariert wird, verarbeiten nicht alle Banking-Apps Umlaute zuverlässig. Empfängername und Verwendungszweck sind daher vor der Kodierung auf den SEPA-Zeichensatz zu reduzieren: `a-z A-Z 0-9 / - ? : ( ) . , ' +` und Leerzeichen. Umlaute werden transliteriert (ä→ae, ö→oe, ü→ue, ß→ss), alle übrigen Zeichen entfernt.

**F6.4 Verwendungszweck.** Aufbau: `{zahlcode} {Projektbezeichnung gekürzt} {Vorname} {Nachname des Kindes}`, insgesamt auf 140 Zeichen begrenzt. Der Zahlcode steht **immer an erster Stelle**, damit er auch dann erhalten bleibt, wenn eine Bank den Zweck kürzt.

**F6.5 Fehlerkorrektur.** Level `M`. Die Ausgabe erfolgt als SVG, damit sie im PDF verlustfrei skaliert. Kantenlänge im Druck mindestens 3 cm, ein Ruhebereich (Quiet Zone) von 4 Modulen ist einzuhalten.

**F6.6 Selbsttest.** Es muss eine Funktion geben, die einen erzeugten Code wieder dekodiert und mit den Eingabedaten vergleicht. Diese läuft als Test, nicht zur Laufzeit.

---

### F7 — Serienbrief und Ausdruck

**F7.1** Für ein Projekt vom Typ `elternbeitrag` erzeugt die Anwendung ein einziges PDF mit einer Seite je Kind, in Klassen- und Namensreihenfolge sortiert, damit es direkt in die Postmappen verteilt werden kann.

**F7.2 Inhalt einer Seite:**

- Schullogo und Briefkopf `[PLATZHALTER: Briefkopf, Anschrift, Kontaktdaten]`
- **TL;DR-Block ganz oben:** Anlass, Betrag, Frist, in kurzen Sätzen und einfacher Sprache
- Freitext des Projekts (vom Nutzer beim Erzeugen bearbeitbar)
- Name des Kindes und Klasse
- Der QR-Code
- Danebenstehend als Klartext zum Abtippen: Empfänger, IBAN, Betrag, Verwendungszweck
- Ein Hinweis für Eltern ohne QR-fähige Banking-App
- `[PLATZHALTER: Hinweistext zur Kostenübernahme bei finanzieller Belastung]` — ein kurzer, nicht stigmatisierender Satz, an wen sich Familien wenden können, wenn die Zahlung schwerfällt

**F7.3** Beim Erzeugen wählt der Nutzer, ob alle Kinder des Projekts, nur die mit offenem Posten oder eine manuelle Auswahl berücksichtigt werden. Letzteres ist für den Nachdruck einzelner Briefe wichtig.

**F7.4** Das Datum der letzten Brieferzeugung wird je Posten festgehalten, damit erkennbar ist, wer wann informiert wurde.

---

### F8 — Auswertungen

**F8.1 Projektübersicht.** Je Projekt:

- **Saldo** = Summe aller nicht stornierten Zuordnungen des Projekts
- **Erwartete Eingänge** = Summe der Sollbeträge offener und teilweise bezahlter Posten mit `richtung = eingang`, abzüglich bereits zugeordneter Beträge
- **Erwartete Ausgänge** = analog für `richtung = ausgang`
- **Prognose** = Saldo + erwartete Eingänge − erwartete Ausgänge
- Zahlungsquote in Prozent und als Verhältnis (z. B. „21 von 26")

**F8.2 Zahlungsstand je Projekt.** Liste aller Kinder mit Status. Sortier- und filterbar. Exportierbar als CSV und PDF.

**F8.3 Vollständigkeitsprüfung (Kontrollrechnung).** Auf dem Dashboard prominent:

```
Kontostand laut letztem Import
− Summe aller nicht stornierten Zuordnungen (kumuliert)
− Summe aller ignorierten Umsätze
= Differenz
```

Ist die Differenz null, ist jeder Umsatz des Kontos verbucht. Ist sie ungleich null, wird die Anzahl nicht zugeordneter Umsätze mit Verlinkung angezeigt. Diese eine Kennzahl ist die wichtigste Anzeige der gesamten Anwendung.

**F8.4 Erinnerungsliste.** Liste aller Kinder mit überfälligen offenen Posten, gruppiert nach Klasse, mit Filter auf ein Stichdatum. Grundlage für den Ausdruck von Erinnerungsschreiben (Wiederverwendung von F7).

Kinder mit `but_status = 1` und Posten mit Status `erlassen` werden in dieser Liste **niemals** ausgegeben, auch nicht ausgegraut oder als Platzhalter.

**F8.5 Kassenbuch-Export.** Chronologische Liste aller Umsätze eines Zeitraums mit Projektzuordnung, als CSV und als PDF. Enthält Anfangs- und Endsaldo, Summen, Erzeugungsdatum und den erzeugenden Benutzer.

**F8.6 Jahresabschluss je Projekt.** PDF mit allen Umsätzen, allen Posten, Endsaldo und Liste der hinterlegten Belege. Dies ist das Dokument, das einer Kassenprüfung vorgelegt wird.

---

### F9 — Stammdatenpflege

**F9.1 CSV-Import von Klassenlisten** mit den Spalten `klasse;nachname;vorname;familie` (Spalte `familie` optional; ist sie leer, wird nach identischem Nachnamen gruppiert und dem Nutzer zur Bestätigung vorgelegt). Trennzeichen Semikolon, Kodierung UTF-8 und Windows-1252 müssen beide erkannt werden.

**F9.2 Schuljahreswechsel.** Funktion zum Hochschreiben: alle Kinder rücken eine Jahrgangsstufe auf, Kinder der Klasse 4 werden auf `inaktiv` gesetzt, neue Klassen 1 werden importiert. Der Vorgang ist vor der Ausführung als Vorschau darzustellen.

**F9.3** BuT-Status je Kind setzbar, nur durch Rolle `kasse` und `admin`, protokollpflichtig.

---

### F10 — Benutzer und Anmeldung

**F10.1** Anmeldung mit Benutzername und Passwort. Passwörter mit Argon2id gehasht. Mindestlänge 12 Zeichen.

**F10.2** Sitzungen als serverseitige Sessions, `Secure`, `HttpOnly`, `SameSite=Lax`. Automatische Abmeldung nach 8 Stunden Inaktivität.

**F10.3** Sperre nach 10 Fehlversuchen für 15 Minuten, IP-bezogen und kontobezogen.

**F10.4** CSRF-Schutz auf allen schreibenden Formularen.

**F10.5** Erster Administrator wird per CLI-Befehl angelegt, nicht über eine Weboberfläche.

---

### F11 — Datenschutz und Aufbewahrung

**F11.1 Zweckbindung.** Die Anwendung verarbeitet Namen und Klassenzugehörigkeit von Kindern sowie Zahlungsdaten. Sie ist ausschließlich im schulischen Netz bzw. über eine gesicherte Verbindung erreichbar und hat keine öffentliche Schnittstelle.

**F11.2 Aufbewahrungskonflikt.** Buchungsbelege unterliegen einer Aufbewahrungsfrist von zehn Jahren, personenbezogene Schülerdaten sind demgegenüber zu löschen, sobald der Zweck entfällt.

Lösung: **Anonymisierungslauf.** Für abgeschlossene Projekte, die älter als `[PLATZHALTER: Frist, Vorschlag 2 Jahre nach Projektabschluss]` sind, wird ein Lauf angeboten, der

- in `kind` Vor- und Nachname durch `Gelöscht #{id}` ersetzt und `anonymisiert = 1` setzt,
- in `bankumsatz` die Felder `gegen_name` und `gegen_iban` durch einen Platzhalter ersetzt,
- in `zahler_iban` die betroffenen Einträge löscht,
- Beträge, Daten, Zahlcodes und Zuordnungen unverändert lässt.

Der Lauf zeigt vorab, was betroffen ist, erfordert eine ausdrückliche Bestätigung und wird protokolliert. Er läuft niemals automatisch.

**F11.3** Ein Kind darf nicht gelöscht werden, solange Zuordnungen daran hängen. Stattdessen greift F11.2.

**F11.4** Zu erzeugen ist eine Vorlage für das Verzeichnis von Verarbeitungstätigkeiten als Markdown-Datei im Repository, mit den Feldern Zweck, Rechtsgrundlage, Kategorien betroffener Personen, Datenkategorien, Empfänger, Löschfristen, technisch-organisatorische Maßnahmen. Inhaltliche Platzhalter sind zu kennzeichnen.

---

### F12 — Betrieb

**F12.1 Konfiguration** über eine `.env`-Datei bzw. Umgebungsvariablen: Datenbankpfad, Secret Key, Uploadverzeichnis, maximale Uploadgröße, Matching-Schwellwert, Anonymisierungsfrist.

**F12.2 Backup.** CLI-Befehl, der einen konsistenten SQLite-Snapshot (`VACUUM INTO`) samt Belegverzeichnis in ein datiertes Archiv schreibt.

**F12.3 Mitzuliefern:**

- `systemd`-Unit-Datei
- nginx-Beispielkonfiguration
- `README.md` mit Installation, Erstkonfiguration und Backup
- `requirements.txt` mit gepinnten Versionen
- CLI-Befehle: `create-admin`, `backup`, `import-camt <datei>`, `migrate`

**F12.4 Testdaten.** Mitzuliefern ist ein Generator, der eine realistische synthetische CAMT.052-Datei erzeugt: Zahlungen mit korrektem Zahlcode, mit verstümmeltem Code, ohne Code, Sammelzahlungen für zwei Kinder, eine Teilzahlung, eine Überzahlung, eine Bankgebühr, eine Erstattung der Stadtkasse, sowie ein vorgemerkter Umsatz. Ohne diese Datei ist die Anwendung nicht abnehmbar.

---

## 13. Nicht-funktionale Anforderungen

| Nr. | Anforderung |
|---|---|
| N1 | Größenordnung: bis 500 Kinder, bis 100 Projekte je Schuljahr, bis 20.000 Umsätze insgesamt. Alle Listenansichten müssen bei dieser Größe unter einer Sekunde laden. |
| N2 | Import einer CAMT-Datei mit 500 Umsätzen inklusive Matching in unter 10 Sekunden. |
| N3 | Bedienung auf Tablet-Bildschirmbreite muss funktionieren (Zuordnungsansicht, Zahlungsstand). Volle Desktop-Optimierung genügt für die Importfunktion. |
| N4 | Keine externen CDN-Aufrufe. Schriften, CSS und JS werden lokal ausgeliefert. |
| N5 | Barrierearme Grundlagen: Beschriftete Formularfelder, ausreichende Kontraste, Bedienbarkeit per Tastatur. |
| N6 | Fehlermeldungen sind fachlich formuliert. „Die Datei enthält keine Umsätze für das Konto DE.. .." statt eines Stacktraces. |
| N7 | Beträge werden ausnahmslos in der Form `1.234,56 €` angezeigt. |

---

## 14. Abnahmekriterien

Die Anwendung gilt als abgenommen, wenn folgende Fälle nachweislich funktionieren. Jeder Punkt ist als automatisierter Test zu hinterlegen.

1. Import der mitgelieferten Testdatei legt die erwartete Anzahl Umsätze an. Ein zweiter Import derselben Datei legt **null** neue Umsätze an.
2. Import einer überlappenden zweiten Datei legt ausschließlich die neuen Umsätze an.
3. Ein Umsatz mit exaktem Zahlcode und exaktem Betrag wird ohne Nutzereingriff zugeordnet, der Posten steht auf `erledigt`.
4. Ein Umsatz mit verstümmeltem Zahlcode (Leerzeichen statt Bindestrich, Kleinschreibung) wird ebenfalls über R1 erkannt.
5. Eine Sammelzahlung über die Summe zweier Geschwisterposten erzeugt einen Aufteilungsvorschlag und lässt sich mit einer Bestätigung auf beide Posten buchen.
6. Eine Teilzahlung setzt den Posten auf `teilweise`; eine spätere Restzahlung auf `erledigt`.
7. Ein erzeugter QR-Code lässt sich dekodieren und liefert exakt die zwölf erwarteten Zeilen mit korrektem Betragsformat.
8. Ein Serienbrief-PDF für eine Klasse mit 26 Kindern enthält 26 Seiten mit paarweise verschiedenen Zahlcodes.
9. Ein Kind mit `but_status = 1` erscheint weder in der Massenanlage noch in der Erinnerungsliste.
10. Die Kontrollrechnung aus F8.3 ergibt null, nachdem alle Umsätze der Testdatei zugeordnet oder ignoriert wurden.
11. Eine stornierte Zuordnung verschwindet aus dem Saldo, bleibt aber in der Detailansicht und im Protokoll sichtbar.
12. Der Anonymisierungslauf entfernt Namen und IBANs, lässt Beträge und Salden aber unverändert. Die Kontrollrechnung ergibt danach weiterhin null.
13. Ein Nutzer der Rolle `lehrkraft` erhält beim Aufruf eines fremden Projekts oder der Umsatzliste HTTP 403.
14. Der Import einer CAMT-Datei mit fremder Konto-IBAN wird mit verständlicher Meldung abgelehnt.

---

## 15. Umsetzungsreihenfolge

Vorschlag für den Coding-Agenten, damit früh etwas Testbares entsteht:

1. Schema, Migrationen, Konfiguration, Anmeldung, `create-admin`
2. Stammdaten: Schuljahr, Klasse, Kind, CSV-Import
3. CAMT-Parser als eigenständiges, testbares Modul samt Testdatengenerator
4. Import-Workflow mit Vorschau und Dedup
5. Projekte und offene Posten inklusive Massenanlage
6. Matching-Kaskade
7. Zuordnungsansicht
8. QR-Erzeugung und Serienbrief
9. Auswertungen und Kontrollrechnung
10. Exporte, Belege, Protokollansicht
11. Anonymisierungslauf, Backup, Deployment-Dateien

Der Parser (Schritt 3) sollte vollständig ohne Weboberfläche testbar sein, bevor darauf aufgebaut wird.

---

## 16. Offene Punkte vor Umsetzungsbeginn

Diese Angaben fehlen noch und müssen vom Auftraggeber geliefert werden:

- `[PLATZHALTER: IBAN, BIC und Kontoinhaber des Schulkontos]`
- `[PLATZHALTER: weitere Konten, z. B. Förderverein]`
- `[PLATZHALTER: Briefkopf als Bilddatei oder HTML]`
- `[PLATZHALTER: Standardtext des Elternbriefs]`
- `[PLATZHALTER: Hinweistext zur Kostenübernahme]`
- `[PLATZHALTER: Aufbewahrungs- und Anonymisierungsfrist nach Abstimmung mit dem Datenschutzbeauftragten]`
- Eine echte, anonymisierte CAMT.052-Beispieldatei der VR-Bank, damit der Parser gegen das reale Format der Hausbank entwickelt wird und nicht nur gegen die synthetische Datei
