"""Wrapper around the Hermes CLI for direct delivery.

``hermes send`` sends to configured messaging platforms without starting an
LLM/agent loop. It is the only Hermes surface used for routine delivery —
transport only, no engineering reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config
from .subprocess_util import CommandResult, run


class HermesError(RuntimeError):
    pass


def hermes_command() -> str:
    return str(config.load_config().get("hermes", "command") or "hermes")


def send(
    *,
    to: str,
    message: str | None = None,
    file: str | None = None,
    subject: str | None = None,
    command: str | None = None,
) -> CommandResult:
    """Deliver a message via ``hermes send`` (no LLM)."""
    command = command or hermes_command()
    argv = [command, "send", "--to", to]
    if file:
        argv += ["--file", str(file)]
    if subject:
        argv += ["--subject", subject]
    if message is not None:
        argv.append(message)
    elif not file:
        raise HermesError("hermes send requires either message or file")
    return run(argv, timeout=60)


def list_targets_raw(platform: str | None = None, command: str | None = None) -> str:
    command = command or hermes_command()
    argv = [command, "send", "--list"]
    if platform:
        argv.append(platform)
    r = run(argv, timeout=60)
    return r.stdout if r.returncode == 0 else ""


_DISCORD_TOKEN_RE = re.compile(r"^(discord:[^\s\]]+)")


def parse_discord_targets(raw: str) -> list[str]:
    targets: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        m = _DISCORD_TOKEN_RE.match(stripped)
        if m:
            targets.append(m.group(1))
    return targets


def normalize_target(target: str) -> str:
    """Normalize channel-name vs id forms: discord:#ops == discord:ops."""
    target = target.strip()
    if target.startswith("discord:"):
        rest = target[len("discord:") :]
        if rest.startswith("#"):
            rest = rest[1:]
        return f"discord:{rest}"
    return target


def target_available(target: str, command: str | None = None) -> bool:
    """Return True if ``target`` appears in ``hermes send --list`` output."""
    raw = list_targets_raw("discord", command=command)
    if target in raw:
        return True
    norm = normalize_target(target)
    for token in parse_discord_targets(raw):
        if normalize_target(token) == norm:
            return True
    return False


def check_installed(command: str | None = None) -> bool:
    command = command or hermes_command()
    r = run([command, "--version"], timeout=30)
    return r.returncode == 0


def check_config_available() -> bool:
    return (Path.home() / ".hermes" / "config.yaml").exists()


def check_plugin_installed(name: str = "hermes-freebuff-workflow") -> bool:
    return (Path.home() / ".hermes" / "plugins" / name).is_dir()


def check_plugin_enabled(
    name: str = "hermes-freebuff-workflow", command: str | None = None
) -> bool:
    # ``plugins list`` truncates long names, so use ``plugins show`` which
    # prints an unambiguous "Status: enabled|disabled" line.
    command = command or hermes_command()
    r = run([command, "plugins", "show", name], timeout=60)
    out = (r.stdout + "\n" + r.stderr).lower()
    if r.returncode != 0:
        return False
    for line in out.splitlines():
        if "status:" in line:
            return "enabled" in line and "not enabled" not in line
    return False


def doctor(command: str | None = None) -> CommandResult:
    command = command or hermes_command()
    return run([command, "doctor"], timeout=120)
