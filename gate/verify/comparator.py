"""Comparator workspace/config/verdict logic (design note 14).

`leanprover/comparator` is the Lean FRO's kernel-level judge: given a
trusted Challenge module and an untrusted Solution module in one Lake
workspace, it certifies that the solution proves *exactly* the
challenge's statement, uses only permitted axioms, and replays through
the kernel. Choir maps a PR onto that model per note 14 §4:

- Challenge side = the **base SHA** tree, module-prefixed under
  `ChoirBase/` so it can coexist with the head tree in one workspace.
  A declaration a human wrote keeps its name under that rename — names
  come from namespaces, not file paths — so both environments hold the
  target under the same name, elaborated against base definitions on one
  side and head definitions on the other.

  **Two generated names do not keep it, and both read the module.**
  `Lean.mkPrivateName` mangles a private declaration to
  `_private.<module>.0.<name>`, and `Lean.Elab.mkInstanceName` appends a
  suffix derived from the *root* of the enclosing module to an anonymous
  `instance` whose type mentions no constant of the current project
  (`Lean/Elab/DeclNameGen.lean`, `mkBaseNameWithSuffix`). Either one
  differs between the two sides, so a statement reaching it can never
  match as a kernel term however correct the solution is. Both are
  detected before the run and decline it — `private_names_in_statement`
  and `suffix_divergent_instances` — because the alternative is to blame
  a contributor for the workspace generator's own rename. Renaming can
  only ever produce a spurious *difference*, never a spurious identity,
  so this costs verdicts and never trust.
- Solution side = the head tree verbatim.
- Generated roots (`Challenge.lean`, `Solution.lean`, `ChoirBase.lean`)
  and three appended `[[lean_lib]]` blocks make both sides buildable.

This module is the pure half — text generation, config mapping, verdict
classification — fully unit-testable without git, Lake, or the
comparator binary. The side-effectful half (git materialization and
invocation) lives in `comparator_cli.py`.
"""

from __future__ import annotations

import json
import re
import tomllib
from enum import Enum

from gate.verify.config import AxiomPolicy, SorryPolicy, VerifyConfig

CORE_PERMITTED_AXIOMS: tuple[str, ...] = (
    "propext",
    "Quot.sound",
    "Classical.choice",
)
"""Mathlib's standard trio — always permitted (note 14 §5)."""

SORRY_AXIOM = "sorryAx"
"""Appended under sorry policy `report`: comparator then degrades to
"right statement, kernel-consistent modulo sorry" for checkpoint PRs."""

COMPARATOR_MIN_TOOLCHAIN: tuple[int, int] = (4, 27)
"""Earliest upstream comparator tag is v4.27.0 — older toolchains have
no comparator build at all and the audit is not applicable."""

CHALLENGE_PREFIX = "ChoirBase"
"""Module prefix for the base-SHA tree inside the generated workspace."""

RESERVED_LIB_NAMES: frozenset[str] = frozenset({"Challenge", "Solution", CHALLENGE_PREFIX})
"""Lib names the generator adds; a project already using one collides."""


class ComparatorConfigError(RuntimeError):
    """Workspace generation cannot proceed (bad lakefile, name collision)."""


class Outcome(Enum):
    """Classification of a comparator run (note 14 §6)."""

    MATCH = "match"
    STATEMENT_MISMATCH = "statement-mismatch"
    ILLEGAL_AXIOM = "illegal-axiom"
    MISSING_CONSTANT = "missing-constant"
    TOOL_ERROR = "tool-error"
    SOLUTION_BUILD_FAILED = "solution-build-failed"
    SANDBOX_UNAVAILABLE = "sandbox-unavailable"


# ---------------------------------------------------------------------------
# toolchain floor
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:[.\-]|$)")


def parse_toolchain_version(text: str) -> tuple[int, int] | None:
    """Extract `(major, minor)` from a `lean-toolchain` pin.

    Accepts `leanprover/lean4:v4.31.0`, `v4.33.0-rc1`, and bare
    `4.31.0`. Returns None for anything it can't read (nightlies,
    garbage) — the caller treats that as "can't tell", not an error.
    """
    tail = text.strip().rsplit(":", 1)[-1]
    m = _VERSION_RE.match(tail)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)))


def toolchain_supported(text: str) -> bool | None:
    """Is the pinned toolchain at or above the comparator floor?

    None when the pin is unparseable (distinct from a definite no).
    """
    version = parse_toolchain_version(text)
    if version is None:
        return None
    return version >= COMPARATOR_MIN_TOOLCHAIN


# ---------------------------------------------------------------------------
# private declarations in a target's statement
# ---------------------------------------------------------------------------
#
# The challenge tree is module-prefixed under `ChoirBase/`, and Lean mangles
# a private name with the module path it was declared in
# (`_private.<Module>.0.<name>`). A public declaration therefore has the same
# name on both sides and a private one does not, so a target whose *statement*
# reaches a private declaration cannot match as a kernel term however correct
# the solution is. Detect it and decline to run rather than render a verdict
# the PR cannot act on.
#
# Text-level, like the gate's other audits. It reads the base tree only, where
# the statement is pre-stated and a contributor cannot reach.

_DECL_KEYWORDS = (
    "def",
    "theorem",
    "lemma",
    "abbrev",
    "instance",
    "structure",
    "inductive",
    "class",
    "opaque",
    "axiom",
)

_MODIFIERS = r"(?:noncomputable[ \t]+|unsafe[ \t]+|partial[ \t]+|nonrec[ \t]+)*"

_PRIVATE_DECL_RE = re.compile(
    r"^[ \t]*private[ \t]+"
    + _MODIFIERS
    + r"(?:"
    + "|".join(_DECL_KEYWORDS)
    + r")[ \t]+([A-Za-z_][A-Za-z0-9_'!?]*)",
    re.M,
)

_OPENERS = {"(": ")", "[": "]", "{": "}", "⟨": "⟩"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}


def private_decl_names(source: str) -> set[str]:
    """Names declared `private` in one Lean module, as written."""
    return set(_PRIVATE_DECL_RE.findall(source))


def _declaration_start(source: str, name: str) -> int | None:
    pattern = re.compile(
        r"^[ \t]*(?:private[ \t]+|protected[ \t]+)?"
        + _MODIFIERS
        + r"(?:"
        + "|".join(_DECL_KEYWORDS)
        + r")[ \t]+"
        + re.escape(name)
        + r"(?![A-Za-z0-9_'!?])",
        re.M,
    )
    match = pattern.search(source)
    return None if match is None else match.start()


def declaration_signature(source: str, decl: str) -> str | None:
    """The text of `decl`'s signature: its header through the end of its type.

    `decl` may be fully qualified; the module declares it under its last
    segment when it sits inside a `namespace`, so both are tried.

    Ends at the first `:=` outside brackets, which is where the body starts
    for every declaration kind the gate enumerates. `None` when the
    declaration is not found in this source.
    """
    start = None
    for name in dict.fromkeys([decl, decl.rsplit(".", 1)[-1]]):
        start = _declaration_start(source, name)
        if start is not None:
            break
    if start is None:
        return None

    depth = 0
    i = start
    end = len(source)
    while i < len(source):
        ch = source[i]
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
        elif depth == 0 and source.startswith(":=", i):
            end = i
            break
        i += 1
    return source[start:end]


def private_names_in_statement(source: str, decl: str) -> list[str]:
    """Private declarations of this module that `decl`'s statement references.

    Empty when the statement reaches none, when the declaration is absent,
    or when the module declares nothing private.
    """
    private = private_decl_names(source)
    if not private:
        return []
    signature = declaration_signature(source, decl)
    if signature is None:
        return []
    own = decl.rsplit(".", 1)[-1]
    found = {
        name
        for name in private
        if name != own
        and re.search(rf"(?<![A-Za-z0-9_'!?.]){re.escape(name)}(?![A-Za-z0-9_'!?])", signature)
    }
    return sorted(found)


# ---------------------------------------------------------------------------
# anonymous instances whose generated name reads the module root
# ---------------------------------------------------------------------------
#
# `Lean.Elab.mkInstanceName` names an anonymous `instance` from its type, and
# appends `moduleToSuffix (getMainModule).getRoot` when the type mentions no
# constant of the current project (`Lean/Elab/DeclNameGen.lean`). Under the
# `ChoirBase/` prefix that root differs, so such an instance is
# `instFoo_choirBase` on one side and `instFoo_<project>` on the other. An
# instance is reachable from a statement without being written in it — a
# `MeasurableSpace` argument is inserted by elaboration — so unlike
# `private_names_in_statement` this cannot be decided from the signature text.
#
# Mentioning *any* project declaration is enough to make Lean skip the suffix,
# so that is the test here. Erring toward "mentions one" errs toward running
# comparator, which is the direction that keeps a real mismatch visible.

_ANON_INSTANCE_RE = re.compile(
    r"^[ \t]*(?:private[ \t]+|protected[ \t]+)?(?:noncomputable[ \t]+)?"
    r"instance(?!\s*[A-Za-z_])",
    re.M,
)

_SIGNATURE_ENDS = (":=", "where")


def _header_text(source: str, start: int) -> str:
    """An instance's header: from `instance` to its body, brackets respected."""
    depth = 0
    i = start
    while i < len(source):
        ch = source[i]
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif depth == 0 and any(source.startswith(e, i) for e in _SIGNATURE_ENDS):
            break
        i += 1
    return source[start:i]


def declared_names(source: str) -> set[str]:
    """Every name this module declares, private or not, instances aside."""
    return set(_PRIVATE_DECL_RE.findall(source)) | set(
        re.findall(
            r"^[ \t]*(?:private[ \t]+|protected[ \t]+)?"
            + _MODIFIERS
            + r"(?:"
            + "|".join(k for k in _DECL_KEYWORDS if k != "instance")
            + r")[ \t]+([A-Za-z_][A-Za-z0-9_'!?]*)",
            source,
            re.M,
        )
    )


def suffix_divergent_instances(source: str, project_names: set[str]) -> list[str]:
    """Headers of this module's anonymous instances that the prefix renames.

    `project_names` is every declaration name the project defines. An
    instance header naming one of them keeps its name across the rename and
    is omitted; the rest are reported as written, since they have no name to
    report.
    """
    out = []
    for match in _ANON_INSTANCE_RE.finditer(source):
        header = _header_text(source, match.start())
        if any(
            re.search(rf"(?<![A-Za-z0-9_'!?.]){re.escape(n)}(?![A-Za-z0-9_'!?])", header)
            for n in project_names
        ):
            continue
        out.append(" ".join(header.split()))
    return out


# ---------------------------------------------------------------------------
# module names + import rewriting
# ---------------------------------------------------------------------------


def module_of_path(path: str) -> str:
    """`A/B.lean` → `A.B`."""
    return path.removesuffix(".lean").replace("/", ".")


def base_module_set(paths: list[str]) -> frozenset[str]:
    """Module names of every base-tree `.lean` file — the rewrite domain."""
    return frozenset(module_of_path(p) for p in paths)


_IMPORT_RE = re.compile(r"^(\s*)import\s+([\w.«»]+)(.*)$")


def imported_modules(source: str) -> list[str]:
    """The modules a Lean source file imports, in source order."""
    out = []
    for line in source.splitlines():
        m = _IMPORT_RE.match(line)
        if m:
            out.append(m.group(2))
    return out


def rewrite_imports(text: str, modules: frozenset[str]) -> str:
    """Prefix internal imports with `ChoirBase.`; leave everything else.

    Only whole-module matches on `import` lines are rewritten —
    `import Mathlib.Tactic` and `import Projection` (a non-internal
    name sharing a prefix with an internal one) pass through untouched.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = _IMPORT_RE.match(stripped)
        if m is not None and m.group(2) in modules:
            newline = "\n" if line.endswith("\n") else ""
            out.append(
                f"{m.group(1)}import {CHALLENGE_PREFIX}.{m.group(2)}{m.group(3)}{newline}"
            )
        else:
            out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# lakefile handling
# ---------------------------------------------------------------------------


def lean_lib_names(lakefile_text: str) -> list[str]:
    """`name` of every `[[lean_lib]]` in a `lakefile.toml` text."""
    try:
        data = tomllib.loads(lakefile_text)
    except tomllib.TOMLDecodeError as e:
        raise ComparatorConfigError(f"malformed lakefile.toml — {e}") from e
    libs = data.get("lean_lib", [])
    if not isinstance(libs, list):
        raise ComparatorConfigError("lakefile.toml lean_lib must be an array of tables")
    return [lib["name"] for lib in libs if isinstance(lib, dict) and "name" in lib]


def workspace_lakefile(base_lakefile_text: str) -> str:
    """Base lakefile text + the three generated `[[lean_lib]]` blocks.

    Appending array-of-table blocks at EOF is valid TOML as long as no
    other top-level table follows — the base text is preserved verbatim
    as the prefix, so parseability of the result is covered by tests.
    """
    existing = lean_lib_names(base_lakefile_text)
    collisions = RESERVED_LIB_NAMES.intersection(existing)
    if collisions:
        raise ComparatorConfigError(
            f"project lakefile already defines reserved lib(s): {sorted(collisions)}"
        )
    blocks = "".join(
        f'\n[[lean_lib]]\nname = "{lib}"\n'
        for lib in ("Challenge", "Solution", CHALLENGE_PREFIX)
    )
    sep = "" if base_lakefile_text.endswith("\n") else "\n"
    return (
        base_lakefile_text
        + sep
        + "\n# --- appended by gate.verify.comparator (design note 14) ---"
        + blocks
    )


# ---------------------------------------------------------------------------
# generated root modules
# ---------------------------------------------------------------------------


def _ordered_modules(root_libs: list[str], target_module: str) -> list[str]:
    mods = list(root_libs)
    if target_module not in mods:
        mods.append(target_module)
    return mods


def challenge_root(root_libs: list[str], target_module: str) -> str:
    """`Challenge.lean` — imports the *prefixed* base-tree modules."""
    return "".join(
        f"import {CHALLENGE_PREFIX}.{m}\n"
        for m in _ordered_modules(root_libs, target_module)
    )


def solution_root(root_libs: list[str], target_module: str) -> str:
    """`Solution.lean` — imports the head-tree modules verbatim."""
    return "".join(
        f"import {m}\n" for m in _ordered_modules(root_libs, target_module)
    )


def choirbase_root(root_libs: list[str]) -> str:
    """`ChoirBase.lean` — a buildable root for the prefixed lib."""
    return "".join(f"import {CHALLENGE_PREFIX}.{m}\n" for m in root_libs)


# ---------------------------------------------------------------------------
# comparator config
# ---------------------------------------------------------------------------


def permitted_axioms(
    config: VerifyConfig, *, is_reduction: bool = False
) -> tuple[str, ...]:
    """Translate base-SHA `.choir/verify.toml` policy to a whitelist.

    Core three always (Choir's introduction-based policies presume
    them); `whitelist` mode unions `allowed_axioms`. `sorryAx` is
    permitted for a declared reduction, and for any submission under
    sorry policy `report` (note 14 §5) — either way comparator degrades
    to "right statement, kernel-consistent modulo a named open
    obligation", which is exactly what a reduction claims. Under the
    default policy an ordinary submission is still refused the axiom,
    so a green gate on a plain proof PR keeps meaning unconditionally
    proved. The translation is honest but not exact — see the note for
    the net_zero false-reject caveat.
    """
    permitted = list(CORE_PERMITTED_AXIOMS)
    if config.axiom_honesty.policy is AxiomPolicy.WHITELIST:
        for name in config.axiom_honesty.allowed_axioms:
            if name not in permitted:
                permitted.append(name)
    if is_reduction or config.sorry_delta.policy is SorryPolicy.REPORT:
        permitted.append(SORRY_AXIOM)
    return tuple(permitted)


def comparator_config_json(target_decl: str, permitted: tuple[str, ...]) -> str:
    """The workspace's `config.json` for the comparator binary."""
    return (
        json.dumps(
            {
                "challenge_module": "Challenge",
                "solution_module": "Solution",
                "theorem_names": [target_decl],
                "permitted_axioms": list(permitted),
                "enable_nanoda": False,
            },
            indent=2,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# verdict classification
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS: tuple[tuple[str, Outcome], ...] = (
    # Genuine verdicts — must stay ahead of the two infra/build patterns
    # below so a real verdict is never masked by incidental build chatter
    # that happens to precede it in the transcript (a failed rebuild can
    # print "error: build failed" and *still* carry a verdict line).
    ("theorem statement do not match", Outcome.STATEMENT_MISMATCH),
    ("Illegal axiom detected", Outcome.ILLEGAL_AXIOM),
    ("Const not found in challenge", Outcome.MISSING_CONSTANT),
    ("Const not found in solution", Outcome.MISSING_CONSTANT),
    ("Const does not match between challenge and target", Outcome.MISSING_CONSTANT),
    # Infrastructure, not the worker's code. Comparator has no unsandboxed
    # mode: with COMPARATOR_LANDRUN unset it PATH-looks-up the literal string
    # "landrun" and dies, so this is also what an unset env var looks like.
    ("could not execute external process 'landrun'", Outcome.SANDBOX_UNAVAILABLE),
    # *A* build in the workspace failed. Lake prints this line for the head
    # tree, for the generated `ChoirBase/*` challenge tree, for a generated
    # lakefile error, and for a Mathlib-from-source build dying after a failed
    # `cache get` — the string itself names no library, so this outcome does
    # **not** establish that the contributor's code is the cause. The
    # attribution ("`rebuild` reports the same root cause") holds only when
    # `rebuild` is also red; see `challenge_side_build_error` and the hedge in
    # `comparator_cli._report_outcome`.
    ("error: build failed", Outcome.SOLUTION_BUILD_FAILED),
)

_CHALLENGE_ERROR_RE = re.compile(rf"{CHALLENGE_PREFIX}/\S*.*\berror:")


def challenge_side_build_error(output: str) -> bool:
    """Does the transcript show a build *error* under `ChoirBase/`?

    A cheap corroborating signal for `SOLUTION_BUILD_FAILED`: the
    `ChoirBase/` tree is generated by Choir from the base SHA, so an error
    there is Choir's own workspace-generation defect, never the
    contributor's diff.

    Requires `error:` on the same line as the path deliberately. A bare
    mention of the prefix proves nothing — Lake prints dotted module names
    (`Built ChoirBase.Proj.Basic`) on every successful build, and the
    challenge tree routinely emits `./ChoirBase/…: warning: declaration uses
    'sorry'` because a sorried target is exactly what a `prove` task's base
    looks like. False negatives are fine (the hedge in the CLI's note covers
    them); a false positive would send a real contributor failure to the
    overseer as infrastructure.
    """
    return any(_CHALLENGE_ERROR_RE.search(line) for line in output.splitlines())

FULL_TRANSCRIPT_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.TOOL_ERROR, Outcome.SOLUTION_BUILD_FAILED, Outcome.SANDBOX_UNAVAILABLE}
)
"""Outcomes where comparator never reached a verdict — the transcript is
the only actionable artifact, so it is carried in full (note 14 diagnosis,
2026-08-18).

Public: this is the seam between the pure classification half of this
module and `comparator_cli.py`'s side-effectful reporting half, which
imports it directly rather than keeping its own duplicate list (fix
round 1, 2026-08-18) — so a new outcome added to this set is picked up by
the CLI's transcript-dump guard automatically, with no second copy to
remember to update."""


def classify_output(exit_code: int, output: str) -> tuple[Outcome, str, str]:
    """Map a comparator run's exit code + combined output to an Outcome.

    The failure strings are comparator's own (Compare.lean / Axioms.lean,
    verified against the v4.31-era source) plus two signatures captured
    live from a real diagnosis session (`could not execute external
    process 'landrun'` for a missing sandbox, `error: build failed` for a
    build that died — usually but *not provably* the solution tree, see
    `_FAILURE_PATTERNS`); anything still unrecognized fails safe to
    TOOL_ERROR. The returned message is the first matching line, or the
    last non-empty output line for MATCH/TOOL_ERROR.

    The third element is the *full* captured output, populated for
    TOOL_ERROR, SOLUTION_BUILD_FAILED, and SANDBOX_UNAVAILABLE. In all
    three, comparator broke or refused rather than rendering a verdict,
    and its one-line summary is routinely uninformative (`uncaught
    exception: Child exited with 1` names neither the child nor the
    cause), so the whole transcript is the only diagnosable artifact.
    """
    if exit_code == 0:
        lines = [ln for ln in output.splitlines() if ln.strip()]
        return Outcome.MATCH, lines[-1] if lines else "comparator accepted", ""
    for pattern, outcome in _FAILURE_PATTERNS:
        for line in output.splitlines():
            if pattern in line:
                full = output if outcome in FULL_TRANSCRIPT_OUTCOMES else ""
                return outcome, line.strip(), full
    lines = [ln for ln in output.splitlines() if ln.strip()]
    message = lines[-1] if lines else f"exit code {exit_code}"
    return Outcome.TOOL_ERROR, message, output
