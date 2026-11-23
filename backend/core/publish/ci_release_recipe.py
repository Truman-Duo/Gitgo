"""Project CI artifacts use the same package lifecycle and project journal."""
from copy import deepcopy
import time

from backend.remote.publish_ci import ProjectCI
from .package_release_recipe import PackageReleaseRecipe
from .release_artifacts import ArtifactContent
from .release_dag import run_dag
from .release_engine import ReleaseBlocked
from .state import PublishError, digest


class CIReleaseRecipe(PackageReleaseRecipe):
    identity = {"id":"project.ci-package", "version":1}

    def __init__(self, *args, candidates, artifacts, journal, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidates, self.artifacts, self.journal = candidates, artifacts, journal
        self.fixed_assets = list(self.assets)
        self.ci = ProjectCI(self.transport.client, cancelled=self.transport.cancelled or (lambda:False),
                            progress=self.transport.progress)

    def validate(self, context):
        super().validate(context)
        # Re-observe every admitted run/attempt before each external phase.
        if not self.observe_only:
            for candidate in self.candidates:
                self.ci.validate(candidate, self.oid)

    def prepare(self, context):
        steps = []
        by_id = {str(candidate["artifact_id"]):candidate for candidate in self.candidates}
        for identifier in by_id:
            steps.extend([{"id":"check-"+identifier}, {"id":"artifact-"+identifier, "needs":["check-"+identifier]}])
        def execute(step, dependencies):
            started = time.monotonic()
            name = step["id"]
            candidate = by_id[name.split("-", 1)[1]]
            self.journal.record(name, "running", needs=step.get("needs",[]),
                                run_id=candidate["run_id"],attempt=candidate["attempt"],artifact_id=candidate["artifact_id"])
            if name.startswith("check-"):
                if self.observe_only:
                    value={"run_id":candidate["run_id"],"attempt":candidate["attempt"],"artifact_id":candidate["artifact_id"]}
                    self.journal.record(name,"observed",seconds=time.monotonic()-started,
                        reason="PUBLISH_RELEASE_RECORDED_INPUTS_ONLY",result=value)
                    return value
                value = self.ci.validate(candidate, self.oid)
                self.journal.record(name, "success", seconds=time.monotonic()-started, result=value)
                return value
            identity = digest(candidate)
            previous = self.journal.acquisition(identity)
            if previous and previous.get("source") == candidate:
                self.artifacts.verify(previous["archive"])
                for asset in previous["outputs"]: self.artifacts.verify(asset)
                self.journal.record(name, "reused", seconds=time.monotonic()-started,
                                    reason="PUBLISH_RELEASE_INPUTS_UNCHANGED")
                return previous
            if self.observe_only:
                raise ReleaseBlocked("prepared", "PUBLISH_CI_ACQUISITION_REQUIRED")
            archive = self.ci.download(candidate, self.artifacts)
            outputs = self.artifacts.unpack(archive)
            value = {"source":deepcopy(candidate), "archive":archive, "outputs":outputs}
            references = self.artifacts.references(archive)
            for asset in outputs: references.extend(self.artifacts.references(asset))
            self.journal.save_acquisition(identity, value, references)
            self.journal.record(name, "success", seconds=time.monotonic()-started,
                                artifact_id=candidate["artifact_id"], outputs=len(outputs))
            return value
        results = run_dag(steps, execute, workers=context.workers, cancelled=context.cancelled or (lambda:False))
        outputs = [asset for identifier in sorted(by_id) for asset in results["artifact-"+identifier]["outputs"]]
        self.assets = self.fixed_assets + [{**asset, "raw":ArtifactContent(self.artifacts, asset)} for asset in outputs]
        if len({asset["name"].casefold() for asset in self.assets}) != len(self.assets):
            raise PublishError("PUBLISH_RELEASE_ASSET_CONFLICT")
        result = super().prepare(context)
        result["ci_artifacts"] = [candidate["artifact_id"] for candidate in self.candidates]
        return result
