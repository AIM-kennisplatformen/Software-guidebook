#!/usr/bin/env python3
"""
Software Guidebook generator.

Triggered when a PR is merged in a watched repo. Reads the .claude/skills/SGB-maintainer
skill, analyses the diff, then creates or updates a full Simon Brown Software Guidebook
(14 sections) in docs/software-guidebook/ of the docs repo.

If a guidebook already exists, each section is individually checked and only
updated where the merged PR introduced relevant changes.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

from doc_builder import build_docs, DOCS_DIR, SECTION_FILENAMES
from github_client import GitHubClient, GitHubError
from skills_loader import load_skills

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

SKILLS_PATH = ".claude/skills"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate/update a Software Guidebook from a merged PR.")
    p.add_argument("--source-repo",  required=True, help="owner/repo that triggered the event")
    p.add_argument("--source-pr",    required=True, type=int, help="Merged PR number in source repo")
    p.add_argument("--docs-repo",    required=True, help="owner/repo where the guidebook lives")
    p.add_argument("--docs-branch",  default="docs", help="Base docs branch (default: docs)")
    p.add_argument("--dry-run",      action="store_true", help="Print actions without creating PRs")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    gh_token       = _require_env("GITHUB_PAT")
    openrouter_key = _require_env("OPENROUTER_API_KEY")

    gh = GitHubClient(gh_token)
    claude = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_key,
    )

    # ------------------------------------------------------------------
    # 0. Pre-flight
    # ------------------------------------------------------------------
    _preflight(gh, args.source_repo, args.source_pr)

    # ------------------------------------------------------------------
    # 1. Fetch PR metadata from the source repo
    # ------------------------------------------------------------------
    log.info("Fetching merged PR #%d from %s", args.source_pr, args.source_repo)
    pr_data = gh.get_pr(args.source_repo, args.source_pr)

    if not pr_data.get("merged"):
        log.warning(
            "PR #%d state='%s' — not merged yet. Proceeding anyway.",
            args.source_pr, pr_data.get("state"),
        )

    merged_by  = pr_data.get("merged_by") or {}
    merger     = merged_by.get("login", "unknown")
    pr_title   = pr_data["title"]
    pr_body    = pr_data.get("body") or ""
    commit_sha = pr_data.get("merge_commit_sha") or pr_data.get("head", {}).get("sha", "unknown")

    log.info("PR merged by @%s — '%s' (commit %s)", merger, pr_title, commit_sha[:8])

    pr_diff       = gh.get_pr_diff(args.source_repo, args.source_pr)
    changed_files = gh.get_pr_files(args.source_repo, args.source_pr)
    log.info("Diff: %d chars, %d changed files", len(pr_diff), len(changed_files))

    # ------------------------------------------------------------------
    # 2. Load SGB-maintainer skills from the docs repo
    # ------------------------------------------------------------------
    log.info("Loading skills from %s/%s (branch: %s)", args.docs_repo, SKILLS_PATH, args.docs_branch)
    skills_items = gh.get_directory_contents(args.docs_repo, SKILLS_PATH, ref=args.docs_branch)
    skills       = load_skills(skills_items)

    if not skills.skill_md:
        log.warning(
            "No SGB-maintainer SKILL.md found in %s/%s. "
            "Ensure .claude/skills/SGB-maintainer/SKILL.md exists on the '%s' branch.",
            args.docs_repo, SKILLS_PATH, args.docs_branch,
        )

    # ------------------------------------------------------------------
    # 3. Fetch any existing guidebook sections from the docs repo
    # ------------------------------------------------------------------
    log.info("Checking for existing guidebook in %s/%s", args.docs_repo, DOCS_DIR)
    existing_docs: dict[str, str] = {}
    for num, filename in SECTION_FILENAMES.items():
        doc_path = f"{DOCS_DIR}/{filename}"
        file_data = gh.get_file(args.docs_repo, doc_path, ref=args.docs_branch)
        if file_data:
            existing_docs[doc_path] = file_data["decoded_content"]
            log.info("  Found existing: %s", doc_path)

    is_new_guidebook = len(existing_docs) == 0
    log.info(
        "%s Software Guidebook (%d existing sections)",
        "Creating new" if is_new_guidebook else "Updating existing",
        len(existing_docs),
    )

    # ------------------------------------------------------------------
    # 4a. Generate / update guidebook sections
    # ------------------------------------------------------------------
    log.info("Generating Software Guidebook sections (14 sections)…")
    # ------------------------------------------------------------------
    # 4b. If no guidebook exists yet — fetch the full codebase snapshot
    # ------------------------------------------------------------------
    codebase_snapshot: dict[str, str] = {}
    if is_new_guidebook:
        log.info(
            "No existing guidebook found — fetching full codebase snapshot from %s@%s",
            args.source_repo, commit_sha[:8],
        )
        codebase_snapshot = gh.get_codebase_snapshot(
            repo=args.source_repo,
            ref=commit_sha,
        )
        log.info(
            "Codebase snapshot ready: %d files fetched",
            len(codebase_snapshot),
        )
    else:
        log.info(
            "Existing guidebook found — using PR diff only for targeted updates"
        )

    generated_docs = build_docs(
        claude=claude,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=pr_diff,
        changed_files=changed_files,
        skills=skills,
        existing_docs=existing_docs,
        repo_name=args.source_repo,
        commit_sha=commit_sha,
        codebase_snapshot=codebase_snapshot,
    )

    if not generated_docs:
        log.info("All sections are already up to date — no PR needed.")
        print("NO_UPDATE")
        return

    log.info("Sections to write: %d", len(generated_docs))

    if args.dry_run:
        _write_dry_run_output(generated_docs, args.source_repo, args.source_pr)
        return

    # ------------------------------------------------------------------
    # 5. Push sections to a new branch in the docs repo
    # ------------------------------------------------------------------
    doc_branch = f"auto-docs/sgb-pr-{args.source_pr}-{int(time.time())}"
    log.info("Creating branch '%s' in %s", doc_branch, args.docs_repo)
    gh.create_branch(args.docs_repo, doc_branch, from_ref=args.docs_branch)

    for doc_path, doc_content in generated_docs.items():
        log.info("  Committing %s", doc_path)
        gh.upsert_file(
            repo=args.docs_repo,
            path=doc_path,
            content=doc_content,
            branch=doc_branch,
            message=f"docs(sgb): {'create' if doc_path not in existing_docs else 'update'} "
                    f"{doc_path.split('/')[-1]} from {args.source_repo}#{args.source_pr}",
        )

    # ------------------------------------------------------------------
    # 6. Open documentation PR and tag the merger
    # ------------------------------------------------------------------
    action_word = "Create" if is_new_guidebook else "Update"
    doc_pr_title = f"docs: {action_word} Software Guidebook from {args.source_repo}#{args.source_pr}"
    doc_pr_body  = _build_pr_body(
        source_repo=args.source_repo,
        source_pr=args.source_pr,
        pr_title=pr_title,
        merger=merger,
        commit_sha=commit_sha,
        is_new=is_new_guidebook,
        generated_docs=generated_docs,
        existing_docs=existing_docs,
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
    # 7. Save metadata for the comment-listener workflow
    # ------------------------------------------------------------------
    meta = {
        "source_repo":    args.source_repo,
        "source_pr":      args.source_pr,
        "merger":         merger,
        "commit_sha":     commit_sha,
        "docs_repo":      args.docs_repo,
        "doc_branch":     doc_branch,
        "docs_branch":    args.docs_branch,
        "doc_pr_number":  doc_pr_number,
        "generated_docs": list(generated_docs.keys()),
        "is_new_guidebook": is_new_guidebook,
    }
    Path("doc_pr_meta.json").write_text(json.dumps(meta, indent=2))
    log.info("Metadata saved to doc_pr_meta.json")

    print(doc_pr_url)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preflight(gh: GitHubClient, source_repo: str, source_pr: int) -> None:
    try:
        user = gh.whoami()
        log.info("Authenticated as GitHub user: %s", user.get("login"))
    except Exception as exc:
        log.error("PAT check failed: %s", exc)
        sys.exit(1)

    try:
        repo_data = gh.check_repo_access(source_repo)
        visibility = "private" if repo_data.get("private") else "public"
        log.info("Repo '%s' is accessible (%s)", source_repo, visibility)
    except GitHubError as exc:
        log.error("Cannot access repo '%s':\n%s", source_repo, exc)
        sys.exit(1)

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
                "PR #%d is not among the 5 most recent closed PRs — double-check the number.",
                source_pr,
            )
    except Exception as exc:
        log.warning("Could not list recent PRs for diagnostics: %s", exc)


def _build_pr_body(
    source_repo: str,
    source_pr: int,
    pr_title: str,
    merger: str,
    commit_sha: str,
    is_new: bool,
    generated_docs: dict[str, str],
    existing_docs: dict[str, str],
) -> str:
    action = "created" if is_new else "updated"
    files_list = "\n".join(
        f"- `{p}` {'🆕 new' if p not in existing_docs else '✏️ updated'}"
        for p in generated_docs
    )
    source_url = f"https://github.com/{source_repo}/pull/{source_pr}"
    commit_url = f"https://github.com/{source_repo}/commit/{commit_sha}"

    return f"""## 📚 Software Guidebook {action.capitalize()}

This PR was automatically {action} after **[{source_repo}#{source_pr}]({source_url})** — _{pr_title}_ was merged.

Hey @{merger} 👋 — your merge triggered this Software Guidebook update (based on [commit `{commit_sha[:8]}`]({commit_url})). Please review the generated sections and leave comments on anything you'd like revised.

### Sections {action}

{files_list}

### How to request changes

Leave a review comment in this format:

```
/docs-update `docs/software-guidebook/01-context.md`
Please add a section about the mobile clients and update the C4 context diagram.
```

The bot will regenerate that section and push a new commit.

### Merging

Once you approve this PR it will be squash-merged into the `docs` branch automatically.

---
_Generated by the auto-docs pipeline following Simon Brown's Software Guidebook methodology_
_Source: [{source_repo}#{source_pr}]({source_url}) • Commit: [{commit_sha[:8]}]({commit_url})_
"""


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return val


def _write_dry_run_output(
    generated_docs: dict[str, str],
    source_repo: str,
    source_pr: int,
) -> None:
    """Write generated guidebook sections to dry-run-output/ for local inspection."""
    import shutil
    output_root = Path("dry-run-output")

    # Clear any previous dry-run output so results are always fresh
    if output_root.exists():
        shutil.rmtree(output_root)

    for doc_path, content in generated_docs.items():
        dest = output_root / doc_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        log.info("[DRY RUN] Written: %s (%d chars)", dest, len(content))

    # Write a manifest so you know exactly what would have been committed
    manifest_lines = [
        f"# Dry-run output",
        f"# Source: {source_repo}#{source_pr}",
        f"# {len(generated_docs)} section(s) would be committed",
        "",
    ]
    for doc_path in generated_docs:
        manifest_lines.append(f"- {doc_path}")
    (output_root / "MANIFEST.txt").write_text("\n".join(manifest_lines))

    abs_path = output_root.resolve()
    log.info("[DRY RUN] All sections written to: %s", abs_path)
    log.info("[DRY RUN] No branch, commit, or PR was created.")
    print(str(abs_path))


if __name__ == "__main__":
    main()
