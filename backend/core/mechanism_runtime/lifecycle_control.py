"""Prepare lifecycle choices against existing task authorities, then commit.

This adapter binds mechanical identities; it creates no permission, instance
store or lifecycle implementation. Explicit legacy assertions stay strict.
"""
from copy import deepcopy

from .capability_runtime import instance_basis
from .contracts import bounded_json, digest
from .semantic_projection import component_contracts, semantic_program

OPERATIONS = {'activate', 'suspend', 'detach', 'reconcile', 'require'}


class LifecycleControl:
    def __init__(self, application):
        self.app = application
        self.prepared = {}

    def _bind(self, args):
        app = self.app
        context = app.capability_runtime.context()
        sources = app.sources(args['source_refs']) if 'source_refs' in args else context['sources']
        bound = {**deepcopy(args), 'source_refs': list(sources)}
        operation = args['operation']
        if operation not in OPERATIONS:
            raise ValueError('MECHANISM_OPERATION_UNSUPPORTED')
        if operation == 'require':
            current, head = app.requirements.read(app.task)
            revision = (current or {}).get('revision', 0)
            if args.get('expected_required_revision', revision) != revision:
                raise ValueError('MECHANISM_REQUIREMENTS_REVISION_CONFLICT')
            bound['expected_required_revision'] = revision
            bindings, targets = [], []
            for item in args['requirements']:
                definition, reference = app.repo.definition(item['definition_id'], item.get('definition_revision'))
                if not definition:
                    raise ValueError('MECHANISM_REQUIREMENT_DEFINITION_MISSING')
                bindings.append({**item, 'definition_revision': definition['revision']})
                targets.append({'definition_id': definition['id'], 'reference': reference,
                    'revision': definition['revision'], 'version_policy': item['version_policy'],
                    'program': semantic_program(definition['compiled']),
                    'component_contracts': component_contracts([definition['compiled']], app.registry)})
            bound['requirements'] = bindings
            basis = {'requirements_head': head}
            subject = {'kind': 'harness_required_constraints', 'current': current,
                'targets': targets, 'affects_owned_child_tasks': True}
        else:
            current, _ = app.repo.instance(app.task, args['instance_id'])
            if current is None:
                raise ValueError('MECHANISM_INSTANCE_MISSING')
            if args.get('expected_revision', current['revision']) != current['revision']:
                raise ValueError('MECHANISM_INSTANCE_REVISION_CONFLICT')
            bound['expected_revision'] = current['revision']
            basis = instance_basis(current)
            if operation == 'reconcile':
                subject = {'kind': 'harness_readonly_reconciliation', 'instance_id': current['id'],
                    'current_revision': current['revision'], 'effect_state': current.get('error', {}).get('effect_state'),
                    'never_replays_effects': True}
            else:
                if current.get('managed_by'):
                    raise ValueError('MECHANISM_REQUIRED_INSTANCE_OWNER_ONLY')
                subject = app.lifecycle_preview(bound)
                # Pin the exact immutable target selected during review. A
                # newly published head cannot change this pending operation.
                bound['definition_revision'] = subject['target_definition_revision']
        return {'context': context, 'bound': bound, 'basis': basis,
            'subject': {**subject, 'operation': operation,
                'source_evidence': {key: {'kind': value.get('kind'), 'digest': digest(value)} for key, value in sources.items()},
                'contract_revision': context['contract_revision'], 'authority_granted': False}}

    def prepare(self, args):
        args = bounded_json(args)
        key = digest(args)
        candidate = self._bind(args)
        previous = self.prepared.get(key)
        if previous is not None and candidate != previous:
            self.prepared.pop(key, None)
            raise ValueError('MECHANISM_LIFECYCLE_PREPARED_STATE_CHANGED: inspect the current state before proposing again')
        self.prepared[key] = candidate
        while len(self.prepared) > 32:
            del self.prepared[next(iter(self.prepared))]
        return deepcopy(candidate['subject'])

    def commit(self, args):
        from .application import configuration_allowed
        from backend.core.loop.mechanism_lifecycle import fence
        if not configuration_allowed(self.app.process):
            raise ValueError('MECHANISM_CONFIGURATION_CAPABILITY_REQUIRED')
        key = digest(args)
        prepared = self.prepared.get(key)
        if prepared is None:
            raise ValueError('MECHANISM_LIFECYCLE_PREPARATION_REQUIRED')
        try:
            with fence(self.app.process):
                if self._bind(args) != prepared:
                    raise ValueError('MECHANISM_LIFECYCLE_PREPARED_STATE_CHANGED')
                # The original coordinator/requirements CAS and capability
                # checks still own the only write and activation boundary.
                return self.app._invoke_current(prepared['bound'])
        finally:
            self.prepared.pop(key, None)
