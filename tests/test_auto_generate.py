import json
import os
import stat
import threading
import time

import pytest

from zcode_gateway import auto_generate as module
from zcode_gateway.auto_generate import AutoGenerateController, DEFAULT_CONFIG, validate_config


class Handle:
    def __init__(self):
        self.done = threading.Event()
        self.result = {}
        self.stops = 0

    def finish(self, qualified=False, account_id="", category="failed"):
        self.result = {"qualified": qualified, "account_id": account_id, "category": category}
        self.done.set()

    def stop(self):
        self.stops += 1
        self.done.set()


def wait_for(predicate):
    deadline = time.perf_counter() + 4
    while not predicate():
        if time.perf_counter() > deadline:
            pytest.fail("scheduler did not reach expected state")
        threading.Event().wait(0.01)


@pytest.fixture
def factory(tmp_path):
    controllers = []

    def create(launch=None, preflight=None):
        handles = []

        def default_launch(config, attempt_id):
            handle = Handle()
            handles.append(handle)
            return handle

        controller = AutoGenerateController(tmp_path, launch or default_launch, preflight)
        controllers.append(controller)
        return controller, handles

    yield create
    for controller in controllers:
        controller.shutdown()


def settings(**kwargs):
    return {**DEFAULT_CONFIG, "cooldown_seconds": 0, "target": 3, **kwargs}


def test_live_session_log_tracks_launch_failure_completion_and_has_no_worker_secrets(factory):
    def launch(config, attempt_id):
        handle = Handle()
        handle.name = 'auto-fixture'
        handle.result = {'qualified': False, 'category': 'no_plan',
                         'error': 'Bearer sensitive-jwt-do-not-expose'}
        handle.done.set()
        return handle
    controller, _ = factory(launch)
    controller.start(settings(mode='attempts', attempt_limit=1))
    wait_for(lambda: controller.status()['status']=='completed')
    status = controller.status()
    messages = '\n'.join(event['message'] for event in status['events'])
    assert 'Preflight passed' in messages and 'launching a worker' in messages
    assert 'no plan' in messages and 'attempt budget exhausted' in messages
    assert any(event['worker']=='auto-fixture' for event in status['events'])
    assert 'sensitive-jwt' not in json.dumps(status)
    assert 'events' not in json.loads(controller._progress_path.read_text())
    with controller._lock:
        for i in range(250):
            controller._event('bounded event %d' % i)
    assert len(controller.status()['events'])==200


def test_launch_exception_has_an_explicit_session_log(factory):
    def fail(*_):
        raise RuntimeError('private credential value')
    controller, _ = factory(fail)
    controller.start(settings(mode='attempts', attempt_limit=1))
    wait_for(lambda:controller.status()['status']=='completed')
    status=controller.status()
    assert status['categories']=={'launch_failed':1}
    assert any('launch failed' in event['message'] for event in status['events'])
    assert 'private credential' not in json.dumps(status)


def test_finish_signup_failure_pauses_instead_of_creating_replacements(factory):
    controller, handles=factory()
    controller.start(settings(target=5,concurrency=1))
    wait_for(lambda:len(handles)==1)
    handles[0].finish(category='finish_signup_failed')
    wait_for(lambda:controller.status()['status']=='paused')
    assert len(handles)==1
    assert controller.status()['reason']=='finish_signup_failed'
    assert any('provider rejected final signup' in event['message'] for event in controller.status()['events'])


@pytest.mark.parametrize("field", list(module._BOUNDS))
@pytest.mark.parametrize("value", [True, False, "1", 1.0, None, -1, 10001])
def test_strict_integer_validation(field, value):
    with pytest.raises(ValueError):
        validate_config({field: value})


@pytest.mark.parametrize("config", [
    {"concurrency": 4}, {"target": 1001}, {"attempt_limit": 10001},
    {"cooldown_seconds": 3601}, {"timeout_seconds": 59}, {"timeout_seconds": 3601},
    {"failure_limit": 101}, {"mode": "wrong"}, {"ip_source": "wrong"},
    {"ip_source": "pin"}, {"ip_source": "pool"}, {"captcha": "manual"},
    {"captcha": "wrong"}, {"secret": "never persist"}, {"target": 10, "attempt_limit": 9},
])
def test_invalid_configs(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_defaults_and_attempt_mode_budget_validation():
    assert validate_config({}) == DEFAULT_CONFIG
    assert validate_config({"mode": "attempts", "target": 100, "attempt_limit": 1})["target"] == 100
    assert validate_config({"ip_source": "proxy"})["ip_source"] == "proxy"


def test_saved_preferences_and_running_snapshot_are_separate(factory, tmp_path):
    controller, handles = factory()
    saved = controller.save_config(settings(target=2))
    saved["target"] = 99
    controller.start(settings(target=1))
    wait_for(lambda: len(handles) == 1)
    controller.save_config(settings(target=5))
    assert controller.status()["config"]["target"] == 1
    assert controller.config()["target"] == 5
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["status"] == "completed")
    assert json.loads((tmp_path / "auto_generate_config.json").read_text())["target"] == 5
    status = controller.status()
    status["config"]["target"] = 40
    assert controller.status()["config"]["target"] == 1
    if os.name != "nt":
        for path in tmp_path.glob("*.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_goal_reservations_wait_for_registration_and_exact_goal(factory):
    controller, handles = factory()
    controller.start(settings(target=2, concurrency=3))
    wait_for(lambda: len(handles) == 2)
    threading.Event().wait(0.25)
    assert len(handles) == controller.status()["active"] == 2
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["qualified"] == 1)
    assert len(handles) == 2
    handles[1].finish(True, "b")
    wait_for(lambda: controller.status()["status"] == "completed")
    assert controller.status()["qualified"] == 2
    assert controller.status()["attempts"] == 2


def test_duplicate_identity_and_no_plan_do_not_satisfy_goal(factory):
    controller, handles = factory()
    controller.start(settings(target=2, attempt_limit=5))
    for index, result in enumerate([(True, "a", "qualified"), (True, "a", "qualified"),
                                    (False, "b", "no_plan"), (True, "b", "qualified")]):
        wait_for(lambda: len(handles) > index)
        handles[index].finish(*result)
    wait_for(lambda: controller.status()["status"] == "completed")
    state = controller.status()
    assert (state["qualified"], state["duplicates"], state["no_plan"], state["attempts"]) == (2, 1, 1, 4)
    assert state["failed"] == 2


def test_fixed_attempt_mode_uses_attempt_limit_not_target(factory):
    controller, handles = factory()
    controller.start(settings(mode="attempts", target=1, attempt_limit=4, concurrency=3))
    for index in range(4):
        wait_for(lambda: len(handles) > index)
        handles[index].finish()
    wait_for(lambda: controller.status()["status"] == "completed")
    assert len(handles) == controller.status()["attempts"] == 4
    assert controller.status()["reason"] == "attempts_exhausted"


def test_fixed_attempt_mode_allows_budget_below_hidden_goal_target(factory):
    controller, handles = factory()
    controller.start(settings(mode="attempts", target=20, attempt_limit=2, concurrency=3))
    wait_for(lambda: len(handles) == 2)
    handles[0].finish(True, "a")
    handles[1].finish(True, "b")
    wait_for(lambda: controller.status()["status"] == "completed")
    state = controller.status()
    assert len(handles) == state["attempts"] == state["completed_attempts"] == 2
    assert state["qualified"] == 2
    assert state["active"] == 0
    assert state["reason"] == "attempts_exhausted"


def test_goal_attempt_limit_and_failure_limit(factory):
    controller, handles = factory()
    controller.start(settings(target=2, attempt_limit=2))
    for index in range(2):
        wait_for(lambda: len(handles) > index)
        handles[index].finish()
    wait_for(lambda: controller.status()["status"] == "completed")
    assert controller.status()["qualified"] == 0
    controller.start(settings(failure_limit=1))
    wait_for(lambda: len(handles) == 3)
    handles[-1].finish()
    wait_for(lambda: controller.status()["status"] == "failed")
    assert controller.status()["reason"] == "failure_limit"


def test_pause_finishes_active_without_replacement_then_resume(factory):
    preflights = []
    controller, handles = factory(preflight=lambda config: preflights.append(config))
    controller.start(settings())
    wait_for(lambda: len(handles) == 1)
    controller.pause()
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["active"] == 0)
    assert controller.status()["status"] == "paused"
    assert len(handles) == 1
    controller.resume()
    wait_for(lambda: len(handles) == 2)
    assert controller.status()["attempts"] == 2
    assert len(preflights) == 2


def test_cooldown_is_per_slot_and_starts_at_completion(factory, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    controller, handles = factory()
    controller.start(settings(mode="attempts", attempt_limit=4, concurrency=2, cooldown_seconds=10))
    wait_for(lambda: len(handles) == 2)
    handles[0].finish()
    wait_for(lambda: controller.status()["completed_attempts"] == 1)
    clock[0] = 109
    controller._wake.set()
    threading.Event().wait(0.25)
    assert len(handles) == 2
    clock[0] = 110
    controller._wake.set()
    wait_for(lambda: len(handles) == 3)
    assert not handles[1].done.is_set()
    controller.stop()
    clock[0] = 999
    controller._wake.set()
    wait_for(lambda: controller.status()["status"] == "stopped")
    assert len(handles) == 3
    assert controller.status()["cooldowns"] == [0, 0]


def test_stop_during_pending_launch_retains_reservation_and_stops_late_handle(factory):
    entered, release = threading.Event(), threading.Event()
    handle = Handle()

    def launch(config, attempt_id):
        entered.set()
        assert release.wait(3)
        return handle

    controller, _ = factory(launch)
    controller.start(settings(concurrency=3))
    assert entered.wait(3)
    assert controller.status()["active"] == 1
    assert controller.stop()["status"] == "stopping"
    release.set()
    wait_for(lambda: controller.status()["status"] == "stopped")
    assert handle.stops == 1
    assert controller.status()["attempts"] == 1
    assert controller.status()["qualified"] == 0


def test_stop_callbacks_outside_lock_and_no_late_success(factory):
    controller, handles = factory()
    controller.start(settings(target=1))
    wait_for(lambda: len(handles) == 1)
    observed = []

    def stop():
        thread = threading.Thread(target=lambda: observed.append(controller.status()["status"]))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        handles[0].finish(True, "late")

    handles[0].stop = stop
    controller.stop()
    wait_for(lambda: controller.status()["status"] == "stopped")
    assert observed == ["stopping"]
    assert controller.status()["qualified"] == 0


def test_persisted_interruption_requires_explicit_resume_preserves_budget_and_ids(factory, tmp_path):
    controller, handles = factory()
    controller.start(settings(target=2, concurrency=2, attempt_limit=3))
    wait_for(lambda: len(handles) == 2)
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["qualified"] == 1)
    before = (tmp_path / "auto_generate_progress.json").read_text()
    restored, new_handles = factory()
    assert restored.status()["status"] == "interrupted"
    assert restored.status()["attempts"] == 2
    assert restored.status()["interrupted_attempts"] == 1
    assert new_handles == []
    assert json.loads(before)["active"] == 1
    controller.stop()
    wait_for(lambda: controller.status()["status"] == "stopped")
    restored.resume()
    wait_for(lambda: len(new_handles) == 1)
    new_handles[0].finish(True, "a")
    wait_for(lambda: restored.status()["status"] == "completed")
    assert restored.status()["qualified"] == 1
    assert restored.status()["duplicates"] == 1
    assert restored.status()["attempts"] == 3


def test_preflight_failure_no_launch_or_secret_leak(factory, tmp_path):
    def preflight(config):
        raise RuntimeError("secret-token-value")

    controller, handles = factory(preflight=preflight)
    with pytest.raises(RuntimeError):
        controller.start(settings())
    assert controller.status()["status"] == "failed"
    assert handles == []
    assert "secret-token-value" not in (tmp_path / "auto_generate_progress.json").read_text()


def test_thread_start_failure_clears_running_state(factory, monkeypatch):
    controller, handles = factory()
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no thread")))
        with pytest.raises(RuntimeError):
            controller.start(settings())
    assert controller.status()["status"] == "failed"
    assert controller.status()["active"] == 0
    assert handles == []
    controller.start(settings(target=1))
    wait_for(lambda: len(handles) == 1)


def test_write_failures_leave_saved_config_unchanged_and_prevent_launch(factory, monkeypatch):
    controller, handles = factory()
    original = controller.config()

    def fail(*args):
        raise OSError("disk full secret")

    monkeypatch.setattr(module, "_atomic_json", fail)
    with pytest.raises(OSError):
        controller.save_config(settings())
    assert controller.config() == original
    with pytest.raises(OSError):
        controller.start(settings())
    assert controller.status()["status"] == "failed"
    assert handles == []


def test_atomic_replace_failure_preserves_old_file_and_cleans_temp(factory, tmp_path, monkeypatch):
    controller, _ = factory()
    controller.save_config(settings(target=1))
    old = (tmp_path / "auto_generate_config.json").read_bytes()
    monkeypatch.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        controller.save_config(settings(target=2))
    assert (tmp_path / "auto_generate_config.json").read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_launch_errors_count_budget_and_raw_results_not_persisted(factory, tmp_path):
    calls = []

    def launch(config, attempt_id):
        calls.append(attempt_id)
        raise RuntimeError("sensitive detail")

    controller, _ = factory(launch)
    controller.start(settings(mode="attempts", attempt_limit=2))
    wait_for(lambda: controller.status()["status"] == "completed")
    assert len(set(calls)) == 2
    assert controller.status()["categories"] == {"launch_failed": 2}
    assert "sensitive detail" not in (tmp_path / "auto_generate_progress.json").read_text()


def test_timeout_stops_owned_handle_once_without_success(factory, monkeypatch):
    real_clock = time.monotonic
    clock = [real_clock()]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    controller, handles = factory()
    controller.start(settings(mode="attempts", attempt_limit=1, timeout_seconds=60))
    wait_for(lambda: len(handles) == 1)
    clock[0] += 61
    controller._wake.set()
    wait_for(lambda: controller.status()["status"] == "completed")
    assert handles[0].stops == 1
    assert controller.status()["categories"] == {"timeout": 1}


def test_timed_out_attempt_still_observes_replacement_cooldown(factory, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    controller, handles = factory()
    controller.start(settings(mode="attempts", attempt_limit=2, timeout_seconds=60, cooldown_seconds=10))
    wait_for(lambda: len(handles) == 1)
    clock[0] = 161
    controller._wake.set()
    wait_for(lambda: controller.status()["completed_attempts"] == 1)
    assert controller.status()["cooldowns"] == [10]
    assert len(handles) == 1
    clock[0] = 171
    controller._wake.set()
    wait_for(lambda: len(handles) == 2)


def test_stop_cancels_cooldown_without_starting_replacement(factory, monkeypatch):
    controller, handles = factory()
    controller.start(settings(cooldown_seconds=60))
    wait_for(lambda: len(handles) == 1)
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["active"] == 0)
    assert controller.status()["cooldowns"][0] > 0
    controller.stop()
    assert controller.status()["status"] == "stopped"
    assert controller.status()["cooldowns"] == [0]
    threading.Event().wait(0.25)
    assert len(handles) == 1


def test_preflight_cannot_mutate_snapshot_or_restart_after_stop(factory):
    entered, release = threading.Event(), threading.Event()

    def preflight(config):
        config["target"] = 100
        entered.set()
        assert release.wait(3)

    controller, handles = factory(preflight=preflight)
    starter = threading.Thread(target=lambda: controller.start(settings(target=1)))
    starter.start()
    assert entered.wait(3)
    with pytest.raises(RuntimeError):
        controller.start(settings())
    controller.stop()
    release.set()
    starter.join(3)
    assert not starter.is_alive()
    assert controller.status()["status"] == "stopped"
    assert controller.status()["config"]["target"] == 1
    assert handles == []


def test_runtime_persistence_failure_terminates_existing_handle(factory, monkeypatch):
    controller, handles = factory()
    controller.start(settings(concurrency=2))
    wait_for(lambda: len(handles) == 2)
    monkeypatch.setattr(module, "_atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    handles[0].finish(True, "a")
    controller._wake.set()
    wait_for(lambda: controller.status()["status"] == "failed" and handles[1].stops == 1)
    wait_for(lambda: controller.status()["active"] == 0)
    assert len(handles) == 2
    assert controller.status()["reason"] == "persistence_error"


def test_untrusted_result_category_cannot_claim_success_or_persist_error(factory, tmp_path):
    controller, handles = factory()
    controller.start(settings(mode="attempts", attempt_limit=2))
    wait_for(lambda: len(handles) == 1)
    handles[0].result = {"qualified": False, "category": "qualified", "error": "secret-one"}
    handles[0].done.set()
    wait_for(lambda: len(handles) == 2)
    handles[1].result = {"qualified": True, "account_id": "", "category": "secret-two"}
    handles[1].done.set()
    wait_for(lambda: controller.status()["status"] == "completed")
    state = controller.status()
    assert state["qualified"] == 0
    assert state["failed"] == 2
    assert "secret" not in (tmp_path / "auto_generate_progress.json").read_text()


def test_load_completed_does_not_start_and_preserves_reason(factory):
    controller, handles = factory()
    controller.start(settings(target=1))
    wait_for(lambda: len(handles) == 1)
    handles[0].finish(True, "a")
    wait_for(lambda: controller.status()["status"] == "completed")
    restored, new_handles = factory()
    assert restored.status()["status"] == "completed"
    assert restored.status()["reason"] == "goal_reached"
    assert restored.status()["qualified"] == 1
    assert new_handles == []


@pytest.mark.parametrize("filename", ["auto_generate_config.json", "auto_generate_progress.json"])
def test_corrupt_persistence_never_autostarts(factory, tmp_path, filename):
    module._atomic_json(tmp_path / filename, {"bad": "value"})
    controller, handles = factory()
    assert controller.status()["status"] == "failed"
    assert handles == []


def test_paused_failures_do_not_bypass_failure_limit_on_resume(factory):
    controller, handles = factory()
    controller.start(settings(failure_limit=1))
    wait_for(lambda: len(handles) == 1)
    controller.pause()
    handles[0].finish()
    wait_for(lambda: controller.status()["active"] == 0)
    controller.resume()
    wait_for(lambda: controller.status()["status"] == "failed")
    assert controller.status()["reason"] == "failure_limit"
    assert len(handles) == 1


@pytest.mark.parametrize("category", ["pool_empty", "mail_error", "rate_limited", "registration_error"])
def test_systemic_failures_pause_replacements_but_keep_active_workers(factory, category):
    controller, handles = factory()
    controller.start(settings(concurrency=2))
    wait_for(lambda: len(handles) == 2)
    handles[0].finish(category=category)
    wait_for(lambda: controller.status()["status"] == "paused")
    assert controller.status()["reason"] == category
    handles[1].finish(True, "a")
    wait_for(lambda: controller.status()["active"] == 0)
    assert len(handles) == 2
    assert controller.status()["qualified"] == 1
