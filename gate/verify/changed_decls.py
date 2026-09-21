"""Changed-declaration autodetection for `verify-trust-report` (design
note 12 §4.1).

Git-diff-driven, profile-generic: find files changed since a PR's base
SHA that match the effective prover's `file_extensions`, extract
declarations at both base and head, and target whichever declarations
are new at head or whose extracted statement differs from base. A
declaration whose extractor returns `None` on *both* sides (structures,
`axiom` decls, anything the syntactic `extract_statement` v0 can't
carve out — see `gate/provers/lean4.py`'s extractor docstring) falls
back to comparing the raw source line the declaration starts on, so
those decl kinds still get flagged when they change.

lean4 names are qualified via `gate.provers.lean4.qualify_decl_names`'s
text-level `namespace`/`section` stack tracker; rocq/isabelle use the
surface names `gate.indexer.extract.extract_declarations` already
finds (design note 12 §4.1: "rocq/isabelle use surface names in v0").

Deterministic ordering (file path, then line within file), capped at
`cap` targets — `detect_changed_decls` reports whether the cap
truncated the result so the caller can note it.

**Enumeration reports every declaration; this module only probes the
ones a prover could resolve.** `decl_keywords` does double duty —
enumeration, which legitimately covers anonymous and parameterized
headers, and probe target discovery, which cannot. An anonymous lean4
`example : T := …` enumerates under the literal name `:`, and
`#print axioms :` is an elaboration error, so `_is_probeable_name`
filters targets by identifier shape here rather than narrowing
enumeration upstream (which would be a worse fail-open — see
`gate.provers.decl_syntax.normalize_decl_name`). Each unprobeable
target cost a wasted prover invocation and a `cap` slot, so enough
`example`s in a diff pushed real declarations out of the report.

Pure git-plumbing + text extraction — no build, no prover invocation.
`gate/verify/trust_report_cli.py`'s auto mode is the caller; probing
the detected targets is `gate.provers.trust.collect_trust_report`'s
job, one declaration at a time, so one unresolvable name never kills
the run.

Note on `git mv`: `_changed_files` uses `git diff --name-only`, which
does no rename detection, so a moved file reads as a plain add at its
new path and every declaration in it is probed as new. Over-reports,
never under-reports; the cost is cap budget.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gate.indexer.extract import extract_declarations
from gate.provers.base import ProverProfile
from gate.provers.lean4 import qualify_decl_names
from gate.verify.prover_dispatch import filter_files_by_profile
from gate.verify.style import comment_stripped_lines

DEFAULT_CAP = 50

# A probe target is a name the prover itself has to resolve
# (`#print axioms <name>`, `Print Assumptions <name>.`, `thm <name>`),
# so a declaration whose enumerated name is not a referenceable
# identifier cannot be probed at all.
#
# A positive identifier shape, not a blacklist of the spellings seen so
# far: enumeration deliberately reports a non-name token rather than
# dropping the declaration (see `normalize_decl_name`), and those tokens
# are many — `:` for an anonymous lean4 `example`, `{α` / `(n` for a
# binder-first header, `'a` / `('a` for a parameterized isabelle
# `datatype` / `record`.
#
# Two deliberate over-rejections, cheap because this check is ADVISORY
# and a wrong reject costs one report line: lean4 guillemet identifiers
# (`«foo»`) and machine-inaccessible names carrying `✝`. Neither appears
# in hand-written source.
_PROBEABLE_NAME_RE = re.compile(r"[^\W\d][\w'!?]*(?:\.[^\W\d][\w'!?]*)*\Z")


def _is_probeable_name(name: str) -> bool:
    """True iff `name` is a shape a prover could be asked to resolve."""
    return _PROBEABLE_NAME_RE.match(name) is not None


class ChangedDeclsError(RuntimeError):
    """Git plumbing failed while detecting changed declarations.

    An infrastructure error — never raised for a normal "nothing
    changed" outcome, which is a valid, empty result instead.
    """


@dataclass(frozen=True)
class ChangedDecl:
    """One declaration the diff-driven detector picked as a probe target."""

    name: str  # qualified where the profile supports it (lean4)
    file: str  # workspace-relative path
    module: str  # import target derived from the file path


@dataclass(frozen=True)
class _Decl:
    """Internal: one extracted declaration, plus its raw source line."""

    name: str
    line: int
    raw_line: str


def _changed_files(workspace: Path, base_sha: str, profile: ProverProfile) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, "--", "."],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ChangedDeclsError(
            f"git diff --name-only {base_sha} failed: {detail}"
        )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return filter_files_by_profile(files, profile)


def _show_at_base(workspace: Path, base_sha: str, rel_path: str) -> str:
    """Contents of `rel_path` at `base_sha`, or `''` if absent (new at head)."""
    result = subprocess.run(
        ["git", "show", f"{base_sha}:{rel_path}"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _module_for(profile: ProverProfile, rel_path: str) -> str:
    """Derive the probe's import target from a changed file's path.

    lean4: the workspace-relative path minus its extension, slashes to
    dots. rocq/isabelle: the file stem (dirpath mappings from
    `_CoqProject` are a validation-slice concern, design note 12 §4.1).
    """
    if profile.name == "lean4":
        stem = rel_path
        for ext in profile.file_extensions:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        return stem.replace("/", ".")
    return Path(rel_path).stem


def _extract(text: str, rel_path: str, profile: ProverProfile) -> list[_Decl]:
    """Declarations in `text`, named per the profile (qualified for lean4).

    `qualify_decl_names` runs on the comment-blanked copy of `text`
    (`comment_stripped_lines`, 1:1 aligned so `loc.line` still indexes
    the real file), not the raw text: it tracks `namespace`/`section`/
    `end` on a text-level stack, so a `namespace Foo` or a bare `end`
    sitting inside a `/- … -/` block would otherwise push or pop that
    stack and requalify every declaration after it under a namespace
    that does not exist. Symmetric while the comment is present on
    both sides of a diff, but a worker who deletes such a comment
    changes every following declaration's *key* — so a name qualified
    one way at base and another way at head no longer matches
    `base_by_name`, and `_is_target` reads an untouched declaration as
    new. `gate.verify.statement_immutability._index_side` has the same
    fix for the same reason (statement-immutability hardening final
    round, F2); this was the report's "one other raw-text caller."

    **The `lean4` gate below is load-bearing — do not "generalize" it to
    `profile.qualify_decl_names`.** As of round 4 that profile hook is
    non-`None` on all three provers, so the substitution now type-checks
    and would look like a tidy-up. It is not: what isabelle and rocq
    supply is a *disambiguation key* (an enclosing-scope path, see
    `gate.provers.decl_syntax.qualify_by_scope`), deliberately not the
    name the prover resolves. That is fine where the key is only matched
    against another key computed the same way, which is what
    `statement_immutability` does — and wrong here, because these names
    become trust-report PROBE TARGETS (`Print Assumptions <name>.`,
    `thm <name>`) that the prover itself has to resolve. A
    scope-path-qualified name that ignores `Include` or locale
    interpretation would simply not resolve, turning a clean probe into
    an unresolved-entry report line. lean4's `qualify_decl_names` is
    real namespace qualification, which is why it alone is used.
    """
    locations = extract_declarations(text, rel_path, profile=profile)
    qualified = (
        qualify_decl_names("\n".join(comment_stripped_lines(text, profile=profile)))
        if profile.name == "lean4"
        else {}
    )
    lines = text.splitlines()
    out: list[_Decl] = []
    for loc in locations:
        name = qualified.get(loc.line, loc.name)
        raw_line = lines[loc.line - 1] if 0 < loc.line <= len(lines) else ""
        out.append(_Decl(name=name, line=loc.line, raw_line=raw_line))
    return out


def _group_by_name(decls: list[_Decl]) -> dict[str, list[_Decl]]:
    """Index declarations by name, keeping every one that shares a name.

    A plain `{d.name: d for d in decls}` silently drops all but the
    last declaration of a repeated name, which is what let `_is_target`
    compare the wrong pair (round 5, F1).
    """
    grouped: dict[str, list[_Decl]] = {}
    for decl in decls:
        grouped.setdefault(decl.name, []).append(decl)
    return grouped


def _is_target(
    decl: _Decl,
    base_by_name: dict[str, list[_Decl]],
    head_by_name: dict[str, list[_Decl]],
    base_text: str,
    head_text: str,
    profile: ProverProfile,
) -> bool:
    """True iff `decl` is new at head or its statement/line differs from base.

    Both indexes map a name to **every** declaration carrying it, not
    to one of them (round 5, F1). A name repeated on either side is
    targeted unconditionally rather than compared: `extract_statement`
    resolves a name to its *first* match in the text it is handed, so
    for a repeated name every same-named declaration would be compared
    against the same one pair and the answer would be about a
    declaration other than `decl`. Under-reporting there is silent —
    a worker retypes the second of two same-named declarations, the
    first pair compares equal, and nothing is probed — so the ambiguous
    case resolves toward probing.

    That is not a hypothetical shape once lean4 is not the prover:
    `_extract` qualifies only lean4 names, so two `Theorem c`s in
    sibling rocq `Module`s (or two isabelle `lemma c`s in sibling
    `locale`s) really do share one surface name in one file. On lean4
    the qualified name makes a genuine repeat illegal source.
    """
    base_matches = base_by_name.get(decl.name, [])
    if not base_matches:
        return True
    if len(base_matches) > 1 or len(head_by_name.get(decl.name, [])) > 1:
        return True
    base_decl = base_matches[0]
    head_stmt = profile.extract_statement(head_text, decl.name)
    base_stmt = profile.extract_statement(base_text, decl.name)
    if head_stmt is None and base_stmt is None:
        return decl.raw_line != base_decl.raw_line
    return head_stmt != base_stmt


def detect_changed_decls(
    workspace: Path, base_sha: str, profile: ProverProfile, *, cap: int = DEFAULT_CAP
) -> tuple[list[ChangedDecl], bool]:
    """Diff `base_sha` against `workspace`'s checkout for probe targets.

    Changed files come from `git diff --name-only <base_sha> -- .` run
    in `workspace`, filtered to `profile.file_extensions`; files
    deleted at head are skipped (nothing left to probe). Per remaining
    file, declarations are extracted at head (the worktree file) and at
    base (`git show <base_sha>:<path>`, tolerating a file new at head —
    `''` base content, so every head declaration counts as new). A
    declaration is a target iff its name is probeable
    (`_is_probeable_name` — an anonymous or parameterized header whose
    enumerated name no prover can resolve is skipped, round 5 F1) and
    it's absent at base, or shares its name with another declaration on
    either side, or its extracted statement (or, when the extractor
    can't determine one on either side, the raw declaration line)
    differs from base.

    Returns `(targets, capped)`: `targets` is deterministically ordered
    (file path, then line within file) and truncated to `cap` entries;
    `capped` is `True` when truncation happened.

    Raises `ChangedDeclsError` for a git-plumbing failure (bad
    `base_sha`, not a git repository, ...) — never for a normal
    zero-target outcome.
    """
    changed_files = _changed_files(workspace, base_sha, profile)

    ordered: list[tuple[str, int, ChangedDecl]] = []
    for rel_path in changed_files:
        head_path = workspace / rel_path
        if not head_path.is_file():
            continue  # deleted at head — nothing to probe
        try:
            head_text = head_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        base_text = _show_at_base(workspace, base_sha, rel_path)

        head_decls = _extract(head_text, rel_path, profile)
        base_decls = _extract(base_text, rel_path, profile)
        base_by_name = _group_by_name(base_decls)
        head_by_name = _group_by_name(head_decls)
        module = _module_for(profile, rel_path)

        for decl in head_decls:
            # Not probeable, so not a target — round 5, F1. Skipped
            # before `_is_target` so an anonymous declaration never
            # consumes a probe subprocess or a `cap` slot.
            if not _is_probeable_name(decl.name):
                continue
            if _is_target(
                decl, base_by_name, head_by_name, base_text, head_text, profile
            ):
                ordered.append(
                    (
                        rel_path,
                        decl.line,
                        ChangedDecl(name=decl.name, file=rel_path, module=module),
                    )
                )

    ordered.sort(key=lambda item: (item[0], item[1]))
    targets = [item[2] for item in ordered]
    capped = len(targets) > cap
    return targets[:cap], capped
