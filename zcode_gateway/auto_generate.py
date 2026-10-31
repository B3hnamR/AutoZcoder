"""Small, callback-driven automatic generation scheduler.

``done`` must mean result processing and registration have finished, not just
that the child process exited. The owner supplies and terminates worker handles;
this module never discovers or kills processes belonging to another run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from collections import deque


DEFAULT_CONFIG = {
    "mode": "goal", "target": 20, "attempt_limit": 100, "concurrency": 1,
    "cooldown_seconds": 60, "ip_source": "direct", "captcha": "ai",
    "timeout_seconds": 900, "failure_limit": 5,
    "browser_display": "background",
}
_BOUNDS = {
    "target": (1, 1000), "attempt_limit": (1, 10000), "concurrency": (1, 3),
    "cooldown_seconds": (0, 3600), "timeout_seconds": (60, 3600),
    "failure_limit": (1, 100),
}
_STATUSES = {
    "idle", "preparing", "running", "paused", "stopping", "completed",
    "stopped", "interrupted", "failed",
}
_ACTIVE_STATUSES = {"preparing", "running", "paused", "stopping"}
_CATEGORIES = {
    "qualified", "duplicate", "failed", "no_plan", "signup_failed",
    "registration_failed", "launch_failed", "timeout", "stopped",
    "interrupted", "proxy_unavailable", "captcha_failed", "other",
    "mail_error", "pool_empty", "rate_limited", "generated", "registration_error",
    "finish_signup_failed", "browser_failed", "mail_timeout",
}
_PAUSE_CATEGORIES = {"pool_empty", "mail_error", "mail_timeout", "rate_limited", "registration_error", "finish_signup_failed"}
_REASONS = {
    "", "preflight", "user_paused", "user_stopped", "process_restart",
    "invalid_saved_config", "invalid_saved_progress", "persistence_error",
    "preflight_failed", "thread_start_failed", "failure_limit", "goal_reached",
    "attempts_exhausted", "scheduler_error", "restored",
} | _PAUSE_CATEGORIES
_COUNTERS = (
    "attempts", "completed_attempts", "qualified", "failed", "duplicates",
    "interrupted_attempts", "consecutive_failures", "no_plan",
)


def validate_config(config):
    """Return a complete, independent configuration; never coerce input types."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    if set(config) - set(DEFAULT_CONFIG):
        raise ValueError("unknown config fields")
    value = {**DEFAULT_CONFIG, **config}
    for name, (low, high) in _BOUNDS.items():
        if type(value[name]) is not int or not low <= value[name] <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    for name, choices in {
        "mode": ("goal", "attempts"), "ip_source": ("direct", "proxy"),
        "captcha": ("ai",),
        "browser_display": ("background", "desktop"),
    }.items():
        if not isinstance(value[name], str) or value[name] not in choices:
            raise ValueError(f"invalid {name}")
    if value["mode"] == "goal" and value["attempt_limit"] < value["target"]:
        raise ValueError("attempt_limit must be at least target in goal mode")
    return value


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        # mkstemp is private on POSIX; chmod also restricts existing destinations
        # after replace. Windows applies its normal inherited directory ACL.
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class AutoGenerateController:
    """Thread-safe run state. All callbacks execute outside the state lock.

    Public mutators return status snapshots; save_config returns saved settings.
    Validation, preflight and synchronous persistence errors propagate to callers.
    Raw worker errors/exceptions are deliberately not persisted or exposed.
    """

    def __init__(self, data_dir, launch, preflight=None):
        self._config_path = Path(data_dir) / "auto_generate_config.json"
        self._progress_path = Path(data_dir) / "auto_generate_progress.json"
        self._launch = launch
        self._preflight = preflight
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._saved_config = dict(DEFAULT_CONFIG)
        self._run_config = None
        self._status = "idle"
        self._reason = ""
        self._run_id = ""
        self._counts = dict.fromkeys(_COUNTERS, 0)
        self._categories = {}
        self._account_ids = set()
        self._active = {}
        self._ready_at = [0.0] * 3
        self._events = deque(maxlen=200)
        self._event_seq = 0
        self._load()
        if self._status == "interrupted":
            self._event("Session interrupted by a gateway restart; Resume is required.", "warn")

    def _event(self, message, level="info", worker=""):
        self._event_seq += 1
        self._events.append({"id": self._event_seq, "ts": time.time(), "level": level,
                             "message": message, "worker": worker})

    def _load(self):
        try:
            self._saved_config = validate_config(json.loads(self._config_path.read_text("utf-8")))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError):
            self._status, self._reason = "failed", "invalid_saved_config"
        try:
            saved = json.loads(self._progress_path.read_text("utf-8"))
            config = validate_config(saved["config"])
            counts = {key: saved[key] for key in _COUNTERS}
            if any(type(v) is not int or not 0 <= v <= 10000 for v in counts.values()):
                raise ValueError("invalid counters")
            ids = saved["account_ids"]
            if (not isinstance(ids, list) or len(ids) > 1000
                    or any(not isinstance(v, str) or not v or len(v) > 256 for v in ids)):
                raise ValueError("invalid account IDs")
            status = saved["status"]
            if status not in _STATUSES:
                raise ValueError("invalid status")
            active = saved["active"]
            if type(active) is not int or not 0 <= active <= 3:
                raise ValueError("invalid active count")
            categories = saved.get("categories", {})
            if (not isinstance(categories, dict) or any(
                    k not in _CATEGORIES or type(v) is not int or not 0 <= v <= 10000
                    for k, v in categories.items())):
                raise ValueError("invalid categories")
            self._run_config, self._counts = config, counts
            self._account_ids, self._categories = set(ids), categories
            self._run_id = uuid.uuid4().hex
            reason = saved.get("reason", "restored")
            if not isinstance(reason, str) or reason not in _REASONS:
                reason = "restored"
            self._status, self._reason = status, reason
            if status in _ACTIVE_STATUSES or active:
                self._status, self._reason = "interrupted", "process_restart"
                self._counts["interrupted_attempts"] += active
                self._counts["completed_attempts"] += active
                self._categories["interrupted"] = self._categories.get("interrupted", 0) + active
                self._persist_locked()
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, KeyError, OSError):
            self._status, self._reason = "failed", "invalid_saved_progress"

    def config(self):
        with self._lock:
            return dict(self._saved_config)

    def save_config(self, config):
        value = validate_config(config)
        with self._lock:
            _atomic_json(self._config_path, value)
            self._saved_config = value
            return dict(value)

    def status(self):
        with self._lock:
            now = time.monotonic()
            return {
                "status": self._status, "reason": self._reason,
                "id": self._run_id,
                "config": dict(self._run_config or self._saved_config),
                **self._counts, "categories": dict(self._categories),
                "active": len(self._active),
                "cooldowns": [max(0.0, deadline - now) for deadline in self._ready_at[
                    : (self._run_config or self._saved_config)["concurrency"]]],
                "events": [dict(event) for event in self._events],
            }

    def _persist_locked(self):
        if self._run_config is None:
            return
        value = self.status()
        value.pop("cooldowns")
        value.pop("events")
        value["account_ids"] = sorted(self._account_ids)
        _atomic_json(self._progress_path, value)

    def _persist_or_fail_locked(self):
        try:
            self._persist_locked()
            return True
        except OSError:
            self._status, self._reason = "failed", "persistence_error"
            return False

    def start(self, config=None):
        with self._lock:
            value = validate_config(self._saved_config if config is None else config)
            if self._closed:
                raise RuntimeError("controller is shut down")
            if self._status in _ACTIVE_STATUSES or self._active:
                raise RuntimeError("a run is already active")
            self._run_config = value
            self._counts = dict.fromkeys(_COUNTERS, 0)
            self._categories, self._account_ids = {}, set()
            self._ready_at = [0.0] * 3
            self._run_id = uuid.uuid4().hex
            self._status, self._reason = "preparing", "preflight"
            self._events.clear()
            self._event_seq = 0
            self._event("Session requested. Checking environment, inbox, AI and selected route.")
            run_id = self._run_id
        return self._prepare(run_id)

    def _prepare(self, run_id):
        with self._lock:
            if self._run_id != run_id or self._status != "preparing":
                return self.status()
            config = dict(self._run_config)
            try:
                self._persist_locked()
            except OSError:
                self._status, self._reason = "failed", "persistence_error"
                raise
        try:
            if self._preflight:
                self._preflight(config)
        except Exception:
            with self._lock:
                if self._run_id == run_id and self._status == "preparing":
                    self._status, self._reason = "failed", "preflight_failed"
                    self._event("Preflight failed; no new browsers will launch. Review the action error.", "error")
                    self._persist_or_fail_locked()
            raise
        with self._lock:
            if self._run_id != run_id or self._status != "preparing":
                return self.status()
            self._status, self._reason = "running", ""
            self._event("Preflight passed. Session running with %d slot(s), %ds completion cooldown, %s route." %
                        (config["concurrency"], config["cooldown_seconds"], config["ip_source"]))
            if not self._persist_or_fail_locked():
                raise OSError("could not persist run state")
            if self._thread is None or not self._thread.is_alive():
                try:
                    self._thread = threading.Thread(target=self._run, name="auto-generate", daemon=True)
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._status, self._reason = "failed", "thread_start_failed"
                    self._persist_or_fail_locked()
                    raise
            self._wake.set()
            return self.status()

    def pause(self):
        with self._lock:
            if self._status != "running":
                raise RuntimeError("only a running run can be paused")
            self._status, self._reason = "paused", "user_paused"
            self._event("Queue paused. Existing attempts may finish; no replacements will start.")
            if not self._persist_or_fail_locked():
                raise OSError("could not persist pause")
            self._wake.set()
            return self.status()

    def resume(self):
        with self._lock:
            if self._closed or self._status not in {"paused", "interrupted"}:
                raise RuntimeError("only paused or interrupted runs can resume")
            self._status, self._reason = "preparing", "preflight"
            self._event("Resume requested; checking prerequisites again.")
            run_id = self._run_id
        return self._prepare(run_id)

    @staticmethod
    def _stop_handles(handles):
        for handle in handles:
            try:
                handle.stop()
            except Exception:
                pass

    def stop(self):
        with self._lock:
            if self._status in _ACTIVE_STATUSES or self._active or self._status == "interrupted":
                self._status, self._reason = "stopping", "user_stopped"
                self._event("Stop requested. Cancelling pending launches and owned active attempts.", "warn")
                self._ready_at = [0.0] * 3
                handles = []
                for slot in self._active.values():
                    slot["cancelled"] = True
                    if slot["handle"] is not None and not slot["stop_sent"]:
                        slot["stop_sent"] = True
                        handles.append(slot["handle"])
                if not self._active:
                    self._status = "stopped"
                self._persist_or_fail_locked()
            else:
                handles = []
            self._wake.set()
        self._stop_handles(handles)
        return self.status()

    def shutdown(self):
        with self._lock:
            self._closed = True
        result = self.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        return self.status() if thread is not None else result

    def _finish_locked(self, attempt_id, result):
        slot = self._active.pop(attempt_id)
        self._ready_at[slot["index"]] = (
            0.0 if slot["cancelled"] and not slot.get("timed_out") else
            time.monotonic() + self._run_config["cooldown_seconds"]
        )
        self._counts["completed_attempts"] += 1
        result = result if isinstance(result, dict) else {}
        account_id = result.get("account_id")
        category = result.get("category", "failed")
        if not isinstance(category, str) or category not in _CATEGORIES:
            category = "other"
        if category == "qualified":
            category = "failed"
        if slot["cancelled"]:
            category = "timeout" if slot.get("timed_out") else "stopped"
        elif result.get("qualified") is True and isinstance(account_id, str) and 0 < len(account_id) <= 256:
            if account_id in self._account_ids:
                self._counts["duplicates"] += 1
                category = "duplicate"
            else:
                self._account_ids.add(account_id)
                self._counts["qualified"] += 1
                self._counts["consecutive_failures"] = 0
                category = "qualified"
        if category != "qualified":
            self._counts["failed"] += 1
            self._counts["consecutive_failures"] += 1
        if category == "no_plan":
            self._counts["no_plan"] += 1
        self._categories[category] = self._categories.get(category, 0) + 1
        worker = slot.get("worker", "")
        number = attempt_id.rsplit("-", 1)[-1]
        self._event("Attempt %s finished: %s. Qualified %d; finished %d; failed %d." %
                    (number, category.replace("_", " "), self._counts["qualified"],
                     self._counts["completed_attempts"], self._counts["failed"]),
                    "info" if category == "qualified" else "warn", worker)
        diagnostics = {
            "finish_signup_failed": "Captcha/email may have passed, but the provider rejected final signup. No plan was verified; account recovery details were saved privately.",
            "captcha_failed": "The captcha stage did not complete. No plan check was reached.",
            "browser_failed": "Browser launch or page interaction failed. Inspect the worker card for the stage.",
            "mail_timeout": "Verification email did not arrive within the worker wait window.",
            "mail_error": "Mail verification failed. Repair the inbox before resuming.",
            "launch_failed": "Worker could not be started. No browser/signup success should be inferred from an attempt reservation.",
        }
        if category in diagnostics:
            self._event(diagnostics[category], "warn", worker)
        if self._ready_at[slot["index"]] > time.monotonic():
            self._event("Slot %d cooling down for %d seconds before another attempt." %
                        (slot["index"] + 1, self._run_config["cooldown_seconds"]), worker=worker)
        if self._status == "running" and category in _PAUSE_CATEGORIES:
            self._status, self._reason = "paused", category
            self._event("Queue paused for prerequisite failure: " + category.replace("_", " ") + ". Repair it before Resume.", "warn")
        if (self._status == "running" and
                self._counts["consecutive_failures"] >= self._run_config["failure_limit"]):
            self._status, self._reason = "failed", "failure_limit"
            self._event("Consecutive failure limit reached; session stopped launching attempts.", "error")

    def _run(self):
        try:
            self._schedule()
        except Exception:
            with self._lock:
                self._status, self._reason = "failed", "scheduler_error"
                self._persist_or_fail_locked()
                handles = [slot["handle"] for slot in self._active.values() if slot["handle"] is not None]
                self._thread = None
            self._stop_handles(handles)

    def _schedule(self):
        while True:
            self._wake.clear()
            handles = []
            launch = None
            with self._lock:
                changed = False
                now = time.monotonic()
                for attempt_id, slot in list(self._active.items()):
                    handle = slot["handle"]
                    if handle is None:
                        continue
                    if handle.done.is_set():
                        self._finish_locked(attempt_id, getattr(handle, "result", {}))
                        changed = True
                    elif now - slot["started"] >= self._run_config["timeout_seconds"]:
                        if not slot.get("timed_out"):
                            self._event("Attempt deadline reached; stopping its owned worker.", "error", slot.get("worker", ""))
                            slot["cancelled"], slot["timed_out"] = True, True
                if self._status in {"failed", "stopping"}:
                    for slot in self._active.values():
                        slot["cancelled"] = True
                for slot in self._active.values():
                    if slot["cancelled"] and slot["handle"] is not None and not slot["stop_sent"]:
                        slot["stop_sent"] = True
                        handles.append(slot["handle"])
                if self._status == "stopping" and not self._active:
                    self._status = "stopped"
                    self._event("Session stopped. No further attempts will launch.")
                    changed = True
                config = self._run_config
                if (self._status == "running" and
                        self._counts["consecutive_failures"] >= config["failure_limit"]):
                    self._status, self._reason = "failed", "failure_limit"
                    changed = True
                if self._status == "running":
                    budget = config["attempt_limit"]
                    goal_met = config["mode"] == "goal" and self._counts["qualified"] >= config["target"]
                    if not self._active and (goal_met or self._counts["attempts"] >= budget):
                        self._status = "completed"
                        self._reason = "goal_reached" if goal_met else "attempts_exhausted"
                        self._event("Session completed: " + ("qualified goal reached." if goal_met else "attempt budget exhausted."))
                        changed = True
                    elif (not goal_met and self._counts["attempts"] < budget and
                          (config["mode"] == "attempts" or
                           self._counts["qualified"] + len(self._active) < config["target"])):
                        occupied = {slot["index"] for slot in self._active.values()}
                        for index in range(config["concurrency"]):
                            if index not in occupied and self._ready_at[index] <= now:
                                self._counts["attempts"] += 1
                                attempt_id = f"{self._run_id}-{self._counts['attempts']}"
                                self._active[attempt_id] = {
                                    "index": index, "handle": None, "started": now,
                                    "cancelled": False, "stop_sent": False,
                                }
                                launch = (attempt_id, dict(config))
                                changed = True
                                break
                if changed and not self._persist_or_fail_locked():
                    # A reserved attempt must never launch if its durable record failed.
                    if launch:
                        self._finish_locked(launch[0], {"category": "launch_failed"})
                        launch = None
                    handles.extend(slot["handle"] for slot in self._active.values()
                                   if slot["handle"] is not None and not slot["stop_sent"])
                    for slot in self._active.values():
                        slot["cancelled"], slot["stop_sent"] = True, True
                finished = not self._active and self._status not in {"running", "paused", "preparing"}
                if finished:
                    # Clear under the lock so a concurrent start cannot reuse a dying thread.
                    self._thread = None
            self._stop_handles(handles)
            if finished:
                return
            if launch:
                attempt_id, config = launch
                handle = None
                with self._lock:
                    self._event("Attempt %s reserved; launching a worker." % attempt_id.rsplit("-", 1)[-1])
                try:
                    handle = self._launch(config, attempt_id)
                    if not callable(handle.done.is_set) or not callable(handle.stop):
                        raise TypeError("invalid worker handle")
                except Exception:
                    if handle is not None:
                        self._stop_handles([handle])
                    with self._lock:
                        self._finish_locked(attempt_id, {"category": "launch_failed"})
                        self._persist_or_fail_locked()
                else:
                    with self._lock:
                        slot = self._active[attempt_id]
                        slot["handle"] = handle
                        # Worker names are identifiers, never arbitrary output.
                        name = getattr(handle, "name", "")
                        slot["worker"] = name if isinstance(name, str) and len(name) <= 80 and all(c.isalnum() or c in "-_" for c in name) else ""
                        self._event("Worker started. Waiting for signup, plan verification and registration.", worker=slot["worker"])
                        cancel = slot["cancelled"] or self._status in {"stopping", "failed"}
                        if cancel:
                            slot["cancelled"], slot["stop_sent"] = True, True
                    if cancel:
                        self._stop_handles([handle])
            self._wake.wait(0.2)
