"""Gitgo's native release recipe; developer file/CLI adapters stay here.

This recipe pins Gitgo's target/signing/feed requirements. The common engine
does not require other projects to adopt this product's package layout.
"""
from . import assets, delivery
from .checks import run_checks
from .downloads import acquire
from .plan import check_source


class GitgoNativeRecipe:
    identity = {"id": "gitgo.native-release", "version": 1}
    supported_phases = ("prepared", "draft", "published", "verified")

    def validate(self, context):
        check_source(context.plan)

    def prepare(self, context):
        plan, evidence, remote = context.plan, context.evidence, context.remote
        delivery.ci_gate(plan, evidence, remote)
        acquire(plan, evidence, remote, context.workers)
        run_checks(plan, evidence, context.workers)
        return assets.prepare(plan, evidence)

    def guard(self, context, prepared):
        delivery.guard(context.plan, prepared)

    def draft(self, context, prepared):
        return delivery.draft(context.plan, context.evidence, context.remote, prepared)

    def publish(self, context, prepared, draft):
        delivery.publish(context.plan, context.evidence, context.remote, prepared, draft)

    def verify(self, context, prepared):
        delivery.verify_public(context.plan, context.evidence, prepared)

    def result(self, phase, draft):
        if phase == "prepared":
            return {"phase": phase, "remote_writes": False}
        if phase == "draft":
            return {"phase": phase, "release_id": draft["id"]}
        return {"phase": phase, "release_id": draft["id"],
                "native_managed_install_verified": False, "formal_terminal_verified": False}
