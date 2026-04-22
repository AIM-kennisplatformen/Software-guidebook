"""
Thin wrapper around the GitHub REST API using only requests.
Includes clear diagnostic errors for common failures (404, 401, 403).
"""

import base64
import logging
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.github.com"


class GitHubError(Exception):
    """Raised with a human-readable message for common GitHub API failures."""


class GitHubClient:
    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # ------------------------------------------------------------------
    # Auth / connectivity check
    # ------------------------------------------------------------------

    def whoami(self) -> dict:
        """Return the authenticated user. Useful for verifying the PAT works."""
        return self._get("/user")

    def check_repo_access(self, repo: str) -> dict:
        """Return repo metadata if accessible. Raises GitHubError on 404/403."""
        return self._get(f"/repos/{repo}")

    # ------------------------------------------------------------------
    # PRs
    # ------------------------------------------------------------------

    def get_pr(self, repo: str, pr_number: int) -> dict:
        """
        Fetch a single PR. Raises GitHubError with a diagnostic message
        if the repo or PR number cannot be found.
        """
        try:
            return self._get(f"/repos/{repo}/pulls/{pr_number}")
        except requests.HTTPError as exc:
            status = exc.response.status_code
            self._diagnose_pr_error(repo, pr_number, status)
            raise  # unreachable but satisfies type checkers

    def get_pr_diff(self, repo: str, pr_number: int) -> str:
        resp = self._session.get(
            f"{_BASE}/repos/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        self._raise_for_status(resp, context=f"diff for {repo}#{pr_number}")
        return resp.text

    def get_pr_files(self, repo: str, pr_number: int) -> list[dict]:
        return self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/files")

    def list_recent_prs(self, repo: str, state: str = "closed", per_page: int = 10) -> list[dict]:
        """List recent PRs — useful for discovering valid PR numbers."""
        return self._get(
            f"/repos/{repo}/pulls",
            params={"state": state, "per_page": per_page, "sort": "updated", "direction": "desc"},
        )

    def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        return self._post(
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )

    def get_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        return self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/comments")

    def get_issue_comments(self, repo: str, pr_number: int) -> list[dict]:
        return self._get_paginated(f"/repos/{repo}/issues/{pr_number}/comments")

    def create_issue_comment(self, repo: str, pr_number: int, body: str) -> dict:
        return self._post(
            f"/repos/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    def merge_pr(self, repo: str, pr_number: int, merge_method: str = "squash") -> dict:
        return self._put(
            f"/repos/{repo}/pulls/{pr_number}/merge",
            json={"merge_method": merge_method},
        )

    def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        return self._get_paginated(f"/repos/{repo}/pulls/{pr_number}/reviews")

    # ------------------------------------------------------------------
    # Commits (fallback when event fires with a commit SHA / push event)
    # ------------------------------------------------------------------

    def get_commit(self, repo: str, sha: str) -> dict:
        return self._get(f"/repos/{repo}/commits/{sha}")

    def get_commit_diff(self, repo: str, sha: str) -> str:
        resp = self._session.get(
            f"{_BASE}/repos/{repo}/commits/{sha}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        self._raise_for_status(resp, context=f"diff for {repo}@{sha}")
        return resp.text

    # ------------------------------------------------------------------
    # Branches & files
    # ------------------------------------------------------------------

    def create_branch(self, repo: str, branch: str, from_ref: str) -> None:
        sha = self._get(f"/repos/{repo}/git/ref/heads/{from_ref}")["object"]["sha"]
        self._post(
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> Optional[dict]:
        try:
            data = self._get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
            data["decoded_content"] = base64.b64decode(data["content"]).decode()
            return data
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def upsert_file(self, repo: str, path: str, content: str, branch: str, message: str) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        payload: dict[str, Any] = {"message": message, "content": encoded, "branch": branch}
        existing = self.get_file(repo, path, ref=branch)
        if existing:
            payload["sha"] = existing["sha"]
        self._put(f"/repos/{repo}/contents/{path}", json=payload)

    def get_directory_contents(self, repo: str, path: str, ref: str = "HEAD") -> list[dict]:
        try:
            items = self._get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
            result: list[dict] = []
            for item in items:
                if item["type"] == "file":
                    file_data = self.get_file(repo, item["path"], ref=ref)
                    if file_data:
                        result.append(file_data)
                elif item["type"] == "dir":
                    result.extend(self.get_directory_contents(repo, item["path"], ref=ref))
            return result
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                log.warning("Directory '%s' not found in %s@%s", path, repo, ref)
                return []
            raise

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _diagnose_pr_error(self, repo: str, pr_number: int, status: int) -> None:
        if status == 401:
            raise GitHubError(
                "GitHub API returned 401 Unauthorized.\n"
                "→ Check that GITHUB_PAT is set and not expired.\n"
                "→ Test it with: curl -H 'Authorization: Bearer <PAT>' https://api.github.com/user"
            )
        if status == 403:
            raise GitHubError(
                f"GitHub API returned 403 Forbidden for '{repo}'.\n"
                "→ Your PAT may lack 'repo' scope, or the repo requires SSO authorisation.\n"
                "→ Visit https://github.com/settings/tokens to check scopes / SSO."
            )
        if status == 404:
            raise GitHubError(
                f"GitHub API returned 404 Not Found for PR #{pr_number} in '{repo}'.\n"
                "\n"
                "Possible causes and fixes:\n"
                "\n"
                f"  1. PR #{pr_number} does not exist in that repo.\n"
                "     → Check valid PR numbers at:\n"
                f"       https://github.com/{repo}/pulls\n"
                "\n"
                "  2. The repo is private and your PAT cannot see it.\n"
                "     → Ensure the PAT has 'repo' scope and SSO is authorised:\n"
                "       https://github.com/settings/tokens\n"
                "\n"
                "  3. The workflow fired with a push/run number instead of a PR number.\n"
                "     → In notify-docs.yml confirm you use:\n"
                "       github.event.pull_request.number  ✓\n"
                "       (NOT github.run_number or github.event.number)\n"
                "\n"
                "  4. Typo in the repository name.\n"
                f"     → Received repo='{repo}' — verify owner and repo name casing."
            )
        raise GitHubError(f"GitHub API returned unexpected status {status} for {repo}#{pr_number}.")

    def _raise_for_status(self, resp: requests.Response, context: str = "") -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = ""
            try:
                body = resp.json().get("message", "")
            except Exception:
                pass
            label = f" [{context}]" if context else ""
            raise requests.HTTPError(
                f"HTTP {resp.status_code}{label} — GitHub: '{body}'",
                response=resp,
            ) from exc

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = self._session.get(f"{_BASE}{path}", params=params)
        self._raise_for_status(resp, context=path)
        return resp.json()

    def _get_paginated(self, path: str) -> list:
        results: list = []
        url: Optional[str] = f"{_BASE}{path}?per_page=100"
        while url:
            resp = self._session.get(url)
            self._raise_for_status(resp, context=path)
            results.extend(resp.json())
            url = resp.links.get("next", {}).get("url")
        return results

    def _post(self, path: str, json: dict) -> Any:
        resp = self._session.post(f"{_BASE}{path}", json=json)
        self._raise_for_status(resp, context=path)
        return resp.json()

    def _put(self, path: str, json: dict) -> Any:
        resp = self._session.put(f"{_BASE}{path}", json=json)
        self._raise_for_status(resp, context=path)
        return resp.json()
