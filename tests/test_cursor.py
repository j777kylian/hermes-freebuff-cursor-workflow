from __future__ import annotations

from workflow import cursor
from workflow.subprocess_util import quote_argv, run


def test_build_argv_shape():
    argv = cursor._build_argv(
        prompt="do the thing",
        repo="/tmp/repo",
        model="auto",
        force=True,
        trust=True,
        output_format="stream-json",
        command="agent",
    )
    assert argv[0] == "agent"
    assert "-p" in argv
    assert "--force" in argv
    assert "--trust" in argv
    assert argv[argv.index("--model") + 1] == "auto"
    assert argv[argv.index("--workspace") + 1] == "/tmp/repo"
    assert argv[-1] == "do the thing"


def test_build_argv_omits_flags_when_false():
    argv = cursor._build_argv(
        prompt="x",
        repo="/tmp/repo",
        model="auto",
        force=False,
        trust=False,
        output_format="text",
        command="agent",
    )
    assert "--force" not in argv
    assert "--trust" not in argv


def test_malicious_prompt_is_single_arg():
    evil = "fix; rm -rf / # $(reboot) && echo pwned"
    argv = cursor._build_argv(
        prompt=evil, repo="/tmp/repo", model="auto", force=True, trust=True,
        output_format="text", command="agent",
    )
    assert argv[-1] == evil
    # No shell metacharacter is split into separate argv elements.
    assert ";" not in [a for a in argv if a != evil]
    # quote_argv re-quotes it so a diagnostic render stays a single token.
    quoted = quote_argv(argv)
    assert evil in quoted


def test_run_uses_shell_false_by_default():
    # A benign command with spaces stays a single argv element.
    r = run(["python3", "-c", "import sys; sys.exit(0)"], timeout=30)
    assert r.returncode == 0


def test_run_captures_nonzero():
    r = run(["python3", "-c", "import sys; sys.exit(7)"], timeout=30)
    assert r.returncode == 7
