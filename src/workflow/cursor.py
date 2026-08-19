"""Deterministic wrapper around the official Cursor CLI (``agent``).

Cursor is the implementation worker only. It may inspect/edit/run checks, but
it may not declare PASS or write the final report — Freebuff review is the
gate. This module never interpolates input into a shell: it uses argument
arrays with ``shell=False``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .subprocess_util import run


class CursorError(RuntimeError):
    pass


@dataclass
class CursorResult:
    argv: list[str]
    stdout: str
    stderr: str
    returncode: int
    started_at: float
    finished_at: float
    timed_out: bool = False
    log_path: str = ""

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "log_path": self.log_path,
        }


def cursor_command() -> str:
    return str(config.load_config().get("cursor", "command") or "agent")


def _build_argv(
    *,
    prompt: str,
    repo: str,
    model: str,
    force: bool,
    trust: bool,
    output_format: str,
    command: str,
) -> list[str]:
    argv = [command, "-p"]
    if force:
        argv.append("--force")
    if trust:
        argv.append("--trust")
    argv += [
        "--model",
        model,
        "--output-format",
        output_format,
        "--workspace",
        str(repo),
        prompt,
    ]
    return argv


def run_agent(
    prompt: str,
    repo: str,
    *,
    model: str | None = None,
    force: bool | None = None,
    trust: bool | None = None,
    output_format: str | None = None,
    timeout: float | None = None,
    command: str | None = None,
    log_path: str | None = None,
) -> CursorResult:
    """Run a single bounded Cursor invocation and capture evidence."""
    cfg = config.load_config()
    command = command or cursor_command()
    model = model or str(cfg.get("cursor", "model") or "auto")
    force = cfg.bool("cursor", "force") if force is None else force
    trust = cfg.bool("cursor", "trust") if trust is None else trust
    output_format = output_format or str(cfg.get("cursor", "output_format") or "stream-json")
    timeout = timeout if timeout is not None else float(cfg.int("cursor", "timeout_seconds"))

    argv = _build_argv(
        prompt=prompt,
        repo=repo,
        model=model,
        force=force,
        trust=trust,
        output_format=output_format,
        command=command,
    )
    started = time.monotonic()
    result = run(argv, cwd=str(repo), timeout=timeout)
    finished = time.monotonic()
    cursor_result = CursorResult(
        argv=argv,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        started_at=started,
        finished_at=finished,
        timed_out=result.timed_out,
        log_path=log_path or "",
    )
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(cursor_result.to_dict(), indent=2))
            fh.write("\n")
            fh.write("---- stdout ----\n")
            fh.write(cursor_result.stdout)
            fh.write("\n---- stderr ----\n")
            fh.write(cursor_result.stderr)
            fh.write("\n")
    return cursor_result


def check_installed(command: str | None = None) -> bool:
    command = command or cursor_command()
    r = run([command, "--version"], timeout=30)
    return r.returncode == 0


def check_auth(command: str | None = None) -> tuple[bool, str]:
    """Return (authenticated, account-or-empty). Never returns credentials."""
    command = command or cursor_command()
    r = run([command, "status"], timeout=30)
    if r.returncode != 0:
        return False, ""
    out = (r.stdout + "\n" + r.stderr).strip()
    if "Logged in" in out or "logged in" in out.lower():
        for line in out.splitlines():
            if "@" in line and ("logged in" in line.lower() or "as " in line):
                account = line.split("as", 1)[-1].strip().split()[0].strip()
                return True, account
        return True, ""
    return False, ""


def list_models(command: str | None = None) -> list[str]:
    command = command or cursor_command()
    r = run([command, "models"], timeout=60)
    if r.returncode != 0:
        return []
    models: list[str] = []
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        line = line.strip()
        if not line or line.startswith("Available") or " - " not in line:
            continue
        name = line.split(" - ", 1)[0].strip()
        if name:
            models.append(name)
    return models


def auto_available(command: str | None = None) -> bool:
    return "auto" in list_models(command)


def smoke_test(command: str | None = None, timeout: float = 180) -> CursorResult:
    """Bounded, harmless headless capability test.

    Determines whether the authenticated Cursor account can run
    ``agent -p --model auto`` without a separate paid API-key path.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hfw-cursor-smoke-") as tmp:
        return run_agent(
            "Reply with exactly: OK",
            tmp,
            model="auto",
            force=True,
            trust=True,
            output_format="text",
            timeout=timeout,
            command=command,
        )
