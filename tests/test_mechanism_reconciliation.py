"""Trusted read-only adapter recovery over real CAS, never provider acceptance."""
from contextlib import contextmanager
import pytest
from backend.core.storage import StorageRuntime
from backend.core.mechanism_runtime.contracts import Element, Port
from backend.core.mechanism_runtime.registry import Registry
from backend.core.mechanism_runtime.repository import Repository
from backend.core.mechanism_runtime.coordinator import Coordinator
from backend.core.mechanism_runtime.runner import Runner, NodeResult, RecoveryResult


@contextmanager
def fixture(root, *, proof=True, invalid=False, read_service=True):
    with StorageRuntime(root, state_home=root / 'state') as storage:
        registry = Registry()
        registry.register_type('record', {'type': 'object'})
        registry.register_type('trigger', {'const': True})
        schema = {'type': 'object', 'additionalProperties': False}
        effects, later = {}, []
        interrupt = [True]
        def event(ctx, params, inputs):
            return NodeResult({'go': True})
        registry.register(Element('event', 1, schema, (Port('event','record'),), (Port('go','trigger'),), ('observed',), event, entry=True))
        def effect(ctx, params, inputs):
            ctx.call('effect.write', {})
            if interrupt[0]:
                raise OSError('synthetic reply lost after effect')
            return NodeResult({'done': True}, state={'count': ctx.state.get('count',0)+1})
        def reconcile(ctx, params, journal):
            assert set(ctx.services) == {'effect.read'}
            seen = ctx.call('effect.read', {})
            if invalid:
                return {'pretend_result': True}
            if not seen.get('found'):
                return None
            return RecoveryResult(NodeResult({'done': True}, state={'count': ctx.state.get('count',0)+1}), ('fixture-canonical:' + ctx.event_id,))
        registry.register(Element('recoverable', 1, schema, (Port('go','trigger'),), (Port('done','trigger'),), ('observed',), effect,
            effect='projection', capabilities=('effect.write',), reconciler=reconcile, recovery_capabilities=('effect.read',)))
        def tail(ctx, params, inputs):
            later.append(ctx.event_id)
            return NodeResult()
        registry.register(Element('tail', 1, schema, (Port('go','trigger'),), (), ('observed',), tail))
        repo = Repository(storage)
        coordinator = Coordinator(registry, repo, capabilities=('effect.write','effect.read'))
        raw = {'schema_version':2, 'id':'recoverable-graph', 'intent':'unit fixture', 'source_refs':['user:fixture'],
            'elements':[{'id':'a','use':'event'},{'id':'b','use':'recoverable'},{'id':'c','use':'tail'}],
            'bindings':[{'kind':'trigger','from':{'node':'a','port':'go'},'to':{'node':'b','port':'go'}},
                {'kind':'trigger','from':{'node':'b','port':'done'},'to':{'node':'c','port':'go'}}]}
        compiled = coordinator.preview(raw)['compiled']
        definition, ref = repo.publish(compiled, {'user:fixture':{}}, expected_head=None)
        repo.create_instance('task','fixture',definition,ref,policy='pinned',sources=definition['sources'])
        coordinator.request('task','fixture',ref,expected_revision=1,sources=definition['sources'])
        coordinator.settle('task','fixture')
        def write(ctx, payload):
            effects[ctx.event_id] = effects.get(ctx.event_id,0) + 1
            return {}
        def read(ctx, payload):
            return {'found': bool(proof and effects.get(ctx.event_id) == 1)}
        runner = Runner(registry,repo,{'effect.write':write,'effect.read':read},
            recovery_services={'effect.read':read} if read_service else {})
        yield runner, coordinator, repo, effects, later, interrupt


def test_recovery_observes_one_effect_without_replay_or_downstream_resume(tmp_path_factory):
    with fixture(tmp_path_factory) as (runner, coordinator, repo, effects, later, interrupt):
        with pytest.raises(OSError):
            runner.dispatch('task','fixture','one','observed',{})
        assert effects == {'one':1} and later == []
        coordinator.reconcile('task','fixture')
        assert runner.reconcile('task','fixture')['reconciled']
        state = repo.instance('task','fixture')[0]
        assert state['phase'] == 'ready' and not state['active_run'] and state['states']['b']['count'] == 1
        assert runner.dispatch('task','fixture','one','observed',{})['phase'] == 'reconciled'
        assert effects == {'one':1} and later == []
        interrupt[0] = False
        runner.dispatch('task','fixture','two','observed',{})
        assert effects == {'one':1,'two':1} and later == ['two']
        assert repo.instance('task','fixture')[0]['states']['b']['count'] == 2


@pytest.mark.parametrize('options', [{'proof':False}, {'invalid':True}, {'read_service':False}])
def test_missing_uncertain_or_invalid_recovery_stays_unknown(tmp_path_factory, options):
    with fixture(tmp_path_factory, **options) as (runner, coordinator, repo, effects, later, interrupt):
        with pytest.raises(OSError):
            runner.dispatch('task','fixture','one','observed',{})
        coordinator.reconcile('task','fixture')
        assert not runner.reconcile('task','fixture')['reconciled']
        state = repo.instance('task','fixture')[0]
        assert state['phase'] == 'blocked' and state['error']['effect_state'] == 'unknown'
        assert effects == {'one':1} and not later


def test_component_recovery_cannot_clear_original_toolpipeline_pin(tmp_path_factory):
    with fixture(tmp_path_factory) as (runner, coordinator, repo, effects, later, interrupt):
        with pytest.raises(OSError):
            runner.dispatch('task','fixture','one','observed',{})
        repo.mutate('task','fixture',lambda v: v.setdefault('active_calls',{}).update({'tool-unit':{'phase':'unknown'}}))
        coordinator.reconcile('task','fixture')
        assert not runner.reconcile('task','fixture')['reconciled']
        assert repo.instance('task','fixture')[0]['active_calls']['tool-unit']['phase'] == 'unknown'
        assert effects == {'one':1} and not later


def test_waiting_checkpoint_restores_unknown_journal_before_process_publication(tmp_path_factory):
    from backend.core.loop.manager import AgentProcessManager, SessionStore
    from backend.core.loop.models import RingLevel, ProcessStatus
    from backend.core.loop.tools import ToolRegistry
    from backend.core.loop.recovery import restore_incomplete_processes
    with fixture(tmp_path_factory) as (runner, coordinator, repo, effects, later, interrupt):
        with pytest.raises(OSError): runner.dispatch('task','fixture','one','observed',{})
        # Fault fixture: os._exit cannot run Runner's exception checkpoint.
        # Recreate its durable pre-exception tombstone, never business facts.
        def abrupt(current):
            current['phase']='ready'
            current.pop('error',None)
            current['runs'][current['active_run']]['phase']='running'
        repo.mutate('task','fixture',abrupt)
        manager=AgentProcessManager()
        manager.storage=repo.storage
        store=SessionStore(tmp_path_factory,storage=repo.storage,migrate_legacy=False)
        manager.session_store=store
        process=manager.fork(None,'worker',ToolRegistry([]),8,RingLevel.RING_3,
            workspace_path=str(tmp_path_factory),task_id='task',actor_kind='worker',capability_profile_id='governance.observe',task_kind='answer')
        from backend.core.loop.decision_support import create_user_decision
        create_user_decision(process,{'question':'Which evidence should be inspected?',
            'kind':'clarification','why_user_must_decide':'Unit fixture ambiguity','options':[],'allow_free_form':True})
        process.status=ProcessStatus.AWAITING_USER
        store.save_process_checkpoint(process)
        restored=AgentProcessManager()
        restored.storage=repo.storage
        restored.session_store=store
        restore_incomplete_processes(store,restored,tmp_path_factory,include_process_ids=[process.process_id])
        value=repo.instance('task','fixture')[0]
        # Native workspace evidence may independently require review in this
        # unit fixture. Component classification must never clear that gate.
        assert restored.get(process.process_id) is not None
        assert restored.get(process.process_id).status in {ProcessStatus.AWAITING_USER,ProcessStatus.RECOVERY_REVIEW_REQUIRED}
        assert value['phase']=='blocked' and value['error']['effect_state']=='unknown'
        assert value['runs'][value['active_run']]['phase']=='reconciliation_required'
        assert effects=={'one':1} and not later
        assert runner.reconcile('task','fixture')['reconciled']
        assert repo.instance('task','fixture')[0]['phase']=='ready'
