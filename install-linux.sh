#!/usr/bin/env bash
# install-linux.sh — installer + manager for the Zcode Gateway on a Linux VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/B3hnamR/AutoZcoder/master/install-linux.sh | sudo bash
#
# First run INSTALLS the gateway and registers the `autozcoder` command.
# Afterwards just type `autozcoder` for the manager menu:
#
#   autozcoder                 interactive menu
#   autozcoder status          service / certificate / URL at a glance
#   autozcoder start|stop|restart
#   autozcoder logs            follow the gateway log (Ctrl+C returns)
#   autozcoder info            dashboard URL + admin/client keys
#   autozcoder update          newer release (SHA-256 verified), config kept
#   autozcoder cert            re-issue / switch the TLS certificate
#   autozcoder install         (re)run the full install
#   autozcoder uninstall       FULLY delete service, certificates, files
#
# The gateway serves HTTPS ITSELF (built-in TLS) — no nginx / reverse proxy is
# installed, so nothing else on the VPS is touched or interrupted. Port 80 is
# only used for ~10 seconds while Let's Encrypt validates the certificate.
#
# The install, in order (each step verified before the next):
#   1. root + OS + arch check
#   2. small prerequisites (curl, openssl, socat)
#   3. binary: GitHub release (SHA-256 verified) or a local source build
#   4. config.yaml with random admin/client keys (an existing one is kept)
#   5. the account generator's environment: a private Python venv with its
#      packages (playwright, ddddocr, opencv, …) and Chromium + the system
#      libraries it needs, so the dashboard never shows "Environment Setup";
#      plus Node.js 22 + wrangler for the Cloudflare mail worker
#   6. firewall check, then TLS. DEFAULT = Let's Encrypt certificate for the
#      server's bare IP (shortlived profile, auto-renewed by a systemd timer),
#      so the dashboard opens at https://<ip>:8443/dash with no warning.
#      Other modes: domain, self-signed, none (gateway stays on 127.0.0.1).
#   7. systemd service `zcodegateway` + certificate renewal timer
#   8. health checks of the gateway AND the public https URL
#   9. a final install report (also saved to <install dir>/install-report.txt)
#
# Package-manager / pip / build output is hidden: the screen shows what is
# being done, and everything is written to /var/log/autozcoder-install.log.
#
# Environment overrides (non-interactive use):
#   ZGW_PORT=8443             public HTTPS port
#   ZGW_SSL_MODE=ip|domain|self|none      (default ip)
#   ZGW_DOMAIN=example.com    (for mode=domain)
#   ZGW_SERVER_IP=1.2.3.4     (for mode=ip / display)
#   ZGW_INSTALL_DIR=/opt/zcodegateway
#   ZGW_SKIP_SYSTEMD=1        do not create/start the service (manual runs)
#   ZGW_NONINTERACTIVE=1      never prompt (also implied without a terminal)
#   ZGW_OPEN_FIREWALL=1       open 80 + the HTTPS port in ufw/firewalld
#   ZGW_FROM_SOURCE=/path     build the binary from a checkout of this repo
#                             (scripts/build/linux.sh) instead of downloading a release
#   ZGW_SKIP_GENERATOR=1      skip the generator environment (venv + Chromium, ~250 MB)
#   ZGW_ALLOW_LOW_RESOURCES=1 accept the first-install CPU/RAM warning explicitly
#   ZGW_PAUSE_UNITS="a b"     systemd units that hold port 80; they are stopped
#                             for a few seconds while Let's Encrypt validates
#                             (install and every renewal), then started again
#   ZGW_RUN_AS_ROOT=1         run the service as root instead of the unprivileged
#                             'autozcoder' user (not recommended)

set -Eeuo pipefail
umask 077

red='\033[0;31m'; green='\033[0;32m'; blue='\033[0;34m'
yellow='\033[0;33m'; bold='\033[1m'; dim='\033[2m'; plain='\033[0m'

ZGW_REPO="B3hnamR/AutoZcoder"
# acme.sh is pinned to a release tarball with a known SHA-256 (never `curl | sh`
# of whatever the host serves today); bump both together after reviewing a release
ACME_VERSION="3.1.6"
ACME_SHA256="0d3f9000ac44a6331314742a88c475f79134e24fc991997883652adc59efc486"
SVC_USER="autozcoder"
ENV_FILE="/etc/autozcoder.env"
MANAGER_BIN="/usr/local/bin/autozcoder"
SERVICE="zcodegateway"
RENEW_UNIT="autozcoder-renew"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
RENEW_SERVICE_FILE="/etc/systemd/system/${RENEW_UNIT}.service"
RENEW_TIMER_FILE="/etc/systemd/system/${RENEW_UNIT}.timer"
UPDATE_LOCK="/run/autozcoder-update.lock"
LOG_FILE="/var/log/autozcoder-install.log"
ACME_HOME="/root/.acme.sh"
# absolute path of this file (empty under `curl | bash`), captured before any cd
SELF="$(readlink -f "${BASH_SOURCE[0]:-}" 2>/dev/null || true)"

# never let the package manager prompt or print restart banners
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

ok()   { echo -e "${green}[✓]${plain} $*"; }
warn() { echo -e "${yellow}[!]${plain} $*"; }
bad()  { echo -e "${red}[✗]${plain} $*" >&2; }
fail() { bad "$*"; exit 1; }
step() { STEP="$1"; echo -e "\n${blue}${bold}==>${plain}${bold} [$1] $2${plain}"; }
say()  { echo -e "  ${dim}•${plain} $*"; }

# Run a noisy command with its output going to the log file; the screen only
# shows what is being done (spinner on a terminal) and the result.
run_quiet() { # run_quiet "what we are doing" cmd args...
    local what="$1"; shift
    local t0=$SECONDS rc=0 pid i=0 spin='|/-\' tty=0
    [[ -t 1 ]] && tty=1
    { echo; echo "=== $(date -u '+%F %T') ${what}: $*"; } >>"$LOG_FILE"
    ( trap - ERR; "$@" ) >>"$LOG_FILE" 2>&1 &
    pid=$!
    if [[ "$tty" == "0" ]]; then echo -e "  ${dim}…${plain} ${what}"; fi
    while kill -0 "$pid" 2>/dev/null; do
        if [[ "$tty" == "1" ]]; then
            printf '\r  %s %s (%ss)\033[K' "${spin:i++%4:1}" "$what" "$((SECONDS - t0))"
        fi
        sleep 0.3
    done
    wait "$pid" || rc=$?
    [[ "$tty" == "1" ]] && printf '\r\033[K'
    if [[ $rc -eq 0 ]]; then
        echo -e "  ${green}✓${plain} ${what} ${dim}($((SECONDS - t0))s)${plain}"
        return 0
    fi
    echo -e "  ${red}✗${plain} ${what} ${dim}(exit ${rc})${plain}" >&2
    echo -e "${dim}    last lines of ${LOG_FILE}:${plain}" >&2
    tail -n 12 "$LOG_FILE" | sed 's/^/      /' >&2
    return "$rc"
}

# ------------------------------------------------------------ state / config
# precedence: environment override > saved state (/etc/autozcoder.env) > default
resolve_config() {
    SAVED_INSTALL_DIR=""; SAVED_PORT=""; SAVED_DOMAIN=""; SAVED_SERVER_IP=""
    SAVED_SCHEME=""; SAVED_SSL_MODE=""; SAVED_ACME_HOST="__legacy__"; SAVED_PAUSE_UNITS=""; SAVED_FROM_SOURCE=""; SAVED_RUN_AS_ROOT=""; SAVED_SKIP_SYSTEMD=""
    # shellcheck disable=SC1090
    [[ -f "$ENV_FILE" ]] && . "$ENV_FILE"
    # the service runs as an unprivileged user unless the operator opts out
    ZGW_RUN_AS_ROOT="${ZGW_RUN_AS_ROOT:-${SAVED_RUN_AS_ROOT:-0}}"
    ZGW_SKIP_SYSTEMD="${ZGW_SKIP_SYSTEMD:-${SAVED_SKIP_SYSTEMD:-0}}"
    ZGW_INSTALL_DIR="${ZGW_INSTALL_DIR:-${SAVED_INSTALL_DIR:-/opt/zcodegateway}}"
    ZGW_PORT="${ZGW_PORT:-${SAVED_PORT:-8443}}"
    ZGW_DOMAIN="${ZGW_DOMAIN:-${SAVED_DOMAIN:-}}"
    ZGW_SERVER_IP="${ZGW_SERVER_IP:-${SAVED_SERVER_IP:-}}"
    # Older state recorded only scheme/domain. In particular, HTTP must not
    # become public IP TLS merely because an operator runs install again.
    if [[ -z "$SAVED_SSL_MODE" && -n "$SAVED_SCHEME" ]]; then
        if [[ "$SAVED_SCHEME" == http ]]; then SAVED_SSL_MODE=none
        elif [[ -n "$SAVED_DOMAIN" ]]; then SAVED_SSL_MODE=domain
        else SAVED_SSL_MODE=ip; fi
    fi
    ZGW_SSL_MODE="${ZGW_SSL_MODE:-${SAVED_SSL_MODE}}"
    if [[ "$SAVED_ACME_HOST" == __legacy__ ]]; then
        SAVED_ACME_HOST=""
        case "$SAVED_SSL_MODE" in
            domain) SAVED_ACME_HOST="$SAVED_DOMAIN" ;;
            ip) SAVED_ACME_HOST="$SAVED_SERVER_IP" ;;
        esac
    fi
    PAUSE_UNITS="${ZGW_PAUSE_UNITS-${SAVED_PAUSE_UNITS}}"
    ZGW_FROM_SOURCE="${ZGW_FROM_SOURCE:-${SAVED_FROM_SOURCE}}"
    CERT_DIR="${ZGW_INSTALL_DIR}/certs"
}

save_state() {
    local tmp
    tmp="$(mktemp "${ENV_FILE}.XXXXXXXX")" || return 1
    {
        printf 'SAVED_INSTALL_DIR=%q\n' "$ZGW_INSTALL_DIR"
        printf 'SAVED_PORT=%q\n' "$ZGW_PORT"
        printf 'SAVED_DOMAIN=%q\n' "$ZGW_DOMAIN"
        printf 'SAVED_SERVER_IP=%q\n' "${SERVER_IP:-${ZGW_SERVER_IP}}"
        printf 'SAVED_SCHEME=%q\n' "${SSL_SCHEME:-https}"
        printf 'SAVED_SSL_MODE=%q\n' "${TLS_MODE_CHOICE:-${ZGW_SSL_MODE:-${SAVED_SSL_MODE:-ip}}}"
        printf 'SAVED_ACME_HOST=%q\n' "${CURRENT_ACME_HOST:-}"
        printf 'SAVED_PAUSE_UNITS=%q\n' "${PAUSE_UNITS:-}"
        printf 'SAVED_FROM_SOURCE=%q\n' "${ZGW_FROM_SOURCE:-}"
        printf 'SAVED_RUN_AS_ROOT=%q\n' "${ZGW_RUN_AS_ROOT:-0}"
        printf 'SAVED_SKIP_SYSTEMD=%q\n' "${ZGW_SKIP_SYSTEMD:-0}"
    } > "$tmp" && chmod 600 "$tmp" && mv -fT -- "$tmp" "$ENV_FILE" \
        || { rm -f -- "$tmp"; return 1; }
}

need_root() { [[ $EUID -eq 0 ]] || fail "run this as root (sudo autozcoder ...)"; }

acquire_mutation_lock() {
    exec {MUTATION_LOCK_FD}>"$UPDATE_LOCK"
    flock -n "$MUTATION_LOCK_FD" || fail "another update is in progress (install/cert/update/uninstall share this lock)"
}

# Root never imports from the working directory, service HOME, a venv or
# inherited Python settings. Each privileged interpreter gets an empty HOME.
system_python() (
    local home rc=0
    home="$(update_tmpdir)" || return 1
    cd / || return 1
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME="$home" TMPDIR="$home" \
        /usr/bin/python3 -I "$@" || rc=$?
    rm -rf -- "$home"
    return "$rc"
)

run_tool_user() {
    runuser -u "$SVC_USER" -- env -i \
        PATH="${ZGW_INSTALL_DIR}/node/bin:/usr/local/bin:/usr/bin:/bin" \
        HOME="${ZGW_INSTALL_DIR}/home" \
        npm_config_cache="${ZGW_INSTALL_DIR}/home/.npm" \
        PLAYWRIGHT_BROWSERS_PATH="${ZGW_INSTALL_DIR}/pw-browsers" "$@"
}

# ------------------------------------------------------- service account
# The gateway is internet-facing and its admin key is code execution, so it must
# not also be root. Everything under the install dir belongs to ${SVC_USER};
# root (installer, acme.sh renewal) only writes and then hands ownership over.
service_user() {
    if [[ "${ZGW_RUN_AS_ROOT:-0}" == "1" || "${ZGW_SKIP_SYSTEMD:-0}" == "1" ]]; then
        echo root
    else
        echo "$SVC_USER"
    fi
}
ensure_service_user() {
    local u="${1:-$(service_user)}"
    [[ "$u" == root ]] && return 0
    id -u "$u" >/dev/null 2>&1 && return 0
    local shell; shell="$(command -v nologin 2>/dev/null || echo /usr/sbin/nologin)"
    useradd --system --no-create-home --home-dir "${ZGW_INSTALL_DIR}/home" \
            --shell "$shell" "$u" >>"$LOG_FILE" 2>&1 \
        || fail "could not create the '${u}' service user (no useradd?) — rerun with ZGW_RUN_AS_ROOT=1 to run as root"
}
fix_ownership() {
    local u; u="$(service_user)"
    ensure_service_user || return 1
    [[ ! -L "$ZGW_INSTALL_DIR" ]] || fail "install directory must not be a symlink"
    # Runtime config/SQLite use atomic sibling writes. A sticky, root-owned
    # directory permits those without letting the service replace root binaries.
    ensure_service_user "$SVC_USER" || return 1
    chown "root:${SVC_USER}" "$ZGW_INSTALL_DIR" && chmod 1770 "$ZGW_INSTALL_DIR" || return 1
    find "$ZGW_INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name ZcodeGateway ! -name certs ! -name node ! -name '.tls.*' \
        -exec chown -hR "${u}:${u}" -- {} + || return 1
    # Private Node is installer-owned executable tooling, not runtime data.
    if [[ -e "$ZGW_INSTALL_DIR/node" || -L "$ZGW_INSTALL_DIR/node" ]]; then
        [[ ! -L "$ZGW_INSTALL_DIR/node" ]] || fail "node directory must not be a symlink"
        chown -hR root:root "$ZGW_INSTALL_DIR/node" || return 1
        chmod -R go-w "$ZGW_INSTALL_DIR/node" || return 1
    fi
    local dir
    for dir in genenv pw-browsers home; do
        if [[ -d "$ZGW_INSTALL_DIR/$dir" ]]; then
            ensure_service_user "$SVC_USER" || return 1
            chown -hR "${SVC_USER}:${SVC_USER}" "$ZGW_INSTALL_DIR/$dir" || return 1
        fi
    done
    if [[ -e "$ZGW_INSTALL_DIR/ZcodeGateway" || -L "$ZGW_INSTALL_DIR/ZcodeGateway" ]]; then
        [[ ! -L "$ZGW_INSTALL_DIR/ZcodeGateway" ]] || fail "gateway executable must not be a symlink"
        chown root:root "$ZGW_INSTALL_DIR/ZcodeGateway" && chmod 755 "$ZGW_INSTALL_DIR/ZcodeGateway" || return 1
    fi
    if [[ -d "$CERT_DIR" ]]; then
        [[ ! -L "$CERT_DIR" ]] || fail "certificate directory must not be a symlink"
        chown "root:${u}" "$CERT_DIR" && chmod 750 "$CERT_DIR" || return 1
        local f
        for f in privkey.pem fullchain.pem; do
            if [[ -e "$CERT_DIR/$f" || -L "$CERT_DIR/$f" ]]; then
                [[ ! -L "$CERT_DIR/$f" ]] || fail "certificate file must not be a symlink"
                chown "root:${u}" "$CERT_DIR/$f" && chmod 640 "$CERT_DIR/$f" || return 1
            fi
        done
    fi
}
# what acme.sh runs after it (re)installs the certificate files
cert_reload_cmd() {
    local u cmd=""; u="$(service_user)"
    [[ "$u" != root ]] && cmd="chown root:${u} ${CERT_DIR}/privkey.pem ${CERT_DIR}/fullchain.pem; chmod 640 ${CERT_DIR}/privkey.pem ${CERT_DIR}/fullchain.pem; "
    echo "${cmd}systemctl restart ${SERVICE} 2>/dev/null || true"
}
is_installed() { [[ -x "${ZGW_INSTALL_DIR}/ZcodeGateway" ]]; }

# a real terminal for prompts, even under `curl | bash` (stdin is the pipe)
TTY_OK=0
if [[ "${ZGW_NONINTERACTIVE:-0}" != "1" ]] && ( : </dev/tty ) 2>/dev/null; then TTY_OK=1; fi
ask() { # ask "prompt" default  -> echoes the answer
    local reply=""
    if [[ "$TTY_OK" == "1" ]]; then
        read -rp "$1" reply </dev/tty || true
    fi
    echo "${reply:-$2}"
}
confirm() { # confirm "question" default(y|n)
    local d="${2:-y}" a
    a="$(ask "$1 [$([[ $d == y ]] && echo Y/n || echo y/N)] " "$d")"
    [[ "$a" =~ ^[Yy] ]]
}

server_resources() {
    # CPU affinity and container quotas can be lower than host capacity.
    local cpus memory limit quota period
    cpus="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"
    memory="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)"
    if [[ -r /sys/fs/cgroup/memory.max ]]; then
        limit="$(cat /sys/fs/cgroup/memory.max)"
    elif [[ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then
        limit="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)"
    else limit=""; fi
    if [[ "$limit" =~ ^[0-9]+$ ]]; then
        memory="$(awk -v host="${memory:-0}" -v bytes="$limit" 'BEGIN {limit=int(bytes/1024); print (host>0 && host<limit)?host:limit}')"
    fi
    if [[ -r /sys/fs/cgroup/cpu.max ]]; then
        read -r quota period </sys/fs/cgroup/cpu.max || true
    elif [[ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us && -r /sys/fs/cgroup/cpu/cpu.cfs_period_us ]]; then
        quota="$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)"
        period="$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)"
    else quota=""; period=""; fi
    if [[ "$quota" =~ ^[0-9]+$ && "$period" =~ ^[0-9]+$ ]] && (( period > 0 )); then
        cpus="$(awk -v host="$cpus" -v q="$quota" -v p="$period" 'BEGIN {limit=q/p; print (host>0 && host<limit)?host:limit}')"
    fi
    printf '%s %s\n' "${cpus:-0}" "${memory:-0}"
}

check_first_install_resources() {
    is_installed && return 0
    local cpus memory gib
    read -r cpus memory < <(server_resources)
    gib="$(awk -v kb="$memory" 'BEGIN {printf "%.1f",kb/1048576}')"
    say "Server capacity: ${cpus} CPU(s), ${gib} GiB RAM (detected limit)"
    if [[ "${ZGW_SKIP_GENERATOR:-0}" == 1 ]]; then
        say "Generator skipped; browser-generation sizing warning does not apply."
        return 0
    fi
    if awk -v cpu="$cpus" -v kb="$memory" 'BEGIN {exit !((cpu>0 && cpu<=1) || (kb>0 && kb<=2097152))}'; then
        warn "Low resources for account generation: ${cpus} CPU(s), ${gib} GiB RAM."
        say "Gateway-only use may be fine, but browser generation can be slow or fail under resource pressure."
        say "Recommended for one browser: 2 reliable vCPUs + 4 GB RAM. Shared CPU contention also matters."
        say "Use one worker; avoid simultaneous proxy scans/builds. Swap is emergency headroom, not browser capacity."
        if [[ "${ZGW_ALLOW_LOW_RESOURCES:-0}" == 1 ]]; then
            warn "Continuing with explicit ZGW_ALLOW_LOW_RESOURCES=1 acknowledgement."
        elif [[ "$TTY_OK" != 1 ]]; then
            fail "low-resource server needs confirmation: rerun interactively, set ZGW_ALLOW_LOW_RESOURCES=1, or use ZGW_SKIP_GENERATOR=1"
        elif ! confirm "This server is not recommended for comfortable generation. Continue anyway?" n; then
            say "Installation cancelled before installing packages or building the gateway."
            exit 0
        fi
    elif awk -v cpu="$cpus" -v kb="$memory" 'BEGIN {exit !(cpu<=0 || kb<=0)}'; then
        warn "Could not determine all resource limits; plan for at least 2 reliable vCPUs + 4 GB RAM for one generator browser."
    fi
}

run_action() { # run a menu action so a failure returns to the menu instead of exiting
    set +e; ( set -e; "$@" ); local rc=$?; set -e
    [[ $rc -ne 0 ]] && warn "action ended with exit code ${rc}"
    return 0
}

# ------------------------------------------------------------------ helpers
detect_server_ip() {
    [[ -n "${ZGW_SERVER_IP}" ]] && { echo "$ZGW_SERVER_IP"; return; }
    local u r
    for u in https://api4.ipify.org https://ipv4.icanhazip.com https://4.ident.me; do
        r="$(curl -fsS --max-time 4 "$u" 2>/dev/null || true)"
        r="${r//[$'\r\n ']/}"
        if [[ "$r" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then echo "$r"; return; fi
    done
    echo ""
}

is_domain() { [[ "$1" =~ ^([A-Za-z0-9](-*[A-Za-z0-9])*\.)+[A-Za-z]{2,}$ ]]; }

install_pkgs() (
    local home
    home="$(update_tmpdir)" || return 1
    trap 'rm -rf -- "$home"' EXIT
    cd / || return 1
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME="$home" TMPDIR="$home"
    local -a clean=(env -i PATH="$PATH" HOME="$HOME" TMPDIR="$TMPDIR" DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1)
    local release=""
    [[ -f /etc/os-release ]] && { . /etc/os-release; release="${ID:-}"; }
    case "${release}" in
        ubuntu|debian|armbian)      "${clean[@]}" apt-get update -qq && "${clean[@]}" apt-get install -y -qq -o Dpkg::Use-Pty=0 "$@" ;;
        fedora|amzn|rhel|almalinux|rocky|ol|centos)
                                    "${clean[@]}" dnf install -y -q "$@" ;;
        arch|manjaro|parch)         "${clean[@]}" pacman -Sy --noconfirm "$@" ;;
        opensuse-*)                 "${clean[@]}" zypper -q install -y "$@" ;;
        *)                          "${clean[@]}" apt-get update -qq && "${clean[@]}" apt-get install -y -qq -o Dpkg::Use-Pty=0 "$@" ;;
    esac
)

host_for_display() { echo "${ZGW_DOMAIN:-${SERVER_IP:-${ZGW_SERVER_IP:-<server-ip>}}}"; }
# the port the gateway itself listens on (https: public port; http: loopback only)
gw_port() { [[ "${SSL_SCHEME:-https}" == "https" ]] && echo "${ZGW_PORT}" || echo 8788; }
gw_health_url() {
    if [[ "${SSL_SCHEME:-https}" == "https" ]]; then echo "https://127.0.0.1:$(gw_port)/health"
    else echo "http://127.0.0.1:8788/health"; fi
}

# All config access uses YAML, including updates: key order, indentation and
# flow-style mappings must not decide whether an existing install is retained.
cfg_yaml() { # cfg_yaml validate|admin|client|has|set [section key [yaml-value]]
    system_python - "${ZGW_INSTALL_DIR}/config.yaml" "$@" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("Config access needs Python3 + PyYAML (install your distro's PyYAML package).")

path = Path(sys.argv[1])
action = sys.argv[2]
try:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a top-level mapping")
    server = data.get("server")
    if not isinstance(server, dict):
        raise ValueError("server must be a mapping")
    admin = server.get("admin_key")
    clients = server.get("client_keys")
    if not isinstance(admin, str) or not admin.strip():
        raise ValueError("server.admin_key must be a nonempty string")
    if not isinstance(clients, list) or not clients or any(
        not isinstance(c, dict) or not isinstance(c.get("key"), str) or not c["key"].strip()
        for c in clients
    ):
        raise ValueError("server.client_keys must contain nonempty string keys")
    if "generator" in data and not isinstance(data["generator"], dict):
        raise ValueError("generator must be a mapping")
    if action == "admin":
        print(admin)
    elif action == "client":
        print(clients[0]["key"])
    elif action == "has":
        section, key = sys.argv[3:5]
        sys.exit(0 if key in data.get(section, {}) else 1)
    elif action == "set":
        section, key, value = sys.argv[3:6]
        block = data.setdefault(section, {})
        if not isinstance(block, dict):
            raise ValueError("config section must be a mapping")
        block[key] = yaml.safe_load(value)
        text = yaml.safe_dump(data, sort_keys=False)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    elif action != "validate":
        raise ValueError("unknown config action")
except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
    # Parser exceptions can embed entire secret-bearing source lines.
    sys.exit(f"Cannot use {path} ({type(exc).__name__}); config was not replaced. Repair/validate its YAML and credential fields before retrying.")
PY
}

cfg_admin_key()  { cfg_yaml admin; }
cfg_client_key() { cfg_yaml client; }
cfg_set_server() { cfg_yaml set server "$1" "$2"; }

# point the gateway at the TLS decision: public https listener, or loopback http
bind_config() {
    if [[ "${SSL_SCHEME}" == "https" ]]; then
        cfg_set_server host "0.0.0.0"
        cfg_set_server port "${ZGW_PORT}"
        cfg_set_server tls_cert "\"${CERT_DIR}/fullchain.pem\""
        cfg_set_server tls_key  "\"${CERT_DIR}/privkey.pem\""
    else
        cfg_set_server host "127.0.0.1"
        cfg_set_server port "8788"
        cfg_set_server tls_cert '""'
        cfg_set_server tls_key  '""'
    fi
}

cert_end_date() {
    openssl x509 -noout -enddate -in "${CERT_DIR}/fullchain.pem" 2>/dev/null | sed 's/^notAfter=//' || true
}
cert_is_selfsigned() {
    local f="${CERT_DIR}/fullchain.pem" i s
    [[ -s "$f" ]] || return 1
    i="$(openssl x509 -noout -issuer  -in "$f" 2>/dev/null | sed 's/^issuer=//')"
    s="$(openssl x509 -noout -subject -in "$f" 2>/dev/null | sed 's/^subject=//')"
    [[ -n "$i" && "$i" == "$s" ]]
}

# ------------------------------------------------- TLS (acme.sh, built-in TLS)
acme() { "${ACME_HOME}/acme.sh" "$@"; }

install_acme() {
    [[ -x "${ACME_HOME}/acme.sh" ]] && return 0
    # Pinned release tarball, SHA-256 checked before anything in it runs.
    # --nocron: renewal runs from our own systemd timer. No contact e-mail on
    # purpose: a placeholder address such as admin@example.com is rejected by
    # Let's Encrypt.
    local d act; d="$(mktemp -d)"
    curl -fsSL --retry 3 -o "$d/acme.tgz" \
        "https://github.com/acmesh-official/acme.sh/archive/refs/tags/v${ACME_VERSION}.tar.gz" \
        || { rm -rf "$d"; return 1; }
    act="$(sha256sum "$d/acme.tgz" | awk '{print $1}')"
    if [[ "$act" != "$ACME_SHA256" ]]; then
        rm -rf "$d"
        echo "acme.sh v${ACME_VERSION} SHA-256 mismatch (expected ${ACME_SHA256}, got ${act}) — refusing to run it" >&2
        return 1
    fi
    tar -xzf "$d/acme.tgz" -C "$d" || { rm -rf "$d"; return 1; }
    ( cd "$d/acme.sh-${ACME_VERSION}" && ./acme.sh --install --nocron ) || { rm -rf "$d"; return 1; }
    rm -rf "$d"
    [[ -x "${ACME_HOME}/acme.sh" ]]
}

# who listens on a TCP port?  -> "pid comm" (empty when free)
port_listener() {
    local line pid
    line="$(ss -H -ltnp "sport = :$1" 2>/dev/null | head -1)"
    [[ -n "$line" ]] || return 0
    pid="$(sed -n 's/.*pid=\([0-9]*\).*/\1/p' <<<"$line" | head -1)"
    echo "${pid:-?} $( [[ -n "$pid" ]] && ps -o comm= -p "$pid" 2>/dev/null || echo unknown)"
}
# the systemd unit a pid belongs to (empty if none)
pid_unit() { grep -o '[^/]*\.service' "/proc/$1/cgroup" 2>/dev/null | tail -1 || true; }

# acme.sh standalone needs port 80 free: pause only the units the operator
# allowed us to pause, and bring them back no matter what happened
with_port80_free() {
    local rc=0
    # shellcheck disable=SC2086
    [[ -n "${PAUSE_UNITS:-}" ]] && { systemctl stop ${PAUSE_UNITS} >/dev/null 2>&1 || true; }
    "$@" || rc=$?
    # shellcheck disable=SC2086
    [[ -n "${PAUSE_UNITS:-}" ]] && { systemctl start ${PAUSE_UNITS} >/dev/null 2>&1 || true; }
    return $rc
}

# Port 80 must be free for the Let's Encrypt validation. Another service may
# own it (we never stop one silently): decide here whether LE is possible.
PORT80_BLOCKED=0
plan_port80() {
    PORT80_BLOCKED=0
    local who pid comm unit
    who="$(port_listener 80)"
    [[ -z "$who" ]] && { ok "port 80 is free for the certificate validation"; return 0; }
    pid="${who%% *}"; comm="${who#* }"
    unit="$(pid_unit "$pid")"
    if [[ -n "${PAUSE_UNITS:-}" ]]; then
        ok "port 80 is held by ${unit:-$comm}; it will be paused briefly for validation (ZGW_PAUSE_UNITS=${PAUSE_UNITS})"
        return 0
    fi
    warn "port 80 is in use by ${comm}${unit:+ (${unit})} — Let's Encrypt needs it for ~10 seconds"
    if [[ "$TTY_OK" == "1" && -n "$unit" ]]; then
        echo -e "  ${green}1.${plain} Pause ${unit} briefly now and during each renewal (every few days) — trusted certificate"
        echo -e "  ${green}2.${plain} Do not touch it — use a self-signed certificate (browser warning, no downtime)"
        if [[ "$(ask "Choose [2]: " 2)" == "1" ]]; then
            PAUSE_UNITS="$unit"
            ok "will pause ${unit} during certificate validation"
            return 0
        fi
    fi
    PORT80_BLOCKED=1
    warn "keeping ${unit:-$comm} running: using a self-signed certificate. For a trusted IP certificate later: free port 80, or run  ZGW_PAUSE_UNITS=${unit:-<unit>} autozcoder cert"
}

install_cert_files() { # install_cert_files <host>
    acme --installcert --force -d "$1" \
        --key-file       "${CERT_DIR}/privkey.pem" \
        --fullchain-file "${CERT_DIR}/fullchain.pem" \
        --reloadcmd "" \
        >>"$LOG_FILE" 2>&1 || return 1
    cert_pair_valid
}

cert_pair_valid() {
    local cert_pub key_pub
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -checkend 0 >/dev/null 2>&1 || return 1
    cert_pub="$(openssl x509 -in "$CERT_DIR/fullchain.pem" -pubkey -noout)" || return 1
    key_pub="$(openssl pkey -in "$CERT_DIR/privkey.pem" -pubout)" || return 1
    [[ -n "$cert_pub" && "$cert_pub" == "$key_pub" ]]
}

activate_acme_registration() {
    if [[ "${TLS_TRUSTED:-0}" == 1 ]]; then
        acme --installcert --force -d "$CURRENT_ACME_HOST" \
            --key-file "$CERT_DIR/privkey.pem" --fullchain-file "$CERT_DIR/fullchain.pem" \
            --reloadcmd "$(cert_reload_cmd)" >>"$LOG_FILE" 2>&1 || return 1
    fi
}

retire_acme_registration() {
    # Only retire the previous entry after the new gateway has passed health.
    if [[ -n "${SAVED_ACME_HOST:-}" && "$SAVED_ACME_HOST" != "${CURRENT_ACME_HOST:-}" ]]; then
        acme --remove -d "$SAVED_ACME_HOST" >>"$LOG_FILE" 2>&1 \
            || warn "could not retire old ACME entry; renewal is restricted to the active host"
    fi
}

issue_le() { # issue_le <host> <extra acme args...>
    local host="$1"; shift
    local hooks=()
    [[ -n "${PAUSE_UNITS:-}" ]] && hooks=(--pre-hook "systemctl stop ${PAUSE_UNITS}" \
                                          --post-hook "systemctl start ${PAUSE_UNITS}")
    run_quiet "Preparing the certificate tool (acme.sh)" install_acme || return 1
    acme --set-default-ca --server letsencrypt >>"$LOG_FILE" 2>&1 || true
    run_quiet "Requesting the Let's Encrypt certificate for ${host} (needs port 80, ~30 s)" \
        with_port80_free acme --issue -d "${host}" --standalone --httpport 80 \
        --server letsencrypt --force "$@" ${hooks[@]+"${hooks[@]}"} || return 1
    install_cert_files "${host}" || { bad "could not install the issued certificate"; return 1; }
    chmod 600 "${CERT_DIR}/privkey.pem"
}

issue_le_ip_cert() {
    say "Let's Encrypt IP certificate for ${SERVER_IP} (shortlived, renews automatically)"
    issue_le "${SERVER_IP}" --certificate-profile shortlived --days 3 || return 1
    TLS_NOTE="Let's Encrypt IP certificate (~6 days) — renewed automatically by ${RENEW_UNIT}.timer"
    TLS_TRUSTED=1
    ok "Let's Encrypt IP certificate installed"
}

issue_le_domain_cert() {
    say "Let's Encrypt certificate for ${ZGW_DOMAIN} (90 days; the A record must point here)"
    issue_le "${ZGW_DOMAIN}" || return 1
    TLS_NOTE="Let's Encrypt certificate (90 days) — renewed automatically by ${RENEW_UNIT}.timer"
    TLS_TRUSTED=1
    ok "Let's Encrypt domain certificate installed"
}

gen_self_signed() {
    say "Generating a self-signed certificate for ${SERVER_IP:-this server}"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "${CERT_DIR}/privkey.pem" \
        -out "${CERT_DIR}/fullchain.pem" \
        -subj "/CN=zcodegateway" \
        -addext "subjectAltName=IP:${SERVER_IP:-127.0.0.1}${ZGW_DOMAIN:+,DNS:${ZGW_DOMAIN}}" \
        >>"$LOG_FILE" 2>&1 || return 1
    chmod 600 "${CERT_DIR}/privkey.pem"
    TLS_NOTE="self-signed certificate (10 years) — browsers show a warning once"
    TLS_TRUSTED=0
    ok "self-signed certificate generated"
}

le_with_selfsigned_fallback() { # <issuer-fn>
    if [[ "$PORT80_BLOCKED" == "1" ]]; then
        gen_self_signed || fail "self-signed certificate generation failed"
        return 0
    fi
    if "$1"; then return 0; fi
    warn "Let's Encrypt issuance failed — falling back to a self-signed certificate"
    warn "(open port 80 in your provider firewall / check DNS, then run: autozcoder cert)"
    gen_self_signed || fail "self-signed certificate generation failed"
}

_apply_tls() {
    SSL_SCHEME="https"
    case "$1" in
        ip)     [[ -n "${SERVER_IP}" ]] || fail "no public IP detected (set ZGW_SERVER_IP=1.2.3.4)"
                le_with_selfsigned_fallback issue_le_ip_cert ;;
        domain) is_domain "${ZGW_DOMAIN}" || fail "a valid domain is required (ZGW_DOMAIN)"
                le_with_selfsigned_fallback issue_le_domain_cert ;;
        self)   gen_self_signed || fail "self-signed certificate generation failed" ;;
        none)   SSL_SCHEME="http"; TLS_TRUSTED=-1
                TLS_NOTE="NO TLS — the gateway listens on 127.0.0.1:8788 only; put your own TLS proxy / SSH tunnel in front"
                warn "$TLS_NOTE" ;;
        *)      fail "unknown ssl mode '$1' (ip|domain|self|none)" ;;
    esac
}

apply_tls() { # Stage a complete pair without deleting a working certificate.
    local live="$CERT_DIR" stage rc=0
    stage="$(mktemp -d "${ZGW_INSTALL_DIR}/.tls.XXXXXXXX")" || return 1
    ( CERT_DIR="$stage"; _apply_tls "$1" || exit $?;
      [[ "$1" == none ]] || cert_pair_valid ) || rc=$?
    # The subshell isolates fail/exit from the transaction. Derive reporting
    # from the validated staged pair without issuing a second certificate.
    if [[ "$rc" != 0 ]]; then rm -rf -- "$stage"; return "$rc"; fi
    SSL_SCHEME=https; CURRENT_ACME_HOST=""; TLS_TRUSTED=0
    if [[ "$1" == none ]]; then
        SSL_SCHEME=http; TLS_TRUSTED=-1; TLS_NOTE="NO TLS (loopback HTTP only)"
        rm -rf -- "$stage"
        return 0
    fi
    if ( CERT_DIR="$stage"; cert_is_selfsigned ); then
        TLS_NOTE="self-signed certificate (browser warning)"
    else
        TLS_TRUSTED=1; TLS_NOTE="Let's Encrypt certificate (automatic renewal)"
        if [[ "$1" == domain ]]; then CURRENT_ACME_HOST="$ZGW_DOMAIN"
        else CURRENT_ACME_HOST="$SERVER_IP"; fi
    fi
    # Callers stop the service before activation. Rename the directory, not two
    # independent PEM files, so no reader can observe a mixed pair.
    [[ ! -L "$live" ]] || { rm -rf -- "$stage"; return 1; }
    if [[ -e "$live" ]]; then
        # Linux rename-exchange publishes the pair in one filesystem operation;
        # a failure leaves the old directory and both PEMs untouched.
        if ! system_python - "$stage" "$live" <<'PY'
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.renameat2(-100, os.fsencode(sys.argv[1]), -100, os.fsencode(sys.argv[2]), 2):
    raise OSError(ctypes.get_errno(), "certificate directory exchange failed")
PY
        then rm -rf -- "$stage"; return 1; fi
        rm -rf -- "$stage"
    else
        mv -T -- "$stage" "$live" || { rm -rf -- "$stage"; return 1; }
    fi
}

# sets TLS_MODE_CHOICE (ip|domain|self|none); a global rather than command
# substitution so a domain typed at the prompt is kept
choose_tls_mode() {
    TLS_MODE_CHOICE="${ZGW_SSL_MODE:-ip}"
    if [[ "$TTY_OK" != "1" || -n "${ZGW_SSL_MODE}" ]]; then return 0; fi
    echo
    echo -e "${bold}Choose the TLS certificate setup:${plain}"
    echo -e "${green} 1.${plain} Let's Encrypt — domain   (90 days; needs port 80 + a DNS A record)"
    echo -e "${green} 2.${plain} Let's Encrypt — this server's IP  ${bold}(recommended: https://<ip>, no warning)${plain}"
    echo -e "${green} 3.${plain} Self-signed             (any IP, no port 80 — one browser warning)"
    echo -e "${green} 4.${plain} No TLS                  (gateway stays on 127.0.0.1; bring your own proxy / tunnel)"
    local c; c="$(ask "Choose an option [2]: " 2)"
    case "$c" in
        1) until is_domain "${ZGW_DOMAIN:-}"; do
               ZGW_DOMAIN="$(ask "Domain name: " "")"
               [[ -z "$ZGW_DOMAIN" ]] && fail "a domain is required for option 1"
           done
           TLS_MODE_CHOICE=domain ;;
        2) TLS_MODE_CHOICE=ip ;;
        3) TLS_MODE_CHOICE=self ;;
        4) TLS_MODE_CHOICE=none ;;
        *) fail "invalid choice" ;;
    esac
}

# a certificate already in CERT_DIR is reused only if it matches the requested
# host AND is not a self-signed leftover from a failed Let's Encrypt attempt
cert_usable() { # cert_usable <mode>
    local mode="$1" f="${CERT_DIR}/fullchain.pem"
    [[ -s "$f" && -s "${CERT_DIR}/privkey.pem" ]] || return 1
    # Renew before expiry, including short-lived IP certificates. Never call an
    # expired or nearly expired matching certificate a reusable trusted cert.
    openssl x509 -noout -checkend 86400 -in "$f" >/dev/null 2>&1 || return 1
    case "$mode" in
        none) return 1 ;;
        self) cert_is_selfsigned ;;
        ip)   [[ -n "$SERVER_IP" ]] && ! cert_is_selfsigned \
                  && openssl x509 -noout -checkip "$SERVER_IP" -in "$f" 2>/dev/null | grep -q 'does match' ;;
        domain) [[ -n "$ZGW_DOMAIN" ]] && ! cert_is_selfsigned \
                  && openssl x509 -noout -checkhost "$ZGW_DOMAIN" -in "$f" 2>/dev/null | grep -q 'does match' ;;
        *) return 1 ;;
    esac
}

install_renew_timer() {
    [[ "${ZGW_SKIP_SYSTEMD:-0}" == "1" ]] && return 0
    if [[ "${TLS_TRUSTED:-0}" != "1" ]]; then
        systemctl disable --now "${RENEW_UNIT}.timer" >/dev/null 2>&1 || true
        rm -f "$RENEW_SERVICE_FILE" "$RENEW_TIMER_FILE"
        return 0
    fi
    cat > "$RENEW_SERVICE_FILE" <<EOF
[Unit]
Description=Renew the Zcode Gateway TLS certificate (acme.sh)

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/flock -n ${UPDATE_LOCK} ${ACME_HOME}/acme.sh --renew --home ${ACME_HOME} --server letsencrypt -d ${CURRENT_ACME_HOST}
EOF
    cat > "$RENEW_TIMER_FILE" <<EOF
[Unit]
Description=Twice-daily certificate renewal check

[Timer]
OnCalendar=*-*-* 03,15:17:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now "${RENEW_UNIT}.timer" >/dev/null 2>&1 || warn "could not enable the renewal timer"
}

# ----------------------------------------------------------------- report
REPORT=()
chk() { # chk ok|warn|bad "text"
    case "$1" in
        ok)   echo -e "  ${green}✓${plain} $2";  REPORT+=("[ OK ] $2") ;;
        warn) echo -e "  ${yellow}!${plain} $2"; REPORT+=("[WARN] $2") ;;
        bad)  echo -e "  ${red}✗${plain} $2";    REPORT+=("[FAIL] $2") ;;
    esac
}

# The manager runs as root, so it is only ever replaced by a copy whose SHA-256
# matches the release's checksum list (never by whatever a branch serves today).
update_tmpdir() {
    # Ignore TMPDIR: it can point into service-controlled storage.
    mktemp -d /var/tmp/autozcoder-update.XXXXXXXX
}

resolve_release() {
    local url prefix="https://github.com/${ZGW_REPO}/releases/tag/" tag
    url="$(curl -fsSL --proto '=https' --proto-redir '=https' --max-time 30 --retry 2 \
        -o /dev/null -w '%{url_effective}' "https://github.com/${ZGW_REPO}/releases/latest" 2>>"$LOG_FILE")" || return 1
    [[ "$url" == "$prefix"* ]] || return 1
    tag="${url#"$prefix"}"
    [[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || return 1
    RELEASE_BASE="https://github.com/${ZGW_REPO}/releases/download/${tag}"
}

verified_asset() { # <stage> <asset name>; sums already fetched
    local d="$1" name="$2" exp act
    exp="$(awk -v n="$name" '$2==n {print $1}' "$d/checksums-linux.sha256")"
    [[ "$exp" =~ ^[0-9a-f]{64}$ ]] || { bad "missing, duplicate or malformed checksum for ${name}"; return 1; }
    curl -fsSL --proto '=https' --proto-redir '=https' --max-time 600 --retry 3 \
        -o "$d/$name" "${RELEASE_BASE}/${name}" 2>>"$LOG_FILE" || return 1
    act="$(sha256sum "$d/$name" | awk '{print $1}')"
    [[ "$act" == "$exp" ]] || { bad "SHA-256 mismatch for ${name}; refusing update"; return 1; }
}

stage_release() { # <private stage>; both artifacts must belong to one tag
    local d="$1"
    case "$(uname -m)" in x86_64|amd64|x64) ;; *) bad "release binaries support x86_64 only"; return 1 ;; esac
    release_runtime_compatible || return 1
    resolve_release || return 1
    curl -fsSL --proto '=https' --proto-redir '=https' --max-time 30 --retry 3 \
        -o "$d/checksums-linux.sha256" "${RELEASE_BASE}/checksums-linux.sha256" 2>>"$LOG_FILE" || return 1
    verified_asset "$d" ZcodeGateway && verified_asset "$d" install-linux.sh || return 1
    bash -n "$d/install-linux.sh" || return 1
    validate_gateway "$d/ZcodeGateway"
}

release_runtime_compatible() {
    local libc major minor
    libc="$(getconf GNU_LIBC_VERSION 2>/dev/null)" || { bad "release requires glibc 2.35 or newer"; return 1; }
    [[ "$libc" =~ ^glibc\ ([0-9]+)\.([0-9]+)$ ]] || { bad "unrecognized glibc version"; return 1; }
    major="${BASH_REMATCH[1]}"; minor="${BASH_REMATCH[2]}"
    if (( major < 2 || (major == 2 && minor < 35) )); then
        bad "release requires glibc 2.35 or newer (${libc} found); build from source on this host"
        return 1
    fi
}

validate_gateway() {
    # Do not execute the downloaded server as root (there is no --version flag).
    local arch; arch="$(uname -m)"
    system_python - "$1" "$arch" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
machine = {"x86_64": 62, "amd64": 62, "x64": 62, "aarch64": 183, "arm64": 183}.get(sys.argv[2])
with p.open("rb") as f:
    h = f.read(20)
if (p.stat().st_size < 1_000_000 or len(h) != 20 or h[:7] != b"\x7fELF\x02\x01\x01"
        or machine is None or int.from_bytes(h[18:20], "little") != machine
        or int.from_bytes(h[16:18], "little") not in (2, 3)):
    sys.exit("Refusing implausible or incompatible Linux executable")
PY
}

atomic_install() { # <source> <destination> <mode>; destination parent is trusted
    local src="$1" dst="$2" mode="$3" tmp
    tmp="$(mktemp "${dst}.XXXXXXXX")" || return 1
    install -m "$mode" -- "$src" "$tmp" && mv -fT -- "$tmp" "$dst" \
        || { rm -f -- "$tmp"; return 1; }
}

fetch_manager_verified() { # <destination> [already verified private stage]
    local d="${2:-}" own=0 rc=0
    if [[ -z "$d" ]]; then
        d="$(update_tmpdir)" || return 1
        own=1
        stage_release "$d" || { rm -rf -- "$d"; return 1; }
    fi
    atomic_install "$d/install-linux.sh" "$1" 755 || rc=$?
    [[ "$own" == 0 ]] || rm -rf -- "$d"
    return "$rc"
}

install_manager() {
    if [[ -n "$SELF" && -f "$SELF" && "$SELF" != /dev/* && "$SELF" != /proc/* ]]; then
        atomic_install "$SELF" "$MANAGER_BIN" 755 2>/dev/null && return 0
    fi
    fetch_manager_verified "$MANAGER_BIN"
}

# ------------------------------------------------------------ binary download
download_release() { # stage/verify before replacing any installed file
    local d
    d="$(update_tmpdir)" || return 1
    stage_release "$d" || { rm -rf -- "$d"; return 1; }
    mkdir -p "${ZGW_INSTALL_DIR}"
    [[ ! -L "$ZGW_INSTALL_DIR" ]] || { rm -rf -- "$d"; return 1; }
    ensure_service_user
    chown "root:$(service_user)" "$ZGW_INSTALL_DIR" && chmod 1770 "$ZGW_INSTALL_DIR" \
        && atomic_install "$d/ZcodeGateway" "$ZGW_INSTALL_DIR/ZcodeGateway" 755 \
        || { rm -rf -- "$d"; return 1; }
    rm -rf -- "$d"
    BIN_SOURCE="release (SHA-256 verified)"
}

_build_in() { ( cd "$1" && bash scripts/build/linux.sh ); }

build_from_source() { # build_from_source <repo dir>
    local src="$1" release=""
    [[ -f "$src/scripts/build/linux.sh" ]] || fail "no scripts/build/linux.sh in ${src}"
    if ! command -v bun >/dev/null 2>&1 && [[ ! -x "$src/tools/captcha-mint/captcha-mint" ]]; then
        fail "source build needs Bun or a native executable at ${src}/tools/captcha-mint/captcha-mint; provide it before retrying"
    fi
    [[ -f /etc/os-release ]] && release="$(. /etc/os-release; echo "${ID:-}")"
    case "$release" in
        ubuntu|debian|armbian)
            run_quiet "Installing build tools (python3-venv, pip, binutils)" \
                install_pkgs python3 python3-venv python3-pip binutils ;;
        *)  run_quiet "Installing build tools (python3, pip, binutils)" \
                install_pkgs python3 python3-pip binutils ;;
    esac
    run_quiet "Building ZcodeGateway from source (PyInstaller, a few minutes)" _build_in "$src" \
        || fail "build failed — details: ${LOG_FILE}"
    [[ -x "$src/.local/dist/ZcodeGateway" ]] || fail "build finished but .local/dist/ZcodeGateway is missing"
    validate_gateway "$src/.local/dist/ZcodeGateway" || fail "source build produced an incompatible executable"
    mkdir -p "${ZGW_INSTALL_DIR}"
    [[ ! -L "$ZGW_INSTALL_DIR" ]] || fail "install directory must not be a symlink"
    ensure_service_user
    chown "root:$(service_user)" "$ZGW_INSTALL_DIR" && chmod 1770 "$ZGW_INSTALL_DIR" \
        || fail "cannot secure installation directory"
    atomic_install "$src/.local/dist/ZcodeGateway" "${ZGW_INSTALL_DIR}/ZcodeGateway" 755 \
        || fail "could not activate source-built executable"
    BIN_SOURCE="built from source (${src})"
}

download_binary() {
    BIN_SOURCE=""
    if [[ -n "${ZGW_FROM_SOURCE:-}" ]]; then
        build_from_source "$ZGW_FROM_SOURCE"
    else
        say "looking for the latest GitHub release…"
        if download_release; then
            :
        else
            fail "could not download the release (does a release with the Linux build exist?). From a checkout use: ZGW_FROM_SOURCE=/path/to/repo"
        fi
    fi
}

write_unit() {
    # unprivileged by default; binding a port below 1024 then needs the one capability
    local u run_as=""; u="$(service_user)"
    if [[ "$u" != root ]]; then
        run_as="User=${u}"$'\n'"Group=${u}"
        (( ZGW_PORT < 1024 )) && run_as+=$'\n'"AmbientCapabilities=CAP_NET_BIND_SERVICE"$'\n'"CapabilityBoundingSet=CAP_NET_BIND_SERVICE"
    fi
    local tmp; tmp="$(mktemp "${UNIT_FILE}.XXXXXXXX")" || return 1
    cat > "$tmp" <<EOF
[Unit]
Description=Zcode Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
${run_as}
WorkingDirectory=${ZGW_INSTALL_DIR}
Environment=PLAYWRIGHT_BROWSERS_PATH=${ZGW_INSTALL_DIR}/pw-browsers
Environment=HOME=${ZGW_INSTALL_DIR}/home
ExecStart=${ZGW_INSTALL_DIR}/ZcodeGateway --config ${ZGW_INSTALL_DIR}/config.yaml
Restart=on-failure
RestartSec=3
TimeoutStopSec=90

# hardening (accounts/, certs/, genenv/, pw-browsers/, gateway.db, config.yaml live in the working dir)
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${ZGW_INSTALL_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$tmp" && mv -fT -- "$tmp" "$UNIT_FILE" || { rm -f -- "$tmp"; return 1; }
    systemctl daemon-reload
}

# =========================================================== commands
banner() {
    echo
    echo -e "${blue}${bold}╔══════════════════════════════════════════════════════════╗${plain}"
    printf "${blue}${bold}║${plain}  %-56s${blue}${bold}║${plain}\n" "$1"
    echo -e "${blue}${bold}╚══════════════════════════════════════════════════════════╝${plain}"
}

# our two listening ports must not be held by something else
preflight_ports() {
    local who comm
    who="$(port_listener "${ZGW_PORT}")"
    if [[ -n "$who" ]]; then
        comm="${who#* }"
        [[ "$comm" == ZcodeGateway* ]] || fail "port ${ZGW_PORT} is already used by ${comm}. Pick another: ZGW_PORT=9443 ... (then re-run)"
    fi
    who="$(port_listener 8788)"
    if [[ -n "$who" ]]; then
        comm="${who#* }"
        [[ "$comm" == ZcodeGateway* ]] || fail "port 8788 is already used by ${comm}"
    fi
    ok "ports ${ZGW_PORT} and 8788 are available"
}

open_firewall() {
    local need="80/tcp and ${ZGW_PORT}/tcp"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        if [[ "${ZGW_OPEN_FIREWALL:-0}" == "1" ]] || confirm "ufw is active — open ${need} now?" y; then
            ufw allow 80/tcp >/dev/null && ufw allow "${ZGW_PORT}/tcp" >/dev/null && ok "ufw: opened ${need}"
        else
            warn "ufw is active — you must run: ufw allow 80/tcp && ufw allow ${ZGW_PORT}/tcp"
        fi
    elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state 2>/dev/null | grep -q running; then
        if [[ "${ZGW_OPEN_FIREWALL:-0}" == "1" ]] || confirm "firewalld is running — open ${need} now?" y; then
            firewall-cmd --permanent --add-port=80/tcp --add-port="${ZGW_PORT}/tcp" >/dev/null \
                && firewall-cmd --reload >/dev/null && ok "firewalld: opened ${need}"
        else
            warn "firewalld is running — you must open ${need}"
        fi
    else
        ok "no active host firewall detected (also open ${need} in your cloud provider's firewall / security group)"
    fi
}

# is <key> present in the top-level `generator:` block of config.yaml?
cfg_has_generator() { # cfg_has_generator <key>
    cfg_yaml has generator "$1"
}

# set (or add) a key inside the top-level `generator:` block of config.yaml
cfg_set_generator() { # cfg_set_generator <key> <yaml-value>
    cfg_yaml set generator "$1" "$2"
}

# the nine modules the generator imports (generator/requirements.txt)
_gen_pip() { # _gen_pip <venv python>
    run_tool_user "$1" -m pip install --disable-pip-version-check playwright opencv-python-headless \
        Pillow numpy requests urllib3 certifi PySocks \
        && run_tool_user "$1" -m pip install --disable-pip-version-check ddddocr   # tightest pins: last
}
_gen_verify() { # _gen_verify <venv python>
    run_tool_user "$1" - <<'PY'
import importlib, os, sys
bad = []
for m in ("playwright", "ddddocr", "cv2", "PIL", "numpy", "requests", "urllib3", "certifi", "socks"):
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f"{m} ({type(e).__name__}: {e})")
if bad:
    sys.exit("missing/broken modules: " + ", ".join(bad))
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    exe = p.chromium.executable_path
    if not (exe and os.path.exists(exe)):
        sys.exit("chromium browser not found at " + str(exe))
print("generator environment OK")
PY
}

_gen_browser_smoke() {
    local py="${ZGW_INSTALL_DIR}/genenv/bin/python"
    run_tool_user "$py" - <<'PY'
import os, shutil, subprocess, tempfile
from playwright.sync_api import sync_playwright

proc = None
try:
    with tempfile.TemporaryFile() as ready:
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError("Xvfb is required for the generator")
        proc = subprocess.Popen([xvfb, "-displayfd", str(ready.fileno()),
                                 "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                                pass_fds=(ready.fileno(),), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        import time
        display = ""
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("Xvfb exited before becoming ready")
            ready.seek(0)
            display = ready.read().decode().strip()
            if display.isdigit():
                break
            time.sleep(.1)
        if not display.isdigit():
            raise RuntimeError("Xvfb readiness timed out")
        os.environ["DISPLAY"] = ":" + display
        os.environ["WAYLAND_DISPLAY"] = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--no-sandbox"], timeout=30000)
            try:
                page = browser.new_page()
                page.set_content("<title>AutoZCoder install check</title>")
                assert page.title() == "AutoZCoder install check"
            finally:
                browser.close()
    print("headed Chromium + private Xvfb launched as the service user")
finally:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
PY
}

# Node.js >= 22 (wrangler 4 needs it) for the Cloudflare mail worker. A system
# Node that is new enough is used as is; otherwise the official build is
# unpacked into <install dir>/node (checksum verified, nothing system-wide).
_node_major() {
    local v; v="$(run_tool_user node --version 2>/dev/null || true)"
    [[ "$v" =~ ^v([0-9]+) ]] && echo "${BASH_REMATCH[1]}" || echo 0
}
_install_node() {
    system_python - "${ZGW_INSTALL_DIR}" <<'PY' || return 1
import hashlib, json, os, platform, shutil, sys, tarfile, tempfile, urllib.request
dest_root = sys.argv[1]
arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
if not arch:
    sys.exit("no prebuilt Node.js for " + platform.machine())
get = lambda u: urllib.request.urlopen(u, timeout=60)
idx = json.load(get("https://nodejs.org/dist/index.json"))
ver = next(e["version"] for e in idx if e.get("lts") and int(e["version"].lstrip("v").split(".")[0]) >= 22)
name = f"node-{ver}-linux-{arch}.tar.xz"
base = f"https://nodejs.org/dist/{ver}"
sums = get(f"{base}/SHASUMS256.txt").read().decode()
expected = next(l.split()[0] for l in sums.splitlines() if l.strip().endswith(" " + name))
tmp = tempfile.mkdtemp(prefix="node-dl-", dir=dest_root)
try:
    archive = os.path.join(tmp, name)
    h = hashlib.sha256()
    with get(f"{base}/{name}") as r, open(archive, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            h.update(chunk); fh.write(chunk)
    if h.hexdigest() != expected:
        sys.exit("Node.js download: SHA-256 mismatch")
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(tmp, filter="data")
        except TypeError:
            tf.extractall(tmp)
    target = os.path.join(dest_root, "node")
    if os.path.islink(target):
        sys.exit("Node.js destination must not be a symlink")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(os.path.join(tmp, name[: -len(".tar.xz")]), target)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("node", ver, "installed")
PY
    chown -hR root:root "${ZGW_INSTALL_DIR}/node" && chmod -R a+rX,go-w "${ZGW_INSTALL_DIR}/node"
}
# warm npx's cache so the dashboard's first deploy does not time out downloading wrangler
_prefetch_wrangler() {
    local d rc=0; d="$(mktemp -d)"
    chown "${SVC_USER}:${SVC_USER}" "$d" || { rm -rf -- "$d"; return 1; }
    ( cd "$d" && run_tool_user npx --yes wrangler --version ) || rc=$?
    rm -rf "$d"
    return $rc
}
setup_node_for_mail() {
    if (( $(_node_major) >= 22 )) && run_tool_user sh -c 'command -v npx' >/dev/null 2>&1; then
        ok "Node.js $(run_tool_user node --version) already present — using it for the mail worker"
    else
        run_quiet "Installing Node.js 22 LTS for the mail worker (official build, checksum verified)" _install_node \
            || { warn "Node.js install failed — the dashboard will retry it on the first mail-worker deploy"; return 0; }
    fi
    mkdir -p "${ZGW_INSTALL_DIR}/home"
    run_quiet "Preparing the Cloudflare tool (wrangler, one-time download)" _prefetch_wrangler \
        || warn "could not pre-download wrangler — the first mail-worker deploy will fetch it"
}

# The account generator needs Python packages + Chromium. Do it here, once, so
# the dashboard's "Environment Setup" popup never appears (and never hits the
# PEP 668 "externally managed" refusal of the system Python).
setup_generator_env() {
    if [[ "${ZGW_SKIP_GENERATOR:-0}" == "1" ]]; then
        warn "skipping the generator environment (ZGW_SKIP_GENERATOR=1) — the dashboard will offer to install it"
        return 0
    fi
    local venv="${ZGW_INSTALL_DIR}/genenv" py release="" xvfb_pkg=xorg-x11-server-Xvfb
    py="${venv}/bin/python"
    [[ -f /etc/os-release ]] && release="$(. /etc/os-release; echo "${ID:-}")"
    case "$release" in
        ubuntu|debian|armbian) xvfb_pkg=xvfb ;;
        arch|manjaro|parch) xvfb_pkg=xorg-server-xvfb ;;
    esac
    # Install browser libraries from the distribution, never via a service-owned
    # Playwright interpreter's install-deps / --with-deps root subprocess.
    local -a browser_libs=()
    case "$release" in
        ubuntu|debian|armbian)
            local alsa=libasound2
            apt-cache show libasound2t64 >/dev/null 2>&1 && alsa=libasound2t64
            browser_libs=(libgl1 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2
                libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
                libgbm1 libpango-1.0-0 libcairo2 libatspi2.0-0 "$alsa" fonts-liberation) ;;
        arch|manjaro|parch) browser_libs=(nss nspr atk at-spi2-core cups libdrm libxkbcommon libxcomposite libxdamage libxfixes libxrandr mesa pango cairo alsa-lib) ;;
        *) browser_libs=(nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage libXfixes libXrandr mesa-libgbm pango cairo alsa-lib) ;;
    esac
    run_quiet "Installing Xvfb and distribution Chromium libraries" install_pkgs "$xvfb_pkg" "${browser_libs[@]}"
    case "$release" in
        ubuntu|debian|armbian)
            run_quiet "Installing Python venv support" install_pkgs python3 python3-venv python3-pip libgl1 ;;
        *)  run_quiet "Installing Python and pip" install_pkgs python3 python3-pip ;;
    esac
    ensure_service_user "$SVC_USER"
    mkdir -p "$venv" "${ZGW_INSTALL_DIR}/home" "${ZGW_INSTALL_DIR}/pw-browsers"
    # Also allows unprivileged tooling when the operator explicitly runs the
    # gateway itself as root. Existing service-owned venvs are never run as root.
    chown "root:${SVC_USER}" "$ZGW_INSTALL_DIR" && chmod 1770 "$ZGW_INSTALL_DIR"
    chown -hR "${SVC_USER}:${SVC_USER}" "$venv" "${ZGW_INSTALL_DIR}/home" "${ZGW_INSTALL_DIR}/pw-browsers"
    [[ -x "$py" ]] || run_quiet "Creating the generator Python environment" run_tool_user /usr/bin/python3 -m venv "$venv" \
        || fail "could not create the venv — details: ${LOG_FILE}"
    run_quiet "Updating pip" run_tool_user "$py" -m pip install --disable-pip-version-check --upgrade pip \
        || fail "pip update failed — details: ${LOG_FILE}"
    run_quiet "Installing generator packages (playwright, ddddocr, opencv, …)" _gen_pip "$py" \
        || fail "generator packages failed to install — details: ${LOG_FILE}"
    run_quiet "Installing Chromium as the tooling user (~250 MB)" \
        run_tool_user "$py" -m playwright install chromium \
        || fail "Chromium install failed — details: ${LOG_FILE}"
    run_quiet "Verifying the generator environment" _gen_verify "$py" \
        || fail "generator environment check failed — details: ${LOG_FILE}"
    # Signup uses a headed browser; the manager supplies a private Xvfb display
    # on no-display Linux hosts. Auto captcha mode does not imply headless=True.
    cfg_set_generator enabled true
    cfg_set_generator python_exe "\"${py}\""
    # only when the operator has not chosen a captcha mode yet (a fresh config)
    if ! cfg_has_generator captcha; then
        cfg_set_generator captcha '"ai"'
        cfg_set_generator headless false
    fi
    chmod 600 "${ZGW_INSTALL_DIR}/config.yaml"
    chk ok "generator environment ready (venv + Chromium) — no setup popup in the dashboard"
    setup_node_for_mail
    chk ok "mail worker tooling ready (Node.js + wrangler) — deploy it from the dashboard with a Cloudflare API token"
}

cmd_install() (
    need_root
    acquire_mutation_lock
    local TLS_NOTE="" TLS_TRUSTED=0 SSL_SCHEME="https" SERVER_IP="" CURRENT_ACME_HOST=""
    ( umask 077; : > "$LOG_FILE" )
    trap 'bad "Install failed at step ${STEP:-?} (line ${LINENO}). Details: ${LOG_FILE}"' ERR
    REPORT=()

    banner "Installing the Zcode Gateway"
    say "full details are written to ${LOG_FILE}"

    step "1/9" "Checking root, OS and architecture"
    local release=""
    [[ -f /etc/os-release ]] && { . /etc/os-release; release="${ID:-}"; }
    ok "OS: ${PRETTY_NAME:-unknown} (${release:-unrecognized})"
    case "$(uname -m)" in
        x86_64|x64|amd64) ok "Arch: x86_64" ;;
        aarch64|arm64) [[ -n "${ZGW_FROM_SOURCE:-}" ]] || fail "ARM64 requires ZGW_FROM_SOURCE=/path (no ARM release binary)"
                      ok "Arch: ARM64 (source build)" ;;
        *) fail "unsupported architecture '$(uname -m)' — prebuilt binaries are x86_64 only; build from source with scripts/build/linux.sh" ;;
    esac
    check_first_install_resources
    if [[ "${ZGW_SKIP_SYSTEMD:-0}" != "1" ]] && ! command -v systemctl >/dev/null 2>&1; then
        warn "systemd not found — the service step will be skipped"
        ZGW_SKIP_SYSTEMD=1
    fi

    step "2/9" "Installing prerequisites (curl, openssl, socat, Python YAML parser)"
    run_quiet "Installing curl, openssl, ca-certificates, socat" \
        install_pkgs curl openssl ca-certificates socat
    for c in curl openssl socat; do
        command -v "$c" >/dev/null 2>&1 || fail "$c did not install correctly"
    done
    # A real YAML parser is required even when the optional generator is skipped.
    # Use distro packages, not pip into a PEP 668-managed system interpreter.
    local python_pkg=python3 yaml_pkg=python3-yaml
    case "$release" in
        arch|manjaro|parch) python_pkg=python; yaml_pkg=python-yaml ;;
        fedora|amzn|rhel|almalinux|rocky|ol|centos) yaml_pkg=python3-pyyaml ;;
        opensuse-*) yaml_pkg=python3-PyYAML ;;
    esac
    run_quiet "Installing Python3 + PyYAML for safe config updates" install_pkgs "$python_pkg" "$yaml_pkg"
    system_python -c 'import yaml' || fail "Python3/PyYAML is required to preserve existing configuration"
    # older installs fronted the gateway with nginx; the gateway now serves
    # HTTPS itself, so retire our site (nginx itself is left alone)
    if [[ -f /etc/nginx/conf.d/zcodegateway.conf ]]; then
        rm -f /etc/nginx/conf.d/zcodegateway.conf
        rm -rf /etc/nginx/zcodegateway-certs
        systemctl reload nginx >/dev/null 2>&1 || true
        ok "retired the old nginx front (the gateway serves HTTPS itself now)"
    fi

    step "3/9" "Getting the ZcodeGateway binary"
    download_binary
    chk ok "binary ready: ${BIN_SOURCE}"
    cd "${ZGW_INSTALL_DIR}"

    step "4/9" "Writing config.yaml (random keys; an existing config is kept)"
    local admin_key="" client_key=""
    if [[ -e config.yaml || -L config.yaml ]]; then
        cfg_yaml validate || fail "existing config.yaml is invalid; left unchanged (no keys regenerated)"
        ok "config.yaml already exists — keeping it (keys unchanged)"
        admin_key="$(cfg_admin_key)"; client_key="$(cfg_client_key)"
        # behind a TLS front or direct, every visitor is "remote": the dashboard
        # admin must be allowed off-box
        cfg_set_server allow_remote_admin true
    else
        admin_key="$(openssl rand -hex 16)"; client_key="$(openssl rand -hex 16)"
        # noclobber prevents an install racing another first-run config writer.
        ( set -o noclobber; cat > config.yaml <<EOF
server:
  host: 127.0.0.1
  port: 8788
  require_client_key: true
  admin_key: ${admin_key}
  allow_remote_admin: true
  expose_ops: false
  tls_cert: ""
  tls_key: ""
  client_keys:
    - key: ${client_key}
      name: vps
upstream:
  base_url: https://zcode.z.ai/api/v1
  chat_path: /zcode-plan/anthropic/v1/messages
pool:
  strategy: drain_first
  cooldown_seconds: 30
  max_in_flight_per_account: 4
  acquire_wait_seconds: 5
  balance_poll_seconds: 60
accounts:
  source: accounts_dir
  dir: ./accounts
  pool_file: ./pool.json
models:
  default: GLM-5.3-Flash
  default_anthropic: GLM-5.3
  default_max_tokens: 128000
  default_effort: high
logging:
  level: INFO
store:
  sqlite_path: ./gateway.db
update:
  enabled: true
  repo: ${ZGW_REPO}
generator:
  enabled: false
EOF
        ) || fail "config.yaml could not be created exclusively; existing file was not overwritten"
        ok "config.yaml written (random admin + client keys)"
    fi
    chmod 600 config.yaml
    mkdir -p accounts logs certs home
    chmod 700 accounts certs 2>/dev/null || true   # live bearer tokens + the TLS key
    chk ok "config.yaml ready (random keys, mode 600)"

    step "5/9" "Account generator environment (Python packages, Chromium, Node.js for the mail worker)"
    setup_generator_env

    step "6/9" "Firewall + TLS (the gateway serves HTTPS itself on :${ZGW_PORT})"
    SERVER_IP="$(detect_server_ip)"
    [[ -n "$SERVER_IP" ]] && ok "server IP: ${SERVER_IP}" \
        || warn "could not auto-detect the server IP (set ZGW_SERVER_IP to override)"
    preflight_ports
    open_firewall
    plan_port80
    choose_tls_mode
    local mode="$TLS_MODE_CHOICE"
    if cert_usable "$mode"; then
        ok "existing certificate in ${CERT_DIR} is valid for this host — reusing it"
        SSL_SCHEME="https"
        if cert_is_selfsigned; then TLS_TRUSTED=0; TLS_NOTE="self-signed certificate — browsers show a warning once"
        else TLS_TRUSTED=1; TLS_NOTE="Let's Encrypt certificate — renewed by ${RENEW_UNIT}.timer"; fi
        CURRENT_ACME_HOST=""
        if [[ "$TLS_TRUSTED" == 1 ]]; then
            if [[ "$mode" == domain ]]; then CURRENT_ACME_HOST="$ZGW_DOMAIN"
            else CURRENT_ACME_HOST="$SERVER_IP"; fi
        fi
    else
        [[ "${ZGW_SKIP_SYSTEMD:-0}" == 1 ]] || systemctl stop "$SERVICE" || true
        apply_tls "$mode"
    fi
    if [[ "${ZGW_SKIP_SYSTEMD:-0}" == "1" && "$TLS_TRUSTED" == "1" ]]; then
        TLS_NOTE="Let's Encrypt certificate — automatic renewal NOT configured (no systemd)"
    fi
    bind_config
    case "${TLS_TRUSTED}" in
        1)  chk ok "TLS: ${TLS_NOTE}" ;;
        0)  chk warn "TLS: ${TLS_NOTE}" ;;
        *)  chk bad  "TLS: ${TLS_NOTE}" ;;
    esac

    step "7/9" "Installing + starting the systemd service"
    fix_ownership
    if [[ "${ZGW_SKIP_GENERATOR:-0}" != "1" ]]; then
        run_quiet "Testing headed Chromium + Xvfb as the service user" _gen_browser_smoke \
            || fail "browser runtime check failed — details: ${LOG_FILE}"
        chk ok "generator browser works as $(service_user) (private virtual display, no signup)"
    fi
    if [[ "${ZGW_SKIP_SYSTEMD:-0}" != "1" ]]; then
        write_unit
        systemctl enable "${SERVICE}" >/dev/null 2>&1
        systemctl restart "${SERVICE}"
        ok "service ${SERVICE} enabled + started"
        install_renew_timer
        [[ "${TLS_TRUSTED}" == "1" ]] && chk ok "certificate auto-renewal: ${RENEW_UNIT}.timer (twice daily)"
    else
        warn "ZGW_SKIP_SYSTEMD=1 — run manually: ${ZGW_INSTALL_DIR}/ZcodeGateway --config ${ZGW_INSTALL_DIR}/config.yaml"
    fi

    step "8/9" "Health-checking the install"
    if [[ "${ZGW_SKIP_SYSTEMD:-0}" == "1" ]]; then
        chk warn "gateway NOT STARTED (no systemd); running-service and public HTTPS checks skipped"
        if [[ "$TLS_TRUSTED" == "1" ]]; then
            chk warn "certificate renewal timer NOT installed; arrange acme.sh --renew -d ${CURRENT_ACME_HOST} scheduling before the certificate expires"
        fi
    else
        local i gw_ok=0 url; url="$(gw_health_url)"
        for i in $(seq 1 30); do
            if curl -fksS --max-time 2 "$url" >/dev/null 2>&1; then gw_ok=1; break; fi
            sleep 1
        done
        if [[ "$gw_ok" != "1" ]]; then
            chk bad "gateway did not answer on ${url}"
            journalctl -u "${SERVICE}" -n 15 --no-pager 2>/dev/null | sed 's/^/      /' >&2 || true
            fail "inspect: journalctl -u ${SERVICE} -e"
        fi
        chk ok "gateway healthy (${url} → 200)"
        local origin="${url%/health}"
        curl -fkSs --max-time 8 "${origin}/dash" >/dev/null \
            || fail "dashboard shell is unavailable"
        for asset in js/boot.js js/views/analytics.js css/tokens.css; do
            curl -fkSs --max-time 8 "${origin}/dash/assets/${asset}" >/dev/null \
                || fail "dashboard asset missing: ${asset}"
        done
        chk ok "modular dashboard shell and assets are bundled and reachable"

        # the real test: the public URL with FULL certificate verification (no -k).
        # Any HTTP status counts — only the TLS handshake/verification matters.
        if [[ "$SSL_SCHEME" == "https" ]]; then
            local pub; pub="$(host_for_display)"
            if curl -sS -o /dev/null --max-time 8 "https://${pub}:${ZGW_PORT}/dash" 2>/dev/null; then
                chk ok "https://${pub}:${ZGW_PORT}/dash opens with a TRUSTED certificate"
            elif [[ "$TLS_TRUSTED" == "1" ]]; then
                chk warn "could not confirm https://${pub}:${ZGW_PORT} from this host (NAT hairpin or closed port ${ZGW_PORT}) — test it from your browser"
            else
                chk warn "https://${pub}:${ZGW_PORT} works but the certificate is self-signed (browser warning)"
            fi
        fi
    fi

    step "9/9" "Registering the 'autozcoder' manager command"
    if install_manager; then
        chk ok "manager installed: type  autozcoder  for the menu"
    else
        chk warn "could not install ${MANAGER_BIN} (offline?) — re-run the installer to get the menu"
    fi

    activate_acme_registration
    if [[ "${ZGW_SKIP_SYSTEMD:-0}" != 1 && "$TLS_TRUSTED" == 1 ]]; then
        # Registering the renewal copy paths invokes its restart hook.
        local activated_health=0
        for i in $(seq 1 30); do
            if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then activated_health=1; break; fi
            sleep 1
        done
        [[ "$activated_health" == 1 ]] || fail "ACME activation health check failed"
    fi
    save_state
    retire_acme_registration
    trap - ERR
    final_report "$admin_key" "$client_key" "$TLS_NOTE"
)

final_report() { # final_report <admin> <client> <tls-note>
    local admin="$1" client="$2" tls_note="$3" host line out port
    host="$(host_for_display)"; port="$(gw_port)"
    line="══════════════════════════════════════════════════════════"
    out="${ZGW_INSTALL_DIR}/install-report.txt"
    {
        echo "Zcode Gateway — install report ($(date -u '+%Y-%m-%d %H:%M:%S UTC'))"
        printf '%s\n' "${REPORT[@]}"
        echo
        echo "Dashboard : ${SSL_SCHEME}://${host}:${port}/dash"
        echo "API base  : ${SSL_SCHEME}://${host}:${port}/v1"
        echo "Admin key : ${admin}"
        echo "Client key: ${client}"
        echo "TLS       : ${tls_note}"
        echo "Manager   : autozcoder"
        echo "Log       : ${LOG_FILE}"
    } > "$out"
    chmod 600 "$out"
    echo
    echo -e "${green}${line}${plain}"
    echo -e "${green}${bold}  ✔ Zcode Gateway installation finished${plain}"
    echo -e "${green}${line}${plain}"
    echo -e "  Dashboard : ${blue}${SSL_SCHEME}://${host}:${port}/dash${plain}"
    echo -e "  API base  : ${blue}${SSL_SCHEME}://${host}:${port}/v1${plain}"
    echo -e "  Admin key : ${blue}${admin}${plain}   ${dim}(enter it in the dashboard)${plain}"
    echo -e "  Client key: ${blue}${client}${plain}   ${dim}(use as the API key)${plain}"
    echo -e "  ${yellow}The admin key is code execution on this machine — do not share it.${plain}"
    echo
    echo -e "  Manage    : ${bold}autozcoder${plain}  (menu)   |  autozcoder status | stop | start | uninstall"
    echo -e "  Logs      : journalctl -u ${SERVICE} -f      install log: ${LOG_FILE}"
    echo -e "  Files     : ${ZGW_INSTALL_DIR}"
    echo -e "  Report    : ${out}"
    echo -e "${green}${line}${plain}"
}

cmd_update() (
    need_root; is_installed || fail "not installed — run: autozcoder install"
    local stage committed=0 activated=0 stopped=0 managed=1 healthy=0 i path name
    [[ "${ZGW_SKIP_SYSTEMD:-0}" == 1 ]] && managed=0
    # Serialize root-owned transactions, including manager refresh.
    acquire_mutation_lock
    stage="$(update_tmpdir)" || fail "cannot create private update staging directory"
    update_cleanup() {
        local rc=$? rollback_ok=1
        trap - EXIT INT TERM
        if [[ "$committed" == 0 && "$activated" == 1 ]]; then
            bad "Update failed; restoring the previous installation"
            [[ "$managed" == 0 ]] || systemctl stop "$SERVICE" || true
            for name in binary manager unit state; do
                case "$name" in
                    binary) path="$ZGW_INSTALL_DIR/ZcodeGateway" ;;
                    manager) path="$MANAGER_BIN" ;;
                    unit) path="$UNIT_FILE" ;;
                    state) path="$ENV_FILE" ;;
                esac
                if [[ -f "$stage/old-$name" ]]; then
                    atomic_install "$stage/old-$name" "$path" "$(stat -c %a "$stage/old-$name")" || rollback_ok=0
                elif [[ -f "$stage/absent-$name" ]]; then
                    rm -f -- "$path" || rollback_ok=0
                fi
            done
            if [[ "$managed" == 1 ]]; then
                systemctl daemon-reload || rollback_ok=0
                systemctl restart "$SERVICE" || rollback_ok=0
                local restored_health=0
                for i in $(seq 1 30); do
                    if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then restored_health=1; break; fi
                    sleep 1
                done
                [[ "$restored_health" == 1 ]] || rollback_ok=0
            fi
            [[ "$rollback_ok" == 1 ]] && warn "previous binary, manager, service unit and state restored" \
                || bad "ROLLBACK FAILED: inspect ${stage} and journalctl -u ${SERVICE}"
            [[ "$rc" != 0 ]] || rc=1
        elif [[ "$committed" == 0 && "$stopped" == 1 ]]; then
            systemctl restart "$SERVICE" || { rollback_ok=0; bad "could not restart unchanged service"; }
        fi
        if [[ "$rollback_ok" == 1 ]]; then rm -rf -- "$stage"; fi
        exit "$rc"
    }
    trap update_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    say "Staging update before stopping the service; running jobs will be interrupted on restart."
    if [[ -n "${ZGW_FROM_SOURCE:-}" ]]; then
        ( ZGW_INSTALL_DIR="$stage/source"; build_from_source "$ZGW_FROM_SOURCE" ) || fail "source staging failed; installed binary unchanged"
        mv -- "$stage/source/ZcodeGateway" "$stage/ZcodeGateway"
        validate_gateway "$stage/ZcodeGateway" || fail "invalid source binary"
    else
        stage_release "$stage" || fail "release staging/verification failed; installed binary unchanged"
    fi
    [[ ! -L "$ZGW_INSTALL_DIR" ]] || fail "install directory must not be a symlink"
    local u; u="$(service_user)"
    ensure_service_user
    chown "root:${u}" "$ZGW_INSTALL_DIR" && chmod 1770 "$ZGW_INSTALL_DIR" \
        || fail "cannot secure installation directory"
    # Stop writers before snapshotting files from older, service-owned installs.
    if [[ "$managed" == 1 ]]; then
        systemctl stop "$SERVICE" || fail "could not stop service"
        stopped=1
    fi
    for name in binary manager unit state; do
        case "$name" in
            binary) path="$ZGW_INSTALL_DIR/ZcodeGateway" ;;
            manager) path="$MANAGER_BIN" ;;
            unit) path="$UNIT_FILE" ;;
            state) path="$ENV_FILE" ;;
        esac
        [[ ! -L "$path" ]] || fail "refusing symlink deployment file: ${path}"
        if [[ -e "$path" ]]; then
            [[ -f "$path" ]] || fail "deployment file is not regular: ${path}"
            cp -p -- "$path" "$stage/old-$name" || fail "cannot back up ${path}"
        else
            : > "$stage/absent-$name"
        fi
    done
    activated=1
    atomic_install "$stage/ZcodeGateway" "$ZGW_INSTALL_DIR/ZcodeGateway" 755 || fail "binary activation failed"
    fix_ownership || fail "ownership update failed"
    if [[ -x "$ZGW_INSTALL_DIR/genenv/bin/python" ]]; then
        _gen_verify "$ZGW_INSTALL_DIR/genenv/bin/python" \
            >>"$LOG_FILE" 2>&1 || fail "generator environment is incompatible; rollback requested (repair with autozcoder install)"
    fi
    if [[ -f "$stage/install-linux.sh" ]]; then
        fetch_manager_verified "$MANAGER_BIN" "$stage" || fail "manager activation failed"
    fi
    if [[ "$managed" == 0 ]]; then
        committed=1
        ok "binary updated; manual service mode preserved (restart the gateway manually)"
        return 0
    fi
    # Keep the existing unit verbatim: custom config paths, flags and root-user
    # choices must not silently change during a binary-only update.
    systemctl restart "${SERVICE}" || fail "updated binary could not restart"
    for i in $(seq 1 30); do
        if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then healthy=1; break; fi
        sleep 1
    done
    [[ "$healthy" == 1 ]] || fail "updated gateway did not become healthy"
    committed=1
    ok "gateway updated and health check passed; service configuration preserved"
)

svc() { # svc start|stop|restart
    need_root; is_installed || fail "not installed — run: autozcoder install"
    systemctl "$1" "${SERVICE}" || fail "systemctl $1 ${SERVICE} failed (journalctl -u ${SERVICE} -e)"
    case "$1" in
        start|restart)
            local i
            for i in 1 2 3 4 5 6 7 8; do
                curl -ksS --max-time 2 "$(gw_health_url)" >/dev/null 2>&1 \
                    && { ok "gateway ${1}ed and healthy"; return 0; }
                sleep 1
            done
            warn "gateway ${1}ed but /health is not answering yet — check: autozcoder logs" ;;
        stop) ok "gateway stopped" ;;
    esac
}

dot() { [[ "$1" == "1" ]] && echo -e "${green}●${plain}" || echo -e "${red}●${plain}"; }

cmd_status() {
    is_installed || { warn "not installed"; return 0; }
    local svc_on=0 gw=0 days="" end host
    systemctl is-active --quiet "${SERVICE}" 2>/dev/null && svc_on=1
    curl -ksS --max-time 2 "$(gw_health_url)" >/dev/null 2>&1 && gw=1
    host="$(host_for_display)"
    echo
    echo -e "  $(dot $svc_on) service ${SERVICE}   : $(systemctl is-active "${SERVICE}" 2>/dev/null || true)"
    echo -e "  $(dot $gw) gateway /health     : $([[ $gw == 1 ]] && echo answering || echo 'not answering')"
    if [[ -s "${CERT_DIR}/fullchain.pem" && "${SSL_SCHEME}" == "https" ]]; then
        end="$(cert_end_date)"
        if [[ -n "$end" ]] && days=$(( ( $(date -d "$end" +%s 2>/dev/null || echo 0) - $(date +%s) ) / 86400 )); then :; fi
        echo -e "  ${blue}●${plain} certificate        : $(cert_is_selfsigned && echo 'self-signed' || echo "Let's Encrypt") — expires ${end:-?}${days:+ (${days} days)}"
        systemctl is-active --quiet "${RENEW_UNIT}.timer" 2>/dev/null \
            && echo -e "  ${green}●${plain} auto-renewal       : ${RENEW_UNIT}.timer active"
    fi
    echo -e "  Dashboard URL      : ${blue}${SSL_SCHEME}://${host}:$(gw_port)/dash${plain}"
    echo
}

cmd_info() {
    is_installed || fail "not installed"
    local host; host="$(host_for_display)"
    echo
    echo -e "  Dashboard : ${blue}${SSL_SCHEME}://${host}:$(gw_port)/dash${plain}"
    echo -e "  API base  : ${blue}${SSL_SCHEME}://${host}:$(gw_port)/v1${plain}"
    echo -e "  Admin key : ${blue}$(cfg_admin_key)${plain}"
    echo -e "  Client key: ${blue}$(cfg_client_key)${plain}"
    echo -e "  Install   : ${ZGW_INSTALL_DIR}"
    echo
}

cmd_logs() { journalctl -u "${SERVICE}" -n 100 -f --no-pager; }

cmd_cert() (
    need_root; is_installed || fail "not installed — run: autozcoder install"
    acquire_mutation_lock
    local TLS_NOTE="" TLS_TRUSTED=0 CURRENT_ACME_HOST="" committed=0 changed=0
    local live="$CERT_DIR" old_scheme="${SSL_SCHEME:-https}" stage name path i
    local managed=1 was_active=0 timer_active=0 timer_enabled=0 host entry
    [[ "${ZGW_SKIP_SYSTEMD:-0}" != 1 ]] || managed=0
    : >> "$LOG_FILE"
    SERVER_IP="$(detect_server_ip)"
    plan_port80
    choose_tls_mode
    local mode="$TLS_MODE_CHOICE"
    stage="$(update_tmpdir)" || fail "cannot create private certificate transaction"
    cert_cleanup() {
        local rc=$? rollback_ok=1
        trap - EXIT INT TERM
        if [[ "$committed" == 0 && "$changed" == 1 ]]; then
            bad "TLS change failed; restoring certificate, config, state and renewal policy"
            [[ "$managed" == 0 ]] || systemctl stop "$SERVICE" || true
            for name in config state renew-service renew-timer; do
                case "$name" in
                    config) path="$ZGW_INSTALL_DIR/config.yaml" ;;
                    state) path="$ENV_FILE" ;;
                    renew-service) path="$RENEW_SERVICE_FILE" ;;
                    renew-timer) path="$RENEW_TIMER_FILE" ;;
                esac
                if [[ -f "$stage/old-$name" ]]; then
                    atomic_install "$stage/old-$name" "$path" "$(stat -c %a "$stage/old-$name")" || rollback_ok=0
                    chown --reference="$stage/old-$name" "$path" || rollback_ok=0
                else rm -f -- "$path" || rollback_ok=0; fi
            done
            rm -rf -- "$live" || rollback_ok=0
            [[ ! -d "$stage/certs" ]] || cp -a -- "$stage/certs" "$live" || rollback_ok=0
            # Issuance changes acme.sh's install paths even before activation.
            # Restore both the old and attempted host entries, including hooks.
            for host in "${SAVED_ACME_HOST:-}" "$new_host"; do
                [[ -n "$host" ]] || continue
                for entry in "$host" "${host}_ecc"; do
                    rm -rf -- "$ACME_HOME/$entry" || rollback_ok=0
                    [[ ! -d "$stage/acme/$entry" ]] || cp -a -- "$stage/acme/$entry" "$ACME_HOME/$entry" || rollback_ok=0
                done
            done
            SSL_SCHEME="$old_scheme"
            if [[ "$managed" == 1 ]]; then
                systemctl daemon-reload || rollback_ok=0
                if [[ "$timer_enabled" == 1 ]]; then systemctl enable "${RENEW_UNIT}.timer" || rollback_ok=0
                else systemctl disable "${RENEW_UNIT}.timer" || rollback_ok=0; fi
                if [[ "$timer_active" == 1 ]]; then systemctl start "${RENEW_UNIT}.timer" || rollback_ok=0
                else systemctl stop "${RENEW_UNIT}.timer" || rollback_ok=0; fi
                if [[ "$was_active" == 1 ]]; then
                    systemctl restart "$SERVICE" || rollback_ok=0
                    local healthy=0
                    for i in $(seq 1 30); do
                        if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then healthy=1; break; fi
                        sleep 1
                    done
                    [[ "$healthy" == 1 ]] || rollback_ok=0
                fi
            fi
            [[ "$rc" != 0 ]] || rc=1
        fi
        if [[ "$rollback_ok" == 1 ]]; then rm -rf -- "$stage"
        else bad "TLS ROLLBACK FAILED: inspect ${stage} and journalctl -u ${SERVICE}"; fi
        exit "$rc"
    }
    trap cert_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    local new_host=""
    case "$mode" in domain) new_host="$ZGW_DOMAIN" ;; ip) new_host="$SERVER_IP" ;; esac
    for host in "${SAVED_ACME_HOST:-}" "$new_host"; do
        [[ -z "$host" ]] && continue
        [[ "$host" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || fail "invalid ACME host"
        for entry in "$host" "${host}_ecc"; do
            if [[ -d "$ACME_HOME/$entry" && ! -e "$stage/acme/$entry" ]]; then
                mkdir -p "$stage/acme"
                cp -a -- "$ACME_HOME/$entry" "$stage/acme/" || fail "cannot back up ACME policy"
            fi
        done
    done
    for name in config state renew-service renew-timer; do
        case "$name" in
            config) path="$ZGW_INSTALL_DIR/config.yaml" ;;
            state) path="$ENV_FILE" ;;
            renew-service) path="$RENEW_SERVICE_FILE" ;;
            renew-timer) path="$RENEW_TIMER_FILE" ;;
        esac
        [[ ! -L "$path" ]] || fail "refusing symlink deployment file: $path"
        [[ ! -e "$path" ]] || cp -p -- "$path" "$stage/old-$name" || fail "cannot back up $path"
    done
    [[ ! -L "$live" ]] || fail "certificate directory must not be a symlink"
    [[ ! -e "$live" ]] || cp -a -- "$live" "$stage/certs" || fail "cannot back up certificates"
    if [[ "$managed" == 1 ]]; then
        systemctl is-active --quiet "$SERVICE" && was_active=1
        systemctl is-active --quiet "${RENEW_UNIT}.timer" && timer_active=1
        systemctl is-enabled --quiet "${RENEW_UNIT}.timer" && timer_enabled=1
    fi
    changed=1
    [[ "$managed" == 0 ]] || systemctl stop "$SERVICE" || fail "could not stop gateway"
    apply_tls "$mode" || fail "certificate staging/validation failed"
    bind_config || fail "TLS config update failed"
    fix_ownership || fail "TLS permissions update failed"
    if [[ "$managed" == 1 ]]; then
        run_quiet "Restarting the gateway with the new certificate" systemctl restart "$SERVICE" || fail "TLS restart failed"
        local healthy=0
        for i in $(seq 1 30); do
            if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then healthy=1; break; fi
            sleep 1
        done
        [[ "$healthy" == 1 ]] || fail "new TLS gateway failed its health check"
    fi
    activate_acme_registration || fail "ACME activation failed"
    if [[ "$managed" == 1 ]]; then
        healthy=0
        for i in $(seq 1 30); do
            if curl -fkSs --max-time 2 "$(gw_health_url)" >/dev/null 2>&1; then healthy=1; break; fi
            sleep 1
        done
        [[ "$healthy" == 1 ]] || fail "ACME activation health check failed"
    fi
    install_renew_timer || fail "renewal policy update failed"
    save_state || fail "TLS state update failed"
    committed=1
    retire_acme_registration
    ok "TLS updated: ${TLS_NOTE}"
)

cmd_uninstall() (
    need_root
    acquire_mutation_lock
    local yes=0; [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && yes=1
    # refuse a path that could wipe something important
    case "${ZGW_INSTALL_DIR}" in
        ""|"/"|"/usr"|"/etc"|"/var"|"/home"|"/root"|"/opt"|"/bin"|"/lib") fail "refusing to delete '${ZGW_INSTALL_DIR}'" ;;
    esac
    [[ "${ZGW_INSTALL_DIR}" == /* ]] || fail "install dir must be absolute: ${ZGW_INSTALL_DIR}"

    banner "Uninstall the Zcode Gateway"
    echo -e "  This ${red}${bold}permanently deletes${plain}:"
    echo -e "    • the ${SERVICE} service and the certificate-renewal timer"
    echo -e "    • ${ZGW_INSTALL_DIR}  (binary, config.yaml, certificates, accounts/, generator venv + Chromium, gateway.db, logs)"
    echo -e "    • the 'autozcoder' command and ${ENV_FILE}"
    echo -e "  ${dim}System packages (curl, openssl, socat, python3-venv, Chromium's libraries) are left in place.${plain}"
    echo
    if [[ "$yes" != "1" ]]; then
        [[ "$TTY_OK" == "1" ]] || fail "no terminal to confirm on — re-run with: autozcoder uninstall --yes"
        if confirm "Save a backup of config + accounts to /root first?" n; then
            local bk; bk="/root/autozcoder-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
            tar -czf "$bk" -C "${ZGW_INSTALL_DIR}" config.yaml accounts gateway.db pool.json 2>/dev/null || true
            chmod 600 "$bk" 2>/dev/null || true
            ok "backup written: $bk"
        fi
        local typed; typed="$(ask "Type DELETE to confirm: " "")"
        [[ "$typed" == "DELETE" ]] || { warn "cancelled — nothing was removed"; return 0; }
    fi

    local removed=()
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "${SERVICE}" >/dev/null 2>&1 || true
        systemctl disable --now "${RENEW_UNIT}.timer" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/${SERVICE}.service" \
              "/etc/systemd/system/${RENEW_UNIT}.service" "/etc/systemd/system/${RENEW_UNIT}.timer"
        systemctl daemon-reload >/dev/null 2>&1 || true
        removed+=("systemd service + renewal timer")
    fi
    # kill a manually started instance as well
    pkill -f "${ZGW_INSTALL_DIR}/ZcodeGateway" >/dev/null 2>&1 || true

    local host; host="${SAVED_ACME_HOST:-}"
    if [[ -x "${ACME_HOME}/acme.sh" && -n "$host" ]]; then
        acme --remove -d "$host" >/dev/null 2>&1 || true
        rm -rf "${ACME_HOME:?}/${host:?}" "${ACME_HOME:?}/${host:?}_ecc" 2>/dev/null || true
        removed+=("acme.sh certificate entry for ${host}")
    fi
    # leftover from the nginx-fronted versions
    if [[ -f /etc/nginx/conf.d/zcodegateway.conf ]]; then
        rm -f /etc/nginx/conf.d/zcodegateway.conf
        rm -rf /etc/nginx/zcodegateway-certs
        systemctl reload nginx >/dev/null 2>&1 || true
        removed+=("old nginx site")
    fi
    rm -rf "${ZGW_INSTALL_DIR}" && removed+=("${ZGW_INSTALL_DIR}")
    rm -f "$ENV_FILE" && removed+=("${ENV_FILE}")
    if id -u "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" >/dev/null 2>&1; then
        removed+=("service user ${SVC_USER}")
    fi

    echo
    local r; for r in "${removed[@]}"; do ok "removed: $r"; done
    ok "Zcode Gateway fully removed."
    # last: the manager removes itself (bash has already read this function)
    rm -f "$MANAGER_BIN" 2>/dev/null || true
)

# ------------------------------------------------------------------- menu
menu() {
    trap ':' INT      # Ctrl+C ends the running action, not the menu
    local choice
    while true; do
        clear 2>/dev/null || true
        banner "Zcode Gateway manager  (autozcoder)"
        cmd_status
        echo -e "  ${green}1.${plain} Start                 ${green}6.${plain} Show URL + keys"
        echo -e "  ${green}2.${plain} Stop                  ${green}7.${plain} View logs"
        echo -e "  ${green}3.${plain} Restart               ${green}8.${plain} Re-issue / change TLS certificate"
        echo -e "  ${green}4.${plain} Status                ${green}9.${plain} Update to the latest release"
        echo -e "  ${green}5.${plain} Reinstall             ${red}10.${plain} ${red}Uninstall (fully delete)${plain}"
        echo -e "  ${green}0.${plain} Exit"
        echo
        choice="$(ask "Select [0-10]: " 0)"
        case "$choice" in
            1) run_action svc start ;;
            2) run_action svc stop ;;
            3) run_action svc restart ;;
            4) run_action cmd_status ;;
            5) run_action cmd_install ;;
            6) run_action cmd_info ;;
            7) run_action cmd_logs ;;
            8) run_action cmd_cert ;;
            9) run_action cmd_update ;;
            10) run_action cmd_uninstall
                is_installed || { echo; echo "Manager removed — goodbye."; exit 0; } ;;
            0|q|Q|"") echo; exit 0 ;;
            *) warn "invalid choice" ;;
        esac
        [[ "$choice" == "0" ]] || { echo; ask "Press Enter to return to the menu… " "" >/dev/null; }
    done
}

usage() {
    sed -n '2,17p' "${SELF:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
    echo "Manager commands read private installation state and require root: sudo autozcoder <command>"
}

# --------------------------------------------------------------------- main
# Help never needs private state. All other commands need it, so give the
# actionable privilege error before trying to source the root-only file.
case "${1:-}" in
    -h|--help|help) usage; exit 0 ;;
esac
need_root
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root TMPDIR=/var/tmp
unset PYTHONPATH PYTHONHOME PYTHONUSERBASE VIRTUAL_ENV NODE_OPTIONS NODE_PATH BASH_ENV ENV
resolve_config
SERVER_IP="${SAVED_SERVER_IP:-}"; SSL_SCHEME="${SAVED_SCHEME:-https}"
[[ -e "$LOG_FILE" ]] || ( umask 077; : > "$LOG_FILE" ) 2>/dev/null || true

case "${1:-}" in
    install|reinstall)   cmd_install ;;
    update|upgrade)      cmd_update ;;
    start)               svc start ;;
    stop)                svc stop ;;
    restart)             svc restart ;;
    status)              cmd_status ;;
    info|keys|show)      cmd_info ;;
    logs|log)            cmd_logs ;;
    cert|ssl|tls)        cmd_cert ;;
    uninstall|remove|delete) shift || true; cmd_uninstall "${1:-}" ;;
    menu)                menu ;;
    -h|--help|help)      usage ;;
    "")
        # installed + a real terminal -> the manager menu; otherwise (first
        # run, or `curl | bash` without a terminal) -> install / refresh
        if is_installed && [[ "$TTY_OK" == "1" ]]; then menu; else cmd_install; fi ;;
    *)  bad "unknown command '$1'"; usage; exit 2 ;;
esac
