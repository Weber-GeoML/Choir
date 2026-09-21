"""Tests for `client.tooling`."""

from __future__ import annotations

from pathlib import Path

from client.config import Config, ConfigError, ToolingConfig
from client.tooling import (
    mathlib_search_advisory,
    read_tools_recommendation,
    tooling_advisory,
)


def _write_pins(workspace: Path, content: str) -> None:
    d = workspace / ".choir"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pins.toml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# tooling_advisory: pure decision core
# ---------------------------------------------------------------------------


def test_advisory_fires_only_when_recommended_and_nothing_is_declared() -> None:
    cases = [
        ("recommended", None, True),
        ("recommended", "", True),
        ("recommended", "   ", True),
        ("recommended", "lean-lsp-mcp", False),
        ("recommended", "agent-provided", False),
        ("recommended", "none", False),
        ("off", None, False),
    ]
    for recommendation, declared, advises in cases:
        assert (tooling_advisory(recommendation, declared) is not None) is advises, declared


def test_advisory_text_names_the_ways_out() -> None:
    text = tooling_advisory("recommended", None)
    assert text is not None
    assert "lean-lsp-mcp" in text
    assert "agent-provided" in text


# ---------------------------------------------------------------------------
# read_tools_recommendation
# ---------------------------------------------------------------------------


def test_recommendation_reads_the_declared_value(tmp_path: Path) -> None:
    _write_pins(tmp_path, '[tools]\nmathlib_search = "off"\n')
    assert read_tools_recommendation(tmp_path) == "off"
    _write_pins(tmp_path, '[tools]\nmathlib_search = "recommended"\n')
    assert read_tools_recommendation(tmp_path) == "recommended"


def test_recommendation_defaults_to_recommended_on_anything_unusable(tmp_path: Path) -> None:
    unusable = [
        None,                                        # no pins.toml at all
        '[pins]\nlean4_skills = "v0.2.3"\n',         # no [tools] section
        '[tools]\nmathlib_search = "recomended"\n',  # typo'd value
        "not valid toml [[[",
        'tools = "nope"\n',                          # [tools] is not a table
    ]
    for pins in unusable:
        if pins is not None:
            _write_pins(tmp_path, pins)
        assert read_tools_recommendation(tmp_path) == "recommended", pins


# ---------------------------------------------------------------------------
# mathlib_search_advisory: composition
# ---------------------------------------------------------------------------


def test_composition_advises_when_recommended_and_undeclared(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.tooling.load_config", Config.default)
    assert mathlib_search_advisory(tmp_path) is not None


def test_composition_silent_when_off(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _write_pins(tmp_path, '[tools]\nmathlib_search = "off"\n')
    monkeypatch.setattr("client.tooling.load_config", Config.default)
    assert mathlib_search_advisory(tmp_path) is None


def test_composition_silent_when_declared(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "client.tooling.load_config",
        lambda: Config(
            tooling=ToolingConfig(search="lean-lsp-mcp"),
        ),
    )
    assert mathlib_search_advisory(tmp_path) is None


def test_composition_degrades_on_config_error(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _boom() -> Config:
        raise ConfigError("malformed")

    monkeypatch.setattr("client.tooling.load_config", _boom)
    # Degrades to "no search declared" -> advises (default recommended), never raises.
    assert mathlib_search_advisory(tmp_path) is not None
