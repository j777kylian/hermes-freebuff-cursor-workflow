from __future__ import annotations

from workflow import hermes


def test_parse_discord_targets():
    raw = """discord:
  discord:hermes  [123]
  discord:alerts  [456]
  discord:j2kylian agents / #hermes / hello / topic 999
"""
    targets = hermes.parse_discord_targets(raw)
    assert "discord:hermes" in targets
    assert "discord:alerts" in targets


def test_normalize_target():
    assert hermes.normalize_target("discord:#hermes") == "discord:hermes"
    assert hermes.normalize_target("discord:hermes") == "discord:hermes"


def test_target_available_normalized(monkeypatch):
    def fake_list(*a, **k):
        return "discord:\n  discord:hermes  [1]\n"

    monkeypatch.setattr(hermes, "list_targets_raw", fake_list)
    assert hermes.target_available("discord:#hermes") is True
    assert hermes.target_available("discord:#nope") is False
