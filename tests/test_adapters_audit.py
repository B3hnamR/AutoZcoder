"""Focused regressions for AUD-025 through AUD-030; use dummy credentials only."""

import asyncio
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from zcode_gateway import accounts_dir, api_anthropic, api_openai, device_auth
from zcode_gateway import zcode_accounts


@pytest.mark.parametrize("ident", [r"x\..\..\owned", "x/../../owned", "..",
                                     "a" * 1000, "x:\\owned"])
def test_collision_filename_stays_a_bounded_basename(tmp_path, ident):
    primary = accounts_dir.safe_name("same@example.test", "first")
    used = {primary: "first"}
    name = accounts_dir.snapshot_filename("same@example.test", ident, used)
    assert name != primary
    assert PureWindowsPath(name).name == name
    assert len(name) < 240
    path = accounts_dir.snapshot_path(tmp_path, name)
    accounts_dir.write_snapshot(path, {"account_id": ident, "access_token": "dummy"})
    assert path.resolve().parent == tmp_path.resolve()


def test_filename_collision_handles_case_and_unreadable_owner():
    used = {"SAME@example.test.txt": "first"}
    name = accounts_dir.snapshot_filename("same@example.test", "second", used)
    assert name.casefold() != "same@example.test.txt"
    used[name] = None
    assert accounts_dir.snapshot_filename("same@example.test", "second", used) != name


@pytest.mark.parametrize("label", ["..", "CON", "nul.txt", "a" * 1000])
def test_safe_name_handles_empty_reserved_and_long_labels(tmp_path, label):
    name = accounts_dir.safe_name(label, "fallback")
    assert name != ".txt"
    assert len(name) < 240
    assert PureWindowsPath(name).name == name
    accounts_dir.write_snapshot(tmp_path / name, {"access_token": "dummy"})


@pytest.mark.parametrize("name", ["../owned.txt", r"..\owned.txt", "C:owned.txt",
                                    "/owned.txt", "..", "bad\x00.txt", "bad.txt "])
def test_snapshot_path_rejects_non_basenames(tmp_path, name):
    with pytest.raises(ValueError):
        accounts_dir.snapshot_path(tmp_path, name)


def test_snapshot_path_rejects_symlink(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("untouched", encoding="utf-8")
    folder = tmp_path / "accounts"
    folder.mkdir()
    link = folder / "alias.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ValueError):
        accounts_dir.snapshot_path(folder, link.name)
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_snapshot_creation_and_replacement_are_private(tmp_path, monkeypatch):
    path = tmp_path / "accounts" / "account.txt"
    replace = accounts_dir.os.replace
    observed = []

    def inspect_replace(source, destination):
        observed.append(stat.S_IMODE(Path(source).stat().st_mode))
        assert source != destination.with_name(destination.name + ".tmp")
        replace(source, destination)

    monkeypatch.setattr(accounts_dir.os, "replace", inspect_replace)
    old_umask = os.umask(0o022)
    try:
        accounts_dir.write_snapshot(path, {"access_token": "first-dummy"})
        path.chmod(0o644)
        accounts_dir.write_snapshot(path, {"access_token": "second-dummy"})
    finally:
        os.umask(old_umask)
    assert observed == [0o600, 0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert accounts_dir.parse_snapshot(path)["access_token"] == "second-dummy"
    assert not list(path.parent.glob(".snapshot-*.tmp"))


def test_snapshot_failed_replace_cleans_unique_temp(tmp_path, monkeypatch):
    path = tmp_path / "account.txt"
    path.write_text("old", encoding="utf-8")
    predictable = tmp_path / "account.txt.tmp"
    predictable.write_text("do not touch", encoding="utf-8")

    def fail_replace(*args):
        raise OSError("fixture replacement failure")

    monkeypatch.setattr(accounts_dir.os, "replace", fail_replace)
    with pytest.raises(OSError):
        accounts_dir.write_snapshot(path, {"access_token": "dummy"})
    assert path.read_text(encoding="utf-8") == "old"
    assert predictable.read_text(encoding="utf-8") == "do not touch"
    assert not list(tmp_path.glob(".snapshot-*.tmp"))


def test_import_ignores_corrupt_snapshot(tmp_path, monkeypatch):
    folder = tmp_path / "accounts"
    folder.mkdir()
    (folder / "broken.txt").write_bytes(b"\xff\xfe")
    assert accounts_dir.parse_snapshot(folder / "broken.txt") == {}
    monkeypatch.setattr(zcode_accounts, "read_credentials", lambda path: [
        {"account_id": "new", "email": "new@example.test", "access_token": "dummy"}])
    result = zcode_accounts.import_credentials("unused-fixture", folder)
    assert result["new_ids"] == ["new"]
    assert accounts_dir.parse_snapshot(folder / result["files"][0])["account_id"] == "new"


def test_import_untrusted_jwt_collision_stays_inside_accounts(tmp_path):
    folder = tmp_path / "accounts"
    folder.mkdir()
    accounts_dir.write_snapshot(folder / "same@example.test.txt",
                                {"account_id": "first", "access_token": "dummy"})
    claims = {"sub": r"x\..\..\owned", "exp": 100}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({
        "zcodejwttoken": f"e30.{payload}.dummy",
        "user": {"email": "same@example.test"},
    }), encoding="utf-8")
    result = zcode_accounts.import_credentials(credentials, folder)
    assert result["written"] == 1
    assert PureWindowsPath(result["files"][0]).name == result["files"][0]
    assert len(list(folder.glob("*.txt"))) == 2
    assert not (tmp_path / "owned.txt").exists()
    assert accounts_dir.parse_snapshot(folder / "same@example.test.txt")["account_id"] == "first"


def test_device_register_ignores_corrupt_snapshot(tmp_path, monkeypatch):
    (tmp_path / "broken.txt").write_bytes(b"\xff\xfe")
    monkeypatch.setattr(device_auth, "jwt_claims", lambda token: {"sub": "new", "exp": 100})
    login = device_auth.DeviceLogin(tmp_path, client=object(),
                                   fingerprint=device_auth.Fingerprint(device_mid="dummy-device"))
    account = asyncio.run(login._register({"token": "dummy", "user": {"email": "new@example.test"}}))
    assert account.id == "new"
    assert accounts_dir.parse_snapshot(login.snapshot_path)["account_id"] == "new"


def test_legacy_stream_preserves_error_and_closes_inner_source():
    closed = []
    error = {"error": {"message": "fixture interrupted", "code": "stream_interrupted"}}

    async def source():
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            frame = ("data: " + json.dumps(error) + "\n\n").encode()
            yield frame[:20]
            yield frame[20:]
            yield b"data: [DONE]\n\n"
        finally:
            closed.append(True)

    async def collect():
        return b"".join([chunk async for chunk in api_openai._legacy_completion_stream(source(), "fixture")])

    frames = [line[6:] for line in asyncio.run(collect()).decode().splitlines() if line.startswith("data: ")]
    assert json.loads(frames[0])["choices"][0]["text"] == "partial"
    assert json.loads(frames[1]) == error
    assert frames[2] == "[DONE]"
    assert closed == [True]


def test_legacy_stream_abandonment_closes_inner_source():
    closed = []

    async def source():
        try:
            while True:
                yield b'data: {"choices":[]}\n\n'
        finally:
            closed.append(True)

    async def abandon():
        stream = api_openai._legacy_completion_stream(source(), "fixture")
        await anext(stream)
        await stream.aclose()

    asyncio.run(abandon())
    assert closed == [True]


@pytest.mark.parametrize("stream", [False, True])
def test_legacy_forwards_sampling_stop_and_stream_cleanup(monkeypatch, stream):
    captured = []
    background = BackgroundTask(lambda: None)

    async def chat(request, body, key):
        captured.append(body)
        if stream:
            return StreamingResponse(iter([]), background=background)
        return JSONResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(api_openai, "chat_completions", chat)
    options = {"stop": ["END"], "temperature": 0, "top_p": 0.7, "top_k": 3}
    response = asyncio.run(api_openai.completions(None, {
        "prompt": "fixture", "stream": stream, **options}, "dummy-key"))
    assert {key: captured[0][key] for key in options} == options
    if stream:
        assert response.background is background


@pytest.mark.parametrize("options", [{"echo": True}, {"n": 2}, {"logprobs": 1},
                                       {"seed": 123}, {"prompt": ["a", "b"]},
                                       {"prompt": [123, 456]}])
def test_legacy_rejects_unsupported_semantics(options):
    result = asyncio.run(api_openai.completions(None, {"prompt": "fixture", **options}, "dummy"))
    assert result.status_code == 400
    assert "error" in json.loads(result.body)


def test_token_estimate_counts_assistant_tool_names_and_arguments(monkeypatch):
    state = SimpleNamespace(registry=SimpleNamespace(resolve=lambda *args: "fixture"))
    monkeypatch.setattr(api_anthropic, "get_state", lambda request: state)

    def estimate(size):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "dummy", "name": "search", "input": {"query": "x" * size}}]}]}
        result = asyncio.run(api_anthropic.count_tokens(None, body, "dummy"))
        expected = len("search") + len(json.dumps({"query": "x" * size}, ensure_ascii=False))
        assert result["input_tokens"] == max(1, expected // 4)
        return result["input_tokens"]

    assert estimate(4000) - estimate(0) == 1000


def test_mail_worker_mime_fixtures():
    """Run only within the coordinator's isolated Linux sandbox with Node present."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the mail-worker fixtures")
    link = "https://chat.z.ai/verify_email?token=dummy&next=fixture"

    def encoded(value):
        return base64.b64encode(value.encode()).decode()

    def part(content_type, encoding, body):
        return f"Content-Type: {content_type}\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n{body}"

    def multipart(*parts, boundary="fixture-boundary"):
        return (f'Content-Type: multipart/alternative;\r\n boundary="{boundary}"\r\n\r\n'
                + "\r\n".join(f"--{boundary}\r\n" + item for item in parts)
                + f"\r\n--{boundary}--\r\nignored epilogue")

    plain = part("text/plain", "base64", encoded(link))
    html = part("text/html; charset=utf-8", "base64", encoded(f'<a href="{link.replace("&", "&amp;")}">Verify</a>'))
    bad = part("text/plain", "base64", "%%% not base64 %%%")
    fixtures = [
        [plain, link],
        [multipart(plain), link],
        [multipart(html), link],
        [multipart(bad, html), link],
        [multipart(part("text/plain", "7bit", "welcome"), multipart(html, boundary="inner")), link],
        [part("text/plain", "quoted-printable", link.replace("=", "=3D").replace("verify_email", "verify_=\r\nemail")), link],
        [part("text/plain", "base64", encoded(link)[:20] + "\r\n" + encoded(link)[20:]), link],
        [multipart(bad), None],
        [part("application/octet-stream", "base64", encoded(link)), None],
        ["Content-Type: multipart/mixed\r\n\r\n" + link, None],
    ]
    worker = Path(__file__).resolve().parents[1] / "mail-worker" / "src" / "index.js"
    script = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const worker = (await import(process.argv[1])).default;
const fixtures = JSON.parse(readFileSync(0, 'utf8'));
for (const [raw, expected] of fixtures) {
  const records = new Map();
  const MAIL = {
    async put(key, value) { records.set(key, value); },
    async get(key) { return records.get(key) || null; },
  };
  const message = {
    to: 'fixture@example.test', from: 'sender@example.test',
    headers: new Headers({subject: 'fixture'}),
    raw: new ReadableStream({start(controller) {
      controller.enqueue(new TextEncoder().encode(raw)); controller.close();
    }}),
  };
  await worker.email(message, {MAIL}, {});
  const key = [...records.keys()].find(key => key.startsWith('msg:'));
  assert.ok(key, 'worker must store the fixture');
  assert.equal(JSON.parse(records.get(key)).link, expected);
}
"""
    subprocess.run([node, "--input-type=module", "-e", script, worker.as_uri()],
                   input=json.dumps(fixtures), text=True, check=True, timeout=20,
                   capture_output=True)
