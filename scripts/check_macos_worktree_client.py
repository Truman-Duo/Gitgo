"""Ordinary Host lifecycle fault probe, invoked only by opt-in acceptance.

The fixed modes simulate abrupt Host death after durable transitions. This is
not a tool entry or production privilege. Broker service stays independent.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def ephemeral_privacy_fixture():
    """Generate a fresh test-only key for rejection probes, never a credential."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def seed_runtime_outputs(path,tag):
    import json
    root=Path(path)/'.gitgo';root.mkdir(exist_ok=True)
    (root/'test_manifest.json').write_text(json.dumps(dict(schema_version=1,tests=[dict(test_id=tag,target='test_fixture.py',
        seeds=[1],results=[dict(seed=1,exit_code=0,duration_ms=1,output_tail='ok')])])))
    name=root/'dependency_observations.jsonl'
    prior=name.read_bytes() if name.exists() else b''
    name.write_bytes(prior+json.dumps(dict(task_id=tag,tool_name='read_file',files=['data.bin'])).encode()+b'\n')
    (root/'dependency_feedback.json').write_text(json.dumps(dict(version=1,entries=[dict(dependent=tag+'.py',dependency='fixture.py',confirmed=True)])))


def resume_rejections(manager,source):
    """Probe admission before checkpoint publication; retain no new authority."""
    import copy
    from backend.core.loop.worktree import WorktreeError
    from backend.core.storage.native_invocations import NAMESPACE
    rows=manager.storage.list_worktree_states()
    history=manager.storage.list_state_refs(NAMESPACE,prefix='history/',limit=100)
    directories=set(manager.worktrees_root.iterdir())
    results=[]
    for name in ('forged','review','same_process','private_key'):
        prior=copy.deepcopy(source);options={};target='refused-resume-'+name
        if name=='forged':prior.promoted=True
        if name=='review':options['mode']='review'
        if name=='same_process':target=source.process_id
        secret=Path(source.path)/'resume-secret-fixture'
        if name=='private_key':
            with secret.open('xb') as stream:stream.write(ephemeral_privacy_fixture())
        try:
            try:manager.create(process_id=target,task_id='resume-rejection',snapshot_commit=source.snapshot_commit,resume_from=prior,**options)
            except WorktreeError as exc:
                expected={'forged':'stale continuation','review':'review cannot resume','same_process':'distinct write lease','private_key':'privacy'}[name]
                assert expected in str(exc),(name,str(exc))
            else:raise AssertionError('Unsafe continuation admitted: '+name)
        finally:
            if name=='private_key':secret.unlink()
        assert manager.storage.list_worktree_states()==rows
        assert manager.storage.list_state_refs(NAMESPACE,prefix='history/',limit=100)==history
        assert set(manager.worktrees_root.iterdir())==directories
        results.append(dict(case=name,rejected=True,no_native_execution=True,no_worktree_side_effect=True))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--commit',required=True)
    parser.add_argument('--mode',required=True,choices=('reserved','building','prepared','published','retiring','unrecorded','index-producing','index-prepared','index-replaced','index-committed','result-producing','result-verified','result-packed-locked','result-prepared','result-renamed','result-committed','result-unacknowledged','seal-prepared','seal-committed','runtime-intent','runtime-first','runtime-last','runtime-ack','finalize-intent','finalize-first','finalize-last','finalize-merged','finalize-sealed','finalize-many','finalize-empty','finalize-upstream','resume-prepared','resume-sealed'))
    parser.add_argument('--upstream',default='')
    parser.add_argument('--uid',required=True,type=int)
    args=parser.parse_args()
    if os.getuid()!=args.uid or os.geteuid()!=args.uid or args.uid==0:raise RuntimeError('ordinary Host only')
    from backend.core.macos_broker_client import broker_session
    from backend.core.loop.worktree import AgentWorktreeManager
    from backend.core.storage import get_storage
    from backend.core import macos_worktree
    with broker_session(args.config) as binding:
        manager=AgentWorktreeManager(binding['project'],get_storage(binding['project']))
        if args.mode.startswith('resume-'):
            source=manager.create(process_id='source-'+args.mode,task_id='resume-acceptance',snapshot_commit=args.commit)
            source_path=Path(source.path)
            (source_path/'data.bin').write_bytes(b'retained partial\0\xff')
            (source_path/'new.bin').write_bytes(b'new partial\0\xfe');(source_path/'new.bin').chmod(0o755)
            seed_runtime_outputs(source.path,'old-execution-only')
            import json
            print(json.dumps(dict(resume_rejections=resume_rejections(manager,source))),flush=True)
            persist=manager._persist
            def crash_resume(value):
                persist(value)
                if value.process_id=='fault-'+args.mode:
                    if args.mode=='resume-prepared' and value.macos_binding.get('phase')=='prepared':os._exit(86)
                    if args.mode=='resume-sealed' and value.state=='sealed':os._exit(86)
            manager._persist=crash_resume
            lease=manager.create(process_id='fault-'+args.mode,task_id='resume-acceptance',snapshot_commit=args.commit,resume_from=source)
            assert (Path(lease.path)/'data.bin').read_bytes()==b'retained partial\0\xff'
            assert (Path(lease.path)/'new.bin').stat().st_mode&0o777==0o755
            assert lease.macos_binding['commit']==args.commit and lease.continuation_commit==source.continuation_commit
            from backend.core.macos_worktree_runtime import NAMES
            for name in NAMES:
                new=Path(lease.path)/'.gitgo'/name;original=Path(binding['project'])/'.gitgo'/name
                assert (new.read_bytes() if new.exists() else None)==(original.read_bytes() if original.exists() else None)
            manager.seal(lease)
            raise RuntimeError('resume fault injection did not trigger')
        if args.mode.startswith('finalize-'):
            from backend.core.storage import metadata_files
            from backend.core.dependency_graph import DependencyGraph,_save_graph
            lease=manager.create(process_id='fault-'+args.mode,task_id='finalize-acceptance',snapshot_commit=args.commit,upstream_commits=[args.upstream] if args.mode=='finalize-upstream' else [])
            (Path(lease.path)/'data.bin').write_bytes(b'published result fixture')
            if args.mode=='finalize-many':
                (Path(lease.path)/'.gitignore').write_text('# runtime metadata\n/.gitgo/\n')
                for i in range(17):(Path(lease.path)/f'new{i:02}').write_text('fixture')
            if args.mode=='finalize-empty':
                for item in Path(lease.path).rglob('*'):
                    if item.is_file() and '.gitgo' not in item.relative_to(lease.path).parts and item.name!='.git':item.unlink()
            # Public completion owns staging and publication for this batch.
            seed_runtime_outputs(lease.path,args.mode);_save_graph(DependencyGraph(Path(lease.path)))
            persist=manager._persist;write=metadata_files.write_metadata_bytes;count=[0]
            def crash_save(value):
                persist(value)
                if args.mode=='finalize-intent' and value.macos_binding.get('runtime_update'):os._exit(86)
                if args.mode=='finalize-merged' and value.macos_binding.get('runtime_merged'):os._exit(86)
                if args.mode in ('finalize-sealed','finalize-many','finalize-empty','finalize-upstream') and value.state=='sealed':os._exit(86)
            def crash_write(*a,**kw):
                result=write(*a,**kw);count[0]+=1
                if args.mode=='finalize-first' and count[0]==1 or args.mode=='finalize-last' and count[0]==3:os._exit(86)
                return result
            manager._persist=crash_save;metadata_files.write_metadata_bytes=crash_write
            manager.seal(lease)
            raise RuntimeError('finalize fault injection did not trigger')
        if args.mode.startswith('runtime-'):
            from backend.core.storage import metadata_files
            lease=manager.create(process_id='fault-'+args.mode,task_id='runtime-acceptance',snapshot_commit=args.commit)
            seed_runtime_outputs(lease.path,args.mode)
            persist=manager._persist;write=metadata_files.write_metadata_bytes;count=[0]
            def crash_save(value):
                persist(value)
                if args.mode=='runtime-intent' and value.macos_binding.get('runtime_update'):os._exit(86)
                if args.mode=='runtime-ack' and value.macos_binding.get('runtime_merged'):os._exit(86)
            def crash_write(*a,**kw):
                result=write(*a,**kw);count[0]+=1
                if args.mode=='runtime-first' and count[0]==1 or args.mode=='runtime-last' and count[0]==3:os._exit(86)
                return result
            manager._persist=crash_save;metadata_files.write_metadata_bytes=crash_write
            manager.merge_runtime_outputs(lease)
            raise RuntimeError('runtime fault injection did not trigger')
        if args.mode.startswith('seal-'):
            lease=manager.create(process_id='fault-'+args.mode,task_id='seal-acceptance',snapshot_commit=args.commit)
            (Path(lease.path)/'data.bin').write_bytes(b'published result fixture')
            manager.stage_index(lease,['data.bin']);manager.publish_index(lease)
            original=manager._persist
            def crash_seal(value):
                if value.state=='sealed' and args.mode=='seal-prepared':os._exit(86)
                original(value)
                if value.state=='sealed' and args.mode=='seal-committed':os._exit(86)
            manager._persist=crash_seal
            manager.seal(lease)
            raise RuntimeError('seal fault injection did not trigger')
        if args.mode.startswith('result-'):
            lease=manager.create(process_id='fault-'+args.mode,task_id='result-acceptance',snapshot_commit=args.commit)
            (Path(lease.path)/'data.bin').write_bytes(b'published result fixture')
            manager.stage_index(lease,['data.bin'])
            original=manager._persist
            def crash_result(value):
                if args.mode=='result-unacknowledged' and value.macos_binding.get('result'):os._exit(86)
                original(value)
                intent=value.macos_binding.get('result_update') or {}
                if args.mode=='result-'+intent.get('phase',''):os._exit(86)
                if args.mode=='result-committed' and value.macos_binding.get('result'):os._exit(86)
            manager._persist=crash_result
            if args.mode=='result-renamed':
                rename=macos_worktree.rename_exclusive
                def crash_rename(fd,old,new):
                    rename(fd,old,new)
                    if old.endswith('.lock'):os._exit(86)
                macos_worktree.rename_exclusive=crash_rename
            manager.publish_index(lease)
            raise RuntimeError('result fault injection did not trigger')
        if args.mode.startswith('index-'):
            lease=manager.create(process_id='fault-'+args.mode,task_id='index-acceptance',snapshot_commit=args.commit)
            (Path(lease.path)/'index-input.bin').write_bytes(b'index fixture\0\xff')
            original=manager._persist
            def crash_index(value):
                original(value)
                pending=value.macos_binding.get('index_update') or {}
                if args.mode=='index-'+pending.get('phase',''):os._exit(86)
                if args.mode=='index-committed' and not pending and value.macos_binding.get('staged_tree'):os._exit(86)
            manager._persist=crash_index
            if args.mode=='index-replaced':
                replace=os.replace
                def crash_replace(src,dst,**kwargs):
                    replace(src,dst,**kwargs)
                    if src=='index.lock':os._exit(86)
                os.replace=crash_replace
            manager.stage_index(lease,['index-input.bin'])
            raise RuntimeError('index fault injection did not trigger')
        original=manager._persist
        def crash(lease):
            phase=lease.macos_binding.get('phase')
            if args.mode=='unrecorded' and phase=='building':os._exit(86)
            original(lease)
            if args.mode==phase:os._exit(86)
        manager._persist=crash
        lease=manager.create(process_id='fault-'+args.mode,task_id='worktree-acceptance',snapshot_commit=args.commit)
        if args.mode=='retiring':
            macos_worktree.erase=lambda *a,**kw:os._exit(86)
            manager.dispose(lease)
        raise RuntimeError('fault injection did not trigger')


if __name__=='__main__':main()
