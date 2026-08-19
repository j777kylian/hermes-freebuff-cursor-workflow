"""Persistent tmux-based Freebuff controller.

Freebuff is the primary software-engineering supervisor and runs in its own
interactive TUI. We drive it through tmux with robust bracketed-paste
semantics. The TUI capture/logging here is diagnostic evidence only — the
authoritative workflow state is ``.workflow/state.json`` in the managed repo.

Startup is a bounded state machine of its own:

    launch -> classify screen -> (model picker) -> select model -> ready
        |-> single-instance conflict / login required / process exited -> fail

``FREEBUFF_RUNNING`` is only valid after the supervisor has *durably*
acknowledged the task (see the ``supervisor-started`` handshake); a process
existing or the composer being visible is NOT sufficient.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import config
from .subprocess_util import run
from .tmux import Tmux, session_name


class FreebuffError(RuntimeError):
    pass


class StartupScreen(StrEnum):
    """Classification of the Freebuff TUI screen during startup."""

    MODEL_PICKER = "MODEL_PICKER"
    READY = "READY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SINGLE_INSTANCE_CONFLICT = "SINGLE_INSTANCE_CONFLICT"
    PROCESS_EXITED = "PROCESS_EXITED"
    UNKNOWN = "UNKNOWN"


# Verified against the currently installed Freebuff version (0.0.150).
READY_MARKERS = (
    "Enter a coding task or / for commands",
)

# Freebuff enforces a single instance per machine/account. When another
# instance is already running, the TUI renders a "Take over / Exit" prompt.
CONFLICT_MARKERS = ("Freebuff is already running", "Only one freebuff instance")

LOGIN_MARKERS = (
    "Log in",
    "Login",
    "Sign in",
    "Not logged in",
    "not authenticated",
    "authentication required",
)

# Model picker detection: a picker title and/or the presence of several known
# model display names at once (the ready composer never lists them).
PICKER_TITLE_MARKERS = ("Select a model", "Choose a model", "Pick a model")

# Verified against the installed Freebuff version (0.0.150): the model picker
# is embedded in the startup "waiting room", whose heading is ``Start coding
# for free``. When collapsed it shows a recommended model plus a
# ``\u2193  See all N models`` affordance; when expanded the affordance flips to
# ``\u2191  Show fewer``. Both forms mean MODEL_PICKER.
PICKER_ROOM_MARKERS = ("Start coding for free", "Show fewer")

# Maximum bounded number of Down presses while navigating to the picker's
# "See all N models" toggle (recommended model + a few offer rows + toggle).
PICKER_EXPAND_MAX_STEPS = 8

# Freebuff renders the focused picker toggle row bold (SGR 1); older pickers
# used reverse video (SGR 7) for the focused row.
_FOCUS_SGR_RE = re.compile(r"\x1b\[1(?:[;m])")

# Stable internal model id -> display-name fragment(s) shown in the picker.
# Obtained from the installed Freebuff binary's bundled agent registry.
MODELS: dict[str, tuple[str, ...]] = {
    "deepseek/deepseek-v4-flash": ("DeepSeek V4 Flash",),
    "deepseek/deepseek-v4-pro": ("DeepSeek V4 Pro",),
    "minimax/minimax-m3": ("MiniMax M3",),
    "mimo/mimo-v2.5": ("MiMo 2.5",),
    "openai/gpt-5.6-luna": ("GPT-5.6 Luna",),
    "z-ai/glm-5.2": ("GLM 5.2",),
    "google/gemini-3.1-flash-lite": ("Gemini 3.1 Flash Lite",),
}

# Models that may consume premium sessions. The bridge must never silently
# switch to one of these when the configured model is unavailable.
PREMIUM_MODELS = frozenset({"deepseek/deepseek-v4-pro", "openai/gpt-5.6-luna"})

_ALIASES: dict[str, str] = {}
for _model_id, _names in MODELS.items():
    _ALIASES[_model_id.lower()] = _model_id
    for _name in _names:
        _ALIASES[_name.lower()] = _model_id
_ALIASES.update(
    {
        "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
        "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        "deepseek flash": "deepseek/deepseek-v4-flash",
        "deepseek pro": "deepseek/deepseek-v4-pro",
        "deepseek v4 flash": "deepseek/deepseek-v4-flash",
        "deepseek v4 pro": "deepseek/deepseek-v4-pro",
        "deepseek v4 flash 07/31": "deepseek/deepseek-v4-flash",
        "deepseek v4 pro 08/13": "deepseek/deepseek-v4-pro",
    }
)


def normalize_model(value: str) -> str | None:
    """Resolve a configured model name/id to a stable internal id, or None."""
    value = (value or "").strip()
    if not value:
        return None
    return _ALIASES.get(value.lower())


def classify_screen(text: str) -> StartupScreen:
    """Classify a captured Freebuff TUI screen into a startup state."""
    t = text or ""
    lowered = t.lower()
    for marker in READY_MARKERS:
        if marker in t:
            return StartupScreen.READY
    for marker in CONFLICT_MARKERS:
        if marker in t:
            return StartupScreen.SINGLE_INSTANCE_CONFLICT
    for marker in LOGIN_MARKERS:
        if marker.lower() in lowered:
            return StartupScreen.LOGIN_REQUIRED
    for marker in PICKER_TITLE_MARKERS:
        if marker in t:
            return StartupScreen.MODEL_PICKER
    # The installed Freebuff renders the picker inside a waiting room headed
    # "Start coding for free", and the collapsed/expanded picker shows a
    # "See all N models" / "Show fewer" affordance row.
    for marker in PICKER_ROOM_MARKERS:
        if marker in t:
            return StartupScreen.MODEL_PICKER
    if find_expand_affordance_line(t) is not None:
        return StartupScreen.MODEL_PICKER
    # No explicit title: treat a screen listing several known model names as
    # the picker (the ready composer never does this).
    if _picker_model_matches(t) >= 2:
        return StartupScreen.MODEL_PICKER
    return StartupScreen.UNKNOWN


def _picker_model_matches(text: str) -> int:
    seen: set[str] = set()
    for _model_id, names in MODELS.items():
        for name in names:
            if name in text and name not in seen:
                seen.add(name)
    return len(seen)


def model_display_name(model_id: str) -> str:
    names = MODELS.get(model_id)
    return names[0] if names else model_id


def parse_model_list(text: str) -> list[str]:
    """Return the ordered list of model ids visible on a picker screen."""
    order: list[str] = []
    for line in (text or "").splitlines():
        for model_id, names in MODELS.items():
            if model_id in order:
                continue
            if any(name in line for name in names):
                order.append(model_id)
                break
    return order


def find_expand_affordance_line(text: str) -> int | None:
    """Return the 0-based line of the picker's ``See all N models`` affordance.

    Returns None when the picker is already expanded (or is not a collapsed
    Freebuff picker). Verified against the installed Freebuff version
    (0.0.150), which renders ``\u2193  See all 5 models`` (``See all`` plus a
    ``models`` token) on a dedicated focusable row.
    """
    for idx, line in enumerate((text or "").splitlines()):
        lowered = line.lower()
        if "see all" in lowered and "model" in lowered:
            return idx
    return None


def picker_is_expanded(text: str) -> bool:
    """True when the picker shows its complete model list.

    A picker is expanded when it renders the ``Show fewer`` affordance, or
    when no ``See all N models`` affordance remains and several model display
    names are visible at once.
    """
    lowered = (text or "").lower()
    if "show fewer" in lowered:
        return True
    return find_expand_affordance_line(text) is None and _picker_model_matches(text) >= 2


def picker_is_collapsed(text: str) -> bool:
    """True when the picker is collapsed behind a ``See all N models`` entry."""
    return find_expand_affordance_line(text) is not None


def _line_at(text: str, index: int) -> str | None:
    lines = (text or "").splitlines()
    if 0 <= index < len(lines):
        return lines[index]
    return None


def _line_is_focused(line: str) -> bool:
    """Detect the picker's focused-row styling (bold or legacy reverse video)."""
    return bool(_FOCUS_SGR_RE.search(line) or "\x1b[7m" in line)


def settings_path() -> Path:
    return Path.home() / ".config" / "manicode" / "settings.json"


@dataclass
class FreebuffSession:
    session: str
    pane: str
    repo: str
    command: list[str]

    @property
    def target(self) -> str:
        return self.pane


class FreebuffController:
    def __init__(self, tmux: Tmux | None = None):
        self.tmux = tmux or Tmux()
        self.cfg = config.load_config()

    # --------------------------------------------------------------- launch

    def freebuff_command(self) -> str:
        return str(self.cfg.get("freebuff", "command") or "freebuff")

    def start(self, repo: str, *, session: str | None = None) -> FreebuffSession:
        """Start Freebuff in a detached tmux session bound to ``repo``."""
        command = self.freebuff_command()
        session = session or session_name(
            Path(repo).name, self.cfg.get("freebuff", "session_prefix")
        )
        if self.tmux.has_session(session):
            raise FreebuffError(f"tmux session already exists: {session}")
        argv = [command, "--cwd", str(repo)]
        self.tmux.new_session(session, argv, cwd=str(repo))
        return FreebuffSession(
            session=session, pane=f"{session}:0.0", repo=str(repo), command=argv
        )

    def detect_conflict(self, session: FreebuffSession) -> bool:
        text = self.tmux.capture_pane(session.target)
        return classify_screen(text) is StartupScreen.SINGLE_INSTANCE_CONFLICT

    # --------------------------------------------------------------- model

    def resolve_model_id(self) -> str:
        """Resolve the configured ``[freebuff] model`` to an internal id.

        Returns ``""`` when no model is configured (use Freebuff's own
        default). Raises :class:`FreebuffError` for an unknown value so we
        fail clearly instead of silently selecting a premium model.
        """
        raw = str(self.cfg.get("freebuff", "model") or "").strip()
        if not raw:
            return ""
        model_id = normalize_model(raw)
        if model_id is None:
            known = ", ".join(sorted(MODELS))
            raise FreebuffError(
                f"unknown configured Freebuff model {raw!r}; known ids: {known}"
            )
        return model_id

    def persist_model(self, model_id: str) -> None:
        """Persist ``freebuffModel`` so the picker defaults to ``model_id``.

        Best-effort and non-secret: it only rewrites
        ``~/.config/manicode/settings.json`` (never credentials). Failure is
        tolerated here because the picker is still handled explicitly.
        """
        if not model_id:
            return
        path = settings_path()
        try:
            data: dict = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (json.JSONDecodeError, OSError):
                    data = {}
            if data.get("freebuffModel") == model_id:
                return
            data["freebuffModel"] = model_id
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # noqa: BLE001 - persistence is best-effort
            print(f"[workflow] warning: could not persist Freebuff model: {exc}")

    def _selected_row(self, session: FreebuffSession) -> int | None:
        """Best-effort detection of the highlighted picker row (0-indexed).

        The installed Freebuff (0.0.150) marks the focused model row with a
        ``›`` (U+203A) prefix; older pickers used reverse video (\x1b[7m)
        or ``❯``/``▶`` glyphs.
        """
        text = self.tmux.capture_pane(session.target, escapes=True)
        order = parse_model_list(text)
        for _idx, line in enumerate((text or "").splitlines()):
            # Reverse video (\x1b[7m) or common list-selection glyphs.
            if "›" in line or "\x1b[7m" in line or "❯" in line or "▶" in line:
                for model_id, names in MODELS.items():
                    if any(name in line for name in names):
                        if model_id in order:
                            return order.index(model_id)
        return None

    def select_model(self, session: FreebuffSession, model_id: str) -> None:
        """Select ``model_id`` from the picker without hard-coding indices.

        The visible model list is parsed from the screen and navigation is
        computed relative to the highlighted row. If the target model is not
        visible we fail clearly (and never silently pick a premium model).
        """
        text = self.tmux.capture_pane(session.target)
        order = parse_model_list(text)
        if model_id not in order:
            available = ", ".join(model_display_name(m) for m in order) or "(none visible)"
            raise FreebuffError(
                f"configured Freebuff model {model_id!r} not available in picker; "
                f"visible: {available}"
            )
        target = order.index(model_id)
        selected = self._selected_row(session)
        selected = 0 if selected is None else selected
        steps = target - selected
        if steps > 20 or steps < -20:
            raise FreebuffError(f"picker navigation out of bounds ({steps} steps)")
        key = "Down" if steps >= 0 else "Up"
        for _ in range(abs(steps)):
            self.tmux.send_keys(session.target, key)
            time.sleep(0.05)
        self.tmux.submit(session.target)

    def _expand_picker(
        self, session: FreebuffSession, text: str, *, timeout: float | None = None
    ) -> str:
        """Expand a collapsed model picker via its ``See all N models`` entry.

        Verified against the installed Freebuff version (0.0.150): the
        collapsed picker renders a recommended model plus a focusable
        ``\u2193  See all N models`` row, which the TUI renders **bold** while
        focused. We press Down (bounded) until that row is focused, activate
        it with Enter, then wait for the picker to re-render its complete
        model list. Returns the expanded picker text so the caller can
        enumerate the full model set before declaring anything unavailable.

        Raises :class:`FreebuffError` on timeout or when the picker changes
        unexpectedly (never a silent fallback to another model).
        """
        if timeout is None:
            timeout = float(self.cfg.int("freebuff", "model_select_timeout_seconds"))
        poll = float(self.cfg.int("freebuff", "screen_poll_interval_seconds"))
        toggle_line = find_expand_affordance_line(text)
        if toggle_line is None:
            raise FreebuffError(
                "model picker has no 'See all' expansion affordance; "
                "cannot inspect the complete model list"
            )
        deadline = time.monotonic() + timeout
        activated = False
        for _ in range(PICKER_EXPAND_MAX_STEPS):
            if time.monotonic() > deadline:
                raise FreebuffError(
                    "timed out navigating to the 'See all' picker toggle"
                )
            esc = self.tmux.capture_pane(session.target, escapes=True)
            if classify_screen(esc) is not StartupScreen.MODEL_PICKER:
                raise FreebuffError(
                    "model picker changed unexpectedly while navigating to the "
                    "'See all' toggle"
                )
            toggle = _line_at(esc, toggle_line)
            if toggle is not None and _line_is_focused(toggle):
                self.tmux.submit(session.target)
                activated = True
                break
            self.tmux.send_keys(session.target, "Down")
            time.sleep(poll)
        if not activated:
            raise FreebuffError(
                "could not focus the 'See all' picker toggle after "
                f"{PICKER_EXPAND_MAX_STEPS} steps; the picker layout may have changed"
            )
        # Wait for the picker to update to the expanded model list.
        while time.monotonic() < deadline:
            new_text = self.tmux.capture_pane(session.target)
            if classify_screen(new_text) is not StartupScreen.MODEL_PICKER:
                raise FreebuffError(
                    "model picker changed unexpectedly while expanding"
                )
            if picker_is_expanded(new_text):
                return new_text
            time.sleep(poll)
        raise FreebuffError("timed out waiting for the model picker to expand")

    def _verify_selected_model(
        self, session: FreebuffSession, model_id: str, *, timeout: float | None = None
    ) -> None:
        """Verify the ready screen identifies ``model_id`` as the selected model.

        Runs after the picker closes and before the supervisor prompt is
        submitted. Accepts the model when the captured ready screen shows its
        display name (the status bar in the installed Freebuff renders it once
        the session is active) or when Freebuff's own settings durably record
        the selection. On failure we raise -- never silently continue on a
        different (possibly premium) model.
        """
        if timeout is None:
            timeout = float(self.cfg.int("freebuff", "model_select_timeout_seconds"))
        poll = float(self.cfg.int("freebuff", "screen_poll_interval_seconds"))
        display = model_display_name(model_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.tmux.capture_pane(session.target)
            if display in text:
                return
            if selected_model() == model_id:
                return
            time.sleep(poll)
        raise FreebuffError(
            f"Freebuff ready screen did not identify the selected model "
            f"{model_id!r} (expected display {display!r}; settings "
            f"freebuffModel={selected_model() or '(unset)'!r})"
        )

    # ------------------------------------------------------------ readiness

    def wait_ready(self, session: FreebuffSession, timeout: float | None = None) -> bool:
        """Wait until the TUI is alive and has drawn content.

        Legacy helper; prefer :meth:`wait_for_startup`, which also classifies
        the screen and handles model selection. Raises :class:`FreebuffError`
        on a single-instance conflict.
        """
        if timeout is None:
            timeout = float(self.cfg.int("freebuff", "freebuff_ready_timeout_seconds"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.tmux.has_session(session.session):
                return False
            screen = classify_screen(self.tmux.capture_pane(session.target))
            if screen is StartupScreen.SINGLE_INSTANCE_CONFLICT:
                raise FreebuffError(
                    "another Freebuff instance is already running; close it and retry"
                )
            if screen is StartupScreen.READY:
                return True
            time.sleep(float(self.cfg.int("freebuff", "screen_poll_interval_seconds")))
        return False

    def wait_for_startup(
        self,
        session: FreebuffSession,
        *,
        model_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Drive Freebuff from launch to a genuinely usable READY composer.

        Bounded polling with a timeout. Handles model selection and raises a
        descriptive :class:`FreebuffError` for conflict/login/exit/timeout so
        the bridge can transition to FAILED with useful diagnostics.
        """
        if timeout is None:
            timeout = float(self.cfg.int("freebuff", "freebuff_ready_timeout_seconds"))
        poll = float(self.cfg.int("freebuff", "screen_poll_interval_seconds"))
        model_select_timeout = float(
            self.cfg.int("freebuff", "model_select_timeout_seconds")
        )
        deadline = time.monotonic() + timeout
        last: StartupScreen = StartupScreen.UNKNOWN
        picker_seen_at: float | None = None
        while time.monotonic() < deadline:
            if not self.tmux.has_session(session.session):
                raise FreebuffError("Freebuff process exited during startup")
            text = self.tmux.capture_pane(session.target)
            screen = classify_screen(text)
            last = screen
            if screen is StartupScreen.SINGLE_INSTANCE_CONFLICT:
                raise FreebuffError(
                    "another Freebuff instance is already running; close it and retry"
                )
            if screen is StartupScreen.LOGIN_REQUIRED:
                raise FreebuffError("Freebuff requires login before running a task")
            if screen is StartupScreen.MODEL_PICKER:
                if not model_id:
                    # No configured model: accept Freebuff's own default.
                    self.tmux.submit(session.target)
                    time.sleep(poll)
                    continue
                if picker_seen_at is None:
                    picker_seen_at = time.monotonic()
                visible = parse_model_list(text)
                if model_id in visible:
                    self.select_model(session, model_id)
                    time.sleep(poll)
                    continue
                # The target is not in the *currently visible* rows. It may be
                # hidden behind the collapsed picker's "See all N models"
                # entry, so it must NOT be declared unavailable until the
                # complete expanded list has been inspected.
                if find_expand_affordance_line(text) is not None:
                    expanded = self._expand_picker(session, text)
                    visible = parse_model_list(expanded)
                    if model_id in visible:
                        self.select_model(session, model_id)
                        time.sleep(poll)
                        continue
                    available = (
                        ", ".join(model_display_name(m) for m in visible)
                        or "(none visible)"
                    )
                    raise FreebuffError(
                        f"configured Freebuff model {model_id!r} not available in "
                        f"expanded picker; visible: {available}"
                    )
                # The picker may still be rendering. Wait a bounded time, then
                # fail clearly (never silently fall back to a premium model).
                if time.monotonic() - picker_seen_at > model_select_timeout:
                    available = ", ".join(model_display_name(m) for m in visible)
                    raise FreebuffError(
                        f"configured Freebuff model {model_id!r} not available in "
                        f"picker; visible: {available or '(none visible)'}"
                    )
                time.sleep(poll)
                continue
            picker_seen_at = None
            if screen is StartupScreen.READY:
                if model_id:
                    self._verify_selected_model(session, model_id)
                return
            time.sleep(poll)
        raise FreebuffError(
            f"timed out waiting for Freebuff READY (last screen: {last.value})"
        )

    # ------------------------------------------------------------ delivery

    def inject(self, session: FreebuffSession, text: str) -> None:
        if not self.tmux.has_session(session.session):
            raise FreebuffError(f"session {session.session} is not running")
        self.tmux.send_prompt(session.target, text)

    def inject_file(self, session: FreebuffSession, path: str | Path) -> None:
        text = Path(path).read_text(encoding="utf-8")
        self.inject(session, text)

    def deliver_prompt(
        self, session: FreebuffSession, text: str, *, retries: int | None = None
    ) -> None:
        """Deliver the supervisor prompt with bounded retry.

        Delivery is *not* considered successful merely because text appears in
        the composer; the caller must additionally wait for the durable
        ``supervisor-started`` acknowledgement.
        """
        retries = (
            self.cfg.int("freebuff", "prompt_delivery_retries")
            if retries is None
            else retries
        )
        last_exc: Exception | None = None
        for _attempt in range(1, retries + 1):
            if not self.tmux.has_session(session.session):
                raise FreebuffError(
                    f"Freebuff session {session.session} exited before prompt delivery"
                )
            try:
                self.inject(session, text)
                return
            except FreebuffError as exc:
                last_exc = exc
                time.sleep(1.0)
        raise FreebuffError(f"prompt delivery failed after {retries} attempts: {last_exc}")

    # ------------------------------------------------------------- lifecycle

    def is_alive(self, session: FreebuffSession) -> bool:
        return self.tmux.has_session(session.session)

    def snapshot(self, session: FreebuffSession, log_path: str | Path) -> str:
        """Capture current pane text and append to a diagnostic log."""
        text = self.tmux.capture_pane(session.target)
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n===== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} =====\n"
            )
            fh.write(text)
            fh.write("\n")
        return text

    def stop(self, session: FreebuffSession) -> None:
        if self.tmux.has_session(session.session):
            self.tmux.kill_session(session.session)

    def running_sessions(self, prefix: str | None = None) -> list[str]:
        prefix = prefix or str(self.cfg.get("freebuff", "session_prefix"))
        return [s for s in self.tmux.list_sessions() if s.startswith(prefix)]


def check_installed(command: str | None = None) -> bool:
    command = command or "freebuff"
    r = run([command, "--version"], timeout=30)
    return r.returncode == 0


def check_version(command: str | None = None) -> str:
    command = command or "freebuff"
    r = run([command, "--version"], timeout=30)
    if r.returncode != 0:
        return ""
    return (r.stdout or r.stderr).strip().splitlines()[-1].strip()


def check_authenticated() -> tuple[bool, str]:
    """Check Freebuff has a credential/config store (does not read secrets)."""
    manicode = Path.home() / ".config" / "manicode"
    creds = manicode / "credentials.json"
    settings = manicode / "settings.json"
    if creds.exists():
        model = selected_model()
        return True, model or "credentials present"
    if settings.exists():
        return True, selected_model() or "settings present"
    return False, ""


def selected_model() -> str:
    """Return the selected Freebuff model if readable without touching secrets."""
    path = settings_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("freebuffModel", ""))
    except (OSError, ValueError):
        return ""
