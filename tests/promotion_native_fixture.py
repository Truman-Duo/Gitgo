"""Ordinary-user promotion fixtures; no privileged-service acceptance claim.

Only broker admission/transport is substituted. Worktrees, immutable Git
objects, publication, SQLite journals and file locks use production code.
"""
import base64
import os
import subprocess


def authorize_project(repo, monkeypatch, request, scratch):
    from backend.core import macos_broker_client as broker
    from backend.core import macos_broker_protocol as wire
    from backend.core import macos_git_checkout as checkout, macos_git_read as read
    from backend.core.sandbox_macos import task_environment
    from backend.core.sandbox_read import ReadRoot
    from tests.test_macos_git_write import run as produce_objects

    scratch.mkdir()
    fd = wire.open_directory(repo)
    try:
        binding = dict(project=str(repo), identity=wire.directory_identity(fd), grant_id="a" * 32)
    finally:
        os.close(fd)
    token = broker._SESSION.set(binding)
    request.addfinalizer(lambda: broker._SESSION.reset(token))
    sequence = []

    def directory(project):
        assert project == repo
        assert broker.project_binding(project) == {key: binding[key] for key in ("identity", "grant_id")}
        path = scratch / str(len(sequence))
        sequence.append(path)
        path.mkdir()
        return path

    def prepare(project, root, commit, **kwargs):
        work = directory(project)
        with ReadRoot(project / ".git/objects") as source:
            read.capture_objects(source, project / ".git/objects").materialize(work / "object-view")
        target = work / "checkout"
        target.mkdir()
        command = checkout.command(work / "object-view", target, commit)
        result = subprocess.run(
            command, env=task_environment(command, work, {"PATH": "/usr/bin:/bin"}),
            capture_output=True, text=True, check=True, timeout=10,
        )
        image = dict(
            files=[[str(p.relative_to(target)), base64.b64encode(p.read_bytes()).decode(),
                    p.stat().st_mode & 0o777] for p in target.rglob("*") if p.is_file()],
            directories=[str(p.relative_to(target)) for p in target.rglob("*") if p.is_dir()],
        )
        return checkout.verify_result(commit, image, result.stdout)

    def produce(project, root, query, data, author, **kwargs):
        return read.QueryResult(produce_objects(project, directory(project), query, data, author) + "\n", 0)

    def query(project, root, arguments, **kwargs):
        work = directory(project)
        with ReadRoot(project / ".git/objects") as source:
            read.capture_objects(source, project / ".git/objects").materialize(work / "object-view")
        result = subprocess.run(read.read_command(work / "object-view", arguments),
                                capture_output=True, text=True, timeout=10)
        return read.QueryResult(result.stdout, result.returncode)

    monkeypatch.setattr(broker, "checkout_git_commit", prepare)
    monkeypatch.setattr(broker, "produce_git_objects", produce)
    monkeypatch.setattr(broker, "query_git_objects", query)


def observe_formal_apply(worktrees, monkeypatch, observe):
    """Observe the actual write boundary for each platform, without replacing it."""
    import sys
    if sys.platform == "darwin":
        from backend.core import macos_promotion_write as writer
        original = writer.publish_file

        def apply(*args, **kwargs):
            observe()
            return original(*args, **kwargs)

        monkeypatch.setattr(writer, "publish_file", apply)
    else:
        original = worktrees._git_input

        def apply(path, data, *args):
            if path == worktrees.repo_root:
                observe()
            return original(path, data, *args)

        monkeypatch.setattr(worktrees, "_git_input", apply)


def fail_committed_ack(store):
    """Lose the durable final acknowledgement, independent of earlier save count."""
    original = store.commit_state_revision

    def save(namespace, ref_id, blob, **kwargs):
        import json
        if namespace == "agent_promotion" and json.loads(store.read_blob(blob))["state"] == "committed":
            raise RuntimeError("ack unavailable")
        return original(namespace, ref_id, blob, **kwargs)

    return save
