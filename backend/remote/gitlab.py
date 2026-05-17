"""GitLabConnector — GitLab REST API v4（httpx）"""

import re
from urllib.parse import quote

import httpx

from .connector import RemoteConnector


class GitLabConnector(RemoteConnector):
    """GitLab REST API v4 连接器"""
    API_BASE = "https://gitlab.com/api/v4"

    def is_configured(self) -> bool:
        return bool(self.token and self.target.url)

    def _parse_project_path(self) -> str | None:
        """从 target.url 解析 GitLab project path。
        支持: https://gitlab.com/group/project.git 或 git@gitlab.com:group/project.git
        返回 URL-encoded path（如 group%2Fproject）。
        """
        url = self.target.url
        m = re.search(r"(?:https?://|git@)gitlab\.com[:/](.+?)(?:\.git)?$", url)
        if not m:
            return None
        path = m.group(1).rstrip("/")
        return quote(path, safe="")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "gitgo",
        }

    def get_repo_info(self) -> dict:
        proj = self._parse_project_path()
        if not proj:
            return {"error": "无法从 URL 解析 GitLab project path"}
        try:
            r = httpx.get(
                f"{self.API_BASE}/projects/{proj}",
                headers=self._headers(), timeout=15.0,
            )
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}", "message": r.text[:200]}
        except Exception as e:
            return {"error": str(e)}

    def create_release(self, tag: str, name: str, body: str) -> tuple[bool, str]:
        proj = self._parse_project_path()
        if not proj:
            return False, "无法从 URL 解析 GitLab project path"
        try:
            r = httpx.post(
                f"{self.API_BASE}/projects/{proj}/releases",
                headers=self._headers(), timeout=30.0,
                json={
                    "tag_name": tag,
                    "name": name,
                    "description": body,
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                return True, data.get("_links", {}).get("self", "Release 已创建")
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    # ── Issue / MR ─────────────────────────────────────────

    def list_issues(self, state: str = "opened") -> list:
        proj = self._parse_project_path()
        if not proj:
            return [{"error": "无法从 URL 解析 GitLab project path"}]
        try:
            r = httpx.get(
                f"{self.API_BASE}/projects/{proj}/issues",
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
        """GitLab 称为 Merge Request (MR)。"""
        proj = self._parse_project_path()
        if not proj:
            return False, "无法从 URL 解析 GitLab project path"
        try:
            r = httpx.post(
                f"{self.API_BASE}/projects/{proj}/merge_requests",
                headers=self._headers(), timeout=30.0,
                json={
                    "title": title,
                    "description": body,
                    "source_branch": head,
                    "target_branch": base,
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                return True, data.get("web_url", "MR 已创建")
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)
