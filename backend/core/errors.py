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


_DEFINITIONS = (
    ErrorDefinition("GITGO-E3701", "BASH_IDENTITY_UNVERIFIED", "Bash identity could not be verified; the script was not executed.", True, "repair_bash_installation_then_retry"),
    ErrorDefinition("GITGO-E3601", "SEARCH_ENGINE_REQUIRED", "This query requires ripgrep; repair the engine or explicitly narrow the query.", True, "repair_search_engine_or_narrow_query"),
    ErrorDefinition("GITGO-E3602", "SEARCH_INCOMPLETE", "Search did not cover its declared scope; absence cannot be inferred from partial results.", True, "narrow_search_scope"),
    ErrorDefinition("GITGO-E3603", "INVALID_SEARCH_ARGUMENTS", "Search arguments are invalid or exceed their bounded limits.", True, "correct_search_arguments"),
    ErrorDefinition("GITGO-E3604", "INVALID_REGEX", "The search regular expression is invalid for ripgrep.", True, "correct_pattern_or_use_literal"),
    ErrorDefinition("GITGO-E3605", "SEARCH_ERROR", "The search engine failed; inspect the preserved diagnostic.", True, "inspect_search_diagnostic"),
    ErrorDefinition("GITGO-E6201", "ENGINEERING_WORKFLOW_RECOVERY_REQUIRED",
                    "Engineering evidence could not be recorded; existing requirements remain active.",
                    True, "inspect_workflow_frontier_or_amend_scope"),
    ErrorDefinition("GITGO-E6202", "ENGINEERING_PREREQUISITE_REQUIRED",
                    "The operation requires current engineering prerequisite evidence.",
                    True, "satisfy_ready_workflow_nodes"),
    ErrorDefinition("GITGO-E3106", "PROCESS_PRESENTATION_INVALID",
                    "Select an existing B process and provide a valid display name or archive action.",
                    True, "correct_process_selection_or_name"),
    ErrorDefinition("GITGO-E7401", "STORAGE_UNSAFE_SQLITE_VERSION",
                    "The loaded SQLite engine has a known WAL corruption vulnerability.",
                    False, "select_patched_runtime"),
    ErrorDefinition("GITGO-E7402", "STORAGE_CORRUPTION",
                    "Persistent state is damaged; an explicit verified recovery is required.",
                    False, "backup_then_recover"),
    ErrorDefinition("GITGO-E7403", "STORAGE_LOCATION_CONFLICT",
                    "The database family is redirected, split or has ambiguous storage locations.",
                    False, "inspect_and_relocate_storage"),
    ErrorDefinition("GITGO-E7404", "STORAGE_MAINTENANCE_BUSY",
                    "Storage maintenance requires all project runtimes to release their leases.",
                    True, "close_runtimes_then_retry"),
    ErrorDefinition("GITGO-E7405", "STORAGE_BLOCKED",
                    "Storage cannot safely accept the operation.",
                    False, "inspect_storage_health"),
    ErrorDefinition("GITGO-E7406", "STORAGE_CAS_REFERENCE_MISSING",
                    "An authoritative content-addressed object is missing.",
                    False, "restore_verified_object_or_continue_from_safe_checkpoint"),
    ErrorDefinition("GITGO-E7201", "DAEMON_COMMAND_FAILED",
                    "The daemon could not complete this command, but remains available.",
                    True, "inspect_diagnostic_then_retry_or_recover"),
    ErrorDefinition("GITGO-E3105", "CONTINUATION_STATE_UNAVAILABLE",
                    "The previous agent ownership or session checkpoint cannot be restored safely.",
                    False, "inspect_storage_or_user_decision"),
    ErrorDefinition("GITGO-E5301", "TASK_TREE_DEADLINE_EXCEEDED",
                    "The admitted task-tree time allowance is exhausted; completed writes are preserved, not certified as task completion.",
                    False, "inspect_evidence_then_continue_session"),
    ErrorDefinition(
        "GITGO-E3101", "CAPABILITY_PROFILE_UNKNOWN",
        "The requested capability profile does not exist.", True,
        "automatic_or_llm_retry",
    ),
    ErrorDefinition(
        "GITGO-E3102", "CAPABILITY_PROFILE_NOT_WORKER",
        "The requested capability profile cannot be used for a B Agent.", True,
        "automatic_or_llm_retry",
    ),
    ErrorDefinition(
        "GITGO-E3103", "DELEGATION_ADMISSION_FAILED",
        "The B Agent could not be admitted.", True, "llm_retry_or_user_decision",
    ),
    ErrorDefinition(
        "GITGO-E3104", "DELEGATION_EMPTY_RESULT",
        "Delegation produced no child Agent.", True, "switch_delegation_strategy",
    ),
    ErrorDefinition(
        "GITGO-E3108", "TOOL_NOT_FOUND",
        "The requested tool is not available in the current task capability set.",
        True, "refresh_capabilities_or_choose_available_tool",
    ),
    ErrorDefinition(
        "GITGO-E3109", "TOOL_CALL_STORM_BLOCKED",
        "The Host stopped a repeated failing tool call before execution.",
        True, "inspect_error_then_change_arguments_or_strategy",
    ),
    ErrorDefinition(
        "GITGO-E3115", "PROVIDER_CAPABILITY_UNAVAILABLE",
        "The active provider or API plan does not expose the requested capability.",
        False, "choose_provider_fallback_or_continue_without_capability",
    ),
    ErrorDefinition(
        "GITGO-E3110", "TOOL_NOT_STARTED",
        "The tool call was not started because its provider turn was rolled back.",
        True, "inspect_current_state_then_retry_if_safe",
    ),
    ErrorDefinition(
        "GITGO-E3111", "COORDINATION_EVENT_NOT_FOUND",
        "The requested coordination event is not owned by this supervisor.",
        True, "list_coordination_events_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E3112", "COORDINATION_RESOLUTION_INVALID",
        "The requested coordination resolution is not valid for this event.",
        True, "inspect_event_then_choose_valid_resolution",
    ),
    ErrorDefinition(
        "GITGO-E3113", "INTERFACE_REVISION_PENDING",
        "A downstream Agent is waiting for its supervisor to resolve an interface revision.",
        True, "supervisor_resolve_or_ask_user",
    ),
    ErrorDefinition(
        "GITGO-E3114", "SELF_EXECUTION_LEASE_REQUIRED",
        "The main process selected self execution but has not yet requested its task-scoped execution lease.",
        True, "request_self_execute_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E6101", "REQUIRED_DELEGATION_UNSATISFIED",
        "The task contract requires a delegated B outcome.", False,
        "continue_or_fail_truthfully",
    ),
    ErrorDefinition(
        "GITGO-E6102", "REQUIRED_ARTIFACT_MISSING",
        "A required workspace artifact is missing.", True,
        "llm_retry_or_user_decision",
    ),
    ErrorDefinition(
        "GITGO-E6103", "COMPLETION_EVIDENCE_INSUFFICIENT",
        "The Host does not have enough evidence to accept completion.", True,
        "continue_work",
    ),
    ErrorDefinition(
        "GITGO-E6104", "FAILURE_EVIDENCE_INSUFFICIENT",
        "The Host does not have enough evidence to accept terminal failure.", True,
        "continue_or_request_user",
    ),
    ErrorDefinition(
        "GITGO-E6105", "COMPLETION_EXCEPTION_DECISION_REQUIRED",
        "The same completion gates remain unresolved after a bounded recovery attempt.",
        True, "continue_accept_partial_or_stop",
    ),
    ErrorDefinition(
        "GITGO-E5201", "CONTEXT_WINDOW_EXCEEDED",
        "The provider rejected the request because its context limit was exceeded.",
        True, "compact_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E5202", "CONTEXT_COMPACTION_FAILED",
        "Ordinary context compaction did not produce a smaller valid epoch.",
        True, "retry_then_user_decision",
    ),
    ErrorDefinition(
        "GITGO-E5203", "CONTEXT_FORCE_COMPACTION_REQUIRED",
        "Three ordinary compaction attempts failed; destructive compaction requires user approval.",
        False, "user_decision",
    ),
    ErrorDefinition(
        "GITGO-E5204", "CONTEXT_LIMIT_CONFIGURATION_INVALID",
        "The configured context limit is invalid or exceeds the provider's observed limit.",
        True, "correct_configuration_or_compact",
    ),
    ErrorDefinition(
        "GITGO-E3201", "RESOURCE_SCOPE_APPROVAL_REQUIRED",
        "The requested resource is outside the Agent's current task scope.",
        True, "request_user_permission",
    ),
    ErrorDefinition(
        "GITGO-E3202", "RESOURCE_SCOPE_APPROVAL_DENIED",
        "The user did not grant the requested resource scope.",
        False, "adjust_plan_or_stop_truthfully",
    ),
    ErrorDefinition(
        "GITGO-E3203", "APPROVAL_GRANT_INVALID",
        "The approval grant does not match this tool invocation.",
        True, "request_precise_user_permission",
    ),
    ErrorDefinition(
        "GITGO-E3204", "SENSITIVE_TOOL_APPROVAL_REQUIRED",
        "This sensitive tool requires approval for the exact invocation.",
        True, "request_exact_user_permission",
    ),
    ErrorDefinition(
        "GITGO-E3205", "TOOL_PERMISSION_DENIED",
        "The user denied this exact tool invocation.",
        False, "change_strategy_or_stop_truthfully",
    ),
    ErrorDefinition(
        "GITGO-E3301", "CUSTOM_TOOL_INVALID",
        "The custom tool definition is invalid or failed its registration tests.",
        True, "inspect_validation_details_then_revise",
    ),
    ErrorDefinition(
        "GITGO-E3302", "CUSTOM_TOOL_EXISTS",
        "A saved custom tool already uses this name.",
        True, "replace_existing_version_or_choose_name",
    ),
    ErrorDefinition(
        "GITGO-E3303", "CUSTOM_TOOL_NOT_FOUND",
        "The requested custom tool or version is not available.",
        True, "list_saved_tools_then_correct_selection",
    ),
    ErrorDefinition(
        "GITGO-E3304", "CUSTOM_TOOL_ARCHIVED",
        "The requested custom tool is archived and cannot be mounted.",
        True, "restore_then_mount",
    ),
    ErrorDefinition(
        "GITGO-E3305", "CUSTOM_TOOL_PRIVACY_BLOCKED",
        "The custom tool source failed the content-level privacy boundary.",
        False, "remove_sensitive_content_then_register",
    ),
    ErrorDefinition(
        "GITGO-E3306", "CUSTOM_TOOL_SOURCE_INTEGRITY_FAILED",
        "The saved custom tool source does not match its immutable digest.",
        False, "stop_using_version_and_inspect_storage",
    ),
    ErrorDefinition(
        "GITGO-E3307", "CUSTOM_TOOL_CATALOG_FAILED",
        "The project custom-tool catalog could not be read.",
        True, "inspect_storage_health_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E3308", "CUSTOM_TOOL_STATE_CHANGE_FAILED",
        "The custom tool state could not be changed safely.",
        True, "refresh_catalog_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E3309", "CUSTOM_TOOL_PRIVILEGED_APPROVAL_REQUIRED",
        "A privileged custom-tool version requires approval bound to its exact source and registration contract.",
        True, "request_exact_user_permission",
    ),
    ErrorDefinition(
        "GITGO-E3401", "DOCUMENT_FORMAT_UNSUPPORTED",
        "The document format is unsupported or its adapter is unavailable.",
        True, "convert_document_or_install_adapter",
    ),
    ErrorDefinition(
        "GITGO-E3402", "DOCUMENT_READ_FAILED",
        "The document could not be read safely.",
        True, "inspect_document_then_retry_or_convert",
    ),
    ErrorDefinition(
        "GITGO-E3405", "DOCUMENT_WRITE_FAILED",
        "The document could not be created or replaced safely.",
        True, "correct_output_specification_then_retry",
    ),
    ErrorDefinition(
        "GITGO-E3403", "WEB_SEARCH_PROVIDER_NOT_CONFIGURED",
        "No public web search provider is configured for this Host.",
        False, "configure_search_provider_or_continue_offline",
    ),
    ErrorDefinition(
        "GITGO-E3404", "WEB_SEARCH_QUERY_REQUIRED",
        "A non-empty public web search query is required.",
        True, "provide_query",
    ),
    ErrorDefinition(
        "GITGO-E3406", "WEB_SEARCH_FAILED",
        "The configured public web search provider could not return a valid response.",
        True, "inspect_provider_then_retry_or_continue_offline",
    ),
    ErrorDefinition(
        "GITGO-E3407", "WEB_FETCH_BLOCKED_URL",
        "The requested page is outside the anonymous public-fetch safety boundary.",
        False, "choose_public_http_url",
    ),
    ErrorDefinition(
        "GITGO-E3408", "WEB_FETCH_FAILED",
        "The selected public page could not be retrieved within the fetch contract.",
        True, "retry_or_choose_another_source",
    ),
    ErrorDefinition(
        "GITGO-E3501", "COMMAND_EXIT_NONZERO",
        "The command ran and returned a non-zero exit code. Inspect its captured stdout and stderr before changing the command, the implementation, or the test.",
        True, "inspect_command_output_then_fix_or_change_strategy",
    ),
    ErrorDefinition(
        "GITGO-E3502", "FILE_EXISTS",
        "The target already exists. Replace it only with an explicit compare-and-swap write using its current SHA-256.",
        True, "reread_then_replace_with_expected_hash",
    ),
    ErrorDefinition(
        "GITGO-E3503", "STRING_NOT_FOUND",
        "The exact edit anchor is absent from the current file version.",
        True, "reread_then_use_a_small_current_anchor",
    ),
    ErrorDefinition(
        "GITGO-E3504", "PATCH_CHECK_FAILED",
        "The unified diff failed preflight validation; no file was changed.",
        True, "repair_headers_and_hunks_or_use_bounded_edits",
    ),
    ErrorDefinition(
        "GITGO-E3505", "FILE_CHANGED",
        "The file changed after it was read, so the compare-and-swap write was rejected.",
        True, "reread_then_recompute_the_edit",
    ),
    ErrorDefinition(
        "GITGO-E3506", "EXPECTED_HASH_REQUIRED",
        "Replacing or deleting an existing file requires its current SHA-256 from read_file.",
        True, "read_file_then_retry_with_expected_hash",
    ),
    ErrorDefinition(
        "GITGO-E5205", "TOOL_RESULT_LOCATOR_INVALID",
        "The oversized tool-result locator is invalid, unavailable or outside this task lineage.",
        True, "reuse_current_locator_or_rerun_source_tool",
    ),
)

_DEFINITIONS += (
    ErrorDefinition("GITGO-E3107", "SUPERVISOR_BUSY",
                    "A already has an active turn. Wait or explicitly interrupt it before creating B.",
                    True, "wait_or_user_decision"),
    ErrorDefinition("GITGO-E7501", "DELETION_BLOCKED",
                    "Deletion cannot safely proceed; inspect its plan before retrying.",
                    True, "inspect_then_retry"),
    ErrorDefinition("GITGO-E7502", "DELETION_CONFIRMATION_REQUIRED",
                    "Deletion requires an explicit, current target preview and confirmation.",
                    False, "preview_then_user_confirmation"),
)

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
    }
    return {
        "error": definition.name,
        "message": info["message"],
        "detail": info["message"],
        "error_info": info,
    }


def catalog_entry(name: str) -> dict[str, Any]:
    definition = ERROR_CATALOG[name]
    return {
        "catalog_id": definition.catalog_id,
        "name": definition.name,
        "default_message": definition.default_message,
        "retryable": definition.retryable,
        "recovery_class": definition.recovery_class,
    }
