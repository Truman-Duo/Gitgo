# Workflow

## Correct a question input

`QUESTION_OPTION_COUNT_INVALID / GITGO-E6221` names an options field with an unsupported count. An open clarification accepts 0..3 options; a fixed choice accepts 2..3. The error gives exact allowed counts. Free-form discussion/amendment already exists in the UI. Correct and resubmit the same intended call; its questions and accompanying answer updates were not published. A parameter error does not require workflow status, reconfiguration or another user decision. `QUESTION_ANSWER_KIND_INVALID / GITGO-E6220` instead identifies the optional choice/evidence discriminator.

`QUESTION_AUTHORITY_OPTION_INVALID / GITGO-E6222` identifies a runtime action embedded in a business answer option. This is distinct from the number of options. Remove that action from the business card; the existing authorization path applies only when a concrete operation actually requires it. No authority or answer was granted.

## Read the current frontier

Read engineering_workflow status (or decision_evidence with engineering focus) for the current plan revision, ready frontier, unresolved nodes and evidence. Follow actual dependency edges; do not run every named step blindly.

## Satisfy ready nodes

Satisfy ready checks with real run_test/registered command receipts, documents with their versioned evidence, and user choices through the actual question path. For TDD, a model's claim that a test failed does not establish red evidence. An operation blocked before execution can be resubmitted after prerequisites are satisfied and its arguments/version are still current.

## Handle stale or unavailable evidence

If evidence recording fails or becomes stale, inspect the specific node and retained result; do not replay a committed mutation to repair an observer.

## Scope and proactive use

A scope change uses propose_amendment and a real user decision, not reconfiguration that silently removes requirements. Proactively configure a workflow when ordered dependencies are useful; this is not mandatory for ordinary tasks.

## Evidence example

- Appropriate: a failed registered test receipt opens the red-to-change dependency in the configured TDD workflow.
- Inappropriate: record the model's statement "the test failed" as if it were a test receipt.
