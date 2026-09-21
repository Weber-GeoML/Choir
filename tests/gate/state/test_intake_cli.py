"""Tests for `gate.state.intake_cli`.

Focus is on the payload shape (what the workflow consumes) and on the
Markdown comment rendering. Heavy validation logic is already covered by
test_intake.py.
"""

from __future__ import annotations

import json
import textwrap

from gate.state import intake_cli

VALID_BODY = textwrap.dedent(
    """\
    ---
    choir-task-version: 1
    type: prove
    target_file: MyProj/Foo.lean
    target_decl: MyProj.Foo.add_comm
    project_ref:
      repo: org/myproj
      commit: 1a2b3c4d
      toolchain: leanprover/lean4:v4.x.y
    deps: [12]
    blueprint_ref: blueprint/foo.tex#add_comm
    ---

    ## Statement
    theorem MyProj.Foo.add_comm ...
    """
)

def test_build_payload_success() -> None:
    payload = intake_cli.build_payload(VALID_BODY, repo="org/myproj")
    assert payload["status"] == "ok"
    assert "task accepted" in payload["comment"]
    assert "MyProj.Foo.add_comm" in payload["comment"]
    assert "#12" in payload["comment"]
    assert "choir/available" in payload["add_labels"]
    assert "choir/type:prove" in payload["add_labels"]
    assert "choir/task" in payload["add_labels"]
    assert "choir/invalid" in payload["remove_labels"]
    assert "errors" not in payload


def test_build_payload_golf_task_accepted() -> None:
    # `type: golf` rides the generic non-review intake branch. Pinned
    # because the golf playbook relies on it (spec 2026-07-18).
    payload = intake_cli.build_payload(
        VALID_BODY.replace("type: prove", "type: golf"), repo="org/myproj"
    )
    assert payload["status"] == "ok"
    assert "choir/type:golf" in payload["add_labels"]
    assert "choir/available" in payload["add_labels"]


def test_build_payload_success_with_no_deps() -> None:
    body = VALID_BODY.replace("deps: [12]", "deps: []")
    payload = intake_cli.build_payload(body, repo="org/myproj")
    assert payload["status"] == "ok"
    assert "_(none)_" in payload["comment"]


def test_build_payload_no_blueprint_ref() -> None:
    body = VALID_BODY.replace("blueprint_ref: blueprint/foo.tex#add_comm\n", "")
    payload = intake_cli.build_payload(body, repo="org/myproj")
    assert payload["status"] == "ok"
    # Two _(none)_ entries: deps (was [12], still has #12) and blueprint_ref.
    # Wait, we kept deps: [12], so only blueprint_ref is none.
    assert payload["comment"].count("_(none)_") == 1


def test_build_payload_error() -> None:
    # Missing required field.
    body = VALID_BODY.replace("type: prove\n", "")
    payload = intake_cli.build_payload(body, repo="org/myproj")
    assert payload["status"] == "error"
    assert "rejected" in payload["comment"].lower()
    assert "choir/invalid" in payload["add_labels"]
    assert "choir/available" in payload["remove_labels"]
    assert "errors" in payload
    assert any(e["code"] == "missing_field" for e in payload["errors"])
    assert any(e["field"] == "type" for e in payload["errors"])


def test_build_payload_repo_mismatch() -> None:
    payload = intake_cli.build_payload(VALID_BODY, repo="other-org/different-repo")
    assert payload["status"] == "error"
    assert any(e["code"] == "repo_mismatch" for e in payload["errors"])


def test_main_writes_json_to_stdout(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY, encoding="utf-8")
    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_handles_error_body(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    body_file = tmp_path / "body.txt"
    body_file.write_text("not even a YAML front-matter here\n", encoding="utf-8")
    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    # Exits 0 even on parse errors — the *workflow* uses the JSON to decide
    # what to do. The CLI's job is to report, not to fail.
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert any(e["code"] == "no_frontmatter" for e in payload["errors"])


# ---------------------------------------------------------------------------
# Idempotency (record_hash + existing_hashes + should_post_comment)
# ---------------------------------------------------------------------------


def test_success_payload_includes_record_hash_and_marker() -> None:
    payload = intake_cli.build_payload(VALID_BODY, repo="org/myproj")
    assert payload["status"] == "ok"
    h = payload["record_hash"]
    # Hash is hex, fixed length.
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)
    # Marker appears in the comment so future runs can detect it.
    assert f"choir-intake-ack:{h}" in payload["comment"]


def test_first_intake_should_post() -> None:
    payload = intake_cli.build_payload(
        VALID_BODY, repo="org/myproj", existing_comments=[]
    )
    assert payload["should_post_comment"] is True


def test_re_intake_with_same_record_should_skip() -> None:
    first = intake_cli.build_payload(VALID_BODY, repo="org/myproj")
    h = first["record_hash"]
    # Simulate the prior comment appearing in the issue history.
    existing = [first["comment"]]
    second = intake_cli.build_payload(
        VALID_BODY, repo="org/myproj", existing_comments=existing
    )
    assert second["record_hash"] == h
    assert second["should_post_comment"] is False


def test_re_intake_with_changed_record_should_post() -> None:
    first = intake_cli.build_payload(VALID_BODY, repo="org/myproj")
    existing = [first["comment"]]
    # Change target_decl — different canonical record → different hash.
    changed = VALID_BODY.replace(
        "target_decl: MyProj.Foo.add_comm",
        "target_decl: MyProj.Foo.add_assoc",
    )
    second = intake_cli.build_payload(
        changed, repo="org/myproj", existing_comments=existing
    )
    assert second["record_hash"] != first["record_hash"]
    assert second["should_post_comment"] is True


def test_hash_is_stable_under_prose_changes() -> None:
    # The prose after the front-matter doesn't affect the canonical record,
    # so the hash must not change. This is the load-bearing property for
    # idempotency on issues.edited.
    h1 = intake_cli.build_payload(VALID_BODY, repo="org/myproj")["record_hash"]
    body_with_more_prose = VALID_BODY + "\n\n## Notes\n\nExtra context here.\n"
    h2 = intake_cli.build_payload(body_with_more_prose, repo="org/myproj")[
        "record_hash"
    ]
    assert h1 == h2


def test_hash_changes_when_deps_order_changes() -> None:
    # deps order is semantically meaningful (it's a sequence); reordering
    # produces a different hash. (If we ever decide order doesn't matter,
    # this test pins the current contract for explicit re-examination.)
    body_a = VALID_BODY.replace("deps: [12]", "deps: [12, 34]")
    body_b = VALID_BODY.replace("deps: [12]", "deps: [34, 12]")
    h_a = intake_cli.build_payload(body_a, repo="org/myproj")["record_hash"]
    h_b = intake_cli.build_payload(body_b, repo="org/myproj")["record_hash"]
    assert h_a != h_b


def test_hash_changes_when_any_field_changes() -> None:
    base = intake_cli.build_payload(VALID_BODY, repo="org/myproj")["record_hash"]
    variants = [
        VALID_BODY.replace("type: prove", "type: golf"),
        VALID_BODY.replace("commit: 1a2b3c4d", "commit: 9a9a9a9a"),
        VALID_BODY.replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Bar.lean"),
        VALID_BODY.replace(
            "toolchain: leanprover/lean4:v4.x.y",
            "toolchain: leanprover/lean4:v4.y.z",
        ),
        VALID_BODY.replace(
            "blueprint_ref: blueprint/foo.tex#add_comm",
            "blueprint_ref: blueprint/foo.tex#add_assoc",
        ),
    ]
    for v in variants:
        h = intake_cli.build_payload(v, repo="org/myproj")["record_hash"]
        assert h != base


def test_unrelated_existing_comments_dont_match() -> None:
    payload = intake_cli.build_payload(
        VALID_BODY,
        repo="org/myproj",
        existing_comments=[
            "Some unrelated maintainer comment.",
            "Another contributor said hi.",
            "choir-intake-ack:0000deadbeef0000",  # marker but different hash
        ],
    )
    assert payload["should_post_comment"] is True


def test_error_payload_always_posts() -> None:
    # Even if the same error keeps occurring, the maintainer should keep
    # seeing it. We don't dedup error comments in v0.
    bad = VALID_BODY.replace("type: prove\n", "")
    payload = intake_cli.build_payload(
        bad, repo="org/myproj", existing_comments=["whatever"]
    )
    assert payload["status"] == "error"
    assert payload["should_post_comment"] is True


def test_existing_hashes_extracts_markers() -> None:
    comments = [
        "Hello world\n<!-- choir-intake-ack:abc123def456 -->\nbody",
        "Another:\n<!-- choir-intake-ack:0123456789abcdef -->\nrest",
        "No marker here",
    ]
    hashes = intake_cli.existing_hashes(comments)
    assert hashes == {"abc123def456", "0123456789abcdef"}


# ---------------------------------------------------------------------------
# Prover-profile resolution (design note 12 §5) — `--prover` flag and the
# default-branch `.choir/project.toml` fallback.
# ---------------------------------------------------------------------------


def _write_project_toml(workspace, project_body: str) -> None:  # type: ignore[no-untyped-def]
    (workspace / ".choir").mkdir(exist_ok=True)
    (workspace / ".choir" / "project.toml").write_text(
        f"[project]\n{project_body}", encoding="utf-8"
    )


def test_main_prover_flag_rejects_extension_outside_profile(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY, encoding="utf-8")  # target_file: MyProj/Foo.lean

    rc = intake_cli.main(
        ["--body-file", str(body_file), "--repo", "org/myproj", "--prover", "rocq"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert any(e["code"] == "target_file_extension" for e in payload["errors"])


def test_main_prover_flag_accepts_matching_extension(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY.replace("Foo.lean", "Foo.v"), encoding="utf-8")

    rc = intake_cli.main(
        ["--body-file", str(body_file), "--repo", "org/myproj", "--prover", "rocq"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_main_default_prover_still_accepts_lean(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    # No --prover flag, no .choir/project.toml at cwd -> lean4 default;
    # a .lean target_file passes as before this change.
    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY, encoding="utf-8")

    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_main_reads_prover_from_cwd_project_toml_rocq_rejects_lean(
    tmp_path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    # Simulates the workflow's default-branch checkout: cwd's
    # .choir/project.toml selects "rocq", so a .lean target_file (this
    # project's a Lean holdover, or a copy-paste mistake) is rejected.
    _write_project_toml(tmp_path, 'prover = "rocq"\n')
    monkeypatch.chdir(tmp_path)

    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY, encoding="utf-8")  # target_file: MyProj/Foo.lean

    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert any(e["code"] == "target_file_extension" for e in payload["errors"])


def test_main_reads_prover_from_cwd_project_toml_rocq_accepts_v(
    tmp_path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    _write_project_toml(tmp_path, 'prover = "rocq"\n')
    monkeypatch.chdir(tmp_path)

    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY.replace("Foo.lean", "Foo.v"), encoding="utf-8")

    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_build_payload_passes_allowed_extensions_through() -> None:
    body = VALID_BODY.replace("Foo.lean", "Foo.v")
    payload = intake_cli.build_payload(body, repo="org/myproj", allowed_extensions=(".lean",))
    assert payload["status"] == "error"
    assert any(e["code"] == "target_file_extension" for e in payload["errors"])

    payload_ok = intake_cli.build_payload(
        body, repo="org/myproj", allowed_extensions=(".v", ".lean")
    )
    assert payload_ok["status"] == "ok"


def test_main_accepts_existing_comments_file(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    body_file = tmp_path / "body.txt"
    body_file.write_text(VALID_BODY, encoding="utf-8")

    # First run — generate the comment and capture its hash.
    rc = intake_cli.main(["--body-file", str(body_file), "--repo", "org/myproj"])
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    h = first["record_hash"]

    # Second run with prior comment present.
    comments_file = tmp_path / "comments.txt"
    # gh --jq '.comments[].body' yields one body per line; preserve that shape.
    comments_file.write_text(
        f"unrelated comment\n<!-- choir-intake-ack:{h} -->\n",
        encoding="utf-8",
    )
    rc = intake_cli.main(
        [
            "--body-file",
            str(body_file),
            "--repo",
            "org/myproj",
            "--existing-comments-file",
            str(comments_file),
        ]
    )
    assert rc == 0
    second = json.loads(capsys.readouterr().out)
    assert second["should_post_comment"] is False
