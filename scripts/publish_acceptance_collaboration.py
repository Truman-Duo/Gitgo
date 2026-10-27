"""Checkpointed extended collaboration acceptance on the owned private fixture."""
import json
import os
from pathlib import Path
import sys


def main():
    root = Path.home() / "Desktop" / "Gitgo-Issue40-Acceptance"
    out = root / "remote-acceptance"
    original = json.loads((out / "checkpoints.json").read_text(encoding="utf8"))
    fixture = Path(original["fixture"]).resolve()
    if fixture.parent != root.resolve():
        raise ValueError("Unexpected fixture")
    os.environ.update(GITGO_STATE_HOME=str(fixture / "state"), GITGO_CONFIG_PATH=str(fixture / "config.json"),
        GITGO_LLM_CONFIG_PATH=str(fixture / "llm.json"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.core.application import ApplicationServices
    from backend.core.config import ConfigManager
    from backend.remote.publish_accounts import resolve_secret
    from backend.remote.publish_platform import PlatformClient, PlatformTarget
    project = ConfigManager.load().projects[0]
    github = json.loads((root / "targets.json").read_text(encoding="utf8"))["github"]
    target = PlatformTarget.parse({"kind": "github", "instance": "https://github.com", "account": github["account"], "repository": github["repository"]})
    if not github["private"] or not target.repository.startswith(target.account + "/gitgo-issue40-acceptance-"):
        raise ValueError("Unexpected target")
    with PlatformClient(target, resolve_secret(target)) as client:
        identity = client.verify_identity()
        repo = client.request("GET", target.repo_path)[0]
        if repo.get("id") != github["repository_id"] or repo.get("private") is not True:
            raise ValueError("Target changed")
    diagnostic = json.loads((out / "comment-diagnostic.json").read_text(encoding="utf8"))
    comment = diagnostic["steps"]["send"]["result"]["item"]["collaboration"]["drafts"][-1]["result"]
    if comment["author"] != identity:
        raise ValueError("Comment is not owned by the fixture account")
    services = ApplicationServices()
    item_id = original["steps"]["issue-send"]["result"]["item"]["id"]
    checkpoint = out / "collaboration-extended.json"
    saved = json.loads(checkpoint.read_text(encoding="utf8")) if checkpoint.exists() else {"fixture": str(fixture), "steps": {}}
    if saved["fixture"] != str(fixture):
        raise ValueError("Unexpected checkpoint")

    def persist():
        temporary = out / "collaboration-extended.tmp"
        temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf8")
        temporary.replace(checkpoint)

    def call(step, operation, **args):
        entry = saved["steps"].setdefault(step, {"attempts": []})
        if entry["attempts"]:
            previous = entry["attempts"][-1]
            response = previous.get("result", {})
            if response.get("effect_state") == "committed" and "item" in response:
                return response["item"]
            if response.get("effect_state") != "not_committed" or response.get("error") not in {
                "PUBLISH_REVISION_CONFLICT", "PUBLISH_DISTRIBUTION_PENDING", "PUBLISH_OPERATION_PENDING"}:
                raise ValueError("Saved attempt requires observation: " + step)
        current = services.invoke("publish.item", {"project": project.name, "item_id": item_id})["item"]
        arguments = {"project": project.name, "item_id": item_id, "expected_revision": current["revision"],
            "request_id": "acceptance-collaboration-extra-" + step + "-" + str(len(entry["attempts"]) + 1), **args}
        attempt = {"operation": operation, "args": arguments, "state": "intent"}
        entry["attempts"].append(attempt)
        persist()
        response = services.invoke("publish." + operation, arguments)
        attempt.update(state="observed", result=response)
        persist()
        print(json.dumps({"step": step, "effect_state": response.get("effect_state"), "error": response.get("error"), "reason": response.get("reason")}), flush=True)
        if response.get("error") or response.get("effect_state") != "committed" or "item" not in response:
            raise ValueError("Attempt requires inspection: " + step)
        return response["item"]

    call("search", "collaborate", action="search", target=target.descriptor(), resource={"kind": "issues"}, query="Synthetic", page=1)
    for kind, content in [("edit", {"title": "Synthetic Publish acceptance (edited)", "body": "Fixture for fixed candidate collaboration and release verification. Edit verified."}),
            ("close", {"kind": "state", "state": "closed", "body": ""}),
            ("reopen", {"kind": "state", "state": "open", "body": ""}),
            ("comment-edit", {"kind": "comment_edit", "body": "Native terminal checks passed; this own-account synthetic comment was edited.",
                "reference": {"kind": "comment", "id": str(comment["id"])}}),
            ("reply", {"body": "Synthetic fixed-reference reply verified.", "reference": {"kind": "comment", "id": str(comment["id"])}})]:
        item = call(kind + "-read", "collaborate", action="refresh", target=target.descriptor(), resource={"kind": "issue", "number": 1})
        snapshot_id = item["collaboration"]["snapshots"][-1]["id"]
        item = call(kind + "-draft", "collaborate", action="draft", snapshot_id=snapshot_id, draft={"kind": kind, **content})
        call(kind + "-send", "collaborate.send", draft_id=item["collaboration"]["drafts"][-1]["id"])
    call("followup", "collaborate", action="followup", snapshot_id=snapshot_id,
        followup={"reference": {"kind": "comment", "id": str(comment["id"])}, "status": "resolved", "owner": target.account,
            "note": "Synthetic fixed-reference reply verified; local follow-up complete."})
    saved["passed"] = True
    persist()
    print(json.dumps({"passed": True, "operations": ["search", "edit", "close", "reopen", "comment_edit", "reply", "followup"]}), flush=True)


if __name__ == "__main__":
    main()
