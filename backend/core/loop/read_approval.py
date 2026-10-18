"""Ephemeral object pins owned by the existing permission/grant lifecycle.

The dict on AgentProcess owns OS resources, not permissions. A pin alone cannot
authorize execution. Serialized records without these live resources are stale.
No file contents are opened by the Host and no handles cross the child protocol.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from backend.core.sandbox_read import ReadBoundaryError, ReadRoot
from backend.core.sandbox_read_plan import ReadPlan

_LOCK = threading.RLock()
_RECORD_FIELDS = ('request_id', 'tool_contract_digest', 'expires_at', 'tool_name',
                  'effect', 'resource', 'arguments_digest', 'read_binding', 'approval_scope')
MAX_PINS = 64
_RETAINED_ADMISSIONS = {}


def requires_read_binding(tool):
    from .execution_contract import ExecutionContract
    contract = getattr(tool, 'execution_contract', None)
    return isinstance(contract, ExecutionContract) and contract.file_action == 'read_existing_file'


def _record(record):
    return json.dumps({key: record.get(key) for key in _RECORD_FIELDS},
                     sort_keys=True, separators=(',', ':'), allow_nan=False)


class ReadApprovalPin:
    def __init__(self, process, record, args):
        from backend.core.tools.workspace_tools import _read_target
        self._owners = ExitStack()
        self._revoked, self._users = False, 0
        self.grant_id = None
        self.grant_scope = None
        self.process_id = process.process_id
        self.task_id = process.active_task_id or process.process_id
        self.expires = datetime.fromisoformat(record['expires_at'])
        seconds = (self.expires - datetime.now(timezone.utc)).total_seconds()
        if not 0 < seconds <= 1800:
            raise ReadBoundaryError('Invalid read approval lifetime')
        self.deadline = time.monotonic() + seconds
        try:
            workspace, _, root, relative = _read_target(args)
            held_workspace = self._owners.enter_context(ReadRoot(workspace))
            held_root = self._owners.enter_context(ReadRoot(root))
            held_file = self._owners.enter_context(held_root.pin(relative))
            self.plan = ReadPlan(str(workspace), str(root), relative,
                held_workspace.identity, held_root.identity, held_file.identity)
            record['read_binding'] = self.plan.to_dict()
            self.record = _record(record)
            self._timer = threading.Timer(seconds, self.close)
            self._timer.daemon = True
            self._timer.start()
        except BaseException:
            self._owners.close()
            raise

    def live(self):
        with _LOCK:
            if time.monotonic() >= self.deadline or datetime.now(timezone.utc) >= self.expires:
                self.close()
            return not self._revoked

    def close(self):
        with _LOCK:
            self._revoked = True
            self._timer.cancel()
            if not self._users:
                self._owners.close()

    def acquire(self, args):
        with _LOCK:
            if not self.live():
                raise ReadBoundaryError('Read approval expired or revoked')
            self.plan.check_arguments(args)
            # Revalidate before spending the grant; the child checks again on
            # its actual readable descriptor. Pins prevent ID reuse in between.
            with ReadRoot(Path(self.plan.workspace)) as workspace, ReadRoot(Path(self.plan.root)) as root, root.pin(self.plan.relative) as file:
                if (workspace.identity, root.identity, file.identity) != (
                        self.plan.workspace_identity, self.plan.root_identity, self.plan.file_identity):
                    raise ReadBoundaryError('Approved read object changed')
            self._users += 1
            return ActiveRead(self)


class ActiveRead:
    def __init__(self, pin):
        self._pin, self._closed = pin, False

    def remaining(self):
        with _LOCK:
            if self._closed or not self._pin.live():
                raise ReadBoundaryError('Read approval is no longer live')
            return max(0, min(self._pin.deadline - time.monotonic(),
                (self._pin.expires - datetime.now(timezone.utc)).total_seconds()))

    def payload(self, args):
        self.remaining()
        self._pin.plan.check_arguments(args)
        return self._pin.plan.to_dict()

    def close(self):
        with _LOCK:
            if not self._closed:
                self._closed = True
                self._pin._users -= 1
                if self._pin._revoked and not self._pin._users:
                    self._pin._owners.close()


class AdmissionResources(ExitStack):
    def __init__(self):
        super().__init__()
        self.read_binding = None

    def retain(self, process, execution_id, call_index):
        """Unknown tree lifetime cannot release the active metadata pins."""
        key = (process.process_id, execution_id, call_index)
        # Keep owners even if the process model is later discarded.
        _RETAINED_ADMISSIONS.setdefault(key, []).append(self.pop_all())
        process.recovery = {**(process.recovery or {}), "resume_forbidden": True,
            "requires_manual_verification": True,
            "reasons": list((process.recovery or {}).get("reasons") or []) +
                [{"code": "NATIVE_CLEANUP_UNCONFIRMED", "execution_id": execution_id,
                  "call_index": call_index, "effect_state": "ambiguous"}]}


def current_pin(process, record):
    with _LOCK:
        pin = getattr(process, '_read_approval_pins', {}).get(record.get('request_id'))
        if not isinstance(pin, ReadApprovalPin) or not pin.live():
            return None
        if (pin.process_id != process.process_id
                or pin.task_id != (process.active_task_id or process.process_id)
                or getattr(process, 'cancel_requested', False)
                or getattr(process, 'cancellation_event', threading.Event()).is_set()):
            pin.close()
            return None
        if pin.record != _record(record) or ('grant_id' in record and (pin.grant_id != record['grant_id'] or pin.grant_scope != record.get('scope'))):
            return None
        return pin


def bind_read_grant(process, grant):
    with _LOCK:
        # grant_id is assigned only once, by the existing decision handler.
        request = {k: v for k, v in grant.items() if k != 'grant_id'}
        pin = current_pin(process, request)
        if pin is None or pin.grant_id is not None:
            return False
        pin.grant_id = grant['grant_id']
        pin.grant_scope = grant['scope']
        return True


def revoke_read_request(process, request_id):
    with _LOCK:
        pin = getattr(process, '_read_approval_pins', {}).pop(request_id, None)
        if pin is not None:
            pin.close()


def revoke_read_requests(process):
    with _LOCK:
        for pin in getattr(process, '_read_approval_pins', {}).values():
            pin.close()
        getattr(process, '_read_approval_pins', {}).clear()


def publish_read_request(process, decision_args, args, publish):
    # Same lock order as lifecycle: coordination -> resource lock. Never take
    # the grant lock while holding the resource lock.
    with process._coordination_lock, _LOCK:
        if process.pending_decision is not None:
            raise RuntimeError('a user decision is already pending')
        pins = process._read_approval_pins
        for key, pin in list(pins.items()):
            if not pin.live() or pin.task_id != (process.active_task_id or process.process_id):
                revoke_read_request(process, key)
        if len(pins) >= MAX_PINS:
            raise ReadBoundaryError('Too many live read approvals')
        record = decision_args['permission_request']
        record['approval_scope'] = ('exact_invocation' if record['approval_scope'] == 'exact_invocation'
                                    else 'exact_read_object_and_arguments')
        for option in decision_args['options']:
            if option.get('action') == 'allow_task':
                option['principle'] = 'Repeat this exact file read with the same arguments until this task ends or 30 minutes elapse.'
                option['immediate_effect'] = 'The same file object may be read again; replacement requires a new approval.'
                option['risks'] = 'Normal edits to the same file can change the contents returned by later reads.'
        pin = ReadApprovalPin(process, record, args)
        pins[record['request_id']] = pin
        try:
            return publish(process, decision_args)
        except BaseException:
            revoke_read_request(process, record['request_id'])
            raise


def admit_read_binding(process, tool, raw_args, prepared_args, selected, admission):
    from .permission_broker import arguments_digest
    if not requires_read_binding(tool) or not selected:
        return None
    with _LOCK:
        unique = {id(item): item for item in selected}.values()
        pins = [current_pin(process, item) for item in unique]
        if (admission is None or any(pin is None for pin in pins)
                or len({id(pin) for pin in pins}) != 1
                or any(item['arguments_digest'] != arguments_digest(raw_args) for item in selected)):
            return {'name': 'APPROVAL_GRANT_INVALID', 'message': 'Live exact read approval is required'}
        try:
            active = pins[0].acquire(prepared_args)
        except (OSError, ValueError) as exc:
            return {'name': 'APPROVAL_OBJECT_CHANGED', 'message': str(exc)}
        admission.read_binding = active
        # Closing the approval after the invocation cannot release pins while
        # another admitted task read still holds them.
        for item in selected:
            if item.get('remaining_uses') == 1:
                admission.callback(revoke_read_request, process, item['request_id'])
        admission.callback(active.close)
    return None
