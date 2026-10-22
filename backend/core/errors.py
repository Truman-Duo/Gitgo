"""Canonical Gitgo error catalog and recoverable wire payloads.

Symbolic names remain the compatibility key used by existing receipts and
Storm Break.  ``catalog_id`` is the stable, language-neutral documentation and
telemetry identifier; ``occurrence_id`` identifies one concrete failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorDefinition:
    catalog_id: str
    name: str
    default_message: str
    retryable: bool
    recovery_class: str
    procedure: str = "feedback"


_DEFINITIONS = (
    ErrorDefinition("GITGO-E3816", "TEST_PROTOCOL_INVALID",
                    "The registered test helper returned unverified seed results; no test record was saved.",
                    False, "inspect_test_runtime_protocol", procedure="feedback"),
    ErrorDefinition("GITGO-E3812", "GIT_BOUNDARY_UNAVAILABLE",
                    "Protected trusted Git is unavailable; the command was refused without an unrestricted fallback.",
                    False, "restore_protected_git_backend", procedure="sandbox"),
    ErrorDefinition("GITGO-E3813", "GIT_CONFIG_UNSUPPORTED",
                    "Repository configuration cannot be protected for trusted Git execution.",
                    False, "inspect_git_config_without_disabling_protection", procedure="sandbox"),
    ErrorDefinition("GITGO-E3814", "GIT_COMMAND_UNSUPPORTED",
                    "The trusted worktree service does not support this command or configuration override.",
                    False, "correct_internal_git_invocation", procedure="sandbox"),
    ErrorDefinition("GITGO-E3815", "GIT_EXECUTABLE_UNAVAILABLE",
                    "A Git executable could not be resolved from the operator PATH outside the execution workspace.",
                    False, "repair_trusted_git_installation", procedure="sandbox"),
    ErrorDefinition("GITGO-E3118", "CHILD_ARTIFACT_NOT_READY",
                    "This child has no completed artifact available. Inspect its current state; wait for active work or review the retained failure before continuing.",
                    True, "inspect_child_state_before_reading", procedure="coordination"),
    ErrorDefinition("GITGO-E6210", "LESSON_FEEDBACK_REQUIRED",
                    "Relevant lesson advice is available before writing; the target is unchanged.",
                    True, "consider_advice_then_resubmit", procedure="lesson"),
    ErrorDefinition("GITGO-E7604", "WORKBENCH_SOURCE_UNAVAILABLE",
                    "The context source is missing or failed integrity resolution.",
                    True, "refresh_source_through_original_producer", procedure="workbench"),
    ErrorDefinition("GITGO-E7605", "WORKBENCH_INVALID_DATA",
                    "The snapshot or requested calculation violates the declared data contract.",
                    True, "inspect_input_types_and_query", procedure="workbench"),
    ErrorDefinition("GITGO-E7601", "WORKBENCH_REVISION_CONFLICT",
                    "The sheet changed; open its current revision before revising.",
                    True, "open_current_sheet_then_reconcile", procedure="workbench"),
    ErrorDefinition("GITGO-E7602", "WORKBENCH_INVALID_INPUT",
                    "The sheet request is invalid or does not belong to this session/process.",
                    True, "inspect_sheet_and_correct_arguments", procedure="workbench"),
    ErrorDefinition("GITGO-E7603", "WORKBENCH_UNAVAILABLE",
                    "Persistent sheets require a bound storage runtime and document dependencies.",
                    True, "restore_workbench_dependencies_or_use_other_available_tools", procedure="workbench"),
    ErrorDefinition("GITGO-E3701", "BASH_IDENTITY_UNVERIFIED", "Bash identity could not be verified; the script was not executed.", True, "repair_bash_installation_then_retry", procedure="command"),
    ErrorDefinition("GITGO-E3702", "TERMINAL_IDENTITY_UNVERIFIED", "Terminal identity could not be verified; continuing in the current terminal.", True, "rescan_or_select_verified_terminal", procedure="command"),
    ErrorDefinition("GITGO-E3703", "TERMINAL_UNAVAILABLE", "The selected terminal is missing or unverified; continuing in the current terminal.", True, "rescan_or_select_verified_terminal", procedure="command"),
    ErrorDefinition("GITGO-E3601", "SEARCH_ENGINE_REQUIRED", "This query requires ripgrep; repair the engine or explicitly narrow the query.", True, "repair_search_engine_or_narrow_query", procedure="external_evidence"),
    ErrorDefinition("GITGO-E3602", "SEARCH_INCOMPLETE", "Search did not cover its declared scope; absence cannot be inferred from partial results.", True, "narrow_search_scope", procedure="external_evidence"),
    ErrorDefinition("GITGO-E3603", "INVALID_SEARCH_ARGUMENTS", "Search arguments are invalid or exceed their bounded limits.", True, "correct_search_arguments", procedure="external_evidence"),
    ErrorDefinition("GITGO-E3604", "INVALID_REGEX", "The search regular expression is invalid for ripgrep.", True, "correct_pattern_or_use_literal", procedure="external_evidence"),
    ErrorDefinition("GITGO-E3605", "SEARCH_ERROR", "The search engine failed; inspect the preserved diagnostic.", True, "inspect_search_diagnostic", procedure="external_evidence"),
    ErrorDefinition("GITGO-E3807", "SANDBOX_CAPABILITY_UNSUPPORTED",
                    "The current native backend does not support the requested capability.",
                    False, "use_supported_native_capability", procedure="sandbox"),
    ErrorDefinition("GITGO-E3801", "SANDBOX_UNAVAILABLE",
                    "Native isolation is unavailable; execution was refused.",
                    False, "configure_native_sandbox", procedure="sandbox"),
    ErrorDefinition("GITGO-E3802", "SANDBOX_LAUNCH_DENIED",
                    "The OS refused the sandbox launch; execution was refused.",
                    False, "configure_native_sandbox", procedure="sandbox"),
    ErrorDefinition("GITGO-E3803", "SANDBOX_POLICY_INVALID",
                    "The Host sandbox policy is invalid; execution was refused.",
                    False, "correct_sandbox_policy", procedure="sandbox"),
    ErrorDefinition("GITGO-E3806", "SANDBOX_CLEANUP_PENDING",
                    "Earlier native process cleanup is unconfirmed; new execution is blocked.",
                    False, "inspect_native_cleanup", procedure="sandbox"),
    ErrorDefinition("GITGO-E3805", "SANDBOX_EXECUTION_FAILED",
                    "The sandboxed runtime exited without a result; side effects are unknown.",
                    False, "inspect_sandbox_runtime", procedure="sandbox"),
    ErrorDefinition("GITGO-E3804", "SANDBOX_OUTPUT_LIMIT",
                    "The sandbox output budget was exceeded; the process tree was terminated.",
                    False, "reduce_tool_output", procedure="sandbox"),
    ErrorDefinition("GITGO-E6201", "ENGINEERING_WORKFLOW_RECOVERY_REQUIRED",
                    "Engineering evidence could not be recorded; existing requirements remain active.",
                    True, "inspect_workflow_frontier_or_amend_scope", procedure="workflow"),
    ErrorDefinition("GITGO-E6202", "ENGINEERING_PREREQUISITE_REQUIRED",
                    "The operation requires current engineering prerequisite evidence.",
                    True, "satisfy_ready_workflow_nodes", procedure="workflow"),
    ErrorDefinition("GITGO-E3106", "PROCESS_PRESENTATION_INVALID",
                    "Select an existing B process and provide a valid display name or archive action.",
                    True, "correct_process_selection_or_name", procedure="coordination"),
    ErrorDefinition("GITGO-E7401", "STORAGE_UNSAFE_SQLITE_VERSION",
                    "The loaded SQLite engine has a known WAL corruption vulnerability.",
                    False, "select_patched_runtime", procedure="storage"),
    ErrorDefinition("GITGO-E7402", "STORAGE_CORRUPTION",
                    "Persistent state is damaged; an explicit verified recovery is required.",
                    False, "backup_then_recover", procedure="storage"),
    ErrorDefinition("GITGO-E7403", "STORAGE_LOCATION_CONFLICT",
                    "The database family is redirected, split or has ambiguous storage locations.",
                    False, "inspect_and_relocate_storage", procedure="storage"),
    ErrorDefinition("GITGO-E7404", "STORAGE_MAINTENANCE_BUSY",
                    "Storage maintenance requires all project runtimes to release their leases.",
                    True, "close_runtimes_then_retry", procedure="storage"),
    ErrorDefinition("GITGO-E7405", "STORAGE_BLOCKED",
                    "Storage cannot safely accept the operation.",
                    False, "inspect_storage_health", procedure="storage"),
    ErrorDefinition("GITGO-E7406", "STORAGE_CAS_REFERENCE_MISSING",
                    "An authoritative content-addressed object is missing.",
                    False, "restore_verified_object_or_continue_from_safe_checkpoint", procedure="storage"),
    ErrorDefinition("GITGO-E7201", "DAEMON_COMMAND_FAILED",
                    "The daemon could not complete this command, but remains available.",
                    True, "inspect_diagnostic_then_retry_or_recover", procedure="storage"),
    ErrorDefinition("GITGO-E7407", "STORAGE_COMMIT_UNKNOWN", "The authoritative commit acknowledgement was lost. Preserve effects and the original evidence; do not replay this operation.", False, "inspect_authoritative_commit_before_new_intent", "storage"),
    ErrorDefinition("GITGO-E7410", "GOVERNANCE_STATE_CHANGED", "Another writer changed governance metadata. A reserved number may remain unused; reload current state before a new intent.", True, "reload_current_governance_state", "storage"),
    ErrorDefinition("GITGO-E7411", "GOVERNANCE_STATE_UNAVAILABLE", "Governance state or its legacy source could not be read safely; no empty successful state was inferred.", False, "inspect_project_state_source", "storage"),
    ErrorDefinition("GITGO-E7412", "GOVERNANCE_WORKSPACE_MISMATCH", "This governance operation belongs to the owning project workspace. A child workspace cannot mutate its parent's state through this tool.", False, "ask_owning_project_supervisor", "coordination"),
    ErrorDefinition("GITGO-E7413", "FORMALIZE_FAILED", "No formal metadata entry was created. Inspect selected commits and current project state.", True, "inspect_formal_selection", "feedback"),
    ErrorDefinition("GITGO-E7414", "FORMALIZE_INVALID_SELECTION", "Formal-commit indices must select entries in the current workspace Git history.", True, "inspect_current_commit_selection", "feedback"),
    ErrorDefinition("GITGO-E7420", "MEMORY_ARCHIVE_UNAVAILABLE", "Tool-memory archive or restore inputs cannot be safely read or published. Inspect bounded source data and existing evidence before a new intent.", False, "inspect_memory_archive_state", "storage"),
    ErrorDefinition("GITGO-E7421", "MEMORY_BACKUP_UNCONFIGURED", "A project-configured backup node is required for tool-memory snapshots and restore.", False, "configure_project_backup", "workflow"),
    ErrorDefinition("GITGO-E7430", "METADATA_FILE_CHANGED", "Product metadata changed after it was read. Reload the current source before a new edit.", True, "reload_current_metadata_source", "storage"),
    ErrorDefinition("GITGO-E7431", "METADATA_COMMIT_UNKNOWN", "Metadata replacement acknowledgement is unknown. Inspect its current bytes and the original intent before any retry.", False, "inspect_metadata_before_retry", "storage"),
    ErrorDefinition("GITGO-E7432", "CONTRACT_STATE_UNAVAILABLE", "The configured project contract could not be safely read or parsed. This is not an absent contract or a successful drift check.", False, "inspect_current_contract_source", "storage"),
    ErrorDefinition("GITGO-E3105", "CONTINUATION_STATE_UNAVAILABLE",
                    "The previous agent ownership or session checkpoint cannot be restored safely.",
                    False, "inspect_storage_or_user_decision", procedure="admission"),
    ErrorDefinition("GITGO-E5301", "TASK_TREE_DEADLINE_EXCEEDED",
                    "The admitted task-tree time allowance is exhausted; completed writes are preserved, not certified as task completion.",
                    False, "inspect_evidence_then_continue_session", procedure="feedback"),
    ErrorDefinition(
        "GITGO-E3101", "CAPABILITY_PROFILE_UNKNOWN",
        "The requested capability profile does not exist.", True,
        "automatic_or_llm_retry",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3102", "CAPABILITY_PROFILE_NOT_WORKER",
        "The requested capability profile cannot be used for a B Agent.", True,
        "automatic_or_llm_retry",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3103", "DELEGATION_ADMISSION_FAILED",
        "The B Agent could not be admitted.", True, "llm_retry_or_user_decision",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3104", "DELEGATION_EMPTY_RESULT",
        "Delegation produced no child Agent.", True, "switch_delegation_strategy",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3108", "TOOL_NOT_FOUND",
        "The requested tool is not available in the current task capability set.",
        True, "refresh_capabilities_or_choose_available_tool",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3109", "TOOL_CALL_STORM_BLOCKED",
        "The Host stopped a repeated failing tool call before execution.",
        True, "inspect_error_then_change_arguments_or_strategy",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3115", "PROVIDER_CAPABILITY_UNAVAILABLE",
        "The active provider or API plan does not expose the requested capability.",
        False, "choose_provider_fallback_or_continue_without_capability",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3110", "TOOL_NOT_STARTED",
        "The tool call was not started because its provider turn was rolled back.",
        True, "inspect_current_state_then_retry_if_safe",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E3111", "COORDINATION_EVENT_NOT_FOUND",
        "The requested coordination event is not owned by this supervisor.",
        True, "list_coordination_events_then_retry",
     procedure="coordination"),
    ErrorDefinition(
        "GITGO-E3112", "COORDINATION_RESOLUTION_INVALID",
        "The requested coordination resolution is not valid for this event.",
        True, "inspect_event_then_choose_valid_resolution",
     procedure="coordination"),
    ErrorDefinition(
        "GITGO-E3113", "INTERFACE_REVISION_PENDING",
        "A downstream Agent is waiting for its supervisor to resolve an interface revision.",
        True, "supervisor_resolve_or_ask_user",
     procedure="coordination"),
    ErrorDefinition(
        "GITGO-E3114", "SELF_EXECUTION_LEASE_REQUIRED",
        "The main process selected self execution but has not yet requested its task-scoped execution lease.",
        True, "request_self_execute_then_retry",
     procedure="admission"),
    ErrorDefinition(
        "GITGO-E6101", "REQUIRED_DELEGATION_UNSATISFIED",
        "The task contract requires a delegated B outcome.", False,
        "continue_or_fail_truthfully",
     procedure="completion"),
    ErrorDefinition(
        "GITGO-E6102", "REQUIRED_ARTIFACT_MISSING",
        "A required workspace artifact is missing.", True,
        "llm_retry_or_user_decision",
     procedure="completion"),
    ErrorDefinition(
        "GITGO-E6103", "COMPLETION_EVIDENCE_INSUFFICIENT",
        "The Host does not have enough evidence to accept completion.", True,
        "continue_work",
     procedure="completion"),
    ErrorDefinition(
        "GITGO-E6104", "FAILURE_EVIDENCE_INSUFFICIENT",
        "The Host does not have enough evidence to accept terminal failure.", True,
        "continue_or_request_user",
     procedure="completion"),
    ErrorDefinition(
        "GITGO-E6105", "COMPLETION_EXCEPTION_DECISION_REQUIRED",
        "The same completion gates remain unresolved after a bounded recovery attempt.",
        True, "continue_accept_partial_or_stop",
     procedure="completion"),
    ErrorDefinition(
        "GITGO-E5201", "CONTEXT_WINDOW_EXCEEDED",
        "The provider rejected the request because its context limit was exceeded.",
        True, "compact_then_retry",
     procedure="context"),
    ErrorDefinition(
        "GITGO-E5202", "CONTEXT_COMPACTION_FAILED",
        "Ordinary context compaction did not produce a smaller valid epoch.",
        True, "retry_then_user_decision",
     procedure="context"),
    ErrorDefinition(
        "GITGO-E5203", "CONTEXT_FORCE_COMPACTION_REQUIRED",
        "Three ordinary compaction attempts failed; destructive compaction requires user approval.",
        False, "user_decision",
     procedure="context"),
    ErrorDefinition(
        "GITGO-E5204", "CONTEXT_LIMIT_CONFIGURATION_INVALID",
        "The configured context limit is invalid or exceeds the provider's observed limit.",
        True, "correct_configuration_or_compact",
     procedure="context"),
    ErrorDefinition(
        "GITGO-E3201", "RESOURCE_SCOPE_APPROVAL_REQUIRED",
        "The requested resource is outside the Agent's current task scope.",
        True, "request_user_permission",
     procedure="permission"),
    ErrorDefinition(
        "GITGO-E3202", "RESOURCE_SCOPE_APPROVAL_DENIED",
        "The user did not grant the requested resource scope.",
        False, "adjust_plan_or_stop_truthfully",
     procedure="permission"),
    ErrorDefinition(
        "GITGO-E3203", "APPROVAL_GRANT_INVALID",
        "The approval grant does not match this tool invocation.",
        True, "request_precise_user_permission",
     procedure="permission"),
    ErrorDefinition(
        "GITGO-E3204", "SENSITIVE_TOOL_APPROVAL_REQUIRED",
        "This sensitive tool requires approval for the exact invocation.",
        True, "request_exact_user_permission",
     procedure="permission"),
    ErrorDefinition(
        "GITGO-E3205", "TOOL_PERMISSION_DENIED",
        "The user denied this exact tool invocation.",
        False, "change_strategy_or_stop_truthfully",
     procedure="permission"),
    ErrorDefinition(
        "GITGO-E3301", "CUSTOM_TOOL_INVALID",
        "The custom tool definition is invalid or failed its registration tests.",
        True, "inspect_validation_details_then_revise",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3302", "CUSTOM_TOOL_EXISTS",
        "A saved custom tool already uses this name.",
        True, "replace_existing_version_or_choose_name",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3303", "CUSTOM_TOOL_NOT_FOUND",
        "The requested custom tool or version is not available.",
        True, "list_saved_tools_then_correct_selection",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3304", "CUSTOM_TOOL_ARCHIVED",
        "The requested custom tool is archived and cannot be mounted.",
        True, "restore_then_mount",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3305", "CUSTOM_TOOL_PRIVACY_BLOCKED",
        "The custom tool source failed the content-level privacy boundary.",
        False, "remove_sensitive_content_then_register",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3306", "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED",
        "The saved custom tool source does not match its immutable digest.",
        False, "stop_using_version_and_inspect_storage",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3307", "CUSTOM_TOOL_CATALOG_FAILED",
        "The project custom-tool catalog could not be read.",
        True, "inspect_storage_health_then_retry",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3308", "CUSTOM_TOOL_STATE_CHANGE_FAILED",
        "The custom tool state could not be changed safely.",
        True, "refresh_catalog_then_retry",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3309", "CUSTOM_TOOL_PRIVILEGED_APPROVAL_REQUIRED",
        "A privileged custom-tool version requires approval bound to its exact source and registration contract.",
        True, "request_exact_user_permission",
     procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3310", "CUSTOM_TOOL_CAPABILITY_REQUIRED",
        "The saved tool requires leaf capabilities outside the current task surface.",
        True, "request_bounded_capability_then_mount",
     procedure="custom_tools"),
    ErrorDefinition("GITGO-E3312", "CUSTOM_TOOL_EXECUTION_FAILED",
        "The custom program reported failure; private outputs were not published.",
        False, "inspect_output_before_revision", procedure="custom_tools"),
    ErrorDefinition(
        "GITGO-E3401", "DOCUMENT_FORMAT_UNSUPPORTED",
        "The document format is unsupported or its adapter is unavailable.",
        True, "convert_document_or_install_adapter",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3402", "DOCUMENT_READ_FAILED",
        "The document could not be read safely.",
        True, "inspect_document_then_retry_or_convert",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3405", "DOCUMENT_WRITE_FAILED",
        "The document could not be created or replaced safely.",
        True, "correct_output_specification_then_retry",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3403", "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
        "No public web search provider is configured for this Host.",
        False, "configure_search_provider_or_continue_offline",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3404", "WEB_SEARCH_QUERY_REQUIRED",
        "A non-empty public web search query is required.",
        True, "provide_query",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3406", "WEB_SEARCH_FAILED",
        "The configured public web search provider could not return a valid response.",
        True, "inspect_provider_then_retry_or_continue_offline",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3407", "WEB_FETCH_BLOCKED_URL",
        "The requested page is outside the anonymous public-fetch safety boundary.",
        False, "choose_public_http_url",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3408", "WEB_FETCH_FAILED",
        "The selected public page could not be retrieved within the fetch contract.",
        True, "retry_or_choose_another_source",
     procedure="external_evidence"),
    ErrorDefinition(
        "GITGO-E3501", "COMMAND_EXIT_NONZERO",
        "The command ran and returned a non-zero exit code. Inspect its captured stdout and stderr before changing the command, the implementation, or the test.",
        True, "inspect_command_output_then_fix_or_change_strategy",
     procedure="command"),
    ErrorDefinition(
        "GITGO-E3502", "FILE_EXISTS",
        "The target already exists. Replace it only with an explicit compare-and-swap write using its current SHA-256.",
        True, "reread_then_replace_with_expected_hash",
     procedure="file_mutation"),
    ErrorDefinition(
        "GITGO-E3503", "STRING_NOT_FOUND",
        "The exact edit anchor is absent from the current file version.",
        True, "reread_then_use_a_small_current_anchor",
     procedure="file_mutation"),
    ErrorDefinition(
        "GITGO-E3504", "PATCH_CHECK_FAILED",
        "The unified diff failed preflight validation; no file was changed.",
        True, "repair_headers_and_hunks_or_use_bounded_edits",
     procedure="file_mutation"),
    ErrorDefinition(
        "GITGO-E3505", "FILE_CHANGED",
        "The file changed after it was read, so the compare-and-swap write was rejected.",
        True, "reread_then_recompute_the_edit",
     procedure="file_mutation"),
    ErrorDefinition(
        "GITGO-E3506", "EXPECTED_HASH_REQUIRED",
        "Replacing or deleting an existing file requires its current SHA-256 from read_file.",
        True, "read_file_then_retry_with_expected_hash",
     procedure="file_mutation"),
    ErrorDefinition(
        "GITGO-E5205", "TOOL_RESULT_LOCATOR_INVALID",
        "The oversized tool-result locator is invalid, unavailable or outside this task lineage.",
        True, "reuse_current_locator_or_rerun_source_tool",
     procedure="context"),
)

_DEFINITIONS += (
    ErrorDefinition("GITGO-E3107", "SUPERVISOR_BUSY",
                    "A already has an active turn. Wait or explicitly interrupt it before creating B.",
                    True, "wait_or_user_decision", procedure="admission"),
    ErrorDefinition("GITGO-E7501", "DELETION_BLOCKED",
                    "Deletion cannot safely proceed; inspect its plan before retrying.",
                    True, "inspect_then_retry", procedure="deletion"),
    ErrorDefinition("GITGO-E7502", "DELETION_CONFIRMATION_REQUIRED",
                    "Deletion requires an explicit, current target preview and confirmation.",
                    False, "preview_then_user_confirmation", procedure="deletion"),
)

_DEFINITIONS += (
    ErrorDefinition("GITGO-E3507", "INVALID_TOOL_ARGUMENTS", "The arguments do not match the current tool schema.", True, "correct_arguments_against_current_schema", "feedback"),
    ErrorDefinition("GITGO-E6203", "ALIGNMENT_REVIEW_REQUIRED", "Current requirement evidence needs semantic review before this effect.", True, "refresh_requirement_review", "alignment"),
    ErrorDefinition("GITGO-E6301", "KNOWN_LESSON_VIOLATION", "A registered lesson check rejected this candidate before commit.", True, "revise_candidate_against_lesson_diagnostic", "lesson"),
    ErrorDefinition("GITGO-E6302", "LESSON_CHECK_UNAVAILABLE", "The required lesson check could not evaluate this candidate.", True, "restore_check_then_resubmit_current_candidate", "lesson"),
    ErrorDefinition("GITGO-E6106", "INDEPENDENT_REVIEW_REQUIRED", "The active verification policy requires independent review evidence.", True, "request_review_then_resolve_findings", "review"),
    ErrorDefinition("GITGO-E6107", "CHILD_REVIEW_REQUIRED", "A required child outcome lacks its supervisor's structured approval.", True, "inspect_child_then_review_outcome", "coordination"),
    ErrorDefinition("GITGO-E6108", "CHILD_PROMOTION_REQUIRED", "Approved isolated child changes have not been promoted.", True, "promote_reviewed_child_changes", "coordination"),
    ErrorDefinition("GITGO-E6110", "VALIDATION_INPUT_CHANGED", "A pinned validation input changed or cannot be verified; recheck before adopting this result.", True, "inspect_changed_input_then_revalidate", "coordination"),
)

_DEFINITIONS += (
    ErrorDefinition("GITGO-E3116", "NO_REGISTRY", 'No tool registry is assigned to this process.', False, "restore_process_capability_registry", "admission"),
    ErrorDefinition("GITGO-E3117", "TOOL_NOT_IN_REGISTRY", 'The tool is outside this process registry.', True, "inspect_current_capabilities", "admission"),
    ErrorDefinition("GITGO-E3206", "TOOL_DENIED", 'The execution contract denies this operation.', False, "choose_an_authorized_strategy", "permission"),
    ErrorDefinition("GITGO-E3207", "TOOL_GOVERNANCE_FAILED", 'The synchronous governance check could not evaluate this operation.', True, "restore_governance_check", "feedback"),
    ErrorDefinition("GITGO-E3208", "TOOL_BLOCKED", 'The current governance policy blocked this operation.', True, "inspect_active_governance_evidence", "feedback"),
    ErrorDefinition("GITGO-E3209", "RING_0_REQUIRED", 'The tool requires authority outside this worker process.', False, "use_existing_parent_capability_path", "admission"),
    ErrorDefinition("GITGO-E3311", "COMPOSITE_CONTINUATION_STALE", 'The retained composite continuation no longer matches the current tool contract.', True, "inspect_committed_leaves_before_replanning", "custom_tools"),
    ErrorDefinition("GITGO-E5101", "PROVIDER_NO_PROGRESS", 'The provider produced no public progress within the bounded recovery allowance.', False, "inspect_provider_recovery_and_preserve_effects", "provider"),
    ErrorDefinition("GITGO-E5102", "PROVIDER_TOOL_CALL_TRUNCATION_STORM", 'Repeated tool-call truncation exhausted the bounded recovery allowance.', False, "inspect_provider_and_revise_output_budget", "provider"),
    ErrorDefinition("GITGO-E5103", "LLM_STREAM_FAILED", 'The provider stream failed after runtime recovery attempts.', True, "inspect_provider_transport_and_effect_state", "provider"),
    ErrorDefinition("GITGO-E5104", "STREAM_FALLBACK_FAILED", 'The configured provider fallback could not recover this turn.', False, "inspect_provider_or_continue_from_safe_checkpoint", "provider"),
    ErrorDefinition("GITGO-E5105", "INVALID_TOOL_ARGUMENT_JSON", 'The provider emitted an incomplete or invalid tool-argument object.', True, "resubmit_one_complete_schema_valid_call", "provider"),
    ErrorDefinition("GITGO-E5106", "PROVIDER_RESPONSE_INCOMPLETE", 'The provider stopped before completing this response.', True, "follow_bounded_provider_continuation", "provider"),
)


_DEFINITIONS += (
    ErrorDefinition("GITGO-E3510", "FILE_NOT_FOUND", "The requested file does not exist. Inspect the current directory or correct the path; do not infer that the whole capability is unavailable.", True, "discover_current_path_then_read", "file_access"),
    ErrorDefinition("GITGO-E3511", "IS_DIRECTORY", "The read target is a directory, not a file. Use file discovery for that scope.", True, "select_a_file_or_list_directory", "file_access"),
    ErrorDefinition("GITGO-E3512", "BINARY_FILE", "This target is binary and cannot be interpreted by the plain text reader.", True, "use_a_supported_format_reader", "file_access"),
    ErrorDefinition("GITGO-E3513", "READ_ERROR", "The selected file could not be read; inspect the concrete diagnostic before changing path or access strategy.", True, "inspect_read_diagnostic", "file_access"),
    ErrorDefinition("GITGO-E3514", "FILE_MUTATION_BUSY", "Another cooperating writer holds the candidate mutation lock; this candidate did not start committing.", True, "inspect_writer_then_refresh_target_version", "file_mutation"),
    ErrorDefinition("GITGO-E3515", "FILE_MUTATION_FAILED", "A file commit or its observation failed. Inspect actual per-file effects and any retained recovery reference before replanning.", False, "inspect_file_effects_before_replanning", "file_mutation"),
    ErrorDefinition("GITGO-E3516", "FILE_CHANGED_AFTER_COMMIT", "The observed target or retained deletion differs from the inspected candidate; effects are uncertain.", False, "inspect_committed_and_recovery_versions", "file_mutation"),
    ErrorDefinition("GITGO-E3517", "PATCH_UNSUPPORTED", "The text candidate interface cannot represent this patch or it exceeds its declared bounds. No formal file was changed.", True, "choose_a_supported_candidate_representation", "file_mutation"),
    ErrorDefinition("GITGO-E3518", "PATCH_TIMEOUT", "Private patch projection exceeded its allowance before any formal commit.", True, "inspect_and_bound_patch_projection", "file_mutation"),
    ErrorDefinition("GITGO-E3519", "FILE_SERVICE_REQUIRED", "Native file mutation handlers do not own formal commit authority. Use the registered trusted file-service tool.", True, "route_through_registered_file_service", "file_mutation"),
    ErrorDefinition("GITGO-E3520", "UNSUPPORTED_ENCODING", "The text file service supports UTF-8 only; no file was changed. Select an explicit supported representation rather than silently transcoding.", True, "select_supported_text_representation", "file_mutation"),
    ErrorDefinition("GITGO-E3521", "FILE_CANDIDATE_STALE", "The original frozen candidate is missing, changed or belongs to another binding. No formal commit was attempted.", True, "inspect_target_before_intentional_regeneration", "file_mutation"),
    ErrorDefinition("GITGO-E3522", "PROMOTION_CANDIDATE_UNAVAILABLE", "The formal Git delta could not be frozen within the supported object and size limits. No formal effect started.", True, "inspect_candidate_representation_and_limits", "file_mutation"),
    ErrorDefinition("GITGO-E3523", "PROMOTION_EFFECT_AMBIGUOUS", "Promotion application or its observation failed. Retain the existing prepared record and inspect per-file effects before reconciliation.", False, "inspect_prepared_promotion_before_reconciliation", "file_mutation"),
    ErrorDefinition("GITGO-E3524", "PROMOTION_METADATA_PENDING", "The formal effect is verified but its promotion acknowledgement failed. Preserve outputs and reconcile the exact prepared request.", False, "reconcile_exact_prepared_promotion", "file_mutation"),
    ErrorDefinition("GITGO-E3525", "WORKSPACE_CANDIDATE_UNAVAILABLE", "A bounded private execution view or complete output candidate could not be obtained. No formal output was published.", True, "inspect_execution_view_limits", "file_mutation"),
    ErrorDefinition("GITGO-E3526", "PROTECTED_PATH", "Business file tools cannot alter Git or Gitgo-owned metadata. Use the relevant project/state operation; do not retry through another file or command tool.", False, "use_registered_metadata_operation", "file_mutation"),
    ErrorDefinition("GITGO-E3810", "TOOL_TIMEOUT", "The tool exceeded its execution allowance. Effects may be uncertain; inspect current state before any replay.", False, "inspect_effects_before_replanning", "feedback"),
    ErrorDefinition("GITGO-E3811", "TOOL_CRASH", "The tool did not return a valid result. Inspect the concrete failure and retained effect evidence.", False, "inspect_tool_failure_and_effects", "feedback"),
    ErrorDefinition("GITGO-E5302", "TOOL_CANCELLED", "Tool execution was cancelled. Preserve effects and follow the actual user/runtime continuation state.", False, "preserve_effects_and_observe_cancellation", "feedback"),
)


@dataclass(frozen=True)
class NoticeDefinition:
    catalog_id: str
    name: str
    procedure: str


_NOTICE_DEFINITIONS = (
    NoticeDefinition("GITGO-W7420", "MEMORY_RETENTION_PENDING", "storage"),
    NoticeDefinition("GITGO-W7421", "MEMORY_RESTORE_RETIRE_PENDING", "file_mutation"),
    NoticeDefinition("GITGO-W7422", "MEMORY_SNAPSHOT_LIST_UNAVAILABLE", "storage"),
    NoticeDefinition("GITGO-W7410", "GOVERNANCE_AUDIT_PENDING", "storage"),
    NoticeDefinition("GITGO-W3521", "DOCUMENT_CANDIDATE_RETIRE_FAILED", "file_mutation"),
    NoticeDefinition("GITGO-W3522", "WORKSPACE_CANDIDATE_RETIRE_FAILED", "file_mutation"),
    NoticeDefinition("GITGO-W7602", "WORKBENCH_MATERIAL_AUDIT_INCOMPLETE", "workbench"),
    NoticeDefinition("GITGO-W7601", "WORKBENCH_SOURCE_RECHECK", "workbench"),
    NoticeDefinition("GITGO-W6301", "LESSON_RECALL_UNAVAILABLE", "lesson"),
    NoticeDefinition("GITGO-W6302", "SOURCE_EXCERPTS_UNAVAILABLE", "lesson"),
    NoticeDefinition("GITGO-W6201", "WORKFLOW_RECOVERY_REQUIRED", "workflow"),
    NoticeDefinition("GITGO-W6202", "WORKFLOW_EVIDENCE_STALE", "workflow"),
    NoticeDefinition("GITGO-W6203", "INTERACTION_STATE_CONFLICT", "alignment"),
    NoticeDefinition("GITGO-W6204", "WORKFLOW_PREREQUISITE_BLOCKED", "workflow"),
)
NOTICE_CATALOG = {definition.name: definition for definition in _NOTICE_DEFINITIONS}


ERROR_CATALOG: dict[str, ErrorDefinition] = {
    definition.name: definition for definition in _DEFINITIONS
}


def error_payload(
    name: str,
    *,
    message: str = "",
    details: dict[str, Any] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
    state_changed: bool = False,
    occurrence_id: str = "",
) -> dict[str, Any]:
    """Build one model-readable and machine-stable error payload.

    ``error`` intentionally remains the symbolic name because ToolPipeline and
    existing callers already treat that field as the business error code.
    """
    definition = ERROR_CATALOG.get(name)
    if definition is None:
        raise KeyError(f"Unknown Gitgo error definition: {name}")
    info = {
        "catalog_id": definition.catalog_id,
        "name": definition.name,
        "occurrence_id": occurrence_id or f"err_{uuid.uuid4().hex}",
        "message": message or definition.default_message,
        "retryable": definition.retryable,
        "state_changed": bool(state_changed),
        "recovery_class": definition.recovery_class,
        "details": dict(details or {}),
        "next_actions": list(next_actions or []),
        "guidance_ref": guidance_reference(definition.catalog_id),
    }
    return {
        "error": definition.name,
        "message": info["message"],
        "detail": info["message"],
        "error_info": info,
    }


def catalog_entry(name: str) -> dict[str, Any]:
    definition = ERROR_CATALOG.get(name) or next((d for d in _DEFINITIONS if d.catalog_id == name), None)
    if definition is None:
        raise KeyError(name)
    return {
        "catalog_id": definition.catalog_id,
        "name": definition.name,
        "procedure": definition.procedure,
        "guidance_ref": guidance_reference(definition.catalog_id),
        "default_message": definition.default_message,
        "retryable": definition.retryable,
        "recovery_class": definition.recovery_class,
    }


def guidance_reference(identifier: str) -> dict[str, Any]:
    """One read-only API contract; a reference never initiates recovery."""
    return {"tool": "decision_evidence", "arguments": {"focus": "guidance", "identifier": identifier}}


def diagnostic_entry(identifier: str) -> dict[str, Any] | None:
    """Resolve only registered symbols/IDs. No interpretation of error prose."""
    definition = ERROR_CATALOG.get(identifier) or next((d for d in _DEFINITIONS if d.catalog_id == identifier), None)
    if definition is not None:
        return {"kind": "error", **catalog_entry(definition.name)}
    notice = NOTICE_CATALOG.get(identifier) or next((d for d in _NOTICE_DEFINITIONS if d.catalog_id == identifier), None)
    if notice is not None:
        return {"kind": "warning", "catalog_id": notice.catalog_id, "name": notice.name,
                "procedure": notice.procedure, "guidance_ref": guidance_reference(notice.catalog_id)}
    return None


def notice_guidance(code: str) -> dict[str, Any]:
    entry = diagnostic_entry(code)
    if entry is None or entry["kind"] != "warning":
        return {}
    return {key: entry[key] for key in ("catalog_id", "guidance_ref")}
