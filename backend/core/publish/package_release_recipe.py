"""Fixed package release recipe using the existing scoped platform transport.

GitHub offers a real draft. GitLab package uploads/release creation happen only
in the published phase. This recipe verifies authenticated asset bytes; it does
not claim anonymous delivery, CI evidence, or Gitgo's signed update feed.
"""
from copy import deepcopy
import hashlib
import time

from .release_engine import ReleaseBlocked, ReleaseCancelled
from .state import PublishError


class PackageReleaseRecipe:
    identity = {"id": "project.fixed-package", "version": 1}

    def __init__(self, transport, package, assets, record, checkpoint, *, observe_only=False, body=None, source_validator=None, anonymous=False):
        from .release_transport import release_body
        self.transport, self.package, self.assets = transport, package, assets
        self.record, self.checkpoint, self.observe_only = record, checkpoint, observe_only
        self.record.setdefault("current", None)
        self.record.setdefault("state", "unverified")
        self.body = release_body(package) if body is None else body
        self.version, self.oid = package["version"], package["source_oid"]
        self.supported_phases = ("prepared", "draft", "published") if transport.target.kind == "github" else ("prepared", "published")
        self.context = None
        self.last_cancel_check = float("-inf")
        self.source_validator = source_validator
        self.anonymous, self.delivery_verified = anonymous, False
        if anonymous:
            self.supported_phases += ("verified",)

    def _check_cancel(self, force=False):
        now = time.monotonic()
        if force or now-self.last_cancel_check >= .2:
            self.last_cancel_check = now
            if self.context and self.context.cancelled and self.context.cancelled():
                raise ReleaseCancelled("platform_io")

    def _save(self, **fields):
        self.record.update(fields)
        self.checkpoint(deepcopy(self.record))

    def _blocked(self, phase, reason):
        self._save(state="partial", current=None, reason=reason)
        raise ReleaseBlocked(phase, reason)

    def _write(self, step, action):
        self._check_cancel(True)
        self._save(current=step, state="intent")
        # A failure after this durable intent is reconciled, never blindly
        # replayed by the engine or an observation-only recovery invocation.
        self._check_cancel(True)
        action()

    def validate(self, context):
        self.context = context
        self._check_cancel()
        if self.source_validator:
            self.source_validator()
        actor, repository = self.transport.client.verify_identity(), self.transport.client.repository()
        if (self.record.get("repository_id", repository["id"]) != repository["id"]
                or self.record.get("account_id", actor["id"]) != actor["id"]):
            raise PublishError("PUBLISH_RELEASE_TARGET_CHANGED")
        self._save(repository_id=repository["id"], account_id=actor["id"])

    def guard(self, context, prepared):
        self._check_cancel()
        if len({asset["name"] for asset in self.assets}) != len(self.assets):
            raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
        for asset in self.assets:
            value = asset["raw"]
            hasher, size = hashlib.sha256(), 0
            for chunk in ([value] if isinstance(value, bytes) else value):
                self._check_cancel()
                hasher.update(chunk); size += len(chunk)
            if hasher.hexdigest() != asset["sha256"] or size != asset["size"]:
                raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")

    def prepare(self, context):
        self.guard(context, None)
        if not self.observe_only:
            self.transport.commit(self.oid)
        tag = self.transport.tag(self.version)
        if tag not in (None, self.oid):
            raise PublishError("PUBLISH_TAG_CONFLICT")
        released = self.transport.release(self.version)
        if released is not None and released["body"] != self.body:
            raise PublishError("PUBLISH_RELEASE_CONFLICT")
        return {"source_oid": self.oid, "assets": len(self.assets)}

    def _tag(self, phase):
        tag = self.transport.tag(self.version)
        if tag is None and not self.observe_only:
            self._write("tag", lambda: self.transport.create_tag(self.version, self.oid))
            tag = self.transport.tag(self.version)
        if tag not in (None, self.oid):
            raise PublishError("PUBLISH_TAG_CONFLICT")
        if tag is None:
            self._blocked(phase, "PUBLISH_RELEASE_TAG_ABSENT")

    def _assets(self, released, phase):
        known = self.transport.assets(released) if released and self.transport.target.kind == "github" else {}
        if set(known) - {row["name"] for row in self.assets}:
            raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
        observed = []
        for asset in self.assets:
            self._check_cancel()
            def observe(known=None):
                if hasattr(self.transport, "download_fact"):
                    return self.transport.download_fact(released, self.version, asset, known)
                raw = self.transport.download(released, self.version, asset["name"], known)
                return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} if raw is not None else None
            fact = observe(known) if released or self.transport.target.kind == "gitlab" else None
            if fact is None and not self.observe_only:
                if released and not released["draft"]:
                    self._blocked(phase, "PUBLISH_RELEASE_IMMUTABLE_INCOMPLETE")
                self._write("upload:" + asset["name"],
                    lambda asset=asset: self.transport.upload(released, self.version, asset, asset["raw"]))
                fact = observe()
            actual = fact["sha256"] if fact is not None else None
            if fact is not None and (actual != asset["sha256"] or fact["size"] != asset["size"]):
                raise PublishError("PUBLISH_ASSET_DIGEST_MISMATCH")
            observed.append({"name": asset["name"], "state": "verified" if fact is not None else "absent", "sha256": actual})
            # The next external intent persists these observations together.
            # Reading each short asset does not require another full revision.
            self.record.update(assets=list(observed), current=None)
        self._save(assets=list(observed), current=None)
        if not all(row["state"] == "verified" for row in observed):
            self._blocked(phase, "PUBLISH_RELEASE_INCOMPLETE")

    def _release(self):
        released = self.transport.release(self.version)
        if released and released["body"] != self.body:
            raise PublishError("PUBLISH_RELEASE_CONFLICT")
        return released

    def draft(self, context, prepared):
        self._tag("draft")
        released = self._release()
        if released is None and not self.observe_only:
            self._write("release", lambda: self.transport.create_release(self.version, self.oid, self.body, self.assets))
            released = self._release()
        if released is None:
            self._blocked("draft", "PUBLISH_RELEASE_INCOMPLETE")
        self._assets(released, "draft")
        self._save(state="draft" if released["draft"] else "published", current=None,
                   release_id=released["id"], url=released["url"], reason="PUBLISH_RELEASE_DRAFT_VERIFIED")
        return released

    def publish(self, context, prepared, draft):
        self._tag("published")
        released = self._release()
        if released is None and self.transport.target.kind == "github":
            self._blocked("published", "PUBLISH_RELEASE_INCOMPLETE")
        self._assets(released, "published")
        if released is None and not self.observe_only:
            self._write("release", lambda: self.transport.create_release(self.version, self.oid, self.body, self.assets))
            released = self._release()
        if released is None:
            self._blocked("published", "PUBLISH_RELEASE_INCOMPLETE")
        if self.transport.target.kind == "gitlab":
            links = released["links"]
            expected = {a["name"]: self.transport.asset_url(self.version, a["name"]) for a in self.assets}
            if {row.get("name"): row.get("url") for row in links} != expected or len(links) != len(self.assets):
                raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
        if released["draft"] and not self.observe_only:
            self._write("publish", lambda: self.transport.publish(released))
            released = self._release()
        if self.transport.tag(self.version) != self.oid:
            raise PublishError("PUBLISH_TAG_CONFLICT")
        if not released or released["draft"]:
            self._blocked("published", "PUBLISH_RELEASE_INCOMPLETE")
        self._save(state="published", current=None, release_id=released["id"], url=released["url"],
                   reason="PUBLISH_RELEASE_VERIFIED")

    def result(self, phase, draft):
        return {"phase": phase, "release": deepcopy(self.record),
                "authenticated_assets_verified": phase != "prepared", "public_delivery_verified": self.delivery_verified,
                "observation_only":self.observe_only,"fresh_execution_evidence":not self.observe_only}

    def verify(self, context, prepared):
        if not self.anonymous:
            raise ValueError("Anonymous delivery verification is not provided by this recipe")
        from .public_delivery import verify_assets
        facts = verify_assets(self.transport.target, self.version, self.assets,
                              cancelled=context.cancelled, progress=self.transport.progress)
        self._save(public_delivery=facts, state="verified", current=None)
        self.delivery_verified = True
