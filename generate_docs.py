#!/usr/bin/env python3
"""
Auto-documentation generator.
Triggered by merged PRs in watched repos. Generates Markdown docs using
Claude + .claude/ skills, opens a documentation PR, and tags PR mergers.
"""

import os
import sys
import json
import time
import argparse
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI
from github_client import GitHubClient, GitHubError
from skills_loader import load_skills
from doc_builder import build_docs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate documentation for a merged PR.")
    p.add_argument("--source-repo",   required=True, help="owner/repo that triggered the event")
    p.add_argument("--source-pr",     required=True, type=int, help="Merged PR number in source repo")
    p.add_argument("--docs-repo",     required=True, help="owner/repo where docs live")
    p.add_argument("--docs-branch",   default="docs", help="Base branch for documentation (default: docs)")
    p.add_argument("--skills-dir",    default=".claude", help="Path to skills directory inside source repo")
    p.add_argument("--dry-run",       action="store_true", help="Print actions without creating PRs")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    gh_token        = _require_env("GITHUB_PAT")
    openrouter_key  = _require_env("OPENROUTER_API_KEY")

    gh     = GitHubClient(gh_token)
    claude = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_key,
    )

    # ------------------------------------------------------------------
    # 0. Pre-flight: verify PAT works and repo/PR are reachable
    # ------------------------------------------------------------------
    _preflight(gh, args.source_repo, args.source_pr)

    # ------------------------------------------------------------------
    # 1. Fetch context from the merged source PR
    # ------------------------------------------------------------------
    log.info("Fetching merged PR #%d from %s", args.source_pr, args.source_repo)
    pr_data = gh.get_pr(args.source_repo, args.source_pr)

    if not pr_data.get("merged"):
        log.warning(
            "PR #%d state='%s' — not merged yet. "
            "Proceeding anyway; remove warning if always running post-merge.",
            args.source_pr, pr_data.get("state"),
        )

    merged_by = pr_data.get("merged_by") or {}
    merger    = merged_by.get("login", "unknown")
    pr_title  = pr_data["title"]
    pr_body   = pr_data.get("body") or ""
    pr_diff   = gh.get_pr_diff(args.source_repo, args.source_pr)
    changed_files = gh.get_pr_files(args.source_repo, args.source_pr)

    log.info("PR merged by @%s — '%s'", merger, pr_title)

    # ------------------------------------------------------------------
    # 2. Load .claude/ skills from the docs repo (or source repo)
    # ------------------------------------------------------------------
    log.info("Loading skills from %s/%s", args.docs_repo, args.skills_dir)
    skills_content = gh.get_directory_contents(args.docs_repo, args.skills_dir, ref=args.docs_branch)
    skills         = load_skills(skills_content)
    log.info("Loaded %d skill(s): %s", len(skills), list(skills.keys()))

    # ------------------------------------------------------------------
    # 3. Generate documentation with Claude
    # ------------------------------------------------------------------
    log.info("Generating documentation via Claude...")
    generated_docs = build_docs(
        claude=claude,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=pr_diff,
        changed_files=changed_files,
        skills=skills,
    )
    log.info("Generated %d documentation file(s)", len(generated_docs))

    if args.dry_run:
        for path, content in generated_docs.items():
            print(f"\n{'='*60}\n📄 {path}\n{'='*60}\n{content[:500]}...")
        log.info("[DRY RUN] No PR created.")
        return

    # ------------------------------------------------------------------
    # 4. Push docs to a new branch in the docs repo
    # ------------------------------------------------------------------
    doc_branch = f"auto-docs/pr-{args.source_pr}-{int(time.time())}"
    log.info("Creating branch '%s' in %s", doc_branch, args.docs_repo)
    gh.create_branch(args.docs_repo, doc_branch, from_ref=args.docs_branch)

    for doc_path, doc_content in generated_docs.items():
        log.info("  Committing %s", doc_path)
        gh.upsert_file(
            repo=args.docs_repo,
            path=doc_path,
            content=doc_content,
            branch=doc_branch,
            message=f"docs: auto-generate {doc_path} from {args.source_repo}#{args.source_pr}",
        )

    # ------------------------------------------------------------------
    # 5. Open a documentation PR and tag the merger
    # ------------------------------------------------------------------
    doc_pr_title = f"docs: auto-update from {args.source_repo}#{args.source_pr} — {pr_title}"
    doc_pr_body  = _build_pr_body(
        source_repo=args.source_repo,
        source_pr=args.source_pr,
        pr_title=pr_title,
        merger=merger,
        generated_docs=generated_docs,
    )

    log.info("Opening documentation PR in %s", args.docs_repo)
    doc_pr = gh.create_pr(
        repo=args.docs_repo,
        title=doc_pr_title,
        body=doc_pr_body,
        head=doc_branch,
        base=args.docs_branch,
    )
    doc_pr_number = doc_pr["number"]
    doc_pr_url    = doc_pr["html_url"]
    log.info("Documentation PR opened: %s", doc_pr_url)

    # ------------------------------------------------------------------
    # 6. Store PR metadata for the comment-listener workflow
    # ------------------------------------------------------------------
    meta = {
        "source_repo":   args.source_repo,
        "source_pr":     args.source_pr,
        "merger":        merger,
        "docs_repo":     args.docs_repo,
        "doc_branch":    doc_branch,
        "docs_branch":   args.docs_branch,
        "doc_pr_number": doc_pr_number,
        "generated_docs": list(generated_docs.keys()),
    }
    meta_path = Path("doc_pr_meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))
    log.info("Metadata saved to %s", meta_path)

    print(doc_pr_url)   # last line = PR URL consumed by CI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return val


def _build_pr_body(
    source_repo: str,
    source_pr: int,
    pr_title: str,
    merger: str,
    generated_docs: dict[str, str],
) -> str:
    files_list = "\n".join(f"- `{p}`" for p in generated_docs)
    return f"""## 📚 Auto-generated Documentation

This PR was automatically created after **[{source_repo}#{source_pr}]** — _{pr_title}_ — was merged.

Hey @{merger} 👋 — your merge triggered this documentation update. Please review the generated docs below and leave comments on any section you'd like revised.

### Files updated
{files_list}

### How to request changes
Add a review comment in the format:
```
/docs-update <filename>
<your instructions here>
```
The bot will regenerate that section and push a new commit.

### Merging
Once you approve this PR it will be merged into the `docs` branch automatically.

---
_Generated by the auto-docs pipeline • [source PR]({f"https://github.com/{source_repo}/pull/{source_pr}"})_
"""



def _preflight(gh: GitHubClient, source_repo: str, source_pr: int) -> None:
    """Verify PAT, repo access, and that the PR number exists before doing any real work."""
    # 1. Check the PAT is valid
    try:
        user = gh.whoami()
        log.info("Authenticated as GitHub user: %s", user.get("login"))
    except Exception as exc:
        log.error("PAT check failed: %s", exc)
        sys.exit(1)

    # 2. Check repo is accessible
    try:
        repo_data = gh.check_repo_access(source_repo)
        visibility = "private" if repo_data.get("private") else "public"
        log.info("Repo '%s' is accessible (%s)", source_repo, visibility)
    except GitHubError as exc:
        log.error("Cannot access repo '%s':\n%s", source_repo, exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Repo check failed: %s", exc)
        sys.exit(1)

    # 3. Check the PR exists — list recent closed PRs so the user can see valid numbers
    try:
        recent = gh.list_recent_prs(source_repo, state="closed", per_page=5)
        recent_nums = [str(p["number"]) for p in recent]
        log.info(
            "Recent closed PRs in %s: [%s]",
            source_repo,
            ", ".join(f"#{n}" for n in recent_nums) or "none",
        )
        if recent_nums and str(source_pr) not in recent_nums:
            log.warning(
                "PR #%d is not among the 5 most recent closed PRs. "
                "Double-check this is the correct number.",
                source_pr,
            )
    except Exception as exc:
        log.warning("Could not list recent PRs for diagnostics: %s", exc)


if __name__ == "__main__":
    main()
