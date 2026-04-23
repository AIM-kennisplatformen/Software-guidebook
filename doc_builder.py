"""
Software Guidebook builder.

Two distinct modes:
  CREATE  — no guidebook exists yet. Uses the full codebase snapshot to write
             each section as a proper architecture document for the whole system.
             The triggering PR is only mentioned in the index as the commit that
             prompted the first generation.

  UPDATE  — a guidebook already exists. Uses the PR diff to determine which
             sections are stale, then updates only those sections — referencing
             both the existing section and the diff to produce a precise delta.

Generation order (concrete → abstract, per SKILL.md):
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

MODEL        = "openai/gpt-5.4"
MAX_TOKENS   = 8192
MAX_DIFF_CHARS = 14_000

DOCS_DIR = "docs/software-guidebook"

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

# Files most relevant to each section — used to select a focused subset of the
# codebase snapshot to include in the prompt for each section.
SECTION_SOURCE_PATTERNS = {
    "01": ["readme", "docs/", ".env.example", "oauth", "config"],
    "02": ["route", "handler", "controller", "command", "main", "app.", "index.", "cli"],
    "03": ["test", "ci", ".github/workflows", "jest", "vitest", "playwright", "sla"],
    "04": [],   # constraints = human knowledge; use README/docs for hints
    "05": ["architecture", "contributing", ".eslintrc", "adr", ".editorconfig"],
    "06": ["src/", "package.json", "docker-compose", "dockerfile", "tsconfig"],
    "07": ["openapi", "swagger", "graphql", "schema", "webhook", "api/"],
    "08": ["src/", "index.", "barrel", "tsconfig", "eslint"],
    "09": ["schema", "migration", "model", "entity", "prisma", "drizzle",
           "sequelize", "typeorm", "alembic", "database", "redis", "s3", "seed"],
    "10": ["docker-compose", "dockerfile", "kubernetes", "k8s", ".yaml", ".yml",
           "terraform", "pulumi", "infra", "nginx", "caddy"],
    "11": [".github/workflows", "ci", "cd", "makefile", "scripts/", "deploy",
           "release", "dockerfile"],
    "12": ["monitor", "log", "alert", "grafana", "prometheus", "sentry",
           "datadog", "newrelic", "runbook"],
    "13": ["adr", "decision", "docs/adr", "docs/decisions", "architecture"],
    "00": [],   # index uses all already-generated sections
}

# Max chars of codebase to include per section prompt (keeps cost reasonable)
MAX_CODEBASE_PER_SECTION = 80_000


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
    existing_docs: dict[str, str],
    repo_name: str,
    commit_sha: str,
    codebase_snapshot: dict[str, str],   # full repo files — empty dict if update-only
) -> dict[str, str]:
    """
    Generate or update all 14 Software Guidebook sections.
    Returns { doc_path: markdown_content } for sections that changed.
    """
    is_new = len(existing_docs) == 0
    diff   = _truncate_diff(pr_diff)
    changed_summary = _summarise_changed_files(changed_files)
    results: dict[str, str] = {}
    generated_so_far: dict[str, str] = {}

    for section_num in SECTION_ORDER:
        filename = SECTION_FILENAMES[section_num]
        doc_path = f"{DOCS_DIR}/{filename}"
        existing = existing_docs.get(doc_path, "")

        if existing and not is_new:
            # UPDATE path: check if this section is affected by the diff
            if not _section_needs_update(claude, section_num, existing, diff,
                                          changed_summary, skills):
                log.info("Section %s is up to date — skipping", filename)
                generated_so_far[section_num] = existing
                continue
            log.info("Section %s needs update", filename)
            action = "UPDATE"
        else:
            log.info("Section %s — creating from full codebase", filename)
            action = "CREATE"

        # Select the slice of the codebase most relevant to this section
        relevant_code = _select_relevant_files(
            codebase_snapshot, section_num, MAX_CODEBASE_PER_SECTION
        ) if codebase_snapshot else {}

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
            relevant_code=relevant_code,
            is_new=is_new,
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
    system = _build_system_prompt(skills)
    system += "\n\nYou are revising an existing Software Guidebook section based on reviewer feedback."
    user = (
        f"## File: `{filename}`\n\n"
        f"### Current content\n\n{current_content}\n\n"
        f"### Revision instructions\n\n{instructions}\n\n"
        "Return the complete revised Markdown. No preamble."
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
    system = (
        "You are a software documentation expert. "
        "Determine whether a Software Guidebook section needs updating based on a git diff. "
        "Reply with ONLY 'YES' or 'NO'."
    )
    user = (
        f"## Section: {section_num} — {SECTION_NAMES[section_num]}\n\n"
        f"### Changed files\n{changed_summary}\n\n"
        f"### Git diff\n```diff\n{diff}\n```\n\n"
        f"### Current section (first 1500 chars)\n{existing_content[:1500]}\n\n"
        "Does this section need updating? Answer YES or NO."
    )
    resp = _call_claude(claude, system, user, max_tokens=10)
    return resp.strip().upper().startswith("YES")


def _generate_section(
    claude: OpenAI,
    section_num: str,
    action: str,
    existing_content: str,
    pr_title: str,
    pr_body: str,
    diff: str,
    changed_summary: str,
    skills: SkillsBundle,
    generated_so_far: dict[str, str],
    repo_name: str,
    commit_sha: str,
    relevant_code: dict[str, str],
    is_new: bool,
) -> str:
    section_name = SECTION_NAMES[section_num]
    filename     = SECTION_FILENAMES[section_num]
    template     = skills.section_template(section_num)
    system       = _build_system_prompt(skills)
    prior        = _build_prior_context(section_num, generated_so_far)

    commit_url = f"https://github.com/{repo_name}/commit/{commit_sha}"
    repo_url   = f"https://github.com/{repo_name}"

    # ---- Assemble the codebase block ----
    if relevant_code:
        code_lines = [
            "## Codebase snapshot\n",
            f"Repository: [{repo_name}]({repo_url}) at commit [`{commit_sha[:8]}`]({commit_url})\n",
            f"({len(relevant_code)} files shown — selected as most relevant to this section)\n",
        ]
        for path, content in relevant_code.items():
            file_url = f"{repo_url}/blob/{commit_sha}/{path}"
            code_lines.append(f"\n### [`{path}`]({file_url})\n\n```\n{content[:6000]}\n```")
        codebase_block = "\n".join(code_lines)
    else:
        codebase_block = ""

    # ---- Craft the task instruction ----
    if action == "CREATE" and is_new:
        task = (
            f"CREATE the `{filename}` section of a Software Guidebook for the repository "
            f"[{repo_name}]({repo_url}).\n\n"
            "IMPORTANT: Write this section as a complete architecture document covering the "
            "ENTIRE codebase, not just the triggering PR. The PR is only the event that "
            "prompted the first generation of this guidebook — the guidebook must describe "
            "the system as a whole.\n\n"
            "Use the codebase snapshot below as your primary source. "
            "Derive everything from what is actually present in the files. "
            "If something cannot be determined from the code, insert a `TODO:` placeholder "
            "rather than inventing content.\n\n"
            "For the index (00), include the commit SHA and GitHub link as the reference point."
        )
        diff_block = (
            f"## PR that triggered first generation (context only)\n"
            f"This PR is NOT the subject of the documentation — it is merely the trigger.\n"
            f"**Title**: {pr_title}\n"
            f"**Commit**: [{commit_sha[:8]}]({commit_url})\n"
        )
    else:
        task = (
            f"UPDATE the existing `{filename}` section to reflect changes introduced by the PR. "
            "Keep all accurate content. Only modify what has changed. "
            "Preserve TODO markers. Return the complete updated section."
        )
        diff_block = (
            f"## PR changes to incorporate\n"
            f"**Title**: {pr_title}\n"
            f"**Description**: {pr_body or '(none)'}\n\n"
            f"### Changed files\n{changed_summary}\n\n"
            f"### Git diff\n```diff\n{diff}\n```\n"
        )

    user = "\n\n".join(filter(None, [
        f"## Task\n{task}",
        f"## Section: {section_num} — {section_name}",
        f"**File**: `docs/software-guidebook/{filename}`",
        f"**Repository**: [{repo_name}]({repo_url})",
        diff_block,
        codebase_block,
        (f"## Existing content to update\n\n{existing_content}" if action == "UPDATE" else ""),
        (f"## Section template\n\n{template}" if template else ""),
        (f"## Previously generated sections (for cross-referencing)\n\n{prior}" if prior else ""),
        "Write ONLY the Markdown content. No preamble or explanation.",
    ]))

    return _call_claude(claude, system, user)


# ---------------------------------------------------------------------------
# Codebase file selection
# ---------------------------------------------------------------------------

def _select_relevant_files(
    snapshot: dict[str, str],
    section_num: str,
    max_chars: int,
) -> dict[str, str]:
    """
    Return a subset of the codebase snapshot most relevant to the given section.
    For section 00 (index) return nothing — it uses generated_so_far instead.
    """
    if section_num == "00":
        return {}

    patterns = SECTION_SOURCE_PATTERNS.get(section_num, [])

    def score(path: str) -> int:
        p = path.lower()
        # Direct pattern match → high priority
        if patterns and any(pat in p for pat in patterns):
            return 0
        # Config / manifest files are always useful
        fname = path.split("/")[-1].lower()
        if fname in {"package.json", "docker-compose.yml", "dockerfile",
                     "readme.md", "makefile", "requirements.txt",
                     "pyproject.toml", "go.mod", "cargo.toml"}:
            return 1
        # Shallow files (entry points) are useful for most sections
        if path.count("/") <= 2:
            return 2
        return 3

    ranked = sorted(snapshot.keys(), key=score)

    selected: dict[str, str] = {}
    total = 0
    for path in ranked:
        content = snapshot[path]
        if total + len(content) > max_chars:
            break
        selected[path] = content
        total += len(content)

    return selected


# ---------------------------------------------------------------------------
# Prompt assembly helpers
# ---------------------------------------------------------------------------

def _build_system_prompt(skills: SkillsBundle) -> str:
    parts = [
        "You are an expert technical writer and software architect.",
        "You create Software Guidebooks following Simon Brown's methodology from "
        "'Software Architecture for Developers Vol. 2'.",
        "",
    ]
    if skills.skill_md:
        parts += ["## Master Instructions (SGB-maintainer SKILL.md)", skills.skill_md, ""]
    if skills.documentation_style:
        parts += ["## Writing Style", skills.documentation_style, ""]
    if skills.referencing_style:
        parts += ["## Referencing Style", skills.referencing_style, ""]
    if skills.c4_mermaid:
        parts += ["## C4 Mermaid Diagram Reference", skills.c4_mermaid, ""]
    return "\n".join(parts)


def _build_prior_context(current: str, generated: dict[str, str]) -> str:
    if current == "00":
        parts = []
        for num in SECTION_ORDER[:-1]:
            if num in generated:
                parts.append(f"### {num} — {SECTION_NAMES[num]}\n{generated[num][:500]}…")
        return "\n\n".join(parts)
    preceding = [n for n in SECTION_ORDER if n < current and n in generated]
    recent = preceding[-2:]
    return "\n\n".join(
        f"### {n} — {SECTION_NAMES[n]}\n{generated[n][:300]}…" for n in recent
    )


def _summarise_changed_files(changed_files: list[dict[str, Any]]) -> str:
    if not changed_files:
        return "(no changed files)"
    return "\n".join(
        f"  {f.get('status','modified'):10s} +{f.get('additions',0)}/-{f.get('deletions',0)}  {f.get('filename','?')}"
        for f in changed_files
    )


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    half = MAX_DIFF_CHARS // 2
    return (
        diff[:half]
        + f"\n\n... [diff truncated — {len(diff) - MAX_DIFF_CHARS} chars omitted] ...\n\n"
        + diff[-half:]
    )


def _call_claude(claude: OpenAI, system: str, user: str, max_tokens: int = MAX_TOKENS) -> str:
    resp = claude.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()
