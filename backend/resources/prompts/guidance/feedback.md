# Feedback

## Identify the result and effects

Read the diagnostic's source, concrete arguments and captured result. Separate a blocked operation, a failed command, a completed effect with an observer warning, and an uncertain effect. Missing effect facts mean unknown, not no change. Inspect durable state before another potentially duplicate effect.

## Choose a bounded recovery

Use the current schema to fix invalid arguments. Producer next_actions are specific to this occurrence; a catalog's retryable flag only says recovery may be possible, not that replay is safe. Change the cause or strategy within a bounded recovery attempt. Never bypass the same restriction through a different executor.

## Query or ask only when needed

Consult the narrow decision_evidence focus only for a material gap. Ask the user only for a choice or authorization that cannot be resolved from available evidence.

## Timeout and cancellation

A timeout does not establish that an effect was undone. Cancellation is not authority to restart: preserve observed effects and follow the actual user/runtime continuation state.
