#!/usr/bin/env bash
# Update einer mit install.sh eingerichteten Schulkasse; Aufruf siehe README.md.
set -Eeuo pipefail
umask 027

usage() {
    cat <<'EOF'
Schulkasse unter /opt/schulkasse auf den Stand dieses Quellverzeichnisses aktualisieren.

sudo bash deploy/update.sh [--dry-run] [--ja]

--dry-run   Prüfen und Plan zeigen, nichts ändern
--ja        Ohne Rückfrage ausführen (z. B. für Automatisierung)
--help      Diese Hilfe

Ablauf: Pakete vorab bauen (App läuft weiter) -> Dienst stoppen -> Sicherung ->
Code und venv austauschen -> Migrationen -> Dienst starten -> Funktionsprüfung.
Schlägt ein Schritt fehl, wird automatisch auf den vorherigen Stand samt
Datenbank zurückgerollt. Die Vorversion bleibt bis zum nächsten Update in
/opt/schulkasse/.vorversion erhalten. .env, instance/ (Datenbank, Belege,
Briefkonfiguration, Sicherungen) und die nginx-Konfiguration bleiben unverändert.
EOF
}

die() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
finde_python() {  # first interpreter >= 3.12 with venv support
    local kandidat
    for kandidat in python3.13 python3.12 python3; do
        if command -v "$kandidat" >/dev/null \
                && "$kandidat" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            echo "$kandidat"
            return 0
        fi
    done
    return 1
}

dry_run=0; ja=0
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --dry-run) dry_run=1; shift ;;
        --ja) ja=1; shift ;;
        *) die "Unbekannter Parameter: $1" ;;
    esac
done

ziel=/opt/schulkasse
vor=$ziel/.vorversion
units=(schulkasse.service schulkasse-backup.service schulkasse-backup.timer)
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)

# ------------------------------------------------------------------ checks
for file in pyproject.toml requirements.txt .env.example brief.example.toml README.md schulkasse/__init__.py \
            deploy/schulkasse.service deploy/schulkasse-backup.service deploy/schulkasse-backup.timer; do
    [[ -f "$source_dir/$file" ]] || die "Quelldatei fehlt: $file"
done
[[ "$source_dir" != "$ziel" && "$source_dir" != "$ziel/"* ]] \
    || die "Bitte aus einem separaten Quellverzeichnis aufrufen, nicht aus $ziel."
py=$(finde_python) || die 'Python >= 3.12 mit venv-Modul erforderlich (unter Ubuntu 22.04: sudo apt install python3.12-venv).'

version_von() {  # version from a pyproject.toml
    sed -n 's/^version *= *"\(.*\)"/\1/p' "$1" | head -n1
}
quell_hash() {  # fingerprint of the application sources in a directory
    (cd "$1" && find schulkasse requirements.txt pyproject.toml -type f ! -path '*/__pycache__/*' -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
}
neu_version="$(version_von "$source_dir/pyproject.toml") ($(quell_hash "$source_dir"))"

if ((dry_run)) && [[ ! -d $ziel ]]; then
    printf 'Plan: %s -> %s\nNeue Version: %s\nHinweis: %s existiert hier nicht; der echte Lauf bricht dann ab.\n' \
        "$source_dir" "$ziel" "$neu_version" "$ziel"
    exit 0
fi
((EUID == 0)) || die 'Bitte mit sudo ausführen.'
[[ -d /run/systemd/system ]] || die 'systemd erforderlich.'
[[ -d $ziel/schulkasse && -x $ziel/.venv/bin/python && -f $ziel/.env && -d $ziel/instance ]] \
    || die "Keine Installation unter $ziel gefunden. Für die Erstinstallation deploy/install.sh verwenden."
getent passwd schulkasse >/dev/null || die 'Systembenutzer schulkasse fehlt.'
for unit in "${units[@]}"; do
    [[ -f /etc/systemd/system/$unit ]] || die "systemd-Unit fehlt: /etc/systemd/system/$unit"
done

exec 9>/run/lock/schulkasse-update.lock
flock -n 9 || die 'Es läuft bereits ein Update.'

frei_mb=$(df -Pm "$ziel" | awk 'NR==2 {print $4}')
((frei_mb >= 1024)) || die "Zu wenig freier Speicher unter $ziel (${frei_mb} MB, mindestens 1024 MB nötig)."

if [[ -f $ziel/VERSION ]]; then
    alt_version=$(sed -n 's/^version=//p' "$ziel/VERSION")
else
    alt_version="$(version_von "$ziel/pyproject.toml") ($(quell_hash "$ziel"))"
fi

# App port: from .env; older installations had it only in the systemd unit.
port=$(sed -n 's/^SCHULKASSE_PORT=//p' "$ziel/.env" | tail -n1)
port_nachtragen=0
if [[ -z $port ]]; then
    port=$(sed -n 's/.*--bind 127\.0\.0\.1:\([0-9]\+\).*/\1/p' /etc/systemd/system/schulkasse.service | head -n1)
    port=${port:-8010}
    port_nachtragen=1
fi

# Settings that .env.example knows but the installed .env does not (defaults apply).
mapfile -t fehlende_env < <(comm -23 \
    <(grep -oE '^[A-Z_]+=' "$source_dir/.env.example" | tr -d '=' | grep -vx SCHULKASSE_PORT | sort -u) \
    <(grep -oE '^[A-Z_]+=' "$ziel/.env" | tr -d '=' | sort -u))
geaenderte_units=()
for unit in "${units[@]}"; do
    cmp -s "$source_dir/deploy/$unit" "/etc/systemd/system/$unit" || geaenderte_units+=("$unit")
done

printf 'Quelle:            %s\n' "$source_dir"
printf 'Installiert:       %s\n' "$alt_version"
printf 'Neu:               %s\n' "$neu_version"
printf 'Geänderte Units:   %s\n' "${geaenderte_units[*]:-keine}"
printf 'App-Port:          127.0.0.1:%s%s\n' "$port" "$( ((port_nachtragen)) && echo ' (wird in .env eingetragen)')"
if ((${#fehlende_env[@]})); then
    printf 'Hinweis: Neue Einstellungen fehlen in %s/.env (es gelten die Standardwerte): %s\n' \
        "$ziel" "${fehlende_env[*]}"
    printf '         Bei Bedarf aus .env.example übernehmen und den Dienst neu starten.\n'
fi
if ((dry_run)); then
    printf '\nNur geprüft, nichts geändert. Die App ist während des Austauschs kurz nicht erreichbar.\n'
    exit 0
fi
if ((!ja)); then
    [[ -t 0 ]] || die 'Keine Rückfrage möglich: mit --ja bestätigen.'
    read -r -p 'Update jetzt ausführen? Die App ist dabei kurz nicht erreichbar. [j/N] ' antwort
    [[ "$antwort" =~ ^[jJ]$ ]] || die 'Abgebrochen, nichts geändert.'
fi

if ((port_nachtragen)); then
    # The unit now reads the port from .env; keep the port the installation already uses.
    printf '\n# Local port of the app server (added by update.sh)\nSCHULKASSE_PORT=%s\n' "$port" >> "$ziel/.env"
fi

arbeit=$(mktemp -d /var/tmp/schulkasse-update.XXXXXX)
aufraeumen() { rm -rf "$arbeit"; }
trap aufraeumen EXIT
trap 'printf "\nFehler: Vorbereitung fehlgeschlagen (Zeile %s). Die App läuft unverändert weiter.\n" "$LINENO" >&2' ERR

# ---------------------------------------- preparation while the app keeps running
info 'Quellen kopieren und Python-Pakete vorab bauen (App läuft weiter)'
mkdir -p "$arbeit/src"
cp -R "$source_dir/schulkasse" "$arbeit/src/"
find "$arbeit/src" -name __pycache__ -type d -prune -exec rm -rf {} +
for file in pyproject.toml requirements.txt README.md .env.example brief.example.toml; do
    cp "$source_dir/$file" "$arbeit/src/$file"
done
"$py" -m venv "$arbeit/bau"
"$arbeit/bau/bin/pip" install --quiet --upgrade pip
# Build all wheels now so that the downtime step installs offline and cannot
# fail halfway because of network problems.
"$arbeit/bau/bin/pip" wheel --quiet -w "$arbeit/wheels" -r "$arbeit/src/requirements.txt" 'setuptools>=69' wheel
# Building the project itself catches packaging errors (e.g. an invalid version)
# before the app is stopped.
"$arbeit/bau/bin/pip" wheel --quiet --no-deps -w "$arbeit/probe" "$arbeit/src"

# ------------------------------------------------------------ rollback
rollback() {
    trap - ERR INT TERM
    set +e
    printf '\n!! Update fehlgeschlagen - Rückkehr zur Vorversion.\n' >&2
    systemctl stop schulkasse.service
    if [[ -d $vor/schulkasse ]]; then
        rm -rf "$ziel/schulkasse" && mv "$vor/schulkasse" "$ziel/schulkasse"
    fi
    if [[ -d $vor/venv ]]; then
        rm -rf "$ziel/.venv" && mv "$vor/venv" "$ziel/.venv"
    fi
    if [[ -d $vor/egg-info ]]; then
        rm -rf "$ziel/schulkasse.egg-info" && mv "$vor/egg-info" "$ziel/schulkasse.egg-info"
    fi
    for file in pyproject.toml requirements.txt README.md .env.example brief.example.toml VERSION; do
        [[ -f $vor/dateien/$file ]] && cp -p "$vor/dateien/$file" "$ziel/$file"
    done
    # Only a completely copied snapshot may replace the current database.
    if [[ -f $vor/db/.komplett ]]; then
        rm -f "$db" "$db-wal" "$db-shm"
        for f in "$vor/db/"*; do cp -p "$f" "$(dirname "$db")/"; done
    fi
    for unit in "${units[@]}"; do
        [[ -f $vor/units/$unit ]] && cp -p "$vor/units/$unit" "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    systemctl start schulkasse.service schulkasse-backup.timer
    if gesund; then
        printf '!! Vorversion läuft wieder (%s). Datenbank auf den Stand vor dem Update zurückgesetzt.\n' \
            "$alt_version" >&2
    else
        printf '!! Auch die Vorversion startet nicht. Diagnose: journalctl -u schulkasse -n 50 --no-pager\n' >&2
    fi
    printf '!! Ursache siehe Ausgabe oben.\n' >&2
    exit 1
}

gesund() {
    local i
    for i in $(seq 1 30); do
        if systemctl is-active --quiet schulkasse.service \
                && curl --fail --silent --max-time 5 -o /dev/null "http://127.0.0.1:$port/anmelden"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# Database path as configured for the running installation.
db=$(sed -n 's/^SCHULKASSE_DB=//p' "$ziel/.env" | tail -n1)
db=${db:-$ziel/instance/schulkasse.sqlite3}
[[ -f $db ]] || die "Datenbank nicht gefunden: $db"

# ------------------------------------------------------------ downtime
info 'Dienst stoppen und Sicherung erstellen'
cd "$ziel"
systemctl stop schulkasse-backup.timer schulkasse-backup.service schulkasse.service
# The previous .vorversion is replaced before the rollback trap is armed, so a
# rollback can only ever restore what this run saved.
rm -rf "$vor"
install -d -m 0700 "$vor" "$vor/db" "$vor/units" "$vor/dateien"
trap rollback ERR INT TERM
runuser -u schulkasse -- "$ziel/.venv/bin/schulkasse" backup
# The service is stopped, so copying the database files is consistent.
for f in "$db" "$db-wal" "$db-shm"; do
    if [[ -f $f ]]; then cp -p "$f" "$vor/db/"; fi
done
touch "$vor/db/.komplett"
for unit in "${units[@]}"; do
    cp -p "/etc/systemd/system/$unit" "$vor/units/$unit"
done
for file in pyproject.toml requirements.txt README.md .env.example brief.example.toml VERSION; do
    if [[ -f $ziel/$file ]]; then cp -p "$ziel/$file" "$vor/dateien/$file"; fi
done

info 'Code und Python-Umgebung austauschen'
# Venvs are not relocatable: the old one only returns to its original path.
mv "$ziel/schulkasse" "$vor/schulkasse"
mv "$ziel/.venv" "$vor/venv"
if [[ -d $ziel/schulkasse.egg-info ]]; then mv "$ziel/schulkasse.egg-info" "$vor/egg-info"; fi
umask 022
cp -R "$arbeit/src/schulkasse" "$ziel/schulkasse"
for file in pyproject.toml requirements.txt README.md .env.example brief.example.toml; do
    install -m 0644 "$arbeit/src/$file" "$ziel/$file"
done
chmod -R a+rX "$ziel/schulkasse"
"$py" -m venv "$ziel/.venv"
"$ziel/.venv/bin/pip" install --quiet --no-index --find-links "$arbeit/wheels" -r "$ziel/requirements.txt" \
    'setuptools>=69' wheel
"$ziel/.venv/bin/pip" install --quiet --no-index --no-deps --no-build-isolation -e "$ziel"
umask 027

info 'Datenbank migrieren'
# No subshell here: it would run the rollback trap a second time.
umask 077
runuser -u schulkasse -- "$ziel/.venv/bin/schulkasse" migrate
umask 027

info 'systemd-Units aktualisieren und Dienst starten'
for unit in "${units[@]}"; do
    install -m 0644 "$source_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl start schulkasse.service
gesund || { printf 'Die neue Version antwortet nicht.\n' >&2; journalctl -u schulkasse -n 20 --no-pager >&2; false; }
systemctl start schulkasse-backup.timer
trap - ERR INT TERM

printf 'version=%s\naktualisiert=%s\nquelle=%s\n' "$neu_version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$source_dir" > "$ziel/VERSION"
chmod 0644 "$ziel/VERSION"

printf '\nUpdate abgeschlossen: %s -> %s\n' "$alt_version" "$neu_version"
printf '%s\n' "Vorversion und Datenbankstand davor: $vor (bis zum nächsten Update)." \
    "Zusätzliche Sicherung: $ziel/instance/backups" \
    'Diagnose: sudo journalctl -u schulkasse -n 50 --no-pager'
