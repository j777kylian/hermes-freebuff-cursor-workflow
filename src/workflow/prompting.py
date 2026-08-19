"""Prompt template rendering.

Templates live in the versioned ``prompts/`` directory and use ``{{KEY}}``
placeholders. Rendering is a plain string substitution — no shell, no eval.
"""

from __future__ import annotations

import os
from pathlib import Path


class PromptError(RuntimeError):
    pass


def prompts_dir() -> Path:
    candidates = [
        os.environ.get("HERMES_FREEBUFF_WORKFLOW_PROMPTS"),
        Path(__file__).resolve().parent.parent.parent / "prompts",
        Path.cwd() / "prompts",
    ]
    for cand in candidates:
        if cand:
            p = Path(cand)
            if p.is_dir():
                return p
    raise PromptError("cannot locate prompts/ directory")


def render(name: str, **kwargs: str) -> str:
    """Load ``name`` and substitute ``{{KEY}}`` placeholders from kwargs."""
    path = prompts_dir() / name
    if not path.exists():
        raise PromptError(f"prompt template not found: {path}")
    text = path.read_text(encoding="utf-8")
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", str(value))
    # Fail loudly if a placeholder was not supplied.
    if "{{" in text:
        missing = [p.split("}}")[0] for p in text.split("{{")[1:]]
        raise PromptError(f"unrendered placeholders in {name}: {missing}")
    return text
