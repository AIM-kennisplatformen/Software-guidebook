"""
Software Guidebook builder.

Uses the SGB-maintainer skill to generate a full Simon Brown Software Guidebook
from a merged PR diff + codebase snapshot. If sections already exist in the
docs repo they are compared and only updated where content has changed.

Generation order (concrete → abstract, as defined in SKILL.md):
  09-data, 08-code, 10-infrastructure, 11-deployment, 13-decision-log,
  06-software-architecture, 07-external-interfaces, 02-functional-overview,
  01-context, 04-constraints, 03-quality-attributes, 05-principles,
  12-operation-support, 00-index  (last)
"""

import logging
from pathlib import PurePosixPath
from typing import Any

from openai import OpenAI

from skills_loader import SkillsBundle

log = logging.getLogger(__name__)

# OpenRouter model string for Claude Sonnet
MODEL = "anthropic/claude-sonnet-4-5"
MAX_TOKENS = 4096
MAX_DIFF_CHARS = 14_000

DOCS_DIR = "docs/software-guidebook"

# Generation order per SKILL.md (concrete → abstract)
SECTION_ORDER = [
    "09", "08", "10", "11", "13",
    "06", "07", "02", "01", "04",
    "03", "05", "12", "00",
]

SECTION_FILENAMES = {
    "00": "00-index.md",
    "01": "01-context.md",
    "02": "02-functional-overview.md",
    "03": "03-quality-attributes.md",
    "04": "04-constraints.md",
    "05": "05-principles.md",
    "06": "06-software-architecture.md",
    "07": "07-external-interfaces.md",
    "08": "08-code.md",
    "09": "09-data.md",
    "10": "10-infrastructure.md",
    "11": "11-deployment.md",
    "12": "12-operation-support.md",
    "13": "13-decision-log.md",
}

SECTION_NAMES = {
    "00": "Index / Table of Contents",
    "01": "Context",
    "02": "Functional Overview",
    "03": "Quality Attributes",
    "04": "Constraints",
    "05": "Principles",
    "06": "Software Architecture",
    "07": "External Interfaces",
    "08": "Code",
    "09": "Data",
    "10": "Infrastructure",
    "11": "Deployment",
    "12": "Operation & Support",
    "13": "Decision Log",
}

# Which source artefacts most influence each section
SECTION_SOURCES = {
    "01": "README, docs/, OAuth configs, API client configs, env files",
    "02": "Route handlers, CLI commands, main entry points, feature flags",
    "03": "Test configs, CI thresholds, security configs, SLA docs",
    "04": "Corporate policies, regulatory requirements, organizational mandates",
    "05": "ARCHITECTURE.md, CONTRIBUTING.md, .eslintrc, ADRs",
    "06": "src/ structure, package boundaries, module imports, Docker/k8s configs",
    "07": "OpenAPI specs, GraphQL schemas, webhook handlers",
    "08": "Directory structure, barrel exports, import patterns",
    "09": "Schema files, ORM models, migrations, database configs",
    "10": "docker-compose, k8s manifests, terraform, cloud configs",
    "11": "CI/CD pipelines, Makefile, scripts/, deploy configs",
    "12": "Monitoring configs, logging setup, incident docs",
    "13": "docs/adr/, docs/decisions/, existing ADRs, PR discussions",
    "00": "All other sections (generated last)",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_docs(
    claude: OpenAI,
    pr_title: str,
    pr_body: str,
    pr_diff: str,
    changed_files: list[dict[str, Any]],
    skills: SkillsBundle,
    existing_docs: dict[str, str],   # path → current content already in repo
    repo_name: str,
    commit_sha: str,
) -> dict[str, str]:
    """
    Generate or update all 14 Software Guidebook sections.

    Returns mapping of { doc_path: new_markdown_content }.
    Only sections that need to be created or updated are included.
    """
    diff = _truncate_diff(pr_diff)
    changed_summary = _summarise_changed_files(changed_files)
    results: dict[str, str] = {}

    # Build previously-generated sections so later sections can reference them
    generated_so_far: dict[str, str] = {}

    for section_num in SECTION_ORDER:
        filename = SECTION_FILENAMES[section_num]
        doc_path = f"{DOCS_DIR}/{filename}"
        existing = existing_docs.get(doc_path, "")

        if existing:
            # Decide whether this section needs updating
            needs_update = _section_needs_update(
                claude=claude,
                section_num=section_num,
                existing_content=existing,
                diff=diff,
                changed_summary=changed_summary,
                skills=skills,
            )
            if not needs_update:
                log.info("Section %s is up to date — skipping", filename)
                generated_so_far[section_num] = existing
                continue
            log.info("Section %s needs update", filename)
            action = "UPDATE"
        else:
            log.info("Section %s does not exist — creating", filename)
            action = "CREATE"

        content = _generate_section(
            claude=claude,
            section_num=section_num,
            action=action,
            existing_content=existing,
            pr_title=pr_title,
            pr_body=pr_body,
            diff=diff,
            changed_summary=changed_summary,
            skills=skills,
            generated_so_far=generated_so_far,
            repo_name=repo_name,
            commit_sha=commit_sha,
        )

        results[doc_path] = content
        generated_so_far[section_num] = content

    return results


def apply_comment_revision(
    claude: OpenAI,
    filename: str,
    current_content: str,
    instructions: str,
    skills: SkillsBundle,
) -> str:
    """Revise a single guidebook section based on PR review comment."""
    system = _build_system_prompt(skills)
    system += "\n\nYou are revising an existing Software Guidebook section based on reviewer feedback."

    user = (
        f"## File to revise: `{filename}`\n\n"
        f"### Current content\n\n{current_content}\n\n"
        f"### Revision instructions\n\n{instructions}\n\n"
        "Return the complete revised Markdown file. No preamble or explanation."
    )

    return _call_claude(claude, system, user)


# ---------------------------------------------------------------------------
# Section generation
# ---------------------------------------------------------------------------

def _section_needs_update(
    claude: OpenAI,
    section_num: str,
    existing_content: str,
    diff: str,
    changed_summary: str,
    skills: SkillsBundle,
) -> bool:
    """Ask Claude if the section content is still accurate given the diff."""
    section_name = SECTION_NAMES[section_num]
    sources = SECTION_SOURCES.get(section_num, "general codebase")

    system = (
        "You are a software documentation expert. "
        "Your task is to determine whether a Software Guidebook section needs updating "
        "based on a git diff. Respond with ONLY 'YES' or 'NO'."
    )

    user = (
        f"## Section: {section_num} — {section_name}\n"
        f"This section is primarily auto-discovered from: {sources}\n\n"
        f"### Changed files in this PR\n{changed_summary}\n\n"
        f"### Git diff (truncated)\n```diff\n{diff}\n```\n\n"
        f"### Current section content (first 1500 chars)\n"
        f"{existing_content[:1500]}\n\n"
        "Does this section need to be updated to reflect the changes in the diff? "
        "Answer YES or NO only."
    )

    resp = _call_claude(claude, system, user, max_tokens=10)
    return resp.strip().upper().startswith("YES")


def _generate_section(
    claude: OpenAI,
    section_num: str,
    action: str,                         # "CREATE" or "UPDATE"
    existing_content: str,
    pr_title: str,
    pr_body: str,
    diff: str,
    changed_summary: str,
    skills: SkillsBundle,
    generated_so_far: dict[str, str],
    repo_name: str,
    commit_sha: str,
) -> str:
    section_name = SECTION_NAMES[section_num]
    filename = SECTION_FILENAMES[section_num]
    template = skills.section_template(section_num)
    sources = SECTION_SOURCES.get(section_num, "general codebase")

    system = _build_system_prompt(skills)

    # Build context from already-generated sections (most relevant ones only)
    prior_context = _build_prior_context(section_num, generated_so_far)

    if action == "UPDATE":
        task_description = (
            f"UPDATE the existing `{filename}` section to reflect the changes introduced by the PR below. "
            "Keep all content that is still accurate. Only modify what has changed. "
            "Preserve any TODO markers. Return the complete updated section."
        )
        existing_block = f"### Existing content\n\n{existing_content}\n\n"
    else:
        task_description = (
            f"CREATE the `{filename}` section from scratch. "
            "Derive all content from the PR diff and changed files. "
            "If information cannot be inferred from the codebase, insert a "
            "`TODO:` placeholder rather than inventing content."
        )
        existing_block = ""

    user = (
        f"## Task: {task_description}\n\n"
        f"## Section: {section_num} — {section_name}\n"
        f"**Filename**: `docs/software-guidebook/{filename}`\n"
        f"**Auto-discover from**: {sources}\n\n"
        f"## Repository\n"
        f"Repo: `{repo_name}`\n"
        f"Commit SHA: `{commit_sha}`\n"
        f"GitHub commit URL: `https://github.com/{repo_name}/commit/{commit_sha}`\n\n"
        f"## PR that triggered this update\n"
        f"**Title**: {pr_title}\n"
        f"**Description**: {pr_body or '(none)'}\n\n"
        f"## Changed files\n{changed_summary}\n\n"
        f"## Git diff\n```diff\n{diff}\n```\n\n"
        + existing_block
        + (f"## Section template to follow\n\n{template}\n\n" if template else "")
        + (f"## Related sections already generated\n\n{prior_context}\n\n" if prior_context else "")
        + "Write ONLY the Markdown content for this section. No preamble."
    )

    return _call_claude(claude, system, user)


def _build_system_prompt(skills: SkillsBundle) -> str:
    parts = [
        "You are an expert technical writer and software architect.",
        "You create and maintain Software Guidebooks following Simon Brown's methodology.",
        "",
    ]

    if skills.skill_md:
        parts += [
            "## Master Instructions (SGB-maintainer SKILL.md)",
            skills.skill_md,
            "",
        ]
    if skills.documentation_style:
        parts += [
            "## Writing Style (documentation SKILL.md)",
            skills.documentation_style,
            "",
        ]
    if skills.referencing_style:
        parts += [
            "## Referencing Style (documentation-referencing SKILL.md)",
            skills.referencing_style,
            "",
        ]
    if skills.c4_mermaid:
        parts += [
            "## C4 Mermaid Diagram Reference",
            skills.c4_mermaid,
            "",
        ]

    return "\n".join(parts)


def _build_prior_context(
    current_section: str,
    generated_so_far: dict[str, str],
) -> str:
    """
    Include the most relevant already-generated sections as context.
    For the index (00) include all; for others include a small subset.
    """
    if current_section == "00":
        # Index needs all sections to build the TOC
        parts = []
        for num in SECTION_ORDER[:-1]:   # everything except 00 itself
            if num in generated_so_far:
                name = SECTION_NAMES[num]
                snippet = generated_so_far[num][:400]
                parts.append(f"### {num} — {name}\n{snippet}…")
        return "\n\n".join(parts)

    # For other sections, only include the immediately preceding ones
    preceding = [n for n in SECTION_ORDER if n < current_section and n in generated_so_far]
    recent = preceding[-2:] if len(preceding) > 2 else preceding
    parts = []
    for num in recent:
        name = SECTION_NAMES[num]
        snippet = generated_so_far[num][:300]
        parts.append(f"### {num} — {name}\n{snippet}…")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarise_changed_files(changed_files: list[dict[str, Any]]) -> str:
    if not changed_files:
        return "(no changed files)"
    lines = []
    for f in changed_files:
        name = f.get("filename", "?")
        status = f.get("status", "modified")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        lines.append(f"  {status:10s} +{additions}/-{deletions}  {name}")
    return "\n".join(lines)


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    half = MAX_DIFF_CHARS // 2
    return (
        diff[:half]
        + f"\n\n... [diff truncated — {len(diff) - MAX_DIFF_CHARS} chars omitted] ...\n\n"
        + diff[-half:]
    )


def _call_claude(
    claude: OpenAI,
    system: str,
    user: str,
    max_tokens: int = MAX_TOKENS,
) -> str:
    resp = claude.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()
