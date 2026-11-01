"""Exact named-remote observations and conditional updates, no broad push."""
import re
from .git import CandidateGit, OID
from .state import PublishError


class RemoteGit:
    def __init__(self, workspace, remote):
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", remote):
            raise PublishError("PUBLISH_INVALID_REMOTE")
        self.git, self.remote = CandidateGit(workspace), remote

    def observe(self, ref, *, identity=""):
        binding = {"expected": identity}
        result = self.git.runner.run(["ls-remote", "--refs", "--exit-code", self.remote, ref],
                                     timeout=30, transport_identity=binding)
        if result.returncode == 2 and not result.stdout.strip():
            oid = None
        elif result.returncode == 0:
            rows = [line.split("\t") for line in result.stdout.splitlines()]
            if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref or not OID.fullmatch(rows[0][0]):
                raise PublishError("PUBLISH_REMOTE_AMBIGUOUS")
            oid = rows[0][0]
        else:
            raise PublishError("PUBLISH_REMOTE_UNAVAILABLE")
        return {"remote": self.remote, "ref": ref, "oid": oid, "identity": binding["observed"]}

    def fetch(self, observation):
        if observation["remote"] != self.remote or not observation["oid"] or not OID.fullmatch(observation["oid"]):
            raise PublishError("PUBLISH_REMOTE_UNAVAILABLE")
        result = self.git.runner.run(["fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            "--no-auto-maintenance", self.remote, observation["oid"]], timeout=60,
            transport_identity={"expected": observation["identity"]})
        if result.returncode or self.git.oid(observation["oid"]) != observation["oid"]:
            raise PublishError("PUBLISH_IMPORT_FAILED")
        return observation["oid"]

    def update(self, observation, result_oid, *, delete=False):
        if observation["remote"] != self.remote:
            raise PublishError("PUBLISH_REMOTE_UNAVAILABLE")
        ref, expected = observation["ref"], observation["oid"]
        if delete:
            if result_oid or not expected or not ref.startswith("refs/heads/"):
                raise PublishError("PUBLISH_INVALID_BRANCH")
        else:
            if not OID.fullmatch(result_oid):
                raise PublishError("PUBLISH_INVALID_SOURCE")
            self.git.oid(result_oid)
            if ref.startswith("refs/tags/") and expected and expected != result_oid:
                raise PublishError("PUBLISH_TAG_CONFLICT")
            if expected and self.git.run(["merge-base", "--is-ancestor", expected, result_oid]).returncode:
                raise PublishError("PUBLISH_REMOTE_NON_FAST_FORWARD")
        # Lease is the exact observed value; even an empty-ref creation is CAS.
        # Ancestor validation above prevents using the lease as force overwrite.
        return self.git.runner.run(["push", "--porcelain", "--force-with-lease=" + ref + ":" + (expected or ""),
            self.remote, (result_oid or "") + ":" + ref], timeout=60,
            transport_identity={"expected": observation["identity"]})
