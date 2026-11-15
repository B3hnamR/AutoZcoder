"""Regressions for the desktop client lifecycle, atomic config writes and the
live smoke script's teardown order."""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from zcode_gateway import gui

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# lifecycle: a user Stop wins over a pending restart
# --------------------------------------------------------------------------- #


class StubServer:
    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 8788
        self.error = None
        self.accounts_loaded = 0
        self.effective_accounts_dir = None
        self.started: list[Path | None] = []

    @property
    def running(self) -> bool:
        return False

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, cfg_path: Path | None) -> None:
        self.started.append(cfg_path)

    def stop(self, wait: float = 5.0) -> None:
        pass

    def wait_stopped(self, timeout: float = 10.0) -> bool:
        return True

    def wait_ready(self, timeout: float = 25.0) -> bool:
        return False


def flush_ui(pending):
    """Run the callbacks a worker thread posted, as the Tk main loop would."""
    while pending:
        func, args = pending.pop(0)
        func(*args)


@pytest.fixture(scope="module")
def tk_app(tmp_path_factory):
    # One Tk interpreter per process: destroying the last root and creating a
    # new one intermittently fails with "Can't find a usable tk.tcl".
    tkinter = pytest.importorskip("tkinter")
    pending: list[tuple] = []
    real_after = tkinter.Misc.after

    def after(self, ms, func=None, *args):
        # tkinter cannot marshal a call from a worker thread unless the real
        # main loop is running; queue it so the test thread runs it instead
        if threading.current_thread() is threading.main_thread():
            return real_after(self, ms, func, *args)
        assert func is not None
        pending.append((func, args))
        return ""

    cfg = tmp_path_factory.mktemp("gui") / "config.yaml"
    cfg.write_text(gui._default_config_yaml(), encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gui, "find_config", lambda: cfg)
        patch.setattr(gui, "messagebox", SimpleNamespace(
            showerror=lambda *a, **k: None, askyesno=lambda *a, **k: True))
        # the lifecycle under test never talks to a real gateway: keep the
        # background pollers from reaching this machine's own processes
        patch.setattr(gui.App, "poll", lambda self, force=False: None)
        patch.setattr(gui.App, "_drain_queue", lambda self: None)
        patch.setattr(gui.App, "_refresh_zcode_status", lambda self: None)
        patch.setattr(gui.App, "_check_client_reasoning", lambda self: None)
        patch.setattr(gui.App, "after", after)
        try:
            app = gui.App(autostart=False)
        except tkinter.TclError as exc:
            pytest.skip(f"no Tk display available: {exc}")
        app.withdraw()
        try:
            yield app, pending
        finally:
            app.destroy()


@pytest.fixture
def lifecycle_app(tk_app):
    app, pending = tk_app
    app._lifecycle = 0
    app._stopping = False
    app._restart_pending = False
    app._restart_message = None
    app._last_state = None
    app._last_avail = None
    app.server = StubServer()
    pending.clear()
    return app, pending


@pytest.fixture
def recording_threads(monkeypatch):
    threads: list[threading.Thread] = []
    real_thread = threading.Thread

    class Recording(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    monkeypatch.setattr(gui.threading, "Thread", Recording)
    return threads


def test_user_stop_cancels_pending_restart(lifecycle_app, recording_threads):
    app, pending = lifecycle_app
    release = threading.Event()
    app.server.wait_stopped = lambda timeout=10.0: release.wait(5.0)

    app.restart_server("Saved. Gateway restarted.")
    restart_thread, = recording_threads
    app.stop_everything()            # the user hits Stop while the restart waits
    release.set()
    restart_thread.join(5)
    assert not restart_thread.is_alive()

    flush_ui(pending)                # deliver whatever the restart thread queued
    assert app.server.started == []


def test_restart_still_restarts_after_a_clean_stop(lifecycle_app, recording_threads):
    app, pending = lifecycle_app

    app.restart_server("Saved. Gateway restarted.")
    restart_thread, = recording_threads
    restart_thread.join(5)
    assert not restart_thread.is_alive()

    flush_ui(pending)
    assert app.server.started == [app.cfg_path]
    assert app._stopping is False


# --------------------------------------------------------------------------- #
# atomic config writes
# --------------------------------------------------------------------------- #


def test_config_write_survives_a_midwrite_failure(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = "server:\n  admin_key: keep-me\n  client_keys:\n    - key: keep-me-too\n"
    path.write_text(original, encoding="utf-8")
    real_fdopen = os.fdopen

    def exploding_fdopen(fd, *args, **kwargs):
        stream = real_fdopen(fd, *args, **kwargs)

        class Failing:
            def write(self, text):
                stream.write(text[: len(text) // 2])
                stream.flush()
                raise OSError("disk full")

            def flush(self):
                stream.flush()

            def fileno(self):
                return stream.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                stream.close()
                return False

        return Failing()

    monkeypatch.setattr("os.fdopen", exploding_fdopen)

    with pytest.raises(OSError):
        gui._write_config_atomic(path, "server:\n  admin_key: new\n")

    assert path.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


def test_config_write_replaces_the_file_atomically(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("old", encoding="utf-8")
    real_replace = os.replace
    replaced = []

    def tracking_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", tracking_replace)

    gui._write_config_atomic(path, "new")

    assert [(src.parent, dst) for src, dst in replaced] == [(tmp_path, path)]
    assert replaced[0][0] != path
    assert path.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]


# --------------------------------------------------------------------------- #
# server controller: stop during startup, readiness against the right URL
# --------------------------------------------------------------------------- #


def _write_config(tmp_path: Path, port: int) -> Path:
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "server": {"host": "127.0.0.1", "port": port},
        "accounts": {"dir": str(accounts)},
    }), encoding="utf-8")
    return cfg


def test_stop_during_startup_prevents_binding(tmp_path, monkeypatch):
    import uvicorn

    from zcode_gateway import app as app_module

    entered = threading.Event()
    release = threading.Event()

    def slow_create_app(cfg):
        entered.set()
        assert release.wait(5)
        return object()

    monkeypatch.setattr(app_module, "create_app", slow_create_app)
    created = []

    class FakeServer:
        def __init__(self, config):
            created.append(config)

        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    controller = gui.ServerController()
    controller.start(_write_config(tmp_path, 9124))
    assert entered.wait(5)

    controller.stop(wait=0.1)        # uvicorn is still being imported/built
    release.set()

    assert controller.wait_stopped(5) is True
    assert created == []
    assert controller.error is None


class Healthy:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_wait_ready_needs_the_config_resolved(monkeypatch):
    controller = gui.ServerController()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: Healthy())

    # something answers /health on the default URL, but this controller never
    # resolved its own host/port, so the listener is not ours
    assert controller.wait_ready(timeout=0.2) is False


def test_wait_ready_probes_the_configured_port(tmp_path, monkeypatch):
    import uvicorn

    from zcode_gateway import app as app_module

    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(app_module, "create_app", lambda cfg: object())
    probed = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: (probed.append(url), Healthy())[1])

    controller = gui.ServerController()
    controller.start(_write_config(tmp_path, 9123))
    try:
        assert controller.wait_ready(timeout=5) is True
        assert probed[0] == "http://127.0.0.1:9123/health"
    finally:
        controller.stop()


# --------------------------------------------------------------------------- #
# live smoke script: drain the service before closing the store
# --------------------------------------------------------------------------- #


def _live_module():
    path = ROOT / "scripts" / "smoke" / "live.py"
    spec = importlib.util.spec_from_file_location("live_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_smoke_drains_the_service_before_closing_the_store(monkeypatch):
    live = _live_module()
    events = []

    class FakePool:
        def __init__(self, cfg, accounts):
            pass

    class FakeAccount:
        def __init__(self):
            self.id = "account-1"
            self.notes = {"quotas": {"GLM-5.3-Flash": {"remaining": 1, "total": 2}}}

    class FakeTokens:
        def __init__(self, cfg, pool):
            pass

        async def check_account(self, account):
            return {"ok": True}

        async def aclose(self):
            pass

    class FakeClient:
        def __init__(self, cfg):
            pass

        async def aclose(self):
            pass

    class FakeService:
        def __init__(self, *args):
            pass

        async def complete(self, payload, **kwargs):
            return SimpleNamespace(raw=json.dumps(
                {"stop_reason": "end_turn", "usage": {"input_tokens": 1}}))

        async def aclose(self):
            events.append("service.aclose")

    class FakeStore:
        def __init__(self, path):
            pass

        def close(self):
            events.append("store.close")

    monkeypatch.setattr(live, "load_config", lambda path: SimpleNamespace(
        accounts=None, pool=None, upstream=None))
    monkeypatch.setattr(live, "load_accounts", lambda cfg: [FakeAccount()])
    monkeypatch.setattr(live, "PoolManager", FakePool)
    monkeypatch.setattr(live, "TokenManager", FakeTokens)
    monkeypatch.setattr(live, "UpstreamClient", FakeClient)
    monkeypatch.setattr(live, "ChatService", FakeService)
    monkeypatch.setattr(live, "Store", FakeStore)
    monkeypatch.setattr(live, "JsonlCapture", lambda *a: None)
    monkeypatch.setattr(live, "Registry", lambda: None)

    status = live.asyncio.run(live.run(SimpleNamespace(config="config.yaml", infer=True)))

    assert status == 0
    assert events == ["service.aclose", "store.close"]
