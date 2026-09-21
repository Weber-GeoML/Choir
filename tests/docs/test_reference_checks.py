"""Guard: `docs/reference.md`'s Checks table matches `gate/checks.py`.

Two hand-maintained copies of one fact drift. This pins the second to the
first, mirroring `tests/gate/test_checks.py`'s idiom.

Parses the pipe-table under `docs/reference.md`'s `## Checks` heading.
Each row's third cell is the check's class — ``Trust``, ``Quality``,
``Advisory``, or a base class with a per-prover relaxation noted inline,
``Trust (advisory on lean4)``. The fourth cell is ``Yes``/``No`` for
`REQUIRED_PRESENT` membership. `review` is deliberately absent from the
table.
"""

from __future__ import annotations

import re
from pathlib import Path

from gate.checks import CHECKS, PROVER_OVERRIDES, REQUIRED_PRESENT, CheckClass

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "docs" / "reference.md"

_CLASS_CELL = re.compile(
    r"^(Trust|Quality|Advisory)(?:\s*\(advisory on (lean4|isabelle|rocq)\))?$"
)
_CLASS_WORD = {
    "Trust": CheckClass.TRUST,
    "Quality": CheckClass.QUALITY,
    "Advisory": CheckClass.ADVISORY,
}
_ROW_NAME = re.compile(r"^`([a-z-]+)`$")


def _extract_section(text: str, heading: str) -> str:
    """Lines strictly between a `## <heading>` line and the next `## `."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    assert start is not None, f"heading {heading!r} not found in {REFERENCE}"
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def _table_rows(text: str) -> list[tuple[str, str, str]]:
    """`(check_name, class_cell, required_cell)` for each real row.

    Header and separator rows have no backtick-quoted first cell, so they
    fall out of the `_ROW_NAME` match without special-casing.
    """
    rows: list[tuple[str, str, str]] = []
    for raw_line in _extract_section(text, "## Checks").splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        m = _ROW_NAME.match(cells[0])
        if not m:
            continue
        rows.append((m.group(1), cells[2], cells[3]))
    return rows


def _diff_against_registry(rows: list[tuple[str, str, str]]) -> list[str]:
    """Mismatches between parsed table rows and `gate/checks.py`.

    Empty list means the table and the registry agree on every live check's
    base class, per-prover override (if any), and `REQUIRED_PRESENT`
    membership.
    """
    problems: list[str] = []
    documented: dict[str, tuple[CheckClass, str | None, bool]] = {}

    for name, class_cell, required_cell in rows:
        m = _CLASS_CELL.match(class_cell)
        if not m:
            problems.append(f"{name}: unparseable class cell {class_cell!r}")
            continue
        if required_cell not in ("Yes", "No"):
            problems.append(f"{name}: unparseable required cell {required_cell!r}")
            continue
        documented[name] = (
            _CLASS_WORD[m.group(1)],
            m.group(2),
            required_cell == "Yes",
        )

    live_checks = set(CHECKS) - {"review"}  # vestigial; deliberately not tabled
    doc_names = set(documented)

    missing = live_checks - doc_names
    if missing:
        problems.append(f"checks missing from the table: {sorted(missing)}")
    extra = doc_names - live_checks
    if extra:
        problems.append(f"table names not a live registered check: {sorted(extra)}")

    expected_override_prover: dict[str, str] = {}
    for prover, overrides in PROVER_OVERRIDES.items():
        for name, cls in overrides.items():
            if cls is CheckClass.ADVISORY:
                expected_override_prover[name] = prover

    for name in live_checks & doc_names:
        base, override_prover, required = documented[name]
        if base is not CHECKS[name]:
            problems.append(
                f"{name}: table base class is {base}, registry says {CHECKS[name]}"
            )
        expected_prover = expected_override_prover.get(name)
        if override_prover != expected_prover:
            problems.append(
                f"{name}: table names override prover {override_prover!r}, "
                f"registry names {expected_prover!r}"
            )
        expected_required = name in REQUIRED_PRESENT
        if required != expected_required:
            problems.append(
                f"{name}: table says required-present={required}, "
                f"registry says {expected_required}"
            )
    return problems


def test_checks_table_matches_registry() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    rows = _table_rows(text)
    assert rows, "no check rows parsed out of docs/reference.md's ## Checks table"
    problems = _diff_against_registry(rows)
    assert not problems, (
        "docs/reference.md's Checks table has drifted from gate/checks.py:\n"
        + "\n".join(problems)
    )


def test_checks_table_guard_catches_a_wrong_row() -> None:
    """Proof the guard has teeth: flip one real row's class and it fails.

    `comparator` is `Trust` with no override in the real table (matching
    `CHECKS["comparator"] is CheckClass.TRUST`, which has no
    `PROVER_OVERRIDES` entry on any prover). Rewriting just that row's
    class cell to `Advisory` — exactly the class of error this guard
    exists to catch, a doc asserting the wrong blocking status for a
    check — must make the comparison fail.
    """
    text = REFERENCE.read_text(encoding="utf-8")
    corrupted, count = re.subn(
        r"(\|\s*`comparator`\s*\|.*?\|\s*)Trust(\s*\|\s*Yes\s*\|)",
        r"\1Advisory\2",
        text,
        count=1,
    )
    assert count == 1, "could not locate the comparator row to corrupt"
    assert corrupted != text

    problems = _diff_against_registry(_table_rows(corrupted))
    assert problems, "corrupting the comparator row did not trip the guard"
    assert any("comparator" in p for p in problems)
