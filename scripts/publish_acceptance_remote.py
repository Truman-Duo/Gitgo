"""Run explicit Publish acceptance against the operator-owned synthetic target.

Uses the already accepted native-terminal fixture, private GitHub repository,
isolated config/state and existing credential helper. Never uploads this repo.
Checkpoints prevent retrying an unknown external write after interruption.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main(root, fixture):
    root, fixture = Path(root).resolve(), Path(fixture).resolve()
    if fixture.parent != root:
        raise ValueError("Acceptance fixture must be directly inside the approved Desktop root")
    targets = json.loads((root / "targets.json").read_text(encoding="utf8"))
    github = targets["github"]
    if not github["private"] or not github["repository"].startswith(github["account"] + "/gitgo-issue40-acceptance-"):
        raise ValueError("Not the owned private synthetic target")
    accepted = json.loads((fixture / "result.json").read_text(encoding="utf8"))
    if not accepted["passed"] or accepted["item"]["status"] != "integrated":
        raise ValueError("Native terminal acceptance has not completed")
    os.environ.update(GITGO_STATE_HOME=str(fixture / "state"), GITGO_CONFIG_PATH=str(fixture / "config.json"),
        GITGO_LLM_CONFIG_PATH=str(fixture / "llm.json"), GCM_INTERACTIVE="Never", GIT_TERMINAL_PROMPT="0")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.core.application import ApplicationServices
    from backend.core.config import ConfigManager
    from backend.core.publish.remote_git import RemoteGit
    from backend.remote.publish_accounts import provision_secret
    from backend.remote.publish_platform import PlatformClient, PlatformTarget

    project = ConfigManager.load().projects[0]
    workspace = Path(project.workspace_path)
    if workspace.parent != fixture or project.backup_path != str(workspace):
        raise ValueError("Only the isolated native fixture may be used")
    target = {"kind": "github", "instance": "https://github.com", "repository": github["repository"], "account": github["account"]}
    credential = subprocess.run(["git", "credential-manager", "get"], input="protocol=https\nhost=github.com\nusername=" + github["account"] + "\n\n",
        capture_output=True, text=True, encoding="utf8", timeout=30)
    fields = dict(row.split("=", 1) for row in credential.stdout.splitlines() if "=" in row)
    token = fields.get("password")
    if credential.returncode or not token:
        raise ValueError("Existing noninteractive credential unavailable")
    provision_secret(target, token)
    with PlatformClient(PlatformTarget.parse(target), token) as client:
        repo = client.request("GET", client.target.repo_path)[0]
        if repo.get("id") != github["repository_id"] or repo.get("private") is not True:
            raise ValueError("Remote repository identity changed")
    del token, fields, credential
    services = ApplicationServices()
    out = root / "remote-acceptance"
    out.mkdir(exist_ok=True)
    checkpoint = out / "checkpoints.json"
    saved = json.loads(checkpoint.read_text(encoding="utf8")) if checkpoint.exists() else {"fixture": str(fixture), "steps": {}}
    if saved["fixture"] != str(fixture):
        raise ValueError("Checkpoint belongs to a different fixture")

    def persist():
        temporary = out / "checkpoints.tmp"
        temporary.write_text(json.dumps(saved, indent=2, ensure_ascii=True) + "\n", encoding="utf8")
        temporary.replace(checkpoint)

    def current():
        return services.invoke("publish.item", {"project": project.name, "item_id": accepted["item"]["id"]})["item"]

    def call(step, operation, **args):
        record = saved["steps"].get(step)
        if record and "result" in record:
            result = record.get("recovery_result", record["result"])
            if step == "comment-send" and result.get("effect_state") == "not_committed" and result.get("reason") == "PUBLISH_PLATFORM_HTTP_400":
                # A separate diagnostic explicitly created and confirmed a NEW
                # draft. Retain the original rejected receipt without replay.
                diagnostic = json.loads((out / "comment-diagnostic.json").read_text(encoding="utf8"))
                confirmed = diagnostic["steps"]["send"]["result"]
                if confirmed.get("effect_state") == "committed" and confirmed.get("item", {}).get("id") == accepted["item"]["id"]:
                    record["recovery_result"] = result = confirmed
                    record["recovery_source"] = "comment-diagnostic.json"
                    persist()
            if result.get("error") or result.get("effect_state") != "committed" or "item" not in result:
                raise ValueError("Saved acceptance step requires inspection: " + step)
            return result["item"]
        if not record:
            item = current()
            record = {"operation": operation, "args": {"project": project.name, "request_id": "acceptance-" + step,
                "item_id": item["id"], "expected_revision": item["revision"], **args}}
            saved["steps"][step] = record
            persist()
        result = services.invoke("publish." + record["operation"], record["args"])
        record["result"] = result
        persist()
        print(json.dumps({"step": step, "effect_state": result.get("effect_state"), "error": result.get("error")}), flush=True)
        if result.get("error") or result.get("effect_state") != "committed" or "item" not in result:
            raise ValueError("Acceptance step requires inspection: " + step)
        return result["item"]

    def git(*args):
        response = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True, encoding="utf8", timeout=60)
        if response.returncode:
            raise ValueError("Synthetic repository preparation failed")
        return response.stdout.strip()

    remote = "acceptance-github"
    if remote in git("remote").splitlines():
        if git("remote", "get-url", remote) != github["clone_url"]:
            raise ValueError("Synthetic remote changed")
    else:
        git("remote", "add", remote, github["clone_url"])
    transport = RemoteGit(workspace, remote)

    def push(step, ref, oid):
        observed = transport.observe(ref)
        if observed["oid"] == oid:
            saved["steps"][step] = {"observed_oid": oid, "ref": ref}
            persist()
            return
        if step in saved["steps"] or observed["oid"] is not None:
            raise ValueError("External branch write needs inspection: " + step)
        saved["steps"][step] = {"state": "intent", "observation": observed, "oid": oid}
        persist()
        result = transport.update(observed, oid)
        if result.returncode or transport.observe(ref, identity=observed["identity"])["oid"] != oid:
            raise ValueError("External branch write was not confirmed")
        saved["steps"][step] = {"observed_oid": oid, "ref": ref}
        persist()

    push("base", "refs/heads/acceptance/base", accepted["original_formal"])
    push("formal", "refs/heads/acceptance/formal", accepted["result_oid"])
    call("shared-preview", "share", action="prepare", remote=remote)
    call("shared-publish", "share.publish")
    row = call("issues-read", "collaborate", action="refresh", target=target, resource={"kind": "issues"})
    row = call("issue-draft", "collaborate", action="draft", snapshot_id=row["collaboration"]["snapshots"][-1]["id"],
        draft={"kind": "issue", "title": "Synthetic Publish acceptance", "body": "Fixture for fixed candidate collaboration and release verification."})
    row = call("issue-send", "collaborate.send", draft_id=row["collaboration"]["drafts"][-1]["id"])
    issue = row["collaboration"]["drafts"][-1]["result"]
    row = call("issue-read", "collaborate", action="refresh", target=target, resource={"kind": "issue", "number": issue["number"]})
    row = call("comment-draft", "collaborate", action="draft", snapshot_id=row["collaboration"]["snapshots"][-1]["id"],
        draft={"kind": "comment", "body": "Native terminal checks passed; this comment is a synthetic acceptance fixture."})
    call("comment-send", "collaborate.send", draft_id=row["collaboration"]["drafts"][-1]["id"])
    row = call("pulls-read", "collaborate", action="refresh", target=target, resource={"kind": "pulls"})
    row = call("pull-draft", "collaborate", action="draft", snapshot_id=row["collaboration"]["snapshots"][-1]["id"],
        draft={"kind": "pull", "title": "Synthetic fixed candidate", "body": "Acceptance fixture only.",
            "head": "acceptance/formal", "base": "acceptance/base", "head_oid": accepted["result_oid"], "base_oid": accepted["original_formal"]})
    row = call("pull-send", "collaborate.send", draft_id=row["collaboration"]["drafts"][-1]["id"])
    pull = row["collaboration"]["drafts"][-1]["result"]
    row = call("pull-read", "collaborate", action="refresh", target=target, resource={"kind": "pull", "number": pull["number"]})
    row = call("review-draft", "collaborate", action="draft", snapshot_id=row["collaboration"]["snapshots"][-1]["id"],
        draft={"kind": "review", "decision": "COMMENT", "body": "Fixed source and target were observed before this synthetic review."})
    call("review-send", "collaborate.send", draft_id=row["collaboration"]["drafts"][-1]["id"])
    row = call("package", "package", version="v0.0.40-alpha.1")
    package = row["packages"][-1]
    previous = saved["steps"].get("release", {}).get("result", {})
    if previous.get("effect_state") == "ambiguous" and previous.get("reason") == "PUBLISH_RELEASE_CONFLICT":
        # Preserve the unknown original receipt. Observe first, then authorize
        # a distinct resume command against the confirmed partial draft.
        row = call("release-observe-draft", "distribution_reconcile")
        operation = row["distribution_operation"]
        if operation["state"] != "partial" or operation["package_id"] != package["id"]:
            raise ValueError("Release recovery requires the confirmed partial package")
        media_failure = saved["steps"].get("release-resume-draft", {}).get("result", {})
        if media_failure.get("effect_state") == "ambiguous" and media_failure.get("reason") == "PUBLISH_PLATFORM_HTTP_415":
            row = call("release-observe-media", "distribution_reconcile")
            if row["distribution_operation"]["state"] != "partial":
                raise ValueError("Media recovery requires an observed partial release")
            call("release-resume-media", "release", package_id=package["id"], target=target)
        else:
            call("release-resume-draft", "release", package_id=package["id"], target=target)
    else:
        call("release", "release", package_id=package["id"], target=target)
    download = out / "download"
    download.mkdir(exist_ok=True)
    row = call("download", "download", package_id=package["id"], target=target, destination=str(download))
    path = download / package["filename"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != package["sha256"]:
        raise ValueError("Downloaded package digest mismatch")
    call("import", "import_package", path=str(path), sha256=package["sha256"])
    final = {"passed": True, "repository": github["url"], "issue": issue.get("url"), "pull": pull.get("url"),
        "source_oid": accepted["result_oid"], "package": package["filename"], "sha256": package["sha256"],
        "download": str(path), "single_account": github["account"], "second_account_verified": False, "gitlab_verified": False}
    (out / "result.json").write_text(json.dumps(final, indent=2) + "\n", encoding="utf8")
    print(json.dumps(final), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args()
    try:
        main(args.root, args.fixture)
    except Exception as exc:
        print(json.dumps({"passed": False, "error": getattr(exc, "code", type(exc).__name__),
            "detail": str(exc) if isinstance(exc, ValueError) else "Inspect saved acceptance checkpoints"}), flush=True)
        raise SystemExit(1)
