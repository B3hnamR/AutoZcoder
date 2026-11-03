"""Offline regressions for worker finalization and durable auto-run receipts."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from zcode_gateway import auto_generate as auto
from zcode_gateway import generator as gen


def wait_for(predicate):
    deadline = time.perf_counter() + 4
    while not predicate():
        assert time.perf_counter() < deadline, "background operation did not settle"
        threading.Event().wait(0.01)


class Handle:
    def __init__(self):
        self.done = threading.Event()
        self.result = {}
        self.stops = 0

    def stop(self):
        self.stops += 1


def saved_run(tmp_path, *, active=1, qualified=0, attempts=1, cooldown=60):
    controller = auto.AutoGenerateController(tmp_path, lambda *_: None)
    controller._run_config = auto.validate_config({"mode": "attempts", "attempt_limit": 10000,
                                                  "cooldown_seconds": cooldown})
    controller._run_id = "a" * 32
    controller._status = "paused"
    controller._counts.update(attempts=attempts, qualified=qualified,
                              completed_attempts=attempts - active)
    controller._account_ids = {f"account-{i}" for i in range(qualified)}
    for index in range(active):
        controller._active[f"{controller._run_id}-{attempts - index}"] = {
            "index": index, "handle": None, "cancelled": False, "stop_sent": False}
    controller._persist_locked()
    return controller


def worker(tmp_path, callback=None):
    return gen.GenWorker("offline", ["unused"], {"ZAI_OUTPUT_DIR": str(tmp_path)},
                         tmp_path, callback)


def claim(tmp_path):
    return gen.ClaimJob("offline-claim", "test@example.invalid", "direct", "", tmp_path,
                        "unused", "", tmp_path)


@pytest.mark.parametrize("kind", ["worker", "claim"])
def test_stopped_or_finalized_worker_cannot_spawn(tmp_path, monkeypatch, kind):
    job = worker(tmp_path) if kind == "worker" else claim(tmp_path)
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_k: pytest.fail("late spawn"))
    job.stop()
    assert job.done.is_set()
    assert job._thread is None
    job.start()
    job.start()
    assert job.proc is None


def test_failed_queued_claim_cannot_spawn(tmp_path, monkeypatch):
    job = claim(tmp_path)
    job.fail("offline route failure")
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_k: pytest.fail("failed job spawned"))
    job.start()
    assert job.done.is_set() and job.proc is None


@pytest.mark.parametrize("kind", ["worker", "claim"])
def test_stop_serializes_with_inflight_popen(tmp_path, monkeypatch, kind):
    job = worker(tmp_path) if kind == "worker" else claim(tmp_path)
    entered, release = threading.Event(), threading.Event()
    proc = SimpleNamespace(stdout=None, poll=lambda: None, wait=lambda: 0)

    def spawn(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return proc

    monkeypatch.setattr(gen.subprocess, "Popen", spawn)
    monkeypatch.setattr(job, "_pump", lambda: None)
    killed = []
    monkeypatch.setattr(gen, "_kill_tree", killed.append)
    starter = threading.Thread(target=job.start)
    stopper = threading.Thread(target=job.stop)
    starter.start()
    assert entered.wait(3)
    stopper.start()
    assert not job.stopped
    release.set()
    starter.join(3)
    stopper.join(3)
    assert not starter.is_alive() and not stopper.is_alive()
    assert job.stopped and killed == [proc]


@pytest.mark.parametrize("kind", ["worker", "claim"])
def test_pump_thread_start_failure_kills_reaps_and_finalizes(tmp_path, monkeypatch, kind):
    calls = []
    job = worker(tmp_path, lambda _j: calls.append("callback")) if kind == "worker" else claim(tmp_path)
    job.on_exit = lambda _j: calls.append("callback")
    proc = SimpleNamespace(stdout=SimpleNamespace(close=lambda: calls.append("close")),
                           poll=lambda: None, wait=lambda: calls.append("wait"))
    monkeypatch.setattr(gen.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(gen, "_kill_tree", lambda _p: calls.append("kill"))
    monkeypatch.setattr(threading.Thread, "start", lambda _s: (_ for _ in ()).throw(RuntimeError("offline")))
    job.start()
    assert job.done.is_set()
    assert calls == ["kill", "wait", "close", "callback"]
    assert job.state == "failed"
    job.start()
    assert calls.count("callback") == 1


@pytest.mark.parametrize("kind", ["worker", "claim"])
def test_done_waits_for_callback_even_after_stop(tmp_path, monkeypatch, kind):
    job = worker(tmp_path) if kind == "worker" else claim(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def callback(_job):
        entered.set()
        assert release.wait(3)

    job.on_exit = callback
    job._started = True
    monkeypatch.setattr(gen, "_kill_tree", lambda _p: None)
    thread = threading.Thread(target=job._finish)
    thread.start()
    assert entered.wait(3)
    job.stop()
    assert not job.done.is_set()
    release.set()
    thread.join(3)
    assert job.done.is_set()


def test_failed_resume_preflight_keeps_active_attempt_and_resumable_state(tmp_path):
    handle = Handle()
    controller = auto.AutoGenerateController(tmp_path, lambda *_: handle)
    try:
        controller.start({"target": 1})
        wait_for(lambda: any(slot["handle"] for slot in controller._active.values()))
        controller.pause()
        before = controller.status()
        controller._preflight = lambda _: (_ for _ in ()).throw(ValueError("offline prerequisite"))
        with pytest.raises(ValueError):
            controller.resume()
        after = controller.status()
        assert after["status"] == "paused"
        assert after["reason"] == before["reason"]
        assert after["attempts"] == before["attempts"] == after["active"] == 1
        assert handle.stops == 0
        controller._preflight = None
        controller.resume()
        assert controller.status()["status"] == "running"
    finally:
        handle.done.set()
        controller.shutdown()


def test_failed_interrupted_resume_can_retry(tmp_path):
    saved_run(tmp_path)
    controller = auto.AutoGenerateController(tmp_path, lambda *_: Handle(),
        lambda _: (_ for _ in ()).throw(ValueError("offline")))
    before = controller.status()
    with pytest.raises(ValueError):
        controller.resume()
    assert controller.status()["status"] == "interrupted"
    assert controller.status()["interrupted_attempts"] == before["interrupted_attempts"] == 1


def test_explicit_duplicate_category_increments_duplicate_counter(tmp_path):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    controller._finish_locked(attempt_id, {"qualified": False, "category": "duplicate"})
    assert controller.status()["duplicates"] == 1
    assert controller.status()["categories"] == {"duplicate": 1}
    assert controller.status()["failed"] == 1


def test_attempt_mode_restores_ten_thousand_unique_account_ids(tmp_path):
    saved_run(tmp_path, active=0, qualified=10000, attempts=10000)
    controller = auto.AutoGenerateController(tmp_path, lambda *_: pytest.fail("unexpected launch"))
    assert controller.status()["status"] == "interrupted"
    assert controller.status()["qualified"] == 10000
    assert len(controller._account_ids) == 10000


def test_cooldown_restores_remaining_wallclock_time_on_new_monotonic_epoch(tmp_path, monkeypatch):
    wall, monotonic = [1000.0], [100.0]
    monkeypatch.setattr(auto.time, "time", lambda: wall[0])
    monkeypatch.setattr(auto.time, "monotonic", lambda: monotonic[0])
    controller = saved_run(tmp_path, active=0)
    controller._ready_at = [130.0, 0.0, 0.0]
    controller._persist_locked()
    wall[0], monotonic[0] = 1012.0, 5000.0
    restored = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert restored.status()["cooldowns"] == [18]
    assert restored._ready_at[0] == 5018
    wall[0] = 1031
    expired = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert expired.status()["cooldowns"] == [0]


def test_durable_ack_reconciles_before_interruption_and_only_once(tmp_path):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    controller.record_registration(attempt_id, "registered-account")
    restored = auto.AutoGenerateController(tmp_path, lambda *_: pytest.fail("unexpected launch"))
    assert restored.status()["qualified"] == restored.status()["completed_attempts"] == 1
    assert restored.status()["interrupted_attempts"] == 0
    assert restored.status()["id"] == controller.status()["id"]
    restored.reconcile_registrations({attempt_id: "registered-account"})
    again = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert again.status()["qualified"] == again.status()["completed_attempts"] == 1
    journal = json.loads(controller._journal_path.read_text())
    receipt = journal["registrations"][attempt_id]
    assert set(receipt) == {"account_id", "registered_at"}
    assert not list(tmp_path.glob("*.tmp"))


def test_snapshot_receipt_repairs_snapshot_to_journal_crash_window(tmp_path):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    restored = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert restored.status()["interrupted_attempts"] == 1
    restored.reconcile_registrations({attempt_id: "snapshot-account"})
    state = restored.status()
    assert state["completed_attempts"] == state["qualified"] == 1
    assert state["interrupted_attempts"] == 0
    assert state["categories"] == {"qualified": 1}
    restored.reconcile_registrations({attempt_id: "snapshot-account"})
    assert restored.status()["qualified"] == 1


def test_acknowledged_registration_survives_progress_replace_failure(tmp_path, monkeypatch):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    controller.record_registration(attempt_id, "durable-account")
    controller._finish_locked(attempt_id, {"qualified": True, "account_id": "durable-account"})
    with monkeypatch.context() as patch:
        patch.setattr(auto.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk failure")))
        assert controller._persist_or_fail_locked() is False
    restored = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert restored.status()["qualified"] == restored.status()["completed_attempts"] == 1
    assert restored.status()["interrupted_attempts"] == 0


def test_journal_replace_failure_does_not_publish_memory_ack(tmp_path, monkeypatch):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    monkeypatch.setattr(auto.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("offline disk")))
    with pytest.raises(OSError):
        controller.record_registration(attempt_id, "not-acknowledged")
    assert controller._registrations == {}
    assert not controller._journal_path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_receipts_are_scoped_by_run_attempt_and_account_identity(tmp_path):
    controller = saved_run(tmp_path, active=2, attempts=2)
    attempts = sorted(controller._active)
    controller.record_registration(attempts[0], "same-account")
    controller.record_registration(attempts[1], "same-account")
    with pytest.raises(ValueError):
        controller.record_registration(attempts[0], "different-account")
    with pytest.raises(ValueError):
        controller.record_registration("foreign-1", "same-account")
    with pytest.raises(ValueError):
        controller.record_registration(controller._run_id + "-3", "same-account")
    restored = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert restored.status()["qualified"] == restored.status()["duplicates"] == 1
    assert restored.status()["completed_attempts"] == 2
    assert restored.status()["interrupted_attempts"] == 0


def test_committed_registration_ack_is_not_erased_by_late_stop(tmp_path):
    controller = saved_run(tmp_path)
    attempt_id = next(iter(controller._active))
    controller.record_registration(attempt_id, "committed-account")
    controller._active[attempt_id]["cancelled"] = True
    controller._finish_locked(attempt_id, {"category": "stopped"})
    assert controller.status()["qualified"] == 1


def test_auto_launch_env_and_registration_callback_carry_actual_attempt_id(tmp_path, monkeypatch):
    manager = object.__new__(gen.GeneratorManager)
    manager._closed = threading.Event()
    manager._lock = threading.Lock()
    manager.data_dir, manager.gen_dir, manager.python_exe = tmp_path, tmp_path, "unused"
    manager.workers = {}
    manager._auto_runtime_snapshot = SimpleNamespace(_worker_env=lambda _: {"ZAI_OUTPUT_DIR": str(tmp_path)})
    monkeypatch.setattr(gen.GenWorker, "start", lambda _: None)
    attempt_id = "a" * 32 + "-7"
    job = manager._auto_launch({"ip_source": "direct"}, attempt_id)
    assert job.env["ZAI_AUTO_ATTEMPT_ID"] == attempt_id
    assert job.env["ZAI_AUTO_RUN_ID"] == "a" * 32
    job.script_result = {"category": "generated"}
    job.check_new_account = lambda: {"email": "test@example.invalid", "password": "private"}
    route = tmp_path / "auto-private" / (job.name + ".json")
    route.write_text('{"proxy":""}', encoding="utf-8")
    seen = []
    manager.on_account = lambda info: seen.append(info) or {"category": "duplicate"}
    manager._registration_lock = threading.Lock()
    manager._worker_done(job)
    assert seen[0]["auto_run_id"] == "a" * 32
    assert seen[0]["auto_attempt_id"] == attempt_id


def test_status_tolerates_background_dictionary_updates(tmp_path):
    manager = object.__new__(gen.GeneratorManager)
    manager._lock = threading.Lock()
    manager.workers, manager.claims = {}, {}
    manager.options = lambda: {}
    manager.auto_generate = SimpleNamespace(status=lambda: {})
    job = SimpleNamespace(to_public=lambda: {}, alive=lambda: False)
    stop = threading.Event()

    def mutate():
        while not stop.is_set():
            with manager._lock:
                manager.workers["offline"] = job
                manager.claims["offline"] = job
                manager.workers.clear()
                manager.claims.clear()

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _ in range(1000):
            assert isinstance(manager.status()["workers"], list)
    finally:
        stop.set()
        thread.join(3)


def test_launch_failed_result_not_overwritten_by_registration_path(tmp_path):
    job = worker(tmp_path)
    job.result = {"qualified": False, "category": "launch_failed"}
    manager = object.__new__(gen.GeneratorManager)
    manager._closed = threading.Event()
    manager._worker_done(job)
    assert job.result["category"] == "launch_failed"


def test_clear_finished_claims_preserves_callback_inflight():
    manager = object.__new__(gen.GeneratorManager)
    manager._lock = threading.Lock()
    job = SimpleNamespace(done=threading.Event(), alive=lambda: False)
    manager.claims = {"inflight": job}
    assert manager.clear_finished_claims() == 0
    job.done.set()
    assert manager.clear_finished_claims() == 1


def test_recovered_receipt_cooldown_uses_ack_time_not_restart_time(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auto.time, "time", lambda: clock[0])
    controller = saved_run(tmp_path, cooldown=60)
    controller.record_registration(next(iter(controller._active)), "registered-account")
    clock[0] = 1070
    restored = auto.AutoGenerateController(tmp_path, lambda *_: None)
    assert restored.status()["cooldowns"] == [0]
