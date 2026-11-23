"""Bounded canonical runtime merge, journaled in the original worktree row.

Recovery observes exact pre/post images; it never appends or resumes a task.
An explicit retry may finish recorded pre-images. Unknown images retain debt.
"""
import base64
import copy
import hashlib
import json
import math
import os
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path

from backend.core import macos_worktree as life, macos_worktree_seal as sealing
from backend.core.storage import metadata_files as metadata
from backend.core.loop.test_manifest import TestManifest, TestRecord, _manifest_lock

NAMES=('test_manifest.json','dependency_observations.jsonl','dependency_feedback.json')
CACHES=('dependency_graph.v2.json',)
LIMIT=1024*1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else ''


def decode(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:life.fail('duplicate runtime JSON key')
            result[key]=value
        return result
    try:
        return json.loads(raw.decode('utf8'),object_pairs_hook=pairs,
                          parse_constant=lambda value:life.fail('nonfinite runtime JSON'))
    except (ValueError,UnicodeError,RecursionError) as exc:
        life.fail('invalid runtime JSON: '+type(exc).__name__)


def records(raw):
    if raw is None:return []
    value=decode(raw)
    if not isinstance(value,dict) or value.get('schema_version',1)!=1 or set(value)-{'schema_version','updated_at','tests'} or not isinstance(value.get('tests'),list):
        life.fail('invalid test manifest schema')
    rows=value['tests'];seen=set()
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('test_id'),str) or not row['test_id'].strip()
                or row['test_id'] in seen or not isinstance(row.get('target'),str)
                or not isinstance(row.get('seeds',[]),list) or any(type(s) is not int for s in row.get('seeds',[]))
                or not isinstance(row.get('results',[]),list)):
            life.fail('invalid or duplicate test record')
        seen.add(row['test_id'])
        for item in row.get('results',[]):
            if (not isinstance(item,dict) or type(item.get('seed')) is not int or type(item.get('exit_code')) is not int
                    or type(item.get('duration_ms')) not in (int,float) or not math.isfinite(item['duration_ms']) or item['duration_ms']<0
                    or not isinstance(item.get('output_tail'),str)):
                life.fail('invalid test seed result')
    try:return [TestRecord.from_dict(row) for row in rows]
    except (ValueError,TypeError,AttributeError,OverflowError):life.fail('invalid test record values')


def business_path(value):
    from backend.core.sandbox_read import relative_parts,ReadBoundaryError
    try:parts=relative_parts(value)
    except ReadBoundaryError:life.fail('runtime dependency escapes the business root')
    if '/'.join(parts)!=value or any(p.casefold() in ('.git','.gitgo') for p in parts):
        life.fail('runtime dependency must name a literal business path')


def feedback(raw):
    if raw is None:return []
    value=decode(raw)
    if not isinstance(value,dict) or value.get('version',1)!=1 or set(value)-{'version','entries'} or not isinstance(value.get('entries'),list):life.fail('invalid dependency feedback schema')
    seen=set()
    for item in value['entries']:
        if (not isinstance(item,dict) or any(not isinstance(item.get(k),str) or not item[k] for k in ('dependent','dependency'))
                or type(item.get('confirmed')) is not bool):life.fail('invalid dependency feedback entry')
        business_path(item['dependent']);business_path(item['dependency'])
        if item['dependent']==item['dependency']:life.fail('self dependency feedback is invalid')
        key=(item['dependent'],item['dependency'])
        if key in seen:life.fail('duplicate dependency feedback entry')
        seen.add(key)
    return value['entries']


def observations(raw):
    if not raw:return
    if not raw.endswith(b'\n'):life.fail('incomplete dependency observation line')
    for line in raw.splitlines():
        if not line:life.fail('empty dependency observation')
        value=decode(line)
        if (not isinstance(value,dict) or not isinstance(value.get('task_id'),str) or not value['task_id']
                or not isinstance(value.get('tool_name'),str) or not isinstance(value.get('files'),list)
                or not all(isinstance(n,str) and n for n in value['files'])):life.fail('invalid dependency observation')
        for name in value['files']:business_path(name)


def merged(name,root,child,lease):
    if name==NAMES[0]:
        # Use the existing canonical seed/validation merge without its permissive
        # load-on-error behavior and without reading or writing a guessed path.
        manifest=TestManifest(lease.root_workspace)
        manifest.records={r.test_id:r for r in records(root)}
        for record in records(child):manifest._merge_record(record)
        result=dict(schema_version=1,updated_at=datetime.now().isoformat(),
                    tests=[manifest.records[k].to_dict() for k in sorted(manifest.records)])
        raw=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False).encode()
    elif name==NAMES[1]:
        observations(root);observations(child)
        offset=lease.runtime_offsets.get(name,0)
        baseline=lease.macos_binding['runtime_input_digests'].get(name,'')
        if type(offset) is not int or offset<0 or offset>len(child):life.fail('observation offset is invalid')
        if (digest(child[:offset]) if name in lease.macos_binding['runtime_input_digests'] else '')!=baseline:
            life.fail('observation prefix changed; refusing offset-based data loss')
        observations(child[:offset]);raw=(root or b'')+child[offset:]
    else:
        entries={(r['dependent'],r['dependency']):r for r in feedback(root)}
        entries.update({(r['dependent'],r['dependency']):r for r in feedback(child)})
        raw=json.dumps(dict(version=1,entries=list(entries.values())),ensure_ascii=False,indent=2,allow_nan=False).encode()
    if len(raw)>LIMIT:life.fail('merged runtime file exceeds byte budget')
    return raw


def save(manager,lease,**fields):
    candidate=copy.deepcopy(lease);candidate.macos_binding.update(fields)
    manager._persist(candidate);lease.__dict__.update(candidate.__dict__)


def admission(manager,lease):
    from backend.core.loop.native_invocation_scope import _SCOPE
    from backend.core.storage.native_invocations import pending_native_invocations
    if _SCOPE.get() is not None or lease.mode!='write' or lease.state!='leased':life.fail('runtime merge requires a Host-owned active write lease')
    if pending_native_invocations(manager.storage,include_active=True):life.fail('native recovery debt blocks runtime merge')
    if any(lease.macos_binding.get(k) for k in ('index_update','result_update')):life.fail('Git transaction must be reconciled before runtime merge')
    if 'runtime_input_digests' not in lease.macos_binding:life.fail('runtime input baseline unavailable')


def derived_cache(raw):
    """Recognize only the existing derived v2 graph; never import its edges.

    The Host graph loader already compares source and auxiliary fingerprints.
    Its own cache is rebuilt from merged canonical observations when required.
    """
    value=decode(raw)
    allowed={'version','analysis_version','generated_at','workspace','file_fingerprints','aux_fingerprints','signal_limits','edges'}
    if (not isinstance(value,dict) or set(value)!=allowed or value['version']!=2 or value['analysis_version']!=1
            or not all(isinstance(value[k],str) for k in ('generated_at','workspace'))
            or not all(isinstance(value[k],dict) for k in ('file_fingerprints','aux_fingerprints','signal_limits'))
            or not isinstance(value['edges'],list)):life.fail('unsupported derived dependency cache retained')
    for name,stamp in value['file_fingerprints'].items():
        business_path(name)
        if not isinstance(stamp,str):life.fail('invalid dependency cache fingerprint')
    if any(n not in ('dependency_feedback.json','dependency_observations.jsonl') or not isinstance(h,str)
           for n,h in value['aux_fingerprints'].items()):life.fail('invalid dependency auxiliary fingerprint')
    for edge in value['edges']:
        if (not isinstance(edge,dict) or set(edge)-{'dependent','dependency','confidence','dismissed','dismissal_reason','evidence'}
                or not isinstance(edge.get('evidence'),list)):life.fail('unknown dependency cache edge fields')
        business_path(edge.get('dependent'));business_path(edge.get('dependency'))
        for evidence in edge['evidence']:
            if not isinstance(evidence,dict) or set(evidence)-{'signal','confidence','detail','count'}:life.fail('unknown dependency cache evidence fields')
    # Decode finite numeric values, including exponent overflow not covered by
    # JSON parse_constant. This cache is acknowledged as derived, not trusted.
    try:json.dumps(value,allow_nan=False)
    except (ValueError,RecursionError):life.fail('invalid dependency cache values')


def snapshot(manager,lease,work):
    from backend.core.sandbox_read import ReadRoot
    from backend.core.macos_worktree_index import selected_file
    _,hashes=sealing.inventory(lease,work);baseline=lease.macos_binding['runtime_input_digests']
    # No unknown evidence is silently excluded from the acknowledgement.
    if any(hashes.get(n)!=h for n,h in baseline.items() if n not in (*NAMES,*CACHES)):life.fail('read-only runtime input changed')
    locks={'metadata-locks/'+hashlib.sha256(n.encode()).hexdigest()+'/storage-maintenance.lock' for n in (*NAMES,*CACHES)}
    if any(n not in baseline and n not in (*NAMES,*CACHES) and not (n in locks and h==digest(b'')) for n,h in hashes.items()):life.fail('unsupported runtime output retained')
    if any(n not in hashes for n in baseline if n not in CACHES):life.fail('runtime input deleted')
    data={}
    with ReadRoot(lease.path) as root:
        if (root.identity.volume,root.identity.file_id)!=tuple(lease.macos_binding['work_identity'][:2]):life.fail('runtime worktree replaced')
        for name in (*NAMES,*CACHES):
            try:raw,_=selected_file(root,work,'.gitgo/'+name)
            except FileNotFoundError:continue
            if digest(raw)!=hashes.get(name):life.fail('runtime output changed during capture')
            if name in CACHES:
                if digest(raw)!=baseline.get(name):derived_cache(raw)
            else:
                if digest(raw)!=baseline.get(name):
                    {NAMES[0]:records,NAMES[1]:observations,NAMES[2]:feedback}[name](raw)
                data[name]=raw
    return hashes,data


def merge(manager,lease,*,cancellation=None,observe=False,_held_context=None):
    admission(manager,lease)
    owner=life.context(manager,lease) if _held_context is None else nullcontext(_held_context)
    with owner as (parent,meta_parent,pins,guard),ExitStack() as stack:
        b=lease.macos_binding
        work=life.open_bound(parent,life.target_name(manager,lease),b['work_identity']);stack.callback(os.close,work)
        meta=life.open_bound(meta_parent,b['metadata'],b['meta_identity']);stack.callback(os.close,meta)
        stack.enter_context(_manifest_lock(manager.repo_root/'.gitgo/test_manifest.json'))
        def checked():
            guard();life.check_files(manager,lease,work,meta);admission(manager,lease)
            if cancellation is not None and cancellation.is_set():life.fail('runtime merge cancelled')
        checked();hashes,child=snapshot(manager,lease,work)
        ack=b.get('runtime_merged')
        if ack:
            if hashes!=ack.get('input_digests'):life.fail('runtime output changed after merge acknowledgement')
            return lease
        pending=b.get('runtime_update')
        if not pending:
            if observe:return lease
            rows=[]
            for name in NAMES:
                if name not in child or hashes[name]==b['runtime_input_digests'].get(name):continue
                root=metadata.read_metadata_bytes(manager.repo_root,name,max_bytes=LIMIT)
                after=merged(name,root,child[name],lease)
                rows.append(dict(name=name,before=digest(root),after=digest(after),data=base64.b64encode(after).decode()))
            # Pin the Host metadata directory, never the worker's choice of target.
            rootfd=life.wire.open_directory(manager.repo_root);stack.callback(os.close,rootfd)
            if life.owned(rootfd)!=pins['project']:life.fail('project replaced before runtime publication')
            try:os.mkdir('.gitgo',0o700,dir_fd=rootfd);os.fsync(rootfd)
            except FileExistsError:pass
            dest=os.open('.gitgo',life.FLAGS,dir_fd=rootfd);stack.callback(os.close,dest)
            dest_id=life.owned(dest)
            checked()
            if snapshot(manager,lease,work)[0]!=hashes:life.fail('runtime output changed before intent')
            pending=dict(version=1,input_digests=hashes,destination=dest_id,files=rows)
            save(manager,lease,runtime_update=pending)
        if pending.get('version')!=1 or pending.get('input_digests')!=hashes:life.fail('runtime merge input differs from recorded intent')
        dest=life.wire.open_directory(manager.repo_root/'.gitgo');stack.callback(os.close,dest)
        if life.owned(dest)!=pending['destination']:life.fail('runtime destination replaced')
        rows=pending['files']
        if len(rows)>3 or len({row['name'] for row in rows})!=len(rows):life.fail('invalid runtime intent members')
        def current(name):
            fd=life.wire.open_directory(manager.repo_root/'.gitgo')
            try:
                if life.owned(fd)!=pending['destination']:life.fail('runtime destination replaced')
            finally:os.close(fd)
            try:return digest(life.read_file(dest,name,LIMIT))
            except FileNotFoundError:return ''
        # Inspect every member before any write; never guess at a foreign image.
        for row in rows:
            if row['name'] not in NAMES:life.fail('invalid runtime intent filename')
            raw=base64.b64decode(row['data'],validate=True)
            if len(raw)>LIMIT or digest(raw)!=row['after']:life.fail('invalid runtime intent bytes')
            if current(row['name']) not in (row['before'],row['after']):life.fail('runtime destination has an unknown image; retain merge debt')
        for row in rows:
            checked()
            if current(row['name'])==row['after']:continue
            if observe:return lease # no append, replacement or replay on recovery
            metadata.write_metadata_bytes(manager.repo_root,row['name'],base64.b64decode(row['data']),
                                          expected_sha256=row['before'],max_bytes=LIMIT)
            checked()
            if current(row['name'])!=row['after']:life.fail('runtime output publication outcome is unknown')
        if snapshot(manager,lease,work)[0]!=hashes:life.fail('runtime output changed before merge acknowledgement')
        checked()
        for row in rows:
            if current(row['name'])!=row['after']:life.fail('runtime output changed before merge acknowledgement')
        os.fsync(dest) # persist directory entries before acknowledging the merge
        save(manager,lease,runtime_update=None,runtime_merged=dict(version=1,input_digests=hashes,
            outputs={row['name']:row['after'] for row in rows},destination=pending['destination'],
            derived_caches={n:hashes.get(n) for n in CACHES if n in hashes or n in b['runtime_input_digests']}))
        return lease
