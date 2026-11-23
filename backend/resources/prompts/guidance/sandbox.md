# Native execution recovery

Use the error code to find a procedure. Decide what happened using the recorded launch, effect, publication and cleanup states. Treat missing facts as unknown. Check the invocation identity before using evidence from another call. Keep completed effects and their receipts. Approval never removes native isolation.

## Earlier invocation blocks admission
Inspect the earlier invocation listed in earlier_invocations in the existing recovery details. If the original evidence is unavailable, refer the blocker to the runtime operator. Keep the current request paused until the earlier launch, cleanup or evidence is reconciled.

## Earlier outcome unknown
Cleanup of the earlier invocation was confirmed. Its business outcome is still unknown. Verify the original file or delivery receipts before replanning. Do not replay the interrupted command.

## Current cleanup unconfirmed
Inspect this invocation's retained cleanup evidence. Preserve any reported effects. Keep further native work blocked wherever the runtime reports pending cleanup.

## Current outcome requires reconciliation
Inspect this invocation's launch, effects and cleanup before replanning. If anything may have been committed, reconcile the original receipt and current target. Do not rerun to obtain a clearer result or more output.

## Confirmed launch refusal
This invocation did not start. Inspect the reported launcher, runtime or platform prerequisite before deciding whether to request a new invocation.

## WORKSPACE_VIEW_LIMIT
Reduce the private input or output scope using the tool's supported options. Check the reported limit before resubmitting.

## TEST_EXECUTION_SCOPE_INVALID
Check which requested test inputs are available in the workspace. Correct the target or explicit input files before requesting the test again.

## SANDBOX_ACCESS_JOURNAL_CAPACITY
Have the runtime operator inspect the scoped access journal and capacity. Preserve its recovery records.

## SANDBOX_ACL_MANAGEMENT_BUSY
Let the existing permissions operation finish. If the receipt confirms the current request did not start, retry within its current budget.

## SANDBOX_PRIVATE_HOME_COLLISION
Have the runtime operator inspect the reserved private-home allocation. Preserve the affected directory for inspection.

## SANDBOX_DIRECTORY_INVENTORY_CHANGED
Inspect the directory change and current object identities. Prepare a new scope only after the source is stable and outstanding effects are resolved.

## SANDBOX_SECURITY_DESCRIPTOR_CONFLICT
Have the runtime operator reconcile the changed object or permissions. Preserve intervening changes made by other owners.

## SANDBOX_HOME_PREPARATION_FAILED
Inspect the private home preparation stage and original OS error. Have the runtime operator repair the configured path or access.

## PYTHON_APPCONTAINER_TOKEN_QUERY_FAILED
Have the runtime operator inspect the child token query and original Windows error. Check the launch receipt for already created processes.

## PYTHON_APPCONTAINER_IDENTITY_INVALID
Have the runtime operator verify the actual child AppContainer identity. Any already launched child still requires cleanup evidence.

## PYTHON_PRIVATE_DACL_INVALID
Have the runtime operator repair the private directory security descriptor using the original diagnostic.

## PYTHON_PRIVATE_DIRECTORY_CREATE_DENIED
Have the runtime operator inspect the allowed parent directory and private temporary directory setup. Use the launch receipt to determine whether the child started.

## PYTHON_PRIVATE_DIRECTORY_CREATE_FAILED
Have the runtime operator inspect the original Windows error and affected directory before further attempts.

## INVOCATION_JOURNAL_UNAVAILABLE
If native launch was refused before saving its intent, restore reliable project storage before requesting a new call. Separately inspect any earlier unresolved records.

## INVOCATION_EVIDENCE_UNAVAILABLE
Recover or inspect the original invocation evidence through the supported storage or operator path. Missing evidence leaves prior launch and effects unknown.

## GIT_BOUNDARY_UNAVAILABLE
Inspect the protected Git backend and its prerequisites. Use supported protected operations when the backend is ready.

## GIT_CONFIG_UNSUPPORTED
Inspect the reported repository configuration or required filter. Report the unsupported configuration without applying content that requires that transformation.

## GIT_COMMAND_UNSUPPORTED
Choose a supported operation in the protected Git workflow. Preserve the requested scope and existing approvals.

## GIT_EXECUTABLE_UNAVAILABLE
Have the runtime operator repair the trusted Git installation, then recheck the protected operation.

## GIT_OBJECT_VIEW_UNSUPPORTED
Inspect the protected object or query scope. Narrow the query if the supported view can still answer it.

## GIT_READ_UNCONFIRMED
Inspect the original Git invocation and cleanup evidence. An unverified query leaves the answer unknown.

## HELPER_GRANT_INVALID
Renew the project's authorized helper binding through the operator. Repeated tool approval does not renew a helper grant.

## HELPER_MAINTENANCE
Wait for the helper's current maintenance to complete, or have the operator reconcile the interrupted maintenance.

## HELPER_UPGRADE_REQUIRED
Have the operator install a helper compatible with the current operation contract. Repeated tool approval does not change compatibility.

## SANDBOX_CAPABILITY_UNSUPPORTED
Use the current tool and platform capability facts to select a supported operation. Changes to approved arguments or script syntax go through normal admission again.

## SANDBOX_UNAVAILABLE
Inspect the required platform backend and have the runtime operator restore its prerequisites. Use an already supported operation if it can meet the same goal.

## SANDBOX_LAUNCH_DENIED
If the receipt confirms no launch and no unresolved cleanup, inspect the launcher, runtime or platform prerequisites. If a process started or its launch is uncertain, inspect its effects and cleanup before trying a new invocation.

## SANDBOX_PREPARATION_FAILED
Use the reported preparation stage and OS cause to repair the runtime or resource scope. Check the launch and cleanup receipts before a new request.

## SANDBOX_POLICY_INVALID
Inspect the rejected workspace scope or execution contract. Have the runtime operator correct invalid deployment policy.

## SANDBOX_CLEANUP_PENDING
Check whether the receipt concerns this invocation or an earlier blocker. Reconcile the identified invocation through the supported recovery path before further blocked native work.

## NATIVE_LAUNCH_UNCONFIRMED
Inspect the original invocation's launch and effect evidence. An intent without a settled receipt leaves execution unknown.

## NATIVE_CLEANUP_UNCONFIRMED
Inspect the original invocation's process retirement and access cleanup evidence. Preserve the recovery record until cleanup is verified.

## NATIVE_OUTCOME_UNCONFIRMED
If cleanup was confirmed but the business result remains unknown, inspect the original effects and receipts before planning any new action.

## SANDBOX_EXECUTION_FAILED
Inspect the original launch, effects and cleanup. A missing or invalid result does not establish that the command had no effect.

## SANDBOX_OUTPUT_LIMIT
Inspect the effects and cleanup using retained evidence. If a fresh call is appropriate, narrow the workload or output.

## TOOL_TIMEOUT
Inspect retained effects and cleanup before planning a bounded new call. Timeout does not establish rollback.

## TOOL_CRASH
Inspect the original invocation and its retained effects and cleanup before replanning.

## TOOL_CANCELLED
Use the confirmed launch and cleanup states. Preserve any committed or partial effects and follow the user's current direction.
