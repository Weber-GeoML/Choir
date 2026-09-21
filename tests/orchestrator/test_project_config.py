"""Tests for `orchestrator.project_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.project_config import (
    MergeAutomation,
    ProjectConfig,
    ProjectConfigError,
    parse_project_config,
    read_project_config,
)


def test_empty_text_defaults_to_auto() -> None:
    cfg = parse_project_config("")
    assert cfg == ProjectConfig()
    assert cfg.merge_automation is MergeAutomation.AUTO


def test_each_level_parses() -> None:
    for level in ("auto", "approve", "manual"):
        cfg = parse_project_config(f'[automation]\nmerge = "{level}"\n')
        assert cfg.merge_automation.value == level


def test_invalid_level_raises_not_defaults() -> None:
    # A typo'd level must not silently become full automation.
    with pytest.raises(ProjectConfigError, match=r"invalid automation\.merge"):
        parse_project_config('[automation]\nmerge = "semi"\n')


def test_malformed_toml_raises() -> None:
    with pytest.raises(ProjectConfigError, match="malformed"):
        parse_project_config("not == toml")


def test_unknown_keys_ignored() -> None:
    cfg = parse_project_config(
        '[automation]\nmerge = "approve"\nfuture_knob = 1\n[future_section]\nx = 2\n'
    )
    assert cfg.merge_automation is MergeAutomation.APPROVE


def test_read_missing_file_defaults(tmp_path: Path) -> None:
    assert read_project_config(tmp_path) == ProjectConfig()


def test_read_from_checkout(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        '[automation]\nmerge = "manual"\n', encoding="utf-8"
    )
    cfg = read_project_config(tmp_path)
    assert cfg.merge_automation is MergeAutomation.MANUAL
