from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the src package is importable when running from the repo root.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect workflow config/runtime to a temp dir for every test."""
    monkeypatch.setenv("HERMES_FREEBUFF_WORKFLOW_HOME", str(tmp_path / "hfw-home"))
    monkeypatch.setenv(
        "HERMES_FREEBUFF_WORKFLOW_TEMPLATE",
        str(ROOT / "project-template"),
    )
    monkeypatch.setenv("HERMES_FREEBUFF_WORKFLOW_PROMPTS", str(ROOT / "prompts"))
    os.makedirs(tmp_path / "hfw-home", exist_ok=True)


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    return repo
