"""Normal dialogue is owned by the main Agent, not a hidden LLM reviewer.

Registered completion/review mechanisms retain their own explicit lifecycle.
The legacy AlignmentReview remains readable for existing diagnostic consumers.
"""
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

    def check(self, candidate='', proposed_questions=None, *, trigger='admission', proposed_contracts=None, proposed_files=None, observation_only=False):
        from .main_dialogue import activate, bind_candidate
        activate(self.process, self.instruction)
        if candidate:
            bind_candidate(self.process, candidate, proposed_files)
        # A normal main-model proposal already contains its semantic choice.
        # The tool pipeline checks actual receipts, versions, graph readiness,
        # effects and permission. This boundary neither calls assess_alignment
        # nor fabricates a successful semantic/delivery review.
        return None
