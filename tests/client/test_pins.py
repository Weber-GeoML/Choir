"""Tests for `client.pins`."""

from __future__ import annotations

import textwrap
from pathlib import Path

from client.pins import (
    DetectionStatus,
    PinCheck,
    Pins,
    check_pins,
    format_pin_report,
    read_pins,
)

# ---------------------------------------------------------------------------
# read_pins
# ---------------------------------------------------------------------------


def _write_pins(workspace: Path, content: str) -> None:
    pins_dir = workspace / ".choir"
    pins_dir.mkdir(parents=True, exist_ok=True)
    (pins_dir / "pins.toml").write_text(content, encoding="utf-8")


def test_read_pins_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_pins(tmp_path) is None


def test_read_pins_empty_file(tmp_path: Path) -> None:
    _write_pins(tmp_path, "")
    pins = read_pins(tmp_path)
    assert pins == Pins()


def test_read_pins_with_lean4_skills(tmp_path: Path) -> None:
    _write_pins(
        tmp_path,
        textwrap.dedent(
            """\
            [pins]
            lean4_skills = "v0.2.3"
            """
        ),
    )
    pins = read_pins(tmp_path)
    assert pins == Pins(lean4_skills="v0.2.3")


def test_read_pins_with_skill_pack_version(tmp_path: Path) -> None:
    _write_pins(
        tmp_path,
        textwrap.dedent(
            """\
            [pins]
            lean4_skills = "v0.2.3"

            [skill_pack]
            version = "v1.0"
            """
        ),
    )
    pins = read_pins(tmp_path)
    assert pins == Pins(lean4_skills="v0.2.3", skill_pack_version="v1.0")


def test_read_pins_ignores_unknown_fields(tmp_path: Path) -> None:
    _write_pins(
        tmp_path,
        textwrap.dedent(
            """\
            [pins]
            lean4_skills = "v0.2.3"
            unknown_tool = "v9.9.9"
            """
        ),
    )
    # Future tooling can read unknown_tool; today we just ignore it.
    pins = read_pins(tmp_path)
    assert pins is not None
    assert pins.lean4_skills == "v0.2.3"


def test_read_pins_malformed_toml_returns_empty(tmp_path: Path) -> None:
    _write_pins(tmp_path, "this is not valid toml [[[")
    pins = read_pins(tmp_path)
    # Malformed → empty pins (don't crash the workspace setup).
    assert pins == Pins()


# ---------------------------------------------------------------------------
# check_pins (with detection mocked)
# ---------------------------------------------------------------------------


def test_check_pins_no_file_returns_empty(tmp_path: Path) -> None:
    assert check_pins(tmp_path) == []


def test_check_pins_with_lean4_skills_pin_not_installed(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "client.pins.detect_lean4_skills_version",
        lambda: (None, DetectionStatus.NOT_INSTALLED),
    )
    _write_pins(tmp_path, '[pins]\nlean4_skills = "v0.2.3"\n')
    checks = check_pins(tmp_path)
    assert len(checks) == 1
    assert checks[0].name == "lean4-skills"
    assert checks[0].pinned == "v0.2.3"
    assert checks[0].observed is None
    assert checks[0].status == DetectionStatus.NOT_INSTALLED
    # Undetectable is treated as match (advisory, not blocking).
    assert checks[0].matches is True


def test_check_pins_with_lean4_skills_pin_timed_out(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "client.pins.detect_lean4_skills_version",
        lambda: (None, DetectionStatus.TIMED_OUT),
    )
    _write_pins(tmp_path, '[pins]\nlean4_skills = "v0.2.3"\n')
    checks = check_pins(tmp_path)
    assert checks[0].status == DetectionStatus.TIMED_OUT


def test_check_pins_matching_version(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "client.pins.detect_lean4_skills_version",
        lambda: ("v0.2.3", DetectionStatus.OBSERVED),
    )
    _write_pins(tmp_path, '[pins]\nlean4_skills = "v0.2.3"\n')
    checks = check_pins(tmp_path)
    assert len(checks) == 1
    assert checks[0].matches is True
    assert checks[0].observed == "v0.2.3"
    assert checks[0].status == DetectionStatus.OBSERVED


def test_check_pins_version_mismatch(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "client.pins.detect_lean4_skills_version",
        lambda: ("v0.1.0", DetectionStatus.OBSERVED),
    )
    _write_pins(tmp_path, '[pins]\nlean4_skills = "v0.2.3"\n')
    checks = check_pins(tmp_path)
    assert len(checks) == 1
    assert checks[0].matches is False
    assert checks[0].observed == "v0.1.0"
    assert checks[0].pinned == "v0.2.3"
    assert checks[0].status == DetectionStatus.OBSERVED


# ---------------------------------------------------------------------------
# format_pin_report
# ---------------------------------------------------------------------------


def test_format_pin_report_empty() -> None:
    assert format_pin_report([]) == ""


def test_format_pin_report_match() -> None:
    out = format_pin_report(
        [PinCheck(name="lean4-skills", pinned="v0.2.3", observed="v0.2.3", matches=True)]
    )
    assert "matches pin" in out
    assert "v0.2.3" in out


def test_format_pin_report_mismatch_uses_warning_marker() -> None:
    out = format_pin_report(
        [PinCheck(name="lean4-skills", pinned="v0.2.3", observed="v0.1.0", matches=False)]
    )
    assert "⚠" in out
    assert "v0.1.0" in out
    assert "v0.2.3" in out


def test_format_pin_report_undetectable_says_so() -> None:
    # No status given → defaults to NOT_DETECTED → "couldn't detect" branch.
    out = format_pin_report(
        [PinCheck(name="lean4-skills", pinned="v0.2.3", observed=None, matches=True)]
    )
    assert "couldn't detect" in out
    assert "verify manually" in out


def test_format_pin_report_not_installed_specific_diagnostic() -> None:
    out = format_pin_report(
        [
            PinCheck(
                name="lean4-skills",
                pinned="v0.2.3",
                observed=None,
                matches=True,
                status=DetectionStatus.NOT_INSTALLED,
            )
        ]
    )
    assert "not installed" in out
    assert "Install" in out
    # Doesn't say "couldn't detect" — that's the generic fallback.
    assert "couldn't detect" not in out


def test_format_pin_report_timed_out_says_so() -> None:
    out = format_pin_report(
        [
            PinCheck(
                name="lean4-skills",
                pinned="v0.2.3",
                observed=None,
                matches=True,
                status=DetectionStatus.TIMED_OUT,
            )
        ]
    )
    assert "timed out" in out
    assert "re-install" in out.lower() or "broken" in out.lower()


def test_format_pin_report_command_failed_says_so() -> None:
    out = format_pin_report(
        [
            PinCheck(
                name="lean4-skills",
                pinned="v0.2.3",
                observed=None,
                matches=True,
                status=DetectionStatus.COMMAND_FAILED,
            )
        ]
    )
    assert "returned an error" in out or "error" in out.lower()
