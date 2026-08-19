"""Deterministic slash-command handlers.

Each handler parses arguments, then calls the deterministic ``workflow`` CLI
through subprocess argument arrays (shell=False). Long-running work
(``/devtask``) is spawned detached so the Discord slash-command request gets an
immediate acknowledgement. No handler invokes an LLM or performs repository
reasoning.

Discord origin routing: Hermes fires the ``pre_command`` observer synchronously
in the same asyncio task that later dispatches the matching slash command, and
passes the full deterministic ``session_key``. We parse it into a task-local
:class:`CommandOrigin` stored in a ``contextvars.ContextVar``, so concurrent
messages stay isolated (no process-global "last origin"). ``/devtask`` reads
that origin and serializes it into the detached ``workflow devtask`` invocation
as structured ``--origin-*`` flags.
"""

from __future__ import annotations

import contextvars
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PLUGIN_ID = "hermes-freebuff-workflow"

# Slash commands whose Discord origin the pre_command observer captures.
_DISCORD_DEV_COMMANDS = frozenset(
    {"devtask", "devstatus", "devdecide", "devstop", "devwhere"}
)


@dataclass(frozen=True)
class CommandOrigin:
    """Safe routing origin of the current Discord command (no credentials)."""

    platform: str
    chat_type: str
    chat_id: str
    thread_id: str
    reply_target: str


# Task-local correlation channel between the ``pre_command`` observer and the
# slash-command handler. ``contextvars`` keeps each asyncio task's value
# isolated, so concurrent Discord messages can never cross-route.
_ORIGIN_VAR: contextvars.ContextVar[CommandOrigin | None] = contextvars.ContextVar(
    "hermes-freebuff-workflow-origin", default=None
)


def _workflow_bin(ctx) -> str:
    """Resolve the workflow CLI path (env override > config > PATH)."""
    env = os.environ.get("HERMES_FREEBUFF_WORKFLOW_BIN")
    if env:
        return env
    try:
        configured = ctx.get_config("workflow_bin", default="")
    except Exception:  # noqa: BLE001 - older host APIs
        configured = ""
    if configured:
        return str(configured)
    found = shutil.which("workflow")
    if found:
        return found
    default = Path.home() / ".local" / "bin" / "workflow"
    return str(default)


def _log_dir() -> Path:
    d = Path.home() / ".config" / "hermes-freebuff-workflow" / "runtime" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_sync(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "(timed out)"
    except FileNotFoundError as exc:
        return -1, f"workflow CLI not found: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    text = out or err or "(no output)"
    return proc.returncode, text[:1900]


def _spawn_detached(argv: list[str]) -> Path:
    log = _log_dir() / f"plugin-devtask-{int(time.time())}.log"
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=open(log, "ab"),  # noqa: SIM115 - owned by child
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"failed to launch background task: {exc}") from exc
    return log


def _reply_target_for(chat_type: str, chat_id: str, thread_id: str) -> str:
    """Build the exact ``hermes send --to`` destination for a Discord origin.

    Verified against Hermes v0.20.1 ``tools/send_message_tool`` target parser:
    ``platform:chat_id:thread_id``, where Discord threads carry the same
    snowflake in both ``chat_id`` and ``thread_id``.
    """
    if chat_type == "thread" and thread_id:
        return f"discord:{chat_id}:{thread_id}"
    return f"discord:{chat_id}"


def _origin_from_session_key(session_key: str) -> CommandOrigin | None:
    """Parse a Hermes session key into a :class:`CommandOrigin` (or None).

    Hermes builds session keys as ``<ns>:<platform>:<chat_type>:<chat_id>[:…]``
    where ``<ns>`` is ``agent:<main|profile>`` (always two segments). For
    Discord (verified against v0.20.1 ``gateway.session.build_session_key``):

    - channel: ``agent:main:discord:group:<chat_id>:<user_id>``
    - thread:  ``agent:main:discord:thread:<chat_id>:<thread_id>``
    - dm:      ``agent:main:discord:dm:<chat_id>``

    Returns None when the key is not Discord or is malformed. The
    participant/user_id slot is never returned, so user identities never reach
    task state or logs.
    """
    if not session_key:
        return None
    parts = str(session_key).split(":")
    # namespace occupies parts[0:2]; platform/chat_type/chat_id follow.
    if len(parts) < 5 or parts[2] != "discord":
        return None
    platform, chat_type, chat_id = parts[2], parts[3], parts[4]
    thread_id = ""
    if chat_type == "thread" and len(parts) > 5:
        thread_id = parts[5]
    return CommandOrigin(
        platform=platform,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        reply_target=_reply_target_for(chat_type, chat_id, thread_id),
    )


def _log_precommand_diagnostic(record: dict[str, object]) -> None:
    """Append one safe pre_command origin observation to the runtime log.

    Only routing metadata is logged — never message content, args, user ids,
    tokens, or credentials.
    """
    try:
        log = _log_dir() / "plugin-precommand.log"
        line = (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"command={record.get('command')!r} "
            f"platform={record.get('platform')!r} "
            f"args_present={record.get('args_present')!r} "
            f"session_key_parseable={record.get('session_key_parseable', True)!r} "
            f"chat_type={record.get('chat_type')!r} "
            f"chat_id={record.get('chat_id')!r} "
            f"thread_id={record.get('thread_id')!r} "
            f"reply_target={record.get('reply_target')!r}\\n"
        )
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001, S110 - logging must never fail dispatch
        pass


def _log_origin_warning(command: str) -> None:
    """Explicit warning when a Discord command's origin cannot be captured."""
    try:
        log = _log_dir() / "plugin-precommand.log"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} WARN "
                f"discord origin not captured for /{command}: malformed or "
                f"unparseable session_key; falling back to configured "
                f"discord_target\\n"
            )
    except Exception:  # noqa: BLE001, S110 - logging must never fail dispatch
        pass


def _on_pre_command(**kwargs) -> None:
    """``pre_command`` observer: capture the current Discord command origin.

    Hermes fires this synchronously inside the same gateway asyncio task that
    later dispatches the matching slash command (``gateway/run.py`` →
    ``fire_pre_command_hook``), passing the full deterministic ``session_key``.
    We parse only Discord keys for our own commands and set the task-local
    ContextVar. The return value is ignored by Hermes v0.20.1 (observer-only).
    """
    if kwargs.get("surface") != "gateway":
        return
    if kwargs.get("platform") != "discord":
        return
    command = kwargs.get("command")
    if command not in _DISCORD_DEV_COMMANDS:
        return
    args_raw = kwargs.get("args_raw") or ""
    origin = _origin_from_session_key(kwargs.get("session_key") or "")
    _ORIGIN_VAR.set(origin)

    record: dict[str, object] = {
        "command": command,
        "platform": kwargs.get("platform"),
        "args_present": bool(args_raw),
        "session_key_parseable": origin is not None,
    }
    if origin is not None:
        record["chat_type"] = origin.chat_type
        record["chat_id"] = origin.chat_id
        record["thread_id"] = origin.thread_id
        record["reply_target"] = origin.reply_target
    _log_precommand_diagnostic(record)
    if origin is None:
        _log_origin_warning(str(command))


def _handle_devtask(ctx, raw_args: str) -> str:
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return "Usage: /devtask <alias> <task description>"
    parts = raw_args.split(None, 1)
    if len(parts) < 2:
        return "Usage: /devtask <alias> <task description>"
    alias, task = parts[0], parts[1]

    # ``task`` is a single argv element placed before the ``--origin-*`` flags
    # so the CLI's ``nargs="*"`` positional stops cleanly at the first option.
    argv = [_workflow_bin(ctx), "devtask", alias, task]
    origin = _ORIGIN_VAR.get()
    if origin is not None:
        argv += [
            "--origin-platform",
            origin.platform,
            "--origin-chat-type",
            origin.chat_type,
            "--origin-chat-id",
            origin.chat_id,
            "--origin-thread-id",
            origin.thread_id,
            "--reply-target",
            origin.reply_target,
        ]
    try:
        log = _spawn_detached(argv)
    except RuntimeError as exc:
        return f"❌ {exc}"
    return (
        f"✅ /devtask accepted: `{alias}`\\n"
        f"Task queued in the background. Monitor with `/devstatus {alias}`.\\n"
        f"Log: {log}"
    )


def _handle_devstatus(ctx, raw_args: str) -> str:
    key = (raw_args or "").strip()
    if not key:
        return "Usage: /devstatus <alias-or-task-id>"
    code, text = _run_sync([_workflow_bin(ctx), "status", key])
    if code != 0:
        return f"❌ status failed: {text}"
    return text


def _handle_devdecide(ctx, raw_args: str) -> str:
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return "Usage: /devdecide <task-id> <decision text>"
    parts = raw_args.split(None, 1)
    if len(parts) < 2:
        return "Usage: /devdecide <task-id> <decision text>"
    task_id, decision = parts[0], parts[1]
    code, text = _run_sync([_workflow_bin(ctx), "devdecide", task_id, decision])
    if code != 0:
        return f"❌ decision rejected: {text}"
    return f"✅ Decision recorded for `{task_id}` and supervisor resumed.\\n{text}"


def _handle_devstop(ctx, raw_args: str) -> str:
    key = (raw_args or "").strip()
    if not key:
        return "Usage: /devstop <alias-or-task-id>"
    code, text = _run_sync([_workflow_bin(ctx), "stop", key])
    if code != 0:
        return f"❌ stop failed: {text}"
    return f"🛑 {text}"


def _handle_devwhere(ctx, raw_args: str) -> str:
    """Diagnostic: show the origin captured for the current command (no LLM)."""
    origin = _ORIGIN_VAR.get()
    if origin is None:
        return "📍 /devwhere: no origin captured (CLI launch or non-Discord)"
    return (
        "📍 /devwhere (captured origin)\\n"
        f"platform: {origin.platform}\\n"
        f"chat_type: {origin.chat_type}\\n"
        f"chat_id: {origin.chat_id}\\n"
        f"thread_id: {origin.thread_id or '(none)'}\\n"
        f"reply_target: {origin.reply_target}"
    )


def register(ctx) -> None:
    ctx.register_command(
        "devtask",
        lambda raw: _handle_devtask(ctx, raw),
        description=(
            "Start a coding task on a registered project "
            "(Freebuff supervises, Cursor implements)"
        ),
        args_hint="<alias> <task>",
    )
    ctx.register_command(
        "devstatus",
        lambda raw: _handle_devstatus(ctx, raw),
        description="Show the current state of a task",
        args_hint="<alias-or-task-id>",
    )
    ctx.register_command(
        "devdecide",
        lambda raw: _handle_devdecide(ctx, raw),
        description="Record a human decision for a task awaiting one",
        args_hint="<task-id> <decision>",
    )
    ctx.register_command(
        "devstop",
        lambda raw: _handle_devstop(ctx, raw),
        description="Stop a running task",
        args_hint="<alias-or-task-id>",
    )
    ctx.register_command(
        "devwhere",
        lambda raw: _handle_devwhere(ctx, raw),
        description="Show the captured Discord origin routing metadata (diagnostic)",
        args_hint="",
    )
    # Capture the Discord origin for our slash commands. Observer-only in
    # v0.20.1; the ContextVar it sets is read back by /devtask in the same
    # asyncio task.
    try:
        ctx.register_hook("pre_command", _on_pre_command)
    except Exception:  # noqa: BLE001, S110 - older host APIs without register_hook
        pass
