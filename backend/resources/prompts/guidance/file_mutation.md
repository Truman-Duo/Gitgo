# File mutation

## Read effect state

Use the error's actual execution/effect state.

## Recompute against current bytes

A rejected compare-and-swap edit requires rereading the current target and recomputing the candidate against that version; use the returned current SHA-256 where the schema requires it. An absent edit anchor calls for a smaller current anchor, not an unconditional whole-file overwrite.

## Patch validation and uncertain effects

For PATCH_CHECK_FAILED, inspect preserved validation diagnostics and repair paths, headers and hunks, or use bounded native edits under the same checks. Patch preflight success is not semantic or lesson validation. A concurrent or uncertain post-commit change requires inspecting current bytes and receipts before retrying. Do not switch to shell or another write path to bypass a check.

## Cooperating writer lock

For FILE_MUTATION_BUSY, respect the existing writer lock. Inspect the active writer or wait through normal scheduling, then refresh the target version before a justified resubmission. Do not remove the lock or spin on identical writes.
