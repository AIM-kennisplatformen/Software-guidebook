# Auto-Documentation Pipeline — Setup Guide

## Architecture

```
Source Repo (watched)                  Docs Repo
─────────────────────                  ──────────────────────────────────────
PR merged
  └─► notify-docs.yml ──dispatch──►  auto-docs-generate.yml
                                           │
                                           ├─ Reads .claude/ skills
                                           ├─ Calls Claude API → Markdown
                                           ├─ Pushes to auto-docs/pr-<N> branch
                                           └─ Opens PR  (@mentions merger)
                                                │
                                    Reviewer leaves /docs-update comment
                                                │
                                           auto-docs-review.yml
                                                ├─ Regenerates that file
                                                └─ Posts ✅ confirmation
                                                │
                                    Reviewer approves
                                                │
                                           auto-docs-review.yml
                                                └─ Squash-merges into `docs`
```

---

## Repository Layout

```
docs-repo/
├── .claude/                    ← skill files consumed by the generator
│   ├── SKILL.md                ← global documentation style guide
│   └── commands/
│       └── document.md         ← per-command prompt overrides
├── docs/                       ← generated documentation lands here
├── generate_docs.py
├── process_comments.py
├── doc_builder.py
├── github_client.py
├── skills_loader.py
├── requirements.txt
└── .github/
    └── workflows/
        ├── auto-docs-generate.yml   ← runs in DOCS repo
        └── auto-docs-review.yml     ← runs in DOCS repo
```

```
source-repo/  (one per watched repo)
└── .github/
    └── workflows/
        └── notify-docs.yml
```

---

## One-time Setup

### 1. Create the docs repository

```bash
gh repo create my-org/my-docs --private
git checkout -b docs && git push -u origin docs
```

### 2. Secrets & variables (docs repo)

| Secret | Value |
|--------|-------|
| `GITHUB_PAT` | Classic PAT — scopes: `repo`, `pull_requests` |
| `OPENROUTER_API_KEY` | Your OpenRouter API key (used to call Claude via OpenRouter) |

### 3. Secrets & variables (each source repo)

| Secret | Value |
|--------|-------|
| `DOCS_REPO_PAT` | Same PAT (needs `repo` scope on the docs repo) |

| Variable | Value |
|----------|-------|
| `DOCS_REPO` | `my-org/my-docs` |

### 4. Copy workflow files

**Source repos** — add `.github/workflows/notify-docs.yml`

**Docs repo** — add all Python files + both generate/review workflows.

### 5. Create the `docs` base branch

The documentation PR base branch must exist:

```bash
# In the docs repo
git checkout --orphan docs
git commit --allow-empty -m "init docs branch"
git push origin docs
```

---

## Skills (.claude/ directory)

Place Markdown files in `.claude/` inside the **docs repo** to control
documentation style and structure.

**Example `.claude/SKILL.md`:**
```markdown
# Documentation Style Guide

- Use active voice.
- Lead every file with a one-sentence summary.
- Code examples must be runnable.
- Group related functions under H3 headings.
```

Skills are loaded fresh on every generation run, so you can iterate on
style without redeploying.

---

## Requesting Documentation Changes

In any review comment on the generated PR, write:

```
/docs-update `docs/my-module.md`
Please add a section explaining the retry logic and add a usage example.
```

The bot will regenerate that file and push a new commit within minutes.

---

## Dry-run Testing

```bash
export GITHUB_PAT=ghp_...
export ANTHROPIC_API_KEY=sk-ant-...

python generate_docs.py \
  --source-repo my-org/my-service \
  --source-pr 42 \
  --docs-repo my-org/my-docs \
  --docs-branch docs \
  --dry-run
```
