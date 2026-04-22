"""
Loads skill definitions from a .claude/ directory fetched via the GitHub API.

Expected structure inside .claude/:
  ├── SKILL.md              # global instructions
  ├── commands/
  │   └── *.md              # named command prompts
  └── *.md                  # any other skill files

Returns a dict: { relative_path: file_content }
"""

import logging
from typing import Any

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json"}


def load_skills(github_items: list[dict[str, Any]]) -> dict[str, str]:
    """
    Parse a flat list of GitHub content objects (as returned by
    GitHubClient.get_directory_contents) into a path→content mapping.

    Only text files with supported extensions are included.
    """
    skills: dict[str, str] = {}

    for item in github_items:
        path: str = item.get("path", "")
        content: str = item.get("decoded_content", "")

        if not content:
            log.debug("Skipping empty file: %s", path)
            continue

        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in SUPPORTED_EXTENSIONS:
            log.debug("Skipping unsupported file type: %s", path)
            continue

        # Use path relative to .claude/ as the key
        # e.g. ".claude/commands/document.md" → "commands/document.md"
        parts = path.split("/")
        try:
            claude_idx = next(i for i, p in enumerate(parts) if p == ".claude")
            rel_path = "/".join(parts[claude_idx + 1 :])
        except StopIteration:
            rel_path = path

        skills[rel_path] = content.strip()
        log.debug("Loaded skill: %s (%d chars)", rel_path, len(content))

    log.info("Skills loaded: %s", list(skills.keys()))
    return skills


def format_skills_for_prompt(skills: dict[str, str]) -> str:
    """
    Render loaded skills into a single block suitable for injection into
    a Claude system prompt.
    """
    if not skills:
        return "(No .claude/ skills found — using default documentation style.)"

    parts: list[str] = ["## Available Skills\n"]
    for path, content in skills.items():
        parts.append(f"### `{path}`\n\n{content}\n")

    return "\n".join(parts)
