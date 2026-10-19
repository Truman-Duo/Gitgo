"""Immutable read execution descriptions; serialization grants no authority.

This module performs no file access, resource allocation or process launch.
Runtime references still require trusted resolution and durable admission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from backend.core.sandbox_read import ObjectIdentity
from backend.core.sandbox_read_plan import ReadPlan
from backend.core.storage.redaction import redact_for_persistence
from .execution_contract import ExecutionContract, NATIVE_FILE_READ

MAX_PLAN_BYTES = 256 * 1024
MAX_PLAN_DEPTH = 16
MAX_SAFE_INTEGER = 2**53 - 1


class ReadExecutionPlanError(ValueError):
    """A description cannot be represented without changing its meaning."""


class ReadExecutionPlanBlocked(RuntimeError):
    """A serializable description is not a durable launch admission."""
    code = 'READ_EXECUTION_PLAN_NOT_ADMITTED'


def _require(condition, code):
    if not condition:
        # Never include caller values or paths in a validation error.
        raise ReadExecutionPlanError(code)


def _object(**fields):
    return ('object', tuple(fields.items()))


def _integer(low=0, high=MAX_SAFE_INTEGER):
    return ('integer', low, high)


def _string(limit=128, pattern=None):
    return ('string', limit, pattern)


def _enum(*values):
    return ('enum', values)


def _array(item, low, high):
    return ('array', item, low, high)


_DIGEST = _string(64, r'[0-9a-f]{64}')
_ID = _string(128, r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_DECIMAL = _string(39, r'(?:0|[1-9][0-9]{0,38})')
_LEASE = _object(id=_ID, generation=_integer(1))
_FILE = _object(platform=_enum('win32', 'linux'), volume=_DECIMAL,
                file_id=_DECIMAL, locator=_string(32768))
_PIPE = _object(role=_enum('request', 'result', 'diagnostic'),
    direction=_enum('host_to_helper', 'helper_to_host'),
    access=_enum('read_data_sync', 'write_data_sync'),
    object_policy_ref=_DIGEST, kernel_buffer_bytes=_integer(1, 2**20))
_LEASE_ROLES = ('object_user', 'runtime', 'native_scope', 'transport', 'scratch',
                'execution_budget', 'host_share')
_INTENT = _object(
    versions=_object(compiler=_DIGEST, policy=_DIGEST, registry=_DIGEST),
    kind=_enum('native_process'), action=_enum('read_existing_file'),
    context=_object(project_id=_ID, session_id=_ID, task_id=_ID, process_id=_ID),
    tool=_object(name=_enum('read_file', 'file_read'), registration_ref=_DIGEST, arguments_ref=_DIGEST),
    read=_object(workspace=_FILE, root=_FILE, file=_FILE,
        relative_parts=_array(_string(255), 1, 256), protection=_LEASE,
        content_semantics=_enum('same_object_mutable_content'), offset=_DECIMAL,
        length=_integer(1, 16 * 1024 * 1024)),
    runtime=_object(manifest_ref=_DIGEST, installation_ref=_DIGEST,
        backend_profile_ref=_DIGEST, launch_gate_profile_ref=_DIGEST,
        capability_report_ref=_DIGEST, descendants=_enum('trusted_manifest_only')),
    isolation=_object(platform=_enum('win32', 'linux'), native_rules_ref=_DIGEST,
        identity_rule=_enum('unique_per_execution'), network=_enum('deny'),
        filesystem_mutation=_enum('deny'), external_services=_enum('deny'),
        ipc_device_profile_ref=_DIGEST, scratch=_object(mode=_enum('private_bounded_arena'),
            data_bytes=_integer(1), object_slots=_integer(1, 256),
            host_storage_bytes=_integer(1), quota_profile_ref=_DIGEST)),
    limits=_object(wall_ms=_integer(1, 1800000), cpu_tree_ms=_integer(1, 1800000),
        memory_tree_bytes=_integer(1), process_tree_count=_integer(1, 256),
        host_buffer_bytes=_integer(1), host_io_workers=_integer(1, 64),
        host_storage_bytes=_integer(1), evidence_bytes=_integer(1), cleanup_ms=_integer(1, 60000)),
    transport=_object(profile_ref=_DIGEST, request_frame_bytes=_integer(1, 65536),
        response_frame_bytes=_integer(1, 65536), request_total_bytes=_integer(1),
        response_total_bytes=_integer(1), messages=_integer(1, 4096), pending=_integer(1, 64),
        read_per_message_bytes=_integer(1, 16 * 1024 * 1024),
        read_total_bytes=_integer(1, 16 * 1024 * 1024), stderr_bytes=_integer(1),
        parser_reserve_bytes=_integer(1), codec_memory_factor=_integer(1, 64),
        pipes=_array(_PIPE, 3, 3)),
    approval=_object(scope=_enum('once', 'exact_object_task'),
        valid_for_ms=_integer(1, 1800000), max_invocations=_integer(1, 1000000)))
_ALLOCATION = _object(execution_id=_ID, owner_id=_ID, owner_boot_id=_ID,
    owner_epoch=_integer(1), parent_execution_id=('nullable', _ID),
    native_identity_ref=_DIGEST, object_bindings_ref=_DIGEST, pipe_instances_ref=_DIGEST,
    scratch_instance_ref=_DIGEST, host_share_ref=_DIGEST,
    prepared_at_utc_ms=_integer(1), deadline_utc_ms=_integer(1),
    leases=_object(**{key: _LEASE for key in _LEASE_ROLES}))
_PLAN = _object(version=('integer', 1, 1), intent=_INTENT, allocation=_ALLOCATION)


def _check_tree(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    _require(depth <= MAX_PLAN_DEPTH and count[0] <= 4096, 'PLAN_STRUCTURE_LIMIT')
    if type(value) is dict:
        _require(len(value) <= 256, 'PLAN_STRUCTURE_LIMIT')
        for key, item in value.items():
            _require(type(key) is str and key.isascii() and len(key) <= 128, 'PLAN_KEY_INVALID')
            _check_tree(item, depth + 1, count)
    elif type(value) is list:
        _require(len(value) <= 256, 'PLAN_STRUCTURE_LIMIT')
        for item in value:
            _check_tree(item, depth + 1, count)
    elif type(value) is str:
        _require(len(value) <= 32768 and '\0' not in value, 'PLAN_STRING_INVALID')
        try:
            size = len(value.encode('utf8', errors='strict'))
        except UnicodeError:
            raise ReadExecutionPlanError('PLAN_UNICODE_INVALID') from None
        _require(size <= 32768, 'PLAN_STRING_LIMIT')
    elif type(value) is int:
        _require(0 <= value <= MAX_SAFE_INTEGER, 'PLAN_INTEGER_INVALID')
    else:
        _require(value is None, 'PLAN_TYPE_INVALID')


def _shape(value, shape):
    kind = shape[0]
    if kind == 'object':
        _require(type(value) is dict and set(value) == {k for k, _ in shape[1]}, 'PLAN_FIELDS_INVALID')
        for key, child in shape[1]:
            _shape(value[key], child)
    elif kind == 'array':
        _require(type(value) is list and shape[2] <= len(value) <= shape[3], 'PLAN_ARRAY_INVALID')
        for item in value:
            _shape(item, shape[1])
    elif kind == 'integer':
        _require(type(value) is int and shape[1] <= value <= shape[2], 'PLAN_INTEGER_INVALID')
    elif kind == 'string':
        _require(type(value) is str and 0 < len(value) <= shape[1], 'PLAN_STRING_INVALID')
        _require(shape[2] is None or re.fullmatch(shape[2], value) is not None, 'PLAN_STRING_INVALID')
    elif kind == 'enum':
        _require(type(value) is str and value in shape[1], 'PLAN_ENUM_INVALID')
    elif kind == 'nullable':
        if value is not None:
            _shape(value, shape[1])
    else:
        raise ReadExecutionPlanError('PLAN_SCHEMA_INVALID')


def _parse(raw):
    _require(type(raw) is bytes and len(raw) <= MAX_PLAN_BYTES, 'PLAN_BYTES_LIMIT')
    try:
        text = raw.decode('utf8', errors='strict')
        # Bound parser nesting before allocating nested JSON containers.
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                _require(depth <= MAX_PLAN_DEPTH, 'PLAN_STRUCTURE_LIMIT')
            elif char in ']}':
                depth -= 1
        def pairs(items):
            result = {}
            for key, item in items:
                _require(key not in result, 'PLAN_DUPLICATE_KEY')
                result[key] = item
            return result
        def invalid_number(_):
            raise ReadExecutionPlanError('PLAN_INTEGER_INVALID')
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_float=invalid_number, parse_constant=invalid_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        if isinstance(error, ReadExecutionPlanError):
            raise
        raise ReadExecutionPlanError('PLAN_JSON_INVALID') from None
    _check_tree(value)
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def _windows_part(part):
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
    reserved.update(prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³')
    return (not part.endswith((' ', '.')) and not any(c in part for c in ':*?"<>|')
            and not any(ord(c) < 32 for c in part)
            and part.split('.')[0].upper() not in reserved)


def _semantics(value):
    intent, allocation = value['intent'], value['allocation']
    read, transport, limits = intent['read'], intent['transport'], intent['limits']
    platform = intent['isolation']['platform']
    path_type = PureWindowsPath if platform == 'win32' else PurePosixPath
    for role in ('workspace', 'root', 'file'):
        entry = read[role]
        _require(entry['platform'] == platform, 'PLAN_PLATFORM_MISMATCH')
        _require(int(entry['volume']) < 2**64 and int(entry['file_id']) < 2**128, 'PLAN_IDENTITY_RANGE')
        path = path_type(entry['locator'])
        _require(path.is_absolute() and '..' not in path.parts, 'PLAN_LOCATOR_INVALID')
        if platform == 'win32':
            _require(len(path.drive) == 2 and path.drive[1] == ':', 'PLAN_LOCAL_DRIVE_REQUIRED')
            _require(all(_windows_part(p) for p in path.parts[1:]), 'PLAN_LOCATOR_INVALID')
        if role != 'file':
            _require(str(path) != path.anchor, 'PLAN_VOLUME_ROOT_DENIED')
    for part in read['relative_parts']:
        _require(part not in ('.', '..') and '/' not in part and '\\' not in part, 'PLAN_RELATIVE_INVALID')
        if platform == 'win32':
            _require(_windows_part(part), 'PLAN_RELATIVE_INVALID')
    expected = path_type(read['root']['locator']).joinpath(*read['relative_parts'])
    _require(str(expected) == read['file']['locator'], 'PLAN_FILE_LOCATOR_MISMATCH')
    _require(int(read['offset']) + read['length'] <= 2**64 - 1, 'PLAN_READ_RANGE')
    _require(transport['read_per_message_bytes'] <= transport['read_total_bytes']
             and read['length'] <= transport['read_total_bytes'], 'PLAN_READ_BUDGET')
    _require(transport['pending'] <= transport['messages'], 'PLAN_PENDING_BUDGET')
    _require(transport['request_frame_bytes'] <= transport['request_total_bytes']
             and transport['response_frame_bytes'] <= transport['response_total_bytes'], 'PLAN_FRAME_BUDGET')
    _require(transport['read_per_message_bytes'] <= transport['response_frame_bytes']
             and transport['read_total_bytes'] <= transport['response_total_bytes'], 'PLAN_RESPONSE_BUDGET')
    _require([p['role'] for p in transport['pipes']] == ['request', 'result', 'diagnostic'], 'PLAN_PIPE_ROLES')
    for pipe in transport['pipes']:
        request = pipe['role'] == 'request'
        _require(pipe['direction'] == ('host_to_helper' if request else 'helper_to_host')
                 and pipe['access'] == ('read_data_sync' if request else 'write_data_sync'), 'PLAN_PIPE_ACCESS')
    working = transport['pending'] * (transport['request_frame_bytes'] + transport['response_frame_bytes']) * transport['codec_memory_factor']
    minimum = transport['response_total_bytes'] + transport['stderr_bytes'] + transport['parser_reserve_bytes'] + working
    _require(limits['host_buffer_bytes'] >= minimum, 'PLAN_BUFFER_BUDGET')
    _require(limits['host_io_workers'] >= 3, 'PLAN_WORKER_BUDGET')
    scratch = intent['isolation']['scratch']
    _require(scratch['host_storage_bytes'] >= scratch['data_bytes'], 'PLAN_SCRATCH_BUDGET')
    _require(limits['host_storage_bytes'] >= scratch['host_storage_bytes'] + limits['evidence_bytes'], 'PLAN_STORAGE_BUDGET')
    lifetime = min(limits['wall_ms'], intent['approval']['valid_for_ms'])
    _require(allocation['prepared_at_utc_ms'] < allocation['deadline_utc_ms']
             <= allocation['prepared_at_utc_ms'] + lifetime, 'PLAN_DEADLINE_INVALID')
    _require(intent['approval']['scope'] != 'once' or intent['approval']['max_invocations'] == 1, 'PLAN_ONCE_BUDGET')
    leases = allocation['leases'].values()
    _require(len({(item['id'], item['generation']) for item in leases}) == len(_LEASE_ROLES), 'PLAN_LEASE_ALIAS')


@dataclass(frozen=True, slots=True, init=False)
class ReadExecutionPlan:
    """A validated immutable byte snapshot, never a launch capability."""
    _encoded: bytes = field(repr=False)

    def __init__(self, value: dict):
        _check_tree(value)
        _shape(value, _PLAN)
        _semantics(value)
        encoded = _canonical(value)
        _require(len(encoded) <= MAX_PLAN_BYTES, 'PLAN_BYTES_LIMIT')
        # Validate the owned snapshot too: no subsequent alias to caller data.
        snapshot = _parse(encoded)
        _shape(snapshot, _PLAN)
        _semantics(snapshot)
        object.__setattr__(self, '_encoded', encoded)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ReadExecutionPlan:
        return cls(_parse(raw))

    def to_bytes(self) -> bytes:
        return self._encoded

    def to_dict(self) -> dict:
        """Return a detached view; mutation cannot change this plan."""
        return _parse(self._encoded)

    @property
    def authorization_digest(self) -> str:
        intent = self.to_dict()['intent']
        return hashlib.sha256(b'gitgo.read.authorization.v1\0' + _canonical(intent)).hexdigest()

    @property
    def plan_digest(self) -> str:
        return hashlib.sha256(b'gitgo.read.plan.v1\0' + self._encoded).hexdigest()

    def persistence_bytes(self) -> bytes:
        """Preserve exact authority bytes without disabling storage redaction."""
        redacted = _canonical(redact_for_persistence(self.to_dict()))
        _require(redacted == self._encoded, 'PLAN_NOT_LOSSLESSLY_PERSISTABLE')
        return self._encoded

    def read_binding(self) -> ReadPlan:
        """Adapt identities for the existing local helper; no pin is acquired."""
        read = self.to_dict()['intent']['read']
        path_type = PureWindowsPath if read['file']['platform'] == 'win32' else PurePosixPath
        def identity(role):
            item = read[role]
            return [item['platform'], int(item['volume']), int(item['file_id'])]
        return ReadPlan.from_dict({'version': 1, 'workspace': read['workspace']['locator'],
            'root': read['root']['locator'], 'relative': str(path_type(*read['relative_parts'])),
            'workspace_identity': identity('workspace'), 'root_identity': identity('root'),
            'file_identity': identity('file')})

    @classmethod
    def from_read_binding(cls, binding: ReadPlan, *, contract: ExecutionContract,
                          intent: dict, allocation: dict, protection: dict,
                          offset: int, length: int) -> ReadExecutionPlan:
        """Compose an explicit description from the existing metadata binding.

        All policy and allocation fields are required from the trusted caller.
        This does not resolve content references or validate a live approval.
        """
        _require(type(binding) is ReadPlan and type(contract) is ExecutionContract
                 and contract == NATIVE_FILE_READ, 'PLAN_READ_CONTRACT_REQUIRED')
        _require(type(intent) is dict and 'read' not in intent, 'PLAN_READ_OVERRIDE_DENIED')
        _require(type(offset) is int and 0 <= offset < 2**64, 'PLAN_READ_RANGE')
        checked = ReadPlan.from_dict(binding.to_dict())
        def file(locator, identity: ObjectIdentity):
            return {'platform': identity.platform, 'volume': str(identity.volume),
                    'file_id': str(identity.file_id), 'locator': locator}
        path_type = PureWindowsPath if checked.file_identity.platform == 'win32' else PurePosixPath
        read = {'workspace': file(checked.workspace, checked.workspace_identity),
                'root': file(checked.root, checked.root_identity),
                'file': file(str(path_type(checked.root) / checked.relative), checked.file_identity),
                'relative_parts': list(path_type(checked.relative).parts), 'protection': protection,
                'content_semantics': 'same_object_mutable_content', 'offset': str(offset), 'length': length}
        return cls({'version': 1, 'intent': {**intent, 'read': read}, 'allocation': allocation})

    def require_admission(self) -> None:
        """No serialized plan can satisfy the State/native launch prerequisites."""
        raise ReadExecutionPlanBlocked('Durable admission and native capability verification are required')
