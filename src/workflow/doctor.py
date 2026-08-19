"""Comprehensive host/workflow diagnostics (``workflow doctor``).

Reports PASS/WARN/FAIL for every required dependency and surface. Never prints
credentials. The Cursor headless Auto smoke test is bounded and can be skipped
with ``--no-smoke``.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, cursor, freebuff, hermes, projects, state, tmux
from .subprocess_util import run

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return Check(
        "Python >= 3.11",
        PASS if ok else FAIL,
        f"{platform.python_version()} @ {sys.executable}",
    )


def _check_tool(name: str, argv: list[str], timeout: float = 30) -> tuple[str, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return FAIL, f"{argv[0]} not found on PATH"
    r = run([exe, *argv[1:]], timeout=timeout)
    if r.returncode != 0:
        return FAIL, f"{exe} exited {r.returncode}"
    detail = (r.stdout or r.stderr).strip().splitlines()
    return PASS, (detail[-1] if detail else exe)


def _check_git() -> Check:
    status, detail = _check_tool("Git", ["git", "--version"])
    return Check("Git", status, detail)


def _check_tmux() -> Check:
    status, detail = _check_tool("tmux", ["tmux", "-V"])
    return Check("tmux", status, detail)


def _check_freebuff_installed(cfg: config.Config) -> Check:
    status, detail = _check_tool("Freebuff", [str(cfg.get("freebuff", "command")), "--version"])
    return Check("Freebuff installed", status, detail)


def _check_freebuff_auth() -> Check:
    ok, detail = freebuff.check_authenticated()
    return Check(
        "Freebuff auth/config", PASS if ok else WARN, detail or "no credential store found"
    )


def _check_freebuff_model() -> Check:
    """The model Freebuff itself has globally selected (its own settings).

    This is NOT an error when it differs from the workflow target: the bridge
    is expected to enforce the workflow target at startup.
    """
    model = freebuff.selected_model()
    return Check(
        "Freebuff global selected model",
        PASS if model else WARN,
        model or "unable to read safely",
    )


def _check_workflow_freebuff_target(cfg: config.Config) -> Check:
    """The model the workflow is configured to select for every task.

    The bridge enforces this target at startup and never silently falls back
    to the globally selected model when they differ.
    """
    target = str(cfg.get("freebuff", "model") or "").strip()
    return Check(
        "Workflow Freebuff target",
        PASS if target else WARN,
        target or "(unset - use Freebuff's own default)",
    )


def _check_cursor_installed(cfg: config.Config) -> Check:
    status, detail = _check_tool("Cursor CLI", [str(cfg.get("cursor", "command")), "--version"])
    return Check("Cursor CLI installed", status, detail)


def _check_cursor_auth(cfg: config.Config) -> Check:
    cmd = str(cfg.get("cursor", "command"))
    ok, account = cursor.check_auth(cmd)
    return Check("Cursor authenticated", PASS if ok else FAIL, account or "not authenticated")


def _check_cursor_auto(cfg: config.Config) -> Check:
    cmd = str(cfg.get("cursor", "command"))
    ok = cursor.auto_available(cmd)
    return Check(
        "Cursor `auto` model",
        PASS if ok else FAIL,
        "auto present" if ok else "auto missing",
    )


def _check_cursor_smoke(cfg: config.Config, enabled: bool) -> Check:
    if not enabled:
        return Check("Cursor headless Auto smoke", WARN, "skipped (--no-smoke)")
    cmd = str(cfg.get("cursor", "command"))
    try:
        result = cursor.smoke_test(cmd)
    except Exception as exc:  # noqa: BLE001
        return Check("Cursor headless Auto smoke", FAIL, str(exc)[:200])
    if result.ok and "OK" in (result.stdout or ""):
        return Check(
            "Cursor headless Auto smoke",
            PASS,
            f"exit {result.returncode} in {result.duration:.1f}s",
        )
    return Check(
        "Cursor headless Auto smoke",
        WARN if result.ok else FAIL,
        f"exit {result.returncode}, timed_out={result.timed_out}",
    )


def _check_hermes_installed(cfg: config.Config) -> Check:
    status, detail = _check_tool("Hermes", [str(cfg.get("hermes", "command")), "--version"])
    return Check("Hermes installed", status, detail)


def _check_hermes_config() -> Check:
    ok = hermes.check_config_available()
    return Check(
        "Hermes config", PASS if ok else FAIL, str(Path.home() / ".hermes" / "config.yaml")
    )


def _check_hermes_discord_target(cfg: config.Config) -> Check:
    target = str(cfg.get("hermes", "discord_target"))
    ok = hermes.target_available(target)
    return Check("Hermes Discord target", PASS if ok else FAIL, target)


def _check_hermes_plugin(cfg: config.Config) -> Check:
    name = "hermes-freebuff-workflow"
    installed = hermes.check_plugin_installed(name)
    if not installed:
        return Check("Hermes plugin", FAIL, f"{name} not installed under ~/.hermes/plugins/")
    enabled = hermes.check_plugin_enabled(name)
    return Check(
        "Hermes plugin",
        PASS if enabled else WARN,
        "installed" + (", enabled" if enabled else ", NOT enabled"),
    )


def _check_workflow_config() -> Check:
    cfg = config.load_config()
    if cfg.path.exists():
        return Check("Workflow config", PASS, str(cfg.path))
    return Check(
        "Workflow config",
        WARN,
        f"using defaults (copy config/workflow.example.toml to {cfg.path})",
    )


def _check_projects() -> Check:
    try:
        entries = projects.Projects().list()
    except projects.ProjectError as exc:
        return Check("Registered projects", FAIL, str(exc))
    if not entries:
        return Check("Registered projects", WARN, "none registered")
    names = ", ".join(p.alias for p in entries)
    missing = [p.alias for p in entries if "[MISSING]" in p.path]
    status = WARN if missing else PASS
    detail = names + ("; missing paths: " + ", ".join(missing) if missing else "")
    return Check("Registered projects", status, detail)


def _check_stale_tmux(cfg: config.Config) -> Check:
    prefix = str(cfg.get("freebuff", "session_prefix"))
    stale = tmux.find_stale_sessions(prefix)
    if not stale:
        return Check("Stale tmux sessions", PASS, "none")
    return Check("Stale tmux sessions", WARN, ", ".join(stale))


def _pid_file_stale(path: Path) -> bool:
    """True when a ``.pid`` file names a dead PID.

    Unreadable/unparseable content is not proven stale and is left alone.
    """
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return not state.pid_alive(pid)


def _check_stale_lock_files() -> Check:
    """Report only locks whose recorded owner PID is dead or was reused.

    A lock file held by a live workflow owner process is active, not stale;
    only orphaned files (dead PID, or a PID reused by an unrelated process)
    are reported.
    """
    lock_dir = config.runtime_dir() / "locks"
    stale: list[str] = []
    if lock_dir.is_dir():
        for p in sorted(lock_dir.glob("*.lock")):
            if state.lock_is_stale(p):
                stale.append(str(p))
    for p in sorted(config.runtime_dir().glob("*.pid")):
        if _pid_file_stale(p):
            stale.append(str(p))
    if not stale:
        return Check("Stale PID/lock files", PASS, "none")
    return Check("Stale PID/lock files", WARN, ", ".join(stale))


def repair_stale_runtime() -> list[str]:
    """Remove proven-stale runtime artifacts (orphaned lock/PID files).

    Read-only ``workflow doctor`` never calls this; only ``workflow doctor
    --repair`` does. A lock is only removed when its recorded owner PID is
    dead or was reused by a different process — never when a live workflow
    owner holds it. Returns the paths removed.
    """
    removed: list[str] = []
    lock_dir = config.runtime_dir() / "locks"
    if lock_dir.is_dir():
        for p in sorted(lock_dir.glob("*.lock")):
            if state.lock_is_stale(p):
                try:
                    p.unlink()
                    removed.append(str(p))
                except OSError:
                    pass
    for p in sorted(config.runtime_dir().glob("*.pid")):
        if _pid_file_stale(p):
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass
    return removed


def run_checks(cfg: config.Config, *, smoke: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks.append(_check_python())
    checks.append(_check_git())
    checks.append(_check_tmux())
    checks.append(_check_freebuff_installed(cfg))
    checks.append(_check_freebuff_auth())
    checks.append(_check_freebuff_model())
    checks.append(_check_workflow_freebuff_target(cfg))
    checks.append(_check_cursor_installed(cfg))
    checks.append(_check_cursor_auth(cfg))
    checks.append(_check_cursor_auto(cfg))
    checks.append(_check_cursor_smoke(cfg, smoke))
    checks.append(_check_hermes_installed(cfg))
    checks.append(_check_hermes_config())
    checks.append(_check_hermes_discord_target(cfg))
    checks.append(_check_hermes_plugin(cfg))
    checks.append(_check_workflow_config())
    checks.append(_check_projects())
    checks.append(_check_stale_tmux(cfg))
    checks.append(_check_stale_lock_files())
    return checks


def render_text(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{c.status:<4}] {c.name:<32} {c.detail}")
    fail = sum(1 for c in checks if c.status == FAIL)
    warn = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    lines.append(f"Summary: {len(checks)} checks, {fail} FAIL, {warn} WARN")
    return "\n".join(lines)


def render_json(checks: list[Check]) -> str:
    return json.dumps([c.to_dict() for c in checks], indent=2)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
