"""Business file services cannot forge Git or Gitgo metadata."""
from pathlib import Path
import hashlib
import os
import pytest
from backend.core.tools.workspace_tools import write_file,edit_file,delete_file
from backend.core.tools.document_service import document_create
from backend.core.tools.file_mutation import FileMutation,commit_file_mutation
from backend.core.tools.file_mutation_batch import FileMutationBatch,commit_file_batch


@pytest.mark.parametrize('name',['.gitgo/session.json','.git/config','nested/.gitgo/state.json'])
def test_write_refuses_metadata_before_creating_target(tmp_path_factory,name):
    result=write_file({'_workspace':str(tmp_path_factory),'path':name,'content':'forged product state'})
    assert result.get('error')=='PROTECTED_PATH',result
    assert not (tmp_path_factory/name).exists()


@pytest.mark.parametrize('operation',['edit','delete'])
@pytest.mark.parametrize('folder',['.git','.gitgo'])
def test_existing_metadata_keeps_exact_bytes(tmp_path_factory,operation,folder):
    path=tmp_path_factory/folder/'state.json';path.parent.mkdir();path.write_bytes(b'original')
    args={'_workspace':str(tmp_path_factory),'path':folder+'/state.json','old_string':'original','new_string':'forged','expected_sha256':hashlib.sha256(b'original').hexdigest()}
    result=(edit_file if operation=='edit' else delete_file)(args)
    assert result.get('error')=='PROTECTED_PATH',result
    assert path.read_bytes()==b'original'


@pytest.mark.parametrize('suffix',['md','pdf'])
def test_document_refuses_metadata_before_rendering(tmp_path_factory,suffix):
    result=document_create({'_workspace':str(tmp_path_factory),'path':'.gitgo/result.'+suffix,'content':'forged'})
    assert result.get('error')=='PROTECTED_PATH',result
    assert not (tmp_path_factory/'.gitgo'/('result.'+suffix)).exists()


def test_common_commit_refuses_manually_constructed_metadata_candidate(tmp_path_factory):
    path=tmp_path_factory/'.gitgo/state.json'
    candidate=FileMutation('write_file',tmp_path_factory,path,'','','forged')
    result=commit_file_mutation(candidate,{'path':str(path)})
    assert result.get('error')=='PROTECTED_PATH',result
    assert not path.exists()


def test_batch_with_metadata_member_has_no_business_effects(tmp_path_factory):
    good=tmp_path_factory/'good.txt';bad=tmp_path_factory/'.gitgo/state.json'
    batch=FileMutationBatch(tmp_path_factory,tuple(FileMutation('write_file',tmp_path_factory,p,'','','forged') for p in (good,bad)))
    result=commit_file_batch(batch,{})
    assert result.get('error')=='PROTECTED_PATH',result
    assert not good.exists() and not bad.exists()


def test_business_workspace_below_state_named_ancestor_remains_usable(tmp_path_factory):
    ws=tmp_path_factory/'.gitgo/managed-workspace';ws.mkdir(parents=True)
    result=write_file({'_workspace':str(ws),'path':'business.txt','content':'ordinary business'})
    assert not result.get('error'),result
    assert (ws/'business.txt').read_text()=='ordinary business'


@pytest.mark.skipif(os.name!='nt',reason='Windows metadata path aliases')
@pytest.mark.parametrize('name',['.gitgo./probe.json','.gitgo /probe.json','.gitgo:probe'])
def test_windows_metadata_alias_is_rejected_before_any_write(tmp_path_factory,name):
    result=write_file({'_workspace':str(tmp_path_factory),'path':name,'content':'forged'})
    assert result.get('error')=='PROTECTED_PATH',result
    assert not (tmp_path_factory/'.gitgo/probe.json').exists()


def test_legacy_append_cannot_modify_metadata(tmp_path_factory):
    from backend.core.tools.file_compat import file_write
    target=tmp_path_factory/'.gitgo/state.json';target.parent.mkdir();target.write_bytes(b'original')
    result=file_write({'_workspace':str(tmp_path_factory),'path':'.gitgo/state.json','mode':'a','content':'forged','expected_sha256':hashlib.sha256(b'original').hexdigest()})
    assert result.get('error')=='PROTECTED_PATH' and target.read_bytes()==b'original'
