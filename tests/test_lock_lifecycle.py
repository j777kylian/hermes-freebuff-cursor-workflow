"""Regression tests for the project-lock lifecycle.

Covers the bug where a task reached a terminal state but its lock file
(``runtime/locks/<hash>.lock``) was never removed, leaving a stale PID behind
that required manual deletion. Also covers correct stale-lock inspection and
the DECISION_REQUIRED protection guarantee.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from workflow import bridge as bridge_mod
from workflow import config, doctor, runner, state
from workflow.bridge import Bridge
from workflow.freebuff import FreebuffError, FreebuffSession
from workflow.state import (
    BLOCKED,
    DECISION_REQUIRED,
    FAILED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    PASS,
    STOPPED,
    StateStore,
    TaskLock,
    TransitionError,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    (repo / ".workflow" / "CURRENT_TASK.md").write_text(
        "# Task\nDo the thing.\n", encoding="utf-8"
    )
    return repo


def _write_fast_config() -> None:
    """Speed up the bridge monitor poll interval for tests."""
    home = config.config_dir()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        "[workflow]\npoll_interval_seconds = 1\n", encoding="utf-8"
    )


def _wait_for_state(store: StateStore, target: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = store.load()
        if st is not None and st.state == target:
            return
        time.sleep(0.02)
    last = store.load().state if store.load() else None
    raise AssertionError(f"state never reached {target!r} (last={last!r})")


def _drive_to_terminal(store: StateStore, target: str) -> None:
    """Perform valid state transitions from FREEBUFF_RUNNING to ``target``."""
    st = store.load()
    assert st is not None and st.state == FREEBUFF_RUNNING
    if target in (PASS, BLOCKED):
        store.transition("CURSOR_RUNNING")
        store.transition("REVIEWING")
        store.transition(target)
    elif target == FAILED:
        store.transition(FAILED, last_error="boom")
    elif target == STOPPED:
        store.transition(STOPPED, reason="stopped by user")
    else:
        raise AssertionError(f"unsupported terminal target {target!r}")


def _install_fake(monkeypatch, **kwargs):
    """Monkeypatch FreebuffController + hermes.send; return the fake controller."""
    controller = _FakeFreebuffController(**kwargs)
    monkeypatch.setattr(bridge_mod, "FreebuffController", lambda *a, **k: controller)
    monkeypatch.setattr(bridge_mod.hermes, "send", lambda **kw: None)
    return controller


class _FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()

    def has_session(self, session: str) -> bool:
        return session in self.sessions

    def kill_session(self, session: str) -> None:
        self.sessions.discard(session)


class _FakeFreebuffController:
    """Minimal controller implementing every surface ``Bridge._run`` touches."""

    def __init__(self, *, startup_error: Exception | None = None, delivery_error=None):
        self.tmux = _FakeTmux()
        self.startup_error = startup_error
        self.delivery_error = delivery_error
        self.repo: str | None = None
        self.started: list[str] = []
        self.stopped: list[str] = []

    def resolve_model_id(self) -> str:
        return ""

    def persist_model(self, model_id: str) -> None:
        pass

    def start(self, repo: str, *, session: str | None = None):
        self.repo = repo
        self.started.append(session)
        return FreebuffSession(
            session=session, pane=f"{session}:0.0", repo=repo, command=["freebuff"]
        )

    def wait_for_startup(self, session, model_id=None):
        if self.startup_error is not None:
            raise self.startup_error

    def snapshot(self, session, log_path):
        pass

    def deliver_prompt(self, session, text):
        if self.delivery_error is not None:
            raise self.delivery_error
        # Simulate the supervisor's first durable action: acknowledge the task.
        assert self.repo is not None
        st = StateStore(self.repo).load()
        if st is not None and st.state == FREEBUFF_STARTING:
            runner.supervisor_started(self.repo, st.task_id)

    def stop(self, session):
        self.stopped.append(session)

    def is_alive(self, session) -> bool:
        return True


def _run_bridge_to_terminal(repo: Path, target: str) -> dict:
    """Run the bridge in a thread and drive the state machine to ``target``."""
    holder: dict = {}
    t = threading.Thread(
        target=lambda: holder.setdefault("outcome", Bridge().start_repo(str(repo))),
        daemon=True,
    )
    t.start()
    try:
        store = StateStore(repo)
        _wait_for_state(store, FREEBUFF_RUNNING)
        _drive_to_terminal(store, target)
    finally:
        t.join(timeout=20)
    assert not t.is_alive(), f"bridge did not finish after driving to {target}"
    return holder["outcome"]


# ---------------------------------------------------------------------------
# TaskLock: release removes the lock file
# ---------------------------------------------------------------------------


def test_lock_file_removed_on_release(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.5)
    assert lock.acquire() is True
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()


def test_lock_file_removed_on_context_exit(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.5)
    with lock:
        assert lock.path.exists()
    assert not lock.path.exists()


def test_lock_file_removed_on_exception(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.5)
    with pytest.raises(RuntimeError):
        with lock:
            raise RuntimeError("boom")
    # Unexpected exception -> lock file removed.
    assert not lock.path.exists()


def test_release_does_not_delete_recreated_lock(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.5)
    assert lock.acquire() is True
    path = lock.path
    # Simulate a later owner taking over: the file is recreated with a new
    # ownership record while the old holder is still around.
    path.unlink()
    path.write_text(json.dumps({"pid": 999999, "started": 0.0}), encoding="utf-8")
    lock.release()
    # The recreated lock must NOT be deleted by the old holder.
    assert path.exists()


def test_acquire_after_recreate_locks_current_file(tmp_path: Path):
    repo = tmp_path / "repo"
    a = TaskLock(repo, timeout_seconds=0.5)
    assert a.acquire() is True
    path = a.path
    # A releaser unlinks the file while a still holds its fd.
    path.unlink()
    # A new acquirer must lock the current (new) inode, not the unlinked one.
    b = TaskLock(repo, timeout_seconds=0.5)
    assert b.acquire() is True
    b.release()
    assert not path.exists()
    # Releasing the stale inode is a safe no-op on the path.
    a.release()


# ---------------------------------------------------------------------------
# Bridge lifecycle: terminal cleanup releases the project lock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [PASS, FAILED, BLOCKED, STOPPED])
def test_terminal_state_removes_lock(monkeypatch, tmp_path: Path, target):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch)
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path
    assert not lock_path.exists()

    outcome = _run_bridge_to_terminal(repo, target)
    assert outcome["final_state"] == target
    # The project lock file must have been removed automatically.
    assert not lock_path.exists()


def test_failed_cleanup_releases_lock(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch, delivery_error=FreebuffError("delivery boom"))
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path

    outcome = Bridge().start_repo(str(repo))
    assert outcome["final_state"] == FAILED
    assert not lock_path.exists()


def test_startup_exception_cleanup_releases_lock(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch, startup_error=FreebuffError("startup boom"))
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path

    outcome = Bridge().start_repo(str(repo))
    assert outcome["final_state"] == FAILED
    assert not lock_path.exists()


def test_decision_required_keeps_project_protected(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch)
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path

    holder: dict = {}
    t = threading.Thread(
        target=lambda: holder.setdefault("outcome", Bridge().start_repo(str(repo))),
        daemon=True,
    )
    t.start()
    try:
        store = StateStore(repo)
        _wait_for_state(store, FREEBUFF_RUNNING)
        store.transition(DECISION_REQUIRED, last_error="human decision required")
        _wait_for_state(store, DECISION_REQUIRED)
        # While the task is still active and awaiting a decision, the project
        # lock is held: a second task against the same repo must be rejected.
        assert TaskLock(str(repo), timeout_seconds=0.2).acquire() is False
        assert lock_path.exists()
        # Resolve the decision by moving to a terminal state.
        store.transition(STOPPED, reason="decision resolved")
    finally:
        t.join(timeout=20)
    assert not t.is_alive()
    assert holder["outcome"]["final_state"] == STOPPED
    assert not lock_path.exists()


def test_active_task_blocks_second_task(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch)
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path

    holder: dict = {}
    t = threading.Thread(
        target=lambda: holder.setdefault("outcome", Bridge().start_repo(str(repo))),
        daemon=True,
    )
    t.start()
    try:
        store = StateStore(repo)
        _wait_for_state(store, FREEBUFF_RUNNING)
        # Direct lock contention: a second TaskLock cannot acquire.
        assert TaskLock(str(repo), timeout_seconds=0.2).acquire() is False
        # Bridge-level: a second task against the same repo is rejected.
        with pytest.raises(TransitionError):
            Bridge().start_repo(str(repo))
        store.transition(STOPPED, reason="done")
    finally:
        t.join(timeout=20)
    assert not t.is_alive()
    assert not lock_path.exists()


def test_terminal_task_allows_second_task(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path)
    _write_fast_config()
    _install_fake(monkeypatch)
    lock = TaskLock(str(repo), timeout_seconds=5.0)
    lock_path = lock.path

    outcome = _run_bridge_to_terminal(repo, STOPPED)
    assert outcome["final_state"] == STOPPED
    assert StateStore(repo).load().state == STOPPED
    assert not lock_path.exists()

    # After terminal cleanup, a new task against the same repo is allowed.
    fresh = TaskLock(str(repo), timeout_seconds=5.0)
    assert fresh.acquire() is True
    fresh.release()
    assert not fresh.path.exists()


# ---------------------------------------------------------------------------
# Stale-lock inspection
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    return pid


def test_lock_is_stale_for_dead_pid(tmp_path: Path):
    path = tmp_path / "dead.lock"
    path.write_text(json.dumps({"pid": _dead_pid(), "started": time.time()}), encoding="utf-8")
    assert state.lock_is_stale(path) is True


def test_lock_is_stale_for_legacy_plain_pid(tmp_path: Path):
    # The exact artifact from the reported bug: a plain PID, no JSON metadata.
    path = tmp_path / "legacy.lock"
    path.write_text(str(_dead_pid()), encoding="utf-8")
    assert state.lock_is_stale(path) is True


def test_lock_active_for_live_owner(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.5)
    assert lock.acquire() is True
    # The current test process is the live workflow owner: not stale.
    assert state.lock_is_stale(lock.path) is False
    lock.release()


def test_lock_stale_for_reused_pid(tmp_path: Path):
    path = tmp_path / "reused.lock"
    # A live PID (ours) but a start time far in the past -> PID was reused.
    path.write_text(json.dumps({"pid": os.getpid(), "started": 1.0}), encoding="utf-8")
    assert state.lock_is_stale(path) is True


def test_doctor_stale_lock_inspection_and_repair(tmp_path: Path):
    repo = tmp_path / "repo"
    live = TaskLock(repo, timeout_seconds=0.5)
    assert live.acquire() is True

    lock_dir = config.runtime_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    dead_file = lock_dir / "dead.lock"
    dead_file.write_text(str(_dead_pid()), encoding="utf-8")

    # Only the dead lock is reported stale; the live owner's lock is active.
    check = doctor._check_stale_lock_files()
    assert check.status == doctor.WARN
    assert str(dead_file) in check.detail
    assert str(live.path) not in check.detail

    # Repair removes only proven-stale artifacts, never a live owner's lock.
    removed = doctor.repair_stale_runtime()
    assert str(dead_file) in removed
    assert not dead_file.exists()
    assert live.path.exists()

    live.release()
    assert not live.path.exists()
    assert doctor._check_stale_lock_files().status == doctor.PASS
