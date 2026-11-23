"""Shared feed replacement: audit every pair before any irreversible effect."""
from .release_engine import ReleaseBlocked
from .state import PublishError


def matches(row, fact):
    return (row.get("state") == "uploaded" and row.get("size") == fact["bytes"]
            and row.get("digest") == "sha256:" + fact["sha256"])


def advance_feed(remote, tag, wanted, journal, *, observe_only=False):
    """Adapters own storage/authorization; matching bytes never authorize writes.

    GitHub has no atomic signature/manifest pair. Signature-first replacement
    keeps clients fail-closed and can resume only the recorded deletion intents.
    """
    feed = remote.release(tag)
    if not feed or feed.get("draft") is not False:
        raise PublishError("PUBLISH_FEED_PUBLIC_BASELINE_REQUIRED")
    found = remote.assets(feed)
    baseline = journal.baseline()
    if baseline and baseline["release_id"] != feed["id"]:
        raise PublishError("PUBLISH_FEED_IDENTITY_CHANGED")
    if baseline is None:
        baseline = {"release_id":feed["id"], "assets":{name:row["id"] for name,row in found.items() if name in wanted}}
        journal.save_baseline(baseline)
    def admissible(name, row):
        value = wanted[name]
        if row is None:
            return journal.delete_recorded(name)
        return matches(row,value["new"]) or (matches(row,value["old"]) and row["id"] == baseline["assets"].get(name))
    for name in wanted:
        if not admissible(name,found.get(name)):
            raise PublishError("PUBLISH_FEED_CHANGED_OUTSIDE_RELEASE")
    # Preserve the supplied dependency order: detached signature then manifest.
    for name,value in wanted.items():
        row = remote.assets(feed).get(name)
        if row and matches(row,value["new"]): continue
        if not admissible(name,row): raise PublishError("PUBLISH_FEED_CHANGED_OUTSIDE_RELEASE")
        if observe_only:
            raise ReleaseBlocked("published", "PUBLISH_FEED_ADVANCE_REQUIRED")
        if row:
            journal.write("feed-delete-"+name, lambda row=row:remote.delete(row["id"]), asset_id=row["id"])
        journal.write("feed-upload-"+name, lambda value=value:remote.upload(feed,value["asset"]), expected=value["new"])
        current = remote.assets(feed).get(name)
        if not current or not matches(current,value["new"]):
            raise PublishError("PUBLISH_FEED_REPLACEMENT_UNCONFIRMED")
    return {"release_id":feed["id"], "assets":len(wanted)}
