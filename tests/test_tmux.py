from __future__ import annotations

from workflow.tmux import session_name


def test_session_name_sanitizes():
    name = session_name("weird/name with spaces", prefix="hfw-freebuff")
    assert name.startswith("hfw-freebuff-")
    assert "/" not in name
    assert " " not in name
    assert len(name) <= 64


def test_session_name_deterministic():
    assert session_name("demo", prefix="p") == session_name("demo", prefix="p")
