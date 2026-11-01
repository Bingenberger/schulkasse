#!/usr/bin/env bash
# Erstinstallation auf dem Server; normalerweise über deploy/remote.sh aufgerufen, siehe README.md.
set -Eeuo pipefail
umask 027

usage() {
    cat <<'EOF'
Schulkasse auf Ubuntu/Debian mit systemd und nginx installieren (Python >= 3.12).

sudo bash deploy/install.sh --domain kasse.schule.de --email it@schule.de [--port 5015] \
  [--allow 192.168.178.0/24 ...] [--le-staging]

--domain HOST    DNS-Name der App; muss bereits auf diesen Server zeigen
--email ADRESSE  Kontaktadresse für Let's Encrypt (Ablaufwarnungen)
--port PORT      Lokaler Port der App hinter nginx (Standard 5015, nur 127.0.0.1)
--allow NETZ     Nur diese IPs/Netze dürfen die App aufrufen; mehrfach möglich.
                 Ohne --allow ist die App aus dem ganzen Internet erreichbar (Anmeldung mit 2FA).
--cert/--key     Statt Let's Encrypt ein vorhandenes Zertifikat samt Schlüssel verwenden
--le-staging     Test-Zertifikat von Let's Encrypt (für Probeläufe, Browser warnen)
--dry-run        Parameter prüfen und Plan zeigen, nichts ändern
--aufraeumen     Eine abgebrochene Erstinstallation entfernen (nur ohne angelegte Benutzer),
                 danach kann die Installation neu gestartet werden. Mit --ja ohne Rückfrage.
--help           Diese Hilfe

Installiert nach /opt/schulkasse. Bestehende Installationen werden abgelehnt.
Ein bereits laufendes nginx wird mitgenutzt: Es kommt nur eine eigene Site-Datei hinzu.
Das Zertifikat wird per HTTP-Prüfung (Port 80) beantragt und von certbot automatisch erneuert.
Das Administrator-Passwort wird interaktiv abgefragt.
EOF
}

die() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
finde_python() {  # first interpreter >= 3.12 (venv support is installed as a package afterwards)
    local kandidat
    for kandidat in python3.13 python3.12 python3; do
        if command -v "$kandidat" >/dev/null \
                && "$kandidat" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            echo "$kandidat"
            return 0
        fi
    done
    return 1
}
domain=''; email=''; cert=''; key=''; port=5015; dry_run=0; staging=0; aufraeumen=0; ja=0; networks=()
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --dry-run) dry_run=1; shift ;;
        --le-staging) staging=1; shift ;;
        --aufraeumen) aufraeumen=1; shift ;;
        --ja) ja=1; shift ;;
        --domain|--email|--cert|--key|--allow|--port)
            (($# >= 2)) && [[ -n "$2" ]] || die "Wert für $1 fehlt."
            case "$1" in
                --domain) domain=$2 ;;
                --email) email=$2 ;;
                --cert) cert=$2 ;;
                --key) key=$2 ;;
                --port) port=$2 ;;
                --allow) networks+=("$2") ;;
            esac
            shift 2 ;;
        *) die "Unbekannter Parameter: $1" ;;
    esac
done
site=/etc/nginx/sites-available/schulkasse
units=(schulkasse.service schulkasse-backup.service schulkasse-backup.timer)

if ((aufraeumen)); then
    ((EUID == 0)) || die 'Bitte mit sudo ausführen.'
    db=/opt/schulkasse/instance/schulkasse.sqlite3
    if [[ -f $db ]]; then
        # Safety net: a finished installation (with users) is never removed by this switch.
        benutzer=$(python3 -c 'import sqlite3, sys
try:
    print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM benutzer").fetchone()[0])
except sqlite3.Error:
    print(0)' "$db")
        ((benutzer == 0)) || die "Die Installation enthält bereits $benutzer Benutzer – das ist keine abgebrochene
       Erstinstallation. Aufräumen verweigert; für Änderungen deploy/update.sh verwenden."
    fi
    vorhanden=()
    for pfad in /opt/schulkasse "$site" /etc/nginx/sites-enabled/schulkasse "${units[@]/#//etc/systemd/system/}"; do
        if [[ -e $pfad || -L $pfad ]]; then vorhanden+=("$pfad"); fi
    done
    getent passwd schulkasse >/dev/null && vorhanden+=('Systembenutzer schulkasse')
    if ((${#vorhanden[@]} == 0)); then
        echo 'Nichts aufzuräumen – es gibt keine Reste einer Installation.'
        exit 0
    fi
    printf 'Wird entfernt:\n'; printf '  %s\n' "${vorhanden[@]}"
    printf 'Bleibt erhalten: Zertifikate unter /etc/letsencrypt (werden beim nächsten Lauf wiederverwendet),\n'
    printf '  installierte Systempakete, andere nginx-Sites.\n'
    if ((!ja)); then
        [[ -t 0 ]] || die 'Keine Rückfrage möglich: mit --ja bestätigen.'
        read -r -p 'Jetzt entfernen? [j/N] ' antwort
        [[ "$antwort" =~ ^[jJ]$ ]] || die 'Abgebrochen, nichts geändert.'
    fi
    systemctl disable --now "${units[@]}" 2>/dev/null || true
    for unit in "${units[@]}"; do rm -f "/etc/systemd/system/$unit"; done
    systemctl daemon-reload
    rm -f /etc/nginx/sites-enabled/schulkasse "$site" "$site.neu"
    if command -v nginx >/dev/null && nginx -t 2>/dev/null; then systemctl reload nginx || true; fi
    rm -rf /opt/schulkasse
    if getent passwd schulkasse >/dev/null; then userdel schulkasse; fi
    if getent group schulkasse >/dev/null; then groupdel schulkasse; fi
    echo 'Aufgeräumt. Die Installation kann jetzt neu gestartet werden.'
    exit 0
fi

[[ -n "$domain" ]] || { usage >&2; die 'Bitte --domain angeben.'; }
if [[ -n "$cert$key" ]]; then
    [[ -n "$cert" && -n "$key" ]] || die '--cert und --key nur gemeinsam angeben.'
    modus=vorhanden
else
    [[ -n "$email" ]] || die "Bitte --email für Let's Encrypt angeben (oder --cert/--key)."
    modus=letsencrypt
fi
[[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1024 && port <= 65535)) || die 'Der Port muss zwischen 1024 und 65535 liegen.'
command -v python3 >/dev/null || die 'Python 3 muss vorinstalliert sein.'
python3 - "$domain" "$email" "${networks[@]}" <<'PY'
import ipaddress
import re
import sys
host, mail = sys.argv[1], sys.argv[2]
if len(host) > 253 or "." not in host or not all(
        re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in host.split('.')):
    sys.exit('Ungültiger DNS-Name: ' + host)
if mail and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', mail):
    sys.exit('Ungültige E-Mail-Adresse: ' + mail)
for net in sys.argv[3:]:
    try:
        ipaddress.ip_network(net, strict=False)
    except ValueError:
        sys.exit('Ungültige IP/CIDR: ' + net)
PY
for path in "$cert" "$key"; do
    [[ -z "$path" || "$path" =~ ^/[a-zA-Z0-9_./-]+$ ]] || die 'TLS-Pfade müssen absolut und ohne Leer- oder Sonderzeichen sein.'
done
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for file in pyproject.toml requirements.txt .env.example brief.example.toml deploy/schulkasse.service \
            deploy/schulkasse-backup.service deploy/schulkasse-backup.timer; do
    [[ -f "$source_dir/$file" ]] || die "Quelldatei fehlt: $file"
done
[[ "$source_dir" != /opt/schulkasse && "$source_dir" != /opt/schulkasse/* ]] \
    || die 'Bitte aus einer Projektkopie außerhalb von /opt/schulkasse aufrufen.'
if ((dry_run)); then
    printf 'Plan: %s -> /opt/schulkasse\nAdresse: https://%s (nginx -> 127.0.0.1:%s)\n' "$source_dir" "$domain" "$port"
    if [[ $modus == letsencrypt ]]; then
        printf "Zertifikat: Let's Encrypt für %s, Kontakt %s%s\n" "$domain" "$email" "$( ((staging)) && echo ' (Test-Zertifikat)')"
    else
        printf 'Zertifikat: %s / %s\n' "$cert" "$key"
    fi
    if ((${#networks[@]})); then printf 'Erlaubtes Netz: %s\n' "${networks[@]}"; else echo 'Zugriff: aus dem Internet (keine IP-Beschränkung)'; fi
    echo 'Nur Parameter geprüft; Server, DNS und Dienste werden beim echten Lauf geprüft.'
    exit 0
fi

# ------------------------------------------------------------------ checks
((EUID == 0)) || die 'Bitte mit sudo ausführen.'
[[ -t 0 ]] || die 'Ein Terminal für die Administrator-Einrichtung ist erforderlich (ssh -t).'
[[ -f /etc/debian_version && -d /run/systemd/system ]] || die 'Debian/Ubuntu mit laufendem systemd erforderlich.'
# Python >= 3.12: system Python (Ubuntu 24.04, Debian 13) or, on older Ubuntu, from the deadsnakes PPA.
py_ppa=0
if ! py=$(finde_python); then
    if grep -qx 'ID=ubuntu' /etc/os-release; then
        py=python3.12
        py_ppa=1
    else
        die 'Python >= 3.12 erforderlich (z. B. Ubuntu 24.04 oder Debian 13).'
    fi
fi
[[ ! -e /opt/schulkasse && ! -L /opt/schulkasse ]] || die '/opt/schulkasse existiert bereits. Für Updates deploy/update.sh verwenden.'
for path in "$site" /etc/nginx/sites-enabled/schulkasse /etc/systemd/system/schulkasse.service \
            /etc/systemd/system/schulkasse-backup.service /etc/systemd/system/schulkasse-backup.timer; do
    [[ ! -e "$path" && ! -L "$path" ]] || die "Konfiguration existiert bereits: $path"
done
if getent passwd schulkasse >/dev/null || getent group schulkasse >/dev/null; then
    die 'Benutzer oder Gruppe schulkasse existiert bereits; bitte vorhandene Einrichtung prüfen.'
fi
if command -v ss >/dev/null && [[ -n "$(ss -Htln "sport = :$port")" ]]; then
    die "Port $port ist auf diesem Server bereits belegt. Bitte mit --port einen freien Port wählen."
fi
if [[ -d /etc/nginx ]] && grep -rqsE "server_name[^;]*[[:space:]]${domain//./\\.}[[:space:];]" /etc/nginx/sites-enabled /etc/nginx/conf.d; then
    die "In nginx gibt es bereits eine Site für $domain. Bitte zuerst entfernen oder eine andere Domain wählen."
fi
if [[ $modus == vorhanden ]]; then
    [[ -r "$cert" && -r "$key" ]] || die 'Zertifikat oder Schlüssel nicht lesbar.'
    openssl x509 -in "$cert" -noout -checkend 0 || die 'Zertifikat ungültig oder abgelaufen.'
    # -checkhost exits 0 even on mismatch, so the printed verdict has to be checked.
    openssl x509 -in "$cert" -noout -checkhost "$domain" | grep -q ' does match certificate' \
        || die 'Zertifikat passt nicht zur Domain.'
    [[ "$(openssl x509 -in "$cert" -pubkey -noout)" == "$(openssl pkey -in "$key" -passin pass: -pubout)" ]] \
        || die 'Zertifikat und Schlüssel passen nicht zusammen.'
else
    getent ahosts "$domain" >/dev/null || die "Der DNS-Name $domain ist nicht auflösbar. Bitte zuerst den DNS-Eintrag anlegen."
fi
trap 'printf "\nInstallation fehlgeschlagen (Zeile %s). Bereits angelegte Dateien bleiben zur Prüfung erhalten.\n%s\n" \
    "$LINENO" "Vor dem Anlegen des Administrators: deploy/remote.sh aufraeumen --host ..., dann erneut installieren.
Danach (App und Administrator vorhanden): Ursache beheben, fehlende Schritte mit deploy/remote.sh update nachholen." >&2' ERR

# ---------------------------------------------------------------- packages
info 'Systempakete installieren'
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
if ((py_ppa)); then
    # Separate interpreter next to the system Python; the system's python3 stays unchanged.
    info 'Python 3.12 aus dem deadsnakes-PPA installieren (System-Python bleibt unverändert)'
    apt-get install -y -q software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -q
    pakete=(python3.12 python3.12-venv python3.12-dev)
else
    pakete=(python3-venv python3-dev)
    [[ $py == python3 ]] || pakete+=("$py-venv" "$py-dev")
fi
pakete+=(build-essential libffi-dev libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core
        nginx curl openssl)
if [[ $modus == letsencrypt ]]; then pakete+=(certbot); fi
apt-get install -y -q "${pakete[@]}"
systemctl enable --now nginx
nginx -t

ipv6=0
if [[ -f /proc/net/if_inet6 ]]; then ipv6=1; fi
listen() {  # listen directives for IPv4 and, if available, IPv6
    printf '    listen %s;\n' "$1"
    if ((ipv6)); then printf '    listen [::]:%s;\n' "$1"; fi
}

# ------------------------------------------------------------- certificate
acme=/var/www/schulkasse-acme
if [[ $modus == letsencrypt ]]; then
    info "Zertifikat bei Let's Encrypt anfordern"
    # nginx (www-data) must be able to read the challenge files.
    install -d -m 0755 "$acme" "$acme/.well-known" "$acme/.well-known/acme-challenge"
    # Temporary site: answers the HTTP challenge, nothing else yet.
    {
        echo 'server {'
        listen 80
        echo "    server_name $domain;"
        echo "    location /.well-known/acme-challenge/ { root $acme; }"
        echo '    location / { return 404; }'
        echo '}'
    } > "$site"
    ln -s "$site" /etc/nginx/sites-enabled/schulkasse
    nginx -t && systemctl reload nginx
    # Self-test before asking Let's Encrypt: is the challenge path served by nginx for this domain?
    probe=$(openssl rand -hex 12)
    (umask 022; echo "$probe" > "$acme/.well-known/acme-challenge/$probe")
    antwort=''
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        antwort=$(curl -s --max-time 3 -H "Host: $domain" "http://127.0.0.1/.well-known/acme-challenge/$probe" || true)
        [[ "$antwort" == "$probe" ]] && break
        sleep 1
    done
    rm -f "$acme/.well-known/acme-challenge/$probe"
    if [[ "$antwort" != "$probe" ]]; then
        rm -f /etc/nginx/sites-enabled/schulkasse "$site"
        systemctl reload nginx || true
        trap - ERR
        die "nginx liefert den Prüfpfad für $domain lokal nicht aus. Bitte die nginx-Konfiguration prüfen
       (z. B. eine andere Site mit default_server, die alles abfängt). Die temporäre Site wurde entfernt."
    fi
    certbot_args=(certonly --webroot -w "$acme" -d "$domain" --email "$email" --agree-tos --non-interactive
                  --keep-until-expiring --deploy-hook 'systemctl reload nginx')
    if ((staging)); then certbot_args+=(--test-cert); fi
    if ! (umask 022; certbot "${certbot_args[@]}"); then
        rm -f /etc/nginx/sites-enabled/schulkasse "$site"
        systemctl reload nginx || true
        trap - ERR
        die "Das Zertifikat konnte nicht ausgestellt werden. Prüfen: Zeigt $domain auf diesen Server, ist Port 80
       aus dem Internet erreichbar (Firewall/Router)? Die temporäre nginx-Site wurde entfernt; einfach erneut starten."
    fi
    cert=/etc/letsencrypt/live/$domain/fullchain.pem
    key=/etc/letsencrypt/live/$domain/privkey.pem
fi

# ------------------------------------------------------------- application
info 'Anwendung installieren'
useradd --system --user-group --home-dir /opt/schulkasse --shell /usr/sbin/nologin schulkasse
install -d -m 0755 /opt/schulkasse
# Copy only application sources; never copy a local database, .env or venv.
cp -R "$source_dir/schulkasse" /opt/schulkasse/
find /opt/schulkasse/schulkasse -name __pycache__ -type d -prune -exec rm -rf {} +
for file in pyproject.toml requirements.txt README.md .env.example brief.example.toml; do
    install -m 0644 "$source_dir/$file" "/opt/schulkasse/$file"
done
chmod -R a+rX /opt/schulkasse/schulkasse
install -d -m 0700 -o schulkasse -g schulkasse /opt/schulkasse/instance
install -m 0600 -o schulkasse -g schulkasse "$source_dir/brief.example.toml" /opt/schulkasse/instance/brief.toml
secret=$(openssl rand -hex 32)
sed -e "s/^SCHULKASSE_SECRET_KEY=.*/SCHULKASSE_SECRET_KEY=$secret/" -e "s/^SCHULKASSE_PORT=.*/SCHULKASSE_PORT=$port/" \
    "$source_dir/.env.example" > /opt/schulkasse/.env
grep -q '^SCHULKASSE_PORT=' /opt/schulkasse/.env || printf 'SCHULKASSE_PORT=%s\n' "$port" >> /opt/schulkasse/.env
unset secret
chown schulkasse:schulkasse /opt/schulkasse/.env
chmod 0600 /opt/schulkasse/.env
# The service user must be able to read and execute the root-owned venv.
umask 022
"$py" -c 'import ensurepip' 2>/dev/null || die "Für $py fehlt das venv-Modul (Paket $py-venv)."
"$py" -m venv /opt/schulkasse/.venv
/opt/schulkasse/.venv/bin/pip install -q --upgrade pip
/opt/schulkasse/.venv/bin/pip install -q -r /opt/schulkasse/requirements.txt
/opt/schulkasse/.venv/bin/pip install -q --no-deps -e /opt/schulkasse
umask 027
cd /opt/schulkasse
# Database files readable by the service user only (like the service's UMask=0077).
umask 077
runuser -u schulkasse -- .venv/bin/schulkasse migrate
info 'Ersten Administrator anlegen'
runuser -u schulkasse -- .venv/bin/schulkasse create-admin
umask 027
for unit in schulkasse.service schulkasse-backup.service schulkasse-backup.timer; do
    install -m 0644 "$source_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now schulkasse.service schulkasse-backup.timer
for _ in $(seq 1 30); do
    curl --fail --silent --max-time 3 -o /dev/null "http://127.0.0.1:$port/anmelden" && break
    sleep 1
done
curl --fail --silent --show-error --max-time 5 -o /dev/null "http://127.0.0.1:$port/anmelden" \
    || { journalctl -u schulkasse -n 20 --no-pager >&2; false; }

# ------------------------------------------------------------------ nginx
info 'nginx einrichten'
{
    echo '# Schulkasse - generated by deploy/install.sh'
    echo 'server {'
    listen 80
    echo "    server_name $domain;"
    echo "    location /.well-known/acme-challenge/ { root $acme; }"
    echo '    location / { return 301 https://$host$request_uri; }'
    echo '}'
    echo 'server {'
    listen '443 ssl'
    echo "    server_name $domain;"
    echo "    ssl_certificate $cert;"
    echo "    ssl_certificate_key $key;"
    echo '    ssl_protocols TLSv1.2 TLSv1.3;'
    echo '    client_max_body_size 20m;'
    for net in "${networks[@]}"; do printf '    allow %s;\n' "$net"; done
    if ((${#networks[@]})); then echo '    deny all;'; fi
    cat <<'EOF'
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options DENY always;
    location /static/ {
        alias /opt/schulkasse/schulkasse/static/;
        expires 7d;
    }
    location / {
EOF
    echo "        proxy_pass http://127.0.0.1:$port;"
    cat <<'EOF'
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_read_timeout 120s;
    }
}
EOF
} > "$site.neu"
mv "$site.neu" "$site"
[[ -L /etc/nginx/sites-enabled/schulkasse ]] || ln -s "$site" /etc/nginx/sites-enabled/schulkasse
nginx -t
systemctl reload nginx
# Check the whole chain nginx -> app locally (the certificate name is checked by browsers, not here).
# "systemctl reload" returns before nginx has switched to the new configuration, so wait for it.
status=''
for _ in $(seq 1 20); do
    status=$(curl --silent --insecure --max-time 5 -o /dev/null -w '%{http_code}' \
        --resolve "$domain:443:127.0.0.1" "https://$domain/anmelden" || true)
    [[ $status == 200 ]] && break
    sleep 1
done
[[ $status == 200 ]] || { printf 'nginx antwortet für %s mit HTTP %s statt 200.\n' "$domain" "$status" >&2; false; }
systemctl start schulkasse-backup.service
version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' /opt/schulkasse/pyproject.toml | head -n1)
printf 'version=%s\ninstalliert=%s\nquelle=%s\n' "$version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$source_dir" \
    > /opt/schulkasse/VERSION
chmod 0644 /opt/schulkasse/VERSION

printf '\nInstallation abgeschlossen: https://%s\n' "$domain"
printf '%s\n' "App: 127.0.0.1:$port (systemd: schulkasse), nginx-Site: $site" \
    'Nächste Schritte: anmelden, Authenticator-App einrichten (2FA ist Pflicht), Konten und Schuljahr anlegen,' \
    '  Briefkopf in /opt/schulkasse/instance/brief.toml anpassen.' \
    'Die .env mit dem Verschlüsselungsschlüssel separat sicher aufbewahren. Server-Uhrzeit synchronisieren (NTP).' \
    'Backups: /opt/schulkasse/instance/backups (täglich 02:30 Uhr; zusätzlich extern sichern).' \
    'Diagnose: sudo journalctl -u schulkasse -n 50 --no-pager'
if [[ $modus == letsencrypt ]]; then
    echo 'Zertifikat: wird von certbot automatisch erneuert (systemctl list-timers | grep certbot).'
fi
if ((${#networks[@]} == 0)); then
    echo 'Hinweis: Ohne --allow ist die App aus dem Internet erreichbar (Schutz: Passwort + 2FA).'
fi
