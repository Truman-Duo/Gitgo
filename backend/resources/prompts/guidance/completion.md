# Completion

## Read current gates

Use decision_evidence with gates or completion focus for current gates and recorded receipts. A successful operation is not proof of task completion.

## Satisfy missing evidence

Address each actual missing fact, then submit complete_task or the completion protocol mounted for this actor/task. Reuse successful receipts; do not rerun a write to obtain another success. A required delegated outcome cannot be replaced by pretending the supervisor performed it. Required artifacts must actually exist with the agreed content.

A required tool-call count can be satisfied by a successfully observed read-only command; a formal file commit is a separate fact. Do not create or delete an unrelated file just to satisfy a call-count gate. If a successful observation is reported missing, inspect the current receipt and gate details rather than broadening the user's task.

## Scope and terminal decisions

A partial-delivery exception uses the runtime's genuine decision path, bound to current gates and contract; it is not a model-selected waiver. Stop truthfully when a non-waivable requirement cannot be met. Look up review or coordination guidance only if that is the actual missing evidence.

## Validation freshness

A passing test exit is a historical observation until its input-version witness is current. Later file changes, a different target under the same test ID, missing legacy witnesses, or failed capture require revalidation. Rerun the actual required target; do not relabel old results. If bounded workspace capture is unavailable, run_test accepts an explicit input_files scope including relevant source/config/data dependencies; the target is always included. Scope selection is a semantic responsibility and does not prove that every implicit or external dependency was discovered.
