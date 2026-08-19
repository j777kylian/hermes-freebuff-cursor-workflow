from __future__ import annotations

from pathlib import Path

import pytest

from workflow import cursor, runner
from workflow.state import (
    BLOCKED,
    CURSOR_RUNNING,
    DECISION_REQUIRED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    REVIEWING,
    TASK_READY,
    StateStore,
)


def _fake_result(ok: bool = True) -> cursor.CursorResult:
    return cursor.CursorResult(
        argv=["agent"],
        stdout="OK",
        stderr="",
        returncode=0 if ok else 1,
        started_at=0.0,
        finished_at=1.0,
    )


def _setup(repo: Path, state: str = TASK_READY) -> None:
    (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    (repo / ".workflow" / "CURRENT_TASK.md").write_text("# task\nfix it\n", encoding="utf-8")
    (repo / ".workflow" / "FREEBUFF_REVIEW.md").write_text("needs repair\n", encoding="utf-8")
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo))
    if state == REVIEWING:
        store.transition(FREEBUFF_STARTING)
        store.transition(FREEBUFF_RUNNING)
        store.transition(CURSOR_RUNNING)
        store.transition(REVIEWING)


def test_cursor_run_sets_reviewing(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup(repo)
    monkeypatch.setattr(cursor, "run_agent", lambda *a, **k: _fake_result())
    result = runner.cursor_run(str(repo))
    assert result.ok
    st = StateStore(repo).load()
    assert st is not None and st.state == REVIEWING
    assert (repo / ".workflow" / "CURSOR_REPORT.md").exists()


def test_cursor_run_rejects_bad_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup(repo, state=REVIEWING)
    # REVIEWING is not a valid starting point for a fresh (non-repair) run.
    with pytest.raises(runner.RunnerError):
        runner.cursor_run(str(repo), repair=False)


def test_repair_loop_blocks_at_max(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup(repo)
    monkeypatch.setattr(cursor, "run_agent", lambda *a, **k: _fake_result())
    # First implementation pass -> REVIEWING.
    runner.cursor_run(str(repo))
    store = StateStore(repo)
    # Repair passes until max (3) is exceeded.
    for _ in range(3):
        runner.cursor_run(str(repo), repair=True)
    st = store.load()
    assert st is not None and st.state == REVIEWING
    assert st.repair_iterations == 3
    # The 4th repair attempt should BLOCK.
    with pytest.raises(runner.RunnerError, match="max repair"):
        runner.cursor_run(str(repo), repair=True)
    st = store.load()
    assert st is not None and st.state == BLOCKED


def test_report_decision_transitions(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _setup(repo, state=REVIEWING)
    path = runner.report_decision(str(repo), "Which option?", "A / B")
    assert path.exists()
    assert StateStore(repo).load().state == DECISION_REQUIRED
