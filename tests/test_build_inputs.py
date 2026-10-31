"""Exercise packaging inputs only: no builds, installs, deployments or network."""

import ast
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_BUILD = "scripts/build/windows.ps1"
LINUX_BUILD = "scripts/build/linux.sh"
HELPER = ROOT / "scripts/packaging/stage_mail_worker.py"
PRISTINE_CONFIG = '''name = "zai-mail"
main = "src/index.js"
compatibility_date = "2025-01-01"

[[kv_namespaces]]
binding = "MAIL"
id = "00000000000000000000000000000000"
'''
SECRET = "DO_NOT_PACKAGE_LOCAL_SECRET_314159"


@pytest.fixture
def worker(tmp_path):
    source = tmp_path / "project with spaces" / "mail-worker"
    inputs = {
        "package.json": '{"name":"test-worker","type":"module"}',
        "README.md": "Worker source documentation",
        "src/index.js": "export default {};",
        "src/utils.js": "export const value = 1;",
        "wrangler.toml": f'account_id = "{SECRET}"\n[vars]\nTOKEN = "{SECRET}"',
        ".dev.vars": SECRET,
        ".dev.vars.production": SECRET,
        ".env": SECRET,
        ".wrangler/state/local.sqlite": SECRET,
        "node_modules/dependency/index.js": SECRET,
        "package-lock.json": SECRET,
        "credentials.json": SECRET,
        "src/.env": SECRET,
        "src/index.js.map": SECRET,
        "src/nested/private.js": SECRET,
    }
    for name, content in inputs.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return source


def staging_command(script_name, source, destination):
    """Extract only the real helper call; never execute the surrounding build."""
    script = (ROOT / script_name).read_text(encoding="utf-8")
    lines = [line.strip() for line in script.splitlines() if "stage_mail_worker.py" in line]
    assert len(lines) == 1
    if script_name == WINDOWS_BUILD:
        match = re.fullmatch(
            r'& \$py \(Join-Path \$Here "([^"]+)"\) '
            r'\(Join-Path \$Here "([^"]+)"\) \$mailStage', lines[0])
        assert match
        helper, worker_path = [value.replace("\\", "/") for value in match.groups()]
    else:
        python, helper, worker_path, stage = shlex.split(lines[0])
        assert python == "$PY" and stage == "$MAIL_STAGE"
    assert worker_path == "mail-worker"
    assert ROOT / helper == HELPER
    return [sys.executable, "-B", str(ROOT / helper), str(source), str(destination)]


@pytest.mark.parametrize("script_name", [WINDOWS_BUILD, LINUX_BUILD])
def test_build_stages_only_whitelisted_inputs(script_name, worker, tmp_path):
    destination = tmp_path / "empty stage"
    destination.mkdir()
    before = {p.relative_to(worker): p.read_bytes() for p in worker.rglob("*") if p.is_file()}
    subprocess.run(staging_command(script_name, worker, destination), check=True,
                   capture_output=True, text=True, timeout=15)
    packaged = {p.relative_to(destination).as_posix(): p.read_bytes()
                for p in destination.rglob("*") if p.is_file()}
    assert set(packaged) == {"package.json", "README.md", "src/index.js", "src/utils.js", "wrangler.toml"}
    assert packaged.pop("wrangler.toml").decode("utf-8") == PRISTINE_CONFIG
    for name, content in packaged.items():
        assert content == before[Path(name)]
        assert SECRET.encode() not in content
    assert before == {p.relative_to(worker): p.read_bytes() for p in worker.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mutation", ["missing-entry", "missing-package", "dirty-stage", "linked-input"])
def test_staging_fails_closed(worker, tmp_path, mutation):
    destination = tmp_path / "stage"
    if mutation == "missing-entry":
        (worker / "src/index.js").unlink()
    elif mutation == "missing-package":
        (worker / "package.json").unlink()
    elif mutation == "dirty-stage":
        destination.mkdir()
        (destination / ".dev.vars").write_text(SECRET, encoding="utf-8")
    else:
        (worker / "src/utils.js").unlink()
        try:
            (worker / "src/utils.js").symlink_to(worker / ".dev.vars")
        except (OSError, NotImplementedError):
            pytest.skip("Creating symlinks is unavailable on this host")
    result = subprocess.run(staging_command(LINUX_BUILD, worker, destination),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert not (destination / "wrangler.toml").exists()


def test_builds_bundle_stages_not_live_worker_tree():
    windows = (ROOT / WINDOWS_BUILD).read_text(encoding="utf-8")
    linux = (ROOT / LINUX_BUILD).read_text(encoding="utf-8")
    assert '"--add-data", "$mailStage;mail-worker"' in windows
    assert '--add-data "$MAIL_STAGE:mail-worker"' in linux
    assert '"--add-data", "$genStage;generator"' in windows
    assert '--add-data "$GEN_STAGE:generator"' in linux
    assert 'Copy-Item -Path (Join-Path $Here "generator\\*.py") -Destination $genStage' in windows
    for script in (windows, linux):
        data_lines = [line for line in script.splitlines() if "--add-data" in line]
        worker_lines = [line for line in data_lines if "mail-worker" in line]
        assert len(worker_lines) == 1
        assert "Copy-Item" not in "\n".join(line for line in script.splitlines() if "mail-worker" in line)
    assert windows.index("if ($LASTEXITCODE -ne 0)", windows.index("stage_mail_worker.py")) < windows.index("$buildArgs =")
    trap = next(line for line in linux.splitlines() if line.startswith("trap "))
    assert 'rm -rf "$GEN_STAGE"' in trap and 'rm -rf "$MAIL_STAGE"' in trap and "EXIT" in trap
    assert linux.index(trap) < linux.index('MAIL_STAGE="$(mktemp') < linux.index("stage_mail_worker.py") < linux.index('"$PY" -m PyInstaller')
    assert not any("rm -rf" in line for line in linux.splitlines()[linux.splitlines().index(trap) + 1:])


def test_relocated_builds_resolve_root_and_keep_outputs_local():
    windows = (ROOT / WINDOWS_BUILD).read_text(encoding="utf-8")
    linux = (ROOT / LINUX_BUILD).read_text(encoding="utf-8")
    assert 'Join-Path $PSScriptRoot "..\\.."' in windows
    assert "Set-Location $Here" in windows
    assert 'realpath -- "${BASH_SOURCE[0]}"' in linux
    assert '$(dirname -- "$SCRIPT_PATH")/../..' in linux
    assert 'cd -- "$ROOT"' in linux
    assert 'Join-Path $Here "requirements\\windows.txt"' in windows
    assert "-r requirements/server.txt" in linux
    assert 'Join-Path $Here "scripts\\packaging\\exe_version.txt"' in windows
    assert '--add-data "$ROOT/config.example.yaml:."' in linux
    assert '--add-data "$ROOT/zcode_gateway/dashboard:zcode_gateway/dashboard"' in linux
    assert '"$ROOT/zcode_gateway/main.py"' in linux
    for option, path in (("--workpath", ".local/build"),
                         ("--distpath", ".local/dist"),
                         ("--specpath", ".local/build/specs")):
        windows_path = path.replace("/", "\\")
        assert f'"{option}", (Join-Path $Here "{windows_path}")' in windows
        assert f"{option} {path}" in linux
    assert 'foreach ($f in @("README.md", "requirements.txt"))' in windows
    assert "for f in README.md requirements.txt; do" in linux
    for old_path in ("build-exe.ps1", "build-linux.sh", "tools/stage_mail_worker.py", "tools/exe_version.txt"):
        assert not (ROOT / old_path).exists()
    assert (ROOT / "scripts/packaging/exe_version.txt").is_file()


def test_builds_keep_known_native_sidecar_inputs():
    # Check specific source inputs, not local binaries or node_modules.
    for name in ("mint.ts", "package.json", "bun.lock"):
        assert (ROOT / "tools/captcha-mint" / name).is_file()
    windows = (ROOT / WINDOWS_BUILD).read_text(encoding="utf-8")
    linux = (ROOT / LINUX_BUILD).read_text(encoding="utf-8")
    assert '$mintDir = Join-Path $Here "tools\\captcha-mint"' in windows
    assert '"--add-data", "$mintExe;captcha-mint"' in windows
    assert 'MINT_DIR="tools/captcha-mint"' in linux
    assert '--add-binary "$ROOT/$MINT_BIN:captcha-mint"' in linux
    assert 'install -m 755 "$MINT_BIN" .local/dist/captcha-mint/captcha-mint' in linux


def test_generator_config_has_no_literal_key_defaults():
    # config.py is part of the existing source allowlist; keep keys runtime-only.
    tree = ast.parse((ROOT / "generator/config.py").read_text(encoding="utf-8"))
    config = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "CONFIG" for target in node.targets))
    values = {key.value: value for key, value in zip(config.keys, config.values)}
    assert ast.literal_eval(values["mail_inbox_secret"]) is None
    key_defaults = [node.args[1] for node in ast.walk(values["gateway_key"])
                    if isinstance(node, ast.Call) and len(node.args) == 2]
    assert len(key_defaults) == 2
    assert all(ast.literal_eval(value) == "" for value in key_defaults)


def test_staged_binding_matches_actual_worker_and_deployer(tmp_path):
    import tomllib
    destination = tmp_path / "worker"
    subprocess.run([sys.executable, str(HELPER), str(ROOT / "mail-worker"), str(destination)],
                   check=True, capture_output=True, timeout=15)
    binding = tomllib.loads((destination / "wrangler.toml").read_text())["kv_namespaces"][0]["binding"]
    assert binding == "MAIL"
    assert "env.MAIL" in (destination / "src/index.js").read_text()
