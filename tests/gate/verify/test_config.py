"""Tests for `gate.verify.config`."""

from __future__ import annotations

import pytest

from gate.verify.config import (
    DEFAULT_STYLE_THRESHOLD_LINES,
    AxiomHonestyConfig,
    AxiomPolicy,
    SorryDeltaConfig,
    SorryPolicy,
    StyleConfig,
    VerifyConfig,
    VerifyConfigError,
    parse_verify_config,
)


def test_empty_text_returns_defaults() -> None:
    cfg = parse_verify_config("")
    assert cfg == VerifyConfig()
    assert cfg.axiom_honesty == AxiomHonestyConfig()
    assert cfg.axiom_honesty.policy is AxiomPolicy.NET_ZERO
    assert cfg.axiom_honesty.allowed_axioms == ()


def test_missing_audits_section_returns_defaults() -> None:
    cfg = parse_verify_config('# just a comment\n[some.other.section]\nfoo = 1\n')
    assert cfg.axiom_honesty.policy is AxiomPolicy.NET_ZERO


def test_net_zero_explicit_with_no_list() -> None:
    cfg = parse_verify_config(
        '[audits.axiom_honesty]\npolicy = "net_zero"\n'
    )
    assert cfg.axiom_honesty.policy is AxiomPolicy.NET_ZERO
    assert cfg.axiom_honesty.allowed_axioms == ()


def test_whitelist_policy_parsed() -> None:
    cfg = parse_verify_config(
        '[audits.axiom_honesty]\n'
        'policy = "whitelist"\n'
        'allowed_axioms = ["propext", "Quot.sound", "Classical.choice"]\n'
    )
    assert cfg.axiom_honesty.policy is AxiomPolicy.WHITELIST
    assert cfg.axiom_honesty.allowed_axioms == (
        "propext",
        "Quot.sound",
        "Classical.choice",
    )


def test_invalid_policy_raises() -> None:
    with pytest.raises(VerifyConfigError, match="invalid"):
        parse_verify_config('[audits.axiom_honesty]\npolicy = "yolo"\n')


def test_malformed_toml_raises() -> None:
    with pytest.raises(VerifyConfigError, match="malformed"):
        parse_verify_config("this = is = not = toml")


def test_allowed_axioms_must_be_string_list() -> None:
    with pytest.raises(VerifyConfigError, match="list of strings"):
        parse_verify_config(
            '[audits.axiom_honesty]\n'
            'policy = "whitelist"\n'
            'allowed_axioms = [1, 2, 3]\n'
        )


def test_sorry_policy_defaults_to_block() -> None:
    cfg = parse_verify_config("")
    assert cfg.sorry_delta == SorryDeltaConfig()
    assert cfg.sorry_delta.policy is SorryPolicy.BLOCK


# --- style threshold knob --------------------------------------------------


def test_style_defaults_to_200() -> None:
    cfg = parse_verify_config("")
    assert cfg.style == StyleConfig()
    assert cfg.style.threshold_lines == DEFAULT_STYLE_THRESHOLD_LINES == 200


def test_style_threshold_parsed() -> None:
    cfg = parse_verify_config("[audits.style]\nthreshold_lines = 150\n")
    assert cfg.style.threshold_lines == 150


def test_style_threshold_must_be_int() -> None:
    with pytest.raises(VerifyConfigError, match="must be an integer"):
        parse_verify_config('[audits.style]\nthreshold_lines = "150"\n')


def test_style_threshold_bool_rejected() -> None:
    with pytest.raises(VerifyConfigError, match="must be an integer"):
        parse_verify_config("[audits.style]\nthreshold_lines = true\n")


def test_style_threshold_must_be_positive() -> None:
    with pytest.raises(VerifyConfigError, match=">= 1"):
        parse_verify_config("[audits.style]\nthreshold_lines = 0\n")


def test_sorry_policy_block_explicit() -> None:
    cfg = parse_verify_config('[audits.sorry_delta]\npolicy = "block"\n')
    assert cfg.sorry_delta.policy is SorryPolicy.BLOCK


def test_sorry_policy_report_parsed() -> None:
    cfg = parse_verify_config('[audits.sorry_delta]\npolicy = "report"\n')
    assert cfg.sorry_delta.policy is SorryPolicy.REPORT


def test_sorry_policy_independent_of_axiom_policy() -> None:
    cfg = parse_verify_config(
        '[audits.axiom_honesty]\n'
        'policy = "whitelist"\n'
        'allowed_axioms = ["propext"]\n'
        '[audits.sorry_delta]\n'
        'policy = "report"\n'
    )
    assert cfg.axiom_honesty.policy is AxiomPolicy.WHITELIST
    assert cfg.sorry_delta.policy is SorryPolicy.REPORT


def test_invalid_sorry_policy_raises() -> None:
    with pytest.raises(VerifyConfigError, match="invalid"):
        parse_verify_config('[audits.sorry_delta]\npolicy = "allow"\n')


def test_unknown_fields_ignored_forward_compat() -> None:
    cfg = parse_verify_config(
        '[audits.axiom_honesty]\n'
        'policy = "net_zero"\n'
        'future_knob = "ignored"\n'
        '[audits.future_audit]\n'
        'enabled = true\n'
    )
    assert cfg.axiom_honesty.policy is AxiomPolicy.NET_ZERO
