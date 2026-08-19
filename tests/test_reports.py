from __future__ import annotations

from workflow import reports
from workflow.state import PASS, TaskState


def test_summarize_short_text_unchanged():
    assert reports.summarize_for_discord("hello") == "hello"


def test_summarize_truncates_long_text():
    long = "x" * 5000
    out = reports.summarize_for_discord(long, max_chars=1800)
    assert len(out) <= 1800
    assert "…[truncated]…" in out


def test_render_final_report_contains_result():
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=PASS)
    out = reports.render_final_report(st, "All good.", "Tests pass.")
    assert "PASS" in out
    assert "t1" in out
    assert "All good." in out


def test_render_terminal_message():
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=PASS)
    msg = reports.render_terminal_message(st)
    assert "PASS" in msg
    assert "t1" in msg
