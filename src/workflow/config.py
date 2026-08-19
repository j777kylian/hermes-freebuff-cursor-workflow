"""Configuration loading and resolution.

Live configuration lives outside Git at
``~/.config/hermes-freebuff-workflow/config.toml``. The committed
``config/workflow.example.toml`` documents every field. Only non-secret
values are allowed; credentials stay in the existing stores (Hermes,
Freebuff, Cursor).
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    """Return the per-user config directory for this workflow."""
    override = os.environ.get("HERMES_FREEBUFF_WORKFLOW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "hermes-freebuff-workflow"


def config_path() -> Path:
    return config_dir() / "config.toml"


def projects_path() -> Path:
    return config_dir() / "projects.json"


def runtime_dir() -> Path:
    return config_dir() / "runtime"


def registry_path() -> Path:
    return runtime_dir() / "tasks.json"


def logs_dir() -> Path:
    return runtime_dir() / "logs"


# Defaults mirror config/workflow.example.toml. Every key read through
# ``Config.int``/``Config.get`` must have an explicit default here so a
# missing/None value is never silently coerced at a call site.
DEFAULTS: dict[str, Any] = {
    "workflow": {
        "max_repair_iterations": 3,
        "task_timeout_seconds": 3600,
        "poll_interval_seconds": 5,
        "track_history_in_git": False,
    },
    "freebuff": {
        "command": "freebuff",
        "session_prefix": "hfw-freebuff",
        "capture_dir": "freebuff",
        # Desired supervisor model: a stable internal id (e.g.
        # "deepseek/deepseek-v4-flash") or a display name (e.g.
        # "DeepSeek V4 Flash"). Empty means "use Freebuff's own default" and
        # skip controlled model selection.
        "model": "",
        # How long to wait for the tmux session to be created.
        "freebuff_start_timeout_seconds": 120,
        # How long to wait for the TUI to reach a classifiable screen.
        "freebuff_ready_timeout_seconds": 60,
        # How long to wait for the model picker selection to complete.
        "model_select_timeout_seconds": 30,
        # How long to wait for the durable supervisor acknowledgement after
        # the supervisor prompt is submitted.
        "ack_timeout_seconds": 300,
        # Screen poll interval for startup classification.
        "screen_poll_interval_seconds": 1,
        # Bounded number of prompt delivery attempts.
        "prompt_delivery_retries": 3,
    },
    "cursor": {
        "command": "agent",
        "model": "auto",
        "force": True,
        "trust": True,
        "output_format": "stream-json",
        "timeout_seconds": 1800,
    },
    "hermes": {
        "command": "hermes",
        "discord_target": "discord:#hermes",
    },
}


class ConfigError(RuntimeError):
    """Raised when live configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Resolved, immutable configuration view."""

    raw: dict[str, Any]
    path: Path

    @staticmethod
    def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
        merged: dict[str, Any] = copy.deepcopy(DEFAULTS.get(name, {}))
        merged.update(raw.get(name, {}))
        return merged

    @property
    def workflow(self) -> dict[str, Any]:
        return self._section(self.raw, "workflow")

    @property
    def freebuff(self) -> dict[str, Any]:
        return self._section(self.raw, "freebuff")

    @property
    def cursor(self) -> dict[str, Any]:
        return self._section(self.raw, "cursor")

    @property
    def hermes(self) -> dict[str, Any]:
        return self._section(self.raw, "hermes")

    def get(self, section: str, key: str) -> Any:
        return self._section(self.raw, section).get(key)

    def int(self, section: str, key: str) -> int:
        """Single validated integer boundary.

        Raises :class:`ConfigError` on missing or non-integer values instead of
        letting ``int(None)`` surface deep inside runtime code.
        """
        value = self.get(section, key)
        if value is None:
            raise ConfigError(f"missing integer config [{section}] {key}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"config [{section}] {key} must be an integer, got {value!r}"
            ) from exc

    def bool(self, section: str, key: str) -> bool:
        value = self.get(section, key)
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None) -> Config:
    """Load live config, merging over defaults. Missing file => defaults."""
    target = path or config_path()
    raw: dict[str, Any] = {}
    if target.exists():
        with target.open("rb") as fh:
            parsed = tomllib.load(fh)
            if isinstance(parsed, dict):
                raw = parsed
    return Config(raw=raw, path=target)


def ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "runtime").mkdir(parents=True, exist_ok=True)
    (d / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
    return d
