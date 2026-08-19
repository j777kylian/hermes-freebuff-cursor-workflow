"""Regression tests for the collapsed Freebuff model-picker repair.

The installed Freebuff (0.0.150) renders its model picker COLLAPSED at
startup: a recommended model plus a focusable ``↓  See all N models`` row.
The full model list is only visible after expanding that row. These tests
verify the driver never declares a configured model "unavailable" until the
complete expanded list has been inspected, and never silently falls back to a
premium model.
"""

from __future__ import annotations

import pytest

from workflow import freebuff as fb
from workflow.freebuff import (
    FreebuffController,
    FreebuffError,
    FreebuffSession,
    StartupScreen,
    classify_screen,
    find_expand_affordance_line,
    model_display_name,
    normalize_model,
    parse_model_list,
    picker_is_collapsed,
    picker_is_expanded,
)
from workflow.tmux import Tmux

FLASH = "deepseek/deepseek-v4-flash"
PRO = "deepseek/deepseek-v4-pro"

# Order shown by the installed Freebuff picker once expanded
# (PREMIUM: Pro, Luna, MiniMax; UNLIMITED: Flash, MiMo).
EXPANDED_MODELS = [
    "DeepSeek V4 Pro 08/13",
    "GPT-5.6 Luna",
    "MiniMax M3",
    "DeepSeek V4 Flash 07/31",
    "MiMo 2.5",
]


def _write_test_config(tmp_path) -> None:
    """Give the controller fast, bounded polling in the isolated home."""
    home = tmp_path / "hfw-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        "[freebuff]\n"
        "screen_poll_interval_seconds = 0\n"
        "model_select_timeout_seconds = 5\n"
        "freebuff_ready_timeout_seconds = 30\n",
        encoding="utf-8",
    )


def _session(tmp_path) -> FreebuffSession:
    return FreebuffSession("s", "s:0.0", str(tmp_path), ["freebuff"])


class FakePickerTmux(Tmux):
    """Simulates the Freebuff 0.0.150 model picker over tmux.

    Mirrors the real TUI's keyboard model: Down/Up move the focus forward /
    backward (wrapping), Enter activates the focused row. The focused model
    row is marked with a ``›`` prefix; the focused ``See all`` toggle row is
    rendered bold (SGR 1) -- exactly what the real renderer does. Activating
    the toggle expands the list; activating a model starts a session and
    shows the ready composer with the selected model's display name.
    """

    def __init__(
        self,
        *,
        expanded_models: list[str] | None = None,
        start: str = "collapsed",
        no_expand: bool = False,
        change_on_down: bool = False,
    ):
        super().__init__("tmux")
        self.state = start
        self.focus = 0
        self.sent: list[str] = []
        self.expanded_models = list(expanded_models or EXPANDED_MODELS)
        self.no_expand = no_expand
        self.change_on_down = change_on_down
        self.selected_display: str | None = None
        self._focusable = {
            "collapsed": ["DeepSeek V4 Pro 08/13", "__toggle__"],
            "expanded": self.expanded_models + ["__toggle__"],
        }

    def has_session(self, session: str) -> bool:
        return True

    def _render(self, escapes: bool) -> str:
        if self.state == "ready":
            selected = self.selected_display or "DeepSeek V4 Pro 08/13"
            return f"Enter a coding task or / for commands\n{selected} · unlimited"
        lines = ["Start coding for free"]
        if self.state == "collapsed":
            rows = self._focusable["collapsed"]
            for idx, row in enumerate(rows):
                if row == "__toggle__":
                    label = "↓  See all 5 models"
                    if escapes and idx == self.focus:
                        label = "\x1b[1m" + label + "\x1b[0m"
                    lines.append(label)
                else:
                    lines.append(("› " if idx == self.focus else "  ") + row)
        else:  # expanded
            lines.append("PREMIUM")
            for idx, row in enumerate(self.expanded_models):
                lines.append(("› " if idx == self.focus else "  ") + row)
                if idx == 2:
                    lines.append("UNLIMITED")
            label = "↑  Show fewer"
            if escapes and self.focus == len(self.expanded_models):
                label = "\x1b[1m" + label + "\x1b[0m"
            lines.append(label)
        # The GLM referral promo mentions "GLM 5.2" (as on the real screen).
        lines.append("Refer friends → GLM 5.2")
        return "\n".join(lines)

    def capture_pane(self, target: str, *, escapes: bool = False) -> str:
        return self._render(escapes)

    def send_keys(self, target: str, *keys: str) -> None:
        self.sent.extend(keys)
        if self.change_on_down and "Down" in keys and self.state != "ready":
            self.state = "ready"
            return
        for key in keys:
            if self.no_expand:
                continue
            if key == "Down":
                self.focus = (self.focus + 1) % len(self._focusable[self.state])
            elif key == "Up":
                self.focus = (self.focus - 1) % len(self._focusable[self.state])

    def submit(self, target: str) -> None:
        self.sent.append("Enter")
        if self.state == "collapsed":
            if self.focus == 1:  # the "See all" toggle row
                self.state = "expanded"
                self.focus = 0
            else:  # accepted the recommended model directly
                self.selected_display = "DeepSeek V4 Pro 08/13"
                self.state = "ready"
        elif self.state == "expanded":
            if self.focus < len(self.expanded_models):
                self.selected_display = self.expanded_models[self.focus]
                self.state = "ready"


# ------------------------------------------------------------------ helpers


def test_find_expand_affordance_line():
    collapsed = "Start coding for free\nDeepSeek V4 Pro 08/13\n↓  See all 5 models"
    assert find_expand_affordance_line(collapsed) == 2
    assert find_expand_affordance_line("DeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31") is None
    assert find_expand_affordance_line("") is None


def test_picker_collapsed_vs_expanded_state():
    collapsed = "DeepSeek V4 Pro 08/13\n↓  See all 5 models"
    expanded = "PREMIUM\nDeepSeek V4 Pro 08/13\nUNLIMITED\nDeepSeek V4 Flash 07/31\n↑  Show fewer"
    assert picker_is_collapsed(collapsed)
    assert not picker_is_expanded(collapsed)
    assert not picker_is_collapsed(expanded)
    assert picker_is_expanded(expanded)
    # Plain full list without any affordance counts as expanded too.
    assert picker_is_expanded("DeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31")


def test_collapsed_picker_parses_only_recommended_plus_promo():
    # Exactly the evidence from the failing E2E: only Pro + GLM are visible
    # on the collapsed screen, even though Flash exists in the full list.
    collapsed = (
        "Start coding for free\n"
        "DeepSeek V4 Pro 08/13\n"
        "↓  See all 5 models\n"
        "Refer friends → GLM 5.2"
    )
    assert parse_model_list(collapsed) == [PRO, "z-ai/glm-5.2"]
    assert classify_screen(collapsed) is StartupScreen.MODEL_PICKER


# ---------------------------------------------------------- normalization


def test_display_value_matches_internal_target():
    # "DeepSeek V4 Flash 07/31" must resolve to the stable internal id
    # regardless of the date/version suffix.
    assert normalize_model("DeepSeek V4 Flash 07/31") == FLASH
    assert normalize_model("deepseek/deepseek-v4-flash") == FLASH
    assert normalize_model("DeepSeek V4 Flash") == FLASH
    assert model_display_name(FLASH) == "DeepSeek V4 Flash"


# ------------------------------------------------- expansion unit behavior


def test_expand_picker_succeeds_and_reveals_flash(tmp_path):
    _write_test_config(tmp_path)
    fake = FakePickerTmux()
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    collapsed = fake.capture_pane("s:0.0")
    expanded = controller._expand_picker(_session(tmp_path), collapsed)
    assert fake.state == "expanded"
    assert find_expand_affordance_line(expanded) is None
    assert picker_is_expanded(expanded)
    assert FLASH in parse_model_list(expanded)
    # Down onto the toggle, then Enter to activate it -- nothing else.
    assert fake.sent == ["Down", "Enter"]


def test_expand_picker_timeout_when_toggle_unreachable(tmp_path):
    _write_test_config(tmp_path)
    fake = FakePickerTmux(no_expand=True)
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    collapsed = fake.capture_pane("s:0.0")
    with pytest.raises(FreebuffError, match="could not focus the 'See all' picker toggle"):
        controller._expand_picker(_session(tmp_path), collapsed)


def test_expand_picker_raises_when_picker_changes_unexpectedly(tmp_path):
    _write_test_config(tmp_path)
    fake = FakePickerTmux(change_on_down=True)
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    collapsed = fake.capture_pane("s:0.0")
    with pytest.raises(FreebuffError, match="changed unexpectedly"):
        controller._expand_picker(_session(tmp_path), collapsed)


# ------------------------------------------------------- startup integration


def test_startup_selects_target_directly_visible(tmp_path):
    # Target visible in the initial (already expanded) picker: no expansion
    # navigation may happen; exactly the 3 Downs + Enter for Pro -> Flash.
    _write_test_config(tmp_path)
    fake = FakePickerTmux(start="expanded")
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    controller.wait_for_startup(_session(tmp_path), model_id=FLASH)
    assert fake.state == "ready"
    assert fake.selected_display == "DeepSeek V4 Flash 07/31"
    assert fake.sent == ["Down", "Down", "Down", "Enter"]


def test_startup_expands_collapsed_picker_then_selects_flash(tmp_path):
    # The E2E scenario: collapsed picker shows only Pro + the "See all 5
    # models" entry; the driver must expand before concluding Flash exists.
    _write_test_config(tmp_path)
    fake = FakePickerTmux()
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    controller.wait_for_startup(_session(tmp_path), model_id=FLASH)
    assert fake.state == "ready"
    assert fake.selected_display == "DeepSeek V4 Flash 07/31"
    # Expansion: Down onto the toggle + Enter. Selection: 3 Downs + Enter.
    assert fake.sent == ["Down", "Enter", "Down", "Down", "Down", "Enter"]


def test_startup_fails_clearly_when_flash_absent_even_after_expansion(tmp_path):
    _write_test_config(tmp_path)
    fake = FakePickerTmux(
        expanded_models=["DeepSeek V4 Pro 08/13", "GPT-5.6 Luna", "MiniMax M3", "MiMo 2.5"]
    )
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    with pytest.raises(FreebuffError, match="not available in expanded picker") as exc:
        controller.wait_for_startup(_session(tmp_path), model_id=FLASH)
    # The full discovered list is reported and no model was selected
    # (no silent fallback to Pro).
    assert "DeepSeek V4 Pro" in str(exc.value)
    assert "MiMo 2.5" in str(exc.value)
    assert fake.state != "ready"
    assert fake.selected_display is None


def test_startup_never_falls_back_to_premium_model(tmp_path, monkeypatch):
    # If the ready screen identifies Pro (a wrong/premium selection), the
    # driver must fail before any prompt is submitted.
    _write_test_config(tmp_path)
    monkeypatch.setattr(fb, "selected_model", lambda: PRO)
    fake = FakePickerTmux(start="ready", no_expand=True)
    fake.selected_display = "DeepSeek V4 Pro 08/13"
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    with pytest.raises(FreebuffError, match="did not identify the selected model"):
        controller.wait_for_startup(_session(tmp_path), model_id=FLASH)


def test_verify_selected_model_accepts_ready_screen(tmp_path):
    _write_test_config(tmp_path)
    fake = FakePickerTmux(start="ready")
    fake.selected_display = "DeepSeek V4 Flash 07/31"
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    controller._verify_selected_model(_session(tmp_path), FLASH)  # must not raise


def test_verify_selected_model_rejects_wrong_model(tmp_path, monkeypatch):
    _write_test_config(tmp_path)
    monkeypatch.setattr(fb, "selected_model", lambda: PRO)
    fake = FakePickerTmux(start="ready")
    fake.selected_display = "DeepSeek V4 Pro 08/13"
    controller = FreebuffController(tmux=fake)  # type: ignore[arg-type]
    with pytest.raises(FreebuffError, match="did not identify the selected model"):
        controller._verify_selected_model(_session(tmp_path), FLASH, timeout=0.01)


# ---------------------------------------------------------------- classifier


def test_classifier_still_distinguishes_all_startup_screens():
    assert (
        classify_screen("Freebuff\n\nEnter a coding task or / for commands\n")
        is StartupScreen.READY
    )
    assert classify_screen("Please log in to continue") is StartupScreen.LOGIN_REQUIRED
    assert (
        classify_screen("Freebuff is already running\nOnly one freebuff instance is allowed")
        is StartupScreen.SINGLE_INSTANCE_CONFLICT
    )
    # New markers: collapsed affordance, expanded affordance, waiting room.
    assert (
        classify_screen("Start coding for free\nDeepSeek V4 Pro 08/13\n↓  See all 5 models")
        is StartupScreen.MODEL_PICKER
    )
    assert (
        classify_screen("↑  Show fewer\nDeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31")
        is StartupScreen.MODEL_PICKER
    )
    assert classify_screen("") is StartupScreen.UNKNOWN
    assert classify_screen("loading...") is StartupScreen.UNKNOWN
