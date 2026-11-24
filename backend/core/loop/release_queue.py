"""Local-operator release continuation owned by the existing Publish Runtime."""
from pathlib import Path
import time

from backend.core.publish.release_engine import ReleaseContext, ReleaseEngine, ReleaseBlocked, ReleaseCancelled
from backend.core.publish.release_journal import ProjectReleaseJournal, plan_for
from backend.core.publish.state import Repository, PublishError, find_item, digest
from backend.core.storage import get_storage


def drive_release_job(project, item_id, job_id, cancelled=lambda: False):
    from backend.core.application.services import ApplicationServices
    from backend.core.publish.service import PublishService
    from backend.core.publish.distribution import _package, _blob
    from backend.core.publish.package_release_recipe import PackageReleaseRecipe
    from backend.core.publish.ci_release_recipe import CIReleaseRecipe
    from backend.core.publish.native_release_recipe import NativeReleaseRecipe
    from backend.core.publish.release_artifacts import ReleaseArtifacts
    from backend.core.publish.release_transport import ReleaseTransport
    from backend.remote.publish_platform import PlatformClient, PlatformTarget
    from backend.remote.publish_accounts import resolve_secret

    repo = Repository(project.workspace_path, get_storage(project.workspace_path))
    journal = ProjectReleaseJournal(repo, item_id, job_id)
    artifact_store = None
    started = time.monotonic()
    try:
        with journal.locked():
            ledger, _ = repo.load()
            item = find_item(ledger, item_id)
            plan, job = plan_for(repo, item), journal.current()
            admission = job.get("admission", {})
            if (job["state"] not in {"queued", "running", "stopping"}
                    or admission.get("actor") != "local_operator"
                    or admission.get("plan_binding") != job["plan_binding"]
                    or admission.get("through") != job["through"]):
                raise PublishError("PUBLISH_RELEASE_ADMISSION_INVALID")
            journal.record("run", "running", through=job["through"], recipe=plan["recipe"])

            def validate_source():
                current = ApplicationServices._publish_project(project.name)
                if Path(current.workspace_path).resolve() != Path(project.workspace_path).resolve():
                    raise PublishError("PUBLISH_CONTEXT_UNAVAILABLE")
                saved, _ = repo.load()
                candidate = find_item(saved, item_id)
                if plan_for(repo, candidate) != plan or plan["recipe"] not in (PackageReleaseRecipe.identity, CIReleaseRecipe.identity, NativeReleaseRecipe.identity):
                    raise PublishError("PUBLISH_RELEASE_PLAN_STALE")
                service = PublishService(current, actor="local_operator")
                service.repo = repo
                if digest(_package(candidate, plan["package"]["id"])) != digest(plan["package"]):
                    raise PublishError("PUBLISH_PACKAGE_STALE")
                # Reconciliation reads the immutable admitted package and
                # fixed remote scope. A moved local branch cannot erase an
                # earlier effect; fresh execution still checks current source.
                if not job["observe_only"]:
                    _, oid = service._accepted_source(candidate)
                    if oid != plan["package"]["source_oid"]:
                        raise PublishError("PUBLISH_PACKAGE_STALE")

            validate_source()
            package = plan["package"]
            assets = [{**row, "raw": _blob(repo.storage, row["blob_ref"])} for row in package["assets"]]
            target = PlatformTarget.parse(plan["target"])
            remote = journal.remote()
            def is_cancelled():
                return cancelled() or journal.cancelled()
            if is_cancelled():
                raise ReleaseCancelled("admission")
            with PlatformClient(target, resolve_secret(target)) as client:
                extra = {}
                recipe_type = PackageReleaseRecipe
                if plan["recipe"] in (CIReleaseRecipe.identity,NativeReleaseRecipe.identity):
                    recipe_type = CIReleaseRecipe if plan["recipe"]==CIReleaseRecipe.identity else NativeReleaseRecipe
                    # The item lease proves older job workers have retired.
                    # A crash may leave only temporary roots, never new truth.
                    for old in item["release_controller"].get("history", []):
                        ReleaseArtifacts.release_pins(repo.storage, old["id"])
                    artifact_store = ReleaseArtifacts(repo.storage, job_id, cancelled=is_cancelled)
                    extra = {"candidates":plan["ci_artifacts"], "artifacts":artifact_store, "journal":journal}
                    if recipe_type is NativeReleaseRecipe:
                        from backend.core.publish.release_signing_keys import resolve
                        extra.update(native=plan["native"],signing_key=lambda:resolve(repo.storage,target,plan["native"]["signing"]))
                recipe = recipe_type(ReleaseTransport(client, cancelled=is_cancelled, progress=journal.transfer), package, assets, remote, journal.save_remote,
                    observe_only=job["observe_only"], body=plan["body"], source_validator=validate_source,
                    anonymous=plan.get("capabilities", {}).get("anonymous_delivery_verification", False), **extra)
                # The outer lease also covers credential resolution and
                # source validation. advance avoids acquiring another lease.
                context = ReleaseContext(plan, journal, client, cancelled=is_cancelled)
                try:
                    result = ReleaseEngine(recipe).advance(context, job["through"])
                except ReleaseBlocked as exc:
                    journal.record("run", "blocked", reason=exc.reason, phase=exc.phase,seconds=time.monotonic()-started)
                except ReleaseCancelled as exc:
                    journal.record("run", "cancelled", before_phase=exc.before_phase,seconds=time.monotonic()-started)
                else:
                    journal.record("run", "success", result=result,seconds=time.monotonic()-started)
    except ReleaseCancelled as exc:
        journal.record("run", "cancelled", before_phase=exc.before_phase,seconds=time.monotonic()-started)
    except Exception as exc:
        # Keep the exact current job; never dispatch a second mutation because
        # a response or observer failed. A fresh explicit recovery is read-only.
        # Adapter/programming failures must also retire the queued/running
        # state; the executor's Future is not a durable status authority.
        if getattr(exc, "code", "") in {"PUBLISH_OPERATION_RUNNING", "PUBLISH_RELEASE_JOB_CHANGED"}:
            return
        journal.record("run", "failed", code=getattr(exc, "code", "PUBLISH_RELEASE_OPERATION_UNCERTAIN"), error=type(exc).__name__,seconds=time.monotonic()-started)
    finally:
        if artifact_store is not None:
            artifact_store.close()
