"""Bounded information discovery over the already authorized live surface.

Groups are presentation metadata, not task classifiers or capability profiles.
Unknown/dynamic tools remain discoverable; no search result can issue a scope.
"""
import json
import re

from .provider_protocol import deterministic_hash
from .tool_context import contract_overview, tool_definition, tool_detail_definition
from backend.core.errors import error_payload

INITIAL_CONTRACT_CHARS = 8192
INITIAL_CATALOG_CHARS = 10240
# Keep the compact interaction boundary available before large file contracts
# exhaust the existing catalog budget. This is interface priority, not a
# classifier deciding whether a user's request needs a question.
ESSENTIAL = ('capability_status', 'request_user_decision', 'complete_task', 'read_file', 'list_files', 'search_text',
             'run_test', 'declare_task_contract', 'report_progress')
GROUPS = {
    'files': ('Inspect and change workspace files and documents.',
              'read_file list_files search_text document_open document_create edit_file write_file delete_file apply_patch'),
    'web': ('Search and read external web evidence.', 'web_search web_fetch'),
    'context': ('Retrieve project context, artifacts, dependency and lesson evidence.',
                'scan status recall_grep recall_semantic recall_rag assemble_context return_context context_open context_search dependency_query artifact_read tool_result_open decompose_task contract_detect_drift contract_get_impact contract_get_changed_symbols lesson_search lesson_list lesson_checker privacy_scan code_dossier decision_evidence'),
    'task': ('Inspect task state, declare scope and maintain or author lifecycle mechanisms.',
             'task_mechanism harness_mechanism declare_task_contract engineering_workflow acknowledge_governance_signal'),
    'interaction': ('Report progress and obtain actual user choices or prescribed authority changes.',
                    'report_progress request_user_decision request_permission configure_capability capability_status request_self_execute'),
    'agents': ('Delegate, communicate, review and complete coordinated work.',
              'delegate_task delegate_task_dag delegate_task_bundle prepare_task_bundle list_agents wait_agents send_feedback cancel_agent request_review review_child_outcome read_child_artifact list_coordination_events resolve_coordination_event promote_agent_changes complete_supervision publish_interface_update escalate_to_supervisor complete_task complete_review'),
    'execution': ('Run processes, tests or workbench operations through existing admission.',
                  'exec_command shell_script run_test workbench dependency_feedback rebuild_dependency_graph'),
    'authoring': ('Define or author callable tools.', 'define_tool author_tool'),
    'memory': ('Maintain recorded lessons and memory snapshots.',
               'formalize lesson_discard lesson_verify lesson_harvest lesson_promote memory_snapshot memory_restore'),
    'publishing': ('Inspect and operate existing publication and collaboration workflows.', ''),
    'other': ('Other registered tools, including dynamically installed tools.', ''),
}
_MEMBERS = {name: group for group, (_, names) in GROUPS.items() for name in names.split()}


def group_for(name):
    # These are registered namespace/name identities, never user-text routing.
    return _MEMBERS.get(name, 'publishing' if name.startswith('publish_') else 'other')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def group_catalog(live):
    counts = {}
    for name in live:
        group = group_for(name)
        counts[group] = counts.get(group, 0) + 1
    return {group: {'description': GROUPS[group][0], 'count': counts[group]}
            for group in GROUPS if group in counts}


def project_catalog(live):
    """No discovery entry point: retain the complete legacy overview catalog."""
    if 'capability_status' not in live:
        return {'runtime.tool_contracts': {name: contract_overview(value) for name, value in live.items()}}
    essential = {}
    groups = group_catalog(live)
    for name in ESSENTIAL:
        if name not in live:
            continue
        # Discovery must itself be usable without another discovery round.
        value = live[name] if name == 'capability_status' else contract_overview(live[name])
        candidate = {**essential, name: value}
        if (len(encode(candidate)) <= INITIAL_CONTRACT_CHARS and len(encode({
                'runtime.tool_contracts': candidate, 'runtime.tool_groups': groups})) <= INITIAL_CATALOG_CHARS):
            essential = candidate
    if 'capability_status' not in essential:
        # A customized oversized discovery contract must not make the surface
        # undiscoverable. This explicit compatibility path keeps all overviews.
        return {'runtime.tool_contracts': {name: contract_overview(value) for name, value in live.items()}}
    return {'runtime.tool_contracts': essential, 'runtime.tool_groups': groups}


def discover(process, active, args):
    """Read current definitions only; callers obtain active via tool_surface."""
    operation = args.get('operation', 'status')
    definitions = {name: tool_definition(tool) for name, tool in active.items()}
    scope = {'process_id': process.process_id, 'task_id': process.active_task_id or process.process_id,
             'session_id': process.session.session_id}
    common = {'kind': 'tool_discovery', **scope, 'authority_granted': False,
              'groups': group_catalog(definitions)}
    if operation == 'describe':
        name = args.get('name', '')
        if name not in definitions:
            return {**common, **error_payload('TOOL_NOT_FOUND', details={'name': name}), 'name': name,
                    'instruction': 'Inspect catalog/status for the current scope; discovery cannot activate tools.'}
        definition = tool_detail_definition(active[name])
        return {**common, 'definition': definition, 'contract_digest': deterministic_hash(definition)}
    group = args.get('group', '')
    if group and group not in common['groups']:
        return {**common, **error_payload('TOOL_GROUP_NOT_ACTIVE', details={'group': group}), 'group': group}
    query = str(args.get('query') or '').casefold()
    terms = re.findall(r'\w+', query)[:32] if operation == 'search' else []
    rows = []
    for name, definition in definitions.items():
        if group and group_for(name) != group:
            continue
        description = str(definition.get('description') or '')
        haystack = (name + ' ' + group_for(name) + ' ' + description).casefold()
        score = sum(4 * (term in name.casefold()) + (term in haystack) for term in terms)
        if terms and not score:
            continue
        rows.append((score, name, {'name': name, 'group': group_for(name),
                                  'description': description.split('\n', 1)[0][:160]}))
    rows.sort(key=lambda row: (-row[0], row[1]))
    identity = deterministic_hash({'scope': scope, 'contracts': definitions,
                                   'operation': operation, 'query': query, 'group': group})
    offset = 0
    cursor = args.get('cursor') or ''
    if cursor:
        try:
            anchor, raw = cursor.split(':', 1)
            offset = int(raw)
            if anchor != identity or offset < 0 or offset > len(rows):
                raise ValueError('stale cursor')
        except (ValueError, AttributeError):
            return {**common, **error_payload('TOOL_CATALOG_CURSOR_STALE'),
                    'instruction': 'Restart this read without cursor; the active definitions or query changed.'}
    limit = max(1, min(20, int(args.get('limit', 12))))
    end = min(len(rows), offset + limit)
    return {**common, 'items': [row[2] for row in rows[offset:end]], 'total': len(rows),
            'next_cursor': f'{identity}:{end}' if end < len(rows) else '',
            'instruction': 'Use describe with one exact name for its complete contract. Search is lexical and can miss matches; catalog without query lists every active tool, with pagination.'}


def mark_discovery_result(process, message, active):
    """Authenticate the exact view produced by the registered discovery tool.

    This metadata is informational. Execution revalidates the live tool again.
    """
    data = message.get('data') or {}
    definition = data.get('definition') if isinstance(data, dict) else None
    if (message.get('_tool_name') != 'capability_status' or message.get('is_error')
            or not definition or data.get('kind') != 'tool_discovery'
            or data.get('session_id') != process.session.session_id
            or data.get('task_id') != (process.active_task_id or process.process_id)):
        return
    tool = active.get(definition.get('name'))
    if tool is None or definition != tool_detail_definition(tool) or data.get('contract_digest') != deterministic_hash(definition):
        return
    message.update(host_authority=True, context_purpose='tool_contract',
                   prompt_sections=[{'name': 'Tool details: ' + tool.name,
                                     'provenance': 'runtime:registered-tool-discovery',
                                     'version': data['contract_digest']}])
