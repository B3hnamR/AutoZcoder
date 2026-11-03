"""Commit hygiene contracts use path strings, never create real secret files."""
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_secret_variants_and_binaries_are_ignored_but_examples_visible():
    excluded=['.env.production','nested/.env.local','mail-worker/.dev.vars',
              'mail-worker/.dev.vars.production','tools/captcha-mint/captcha-mint',
              'tools/captcha-mint/captcha-mint.exe','ZcodeGateway.new.exe.sha256']
    result=subprocess.run(['git','check-ignore','--stdin'],cwd=ROOT,
                          input=('\n'.join(excluded)+'\n').encode(),capture_output=True,check=False)
    assert set(result.stdout.decode().splitlines())==set(excluded)
    for example in ['.env.example','.env.sample']:
        assert subprocess.run(['git','check-ignore',example],cwd=ROOT,
                              capture_output=True).returncode==1


def test_proxy_source_override_is_optional_not_machine_specific():
    text=(ROOT/'generator/proxy_pool.py').read_text(encoding='utf-8')
    assert "os.environ.get('KICKVIEW_SOURCES', '')" in text
    assert 'D:\\JB\\' not in text


def test_sidecar_regressions_are_ci_and_release_gates():
    import json
    package=json.loads((ROOT/'tools/captcha-mint/package.json').read_text())
    assert package['scripts']['test'].startswith('bun test ')
    assert 'audit.test.ts' in package['scripts']['test']
    for workflow in ['ci.yml','release.yml']:
        text=(ROOT/'.github/workflows'/workflow).read_text()
        assert 'bun run test' in text
        assert 'bun install --frozen-lockfile' in text


def test_local_outputs_ignored_but_canonical_build_scripts_visible():
    for name in ('.local/runtime/config.yaml','.local/dist/ZcodeGateway.exe','.local/build/specs/ZcodeGateway.spec'):
        assert subprocess.run(['git','check-ignore',name],cwd=ROOT,capture_output=True).returncode==0
    for name in ('scripts/build/windows.ps1','scripts/build/linux.sh',
                 'scripts/packaging/stage_mail_worker.py','scripts/accounts/import_account.py','requirements/server.txt'):
        assert (ROOT/name).is_file()
        assert subprocess.run(['git','check-ignore',name],cwd=ROOT,capture_output=True).returncode==1


def test_source_checkout_bootstrap_isolated_from_repository_root(tmp_path,monkeypatch):
    from zcode_gateway import paths
    import yaml
    checkout=tmp_path/'checkout';checkout.mkdir();(checkout/'.git').mkdir()
    monkeypatch.setattr(paths,'app_dir',lambda:checkout)
    monkeypatch.setattr(paths.sys,'frozen',False,raising=False)
    monkeypatch.setenv('ZCODE_GATEWAY_CONFIG','')
    config=paths.bootstrap_config()
    assert config==checkout/'.local/runtime/config.yaml'
    assert not (checkout/'config.yaml').exists()
    assert (config.parent/'accounts').is_dir()
    assert yaml.safe_load(config.read_text())['server']['admin_key']
