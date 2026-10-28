"""Real managed Git transfers and executable-extension provenance boundaries."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.adapters.local_git_runner import LocalGitRunner
from backend.core.loop.git_execution import protected_git, GitBoundaryError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows protected Git backend")


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


@pytest.fixture
def case(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    home = root / "operator"
    home.mkdir()
    for key in tuple(os.environ):
        if key.upper().startswith(("GIT_", "GCM_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    global_config = home / ".gitconfig"
    global_config.write_text('[user]\nname = Operator\nemail = operator@gitgo.invalid\n', encoding="utf8")
    repo, remote = root / "client", root / "remote.git"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "push.default", "current")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "business.txt").write_bytes(b"base\n")
    git(repo, "add", "business.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "base")
    git(root, "init", "--bare", str(remote))
    return repo, remote, root, global_config


def program(root, name, tail=""):
    marker, script = root / (name + ".marker"), root / (name + ".py")
    script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n" + tail, encoding="utf8")
    command = shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(str(script).replace("\\", "/"))
    return command, marker


def add_remote(repo, value):
    r = LocalGitRunner(repo).run(["remote", "add", "origin", str(value)])
    assert r.returncode == 0, r.stderr


def test_local_push_fetch_and_narrow_remote_operations_preserve_data(case):
    repo, remote, root, config = case
    runner = LocalGitRunner(repo)
    add_remote(repo, remote)
    assert runner.run(["remote", "get-url", "origin"]).stdout.strip() == str(remote)
    ok, err = runner.push()
    assert ok, err
    head = runner.rev_parse()
    assert git(remote, "rev-parse", head) == head
    assert git(remote, "show", head + ":business.txt") == "base"
    ok, err = runner.fetch()
    assert ok, err
    assert runner.rev_parse("refs/remotes/origin/" + git(repo, "branch", "--show-current")) == head
    assert runner.run(["remote", "set-url", "origin", str(remote)]).returncode == 0
    assert runner.run(["remote", "remove", "origin"]).returncode == 0
    assert not runner.fetch()[0]
    assert runner.run(["remote", "update"]).returncode == 126


def test_operator_identity_remains_available_for_local_commits(case):
    repo, remote, root, config = case
    (repo / "business.txt").write_bytes(b"operator commit\n")
    runner = LocalGitRunner(repo)
    assert runner.add_all()
    ok, err = runner.commit("operator identity")
    assert ok, err
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "Operator <operator@gitgo.invalid>"


def test_named_fetch_transfers_a_new_pack_without_an_empty_pack_hook(case):
    repo, remote, root, _ = case
    runner = LocalGitRunner(repo)
    add_remote(repo, remote)
    assert runner.push()[0]
    writer = root / "writer"
    git(root, "clone", str(remote), str(writer))
    (writer / "new-object.txt").write_text("new object requiring a pack\n", encoding="utf8")
    git(writer, "add", "new-object.txt"); git(writer, "commit", "-m", "new remote object")
    git(writer, "push")
    oid = git(writer, "rev-parse", "HEAD")
    success, error = runner.fetch()
    assert success, error
    assert git(repo, "show", oid + ":new-object.txt") == "new object requiring a pack"


def test_fixed_local_import_rejects_source_programs_and_extra_refspecs(case):
    repo, remote, root, _ = case
    runner = LocalGitRunner(repo)
    add_remote(repo, remote)
    assert runner.push()[0]
    oid = runner.rev_parse()
    args = ["fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            "--no-auto-maintenance", str(remote), oid]
    assert runner.run([*args, "HEAD:refs/heads/unwanted"]).returncode == 126
    command, marker = program(root, "pack-hook")
    git(remote, "config", "uploadpack.packObjectsHook", command)
    assert runner.run(args).returncode == 126
    assert not marker.exists()


@pytest.mark.parametrize("where,name", [("client", "pre-push"), ("client", "reference-transaction"), ("server", "pre-receive"), ("server", "post-receive")])
def test_repository_hooks_are_rejected_without_executing_or_publishing(case, where, name):
    repo, remote, root, config = case
    command, marker = program(root, "hook")
    hooks = (repo / ".git" if where == "client" else remote) / "hooks"
    hook = hooks / name
    hook.write_text("#!/bin/sh\n" + command + "\n", encoding="utf8")
    hook.chmod(0o755)
    add_remote(repo, remote)
    result = LocalGitRunner(repo).run(["push", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr
    assert not marker.exists()
    assert git(remote, "for-each-ref", "--format=%(refname)") == ""


@pytest.mark.parametrize("key,url", [("core.sshCommand", "ssh://example.invalid/fixture"), ("credential.helper", "https://example.invalid/fixture"), ("core.askPass", "https://example.invalid/fixture"), ("remote.origin.uploadpack", "ssh://example.invalid/fixture"), ("remote.origin.vcs", "https://example.invalid/fixture")])
def test_repository_transport_programs_never_execute(case, key, url):
    repo, remote, root, config = case
    command, marker = program(root, "transport")
    add_remote(repo, url)
    git(repo, "config", key, command)
    result = LocalGitRunner(repo).run(["fetch", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("rewrite", [False, True])
def test_custom_transport_helpers_are_rejected_including_url_rewrites(case, rewrite):
    repo, remote, root, config = case
    command, marker = program(root, "custom")
    url = "ext::" + command
    if rewrite:
        git(repo, "config", "url." + url + ".insteadOf", "fixture:")
        url = "fixture:"
    add_remote(repo, url)
    result = LocalGitRunner(repo).run(["fetch", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr
    assert not marker.exists()


def test_operator_ssh_command_is_available_but_not_exposed_in_argv(case):
    repo, remote, root, config = case
    command, marker = program(root, "operator-ssh", "raise SystemExit(1)\n")
    git(repo, "config", "--file", str(config), "core.sshCommand", command)
    add_remote(repo, "ssh://example.invalid/fixture")
    with protected_git(repo, ["git", "fetch", "origin"]) as (argv, env):
        assert command not in " ".join(argv)
        assert command in env.values()
        with pytest.raises(PermissionError):
            config.write_text("changed")
    result = LocalGitRunner(repo).run(["fetch", "origin"], timeout=15)
    assert result.returncode != 0  # Synthetic SSH command is not an SSH server.
    assert result.returncode != 126 and marker.exists()


def test_operator_credential_helper_handles_a_real_http_auth_challenge(case):
    repo, remote, root, config = case
    command, marker = program(root, "operator-credential", "import sys\nif sys.argv[-1] == 'get':\n print('username=fixture')\n print('password=synthetic-only')\n print()\n")
    git(repo, "config", "--file", str(config), "credential.helper", "")
    git(repo, "config", "--file", str(config), "--add", "credential.helper", "!" + command)
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.headers.get("Authorization"))
            self.send_response(404 if observed[-1] else 401)
            self.send_header("WWW-Authenticate", 'Basic realm="synthetic-gitgo"')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        add_remote(repo, f"http://127.0.0.1:{server.server_port}/fixture.git")
        result = LocalGitRunner(repo).run(["fetch", "origin"], timeout=15)
        assert result.returncode not in {0, 126}  # Endpoint is an auth probe, not a Git server.
        assert marker.exists() and any(value and value.startswith("Basic ") for value in observed)
        assert "synthetic-only" not in " ".join(result.args)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("conditional", [False, True])
def test_repository_cannot_be_laundered_into_operator_configuration(case, conditional):
    repo, remote, root, config = case
    included = repo / "operator-include.conf"
    included.write_text('[core]\nsshCommand = echo repository-program\n', encoding="utf8")
    key = "includeIf.onbranch:never-matches.path" if conditional else "include.path"
    git(repo, "config", "--file", str(config), key, str(included))
    add_remote(repo, remote)
    result = LocalGitRunner(repo).run(["fetch", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr


def test_local_target_configuration_stays_pinned_for_transfer(case):
    repo, remote, root, config = case
    add_remote(repo, remote)
    with protected_git(repo, ["git", "push", "origin"]):
        with pytest.raises(PermissionError):
            (remote / "config").write_text("changed")
        with pytest.raises(PermissionError):
            (repo / ".git/config").write_text("changed")


def test_local_target_per_worktree_configuration_is_not_unchecked(case):
    repo, remote, root, config = case
    git(remote, "config", "extensions.worktreeConfig", "true")
    (remote / "config.worktree").write_text('[core]\nhooksPath = unchecked\n', encoding="utf8")
    add_remote(repo, remote)
    result = LocalGitRunner(repo).run(["push", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr


def test_cherry_pick_works_and_custom_merge_programs_are_not_run(case):
    repo, remote, root, config = case
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "topic")
    (repo / "business.txt").write_bytes(b"topic\n")
    git(repo, "add", "business.txt")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", "topic")
    topic = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", base)
    runner = LocalGitRunner(repo)
    ok, err = runner.cherry_pick(topic)
    assert ok, err
    assert (repo / "business.txt").read_bytes() == b"topic\n"
    git(repo, "reset", "--hard", base)
    command, marker = program(root, "merge-driver")
    git(repo, "config", "merge.special.driver", command)
    result = runner.run(["cherry-pick", "-Xtheirs", topic])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr
    assert not marker.exists() and git(repo, "rev-parse", "HEAD") == base


def test_hook_created_after_preflight_is_disabled_in_local_receiver(case):
    repo, remote, root, config = case
    add_remote(repo, remote)
    command, marker = program(root, "late-receive")
    with protected_git(repo, ["git", "push", "origin"]) as (argv, env):
        hook = remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\n" + command + "\n", encoding="utf8")
        hook.chmod(0o755)
        result = subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert git(remote, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")


def test_absent_local_remote_returns_typed_failure(case):
    repo, remote, root, config = case
    add_remote(repo, root / "absent.git")
    result = LocalGitRunner(repo).run(["fetch", "origin"])
    assert result.returncode == 126 and "GIT_REMOTE_UNAVAILABLE" in result.stderr


def test_operator_include_from_main_repository_is_not_trusted_in_linked_worktree(case):
    repo, remote, root, config = case
    linked = root / "linked"
    git(repo, "worktree", "add", "--detach", str(linked))
    included = repo / "operator-include.conf"
    included.write_text('[core]\nsshCommand = echo repository-program\n', encoding="utf8")
    git(repo, "config", "--file", str(config), "include.path", str(included))
    add_remote(repo, remote)
    result = LocalGitRunner(linked).run(["fetch", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr


def test_local_receiver_checkout_updates_are_not_executed(case):
    repo, remote, root, config = case
    git(remote, "config", "receive.denyCurrentBranch", "updateInstead")
    add_remote(repo, remote)
    result = LocalGitRunner(repo).run(["push", "origin"])
    assert result.returncode == 126 and "GIT_REPOSITORY_EXTENSION_UNSUPPORTED" in result.stderr
    assert git(remote, "for-each-ref", "--format=%(refname)") == ""
