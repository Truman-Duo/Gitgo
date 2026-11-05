"""Small edits reuse the same compiler, publication CAS and live coordinator."""
from copy import deepcopy
import pytest
from tests.test_mechanism_interactions import runtime
from backend.core.loop.task_maintenance import source_receipts


def install(app,p):
    raw={'schema_version':2,'id':'editable','intent':'Fixture guidance before model context',
        'source_refs':list(source_receipts(p)),'scope':'task',
        'elements':[{'id':'event','use':'runtime.event','parameters':{'boundaries':['before_context']}},
            {'id':'note','use':'context.project','parameters':{'content':'original guidance'}}],
        'bindings':[{'kind':'trigger','from':{'node':'event','port':'go'},'to':{'node':'note','port':'go'}}]}
    preview=app.invoke({'operation':'preview','program':raw})
    app.invoke({'operation':'publish','program':raw,'preview_digest':preview['preview_digest']})
    app.invoke({'operation':'instantiate','definition_id':'editable','version_policy':'follow_compatible','source_refs':raw['source_refs']})
    return app.repo.definition('editable')


def revision(app,p,head,changes=None):
    return {'operation':'preview_revision','definition_id':'editable','expected_head':head,
        'source_refs':list(source_receipts(p)), 'changes':changes or [
            {'op':'replace','path':'/elements/1/parameters/content','value':'revised guidance'}]}


def test_parameter_revision_changes_actual_projection_and_preserves_other_nodes(runtime):
    app,p,*_=runtime
    old,head=install(app,p)
    assert next(d for d in app.invoke({'operation':'status'})['definitions'] if d['id']=='editable')['head_ref']==head
    assert app.invoke({'operation':'status','definition_id':'editable'})['definitions'][0]['head_ref']==head
    assert app.invoke({'operation':'status','definition_id':'not-present'})['definitions']==[]
    app.dispatch('before_context','first',{})
    assert app.context_sections()['harness.projections']['editable']['note']['content']=='original guidance'
    p.session.append_user('Unit fixture: revise only the guidance, retain all other structure.')
    args=revision(app,p,head)
    preview=app.invoke(args)
    assert app.repo.definition('editable')[1]==head
    assert preview['compiled']['elements'][0]==old['compiled']['elements'][0]
    result=app.invoke({**args,'operation':'publish_revision','preview_digest':preview['preview_digest']})
    assert result['definition']['revision']==2 and not result['authority_granted']
    app.maintain();app.dispatch('before_context','second',{})
    assert app.context_sections()['harness.projections']['editable']['note']['content']=='revised guidance'
    assert not p.approval_grants


@pytest.mark.parametrize('path',['/id','/scope','/source_refs','/elements/99/parameters/content','/elements/01/parameters/content'])
def test_invalid_or_identity_patch_never_changes_saved_definition(runtime,path):
    app,p,*_=runtime
    _,head=install(app,p)
    with pytest.raises(ValueError):
        app.invoke(revision(app,p,head,[{'op':'replace','path':path,'value':'invalid'}]))
    assert app.repo.definition('editable')[1]==head


def test_stale_base_and_stale_preview_are_rejected_by_existing_publication_path(runtime):
    app,p,*_=runtime
    _,head=install(app,p)
    args=revision(app,p,head);preview=app.invoke(args)
    app.invoke({**args,'operation':'publish_revision','preview_digest':preview['preview_digest']})
    with pytest.raises(ValueError,match='REVISION_BASE'):
        app.invoke({**args,'operation':'publish_revision','preview_digest':preview['preview_digest']})
    _,head2=app.repo.definition('editable')
    next_args=revision(app,p,head2,[{'op':'replace','path':'/elements/1/parameters/content','value':'another guidance'}])
    next_preview=app.invoke(next_args)
    with pytest.raises(ValueError,match='PREVIEW_STALE'):
        app.invoke({**next_args,'operation':'publish_revision','preview_digest':preview['preview_digest']})
    assert app.repo.definition('editable')[1]==head2


def test_removing_required_binding_still_uses_the_original_compiler(runtime):
    app,p,*_=runtime
    _,head=install(app,p)
    with pytest.raises(ValueError):
        app.invoke(revision(app,p,head,[{'op':'remove','path':'/bindings/0'}]))
    assert app.repo.definition('editable')[1]==head


def test_add_and_remove_edits_remain_pure_until_normal_publication(runtime):
    app,p,*_=runtime
    _,head=install(app,p)
    args=revision(app,p,head,[
        {'op':'add','path':'/bindings/-','value':{'kind':'data','from':{'node':'event','port':'event'},'to':{'node':'note','port':'event'}}},
        {'op':'add','path':'/elements/1/parameters/include_event','value':True}])
    preview=app.invoke(args)
    assert len(preview['compiled']['bindings'])==2 and app.repo.definition('editable')[1]==head
    removal=revision(app,p,head,[{'op':'remove','path':'/elements/1/parameters/include_event'}])
    assert app.invoke(removal)['compiled']['elements'][1]['parameters']['include_event'] is False
    assert app.repo.definition('editable')[1]==head


def test_later_invalid_edit_rolls_back_and_readonly_actor_cannot_publish(runtime):
    app,p,*_=runtime
    _,head=install(app,p)
    args=revision(app,p,head,[{'op':'replace','path':'/elements/1/parameters/content','value':'valid change'},
        {'op':'replace','path':'/elements/99/parameters/content','value':'invalid change'}])
    with pytest.raises(ValueError):app.invoke(args)
    assert app.repo.definition('editable')[1]==head
    args=revision(app,p,head);preview=app.invoke(args)
    p.capability_profile_id='text.only'
    assert app.invoke(args)['preview_digest']==preview['preview_digest']
    with pytest.raises(ValueError,match='CAPABILITY_REQUIRED'):
        app.invoke({**args,'operation':'publish_revision','preview_digest':preview['preview_digest']})
    assert app.repo.definition('editable')[1]==head
