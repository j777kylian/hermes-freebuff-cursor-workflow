from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "integrations" / "hermes-plugin"


def _load_commands():
    spec = importlib.util.spec_from_file_location("wf_plugin_commands", PLUGIN_DIR / "commands.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wf_plugin_commands"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def get_config(self, key, default=""):
        return default


@pytest.fixture
def commands(monkeypatch):
    mod = _load_commands()
    monkeypatch.setattr(mod, "_workflow_bin", lambda ctx: "/fake/workflow")
    # Never touch the real runtime log directory during tests.
    monkeypatch.setattr(mod, "_log_precommand_diagnostic", lambda record: None)
    monkeypatch.setattr(mod, "_log_origin_warning", lambda command: None)
    return mod


def _capture_spawn(commands, monkeypatch):
    captured = {}

    def fake_spawn(argv):
        captured["argv"] = argv
        return Path("/tmp/x.log")

    monkeypatch.setattr(commands, "_spawn_detached", fake_spawn)
    return captured


# --- session key parsing + reply target construction -------------------------


def test_origin_from_session_key_channel(commands):
    origin = commands._origin_from_session_key(
        "agent:main:discord:group:123456789012345678:987654321098765432"
    )
    assert origin is not None
    assert origin.platform == "discord"
    assert origin.chat_type == "group"
    assert origin.chat_id == "123456789012345678"
    assert origin.thread_id == ""
    assert origin.reply_target == "discord:123456789012345678"


def test_origin_from_session_key_thread(commands):
    origin = commands._origin_from_session_key(
        "agent:main:discord:thread:555555555555555555:555555555555555555"
    )
    assert origin is not None
    assert origin.chat_type == "thread"
    assert origin.chat_id == "555555555555555555"
    assert origin.thread_id == "555555555555555555"
    assert origin.reply_target == "discord:555555555555555555:555555555555555555"


def test_origin_from_session_key_dm(commands):
    origin = commands._origin_from_session_key("agent:main:discord:dm:777777777777777777")
    assert origin is not None
    assert origin.chat_type == "dm"
    assert origin.chat_id == "777777777777777777"
    assert origin.thread_id == ""
    assert origin.reply_target == "discord:777777777777777777"


def test_origin_from_session_key_rejects_non_discord(commands):
    assert commands._origin_from_session_key("agent:main:telegram:group:123:456") is None


def test_origin_from_session_key_rejects_malformed(commands):
    assert commands._origin_from_session_key("") is None
    assert commands._origin_from_session_key("agent:main:discord") is None


# --- pre_command hook: ContextVar + redaction --------------------------------


def test_on_pre_command_sets_contextvar(commands):
    commands._on_pre_command(
        surface="gateway",
        platform="discord",
        command="devtask",
        args_raw="demo Fix the parser",
        session_key="agent:main:discord:group:123456789012345678:987654321098765432",
    )
    origin = commands._ORIGIN_VAR.get()
    assert origin is not None
    assert origin.reply_target == "discord:123456789012345678"


def test_on_pre_command_clears_contextvar_on_malformed(commands):
    # Seed a value, then a malformed Discord key must clear it to None.
    commands._on_pre_command(
        surface="gateway",
        platform="discord",
        command="devtask",
        args_raw="x",
        session_key="agent:main:discord:group:1:2",
    )
    assert commands._ORIGIN_VAR.get() is not None
    commands._on_pre_command(
        surface="gateway",
        platform="discord",
        command="devtask",
        args_raw="x",
        session_key="agent:main:discord",
    )
    assert commands._ORIGIN_VAR.get() is None


def test_on_pre_command_ignores_other_surfaces_platforms_commands(commands):
    # CLI surface — no origin.
    commands._on_pre_command(surface="cli", platform=None, command="devtask", args_raw="x")
    assert commands._ORIGIN_VAR.get() is None
    # Non-Discord platform — no origin.
    commands._on_pre_command(
        surface="gateway",
        platform="telegram",
        command="devtask",
        args_raw="x",
        session_key="agent:main:telegram:dm:1",
    )
    assert commands._ORIGIN_VAR.get() is None
    # A command this plugin does not own — no origin.
    commands._on_pre_command(
        surface="gateway",
        platform="discord",
        command="model",
        args_raw="",
        session_key="agent:main:discord:group:1:2",
    )
    assert commands._ORIGIN_VAR.get() is None


def test_on_pre_command_record_never_contains_user_id(commands, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        commands, "_log_precommand_diagnostic", lambda record: captured.update(record)
    )
    commands._on_pre_command(
        surface="gateway",
        platform="discord",
        command="devtask",
        args_raw="demo Fix the parser",
        session_key="agent:main:discord:group:123456789012345678:987654321098765432",
    )
    # user_id is the trailing slot of a group key; it must never be surfaced.
    assert "987654321098765432" not in captured.values()
    assert captured["reply_target"] == "discord:123456789012345678"


# --- handler consumes the ContextVar -----------------------------------------


def test_devtask_parses_and_backgrounds(commands, monkeypatch):
    captured = _capture_spawn(commands, monkeypatch)
    out = commands._handle_devtask(_FakeCtx(), "demo Fix the parser")
    assert "demo" in out
    assert captured["argv"] == ["/fake/workflow", "devtask", "demo", "Fix the parser"]


def test_devtask_appends_origin_flags(commands, monkeypatch):
    captured = _capture_spawn(commands, monkeypatch)
    commands._ORIGIN_VAR.set(
        commands.CommandOrigin(
            platform="discord",
            chat_type="thread",
            chat_id="555555555555555555",
            thread_id="555555555555555555",
            reply_target="discord:555555555555555555:555555555555555555",
        )
    )
    commands._handle_devtask(_FakeCtx(), "demo Fix the parser")
    assert captured["argv"] == [
        "/fake/workflow",
        "devtask",
        "demo",
        "Fix the parser",
        "--origin-platform",
        "discord",
        "--origin-chat-type",
        "thread",
        "--origin-chat-id",
        "555555555555555555",
        "--origin-thread-id",
        "555555555555555555",
        "--reply-target",
        "discord:555555555555555555:555555555555555555",
    ]


def test_devtask_without_origin_has_no_flags(commands, monkeypatch):
    captured = _capture_spawn(commands, monkeypatch)
    assert commands._ORIGIN_VAR.get() is None
    commands._handle_devtask(_FakeCtx(), "demo Fix the parser")
    assert "--origin-platform" not in captured["argv"]


def test_devtask_argv_is_list_not_shell_string(commands, monkeypatch):
    captured = _capture_spawn(commands, monkeypatch)
    commands._handle_devtask(_FakeCtx(), "demo Fix the parser")
    # shell=False safety: the detached launch receives an argument array, never
    # an interpolated shell string.
    assert isinstance(captured["argv"], list)
    assert all(isinstance(a, str) for a in captured["argv"])


def test_devtask_requires_task(commands):
    out = commands._handle_devtask(_FakeCtx(), "demo")
    assert "Usage" in out


def test_devtask_background_failure(commands, monkeypatch):
    def boom(argv):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(commands, "_spawn_detached", boom)
    out = commands._handle_devtask(_FakeCtx(), "demo do stuff")
    assert "❌" in out
    assert "spawn exploded" in out


def test_devdecide_parses(commands, monkeypatch):
    captured = {}

    def fake_run(argv, timeout=60):
        captured["argv"] = argv
        return 0, "resumed"

    monkeypatch.setattr(commands, "_run_sync", fake_run)
    out = commands._handle_devdecide(_FakeCtx(), "t1 use option B")
    assert captured["argv"] == ["/fake/workflow", "devdecide", "t1", "use option B"]
    assert "t1" in out


def test_devstatus_requires_key(commands):
    assert "Usage" in commands._handle_devstatus(_FakeCtx(), "")


def test_devstop(commands, monkeypatch):
    captured = {}

    def fake_run(argv, timeout=60):
        captured["argv"] = argv
        return 0, "task t1 -> STOPPED"

    monkeypatch.setattr(commands, "_run_sync", fake_run)
    out = commands._handle_devstop(_FakeCtx(), "demo")
    assert captured["argv"] == ["/fake/workflow", "stop", "demo"]
    assert "STOPPED" in out


# --- /devwhere diagnostic ----------------------------------------------------


def test_devwhere_shows_captured_origin(commands):
    commands._ORIGIN_VAR.set(
        commands.CommandOrigin(
            platform="discord",
            chat_type="thread",
            chat_id="5",
            thread_id="5",
            reply_target="discord:5:5",
        )
    )
    out = commands._handle_devwhere(_FakeCtx(), "")
    assert "chat_id: 5" in out
    assert "thread_id: 5" in out
    assert "reply_target: discord:5:5" in out


def test_devwhere_no_origin(commands):
    assert commands._ORIGIN_VAR.get() is None
    out = commands._handle_devwhere(_FakeCtx(), "")
    assert "no origin captured" in out


# --- concurrency: two asyncio tasks must not cross-route ---------------------


def test_concurrent_contexts_do_not_cross_route(commands):
    async def simulate(session_key: str) -> str | None:
        commands._on_pre_command(
            surface="gateway",
            platform="discord",
            command="devtask",
            args_raw="x",
            session_key=session_key,
        )
        await asyncio.sleep(0)  # yield to interleave the two tasks
        origin = commands._ORIGIN_VAR.get()
        return origin.reply_target if origin else None

    async def main():
        return await asyncio.gather(
            simulate("agent:main:discord:group:111:u1"),
            simulate("agent:main:discord:thread:222:222"),
        )

    a, b = asyncio.run(main())
    assert a == "discord:111"
    assert b == "discord:222:222"
