from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workflow.freebuff import FreebuffController, FreebuffError, FreebuffSession
from workflow.subprocess_util import CommandResult, run
from workflow.tmux import Tmux


class RecordingTmux(Tmux):
    def __init__(self):
        super().__init__("tmux")
        self.argv_calls: list[list[str]] = []
        self.pasted_texts: list[str] = []

    def _run(self, argv: list[str], *, check: bool = False) -> CommandResult:
        full = [self.command, *argv]
        self.argv_calls.append(list(full))
        if argv and argv[0] == "load-buffer":
            # argv: [load-buffer, -b, hfw-paste, <tmpfile>]
            self.pasted_texts.append(Path(argv[-1]).read_text(encoding="utf-8"))
        return CommandResult(
            argv=list(full), stdout="", stderr="", returncode=0,
            started_at=0.0, finished_at=0.0,
        )


def test_send_prompt_preserves_multiline_unicode_and_submits():
    tmux = RecordingTmux()
    text = "line one\nline two; $(rm -rf /) 'quoted' ünïcödé 日本語\nthird"
    tmux.send_prompt("s:0.0", text)
    # The full literal text (multiline + unicode + shell metacharacters) is
    # written verbatim to the paste file — never reinterpreted as shell code.
    assert tmux.pasted_texts == [text]
    # Clear composer first, paste with bracketed paste, then submit.
    assert ["tmux", "send-keys", "-t", "s:0.0", "C-u"] in tmux.argv_calls
    paste = [a for a in tmux.argv_calls if a[1] == "paste-buffer"]
    assert paste and paste[0][2] == "-d"
    assert ["tmux", "send-keys", "-t", "s:0.0", "Enter"] in tmux.argv_calls


def test_send_text_omits_enter_when_requested():
    tmux = RecordingTmux()
    tmux.send_text("s:0.0", "hello", send_enter=False)
    assert not any(a[1] == "send-keys" and "Enter" in a for a in tmux.argv_calls)


def test_deliver_prompt_retries_bounded(monkeypatch):
    controller = FreebuffController()
    calls: list[int] = []

    def fake_inject(session, text):
        calls.append(1)
        raise FreebuffError("nope")

    monkeypatch.setattr(controller, "inject", fake_inject)
    monkeypatch.setattr(controller.tmux, "has_session", lambda s: True)
    monkeypatch.setattr("workflow.freebuff.time.sleep", lambda *a, **k: None)
    session = FreebuffSession("s", "s:0.0", "/tmp", ["freebuff"])
    with pytest.raises(FreebuffError, match="3 attempts"):
        controller.deliver_prompt(session, "x", retries=3)
    assert len(calls) == 3


def test_run_never_uses_shell(monkeypatch):
    captured: dict[str, object] = {}
    real = subprocess.run

    def fake(*args, **kwargs):
        captured["shell"] = kwargs.get("shell")
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake)
    run(["python3", "-c", "import sys; sys.exit(0)"], timeout=30)
    assert captured["shell"] in (False, None)
