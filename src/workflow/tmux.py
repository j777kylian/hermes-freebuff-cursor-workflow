"""Low-level tmux helpers.

Prompt injection uses tmux buffers + ``paste-buffer`` (bracketed paste) rather
than naive long-string ``send-keys``, which can lose characters and mis-handle
multi-line TUI input.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import config
from .subprocess_util import CommandResult, run

TMUX = "tmux"


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, command: str = TMUX):
        self.command = command

    def _run(self, argv: list[str], *, check: bool = False) -> CommandResult:
        return run([self.command, *argv], check=check)

    def has_session(self, session: str) -> bool:
        r = self._run(["has-session", "-t", session])
        return r.returncode == 0

    def new_session(self, session: str, command: list[str], cwd: str | None = None) -> None:
        argv = ["new-session", "-d", "-s", session]
        if cwd:
            argv += ["-c", cwd]
        argv += command
        r = self._run(argv)
        if r.returncode != 0:
            raise TmuxError(f"tmux new-session failed: {r.stderr.strip()[:1000]}")

    def send_keys(self, target: str, *keys: str) -> None:
        argv = ["send-keys", "-t", target, *keys]
        r = self._run(argv)
        if r.returncode != 0:
            raise TmuxError(f"tmux send-keys failed: {r.stderr.strip()[:1000]}")

    def clear_line(self, target: str) -> None:
        """Clear the current composer line (C-u), matching Freebuff's own helper."""
        self.send_keys(target, "C-u")

    def submit(self, target: str) -> None:
        """Submit the composed input with Enter."""
        self.send_keys(target, "Enter")

    def send_text(self, target: str, text: str, *, send_enter: bool = True) -> None:
        """Paste ``text`` into ``target`` via tmux bracketed paste.

        This is tmux's own bracketed-paste transport — the same mechanism
        Freebuff's bundled tmux test helper uses (``send-keys`` wrapped in
        ``\\x1b[200~``/``\\x1b[201~`` or ``paste-buffer -d``) — adapted here as
        a file-backed buffer so arbitrary text (multiline, Unicode, shell
        metacharacters) is delivered literally and atomically with no shell
        interpolation and no key-name reinterpretation.
        """
        fd, tmp = tempfile.mkstemp(prefix="hfw-paste-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            r = self._run(["load-buffer", "-b", "hfw-paste", tmp])
            if r.returncode != 0:
                raise TmuxError(f"tmux load-buffer failed: {r.stderr.strip()[:1000]}")
            r = self._run(["paste-buffer", "-d", "-b", "hfw-paste", "-t", target])
            if r.returncode != 0:
                raise TmuxError(f"tmux paste-buffer failed: {r.stderr.strip()[:1000]}")
            if send_enter:
                self.submit(target)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def send_prompt(self, target: str, text: str) -> None:
        """Clear the composer, paste ``text``, and submit — the full prompt path."""
        self.clear_line(target)
        self.send_text(target, text, send_enter=True)

    def capture_pane(self, target: str, *, escapes: bool = False) -> str:
        argv = ["capture-pane", "-p"]
        if escapes:
            argv.append("-e")
        argv += ["-t", target]
        r = self._run(argv)
        return r.stdout if r.returncode == 0 else ""

    def kill_session(self, session: str) -> None:
        self._run(["kill-session", "-t", session])

    def list_sessions(self) -> list[str]:
        r = self._run(["list-sessions", "-F", "#{session_name}"])
        if r.returncode != 0:
            return []
        return [line for line in r.stdout.splitlines() if line.strip()]

    def pane_is_alive(self, target: str) -> bool:
        return self.has_session(target.split(":")[0])


def default_session_prefix() -> str:
    return "hfw-freebuff"


def session_name(alias_or_repo: str, prefix: str | None = None) -> str:
    """Deterministic, safe tmux session name from an alias/repo key."""
    prefix = prefix or default_session_prefix()
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in alias_or_repo)
    return f"{prefix}-{safe}"[:64].rstrip("-")


def find_stale_sessions(prefix: str | None = None) -> list[str]:
    """List tmux sessions matching our prefix (used by doctor)."""
    prefix = prefix or default_session_prefix()
    tmux = Tmux()
    return [s for s in tmux.list_sessions() if s.startswith(prefix)]


def capture_dir() -> Path:
    cfg = config.load_config()
    rel = cfg.get("freebuff", "capture_dir") or "freebuff"
    p = Path(rel)
    if not p.is_absolute():
        p = config.runtime_dir() / p
    return p
