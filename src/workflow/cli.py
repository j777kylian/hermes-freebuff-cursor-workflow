"""Command-line interface for the workflow bridge."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, config, doctor, projects, runner, state
from .bridge import Bridge, BridgeError
from .freebuff import FreebuffController
from .state import TransitionError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workflow",
        description="Deterministic Discord -> Freebuff -> Cursor workflow bridge",
    )
    p.add_argument("--version", action="version", version=f"workflow {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # project ------------------------------------------------------------
    prj = sub.add_parser("project", help="Manage the project alias registry")
    prj_sub = prj.add_subparsers(dest="project_command", required=True)
    pa = prj_sub.add_parser("add", help="Register an alias -> path")
    pa.add_argument("alias")
    pa.add_argument("path")
    pr = prj_sub.add_parser("remove", help="Remove an alias")
    pr.add_argument("alias")
    prj_sub.add_parser("list", help="List registered aliases")
    pi = prj_sub.add_parser("init", help="Install repo-side context files into a repo")
    pi.add_argument("--repo", required=True)
    pi.add_argument("--force", action="store_true", help="also (re)install missing files")

    # task lifecycle ------------------------------------------------------
    dt = sub.add_parser("devtask", help="Start a task from Discord (/devtask)")
    dt.add_argument("alias")
    # ``task`` is a single free-text token (the plugin passes it as one argv
    # element). ``nargs="*"`` (not REMAINDER) so the trailing ``--origin-*``
    # flags below are parsed as options instead of being swallowed into the
    # task text. Keep ``task`` declared BEFORE the origin options so argparse
    # matches it first and stops at the first option token.
    dt.add_argument("task", nargs="*")
    dt.add_argument("--origin-platform", default="")
    dt.add_argument("--origin-chat-type", default="")
    dt.add_argument("--origin-chat-id", default="")
    dt.add_argument("--origin-thread-id", default="")
    dt.add_argument("--reply-target", default="")

    st = sub.add_parser("start", help="Run the orchestrator for a repo (E2E entry)")
    st.add_argument("--repo", required=True)
    st.add_argument("--task-id", default=None)

    sub.add_parser("status", help="Show task status").add_argument("key")
    sub.add_parser("stop", help="Stop a task (/devstop)").add_argument("key")

    dd = sub.add_parser("devdecide", help="Record a human decision (/devdecide)")
    dd.add_argument("task_id")
    dd.add_argument("decision", nargs=argparse.REMAINDER)

    # runner commands (invoked by Freebuff) --------------------------------
    cr = sub.add_parser("cursor", help="Run the Cursor implementation worker")
    cr_sub = cr.add_subparsers(dest="cursor_command", required=True)
    crun = cr_sub.add_parser("run", help="Run one Cursor pass")
    crun.add_argument("--repo", required=True)
    crun.add_argument("--prompt-file", default=None)
    crun.add_argument("--model", default=None)
    crun.add_argument("--repair", action="store_true")

    ss = sub.add_parser("state", help="Inspect/transition task state")
    ss_sub = ss.add_subparsers(dest="state_command", required=True)
    sset = ss_sub.add_parser("set", help="Transition to a state")
    sset.add_argument("--repo", required=True)
    sset.add_argument("state")
    sset.add_argument("--reason", default="")
    ssack = ss_sub.add_parser(
        "supervisor-started",
        help="Durable Freebuff supervisor startup acknowledgement",
    )
    ssack.add_argument("--repo", required=True)
    ssack.add_argument("--task-id", default="")

    rp = sub.add_parser("report", help="Write final/decision reports")
    rp_sub = rp.add_subparsers(dest="report_command", required=True)
    rf = rp_sub.add_parser("final")
    rf.add_argument("--repo", required=True)
    rf.add_argument("--summary-file", required=True)
    rf.add_argument("--details-file", default="")
    rd = rp_sub.add_parser("decision")
    rd.add_argument("--repo", required=True)
    rd.add_argument("--question", required=True)
    rd.add_argument("--options", default="")

    # freebuff ------------------------------------------------------------
    fb = sub.add_parser("freebuff", help="Freebuff session control")
    fb_sub = fb.add_subparsers(dest="freebuff_command", required=True)
    fb_sub.add_parser("status", help="List running Freebuff tmux sessions")

    # doctor ---------------------------------------------------------------
    doc = sub.add_parser("doctor", help="Run host/workflow diagnostics")
    doc.add_argument("--no-smoke", action="store_true", help="skip the Cursor Auto smoke test")
    doc.add_argument("--json", action="store_true", help="emit JSON")
    doc.add_argument(
        "--repair",
        action="store_true",
        help="remove proven-stale runtime lock/PID files (doctor is read-only by default)",
    )

    return p


def _task_text(argv: Sequence[str]) -> str:
    return " ".join(argv).strip()


def _origin_from_args(args: argparse.Namespace) -> state.TaskOrigin | None:
    """Build a TaskOrigin from parsed ``--origin-*`` flags, or None.

    Returns None when no origin flags were supplied (plain CLI use), so the
    bridge falls back to the configured ``[hermes].discord_target``.
    """
    if not (args.origin_platform or args.origin_chat_id or args.reply_target):
        return None
    return state.TaskOrigin(
        platform=args.origin_platform,
        chat_type=args.origin_chat_type,
        chat_id=args.origin_chat_id,
        thread_id=args.origin_thread_id,
        reply_target=args.reply_target,
    )


def _cmd_project(args: argparse.Namespace) -> int:
    if args.project_command == "add":
        prj = projects.Projects().add(args.alias, args.path)
        print(f"registered {prj.alias} -> {prj.path}")
        return 0
    if args.project_command == "remove":
        projects.Projects().remove(args.alias)
        print(f"removed {args.alias}")
        return 0
    if args.project_command == "list":
        for prj in projects.Projects().list():
            print(f"{prj.alias}\t{prj.path}")
        return 0
    if args.project_command == "init":
        created = projects.install_template(args.repo)
        for c in created:
            print(f"installed {c}")
        print(f"initialized workflow context in {args.repo}")
        return 0
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config.load_config()

    try:
        if args.command == "project":
            return _cmd_project(args)
        if args.command == "devtask":
            text = _task_text(args.task)
            if not text:
                print("error: /devtask requires a task description", file=sys.stderr)
                return 2
            origin = _origin_from_args(args)
            outcome = Bridge(cfg).start_task(args.alias, text, origin=origin)
            print(f"task {outcome['task_id']} finished: {outcome['final_state']}")
            return 0
        if args.command == "start":
            outcome = Bridge(cfg).start_repo(args.repo, task_id=args.task_id)
            print(f"task {outcome['task_id']} finished: {outcome['final_state']}")
            return 0
        if args.command == "status":
            info = Bridge(cfg).status(args.key)
            for k, v in info.items():
                print(f"{k}: {v}")
            return 0
        if args.command == "stop":
            info = Bridge(cfg).stop(args.key)
            print(f"task {info['task_id']} -> {info['state']}")
            return 0
        if args.command == "devdecide":
            decision = _task_text(args.decision)
            if not decision:
                print("error: /devdecide requires decision text", file=sys.stderr)
                return 2
            info = Bridge(cfg).decide(args.task_id, decision)
            print(f"task {info['task_id']} -> {info['state']}")
            return 0
        if args.command == "cursor":
            if args.cursor_command != "run":
                return 2
            cres = runner.cursor_run(
                args.repo,
                prompt_file=args.prompt_file,
                repair=args.repair,
                model=args.model,
            )
            print(
                f"cursor exit={cres.returncode} timed_out={cres.timed_out} "
                f"duration={cres.duration:.1f}s"
            )
            return 0 if cres.ok else 1
        if args.command == "state":
            if args.state_command == "set":
                st = runner.state_set(args.repo, args.state, reason=args.reason)
                print(f"state: {st.state}")
                return 0
            if args.state_command == "supervisor-started":
                st = runner.supervisor_started(args.repo, task_id=args.task_id)
                print(f"supervisor started: {st.state}")
                return 0
            return 2
        if args.command == "report":
            if args.report_command == "final":
                path = runner.report_final(args.repo, args.summary_file, args.details_file)
                print(f"wrote {path}")
                return 0
            if args.report_command == "decision":
                path = runner.report_decision(args.repo, args.question, args.options)
                print(f"wrote {path}")
                return 0
            return 2
        if args.command == "freebuff":
            if args.freebuff_command == "status":
                sessions = FreebuffController().running_sessions()
                for s in sessions:
                    print(s)
                if not sessions:
                    print("(no running freebuff sessions)")
                return 0
            return 2
        if args.command == "doctor":
            if args.repair:
                removed = doctor.repair_stale_runtime()
                if removed:
                    for r in removed:
                        print(f"removed {r}")
                else:
                    print("nothing to repair")
            checks = doctor.run_checks(cfg, smoke=not args.no_smoke)
            if args.json:
                print(doctor.render_json(checks))
            else:
                print(doctor.render_text(checks))
            return doctor.exit_code(checks)
    except (BridgeError, projects.ProjectError, runner.RunnerError, TransitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard for the CLI
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
