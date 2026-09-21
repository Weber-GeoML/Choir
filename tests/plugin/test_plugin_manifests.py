"""Structural checks for the agent plugin (Claude Code + Codex).

One shared plugin/ directory serves both harnesses through harness-specific
manifests (.claude-plugin/ + .codex-plugin/, and the two root marketplace
files). These tests keep the manifests parseable and version-synced, the
plugin version explicit (an omitted version makes every repo commit a
plugin update for installed users), and every repo path the skills
reference existing, so a future rename breaks this suite, not a user's
onboarding.
"""

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
CODEX_MARKETPLACE = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
PLUGIN_DIR = REPO_ROOT / "plugin"
PLUGIN_MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
CODEX_PLUGIN_MANIFEST = PLUGIN_DIR / ".codex-plugin" / "plugin.json"

SKILLS_DIR = PLUGIN_DIR / "skills"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Repo paths a skill may reference, relative to the Choir checkout root.
REPO_PATH_RE = re.compile(r"(?:scripts/[\w.-]+\.sh|docs/[A-Z_]+\.md)")


def _skill_files() -> list[Path]:
    files = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    assert files, "no skills found under plugin/skills/"
    return files


def _frontmatter(skill_md: Path) -> dict:
    match = FRONTMATTER_RE.match(skill_md.read_text(encoding="utf-8"))
    assert match, f"{skill_md} lacks YAML frontmatter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), f"{skill_md} frontmatter is not a mapping"
    return data


def test_marketplace_manifest_parses_with_one_choir_plugin() -> None:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert data["name"] == "choir"
    (entry,) = data["plugins"]
    assert entry["name"] == "choir"


def test_marketplace_source_resolves_to_plugin_dir() -> None:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    (entry,) = data["plugins"]
    source = (REPO_ROOT / entry["source"]).resolve()
    assert source == PLUGIN_DIR.resolve()
    assert source.is_dir()


def test_plugin_manifest_has_explicit_semver_version() -> None:
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "choir"
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["version"]), (
        "plugin.json must pin an explicit version; omitting it makes every "
        "commit SHA a new plugin version for installed users"
    )


def test_codex_marketplace_source_resolves_to_plugin_dir() -> None:
    data = json.loads(CODEX_MARKETPLACE.read_text(encoding="utf-8"))
    assert data["name"] == "choir"
    (entry,) = data["plugins"]
    assert entry["name"] == "choir"
    assert entry["source"]["source"] == "local"
    source = (REPO_ROOT / entry["source"]["path"]).resolve()
    assert source == PLUGIN_DIR.resolve()
    assert source.is_dir()


def test_codex_manifest_mirrors_claude_manifest() -> None:
    # One plugin, two harness manifests: same identity, same version.
    # A lone bump would ship different plugin versions to the two harnesses.
    claude = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    codex = json.loads(CODEX_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert codex["name"] == claude["name"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", codex["version"])
    assert codex["version"] == claude["version"]
    assert codex["description"] == claude["description"]


def test_skill_frontmatter_name_matches_directory() -> None:
    # Codex requires `name`; Claude Code derives it from the directory.
    # Keeping them equal keeps one skill identity across both harnesses.
    for skill_md in _skill_files():
        fm = _frontmatter(skill_md)
        assert fm.get("name") == skill_md.parent.name, f"{skill_md}"


def test_public_copy_follows_public_pitch_rule() -> None:
    # Public copy never namedrops prior art (AGENTS.md rule). Covers the
    # manifests, the skill prose, and the README — the surfaces a user sees.
    public_texts = [
        MARKETPLACE,
        CODEX_MARKETPLACE,
        PLUGIN_MANIFEST,
        CODEX_PLUGIN_MANIFEST,
        REPO_ROOT / "README.md",
        *_skill_files(),
    ]
    for path in public_texts:
        text = path.read_text(encoding="utf-8").lower()
        for banned in ("repoprover", "symphony", "gradienthq"):
            assert banned not in text, f"{path}: mentions {banned}"


def test_expected_skills_present() -> None:
    names = {p.parent.name for p in _skill_files()}
    assert names == {"formalize", "join"}


def test_skill_frontmatter_has_description_and_argument_hint() -> None:
    for skill_md in _skill_files():
        fm = _frontmatter(skill_md)
        assert str(fm.get("description", "")).strip(), f"{skill_md}: empty description"
        assert str(fm.get("argument-hint", "")).strip(), f"{skill_md}: empty argument-hint"


def test_skill_descriptions_name_choir() -> None:
    # Free-form prose triggers skills via description matching; naming
    # Choir in the description is what makes "… with Choir" reliable.
    for skill_md in _skill_files():
        fm = _frontmatter(skill_md)
        assert "choir" in str(fm["description"]).lower(), f"{skill_md}"


def test_referenced_repo_paths_exist() -> None:
    for skill_md in _skill_files():
        refs = set(REPO_PATH_RE.findall(skill_md.read_text(encoding="utf-8")))
        assert refs, f"{skill_md} references no scripts/docs"
        for ref in refs:
            assert (REPO_ROOT / ref).exists(), f"{skill_md} references missing {ref}"
