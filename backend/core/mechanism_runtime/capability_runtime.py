"""Task-bound capability deployment; one prepared meaning and atomic commit.

The Agent chooses behavior. This runtime owns source/version binding, typed
lowering, publication, instance staging and receipt identity. It executes no
business callback and never grants authority. Legacy programs remain readable.
"""
from copy import deepcopy
from contextlib import ExitStack

from .capability_declaration import lower, revise
from .contracts import digest, encoded, bounded_json, identifier
from .coordinator import difference
from .repository import NAMESPACE
from .semantic_projection import semantic_program, component_contracts, describe_instance


def instance_basis(value):
    # Callback progress/counters are deliberately not a semantic revision.
    # They are merged from the freshest CAS head, never reset by deployment.
    return {key: deepcopy((value or {}).get(key)) for key in (
        'id', 'definition_id', 'revision', 'epoch', 'desired_ref', 'effective_ref',
        'version_policy', 'user_control', 'managed_by', 'phase')}


class CapabilityRuntime:
    def __init__(self, application):
        self.app = application
        self.prepared = {}

    def context(self):
        from backend.core.loop.task_maintenance import source_receipts
        process = self.app.process
        sources = source_receipts(process)
        if not sources:
            raise ValueError('MECHANISM_ACTUAL_USER_SOURCE_REQUIRED')
        context, _ = process.read_context_snapshot()
        return {'task': self.app.task, 'sources': sources,
            'contract_revision': (context.get('task_contract') or {}).get('revision', 0)}

    def prepare(self, args, *, remember=True):
        args = bounded_json(args)
        app, repo = self.app, self.app.repo
        operation = args['operation']
        if operation == 'commit':
            if set(args) != {'operation', 'preview_ref'}:
                raise ValueError('CAPABILITY_PREVIEW_ARGUMENTS_IMMUTABLE')
            from .capability_preview import prepare
            return prepare(self, args)
        if operation not in {'apply', 'revise', 'use'}:
            raise ValueError('CAPABILITY_OPERATION_UNSUPPORTED')
        if set(args) - {'operation', 'declaration', 'definition_id', 'instance_id', 'version_policy', 'definition_revision', 'revision'}:
            raise ValueError('CAPABILITY_RUNTIME_BINDINGS_ARE_NOT_MODEL_ARGUMENTS')
        context = self.context()
        request = digest({'arguments': args, 'context': context})
        receipt_key = 'capability_operation/' + digest([app.task, request])
        receipt, _ = repo._read(receipt_key)
        if receipt:
            prepared = {'replay': receipt, 'context': context, 'request': request}
            if remember:
                self.prepared[digest(args)] = prepared
            return deepcopy(receipt['review_subject'])
        cached = self.prepared.get(digest(args)) if remember else None
        if cached is not None:
            if cached['context'] != context:
                self.prepared.pop(digest(args), None)
                raise ValueError('CAPABILITY_SOURCE_OR_CONTRACT_CHANGED')
            if 'replay' not in cached:
                _, head = repo.definition(cached['definition']['id'])
                current, _ = repo.instance(app.task, cached['instance_id'])
                if head != cached['head'] or instance_basis(current) != cached['basis']:
                    self.prepared.pop(digest(args), None)
                    raise ValueError('CAPABILITY_PREPARED_STATE_CHANGED: inspect current state before proposing again')
            # A second execution-boundary check must not silently compile a
            # different meaning from the subject already semantically reviewed.
            return deepcopy(cached.get('replay', {}).get('review_subject') or cached['subject'])
        name = identifier(args['declaration']['id'] if operation == 'apply' else args['definition_id'])
        previous, head = repo.definition(name)
        instance_id = identifier(args.get('instance_id', name))
        current, _ = repo.instance(app.task, instance_id)
        if current and (current['definition_id'] != name or current.get('managed_by')):
            raise ValueError('CAPABILITY_INSTANCE_OWNER_CONFLICT')
        if current and current['phase'] == 'blocked':
            raise ValueError('MECHANISM_RECONCILIATION_REQUIRED')
        if operation == 'use':
            definition, reference = repo.definition(name, args.get('definition_revision'))
            if not definition:
                raise ValueError('MECHANISM_DEFINITION_MISSING')
            compiled = definition['compiled']
            declaration = definition.get('declaration')
        else:
            if operation == 'revise':
                if not previous or not previous.get('declaration'):
                    raise ValueError('CAPABILITY_DECLARATION_REQUIRED: use apply to migrate this legacy definition explicitly')
                declaration = revise(previous['declaration'], args['revision'])
            else:
                declaration = args['declaration']
            raw = lower(declaration, app.registry, context['sources'])
            compiled = app.coordinator.preview(raw, previous=(previous or {}).get('compiled'))['compiled']
            definition = {'schema_version': 2, 'project_id': repo.project_id, 'id': name,
                'revision': (previous or {}).get('revision', 0) + 1, 'compiled': compiled,
                'sources': deepcopy(context['sources']), 'prior_ref': head, 'declaration': deepcopy(declaration)}
            reference = 'sha256:' + digest(definition)
        policy = args.get('version_policy', (current or {}).get('version_policy', 'pinned'))
        if policy not in {'pinned', 'follow_compatible'}:
            raise ValueError('MECHANISM_VERSION_POLICY_INVALID')
        constraints = (current or {}).get('required_constraints',
            app.process.runtime_preferences.get('harness_required_constraints', []))
        app.coordinator.check_constraints(compiled, constraints)
        # Compile/use is bound to exact registered implementations, not latest
        # names at invocation time. Missing services fail before publication.
        if not set(compiled['required_capabilities']).issubset(app.services):
            raise ValueError('MECHANISM_ACTIVATION_SERVICE_MISSING')
        for node in compiled['elements']:
            descriptor = app.registry.resolve(node['use'], node['version'])
            if descriptor['digest'] != node['contract_digest']:
                raise ValueError('MECHANISM_ELEMENT_CHANGED')
        if current and policy != current['version_policy']:
            raise ValueError('CAPABILITY_VERSION_POLICY_CHANGE_REQUIRES_EXPLICIT_MIGRATION')
        # A revision to a disabled capability stays disabled. Resumption is an
        # explicit lifecycle request and is never inferred from authoring.
        action = 'preserve_inactive' if current and current['phase'] in {'suspended', 'detached', 'saved'} else 'activate'
        # Reuse changes this task's selected instance, not the project library
        # head. Comparing a historical selection with the newest library entry
        # falsely described an absent task instance as an active newer version.
        before = previous
        if operation == 'use':
            before_ref = (current or {}).get('effective_ref')
            before = repo.definition_ref(before_ref) if before_ref else None
        subject = {'kind': 'harness_capability_deployment', 'operation': operation,
            'instance_id': instance_id, 'action': action, 'version_policy': policy,
            'definition_id': name, 'definition_revision': definition['revision'],
            'selection': 'explicit_revision' if operation == 'use' and 'definition_revision' in args else
                'latest_saved_revision' if operation == 'use' else 'new_revision',
            'definition_head_revision': (previous or {}).get('revision'),
            'previous_definition_revision': (before or {}).get('revision'),
            'declaration': deepcopy(declaration),
            'previous_declaration': deepcopy((before or {}).get('declaration')),
            'previous': semantic_program(before['compiled']) if before else None,
            'target': semantic_program(compiled), 'diff': difference((before or {}).get('compiled'), compiled),
            'component_contracts': component_contracts([compiled], app.registry),
            'expected_definition_head': head, 'instance_basis': instance_basis(current),
            'source_evidence': {ref: {'kind': value.get('kind'), 'digest': digest(value)} for ref, value in context['sources'].items()},
            'contract_revision': context['contract_revision'], 'authority_granted': False}
        plan = {'context': context, 'request': request, 'receipt_key': receipt_key,
            'definition': definition, 'definition_ref': reference, 'head': head, 'publish': operation != 'use',
            'instance_id': instance_id, 'basis': instance_basis(current), 'policy': policy,
            'constraints': deepcopy(constraints), 'action': action, 'subject': subject}
        if remember:
            self.prepared[digest(args)] = plan
        # Retention is local preparation only, never durable authority.
        while len(self.prepared) > 32:
            del self.prepared[next(iter(self.prepared))]
        return deepcopy(subject)

    def commit(self, args):
        from .application import configuration_allowed
        from backend.core.loop.mechanism_lifecycle import fence
        if not configuration_allowed(self.app.process):
            raise ValueError('MECHANISM_CONFIGURATION_CAPABILITY_REQUIRED')
        prepared = self.prepared.get(digest(args))
        if prepared is None:
            raise ValueError('CAPABILITY_PREPARATION_REQUIRED')
        with fence(self.app.process):
            if prepared['context'] != self.context():
                raise ValueError('CAPABILITY_SOURCE_OR_CONTRACT_CHANGED')
            if 'replay' in prepared:
                return self.result(prepared['replay'], replay=True)
            with ExitStack() as leases:
                for node in prepared['definition']['compiled']['elements']:
                    leases.enter_context(self.app.registry.lease(node['use'], node['version'], node['contract_digest']))
                return self._commit(prepared)

    def _commit(self, plan):
        app, repo = self.app, self.app.repo
        definition, reference = plan['definition'], plan['definition_ref']
        # Independent CAS blobs may remain orphaned after a failed commit;
        # only the existing authoritative transaction publishes state roots.
        if plan['publish']:
            bounded_json(definition, maximum=524288)
            stored = repo.storage.put_blob(encoded(definition), media_type='application/json')
            if stored != reference:
                raise RuntimeError('CAPABILITY_DEFINITION_DIGEST_MISMATCH')
        for _ in range(8):
            receipt, _ = repo._read(plan['receipt_key'])
            if receipt:
                return self.result(receipt, replay=True)
            _, head = repo.definition(definition['id'])
            if head != plan['head']:
                raise ValueError('CAPABILITY_DEFINITION_CHANGED')
            current, instance_ref = repo.instance(app.task, plan['instance_id'])
            if instance_basis(current) != plan['basis']:
                raise ValueError('CAPABILITY_INSTANCE_CHANGED')
            if current:
                value = deepcopy(current)
                value['revision'] += 1
                value['desired_ref'] = reference
            else:
                value = repo.instance_record(app.task, plan['instance_id'], definition, reference,
                    policy=plan['policy'], sources=plan['context']['sources'], constraints=plan['constraints'])
                value['revision'] += 1
            if plan['action'] == 'activate':
                value['phase'] = 'pending_activation'
                value['activation'] = {'id': digest({'revision': value['revision'], 'reference': reference, 'action': 'activate'}),
                    'phase': 'pending_activation', 'action': 'activate', 'from_epoch': value['epoch'],
                    'sources': deepcopy(plan['context']['sources'])}
            bounded_json(value, maximum=2 * 1024 * 1024)
            new_instance_ref = repo.storage.put_blob(encoded(value), media_type='application/json')
            receipt = {'project_id': repo.project_id, 'task_id': app.task, 'instance_id': value['id'],
                'request': plan['request'], 'definition_id': definition['id'], 'definition_ref': reference,
                'definition_revision': definition['revision'], 'review_subject': plan['subject'],
                'authority_granted': False}
            receipt_ref = repo.storage.put_blob(encoded(receipt), media_type='application/json')
            updates = [
                {'key': repo.instance_key(app.task, value['id']), 'value_ref': new_instance_ref, 'expected_ref': instance_ref},
                {'key': plan['receipt_key'], 'value_ref': receipt_ref, 'expected_ref': None},
                {'key': 'definition/' + definition['id'] + '/head',
                    'value_ref': reference if plan['publish'] else head, 'expected_ref': head,
                    **({'revision_key': 'definition/' + definition['id'] + '/revision/' + str(definition['revision'])} if plan['publish'] else {})}]
            if not repo.storage.commit_state_batch(NAMESPACE, updates):
                continue
            app.process.runtime_preferences['harness_mechanisms'] = True
            # Consumer activation is recoverable from the same committed
            # desired_ref. Failure cannot turn publication into a retry.
            return self.result(receipt, replay=False)
        raise ValueError('MECHANISM_STATE_CONTENTION')

    def result(self, receipt, *, replay):
        app = self.app
        try:
            value = app.coordinator.settle(app.task, receipt['instance_id'])
            maintenance = app.coordinator.maintain(owner_task_id=app.task)
        except (ValueError, RuntimeError, OSError) as error:
            value, _ = app.repo.instance(app.task, receipt['instance_id'])
            maintenance = {'complete': False, 'error': str(error), 'effect_state': 'inspect_required'}
        try:
            app.observe('mechanism_instance_changed', value)
        except Exception as error:
            maintenance = {**maintenance, 'notification_error': str(error), 'complete': False}
        return {'committed': True, 'replayed': replay, 'definition_id': receipt['definition_id'],
            'definition_revision': receipt['definition_revision'],
            'instance': describe_instance(app.process, value, observations=True),
            'maintenance': maintenance, 'authority_granted': False}
