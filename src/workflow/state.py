"""Deterministic lifecycle state machine.

``.workflow/state.json`` inside each managed repo is the authoritative
runtime state. Agent chat history (Hermes, Freebuff, Cursor) is never
authoritative. Transitions are validated; a lock prevents two tasks from
running concurrently against the same working tree.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .subprocess_util import run

IDLE = "IDLE"
TASK_READY = "TASK_READY"
FREEBUFF_STARTING = "FREEBUFF_STARTING"
FREEBUFF_RUNNING = "FREEBUFF_RUNNING"
CURSOR_RUNNING = "CURSOR_RUNNING"
REVIEWING = "REVIEWING"
REPAIR_REQUIRED = "REPAIR_REQUIRED"
DECISION_REQUIRED = "DECISION_REQUIRED"
PASS = "PASS"
BLOCKED = "BLOCKED"
STOPPED = "STOPPED"
FAILED = "FAILED"

ALL_STATES = (
    IDLE,
    TASK_READY,
    FREEBUFF_STARTING,
    FREEBUFF_RUNNING,
    CURSOR_RUNNING,
    REVIEWING,
    REPAIR_REQUIRED,
    DECISION_REQUIRED,
    PASS,
    BLOCKED,
    STOPPED,
    FAILED,
)

TERMINAL_STATES = frozenset({PASS, BLOCKED, STOPPED, FAILED})

# States in which the Freebuff supervisor process/session must be alive.
# FREEBUFF_RUNNING requires a durable supervisor acknowledgement; a process
# merely existing is NOT sufficient (see the ack handshake in bridge.py).
FREEBUFF_REQUIRED_STATES = frozenset({FREEBUFF_STARTING, FREEBUFF_RUNNING})

# Explicit allowed transitions. STOPPED is additionally reachable from any
# non-terminal state (handled in can_transition).
TRANSITIONS: dict[str, frozenset[str]] = {
    IDLE: frozenset({TASK_READY}),
    TASK_READY: frozenset({FREEBUFF_STARTING, CURSOR_RUNNING, FAILED, STOPPED}),
    FREEBUFF_STARTING: frozenset({FREEBUFF_RUNNING, FAILED, STOPPED}),
    FREEBUFF_RUNNING: frozenset({CURSOR_RUNNING, DECISION_REQUIRED, FAILED, STOPPED}),
    CURSOR_RUNNING: frozenset({REVIEWING, FAILED, STOPPED}),
    REVIEWING: frozenset({REPAIR_REQUIRED, PASS, BLOCKED, FAILED, DECISION_REQUIRED, STOPPED}),
    REPAIR_REQUIRED: frozenset({CURSOR_RUNNING, BLOCKED, FAILED, STOPPED}),
    DECISION_REQUIRED: frozenset({FREEBUFF_STARTING, FREEBUFF_RUNNING, BLOCKED, FAILED, STOPPED}),
    PASS: frozenset(),
    BLOCKED: frozenset(),
    STOPPED: frozenset(),
    FAILED: frozenset(),
}

WORKFLOW_DIRNAME = ".workflow"
STATE_FILENAME = "state.json"


def workflow_dir(repo: str | Path) -> Path:
    return Path(repo) / WORKFLOW_DIRNAME


def state_path(repo: str | Path) -> Path:
    return workflow_dir(repo) / STATE_FILENAME


def can_transition(current: str, target: str) -> bool:
    if target not in ALL_STATES:
        return False
    if target == STOPPED and current not in TERMINAL_STATES:
        return True
    return target in TRANSITIONS.get(current, frozenset())


def validate_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise TransitionError(
            f"illegal state transition {current!r} -> {target!r}"
        )


class TransitionError(RuntimeError):
    pass


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class TaskOrigin:
    """Where a task's asynchronous reports belong (Discord origin routing).

    Captured once at task creation from the Hermes ``pre_command`` session key
    and persisted into ``.workflow/state.json``. Never contains tokens or
    credentials — only safe routing metadata.
    """

    platform: str = ""
    chat_type: str = ""
    chat_id: str = ""
    thread_id: str = ""
    # Exact ``hermes send --to`` destination, e.g. ``discord:123`` or
    # ``discord:123:456`` for threads.
    reply_target: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Any) -> TaskOrigin | None:
        if not isinstance(data, dict):
            return None
        return TaskOrigin(
            platform=str(data.get("platform", "")),
            chat_type=str(data.get("chat_type", "")),
            chat_id=str(data.get("chat_id", "")),
            thread_id=str(data.get("thread_id", "")),
            reply_target=str(data.get("reply_target", "")),
        )

    def usable(self) -> bool:
        """True when this origin names an exact Discord destination."""
        return bool(
            self.platform == "discord"
            and self.reply_target.startswith("discord:")
        )


@dataclass
class TaskState:
    task_id: str = ""
    alias: str = ""
    repo: str = ""
    state: str = IDLE
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    repair_iterations: int = 0
    max_repair_iterations: int = 3
    decision: dict[str, Any] | None = None
    last_error: str = ""
    # Human-readable reason for reaching a terminal state (e.g. "stopped by
    # user"). Distinct from ``last_error``, which is reserved for actual
    # FAILED/error conditions so a normal STOPPED does not look like an error.
    terminal_reason: str = ""
    # Timestamp of the durable supervisor acknowledgement
    # (``workflow state supervisor-started``). Empty until the supervisor has
    # acknowledged this task, i.e. before FREEBUFF_RUNNING becomes valid.
    supervisor_started_at: str = ""
    session: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    # Discord routing origin captured at task creation. Preserved through every
    # transition and archive; authoritative for async reports.
    origin: TaskOrigin | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["origin"] = self.origin.to_dict() if self.origin else None
        return d

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TaskState:
        return TaskState(
            task_id=str(data.get("task_id", "")),
            alias=str(data.get("alias", "")),
            repo=str(data.get("repo", "")),
            state=str(data.get("state", IDLE)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            repair_iterations=int(data.get("repair_iterations", 0)),
            max_repair_iterations=int(data.get("max_repair_iterations", 3)),
            decision=data.get("decision"),
            last_error=str(data.get("last_error", "")),
            terminal_reason=str(data.get("terminal_reason", "")),
            supervisor_started_at=str(data.get("supervisor_started_at", "")),
            session=str(data.get("session", "")),
            history=list(data.get("history", [])),
            origin=TaskOrigin.from_dict(data.get("origin")),
        )


class StateStore:
    """Read/write the authoritative state file with atomic, validated updates."""

    def __init__(self, repo: str | Path):
        self.repo = str(Path(repo).expanduser().resolve())
        self.path = state_path(self.repo)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TaskState | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return TaskState.from_dict(data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TransitionError(f"corrupt state file {self.path}: {exc}") from exc

    def _append_history(self, state: TaskState, previous: str, reason: str) -> None:
        state.history.append(
            {
                "from": previous,
                "to": state.state,
                "at": state.updated_at,
                "reason": reason,
            }
        )
        # Keep history bounded.
        if len(state.history) > 200:
            state.history = state.history[-200:]

    @contextlib.contextmanager
    def _transition_lock(self) -> Iterator[None]:
        """Cross-process lock so concurrent readers/writers cannot race a transition."""
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "w")  # noqa: SIM115 - held for the transition
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def transition(
        self,
        target: str,
        *,
        validate: bool = True,
        reason: str = "",
        **updates: Any,
    ) -> TaskState:
        """Atomically transition to ``target`` (validating) with extra field updates.

        ``reason`` is recorded in the transition history. For terminal states it
        is also persisted to ``terminal_reason`` (STOPPED) or ``last_error``
        (FAILED) when the caller did not supply an explicit value.
        """
        if target not in ALL_STATES:
            raise TransitionError(f"unknown state {target!r}")
        with self._transition_lock():
            current = self.load() or TaskState(task_id="", repo=self.repo, state=IDLE)
            previous = current.state
            if validate:
                validate_transition(previous, target)
            # ``created_at`` must be set exactly once and remain stable for the
            # lifetime of the task. A fresh/template state may carry an empty
            # value; initialize it now and never touch it again.
            if not current.created_at:
                current.created_at = utc_now()
            current.state = target
            current.updated_at = utc_now()
            for key, value in updates.items():
                if not hasattr(current, key):
                    raise TransitionError(f"unknown state field {key!r}")
                setattr(current, key, value)
            # Terminal-reason bookkeeping so a normal STOPPED is not
            # represented only through last_error.
            if target == STOPPED and not current.terminal_reason:
                current.terminal_reason = reason or "stopped by user"
            elif target == FAILED and not current.last_error:
                current.last_error = reason or "failed"
            history_reason = reason or current.last_error or current.terminal_reason
            self._append_history(current, previous, history_reason)
            self.save(current)
            return current

    def set_state(self, target: str, **updates: Any) -> TaskState:
        return self.transition(target, validate=True, **updates)

    def save(self, state: TaskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)


# -------------------------------------------------------------------- locks

# Tolerance (seconds) when comparing a recorded process start time against the
# live process. Sub-second clocks and ``ps`` rounding make exact equality
# unrealistic, while a reused PID starts at a completely different time.
LOCK_START_TOLERANCE = 10.0


def pid_alive(pid: int) -> bool:
    """Return True when ``pid`` refers to a live process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _boot_time() -> float:
    """Approximate system boot time (epoch seconds), for /proc starttime math."""
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return time.time() - time.monotonic()


def process_start_time(pid: int) -> float | None:
    """Return the epoch start time of ``pid``, or None when unavailable.

    Used to detect PID reuse: a live PID whose start time does not match the
    recorded lock owner is a *different* process, so the lock is stale even
    though the PID is alive.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        stat = ""
    if stat:
        try:
            # comm may contain spaces/parens; everything after the last ')' is
            # fields 3..N. starttime is field 22 -> index 19 after comm.
            after_comm = stat[stat.rfind(")") + 1 :].split()
            startticks = int(after_comm[19])
            clk = os.sysconf("SC_CLK_TCK")
            return _boot_time() + startticks / clk
        except (ValueError, IndexError, OSError):
            return None
    # macOS/BSD fallback: `ps -o lstart= -p <pid>` (no /proc on macOS).
    r = run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def lock_is_stale(path: Path) -> bool:
    """Return True when a lock file's recorded owner is no longer live.

    A lock is stale when its PID is dead, or when the PID was reused by a
    different process (detected via the recorded process start time). A lock
    with a live workflow owner PID is active, not stale. Unreadable/empty
    content is treated as *not* stale: we only ever clean locks we can prove
    are orphaned, never a lock a live process may still be writing.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not text:
        return False

    pid: int | None = None
    started: Any = None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            pid = data.get("pid")
            started = data.get("started")
    except ValueError:
        pass
    if pid is None:
        # Legacy plain-PID lock files (pre-ownership-metadata format).
        try:
            pid = int(text)
        except ValueError:
            return False
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not pid_alive(pid):
        return True
    if isinstance(started, (int, float)):
        actual = process_start_time(pid)
        if actual is not None and abs(actual - started) > LOCK_START_TOLERANCE:
            return True  # PID was reused by a different process
    return False


class TaskLock:
    """A cross-process advisory lock keyed to a working tree.

    Uses ``fcntl.flock`` so the lock is released automatically if the holder
    process dies. Non-blocking: raising on contention prevents two tasks from
    running against the same tree.

    The lock file is *removed* when the holder releases it (``release()`` /
    context-manager exit), so a completed task never leaves a stale PID file
    behind. Removal is ownership-guarded: the file is only unlinked while we
    still hold the flock and the path still refers to the inode we locked, so
    a lock that a later process has recreated is never deleted. ``acquire()``
    re-verifies the path inode after locking, which closes the unlink/recreate
    race with a releasing process.
    """

    def __init__(self, repo: str | Path, timeout_seconds: float = 5.0):
        resolved = str(Path(repo).expanduser().resolve())
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        self.key = resolved
        self.lock_dir = config.runtime_dir() / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.lock_dir / f"{digest}.lock"
        self.timeout_seconds = timeout_seconds
        self._fh: Any = None

    @staticmethod
    def _path_is_same_file(path: Path, fh: Any) -> bool:
        """True when ``path`` still refers to the same inode as open file ``fh``."""
        try:
            st_path = path.stat()
            st_fd = os.fstat(fh.fileno())
            return st_path.st_dev == st_fd.st_dev and st_path.st_ino == st_fd.st_ino
        except OSError:
            return False

    def _write_owner(self, fh: Any) -> None:
        """Write our ownership record (PID + process start time) to locked ``fh``."""
        owner = {"pid": os.getpid(), "started": process_start_time(os.getpid()) or time.time()}
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(owner) + "\n")
        fh.flush()

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            # Append mode: a contending process never truncates the current
            # holder's ownership record before failing to lock.
            fh = open(self.path, "a")  # noqa: SIM115 - held for process lifetime
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
                continue
            # Guard the unlink/recreate race: a releasing process may have
            # unlinked this file while we were blocked on the flock, so the
            # path may now name a different inode. Only proceed when the path
            # still refers to the inode we actually locked.
            if not self._path_is_same_file(self.path, fh):
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                fh.close()
                continue
            self._write_owner(fh)
            self._fh = fh
            return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            # Remove the lock file while we still hold the flock, and only if
            # the path still names the inode we locked. If the file was
            # recreated by a later owner, leave it alone.
            if self._path_is_same_file(self.path, self._fh):
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self) -> TaskLock:
        if not self.acquire():
            raise TransitionError(f"another task is already running against {self.key}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def terminal_states() -> Iterable[str]:
    return sorted(TERMINAL_STATES)
