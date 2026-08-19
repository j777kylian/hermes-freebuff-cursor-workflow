from __future__ import annotations

import pytest

from workflow.freebuff import (
    PREMIUM_MODELS,
    FreebuffError,
    StartupScreen,
    classify_screen,
    normalize_model,
    parse_model_list,
)


def test_classify_ready():
    assert (
        classify_screen("Freebuff\n\nEnter a coding task or / for commands\n")
        is StartupScreen.READY
    )


def test_classify_model_picker_by_title():
    assert (
        classify_screen("Select a model\nDeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31")
        is StartupScreen.MODEL_PICKER
    )


def test_classify_model_picker_by_model_names():
    # Two known model display names without an explicit title is the picker.
    assert (
        classify_screen("DeepSeek V4 Pro 08/13\nMiniMax M3\nMiMo 2.5")
        is StartupScreen.MODEL_PICKER
    )


def test_classify_login_required():
    assert classify_screen("Please log in to continue") is StartupScreen.LOGIN_REQUIRED
    assert classify_screen("Not logged in") is StartupScreen.LOGIN_REQUIRED


def test_classify_single_instance_conflict():
    text = "Freebuff is already running\nOnly one freebuff instance is allowed at a time."
    assert classify_screen(text) is StartupScreen.SINGLE_INSTANCE_CONFLICT


def test_classify_unknown_and_empty():
    assert classify_screen("") is StartupScreen.UNKNOWN
    assert classify_screen("loading...") is StartupScreen.UNKNOWN


def test_classify_ready_wins_over_model_name():
    # The ready composer might echo prior text; READY must take precedence.
    text = "Enter a coding task or / for commands\nDeepSeek V4 Flash 07/31"
    assert classify_screen(text) is StartupScreen.READY


def test_normalize_model_ids_and_names():
    assert normalize_model("deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
    assert normalize_model("DeepSeek V4 Flash") == "deepseek/deepseek-v4-flash"
    assert normalize_model("DeepSeek V4 Flash 07/31") == "deepseek/deepseek-v4-flash"
    assert normalize_model("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
    assert normalize_model("DeepSeek V4 Pro 08/13") == "deepseek/deepseek-v4-pro"


def test_normalize_model_unknown_is_none():
    assert normalize_model("totally-unknown-model") is None
    assert normalize_model("") is None


def test_premium_models_known():
    assert "deepseek/deepseek-v4-pro" in PREMIUM_MODELS
    assert "deepseek/deepseek-v4-flash" not in PREMIUM_MODELS


def test_parse_model_list_order():
    screen = "DeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31\nMiniMax M3\nMiMo 2.5"
    order = parse_model_list(screen)
    assert order == [
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "minimax/minimax-m3",
        "mimo/mimo-v2.5",
    ]


def test_select_model_fails_clearly_when_absent(monkeypatch, tmp_path):
    from workflow.freebuff import FreebuffController, FreebuffSession
    from workflow.tmux import Tmux

    class FakeTmux(Tmux):
        def __init__(self):
            super().__init__("tmux")
            self.sent: list[str] = []

        def capture_pane(self, target, *, escapes=False):
            return "DeepSeek V4 Pro 08/13\nMiniMax M3\n"

        def send_keys(self, target, *keys):
            self.sent.extend(keys)

        def submit(self, target):
            self.sent.append("Enter")

    controller = FreebuffController(tmux=FakeTmux())  # type: ignore[arg-type]
    session = FreebuffSession("s", "s:0.0", str(tmp_path), ["freebuff"])
    with pytest.raises(FreebuffError, match="not available"):
        controller.select_model(session, "deepseek/deepseek-v4-flash")


def test_select_model_navigates_data_driven(monkeypatch, tmp_path):
    from workflow.freebuff import FreebuffController, FreebuffSession
    from workflow.tmux import Tmux

    class FakeTmux(Tmux):
        def __init__(self):
            super().__init__("tmux")
            self.sent: list[str] = []

        def capture_pane(self, target, *, escapes=False):
            # No reverse-video marker: default selection is index 0 (Pro).
            return "DeepSeek V4 Pro 08/13\nDeepSeek V4 Flash 07/31\nMiniMax M3\n"

        def send_keys(self, target, *keys):
            self.sent.extend(keys)

        def submit(self, target):
            self.sent.append("Enter")

    controller = FreebuffController(tmux=FakeTmux())  # type: ignore[arg-type]
    session = FreebuffSession("s", "s:0.0", str(tmp_path), ["freebuff"])
    controller.select_model(session, "deepseek/deepseek-v4-flash")
    # Target is index 1, default selection index 0 -> exactly one Down, no more.
    assert controller.tmux.sent == ["Down", "Enter"]  # type: ignore[attr-defined]
