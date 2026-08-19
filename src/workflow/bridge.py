"""Deterministic workflow bridge / orchestrator.

Owns task lifecycle: ingress -> Freebuff supervisor -> Cursor worker ->
review -> repair/decision -> completion reporting. No LLM runs here; every
step is plain Python and validated against the on-disk state machine.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import config, hermes, projects, prompting, reports, state
from .freebuff import FreebuffController, FreebuffError, FreebuffSession
from .registry import Registry
from .state import (
    BLOCKED,
    DECISION_REQUIRED,
    FAILED,
    FREEBUFF_STARTING,
    STOPPED,
    TASK_READY,
    StateStore,
    TaskLock,
    TaskOrigin,
    TaskState,
    utc_now,
)


class BridgeError(RuntimeError):
    pass


def new_task_id(alias: str) -> str:
    return f"{alias}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"


def write_current_task(repo: str, task_text: str, task_id: str) -> Path:
    path = Path(repo) / ".workflow" / "CURRENT_TASK.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Current Task\n\n- Task id: `{task_id}`\n- Started: {utc_now()}\n\n"
        f"{task_text.strip()}\n",
        encoding="utf-8",
    )
    return path


class Bridge:
    def __init__(self, cfg: config.Config | None = None):
        self.cfg = cfg or config.load_config()

    # ------------------------------------------------------------------ task

    def start_task(
        self,
        alias: str,
        task_text: str,
        task_id: str | None = None,
        origin: TaskOrigin | None = None,
    ) -> dict[str, Any]:
        project = projects.Projects().get(alias)
        return self._run(
            repo=project.path,
            alias=alias,
            task_text=task_text,
            task_id=task_id,
            origin=origin,
        )

    def start_repo(self, repo: str, task_id: str | None = None) -> dict[str, Any]:
        repo = str(Path(repo).expanduser().resolve())
        alias = Path(repo).name
        task_file = Path(repo) / ".workflow" / "CURRENT_TASK.md"
        if not task_file.exists():
            raise BridgeError(
                f"{task_file} not found; run `workflow project init --repo {repo}` "
                f"and create a task first"
            )
        return self._run(repo=repo, alias=alias, task_text="", task_id=task_id, from_file=True)

    def _run(
        self,
        repo: str,
        alias: str,
        task_text: str,
        task_id: str | None,
        from_file: bool = False,
        origin: TaskOrigin | None = None,
    ) -> dict[str, Any]:
        repo = str(Path(repo).expanduser().resolve())
        store = StateStore(repo)
        reg = Registry()

        with TaskLock(repo, timeout_seconds=0.5):
            existing = store.load()
            if existing is not None:
                if existing.state in state.TERMINAL_STATES:
                    self._archive(repo, existing)
                    store.path.unlink(missing_ok=True)
                elif existing.state == state.IDLE:
                    store.path.unlink(missing_ok=True)  # fresh template state
                else:
                    raise BridgeError(
                        f"task already in progress (state={existing.state}); "
                        f"resolve or run `workflow stop {alias}` first"
                    )

            projects.ensure_context(repo)
            task_id = task_id or new_task_id(alias)
            if from_file:
                task_text = (Path(repo) / ".workflow" / "CURRENT_TASK.md").read_text(
                    encoding="utf-8"
                )
            else:
                write_current_task(repo, task_text, task_id)

            max_iter = self.cfg.int("workflow", "max_repair_iterations")
            store.transition(
                TASK_READY,
                task_id=task_id,
                alias=alias,
                repo=repo,
                max_repair_iterations=max_iter,
                repair_iterations=0,
                last_error="",
                origin=origin,
            )
            reg.put(task_id, alias=alias, repo=repo, session="", state=TASK_READY)

            supervisor_prompt = prompting.render(
                "freebuff_supervisor.md",
                REPO=repo,
                TASK_ID=task_id,
                MAX_REPAIR_ITERATIONS=str(max_iter),
            )

            controller = FreebuffController()
            prefix = str(self.cfg.get("freebuff", "session_prefix"))
            session_name = self._session_for(alias, task_id, prefix)
            try:
                # Resolve the configured model up-front so a bad value fails
                # cleanly (before launching anything) instead of silently
                # falling back to a premium model.
                model_id = controller.resolve_model_id()
                if controller.tmux.has_session(session_name):
                    controller.tmux.kill_session(session_name)  # stale
                if model_id:
                    controller.persist_model(model_id)
                session = controller.start(repo, session=session_name)
                # The process exists now, but the supervisor has NOT begun
                # executing this task yet. Only FREEBUFF_STARTING is honest.
                store.transition(FREEBUFF_STARTING, session=session.session)
                reg.put(task_id, session=session.session, state=FREEBUFF_STARTING)
                # Classify screens, select the configured model, and reach a
                # genuinely usable READY composer.
                controller.wait_for_startup(session, model_id=model_id or None)
            except FreebuffError as exc:
                store.transition(FAILED, last_error=str(exc))
                reg.put(task_id, state=FAILED)
                if controller.tmux.has_session(session_name):
                    controller.tmux.kill_session(session_name)
                return {"task_id": task_id, "alias": alias, "repo": repo, "final_state": FAILED}

            capture_log = self._capture_log_path(task_id)
            controller.snapshot(session, capture_log)
            try:
                controller.deliver_prompt(session, supervisor_prompt)
            except FreebuffError as exc:
                store.transition(FAILED, last_error=str(exc))
                reg.put(task_id, state=FAILED)
                controller.stop(session)
                return {"task_id": task_id, "alias": alias, "repo": repo, "final_state": FAILED}

            # FREEBUFF_RUNNING is only valid after the supervisor durably
            # acknowledges this task (first workflow action).
            acked = self._wait_ack(store, session, controller, task_id, capture_log)
            if not acked:
                controller.stop(session)
                reg.put(task_id, state=FAILED)
                self._on_terminal(store, session, controller, task_id, alias, reg, capture_log)
                return {"task_id": task_id, "alias": alias, "repo": repo, "final_state": FAILED}

            final = self._monitor(
                store=store,
                session=session,
                controller=controller,
                task_id=task_id,
                alias=alias,
                reg=reg,
                capture_log=capture_log,
            )
            self._on_terminal(store, session, controller, task_id, alias, reg, capture_log)
            return {
                "task_id": task_id,
                "alias": alias,
                "repo": repo,
                "final_state": final,
            }

    def _session_for(self, alias: str, task_id: str, prefix: str) -> str:
        from .tmux import session_name

        return session_name(f"{alias}-{task_id[-8:]}", prefix)

    def _capture_log_path(self, task_id: str) -> str:
        return str(config.logs_dir() / f"freebuff-{task_id}.log")

    def _wait_ack(
        self,
        store: StateStore,
        session: FreebuffSession,
        controller: FreebuffController,
        task_id: str,
        capture_log: str,
    ) -> bool:
        """Wait for the durable supervisor acknowledgement.

        The supervisor prompt requires Freebuff, as its first workflow action,
        to run ``workflow state supervisor-started --task-id <id>``. That
        command validates the task and transitions FREEBUFF_STARTING ->
        FREEBUFF_RUNNING with a timestamp. We wait for that durable on-disk
        evidence (``supervisor_started_at``) — never mere terminal text.
        """
        timeout = self.cfg.int("freebuff", "ack_timeout_seconds")
        poll = max(1, self.cfg.int("workflow", "poll_interval_seconds"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = store.load()
            if st is None:
                return False
            if st.supervisor_started_at:
                return True
            if st.state in state.TERMINAL_STATES:
                return False
            # Freebuff is required alive until the ack arrives.
            if st.state in state.FREEBUFF_REQUIRED_STATES and not controller.is_alive(session):
                try:
                    store.transition(
                        FAILED, last_error="Freebuff tmux session exited unexpectedly."
                    )
                except state.TransitionError:
                    pass
                return False
            time.sleep(poll)
        try:
            store.transition(
                FAILED,
                last_error="supervisor did not acknowledge the task before timeout",
            )
        except state.TransitionError:
            pass
        controller.snapshot(session, capture_log)
        return False

    def _monitor(
        self,
        store: StateStore,
        session: FreebuffSession,
        controller: FreebuffController,
        task_id: str,
        alias: str,
        reg: Registry,
        capture_log: str,
    ) -> str:
        poll = max(1, self.cfg.int("workflow", "poll_interval_seconds"))
        timeout = self.cfg.int("workflow", "task_timeout_seconds")
        deadline = time.monotonic() + timeout
        decision_sent = False

        while True:
            st = store.load()
            if st is None:
                return FAILED
            if st.state in state.TERMINAL_STATES:
                return st.state

            if st.state == DECISION_REQUIRED:
                if not decision_sent:
                    self._send_decision_request(alias, task_id, st)
                    reg.put(task_id, decision_requested_at=utc_now())
                    decision_sent = True
            else:
                # Only fail on a dead session when we are NOT waiting on a
                # human decision (a decision may resume via a fresh session).
                if not controller.is_alive(session):
                    try:
                        store.transition(
                            FAILED,
                            last_error="Freebuff tmux session exited unexpectedly.",
                        )
                    except state.TransitionError:
                        pass
                    return FAILED

            if time.monotonic() >= deadline:
                try:
                    store.transition(BLOCKED, last_error="task timeout exceeded")
                except state.TransitionError:
                    pass
                return "TIMEOUT"

            time.sleep(poll)

    def _send_decision_request(self, alias: str, task_id: str, st: TaskState) -> None:
        target = self._resolve_target(st)
        repo = st.repo or ""
        decision_md = Path(repo) / ".workflow" / "DECISION_REQUIRED.md"
        body = reports.read_md(decision_md) if repo else ""
        msg = reports.summarize_for_discord(
            body or f"Task `{task_id}` on `{alias}` requires a human decision."
        )
        self._safe_send(target, subject=f"[workflow] decision required {task_id}", message=msg)

    def _on_terminal(
        self,
        store: StateStore,
        session: FreebuffSession,
        controller: FreebuffController,
        task_id: str,
        alias: str,
        reg: Registry,
        capture_log: str,
    ) -> None:
        st = store.load() or TaskState(task_id=task_id, alias=alias, repo="")
        try:
            controller.snapshot(session, capture_log)
        except Exception as exc:  # noqa: BLE001 - snapshot is best-effort
            print(f"[workflow] warning: final snapshot failed: {exc}")
        controller.stop(session)
        reg.put(task_id, state=st.state, finished_at=utc_now())
        self._archive(st.repo or "", st, capture_log)
        self._send_terminal_report(st, alias)

    def _send_terminal_report(self, st: TaskState, alias: str) -> None:
        target = self._resolve_target(st)
        final_report = Path(st.repo) / ".workflow" / "FINAL_REPORT.md"
        body = reports.read_md(final_report)
        if body:
            # Prefer the on-disk summary section if present.
            summary = ""
            if "## Summary" in body:
                summary = body.split("## Summary", 1)[1].split("## Details", 1)[0].strip()
            msg = reports.summarize_for_discord(
                summary
                or reports.render_terminal_message(st, extra=body)
            )
        else:
            msg = reports.summarize_for_discord(reports.render_terminal_message(st))
        self._safe_send(target, subject=f"[workflow] {st.state} {st.task_id}", message=msg)

    def _safe_send(self, target: str, *, subject: str, message: str) -> None:
        try:
            hermes.send(to=target, subject=subject, message=message)
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            print(f"[workflow] warning: hermes send failed: {exc}")

    def _resolve_target(self, st: TaskState) -> str:
        """Choose the delivery destination for a task's async reports.

        Precedence:
        1. ``task.origin.reply_target`` — the exact Discord channel/thread the
           task was started from (persisted at task creation).
        2. configured ``[hermes].discord_target`` — fallback for CLI-launched
           tasks, legacy states without origin, and non-Discord workflows.

        A valid task origin is never silently replaced by the configured main
        channel.
        """
        origin = st.origin
        if origin is not None and origin.usable():
            return origin.reply_target
        return str(self.cfg.get("hermes", "discord_target"))

    # ----------------------------------------------------------------- decide

    def decide(self, task_id: str, decision: str) -> dict[str, Any]:
        reg = Registry()
        entry = reg.get(task_id)
        if entry is None:
            raise BridgeError(f"unknown task id {task_id!r}")
        repo = entry["repo"]
        alias = entry.get("alias", "")
        store = StateStore(repo)
        st = store.load()
        if st is None or st.state != DECISION_REQUIRED:
            raise BridgeError(
                f"task {task_id} is not awaiting a decision (state={st.state if st else 'none'})"
            )

        # Durable, project-side decision evidence.
        decision_path = Path(repo) / ".workflow" / "decision.json"
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(
            json.dumps(
                {"task_id": task_id, "decision": decision, "at": utc_now()},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        dreq = Path(repo) / ".workflow" / "DECISION_REQUIRED.md"
        if dreq.exists():
            with dreq.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## Decision ({utc_now()})\n\n{decision.strip()}\n")

        # Resume via the same startup handshake as a fresh task: the resume
        # prompt's first action re-acknowledges the task, moving the state from
        # FREEBUFF_STARTING to FREEBUFF_RUNNING.
        store.transition(FREEBUFF_STARTING, decision={"text": decision, "at": utc_now()})
        reg.put(task_id, state=FREEBUFF_STARTING)

        resume_prompt = prompting.render("freebuff_resume.md", TASK_ID=task_id, REPO=repo)
        session_name = entry.get("session") or ""
        controller = FreebuffController()
        session = FreebuffSession(
            session=session_name, pane=f"{session_name}:0.0", repo=repo, command=[]
        )
        try:
            if session_name and controller.is_alive(session):
                controller.inject(session, resume_prompt)
            else:
                # Fresh supervisor session; the resume prompt is self-contained.
                prefix = str(self.cfg.get("freebuff", "session_prefix"))
                session_name = self._session_for(alias, task_id, prefix)
                model_id = controller.resolve_model_id()
                if model_id:
                    controller.persist_model(model_id)
                if controller.tmux.has_session(session_name):
                    controller.tmux.kill_session(session_name)
                session = controller.start(repo, session=session_name)
                reg.put(task_id, session=session.session)
                controller.wait_for_startup(session, model_id=model_id or None)
                controller.inject(session, resume_prompt)
        except FreebuffError as exc:
            store.transition(FAILED, last_error=str(exc))
            reg.put(task_id, state=FAILED)
            raise BridgeError(str(exc)) from exc
        return {"task_id": task_id, "state": FREEBUFF_STARTING, "decision": decision}

    # ------------------------------------------------------------------ stop

    def stop(self, key: str) -> dict[str, Any]:
        entry = self._resolve_entry(key)
        repo = entry["repo"]
        store = StateStore(repo)
        st = store.load()
        if st is None:
            raise BridgeError(f"no task for {key!r}")
        if st.state in state.TERMINAL_STATES:
            # Already terminal: reconcile a dead-session RUNNING into a
            # coherent terminal state if necessary, but stay idempotent.
            self._reconcile_stale_process(store, st, entry.get("session") or st.session)
            return {"task_id": st.task_id, "state": st.state}
        # A user-requested stop is a normal STOPPED transition, never an error.
        store.transition(STOPPED, reason="stopped by user")
        self._kill_owned_session(entry.get("session") or st.session)
        Registry().put(st.task_id, state=STOPPED)
        return {"task_id": st.task_id, "state": STOPPED}

    def _kill_owned_session(self, session_name: str) -> None:
        """Kill only the workflow-owned Freebuff tmux session.

        Never broad-kills (no ``pkill freebuff``) and never touches sessions
        that do not match our prefix.
        """
        if not session_name:
            return
        prefix = str(self.cfg.get("freebuff", "session_prefix"))
        if not session_name.startswith(prefix):
            return
        controller = FreebuffController()
        if controller.tmux.has_session(session_name):
            controller.tmux.kill_session(session_name)

    # ---------------------------------------------------------------- status

    def _reconcile_stale_process(
        self, store: StateStore, st: TaskState, session_name: str
    ) -> TaskState:
        """Reconcile a state that requires Freebuff against the live tmux session.

        If Freebuff is unexpectedly gone while the state machine still requires
        it, transition to FAILED with a clear ``last_error``.
        """
        if st.state in state.FREEBUFF_REQUIRED_STATES and session_name:
            controller = FreebuffController()
            if not controller.tmux.has_session(session_name):
                store.transition(
                    FAILED, last_error="Freebuff tmux session exited unexpectedly."
                )
                return store.load() or st
        return st

    def status(self, key: str) -> dict[str, Any]:
        entry = self._resolve_entry(key)
        repo = entry["repo"]
        store = StateStore(repo)
        st = store.load()
        if st is None:
            return {"repo": repo, "state": "IDLE", "task_id": None}
        # ``status`` reconciles stale process state before reporting.
        st = self._reconcile_stale_process(store, st, entry.get("session") or st.session)
        return {
            "task_id": st.task_id,
            "alias": st.alias,
            "repo": st.repo,
            "state": st.state,
            "repair_iterations": st.repair_iterations,
            "max_repair_iterations": st.max_repair_iterations,
            "updated_at": st.updated_at,
            "last_error": st.last_error,
            "terminal_reason": st.terminal_reason,
        }

    def _resolve_entry(self, key: str) -> dict[str, Any]:
        """Resolve a key (alias or task id) to a registry entry."""
        reg = Registry()
        entry = reg.get(key)
        if entry is not None:
            return entry
        # Try alias: latest entry for that alias, else the project registry.
        by_alias = reg.find_by_alias(key)
        if by_alias:
            return by_alias[-1]
        project = projects.Projects().get(key)
        return {"alias": key, "repo": project.path, "session": "", "task_id": ""}

    # --------------------------------------------------------------- archive

    def _archive(self, repo: str, st: TaskState, capture_log: str | None = None) -> None:
        if not repo:
            return
        src = Path(repo) / ".workflow"
        dest = config.runtime_dir() / "archive" / (st.task_id or "task")
        if not src.is_dir():
            return
        dest.mkdir(parents=True, exist_ok=True)
        for item in src.glob("*.md"):
            shutil.copy2(item, dest / item.name)
        for item in src.glob("*.json"):
            shutil.copy2(item, dest / item.name)
        if capture_log and Path(capture_log).exists():
            shutil.copy2(capture_log, dest / "freebuff-session.log")
