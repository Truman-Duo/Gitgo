"""Pure plan boundaries: no grants, files or processes are acquired here."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys

import pytest

from backend.core.loop.execution_contract import NATIVE_FILE_READ, NATIVE_PROCESS, data_broker
from backend.core.loop.read_execution_plan import (
    ReadExecutionPlan, ReadExecutionPlanBlocked, ReadExecutionPlanError,
)
from backend.core.sandbox_read import ObjectIdentity, ReadBoundaryError, ReadRoot
from backend.core.sandbox_read_plan import ReadPlan


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / 'fixtures' / 'read_execution_plan.json').read_text(encoding='utf8'))


def change(payload, path, value):
    result = deepcopy(payload)
    node = result
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return result


def test_round_trip_and_known_digest(payload):
    plan = ReadExecutionPlan(payload)
    assert ReadExecutionPlan.from_bytes(plan.to_bytes()) == plan
    assert plan.authorization_digest == '449d6f275baed6eb7884fe6d21814393b0946ba23352bcde921a55665b454cd1'
    assert plan.plan_digest == '86cf1db2b8ff7eaed6bfe5d1d6aab534b5f997465ec74d8c15341b6f1c1fe460'
    assert plan.persistence_bytes() == plan.to_bytes()
    reordered = dict(reversed(list(payload.items())))
    assert ReadExecutionPlan(reordered).to_bytes() == plan.to_bytes()


def test_no_nested_mutable_alias(payload):
    plan = ReadExecutionPlan(payload)
    original = plan.to_bytes()
    payload['intent']['limits']['wall_ms'] = 1
    view = plan.to_dict()
    view['intent']['transport']['pipes'][0]['object_policy_ref'] = 'f' * 64
    assert plan.to_bytes() == original
    assert plan.to_dict()['intent']['limits']['wall_ms'] == 5000
    with pytest.raises(FrozenInstanceError):
        plan._encoded = b'{}'


@pytest.mark.parametrize(('path', 'value'), [
    (['version'], 2), (['version'], True), (['version'], 1.0),
    (['intent', 'kind'], 'data_broker'), (['intent', 'action'], 'write_file'),
    (['intent', 'runtime', 'descendants'], 'any_tool'),
    (['intent', 'isolation', 'network'], 'allow'),
    (['intent', 'isolation', 'filesystem_mutation'], 'allow'),
    (['intent', 'isolation', 'platform'], 'darwin'),
    (['intent', 'read', 'file', 'file_id'], str(2**128)),
    (['intent', 'read', 'file', 'volume'], str(2**64)),
    (['intent', 'read', 'file', 'file_id'], '01'),
    (['intent', 'read', 'file', 'platform'], 'linux'),
    (['intent', 'read', 'offset'], str(2**64 - 1)),
    (['intent', 'read', 'relative_parts'], ['..', 'note.txt']),
    (['intent', 'read', 'relative_parts'], ['note.txt:stream']),
    (['intent', 'read', 'relative_parts'], ['CON.txt']),
    (['intent', 'read', 'root', 'locator'], 'C:\\'),
    (['intent', 'read', 'root', 'locator'], '\\\\server\\share\\project'),
    (['intent', 'read', 'workspace', 'locator'], 'C:\\CON\\project'),
    (['intent', 'read', 'file', 'locator'], 'C:\\fixture\\project\\sibling.txt'),
    (['intent', 'limits', 'process_tree_count'], 0),
    (['intent', 'limits', 'host_buffer_bytes'], 10000),
    (['intent', 'limits', 'host_io_workers'], 2),
    (['intent', 'limits', 'host_storage_bytes'], 65536),
    (['intent', 'transport', 'pending'], 5),
    (['intent', 'transport', 'request_total_bytes'], 127),
    (['intent', 'transport', 'read_per_message_bytes'], 65),
    (['intent', 'transport', 'pipes', 0, 'access'], 'write_data_sync'),
    (['intent', 'transport', 'pipes', 1, 'role'], 'request'),
    (['intent', 'approval', 'max_invocations'], 2),
    (['allocation', 'deadline_utc_ms'], 1792022405001),
    (['allocation', 'deadline_utc_ms'], 1792022400000),
    (['allocation', 'owner_epoch'], -1),
], ids=lambda value: str(value)[:60])
def test_invalid_authority_or_budget_description(payload, path, value):
    with pytest.raises(ReadExecutionPlanError):
        ReadExecutionPlan(change(payload, path, value))


def test_no_missing_field_or_backend_default(payload):
    paths = []
    def visit(value, prefix):
        if type(value) is dict:
            for key, item in value.items():
                paths.append(prefix + [key])
                visit(item, prefix + [key])
        elif type(value) is list and value:
            visit(value[0], prefix + [0])
    visit(payload, [])
    assert len(paths) == 129
    for path in paths:
        broken = deepcopy(payload)
        node = broken
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
        with pytest.raises(ReadExecutionPlanError):
            ReadExecutionPlan(broken)


def test_unknown_fields_and_lease_alias_rejected(payload):
    payload['intent']['limits']['default_permissions'] = {}
    with pytest.raises(ReadExecutionPlanError):
        ReadExecutionPlan(payload)
    del payload['intent']['limits']['default_permissions']
    payload['allocation']['leases']['scratch'] = payload['allocation']['leases']['runtime']
    with pytest.raises(ReadExecutionPlanError):
        ReadExecutionPlan(payload)


@pytest.mark.parametrize('raw', [
    b'{"version":1,"version":1}', b'{"version":NaN}', b'{"version":Infinity}',
    b'{"version":1e0}', b'{"version":1.0}', b'"\xff"', b'"\\ud800"', b'"\\u0000"',
    b' ' * (256 * 1024 + 1), b'[' * 10000 + b'0' + b']' * 10000,
    b'{"version":' + b'9' * 5000 + b'}', b'[]', b'null',
], ids=['duplicate', 'nan', 'infinity', 'exponent', 'float', 'utf8', 'surrogate', 'nul',
        'byte-limit', 'depth-limit', 'integer-limit', 'array', 'null'])
def test_untrusted_bytes_fail_with_bounded_errors(raw):
    with pytest.raises(ReadExecutionPlanError) as caught:
        ReadExecutionPlan.from_bytes(raw)
    assert len(str(caught.value)) < 80


def test_duplicate_nested_key_and_braces_in_string(payload):
    raw = json.dumps(payload).encode().replace(b'"wall_ms": 5000', b'"wall_ms":5000,"wall_ms":5000')
    with pytest.raises(ReadExecutionPlanError, match='PLAN_DUPLICATE_KEY'):
        ReadExecutionPlan.from_bytes(raw)
    payload['intent']['read']['relative_parts'] = ['braces{[\\".txt']
    # Unsafe Windows component still fails semantic validation, not JSON depth.
    with pytest.raises(ReadExecutionPlanError, match='PLAN_RELATIVE_INVALID|PLAN_LOCATOR'):
        ReadExecutionPlan.from_bytes(json.dumps(payload).encode())


def test_ordered_path_components_can_repeat(payload):
    payload['intent']['read']['relative_parts'] = ['dir', 'dir', 'note.txt']
    payload['intent']['read']['file']['locator'] = 'C:\\fixture\\project\\dir\\dir\\note.txt'
    assert ReadExecutionPlan(payload).to_dict()['intent']['read']['relative_parts'] == ['dir', 'dir', 'note.txt']


def test_fresh_execution_preserves_approval_but_changes_plan(payload):
    original = ReadExecutionPlan(payload)
    payload['allocation']['execution_id'] = 'another-execution'
    payload['allocation']['native_identity_ref'] = 'f' * 64
    fresh = ReadExecutionPlan(payload)
    assert fresh.authorization_digest == original.authorization_digest
    assert fresh.plan_digest != original.plan_digest


@pytest.mark.parametrize(('path', 'value'), [
    (['intent', 'context', 'task_id'], 'another-task'),
    (['intent', 'read', 'protection', 'generation'], 2),
    (['intent', 'runtime', 'manifest_ref'], 'a' * 64),
    (['intent', 'tool', 'arguments_ref'], 'b' * 64),
    (['intent', 'transport', 'pipes', 0, 'object_policy_ref'], 'c' * 64),
    (['intent', 'limits', 'memory_tree_bytes'], 134217729),
])
def test_permissions_and_identity_change_approval_digest(payload, path, value):
    assert ReadExecutionPlan(change(payload, path, value)).authorization_digest != ReadExecutionPlan(payload).authorization_digest


def test_redaction_cannot_change_plan_on_persistence(payload):
    # This is fake test text, not a credential.
    leaf = 'sk-' + 'TESTFIXTURE' + '1234567890.txt'
    payload['intent']['read']['relative_parts'] = [leaf]
    payload['intent']['read']['file']['locator'] = 'C:\\fixture\\project\\' + leaf
    plan = ReadExecutionPlan(payload)
    assert leaf not in repr(plan)
    with pytest.raises(ReadExecutionPlanError, match='PLAN_NOT_LOSSLESSLY_PERSISTABLE') as caught:
        plan.persistence_bytes()
    assert leaf not in str(caught.value)


def local_binding():
    root = 'C:\\fixture\\project' if sys.platform == 'win32' else '/fixture/project'
    return ReadPlan(root, root, 'note.txt', ObjectIdentity(sys.platform, 7, 1),
                    ObjectIdentity(sys.platform, 7, 1), ObjectIdentity(sys.platform, 7, 2))


def test_existing_binding_adapter_is_pure_and_round_trips(payload, monkeypatch):
    if sys.platform not in {'win32', 'linux'}:
        pytest.skip('Existing native read binding supports Windows/Linux')
    def forbidden(*args, **kwargs):
        pytest.fail('Plan adaptation must not access files or launch processes')
    monkeypatch.setattr(ReadRoot, '__init__', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    intent = deepcopy(payload['intent'])
    intent.pop('read')
    intent['isolation']['platform'] = sys.platform
    binding = local_binding()
    plan = ReadExecutionPlan.from_read_binding(binding, contract=NATIVE_FILE_READ,
        intent=intent, allocation=payload['allocation'], protection={'id': 'pin-adapter', 'generation': 1},
        offset=0, length=64)
    assert plan.read_binding() == binding
    assert plan.to_dict()['intent']['read']['file']['file_id'] == '2'
    with pytest.raises(ReadExecutionPlanBlocked):
        plan.require_admission()


@pytest.mark.parametrize('contract', [NATIVE_PROCESS, data_broker('test.reader')])
def test_binding_adapter_cannot_switch_execution_authority(payload, contract):
    with pytest.raises(ReadExecutionPlanError, match='PLAN_READ_CONTRACT_REQUIRED'):
        ReadExecutionPlan.from_read_binding(local_binding(), contract=contract,
            intent=payload['intent'], allocation=payload['allocation'], protection={'id': 'pin', 'generation': 1},
            offset=0, length=64)


def test_valid_references_and_digests_do_not_admit_launch(payload):
    plan = ReadExecutionPlan.from_bytes(ReadExecutionPlan(payload).to_bytes())
    with pytest.raises(ReadExecutionPlanBlocked) as caught:
        plan.require_admission()
    assert caught.value.code == 'READ_EXECUTION_PLAN_NOT_ADMITTED'


def test_foreign_plan_does_not_create_local_read_binding(payload):
    if sys.platform == 'win32':
        payload['intent']['isolation']['platform'] = 'linux'
        for role in ('workspace', 'root', 'file'):
            item = payload['intent']['read'][role]
            item['platform'] = 'linux'
            item['locator'] = '/fixture/project' + ('/note.txt' if role == 'file' else '')
    else:
        assert payload['intent']['isolation']['platform'] == 'win32'
    plan = ReadExecutionPlan(payload)
    with pytest.raises(ReadBoundaryError):
        plan.read_binding()
