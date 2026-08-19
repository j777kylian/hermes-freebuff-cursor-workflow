"""Project alias registry (outside Git) and repository-side template install.

The registry maps a short alias (used in Discord) to a real filesystem path:
``~/.config/hermes-freebuff-workflow/projects.json``. Full paths are never
required in routine Discord messages.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

AGENTS_MARKER_BEGIN = "<!-- hermes-freebuff-workflow: begin -->"
AGENTS_MARKER_END = "<!-- hermes-freebuff-workflow: end -->"


class ProjectError(RuntimeError):
    pass


def validate_alias(alias: str) -> str:
    alias = (alias or "").strip()
    if not ALIAS_RE.match(alias):
        raise ProjectError(
            f"invalid alias {alias!r}: use 1-64 chars of [A-Za-z0-9._-], starting alphanumeric"
        )
    return alias


def validate_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise ProjectError(f"path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ProjectError(f"path is not a directory: {resolved}")
    return resolved


@dataclass
class Project:
    alias: str
    path: str


class Projects:
    def __init__(self, path: Path | None = None):
        self.path = path or config.projects_path()
        self.data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self.data = {str(k): str(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError) as exc:
                raise ProjectError(f"corrupt project registry {self.path}: {exc}") from exc

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def add(self, alias: str, path: str | Path) -> Project:
        alias = validate_alias(alias)
        resolved = validate_path(path)
        if alias in self.data and self.data[alias] != str(resolved):
            raise ProjectError(
                f"alias {alias!r} already registered to {self.data[alias]}"
            )
        self.data[alias] = str(resolved)
        self._save()
        return Project(alias, str(resolved))

    def remove(self, alias: str) -> None:
        alias = validate_alias(alias)
        if alias not in self.data:
            raise ProjectError(f"unknown alias {alias!r}")
        del self.data[alias]
        self._save()

    def get(self, alias: str) -> Project:
        alias = validate_alias(alias)
        if alias not in self.data:
            raise ProjectError(f"unknown alias {alias!r}")
        path = self.data[alias]
        # Validate the path still exists so a stale registry entry fails loudly.
        validate_path(path)
        return Project(alias, path)

    def list(self) -> list[Project]:
        out = []
        for alias in sorted(self.data):
            path = self.data[alias]
            exists = Path(path).expanduser().resolve().is_dir()
            out.append(Project(alias, path))
            if not exists:
                out[-1].path = path + "  [MISSING]"
        return out

    def as_dict(self) -> dict[str, str]:
        return dict(self.data)


def find_template_dir() -> Path:
    """Locate the versioned ``project-template/`` directory."""
    candidates = [
        os.environ.get("HERMES_FREEBUFF_WORKFLOW_TEMPLATE"),
        Path(__file__).resolve().parent.parent.parent / "project-template",
        Path.cwd() / "project-template",
    ]
    for cand in candidates:
        if cand:
            p = Path(cand)
            if (p / ".workflow").is_dir():
                return p
    raise ProjectError(
        "cannot locate project-template/ directory; set HERMES_FREEBUFF_WORKFLOW_TEMPLATE"
    )


def _append_agents_workflow_section(repo: Path) -> None:
    """Point AGENTS.md at AGENTS.workflow.md without overwriting existing content."""
    agents = repo / "AGENTS.md"
    pointer = repo / "AGENTS.workflow.md"
    if not pointer.exists():
        return
    section = (
        f"\n\n{AGENTS_MARKER_BEGIN}\n"
        "## Workflow agent policy\n"
        "This repository is managed by the hermes-freebuff-cursor workflow.\n"
        "Read `AGENTS.workflow.md` together with this file before starting any\n"
        "task. Workflow runtime state lives in `.workflow/` and is authoritative.\n"
        f"{AGENTS_MARKER_END}\n"
    )
    if agents.exists():
        text = agents.read_text(encoding="utf-8")
        if AGENTS_MARKER_BEGIN in text:
            return  # already installed (idempotent)
        with agents.open("a", encoding="utf-8") as fh:
            fh.write(section)
    else:
        agents.write_text(
            "# Project agents\n\n"
            "See `AGENTS.workflow.md` for the workflow-managed policy.\n" + section,
            encoding="utf-8",
        )


def install_template(repo: str | Path) -> list[str]:
    """Install repository-side context files into ``repo`` (never overwriting AGENTS.md)."""
    repo = validate_path(repo)
    template = find_template_dir()
    installed: list[str] = []
    for item in template.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(template)
        dest = repo / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            installed.append(str(rel))
    _append_agents_workflow_section(repo)
    if "AGENTS.workflow.md" in installed:
        installed.append("AGENTS.md (reference appended)")
    return installed


def ensure_context(repo: str | Path) -> list[str]:
    """Install template files if missing; returns files created in this call."""
    repo = validate_path(repo)
    template = find_template_dir()
    created: list[str] = []
    for item in template.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(template)
        dest = repo / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            created.append(str(rel))
    if not (repo / ".workflow").is_dir():
        (repo / ".workflow").mkdir(parents=True, exist_ok=True)
    return created


def read_workflow_file(repo: str | Path, name: str) -> str:
    p = Path(repo) / ".workflow" / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")
