"""GitHubConnector — GitHub REST API（httpx）"""

import re
import httpx

from .connector import RemoteConnector


class GitHubConnector(RemoteConnector):
    """GitHub REST API 连接器"""
    API_BASE = "https://api.github.com"

    def is_configured(self) -> bool:
        return bool(self.token and self.target.url)

    def _parse_owner_repo(self) -> tuple[str, str] | None:
        """从 target.url 解析 owner/repo。
        支持: https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
        """
        url = self.target.url
        m = re.search(r"(?:https?://|git@)github\.com[:/](.+?)/(.+?)(?:\.git)?$", url)
        if not m:
            return None
        return m.group(1), m.group(2).rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gitgo",
        }

    def get_repo_info(self) -> dict:
        parsed = self._parse_owner_repo()
        if not parsed:
            return {"error": "无法从 URL 解析 owner/repo"}
        owner, repo = parsed
        try:
            r = httpx.get(
                f"{self.API_BASE}/repos/{owner}/{repo}",
                headers=self._headers(), timeout=15.0,
            )
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}", "message": r.text[:200]}
        except Exception as e:
            return {"error": str(e)}

    def list_issues(self, state: str = "open") -> list:
        parsed = self._parse_owner_repo()
        if not parsed:
            return [{"error": "无法从 URL 解析 owner/repo"}]
        owner, repo = parsed
        try:
            r = httpx.get(
                f"{self.API_BASE}/repos/{owner}/{repo}/issues",
                headers=self._headers(), timeout=15.0,
                params={"state": state, "per_page": 30},
            )
            if r.status_code == 200:
                return r.json()
            return [{"error": f"HTTP {r.status_code}", "message": r.text[:200]}]
        except Exception as e:
            return [{"error": str(e)}]

    def create_pr(self, title: str, body: str, head: str,
                  base: str = "main") -> tuple[bool, str]:
        parsed = self._parse_owner_repo()
        if not parsed:
            return False, "无法从 URL 解析 owner/repo"
        owner, repo = parsed
        try:
            r = httpx.post(
                f"{self.API_BASE}/repos/{owner}/{repo}/pulls",
                headers=self._headers(), timeout=30.0,
                json={
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                return True, data.get("html_url", "PR 已创建")
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def create_release(self, tag: str, name: str, body: str) -> tuple[bool, str]:
        parsed = self._parse_owner_repo()
        if not parsed:
            return False, "无法从 URL 解析 owner/repo"
        owner, repo = parsed
        try:
            r = httpx.post(
                f"{self.API_BASE}/repos/{owner}/{repo}/releases",
                headers=self._headers(), timeout=30.0,
                json={
                    "tag_name": tag,
                    "name": name,
                    "body": body,
                    "draft": False,
                    "prerelease": False,
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                return True, data.get("html_url", "Release 已创建")
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)
