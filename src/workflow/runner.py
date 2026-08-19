"""Deterministic runner commands invoked by the Freebuff supervisor.

These are the only "worker" commands Freebuff needs for the implementation
loop. Each one validates the state machine and records durable evidence, so
correctness does not depend on Freebuff remembering anything.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import cursor, prompting, reports
from .state import (
    BLOCKED,
    CURSOR_RUNNING,
    DECISION_REQUIRED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    REPAIR_REQUIRED,
    REVIEWING,
    TASK_READY,
    StateStore,
    TaskState,
    utc_now,
)


class RunnerError(RuntimeError):
    pass


def _load_state(repo: str) -> TaskState:
    store = StateStore(repo)
    st = store.load()
    if st is None:
        raise RunnerError(f"no state.json for {repo}; start a task first")
    return st


def _resolve_prompt(repo: str, prompt_file: str | None, repair: bool) -> str:
    if prompt_file:
        p = Path(prompt_file)
        if not p.is_absolute():
            p = Path(repo) / p
        if not p.exists():
            raise RunnerError(f"prompt file not found: {p}")
        return p.read_text(encoding="utf-8")

    if repair:
        repair_prompt = Path(repo) / ".workflow" / "CURSOR_REPAIR_PROMPT.md"
        if repair_prompt.exists():
            return repair_prompt.read_text(encoding="utf-8")
        review = (Path(repo) / ".workflow" / "FREEBUFF_REVIEW.md").read_text(encoding="utf-8")
        task = (Path(repo) / ".workflow" / "CURRENT_TASK.md").read_text(encoding="utf-8")
        return prompting.render("cursor_repair.md", TASK=task, REPAIR=review, REPO=repo)

    impl_prompt = Path(repo) / ".workflow" / "CURSOR_PROMPT.md"
    if impl_prompt.exists():
        return impl_prompt.read_text(encoding="utf-8")
    task = (Path(repo) / ".workflow" / "CURRENT_TASK.md").read_text(encoding="utf-8")
    context = ""
    plan = Path(repo) / ".workflow" / "PLAN.md"
    if plan.exists():
        context = plan.read_text(encoding="utf-8")
    return prompting.render("cursor_implement.md", TASK=task, REPO=repo, CONTEXT=context)


def write_cursor_report(repo: str, result: cursor.CursorResult, prompt: str) -> Path:
    path = Path(repo) / ".workflow" / "CURSOR_REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tail_stdout = result.stdout[-4000:]
    tail_stderr = result.stderr[-4000:]
    path.write_text(
        f"# Cursor Report\n\n"
        f"- Exit code: {result.returncode}\n"
        f"- Timed out: {result.timed_out}\n"
        f"- Duration: {result.duration:.1f}s\n"
        f"- Finished: {utc_now()}\n"
        f"- Log: {result.log_path or 'n/a'}\n\n"
        f"## Prompt\n\n```\n{prompt[:2000]}\n```\n\n"
        f"## stdout (tail)\n\n```\n{tail_stdout}\n```\n\n"
        f"## stderr (tail)\n\n```\n{tail_stderr}\n```\n",
        encoding="utf-8",
    )
    return path


def cursor_run(
    repo: str,
    *,
    prompt_file: str | None = None,
    repair: bool = False,
    model: str | None = None,
) -> cursor.CursorResult:
    """Run one Cursor implementation/repair pass under state-machine control."""
    store = StateStore(repo)
    st = _load_state(repo)

    if repair:
        if st.state not in (REVIEWING, REPAIR_REQUIRED):
            raise RunnerError(f"cannot repair from state {st.state}")
        new_iter = st.repair_iterations + 1
        if new_iter > st.max_repair_iterations:
            store.transition(
                BLOCKED,
                repair_iterations=new_iter,
                last_error=f"max repair iterations ({st.max_repair_iterations}) exceeded",
            )
            raise RunnerError(
                f"BLOCKED: max repair iterations ({st.max_repair_iterations}) exceeded"
            )
        store.transition(REPAIR_REQUIRED, repair_iterations=new_iter)
    elif st.state not in (TASK_READY, FREEBUFF_RUNNING):
        raise RunnerError(f"cannot run cursor from state {st.state}")

    prompt = _resolve_prompt(repo, prompt_file, repair)
    store.transition(CURSOR_RUNNING)

    from . import config

    log_path = str(
        config.logs_dir() / f"cursor-{Path(repo).name}-{int(time.time())}.log"
    )
    result = cursor.run_agent(prompt, repo, model=model, log_path=log_path)
    write_cursor_report(repo, result, prompt)
    store.transition(REVIEWING, last_error="" if result.ok else "cursor exited non-zero")
    return result


def state_set(repo: str, state: str, reason: str = "") -> TaskState:
    store = StateStore(repo)
    return store.transition(state, last_error=reason or "")


def supervisor_started(repo: str, task_id: str = "") -> TaskState:
    """Durable startup acknowledgement from the Freebuff supervisor.

    This is the ONLY way a task may enter FREEBUFF_RUNNING. It validates the
    task id, validates the legal state (FREEBUFF_STARTING), records a
    timestamp, and writes the transition to ``state.json``. Stale or wrong
    acknowledgements are rejected.
    """
    store = StateStore(repo)
    st = store.load()
    if st is None:
        raise RunnerError(f"no state.json for {repo}; start a task first")
    task_id = (task_id or "").strip()
    if task_id and st.task_id and task_id != st.task_id:
        raise RunnerError(
            f"supervisor acknowledgement for wrong task {task_id!r} "
            f"(current task: {st.task_id!r})"
        )
    if st.state == FREEBUFF_RUNNING and st.supervisor_started_at:
        return st  # idempotent: already acknowledged this task
    if st.state != FREEBUFF_STARTING:
        raise RunnerError(
            f"cannot acknowledge supervisor from state {st.state} "
            f"(expected FREEBUFF_STARTING)"
        )
    return store.transition(
        FREEBUFF_RUNNING, supervisor_started_at=utc_now(), last_error=""
    )


def report_final(repo: str, summary_file: str, details_file: str = "") -> Path:
    st = _load_state(repo)
    summary = (
        Path(summary_file).read_text(encoding="utf-8") if Path(summary_file).exists() else ""
    )
    details = (
        Path(details_file).read_text(encoding="utf-8")
        if details_file and Path(details_file).exists()
        else ""
    )
    return reports.write_final_report(repo, st, summary, details)


def report_decision(repo: str, question: str, options: str = "") -> Path:
    st = _load_state(repo)
    path = reports.write_decision_required(repo, st, question, options)
    StateStore(repo).transition(DECISION_REQUIRED, last_error="human decision required")
    return path
