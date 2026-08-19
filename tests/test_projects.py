from __future__ import annotations

from pathlib import Path

import pytest

from workflow.projects import AGENTS_MARKER_BEGIN, ProjectError, Projects, install_template


def test_add_list_remove(tmp_path: Path):
    repo = tmp_path / "proj"
    repo.mkdir()
    reg = Projects()
    reg.add("demo", repo)
    assert reg.get("demo").path == str(repo.resolve())
    assert any(p.alias == "demo" for p in reg.list())
    reg.remove("demo")
    with pytest.raises(ProjectError):
        reg.get("demo")


def test_invalid_alias_rejected(tmp_path: Path):
    reg = Projects()
    for bad in ["", "../etc", "has space", "a/b", "-leading"]:
        with pytest.raises(ProjectError):
            reg.add(bad, tmp_path)


def test_missing_path_rejected(tmp_path: Path):
    reg = Projects()
    with pytest.raises(ProjectError):
        reg.add("demo", tmp_path / "nope")


def test_add_duplicate_alias_different_path(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg = Projects()
    reg.add("demo", a)
    with pytest.raises(ProjectError):
        reg.add("demo", b)
    # Same path re-add is a no-op.
    reg.add("demo", a)


def test_install_template_never_overwrites_agents(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = repo / "AGENTS.md"
    agents.write_text("# My custom agents\n\nKeep this.\n", encoding="utf-8")
    installed = install_template(repo)
    content = agents.read_text(encoding="utf-8")
    assert "Keep this." in content
    assert AGENTS_MARKER_BEGIN in content
    assert (repo / "AGENTS.workflow.md").exists()
    assert (repo / ".workflow" / "state.json").exists()
    assert "AGENTS.md (reference appended)" in installed


def test_install_template_creates_agents_md(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    install_template(repo)
    assert (repo / "AGENTS.md").exists()
    assert (repo / "AGENTS.workflow.md").exists()


def test_install_template_idempotent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    install_template(repo)
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8")
    install_template(repo)
    # Marker must not be duplicated.
    assert agents.count(AGENTS_MARKER_BEGIN) == 1
