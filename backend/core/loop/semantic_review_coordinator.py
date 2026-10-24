"""Wire semantic review to billing, route state and observable outcomes."""
from .alignment_review import AlignmentReview, AlignmentReviewHooks
from .budget_runtime import reserve_provider_call
from .budget import TaskBudgetExceeded
from .provider_turn_protocol import _provider_idle_timeout
from .runtime_outcomes import _make_result, _cancellation_result, _budget_exhausted_result


class SemanticReviewCoordinator:
    def __init__(self, process, provider, instruction, route, refresh_prompt, workflow, emit, frontend):
        self.process, self.provider, self.instruction, self.route = process, provider, instruction, route
        self.refresh_prompt, self.workflow, self.emit, self.frontend = refresh_prompt, workflow, emit, frontend

    def before_attempt(self, index, client):
        process = self.process
        if process.steps_used >= process.max_steps:
            raise TaskBudgetExceeded('ALIGNMENT_STEP_BUDGET_EXHAUSTED', 'semantic checks exhausted this process step budget')
        if process.task_budget:
            reserve_provider_call(process, self.emit)
        return _provider_idle_timeout(process)

    def check(self, candidate='', proposed_questions=None, *, trigger='admission', proposed_contracts=None, proposed_files=None):
        process, session = self.process, self.process.session
        initial_route = self.route.route
        hooks = AlignmentReviewHooks(emit=self.emit, frontend_emit=self.frontend, notice=self.workflow.notice,
            refresh_prompt=self.refresh_prompt, before_attempt=self.before_attempt,
            route_notice=lambda artifact: self.route.alignment_notice(artifact, initial_route, self.emit, self.workflow.notice),
            result=lambda response, **options: _make_result(process, session, response, **options),
            cancellation=lambda: _cancellation_result(process, session),
            budget_exhausted=lambda error: _budget_exhausted_result(process, session, error))
        return AlignmentReview(process, self.provider, hooks).check(self.instruction, candidate, proposed_questions,
            trigger=trigger, proposed_contracts=proposed_contracts, proposed_files=proposed_files)
