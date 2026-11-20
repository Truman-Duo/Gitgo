"""Operator-provisioned user broker with bounded project authority.

A grant fixes OS user, directory object, runtime and expiry. Root never opens
client-selected project content or changes real-project ownership/ACLs. All
input is bounded snapshot data; only the fixed isolated tool runner or the
fixed object-only Git queries execute, under the same dedicated-UID owner.
"""
from __future__ import annotations
from contextlib import nullcontext
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from backend.core import macos_broker_protocol as wire
from backend.core import macos_identity as identity
from backend.core.macos_broker_capabilities import (
    BrokerAuthorityError, BrokerGrantInvalidError, BrokerMaintenanceError, denial,
)
from backend.core.sandbox_macos import execution_trial, private_directory
from backend.core.sandbox import SandboxPolicy, sandbox_popen
from backend.core.child_process import tool_runner_command, owned_child_cwd
from backend.core.sandbox_io import BoundedCommunication

# Captures AFTER confirmed UID retirement, in a child dropped to that same UID.
# No privileged file walk through paths authored by a task.
EXPORT = r'''
import base64,json,os,stat,sys
files=[];dirs=[];used=0;nodes=0
flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC
def walk(fd,prefix=''):
 global used,nodes
 for entry in os.scandir(fd):
  name=entry.name
  nodes+=1
  if nodes>640: raise ValueError('Too many output entries')
  if name.casefold() in ('.git','.gitgo'):
   if not prefix and name=='.gitgo':
    child=os.open(name,flags|os.O_DIRECTORY,dir_fd=fd)
    try:
     if any(n.name!='sandbox' for n in os.scandir(child)): raise ValueError('Protected output')
    finally: os.close(child)
    continue
   raise ValueError('Protected output')
  path=prefix+name
  child=os.open(name,flags,dir_fd=fd)
  try:
   info=os.fstat(child)
   if stat.S_ISDIR(info.st_mode):
    dirs.append(path);walk(child,path+'/')
   elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
    if len(files)>=128 or info.st_size>4194304: raise ValueError('Output exceeds budget')
    chunks=[]
    while True:
     chunk=os.read(child,65536)
     if not chunk: break
     used+=len(chunk)
     if used>4194304: raise ValueError('Output exceeds budget')
     chunks.append(chunk)
    files.append([path,base64.b64encode(b''.join(chunks)).decode(),stat.S_IMODE(info.st_mode)])
   else: raise ValueError('Unsupported output object')
  finally: os.close(child)
fd=os.open('.',flags|os.O_DIRECTORY)
try: walk(fd)
finally: os.close(fd)
print(json.dumps({'files':files,'directories':dirs}))
'''


def grant(root, *, live=True):
    private_directory(root/'private')
    value=json.loads(identity.bounded_read(root/'private/grant.json',0,True))
    from backend.core.macos_helper_lifecycle import managed, admission_allowed
    persistent=managed(root)
    if ((live or not persistent) and value['boot']!=identity.boot_id() or
            live and (value.get('revoked') or raw_time()>=value['expires'])):
        raise BrokerGrantInvalidError('Project grant expired or revoked')
    if live and persistent:
        from backend.core.macos_project_authority import pending
        if pending(root): raise BrokerMaintenanceError('Authorization change is incomplete; new execution refused')
        admission_allowed(root)
    return value


def raw_time(): return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)/1e9


def provision(project, owner_uid, *, lifetime=180, root=None, resources=None):
    """Explicit operator API for a bounded OS-user/project capability.

    Never callable over the request socket. Does not grant access to the
    formal directory or edit its ACL. launchd registration is managed separately.
    """
    if os.getuid()!=0 or os.geteuid()!=0:
        raise PermissionError('Grant provisioning requires administrator authority')
    from backend.core.macos_resource_guard import validate
    resources=validate(dict(memory_bytes=512*1024**2,process_limit=32,cpu_seconds=120) if resources is None else resources)
    if type(owner_uid) is not int or not 501<=owner_uid<identity.UID_MIN:
        raise ValueError('An ordinary login user is required')
    if type(lifetime) is not int or not 10<=lifetime<=(86400 if root is not None else 600):
        raise ValueError('Trial grant lifetime must be bounded')
    import pwd
    pwd.getpwuid(owner_uid)
    project=Path(project).absolute()
    fd=wire.open_directory(project)
    try:
        key=wire.directory_identity(fd)
        from backend.core.macos_project_identity import persistent_identity
        project_object=persistent_identity(fd) if root is not None else None
    finally: os.close(fd)
    if key[2]!=owner_uid: raise PermissionError('Project must belong to the authorized user')
    if root is None:
        root=Path(tempfile.mkdtemp(prefix='gitgo-macos-broker-',dir='/private/tmp'))
    else:
        from backend.core.macos_helper_lifecycle import managed
        from backend.core.macos_helper_install import trusted_directory
        root=Path(root).absolute()
        if not managed(root): raise ValueError('Invalid persistent service directory')
        trusted_directory(root.parent);root.mkdir(mode=0o711)
    root.chmod(0o711);(root/'private').mkdir(mode=0o700)
    token=secrets.token_hex(32);grant_id=uuid.uuid4().hex
    value=dict(owner_uid=owner_uid,project=str(project),identity=key,grant_id=grant_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),boot=identity.boot_id(),
        expires=raw_time()+lifetime,revoked=False,resources=resources)
    if project_object is not None:value['project_object']=project_object
    identity.atomic_json(root/'private/grant.json',value)
    config=root/'client.json'
    client=dict(socket=str(root/'broker.sock'),token=token,project=str(project),identity=key,grant_id=grant_id)
    if project_object is not None:client['project_object']=project_object
    identity.atomic_json(config,client)
    os.chown(config,owner_uid,-1)
    return root


def authorize(connection, project_fd, request, value):
    if wire.peer_uid(connection)!=value['owner_uid']:
        raise BrokerAuthorityError('Peer user does not own this grant')
    fields={'op','token','grant_id','invocation_id','execution_basis'}
    if request.get('op')=='run': fields|={'timeout','request','image'}
    if request.get('op') in ('git_read','git_objects'): fields|={'timeout','query','objects','view_sha256'}
    if request.get('op')=='git_objects':fields|={'input','author'}
    if request.get('op')=='git_checkout':fields|={'timeout','commit','objects','view_sha256'}
    if request.get('op')=='status' and 'original_grant_id' in request:
        fields.add('original_grant_id')
        if type(request['original_grant_id']) is not str or not re.fullmatch('[0-9a-f]{32}',request['original_grant_id']):
            raise ValueError('Invalid original grant identity')
    if request.get('op') not in ('run','git_read','git_objects','git_checkout','status','capabilities') or set(request)!=fields:
        raise ValueError('Unsupported broker operation or fields')
    if type(request['token']) is not str or not hmac.compare_digest(hashlib.sha256(request['token'].encode()).hexdigest(),value['token_hash']):
        raise BrokerAuthorityError('Invalid project capability')
    if request['grant_id']!=value['grant_id'] or wire.directory_identity(project_fd)!=value['identity']:
        raise BrokerAuthorityError('Wrong authorized project object')
    from backend.core.macos_project_identity import check_directory
    check_directory(project_fd,value)
    # Metadata-only reopen of the operator-selected path, never a request path.
    current=wire.open_directory(value['project'])
    try:
        if wire.directory_identity(current)!=value['identity']: raise BrokerAuthorityError('Project replaced')
        check_directory(current,value)
    finally: os.close(current)
    identifier=request['invocation_id']
    if type(identifier) is not str or not re.fullmatch('[0-9a-f]{32}',identifier):
        raise ValueError('Invalid invocation identity')
    if type(request['execution_basis']) is not str or not re.fullmatch('[0-9a-f]{64}',request['execution_basis']):
        raise ValueError('Invalid execution basis')
    if request['op'] in ('status','capabilities'): return identifier
    timeout=request['timeout']
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=60:
        raise ValueError('Invalid trial execution deadline')
    if request['op']=='git_checkout':
        from backend.core.macos_git_checkout import validate_commit
        from backend.core.macos_git_read import view_from_wire
        validate_commit(request['commit'])
        view_from_wire(request['objects'],request['view_sha256'])
        return identifier
    if request['op']=='git_objects':
        from backend.core.macos_git_write import validate,decode_input
        from backend.core.macos_git_read import view_from_wire
        validate(request['query'],decode_input(request['input']),request['author'])
        view_from_wire(request['objects'],request['view_sha256'])
        return identifier
    if request['op']=='git_read':
        from backend.core.macos_git_read import read_command, view_from_wire
        read_command('/private/tmp/gitgo-query-validation', request['query'])
        view_from_wire(request['objects'], request['view_sha256'])
        return identifier
    tool=request['request']
    if (type(tool) is not dict or set(tool)!={'tool_name','args','_native_sandbox_workspace'} or
            tool['tool_name']!='exec_command' or type(tool['args']) is not dict or
            type(tool['_native_sandbox_workspace']) is not str or
            tool['args'].get('_workspace')!=tool['_native_sandbox_workspace']):
        raise ValueError('Only the fixed native command producer is supported')
    # _workspace is translated as a role, never used for filesystem access.
    if any(k.startswith('_') and k!='_workspace' for k in tool['args']):
        raise ValueError('Unrecognized Host-private argument')
    wire.validate_image(request['image'])
    return identifier


def recovery_status(root, request, approved):
    """Read only: never starts, signals, replays or exports task output."""
    from backend.core.sandbox_macos import lease_root, load_state
    identifier=request['invocation_id']
    binding=json.loads(identity.bounded_read(root/'private'/('invocation-'+identifier+'.json'),0,True))
    durable=binding.get('lease_store')=='persistent-v1'
    observed_boot=identity.boot_id() if durable else approved['boot']
    expected=dict(invocation_id=identifier,execution_basis=request['execution_basis'],
                  grant_id=request.get('original_grant_id',approved['grant_id']),owner_uid=approved['owner_uid'],
                  project_identity=approved['identity'],boot=binding['boot'] if durable else approved['boot'])
    if 'project_object' in approved:
        expected['project_object']=approved['project_object']
        # Preserve the original boot's dev/inode tuple as historical evidence.
        # Cross-mount equivalence requires the pre-recorded persistent object.
        if durable and binding['boot']!=observed_boot:
            prior=binding.get('project_identity')
            if type(prior) is not list or len(prior)!=3 or prior[1:]!=approved['identity'][1:]:
                raise ValueError('Historical project inode or owner changed')
            expected['project_identity']=prior
    if any(binding.get(k)!=v for k,v in expected.items()): raise ValueError('Recovery binding changed')
    lease=binding.get('lease_id')
    if type(lease) is not str or not re.fullmatch('[0-9a-f]{32}',lease): raise ValueError('Invalid recovery lease')
    directory=lease_root(binding);state=load_state(directory)
    if (state['invocation_id']!=identifier or state['uid']!=binding['uid'] or state['boot_id']!=expected['boot']):
        raise ValueError('Recovery lease identity changed')
    if observed_boot!=expected['boot']:
        from backend.core.macos_boot_recovery import verify_recovery
        verify_recovery(directory,state,observed_boot)
        expected.update(origin_boot=expected['boot'],boot=observed_boot)
    cleanup=state.get('cleanup') or {}
    complete=(state['phase']=='clean' and state['launch_count']==1 and cleanup.get('observed_empty') is True
              and not cleanup.get('issues') and not identity.owned_rows(state['uid']))
    return dict(event='status',**expected,uid=state['uid'],cleanup=dict(complete=complete,mode='macos_uid',
        invocation_id=identifier,issues=[] if complete else ['recovery_not_confirmed']),outcome='unknown')


def export_image(work,uid, *, object_files=False):
    helper=identity.low_spawn([identity.PYTHON,'-I','-c',EXPORT.replace('stat.S_IMODE(info.st_mode)','0o644') if object_files else EXPORT],uid,work,
        {'PATH':'/usr/bin:/bin','HOME':str(work),'TMPDIR':str(work)},stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        out,err=helper.communicate(timeout=5)
        if helper.returncode: raise RuntimeError('Untrusted output could not be captured: '+err.decode(errors='replace')[-500:])
        value=json.loads(out);wire.validate_image(value);return value
    finally:
        if helper.poll() is None: helper.kill();helper.wait(timeout=3)


def execute(connection, request, root, approved):
    identifier=request['invocation_id'];proc=None;trial=None;owner_alive=True
    base=Path(tempfile.mkdtemp(prefix='gitgo-macos-chain-',dir='/private/tmp'));base.chmod(0o700)
    work=base/'work';work.mkdir(mode=0o700)
    try:
        from backend.core.macos_helper_lifecycle import managed
        persistent=managed(root)
        with execution_trial(work,persistent=persistent,wall_seconds=request['timeout']) as trial:
            mapping=dict(
                invocation_id=identifier,execution_basis=request['execution_basis'],grant_id=approved['grant_id'],
                owner_uid=approved['owner_uid'],project_identity=approved['identity'],boot=approved['boot'],
                uid=trial['uid'],lease_id=trial['root'].name,
                lease_store='persistent-v1' if persistent else 'temporary')
            if 'project_object' in approved:mapping['project_object']=approved['project_object']
            identity.atomic_json(root/'private'/('invocation-'+identifier+'.json'),mapping)
            identity.atomic_json(root/'private/active.json',{'invocation_id':identifier,'lease':str(trial['root'])})
            git_read=request['op']=='git_read'
            git_objects=request['op']=='git_objects'
            git_checkout=request['op']=='git_checkout'
            git_operation=git_read or git_objects or git_checkout
            environment={}
            if git_operation:
                from backend.core.macos_git_read import read_command, view_from_wire
                # Root writes only validated bytes into its own fresh private
                # view. No source project pathname, config or refs reach Git.
                destination=work/'object-view'
                view_from_wire(request['objects'],request['view_sha256']).materialize(destination)
                if git_checkout:
                    from backend.core.macos_git_checkout import command as checkout_command
                    checkout=work/'checkout';checkout.mkdir(mode=0o700)
                    os.chown(checkout,trial['uid'],trial['uid'])
                    command=checkout_command(destination,checkout,request['commit']);payload=None
                elif git_objects:
                    from backend.core.macos_git_write import command as producer_command,decode_input
                    payload=decode_input(request['input'])
                    command,environment=producer_command(destination,request['query'],payload,request['author'])
                    # Only our freshly materialized private object directories.
                    for directory,_,_ in os.walk(destination/'objects'):
                        os.chown(directory,trial['uid'],trial['uid']);os.chmod(directory,0o700)
                else:command=read_command(destination,request['query']);payload=None
            else:
                wire.materialize(request['image'],work,uid=trial['uid'])
                tool=request['request'];old=tool['_native_sandbox_workspace']
                args={**tool['args'],'_workspace':str(work)}
                if args.get('workspace_path')==old: args['workspace_path']=str(work)
                payload={'tool_name':'exec_command','args':args,'_native_sandbox_workspace':str(work)}
                command=tool_runner_command()
            # Registration loads the actual required dylibs before policy build.
            from backend.core.tools.runner import handler_bindings
            handler_bindings()
            from backend.core.macos_resource_guard import validate
            limits=validate(approved.get('resources',dict(memory_bytes=512*1024**2,process_limit=32,cpu_seconds=120)))
            # CPU budget and elapsed-time deadline are separate contracts.
            proc=sandbox_popen(command,SandboxPolicy(work,**limits),
                invocation_id=identifier,cwd=str(work if git_operation else owned_child_cwd(Path(__file__).resolve().parents[2])),
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=not git_objects,env=environment)
            wire.send(connection,{'event':'started','invocation_id':identifier,'pid':proc.pid})
            communication=BoundedCommunication(proc,limit=100000 if git_checkout else 4096 if git_operation else 2_000_000)
            first=True;end=time.monotonic()+request['timeout']
            reason='complete'
            while True:
                if select.select([connection],[],[],0)[0]:
                    try:
                        command=wire.receive(connection)
                        if command!={'op':'cancel'}: raise ValueError('Invalid control command')
                        reason='cancel'
                    except (EOFError,OSError): owner_alive=False;reason='owner_eof'
                    proc.kill()
                try:
                    current=grant(root)
                    if current!=approved: raise ValueError('Grant changed')
                    fd=wire.open_directory(current['project'])
                    try:
                        from backend.core.macos_project_identity import check_directory
                        check_directory(fd,current)
                    finally: os.close(fd)
                except (ValueError,OSError):
                    reason='grant_revoked';proc.kill()
                if time.monotonic()>=end:
                    reason='timeout';proc.kill()
                try:
                    out,err=communication.communicate(input=(payload if git_objects else json.dumps(payload)) if first and payload is not None else None,timeout=.15)
                    break
                except subprocess.TimeoutExpired:
                    first=False
                    if time.monotonic()>end+20: raise TimeoutError('Recovery outcome unavailable')
            proc.close_sandbox()
            if getattr(proc,'resource_reason',None):reason=proc.resource_reason
            cleanup=proc._gitgo_cleanup
            if not cleanup['complete'] or cleanup['issues']: raise RuntimeError('UID cleanup not confirmed')
            image=export_image(work,trial['uid']) if not git_operation and proc.returncode==0 and reason=='complete' else {'files':[],'directories':[]}
            response={'event':'finished','invocation_id':identifier,'cleanup':cleanup,
                'exit_code':proc.returncode if reason=='complete' else -1,'reason':reason,
                'stdout':out,'stderr':err,'image':image}
            if git_objects and proc.returncode==0 and reason=='complete':
                image=export_image(destination/'objects',trial['uid'],object_files=True)
                image['directories']=[]
                response['objects']=image
            if git_checkout and proc.returncode==0 and reason=='complete':
                # The exporter also drops to the retired UID. Root never walks
                # task-authored working files, even after the process exits.
                response['checkout']=export_image(checkout,trial['uid'])
                response['commit']=request['commit']
            if git_operation:
                response.pop('image')
                response['view_sha256']=request['view_sha256']
            # Exporter is our direct reaped low-UID child; verify it too.
            if identity.owned_rows(trial['uid']): raise RuntimeError('Post-export identity not empty')
            identity.atomic_json(root/'private/last-result.json',response)
            if owner_alive: wire.send(connection,response)
    finally:
        if proc:
            try: proc.close_sandbox()
            finally:
                for stream in (proc.stdin,proc.stdout,proc.stderr):
                    if stream: stream.close()


def serve(root):
    if os.getuid()!=0 or os.geteuid()!=0: raise PermissionError('Broker requires root')
    root=Path(root).absolute()
    from backend.core.macos_helper_lifecycle import managed, validate_service_entry, locked
    persistent=managed(root)
    if not persistent and (root.parent!=Path('/private/tmp') or not root.name.startswith('gitgo-macos-broker-')):
        raise ValueError('Invalid broker installation')
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
            raise ValueError('Broker path is not root-owned')
        identity.require_no_extended_acl(directory)
    finally: os.close(directory)
    if persistent: validate_service_entry(root)
    with locked(root,'broker.lock') if persistent else nullcontext():
        if persistent:
            from backend.core.macos_boot_recovery import recover_store, RecoveryBusy, RecoveryTimeout
            from backend.core.macos_helper_lifecycle import begin_startup, startup_progress
            attempt=begin_startup(root,str(Path(sys.executable).absolute()))
            def progress(value):
                startup_progress(root,attempt,**value,
                    error='RECOVERY_LOCK_BUSY' if value['phase']=='recover_busy' else None)
            stage='recovery'
            try:
                recover_store(progress=progress)
                stage='listener'
                return listen(root,persistent=True,on_ready=lambda:
                    startup_progress(root,attempt,phase='ready'))
            except Exception as failure:
                error=('RECOVERY_LOCK_BUSY' if isinstance(failure,RecoveryBusy) else
                       'RECOVERY_TIMEOUT' if isinstance(failure,RecoveryTimeout) else
                       'RECOVERY_FAILED' if stage=='recovery' else 'STARTUP_FAILED')
                try: startup_progress(root,attempt,phase='failed',error=error)
                except (OSError,ValueError,RuntimeError): pass  # Preserve the actual failure.
                raise
        return listen(root,persistent=persistent)


def negotiate(connection,fd,request,root,approved):
    """Negotiate under admission.lock, on the authenticated launch connection.

    Older clients retain their existing strict operation contracts. New clients
    must bind the following action to this exact probe; probing never reserves
    an invocation, starts a process, or weakens the final live authorization.
    """
    from backend.core import macos_broker_capabilities as capabilities
    if request['op']!=capabilities.CAPABILITY_OPERATION:
        return request,approved
    deadline=time.monotonic()+5
    wire.send(connection,capabilities.reply(request,approved,boot=identity.boot_id()))
    action=wire.receive_until(connection,deadline)
    if action.get('op') not in capabilities.OPERATION_CONTRACTS:
        raise ValueError('Capability negotiation requires one functional operation')
    for key in ('token','grant_id','invocation_id','execution_basis'):
        if type(action.get(key)) is not type(request[key]) or action[key]!=request[key]:
            raise PermissionError('Invocation changed after capability negotiation')
    approved=grant(root)
    authorize(connection,fd,action,approved)
    return action,approved


def listen(root,*,persistent=False,on_ready=None):
    from backend.core.macos_helper_lifecycle import locked
    value=grant(root,live=False)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    address=root/'broker.sock'
    if address.exists(): address.unlink()
    listener.bind(str(address));os.chown(address,value['owner_uid'],-1);address.chmod(0o600)
    listener.listen(4);listener.settimeout(.5)
    ledger=root/'private/seen.json'
    if not ledger.exists(): identity.atomic_json(ledger,[])
    try:
        if on_ready is not None: on_ready()
        count=0
        while persistent or count<64:
            count+=1
            while True:
                grant(root,live=False)
                try: connection,_=listener.accept();break
                except socket.timeout: continue
            fd=None;request={};accepted=False
            try:
                connection.settimeout(5)
                approved=grant(root,live=False)
                if wire.peer_uid(connection)!=approved['owner_uid']: raise PermissionError('Wrong peer user')
                fd=wire.receive_directory(connection);request=wire.receive(connection)
                identifier=authorize(connection,fd,request,approved)
                if request['op']=='status':
                    wire.send(connection,recovery_status(root,request,approved))
                    continue
                with locked(root,'admission.lock') if persistent else nullcontext():
                    approved=grant(root)
                    # Renewal can occur after the first authentication but
                    # before this lock. Never execute using stale credentials.
                    identifier=authorize(connection,fd,request,approved)
                    request,approved=negotiate(connection,fd,request,root,approved)
                    seen=json.loads(identity.bounded_read(ledger,0,True))
                    if (not isinstance(seen,list) or len(seen)>=(4096 if persistent else 64) or identifier in seen or
                            any(type(item) is not str or not re.fullmatch('[0-9a-f]{32}',item) for item in seen)):
                        raise ValueError('Replay or invalid invocation ledger')
                    seen.append(identifier)
                    identity.atomic_json(ledger,seen)
                    accepted=True
                    execute(connection,request,root,approved)
            except Exception as exc:
                identity.atomic_json(root/'private/last-error.json',{'type':type(exc).__name__,'detail':str(exc),'accepted':accepted})
                if not accepted:
                    try: wire.send(connection,denial(request,exc))
                    except OSError: pass
                # After reservation a lost response remains unknown; do not
                # manufacture not_started or run the invocation again.
            finally:
                if fd is not None: os.close(fd)
                connection.close()
    finally:
        listener.close()


if __name__=='__main__': serve(sys.argv[1])
