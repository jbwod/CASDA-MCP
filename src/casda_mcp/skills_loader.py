"""Load packaged CASDA agent skills from importlib resources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from casda_mcp.errors import CasdaError

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESCRIPTION_RE = re.compile(r"^description:\s*>-\s*\n((?:[ \t]+.+\n)+)|^description:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """Metadata for one packaged skill."""

    name: str
    description: str
    markdown: str


def _skills_root() -> resources.abc.Traversable:
    return resources.files("casda_mcp").joinpath("skills")


def _parse_description(frontmatter: str) -> str:
    match = _DESCRIPTION_RE.search(frontmatter)
    if match is None:
        return ""
    folded = match.group(1)
    if folded is not None:
        lines = [line.strip() for line in folded.splitlines() if line.strip()]
        return " ".join(lines)
    return (match.group(2) or "").strip()


def _parse_skill(markdown: str, *, directory_name: str) -> SkillInfo:
    frontmatter_match = _FRONTMATTER_RE.match(markdown)
    if frontmatter_match is None:
        raise CasdaError(
            "SKILL_INVALID",
            f"Skill '{directory_name}' is missing YAML frontmatter.",
            retryable=False,
        )
    frontmatter = frontmatter_match.group(1)
    name_match = _NAME_RE.search(frontmatter)
    if name_match is None:
        raise CasdaError(
            "SKILL_INVALID",
            f"Skill '{directory_name}' is missing a name field.",
            retryable=False,
        )
    name = name_match.group(1).strip()
    if name != directory_name:
        raise CasdaError(
            "SKILL_INVALID",
            f"Skill directory '{directory_name}' does not match name '{name}'.",
            retryable=False,
        )
    description = _parse_description(frontmatter)
    if not description:
        raise CasdaError(
            "SKILL_INVALID",
            f"Skill '{directory_name}' is missing a description.",
            retryable=False,
        )
    return SkillInfo(name=name, description=description, markdown=markdown)


def list_skills() -> list[SkillInfo]:
    """Return all packaged skills sorted by name."""
    root = _skills_root()
    if not root.is_dir():
        raise CasdaError("SKILL_NOT_FOUND", "The packaged skills directory is missing.")
    skills: list[SkillInfo] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        skill_file = entry.joinpath("SKILL.md")
        if not skill_file.is_file():
            continue
        skills.append(_parse_skill(skill_file.read_text(encoding="utf-8"), directory_name=entry.name))
    if not skills:
        raise CasdaError("SKILL_NOT_FOUND", "No packaged CASDA skills were found.")
    return skills


def get_skill(skill_name: str) -> SkillInfo:
    """Return one packaged skill by directory/name."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_name):
        raise CasdaError("VALIDATION_ERROR", "Skill name must be a lowercase hyphenated identifier.")
    skill_file = _skills_root().joinpath(skill_name, "SKILL.md")
    if not skill_file.is_file():
        raise CasdaError("SKILL_NOT_FOUND", f"Skill '{skill_name}' is not known.")
    return _parse_skill(skill_file.read_text(encoding="utf-8"), directory_name=skill_name)


def skills_index() -> dict[str, object]:
    """JSON-serialisable index of packaged skills."""
    skills = list_skills()
    return {
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "uri": f"casda://skills/{skill.name}",
            }
            for skill in skills
        ]
    }
