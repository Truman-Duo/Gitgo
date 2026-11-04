"""Retain a reviewed draft's inputs, never an execution or authorization ticket.

The existing project CAS/KV binds its owner, source and exact compiled meaning.
Submission still passes normal semantic admission and the original commit fence.
"""
from copy import deepcopy

from .contracts import bounded_json, digest, encoded
from .repository import NAMESPACE


def preview_key(runtime, reference):
    return 'capability_preview/' + digest([runtime.app.task, runtime.app.process.process_id, reference])


def retain(runtime, arguments, subject):
    repo = runtime.app.repo
    record = {'project_id': repo.project_id, 'kind': 'capability_preview',
        'task_id': runtime.app.task, 'process_id': runtime.app.process.process_id,
        'arguments': deepcopy(arguments), 'context': runtime.context(),
        'subject_digest': digest(subject)}
    bounded_json(record, maximum=524288)
    reference = repo.storage.put_blob(encoded(record), media_type='application/json')
    key = preview_key(runtime, reference)
    if not repo.storage.compare_state_ref(NAMESPACE, key, reference, expected_ref=None):
        existing, stored = repo._read(key)
        if existing != record or stored != reference:
            raise ValueError('CAPABILITY_PREVIEW_IDENTITY_CONFLICT')
    return reference


def prepare(runtime, request):
    reference = request['preview_ref']
    record, stored = runtime.app.repo._read(preview_key(runtime, reference))
    if (not record or stored != reference or record.get('kind') != 'capability_preview'
            or record.get('task_id') != runtime.app.task
            or record.get('process_id') != runtime.app.process.process_id):
        raise ValueError('CAPABILITY_PREVIEW_UNAVAILABLE: preview again in this task; no authority was granted')
    if record['context'] != runtime.context():
        raise ValueError('CAPABILITY_PREVIEW_STALE: actual sources or task contract changed; preview again')
    arguments = record['arguments']
    subject = runtime.prepare(arguments)
    if digest(subject) != record['subject_digest']:
        raise ValueError('CAPABILITY_PREVIEW_STALE: definition, instance or component meaning changed; preview again')
    # Merely possessing a draft ref does not prepare an execution. Only this
    # normal admission path binds the exact plan to the short commit request.
    runtime.prepared[digest(request)] = deepcopy(runtime.prepared[digest(arguments)])
    return subject
