"""Per-invocation compatibility for the authenticated macOS project broker.

This is a wire contract, not an authorization mechanism or a health probe.
The caller must authenticate the peer and project before generating a reply,
and must recheck the live grant under the admission lock before execution.
Clients validate on the same connection that will carry the operation; a
receipt must never be cached across invocations, connections or helper swaps.

Protocol 1 binds the operation contracts to the original native invocation,
OS user, project grant and current boot. An operation revision covers its
request/result schemas and execution, cleanup and no-replay semantics. Additive
operations may use this protocol; incompatible shared semantics require a new
protocol version. A service being ready does not establish compatibility.
"""
from __future__ import annotations

from copy import deepcopy
import re
from types import MappingProxyType


CAPABILITY_OPERATION = 'capabilities'
CAPABILITY_REQUEST_FIELDS = frozenset({
    'op', 'token', 'grant_id', 'invocation_id', 'execution_basis',
})
PROTOCOL = 'gitgo.macos.project-broker'
PROTOCOL_VERSION = 1
OPERATION_CONTRACTS = MappingProxyType({
    'run': 1,
    'git_read': 1,
    'git_objects': 1,
    'git_checkout': 1,
})
_RECEIPT_FIELDS = frozenset({
    'event', 'protocol', 'protocol_version', 'operation_contracts',
    'invocation_id', 'execution_basis', 'grant_id', 'owner_uid',
    'project_identity', 'boot',
})
_DENIAL_FIELDS = frozenset({
    'event', 'protocol', 'protocol_version', 'invocation_id',
    'execution_basis', 'grant_id', 'code', 'error',
})
DENIAL_MESSAGES = MappingProxyType({
    'HELPER_GRANT_INVALID': 'The project authorization is no longer valid. Renew the authorized project binding before execution.',
    'HELPER_MAINTENANCE': 'The helper is in maintenance or its authorization change is incomplete. Complete or recover that operation before execution.',
    'HELPER_UPGRADE_REQUIRED': 'The helper protocol is incompatible. Install a compatible helper before execution.',
})


class BrokerGrantInvalidError(ValueError):
    """The service could not validate the current project grant."""

    code = 'HELPER_GRANT_INVALID'


class BrokerAuthorityError(PermissionError):
    """Keep the existing PermissionError boundary for failed credentials."""

    code = 'HELPER_GRANT_INVALID'


class BrokerMaintenanceError(ValueError):
    """A known lifecycle state prevents new execution."""

    code = 'HELPER_MAINTENANCE'


class BrokerBusyError(RuntimeError):
    """Preserve existing RuntimeError handling for a held lifecycle lock."""

    code = 'HELPER_MAINTENANCE'


class BrokerAdmissionError(ValueError):
    """A structured denial from the authenticated peer, before execution."""

    def __init__(self, code):
        if code not in DENIAL_MESSAGES:
            raise ValueError('Unsupported broker denial code')
        self.code = code
        super().__init__(code + ': ' + DENIAL_MESSAGES[code])


class BrokerCompatibilityError(ValueError):
    """No execution is authorized by a missing or incompatible handshake."""

    code = 'HELPER_UPGRADE_REQUIRED'

    def __init__(self, detail):
        super().__init__(self.code + ': ' + detail)


def denial(request, error):
    """Add typed refusal metadata only to a well-formed capability probe.

    Unknown exceptions keep the legacy denial shape. Neither arbitrary error
    text nor a caller-selected code can become a recognized admission result.
    Functional requests and recovery status retain their original wire format.
    """
    if type(request) is not dict:
        request = {}
    result = {'event': 'denied', 'invocation_id': request.get('invocation_id', ''),
              'error': str(error)}
    if (set(request) != CAPABILITY_REQUEST_FIELDS
            or request.get('op') != CAPABILITY_OPERATION
            or not isinstance(error, (BrokerGrantInvalidError, BrokerAuthorityError,
                                      BrokerMaintenanceError, BrokerBusyError, BrokerCompatibilityError))):
        return result
    result.update(protocol=PROTOCOL, protocol_version=PROTOCOL_VERSION,
                  execution_basis=request['execution_basis'], grant_id=request['grant_id'],
                  code=error.code, error=DENIAL_MESSAGES[error.code])
    return result


def require_denial(receipt, binding, invocation_id, execution_basis):
    """Recognize only this protocol's finite, invocation-bound refusals."""
    expected = {'event': 'denied', 'protocol': PROTOCOL, 'protocol_version': PROTOCOL_VERSION,
                'invocation_id': invocation_id, 'execution_basis': execution_basis,
                'grant_id': binding['grant_id']}
    if (type(receipt) is not dict or set(receipt) != _DENIAL_FIELDS
            or any(not _same_value(receipt[key], value) for key, value in expected.items())
            or type(receipt['code']) is not str or receipt['code'] not in DENIAL_MESSAGES
            or type(receipt['error']) is not str or len(receipt['error']) > 1024):
        raise BrokerCompatibilityError('Helper did not return a recognized bound denial')
    if receipt['code'] == 'HELPER_UPGRADE_REQUIRED':
        raise BrokerCompatibilityError(DENIAL_MESSAGES[receipt['code']])
    raise BrokerAdmissionError(receipt['code'])


def _same_value(actual, expected):
    """Protocol equality must not treat True as revision/UID/device number 1."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return (set(actual) == set(expected)
                and all(_same_value(actual[key], value) for key, value in expected.items()))
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _same_value(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def reply(request, approved, *, boot):
    """Return metadata after the service's existing live authorization checks.

    ``approved`` is the grant returned by that check, never client JSON. This
    function neither reads credentials nor reserves an invocation or lease.
    ``boot`` is the service's current verified boot identity.
    """
    if (type(request) is not dict or set(request) != CAPABILITY_REQUEST_FIELDS
            or request['op'] != CAPABILITY_OPERATION):
        raise ValueError('Invalid broker capability request')
    if (type(request['invocation_id']) is not str
            or re.fullmatch('[0-9a-f]{32}', request['invocation_id']) is None
            or type(request['execution_basis']) is not str
            or re.fullmatch('[0-9a-f]{64}', request['execution_basis']) is None):
        raise ValueError('Invalid capability invocation binding')
    if not _same_value(request['grant_id'], approved['grant_id']):
        raise ValueError('Capability grant changed')
    result = {
        'event': CAPABILITY_OPERATION,
        'protocol': PROTOCOL,
        'protocol_version': PROTOCOL_VERSION,
        'operation_contracts': dict(OPERATION_CONTRACTS),
        'invocation_id': request['invocation_id'],
        'execution_basis': request['execution_basis'],
        'grant_id': approved['grant_id'],
        'owner_uid': approved['owner_uid'],
        'project_identity': approved['identity'],
        'boot': boot,
    }
    if 'project_object' in approved:
        result['project_object'] = approved['project_object']
    return deepcopy(result)


def require_operation(receipt, operation, *, binding, invocation_id,
                      execution_basis, owner_uid, boot):
    """Refuse to send a functional request unless this exact receipt permits it.

    Expected values come from the current authenticated Host binding and its
    original native record. Old-helper denials are not retried as legacy
    functional requests. Recovery ``status`` deliberately keeps its separate
    read-only protocol, including expired-grant and cross-boot reconciliation.
    """
    if type(operation) is not str or operation not in OPERATION_CONTRACTS:
        raise BrokerCompatibilityError('Operation has no supported broker contract')
    if type(receipt) is dict and receipt.get('event') == 'denied':
        require_denial(receipt, binding, invocation_id, execution_basis)
    fields = _RECEIPT_FIELDS | ({'project_object'} if 'project_object' in binding else set())
    if type(receipt) is not dict or set(receipt) != fields:
        raise BrokerCompatibilityError('Helper did not return a compatible capability receipt')
    if (receipt['event'] != CAPABILITY_OPERATION or receipt['protocol'] != PROTOCOL
            or type(receipt['protocol_version']) is not int
            or receipt['protocol_version'] != PROTOCOL_VERSION):
        raise BrokerCompatibilityError('Helper protocol is unsupported')

    expected = {
        'invocation_id': invocation_id,
        'execution_basis': execution_basis,
        'grant_id': binding['grant_id'],
        'owner_uid': owner_uid,
        'project_identity': binding['identity'],
        'boot': boot,
    }
    if 'project_object' in binding:
        expected['project_object'] = binding['project_object']
    if any(not _same_value(receipt[key], value) for key, value in expected.items()):
        raise BrokerCompatibilityError('Helper capability receipt is not bound to this invocation')

    contracts = receipt['operation_contracts']
    if (type(contracts) is not dict or len(contracts) > 32
            or any(type(name) is not str or re.fullmatch('[a-z][a-z0-9_]{0,63}', name) is None
                   or type(revision) is not int or not 1 <= revision <= 2**31 - 1
                   for name, revision in contracts.items())):
        raise BrokerCompatibilityError('Helper operation contracts are malformed')
    if contracts.get(operation) != OPERATION_CONTRACTS[operation]:
        raise BrokerCompatibilityError('Helper does not support the required ' + operation + ' contract')
