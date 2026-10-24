# Provider

## Runtime owns continuation

The model-access and task runtimes own transport retries, incomplete-output continuation, failed-turn quarantine, provider switching and their bounded limits. Follow the current continuation request; do not create a second unbounded retry loop.

## Complete tool arguments

Invalid or truncated tool arguments must be resubmitted as one complete object matching the current schema. Partial calls are not execution evidence.

## Preserve terminal and effect facts

If the runtime has stopped or is waiting for a real user decision, help lookup does not restart it or select that answer. Prior tool effects remain separate from provider output failure: inspect retained receipts and state before replaying a mutation. Failed-turn recovery text may be quarantined from later provider input while its audit records remain; do not reinsert it as a user requirement.

## Check material provider limits

Proactively use capability_status for a material provider capability gap rather than assuming all models expose the same tools or modalities.
