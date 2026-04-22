#!/usr/bin/env python3
"""
process_comments.py

Polls a documentation PR for:
  1. /docs-update <filename> instructions in review comments → regenerates that file.
  2. An approved review → merges the PR into the docs branch.

Designed to be run on a schedule (e.g. every 5 min) or triggered by a
`pull_request_review` / `pull_request_review_comment` GitHub event.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from openai import OpenAI

from doc_builder import apply_comment_revision
from github_client import GitHubClient
from skills_loader import load_skills

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Marker we add to bot comments so we don't re-process them
BOT_MARKER = "<!-- auto-docs-bot -->"
DOCS_UPDATE_RE = re.compile(
    r"^/docs-update\s+(`?)(?P<filename>[^\s`]+)\1\s*\n(?P<instructions>.+)",
    re.DOTALL | re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--meta-file", default="doc_pr_meta.json", help="Path to metadata JSON")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    gh_token        = _require_env("GITHUB_PAT")
    openrouter_key  = _require_env("OPENROUTER_API_KEY")

    meta = _load_meta(args.meta_file)
    docs_repo     = meta["docs_repo"]
    doc_branch    = meta["doc_branch"]
    docs_branch   = meta["docs_branch"]
    doc_pr_number = meta["doc_pr_number"]
    source_repo   = meta["source_repo"]
    source_pr     = meta["source_pr"]

    gh     = GitHubClient(gh_token)
    claude = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_key,
    )

    # Load skills (same source as generation step)
    skills_content = gh.get_directory_contents(docs_repo, ".claude", ref=docs_branch)
    skills         = load_skills(skills_content)

    # ------------------------------------------------------------------
    # 1. Process /docs-update commands in review comments
    # ------------------------------------------------------------------
    review_comments = gh.get_pr_comments(docs_repo, doc_pr_number)
    issue_comments  = gh.get_issue_comments(docs_repo, doc_pr_number)
    all_comments    = review_comments + issue_comments

    for comment in all_comments:
        body = comment.get("body", "")

        # Skip our own bot comments
        if BOT_MARKER in body:
            continue

        match = DOCS_UPDATE_RE.search(body.strip())
        if not match:
            continue

        filename     = match.group("filename")
        instructions = match.group("instructions").strip()
        comment_id   = comment["id"]
        comment_url  = comment.get("html_url", "")

        log.info("Processing /docs-update for '%s' (comment %s)", filename, comment_id)

        # Validate filename is one we generated
        if filename not in meta.get("generated_docs", []):
            _post_reply(
                gh, docs_repo, doc_pr_number, args.dry_run,
                f"{BOT_MARKER}\n⚠️ Unknown file `{filename}`. "
                f"Available files: {', '.join(f'`{f}`' for f in meta['generated_docs'])}",
            )
            continue

        # Fetch current content from the branch
        file_data = gh.get_file(docs_repo, filename, ref=doc_branch)
        if not file_data:
            log.warning("File %s not found on branch %s", filename, doc_branch)
            continue

        current_content = file_data["decoded_content"]

        # Regenerate with Claude
        revised = apply_comment_revision(
            claude=claude,
            filename=filename,
            current_content=current_content,
            instructions=instructions,
            skills=skills,
        )

        if not args.dry_run:
            gh.upsert_file(
                repo=docs_repo,
                path=filename,
                content=revised,
                branch=doc_branch,
                message=f"docs: revise {filename} per review comment ({comment_url})",
            )
            _post_reply(
                gh, docs_repo, doc_pr_number, args.dry_run,
                f"{BOT_MARKER}\n✅ `{filename}` has been updated per your instructions. "
                f"Please re-review.",
            )
            log.info("Updated %s and posted confirmation comment.", filename)
        else:
            log.info("[DRY RUN] Would update %s with revised content.", filename)

    # ------------------------------------------------------------------
    # 2. Auto-merge if PR is approved
    # ------------------------------------------------------------------
    if _is_approved(gh, docs_repo, doc_pr_number):
        log.info("PR #%d is approved — merging into '%s'.", doc_pr_number, docs_branch)
        if not args.dry_run:
            gh.merge_pr(docs_repo, doc_pr_number, merge_method="squash")
            log.info("PR #%d merged successfully.", doc_pr_number)
        else:
            log.info("[DRY RUN] Would merge PR #%d.", doc_pr_number)
    else:
        log.info("PR #%d is not yet approved — skipping merge.", doc_pr_number)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_approved(gh: GitHubClient, repo: str, pr_number: int) -> bool:
    """Return True if at least one APPROVED review exists and no CHANGES_REQUESTED."""
    reviews = gh.get_pr_reviews(repo, pr_number)
    # Use the latest review per reviewer
    latest: dict[str, str] = {}
    for r in reviews:
        reviewer = r["user"]["login"]
        latest[reviewer] = r["state"]
    states = set(latest.values())
    return "APPROVED" in states and "CHANGES_REQUESTED" not in states


def _post_reply(gh: GitHubClient, repo: str, pr_number: int, dry_run: bool, body: str) -> None:
    if dry_run:
        log.info("[DRY RUN] Would post comment: %s", body[:120])
        return
    gh.create_issue_comment(repo, pr_number, body)


def _load_meta(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        log.error("Metadata file not found: %s", path)
        sys.exit(1)
    return json.loads(p.read_text())


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return val


if __name__ == "__main__":
    main()
