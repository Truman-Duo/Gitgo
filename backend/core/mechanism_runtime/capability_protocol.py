"""Current model signature, separate from accepted legacy program calls."""
from copy import deepcopy

from .contracts import bounded_json


def object_schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1}
CALL = object_schema({
    'id': TEXT, 'use': TEXT, 'version': {'type': 'integer', 'minimum': 1},
    'parameters': {'type': 'object'},
    'on': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT,
        'description': 'Optional subset of declaration.on and this component\'s supported boundaries. Omit only if the call should run at every applicable subscribed boundary.'},
    'inputs': {'type': 'object', 'additionalProperties': TEXT},
    'when': {'anyOf': [TEXT, object_schema({'output': TEXT, 'equals': {'type': 'boolean'}}, ('output',))]},
    'after': TEXT}, ('id', 'use'))
DECLARATION = object_schema({'id': TEXT, 'intent': TEXT,
    'on': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT},
    'calls': {'type': 'array', 'minItems': 1, 'maxItems': 63, 'items': CALL}}, ('id', 'intent', 'on', 'calls'))
EDIT = {**CALL, 'required': ['id']}
REVISION = {**object_schema({'intent': TEXT, 'on': DECLARATION['properties']['on'],
    'calls': {'type': 'array', 'maxItems': 63, 'items': EDIT},
    'remove': {'type': 'array', 'uniqueItems': True, 'items': TEXT}}), 'minProperties': 1}
AUTHORING = {'apply', 'revise', 'use'}
COMMIT = object_schema({'operation': {'const': 'commit'},
    'preview_ref': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}}, ('operation', 'preview_ref'))
LEGACY = {'preview_revision', 'publish', 'publish_revision', 'instantiate'}
OBSERVATIONS = {'catalog', 'status', 'preview', 'preview_revision'}
INBOX = {'inbox_revise', 'inbox_merge', 'inbox_discard', 'inbox_adopt'}
LIFECYCLE = {'activate', 'suspend', 'detach', 'reconcile', 'require'}


def lifecycle_schema(operation, *, legacy=False):
    fields = {'operation': {'const': operation}}
    if operation == 'require':
        fields['requirements'] = {'type': 'array', 'maxItems': 16, 'items': object_schema({
            'definition_id': TEXT, 'definition_revision': {'type': 'integer', 'minimum': 1},
            'version_policy': {'enum': ['pinned', 'follow_compatible']}}, ('definition_id', 'version_policy'))}
        required = ['operation', 'requirements']
    else:
        fields['instance_id'] = TEXT
        required = ['operation', 'instance_id']
        if operation == 'activate':
            fields['definition_revision'] = {'type': 'integer', 'minimum': 1}
    if legacy:
        fields['source_refs'] = {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': TEXT}
        fields['expected_required_revision' if operation == 'require' else 'expected_revision'] = {
            'type': 'integer', 'minimum': 0 if operation == 'require' else 1}
        if operation in {'suspend', 'detach'}:
            fields['definition_revision'] = {'type': 'integer', 'minimum': 1}
    return object_schema(fields, required)


def inbox_schema(operation):
    fields = {'operation': {'const': operation}, 'instance_id': TEXT}
    required = ['operation']
    if operation == 'inbox_merge':
        fields['candidates'] = {'type': 'array', 'minItems': 2, 'maxItems': 8,
            'items': object_schema({'id': TEXT}, ('id',))}
        required.append('candidates')
    else:
        fields['candidate_id'] = TEXT
        required.append('candidate_id')
    if operation in {'inbox_revise', 'inbox_merge'}:
        fields['text'] = {**TEXT, 'maxLength': 16000}
        required.append('text')
    if operation == 'inbox_adopt':
        fields['quote'] = {**TEXT, 'maxLength': 4000}
        required.append('quote')
    return object_schema(fields, required)


def authoring_operation(args):
    if 'declaration' in args:
        return 'apply'
    if 'revision' in args:
        return 'revise'
    return 'use'


def authoring_schema(operation, *, preview=False):
    fields = {'operation': {'const': 'preview' if preview else operation},
        'instance_id': TEXT, 'version_policy': {'enum': ['pinned', 'follow_compatible']}}
    required = ['operation']
    if operation == 'apply':
        fields['declaration'] = DECLARATION
        required += ['declaration']
    else:
        fields['definition_id'] = TEXT
        required += ['definition_id']
        if operation == 'revise':
            fields['revision'] = REVISION
            required += ['revision']
        else:
            fields['definition_revision'] = {'type': 'integer', 'minimum': 1,
                'description': 'Exact saved revision for use or its preview. Omission selects the latest saved revision, not a previously previewed revision. pinned fixes the selected version; it does not select an older one.'}
    return object_schema(fields, required)


def current_schema(accepted, allowed_operations):
    """The advertised surface omits IR, source ids and publication receipts."""
    properties = deepcopy(accepted['properties'])
    names = [op for op in allowed_operations if op not in LEGACY]
    properties['operation'] = {'enum': names}
    properties['declaration'] = DECLARATION
    properties['revision'] = REVISION
    for key in ('program', 'preview_digest', 'changes', 'expected_head', 'source_refs',
                'expected_revision', 'expected_required_revision', 'candidate_revision', 'contract_revision'):
        properties.pop(key, None)
    variants = [authoring_schema(op) for op in sorted(AUTHORING) if op in names]
    if 'commit' in names:
        variants.append(deepcopy(COMMIT))
    variants += [authoring_schema(op, preview=True) for op in sorted(AUTHORING)]
    variants += [inbox_schema(op) for op in sorted(INBOX) if op in names]
    variants += [lifecycle_schema(op) for op in sorted(LIFECYCLE) if op in names]
    remaining = [op for op in names if op not in AUTHORING | INBOX | LIFECYCLE | {'preview', 'commit'}]
    variants.append({'properties': {'operation': {'enum': remaining}}})
    # Root properties already validate these exact subschemas. Branches keep
    # each allowed property name (for additionalProperties=False), their own
    # discriminator and required list, without printing the same declaration
    # several times. Unequal constraints stay local; admission is unchanged.
    for branch in variants:
        for name, schema in list(branch.get('properties', {}).items()):
            if name in properties and schema == properties[name]:
                branch['properties'][name] = {}
    return {**object_schema(properties, ('operation',)), 'oneOf': variants}


def validate_request(args):
    """Discriminate first so errors identify the selected signature, not a
    many-branch anyOf dump. Never include project-private argument values.
    """
    bounded_json(args)
    operation = args.get('operation')
    if operation == 'commit' or operation in AUTHORING or operation == 'preview' and 'program' not in args:
        schema = COMMIT if operation == 'commit' else authoring_schema(
            authoring_operation(args) if operation == 'preview' else operation, preview=operation == 'preview')
        from jsonschema import Draft202012Validator
        errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: str(e.path))
        if errors:
            error = errors[0]
            path = '.'.join(map(str, error.path)) or 'arguments'
            if error.validator == 'additionalProperties':
                unknown = sorted(set(error.instance) - set(error.schema.get('properties', {})))
                reason = 'unsupported fields: ' + ', '.join(unknown)
            elif error.validator == 'required':
                reason = 'required fields: ' + ', '.join(error.schema['required'])
            else:
                reason = error.validator
            raise ValueError(f'CAPABILITY_ARGUMENT_INVALID: {operation}.{path}: {reason}')
    elif operation in LIFECYCLE:
        from jsonschema import Draft202012Validator
        errors = sorted(Draft202012Validator(lifecycle_schema(operation, legacy=True)).iter_errors(args), key=lambda e: str(e.path))
        if errors:
            raise ValueError(f'CAPABILITY_ARGUMENT_INVALID: {operation}: supply instance_id, or requirements for require. Runtime binds real sources and current versions; explicit legacy assertions must match.')
    elif operation in INBOX and 'source_refs' not in args:
        from jsonschema import Draft202012Validator
        errors = sorted(Draft202012Validator(inbox_schema(operation)).iter_errors(args), key=lambda e: str(e.path))
        if errors:
            raise ValueError(f'CAPABILITY_ARGUMENT_INVALID: {operation}: use candidate_id (or candidates with id), text for revise/merge, and actual user quote for adopt; instance_id is needed only if ambiguous. Runtime binds sources and versions.')
    elif ('declaration' in args or 'revision' in args):
        raise ValueError('CAPABILITY_PROTOCOL_MIXED: use preview/apply/revise with declarations; legacy programs cannot carry declaration/revision')


def preview_request(runtime, args):
    operation = authoring_operation(args)
    arguments = {**args, 'operation': operation}
    subject = runtime.prepare(arguments, remember=False)
    from .capability_preview import retain
    reference = retain(runtime, arguments, subject)
    return {**subject, 'committed': False, 'effect_state': 'not_committed',
        'next_operation': operation, 'preview_ref': reference, 'authority_granted': False}
