"""One fresh, checkpointed comment attempt on the owned acceptance Issue."""
import json
import os
from pathlib import Path
import sys


def main():
    root = Path.home() / "Desktop" / "Gitgo-Issue40-Acceptance"
    out = root / "remote-acceptance"
    checkpoint = json.loads((out / "checkpoints.json").read_text(encoding="utf8"))
    fixture = Path(checkpoint["fixture"]).resolve()
    if fixture.parent != root.resolve():
        raise ValueError("Unexpected fixture")
    failed = checkpoint["steps"]["comment-send"]["result"]
    if failed["effect_state"] != "not_committed" or failed["reason"] != "PUBLISH_PLATFORM_HTTP_400":
        raise ValueError("Original attempt requires observation")
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
    record_path = out / "comment-diagnostic.json"
    if record_path.exists():
        print(json.dumps({"state": "existing_checkpoint_requires_inspection"}))
        return
    with PlatformClient(target, resolve_secret(target)) as client:
        repo = client.request("GET", target.repo_path)[0]
        if repo.get("id") != github["repository_id"] or repo.get("private") is not True:
            raise ValueError("Target changed")
        existing, more = client.page(target.repo_path + "/issues/1/comments")
        old = failed["item"]["collaboration"]["drafts"][-1]
        if more or any(old["marker"] in row.get("body", "") for row in existing):
            raise ValueError("Previous effect requires observation")
    services = ApplicationServices()
    item_id = failed["item"]["id"]
    record = {"original_status": 400, "original_effect_state": "not_committed", "previous_marker_absent": True,
        "change": "Do not send parent Issue ETag as If-Match for comment collection POST", "steps": {}}

    def persist():
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf8")

    def call(step, operation, **args):
        current = services.invoke("publish.item", {"project": project.name, "item_id": item_id})["item"]
        arguments = {"project": project.name, "item_id": item_id, "expected_revision": current["revision"],
            "request_id": "acceptance-comment-conditional-fix-" + step, **args}
        record["steps"][step] = {"operation": operation, "args": arguments, "state": "intent"}
        persist()
        response = services.invoke("publish." + operation, arguments)
        record["steps"][step].update(state="observed", result=response)
        persist()
        if response.get("error") or response.get("effect_state") != "committed":
            raise ValueError("Attempt needs inspection: " + str(response.get("reason", response.get("error"))))
        return response["item"]

    item = call("refresh", "collaborate", action="refresh", target=target.descriptor(), resource={"kind": "issue", "number": 1})
    item = call("draft", "collaborate", action="draft", snapshot_id=item["collaboration"]["snapshots"][-1]["id"],
        draft={"kind": "comment", "body": "Native terminal checks passed; this comment is a synthetic acceptance fixture."})
    item = call("send", "collaborate.send", draft_id=item["collaboration"]["drafts"][-1]["id"])
    sent = item["collaboration"]["drafts"][-1]
    record.update(passed=sent["state"] == "sent", comment_url=sent.get("result", {}).get("url"))
    persist()
    print(json.dumps({"passed": record["passed"], "effect_state": "committed", "comment_url": record["comment_url"]}))


if __name__ == "__main__":
    main()
