"""Report generation.

The full report is always written to disk (``.workflow/``). A concise
Discord-oriented summary (<= ~1800 chars) is produced for ``hermes send``.
"""

from __future__ import annotations

import time
from pathlib import Path

from .state import BLOCKED, FAILED, PASS, TaskState

MAX_DISCORD_CHARS = 1800


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def summarize_for_discord(text: str, max_chars: int = MAX_DISCORD_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 80].rstrip()
    tail = text[-60:].lstrip()
    return f"{head}\n…[truncated]…\n{tail}"


def render_final_report(state: TaskState, summary: str, details: str = "") -> str:
    return (
        f"# Final Report\n\n"
        f"- Task id: `{state.task_id}`\n"
        f"- Project: `{state.alias}` ({state.repo})\n"
        f"- Result: **{state.state}**\n"
        f"- Repair iterations: {state.repair_iterations} / {state.max_repair_iterations}\n"
        f"- Completed: {utc_now()}\n\n"
        f"## Summary\n\n{summary.strip()}\n\n"
        f"## Details\n\n{details.strip() or '_No additional details._'}\n"
    )


def render_decision_request(state: TaskState, question: str, options: str = "") -> str:
    return (
        f"# Decision Required\n\n"
        f"- Task id: `{state.task_id}`\n"
        f"- Project: `{state.alias}` ({state.repo})\n\n"
        f"## Question\n\n{question.strip()}\n\n"
        f"## Options\n\n{options.strip() or '_No options provided._'}\n\n"
        f"Respond with:\n"
        f"`/devdecide {state.task_id} <your decision>`\n"
    )


def render_terminal_message(state: TaskState, extra: str = "") -> str:
    label = {
        PASS: "PASS",
        BLOCKED: "BLOCKED",
        FAILED: "FAILED",
    }.get(state.state, state.state)
    lines = [
        f"[{label}] task `{state.task_id}` on `{state.alias}`.",
        f"Repair iterations: {state.repair_iterations}/{state.max_repair_iterations}.",
    ]
    if extra.strip():
        lines.append(extra.strip())
    return "\n".join(lines)


def write_final_report(
    repo: str | Path, state: TaskState, summary: str, details: str = ""
) -> Path:
    path = Path(repo) / ".workflow" / "FINAL_REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_final_report(state, summary, details), encoding="utf-8")
    return path


def write_decision_required(
    repo: str | Path, state: TaskState, question: str, options: str = ""
) -> Path:
    path = Path(repo) / ".workflow" / "DECISION_REQUIRED.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_decision_request(state, question, options), encoding="utf-8")
    return path


def read_md(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""
