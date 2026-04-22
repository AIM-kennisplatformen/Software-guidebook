"""
Builds Markdown documentation files by calling the Claude API.

Generates:
  - docs/README.md          — high-level summary of the PR's changes
  - docs/<module>.md        — per-module reference for each changed file
"""

import logging
from pathlib import PurePosixPath
from typing import Any

from openai import OpenAI

from skills_loader import format_skills_for_prompt

log = logging.getLogger(__name__)

# OpenRouter model string for Claude Sonnet 4
MODEL = "anthropic/claude-sonnet-4-5"
MAX_TOKENS = 4096

# How many chars of diff to include before truncating (keep costs predictable)
MAX_DIFF_CHARS = 12_000


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def build_docs(
    claude: OpenAI,
    pr_title: str,
    pr_body: str,
    pr_diff: str,
    changed_files: list[dict[str, Any]],
    skills: dict[str, str],
) -> dict[str, str]:
    """
    Return a mapping of { doc_path: markdown_content } to commit.
    """
    skills_block = format_skills_for_prompt(skills)
    truncated_diff = _truncate_diff(pr_diff)

    # Group changed files by top-level module / directory
    modules = _group_by_module(changed_files)

    generated: dict[str, str] = {}

    # 1. Top-level README / summary
    generated["docs/CHANGES.md"] = _generate_summary(
        claude=claude,
        pr_title=pr_title,
        pr_body=pr_body,
        diff=truncated_diff,
        skills_block=skills_block,
    )

    # 2. Per-module docs
    for module, files in modules.items():
        doc_path = f"docs/{module}.md"
        generated[doc_path] = _generate_module_doc(
            claude=claude,
            module=module,
            files=files,
            diff=truncated_diff,
            skills_block=skills_block,
        )

    return generated


def apply_comment_revision(
    claude: OpenAI,
    filename: str,
    current_content: str,
    instructions: str,
    skills: dict[str, str],
) -> str:
    """
    Revise a single documentation file based on PR comment instructions.
    Returns the updated Markdown content.
    """
    skills_block = format_skills_for_prompt(skills)

    system = f"""You are a technical documentation writer.
You will receive the current content of a Markdown documentation file and
revision instructions from a code reviewer. Apply ONLY the requested changes
and return the complete revised Markdown file.

{skills_block}
"""

    user = f"""## File: `{filename}`

### Current content
{current_content}

### Revision instructions
{instructions}

Return the complete revised Markdown file with no preamble or explanation.
"""

    resp = claude.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _generate_summary(
    claude: OpenAI,
    pr_title: str,
    pr_body: str,
    diff: str,
    skills_block: str,
) -> str:
    system = f"""You are an expert technical documentation writer.
Given a pull-request title, description, and unified diff, write a clear
Markdown document summarising what changed and why.

Structure:
# <PR title>
## Overview
## What Changed
## Impact / Breaking Changes (omit section if none)
## Migration Notes (omit section if none)

Use plain language. Be concise. Do not reproduce raw diffs.

{skills_block}
"""

    user = f"""## PR Title
{pr_title}

## PR Description
{pr_body or '(no description provided)'}

## Diff (truncated)
```diff
{diff}
```

Write the documentation file now.
"""

    resp = claude.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def _generate_module_doc(
    claude: OpenAI,
    module: str,
    files: list[dict[str, Any]],
    diff: str,
    skills_block: str,
) -> str:
    file_list = "\n".join(f"- `{f['filename']}`" for f in files)

    system = f"""You are an expert technical documentation writer.
Given a list of changed source files and a unified diff, produce a
per-module Markdown reference document.

Structure:
# Module: <module name>
## Overview
## Files
## Key Changes
## API / Interface Reference (if applicable)
## Examples (if applicable)

{skills_block}
"""

    user = f"""## Module: `{module}`

### Changed files
{file_list}

### Diff (truncated)
```diff
{diff}
```

Write the module documentation now.
"""

    resp = claude.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def _group_by_module(changed_files: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Group files by their top-level directory (or 'root' if none)."""
    groups: dict[str, list[dict]] = {}
    for f in changed_files:
        filename = f.get("filename", "")
        parts = PurePosixPath(filename).parts
        module = parts[0] if len(parts) > 1 else "root"
        # Strip common non-code top-level dirs that don't need module docs
        if module in {".github", ".claude", "docs"}:
            continue
        groups.setdefault(module, []).append(f)
    return groups


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    half = MAX_DIFF_CHARS // 2
    return (
        diff[:half]
        + f"\n\n... [diff truncated — {len(diff) - MAX_DIFF_CHARS} chars omitted] ...\n\n"
        + diff[-half:]
    )
