"""Deployment regressions. Run only in the coordinator's isolated Linux runner."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

from zcode_gateway import paths


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux deployment checks")


def _shell(tmp_path, body, *, installer=True):
    script = (ROOT / ("install-linux.sh" if installer else "scripts/build/linux.sh")).read_text(encoding="utf-8")
    if installer:
        script = script.split("# --------------------------------------------------------------------- main", 1)[0]
    else:
        script = script.split("# 2. python + deps", 1)[0]
    env = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LC_ALL": "C",
        "ZGW_NONINTERACTIVE": "1", "ZGW_INSTALL_DIR": str(tmp_path),
    }
    return subprocess.run(
        [shutil.which("bash"), "-c", script + "\n" + body],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15,
    )


def _config(tmp_path, text=None):
    path = tmp_path / "config.yaml"
    path.write_text(text or """server:
  admin_key: 'admin: quoted # credential'
  client_keys:
  - name: vps
    key: 'client: quoted # credential'
generator:
  custom: keep
custom: {nested: [one, two], enabled: true}
""", encoding="utf-8")
    return path


@pytest.mark.parametrize("style", ["reordered", "safe_dump", "flow"])
def test_yaml_config_updates_keep_credentials_and_custom_settings(tmp_path, style):
    path = _config(tmp_path)
    original = yaml.safe_load(path.read_text())
    if style == "safe_dump":
        path.write_text(yaml.safe_dump(original), encoding="utf-8")
    elif style == "flow":
        path.write_text(yaml.safe_dump(original, default_flow_style=True), encoding="utf-8")
    result = _shell(tmp_path, """
ZGW_INSTALL_DIR="$PWD"
cfg_yaml validate
cfg_admin_key
cfg_client_key
cfg_set_server port 9443
cfg_set_generator enabled true
cfg_has_generator custom
""")
    assert result.returncode == 0, result.stderr
    updated = yaml.safe_load(path.read_text())
    assert updated["server"]["admin_key"] == original["server"]["admin_key"]
    assert updated["server"]["client_keys"] == original["server"]["client_keys"]
    assert updated["custom"] == original["custom"]
    assert updated["generator"]["custom"] == "keep"
    assert updated["server"]["port"] == 9443
    assert updated["generator"]["enabled"] is True
    assert original["server"]["admin_key"] in result.stdout
    assert original["server"]["client_keys"][0]["key"] in result.stdout
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".config-*.tmp"))


@pytest.mark.parametrize("text", ["server: [", "[]", "server: {}", "server: null"])
def test_invalid_existing_config_is_never_replaced(tmp_path, text):
    path = _config(tmp_path, text)
    before = path.read_bytes()
    result = _shell(tmp_path, 'ZGW_INSTALL_DIR="$PWD"; cfg_set_server port 9443')
    assert result.returncode != 0
    assert "config was not replaced" in result.stderr
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".config-*.tmp"))


def test_bootstrap_creation_is_private_before_first_write(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    monkeypatch.setenv("ZCODE_GATEWAY_CONFIG", "")
    original_fdopen = paths.os.fdopen
    observed = []

    def check_fdopen(fd, *args, **kwargs):
        observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(paths.os, "fdopen", check_fdopen)
    previous = os.umask(0o022)
    try:
        config = paths.bootstrap_config()
    finally:
        os.umask(previous)
    assert observed == [0o600]
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "accounts").stat().st_mode) == 0o700


def test_bootstrap_preserves_config_created_by_another_launch(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("existing config", encoding="utf-8")
    paths._write_initial_config(config, "replacement secrets")
    assert config.read_text() == "existing config"


def test_bootstrap_fallback_creates_private_directories(tmp_path, monkeypatch):
    unavailable = tmp_path / "unavailable"
    unavailable.write_text("not a directory", encoding="utf-8")
    fallback = tmp_path / "private-data"
    monkeypatch.setattr(paths, "app_dir", lambda: unavailable)
    monkeypatch.setattr(paths, "_per_user_dir", lambda: fallback)
    monkeypatch.setenv("ZCODE_GATEWAY_CONFIG", "")
    previous = os.umask(0o022)
    try:
        config = paths.bootstrap_config()
    finally:
        os.umask(previous)
    assert config == fallback / "config.yaml"
    for directory in (fallback, fallback / "accounts"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.parametrize("remaining,expected", [(172800, 0), (3600, 1), (-60, 1)])
def test_certificate_reuse_requires_renewal_margin(tmp_path, remaining, expected):
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "fullchain.pem").write_text("fixture", encoding="utf-8")
    (certs / "privkey.pem").write_text("fixture", encoding="utf-8")
    result = _shell(tmp_path, f"""
CERT_DIR="$PWD/certs"
SERVER_IP=192.0.2.1
cert_is_selfsigned() {{ return 1; }}
openssl() {{
    case " $* " in
        *' -checkend 86400 '*) (( {remaining} > 86400 )) ;;
        *' -checkip '*) echo 'IP 192.0.2.1 does match certificate' ;;
        *) return 1 ;;
    esac
}}
if cert_usable ip; then exit 0; else exit 1; fi
""")
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_no_systemd_install_completes_without_live_health_checks(tmp_path, existing):
    if existing:
        _config(tmp_path)
    result = _shell(tmp_path, """
ZGW_INSTALL_DIR="$PWD"; CERT_DIR="$PWD/certs"; LOG_FILE="$PWD/install.log"
ZGW_SKIP_SYSTEMD=1; ZGW_SKIP_GENERATOR=1; ZGW_PORT=8443
ZGW_SSL_MODE=self; ZGW_SERVER_IP=192.0.2.1; ZGW_DOMAIN=""
need_root() { :; }
uname() { echo x86_64; }
openssl() { echo fixture-random-key; }
socat() { :; }
cat() {
    if [[ -e config.yaml ]]; then
        [[ "$(stat -c %a config.yaml)" == 600 ]] || return 93
    fi
    command cat "$@"
}
run_quiet() { :; }
download_binary() { BIN_SOURCE=fixture; }
setup_generator_env() { :; }
fix_ownership() { :; }
detect_server_ip() { echo 192.0.2.1; }
preflight_ports() { :; }
open_firewall() { :; }
plan_port80() { :; }
choose_tls_mode() { TLS_MODE_CHOICE=self; }
cert_usable() { return 0; }
cert_is_selfsigned() { return 0; }
bind_config() { :; }
install_manager() { touch "$PWD/manager-written"; }
save_state() { touch "$PWD/state-written"; }
final_report() { touch "$PWD/report-written"; }
curl() { echo 'UNEXPECTED_NETWORK_CALL' >&2; return 90; }
systemctl() { echo 'UNEXPECTED_SYSTEMD_CALL' >&2; return 91; }
journalctl() { echo 'UNEXPECTED_JOURNAL_CALL' >&2; return 92; }
cmd_install
""")
    assert result.returncode == 0, result.stderr
    assert "NOT STARTED" in result.stdout
    assert "UNEXPECTED_" not in result.stderr
    for marker in ("manager-written", "state-written", "report-written"):
        assert (tmp_path / marker).is_file()
    assert stat.S_IMODE((tmp_path / "config.yaml").stat().st_mode) == 0o600


@pytest.mark.parametrize("kind,success", [
    ("missing", False), ("windows-only", False), ("pe", False),
    ("wrong-arch", False), ("not-executable", False), ("native", True),
])
def test_sidecar_preflight_rejects_missing_or_foreign_binaries(tmp_path, kind, success):
    mint = tmp_path / "tools" / "captcha-mint"
    mint.mkdir(parents=True)
    (mint / "mint.ts").write_text("// source fixture; never executed", encoding="utf-8")
    header = bytearray(20)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (3).to_bytes(2, "little")
    header[18:20] = (183 if kind == "wrong-arch" else 62).to_bytes(2, "little")
    if kind not in ("missing", "windows-only"):
        binary = mint / "captcha-mint"
        binary.write_bytes(b"MZ" + b"\0" * 18 if kind == "pe" else header)
        binary.chmod(0o600 if kind == "not-executable" else 0o700)
    if kind == "windows-only":
        (mint / "captcha-mint.exe").write_bytes(b"MZ" + b"\0" * 18)
    # Hide Bun so no dependencies or actual sidecar can run. Pin uname so the
    # fixture is deterministic on arm64 Linux test runners too.
    build_path = tmp_path / "scripts" / "build" / "linux.sh"
    build_path.parent.mkdir(parents=True)
    build = (ROOT / "scripts" / "build" / "linux.sh").read_text(encoding="utf-8")
    prefix = """uname() { if [[ "$1" == "-s" ]]; then echo Linux; else echo x86_64; fi; }
command() {
    if [[ "$1" == "-v" && "${2:-}" == "bun" ]]; then return 1; fi
    builtin command "$@"
}
"""
    script = build.split("# 2. python + deps", 1)[0]
    build_path.write_text(prefix + script, encoding="utf-8")
    env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
           "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LC_ALL": "C"}
    result = subprocess.run([shutil.which("bash"), str(build_path)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert (result.returncode == 0) is success, result.stderr
    assert not (tmp_path / ".venv").exists()


def test_documented_unit_has_valid_working_directory_and_private_umask():
    doc = (ROOT / "docs" / "guides" / "VPS-DEPLOY.md").read_text(encoding="utf-8")
    unit = doc.split("```ini\n", 1)[1].split("```", 1)[0]
    assert "WorkingDirectory=/opt/zcodegateway\n" in unit
    assert "UMask=0077\n" in unit
    assert "User=zcodegateway\n" in unit
    installer = (ROOT / "install-linux.sh").read_text(encoding="utf-8")
    generated_unit = installer.split("write_unit()", 1)[1].split("# ===========================================================", 1)[0]
    assert "UMask=0077\n" in generated_unit


@pytest.mark.parametrize("restart_ok,health_ok", [(False, True), (True, False), (True, True)])
def test_manager_update_requires_restart_and_health(tmp_path, restart_ok, health_ok):
    for name in ("ZcodeGateway", "manager", "unit", "state"):
        (tmp_path / name).write_text("old-" + name)
    (tmp_path / "config.yaml").write_text("config-kept")
    result = _shell(tmp_path, f"""
ZGW_INSTALL_DIR="$PWD"; ZGW_FROM_SOURCE=""; SERVICE=zcodegateway
MANAGER_BIN="$PWD/manager"; UNIT_FILE="$PWD/unit"; ENV_FILE="$PWD/state"
UPDATE_LOCK="$PWD/update.lock"; LOG_FILE="$PWD/log"; ZGW_SKIP_SYSTEMD=0
update_tmpdir() {{ mktemp -d "$PWD/stage.XXXXXXXX"; }}
stage_release() {{ printf new-binary > "$1/ZcodeGateway"; printf new-manager > "$1/install-linux.sh"; }}
need_root() {{ :; }}
is_installed() {{ return 0; }}
ensure_service_user() {{ :; }}
chown() {{ :; }}
chmod() {{ :; }}
fix_ownership() {{ :; }}
systemctl() {{
    echo "$*" >> "$PWD/systemctl.log"
    if [[ "$1" == restart && "$(command cat "$PWD/ZcodeGateway")" == new-binary ]]; then
        return {0 if restart_ok else 1}
    fi
    return 0
}}
curl() {{
    [[ "$(command cat "$PWD/ZcodeGateway")" == old-ZcodeGateway ]] && return 0
    return {0 if health_ok else 22}
}}
sleep() {{ :; }}
gw_health_url() {{ echo https://127.0.0.1:8443/health; }}
cmd_update
""")
    assert (result.returncode == 0) is (restart_ok and health_ok)
    success = restart_ok and health_ok
    assert (tmp_path / "ZcodeGateway").read_text() == ("new-binary" if success else "old-ZcodeGateway")
    assert (tmp_path / "manager").read_text() == ("new-manager" if success else "old-manager")
    assert (tmp_path / "unit").read_text() == "old-unit"
    assert (tmp_path / "state").read_text() == "old-state"
    assert (tmp_path / "config.yaml").read_text() == "config-kept"
    assert not list(tmp_path.glob("stage.*"))
    if not (restart_ok and health_ok):
        assert "health check passed" not in result.stdout
        assert "restored" in result.stdout
        assert (tmp_path / "systemctl.log").read_text().endswith("daemon-reload\nrestart zcodegateway\n")


def test_release_staging_pins_assets_and_rejects_duplicate_checksums(tmp_path):
    result = _shell(tmp_path, r'''
LOG_FILE="$PWD/log"
uname() { echo x86_64; }
release_runtime_compatible() { :; }
validate_gateway() { :; }
curl() {
    echo "$*" >> "$PWD/requests"
    local out="" url="${@: -1}"
    while (( $# )); do
        if [[ "$1" == -o ]]; then out="$2"; shift; fi
        shift
    done
    case "$url" in
      */releases/latest) printf 'https://github.com/B3hnamR/AutoZcoder/releases/tag/v2.0.0' ;;
      */checksums-linux.sha256)
        printf '%s  ZcodeGateway\n%s  install-linux.sh\n' \
          "$(printf binary | sha256sum | cut -d' ' -f1)" \
          "$(printf '#!/bin/bash\n' | sha256sum | cut -d' ' -f1)" > "$out" ;;
      */ZcodeGateway) printf binary > "$out" ;;
      */install-linux.sh) printf '#!/bin/bash\n' > "$out" ;;
      *) return 99 ;;
    esac
}
mkdir stage
stage_release "$PWD/stage"
printf '%064d  ZcodeGateway\n%064d  ZcodeGateway\n' 0 0 > "$PWD/stage/checksums-linux.sha256"
if verified_asset "$PWD/stage" ZcodeGateway; then exit 91; fi
''')
    assert result.returncode == 0, result.stderr
    requests = (tmp_path / "requests").read_text().splitlines()
    assert len(requests) == 4
    assert requests[0].endswith("/releases/latest")
    assert all("/releases/download/v2.0.0/" in request for request in requests[1:])


def test_update_staging_failure_never_stops_or_changes_install(tmp_path):
    (tmp_path / "ZcodeGateway").write_text("original")
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; ZGW_FROM_SOURCE=""; UPDATE_LOCK="$PWD/lock"
need_root() { :; }; is_installed() { return 0; }
update_tmpdir() { mktemp -d "$PWD/stage.XXXXXXXX"; }
stage_release() { return 1; }
systemctl() { echo UNEXPECTED_STOP; return 91; }
cmd_update
''')
    assert result.returncode != 0
    assert "UNEXPECTED_STOP" not in result.stdout
    assert (tmp_path / "ZcodeGateway").read_text() == "original"
    assert not list(tmp_path.glob("stage.*"))


def test_atomic_install_replaces_symlink_without_writing_target(tmp_path):
    (tmp_path / "source").write_text("new")
    (tmp_path / "sentinel").write_text("preserved")
    (tmp_path / "destination").symlink_to(tmp_path / "sentinel")
    result = _shell(tmp_path, 'atomic_install "$PWD/source" "$PWD/destination" 755')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sentinel").read_text() == "preserved"
    assert not (tmp_path / "destination").is_symlink()
    assert (tmp_path / "destination").read_text() == "new"


def test_manual_update_preserves_mode_and_does_not_use_systemd(tmp_path):
    for name in ("ZcodeGateway", "manager", "state"):
        (tmp_path / name).write_text("old")
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; ZGW_FROM_SOURCE=""; ZGW_SKIP_SYSTEMD=1
MANAGER_BIN="$PWD/manager"; UNIT_FILE="$PWD/unit"; ENV_FILE="$PWD/state"; UPDATE_LOCK="$PWD/lock"
need_root() { :; }; is_installed() { return 0; }; ensure_service_user() { :; }
chown() { :; }; chmod() { :; }; fix_ownership() { :; }
update_tmpdir() { mktemp -d "$PWD/stage.XXXXXXXX"; }
stage_release() { printf new > "$1/ZcodeGateway"; printf manager > "$1/install-linux.sh"; }
systemctl() { echo UNEXPECTED_SYSTEMD; return 91; }
cmd_update
''')
    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED_SYSTEMD" not in result.stdout
    assert (tmp_path / "ZcodeGateway").read_text() == "new"
    assert not (tmp_path / "unit").exists()
    assert not list(tmp_path.glob("stage.*"))


@pytest.mark.parametrize("fault", ["activate", "manager", "signal"])
def test_update_rolls_back_partial_activation(tmp_path, fault):
    for name in ("ZcodeGateway", "manager", "unit", "state"):
        (tmp_path / name).write_text("old-" + name)
    result = _shell(tmp_path, f'''
ZGW_INSTALL_DIR="$PWD"; ZGW_FROM_SOURCE=""; ZGW_SKIP_SYSTEMD=0
MANAGER_BIN="$PWD/manager"; UNIT_FILE="$PWD/unit"; ENV_FILE="$PWD/state"; UPDATE_LOCK="$PWD/lock"
need_root() {{ :; }}; is_installed() {{ return 0; }}; ensure_service_user() {{ :; }}
chown() {{ :; }}; chmod() {{ :; }}; fix_ownership() {{ :; }}
update_tmpdir() {{ mktemp -d "$PWD/stage.XXXXXXXX"; }}
stage_release() {{ printf new > "$1/ZcodeGateway"; printf manager > "$1/install-linux.sh"; }}
# Inject failures only in activation, not the restore path.
atomic_install_original="$(declare -f atomic_install)"
eval "${{atomic_install_original/atomic_install ()/real_atomic_install ()}}"
atomic_install() {{
    if [[ '{fault}' == activate && "$1" == */stage.*/ZcodeGateway ]]; then return 73; fi
    real_atomic_install "$@"
}}
fetch_manager_verified() {{
    if [[ '{fault}' == signal ]]; then kill -TERM "$BASHPID"; fi
    [[ '{fault}' != manager ]] || return 74
    atomic_install "$2/install-linux.sh" "$1" 755
}}
systemctl() {{ echo "$*" >> "$PWD/systemctl.log"; }}
curl() {{ return 0; }}; sleep() {{ :; }}
gw_health_url() {{ echo https://127.0.0.1:8443/health; }}
cmd_update
''')
    assert result.returncode != 0, result.stderr
    for name in ("ZcodeGateway", "manager", "unit", "state"):
        assert (tmp_path / name).read_text() == "old-" + name
    assert not list(tmp_path.glob("stage.*"))


def test_state_roundtrip_quotes_values_and_preserves_manual_mode(tmp_path):
    result = _shell(tmp_path, r'''
ENV_FILE="$PWD/state"
ZGW_INSTALL_DIR="$PWD/path with ' quote"
ZGW_PORT=9443; ZGW_DOMAIN=""; ZGW_SERVER_IP=192.0.2.1
ZGW_SKIP_SYSTEMD=1; ZGW_RUN_AS_ROOT=1; ZGW_FROM_SOURCE="/source with ' quote"
save_state
unset ZGW_INSTALL_DIR ZGW_SKIP_SYSTEMD ZGW_RUN_AS_ROOT ZGW_FROM_SOURCE
resolve_config
[[ "$ZGW_INSTALL_DIR" == "$PWD/path with ' quote" ]]
[[ "$ZGW_FROM_SOURCE" == "/source with ' quote" ]]
[[ "$ZGW_SKIP_SYSTEMD" == 1 && "$ZGW_RUN_AS_ROOT" == 1 ]]
''')
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o600


@pytest.mark.parametrize("kind,success", [("valid", True), ("small", False), ("pe", False), ("arm", False)])
def test_gateway_validation_rejects_wrong_format_size_and_architecture(tmp_path, kind, success):
    payload = bytearray(20 if kind == "small" else 1_000_001)
    payload[:7] = b"\x7fELF\x02\x01\x01"
    payload[16:18] = (3).to_bytes(2, "little")
    payload[18:20] = (183 if kind == "arm" else 62).to_bytes(2, "little")
    if kind == "pe":
        payload[:2] = b"MZ"
    (tmp_path / "candidate").write_bytes(payload)
    result = _shell(tmp_path, 'uname() { echo x86_64; }; validate_gateway "$PWD/candidate"')
    assert (result.returncode == 0) is success, result.stderr


def test_download_does_not_follow_old_predictable_staging_symlinks(tmp_path):
    install = tmp_path / "installed"
    install.mkdir()
    (tmp_path / "sentinel").write_text("preserved")
    for name in ("checksums-linux.sha256", "ZcodeGateway.new", "ZcodeGateway"):
        (install / name).symlink_to(tmp_path / "sentinel")
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD/installed"
ensure_service_user() { :; }; chown() { :; }; chmod() { :; }
update_tmpdir() { mktemp -d "$PWD/stage.XXXXXXXX"; }
stage_release() { printf new > "$1/ZcodeGateway"; }
download_release
''')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sentinel").read_text() == "preserved"
    assert (install / "ZcodeGateway").read_text() == "new"
    assert (install / "checksums-linux.sha256").is_symlink()
    assert (install / "ZcodeGateway.new").is_symlink()


def test_release_failure_never_silently_builds_local_checkout(tmp_path):
    build_path = tmp_path / "scripts" / "build" / "linux.sh"
    build_path.parent.mkdir(parents=True)
    build_path.write_text("# untrusted local source")
    result = _shell(tmp_path, r'''
SELF="$PWD/install-linux.sh"; ZGW_FROM_SOURCE=""
download_release() { return 1; }
build_from_source() { echo UNEXPECTED_SOURCE_BUILD; }
download_binary
''')
    assert result.returncode != 0
    assert "UNEXPECTED_SOURCE_BUILD" not in result.stdout


def test_release_staging_rejects_unsupported_arch_before_network(tmp_path):
    result = _shell(tmp_path, r'''
uname() { echo aarch64; }
curl() { echo UNEXPECTED_NETWORK; return 99; }
stage_release "$PWD"
''')
    assert result.returncode != 0
    assert "UNEXPECTED_NETWORK" not in result.stdout


def test_update_lock_rejects_second_transaction_before_staging(tmp_path):
    result = _shell(tmp_path, r'''
need_root() { :; }; is_installed() { return 0; }
UPDATE_LOCK="$PWD/lock"
exec 9>"$UPDATE_LOCK"
flock -n 9
update_tmpdir() { echo UNEXPECTED_STAGE >&2; return 99; }
cmd_update
''')
    assert result.returncode != 0
    assert "another update is in progress" in result.stderr
    assert "UNEXPECTED_STAGE" not in result.stderr


@pytest.mark.parametrize("libc,success", [("glibc 2.35", True), ("glibc 2.39", True), ("glibc 2.31", False), ("musl", False)])
def test_release_glibc_baseline_checked_before_download(tmp_path, libc, success):
    result = _shell(tmp_path, f'getconf() {{ echo "{libc}"; }}; release_runtime_compatible')
    assert (result.returncode == 0) is success, result.stderr


def test_existing_generator_environment_failure_restores_old_install(tmp_path):
    for name in ("ZcodeGateway", "manager", "unit", "state"):
        (tmp_path / name).write_text("old-" + name)
    py = tmp_path / "genenv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 42\n")
    py.chmod(0o700)
    result = _shell(tmp_path, r'''
ZGW_INSTALL_DIR="$PWD"; ZGW_FROM_SOURCE=""; ZGW_SKIP_SYSTEMD=0; ZGW_RUN_AS_ROOT=1
MANAGER_BIN="$PWD/manager"; UNIT_FILE="$PWD/unit"; ENV_FILE="$PWD/state"; UPDATE_LOCK="$PWD/lock"; LOG_FILE="$PWD/log"
need_root() { :; }; is_installed() { return 0; }; ensure_service_user() { :; }
chown() { :; }; chmod() { :; }; fix_ownership() { :; }
update_tmpdir() { mktemp -d "$PWD/stage.XXXXXXXX"; }
stage_release() { printf new > "$1/ZcodeGateway"; printf manager > "$1/install-linux.sh"; }
systemctl() { :; }; curl() { return 0; }; sleep() { :; }
gw_health_url() { echo https://127.0.0.1:8443/health; }
cmd_update
''')
    assert result.returncode != 0
    assert "generator environment is incompatible" in result.stderr
    for name in ("ZcodeGateway", "manager", "unit", "state"):
        assert (tmp_path / name).read_text() == "old-" + name


def test_source_build_requires_sidecar_before_provisioning(tmp_path):
    build_path = tmp_path / "src" / "scripts" / "build" / "linux.sh"
    build_path.parent.mkdir(parents=True)
    build_path.write_text("# fixture")
    result = _shell(tmp_path, """
command() {
  if [[ "$1" == -v && "${2:-}" == bun ]]; then return 1; fi
  builtin command "$@"
}
run_quiet() { echo UNEXPECTED_PROVISIONING; return 99; }
build_from_source "$PWD/src"
""")
    assert result.returncode != 0
    assert "source build needs Bun" in result.stderr
    assert "UNEXPECTED_PROVISIONING" not in result.stdout


@pytest.mark.parametrize("cpus,memory,warning", [
    (1, 8 * 1048576, True), (4, 2 * 1048576, True),
    (1, 1048576, True), (0.5, 4 * 1048576, True),
    (2, 4 * 1048576, False), (4, 8 * 1048576, False),
])
def test_first_install_resource_warning_threshold(tmp_path, cpus, memory, warning):
    result = _shell(tmp_path, f"""
is_installed() {{ return 1; }}
server_resources() {{ echo '{cpus} {memory}'; }}
TTY_OK=1
confirm() {{ echo "PROMPT:$1 DEFAULT:$2" >&2; return 0; }}
check_first_install_resources
echo CONTINUED
""")
    assert result.returncode == 0
    assert ("PROMPT:" in result.stderr) is warning
    if warning:
        assert "Continue anyway?" in result.stderr
        assert "DEFAULT:n" in result.stderr
    assert "CONTINUED" in result.stdout


def test_declined_low_resource_install_stops_before_provisioning(tmp_path):
    result = _shell(tmp_path, """
is_installed() { return 1; }
server_resources() { echo '1 1900000'; }
TTY_OK=1
confirm() { return 1; }
check_first_install_resources
echo SHOULD_NOT_INSTALL
""")
    assert result.returncode == 0
    assert "Installation cancelled" in result.stdout
    assert "SHOULD_NOT_INSTALL" not in result.stdout


@pytest.mark.parametrize("override,skip,installed,accepted", [
    (False, False, False, False), (True, False, False, True),
    (False, True, False, True), (False, False, True, True),
])
def test_noninteractive_resource_warning_requires_explicit_acceptance(tmp_path, override, skip, installed, accepted):
    result = _shell(tmp_path, f"""
is_installed() {{ return {0 if installed else 1}; }}
server_resources() {{ echo '1 1900000'; }}
TTY_OK=0
ZGW_ALLOW_LOW_RESOURCES={int(override)}
ZGW_SKIP_GENERATOR={int(skip)}
confirm() {{ echo UNEXPECTED_PROMPT; return 99; }}
check_first_install_resources
echo CONTINUED
""")
    assert (result.returncode == 0) is accepted
    assert ("CONTINUED" in result.stdout) is accepted
    assert "UNEXPECTED_PROMPT" not in result.stdout
    if not accepted:
        assert "ZGW_ALLOW_LOW_RESOURCES=1" in result.stderr
