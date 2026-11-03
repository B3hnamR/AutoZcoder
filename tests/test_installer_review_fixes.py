"""Offline installer fixtures: no package installs, real runuser, or ACME calls."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_deployment_audit import _shell


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux installer fixtures")


@pytest.mark.parametrize("mode,scheme,domain", [
    ("none", "http", "old.example.com"), ("domain", "https", "old.example.com"),
    ("self", "https", ""), ("ip", "https", ""),
])
def test_tls_mode_roundtrip_and_legacy_migration(tmp_path, mode, scheme, domain):
    result = _shell(tmp_path, f'''
ENV_FILE="$PWD/state"; ZGW_PORT=8443; ZGW_DOMAIN="{domain}"
ZGW_SERVER_IP=192.0.2.1; SSL_SCHEME={scheme}; TLS_MODE_CHOICE={mode}
CURRENT_ACME_HOST=""; save_state
unset ZGW_SSL_MODE ZGW_DOMAIN
resolve_config
[[ "$ZGW_SSL_MODE" == {mode} ]]
choose_tls_mode
[[ "$TLS_MODE_CHOICE" == {mode} ]]
sed -i '/^SAVED_SSL_MODE=/d; /^SAVED_ACME_HOST=/d' "$ENV_FILE"
unset ZGW_SSL_MODE
resolve_config
[[ "$ZGW_SSL_MODE" == {"none" if scheme == "http" else "domain" if domain else "ip"} ]]
''')
    assert result.returncode == 0, result.stderr


def test_explicit_mode_override_wins_and_interactive_choice_is_saved(tmp_path):
    result = _shell(tmp_path, r'''
ENV_FILE="$PWD/state"; printf 'SAVED_SSL_MODE=none\n' > "$ENV_FILE"
ZGW_SSL_MODE=domain; ZGW_DOMAIN=new.example.com; resolve_config
[[ "$ZGW_SSL_MODE" == domain ]]
ZGW_SSL_MODE=""; TTY_OK=1
ask() { echo 3; }
choose_tls_mode
save_state
unset ZGW_SSL_MODE
resolve_config
[[ "$ZGW_SSL_MODE" == self ]]
''')
    assert result.returncode == 0, result.stderr


def test_privileged_python_ignores_service_imports_and_environment(tmp_path):
    (tmp_path / "yaml.py").write_text('raise RuntimeError("service module imported")\n')
    result = _shell(tmp_path, r'''
export PYTHONPATH="$PWD" PYTHONHOME="$PWD" PYTHONUSERBASE="$PWD" HOME="$PWD"
system_python -c 'import os, pathlib, yaml; assert "PYTHONPATH" not in os.environ; assert "PYTHONHOME" not in os.environ; assert pathlib.Path.home() != pathlib.Path.cwd(); assert not list(pathlib.Path.home().iterdir()); print(yaml.__file__)'
''')
    assert result.returncode == 0, result.stderr
    assert str(tmp_path / "yaml.py") not in result.stdout
    assert not list(tmp_path.glob("stage.*"))


def test_generator_and_npm_commands_only_cross_mocked_runuser(tmp_path):
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; LOG_FILE="$PWD/log"
COMMANDS="$PWD/commands"
ZGW_RUN_AS_ROOT=1; ZGW_SKIP_SYSTEMD=1
ensure_service_user() { :; }; chown() { :; }; chmod() { :; }
cfg_set_generator() { :; }; cfg_has_generator() { return 0; }
setup_node_for_mail() { :; }
run_quiet() { local label="$1"; shift; "$@"; }
install_pkgs() { printf 'PKG %s\n' "$*" >> "$PWD/commands"; }
runuser() {
    [[ "$1" == -u && "$2" == autozcoder && "$3" == -- && "$4" == env && "$5" == -i ]] || return 93
    printf 'USER %s\n' "$*" >> "$COMMANDS"
}
mkdir -p genenv/bin home pw-browsers
printf '#!/bin/sh\necho ROOT_EXECUTED >&2; exit 94\n' > genenv/bin/python
command chmod 700 genenv/bin/python
setup_generator_env
_gen_browser_smoke
_prefetch_wrangler
''')
    assert result.returncode == 0, result.stderr
    assert "ROOT_EXECUTED" not in result.stderr
    commands = (tmp_path / "commands").read_text().splitlines()
    assert any(line.startswith("PKG ") and "libnss3" in line for line in commands)
    assert any("-m playwright install chromium" in line for line in commands)
    assert any("npx --yes wrangler --version" in line for line in commands)
    assert all("--with-deps" not in line and "install-deps" not in line for line in commands)
    assert sum(line.startswith("USER ") for line in commands) >= 7


def test_new_venv_is_created_as_tooling_user(tmp_path):
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; LOG_FILE="$PWD/log"
ensure_service_user() { :; }; chown() { :; }; chmod() { :; }
cfg_set_generator() { :; }; cfg_has_generator() { return 0; }; setup_node_for_mail() { :; }
run_quiet() { shift; "$@"; }; install_pkgs() { :; }
runuser() { printf '%s\n' "$*" >> "$PWD/commands"; }
setup_generator_env
''')
    assert result.returncode == 0, result.stderr
    assert "/usr/bin/python3 -m venv " in (tmp_path / "commands").read_text()


def test_node_is_excluded_from_service_ownership_and_probes_use_runuser(tmp_path):
    for directory in ("node/bin", "home", "genenv", "pw-browsers", "certs"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; CERT_DIR="$PWD/certs"; ZGW_SKIP_SYSTEMD=0
ensure_service_user() { :; }
chown() { printf '%s\n' "$*" >> "$PWD/owners"; }
chmod() { :; }
find() { printf '%s\n' "$*" > "$PWD/find-args"; }
runuser() { echo v22.1.0; }
fix_ownership
[[ "$(_node_major)" == 22 ]]
''')
    assert result.returncode == 0, result.stderr
    assert "! -name node" in (tmp_path / "find-args").read_text()
    owners = (tmp_path / "owners").read_text().splitlines()
    assert any("root:root" in line and line.endswith("/node") for line in owners)
    assert not any("autozcoder:autozcoder" in line and line.endswith("/node") for line in owners)


@pytest.mark.parametrize("command", ["cmd_install", "cmd_cert", "cmd_update", "cmd_uninstall --yes"])
def test_all_mutations_share_lock_before_any_work(tmp_path, command):
    result = _shell(tmp_path, f'''
need_root() {{ :; }}; is_installed() {{ return 0; }}
exec 9>"$UPDATE_LOCK"; flock -n 9
update_tmpdir() {{ echo UNEXPECTED_STAGE >&2; return 99; }}
detect_server_ip() {{ echo UNEXPECTED_NETWORK >&2; return 99; }}
systemctl() {{ echo UNEXPECTED_SYSTEMD >&2; return 99; }}
{command}
''')
    assert result.returncode != 0
    assert "share this lock" in result.stderr
    assert "UNEXPECTED_" not in result.stderr


@pytest.mark.parametrize("kind,success", [("pair", True), ("mismatch", False), ("malformed", False)])
def test_pair_validation_and_staging_never_delete_live_on_failure(tmp_path, kind, success):
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "fullchain.pem").write_text("old-cert")
    (certs / "privkey.pem").write_text("old-key")
    result = _shell(tmp_path, f'''
CERT_DIR="$PWD/certs"; LOG_FILE="$PWD/log"; SERVER_IP=192.0.2.1
_apply_tls() {{ printf new-cert > "$CERT_DIR/fullchain.pem"; printf new-key > "$CERT_DIR/privkey.pem"; }}
cert_is_selfsigned() {{ return 0; }}
openssl() {{
    case " $* " in
        *' -checkend '*) return {1 if kind == "malformed" else 0} ;;
        *' -pubkey '*) echo public ;;
        *' -pubout '*) echo {"other" if kind == "mismatch" else "public"} ;;
        *) return 99 ;;
    esac
}}
apply_tls self
''')
    assert (result.returncode == 0) is success, result.stderr
    assert (certs / "fullchain.pem").read_text() == ("new-cert" if success else "old-cert")
    assert (certs / "privkey.pem").read_text() == ("new-key" if success else "old-key")
    assert not list(tmp_path.glob(".tls.*"))


def test_failed_acme_copy_is_not_accepted_even_with_existing_files(tmp_path):
    (tmp_path / "privkey.pem").write_text("existing-key")
    (tmp_path / "fullchain.pem").write_text("existing-cert")
    result = _shell(tmp_path, r'''
CERT_DIR="$PWD"; LOG_FILE="$PWD/log"
acme() { return 1; }
install_cert_files new.example.com
''')
    assert result.returncode != 0


@pytest.mark.parametrize("fault", ["issue", "pair", "bind", "restart", "health", "policy", "state", "signal", "none", "success"])
def test_cert_transaction_restores_all_files_policy_and_service(tmp_path, fault):
    for name in ("config.yaml", "state", "renew-service", "renew-timer"):
        (tmp_path / name).write_text("old-" + name)
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "privkey.pem").write_text("old-key")
    (certs / "fullchain.pem").write_text("old-cert")
    for host in ("old.example.com", "new.example.com"):
        entry = tmp_path / "acme" / host
        entry.mkdir(parents=True)
        (entry / "policy").write_text("old-" + host)
    result = _shell(tmp_path, f'''
ZGW_INSTALL_DIR="$PWD"; CERT_DIR="$PWD/certs"; ENV_FILE="$PWD/state"; LOG_FILE="$PWD/log"
RENEW_SERVICE_FILE="$PWD/renew-service"; RENEW_TIMER_FILE="$PWD/renew-timer"
ACME_HOME="$PWD/acme"; SAVED_ACME_HOST=old.example.com; ZGW_DOMAIN=new.example.com
ZGW_SKIP_SYSTEMD=0; SSL_SCHEME=https; ZGW_PORT=8443
need_root() {{ :; }}; is_installed() {{ return 0; }}; fix_ownership() {{ :; }}
detect_server_ip() {{ echo 192.0.2.1; }}; plan_port80() {{ :; }}
choose_tls_mode() {{ TLS_MODE_CHOICE={"none" if fault == "none" else "domain"}; }}
run_quiet() {{ shift; "$@"; }}
_apply_tls() {{
    [[ '{fault}' != none ]] || return 0
    printf changed > "$ACME_HOME/new.example.com/policy"
    [[ '{fault}' != issue ]] || return 71
    printf new-cert > "$CERT_DIR/fullchain.pem"; printf new-key > "$CERT_DIR/privkey.pem"
}}
cert_pair_valid() {{ [[ '{fault}' != pair ]]; }}
cert_is_selfsigned() {{ return 1; }}
bind_config() {{
    printf new-config > "$PWD/config.yaml"
    if [[ '{fault}' == signal ]]; then kill -TERM "$BASHPID"; fi
    [[ '{fault}' != bind ]]
}}
install_renew_timer() {{
    printf new-policy > "$RENEW_SERVICE_FILE"; rm -f "$RENEW_TIMER_FILE"
    [[ '{fault}' != policy ]]
}}
save_state() {{
    [[ "$CURRENT_ACME_HOST" == new.example.com || '{fault}' == none ]] || return 89
    printf new-state > "$ENV_FILE"; [[ '{fault}' != state && '{fault}' != none ]];
}}
acme() {{ printf '%s\n' "$*" >> "$PWD/acme-calls"; }}
systemctl() {{
    printf '%s\n' "$*" >> "$PWD/systemctl-calls"
    if [[ "$1" == restart && '{fault}' == restart && "$(cat "$PWD/config.yaml")" == new-config ]]; then return 72; fi
    return 0
}}
curl() {{ [[ '{fault}' != health || "$(cat "$PWD/config.yaml")" != new-config ]]; }}
sleep() {{ :; }}
cmd_cert
''')
    if fault == "success":
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "config.yaml").read_text() == "new-config"
        assert (tmp_path / "state").read_text() == "new-state"
        assert (certs / "fullchain.pem").read_text() == "new-cert"
        assert (certs / "privkey.pem").read_text() == "new-key"
        assert (tmp_path / "acme-calls").read_text().endswith("--remove -d old.example.com\n")
        assert not list(tmp_path.glob("stage.*"))
        return
    assert result.returncode != 0, result.stdout
    for name in ("config.yaml", "state", "renew-service", "renew-timer"):
        assert (tmp_path / name).read_text() == "old-" + name
    assert (certs / "fullchain.pem").read_text() == "old-cert"
    assert (certs / "privkey.pem").read_text() == "old-key"
    for host in ("old.example.com", "new.example.com"):
        assert (tmp_path / "acme" / host / "policy").read_text() == "old-" + host
    assert not list(tmp_path.glob("stage.*"))
    assert not list(tmp_path.glob(".tls.*"))
    assert (tmp_path / "systemctl-calls").read_text().endswith("restart zcodegateway\n")
    if (tmp_path / "acme-calls").exists():
        assert "--remove" not in (tmp_path / "acme-calls").read_text()


def test_renewal_is_scoped_to_current_host_and_retirement_is_deferred(tmp_path):
    result = _shell(tmp_path, r'''
ZGW_SKIP_SYSTEMD=0; TLS_TRUSTED=1; CURRENT_ACME_HOST=new.example.com
SAVED_ACME_HOST=old.example.com; CERT_DIR="$PWD/certs"; LOG_FILE="$PWD/log"
RENEW_SERVICE_FILE="$PWD/renew-service"; RENEW_TIMER_FILE="$PWD/renew-timer"
acme() { printf '%s\n' "$*" >> "$PWD/acme-calls"; }
systemctl() { :; }
activate_acme_registration
[[ "$(cat "$PWD/acme-calls")" != *--remove* ]]
install_renew_timer
retire_acme_registration
''')
    assert result.returncode == 0, result.stderr
    unit = (tmp_path / "renew-service").read_text()
    assert "--cron" not in unit
    assert "--renew" in unit and "--server letsencrypt -d new.example.com" in unit
    assert "/usr/bin/flock -n " in unit
    calls = (tmp_path / "acme-calls").read_text().splitlines()
    assert calls[-1] == "--remove -d old.example.com"


def test_saved_self_signed_fallback_does_not_resurrect_old_acme_host(tmp_path):
    result = _shell(tmp_path, r'''
ENV_FILE="$PWD/state"; ZGW_DOMAIN=old.example.com; ZGW_PORT=8443
ZGW_SERVER_IP=192.0.2.1; TLS_MODE_CHOICE=domain; SSL_SCHEME=https
CURRENT_ACME_HOST=""; save_state
unset ZGW_SSL_MODE
resolve_config
[[ "$SAVED_ACME_HOST" == "" && "$ZGW_SSL_MODE" == domain ]]
''')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("arch,source,accepted", [
    ("aarch64", True, True), ("arm64", True, True),
    ("aarch64", False, False), ("armv7l", True, False),
])
def test_installer_arm_source_gate_before_packages(tmp_path, arch, source, accepted):
    result = _shell(tmp_path, f'''
LOG_FILE="$PWD/log"; ZGW_FROM_SOURCE={"/explicit/source" if source else ""}
need_root() {{ :; }}; uname() {{ echo {arch}; }}
check_first_install_resources() {{ echo ARCH_ACCEPTED; exit 0; }}
install_pkgs() {{ echo UNEXPECTED_PACKAGES; return 99; }}
cmd_install
''')
    assert (result.returncode == 0) is accepted, result.stderr
    assert ("ARCH_ACCEPTED" in result.stdout) is accepted
    assert "UNEXPECTED_PACKAGES" not in result.stdout


@pytest.mark.parametrize("host,machine,accepted", [("aarch64", 183, True), ("aarch64", 62, False), ("x86_64", 183, False)])
def test_native_source_binary_validation_uses_host_arch(tmp_path, host, machine, accepted):
    payload = bytearray(1_000_001)
    payload[:7] = b"\x7fELF\x02\x01\x01"
    payload[16:18] = (3).to_bytes(2, "little")
    payload[18:20] = machine.to_bytes(2, "little")
    (tmp_path / "candidate").write_bytes(payload)
    result = _shell(tmp_path, f'uname() {{ echo {host}; }}; validate_gateway "$PWD/candidate"')
    assert (result.returncode == 0) is accepted, result.stderr


def test_nonroot_commands_fail_before_private_state_but_help_works(tmp_path):
    # Replace the state path in a disposable script, never /etc or the real UID.
    state = tmp_path / "private-state"
    state.write_text('echo PRIVATE_STATE_EXECUTED >&2\n')
    script = (ROOT / "install-linux.sh").read_text(encoding="utf-8")
    script = script.replace('ENV_FILE="/etc/autozcoder.env"', f'ENV_FILE="{state}"')
    # Mock just the UID predicate so this is safe even on root CI runners.
    script = script.replace('[[ $EUID -eq 0 ]]', '[[ 1000 -eq 0 ]]')
    installer = tmp_path / "installer.sh"
    installer.write_text(script)
    for args, success in [(["--help"], True), (["status"], False), (["info"], False), (["install"], False)]:
        result = subprocess.run(
            [shutil.which("bash"), str(installer), *args], cwd=tmp_path,
            env={"PATH": os.defpath, "HOME": str(tmp_path), "ZGW_NONINTERACTIVE": "1"},
            text=True, capture_output=True, timeout=10,
        )
        assert (result.returncode == 0) is success
        assert "PRIVATE_STATE_EXECUTED" not in result.stderr
        if not success:
            assert "run this as root" in result.stderr
