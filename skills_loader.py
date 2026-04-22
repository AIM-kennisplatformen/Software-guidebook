"""
Loads skill definitions from a .claude/skills/ directory fetched via the GitHub API.
Returns a structured SkillsBundle used by the guidebook generator.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json"}


@dataclass
class SkillsBundle:
    """All content loaded from .claude/skills/SGB-maintainer/"""
    skill_md: str = ""                          # SKILL.md — master instructions
    c4_mermaid: str = ""                        # references/c4-mermaid.md
    section_templates: dict[str, str] = field(default_factory=dict)  # "00" → content
    documentation_style: str = ""              # documentation/SKILL.md
    referencing_style: str = ""                # documentation-referencing/SKILL.md
    other: dict[str, str] = field(default_factory=dict)  # anything else

    def section_template(self, number: str) -> str:
        """Return template for a section number like '01', '06', etc."""
        return self.section_templates.get(number, "")

    def all_templates_text(self) -> str:
        parts = []
        for num in sorted(self.section_templates.keys()):
            parts.append(f"### Section {num} Template\n\n{self.section_templates[num]}")
        return "\n\n---\n\n".join(parts)


def load_skills(github_items: list[dict[str, Any]]) -> SkillsBundle:
    """
    Parse GitHub content objects into a SkillsBundle.
    """
    bundle = SkillsBundle()

    for item in github_items:
        path: str = item.get("path", "")
        content: str = item.get("decoded_content", "").strip()

        if not content:
            continue

        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        # Normalise to relative path after .claude/
        parts = path.replace("\\", "/").split("/")
        try:
            idx = next(i for i, p in enumerate(parts) if p == ".claude")
            rel = "/".join(parts[idx + 1:])
        except StopIteration:
            rel = path

        log.debug("Loading skill file: %s", rel)

        # Route to the right field
        if rel == "skills/SGB-maintainer/SKILL.md":
            bundle.skill_md = content
        elif rel == "skills/SGB-maintainer/references/c4-mermaid.md":
            bundle.c4_mermaid = content
        elif rel.startswith("skills/SGB-maintainer/references/sections/"):
            fname = rel.split("/")[-1]          # e.g. "01-context.md"
            num = fname.split("-")[0]           # e.g. "01"
            bundle.section_templates[num] = content
        elif rel == "skills/documentation/SKILL.md":
            bundle.documentation_style = content
        elif rel == "skills/documentation-referencing/SKILL.md":
            bundle.referencing_style = content
        else:
            bundle.other[rel] = content

    log.info(
        "SkillsBundle loaded: skill_md=%d chars, %d section templates, c4=%d chars",
        len(bundle.skill_md), len(bundle.section_templates), len(bundle.c4_mermaid),
    )
    return bundle


def format_skills_for_prompt(skills: SkillsBundle) -> str:
    """Legacy helper — returns the master SKILL.md for simple prompts."""
    if skills.skill_md:
        return skills.skill_md
    return "(No SGB-maintainer SKILL.md found)"
