"""Guard: no module under `gate/` compiles its own copy of the
declaration-boundary regex.

Four modules once each hand-rolled `^\\s*(KEYWORDS)\\s+(\\S+)` — a
duplicate of what `gate.provers.decl_syntax.decl_line_regex` now
supplies as the single source of truth. Only one copy ever got the
name-normalization fix for the colon-swallowing bug (fix2); the other
three carried it unfixed until this task. `gate/checks.py` exists in
this project precisely because two hand-maintained copies of one fact
drifted apart into a live bug — a fifth copy of this regex is cheaper
to add than to find, so this test greps for the regex's *shape*
(structure), not the literal string, so a differently-spelled fifth
copy still trips it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_ROOT = REPO_ROOT / "gate"

# The shape: a `^\s*` line anchor, then an opening capturing or
# non-capturing group *immediately* following it (a keyword
# alternation, whether inline or interpolated), a closing paren,
# `\s+`, and a final `(\S+)` name capture. Matches every historical
# duplicate spelling (see `_HISTORICAL_SHAPES` below) regardless of
# whether the keyword group itself captures.
#
# Does NOT match `gate.provers.decl_syntax.decl_line_regex`'s own
# construction (`rf"^\s*{prefix}({keyword_alt})\s+(\S+)"`): the
# `{prefix}` fragment sits between the anchor and the opening paren,
# so the paren is not *immediate* — verified directly against that
# file below rather than assumed, and there is no separate allowlist
# for it. It also does not match purpose-built regexes with a
# different name-capture shape (`gate.inventory.scan`'s `_AXIOM_RE`,
# whose name group is a character class, not `(\S+)`) or a single
# literal keyword instead of a group (`gate.provers.lean4`'s
# `_NAMESPACE_OPEN_RE`/`_SECTION_OPEN_RE`/`_END_RE`).
_SHAPE_RE = re.compile(r"\^\\s\*\(.*?\)\\s\+\(\\S\+\)")

# The regex-source text of the four historical duplicates (as they
# would appear literally in a .py file), pinned so this guard's own
# detector is proven non-vacuous rather than just asserted clean.
_HISTORICAL_SHAPES = (
    r"^\s*({alternation})\s+(\S+)",  # gate/verify/style.py's original
    # gate/indexer/extract.py's and gate/inventory/scan.py's original:
    r"^\s*(?:{alternation})\s+(\S+)",
    # gate/provers/lean4.py's original:
    r"^\s*(?:{'|'.join(re.escape(k) for k in _DECL_KEYWORDS)})\s+(\S+)",
)


def test_guard_detects_each_historical_duplicate_shape() -> None:
    """Sanity check: the detector isn't vacuous — it must flag every
    historical spelling if any were reintroduced verbatim."""
    for shape in _HISTORICAL_SHAPES:
        assert _SHAPE_RE.search(shape), shape


def test_guard_does_not_flag_decl_syntax_own_construction() -> None:
    """`decl_line_regex`'s own regex-building line is exempt by
    construction (the `{prefix}` fragment breaks the immediate
    anchor-then-paren adjacency this shape requires), not by an
    allowlist. Checked directly against the real file."""
    text = (GATE_ROOT / "provers" / "decl_syntax.py").read_text(encoding="utf-8")
    assert not _SHAPE_RE.search(text)


def test_no_module_under_gate_compiles_the_duplicate_shape() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(GATE_ROOT.rglob("*.py"))
        if _SHAPE_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "Found a hand-rolled declaration-boundary regex outside "
        "gate.provers.decl_syntax.decl_line_regex in: "
        f"{offenders}. Use decl_line_regex (and normalize_decl_name) "
        "instead of compiling a new copy."
    )
