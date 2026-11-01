#!/usr/bin/env bash
# Deploy from this machine to a server via ssh + rsync (install or update).
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Schulkasse von diesem Rechner aus auf einem Server installieren oder aktualisieren (ssh + rsync).

Erstinstallation:
  deploy/remote.sh install --host benutzer@server --domain kasse.schule.de --email it@schule.de \
      [--port 5015] [--allow 192.168.178.0/24 ...] [--le-staging]

Update:
  deploy/remote.sh update --host benutzer@server [--ja]

Abgebrochene Erstinstallation entfernen (danach install erneut starten):
  deploy/remote.sh aufraeumen --host benutzer@server

Allgemein:
  --host ZIEL        ssh-Ziel, z. B. admin@server.schule.de (Benutzer mit sudo-Rechten oder root)
  --ssh-port PORT    ssh-Port, falls nicht 22
  --ssh-option OPT   zusätzliche ssh-Option, mehrfach möglich (z. B. --ssh-option "-i ~/.ssh/schule")
  --quelle PFAD      Ablage der Projektkopie auf dem Server (Standard: ~/schulkasse-quelle)
  --ohne-tests       lokale Tests vor der Übertragung überspringen
  --dry-run          nur prüfen: Verbindung, Übertragung (ohne Schreiben) und Plan auf dem Server
  --help             diese Hilfe

Nur bei install: --domain, --email, --port, --allow, --le-staging (siehe deploy/install.sh --help).
Nur bei update:  --ja (ohne Rückfrage).

Die Projektkopie wird per rsync auf den Server übertragen, danach läuft dort deploy/install.sh bzw.
deploy/update.sh mit sudo. Passwortabfragen (sudo, Administrator) erscheinen in diesem Terminal.
Nicht übertragen werden: .venv, instance/, .env, Tests-Caches, Git-Daten.
EOF
}

die() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

(($#)) || { usage; exit 1; }
befehl=$1; shift
case "$befehl" in
    install|update|aufraeumen) ;;
    --help|-h|help) usage; exit 0 ;;
    *) die "Unbekannter Befehl: $befehl (install, update oder aufraeumen)" ;;
esac

host=''; ssh_port=''; ssh_optionen=(); quelle='schulkasse-quelle'; tests=1; dry_run=0; ja=0; staging=0
domain=''; email=''; port=''; networks=()
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --ohne-tests) tests=0; shift ;;
        --dry-run) dry_run=1; shift ;;
        --ja) ja=1; shift ;;
        --le-staging) staging=1; shift ;;
        --host|--ssh-port|--ssh-option|--quelle|--domain|--email|--port|--allow)
            (($# >= 2)) && [[ -n "$2" ]] || die "Wert für $1 fehlt."
            case "$1" in
                --host) host=$2 ;;
                --ssh-port) ssh_port=$2 ;;
                --ssh-option) read -r -a teile <<<"$2"; ssh_optionen+=("${teile[@]}") ;;
                --quelle) quelle=$2 ;;
                --domain) domain=$2 ;;
                --email) email=$2 ;;
                --port) port=$2 ;;
                --allow) networks+=("$2") ;;
            esac
            shift 2 ;;
        *) die "Unbekannter Parameter: $1" ;;
    esac
done
[[ -n "$host" ]] || die 'Bitte --host angeben (z. B. admin@server.schule.de).'
[[ "$quelle" =~ ^[A-Za-z0-9._/~-]+$ && "$quelle" != *..* ]] || die 'Ungültiger Pfad für --quelle.'
[[ "$quelle" != /opt/schulkasse* ]] || die '--quelle darf nicht unter /opt/schulkasse liegen.'
if [[ $befehl == install ]]; then
    [[ -n "$domain" ]] || die 'Bitte --domain angeben.'
    [[ -n "$email" ]] || die "Bitte --email für Let's Encrypt angeben."
fi
command -v ssh >/dev/null || die 'ssh ist nicht installiert.'
command -v rsync >/dev/null || die 'rsync ist auf diesem Rechner nicht installiert (sudo apt install rsync).'

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# One shared ssh connection for all steps, so a password is asked only once.
steuer=$(mktemp -d "${TMPDIR:-/tmp}/schulkasse-ssh.XXXXXX")
ssh_cmd=(ssh -o ControlMaster=auto -o "ControlPath=$steuer/%C" -o ControlPersist=120)
[[ -n "$ssh_port" ]] && ssh_cmd+=(-p "$ssh_port")
ssh_cmd+=("${ssh_optionen[@]}")
verbindung_schliessen() { "${ssh_cmd[@]}" -O exit "$host" 2>/dev/null || true; rm -rf "$steuer"; }
trap verbindung_schliessen EXIT

# ------------------------------------------------------------- local tests
if ((tests)) && [[ $befehl != aufraeumen ]]; then
    if [[ -x "$source_dir/.venv/bin/python" ]] && "$source_dir/.venv/bin/python" -c 'import pytest' 2>/dev/null; then
        info 'Tests lokal ausführen'
        (cd "$source_dir" && .venv/bin/python -m pytest -q) || die 'Tests fehlgeschlagen – nichts übertragen.'
    else
        printf 'Hinweis: Keine lokale Testumgebung (.venv mit pytest) gefunden, Tests übersprungen.\n'
    fi
fi

# --------------------------------------------------------------- the server
info "Verbindung zu $host prüfen"
# shellcheck disable=SC2016
pruefung='command -v rsync >/dev/null || { echo "rsync fehlt auf dem Server (sudo apt install rsync)"; exit 3; }
if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null; then echo "sudo fehlt auf dem Server"; exit 4; fi
echo "ok: $(hostname) $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"'
"${ssh_cmd[@]}" "$host" "$pruefung" || die "Server nicht bereit (siehe Meldung oben)."

info "Projekt nach $host:$quelle übertragen"
rsync_args=(-az --delete --human-readable --stats
            --exclude=/.venv/ --exclude=/instance/ --exclude=/.env --exclude=/.git/ --exclude='*.egg-info/'
            --exclude=.pytest_cache/ --exclude=__pycache__/ --exclude='*.pyc')
((dry_run)) && rsync_args+=(--dry-run)
((dry_run)) || "${ssh_cmd[@]}" "$host" "mkdir -p $quelle"
rsync "${rsync_args[@]}" -e "$(printf '%q ' "${ssh_cmd[@]}")" "$source_dir/" "$host:$quelle/" | tail -n 3

# Remote command, every argument quoted for the remote shell.
if [[ $befehl == install ]]; then
    fern=(bash "$quelle/deploy/install.sh" --domain "$domain" --email "$email")
    [[ -n "$port" ]] && fern+=(--port "$port")
    for net in "${networks[@]}"; do fern+=(--allow "$net"); done
    ((staging)) && fern+=(--le-staging)
elif [[ $befehl == aufraeumen ]]; then
    fern=(bash "$quelle/deploy/install.sh" --aufraeumen)
    ((ja)) && fern+=(--ja)
else
    fern=(bash "$quelle/deploy/update.sh")
    ((ja)) && fern+=(--ja)
fi
((dry_run)) && fern+=(--dry-run)
# ~ must stay unquoted so that the remote shell expands it.
befehlszeile=$(printf '%q ' "${fern[@]}" | sed "s|'~/|~/|g; s|\\\\~/|~/|g")
if ((dry_run)) && [[ $befehl == install ]]; then
    info 'Plan auf dem Server (ohne Änderungen)'
    "${ssh_cmd[@]}" "$host" "cd ~ && if [ -f $quelle/deploy/install.sh ]; then $befehlszeile; else echo 'Projektkopie ist noch nicht auf dem Server – der Plan wird nach der ersten Übertragung angezeigt.'; fi"
    exit 0
fi
case "$befehl" in
    install) info "Installation auf $host" ;;
    update) info "Update auf $host" ;;
    aufraeumen) info "Aufräumen auf $host" ;;
esac
sudo_prefix='sudo'
"${ssh_cmd[@]}" -t "$host" "cd ~ && if [ \"\$(id -u)\" -eq 0 ]; then $befehlszeile; else $sudo_prefix $befehlszeile; fi"
