"""测试 RemoteConnector — GitHub/GitLab 连接器（纯逻辑 + mock API）"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.models import RemoteTarget
from backend.remote import (
    GitHubConnector,
    GitLabConnector,
    RemoteConnector,
    create_connector,
)


# ── create_connector 工厂 ──────────────────────────────────

def test_create_connector_returns_none_for_bare():
    t = RemoteTarget(kind="bare", url="https://example.com/repo.git")
    assert create_connector(t) is None


def test_create_connector_returns_none_for_empty_kind():
    t = RemoteTarget(kind="", url="https://example.com/repo.git")
    assert create_connector(t) is None


def test_create_connector_returns_none_for_none_target():
    assert create_connector(None) is None


def test_create_connector_returns_github():
    t = RemoteTarget(kind="github", url="https://github.com/owner/repo.git")
    c = create_connector(t, token="gh_token")
    assert isinstance(c, GitHubConnector)
    assert c.token == "gh_token"


def test_create_connector_returns_gitlab():
    t = RemoteTarget(kind="gitlab", url="https://gitlab.com/group/proj.git")
    c = create_connector(t, token="gl_token")
    assert isinstance(c, GitLabConnector)
    assert c.token == "gl_token"


def test_create_connector_reads_github_env_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_gh_token")
    t = RemoteTarget(kind="github", url="https://github.com/owner/repo.git")
    c = create_connector(t)
    assert c.token == "env_gh_token"


def test_create_connector_reads_gitlab_env_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "env_gl_token")
    t = RemoteTarget(kind="gitlab", url="https://gitlab.com/group/proj.git")
    c = create_connector(t)
    assert c.token == "env_gl_token"


# ── GitHubConnector URL 解析 ───────────────────────────────

def test_github_parse_https():
    t = RemoteTarget(url="https://github.com/org/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    assert c._parse_owner_repo() == ("org", "repo")


def test_github_parse_ssh():
    t = RemoteTarget(url="git@github.com:org/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    assert c._parse_owner_repo() == ("org", "repo")


def test_github_parse_no_trailing_dotgit():
    t = RemoteTarget(url="https://github.com/a/b", kind="github")
    c = GitHubConnector(t, "tok")
    assert c._parse_owner_repo() == ("a", "b")


def test_github_parse_invalid_url():
    t = RemoteTarget(url="https://notgithub.com/x.git", kind="github")
    c = GitHubConnector(t, "tok")
    assert c._parse_owner_repo() is None


# ── GitLabConnector URL 解析 ──────────────────────────────

def test_gitlab_parse_https():
    t = RemoteTarget(url="https://gitlab.com/group/project.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    assert c._parse_project_path() == "group%2Fproject"


def test_gitlab_parse_ssh():
    t = RemoteTarget(url="git@gitlab.com:group/project.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    assert c._parse_project_path() == "group%2Fproject"


def test_gitlab_parse_nested_namespace():
    t = RemoteTarget(url="https://gitlab.com/g1/g2/g3/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    assert c._parse_project_path() == "g1%2Fg2%2Fg3%2Fproj"


def test_gitlab_parse_invalid_url():
    t = RemoteTarget(url="https://notgitlab.com/x.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    assert c._parse_project_path() is None


# ── is_configured ─────────────────────────────────────────

def test_is_configured_false_no_token():
    t = RemoteTarget(url="https://github.com/a/b", kind="github")
    c = GitHubConnector(t, "")
    assert not c.is_configured()


def test_is_configured_false_no_url():
    t = RemoteTarget(url="", kind="github")
    c = GitHubConnector(t, "tok")
    assert not c.is_configured()


def test_is_configured_true():
    t = RemoteTarget(url="https://github.com/a/b", kind="github")
    c = GitHubConnector(t, "tok")
    assert c.is_configured()


# ── get_repo_info 错误路径（mock httpx） ──────────────────

def test_github_get_repo_info_invalid_url():
    t = RemoteTarget(url="bad-url", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.get_repo_info()
    assert "error" in result


def test_gitlab_get_repo_info_invalid_url():
    t = RemoteTarget(url="bad-url", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.get_repo_info()
    assert "error" in result


@patch("backend.remote.github.httpx.get")
def test_github_get_repo_info_api_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = '{"message":"Bad credentials"}'
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.get_repo_info()
    assert result["error"] == "HTTP 401"


@patch("backend.remote.gitlab.httpx.get")
def test_gitlab_get_repo_info_api_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = '{"message":"Not Found"}'
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.get_repo_info()
    assert result["error"] == "HTTP 404"


# ── create_release 错误路径（mock httpx） ─────────────────

@patch("backend.remote.github.httpx.post")
def test_github_create_release_invalid_url(mock_post):
    t = RemoteTarget(url="bad-url", kind="github")
    c = GitHubConnector(t, "tok")
    ok, msg = c.create_release("v1.0", "v1.0", "body")
    assert not ok
    assert "解析" in msg


@patch("backend.remote.gitlab.httpx.post")
def test_gitlab_create_release_invalid_url(mock_post):
    t = RemoteTarget(url="bad-url", kind="gitlab")
    c = GitLabConnector(t, "tok")
    ok, msg = c.create_release("v1.0", "v1.0", "body")
    assert not ok
    assert "解析" in msg


@patch("backend.remote.github.httpx.post")
def test_github_create_release_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/owner/repo/releases/tag/v1.0"}
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    ok, msg = c.create_release("v1.0", "v1.0", "body")
    assert ok
    assert "github.com" in msg


@patch("backend.remote.gitlab.httpx.post")
def test_gitlab_create_release_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"_links": {"self": "https://gitlab.com/api/v4/projects/group%2Fproj/releases/v1.0"}}
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    ok, msg = c.create_release("v1.0", "v1.0", "body")
    assert ok
    assert "releases" in msg


# ── get_repo_info 成功路径（mock httpx） ──────────────────

@patch("backend.remote.github.httpx.get")
def test_github_get_repo_info_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.get_repo_info()
    assert result["full_name"] == "owner/repo"


@patch("backend.remote.gitlab.httpx.get")
def test_gitlab_get_repo_info_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"path_with_namespace": "group/proj", "web_url": "https://gitlab.com/group/proj"}
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.get_repo_info()
    assert result["path_with_namespace"] == "group/proj"


# ── GitHub list_issues ──────────────────────────────────────

@patch("backend.remote.github.httpx.get")
def test_github_list_issues_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"number": 1, "title": "Fix login bug", "state": "open"},
        {"number": 2, "title": "Add dark mode", "state": "closed"},
    ]
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.list_issues(state="all")
    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[1]["title"] == "Add dark mode"


def test_github_list_issues_invalid_url():
    t = RemoteTarget(url="bad-url", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.list_issues()
    assert len(result) == 1
    assert "error" in result[0]


@patch("backend.remote.github.httpx.get")
def test_github_list_issues_api_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = '{"message":"Rate limit exceeded"}'
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    result = c.list_issues()
    assert len(result) == 1
    assert "403" in result[0]["error"]


# ── GitHub create_pr ────────────────────────────────────────

@patch("backend.remote.github.httpx.post")
def test_github_create_pr_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"html_url": "https://github.com/owner/repo/pull/42"}
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    ok, msg = c.create_pr("feat: new feature", "Description here", "feature-branch")
    assert ok
    assert "pull/42" in msg


def test_github_create_pr_invalid_url():
    t = RemoteTarget(url="bad-url", kind="github")
    c = GitHubConnector(t, "tok")
    ok, msg = c.create_pr("title", "body", "head")
    assert not ok
    assert "解析" in msg


@patch("backend.remote.github.httpx.post")
def test_github_create_pr_api_error(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 422
    mock_resp.text = '{"message":"No commits between main and feature-branch"}'
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://github.com/owner/repo.git", kind="github")
    c = GitHubConnector(t, "tok")
    ok, msg = c.create_pr("title", "body", "head")
    assert not ok
    assert "422" in msg


# ── GitLab list_issues ──────────────────────────────────────

@patch("backend.remote.gitlab.httpx.get")
def test_gitlab_list_issues_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"iid": 1, "title": "Memory leak", "state": "opened"},
        {"iid": 2, "title": "Update docs", "state": "closed"},
    ]
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.list_issues(state="opened")
    assert len(result) == 2
    assert result[0]["iid"] == 1
    assert result[1]["title"] == "Update docs"


def test_gitlab_list_issues_invalid_url():
    t = RemoteTarget(url="bad-url", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.list_issues()
    assert len(result) == 1
    assert "error" in result[0]


@patch("backend.remote.gitlab.httpx.get")
def test_gitlab_list_issues_api_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = '{"message":"Unauthorized"}'
    mock_get.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    result = c.list_issues()
    assert len(result) == 1
    assert "401" in result[0]["error"]


# ── GitLab create_pr ────────────────────────────────────────

@patch("backend.remote.gitlab.httpx.post")
def test_gitlab_create_pr_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"web_url": "https://gitlab.com/group/proj/-/merge_requests/7"}
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    ok, msg = c.create_pr("feat: MR test", "MR body", "dev")
    assert ok
    assert "merge_requests/7" in msg


def test_gitlab_create_pr_invalid_url():
    t = RemoteTarget(url="bad-url", kind="gitlab")
    c = GitLabConnector(t, "tok")
    ok, msg = c.create_pr("title", "body", "head")
    assert not ok
    assert "解析" in msg


@patch("backend.remote.gitlab.httpx.post")
def test_gitlab_create_pr_api_error(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.text = '{"message":"MR already exists"}'
    mock_post.return_value = mock_resp

    t = RemoteTarget(url="https://gitlab.com/group/proj.git", kind="gitlab")
    c = GitLabConnector(t, "tok")
    ok, msg = c.create_pr("dup", "dup", "dup")
    assert not ok
    assert "409" in msg


# ── RemoteConnector ABC — 新抽象方法 ──────────────────────

def test_remote_connector_abc_enforces_list_issues():
    from backend.remote.connector import RemoteConnector
    # 验证 list_issues 是抽象方法
    assert hasattr(RemoteConnector, "list_issues")
    assert hasattr(RemoteConnector, "create_pr")


def test_github_implements_all_abstract_methods():
    t = RemoteTarget(url="https://github.com/a/b", kind="github")
    c = GitHubConnector(t, "tok")
    # 所有 RemoteConnector 抽象方法必须可调用
    assert callable(c.is_configured)
    assert callable(c.create_release)
    assert callable(c.get_repo_info)
    assert callable(c.list_issues)
    assert callable(c.create_pr)


def test_gitlab_implements_all_abstract_methods():
    t = RemoteTarget(url="https://gitlab.com/a/b", kind="gitlab")
    c = GitLabConnector(t, "tok")
    assert callable(c.is_configured)
    assert callable(c.create_release)
    assert callable(c.get_repo_info)
    assert callable(c.list_issues)
    assert callable(c.create_pr)
