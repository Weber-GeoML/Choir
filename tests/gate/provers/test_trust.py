"""Tests for `gate.provers.trust` and each profile's trust-report hooks
(design note 12 §4).

Canned-transcript tests for all three provers, probe-builder tests for
lean4/rocq, the shared runner's per-decl-vs-single-probe looping, and a
live integration test against a real `lake env lean` run (skipped when
`lake`/`lean` aren't on PATH).
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.base import TrustEntry
from gate.provers.isabelle import (
    ISABELLE,
    build_isabelle_trust_ml,
    isabelle_trust_report_command,
    parse_isabelle_trust_report,
)
from gate.provers.lean4 import (
    LEAN4,
    build_lean_trust_probe,
    lean_trust_report_command,
    parse_lean_trust_report,
)
from gate.provers.rocq import (
    ROCQ,
    build_rocq_trust_probe,
    parse_rocq_trust_report,
    rocq_trust_report_command,
)
from gate.provers.trust import collect_trust_report
from gate.verify.changed_decls import detect_changed_decls

# ---------------------------------------------------------------------------
# lean4: probe builder
# ---------------------------------------------------------------------------


def test_build_lean_trust_probe_contents(tmp_path: Path) -> None:
    probe = build_lean_trust_probe(tmp_path, ["foo", "Bar.baz"], ["MyProject"])
    assert probe == tmp_path / ".choir-trust-probe.lean"
    assert probe.read_text() == "import MyProject\n#print axioms foo\n#print axioms Bar.baz\n"


def test_build_lean_trust_probe_no_imports(tmp_path: Path) -> None:
    probe = build_lean_trust_probe(tmp_path, ["foo"], [])
    assert probe.read_text() == "#print axioms foo\n"


def test_lean_trust_report_command_writes_probe_and_returns_argv(tmp_path: Path) -> None:
    argv = lean_trust_report_command(tmp_path, ["foo"], ["MyProject"])
    assert argv == ["lake", "env", "lean", ".choir-trust-probe.lean"]
    assert (tmp_path / ".choir-trust-probe.lean").exists()


def test_lean4_profile_hooks_are_wired() -> None:
    assert LEAN4.trust_report_command is lean_trust_report_command
    assert LEAN4.parse_trust_report is parse_lean_trust_report


# ---------------------------------------------------------------------------
# lean4: parser (canned output — both line shapes + noise lines).
#
# These exact strings are the empirically-confirmed shape from a live
# `lake env lean` run (Lean 4.14.0, dependency-free project) — see
# test_live_lean4_trust_report below.
# ---------------------------------------------------------------------------


def test_parse_lean_trust_report_clean_and_axioms() -> None:
    output = "\n".join(
        [
            "'t' does not depend on any axioms",
            "'uses_bad' depends on axioms: [bad]",
            "'uses_two_axioms' depends on axioms: [bad, bad2]",
        ]
    )
    entries = parse_lean_trust_report(output)
    assert entries == [
        TrustEntry(decl="t", assumptions=(), clean=True),
        TrustEntry(decl="uses_bad", assumptions=("bad",), clean=False),
        TrustEntry(decl="uses_two_axioms", assumptions=("bad", "bad2"), clean=False),
    ]


def test_parse_lean_trust_report_ignores_noise_lines() -> None:
    # ".choir-trust-probe.lean:5:14: error: unknown constant 'x'" is the
    # real shape of a mis-typed --decl name — observed live, and note
    # that it lands on *stdout*, not stderr.
    output = "\n".join(
        [
            "'t' does not depend on any axioms",
            "some build banner line",
            ".choir-trust-probe.lean:5:14: error: unknown constant 'x'",
        ]
    )
    entries = parse_lean_trust_report(output)
    assert entries == [TrustEntry(decl="t", assumptions=(), clean=True)]


def test_parse_lean_trust_report_empty_output() -> None:
    assert parse_lean_trust_report("") == []


# ---------------------------------------------------------------------------
# rocq: probe builder
# ---------------------------------------------------------------------------


def test_build_rocq_trust_probe_contents(tmp_path: Path) -> None:
    probe = build_rocq_trust_probe(tmp_path, ["foo"], ["Arith"])
    assert probe == tmp_path / ".choir-trust-probe.v"
    assert probe.read_text() == "Require Import Arith.\nPrint Assumptions foo.\n"


def test_build_rocq_trust_probe_no_imports(tmp_path: Path) -> None:
    probe = build_rocq_trust_probe(tmp_path, ["foo"], [])
    assert probe.read_text() == "Print Assumptions foo.\n"


def test_rocq_trust_report_command_writes_probe_and_returns_argv(tmp_path: Path) -> None:
    argv = rocq_trust_report_command(tmp_path, ["foo"], [])
    assert argv == ["rocq", "c", ".choir-trust-probe.v"]
    assert (tmp_path / ".choir-trust-probe.v").exists()


def test_rocq_profile_hooks_are_wired() -> None:
    assert ROCQ.trust_report_command is rocq_trust_report_command
    assert ROCQ.parse_trust_report is parse_rocq_trust_report
    assert ROCQ.one_probe_per_decl is True


# ---------------------------------------------------------------------------
# rocq: parser (canned `Print Assumptions` transcripts, per Rocq's
# documented output format — no live Rocq install here, per design
# note 12 §9).
# ---------------------------------------------------------------------------


def test_parse_rocq_trust_report_closed_context() -> None:
    output = "Closed under the global context\n"
    assert parse_rocq_trust_report(output) == [
        TrustEntry(decl="", assumptions=(), clean=True)
    ]


def test_parse_rocq_trust_report_axioms_block() -> None:
    output = textwrap.dedent(
        """\
        Axioms:
        classic : forall P : Prop, P \\/ ~ P
        my_axiom : nat -> Prop
        """
    )
    assert parse_rocq_trust_report(output) == [
        TrustEntry(decl="", assumptions=("classic", "my_axiom"), clean=False)
    ]


def test_parse_rocq_trust_report_axioms_block_stops_at_blank_line() -> None:
    output = "Axioms:\nfoo : nat\n\nsome trailing compiler banner\n"
    assert parse_rocq_trust_report(output) == [
        TrustEntry(decl="", assumptions=("foo",), clean=False)
    ]


def test_parse_rocq_trust_report_no_recognized_marker() -> None:
    assert parse_rocq_trust_report("some unrelated compiler output\n") == []


# ---------------------------------------------------------------------------
# isabelle: parser for the tagged `choir-trust:<thm>:<oracle,...>` format
# the probe emits. VALIDATED against a live Isabelle2025-2 in round 5
# (F3) — the previous recipe invoked a nonexistent `isabelle process`
# tool; see `gate/provers/isabelle.py` for the corrected invocation and
# what was run.
# ---------------------------------------------------------------------------


def test_parse_isabelle_trust_report_clean_and_oracles() -> None:
    output = "\n".join(
        [
            "choir-trust:add_comm_nat:",
            "choir-trust:le_trans_ex:Pure.skip_proof",
            "choir-trust:uses_oracle:my_oracle,Pure.skip_proof",
        ]
    )
    entries = parse_isabelle_trust_report(output)
    assert entries == [
        TrustEntry(decl="add_comm_nat", assumptions=(), clean=True),
        TrustEntry(decl="le_trans_ex", assumptions=("Pure.skip_proof",), clean=False),
        TrustEntry(
            decl="uses_oracle",
            assumptions=("my_oracle", "Pure.skip_proof"),
            clean=False,
        ),
    ]


def test_parse_isabelle_trust_report_matches_live_transcript() -> None:
    """Captured verbatim from the live run recorded in the round-5 report.

    `isabelle ML_process -d . -o quick_and_dirty -o show_results=false
    -e <ML>` against a two-lemma theory, one honest and one `sorry`ed.
    """
    output = "\n".join(
        [
            'Loading theory "Choir.Sample"',
            '### theory "Choir.Sample"',
            "### 0.067s elapsed time, 0.090s cpu time, 0.000s GC time",
            "choir-trust:clean_lemma:",
            "choir-trust:sorried_lemma:Pure.skip_proof",
            "val it = (): unit",
        ]
    )
    entries = parse_isabelle_trust_report(output)
    assert entries == [
        TrustEntry(decl="clean_lemma", assumptions=(), clean=True),
        TrustEntry(
            decl="sorried_lemma", assumptions=("Pure.skip_proof",), clean=False
        ),
    ]


def test_parse_isabelle_trust_report_ignores_untagged_lines() -> None:
    """The tag is what makes the parse unambiguous (round 5, F3).

    Isabelle echoes each loaded theorem as `theorem name: prop`, and its
    pretty-printer can emit `foo:: "nat"` for a constant declaration —
    which the previous untagged `^(\\S+):\\s*(.*)$` parser read as a
    report line for a declaration named `foo:`. `show_results=false`
    suppresses those echoes and the tag makes them harmless either way.
    """
    output = "\n".join(
        [
            'Loading theory "Choir.Sample"',
            "consts",
            '  foo:: "nat"',
            "theorem clean_lemma: foo = 5",
            "choir-trust:add_comm_nat:",
            "",
            "### Some Isabelle warning banner",
            "val it = (): unit",
        ]
    )
    entries = parse_isabelle_trust_report(output)
    assert entries == [TrustEntry(decl="add_comm_nat", assumptions=(), clean=True)]


def test_isabelle_trust_report_command_invokes_a_tool_that_exists() -> None:
    """F3's defect, pinned by name.

    The recipe used to build `isabelle process -e <ML>`, and there is no
    `isabelle process` tool on Isabelle2025-2 — the tool list has
    `ML_process`, `console` and `process_theories`. Nothing about a
    documented-but-unvalidated recipe would have caught that; asserting
    the tool name does.
    """
    argv = isabelle_trust_report_command(Path("/ws"), ["foo"], ["Sample"])
    assert argv[:2] == ["isabelle", "ML_process"]
    assert "process" not in argv


def test_isabelle_trust_probe_ml_loads_imports_as_theories() -> None:
    """`imports` are theory names loaded from source — not a session.

    The old recipe passed `imports[0]` to `-l` as a session name, which
    the auto-detection caller cannot supply (it derives a theory stem
    from the changed file's path) and which needs a heap image the
    shipped isabelle workflow's `isabelle build -D .` does not produce.
    """
    ml = build_isabelle_trust_ml(["foo", "bar"], ["Sample", "Other"])
    assert "Thm_Deps.all_oracles" in ml
    assert "Proofterm.all_oracles_of" not in ml  # does not exist in Pure
    assert '("Sample", Position.none)' in ml
    assert '("Other", Position.none)' in ml
    assert 'report "foo"' in ml
    assert 'report "bar"' in ml

    argv = isabelle_trust_report_command(Path("/ws"), ["foo"], ["Sample"])
    assert "-l" not in argv  # base logic comes from ISABELLE_LOGIC
    assert "quick_and_dirty" in argv  # or a `sorry` theory will not load
    assert "show_results=false" in argv


def test_isabelle_trust_probe_ml_omits_the_load_step_with_no_imports() -> None:
    ml = build_isabelle_trust_ml(["foo"], [])
    assert "Thy_Info.use_theories" not in ml
    assert 'report "foo"' in ml


def test_isabelle_profile_hooks_are_wired() -> None:
    assert ISABELLE.trust_report_command is isabelle_trust_report_command
    assert ISABELLE.parse_trust_report is parse_isabelle_trust_report
    assert ISABELLE.one_probe_per_decl is False


# ---------------------------------------------------------------------------
# collect_trust_report: shared runner
# ---------------------------------------------------------------------------


def test_collect_trust_report_empty_targets_short_circuits(tmp_path: Path) -> None:
    # LEAN4's real trust_report_command would try to run `lake` if this
    # didn't short-circuit — proves the empty-targets guard without
    # needing a Lean toolchain.
    assert collect_trust_report(LEAN4, tmp_path, [], []) == []


def test_collect_trust_report_single_probe_for_multi_decl_profile(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        calls.append(list(targets))
        return ["true"]

    def fake_parse(output: str) -> list[TrustEntry]:
        return [
            TrustEntry(decl="t", assumptions=(), clean=True),
            TrustEntry(decl="u", assumptions=("bad",), clean=False),
        ]

    fake_profile = dataclasses.replace(
        LEAN4, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    entries = collect_trust_report(fake_profile, tmp_path, ["t", "u"], ["Mod"])

    assert calls == [["t", "u"]]  # one combined probe, not two
    assert [e.decl for e in entries] == ["t", "u"]


def test_collect_trust_report_loops_per_decl_and_attributes_decl_names(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        calls.append(list(targets))
        return ["true"]

    def fake_parse(output: str) -> list[TrustEntry]:
        # rocq-shaped: decl-less placeholder, filled in by the runner.
        return [TrustEntry(decl="", assumptions=(), clean=True)]

    fake_profile = dataclasses.replace(
        ROCQ, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    entries = collect_trust_report(fake_profile, tmp_path, ["foo", "bar"], [])

    assert calls == [["foo"], ["bar"]]  # one probe per decl
    assert [e.decl for e in entries] == ["foo", "bar"]
    assert all(e.clean for e in entries)


def test_collect_trust_report_nonzero_exit_raises_prover_error(tmp_path: Path) -> None:
    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        return ["false"]

    def fake_parse(output: str) -> list[TrustEntry]:
        return []

    fake_profile = dataclasses.replace(
        LEAN4, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    with pytest.raises(ProverError, match="trust-report probe failed"):
        collect_trust_report(fake_profile, tmp_path, ["t"], [])


def test_collect_trust_report_prefers_stderr_detail_but_falls_back_to_stdout(
    tmp_path: Path,
) -> None:
    # Lean's own errors land on stdout with empty stderr (observed
    # live) — the runner must not raise an empty-detail error in that
    # case.
    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        return ["sh", "-c", "echo boom-on-stdout; exit 1"]

    def fake_parse(output: str) -> list[TrustEntry]:
        return []

    fake_profile = dataclasses.replace(
        LEAN4, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    with pytest.raises(ProverError, match="boom-on-stdout"):
        collect_trust_report(fake_profile, tmp_path, ["t"], [])


def test_collect_trust_report_cleans_up_probe_file_on_success(tmp_path: Path) -> None:
    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        (workspace / ".choir-trust-probe.lean").write_text("probe contents\n")
        return ["true"]

    def fake_parse(output: str) -> list[TrustEntry]:
        return [TrustEntry(decl="t", assumptions=(), clean=True)]

    fake_profile = dataclasses.replace(
        LEAN4, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    collect_trust_report(fake_profile, tmp_path, ["t"], [])

    assert list(tmp_path.glob(".choir-trust-probe.*")) == []


def test_collect_trust_report_cleans_up_probe_file_on_error(tmp_path: Path) -> None:
    def fake_command(workspace: Path, targets: list[str], imports: list[str]) -> list[str]:
        (workspace / ".choir-trust-probe.lean").write_text("probe contents\n")
        return ["false"]

    def fake_parse(output: str) -> list[TrustEntry]:
        return []

    fake_profile = dataclasses.replace(
        LEAN4, trust_report_command=fake_command, parse_trust_report=fake_parse
    )
    with pytest.raises(ProverError, match="trust-report probe failed"):
        collect_trust_report(fake_profile, tmp_path, ["t"], [])

    assert list(tmp_path.glob(".choir-trust-probe.*")) == []


# ---------------------------------------------------------------------------
# Live lean4 integration test (design note 12 §4/§10).
# ---------------------------------------------------------------------------

_LAKE_AND_LEAN_AVAILABLE = shutil.which("lake") is not None and shutil.which("lean") is not None


def _default_lean_toolchain() -> str:
    """Best-effort toolchain pin for the live-test project.

    Prefers whatever elan already has set as the machine's default;
    falls back to the pin `samples/lean4/lean-toolchain` uses (this
    repo's own reference project) so the test works even when no
    default toolchain is configured (e.g. `elan ... --default-toolchain
    none`, which is how this test was validated).
    """
    try:
        result = subprocess.run(
            ["elan", "show"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            if "(default)" in line:
                return line.split()[0]

    fallback = Path(__file__).resolve().parents[3] / "samples" / "lean4" / "lean-toolchain"
    if fallback.exists():
        return fallback.read_text().strip()
    return "leanprover/lean4:stable"


@pytest.mark.slow
@pytest.mark.skipif(
    not _LAKE_AND_LEAN_AVAILABLE,
    reason="lake/lean not on PATH — live lean4 trust-report test needs a local elan toolchain",
)
def test_live_lean4_trust_report(tmp_path: Path) -> None:
    project = tmp_path / "trust_probe_project"
    project.mkdir()
    (project / "lean-toolchain").write_text(_default_lean_toolchain() + "\n")
    (project / "lakefile.toml").write_text(
        'name = "TrustProbe"\n'
        'defaultTargets = ["TrustProbe"]\n'
        "\n"
        "[[lean_lib]]\n"
        'name = "TrustProbe"\n'
    )
    (project / "TrustProbe.lean").write_text(
        textwrap.dedent(
            """\
            theorem t : 1 = 1 := rfl

            axiom bad : False

            theorem uses_bad : False := bad
            """
        )
    )

    build = subprocess.run(
        ["lake", "build"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    entries = collect_trust_report(LEAN4, project, ["t", "uses_bad"], ["TrustProbe"])
    by_decl = {e.decl: e for e in entries}

    assert by_decl["t"].clean is True
    assert by_decl["t"].assumptions == ()
    assert by_decl["uses_bad"].clean is False
    assert by_decl["uses_bad"].assumptions == ("bad",)


@pytest.mark.slow
@pytest.mark.skipif(
    not _LAKE_AND_LEAN_AVAILABLE,
    reason="lake/lean not on PATH — live lean4 trust-report test needs a local elan toolchain",
)
def test_live_lean4_trust_report_auto_mode(tmp_path: Path) -> None:
    """End-to-end for design note 12 §4.1: detect, then probe each target.

    Same live-toolchain fixture as `test_live_lean4_trust_report` above,
    but exercised through `detect_changed_decls` (git-diff-driven)
    instead of hand-supplied `--decl` targets — this is what
    `verify-trust-report.yml`'s `--base-sha` auto mode does end to end.
    """
    project = tmp_path / "trust_probe_auto_project"
    project.mkdir()
    (project / "lean-toolchain").write_text(_default_lean_toolchain() + "\n")
    (project / "lakefile.toml").write_text(
        'name = "TrustProbeAuto"\n'
        'defaultTargets = ["TrustProbeAuto"]\n'
        "\n"
        "[[lean_lib]]\n"
        'name = "TrustProbeAuto"\n'
    )
    (project / "TrustProbeAuto.lean").write_text(
        "theorem t : 1 = 1 := rfl\n"
    )

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=project, capture_output=True, text=True, check=True
        )
        return result.stdout

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", "-A")
    _git("commit", "-q", "-m", "base: clean theorem only")
    base_sha = _git("rev-parse", "HEAD").strip()

    # Head: add an axiom-using theorem alongside the untouched clean one.
    (project / "TrustProbeAuto.lean").write_text(
        textwrap.dedent(
            """\
            theorem t : 1 = 1 := rfl

            axiom bad : False

            theorem uses_bad : False := bad
            """
        )
    )

    build = subprocess.run(
        ["lake", "build"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    targets, capped = detect_changed_decls(project, base_sha, LEAN4)
    assert capped is False
    names = {t.name for t in targets}
    # `t` is untouched (same statement, same line) — must not be
    # re-probed; `bad`/`uses_bad` are new declarations.
    assert names == {"bad", "uses_bad"}

    for target in targets:
        entries = collect_trust_report(
            LEAN4, project, [target.name], [target.module]
        )
        assert entries and entries[0].decl == target.name


# ---------------------------------------------------------------------------
# Live isabelle integration test (round 5, F3).
#
# The recipe this exercises replaced an `isabelle process -e` invocation
# that named a tool which does not exist. A canned-transcript parser test
# cannot catch that class of defect — only running the argv can — so the
# fix ships with a live test, skipped when `isabelle` is not on PATH.
#
# Deliberately needs no built project heap: the probe loads the theory
# from source, which is what makes it work under the shipped isabelle
# workflow's `isabelle build -D .` (no `-b`, so no heap image).
# ---------------------------------------------------------------------------

_ISABELLE_AVAILABLE = shutil.which("isabelle") is not None

_ISABELLE_PROBE_THEORY = """\
theory TrustProbe
  imports Main
begin

definition foo :: nat where "foo = 5"

lemma clean_lemma: "foo = 5"
  unfolding foo_def by simp

lemma sorried_lemma: "foo = 6"
  sorry

end
"""


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — live isabelle trust-report test needs a local install",
)
def test_live_isabelle_trust_report(tmp_path: Path) -> None:
    """`sorry` must surface as the `Pure.skip_proof` oracle, honest proofs clean."""
    project = tmp_path / "isabelle_trust_project"
    project.mkdir()
    (project / "ROOT").write_text(
        "session TrustProbeSession = HOL +\n  theories\n    TrustProbe\n"
    )
    (project / "TrustProbe.thy").write_text(_ISABELLE_PROBE_THEORY)

    entries = collect_trust_report(
        ISABELLE, project, ["clean_lemma", "sorried_lemma"], ["TrustProbe"]
    )
    by_decl = {e.decl: e for e in entries}

    assert by_decl["clean_lemma"].clean is True
    assert by_decl["clean_lemma"].assumptions == ()
    assert by_decl["sorried_lemma"].clean is False
    assert by_decl["sorried_lemma"].assumptions == ("Pure.skip_proof",)


@pytest.mark.slow
@pytest.mark.skipif(
    not _ISABELLE_AVAILABLE,
    reason="isabelle not on PATH — live isabelle trust-report test needs a local install",
)
def test_live_isabelle_trust_report_auto_mode(tmp_path: Path) -> None:
    """The CI-shaped invocation: detect changed declarations, then probe each.

    Same shape as `test_live_lean4_trust_report_auto_mode`, and the one
    that matters for F3 — `detect_changed_decls` supplies the theory
    stem as the probe's single import, which is exactly what the old
    recipe mis-used as a session name.
    """
    project = tmp_path / "isabelle_trust_auto_project"
    project.mkdir()
    (project / "ROOT").write_text(
        "session TrustProbeAuto = HOL +\n  theories\n    TrustProbe\n"
    )
    (project / "TrustProbe.thy").write_text(
        "theory TrustProbe\n"
        "  imports Main\n"
        "begin\n"
        "\n"
        'definition foo :: nat where "foo = 5"\n'
        "\n"
        'lemma base_lemma: "foo = 5"\n'
        "  unfolding foo_def by simp\n"
        "\n"
        "end\n"
    )

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=project, capture_output=True, text=True, check=True
        )
        return result.stdout

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", "-A")
    _git("commit", "-q", "-m", "base: one honest lemma")
    base_sha = _git("rev-parse", "HEAD").strip()

    (project / "TrustProbe.thy").write_text(_ISABELLE_PROBE_THEORY)

    targets, capped = detect_changed_decls(project, base_sha, ISABELLE)
    assert capped is False
    assert {t.name for t in targets} == {"clean_lemma", "sorried_lemma"}

    seen: dict[str, TrustEntry] = {}
    for target in targets:
        entries = collect_trust_report(
            ISABELLE, project, [target.name], [target.module]
        )
        assert entries, target.name
        seen[target.name] = entries[0]

    assert seen["clean_lemma"].clean is True
    assert seen["sorried_lemma"].assumptions == ("Pure.skip_proof",)
