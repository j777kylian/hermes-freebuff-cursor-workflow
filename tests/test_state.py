from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow import state
from workflow.state import (
    BLOCKED,
    CURSOR_RUNNING,
    DECISION_REQUIRED,
    FAILED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    IDLE,
    PASS,
    REPAIR_REQUIRED,
    REVIEWING,
    STOPPED,
    TASK_READY,
    StateStore,
    TaskLock,
    TaskState,
    TransitionError,
    can_transition,
    validate_transition,
)


def test_terminal_states():
    assert PASS in state.TERMINAL_STATES
    assert BLOCKED in state.TERMINAL_STATES
    assert STOPPED in state.TERMINAL_STATES
    assert FAILED in state.TERMINAL_STATES
    assert DECISION_REQUIRED not in state.TERMINAL_STATES


def test_valid_transitions():
    assert can_transition(IDLE, TASK_READY)
    assert can_transition(TASK_READY, FREEBUFF_STARTING)
    assert can_transition(FREEBUFF_STARTING, FREEBUFF_RUNNING)
    assert can_transition(FREEBUFF_RUNNING, CURSOR_RUNNING)
    assert can_transition(CURSOR_RUNNING, REVIEWING)
    assert can_transition(REVIEWING, REPAIR_REQUIRED)
    assert can_transition(REPAIR_REQUIRED, CURSOR_RUNNING)
    assert can_transition(REVIEWING, PASS)
    assert can_transition(REVIEWING, BLOCKED)
    assert can_transition(REVIEWING, DECISION_REQUIRED)
    assert can_transition(DECISION_REQUIRED, FREEBUFF_STARTING)
    assert can_transition(DECISION_REQUIRED, FREEBUFF_RUNNING)


def test_stop_from_any_non_terminal():
    states = (
        TASK_READY,
        FREEBUFF_RUNNING,
        CURSOR_RUNNING,
        REVIEWING,
        REPAIR_REQUIRED,
        DECISION_REQUIRED,
    )
    for s in states:
        assert can_transition(s, STOPPED), s


def test_invalid_transitions():
    assert not can_transition(IDLE, PASS)
    assert not can_transition(PASS, TASK_READY)
    assert not can_transition(TASK_READY, PASS)
    assert not can_transition(CURSOR_RUNNING, PASS)
    assert not can_transition(FREEBUFF_RUNNING, REVIEWING)
    assert not can_transition(TASK_READY, FREEBUFF_RUNNING)  # must ack first
    assert not can_transition(IDLE, FREEBUFF_STARTING)


def test_unknown_state_rejected():
    with pytest.raises(TransitionError):
        validate_transition(IDLE, "NOT_A_STATE")


def test_store_roundtrip_and_archive(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    st = store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    assert st.state == TASK_READY
    loaded = store.load()
    assert loaded is not None and loaded.task_id == "t1"
    raw = json.loads((store.path).read_text(encoding="utf-8"))
    assert raw["state"] == TASK_READY


def test_store_rejects_illegal_transition(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY)
    with pytest.raises(TransitionError):
        store.transition(PASS)  # TASK_READY -> PASS is illegal


def test_lock_prevents_concurrent(tmp_path: Path):
    repo = tmp_path / "repo"
    lock = TaskLock(repo, timeout_seconds=0.2)
    assert lock.acquire() is True
    other = TaskLock(repo, timeout_seconds=0.2)
    assert other.acquire() is False
    lock.release()
    assert other.acquire() is True
    other.release()


def test_state_roundtrip_via_dataclass():
    ts = TaskState(task_id="x", alias="y", repo="z", state=IDLE)
    data = ts.to_dict()
    assert data["state"] == IDLE
    assert TaskState.from_dict(data).task_id == "x"


def test_created_at_initialized_and_stable(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    # Simulate the project-template state (created_at/updated_at empty).
    store.save(TaskState(task_id="", repo=str(repo), state=IDLE, created_at="", updated_at=""))
    st = store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    assert st.created_at
    first = st.created_at
    store.transition(FREEBUFF_STARTING)
    store.transition(FREEBUFF_RUNNING)
    assert store.load().created_at == first


def test_updated_at_changes_on_transition(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    stamps = iter(
        ["2026-08-18T00:00:01Z", "2026-08-18T00:00:02Z", "2026-08-18T00:00:03Z"]
    )
    monkeypatch.setattr(state, "utc_now", lambda: next(stamps))
    st = store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    before = st.updated_at
    store.transition(FREEBUFF_STARTING)
    assert store.load().updated_at != before


def test_history_from_preserves_previous(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    store.transition(FREEBUFF_STARTING)
    store.transition(FREEBUFF_RUNNING, supervisor_started_at="2026-08-18T00:00:00Z")
    history = store.load().history
    assert [h["from"] for h in history] == [IDLE, TASK_READY, FREEBUFF_STARTING]
    assert [h["to"] for h in history] == [TASK_READY, FREEBUFF_STARTING, FREEBUFF_RUNNING]


def test_stop_sets_terminal_reason_not_last_error(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    st = store.transition(STOPPED, reason="stopped by user")
    assert st.terminal_reason == "stopped by user"
    assert st.last_error == ""


def test_failed_sets_last_error(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    st = store.transition(FAILED, last_error="boom")
    assert st.last_error == "boom"
    # FAILED uses last_error, not terminal_reason (which is for STOPPED).
    assert st.terminal_reason == ""


def test_running_requires_ack_path(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="a", repo=str(repo))
    # TASK_READY -> FREEBUFF_RUNNING is illegal: the ack (STARTING -> RUNNING)
    # is the only path into RUNNING.
    with pytest.raises(TransitionError):
        store.transition(FREEBUFF_RUNNING)
