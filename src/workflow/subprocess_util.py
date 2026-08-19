"""Safe subprocess helpers.

All external commands are executed with explicit argument arrays and
``shell=False``. Untrusted input is never interpolated into a shell string.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO


class CommandError(RuntimeError):
    """Raised when a subprocess fails and ``check=True`` was requested."""


@dataclass
class CommandResult:
    argv: list[str]
    stdout: str
    stderr: str
    returncode: int
    started_at: float
    finished_at: float
    timed_out: bool = False

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run ``argv`` with shell=False and capture output.

    ``argv`` must be a sequence of strings; each element is a single
    argument. No shell parsing is performed.
    """
    args = [str(a) for a in argv]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            timeout=timeout,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            argv=args,
            stdout=(exc.stdout or b"").decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr=(exc.stderr or b"").decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or ""),
            returncode=-1,
            started_at=started,
            finished_at=time.monotonic(),
            timed_out=True,
        )
        if check:
            raise CommandError(
                f"command timed out after {timeout}s: {' '.join(map(shlex.quote, args))}"
            ) from exc
        return result

    result = CommandResult(
        argv=args,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
        started_at=started,
        finished_at=time.monotonic(),
        timed_out=False,
    )
    if check and proc.returncode != 0:
        raise CommandError(
            f"command exited {proc.returncode}: {' '.join(map(shlex.quote, args))}\n"
            f"stderr: {result.stderr.strip()[:2000]}"
        )
    return result


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    log_path: str | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn ``argv`` detached from the parent (new session) for background work.

    Output is redirected to ``log_path`` (or /dev/null). The caller must not
    block on a long-running coding task.
    """
    args = [str(a) for a in argv]
    stdout: int | IO[bytes] = subprocess.DEVNULL
    stderr: int | IO[bytes] = subprocess.DEVNULL
    if log_path:
        fh = open(log_path, "ab")  # noqa: SIM115 - owned by the child process
        stdout = fh
        stderr = fh
    return subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )


def quote_argv(argv: Sequence[str]) -> str:
    """Render an argv for human-readable diagnostics (never re-executed)."""
    return " ".join(shlex.quote(str(a)) for a in argv)
