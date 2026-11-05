"""Explicit owner-declared requirements, separate from local task instances."""
from copy import deepcopy
import json

from .contracts import digest, encoded
from .repository import NAMESPACE


class Requirements:
    def __init__(self, application):
        self.app = application

    @staticmethod
    def key(task):
        return 'requirements/' + digest(task)

    def read(self, task):
        ref = self.app.repo.storage.get_state_ref(NAMESPACE, self.key(task))
        return (json.loads(self.app.repo.storage.read_blob(ref)), ref) if ref else (None, None)

    def declare(self, args):
        app = self.app
        sources = app.sources(args.get('source_refs'))
        old, head = self.read(app.task)
        if args.get('expected_required_revision') != (old or {}).get('revision', 0):
            raise ValueError('MECHANISM_REQUIREMENTS_REVISION_CONFLICT')
        items = args.get('requirements')
        if not isinstance(items, list) or len(items) > 16:
            raise ValueError('MECHANISM_REQUIREMENTS_INVALID')
        bindings = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {'definition_id', 'definition_revision', 'version_policy'}:
                raise ValueError('MECHANISM_REQUIREMENTS_INVALID')
            definition, reference = app.repo.definition(item['definition_id'], item.get('definition_revision'))
            if definition is None or item.get('version_policy') not in {'pinned', 'follow_compatible'}:
                raise ValueError('MECHANISM_REQUIREMENT_DEFINITION_MISSING')
            bindings.append({'definition_id': definition['id'], 'reference': reference, 'version_policy': item['version_policy']})
        if len({item['definition_id'] for item in bindings}) != len(bindings):
            raise ValueError('MECHANISM_REQUIREMENTS_DUPLICATE')
        value = {'project_id': app.repo.project_id, 'task_id': app.task,
            'owner_session_id': app.process.session.session_id, 'owner_process_id': app.process.process_id,
            'revision': (old or {}).get('revision', 0) + 1, 'bindings': bindings, 'sources': sources}
        reference = app.repo.storage.put_blob(encoded(value), media_type='application/json')
        if not app.repo.storage.commit_state_revision(NAMESPACE, self.key(app.task), reference, expected_ref=head,
                revision_key=self.key(app.task) + '/revision/' + str(value['revision'])):
            raise ValueError('MECHANISM_REQUIREMENTS_REVISION_CONFLICT')
        # Publish the one owner head first. Every child's next admission reads
        # this same head, even if projection staging is interrupted. Parked
        # children must also show pending inheritance without executing their
        # callbacks or borrowing the owner's runtime capabilities.
        staged = []
        manager = getattr(app.process, '_manager', None)
        for child in manager.children_of(app.process.process_id) if manager else []:
            if child.status.value in {'completed', 'failed', 'cancelled', 'timed_out', 'killed'}:
                continue
            try:
                from .application import Application
                child_app = getattr(child, '_harness_application', None) or Application(child, registry=app.registry)
                names = child_app.requirements.synchronize(stage_only=True)
                staged.append({'process_id': child.process_id, 'instances': names, 'phase': 'pending_boundary'})
                manager.notify_state_changed(child.process_id)
            except (ValueError, RuntimeError, OSError) as error:
                staged.append({'process_id': child.process_id, 'phase': 'inspect_required', 'error': str(error)[:400]})
        return {'requirements': value, 'children': staged, 'authority_granted': False}

    def synchronize(self, *, stage_only=False):
        """Actual ownership binds inheritance; parenthood alone adds no policy.

        Local definitions and tool scopes are never copied from the parent.
        Shared requirements are immutable refs explicitly declared by its owner.
        """
        app = self.app
        manager = getattr(app.process, '_manager', None)
        parent = manager.get(app.process.parent_id) if manager and app.process.parent_id else None
        if parent is None:
            return []
        parent_task = parent.active_task_id or parent.process_id
        requirement, _ = self.read(parent_task)
        if not requirement or requirement['owner_session_id'] != parent.session.session_id:
            return []
        required_ids = set()
        for binding in requirement['bindings']:
            name = 'required-' + digest([parent_task, binding['definition_id']])[:24]
            required_ids.add(name)
            definition = app.repo.definition_ref(binding['reference'])
            reference = binding['reference']
            value, _ = app.repo.instance(app.task, name)
            if binding['version_policy'] == 'follow_compatible':
                from .coordinator import compatible
                latest, latest_ref = app.repo.definition(binding['definition_id'])
                if compatible((value or definition)['compiled'], latest['compiled']):
                    definition, reference = latest, latest_ref
            owner = {'task_id': parent_task, 'session_id': parent.session.session_id,
                     'requirements_revision': requirement['revision'], 'definition_id': binding['definition_id']}
            if value is None:
                value = app.repo.create_instance(app.task, name, definition, reference, policy=binding['version_policy'],
                    sources=requirement['sources'])
            if value.get('managed_by') and value['managed_by']['task_id'] != parent_task:
                raise ValueError('MECHANISM_REQUIREMENT_OWNER_CHANGED')
            def bind(current):
                current['managed_by'] = deepcopy(owner)
                current['version_policy'] = binding['version_policy']
            app.repo.mutate(app.task, name, bind)
            if value['desired_ref'] != reference or value['phase'] == 'saved':
                app.coordinator.request(app.task, name, reference, expected_revision=value['revision'], sources=requirement['sources'])
                if not stage_only:
                    app.coordinator.settle(app.task, name)
            elif not stage_only and value['phase'] in {'pending_activation', 'activating'}:
                app.coordinator.settle(app.task, name)
            app.process.runtime_preferences['harness_mechanisms'] = True
        for value in app.repo.list_instances(app.task, limit=100):
            if (value.get('managed_by') or {}).get('task_id') == parent_task and value['id'] not in required_ids and value['phase'] != 'detached':
                app.coordinator.request(app.task, value['id'], value['desired_ref'], expected_revision=value['revision'],
                    sources=requirement['sources'], action='detach')
                if not stage_only:
                    app.coordinator.settle(app.task, value['id'])
        return sorted(required_ids)
