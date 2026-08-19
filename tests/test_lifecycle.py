from __future__ import annotations

from pathlib import Path

import pytest

from workflow import bridge as bridge_mod
from workflow import runner
from workflow.bridge import Bridge
from workflow.registry import Registry
from workflow.state import (
    FAILED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    STOPPED,
    TASK_READY,
    StateStore,
)


class FakeTmux:
    def __init__(self, sessions=None):
        self.sessions = set(sessions or [])
        self.killed: list[str] = []

    def has_session(self, session: str) -> bool:
        return session in self.sessions

    def kill_session(self, session: str) -> None:
        if session in self.sessions:
            self.sessions.remove(session)
            self.killed.append(session)

    def list_sessions(self) -> list[str]:
        return sorted(self.sessions)


class FakeController:
    def __init__(self, tmux=None):
        self.tmux = tmux if tmux is not None else FakeTmux()


def _create_task(
    repo: Path,
    task_id: str = "t1",
    alias: str = "demo",
    session: str = "hfw-freebuff-demo-123",
    running: bool = True,
) -> StateStore:
    (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    store = StateStore(repo)
    store.transition(TASK_READY, task_id=task_id, alias=alias, repo=str(repo))
    store.transition(FREEBUFF_STARTING, session=session)
    if running:
        store.transition(FREEBUFF_RUNNING, supervisor_started_at="2026-08-18T00:00:00Z")
    Registry().put(
        task_id, alias=alias, repo=str(repo), session=session,
        state=FREEBUFF_RUNNING if running else FREEBUFF_STARTING,
    )
    return store


@pytest.fixture
def fake_freebuff(monkeypatch):
    shared = FakeTmux()
    monkeypatch.setattr(
        bridge_mod, "FreebuffController", lambda *a, **k: FakeController(tmux=shared)
    )
    return shared


def test_status_reconciles_dead_running(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, session="hfw-freebuff-demo-123")  # session NOT in fake tmux
    info = Bridge().status("demo")
    assert info["state"] == FAILED
    assert "exited unexpectedly" in info["last_error"]
    assert StateStore(repo).load().state == FAILED


def test_status_reconciles_dead_starting(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, session="hfw-freebuff-demo-123", running=False)
    info = Bridge().status("t1")
    assert info["state"] == FAILED


def test_stop_dead_process_is_safe_and_idempotent(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, session="hfw-freebuff-demo-123")
    out = Bridge().stop("demo")
    assert out["state"] == STOPPED
    st = StateStore(repo).load()
    assert st.state == STOPPED
    assert st.terminal_reason == "stopped by user"
    assert st.last_error == ""  # a normal stop is not an error
    # Second stop is a no-op and must not raise.
    out2 = Bridge().stop("demo")
    assert out2["state"] == STOPPED


def test_stop_kills_only_owned_session(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, session="hfw-freebuff-demo-123")
    fake_freebuff.sessions = {"hfw-freebuff-demo-123", "unrelated-session"}
    Bridge().stop("t1")
    assert fake_freebuff.killed == ["hfw-freebuff-demo-123"]
    assert "unrelated-session" in fake_freebuff.sessions


def test_stop_ignores_non_owned_session_name(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, session="some-other-app")
    fake_freebuff.sessions = {"some-other-app"}
    Bridge().stop("demo")
    # Session name does not carry our prefix: it must not be killed.
    assert fake_freebuff.killed == []


def test_status_by_alias_and_task_id(tmp_path, fake_freebuff):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_task(repo, task_id="t9", alias="demo", session="hfw-freebuff-demo-123")
    fake_freebuff.sessions = {"hfw-freebuff-demo-123"}
    by_alias = Bridge().status("demo")
    by_task = Bridge().status("t9")
    assert by_alias["task_id"] == "t9"
    assert by_task["task_id"] == "t9"
    assert by_alias["state"] == FREEBUFF_RUNNING


# ---------------------------------------------------------------------- ack


def test_supervisor_ack_transitions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo))
    store.transition(FREEBUFF_STARTING)
    st = runner.supervisor_started(str(repo), "t1")
    assert st.state == FREEBUFF_RUNNING
    assert st.supervisor_started_at


def test_supervisor_ack_wrong_task_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo))
    store.transition(FREEBUFF_STARTING)
    with pytest.raises(runner.RunnerError, match="wrong task"):
        runner.supervisor_started(str(repo), "other-task")


def test_supervisor_ack_wrong_state_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo))
    with pytest.raises(runner.RunnerError, match="FREEBUFF_STARTING"):
        runner.supervisor_started(str(repo), "t1")


def test_supervisor_ack_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo))
    store.transition(FREEBUFF_STARTING)
    first = runner.supervisor_started(str(repo), "t1")
    second = runner.supervisor_started(str(repo), "t1")
    assert first.state == second.state == FREEBUFF_RUNNING
    assert first.supervisor_started_at == second.supervisor_started_at
