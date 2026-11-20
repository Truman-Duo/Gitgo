"""Compatibility receipt tests; no administrator or native process is used."""
from copy import deepcopy
import json

import pytest

from backend.core import macos_broker_capabilities as capabilities


BOOT = '6e130791-3c47-4d94-9149-758b1659e0ee'


@pytest.fixture
def handshake():
    request = dict(op='capabilities', token='private-token', grant_id='c' * 32,
                   invocation_id='a' * 32, execution_basis='b' * 64)
    approved = dict(grant_id=request['grant_id'], owner_uid=501, identity=[10, 20, 501],
                    token_hash='private-hash', boot=BOOT, expires=500, project='/private/project')
    approved['project_object'] = dict(schema=1, volume_uuid='d' * 32, file_id=20,
                                      birth=[100, 0], owner_uid=501)
    binding = {**approved, 'token': request['token']}
    expected = dict(binding=binding, invocation_id=request['invocation_id'],
                    execution_basis=request['execution_basis'], owner_uid=501, boot=BOOT)
    receipt = capabilities.reply(request, approved, boot=BOOT)
    return request, approved, expected, receipt


@pytest.mark.parametrize('operation', list(capabilities.OPERATION_CONTRACTS))
def test_current_contract_round_trips_without_exposing_authority(handshake, operation):
    request, approved, expected, receipt = handshake
    # Exercise the JSON representation used by the real local socket protocol.
    received = json.loads(json.dumps(receipt))
    assert capabilities.require_operation(received, operation, **expected) is None
    text = json.dumps(received)
    for secret in (request['token'], approved['token_hash'], approved['project']):
        assert secret not in text
    assert not {'token', 'token_hash', 'expires', 'project'} & set(received)
    assert set(received['operation_contracts']) == {'run', 'git_read', 'git_objects', 'git_checkout'}


def test_reply_is_a_snapshot_and_cannot_mutate_live_authority(handshake):
    _, approved, expected, receipt = handshake
    receipt['project_identity'][0] = 11
    receipt['project_object']['birth'][0] = 101
    receipt['operation_contracts']['run'] = 2
    assert approved['identity'] == [10, 20, 501]
    assert expected['binding']['project_object']['birth'] == [100, 0]
    assert capabilities.OPERATION_CONTRACTS['run'] == 1


@pytest.mark.parametrize('changed', [
    {'event': 'denied', 'error': 'Unsupported broker operation or fields'},
    {'event': 'denied'},
    {'event': 'started', 'pid': 123},
    {}, None, [], True,
])
def test_readiness_or_old_helper_denial_never_counts_as_compatibility(handshake, changed):
    _, _, expected, _ = handshake
    with pytest.raises(capabilities.BrokerCompatibilityError, match='HELPER_UPGRADE_REQUIRED') as error:
        capabilities.require_operation(changed, 'git_checkout', **expected)
    assert error.value.code == 'HELPER_UPGRADE_REQUIRED'


@pytest.mark.parametrize('change', [
    {'event': 'status'}, {'protocol': 'another-broker'},
    {'protocol_version': True}, {'protocol_version': 0}, {'protocol_version': 2},
    {'invocation_id': 'e' * 32}, {'execution_basis': 'e' * 64}, {'grant_id': 'e' * 32},
    {'owner_uid': 502}, {'project_identity': [11, 20, 501]},
    {'boot': '7e130791-3c47-4d94-9149-758b1659e0ee'},
    {'unknown': 'field'},
])
def test_receipt_cannot_move_between_calls_grants_projects_or_boots(handshake, change):
    _, _, expected, receipt = handshake
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation({**receipt, **change}, 'run', **expected)


@pytest.mark.parametrize('field,value', [
    ('volume_uuid', 'e' * 32), ('file_id', 21), ('birth', [101, 0]), ('owner_uid', 502),
    ('schema', True),
])
def test_persistent_project_identity_is_bound_with_exact_types(handshake, field, value):
    _, _, expected, receipt = handshake
    receipt['project_object'][field] = value
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(receipt, 'run', **expected)


def test_project_object_cannot_be_dropped_or_added_to_trial_receipt(handshake):
    request, approved, expected, receipt = handshake
    del receipt['project_object']
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(receipt, 'run', **expected)
    del approved['project_object']
    del expected['binding']['project_object']
    trial = capabilities.reply(request, approved, boot=BOOT)
    capabilities.require_operation(trial, 'run', **expected)
    trial['project_object'] = {'schema': 1}
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(trial, 'run', **expected)


@pytest.mark.parametrize('contracts', [
    {}, {'run': 2}, {'run': True}, {'run': 0}, {'run': -1}, {'run': 1.0},
    {'run': '1'}, {'run': 1, 'bad/op': 1}, {'run': 1, 'future': True},
    {'run': 1, 'future': 2**31}, [], None,
    {**{'run': 1}, **{'future_' + str(i): 1 for i in range(32)}},
])
def test_missing_incompatible_or_malformed_operation_contract_refused(handshake, contracts):
    _, _, expected, receipt = handshake
    receipt['operation_contracts'] = contracts
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(receipt, 'run', **expected)


def test_contracts_are_selected_per_operation_without_claiming_future_support(handshake):
    _, _, expected, receipt = handshake
    receipt['operation_contracts'] = {'run': 1, 'git_checkout': 2, 'future_operation': 1}
    capabilities.require_operation(receipt, 'run', **expected)
    for operation in ('git_read', 'git_checkout', 'future_operation', 'status'):
        with pytest.raises(capabilities.BrokerCompatibilityError):
            capabilities.require_operation(receipt, operation, **expected)


@pytest.mark.parametrize('change', [
    {'op': 'run'}, {'grant_id': 'e' * 32}, {'invocation_id': 'bad'},
    {'execution_basis': True}, {'extra': 'field'},
])
def test_reply_refuses_wrong_request_or_grant_before_advertising(handshake, change):
    request, approved, _, _ = handshake
    with pytest.raises(ValueError):
        capabilities.reply({**request, **change}, approved, boot=BOOT)


def test_changed_current_binding_invalidates_an_earlier_success(handshake):
    _, _, expected, receipt = handshake
    capabilities.require_operation(receipt, 'run', **expected)
    renewed = deepcopy(expected)
    renewed['binding']['grant_id'] = 'e' * 32
    with pytest.raises(capabilities.BrokerCompatibilityError):
        capabilities.require_operation(receipt, 'run', **renewed)
