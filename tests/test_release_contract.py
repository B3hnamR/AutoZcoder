"""Local release-workflow contracts; never run a build or contact GitHub."""

import ast
import hashlib
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
    Loader=yaml.BaseLoader,
)
JOBS = WORKFLOW["jobs"]
PAYLOADS = {"ZcodeGateway.exe": b"dummy windows", "ZcodeGateway": b"dummy linux",
            "install-linux.sh": b"#!/bin/sh\nexit 0\n"}


def step(job, name):
    return next(item for item in JOBS[job]["steps"] if item.get("name") == name)


def python_blocks(run):
    """Extract only embedded Python, so tests cannot invoke gh or build commands."""
    return [part.split("\nPY", 1)[0]
            for part in run.split("<<'PY'\n")[1:]]


def execute(script):
    exec(compile(script, "<release-workflow>", "exec"), {})


def make_assets(tmp_path):
    root = tmp_path / "release-assets"
    root.mkdir()
    for name, payload in PAYLOADS.items():
        (root / name).write_bytes(payload)
    for manifest, names in {
        "checksums.sha256": ["ZcodeGateway.exe"],
        "checksums-linux.sha256": ["ZcodeGateway", "install-linux.sh"],
    }.items():
        (root / manifest).write_text("".join(
            f"{hashlib.sha256(PAYLOADS[name]).hexdigest()}  {name}\n" for name in names
        ), encoding="ascii")
    return root


def test_tag_only_publication_and_least_privilege():
    # BaseLoader preserves the literal key `on`, unlike YAML 1.1 safe_load.
    assert WORKFLOW["on"] == {"push": {"tags": ["v*"]}, "workflow_dispatch": ""}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert WORKFLOW["concurrency"] == {
        "group": "release-${{ github.ref }}", "cancel-in-progress": "false",
    }
    assert JOBS["test"]["needs"] == "validate"
    assert JOBS["publish"]["needs"] == ["build-windows", "build-linux"]
    assert JOBS["publish"]["permissions"] == {"contents": "write"}
    for name, job in JOBS.items():
        if name != "publish":
            assert "permissions" not in job
            assert "GH_TOKEN" not in job.get("env", {})
        for item in job["steps"]:
            assert "softprops/action-gh-release" not in item.get("uses", "")
            if item.get("uses", "").startswith("actions/checkout@"):
                assert item["with"]["persist-credentials"] == "false"
            if "gh release" in item.get("run", ""):
                assert name == "publish"


def test_both_platform_test_gates_install_and_require_chromium():
    test = JOBS["test"]
    assert test["strategy"]["fail-fast"] == "false"
    matrix = test["strategy"]["matrix"]["include"]
    assert {(row["os"], row["python-version"]) for row in matrix} == {
        ("windows-latest", "3.13"), ("ubuntu-22.04", "3.12"),
    }
    assert matrix[0]["requirements"] == "requirements/windows.txt"
    assert matrix[1]["requirements"] == "requirements/server.txt"
    install = step("test", "Create venv and install runtime and UI test dependencies")["run"]
    assert "requirements/dev.txt playwright Pillow PySocks cryptography" in install
    assert "playwright install chromium" in step("test", "Install Chromium (Windows)")["run"]
    assert "playwright install --with-deps chromium" in step(
        "test", "Install Chromium and system libraries (Linux)")["run"]
    preflight = step("test", "Require a working browser before running UI tests")["run"]
    assert "runtime.chromium.launch()" in preflight
    assert "except" not in preflight
    assert "pytest tests -q --junitxml=release-tests.xml" in step("test", "Run tests")["run"]


@pytest.mark.parametrize("reason", ["Installed Chromium unavailable", "No module named playwright"])
def test_browser_skips_fail_the_release(tmp_path, monkeypatch, reason):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "release-tests.xml").write_text(
        f'<testsuites><testsuite><testcase classname="tests.test_future_ui" name="test_ui">'
        f'<skipped message="{reason}"/></testcase></testsuite></testsuites>'
    )
    script = python_blocks(step("test", "Reject skipped browser tests")["run"])[0]
    with pytest.raises(SystemExit, match="Browser tests must not skip"):
        execute(script)


def test_platform_specific_non_browser_skips_are_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "release-tests.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_review_fixes" name="test_posix">'
        '<skipped message="POSIX only"/></testcase></testsuite></testsuites>'
    )
    execute(python_blocks(step("test", "Reject skipped browser tests")["run"])[0])


def test_builds_only_upload_explicit_release_assets_after_smoke():
    expected = {
        "build-windows": {".local/dist/ZcodeGateway.exe", ".local/dist/checksums.sha256"},
        "build-linux": {".local/dist/ZcodeGateway", ".local/dist/checksums-linux.sha256", ".local/dist/install-linux.sh"},
    }
    for name, paths in expected.items():
        job = JOBS[name]
        assert job["needs"] == "test"
        uploads = [s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")]
        assert len(uploads) == 1
        upload = uploads[0]
        assert upload["with"]["if-no-files-found"] == "error"
        assert set(upload["with"]["path"].splitlines()) == paths
        smoke = next(s for s in job["steps"] if "scripts/smoke/frozen_local.py" in s.get("run", "")
                         or "scripts\\smoke\\frozen_local.py" in s.get("run", ""))
        assert "AUDIT_BINARY" in smoke["run"]
        assert job["steps"].index(smoke) < job["steps"].index(upload)
        bun = next(s for s in job["steps"] if s.get("uses", "").startswith("oven-sh/setup-bun@"))
        assert bun["with"]["bun-version"] == "1.3.14"
    assert JOBS["build-linux"]["runs-on"] == "ubuntu-22.04"
    linux_checks = step("build-linux", "Checksum Linux release assets (sidecar is embedded, not uploaded)")["run"]
    assert "sha256sum ZcodeGateway install-linux.sh" in linux_checks
    assert "sha256sum tools/captcha-mint" not in linux_checks
    baseline = step("build-linux", "Verify the native sidecar and runtime library baseline")["run"]
    assert "ldd" in baseline and "not found" in baseline


@pytest.mark.parametrize("mutation", [None, "branch", "tag", "numeric", "string"])
def test_version_gate_checks_tag_and_all_executable_metadata(tmp_path, monkeypatch, mutation):
    monkeypatch.chdir(tmp_path)
    for name in ("zcode_gateway", "scripts/packaging"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "zcode_gateway/__init__.py").write_text('__version__ = "2.0.0"\n')
    metadata = """VSVersionInfo(
        ffi=FixedFileInfo(filevers=(2, 0, 0, 0), prodvers=(2, 0, 0, 0)),
        kids=[StringStruct('FileVersion', '2.0.0.0'),
              StringStruct('ProductVersion', '2.0.0.0')])"""
    if mutation == "numeric":
        metadata = metadata.replace("filevers=(2, 0, 0, 0)", "filevers=(1, 0, 0, 0)")
    if mutation == "string":
        metadata = metadata.replace("'ProductVersion', '2.0.0.0'", "'ProductVersion', '1.0.0.0'")
    (tmp_path / "scripts/packaging/exe_version.txt").write_text(metadata)
    monkeypatch.setenv("RELEASE_REF_TYPE", "branch" if mutation == "branch" else "tag")
    monkeypatch.setenv("RELEASE_TAG", "v2.0.1" if mutation == "tag" else "v2.0.0")
    script = python_blocks(step("validate", "Require a version tag matching app and executable metadata")["run"])[0]
    if mutation is None:
        execute(script)
    else:
        with pytest.raises(SystemExit):
            execute(script)


@pytest.mark.parametrize("mutation", [None, "missing", "extra", "changed", "duplicate", "sidecar", "traversal", "empty"])
def test_artifact_verifier_rejects_incomplete_or_invalid_assets(tmp_path, monkeypatch, mutation):
    monkeypatch.chdir(tmp_path)
    root = make_assets(tmp_path)
    manifest = root / "checksums-linux.sha256"
    if mutation == "missing":
        (root / "install-linux.sh").unlink()
    elif mutation == "extra":
        (root / "credentials.json").write_text("dummy")
    elif mutation == "changed":
        (root / "ZcodeGateway").write_bytes(b"wrong payload")
    elif mutation == "duplicate":
        manifest.write_text(manifest.read_text() * 2)
    elif mutation == "sidecar":
        manifest.write_text(manifest.read_text() + f"{'0' * 64}  captcha-mint\n")
    elif mutation == "traversal":
        manifest.write_text(f"{'0' * 64}  ../ZcodeGateway\n")
    elif mutation == "empty":
        (root / "checksums.sha256").write_text("")
    script = python_blocks(step("publish", "Verify the complete artifact set and every payload checksum")["run"])[0]
    if mutation is None:
        execute(script)
    else:
        with pytest.raises(SystemExit):
            execute(script)


def test_publish_orders_draft_upload_remote_verification_then_visibility():
    steps = JOBS["publish"]["steps"]
    names = [item.get("name") for item in steps]
    required = ["Verify the complete artifact set and every payload checksum",
                "Create or resume a draft without modifying a public release",
                "Upload all five assets while the release remains a draft",
                "Verify uploaded assets before publishing",
                "Publish only the fully verified draft"]
    assert [names.index(name) for name in required] == sorted(names.index(name) for name in required)
    downloads = [item["with"]["name"] for item in steps
                 if item.get("uses", "").startswith("actions/download-artifact@")]
    assert downloads == ["release-windows", "release-linux"]
    draft = step("publish", required[1])["run"]
    assert "gh api --paginate" in draft
    assert "--verify-tag --draft --generate-notes" in draft
    assert "Refusing to overwrite existing public release" in draft
    assert "glibc 2.35" in draft
    assert "gh release delete" not in draft
    assert "gh release download" in step("publish", required[3])["run"]
    assert steps[-1]["run"] == 'gh release edit "$RELEASE_TAG" --draft=false'


@pytest.mark.parametrize("mutation", [None, "public", "missing", "changed"])
def test_uploaded_release_must_remain_draft_and_match_local_bytes(tmp_path, monkeypatch, mutation):
    monkeypatch.chdir(tmp_path)
    root = make_assets(tmp_path)
    remote = tmp_path / "uploaded-assets"
    remote.mkdir()
    for local in root.iterdir():
        (remote / local.name).write_bytes(local.read_bytes())
    names = [p.name for p in root.iterdir()]
    if mutation == "missing":
        names.remove("ZcodeGateway.exe")
    if mutation == "changed":
        (remote / "ZcodeGateway.exe").write_bytes(b"damaged upload")
    (tmp_path / "uploaded-release.json").write_text(json.dumps({
        "isDraft": mutation != "public", "assets": [{"name": name} for name in names],
    }))
    scripts = python_blocks(step("publish", "Verify uploaded assets before publishing")["run"])
    if mutation is None:
        for script in scripts:
            execute(script)
    else:
        with pytest.raises(SystemExit):
            for script in scripts:
                execute(script)


def test_all_embedded_python_is_valid():
    for job in JOBS.values():
        for item in job["steps"]:
            for script in python_blocks(item.get("run", "")):
                ast.parse(script)
