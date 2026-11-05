# Schulkasse: Containervertrag

Python 3.12.12/amd64, Flask 3.1.3, WeasyPrint 70.0, Gunicorn 26.2.0 aus Hash-Lockfiles
(`requirements.lock`); Basisimage mit Digest. Läuft schreibgeschützt als UID/GID 10001
ohne Capabilities; beschreibbar sind `/data` und `/tmp`.

## Eigenständiger Start

```sh
python3 -c "import secrets; print(secrets.token_hex(32))" > .secret-key && chmod 600 .secret-key
export SCHULKASSE_SECRET_FILE="$PWD/.secret-key"
docker compose build
docker compose run --rm app python -m schulkasse migrate
docker compose run --rm app python -m schulkasse create-admin    # persönliches Konto; 2FA beim ersten Login
docker compose up -d --no-build
```

## Der Schlüssel

`SCHULKASSE_SECRET_KEY` (oder `SCHULKASSE_SECRET_KEY_FILE`) signiert nicht nur Sitzungen,
er **verschlüsselt auch die Zwei-Faktor-Geheimnisse**. Er muss über Neustarts, Updates
und einen Umzug hinweg gleich bleiben; ein neuer Schlüssel macht alle Authenticator-
Einträge unbrauchbar (Ausweg: `schulkasse reset-2fa BENUTZER` je Konto).

## Schema und Migration

Im Container (`SCHULKASSE_AUTO_MIGRATE=0`) wird beim Start nichts migriert; nativ bleibt
der bisherige Ablauf (Migration beim Start) erhalten. `schulkasse migrate` sperrt, wendet
die nummerierten SQL-Migrationen an, prüft die Integrität und legt Dateien mit umask 077
an. Eine Datenbank mit unbekannten (neueren) Migrationen wird ohne Änderung abgewiesen.

- `/health/live`: Prozess antwortet.
- `/health/ready`: alle mitgelieferten Migrationen angewendet, keine unbekannten; sonst 503.

## Proxy und Cookies

`SCHULKASSE_PROXY_IPS` nennt die Adressen des Reverse-Proxys; nur von dort werden
`X-Forwarded-For`/`-Proto` übernommen (Login-Sperre je Client). Ohne diese Angabe gilt
nativ weiter `SCHULKASSE_HINTER_PROXY`. `SCHULKASSE_COOKIE_SECURE=1` hinter HTTPS.

## Daten

`/data/schulkasse.sqlite3` (WAL), `/data/belege/`, optional `/data/brief.toml` samt Logo.
Die app-eigene Sicherung (`schulkasse backup`) wird im Container nicht gebraucht; die
Plattform sichert das Datenvolume.
