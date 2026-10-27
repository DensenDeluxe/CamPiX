"""Project skills stay discoverable by Cursor and Claude Code."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "campix-installer",
    "campix-persistence",
    "campix-runtime",
    "campix-security",
    "campix-ui",
    "campix-verify",
)


def _frontmatter(text):
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    parts = text.split("---", 2)
    assert len(parts) == 3 and parts[2].lstrip()
    return parts[1]


def test_project_skills_resolve_for_cursor_and_claude_code():
    agents = ROOT / ".agents" / "skills"
    claude = ROOT / ".claude" / "skills"
    found = sorted(path.name for path in agents.iterdir() if path.is_dir())
    assert found == list(SKILLS)
    for name in SKILLS:
        skill = agents / name / "SKILL.md"
        frontmatter = _frontmatter(skill.read_text(encoding="utf-8"))
        assert f"name: {name}\n" in frontmatter
        assert "description:" in frontmatter
        link = claude / name
        assert link.is_symlink(), name
        assert link.readlink().as_posix() == f"../../.agents/skills/{name}"
        assert link.joinpath("SKILL.md").is_file()
