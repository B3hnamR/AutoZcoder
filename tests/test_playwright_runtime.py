"""Runtime browser preflight regressions; subprocess boundaries are mocked."""
from types import SimpleNamespace

import pytest

from zcode_gateway import generator as gen


@pytest.fixture(autouse=True)
def block_processes(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected real subprocess call")

    monkeypatch.setattr(gen.subprocess, "run", unexpected)
    monkeypatch.setattr(gen.subprocess, "Popen", unexpected)


def probe_result(state="ready", *, returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=state + "\n", stderr=stderr)


def test_ready_root_skips_all_installers_and_inherits_browser_path(monkeypatch):
    monkeypatch.setattr(gen.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/app/browsers")
    calls = []

    def probe(argv, **kwargs):
        calls.append((argv, kwargs))
        assert gen.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/app/browsers"
        return probe_result()

    monkeypatch.setattr(gen.subprocess, "run", probe)
    setup = gen.PlaywrightSetup()
    assert setup.ensure("/opt/app/genenv/bin/python", lambda _: None)
    assert setup.ensure("/opt/app/genenv/bin/python", lambda _: None)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["/opt/app/genenv/bin/python", "-B", "-c"]
    assert "p.chromium.executable_path" in argv[3]
    assert "os.path.isfile(exe)" in argv[3]
    assert "install" not in argv[3]
    assert "env" not in kwargs
    assert kwargs["capture_output"] and kwargs["timeout"] == 60
    assert setup.status() == {"installing": False, "ready": True}


def test_missing_browser_runtime_never_installs_system_dependencies(monkeypatch):
    monkeypatch.setattr(gen, "os", SimpleNamespace(
        name="posix", geteuid=lambda: 0, environ=gen.os.environ))
    states = iter(["chromium-missing", "ready"])
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(next(states)))
    calls = []
    setup = gen.PlaywrightSetup()

    def install(argv, emit):
        calls.append(argv)
        return True

    monkeypatch.setattr(setup, "_run", install)
    assert setup.ensure("chosen-python", lambda _: None)
    assert calls == [["chosen-python", "-m", "playwright", "install", "chromium"]]
    assert gen._chromium_install_argv("chosen-python") == [
        "chosen-python", "-m", "playwright", "install", "--with-deps", "chromium"]


def test_package_install_reprobes_before_deciding_browser_missing(monkeypatch):
    states = iter(["package-missing", "ready"])
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(next(states)))
    setup = gen.PlaywrightSetup()
    calls = []
    monkeypatch.setattr(setup, "_run", lambda argv, emit: calls.append(argv) or True)
    assert setup.ensure("chosen-python", lambda _: None)
    assert calls == [["chosen-python", "-m", "pip", "install", "--upgrade", "playwright"]]


def test_readiness_cache_is_per_python_and_browser_path(monkeypatch):
    calls = []
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    def probe(argv, **kwargs):
        calls.append((argv[0], gen.os.environ.get("PLAYWRIGHT_BROWSERS_PATH")))
        return probe_result()

    monkeypatch.setattr(gen.subprocess, "run", probe)
    setup = gen.PlaywrightSetup()
    for py in ("python-a", "python-b", "python-a"):
        assert setup.ensure(py, lambda _: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/different/browsers")
    assert setup.ensure("python-a", lambda _: None)
    assert calls == [("python-a", None), ("python-b", None), ("python-a", "/different/browsers")]


def test_concurrent_missing_browser_installs_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    entered, release = threading.Event(), threading.Event()
    states = iter(["chromium-missing", "ready"])
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(next(states)))
    setup = gen.PlaywrightSetup()
    calls = []

    def install(argv, emit):
        calls.append(argv)
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(setup, "_run", install)
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(setup.ensure, "chosen-python", lambda _: None)
        try:
            assert entered.wait(3)
            second = threads.submit(setup.ensure, "chosen-python", lambda _: None)
        finally:
            release.set()
        assert first.result(3) and second.result(3)
    assert len(calls) == 1


def test_callback_errors_do_not_abort_probe_or_install(monkeypatch):
    states = iter(["chromium-missing", "ready"])
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(next(states)))
    setup = gen.PlaywrightSetup()

    def broken_callback(line):
        raise ValueError("dashboard disconnected")

    def install(argv, emit):
        emit("installer progress")
        return True

    monkeypatch.setattr(setup, "_run", install)
    assert setup.ensure("chosen-python", broken_callback)
    assert setup.status() == {"installing": False, "ready": True}


@pytest.mark.parametrize("error", [FileNotFoundError("not found"), PermissionError("not executable")])
def test_invalid_interpreter_reports_failure_without_installing(monkeypatch, error):
    def probe(*args, **kwargs):
        raise error

    monkeypatch.setattr(gen.subprocess, "run", probe)
    setup = gen.PlaywrightSetup()
    messages = []
    assert not setup.ensure("invalid-python", messages.append)
    assert "invalid-python" in " ".join(messages)
    assert str(error) in " ".join(messages)
    assert setup.status() == {"installing": False, "ready": False}


@pytest.mark.parametrize("py", ["", "bad\0python", None])
def test_invalid_interpreter_argument_never_spawns(py):
    messages = []
    assert not gen.PlaywrightSetup().ensure(py, messages.append)
    assert "valid Python interpreter" in messages[0]


def test_probe_error_is_not_treated_as_missing_package(monkeypatch):
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(
        "", returncode=1, stderr="error while loading shared libraries: libnss3.so"))
    messages = []
    setup = gen.PlaywrightSetup()
    assert not setup.ensure("chosen-python", messages.append)
    message = " ".join(messages)
    assert "libnss3.so" in message
    assert "outside the hardened service" in message
    assert "install-deps chromium" in message
    assert "Keep service filesystem protections and browser sandboxing enabled" in message
    assert not setup.status()["ready"]


def test_unexpected_programming_error_is_not_suppressed(monkeypatch):
    def probe(*args, **kwargs):
        raise TypeError("programming error")

    monkeypatch.setattr(gen.subprocess, "run", probe)
    setup = gen.PlaywrightSetup()
    with pytest.raises(TypeError, match="programming error"):
        setup.ensure("chosen-python", lambda _: None)
    assert not setup.status()["installing"]


@pytest.mark.parametrize("install_ok,final_state", [(False, "ready"), (True, "chromium-missing")])
def test_failed_install_is_not_cached_and_reports_external_setup(monkeypatch, install_ok, final_state):
    states = iter(["chromium-missing", final_state, "ready"] if install_ok else ["chromium-missing", "ready"])
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **kw: probe_result(next(states)))
    setup = gen.PlaywrightSetup()
    monkeypatch.setattr(setup, "_run", lambda argv, emit: install_ok)
    messages = []
    assert not setup.ensure("chosen-python", messages.append)
    assert not setup.status()["ready"]
    assert "external installer" in " ".join(messages)
    assert setup.ensure("chosen-python", messages.append)


def test_installer_wrapper_keeps_process_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/service/browsers")
    monkeypatch.setattr(gen.os.path, "isfile", lambda _: False)

    def installer(argv, emit, timeout, **kwargs):
        calls.append(kwargs)
        assert gen.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/service/browsers"
        return True

    monkeypatch.setattr(gen, "_run_installer", installer)
    assert gen.PlaywrightSetup()._run(["python", "-m", "playwright", "install", "chromium"], lambda _: None)
    assert calls == [{"cwd": None}]
