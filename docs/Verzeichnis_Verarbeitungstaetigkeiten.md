# Verzeichnis von Verarbeitungstätigkeiten (Art. 30 DSGVO) – Vorlage

**Verarbeitungstätigkeit:** Schulkasse – Verwaltung von Elternbeiträgen und projektbezogenes Kassenbuch

Mit `[PLATZHALTER: …]` markierte Stellen sind von der Schule auszufüllen und mit dem/der Datenschutzbeauftragten abzustimmen.

| Feld | Angabe |
|---|---|
| Verantwortliche Stelle | [PLATZHALTER: Name und Anschrift der Schule, Schulleitung] |
| Datenschutzbeauftragte/r | [PLATZHALTER: Name, Kontakt] |
| Stand | [PLATZHALTER: Datum] |

## 1. Zweck der Verarbeitung

- Erhebung und Nachverfolgung von Elternbeiträgen für schulische Veranstaltungen (Klassenfahrten, Ausflüge,
  Theaterbesuche) und Sammelbestellungen (Arbeitshefte, Kopiergeld, Werkmaterial).
- Erzeugung von Zahlungsinformationen (Elternbrief mit Überweisungs-QR-Code).
- Zuordnung eingehender Zahlungen zu offenen Beiträgen anhand der Kontoauszüge des Schulkontos.
- Führung eines projektbezogenen Kassenbuchs einschließlich Vorkasse, Erstattungen, Spenden und Gebühren,
  Nachweis gegenüber der Kassenprüfung.

Es werden keine Zahlungen ausgelöst und kein Geld verwahrt.

## 2. Rechtsgrundlage

[PLATZHALTER: z. B. Art. 6 Abs. 1 lit. c und e DSGVO i. V. m. § 120 und § 121 SchulG NRW sowie der
VO-DV I; für die Kassenführung haushaltsrechtliche Vorschriften / Runderlass zur Verwaltung von Schulgirokonten.
Für den BuT-/Härtefallstatus ggf. gesonderte Grundlage prüfen.]

## 3. Kategorien betroffener Personen

- Schülerinnen und Schüler
- Eltern bzw. Erziehungsberechtigte und sonstige Zahlende (z. B. Großeltern)
- Lieferanten, Stadtkasse, Spenderinnen und Spender
- Beschäftigte der Schule mit Zugang zur Anwendung (Schulleitung, Sekretariat, Lehrkräfte)

## 4. Kategorien personenbezogener Daten

| Personengruppe | Daten |
|---|---|
| Schülerinnen/Schüler | Vor- und Nachname, Klasse, Schuljahr, Familienzuordnung (Geschwister), Zahlungsstatus je Beitrag, BuT-/Härtefallkennzeichen mit Notiz |
| Zahlende | Name und IBAN laut Kontoauszug, Verwendungszweck, Betrag, Datum; gelernte Zuordnung IBAN → Familie |
| Lieferanten/Sonstige | Name, IBAN, Verwendungszweck, Betrag, Datum |
| Beschäftigte | Benutzername, Anzeigename, Rolle, Passwort-Hash (Argon2id), Zeitpunkt der letzten Anmeldung, Protokoll der Änderungen mit IP-Adresse |

Besonders schutzwürdig ist das BuT-/Härtefallkennzeichen: Es ist nur für die Rollen Kasse und Administration
sichtbar und erscheint in keiner Liste, die an Dritte oder in die Klasse gegeben wird.

## 5. Empfänger

- Intern: Schulleitung, Sekretariat (Rolle Kasse), Administration; Lehrkräfte ausschließlich für Projekte, für die
  sie verantwortlich sind (ohne IBANs, ohne BuT-Kennzeichen).
- Kassenprüfung: Jahresabschluss je Projekt und Kassenbuch als PDF.
- Eltern: nur der eigene Elternbrief in Papierform.
- [PLATZHALTER: Schulträger / Stadtkasse, soweit Erstattungen nachgewiesen werden]
- Keine Übermittlung in Drittländer. Keine externen Dienste, keine Cloud, keine Schnittstellen nach außen.

## 6. Löschfristen

| Daten | Frist |
|---|---|
| Buchungen (Beträge, Daten, Zahlcodes, Zuordnungen), Belege | 10 Jahre [PLATZHALTER: bestätigen] |
| Namen der Kinder, Namen und IBANs der Zahlenden | Anonymisierung [PLATZHALTER: Frist, Vorschlag 2 Jahre] nach Abschluss des letzten betroffenen Projekts und Verlassen der Schule (manueller Anonymisierungslauf, protokolliert) |
| Gelernte Zahler-IBANs | mit der Anonymisierung der Familie; einzeln jederzeit löschbar |
| Login-Versuche | 30 Tage |
| Sitzungen | 8 Stunden nach letzter Aktivität |
| Datensicherungen | [PLATZHALTER: Aufbewahrungsdauer der Backup-Archive, z. B. 30 Tage rollierend] |
| Protokoll | [PLATZHALTER: Frist, i. d. R. wie Buchungen] |

## 7. Technische und organisatorische Maßnahmen

- Betrieb auf einem Server der Schule; Zugriff nur aus dem Schulnetz bzw. über VPN (nginx-Zugriffsliste), TLS-Verschlüsselung.
- Keine Selbstregistrierung; Benutzer werden nur durch die Administration angelegt; Rollenkonzept (admin, kasse, lehrkraft).
- Passwörter mindestens 12 Zeichen, gespeichert als Argon2id-Hash; Sperre nach 10 Fehlversuchen für 15 Minuten
  (je IP und je Konto); automatische Abmeldung nach 8 Stunden Inaktivität.
- Serverseitige Sitzungen, Cookies `Secure`, `HttpOnly`, `SameSite=Lax`; CSRF-Schutz aller schreibenden Formulare;
  Content-Security-Policy ohne externe Quellen.
- Vollständiges Änderungsprotokoll (wer, wann, was, alter/neuer Wert) für Zuordnungen, Posten, Projekte, Kinder,
  Benutzer, Importe und Löschläufe; Zuordnungen werden nie gelöscht, nur storniert.
- Kontoumsätze werden nur lesend aus Dateien übernommen; keine Bankzugangsdaten gespeichert; Import nur für
  hinterlegte Konten.
- Datenminimierung: Zahlcodes ohne personenbezogene Bestandteile; Lehrkräfte sehen keine IBANs.
- Tägliche Sicherung (konsistenter Snapshot), Archive mit Dateirechten 600; [PLATZHALTER: externe Ablage der Sicherungen, Verschlüsselung].
- systemd-Härtung des Dienstes (eigener Systembenutzer, schreibgeschütztes Dateisystem außer Datenverzeichnis).
- [PLATZHALTER: Serverstandort, physischer Zugangsschutz, Patch-Management, Vertretungsregelung]
