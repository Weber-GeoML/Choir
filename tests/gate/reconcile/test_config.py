"""Tests for `gate.reconcile.config` and the CLI threshold precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.reconcile.cli import _resolve_threshold
from gate.reconcile.config import (
    DEFAULT_STALE_AFTER_DAYS,
    ReconcileConfig,
    ReconcileConfigError,
    parse_reconcile_config,
    read_reconcile_config,
)


def test_empty_text_defaults() -> None:
    cfg = parse_reconcile_config("")
    assert cfg == ReconcileConfig()
    assert cfg.stale_after_days == DEFAULT_STALE_AFTER_DAYS


def test_explicit_value_parses() -> None:
    cfg = parse_reconcile_config("[reconcile]\nstale_after_days = 21\n")
    assert cfg.stale_after_days == 21


def test_automation_section_alone_yields_default() -> None:
    # The orchestrator's [automation] section shares the file and must not
    # trip this reader.
    cfg = parse_reconcile_config('[automation]\nmerge = "approve"\n')
    assert cfg.stale_after_days == DEFAULT_STALE_AFTER_DAYS


def test_malformed_toml_raises() -> None:
    with pytest.raises(ReconcileConfigError, match="malformed"):
        parse_reconcile_config("not == toml")


def test_non_integer_raises() -> None:
    with pytest.raises(ReconcileConfigError, match="must be an integer"):
        parse_reconcile_config('[reconcile]\nstale_after_days = "7"\n')


def test_bool_rejected_not_coerced_to_int() -> None:
    # bool subclasses int in Python; `= true` must not become 1.
    with pytest.raises(ReconcileConfigError, match="must be an integer"):
        parse_reconcile_config("[reconcile]\nstale_after_days = true\n")


def test_zero_and_negative_rejected() -> None:
    for bad in (0, -3):
        with pytest.raises(ReconcileConfigError, match=">= 1"):
            parse_reconcile_config(f"[reconcile]\nstale_after_days = {bad}\n")


def test_unknown_keys_ignored() -> None:
    cfg = parse_reconcile_config(
        "[reconcile]\nstale_after_days = 14\nfuture_knob = 1\n"
    )
    assert cfg.stale_after_days == 14


def test_read_missing_file_defaults(tmp_path: Path) -> None:
    assert read_reconcile_config(tmp_path) == ReconcileConfig()


def test_read_from_checkout(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "[automation]\nmerge = \"auto\"\n[reconcile]\nstale_after_days = 30\n",
        encoding="utf-8",
    )
    assert read_reconcile_config(tmp_path).stale_after_days == 30


def test_resolve_threshold_flag_wins(tmp_path: Path) -> None:
    # Explicit flag beats project config (operator one-off override).
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "[reconcile]\nstale_after_days = 30\n", encoding="utf-8"
    )
    assert _resolve_threshold(3, tmp_path) == 3


def test_resolve_threshold_reads_config_when_no_flag(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "[reconcile]\nstale_after_days = 30\n", encoding="utf-8"
    )
    assert _resolve_threshold(None, tmp_path) == 30


def test_resolve_threshold_default_when_no_flag_no_config(tmp_path: Path) -> None:
    assert _resolve_threshold(None, tmp_path) == DEFAULT_STALE_AFTER_DAYS


def test_resolve_threshold_warns_and_defaults_on_bad_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A malformed config must not crash the daily job — warn + default.
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "[reconcile]\nstale_after_days = -1\n", encoding="utf-8"
    )
    assert _resolve_threshold(None, tmp_path) == DEFAULT_STALE_AFTER_DAYS
    assert "warning" in capsys.readouterr().err.lower()
