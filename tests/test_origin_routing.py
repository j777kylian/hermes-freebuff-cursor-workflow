"""Focused tests for production Discord origin routing.

Covers: TaskOrigin/TaskState persistence, transition preservation, archive
preservation, delivery precedence (origin vs configured fallback), terminal
state routing, /devdecide non-overwrite, and CLI flag parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow import bridge as bridge_mod
from workflow import config
from workflow.bridge import Bridge
from workflow.cli import _origin_from_args, build_parser
from workflow.registry import Registry
from workflow.state import (
    BLOCKED,
    DECISION_REQUIRED,
    FAILED,
    FREEBUFF_RUNNING,
    FREEBUFF_STARTING,
    PASS,
    REVIEWING,
    TASK_READY,
    StateStore,
    TaskOrigin,
    TaskState,
)


def _origin(**kw) -> TaskOrigin:
    defaults = dict(
        platform="discord",
        chat_type="group",
        chat_id="111",
        thread_id="",
        reply_target="discord:111",
    )
    defaults.update(kw)
    return TaskOrigin(**defaults)


# --- durable state -----------------------------------------------------------


def test_taskorigin_roundtrip():
    o = _origin(chat_type="thread", chat_id="5", thread_id="5", reply_target="discord:5:5")
    assert TaskOrigin.from_dict(o.to_dict()) == o


def test_taskorigin_never_carries_credentials():
    o = _origin()
    d = o.to_dict()
    assert set(d) == {"platform", "chat_type", "chat_id", "thread_id", "reply_target"}
    assert "token" not in json.dumps(d)
    assert "secret" not in json.dumps(d)
    assert "key" not in json.dumps(d)


def test_taskstate_roundtrip_preserves_origin():
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=TASK_READY, origin=_origin())
    restored = TaskState.from_dict(st.to_dict())
    assert restored.origin is not None
    assert restored.origin.reply_target == "discord:111"


def test_transitions_preserve_origin(tmp_path: Path):
    repo = tmp_path / "repo"
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo), origin=_origin())
    store.transition(FREEBUFF_STARTING, session="s")
    store.transition(FREEBUFF_RUNNING, supervisor_started_at="t")
    store.transition("CURSOR_RUNNING")
    store.transition(REVIEWING)
    store.transition(PASS)
    st = store.load()
    assert st is not None and st.state == PASS
    assert st.origin is not None
    assert st.origin.reply_target == "discord:111"


def test_archive_preserves_origin(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo), origin=_origin())
    store.transition(FREEBUFF_STARTING)
    store.transition(FREEBUFF_RUNNING)
    store.transition("CURSOR_RUNNING")
    store.transition(REVIEWING)
    store.transition(BLOCKED)

    st = store.load()
    assert st is not None
    Bridge()._archive(str(repo), st)

    archived = config.runtime_dir() / "archive" / "t1" / "state.json"
    raw = json.loads(archived.read_text(encoding="utf-8"))
    assert raw["origin"]["reply_target"] == "discord:111"


# --- delivery precedence -----------------------------------------------------


def test_resolve_target_prefers_origin():
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=PASS, origin=_origin())
    assert Bridge()._resolve_target(st) == "discord:111"


def test_resolve_target_falls_back_when_no_origin():
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=PASS, origin=None)
    assert Bridge()._resolve_target(st) == "discord:#hermes"


def test_resolve_target_falls_back_for_non_discord_origin():
    o = TaskOrigin(
        platform="telegram", chat_type="dm", chat_id="9", thread_id="",
        reply_target="telegram:9",
    )
    st = TaskState(task_id="t1", alias="demo", repo="/r", state=PASS, origin=o)
    assert Bridge()._resolve_target(st) == "discord:#hermes"


@pytest.mark.parametrize("terminal", [PASS, FAILED, BLOCKED])
def test_terminal_report_uses_task_origin(monkeypatch, terminal):
    sent = {}
    monkeypatch.setattr(bridge_mod.hermes, "send", lambda **kw: sent.update(kw))
    st = TaskState(task_id="t1", alias="demo", repo="/tmp", state=terminal, origin=_origin())
    Bridge()._send_terminal_report(st, "demo")
    assert sent["to"] == "discord:111"


def test_decision_request_uses_task_origin(monkeypatch):
    sent = {}
    monkeypatch.setattr(bridge_mod.hermes, "send", lambda **kw: sent.update(kw))
    st = TaskState(
        task_id="t1", alias="demo", repo="/tmp", state=DECISION_REQUIRED, origin=_origin()
    )
    Bridge()._send_decision_request("demo", "t1", st)
    assert sent["to"] == "discord:111"


def test_cli_task_without_origin_uses_configured_fallback(monkeypatch):
    sent = {}
    monkeypatch.setattr(bridge_mod.hermes, "send", lambda **kw: sent.update(kw))
    st = TaskState(task_id="t1", alias="demo", repo="/tmp", state=PASS, origin=None)
    Bridge()._send_terminal_report(st, "demo")
    assert sent["to"] == "discord:#hermes"


# --- /devdecide must not overwrite origin ------------------------------------


class _DecideController:
    def is_alive(self, session) -> bool:
        return True

    def inject(self, session, text) -> None:
        pass


def test_devdecide_preserves_original_origin(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    store = StateStore(repo)
    store.transition(TASK_READY, task_id="t1", alias="demo", repo=str(repo), origin=_origin())
    store.transition(FREEBUFF_STARTING, session="hfw-freebuff-demo-1")
    store.transition(FREEBUFF_RUNNING, supervisor_started_at="t")
    store.transition(DECISION_REQUIRED, last_error="human decision required")
    Registry().put(
        "t1", alias="demo", repo=str(repo),
        session="hfw-freebuff-demo-1", state=DECISION_REQUIRED,
    )

    monkeypatch.setattr(bridge_mod, "FreebuffController", lambda: _DecideController())
    out = Bridge().decide("t1", "use option B")
    assert out["state"] == FREEBUFF_STARTING

    st = StateStore(repo).load()
    assert st is not None
    # The decision command's own origin must NOT replace the task's original.
    assert st.origin is not None
    assert st.origin.reply_target == "discord:111"
    assert st.origin.chat_id == "111"


# --- CLI flag parsing --------------------------------------------------------


def test_cli_parses_origin_flags():
    args = build_parser().parse_args(
        [
            "devtask", "demo", "Fix the parser",
            "--origin-platform", "discord",
            "--origin-chat-type", "thread",
            "--origin-chat-id", "123",
            "--origin-thread-id", "123",
            "--reply-target", "discord:123:123",
        ]
    )
    assert " ".join(args.task) == "Fix the parser"
    origin = _origin_from_args(args)
    assert origin is not None
    assert origin.platform == "discord"
    assert origin.chat_type == "thread"
    assert origin.chat_id == "123"
    assert origin.thread_id == "123"
    assert origin.reply_target == "discord:123:123"


def test_cli_without_origin_flags_returns_none():
    args = build_parser().parse_args(["devtask", "demo", "Fix the parser"])
    assert _origin_from_args(args) is None
