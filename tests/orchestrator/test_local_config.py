"""Tests for `orchestrator.local_config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import local_config
from orchestrator.local_config import (
    DEFAULT_MAX_WAIT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    LocalConfig,
    LocalConfigError,
    parse_local_config,
    project_orchestrator_config_path,
    read_local_config,
)


def test_empty_text_all_defaults() -> None:
    cfg = parse_local_config("")
    assert cfg == LocalConfig()
    assert cfg.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert cfg.max_wait_seconds == DEFAULT_MAX_WAIT_SECONDS
    assert cfg.repo is None
    assert cfg.checkout is None


def test_full_config_parses() -> None:
    cfg = parse_local_config(
        '[project]\nrepo = "a/b"\ncheckout = "~/work/b"\n'
        "[loop]\npoll_interval_seconds = 120\nmax_wait_seconds = 900\n"
    )
    assert cfg.repo == "a/b"
    assert cfg.checkout == Path("~/work/b").expanduser()
    assert cfg.poll_interval_seconds == 120
    assert cfg.max_wait_seconds == 900


def test_tilde_expanded_in_checkout() -> None:
    cfg = parse_local_config('[project]\ncheckout = "~/x"\n')
    assert cfg.checkout is not None
    assert "~" not in str(cfg.checkout)


def test_zero_interval_rejected() -> None:
    with pytest.raises(LocalConfigError, match="positive integer"):
        parse_local_config("[loop]\npoll_interval_seconds = 0\n")


def test_non_int_interval_rejected() -> None:
    with pytest.raises(LocalConfigError, match="positive integer"):
        parse_local_config('[loop]\npoll_interval_seconds = "ten"\n')


def test_malformed_toml_raises() -> None:
    with pytest.raises(LocalConfigError, match="malformed"):
        parse_local_config("not == toml")


def test_unknown_keys_ignored() -> None:
    cfg = parse_local_config("[loop]\nfuture = 1\n[future_section]\nx = 2\n")
    assert cfg.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS


def test_read_missing_file_defaults(tmp_path: Path) -> None:
    assert read_local_config(path=tmp_path / "absent.toml") == LocalConfig()


def test_read_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "orchestrator.toml"
    p.write_text('[project]\nrepo = "x/y"\n', encoding="utf-8")
    assert read_local_config(path=p).repo == "x/y"


# --- per-project resolution (multi-project on one machine) -----------------


def _redirect_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(
        local_config, "ORCHESTRATOR_CONFIG_PATH", tmp_path / "orchestrator.toml"
    )


def test_project_path_shape(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _redirect_paths(monkeypatch, tmp_path)
    assert (
        project_orchestrator_config_path("alice/proj")
        == tmp_path / "projects" / "alice" / "proj" / "orchestrator.toml"
    )


def test_per_project_preferred_over_legacy(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "orchestrator.toml").write_text(
        '[project]\nrepo = "legacy/repo"\n', encoding="utf-8"
    )
    pp = tmp_path / "projects" / "alice" / "proj" / "orchestrator.toml"
    pp.parent.mkdir(parents=True)
    pp.write_text('[project]\ncheckout = "/work/proj"\n', encoding="utf-8")
    cfg = read_local_config(repo="alice/proj")
    assert cfg.checkout == Path("/work/proj")  # per-project file used


def test_falls_back_to_legacy_when_no_per_project(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "orchestrator.toml").write_text(
        '[project]\nrepo = "legacy/repo"\n', encoding="utf-8"
    )
    assert read_local_config(repo="alice/proj").repo == "legacy/repo"


def test_repo_with_nothing_present_defaults(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _redirect_paths(monkeypatch, tmp_path)
    assert read_local_config(repo="alice/proj") == LocalConfig()
